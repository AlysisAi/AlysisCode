"""Native document work stops before publishing previews, outputs or check receipts."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from docx import Document
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from alysis_code.artifact_checks import ArtifactChecks
from alysis_code.execution_deadline import DeadlineExhausted, ExecutionDeadline
from alysis_code.office.operations import run_operation


@pytest.mark.parametrize("route", ["pdf_check", "pdf_create", "office_check"])
@pytest.mark.parametrize("reason", ["cancel", "deadline"])
def test_render_stops_between_pages_without_publishing(tmp_path, monkeypatch, route, reason):
    pdf_path = tmp_path / "source.pdf"
    pdf = Canvas(str(pdf_path))
    for page in range(2):
        pdf.drawString(72, 720, f"Synthetic page {page + 1}")
        pdf.showPage()
    pdf.save()
    original = pdf_path.read_bytes()
    state = {"cancelled": False, "clock": 0.0, "images": 0}
    deadline = ExecutionDeadline.from_duration(60, clock=lambda: state["clock"])

    save = Image.Image.save

    def stop_after_first_page(image, *args, **kwargs):
        save(image, *args, **kwargs)
        state["images"] += 1
        state["cancelled"] = reason == "cancel"
        state["clock"] = 61 if reason == "deadline" else 0

    monkeypatch.setattr(Image.Image, "save", stop_after_first_page)
    options = {"execution_deadline": deadline, "cancel_check": lambda: state["cancelled"]}
    service = ArtifactChecks(tmp_path, **options)
    if route == "office_check":
        from alysis_code.office import rendering

        Document().save(tmp_path / "source.docx")
        monkeypatch.setattr("alysis_code.office.renderer_path", lambda: Path(sys.executable))
        monkeypatch.setattr(rendering, "renderer_path", lambda: Path(sys.executable))

        def convert(executable, source, destination, profile, **kwargs):
            shutil.copyfile(pdf_path, destination / (source.stem + ".pdf"))

        monkeypatch.setattr(rendering, "_convert", convert)

    with pytest.raises(KeyboardInterrupt if reason == "cancel" else DeadlineExhausted):
        if route == "pdf_create":
            (tmp_path / "draft.md").write_text("# Synthetic report\n\nA short test document.")
            run_operation(
                tmp_path,
                {"action": "create", "format": "pdf", "source": "draft.md", "output": "out.pdf"},
                read_guard=lambda path: None,
                write_guard=lambda path: None,
                **options,
            )
        else:
            service.check("source.docx" if route == "office_check" else "source.pdf")

    assert state["images"] == 1
    assert not (tmp_path / "out.pdf").exists()
    assert not list((tmp_path / "Task reviews").glob("*"))
    assert not list(tmp_path.glob(".alysis-*"))
    assert service.reports() == []
    assert pdf_path.read_bytes() == original


def test_cancel_during_preview_publication_cleans_its_owned_directory(tmp_path, monkeypatch):
    from alysis_code.documents import __main__ as documents

    (tmp_path / "draft.md").write_text("# Synthetic report\n\nPublication test.")
    stopped = False
    copy = shutil.copyfile

    def stop_after_copy(*args, **kwargs):
        nonlocal stopped
        result = copy(*args, **kwargs)
        stopped = True
        return result

    monkeypatch.setattr(documents.shutil, "copyfile", stop_after_copy)
    with pytest.raises(KeyboardInterrupt):
        run_operation(
            tmp_path,
            {"action": "create", "format": "pdf", "source": "draft.md", "output": "out.pdf"},
            read_guard=lambda path: None,
            write_guard=lambda path: None,
            cancel_check=lambda: stopped,
        )
    assert stopped
    assert not (tmp_path / "out.pdf").exists()
    assert not list((tmp_path / "Task reviews").glob("*"))
    assert not list(tmp_path.glob(".alysis-*"))
