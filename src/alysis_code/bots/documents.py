"""What an owl can read from the files people give it: images and office documents.

Pure functions over a file's bytes, with no storage, model or network access. The kind
of file is decided from its content, never from its name. File content is untrusted
data: it is parsed, never executed, and every result is bounded. Text comes back in
windows (offset and length in characters, like text files), so a long document is read
in parts without extracting more than the requested part needs.
"""

from __future__ import annotations

import io
import json
import re
import struct
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from xml.etree import ElementTree

from ..documents import PDFIUM_LOCK
from .browser_images import MAX_IMAGE_BYTES, FileImageResult, file_image

# Enough of a file's start to recognise every kind below.
SNIFF_BYTES = 4096
# Images are shown at most this size: legible for text and photos, small for the model.
MAX_IMAGE_SIDE = 1568
MAX_IMAGE_PIXELS = 1_200_000
# Larger sources are refused before decoding (the image library's own bomb limit).
MAX_SOURCE_PIXELS = 89_478_485
# Office packages are zip archives; refuse ones that expand past these before parsing.
MAX_PACKAGE_PARTS = 10_000
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_CONTENT_TYPES_BYTES = 1024 * 1024
# How many page numbers a message lists before summarising the rest.
MAX_LISTED_PAGES = 20

IMAGE_KINDS = frozenset({"png", "jpeg", "gif", "webp"})
LABELS = {
    "png": "PNG",
    "jpeg": "JPEG",
    "gif": "GIF",
    "webp": "WebP",
    "pdf": "PDF",
    "docx": "DOCX",
    "xlsx": "XLSX",
    "pptx": "PPTX",
}
# Main-part content types of the office packages the installed readers open.
OFFICE_MAIN_TYPES = {
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"},
    "xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
        "application/vnd.ms-excel.template.macroEnabled.main+xml",
    },
    "pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
    },
}
CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Streams that an office file protected with a password keeps in its compound container.
ENCRYPTED_PACKAGE_STREAMS = {"EncryptionInfo", "EncryptedPackage"}

DAMAGED = "This file looks damaged or incomplete, so it can't be read. A fresh copy of it may work."
PROTECTED = (
    "This file is protected with a password, so it can't be read. A copy saved without the "
    "password can be read."
)
TOO_LARGE = "This file is too large to read here once it is opened up. A smaller copy can be read."
TOO_MANY_PIXELS = (
    "This image has too many pixels to open safely here. A smaller copy of it can be read."
)
UNAVAILABLE = "Reading this kind of file isn't available in this setup."
IMAGE_NOT_VISIBLE = (
    "This file is an image ({label}, {width}×{height} pixels). The current model can't see "
    "images, so its contents can't be read here; a model that can see images would be able to "
    "read it. Tell the person this plainly."
)
PAGE_NOT_VISIBLE = (
    "The current model can't see images, so page {page} can't be viewed as an image here; a "
    "model that can see images would be able to. The page's text, if it has any, comes from "
    "reading the file without page."
)
PAGE_ONLY_PDF = "page applies only to PDF files, so it was ignored."
FORMULA_NOTE = (
    "Formulas show the result saved in the file; a formula that was never calculated shows as "
    "empty."
)
NO_TEXT_PAGE = "[This page has no text that can be extracted. It may be a scan or a picture.]"


class DocumentError(Exception):
    """A file of a known kind that can't be read. The message is plain and safe to relay."""


@dataclass(frozen=True)
class Picture:
    """An image ready for the model: a bounded PNG or JPEG data URL and its sizes."""

    data_url: str
    width: int
    height: int
    original_width: int
    original_height: int
    frames: int = 1


@dataclass(frozen=True)
class TextWindow:
    text: str
    next_offset: int | None
    # Known only once extraction reached the end of the document.
    total_characters: int | None
    # Pages without extractable text whose marker overlaps this window (PDF only).
    pages_without_text: tuple[int, ...] = ()


