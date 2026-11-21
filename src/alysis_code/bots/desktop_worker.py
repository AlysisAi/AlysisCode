"""Standalone credential-free offline desktop worker. Private pipe protocol only.

This file is mounted alone in the child; it must not import the Bots service.
All returned page, pixel, file and command content is untrusted task data.
The shell has no network; in an owl computer whose bridge binds in its package-index
guard, package installers alone reach the declared package indexes through it.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from PIL import ImageGrab


def software_inventory():
    """Observe the actual image, without importing packages or reading credentials."""
    distributions = sorted(
        ((d.metadata.get("Name", ""), d.version) for d in importlib.metadata.distributions()),
        key=lambda item: item[0].casefold(),
    )
    items, used = [], 0
    for name, version in distributions:
        item = {"name": name, "version": version}
        size = len(json.dumps(item, ensure_ascii=False))
        if used + size > 12000:
            continue
        items.append(item)
        used += size
    return {
        "python": sys.version.split()[0],
        "python_distributions": items,
        "distribution_count": len(distributions),
        "complete": len(items) == len(distributions),
        "meaning": "Installed package metadata, not proof an import or document check succeeded. "
        "Distribution and import names may differ. Use these installed readers/writers where suitable; "
        "probe imports and verify actual outputs. If incomplete, enumerate importlib.metadata.distributions().",
    }


WIDTH, HEIGHT = 1280, 800
MAX_OUTPUT = 64000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024

# Window elements (the accessibility tree): read and acted on by desktop_accessibility.py, the
# file mounted beside this one, run as a short child in an application namespace. Declared in
# the ping reply only when this desktop's accessibility bus answers, so a host offers element
# tools only for a computer that has them.
ELEMENTS = "elements"
ACCESSIBILITY = Path(__file__).with_name("desktop_accessibility.py")
SESSION_BUS = Path("/usr/bin/dbus-daemon")
# The standard AT-SPI bus launcher, started on request by the session bus.
ACCESSIBILITY_BUS_SERVICE = Path("/usr/share/dbus-1/services/org.a11y.Bus.service")
# GTK 3 and 4 and LibreOffice publish their windows whenever the bus runs; Qt only when a desktop
# session asks for it, or with this switch.
ACCESSIBILITY_ENV = {"QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1"}
# Startup readiness must stay well inside the host's 15-second ping (desktop_linux.py).
ACCESSIBILITY_STARTUP_SECONDS = 10
READ_SECONDS = 8
ACT_SECONDS = 10
MAX_ACCESSIBILITY_REPLY = 512 * 1024
MAX_ACCESSIBILITY_REQUEST = 60_000
# Refusals the element reader may return (nothing was sent), passed on as they are.
ELEMENT_REFUSALS = frozenset(
    {
        "gone",
        "changed",
        "hidden",
        "disabled",
        "unresponsive",
        "no_position",
        "read_only",
        "not_toggle",
        "radio",
    }
)
# Reader failures before it sent anything to an application.
UNSENT = frozenset({"bus", "request", "operation"})

# What this desktop's own tools can do beyond the first images', declared in the ping reply beside
# ELEMENTS, so a host offers each only for a computer that has it (an older image declares none).
LONG_COMMANDS = "long_commands"  # commands up to COMMAND_SECONDS, waited for in short requests
PROCESS_OUTPUT = "process_output"  # a launched process's output, kept and read from an offset
POINTER = "pointer"  # double-click, right-click, hover and drag, at pixels or at an element
WINDOWS = "windows"  # the open windows, and bringing one to the front
CLIPBOARD = "clipboard"  # reading the clipboard's text
SHORT_COMMAND_SECONDS = 30  # the bound of ``run``, which answers only when its command ends
COMMAND_SECONDS = 300
# One request waits at most this long for a command's end, so the host checks Stop between them
# and every transport (a pipe, a VM's socket, a Cloud bridge) stays within its own deadline.
MAX_WAIT_SECONDS = 5
LOG_BYTES = 256 * 1024  # the latest output kept for each launched process
MAX_JOBS = 32  # launched processes remembered; the oldest finished one is forgotten first
CLIPBOARD_BYTES = 64000
CLIPBOARD_SECONDS = 3
POINTER_ACTIONS = frozenset({"double_click", "right_click", "hover", "drag"})
DOUBLE_CLICK_MS = 80  # well inside every toolkit's double-click interval
DRAG_STEPS = 10  # applications start a drag only once the pressed pointer moves
# Pointing shows what an application shows on hover (a tooltip, a submenu) after a delay it sets,
# commonly under a second: a hover lasts this long before its observation.
HOVER_SECONDS = 1.0
FOCUS_SECONDS = 1.0  # how long the window manager has to confirm a window came to the front
MAX_WINDOWS = 30
WINDOW_TITLE_CHARS = 300
XLIB = "libX11.so.6"
XCLIP = "/usr/bin/xclip"


class Rejected(Exception):
    pass


def accessibility_module():
    """desktop_accessibility.py, loaded from beside this file (the jail mounts both in /opt)."""
    module = sys.modules.get("_alysis_desktop_accessibility")
    if module is None:
        spec = importlib.util.spec_from_file_location(
            "_alysis_desktop_accessibility", ACCESSIBILITY
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["_alysis_desktop_accessibility"] = module
    return module


def accessibility_installed() -> bool:
    """The image ships what reading window elements needs: a session bus, the AT-SPI bus
    service, GLib's Python bindings, and the reader."""
    return (
        SESSION_BUS.is_file()
        and ACCESSIBILITY_BUS_SERVICE.is_file()
        and ACCESSIBILITY.is_file()
        and importlib.util.find_spec("gi") is not None
    )


def child_reply(process, payload: bytes | None, timeout: float) -> dict:
    """The JSON object a short child prints, read within ``timeout`` seconds and a byte bound.

    The child (started in its own session) is ended either way. A child that overran its time
    replies ``{"unavailable": "timeout"}``: it may have sent input before it was stopped.
    """
    output = bytearray()
    try:
        if process.stdin is not None:
            try:
                # Smaller than a pipe's buffer, so this write never waits for the child; closing
                # it ends the request (a child reading its request gets end of input).
                if payload:
                    process.stdin.write(payload)
                process.stdin.close()
            except OSError:
                pass  # The child already ended; its output says what happened.
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"unavailable": "timeout"}
            if select.select([process.stdout], [], [], min(remaining, 0.1))[0]:
                part = os.read(process.stdout.fileno(), 65536)
                if not part:
                    break
                output.extend(part)
                if len(output) > MAX_ACCESSIBILITY_REPLY:
                    return {"unavailable": "size"}
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # It ended on its own; the wait below reaps it.
        process.wait(timeout=3)
        process.stdout.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    try:
        reply = json.loads(output)
    except ValueError:
        return {"unavailable": "invalid"}
    return reply if isinstance(reply, dict) else {"unavailable": "invalid"}


def integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise Rejected("Invalid bounded integer argument.")
    return value


