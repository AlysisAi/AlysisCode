from __future__ import annotations

import os
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
from ..process_reaping import CommandCancelledError, CommandCleanupError, runner_takes_cancel_check
from ..sandbox_runner import execution_scope_fingerprint
from ..sandbox_workspace import contains_workspace_path, runner_workspace_roots
from .errors import ShellError

CANCELLED_COMMAND_MESSAGE = "Command stopped because the run was cancelled."
_OUTPUT_LIMIT = 20000


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) > _OUTPUT_LIMIT:
        return text[:_OUTPUT_LIMIT] + "...(truncated)", True
    return text, False


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
        if not contains_workspace_path(base, cwd_path, runner_workspace_roots(runner)):
            raise ShellError(f"cwd escapes root and approved additional folders: {cwd}")
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
        if os.name == "nt":
            from ..git_native import native_argv
            from ..git_shell_inspection import inspection_argv
            from ..sandbox_runner import LazyShellRunner
            from ..windows_mxc import WindowsMxcRunner

            argv = inspection_argv(command_text)
            mutation = native_argv(command_text)
            native_read = native_argv(command_text, inspection=True)
            # The desktop retains a lazy runner. Resolve its actual authorized
            # backend, not mutable config or a guessed backend string. Loading
            # validates the helper; it does not prepare a writable command.
            actual_runner = (
                runner._resolve_runner()
                if (argv is not None or mutation is not None or native_read is not None)
                and isinstance(runner, LazyShellRunner)
                else runner
            )
            if native_read is not None and isinstance(actual_runner, WindowsMxcRunner):
                from ..git_native import run

                roots = [base, *(Path(p).resolve() for p in runner_workspace_roots(runner))]
                granted_root = next(p for p in roots if cwd_path.is_relative_to(p))
                completed = run(
                    settings=actual_runner.settings,
                    root=granted_root,
                    cwd=cwd_path,
                    argv=native_read,
                    timeout_s=max(1, int(timeout_s)),
                    cancel_check=cancel_check,
                    inspection=True,
                )
                return completed, [completed.returncode], completed.stderr
            if argv is not None and isinstance(actual_runner, WindowsMxcRunner):
                from ..contained_read_tools import ContainedReadTools

                # Keep the existing approval and cwd checks above. Select the
                # narrower read-only worker before shell setup can reserve files.
                roots = [base, *(Path(p).resolve() for p in runner_workspace_roots(runner))]
                granted_root = next(p for p in roots if cwd_path.is_relative_to(p))
                result = ContainedReadTools(actual_runner.settings, granted_root).run(
                    "git_shell_inspect",
                    {"argv": argv, "cwd": str(cwd_path.relative_to(granted_root))},
                    cancel_check=cancel_check,
                    timeout_s=max(1, int(timeout_s)),
                )
                completed = subprocess.CompletedProcess(
                    command_text, result["exit_code"], result["stdout"], result["stderr"]
                )
                completed.inspection_truncated = result["truncated"]
                completed.inspection_backend = "contained-readonly-git"
                return completed, [completed.returncode], completed.stderr
            if mutation is not None and isinstance(actual_runner, WindowsMxcRunner):
                from ..git_native import run

                roots = [base, *(Path(p).resolve() for p in runner_workspace_roots(runner))]
                granted_root = next(p for p in roots if cwd_path.is_relative_to(p))
                completed = run(
                    settings=actual_runner.settings,
                    root=granted_root,
                    cwd=cwd_path,
                    argv=mutation,
                    timeout_s=max(1, int(timeout_s)),
                    cancel_check=cancel_check,
                )
                return completed, [completed.returncode], completed.stderr
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

    def _cleanup_failed(e: CommandCleanupError) -> ShellError:
        partial_stdout, stdout_truncated = _truncate(e.stdout)
        partial_stderr, stderr_truncated = _truncate(e.stderr)
        return ShellError(
            str(e),
            result_payload={
                "error": str(e),
                "cancelled": False,
                "cleanup_confirmed": False,
                "cmd": cmd,
                "cwd": str(cwd_path),
                "stdout": partial_stdout,
                "stderr": partial_stderr,
                "truncated": stdout_truncated or stderr_truncated,
            },
        )

    def _timed_out(e: subprocess.TimeoutExpired) -> ShellError:
        def output_text(value: str | bytes | None) -> str:
            return (
                value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            )

        stdout, stdout_truncated = _truncate(output_text(e.stdout))
        stderr, stderr_truncated = _truncate(output_text(e.stderr))
        message = f"Command timed out after {timeout_s:g}s"
        return ShellError(
            message,
            result_payload={
                "error": message,
                "timed_out": True,
                "cmd": cmd,
                "cwd": str(cwd_path),
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_truncated or stderr_truncated,
            },
        )

    try:
        cp, stage_status, stderr_text = _run(cmd)
    except subprocess.TimeoutExpired as e:
        raise _timed_out(e) from e
    except CommandCancelledError as e:
        raise _cancelled(e) from e
    except CommandCleanupError as e:
        raise _cleanup_failed(e) from e
    except Exception as e:  # noqa: BLE001
        raise ShellError(f"Failed to run command: {e}") from e

    effective_cmd = cmd
    stdout, stdout_truncated = _truncate(cp.stdout or "")
    stderr, stderr_truncated = _truncate(stderr_text)
    truncated = stdout_truncated or stderr_truncated or getattr(cp, "inspection_truncated", False)

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
        **({"sandbox_policy": cp.sandbox_policy} if getattr(cp, "sandbox_policy", None) else {}),
        **(
            {"execution_context": cp.execution_context}
            if getattr(cp, "execution_context", None)
            else {}
        ),
        **({"git_observation": cp.git_observation} if getattr(cp, "git_observation", None) else {}),
        **(
            {"execution_backend": cp.inspection_backend}
            if getattr(cp, "inspection_backend", None)
            else {}
        ),
        **(
            {"host_launcher_unavailable": True}
            if getattr(cp, "host_launcher_unavailable", False) is True
            else {}
        ),
        "pipeline_stages": pipeline_stages,
        "pipeline_stage_status": stage_status,
        "verification_execution_scope": execution_scope_fingerprint(runner),
    }
