"""One chart element for Word, Excel, PowerPoint and PDF documents.

Every format takes the same chart object: its kind, an optional title, the categories, and
series of numbers (one per category). The Office formats get native, editable charts (Excel
and PowerPoint through their own libraries; Word through the same DrawingML chart part and
embedded data workbook that PowerPoint uses); the PDF draws it as vector graphics
(documents/charts.py). This module validates the object, describes it, builds the Office chart
XML, and summarizes the charts a file already has so inspection can list them.

Heavy libraries load only when a chart is built.
"""

from __future__ import annotations

import io
import math
import posixpath
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Literal
from xml.etree import ElementTree as ET

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

MAX_SERIES = 50
MAX_POINTS = 1000
MAX_VALUES = 10_000
MAX_CHARTS = 50
MAX_LABEL = 200
MAX_MAGNITUDE = 1e15
FIELDS = (
    "type",
    "chart",
    "title",
    "categories",
    "series",
    "x_title",
    "y_title",
    "number_format",
)

SHAPE = (
    "chart: column (default), bar (horizontal bars), line, pie or scatter. title: optional. "
    "categories: the labels along the axis (for scatter, the x numbers). series: 1-50 of "
    "{name, values}, one finite number per category. x_title and y_title: optional titles of "
    "the categories axis and the values axis. number_format: optional, such as 0, #,##0, "
    '0.0%, #,##0.00 "€" (other text in double quotes). Up to 1000 categories, 10000 values and '
    f"{MAX_CHARTS} charts per file; a pie has one series of non-negative values and no axis "
    "titles. Charts are native and editable in Word, Excel and PowerPoint."
)

EXAMPLE = {
    "chart": "column",
    "title": "Αιτήματα ανά εβδομάδα",
    "categories": ["Εβδ. 1", "Εβδ. 2", "Εβδ. 3"],
    "series": [{"name": "Δίκτυο", "values": [12, 9, 14]}, {"name": "Email", "values": [5, 7, 4]}],
    "y_title": "Αιτήματα",
}

_BAD_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]|[\ud800-\udfff]")
_LITERAL = r'"[^"\x00-\x1f]{1,20}"|[ $+\-()/:]'
_NUMBER_FORMAT = re.compile(
    rf"(?P<prefix>(?:{_LITERAL}){{0,4}})(?P<digits>#,##0|0)(?:\.(?P<decimals>0{{1,6}}))?"
    rf"(?P<percent>%?)(?P<suffix>(?:{_LITERAL}){{0,4}})\Z"
)
_RANGE = re.compile(
    r"(?:(?P<sheet>'(?:[^']|'')+'|[^'!:]+)!)?"
    r"(?P<first>\$?[A-Za-z]{1,3}\$?[0-9]{1,7})(?::(?P<last>\$?[A-Za-z]{1,3}\$?[0-9]{1,7}))?\Z"
)


@dataclass(frozen=True)
class CellRange:
    """A cell range of an xlsx chart (validated against the workbook by the sheet writer)."""

    sheet: str | None
    first: str
    last: str
    text: str


def _label(value: object, what: str = "", *, empty: bool = False) -> str:
    """Text of a title, name or category; what names an item within a list."""
    what = f"{what} " if what else ""
    if not isinstance(value, str):
        raise ValueError(f"{what}must be text")
    if len(value) > MAX_LABEL:
        raise ValueError(f"{what}must be at most {MAX_LABEL} characters")
    if not empty and not value.strip():
        raise ValueError(f"{what}must not be empty")
    if _BAD_TEXT.search(value) or "\n" in value or "\r" in value or "\t" in value:
        raise ValueError(f"{what}must be one line of text without control characters")
    return value


