"""Benchmark task files: loading, the schema check, and the shipped set's composition rules.

A task file is JSON: ``{"schema": 1, "tasks": [...]}`` (one file per category) or
``{"schema": 1, "task": {...}}`` (one file per task). Attachments are paths relative to the
task file's folder. Run ``python3 -m scripts.qa.owls_bench.tasks`` to check the shipped set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

TASKS_DIR = Path(__file__).resolve().parent / "tasks"
SCHEMA_VERSION = 1
CATEGORIES = (
    "research_to_spreadsheet",
    "comparison_to_document",
    "deck",
    "website_task",
    "price_check",
    "data_file",
    "long_job",
    "repeat_job",
)
SPLITS = ("dev", "heldout")
# What the task must deliver: a file of this kind, or "none" for an answer in the reply.
DELIVERABLE_KINDS = ("xlsx", "docx", "pptx", "pdf", "csv", "text", "none")
SOURCE_UNITS = ("url", "host")
MAX_PROMPT_CHARS = 20_000
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_S = 1800
LONG_JOB_MIN_SOURCES = 20

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")
_TASK_KEYS = {
    "id",
    "category",
    "language",
    "split",
    "prompt",
    "attachments",
    "expect",
    "timeout_s",
}
_EXPECT_KEYS = {
    "deliverable",
    "min_rows",
    "min_slides",
    "max_slides",
    "min_pages",
    "min_items",
    "columns",
    "min_sources",
    "source_unit",
    "totals_reconcile",
    "values",
    "chart",
    "reply_language",
    "repeat",
}
# Which counts each deliverable kind can be checked for.
_ROWS_KINDS = {"xlsx", "csv", "docx", "text", "none"}
_PAGES_KINDS = {"pdf", "docx"}


class TaskError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ExpectedValue:
    meaning: str
    value: float
    decimals: int


@dataclass(frozen=True)
class Expectations:
    deliverable: str
    reply_language: str
    min_rows: int | None = None
    min_slides: int | None = None
    max_slides: int | None = None
    min_pages: int | None = None
    min_items: int | None = None
    columns: tuple[str, ...] = ()
    min_sources: int = 0
    source_unit: str = "url"
    totals_reconcile: bool = False
    values: tuple[ExpectedValue, ...] = ()
    chart: bool = False
    repeat: int = 1

    def as_dict(self) -> dict:
        return {
            "deliverable": self.deliverable,
            "reply_language": self.reply_language,
            "min_rows": self.min_rows,
            "min_slides": self.min_slides,
            "max_slides": self.max_slides,
            "min_pages": self.min_pages,
            "min_items": self.min_items,
            "columns": list(self.columns),
            "min_sources": self.min_sources,
            "source_unit": self.source_unit,
            "totals_reconcile": self.totals_reconcile,
            "values": [
                {"meaning": v.meaning, "value": v.value, "decimals": v.decimals}
                for v in self.values
            ],
            "chart": self.chart,
            "repeat": self.repeat,
        }


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    language: str
    split: str
    prompt: str
    expect: Expectations
    attachments: tuple[Path, ...] = ()
    timeout_s: int | None = None
    source: Path | None = field(default=None, compare=False)
    sha256: str = ""

    @property
    def primary_language(self) -> str:
        return self.language.split("-")[0].lower()


def _is_int(value, low: int, high: int | None = None) -> bool:
    # bool is an int subclass; a true/false is never a count.
    return type(value) is int and value >= low and (high is None or value <= high)


def _check_expect(task_id: str, category: str, expect, problems: list[str]) -> None:
    where = f"{task_id}: expect"
    if not isinstance(expect, dict):
        problems.append(f"{where} must be an object")
        return
    for key in sorted(set(expect) - _EXPECT_KEYS):
        problems.append(f"{where} has an unknown key {key!r}")
    kind = expect.get("deliverable")
    if kind not in DELIVERABLE_KINDS:
        problems.append(f"{where}.deliverable must be one of {', '.join(DELIVERABLE_KINDS)}")
    language = expect.get("reply_language")
    if not isinstance(language, str) or not _LANGUAGE_TAG.match(language):
        problems.append(f"{where}.reply_language must be a BCP-47 language tag")
    for key in ("min_rows", "min_slides", "max_slides", "min_pages", "min_items"):
        if key in expect and not _is_int(expect[key], 1, 10_000):
            problems.append(f"{where}.{key} must be a whole number from 1")
    if "min_rows" in expect and kind not in _ROWS_KINDS:
        problems.append(f"{where}.min_rows does not apply to a {kind} deliverable")
    if ("min_slides" in expect or "max_slides" in expect) and kind != "pptx":
        problems.append(f"{where}: slide counts apply only to a pptx deliverable")
    if "min_pages" in expect and kind not in _PAGES_KINDS:
        problems.append(f"{where}.min_pages applies only to pdf or docx deliverables")
    if "min_items" in expect and kind != "none":
        problems.append(f"{where}.min_items counts items in a reply-only answer")
    if (
        _is_int(expect.get("min_slides"), 1)
        and _is_int(expect.get("max_slides"), 1)
        and expect["min_slides"] > expect["max_slides"]
    ):
        problems.append(f"{where}: min_slides is larger than max_slides")
    columns = expect.get("columns", [])
    if not isinstance(columns, list) or any(
        not isinstance(text, str) or not text.strip() or len(text) > 200 for text in columns
    ):
        problems.append(f"{where}.columns must be a list of short descriptions")
    if "min_sources" in expect and not _is_int(expect["min_sources"], 0, 10_000):
        problems.append(f"{where}.min_sources must be a whole number from 0")
    if expect.get("source_unit", "url") not in SOURCE_UNITS:
        problems.append(f"{where}.source_unit must be one of {', '.join(SOURCE_UNITS)}")
    for key in ("totals_reconcile", "chart"):
        if key in expect and type(expect[key]) is not bool:
            problems.append(f"{where}.{key} must be true or false")
    values = expect.get("values", [])
    if not isinstance(values, list):
        problems.append(f"{where}.values must be a list")
        values = []
    for index, item in enumerate(values):
        if (
            not isinstance(item, dict)
            or set(item) != {"meaning", "value", "decimals"}
            or not isinstance(item.get("meaning"), str)
            or not item["meaning"].strip()
            or isinstance(item.get("value"), bool)
            or not isinstance(item.get("value"), (int, float))
            or not _is_int(item.get("decimals"), 0, 10)
        ):
            problems.append(
                f"{where}.values[{index}] needs a meaning, a numeric value and decimals (0-10)"
            )
    repeat = expect.get("repeat", 1)
    if not _is_int(repeat, 1, 5):
        problems.append(f"{where}.repeat must be a whole number from 1 to 5")
    if category == "repeat_job" and not (_is_int(repeat, 2, 5)):
        problems.append(f"{where}.repeat must be at least 2 for a repeat_job")
    if category != "repeat_job" and repeat != 1:
        problems.append(f"{where}.repeat is only for repeat_job tasks")
    if category == "long_job" and not (
        _is_int(expect.get("min_sources"), LONG_JOB_MIN_SOURCES)
        or _is_int(expect.get("min_pages"), LONG_JOB_MIN_SOURCES)
    ):
        problems.append(
            f"{where}: a long_job must need at least {LONG_JOB_MIN_SOURCES} sources or pages"
        )
    if kind == "none" and expect.get("totals_reconcile"):
        problems.append(f"{where}.totals_reconcile needs a file with a table")


def _attachment_path(base: Path, value, task_id: str, problems: list[str]) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{task_id}: each attachment must be a relative path")
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        problems.append(f"{task_id}: attachment {value!r} must stay inside the task folder")
        return None
    path = base.joinpath(*relative.parts)
    if not path.is_file():
        problems.append(f"{task_id}: attachment {value!r} does not exist")
        return None
    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        problems.append(f"{task_id}: attachment {value!r} is larger than 50 MiB")
        return None
    return path


def _digest(raw: dict, attachments: list[Path]) -> str:
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    )
    for path in attachments:
        digest.update(b"\0" + path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def parse_task(raw, *, base: Path, source: Path | None = None) -> tuple[Task | None, list[str]]:
    """One task from its JSON object, and every schema problem found in it."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["a task must be an object"]
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not _ID.match(task_id) or len(task_id) > 60:
        problems.append(f"task id {task_id!r} must be lower-case words joined by hyphens")
        task_id = str(task_id)
    for key in sorted(set(raw) - _TASK_KEYS):
        problems.append(f"{task_id}: unknown key {key!r}")
    for key in ("id", "category", "language", "split", "prompt", "expect"):
        if key not in raw:
            problems.append(f"{task_id}: missing {key!r}")
    category = raw.get("category")
    if category not in CATEGORIES:
        problems.append(f"{task_id}: category must be one of {', '.join(CATEGORIES)}")
    language = raw.get("language")
    if not isinstance(language, str) or not _LANGUAGE_TAG.match(language):
        problems.append(f"{task_id}: language must be a BCP-47 tag such as el or zh-Hans")
    if raw.get("split") not in SPLITS:
        problems.append(f"{task_id}: split must be dev or heldout")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        problems.append(f"{task_id}: prompt must be text of at most {MAX_PROMPT_CHARS} characters")
    timeout = raw.get("timeout_s")
    if timeout is not None and not _is_int(timeout, 60, 86_400):
        problems.append(f"{task_id}: timeout_s must be seconds from 60 to 86400")
    attachments_raw = raw.get("attachments", [])
    attachments: list[Path] = []
    if not isinstance(attachments_raw, list):
        problems.append(f"{task_id}: attachments must be a list")
        attachments_raw = []
    for value in attachments_raw:
        path = _attachment_path(base, value, task_id, problems)
        if path is not None:
            attachments.append(path)
    if category == "data_file" and not attachments_raw:
        problems.append(f"{task_id}: a data_file task needs an attached file")
    if len({path.name for path in attachments}) != len(attachments):
        problems.append(f"{task_id}: attachment file names must differ")
    expect = raw.get("expect")
    _check_expect(task_id, category, expect, problems)
    if problems:
        return None, problems
    expectations = Expectations(
        deliverable=expect["deliverable"],
        reply_language=expect["reply_language"],
        min_rows=expect.get("min_rows"),
        min_slides=expect.get("min_slides"),
        max_slides=expect.get("max_slides"),
        min_pages=expect.get("min_pages"),
        min_items=expect.get("min_items"),
        columns=tuple(text.strip() for text in expect.get("columns", [])),
        min_sources=expect.get("min_sources", 0),
        source_unit=expect.get("source_unit", "url"),
        totals_reconcile=expect.get("totals_reconcile", False),
        values=tuple(
            ExpectedValue(item["meaning"], float(item["value"]), item["decimals"])
            for item in expect.get("values", [])
        ),
        chart=expect.get("chart", False),
        repeat=expect.get("repeat", 1),
    )
    task = Task(
        id=task_id,
        category=category,
        language=language,
        split=raw["split"],
        prompt=prompt,
        expect=expectations,
        attachments=tuple(attachments),
        timeout_s=timeout,
        source=source,
        sha256=_digest(raw, attachments),
    )
    return task, []


