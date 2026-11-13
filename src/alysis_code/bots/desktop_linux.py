"""Offline desktop child for the qualified local Linux lab.

The parent owns credentials and authorization. This executor receives neither.
It is a process/mount/network boundary inside the disposable lab, not a claim of
production tenant isolation. No host display, arbitrary runtime root or network
enable switch is accepted. Runtime tool admission is deliberately separate.
"""

from __future__ import annotations

import json
import os
import platform
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from ..cancellation import CooperativeCancellationError
from .screen_channel import capture as capture_screen

MAX_REPLY_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
# Captured from the trusted controller installation, never supplied by a Bot.
PYTHON = sys.executable


def runtime_paths() -> tuple[Path, ...]:
    """Expose installed code, never a Python prefix's unrelated config files."""
    prefix = Path(sys.prefix).absolute()
    if sys.prefix == sys.base_prefix or prefix.resolve() in {
        Path("/"),
        Path("/tmp"),
        Path.home().resolve(),
    }:
        raise DesktopError("Private computers require a dedicated managed Python environment.")
    paths = [prefix]
    for base in {Path(sys.base_prefix), Path(sys.base_exec_prefix)}:
        paths.extend(base / name for name in ("bin", "lib", "lib64"))
    result = []
    for path in paths:
        if path.exists() and not any(
            path == Path(base) or Path(base) in path.parents
            for base in ("/usr", "/bin", "/lib", "/lib64")
        ):
            if path not in result:
                result.append(path)
    return tuple(result)


def available() -> bool:
    """Qualify the actual local namespace and display, without a model call."""
    if platform.system() != "Linux" or any(
        not Path("/usr/bin", name).is_file()
        for name in ("bwrap", "Xvfb", "xdotool", "xclip", "openbox")
    ):
        return False
    with tempfile.TemporaryDirectory(prefix="alysis-computer-probe-") as directory:
        try:
            desktop = OfflineLinuxDesktop("setup", Path(directory))
        except DesktopStartupCleanupError:
            raise
        except (DesktopError, OSError):
            return False
        desktop.close()
        return True


class DesktopError(RuntimeError):
    pass


class DesktopCapacityBusy(DesktopError):
    """Definite capacity refusal before any provider creation was dispatched."""


class ComputerActionUncertain(DesktopError):
    """An action may have run; end this task instead of offering a retry."""


class DesktopStartupCleanupError(DesktopError):
    def __init__(self, executor):
        super().__init__("Offline computer startup cleanup is pending.")
        self.executor = executor


