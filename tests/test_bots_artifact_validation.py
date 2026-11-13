import io
import threading
from concurrent.futures import ThreadPoolExecutor
from zipfile import ZipFile

import pytest
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from alysis_code.bots import artifact_validation
from alysis_code.bots.desktop_worker import Computer, Rejected
from alysis_code.office.rendering import _check_renderable


def test_private_checker_keeps_control_responsive_and_fences_result_identity(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_check(root, name):
        started.set()
        assert release.wait(5)
        return {"items": [], "path": name}

    monkeypatch.setattr(artifact_validation, "check_file", slow_check)
    computer = Computer.__new__(Computer)
    computer._check = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        computer._check_pool = pool
        try:
            receipt = computer.dispatch("check_file_start", {"name": "report.pdf"})
            assert started.wait(2)
            assert computer.dispatch("check_file_result", receipt) == {"running": True}
            with pytest.raises(Rejected):
                computer.dispatch("check_file_result", {"check_id": "another-check"})
            with pytest.raises(Rejected):
                computer.dispatch("check_file_start", {"name": "another.pdf"})
            release.set()
            computer._check[1].result(timeout=2)
            result = computer.dispatch("check_file_result", receipt)
            assert result == {"running": False, "report": {"items": [], "path": "report.pdf"}}
            with pytest.raises(Rejected):
                computer.dispatch("check_file_result", receipt)
        finally:
            release.set()


@pytest.mark.parametrize("embedded", ["ordinary", "macro", "external", "program"])
def test_chart_data_is_renderable_but_embedded_active_content_is_rejected(tmp_path, embedded):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["Athens", "Tokyo"]
    data.add_series("Amount", [17, 42])
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(8), Inches(4), data
    )
    source = tmp_path / "chart.pptx"
    presentation.save(source)
    if embedded != "ordinary":
        target = tmp_path / "changed.pptx"
        with ZipFile(source) as package, ZipFile(target, "w") as output:
            for entry in package.infolist():
                content = package.read(entry.filename)
                if "/embeddings/" in entry.filename:
                    if embedded == "program":
                        content = b"executable payload disguised as xlsx"
                    else:
                        workbook = io.BytesIO()
                        with (
                            ZipFile(io.BytesIO(content)) as original,
                            ZipFile(workbook, "w") as changed,
                        ):
                            for part in original.infolist():
                                changed.writestr(part, original.read(part.filename))
                            if embedded == "macro":
                                changed.writestr("xl/vbaProject.bin", b"program")
                            else:
                                changed.writestr(
                                    "xl/_rels/external.xml.rels",
                                    '<Relationships><Relationship Type="external-data" TargetMode="External" Target="https://example.invalid/data"/></Relationships>',
                                )
                        content = workbook.getvalue()
                output.writestr(entry, content)
        source = target
    if embedded == "ordinary":
        _check_renderable(source)
    else:
        with pytest.raises((ValueError, OSError)):
            _check_renderable(source)
