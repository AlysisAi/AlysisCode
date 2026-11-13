#!/usr/bin/env python3
"""Defect regression suite for the 0.13.0 reliability wave (PRs 1-6).

WHAT THIS IS
------------
A runnable check that a built Alysis Code agent still fixes the six production
defects the 0.13.0 wave was written for. It has two halves, split on purpose:

* a **dependency-light, importable core** (everything above ``main``) that
  inspects an already-produced archive of run artifacts and asserts the
  defect-specific expectation for each task. Standard library only, no imports
  from ``alysis_code`` -- so it runs in the same bare interpreter the
  wave's other stdlib tests do, and ``tests/test_regression_suite.py`` exercises
  it against synthetic fixtures with no Docker, no Harbor, and no network.

* a **thin Harbor-invoking wrapper** (``_run_live``) that a human runs on the
  runner box -- which has Docker + Harbor + deps -- to actually produce those
  artifacts, one task at a time, with ``--include-task-name``. That half cannot
  be unit-tested here and is guarded behind ``--run``.

USAGE
-----
Offline (this box, or any box with an existing artifact archive)::

    python3 scripts/regression_suite.py --check-artifacts <dir>
    python3 scripts/regression_suite.py --check-artifacts <dir> --task compile-compcert
    python3 scripts/regression_suite.py --check-artifacts <dir> --canary-value "$ALYSIS_API_KEY"

Live (runner box, produces artifacts then asserts)::

    python3 scripts/regression_suite.py --run \
        --harbor-cmd 'bash scripts/benchmarks/terminal_bench/run_harbor_tbench.sh' \
        --artifacts-root ./runs/regression \
        --canary-value "$ALYSIS_REGRESSION_CANARY_VALUE"

Exit code: ``0`` when every hard check passed (WARN/SKIP do not fail); ``1``
when any hard check FAILED; ``2`` on a usage error.

WHAT EACH TASK PROVES (task -> PR)
---------------------------------
* compile-compcert (PR2/PR3): no ``NonZeroAgentExitCodeError``; a budget stop is
  a clean stop (``stop_reason`` present, no errored terminal status);
  ``dispatch_overhead_seconds`` telemetry present.
* hf-model-inference / configure-git-webserver / qemu-alpine-ssh (PR4): if a
  persist-mode service was started, its liveness was recorded (or the
  dead-service notice fired) -- asserted on events, never on reward.
* filter-js-from-html (PR5): a full-file rewrite warning fired (proxy for the
  guard being wired) OR the task's clean-file check passed.
* gcode-to-text (PR5): the run reached a terminal state without thrashing
  unbounded (thrash notice, if any, is bounded) -- soft signal.
* cross-cutting: zero cleartext of the canary ``ALYSIS_API_KEY`` in any
  artifact (PR1); build identity present and non-dirty in provenance (PR6).

The inspection is deliberately layout-tolerant: it walks the archive for every
``*.jsonl`` (session log, crash-diagnostics log) and reads every file as text
for the canary scan, so it works whether the artifacts came from the plain
Harbor adapter (``.alysis/`` copied to ``/logs/agent/alysis/runtime``) or
the box adapter (``/logs/artifacts/alysis-session/*.jsonl`` +
``alysis-crash.jsonl``).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# The tasks, and the model-visible / machine markers we look for.
#
# The string markers are copied here as literals rather than imported, because
# this module must load without the package. Each one is annotated with the
# source constant it mirrors so a drift is a one-line grep to catch.
# ---------------------------------------------------------------------------

#: Exactly the tasks this suite runs, in a fixed order. Passed to Harbor one at a
#: time with ``--include-task-name``.
REGRESSION_TASKS: tuple[str, ...] = (
    "compile-compcert",
    "hf-model-inference",
    "configure-git-webserver",
    "polyglot-rust-c",
    "qemu-alpine-ssh",
    "filter-js-from-html",
    "gcode-to-text",
)

#: budget_policy.STOP_REASON_RUN_BUDGET_EXHAUSTED
STOP_REASON_RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"
#: dispatch_timing.DISPATCH_OVERHEAD_OPERATION
DISPATCH_OVERHEAD_KEY = "dispatch_overhead_seconds"
#: A substring of edit_discipline.REWRITE_WARNING_TEMPLATE (PR5).
REWRITE_WARNING_MARKER = "full-file rewrite of"
#: A substring of edit_discipline.THRASH_NOTICE_TEMPLATE (PR5).
THRASH_NOTICE_MARKER = "similar attempts on"
#: A substring of service_persistence.SERVICE_CHECK_NOTICE_TEMPLATE (PR4).
DEAD_SERVICE_NOTICE_MARKER = "process started as a persistent service is no longer running"
#: The Harbor-side error the budget/exit-code fix (PR2) exists to eliminate.
NONZERO_AGENT_EXIT_MARKER = "NonZeroAgentExitCodeError"
#: run_provenance.MASKED_VALUE / the redaction placeholder prefix (PR1/PR6).
SECRET_MASK_MARKERS = ("[secret]", "«redacted:")

#: The canary env var whose value must never appear in cleartext (PR1).
CANARY_ENV_VAR = "ALYSIS_API_KEY"
#: Documented default the operator is told to set as the canary key value when
#: they cannot pass the real one. Chosen to be unmistakable and non-secret.
DEFAULT_CANARY_VALUE = "sk-alysis-canary-DO-NOT-USE-000000000000"

#: Tasks whose primary assertion is service persistence (PR4).
SERVICE_TASKS = frozenset({"hf-model-inference", "configure-git-webserver", "qemu-alpine-ssh"})

# ---------------------------------------------------------------------------
# Check + report vocabulary
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    """One asserted expectation and its outcome."""

    name: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """A check that does not fail the suite (PASS/WARN/SKIP all pass)."""
        return self.status != FAIL


@dataclass
class TaskReport:
    task: str
    pr: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)


# ---------------------------------------------------------------------------
# Artifact bundle: parse once, assert many times
# ---------------------------------------------------------------------------


def _event_type(obj: object) -> str:
    """Discriminator for a log record, tolerating both sinks.

    The session log uses ``type``; the crash-diagnostics log uses ``event``.
    """
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("type") or obj.get("event") or "").strip()


def _event_payload(obj: object) -> dict:
    if not isinstance(obj, dict):
        return {}
    payload = obj.get("payload")
    if isinstance(payload, dict):
        return payload
    # Crash-diagnostics records carry their fields at the top level; treat the
    # whole record (minus the discriminator) as the payload in that case.
    return {k: v for k, v in obj.items() if k not in ("type", "event")}


def deep_find_key(obj: object, key: str) -> Iterator[object]:
    """Yield every value stored under ``key`` anywhere in a nested structure.

    Catches ``dispatch_overhead_seconds`` whether it sits as a top-level shell
    result key or nested under ``deadline.duration_observations``.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from deep_find_key(v, key)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from deep_find_key(item, key)


