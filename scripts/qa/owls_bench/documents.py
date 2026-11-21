"""Open the files a run produced and measure them, with the standard library only.

Office files (xlsx, docx, pptx) are zip packages of XML parts. Each is opened with
``zipfile`` and read with ``xml.etree``; elements are matched by their local name, so
both the transitional and the strict OOXML namespaces work. A PDF is checked by its
header and its page tree is counted conservatively. Nothing here runs or renders a
document: it only reads the parts it needs, within fixed size bounds.
"""

from __future__ import annotations

import codecs
import csv
import io
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PARTS = 10_000
MAX_CELLS = 500_000
MAX_INFLATED_BYTES = 32 * 1024 * 1024
TEXT_LIMIT = 400_000

KINDS = ("xlsx", "docx", "pptx", "pdf", "csv", "text", "image", "unknown")
_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text", ".json", ".html", ".htm", ".xml"}
_CSV_SUFFIXES = {".csv", ".tsv"}
# Links that address a person rather than a page: neither sources nor broken links.
CONTACT_SCHEMES = frozenset({"mailto", "tel", "sms"})
WEB_SCHEMES = frozenset({"http", "https"})


class DocumentError(ValueError):
    """The file does not open as the kind of document it claims to be."""


@dataclass
class Cell:
    text: str = ""
    number: float | None = None
    decimals: int | None = None
    formula: str | None = None
    ref: str | None = None

    @property
    def empty(self) -> bool:
        return self.number is None and not self.text.strip() and not self.formula


@dataclass
class Table:
    name: str
    rows: list[list[Cell]] = field(default_factory=list)

    def nonempty_rows(self) -> list[list[Cell]]:
        return [row for row in self.rows if any(not cell.empty for cell in row)]

    @property
    def data_rows(self) -> int:
        """Rows under the header: every non-empty row but the first."""
        return max(0, len(self.nonempty_rows()) - 1)

    @property
    def header_width(self) -> int:
        rows = self.nonempty_rows()
        return sum(not cell.empty for cell in rows[0]) if rows else 0

    def by_ref(self) -> dict[str, Cell]:
        return {cell.ref.upper(): cell for row in self.rows for cell in row if cell.ref}

    def shape(self) -> dict:
        return {
            "name": self.name,
            "data_rows": self.data_rows,
            "header_width": self.header_width,
        }


@dataclass
class Inspection:
    """What one produced file is and what it holds."""

    name: str
    kind: str
    opens: bool
    size_bytes: int = 0
    error: str | None = None
    tables: list[Table] = field(default_factory=list)
    paragraphs: int = 0
    slides: int | None = None
    pages: int | None = None
    notes: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    text: str = ""
    has_chart: bool = False
    has_image: bool = False

    def summary(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "opens": self.opens,
            "size_bytes": self.size_bytes,
            "error": self.error,
            "tables": [table.shape() for table in self.tables],
            "paragraphs": self.paragraphs,
            "slides": self.slides,
            "pages": self.pages,
            "notes": list(self.notes),
            "links": len(self.links),
            "has_chart": self.has_chart,
            "has_image": self.has_image,
        }


# --- Links -------------------------------------------------------------------------------

# A scheme followed by "://", then anything up to whitespace, quotes, angle brackets, the CJK
# punctuation and full-width blocks, or brackets. Non-ASCII letters stay: many sites show
# their addresses decoded, and those are distinct pages.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,20}://[^\s<>\"'`　-〿＀-￯\[\]{}|\\^]+")
_TRAILING = ".,;:!?*_~'\"»”’"


def find_urls(text: str) -> list[str]:
    """Every scheme://address written in the text, without the punctuation that ends a
    sentence or closes a bracket around it."""
    found = []
    for match in _URL.finditer(text or ""):
        url = match.group(0)
        while url:
            if url[-1] in _TRAILING:
                url = url[:-1]
            elif url.endswith(")") and url.count("(") < url.count(")"):
                url = url[:-1]
            else:
                break
        if "://" in url and not url.endswith("://"):
            found.append(url)
    return found


def link_scheme(link: str) -> str:
    try:
        return urlsplit(link.strip()).scheme.lower()
    except ValueError:
        return ""


