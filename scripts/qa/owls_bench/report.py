"""Aggregate scored runs into report.json and report.md, and show drift from a baseline.

Results are grouped by system (owls, the reference app), then by split: dev and held-out
are always reported separately and never pooled. Within a split, each category and each
task gets the deterministic pass rate, the judge score, the median wall time, model calls,
tokens and cost (when the source reports them), and the approvals, questions and failures.

    python3 -m scripts.qa.owls_bench.report --out <folder> [--baseline <old report.json>]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from .tasks import SPLITS

REPORT_SCHEMA = 1
DRIFT_FIELDS = (
    "deterministic_pass_rate",
    "check_pass_rate",
    "judge_score",
    "median_wall_s",
    "median_model_calls",
    "median_tokens",
    "median_cost_usd",
)


def _median(values: list) -> float | None:
    present = [value for value in values if isinstance(value, (int, float))]
    return round(statistics.median(present), 4) if present else None


def _mean(values: list) -> float | None:
    present = [value for value in values if isinstance(value, (int, float))]
    return round(sum(present) / len(present), 4) if present else None


def _total(values: list) -> int | None:
    present = [value for value in values if isinstance(value, (int, float))]
    return int(sum(present)) if present else None


def aggregate(scores: list[dict]) -> dict:
    """One metrics block over a group of scored runs."""
    runs = len(scores)
    det = [score["deterministic"] for score in scores]
    metrics = [score.get("metrics") or {} for score in scores]
    judged = [
        score["judge"]["score"]
        for score in scores
        if score.get("judge") and score["judge"].get("score") is not None
    ]
    first = [m.get("first_wall_s") for m in metrics]
    last_of_repeats = [m.get("wall_s") for m in metrics if m.get("first_wall_s") is not None]
    block = {
        "runs": runs,
        "tasks": len({score["task_id"] for score in scores}),
        "finished_rate": round(sum(m.get("status") == "completed" for m in metrics) / runs, 4)
        if runs
        else None,
        "deterministic_pass_rate": round(sum(d["all_passed"] for d in det) / runs, 4)
        if runs
        else None,
        "check_pass_rate": round(
            sum(d["passed"] for d in det) / max(1, sum(d["applicable"] for d in det)), 4
        )
        if runs
        else None,
        "judge_score": _mean(judged),
        "judged_runs": len(judged),
        "median_wall_s": _median([m.get("wall_s") for m in metrics]),
        "median_model_calls": _median([m.get("model_calls") for m in metrics]),
        "median_tokens": _median([m.get("tokens") for m in metrics]),
        "tokens_reported_runs": sum(isinstance(m.get("tokens"), (int, float)) for m in metrics),
        "median_cost_usd": _median([m.get("cost_usd") for m in metrics]),
        "cost_reported_runs": sum(isinstance(m.get("cost_usd"), (int, float)) for m in metrics),
        "approvals_asked": _total([m.get("approvals") for m in metrics]),
        "questions_asked": _total([m.get("questions") for m in metrics]),
        "failures": _total([m.get("failures") for m in metrics]),
    }
    if any(value is not None for value in first):
        first_median, repeat_median = _median(first), _median(last_of_repeats)
        block["repeat"] = {
            "median_first_wall_s": first_median,
            "median_repeat_wall_s": repeat_median,
            "repeat_to_first_ratio": round(repeat_median / first_median, 4)
            if first_median and repeat_median is not None
            else None,
        }
    failing: dict[str, int] = defaultdict(int)
    for score in scores:
        for check in score["checks"]:
            if check["status"] != "pass":
                failing[check["id"]] += 1
    block["checks_not_passed"] = dict(sorted(failing.items()))
    return block


def load_scores(out: Path) -> list[dict]:
    scores = []
    for path in sorted((out / "runs").glob("*/*/run-*/score.json")):
        scores.append(json.loads(path.read_text(encoding="utf-8")))
    return scores


def build_report(scores: list[dict], *, judge_info: dict | None = None) -> dict:
    systems: dict[str, dict] = {}
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    labels: dict[str, str] = {}
    for score in scores:
        grouped[score["system"]][score["split"]].append(score)
        labels.setdefault(score["system"], score.get("label") or score["system"])
    for system, splits in sorted(grouped.items()):
        block = {"label": labels[system], "splits": {}}
        for split in SPLITS:
            split_scores = splits.get(split, [])
            if not split_scores:
                continue
            categories: dict[str, list[dict]] = defaultdict(list)
            per_task: dict[str, list[dict]] = defaultdict(list)
            for score in split_scores:
                categories[score["category"]].append(score)
                per_task[score["task_id"]].append(score)
            block["splits"][split] = {
                "summary": aggregate(split_scores),
                "categories": {
                    name: aggregate(group) for name, group in sorted(categories.items())
                },
                "tasks": {
                    task_id: {
                        **aggregate(group),
                        "category": group[0]["category"],
                        "language": group[0]["language"],
                        "task_sha256": group[-1]["task_sha256"],
                        "task_changed_since_run": any(
                            score.get("recorded_task_sha256") not in (None, score["task_sha256"])
                            for score in group
                        ),
                    }
                    for task_id, group in sorted(per_task.items())
                },
            }
        systems[system] = block
    digests = sorted(
        {
            score["judge"].get("digest")
            for score in scores
            if score.get("judge") and score["judge"].get("digest")
        }
    )
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "judge": {**(judge_info or {}), "prompt_digests": digests}
        if digests or judge_info
        else None,
        "systems": systems,
    }


def drift(current: dict, baseline: dict) -> dict:
    """Per system, split and category: each metric now, before, and the change."""
    changes: dict = {}
    for system, block in current.get("systems", {}).items():
        before_system = baseline.get("systems", {}).get(system)
        if not before_system:
            continue
        for split, split_block in block["splits"].items():
            before_split = before_system["splits"].get(split)
            if not before_split:
                continue
            for category, now in split_block["categories"].items():
                before = before_split["categories"].get(category)
                if not before:
                    continue
                row = {}
                for name in DRIFT_FIELDS:
                    a, b = now.get(name), before.get(name)
                    row[name] = {
                        "now": a,
                        "before": b,
                        "change": round(a - b, 4)
                        if isinstance(a, (int, float)) and isinstance(b, (int, float))
                        else None,
                    }
                changes.setdefault(system, {}).setdefault(split, {})[category] = row
    notes = []
    now_digests = set((current.get("judge") or {}).get("prompt_digests") or [])
    before_digests = set((baseline.get("judge") or {}).get("prompt_digests") or [])
    if now_digests and before_digests and now_digests != before_digests:
        notes.append(
            "The judge prompts differ between the two reports; judge scores are not comparable."
        )
    changed = sorted(
        task_id
        for system, block in current.get("systems", {}).items()
        for split, split_block in block["splits"].items()
        for task_id, task in split_block["tasks"].items()
        if (
            (baseline.get("systems", {}).get(system, {}).get("splits", {}).get(split, {}))
            .get("tasks", {})
            .get(task_id, {})
            .get("task_sha256")
            not in (None, task.get("task_sha256"))
        )
    )
    if changed:
        notes.append(f"These tasks changed between the two reports: {', '.join(changed)}.")
    return {"changes": changes, "notes": notes}


def _fmt(value, kind: str = "") -> str:
    if value is None:
        return "–"
    if kind == "rate":
        return f"{value * 100:.0f}%"
    if kind == "money":
        return f"${value:.4f}"
    if isinstance(value, float):
        return f"{value:.1f}" if abs(value) >= 10 else f"{value:.2f}"
    return str(value)


_COLUMNS = (
    ("Runs", "runs", ""),
    ("Finished", "finished_rate", "rate"),
    ("Checks all pass", "deterministic_pass_rate", "rate"),
    ("Checks passed", "check_pass_rate", "rate"),
    ("Judge", "judge_score", "rate"),
    ("Wall s (median)", "median_wall_s", ""),
    ("Model calls", "median_model_calls", ""),
    ("Tokens", "median_tokens", ""),
    ("Cost", "median_cost_usd", "money"),
    ("Approvals", "approvals_asked", ""),
    ("Questions", "questions_asked", ""),
    ("Failures", "failures", ""),
)


def _table(rows: list[tuple[str, dict]], first: str) -> list[str]:
    lines = [
        "| " + " | ".join([first, *(title for title, _, _ in _COLUMNS)]) + " |",
        "|" + "---|" * (len(_COLUMNS) + 1),
    ]
    for name, block in rows:
        cells = [_fmt(block.get(key), kind) for _, key, kind in _COLUMNS]
        lines.append("| " + " | ".join([name, *cells]) + " |")
    return lines


def _change(row: dict, name: str, kind: str = "") -> str:
    delta = row[name]["change"]
    if delta is None:
        return "–"
    if kind == "rate":
        return f"{delta * 100:+.0f} pts"
    return f"{delta:+.2f}"


def render_markdown(report: dict, drift_result: dict | None = None) -> str:
    lines = ["# Owls benchmark report", "", f"Generated {report['generated_at']}.", ""]
    judge = report.get("judge")
    if judge:
        model = judge.get("model") or "recorded in the runs"
        lines += [
            f"Judge: {model}; prompt digest {', '.join(d[:12] for d in judge.get('prompt_digests', [])) or '–'}.",
            "",
        ]
    lines += [
        "Dev and held-out results are separate. Tune only on dev; held-out shows whether a "
        "change generalises. Cost and tokens count only runs whose source reported them.",
        "",
    ]
    for system, block in report["systems"].items():
        lines += [f"## {block['label']} ({system})", ""]
        for split, split_block in block["splits"].items():
            summary = split_block["summary"]
            lines += [f"### {'Held-out' if split == 'heldout' else 'Dev'} split", ""]
            lines += _table([("All", summary)], "Scope")
            lines.append("")
            lines += _table(list(split_block["categories"].items()), "Category")
            lines.append("")
            lines += _table(list(split_block["tasks"].items()), "Task")
            lines.append("")
            repeats = [
                (name, cat["repeat"])
                for name, cat in split_block["categories"].items()
                if cat.get("repeat")
            ]
            for name, repeat in repeats:
                lines.append(
                    f"Repeat timing ({name}): first run {_fmt(repeat['median_first_wall_s'])} s, "
                    f"repeat {_fmt(repeat['median_repeat_wall_s'])} s, ratio "
                    f"{_fmt(repeat['repeat_to_first_ratio'])}."
                )
            if repeats:
                lines.append("")
            failing = summary.get("checks_not_passed") or {}
            if failing:
                lines.append(
                    "Checks not passed: "
                    + ", ".join(f"{name} ({count})" for name, count in failing.items())
                    + "."
                )
                lines.append("")
    if drift_result:
        lines += ["## Drift from the baseline", ""]
        for note in drift_result["notes"]:
            lines += [f"- {note}"]
        if drift_result["notes"]:
            lines.append("")
        lines += [
            "| System | Split | Category | Checks all pass | Judge | Wall s | Model calls | Tokens | Cost |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for system, splits in drift_result["changes"].items():
            for split, categories in splits.items():
                for category, row in categories.items():
                    lines.append(
                        f"| {system} | {split} | {category} | "
                        f"{_change(row, 'deterministic_pass_rate', 'rate')} | "
                        f"{_change(row, 'judge_score', 'rate')} | {_change(row, 'median_wall_s')} | "
                        f"{_change(row, 'median_model_calls')} | {_change(row, 'median_tokens')} | "
                        f"{_change(row, 'median_cost_usd')} |"
                    )
        lines.append("")
    return "\n".join(lines)


def write_report(
    out: Path,
    *,
    judge: tuple | None = None,
    baseline: Path | None = None,
) -> Path:
    judge_info = judge[1].describe() if judge else None
    report = build_report(load_scores(out), judge_info=judge_info)
    drift_result = None
    if baseline is not None:
        drift_result = drift(report, json.loads(Path(baseline).read_text(encoding="utf-8")))
        report["drift"] = drift_result
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(render_markdown(report, drift_result), encoding="utf-8")
    return out / "report.md"


def print_drift(drift_result: dict) -> None:
    for note in drift_result["notes"]:
        print(note)
    for system, splits in drift_result["changes"].items():
        for split, categories in splits.items():
            for category, row in categories.items():
                parts = []
                for name in (
                    "deterministic_pass_rate",
                    "judge_score",
                    "median_wall_s",
                    "median_model_calls",
                    "median_tokens",
                    "median_cost_usd",
                ):
                    delta = row[name]["change"]
                    if delta is not None:
                        parts.append(f"{name} {delta:+.4g}")
                print(f"{system} {split} {category}: {', '.join(parts) or 'no comparable metrics'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the owls benchmark report.")
    parser.add_argument("--out", type=Path, required=True, help="the benchmark output folder")
    parser.add_argument("--baseline", type=Path, help="an earlier report.json to compare with")
    args = parser.parse_args(argv)
    if not (args.out / "runs").is_dir():
        print(f"{args.out} holds no benchmark runs.", file=sys.stderr)
        return 2
    path = write_report(args.out, baseline=args.baseline)
    if args.baseline:
        print_drift(json.loads((args.out / "report.json").read_text(encoding="utf-8"))["drift"])
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