@dataclass
class ArtifactBundle:
    """Everything an assertion needs from one archive, parsed once.

    ``events`` are the parsed JSONL records (session + crash). ``texts`` is the
    ``(relative_path, content)`` of every readable file, for full-text scans
    such as the canary sweep. Construct via :meth:`from_dir` for a real archive,
    or directly with synthetic data in a test.
    """

    events: list[dict] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_dir(cls, root: str | os.PathLike[str]) -> ArtifactBundle:
        root_path = Path(root)
        bundle = cls()
        if not root_path.exists():
            return bundle
        for path in sorted(_iter_files(root_path)):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = _safe_relpath(path, root_path)
            bundle.texts.append((rel, raw))
            if path.suffix == ".jsonl" or path.name.endswith(".jsonl"):
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(obj, dict):
                        bundle.events.append(obj)
        return bundle

    # -- queries ----------------------------------------------------------

    def events_of_type(self, *type_names: str) -> list[dict]:
        wanted = {t for t in type_names}
        return [e for e in self.events if _event_type(e) in wanted]

    def text_contains(self, needle: str) -> bool:
        return any(needle in content for _, content in self.texts)

    def files_containing(self, needle: str) -> list[str]:
        return [rel for rel, content in self.texts if needle in content]

    def has_key_anywhere(self, key: str) -> bool:
        return any(True for e in self.events for _ in deep_find_key(e, key))

    def config_snapshot_build(self) -> dict | None:
        """The ``build`` sub-dict of the once-per-run ``config_snapshot`` event."""
        for event in self.events_of_type("config_snapshot"):
            build = _event_payload(event).get("build")
            if isinstance(build, dict):
                return build
        return None

    def persist_service_starts(self) -> list[dict]:
        """``service_start`` events that were persist-mode."""
        out: list[dict] = []
        for event in self.events_of_type("service_start"):
            payload = _event_payload(event)
            if bool(payload.get("persist")):
                out.append(payload)
        return out