def whole_characters(data: bytes) -> int:
    """The length of ``data`` without a UTF-8 character cut off at its end."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 != 0x80:  # ASCII or the first byte of a character
            length = 1 if byte < 0x80 else 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return len(data) - back if length > back else len(data)
    return len(data)


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # It ended on its own; its owner reaps it.


class OutputLog:
    """A process's combined stdout and stderr, drained by a thread so it never waits on a full
    pipe. A command (``cap``) keeps its first ``cap`` bytes and is stopped once they fill or at
    its ``deadline``, as ``run`` stops one; a launched process (``ring``) keeps its latest
    ``ring`` bytes and runs on. Offsets count bytes of the whole output, kept or not."""

    def __init__(self, process, *, cap=None, ring=None, deadline=None):
        self.process, self.cap, self.ring, self.deadline = process, cap, ring, deadline
        self.data = bytearray()
        self.first = 0  # the offset of data[0]
        self.lock = threading.Lock()
        self.ended = threading.Event()  # all output read; for a command, the command ended too
        self.timed_out = self.truncated = False
        target = self._command if cap is not None else self._launched
        threading.Thread(target=target, name="alysis-process-output", daemon=True).start()

    def _keep(self, part) -> bool:
        """Keep ``part``; True when a command's output filled its cap."""
        with self.lock:
            if self.cap is not None:
                self.data.extend(part[: self.cap - len(self.data)])
                return len(self.data) >= self.cap
            self.data.extend(part)
            excess = len(self.data) - self.ring
            if excess > 0:
                del self.data[:excess]
                self.first += excess
            return False

    def _launched(self):
        stream = self.process.stdout
        try:
            while part := os.read(stream.fileno(), 65536):
                self._keep(part)
        finally:
            stream.close()
            self.ended.set()

    def _command(self):
        stream, reading = self.process.stdout, True
        try:
            while True:
                if time.monotonic() >= self.deadline:
                    self.timed_out = True
                    break
                if not reading:
                    # Its output closed; the command ends when its process does.
                    try:
                        self.process.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if not select.select([stream], [], [], 0.1)[0]:
                    continue
                part = os.read(stream.fileno(), 65536)
                if not part:
                    reading = False
                elif self._keep(part):
                    self.truncated = True
                    break
        finally:
            if self.process.poll() is None:
                kill_group(self.process)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass  # exit_code stays None: the receipt says it is unconfirmed
            stream.close()
            self.ended.set()

    def receipt(self) -> dict:
        """A finished command's receipt, the shape ``run`` returns."""
        with self.lock:
            output = bytes(self.data)
        if self.truncated:
            output = output[: whole_characters(output)]
        return {
            "exit_code": self.process.poll(),
            "output": output.decode("utf-8", errors="replace"),
            "timed_out": self.timed_out,
            "output_truncated": self.truncated,
            "untrusted_computer_data": True,
        }

    def read(self, offset: int, limit: int) -> dict:
        """At most ``limit`` bytes of output from ``offset``, as whole characters."""
        with self.lock:
            end = self.first + len(self.data)
            start = min(max(offset, self.first), end)
            chunk = bytes(self.data[start - self.first : start - self.first + limit])
            dropped = max(self.first - offset, 0)
            ended = self.ended.is_set()
        # A read whose start was dropped from the log begins at the next whole character.
        skip = 0
        while skip < min(3, len(chunk)) and chunk[skip] & 0xC0 == 0x80:
            skip += 1
        chunk = chunk[skip:]
        if start + skip + len(chunk) < end or not ended:
            chunk = chunk[: whole_characters(chunk)]  # the rest of a cut character comes next
        begin = start + skip
        reply = {
            "output": chunk.decode("utf-8", errors="replace"),
            "output_from": begin,
            "next_output_from": begin + len(chunk),
            "output_complete": ended and begin + len(chunk) == end,
        }
        if dropped:
            reply["output_dropped"] = dropped
        return reply


_X_ERRORS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
# A window may close while it is read; Xlib's default answer to that error ends the process.
_IGNORE_X_ERRORS = _X_ERRORS(lambda display, error: 0)


