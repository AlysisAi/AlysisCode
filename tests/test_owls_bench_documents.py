"""Offline checks of the owls benchmark's file readers, on small files built in the test."""

from __future__ import annotations

import io
import zipfile

import pytest

from scripts.qa.owls_bench import builders, documents


def _rezip(data: bytes, *, drop: set[str] = frozenset(), replace: dict | None = None) -> bytes:
    """A copy of a zip package without some parts, or with some parts' bytes replaced."""
    replace = replace or {}
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            if info.filename in drop:
                continue
            target.writestr(info.filename, replace.get(info.filename, source.read(info.filename)))
    return buffer.getvalue()


def test_good_workbook_counts_rows_columns_links_and_charts():
    data = builders.xlsx_bytes(
        {
            "Μουσεία": [
                ["Όνομα", "Τιμή", "Πηγή"],
                ["Ακρόπολη", 20, "https://museum-one.example/el"],
                ["Μπενάκη", 12.5, "https://museum-two.example/info"],
                ["Σύνολο", builders.Formula('HYPERLINK("https://museum-three.example/","x")')],
            ]
        },
        hyperlinks={"Μουσεία": [("C2", "https://museum-one.example/el")]},
        chart=True,
    )
    inspection = documents.inspect_bytes("museums.xlsx", data)
    assert inspection.opens and inspection.kind == "xlsx"
    table = inspection.tables[0]
    assert table.name == "Μουσεία"
    assert table.data_rows == 3
    assert table.header_width == 3
    assert table.rows[1][0].text == "Ακρόπολη"
    assert table.rows[2][1].number == 12.5 and table.rows[2][1].decimals == 1
    sources = documents.count_sources(inspection.links)
    assert sources["distinct_urls"] == 3
    assert inspection.has_chart


def test_inline_strings_read_like_shared_strings():
    rows = [["Πόλη", "Τιμή"], ["Ηράκλειο", 3]]
    shared = documents.inspect_bytes("a.xlsx", builders.xlsx_bytes({"S": rows}))
    inline = documents.inspect_bytes(
        "b.xlsx", builders.xlsx_bytes({"S": rows}, shared_strings=False)
    )
    assert [c.text for c in shared.tables[0].rows[1]] == [c.text for c in inline.tables[0].rows[1]]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: _rezip(data, drop={"xl/worksheets/sheet1.xml"}), "missing"),
        (lambda data: _rezip(data, replace={"xl/workbook.xml": b"<workbook"}), "well-formed"),
        (lambda data: data[: len(data) // 2], "zip"),
        (lambda data: _rezip(data, drop={"_rels/.rels"}), "not a Word, Excel or PowerPoint"),
    ],
)
def test_broken_workbooks_do_not_open(mutate, message):
    good = builders.xlsx_bytes({"S": [["a", "b"], ["c", 1]]})
    inspection = documents.inspect_bytes("broken.xlsx", mutate(good))
    assert not inspection.opens
    assert message in inspection.error


def test_document_paragraphs_tables_links_and_pages():
    data = builders.docx_bytes(
        ["Vergleich", "Empfehlung für eine Familie"],
        tables=[[["Produkt", "Lizenz"], ["Eins", "GPL"], ["Zwei", "AGPL"], ["Drei", "MIT"]]],
        hyperlinks=[("Quelle", "https://source.example/a")],
        pages=2,
        image=True,
    )
    inspection = documents.inspect_bytes("vergleich.docx", data)
    assert inspection.opens and inspection.kind == "docx"
    assert inspection.paragraphs == 3
    assert inspection.tables[0].data_rows == 3
    assert inspection.pages == 2
    assert "https://source.example/a" in inspection.links
    assert inspection.has_image and not inspection.has_chart


def test_document_without_recorded_page_count_says_so():
    inspection = documents.inspect_bytes("a.docx", builders.docx_bytes(["one"]))
    assert inspection.opens and inspection.pages is None
    assert any("page count" in note for note in inspection.notes)


def test_document_without_body_does_not_open():
    good = builders.docx_bytes(["one"])
    broken = _rezip(
        good,
        replace={
            "word/document.xml": b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main"/>'
        },
    )
    inspection = documents.inspect_bytes("a.docx", broken)
    assert not inspection.opens and "body" in inspection.error


def test_deck_counts_slides_and_links():
    data = builders.pptx_bytes(
        [["二十四节气"], ["由来"], ["春"], ["习俗"], ["总结", "https://source-a.example/x"]],
        hyperlinks={4: ["https://source-b.example/y"]},
    )
    inspection = documents.inspect_bytes("deck.pptx", data)
    assert inspection.opens and inspection.slides == 5
    assert documents.count_sources(inspection.links)["distinct_urls"] == 2


