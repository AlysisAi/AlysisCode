"""Alysis team-of-owls banners, drawn in square pixels like make_brand_svgs.py.

No fonts: every letter is a 5x7 pixel glyph, so text stays crisp in any renderer.

Usage, from the repository root:

    python3 scripts/brand/make_team_banners.py docs/assets/brand

Writes the files the README and social preview use: alysis-banner-light.svg,
alysis-banner-dark.svg, og-image-1200x630.svg, owl-cast.svg, and
x-banner-1500x500.svg. Render a PNG with cairosvg or rsvg-convert when a raster
file is needed. GitHub's social preview uses og-image-1200x630.png.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

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


# Split ink cells into body (reachable from outside) and features (eyes, beak).
def _classify():
    H, W = len(OWL), len(OWL[0])
    seen, stack = set(), []
    for y in range(H):
        for x in range(W):
            if (x in (0, W - 1) or y in (0, H - 1)) and OWL[y][x] != "o":
                stack.append((x, y))
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (0 <= x < W and 0 <= y < H) or OWL[y][x] == "o":
            continue
        seen.add((x, y))
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    body, eyes, beak, face = [], [], [], []
    for y in range(H):
        for x in range(W):
            c = OWL[y][x]
            if c == "o":
                face.append((x, y))
            elif c == "#":
                if (x, y) in seen:
                    body.append((x, y))
                elif y < 10:
                    eyes.append((x, y))
                else:
                    beak.append((x, y))
    occupied = set(body + eyes + beak + face)
    outline = sorted(
        {(x + dx, y + dy) for x, y in occupied for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
        - occupied
    )
    return body, eyes, beak, face, outline


BODY, EYES, BEAK, FACE, OUTLINE = _classify()

FONT = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    ".": [".", ".", ".", ".", ".", ".", "#"],
    "$": ["..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    " ": ["...", "...", "...", "...", "...", "...", "..."],
}


def cells_to_path(cells, x0, y0, size):
    rows = {}
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


def text_cells(text):
    cells, cursor = [], 0
    for i, ch in enumerate(text):
        g = FONT[ch]
        for cy, row in enumerate(g):
            for cx, bit in enumerate(row):
                if bit == "#":
                    cells.append((cursor + cx, cy))
        cursor += len(g[0]) + (1 if i < len(text) - 1 else 0)
    return cells, cursor


def text_w(text, size):
    return text_cells(text)[1] * size


def text(t, x, y, size, fill):
    cells, w = text_cells(t)
    return f'<path fill="{fill}" d="{cells_to_path(cells, x, y, size)}"/>'


def wordmark(x, y, size, ink, accent):
    w = text_w("ALYSIS", size)
    return (
        text("ALYSIS", x, y, size, ink)
        + f'<rect x="{x + w + size:g}" y="{y + 6 * size:g}" width="{size:g}" height="{size:g}" fill="{accent}"/>',
        w + 2 * size,
    )


# The cast. Colors stay in the Alysis palette family.
CAST = {
    "ALY": {
        "body": "#0E0F0E",
        "face": "#FFFFFF",
        "eyes": "#0E0F0E",
        "beak": "#0E0F0E",
        "outline": None,
    },
    "BIT": {
        "body": "#0C8050",
        "face": "#FFFFFF",
        "eyes": "#0E0F0E",
        "beak": "#0E0F0E",
        "outline": None,
    },
    "QUILL": {
        "body": "#C97A2B",
        "face": "#FFFFFF",
        "eyes": "#0E0F0E",
        "beak": "#0E0F0E",
        "outline": None,
    },
    "LUNA": {
        "body": "#3A3E39",
        "face": "#E8E5DA",
        "eyes": "#3CCB7F",
        "beak": "#0E0F0E",
        "outline": None,
    },
}


def owl(name, x0, y0, size, dark=False):
    c = dict(CAST[name])
    if dark and name == "BIT":
        c["body"] = "#3CCB7F"
    if dark and name == "QUILL":
        c["body"] = "#E39A45"
    if dark and name == "LUNA":
        c["outline"] = "#E8E5DA"
    out = ""
    if c["outline"]:
        out += f'<path fill="{c["outline"]}" d="{cells_to_path(OUTLINE, x0, y0, size)}"/>'
    for part, cells in (("face", FACE), ("body", BODY), ("eyes", EYES), ("beak", BEAK)):
        out += f'<path fill="{c[part]}" d="{cells_to_path(cells, x0, y0, size)}"/>'
    return out


def moon(cx, cy, r, disc, bead, beads=48, bead_size=6):
    out = [f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{disc}"/>']
    ring = r + 16
    for i in range(beads):
        a = 2 * math.pi * i / beads
        out.append(
            f'<rect x="{cx + ring * math.cos(a) - bead_size / 2:.1f}" y="{cy + ring * math.sin(a) - bead_size / 2:.1f}" width="{bead_size:g}" height="{bead_size:g}" fill="{bead}"/>'
        )
    return f'<g shape-rendering="geometricPrecision">{"".join(out)}</g>'


def branch(x, y, w, size, fill):
    # a pixel branch: a long bar with a few twigs
    s = f'<rect x="{x}" y="{y}" width="{w}" height="{size * 2}" fill="{fill}"/>'
    for i, tx in enumerate(range(int(x + 30), int(x + w - 20), 140)):
        dy = -size if i % 2 else size * 2
        s += f'<rect x="{tx}" y="{y + dy}" width="{size * 3}" height="{size}" fill="{fill}"/>'
    return s


def stars(pts, fill):
    return "".join(
        f'<rect x="{x}" y="{y}" width="{s}" height="{s}" fill="{fill}" fill-opacity="{o}"/>'
        for x, y, s, o in pts
    )


ORDER = ["BIT", "ALY", "LUNA", "QUILL"]


def svg(W, H, label, body):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="{label}" shape-rendering="crispEdges"><title>{label}</title>{body}</svg>\n'
    )


def x_banner():
    W, H = 1500, 500
    bg, word, claim, accent, sand = "#111311", "#F2F0E8", "#BDBBB2", "#3CCB7F", "#CFCABB"
    b = f'<rect width="{W}" height="{H}" fill="{bg}"/>'
    b += stars(
        [
            (60, 50, 4, 0.8),
            (420, 40, 4, 0.5),
            (700, 70, 6, 0.8),
            (760, 300, 4, 0.5),
            (980, 40, 4, 0.6),
            (1440, 60, 6, 0.8),
            (1460, 300, 4, 0.5),
            (560, 330, 4, 0.4),
            (250, 60, 4, 0.5),
            (1300, 30, 4, 0.6),
        ],
        "#E9E6DA",
    )
    x0 = 110
    wm, _ = wordmark(x0, 62, 14, word, accent)
    b += wm
    b += text("A TEAM OF OWLS", x0, 186, 6, word)
    b += text("FOR ALL YOUR WORK.", x0, 240, 6, word)
    b += text("ANY AI MODEL. FROM $1.", x0, 300, 4, claim)
    size = 7
    ow = 21 * size
    gap = 18
    total = 4 * ow + 3 * gap
    ox = 1440 - total
    base = 372
    b += moon(ox + total / 2, 230, 170, "#E9E6DA", "#3A3E39", beads=56)
    b += branch(ox - 40, base, total + 70, size, "#6B6455")
    for i, n in enumerate(ORDER):
        x = ox + i * (ow + gap)
        b += owl(n, x, base - ow + size, size, dark=True)
        tw = text_w(n, 3)
        b += text(n, x + ow / 2 - tw / 2, base + 34, 3, sand)
    return svg(
        W,
        H,
        "Alysis: four pixel owls, Bit, Aly, Luna and Quill, on a branch under the moon, next to the words: A team of owls for all your work. Any AI model. From $1.",
        b,
    )


THEMES = {
    "light": dict(
        bg="#F7F7F2",
        border="#E2E0D6",
        disc="#E8E5DA",
        bead="#CFCABB",
        word="#0E0F0E",
        claim="#3F423D",
        accent="#0C8050",
        star=None,
        twig="#B9B4A4",
        dark=False,
    ),
    "dark": dict(
        bg="#111311",
        border="#232622",
        disc="#E9E6DA",
        bead="#3A3E39",
        word="#F2F0E8",
        claim="#BDBBB2",
        accent="#3CCB7F",
        star="#E9E6DA",
        twig="#6B6455",
        dark=True,
    ),
}


def readme_banner(theme):
    t = THEMES[theme]
    W, H = 960, 320
    b = f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="20" fill="{t["bg"]}" stroke="{t["border"]}" stroke-width="2"/>'
    if t["star"]:
        b += stars(
            [
                (40, 40, 4, 0.9),
                (380, 30, 4, 0.5),
                (900, 40, 6, 0.9),
                (920, 270, 4, 0.6),
                (44, 276, 4, 0.6),
                (600, 290, 4, 0.5),
            ],
            t["star"],
        )
    size = 4
    ow = 21 * size
    gap = 8
    total = 4 * ow + 3 * gap
    ox = 50
    base = 222
    b += moon(ox + total / 2, 150, 104, t["disc"], t["bead"], beads=44)
    b += branch(ox - 20, base, total + 40, size, t["twig"])
    for i, n in enumerate(ORDER):
        b += owl(n, ox + i * (ow + gap), base - ow + size, size, dark=t["dark"])
    x0 = 450
    wm, _ = wordmark(x0, 64, 11, t["word"], t["accent"])
    b += wm
    b += text("A TEAM OF OWLS", x0, 178, 5, t["word"])
    b += text("FOR ALL YOUR WORK.", x0, 224, 5, t["word"])
    return svg(
        W,
        H,
        "Alysis: four pixel owls on a branch in front of the moon, next to the words: A team of owls for all your work.",
        b,
    )


def og_image():
    t = THEMES["light"]
    W, H = 1200, 630
    b = f'<rect width="{W}" height="{H}" fill="{t["bg"]}"/>'
    wm, wmw = wordmark(0, 0, 10, t["word"], t["accent"])
    b += wordmark((W - wmw) / 2, 60, 10, t["word"], t["accent"])[0]
    size = 6
    ow = 21 * size
    gap = 20
    total = 4 * ow + 3 * gap
    ox = (W - total) / 2
    base = 380
    b += moon(W / 2, 282, 118, t["disc"], t["bead"], beads=48)
    b += branch(ox - 30, base, total + 60, size, t["twig"])
    for i, n in enumerate(ORDER):
        b += owl(n, ox + i * (ow + gap), base - ow + size, size)
    line = "A TEAM OF OWLS FOR ALL YOUR WORK."
    b += text(line, (W - text_w(line, 5)) / 2, 450, 5, t["word"])
    sub = "ANY AI MODEL. FROM $1."
    b += text(sub, (W - text_w(sub, 3)) / 2, 520, 3, t["claim"])
    return svg(W, H, "Alysis: a team of owls for all your work. Any AI model. From $1.", b)


def cast_sheet():
    W, H = 1200, 420
    b = f'<rect width="{W}" height="{H}" fill="#F7F7F2"/>'
    size = 10
    ow = 210
    gap = 70
    total = 4 * ow + 3 * gap
    ox = (W - total) / 2
    for i, n in enumerate(ORDER):
        x = ox + i * (ow + gap)
        b += owl(n, x, 50, size)
        b += text(n, x + ow / 2 - text_w(n, 5) / 2, 320, 5, "#0E0F0E")
    return svg(W, H, "The Alysis owl cast: Bit, Aly, Luna and Quill", b)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "x-banner-1500x500.svg": x_banner(),
        "alysis-banner-light.svg": readme_banner("light"),
        "alysis-banner-dark.svg": readme_banner("dark"),
        "og-image-1200x630.svg": og_image(),
        "owl-cast.svg": cast_sheet(),
    }
    for name, contents in files.items():
        (out / name).write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    main()
