"""Unicode scalar text at boundaries receiving UTF-16 strings from UI hosts."""

from __future__ import annotations

import re

_SURROGATE = re.compile(r"[\ud800-\udfff]")


def well_formed_text(value: str) -> str:
    """Replace isolated UTF-16 surrogates without altering valid Unicode text."""
    if _SURROGATE.search(value) is None:
        return value
    return value.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")
