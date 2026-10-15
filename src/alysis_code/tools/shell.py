from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..pipeline_facts import (
    build_pipeline_status_capture_command,
    command_has_top_level_pipe,
    extract_pipeline_status,
    pipeline_meaningful_stage,
    split_top_level_pipeline,
)
from ..process_reaping import CommandCancelledError, runner_takes_cancel_check


class ShellError(RuntimeError):
    def __init__(self, message: str, *, result_payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        # The tool result to record instead of the bare message, when there is one.
        self.result_payload = result_payload


CANCELLED_COMMAND_MESSAGE = "Command stopped because the run was cancelled."
_OUTPUT_LIMIT = 20000


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) > _OUTPUT_LIMIT:
        return text[:_OUTPUT_LIMIT] + "...(truncated)", True
    return text, False


_PYTHON_CMD_PATTERN = re.compile(r"(^|(?:&&|\|\||;)\s*)python(?=\s)")


def _python3_fallback_cmd(cmd: str) -> str | None:
    match = _PYTHON_CMD_PATTERN.search(cmd)
    if not match:
        return None
    start, end = match.span()
    prefix = match.group(1)
    replacement = f"{prefix}python3"
    return f"{cmd[:start]}{replacement}{cmd[end:]}"


def _is_python_permission_denied(stderr: str) -> bool:
    lowered = stderr.lower()
    return "python: permission denied" in lowered


def _pipeline_capture_platform_ok() -> bool:
    # PIPESTATUS capture uses a POSIX-shell wrapper; it is unavailable under the
    # Windows cmd.exe shell. (Kept as a separate function so tests can exercise
    # the capture path on any host.)
    return os.name != "nt"


def shell_run(
    *,
    root: Path,
    cmd: str,
    cwd: str | None = None,
    timeout_s: float = 60,
    runner: Any | None = None,
    capture_pipeline_status: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    base = root.resolve()
    if cwd:
        cwd_path = (base / cwd).resolve()
        try:
            cwd_path.relative_to(base)
        except ValueError as e:
            raise ShellError(f"cwd escapes root: {cwd}") from e
    else:
        cwd_path = base

    if runner is None:
        raise ShellError("Shell runner is required; implicit host execution is disabled.")

    # Observe per-stage pipeline exit codes (bash PIPESTATUS) only when it would
    # matter and can be done safely: a top-level pipeline is present, on a POSIX
    # host. The wrapper preserves stdout/stderr/exit-code exactly, so plain
    # commands and non-capturing runs behave identically to before.
    wants_capture = bool(
        capture_pipeline_status
        and _pipeline_capture_platform_ok()
        and pipeline_meaningful_stage(cmd) is not None
    )

    # A Stop (or the run's budget) ends a command that is still running, when
    # the caller can see the run's cancellation and the runner can stop it.
    run_kwargs: dict[str, Any] = {}
    if cancel_check is not None and runner_takes_cancel_check(runner):
        run_kwargs["cancel_check"] = cancel_check

    def _run(command_text: str) -> tuple[Any, list[int] | None, str]:
        run_cmd = (
            build_pipeline_status_capture_command(command_text) if wants_capture else command_text
        )
        completed = runner.run(
            root=base, cwd=cwd_path, cmd=run_cmd, timeout_s=timeout_s, **run_kwargs
        )
        if wants_capture:
            status, cleaned_stderr = extract_pipeline_status(completed.stderr or "")
            return completed, status, cleaned_stderr
        return completed, None, (completed.stderr or "")

    def _cancelled(e: CommandCancelledError) -> ShellError:
        # Reported with what the command printed before it was stopped, as a
        # cancelled ``shell_wait`` reports its output; the turn then ends.
        partial_stdout, stdout_truncated = _truncate(str(e.stdout or ""))
        partial_stderr, stderr_truncated = _truncate(str(e.stderr or ""))
        return ShellError(
            CANCELLED_COMMAND_MESSAGE,
            result_payload={
                "error": CANCELLED_COMMAND_MESSAGE,
                "cancelled": True,
                "cmd": cmd,
                "cwd": str(cwd_path),
                "stdout": partial_stdout,
                "stderr": partial_stderr,
                "truncated": stdout_truncated or stderr_truncated,
            },
        )

    try:
        cp, stage_status, stderr_text = _run(cmd)
    except subprocess.TimeoutExpired as e:
        raise ShellError(f"Command timed out after {timeout_s:g}s") from e
    except CommandCancelledError as e:
        raise _cancelled(e) from e
    except Exception as e:  # noqa: BLE001
        raise ShellError(f"Failed to run command: {e}") from e

    effective_cmd = cmd
    fallback_cmd = _python3_fallback_cmd(cmd)
    if fallback_cmd and cp.returncode != 0 and _is_python_permission_denied(stderr_text):
        try:
            cp, stage_status, stderr_text = _run(fallback_cmd)
            effective_cmd = fallback_cmd
        except subprocess.TimeoutExpired as e:
            raise ShellError(f"Command timed out after {timeout_s:g}s") from e
        except CommandCancelledError as e:
            raise _cancelled(e) from e
        except Exception as e:  # noqa: BLE001
            raise ShellError(f"Failed to run command: {e}") from e

    stdout, stdout_truncated = _truncate(cp.stdout or "")
    stderr, stderr_truncated = _truncate(stderr_text)
    truncated = stdout_truncated or stderr_truncated

    # A plain command (no top-level pipe) has a one-element status list equal to
    # its exit code. A pipeline whose per-stage status could not be observed
    # leaves the list as None so the classifier falls back to a single re-run.
    if stage_status is None and not command_has_top_level_pipe(effective_cmd):
        stage_status = [cp.returncode]
    pipeline_stages = split_top_level_pipeline(effective_cmd) or [effective_cmd]

    return {
        "cmd": cmd,
        "effective_cmd": effective_cmd,
        "cwd": str(cwd_path),
        "exit_code": cp.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "pipeline_stages": pipeline_stages,
        "pipeline_stage_status": stage_status,
    }
