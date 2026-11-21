"""An owl computer's newer hands: commands that run for minutes, launched processes whose output
can be read, double-click, right-click, hover and drag, bringing a window to the front, and
reading the clipboard. Each is offered only while the running computer declares it; an older
image keeps today's tools and requests."""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from alysis_code.bots import desktop as desktop_module
from alysis_code.bots import desktop_elements
from alysis_code.bots import desktop_worker as worker_module
from alysis_code.bots.browser import BrowserImageResult
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.desktop_worker import Computer, Rejected
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken

HANDS = ("long_commands", "process_output", "pointer", "windows", "clipboard")
# Process groups and pipes of a real child: POSIX only (the owl computer is Linux).
posix_processes = pytest.mark.skipif(not hasattr(os, "killpg"), reason="needs POSIX process groups")
GREEK = "Έγγραφο 1 — Επεξεργαστής κειμένου"
CHINESE = "季度报告.docx - 文字处理"


def png():
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "teal").save(image, "PNG")
    return image.getvalue()


# --- The guest worker ---------------------------------------------------------------------


@pytest.fixture
def worker(monkeypatch, tmp_path):
    worker = Computer.__new__(Computer)
    worker.shared_display = False
    worker.display = ":7"
    worker.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    worker.package_indexes = ()
    worker.features = (*HANDS, "elements")
    worker.jobs, worker.logs, worker.command = {}, {}, None
    worker.frame = ("frame", time.monotonic(), b"pixels")
    worker.capture = lambda: b"pixels"
    worker.calls = []
    worker.xdo = lambda *args, **kwargs: worker.calls.append(args)
    worker.move_pointer = lambda x, y: worker.calls.append(("move", x, y))
    worker.observe = lambda: {"frame_id": "new", "png": "image"}
    worker.replies = []
    worker.requests = []

    def accessibility(operation, request=None, **kwargs):
        worker.requests.append((operation, request))
        return worker.replies.pop(0)

    worker.accessibility = accessibility
    worker._software_reported = True
    # Commands run as plain children here, in a scratch folder; in the computer they run in
    # their own namespace, in /workspace.
    monkeypatch.setattr(worker_module, "application_argv", lambda argv: argv)
    popen = subprocess.Popen

    def start(argv, **kwargs):
        if kwargs.get("cwd") == "/workspace":
            kwargs["cwd"] = tmp_path
        return popen(argv, **kwargs)

    monkeypatch.setattr(worker_module.subprocess, "Popen", start)
    monkeypatch.setattr(worker_module, "accessibility_module", lambda: _Accessibility)
    return worker


class _Accessibility:
    @staticmethod
    def element_identity(value):
        from alysis_code.bots.desktop_accessibility import element_identity

        return element_identity(value)


def finish(worker, reply, *, seconds=20):
    deadline = time.monotonic() + seconds
    while reply.get("running") is True:
        assert time.monotonic() < deadline, "the command never ended"
        reply = worker.dispatch("command_wait", {"command_id": reply["command_id"], "wait": 1})
    return reply


@posix_processes
def test_a_command_past_the_old_30_second_bound_runs_and_its_output_arrives_whole(worker):
    reply = worker.dispatch(
        "command_start",
        {"command": "sleep 1; printf 'Καλημέρα 你好\\n'; exit 3", "timeout": 90, "wait": 0},
    )
    assert reply == {"command_id": reply["command_id"], "running": True}
    done = finish(worker, reply)
    assert done["output"] == "Καλημέρα 你好\n" and done["exit_code"] == 3
    assert done["timed_out"] is False and done["output_truncated"] is False
    assert done["untrusted_computer_data"] is True and done["running"] is False
    assert worker.command is None
    with pytest.raises(Rejected, match="not found"):
        worker.dispatch("command_wait", {"command_id": reply["command_id"], "wait": 0})
    # The bounds: up to 300 seconds, a wait of at most 5 seconds per request.
    for bad in ({"timeout": 301}, {"timeout": 0}, {"wait": 6}, {"timeout": "90"}):
        with pytest.raises(Rejected):
            worker.dispatch("command_start", {"command": "true", "timeout": 10, **bad})


@posix_processes
def test_a_command_is_stopped_at_its_time_limit_even_when_nobody_waits(worker):
    reply = worker.dispatch("command_start", {"command": "echo started; sleep 30", "timeout": 1})
    time.sleep(1.6)  # nobody asks: the computer still stops it at its limit
    done = finish(worker, reply)
    assert done["timed_out"] is True and done["output"] == "started\n"
    assert done["exit_code"] is not None and done["exit_code"] < 0  # killed, not finished


@posix_processes
def test_a_command_that_prints_more_than_one_receipt_holds_is_stopped_at_the_cap(worker):
    done = finish(worker, worker.dispatch("command_start", {"command": "yes Ω", "timeout": 60}))
    assert done["output_truncated"] is True and done["timed_out"] is False
    encoded = done["output"].encode()
    assert len(encoded) <= worker_module.MAX_OUTPUT
    assert "�" not in done["output"]  # cut between characters, never inside one
    assert done["exit_code"] is not None


