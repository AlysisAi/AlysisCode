from __future__ import annotations

import ctypes
import os
import queue
import re
import select
import subprocess
import threading
import time
import weakref
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cancellation import raise_if_cancelled
from .cancellation import current_mcp_cancellation_token
from .errors import McpProcessError, McpProtocolError, McpTimeoutError
from .jsonrpc import (
    JsonRpcIdGenerator,
    JsonRpcNotification,
    JsonRpcProtocolError,
    JsonRpcRequest,
    JsonRpcResponse,
    build_jsonrpc_error_response,
    build_jsonrpc_notification,
    build_jsonrpc_request,
    build_jsonrpc_result_response,
    encode_jsonrpc_message,
    parse_jsonrpc_line,
)
from .models import ResolvedMcpServer
from .server_requests import (
    McpServerRequestContext,
    McpServerRequestHandler,
    McpServerRequestHandlerError,
    McpUnsupportedServerRequestError,
)

_STDERR_LINE_LIMIT = 80
_STDERR_CHAR_LIMIT = 4000
_SHUTDOWN_WAIT_S = 0.75
_TERMINATE_WAIT_S = 1.0
_UNSUPPORTED_CLIENT_REQUEST_CODE = -32601
_STDOUT_READ_CHUNK_SIZE = 4096
_STDERR_READ_CHUNK_SIZE = 4096
_STDOUT_NOTIFICATION_QUIESCENCE_WAIT_S = 0.25
_STDOUT_RESPONSE_QUIESCENCE_WAIT_S = 0.1
_STDOUT_RESPONSE_FOLLOW_UP_BUDGET_S = _STDOUT_RESPONSE_QUIESCENCE_WAIT_S * 2
_STDERR_QUIESCENCE_WAIT_S = 0.75
_FD_READY_POLL_INTERVAL_S = 0.01
_READER_THREAD_JOIN_WAIT_S = 1.0
_RETIRED_REQUEST_STATE_RETENTION_LIMIT = 1024
_REQUEST_STATE_AWAITING_RESPONSE = "awaiting_response"
_REQUEST_STATE_AWAITING_FOLLOW_UP = "awaiting_follow_up"
_REQUEST_STATE_COMPLETED = "completed"
_REQUEST_STATE_RESPONSE_TIMED_OUT = "response_timed_out"
_REQUEST_STATE_FOLLOW_UP_TIMED_OUT = "follow_up_timed_out"
_REQUEST_STATE_CANCELLED = "cancelled"
_REQUEST_STATES_DISCARD_LATE_RESPONSES = frozenset(
    {
        _REQUEST_STATE_RESPONSE_TIMED_OUT,
        _REQUEST_STATE_FOLLOW_UP_TIMED_OUT,
        _REQUEST_STATE_CANCELLED,
    }
)
_CONSERVATIVE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "TERM",
    "SystemRoot",
    "SYSTEMROOT",
    "ComSpec",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    # Network plumbing: a server behind a corporate proxy or a TLS-inspecting gateway
    # cannot reach anything without these. Both spellings, since tools differ.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
_INTERNAL_CLIENT_REQUEST_CODE = -32603
_IS_WINDOWS = os.name == "nt"
_WINDOWS_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
_WINDOWS_BATCH_EXTENSIONS = frozenset({".bat", ".cmd"})
# cmd.exe re-reads a batch file's arguments: quoting keeps these characters literal,
# but nothing reliably protects a double quote, a percent sign or a line break.
_WINDOWS_BATCH_QUOTED_CHARS = frozenset(" \t&|<>^(),;=!")
_WINDOWS_BATCH_REFUSED_CHARS = frozenset('"%\r\n')
_INVALID_STDOUT_UTF8_MESSAGE = "received invalid UTF-8 on stdio stdout"
_LIVE_STDIO_TRANSPORTS: weakref.WeakSet[Any] = weakref.WeakSet()


class McpStdioTransportError(McpProcessError):
    pass


class McpStdioTransportTimeoutError(McpStdioTransportError, McpTimeoutError):
    error_code = "mcp_stdio_transport_timeout"


class McpStdioTransportProtocolError(McpStdioTransportError, McpProtocolError):
    error_code = "mcp_stdio_transport_protocol_error"
    retryable = False


def _decode_stdout_protocol_line(line_bytes: bytes) -> str:
    try:
        return line_bytes.decode("utf-8").rstrip("\r")
    except UnicodeDecodeError as exc:
        raise McpStdioTransportProtocolError(_INVALID_STDOUT_UTF8_MESSAGE) from exc


@dataclass(frozen=True)
class _QueuedResponse:
    response: JsonRpcResponse


@dataclass(frozen=True)
class _DeferredResponse:
    response: JsonRpcResponse
    follow_up_deadline_monotonic: float


@dataclass(frozen=True)
class _ObservedResponse:
    follow_up_deadline_monotonic: float


@dataclass(frozen=True)
class _DeferredResponseFollowUpTimeout:
    pass


@dataclass
class _PendingRequest:
    response_queue: queue.Queue[Any]
    follow_up_budget_s: float


@dataclass(frozen=True)
class _FatalTransportState:
    message: str
    exc_type: type[McpStdioTransportError]
    stderr_append_sequence: int
    stderr_read_sequence: int
    stderr_last_activity_monotonic: float
    fatal_monotonic: float


def _env_value(env: Mapping[str, str], name: str) -> str:
    """Windows environment names are case-insensitive (`Path` and `PATH` are one variable)."""
    value = env.get(name)
    if value is not None:
        return value
    folded = name.casefold()
    for key, item in env.items():
        if key.casefold() == folded:
            return item
    return ""


