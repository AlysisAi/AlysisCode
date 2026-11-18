from __future__ import annotations

import json
import math
import os
import threading
import time
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .error_text import sanitize_error_summary
from .failure_category import classify_failure_category, extract_status_code
from .logging_redaction import redact_log_text
from .transport_retry import transport_connection_failure_reason

CRASH_DIAGNOSTIC_SCHEMA_VERSION = 1

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}

_ALLOWED_FIELDS = {
    "budget_seconds",
    "checkpoint_fraction",
    "completion_gate_decision",
    "deadline_config_source",
    "deadline",
    "deadline_configured_seconds",
    "deadline_exhausted",
    "deadline_finalization_reason",
    "deadline_finalization_reserve_seconds",
    "deadline_normal_work_remaining_seconds",
    "deadline_phase",
    "deadline_prevented_launch",
    "deadline_remaining_seconds",
    "deadline_start_decision",
    "duration_ms",
    "elapsed_fraction",
    "elapsed_seconds",
    "error_summary",
    "error_type",
    "exit_code",
    "failure_category",
    "grace_seconds",
    "material_edit_count",
    "max_steps",
    "model",
    "operation",
    "provider_status_code",
    "reason",
    "remaining_seconds",
    "runtime_kind",
    "session_source",
    "status",
    "step",
    "steps_attempted",
    "steps_completed",
    # Machine-readable terminal marker. Without this entry the key is silently
    # dropped by the allowlist filter below and a budget stop stays
    # indistinguishable from a crash in the crash log.
    "stop_reason",
    "subagent",
    "subagent_role",
    "subagent_session_id",
    "success",
    "termination_kind",
    "tool_name",
    "usage_counts",
    "verification_attempt_count",
}
_NESTED_ALLOWED_FIELDS = {
    "allowed",
    "average_seconds",
    "configured_seconds",
    "count",
    "deadline_monotonic",
    "dispatch_overhead_seconds",
    "duration_observations",
    "elapsed_seconds",
    "enabled",
    "estimated_duration_seconds",
    "exhausted",
    "finalization_directive_sent",
    "finalization_llm_started",
    "finalization_reason",
    "finalization_reserve_seconds",
    "adaptive_retry_llm",
    "compaction_llm",
    "exploration_tool",
    "local_final_summary",
    "main_llm",
    "main_llm_retry",
    "max_seconds",
    "minimum_required_seconds",
    "mutation_tool",
    "normal_work_remaining_seconds",
    "operation",
    "phase",
    "provider_retry_sleep",
    "remaining_seconds",
    "shell_background",
    "shell_tool",
    "source",
    "subagent",
    "tool_dispatch",
    "verification",
}

_HIGH_VALUE_EVENTS = {
    "run_started",
    "terminal_error",
    "deadline_exhausted",
    "progress_checkpoint_failed",
    "run_budget_watchdog_fired",
    "run_finished",
    "turn_finished",
}


def _lock_for_path(path: Path) -> threading.Lock:
    key = os.fspath(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _now_ts() -> str:
    return datetime.now(UTC).isoformat()


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _safe_scalar(item)
            for key, item in value.items()
            if str(key) in _ALLOWED_FIELDS or str(key) in _NESTED_ALLOWED_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(item) for item in list(value)[:20]]
    return str(value)[:120]


@dataclass
class CrashDiagnosticLogger:
    path: Path | None
    run_id: str
    session_id: str
    runtime_kind: str = ""
    _started_at_monotonic: float = field(default_factory=time.monotonic)
    write_failures: int = 0

    @classmethod
    def disabled(cls) -> CrashDiagnosticLogger:
        return cls(path=None, run_id="", session_id="", runtime_kind="")

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        durable: bool = False,
    ) -> None:
        if self.path is None:
            return
        event_type = str(event_type or "").strip()
        if not event_type:
            return
        raw_payload = dict(payload or {})
        safe_payload = {
            key: _safe_scalar(value) for key, value in raw_payload.items() if key in _ALLOWED_FIELDS
        }
        if self.runtime_kind and "runtime_kind" not in safe_payload:
            safe_payload["runtime_kind"] = self.runtime_kind
        event = {
            "schema_version": CRASH_DIAGNOSTIC_SCHEMA_VERSION,
            "event_type": event_type,
            "ts": _now_ts(),
            "monotonic_elapsed_seconds": round(
                max(0.0, time.monotonic() - self._started_at_monotonic),
                6,
            ),
            "run_id": self.run_id,
            "session_id": self.session_id,
            "payload": safe_payload,
        }
        self._write_line(event, durable=durable or event_type in _HIGH_VALUE_EVENTS)

    def _write_line(self, event: dict[str, Any], *, durable: bool) -> None:
        assert self.path is not None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = redact_log_text(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
            lock = _lock_for_path(self.path)
            with lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                    if durable:
                        os.fsync(fh.fileno())
        except Exception as exc:
            # A broken diagnostic sink must not crash the run, but it must not be
            # silent either: an autonomous fix-loop that "captured nothing" while
            # believing it captured everything is worse than a loud one-time warning.
            self.write_failures += 1
            if self.write_failures == 1:
                warnings.warn(
                    f"crash diagnostic write to {self.path} failed "
                    f"({type(exc).__name__}: {exc}); subsequent write failures are counted "
                    "in write_failures but not re-warned.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return


def build_crash_diagnostic_logger(
    *,
    path: str | os.PathLike[str] | None,
    run_id: str,
    session_id: str,
    runtime_kind: str = "",
) -> CrashDiagnosticLogger:
    raw = str(path or "").strip()
    if not raw:
        return CrashDiagnosticLogger.disabled()
    return CrashDiagnosticLogger(
        path=Path(raw),
        run_id=run_id,
        session_id=session_id,
        runtime_kind=runtime_kind,
    )


def build_error_event_fields(
    error: BaseException | str | None,
    *,
    operation: str | None = None,
    step: int | None = None,
) -> dict[str, Any]:
    """Build the redacted, joinable error block for a terminal/diagnostic event.

    The block answers the question an autonomous fix-loop actually needs after a
    failed build — *why* did it die — without ever persisting a secret:

    - ``error_type``: the exception class name (e.g. ``LLMError``),
    - ``error_summary``: the message, secret-redacted and length-bounded via the
      shared :func:`sanitize_error_summary`,
    - ``failure_category``: a real :class:`FailureCategory` (never the legacy
      ``"llm_error"`` literal) so the cause joins across chat/run artifacts,
    - ``provider_status_code``: the HTTP status when one can be recovered.

    Safe to call from inside a failure path: it never raises.
    """
    fields: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error_summary": sanitize_error_summary(str(error or "")),
        "failure_category": classify_failure_category(error).value,
    }
    status_code = extract_status_code(error)
    if status_code is not None:
        fields["provider_status_code"] = status_code
    if isinstance(error, BaseException):
        # A run killed by a route that kept dropping the connection is not the
        # same event as a model that failed the task, but both landed in
        # diagnostics as an anonymous infrastructure error. Present only when
        # it applies, so every other error's block is unchanged.
        transport_reason = transport_connection_failure_reason(error)
        if transport_reason is not None:
            fields["stop_reason"] = transport_reason
    if operation:
        fields["operation"] = str(operation)
    if step is not None:
        fields["step"] = int(step)
    return fields
