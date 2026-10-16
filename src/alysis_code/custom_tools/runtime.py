from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import CustomToolSpec

# NOTE: this module must stay free of package-relative imports at module scope.
# The tool worker loads it standalone via importlib.spec_from_file_location
# (see _WORKER_BOOTSTRAP_CODE below), which gives it no parent package, so a
# `from ..x import y` here raises ImportError before any tool can run.
# Anything from the package must be imported lazily, by absolute name, inside
# the function that needs it — the bootstrap puts the package root on sys.path,
# so absolute imports resolve in both the host and the worker.

_MAX_RESULT_BYTES = 24_000
_MAX_RESULT_STRING_CHARS = 2_000
_MAX_RESULT_DEPTH = 10
_MAX_STREAM_PREVIEW_CHARS = 1_200
_STREAM_TAIL_READ_BYTES = _MAX_STREAM_PREVIEW_CHARS * 8
_MINIMUM_PLATFORM_ENV = ("PATH", "HOME", "LANG", "TEMP", "TMP")
_WINDOWS_PLATFORM_ENV = ("SYSTEMROOT",)
_INJECTED_ENV_NAMES = (
    "ALYSIS_WORKSPACE_ROOT",
    "ALYSIS_SESSION_ID",
    "ALYSIS_TOOL_PATH",
    "ALYSIS_TOOL_SCOPE",
    "ALYSIS_TOOL_NAME",
)
_WORKER_BOOTSTRAP_CODE = (
    "import importlib.util, sys; "
    "sys.path.insert(0, {package_root!r}); "
    "_spec = importlib.util.spec_from_file_location("
    "'_alysis_custom_tool_runtime_worker', {runtime_path!r}); "
    "assert _spec is not None and _spec.loader is not None; "
    "_module = importlib.util.module_from_spec(_spec); "
    "sys.modules[_spec.name] = _module; "
    "_spec.loader.exec_module(_module); "
    "sys.modules.pop(_spec.name, None); "
    "raise SystemExit(_module._worker_main())"
)


class CustomToolRuntimeError(RuntimeError):
    pass


class WorkerProcessTreeGuardError(RuntimeError):
    pass


