"""PDFs made from Markdown draw every script the bundled fonts cover.

A character the body font (Liberation Sans) lacks is drawn by a bundled fallback font
that has a glyph for it, decided by glyph coverage alone, and paragraphs with wide East
Asian characters wrap between characters. Every test reads the text and each
character's font and position back from the PDF itself.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import struct
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_documents import create, extracted

import alysis_code.documents as document_tools
from alysis_code.artifact_checks import ArtifactChecks
from alysis_code.documents import __main__ as documents
from alysis_code.documents import pdf_capability_status

SAMPLES = {
    "simplified_chinese": "简体中文报告：我们在第三季度完成了所有目标，客户满意度显著提高。",
    "traditional_chinese": "繁體中文報告：我們在第三季度完成了所有目標，客戶滿意度顯著提高。",
    "japanese": "日本語の報告書：ひらがな、カタカナ、漢字がすべて正しく表示されます。",
    "korean": "한국어 보고서: 우리는 3분기에 모든 목표를 달성했습니다. 가격도 안정적이었습니다.",
}
# Text inside the page's content frame: A4 width less two 54pt margins and 6pt padding.
CONTENT_LEFT = 54 + 6


def characters(pdf_path):
    """Every visible character of the page bodies (not page numbers): font, box, baseline."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    found = []
    with pdfium.PdfDocument(pdf_path) as pdf:
        for index in range(len(pdf)):
            with closing(pdf[index]) as page, closing(page.get_textpage()) as textpage:
                width = page.get_size()[0]
                for position in range(textpage.count_chars()):
                    char = textpage.get_text_range(position, 1)
                    left, bottom, right, top = textpage.get_charbox(position)
                    if char.isspace() or top <= 45:
                        continue
                    size = raw.FPDFText_GetFontInfo(textpage, position, None, 0, None)
                    name = ctypes.create_string_buffer(size)
                    raw.FPDFText_GetFontInfo(textpage, position, name, size, None)
                    x, y = ctypes.c_double(), ctypes.c_double()
                    raw.FPDFText_GetCharOrigin(textpage, position, x, y)
                    found.append(
                        SimpleNamespace(
                            char=char,
                            font=name.value.decode(),
                            left=left,
                            right=right,
                            line=(index, round(y.value, 1)),
                            page=index,
                            page_width=width,
                        )
                    )
    return found


def font_glyphs():
    """PostScript name -> characters it has, for the body font and every fallback font."""
    from reportlab.pdfbase import pdfmetrics

    documents._font_setup()
    body = pdfmetrics.getFont("AlysisDocumentRegular").face
    glyphs = {body.name.decode(): frozenset(body.charToGlyph)}
    for name, filename in documents.FALLBACK_FONTS:
        documents._register_font(name, filename, single_weight=True)
        face = pdfmetrics.getFont(name).face
        glyphs[face.name.decode()] = frozenset(face.charToGlyph)
    return glyphs


def assert_inside_content_frame(found):
    for item in found:
        assert item.left >= CONTENT_LEFT - 1, item
        assert item.right <= item.page_width - CONTENT_LEFT + 1, item


def lines(found):
    """Each text line's characters, in order; a line's characters share one baseline."""
    grouped = {}
    for item in found:
        grouped.setdefault(item.line, []).append(item.char)
    return ["".join(chars) for chars in grouped.values()]


def compact(text):
    return re.sub(r"\s+", "", text)


def drawn_text(found):
    return "".join(item.char for item in found)


@pytest.mark.parametrize("sample", SAMPLES)
def test_east_asian_text_round_trips_in_a_font_that_has_each_glyph(tmp_path, sample):
    text = SAMPLES[sample]
    result = create(tmp_path, f"# {text[:6]}\n\n{text}\n\n- {text}\n")
    assert result["status"] == "created"
    assert all(page["content_characters"] for page in result["pages"])
    found = characters(tmp_path / "outputs/report.pdf")
    assert drawn_text(found) == compact(f"{text[:6]}{text}•{text}")

    glyphs = font_glyphs()
    body_name, body_glyphs = next(iter(glyphs.items()))
    borrowed = set()
    for item in found:
        if item.char == "•":  # ReportLab's own bullet, drawn in a standard PDF font.
            continue
        if ord(item.char) in body_glyphs:
            assert item.font.startswith(body_name.split("-")[0]), item
        else:
            owner = next(name for name in glyphs if name in item.font)
            assert ord(item.char) in glyphs[owner], item
            borrowed.add(owner)
    # The paragraph stays in one fallback typeface, even for characters two fonts share.
    assert len(borrowed) == 1
    assert_inside_content_frame(found)