@posix_processes
def test_a_new_command_stops_one_whose_wait_was_abandoned(worker):
    first = worker.dispatch("command_start", {"command": "sleep 30", "timeout": 60})
    process = worker.command[1].process
    second = finish(worker, worker.dispatch("command_start", {"command": "echo next"}))
    assert second["output"] == "next\n"
    assert process.poll() is not None
    with pytest.raises(Rejected, match="not found"):
        worker.dispatch("command_wait", {"command_id": first["command_id"]})


@posix_processes
def test_a_launched_process_output_is_read_from_an_offset_with_its_exit_code(worker):
    launched = worker.dispatch(
        "launch", {"command": "printf 'ένα\\n'; sleep 0.3; printf 'δύο 三\\n'; exit 4"}
    )
    process_id = launched["process_id"]
    deadline = time.monotonic() + 10
    while True:
        status = worker.dispatch("status", {"process_id": process_id, "output_from": 0})
        if status["running"] is False and status["output_complete"]:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert status["exit_code"] == 4 and status["output"] == "ένα\nδύο 三\n"
    assert status["output_from"] == 0 and status["untrusted_computer_data"] is True
    end = status["next_output_from"]
    assert end == len("ένα\nδύο 三\n".encode())
    later = worker.dispatch("status", {"process_id": process_id, "output_from": end})
    assert later["output"] == "" and later["next_output_from"] == end
    # From inside a character: the read starts at the next whole one.
    inside = worker.dispatch("status", {"process_id": process_id, "output_from": 1})
    assert inside["output"] == "να\nδύο 三\n" and inside["output_from"] == 2
    # Today's request (no offset) keeps today's reply.
    assert worker.dispatch("status", {"process_id": process_id}) == {
        "exit_code": 4,
        "running": False,
    }
    with pytest.raises(Rejected):
        worker.dispatch("status", {"process_id": process_id, "output_from": -1})


@posix_processes
def test_a_launched_process_keeps_its_latest_output_and_says_what_was_dropped(worker, monkeypatch):
    monkeypatch.setattr(worker_module, "LOG_BYTES", 64)
    launched = worker.dispatch("launch", {"command": "for i in $(seq 1 40); do echo line$i; done"})
    deadline = time.monotonic() + 10
    while not worker.logs[launched["process_id"]].ended.is_set():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    status = worker.dispatch("status", {"process_id": launched["process_id"], "output_from": 0})
    assert status["output"].endswith("line40\n") and len(status["output"]) <= 64
    assert status["output_dropped"] == status["output_from"] > 0
    assert status["next_output_from"] == sum(len(f"line{i}\n") for i in range(1, 41))


