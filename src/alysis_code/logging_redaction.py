"""Write-path secret redaction for Alysis Code log and telemetry sinks.

Background: a live ``ALYSIS_API_KEY`` value once reached a session JSONL file
in cleartext. A tool call dumped the process environment and the tool output was
logged verbatim. An archive-time scanner caught it after the fact, but by then
the credential had already been written to disk. This module closes that gap at
the write boundary: each Alysis Code log/telemetry sink funnels its final
serialized string through :func:`redact_log_text` immediately before the write,
so a captured secret value cannot reach disk in the first place.

The archive-time scanner in ``feedback_report`` is deliberately left untouched
and remains the second, independent layer.

Design constraints:

* Stdlib only, with no intra-package imports. Every sink module must be able to
  import this one without creating a cycle and without dragging in heavy
  third-party dependencies (httpx, pydantic, rich).
* Deterministic and idempotent: ``redact(redact(t)) == redact(t)``.
* Fast: one precompiled alternation covers every known spelling of every
  captured value. Risky assignment matching uses a lowercase view for ASCII
  logs while retaining original payloads and syntax; Unicode logs use the full
  case-insensitive matcher. Run ``python -m alysis_code.logging_redaction`` for
  a micro-benchmark.

What is captured
----------------
At construction the redactor snapshots the *values* of environment variables
whose names end in ``_API_KEY``, ``_TOKEN``, ``_SECRET`` or ``_PASSWORD`` (or
that are exactly ``API_KEY``/``TOKEN``/``SECRET``/``PASSWORD``), plus any
``ALYSIS_*`` variable whose value looks credential-like. Values shorter than
``MIN_VALUE_LENGTH`` are ignored: they collide with ordinary log prose far too
often to be replaced safely.

Known limitations (deliberate)
------------------------------
* Base64 is matched only for the exact UTF-8 bytes of the value at alignment 0
  (standard and URL-safe, padded and unpadded). A secret base64-encoded as part
  of a *larger* blob lands at a shifted alignment and is not detected; the
  archive-time scanner remains the backstop for that case.
* A secret deliberately split across two fields (for example the first half in
  one JSON value and the second half in another) is not reassembled. Each half
  is redacted only if it independently matches a captured value.
* The risky-pattern pass requires a high-entropy payload, so a low-entropy
  placeholder such as ``password=aaaaaaaaaaaaaaaa`` is left alone on purpose.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
from collections import Counter
from collections.abc import Mapping
from urllib.parse import quote, quote_plus

__all__ = [
    "MIN_CREDENTIAL_LENGTH",
    "MIN_VALUE_LENGTH",
    "SecretRedactor",
    "default_redactor",
    "redact_log_text",
    "reset_default_redactor",
]

# Guillemets keep the marker visually distinct and, more importantly, outside the
# payload character classes below, which is what makes redaction idempotent by
# construction rather than by convention.
_PLACEHOLDER_PREFIX = "«redacted:"
_PLACEHOLDER_SUFFIX = "»"
_PATTERN_PLACEHOLDER = f"{_PLACEHOLDER_PREFIX}pattern{_PLACEHOLDER_SUFFIX}"

#: Values shorter than this are never captured: too collision-prone.
MIN_VALUE_LENGTH = 8
#: Length floor for the ``ALYSIS_*`` "looks like a credential" heuristic.
MIN_CREDENTIAL_LENGTH = 16
#: Shannon entropy floor, in bits per character.
MIN_ENTROPY_BITS_PER_CHAR = 3.0
#: Entropy floor for a payload found by the risky-pattern pass.
MIN_PATTERN_ENTROPY_BITS_PER_CHAR = 3.0

_SECRET_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_SECRET_NAME_EXACT = frozenset({"API_KEY", "TOKEN", "SECRET", "PASSWORD"})
_HEURISTIC_NAME_PREFIX = "ALYSIS_"

# Characters a credential may plausibly be made of. Excludes whitespace, quotes
# and braces so that a JSON blob or an English sentence can never be mistaken
# for one value.
_TOKEN_CHARSET_RE = re.compile(r"[A-Za-z0-9._~+/=:-]+")
_PATHISH_RE = re.compile(r"(?:\A[./~]|\A[A-Za-z]:[\\/]|://)")
_HEXISH_RE = re.compile(r"[0-9a-fA-F]+")


def _shannon_entropy_bits_per_char(value: str) -> float:
    """Return Shannon entropy of ``value`` in bits per character."""
    if not value:
        return 0.0
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in Counter(value).values())


def _is_named_secret(name: str) -> bool:
    upper = name.upper()
    return upper in _SECRET_NAME_EXACT or upper.endswith(_SECRET_NAME_SUFFIXES)


def _looks_credential_like(value: str) -> bool:
    """Heuristic for ``ALYSIS_*`` values whose name does not give them away.

    Deliberately conservative about *shape* rather than length alone, so that
    ordinary configuration such as a model id or a file path is not swept up.
    """
    if len(value) < MIN_CREDENTIAL_LENGTH:
        return False
    if not _TOKEN_CHARSET_RE.fullmatch(value):
        return False
    if _PATHISH_RE.search(value):
        return False
    if _shannon_entropy_bits_per_char(value) < MIN_ENTROPY_BITS_PER_CHAR:
        return False
    has_lower = any(char.islower() for char in value)
    has_upper = any(char.isupper() for char in value)
    has_digit = any(char.isdigit() for char in value)
    if has_lower and has_upper and has_digit:
        return True
    # Long single-case hex digests are credential-shaped even though they only
    # use two character classes.
    return len(value) >= 32 and _HEXISH_RE.fullmatch(value) is not None


def _value_variants(value: str) -> list[str]:
    """Every spelling of ``value`` that could plausibly be written to a log."""
    variants = {value}
    # JSON string escaping. Sinks serialize with ensure_ascii=True, but cover
    # both settings so a non-ASCII secret is caught either way.
    for ensure_ascii in (True, False):
        variants.add(json.dumps(value, ensure_ascii=ensure_ascii)[1:-1])
    # Percent-encoding, as seen in URLs and form bodies.
    variants.add(quote(value, safe=""))
    variants.add(quote_plus(value, safe=""))
    # Base64 of the exact bytes at alignment 0 (see module docstring).
    raw = value.encode("utf-8")
    for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = encoded.decode("ascii")
        variants.add(text)
        variants.add(text.rstrip("="))
    return [variant for variant in variants if len(variant) >= MIN_VALUE_LENGTH]


# Assignment-style and Authorization-header patterns whose payload is long and
# high-entropy. The payload class excludes the guillemets used by the
# placeholders, so an already-redacted string can never be re-matched.
_RISKY_PATTERN = r"""
    (?:
        \b(?:
            api[-_]?key | access[-_]?key | secret[-_]?key | client[-_]?secret
            | auth[-_]?token | access[-_]?token | refresh[-_]?token | api[-_]?token
            | authorization | token | secret | password | passwd
        )\b
        \s* ["']? \s* [:=] \s* ["']? \s*
        (?: (?: bearer | basic | token ) \s+ )?
        (?P<assigned> [A-Za-z0-9._~+/=-]{16,})
        |
        \b bearer \s+ (?P<bearer> [A-Za-z0-9._~+/=-]{16,})
    )
    """
_RISKY_PATTERN_RE = re.compile(_RISKY_PATTERN, re.VERBOSE | re.IGNORECASE)
_LOWERCASE_RISKY_PATTERN_RE = re.compile(_RISKY_PATTERN, re.VERBOSE)


def _replace_risky(match: re.Match) -> str:
    assigned = match.group("assigned")
    if assigned is not None:
        payload, payload_start = assigned, match.start("assigned")
    else:
        payload, payload_start = match.group("bearer"), match.start("bearer")
    if payload.startswith(_PLACEHOLDER_PREFIX):
        return match.group(0)
    if _shannon_entropy_bits_per_char(payload) < MIN_PATTERN_ENTROPY_BITS_PER_CHAR:
        # A low-entropy payload is almost always a placeholder or an enum value;
        # redacting it would only make real logs harder to read.
        return match.group(0)
    # The payload is always the tail of the match, so keeping everything before
    # it preserves the surrounding syntax (including JSON quoting).
    return match.group(0)[: payload_start - match.start()] + _PATTERN_PLACEHOLDER


def _redact_ascii_risky(text: str) -> str:
    # The caller proves that the original input and placeholder names are ASCII.
    # Exact substitutions can add only the non-word guillemets, whose lowercase
    # spelling and width are unchanged. Match offsets therefore still refer to
    # the original text. Entropy must use original casing: AaBbCcDd has more
    # entropy than aabbccdd, even though the matching view is lowercase.
    parts: list[str] = []
    previous_end = 0
    for match in _LOWERCASE_RISKY_PATTERN_RE.finditer(text.lower()):
        group = "assigned" if match.group("assigned") is not None else "bearer"
        payload_start, payload_end = match.span(group)
        payload = text[payload_start:payload_end]
        if _shannon_entropy_bits_per_char(payload) < MIN_PATTERN_ENTROPY_BITS_PER_CHAR:
            continue
        parts.extend((text[previous_end:payload_start], _PATTERN_PLACEHOLDER))
        previous_end = payload_end
    if not parts:
        return text
    parts.append(text[previous_end:])
    return "".join(parts)


class SecretRedactor:
    """An immutable snapshot of the secret values known at construction time.

    Pass ``environ`` explicitly to get a deterministic redactor in tests; the
    default reads :data:`os.environ`.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        source = os.environ if environ is None else environ
        replacements: dict[str, str] = {}
        names: list[str] = []
        for name, value in sorted(source.items()):
            if not isinstance(value, str) or len(value) < MIN_VALUE_LENGTH:
                continue
            if _is_named_secret(name):
                pass
            elif name.startswith(_HEURISTIC_NAME_PREFIX) and _looks_credential_like(value):
                pass
            else:
                continue
            names.append(name)
            placeholder = f"{_PLACEHOLDER_PREFIX}{name}{_PLACEHOLDER_SUFFIX}"
            for variant in _value_variants(value):
                # Iteration is sorted by name, so if two variables share a value
                # the alphabetically-first name wins. Deterministic either way.
                replacements.setdefault(variant, placeholder)
        self.secret_names: tuple[str, ...] = tuple(names)
        self._ascii_placeholder_names = all(name.isascii() for name in names)
        self._replacements = replacements
        self._exact_re = self._compile(replacements)

    @staticmethod
    def _compile(replacements: Mapping[str, str]) -> re.Pattern | None:
        if not replacements:
            return None
        # Longest first, so that when one variant is a prefix of another the
        # longer (more specific) spelling wins at any given position.
        ordered = sorted(replacements, key=lambda variant: (-len(variant), variant))
        return re.compile("|".join(re.escape(variant) for variant in ordered))

    def _replace_exact(self, match: re.Match) -> str:
        return self._replacements[match.group(0)]

    def redact(self, text: str) -> str:
        """Return ``text`` with every known secret value and risky payload masked."""
        if not text:
            return text
        ascii_input = text.isascii() and self._ascii_placeholder_names
        if self._exact_re is not None:
            text = self._exact_re.sub(self._replace_exact, text)
        if ascii_input:
            return _redact_ascii_risky(text)
        return _RISKY_PATTERN_RE.sub(_replace_risky, text)


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_REDACTOR: SecretRedactor | None = None
_DEFAULT_SIGNATURE: tuple[tuple[str, str], ...] | None = None


def _environ_signature(source: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Cheap fingerprint of the secret-bearing part of the environment."""
    matches = [
        (name, value)
        for name, value in source.items()
        if isinstance(value, str)
        and len(value) >= MIN_VALUE_LENGTH
        and (_is_named_secret(name) or name.startswith(_HEURISTIC_NAME_PREFIX))
    ]
    matches.sort()
    return tuple(matches)


def default_redactor() -> SecretRedactor:
    """Return the process-wide redactor, rebuilding it if the environment moved.

    Credentials are often loaded into the environment after import, so a
    build-once cache would miss exactly the value that matters. Comparing a
    cheap signature keeps the compiled regex hot while staying correct.
    """
    global _DEFAULT_REDACTOR, _DEFAULT_SIGNATURE
    signature = _environ_signature(os.environ)
    with _DEFAULT_LOCK:
        if _DEFAULT_REDACTOR is None or signature != _DEFAULT_SIGNATURE:
            _DEFAULT_REDACTOR = SecretRedactor()
            _DEFAULT_SIGNATURE = signature
        return _DEFAULT_REDACTOR


def reset_default_redactor() -> None:
    """Drop the cached redactor. Intended for tests."""
    global _DEFAULT_REDACTOR, _DEFAULT_SIGNATURE
    with _DEFAULT_LOCK:
        _DEFAULT_REDACTOR = None
        _DEFAULT_SIGNATURE = None


def redact_log_text(text: str) -> str:
    """Redact ``text`` immediately before it is written to a log sink."""
    if not text:
        return text
    return default_redactor().redact(text)


def _benchmark(size_mb: float = 100.0) -> None:
    """Micro-benchmark the redaction budget: 100MB in well under a minute."""
    import time

    secret = "sk-syl-" + ("A7bQ9zX2mK4pL8vN" * 2)
    environ = {"ALYSIS_API_KEY": secret, "PATH": "/usr/bin:/bin"}
    redactor = SecretRedactor(environ)
    block = (
        "2026-08-21T10:00:00Z level=info msg=tool_result "
        'payload={"stdout": "HOME=/root\\nSHELL=/bin/bash\\n"} '
        f"ALYSIS_API_KEY={secret} "
        "Authorization: Bearer q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6 "
        "ordinary log prose that must survive untouched\n"
    )
    repeats = max(1, int((size_mb * 1024 * 1024) / len(block)))
    text = block * repeats
    actual_mb = len(text) / (1024 * 1024)

    start = time.perf_counter()
    result = redactor.redact(text)
    elapsed = time.perf_counter() - start

    print(f"input        : {actual_mb:.1f} MB ({repeats} blocks)")
    print(f"elapsed      : {elapsed:.3f} s")
    print(f"throughput   : {actual_mb / elapsed:.1f} MB/s")
    print(f"secret leaked: {secret in result}")
    print(f"projected 100MB: {elapsed * (100.0 / actual_mb):.2f} s")

    start = time.perf_counter()
    again = redactor.redact(result)
    print(f"idempotent   : {again == result} (second pass {time.perf_counter() - start:.3f} s)")


if __name__ == "__main__":
    _benchmark()