class _CustomToolPolicyViolation(RuntimeError):
    def __init__(self, *, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


class _RuntimeToolSpec:
    def __init__(self, *, name: str, timeout_s: float) -> None:
        self.name = name
        self.timeout_s = timeout_s


def run_custom_tool(
    *,
    spec: CustomToolSpec,
    args: dict[str, Any],
    workspace_root: Path,
    session_id: str,
    artifact_dir: Path | None = None,
    artifact_reference_prefix: str | None = None,
) -> dict[str, Any]:
    from alysis_code.branding import env_get

    validated_args = _validate_input_args(spec.input_schema, args)
    missing_env = [name for name in spec.required_env if not str(env_get(name) or "").strip()]
    if missing_env:
        missing = ", ".join(missing_env)
        return _runtime_failure_payload(
            spec=spec,
            error_text=f"Missing required env vars: {missing}",
            error_type="MissingEnvironment",
            timeout=False,
            elapsed_ms=0,
            stream_info=_empty_stream_info(),
        )

    try:
        source_bytes = spec.source_path.read_bytes()
    except OSError as exc:
        return _runtime_failure_payload(
            spec=spec,
            error_text=f"Unable to read custom tool source at execution time: {exc}",
            error_type="ToolSourceUnavailable",
            timeout=False,
            elapsed_ms=0,
            stream_info=_empty_stream_info(),
        )

    actual_hash = hashlib.sha256(source_bytes).hexdigest()
    if actual_hash != spec.file_hash:
        return _runtime_failure_payload(
            spec=spec,
            error_text=(
                "Custom tool source changed since discovery; refusing to execute. "
                f"expected_sha256={spec.file_hash} actual_sha256={actual_hash}"
            ),
            error_type="StaleToolHash",
            timeout=False,
            elapsed_ms=0,
            stream_info=_empty_stream_info(),
        )

    if spec.source_scope == "project":
        try:
            trusted = _is_project_tool_trusted_at_execution(spec)
        except RuntimeError as exc:
            return _runtime_failure_payload(
                spec=spec,
                error_text=f"Unable to reload project custom tool trust state: {exc}",
                error_type="TrustStateError",
                timeout=False,
                elapsed_ms=0,
                stream_info=_empty_stream_info(),
            )
        if not trusted:
            return _runtime_failure_payload(
                spec=spec,
                error_text="Project custom tool is no longer trusted at execution time.",
                error_type="TrustRevoked",
                timeout=False,
                elapsed_ms=0,
                stream_info=_empty_stream_info(),
            )

    return _run_tool_subprocess(
        spec=spec,
        args=validated_args,
        workspace_root=workspace_root,
        session_id=session_id,
        source_bytes=source_bytes,
        artifact_dir=artifact_dir,
        artifact_reference_prefix=artifact_reference_prefix,
    )


def _run_tool_subprocess(
    *,
    spec: CustomToolSpec,
    args: dict[str, Any],
    workspace_root: Path,
    session_id: str,
    source_bytes: bytes,
    artifact_dir: Path | None,
    artifact_reference_prefix: str | None,
) -> dict[str, Any]:
    resolved_workspace = workspace_root.resolve()
    stdout_path, stderr_path, stdout_relpath, stderr_relpath = _tool_log_artifact_paths(
        workspace_root=resolved_workspace,
        session_id=session_id,
        tool_name=spec.name,
        artifact_dir=artifact_dir,
        artifact_reference_prefix=artifact_reference_prefix,
    )
    with tempfile.TemporaryDirectory(prefix="alysis-custom-tool-") as temp_dir:
        temp_root = Path(temp_dir)
        sealed_source_path = temp_root / _sealed_source_filename(spec.source_path)
        result_path = temp_root / "result.json"
        sealed_source_path.write_bytes(source_bytes)
        tool_env = _build_tool_env(
            spec=spec,
            workspace_root=resolved_workspace,
            session_id=session_id,
        )
        worker_payload = {
            "sealed_source_path": os.fspath(sealed_source_path),
            "original_source_path": os.fspath(spec.source_path),
            "original_tool_dir": os.fspath(spec.source_path.parent.resolve()),
            "relative_tool_path": spec.relative_tool_path,
            "file_hash": spec.file_hash,
            "tool_name": spec.name,
            "tool_scope": spec.source_scope,
            "capabilities": spec.capabilities.to_dict(),
            "timeout_s": spec.timeout_s,
            "workspace_root": os.fspath(resolved_workspace),
            "session_id": session_id,
            "args": args,
            "result_path": os.fspath(result_path),
            "tool_env": tool_env,
        }
        started_at = time.perf_counter()
        proc: subprocess.Popen[bytes] | None = None
        guard: _WorkerProcessTreeGuard | None = None
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                try:
                    proc = _popen_worker_process(
                        stdout_handle=stdout_handle,
                        stderr_handle=stderr_handle,
                        env=_build_worker_bootstrap_env(tool_env),
                        cwd=temp_root,
                    )
                except OSError as exc:
                    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                    return _runtime_failure_payload(
                        spec=spec,
                        error_text=f"Unable to start custom tool worker process: {exc}",
                        error_type="WorkerSpawnError",
                        timeout=False,
                        elapsed_ms=elapsed_ms,
                        stream_info=_stream_info_from_artifacts(
                            workspace_root=resolved_workspace,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            stdout_relpath=stdout_relpath,
                            stderr_relpath=stderr_relpath,
                        ),
                    )
                try:
                    guard = _create_worker_process_tree_guard(proc)
                except WorkerProcessTreeGuardError as exc:
                    _terminate_immediate_worker(proc)
                    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                    return _runtime_failure_payload(
                        spec=spec,
                        error_text=f"Unable to establish custom tool process-tree guard: {exc}",
                        error_type="WorkerProcessTreeGuardError",
                        timeout=False,
                        elapsed_ms=elapsed_ms,
                        stream_info=_stream_info_from_artifacts(
                            workspace_root=resolved_workspace,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            stdout_relpath=stdout_relpath,
                            stderr_relpath=stderr_relpath,
                        ),
                    )
                except BaseException:
                    _terminate_immediate_worker(proc)
                    raise
                try:
                    _communicate_worker_process(
                        proc,
                        json.dumps(worker_payload, ensure_ascii=True).encode("utf-8"),
                        timeout_s=_subprocess_timeout_s(spec.timeout_s),
                    )
                except subprocess.TimeoutExpired:
                    guard.terminate()
                    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                    return _runtime_failure_payload(
                        spec=spec,
                        error_text=f"Custom tool timed out after {spec.timeout_s:g}s",
                        error_type="TimeoutError",
                        timeout=True,
                        elapsed_ms=elapsed_ms,
                        stream_info=_stream_info_from_artifacts(
                            workspace_root=resolved_workspace,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            stdout_relpath=stdout_relpath,
                            stderr_relpath=stderr_relpath,
                        ),
                    )
                except BaseException:
                    guard.terminate()
                    raise
                else:
                    guard.cleanup_after_completion()
        finally:
            if guard is not None:
                guard.close()

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        stream_info = _stream_info_from_artifacts(
            workspace_root=resolved_workspace,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_relpath=stdout_relpath,
            stderr_relpath=stderr_relpath,
        )
        if proc is None:
            return _runtime_failure_payload(
                spec=spec,
                error_text="Custom tool worker process was not started",
                error_type="WorkerSpawnError",
                timeout=False,
                elapsed_ms=elapsed_ms,
                stream_info=stream_info,
            )
        if not result_path.exists():
            detail = f"exit_code={proc.returncode}"
            return _runtime_failure_payload(
                spec=spec,
                error_text=f"Custom tool worker did not write a result payload ({detail})",
                error_type="WorkerProtocolError",
                timeout=False,
                elapsed_ms=elapsed_ms,
                stream_info=stream_info,
            )
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _runtime_failure_payload(
                spec=spec,
                error_text=f"Custom tool worker returned invalid result payload: {exc}",
                error_type="WorkerProtocolError",
                timeout=False,
                elapsed_ms=elapsed_ms,
                stream_info=stream_info,
            )
        if not isinstance(payload, dict):
            return _runtime_failure_payload(
                spec=spec,
                error_text="Custom tool worker returned invalid result payload",
                error_type="WorkerProtocolError",
                timeout=False,
                elapsed_ms=elapsed_ms,
                stream_info=stream_info,
            )
        payload["elapsed_ms"] = elapsed_ms
        payload.update(stream_info)
        return payload


def _runtime_success_payload(
    *,
    spec: CustomToolSpec,
    result: Any,
    elapsed_ms: int,
    stream_info: dict[str, Any],
) -> dict[str, Any]:
    sanitized = _sanitize_json_value(result)
    summary = f"Custom tool '{spec.name}' completed successfully."
    if stream_info.get("stdout_artifact_path"):
        summary += " stdout captured."
    if stream_info.get("stderr_artifact_path"):
        summary += " stderr captured."
    preview = _preview_result(sanitized)
    return {
        "success": True,
        "timeout": False,
        "elapsed_ms": elapsed_ms,
        "result": sanitized,
        **stream_info,
        "summary": summary,
        "preview": preview,
    }


def _runtime_failure_payload(
    *,
    spec: CustomToolSpec,
    error_text: str,
    error_type: str,
    timeout: bool,
    elapsed_ms: int,
    stream_info: dict[str, Any],
) -> dict[str, Any]:
    summary = f"Custom tool '{spec.name}' failed."
    if timeout:
        summary = f"Custom tool '{spec.name}' timed out."
    if stream_info.get("stderr_artifact_path"):
        summary += " stderr captured."
    return {
        "success": False,
        "timeout": timeout,
        "elapsed_ms": elapsed_ms,
        "error": error_text,
        "error_info": {
            "type": error_type,
            "message": error_text,
        },
        **stream_info,
        "summary": summary,
        "preview": stream_info.get("stderr_preview") or stream_info.get("stdout_preview") or "",
    }


def _empty_stream_info() -> dict[str, Any]:
    return {
        "stdout_preview": "",
        "stdout_truncated": False,
        "stderr_preview": "",
        "stderr_truncated": False,
    }


def _tool_log_artifact_paths(
    *,
    workspace_root: Path,
    session_id: str,
    tool_name: str,
    artifact_dir: Path | None = None,
    artifact_reference_prefix: str | None = None,
) -> tuple[Path, Path, str, str]:
    session_key = _sanitize_path_component(session_id) or "session"
    tool_key = _sanitize_path_component(tool_name) or "tool"
    rel_dir = Path(".alysis") / "runs" / session_key / "tool_logs"
    logs_dir = artifact_dir if artifact_dir is not None else workspace_root / rel_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{tool_key}-{uuid.uuid4().hex}"
    stdout_name = f"{stem}.stdout.log"
    stderr_name = f"{stem}.stderr.log"
    if artifact_reference_prefix is not None:
        reference_prefix = _normalize_artifact_reference_prefix(artifact_reference_prefix)
        stdout_ref = f"{reference_prefix}/{stdout_name}" if reference_prefix else stdout_name
        stderr_ref = f"{reference_prefix}/{stderr_name}" if reference_prefix else stderr_name
    elif artifact_dir is not None:
        stdout_ref = (logs_dir / stdout_name).as_posix()
        stderr_ref = (logs_dir / stderr_name).as_posix()
    else:
        stdout_ref = (rel_dir / stdout_name).as_posix()
        stderr_ref = (rel_dir / stderr_name).as_posix()
    return logs_dir / stdout_name, logs_dir / stderr_name, stdout_ref, stderr_ref


def _normalize_artifact_reference_prefix(value: str) -> str:
    parts: list[str] = []
    for raw_part in str(value).strip().replace("\\", "/").split("/"):
        part = raw_part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise CustomToolRuntimeError("artifact_reference_prefix cannot traverse upward")
        parts.append(part)
    return "/".join(parts)


def _stream_info_from_artifacts(
    *,
    workspace_root: Path,
    stdout_path: Path,
    stderr_path: Path,
    stdout_relpath: str,
    stderr_relpath: str,
) -> dict[str, Any]:
    stdout_preview, stdout_truncated, stdout_has_content = _preview_artifact_tail(stdout_path)
    stderr_preview, stderr_truncated, stderr_has_content = _preview_artifact_tail(stderr_path)
    payload = {
        "stdout_preview": stdout_preview,
        "stdout_truncated": stdout_truncated,
        "stderr_preview": stderr_preview,
        "stderr_truncated": stderr_truncated,
    }
    if stdout_has_content:
        payload["stdout_artifact_path"] = _normalize_artifact_relpath(
            workspace_root=workspace_root,
            artifact_path=stdout_path,
            fallback_relpath=stdout_relpath,
        )
    if stderr_has_content:
        payload["stderr_artifact_path"] = _normalize_artifact_relpath(
            workspace_root=workspace_root,
            artifact_path=stderr_path,
            fallback_relpath=stderr_relpath,
        )
    return payload


def _preview_artifact_tail(path: Path) -> tuple[str, bool, bool]:
    try:
        size = path.stat().st_size
    except OSError:
        return "", False, False
    if size <= 0:
        return "", False, False
    read_size = min(size, _STREAM_TAIL_READ_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - read_size))
            raw = handle.read(read_size)
    except OSError:
        return "", False, False
    text = raw.decode("utf-8", errors="replace")
    truncated = size > read_size
    if len(text) > _MAX_STREAM_PREVIEW_CHARS:
        text = text[-_MAX_STREAM_PREVIEW_CHARS:]
        truncated = True
    return text, truncated, True


