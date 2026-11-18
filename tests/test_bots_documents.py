"""Owls read the files people give them: images and office documents, by content.

Files are generated here with the same libraries, with Greek and English text. No model
provider or computer is started; the runtime uses scripted clients.
"""

from __future__ import annotations

import base64
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from test_bots_browser_runtime import ClientFixture, response
from test_bots_desktop import Executor

import alysis_code.documents as document_tools
from alysis_code.bots import documents
from alysis_code.bots.browser import BrowserImageResult
from alysis_code.bots.browser_images import (
    FILE_IMAGE_NOT_SHOWN,
    FILE_IMAGE_RETIRED,
    MAX_IMAGE_BYTES,
    SCREENSHOT_RETIRED,
    FileImageResult,
    append_browser_image,
    context_characters,
)
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime, tool_schema
from alysis_code.bots.store import LimitReached, Store
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

GREEK = "Γεια σου κόσμε"
ENGLISH = "Hello world"


# ---------------------------------------------------------------- generated files


def pdf_bytes(*pages: str | None, password: str | None = None) -> bytes:
    """One page per entry: its text, or None for a page holding only a picture (a scan)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen.canvas import Canvas

    font = Path(document_tools.__file__).parent / "fonts" / "LiberationSans-Regular.ttf"
    if "TestGreek" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("TestGreek", str(font)))
    out = io.BytesIO()
    canvas = Canvas(out, encrypt=password) if password else Canvas(out)
    for text in pages:
        if text is None:
            canvas.drawInlineImage(Image.new("RGB", (60, 40), "navy"), 72, 600, 200, 130)
        else:
            canvas.setFont("TestGreek", 12)
            for number, line in enumerate(text.split("\n")):
                canvas.drawString(72, 700 - 16 * number, line)
        canvas.showPage()
    canvas.save()
    return out.getvalue()


def docx_bytes() -> bytes:
    import docx
    from docx.enum.style import WD_STYLE_TYPE

    document = docx.Document()
    document.add_heading("Ετήσια αναφορά", level=1)
    document.add_paragraph(f"{GREEK}. {ENGLISH}.")
    # A heading style named in another language is still a heading: levels come from the
    # outline level it inherits, not from its name.
    own = document.styles.add_style("Ενότητα", WD_STYLE_TYPE.PARAGRAPH)
    own.base_style = document.styles["Heading 2"]
    document.add_paragraph("Αποτελέσματα", style=own)
    document.add_paragraph("Πρώτο σημείο", style="List Bullet")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Όνομα"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Καφές\nespresso"
    table.cell(1, 1).text = "3,5"
    document.add_paragraph("Τέλος")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def replace_part(data: bytes, name: str, change) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            body = source.read(item)
            target.writestr(item, change(body) if item.filename == name else body)
    return out.getvalue()


def xlsx_bytes() -> bytes:
    import openpyxl

    book = openpyxl.Workbook()
    sales = book.active
    sales.title = "Πωλήσεις"
    sales.append(["Προϊόν", "Ποσό"])
    sales.append(["Καφές", 3.5])
    sales.append(["Tea", 2])
    sales["B4"] = "=SUM(B2:B3)"
    sales["C7"] = "Σημείωση\nδεύτερη γραμμή"
    book.create_sheet("Κενό")
    hidden = book.create_sheet("Archive")
    hidden["A1"] = "παλιό"
    hidden.sheet_state = "hidden"
    out = io.BytesIO()
    book.save(out)
    # A spreadsheet application saves each formula with its last result.
    return replace_part(
        out.getvalue(),
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(b"<f>SUM(B2:B3)</f><v></v>", b"<f>SUM(B2:B3)</f><v>5.5</v>"),
    )


def pptx_bytes() -> bytes:
    import pptx
    from pptx.util import Inches

    presentation = pptx.Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[1])
    first.shapes.title.text = "Στρατηγική 2027"
    first.placeholders[1].text = f"{GREEK}\n{ENGLISH}"
    first.notes_slide.notes_text_frame.text = "Σημειώσεις ομιλητή"
    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "Αριθμοί"
    table = second.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    for (row, column), text in {
        (0, 0): "Έτος",
        (0, 1): "Total",
        (1, 0): "2026",
        (1, 1): "42",
    }.items():
        table.cell(row, column).text = text
    group = second.shapes.add_group_shape()
    group.shapes.add_textbox(
        Inches(1), Inches(4), Inches(2), Inches(1)
    ).text_frame.text = "Ομάδα κειμένου"
    out = io.BytesIO()
    presentation.save(out)
    return out.getvalue()


def image_bytes(kind: str, size=(64, 48), color="teal", **options) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA" if kind == "WEBP" else "RGB", size, color).save(out, format=kind, **options)
    return out.getvalue()


def photo_bytes(size=(4000, 3000)) -> bytes:
    """A detailed photo-like JPEG: as PNG it would be far over the image allowance."""
    noise = Image.effect_noise(size, 90).convert("RGB")
    out = io.BytesIO()
    noise.save(out, format="JPEG", quality=95)
    return out.getvalue()


def compound_file(*streams: str) -> bytes:
    """A minimal compound file: the container of password-protected and older office files."""
    header = bytearray(512)
    header[:8] = documents.CFB_SIGNATURE
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<II", header, 44, 1, 1)  # one FAT sector; directory at sector 1
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<IIII", header, 60, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    struct.pack_into("<109I", header, 76, 0, *[0xFFFFFFFF] * 108)
    fat = struct.pack("<128I", 0xFFFFFFFD, 0xFFFFFFFE, *[0xFFFFFFFF] * 126)
    directory = bytearray(512)
    for index, (name, kind) in enumerate([("Root Entry", 5), *((s, 2) for s in streams)]):
        encoded = name.encode("utf-16-le") + b"\0\0"
        directory[index * 128 : index * 128 + len(encoded)] = encoded
        struct.pack_into("<HB", directory, index * 128 + 64, len(encoded), kind)
    return bytes(header) + fat + bytes(directory)


def read(name, data, *, offset=0, length=12000, page=None, image_input=False):
    return documents.read(
        name,
        data,
        documents.sniff(data[: documents.SNIFF_BYTES]),
        offset=offset,
        length=length,
        page=page,
        image_input=image_input,
    )


def decoded(result: FileImageResult) -> Image.Image:
    header, encoded = result.image_url.split(",", 1)
    assert header in ("data:image/png;base64", "data:image/jpeg;base64")
    data = base64.b64decode(encoded)
    assert len(data) <= MAX_IMAGE_BYTES
    return Image.open(io.BytesIO(data))


# ---------------------------------------------------------------- recognising files


def test_kinds_come_from_content_not_names():
    assert documents.sniff(image_bytes("PNG")) == "png"
    assert documents.sniff(image_bytes("JPEG")) == "jpeg"
    assert documents.sniff(image_bytes("GIF")) == "gif"
    assert documents.sniff(image_bytes("WEBP")) == "webp"
    assert documents.sniff(pdf_bytes(GREEK)) == "pdf"
    assert documents.sniff(docx_bytes()) == "zip"
    assert documents.sniff(compound_file()) == "cfb"
    assert documents.sniff(f"{GREEK}, plain text".encode()) is None
    assert documents.office_kind(docx_bytes()) == "docx"
    assert documents.office_kind(xlsx_bytes()) == "xlsx"
    assert documents.office_kind(pptx_bytes()) == "pptx"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as plain:
        plain.writestr("σημειώσεις.txt", GREEK)
    assert documents.office_kind(archive.getvalue()) is None
    # A picture named like a document is read as the picture it is.
    picture = read("αναφορά.docx", image_bytes("PNG"), image_input=True)
    assert isinstance(picture, FileImageResult)
    assert picture.observation["format"] == "PNG"


# ---------------------------------------------------------------- documents


def test_pdf_pages_are_marked_and_a_page_without_text_says_so():
    data = pdf_bytes(f"{GREEK}\n{ENGLISH}", None, "Τρίτη σελίδα")
    for image_input in (False, True):
        result = read("έκθεση.pdf", data, image_input=image_input)
        assert result["format"] == "PDF" and result["pages"] == 3
        assert result["content"] == (
            f"--- Page 1 of 3 ---\n{GREEK}\n{ENGLISH}\n\n"
            f"--- Page 2 of 3 ---\n{documents.NO_TEXT_PAGE}\n\n"
            "--- Page 3 of 3 ---\nΤρίτη σελίδα\n\n"
        )
        assert result["next_offset"] is None
        assert result["total_characters"] == len(result["content"])
        assert "Pages without extractable text: 2." in result["message"]
        if image_input:
            assert "page set to its number" in result["message"]
        else:
            assert "can't see images" in result["message"]


def test_long_documents_are_read_in_parts_that_join_up():
    for name, data in [
        ("έκθεση.pdf", pdf_bytes(*(f"Σελίδα {n}: {GREEK} {ENGLISH}" for n in range(1, 30)))),
        ("σημείωμα.docx", docx_bytes()),
        ("πωλήσεις.xlsx", xlsx_bytes()),
        ("παρουσίαση.pptx", pptx_bytes()),
    ]:
        whole = read(name, data)
        assert whole["next_offset"] is None
        parts, offset = [], 0
        while offset is not None:
            part = read(name, data, offset=offset, length=50)
            parts.append(part["content"])
            if part["next_offset"] is not None:
                assert part["next_offset"] == offset + 50
                assert "total_characters" not in part
                assert f"offset={offset + 50} to read on" in part["message"]
            offset = part["next_offset"]
        assert "".join(parts) == whole["content"]
        assert part["total_characters"] == len(whole["content"])
        beyond = read(name, data, offset=len(whole["content"]) + 5)
        assert beyond["content"] == "" and beyond["next_offset"] is None
        assert "past the end" in beyond["message"]


def test_docx_headings_lists_and_tables_keep_their_structure():
    result = read("σημείωμα.docx", docx_bytes())
    assert result["format"] == "DOCX"
    assert result["content"] == (
        "# Ετήσια αναφορά\n"
        f"{GREEK}. {ENGLISH}.\n"
        "\n## Αποτελέσματα\n"
        "- Πρώτο σημείο\n"
        "\nΌνομα\tValue\nΚαφές espresso\t3,5\n\n"
        "Τέλος\n"
    )


def test_xlsx_sheets_rows_and_saved_formula_results():
    result = read("πωλήσεις.xlsx", xlsx_bytes())
    assert result["format"] == "XLSX" and result["sheets"] == 3
    assert result["content"] == (
        "--- Sheet 1 of 3: Πωλήσεις ---\n"
        "Row 1: Προϊόν\tΠοσό\n"
        "Row 2: Καφές\t3.5\n"
        "Row 3: Tea\t2\n"
        "Row 4: \t5.5\n"
        "Row 7: \t\tΣημείωση δεύτερη γραμμή\n\n"
        "--- Sheet 2 of 3: Κενό ---\n[This sheet is empty.]\n\n"
        "--- Sheet 3 of 3: Archive (hidden) ---\nRow 1: παλιό\n\n"
    )
    assert documents.FORMULA_NOTE in result["message"]


def test_pptx_slides_tables_groups_and_notes():
    result = read("παρουσίαση.pptx", pptx_bytes())
    assert result["format"] == "PPTX" and result["slides"] == 2
    assert result["content"] == (
        "--- Slide 1 of 2 ---\n"
        f"Στρατηγική 2027\n{GREEK}\n{ENGLISH}\n"
        "Notes: Σημειώσεις ομιλητή\n\n"
        "--- Slide 2 of 2 ---\n"
        "Αριθμοί\nΈτος\tTotal\n2026\t42\nΟμάδα κειμένου\n\n"
    )


def test_protected_and_damaged_files_get_a_plain_answer():
    protected = [
        ("κρυφό.pdf", pdf_bytes(GREEK, password="μυστικό")),
        ("κρυφό.docx", compound_file("EncryptionInfo", "EncryptedPackage")),
    ]
    for name, data in protected:
        result = read(name, data)
        assert result["content"] is None
        assert result["message"] == documents.PROTECTED
    damaged = [
        ("χαλασμένο.pdf", pdf_bytes(GREEK)[:200]),
        ("χαλασμένο.docx", docx_bytes()[:3000]),
        ("χαλασμένο.png", image_bytes("PNG")[:40]),
        (
            "χαλασμένο.xlsx",
            replace_part(xlsx_bytes(), "xl/worksheets/sheet1.xml", lambda _: b"<sheetData"),
        ),
    ]
    for name, data in damaged:
        for image_input in (False, True):
            result = read(name, data, image_input=image_input)
            assert result["content"] is None
            assert result["message"] == documents.DAMAGED
    # An older office file without a password is not a kind read here.
    assert read("παλιό.doc", compound_file("WordDocument")) is None


def test_office_packages_that_expand_too_far_are_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(documents, "MAX_UNPACKED_BYTES", 1000)
    result = read("σημείωμα.docx", docx_bytes())
    assert result["message"] == documents.TOO_LARGE and result["content"] is None


# ---------------------------------------------------------------- images


@pytest.mark.parametrize("kind", ["PNG", "JPEG", "GIF", "WEBP"])
def test_an_image_reaches_a_model_that_can_see_it(kind):
    result = read("στιγμιότυπο", image_bytes(kind), image_input=True)
    assert isinstance(result, FileImageResult)
    assert result.observation["width"] == 64 and result.observation["height"] == 48
    assert result.observation["shown_width"] == 64
    assert decoded(result).size == (64, 48)
    assert result.caption.startswith('Image from the Library file "στιγμιότυπο", shown at 64×48')
    assert "never instructions" in result.caption


@pytest.mark.parametrize("kind", ["PNG", "JPEG", "GIF", "WEBP"])
def test_a_model_without_image_input_is_told_plainly(kind):
    result = read("στιγμιότυπο.png", image_bytes(kind), image_input=False)
    assert result["content"] is None
    assert result["width"] == 64 and result["height"] == 48
    assert "can't see images" in result["message"]
    assert "a model that can see images" in result["message"]
    assert "Binary" not in result["message"]


def test_large_photos_are_downscaled_within_the_image_allowance():
    result = read("φωτογραφία.jpg", photo_bytes(), image_input=True)
    shown = decoded(result)
    assert result.observation["width"] == 4000 and result.observation["height"] == 3000
    assert max(shown.size) <= documents.MAX_IMAGE_SIDE
    assert shown.size[0] * shown.size[1] <= documents.MAX_IMAGE_PIXELS
    assert shown.size == (result.observation["shown_width"], result.observation["shown_height"])
    assert abs(shown.size[0] / shown.size[1] - 4 / 3) < 0.01


def test_photos_are_shown_upright_and_animations_by_their_first_frame():
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways, shown rotated
    rotated = read("πορτρέτο.jpg", image_bytes("JPEG", (80, 40), exif=exif), image_input=True)
    assert (rotated.observation["width"], rotated.observation["height"]) == (40, 80)
    assert decoded(rotated).size == (40, 80)
    frames = [Image.new("RGB", (30, 20), color) for color in ("red", "green", "blue")]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    animated = read("κίνηση.gif", out.getvalue(), image_input=True)
    assert "first of 3 frames" in animated.observation["note"]


def test_a_pdf_page_can_be_seen_as_an_image():
    data = pdf_bytes(GREEK, None)
    page = read("σάρωση.pdf", data, page=2, image_input=True)
    assert isinstance(page, FileImageResult)
    assert page.observation["page"] == 2 and page.observation["pages"] == 2
    shown = decoded(page)
    assert max(shown.size) <= documents.MAX_IMAGE_SIDE
    assert '"σάρωση.pdf" (page 2 of 2)' in page.caption
    unseen = read("σάρωση.pdf", data, page=2, image_input=False)
    assert unseen["content"] is None and "can't see images" in unseen["message"]
    missing = read("σάρωση.pdf", data, page=7, image_input=True)
    assert missing["content"] is None
    assert missing["message"] == "This PDF has 2 pages, so it has no page 7."


# ---------------------------------------------------------------- one image place


def test_library_images_share_the_one_image_place_with_screenshots():
    messages = []
    picture = read("σχέδιο.png", image_bytes("PNG"), image_input=True)
    append_browser_image(messages, picture)
    screenshot = BrowserImageResult(
        {"source_id": "frame", "frame": {"width": 64, "height": 48}},
        "data:image/png;base64," + base64.b64encode(image_bytes("PNG")).decode(),
    )
    append_browser_image(messages, screenshot)
    assert messages[0]["content"][0]["text"] == picture.caption
    assert messages[0]["content"][1] == {"type": "text", "text": FILE_IMAGE_RETIRED}
    append_browser_image(messages, read("φωτό.jpg", photo_bytes((900, 700)), image_input=True))
    assert messages[1]["content"][1] == {"type": "text", "text": SCREENSHOT_RETIRED}
    assert messages[2]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    context_characters(messages)  # exactly one current image, within its allowance
    # The declared type must match the bytes it carries.
    mislabelled = BrowserImageResult(
        screenshot.observation,
        "data:image/jpeg;base64," + base64.b64encode(image_bytes("PNG")).decode(),
    )
    with pytest.raises(LimitReached):
        append_browser_image([], mislabelled)


# ---------------------------------------------------------------- read_file in a turn


def upload(store, owner, bot_id, name, data):
    return store.save_bytes(owner, bot_id, name, data)["id"]


@pytest.fixture
def chat_turn(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotInput(name="Αρχείο", purpose="Read the person's files"))
    chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue("owner", chat["id"], TaskInput(message="Τι λέει;", request_id="read-files"))
    return store, bot, store.claim("owner")


def calls(*arguments):
    return LLMResponse(
        "",
        [ToolCall(f"read-{n}", "read_file", args) for n, args in enumerate(arguments)],
        {},
        usage=LLMUsage(10, 5, 15),
    )


def images_in(request):
    return [
        part
        for message in request["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]


def receipts(request):
    return [json.loads(m["content"]) for m in request["messages"] if m["role"] == "tool"]


def test_read_file_returns_document_text_and_keeps_text_files_unchanged(chat_turn):
    store, bot, task = chat_turn
    runtime = BotRuntime(store, "owner", lambda _: None)
    pdf = upload(store, "owner", bot["id"], "έκθεση.pdf", pdf_bytes(GREEK))
    result = runtime._tool(task, "read_file", {"file_id": pdf}, ["read_file"])
    assert result["content"] == f"--- Page 1 of 1 ---\n{GREEK}\n\n"
    text = upload(store, "owner", bot["id"], "σημειώσεις.txt", f"{GREEK}\n".encode())
    assert runtime._tool(task, "read_file", {"file_id": text, "length": 5}, ["read_file"]) == {
        "name": "σημειώσεις.txt",
        "content": GREEK[:5],
        "next_offset": 5,
        "total_characters": len(GREEK) + 1,
    }


def test_the_read_file_description_names_what_it_reads():
    description = tool_schema("read_file")["function"]["description"]
    for kind in ("PDF", "DOCX", "XLSX", "PPTX", "PNG", "JPEG", "GIF", "WebP"):
        assert kind in description
    assert "page" in tool_schema("read_file")["function"]["parameters"]["properties"]


@pytest.mark.parametrize("image_input", [True, False])
def test_a_chat_turn_shows_a_library_image_only_to_a_model_that_can_see(chat_turn, image_input):
    store, bot, task = chat_turn
    file_id = upload(store, "owner", bot["id"], "οθόνη.png", image_bytes("PNG"))
    client = ClientFixture([calls({"file_id": file_id}), response("Είδα την εικόνα.")])
    client._alysis_browser_image_input = image_input
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "completed"
    last = client.requests[-1]
    (receipt,) = receipts(last)
    assert "image_url" not in json.dumps(receipt)
    if image_input:
        (image,) = images_in(last)
        assert image["image_url"]["url"].startswith("data:image/png;base64,")
        caption = next(m for m in last["messages"] if isinstance(m.get("content"), list))
        assert caption["role"] == "user"
        assert caption["content"][0]["text"].startswith('Image from the Library file "οθόνη.png"')
        assert receipt["shown_width"] == 64
    else:
        assert images_in(last) == []
        assert "can't see images" in receipt["message"]


def test_two_images_in_one_step_show_the_last_and_say_the_first_was_not_shown(chat_turn):
    store, bot, task = chat_turn
    first = upload(store, "owner", bot["id"], "πρώτη.png", image_bytes("PNG", color="red"))
    second = upload(store, "owner", bot["id"], "δεύτερη.jpg", image_bytes("JPEG"))
    client = ClientFixture([calls({"file_id": first}, {"file_id": second}), response("Τέλος")])
    client._alysis_browser_image_input = True
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.run(task, runtime.task_token(task["id"]))
    last = client.requests[-1]
    assert len(images_in(last)) == 1
    shown_first, shown_second = receipts(last)
    assert shown_first["name"] == "πρώτη.png"
    assert shown_first["message"] == FILE_IMAGE_NOT_SHOWN
    assert shown_second["name"] == "δεύτερη.jpg"
    assert "attached after the tool results" in shown_second["message"]
    caption = next(m for m in last["messages"] if isinstance(m.get("content"), list))
    assert '"δεύτερη.jpg"' in caption["content"][0]["text"]


# ---------------------------------------------------------------- work on the private computer


@pytest.fixture
def computer_work(tmp_path):
    """A task whose offline computer the person approved, so it runs as background work."""
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Μίρα", purpose="Work offline"))
    chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Διάβασε το αρχείο", request_id="read-on-computer")
    )
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        task,
        ActionProposal(
            question="Run the offline task?",
            instructions="Read the person's file.",
            offline_computer=True,
        ),
    )
    store.finish("owner", task["id"], "completed", content="Please review the scope.")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    manager = DesktopComputers("owner", ComputerViews(), factory=Executor)
    yield store, bot, store.claim("owner"), manager
    manager.close()


@pytest.mark.parametrize("image_input", [True, False])
def test_background_work_reads_library_images_and_documents(computer_work, image_input):
    store, bot, task, manager = computer_work
    image = upload(store, "owner", bot["id"], "διάγραμμα.png", image_bytes("PNG"))
    sheet = upload(store, "owner", bot["id"], "πωλήσεις.xlsx", xlsx_bytes())
    client = ClientFixture(
        [
            response(name="read_file", arguments={"file_id": sheet}),
            response(name="read_file", arguments={"file_id": image}),
            response("Διάβασα και τα δύο."),
        ]
    )
    client._alysis_browser_image_input = image_input
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.run(task, runtime.task_token(task["id"]))
    queued = runtime.background.claim()
    assert queued["id"] == task["id"]
    runtime.run(queued, runtime.task_token(queued["id"]), background=True)
    assert store.task("owner", task["id"])["status"] == "completed"
    last = client.requests[-1]
    sheet_read, image_read = receipts(last)
    assert "Row 2: Καφές\t3.5\n" in sheet_read["content"]
    if image_input:
        (picture,) = images_in(last)
        assert picture["image_url"]["url"].startswith("data:image/png;base64,")
        caption = next(m for m in last["messages"] if isinstance(m.get("content"), list))
        assert caption["content"][0]["text"].startswith(
            'Image from the Library file "διάγραμμα.png"'
        )
        assert "attached after the tool results" in image_read["message"]
    else:
        assert images_in(last) == []
        assert image_read["content"] is None
        assert "can't see images" in image_read["message"]