@dataclass
class _Spans:
    """Where marked blocks start and stop in the extracted text, recorded as they are made."""

    items: list[tuple[int, int, int]] = field(default_factory=list)

    def within(self, offset: int, end: int) -> tuple[int, ...]:
        return tuple(page for page, start, stop in self.items if start < end and stop > offset)


# ---------------------------------------------------------------- recognising a file


def sniff(head: bytes) -> str | None:
    """The kind of file its first bytes show, or None for anything not handled here."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    # PDF readers accept the header anywhere in the first kilobyte.
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(CFB_SIGNATURE):
        return "cfb"
    return None


def office_kind(data: bytes) -> str | None:
    """'docx', 'xlsx' or 'pptx' from the package's declared main part; None for other zips."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "[Content_Types].xml" not in archive.namelist():
                return None
            types = archive.getinfo("[Content_Types].xml")
            if types.flag_bits & 1:
                raise DocumentError(PROTECTED)
            if types.file_size > MAX_CONTENT_TYPES_BYTES:
                raise DocumentError(DAMAGED)
            root = ElementTree.fromstring(archive.read(types))
            declared = {element.get("ContentType", "") for element in root.iter()}
            kind = next((k for k, mains in OFFICE_MAIN_TYPES.items() if declared & mains), None)
            if kind is None:
                return None
            parts = archive.infolist()
            if any(part.flag_bits & 1 for part in parts):
                raise DocumentError(PROTECTED)
            if (
                len(parts) > MAX_PACKAGE_PARTS
                or sum(part.file_size for part in parts) > MAX_UNPACKED_BYTES
            ):
                raise DocumentError(TOO_LARGE)
            return kind
    except (DocumentError, MemoryError):
        raise
    except Exception:  # noqa: BLE001 - any archive or XML failure means this file can't be read
        raise DocumentError(DAMAGED) from None


def compound_stream_names(data: bytes) -> set[str]:
    """Names in a compound file's directory (the container of password-protected office
    files and of older office formats). Malformed containers give what could be read."""
    if len(data) < 512 or not data.startswith(CFB_SIGNATURE):
        return set()
    (shift,) = struct.unpack_from("<H", data, 30)
    if shift not in (9, 12):
        return set()
    size = 1 << shift
    sectors = max(0, len(data) // size - 1)
    fat_count, directory_start = struct.unpack_from("<II", data, 44)
    difat_start, difat_count = struct.unpack_from("<II", data, 68)

    def sector(number: int) -> bytes:
        return data[(number + 1) * size : (number + 2) * size] if number < sectors else b""

    def words(block: bytes) -> list[int]:
        return list(struct.unpack_from(f"<{len(block) // 4}I", block))

    last = 0xFFFFFFFA  # higher values mark free, end-of-chain or special sectors
    fat_sectors = words(data[76:512])
    number = difat_start
    for _ in range(min(difat_count, sectors)):
        block = sector(number)
        if number > last or not block:
            break
        chain = words(block)
        fat_sectors += chain[:-1]
        number = chain[-1]
    fat: list[int] = []
    for number in [s for s in fat_sectors if s <= last][: min(fat_count, sectors)]:
        fat += words(sector(number))
    names = set()
    number = directory_start
    for _ in range(sectors):
        block = sector(number)
        if number > last or not block:
            break
        for start in range(0, len(block) - 127, 128):
            (length,) = struct.unpack_from("<H", block, start + 64)
            if block[start + 66] in (1, 2) and 2 <= length <= 64:
                names.add(block[start : start + length - 2].decode("utf-16-le", "replace"))
        number = fat[number] if number < len(fat) else last + 1
    return names


# ---------------------------------------------------------------- images


_IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")


def _open_image(data: bytes):
    """The image (header read only) with its upright size and frame count."""
    try:
        from PIL import Image
    except ImportError:
        raise DocumentError(UNAVAILABLE) from None
    try:
        # Only the decoders of the kinds read here ever see the bytes.
        image = Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS)
    except Image.DecompressionBombError:
        raise DocumentError(TOO_MANY_PIXELS) from None
    try:
        width, height = image.size
        if width < 1 or height < 1:
            raise DocumentError(DAMAGED)
        if width * height > MAX_SOURCE_PIXELS:
            raise DocumentError(TOO_MANY_PIXELS)
        if image.getexif().get(0x0112, 1) in (5, 6, 7, 8):
            width, height = height, width  # shown upright, as photo viewers do
        return image, width, height, getattr(image, "n_frames", 1)
    except BaseException:
        image.close()
        raise


