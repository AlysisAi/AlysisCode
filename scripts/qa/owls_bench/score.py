"""Score benchmark runs: deterministic checks first, from the produced files and the reply
only, then the optional judge.

Each run folder (``<out>/runs/<system>/<task>/run-<n>/``) holds the runner's
``record.json`` and the delivered files. Scoring writes ``score.json`` beside them, so a
benchmark can be re-scored (for example with a judge added later) without running it again:

    python3 -m scripts.qa.owls_bench.score --out .agent-notes/owls-bench/<run>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import judge as judge_module
from .documents import (
    Inspection,
    Table,
    cell_value,
    count_sources,
    find_urls,
    inspect_file,
    list_items,
    markdown_tables,
    numbers_in,
)
from .language import check_reply_script
from .tasks import TASKS_DIR, Task, TaskError, load_tasks

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
FILE_TEXT_LIMIT = 8_000
MAX_DECIMALS = 6
# How a run that did not finish ended, in words.
_ENDINGS = {
    "blocked": "stopped on a card waiting for the person",
    "asked": "ended by asking the person something",
    "timeout": "ran out of time",
    "exit_nonzero": "exited with an error code",
    "error": "could not run",
}
# File names that claim a deliverable kind.
_SUFFIXES = {
    "xlsx": (".xlsx", ".xlsm"),
    "docx": (".docx",),
    "pptx": (".pptx",),
    "pdf": (".pdf",),
    "csv": (".csv", ".tsv"),
    "text": (".md", ".markdown", ".txt"),
}


def _check(check_id: str, status: str, detail: str, **extra) -> dict:
    return {"id": check_id, "status": status, "detail": detail, **extra}


def scored_attempt(record: dict) -> dict:
    """The attempt a run is scored on: the last one (the timed repeat of a repeat_job)."""
    attempts = record.get("attempts") or []
    return attempts[-1] if attempts else {}


def inspect_attempt(record_dir: Path, attempt: dict) -> list[Inspection]:
    inspections = []
    for relative in attempt.get("files") or []:
        path = (record_dir / relative).resolve()
        if record_dir.resolve() not in path.parents or not path.is_file():
            continue
        inspection = inspect_file(path)
        inspection.name = relative
        inspections.append(inspection)
    return inspections


# --- Totals ------------------------------------------------------------------------------

_REF = re.compile(r"^\$?([A-Z]{1,3})\$?(\d{1,7})$")


def _ref_parts(ref: str) -> tuple[int, int] | None:
    match = _REF.match(ref.strip().upper())
    if not match:
        return None
    column = 0
    for ch in match.group(1):
        column = column * 26 + ord(ch) - 64
    return column - 1, int(match.group(2))


def _formula_value(formula: str, refs: dict, decimal_mark: str) -> tuple[float, int] | None:
    """The value of a same-sheet SUM(...) or a+b+c formula over plain cells, if it is one."""
    text = formula.strip().lstrip("=").upper().replace(" ", "")
    match = re.fullmatch(r"SUM\((.+)\)", text)
    if match:
        arguments = re.split(r"[,;]", match.group(1))
    elif re.fullmatch(r"\$?[A-Z]{1,3}\$?\d+(?:\+\$?[A-Z]{1,3}\$?\d+)+", text):
        arguments = text.split("+")
    else:
        return None
    total, decimals = 0.0, 0
    for argument in arguments:
        if "!" in argument:
            return None
        ends = argument.split(":")
        if len(ends) not in (1, 2):
            return None
        corners = [_ref_parts(end) for end in ends]
        if any(corner is None for corner in corners):
            return None
        (c1, r1), (c2, r2) = corners[0], corners[-1]
        if (c2 - c1 + 1) * (r2 - r1 + 1) > 100_000:
            return None
        for column in range(min(c1, c2), max(c1, c2) + 1):
            for row in range(min(r1, r2), max(r1, r2) + 1):
                letters = ""
                index = column + 1
                while index:
                    index, rest = divmod(index - 1, 26)
                    letters = chr(65 + rest) + letters
                cell = refs.get(f"{letters}{row}")
                value = cell_value(cell, decimal_mark) if cell is not None else None
                if value is not None:
                    total += value[0]
                    decimals = max(decimals, value[1])
    return total, decimals


def _tolerance(value: float, decimals: int) -> float:
    return 0.5 * 10 ** -min(decimals, MAX_DECIMALS) + 1e-9 * max(1.0, abs(value))


def reconcile_table(table: Table) -> dict | None:
    """A numeric column whose total cell equals the sum of the cells above it.

    The total is one of the column's last three numbers (a total row may be followed by an
    average or a note). Text numbers are read with either decimal mark, the same mark for
    the whole column. A SUM formula without a saved value is evaluated over its cells. A
    plain 1, 2, 3 ... numbering column is not a total.
    """
    rows = table.nonempty_rows()
    if len(rows) < 4:
        return None
    refs = table.by_ref()
    width = max(len(row) for row in rows)
    for decimal_mark in (".", ","):
        for column in range(width):
            values = []
            for index, row in enumerate(rows[1:], start=1):
                if column >= len(row):
                    continue
                cell = row[column]
                value = cell_value(cell, decimal_mark)
                if value is None and cell.formula:
                    value = _formula_value(cell.formula, refs, decimal_mark)
                if value is not None:
                    values.append((index, value[0], value[1]))
            if len(values) < 3:
                continue
            numbers = [value for _, value, _ in values]
            if numbers == [float(n) for n in range(1, len(numbers) + 1)]:
                continue
            for position in range(len(values) - 1, max(len(values) - 4, 1), -1):
                parts = numbers[:position]
                total_row, total, total_decimals = values[position]
                if len(parts) < 2:
                    break
                if abs(sum(parts) - total) <= _tolerance(total, total_decimals):
                    return {
                        "table": table.name,
                        "column": column + 1,
                        "total_row": total_row + 1,
                        "total": total,
                        "parts": len(parts),
                        "decimal_mark": decimal_mark,
                    }
    return None


# --- Values ------------------------------------------------------------------------------


def _readings(inspections: list[Inspection], reply: str) -> list[tuple[float, int]]:
    readings: list[tuple[float, int]] = []
    for inspection in inspections:
        for table in inspection.tables:
            for row in table.rows:
                for cell in row:
                    if cell.number is not None:
                        readings.append((cell.number, cell.decimals or 0))
                    elif cell.text:
                        readings.extend(numbers_in(cell.text))
        readings.extend(numbers_in(inspection.text))
    readings.extend(numbers_in(reply))
    return readings


def value_found(expected: float, decimals: int, readings: list[tuple[float, int]]) -> bool:
    """A written number equal to the expected value at the precision it was written with
    (never coarser than whole units), or at the expected precision if finer."""
    for value, written in readings:
        precision = min(written, decimals)
        if abs(value - expected) <= _tolerance(expected, precision):
            return True
    return False


# --- Checks ------------------------------------------------------------------------------


def _chart_delivered(inspections: list[Inspection]) -> Inspection | None:
    for inspection in inspections:
        if not inspection.opens:
            continue
        if inspection.kind == "image" or inspection.has_chart:
            return inspection
        if inspection.kind in ("xlsx", "docx", "pptx") and inspection.has_image:
            return inspection
    return None


def deterministic_checks(task: Task, record: dict, inspections: list[Inspection]) -> list[dict]:
    expect = task.expect
    attempts = record.get("attempts") or []
    attempt = scored_attempt(record)
    reply = attempt.get("reply") or ""
    status = attempt.get("status") or "not run"
    checks = [
        _check(
            "finished",
            PASS if status == "completed" else FAIL,
            "The run finished."
            if status == "completed"
            else f"The run {_ENDINGS.get(status, 'ended as ' + status)}.",
        )
    ]
    if expect.repeat > 1:
        done = sum(item.get("status") == "completed" for item in attempts)
        checks.append(
            _check(
                "repeat_completed",
                PASS if done == expect.repeat == len(attempts) else FAIL,
                f"{done} of {expect.repeat} repeats finished.",
            )
        )
    opened = [item for item in inspections if item.opens]
    if expect.deliverable == "none":
        targets: list[Inspection] = []
        tables = markdown_tables(reply)
        checks.append(
            _check(
                "reply_present",
                PASS if reply.strip() else FAIL,
                "The answer is in the reply." if reply.strip() else "The reply is empty.",
            )
        )
        if expect.min_items:
            items = list_items(reply)
            checks.append(
                _check(
                    "min_items",
                    PASS if items >= expect.min_items else FAIL,
                    f"{items} items in the reply, {expect.min_items} required.",
                    observed=items,
                    expected=expect.min_items,
                )
            )
    else:
        # A file counts as the deliverable when it opens as the expected kind; one that only
        # carries the kind's name (report.xlsx that is not a workbook) is named as broken.
        suffixes = _SUFFIXES.get(expect.deliverable, ())
        matching = [
            item
            for item in inspections
            if item.kind == expect.deliverable or Path(item.name).suffix.lower() in suffixes
        ]
        targets = [item for item in matching if item.opens and item.kind == expect.deliverable]
        if targets:
            detail = f"{', '.join(item.name for item in targets)} opens as {expect.deliverable}."
        elif matching:
            detail = "; ".join(
                f"{item.name} does not open as {expect.deliverable}: "
                f"{item.error or 'it is ' + item.kind}"
                for item in matching
            )
        else:
            kinds = sorted({item.kind for item in inspections}) or ["no files"]
            detail = f"No {expect.deliverable} file was delivered (found: {', '.join(kinds)})."
        checks.append(_check("deliverable_opens", PASS if targets else FAIL, detail))
        tables = [table for item in targets for table in item.tables]
    if expect.min_rows:
        rows = max((table.data_rows for table in tables), default=0)
        checks.append(
            _check(
                "min_rows",
                PASS if rows >= expect.min_rows else FAIL,
                f"{rows} rows under the header, {expect.min_rows} required.",
                observed=rows,
                expected=expect.min_rows,
            )
        )
    if expect.columns:
        widest = max(tables, key=lambda table: table.data_rows, default=None)
        width = widest.header_width if widest else 0
        checks.append(
            _check(
                "columns",
                PASS if width >= len(expect.columns) else FAIL,
                f"The main table has {width} header cells, {len(expect.columns)} required; "
                "whether they hold the right information is left to the judge.",
                observed=width,
                expected=len(expect.columns),
            )
        )
    if expect.min_slides or expect.max_slides:
        slides = max((item.slides or 0 for item in targets), default=0)
        low, high = expect.min_slides or 1, expect.max_slides
        within = slides >= low and (high is None or slides <= high)
        wanted = f"exactly {low}" if low == high else f"{low} to {high or 'any'}"
        checks.append(
            _check(
                "slides",
                PASS if within else FAIL,
                f"{slides} slides, {wanted} required.",
                observed=slides,
                expected=[low, high],
            )
        )
    if expect.min_pages:
        known = [item.pages for item in targets if item.pages is not None]
        if known:
            pages = max(known)
            checks.append(
                _check(
                    "min_pages",
                    PASS if pages >= expect.min_pages else FAIL,
                    f"{pages} pages, {expect.min_pages} required.",
                    observed=pages,
                    expected=expect.min_pages,
                )
            )
        else:
            checks.append(
                _check(
                    "min_pages",
                    UNKNOWN if targets else FAIL,
                    "The page count could not be read from the file."
                    if targets
                    else "No file to count pages in.",
                )
            )
    links = [link for item in opened for link in item.links] + find_urls(reply)
    sources = count_sources(links)
    if links or expect.min_sources:
        bad = sources["not_web_count"]
        checks.append(
            _check(
                "links_are_web",
                PASS if links and not bad else FAIL,
                f"{bad} links are not http(s) web addresses: {', '.join(sources['not_web'][:5])}."
                if bad
                else (f"All {len(links)} links are http(s)." if links else "No links were found."),
            )
        )
    if expect.min_sources:
        unit = "distinct_hosts" if expect.source_unit == "host" else "distinct_urls"
        observed = sources[unit]
        label = "sites" if expect.source_unit == "host" else "pages"
        checks.append(
            _check(
                "min_sources",
                PASS if observed >= expect.min_sources else FAIL,
                f"{observed} distinct source {label}, {expect.min_sources} required.",
                observed=observed,
                expected=expect.min_sources,
            )
        )
    if expect.totals_reconcile:
        found = next((r for r in map(reconcile_table, tables) if r is not None), None)
        checks.append(
            _check(
                "totals_reconcile",
                PASS if found else FAIL,
                f"Column {found['column']} of {found['table']} sums to its total "
                f"in row {found['total_row']}."
                if found
                else "No numeric column's total equals the sum of the values above it.",
                **({"where": found} if found else {}),
            )
        )
    if expect.values:
        readings = _readings(opened, reply)
        for index, item in enumerate(expect.values, start=1):
            found_value = value_found(item.value, item.decimals, readings)
            checks.append(
                _check(
                    f"value_{index}",
                    PASS if found_value else FAIL,
                    f"{item.meaning}: {item.value:.{item.decimals}f} "
                    + ("appears in the work." if found_value else "was not found."),
                    expected=item.value,
                )
            )
    if expect.chart:
        chart = _chart_delivered(opened)
        checks.append(
            _check(
                "chart",
                PASS if chart else FAIL,
                f"{chart.name} holds a chart or picture." if chart else "No chart was delivered.",
            )
        )
    script = check_reply_script(reply, expect.reply_language)
    checks.append(
        _check(
            "reply_script",
            script["status"],
            script["detail"],
            language_identity=script["language_identity"],
            shares=script["shares"],
        )
    )
    return checks


def summarize_checks(checks: list[dict]) -> dict:
    passed = sum(check["status"] == PASS for check in checks)
    return {
        "passed": passed,
        "applicable": len(checks),
        "unknown": sum(check["status"] == UNKNOWN for check in checks),
        "all_passed": passed == len(checks),
        "pass_rate": round(passed / len(checks), 4) if checks else None,
    }


def metrics(record: dict) -> dict:
    """The numbers the report aggregates, from the scored attempt (and the first one)."""
    attempts = record.get("attempts") or []
    last = scored_attempt(record)
    first = attempts[0] if attempts else {}
    tokens = last.get("tokens") or {}
    return {
        "status": last.get("status"),
        "wall_s": last.get("wall_s"),
        "model_calls": last.get("model_calls"),
        "tokens": tokens.get("total"),
        "tokens_complete": tokens.get("unreported_calls") == 0 if tokens else None,
        "cost_usd": last.get("cost_usd"),
        "approvals": last.get("approvals"),
        "questions": last.get("questions"),
        "failures": last.get("failures"),
        "first_wall_s": first.get("wall_s") if len(attempts) > 1 else None,
        "first_model_calls": first.get("model_calls") if len(attempts) > 1 else None,
    }


def files_text(inspections: list[Inspection], limit: int = FILE_TEXT_LIMIT) -> str:
    parts = []
    for item in inspections:
        header = f"== {item.name} ({item.kind}{'' if item.opens else ', does not open'}) =="
        body = item.text if item.opens else (item.error or "")
        if len(body) > limit:
            body = body[:limit] + f"\n[... {len(body) - limit} more characters not shown]"
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


def receipts(record: dict, checks: list[dict], inspections: list[Inspection]) -> dict:
    attempt = scored_attempt(record)
    return {
        "system": record.get("system"),
        "attempts": len(record.get("attempts") or []),
        "status": attempt.get("status"),
        "stopped_on": attempt.get("stopped_on") or [],
        "wall_s": attempt.get("wall_s"),
        "model_calls": attempt.get("model_calls"),
        "tokens": attempt.get("tokens"),
        "approvals": attempt.get("approvals"),
        "questions": attempt.get("questions"),
        "failures": attempt.get("failures"),
        "tool_steps": attempt.get("tool_steps"),
        "sites_visited": attempt.get("hosts"),
        "files": [item.summary() for item in inspections],
        "deterministic_checks": [
            {"id": check["id"], "status": check["status"], "detail": check["detail"]}
            for check in checks
        ],
    }


def score_record(
    task: Task,
    record_dir: Path,
    *,
    judge: tuple | None = None,
    opener=None,
) -> dict:
    """Score one run folder and write its score.json. ``judge`` is (prompts, config)."""
    record = json.loads((record_dir / "record.json").read_text(encoding="utf-8"))
    attempt = scored_attempt(record)
    inspections = inspect_attempt(record_dir, attempt)
    checks = deterministic_checks(task, record, inspections)
    links = [link for item in inspections if item.opens for link in item.links]
    result = {
        "schema": 1,
        "system": record.get("system"),
        "label": record.get("label"),
        "task_id": task.id,
        "task_sha256": task.sha256,
        "recorded_task_sha256": record.get("task_sha256"),
        "split": task.split,
        "category": task.category,
        "language": task.language,
        "run": record.get("run"),
        "checks": checks,
        "deterministic": summarize_checks(checks),
        "files": [item.summary() for item in inspections],
        "sources": count_sources(links + find_urls(attempt.get("reply") or "")),
        "metrics": metrics(record),
        "judge": None,
    }
    if judge is not None:
        prompts, config = judge
        result["judge"] = judge_module.judge_run(
            prompts,
            config,
            task={
                "id": task.id,
                "category": task.category,
                "prompt": task.prompt,
                "language": task.language,
                "attachments": [path.name for path in task.attachments],
                "expect": task.expect.as_dict(),
            },
            category=task.category,
            reply=attempt.get("reply") or "",
            files_text=files_text(inspections),
            receipts=receipts(record, checks, inspections),
            opener=opener,
        )
    (record_dir / "score.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def record_dirs(out: Path) -> list[Path]:
    return sorted(path.parent for path in (out / "runs").glob("*/*/run-*/record.json"))


def score_out(out: Path, tasks: list[Task], *, judge: tuple | None = None) -> list[dict]:
    by_id = {task.id: task for task in tasks}
    scores = []
    for record_dir in record_dirs(out):
        task = by_id.get(record_dir.parent.name)
        if task is None:
            print(f"Skipping {record_dir}: its task is not in the task files.", file=sys.stderr)
            continue
        scores.append(score_record(task, record_dir, judge=judge))
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score recorded owls benchmark runs again.")
    parser.add_argument("--out", type=Path, required=True, help="the benchmark output folder")
    parser.add_argument("--tasks", type=Path, default=TASKS_DIR, help="folder of task files")
    judge_module.add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(args.tasks)
        config = judge_module.config_from(args)
        judge = (judge_module.verify(), config) if config else None
    except (TaskError, ValueError, judge_module.JudgeIntegrityError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    scores = score_out(args.out, tasks, judge=judge)
    from .report import write_report

    report = write_report(args.out, judge=judge)
    print(f"Scored {len(scores)} runs; report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
