"""Owls make, change and check Word, Excel, PowerPoint and PDF files without their computer.

The engine's own office operations and artifact check run for real. Office rendering and
formula calculation need LibreOffice, so most tests switch it off (the checks then say they did
not render or calculate); the tests that need a renderer fake its conversion step, and the ones
that need real calculation run when ALYSIS_TEST_OFFICE_SOFFICE names an installed LibreOffice.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest
from docx import Document
from openpyxl import load_workbook
from PIL import Image
from pptx import Presentation
from pydantic import ValidationError
from reportlab.pdfgen.canvas import Canvas
from test_bots_browser_runtime import ClientFixture, response

from alysis_code.bots import owl_documents
from alysis_code.bots.browser_images import FILE_IMAGE_CAPTION, FileImageResult
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_documents import DOCUMENT_TOOLS
from alysis_code.bots.runtime import BotRuntime, tool_schema
from alysis_code.bots.store import Conflict, LimitReached, Store
from alysis_code.cancellation import CooperativeCancellationError

GREEK = "Γεια σου κόσμε"
CHINESE = "你好，世界"
TOOLS = list(DOCUMENT_TOOLS)


@pytest.fixture
def no_renderer(tmp_path, monkeypatch):
    """No LibreOffice: Office pages are not rendered and formulas are not recalculated."""
    monkeypatch.setenv("ALYSIS_OFFICE_SOFFICE", str(tmp_path / "missing" / "soffice"))


def owl(tmp_path, *, files_enabled=True, message="Φτιάξε το έγγραφο"):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot(
        "owner", BotInput(name="Γραφείο", purpose="Documents", files_enabled=files_enabled)
    )
    chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue("owner", chat["id"], TaskInput(message=message, request_id="documents-1"))
    return store, bot, store.claim("owner")


def call(runtime, task, name, arguments, *, image_input=False):
    return runtime._tool(
        task,
        name,
        arguments,
        TOOLS,
        image_input=image_input,
        token=runtime.task_token(task["id"]),
    )


def library(store, bot):
    return store.list_files("owner", bot["id"])


def scratch_left(store) -> list[Path]:
    place = Path(store.path).parent / owl_documents.SCRATCH
    return [path for path in place.rglob("*") if path.is_file()] if place.exists() else []


SPECS = {
    "docx": {
        "title": "Αναφορά",
        "blocks": [
            {"kind": "heading", "text": GREEK, "level": 1},
            {"kind": "paragraph", "text": CHINESE},
            {"kind": "table", "rows": [["Πόλη", "城市"], ["Αθήνα", "北京"]]},
        ],
    },
    "xlsx": {
        "sheets": [
            {
                "name": "Έσοδα",
                "header_row": True,
                "rows": [["Πελάτης", "Ποσό"], [GREEK, 120], [CHINESE, 80]],
            }
        ]
    },
    "pptx": {"title": "Παρουσίαση", "slides": [{"title": GREEK, "body": [CHINESE, "Αθήνα"]}]},
}


def text_of(kind: str, data: bytes) -> str:
    if kind == "docx":
        document = Document(io.BytesIO(data))
        cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
        return "\n".join([p.text for p in document.paragraphs] + cells)
    if kind == "xlsx":
        book = load_workbook(io.BytesIO(data))
        return "\n".join(
            str(cell.value) for sheet in book for row in sheet.iter_rows() for cell in row
        )
    if kind == "pptx":
        deck = Presentation(io.BytesIO(data))
        return "\n".join(
            shape.text_frame.text
            for slide in deck.slides
            for shape in slide.shapes
            if shape.has_text_frame
        )
    import pypdfium2

    with pypdfium2.PdfDocument(data) as pdf:
        return "\n".join(page.get_textpage().get_text_range() for page in pdf)


# ---------------------------------------------------------------- making each format


@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx"])
def test_each_office_format_is_made_from_a_spec_and_kept_in_the_library(
    tmp_path, no_renderer, kind
):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    name = f"Έγγραφο 文件.{kind}"
    result = call(
        runtime, task, "create_document", {"format": kind, "name": name, "spec": SPECS[kind]}
    )
    assert result["saved"] is True and result["name"] == name and result["version"] == 1
    assert {"label": "File structure", "status": "passed"} in result["checks"]
    # No LibreOffice here: the check says it did not render, rather than claiming it did.
    assert {check["status"] for check in result["checks"]} <= {"passed", "not_checked"}
    (file,) = library(store, bot)
    assert file["id"] == result["id"] and file["name"] == name
    assert file["source_task_id"] == task["id"]  # shown as a file card with this reply
    data = store.file_data("owner", bot["id"], file["id"])
    assert file["binary"] and file["sha256"] == result["sha256"]
    text = text_of(kind, data)
    assert GREEK in text and CHINESE in text
    assert scratch_left(store) == []


def test_a_pdf_is_made_from_markdown_in_greek(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    markdown = (
        f"# {GREEK}\n\nΚείμενο της αναφοράς.\n\n| Μήνας | Ποσό |\n|---|---|\n| Μάιος | 12 |\n"
    )
    result = call(
        runtime,
        task,
        "create_document",
        {"format": "pdf", "name": "Αναφορά.pdf", "markdown": markdown},
    )
    assert result["saved"] is True and result["pages"] == 1
    # A PDF renders without LibreOffice, so its page was checked.
    assert {"label": "Rendered pages", "status": "passed"} == {
        key: value for key, value in result["checks"][-1].items() if key in ("label", "status")
    }
    (file,) = library(store, bot)
    assert GREEK in text_of("pdf", store.file_data("owner", bot["id"], file["id"]))


def test_a_pdf_takes_chinese_japanese_and_korean_text(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    lines = [CHINESE, "報告書を作成しました", "보고서가 완성되었습니다", GREEK]
    result = call(
        runtime,
        task,
        "create_document",
        {"format": "pdf", "name": "报告.pdf", "markdown": "\n\n".join(lines)},
    )
    assert result["saved"] is True
    (file,) = library(store, bot)
    text = text_of("pdf", store.file_data("owner", bot["id"], file["id"]))
    assert all(line in text for line in lines)
    assert scratch_left(store) == []


def test_text_a_pdf_font_cannot_show_is_refused_plainly_and_nothing_is_kept(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    # An ideograph outside the blocks the bundled fonts cover.
    with pytest.raises(Conflict, match="Unsupported font glyphs"):
        call(
            runtime,
            task,
            "create_document",
            {"format": "pdf", "name": "报告.pdf", "markdown": f"# {CHINESE} \U00020bb7"},
        )
    assert library(store, bot) == [] and scratch_left(store) == []
    # The guide says which scripts a PDF takes, so the owl can pick a docx instead.
    guide = call(runtime, task, "describe_document_format", {"format": "pdf"})
    supports = " ".join(guide["supports"])
    assert "Greek" in supports and "Chinese/Japanese/Korean" in supports
    assert "Unsupported glyphs fail explicitly" in guide["limits"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"format": "docx", "name": "Αναφορά", "spec": SPECS["docx"]},  # no extension
        {"format": "docx", "name": "Αναφορά.pdf", "spec": SPECS["docx"]},
        {"format": "pdf", "name": "Αναφορά.pdf", "spec": SPECS["docx"]},
        {"format": "docx", "name": "Αναφορά.docx", "markdown": "# Τίτλος"},
        {"format": "docx", "name": "../Αναφορά.docx", "spec": SPECS["docx"]},
    ],
)
def test_requests_that_do_not_fit_their_format_are_refused_before_any_work(tmp_path, arguments):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    with pytest.raises(ValidationError):
        call(runtime, task, "create_document", arguments)
    assert library(store, bot) == []
    assert not (Path(store.path).parent / owl_documents.SCRATCH).exists()


def test_a_spec_the_engine_refuses_comes_back_in_its_own_words(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    with pytest.raises(Conflict, match="Unsupported workbook keys: tabs"):
        call(
            runtime,
            task,
            "create_document",
            {"format": "xlsx", "name": "Πίνακας.xlsx", "spec": {"tabs": []}},
        )
    assert library(store, bot) == [] and scratch_left(store) == []


# ---------------------------------------------------------------- charts

CHART = {
    "chart": "column",
    "title": f"{GREEK} {CHINESE}",
    "categories": ["Εβδ. 1", "第二周"],
    "series": [{"name": "Δίκτυο", "values": [3, 5]}, {"name": "网络", "values": [4, 1]}],
}
CHART_SPECS = {
    "docx": {"blocks": [{"kind": "paragraph", "text": "Σύνοψη"}, {"kind": "chart", **CHART}]},
    "xlsx": {"sheets": [{"name": "Γράφημα", "charts": [CHART]}]},
    "pptx": {
        "slides": [
            {"elements": [{"type": "chart", **CHART, "x": 1, "y": 1, "width": 8, "height": 5}]}
        ]
    },
}


@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx", "pdf"])
def test_a_chart_in_the_spec_reaches_the_saved_document(tmp_path, no_renderer, kind):
    import zipfile

    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    arguments = {"format": kind, "name": f"Γράφημα.{kind}"}
    if kind == "pdf":
        arguments["markdown"] = "# Αναφορά\n\n```chart\n" + json.dumps(CHART) + "\n```\n"
    else:
        arguments["spec"] = CHART_SPECS[kind]
    made = call(runtime, task, "create_document", arguments)
    assert made["saved"] is True
    data = store.file_data("owner", bot["id"], made["id"])
    if kind == "pdf":
        text = text_of("pdf", data)
        assert GREEK in text and CHINESE in text and "第二周" in text
        return
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert any(name.endswith("charts/chart1.xml") for name in archive.namelist())
    # Inspection lists the chart, so the owl can refer to it.
    content = call(runtime, task, "inspect_document", {"file_id": made["id"]})["content"]
    listed = (
        content["charts"]
        if kind == "docx"
        else content["sheets"][0]["charts"]
        if kind == "xlsx"
        else [shape["chart"] for shape in content["slides"][0]["shapes"] if "chart" in shape]
    )
    assert [(item["chart"], item["title"]) for item in listed] == [("column", CHART["title"])]
    assert [entry["name"] for entry in listed[0]["series"]] == ["Δίκτυο", "网络"]


def test_a_chart_the_engine_refuses_comes_back_in_its_words_and_the_guide_shows_charts(
    tmp_path, no_renderer
):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    broken = {**CHART, "series": [{"name": "Δίκτυο", "values": [3]}]}
    with pytest.raises(Conflict, match="series 'Δίκτυο' has 1 values for 2 categories"):
        call(
            runtime,
            task,
            "create_document",
            {"format": "docx", "name": "Α.docx", "spec": {"blocks": [{"kind": "chart", **broken}]}},
        )
    assert library(store, bot) == [] and scratch_left(store) == []
    for kind in ("docx", "xlsx", "pptx", "pdf"):
        guide = call(runtime, task, "describe_document_format", {"format": kind})
        described = json.dumps(guide, ensure_ascii=False)
        assert "chart" in described and "scatter" in described, kind


# ---------------------------------------------------------------- a failed check


def clipped_render(tmp_path, monkeypatch):
    """A renderer whose second page has text outside the page, as a broken layout would."""
    from alysis_code.office import rendering

    pdf_path = tmp_path / "rendered.pdf"
    canvas = Canvas(str(pdf_path))
    canvas.drawString(72, 720, "Σελίδα 1")
    canvas.showPage()
    canvas.drawString(560, 400, "Κείμενο που βγαίνει έξω από τη σελίδα")
    canvas.showPage()
    canvas.save()

    def convert(executable, source, destination, profile, **kwargs):
        (destination / (source.stem + ".pdf")).write_bytes(pdf_path.read_bytes())

    monkeypatch.setattr("alysis_code.office.renderer_path", lambda: Path(sys.executable))
    monkeypatch.setattr(rendering, "renderer_path", lambda: Path(sys.executable))
    monkeypatch.setattr(rendering, "_convert", convert)


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="Office renderer platforms")
@pytest.mark.parametrize("image_input", [True, False])
def test_a_failed_check_saves_nothing_and_says_what_to_fix(tmp_path, monkeypatch, image_input):
    store, bot, task = owl(tmp_path)
    clipped_render(tmp_path, monkeypatch)
    runtime = BotRuntime(store, "owner", lambda _: None)
    result = call(
        runtime,
        task,
        "create_document",
        {"format": "docx", "name": "Αναφορά.docx", "spec": SPECS["docx"]},
        image_input=image_input,
    )
    receipt = result.observation if image_input else result
    assert receipt["saved"] is False and "id" not in receipt
    failed = [check for check in receipt["checks"] if check["status"] == "failed"]
    assert failed == [
        {
            "label": "Rendered pages",
            "status": "failed",
            "detail": "Text extends outside page bounds.",
        }
    ]
    assert receipt["pages"] == 2 and receipt["pages_with_text_outside"] == [2]
    assert receipt["next"].startswith("Nothing was saved")
    assert library(store, bot) == [] and scratch_left(store) == []
    if image_input:
        # The page with the problem is the one shown, named as an unsaved draft.
        assert isinstance(result, FileImageResult) and receipt["shown_page"] == 2
        assert result.caption.startswith(FILE_IMAGE_CAPTION)
        assert '"Αναφορά.docx" (unsaved draft, rendered page 2 of 2)' in result.caption
        assert result.image_url.startswith("data:image/png;base64,")
    else:
        assert not isinstance(result, FileImageResult)
        assert receipt["page_image"] == owl_documents.NOT_VISIBLE


@pytest.fixture
def real_calculator(monkeypatch):
    executable = os.environ.get("ALYSIS_TEST_OFFICE_SOFFICE")
    if not executable:
        pytest.skip("Set ALYSIS_TEST_OFFICE_SOFFICE to exercise an installed real calculator")
    monkeypatch.setenv("ALYSIS_OFFICE_SOFFICE", executable)


def test_formulas_are_calculated_and_a_formula_error_blocks_saving(tmp_path, real_calculator):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    rows = [["Πόλη", "Ποσό"], ["Αθήνα", 120], ["北京", 80], ["Σύνολο", {"formula": "=SUM(B2:B3)"}]]
    made = call(
        runtime,
        task,
        "create_document",
        {
            "format": "xlsx",
            "name": "Έσοδα.xlsx",
            "spec": {"sheets": [{"name": "Φύλλο", "rows": rows}]},
        },
    )
    assert made["saved"] is True
    assert {"label": "Formula results", "status": "passed"}.items() <= next(
        check for check in made["checks"] if check["label"] == "Formula results"
    ).items()
    assert made["calculation"]["results"][0]["value"] == "200"
    saved = load_workbook(
        io.BytesIO(store.file_data("owner", bot["id"], made["id"])), data_only=True
    )
    assert saved["Φύλλο"]["B4"].value == 200
    broken = call(
        runtime,
        task,
        "create_document",
        {
            "format": "xlsx",
            "name": "Λάθος.xlsx",
            "spec": {"sheets": [{"name": "Φύλλο", "rows": [["Ποσό"], [{"formula": "=1/0"}]]}]},
        },
    )
    assert broken["saved"] is False
    assert broken["calculation"]["errors"][0]["cell"] == "A2"
    assert "calculation.errors" in broken["next"]
    assert [file["name"] for file in library(store, bot)] == ["Έσοδα.xlsx"]
    assert scratch_left(store) == []


# ---------------------------------------------------------------- editing a Library file


def test_editing_a_library_file_saves_its_next_version_and_keeps_the_original(
    tmp_path, no_renderer
):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    made = call(
        runtime,
        task,
        "create_document",
        {"format": "docx", "name": "Αναφορά.docx", "spec": SPECS["docx"]},
    )
    inspected = call(runtime, task, "inspect_document", {"file_id": made["id"]})
    assert inspected["source_sha256"] == made["sha256"]
    paragraph = next(p for p in inspected["content"]["paragraphs"] if p["text"] == CHINESE)
    spec = {
        "expected_source_sha256": inspected["source_sha256"],
        "edits": [
            {
                "locator": paragraph["locator"],
                "expected_text_sha256": paragraph["text_sha256"],
                "find": CHINESE,
                "replace": "Καλημέρα 早上好",
            }
        ],
    }
    edited = call(runtime, task, "edit_document", {"file_id": made["id"], "spec": spec})
    assert edited["saved"] is True and edited["version"] == 2
    assert edited["name"] == "Αναφορά.docx" and edited["source_file_id"] == made["id"]
    versions = store.list_files("owner", bot["id"])
    assert {file["revision"] for file in versions} == {1, 2}
    assert len({file["series_id"] for file in versions}) == 1
    assert "Καλημέρα 早上好" in text_of("docx", store.file_data("owner", bot["id"], edited["id"]))
    original = text_of("docx", store.file_data("owner", bot["id"], made["id"]))
    assert CHINESE in original and "Καλημέρα" not in original
    # The older version is never edited over the newer one.
    with pytest.raises(Conflict, match="older version"):
        call(runtime, task, "edit_document", {"file_id": made["id"], "spec": spec})
    # Under another name the result is a separate file.
    copy = call(
        runtime,
        task,
        "edit_document",
        {"file_id": made["id"], "spec": spec, "name": "Αντίγραφο.docx"},
    )
    assert copy["saved"] is True and copy["version"] == 1 and copy["name"] == "Αντίγραφο.docx"
    assert scratch_left(store) == []


def test_a_pdf_or_other_file_is_not_taken_for_an_editable_document(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    pdf = call(
        runtime, task, "create_document", {"format": "pdf", "name": "Α.pdf", "markdown": "# Α"}
    )
    with pytest.raises(Conflict, match="read_file"):
        call(runtime, task, "inspect_document", {"file_id": pdf["id"]})
    # The kind comes from the bytes: a text file named .docx is not a document.
    text = store.save_bytes("owner", bot["id"], "σημειώσεις.docx", "Κείμενο".encode())
    with pytest.raises(Conflict, match="isn't a Word, Excel, PowerPoint or PDF"):
        call(runtime, task, "check_document", {"file_id": text["id"]})


# ---------------------------------------------------------------- checking a Library file


def test_checking_a_library_file_changes_nothing_and_shows_the_asked_page(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    markdown = "\n\n".join(f"## Ενότητα {n}\n\n" + "Κείμενο. " * 400 for n in range(1, 4))
    pdf = call(
        runtime,
        task,
        "create_document",
        {"format": "pdf", "name": "Μακρύ.pdf", "markdown": markdown},
    )
    assert pdf["pages"] >= 2
    before = library(store, bot)
    checked = call(
        runtime, task, "check_document", {"file_id": pdf["id"], "page": 2}, image_input=True
    )
    assert isinstance(checked, FileImageResult)
    assert checked.observation["passed"] is True and checked.observation["shown_page"] == 2
    assert f'"Μακρύ.pdf" (rendered page 2 of {pdf["pages"]})' in checked.caption
    far = call(
        runtime, task, "check_document", {"file_id": pdf["id"], "page": 50}, image_input=True
    )
    assert far.observation["shown_page"] == 1 and "no page 50" in far.observation["page_note"]
    assert library(store, bot) == before and scratch_left(store) == []


# ---------------------------------------------------------------- Library inputs


def test_slide_images_come_from_the_library_by_file_id(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    picture = io.BytesIO()
    Image.new("RGB", (64, 48), "teal").save(picture, format="PNG")
    image = store.save_bytes("owner", bot["id"], "εικόνα.png", picture.getvalue())
    element = {"type": "image", "x": 1, "y": 1, "width": 4, "height": 3}
    spec = {"slides": [{"title": GREEK, "elements": [{**element, "file_id": image["id"]}]}]}
    made = call(
        runtime, task, "create_document", {"format": "pptx", "name": "Δ.pptx", "spec": spec}
    )
    deck = Presentation(io.BytesIO(store.file_data("owner", bot["id"], made["id"])))
    assert any(shape.shape_type == 13 for shape in deck.slides[0].shapes)  # a picture
    guide = call(runtime, task, "describe_document_format", {"format": "pptx"})
    assert "file_id" in json.dumps(guide["spec"]["elements"]) and "path" not in json.dumps(
        guide["spec"]["elements"]
    )
    for wrong in ({**element, "path": "εικόνα.png"}, element):
        with pytest.raises(Conflict, match="file_id"):
            call(
                runtime,
                task,
                "create_document",
                {"format": "pptx", "name": "Ε.pptx", "spec": {"slides": [{"elements": [wrong]}]}},
            )


# ---------------------------------------------------------------- Stop and scratch


def test_stop_during_an_operation_leaves_no_file_and_no_scratch(tmp_path, no_renderer, monkeypatch):
    from alysis_code.office import word

    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    create = word.create
    seen = {}

    def stopped_while_writing(spec, output, root, *args, **kwargs):
        seen["root"] = root
        result = create(spec, output, root, *args, **kwargs)
        runtime.cancel(task["id"])  # the person presses Stop mid-operation
        return result

    monkeypatch.setattr(word, "create", stopped_while_writing)
    with pytest.raises(CooperativeCancellationError):
        call(
            runtime,
            task,
            "create_document",
            {"format": "docx", "name": "Α.docx", "spec": SPECS["docx"]},
        )
    assert library(store, bot) == []
    # It worked in the owl's place, beside its database, and nothing of it is left.
    place = Path(store.path).parent.resolve() / owl_documents.SCRATCH
    assert Path(seen["root"]).resolve().is_relative_to(place)
    assert not Path(seen["root"]).exists() and scratch_left(store) == []


def test_what_a_crash_left_is_removed_once_its_task_has_ended(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    call(runtime, task, "describe_document_format", {"format": "docx"})
    assert not (Path(store.path).parent / owl_documents.SCRATCH).exists()  # needs no scratch
    made = call(
        runtime,
        task,
        "create_document",
        {"format": "docx", "name": "Α.docx", "spec": SPECS["docx"]},
    )
    assert made["saved"]
    (person,) = (Path(store.path).parent / owl_documents.SCRATCH).iterdir()
    other = store.save_bot("owner", BotInput(name="Άλλος", purpose="Other work"))
    chat = store.create_chat("owner", other["id"], "Another")
    store.enqueue("owner", chat["id"], TaskInput(message="Κι άλλο", request_id="documents-2"))
    running = store.claim("owner")
    assert running["status"] == "running"
    ended = store.enqueue("owner", chat["id"], TaskInput(message="Τέλος", request_id="documents-3"))
    store.cancel("owner", ended["id"])
    for left in (running["id"], ended["id"], "a-task-that-is-gone"):
        (person / left / "call").mkdir(parents=True)
        (person / left / "call" / "draft.docx").write_bytes(b"left by a crash")
    call(runtime, task, "check_document", {"file_id": made["id"]})
    assert sorted(path.name for path in person.iterdir()) == [running["id"]]


def test_a_full_library_keeps_nothing_and_says_why(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    store.set_library_limit("owner", 1000)
    with pytest.raises(LimitReached, match="Library allowance"):
        call(
            runtime,
            task,
            "create_document",
            {"format": "docx", "name": "Α.docx", "spec": SPECS["docx"]},
        )
    assert library(store, bot) == [] and scratch_left(store) == []


def test_files_turned_off_during_a_task_stop_document_work(tmp_path, no_renderer):
    store, bot, task = owl(tmp_path)
    runtime = BotRuntime(store, "owner", lambda _: None)
    # Setup edits wait for running work; inject a persisted revocation to show each call still
    # reads the current setting rather than the one the task started with.
    with store.connect(write=True) as db:
        db.execute("UPDATE bots SET files_enabled=0 WHERE id=?", (bot["id"],))
    for name, arguments in (
        ("describe_document_format", {"format": "docx"}),
        ("create_document", {"format": "docx", "name": "Α.docx", "spec": SPECS["docx"]}),
    ):
        with pytest.raises(Conflict, match="Files are turned off"):
            call(runtime, task, name, arguments)
    assert library(store, bot) == []


# ---------------------------------------------------------------- in a chat turn


def offered(client) -> set[str]:
    return {tool["function"]["name"] for tool in client.requests[0]["tools"] or []}


@pytest.mark.parametrize("files_enabled", [True, False])
def test_the_tools_are_offered_only_with_files_on(tmp_path, files_enabled):
    store, bot, task = owl(tmp_path, files_enabled=files_enabled)
    client = ClientFixture([response("Εντάξει.")])
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.run(task, runtime.task_token(task["id"]))
    names = offered(client)
    if files_enabled:
        assert set(DOCUMENT_TOOLS) <= names
    else:
        assert "save_file" not in names and not set(DOCUMENT_TOOLS) & names


def test_the_tools_are_not_offered_when_the_engine_lacks_the_libraries(tmp_path, monkeypatch):
    store, bot, task = owl(tmp_path)
    monkeypatch.setattr("alysis_code.bots.runtime.ready_formats", lambda: ())
    client = ClientFixture([response("Εντάξει.")])
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.run(task, runtime.task_token(task["id"]))
    assert "save_file" in offered(client) and not set(DOCUMENT_TOOLS) & offered(client)


@pytest.mark.parametrize("image_input", [True, False])
def test_a_chat_turn_makes_a_pdf_and_sees_its_page_only_with_image_input(
    tmp_path, no_renderer, image_input
):
    store, bot, task = owl(tmp_path)
    arguments = {"format": "pdf", "name": "Σημείωμα.pdf", "markdown": f"# {GREEK}\n\nΚείμενο."}
    client = ClientFixture(
        [response(name="create_document", arguments=arguments), response("Το έφτιαξα.")]
    )
    client._alysis_browser_image_input = image_input
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.run(task, runtime.task_token(task["id"]))
    finished = store.task("owner", task["id"])
    assert finished["status"] == "completed"
    (file,) = library(store, bot)
    assert file["name"] == "Σημείωμα.pdf" and file["source_task_id"] == task["id"]
    completed = [e for e in finished["events"] if e["kind"] == "tool_completed"]
    assert completed[-1]["data"] == {"name": "create_document", "id": file["id"]}
    last = client.requests[-1]
    (receipt,) = [json.loads(m["content"]) for m in last["messages"] if m["role"] == "tool"]
    assert receipt["saved"] is True and receipt["id"] == file["id"]
    images = [
        part
        for message in last["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    if image_input:
        (image,) = images
        assert image["image_url"]["url"].startswith("data:image/png;base64,")
        assert "attached after the tool results" in receipt["message"]
    else:
        assert images == [] and receipt["page_image"] == owl_documents.NOT_VISIBLE
    assert scratch_left(store) == []


def test_every_document_tool_schema_is_one_plain_object():
    for name in DOCUMENT_TOOLS:
        parameters = tool_schema(name)["function"]["parameters"]
        assert parameters["type"] == "object"
        assert not {"anyOf", "oneOf", "allOf"} & set(parameters)
