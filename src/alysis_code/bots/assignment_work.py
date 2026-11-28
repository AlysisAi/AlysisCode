"""Create a workbook from the data in one approved assignment."""

import json
import re
from importlib.util import find_spec
from io import BytesIO

from pydantic import ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from .models import FileInput, Input
from .owl_documents import LibraryDocument, OwlDocuments, ready_formats
from .store import Conflict, Store

MAX_CELLS = 10_000
WORKBOOK_DOCUMENT_TOOLS = (
    "describe_document_format",
    "create_document",
    "inspect_document",
    "edit_document",
    "check_document",
)


def attachment_card_text(text: str, files: list[dict]) -> str:
    """Keep the specialist's prose. The coordinator's cards open the delivered files."""
    names = {file[key]: file["name"] for file in files for key in ("source_file_id", "id")}

    def link_label(match):
        target = match[2].strip().lstrip("<").lower()
        if target.startswith(("https://", "http://", "mailto:")) and not any(
            file_id in target for file_id in names
        ):
            return match[0]
        return match[1]

    text = re.sub(
        r"!?\[([^\[\]\n]*)\]\(\s*(<[^>\n]*>|(?:[^()\n]|\([^()\n]*\))+)\)", link_label, text
    )

    def local_uri(match):
        uri = match[0].rstrip(".,;:!?")
        name = next((name for file_id, name in names.items() if file_id in uri), "")
        return name + match[0][len(uri) :]

    text = re.sub(r"(?i)\b(?:sandbox|file):/+[^\s)\]>`]+", local_uri, text)
    for file_id, name in names.items():
        text = text.replace(file_id, name)
    return text.strip()


class AssignmentFiles:
    """Only the inputs approved on the card and the workbook this task is making."""

    def __init__(self, store, owner, task, token, inputs, read_file):
        self.store = store
        self.owner = owner
        self.task = task
        self.token = token
        self.inputs = {entry["id"] for entry in inputs}
        self.read_file = read_file
        self.output = None
        self.input_samples = {}

    def allowed(self, file_id):
        return file_id in self.inputs or bool(self.output and self.output["id"] == file_id)

    def read(self, args, *, image_input):
        if not self.allowed(args.file_id):
            raise Conflict("Choose an approved input or this assignment's saved output.")
        if not self.store.bot(self.owner, self.task["bot_id"])["files_enabled"]:
            raise Conflict("Library files were turned off for this owl.")
        result = self.read_file(self.task["bot_id"], args, image_input=image_input)
        if args.file_id in self.inputs and isinstance(result, dict) and result.get("content"):
            # Keep bounded, service-read data for the separate review, independent of model prose.
            if args.offset == 0 and args.file_id not in self.input_samples:
                text = result["content"]
                self.input_samples[args.file_id] = {
                    "name": result["name"],
                    "text": text[:3000],
                    "partial": bool(result.get("next_offset") or len(text) > 3000),
                }
        return result

    def document(self, name, args, *, image_input):
        if name in ("describe_document_format", "create_document") and args.format != "xlsx":
            raise Conflict("This assignment can produce an Excel workbook.")
        if name == "create_document" and (args.previous_file_id or self.output):
            raise Conflict("Create one new workbook. Use edit_document for a saved revision.")
        if name in ("inspect_document", "check_document") and not self.allowed(args.file_id):
            raise Conflict("Choose an approved input or this assignment's saved output.")
        if name == "edit_document" and (
            not self.output
            or self.output["id"] != args.file_id
            or args.name not in (None, self.output["name"])
        ):
            raise Conflict(
                "Only the current revision of this assignment's output can be edited in place."
            )
        result = OwlDocuments(self.store, self.owner, self.token).call(
            self.task, name, args, image_input=image_input
        )
        receipt = getattr(result, "observation", result)
        if name in ("create_document", "edit_document") and receipt.get("saved"):
            self.output = {k: receipt[k] for k in ("id", "name", "sha256")}
            self.output["checks"] = receipt.get("checks")
            self.store.checkpoint_assignment_result(
                self.owner,
                self.task["id"],
                self.output["name"],
                output_file_ids=[self.output["id"]],
            )
        return result

    def workbook_review(self, read_model):
        # Reopen the immutable output for the reviewer, including after restart.
        sample = self.read_file(
            self.task["bot_id"],
            read_model(file_id=self.output["id"], length=12000),
            image_input=False,
        )
        worksheet_evidence = {}
        if "xlsx" in ready_formats():
            inspected = self.document(
                "inspect_document", LibraryDocument(file_id=self.output["id"]), image_input=False
            )
            if inspected["source_sha256"] != self.output["sha256"]:
                raise Conflict("The workbook's reviewed bytes do not match its saved revision.")
            contents = inspected["content"]
            formulas = [
                {"sheet": sheet["name"], "cell": cell["cell"], "formula": cell["formula"]}
                for sheet in contents["sheets"]
                for cell in sheet["cells"]
                if "formula" in cell
            ][:12]
            worksheet_evidence = {
                "formula_count": contents["formula_count"],
                "formula_examples": formulas,
            }
        return {
            "workbook_file": self.output["name"],
            "workbook_contents": sample.get("content") if isinstance(sample, dict) else None,
            "workbook_contents_partial": bool(sample.get("next_offset")),
            "workbook_sha256": self.output["sha256"],
            "input_samples": list(self.input_samples.values()),
            **worksheet_evidence,
            **({"artifact_checks": self.output["checks"]} if self.output.get("checks") else {}),
        }