def image_size(data: bytes) -> tuple[int, int]:
    """An image's upright width and height, from its header."""
    try:
        image, width, height, _ = _open_image(data)
        image.close()
    except (DocumentError, MemoryError):
        raise
    except Exception:  # noqa: BLE001 - any decoder failure means this file can't be decoded
        raise DocumentError(DAMAGED) from None
    return width, height


def picture(data: bytes) -> Picture:
    """A PNG, JPEG, GIF or WebP file as a bounded image (the first frame of an animation)."""
    try:
        from PIL import ImageOps

        image, width, height, frames = _open_image(data)
        with image:
            # JPEG decodes at a reduced scale when the full size isn't needed.
            image.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            image.load()
            upright = ImageOps.exif_transpose(image)
        url, shown_width, shown_height = fit_image(upright)
    except (DocumentError, MemoryError):
        raise
    except Exception:  # noqa: BLE001 - any decoder failure means this file can't be decoded
        raise DocumentError(DAMAGED) from None
    return Picture(url, shown_width, shown_height, width, height, frames)


def fit_image(image) -> tuple[str, int, int]:
    """A data URL within the model image budget: at most MAX_IMAGE_SIDE and MAX_IMAGE_PIXELS,
    PNG when that fits (sharp text), otherwise JPEG, smaller still when neither fits."""
    from PIL import Image

    if image.mode not in ("RGB", "RGBA", "L", "LA"):
        transparent = image.mode in ("PA", "RGBa", "La") or "transparency" in image.info
        image = image.convert("RGBA" if transparent else "RGB")
    width, height = image.size
    scale = min(
        1.0, MAX_IMAGE_SIDE / max(width, height), (MAX_IMAGE_PIXELS / (width * height)) ** 0.5
    )
    while True:
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        frame = image if size == image.size else image.resize(size, Image.Resampling.LANCZOS)
        frame.info = {}  # no metadata (such as a photo's location) goes to the model
        for kind, encode in (("png", _png), ("jpeg", _jpeg)):
            data = encode(frame)
            if len(data) <= MAX_IMAGE_BYTES:
                return f"data:image/{kind};base64," + _base64(data), size[0], size[1]
        if max(size) <= 64:
            raise DocumentError(DAMAGED)
        scale *= 0.75


def _png(frame) -> bytes:
    out = io.BytesIO()
    frame.save(out, format="PNG")
    return out.getvalue()


def _jpeg(frame) -> bytes:
    from PIL import Image

    if frame.mode in ("RGBA", "LA"):
        rgba = frame.convert("RGBA")
        opaque = Image.new("RGB", rgba.size, "white")
        opaque.paste(rgba, mask=rgba.getchannel("A"))
        frame = opaque
    out = io.BytesIO()
    frame.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _base64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- text windows


def window(
    blocks: Iterable[str], offset: int, length: int, spans: _Spans | None = None
) -> TextWindow:
    """Characters [offset, offset + length) of the joined blocks, consuming no more blocks
    than needed to know whether text follows."""
    end = offset + length
    position = 0
    parts = []
    for block in blocks:
        start = position
        position += len(block)
        if position > offset and start < end:
            parts.append(block[max(0, offset - start) : end - start])
        if position > end:
            return TextWindow("".join(parts), end, None, spans.within(offset, end) if spans else ())
    return TextWindow(
        "".join(parts),
        None,
        position,
        spans.within(offset, min(end, position)) if spans else (),
    )


_CONTROL = dict.fromkeys([*range(9), 11, 12, *range(14, 32), 127])