def normalize_url(link: str) -> str | None:
    """One spelling per web page: lower-case scheme and host, no fragment, no final slash."""
    try:
        parts = urlsplit(link.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in WEB_SCHEMES or not host:
        return None
    netloc = host if port is None else f"{host}:{port}"
    path = unquote(parts.path).rstrip("/")
    query = f"?{unquote(parts.query)}" if parts.query else ""
    return f"{parts.scheme.lower()}://{netloc}{path}{query}"


def site_of(link: str) -> str | None:
    """The site a web link belongs to: its host without a leading "www."."""
    try:
        host = (urlsplit(link.strip()).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def count_sources(links: list[str]) -> dict:
    """Distinct web pages and sites among links, and the links that are not web links."""
    urls: set[str] = set()
    hosts: set[str] = set()
    not_web: list[str] = []
    for link in links:
        scheme = link_scheme(link)
        if scheme in CONTACT_SCHEMES:
            continue
        url = normalize_url(link)
        if url is None:
            not_web.append(link)
            continue
        urls.add(url)
        site = site_of(link)
        if site:
            hosts.add(site)
    return {
        "links": len(links),
        "distinct_urls": len(urls),
        "distinct_hosts": len(hosts),
        "hosts": sorted(hosts),
        "not_web": not_web[:20],
        "not_web_count": len(not_web),
    }


# --- Numbers -----------------------------------------------------------------------------

_GROUP_MARKS = "  '’"
_NUMBER_RUN = re.compile(r"[-+−]?\d[\d.,  '’]*")
_SPACED_NUMBER = re.compile(r"[-+−]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?")


def _number(digits: str, decimal_mark: str | None, sign: str) -> tuple[float, int] | None:
    """The value of a digit run read with one decimal mark; groups of three otherwise."""
    if decimal_mark is not None and digits.count(decimal_mark) > 1:
        return None
    if decimal_mark is not None and decimal_mark in digits:
        whole, fraction = digits.rsplit(decimal_mark, 1)
    else:
        whole, fraction = digits, ""
    groups = re.split(r"[.,  '’ ]", whole)
    if len(groups) > 1 and (
        not 1 <= len(groups[0]) <= 3 or any(len(group) != 3 for group in groups[1:])
    ):
        return None
    whole = "".join(groups)
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        return None
    try:
        value = Decimal(whole + ("." + fraction if fraction else ""))
    except InvalidOperation:
        return None
    if sign in ("-", "−"):
        value = -value
    return float(value), len(fraction)


def number_readings(token: str) -> list[tuple[float, int]]:
    """Every way a written number can be read, as (value, decimals).

    "1.234,56" and "1,234.56" each read one way. "12,5" and "12.5" read as decimals. "1,234"
    is ambiguous (a decimal comma, or a thousands group) and reads both ways.
    """
    token = token.strip()
    sign = ""
    if token[:1] in "+-−":
        sign, token = token[0], token[1:]
    if not token or not token[0].isdigit():
        return []
    marks = [ch for ch in token if ch in ".,"]
    readings: list[tuple[float, int]] = []
    if not marks:
        candidates = [None]
    elif "." in marks and "," in marks:
        candidates = [marks[-1]]
    else:
        mark = marks[0]
        if len(marks) > 1:
            candidates = [None]
        else:
            fraction = token.rsplit(mark, 1)[1]
            candidates = [mark] if len(fraction) != 3 else [mark, None]
    for mark in candidates:
        reading = _number(token, mark, sign)
        if reading is not None and reading not in readings:
            readings.append(reading)
    return readings


def numbers_in(text: str) -> list[tuple[float, int]]:
    """All readings of all numbers written in a text."""
    found: list[tuple[float, int]] = []
    for pattern in (_NUMBER_RUN, _SPACED_NUMBER):
        for match in pattern.finditer(text or ""):
            token = match.group(0).rstrip(".,'’" + "  ")
            found.extend(number_readings(token))
    return found


def cell_value(cell: Cell, decimal_mark: str) -> tuple[float, int] | None:
    """A cell's number: its stored value, or its text read with this decimal mark."""
    if cell.number is not None:
        return cell.number, cell.decimals or 0
    text = cell.text.strip()
    match = re.fullmatch(r"[^\d+\-−]*([-+−]?\d[\d.,  '’ ]*)\D*", text)
    if not match:
        return None
    token = match.group(1).strip()
    sign = ""
    if token[:1] in "+-−":
        sign, token = token[0], token[1:]
    marks = [ch for ch in token if ch in ".,"]
    if decimal_mark not in marks:
        return _number(token, None, sign)
    return _number(token, decimal_mark, sign)


# --- Packages ----------------------------------------------------------------------------


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _rel_id(element) -> str | None:
    for key, value in element.attrib.items():
        if key.startswith("{") and key.endswith("}id") and "relationships" in key:
            return value
    return None


@dataclass(frozen=True)
class Relationship:
    type: str
    target: str
    external: bool


class Package:
    """A zip package of XML parts, read within size bounds."""

    def __init__(self, data: bytes):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise DocumentError("not a readable zip package") from exc
        infos = self.zip.infolist()
        if len(infos) > MAX_PARTS:
            raise DocumentError("the package has too many parts")
        if sum(info.file_size for info in infos) > MAX_PACKAGE_BYTES:
            raise DocumentError("the package expands beyond the size limit")
        self.names = {info.filename for info in infos}

    def read(self, name: str) -> bytes | None:
        if name not in self.names:
            return None
        info = self.zip.getinfo(name)
        if info.file_size > MAX_PART_BYTES:
            raise DocumentError(f"{name} is larger than the part size limit")
        try:
            with self.zip.open(info) as handle:
                data = handle.read(MAX_PART_BYTES + 1)
        except (zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as exc:
            raise DocumentError(f"{name} cannot be read from the package") from exc
        if len(data) > MAX_PART_BYTES:
            raise DocumentError(f"{name} is larger than the part size limit")
        return data

    def xml(self, name: str):
        data = self.read(name)
        if data is None:
            return None
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise DocumentError(f"{name} is not well-formed XML") from exc

    @staticmethod
    def rels_name(part: str) -> str:
        return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")

    @staticmethod
    def resolve(part: str, target: str) -> str:
        target = unquote(target)
        if target.startswith("/"):
            return posixpath.normpath(target.lstrip("/"))
        return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))

    def rels(self, part: str) -> dict[str, Relationship]:
        root = self.xml(self.rels_name(part))
        found: dict[str, Relationship] = {}
        if root is None:
            return found
        for element in root:
            if _local(element.tag) != "Relationship":
                continue
            rel_id, target = element.get("Id"), element.get("Target") or ""
            external = (element.get("TargetMode") or "").lower() == "external"
            if rel_id:
                found[rel_id] = Relationship(
                    element.get("Type") or "",
                    target if external else self.resolve(part, target),
                    external,
                )
        return found

    def main_part(self) -> str | None:
        for rel in self.rels("").values():
            if rel.type.endswith("/officeDocument") and not rel.external:
                return rel.target
        return None

    def external_hyperlinks(self) -> list[str]:
        """Every external hyperlink any part of the package declares."""
        links = []
        for name in sorted(self.names):
            if not name.endswith(".rels"):
                continue
            root = self.xml(name)
            for element in root if root is not None else ():
                if (
                    _local(element.tag) == "Relationship"
                    and (element.get("TargetMode") or "").lower() == "external"
                    and (element.get("Type") or "").endswith("/hyperlink")
                ):
                    links.append(element.get("Target") or "")
        return links

    def has_chart(self) -> bool:
        return any(
            ("/charts/" in name and posixpath.basename(name).startswith("chart"))
            or "/chartsheets/" in name
            for name in self.names
        )

    def has_image(self) -> bool:
        return any("/media/" in name for name in self.names)

    def property_count(self, name: str) -> int | None:
        """A count from docProps/app.xml (Pages, Slides), when the writer recorded it."""
        root = self.xml("docProps/app.xml")
        if root is None:
            return None
        for element in root.iter():
            if _local(element.tag) == name and (element.text or "").strip().isdigit():
                return int(element.text.strip())
        return None


def _rich_text(element) -> str:
    parts: list[str] = []

    def walk(node):
        for child in node:
            name = _local(child.tag)
            if name in ("rPh", "phoneticPr"):
                continue
            if name == "t":
                parts.append(child.text or "")
            else:
                walk(child)

    walk(element)
    return "".join(parts)


def _column_index(ref: str) -> int | None:
    letters = re.match(r"[A-Za-z]+", ref or "")
    if not letters:
        return None
    index = 0
    for ch in letters.group(0).upper():
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _decimals(text: str) -> int:
    try:
        exponent = Decimal(text.strip()).normalize().as_tuple().exponent
    except InvalidOperation:
        return 0
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _sheet_table(root, shared: list[str], name: str, budget: list[int]) -> Table:
    rows: list[list[Cell]] = []
    for row_element in (el for el in root.iter() if _local(el.tag) == "row"):
        cells: dict[int, Cell] = {}
        next_column = 0
        for element in row_element:
            if _local(element.tag) != "c":
                continue
            budget[0] -= 1
            if budget[0] < 0:
                raise DocumentError("the workbook has more cells than the check reads")
            ref = element.get("r")
            column = _column_index(ref) if ref else None
            column = next_column if column is None else column
            next_column = column + 1
            kind = element.get("t", "n")
            value = formula = inline = None
            for child in element:
                child_name = _local(child.tag)
                if child_name == "v":
                    value = child.text or ""
                elif child_name == "f":
                    formula = (child.text or "").strip() or None
                elif child_name == "is":
                    inline = _rich_text(child)
            cell = Cell(ref=ref, formula=formula)
            if kind == "s" and value is not None:
                index = int(value) if value.strip().isdigit() else -1
                cell.text = shared[index] if 0 <= index < len(shared) else ""
            elif kind == "inlineStr":
                cell.text = inline or ""
            elif kind in ("str", "e"):
                cell.text = value or ""
            elif kind == "b":
                cell.text = "TRUE" if (value or "").strip() == "1" else "FALSE"
            elif value is not None and value.strip():
                try:
                    cell.number = float(value)
                except ValueError:
                    cell.text = value
                else:
                    cell.decimals = _decimals(value)
                    cell.text = value.strip()
            cells[column] = cell
        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(index, Cell()) for index in range(width)])
    return Table(name, rows)


