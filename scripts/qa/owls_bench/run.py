"""Run the owls benchmark: real tasks through an owls service and/or another app's headless
command, then score and report them.

    python3 -m scripts.qa.owls_bench.run --out .agent-notes/owls-bench/<name> \\
        --owls-url http://127.0.0.1:<port> [--owls-model <id>] \\
        [--reference-command 'tool --prompt {prompt} --output-dir {out}' --reference-label 'Tool']

See README.md. The owls token comes from --owls-token or ALYSIS_OWLS_BENCH_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from . import judge as judge_module
from .owls_client import OwlsClient, OwlsError
from .report import write_report
from .score import score_record
from .tasks import DEFAULT_TIMEOUT_S, TASKS_DIR, Task, TaskError, load_tasks, select
from .timing import time_split

RECORD_SCHEMA = 1
OUTPUT_LIMIT = 4 * 1024 * 1024
REPLY_LIMIT = 200_000
CANCEL_WAIT_S = 30.0

# The owls service's task states and event kinds (bots/store.py, runtime.py,
# action_review.py, clarifications.py, proposals.py).
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
EVENT_TOOL_STARTED = "tool_started"
EVENT_TOOL_FAILED = "tool_failed"
EVENT_APPROVAL = "action_approval_requested"
EVENT_QUESTION = "clarification_requested"
EVENT_PROPOSALS = ("action_proposed", "setup_proposed")
# How a turn's tools were offered: all directly, or some through look-up (bots/owl_toolset.py).
EVENT_TOOLS_OFFERED = "tools_offered"
# Needs-you items that wait for the person (all but a failed turn the person hasn't seen).
NOT_A_CARD = ("failed",)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_name(name: str) -> str:
    cleaned = "".join("_" if ch in '/\\:*?"<>|' or ord(ch) < 32 else ch for ch in name).strip()
    return cleaned.strip(".") or "file"


def _record_base(system: str, label: str, task: Task, run_number: int) -> dict:
    return {
        "schema": RECORD_SCHEMA,
        "system": system,
        "label": label,
        "task_id": task.id,
        "task_sha256": task.sha256,
        "split": task.split,
        "category": task.category,
        "language": task.language,
        "run": run_number,
        "started_at": _now_iso(),
        "attempts": [],
        "error": None,
    }


# --- Owls --------------------------------------------------------------------------------


def task_receipts(row: dict) -> dict:
    """What one owl turn did, from its task record: steps, sites, usage, cards, failures."""
    events = row.get("events") or []
    kinds = Counter(event.get("kind") for event in events)
    steps: Counter = Counter()
    hosts: set[str] = set()
    tool_modes: list[str] = []
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event.get("kind") == EVENT_TOOL_STARTED:
            steps[str(data.get("name"))] += 1
            if isinstance(data.get("host"), str):
                hosts.add(data["host"])
        elif event.get("kind") == EVENT_TOOLS_OFFERED and isinstance(data.get("mode"), str):
            tool_modes.append(data["mode"])
    calls = row.get("usage") or []
    reported = [call["usage"] for call in calls if isinstance(call.get("usage"), dict)]
    prompt = sum(int(usage.get("prompt_tokens") or 0) for usage in reported)
    completion = sum(int(usage.get("completion_tokens") or 0) for usage in reported)
    costs = [usage.get("provider_cost_usd") for usage in reported]
    complete_costs = (
        bool(calls)
        and len(costs) == len(calls)
        and all(isinstance(cost, (int, float)) for cost in costs)
    )
    status = row.get("status")
    return {
        "model_calls": len(calls),
        "tokens": {
            "input": prompt,
            "output": completion,
            "total": prompt + completion,
            "reported_calls": len(reported),
            "unreported_calls": len(calls) - len(reported),
        },
        "cost_usd": round(sum(costs), 6) if complete_costs else (0.0 if not calls else None),
        "approvals": kinds[EVENT_APPROVAL],
        "questions": kinds[EVENT_QUESTION],
        "proposals": sum(kinds[kind] for kind in EVENT_PROPOSALS),
        "failures": kinds[EVENT_TOOL_FAILED] + (1 if status in ("failed", "interrupted") else 0),
        "tool_steps": dict(steps.most_common()),
        # Each mode the turn's tools were offered in, in order (full, apps_by_lookup, everyday).
        "tool_modes": tool_modes,
        "hosts": sorted(hosts),
        "event_kinds": dict(sorted(kinds.items())),
        "failure": row.get("failure"),
        "error": row.get("error"),
        "server_wall_s": _server_wall(row, events),
        # Where the turn's time went: model calls by purpose, tool steps by kind, the rest.
        "time_split": time_split(row),
    }


def _server_wall(row: dict, events: list[dict]) -> float | None:
    """Seconds from the message being queued to the turn's end, by the service's own clock.

    Read from the queued and final events, so the poll interval adds nothing; the task's
    timestamps are the fallback when events carry no times.
    """
    times = [
        (event.get("kind"), event.get("created_at"))
        for event in events
        if isinstance(event.get("created_at"), (int, float))
    ]
    start = next((at for kind, at in times if kind == "queued"), None)
    end = next((at for kind, at in reversed(times) if kind in TERMINAL), None)
    if start is not None and end is not None and end >= start:
        return round(end - start, 3)
    if row.get("status") in TERMINAL and all(
        isinstance(row.get(key), (int, float)) for key in ("created_at", "updated_at")
    ):
        return round(row["updated_at"] - row["created_at"], 3)
    return None


def _card(item: dict) -> dict:
    """A needs-you item as recorded: its kind and words, never answered."""
    card = {"kind": item.get("kind"), "id": item.get("id"), "text": item.get("text")}
    if isinstance(item.get("action"), dict):
        card["tool"] = item["action"].get("tool")
    for key in ("state", "help", "routine", "pause_code"):
        if item.get(key) is not None:
            card[key] = item[key]
    return card


class OwlsRunner:
    """Runs each task in a new owl of its own, on one owls service (one place)."""

    system = "owls"

    def __init__(
        self,
        client: OwlsClient,
        *,
        label: str = "Owls",
        model: str | None = None,
        timeout_s: float | None = None,
        poll_s: float = 2.0,
        max_poll_s: float = 5.0,
        delete_owls: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.label = label
        self.model = model
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.max_poll_s = max_poll_s
        self.delete_owls = delete_owls
        self.sleep = sleep
        self.clock = clock
        self.place: dict = {}
        self.current_task: str | None = None

    def preflight(self, tasks: list[Task]) -> list[str]:
        """Problems that stop the run before it starts: service, model, attachments."""
        problems = []
        status = self.client.status()
        capabilities = set(status.get("capabilities") or [])
        self.place = {
            "mode": status.get("mode"),
            "model": status.get("model"),
            "platform": status.get("platform"),
            "private_computer_available": status.get("private_computer_available"),
        }
        if not status.get("worker_ready"):
            problems.append("The owls service's task worker is not running.")
        if any(task.attachments for task in tasks) and "message_files" not in capabilities:
            problems.append("This owls service cannot attach files to a message (message_files).")
        if self.model:
            if "owl_models" not in capabilities:
                problems.append("This owls service does not let an owl pick its model.")
            else:
                offered = self.client.models()
                chosen = next((item for item in offered if item.get("id") == self.model), None)
                if chosen is None:
                    ids = ", ".join(str(item.get("id")) for item in offered) or "none"
                    problems.append(f"Model {self.model!r} is not offered here. Offered: {ids}.")
                else:
                    self.place["owl_model"] = {
                        key: chosen.get(key)
                        for key in ("id", "label", "model", "image_input", "context_window")
                    }
        return problems

    def _cards(self, task_id: str) -> list[dict]:
        return [
            _card(item)
            for item in self.client.needs_you()
            if item.get("task_id") == task_id and item.get("kind") not in NOT_A_CARD
        ]

    def _wait(self, task_id: str, deadline: float) -> tuple[dict, str | None]:
        """Poll until the turn ends, stops on a card, or runs out of time."""
        delay = self.poll_s
        while True:
            row = self.client.task(task_id)
            if row.get("status") in TERMINAL:
                return row, None
            if row.get("status") == "waiting" and self._cards(task_id):
                return row, "card"
            now = self.clock()
            if now >= deadline:
                return row, "timeout"
            self.sleep(min(delay, max(0.0, deadline - now)))
            delay = min(delay * 1.5, self.max_poll_s)

    def _stop(self, task_id: str) -> dict:
        """Cancel a turn that is stuck on a card or out of time, and wait for it to settle."""
        try:
            self.client.cancel(task_id)
        except OwlsError:
            pass  # It ended on its own meanwhile; the state read below says how.
        deadline = self.clock() + CANCEL_WAIT_S
        row = self.client.task(task_id)
        while row.get("status") not in TERMINAL and self.clock() < deadline:
            self.sleep(self.poll_s)
            row = self.client.task(task_id)
        return row

    def _attempt(
        self, task: Task, owl: dict, chat: dict, file_ids: list[str], index: int, record_dir: Path
    ) -> dict:
        timeout = self.timeout_s or task.timeout_s or DEFAULT_TIMEOUT_S
        started = self.clock()
        sent = self.client.send(chat["id"], task.prompt, file_ids)
        task_id = sent["id"]
        self.current_task = task_id
        row, stopped = self._wait(task_id, started + timeout)
        wall = self.clock() - started
        cards = self._cards(task_id) if stopped == "card" else []
        if stopped:
            self._stop(task_id)
        self.current_task = None
        row = self.client.task(task_id)
        if not stopped:
            # A finished reply can end by asking the person (a question, a suggested next
            # step): the job waits for them, so the run did not finish it.
            cards = self._cards(task_id)
            if cards and row.get("status") == "completed":
                stopped = "asked"
        status = {"card": "blocked", "timeout": "timeout", "asked": "asked"}.get(
            stopped or "", row.get("status")
        )
        receipts = task_receipts(row)
        # The stopwatch: the service's own time for a turn that ended by itself, else the
        # time this runner waited (a turn stopped on a card or out of time).
        server_wall = receipts["server_wall_s"]
        wall_s = server_wall if stopped in (None, "asked") and server_wall is not None else wall
        files = []
        folder = record_dir / "files" / f"attempt-{index}"
        names: set[str] = set()
        for item in self.client.task_files(owl["id"], task_id):
            name = _safe_name(item.get("name") or item["id"])
            if name in names:
                stem, dot, suffix = name.rpartition(".")
                name = (
                    f"{stem} (revision {item.get('revision')}){dot}{suffix}"
                    if dot
                    else f"{name} (revision {item.get('revision')})"
                )
            names.add(name)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(self.client.download(owl["id"], item))
            files.append((folder / name).relative_to(record_dir).as_posix())
        (record_dir / f"task-attempt-{index}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "index": index,
            "task_id": task_id,
            "status": status,
            "service_status": row.get("status"),
            "stopped_on": cards,
            "wall_s": round(wall_s, 3),
            "client_wall_s": round(wall, 3),
            "reply": (row.get("output") or "")[:REPLY_LIMIT]
            if row.get("status") == "completed"
            else "",
            "files": files,
            **receipts,
        }

    def run(self, task: Task, run_number: int, record_dir: Path) -> dict:
        record = {**_record_base(self.system, self.label, task, run_number), "place": self.place}
        record_dir.mkdir(parents=True, exist_ok=True)
        owl = None
        try:
            owl = self.client.create_owl(f"Bench {task.id} #{run_number}", model=self.model)
            record["owl"] = {"id": owl["id"], "name": owl.get("name"), "model": owl.get("model")}
            chat = self.client.chat(owl["id"])
            file_ids = [self.client.upload(owl["id"], path)["id"] for path in task.attachments]
            for index in range(1, task.expect.repeat + 1):
                record["attempts"].append(
                    self._attempt(task, owl, chat, file_ids, index, record_dir)
                )
            conversation = self.client.conversation(chat["id"])
            (record_dir / "transcript.json").write_text(
                json.dumps(conversation, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (OwlsError, OSError, ValueError, KeyError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            self.abort()
            if not record["attempts"]:
                record["attempts"].append({"index": 1, "status": "error", "reply": "", "files": []})
        finally:
            if owl is not None and self.delete_owls:
                try:
                    self.client.delete_owl(owl["id"])
                except (OwlsError, OSError) as exc:
                    record["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        return record

    def abort(self) -> None:
        """Stop the turn in progress when the benchmark itself is interrupted."""
        if self.current_task:
            try:
                self.client.cancel(self.current_task)
            except (OwlsError, OSError):
                pass  # The interrupted run is recorded as incomplete either way.
            self.current_task = None


# --- Reference app -----------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{(prompt|out|task_id|inputs)\}")


def parse_reference_command(template: str) -> list[str]:
    """The reference command as an argument list, from a JSON array or shell-style words.

    The template is split into words once, before any task text is inserted; each
    placeholder is then replaced inside its word. A prompt therefore always reaches the
    program as data in its own argument and is never read by a shell.
    """
    template = template.strip()
    if template.startswith("["):
        try:
            argv = json.loads(template)
        except ValueError as exc:
            raise ValueError(f"The reference command is not a JSON list: {exc}") from exc
        if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
            raise ValueError("The reference command JSON must be a list of strings.")
    else:
        argv = shlex.split(template)
    if not argv:
        raise ValueError("The reference command is empty.")
    if not any("{prompt}" in part for part in argv):
        raise ValueError("The reference command must pass the task with {prompt}.")
    return argv


def build_reference_argv(argv: list[str], values: dict[str, str]) -> list[str]:
    """Placeholders replaced in one pass, so inserted text is never scanned again."""
    return [PLACEHOLDER.sub(lambda match: values[match.group(1)], part) for part in argv]


def run_process(argv: list[str], *, cwd: Path, timeout_s: float) -> dict:
    """Run a program without a shell; on timeout, stop it and everything it started."""
    options: dict = {}
    if os.name == "posix":
        options["start_new_session"] = True
    else:
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
    except OSError as exc:
        return {
            "exit_code": None,
            "timed_out": False,
            "stdout": b"",
            "stderr": str(exc).encode(),
            "wall_s": 0.0,
            "error": f"{type(exc).__name__}: the program could not start",
        }

    def stop():
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Already gone.
        else:
            process.kill()

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop()
        stdout, stderr = process.communicate()
    except BaseException:
        stop()
        process.communicate()
        raise
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "wall_s": round(time.monotonic() - started, 3),
        "error": None,
    }


class ReferenceRunner:
    """Runs each task through another app's headless command."""

    system = "reference"

    def __init__(
        self, template: str, *, label: str = "Reference app", timeout_s: float | None = None
    ):
        self.argv = parse_reference_command(template)
        self.label = label
        self.timeout_s = timeout_s

    def run(self, task: Task, run_number: int, record_dir: Path) -> dict:
        record = _record_base(self.system, self.label, task, run_number)
        # Only the program's name is recorded: its other words may carry settings or keys.
        record["program"] = Path(self.argv[0]).name
        timeout = self.timeout_s or task.timeout_s or DEFAULT_TIMEOUT_S
        for index in range(1, task.expect.repeat + 1):
            out = record_dir / "files" / f"attempt-{index}"
            inputs = record_dir / "inputs" / f"attempt-{index}"
            out.mkdir(parents=True, exist_ok=True)
            inputs.mkdir(parents=True, exist_ok=True)
            for path in task.attachments:
                shutil.copy2(path, inputs / path.name)
            argv = build_reference_argv(
                self.argv,
                {
                    "prompt": task.prompt,
                    "out": str(out.resolve()),
                    "task_id": task.id,
                    "inputs": str(inputs.resolve()),
                },
            )
            result = run_process(argv, cwd=inputs, timeout_s=timeout)
            for stream in ("stdout", "stderr"):
                (record_dir / f"{stream}-attempt-{index}.txt").write_bytes(
                    result[stream][:OUTPUT_LIMIT]
                )
            if result["error"]:
                status = "error"
            elif result["timed_out"]:
                status = "timeout"
            elif result["exit_code"] == 0:
                status = "completed"
            else:
                status = "exit_nonzero"
            files = sorted(
                path.relative_to(record_dir).as_posix() for path in out.rglob("*") if path.is_file()
            )
            record["attempts"].append(
                {
                    "index": index,
                    "status": status,
                    "exit_code": result["exit_code"],
                    "wall_s": result["wall_s"],
                    "reply": result["stdout"]
                    .decode("utf-8", errors="replace")
                    .strip()[:REPLY_LIMIT],
                    "files": files,
                    "stopped_on": [],
                    "model_calls": None,
                    "tokens": None,
                    "cost_usd": None,
                    "approvals": None,
                    "questions": None,
                    "failures": 0 if status == "completed" else 1,
                    "error": result["error"],
                }
            )
        return record