def clean(text: str) -> str:
    """Line endings as \\n; other control characters (except tab) removed."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\v", "\n").translate(_CONTROL)


def cell_text(value: object) -> str:
    """One table or sheet cell on one line, so rows stay one line with tab-separated cells."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time() else value.isoformat(" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    return re.sub(r"\s*[\t\n\r\v\f]+\s*", " ", clean(str(value))).strip()


# ---------------------------------------------------------------- PDF


def _pdfium():
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise DocumentError(UNAVAILABLE) from None
    return pdfium


def _open_pdf(pdfium, data: bytes):
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        protected = getattr(error, "err_code", None) in (
            pdfium.raw.FPDF_ERR_PASSWORD,
            pdfium.raw.FPDF_ERR_SECURITY,
        )
        raise DocumentError(PROTECTED if protected else DAMAGED) from None


def pdf_text(data: bytes, offset: int, length: int) -> tuple[TextWindow, int]:
    """A window of a PDF's text with a marker before each page, and its page count."""
    pdfium = _pdfium()
    spans = _Spans()
    with PDFIUM_LOCK:  # PDFium is not thread-safe; owls read files on several threads
        pdf = _open_pdf(pdfium, data)
        try:
            count = len(pdf)

            def blocks() -> Iterator[str]:
                position = 0
                for index in range(count):
                    page = pdf[index]
                    try:
                        textpage = page.get_textpage()
                        try:
                            text = clean(textpage.get_text_range()).strip()
                        finally:
                            textpage.close()
                    finally:
                        page.close()
                    block = f"--- Page {index + 1} of {count} ---\n{text or NO_TEXT_PAGE}\n\n"
                    if not text:
                        spans.items.append((index + 1, position, position + len(block)))
                    position += len(block)
                    yield block

            try:
                return window(blocks(), offset, length, spans), count
            except pdfium.PdfiumError:
                raise DocumentError(DAMAGED) from None
        finally:
            pdf.close()


def pdf_page_image(data: bytes, page: int) -> tuple[Picture, int]:
    """One PDF page rendered as a bounded image, and the page count."""
    pdfium = _pdfium()
    with PDFIUM_LOCK:
        pdf = _open_pdf(pdfium, data)
        try:
            count = len(pdf)
            if not 1 <= page <= count:
                raise DocumentError(
                    f"This PDF has {count} page{'' if count == 1 else 's'}, so it has no page {page}."
                )
            try:
                current = pdf[page - 1]
                try:
                    width, height = current.get_size()
                    if not (width > 0 and height > 0):
                        raise DocumentError(DAMAGED)
                    scale = min(
                        4.0,
                        MAX_IMAGE_SIDE / max(width, height),
                        (MAX_IMAGE_PIXELS / (width * height)) ** 0.5,
                    )
                    bitmap = current.render(scale=scale)
                    try:
                        image = bitmap.to_pil().copy()  # the bitmap's buffer is freed below
                    finally:
                        bitmap.close()
                finally:
                    current.close()
            except pdfium.PdfiumError:
                raise DocumentError(DAMAGED) from None
        finally:
            pdf.close()
    url, shown_width, shown_height = fit_image(image)
    return Picture(url, shown_width, shown_height, image.width, image.height), count


# ---------------------------------------------------------------- DOCX

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _w(name: str) -> str:
    return f"{{{_W}}}{name}"


def _paragraph_text(paragraph) -> str:
    """A paragraph's visible text; text boxes inside it are read as their own paragraphs."""
    values = []
    for node in paragraph.iter(_w("t"), _w("tab"), _w("br"), _w("cr")):
        owner = next(node.iterancestors(_w("p")), None)
        if owner is not paragraph:
            continue
        if node.tag == _w("t"):
            values.append(node.text or "")
        elif node.getparent().tag == _w("r"):  # not a tab stop or break setting
            values.append("\t" if node.tag == _w("tab") else "\n")
    return clean("".join(values)).strip()


def _nested_paragraphs(element) -> list:
    """Paragraphs inside text boxes and tables of an element, skipping the duplicate copies
    that files keep for older readers."""
    return [
        p
        for p in element.iter(_w("p"))
        if p is not element and not any(a.tag == f"{{{_MC}}}Fallback" for a in p.iterancestors())
    ]


