"""Standalone credential-free offline desktop worker. Private pipe protocol only.

This file is mounted alone in the child; it must not import the Bots service.
All returned page, pixel, file and command content is untrusted task data.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import importlib.metadata
import io
import json
import os
import select
import signal
import stat
import subprocess
import sys
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


class Rejected(Exception):
    pass


def integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise Rejected("Invalid bounded integer argument.")
    return value


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


# The owl desktops people see (AL, 2026-11-07; a bar like Grok Bot's, 2026-11-13): a soft
# wallpaper and a bar along the bottom with app launchers, the open windows and the computer's
# clock. The owl's online browser is its own screen with the same look (browser_linux.py).
# Everything here works offline, like the rest of this desktop. Cosmetic only: a missing tool
# never stops the desktop.
WALLPAPER = ("#eef0f3", "#c6cad1")
DOCK = Path("/tmp/alysis-dock")
DOCK_APPS = (
    ("files", "Files", "system-file-manager", ["/usr/bin/pcmanfm", "/workspace"]),
    ("libreoffice", "LibreOffice", "libreoffice-startcenter", ["/usr/bin/libreoffice", "--nologo"]),
    (
        "terminal",
        "Terminal",
        "utilities-terminal",
        [
            "/usr/bin/xterm",
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
rounded = 0
border_width = 0
background_color = #1f2328 94
border_color = #000000 0
# 2: the window that has focus, and any button under the pointer
rounded = 6
border_width = 0
background_color = #ffffff 16
border_color = #000000 0

panel_items = LTC
panel_size = 100% 40
panel_margin = 0 0
panel_padding = 10 4 8
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
launcher_icon_size = 26
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
task_text = 1
task_icon = 1
task_centered = 0
task_tooltip = 1
task_maximum_size = 220 30
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
time1_format = %Y-%m-%d  %H:%M %Z
time1_font = DejaVu Sans 9
clock_font_color = #ffffff 85
clock_padding = 8 0
clock_background_id = 0
"""


def desktop_exec(argv):
    """Quote argv for a .desktop Exec line (spaces and quotes; % is reserved)."""
    quoted = []
    for argument in argv:
        argument = argument.replace("%", "%%")
        if any(character in argument for character in ' "\\'):
            argument = '"' + argument.replace("\\", "\\\\").replace('"', '\\"') + '"'
        quoted.append(argument)
    return " ".join(quoted)


def write_dock(directory=DOCK, available=os.path.exists):
    """Write the dock's launchers (only for installed apps) and tint2 settings; return the settings."""
    directory.mkdir(parents=True, exist_ok=True)
    launchers = []
    for name, title, icon, argv in DOCK_APPS:
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


class Computer:
    def __init__(self):
        self.processes = []
        self.jobs = {}
        self.frame = None
        self._check = None
        self._check_pool = ThreadPoolExecutor(max_workers=1)
        self.env = {
            "PATH": "/opt/alysis/.venv/bin:/usr/bin:/bin",
            "HOME": "/tmp/home",
            "LANG": "C.UTF-8",
        }
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
        self.decorations = []
        self._decorate()

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
            settings = write_dock()
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
            "network": "offline",
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
        timeout = integer(args.get("timeout", 10), 1, 30)
        if background and sum(p.poll() is None for p in self.jobs.values()) >= 4:
            raise Rejected("This computer already has four running applications.")
        self.frame = None
        # A second PID namespace prevents a task command from reaching the
        # controller's /proc/<pid>/fd/1 or signalling the pipe protocol worker.
        # The shared filesystem here is already the minimal offline outer jail.
        process = subprocess.Popen(
            application_argv(["/bin/sh", "-c", command]),
            env=self.env,
            cwd="/workspace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if background else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        if background:
            process_id = uuid4().hex
            self.jobs[process_id] = process
            return {
                "process_id": process_id,
                "started": True,
                "exit_code": process.poll(),
                "verification_required": True,
                "network": "offline",
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
            "network": "offline",
        }

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
            return {"ready": True, "network": "offline"}
        if operation == "capture":
            return {"png": base64.b64encode(self.capture()).decode()}
        if operation == "observe":
            return self.observe()
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
        if operation in {"run", "launch"}:
            result = self.run(args, background=operation == "launch")
            if not getattr(self, "_software_reported", False):
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
            process = self.jobs.get(args.get("process_id"))
            if process is None:
                raise Rejected("Application not found in this computer.")
            return {"exit_code": process.poll(), "running": process.poll() is None}
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
            result["observation"] = self.observe()
        except Exception:
            result["observation_unavailable"] = True
        return result


def main():
    from pathlib import Path

    # Only these standalone, read-only modules exist at this fixed jail path.
    sys.path.insert(0, "/opt")
    from screen_channel import ScreenServer

    computer = Computer()
    screen = ScreenServer(Path("/run/alysis-screen"), sys.argv[1])
    screen.bind(computer.capture)
    try:
        serve(computer)
    finally:
        screen.close()


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