@posix_processes
def test_launched_processes_are_remembered_within_a_bound(worker, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_JOBS", 3)
    ids = []
    for _ in range(5):
        ids.append(worker.dispatch("launch", {"command": "true"})["process_id"])
        worker.jobs[ids[-1]].wait(5)
    assert len(worker.jobs) <= 3 and ids[-1] in worker.jobs and ids[0] not in worker.jobs
    assert set(worker.logs) == set(worker.jobs)


def test_double_and_right_click_hover_and_drag_send_exact_argument_lists(worker, monkeypatch):
    slept = []
    monkeypatch.setattr(worker_module.time, "sleep", slept.append)
    worker.dispatch(
        "pointer", {"action": "double_click", "at": {"x": 20, "y": 30}, "frame_id": "frame"}
    )
    assert worker.calls == [("move", 20, 30), ("click", "--repeat", "2", "--delay", "80", "1")]
    for action, sent in (("right_click", [("click", "3")]), ("hover", [])):
        worker.calls.clear()
        worker.frame = ("frame", time.monotonic(), b"pixels")
        result = worker.dispatch(
            "pointer", {"action": action, "at": {"x": 5, "y": 6}, "frame_id": "frame"}
        )
        assert worker.calls == [("move", 5, 6), *sent]
        assert result["dispatched"] and result["observation"]["frame_id"] == "new"
    assert slept[-1] == worker_module.HOVER_SECONDS
    worker.calls.clear()
    worker.frame = ("frame", time.monotonic(), b"pixels")
    worker.dispatch(
        "pointer",
        {"action": "drag", "at": {"x": 0, "y": 0}, "to": {"x": 100, "y": 50}, "frame_id": "frame"},
    )
    assert worker.calls[:2] == [("move", 0, 0), ("mousedown", "1")]
    steps = worker.calls[2:-2]
    assert len(steps) == worker_module.DRAG_STEPS - 1 and steps[0] == ("mousemove", "10", "5")
    assert worker.calls[-2:] == [("move", 100, 50), ("mouseup", "1")]
    # Every argument is its own list item: nothing reaches a shell.
    assert all(
        isinstance(part, str | int) and " " not in str(part) for c in worker.calls for part in c
    )


def test_a_drag_releases_the_button_whatever_fails(worker):
    def xdo(*args, **kwargs):
        worker.calls.append(args)
        if args[0] == "mousemove":
            raise subprocess.CalledProcessError(1, "xdotool")

    worker.xdo = xdo
    with pytest.raises(subprocess.CalledProcessError):
        worker.dispatch(
            "pointer",
            {
                "action": "drag",
                "at": {"x": 1, "y": 1},
                "to": {"x": 9, "y": 9},
                "frame_id": "frame",
            },
        )
    assert worker.calls[-1] == ("mouseup", "1")


def test_an_element_target_is_located_first_and_nothing_is_sent_when_it_is_gone(worker):
    element = {"bus": ":1.7", "path": "/org/a11y/atspi/accessible/12", "role": "list item"}
    worker.replies = [{"pointer": {"x": 40, "y": 41}}]
    result = worker.dispatch("pointer", {"action": "right_click", "at": {"element": element}})
    assert worker.requests == [("locate", {"element": element})]
    assert worker.calls == [("move", 40, 41), ("click", "3")] and result["dispatched"]
    worker.calls.clear()
    worker.replies = [{"refused": "gone"}]
    assert worker.dispatch("pointer", {"action": "double_click", "at": {"element": element}}) == {
        "dispatched": False,
        "refused": "gone",
    }
    worker.replies = [{"unavailable": "timeout"}]
    refused = worker.dispatch("pointer", {"action": "hover", "at": {"element": element}})
    assert refused["refused"] == "unavailable" and worker.calls == []
    # A mixed drag locates the element before it checks the frame: a refusal keeps the frame.
    worker.frame = ("frame", time.monotonic(), b"pixels")
    worker.replies = [{"refused": "hidden"}]
    worker.dispatch(
        "pointer",
        {"action": "drag", "at": {"x": 1, "y": 1}, "to": {"element": element}, "frame_id": "frame"},
    )
    assert worker.frame is not None and worker.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {"action": "triple_click", "at": {"x": 1, "y": 1}},
        {"action": "hover", "at": {"x": 1280, "y": 1}},
        {"action": "hover", "at": {"x": "1; rm -rf /", "y": 1}},
        {"action": "hover", "at": {"x": 1, "y": 1, "z": 2}},
        {"action": "hover", "at": {"x": 1, "y": 1}, "to": {"x": 2, "y": 2}},
        {"action": "drag", "at": {"x": 1, "y": 1}},
        {"action": "hover", "at": {"element": {"bus": "x; y", "path": "/", "role": "button"}}},
    ],
)
def test_unclear_pointer_requests_send_nothing_and_keep_the_frame(worker, args, monkeypatch):
    with pytest.raises(Rejected):
        worker.dispatch("pointer", {**args, "frame_id": "frame"})
    assert worker.calls == [] and worker.frame is not None
    # The frame kept is still good for a clear request.
    monkeypatch.setattr(worker_module.time, "sleep", lambda _: None)
    clear = worker.dispatch(
        "pointer", {"action": "hover", "at": {"x": 1, "y": 1}, "frame_id": "frame"}
    )
    assert clear["dispatched"] is True and worker.calls == [("move", 1, 1)]


def test_changed_pixels_stop_a_pointer_action_before_any_input(worker):
    worker.capture = lambda: b"changed"
    with pytest.raises(Rejected, match="pixels changed"):
        worker.dispatch(
            "pointer", {"action": "double_click", "at": {"x": 1, "y": 1}, "frame_id": "frame"}
        )
    assert worker.calls == []


def test_a_window_is_brought_to_the_front_by_its_id_and_confirmed(worker, monkeypatch):
    monkeypatch.setattr(worker_module.time, "sleep", lambda _: None)
    listed = [
        {"id": 0x400003, "title": GREEK, "class": "libreoffice-writer", "active": False},
        {"id": 0x600007, "title": CHINESE, "class": "XTerm", "active": True},
    ]

    def windows(display):
        return [dict(item) for item in listed]

    monkeypatch.setattr(worker_module, "open_windows", windows)

    def xdo(*args, **kwargs):
        worker.calls.append(args)
        for item in listed:
            item["active"] = str(item["id"]) == args[1]

    worker.xdo = xdo
    result = worker.dispatch("focus_window", {"window": 0x400003, "windows": True})
    assert worker.calls == [("windowactivate", str(0x400003))]
    assert result["dispatched"] and result["confirmed"] is True
    assert result["observation"]["windows"][0]["title"] == GREEK
    worker.calls.clear()
    assert worker.dispatch("focus_window", {"window": 0x999}) == {
        "dispatched": False,
        "refused": "gone",
    }
    assert worker.calls == []
    with pytest.raises(Rejected):
        worker.dispatch("focus_window", {"window": "0x400003; xterm"})


def test_clipboard_text_is_capped_whole_and_marked_untrusted(worker, monkeypatch):
    monkeypatch.setattr(worker_module, "CLIPBOARD_BYTES", 7)
    worker._selection = lambda target: "Ωμέγα 中".encode() if target == "UTF8_STRING" else None
    result = worker.dispatch("read_clipboard", {})
    assert result == {"text": "Ωμέ", "truncated": True, "untrusted_computer_data": True}
    worker._selection = lambda target: "café".encode("latin-1") if target == "STRING" else None
    assert worker.dispatch("read_clipboard", {})["text"] == "café"
    worker._selection = lambda target: None
    assert worker.dispatch("read_clipboard", {}) == {"text": None, "empty": True}
    # What the person typed while in control is served from the clipboard: never read.
    worker._human_input = SimpleNamespace(_clipboard=SimpleNamespace(poll=lambda: None))
    worker._selection = lambda target: b"the person's password"
    assert worker.dispatch("read_clipboard", {}) == {"text": None, "private": True}


