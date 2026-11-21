"""Charts in PDFs made from Markdown: a ```chart block holding the shared chart object
(office/charts.py), drawn as vector graphics with the document's own fonts.

Text in any script the PDF supports goes through the same per-character font fallback as the
body text, so a Greek title or Chinese categories print as they do in a paragraph.
"""

from __future__ import annotations

import json
import math

from ..office import charts

WIDTH = 475.0  # The text frame's width: A4 less the margins and the frame's padding.
PAD = 2.0
TITLE_SIZE = 11.0
LABEL_SIZE = 8.0
# The reference categorical palette, in its fixed order.
PALETTE = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
)
INK = "#17252f"
MUTED = "#52514e"
GRID = "#e3e6e8"
AXIS = "#9aa3a9"
ROTATED = math.sqrt(0.5)  # sin and cos of the 45° category labels


def parse(source: str, number: int) -> charts.Chart:
    """The chart a ```chart block holds, or a ValueError that says what to fix."""
    where = f"Chart block {number}"

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{where}: the field {key!r} appears twice")
            result[key] = value
        return result

    def not_finite(value):
        raise ValueError(f"{where}: numbers must be finite ({value} is not)")

    try:
        value = json.loads(source, object_pairs_hook=unique, parse_constant=not_finite)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{where} must hold one JSON chart object ({error.msg}, line {error.lineno})"
        ) from None
    return charts.parse(value, where)