def _styles(document) -> Callable[[object], tuple[int | None, bool]]:
    """(heading level, is list item) of a paragraph from its own and its style's settings.
    Headings come from outline levels, which a document keeps the same in every language."""
    known: dict[str, tuple[int | None, bool | None, str | None]] = {}
    default = None
    for style in document.styles.element.iter(_w("style")):
        if style.get(_w("type")) != "paragraph":
            continue
        settings = style.find(_w("pPr"))
        based = style.find(_w("basedOn"))
        known[style.get(_w("styleId"))] = (
            _outline(settings),
            _numbered(settings),
            based.get(_w("val")) if based is not None else None,
        )
        if style.get(_w("default")) in ("1", "true", "on"):
            default = style.get(_w("styleId"))

    def describe(paragraph) -> tuple[int | None, bool]:
        settings = paragraph.find(_w("pPr"))
        level, listed = _outline(settings), _numbered(settings)
        chosen = settings.find(_w("pStyle")) if settings is not None else None
        style_id = chosen.get(_w("val")) if chosen is not None else default
        for _ in range(len(known)):  # follow basedOn, never round a loop
            if style_id not in known or (level is not None and listed is not None):
                break
            style_level, style_listed, style_id = known[style_id]
            level = style_level if level is None else level
            listed = style_listed if listed is None else listed
        return (level + 1 if level is not None and 0 <= level < 9 else None), bool(listed)

    return describe


def _outline(settings) -> int | None:
    """The outline level a paragraph or style sets (0 is the top heading level)."""
    outline = settings.find(_w("outlineLvl")) if settings is not None else None
    try:
        return int(outline.get(_w("val"))) if outline is not None else None
    except (TypeError, ValueError):
        return None


def _numbered(settings) -> bool | None:
    """Whether numbering (a list) is set, removed (numbering id 0) or left to the style."""
    numbering = settings.find(_w("numPr")) if settings is not None else None
    if numbering is None:
        return None
    number = numbering.find(_w("numId"))
    return number is None or number.get(_w("val")) != "0"


def _docx_blocks(document) -> Iterator[str]:
    describe = _styles(document)

    def paragraph(p) -> Iterator[str]:
        text = _paragraph_text(p)
        if text:
            level, listed = describe(p)
            if level:
                yield f"\n{'#' * level} {text}\n"
            else:
                yield f"- {text}\n" if listed else f"{text}\n"
        for nested in _nested_paragraphs(p):
            text = _paragraph_text(nested)
            if text:
                yield f"{text}\n"

    def table(grid) -> Iterator[str]:
        yield "\n"
        for row in grid.iter(_w("tr")):
            if next(row.iterancestors(_w("tbl")), None) is not grid:
                continue  # a nested table's rows are read as part of their cell
            cells = []
            for cell in row.findall(_w("tc")):
                texts = (_paragraph_text(p) for p in _nested_paragraphs(cell))
                cells.append(cell_text(" ".join(text for text in texts if text)))
            yield "\t".join(cells).rstrip("\t") + "\n"
        yield "\n"

    def children(element) -> Iterator[str]:
        for child in element:
            if child.tag == _w("p"):
                yield from paragraph(child)
            elif child.tag == _w("tbl"):
                yield from table(child)
            elif child.tag == _w("sdt"):  # content controls hold ordinary body content
                content = child.find(_w("sdtContent"))
                if content is not None:
                    yield from children(content)

    blocks = children(document.element.body)
    first = next(blocks, None)
    if first is not None:
        yield first.lstrip("\n")  # no blank line before the document's first heading or table
        yield from blocks


def docx_text(data: bytes, offset: int, length: int) -> TextWindow:
    """A window of a DOCX document's text: headings marked with #, list items with -,
    table rows as tab-separated cells."""
    try:
        import docx
    except ImportError:
        raise DocumentError(UNAVAILABLE) from None
    try:
        document = docx.Document(io.BytesIO(data))
        return window(_docx_blocks(document), offset, length)
    except (DocumentError, MemoryError):
        raise
    except Exception:  # noqa: BLE001 - any parser failure means this file can't be read
        raise DocumentError(DAMAGED) from None