def _number(value: object, what: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number")
    try:
        finite = math.isfinite(value) and abs(value) <= MAX_MAGNITUDE
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{what} must be a finite number between -1e15 and 1e15")
    return value


def _cell_range(value: str, info: ValidationInfo) -> CellRange:
    if not (info.context or {}).get("ranges"):
        raise ValueError("must be a list (cell ranges work only in an xlsx workbook)")
    match = _RANGE.fullmatch(value.strip())
    if not match:
        raise ValueError("must be a list or a cell range such as A2:A9 or 'Sheet name'!B2:B9")
    sheet = match["sheet"]
    if sheet and sheet.startswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    first = match["first"].replace("$", "").upper()
    last = (match["last"] or match["first"]).replace("$", "").upper()
    return CellRange(sheet, first, last, value.strip())


class ChartSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Any
    values: Any

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value):
        return _label(value)

    @field_validator("values", mode="before")
    @classmethod
    def _values(cls, value, info: ValidationInfo):
        if isinstance(value, str):
            return _cell_range(value, info)
        if not isinstance(value, list) or not value:
            raise ValueError("must be a list of numbers, one per category")
        if len(value) > MAX_POINTS:
            raise ValueError(f"can hold at most {MAX_POINTS} numbers")
        return tuple(_number(item, f"value {index}") for index, item in enumerate(value, 1))


class Chart(BaseModel):
    """The chart object every format takes; ranges only in an xlsx sheet's charts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["chart"] | None = None
    chart: Literal["column", "bar", "line", "pie", "scatter"] = "column"
    title: Any = None
    categories: Any
    series: list[ChartSeries]
    x_title: Any = None
    y_title: Any = None
    number_format: Any = None

    @field_validator("title", "x_title", "y_title", mode="before")
    @classmethod
    def _titles(cls, value):
        return None if value is None else _label(value)

    @field_validator("categories", mode="before")
    @classmethod
    def _categories(cls, value, info: ValidationInfo):
        if isinstance(value, str):
            return _cell_range(value, info)
        if not isinstance(value, list) or not value:
            raise ValueError("must be a non-empty list of labels")
        if len(value) > MAX_POINTS:
            raise ValueError(
                f"at most {MAX_POINTS} categories fit a chart (got {len(value)}); group or sum "
                "the data first"
            )
        return tuple(
            _label(item, f"category {index}", empty=True)
            if isinstance(item, str)
            else _number(item, f"category {index}")
            for index, item in enumerate(value, 1)
        )

    @field_validator("series", mode="before")
    @classmethod
    def _series_count(cls, value):
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_SERIES:
            raise ValueError(f"must be a list of 1 to {MAX_SERIES} series")
        return value

    @field_validator("number_format", mode="before")
    @classmethod
    def _number_format(cls, value):
        if value is None or value == "General":
            return None
        if not isinstance(value, str) or not _NUMBER_FORMAT.fullmatch(value):
            raise ValueError(
                'must be like 0, 0.00, #,##0, 0.0% or #,##0.00 "€": digits with optional '
                "decimals and %, other text in double quotes"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self):
        ranged = [isinstance(self.categories, CellRange)] + [
            isinstance(item.values, CellRange) for item in self.series
        ]
        if any(ranged) and not all(ranged):
            raise ValueError(
                "use lists for the categories and every series' values, or cell ranges for all "
                "of them"
            )
        if self.chart == "pie":
            if len(self.series) != 1:
                raise ValueError("a pie chart takes exactly one series; use column or bar for more")
            if self.x_title is not None or self.y_title is not None:
                raise ValueError("a pie chart has no axes; remove x_title and y_title")
        if ranged[0]:
            return self
        count = len(self.categories)
        for item in self.series:
            if len(item.values) != count:
                raise ValueError(
                    f"series {item.name!r} has {len(item.values)} values for {count} categories; "
                    "give exactly one value per category"
                )
        if count * len(self.series) > MAX_VALUES:
            raise ValueError(f"a chart holds at most {MAX_VALUES} values in all its series")
        if self.chart == "scatter" and any(isinstance(item, str) for item in self.categories):
            raise ValueError("a scatter chart's categories are its x values and must be numbers")
        if self.chart == "pie":
            values = self.series[0].values
            negative = next((i for i, v in enumerate(values, 1) if v < 0), None)
            if negative is not None:
                raise ValueError(
                    f"a pie chart's values must not be negative (value {negative} is "
                    f"{values[negative - 1]}); use a column or bar chart for negative values"
                )
            if not any(values):
                raise ValueError("a pie chart needs at least one value above zero")
        return self

    # ------------------------------------------------------------ helpers

    @property
    def inline(self) -> bool:
        return not isinstance(self.categories, CellRange)

    @property
    def legend(self) -> bool:
        return self.chart == "pie" or len(self.series) > 1

    def text_length(self) -> int:
        texts = [self.title, self.x_title, self.y_title, *(item.name for item in self.series)]
        if self.inline:
            texts.extend(item for item in self.categories if isinstance(item, str))
        return sum(len(text) for text in texts if text)


def _place(location: tuple) -> str:
    parts: list[str] = []
    for item in location:
        if isinstance(item, int) and parts:
            parts[-1] = f"{parts[-1]} {item + 1}"
        else:
            parts.append(str(item))
    return " ".join(parts)


def parse(value: object, where: str, *, ranges: bool = False) -> Chart:
    """The validated chart, or a ValueError that says plainly what to fix."""
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    try:
        return Chart.model_validate(value, context={"ranges": ranges})
    except ValidationError as error:
        messages = []
        for item in error.errors()[:3]:
            place = _place(item["loc"])
            if item["type"] == "extra_forbidden":
                text = (
                    "is not a series field; use name and values"
                    if len(item["loc"]) > 1
                    else "is not a chart field; use " + ", ".join(FIELDS)
                )
            elif item["type"] == "missing":
                text = "is required"
            elif item["type"] in {"model_type", "dict_type"}:
                text = "must be an object"
            else:
                text = str(item["msg"]).removeprefix("Value error, ")
            messages.append(f"{place}: {text}" if place else text)
        raise ValueError(f"{where}: " + "; ".join(messages)) from None


def general(value: float | int) -> str:
    """A number as Excel's General format shows it, for labels made from numbers."""
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{value:.15g}"