def test_mixed_greek_chinese_english_paragraph_wraps_between_words(tmp_path):
    sentence = (
        "Η ετήσια αναφορά (annual report) περιέχει 中文内容和数据 and English explanations, "
        "以及更多的中文文字说明。"
    )
    paragraph = sentence * 8
    create(tmp_path, paragraph)
    content = extracted(tmp_path / "outputs/report.pdf")
    # Lines break between words and ideographs, never inside a Greek or English word.
    for word in set(re.findall(r"[^\W\d_]+", sentence)):
        if word.isascii() or all("\u0370" <= c <= "\u03ff" for c in word):
            assert content.count(word) == paragraph.count(word), word

    found = characters(tmp_path / "outputs/report.pdf")
    assert drawn_text(found) == compact(paragraph)
    assert len(lines(found)) > 3
    fonts = {item.char: item.font for item in found}
    assert "Liberation" in fonts["Η"] and "Liberation" in fonts["a"]
    assert "DroidSansFallback" in fonts["中"]
    assert_inside_content_frame(found)


def test_long_text_without_spaces_wraps_inside_the_page(tmp_path):
    ideographs = "".join(chr(0x4E00 + 7 * index) for index in range(1500))
    # Closing marks may hang past a line's end; across these lengths one lands on the edge.
    hanging = "\n\n".join(
        "测" * length + mark + "后" * 8 for length in range(30, 60) for mark in "?）"
    )
    result = create(tmp_path, f"{ideographs}\n\n{hanging}\n")
    assert result["page_count"] >= 2
    found = characters(tmp_path / "outputs/report.pdf")
    assert drawn_text(found) == compact(ideographs + hanging)
    assert len(lines(found)) > 30
    assert_inside_content_frame(found)


def test_closing_punctuation_never_starts_a_line(tmp_path):
    marks = "，。！？：；、）》」”…"
    words = "我们的团队已经完成了这个季度的全部项目工作"
    # Clauses of varying length put every mark at a line's end somewhere.
    text = "".join(words[: 3 + index % 17] + marks[index % len(marks)] for index in range(400))
    create(tmp_path, f"{text}\n\n- {text}\n")
    found = characters(tmp_path / "outputs/report.pdf")
    assert drawn_text(found) == compact(f"{text}•{text}")
    assert len(lines(found)) > 40
    assert not [line for line in lines(found) if line[0] in marks]
    assert_inside_content_frame(found)


def test_table_cells_and_list_items_wrap_east_asian_text_inside_their_column(tmp_path):
    long = "表格单元格中的长文本没有空格必须在列内换行" * 6
    markdown = (
        f"| 项目 | 説明 |\n| --- | --- |\n| {long} | 한국어 설명 {long} |\n\n"
        f"1. 第一项：{long}\n   - 嵌套项目：{long}\n"
    )
    create(tmp_path, markdown)
    found = characters(tmp_path / "outputs/report.pdf")
    assert drawn_text(found) == compact(
        f"项目 説明 {long} 한국어 설명 {long} 1. 第一项：{long} • 嵌套项目：{long}"
    )
    table = found[: len(compact(f"项目 説明 {long} 한국어 설명 {long}"))]
    divider = CONTENT_LEFT + (table[0].page_width - 2 * CONTENT_LEFT) / 2
    # No table character crosses the line between the two equal columns.
    assert not [item for item in table if item.left < divider < item.right]
    assert {item.left < divider for item in table} == {True, False}
    assert_inside_content_frame(found)


def test_bold_italic_and_heading_text_keep_east_asian_characters_in_the_regular_face(tmp_path):
    create(tmp_path, "# 見出し Heading\n\n**粗体中文 bold** and *斜体の日本語 italic*\n")
    found = characters(tmp_path / "outputs/report.pdf")
    assert drawn_text(found) == compact("見出し Heading 粗体中文 bold and 斜体の日本語 italic")
    fonts = {item.char: item.font for item in found}
    assert "Bold" in fonts["H"] and "Bold" in fonts["b"]
    assert "Italic" in fonts["c"]
    # The fallback font has one weight: these characters stay regular, as capabilities say.
    for char in "見粗斜":
        assert fonts[char].endswith("DroidSansFallback"), fonts[char]
    assert "single regular weight" in pdf_capability_status()["limits"]