# ---------------------------------------------------------------- XLSX


def xlsx_text(data: bytes, offset: int, length: int) -> tuple[TextWindow, int]:
    """A window of a workbook's cells, sheet by sheet, and its sheet count. Each non-empty
    row is one line: its row number, then its cells separated by tabs."""
    try:
        import openpyxl
    except ImportError:
        raise DocumentError(UNAVAILABLE) from None
    try:
        # read_only streams rows; data_only gives each formula's saved result.
        book = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True, keep_links=False
        )
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - any parser failure means this file can't be read
        raise DocumentError(DAMAGED) from None
    try:
        sheets = book.worksheets

        def blocks() -> Iterator[str]:
            for number, sheet in enumerate(sheets, 1):
                hidden = "" if sheet.sheet_state == "visible" else " (hidden)"
                yield f"--- Sheet {number} of {len(sheets)}: {cell_text(sheet.title)}{hidden} ---\n"
                # The declared size is not trusted: rows are read as they are stored.
                sheet.reset_dimensions()
                empty = True
                for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                    cells = "\t".join(cell_text(value) for value in row).rstrip("\t")
                    if cells.strip():
                        empty = False
                        yield f"Row {row_number}: {cells}\n"
                yield "[This sheet is empty.]\n\n" if empty else "\n"

        try:
            return window(blocks(), offset, length), len(sheets)
        except (DocumentError, MemoryError):
            raise
        except Exception:  # noqa: BLE001 - any parser failure means this file can't be read
            raise DocumentError(DAMAGED) from None
    finally:
        book.close()


# ---------------------------------------------------------------- PPTX


def _shape_lines(shapes) -> Iterator[str]:
    from pptx.shapes.group import GroupShape

    for shape in shapes:
        if isinstance(shape, GroupShape):
            yield from _shape_lines(shape.shapes)
        elif shape.has_table:
            for row in shape.table.rows:
                yield "\t".join(cell_text(cell.text) for cell in row.cells).rstrip("\t")
        elif shape.has_text_frame:
            text = clean(shape.text_frame.text).strip()
            if text:
                yield text


def pptx_text(data: bytes, offset: int, length: int) -> tuple[TextWindow, int]:
    """A window of a presentation's text with a marker before each slide, its speaker
    notes after it, and the slide count."""
    try:
        import pptx
    except ImportError:
        raise DocumentError(UNAVAILABLE) from None
    try:
        presentation = pptx.Presentation(io.BytesIO(data))
        slides = presentation.slides
        count = len(slides)

        def blocks() -> Iterator[str]:
            for number, slide in enumerate(slides, 1):
                hidden = " (hidden)" if slide.element.get("show") in ("0", "false") else ""
                yield f"--- Slide {number} of {count}{hidden} ---\n"
                for line in _shape_lines(slide.shapes):
                    yield line + "\n"
                if slide.has_notes_slide:
                    frame = slide.notes_slide.notes_text_frame
                    notes = clean(frame.text).strip() if frame is not None else ""
                    if notes:
                        yield f"Notes: {notes}\n"
                yield "\n"

        return window(blocks(), offset, length), count
    except (DocumentError, MemoryError):
        raise
    except Exception:  # noqa: BLE001 - any parser failure means this file can't be read
        raise DocumentError(DAMAGED) from None


# ---------------------------------------------------------------- read_file


def read(
    name: str,
    data: bytes,
    kind: str,
    *,
    offset: int,
    length: int,
    page: int | None,
    image_input: bool,
) -> dict | FileImageResult | None:
    """read_file's result for a file whose bytes were recognised by sniff().

    An image goes to a model that can see images as a FileImageResult; anything else is a
    text result. None means the file is not a kind read here.
    """
    try:
        if kind == "cfb":
            if ENCRYPTED_PACKAGE_STREAMS <= compound_stream_names(data):
                raise DocumentError(PROTECTED)
            return None
        if kind == "zip":
            kind = office_kind(data)
            if kind is None:
                return None
        if kind in IMAGE_KINDS:
            return _image_result(name, data, kind, image_input, page)
        if kind == "pdf" and page is not None:
            return _page_result(name, data, page, image_input)
        return _text_result(name, data, kind, offset, length, page, image_input)
    except DocumentError as error:
        label = {"format": LABELS[kind]} if kind in LABELS else {}
        return {"name": name, **label, "content": None, "message": str(error)}