def load_tasks(folder: Path = TASKS_DIR) -> list[Task]:
    """Every task in the folder's JSON files (not in subfolders), checked against the schema.

    Raises TaskError listing every problem found, so one run shows them all.
    """
    folder = Path(folder)
    problems: list[str] = []
    tasks: list[Task] = []
    files = sorted(folder.glob("*.json"))
    if not files:
        raise TaskError([f"{folder} holds no task files"])
    for path in files:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: not readable JSON ({exc})")
            continue
        if not isinstance(document, dict) or document.get("schema") != SCHEMA_VERSION:
            problems.append(f'{path.name}: needs "schema": {SCHEMA_VERSION}')
            continue
        if set(document) - {"schema", "tasks", "task"} or ("tasks" in document) == (
            "task" in document
        ):
            problems.append(f'{path.name}: holds either "tasks" (a list) or "task" (one)')
            continue
        raws = document["tasks"] if "tasks" in document else [document["task"]]
        if not isinstance(raws, list) or not raws:
            problems.append(f'{path.name}: "tasks" must be a non-empty list')
            continue
        for raw in raws:
            task, found = parse_task(raw, base=path.parent, source=path)
            problems.extend(f"{path.name}: {problem}" for problem in found)
            if task is not None:
                tasks.append(task)
    counts = Counter(task.id for task in tasks)
    problems.extend(
        f"task id {task_id!r} is used more than once" for task_id, n in counts.items() if n > 1
    )
    if problems:
        raise TaskError(problems)
    return tasks