_HYPERLINK_FORMULA = re.compile(r'HYPERLINK\(\s*"([^"]+)"', re.IGNORECASE)
_HYPERLINK_FIELD = re.compile(r'HYPERLINK\s+"([^"]+)"', re.IGNORECASE)


def _read_xlsx(package: Package, main: str, inspection: Inspection) -> None:
    workbook = package.xml(main)
    rels = package.rels(main)
    shared: list[str] = []
    for rel in rels.values():
        if rel.type.endswith("/sharedStrings") and not rel.external:
            root = package.xml(rel.target)
            if root is not None:
                shared = [_rich_text(si) for si in root if _local(si.tag) == "si"]
    sheets = [el for el in workbook.iter() if _local(el.tag) == "sheet"]
    if not sheets:
        raise DocumentError("the workbook lists no sheets")
    budget = [MAX_CELLS]
    lines: list[str] = []
    for sheet in sheets:
        name = sheet.get("name") or "Sheet"
        rel = rels.get(_rel_id(sheet) or "")
        if rel is None or rel.external:
            inspection.notes.append(f"sheet {name!r} has no part")
            continue
        root = package.xml(rel.target)
        if root is None:
            raise DocumentError(f"sheet {name!r} is listed but its part is missing")
        if _local(root.tag) != "worksheet":
            continue
        table = _sheet_table(root, shared, name, budget)
        inspection.tables.append(table)
        lines.append(f"[{name}]")
        for row in table.nonempty_rows():
            lines.append("\t".join(cell.text for cell in row))
            for cell in row:
                if cell.formula:
                    inspection.links.extend(_HYPERLINK_FORMULA.findall(cell.formula))
    inspection.text = "\n".join(lines)


