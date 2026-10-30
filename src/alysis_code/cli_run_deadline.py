"""Finalize an expired managed CLI launch before loading the execution runtime.

This boundary runs after normal CLI argument, workspace and named-session
admission. It never starts a provider, tools, hooks or an alternate agent. Live
budgets and invalid configurations continue through the normal runtime path.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Any

from .managed_host_deadline import MANAGED_HOST_DEADLINE_UNIX_ENV


def finish_expired_managed_launch(**options: Any) -> int | None:
    # Only a required, host-anchored, one-shot invocation has this contract.
    # Interactive turns retain their normal session lifecycle and media policy.
    if not options.get("require_run_deadline") or not options.get("one_shot_execution"):
        return None
    if options.get("execution_deadline") is not None or options.get("no_run_deadline"):
        return None
    if options.get("session_id_override") and options.get("managed_session_persistence") is None:
        # Explicit names must already hold the CLI's sealed filesystem capability.
        return None
    anchor = os.environ.get(MANAGED_HOST_DEADLINE_UNIX_ENV)
    try:
        anchor_value = float(anchor) if anchor is not None else math.inf
    except (ValueError, TypeError):
        return None
    if not math.isfinite(anchor_value) or anchor_value <= 0 or anchor_value > time.time():
        return None

    from .config import resolve_run_deadline
    from .execution_deadline import ExecutionDeadline
    from .managed_host_deadline import clamp_to_managed_host_deadline

    cfg = options["cfg"]
    resolved = resolve_run_deadline(cfg, cli_deadline_seconds=options.get("run_deadline_seconds"))
    if resolved.seconds is None:
        # Preserve the regular missing-required-deadline diagnostic.
        return None
    deadline = ExecutionDeadline.from_duration(resolved.seconds, source=resolved.source)
    anchor_record = clamp_to_managed_host_deadline(deadline, anchor_value)
    if anchor_record["status"] != "exhausted":
        return None

    from .atomic_io import atomic_write_json
    from .branding import env_get
    from .budget_policy import BUDGET_STOP_EXIT_CODE, STOP_REASON_RUN_BUDGET_EXHAUSTED
    from .config import ConfigError
    from .run_outcome import task_outcome_record
    from .runtime_kind import resolve_session_runtime_kind
    from .session_store import (
        SessionStore,
        SessionStoreUnavailableError,
        make_session_id,
        resolve_sessions_dir,
    )

    runtime = resolve_session_runtime_kind(
        runtime_kind=options.get("runtime_kind"), one_shot_execution=True, subagent_depth=0
    )
    persistence = options.get("managed_session_persistence")
    session_id = options.get("session_id_override") or make_session_id()
    sessions_dir = options.get("session_log_dir_override")
    sessions_dir = Path(sessions_dir) if sessions_dir is not None else resolve_sessions_dir(cfg)
    root = Path(options["root"])
    no_log = bool(options.get("no_log"))
    try:
        store = SessionStore(
            enabled=not no_log,
            artifact_persistence_enabled=not no_log
            or options.get("session_log_dir_override") is not None,
            sessions_dir=sessions_dir,
            session_id=session_id,
            cwd=str(root),
            repo_root=str(root),
            workspace_root=str(root),
            runtime_kind=runtime.value,
            managed_session_persistence=persistence,
        )
    except SessionStoreUnavailableError as exc:
        raise ConfigError(f"Session log unavailable: {exc}") from exc
    try:
        record = task_outcome_record(exit_code=BUDGET_STOP_EXIT_CODE, reason="deadline_exhausted")
        record.update(session_id=store.session_id, runtime_kind=runtime.value)
        payload = {
            "operation": "managed_launch",
            "stop_reason": STOP_REASON_RUN_BUDGET_EXHAUSTED,
            "deadline_exhausted": True,
            "deadline": deadline.telemetry_snapshot(),
        }
        for event, value in (
            ("managed_host_deadline_anchor", anchor_record),
            ("deadline_exhausted", payload),
            ("turn_outcome", record),
        ):
            try:
                store.append(event, value)
            except Exception:
                # As in AgentSession, an admitted log failing later must not
                # suppress the independent, explicitly requested host receipt.
                pass
        if store.artifact_persistence_enabled:
            try:
                atomic_write_json(store.path.with_suffix(".outcome.json"), record)
            except OSError:
                pass
        host_output = env_get("ALYSIS_TASK_OUTCOME_PATH")
        if host_output:
            atomic_write_json(Path(host_output).expanduser().resolve(), record)
        logger = options.get("crash_diagnostic_logger")
        if logger is None and options.get("crash_diagnostic_log_path"):
            from .crash_diagnostics import build_crash_diagnostic_logger

            logger = build_crash_diagnostic_logger(
                path=options["crash_diagnostic_log_path"],
                run_id=store.session_id,
                session_id=store.session_id,
                runtime_kind=runtime.value,
            )
        if logger is not None:
            logger.event("managed_host_deadline_anchor", anchor_record, durable=True)
            logger.event("deadline_exhausted", payload, durable=True)
        console = options.get("console")
        if console is not None:
            console.print("The run deadline expired during startup. No task actions were started.")
        return BUDGET_STOP_EXIT_CODE
    finally:
        store.close()