@posix_processes
def test_reading_the_clipboard_is_bounded_in_bytes_and_time(worker, monkeypatch, tmp_path):
    endless = tmp_path / "clipboard-holder"
    endless.write_text("#!/bin/sh\nwhile :; do printf 'Ω'; done\n")
    endless.chmod(0o755)
    monkeypatch.setattr(worker_module, "XCLIP", str(endless))
    started = time.monotonic()
    data = worker._selection("UTF8_STRING")
    assert worker_module.CLIPBOARD_BYTES < len(data) <= worker_module.CLIPBOARD_BYTES + 65536
    silent = tmp_path / "silent-holder"
    silent.write_text("#!/bin/sh\nsleep 30\n")
    silent.chmod(0o755)
    monkeypatch.setattr(worker_module, "XCLIP", str(silent))
    monkeypatch.setattr(worker_module, "CLIPBOARD_SECONDS", 0.5)
    with pytest.raises(Rejected, match="did not answer"):
        worker._selection("UTF8_STRING")
    assert time.monotonic() - started < 10


def test_the_desktop_declares_its_hands_and_an_older_request_stays_refused(worker, monkeypatch):
    monkeypatch.setattr(worker_module, "open_windows", lambda display, supported=False: [])
    monkeypatch.setattr(worker_module.os.path, "exists", lambda path: True)
    assert set(worker._hands()) == set(HANDS)

    def unsupported(display, supported=False):
        raise Rejected("The window manager does not list its windows.")

    monkeypatch.setattr(worker_module, "open_windows", unsupported)
    monkeypatch.setattr(worker_module.os.path, "exists", lambda path: False)
    assert worker._hands() == ("long_commands", "process_output", "pointer")
    worker.features = ()
    for operation, args in (
        ("pointer", {"action": "hover", "at": {"x": 1, "y": 1}}),
        ("read_clipboard", {}),
        ("focus_window", {"window": 5}),
    ):
        with pytest.raises(Rejected):
            worker.dispatch(operation, args)
    assert worker.calls == []


@pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("Xvfb") or not ctypes.util.find_library("X11"),
    reason="needs an X server (Xvfb) and Xlib",
)
def test_the_window_list_is_read_from_a_real_display_in_any_script():
    read, write = os.pipe()
    server = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write), "-screen", "0", "320x200x24", "-nolisten", "tcp"],
        pass_fds=(write,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write)
    try:
        number = b""
        while not number.endswith(b"\n"):
            part = os.read(read, 16)
            assert part, "Xvfb did not start"
            number += part
        display_name = ":" + number.strip().decode()
        x11 = worker_module._xlib()
        pointer, ulong = ctypes.c_void_p, ctypes.c_ulong
        x11.XCreateSimpleWindow.argtypes = [pointer, ulong, *[ctypes.c_int] * 2]
        x11.XCreateSimpleWindow.argtypes += [*[ctypes.c_uint] * 3, ulong, ulong]
        x11.XCreateSimpleWindow.restype = ulong
        x11.XChangeProperty.argtypes = [pointer, ulong, ulong, ulong, ctypes.c_int]
        x11.XChangeProperty.argtypes += [ctypes.c_int, pointer, ctypes.c_int]
        x11.XDestroyWindow.argtypes = [pointer, ulong]
        display = x11.XOpenDisplay(display_name.encode())
        root = x11.XDefaultRootWindow(display)

        def atom(name):
            return x11.XInternAtom(display, name.encode(), 0)

        def text(window, name, value, kind="UTF8_STRING"):
            data = value.encode("utf-8" if kind == "UTF8_STRING" else "latin-1")
            x11.XChangeProperty(
                display, window, atom(name), atom(kind), 8, 0, ctypes.c_char_p(data), len(data)
            )

        def ids(name, values, kind="WINDOW"):
            array = (ctypes.c_ulong * len(values))(*values)
            x11.XChangeProperty(display, root, atom(name), atom(kind), 32, 0, array, len(values))

        greek, chinese, latin, dock, closed = (
            x11.XCreateSimpleWindow(display, root, 0, 0, 10, 10, 0, 0, 0) for _ in range(5)
        )
        text(greek, "_NET_WM_NAME", GREEK)
        text(greek, "WM_CLASS", "soffice\0libreoffice-writer\0", kind="STRING")
        text(chinese, "_NET_WM_NAME", CHINESE)
        text(latin, "WM_NAME", "Café", kind="STRING")
        dock_type = (ctypes.c_ulong * 1)(atom("_NET_WM_WINDOW_TYPE_DOCK"))
        x11.XChangeProperty(
            display, dock, atom("_NET_WM_WINDOW_TYPE"), atom("ATOM"), 32, 0, dock_type, 1
        )
        x11.XDestroyWindow(display, closed)
        ids("_NET_CLIENT_LIST", [greek, closed, chinese, dock, latin])
        ids("_NET_ACTIVE_WINDOW", [chinese])
        x11.XSync(display, 0)
        with pytest.raises(Rejected, match="does not list"):
            worker_module.open_windows(display_name, supported=True)
        ids("_NET_SUPPORTED", [atom("_NET_CLIENT_LIST"), atom("_NET_ACTIVE_WINDOW")], "ATOM")
        x11.XSync(display, 0)
        listed = worker_module.open_windows(display_name, supported=True)
        assert listed == [
            {"id": greek, "title": GREEK, "class": "libreoffice-writer", "active": False},
            {"id": chinese, "title": CHINESE, "class": "", "active": True},
            {"id": latin, "title": "Café", "class": "", "active": False},
        ]
        x11.XCloseDisplay(display)
    finally:
        os.close(read)
        server.kill()
        server.wait(5)