def precision(number_format: str | None) -> tuple[int, int] | None:
    """The decimals number_format shows, and the factor it multiplies by (100 for %)."""
    if not number_format:
        return None
    match = _NUMBER_FORMAT.fullmatch(number_format)
    return len(match["decimals"] or ""), 100 if match["percent"] else 1


def format_number(value: float, number_format: str | None, decimals: int = 0) -> str:
    """value as number_format shows it (the validated subset), else with decimals digits.

    Halves round away from zero, as spreadsheets show them.
    """
    from decimal import ROUND_HALF_UP, Decimal

    grouping, percent, prefix, suffix = False, False, "", ""
    if number_format:
        match = _NUMBER_FORMAT.fullmatch(number_format)
        grouping = match["digits"] == "#,##0"
        decimals = len(match["decimals"] or "")
        percent = bool(match["percent"])
        prefix, suffix = (
            "".join(part.strip('"') for part in re.findall(_LITERAL, match[name]))
            for name in ("prefix", "suffix")
        )
    shown = Decimal(repr(float(value))) * (100 if percent else 1)
    rounded = abs(shown).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{rounded:{',' if grouping else ''}.{decimals}f}"
    negative = shown < 0 and any(char not in "0.," for char in text)
    return f"{'-' if negative else ''}{prefix}{text}{'%' if percent else ''}{suffix}"


# ---------------------------------------------------------------- Office charts


def _xl_type(kind: str):
    from pptx.enum.chart import XL_CHART_TYPE

    return {
        "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "bar": XL_CHART_TYPE.BAR_CLUSTERED,
        "line": XL_CHART_TYPE.LINE_MARKERS,
        "pie": XL_CHART_TYPE.PIE,
        "scatter": XL_CHART_TYPE.XY_SCATTER,
    }[kind]


