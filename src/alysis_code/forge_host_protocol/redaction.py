"""Shared secret and private-host-path redaction policy for protocol transport."""

from __future__ import annotations

import re

SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:authorization|cookie)\s*[:=]"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:"
        r"(?:[a-z][a-z0-9]*[_-])?api[_-]?key|"
        r"aws[_-]?secret[_-]?access[_-]?key|"
        r"access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|passwd|secret|token"
        r")\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}"
    ),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ),
    re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)[?&](?:api[_-]?key|access[_-]?token|password|secret|token)="),
)

# A single slash preceded by punctuation is an absolute-path boundary unless it
# is the first half of ``://``.  This catches routine diagnostics such as
# ``failed:/Users/...`` and quoted/bracketed paths without treating HTTPS URLs as
# local paths.
PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?i)\b(?:file|smb|afp|nfs)://"),
    re.compile(r"(?<!:)//[A-Za-z0-9._-]+(?:/[^\s,;]+)+"),
    re.compile(r"(?<![A-Za-z0-9/])/(?!/)(?:[^/\s,;]+/)*[^\s,;]*"),
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/](?:[^\s,;]*)?"),
    re.compile(r"(?<![A-Za-z0-9])\\\\[^\\\s]+\\[^\s,;]+"),
    re.compile(r"(?<![A-Za-z0-9])~/(?:[^\s,;]+)"),
)


def contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def contains_private_host_path(value: str) -> bool:
    return any(pattern.search(value) for pattern in PRIVATE_PATH_PATTERNS)


def validate_redacted_runtime_text(value: str) -> str:
    if contains_secret(value) or contains_private_host_path(value):
        raise ValueError("runtime text must be redacted before transport")
    return value