# --- The host: offering, admission, requests and receipts ----------------------------------


class HandsExecutor:
    """A running owl computer that declares ``features``; it answers each operation the way the
    worker does and records the requests."""

    def __init__(self, owner, workspace, *, cancel, features=HANDS):
        self.owner, self.workspace = owner, workspace
        self.closed = False
        self.calls = []
        self.desktop_features = frozenset(features)
        self.png = png()
        self.windows = [
            {"id": 0x400003, "title": GREEK, "class": "libreoffice-writer", "active": False},
            {"id": 0x600007, "title": CHINESE, "class": "wps", "active": False},
            {"id": 0x800001, "title": "user@owl: /workspace", "class": "XTerm", "active": True},
        ]
        self.command_polls = 0
        self.command_length = 0
        self.replies = {}

    def alive(self):
        return not self.closed

    def capture(self):
        return self.png

    def close(self):
        self.closed = True

    def observe(self, args):
        observation = {
            "frame_id": uuid4().hex,
            "width": 2,
            "height": 2,
            "png": base64.b64encode(self.png).decode(),
        }
        if args.get("windows") is True:
            observation["windows"] = [dict(item) for item in self.windows]
        if args.get("image") is False:
            observation.pop("png")
        return observation

    def call(self, owner, operation, args, *, cancel):
        assert owner == self.owner
        self.calls.append((operation, args))
        if operation in self.replies:
            return self.replies[operation]
        if operation == "observe":
            return self.observe(args)
        if operation in {"pointer", "focus_window", "click"}:
            return {
                "dispatched": True,
                "verification_required": True,
                "observation": self.observe(args),
            }
        if operation == "command_start":
            self.command_polls = 0
            return {"command_id": "c1", "running": True}
        if operation == "command_wait":
            self.command_polls += 1
            if self.command_polls * args["wait"] < self.command_length:
                return {"command_id": args["command_id"], "running": True}
            return {
                "running": False,
                "exit_code": 0,
                "output": "εγκαταστάθηκε 已安装\n",
                "timed_out": False,
                "output_truncated": False,
                "untrusted_computer_data": True,
                "network": "offline",
            }
        if operation == "read_clipboard":
            return {"text": "Ωμέγα 中文", "truncated": False}
        return {"exit_code": 0, "output": "fixture result"}


@pytest.fixture
def computers():
    made, managers = [], []

    def make(features=HANDS):
        def factory(*args, **kwargs):
            made.append(HandsExecutor(*args, **kwargs, features=features))
            return made[-1]

        managers.append(DesktopComputers("owner", ComputerViews(), factory=factory))
        return managers[-1]

    yield make, made
    for manager in managers:
        manager.close()


def task():
    return {"id": "task", "bot_id": "bot", "config": {"files_enabled": False}}


def token(event=None):
    return EventCancellationToken(event or threading.Event())


def offered(manager, image_input):
    schemas = manager.schemas(task(), True, approved=True, image_input=image_input)
    return {
        s["function"]["name"]: s for s in manager.offered(task(), schemas, image_input=image_input)
    }


def call(manager, name, args=None, *, image_input=True, review=None, cancel=None):
    return manager.call(
        task(),
        name,
        args or {},
        cancel or token(),
        approved=True,
        image_input=image_input,
        review_action=review,
    )


def view(result):
    return result.observation if isinstance(result, BrowserImageResult) else result


def test_an_older_computer_keeps_todays_tools_requests_and_bounds(computers):
    make, made = computers
    manager = make(features=())
    for image_input in (False, True):
        tools = offered(manager, image_input)
        assert not {"computer_pointer", "computer_focus_window", "computer_read_clipboard"} & set(
            tools
        )
    call(manager, "computer_run", {"command": "true", "timeout": 30})
    tools = offered(manager, True)
    assert not {"computer_pointer", "computer_focus_window", "computer_read_clipboard"} & set(tools)
    run = tools["computer_run"]["function"]
    assert run["parameters"]["properties"]["timeout"]["maximum"] == 30
    assert "run long installs with computer_launch" in run["description"]
    assert "output_from" not in tools["computer_process"]["function"]["parameters"]["properties"]
    assert "stdout/stderr are discarded" in tools["computer_launch"]["function"]["description"]
    assert made[0].calls == [("run", {"command": "true", "timeout": 30})]
    # What it can't do is refused before anything is sent.
    with pytest.raises(DesktopError, match="at most 30 seconds"):
        call(manager, "computer_run", {"command": "pip install pandas", "timeout": 120})
    observed = view(call(manager, "computer_observe"))
    assert "other_windows" not in observed and made[0].calls[-1] == ("observe", {})
    for name, args, words in (
        ("computer_read_clipboard", {}, "cannot read its clipboard"),
        ("computer_focus_window", {"source_id": observed["source_id"], "ref": 1}, "cannot bring"),
        (
            "computer_pointer",
            {"source_id": observed["source_id"], "action": "hover", "x": 1, "y": 1},
            "only click",
        ),
        ("computer_process", {"process_id": "p", "output_from": 0}, "does not keep"),
    ):
        with pytest.raises(DesktopError, match=words):
            call(manager, name, args)
    assert made[0].calls[-1] == ("observe", {})
    call(manager, "computer_process", {"process_id": "p"})
    assert made[0].calls[-1] == ("status", {"process_id": "p"})