def _normalize_artifact_relpath(
    *,
    workspace_root: Path,
    artifact_path: Path,
    fallback_relpath: str,
) -> str:
    try:
        return artifact_path.relative_to(workspace_root).as_posix()
    except ValueError:
        return fallback_relpath


def _sanitize_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    sanitized = sanitized.strip(".-")
    return sanitized[:80]


def _sealed_source_filename(source_path: Path) -> str:
    suffix = source_path.suffix if source_path.suffix == ".py" else ".py"
    stem = _sanitize_path_component(source_path.stem) or "tool"
    return f"{stem}-{uuid.uuid4().hex}{suffix}"


def _build_tool_env(
    *,
    spec: CustomToolSpec,
    workspace_root: Path,
    session_id: str,
) -> dict[str, str]:
    from alysis_code.branding import env_get

    env: dict[str, str] = {}
    for key in _MINIMUM_PLATFORM_ENV:
        value = os.environ.get(key)
        if str(value or "").strip():
            env[key] = str(value)
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        for key in _WINDOWS_PLATFORM_ENV:
            value = os.environ.get(key)
            if str(value or "").strip():
                env[key] = str(value)
    for key in _dedupe_env_names([*spec.required_env, *spec.capabilities.secret_refs]):
        value = env_get(key)
        if str(value or "").strip():
            env[key] = str(value)
    env.update(
        {
            "ALYSIS_WORKSPACE_ROOT": os.fspath(workspace_root.resolve()),
            "ALYSIS_SESSION_ID": session_id,
            "ALYSIS_TOOL_PATH": os.fspath(spec.source_path),
            "ALYSIS_TOOL_SCOPE": spec.source_scope,
            "ALYSIS_TOOL_NAME": spec.name,
        }
    )
    return env


