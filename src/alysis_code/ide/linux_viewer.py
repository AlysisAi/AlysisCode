"""Owned Linux viewer handoff primitive for a trusted, authenticated relay.

There is deliberately no listener or authentication endpoint here. The caller
supplies one already-authorized duplex socket, for a private X11 display used by
the managed browser. Other input-capable X11 clients must not share that display.
This is not yet wired to Bots or the desktop UI.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
from typing import Any

from .managed_browser import BrowserControlError, CancelCheck, ManagedBrowserService

_CLEANUP_PHASE_PREFIX = b"ALYSIS_INPUT_CLEANUP_PHASE="
_CLEANUP_PHASES = (
    "helper-entry",
    "display-open",
    "state-read",
    "input-release",
    "input-sync",
    "state-verify",
    "verified-clear",
    "display-close",
    "display-closed",
)
_CLEANUP_DIAGNOSTIC_BYTES = 4096


def _cleanup_phase(phase: str) -> None:
    # Fixed, unbuffered protocol markers; never include display or input data.
    if phase not in _CLEANUP_PHASES:
        raise ValueError("Unknown input cleanup phase.")
    try:
        os.write(2, _CLEANUP_PHASE_PREFIX + phase.encode("ascii") + b"\n")
    except OSError:
        # Diagnostic delivery must not prevent the actual release or close.
        pass


def _cleanup_diagnostics(stderr: bytes | str | None) -> str:
    """Extract only complete allowlisted markers from a bounded stderr prefix."""
    if isinstance(stderr, str):
        data = stderr[:_CLEANUP_DIAGNOSTIC_BYTES].encode("ascii", errors="replace")
    elif isinstance(stderr, bytes):
        data = stderr[:_CLEANUP_DIAGNOSTIC_BYTES]
    else:
        data = b""
    allowed = {_CLEANUP_PHASE_PREFIX + phase.encode("ascii"): phase for phase in _CLEANUP_PHASES}
    phases = list(
        dict.fromkeys(allowed[line] for line in data.split(b"\n")[:-1] if line in allowed)
    )
    if not phases:
        return "Helper entry unconfirmed."
    return "Helper phases: " + ", ".join(phases) + "."


class LinuxViewerError(RuntimeError):
    """Viewer control could not be established or safely relinquished."""


class LinuxBrowserHandoff:
    """Fence agent commands while exactly one owned viewer process can input.

    ``begin`` consumes the supplied socket, including on failure. It grants input
    capability, not proof that a viewer completed its RFB handshake. Disconnect
    does not resume the agent: the relay must call ``end`` after EOF. Failed
    revocation, input cleanup or fresh observation keeps agent admission closed.
    Cleanup may complete a mouse gesture; it cannot undo already-delivered input.
    """

    def __init__(
        self,
        browser: ManagedBrowserService,
        owner_id: str,
        session_id: str,
        *,
        executable: str = "x11vnc",
    ) -> None:
        self._browser = browser
        self._owner = owner_id
        self._session = session_id
        self._display: str | None = None
        self._lease: str | None = None
        self._executable = executable
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._generation: int | None = None
        self._cleanup_required = False
        self._closing = False
        self._closed = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            browser = self._browser.status(self._owner, self._session)
            alive = self._process is not None and self._process.poll() is None
            state = "closed" if self._closed else "paused" if browser.commands_paused else "agent"
            if self._closing and not self._closed:
                state = "closing"
            elif self._process is not None:
                state = "viewer" if alive else "disconnected"
            return {
                "state": state,
                "viewer_process_alive": alive,
                "commands_paused": browser.commands_paused,
                "command_generation": browser.command_generation,
                "input_cleanup_pending": self._cleanup_required,
            }

    def begin(
        self, connection: socket.socket, *, timeout: float = 10, cancel: CancelCheck = None
    ) -> dict[str, Any]:
        """Drain browser work before passing one authorized socket to x11vnc."""
        try:
            with self._lock:
                if self._closing or self._process is not None or self._cleanup_required:
                    raise LinuxViewerError("The previous viewer must be closed before a new grant.")
                if self._lease is None:
                    self._display, self._lease = self._browser.claim_viewer_control(
                        self._owner, self._session
                    )
                paused = self._browser.pause_commands(
                    self._owner, self._session, timeout=timeout, cancel=cancel
                )
                self._generation = paused.command_generation
                try:
                    self._process = subprocess.Popen(
                        [
                            self._executable,
                            "-norc",
                            "-display",
                            self._display,
                            "-inetd",
                            "-once",
                            "-nopw",
                            "-input",
                            "KMBC",
                            "-novncconnect",
                            "-noremote",
                            "-nocmds",
                            "-q",
                        ],
                        stdin=connection,
                        stdout=connection,
                        stderr=subprocess.DEVNULL,
                        env={"PATH": os.defpath, "DISPLAY": self._display},
                        close_fds=True,
                    )
                except OSError:
                    raise LinuxViewerError(
                        "Viewer startup failed; browser commands remain paused."
                    ) from None
                self._cleanup_required = True
                return self.status()
        finally:
            connection.close()

    def end(
        self, *, resume: bool = True, timeout: float = 10, cancel: CancelCheck = None
    ) -> dict[str, Any]:
        """Reap the input process and verify X11 cleanup before any agent resume.

        Cancellation can prevent resume but cannot interrupt input revocation.
        The socket cannot be reused: a later grant requires a new connection and
        process, eliminating stale viewer input and x11vnc button bookkeeping.
        """
        with self._lock:
            if resume and self._closing:
                raise BrowserControlError("Viewer shutdown cannot resume browser commands.")
            self._revoke()
            if not resume:
                return self.status()
            if self._closed or self._generation is None:
                raise BrowserControlError("No completed viewer pause is available to resume.")
            snapshot = self._browser.resume_commands(
                self._owner,
                self._session,
                expected_generation=self._generation,
                timeout=timeout,
                cancel=cancel,
            )
            self._generation = None
            return snapshot

    def close(self) -> None:
        """Terminal, retryable revocation; never resume or depend on a disposed browser."""
        with self._lock:
            if self._closed:
                return
            # Retain every failed cleanup handle and the browser lease. Only an
            # explicit close retry may finish shutdown, never end(resume=True).
            self._closing = True
            self._revoke()
            self._closed = True

    def _revoke(self) -> None:
        self._stop_process()
        if self._cleanup_required:
            self._release_input()
            self._cleanup_required = False
        if self._lease is not None:
            self._browser.release_viewer_control(self._owner, self._session, self._lease)
            self._lease = None

    def _stop_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            # Retain the handle: a subsequent explicit cleanup can try again.
            raise LinuxViewerError(
                "Viewer revocation is unconfirmed; commands remain paused."
            ) from None
        self._process = None

    def _release_input(self) -> None:
        # Xlib itself can block if the display is unhealthy. Isolate and bound it
        # in a helper process instead of hanging the control coordinator.
        reason = "invalid helper result"
        stderr = None
        try:
            result = subprocess.run(
                [sys.executable, "-m", __name__, "--release-input", self._display],
                env={"PATH": os.defpath},
                capture_output=True,
                timeout=3,
                check=True,
            )
            stderr = getattr(result, "stderr", None)
            outcome = json.loads(result.stdout)
            if outcome.get("remaining_keys") != 0 or outcome.get("remaining_buttons") != 0:
                reason = "pressed input remains"
                raise ValueError(reason)
            return
        except subprocess.TimeoutExpired as error:
            reason = "helper timeout"
            stderr = error.stderr
        except subprocess.CalledProcessError as error:
            reason = f"helper exit {error.returncode}"
            stderr = error.stderr
        except OSError:
            reason = "helper launch failed"
        except (ValueError, TypeError, AttributeError):
            pass
        raise LinuxViewerError(
            f"Input cleanup is unconfirmed ({reason}); commands remain paused. "
            + _cleanup_diagnostics(stderr)
        ) from None


def _pressed_x11_buttons(x11: Any, display: Any) -> list[int]:
    """XInput2 includes extended buttons absent from the core five-bit mask."""

    class Buttons(ctypes.Structure):
        _fields_ = [("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte))]

    class Modifiers(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int) for name in ("base", "latched", "locked", "effective")]

    xi = ctypes.CDLL("libXi.so.6")
    pointer = ctypes.c_void_p
    xi.XIGetClientPointer.argtypes = [pointer, ctypes.c_ulong, pointer]
    xi.XIQueryPointer.argtypes = [pointer, ctypes.c_int, ctypes.c_ulong] + [pointer] * 9
    x11.XFree.argtypes = [pointer]
    device = ctypes.c_int()
    if not xi.XIGetClientPointer(display, 0, ctypes.byref(device)):
        raise LinuxViewerError("Master pointer unavailable.")
    root, child = ctypes.c_ulong(), ctypes.c_ulong()
    x, y, wx, wy = (ctypes.c_double() for _ in range(4))
    buttons, modifiers, group = Buttons(), Modifiers(), Modifiers()
    if not xi.XIQueryPointer(
        display,
        device.value,
        x11.XDefaultRootWindow(display),
        ctypes.byref(root),
        ctypes.byref(child),
        ctypes.byref(x),
        ctypes.byref(y),
        ctypes.byref(wx),
        ctypes.byref(wy),
        ctypes.byref(buttons),
        ctypes.byref(modifiers),
        ctypes.byref(group),
    ):
        raise LinuxViewerError("Extended pointer state unavailable.")
    try:
        if not 0 <= buttons.mask_len <= 256:
            raise LinuxViewerError("Unexpected pointer state size.")
        return [
            button
            for button in range(1, buttons.mask_len * 8)
            if buttons.mask[button // 8] & (1 << (button % 8))
        ]
    finally:
        x11.XFree(buttons.mask)


def _release_x11_input(display_name: str) -> dict[str, int]:
    """Release actual pressed keys and XInput2 master-pointer buttons."""
    _cleanup_phase("helper-entry")
    x11, xtst = ctypes.CDLL("libX11.so.6"), ctypes.CDLL("libXtst.so.6")
    pointer = ctypes.c_void_p
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], pointer
    x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [pointer], ctypes.c_ulong
    x11.XQueryKeymap.argtypes = [pointer, pointer]
    x11.XSync.argtypes, x11.XCloseDisplay.argtypes = [pointer, ctypes.c_int], [pointer]
    xtst.XTestFakeKeyEvent.argtypes = [pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    _cleanup_phase("display-open")
    display = x11.XOpenDisplay(display_name.encode())
    if not display:
        raise LinuxViewerError("Display unavailable.")

    def pressed() -> tuple[list[int], list[int]]:
        keys = (ctypes.c_ubyte * 32)()
        if not x11.XQueryKeymap(display, keys):
            raise LinuxViewerError("Keyboard state unavailable.")
        return (
            [code for code in range(8, 256) if keys[code // 8] & (1 << (code % 8))],
            _pressed_x11_buttons(x11, display),
        )

    try:
        _cleanup_phase("state-read")
        keys, buttons = pressed()
        _cleanup_phase("input-release")
        for code in keys:
            if not xtst.XTestFakeKeyEvent(display, code, 0, 0):
                raise LinuxViewerError("Keyboard release failed.")
        for button in buttons:
            if not xtst.XTestFakeButtonEvent(display, button, 0, 0):
                raise LinuxViewerError("Pointer release failed.")
        _cleanup_phase("input-sync")
        x11.XSync(display, 0)
        _cleanup_phase("state-verify")
        remaining_keys, remaining_buttons = pressed()
        if not remaining_keys and not remaining_buttons:
            _cleanup_phase("verified-clear")
        return {
            "released_keys": len(keys),
            "released_buttons": len(buttons),
            "remaining_keys": len(remaining_keys),
            "remaining_buttons": len(remaining_buttons),
        }
    finally:
        _cleanup_phase("display-close")
        x11.XCloseDisplay(display)
        _cleanup_phase("display-closed")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--release-input":
        raise SystemExit(2)
    print(json.dumps(_release_x11_input(sys.argv[2])))
