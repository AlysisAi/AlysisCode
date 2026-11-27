"""Build the small Alysis owl coin used in the README footer.

README banners, the social preview, and the owl cast are drawn by
scripts/brand/make_team_banners.py. Everything is square pixels, so the SVGs
need no fonts.

Usage, from the repository root:
    python3 scripts/brand/make_brand_svgs.py docs/assets/brand
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# The official Alysis pixel owl, 21 x 21 cells.  '#' = ink, 'o' = white.
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
    """Write the owl coin. Team banners are produced by make_team_banners.py."""
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    (out / "alysis-owl.svg").write_text(owl_icon(), encoding="utf-8")


if __name__ == "__main__":
    main()
