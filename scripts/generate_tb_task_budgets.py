#!/usr/bin/env python3
"""Regenerate ``scripts/benchmarks/terminal_bench/task_budgets.py`` from a dataset checkout.

The checked-in table was transcribed from observed Harbor kill times, which is
evidence but not configuration: it only covers tasks that actually got killed,
and each value is a lower bound inferred from when the axe fell. This script
replaces that with the real thing -- each task's configured agent time limit,
read from the dataset -- for every task, including the ones that never timed
out.

Run it on the box where a terminal-bench-2-1 checkout lives:

    python3 scripts/generate_tb_task_budgets.py --dataset-dir /path/to/terminal-bench-2-1

Then review the diff and commit it. Nothing at runtime invokes this; the table
is a checked-in artifact so the adapter has no dataset dependency.

Why the parsing is defensive
----------------------------
This script was written without access to a dataset checkout, so it does not
assume a single config shape. It scans each task directory for any of the
common config filenames, and inside those looks for any of the common
timeout-like key names:

    CONFIG FILENAMES   task.yaml, task.yml, task.toml, task.json, config.json,
                       config.yaml, config.yml, config.toml, meta.json,
                       meta.yaml, meta.yml

    TIMEOUT KEYS       agent_timeout_sec, agent_timeout_seconds, agent_timeout_s,
                       agent_timeout, max_agent_time_sec, max_agent_time_seconds,
                       max_agent_time, agent_time_limit_sec,
                       agent_time_limit_seconds, agent_time_limit,
                       timeout_sec, timeout_seconds, timeout_s, timeout

Keys are tried in that order and the first hit wins, so a specific
``agent_timeout`` beats a generic ``timeout`` in the same file. Values may be
bare numbers or carry a unit suffix (``3600``, ``3600s``, ``60m``, ``1h``).

YAML and TOML are read with a line-oriented scanner rather than a parser:
Python 3.10 has no ``tomllib`` and PyYAML is not a dependency, and the only
thing needed here is one scalar. JSON is parsed properly and searched
recursively. A task whose timeout cannot be found is omitted from the table
and falls back to the adapter's flat default -- omission is the safe outcome,
so an unrecognised shape degrades rather than inventing a number.

If the real dataset turns out to use a shape this misses, add its filename to
``CONFIG_FILENAMES`` or its key to ``TIMEOUT_KEYS`` -- both are plain tuples
at the top of this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# Candidate per-task config files, tried in order within each task directory.
CONFIG_FILENAMES: tuple[str, ...] = (
    "task.yaml",
    "task.yml",
    "task.toml",
    "task.json",
    "config.json",
    "config.yaml",
    "config.yml",
    "config.toml",
    "meta.json",
    "meta.yaml",
    "meta.yml",
)

# Candidate timeout field names, most specific first. The first key that
# resolves to a positive duration wins.
TIMEOUT_KEYS: tuple[str, ...] = (
    "agent_timeout_sec",
    "agent_timeout_seconds",
    "agent_timeout_s",
    "agent_timeout",
    "max_agent_time_sec",
    "max_agent_time_seconds",
    "max_agent_time",
    "agent_time_limit_sec",
    "agent_time_limit_seconds",
    "agent_time_limit",
    "timeout_sec",
    "timeout_seconds",
    "timeout_s",
    "timeout",
)

DEFAULT_FRACTION = 0.85
DEFAULT_AGENT_MULTIPLIER = 1.0
DEFAULT_CAP_SECONDS = 10800

_UNIT_SECONDS: dict[str, float] = {
    "": 1.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
}

# `key: value` (YAML) or `key = value` (TOML), with optional quoting on either
# side and an optional trailing comment.
_ASSIGNMENT_RE = re.compile(
    r"""^\s*
        ['"]?(?P<key>[A-Za-z_][A-Za-z0-9_-]*)['"]?
        \s*[:=]\s*
        (?P<value>[^#\n]*?)
        \s*(?:\#.*)?$
    """,
    re.VERBOSE,
)

_DURATION_RE = re.compile(
    r"^(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[A-Za-z]*)$",
)


# --------------------------------------------------------------------------
# Pure parsing / derivation core. Everything below is import-safe and free of
# I/O except where a Path is explicitly passed in, so tests can drive it
# against synthetic fixture directories.
# --------------------------------------------------------------------------


def parse_duration_seconds(raw: Any) -> float | None:
    """Parse ``3600``, ``"3600"``, ``"3600s"``, ``"60m"``, ``"1h"`` to seconds.

    Returns ``None`` for anything that is not a positive finite duration --
    including booleans, which are ``int`` in Python and would otherwise parse
    as ``1``.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 0 else None
    text = str(raw).strip().strip("'\"").strip()
    if not text:
        return None
    match = _DURATION_RE.match(text)
    if match is None:
        return None
    unit = match.group("unit").lower()
    if unit not in _UNIT_SECONDS:
        return None
    try:
        value = float(match.group("number")) * _UNIT_SECONDS[unit]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def find_timeout_in_mapping(data: Any, keys: Iterable[str] = TIMEOUT_KEYS) -> float | None:
    """Recursively search a parsed JSON structure for the first timeout key.

    Each key is searched across the whole structure before the next key is
    tried, so a nested ``agent_timeout`` still beats a top-level ``timeout``.
    """
    for key in keys:
        found = _search_key(data, key)
        if found is not None:
            return found
    return None


def _search_key(data: Any, key: str) -> float | None:
    if isinstance(data, Mapping):
        for candidate, value in data.items():
            if str(candidate).strip().lower() == key:
                parsed = parse_duration_seconds(value)
                if parsed is not None:
                    return parsed
        for value in data.values():
            nested = _search_key(value, key)
            if nested is not None:
                return nested
        return None
    if isinstance(data, (list, tuple)):
        for item in data:
            nested = _search_key(item, key)
            if nested is not None:
                return nested
    return None


def find_timeout_in_text(text: str, keys: Iterable[str] = TIMEOUT_KEYS) -> float | None:
    """Line-scan YAML/TOML-ish text for the first timeout-like assignment.

    Deliberately shape-agnostic: indentation and table headers are ignored, so
    ``agent_timeout`` is found whether it sits at the top level or inside an
    ``[agent]`` / ``agent:`` block.
    """
    assignments: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT_RE.match(line)
        if match is None:
            continue
        key = match.group("key").strip().lower()
        # A JSON-ish line carries a trailing comma, and a closing brace may
        # follow a single-line object. Neither is part of the value.
        value = match.group("value").strip().rstrip(",}]").strip()
        if value:
            assignments.setdefault(key, []).append(value)
    for key in keys:
        for value in assignments.get(key, []):
            parsed = parse_duration_seconds(value)
            if parsed is not None:
                return parsed
    return None


def extract_timeout_seconds(
    path: Path,
    text: str,
    keys: Iterable[str] = TIMEOUT_KEYS,
) -> float | None:
    """Extract a timeout from one config file's text, by suffix.

    JSON gets a real parse; anything else gets the line scanner. A JSON file
    that fails to parse still falls through to the scanner rather than being
    discarded, since a near-JSON config is more useful read loosely than not
    at all.
    """
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        if data is not None:
            found = find_timeout_in_mapping(data, keys)
            if found is not None:
                return found
    return find_timeout_in_text(text, keys)


def derive_budget_seconds(
    timeout_seconds: float,
    *,
    agent_multiplier: float = DEFAULT_AGENT_MULTIPLIER,
    fraction: float = DEFAULT_FRACTION,
    cap: int = DEFAULT_CAP_SECONDS,
) -> int:
    """Turn a configured limit into the budget the agent should be given.

    ``limit x agent_multiplier x fraction``, clamped to ``cap`` and to a
    minimum of one second. The multiplier mirrors Harbor's
    ``--agent-timeout-multiplier`` so a campaign that scales the harness can
    scale the budgets with it; the fraction is the clean-stop headroom.
    """
    value = float(timeout_seconds) * float(agent_multiplier) * float(fraction)
    if value <= 0:
        return 1
    budget = int(value)
    if cap is not None and budget > int(cap):
        return int(cap)
    return max(1, budget)


def discover_task_timeouts(
    dataset_dir: Path,
    *,
    config_filenames: Iterable[str] = CONFIG_FILENAMES,
    keys: Iterable[str] = TIMEOUT_KEYS,
) -> tuple[dict[str, float], list[str]]:
    """Find each task's configured timeout under ``dataset_dir``.

    A "task" is any immediate subdirectory that contains one of the candidate
    config files, searched both at ``<dataset>/<task>/`` and one level deeper
    at ``<dataset>/tasks/<task>/``. Returns ``(timeouts, skipped_task_names)``.
    """
    roots = [dataset_dir]
    tasks_subdir = dataset_dir / "tasks"
    if tasks_subdir.is_dir():
        roots.append(tasks_subdir)

    timeouts: dict[str, float] = {}
    skipped: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            name = task_dir.name
            if name.startswith(".") or name == "tasks" or name in seen:
                continue
            found: float | None = None
            for filename in config_filenames:
                config_path = task_dir / filename
                if not config_path.is_file():
                    continue
                try:
                    text = config_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                found = extract_timeout_seconds(config_path, text, keys)
                if found is not None:
                    break
            if found is None:
                # Only a directory that looked like a task at all is worth
                # reporting as skipped; a stray directory is not news.
                if any((task_dir / f).is_file() for f in config_filenames):
                    skipped.append(name)
                continue
            seen.add(name)
            timeouts[name] = found
    return timeouts, skipped


def render_table_block(budgets: Mapping[str, int]) -> str:
    """Render the dict literal body, sorted and stably formatted."""
    lines = ["TASK_BUDGET_SECONDS: dict[str, int] = {"]
    for name in sorted(budgets):
        lines.append(f'    "{name}": {int(budgets[name])},')
    lines.append("}")
    return "\n".join(lines)


_TABLE_RE = re.compile(
    r"^TASK_BUDGET_SECONDS: dict\[str, int\] = \{.*?^\}",
    re.DOTALL | re.MULTILINE,
)


def replace_table_block(source: str, budgets: Mapping[str, int]) -> str:
    """Swap the table in ``source`` for a freshly rendered one.

    Only the dict literal is touched, so the module's docstring, provenance
    notes, and helper functions survive regeneration untouched.
    """
    if _TABLE_RE.search(source) is None:
        raise ValueError("TASK_BUDGET_SECONDS block not found in target module")
    return _TABLE_RE.sub(lambda _m: render_table_block(budgets), source, count=1)


def build_budgets(
    timeouts: Mapping[str, float],
    *,
    agent_multiplier: float = DEFAULT_AGENT_MULTIPLIER,
    fraction: float = DEFAULT_FRACTION,
    cap: int = DEFAULT_CAP_SECONDS,
) -> dict[str, int]:
    return {
        name: derive_budget_seconds(
            seconds,
            agent_multiplier=agent_multiplier,
            fraction=fraction,
            cap=cap,
        )
        for name, seconds in timeouts.items()
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _default_target() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "benchmarks"
        / "terminal_bench"
        / "task_budgets.py"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the Terminal-Bench per-task budget table.",
    )
    parser.add_argument(
        "--dataset-dir",
        required=True,
        type=Path,
        help="Local terminal-bench-2-1 checkout containing the task directories.",
    )
    parser.add_argument(
        "--agent-multiplier",
        type=float,
        default=DEFAULT_AGENT_MULTIPLIER,
        help=(
            "Harbor --agent-timeout-multiplier in force for the campaign "
            f"(default: {DEFAULT_AGENT_MULTIPLIER})."
        ),
    )
    parser.add_argument(
        "--fraction",
        type=float,
        default=DEFAULT_FRACTION,
        help=(
            "Fraction of the limit given to the agent, leaving room for the "
            f"clean stop (default: {DEFAULT_FRACTION})."
        ),
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=DEFAULT_CAP_SECONDS,
        help=f"Upper bound on any single budget (default: {DEFAULT_CAP_SECONDS}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Module to rewrite (default: scripts/benchmarks/terminal_bench/task_budgets.py).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the table that would be written and change nothing.",
    )
    args = parser.parse_args(argv)

    dataset_dir: Path = args.dataset_dir
    if not dataset_dir.is_dir():
        print(f"error: --dataset-dir is not a directory: {dataset_dir}", file=sys.stderr)
        return 2

    timeouts, skipped = discover_task_timeouts(dataset_dir)
    if not timeouts:
        print(
            "error: no task timeouts discovered. Checked filenames "
            f"{', '.join(CONFIG_FILENAMES)} for keys {', '.join(TIMEOUT_KEYS)}.",
            file=sys.stderr,
        )
        return 1

    budgets = build_budgets(
        timeouts,
        agent_multiplier=args.agent_multiplier,
        fraction=args.fraction,
        cap=args.cap,
    )
    capped = [name for name, value in budgets.items() if value == int(args.cap)]

    print(f"dataset:          {dataset_dir}")
    print(f"tasks with limit: {len(budgets)}")
    print(f"tasks skipped:    {len(skipped)}")
    print(f"multiplier={args.agent_multiplier}  fraction={args.fraction}  cap={args.cap}")
    if capped:
        print(f"clamped to cap:   {len(capped)} ({', '.join(sorted(capped)[:5])}...)")
    if skipped:
        print(f"no timeout found: {', '.join(sorted(skipped)[:10])}")

    if args.dry_run:
        print()
        print(render_table_block(budgets))
        return 0

    target: Path = args.output or _default_target()
    if not target.is_file():
        print(f"error: target module not found: {target}", file=sys.stderr)
        return 2
    source = target.read_text(encoding="utf-8")
    try:
        updated = replace_table_block(source, budgets)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if updated == source:
        print(f"unchanged: {target}")
        return 0
    target.write_text(updated, encoding="utf-8")
    print(f"wrote: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