def test_a_newer_computer_offers_its_hands_once_it_runs(computers):
    make, _ = computers
    manager = make()
    before = offered(manager, True)
    assert "computer_pointer" not in before  # nothing runs yet: today's tools
    assert (
        before["computer_run"]["function"]["parameters"]["properties"]["timeout"]["maximum"] == 30
    )
    call(manager, "computer_observe")
    tools = offered(manager, True)
    assert {"computer_pointer", "computer_focus_window", "computer_read_clipboard"} <= set(tools)
    run = tools["computer_run"]["function"]
    assert run["parameters"]["properties"]["timeout"]["maximum"] == 300
    assert "output_from" in tools["computer_process"]["function"]["parameters"]["properties"]
    # Without element reading this model aims at pixels only.
    pointer = tools["computer_pointer"]["function"]["parameters"]["properties"]
    assert {"x", "y", "to_x", "to_y"} <= set(pointer) and not {"ref", "to_ref"} & set(pointer)
    # A model without images and no element view has nothing to aim with or switch between.
    assert not {"computer_pointer", "computer_focus_window"} & set(offered(manager, False))
    assert "computer_read_clipboard" in offered(manager, False)


def test_a_text_only_model_aims_by_ref_on_a_computer_that_reads_elements(computers):
    make, _ = computers
    manager = make(features=(*HANDS, "elements"))
    call(manager, "computer_run", {"command": "true"}, image_input=False)
    pointer = offered(manager, False)["computer_pointer"]["function"]["parameters"]["properties"]
    assert {"ref", "to_ref"} <= set(pointer) and not {"x", "y"} & set(pointer)
    assert "computer_focus_window" in offered(manager, False)


def test_a_90_second_command_is_waited_for_in_short_requests(computers, monkeypatch):
    make, made = computers
    manager = make()
    now = [1000.0]
    monkeypatch.setattr(desktop_module.time, "monotonic", lambda: now[0])
    call(manager, "computer_observe")
    executor = made[0]
    executor.command_length = 90
    original = executor.call

    def call_with_clock(owner, operation, args, *, cancel):
        if operation == "command_wait":
            now[0] += args["wait"]  # each request waits its full window
        return original(owner, operation, args, cancel=cancel)

    executor.call = call_with_clock
    result = call(manager, "computer_run", {"command": "pip install pandas", "timeout": 90})
    assert result["output"] == "εγκαταστάθηκε 已安装\n" and result["exit_code"] == 0
    assert "running" not in result and "command_id" not in result
    assert result["computer_session_id"]
    started = [c for c in executor.calls if c[0] == "command_start"]
    assert started == [
        ("command_start", {"command": "pip install pandas", "timeout": 90, "wait": 2})
    ]
    waits = [c for c in executor.calls if c[0] == "command_wait"]
    assert len(waits) == 45 and all(c[1]["wait"] == 2 for c in waits)
    assert now[0] - 1000.0 == 90


def test_a_long_command_the_computer_stopped_says_its_own_limit_and_may_have_partly_run(
    computers,
):
    make, made = computers
    manager = make()
    call(manager, "computer_observe")
    made[0].replies["command_wait"] = {
        "exit_code": None,
        "output": "λήψη 下载\n",
        "timed_out": True,
        "output_truncated": False,
    }
    result = call(manager, "computer_run", {"command": "make all", "timeout": 280})
    assert result["timed_out"] is True and "command_id" not in result
    assert "280-second limit" in result["note"] and "may have partly run" in result["note"]
    assert "computer_launch" in result["note"]


def test_stop_ends_the_wait_for_a_long_command(computers):
    make, made = computers
    manager = make()
    call(manager, "computer_observe")
    executor = made[0]
    executor.command_length = 10_000
    stop = threading.Event()
    original = executor.call

    def stop_after_two(owner, operation, args, *, cancel):
        if operation == "command_wait" and executor.command_polls >= 1:
            stop.set()
        return original(owner, operation, args, cancel=cancel)

    executor.call = stop_after_two
    started = time.monotonic()
    with pytest.raises(CooperativeCancellationError):
        call(manager, "computer_run", {"command": "sleep 250", "timeout": 300}, cancel=token(stop))
    assert time.monotonic() - started < 5
    assert [c[0] for c in executor.calls].count("command_wait") == 2


