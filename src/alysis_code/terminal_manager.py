from __future__ import annotations

import codecs
import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from .background_runner import (
    BackgroundProcessSpawn,
    BackgroundShellRunner,
    BackgroundTerminationMode,
)
from .sandbox_settings import ShellSandboxSettings
from .terminal_ownership import TerminalOwnershipHandle, TerminalOwnershipLedger

_LOGGER = logging.getLogger("alysis_code.terminal_manager")
_STREAM_READ_CHUNK_SIZE = 8192
_POST_SIGKILL_WAIT_TIMEOUT_S = 1.0

ProcessStatus = Literal["running", "exited", "killed", "failed"]
OutputStream = Literal["stdout", "stderr"]
WaitUntil = Literal["output_available", "process_exited", "either"]
_TERMINAL_STATUSES: set[ProcessStatus] = {"exited", "killed", "failed"}


class TerminalLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutputLine:
    seq: int
    stream: OutputStream
    text: str
    ts: float


@dataclass(frozen=True)
class ProcessOutputSnapshot:
    process_id: str
    status: ProcessStatus
    exit_code: int | None
    failure_reason: str | None
    lines: tuple[OutputLine, ...]
    next_seq: int
    dropped_lines: int
    started_at_wall: float
    runtime_s: float
    total_bytes: int


@dataclass(frozen=True)
class ProcessSummary:
    process_id: str
    cmd: str
    cwd: Path
    status: ProcessStatus
    exit_code: int | None
    runtime_s: float
    started_at_wall: float


class _OutputBuffer:
    def __init__(self, *, max_lines: int, max_bytes: int) -> None:
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._lines: deque[OutputLine] = deque(maxlen=max_lines)
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._current_bytes = 0
        self._total_bytes = 0
        self._next_seq = 1
        self._dropped_lines = 0
        self._lock = threading.Lock()

    def append_line(self, *, stream: OutputStream, text: str) -> None:
        encoded_len = len(text.encode("utf-8", errors="replace"))
        with self._lock:
            if len(self._lines) >= self._max_lines:
                self._drop_oldest_locked()
            line = OutputLine(
                seq=self._next_seq,
                stream=stream,
                text=text,
                ts=time.time(),
            )
            self._next_seq += 1
            self._lines.append(line)
            self._current_bytes += encoded_len
            self._total_bytes += encoded_len
            while self._current_bytes > self._max_bytes and self._lines:
                self._drop_oldest_locked()

    def snapshot_since(self, since: int) -> tuple[tuple[OutputLine, ...], int, int, int]:
        with self._lock:
            lines = tuple(line for line in self._lines if line.seq > since)
            last_seq = self._next_seq - 1
            return lines, last_seq, self._dropped_lines, self._total_bytes

    def _drop_oldest_locked(self) -> None:
        dropped = self._lines.popleft()
        self._current_bytes -= len(dropped.text.encode("utf-8", errors="replace"))
        self._dropped_lines += 1