def _paragraph_text(paragraph) -> str:
    parts: list[str] = []
    for element in paragraph.iter():
        name = _local(element.tag)
        if name == "t":
            parts.append(element.text or "")
        elif name == "tab":
            parts.append("\t")
        elif name in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _children(element, name: str):
    return [child for child in element if _local(child.tag) == name]


def _word_table(table, index: int) -> Table:
    rows = []
    for row in _children(table, "tr"):
        cells = []
        for cell in _children(row, "tc"):
            text = " ".join(_paragraph_text(p) for p in cell.iter() if _local(p.tag) == "p").strip()
            cells.append(Cell(text=text))
        rows.append(cells)
    return Table(f"table {index}", rows)


def _read_docx(package: Package, main: str, inspection: Inspection) -> None:
    root = package.xml(main)
    body = next((el for el in root.iter() if _local(el.tag) == "body"), None)
    if body is None:
        raise DocumentError("the document has no body")
    lines: list[str] = []

    def walk(container):
        for child in container:
            name = _local(child.tag)
            if name == "p":
                text = _paragraph_text(child)
                if text.strip():
                    inspection.paragraphs += 1
                    lines.append(text)
            elif name == "tbl":
                table = _word_table(child, len(inspection.tables) + 1)
                inspection.tables.append(table)
                lines.extend("\t".join(cell.text for cell in row) for row in table.rows)
            elif name in ("sdt", "sdtContent", "customXml", "smartTag"):
                walk(child)

    walk(body)
    fields = " ".join(el.text or "" for el in root.iter() if _local(el.tag) == "instrText")
    inspection.links.extend(_HYPERLINK_FIELD.findall(fields))
    inspection.text = "\n".join(lines)
    inspection.pages = package.property_count("Pages")
    if inspection.pages is None:
        inspection.notes.append("the document does not record its page count")


