"""Build the Alysis README banner (light + dark) and the small owl coin icon.

Everything is drawn with square pixels, so the SVGs need no fonts: GitHub and
PyPI show them as images, where external fonts never load.

Usage, from the repository root:
    python3 scripts/brand/make_brand_svgs.py docs/assets/brand
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# The official Alysis pixel owl, 21 x 21 cells, taken cell for cell from
# docs/assets/banner.svg in AlysisAi/AlysisCode.  '#' = ink, 'o' = white.
OWL = """\
.####.........####...
..###.........###....
...#############.....
...#############.....
...##############....
..##ooo#####ooo###...
..##ooooo##oooooo#...
..##o##oo##o##ooo#...
.###o##ooooo##ooo#...
.##ooooooooooooo##...
.##ooooo###ooo#####..
.##ooooo###o#######..
###oooooo#oo########.
##oooooooooo########.
##oooooooooo#########
##oooooooooo#########
###ooooooooo#########
.##ooooooooo#########
.##oooooooooo#######.
.###ooooooooo#######.
..################...""".splitlines()

# 5 x 7 pixel font: only the glyphs the banner uses.
FONT = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    ".": [".", ".", ".", ".", ".", ".", "#"],
    " ": ["...", "...", "...", "...", "...", "...", "..."],
}

THEMES = {
    "light": {
        "bg": "#F7F7F2",
        "border": "#E2E0D6",
        "disc": "#E8E5DA",
        "bead": "#CFCABB",
        "ink": "#0E0F0E",
        "white": "#FFFFFF",
        "word": "#0E0F0E",
        "claim": "#3F423D",
        "accent": "#0C8050",
        "star": None,
    },
    "dark": {
        "bg": "#111311",
        "border": "#232622",
        "disc": "#E9E6DA",
        "bead": "#3A3E39",
        "ink": "#0E0F0E",
        "white": "#FFFFFF",
        "word": "#F2F0E8",
        "claim": "#BDBBB2",
        "accent": "#3CCB7F",
        "star": "#E9E6DA",
    },
}


def cells_to_path(cells: list[tuple[int, int]], x0: float, y0: float, size: float) -> str:
    """Merge horizontal runs of cells into one SVG path (keeps files small)."""
    rows: dict[int, list[int]] = {}
    for cx, cy in cells:
        rows.setdefault(cy, []).append(cx)
    parts = []
    for cy in sorted(rows):
        xs = sorted(rows[cy])
        start = prev = xs[0]
        for cx in xs[1:] + [None]:
            if cx is not None and cx == prev + 1:
                prev = cx
                continue
            w = (prev - start + 1) * size
            parts.append(f"M{x0 + start * size:g} {y0 + cy * size:g}h{w:g}v{size:g}h{-w:g}z")
            if cx is not None:
                start = prev = cx
    return "".join(parts)


def owl_paths(x0: float, y0: float, size: float, ink: str, white: str) -> str:
    ink_cells, white_cells = [], []
    for cy, row in enumerate(OWL):
        for cx, ch in enumerate(row):
            if ch == "#":
                ink_cells.append((cx, cy))
            elif ch == "o":
                white_cells.append((cx, cy))
    return (
        f'<path fill="{white}" d="{cells_to_path(white_cells, x0, y0, size)}"/>'
        f'<path fill="{ink}" d="{cells_to_path(ink_cells, x0, y0, size)}"/>'
    )


def text_cells(text: str) -> tuple[list[tuple[int, int]], int]:
    """Lay out text in the pixel font; returns cells and the width in cells."""
    cells, cursor = [], 0
    for i, ch in enumerate(text):
        glyph = FONT[ch]
        for cy, row in enumerate(glyph):
            for cx, bit in enumerate(row):
                if bit == "#":
                    cells.append((cursor + cx, cy))
        cursor += len(glyph[0])
        if i < len(text) - 1:
            cursor += 1  # one empty column between glyphs
    return cells, cursor


def text_path(text: str, x0: float, y0: float, size: float, fill: str) -> tuple[str, float]:
    cells, width = text_cells(text)
    return f'<path fill="{fill}" d="{cells_to_path(cells, x0, y0, size)}"/>', width * size


def coin(
    cx: float, cy: float, r: float, disc: str, bead: str, beads: int = 44, bead_size: float = 6
) -> str:
    out = [f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{disc}"/>']
    ring = r + 16
    for i in range(beads):
        a = 2 * math.pi * i / beads
        bx = cx + ring * math.cos(a) - bead_size / 2
        by = cy + ring * math.sin(a) - bead_size / 2
        out.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bead_size:g}" height="{bead_size:g}" fill="{bead}"/>'
        )
    return "".join(out)


STARS = [
    (40, 40, 4, 0.9),
    (380, 30, 4, 0.5),
    (880, 48, 6, 0.9),
    (920, 120, 4, 0.6),
    (896, 200, 4, 0.45),
    (918, 268, 6, 0.8),
    (640, 292, 4, 0.5),
    (44, 276, 4, 0.6),
    (820, 292, 4, 0.7),
    (580, 34, 4, 0.4),
]


def banner(theme: str) -> str:
    t = THEMES[theme]
    W, H = 960, 320
    owl_size = 8
    cx, cy, r = 226, 160, 108
    owl_x = cx - 21 * owl_size / 2
    owl_y = cy - 21 * owl_size / 2

    word, word_w = text_path("ALYSIS", 402, 64, 12, t["word"])
    dot = f'<rect x="{402 + word_w + 12:g}" y="{64 + 6 * 12:g}" width="12" height="12" fill="{t["accent"]}"/>'
    line1, _ = text_path("EVERY AGENT YOU NEED.", 402, 178, 4, t["claim"])
    line2, _ = text_path("ONE APP.", 402, 220, 6, t["word"])

    stars = ""
    if t["star"]:
        stars = "".join(
            f'<rect x="{x}" y="{y}" width="{s}" height="{s}" fill="{t["star"]}" fill-opacity="{o}"/>'
            for x, y, s, o in STARS
        )

    label = "Alysis: the Alysis pixel owl on a coin-shaped moon, next to the words: Every agent you need. One app."
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="{label}" shape-rendering="crispEdges">'
        f"<title>{label}</title>"
        f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="20" fill="{t["bg"]}" stroke="{t["border"]}" stroke-width="2"/>'
        f"{stars}"
        f'<g shape-rendering="geometricPrecision">{coin(cx, cy, r, t["disc"], t["bead"])}</g>'
        f"{owl_paths(owl_x, owl_y, owl_size, t['ink'], t['white'])}"
        f"{word}{dot}{line1}{line2}"
        "</svg>\n"
    )


def owl_icon() -> str:
    """Small owl on its coin; reads on light and dark pages alike."""
    size = 4
    W = 132
    c = W / 2
    t = THEMES["light"]
    label = "The Alysis pixel owl"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{W}" viewBox="0 0 {W} {W}" '
        f'role="img" aria-label="{label}" shape-rendering="crispEdges"><title>{label}</title>'
        f'<g shape-rendering="geometricPrecision">{coin(c, c, 50, t["disc"], "#B9B4A4", beads=32, bead_size=4)}</g>'
        f"{owl_paths(c - 21 * size / 2, c - 21 * size / 2, size, t['ink'], t['white'])}"
        "</svg>\n"
    )


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    (out / "alysis-banner-light.svg").write_text(banner("light"), encoding="utf-8")
    (out / "alysis-banner-dark.svg").write_text(banner("dark"), encoding="utf-8")
    (out / "alysis-owl.svg").write_text(owl_icon(), encoding="utf-8")


if __name__ == "__main__":
    main()
