"""Reusable live evaluation orchestration; planning/preparation never call a model.

Run ``python -m scripts.qa.subagent_live_eval --help``. Live execution requires
an explicit attempt ID and --execute-live; no retries or subsequent attempts are
started automatically. All artifacts stay in the operator's chosen output path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.qa.subagent_eval_cases import REPORT_RUBRIC, all_cases
from scripts.qa.subagent_eval_evidence import (
    completed_turns,
    jsonl,
    numeric,
    provider_observed_usage,
    summarize_evidence,
)

MODELS = ("gpt-5.6-luna", "gpt-5.6-terra")
DEFAULT_BOUNDS = {
    "attempt_timeout_s": 720,
    "max_steps": 48,
    "batch_wall_s": 10800,
    "max_uncached_input_tokens": 3000000,
    "max_output_tokens": 250000,
    "max_attempts": 12,
}
ACCEPTANCE_TIMEOUT_S = 90
# Resolve POSIX-only constants at signal time so planning and test collection
# also work on Windows, where signal.SIGKILL is not defined.
PROCESS_STOP_STAGES = (("SIGINT", 15), ("SIGTERM", 5), ("SIGKILL", 5))
# At most one acceptance oracle remains after the live deadline. Include the
# existing stop grace and five seconds of supervisor overhead in admission.
POST_ATTEMPT_WALL_ALLOWANCE_S = (
    ACCEPTANCE_TIMEOUT_S + sum(wait for _, wait in PROCESS_STOP_STAGES) + 5
)


def harness_hashes() -> dict[str, str]:
    return {
        path.name: digest(path.read_bytes())
        for path in sorted(Path(__file__).parent.glob("subagent_*.py"))
    }


def dump(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "--no-optional-locks", "-C", str(root), *args], text=True
    ).strip()


def file_hashes(root: Path, *, exclude_git: bool = True) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and (not exclude_git or ".git" not in path.relative_to(root).parts)
        and "__pycache__" not in path.parts
    }


def freeze_source(source: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("A source freeze destination must be new")
    if not (source / "src" / "alysis_code" / "config.py").is_file():
        raise ValueError("Source must contain src/alysis_code/config.py")
    destination.mkdir(parents=True)
    shutil.copytree(
        source / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    if (source / "pyproject.toml").is_file():
        shutil.copy2(source / "pyproject.toml", destination / "pyproject.toml")
    dump(
        destination / "evaluation-source.json",
        {
            "origin": str(source.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": file_hashes(destination),
        },
    )


def verify_source(source: Path) -> str:
    manifest = source / "evaluation-source.json"
    data = json.loads(manifest.read_text())
    actual = file_hashes(source)
    actual.pop("evaluation-source.json", None)
    if actual != data["files"]:
        raise ValueError(f"Frozen runtime source changed: {source}")
    return digest(manifest.read_bytes())


def safe_id(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and all(c.isascii() and (c.isalnum() or c in "-_.") for c in value)
    )


def load_plan(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if data.get("version") != 1:
        raise ValueError("Expected manifest version 1")
    variants = data.get("variants", [])
    attempts = data.get("attempts", [])
    bounds = data.get("bounds", {})
    if set(bounds) != set(DEFAULT_BOUNDS) or any(
        not isinstance(v, (int, float)) or v <= 0 for v in bounds.values()
    ):
        raise ValueError("All explicit positive batch/attempt bounds are required")
    if len(attempts) > bounds["max_attempts"]:
        raise ValueError("Manifest exceeds its predeclared attempt count")
    variant_ids = [v["id"] for v in variants]
    ids = [a["id"] for a in attempts]
    if len(set(ids)) != len(ids) or len(set(variant_ids)) != len(variant_ids):
        raise ValueError("Variant/attempt IDs must be unique")
    cases = all_cases()
    for attempt in attempts:
        if not safe_id(attempt["id"]) or attempt["variant"] not in variant_ids:
            raise ValueError("Invalid attempt ID or variant reference")
        if attempt["model"] not in MODELS or attempt["interface"] not in {"chat", "run"}:
            raise ValueError("Unsupported model/interface")
        case = cases[attempt["task"]]
        if attempt["interface"] == "run" and len(case.turns) != 1:
            raise ValueError("Multi-turn tasks require one genuine chat process")
        if case.requires_subagents and attempt.get("direct"):
            raise ValueError("A required-child scenario cannot be a direct control")
        if attempt.get("repeat", 1) not in {1, 2}:
            raise ValueError("Only one predeclared confirmation repeat is supported")
    return data


def screen_manifest(baseline: Path, candidate: Path, output: Path, destination: Path) -> None:
    variants = [
        {"id": "current-v6", "source": str(baseline.resolve()), "guidance": None},
        {
            "id": "shared",
            "source": str(candidate.resolve()),
            "guidance": {"default": "balanced", "model_profiles": {}},
        },
        {
            "id": "profiled",
            "source": str(candidate.resolve()),
            "guidance": {
                "default": "balanced",
                "model_profiles": {"gpt-5.6-luna": "expanded", "gpt-5.6-terra": "balanced"},
            },
        },
    ]
    for variant in variants:
        source = Path(variant["source"])
        variant["source_manifest_sha256"] = verify_source(source) if source.exists() else None
    attempts = []
    for task in ("dev-request-log", "dev-quote-chat"):
        for model in MODELS:
            for variant in ("current-v6", "shared", "profiled"):
                # This is an explicit experimental equivalence, not model routing.
                if model == "gpt-5.6-terra" and variant == "profiled":
                    continue
                attempts.append(
                    {
                        "id": f"{task}-{model}-{variant}-r1",
                        "task": task,
                        "model": model,
                        "variant": variant,
                        "interface": "chat",
                        "direct": False,
                        "repeat": 1,
                    }
                )
    for model in MODELS:
        attempts.append(
            {
                "id": f"dev-request-log-{model}-shared-direct-r1",
                "task": "dev-request-log",
                "model": model,
                "variant": "shared",
                "interface": "chat",
                "direct": True,
                "repeat": 1,
            }
        )
    data = {
        "version": 1,
        "phase": "development-screening",
        "output_dir": str(output.resolve()),
        "python": sys.executable,
        "bounds": DEFAULT_BOUNDS,
        "variants": variants,
        "attempts": attempts,
        "confirmation_policy": "Exactly one new predeclared confirmation repeat per selected improvement candidate; retain every earlier outcome. No automatic retry on ambiguity or provider interruption.",
        "equivalence": "Terra shared and profiled use the same balanced instructions; the duplicate cells are deliberately omitted.",
        "heldout_policy": "Detailed held-out briefs/rubrics must remain unread until shared/profile source is frozen. Record their hashes now; do not tune guidance after seeing held-out outputs.",
        "case_hashes": {key: case_hash(case) for key, case in all_cases().items()},
        "harness_hashes": harness_hashes(),
        "token_bound_semantics": "Stop on observed provider usage during an attempt and before dispatch. Usage arrives after requests, so a single in-flight response may cross a threshold; unavailable terminal usage pauses later dispatch.",
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    dump(destination, data)


def case_hash(case) -> str:
    from dataclasses import asdict

    return digest(json.dumps(asdict(case), sort_keys=True, ensure_ascii=False).encode())


def prepare_attempt(directory: Path, case) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=False)
    return _populate_attempt(directory, case)


def _populate_attempt(directory: Path, case) -> dict[str, Any]:
    root = directory / "workspace"
    root.mkdir()
    for relative, content in case.files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    git(root, "init", "-q")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Evaluation Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-qm",
        "Frozen evaluation fixture",
    )
    for relative, content in case.dirty_files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    if case.acceptance:
        (directory / "acceptance.py").write_bytes(case.acceptance.encode("utf-8"))
    # Classic chat consumes one line per message. Preserve both authored and
    # transmitted prompts so interface formatting differences are explicit.
    prompts = [
        {"authored": prompt, "chat_line": " ".join(prompt.splitlines())} for prompt in case.turns
    ]
    dump(directory / "prompts.json", prompts)
    dump(
        directory / "manual-rubric.json",
        {"common": REPORT_RUBRIC, "case": case.rubric, "review": None},
    )
    fixture = {
        "task": case.id,
        "case_sha256": case_hash(case),
        "baseline_head": git(root, "rev-parse", "HEAD"),
        "baseline_staged_diff": git(root, "diff", "--cached", "--binary"),
        "initial_files": file_hashes(root),
        "preserved_files": {key: digest(value.encode()) for key, value in case.dirty_files.items()},
        "acceptance_sha256": digest(case.acceptance.encode()) if case.acceptance else None,
        "acceptance_stages": case.acceptance_stages,
        "readonly": case.readonly,
    }
    dump(directory / "fixture.json", fixture)
    return fixture


def acceptance(directory: Path, python: str, stage: int) -> dict[str, Any]:
    fixture = json.loads((directory / "fixture.json").read_text())
    root = directory / "workspace"
    source = directory / "acceptance.py"
    expected = fixture["acceptance_sha256"]
    if expected and digest(source.read_bytes()) != expected:
        raise ValueError("Frozen external acceptance was altered")
    current_files = file_hashes(root)
    preservation = {
        "head_unchanged": git(root, "rev-parse", "HEAD") == fixture["baseline_head"],
        "staged_state_unchanged": git(root, "diff", "--cached", "--binary")
        == fixture["baseline_staged_diff"],
        "dirty_files_unchanged": all(
            current_files.get(path) == sha for path, sha in fixture["preserved_files"].items()
        ),
        "readonly_files_unchanged": current_files == fixture["initial_files"]
        if fixture["readonly"]
        else None,
    }
    result: dict[str, Any] = {"stage": stage, "preservation": preservation, "returncode": None}
    if expected:
        scenario = fixture["acceptance_stages"][stage]
        env = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            ORDERQUOTE_CASE_ROOT=str(root),
            ORDERQUOTE_CASE_SCENARIO=scenario,
            ALYSIS_EVAL_WORKSPACE=str(root),
            ALYSIS_EVAL_CASE_STAGE=scenario,
        )
        run = subprocess.run(
            [python, "-B", str(source)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=ACCEPTANCE_TIMEOUT_S,
        )
        (directory / f"acceptance-turn-{stage + 1}.txt").write_text(
            run.stdout + run.stderr, encoding="utf-8"
        )
        result["returncode"] = run.returncode
    dump(directory / f"acceptance-turn-{stage + 1}.json", result)
    return result


def check_budget(plan: dict[str, Any]) -> dict[str, float]:
    directories = list(Path(plan["output_dir"]).glob("attempts/*"))
    if any(not (path / "result.json").is_file() for path in directories):
        raise ValueError("Batch paused: an earlier attempt has no terminal result")
    results = [json.loads((path / "result.json").read_text()) for path in directories]
    if any(result.get("evidence", {}).get("unknown_usage", True) for result in results):
        raise ValueError(
            "Batch paused: an earlier attempt has unknown usage; review it before authorizing a new bounded campaign"
        )
    bounds = plan["bounds"]
    spent = {
        "batch_wall_s": sum(r.get("wall_seconds", 0) for r in results),
        "max_uncached_input_tokens": sum(
            r["evidence"]["known_usage"]["uncached_prompt_tokens"] for r in results
        ),
        "max_output_tokens": sum(
            r["evidence"]["known_usage"]["completion_tokens"] for r in results
        ),
        "max_attempts": len(results),
    }
    for key, value in spent.items():
        if value >= bounds[key]:
            raise ValueError(f"Batch bound reached: {key}")
    if (
        spent["batch_wall_s"] + bounds["attempt_timeout_s"] + POST_ATTEMPT_WALL_ALLOWANCE_S
        > bounds["batch_wall_s"]
    ):
        raise ValueError("Batch wall bound cannot accommodate another bounded attempt")
    return {key: bounds[key] - value for key, value in spent.items()}


def observed_token_bound(directory: Path, remaining: dict[str, float]) -> str | None:
    """Check actual completed provider telemetry; never infer missing usage."""
    records = [
        provider_observed_usage(record)
        for path in (directory / "data" / "sessions").glob("*/diagnostics/provider_telemetry.jsonl")
        for record in jsonl(path, partial=True)
    ]
    for field, limit in (
        ("uncached_prompt_tokens", "max_uncached_input_tokens"),
        ("completion_tokens", "max_output_tokens"),
    ):
        known = sum(r[field] for r in records if numeric(r.get(field)))
        if known >= remaining[limit]:
            return limit
    return None


def stop_process(process: subprocess.Popen) -> None:
    if not hasattr(os, "killpg"):
        from alysis_code.agent_runtimes.codex_cli import _terminate_process_tree

        _terminate_process_tree(process)
        return
    for sig, wait in PROCESS_STOP_STAGES:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, getattr(signal, sig))
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue


def run_attempt(plan_path: Path, attempt_id: str, *, execute_live: bool) -> dict[str, Any]:
    plan = load_plan(plan_path)
    attempt = next(a for a in plan["attempts"] if a["id"] == attempt_id)
    variant = next(v for v in plan["variants"] if v["id"] == attempt["variant"])
    case = all_cases()[attempt["task"]]
    if case_hash(case) != plan["case_hashes"][case.id]:
        raise ValueError("Task brief/fixture/oracle/rubric changed after manifest declaration")
    source = Path(variant["source"])
    source_sha = verify_source(source)
    if source_sha != variant.get("source_manifest_sha256"):
        raise ValueError("Source freeze must match its predeclared manifest hash")
    if harness_hashes() != plan.get("harness_hashes"):
        raise ValueError("Evaluation harness changed after manifest declaration")
    if not execute_live:
        raise ValueError("Live provider execution requires explicit --execute-live dispatch")
    remaining = check_budget(plan)
    output = Path(plan["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    lock = output / "live-attempt.lock"
    with lock.open("x") as stream:
        stream.write(attempt_id + "\n")
    directory = output / "attempts" / attempt_id
    process = None
    owns_directory = False
    started = time.monotonic()
    result: dict[str, Any] = {"attempt": attempt_id, "acceptance": []}
    sent = 0
    try:
        # Claim a new attempt atomically. Neither redispatch nor a race may
        # replace artifacts belonging to an earlier (even crashed) attempt.
        directory.mkdir(parents=True, exist_ok=False)
        owns_directory = True
        _populate_attempt(directory, case)
        dump(directory / "dispatch-manifest.json", plan)
        harness = directory / "harness"
        harness.mkdir()
        for path in Path(__file__).parent.glob("subagent_*.py"):
            shutil.copy2(path, harness / path.name)
        dump(directory / "harness-manifest.json", file_hashes(harness))
        command = [
            plan["python"],
            str(harness / "subagent_eval_subscription.py"),
            attempt["interface"],
        ]
        if attempt["interface"] == "run":
            command += [case.turns[0], "--no-deadline"]
        command += [
            "--path",
            str(directory / "workspace"),
            "--mode",
            "fullaccess",
            "--max-steps",
            str(plan["bounds"]["max_steps"]),
            "--diagnostic-log",
            str(directory / "diagnostics.jsonl"),
        ]
        if attempt.get("direct"):
            command.append("--no-subagents")
        boundaries = directory / "turn-boundaries.jsonl"
        boundaries.touch()
        env = dict(
            os.environ,
            PYTHONPATH=str(source / "src"),
            PYTHONDONTWRITEBYTECODE="1",
            ALYSIS_EVAL_SOURCE=str(source),
            ALYSIS_EVAL_WORKSPACE=str(directory / "workspace"),
            ALYSIS_EVAL_MODEL=attempt["model"],
            ALYSIS_EVAL_GUIDANCE=json.dumps(variant.get("guidance")),
            ALYSIS_EVAL_SELECTION_PATH=str(directory / "selection.json"),
            ALYSIS_EVAL_TURN_BOUNDARIES=str(boundaries),
            ALYSIS_DATA_DIR=str(directory / "data"),
            ALYSIS_TUI="0",
            ALYSIS_UPDATE_CHECK_ENABLED="0",
            NO_COLOR="1",
        )
        dump(
            directory / "attempt.json",
            {
                **attempt,
                "source": str(source),
                "source_manifest_sha256": source_sha,
                "command": command,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "bounds": plan["bounds"],
            },
        )
        with (directory / "console.txt").open("w", encoding="utf-8") as console:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=console,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=source,
                start_new_session=True,
            )
            assert process.stdin is not None
            completed = 0
            if attempt["interface"] == "chat":
                process.stdin.write(" ".join(case.turns[0].splitlines()) + "\n")
                process.stdin.flush()
                sent = 1
            else:
                sent = 1
            last_budget_check = started
            while process.poll() is None:
                if time.monotonic() - started > plan["bounds"]["attempt_timeout_s"]:
                    result["timed_out"] = True
                    stop_process(process)
                    break
                if time.monotonic() - last_budget_check >= 5:
                    last_budget_check = time.monotonic()
                    reached = observed_token_bound(directory, remaining)
                    if reached:
                        result["observed_token_bound_reached"] = reached
                        stop_process(process)
                        break
                finished = completed_turns(directory)
                if attempt["interface"] == "chat" and len(finished) > completed:
                    if len(finished) != completed + 1 or completed >= sent:
                        raise ValueError("Unexpected real chat turn boundary sequence")
                    terminal = finished[completed]
                    result["acceptance"].append(acceptance(directory, plan["python"], completed))
                    completed += 1
                    print(f"{attempt_id}: turn {completed} finished", flush=True)
                    if terminal.get("payload", {}).get("exit_code") != 0 or completed == len(
                        case.turns
                    ):
                        process.stdin.write("/exit\n")
                        process.stdin.flush()
                        process.stdin.close()
                    elif sent == completed:
                        # An oracle can consume the remaining live allowance.
                        # Recheck immediately before another real user turn.
                        if time.monotonic() - started >= plan["bounds"]["attempt_timeout_s"]:
                            result["timed_out"] = True
                            stop_process(process)
                            break
                        reached = observed_token_bound(directory, remaining)
                        if reached:
                            result["observed_token_bound_reached"] = reached
                            stop_process(process)
                            break
                        process.stdin.write(" ".join(case.turns[sent].splitlines()) + "\n")
                        process.stdin.flush()
                        sent += 1
                time.sleep(0.2)
            process.wait()
        result["returncode"] = process.returncode
        # Retain the code outcome even when a provider interruption prevented a
        # normal terminal return. It is separate from completed user delivery.
        if len(result["acceptance"]) < sent:
            checked = acceptance(directory, plan["python"], sent - 1)
            checked["after_process_exit"] = True
            result["acceptance"].append(checked)
        result["turns_sent"] = sent
        result["source_unchanged"] = verify_source(source) == source_sha
    except BaseException as error:
        if process is not None:
            stop_process(process)
        result["harness_error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if owns_directory:
            result["wall_seconds"] = time.monotonic() - started
            try:
                result["evidence"] = summarize_evidence(directory)
                # A final response can cross a bound and exit between polling
                # checks. Report that outcome without claiming supervision
                # stopped the process; preserve its actual return code/finals.
                usage = result["evidence"].get("known_usage", {})
                result["final_observed_token_bounds_reached"] = [
                    limit
                    for field, limit in (
                        ("uncached_prompt_tokens", "max_uncached_input_tokens"),
                        ("completion_tokens", "max_output_tokens"),
                    )
                    if numeric(usage.get(field)) and usage[field] >= remaining[limit]
                ]
            except Exception as error:
                result["evidence"] = {"unknown_usage": True, "parse_error": type(error).__name__}
            dump(directory / "result.json", result)
        lock.unlink()
    return result


def summarize(output: Path) -> None:
    cases = []
    for path in sorted(output.glob("attempts/*/result.json")):
        data = json.loads(path.read_text())
        cases.append(data)
        evidence = data.get("evidence", {})
        finals = evidence.get("finals", [])
        (path.parent / "finals.md").write_text(
            "\n\n---\n\n".join(str(f.get("content", "")) for f in finals), encoding="utf-8"
        )
    dump(
        output / "summary.json",
        {
            "attempts": cases,
            "interpretation": "Manual quality judgment required. Process exit, acceptance and final delivery are separate. Unknown usage pauses further dispatch; subscription dollars are unavailable.",
        },
    )
    lines = [
        "# Live subagent evaluation",
        "",
        "| Attempt | Exit | Seconds | Acceptance | Unknown usage |",
        "|---|---:|---:|---|---|",
    ]
    for case in cases:
        lines.append(
            f"| {case['attempt']} | {case.get('returncode')} | {case.get('wall_seconds', 0):.1f} | {[a.get('returncode') for a in case.get('acceptance', [])]} | {case.get('evidence', {}).get('unknown_usage', True)} |"
        )
    lines += [
        "",
        "Read each attempt's frozen manual-rubric.json, finals.md, source/patch evidence and checks. No semantic keyword score is generated.",
    ]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    describe = commands.add_parser("describe")
    describe.add_argument("task")
    freeze = commands.add_parser("freeze-source")
    freeze.add_argument("source", type=Path)
    freeze.add_argument("destination", type=Path)
    screen = commands.add_parser("screen-manifest")
    screen.add_argument("--baseline", type=Path, required=True)
    screen.add_argument("--candidate", type=Path, required=True)
    screen.add_argument("--output", type=Path, required=True)
    screen.add_argument("--manifest", type=Path, required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("manifest", type=Path)
    run = commands.add_parser("run")
    run.add_argument("manifest", type=Path)
    run.add_argument("--attempt", required=True)
    run.add_argument("--execute-live", action="store_true")
    report = commands.add_parser("summarize")
    report.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "list":
        print(
            json.dumps(
                [
                    {
                        "id": c.id,
                        "split": c.split,
                        "family": c.family,
                        "turns": len(c.turns),
                        "sha256": case_hash(c),
                    }
                    for c in all_cases().values()
                ],
                indent=2,
            )
        )
    elif args.command == "describe":
        case = all_cases()[args.task]
        print(
            json.dumps(
                {"id": case.id, "turns": case.turns, "rubric": (*REPORT_RUBRIC, *case.rubric)},
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.command == "freeze-source":
        freeze_source(args.source, args.destination)
    elif args.command == "screen-manifest":
        screen_manifest(args.baseline, args.candidate, args.output, args.manifest)
    elif args.command == "plan":
        plan = load_plan(args.manifest)
        print(
            json.dumps(
                {
                    "phase": plan["phase"],
                    "bounds": plan["bounds"],
                    "attempts": plan["attempts"],
                    "confirmation_policy": plan["confirmation_policy"],
                },
                indent=2,
            )
        )
    elif args.command == "run":
        run_attempt(args.manifest, args.attempt, execute_live=args.execute_live)
    elif args.command == "summarize":
        summarize(args.output)


if __name__ == "__main__":
    main()