def _color(index: int):
    """The palette in order; past its end, the same hues darkened, so series stay apart."""
    from reportlab.lib import colors

    base = colors.HexColor(PALETTE[index % len(PALETTE)])
    shade = 1 - 0.3 * min(2, index // len(PALETTE))
    return colors.Color(base.red * shade, base.green * shade, base.blue * shade)


def _decimals(step: float) -> int:
    """The decimals that show every multiple of step exactly."""
    decimals = max(0, -math.floor(math.log10(step) + 1e-9))
    if abs(round(step * 10**decimals) - step * 10**decimals) > 1e-6:
        decimals += 1
    return min(decimals, 10)


def _ticks(
    low: float, high: float, *, zero: bool, number_format: str | None = None
) -> tuple[list[float], int]:
    """Evenly spaced round axis values covering low..high, and the decimals they need.

    With a number format, the step is one the format shows exactly (no 2.5 under 0).
    """
    if zero:
        low, high = min(low, 0.0), max(high, 0.0)
    if low == high:
        low, high = (low - 1, high + 1) if low else (0.0, 1.0)
    raw = (high - low) / 5
    magnitude = 10 ** math.floor(math.log10(raw))
    shown = charts.precision(number_format)
    steps = [m * magnitude for m in (1, 2, 2.5, 5, 10)]
    if shown is not None:
        decimals, factor = shown
        steps = [step for step in steps if _decimals(step * factor) <= decimals] or [
            10.0**-decimals / factor
        ]
    step = next((step for step in steps if step >= raw * 0.999), steps[-1])
    start = math.floor(low / step + 1e-9) * step
    count = max(1, math.ceil((high - start) / step - 1e-9))
    ticks = [start + index * step for index in range(count + 1)]
    ticks = [0.0 if abs(tick) < step * 1e-9 else tick for tick in ticks]
    return ticks, _decimals(step)


def _includes_zero(values: list[float]) -> bool:
    """Whether a line or scatter axis starts at zero, as spreadsheet charts decide it: when
    zero is near the data relative to its spread."""
    low, high = min(values), max(values)
    if low >= 0:
        return low <= high * 5 / 6
    if high <= 0:
        return high >= low * 5 / 6
    return True


class _Text:
    """Text in the document's fonts, split into runs a font has glyphs for."""

    def __init__(self, fonts: list[str], supported: frozenset[int]):
        self.regular, self.bold = fonts[0], fonts[1]
        self.supported = supported

    def runs(self, text: str, bold: bool) -> list[tuple[str, str]]:
        from .__main__ import _SHAPING_RANGES, _fallback_fonts, _font_runs

        if any(start <= ord(c) <= end for c in text for start, end in _SHAPING_RANGES):
            raise ValueError(
                "This text requires complex-script shaping. Use a renderer with shaping support."
            )
        body = self.bold if bold else self.regular
        fallbacks = _fallback_fonts(text, self.supported)
        return [(font or body, part) for font, part in _font_runs(text, self.supported, fallbacks)]

    def width(self, text: str, size: float, bold: bool = False) -> float:
        from reportlab.pdfbase.pdfmetrics import stringWidth

        return sum(stringWidth(part, font, size) for font, part in self.runs(text, bold))

    def fit(self, text: str, size: float, limit: float, bold: bool = False) -> str:
        """text, shortened with an ellipsis to fit limit."""
        if self.width(text, size, bold) <= limit:
            return text
        while text and self.width(text + "…", size, bold) > limit:
            text = text[:-1]
        return text.rstrip() + "…" if text else ""

    def wrap(self, text: str, size: float, limit: float, lines: int, bold: bool = False):
        """Lines of text within limit: broken at spaces, or anywhere in a word too long for
        a line (as text without spaces needs); past lines, the last line is shortened."""
        result, line = [], ""
        for char in text:
            if self.width(line + char, size, bold) <= limit or not line:
                line += char
                continue
            cut = line.rfind(" ")
            if cut > 0 and not char.isspace():
                result.append(line[:cut])
                line = line[cut + 1 :] + char
            else:
                result.append(line)
                line = "" if char.isspace() else char
        result.append(line)
        result = [item.strip() for item in result if item.strip()]
        if len(result) > lines:
            result = result[: lines - 1] + [self.fit(" ".join(result[lines - 1 :]), size, limit)]
        return result

    def draw(self, group, text, x, y, size, *, bold=False, anchor="start", color=INK, angle=0):
        from reportlab.graphics.shapes import Group, String
        from reportlab.lib import colors
        from reportlab.pdfbase.pdfmetrics import stringWidth

        runs = self.runs(text, bold)
        total = sum(stringWidth(part, font, size) for font, part in runs)
        start = {"start": 0.0, "middle": -total / 2, "end": -total}[anchor]
        fill = colors.HexColor(color)
        holder = Group()
        holder.translate(x, y)
        if angle:
            holder.rotate(angle)
        for font, part in runs:
            holder.add(String(start, 0, part, fontName=font, fontSize=size, fillColor=fill))
            start += stringWidth(part, font, size)
        group.add(holder)


def drawing(chart: charts.Chart, fonts: list[str], supported: frozenset[int]):
    """The chart as a reportlab Drawing that fits the text frame's width."""
    from reportlab.graphics.shapes import Drawing

    text = _Text(fonts, supported)
    height = WIDTH * 0.6
    if chart.chart == "bar":
        height = min(680.0, max(height, 90 + 15 * len(chart.categories)))
    canvas = Drawing(WIDTH, height)
    top = height - PAD
    if chart.title:
        for line in text.wrap(chart.title, TITLE_SIZE, WIDTH - 2 * PAD, 3, bold=True):
            top -= TITLE_SIZE * 1.25
            text.draw(canvas, line, WIDTH / 2, top + 2, TITLE_SIZE, bold=True, anchor="middle")
        top -= 8
    bottom = PAD + _legend(canvas, chart, text)
    if chart.chart == "pie":
        _pie(canvas, chart, text, top, bottom)
    elif chart.chart == "bar":
        _bars(canvas, chart, text, top, bottom)
    else:
        _columns_lines_points(canvas, chart, text, top, bottom)
    return canvas


def _legend(canvas, chart: charts.Chart, text: _Text) -> float:
    """The legend along the bottom (for several series, or a pie's slices); its height."""
    from reportlab.graphics.shapes import Rect

    if not chart.legend:
        return 0.0
    names = (
        [label if isinstance(label, str) else charts.general(label) for label in chart.categories]
        if chart.chart == "pie"
        else [item.name for item in chart.series]
    )
    entries = []
    for index, name in enumerate(names):
        name = text.fit(name, LABEL_SIZE, WIDTH * 0.45)
        entries.append((index, name, 12 + text.width(name, LABEL_SIZE) + 12))
    rows, row, used = [], [], 0.0
    for entry in entries:
        if row and used + entry[2] > WIDTH - 2 * PAD:
            rows.append((row, used))
            row, used = [], 0.0
        row.append(entry)
        used += entry[2]
    rows.append((row, used))
    line = LABEL_SIZE + 5
    rows = rows[: max(1, int(WIDTH * 0.25 // line))]
    for number, (items, used) in enumerate(reversed(rows)):
        y = PAD + number * line
        x = (WIDTH - used + 12) / 2
        for index, name, width in items:
            canvas.add(Rect(x, y, 8, 8, fillColor=_color(index), strokeColor=None))
            text.draw(canvas, name, x + 12, y + 1, LABEL_SIZE, color=MUTED)
            x += width
    return len(rows) * line + 6


def _value_labels(chart, ticks, decimals) -> list[str]:
    return [charts.format_number(tick, chart.number_format, decimals) for tick in ticks]


def _grid(canvas, ticks, scale, start, end, *, horizontal: bool):
    from reportlab.graphics.shapes import Line
    from reportlab.lib import colors

    for tick in ticks:
        position = scale(tick)
        if horizontal:
            canvas.add(Line(start, position, end, position, strokeColor=colors.HexColor(GRID)))
        else:
            canvas.add(Line(position, start, position, end, strokeColor=colors.HexColor(GRID)))


def _columns_lines_points(canvas, chart: charts.Chart, text: _Text, top: float, bottom: float):
    """Columns, lines or scattered points against a vertical values axis."""
    from reportlab.graphics.shapes import Circle, Line, PolyLine, Rect
    from reportlab.lib import colors

    values = [value for item in chart.series for value in item.values]
    scatter = chart.chart == "scatter"
    ticks, decimals = _ticks(
        min(values),
        max(values),
        zero=chart.chart == "column" or _includes_zero(values),
        number_format=chart.number_format,
    )
    labels = _value_labels(chart, ticks, decimals)
    left = PAD + max(text.width(label, LABEL_SIZE) for label in labels) + 5
    if chart.y_title:
        left += LABEL_SIZE + 6
        text.draw(
            canvas,
            text.fit(chart.y_title, LABEL_SIZE, top - bottom),
            PAD + LABEL_SIZE,
            (top + bottom) / 2,
            LABEL_SIZE,
            anchor="middle",
            color=MUTED,
            angle=90,
        )
    if chart.x_title:
        text.draw(
            canvas,
            text.fit(chart.x_title, LABEL_SIZE, WIDTH - 2 * PAD),
            WIDTH / 2,
            bottom,
            LABEL_SIZE,
            anchor="middle",
            color=MUTED,
        )
        bottom += LABEL_SIZE + 6
    if scatter:
        x_ticks, x_decimals = _ticks(
            min(chart.categories), max(chart.categories), zero=_includes_zero(chart.categories)
        )
        x_labels = [charts.format_number(tick, None, x_decimals) for tick in x_ticks]
        right = WIDTH - PAD - text.width(x_labels[-1], LABEL_SIZE) / 2
        left = max(left, PAD + text.width(x_labels[0], LABEL_SIZE) / 2)
        bottom += LABEL_SIZE + 6
        span = (x_ticks[0], x_ticks[-1])

        def x_of(value):
            return left + (value - span[0]) / (span[1] - span[0]) * (right - left)

        step = 1
        while step < len(x_ticks) and any(
            x_of(x_ticks[i + step]) - x_of(x_ticks[i])
            < (text.width(x_labels[i], LABEL_SIZE) + text.width(x_labels[i + step], LABEL_SIZE)) / 2
            + 4
            for i in range(0, len(x_ticks) - step, step)
        ):
            step += 1
        for index in range(0, len(x_ticks), step):
            text.draw(
                canvas,
                x_labels[index],
                x_of(x_ticks[index]),
                bottom - LABEL_SIZE - 3,
                LABEL_SIZE,
                anchor="middle",
                color=MUTED,
            )
        _grid(canvas, x_ticks, x_of, bottom, top, horizontal=False)
    else:
        right = WIDTH - PAD
        count = len(chart.categories)
        names = [
            label if isinstance(label, str) else charts.general(label) for label in chart.categories
        ]
        widths = [text.width(name, LABEL_SIZE) for name in names]
        slot = (right - left) / count
        if max(widths) <= slot - 3:
            label_height, rotated, step = LABEL_SIZE + 4, False, 1
        else:
            # 45° labels, every step-th one when the slots are narrower than a line of text.
            rotated, step = True, max(1, math.ceil((LABEL_SIZE + 3) / ROTATED / slot))
            longest = min(max(widths), 110.0)
            label_height = (longest + LABEL_SIZE) * ROTATED + 4
            # Room at the left for the first labels, which reach left of their slots.
            reach = max(
                min(widths[i], 110.0) * ROTATED - (i + 0.5) * slot
                for i in range(0, min(count, 20), step)
            )
            left = max(left, PAD + reach)
            slot = (right - left) / count
        bottom += label_height
        for index in range(0, count, step):
            x = left + (index + 0.5) * slot
            if rotated:
                name = text.fit(names[index], LABEL_SIZE, min(110.0, (x - PAD) / ROTATED))
                text.draw(
                    canvas,
                    name,
                    x + LABEL_SIZE * 0.35,
                    bottom - 6,
                    LABEL_SIZE,
                    anchor="end",
                    color=MUTED,
                    angle=45,
                )
            else:
                text.draw(
                    canvas,
                    names[index],
                    x,
                    bottom - LABEL_SIZE - 2,
                    LABEL_SIZE,
                    anchor="middle",
                    color=MUTED,
                )

        def x_of(index):
            return left + (index + 0.5) * slot

    def y_of(value):
        return bottom + (value - ticks[0]) / (ticks[-1] - ticks[0]) * (top - bottom)

    _grid(canvas, ticks, y_of, left, right, horizontal=True)
    for tick, label in zip(ticks, labels, strict=True):
        text.draw(canvas, label, left - 4, y_of(tick) - 3, LABEL_SIZE, anchor="end", color=MUTED)
    baseline = y_of(min(max(0.0, ticks[0]), ticks[-1]))
    canvas.add(Line(left, baseline, right, baseline, strokeColor=colors.HexColor(AXIS)))
    series_count = len(chart.series)
    for number, item in enumerate(chart.series):
        color = _color(number)
        if scatter:
            for x, y in zip(chart.categories, item.values, strict=True):
                canvas.add(
                    Circle(
                        x_of(x),
                        y_of(y),
                        2.6,
                        fillColor=color,
                        strokeColor=colors.white,
                        strokeWidth=0.6,
                    )
                )
        elif chart.chart == "line":
            points = [
                coordinate
                for index, value in enumerate(item.values)
                for coordinate in (x_of(index), y_of(value))
            ]
            if len(item.values) > 1:
                canvas.add(PolyLine(points, strokeColor=color, strokeWidth=1.6))
            if len(item.values) <= 60:
                for index, value in enumerate(item.values):
                    canvas.add(
                        Circle(
                            x_of(index),
                            y_of(value),
                            2.2,
                            fillColor=color,
                            strokeColor=colors.white,
                            strokeWidth=0.6,
                        )
                    )
        else:
            slot = x_of(1) - x_of(0) if len(chart.categories) > 1 else right - left
            group = slot * 0.72
            width = group / series_count
            gap = min(1.0, width * 0.15)
            for index, value in enumerate(item.values):
                x = x_of(index) - group / 2 + number * width
                y = y_of(value)
                canvas.add(
                    Rect(
                        x + gap / 2,
                        min(y, baseline),
                        max(width - gap, 0.2),
                        abs(y - baseline),
                        fillColor=color,
                        strokeColor=None,
                    )
                )


def _bars(canvas, chart: charts.Chart, text: _Text, top: float, bottom: float):
    """Horizontal bars: categories down the left from the top, values along the bottom."""
    from reportlab.graphics.shapes import Line, Rect
    from reportlab.lib import colors

    values = [value for item in chart.series for value in item.values]
    ticks, decimals = _ticks(min(values), max(values), zero=True, number_format=chart.number_format)
    labels = _value_labels(chart, ticks, decimals)
    if chart.y_title:
        text.draw(
            canvas,
            text.fit(chart.y_title, LABEL_SIZE, WIDTH - 2 * PAD),
            WIDTH / 2,
            bottom,
            LABEL_SIZE,
            anchor="middle",
            color=MUTED,
        )
        bottom += LABEL_SIZE + 6
    bottom += LABEL_SIZE + 6
    left = PAD
    if chart.x_title:
        text.draw(
            canvas,
            text.fit(chart.x_title, LABEL_SIZE, top - bottom),
            PAD + LABEL_SIZE,
            (top + bottom) / 2,
            LABEL_SIZE,
            anchor="middle",
            color=MUTED,
            angle=90,
        )
        left += LABEL_SIZE + 6
    names = [
        text.fit(
            label if isinstance(label, str) else charts.general(label), LABEL_SIZE, WIDTH * 0.3
        )
        for label in chart.categories
    ]
    left = max(
        left + max(text.width(name, LABEL_SIZE) for name in names) + 5,
        PAD + text.width(labels[0], LABEL_SIZE) / 2,
    )
    right = WIDTH - PAD - text.width(labels[-1], LABEL_SIZE) / 2
    count = len(chart.categories)
    slot = (top - bottom) / count
    step = max(1, math.ceil((LABEL_SIZE + 2) / slot))

    def x_of(value):
        return left + (value - ticks[0]) / (ticks[-1] - ticks[0]) * (right - left)

    def y_of(index):  # the middle of a category's slot, the first at the top
        return top - (index + 0.5) * slot

    _grid(canvas, ticks, x_of, bottom, top, horizontal=False)
    for tick, label in zip(ticks, labels, strict=True):
        text.draw(
            canvas,
            label,
            x_of(tick),
            bottom - LABEL_SIZE - 3,
            LABEL_SIZE,
            anchor="middle",
            color=MUTED,
        )
    for index in range(0, count, step):
        text.draw(
            canvas, names[index], left - 4, y_of(index) - 3, LABEL_SIZE, anchor="end", color=MUTED
        )
    baseline = x_of(min(max(0.0, ticks[0]), ticks[-1]))
    canvas.add(Line(baseline, bottom, baseline, top, strokeColor=colors.HexColor(AXIS)))
    group = slot * 0.72
    height = group / len(chart.series)
    gap = min(1.0, height * 0.15)
    for number, item in enumerate(chart.series):
        for index, value in enumerate(item.values):
            y = y_of(index) + group / 2 - (number + 1) * height
            x = x_of(value)
            canvas.add(
                Rect(
                    min(x, baseline),
                    y + gap / 2,
                    abs(x - baseline),
                    max(height - gap, 0.2),
                    fillColor=_color(number),
                    strokeColor=None,
                )
            )


def _pie(canvas, chart: charts.Chart, text: _Text, top: float, bottom: float):
    """Slices clockwise from the top, each labelled with its share."""
    from reportlab.graphics.shapes import Circle, Wedge
    from reportlab.lib import colors

    values = chart.series[0].values
    total = float(sum(values))
    label_room = text.width("100%", LABEL_SIZE) + 6
    radius = max(10.0, min((top - bottom) / 2 - LABEL_SIZE - 4, WIDTH / 2 - label_room - PAD))
    cx, cy = WIDTH / 2, (top + bottom) / 2
    angle = 90.0
    for index, value in enumerate(values):
        if value <= 0:
            continue
        sweep = value / total * 360
        if sweep >= 359.999:
            canvas.add(Circle(cx, cy, radius, fillColor=_color(index), strokeColor=None))
        else:
            canvas.add(
                Wedge(
                    cx,
                    cy,
                    radius,
                    angle - sweep,
                    angle,
                    fillColor=_color(index),
                    strokeColor=colors.white,
                    strokeWidth=1,
                )
            )
        share = value / total * 100
        if share >= 3:
            middle = math.radians(angle - sweep / 2)
            x = cx + (radius + 5) * math.cos(middle)
            y = cy + (radius + 5) * math.sin(middle) - LABEL_SIZE / 3
            cosine = math.cos(middle)
            anchor = "start" if cosine > 0.2 else "end" if cosine < -0.2 else "middle"
            if anchor == "middle":
                y += 3 if math.sin(middle) > 0 else -LABEL_SIZE + 2
            label = f"{share:.0f}%"
            text.draw(canvas, label, x, y, LABEL_SIZE, anchor=anchor, color=MUTED)
        angle -= sweep