class OfflineLinuxDesktop:
    """One owned ephemeral computer; any uncertain transport failure retires it."""

    def __init__(self, owner: str, workspace: Path, *, cancel=lambda: False):
        if platform.system() != "Linux":
            raise DesktopError("The private desktop executor requires Linux.")
        if not owner or not workspace.is_dir() or workspace.is_symlink():
            raise ValueError("An owner and explicit workspace directory are required.")
        self.owner = owner
        self.session_id = uuid4().hex
        self.workspace = workspace.resolve(strict=True)
        self._lock = threading.RLock()
        self._view_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._cleaned = False
        self._process = None
        self._buffer = b""
        self._directory = tempfile.TemporaryDirectory(prefix="alysis-offline-computer-")
        root = Path(self._directory.name)
        (root / "screen").mkdir(mode=0o700)
        uid, gid = os.getuid(), os.getgid()
        (root / "passwd").write_text(f"user:x:{uid}:{gid}:user:/tmp/home:/bin/sh\n")
        (root / "group").write_text(f"user:x:{gid}:\n")
        try:
            self._process = subprocess.Popen(
                self.build_argv(root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.workspace,
                env={"PATH": "/usr/bin:/bin"},
                start_new_session=True,
                close_fds=True,
                bufsize=0,
            )
            self._request("ping", {}, cancel=cancel, timeout=15)
        except BaseException:
            try:
                self.close()
            except Exception:
                raise DesktopStartupCleanupError(self) from None
            raise

    def human_input(self, event):
        return self._request("human_input", event, cancel=lambda: False, timeout=12)

    def build_argv(self, root: Path) -> list[str]:
        args = [
            "/usr/bin/bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--cap-drop",
            "ALL",
        ]
        # System roots and this Python installation only. Never mount sys.path, HOME, the
        # service database, config, API /proc, Docker socket or another display.
        for path in (
            "/usr",
            "/bin",
            "/lib",
            "/lib64",
            "/etc/fonts",
            "/etc/libreoffice",
            "/etc/chromium.d",
        ):
            if Path(path).exists():
                args.extend(["--ro-bind", path, path])
        args.extend(
            [
                "--ro-bind",
                str(Path(__file__).with_name("desktop_worker.py")),
                "/opt/desktop_worker.py",
                "--ro-bind",
                str(Path(__file__).with_name("screen_channel.py")),
                "/opt/screen_channel.py",
                "--ro-bind",
                str(Path(__file__).with_name("human_input.py")),
                "/opt/human_input.py",
                "--bind",
                str(root / "screen"),
                "/run/alysis-screen",
                "--bind",
                str(self.workspace),
                "/workspace",
                "--dir",
                "/etc",
                "--ro-bind",
                str(root / "passwd"),
                "/etc/passwd",
                "--ro-bind",
                str(root / "group"),
                "/etc/group",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/tmp/home",
                "--chdir",
                "/workspace",
                "--setenv",
                "PATH",
                f"{Path(PYTHON).parent}:/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/tmp/home",
                "--setenv",
                "LANG",
                "C.UTF-8",
            ]
        )
        paths = runtime_paths()
        # A bundled interpreter can itself live under /tmp. Mount it after the
        # private /tmp and seal its virtual ancestors, without exposing siblings.
        temporary_roots = sorted(
            {Path("/tmp") / path.parts[2] for path in paths if Path("/tmp") in path.parents}
        )
        if Path("/tmp/home") in temporary_roots:
            raise DesktopError("The Python installation overlaps the private computer's home.")
        for path in temporary_roots:
            args.extend(["--tmpfs", str(path)])
        for path in paths:
            args.extend(["--ro-bind", str(path), str(path)])
        for path in temporary_roots:
            args.extend(["--remount-ro", str(path)])
        args.extend(
            ["--remount-ro", "/", "--", PYTHON, "-I", "/opt/desktop_worker.py", self.session_id]
        )
        return args

    def alive(self) -> bool:
        return (
            not self._closed
            and not self._stop.is_set()
            and self._process is not None
            and self._process.poll() is None
        )

    def _request(self, operation: str, arguments: dict, *, cancel, timeout: float):
        with self._lock:
            if not self.alive():
                raise DesktopError("This computer is closed; create a fresh session.")
            request_id = uuid4().hex
            payload = (
                json.dumps(
                    {"id": request_id, "operation": operation, "arguments": arguments}
                ).encode()
                + b"\n"
            )
            if len(payload) > MAX_REQUEST_BYTES:
                raise ValueError("Computer request exceeds its byte allowance.")
            deadline = time.monotonic() + timeout
            try:
                if cancel() or self._stop.is_set():
                    raise CooperativeCancellationError("Computer operation cancelled.")
                descriptor = self._process.stdin.fileno()
                os.set_blocking(descriptor, False)
                offset = 0
                while offset < len(payload):
                    if cancel() or self._stop.is_set():
                        raise CooperativeCancellationError("Computer operation cancelled.")
                    if time.monotonic() >= deadline:
                        raise DesktopError("Computer request timed out; outcome may be uncertain.")
                    if select.select([], [descriptor], [], 0.05)[1]:
                        try:
                            offset += os.write(descriptor, payload[offset:])
                        except BlockingIOError:
                            pass
                while b"\n" not in self._buffer:
                    if cancel() or self._stop.is_set():
                        raise CooperativeCancellationError("Computer operation cancelled.")
                    if time.monotonic() >= deadline:
                        raise DesktopError(
                            "Computer operation timed out; outcome may be uncertain."
                        )
                    if select.select([self._process.stdout], [], [], 0.05)[0]:
                        chunk = os.read(self._process.stdout.fileno(), 65536)
                        if not chunk:
                            raise DesktopError("Computer disconnected; outcome may be uncertain.")
                        self._buffer += chunk
                        if len(self._buffer) > MAX_REPLY_BYTES:
                            raise DesktopError("Computer reply exceeded its byte allowance.")
                line, self._buffer = self._buffer.split(b"\n", 1)
                reply = json.loads(line)
                if not isinstance(reply, dict) or reply.get("id") != request_id:
                    raise DesktopError("Computer reply did not match the operation.")
                if not (
                    isinstance(reply.get("result"), dict) or isinstance(reply.get("error"), str)
                ):
                    raise DesktopError("Computer reply is invalid.")
            except BaseException:
                self.close()
                raise
            if reply.get("error"):
                # The worker returns fixed validation/error codes, not exception
                # strings containing filesystem contents or credentials.
                if reply.get("fatal"):
                    self.close()
                raise DesktopError(str(reply["error"]))
            return reply["result"]

    def call(self, owner: str, operation: str, arguments: dict, *, cancel=lambda: False):
        if owner != self.owner:
            raise DesktopError("This computer belongs to another owner.")
        if operation == "check_file":
            from .artifact_validation import await_check

            return await_check(
                lambda operation, **args: self._request(
                    operation, {**arguments, **args}, cancel=cancel, timeout=40
                )
            )
        if operation not in {
            "observe",
            "click",
            "type",
            "key",
            "scroll",
            "run",
            "launch",
            "status",
            "stop_process",
            "write_file",
            "read_file",
            "check_file_start",
            "check_file_result",
        }:
            raise ValueError("Unsupported computer operation.")
        return self._request(operation, arguments, cancel=cancel, timeout=40)

    def capture(self) -> bytes:
        if not self._view_lock.acquire(blocking=False):
            raise BlockingIOError("Screen capture is already running.")
        try:
            if not self.alive():
                raise DesktopError("Computer screen is closed.")
            pixels = capture_screen(Path(self._directory.name) / "screen", self.session_id)
            if not self.alive():
                raise DesktopError("Computer screen was revoked.")
            return pixels
        finally:
            self._view_lock.release()

    def close(self) -> None:
        # Wake the operation holding the lock before waiting for its cleanup.
        self._stop.set()
        with self._lock:
            if self._cleaned:
                return
            self._closed = True
            process = self._process
            if process is not None:

                def signal_owned_group(signum):
                    try:
                        os.killpg(process.pid, signum)
                    except (ProcessLookupError, PermissionError):
                        # A child can exit between poll and killpg (macOS may
                        # report EPERM for its disappearing process group).
                        # Accept only a confirmed exit of our Popen child.
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            raise DesktopError(
                                "Computer shutdown is not confirmed; cleanup must retry."
                            ) from None

                if process.poll() is None:
                    signal_owned_group(signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        signal_owned_group(signal.SIGKILL)
                        process.wait(timeout=3)
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        pipe.close()
            self._directory.cleanup()
            self._cleaned = True
