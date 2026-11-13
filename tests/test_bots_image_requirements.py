"""The owl computer image refuses requirements without the engine's document toolkit."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bots-local"))

import image_requirements  # noqa: E402

HASH = "    --hash=sha256:" + "0" * 64


def export(*names: str) -> str:
    return "\n".join(f"{name}==1.0 \\\n{HASH}" for name in names) + "\n"


def test_an_export_without_the_documents_extra_is_named_and_refused(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text(export("httpx", "pydantic", "lxml"), encoding="utf-8")
    assert image_requirements.missing_document_packages(plain) == sorted(
        image_requirements.DOCUMENT_PACKAGES - {"lxml"}
    )
    full = tmp_path / "full.txt"
    full.write_text(
        export(
            "httpx",
            "python-docx",
            "OpenPyXL",
            "XlsxWriter",
            "python_pptx",
            "pypdfium2",
            "reportlab",
            "lxml",
        ),
        encoding="utf-8",
    )
    assert image_requirements.missing_document_packages(full) == []


def test_the_engine_lock_provides_the_documents_extra():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    block = pyproject.split("documents = [", 1)[1].split("]", 1)[0].lower()
    for package in image_requirements.DOCUMENT_PACKAGES:
        assert package in block, package