def chart_data(spec: Chart):
    """python-pptx chart data (and so its embedded workbook) for an inline chart."""
    from pptx.chart.data import CategoryChartData, XyChartData

    number_format = spec.number_format or "General"
    if spec.chart == "scatter":
        data = XyChartData(number_format=number_format)
        for item in spec.series:
            series = data.add_series(item.name, number_format=number_format)
            for x, y in zip(spec.categories, item.values, strict=True):
                series.add_data_point(x, y)
        return data
    data = CategoryChartData(number_format=number_format)
    categories = spec.categories
    if any(isinstance(item, str) for item in categories):
        # Mixed labels are all written as text, so the category axis has one kind of value.
        categories = [item if isinstance(item, str) else general(item) for item in categories]
    data.categories = list(categories)
    for item in spec.series:
        data.add_series(item.name, list(item.values))
    return data


def style_chart(chart, spec: Chart, *, font_size: float, title_size: float) -> None:
    """Title, axis titles, number format, legend and labels of a python-pptx chart."""
    from pptx.enum.chart import XL_LEGEND_POSITION
    from pptx.util import Pt

    def titled(frame, text, size, bold):
        frame.text = text
        for paragraph in frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(size)
                run.font.bold = bold

    chart.font.size = Pt(font_size)
    # The documents' own typeface; other scripts fall back as in the rest of the file.
    chart.font.name = "Arial"
    chart.has_title = spec.title is not None
    if spec.title is not None:
        titled(chart.chart_title.text_frame, spec.title, title_size, True)
    chart.has_legend = spec.legend
    if spec.legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    if spec.chart == "pie":
        plot = chart.plots[0]
        plot.has_data_labels = True
        labels = plot.data_labels
        labels.show_percentage = True
        labels.show_value = False
        labels.number_format = "0%"
        labels.number_format_is_linked = False
        return
    if spec.chart == "scatter":
        from pptx.enum.chart import XL_MARKER_STYLE

        for series in chart.plots[0].series:
            series.marker.style = XL_MARKER_STYLE.CIRCLE
            series.marker.size = 7
    categories, values = chart.category_axis, chart.value_axis
    if spec.chart == "bar":
        # Categories read from the top, in the order they were listed. The values axis then
        # sits above the bars, where Office and LibreOffice both draw it.
        categories.reverse_order = True
    for axis, text in ((categories, spec.x_title), (values, spec.y_title)):
        axis.has_title = text is not None
        if text is not None:
            titled(axis.axis_title.text_frame, text, font_size, False)
    if spec.number_format:
        values.tick_labels.number_format = spec.number_format
        values.tick_labels.number_format_is_linked = False


def _square_corners(chart_space) -> None:
    """Office draws a chart with rounded corners unless its chart part says otherwise."""
    from pptx.oxml.ns import qn

    if chart_space.find(qn("c:roundedCorners")) is not None:
        return
    element = chart_space.makeelement(qn("c:roundedCorners"), {"val": "0"})
    anchor = chart_space.find(qn("c:date1904"))
    if anchor is None:
        chart_space.insert(0, element)
    else:
        anchor.addnext(element)


def add_slide_chart(shapes, spec: Chart, x, y, width, height):
    """A native PowerPoint chart on a slide; its data travels in an embedded workbook."""
    frame = shapes.add_chart(_xl_type(spec.chart), x, y, width, height, chart_data(spec))
    style_chart(frame.chart, spec, font_size=14, title_size=20)
    _square_corners(frame.chart._chartSpace)
    if spec.title:
        frame._element.nvGraphicFramePr.cNvPr.set("descr", spec.title)
    return frame