def _build_worker_bootstrap_env(tool_env: dict[str, str]) -> dict[str, str]:
    return dict(tool_env)


def _worker_command() -> list[str]:
    package_root = os.fspath(Path(__file__).resolve().parents[2])
    runtime_path = os.fspath(Path(__file__).resolve())
    bootstrap_code = _WORKER_BOOTSTRAP_CODE.format(
        package_root=package_root,
        runtime_path=runtime_path,
    )
    return [sys.executable, "-I", "-c", bootstrap_code]


def _is_project_tool_trusted_at_execution(spec: CustomToolSpec) -> bool:
    from .trust import is_project_tool_trusted

    return is_project_tool_trusted(spec)


def _popen_worker_process(
    *,
    stdout_handle: Any,
    stderr_handle: Any,
    env: dict[str, str],
    cwd: Path,
) -> subprocess.Popen[bytes]:
    kwargs: dict[str, Any] = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        _worker_command(),
        stdin=subprocess.PIPE,
        stdout=stdout_handle,
        stderr=stderr_handle,
        env=env,
        cwd=os.fspath(cwd),
        **kwargs,
    )


def _communicate_worker_process(
    proc: subprocess.Popen[bytes],
    payload: bytes,
    *,
    timeout_s: float,
) -> None:
    proc.communicate(input=payload, timeout=timeout_s)


def _create_worker_process_tree_guard(
    proc: subprocess.Popen[bytes],
) -> _WorkerProcessTreeGuard:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return _WindowsJobObjectGuard.create(proc)
    return _PosixProcessGroupGuard(proc)