def _read_pptx(package: Package, main: str, inspection: Inspection) -> None:
    root = package.xml(main)
    rels = package.rels(main)
    lines: list[str] = []
    slides = 0
    for slide_id in (el for el in root.iter() if _local(el.tag) == "sldId"):
        rel = rels.get(_rel_id(slide_id) or "")
        if rel is None or rel.external or rel.target not in package.names:
            inspection.notes.append("a listed slide has no part")
            continue
        slides += 1
        slide = package.xml(rel.target)
        lines.append(f"[slide {slides}]")
        for paragraph in (el for el in slide.iter() if _local(el.tag) == "p"):
            text = "".join(t.text or "" for t in paragraph.iter() if _local(t.tag) == "t")
            if text.strip():
                inspection.paragraphs += 1
                lines.append(text)
        for table in (el for el in slide.iter() if _local(el.tag) == "tbl"):
            rows = []
            for row in (el for el in table.iter() if _local(el.tag) == "tr"):
                rows.append(
                    [
                        Cell(
                            text="".join(t.text or "" for t in cell.iter() if _local(t.tag) == "t")
                        )
                        for cell in row
                        if _local(cell.tag) == "tc"
                    ]
                )
            inspection.tables.append(Table(f"slide {slides} table", rows))
    if slides == 0:
        raise DocumentError("the presentation has no slides")
    inspection.slides = slides
    inspection.text = "\n".join(lines)


_OFFICE_READERS = {"workbook": "xlsx", "document": "docx", "presentation": "pptx"}


def _read_package(data: bytes, inspection: Inspection) -> None:
    package = Package(data)
    main = package.main_part()
    root = package.xml(main) if main else None
    kind = _OFFICE_READERS.get(_local(root.tag)) if root is not None else None
    if kind is None:
        raise DocumentError("the zip package is not a Word, Excel or PowerPoint document")
    inspection.kind = kind
    {"xlsx": _read_xlsx, "docx": _read_docx, "pptx": _read_pptx}[kind](package, main, inspection)
    inspection.links = package.external_hyperlinks() + inspection.links
    inspection.has_chart = package.has_chart()
    inspection.has_image = package.has_image()


# --- PDF ---------------------------------------------------------------------------------

_PDF_OBJECT = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_PDF_STREAM = re.compile(rb"\bstream\r?\n")


def _inflate(head: bytes, raw: bytes) -> bytes | None:
    filters = re.findall(rb"/([A-Za-z0-9]+Decode)\b", head)
    if not filters:
        return raw
    if any(name != b"FlateDecode" for name in filters):
        return None
    inflater = zlib.decompressobj()
    try:
        data = inflater.decompress(raw, MAX_INFLATED_BYTES)
    except zlib.error:
        return None
    return data


def _pdf_string(raw: bytes) -> str:
    out = bytearray()
    index = 0
    escapes = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
    while index < len(raw):
        byte = raw[index : index + 1]
        if byte == b"\\" and index + 1 < len(raw):
            following = raw[index + 1 : index + 2]
            octal = re.match(rb"[0-7]{1,3}", raw[index + 1 : index + 4])
            if octal:
                out.append(int(octal.group(0), 8) & 0xFF)
                index += 1 + len(octal.group(0))
                continue
            out += escapes.get(following, following)
            index += 2
            continue
        out += byte
        index += 1
    if out.startswith(codecs.BOM_UTF16_BE):
        return bytes(out[2:]).decode("utf-16-be", errors="replace")
    return bytes(out).decode("latin-1")


_PDF_TEXT = re.compile(rb"\(((?:\\.|[^\\)])*)\)\s*(?:Tj|'|\")|\[((?:\\.|[^\]])*)\]\s*TJ", re.S)
_PDF_ARRAY_STRING = re.compile(rb"\(((?:\\.|[^\\)])*)\)", re.S)