class WorkbookSheet(Input):
    model_config = ConfigDict(str_strip_whitespace=False)

    name: str = Field(min_length=1, max_length=31)
    rows: list[list[StrictStr | StrictInt | StrictFloat | StrictBool | None]] = Field(
        min_length=1, max_length=1000
    )


class CreateWorkbook(Input):
    name: str = Field(min_length=1, max_length=100)
    sheets: list[WorkbookSheet] = Field(min_length=1, max_length=8)


def workbook_available() -> bool:
    return find_spec("openpyxl") is not None


def workbook_text(data: CreateWorkbook, max_chars: int = 12_000) -> str:
    """The workbook's sheets and cells as text, one row a line, for checking what was saved.
    Rows past ``max_chars`` are counted rather than shown."""
    lines: list[str] = []
    used = 0
    left_out = 0
    for sheet in data.sheets:
        heading = f"Sheet {json.dumps(sheet.name, ensure_ascii=False)}:"
        for index, line in enumerate(
            [heading, *(json.dumps(row, ensure_ascii=False) for row in sheet.rows)]
        ):
            if left_out or used + len(line) + 1 > max_chars:
                left_out += index > 0
                continue
            lines.append(line)
            used += len(line) + 1
    if left_out:
        lines.append(f"({left_out} more rows not shown)")
    return "\n".join(lines)


def save_workbook(store: Store, owner: str, task: dict, data: CreateWorkbook) -> dict:
    from openpyxl import Workbook

    cells = [cell for sheet in data.sheets for row in sheet.rows for cell in row]
    if (
        not cells
        or len(cells) > MAX_CELLS
        or any(isinstance(cell, str) and len(cell) > 2000 for cell in cells)
    ):
        raise Conflict("Use at most 10,000 cells and up to 2,000 characters per cell.")
    stem = data.name.removesuffix(".xlsx")
    # Use a task-specific name so separate runs never replace an earlier workbook.
    file = FileInput(name=f"{stem} ({task['id'][:10]}).xlsx", content="")
    workbook = Workbook()
    # OpenPyXL otherwise emits an empty workbookProtection marker. These newly authored
    # files are unprotected and can be revised by the Office editor after a review.
    workbook.security = None
    workbook.remove(workbook.active)
    for sheet_data in data.sheets:
        sheet = workbook.create_sheet(sheet_data.name)
        for row_number, row in enumerate(sheet_data.rows, start=1):
            for column, value in enumerate(row, start=1):
                cell = sheet.cell(row_number, column, value)
                if isinstance(value, str):
                    # A value copied from screen or supplied data must remain literal text.
                    cell.data_type = "s"
        sheet.freeze_panes = "A2"
    with BytesIO() as output:
        workbook.save(output)
        saved = store.save_file(
            owner, task["bot_id"], file, source_task_id=task["id"], payload=output.getvalue()
        )
    return {"id": saved["id"], "name": saved["name"], "sha256": saved["sha256"]}
