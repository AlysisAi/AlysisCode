"""Small, valid Office and PDF files, written with the standard library.

The benchmark's attached workbook is built with these, and the offline tests use them to
make good and broken deliverables. The packages hold only the parts a reader needs; a
workbook also carries styles so spreadsheet apps open it without repair.
"""

from __future__ import annotations

import io
import zipfile
import zlib
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_OFFICE_DOC = _DOC_REL + "/officeDocument"
_HYPERLINK = _DOC_REL + "/hyperlink"
_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PRES_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAW_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
# A 1x1 transparent PNG, for packages that embed a picture.
PNG_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
    b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True)
class Formula:
    """A cell formula, with the value a spreadsheet app would have cached (or none)."""

    text: str
    cached: float | None = None


def _column(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _rels(items: list[tuple[str, str, str, bool]]) -> str:
    body = "".join(
        f"<Relationship Id={quoteattr(rid)} Type={quoteattr(kind)} Target={quoteattr(target)}"
        + (' TargetMode="External"' if external else "")
        + "/>"
        for rid, kind, target, external in items
    )
    return f'{_DECL}<Relationships xmlns="{_PKG_REL}">{body}</Relationships>'


def _content_types(overrides: dict[str, str], *, png: bool = False) -> str:
    defaults = (
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + ('<Default Extension="png" ContentType="image/png"/>' if png else "")
    )
    body = "".join(
        f"<Override PartName={quoteattr('/' + part)} ContentType={quoteattr(kind)}/>"
        for part, kind in overrides.items()
    )
    return f'{_DECL}<Types xmlns="{_CT}">{defaults}{body}</Types>'


def _chart_part() -> str:
    return (
        f'{_DECL}<c:chartSpace xmlns:c="{_CHART_NS}" xmlns:a="{_DRAW_NS}">'
        '<c:chart><c:plotArea><c:layout/><c:barChart><c:barDir val="col"/>'
        '<c:grouping val="clustered"/><c:axId val="1"/><c:axId val="2"/></c:barChart>'
        "</c:plotArea></c:chart></c:chartSpace>"
    )


def _app_properties(name: str, count: int | None) -> str:
    inner = f"<{name}>{count}</{name}>" if count is not None else ""
    return f'{_DECL}<Properties xmlns="{_APP_NS}">{inner}</Properties>'


def _zip(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8") if isinstance(content, str) else content)
    return buffer.getvalue()


def xlsx_bytes(
    sheets: dict[str, list[list[object]]],
    *,
    shared_strings: bool = True,
    hyperlinks: dict[str, list[tuple[str, str]]] | None = None,
    chart: bool = False,
) -> bytes:
    """A workbook. Cells are str, int, float, Formula or None; hyperlinks are (cell, url)."""
    hyperlinks = hyperlinks or {}
    strings: list[str] = []
    string_index: dict[str, int] = {}
    parts: dict[str, str | bytes] = {}
    overrides = {
        "xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "xl/styles.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
        "docProps/app.xml": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    }
    workbook_rels = [("rIdStyles", _DOC_REL + "/styles", "styles.xml", False)]
    sheet_entries = []
    for number, (name, rows) in enumerate(sheets.items(), start=1):
        xml_rows = []
        for row_number, row in enumerate(rows, start=1):
            cells = []
            for column, value in enumerate(row):
                ref = f"{_column(column)}{row_number}"
                if value is None:
                    continue
                if isinstance(value, Formula):
                    cached = f"<v>{value.cached!r}</v>" if value.cached is not None else ""
                    cells.append(f'<c r="{ref}"><f>{escape(value.text)}</f>{cached}</c>')
                elif isinstance(value, bool):
                    cells.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
                elif isinstance(value, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{value!r}</v></c>')
                elif shared_strings:
                    if value not in string_index:
                        string_index[value] = len(strings)
                        strings.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{string_index[value]}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                        f"{escape(str(value))}</t></is></c>"
                    )
            xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        links = hyperlinks.get(name, [])
        link_xml = ""
        sheet_rels = []
        if links:
            items = []
            for index, (ref, url) in enumerate(links, start=1):
                items.append(f'<hyperlink ref="{ref}" r:id="rIdLink{index}"/>')
                sheet_rels.append((f"rIdLink{index}", _HYPERLINK, url, True))
            link_xml = f"<hyperlinks>{''.join(items)}</hyperlinks>"
        parts[f"xl/worksheets/sheet{number}.xml"] = (
            f'{_DECL}<worksheet xmlns="{_SHEET_NS}" xmlns:r="{_DOC_REL}">'
            f"<sheetData>{''.join(xml_rows)}</sheetData>{link_xml}</worksheet>"
        )
        if sheet_rels:
            parts[f"xl/worksheets/_rels/sheet{number}.xml.rels"] = _rels(sheet_rels)
        overrides[f"xl/worksheets/sheet{number}.xml"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
        )
        workbook_rels.append(
            (f"rId{number}", _DOC_REL + "/worksheet", f"worksheets/sheet{number}.xml", False)
        )
        sheet_entries.append(
            f'<sheet name={quoteattr(name)} sheetId="{number}" r:id="rId{number}"/>'
        )
    if shared_strings and strings:
        items = "".join(f'<si><t xml:space="preserve">{escape(text)}</t></si>' for text in strings)
        parts["xl/sharedStrings.xml"] = (
            f'{_DECL}<sst xmlns="{_SHEET_NS}" count="{len(strings)}" '
            f'uniqueCount="{len(strings)}">{items}</sst>'
        )
        overrides["xl/sharedStrings.xml"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
        )
        workbook_rels.append(
            ("rIdStrings", _DOC_REL + "/sharedStrings", "sharedStrings.xml", False)
        )
    if chart:
        parts["xl/charts/chart1.xml"] = _chart_part()
        overrides["xl/charts/chart1.xml"] = (
            "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
        )
    parts["xl/workbook.xml"] = (
        f'{_DECL}<workbook xmlns="{_SHEET_NS}" xmlns:r="{_DOC_REL}">'
        f"<sheets>{''.join(sheet_entries)}</sheets></workbook>"
    )
    parts["xl/styles.xml"] = (
        f'{_DECL}<styleSheet xmlns="{_SHEET_NS}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        "</styleSheet>"
    )
    parts["xl/_rels/workbook.xml.rels"] = _rels(workbook_rels)
    parts["docProps/app.xml"] = _app_properties("Application", None)
    parts["_rels/.rels"] = _rels(
        [
            ("rId1", _OFFICE_DOC, "xl/workbook.xml", False),
            ("rId2", _DOC_REL + "/extended-properties", "docProps/app.xml", False),
        ]
    )
    parts["[Content_Types].xml"] = _content_types(overrides)
    return _zip({"[Content_Types].xml": parts.pop("[Content_Types].xml"), **parts})


def docx_bytes(
    paragraphs: list[str],
    *,
    tables: list[list[list[str]]] | None = None,
    hyperlinks: list[tuple[str, str]] | None = None,
    pages: int | None = None,
    image: bool = False,
    chart: bool = False,
) -> bytes:
    """A Word document: paragraphs, then tables, then hyperlinked paragraphs (text, url)."""
    body = []
    rels = []
    for text in paragraphs:
        body.append(f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')
    for table in tables or []:
        rows = "".join(
            "<w:tr>"
            + "".join(
                f'<w:tc><w:p><w:r><w:t xml:space="preserve">{escape(cell)}</w:t></w:r></w:p></w:tc>'
                for cell in row
            )
            + "</w:tr>"
            for row in table
        )
        body.append(f"<w:tbl>{rows}</w:tbl>")
    for index, (text, url) in enumerate(hyperlinks or [], start=1):
        rels.append((f"rIdLink{index}", _HYPERLINK, url, True))
        body.append(
            f'<w:p><w:hyperlink r:id="rIdLink{index}"><w:r><w:t>{escape(text)}</w:t></w:r>'
            "</w:hyperlink></w:p>"
        )
    parts: dict[str, str | bytes] = {
        "word/document.xml": (
            f'{_DECL}<w:document xmlns:w="{_WORD_NS}" xmlns:r="{_DOC_REL}">'
            f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
        ),
        "docProps/app.xml": _app_properties("Pages", pages),
        "_rels/.rels": _rels(
            [
                ("rId1", _OFFICE_DOC, "word/document.xml", False),
                ("rId2", _DOC_REL + "/extended-properties", "docProps/app.xml", False),
            ]
        ),
    }
    overrides = {
        "word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "docProps/app.xml": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    }
    if image:
        parts["word/media/image1.png"] = PNG_PIXEL
        rels.append(("rIdImage1", _DOC_REL + "/image", "media/image1.png", False))
    if chart:
        parts["word/charts/chart1.xml"] = _chart_part()
        overrides["word/charts/chart1.xml"] = (
            "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
        )
        rels.append(("rIdChart1", _DOC_REL + "/chart", "charts/chart1.xml", False))
    if rels:
        parts["word/_rels/document.xml.rels"] = _rels(rels)
    return _zip({"[Content_Types].xml": _content_types(overrides, png=image), **parts})


def pptx_bytes(
    slides: list[list[str]],
    *,
    hyperlinks: dict[int, list[str]] | None = None,
    missing_slide_parts: int = 0,
    chart: bool = False,
) -> bytes:
    """A presentation, one list of text lines per slide; hyperlinks by slide index (0-based).

    ``missing_slide_parts`` lists that many extra slides whose parts are absent, as a broken
    writer would leave them.
    """
    hyperlinks = hyperlinks or {}
    parts: dict[str, str | bytes] = {}
    overrides = {
        "ppt/presentation.xml": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    }
    presentation_rels = []
    slide_ids = []
    for index, lines in enumerate(slides):
        number = index + 1
        text = "".join(
            f'<a:p><a:r><a:t xml:space="preserve">{escape(line)}</a:t></a:r></a:p>'
            for line in lines
        )
        parts[f"ppt/slides/slide{number}.xml"] = (
            f'{_DECL}<p:sld xmlns:p="{_PRES_NS}" xmlns:a="{_DRAW_NS}" xmlns:r="{_DOC_REL}">'
            f"<p:cSld><p:spTree><p:sp><p:txBody><a:bodyPr/>{text}</p:txBody></p:sp>"
            "</p:spTree></p:cSld></p:sld>"
        )
        overrides[f"ppt/slides/slide{number}.xml"] = (
            "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
        )
        links = hyperlinks.get(index, [])
        if links:
            parts[f"ppt/slides/_rels/slide{number}.xml.rels"] = _rels(
                [(f"rIdLink{i}", _HYPERLINK, url, True) for i, url in enumerate(links, start=1)]
            )
        presentation_rels.append(
            (f"rId{number}", _DOC_REL + "/slide", f"slides/slide{number}.xml", False)
        )
        slide_ids.append(f'<p:sldId id="{255 + number}" r:id="rId{number}"/>')
    for extra in range(missing_slide_parts):
        number = len(slides) + extra + 1
        presentation_rels.append(
            (f"rId{number}", _DOC_REL + "/slide", f"slides/slide{number}.xml", False)
        )
        slide_ids.append(f'<p:sldId id="{255 + number}" r:id="rId{number}"/>')
    if chart:
        parts["ppt/charts/chart1.xml"] = _chart_part()
        overrides["ppt/charts/chart1.xml"] = (
            "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
        )
    parts["ppt/presentation.xml"] = (
        f'{_DECL}<p:presentation xmlns:p="{_PRES_NS}" xmlns:r="{_DOC_REL}">'
        f"<p:sldIdLst>{''.join(slide_ids)}</p:sldIdLst></p:presentation>"
    )
    parts["ppt/_rels/presentation.xml.rels"] = _rels(presentation_rels)
    parts["_rels/.rels"] = _rels([("rId1", _OFFICE_DOC, "ppt/presentation.xml", False)])
    return _zip({"[Content_Types].xml": _content_types(overrides), **parts})


def pdf_bytes(
    pages: list[str], *, object_stream: bool = False, links: list[str] | None = None
) -> bytes:
    """A PDF with one line of text per page.

    ``object_stream`` packs the page objects into a compressed object stream, the way PDF 1.5
    writers store them, to exercise readers. Its cross-reference table then lists only the
    uncompressed objects.
    """
    objects: dict[int, bytes] = {}
    page_numbers = list(range(4, 4 + 2 * len(pages), 2))
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    annotations = ""
    if links:
        link_numbers = list(range(4 + 2 * len(pages), 4 + 2 * len(pages) + len(links)))
        for number, url in zip(link_numbers, links, strict=True):
            objects[number] = (
                f"<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /A << /S /URI /URI ({url}) >> >>"
            ).encode()
        annotations = " /Annots [" + " ".join(f"{n} 0 R" for n in link_numbers) + "]"
    streams: dict[int, bytes] = {}
    page_objects: dict[int, bytes] = {}
    for page_number, text in zip(page_numbers, pages, strict=True):
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        streams[page_number + 1] = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
        page_objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_number + 1} 0 R"
            f" /Resources << /Font << /F1 3 0 R >> >>"
            + (annotations if page_number == page_numbers[0] else "")
            + " >>"
        ).encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    def write(number: int, body: bytes) -> None:
        offsets[number] = out.tell()
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")

    for number, body in sorted(objects.items()):
        write(number, body)
    for number, content in sorted(streams.items()):
        write(number, f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    if object_stream:
        header = b""
        payload = b""
        for number, body in sorted(page_objects.items()):
            header += f"{number} {len(payload)} ".encode()
            payload += body + b"\n"
        packed = zlib.compress(header + payload)
        stream_number = max([*objects, *streams, *page_objects]) + 1
        write(
            stream_number,
            f"<< /Type /ObjStm /N {len(page_objects)} /First {len(header)} /Filter /FlateDecode"
            f" /Length {len(packed)} >>\nstream\n".encode()
            + packed
            + b"\nendstream",
        )
    else:
        for number, body in sorted(page_objects.items()):
            write(number, body)
    size = max(offsets) + 1
    xref = out.tell()
    out.write(f"xref\n0 {size}\n0000000000 65535 f \n".encode())
    for number in range(1, size):
        if number in offsets:
            out.write(f"{offsets[number]:010d} 00000 n \n".encode())
        else:
            out.write(b"0000000000 65535 f \n")
    out.write(f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
