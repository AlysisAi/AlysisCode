"""What the owl computer image's Python environment must include.

Owls make and check Word, Excel and PowerPoint files and PDFs with the engine's own document
toolkit (``alysis_code.office``: create, inspect, edit, render pages), which needs the engine's
``documents`` extra. Export the image requirements with it:

    uv export --frozen --no-dev --no-emit-project --extra documents --format requirements-txt -o requirements.txt
"""

from __future__ import annotations

import re
from pathlib import Path

EXPORT = "uv export --frozen --no-dev --no-emit-project --extra documents --format requirements-txt"
DOCUMENT_PACKAGES = frozenset(
    {"python-docx", "openpyxl", "xlsxwriter", "python-pptx", "pypdfium2", "reportlab", "lxml"}
)


def _name(line: str) -> str | None:
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==", line)
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else None


def missing_document_packages(requirements: Path) -> list[str]:
    """Document packages absent from a hashed requirements export (empty when complete)."""
    names = {_name(line) for line in requirements.read_text(encoding="utf-8").splitlines()}
    return sorted(package for package in DOCUMENT_PACKAGES if package not in names)
