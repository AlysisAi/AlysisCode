"""Durable ownership records for background terminal processes.

The in-memory terminal manager can terminate everything during an orderly shutdown. These records
cover the harder case where the bridge is killed before teardown runs. A later Alysis Code process
only signals an exact PID/process-group whose creation token still matches a record written at
spawn time; it never enumerates or matches processes by name or command line.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import signal
import stat
import subprocess
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .branding import canonical_user_data_dir, env_get
from .command_text import command_text

_SCHEMA_VERSION = 1
_MAX_RECORD_BYTES = 8192
_MAX_RECORDS = 256
_TERMINATION_CONFIRM_TIMEOUT_S = 1.0
_TERMINATION_CONFIRM_INTERVAL_S = 0.02
_DOCKER_CONTAINER_PATTERN = re.compile(r"^alysis-bgsbx-[0-9a-f]{12}$")
_ProcessState = Literal["matching", "dead", "reused", "unknown"]


@dataclass(frozen=True)
class TerminalOwnershipHandle:
    record_path: Path


@dataclass(frozen=True)
class TerminalScavengeResult:
    record: str
    action: Literal["preserved", "terminated", "absent", "refused", "invalid"]


class TerminalOwnershipLedger:
    def __init__(self, state_dir: Path | None = None) -> None:
        configured = str(env_get("ALYSIS_TERMINAL_OWNERSHIP_DIR") or "").strip()
        configured_data = str(env_get("ALYSIS_DATA_DIR") or "").strip()
        data_root = (
            Path(configured_data).expanduser() if configured_data else canonical_user_data_dir()
        )
        default = Path(configured).expanduser() if configured else data_root / "terminal-processes"
        self.state_dir = (state_dir or default).expanduser().resolve()

    def scavenge_stale(self) -> tuple[TerminalScavengeResult, ...]:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return ()
        results: list[TerminalScavengeResult] = []
        try:
            candidates = sorted(self.state_dir.glob("*.json"))[:_MAX_RECORDS]
        except OSError:
            return ()
        for path in candidates:
            payload = _read_record(path)
            if payload is None:
                _unlink_record(path)
                results.append(TerminalScavengeResult(path.name, "invalid"))
                continue
            owner_state = _process_state(payload["owner_pid"], payload["owner_start_token"])
            if owner_state in {"matching", "unknown"}:
                results.append(TerminalScavengeResult(path.name, "preserved"))
                continue
            child_state = _process_state(payload["child_pid"], payload["child_start_token"])
            if child_state == "matching":
                terminated = _terminate_owned_process(payload)
                action: TerminalScavengeResult = TerminalScavengeResult(
                    path.name, "terminated" if terminated else "refused"
                )
                consume_record = terminated
            elif child_state == "dead":
                _cleanup_owned_resource(payload)
                action = TerminalScavengeResult(path.name, "absent")
                consume_record = True
            elif child_state == "reused":
                # The recorded process no longer exists. Never signal its recycled PID, but the
                # stale ownership record can safely be consumed.
                action = TerminalScavengeResult(path.name, "refused")
                consume_record = True
            else:
                # An unverifiable identity may still be the owned process. Keep its record so a
                # later scavenger can retry after a transient inspection or access failure.
                action = TerminalScavengeResult(path.name, "refused")
                consume_record = False
            if consume_record:
                _unlink_record(path)
            results.append(action)
        return tuple(results)

    def track(
        self,
        *,
        child_pid: int,
        termination_mode: str,
        posix_pgid: int | None,
        cleanup_kind: str | None = None,
        cleanup_id: str | None = None,
    ) -> TerminalOwnershipHandle | None:
        owner_pid = os.getpid()
        owner_token = _pid_start_token(owner_pid)
        child_token = _pid_start_token(child_pid)
        if owner_pid <= 1 or child_pid <= 1 or owner_token is None or child_token is None:
            return None
        if termination_mode not in {"process_group", "direct"}:
            return None
        pgid: int | None = None
        if termination_mode == "process_group" and os.name != "nt":
            if not isinstance(posix_pgid, int) or posix_pgid <= 1:
                return None
            pgid = posix_pgid
        if cleanup_kind is not None:
            if cleanup_kind != "docker-container" or not _valid_cleanup_id(cleanup_id):
                return None
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "record_id": uuid.uuid4().hex,
            "owner_pid": owner_pid,
            "owner_start_token": owner_token,
            "child_pid": child_pid,
            "child_start_token": child_token,
            "termination_mode": termination_mode,
            "posix_pgid": pgid,
            "cleanup_kind": cleanup_kind,
            "cleanup_id": cleanup_id,
            "created_at": time.time(),
        }
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.state_dir / f"{payload['record_id']}.json"
            temporary = self.state_dir / f".{payload['record_id']}.{uuid.uuid4().hex}.tmp"
            raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                _fsync_directory(self.state_dir)
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()
            return TerminalOwnershipHandle(path)
        except OSError:
            return None

    def release(self, handle: TerminalOwnershipHandle | None) -> None:
        if handle is None:
            return
        try:
            resolved = handle.record_path.resolve()
            if resolved.parent != self.state_dir or resolved.suffix != ".json":
                return
        except OSError:
            return
        _unlink_record(handle.record_path)


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _MAX_RECORD_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "record_id",
        "owner_pid",
        "owner_start_token",
        "child_pid",
        "child_start_token",
        "termination_mode",
        "posix_pgid",
        "cleanup_kind",
        "cleanup_id",
        "created_at",
    }:
        return None
    if payload.get("schema_version") != _SCHEMA_VERSION:
        return None
    if (
        not isinstance(payload.get("record_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", payload["record_id"]) is None
    ):
        return None
    if path.name != f"{payload['record_id']}.json":
        return None
    for key in ("owner_pid", "child_pid"):
        if (
            not isinstance(payload.get(key), int)
            or isinstance(payload[key], bool)
            or payload[key] <= 1
        ):
            return None
    for key in ("owner_start_token", "child_start_token"):
        if not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > 256:
            return None
    if payload.get("termination_mode") not in {"process_group", "direct"}:
        return None
    pgid = payload.get("posix_pgid")
    if pgid is not None and (not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1):
        return None
    cleanup_kind = payload.get("cleanup_kind")
    cleanup_id = payload.get("cleanup_id")
    if cleanup_kind is not None and (
        cleanup_kind != "docker-container" or not _valid_cleanup_id(cleanup_id)
    ):
        return None
    if cleanup_kind is None and cleanup_id is not None:
        return None
    if not isinstance(payload.get("created_at"), (int, float)):
        return None
    return payload


def _valid_cleanup_id(value: Any) -> bool:
    return isinstance(value, str) and _DOCKER_CONTAINER_PATTERN.fullmatch(value) is not None


def _unlink_record(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _process_state(pid: int, expected_token: str) -> _ProcessState:
    existence = _pid_exists(pid)
    if existence is False:
        return "dead"
    token = _pid_start_token(pid)
    if token is not None:
        return "matching" if token == expected_token else "reused"
    return "unknown"


def _pid_start_token(pid: int) -> str | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        return _windows_process_start_token(pid)
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
        after_comm = text.rsplit(") ", 1)[1]
        return f"linux:{after_comm.split()[19]}"
    except (OSError, IndexError, ValueError):
        pass
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            **command_text(),
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return f"ps:{value}" if completed.returncode == 0 and value else None


def _pid_exists(pid: int) -> bool | None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        handle = _windows_open_process(pid, 0x00100000)
        if not handle:
            # OpenProcess uses ERROR_INVALID_PARAMETER for a PID that no longer exists. Access
            # denied remains unknown and is deliberately preserved rather than guessed dead.
            if ctypes.get_last_error() == 87:
                return False
            return None
        try:
            result = _windows_kernel32().WaitForSingleObject(handle, 0)
            return result == 0x00000102
        finally:
            _windows_kernel32().CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _windows_open_process(pid: int, access: int) -> Any:
    try:
        return _windows_kernel32().OpenProcess(access, False, pid)
    except (AttributeError, OSError):
        return None


def _windows_kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    return kernel32


def _windows_process_start_token(pid: int) -> str | None:
    handle = _windows_open_process(pid, 0x1000)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not _windows_kernel32().GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return f"windows:{ticks}"
    except (AttributeError, OSError):
        return None
    finally:
        _windows_kernel32().CloseHandle(handle)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _terminate_owned_process(payload: dict[str, Any]) -> bool:
    # Re-check immediately before signalling to narrow the PID-reuse race.
    if _process_state(payload["child_pid"], payload["child_start_token"]) != "matching":
        return False
    _cleanup_owned_resource(payload)
    child_pid = payload["child_pid"]
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        return _terminate_windows_process_tree(child_pid, payload["child_start_token"])
    if payload["termination_mode"] == "process_group":
        pgid = payload.get("posix_pgid")
        if pgid != child_pid or pgid == os.getpgrp():
            return False
        try:
            if os.getpgid(child_pid) != pgid:
                return False
        except ProcessLookupError:
            return True
        return _terminate_posix_group(pgid)
    try:
        os.kill(child_pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _terminate_windows_process_tree(child_pid: int, child_start_token: str) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(child_pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass

    # taskkill can return a non-zero status when the exact target exits during its own tree walk.
    # Conversely, a zero status is not sufficient reason to discard durable ownership state until
    # the recorded PID/token identity is confirmed gone. This also prevents a recycled PID from
    # being mistaken for the process we attempted to terminate.
    deadline = time.monotonic() + _TERMINATION_CONFIRM_TIMEOUT_S
    while True:
        state = _process_state(child_pid, child_start_token)
        if state in {"dead", "reused"}:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_TERMINATION_CONFIRM_INTERVAL_S)


def _terminate_posix_group(pgid: int) -> bool:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            break
        time.sleep(0.02)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    return True


def _cleanup_owned_resource(payload: dict[str, Any]) -> None:
    if payload.get("cleanup_kind") != "docker-container" or not _valid_cleanup_id(
        payload.get("cleanup_id")
    ):
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["docker", "rm", "-f", payload["cleanup_id"]],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
        )