def test_a_computer_that_never_reports_the_end_is_not_waited_on_forever(computers, monkeypatch):
    make, made = computers
    manager = make()
    now = [0.0]
    monkeypatch.setattr(desktop_module.time, "monotonic", lambda: now[0])
    call(manager, "computer_observe")
    made[0].command_length = 10**9
    original = made[0].call

    def slow(owner, operation, args, *, cancel):
        now[0] += 10
        return original(owner, operation, args, cancel=cancel)

    made[0].call = slow
    with pytest.raises(DesktopError, match="may have partly run"):
        call(manager, "computer_run", {"command": "sleep 100", "timeout": 60})


def test_a_launched_process_output_is_read_through_computer_process(computers):
    make, made = computers
    manager = make()
    call(manager, "computer_launch", {"command": "ffmpeg -i in.mov out.mp4"})
    made[0].replies["status"] = {
        "exit_code": None,
        "running": True,
        "output": "frame=  120 fps=30\n",
        "output_from": 0,
        "next_output_from": 19,
        "output_complete": False,
        "untrusted_computer_data": True,
    }
    result = call(manager, "computer_process", {"process_id": "p1", "output_from": 0})
    assert made[0].calls[-1] == ("status", {"process_id": "p1", "output_from": 0})
    assert result["next_output_from"] == 19 and result["untrusted_computer_data"] is True


def test_pointer_actions_carry_their_targets_and_pass_the_review(computers):
    make, made = computers
    manager = make()
    reviews = []
    observed = view(call(manager, "computer_observe"))
    result = view(
        call(
            manager,
            "computer_pointer",
            {"source_id": observed["source_id"], "action": "double_click", "x": 10, "y": 20},
            review=lambda *args, **kwargs: reviews.append((args, kwargs)),
        )
    )
    sent = made[0].calls[-1]
    assert sent == (
        "pointer",
        {"action": "double_click", "at": {"x": 10, "y": 20}, "frame_id": observed["source_id"]},
    )
    assert "other_windows" not in result  # pixel input stays as fast as a click
    (name, arguments, context), options = reviews[0]
    assert name == "computer_pointer" and options == {"private_computer": True}
    assert context["screenshot"].startswith("data:image/png;base64,")
    assert arguments["action"] == "double_click"
    # The frame was used: the same source_id is refused before anything is sent.
    with pytest.raises(DesktopError, match="expired"):
        call(
            manager,
            "computer_pointer",
            {"source_id": observed["source_id"], "action": "hover", "x": 1, "y": 1},
        )
    assert made[0].calls[-1] == sent
    # A model that doesn't see the screen image can't aim at its pixels.
    with pytest.raises(DesktopError, match="does not see the screen image"):
        call(
            manager,
            "computer_pointer",
            {"source_id": result["source_id"], "action": "hover", "x": 1, "y": 1},
            image_input=False,
        )


def test_windows_get_refs_in_any_script_and_come_to_the_front_by_them(computers):
    make, made = computers
    manager = make()
    reviews = []
    observed = view(call(manager, "computer_observe"))
    assert made[0].calls[-1] == ("observe", {"windows": True})
    assert observed["other_windows"] == [
        f"window {json.dumps(GREEK, ensure_ascii=False)} (libreoffice-writer) [ref=e1]",
        f"window {json.dumps(CHINESE, ensure_ascii=False)} (wps) [ref=e2]",
    ]
    assert observed["focused_window"] == 'window "user@owl: /workspace" (XTerm)'
    focused = view(
        call(
            manager,
            "computer_focus_window",
            {"source_id": observed["source_id"], "ref": 2},
            review=lambda *args, **kwargs: reviews.append((args, kwargs)),
        )
    )
    assert made[0].calls[-1] == ("focus_window", {"window": 0x600007, "windows": True})
    (name, _, context), _ = reviews[0]
    assert name == "computer_focus_window"
    assert context["window"] == f"window {json.dumps(CHINESE, ensure_ascii=False)} (wps)"
    # A window keeps its ref while it is open.
    assert "[ref=e1]" in focused["other_windows"][0]
    # Refs name windows or elements, never the other.
    with pytest.raises(desktop_elements.ElementRefusal, match="not a window"):
        call(manager, "computer_focus_window", {"source_id": focused["source_id"], "ref": 9})
    made[0].replies["focus_window"] = {"dispatched": False, "refused": "gone"}
    with pytest.raises(desktop_elements.ElementRefusal, match="no longer open"):
        call(manager, "computer_focus_window", {"source_id": focused["source_id"], "ref": 1})
    with pytest.raises(DesktopError, match="expired"):
        call(manager, "computer_focus_window", {"source_id": "not-an-observation", "ref": 1})


