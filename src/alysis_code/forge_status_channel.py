"""Authenticated private-fd status channel for Alysis Code desktop Forge runs.

The public Forge stdout/stderr surfaces remain human- and machine-event output.
This module writes the small, nonce-bound lifecycle contract consumed by the
Desktop App to a private descriptor (fd 3 in the packaged runtime).
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any

from .branding import env_get

FORGE_STATUS_PROTOCOL = "alysis.forge.status"
FORGE_STATUS_SCHEMA_VERSION = 1
FORGE_STATUS_FD_ENV = "ALYSIS_RUNTIME_STATUS_FD"
FORGE_STATUS_NONCE_ENV = "ALYSIS_RUNTIME_STATUS_NONCE"
FORGE_OPERATION_ID_ENV = "ALYSIS_RUNTIME_OPERATION_ID"


class ForgeTerminalStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PREFLIGHT_BLOCKED = "preflight_blocked"
    INFRASTRUCTURE_UNAVAILABLE = "infrastructure_unavailable"
    VERIFICATION_FAILED = "verification_failed"
    EXECUTION_FAILED = "execution_failed"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    REVIEW_CHANGES_REQUESTED_EXHAUSTED = "review_changes_requested_exhausted"
    PROVIDER_FAILED = "provider_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MERGE_FAILED = "merge_failed"
    CANCELLED = "cancelled"


ALLOWED_FORGE_TERMINAL_STATUSES = frozenset(status.value for status in ForgeTerminalStatus)
FORGE_STATUS_EXIT_CODES = {
    ForgeTerminalStatus.SUCCEEDED: 0,
    ForgeTerminalStatus.VERIFICATION_FAILED: 1,
    ForgeTerminalStatus.EXECUTION_FAILED: 1,
    ForgeTerminalStatus.REVIEW_CHANGES_REQUESTED: 1,
    ForgeTerminalStatus.REVIEW_CHANGES_REQUESTED_EXHAUSTED: 1,
    ForgeTerminalStatus.PREFLIGHT_BLOCKED: 2,
    ForgeTerminalStatus.INFRASTRUCTURE_UNAVAILABLE: 3,
    ForgeTerminalStatus.PROVIDER_FAILED: 4,
    ForgeTerminalStatus.BUDGET_EXHAUSTED: 5,
    ForgeTerminalStatus.MERGE_FAILED: 6,
    ForgeTerminalStatus.CANCELLED: 130,
}


@dataclass(frozen=True)
class ForgePlanResult:
    """Structured legacy-planner result retained for status-channel callers."""

    succeeded: bool
    terminal_status: ForgeTerminalStatus
    reason_code: str
    message: str
    action: str | None = None
    failure_category: str | None = None
    details: tuple[dict[str, Any], ...] = ()
    provider_calls: int = 0
    retryable: bool = False

    def __bool__(self) -> bool:
        return self.succeeded

    @property
    def exit_code(self) -> int:
        return FORGE_STATUS_EXIT_CODES[self.terminal_status]

    @classmethod
    def completed(cls, *, provider_calls: int) -> ForgePlanResult:
        return cls(
            succeeded=True,
            terminal_status=ForgeTerminalStatus.SUCCEEDED,
            reason_code="plan_completed",
            message="Forge saved a valid plan for review.",
            provider_calls=provider_calls,
        )

    @classmethod
    def incomplete(cls, *, provider_calls: int = 0) -> ForgePlanResult:
        return cls(
            succeeded=False,
            terminal_status=ForgeTerminalStatus.EXECUTION_FAILED,
            reason_code="plan_incomplete",
            message="Forge planning ended before a valid draft was finalized.",
            action="Retry the planner and answer any remaining clarification.",
            provider_calls=provider_calls,
            retryable=True,
        )


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,255}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+|api[_ -]?key\s*[:=]\s*|token\s*[:=]\s*|secret\s*[:=]\s*)\S+"
    r"|\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{8,}\b"
)
_CONTENT_FIELD_RE = re.compile(
    r"\b(?:prompt|command|messages?|authorization|tools?)\s*[:=]\s*.+",
    re.IGNORECASE | re.DOTALL,
)
_MAX_SAFE_INTEGER = 2**53 - 1


class ForgeStatusChannelError(RuntimeError):
    """The authenticated status channel could not be configured or written."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Invalid Forge {label}")
    return value


def _nonce(value: str) -> str:
    if not isinstance(value, str) or not _NONCE_RE.fullmatch(value):
        raise ValueError("Invalid Forge status nonce")
    return value


def _status(value: ForgeTerminalStatus | str) -> ForgeTerminalStatus:
    try:
        return ForgeTerminalStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Unsupported Forge terminal status: {value!r}") from exc