def test_deck_slide_listed_without_its_part_is_not_counted():
    data = builders.pptx_bytes([["one"], ["two"]], missing_slide_parts=1)
    inspection = documents.inspect_bytes("deck.pptx", data)
    assert inspection.opens and inspection.slides == 2
    assert any("no part" in note for note in inspection.notes)


def test_deck_without_slides_does_not_open():
    inspection = documents.inspect_bytes("deck.pptx", builders.pptx_bytes([]))
    assert not inspection.opens


@pytest.mark.parametrize("object_stream", [False, True])
def test_pdf_pages_and_links(object_stream):
    data = builders.pdf_bytes(
        ["Bayern", "Berlin", "Hamburg (Stadtstaat)"],
        object_stream=object_stream,
        links=["https://de.example/wiki/Bayern"],
    )
    inspection = documents.inspect_bytes("laender.pdf", data)
    assert inspection.opens and inspection.kind == "pdf"
    assert inspection.pages == 3
    assert "https://de.example/wiki/Bayern" in inspection.links
    assert "Hamburg (Stadtstaat)" in inspection.text


def test_pdf_page_count_is_conservative_when_the_tree_disagrees():
    data = builders.pdf_bytes(["a", "b"]).replace(b"/Count 2", b"/Count 9")
    inspection = documents.inspect_bytes("a.pdf", data)
    assert inspection.pages == 2
    assert any("smaller count" in note for note in inspection.notes)


def test_pdf_without_header_does_not_open_and_truncation_is_noted():
    assert not documents.inspect_bytes("a.pdf", b"not a pdf at all").opens
    truncated = builders.pdf_bytes(["a", "b"])[:-30]
    inspection = documents.inspect_bytes("a.pdf", truncated)
    assert inspection.opens
    assert any("cut short" in note for note in inspection.notes)


def test_csv_with_semicolons_bom_and_greek_text():
    text = "\ufeffΚατάστημα;Έσοδα (€)\nΠάτρα;1.234,50\nΗράκλειο;99,00\n"
    inspection = documents.inspect_bytes("sales.csv", text.encode("utf-8"))
    assert inspection.opens and inspection.kind == "csv"
    table = inspection.tables[0]
    assert table.rows[0][0].text == "Κατάστημα"
    assert table.data_rows == 2 and table.header_width == 2


def test_csv_that_is_not_utf8_opens_with_a_note():
    inspection = documents.inspect_bytes("a.csv", "Café;1\nThé;2\n".encode("latin-1"))
    assert inspection.opens
    assert any("not UTF-8" in note for note in inspection.notes)


def test_markdown_tables_and_list_items():
    reply = (
        "Αποτελέσματα:\n\n| Εταιρεία | Ώρα |\n|---|---|\n| Α | 07:00 |\n| Β | 08:30 |\n"
        "| Γ | 12:00 |\n\n1. πρώτο\n2. δεύτερο\n"
    )
    tables = documents.markdown_tables(reply)
    assert len(tables) == 1 and tables[0].data_rows == 3
    assert documents.list_items(reply) == 3
    assert documents.list_items("- one\n- two\n* three\n• four\n") == 4


def test_number_readings_are_locale_agnostic():
    assert documents.number_readings("1.234,56") == [(1234.56, 2)]
    assert documents.number_readings("1,234.56") == [(1234.56, 2)]
    assert documents.number_readings("12,5") == [(12.5, 1)]
    assert sorted(documents.number_readings("1,234")) == [(1.234, 3), (1234.0, 0)]
    assert documents.number_readings("-7") == [(-7.0, 0)]
    assert (219877.7, 2) in documents.numbers_in("Σύνολο: 219.877,70 €")
    assert (1234.5, 1) in documents.numbers_in("1\u202f234,5 mm")


def test_find_urls_trims_sentence_punctuation():
    text = (
        "δες https://el.wikipedia.org/wiki/Αθήνα. και (https://en.example.org/wiki/A_(b)) "
        "见https://zh.example.cn/页面。更多 [x](https://md.example/a), file:///C:/x.txt"
    )
    assert documents.find_urls(text) == [
        "https://el.wikipedia.org/wiki/Αθήνα",
        "https://en.example.org/wiki/A_(b)",
        "https://zh.example.cn/页面",
        "https://md.example/a",
        "file:///C:/x.txt",
    ]


def test_count_sources_distinct_pages_sites_and_non_web_links():
    links = [
        "https://www.shop-a.example/p/1",
        "https://shop-a.example/p/1/",
        "https://shop-a.example/p/2#reviews",
        "HTTPS://Shop-B.example/item",
        "mailto:info@shop-a.example",
        "file:///home/user/notes.txt",
        "javascript:void(0)",
    ]
    counted = documents.count_sources(links)
    assert counted["distinct_urls"] == 4
    assert counted["distinct_hosts"] == 2
    assert counted["not_web_count"] == 2
