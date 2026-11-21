"""Owls make, change and check Word, Excel, PowerPoint and PDF files without their computer.

save_file keeps text only, so before these tools any .docx, .xlsx, .pptx or .pdf needed the
owl's private computer. Here the engine's own office operations (office/operations.py) build
and edit the file, and its artifact check (artifact_checks.py) reopens the exact bytes,
recalculates workbook formulas and renders the pages, all in the engine itself.

Every call works in a scratch folder of its own in the owl's place (beside its database, never
in the person's folders) and removes it before returning; one left behind by a crash is removed
by a later call once its task has ended. Inputs come from the Library by file id. A result
reaches the Library, as a new file or a new version (never over another), only when no check of
its exact bytes failed; a failed check returns what to fix instead. Page images of the check go
to the model only when its model declares image input.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ..cancellation import EventCancellationToken
from ..documents.__main__ import MAX_PAGES, MAX_SOURCE_BYTES
from . import documents
from .artifact_validation import failed
from .browser_images import FileImageResult, file_image
from .files import MAX_FILE_BYTES
from .models import FileInput, Input
from .store import TERMINAL, Conflict, NotFound

OFFICE_FORMATS = ("docx", "xlsx", "pptx")
FORMATS = (*OFFICE_FORMATS, "pdf")
Format = Literal["docx", "xlsx", "pptx", "pdf"]
LABELS = {"docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint", "pdf": "PDF"}
# The PDF builder bounds its Markdown source in bytes; a text of at most that many characters
# can still exceed it, and the builder then says so.
MAX_MARKDOWN = MAX_SOURCE_BYTES
# Folder in the owl's place for this work; a folder per person, then per task, then per call.
SCRATCH = "owl-documents"
# A task's Stop is read from its store row (JobCancellationToken), and rendering asks after
# every few hundred characters of every page: ask the store at most this often.
STOP_POLL_SECONDS = 0.2

# In the order a turn offers them.
DOCUMENT_TOOLS = (
    "describe_document_format",
    "create_document",
    "inspect_document",
    "edit_document",
    "check_document",
)

FILES_OFF = "Files are turned off for this owl, so it can't make or change documents."
UNAVAILABLE = "Making and checking {label} files isn't available in this setup."
NOT_A_DOCUMENT = "This file isn't a Word, Excel, PowerPoint or PDF file, so it can't be used here."
PDF_NOT_EDITABLE = (
    "A PDF can't be inspected or edited here. Read it with read_file, then make a new PDF with "
    "create_document."
)
TOO_LARGE = f"This file is larger than {MAX_FILE_BYTES // 2**20} MiB, so it can't be used here."
CHANGED = "The file changed while it was being read. Try again."
OLDER_VERSION = (
    "This is an older version of {name}. Edit its latest version (list_files shows it), or give "
    "a new name to save the result as a separate file."
)
NO_FORMULAS = "This workbook has no formulas to recalculate."
NO_RECALCULATION = (
    "Recalculating formulas isn't available in this setup. The workbook keeps the results "
    "saved in it."
)
WORKING_FILES = "The engine couldn't read or write its working files for this document."
NESTED = "The document or spec is nested too deeply to process."
IMAGE_BY_ID = (
    "A slide image comes from the Library: give file_id, the id of a PNG or JPEG Library file, "
    "in place of path."
)
NAME_EXTENSION = "name must end with .{format}"
NOT_VISIBLE = (
    "Its pages were rendered, but the current model can't see images, so none is shown. The "
    "checks listed are what was verified."
)
NOT_RENDERED = "No page could be rendered here, so none is shown; see the render check."
NO_SUCH_PAGE = "It has {pages} pages, so it has no page {page}; page {shown} is shown."
SAVED = (
    "Saved in the Library as {what}; it shows as a file card with your reply. Look at the shown "
    "page for layout problems before calling it done (check_document with page shows another "
    "page). These checks don't prove the content is right."
)
NOT_SAVED = (
    "Nothing was saved: a check failed. Fix what the failed checks say{formulas}, then call "
    "{tool} again{again}."
)
CHECKED = (
    "Nothing was changed. These checks don't prove the content is right. check_document with "
    "page shows another rendered page."
)
INSPECTED = (
    "To change this file, call edit_document with this file_id and an edit spec (see "
    "describe_document_format) whose expected_source_sha256 is source_sha256 and whose text "
    "hashes come from this content."
)
GUIDE_NOTES = (
    "create_document takes the create spec as spec; edit_document takes the edit spec, whose "
    "expected_source_sha256 and text hashes come from inspect_document. The tools keep the "
    "files themselves: there are no paths, and each result is checked and then saved in the "
    "Library."
)
PDF_GUIDE = (
    "Write the whole document as Markdown in create_document's markdown field: headings, "
    "paragraphs, lists, tables, links and charts (see chart). Its title defaults to the file "
    "name."
)

# Errors the engine's document operations raise for a request or file they refuse (the same
# set its command line reports, plus the parsers' syntax errors for a damaged file). Anything
# else is a defect and fails the task.
ENGINE_ERRORS = (
    ValueError,
    OSError,
    ImportError,
    zipfile.BadZipFile,
    RecursionError,
    SyntaxError,
    documents.DocumentError,
)


def _library_name(value: str) -> str:
    return FileInput(name=value, content="").name


class DocumentFormat(Input):
    format: Format


class CreateDocument(Input):
    format: Format
    name: str = Field(
        min_length=1,
        max_length=160,
        description="The Library file name, ending with the format's extension.",
    )
    spec: dict[str, Any] | None = Field(
        default=None,
        description="For docx, xlsx and pptx: the create spec from describe_document_format.",
    )
    markdown: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MARKDOWN,
        description="For pdf: the whole document as Markdown.",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="For pdf: its title. Left out, the file name without its extension.",
    )
    previous_file_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="For a new version of a Library file with this exact name: its latest file "
        "id. Leave out for a new file.",
    )

    @field_validator("name")
    @classmethod
    def simple_name(cls, value: str) -> str:
        return _library_name(value)

    @model_validator(mode="after")
    def fits_format(self):
        if not self.name.casefold().endswith("." + self.format):
            raise ValueError(NAME_EXTENSION.format(format=self.format))
        if self.format == "pdf":
            if self.markdown is None or self.spec is not None:
                raise ValueError("A pdf takes markdown, not spec.")
        elif self.spec is None or self.markdown is not None or self.title is not None:
            raise ValueError(
                f"A {self.format} takes spec (see describe_document_format), not markdown or title."
            )
        return self


class LibraryDocument(Input):
    file_id: str = Field(min_length=1, max_length=100)


class EditDocument(LibraryDocument):
    spec: dict[str, Any] | None = Field(
        default=None,
        description="The edit spec from describe_document_format, built from a fresh "
        "inspect_document of this file.",
    )
    recalculate: bool = Field(
        default=False,
        strict=True,
        description="true, without spec, to recalculate an xlsx workbook's formulas.",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="Save the result as a separate Library file with this name. Left out, it "
        "is saved as a new version of this file.",
    )

    @field_validator("name")
    @classmethod
    def simple_name(cls, value: str | None) -> str | None:
        return None if value is None else _library_name(value)

    @model_validator(mode="after")
    def one_change(self):
        if (self.spec is None) == (not self.recalculate):
            raise ValueError("Give spec, or recalculate=true without spec.")
        return self


class CheckDocument(LibraryDocument):
    page: int | None = Field(
        default=None,
        ge=1,
        le=MAX_PAGES,
        description="The rendered page to show. Left out, the first page with a problem, or "
        "else page 1.",
    )


def ready_formats() -> tuple[str, ...]:
    """The formats whose libraries the engine has, from its own capability report."""
    from ..documents import pdf_capability_status
    from ..office import office_capability_status

    office = office_capability_status()["formats"]
    ready = [name for name in OFFICE_FORMATS if office[name]["state"] == "ready"]
    if pdf_capability_status()["state"] == "ready":
        ready.append("pdf")
    return tuple(ready)


def guide(format_name: str) -> dict:
    """describe_document_format: the engine's own spec guide and example for this format."""
    if format_name == "pdf":
        from ..documents import pdf_capability_status

        status = pdf_capability_status()
        return {
            "format": "pdf",
            "guide": PDF_GUIDE,
            "supports": status["supports"],
            "chart": status["chart"],
            "limits": status["limits"],
            "example": {
                "format": "pdf",
                "name": "Αναφορά.pdf",
                "title": "Μηνιαία αναφορά",
                "markdown": "# Μηνιαία αναφορά\n\nΣύνοψη.\n\n| Μήνας | Ποσό |\n|---|---|\n"
                "| Ιανουάριος | 120 |\n",
            },
        }
    from ..office.__main__ import run

    described = run(SimpleNamespace(command="describe", format=format_name))
    spec = copy.deepcopy(described["guide"])
    # The engine's own file workflow (output paths, copying a reviewed draft); these tools keep
    # their files themselves.
    spec.pop("delivery_workflow", None)
    notes = [GUIDE_NOTES]
    if format_name == "pptx":
        for element in spec.get("elements") or ():
            if isinstance(element, dict) and element.get("type") == "image" and "path" in element:
                element.pop("path")
                element["file_id"] = "Library file id of a PNG or JPEG"
        notes.append(IMAGE_BY_ID)
    return {
        "format": format_name,
        "spec": spec,
        "example": copy.deepcopy(described["example"]),
        "notes": " ".join(notes),
    }