# --- Command line ------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the owls benchmark against an owls service and/or a reference app."
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="output folder (keep it gitignored)"
    )
    parser.add_argument("--tasks", type=Path, default=TASKS_DIR, help="folder of task files")
    parser.add_argument(
        "--split", choices=("dev", "heldout", "all"), default="dev", help="default: dev"
    )
    parser.add_argument("--only", help="comma-separated task ids")
    parser.add_argument("--runs", type=int, default=3, help="runs per task (default 3)")
    parser.add_argument("--timeout", type=float, help="seconds per task, overriding task files")
    parser.add_argument("--resume", action="store_true", help="keep finished runs in --out")
    parser.add_argument("--baseline", type=Path, help="an earlier report.json to compare with")
    owls = parser.add_argument_group("owls service")
    owls.add_argument(
        "--owls-url",
        default=os.environ.get("ALYSIS_OWLS_BENCH_URL"),
        help="the owls service address (or ALYSIS_OWLS_BENCH_URL)",
    )
    owls.add_argument(
        "--owls-token", help="prefer the ALYSIS_OWLS_BENCH_TOKEN environment variable"
    )
    owls.add_argument("--owls-model", help="a model id from the service's GET /v1/models")
    owls.add_argument("--owls-label", default="Owls", help="name for the owls in reports")
    owls.add_argument(
        "--poll-seconds", type=float, default=2.0, help="first wait between status reads"
    )
    owls.add_argument(
        "--delete-owls", action="store_true", help="delete each benchmark owl after its run"
    )
    reference = parser.add_argument_group("reference app")
    reference.add_argument(
        "--reference-command",
        help="headless command with {prompt}, {out}, {task_id} and {inputs}; shell words or a JSON list",
    )
    reference.add_argument(
        "--reference-label", default="Reference app", help="name for it in reports"
    )
    judge_module.add_arguments(parser)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if not args.owls_url and not args.reference_command:
        parser.error("give --owls-url, --reference-command, or both")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tasks = select(
            load_tasks(args.tasks),
            split=args.split,
            only=[item.strip() for item in args.only.split(",") if item.strip()]
            if args.only
            else None,
        )
        config = judge_module.config_from(args)
        judge = (judge_module.verify(), config) if config else None
    except (TaskError, ValueError, judge_module.JudgeIntegrityError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not tasks:
        print("No tasks match the chosen split and ids.", file=sys.stderr)
        return 2
    runners: list = []
    try:
        if args.owls_url:
            token = args.owls_token or os.environ.get("ALYSIS_OWLS_BENCH_TOKEN", "")
            client = OwlsClient(args.owls_url, token)
            owls = OwlsRunner(
                client,
                label=args.owls_label,
                model=args.owls_model,
                timeout_s=args.timeout,
                poll_s=args.poll_seconds,
                delete_owls=args.delete_owls,
            )
            problems = owls.preflight(tasks)
            if problems:
                print("The owls service is not ready:", file=sys.stderr)
                for problem in problems:
                    print(f"- {problem}", file=sys.stderr)
                return 2
            runners.append(owls)
        if args.reference_command:
            runners.append(
                ReferenceRunner(
                    args.reference_command, label=args.reference_label, timeout_s=args.timeout
                )
            )
    except (ValueError, OwlsError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(
        json.dumps(
            {
                "started_at": _now_iso(),
                "split": args.split,
                "runs": args.runs,
                "timeout_s": args.timeout,
                "tasks": {task.id: task.sha256 for task in tasks},
                "systems": [
                    {
                        "system": runner.system,
                        "label": runner.label,
                        **(
                            {"service": runner.client.describe(), "place": runner.place}
                            if isinstance(runner, OwlsRunner)
                            else {"program": Path(runner.argv[0]).name}
                        ),
                    }
                    for runner in runners
                ],
                "judge": config.describe() if config else None,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    interrupted = False
    try:
        for task in tasks:
            for run_number in range(1, args.runs + 1):
                for runner in runners:
                    record_dir = out / "runs" / runner.system / task.id / f"run-{run_number}"
                    if (record_dir / "record.json").exists():
                        if not args.resume:
                            print(
                                f"{record_dir} already has a result; use a new --out or --resume.",
                                file=sys.stderr,
                            )
                            return 2
                        if not (record_dir / "score.json").exists():
                            score_record(task, record_dir, judge=judge)
                        continue
                    if record_dir.exists():
                        # An interrupted run left partial output: start that run afresh.
                        shutil.rmtree(record_dir)
                    record = runner.run(task, run_number, record_dir)
                    (record_dir / "record.json").write_text(
                        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    score = score_record(task, record_dir, judge=judge)
                    attempt = record["attempts"][-1]
                    print(
                        f"{runner.system} {task.id} run {run_number}: {attempt.get('status')}"
                        f" in {attempt.get('wall_s', '–')} s; checks "
                        f"{score['deterministic']['passed']}/{score['deterministic']['applicable']}",
                        flush=True,
                    )
    except KeyboardInterrupt:
        interrupted = True
        for runner in runners:
            if isinstance(runner, OwlsRunner):
                runner.abort()
        print("Interrupted; reporting the runs that finished.", file=sys.stderr)
    path = write_report(out, judge=judge, baseline=args.baseline)
    print(f"Report: {path}")
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