def _iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Cross-cutting assertions (apply to every task's archive)
# ---------------------------------------------------------------------------


def check_no_canary_cleartext(bundle: ArtifactBundle, canary_value: str | None) -> Check:
    """PR1: the canary API key value never appears in cleartext.

    When no canary value is supplied we cannot scan for it, so this reports SKIP
    rather than a false PASS -- the operator is told to re-run with
    ``--canary-value``.
    """
    value = (canary_value or "").strip()
    if not value:
        return Check(
            "pr1_canary_no_cleartext",
            SKIP,
            "no --canary-value given; cannot scan for the secret. "
            "Re-run with --canary-value set to the campaign's ALYSIS_API_KEY.",
        )
    hits = bundle.files_containing(value)
    if hits:
        shown = ", ".join(sorted(hits)[:5])
        return Check(
            "pr1_canary_no_cleartext",
            FAIL,
            f"canary {CANARY_ENV_VAR} value found in cleartext in: {shown}",
        )
    detail = "canary value absent from all artifacts"
    if any(bundle.text_contains(m) for m in SECRET_MASK_MARKERS):
        detail += "; redaction/masking markers present"
    return Check("pr1_canary_no_cleartext", PASS, detail)


def check_build_identity_clean(bundle: ArtifactBundle) -> Check:
    """PR6: provenance records a build that is identifiable and non-dirty."""
    build = bundle.config_snapshot_build()
    if build is None:
        return Check(
            "pr6_build_identity_clean",
            FAIL,
            "no config_snapshot event with a build record found",
        )
    clean = build.get("clean")
    dirty = build.get("dirty")
    identifiable = build.get("identifiable")
    commit = str(build.get("commit") or "")
    # Prefer the explicit derived flag; fall back to its two components.
    is_clean = bool(clean) if clean is not None else (identifiable is True and dirty is False)
    if is_clean:
        return Check(
            "pr6_build_identity_clean",
            PASS,
            f"clean build recorded (commit={commit[:12] or 'unknown'}, dirty={dirty})",
        )
    return Check(
        "pr6_build_identity_clean",
        FAIL,
        f"build not clean: clean={clean}, dirty={dirty}, identifiable={identifiable}, "
        f"commit={commit[:12] or 'unknown'}",
    )


# ---------------------------------------------------------------------------
# Per-task assertions
# ---------------------------------------------------------------------------


def _budget_stop_records(bundle: ArtifactBundle) -> list[dict]:
    """Records that assert a clean run-budget stop, from any sink."""
    out: list[dict] = []
    for event in bundle.events:
        payload = _event_payload(event)
        reason = str(payload.get("stop_reason") or payload.get("degraded_reason") or "").strip()
        if reason == STOP_REASON_RUN_BUDGET_EXHAUSTED:
            out.append(event)
        elif _event_type(event) == "deadline_exhausted":
            out.append(event)
    return out


def _errored_terminal(bundle: ArtifactBundle) -> list[dict]:
    """Terminal error records (a real failure, distinct from a budget stop)."""
    return bundle.events_of_type("terminal_error")


def evaluate_compile_compcert(bundle: ArtifactBundle) -> TaskReport:
    report = TaskReport("compile-compcert", "PR2/PR3")

    # (1) No NonZeroAgentExitCodeError anywhere in the archive.
    if bundle.text_contains(NONZERO_AGENT_EXIT_MARKER):
        hits = ", ".join(bundle.files_containing(NONZERO_AGENT_EXIT_MARKER)[:5])
        report.add(
            "pr2_no_nonzero_exit_error", FAIL, f"found {NONZERO_AGENT_EXIT_MARKER} in: {hits}"
        )
    else:
        report.add(
            "pr2_no_nonzero_exit_error", PASS, f"no {NONZERO_AGENT_EXIT_MARKER} in artifacts"
        )

    # (2) If the budget was hit, it was a clean stop, not an errored status.
    budget_stops = _budget_stop_records(bundle)
    errors = _errored_terminal(bundle)
    if budget_stops:
        if errors:
            report.add(
                "pr2_clean_budget_stop",
                FAIL,
                "budget stop recorded but a terminal_error is also present",
            )
        else:
            report.add(
                "pr2_clean_budget_stop",
                PASS,
                f"clean stop_reason={STOP_REASON_RUN_BUDGET_EXHAUSTED} present, no terminal_error",
            )
    else:
        report.add(
            "pr2_clean_budget_stop",
            SKIP,
            "no budget stop recorded (run finished within budget); nothing to assert",
        )

    # (3) dispatch_overhead_seconds telemetry present.
    if bundle.has_key_anywhere(DISPATCH_OVERHEAD_KEY):
        report.add("pr3_dispatch_overhead_present", PASS, f"{DISPATCH_OVERHEAD_KEY} present")
    else:
        report.add(
            "pr3_dispatch_overhead_present",
            FAIL,
            f"{DISPATCH_OVERHEAD_KEY} not found in any event",
        )
    return report


def evaluate_service_task(bundle: ArtifactBundle, task: str) -> TaskReport:
    report = TaskReport(task, "PR4")
    persist_starts = bundle.persist_service_starts()
    dead_notice = bundle.text_contains(DEAD_SERVICE_NOTICE_MARKER)

    if not persist_starts and not dead_notice:
        report.add(
            "pr4_service_persistence_signal",
            SKIP,
            "no persist-mode service_start and no dead-service notice; "
            "agent did not exercise persist mode this run",
        )
        return report

    if persist_starts:
        # Liveness was recorded at start time (pid_alive / liveness keys).
        recorded = [
            p
            for p in persist_starts
            if ("pid_alive" in p or "liveness" in p or "port_listening" in p)
        ]
        if recorded:
            report.add(
                "pr4_service_liveness_recorded",
                PASS,
                f"{len(recorded)} persist-mode service_start event(s) carry a liveness record",
            )
        else:
            report.add(
                "pr4_service_liveness_recorded",
                WARN,
                f"{len(persist_starts)} persist-mode start(s) but no liveness fields captured",
            )
    if dead_notice:
        report.add(
            "pr4_dead_service_notice_or_alive",
            PASS,
            "dead-service finalize notice fired (persistence re-check ran)",
        )
    elif persist_starts:
        report.add(
            "pr4_dead_service_notice_or_alive",
            PASS,
            "persist service started and no dead-service notice (service stayed alive)",
        )
    return report


def evaluate_filter_js(bundle: ArtifactBundle, *, reward: float | None = None) -> TaskReport:
    report = TaskReport("filter-js-from-html", "PR5")
    if bundle.text_contains(REWRITE_WARNING_MARKER):
        report.add(
            "pr5_rewrite_warning_or_clean",
            PASS,
            "full-file rewrite warning fired (guard is wired)",
        )
    elif reward is not None and reward > 0:
        report.add(
            "pr5_rewrite_warning_or_clean",
            PASS,
            f"no rewrite warning, but task resolved (reward={reward})",
        )
    else:
        report.add(
            "pr5_rewrite_warning_or_clean",
            WARN,
            "no rewrite warning and reward unknown: a clean targeted edit is a "
            "legitimate no-warning pass, so this is inconclusive, not a failure",
        )
    return report


def evaluate_gcode(bundle: ArtifactBundle) -> TaskReport:
    report = TaskReport("gcode-to-text", "PR5")
    # The thrash guard is bounded by construction (<= 2 notices/run); its
    # presence proves it is wired, its absence is normal. Either way, assert the
    # run reached a terminal state rather than spinning.
    terminal = bundle.events_of_type(
        "final", "run_finished", "deadline_exhausted", "terminal_error"
    )
    thrash = bundle.text_contains(THRASH_NOTICE_MARKER)
    if thrash:
        report.add("pr5_thrash_guard_wired", PASS, "thrash progress notice fired (guard is wired)")
    else:
        report.add("pr5_thrash_guard_wired", SKIP, "no thrash notice (run did not thrash)")
    if terminal:
        report.add(
            "pr5_reached_terminal_state", PASS, f"{len(terminal)} terminal record(s) present"
        )
    else:
        report.add(
            "pr5_reached_terminal_state",
            WARN,
            "no terminal record found; archive may be incomplete",
        )
    return report


def evaluate_generic(bundle: ArtifactBundle, task: str) -> TaskReport:
    """Fallback for a task with no task-specific defect assertion (polyglot-rust-c).

    polyglot-rust-c exercises the same budget/exit-code path as compile-compcert,
    so it gets those two checks plus dispatch telemetry.
    """
    report = TaskReport(task, "PR2/PR3")
    if bundle.text_contains(NONZERO_AGENT_EXIT_MARKER):
        report.add("pr2_no_nonzero_exit_error", FAIL, f"found {NONZERO_AGENT_EXIT_MARKER}")
    else:
        report.add("pr2_no_nonzero_exit_error", PASS, f"no {NONZERO_AGENT_EXIT_MARKER}")
    budget_stops = _budget_stop_records(bundle)
    if budget_stops and not _errored_terminal(bundle):
        report.add("pr2_clean_budget_stop", PASS, "clean budget stop, no terminal_error")
    elif budget_stops:
        report.add("pr2_clean_budget_stop", FAIL, "budget stop alongside a terminal_error")
    else:
        report.add("pr2_clean_budget_stop", SKIP, "no budget stop recorded")
    if bundle.has_key_anywhere(DISPATCH_OVERHEAD_KEY):
        report.add("pr3_dispatch_overhead_present", PASS, f"{DISPATCH_OVERHEAD_KEY} present")
    else:
        report.add("pr3_dispatch_overhead_present", WARN, f"{DISPATCH_OVERHEAD_KEY} absent")
    return report


def evaluate_task(
    task: str,
    bundle: ArtifactBundle,
    *,
    canary_value: str | None = None,
    reward: float | None = None,
) -> TaskReport:
    """Run one task's defect assertions plus the two cross-cutting checks."""
    if task == "compile-compcert":
        report = evaluate_compile_compcert(bundle)
    elif task in SERVICE_TASKS:
        report = evaluate_service_task(bundle, task)
    elif task == "filter-js-from-html":
        report = evaluate_filter_js(bundle, reward=reward)
    elif task == "gcode-to-text":
        report = evaluate_gcode(bundle)
    else:
        report = evaluate_generic(bundle, task)
    # Cross-cutting checks belong to every task's archive.
    report.checks.append(check_no_canary_cleartext(bundle, canary_value))
    report.checks.append(check_build_identity_clean(bundle))
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(reports: Iterable[TaskReport]) -> str:
    lines: list[str] = []
    total = passed = failed = warned = skipped = 0
    for report in reports:
        lines.append(f"\n=== {report.task}  [{report.pr}] ===")
        for check in report.checks:
            total += 1
            if check.status == PASS:
                passed += 1
            elif check.status == FAIL:
                failed += 1
            elif check.status == WARN:
                warned += 1
            else:
                skipped += 1
            lines.append(f"  [{check.status:4}] {check.name}: {check.detail}")
    lines.append(
        f"\nTOTAL {total} checks: {passed} PASS, {failed} FAIL, {warned} WARN, {skipped} SKIP"
    )
    return "\n".join(lines)


def any_failed(reports: Iterable[TaskReport]) -> bool:
    return any(r.failed for r in reports)


# ---------------------------------------------------------------------------
# Thin, untestable live wrapper: invoke Harbor per task, then inspect
# ---------------------------------------------------------------------------


def _run_live(args: argparse.Namespace) -> int:  # pragma: no cover - needs Harbor
    """Invoke Harbor once per task with --include-task-name, then assert.

    This is the half that cannot run in the sandbox. It shells out to a
    configurable Harbor command (default: the repo's TB runner), passing the
    task name via the ``TB_INCLUDE_TASK`` environment variable the runner reads
    and translates into ``--include-task-name``. Each task's artifacts are
    expected under ``<artifacts-root>/<task>``.
    """
    artifacts_root = Path(args.artifacts_root)
    artifacts_root.mkdir(parents=True, exist_ok=True)
    reports: list[TaskReport] = []
    for task in REGRESSION_TASKS:
        task_out = artifacts_root / task
        task_out.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["TB_INCLUDE_TASK"] = task
        env.setdefault("TB_ARTIFACTS_DIR", str(task_out))
        print(f"\n>>> Harbor: {task}", flush=True)
        completed = subprocess.run(  # noqa: S603 - operator-supplied command
            args.harbor_cmd,
            shell=True,
            env=env,
            cwd=str(Path.cwd()),
        )
        print(f"<<< Harbor exit={completed.returncode} for {task}", flush=True)
        bundle = ArtifactBundle.from_dir(task_out)
        reports.append(evaluate_task(task, bundle, canary_value=args.canary_value))
    print(format_report(reports))
    return 1 if any_failed(reports) else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Defect regression suite for the 0.13.0 reliability wave.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-artifacts",
        metavar="DIR",
        help="Assert against an existing artifact archive (offline; no Harbor).",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Live mode: invoke Harbor per task, then assert (runner box only).",
    )
    parser.add_argument(
        "--task",
        choices=REGRESSION_TASKS,
        help="With --check-artifacts, scope to one task's expectations.",
    )
    parser.add_argument(
        "--canary-value",
        default=os.environ.get("ALYSIS_REGRESSION_CANARY_VALUE"),
        help="The campaign's ALYSIS_API_KEY value to scan for in cleartext "
        f"(default: $ALYSIS_REGRESSION_CANARY_VALUE; documented sentinel is "
        f"{DEFAULT_CANARY_VALUE!r}).",
    )
    parser.add_argument(
        "--reward",
        type=float,
        default=None,
        help="Optional task reward (0/1) to let filter-js-from-html pass on a "
        "clean edit that produced no rewrite warning.",
    )
    parser.add_argument(
        "--harbor-cmd",
        default="bash scripts/benchmarks/terminal_bench/run_harbor_tbench.sh",
        help="Live mode only: the Harbor command; reads TB_INCLUDE_TASK per task.",
    )
    parser.add_argument(
        "--artifacts-root",
        default="./runs/regression",
        help="Live mode only: where per-task artifacts are collected.",
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
    bundle = ArtifactBundle.from_dir(root)
    if not bundle.events and not bundle.texts:
        print(f"error: no readable artifacts under {root}", file=sys.stderr)
        return 2

    tasks = (args.task,) if args.task else REGRESSION_TASKS
    reports = [
        evaluate_task(task, bundle, canary_value=args.canary_value, reward=args.reward)
        for task in tasks
    ]
    print(format_report(reports))
    return 1 if any_failed(reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