class _WorkerProcessTreeGuard:
    def terminate(self) -> None:
        raise NotImplementedError

    def cleanup_after_completion(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _PosixProcessGroupGuard(_WorkerProcessTreeGuard):
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def terminate(self) -> None:
        _terminate_posix_process_group(self._proc)

    def cleanup_after_completion(self) -> None:
        _terminate_posix_process_group(self._proc)

    def close(self) -> None:
        return


class _WindowsJobObjectGuard(_WorkerProcessTreeGuard):
    def __init__(self, proc: subprocess.Popen[bytes], kernel32: Any, job_handle: Any) -> None:
        self._proc = proc
        self._kernel32 = kernel32
        self._job_handle = job_handle

    @classmethod
    def create(cls, proc: subprocess.Popen[bytes]) -> _WindowsJobObjectGuard:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            _raise_windows_guard_error("CreateJobObjectW")
        try:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                job_handle,
                9,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                _raise_windows_guard_error("SetInformationJobObject")
            process_handle = getattr(proc, "_handle", None)
            if process_handle is None:
                raise WorkerProcessTreeGuardError("worker process handle is unavailable")
            if not kernel32.AssignProcessToJobObject(
                job_handle,
                wintypes.HANDLE(int(process_handle)),
            ):
                _raise_windows_guard_error("AssignProcessToJobObject")
        except BaseException:
            kernel32.CloseHandle(job_handle)
            raise
        return cls(proc, kernel32, job_handle)

    def terminate(self) -> None:
        if self._job_handle:
            self._kernel32.TerminateJobObject(self._job_handle, 1)
        _wait_for_worker_exit(self._proc, timeout_s=1.0)

    def cleanup_after_completion(self) -> None:
        self.close()
        _wait_for_worker_exit(self._proc, timeout_s=1.0)

    def close(self) -> None:
        if self._job_handle:
            self._kernel32.CloseHandle(self._job_handle)
            self._job_handle = None


def _raise_windows_guard_error(action: str) -> None:
    import ctypes

    error_code = ctypes.get_last_error()
    raise WorkerProcessTreeGuardError(f"{action} failed: {ctypes.WinError(error_code)}")


def _terminate_immediate_worker(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(Exception):
        if proc.stdin is not None:
            proc.stdin.close()
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        if not _wait_for_worker_exit(proc, timeout_s=0.5):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            _wait_for_worker_exit(proc, timeout_s=1.0)


def _terminate_posix_process_group(proc: subprocess.Popen[bytes]) -> None:
    pgid = proc.pid
    if pgid <= 0:
        _terminate_immediate_worker(proc)
        return
    if not _signal_posix_process_group(pgid, signal.SIGTERM):
        _terminate_immediate_worker(proc)
        return
    _wait_for_worker_exit(proc, timeout_s=0.5)
    if not _wait_for_posix_process_group_exit(pgid, timeout_s=0.5):
        if not _signal_posix_process_group(pgid, signal.SIGKILL):
            _terminate_immediate_worker(proc)
            return
        _wait_for_posix_process_group_exit(pgid, timeout_s=1.0)
    if not _wait_for_worker_exit(proc, timeout_s=1.0):
        _terminate_immediate_worker(proc)


def _signal_posix_process_group(pgid: int, signum: int) -> bool:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _wait_for_posix_process_group_exit(pgid: int, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _posix_process_group_exists(pgid):
            return True
        time.sleep(0.02)
    return not _posix_process_group_exists(pgid)


def _posix_process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_worker_exit(proc: subprocess.Popen[bytes], *, timeout_s: float) -> bool:
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False
    return True


class _CustomToolAuditPolicy:
    _BLOCKED_IMPORT_ROOTS = (
        "alysis_code",
        "ctypes",
        "_ctypes",
        "cffi",
        "_cffi_backend",
    )
    _PROCESS_EVENTS = {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
    }

    def __init__(
        self,
        *,
        capabilities: dict[str, Any],
        workspace_root: Path,
        original_tool_dir: Path,
        result_path: Path,
    ) -> None:
        self._capabilities = dict(capabilities)
        self._workspace_root = workspace_root.resolve()
        self._original_tool_dir = original_tool_dir.resolve()
        self._result_path = result_path.resolve()
        self._allowed_resolved_hosts: set[str] = set()
        self._allowed_workspace_writes: set[tuple[str, str]] = set()
        self._resolving_allowed_host = False
        self.first_violation: _CustomToolPolicyViolation | None = None

    def handle_event(self, event: str, args: tuple[Any, ...]) -> None:
        try:
            self._check_event(event, args)
        except _CustomToolPolicyViolation as exc:
            if self.first_violation is None:
                self.first_violation = exc
            raise

    def _check_event(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "import" and args:
            self._check_import(str(args[0]))
            return
        if event == "ctypes.dlopen":
            self._deny_host_import("ctypes.dlopen")
        if event == "open":
            self._check_open(args)
            return
        if event in self._PROCESS_EVENTS:
            self._check_process_spawn(event)
            return
        if event == "socket.connect":
            self._check_socket_connect(args)
            return
        if event in {
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.getnameinfo",
        }:
            self._check_socket_name_lookup(event, args)
            return
        self._check_filesystem_mutation(event, args)

    def _check_import(self, module_name: str) -> None:
        normalized = module_name.strip()
        for root in self._BLOCKED_IMPORT_ROOTS:
            if normalized == root or normalized.startswith(f"{root}."):
                self._deny_host_import(normalized)

    def _deny_host_import(self, module_name: str) -> None:
        raise _CustomToolPolicyViolation(
            error_type="HostImportBlocked",
            message=f"Blocked custom tool import or native bypass attempt: {module_name}",
        )

    def _check_open(self, args: tuple[Any, ...]) -> None:
        if len(args) < 3:
            return
        path, mode, flags = args[:3]
        if not _open_event_is_write_like(mode=mode, flags=flags):
            return
        target = self._policy_path(path)
        if target is None:
            if self._write_scope() == "unrestricted":
                return
            self._deny_capability("filesystem write via open", "filesystem.write")
        self._check_write_target(target, operation="filesystem write via open")

    def _check_filesystem_mutation(self, event: str, args: tuple[Any, ...]) -> None:
        targets = _filesystem_mutation_targets(event, args)
        if not targets:
            return
        for target_raw in targets:
            target = self._policy_path(target_raw)
            if target is None:
                if self._write_scope() == "unrestricted":
                    continue
                self._deny_capability(event, "filesystem.write")
            self._check_write_target(target, operation=event)

    def _check_write_target(self, target: Path, *, operation: str) -> None:
        if target == self._result_path:
            return
        write_scope = self._write_scope()
        if write_scope == "unrestricted":
            self._record_allowed_workspace_write(target, scope=write_scope)
            return
        if write_scope in {"none", "unspecified"}:
            self._deny_capability(operation, "filesystem.write")
        allowed_root: Path | None = None
        if write_scope == "workspace":
            allowed_root = self._workspace_root
        elif write_scope == "tool_dir":
            allowed_root = self._original_tool_dir
        if allowed_root is None or not _path_is_within(target, allowed_root):
            self._deny_capability(
                f"{operation} outside {write_scope} scope: {target}",
                "filesystem.write",
            )
        self._record_allowed_workspace_write(target, scope=write_scope)

    def _write_scope(self) -> str:
        return str(self._capabilities.get("filesystem_write_scope") or "unspecified")

    def _record_allowed_workspace_write(self, target: Path, *, scope: str) -> None:
        try:
            rel_path = target.relative_to(self._workspace_root).as_posix()
        except ValueError:
            return
        if rel_path:
            self._allowed_workspace_writes.add((rel_path, scope))

    def workspace_write_side_effects(self) -> list[dict[str, str]]:
        return [
            {"path": path, "scope": scope} for path, scope in sorted(self._allowed_workspace_writes)
        ]

    def _check_socket_connect(self, args: tuple[Any, ...]) -> None:
        address = args[1] if len(args) > 1 else None
        host = _socket_connect_host(address)
        network_access = str(self._capabilities.get("network_access") or "unspecified")
        if network_access == "unrestricted":
            return
        if network_access in {"none", "unspecified"}:
            self._deny_capability(
                f"network socket connect to {host or address!r}",
                "network_access",
            )
        if network_access == "local":
            if host is not None and _host_is_loopback(host):
                return
            self._deny_capability(
                f"network socket connect to non-loopback host {host or address!r}",
                "network_access",
            )
        if network_access == "restricted":
            allowed_hosts = {
                str(hostname) for hostname in self._capabilities.get("network_hosts") or ()
            }
            if host is not None and (host in allowed_hosts or host in self._allowed_resolved_hosts):
                return
            self._deny_capability(
                f"network socket connect to undeclared host {host or address!r}",
                "network_hosts",
            )

    def _check_socket_name_lookup(self, event: str, args: tuple[Any, ...]) -> None:
        if self._resolving_allowed_host:
            return
        if event == "socket.getnameinfo":
            host = _socket_connect_host(args[0] if args else None)
        else:
            host = _socket_connect_host((args[0], None)) if args else None
        network_access = str(self._capabilities.get("network_access") or "unspecified")
        if network_access == "unrestricted":
            return
        if network_access in {"none", "unspecified"}:
            self._deny_capability(f"network name lookup for {host!r}", "network_access")
        if network_access == "local":
            if host is not None and _host_is_loopback(host):
                return
            self._deny_capability(
                f"network name lookup for non-loopback host {host!r}",
                "network_access",
            )
        if network_access == "restricted":
            allowed_hosts = {
                str(hostname) for hostname in self._capabilities.get("network_hosts") or ()
            }
            if host is not None and host in allowed_hosts:
                self._record_allowed_resolved_hosts(host)
                return
            self._deny_capability(
                f"network name lookup for undeclared host {host!r}",
                "network_hosts",
            )

    def _record_allowed_resolved_hosts(self, host: str) -> None:
        self._allowed_resolved_hosts.add(host)
        self._resolving_allowed_host = True
        try:
            for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
                if family in {socket.AF_INET, socket.AF_INET6}:
                    resolved_host = _socket_connect_host(sockaddr)
                    if resolved_host:
                        self._allowed_resolved_hosts.add(resolved_host)
        except OSError:
            return
        finally:
            self._resolving_allowed_host = False

    def _check_process_spawn(self, event: str) -> None:
        process_spawn = str(self._capabilities.get("process_spawn") or "unspecified")
        if process_spawn == "unrestricted":
            return
        self._deny_capability(event, "process_spawn")

    def check_process_spawn_call(self, operation: str) -> None:
        self._check_process_spawn(operation)

    def _policy_path(self, value: Any) -> Path | None:
        if isinstance(value, int):
            return None
        try:
            path = Path(os.fsdecode(value))
        except (TypeError, ValueError):
            return None
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            return path.resolve(strict=False)
        except OSError:
            return path.absolute()

    def _deny_capability(self, operation: str, capability: str) -> None:
        raise _CustomToolPolicyViolation(
            error_type="CapabilityViolation",
            message=f"Denied custom tool operation {operation!r}; capability {capability} is not allowed",
        )


def _open_event_is_write_like(*, mode: Any, flags: Any) -> bool:
    mode_text = str(mode or "")
    if any(marker in mode_text for marker in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        write_flags = (
            getattr(os, "O_WRONLY", 0)
            | getattr(os, "O_RDWR", 0)
            | getattr(os, "O_CREAT", 0)
            | getattr(os, "O_TRUNC", 0)
            | getattr(os, "O_APPEND", 0)
        )
        return bool(flags & write_flags)
    return False


def _filesystem_mutation_targets(event: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
    if event in {"os.remove", "os.unlink", "os.mkdir", "os.rmdir"} and args:
        if len(args) >= 2 and _is_active_dir_fd(args[1]):
            return (None,)
        return (args[0],)
    if event in {"os.rename", "os.replace"} and len(args) >= 2:
        return (args[0], args[1])
    if event in {"os.symlink", "os.link"} and len(args) >= 2:
        return (args[1],)
    if event in {"os.truncate", "os.chmod", "os.chown", "os.utime"} and args:
        return (args[0],)
    if event == "shutil.copyfile" and len(args) >= 2:
        return (args[1],)
    if event in {"shutil.copymode", "shutil.copystat", "shutil.copytree"} and len(args) >= 2:
        return (args[1],)
    if event == "shutil.rmtree" and args:
        return (args[0],)
    if event == "shutil.move" and len(args) >= 2:
        return (args[0], args[1])
    return ()


def _is_active_dir_fd(value: Any) -> bool:
    return isinstance(value, int) and value >= 0


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _socket_connect_host(address: Any) -> str | None:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:
        host = address
    if isinstance(host, bytes):
        with contextlib.suppress(UnicodeDecodeError):
            return host.decode("utf-8")
        return None
    if isinstance(host, str):
        return host.strip()
    return None


def _host_is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _policy_failure_payload(
    *,
    spec: _RuntimeToolSpec,
    violation: _CustomToolPolicyViolation,
    elapsed_ms: int,
) -> dict[str, Any]:
    return _runtime_failure_payload(
        spec=spec,
        error_text=violation.message,
        error_type=violation.error_type,
        timeout=False,
        elapsed_ms=elapsed_ms,
        stream_info=_empty_stream_info(),
    )


@contextlib.contextmanager
def _patched_spawn_functions(policy: _CustomToolAuditPolicy):
    spawn_names = [name for name in dir(os) if name.startswith("spawn")]
    originals = {name: getattr(os, name) for name in spawn_names}

    def _blocked_spawn(name: str):
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            policy.check_process_spawn_call(f"os.{name}")
            return originals[name](*args, **kwargs)

        return wrapper

    try:
        for name in spawn_names:
            setattr(os, name, _blocked_spawn(name))
        yield
    finally:
        for name, original in originals.items():
            setattr(os, name, original)


def _dedupe_env_names(names: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        key = str(name or "").strip()
        if not key or key in seen or key in _INJECTED_ENV_NAMES:
            continue
        seen.add(key)
        result.append(key)
    return tuple(result)


def _subprocess_timeout_s(timeout_s: float) -> float:
    return max(0.25, float(timeout_s) + 0.25)


@contextlib.contextmanager
def _worker_tool_execution_context(
    *,
    tool_env: dict[str, str],
    workspace_root: Path,
):
    old_cwd = Path.cwd()
    old_env = dict(os.environ)
    old_sys_path = list(sys.path)
    old_dont_write_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        resolved_workspace = workspace_root.resolve()
        os.chdir(resolved_workspace)
        # Isolated startup keeps the workspace off sys.path until the trusted payload is read.
        # Restore the old import behavior only inside the scrubbed tool execution context.
        sys.path.insert(0, os.fspath(resolved_workspace))
        os.environ.clear()
        os.environ.update(tool_env)
        yield
    finally:
        os.chdir(old_cwd)
        sys.path[:] = old_sys_path
        sys.dont_write_bytecode = old_dont_write_bytecode
        os.environ.clear()
        os.environ.update(old_env)


def _load_tool_module(source_path: Path) -> Any:
    module_name = f"_alysis_custom_tool_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise CustomToolRuntimeError(f"Unable to load custom tool: {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _format_tool_exception(exc: BaseException) -> str:
    if isinstance(exc, SystemExit):
        code = exc.code
        return f"SystemExit: {code!r}"
    return f"{type(exc).__name__}: {exc}"


def _preview_result(value: Any) -> str:
    try:
        preview = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        preview = repr(value)
    if len(preview) > 200:
        return preview[:200] + "..."
    return preview


def _sanitize_json_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"omitted": True, "reason": "non_finite_number"}
    if isinstance(value, str):
        if len(value) <= _MAX_RESULT_STRING_CHARS:
            return value
        return value[:_MAX_RESULT_STRING_CHARS] + "..."
    if depth >= _MAX_RESULT_DEPTH:
        return {"omitted": True, "reason": "max_depth_exceeded"}
    if isinstance(value, list):
        sanitized = [_sanitize_json_value(item, depth=depth + 1) for item in value]
        return _cap_serialized_payload(sanitized)
    if isinstance(value, dict):
        sanitized = {
            str(key): _sanitize_json_value(item, depth=depth + 1) for key, item in value.items()
        }
        return _cap_serialized_payload(sanitized)
    return {"omitted": True, "reason": "unsupported_type", "type": type(value).__name__}


def _cap_serialized_payload(value: Any) -> Any:
    try:
        payload_size = len(
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    except TypeError:
        return {"omitted": True, "reason": "non_serializable"}
    if payload_size <= _MAX_RESULT_BYTES:
        return value
    return {"omitted": True, "reason": "result_too_large", "size_bytes": payload_size}


def _validate_input_args(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise CustomToolRuntimeError("Custom tool arguments must be an object")
    _validate_schema_value(schema=schema, value=args, path="$")
    return dict(args)


def _validate_schema_value(*, schema: dict[str, Any], value: Any, path: str) -> None:
    if not isinstance(schema, dict):
        return
    if "const" in schema and value != schema["const"]:
        raise CustomToolRuntimeError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema:
        enum_values = schema["enum"]
        if isinstance(enum_values, list) and value not in enum_values:
            raise CustomToolRuntimeError(f"{path} must be one of the declared enum values")
    expected_type = schema.get("type")
    if expected_type is not None:
        _validate_type(expected_type=expected_type, value=value, path=path)
    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise CustomToolRuntimeError(f"{path} must be at least {min_length} chars")
        if isinstance(max_length, int) and len(value) > max_length:
            raise CustomToolRuntimeError(f"{path} must be at most {max_length} chars")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise CustomToolRuntimeError(f"{path} must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise CustomToolRuntimeError(f"{path} must be <= {maximum}")
    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise CustomToolRuntimeError(f"{path} must contain at least {min_items} items")
        if isinstance(max_items, int) and len(value) > max_items:
            raise CustomToolRuntimeError(f"{path} must contain at most {max_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(schema=item_schema, value=item, path=f"{path}[{index}]")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    raise CustomToolRuntimeError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        if isinstance(properties, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}"
                child_schema = properties.get(key)
                if isinstance(child_schema, dict):
                    _validate_schema_value(schema=child_schema, value=item, path=child_path)
                    continue
                if additional is False:
                    raise CustomToolRuntimeError(f"{child_path} is not allowed")
                if isinstance(additional, dict):
                    _validate_schema_value(schema=additional, value=item, path=child_path)


def _validate_type(*, expected_type: Any, value: Any, path: str) -> None:
    options = expected_type if isinstance(expected_type, list) else [expected_type]
    if any(_value_matches_type(option, value) for option in options):
        return
    rendered = ", ".join(str(option) for option in options)
    raise CustomToolRuntimeError(f"{path} must match type {rendered}")


def _value_matches_type(expected_type: Any, value: Any) -> bool:
    expected = str(expected_type)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _worker_main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        print("Missing worker payload", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid worker payload: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("Invalid worker payload shape", file=sys.stderr)
        return 1

    result_path = Path(str(payload.get("result_path") or ""))
    sealed_source_path = Path(str(payload.get("sealed_source_path") or ""))
    original_tool_dir = Path(str(payload.get("original_tool_dir") or "."))
    workspace_root = Path(str(payload.get("workspace_root") or "."))
    capabilities_raw = payload.get("capabilities")
    capabilities = dict(capabilities_raw) if isinstance(capabilities_raw, dict) else {}
    tool_env_raw = payload.get("tool_env")
    tool_env = (
        {
            str(key): str(value)
            for key, value in tool_env_raw.items()
            if isinstance(tool_env_raw, dict) and isinstance(key, str)
        }
        if isinstance(tool_env_raw, dict)
        else {}
    )
    args_obj = payload.get("args")
    if not isinstance(args_obj, dict):
        args_obj = {}

    spec = _RuntimeToolSpec(
        name=str(payload.get("tool_name") or ""),
        timeout_s=float(payload.get("timeout_s") or 15.0),
    )

    policy = _CustomToolAuditPolicy(
        capabilities=capabilities,
        workspace_root=workspace_root,
        original_tool_dir=original_tool_dir,
        result_path=result_path,
    )
    sys.addaudithook(policy.handle_event)

    started_at = time.perf_counter()
    try:
        with (
            _patched_spawn_functions(policy),
            _worker_tool_execution_context(tool_env=tool_env, workspace_root=workspace_root),
        ):
            module = _load_tool_module(sealed_source_path)
            run_callable = getattr(module, "run", None)
            if not callable(run_callable):
                raise CustomToolRuntimeError("Custom tool does not define callable run(args)")
            result = run_callable(dict(args_obj))
    except BaseException as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        if isinstance(exc, _CustomToolPolicyViolation):
            return _write_worker_result(
                result_path,
                _policy_failure_payload(spec=spec, violation=exc, elapsed_ms=elapsed_ms),
                success=False,
            )
        if policy.first_violation is not None:
            return _write_worker_result(
                result_path,
                _policy_failure_payload(
                    spec=spec,
                    violation=policy.first_violation,
                    elapsed_ms=elapsed_ms,
                ),
                success=False,
            )
        payload_out = _runtime_failure_payload(
            spec=spec,
            error_text=_format_tool_exception(exc),
            error_type=type(exc).__name__,
            timeout=False,
            elapsed_ms=elapsed_ms,
            stream_info=_empty_stream_info(),
        )
        return _write_worker_result(result_path, payload_out, success=False)

    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    if policy.first_violation is not None:
        return _write_worker_result(
            result_path,
            _policy_failure_payload(
                spec=spec,
                violation=policy.first_violation,
                elapsed_ms=elapsed_ms,
            ),
            success=False,
        )
    payload_out = _runtime_success_payload(
        spec=spec,
        result=result,
        elapsed_ms=elapsed_ms,
        stream_info=_empty_stream_info(),
    )
    side_effects = policy.workspace_write_side_effects()
    if side_effects:
        payload_out["side_effects"] = {"workspace_writes": side_effects}
    return _write_worker_result(result_path, payload_out, success=True)


def _write_worker_result(result_path: Path, payload: dict[str, Any], *, success: bool) -> int:
    try:
        result_path.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Failed to write worker result payload: {exc}", file=sys.stderr)
        return 1
    return 0 if success else 0


if __name__ == "__main__":
    if "--worker" in sys.argv[1:]:
        raise SystemExit(_worker_main())
    raise SystemExit("custom tool runtime is not a public CLI entrypoint")