def chart_part(spec: Chart, workbook_rid: str) -> tuple[bytes, bytes]:
    """The chart part XML (citing its data workbook by workbook_rid) and that workbook.

    PowerPoint, Word and Excel share DrawingML charts; python-pptx writes the chart XML and its
    embedded data workbook, and the chart is styled through python-pptx's own chart objects.
    """
    from lxml import etree
    from pptx.chart.chart import Chart as SlideChart
    from pptx.oxml import parse_xml

    data = chart_data(spec)
    chart_space = parse_xml(data.xml_bytes(_xl_type(spec.chart)))
    style_chart(SlideChart(chart_space, None), spec, font_size=9, title_size=12)
    _square_corners(chart_space)
    chart_space.get_or_add_externalData().rId = workbook_rid
    xml = etree.tostring(chart_space, xml_declaration=True, encoding="UTF-8", standalone=True)
    return xml, data.xlsx_blob


# ---------------------------------------------------------------- reading charts back

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELATIONSHIP = R + "/package"
CHART_RELATIONSHIP = R + "/chart"
_PLOTS = {
    "barChart": None,
    "bar3DChart": None,
    "lineChart": "line",
    "line3DChart": "line",
    "pieChart": "pie",
    "pie3DChart": "pie",
    "ofPieChart": "pie",
    "doughnutChart": "doughnut",
    "scatterChart": "scatter",
    "areaChart": "area",
    "area3DChart": "area",
    "radarChart": "radar",
    "bubbleChart": "bubble",
    "stockChart": "stock",
    "surfaceChart": "surface",
    "surface3DChart": "surface",
}


def _texts(element) -> str:
    if element is None:
        return ""
    rich = [node.text or "" for node in element.iter(f"{{{A}}}t")]
    if rich:
        return "".join(rich)
    return "".join(node.text or "" for node in element.iter(f"{{{C}}}v"))


def _point_count(element) -> int | None:
    if element is None:
        return None
    count = element.find(f".//{{{C}}}ptCount")
    if count is not None and str(count.get("val", "")).isdigit():
        return int(count.get("val"))
    reference = element.find(f".//{{{C}}}f")
    if reference is not None and reference.text:
        cells = reference.text.rsplit("!", 1)[-1].replace("$", "").split(":")
        try:
            (r1, c1), (r2, c2) = (_a1(cells[0]), _a1(cells[-1]))
        except ValueError:
            return None
        return (abs(r2 - r1) + 1) * (abs(c2 - c1) + 1)
    return None


def _a1(cell: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([0-9]{1,7})", cell.upper())
    if not match:
        raise ValueError(cell)
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    return int(match[2]), column


def summarize(xml: bytes, *, ranges: bool = False) -> dict:
    """What a chart part shows: its kind, title, and each series' name and point count (and,
    with ranges, the workbook cells its values come from)."""
    root = ET.fromstring(xml)
    chart = root.find(f"{{{C}}}chart")
    plot_area = chart.find(f"{{{C}}}plotArea") if chart is not None else None
    kinds, series = [], []
    for plot in list(plot_area) if plot_area is not None else ():
        name = plot.tag.rsplit("}", 1)[-1]
        if name not in _PLOTS:
            continue
        kind = _PLOTS[name]
        if kind is None:
            direction = plot.find(f"{{{C}}}barDir")
            kind = "bar" if direction is not None and direction.get("val") == "bar" else "column"
        kinds.append(kind)
        for item in plot.findall(f"{{{C}}}ser"):
            if len(series) >= MAX_SERIES:
                break
            values = item.find(f"{{{C}}}val")
            if values is None:
                values = item.find(f"{{{C}}}yVal")
            entry = {"name": _texts(item.find(f"{{{C}}}tx"))[:MAX_LABEL], "points": None}
            entry["points"] = _point_count(values)
            reference = values.find(f".//{{{C}}}f") if values is not None else None
            if ranges and reference is not None and reference.text:
                entry["values_range"] = reference.text[:MAX_LABEL]
            series.append(entry)
    title = chart.find(f"{{{C}}}title") if chart is not None else None
    return {
        "chart": kinds[0] if len(kinds) == 1 else (kinds or ["unknown"]),
        "title": _texts(title)[:MAX_LABEL] or None,
        "series": series,
    }


def _relationships(archive: zipfile.ZipFile, part: str) -> list[tuple[str, str, str]]:
    """(id, type, target part) of a part's internal relationships."""
    folder, name = posixpath.split(part)
    rels = posixpath.join(folder, "_rels", name + ".rels")
    if rels not in archive.namelist():
        return []
    result = []
    for relation in ET.fromstring(archive.read(rels)):
        if relation.get("TargetMode", "").lower() == "external":
            continue
        target = relation.get("Target", "")
        target = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join(folder, target))
        )
        result.append((relation.get("Id", ""), relation.get("Type", ""), target))
    return result


