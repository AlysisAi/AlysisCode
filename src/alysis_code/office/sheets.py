"""Bounded XLSX authoring and conservative, copy-only editing.

The caller validates workspace paths and atomically publishes the staged output.
No formula is evaluated here. Inspection deliberately never reads cached results.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import warnings
import zipfile
from copy import copy
from datetime import date, datetime, time, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from . import SpreadsheetAuthoringError, charts

MAX_SHEETS = 16
MAX_CELLS = 50_000
MAX_ROWS = 10_000
MAX_COLUMNS = 256
MAX_TEXT = 2 * 1024 * 1024
MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_PREVIEW_CELLS = 1_000
MAX_PREVIEW_TEXT = 64 * 1024
MAX_CHANGE_EXAMPLES = 12
_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,4})\Z")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_BAD_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]|[\ud800-\udfff]")
_FORMAT_KEYS = {
    "bold",
    "italic",
    "font_color",
    "font_size",
    "fill",
    "number_format",
    "align",
    "wrap",
}
_PARTS = re.compile(
    r"(?:\[Content_Types\]\.xml|_rels/\.rels|docProps/(?:app|core)\.xml|"
    r"xl/(?:workbook|styles|sharedStrings)\.xml|xl/theme/theme[0-9]+\.xml|"
    r"xl/worksheets/sheet[0-9]+\.xml|xl/tables/table[0-9]+\.xml|"
    r"xl/_rels/workbook\.xml\.rels|xl/worksheets/_rels/sheet[0-9]+\.xml\.rels)\Z"
)
_UNSUPPORTED_ELEMENTS = {
    "extLst",
    "AlternateContent",
    "oleObjects",
    "controls",
    "legacyDrawing",
    "drawing",
    "customSheetViews",
    "customWorkbookViews",
    "pivotTableParts",
    "externalReferences",
    "dataValidations",
    "conditionalFormatting",
    "sheetProtection",
    "workbookProtection",
    "webPublishItems",
    "smartTags",
    "cellSmartTags",
    "scenarios",
    "richValue",
    "ignoredErrors",
    "phoneticPr",
    "rPh",
    "calcChain",
    "webPublishing",
    "fileSharing",
}
_ROOT_CHILDREN = {
    "workbook": {"fileVersion", "workbookPr", "bookViews", "sheets", "definedNames", "calcPr"},
    "worksheet": {
        "sheetPr",
        "dimension",
        "sheetViews",
        "sheetFormatPr",
        "cols",
        "sheetData",
        "sheetCalcPr",
        "autoFilter",
        "mergeCells",
        "hyperlinks",
        "printOptions",
        "pageMargins",
        "pageSetup",
        "headerFooter",
        "tableParts",
    },
    "table": {"autoFilter", "sortState", "tableColumns", "tableStyleInfo"},
}

GUIDE = {
    "create": "sheets: list of {name, rows, cells?, header_row?, column_widths?, row_heights?, merge_ranges?, freeze_panes?, tables?, charts?, page_setup?}",
    "cell": "JSON scalar is literal; {value: scalar, format?: {...}} or {formula: '=SUM(B2:B3)', format?: {...}}",
    "format": "bold/italic/wrap booleans; font_size 6..72 points; font_color/fill #RRGGBB; number_format text; align left/center/right. On creation, a header with a custom fill gets contrasting text unless font_color is set. Long bold headings and table headers wrap within their available columns unless wrap is explicitly false.",
    "layout": "Optional merge_ranges: ['A1:D1'] spans a heading, note or formula across adjacent empty cells on one row, outside tables. Put its content and format in the leftmost cell. row_heights: {'1': 36} sets row height in points (1..409). For a meeting or decision, lead the first visible sheet with the most important supported finding in the reader's language, then its evidence and relevant exception or next action. Give that finding visual prominence; keep working data in a sortable grid.",
    "table": "{range: 'A1:B3', name: 'Revenue', style?: 'Table Style Medium 2'}; first row must be distinct literal text headers",
    "page_setup": "{orientation?: 'portrait'|'landscape', fit_to_pages?: [width, height], print_area?: 'A1:J20'}; 0 means unlimited pages on that axis, 1..10 limits pages. Wrap long notes and inspect rendered pages; scaling alone does not prove legibility.",
    "chart": {
        "shape": "A sheet's charts: [chart, ...]; each chart may add anchor: 'H2' (its top-left "
        "cell; by default charts sit beside the sheet's data). "
        + charts.SHAPE
        + " In a workbook, categories and every series' values may instead be cell ranges such "
        "as 'A2:A9' or \"'Other sheet'!B2:B9\" (one row or column; value cells hold numbers or "
        "formulas). Listed values are written beside the sheet's data and the chart refers to "
        "those cells. Unless page_setup sets fit_to_pages or print_area, a sheet with charts "
        "prints landscape (or its orientation), scaled to the page width, and no page cuts a "
        "chart.",
        "example": {
            "name": "Αιτήματα",
            "rows": [["Εβδομάδα", "Δίκτυο", "Email"], ["Εβδ. 1", 12, 5], ["Εβδ. 2", 9, 7]],
            "charts": [
                {
                    "chart": "line",
                    "title": "Αιτήματα ανά εβδομάδα",
                    "categories": "A2:A3",
                    "series": [
                        {"name": "Δίκτυο", "values": "B2:B3"},
                        {"name": "Email", "values": "C2:C3"},
                    ],
                }
            ],
        },
    },
    "edit": "{expected_source_sha256: inspect.source_sha256, changes: [{sheet, cell: 'B2', value OR formula, format?}]}. To change only formatting, supply format without value or formula. To lay out a note, include a separate change such as {sheet, merge_ranges: ['A5:D5'], row_heights: {'5': 34}} after setting the note's anchor. Its optional extend_print_area: 'A1:D5' expands an existing print area to encompass the note. Set the height needed for a wrapped merged note. When print_coverage identifies omitted rows that belong in the report, use {sheet, extend_printed_table: 'TableName'} as a change to include them in the table, filter and print area.",
    "revision": "When the reader asks what changed, put the source identifier and previous and new values beside the affected result on the first relevant sheet. Include the previous and new result and the difference in its own unit where meaningful. Use the edit receipt's revision_evidence to check saved values and recalculated effects. Never infer a previous value from a missing cache. Keep identifiers, units, formulas and unrelated rows intact.",
    "limits": {
        "sheets": MAX_SHEETS,
        "cells_and_bounding_area": MAX_CELLS,
        "rows": MAX_ROWS,
        "columns": MAX_COLUMNS,
        "text_bytes": MAX_TEXT,
        "inspection_preview_cells": MAX_PREVIEW_CELLS,
    },
    "formula_functions": "Function names are preserved; the installed calculation engine determines support. Writing a formula does not prove it calculates.",
    "opening_view": "When the workbook supports a decision, lead with the current recommendation, its criterion and the supporting numbers together on the first screen. After a revision, put the changed priority first in the comparison or label its ordering. Fit the important chart labels and conclusions within the opening viewport and first printed page.",
    "working_calculations": "For an editable report, identify each authoritative input in the original records. Connect the reported metrics directly to those cells, or use native row formulas for every intermediate flag, bucket, eligibility decision or conversion. A copied or manually classified helper severs that connection even when today's counts are correct. When revising a source record, change the authoritative input once and confirm each dependent result updates without separately editing its helpers. Keep source identifiers literal. If a takeaway states a changing rank or number, link it to the live results or clearly date it as a snapshot.",
    "decision_example": {
        "use": "When a reader must choose an option, show its current name in a prominent formula cell. On Review, A5:A7 holds eligible option names and B5:B7 their calculated numeric metrics. Place a label such as 'Current choice' above the formula in A2. This selects the lowest metric. For the highest, replace each MIN with MAX. Localize the tie and no-data messages. Keep a dated correction note separate from the current choice.",
        "cells": {
            "A2": {
                "formula": '=IF(COUNT(B5:B7)=0,"No eligible option",IF(COUNTIF(B5:B7,MIN(B5:B7))>1,"Multiple options tied",INDEX(A5:A7,MATCH(MIN(B5:B7),B5:B7,0))))',
            }
        },
    },
    "input_check": "Verify a representative authoritative input in a private copy when the report is meant to update. Include top-level input_check in the create/edit spec (or pass it to check_document or an inspect spec). Shape: {input:{sheet:'Inputs',cell:'B3',value:8},expect:[{sheet:'Summary',cell:'C2',before:15,after:20}]}. Supply a different literal input and the expected baseline and new values for 1..8 dependent formula cells. For a decision, choose a change that alters the recommendation and include its visible text cell alongside the supporting number. Derive expectations independently from the source. Numeric values are stored units (e.g. 0.5 for 50%). The calculator tests this case and preserves the delivered inputs; failed create/edit checks withhold the candidate. If unavailable, report the untested dependency.",
    "delivery_workflow": "Create, recalculate, check and visually review working copies outside final deliverables. Repair layout and formulas there, then use fs_copy to copy the reviewed workbook to the requested new final filename and check that exact delivered file. Every operation requires a new output path. Reserve the final name until review is complete; do not delete a draft to replace it. Keep specifications and drafts separate from delivered workbooks.",
    "limitations": [
        "Create and edit recalculate formulas in the delivered workbook when LibreOffice is configured. See calculation.state. Inspecting formulas alone does not verify cached results. No macros or external workbook references.",
        "Formula references use local A1 ranges and English functions; named/structured/array formulas are unsupported.",
        "Edit only simple XLSX workbooks. Unsupported parts, charts, rich text, protected sheets, validation and conditional formatting are rejected; create a workbook with charts again to change its data.",
        "Edits change existing sheet cells; no row/column insertion, renaming or formula-reference rewriting.",
        "Inspection returns bounded cell text previews, complete counts and an editability report; caches are not returned.",
    ],
}


def example_spec() -> dict:
    return {
        "sheets": [
            {
                "name": "Έσοδα",
                "header_row": True,
                "column_widths": [28, 18],
                "freeze_panes": "A2",
                "rows": [
                    ["Πελάτης", "Ποσό"],
                    ["Αθήνα", 120],
                    ["Θεσσαλονίκη", 80],
                    [
                        "Σύνολο",
                        {
                            "formula": "=SUM(B2:B3)",
                            "format": {"number_format": '#,##0.00 "€"', "bold": True},
                        },
                    ],
                ],
                "tables": [{"range": "A1:B3", "name": "Revenue"}],
            }
        ]
    }


def _object(value, allowed: set[str], where: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{where} must be an object")
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"Unsupported {where} keys: {', '.join(sorted(extra))}")
    return value


def _text(value, where: str, limit: int = 32767) -> str:
    if not isinstance(value, str) or len(value) > limit or _BAD_TEXT.search(value):
        raise ValueError(f"{where} must be valid text of at most {limit} characters")
    return value


def _position(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not (match := _CELL.fullmatch(value)):
        raise ValueError("Cell references must use uppercase A1 notation")
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    row = int(match[2])
    if row > MAX_ROWS or column > MAX_COLUMNS:
        raise ValueError(f"Cell exceeds {MAX_ROWS} rows / {MAX_COLUMNS} columns")
    return row, column


def _range(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or len(value.split(":")) != 2:
        raise ValueError("Table range must use A1:B3 notation")
    first, last = value.split(":")
    r1, c1 = _position(first)
    r2, c2 = _position(last)
    if r2 <= r1 or c2 < c1 or (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_CELLS:
        raise ValueError("Table requires a header, data rows and bounded increasing range")
    return r1, c1, r2, c2


def _print_range(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or len(value.split(":")) != 2:
        raise ValueError("Print area must use A1:B3 notation")
    first, last = value.split(":")
    r1, c1 = _position(first)
    r2, c2 = _position(last)
    if r2 < r1 or c2 < c1 or (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_CELLS:
        raise ValueError("Print area must be a bounded increasing range")
    return r1, c1, r2, c2


def _is_local_print_area(element, workbook) -> bool:
    # Print areas are workbook-defined names, but are layout metadata, not
    # named formula operands. Admit only a bounded range on its owning sheet.
    if element.get("name") != "_xlnm.Print_Area" or set(element.attrib) != {"name", "localSheetId"}:
        return False
    try:
        index = int(element.get("localSheetId", ""))
        names = [sheet.get("name") for sheet in workbook.findall("{*}sheets/{*}sheet")]
        sheet, reference = (element.text or "").rsplit("!", 1)
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        if not 0 <= index < len(names) or sheet != names[index]:
            return False
        if not re.fullmatch(r"\$?[A-Z]+\$?[1-9][0-9]*:\$?[A-Z]+\$?[1-9][0-9]*", reference):
            return False
        _print_range(reference.replace("$", ""))
        return True
    except (ValueError, TypeError):
        return False


def _formula(value: str) -> str:
    from openpyxl.formula import Tokenizer

    _text(value, "Formula", 8192)
    if not value.startswith("=") or value == "=":
        raise ValueError("Explicit formula must begin with = and contain an expression")
    # Validate local references structurally. Function support belongs to the
    # calculation engine, not an incomplete hand-maintained function whitelist.
    try:
        tokens = Tokenizer(value).items
    except Exception as exc:
        raise ValueError("Invalid formula syntax") from exc
    for token in tokens:
        if token.type == "ARRAY" or token.subtype == "ROW":
            raise ValueError("Array formulas are unsupported")
        if token.type == "OPERAND" and token.subtype == "RANGE":
            ref = token.value
            if any(char in ref for char in "[]|\\"):
                raise ValueError(
                    "External, named and structured formula references are unsupported"
                )
            if "!" in ref:
                sheet, ref = ref.rsplit("!", 1)
                if sheet.startswith("'") and sheet.endswith("'"):
                    sheet = sheet[1:-1].replace("''", "'")
                _sheet_name(sheet)
            for cell in ref.split(":"):
                _position(cell.replace("$", "").upper())
            if len(ref.split(":")) > 2:
                raise ValueError("Unsupported formula range")
        if token.type == "OPERAND" and token.subtype == "ERROR":
            raise ValueError("Formula contains an Excel error reference")
    return value


def _format(value) -> dict:
    value = _object(value, _FORMAT_KEYS, "cell format")
    for key, item in value.items():
        if key in {"bold", "italic", "wrap"} and not isinstance(item, bool):
            raise ValueError(f"{key} must be boolean")
        if key in {"font_color", "fill"} and (
            not isinstance(item, str) or not _COLOR.fullmatch(item)
        ):
            raise ValueError(f"{key} must be a #RRGGBB color")
        if key == "number_format":
            _text(item, "number_format", 128)
        if key == "font_size" and (type(item) not in (int, float) or not 6 <= item <= 72):
            raise SpreadsheetAuthoringError(
                "spreadsheet_font_size_invalid", "font_size must be between 6 and 72 points"
            )
        if key == "align" and (
            not isinstance(item, str) or item not in {"left", "center", "right"}
        ):
            raise ValueError("align must be left, center or right")
    return value


def _cell(value) -> tuple[object, bool, dict]:
    style = {}
    if isinstance(value, dict):
        _object(value, {"value", "formula", "format"}, "cell")
        style = _format(value.get("format", {}))
        if ("value" in value) == ("formula" in value):
            raise ValueError("Cell object requires exactly one of value or formula")
        if "formula" in value:
            return _formula(value["formula"]), True, style
        value = value["value"]
    if isinstance(value, str):
        _text(value, "Cell value")
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise ValueError("Cell value must be a JSON string, finite number, boolean or null")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("Cell value must be a finite number")
        if isinstance(value, int) and abs(value) > 999_999_999_999_999:
            raise ValueError("Integers longer than 15 digits must be supplied as literal strings")
    return value, False, style


def _sheet_name(value: str) -> str:
    _text(value, "Sheet name", 31)
    if (
        not value
        or re.search(r"[\[\]:*?/\\]", value)
        or value.startswith("'")
        or value.endswith("'")
    ):
        raise ValueError("Invalid sheet name")
    if value.casefold() == "history":
        raise ValueError("History is a reserved sheet name")
    return value


def _bounded(cells: dict) -> tuple[int, int]:
    max_row = max((r for r, _ in cells), default=0)
    max_col = max((c for _, c in cells), default=0)
    if len(cells) > MAX_CELLS or max_row * max_col > MAX_CELLS:
        raise ValueError(f"Sheet bounding area exceeds {MAX_CELLS} cells")
    return max_row, max_col


def _heading_ranges(ranges) -> dict:
    if not isinstance(ranges, list) or len(ranges) > 128:
        raise ValueError("merge_ranges must be a list of at most 128 horizontal ranges")
    merges = {}
    for ref in ranges:
        row, first, last_row, last = _print_range(ref)
        if row != last_row or first >= last:
            raise ValueError("Each merge must span adjacent columns on one row")
        if any(r == row and c <= last and end >= first for (r, c), end in merges.items()):
            raise ValueError("A merge cannot overlap another merge")
        merges[row, first] = last
    return merges


def _row_heights(heights, max_row) -> dict:
    if not isinstance(heights, dict) or any(
        not isinstance(row, str)
        or not re.fullmatch(r"[1-9][0-9]{0,4}", row)
        or int(row) > max_row
        or type(height) not in (int, float)
        or not 1 <= height <= 409
        for row, height in heights.items()
    ):
        raise ValueError("row_heights maps existing 1-based rows to heights of 1..409 points")
    return {int(row): height for row, height in heights.items()}


def _create_spec(spec: dict) -> list[dict]:
    _object(spec, {"sheets"}, "workbook")
    sheets = spec.get("sheets")
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= MAX_SHEETS:
        raise ValueError(f"sheets must contain between 1 and {MAX_SHEETS} worksheets")
    result, names, table_names = [], set(), set()
    total_area = total_text = chart_count = 0
    for sheet in sheets:
        _object(
            sheet,
            {
                "name",
                "rows",
                "cells",
                "header_row",
                "column_widths",
                "row_heights",
                "merge_ranges",
                "freeze_panes",
                "tables",
                "page_setup",
                "charts",
            },
            "sheet",
        )
        name = _sheet_name(sheet.get("name"))
        if name.casefold() in names:
            raise ValueError("Sheet names must be unique ignoring case")
        names.add(name.casefold())
        cells = {}
        rows = sheet.get("rows", [])
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError(f"rows must be a list of at most {MAX_ROWS} rows")
        for r, row in enumerate(rows, 1):
            if not isinstance(row, list) or len(row) > MAX_COLUMNS:
                raise ValueError(f"Each row must contain at most {MAX_COLUMNS} cells")
            for c, value in enumerate(row, 1):
                cells[r, c] = _cell(value)
        extra = sheet.get("cells", {})
        if not isinstance(extra, dict) or len(extra) > MAX_CELLS:
            raise ValueError("cells must be a bounded A1-keyed object")
        for ref, value in extra.items():
            pos = _position(ref)
            if pos in cells:
                raise ValueError("cells must not overlap rows; specify each cell once")
            cells[pos] = _cell(value)
        widths = sheet.get("column_widths", [])
        if (
            not isinstance(widths, list)
            or len(widths) > MAX_COLUMNS
            or any(
                isinstance(w, bool) or not isinstance(w, (int, float)) or not 1 <= w <= 100
                for w in widths
            )
        ):
            raise ValueError("column_widths must be a list of widths from 1 to 100")
        area = _bounded(cells)
        merges = _heading_ranges(sheet.get("merge_ranges", []))
        for (r1, c1), c2 in merges.items():
            if cells.get((r1, c1), (None,))[0] is None:
                raise ValueError("Each merge requires content in its leftmost cell")
            for column in range(c1 + 1, c2 + 1):
                pos = (r1, column)
                if cells.pop(pos, (None, False, {})) != (None, False, {}):
                    raise SpreadsheetAuthoringError(
                        "spreadsheet_merge_occupied", "A merge cannot cover a value or styled cell"
                    )
            area = (area[0], max(area[1], c2))
        heights = _row_heights(sheet.get("row_heights", {}), area[0])
        raw_charts = sheet.get("charts", [])
        if not isinstance(raw_charts, list):
            raise ValueError("charts must be a list of chart objects")
        sheet_charts, chart_widths = [], {}
        # Listed chart values go beside the sheet's cells, one blank column apart.
        next_column = area[1] + 2 if area[1] else 1
        for number, item in enumerate(raw_charts, 1):
            chart_count += 1
            if chart_count > charts.MAX_CHARTS:
                raise ValueError(f"A workbook can hold at most {charts.MAX_CHARTS} charts")
            where = f"Sheet {name!r} chart {number}"
            if not isinstance(item, dict):
                raise ValueError(f"{where} must be an object")
            chart = charts.parse(
                {key: value for key, value in item.items() if key != "anchor"}, where, ranges=True
            )
            anchor = _position(item["anchor"]) if "anchor" in item else None
            data = None
            if chart.inline:
                last_row = 1 + len(chart.categories)
                last_column = next_column + len(chart.series)
                if last_column > MAX_COLUMNS:
                    raise ValueError(
                        f"{where}: its values do not fit beside the sheet's cells; put them in "
                        "the sheet and give cell ranges"
                    )
                data = (1, next_column)
                if next_column > len(widths):
                    longest = max(
                        len(str(item)) for item in (chart.x_title or "", *chart.categories)
                    )
                    chart_widths[next_column] = min(40, max(10, longest + 2))
                next_column = last_column + 2
                area = (max(area[0], last_row), max(area[1], last_column))
                total_text += sum(
                    len(text.encode("utf-8"))
                    for text in (
                        chart.x_title or "",
                        *(item.name for item in chart.series),
                        *(c for c in chart.categories if isinstance(c, str)),
                    )
                )
            sheet_charts.append({"chart": chart, "anchor": anchor, "data": data, "where": where})
        if area[0] * area[1] > MAX_CELLS:
            raise ValueError(f"Sheet bounding area exceeds {MAX_CELLS} cells")
        total_area += area[0] * area[1]
        total_text += sum(
            len(v.encode("utf-8")) for v, _, _ in cells.values() if isinstance(v, str)
        )
        if total_area > MAX_CELLS or total_text > MAX_TEXT:
            raise ValueError("Workbook exceeds total cell/text limits")
        header = sheet.get("header_row", False)
        if not isinstance(header, bool):
            raise ValueError("header_row must be boolean")
        freeze = _position(sheet["freeze_panes"]) if "freeze_panes" in sheet else None
        page_setup = _object(
            sheet.get("page_setup", {}), {"orientation", "fit_to_pages", "print_area"}, "page_setup"
        )
        if "orientation" in page_setup and (
            not isinstance(page_setup["orientation"], str)
            or page_setup["orientation"] not in {"portrait", "landscape"}
        ):
            raise ValueError("page_setup.orientation must be portrait or landscape")
        fit = page_setup.get("fit_to_pages")
        if "fit_to_pages" in page_setup and (
            not isinstance(fit, list)
            or len(fit) != 2
            or any(type(value) is not int or not 0 <= value <= 10 for value in fit)
        ):
            raise ValueError("page_setup.fit_to_pages must be [width, height] with integers 0..10")
        print_bounds = (
            _print_range(page_setup["print_area"]) if "print_area" in page_setup else None
        )
        if print_bounds and (print_bounds[2] > area[0] or print_bounds[3] > area[1]):
            raise ValueError("page_setup.print_area must fit existing cells")
        tables = sheet.get("tables", [])
        if not isinstance(tables, list) or len(tables) > 32:
            raise ValueError("tables must be a list of at most 32 tables")
        normalized_tables = []
        for table in tables:
            _object(table, {"range", "name", "style"}, "table")
            bounds = _range(table.get("range"))
            r1, c1, r2, c2 = bounds
            if any(
                r1 <= row <= r2 and start <= c2 and end >= c1
                for (row, start), end in merges.items()
            ):
                raise ValueError("A merge cannot overlap a table")
            if r2 > area[0] or c2 > area[1]:
                raise ValueError("Table range must fit existing cells")
            tname = table.get("name")
            if (
                not isinstance(tname, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", tname)
                or re.fullmatch(r"[A-Za-z]+[0-9]+|[RrCc]", tname)
            ):
                raise ValueError("Table name must be a non-cell ASCII identifier")
            if tname.casefold() in table_names:
                raise ValueError("Table names must be unique ignoring case")
            table_names.add(tname.casefold())
            style = table.get("style", "Table Style Medium 2")
            if not isinstance(style, str) or not re.fullmatch(
                r"Table Style (Light (?:[1-9]|1[0-9]|2[01])|Medium (?:[1-9]|1[0-9]|2[0-8])|Dark (?:[1-9]|1[01]))",
                style,
            ):
                raise ValueError("Unsupported table style")
            headers = [cells.get((r1, c), (None, False, {})) for c in range(c1, c2 + 1)]
            if any(not isinstance(v, str) or not v or f or len(v) > 255 for v, f, _ in headers):
                raise ValueError(
                    "Table headers must be nonempty literal text, at most 255 characters"
                )
            if len({v.casefold() for v, _, _ in headers}) != len(headers):
                raise ValueError("Table headers must be unique ignoring case")
            for other in normalized_tables:
                a, b, c, d = other["bounds"]
                if not (r2 < a or r1 > c or c2 < b or c1 > d):
                    raise ValueError("Table ranges must not overlap")
            normalized_tables.append(
                {
                    "bounds": bounds,
                    "name": tname,
                    "style": style,
                    "headers": [v for v, _, _ in headers],
                }
            )
        # Resolve explicit formula sheet references before creating an output in a later pass.
        result.append(
            {
                "name": name,
                "cells": cells,
                "widths": widths,
                "heights": heights,
                "merges": merges,
                "freeze": freeze,
                "header": header,
                "tables": normalized_tables,
                "page_setup": page_setup,
                "charts": sheet_charts,
                "chart_column": next_column,
                "chart_widths": chart_widths,
            }
        )
    for sheet in result:
        for value, formula, _ in sheet["cells"].values():
            if formula:
                _formula_sheets(value, names)
        _size_merged_rows(sheet)
    _chart_ranges(result)
    return result


def _size_merged_rows(sheet: dict) -> None:
    """Excel does not auto-fit wrapped merged cells; an explicit author height takes precedence."""
    from xlsxwriter.utility import cell_autofit_width

    affected = {row for row, _ in sheet["merges"]} - sheet["heights"].keys()
    if not affected:
        return
    for (row, first), (value, formula, style) in sheet["cells"].items():
        if row not in affected:
            continue
        last = sheet["merges"].get((row, first), first)
        size = style.get("font_size", 11)
        lines = 1
        if isinstance(value, str) and not formula:
            width = max(1, sum(_column_pixels(sheet, col) for col in range(first, last + 1)) - 7)
            lines = 0
            for line in value.split("\n"):
                line_count, used = 1, 0
                if style.get(
                    "wrap", last > first or style.get("bold") or (row == 1 and sheet["header"])
                ):
                    for word in line.split():
                        pixels = max(1, (cell_autofit_width(word) - 7) * size / 11)
                        if used and used + pixels > width:
                            line_count += 1
                            used = 0
                        line_count += math.ceil(pixels / width) - 1
                        used += (pixels - 1) % width + 1 + 4 * size / 11
                lines += line_count
        height = min(409, max(15, math.ceil(lines * size * 1.35 + 2)))
        sheet["heights"][row] = max(sheet["heights"].get(row, 0), height)


def _chart_range(cell_range, sheet: dict, lookup: dict, where: str, what: str):
    """A chart range's sheet and its cells, in order; one row or one column."""
    target = lookup.get((cell_range.sheet or sheet["name"]).casefold())
    if target is None:
        raise ValueError(f"{where}: {what} {cell_range.text} names a sheet the workbook lacks")
    try:
        (r1, c1), (r2, c2) = _position(cell_range.first), _position(cell_range.last)
    except ValueError as error:
        raise ValueError(f"{where}: {what} {cell_range.text}: {error}") from None
    (r1, r2), (c1, c2) = sorted((r1, r2)), sorted((c1, c2))
    if r1 != r2 and c1 != c2:
        raise ValueError(f"{where}: {what} {cell_range.text} must be a single row or column")
    cells = [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
    if len(cells) > charts.MAX_POINTS:
        raise ValueError(
            f"{where}: {what} {cell_range.text} has more than {charts.MAX_POINTS} cells"
        )
    return target, (r1, c1, r2, c2), cells


def _chart_numbers(target: dict, cells: list, where: str, what: str, *, pie: bool) -> None:
    """Cells a chart plots as numbers hold numbers or formulas (blank cells are gaps)."""
    from xlsxwriter.utility import xl_rowcol_to_cell

    for r, c in cells:
        value, formula, _ = target["cells"].get((r, c), (None, False, {}))
        if formula or value is None:
            continue
        reference = xl_rowcol_to_cell(r - 1, c - 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"{where}: {what} cell {reference} is not a number; the cells a chart plots "
                "must hold numbers or formulas"
            )
        if pie and value < 0:
            raise ValueError(
                f"{where}: a pie chart's values must not be negative ({reference} is {value})"
            )


def _chart_ranges(sheets: list[dict]) -> None:
    """Resolve each ranged chart's cells, and place every chart and size it."""
    lookup = {sheet["name"].casefold(): sheet for sheet in sheets}
    for sheet in sheets:
        for entry in sheet["charts"]:
            chart, where = entry["chart"], entry["where"]
            if chart.inline:
                points = len(chart.categories)
            else:
                target, bounds, cells = _chart_range(
                    chart.categories, sheet, lookup, where, "categories"
                )
                if chart.chart == "scatter":
                    _chart_numbers(target, cells, where, "categories", pie=False)
                entry["categories"] = (target["name"], bounds)
                points = len(cells)
                entry["values"] = []
                for item in chart.series:
                    target, bounds, cells = _chart_range(
                        item.values, sheet, lookup, where, f"series {item.name!r} values"
                    )
                    if len(cells) != points:
                        raise ValueError(
                            f"{where}: series {item.name!r} has {len(cells)} values for "
                            f"{points} categories; give ranges of the same length"
                        )
                    _chart_numbers(
                        target,
                        cells,
                        where,
                        f"series {item.name!r} value",
                        pie=chart.chart == "pie",
                    )
                    entry["values"].append((target["name"], bounds))
                if points * len(chart.series) > charts.MAX_VALUES:
                    raise ValueError(
                        f"{where}: a chart holds at most {charts.MAX_VALUES} values in all its "
                        "series"
                    )
            # 600 x 360 pixels; horizontal bars get about 24 pixels per category.
            height = 360
            if chart.chart == "bar":
                height = max(height, 100 + 24 * points)
            entry["size"] = (CHART_WIDTH, height)
        if sheet["charts"]:
            _print_layout(sheet)


# Default column width and row height in pixels, and the printable area of a page at 96
# pixels per inch: the smaller of A4 and Letter, less the default margins (0.7 inch at the
# sides, 0.75 inch at the top and bottom).
COLUMN_PIXELS = 64
ROW_PIXELS = 20
CHART_WIDTH = 600
PRINTABLE = {"landscape": (921, 650), "portrait": (659, 912)}
# A column width counts characters of the workbook font's widest digit: 7 pixels for its
# 11 point Calibri in Excel. A renderer without that font draws every column with the font it
# substitutes, whose digits can be up to 0.64 em wide (DejaVu Sans, Verdana): about 9.4 pixels.
EXCEL_DIGIT = 7
WIDEST_DIGIT = 0.64 * 11 * 96 / 72
DEFAULT_COLUMN_CHARACTERS = 8.43
# Excel's filter button occupies part of a table header on screen.
FILTER_BUTTON_PIXELS = 16


def _column_pixels(sheet: dict, column: int, digit: float = EXCEL_DIGIT) -> int:
    """A column's width in pixels, as a renderer whose font has digits `digit` pixels wide
    sizes a width given in characters (Excel's own: 7)."""
    width = sheet["chart_widths"].get(column)
    if width is None and column <= len(sheet["widths"]):
        width = sheet["widths"][column - 1]
    if width is None:
        width = DEFAULT_COLUMN_CHARACTERS
    return int(width * (digit + 5) + 0.5) if width < 1 else int(width * digit + 0.5) + 5


def _chart_edge(sheet: dict, column: int, width: int, digit: float = EXCEL_DIGIT) -> int:
    """Where the last column a chart covers ends, in pixels from the sheet's left edge, as a
    renderer whose font has digits `digit` pixels wide draws the sheet: printing breaks pages
    between whole columns, and a chart keeps the columns Excel's widths give it."""
    left = sum(_column_pixels(sheet, c) for c in range(1, column))
    edge = left
    drawn = sum(_column_pixels(sheet, c, digit) for c in range(1, column))
    while edge < left + width:
        edge += _column_pixels(sheet, column)
        drawn += _column_pixels(sheet, column, digit)
        column += 1
    return drawn


def _print_layout(sheet: dict) -> None:
    """Place the sheet's charts so that none is cut by a page when printed.

    Charts stack beside the data. Unless the sheet sets its own fit or print area, the page
    is scaled so the sheet's width fits it, and a chart that would cross a page starts the
    next one (with a page break there, which a scaled page honours).
    """
    setup = sheet["page_setup"]
    own_layout = "fit_to_pages" in setup or "print_area" in setup
    orientation = setup.get("orientation", "landscape")
    # The page is scaled for the widest columns a renderer may draw, so no font it uses in
    # place of the workbook's pushes a chart past the page's edge.
    right = _chart_edge(sheet, sheet["chart_column"], CHART_WIDTH, WIDEST_DIGIT)
    for entry in sheet["charts"]:
        if entry["anchor"] is not None:
            right = max(
                right, _chart_edge(sheet, entry["anchor"][1], entry["size"][0], WIDEST_DIGIT)
            )
    page_height = None
    if not own_layout:
        # A small reserve keeps a page's content inside what a renderer fits on it.
        width, height = (size * 0.95 for size in PRINTABLE[orientation])
        scale = max(10, min(100, int(100 * width / right)))
        page_height = int(height * 100 / scale) // ROW_PIXELS * ROW_PIXELS
        sheet["print"] = {"orientation": orientation, "scale": scale, "breaks": []}
    # XlsxWriter positions native chart anchors in worksheet pixels, including custom row heights.
    from bisect import bisect_left, bisect_right

    tops = [0]

    def extend_to(pixel):
        while tops[-1] < pixel:
            points = sheet["heights"].get(len(tops), 15)
            tops.append(tops[-1] + max(1, int(points * 4 / 3)))

    page_first = 1

    def page_end():
        bottom = tops[page_first - 1] + page_height
        extend_to(bottom)
        return bisect_right(tops, bottom) - 1

    row = 1
    for entry in sheet["charts"]:
        if page_height:
            # Never taller than a page, with a free row at either edge.
            entry["size"] = (
                entry["size"][0],
                min(entry["size"][1], page_height - 3 * ROW_PIXELS),
            )
        if entry["anchor"] is not None:
            continue
        if page_height:
            end = page_end()
            while row > end or tops[row - 1] + entry["size"][1] + ROW_PIXELS > tops[end]:
                if row == page_first:
                    break  # A tall first row can leave no whole row for additional padding.
                sheet["print"]["breaks"].append(end)
                row = page_first = end + 1
                end = page_end()
                if tops[row] + entry["size"][1] + ROW_PIXELS <= tops[end]:
                    row += 1
        entry["anchor"] = (row, sheet["chart_column"])
        bottom = tops[row - 1] + entry["size"][1] + 2 * ROW_PIXELS
        extend_to(bottom)
        row = bisect_left(tops, bottom) + 1
    if page_height:
        end = page_end()
        while page_first <= end < row - 1:
            sheet["print"]["breaks"].append(end)
            page_first = end + 1
            end = page_end()


def _formula_sheets(value: str, names: set[str]) -> None:
    from openpyxl.formula import Tokenizer

    for token in Tokenizer(value).items:
        if token.type == "OPERAND" and token.subtype == "RANGE" and "!" in token.value:
            sheet = token.value.rsplit("!", 1)[0]
            if sheet.startswith("'") and sheet.endswith("'"):
                sheet = sheet[1:-1].replace("''", "'")
            if sheet.casefold() not in names:
                raise ValueError(f"Formula refers to missing sheet: {sheet}")


def _luminance(rgb: str) -> float:
    """Relative luminance of a six-digit sRGB color."""
    channels = [int(rgb[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    red, green, blue = linear
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def create(spec: dict, output: Path, workspace: Path) -> dict:
    import xlsxwriter
    from xlsxwriter.utility import cell_autofit_width

    sheets = _create_spec(spec)
    with output.open("xb") as stream:
        with xlsxwriter.Workbook(
            stream,
            {
                "strings_to_formulas": False,
                "strings_to_urls": False,
                "strings_to_numbers": False,
                "in_memory": True,
            },
        ) as book:
            book.set_calc_mode("auto")
            for sheet in sheets:
                ws = book.add_worksheet(sheet["name"])
                page_setup = sheet["page_setup"]
                if page_setup.get("orientation") == "landscape":
                    ws.set_landscape()
                elif page_setup.get("orientation") == "portrait":
                    ws.set_portrait()
                if "fit_to_pages" in page_setup:
                    ws.fit_to_pages(*page_setup["fit_to_pages"])
                if "print_area" in page_setup:
                    ws.print_area(page_setup["print_area"])
                for index, width in enumerate(sheet["widths"]):
                    ws.set_column(index, index, width)
                for row, height in sheet["heights"].items():
                    ws.set_row(row - 1, height)
                if sheet["freeze"]:
                    row, col = sheet["freeze"]
                    ws.freeze_panes(row - 1, col - 1)
                table_headers = {}
                for table in sheet["tables"]:
                    row, first, _, last = table["bounds"]
                    for column in range(first, last + 1):
                        table_headers[row, column] = None
                for (r, c), (value, formula, style) in sheet["cells"].items():
                    style = dict(style)
                    in_table_header = (r, c) in table_headers
                    if (in_table_header or (sheet["header"] and r == 1)) and (
                        "fill" in style and "font_color" not in style
                    ):
                        # A native table may supply white text even on an explicit pale fill.
                        light = _luminance(style["fill"][1:]) + 0.05
                        style["font_color"] = (
                            "#000000" if light / 0.05 >= 1.05 / light else "#FFFFFF"
                        )
                    if sheet["header"] and r == 1 and not in_table_header:
                        style = {"bold": True, "fill": "#183153", "font_color": "#FFFFFF", **style}
                    # A title or header next to another cell or at a print boundary cannot
                    # rely on overflowing into the next column. Let the reader fit its row.
                    last = sheet["merges"].get((r, c), c)
                    width = sum(_column_pixels(sheet, col) for col in range(c, last + 1))
                    font_size = style.get("font_size", 11)
                    if (
                        isinstance(value, str)
                        and not formula
                        and (style.get("bold") or in_table_header or last > c)
                        and "wrap" not in style
                        and max(
                            (cell_autofit_width(line) for line in value.splitlines()), default=0
                        )
                        * font_size
                        / 11
                        + (FILTER_BUTTON_PIXELS if in_table_header else 0)
                        > width
                    ):
                        style["wrap"] = True
                    mapped = {
                        {
                            "fill": "bg_color",
                            "number_format": "num_format",
                            "wrap": "text_wrap",
                        }.get(k, k): v
                        for k, v in style.items()
                    }
                    fmt = book.add_format(mapped) if mapped else None
                    if last > c:
                        # Write the actual anchor below with its original literal/formula type.
                        ws.merge_range(r - 1, c - 1, r - 1, last - 1, "", fmt)
                    if in_table_header:
                        # add_table rewrites these cells. Give it the intended alignment
                        # and explicit format while letting Excel supply the table theme.
                        table_headers[r, c] = fmt
                    if formula:
                        # Empty cache avoids presenting XlsxWriter's default zero as a result.
                        ws.write_formula(r - 1, c - 1, value, fmt, "")
                    elif isinstance(value, str):
                        ws.write_string(r - 1, c - 1, value, fmt)
                    elif value is None:
                        ws.write_blank(r - 1, c - 1, None, fmt)
                    else:
                        ws.write(r - 1, c - 1, value, fmt)
                for entry in sheet["charts"]:
                    _write_chart(book, ws, sheet, entry)
                if "print" in sheet:
                    if sheet["print"]["orientation"] == "landscape":
                        ws.set_landscape()
                    ws.set_print_scale(sheet["print"]["scale"])
                    if sheet["print"]["breaks"]:
                        ws.set_h_pagebreaks(sheet["print"]["breaks"])
                for table in sheet["tables"]:
                    r1, c1, r2, c2 = table["bounds"]
                    ws.add_table(
                        r1 - 1,
                        c1 - 1,
                        r2 - 1,
                        c2 - 1,
                        {
                            "name": table["name"],
                            "style": table["style"],
                            "columns": [
                                {"header": h, "header_format": table_headers[r1, column]}
                                for column, h in enumerate(table["headers"], c1)
                            ],
                        },
                    )
    return {**inspect(output), "status": "created"}


def _literal_text(chart) -> None:
    """Keep chart text literal: XlsxWriter otherwise reads text like 'Q1!A1' as a cell."""
    resolve = chart._process_names
    chart._process_names = lambda name, formula: (
        (name, formula) if isinstance(name, str) else resolve(name, formula)
    )


def _write_chart(book, ws, sheet: dict, entry: dict) -> None:
    """A native Excel chart; listed values are written to cells first so it refers to them."""
    chart, name = entry["chart"], sheet["name"]
    number_format = (
        book.add_format({"num_format": chart.number_format}) if chart.number_format else None
    )
    if chart.inline:
        top, left = entry["data"][0] - 1, entry["data"][1] - 1
        bold = book.add_format({"bold": True})
        count = len(chart.categories)
        if chart.x_title:
            ws.write_string(top, left, chart.x_title, bold)
        for row, category in enumerate(chart.categories, top + 1):
            if isinstance(category, str):
                ws.write_string(row, left, category)
            else:
                ws.write_number(row, left, category)
        names, values = [], []
        for column, item in enumerate(chart.series, left + 1):
            ws.write_string(top, column, item.name, bold)
            for row, value in enumerate(item.values, top + 1):
                ws.write_number(row, column, value, number_format)
            names.append([name, top, column])
            values.append([name, top + 1, column, top + count, column])
        categories = [name, top + 1, left, top + count, left]
        if left + 1 in sheet["chart_widths"]:
            ws.set_column(left, left, sheet["chart_widths"][left + 1])
    else:

        def cells(reference):
            target, (r1, c1, r2, c2) = reference
            return [target, r1 - 1, c1 - 1, r2 - 1, c2 - 1]

        categories = cells(entry["categories"])
        values = [cells(reference) for reference in entry["values"]]
        names = [item.name for item in chart.series]
    plot = book.add_chart({"type": chart.chart})
    _literal_text(plot)
    for series_name, series_values in zip(names, values, strict=True):
        options = {"name": series_name, "categories": categories, "values": series_values}
        if chart.chart == "line":
            options["marker"] = {"type": "automatic"}
        elif chart.chart == "scatter":
            options["marker"] = {"type": "circle", "size": 7}
        elif chart.chart == "pie":
            options["data_labels"] = {"percentage": True}
        plot.add_series(options)
    plot.set_title({"name": chart.title} if chart.title else {"none": True})
    plot.set_legend({"position": "bottom"} if chart.legend else {"none": True})
    if chart.chart != "pie":
        category_axis = {"name": chart.x_title} if chart.x_title else {}
        value_axis = {"name": chart.y_title} if chart.y_title else {}
        if chart.number_format:
            value_axis["num_format"] = chart.number_format
        if chart.chart == "bar":
            # XlsxWriter's y axis is the vertical one: here the categories, read from the top.
            plot.set_y_axis({**category_axis, "reverse": True})
            plot.set_x_axis(value_axis)
        else:
            plot.set_x_axis(category_axis)
            plot.set_y_axis(value_axis)
    plot.set_size({"width": entry["size"][0], "height": entry["size"][1]})
    row, column = entry["anchor"]
    ws.insert_chart(row - 1, column - 1, plot, {"description": chart.title} if chart.title else {})


def _snapshot(source: Path) -> bytes:
    if source.suffix.lower() != ".xlsx":
        raise ValueError("Only .xlsx workbooks are supported; macro-enabled files are unsupported")
    if source.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("Workbook exceeds compressed size limit")
    return source.read_bytes()


def _package(data: bytes) -> list[str]:
    """Check bounds before openpyxl parses the package; report edit-fidelity blockers."""
    blockers = set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 512 or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
                raise ValueError("Workbook exceeds package expansion limit")
            if len({i.filename for i in infos}) != len(infos):
                raise ValueError("Workbook contains duplicate package parts")
            sheet_count = cell_count = total_area = total_text = 0
            merged_area = element_count = 0
            for info in infos:
                if info.flag_bits & 1:
                    raise ValueError("Encrypted workbook parts are unsupported")
                name = info.filename
                if re.fullmatch(r"xl/charts/chart[0-9]+\.xml", name):
                    blockers.add(
                        "Charts are not kept by cell edits; create the workbook again with the "
                        "changed values"
                    )
                if not _PARTS.fullmatch(name):
                    blockers.add(f"Unsupported package part: {name}")
                if not name.endswith((".xml", ".rels")):
                    continue
                raw = archive.read(info)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper() or b"\x00" in raw:
                    raise ValueError(
                        "Workbook XML must not contain DTDs, entities or alternate encodings"
                    )
                root = ET.fromstring(raw)
                if (
                    name.startswith("xl/")
                    and not name.endswith(".rels")
                    and not name.startswith("xl/theme/")
                ):
                    if any(
                        not e.tag.startswith(
                            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                        )
                        for e in root.iter()
                    ):
                        blockers.add("Unsupported spreadsheet XML namespace")
                root_tag = root.tag.rsplit("}", 1)[-1]
                if root_tag in _ROOT_CHILDREN:
                    for child in root:
                        child_tag = child.tag.rsplit("}", 1)[-1]
                        if child_tag not in _ROOT_CHILDREN[root_tag]:
                            blockers.add(f"Unsupported {root_tag} feature: {child_tag}")
                if name == "[Content_Types].xml":
                    if any(
                        "macroenabled" in str(e.attrib).lower() or "vba" in str(e.attrib).lower()
                        for e in root
                    ):
                        blockers.add("Macro-enabled content is unsupported")
                sheet = bool(re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", name))
                positions = {}
                extent_row = extent_col = 0
                if sheet:
                    sheet_count += 1
                for element in root.iter():
                    element_count += 1
                    if element_count > 500_000:
                        raise ValueError("Workbook exceeds XML element limit")
                    tag = element.tag.rsplit("}", 1)[-1]
                    if tag in _UNSUPPORTED_ELEMENTS:
                        blockers.add(f"Unsupported workbook feature: {tag}")
                    if (
                        tag == "Relationship"
                        and element.get("TargetMode", "").lower() == "external"
                    ):
                        blockers.add("External relationships are unsupported")
                    if tag == "definedName" and not _is_local_print_area(element, root):
                        blockers.add(
                            "Defined names other than local print areas are unsupported for editing"
                        )
                    if tag == "r" and name == "xl/sharedStrings.xml":
                        blockers.add("Rich cell text is unsupported for editing")
                    if tag in {"t", "f", "v"}:
                        total_text += len((element.text or "").encode("utf-8"))
                    if tag == "f":
                        if element.get("t", "normal") != "normal":
                            blockers.add("Shared, array and data-table formulas are unsupported")
                        if element.text:
                            try:
                                _formula("=" + element.text)
                            except ValueError:
                                blockers.add("Workbook contains unsupported formulas")
                    if sheet and tag == "c":
                        pos = _position(element.get("r", ""))
                        if pos in positions:
                            raise ValueError("Worksheet contains duplicate cells")
                        positions[pos] = None
                        if any(e.tag.rsplit("}", 1)[-1] == "r" for e in element.iter()):
                            blockers.add("Rich cell text is unsupported for editing")
                    if sheet and tag == "dimension":
                        for ref in element.get("ref", "A1").split(":"):
                            row, col = _position(ref)
                            extent_row, extent_col = max(row, extent_row), max(col, extent_col)
                    if tag == "mergeCell":
                        refs = element.get("ref", "").split(":")
                        if len(refs) != 2:
                            raise ValueError("Invalid merged-cell range")
                        r1, c1 = _position(refs[0])
                        r2, c2 = _position(refs[1])
                        if r2 < r1 or c2 < c1:
                            raise ValueError("Invalid merged-cell range")
                        merged_area += (r2 - r1 + 1) * (c2 - c1 + 1)
                        if merged_area > MAX_CELLS:
                            raise ValueError("Workbook exceeds merged-cell area limit")
                        extent_row, extent_col = max(r2, extent_row), max(c2, extent_col)
                    if tag == "calculatedColumnFormula" or tag == "totalsRowFormula":
                        blockers.add("Table formulas are unsupported for editing")
                if sheet:
                    max_row, max_col = _bounded(positions)
                    total_area += max(max_row, extent_row) * max(max_col, extent_col)
                    cell_count += len(positions)
            if (
                not 1 <= sheet_count <= MAX_SHEETS
                or cell_count > MAX_CELLS
                or total_area > MAX_CELLS
                or total_text > MAX_TEXT
            ):
                raise ValueError("Workbook exceeds sheet/cell/text limits")
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError) as exc:
        raise ValueError("Invalid XLSX package") from exc
    return sorted(blockers)


def _load(data: bytes):
    from openpyxl import load_workbook

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            book = load_workbook(io.BytesIO(data), data_only=False, keep_links=False)
        except Exception as exc:
            raise ValueError("Workbook cannot be safely read by openpyxl") from exc
    notes = ["openpyxl reported unsupported or lossy content"] if caught else []
    if any(
        isinstance(cell.value, float) and not math.isfinite(cell.value)
        for ws in book.worksheets
        for row in ws.iter_rows()
        for cell in row
    ):
        notes.append("Non-finite numeric values are unsupported for editing")
    return book, notes


def _json_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_number": str(value)}
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return {"duration_seconds": value.total_seconds()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"unsupported_type": type(value).__name__}


def _stored_column_pixels(ws) -> list[float]:
    # Stored OOXML widths already include the padding which set_column adds.
    default = ws.sheet_format.defaultColWidth
    widths = [default * EXCEL_DIGIT if default else 64.0] * ws.max_column
    for dim in ws.column_dimensions.values():
        if dim.min and dim.max and dim.width is not None:
            for column in range(max(1, dim.min), min(dim.max, len(widths)) + 1):
                widths[column - 1] = 0.0 if dim.hidden else dim.width * EXCEL_DIGIT
    return widths


def _heading_overflows(cell, widths, merged_ends, table_headers) -> bool:
    key = (cell.row, cell.column)
    if not (
        cell.data_type == "s"
        and isinstance(cell.value, str)
        and (key in table_headers or (cell.font.bold and cell.fill.patternType == "solid"))
        and not cell.alignment.wrap_text
        and not cell.alignment.shrink_to_fit
        and not cell.alignment.text_rotation
    ):
        return False
    from xlsxwriter.utility import cell_autofit_width

    end_column = merged_ends.get(key, cell.column)
    width = sum(widths[cell.column - 1 : end_column])
    required = max((cell_autofit_width(line) for line in cell.value.splitlines()), default=0)
    return 0 < width < required * (cell.font.sz or 11) / 11 + table_headers.get(key, 0)


def _header_contrast(cell, in_table: bool) -> dict | None:
    """Only use solid, untinted direct sRGB. Excel can supply a table's actual font color."""
    if cell.data_type != "s" or not cell.value or not (in_table or cell.font.bold):
        return None
    fill = cell.fill
    if (
        getattr(fill, "patternType", None) != "solid"
        or fill.fgColor.type != "rgb"
        or fill.fgColor.tint
    ):
        return None
    background = fill.fgColor.rgb[-6:]
    font = cell.font.color
    if font is None or font.type != "rgb" or font.tint:
        return {"reason": "theme_dependent", "fill": "#" + background} if in_table else None
    foreground = font.rgb[-6:]
    dark, light = sorted((_luminance(foreground), _luminance(background)))
    ratio = (light + 0.05) / (dark + 0.05)
    minimum = 3 if (cell.font.sz or 11) >= (14 if cell.font.bold else 18) else 4.5
    if ratio >= minimum:
        return None
    return {
        "reason": "low_contrast",
        "font": "#" + foreground,
        "fill": "#" + background,
        "ratio": round(ratio, 3),
        "minimum": minimum,
    }


def _printed_table_continuations(ws) -> list[dict]:
    """Locate record-shaped rows immediately below both a printed area and a native table."""
    from openpyxl.worksheet.cell_range import CellRange
    from openpyxl.worksheet.print_settings import PrintArea

    if not ws.print_area or not ws.tables or ws.sheet_state != "visible":
        return []
    areas = PrintArea.from_string(ws.print_area).ranges
    if len(areas) != 1:
        return []
    area = next(iter(areas))
    last_row = min(ws.max_row, MAX_ROWS)
    if area.max_row >= last_row:
        return []
    ranges = {table.name: CellRange(table.ref) for table in ws.tables.values()}
    found = []

    def record_shape(row, body):
        cells = (ws.cell(row, col) for col in range(body.min_col, body.max_col + 1))
        return tuple(
            ("number" if cell.data_type in ("n", "f") else cell.data_type)
            if cell.value is not None
            else None
            for cell in cells
        )

    for table in ws.tables.values():
        body = ranges[table.name]
        if (
            table.totalsRowCount
            or table.totalsRowShown
            or body.max_row <= body.min_row
            or body.max_col > MAX_COLUMNS
            or body.max_row != area.max_row
            or not body.issubset(area)
        ):
            continue

        shape = record_shape(body.max_row, body)
        if not shape[0] or sum(kind is not None for kind in shape) < 2:
            continue
        through = body.max_row
        while through < last_row and record_shape(through + 1, body) == shape:
            through += 1
        if through == body.max_row:
            continue
        extended = CellRange(
            min_col=body.min_col, min_row=body.min_row, max_col=body.max_col, max_row=through
        )
        if any(
            not extended.isdisjoint(other) for name, other in ranges.items() if name != table.name
        ):
            continue
        printed = CellRange(
            min_col=area.min_col, min_row=area.min_row, max_col=area.max_col, max_row=through
        )
        found.append(
            {
                "sheet": ws.title,
                "table": table.name,
                "table_range": body.coord,
                "print_area": area.coord,
                "first_outside_row": body.max_row + 1,
                "through_row": through,
                "extended_table": extended.coord,
                "extended_print_area": printed.coord,
            }
        )
    return found


def _inspection(data: bytes, book, blockers: list[str]) -> dict:
    from openpyxl.utils.cell import range_boundaries

    preview_left, text_left = MAX_PREVIEW_CELLS, MAX_PREVIEW_TEXT
    sheets, total_cells, formula_count = [], 0, 0
    sheet_charts = charts.xlsx_charts(data)
    heading_count, heading_examples = 0, []
    contrast_count, contrast_examples = 0, []
    print_continuations = []
    for ws in book.worksheets:
        # Preflight bounded both dimensions and area; iter_rows cannot expand an
        # adversarial A1:XFD1048576 worksheet here.
        cells, count, formulas = [], 0, 0
        widths = _stored_column_pixels(ws)
        check_colors = not ws.conditional_formatting
        print_continuations.extend(_printed_table_continuations(ws))
        table_headers = {}
        for table in ws.tables.values():
            if table.headerRowCount:
                first, row, last, _ = range_boundaries(table.ref)
                for column in range(first, min(last, len(widths)) + 1):
                    table_headers[row, column] = FILTER_BUTTON_PIXELS if table.autoFilter else 0
        merged_ends = {
            (area.min_row, area.min_col): area.max_col for area in ws.merged_cells.ranges
        }
        for row in ws.iter_rows():
            dimension = ws.row_dimensions.get(row[0].row)
            visible_row = ws.sheet_state == "visible" and not (dimension and dimension.hidden)
            for cell in row:
                if cell.value is None and not cell.has_style:
                    continue
                count += 1
                is_formula = cell.data_type == "f"
                formulas += is_formula
                if check_colors and visible_row and widths[cell.column - 1]:
                    issue = _header_contrast(cell, (cell.row, cell.column) in table_headers)
                    if issue:
                        contrast_count += 1
                        if len(contrast_examples) < 20:
                            contrast_examples.append(
                                {"sheet": ws.title, "cell": cell.coordinate, **issue}
                            )
                if visible_row and _heading_overflows(cell, widths, merged_ends, table_headers):
                    heading_count += 1
                    if len(heading_examples) < 20:
                        heading_examples.append({"sheet": ws.title, "cell": cell.coordinate})
                if preview_left <= 0 or text_left <= 0:
                    continue
                value = _json_value(cell.value)
                truncated = isinstance(value, str) and len(value) > min(512, text_left)
                if isinstance(value, str):
                    value = value[: min(512, text_left)]
                    text_left -= len(value)
                cells.append(
                    {
                        "cell": cell.coordinate,
                        "formula" if is_formula else "value": value,
                        "data_type": cell.data_type,
                        "number_format": cell.number_format,
                        **(
                            {"font_size": cell.font.sz}
                            if cell.font.sz and cell.font.sz != 11
                            else {}
                        ),
                        **({"text_truncated": True} if truncated else {}),
                    }
                )
                preview_left -= 1
        merged = sorted(ws.merged_cells.ranges, key=lambda span: (span.min_row, span.min_col))
        heights = [
            (row, dim.height)
            for row, dim in sorted(ws.row_dimensions.items())
            if dim.height is not None
        ]
        sheets.append(
            {
                "name": ws.title,
                "rows": ws.max_row,
                "columns": ws.max_column,
                "cell_count": count,
                "formula_count": formulas,
                "cells": cells,
                "cells_truncated": len(cells) < count,
                "tables": [{"name": t.name, "range": t.ref} for t in ws.tables.values()],
                "charts": sheet_charts.get(ws.title, []),
                "layout": {
                    "print_area": ws.print_area or None,
                    "merge_ranges": [span.coord for span in merged[:20]],
                    "row_heights": dict(heights[:20]),
                    "truncated": len(merged) > 20 or len(heights) > 20,
                },
            }
        )
        total_cells += count
        formula_count += formulas
    return {
        "format": "xlsx",
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "sheet_count": len(sheets),
        "cell_count": total_cells,
        "formula_count": formula_count,
        "heading_fit": {"count": heading_count, "examples": heading_examples},
        "header_contrast": {"count": contrast_count, "examples": contrast_examples},
        "print_coverage": {"count": len(print_continuations), "examples": print_continuations[:20]},
        "sheets": sheets,
        "editable": not blockers,
        "edit_blockers": sorted(set(blockers)),
        "needs_recalculation": bool(formula_count),
        "formula_results": "not_calculated",
        "cached_values": "not_read_or_trusted",
        "visual_review": "not_performed",
    }


def inspect(source: Path) -> dict:
    data = _snapshot(source)
    blockers = _package(data)
    book, notes = _load(data)
    try:
        return {**_inspection(data, book, blockers + notes), "status": "inspected"}
    finally:
        book.close()


def _apply_style(cell, style: dict) -> None:
    from openpyxl.styles import PatternFill

    if any(key in style for key in {"bold", "italic", "font_color", "font_size"}):
        font = copy(cell.font)
        if "bold" in style:
            font.bold = style["bold"]
        if "italic" in style:
            font.italic = style["italic"]
        if "font_size" in style:
            font.sz = style["font_size"]
        if "font_color" in style:
            font.color = style["font_color"][1:]
        cell.font = font
    if "fill" in style:
        cell.fill = PatternFill("solid", fgColor=style["fill"][1:])
    if "number_format" in style:
        cell.number_format = style["number_format"]
    if "align" in style or "wrap" in style:
        alignment = copy(cell.alignment)
        if "align" in style:
            alignment.horizontal = style["align"]
        if "wrap" in style:
            alignment.wrap_text = style["wrap"]
        cell.alignment = alignment


def _evidence_cell(cell) -> dict:
    value = _json_value(cell.value)
    entry = {"type": cell.data_type, "value": value}
    if isinstance(value, str) and len(value) > 160:
        entry.update(value=value[:160], text_truncated=True)
    if cell.number_format and cell.number_format != "General":
        entry["number_format"] = cell.number_format[:128]
    return entry


def saved_values(path, locations):
    """Read selected cells from checked XLSX bytes without trusting worksheet dimension hints."""
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        values = {}
        for name, wanted in locations.items():
            if name not in book.sheetnames:
                raise ValueError("Checked cell must name an existing worksheet exactly")
            rows, columns = zip(*(_position(ref) for ref in wanted), strict=True)
            for row in book[name].iter_rows(
                min_row=min(rows), max_row=max(rows), min_col=min(columns), max_col=max(columns)
            ):
                for cell in row:
                    if getattr(cell, "coordinate", None) in wanted:
                        values[name, cell.coordinate] = cell
        return values
    finally:
        book.close()


def formula_changes(source: Path, output: Path, calculation: dict) -> dict:
    """Compare saved display values only at addresses actually calculated this operation."""
    from decimal import Decimal

    locations = {}
    for record in calculation["results"]:
        locations.setdefault(record["sheet"], set()).add(record["cell"])

    before, after = saved_values(source, locations), saved_values(output, locations)
    examples = []
    changed = missing = 0
    for key, current in after.items():
        previous = before.get(key)
        if previous is None or previous.value is None:
            missing += 1
            continue
        if previous.data_type == current.data_type and previous.value == current.value:
            continue
        changed += 1
        if len(examples) < MAX_CHANGE_EXAMPLES:
            example = {
                "sheet": key[0],
                "cell": key[1],
                "previous": _evidence_cell(previous),
                "current": _evidence_cell(current),
            }
            if (
                previous.data_type == current.data_type == "n"
                and previous.number_format == current.number_format
                and type(previous.value) in (int, float)
                and type(current.value) in (int, float)
            ):
                example["numeric_difference"] = str(
                    Decimal(str(current.value)) - Decimal(str(previous.value))
                )
            examples.append(example)
    return {
        "basis": "Previous values are the displays saved in the source, which was not recalculated here. Current values come from the recalculated output.",
        "compared": len(after) - missing,
        "source_value_unavailable": missing,
        "changed": changed,
        "unexamined": calculation["formula_count"] - len(after),
        "examples": examples,
        "examples_truncated": changed > len(examples),
    }


def _edit_layout(ws, change: dict) -> None:
    from openpyxl.worksheet.cell_range import CellRange
    from openpyxl.worksheet.print_settings import PrintArea

    merges = _heading_ranges(change.get("merge_ranges", []))
    heights = _row_heights(change.get("row_heights", {}), ws.max_row)
    if not merges and not heights and "extend_print_area" not in change:
        raise ValueError("A layout change requires a merge, row height or print area extension")
    tables = [CellRange(table.ref) for table in ws.tables.values()]
    for (row, first), last in merges.items():
        if row * last > MAX_CELLS or ws.cell(row, first).value is None:
            raise ValueError("Each merge requires a bounded span with content in its leftmost cell")
        span = CellRange(min_row=row, max_row=row, min_col=first, max_col=last)
        if span in ws.merged_cells.ranges:
            continue
        if any(not span.isdisjoint(other) for other in (*ws.merged_cells.ranges, *tables)):
            raise ValueError("A merge cannot overlap a table or an existing merge")
        for column in range(first + 1, last + 1):
            cell = ws.cell(row, column)
            if cell.value is not None or cell.has_style or cell.comment or cell.hyperlink:
                raise SpreadsheetAuthoringError(
                    "spreadsheet_merge_occupied",
                    "A merge cannot cover an existing value, style, link or comment",
                )
        ws.merge_cells(span.coord)
    for row, height in heights.items():
        ws.row_dimensions[row].height = height
    if "extend_print_area" in change:
        ref = change["extend_print_area"]
        _, _, last_row, last_col = _print_range(ref)
        if last_row > ws.max_row or last_col > ws.max_column or not ws.print_area:
            raise ValueError("Only extend an existing print area within the sheet's used cells")
        old = PrintArea.from_string(ws.print_area).ranges
        if len(old) != 1 or not next(iter(old)).issubset(CellRange(ref)):
            raise ValueError("The extended print area must include the whole original area")
        ws.print_area = ref


def edit(source: Path, spec: dict, output: Path, workspace: Path) -> dict:
    from openpyxl.cell.cell import MergedCell
    from openpyxl.workbook.properties import CalcProperties

    _object(spec, {"expected_source_sha256", "changes"}, "edit")
    expected = spec.get("expected_source_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("expected_source_sha256 from inspect is required")
    if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
        raise ValueError("Editing must write a separate output; the original is never overwritten")
    data = _snapshot(source)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Source is stale: expected_source_sha256 does not match")
    blockers = _package(data)
    if blockers:
        raise ValueError("Workbook cannot be safely edited: " + "; ".join(blockers[:8]))
    changes = spec.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CELLS:
        raise ValueError("changes must be a nonempty bounded list")
    book, notes = _load(data)
    try:
        if notes:
            raise ValueError("Workbook cannot be safely edited: " + "; ".join(notes))
        names = {s.casefold() for s in book.sheetnames}
        normalized, touched = [], set()
        layouts = {}
        extended_tables = set()
        for change in changes:
            if isinstance(change, dict) and {
                "merge_ranges",
                "row_heights",
                "extend_print_area",
            }.intersection(change):
                _object(
                    change,
                    {"sheet", "merge_ranges", "row_heights", "extend_print_area"},
                    "sheet layout change",
                )
                name = change.get("sheet")
                if not isinstance(name, str) or name not in book.sheetnames or name in layouts:
                    raise ValueError("Specify layout changes once per existing worksheet")
                layouts[name] = change
                continue
            if isinstance(change, dict) and "extend_printed_table" in change:
                _object(change, {"sheet", "extend_printed_table"}, "printed table change")
                name, table_name = change.get("sheet"), change.get("extend_printed_table")
                if (
                    not isinstance(name, str)
                    or name not in book.sheetnames
                    or not isinstance(table_name, str)
                ):
                    raise ValueError(
                        "Name the sheet and table identified by the print coverage check"
                    )
                ws = book[name]
                match = next(
                    (
                        item
                        for item in _printed_table_continuations(ws)
                        if item["table"] == table_name
                    ),
                    None,
                )
                if not match or name in extended_tables:
                    raise ValueError(
                        "Choose one printed table per sheet with adjacent rows flagged by the print coverage check"
                    )
                table = ws.tables[table_name]
                table.ref = match["extended_table"]
                if table.autoFilter:
                    table.autoFilter.ref = table.ref
                ws.print_area = match["extended_print_area"]
                extended_tables.add(name)
                continue
            _object(change, {"sheet", "cell", "value", "formula", "format"}, "change")
            sheet, ref = change.get("sheet"), change.get("cell")
            if not isinstance(sheet, str) or sheet not in book.sheetnames:
                raise ValueError("Change must name an existing worksheet exactly")
            _position(ref)
            if (sheet, ref) in touched:
                raise ValueError("Specify each changed cell once")
            touched.add((sheet, ref))
            style_only = "value" not in change and "formula" not in change
            if style_only:
                style = _format(change.get("format", {}))
                if not style:
                    raise ValueError("A formatting-only change requires a nonempty format")
                value, formula = None, False
            else:
                value, formula, style = _cell(
                    {k: v for k, v in change.items() if k not in {"sheet", "cell"}}
                )
            if formula:
                _formula_sheets(value, names)
            ws = book[sheet]
            cell = ws[ref]
            if isinstance(cell, MergedCell):
                raise ValueError("Cannot change a merged cell except its top-left cell")
            for table in ws.tables.values():
                r1, c1, r2, c2 = _range(table.ref)
                if not style_only and r1 == cell.row and c1 <= cell.column <= c2:
                    raise ValueError(
                        "Editing table header cells is unsupported; keep table metadata consistent"
                    )
            normalized.append((cell, value, formula, style, style_only))
        changed = 0
        examples = []
        for cell, value, formula, style, style_only in normalized:
            if not style_only:
                old_value, old_type = cell.value, cell.data_type
                previous = _evidence_cell(cell) if len(examples) < MAX_CHANGE_EXAMPLES else None
                cell.value = value
                if isinstance(value, str) and not formula:
                    cell.data_type = "s"
            _apply_style(cell, style)
            if not style_only and (old_type != cell.data_type or old_value != cell.value):
                changed += 1
                if previous is not None:
                    examples.append(
                        {
                            "sheet": cell.parent.title,
                            "cell": cell.coordinate,
                            "previous": previous,
                            "current": _evidence_cell(cell),
                        }
                    )
        for name, layout in layouts.items():
            _edit_layout(book[name], layout)
        if sum(ws.max_row * ws.max_column for ws in book.worksheets) > MAX_CELLS:
            raise ValueError("Edited workbook exceeds total cell area limit")
        total_text = sum(
            len(cell.value.encode("utf-8"))
            for ws in book.worksheets
            for row in ws.iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        )
        if total_text > MAX_TEXT:
            raise ValueError("Edited workbook exceeds total text limit")
        book.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True, forceFullCalc=True)
        # A source changed during validation is also stale. The loaded bytes remain
        # isolated from the original, and no original file is ever opened for writes.
        if hashlib.sha256(_snapshot(source)).hexdigest() != expected:
            raise ValueError("Source is stale: changed during edit")
        with output.open("xb") as stream:
            book.save(stream)
    finally:
        book.close()
    result = inspect(output)
    return {
        **result,
        "status": "edited",
        "original_sha256": expected,
        "changes_applied": len(changes),
        "revision_evidence": {
            "source_sha256": expected,
            "cell_edits": {
                "changed": changed,
                "examples": examples,
                "examples_truncated": changed > len(examples),
            },
        },
    }
