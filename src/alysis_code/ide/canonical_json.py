"""Canonical JSON for Computer Use request fingerprints (Alysis canonical JSON v1).

The desktop app recomputes ``arguments_fingerprint`` from the arguments it parses
off the wire, using ``canonicalJson`` in ``electron/runtime/ideBridgeProtocol.ts``.
Both sides must produce the same text byte for byte, so this module writes what
ECMAScript ``JSON.stringify`` writes for the value ``JSON.parse`` reads from our
output, with object keys sorted by Unicode code point:

* Numbers use ECMAScript ``Number::toString`` of the IEEE-754 binary64 value:
  ``512.0`` is ``512``, ``1e-05`` is ``0.00001``, ``1e21`` is ``1e+21`` and ``-0.0``
  is ``0``. Integers are first rounded to binary64, as ``JSON.parse`` rounds them.
* Strings escape only ``"``, ``\\``, C0 controls (``\\b \\t \\n \\f \\r`` short
  forms, lowercase ``\\u00xx`` otherwise) and lone surrogates (lowercase
  ``\\udxxx``). Everything else, including U+007F and U+2028, is written as is.
* Object keys are ordered by code point. This is not RFC 8785, which orders by
  UTF-16 code unit, and not a locale order.

Non-finite numbers and values outside the JSON data model have no canonical form
and raise ``CanonicalJsonError``.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

__all__ = ["CanonicalJsonError", "canonical_json", "canonical_json_fingerprint"]

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class CanonicalJsonError(ValueError):
    """The value has no Alysis canonical JSON v1 form."""


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text of ``value``."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        try:
            return _number(float(value))
        except OverflowError:
            raise CanonicalJsonError("integer is outside the binary64 range") from None
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return _string(_paired(value))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        members: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError("object keys must be strings")
            paired = _paired(key)
            if paired in members:
                raise CanonicalJsonError("object keys collide after surrogate pairing")
            members[paired] = child
        return (
            "{"
            + ",".join(_string(key) + ":" + canonical_json(members[key]) for key in sorted(members))
            + "}"
        )
    raise CanonicalJsonError(f"{type(value).__name__} is not a JSON value")


def canonical_json_fingerprint(value: Any) -> str:
    """Return ``sha256:<hex>`` over the UTF-8 canonical JSON of ``value``."""

    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise CanonicalJsonError("numbers must be finite")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + _number(-value)
    # repr() gives the shortest round-trip digits, nearest to the exact value when
    # several are equally short: the same digits ECMAScript Number::toString picks.
    mantissa, _, exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    padded = whole + fraction
    stripped = padded.lstrip("0")
    # value == 0.<digits> * 10**point
    point = len(whole) + int(exponent or "0") - (len(padded) - len(stripped))
    digits = stripped.rstrip("0")
    count = len(digits)
    if count <= point <= 21:
        return digits + "0" * (point - count)
    if 0 < point <= 21:
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return "0." + "0" * -point + digits
    scale = point - 1
    leading = digits if count == 1 else digits[0] + "." + digits[1:]
    return f"{leading}e{'+' if scale >= 0 else '-'}{abs(scale)}"


def _paired(text: str) -> str:
    # Adjacent surrogate halves travel as one escaped pair, which JSON.parse reads
    # as a single code point. Join them here so ordering and escaping agree.
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def _string(text: str) -> str:
    parts = ['"']
    for char in text:
        escaped = _SHORT_ESCAPES.get(char)
        if escaped is None:
            code = ord(char)
            escaped = f"\\u{code:04x}" if code < 0x20 or 0xD800 <= code <= 0xDFFF else char
        parts.append(escaped)
    parts.append('"')
    return "".join(parts)
