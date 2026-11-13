#!/usr/bin/env python3
"""Canary suite: a fast regression tripwire on always-solved benchmark tasks.

WHAT THIS IS
------------
The regression suite (``scripts/regression_suite.py``) proves the six defects
stay fixed. The canary suite proves the wave did not *break* anything that used
to work: it runs a small, stratified set of tasks that the pre-fix build solved
3/3, and asserts they still pass -- and reports dispatch overhead and cost per
task so a silent efficiency regression is visible too.

Same split as the regression suite:

* a **dependency-light, importable core** (task-list parsing, per-task metric
  extraction from artifacts, aggregation, and the baseline comparison table).
  Standard library only; ``tests/test_canary_suite.py`` exercises it with
  synthetic fixtures.
* a **thin Harbor-invoking wrapper** (``_run_live``), guarded behind ``--run``.

USAGE
-----
Offline, over an existing archive laid out as ``<dir>/<task>/...`` ::

    python3 scripts/canary_suite.py --check-artifacts ./runs/canary
    python3 scripts/canary_suite.py --check-artifacts ./runs/canary --baseline baseline.json

Live (runner box)::

    python3 scripts/canary_suite.py --run \
        --tasks scripts/canary_tasks.txt \
        --harbor-cmd 'bash scripts/benchmarks/terminal_bench/run_harbor_tbench.sh' \
        --artifacts-root ./runs/canary

Exit code: ``0`` when no canary regressed against the baseline; ``1`` when at
least one always-solved task failed (or the observed pass count fell below the
baseline); ``2`` on a usage error.

The default baseline expects *every* listed task to pass, because that is what
"always-solved" means -- any failure is the alarm this suite exists to raise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TASK_LIST = "scripts/canary_tasks.txt"

# Keys we look for, tolerantly, across Harbor + Alysis Code artifacts.
_RESOLVED_BOOL_KEYS = ("resolved", "is_resolved", "passed", "success")
_RESOLVED_NUM_KEYS = ("reward", "score")
_DISPATCH_KEY = "dispatch_overhead_seconds"
_COST_KEYS = ("cost_usd", "total_cost", "cost", "usd")
_TOKEN_KEYS = ("total_tokens", "tokens")


# ---------------------------------------------------------------------------
# Task list
# ---------------------------------------------------------------------------


def read_task_list(path: str | os.PathLike[str]) -> list[str]:
    """Read one task name per line, dropping blanks and ``#`` comments.

    Comments may be whole-line or trailing (``fix-git  # 4.4m``); task names
    never contain ``#``, so everything from the first ``#`` is annotation.
    """
    text = Path(path).read_text(encoding="utf-8")
    tasks: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        tasks.append(stripped)
    return tasks


# ---------------------------------------------------------------------------
# Artifact walking (self-contained so the suite is independent)
# ---------------------------------------------------------------------------


def deep_find_key(obj: object, key: str) -> Iterator[object]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from deep_find_key(v, key)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from deep_find_key(item, key)


def iter_json_objects(root: str | os.PathLike[str]) -> Iterator[object]:
    """Yield every JSON value from every ``*.json`` and ``*.jsonl`` under root."""
    root_path = Path(root)
    if not root_path.exists():
        return
    paths = [root_path] if root_path.is_file() else sorted(root_path.rglob("*"))
    for path in paths:
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".jsonl"):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, TypeError):
                    continue
        elif name.endswith(".json"):
            try:
                yield json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError, TypeError):
                continue


# ---------------------------------------------------------------------------
# Per-task metric extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskMetrics:
    task: str
    resolved: bool | None
    dispatch_overhead_seconds: float | None
    cost_usd: float | None
    tokens: int | None


def _first_resolution(objects: list[object]) -> bool | None:
    """Best-effort resolution verdict from Harbor's result records."""
    for obj in objects:
        for key in _RESOLVED_BOOL_KEYS:
            for value in deep_find_key(obj, key):
                if isinstance(value, bool):
                    return value
    for obj in objects:
        for key in _RESOLVED_NUM_KEYS:
            for value in deep_find_key(obj, key):
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return value > 0
    return None


def _sum_dispatch_overhead(objects: list[object]) -> float | None:
    """Sum dispatch overhead, tolerating both the float form and the
    ``{count,total,max}`` deadline-observation form."""
    total = 0.0
    found = False
    for obj in objects:
        for value in deep_find_key(obj, _DISPATCH_KEY):
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                total += float(value)
                found = True
            elif isinstance(value, dict) and isinstance(value.get("total"), (int, float)):
                total += float(value["total"])
                found = True
    return total if found else None


def _sum_numeric(objects: list[object], keys: tuple[str, ...]) -> float | None:
    total = 0.0
    found = False
    for obj in objects:
        for key in keys:
            for value in deep_find_key(obj, key):
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    total += float(value)
                    found = True
    return total if found else None


def parse_task_metrics(root: str | os.PathLike[str], task: str) -> TaskMetrics:
    objects = list(iter_json_objects(root))
    tokens = _sum_numeric(objects, _TOKEN_KEYS)
    return TaskMetrics(
        task=task,
        resolved=_first_resolution(objects),
        dispatch_overhead_seconds=_sum_dispatch_overhead(objects),
        cost_usd=_sum_numeric(objects, _COST_KEYS),
        tokens=int(tokens) if tokens is not None else None,
    )


# ---------------------------------------------------------------------------
# Aggregation + baseline comparison
# ---------------------------------------------------------------------------


@dataclass
class CanarySummary:
    rows: list[TaskMetrics]

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.rows if r.resolved is True)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.rows if r.resolved is False)

    @property
    def unknown_count(self) -> int:
        return sum(1 for r in self.rows if r.resolved is None)

    @property
    def mean_dispatch_overhead_seconds(self) -> float | None:
        values = [
            r.dispatch_overhead_seconds
            for r in self.rows
            if r.dispatch_overhead_seconds is not None
        ]
        return (sum(values) / len(values)) if values else None

    @property
    def total_cost_usd(self) -> float | None:
        values = [r.cost_usd for r in self.rows if r.cost_usd is not None]
        return sum(values) if values else None