def test_a_window_ref_is_not_an_element_and_an_element_target_needs_the_element_view(
    computers,
):
    make, made = computers
    manager = make(features=(*HANDS, "elements"))
    reading = {"focused": None, "windows": [], "nodes": [], "stopped": None}
    original = HandsExecutor.observe

    def observe(self, args):
        observation = original(self, args)
        if args.get("elements") is True:
            observation["elements"] = reading
        return observation

    made_observe = observe
    call(manager, "computer_run", {"command": "true"})
    made[0].observe = made_observe.__get__(made[0])
    observed = view(call(manager, "computer_observe"))
    assert made[0].calls[-1] == ("observe", {"elements": True, "windows": True})
    assert "bring one of other_windows to the front" in observed["element_view"]
    with pytest.raises(desktop_elements.ElementRefusal, match="is a window"):
        call(
            manager,
            "computer_pointer",
            {"source_id": observed["source_id"], "action": "right_click", "ref": 1},
        )
    with pytest.raises(desktop_elements.ElementRefusal, match="is a window"):
        call(
            manager,
            "computer_element",
            {"source_id": observed["source_id"], "ref": 1, "action": "activate"},
        )


def test_the_clipboard_is_read_as_untrusted_data_without_using_the_frame(computers):
    make, made = computers
    manager = make()
    observed = view(call(manager, "computer_observe"))
    read = call(manager, "computer_read_clipboard")
    assert made[0].calls[-1] == ("read_clipboard", {})
    assert read["text"] == "Ωμέγα 中文" and read["untrusted_computer_data"] is True
    # Reading changes nothing on the screen: the observation is still usable for input.
    call(manager, "computer_click", {"source_id": observed["source_id"], "x": 1, "y": 1})
    made[0].replies["read_clipboard"] = {"text": None, "private": True}
    assert "not read" in call(manager, "computer_read_clipboard")["note"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "hover"},
        {"action": "hover", "x": 1},
        {"action": "hover", "x": 1, "y": 1, "ref": 2},
        {"action": "hover", "x": 1, "y": 1, "to_x": 2, "to_y": 2},
        {"action": "drag", "x": 1, "y": 1},
        {"action": "drag", "x": 1, "y": 1, "to_x": 2},
        {"action": "drag", "ref": 1, "to_ref": 2, "to_x": 1, "to_y": 1},
        {"action": "triple_click", "x": 1, "y": 1},
        {"action": "hover", "x": 1280, "y": 1},
        {"action": "hover", "x": "1", "y": 1},
    ],
)
def test_unclear_pointer_requests_are_refused_before_the_computer(computers, arguments):
    make, made = computers
    manager = make()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        call(manager, "computer_pointer", {"source_id": "s", **arguments})
    assert made == []


def test_both_computer_transports_forward_the_new_operations(tmp_path, monkeypatch):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop

    operations = {
        "command_start": {"command": "true", "timeout": 120, "wait": 2},
        "command_wait": {"command_id": "c1", "wait": 2},
        "pointer": {"action": "hover", "at": {"x": 1, "y": 1}, "frame_id": "f"},
        "focus_window": {"window": 5},
        "read_clipboard": {},
    }
    provider = Provider()
    factory = CloudDesktopFactory(
        "owner", provider, ComputerSpec("prepared-image"), tmp_path / "ledger"
    )
    computer = factory("bot", tmp_path, cancel=lambda: False)
    try:
        for operation, args in operations.items():
            computer.call("bot", operation, args)
            sent = json.loads(base64.b64decode(provider.actions[-1][-1]))
            assert sent["operation"] == operation and sent["arguments"] == args
    finally:
        computer.close()
    local = OfflineLinuxDesktop.__new__(OfflineLinuxDesktop)
    local.owner = "owner"
    requested = []
    local._request = lambda operation, args, **kwargs: requested.append((operation, args)) or {}
    for operation, args in operations.items():
        local.call("owner", operation, args)
    assert requested == list(operations.items())


def test_a_ref_target_is_sent_as_its_element_and_reviewed_as_observed():
    from test_bots_desktop_elements import ElementExecutor

    class Executor(ElementExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, features=("elements", "pointer"))

        def call(self, owner, operation, args, *, cancel):
            if operation != "pointer":
                return super().call(owner, operation, args, cancel=cancel)
            self.calls.append((operation, args))
            return {
                "dispatched": True,
                "verification_required": True,
                "observation": self.observe(args),
            }

    made = []
    manager = DesktopComputers(
        "owner", ComputerViews(), factory=lambda *a, **k: made.append(Executor(*a, **k)) or made[-1]
    )
    try:
        call(manager, "computer_run", {"command": "soffice"}, image_input=False)
        observed = call(manager, "computer_observe", image_input=False)
        assert '- check box "Αποθήκευση με κωδικό" [ref=e3]' in observed["element_view"]
        reviews = []
        moved = call(
            manager,
            "computer_pointer",
            {"source_id": observed["source_id"], "action": "drag", "ref": 3, "to_ref": 7},
            image_input=False,
            review=lambda *args, **kwargs: reviews.append(args),
        )
        bus = made[0].bus
        assert made[0].calls[-1] == (
            "pointer",
            {
                "action": "drag",
                "at": {"element": bus.element("Αποθήκευση με κωδικό")},
                "to": {"element": bus.element("Αποθήκευση")},
                "elements": True,
                "image": False,
            },
        )
        (_, _, context) = reviews[0]
        assert context["element"]["name"] == "Αποθήκευση με κωδικό"
        assert context["to_element"]["role"] == "push button" and "screenshot" not in context
        assert "element_view" in moved and moved["dispatched"] is True
    finally:
        manager.close()