@pytest.mark.parametrize(
    "text,missing",
    [
        ("𠮷野家", "U+20BB7"),  # an ideograph outside the bundled fonts' blocks
        ("Status 😀 ready", "U+1F600"),
        ("ሰላም", "U+120B, U+121D, U+1230"),
        ("\u304b\u3099", "U+3099"),  # a combining mark is never borrowed from a fallback font
    ],
)
def test_characters_no_bundled_font_can_draw_still_fail_clearly(tmp_path, text, missing):
    with pytest.raises(ValueError, match=f"^Unsupported font glyphs: {re.escape(missing)}$"):
        create(tmp_path, text)
    assert not (tmp_path / "outputs/report.pdf").exists()
    assert not (tmp_path / "scratch/review").exists()


def test_decomposed_hangul_needs_shaping_and_is_refused(tmp_path):
    with pytest.raises(ValueError, match="complex-script shaping"):
        create(tmp_path, "\u1112\u1161\u11ab\u1100\u116e\u11a8")
    assert not (tmp_path / "outputs/report.pdf").exists()


def test_text_the_body_font_covers_loads_no_fallback_font():
    from reportlab.pdfbase import pdfmetrics

    body = frozenset(pdfmetrics.getFont(documents._font_setup()[0]).face.charToGlyph)
    assert documents._fallback_fonts("Ελληνικά, Русский and English 2026.", body) == ()
    assert documents._fallback_fonts("中文", body)


def test_fallback_fonts_ship_in_the_package_with_their_licenses_and_sources():
    from reportlab.pdfbase import pdfmetrics

    package_fonts = Path(document_tools.__file__).resolve().parent / "fonts"
    assert documents.FONT_DIRECTORY.resolve() == package_fonts
    upstream = json.loads((package_fonts / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert set(upstream["fonts"]) == {filename for _name, filename in documents.FALLBACK_FONTS}
    for name, filename in documents.FALLBACK_FONTS:
        record = upstream["fonts"][filename]
        font = package_fonts / filename
        assert hashlib.sha256(font.read_bytes()).hexdigest() == record["sha256"]
        assert record["modified"] is False and record["repository"] and record["path"]
        license_text = (package_fonts / record["license_file"]).read_text(encoding="utf-8")
        assert {"Apache-2.0": "Apache License", "OFL-1.1": "SIL OPEN FONT LICENSE"}[
            record["license"]
        ] in license_text
        documents._register_font(name, filename, single_weight=True)
        assert Path(pdfmetrics.getFont(name).face.filename).resolve() == font


def test_artifact_check_renders_an_east_asian_pdf_without_clipping(tmp_path):
    create(tmp_path, "\n\n".join([*SAMPLES.values(), "".join(SAMPLES.values()) * 4]))
    report = ArtifactChecks(tmp_path).check("outputs/report.pdf")
    assert [(item["id"], item["status"]) for item in report["items"]] == [
        ("structure", "passed"),
        ("render", "passed"),
    ]
    assert all(not page["characters_outside_page"] for page in report["preview"]["pages"])
    assert all(page["text_characters"] for page in report["preview"]["pages"])


def _font_license_field(font: Path) -> str:
    """The license description a TrueType font carries about itself (name ID 13)."""
    data = font.read_bytes()
    tables = struct.unpack(">H", data[4:6])[0]
    for index in range(tables):
        tag, _checksum, offset, _length = struct.unpack(
            ">4sLLL", data[12 + 16 * index : 28 + 16 * index]
        )
        if tag == b"name":
            break
    else:
        raise AssertionError(f"{font.name} has no name table")
    _format, count, strings = struct.unpack(">HHH", data[offset : offset + 6])
    for index in range(count):
        record = data[offset + 6 + 12 * index : offset + 18 + 12 * index]
        platform, _encoding, _language, name_id, length, start = struct.unpack(">6H", record)
        if name_id == 13 and platform == 3:
            text = data[offset + strings + start : offset + strings + start + length]
            return text.decode("utf-16-be")
    raise AssertionError(f"{font.name} names no license")


def test_body_fonts_ship_with_the_license_they_name_themselves():
    package_fonts = Path(document_tools.__file__).resolve().parent / "fonts"
    license_text = (package_fonts / "LICENSE_LIBERATION").read_text(encoding="utf-8")
    shipped_licenses = {
        "Liberation Fonts license": ("LIBERATION FONT SOFTWARE", "GNU GENERAL PUBLIC LICENSE"),
        "SIL Open Font License": ("SIL OPEN FONT LICENSE",),
    }
    body_fonts = sorted(package_fonts.glob("LiberationSans-*.ttf"))
    assert len(body_fonts) == 4
    for font in body_fonts:
        field = _font_license_field(font)
        (expected,) = [texts for name, texts in shipped_licenses.items() if name in field]
        assert all(text in license_text for text in expected), font.name