def resolve_windows_command(command: str, *, env: Mapping[str, str]) -> str | None:
    """Find a bare command the way a Windows shell would, or return None.

    CreateProcess only adds ".exe" to a bare name and searches the parent's PATH, so a
    launcher such as `npx` (npx.cmd) is never found. This looks on the child's PATH and
    tries each PATHEXT extension. A command with a directory part is left alone.
    """
    if not command or os.path.dirname(command):
        return None
    extensions = [
        ext.strip()
        for ext in (_env_value(env, "PATHEXT") or _WINDOWS_DEFAULT_PATHEXT).split(";")
        if ext.strip()
    ]
    suffix = os.path.splitext(command)[1].casefold()
    if suffix and suffix in {ext.casefold() for ext in extensions}:
        names = [command]
    else:
        names = [command + ext for ext in extensions]
    for raw_directory in _env_value(env, "PATH").split(os.pathsep):
        directory = raw_directory.strip().strip('"')
        if not directory:
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def windows_batch_command_line(executable: str, args: Sequence[str]) -> str:
    """Build the command line for a .bat/.cmd launcher.

    cmd.exe runs these files and reads their arguments again, so an unquoted `&` or `|`
    would start a second command. Every value holding such a character is quoted, and
    values that quoting cannot protect are refused.
    """
    parts: list[str] = []
    for index, value in enumerate((executable, *args)):
        refused = sorted(_WINDOWS_BATCH_REFUSED_CHARS.intersection(value))
        if refused:
            label = "the command" if index == 0 else f"argument {index}"
            shown = ", ".join(repr(char) for char in refused)
            raise ValueError(
                f"{label} contains {shown}, which a .cmd or .bat launcher cannot receive "
                "safely; point the command at the program itself (for example node.exe)."
            )
        if value and not _WINDOWS_BATCH_QUOTED_CHARS.intersection(value):
            parts.append(value)
            continue
        # A quoted value must not end in a backslash that would escape the closing quote.
        escaped = re.sub(r"(\\+)$", r"\1\1", value)
        parts.append(f'"{escaped}"')
    return " ".join(parts)