def summarize(rows: list[TaskMetrics]) -> CanarySummary:
    return CanarySummary(rows=list(rows))


@dataclass(frozen=True)
class Baseline:
    """What the pre-fix build is expected to have delivered on the canary set.

    ``pass_count is None`` means "expect every task to pass", the correct default
    for an always-solved set.
    """

    pass_count: int | None = None
    mean_dispatch_overhead_seconds: float | None = None
    total_cost_usd: float | None = None

    @classmethod
    def from_json(cls, path: str | os.PathLike[str]) -> Baseline:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            pass_count=data.get("pass_count"),
            mean_dispatch_overhead_seconds=data.get("mean_dispatch_overhead_seconds"),
            total_cost_usd=data.get("total_cost_usd"),
        )


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_table(summary: CanarySummary, baseline: Baseline) -> str:
    lines: list[str] = []
    lines.append(f"{'task':<32} {'resolved':<9} {'dispatch_s':<12} {'cost_usd':<10} {'tokens':<10}")
    lines.append("-" * 76)
    for row in summary.rows:
        resolved = "-" if row.resolved is None else ("pass" if row.resolved else "FAIL")
        lines.append(
            f"{row.task:<32} {resolved:<9} "
            f"{_fmt(row.dispatch_overhead_seconds):<12} "
            f"{_fmt(row.cost_usd):<10} {_fmt(row.tokens):<10}"
        )
    lines.append("-" * 76)

    expected = baseline.pass_count if baseline.pass_count is not None else summary.total
    lines.append("")
    lines.append("Comparison to baseline")
    lines.append(
        f"  pass count:            {summary.pass_count}/{summary.total} "
        f"(baseline expected {expected}/{summary.total})"
    )
    lines.append(f"  failed:                {summary.fail_count}")
    lines.append(f"  unknown resolution:    {summary.unknown_count}")
    md = summary.mean_dispatch_overhead_seconds
    lines.append(
        f"  mean dispatch overhead: {_fmt(md)} s"
        + (
            f"  (baseline {_fmt(baseline.mean_dispatch_overhead_seconds)} s)"
            if baseline.mean_dispatch_overhead_seconds is not None
            else ""
        )
    )
    lines.append(
        f"  total cost:            {_fmt(summary.total_cost_usd)}"
        + (
            f"  (baseline {_fmt(baseline.total_cost_usd)})"
            if baseline.total_cost_usd is not None
            else ""
        )
    )
    verdict, _ = evaluate_verdict(summary, baseline)
    lines.append("")
    lines.append(f"VERDICT: {verdict}")
    return "\n".join(lines)