def chart_parts(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    """The chart parts a part relates to, by relationship id."""
    return {
        rid: target
        for rid, kind, target in _relationships(archive, part)
        if kind == CHART_RELATIONSHIP and target in archive.namelist()
    }


def xlsx_charts(data: bytes) -> dict[str, list[dict]]:
    """Each sheet's charts (where they are anchored, and what they show), by sheet name."""
    result: dict[str, list[dict]] = {}
    count = 0
    s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    xdr = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if "xl/workbook.xml" not in archive.namelist():
            return result
        sheets = {rid: target for rid, _, target in _relationships(archive, "xl/workbook.xml")}
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        for sheet in workbook.iter(f"{{{s}}}sheet"):
            part = sheets.get(sheet.get(f"{{{R}}}id", ""))
            if part is None or part not in archive.namelist():
                continue
            for _, kind, drawing in _relationships(archive, part):
                if not kind.endswith("/drawing") or drawing not in archive.namelist():
                    continue
                charts = chart_parts(archive, drawing)
                for anchor in ET.fromstring(archive.read(drawing)):
                    reference = anchor.find(f".//{{{C}}}chart")
                    target = (
                        None if reference is None else charts.get(reference.get(f"{{{R}}}id", ""))
                    )
                    if target is None or count >= MAX_CHARTS:
                        continue
                    count += 1
                    entry = summarize(archive.read(target), ranges=True)
                    start = anchor.find(f"{{{xdr}}}from")
                    if start is not None:
                        column = int(start.findtext(f"{{{xdr}}}col", "0"))
                        row = int(start.findtext(f"{{{xdr}}}row", "0"))
                        entry["anchor"] = _column_name(column + 1) + str(row + 1)
                    result.setdefault(sheet.get("name", ""), []).append(entry)
    return result


def _column_name(column: int) -> str:
    name = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        name = chr(65 + remainder) + name
    return name


def chart_workbook_problem(data: bytes) -> str | None:
    """Why an embedded chart data workbook is not a plain OOXML workbook, or None."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                len(entries) > 512
                or sum(entry.file_size for entry in entries) > 32 * 1024 * 1024
                or len(set(names)) != len(names)
            ):
                return "Embedded chart data exceeds the supported size."
            for entry in entries:
                lowered = entry.filename.lower()
                if entry.flag_bits & 1 or "\\" in lowered or ".." in lowered.split("/"):
                    return "Embedded chart data must be a plain workbook."
                if any(marker in lowered for marker in ("vbaproject", "embeddings/", "activex")):
                    return "Embedded chart data must not hold programs or other files."
                if lowered.endswith((".xml", ".rels")):
                    raw = archive.read(entry)
                    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                        return "Embedded chart data must not use XML entities."
                    if lowered.endswith(".rels"):
                        for relation in ET.fromstring(raw):
                            if relation.get("TargetMode", "").lower() == "external":
                                return "Embedded chart data must not link to other files."
            if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
                return "Embedded chart data must be an Excel workbook."
            types = archive.read("[Content_Types].xml").lower()
            if b"macroenabled" in types:
                return "Embedded chart data must not hold programs or other files."
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError, NotImplementedError):
        return "Embedded chart data must be a valid Excel workbook."
    return None