def _xlib():
    x11 = ctypes.CDLL(XLIB)
    pointer, ulong = ctypes.c_void_p, ctypes.c_ulong
    x11.XOpenDisplay.argtypes, x11.XOpenDisplay.restype = [ctypes.c_char_p], pointer
    x11.XDefaultRootWindow.argtypes, x11.XDefaultRootWindow.restype = [pointer], ulong
    x11.XInternAtom.argtypes = [pointer, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = ulong
    x11.XGetWindowProperty.argtypes = [
        pointer,
        ulong,
        ulong,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_int,
        ulong,
        ctypes.POINTER(ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ulong),
        ctypes.POINTER(ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    x11.XGetWindowProperty.restype = ctypes.c_int
    x11.XFree.argtypes = [pointer]
    x11.XSetErrorHandler.argtypes, x11.XSetErrorHandler.restype = [pointer], pointer
    x11.XSync.argtypes = [pointer, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [pointer]
    return x11


def _window_property(x11, display, window, atom, length):
    """(type, values) of one property: ints for 32-bit data, bytes for 8-bit; (0, None) when the
    window has none, (None, None) when the window is gone."""
    kind, size, count, after = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    status = x11.XGetWindowProperty(
        display,
        window,
        atom,
        0,
        length,
        0,
        0,  # AnyPropertyType
        ctypes.byref(kind),
        ctypes.byref(size),
        ctypes.byref(count),
        ctypes.byref(after),
        ctypes.byref(data),
    )
    if status != 0:
        return None, None
    try:
        if not data or not kind.value:
            return 0, None
        if size.value == 32:  # Xlib hands 32-bit items over as C longs
            return kind.value, list(
                ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[: count.value]
            )
        if size.value == 8:
            return kind.value, ctypes.string_at(data, count.value)
        return kind.value, None
    finally:
        if data:
            x11.XFree(data)


def open_windows(display_name: str, *, supported: bool = False) -> list[dict]:
    """The windows the window manager lists as open (its EWMH client list, what the dock shows),
    in its order: each one's X id, title as its application set it, class, and whether it is the
    active window. A window that closes while it is read is left out. ``supported``: first
    require the window manager to declare that it lists its windows and brings one to the
    front (EWMH _NET_SUPPORTED)."""
    x11 = _xlib()
    display = x11.XOpenDisplay(display_name.encode())
    if not display:
        raise Rejected("Private display unavailable.")
    previous = x11.XSetErrorHandler(ctypes.cast(_IGNORE_X_ERRORS, ctypes.c_void_p))
    try:

        def atom(name):
            return x11.XInternAtom(display, name.encode(), 0)

        def text(window, name):
            kind, value = _window_property(x11, display, window, atom(name), 4096)
            if not isinstance(value, bytes):
                return None
            # Titles are UTF-8 (EWMH), or Latin-1 in the ICCCM's plain STRING type.
            return value.decode("latin-1" if kind == atom("STRING") else "utf-8", "replace")

        root = x11.XDefaultRootWindow(display)
        if supported:
            _, declared = _window_property(x11, display, root, atom("_NET_SUPPORTED"), 4096)
            if not {atom("_NET_CLIENT_LIST"), atom("_NET_ACTIVE_WINDOW")} <= set(declared or ()):
                raise Rejected("The window manager does not list its windows.")
        _, clients = _window_property(x11, display, root, atom("_NET_CLIENT_LIST"), 4096)
        _, active = _window_property(x11, display, root, atom("_NET_ACTIVE_WINDOW"), 1)
        active = active[0] if active else 0
        hidden = {atom("_NET_WM_WINDOW_TYPE_DOCK"), atom("_NET_WM_WINDOW_TYPE_DESKTOP")}
        windows = []
        for window in clients or ():
            if len(windows) >= MAX_WINDOWS:
                break
            kind, types = _window_property(x11, display, window, atom("_NET_WM_WINDOW_TYPE"), 32)
            if kind is None:
                continue  # closed while it was read
            if hidden & set(types or ()):
                continue  # the dock and the desktop are not windows a person switches to
            title = text(window, "_NET_WM_NAME")
            if title is None:
                title = text(window, "WM_NAME") or ""
            _, names = _window_property(x11, display, window, atom("WM_CLASS"), 64)
            parts = names.split(b"\0") if isinstance(names, bytes) else []
            windows.append(
                {
                    "id": window,
                    "title": " ".join(title.split())[:WINDOW_TITLE_CHARS],
                    "class": parts[1].decode("utf-8", "replace")[:80] if len(parts) > 1 else "",
                    "active": window == active,
                }
            )
        return windows
    finally:
        x11.XSync(display, 0)  # errors of these reads reach the handler above, not the default
        x11.XCloseDisplay(display)
        x11.XSetErrorHandler(previous)


def workspace_file(operation, args):
    """Transfer one bounded regular file, without following workspace symlinks."""
    name = args.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 160
        or name in {".", ".."}
        or any(c in name for c in "/\\")
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise Rejected(
            "Use a simple workspace file name without directories or control characters."
        )
    if operation == "write_file":
        if "data_base64" in args:
            value = args["data_base64"]
            if "content" in args or not isinstance(value, str):
                raise Rejected("Invalid file payload.")
            try:
                data = base64.b64decode(value, validate=True)
            except ValueError:
                raise Rejected("Invalid file encoding.") from None
        else:
            content = args.get("content")
            if not isinstance(content, str):
                raise Rejected("Import requires a file payload.")
            data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise Rejected("File exceeds the 2 MiB transfer allowance.")
    directory = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        if operation == "write_file":
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            with os.fdopen(descriptor, "wb", closefd=False) as file:
                file.write(data)
                file.flush()
                os.fsync(descriptor)
            return {
                "name": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "written": True,
            }
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise Rejected("Export requires a regular file of at most 2 MiB.")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            data = file.read(MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(data) > MAX_FILE_BYTES or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Rejected(
                "The workspace file changed during export. Stop its writer before retrying."
            )
        payload = {"data_base64": base64.b64encode(data).decode("ascii")}
        if len(data) <= 100000:
            try:
                content = data.decode("utf-8")
                if "\0" not in content:
                    payload = {"content": content}
            except UnicodeDecodeError:
                pass
        return {
            "name": name,
            **payload,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    except OSError:
        raise Rejected(
            "Workspace transfer failed. Imports require a new name; exports require an existing regular file without symlinks."
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


# One quiet desktop with a compact dock for launchers and open windows. Cloud
# Chrome shares this display; standalone local desktops remain offline.
WALLPAPER = ("#11191c", "#304548")
DOCK = Path("/tmp/alysis-dock")
DOCK_APPS = (
    ("files", "Files", "folder", ["/usr/bin/pcmanfm", "/workspace"]),
    ("libreoffice", "LibreOffice", "libreoffice-startcenter", ["/usr/bin/libreoffice", "--nologo"]),
    (
        "terminal",
        "Terminal",
        "/usr/share/pixmaps/xterm-color_48x48.xpm",
        [
            "/usr/bin/xterm",
            # The viewer owns the clipboard and sends Ctrl+V. Configure our
            # terminal to consume it just like the desktop's other GUI apps.
            "-xrm",
            "XTerm*VT100.translations: #override Ctrl <Key>v: insert-selection(CLIPBOARD)",
            "-fa",
            "DejaVu Sans Mono",
            "-fs",
            "11",
            "-bg",
            "#ffffff",
            "-fg",
            "#1f2328",
        ],
    ),
)
TINT2_SETTINGS = """# Alysis owl computer bar
# 1: the bar
rounded = 12
border_width = 0
background_color = #151c20 100
border_color = #000000 0
# 2: the window that has focus, and any button under the pointer
rounded = 6
border_width = 0
background_color = #ffffff 16
border_color = #000000 0

panel_items = LTC
panel_size = 100% 48
panel_shrink = 1
panel_margin = 0 12
panel_padding = 12 6 8
panel_background_id = 1
panel_position = bottom center horizontal
panel_layer = top
autohide = 0
strut_policy = follow_size
panel_window_name = alysis-dock
mouse_effects = 1
mouse_hover_icon_asb = 100 0 12

launcher_padding = 2 0 8
launcher_background_id = 0
launcher_icon_background_id = 0
launcher_icon_size = 32
launcher_icon_theme = Adwaita
startup_notifications = 0
launcher_tooltip = 1
tooltip_show_timeout = 0.4
tooltip_hide_timeout = 0.1
tooltip_padding = 6 4
tooltip_background_id = 1
tooltip_font_color = #ffffff 100
tooltip_font = DejaVu Sans 10

taskbar_mode = single_desktop
taskbar_padding = 6 0 6
taskbar_background_id = 0
taskbar_active_background_id = 0
taskbar_name = 0
task_text = 0
task_icon = 1
task_centered = 0
task_tooltip = 1
task_maximum_size = 36 36
task_padding = 6 3 6
task_font = DejaVu Sans 9
task_font_color = #ffffff 80
task_active_font_color = #ffffff 100
task_icon_asb = 100 0 0
task_background_id = 0
task_active_background_id = 2
task_urgent_background_id = 2
task_iconified_background_id = 0
# A click brings a window forward; nothing on the bar hides or closes one (closing the browser's
# last window would end the owl's browser).
mouse_left = activate
mouse_middle = none
mouse_right = none
mouse_scroll_up = none
mouse_scroll_down = none

# Digits only, in any language; the zone name says whose time it is (the computer's, not the
# person's).
time1_format = %H:%M %Z
time1_font = DejaVu Sans 9
clock_font_color = #ffffff 85
clock_padding = 8 0
clock_background_id = 0
"""


def desktop_exec(argv):
    """Quote the dock argv, including colours that tint2 passes through a shell."""
    quoted = []
    for argument in argv:
        argument = argument.replace("%", "%%")
        if any(character in argument for character in ' #"\\'):
            argument = '"' + argument.replace("\\", "\\\\").replace('"', '\\"') + '"'
        quoted.append(argument)
    return " ".join(quoted)


def write_dock(directory=DOCK, available=os.path.exists, *, browser_request=None):
    """Write the dock's launchers (only for installed apps) and tint2 settings; return the settings."""
    directory.mkdir(parents=True, exist_ok=True)
    launchers = []
    apps = DOCK_APPS
    if browser_request is not None:
        apps = (
            ("chrome", "Google Chrome", "google-chrome", ["/usr/bin/touch", str(browser_request)]),
            *apps,
        )
    for name, title, icon, argv in apps:
        if not available(argv[0]):
            continue
        launcher = directory / f"{name}.desktop"
        launcher.write_text(
            "[Desktop Entry]\nType=Application\n"
            f"Name={title}\nIcon={icon}\nExec={desktop_exec(argv)}\nTerminal=false\n"
        )
        launchers.append(launcher)
    settings = directory / "tint2rc"
    settings.write_text(
        TINT2_SETTINGS + "".join(f"launcher_item_app = {launcher}\n" for launcher in launchers)
    )
    return settings


def application_argv(argv):
    # GUI launchers can execute task-controlled configuration too. Every such
    # application, including the window manager, must hide the controller's proc.
    return [
        "/usr/bin/bwrap",
        "--unshare-pid",
        "--unshare-ipc",
        "--die-with-parent",
        "--new-session",
        "--bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        # Task-controlled applications must not reach or replace the trusted
        # worker's screen socket. Their /proc also excludes the outer worker.
        "--tmpfs",
        "/run/alysis-screen",
        "--",
        *argv,
    ]


# An owl computer's package-index guard (package_access.py), bound read-only here by the
# computer's bridge: its Unix socket and the package indexes it admits. Older images and
# standalone desktops have none, and their shell stays fully offline.
PACKAGES = Path("/run/alysis-packages")
# Installed packages live in this computer's private home: never the image, the workspace or
# the person's machine.
PACKAGE_HOME = Path("/tmp/home/.alysis-packages")
RELAY_CONNECTIONS = 32
RELAY_IDLE_SECONDS = 120


def pump(first, second):
    """Copy bytes both ways until both sides finish or the link goes idle."""
    other = {first: second, second: first}
    reading = [first, second]
    while reading:
        ready, _, _ = select.select(reading, [], [], RELAY_IDLE_SECONDS)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if data:
                other[source].sendall(data)
                continue
            reading.remove(source)
            try:
                other[source].shutdown(socket.SHUT_WR)
            except OSError:
                pass


class PackageRelay:
    """A loopback port inside this networkless namespace. Each connection is passed to the
    guard's socket unchanged; the guard alone decides where it may lead."""

    def __init__(self, guard_socket):
        self.guard_socket = str(guard_socket)
        self.slots = threading.BoundedSemaphore(RELAY_CONNECTIONS)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(RELAY_CONNECTIONS)
        except OSError:
            self.listener.close()
            raise
        self.url = "http://127.0.0.1:" + str(self.listener.getsockname()[1])
        threading.Thread(target=self._accept, name="alysis-package-relay", daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client):
        try:
            with client, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as guard:
                guard.settimeout(5)
                guard.connect(self.guard_socket)
                client.settimeout(RELAY_IDLE_SECONDS)
                guard.settimeout(RELAY_IDLE_SECONDS)
                pump(client, guard)
        except OSError:
            pass
        finally:
            self.slots.release()

    def close(self):
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()


def python_installer(env, index_url, proxy, home):
    """pip, installing into a private environment layered over this computer's own Python
    packages: installed packages take precedence, the image's are never changed, and what is
    already installed is reused. Needs pip in this computer's Python."""
    import importlib.util

    if importlib.util.find_spec("pip") is None:
        return False
    import sysconfig
    import venv

    overlay = home / "python"
    venv.EnvBuilder(symlinks=True, with_pip=False).create(overlay)
    keys = ("base", "platbase", "installed_base", "installed_platbase")
    paths = sysconfig.get_paths("venv", vars={key: str(overlay) for key in keys})
    own = sysconfig.get_paths()
    Path(paths["purelib"], "_alysis_computer_packages.pth").write_text(
        "".join(
            f"import site; site.addsitedir({path!r})\n"
            for path in dict.fromkeys((own["purelib"], own["platlib"]))
        )
    )
    python = Path(paths["scripts"], "python")
    for name in ("pip", "pip3"):
        script = Path(paths["scripts"], name)
        script.write_text(
            f"#!{python}\nimport sys\nfrom pip._internal.cli.main import main\nsys.exit(main())\n"
        )
        script.chmod(0o755)
    env.update(
        PIP_INDEX_URL=index_url,
        PIP_PROXY=proxy,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_INPUT="1",
        PIP_PROGRESS_BAR="off",
        # The home is memory-backed: keep one copy of each package, not a second in a cache.
        PIP_NO_CACHE_DIR="1",
    )
    env["PATH"] = paths["scripts"] + os.pathsep + env["PATH"]
    return True


def node_installer(env, index_url, proxy, home):
    """npm, with its registry reached through the guard and global installs in the private
    home. Needs npm in this computer."""
    if shutil.which("npm", path=env["PATH"]) is None:
        return False
    prefix = home / "node"
    env.update(
        npm_config_registry=index_url,
        npm_config_proxy=proxy,
        npm_config_https_proxy=proxy,
        npm_config_prefix=str(prefix),
        npm_config_update_notifier="false",
        npm_config_fund="false",
        NODE_PATH=str(prefix / "lib" / "node_modules"),
    )
    env["PATH"] = str(prefix / "bin") + os.pathsep + env["PATH"]
    return True


# How each package ecosystem's installer is pointed at its index; the indexes themselves, and
# so what is reachable, are the guard's declared configuration.
INSTALLERS = {"python": python_installer, "node": node_installer}


def package_installs(env, *, root=PACKAGES, home=PACKAGE_HOME, installers=None, relay=None):
    """Point the installers present here at the package indexes this computer's guard admits.
    Returns the declared ecosystems and the relay; nothing without a guard."""
    try:
        declared = json.loads((root / "indexes.json").read_text(encoding="utf-8"))["indexes"]
    except (OSError, ValueError, KeyError, TypeError):
        return (), None
    guard = root / "proxy.sock"
    if not isinstance(declared, dict) or not guard.is_socket():
        return (), None
    installers = INSTALLERS if installers is None else installers
    link = (relay or PackageRelay)(guard)
    names = []
    for name in sorted(declared):
        index = declared[name]
        install = installers.get(name)
        if install is None or not isinstance(index, dict):
            continue
        if not isinstance(index.get("index_url"), str):
            continue
        try:
            if install(env, index["index_url"], link.url, home):
                names.append(name)
        except (OSError, ValueError):
            continue  # This ecosystem stays undeclared; the others still work.
    if not names:
        link.close()
        return (), None
    return tuple(names), link


class Computer:
    # Package ecosystems whose installers reach their index through the guard (none offline).
    package_indexes = ()

    def __init__(self, shared_display=None):
        started = time.monotonic()
        self.shared_display = shared_display
        self.processes = []
        self.jobs = {}
        self.logs = {}
        self.command = None
        self.features = ()
        self.services = []
        self.frame = None
        self._check = None
        self._check_pool = ThreadPoolExecutor(max_workers=1)
        self.env = {
            "PATH": "/opt/alysis/.venv/bin:/usr/bin:/bin",
            "HOME": "/tmp/home",
            "LANG": "C.UTF-8",
        }
        # Before any application starts, so commands and dock apps share the same settings.
        self.package_indexes, self.package_relay = package_installs(self.env)
        if shared_display is not None:
            import re

            if not re.fullmatch(r":[0-9]{1,5}", shared_display):
                raise Rejected("Invalid owned display.")
            self.display = shared_display
            self.env["DISPLAY"] = shared_display
        else:
            read_fd, write_fd = os.pipe()
            try:
                display = subprocess.Popen(
                    [
                        "/usr/bin/Xvfb",
                        "-displayfd",
                        str(write_fd),
                        "-screen",
                        "0",
                        f"{WIDTH}x{HEIGHT}x24",
                        "-nolisten",
                        "tcp",
                    ],
                    env=self.env,
                    pass_fds=(write_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.processes.append(display)
                os.close(write_fd)
                write_fd = -1
                # Xvfb may write the number and its newline separately. Closing
                # the pipe after the first read can give it EPIPE and kill a
                # display that appeared ready. Keep the read end until the full
                # bounded startup record arrives (also on a single-core VM).
                deadline = time.monotonic() + 8
                data = b""
                while b"\n" not in data:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([read_fd], [], [], remaining)[0]:
                        raise Rejected("Private display startup timed out.")
                    part = os.read(read_fd, 32)
                    if not part or len(data) + len(part) > 32:
                        raise Rejected("Private display did not start.")
                    data += part
                number = data.strip().decode("ascii")
                if not number.isdigit() or len(number) > 5:
                    raise Rejected("Private display did not start.")
                self.display = ":" + number
                self.env["DISPLAY"] = self.display
            finally:
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)
        self.processes.append(
            subprocess.Popen(
                application_argv(["/usr/bin/openbox", "--sm-disable"]),
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        # Starting the process is not window-manager readiness. A client that
        # maps during Openbox startup can be left unmapped indefinitely. Wait
        # for its actual EWMH desktop property before admitting applications.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in self.processes):
                raise Rejected("Private desktop infrastructure exited during startup.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = subprocess.run(
                    ["/usr/bin/xdotool", "get_desktop"],
                    env=self.env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=min(1, remaining),
                )
            except subprocess.TimeoutExpired:
                # This is a read-only readiness probe, not an agent action.
                # A busy single-core VM can delay it past one probe's budget.
                continue
            if ready.returncode == 0 and ready.stdout.strip().isdigit():
                break
            time.sleep(0.05)
        if time.monotonic() >= deadline:
            raise Rejected("Private window manager did not become ready.")
        self.features = self._hands()
        self.decorations = []
        # Before the dock, so every application opened from it or for a task inherits it; within
        # a startup budget, so a slow computer starts as before, without window elements.
        deadline = started + ACCESSIBILITY_STARTUP_SECONDS
        probe = self._start_accessibility(deadline)
        self._decorate()
        if probe is not None:
            remaining = max(deadline - time.monotonic(), 0.05)
            if child_reply(probe, None, remaining).get("ready") is True:
                self.features = (*self.features, ELEMENTS)

    def _hands(self) -> tuple[str, ...]:
        """What this desktop's own tools can do beyond the first images' (no service needed):
        xdotool and the worker's process logs always; the clipboard with xclip; the window list
        once the window manager lists its windows."""
        features = [LONG_COMMANDS, PROCESS_OUTPUT, POINTER]
        if os.path.exists(XCLIP):
            features.append(CLIPBOARD)
        try:
            open_windows(self.display, supported=True)
        except (OSError, Rejected):
            pass  # This desktop keeps today's tools for its windows.
        else:
            features.append(WINDOWS)
        return tuple(features)

    def _start_accessibility(self, deadline):
        """Start this desktop's accessibility bus when the image ships it, and return the
        probe that confirms it answers (None without it: the desktop is exactly as before).

        A private session bus, run like any application, starts the standard AT-SPI bus
        launcher and registry when the probe first asks for them. Applications opened later
        publish their windows' elements there, whatever their toolkit or language.
        """
        if not accessibility_installed() or time.monotonic() >= deadline:
            return None
        try:
            bus = subprocess.Popen(
                application_argv(
                    [str(SESSION_BUS), "--session", "--nofork", "--nopidfile", "--print-address=1"]
                ),
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return None
        self.services.append(bus)
        address = b""
        try:
            # The bus prints its address once it listens: that is its readiness.
            deadline = min(deadline, time.monotonic() + 2)
            while b"\n" not in address and len(address) <= 1024:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([bus.stdout], [], [], remaining)[0]:
                    break
                part = os.read(bus.stdout.fileno(), 1025)
                if not part:
                    break
                address += part
        finally:
            bus.stdout.close()
        address = address.split(b"\n", 1)[0].decode("ascii", "replace")
        if (
            not address.startswith("unix:")
            or len(address) > 1024
            or not address.isprintable()
            or any(character.isspace() for character in address)
        ):
            bus.kill()
            return None
        self.env.update(DBUS_SESSION_BUS_ADDRESS=address, **ACCESSIBILITY_ENV)
        try:
            return self._reader("probe")
        except OSError:
            return None

    def _reader(self, operation):
        return subprocess.Popen(
            application_argv([sys.executable, "-I", str(ACCESSIBILITY), operation]),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def accessibility(self, operation, request=None, *, timeout=READ_SECONDS):
        """One run of the element reader; its reply, or why there is none."""
        payload = json.dumps(request or {}, ensure_ascii=False).encode()
        if len(payload) > MAX_ACCESSIBILITY_REQUEST:
            raise Rejected("The element request is too large.")
        try:
            process = self._reader(operation)
        except OSError:
            return {"unavailable": "bus"}
        return child_reply(process, payload, timeout)

    def with_elements(self, observation, args):
        """An observation with the focused window's elements, when the host asked for them; and
        without its image when the host's model reads text only (the frame keeps it)."""
        if args.get("elements") is True:
            reply = (
                self.accessibility("read")
                if ELEMENTS in getattr(self, "features", ())
                else {"unavailable": "unsupported"}
            )
            if isinstance(reply.get("nodes"), list):
                observation["elements"] = reply
            else:
                reason = reply.get("unavailable") or reply.get("refused")
                observation["elements_unavailable"] = (
                    reason if isinstance(reason, str) and len(reason) <= 20 else "failed"
                )
        if args.get("windows") is True:
            try:
                observation["windows"] = self.windows()
            except (OSError, Rejected):
                observation["windows_unavailable"] = True
        if args.get("image") is False:
            observation.pop("png", None)
        return observation

    def element(self, args):
        """Activate, set the text of, select or toggle one element of the element view."""
        if ELEMENTS not in getattr(self, "features", ()):
            raise Rejected("This desktop cannot act on window elements.")
        module = accessibility_module()
        target = args.get("element")
        try:
            bus, path = module.element_identity(target)
        except ValueError:
            raise Rejected("Invalid element.") from None
        role, action = target.get("role"), args.get("action")
        if not isinstance(role, str) or not 1 <= len(role) <= 60 or action not in module.ACTIONS:
            raise Rejected("Invalid element action.")
        request = {"element": {"bus": bus, "path": path, "role": role}, "action": action}
        if action == "set_text":
            text = args.get("text")
            if not isinstance(text, str) or len(text) > module.MAX_SET_TEXT or "\0" in text:
                raise Rejected("Invalid bounded text.")
            request["text"] = text
        if action == "toggle":
            if type(args.get("checked")) is not bool:
                raise Rejected("Toggle needs the state to reach.")
            request["checked"] = args["checked"]
        reply = self.accessibility("act", request, timeout=ACT_SECONDS)
        refused = reply.get("refused")
        if refused is not None or reply.get("unavailable") in UNSENT:
            return {
                "dispatched": False,
                "refused": refused if refused in ELEMENT_REFUSALS else "unavailable",
            }
        if "unavailable" in reply:
            # The reader stopped part-way: an action may have reached the application.
            result = {"dispatched": None, "uncertain": True, "verification_required": True}
        else:
            result = self._element_input(reply, request)
            if isinstance(result.get("refused"), str):
                return result
        try:
            result["observation"] = self.with_elements(
                self.observe(), {"elements": True, "image": args.get("image") is not False}
            )
        except Exception:
            result["observation_unavailable"] = True
        return result

    def _element_input(self, reply, request):
        """What the reader did, and the pointer click or typing it left for this worker."""
        pointer = reply.get("pointer")
        if isinstance(pointer, dict):
            try:
                x = integer(pointer.get("x"), 0, WIDTH - 1)
                y = integer(pointer.get("y"), 0, HEIGHT - 1)
            except Rejected:
                return {"dispatched": False, "refused": "no_position"}
        self.frame = None
        if reply.get("unchanged") is True:
            result = {"dispatched": False, "unchanged": True, "verification_required": True}
        else:
            method = "pointer" if isinstance(pointer, dict) else "accessibility"
            if reply.get("focused") is True:
                method = "focus"
            result = {"dispatched": True, "method": method, "verification_required": True}
        if isinstance(pointer, dict):
            self.move_pointer(x, y)
            try:
                self.xdo("mousedown", "1")
            finally:
                self.xdo("mouseup", "1")
        if reply.get("then") == "paste":
            if request["text"]:
                self.paste_text(request["text"])
            result["typed_at_cursor"] = True
        for key in ("confirmed", "checked", "selected", "value_matches"):
            if type(reply.get(key)) is bool:
                result[key] = reply[key]
        if reply.get("then") == "verify":
            after = self.accessibility("state", {"element": request["element"]})
            for key in ("checked", "selected"):
                if type(after.get(key)) is bool:
                    result[key] = after[key]
        return result

    def _decorate(self):
        quiet = {
            "env": self.env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        try:
            if os.path.exists("/usr/bin/hsetroot"):
                wallpaper = ["/usr/bin/hsetroot", "-add", WALLPAPER[0], "-add", WALLPAPER[1]]
                subprocess.run([*wallpaper, "-gradient", "160"], timeout=5, **quiet)
            else:
                subprocess.run(["/usr/bin/xsetroot", "-solid", "#d6d9de"], timeout=5, **quiet)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not os.path.exists("/usr/bin/tint2"):
            return
        try:
            Path(self.env["HOME"]).mkdir(parents=True, exist_ok=True)
            settings = write_dock(
                browser_request=Path("/run/alysis-launchers/open-browser")
                if self.shared_display
                else None
            )
            self.decorations.append(
                subprocess.Popen(application_argv(["/usr/bin/tint2", "-c", str(settings)]), **quiet)
            )
        except OSError:
            pass

    def capture(self):
        with ImageGrab.grab(xdisplay=self.display) as pixels:
            if pixels.size != (WIDTH, HEIGHT):
                raise Rejected("Unexpected desktop dimensions.")
            output = io.BytesIO()
            pixels.save(output, format="PNG")
        png = output.getvalue()
        if len(png) > 1024 * 1024:
            raise Rejected("Desktop frame exceeded its image allowance.")
        return png

    def observe(self):
        png = self.capture()
        # Native input can return before the application paints focus/text.
        # Prefer two identical samples, but stay bounded for animated screens.
        # The returned pixels are always an actual capture, not a blended frame.
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            time.sleep(0.075)
            current = self.capture()
            if current == png:
                break
            png = current
        frame_id = uuid4().hex
        self.frame = (frame_id, time.monotonic(), png)
        return {
            "frame_id": frame_id,
            "width": WIDTH,
            "height": HEIGHT,
            "png": base64.b64encode(png).decode(),
            "network": "browser-mediated" if self.shared_display else "offline",
            "untrusted_computer_data": True,
        }

    def fresh(self, frame_id):
        frame, self.frame = self.frame, None
        if not frame or frame[0] != frame_id or time.monotonic() - frame[1] > 60:
            raise Rejected("Desktop frame expired or was used; observe again. No input was sent.")
        # A blinking caret can return to the exact observed image. Wait briefly
        # for that exact state, never ignore changed pixels or extend frame age.
        deadline = min(frame[1] + 60, time.monotonic() + 1.2)
        while True:
            current = self.capture()
            if time.monotonic() > deadline:
                raise Rejected("Desktop pixels changed; observe again. No input was sent.")
            if current == frame[2]:
                return
            time.sleep(0.05)

    def xdo(self, *args, input=None):
        subprocess.run(
            ["/usr/bin/xdotool", *args],
            env=self.env,
            input=input,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )

    def move_pointer(self, x, y):
        # mousemove --sync waits for movement and hangs when already at (x, y).
        # Query the pointer on that same X connection instead: the round trip
        # confirms the preceding move was processed, including an unchanged point.
        reply = subprocess.run(
            ["/usr/bin/xdotool", "mousemove", str(x), str(y), "getmouselocation", "--shell"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        ).stdout.decode("ascii")
        position = dict(line.split("=", 1) for line in reply.splitlines() if "=" in line)
        if position.get("X") != str(x) or position.get("Y") != str(y):
            raise Rejected("Pointer target was not confirmed; no click or scroll was sent.")

    def paste_text(self, text):
        # Keycode synthesis drops characters outside the keyboard layout. Use
        # UTF-8 on this private display's CLIPBOARD selection and standard paste.
        # Applications still decide how to handle it; inspect the actual result.
        subprocess.run(
            ["/usr/bin/xclip", "-selection", "clipboard", "-in", "-target", "UTF8_STRING"],
            input=text.encode("utf-8"),
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        try:
            self.xdo("keydown", "Control_L+v")
        finally:
            self.xdo("keyup", "v+Control_L")

    def run(self, args, *, background=False):
        command = args.get("command")
        if not isinstance(command, str) or not 1 <= len(command) <= 8192 or "\0" in command:
            raise Rejected("Invalid bounded command.")
        timeout = integer(args.get("timeout", 10), 1, SHORT_COMMAND_SECONDS)
        if background and sum(p.poll() is None for p in self.jobs.values()) >= 4:
            raise Rejected("This computer already has four running applications.")
        self.frame = None
        # A launched process's output is kept (PROCESS_OUTPUT), drained so it never blocks.
        process = self._start(command, output=True)
        if background:
            process_id = uuid4().hex
            self._forget_finished()
            self.jobs[process_id] = process
            self.logs[process_id] = OutputLog(process, ring=LOG_BYTES)
            return {
                "process_id": process_id,
                "started": True,
                "exit_code": process.poll(),
                "verification_required": True,
                **self.network_receipt(),
            }
        output = bytearray()
        deadline = time.monotonic() + timeout
        timed_out = truncated = False
        try:
            while True:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if select.select([process.stdout], [], [], 0.05)[0]:
                    part = os.read(process.stdout.fileno(), 8192)
                    if not part:
                        break
                    output.extend(part[: MAX_OUTPUT - len(output)])
                    if len(output) >= MAX_OUTPUT:
                        truncated = True
                        break
            if timed_out or truncated:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The bounded wait below must still confirm child exit.
            process.wait(timeout=3)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            process.stdout.close()
        return {
            "exit_code": process.returncode,
            "output": output.decode("utf-8", errors="replace"),
            "timed_out": timed_out,
            "output_truncated": truncated,
            "untrusted_computer_data": True,
            **self.network_receipt(),
        }

    def _start(self, command, *, output):
        # A second PID namespace prevents a task command from reaching the
        # controller's /proc/<pid>/fd/1 or signalling the pipe protocol worker.
        # The shared filesystem here is already the minimal offline outer jail.
        return subprocess.Popen(
            application_argv(["/bin/sh", "-c", command]),
            env=self.env,
            cwd="/workspace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if output else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    def _forget_finished(self):
        """Keep at most MAX_JOBS launched processes: the oldest finished ones go first (at most
        four run at once, so one has always finished)."""
        logs = getattr(self, "logs", None)
        if logs is None:
            logs = self.logs = {}
        for process_id, process in list(self.jobs.items()):
            if len(self.jobs) < MAX_JOBS:
                return
            if process.poll() is not None:
                del self.jobs[process_id]
                logs.pop(process_id, None)

    def command_start(self, args):
        """Start a command of up to COMMAND_SECONDS and wait, at most ``wait`` seconds, for its
        end: its receipt then, else its id for ``command_wait``. The command is stopped at its
        time limit, or once its output fills MAX_OUTPUT, here whether or not anyone waits."""
        command = args.get("command")
        if not isinstance(command, str) or not 1 <= len(command) <= 8192 or "\0" in command:
            raise Rejected("Invalid bounded command.")
        timeout = integer(args.get("timeout", 10), 1, COMMAND_SECONDS)
        wait = integer(args.get("wait", 0), 0, MAX_WAIT_SECONDS)
        earlier = getattr(self, "command", None)
        if earlier is not None and not earlier[1].ended.is_set():
            # The host waits for each command before its next request, so a command still
            # running here was abandoned by a wait that failed: it is stopped, not left to run.
            kill_group(earlier[1].process)
            earlier[1].ended.wait(5)
        self.frame = None
        log = OutputLog(
            self._start(command, output=True),
            cap=MAX_OUTPUT,
            deadline=time.monotonic() + timeout,
        )
        self.command = (uuid4().hex, log)
        return self._command_reply(wait)

    def command_wait(self, args):
        current = getattr(self, "command", None)
        if current is None or args.get("command_id") != current[0]:
            raise Rejected("Command not found in this computer.")
        return self._command_reply(integer(args.get("wait", 0), 0, MAX_WAIT_SECONDS))

    def _command_reply(self, wait):
        identity, log = self.command
        if not log.ended.wait(wait):
            return {"command_id": identity, "running": True}
        self.command = None
        return {**log.receipt(), "running": False, **self.network_receipt()}

    def status(self, args):
        process = self.jobs.get(args.get("process_id"))
        if process is None:
            raise Rejected("Application not found in this computer.")
        code = process.poll()
        result = {"exit_code": code, "running": code is None}
        if "output_from" in args:
            offset = integer(args["output_from"], 0, 2**53)
            log = getattr(self, "logs", {}).get(args["process_id"])
            if log is None:
                raise Rejected("This process's output was not kept.")
            result.update(log.read(offset, MAX_OUTPUT), untrusted_computer_data=True)
        return result

    def windows(self):
        if WINDOWS not in getattr(self, "features", ()):
            raise Rejected("This desktop cannot list its windows.")
        return open_windows(self.display)

    def focus_window(self, args):
        """Bring one open window to the front, as a click on it in the dock does."""
        window = integer(args.get("window"), 1, 0xFFFFFFFF)
        if window not in {item["id"] for item in self.windows()}:
            return {"dispatched": False, "refused": "gone"}
        self.frame = None
        try:
            # The window manager raises it, gives it the keyboard and restores it if minimised.
            self.xdo("windowactivate", str(window))
        except subprocess.CalledProcessError:
            return {"dispatched": False, "refused": "unavailable"}
        deadline = time.monotonic() + FOCUS_SECONDS
        confirmed = False
        while not confirmed and time.monotonic() < deadline:
            time.sleep(0.05)
            confirmed = any(item["id"] == window and item["active"] for item in self.windows())
        result = {"dispatched": True, "confirmed": confirmed, "verification_required": True}
        try:
            result["observation"] = self.with_elements(self.observe(), args)
        except Exception:
            result["observation_unavailable"] = True
        return result

    def _aim(self, target):
        """A pointer target: ("pixel", (x, y)) or ("element", the element to locate)."""
        if not isinstance(target, dict):
            raise Rejected("Invalid pointer target.")
        if set(target) == {"x", "y"}:
            return "pixel", (
                integer(target["x"], 0, WIDTH - 1),
                integer(target["y"], 0, HEIGHT - 1),
            )
        if set(target) != {"element"}:
            raise Rejected("Invalid pointer target.")
        if ELEMENTS not in getattr(self, "features", ()):
            raise Rejected("This desktop cannot act on window elements.")
        element = target["element"]
        try:
            bus, path = accessibility_module().element_identity(element)
        except ValueError:
            raise Rejected("Invalid element.") from None
        role = element.get("role")
        if not isinstance(role, str) or not 1 <= len(role) <= 60:
            raise Rejected("Invalid element.")
        return "element", {"bus": bus, "path": path, "role": role}

    def pointer(self, args):
        """Double-click, right-click, hover or drag at pixels of the observed frame or at the
        centre of an element, as a person does with a mouse."""
        action = args.get("action")
        if action not in POINTER_ACTIONS:
            raise Rejected("Invalid pointer action.")
        targets = [self._aim(args.get("at"))]
        if action == "drag":
            targets.append(self._aim(args.get("to")))
        elif args.get("to") is not None:
            raise Rejected("Only a drag has an end point.")
        points = []
        for kind, value in targets:
            if kind == "pixel":
                points.append(value)
                continue
            # Where the element is now; nothing is sent when it can't be found.
            reply = self.accessibility("locate", {"element": value}, timeout=ACT_SECONDS)
            position = reply.get("pointer")
            if not isinstance(position, dict):
                refused = reply.get("refused")
                return {
                    "dispatched": False,
                    "refused": refused if refused in ELEMENT_REFUSALS else "unavailable",
                }
            try:
                points.append(
                    (
                        integer(position.get("x"), 0, WIDTH - 1),
                        integer(position.get("y"), 0, HEIGHT - 1),
                    )
                )
            except Rejected:
                return {"dispatched": False, "refused": "no_position"}
        if any(kind == "pixel" for kind, _ in targets):
            self.fresh(args.get("frame_id"))
        self.frame = None
        x, y = points[0]
        if action == "drag":
            self.drag(points[0], points[1])
        else:
            self.move_pointer(x, y)
            if action == "double_click":
                self.xdo("click", "--repeat", "2", "--delay", str(DOUBLE_CLICK_MS), "1")
            elif action == "right_click":
                self.xdo("click", "3")
            else:
                time.sleep(HOVER_SECONDS)
        result = {"dispatched": True, "verification_required": True}
        try:
            result["observation"] = self.with_elements(self.observe(), args)
        except Exception:
            result["observation_unavailable"] = True
        return result

    def drag(self, start, end):
        """Press at ``start``, move to ``end`` in steps and release there; the button is
        released whatever fails."""
        (x0, y0), (x1, y1) = start, end
        self.move_pointer(x0, y0)
        try:
            self.xdo("mousedown", "1")
            for step in range(1, DRAG_STEPS):
                self.xdo(
                    "mousemove",
                    str(x0 + (x1 - x0) * step // DRAG_STEPS),
                    str(y0 + (y1 - y0) * step // DRAG_STEPS),
                )
            self.move_pointer(x1, y1)
        finally:
            self.xdo("mouseup", "1")

    def read_clipboard(self):
        """The text on this computer's clipboard, at most CLIPBOARD_BYTES of it."""
        helper = getattr(getattr(self, "_human_input", None), "_clipboard", None)
        if helper is not None and helper.poll() is None:
            # The clipboard serves what the person typed while in control: never read it.
            return {"text": None, "private": True}
        # Text as UTF-8, else as the older Latin-1 STRING an application may offer alone.
        data, encoding = self._selection("UTF8_STRING"), "utf-8"
        if data is None:
            data, encoding = self._selection("STRING"), "latin-1"
        if data is None:
            return {"text": None, "empty": True}
        truncated = len(data) > CLIPBOARD_BYTES
        if truncated:
            data = data[:CLIPBOARD_BYTES]
            if encoding == "utf-8":
                data = data[: whole_characters(data)]
        return {
            "text": data.decode(encoding, errors="replace"),
            "truncated": truncated,
            "untrusted_computer_data": True,
        }

    def _selection(self, target):
        """The clipboard's content in one format, or None when it holds none in it. Bounded in
        bytes and time: the application that holds the clipboard answers, or is not waited on."""
        process = subprocess.Popen(
            [XCLIP, "-selection", "clipboard", "-out", "-target", target],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        data = bytearray()
        deadline = time.monotonic() + CLIPBOARD_SECONDS
        late = False
        try:
            while len(data) <= CLIPBOARD_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    late = True
                    break
                part = os.read(process.stdout.fileno(), 65536)
                if not part:
                    break
                data.extend(part)
        finally:
            if process.poll() is None:
                kill_group(process)
            process.wait(timeout=3)
            process.stdout.close()
        if late and not data:
            raise Rejected(
                "The application holding the clipboard did not answer; nothing was read."
            )
        if process.returncode != 0 and not data:
            return None
        return bytes(data)

    def network_receipt(self):
        receipt = {"network": "browser-mediated" if self.shared_display else "offline"}
        if self.package_indexes:
            # Declared only when this computer's guard and the installer are both present.
            receipt["package_indexes"] = list(self.package_indexes)
        return receipt

    def dispatch(self, operation, args):
        if not isinstance(args, dict):
            raise Rejected("Computer arguments must be an object.")
        if operation == "human_input":
            from human_input import HumanInput

            self.frame = None
            if not hasattr(self, "_human_input"):
                self._human_input = HumanInput(self.env["DISPLAY"])
            return self._human_input(args)
        if operation == "ping":
            # features: what this desktop can do beyond the first images' (ELEMENTS and its
            # own tools' newer operations, Computer._hands).
            features = list(getattr(self, "features", ()))
            return {"ready": True, "network": "offline", "features": features}
        if operation == "capture":
            return {"png": base64.b64encode(self.capture()).decode()}
        if operation == "observe":
            return self.with_elements(self.observe(), args)
        if operation == "element":
            return self.element(args)
        if operation in {"write_file", "read_file"}:
            return workspace_file(operation, args)
        if operation == "check_file_start":
            from alysis_code.bots.artifact_validation import check_file

            if self._check is not None:
                raise Rejected("A file check is already pending.")
            identity = uuid4().hex
            self._check = (
                identity,
                self._check_pool.submit(check_file, Path("/workspace"), args["name"]),
            )
            return {"check_id": identity}
        if operation == "check_file_result":
            if self._check is None or self._check[0] != args["check_id"]:
                raise Rejected("File check not found in this computer.")
            future = self._check[1]
            if not future.done():
                return {"running": True}
            self._check = None
            try:
                return {"running": False, "report": future.result()}
            except (ValueError, OSError, ImportError, RuntimeError) as exc:
                raise Rejected(str(exc)) from None
        if operation in {"run", "launch", "command_start", "command_wait"}:
            if operation == "command_start":
                result = self.command_start(args)
            elif operation == "command_wait":
                result = self.command_wait(args)
            else:
                result = self.run(args, background=operation == "launch")
            if result.get("running") is not True and not getattr(self, "_software_reported", False):
                # Both local and hosted bridges forward this actual worker
                # receipt. Do not substitute the controller's installed packages.
                result["software"] = software_inventory()
                self._software_reported = True
            return result
        if operation == "stop_process":
            process = self.jobs.get(args.get("process_id"))
            if process is None:
                raise Rejected("Application not found in this computer.")
            self.frame = None
            requested = False
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    requested = True
                except ProcessLookupError:
                    pass  # Independently confirm exit; never signal an arbitrary PID.
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # No destructive escalation while other work is ongoing.
            code = process.poll()
            return {
                "termination_requested": requested,
                "stopped": code is not None,
                "running": code is None,
                "exit_code": code,
            }
        if operation == "status":
            return self.status(args)
        if operation == "pointer":
            if POINTER not in getattr(self, "features", ()):
                raise Rejected("Unsupported desktop operation.")
            return self.pointer(args)
        if operation == "focus_window":
            return self.focus_window(args)
        if operation == "read_clipboard":
            if CLIPBOARD not in getattr(self, "features", ()):
                raise Rejected("Unsupported desktop operation.")
            return self.read_clipboard()
        if operation not in {"click", "type", "key", "scroll"}:
            raise Rejected("Unsupported desktop operation.")
        if operation == "click":
            x, y = integer(args.get("x"), 0, WIDTH - 1), integer(args.get("y"), 0, HEIGHT - 1)
            button = integer(args.get("button", 1), 1, 3)
        elif operation == "type":
            text = args.get("text")
            if not isinstance(text, str) or not 1 <= len(text) <= 8000 or "\0" in text:
                raise Rejected("Invalid bounded text.")
        elif operation == "key":
            keys = args.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
                raise Rejected("Supply one to four X11 key names.")
            xlib = ctypes.CDLL("libX11.so.6")
            xlib.XStringToKeysym.argtypes = [ctypes.c_char_p]
            xlib.XStringToKeysym.restype = ctypes.c_ulong
            for key in keys:
                if (
                    not isinstance(key, str)
                    or not key.isascii()
                    or not 1 <= len(key) <= 40
                    or not xlib.XStringToKeysym(key.encode())
                ):
                    raise Rejected("Invalid X11 key name.")
        else:
            delta = integer(args.get("ticks"), -20, 20)
            x, y = integer(args.get("x"), 0, WIDTH - 1), integer(args.get("y"), 0, HEIGHT - 1)
        self.fresh(args.get("frame_id"))
        if operation == "click":
            self.move_pointer(x, y)
            try:
                self.xdo("mousedown", str(button))
            finally:
                self.xdo("mouseup", str(button))
        elif operation == "type":
            self.paste_text(text)
        elif operation == "key":
            try:
                self.xdo("keydown", "+".join(keys))
            finally:
                self.xdo("keyup", "+".join(reversed(keys)))
        elif delta:
            self.move_pointer(x, y)
            self.xdo(
                "click", "--repeat", str(abs(delta)), "--delay", "20", "5" if delta > 0 else "4"
            )
        result = {"dispatched": operation != "scroll" or bool(delta), "verification_required": True}
        try:
            result["observation"] = self.with_elements(self.observe(), args)
        except Exception:
            result["observation_unavailable"] = True
        return result


def main():
    from pathlib import Path

    # Only these standalone, read-only modules exist at this fixed jail path.
    sys.path.insert(0, "/opt")
    from desktop_rfb import RfbSessions
    from screen_channel import ScreenServer

    computer = Computer(shared_display=sys.argv[2] if len(sys.argv) == 3 else None)
    screen = ScreenServer(Path("/run/alysis-screen"), sys.argv[1])
    screen.bind(computer.capture)
    live = RfbSessions(computer.env["DISPLAY"], computer.env)
    screen.bind_live(live.exchange)
    try:
        serve(computer)
    finally:
        screen.close()
        live.close()


def serve(computer):
    for line in iter(lambda: sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1), b""):
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            return
        request = json.loads(line)
        if any(process.poll() is not None for process in computer.processes):
            return  # Loss of the display/window manager retires the whole child.
        try:
            result = computer.dispatch(request["operation"], request["arguments"])
            reply = {"id": request["id"], "result": result}
        except Rejected as exc:
            reply = {"id": request["id"], "error": str(exc)}
        except Exception:
            reply = {
                "id": request["id"],
                "error": "Computer operation failed; input may have been sent. Observe before repeating.",
                "fatal": True,
            }
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