def evaluate_verdict(summary: CanarySummary, baseline: Baseline) -> tuple[str, bool]:
    """Return ``(text, regressed)``. ``regressed`` True fails the suite."""
    expected = baseline.pass_count if baseline.pass_count is not None else summary.total
    if summary.fail_count > 0:
        return (
            f"REGRESSION -- {summary.fail_count} always-solved task(s) failed",
            True,
        )
    # All-unknown must be judged before the pass-count shortfall: with no
    # resolution data at all there is nothing to regress against, only artifacts
    # to go collect.
    if summary.total and summary.unknown_count == summary.total:
        return ("INCONCLUSIVE -- no resolution data parsed from artifacts", False)
    if summary.pass_count < expected:
        return (
            f"REGRESSION -- pass count {summary.pass_count} below baseline {expected} "
            f"({summary.unknown_count} unknown; confirm artifacts)",
            True,
        )
    return (f"OK -- {summary.pass_count}/{summary.total} canary tasks solved", False)


# ---------------------------------------------------------------------------
# Thin, untestable live wrapper
# ---------------------------------------------------------------------------


def _run_live(args: argparse.Namespace) -> int:  # pragma: no cover - needs Harbor
    tasks = read_task_list(args.tasks)
    artifacts_root = Path(args.artifacts_root)
    artifacts_root.mkdir(parents=True, exist_ok=True)
    rows: list[TaskMetrics] = []
    for task in tasks:
        task_out = artifacts_root / task
        task_out.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["TB_INCLUDE_TASK"] = task
        env.setdefault("TB_ARTIFACTS_DIR", str(task_out))
        print(f"\n>>> Harbor canary: {task}", flush=True)
        completed = subprocess.run(  # noqa: S603 - operator-supplied command
            args.harbor_cmd, shell=True, env=env, cwd=str(Path.cwd())
        )
        print(f"<<< exit={completed.returncode} for {task}", flush=True)
        rows.append(parse_task_metrics(task_out, task))
    baseline = Baseline.from_json(args.baseline) if args.baseline else Baseline()
    summary = summarize(rows)
    print(render_table(summary, baseline))
    _, regressed = evaluate_verdict(summary, baseline)
    return 1 if regressed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Canary regression tripwire over always-solved benchmark tasks.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-artifacts",
        metavar="DIR",
        help="Offline: parse an existing archive laid out as <dir>/<task>/...",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Live mode: invoke Harbor over the task list, then parse (runner box).",
    )
    parser.add_argument(
        "--tasks",
        default=DEFAULT_TASK_LIST,
        help=f"Task-list file, one task per line (default: {DEFAULT_TASK_LIST}).",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Optional baseline JSON (pass_count, mean_dispatch_overhead_seconds, total_cost_usd).",
    )
    parser.add_argument(
        "--harbor-cmd",
        default="bash scripts/benchmarks/terminal_bench/run_harbor_tbench.sh",
        help="Live mode only: Harbor command; reads TB_INCLUDE_TASK per task.",
    )
    parser.add_argument(
        "--artifacts-root",
        default="./runs/canary",
        help="Where per-task artifacts live (as <root>/<task>/...).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run:
        return _run_live(args)

    root = Path(args.check_artifacts)
    if not root.exists():
        print(f"error: artifact dir does not exist: {root}", file=sys.stderr)
        return 2
    try:
        tasks = read_task_list(args.tasks)
    except OSError as exc:
        print(f"error: cannot read task list {args.tasks}: {exc}", file=sys.stderr)
        return 2
    rows = [parse_task_metrics(root / task, task) for task in tasks]
    baseline = Baseline.from_json(args.baseline) if args.baseline else Baseline()
    summary = summarize(rows)
    print(render_table(summary, baseline))
    _, regressed = evaluate_verdict(summary, baseline)
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