def build_stdio_subprocess_env(
    *,
    overlay_env: Mapping[str, str],
    host_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source_env = os.environ if host_env is None else host_env
    env: dict[str, str] = {}
    for key in _CONSERVATIVE_ENV_KEYS:
        value = source_env.get(key)
        if value:
            env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    for key, value in overlay_env.items():
        env[str(key)] = str(value)
    return env


class McpStdioTransport:
    def __init__(
        self,
        *,
        server: ResolvedMcpServer,
        workspace_root: Path,
        host_env: Mapping[str, str] | None = None,
        server_request_context: McpServerRequestContext | None = None,
        server_request_handler: McpServerRequestHandler | None = None,
    ) -> None:
        if server.transport != "stdio":
            raise McpStdioTransportError(f"MCP server '{server.id}' is not a stdio transport.")
        if server.command is None:
            raise McpStdioTransportError(f"MCP server '{server.id}' is missing a stdio command.")
        self.server = server
        self.workspace_root = workspace_root.resolve()
        self.host_env = os.environ if host_env is None else host_env
        self._server_request_context = server_request_context
        self._server_request_handler = server_request_handler
        self.spawn_env = build_stdio_subprocess_env(
            overlay_env=server.env,
            host_env=self.host_env,
        )
        self._id_generator = JsonRpcIdGenerator()
        self._notifications: queue.Queue[JsonRpcNotification] = queue.Queue()
        self._pending: dict[int | str, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._request_states: dict[int | str, str] = {}
        self._retired_request_state_ids: deque[int | str] = deque()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_LINE_LIMIT)
        self._stderr_state_condition = threading.Condition()
        self._stderr_append_sequence = 0
        self._stderr_read_sequence = 0
        self._stderr_quiescence_generation = 0
        self._stderr_last_activity_monotonic = time.monotonic()
        self._stderr_reader_idle = False
        self._stderr_reader_closed = False
        self._stdout_state_condition = threading.Condition()
        self._stdout_quiescence_generation = 0
        self._stdout_read_sequence = 0
        self._stdout_last_activity_monotonic = time.monotonic()
        self._stdout_reader_idle = False
        self._stdout_reader_closed = False
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._started = False
        self._closed = False
        self._closing = False
        self._fatal_state: _FatalTransportState | None = None

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    @property
    def closed(self) -> bool:
        return self._closed

    def diagnostic_snapshot(self) -> dict[str, Any]:
        process = self._process
        return {
            "transport_id": id(self),
            "server_id": self.server.id,
            "command": self.server.command,
            "pid": process.pid if process is not None else None,
            "returncode": process.poll() if process is not None else None,
            "started": self._started,
            "closed": self._closed,
            "closing": self._closing,
            "stdout_thread_alive": bool(
                self._stdout_thread is not None and self._stdout_thread.is_alive()
            ),
            "stderr_thread_alive": bool(
                self._stderr_thread is not None and self._stderr_thread.is_alive()
            ),
        }

    def _has_live_process_or_threads(self) -> bool:
        process = self._process
        return bool(
            (process is not None and process.poll() is None)
            or (self._stdout_thread is not None and self._stdout_thread.is_alive())
            or (self._stderr_thread is not None and self._stderr_thread.is_alive())
        )

    def stderr_tail(self) -> str:
        with self._stderr_state_condition:
            joined = "\n".join(self._stderr_tail)
        if len(joined) <= _STDERR_CHAR_LIMIT:
            return joined
        return joined[-_STDERR_CHAR_LIMIT:]

    def _context(self) -> str:
        return f"server '{self.server.id}' (command '{self.server.command}')"

    def _format_error_message(self, message: str, *, stderr_tail: str = "") -> str:
        full_message = f"MCP stdio {self._context()}: {message}"
        cleaned_tail = str(stderr_tail or "").strip()
        if cleaned_tail:
            full_message += f"\nstderr tail:\n{cleaned_tail}"
        return full_message

    def _build_error(
        self,
        message: str,
        *,
        exc_type: type[McpStdioTransportError] = McpStdioTransportError,
    ) -> McpStdioTransportError:
        return exc_type(self._format_error_message(message, stderr_tail=self.stderr_tail().strip()))

    def _build_fatal_state(
        self,
        message: str,
        *,
        exc_type: type[McpStdioTransportError] = McpStdioTransportError,
    ) -> _FatalTransportState:
        with self._stderr_state_condition:
            stderr_sequence = self._stderr_append_sequence
            stderr_read_sequence = self._stderr_read_sequence
            stderr_last_activity_monotonic = self._stderr_last_activity_monotonic
        return _FatalTransportState(
            message=message,
            exc_type=exc_type,
            stderr_append_sequence=stderr_sequence,
            stderr_read_sequence=stderr_read_sequence,
            stderr_last_activity_monotonic=stderr_last_activity_monotonic,
            fatal_monotonic=time.monotonic(),
        )

    def _set_fatal_error(self, fatal_state: _FatalTransportState) -> None:
        with self._pending_lock:
            if self._fatal_state is not None:
                return
            self._fatal_state = fatal_state
            pending = [entry.response_queue for entry in self._pending.values()]
            self._pending.clear()
        with self._stdout_state_condition:
            self._stdout_state_condition.notify_all()
        with self._stderr_state_condition:
            self._stderr_state_condition.notify_all()
        for response_queue in pending:
            response_queue.put(fatal_state)

    def _set_request_state_locked(self, request_id: int | str, state: str) -> None:
        if state == _REQUEST_STATE_COMPLETED:
            self._request_states.pop(request_id, None)
            return
        self._request_states[request_id] = state
        if state not in _REQUEST_STATES_DISCARD_LATE_RESPONSES:
            return
        self._retired_request_state_ids.append(request_id)
        while len(self._retired_request_state_ids) > _RETIRED_REQUEST_STATE_RETENTION_LIMIT:
            retired_request_id = self._retired_request_state_ids.popleft()
            if (
                self._request_states.get(retired_request_id)
                in _REQUEST_STATES_DISCARD_LATE_RESPONSES
            ):
                self._request_states.pop(retired_request_id, None)

    def _finalize_fatal_error(self, fatal_state: _FatalTransportState) -> McpStdioTransportError:
        stderr_tail = self._wait_for_stderr_quiescence(
            after_sequence=fatal_state.stderr_append_sequence,
            after_read_sequence=fatal_state.stderr_read_sequence,
            quiet_since=max(
                fatal_state.stderr_last_activity_monotonic,
                fatal_state.fatal_monotonic,
            ),
        )
        return fatal_state.exc_type(
            self._format_error_message(
                fatal_state.message,
                stderr_tail=stderr_tail,
            )
        )

    def _raise_if_fatal(self) -> None:
        if self._fatal_state is None:
            return
        raise self._finalize_fatal_error(self._fatal_state)

    def _fd_bytes_available(self, fd: int) -> bool:
        if fd < 0:
            return False
        if os.name == "nt":
            try:
                import msvcrt

                handle = msvcrt.get_osfhandle(fd)
                available = ctypes.c_ulong(0)
                ok = ctypes.windll.kernel32.PeekNamedPipe(  # type: ignore[attr-defined]
                    ctypes.c_void_p(handle),
                    None,
                    0,
                    None,
                    ctypes.byref(available),
                    None,
                )
                return bool(ok) and available.value > 0
            except Exception:
                return False
        try:
            readable, _, _ = select.select([fd], [], [], 0)
        except Exception:
            return False
        return bool(readable)

    def _read_fd_chunk(self, fd: int, *, size: int) -> bytes:
        while True:
            try:
                return os.read(fd, size)
            except InterruptedError:
                continue

    def _wait_for_fd_readable(self, fd: int, *, timeout_s: float) -> bool:
        if timeout_s <= 0:
            return self._fd_bytes_available(fd)
        if os.name != "nt":
            try:
                readable, _, _ = select.select([fd], [], [], timeout_s)
            except Exception:
                pass
            else:
                return bool(readable)
        deadline = time.monotonic() + timeout_s
        while True:
            if self._fd_bytes_available(fd):
                return True
            process = self._process
            if process is not None and process.poll() is not None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_FD_READY_POLL_INTERVAL_S, remaining))

    def _mark_stdout_quiescent(self) -> int:
        # A new generation is published only after the reader has drained every
        # currently available stdout line and is about to block for more input.
        with self._stdout_state_condition:
            self._stdout_reader_idle = True
            self._stdout_quiescence_generation += 1
            generation = self._stdout_quiescence_generation
            self._stdout_state_condition.notify_all()
        return generation

    def _set_stdout_reader_active(self) -> None:
        with self._stdout_state_condition:
            if not self._stdout_reader_idle:
                return
            self._stdout_reader_idle = False
            self._stdout_state_condition.notify_all()

    def _mark_stdout_reader_closed(self) -> None:
        with self._stdout_state_condition:
            self._stdout_reader_idle = True
            self._stdout_reader_closed = True
            self._stdout_quiescence_generation += 1
            self._stdout_state_condition.notify_all()

    def _mark_stdout_bytes_read(self) -> None:
        with self._stdout_state_condition:
            self._stdout_read_sequence += 1
            self._stdout_last_activity_monotonic = time.monotonic()
            self._stdout_state_condition.notify_all()

    def _wait_for_post_write_quiescence(
        self,
        *,
        after_generation: int,
        after_read_sequence: int,
        timeout_s: float,
        allow_idle_return_without_activity: bool,
    ) -> None:
        # Notifications have no response marker. The host therefore waits for the
        # next reader-published quiescent boundary after the write. A short no-output
        # grace window handles notifications with no completion timeout. When the
        # caller provides an explicit completion timeout, "no output yet" remains
        # pending until the full timeout elapses so delayed post-write stdout can
        # still surface before success.
        activity_deadline = time.monotonic() + timeout_s
        no_output_deadline = time.monotonic() + (
            _STDOUT_NOTIFICATION_QUIESCENCE_WAIT_S
            if allow_idle_return_without_activity
            else timeout_s
        )
        while True:
            with self._stdout_state_condition:
                if self._fatal_state is not None:
                    return
                if self._stdout_reader_closed:
                    return
                if self._stdout_quiescence_generation > after_generation:
                    return
                read_sequence = self._stdout_read_sequence
                reader_idle = self._stdout_reader_idle
            observed_post_write_activity = read_sequence > after_read_sequence
            if not observed_post_write_activity and reader_idle:
                remaining = no_output_deadline - time.monotonic()
                if remaining <= 0:
                    return
            else:
                remaining = activity_deadline - time.monotonic()
                if remaining <= 0:
                    raise self._build_error(
                        "timed out waiting for post-notification stdout quiescence.",
                        exc_type=McpStdioTransportTimeoutError,
                    )
            with self._stdout_state_condition:
                if self._fatal_state is not None:
                    return
                if self._stdout_reader_closed:
                    return
                if self._stdout_quiescence_generation > after_generation:
                    return
                if not observed_post_write_activity and reader_idle:
                    remaining = no_output_deadline - time.monotonic()
                else:
                    remaining = activity_deadline - time.monotonic()
                if remaining <= 0:
                    if not observed_post_write_activity and reader_idle:
                        return
                    raise self._build_error(
                        "timed out waiting for post-notification stdout quiescence.",
                        exc_type=McpStdioTransportTimeoutError,
                    )
                self._stdout_state_condition.wait(timeout=remaining)

    def _append_stderr_line(self, line: str) -> None:
        cleaned = line.rstrip("\r")
        if not cleaned:
            return
        with self._stderr_state_condition:
            self._stderr_tail.append(cleaned)
            self._stderr_append_sequence += 1
            self._stderr_last_activity_monotonic = time.monotonic()
            self._stderr_state_condition.notify_all()

    def _mark_stderr_quiescent(self) -> int:
        with self._stderr_state_condition:
            self._stderr_reader_idle = True
            self._stderr_quiescence_generation += 1
            generation = self._stderr_quiescence_generation
            self._stderr_state_condition.notify_all()
        return generation

    def _mark_stderr_bytes_read(self) -> None:
        with self._stderr_state_condition:
            self._stderr_read_sequence += 1
            self._stderr_last_activity_monotonic = time.monotonic()
            self._stderr_state_condition.notify_all()

    def _set_stderr_reader_active(self) -> None:
        with self._stderr_state_condition:
            if not self._stderr_reader_idle:
                return
            self._stderr_reader_idle = False
            self._stderr_state_condition.notify_all()

    def _mark_stderr_reader_closed(self) -> None:
        with self._stderr_state_condition:
            self._stderr_reader_idle = True
            self._stderr_reader_closed = True
            self._stderr_quiescence_generation += 1
            self._stderr_state_condition.notify_all()

    def _wait_for_stderr_quiescence(
        self,
        *,
        after_sequence: int,
        after_read_sequence: int,
        quiet_since: float,
    ) -> str:
        # Fatal stderr diagnostics stay live until stderr has actually gone quiet
        # after the fatal point; the first later quiescent generation is not enough.
        observed_append_sequence = after_sequence
        observed_read_sequence = after_read_sequence
        while True:
            with self._stderr_state_condition:
                append_sequence = self._stderr_append_sequence
                read_sequence = self._stderr_read_sequence
                last_activity_monotonic = self._stderr_last_activity_monotonic
                reader_closed = self._stderr_reader_closed
                if (
                    append_sequence > observed_append_sequence
                    or read_sequence > observed_read_sequence
                ):
                    observed_append_sequence = append_sequence
                    observed_read_sequence = read_sequence
                    quiet_since = max(quiet_since, last_activity_monotonic)
                if reader_closed:
                    joined = "\n".join(self._stderr_tail)
                    break
                remaining = (quiet_since + _STDERR_QUIESCENCE_WAIT_S) - time.monotonic()
                if remaining <= 0:
                    joined = "\n".join(self._stderr_tail)
                    break
                self._stderr_state_condition.wait(timeout=remaining)
        if len(joined) <= _STDERR_CHAR_LIMIT:
            return joined.strip()
        return joined[-_STDERR_CHAR_LIMIT:].strip()

    def _wait_for_deferred_response_follow_up(
        self,
        *,
        stdout_fd: int,
        deferred_responses: Mapping[int | str, _DeferredResponse],
    ) -> bool:
        with self._stdout_state_condition:
            quiet_deadline = (
                self._stdout_last_activity_monotonic + _STDOUT_RESPONSE_QUIESCENCE_WAIT_S
            )
        earliest_follow_up_deadline = min(
            deferred.follow_up_deadline_monotonic for deferred in deferred_responses.values()
        )
        remaining = min(
            quiet_deadline - time.monotonic(),
            earliest_follow_up_deadline - time.monotonic(),
        )
        if remaining <= 0:
            return False
        return self._wait_for_fd_readable(stdout_fd, timeout_s=remaining)

    def _deferred_response_ids_ready_for_release(
        self,
        deferred_responses: Mapping[int | str, _DeferredResponse],
    ) -> tuple[int | str, ...]:
        if not deferred_responses:
            return ()
        with self._stdout_state_condition:
            quiet_deadline = (
                self._stdout_last_activity_monotonic + _STDOUT_RESPONSE_QUIESCENCE_WAIT_S
            )
        now = time.monotonic()
        if now >= quiet_deadline:
            return tuple(deferred_responses.keys())
        return ()

    def _deferred_response_ids_timed_out(
        self,
        deferred_responses: Mapping[int | str, _DeferredResponse],
    ) -> tuple[int | str, ...]:
        now = time.monotonic()
        return tuple(
            request_id
            for request_id, deferred in deferred_responses.items()
            if now >= deferred.follow_up_deadline_monotonic
        )

    def start(self) -> None:
        if self._started:
            self._raise_if_fatal()
            return
        if self._closed:
            raise self._build_error("transport is already closed.")
        try:
            self._process = self._spawn([self.server.command, *self.server.args])
        except FileNotFoundError as exc:
            self._process = self._spawn_resolved_windows_command(exc)
        except Exception as exc:  # noqa: BLE001
            raise self._build_error(f"failed to launch process: {exc}") from exc
        self._started = True
        _LIVE_STDIO_TRANSPORTS.add(self)
        self._stdout_thread = threading.Thread(
            target=self._stdout_reader,
            name=f"mcp-stdout-{self.server.id}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader,
            name=f"mcp-stderr-{self.server.id}",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _spawn(self, launch: list[str] | str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            launch,
            cwd=self.workspace_root,
            env=self.spawn_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def _spawn_resolved_windows_command(
        self, not_found: FileNotFoundError
    ) -> subprocess.Popen[bytes]:
        """Retry a bare command that Windows could not find, as `npx` -> `npx.cmd`."""
        resolved = (
            resolve_windows_command(self.server.command, env=self.spawn_env)
            if _IS_WINDOWS
            else None
        )
        if resolved is None:
            raise self._build_error(f"failed to launch process: {not_found}") from not_found
        try:
            if os.path.splitext(resolved)[1].casefold() in _WINDOWS_BATCH_EXTENSIONS:
                return self._spawn(windows_batch_command_line(resolved, self.server.args))
            return self._spawn([resolved, *self.server.args])
        except Exception as exc:  # noqa: BLE001
            raise self._build_error(f"failed to launch process: {exc}") from exc

    def _stderr_reader(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            stderr_fd = process.stderr.fileno()
        except Exception:
            self._mark_stderr_reader_closed()
            return
        buffered_bytes = bytearray()
        self._mark_stderr_quiescent()
        try:
            while True:
                chunk = self._read_fd_chunk(stderr_fd, size=_STDERR_READ_CHUNK_SIZE)
                if not chunk:
                    if buffered_bytes:
                        self._append_stderr_line(
                            bytes(buffered_bytes).decode("utf-8", errors="replace")
                        )
                        buffered_bytes.clear()
                    break
                self._set_stderr_reader_active()
                self._mark_stderr_bytes_read()
                buffered_bytes.extend(chunk)
                self._drain_stderr_lines(buffered_bytes)
                while self._fd_bytes_available(stderr_fd):
                    extra_chunk = self._read_fd_chunk(
                        stderr_fd,
                        size=_STDERR_READ_CHUNK_SIZE,
                    )
                    if not extra_chunk:
                        break
                    self._mark_stderr_bytes_read()
                    buffered_bytes.extend(extra_chunk)
                    self._drain_stderr_lines(buffered_bytes)
                if not buffered_bytes:
                    self._mark_stderr_quiescent()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._mark_stderr_reader_closed()

    def _stdout_reader(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            stdout_fd = process.stdout.fileno()
        except Exception:
            self._set_fatal_error(self._build_fatal_state("stdout binary stream is unavailable."))
            return
        buffered_bytes = bytearray()
        deferred_responses: dict[int | str, _DeferredResponse] = {}
        self._mark_stdout_quiescent()
        try:
            while True:
                if not buffered_bytes and deferred_responses and self._fatal_state is None:
                    if not self._wait_for_deferred_response_follow_up(
                        stdout_fd=stdout_fd,
                        deferred_responses=deferred_responses,
                    ):
                        self._mark_stdout_quiescent()
                        self._flush_deferred_responses(deferred_responses)
                        continue
                chunk = self._read_fd_chunk(stdout_fd, size=_STDOUT_READ_CHUNK_SIZE)
                if not chunk:
                    if buffered_bytes:
                        trailing_line = _decode_stdout_protocol_line(bytes(buffered_bytes))
                        for message in parse_jsonrpc_line(trailing_line):
                            self._handle_incoming_message(
                                message,
                                deferred_responses=deferred_responses,
                            )
                        buffered_bytes.clear()
                    break
                self._set_stdout_reader_active()
                self._mark_stdout_bytes_read()
                buffered_bytes.extend(chunk)
                self._drain_stdout_lines(
                    buffered_bytes,
                    deferred_responses=deferred_responses,
                )
                while self._fd_bytes_available(stdout_fd):
                    extra_chunk = self._read_fd_chunk(
                        stdout_fd,
                        size=_STDOUT_READ_CHUNK_SIZE,
                    )
                    if not extra_chunk:
                        break
                    self._mark_stdout_bytes_read()
                    buffered_bytes.extend(extra_chunk)
                    self._drain_stdout_lines(
                        buffered_bytes,
                        deferred_responses=deferred_responses,
                    )
                if not buffered_bytes and not deferred_responses:
                    self._mark_stdout_quiescent()
                    self._flush_deferred_responses(deferred_responses)
        except JsonRpcProtocolError as exc:
            self._set_fatal_error(
                self._build_fatal_state(
                    f"received malformed JSON-RPC payload: {exc}",
                    exc_type=McpStdioTransportProtocolError,
                )
            )
        except McpStdioTransportProtocolError as exc:
            self._set_fatal_error(
                self._build_fatal_state(
                    str(exc),
                    exc_type=McpStdioTransportProtocolError,
                )
            )
        except Exception as exc:  # noqa: BLE001
            if not self._closing:
                self._set_fatal_error(self._build_fatal_state(f"stdout reader failed: {exc}"))
        finally:
            self._mark_stdout_reader_closed()
            if not self._closing:
                return_code = process.poll()
                detail = "stdio process exited unexpectedly"
                if return_code is not None:
                    detail = f"stdio process exited with code {return_code}"
                self._set_fatal_error(self._build_fatal_state(detail))

    def _drain_stderr_lines(self, buffered_bytes: bytearray) -> None:
        while True:
            newline_index = buffered_bytes.find(b"\n")
            if newline_index < 0:
                return
            line_bytes = bytes(buffered_bytes[:newline_index])
            del buffered_bytes[: newline_index + 1]
            self._append_stderr_line(line_bytes.decode("utf-8", errors="replace"))

    def _drain_stdout_lines(
        self,
        buffered_bytes: bytearray,
        *,
        deferred_responses: dict[int | str, _DeferredResponse],
    ) -> None:
        while True:
            newline_index = buffered_bytes.find(b"\n")
            if newline_index < 0:
                return
            line_bytes = bytes(buffered_bytes[:newline_index])
            del buffered_bytes[: newline_index + 1]
            line = _decode_stdout_protocol_line(line_bytes)
            for message in parse_jsonrpc_line(line):
                self._handle_incoming_message(
                    message,
                    deferred_responses=deferred_responses,
                )

    def _flush_deferred_responses(
        self, deferred_responses: dict[int | str, _DeferredResponse]
    ) -> None:
        # Responses stay hidden until the reader reaches the current stdout quiet
        # boundary. If later stdout activity moves that boundary forward and the
        # bounded follow-up budget expires first, the active call fails instead.
        if not deferred_responses or self._fatal_state is not None:
            deferred_responses.clear()
            return
        ready_ids = self._deferred_response_ids_ready_for_release(deferred_responses)
        timed_out_ids = self._deferred_response_ids_timed_out(deferred_responses)
        if timed_out_ids:
            timed_out_id_set = set(timed_out_ids)
            ready_ids = tuple(
                request_id for request_id in ready_ids if request_id not in timed_out_id_set
            )
        if not ready_ids and not timed_out_ids:
            return
        queued: list[int | str] = []
        unknown_request_id: int | str | None = None
        duplicate_request_id: int | str | None = None
        expiring_ids = timed_out_ids
        releasable_ids = ready_ids
        with self._pending_lock:
            for request_id in expiring_ids:
                pending_request = self._pending.get(request_id)
                request_state = self._request_states.get(request_id)
                if pending_request is None:
                    if request_state in _REQUEST_STATES_DISCARD_LATE_RESPONSES:
                        queued.append(request_id)
                        continue
                    if request_state == _REQUEST_STATE_COMPLETED:
                        duplicate_request_id = request_id
                        break
                    unknown_request_id = request_id
                    break
                if request_state == _REQUEST_STATE_AWAITING_FOLLOW_UP:
                    pending_request.response_queue.put(_DeferredResponseFollowUpTimeout())
                    self._pending.pop(request_id, None)
                    self._set_request_state_locked(request_id, _REQUEST_STATE_FOLLOW_UP_TIMED_OUT)
                    queued.append(request_id)
                    continue
                if request_state in _REQUEST_STATES_DISCARD_LATE_RESPONSES:
                    self._pending.pop(request_id, None)
                    queued.append(request_id)
                    continue
                unknown_request_id = request_id
                break
            if unknown_request_id is None and duplicate_request_id is None:
                for request_id in releasable_ids:
                    deferred = deferred_responses.get(request_id)
                    if deferred is None:
                        continue
                    pending_request = self._pending.get(request_id)
                    request_state = self._request_states.get(request_id)
                    if pending_request is None:
                        if request_state in _REQUEST_STATES_DISCARD_LATE_RESPONSES:
                            queued.append(request_id)
                            continue
                        if request_state == _REQUEST_STATE_COMPLETED:
                            duplicate_request_id = request_id
                            break
                        unknown_request_id = request_id
                        break
                    if request_state == _REQUEST_STATE_AWAITING_FOLLOW_UP:
                        pending_request.response_queue.put(
                            _QueuedResponse(response=deferred.response)
                        )
                        self._pending.pop(request_id, None)
                        self._set_request_state_locked(request_id, _REQUEST_STATE_COMPLETED)
                        queued.append(request_id)
                        continue
                    if request_state in _REQUEST_STATES_DISCARD_LATE_RESPONSES:
                        self._pending.pop(request_id, None)
                        queued.append(request_id)
                        continue
                    unknown_request_id = request_id
                    break
        if duplicate_request_id is not None:
            self._set_fatal_error(
                self._build_fatal_state(
                    f"received duplicate response for request id {duplicate_request_id!r}",
                    exc_type=McpStdioTransportProtocolError,
                )
            )
            return
        if unknown_request_id is not None:
            self._set_fatal_error(
                self._build_fatal_state(
                    f"received response for unknown request id {unknown_request_id!r}",
                    exc_type=McpStdioTransportProtocolError,
                )
            )
            return
        for request_id in queued:
            deferred_responses.pop(request_id, None)

    def _handle_response_message(
        self,
        message: JsonRpcResponse,
        *,
        deferred_responses: dict[int | str, _DeferredResponse],
    ) -> None:
        if message.id in deferred_responses:
            self._set_fatal_error(
                self._build_fatal_state(
                    f"received duplicate response for request id {message.id!r}",
                    exc_type=McpStdioTransportProtocolError,
                )
            )
            return
        fatal_message: str | None = None
        with self._pending_lock:
            pending_request = self._pending.get(message.id)
            request_state = self._request_states.get(message.id)
            if pending_request is None:
                if request_state in _REQUEST_STATES_DISCARD_LATE_RESPONSES:
                    return
                if request_state == _REQUEST_STATE_COMPLETED:
                    fatal_message = f"received duplicate response for request id {message.id!r}"
                else:
                    fatal_message = f"received response for unknown request id {message.id!r}"
            elif request_state == _REQUEST_STATE_AWAITING_RESPONSE:
                follow_up_deadline = time.monotonic() + pending_request.follow_up_budget_s
                pending_request.response_queue.put(
                    _ObservedResponse(follow_up_deadline_monotonic=follow_up_deadline)
                )
                self._set_request_state_locked(message.id, _REQUEST_STATE_AWAITING_FOLLOW_UP)
                deferred_responses[message.id] = _DeferredResponse(
                    response=message,
                    follow_up_deadline_monotonic=follow_up_deadline,
                )
                return
            elif request_state == _REQUEST_STATE_AWAITING_FOLLOW_UP:
                fatal_message = f"received duplicate response for request id {message.id!r}"
            elif request_state in _REQUEST_STATES_DISCARD_LATE_RESPONSES:
                self._pending.pop(message.id, None)
                return
            else:
                fatal_message = f"received response for unknown request id {message.id!r}"
        if fatal_message is not None:
            self._set_fatal_error(
                self._build_fatal_state(
                    fatal_message,
                    exc_type=McpStdioTransportProtocolError,
                )
            )

    def _retire_request_on_timeout(
        self,
        *,
        request_id: int | str,
        observed_response: bool,
    ) -> None:
        with self._pending_lock:
            pending_request = self._pending.pop(request_id, None)
            request_state = self._request_states.get(request_id)
            if pending_request is None and request_state == _REQUEST_STATE_COMPLETED:
                return
            if observed_response or request_state == _REQUEST_STATE_AWAITING_FOLLOW_UP:
                self._set_request_state_locked(request_id, _REQUEST_STATE_FOLLOW_UP_TIMED_OUT)
                return
            self._set_request_state_locked(request_id, _REQUEST_STATE_RESPONSE_TIMED_OUT)

    def _send_serialized(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise self._build_error("stdin is unavailable.")
        if process.poll() is not None:
            raise self._build_error(f"stdio process already exited with code {process.poll()}")
        with self._write_lock:
            try:
                process.stdin.write((encode_jsonrpc_message(payload) + "\n").encode("utf-8"))
                process.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                raise self._build_error(f"failed to write request: {exc}") from exc

    def _fatal_unsupported_server_request(self, method: str) -> None:
        self._set_fatal_error(
            self._build_fatal_state(
                f"received unsupported server-initiated request '{method}'",
                exc_type=McpStdioTransportProtocolError,
            )
        )

    def _handle_server_request_message(self, message: JsonRpcRequest) -> None:
        if self._server_request_handler is None or self._server_request_context is None:
            try:
                self._send_serialized(
                    build_jsonrpc_error_response(
                        request_id=message.id,
                        code=_UNSUPPORTED_CLIENT_REQUEST_CODE,
                        message=(
                            "Server-initiated requests are not supported by this MCP host runtime."
                        ),
                    )
                )
            except Exception:
                pass
            self._fatal_unsupported_server_request(message.method)
            return
        try:
            result = self._server_request_handler.handle_request(
                context=self._server_request_context,
                method=message.method,
                request_id=message.id,
                params=message.params,
            )
        except McpUnsupportedServerRequestError as exc:
            try:
                self._send_serialized(
                    build_jsonrpc_error_response(
                        request_id=message.id,
                        code=exc.code,
                        message=exc.message,
                    )
                )
            except Exception:
                pass
            self._fatal_unsupported_server_request(message.method)
            return
        except McpServerRequestHandlerError as exc:
            try:
                self._send_serialized(
                    build_jsonrpc_error_response(
                        request_id=message.id,
                        code=exc.code,
                        message=exc.message,
                    )
                )
            except Exception:
                pass
            self._set_fatal_error(
                self._build_fatal_state(
                    f"failed to handle server-initiated request '{message.method}'",
                    exc_type=McpStdioTransportProtocolError,
                )
            )
            return
        except Exception:
            try:
                self._send_serialized(
                    build_jsonrpc_error_response(
                        request_id=message.id,
                        code=_INTERNAL_CLIENT_REQUEST_CODE,
                        message="Internal MCP host error while handling server-initiated request.",
                    )
                )
            except Exception:
                pass
            self._set_fatal_error(
                self._build_fatal_state(
                    f"failed to handle server-initiated request '{message.method}'",
                    exc_type=McpStdioTransportProtocolError,
                )
            )
            return
        try:
            self._send_serialized(
                build_jsonrpc_result_response(
                    request_id=message.id,
                    result=result,
                )
            )
        except Exception as exc:
            self._set_fatal_error(
                self._build_fatal_state(
                    f"failed to respond to server-initiated request '{message.method}': {exc}",
                    exc_type=McpStdioTransportProtocolError,
                )
            )

    def _handle_incoming_message(
        self,
        message: JsonRpcRequest | JsonRpcNotification | JsonRpcResponse,
        *,
        deferred_responses: dict[int | str, _DeferredResponse],
    ) -> None:
        if isinstance(message, JsonRpcNotification):
            self._notifications.put(message)
            return
        if isinstance(message, JsonRpcResponse):
            self._handle_response_message(
                message,
                deferred_responses=deferred_responses,
            )
            return
        self._handle_server_request_message(message)

    def request(
        self,
        *,
        method: str,
        params: dict[str, Any] | None,
        timeout_s: float,
    ) -> JsonRpcResponse:
        if timeout_s <= 0:
            raise self._build_error("timeout_s must be > 0.")
        cancellation_token = current_mcp_cancellation_token()
        raise_if_cancelled(cancellation_token)
        self.start()
        self._raise_if_fatal()
        request_id = self._id_generator.next()
        response_queue: queue.Queue[Any] = queue.Queue()
        response_deadline = time.monotonic() + timeout_s
        with self._pending_lock:
            self._pending[request_id] = _PendingRequest(
                response_queue=response_queue,
                follow_up_budget_s=_STDOUT_RESPONSE_FOLLOW_UP_BUDGET_S,
            )
            self._set_request_state_locked(request_id, _REQUEST_STATE_AWAITING_RESPONSE)
        try:
            self._send_serialized(
                build_jsonrpc_request(
                    request_id=request_id,
                    method=method,
                    params=params,
                )
            )
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
                self._set_request_state_locked(request_id, _REQUEST_STATE_CANCELLED)
            raise
        try:
            return self._wait_for_response(
                request_id=request_id,
                method=method,
                timeout_s=timeout_s,
                response_deadline=response_deadline,
                response_queue=response_queue,
                cancellation_token=cancellation_token,
            )
        except BaseException:
            # Only this request is revoked. A pooled MCP connection (and other
            # chats' connections) remains usable after the server acknowledges.
            with self._pending_lock:
                self._pending.pop(request_id, None)
                request_state = self._request_states.get(request_id)
                awaiting_response = request_state in {
                    _REQUEST_STATE_AWAITING_RESPONSE,
                    _REQUEST_STATE_RESPONSE_TIMED_OUT,
                }
                if (
                    request_state is not None
                    and request_state not in _REQUEST_STATES_DISCARD_LATE_RESPONSES
                ):
                    self._set_request_state_locked(request_id, _REQUEST_STATE_CANCELLED)
            try:
                # A received response has already completed the server's work,
                # even while we are still checking trailing stdout for errors.
                if awaiting_response and method != "initialize":
                    self._send_serialized(
                        build_jsonrpc_notification(
                            method="notifications/cancelled",
                            params={"requestId": request_id, "reason": "Request interrupted"},
                        )
                    )
            except Exception:
                pass
            raise

    def _wait_for_response(
        self,
        *,
        request_id: int | str,
        method: str,
        timeout_s: float,
        response_deadline: float,
        response_queue: queue.Queue[Any],
        cancellation_token: Any | None,
    ) -> JsonRpcResponse:
        current_deadline = response_deadline
        observed_response = False
        while True:
            raise_if_cancelled(cancellation_token)
            remaining = current_deadline - time.monotonic()
            if remaining <= 0:
                try:
                    response = response_queue.get_nowait()
                except queue.Empty as exc:
                    self._retire_request_on_timeout(
                        request_id=request_id,
                        observed_response=observed_response,
                    )
                    message = f"timed out waiting for response to '{method}' after {timeout_s:.3f}s"
                    if observed_response:
                        message = (
                            f"timed out waiting for post-response stdout follow-up for "
                            f"'{method}' after {timeout_s:.3f}s"
                        )
                    raise self._build_error(
                        message,
                        exc_type=McpStdioTransportTimeoutError,
                    ) from exc
            else:
                try:
                    response = response_queue.get(
                        timeout=min(remaining, 0.05)
                        if cancellation_token is not None
                        else remaining
                    )
                except queue.Empty:
                    continue
            if isinstance(response, _ObservedResponse):
                observed_response = True
                current_deadline = response.follow_up_deadline_monotonic
                continue
            if isinstance(response, _DeferredResponseFollowUpTimeout):
                observed_response = True
                raise self._build_error(
                    f"timed out waiting for post-response stdout follow-up for "
                    f"'{method}' after {timeout_s:.3f}s",
                    exc_type=McpStdioTransportTimeoutError,
                )
            if isinstance(response, _FatalTransportState):
                raise self._finalize_fatal_error(response)
            if not isinstance(response, _QueuedResponse):
                raise self._build_error(
                    f"received invalid response object for '{method}'",
                    exc_type=McpStdioTransportProtocolError,
                )
            self._raise_if_fatal()
            return response.response

    def send_notification(
        self,
        *,
        method: str,
        params: dict[str, Any] | None,
        completion_timeout_s: float | None = None,
    ) -> None:
        self.start()
        self._raise_if_fatal()
        with self._stdout_state_condition:
            post_write_generation = self._stdout_quiescence_generation
            post_write_read_sequence = self._stdout_read_sequence
        self._send_serialized(
            build_jsonrpc_notification(
                method=method,
                params=params,
            )
        )
        self._wait_for_post_write_quiescence(
            after_generation=post_write_generation,
            after_read_sequence=post_write_read_sequence,
            timeout_s=max(
                completion_timeout_s or _STDOUT_NOTIFICATION_QUIESCENCE_WAIT_S,
                _STDOUT_NOTIFICATION_QUIESCENCE_WAIT_S,
            ),
            allow_idle_return_without_activity=completion_timeout_s is None,
        )
        self._raise_if_fatal()

    def drain_notifications(self) -> tuple[JsonRpcNotification, ...]:
        self._raise_if_fatal()
        notifications: list[JsonRpcNotification] = []
        while True:
            try:
                notifications.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        self._raise_if_fatal()
        return tuple(notifications)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        with self._pending_lock:
            self._pending.clear()
            self._request_states.clear()
            self._retired_request_state_ids.clear()
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=_SHUTDOWN_WAIT_S)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=_TERMINATE_WAIT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=_TERMINATE_WAIT_S)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            for thread in (self._stdout_thread, self._stderr_thread):
                if thread is not None:
                    thread.join(timeout=_READER_THREAD_JOIN_WAIT_S)
            if not self._has_live_process_or_threads():
                _LIVE_STDIO_TRANSPORTS.discard(self)


def live_stdio_transport_diagnostics() -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for transport in list(_LIVE_STDIO_TRANSPORTS):
        snapshot = transport.diagnostic_snapshot()
        if (
            not snapshot["closed"]
            or snapshot["returncode"] is None
            or snapshot["stdout_thread_alive"]
            or snapshot["stderr_thread_alive"]
        ):
            diagnostics.append(snapshot)
        else:
            _LIVE_STDIO_TRANSPORTS.discard(transport)
    diagnostics.sort(key=lambda item: (str(item["server_id"]), int(item["transport_id"])))
    return diagnostics