class BackgroundProcess:
    def __init__(
        self,
        *,
        spawn: BackgroundProcessSpawn,
        cmd: str,
        cwd: Path,
        output_max_lines: int,
        output_max_bytes: int,
        ownership_ledger: TerminalOwnershipLedger | None = None,
    ) -> None:
        self.process_id = uuid.uuid4().hex[:12]
        self.cmd = cmd
        self.cwd = cwd
        self.started_at = time.perf_counter()
        self.created_at_wall = time.time()
        self.status: ProcessStatus = "running"
        self.exit_code: int | None = None
        self.failure_reason: str | None = None
        self.started_argv = spawn.started_argv

        self._popen = spawn.popen
        self._cleanup = spawn.cleanup
        self._process_tree = spawn.process_tree
        self._termination_mode: BackgroundTerminationMode = spawn.termination_mode
        self._output = _OutputBuffer(max_lines=output_max_lines, max_bytes=output_max_bytes)
        self._condition = threading.Condition()
        self._cleanup_lock = threading.Lock()
        self._kill_lock = threading.Lock()
        self._cleanup_called = False
        self._kill_requested = False
        self._failure_requested = False
        self._ended_at: float | None = None
        self._posix_pgid = (
            _resolve_posix_process_group_id(self._popen)
            if self._termination_mode == "process_group"
            else None
        )
        self._ownership_ledger = ownership_ledger
        self._ownership_handle: TerminalOwnershipHandle | None = None
        if ownership_ledger is not None:
            self._ownership_handle = ownership_ledger.track(
                child_pid=self._popen.pid,
                termination_mode=self._termination_mode,
                posix_pgid=self._posix_pgid,
                cleanup_kind=spawn.orphan_cleanup_kind,
                cleanup_id=spawn.orphan_cleanup_id,
            )
        self._stdout_done = threading.Event()
        self._stderr_done = threading.Event()

        self._stdout_reader = threading.Thread(
            target=self._read_stream,
            name=f"alysis-bg-{self.process_id}-stdout",
            args=("stdout", self._popen.stdout, self._stdout_done),
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stream,
            name=f"alysis-bg-{self.process_id}-stderr",
            args=("stderr", self._popen.stderr, self._stderr_done),
            daemon=True,
        )
        self._waiter = threading.Thread(
            target=self._wait_for_process,
            name=f"alysis-bg-{self.process_id}-waiter",
            daemon=True,
        )
        self._stdout_reader.start()
        self._stderr_reader.start()
        self._waiter.start()

    def read(self, since: int = 0) -> ProcessOutputSnapshot:
        lines, next_seq, dropped_lines, total_bytes = self._output.snapshot_since(since)
        with self._condition:
            status = self.status
            exit_code = self.exit_code
            failure_reason = self.failure_reason
            runtime_s = self._runtime_s_locked()
        return ProcessOutputSnapshot(
            process_id=self.process_id,
            status=status,
            exit_code=exit_code,
            failure_reason=failure_reason,
            lines=lines,
            next_seq=next_seq,
            dropped_lines=dropped_lines,
            started_at_wall=self.created_at_wall,
            runtime_s=runtime_s,
            total_bytes=total_bytes,
        )

    def wait_for_output(
        self,
        *,
        since: int = 0,
        timeout_s: float = 0.0,
        until: WaitUntil = "either",
    ) -> tuple[ProcessOutputSnapshot, bool]:
        if since < 0:
            raise ValueError("since must be non-negative")
        if until not in {"output_available", "process_exited", "either"}:
            raise ValueError(f"unsupported wait condition: {until}")
        deadline = time.perf_counter() + max(0.0, float(timeout_s))
        with self._condition:
            while True:
                # Check and subscribe under one lock: output/exit published
                # between the read and wait must not become a lost wakeup.
                snapshot = self.read(since)
                has_output = bool(snapshot.lines)
                exited = snapshot.status in _TERMINAL_STATUSES
                if until == "output_available" and has_output:
                    return snapshot, False
                if until == "process_exited" and exited:
                    return snapshot, False
                if until == "either" and (has_output or exited):
                    return snapshot, False
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return snapshot, True
                self._condition.wait(remaining)

    def kill(self, timeout_s: float) -> ProcessOutputSnapshot:
        with self._kill_lock:
            with self._condition:
                if self._is_terminal_locked():
                    already_terminal = True
                else:
                    already_terminal = False
                    self._kill_requested = True
            if already_terminal:
                return self.read()

            self._send_terminate()
            if not self.wait_for_exit(timeout_s):
                self._send_kill()
                self.wait_for_exit(_POST_SIGKILL_WAIT_TIMEOUT_S)
            return self.read()

    def wait_for_exit(self, timeout_s: float | None) -> bool:
        deadline = None if timeout_s is None else time.perf_counter() + max(timeout_s, 0.0)
        with self._condition:
            while not self._is_terminal_locked():
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def is_terminal(self) -> bool:
        with self._condition:
            return self._is_terminal_locked()

    def is_terminal_older_than(self, older_than_s: float) -> bool:
        with self._condition:
            if not self._is_terminal_locked() or self._ended_at is None:
                return False
            return time.perf_counter() - self._ended_at >= older_than_s

    def summary(self) -> ProcessSummary:
        with self._condition:
            return ProcessSummary(
                process_id=self.process_id,
                cmd=_truncate_command(self.cmd),
                cwd=self.cwd,
                status=self.status,
                exit_code=self.exit_code,
                runtime_s=self._runtime_s_locked(),
                started_at_wall=self.created_at_wall,
            )

    def _read_stream(
        self,
        stream_name: OutputStream,
        stream: IO[bytes] | None,
        done: threading.Event,
    ) -> None:
        if stream is None:
            done.set()
            return
        pending = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = os.read(stream.fileno(), _STREAM_READ_CHUNK_SIZE)
                if not chunk:
                    break
                pending = self._append_decoded_chunk(
                    stream_name=stream_name,
                    pending=pending,
                    decoded=decoder.decode(chunk),
                )
            pending = self._append_decoded_chunk(
                stream_name=stream_name,
                pending=pending,
                decoded=decoder.decode(b"", final=True),
            )
            if pending:
                self._append_output_line(stream=stream_name, text=pending)
        except OSError as exc:
            if not self._kill_requested:
                self._fail_reader_and_request_kill(f"{stream_name} reader failed: {exc}")
                _LOGGER.exception(
                    "Background process %s %s reader failed",
                    self.process_id,
                    stream_name,
                )
        except Exception as exc:  # noqa: BLE001
            # Reader threads must never die silently; surface the failure in process state.
            self._fail_reader_and_request_kill(f"{stream_name} reader failed: {exc}")
            _LOGGER.exception(
                "Background process %s %s reader crashed",
                self.process_id,
                stream_name,
            )
        finally:
            with contextlib.suppress(OSError):
                stream.close()
            done.set()

    def _append_decoded_chunk(
        self,
        *,
        stream_name: OutputStream,
        pending: str,
        decoded: str,
    ) -> str:
        pending += decoded
        while True:
            newline_idx = pending.find("\n")
            if newline_idx < 0:
                return pending
            line = pending[: newline_idx + 1]
            pending = pending[newline_idx + 1 :]
            self._append_output_line(stream=stream_name, text=line)

    def _append_output_line(self, *, stream: OutputStream, text: str) -> None:
        self._output.append_line(stream=stream, text=text)
        with self._condition:
            self._condition.notify_all()

    def _wait_for_process(self) -> None:
        try:
            exit_code = self._popen.wait()
        except Exception as exc:  # noqa: BLE001
            # The waiter is the only code path allowed to reap the child process.
            cleanup_reason = self._run_cleanup_once()
            reason = f"process wait failed: {exc}"
            if cleanup_reason:
                reason = f"{reason}; {cleanup_reason}"
            _LOGGER.exception("Background process %s waiter failed", self.process_id)
            self._publish_terminal_status(
                status="failed",
                exit_code=None,
                failure_reason=reason,
            )
            return

        # Detached children can keep pipes open after their parent exits. Stop
        # owned Seatbelt descendants before waiting for the readers' EOF.
        cleanup_reason = self._run_cleanup_once() if self._process_tree is not None else None
        if cleanup_reason is None:
            self._stdout_done.wait()
            self._stderr_done.wait()
        cleanup_reason = cleanup_reason or self._run_cleanup_once()
        if cleanup_reason is None and self._ownership_ledger is not None:
            self._ownership_ledger.release(self._ownership_handle)

        with self._condition:
            self.exit_code = exit_code
            if cleanup_reason:
                self.failure_reason = cleanup_reason
                self._failure_requested = True
            if self._failure_requested:
                self.status = "failed"
            else:
                self.status = "killed" if self._kill_requested else "exited"
            if self._ended_at is None:
                self._ended_at = time.perf_counter()
            self._condition.notify_all()

    def _fail_reader_and_request_kill(self, reason: str) -> None:
        with self._condition:
            self.failure_reason = reason
            self._failure_requested = True
            if self.status == "running":
                self._kill_requested = True
            else:
                self._condition.notify_all()
                return
        self._send_kill()

    def _send_terminate(self) -> None:
        if self._process_tree is not None:
            self._process_tree.signal(signal.SIGTERM)
        if self._termination_mode == "direct":
            self._run_cleanup_and_record_failure()
            _terminate_direct_popen(self._popen)
            return

        if os.name == "nt":  # pragma: no cover - exercised on Windows
            with contextlib.suppress(ProcessLookupError):
                self._popen.send_signal(signal.CTRL_BREAK_EVENT)
            return

        if self._posix_pgid is None:
            with contextlib.suppress(ProcessLookupError):
                self._popen.terminate()
            return
        _signal_posix_process_group(self._posix_pgid, signal.SIGTERM)

    def _send_kill(self) -> None:
        if self._process_tree is not None:
            self._process_tree.signal(signal.SIGKILL)
        if self._termination_mode == "direct":
            self._run_cleanup_and_record_failure()
            _kill_direct_popen(self._popen)
            return

        if os.name == "nt":  # pragma: no cover - exercised on Windows
            _kill_windows_process_tree(self._popen)
            return

        if self._posix_pgid is None:
            with contextlib.suppress(ProcessLookupError):
                self._popen.kill()
            return
        _signal_posix_process_group(self._posix_pgid, signal.SIGKILL)

    def _run_cleanup_once(self) -> str | None:
        cleanup: Callable[[], None] | None = None
        with self._cleanup_lock:
            if self._cleanup_called:
                return None
            self._cleanup_called = True
            cleanup = self._cleanup
        try:
            cleanup()
            return None
        except Exception as exc:  # noqa: BLE001
            # Cleanup failures are preserved on the process and logged for operators.
            _LOGGER.exception("Background process %s cleanup failed", self.process_id)
            return f"process cleanup failed: {exc}"

    def _run_cleanup_and_record_failure(self) -> None:
        cleanup_reason = self._run_cleanup_once()
        if cleanup_reason is None:
            return
        with self._condition:
            self.failure_reason = cleanup_reason
            self._failure_requested = True
            self._condition.notify_all()

    def _publish_terminal_status(
        self,
        *,
        status: ProcessStatus,
        exit_code: int | None,
        failure_reason: str | None,
    ) -> None:
        with self._condition:
            self.status = status
            self.exit_code = exit_code
            self.failure_reason = failure_reason
            if status == "failed":
                self._failure_requested = True
            if self._ended_at is None:
                self._ended_at = time.perf_counter()
            self._condition.notify_all()

    def _is_terminal_locked(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def _runtime_s_locked(self) -> float:
        end = self._ended_at if self._ended_at is not None else time.perf_counter()
        return max(0.0, end - self.started_at)


class TerminalManager:
    def __init__(
        self,
        *,
        runner: BackgroundShellRunner,
        settings: ShellSandboxSettings,
        ownership_ledger: TerminalOwnershipLedger | None = None,
    ) -> None:
        self._runner = runner
        self._settings = settings
        self._ownership_ledger = ownership_ledger or TerminalOwnershipLedger()
        self._ownership_ledger.scavenge_stale()
        self._processes: dict[str, BackgroundProcess] = {}
        self._pending_starts = 0
        self._lock = threading.Lock()

    def start(
        self,
        *,
        cmd: str,
        cwd: Path,
        root: Path,
        env_overrides: dict[str, str] | None = None,
        scoped_runner: BackgroundShellRunner | None = None,
        scoped_settings: ShellSandboxSettings | None = None,
    ) -> str:
        if (scoped_runner is None) != (scoped_settings is None):
            raise ValueError("Scoped background execution requires both runner and settings")
        settings = scoped_settings or self._settings
        with self._lock:
            if not cmd.strip():
                raise ValueError("cmd cannot be empty")

            root_abs = root.resolve()
            cwd_abs = cwd.resolve()
            from .sandbox_workspace import contains_workspace_path

            if not contains_workspace_path(
                root_abs,
                cwd_abs,
                (*settings.additional_workspace_roots, *settings.additional_read_roots),
            ):
                raise ValueError(f"cwd escapes root and approved additional folders: {cwd}")

            running_count = sum(
                1 for process in self._processes.values() if not process.is_terminal()
            )
            if running_count + self._pending_starts >= self._settings.background_max_concurrent:
                raise TerminalLimitError(
                    "Maximum background process count reached "
                    f"({self._settings.background_max_concurrent})."
                )
            self._pending_starts += 1

        spawn: BackgroundProcessSpawn | None = None
        pending_released = False
        try:
            spawn = (scoped_runner or self._runner).start(
                root=root_abs,
                cwd=cwd_abs,
                cmd=cmd,
                env_overrides=env_overrides,
            )
            try:
                process = BackgroundProcess(
                    spawn=spawn,
                    cmd=cmd,
                    cwd=cwd_abs,
                    output_max_lines=self._settings.background_output_max_lines,
                    output_max_bytes=self._settings.background_output_max_bytes,
                    ownership_ledger=self._ownership_ledger,
                )
            except BaseException:
                with self._lock:
                    self._pending_starts -= 1
                    pending_released = True
                _terminate_spawned_process(spawn)
                raise
            with self._lock:
                self._processes[process.process_id] = process
                self._pending_starts -= 1
                pending_released = True
            return process.process_id
        except BaseException:
            if not pending_released:
                with self._lock:
                    self._pending_starts -= 1
            raise

    def read(self, process_id: str, *, since: int = 0) -> ProcessOutputSnapshot:
        return self._get_process(process_id).read(since)

    def wait_for_output(
        self,
        process_id: str,
        *,
        since: int = 0,
        timeout_s: float = 0.0,
        until: WaitUntil = "either",
    ) -> tuple[ProcessOutputSnapshot, bool]:
        return self._get_process(process_id).wait_for_output(
            since=since,
            timeout_s=timeout_s,
            until=until,
        )

    def kill(self, process_id: str) -> ProcessOutputSnapshot:
        process = self._get_process(process_id)
        return process.kill(self._settings.background_kill_timeout_s)

    def list(self) -> tuple[ProcessSummary, ...]:
        with self._lock:
            summaries = [process.summary() for process in self._processes.values()]
        return tuple(sorted(summaries, key=lambda summary: summary.started_at_wall))

    def shutdown_all(self, *, kill_timeout_s: float | None = None) -> None:
        timeout_s = (
            self._settings.background_kill_timeout_s if kill_timeout_s is None else kill_timeout_s
        )
        with self._lock:
            processes = tuple(self._processes.values())
        for process in processes:
            if process.is_terminal():
                continue
            try:
                process.kill(timeout_s)
            except Exception:  # noqa: BLE001
                # Shutdown is best-effort across independent child processes.
                _LOGGER.exception(
                    "Failed to shut down background process %s",
                    process.process_id,
                )

    def prune(self, *, older_than_s: float = 600.0) -> None:
        with self._lock:
            stale_ids = [
                process_id
                for process_id, process in self._processes.items()
                if process.is_terminal_older_than(older_than_s)
            ]
            for process_id in stale_ids:
                del self._processes[process_id]

    def _get_process(self, process_id: str) -> BackgroundProcess:
        with self._lock:
            return self._processes[process_id]


def _truncate_command(cmd: str) -> str:
    if len(cmd) <= 120:
        return cmd
    return f"{cmd[:117]}..."


def _resolve_posix_process_group_id(popen: subprocess.Popen[bytes]) -> int | None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return None
    pid = popen.pid
    if pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return pid


def _signal_posix_process_group(pgid: int, signum: int) -> bool:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return True
    except OSError:
        _LOGGER.exception("Failed to signal process group %s with signal %s", pgid, signum)
        return False
    return True


def _terminate_direct_popen(popen: subprocess.Popen[bytes]) -> None:
    if popen.poll() is not None:
        return
    try:
        popen.terminate()
    except ProcessLookupError:
        return
    except OSError:
        _LOGGER.exception("Failed to terminate background process %s", popen.pid)


def _kill_direct_popen(popen: subprocess.Popen[bytes]) -> None:
    if popen.poll() is not None:
        return
    try:
        popen.kill()
    except ProcessLookupError:
        return
    except OSError:
        _LOGGER.exception("Failed to kill background process %s", popen.pid)


def _kill_windows_process_tree(popen: subprocess.Popen[bytes]) -> None:
    kwargs: dict[str, object] = {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if create_no_window is not None:
        kwargs["creationflags"] = create_no_window

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(popen.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        result = None

    if result is not None and result.returncode == 0:
        return

    with contextlib.suppress(ProcessLookupError):
        popen.kill()


def _terminate_direct_process(popen: subprocess.Popen[bytes], *, timeout_s: float) -> None:
    _terminate_direct_popen(popen)
    if not _wait_for_popen_exit(popen, timeout_s=timeout_s) and popen.poll() is None:
        _kill_direct_popen(popen)
        _wait_for_popen_exit(popen, timeout_s=_POST_SIGKILL_WAIT_TIMEOUT_S)


def _terminate_process_tree(popen: subprocess.Popen[bytes], *, timeout_s: float) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        if popen.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                popen.send_signal(signal.CTRL_BREAK_EVENT)
        if not _wait_for_popen_exit(popen, timeout_s=timeout_s) and popen.poll() is None:
            _kill_windows_process_tree(popen)
            _wait_for_popen_exit(popen, timeout_s=_POST_SIGKILL_WAIT_TIMEOUT_S)
        return

    pgid = _resolve_posix_process_group_id(popen)
    if popen.poll() is None:
        if pgid is None:
            with contextlib.suppress(ProcessLookupError):
                popen.terminate()
        else:
            _signal_posix_process_group(pgid, signal.SIGTERM)
    if not _wait_for_popen_exit(popen, timeout_s=timeout_s) and popen.poll() is None:
        if pgid is None:
            with contextlib.suppress(ProcessLookupError):
                popen.kill()
        else:
            _signal_posix_process_group(pgid, signal.SIGKILL)
        _wait_for_popen_exit(popen, timeout_s=_POST_SIGKILL_WAIT_TIMEOUT_S)


def _wait_for_popen_exit(popen: subprocess.Popen[bytes], *, timeout_s: float) -> bool:
    try:
        popen.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False
    return True


def _terminate_spawned_process(spawn: BackgroundProcessSpawn) -> None:
    if spawn.termination_mode == "direct":
        try:
            spawn.cleanup()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Background process spawn cleanup failed after start error")
        _terminate_direct_process(spawn.popen, timeout_s=1.0)
    else:
        _terminate_process_tree(spawn.popen, timeout_s=1.0)
        try:
            spawn.cleanup()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Background process spawn cleanup failed after start error")
    with contextlib.suppress(OSError):
        if spawn.popen.stdout is not None:
            spawn.popen.stdout.close()
    with contextlib.suppress(OSError):
        if spawn.popen.stderr is not None:
            spawn.popen.stderr.close()
