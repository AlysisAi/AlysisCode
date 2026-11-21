"""Charts in Word, Excel, PowerPoint and PDF files made by the engine's document operations.

One chart object for every format: native, editable charts in the Office formats (Word's with
its own chart part and embedded data workbook), and a vector drawing in a PDF. Text in any
script is kept; the PDF draws it with the same font fallback as its body text.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from docx import Document
from lxml import etree
from pptx import Presentation
from pptx.enum.chart import XL_CHART_TYPE

from alysis_code.documents.__main__ import run as run_pdf
from alysis_code.office import charts, sheets, slides, word
from alysis_code.office.__main__ import run as run_office
from alysis_code.office.rendering import _check_renderable

GREEK_TITLE = "Αιτήματα ανά εβδομάδα"
CHINESE_TITLE = "每周工单数量"
CATEGORIES = ["Εβδ. 1", "第二周", "Εβδ. 3", "第四周"]
CHART = {
    "chart": "column",
    "title": f"{GREEK_TITLE} {CHINESE_TITLE}",
    "categories": CATEGORIES,
    "series": [
        {"name": "Δίκτυο", "values": [12, 9, 14, 7]},
        {"name": "网络", "values": [5, 7, 4, 11.5]},
    ],
    "x_title": "Εβδομάδα",
    "y_title": "Αιτήματα",
    "number_format": "#,##0",
}
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"


def chart(**changes) -> dict:
    value = copy.deepcopy(CHART)
    value.update(changes)
    return value


# ---------------------------------------------------------------- the chart object


@pytest.mark.parametrize(
    "changes,message",
    [
        (
            {"series": [{"name": "Δίκτυο", "values": [1, 2, 3]}]},
            "series 'Δίκτυο' has 3 values for 4 categories; give exactly one value per category",
        ),
        (
            {"series": [{"name": "A", "values": [1, float("nan"), 3, 4]}]},
            "series 1 values: value 2 must be a finite number",
        ),
        (
            {"series": [{"name": "A", "values": [1, float("inf"), 3, 4]}]},
            "value 2 must be a finite number",
        ),
        ({"series": [{"name": "A", "values": [1, True, 3, 4]}]}, "value 2 must be a number"),
        ({"series": [{"name": "A", "values": [1, "2", 3, 4]}]}, "value 2 must be a number"),
        (
            {
                "chart": "pie",
                "x_title": None,
                "y_title": None,
                "series": [{"name": "A", "values": [4, -1, 3, 2]}],
            },
            "a pie chart's values must not be negative (value 2 is -1)",
        ),
        ({"chart": "pie"}, "a pie chart takes exactly one series"),
        (
            {"chart": "pie", "series": [{"name": "A", "values": [1, 2, 3, 4]}]},
            "a pie chart has no axes; remove x_title and y_title",
        ),
        (
            {
                "chart": "pie",
                "x_title": None,
                "y_title": None,
                "series": [{"name": "A", "values": [0, 0, 0, 0]}],
            },
            "a pie chart needs at least one value above zero",
        ),
        (
            {
                "categories": [f"{n}" for n in range(1001)],
                "series": [{"name": "A", "values": [1] * 1001}],
            },
            "at most 1000 categories fit a chart (got 1001)",
        ),
        (
            {"series": [{"name": f"S{n}", "values": [1, 2, 3, 4]} for n in range(51)]},
            "series: must be a list of 1 to 50 series",
        ),
        (
            {
                "categories": list(range(300)),
                "series": [{"name": f"S{n}", "values": [1] * 300} for n in range(34)],
            },
            "a chart holds at most 10000 values",
        ),
        ({"chart": "area"}, "chart: Input should be 'column', 'bar', 'line', 'pie' or 'scatter'"),
        ({"colour": "red"}, "colour: is not a chart field"),
        (
            {"series": [{"name": "A", "values": [1, 2, 3, 4], "color": "red"}]},
            "series 1 color: is not a series field",
        ),
        ({"series": ["A"]}, "series 1: must be an object"),
        ({"series": []}, "series: must be a list of 1 to 50 series"),
        ({"categories": []}, "categories: must be a non-empty list"),
        ({"chart": "scatter"}, "a scatter chart's categories are its x values and must be numbers"),
        ({"number_format": "0.00;[Red]-0.00"}, "number_format: must be like 0, 0.00"),
        ({"title": "Line\nbreak"}, "title: must be one line of text"),
        ({"series": [{"name": "", "values": [1, 2, 3, 4]}]}, "series 1 name: must not be empty"),
        ({"categories": "A2:A5"}, "cell ranges work only in an xlsx workbook"),
    ],
)
def test_a_chart_the_formats_refuse_says_plainly_what_to_fix(changes, message):
    value = chart(**changes)
    for key in [key for key, item in value.items() if item is None]:
        value.pop(key)
    with pytest.raises(ValueError) as error:
        charts.parse(value, "Chart 1")
    assert str(error.value).startswith("Chart 1: ")
    assert message in str(error.value)
    # Plain words, not the validation library's own wording.
    assert "Value error" not in str(error.value) and "pydantic" not in str(error.value)


def test_a_chart_without_a_kind_is_a_column_chart_and_takes_its_type_marker():
    parsed = charts.parse(
        {"type": "chart", "categories": ["α"], "series": [{"name": "Α", "values": [1]}]}, "Chart"
    )
    assert parsed.chart == "column" and parsed.title is None and not parsed.legend


@pytest.mark.parametrize(
    "value,number_format,expected",
    [
        (1234.5, "#,##0.00", "1,234.50"),
        (-1234.5, '#,##0 "€"', "-1,235 €"),
        (0.256, "0.0%", "25.6%"),
        (2.5, "0", "3"),  # halves round away from zero, as spreadsheets show them
        (-0.4, "0", "0"),
        (12, '"$"#,##0', "$12"),
        (7.25, None, "7.25"),
    ],
)
def test_number_formats_show_values_as_spreadsheets_do(value, number_format, expected):
    decimals = 2 if number_format is None else 0
    assert charts.format_number(value, number_format, decimals) == expected


# ---------------------------------------------------------------- Word


def docx_with_chart(tmp_path, **changes) -> tuple[Path, dict]:
    output = tmp_path / "report.docx"
    result = word.create(
        {
            "title": "Αναφορά",
            "blocks": [
                {"kind": "paragraph", "text": "Σύνοψη 摘要"},
                {"kind": "chart", **chart(**changes)},
                {"kind": "paragraph", "text": "Τέλος"},
            ],
        },
        output,
        tmp_path,
    )
    return output, result


def test_a_word_chart_is_native_with_its_data_workbook_and_inline_drawing(tmp_path):
    output, result = docx_with_chart(tmp_path)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        types = archive.read("[Content_Types].xml").decode()
        chart_xml = archive.read("word/charts/chart1.xml")
        chart_rels = archive.read("word/charts/_rels/chart1.xml.rels").decode()
        document_rels = archive.read("word/_rels/document.xml.rels").decode()
        body = etree.fromstring(archive.read("word/document.xml"))
        workbook = archive.read("word/embeddings/Microsoft_Excel_Sheet1.xlsx")
    assert (
        'PartName="/word/charts/chart1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"'
    ) in types
    assert "spreadsheetml.sheet" in types and "word/embeddings/Microsoft_Excel_Sheet1.xlsx" in names
    # The chart cites its data workbook, as Word's Edit Data needs.
    space = etree.fromstring(chart_xml)
    data_rid = space.find(f"{C}externalData").get(
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    )
    assert f'Id="{data_rid}"' in chart_rels and "relationships/package" in chart_rels
    assert "../embeddings/Microsoft_Excel_Sheet1.xlsx" in chart_rels
    assert 'relationships/chart" Target="charts/chart1.xml"' in document_rels
    # The body shows it inline, at the full text width.
    inline = body.find(".//{*}drawing/{*}inline")
    assert inline is not None and inline.find(f".//{C}chart") is not None
    extent = inline.find("{*}extent")
    section = Document(output).sections[0]
    assert int(extent.get("cx")) == section.page_width - section.left_margin - section.right_margin
    assert inline.find("{*}docPr").get("descr") == CHART["title"]
    # The data workbook holds the chart's categories and values.
    book = openpyxl.load_workbook(io.BytesIO(workbook))
    rows = [[cell.value for cell in row] for row in book.active.iter_rows()]
    assert rows[1:] == [
        [c, *v] for c, *v in zip(CATEGORIES, [12, 9, 14, 7], [5, 7, 4, 11.5], strict=True)
    ]
    assert rows[0][1:] == ["Δίκτυο", "网络"]
    # python-docx reopens it; the title, axis titles and number format are the chart's own.
    Document(output)
    texts = "".join(node.text or "" for node in space.iter())
    assert CHART["title"] in texts and "Εβδομάδα" in texts and "Αιτήματα" in texts
    assert space.find(f".//{C}valAx/{C}numFmt").get("formatCode") == "#,##0"
    # Inspection lists it where it is, so an edit can refer to it.
    assert result["charts"] == [
        {
            "locator": "paragraph:3",
            "chart": "column",
            "title": CHART["title"],
            "series": [{"name": "Δίκτυο", "points": 4}, {"name": "网络", "points": 4}],
        }
    ]
    assert word.inspect(output)["charts"] == result["charts"]
    # The renderer's own package check accepts the chart's data workbook.
    _check_renderable(output)


@pytest.mark.parametrize("kind", ["bar", "line", "scatter", "pie"])
def test_every_chart_kind_is_a_native_word_chart(tmp_path, kind):
    changes = {"chart": kind}
    if kind == "scatter":
        changes["categories"] = [1, 2.5, 4, 8]
    if kind == "pie":
        changes.update(series=[{"name": "Μερίδιο", "values": [5, 3, 0, 2]}])
    value = chart(**changes)
    if kind == "pie":
        value.pop("x_title"), value.pop("y_title")
    output = tmp_path / "kinds.docx"
    result = word.create({"blocks": [{"kind": "chart", **value}]}, output, tmp_path)
    assert result["charts"][0]["chart"] == kind
    with zipfile.ZipFile(output) as archive:
        space = etree.fromstring(archive.read("word/charts/chart1.xml"))
    plots = {element.tag.removeprefix(C) for element in space.find(f"{C}chart/{C}plotArea")}
    expected = {
        "bar": "barChart",
        "line": "lineChart",
        "scatter": "scatterChart",
        "pie": "pieChart",
    }
    assert expected[kind] in plots
    if kind == "bar":
        # Categories read from the top, in the order given.
        assert space.find(f".//{C}catAx/{C}scaling/{C}orientation").get("val") == "maxMin"


def test_a_word_chart_survives_an_edit_of_another_paragraph(tmp_path):
    source, _ = docx_with_chart(tmp_path)
    inspected = word.inspect(source)
    paragraph = next(p for p in inspected["paragraphs"] if p["text"] == "Τέλος")
    output = tmp_path / "edited.docx"
    word.edit(
        source,
        {
            "expected_source_sha256": inspected["source_sha256"],
            "edits": [
                {
                    "locator": paragraph["locator"],
                    "expected_text_sha256": paragraph["text_sha256"],
                    "find": "Τέλος",
                    "replace": "Τέλος 结束",
                }
            ],
        },
        output,
        tmp_path,
    )
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        for name in before.namelist():
            if name != word.DOC_PART:
                assert before.read(name) == after.read(name), name
    assert word.inspect(output)["charts"] == inspected["charts"]


def rewrite(path: Path, changes: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts.update(changes)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def test_only_a_charts_own_plain_workbook_is_accepted_as_embedded_data(tmp_path):
    source, _ = docx_with_chart(tmp_path)
    with zipfile.ZipFile(source) as archive:
        workbook = archive.read("word/embeddings/Microsoft_Excel_Sheet1.xlsx")
    # A workbook no chart cites is an embedded file like any other.
    stray = tmp_path / "stray.docx"
    stray.write_bytes(source.read_bytes())
    rewrite(stray, {"word/embeddings/other.xlsx": workbook})
    with pytest.raises(ValueError, match="embedded files"):
        word.inspect(stray)
    # A chart's data workbook that carries a program is refused.
    with zipfile.ZipFile(io.BytesIO(workbook)) as inner:
        inner_parts = {name: inner.read(name) for name in inner.namelist()}
    inner_parts["xl/vbaProject.bin"] = b"program"
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as inner:
        for name, data in inner_parts.items():
            inner.writestr(name, data)
    macro = tmp_path / "macro.docx"
    macro.write_bytes(source.read_bytes())
    rewrite(macro, {"word/embeddings/Microsoft_Excel_Sheet1.xlsx": packed.getvalue()})
    with pytest.raises(ValueError, match="must not hold programs"):
        word.inspect(macro)


def test_an_invalid_word_chart_writes_nothing(tmp_path):
    output = tmp_path / "invalid.docx"
    with pytest.raises(ValueError, match=r"Block 2 \(chart\): series 1 values: value 3"):
        word.create(
            {
                "blocks": [
                    {"kind": "paragraph", "text": "Α"},
                    {"kind": "chart", **chart(series=[{"name": "A", "values": [1, 2, None, 4]}])},
                ]
            },
            output,
            tmp_path,
        )
    assert not output.exists()


# ---------------------------------------------------------------- PowerPoint


def test_a_slide_chart_is_native_and_inspection_lists_it(tmp_path):
    output = tmp_path / "deck.pptx"
    result = slides.create(
        {
            "slides": [
                {
                    "title": "Αιτήματα 工单",
                    "elements": [
                        {"type": "chart", **chart(), "x": 0.6, "y": 1.5, "width": 12, "height": 5.5}
                    ],
                }
            ]
        },
        output,
        tmp_path,
    )
    frame = next(shape for shape in Presentation(output).slides[0].shapes if shape.has_chart)
    native = frame.chart
    assert native.chart_type == XL_CHART_TYPE.COLUMN_CLUSTERED
    assert native.chart_title.text_frame.text == CHART["title"]
    assert list(native.plots[0].categories) == CATEGORIES
    assert [(s.name, s.values) for s in native.plots[0].series] == [
        ("Δίκτυο", (12.0, 9.0, 14.0, 7.0)),
        ("网络", (5.0, 7.0, 4.0, 11.5)),
    ]
    assert native.value_axis.axis_title.text_frame.text == "Αιτήματα"
    assert native.value_axis.tick_labels.number_format == "#,##0"
    assert native.part.chart_workbook.xlsx_part is not None  # PowerPoint's Edit Data
    listed = [shape["chart"] for shape in result["slides"][0]["shapes"] if "chart" in shape]
    assert listed == [
        {
            "chart": "column",
            "title": CHART["title"],
            "series": [{"name": "Δίκτυο", "points": 4}, {"name": "网络", "points": 4}],
        }
    ]
    _check_renderable(output)


def test_a_slide_chart_must_fit_the_slide_and_be_valid(tmp_path):
    element = {"type": "chart", **chart(), "x": 10, "y": 1, "width": 6, "height": 4}
    with pytest.raises(ValueError, match="outside the slide"):
        slides.create({"slides": [{"elements": [element]}]}, tmp_path / "a.pptx", tmp_path)
    element.update(x=1, chart="donut")
    with pytest.raises(ValueError, match="Slide 1 chart: chart: Input should be"):
        slides.create({"slides": [{"elements": [element]}]}, tmp_path / "b.pptx", tmp_path)
    assert not list(tmp_path.glob("*.pptx"))


# ---------------------------------------------------------------- Excel


def test_listed_values_are_written_beside_the_data_and_the_chart_refers_to_them(tmp_path):
    output = tmp_path / "book.xlsx"
    result = sheets.create(
        {
            "sheets": [
                {
                    "name": "Σύνοψη",
                    "rows": [["Πόλη", "Ποσό"], ["Αθήνα", 3]],
                    "charts": [chart()],
                }
            ]
        },
        output,
        tmp_path,
    )
    sheet = openpyxl.load_workbook(output)["Σύνοψη"]
    # Data area: one blank column after the sheet's data, the categories then each series.
    assert [[cell.value for cell in row] for row in sheet["D1:F5"]] == [
        ["Εβδομάδα", "Δίκτυο", "网络"],
        *([c, *v] for c, *v in zip(CATEGORIES, [12, 9, 14, 7], [5, 7, 4, 11.5], strict=True)),
    ]
    assert sheet["E2"].number_format == "#,##0"
    (native,) = sheet._charts
    assert native.series[0].val.numRef.f == "Σύνοψη!$E$2:$E$5"
    assert native.series[0].cat.strRef.f == "Σύνοψη!$D$2:$D$5"
    assert native.series[0].tx.strRef.f == "Σύνοψη!$E$1"
    (listed,) = result["sheets"][0]["charts"]
    assert listed["chart"] == "column" and listed["title"] == CHART["title"]
    assert listed["anchor"] == "H1"  # beside the data area
    assert [(s["name"], s["points"]) for s in listed["series"]] == [("Δίκτυο", 4), ("网络", 4)]
    assert listed["series"][1]["values_range"] == "Σύνοψη!$F$2:$F$5"
    # A chart is not kept by openpyxl's cell edits, so editing says to create it again.
    assert not result["editable"]
    assert any("create the workbook again" in item for item in result["edit_blockers"])


def test_a_chart_can_refer_to_cells_already_in_the_workbook(tmp_path):
    output = tmp_path / "ranges.xlsx"
    rows = [
        ["Εβδομάδα", "Δίκτυο"],
        *([c, n] for c, n in zip(CATEGORIES, [3, 5, 2, 7], strict=True)),
    ]
    result = sheets.create(
        {
            "sheets": [
                {"name": "Δεδομένα", "rows": rows},
                {
                    "name": "Γράφημα",
                    "charts": [
                        {
                            "chart": "line",
                            "title": "Q1!A1 τάση",  # text that looks like a cell stays text
                            "categories": "'Δεδομένα'!A2:A5",
                            "series": [{"name": "Δίκτυο", "values": "Δεδομένα!$B$2:$B$5"}],
                            "anchor": "B2",
                        }
                    ],
                },
            ]
        },
        output,
        tmp_path,
    )
    book = openpyxl.load_workbook(output)
    (native,) = book["Γράφημα"]._charts
    assert native.series[0].val.numRef.f == "Δεδομένα!$B$2:$B$5"
    assert native.series[0].tx.v == "Δίκτυο"
    assert "".join(run.t for p in native.title.tx.rich.p for run in p.r) == "Q1!A1 τάση"
    assert book["Γράφημα"].max_column == 1  # nothing written beside: the cells exist already
    (listed,) = result["sheets"][1]["charts"]
    assert listed["anchor"] == "B2" and listed["series"][0]["points"] == 4


@pytest.mark.parametrize(
    "reference,message",
    [
        ({"categories": "A2:A5", "values": "B2:B4"}, "has 3 values for 4 categories"),
        ({"categories": "A2:A5", "values": "A2:A5"}, "cell A2 is not a number"),
        ({"categories": "A2:B5", "values": "B2:B5"}, "must be a single row or column"),
        ({"categories": "Άλλο!A2:A5", "values": "B2:B5"}, "names a sheet the workbook lacks"),
        ({"categories": "A2:A5", "values": [1, 2, 3, 4]}, "or cell ranges for all of them"),
    ],
)
def test_chart_ranges_that_do_not_fit_the_data_are_refused(tmp_path, reference, message):
    rows = [
        ["Εβδομάδα", "Δίκτυο"],
        *([c, n] for c, n in zip(CATEGORIES, [3, 5, 2, 7], strict=True)),
    ]
    spec = {
        "sheets": [
            {
                "name": "Δεδομένα",
                "rows": rows,
                "charts": [
                    {
                        "categories": reference["categories"],
                        "series": [{"name": "Δίκτυο", "values": reference["values"]}],
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match=re.escape(message)):
        sheets.create(spec, tmp_path / "invalid.xlsx", tmp_path)
    assert not (tmp_path / "invalid.xlsx").exists()


def test_charts_are_placed_so_that_no_page_cuts_one_when_printed(tmp_path):
    output = tmp_path / "pages.xlsx"
    rows = [["Εβδομάδα", "Δίκτυο"], *([f"Εβδ. {n}", n] for n in range(1, 41))]
    spec = {
        "sheets": [
            {
                "name": "Δεδομένα",
                "rows": rows,
                "charts": [
                    {
                        "chart": kind,
                        "categories": "A2:A41",
                        "series": [{"name": "Α", "values": "B2:B41"}],
                    }
                    for kind in ("column", "line", "bar", "pie", "column")
                ],
            }
        ]
    }
    parsed = sheets._create_spec(spec)[0]
    layout = parsed["print"]
    page_rows = layout["breaks"][0]
    assert layout["orientation"] == "landscape" and 10 <= layout["scale"] <= 100
    for entry in parsed["charts"]:
        top = entry["anchor"][0]
        bottom = top + -(-entry["size"][1] // sheets.ROW_PIXELS)
        # Each chart sits inside one page, clear of its edges, with a page break before it.
        assert (top - 1) // page_rows == (bottom - 1) // page_rows
        assert (top - 1) % page_rows != 0 or top == 1
    sheets.create(spec, output, tmp_path)
    xml = zipfile.ZipFile(output).read("xl/worksheets/sheet1.xml").decode()
    assert f'scale="{layout["scale"]}"' in xml or layout["scale"] == 100
    assert f'<brk id="{page_rows}"' in xml


def test_the_print_scale_fits_a_renderer_that_draws_wider_columns():
    # Without the workbook's font a renderer draws each column with the font it substitutes;
    # charts beside wide data must still print inside the page's width.
    charts = [chart(chart=kind) for kind in ("column", "bar", "line")]
    pie = chart(chart="pie")
    pie.update(series=pie["series"][:1])
    pie.pop("x_title"), pie.pop("y_title")
    spec = {"sheets": [{"name": "Φύλλο", "charts": [*charts, pie]}]}
    parsed = sheets._create_spec(spec)[0]
    layout = parsed["print"]
    page_width = sheets.PRINTABLE[layout["orientation"]][0]
    for digit in (sheets.EXCEL_DIGIT, sheets.WIDEST_DIGIT):
        right = max(
            sheets._chart_edge(parsed, entry["anchor"][1], entry["size"][0], digit)
            for entry in parsed["charts"]
        )
        assert right * layout["scale"] / 100 <= page_width


# ---------------------------------------------------------------- PDF


def make_pdf(root: Path, markdown: str) -> dict:
    (root / "draft.md").write_text(markdown, encoding="utf-8")
    return run_pdf(
        SimpleNamespace(
            workspace=str(root),
            command="create",
            source="draft.md",
            output="report.pdf",
            preview_dir="review",
            title="Αναφορά",
            overwrite=False,
        )
    )


def fence(value) -> str:
    return "```chart\n" + (value if isinstance(value, str) else json.dumps(value)) + "\n```\n"


def test_a_pdf_draws_a_chart_block_with_greek_and_chinese_text(tmp_path):
    import pypdfium2
    import pypdfium2.raw as pdfium_c

    markdown = "# Αναφορά\n\nΣύνοψη.\n\n" + fence(chart()) + "\nΤέλος.\n"
    result = make_pdf(tmp_path, markdown)
    assert result["status"] == "created" and result["page_count"] == 1
    with pypdfium2.PdfDocument(tmp_path / "report.pdf") as pdf:
        page = pdf[0]
        text = page.get_textpage().get_text_range()
        paths = [obj for obj in page.get_objects() if obj.type == pdfium_c.FPDF_PAGEOBJ_PATH]
    for expected in [GREEK_TITLE, CHINESE_TITLE, *CATEGORIES, "Δίκτυο", "网络", "Αιτήματα"]:
        assert expected in text.replace("\r\n", " ").replace("\n", " ")
    # Eight bars, the gridlines and the legend's swatches are vector paths.
    assert len(paths) >= 8 + 2
    # The values axis uses the chart's number format.
    assert "15" in text and "15.0" not in text


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("bar", {}),
        ("line", {}),
        ("scatter", {"categories": [1, 2.5, 4, 8]}),
        ("pie", {"series": [{"name": "Μ", "values": [5, 3, 1, 2]}]}),
    ],
)
def test_a_pdf_draws_every_chart_kind_inside_its_page(tmp_path, kind, changes):
    value = chart(chart=kind, **changes)
    if kind == "pie":
        value.pop("x_title"), value.pop("y_title")
    result = make_pdf(tmp_path, fence(value))
    # The builder's own check: no text outside the content frame of any page.
    assert all(not page["characters_outside_content_frame"] for page in result["pages"])
    import pypdfium2
    import pypdfium2.raw as pdfium_c

    with pypdfium2.PdfDocument(tmp_path / "report.pdf") as pdf:
        page = pdf[0]
        text = page.get_textpage().get_text_range()
        paths = sum(obj.type == pdfium_c.FPDF_PAGEOBJ_PATH for obj in page.get_objects())
    # Drawn, not printed as code: the title is there, the JSON is not, and marks are paths.
    assert GREEK_TITLE in text and '"series"' not in text and paths >= 4


@pytest.mark.parametrize(
    "block,message",
    [
        ('{"categories": ["α"], "series": [{"name": "Α", "values": [NaN]}]}', "must be finite"),
        ('{"categories": ["α"], "categories": ["β"]}', "the field 'categories' appears twice"),
        ('{"categories": ["α"], "series": [', "must hold one JSON chart object"),
        (
            json.dumps({"categories": "A1:A3", "series": [{"name": "Α", "values": "B1:B3"}]}),
            "cell ranges work only in an xlsx workbook",
        ),
        (
            json.dumps({"categories": ["α", "β"], "series": [{"name": "Α", "values": [1]}]}),
            "Chart block 1: series 'Α' has 1 values for 2 categories",
        ),
    ],
)
def test_a_pdf_chart_block_that_cannot_be_drawn_publishes_nothing(tmp_path, block, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        make_pdf(tmp_path, "# Α\n\n" + fence(block))
    assert not (tmp_path / "report.pdf").exists()


def test_other_fenced_blocks_stay_code(tmp_path):
    make_pdf(tmp_path, "# Α\n\n```json\n" + json.dumps(chart()) + "\n```\n")
    import pypdfium2

    with pypdfium2.PdfDocument(tmp_path / "report.pdf") as pdf:
        assert '"categories"' in pdf[0].get_textpage().get_text_range()


# ---------------------------------------------------------------- describing the element


@pytest.mark.parametrize("format_name", ["docx", "xlsx", "pptx"])
def test_each_formats_description_has_a_chart_example_that_works(tmp_path, format_name):
    guide = run_office(SimpleNamespace(command="describe", format=format_name))["guide"]
    text = json.dumps(guide, ensure_ascii=False)
    assert "pie" in text and "scatter" in text and "number_format" in text
    output = tmp_path / f"example.{format_name}"
    if format_name == "docx":
        word.create({"blocks": [guide["chart"]["block"]]}, output, tmp_path)
        assert word.inspect(output)["charts"][0]["chart"] == "column"
    elif format_name == "pptx":
        element = next(item for item in guide["elements"] if item["type"] == "chart")
        result = slides.create({"slides": [{"elements": [element]}]}, output, tmp_path)
        assert any("chart" in shape for shape in result["slides"][0]["shapes"])
    else:
        result = sheets.create({"sheets": [guide["chart"]["example"]]}, output, tmp_path)
        assert result["sheets"][0]["charts"][0]["chart"] == "line"


def test_the_pdf_description_has_a_chart_example_that_works(tmp_path):
    from alysis_code.documents import pdf_capability_status

    status = pdf_capability_status()
    assert any("chart" in item for item in status["supports"])
    example = status["chart"]["example"]
    result = make_pdf(tmp_path, "# Α\n\n" + example + "\n")
    assert result["status"] == "created"


# ---------------------------------------------------------------- rendering


@pytest.fixture
def real_renderer(monkeypatch):
    executable = os.environ.get("ALYSIS_TEST_OFFICE_SOFFICE")
    if not executable:
        pytest.skip("Set ALYSIS_TEST_OFFICE_SOFFICE to render with an installed LibreOffice")
    if sys.platform not in {"darwin", "linux"}:
        pytest.skip("Office renderer platforms")
    monkeypatch.setenv("ALYSIS_OFFICE_SOFFICE", executable)


@pytest.mark.parametrize("format_name", ["docx", "pptx", "xlsx"])
def test_documents_with_charts_render_with_every_page_inside(tmp_path, real_renderer, format_name):
    from alysis_code.artifact_checks import ArtifactChecks

    output = tmp_path / f"charts.{format_name}"
    kinds = ("column", "bar", "line", "pie")

    def kind_chart(kind):
        value = chart(chart=kind)
        if kind == "pie":
            value.update(series=value["series"][:1])
            value.pop("x_title"), value.pop("y_title")
        return value

    if format_name == "docx":
        word.create(
            {"blocks": [{"kind": "chart", **kind_chart(kind)} for kind in kinds]}, output, tmp_path
        )
    elif format_name == "pptx":
        slides.create(
            {
                "slides": [
                    {
                        "elements": [
                            {
                                "type": "chart",
                                **kind_chart(k),
                                "x": 0.5,
                                "y": 0.5,
                                "width": 12,
                                "height": 6,
                            }
                        ]
                    }
                    for k in kinds
                ]
            },
            output,
            tmp_path,
        )
    else:
        sheets.create(
            {"sheets": [{"name": "Φύλλο", "charts": [kind_chart(k) for k in kinds]}]},
            output,
            tmp_path,
        )
    report = ArtifactChecks(tmp_path).check(output.name)
    assert [(item["label"], item["status"]) for item in report["items"]] == [
        ("File structure", "passed"),
        ("Rendered pages", "passed"),
    ]
    rendered = Path(tmp_path / report["preview"]["pdf"])
    import pypdfium2

    with pypdfium2.PdfDocument(rendered) as pdf:
        text = " ".join(page.get_textpage().get_text_range() for page in pdf)
    assert "Δίκτυο" in text and GREEK_TITLE in text