def select(tasks: list[Task], *, split: str = "dev", only: list[str] | None = None) -> list[Task]:
    """Tasks of one split (or "all"), optionally only the named ids (which must exist)."""
    if split not in (*SPLITS, "all"):
        raise ValueError("split must be dev, heldout or all")
    chosen = [task for task in tasks if split == "all" or task.split == split]
    if only:
        known = {task.id for task in tasks}
        missing = [task_id for task_id in only if task_id not in known]
        if missing:
            raise ValueError(f"unknown task ids: {', '.join(missing)}")
        wanted = set(only)
        chosen = [task for task in chosen if task.id in wanted]
    return chosen


def check_task_set(tasks: list[Task]) -> list[str]:
    """The shipped set's composition: 8 categories x 3 tasks, a third or more not in English,
    and each category split between dev and held-out as evenly as possible."""
    problems: list[str] = []
    by_category = Counter(task.category for task in tasks)
    if len(tasks) != 3 * len(CATEGORIES):
        problems.append(f"expected {3 * len(CATEGORIES)} tasks, found {len(tasks)}")
    for category in CATEGORIES:
        if by_category[category] != 3:
            problems.append(f"{category} has {by_category[category]} tasks, expected 3")
    other = sum(task.primary_language != "en" for task in tasks)
    if tasks and other * 3 < len(tasks):
        problems.append(f"only {other} of {len(tasks)} tasks are not in English")
    for category in CATEGORIES:
        splits = Counter(task.split for task in tasks if task.category == category)
        if by_category[category] >= 2 and (not splits["dev"] or not splits["heldout"]):
            problems.append(f"{category} needs both dev and held-out tasks")
        if abs(splits["dev"] - splits["heldout"]) > 1:
            problems.append(f"{category} is not split evenly between dev and held-out")
    overall = Counter(task.split for task in tasks)
    if abs(overall["dev"] - overall["heldout"]) > 1:
        problems.append(
            f"dev has {overall['dev']} tasks and held-out {overall['heldout']}; keep them even"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the owls benchmark task files.")
    parser.add_argument("--tasks", type=Path, default=TASKS_DIR, help="folder of task files")
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(args.tasks)
    except TaskError as exc:
        print("Task files have problems:", file=sys.stderr)
        for problem in exc.problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    problems = check_task_set(tasks)
    languages = Counter(task.primary_language for task in tasks)
    splits = Counter(task.split for task in tasks)
    print(f"{len(tasks)} tasks; splits {dict(splits)}; languages {dict(sorted(languages.items()))}")
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