def _sanitize_text(value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Forge status text must be a string")
    cleaned = _SECRET_RE.sub("[redacted]", value)
    cleaned = _CONTENT_FIELD_RE.sub("[redacted]", cleaned)
    cleaned = _CONTROL_RE.sub(" ", cleaned)
    collapsed = " ".join(cleaned.split())
    # Electron validates JavaScript string lengths (UTF-16 code units), while
    # Python slices Unicode code points. Clip in UTF-16 so astral characters
    # cannot make a producer-valid record fail the App decoder's exact bound.
    encoded = collapsed.encode("utf-16-le")[: maximum * 2]
    return encoded.decode("utf-16-le", errors="ignore") or None


def _exception_exit_code(exc: BaseException | None) -> int:
    if exc is None:
        return 0
    if isinstance(exc, KeyboardInterrupt):
        return 130
    for attribute in ("exit_code", "code"):
        raw = getattr(exc, attribute, None)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return 1


class ForgeStatusChannel:
    """Emit authenticated progress followed by exactly one terminal JSONL record."""

    def __init__(self, *, operation_id: str, nonce: str, fd: int) -> None:
        self.operation_id = _identifier(operation_id, "operation id")
        self.nonce = _nonce(nonce)
        if isinstance(fd, bool) or not isinstance(fd, int) or fd < 3:
            raise ValueError("Forge status fd must be a private descriptor")
        self.fd = fd
        self._lock = threading.Lock()
        self._terminal_emitted = False
        self._sequence = 0

    @classmethod
    def from_environment(cls, *, required: bool = True) -> ForgeStatusChannel | None:
        """Build the channel from the all-or-nothing Desktop environment tuple."""

        # Canonical ALYSIS_* names; env_get also accepts the pre-rename ALYSIS_*
        # spelling that older desktop builds set.
        raw_fd = env_get(FORGE_STATUS_FD_ENV)
        operation_id = env_get(FORGE_OPERATION_ID_ENV, "") or ""
        nonce = env_get(FORGE_STATUS_NONCE_ENV, "") or ""
        if raw_fd is None and not operation_id and not nonce and not required:
            return None
        if raw_fd is None or not operation_id or not nonce:
            raise ForgeStatusChannelError("Forge status environment is incomplete")
        try:
            return cls(operation_id=operation_id, nonce=nonce, fd=int(raw_fd))
        except (TypeError, ValueError) as exc:
            raise ForgeStatusChannelError(str(exc)) from exc

    @property
    def emitted(self) -> bool:
        with self._lock:
            return self._terminal_emitted

    def _write_payload_locked(self, payload: dict[str, Any], *, terminal: bool) -> bool:
        if self._terminal_emitted:
            return False
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        try:
            written = os.write(self.fd, encoded)
        except OSError as exc:
            raise ForgeStatusChannelError("Unable to write Forge status") from exc
        if written != len(encoded):
            raise ForgeStatusChannelError("Incomplete Forge status write")
        if terminal:
            self._terminal_emitted = True
        return True

    def emit_progress(
        self,
        *,
        phase: str,
        message: str,
        task_id: str | None = None,
        run_id: str | None = None,
        heartbeat: bool = False,
    ) -> bool:
        resolved_phase = _identifier(phase, "progress phase")
        if not isinstance(heartbeat, bool):
            raise ValueError("Forge progress heartbeat must be a boolean")
        resolved_task_id = _identifier(task_id, "task id") if task_id is not None else None
        resolved_run_id = _identifier(run_id, "run id") if run_id is not None else None
        sanitized_message = _sanitize_text(message, maximum=2048) or "Forge is working."

        with self._lock:
            if self._terminal_emitted:
                return False
            self._sequence += 1
            payload: dict[str, Any] = {
                "protocol": FORGE_STATUS_PROTOCOL,
                "schema_version": FORGE_STATUS_SCHEMA_VERSION,
                "operation_id": self.operation_id,
                "nonce": self.nonce,
                "event": "heartbeat" if heartbeat else "progress",
                "status": "running",
                "sequence": self._sequence,
                "phase": resolved_phase,
                "message": sanitized_message,
            }
            if resolved_task_id is not None:
                payload["task_id"] = resolved_task_id
            if resolved_run_id is not None:
                payload["run_id"] = resolved_run_id
            return self._write_payload_locked(payload, terminal=False)

    def emit(
        self,
        status: ForgeTerminalStatus | str,
        *,
        reason_code: str | None = None,
        exit_code: int | None = None,
        message: str | None = None,
        action: str | None = None,
        run_id: str | None = None,
        provider_calls: int | None = None,
        retryable: bool = False,
    ) -> bool:
        resolved_status = _status(status)
        resolved_reason = _identifier(reason_code or resolved_status.value, "reason code")
        expected_exit = FORGE_STATUS_EXIT_CODES[resolved_status]
        resolved_exit = expected_exit if exit_code is None else exit_code
        if isinstance(resolved_exit, bool) or not isinstance(resolved_exit, int):
            raise ValueError("Forge status exit code must be an integer")
        if resolved_exit != expected_exit:
            raise ValueError("Forge status exit code does not match terminal status")
        if not isinstance(retryable, bool):
            raise ValueError("Forge status retryable must be a boolean")
        if provider_calls is not None and (
            isinstance(provider_calls, bool)
            or not isinstance(provider_calls, int)
            or provider_calls < 0
            or provider_calls > _MAX_SAFE_INTEGER
        ):
            raise ValueError("Forge status provider_calls must be a non-negative safe integer")
        resolved_run_id = _identifier(run_id, "run id") if run_id is not None else None
        payload: dict[str, Any] = {
            "protocol": FORGE_STATUS_PROTOCOL,
            "schema_version": FORGE_STATUS_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "nonce": self.nonce,
            "status": resolved_status.value,
            "reason_code": resolved_reason,
            "exit_code": resolved_exit,
            "message": _sanitize_text(message, maximum=4096)
            or resolved_status.value.replace("_", " ").capitalize(),
            "retryable": retryable,
        }
        sanitized_action = _sanitize_text(action, maximum=2048)
        if sanitized_action is not None:
            payload["action"] = sanitized_action
        if resolved_run_id is not None:
            payload["run_id"] = resolved_run_id
        if provider_calls is not None:
            payload["provider_calls"] = provider_calls
        with self._lock:
            return self._write_payload_locked(payload, terminal=True)


class ForgeExecutionStatusSession:
    """Fallback terminal-emission guard for a Forge execution command.

    Known command outcomes should emit their specific terminal status directly.
    This context manager then becomes a no-op because the channel is terminal-once.
    If a command exits unexpectedly, it emits a generic, content-free fallback that
    matches the public exit-code family without copying exception text to Desktop.
    """

    def __init__(
        self,
        channel: ForgeStatusChannel | None,
        *,
        command: str,
        run_id: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.channel = channel
        self.command = _identifier(command.replace("-", "_"), "command")
        self.run_id = _identifier(run_id, "run id") if run_id is not None else None
        self.dry_run = bool(dry_run)

    def __enter__(self) -> ForgeExecutionStatusSession:
        return self

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = _identifier(run_id, "run id")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        del exc_type, tb
        if self.channel is None or self.channel.emitted:
            return False
        exit_code = _exception_exit_code(exc)
        if exit_code == 0:
            suffix = "dry_run_completed" if self.dry_run else "completed"
            self.channel.emit(
                ForgeTerminalStatus.SUCCEEDED,
                reason_code=f"{self.command}_{suffix}",
                message=(
                    f"{self.command.replace('_', ' ').title()} dry run completed successfully."
                    if self.dry_run
                    else f"{self.command.replace('_', ' ').title()} completed successfully."
                ),
                run_id=self.run_id,
                provider_calls=0 if self.dry_run else None,
            )
            return False

        status_by_exit_code = {
            1: ForgeTerminalStatus.EXECUTION_FAILED,
            2: ForgeTerminalStatus.PREFLIGHT_BLOCKED,
            3: ForgeTerminalStatus.INFRASTRUCTURE_UNAVAILABLE,
            4: ForgeTerminalStatus.PROVIDER_FAILED,
            5: ForgeTerminalStatus.BUDGET_EXHAUSTED,
            6: ForgeTerminalStatus.MERGE_FAILED,
            130: ForgeTerminalStatus.CANCELLED,
        }
        status = status_by_exit_code.get(exit_code, ForgeTerminalStatus.EXECUTION_FAILED)
        reason_suffix = {
            ForgeTerminalStatus.EXECUTION_FAILED: "execution_failed",
            ForgeTerminalStatus.PREFLIGHT_BLOCKED: "preflight_blocked",
            ForgeTerminalStatus.INFRASTRUCTURE_UNAVAILABLE: "infrastructure_unavailable",
            ForgeTerminalStatus.PROVIDER_FAILED: "provider_failed",
            ForgeTerminalStatus.BUDGET_EXHAUSTED: "budget_exhausted",
            ForgeTerminalStatus.MERGE_FAILED: "merge_failed",
            ForgeTerminalStatus.CANCELLED: "cancelled",
        }[status]
        self.channel.emit(
            status,
            reason_code=f"{self.command}_{reason_suffix}",
            message=f"{self.command.replace('_', ' ').title()} did not complete successfully.",
            action=(
                "Review the Forge output, correct the blocking condition, and retry."
                if status is not ForgeTerminalStatus.CANCELLED
                else None
            ),
            run_id=self.run_id,
            retryable=status is not ForgeTerminalStatus.CANCELLED,
        )
        return False


def emit_forge_terminal_status(
    *,
    fd: int,
    operation_id: str,
    nonce: str,
    status: ForgeTerminalStatus | str,
    **fields: Any,
) -> bool:
    """Emit one terminal record without retaining a channel instance."""

    return ForgeStatusChannel(operation_id=operation_id, nonce=nonce, fd=fd).emit(status, **fields)


ForgeStatus = ForgeTerminalStatus
ForgeProviderStatusChannel = ForgeStatusChannel