def _read_pdf(data: bytes, inspection: Inspection) -> None:
    start = data.find(b"%PDF-")
    if start < 0 or start > 1024:
        raise DocumentError("no PDF header")
    if b"%%EOF" not in data[-4096:]:
        inspection.notes.append("no end-of-file marker; the file may be cut short")
    objects: dict[int, bytes] = {}
    streams: list[tuple[bytes, bytes]] = []
    for match in _PDF_OBJECT.finditer(data):
        end = data.find(b"endobj", match.end())
        if end < 0:
            break
        body = data[match.end() : end]
        stream = _PDF_STREAM.search(body)
        if stream:
            raw = body[stream.end() :]
            stop = raw.rfind(b"endstream")
            streams.append((body[: stream.start()], raw[:stop] if stop >= 0 else raw))
            body = body[: stream.start()]
        objects[int(match.group(1))] = body
    for head, raw in streams:
        if not re.search(rb"/Type\s*/ObjStm\b", head):
            continue
        content = _inflate(head, raw)
        count = re.search(rb"/N\s+(\d+)", head)
        first = re.search(rb"/First\s+(\d+)", head)
        if content is None or not count or not first:
            continue
        first_offset = int(first.group(1))
        numbers = content[:first_offset].split()
        pairs = [
            (int(numbers[i]), int(numbers[i + 1]))
            for i in range(0, min(len(numbers) - 1, 2 * int(count.group(1))), 2)
            if numbers[i].isdigit() and numbers[i + 1].isdigit()
        ]
        for index, (number, offset) in enumerate(pairs):
            stop = pairs[index + 1][1] if index + 1 < len(pairs) else len(content) - first_offset
            objects.setdefault(number, content[first_offset + offset : first_offset + stop])
    tree_nodes = [body for body in objects.values() if re.search(rb"/Type\s*/Pages\b", body)]
    leaves = sum(1 for body in objects.values() if re.search(rb"/Type\s*/Page(?![A-Za-z])", body))
    roots = [body for body in tree_nodes if not re.search(rb"/Parent\b", body)]
    declared = None
    if len(roots) == 1:
        found = re.search(rb"/Count\s+(\d+)", roots[0])
        declared = int(found.group(1)) if found else None
    elif len(roots) > 1:
        inspection.notes.append("the file has more than one page-tree root")
    counts = [value for value in (declared, leaves or None) if value is not None]
    inspection.pages = min(counts) if counts else None
    if declared is not None and leaves and declared != leaves:
        inspection.notes.append(
            f"the page tree declares {declared} pages but {leaves} page objects were found; "
            "the smaller count is used"
        )
    if inspection.pages is None:
        inspection.notes.append("the page count could not be read")
    for uri in re.findall(rb"/URI\s*\(((?:\\.|[^\\)])*)\)", data):
        inspection.links.append(_pdf_string(uri))
    if re.search(rb"/Encrypt\b", data):
        inspection.notes.append("the file is encrypted; its text was not read")
        return
    texts: list[str] = []
    size = 0
    for head, raw in streams:
        if re.search(rb"/(?:ObjStm|XRef|XObject|Font|FontDescriptor)\b|/Length[123]\b", head):
            continue
        content = _inflate(head, raw)
        if content is None:
            continue
        for match in _PDF_TEXT.finditer(content):
            if match.group(1) is not None:
                piece = _pdf_string(match.group(1))
            else:
                piece = "".join(
                    _pdf_string(part) for part in _PDF_ARRAY_STRING.findall(match.group(2))
                )
            texts.append(piece)
            size += len(piece)
            if size > TEXT_LIMIT:
                break
    inspection.text = "\n".join(texts)
    if texts:
        inspection.notes.append("text read from the PDF is approximate")


# --- Text, CSV, images -------------------------------------------------------------------