def _plain(error: BaseException) -> str:
    """What to tell the model about a refused operation, without paths of the owl's place."""
    if isinstance(error, (ValueError, documents.DocumentError)):
        return str(error)  # The engine's own wording for a spec, file or limit it refuses.
    if isinstance(error, (zipfile.BadZipFile, SyntaxError)):
        return documents.DAMAGED
    if isinstance(error, RecursionError):
        return NESTED
    if isinstance(error, ImportError):
        return "Making documents isn't available in this setup."
    reason = getattr(error, "strerror", None)
    return f"{WORKING_FILES} ({reason})" if reason else WORKING_FILES


def _relative(root: Path, value: str) -> Path:
    from ..documents.__main__ import _path

    return _path(root, value)


class OwlDocuments:
    """One owl's document work for one call; the runtime makes one per tool call."""

    def __init__(self, store, owner: str, token: EventCancellationToken):
        self.store = store
        self.owner = owner
        self.token = token
        # The owl's place: the folder of its database (or of the shared store's scratch).
        self.place = Path(store.path).parent / SCRATCH
        self._stopped = False
        self._next_poll = 0.0

    # ------------------------------------------------------------ the tools

    def call(
        self, task: dict, name: str, args: Input, *, image_input: bool
    ) -> dict | FileImageResult:
        self.token.throw_if_cancelled()
        if not self.store.bot(self.owner, task["bot_id"])["files_enabled"]:
            raise Conflict(FILES_OFF)
        if name == "describe_document_format":
            self._require(args.format)
            return guide(args.format)
        if name == "create_document":
            return self._create(task, args, image_input)
        if name == "inspect_document":
            return self._inspect(task, args)
        if name == "edit_document":
            return self._edit(task, args, image_input)
        if name == "check_document":
            return self._check_file(task, args, image_input)
        raise ValueError("Unsupported document tool.")

    def _create(self, task, args: CreateDocument, image_input):
        kind = args.format
        self._require(kind)
        output = "document." + kind
        with self._scratch(task) as root:
            if kind == "pdf":
                (root / "source.md").write_text(args.markdown, encoding="utf-8")
                produced = self._operate(
                    root,
                    {
                        "action": "create",
                        "format": "pdf",
                        "source": "source.md",
                        "output": output,
                        # The name ends with .pdf (validated), so this is the name without it.
                        "title": args.title or args.name.rsplit(".", 1)[0],
                    },
                )
            else:
                spec = args.spec
                if kind == "pptx":
                    spec = self._slide_images(task, spec, root)
                self._write_spec(root, spec)
                produced = self._operate(
                    root,
                    {"action": "create", "format": kind, "spec": "spec.json", "output": output},
                )
            output, calculation = self._calculated(root, kind, output, produced)
            outcome = self._checked(root, output, None, image_input, keep=True)
        return self._finish(
            task,
            outcome,
            kind=kind,
            name=args.name,
            previous_file_id=args.previous_file_id,
            calculation=calculation,
            tool="create_document",
            again=" with the corrected spec" if kind != "pdf" else " with corrected markdown",
        )

    def _inspect(self, task, args: LibraryDocument) -> dict:
        file, data, kind = self._source(task, args.file_id)
        if kind == "pdf":
            raise Conflict(PDF_NOT_EDITABLE)
        with self._scratch(task) as root:
            source = "source." + kind
            (root / source).write_bytes(data)
            inspected = self._operate(root, {"action": "inspect", "source": source})
        return {
            "file_id": file["id"],
            "name": file["name"],
            "format": kind,
            "source_sha256": inspected["source_sha256"],
            "content": inspected["content"],
            "next": INSPECTED,
        }

    def _edit(self, task, args: EditDocument, image_input):
        file, data, kind = self._source(task, args.file_id)
        if kind == "pdf":
            raise Conflict(PDF_NOT_EDITABLE)
        name = args.name or file["name"]
        if args.name and not name.casefold().endswith("." + kind):
            raise Conflict(NAME_EXTENSION.format(format=kind))
        # Saved under the file's own name, the result is its next version.
        previous = file["id"] if name == file["name"] else None
        if previous and not file["is_latest"]:
            raise Conflict(OLDER_VERSION.format(name=file["name"]))
        if args.recalculate and kind != "xlsx":
            raise Conflict("Only an xlsx workbook has formulas to recalculate.")
        with self._scratch(task) as root:
            source = "source." + kind
            (root / source).write_bytes(data)
            if args.recalculate:
                output, calculation = self._recalculate(root, source)
                if not calculation["formula_count"]:
                    raise Conflict(NO_FORMULAS)
            else:
                self._write_spec(root, args.spec)
                output = "document." + kind
                produced = self._operate(
                    root,
                    {"action": "edit", "source": source, "spec": "spec.json", "output": output},
                )
                output, calculation = self._calculated(root, kind, output, produced)
            outcome = self._checked(root, output, None, image_input, keep=True)
        return self._finish(
            task,
            outcome,
            kind=kind,
            name=name,
            previous_file_id=previous,
            calculation=calculation,
            tool="edit_document",
            again=""
            if args.recalculate
            else " with a corrected spec (its expected_source_sha256 stays the same)",
            source_file_id=file["id"],
        )

    def _check_file(self, task, args: CheckDocument, image_input):
        file, data, kind = self._source(task, args.file_id)
        with self._scratch(task) as root:
            source = "document." + kind
            (root / source).write_bytes(data)
            report, view, shown, _ = self._checked(root, source, args.page, image_input, keep=False)
        receipt = {
            "file_id": file["id"],
            "name": file["name"],
            "format": kind,
            "passed": not failed(report),
            **_summary(report),
            **view,
            "next": CHECKED,
        }
        return self._shown(receipt, shown, file["name"], saved=True)

    # ------------------------------------------------------------ steps

    def _require(self, kind: str) -> None:
        if kind not in ready_formats():
            raise Conflict(UNAVAILABLE.format(label=LABELS[kind]))

    def _source(self, task, file_id: str) -> tuple[dict, bytes, str]:
        """A Library file's exact bytes and its kind, read from its content, never its name."""
        file, data = self._library_bytes(task, file_id)
        head = data[: documents.SNIFF_BYTES]
        kind = documents.sniff(head)
        if kind == "zip":
            try:
                kind = documents.office_kind(data)
            except documents.DocumentError as error:
                raise Conflict(str(error)) from None
        elif kind == "cfb" and documents.ENCRYPTED_PACKAGE_STREAMS <= (
            documents.compound_stream_names(data)
        ):
            raise Conflict(documents.PROTECTED)
        if kind not in FORMATS:
            raise Conflict(NOT_A_DOCUMENT)
        self._require(kind)
        return file, data, kind

    def _library_bytes(self, task, file_id: str) -> tuple[dict, bytes]:
        bot_id = task["bot_id"]
        file = self.store.file(self.owner, bot_id, file_id)
        if file["size_bytes"] > MAX_FILE_BYTES:
            raise Conflict(TOO_LARGE)
        parts, size, digest = [], 0, hashlib.sha256()
        with closing(self.store.file_chunks(self.owner, bot_id, file_id)) as chunks:
            for chunk in chunks:
                self.token.throw_if_cancelled()
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise Conflict(TOO_LARGE)
                digest.update(chunk)
                parts.append(chunk)
        if size != file["size_bytes"] or digest.hexdigest() != file["sha256"]:
            raise Conflict(CHANGED)
        return file, b"".join(parts)

    def _slide_images(self, task, spec: dict, root: Path) -> dict:
        """A deck spec whose image elements name Library files, rewritten to scratch copies."""
        slides = spec.get("slides")
        if not isinstance(slides, list):
            return spec  # The engine refuses the spec in its own words.
        spec = copy.deepcopy(spec)
        count = 0
        for slide in spec["slides"]:
            elements = slide.get("elements") if isinstance(slide, dict) else None
            for element in elements if isinstance(elements, list) else ():
                if not isinstance(element, dict) or element.get("type") != "image":
                    continue
                file_id = element.pop("file_id", None)
                if "path" in element or not isinstance(file_id, str) or not file_id:
                    raise Conflict(IMAGE_BY_ID)
                _, data = self._library_bytes(task, file_id)
                count += 1
                path = f"images/{count}"
                (root / "images").mkdir(exist_ok=True)
                (root / path).write_bytes(data)
                element["path"] = path
        return spec

    @staticmethod
    def _write_spec(root: Path, spec: dict) -> None:
        try:
            text = json.dumps(spec, ensure_ascii=False, allow_nan=False)
        except ValueError:
            raise Conflict("spec must use finite numbers.") from None
        (root / "spec.json").write_text(text, encoding="utf-8")

    def _calculated(self, root, kind, output, produced) -> tuple[str, dict | None]:
        """A new workbook with formulas gets their results, where the engine can calculate."""
        from ..office import office_capability_status

        if kind != "xlsx" or not produced.get("content", {}).get("formula_count"):
            return output, None
        if office_capability_status()["calculation"]["state"] != "configured":
            return output, None  # The check then reports formula results as not checked.
        return self._recalculate(root, output)

    def _recalculate(self, root, source) -> tuple[str, dict]:
        from ..office import office_capability_status

        if office_capability_status()["calculation"]["state"] != "configured":
            raise Conflict(NO_RECALCULATION)
        output = "calculated.xlsx"
        result = self._operate(root, {"action": "recalculate", "source": source, "output": output})
        return output, result["calculation"]

    def _operate(self, root: Path, arguments: dict) -> dict:
        from ..office.operations import run_operation

        def scratch_only(path):
            _relative(root, path)  # Every path stays in this call's scratch folder.

        return self._engine(
            run_operation,
            root,
            arguments,
            read_guard=scratch_only,
            write_guard=scratch_only,
            cancel_check=self._stop_requested,
        )

    def _checked(self, root: Path, output: str, page, image_input, *, keep: bool):
        """The engine's check of the exact output, the page to show the model, and (to keep,
        when no check failed) the checked bytes themselves."""
        from ..artifact_checks import ArtifactChecks

        checks = ArtifactChecks(root, cancel_check=self._stop_requested)
        report = self._engine(checks.check, output)
        data = None
        if keep and not failed(report):
            data = _relative(root, output).read_bytes()
            if hashlib.sha256(data).hexdigest() != report["documentSha256"]:
                raise Conflict(CHANGED)
        view, shown = self._page(root, report, page, image_input)
        return report, view, shown, data

    def _engine(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except KeyboardInterrupt:
            # The engine's operations stop this way when asked to (cancel_check).
            self.token.throw_if_cancelled()
            raise
        except ENGINE_ERRORS as error:
            self.token.throw_if_cancelled()
            raise Conflict(_plain(error)) from None

    def _stop_requested(self) -> bool:
        now = time.monotonic()
        if not self._stopped and now >= self._next_poll:
            self._stopped = bool(self.token.is_cancelled)
            self._next_poll = now + STOP_POLL_SECONDS
        return self._stopped

    def _page(self, root: Path, report: dict, page: int | None, image_input: bool):
        """Which rendered page to show: the one asked for, else the first with text outside
        it, else the first; and the image of it for a model that can see images."""
        preview = report.get("preview")
        if not preview or not preview.get("pages"):
            return {"page_image": NOT_RENDERED}, None
        pages = preview["pages"]
        view = {}
        shown = page
        if shown is None:
            shown = next((p["page"] for p in pages if p["characters_outside_page"]), 1)
        elif shown > len(pages):
            view["page_note"] = NO_SUCH_PAGE.format(pages=len(pages), page=shown, shown=1)
            shown = 1
        if not image_input:
            return {**view, "page_image": NOT_VISIBLE}, None
        try:
            picture = documents.picture(_relative(root, pages[shown - 1]["image"]).read_bytes())
        except documents.DocumentError as error:
            # The checks stand; only the picture of the page is missing, and the model is told.
            return {**view, "page_image": str(error)}, None
        view.update(shown_page=shown, shown_width=picture.width, shown_height=picture.height)
        return view, (picture, shown, len(pages))

    @staticmethod
    def _shown(receipt: dict, shown, name: str, *, saved: bool):
        """The receipt, with the rendered page attached as the step's one image."""
        if shown is None:
            return receipt
        picture, page, pages = shown
        state = "" if saved else "unsaved draft, "
        source = (
            f"{json.dumps(name[:200], ensure_ascii=False)} ({state}rendered page {page} of {pages})"
        )
        return file_image(receipt, picture.data_url, source, picture.width, picture.height)

    def _finish(
        self,
        task,
        outcome,
        *,
        kind,
        name,
        previous_file_id,
        calculation,
        tool,
        again,
        source_file_id=None,
    ):
        report, view, shown, data = outcome
        receipt = {
            "name": name,
            "format": kind,
            **({"source_file_id": source_file_id} if source_file_id else {}),
            **_summary(report),
            **({"calculation": _calculation(calculation)} if calculation else {}),
            **view,
        }
        if failed(report):
            formulas = (
                " (the workbook was already recalculated: correct the formulas listed in "
                "calculation.errors)"
                if calculation and calculation.get("error_count")
                else ""
            )
            receipt = {
                "saved": False,
                **receipt,
                "next": NOT_SAVED.format(formulas=formulas, tool=tool, again=again),
            }
            return self._shown(receipt, shown, name, saved=False)
        # Stop between the check and the save leaves nothing; the store also refuses a write
        # from a task that is no longer running.
        self.token.throw_if_cancelled()
        saved = self.store.save_bytes(
            self.owner,
            task["bot_id"],
            name,
            data,
            source_task_id=task["id"],
            previous_file_id=previous_file_id,
        )
        what = (
            f"version {saved['revision']} of {saved['name']}"
            if saved["revision"] > 1
            else f"the new file {saved['name']}"
        )
        receipt = {
            "id": saved["id"],
            "saved": True,
            **receipt,
            "name": saved["name"],
            "version": saved["revision"],
            "bytes": saved["size_bytes"],
            "sha256": saved["sha256"],
            "next": SAVED.format(what=what),
        }
        return self._shown(receipt, shown, saved["name"], saved=True)

    # ------------------------------------------------------------ scratch

    @contextmanager
    def _scratch(self, task) -> Iterator[Path]:
        """A new folder for one call in the owl's place, removed before the call returns."""
        person = self.place / hashlib.sha256(self.owner.encode("utf-8")).hexdigest()[:16]
        folder = person / task["id"]
        try:
            self._sweep(person, task["id"])
            folder.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(dir=folder)).resolve()
        except OSError as error:
            raise Conflict(_plain(error)) from None
        try:
            yield work
        except BaseException:
            # Keep the original failure or Stop. A folder that can't be removed now (a file
            # still held open on Windows) is removed by a later call once this task has ended.
            shutil.rmtree(work, ignore_errors=True)
            raise
        try:
            shutil.rmtree(work)
        except OSError as error:
            raise Conflict(_plain(error) + " Nothing was saved; try again.") from None
        if not any(folder.iterdir()):
            folder.rmdir()

    def _sweep(self, person: Path, current: str) -> None:
        """Remove what crashed calls left behind for this person's tasks that have ended."""
        if not person.is_dir():
            return
        for entry in person.iterdir():
            if entry.name == current:
                continue
            try:
                ended = self.store.task(self.owner, entry.name)["status"] in TERMINAL
            except NotFound:
                ended = True
            if ended:
                # A folder whose files are still held open (a stopped call finishing on
                # Windows) stays until a later call.
                shutil.rmtree(entry, ignore_errors=True)


def _summary(report: dict) -> dict:
    """The check as the model needs it: each check, its result and what to fix."""
    summary = {
        "checks": [
            {key: item[key] for key in ("label", "status", "detail") if key in item}
            for item in report["items"]
        ],
        "check_scope": report["scope"],
    }
    preview = report.get("preview")
    if preview:
        summary["pages"] = preview["pageCount"]
        outside = [page["page"] for page in preview["pages"] if page["characters_outside_page"]]
        if outside:
            summary["pages_with_text_outside"] = outside
    if report.get("calculation"):
        summary["formula_check"] = report["calculation"]
    return summary


def _calculation(result: dict) -> dict:
    return {
        key: result[key]
        for key in (
            "state",
            "formula_count",
            "error_count",
            "errors",
            "results",
            "results_truncated",
        )
        if key in result
    }
