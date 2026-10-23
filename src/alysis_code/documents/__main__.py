"""Create/review text-led PDFs through the ordinary authorized shell tool.

No downloads, subprocesses, external assets or hidden dependency installation.
The helper's paths must stay in the selected workspace (cwd by default).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

MAX_SOURCE_BYTES = 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PAGES = 50


def _path(root: Path, value: str) -> Path:
    candidate = Path(os.path.abspath(root / value))
    if not candidate.is_relative_to(root) or candidate == root:
        raise ValueError("Document paths must stay inside the workspace.")
    for part in [candidate, *candidate.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("Document paths may not contain symbolic links.")
    return candidate


def _font_setup():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    names = []
    for suffix in ("Regular", "Bold", "Italic", "BoldItalic"):
        name = f"AlysisDocument{suffix}"
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(
                TTFont(name, str(Path(__file__).parent / "fonts" / f"LiberationSans-{suffix}.ttf"))
            )
        names.append(name)
    pdfmetrics.registerFontFamily(
        names[0], normal=names[0], bold=names[1], italic=names[2], boldItalic=names[3]
    )
    return names


def _inline(tokens, supported: set[int]) -> str:
    parts = []
    for token in tokens:
        kind = token.type
        if kind in {"text", "code_inline"}:
            if any(0x0590 <= ord(c) <= 0x109F or 0x1780 <= ord(c) <= 0x18AF for c in token.content):
                raise ValueError(
                    "This text requires complex-script shaping. Use a renderer with shaping support."
                )
            missing = sorted(
                {ord(c) for c in token.content if not c.isspace() and ord(c) not in supported}
            )
            if missing:
                raise ValueError(
                    "Unsupported font glyphs: " + ", ".join(f"U+{c:04X}" for c in missing[:12])
                )
            value = html.escape(token.content)
            if kind == "code_inline":
                value = value.replace(" ", "&nbsp;")
            parts.append(
                f'<font backColor="#eef1f4">{value}</font>' if kind == "code_inline" else value
            )
        elif kind in {"softbreak", "hardbreak"}:
            parts.append(" " if kind == "softbreak" else "<br/>")
        elif kind in {"strong_open", "strong_close", "em_open", "em_close"}:
            parts.append(
                {
                    "strong_open": "<b>",
                    "strong_close": "</b>",
                    "em_open": "<i>",
                    "em_close": "</i>",
                }[kind]
            )
        elif kind == "link_open":
            href = token.attrGet("href") or ""
            if urlsplit(href).scheme.lower() not in {"http", "https", "mailto"}:
                raise ValueError("PDF links must use http, https or mailto URLs.")
            parts.append(f'<link href="{html.escape(href, quote=True)}" color="#174e73">')
        elif kind == "link_close":
            parts.append("</link>")
        else:
            raise ValueError(
                f"Unsupported Markdown inline content: {kind}. Use a different renderer for images or complex layout."
            )
    return "".join(parts)


def _story(markdown: str):
    from markdown_it import MarkdownIt
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.platypus import HRFlowable, LongTable, Paragraph, Spacer, TableStyle

    fonts = _font_setup()
    supported = set.intersection(
        *(set(pdfmetrics.getFont(name).face.charToGlyph) for name in fonts)
    )
    body = ParagraphStyle(
        "Body",
        fontName=fonts[0],
        fontSize=10.5,
        leading=15,
        textColor=colors.HexColor("#17252f"),
        spaceAfter=8,
        splitLongWords=True,
    )
    headings = {
        f"h{level}": ParagraphStyle(
            f"Heading{level}",
            parent=body,
            fontName=fonts[1],
            fontSize={1: 24, 2: 16, 3: 12}.get(level, 11),
            leading={1: 29, 2: 21, 3: 17}.get(level, 16),
            spaceBefore=14,
            spaceAfter=9,
            keepWithNext=True,
        )
        for level in range(1, 7)
    }
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(markdown)
    story = []
    lists = []
    bullet = None
    style = body
    table_rows = None
    row = None
    for token in tokens:
        kind = token.type
        if kind == "heading_open":
            style = headings[token.tag]
        elif kind == "paragraph_open":
            style = (
                ParagraphStyle(
                    "ListBody",
                    parent=body,
                    leftIndent=16 * len(lists),
                    bulletIndent=16 * max(0, len(lists) - 1),
                )
                if lists
                else body
            )
        elif kind == "inline":
            paragraph = Paragraph(
                _inline(token.children or [], supported), style, bulletText=bullet
            )
            bullet = None
            if row is not None:
                row.append(paragraph)
            else:
                story.append(paragraph)
        elif kind in {"heading_close", "paragraph_close", "th_close", "td_close"}:
            style = body
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append(None if kind == "bullet_list_open" else int(token.attrGet("start") or 1))
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif kind == "list_item_open":
            if lists[-1] is None:
                bullet = "•"
            else:
                bullet = str(lists[-1]) + "."
                lists[-1] += 1
        elif kind in {
            "list_item_close",
            "blockquote_open",
            "blockquote_close",
            "thead_open",
            "thead_close",
            "tbody_open",
            "tbody_close",
        }:
            pass
        elif kind == "table_open":
            table_rows = []
        elif kind == "tr_open":
            row = []
        elif kind in {"th_open", "td_open"}:
            style = ParagraphStyle(
                "Cell",
                parent=body,
                fontName=fonts[1] if kind == "th_open" else fonts[0],
                fontSize=9,
                leading=13,
                spaceAfter=0,
            )
        elif kind == "tr_close":
            table_rows.append(row)
            row = None
        elif kind == "table_close":
            if len(table_rows[0]) > 8:
                raise ValueError("Tables support at most eight columns. Split wide tables.")
            # A4 page width less two 54pt margins; wrapped paragraphs use exact font metrics.
            # SimpleDocTemplate's frame also has 6pt padding on each side.
            width = 595.2755905511812 - 108 - 12
            table = LongTable(
                table_rows,
                colWidths=[width / len(table_rows[0])] * len(table_rows[0]),
                repeatRows=1,
                splitInRow=1,
                hAlign="LEFT",
            )
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7eef3")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cad3da")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                )
            )
            story.extend([table, Spacer(1, 10)])
            table_rows = None
        elif kind == "hr":
            story.append(
                HRFlowable(
                    width="100%", color=colors.HexColor("#cad3da"), spaceBefore=8, spaceAfter=10
                )
            )
        elif kind in {"fence", "code_block"}:
            # Escape every code line, and let Paragraph wrap long lines to avoid page overflow.
            for line in token.content.splitlines():
                from markdown_it.token import Token

                text = Token("code_inline", "", 0)
                text.content = line.expandtabs(4) or " "
                story.append(
                    Paragraph(
                        _inline([text], supported),
                        ParagraphStyle(
                            "Code",
                            parent=body,
                            fontSize=9,
                            leading=12,
                            spaceAfter=0,
                            backColor=colors.HexColor("#eef1f4"),
                        ),
                    )
                )
            story.append(Spacer(1, 8))
        else:
            raise ValueError(f"Unsupported Markdown block: {kind}.")
    if not story:
        raise ValueError("The Markdown source has no content.")
    return story


def _make_pdf(markdown: str, output: Path, title: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.platypus.doctemplate import LayoutError

    def footer(canvas, document):
        if document.page > MAX_PAGES:
            raise ValueError(f"PDF exceeds {MAX_PAGES} pages. Split the document.")
        canvas.saveState()
        canvas.setFont("AlysisDocumentRegular", 9)
        canvas.setFillColorRGB(0.35, 0.4, 0.45)
        canvas.drawRightString(A4[0] - 54, 30, str(document.page))
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=54,
        leftMargin=54,
        topMargin=54,
        bottomMargin=54,
        title=title,
        author="",
        pageCompression=1,
    )
    try:
        doc.build(_story(markdown), onFirstPage=footer, onLaterPages=footer)
    except LayoutError as exc:
        raise ValueError(
            "Content cannot fit the PDF layout. Split oversized blocks or tables."
        ) from exc


def _render(source: Path, destination: Path, *, check_content: bool = False) -> dict:
    import pypdfium2 as pdfium

    if source.stat().st_size > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the 50 MiB review limit.")
    pages = []
    with pdfium.PdfDocument(source) as pdf:
        if not 1 <= len(pdf) <= MAX_PAGES:
            raise ValueError(f"PDF must contain 1–{MAX_PAGES} pages.")
        for index in range(len(pdf)):
            with closing(pdf[index]) as page:
                width, height = page.get_size()
                if not 1 <= width <= 1440 or not 1 <= height <= 1440:
                    raise ValueError("Page dimensions exceed the bounded preview size (20 inches).")
                with closing(page.get_textpage()) as textpage:
                    text = textpage.get_text_range()
                    outside = 0
                    outside_content = 0
                    content_characters = 0
                    for char in range(textpage.count_chars()):
                        if textpage.get_text_range(char, 1).isspace():
                            continue
                        left, bottom, right, top = textpage.get_charbox(char)
                        if left < -1 or bottom < -1 or right > width + 1 or top > height + 1:
                            outside += 1
                        # The generated page number is below the content frame.
                        if check_content and top > 45:
                            content_characters += 1
                            if left < 59 or right > width - 59 or bottom < 53 or top > height - 53:
                                outside_content += 1
                filename = f"page-{index + 1:03}.png"
                with closing(page.render(scale=1.5)) as bitmap:
                    image = bitmap.to_pil()
                    image.save(destination / filename)
                    image.close()
                pages.append(
                    {
                        "page": index + 1,
                        "image": filename,
                        "text_characters": len(text),
                        "characters_outside_page": outside,
                        "content_characters": content_characters if check_content else None,
                        "characters_outside_content_frame": outside_content
                        if check_content
                        else None,
                    }
                )
    return {
        "page_count": len(pages),
        "pages": pages,
        "visual_review": "not_performed",
        "next": "Inspect every page image with asset_view; text extraction and rendering do not prove layout quality.",
    }


def run(args) -> dict:
    root = Path(args.workspace).resolve(strict=True)
    source = _path(root, args.source)
    preview = _path(root, args.preview_dir)
    if not source.is_file():
        raise ValueError("Source must be an existing workspace file.")
    if source.stat().st_nlink != 1:
        raise ValueError("Document source must not be a hard link.")
    if preview.exists():
        raise ValueError("Use a new preview directory for each render/revision.")
    output = _path(root, args.output) if args.command == "create" else None
    if output is not None:
        if output == source:
            raise ValueError("PDF output must differ from the editable source.")
        if output.suffix.lower() != ".pdf":
            raise ValueError("PDF output must use the .pdf extension.")
        if output.exists() and not args.overwrite:
            raise ValueError("PDF already exists; use --overwrite only for an authorized revision.")
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("Markdown exceeds the 1 MiB limit.")
        markdown = source.read_text(encoding="utf-8")
    # Temporary files are private to this invocation and are never passed to a model.
    with tempfile.TemporaryDirectory(prefix=".alysis-pdf-", dir=root) as temp:
        stage = Path(temp)
        pdf = stage / "document.pdf" if output is not None else source
        if output is not None:
            _make_pdf(markdown, pdf, args.title or source.stem)
        previews = stage / "previews"
        previews.mkdir()
        try:
            result = _render(pdf, previews, check_content=output is not None)
        except RuntimeError as exc:
            raise ValueError("PDF rendering failed; no PDF was published.") from exc
        if output is not None and (
            any(
                p["characters_outside_page"] or p["characters_outside_content_frame"]
                for p in result["pages"]
            )
            or any(not p["content_characters"] for p in result["pages"])
        ):
            raise ValueError("PDF validation found empty or clipped text. No PDF was published.")
        # Recheck paths after rendering before publishing, including parent symlinks.
        _path(root, str(preview))
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.mkdir()  # exclusive: never replace someone else's existing previews
        for file in previews.iterdir():
            shutil.copyfile(file, preview / file.name)
        if output is not None:
            _path(root, str(output))
            output.parent.mkdir(parents=True, exist_ok=True)
            if args.overwrite:
                os.replace(pdf, output)
            else:
                # Same-workspace staging allows exclusive, atomic publication.
                # A pre-existing output is never truncated, including on races.
                os.link(pdf, output)
        for page in result["pages"]:
            page["image"] = str((preview / page["image"]).relative_to(root))
        result.update(
            {
                "status": "created" if output else "rendered",
                "pdf": str((output or source).relative_to(root)),
                "editable_source": str(source.relative_to(root)) if output else None,
            }
        )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a PDF with page previews from text-led Markdown, or render an existing PDF. No converter discovery or downloads."
    )
    parser.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="Authorized workspace; paths must stay within it (default cwd).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for action in ("create", "render"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--source", required=True)
        cmd.add_argument(
            "--preview-dir",
            required=True,
            help="New scratch directory for PNGs; never an existing directory.",
        )
        if action == "create":
            cmd.add_argument("--output", required=True)
            cmd.add_argument("--title", default="")
            cmd.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False))
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