def decode_text(data: bytes) -> tuple[str, str | None]:
    """Text from bytes: UTF-8 (with or without a BOM), UTF-16 with a BOM, else Latin-1."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace"), None
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return data.decode("latin-1"), "the file is not UTF-8 text; read as Latin-1"


_TABLE_LINE = re.compile(r"^\s*\|?.*\|.*\|?\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•▪◦–]|\d{1,3}[.)、])\s+\S")


def markdown_tables(text: str) -> list[Table]:
    """Pipe tables in Markdown text, without their |---| rule lines."""
    tables: list[Table] = []
    block: list[str] = []

    def close():
        rows = [line for line in block if not _TABLE_RULE.match(line)]
        if len(block) >= 2 and len(rows) >= 2:
            table = Table(f"table {len(tables) + 1}")
            for line in rows:
                inner = line.strip()
                inner = inner[1:] if inner.startswith("|") else inner
                inner = inner[:-1] if inner.endswith("|") else inner
                table.rows.append([Cell(text=part.strip()) for part in inner.split("|")])
            tables.append(table)
        block.clear()

    for line in (text or "").splitlines():
        if line.count("|") >= 2 and _TABLE_LINE.match(line):
            block.append(line)
        else:
            close()
    close()
    return tables


def list_items(text: str) -> int:
    """Bulleted or numbered items in a reply, or rows of its largest table if more."""
    bullets = sum(1 for line in (text or "").splitlines() if _LIST_ITEM.match(line))
    table_rows = max((table.data_rows for table in markdown_tables(text)), default=0)
    return max(bullets, table_rows)


def _read_csv(data: bytes, name: str, inspection: Inspection) -> None:
    text, note = decode_text(data)
    if note:
        inspection.notes.append(note)
    if "\x00" in text:
        raise DocumentError("the file holds binary data, not CSV text")
    sample = text[:16384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if name.lower().endswith(".tsv") else csv.excel
    try:
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error as exc:
        raise DocumentError(f"the CSV cannot be read: {exc}") from exc
    if not rows:
        raise DocumentError("the CSV is empty")
    if sum(len(row) for row in rows) > MAX_CELLS:
        raise DocumentError("the CSV has more cells than the check reads")
    inspection.tables.append(Table(name, [[Cell(text=value) for value in row] for row in rows]))
    inspection.text = text


def _read_text(data: bytes, inspection: Inspection) -> None:
    text, note = decode_text(data)
    if note:
        inspection.notes.append(note)
    if "\x00" in text:
        raise DocumentError("the file holds binary data, not text")
    inspection.text = text
    inspection.paragraphs = sum(1 for block in re.split(r"\n\s*\n", text) if block.strip())
    inspection.tables.extend(markdown_tables(text))


def _image_kind(head: bytes) -> bool:
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    )


def detect_kind(name: str, head: bytes) -> str:
    """The kind of file by its content first, then by its name for plain-text formats."""
    suffix = Path(name).suffix.lower()
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if _image_kind(head):
        return "image"
    if suffix == ".svg" and b"<svg" in head[:4096].lower():
        return "image"
    if suffix in _CSV_SUFFIXES:
        return "csv"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    return "unknown"


def inspect_bytes(name: str, data: bytes) -> Inspection:
    """Open one file's bytes as what it is, and measure it. Never raises for bad content."""
    kind = detect_kind(name, data[:4096])
    inspection = Inspection(name=name, kind=kind, opens=False, size_bytes=len(data))
    try:
        if len(data) > MAX_FILE_BYTES:
            raise DocumentError("the file is larger than the check reads")
        if kind == "zip":
            inspection.kind = "unknown"
            _read_package(data, inspection)
        elif kind == "pdf":
            _read_pdf(data, inspection)
        elif kind == "csv":
            _read_csv(data, name, inspection)
        elif kind == "text":
            _read_text(data, inspection)
        elif kind == "image":
            inspection.has_image = True
        else:
            raise DocumentError("unrecognised file type")
    except (
        DocumentError,
        ET.ParseError,
        zipfile.BadZipFile,
        zlib.error,
        ValueError,
        KeyError,
        IndexError,
        EOFError,
        NotImplementedError,
    ) as exc:
        # The file is broken in a way its own kind's reader rejects: it does not open.
        inspection.error = str(exc) or type(exc).__name__
        return inspection
    inspection.opens = True
    inspection.links.extend(find_urls(inspection.text))
    inspection.text = inspection.text[:TEXT_LIMIT]
    return inspection


def inspect_file(path: Path) -> Inspection:
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            with path.open("rb") as handle:
                head = handle.read(4096)
            return Inspection(
                name=path.name,
                kind=detect_kind(path.name, head),
                opens=False,
                size_bytes=size,
                error="the file is larger than the check reads",
            )
        data = path.read_bytes()
    except OSError as exc:
        return Inspection(name=path.name, kind="unknown", opens=False, error=str(exc))
    return inspect_bytes(path.name, data)