def _source(name: str, page: int | None = None, pages: int | None = None) -> str:
    quoted = json.dumps(name[:200], ensure_ascii=False)
    return quoted if page is None else f"{quoted} (page {page} of {pages})"


def _image_result(name, data, kind, image_input, page) -> dict | FileImageResult:
    if not image_input:
        width, height = image_size(data)
        message = IMAGE_NOT_VISIBLE.format(label=LABELS[kind], width=width, height=height)
        return {
            "name": name,
            "format": LABELS[kind],
            "width": width,
            "height": height,
            "content": None,
            "message": message,
        }
    image = picture(data)
    receipt = {
        "name": name,
        "format": LABELS[kind],
        "width": image.original_width,
        "height": image.original_height,
        "shown_width": image.width,
        "shown_height": image.height,
    }
    notes = [PAGE_ONLY_PDF] if page is not None else []
    if image.frames > 1:
        notes.append(f"This image is animated; only its first of {image.frames} frames is shown.")
    if notes:
        receipt["note"] = " ".join(notes)
    return file_image(receipt, image.data_url, _source(name), image.width, image.height)


def _page_result(name, data, page, image_input) -> dict | FileImageResult:
    if not image_input:
        return {
            "name": name,
            "format": "PDF",
            "page": page,
            "content": None,
            "message": PAGE_NOT_VISIBLE.format(page=page),
        }
    image, pages = pdf_page_image(data, page)
    receipt = {
        "name": name,
        "format": "PDF",
        "page": page,
        "pages": pages,
        "shown_width": image.width,
        "shown_height": image.height,
    }
    return file_image(
        receipt, image.data_url, _source(name, page, pages), image.width, image.height
    )


def _text_result(name, data, kind, offset, length, page, image_input) -> dict:
    counts = {}
    if kind == "pdf":
        text, counts["pages"] = pdf_text(data, offset, length)
    elif kind == "docx":
        text = docx_text(data, offset, length)
    elif kind == "xlsx":
        text, counts["sheets"] = xlsx_text(data, offset, length)
    else:
        text, counts["slides"] = pptx_text(data, offset, length)
    notes = []
    if text.next_offset is not None:
        notes.append(
            f"This is part of the text (characters {offset} to {text.next_offset}). Call "
            f"read_file again with offset={text.next_offset} to read on."
        )
    elif offset and not text.text:
        notes.append(
            f"offset {offset} is past the end of the text ({text.total_characters} characters)."
        )
    if text.pages_without_text:
        pages = _page_list(text.pages_without_text)
        if image_input:
            notes.append(
                f"Pages without extractable text: {pages}. They may be scans or pictures; to "
                "look at one, call read_file with this file_id and page set to its number."
            )
        else:
            notes.append(
                f"Pages without extractable text: {pages}. They may be scans or pictures. The "
                "current model can't see images, so they can't be read here; a model that can "
                "see images would be able to read them."
            )
    if kind == "xlsx":
        notes.append(FORMULA_NOTE)
    if page is not None:
        notes.append(PAGE_ONLY_PDF)
    result = {
        "name": name,
        "format": LABELS[kind],
        **counts,
        "content": text.text,
        "next_offset": text.next_offset,
    }
    if text.total_characters is not None:
        result["total_characters"] = text.total_characters
    if notes:
        result["message"] = " ".join(notes)
    return result


def _page_list(pages: tuple[int, ...]) -> str:
    shown = ", ".join(str(page) for page in pages[:MAX_LISTED_PAGES])
    more = len(pages) - MAX_LISTED_PAGES
    return f"{shown} and {more} more" if more > 0 else shown
