"""Offline desktop protocol failure tests; real namespaces are qualified in Linux QA."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from alysis_code.bots.desktop_linux import DesktopError, OfflineLinuxDesktop
from alysis_code.bots.desktop_worker import Computer, Rejected
from alysis_code.cancellation import CooperativeCancellationError


def test_first_command_receipt_carries_actual_bounded_package_metadata(monkeypatch):
    from types import SimpleNamespace

    from alysis_code.bots import desktop_worker as module

    names = ["図表", "reader-package", "third-package"]
    monkeypatch.setattr(
        module.importlib.metadata,
        "distributions",
        lambda: [SimpleNamespace(metadata={"Name": name}, version="1.2.3") for name in names],
    )
    computer = Computer.__new__(Computer)
    computer.run = lambda args, background=False: {"exit_code": 0, "output": "actual receipt"}
    first = computer.dispatch("run", {"command": "inspect"})
    assert {d["name"] for d in first["software"]["python_distributions"]} == set(names)
    assert first["software"]["complete"] is True
    assert first["output"] == "actual receipt"
    assert "software" not in computer.dispatch("run", {"command": "inspect again"})
    monkeypatch.setattr(
        module.importlib.metadata,
        "distributions",
        lambda: [
            SimpleNamespace(metadata={"Name": "package" + str(i)}, version="v" * 100)
            for i in range(500)
        ],
    )
    bounded = module.software_inventory()
    assert not bounded["complete"] and bounded["distribution_count"] == 500
    assert len(json.dumps(bounded)) < 14000


@pytest.mark.parametrize("chunks", [(b"12\n",), (b"1", b"2", b"\n"), (b"12", b"")])
def test_display_startup_requires_complete_line_before_closing_pipe(monkeypatch, chunks):
    from types import SimpleNamespace

    import alysis_code.bots.desktop_worker as worker_module

    unread = list(chunks)
    closed = []
    monkeypatch.setattr(worker_module.os, "pipe", lambda: (101, 102))
    monkeypatch.setattr(worker_module.os, "close", closed.append)
    monkeypatch.setattr(worker_module.os, "read", lambda *_: unread.pop(0))
    monkeypatch.setattr(worker_module.select, "select", lambda *args: ([101], [], []))

    def start(argv, **kwargs):
        if "openbox" in " ".join(argv):
            # The display's complete record must arrive before launching a client.
            assert not unread and 101 in closed
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(worker_module.subprocess, "Popen", start)
    monkeypatch.setattr(
        worker_module.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"0\n"),
    )
    if chunks[-1] == b"":
        with pytest.raises(Rejected, match="did not start"):
            Computer()
    else:
        assert Computer().display == ":12"
    assert not unread and set(closed) == {101, 102}


@pytest.fixture
def pipe_computer():
    computers = []

    def make(script):
        computer = OfflineLinuxDesktop.__new__(OfflineLinuxDesktop)
        computer.owner = "owner"
        computer._closed = computer._cleaned = False
        computer._stop = threading.Event()
        computer._lock = threading.RLock()
        computer._view_lock = threading.Lock()
        computer._buffer = b""
        computer._directory = tempfile.TemporaryDirectory()
        computer._screen_directory = Path(computer._directory.name) / "screen"
        computer._screen_directory.mkdir()
        computer.session_id = uuid4().hex
        computer._process = subprocess.Popen(
            [sys.executable, "-I", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
        computers.append(computer)
        return computer

    yield make
    for computer in computers:
        computer.close()


def test_wrong_owner_cannot_send_a_request(pipe_computer):
    computer = pipe_computer("import time; time.sleep(30)")
    with pytest.raises(DesktopError, match="another owner"):
        computer.call("other", "observe", {})
    assert computer.alive()


@pytest.mark.parametrize(
    "response",
    [
        'print(\'{"id":"wrong","result":{}}\', flush=True)',
        "print('x' * 2100000, flush=True)",
        "print('not-json', flush=True)",
        "pass",
    ],
)
def test_bad_transport_retires_the_computer(pipe_computer, response):
    computer = pipe_computer("import sys; sys.stdin.readline(); " + response)
    with pytest.raises((DesktopError, ValueError)):
        computer.call("owner", "observe", {})
    assert not computer.alive() and computer._cleaned


def test_timeout_closes_child_and_cannot_receive_late_success(pipe_computer):
    computer = pipe_computer("import sys,time; sys.stdin.readline(); time.sleep(30)")
    with pytest.raises(DesktopError, match="timed out"):
        computer._request("observe", {}, cancel=lambda: False, timeout=0.1)
    assert computer._process.poll() is not None
    with pytest.raises(DesktopError, match="closed"):
        computer.call("owner", "observe", {})


@pytest.mark.parametrize("cancelled", [False, True])
def test_full_input_pipe_remains_cancellable_and_deadline_bounded(pipe_computer, cancelled):
    computer = pipe_computer("import time; time.sleep(30)")
    descriptor = computer._process.stdin.fileno()
    os.set_blocking(descriptor, False)
    for _ in range(256):
        try:
            os.write(descriptor, b"x" * 8192)
        except BlockingIOError:
            break
    else:
        pytest.fail("Fixture did not fill its actual pipe")
    started = time.monotonic()
    error = CooperativeCancellationError if cancelled else DesktopError
    with pytest.raises(error):
        computer._request(
            "type",
            {"text": "Ω" * 7000},
            timeout=0.2,
            cancel=lambda: cancelled and time.monotonic() - started > 0.05,
        )
    assert time.monotonic() - started < 2
    assert computer._cleaned and not computer.alive()


@pytest.mark.parametrize("fatal", [False, True])
def test_safe_rejection_keeps_session_but_uncertain_executor_failure_retires_it(
    pipe_computer, fatal
):
    script = (
        "import sys,json,time; request=json.loads(sys.stdin.readline()); "
        f"print(json.dumps({{'id':request['id'],'error':'Operation rejected','fatal':{fatal}}}),flush=True); "
        "time.sleep(30)"
    )
    computer = pipe_computer(script)
    with pytest.raises(DesktopError, match="rejected"):
        computer.call("owner", "observe", {})
    assert computer.alive() is not fatal


def test_stop_interrupts_in_flight_operation_and_viewer_never_queues(pipe_computer):
    from alysis_code.bots.screen_channel import ScreenServer

    computer = pipe_computer("import sys,time; sys.stdin.readline(); time.sleep(30)")
    screen = ScreenServer(computer._screen_directory, computer.session_id)
    screen.bind(lambda: b"fresh pixels")
    errors = []

    def run():
        try:
            computer.call("owner", "run", {"command": "sleep 30"})
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.1)
    start = time.monotonic()
    try:
        assert computer.capture() == b"fresh pixels"
        # Only another viewer capture contends; the pending command does not.
        with computer._view_lock:
            with pytest.raises(BlockingIOError, match="already running"):
                computer.capture()
        computer.close()
    finally:
        screen.close()
    thread.join(timeout=2)
    assert time.monotonic() - start < 2
    assert not thread.is_alive() and isinstance(errors[0], CooperativeCancellationError)


@pytest.fixture
def worker():
    worker = Computer.__new__(Computer)
    worker.frame = ("frame", time.monotonic(), b"pixels")
    worker.capture = lambda: b"pixels"
    worker.calls = []
    worker.xdo = lambda *args, **kwargs: worker.calls.append((args, kwargs))
    worker.move_pointer = lambda x, y: worker.calls.append(("move", x, y))
    worker.paste_text = lambda text: worker.calls.append(("paste", text))
    worker.observe = lambda: {"frame_id": "new", "png": "image"}
    return worker


def test_frame_changes_and_replay_reject_before_desktop_input(worker):
    worker.capture = lambda: b"changed"
    with pytest.raises(Rejected, match="pixels changed"):
        worker.dispatch("click", {"frame_id": "frame", "x": 20, "y": 20})
    assert worker.calls == [] and worker.frame is None
    with pytest.raises(Rejected, match="expired"):
        worker.dispatch("type", {"frame_id": "frame", "text": "Text"})
    assert worker.calls == []


def test_periodic_pixels_must_return_exactly_before_input(worker, monkeypatch):
    pixels = iter([b"blink", b"blink", b"pixels"])
    worker.capture = lambda: next(pixels)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    result = worker.dispatch("click", {"frame_id": "frame", "x": 20, "y": 20})
    assert result["dispatched"] and worker.frame is None


def test_waiting_for_pixels_never_extends_original_frame_age(worker, monkeypatch):
    now = [100.0]
    worker.frame = ("frame", 40.02, b"pixels")
    worker.capture = lambda: b"pixels" if now[0] > 100 else b"blink"
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(Rejected, match="pixels changed"):
        worker.dispatch("click", {"frame_id": "frame", "x": 20, "y": 20})
    assert worker.calls == [] and worker.frame is None


def test_observation_waits_for_actual_focus_paint_without_combining_frames(worker, monkeypatch):
    pixels = iter([b"before", b"after", b"after"])
    worker.capture = lambda: next(pixels)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    observation = Computer.observe(worker)
    assert worker.frame[2] == b"after"
    import base64

    assert base64.b64decode(observation["png"]) == b"after"


def test_coordinate_input_releases_mouse_even_when_press_fails(worker):
    def xdo(*args, **kwargs):
        worker.calls.append(args)
        if args[0] == "mousedown":
            raise RuntimeError("input channel failed")

    worker.xdo = xdo
    with pytest.raises(RuntimeError):
        worker.dispatch("click", {"frame_id": "frame", "x": 20, "y": 20})
    assert worker.calls[-1] == ("mouseup", "1")
    assert worker.frame is None


@pytest.mark.parametrize("operation", ["click", "scroll"])
def test_unconfirmed_pointer_target_rejects_click_or_scroll(worker, monkeypatch, operation):
    worker.env = {}
    worker.move_pointer = Computer.move_pointer.__get__(worker)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, b"X=40\nY=50\n"),
    )
    with pytest.raises(Rejected, match="no click or scroll"):
        worker.dispatch(operation, {"frame_id": "frame", "x": 20, "y": 20, "ticks": 3})
    assert worker.calls == [] and worker.frame is None


def test_post_action_capture_failure_preserves_dispatch_receipt(worker):
    def fail():
        raise RuntimeError("capture failed")

    worker.observe = fail
    result = worker.dispatch("type", {"frame_id": "frame", "text": "Ω — 日本語"})
    assert result == {
        "dispatched": True,
        "verification_required": True,
        "observation_unavailable": True,
    }
    assert worker.calls[0] == ("paste", "Ω — 日本語")


@pytest.mark.parametrize(
    "operation,args",
    [
        ("click", {"x": True, "y": 2}),
        ("click", {"x": 1280, "y": 2}),
        ("click", {"x": 2, "y": -1}),
        ("scroll", {"x": 2, "y": 2, "ticks": 21}),
        ("type", {"text": "\0"}),
        ("type", {"text": "a" * 8001}),
    ],
)
def test_invalid_input_does_not_consume_frame_or_dispatch(worker, operation, args):
    with pytest.raises(Rejected):
        worker.dispatch(operation, {"frame_id": "frame", **args})
    assert worker.frame is not None and worker.calls == []


def test_exit_between_poll_and_signal_is_accepted_only_after_reaping(pipe_computer, monkeypatch):
    computer = pipe_computer("import time; time.sleep(30)")
    kill = os.killpg

    def exit_then_denied(pid, sig):
        kill(pid, sig)
        computer._process.wait(timeout=2)
        raise PermissionError("Process group disappeared")

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", exit_then_denied)
        computer.close()
    assert computer._cleaned and computer._process.poll() is not None


def test_denied_signal_with_live_child_retains_cleanup_handles(pipe_computer, monkeypatch):
    computer = pipe_computer("import time; time.sleep(30)")

    def denied(*args):
        raise PermissionError("Synthetic denied signal")

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", denied)
        with pytest.raises(DesktopError, match="not confirmed"):
            computer.close()
    assert not computer._cleaned and computer._process.poll() is None
    computer.close()
    assert computer._cleaned and computer._process.poll() is not None


@pytest.mark.parametrize("recovers", [True, False])
def test_window_manager_probe_timeout_keeps_bounded_startup_budget(monkeypatch, recovers):
    from types import SimpleNamespace

    import alysis_code.bots.desktop_worker as module

    now, timeouts = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.os, "pipe", lambda: (101, 102))
    monkeypatch.setattr(module.os, "close", lambda _: None)
    monkeypatch.setattr(module.os, "read", lambda *_: b"12\n")
    monkeypatch.setattr(module.select, "select", lambda *args: ([101], [], []))
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda *a, **k: SimpleNamespace(poll=lambda: None)
    )

    def probe(argv, **kwargs):
        if argv[:2] != ["/usr/bin/xdotool", "get_desktop"]:
            # The wallpaper and the dock come after the window manager is ready;
            # the budget under test is the readiness probe's.
            return SimpleNamespace(returncode=0, stdout=b"")
        timeouts.append(kwargs["timeout"])
        if recovers and len(timeouts) > 1:
            return SimpleNamespace(returncode=0, stdout=b"0\n")
        now[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", probe)
    if recovers:
        assert Computer().display == ":12"
        assert len(timeouts) == 2
    else:
        with pytest.raises(Rejected, match="window manager did not become ready"):
            Computer()
        assert now[0] == 5
        assert len(timeouts) == 5
    assert all(0 < limit <= 1 for limit in timeouts)


def test_truncated_output_preserves_receipt_if_child_already_exited(monkeypatch):
    from types import SimpleNamespace

    import alysis_code.bots.desktop_worker as module

    waited, closed = [], []
    process = SimpleNamespace(
        pid=42,
        returncode=0,
        stdout=SimpleNamespace(fileno=lambda: 101, close=lambda: closed.append(True)),
        poll=lambda: 0,
        wait=lambda **kw: waited.append(kw),
    )
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(module.select, "select", lambda *a: ([process.stdout], [], []))
    monkeypatch.setattr(module.os, "read", lambda *a: b"x" * module.MAX_OUTPUT)

    def exited(*args):
        raise ProcessLookupError

    monkeypatch.setattr(module.os, "killpg", exited)
    computer = Computer.__new__(Computer)
    computer.env = {}
    result = computer.run({"command": "synthetic bounded output"})
    assert result["exit_code"] == 0 and result["output_truncated"]
    assert len(result["output"]) == module.MAX_OUTPUT
    assert waited == [{"timeout": 3}] and closed == [True]


@pytest.mark.parametrize("running", [True, False])
def test_selective_stop_never_escalates_and_confirms_actual_exit(monkeypatch, running):
    from types import SimpleNamespace

    from alysis_code.bots import desktop_worker as module

    signals = []
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    def wait(timeout):
        raise subprocess.TimeoutExpired("owned helper", timeout)

    computer = Computer.__new__(Computer)
    computer.frame = "prior-frame"
    computer.jobs = {
        "owned": SimpleNamespace(pid=123, poll=lambda: None if running else 0, wait=wait)
    }
    result = computer.dispatch("stop_process", {"process_id": "owned"})
    assert result["running"] is running and result["stopped"] is not running
    assert result["termination_requested"] is running
    assert signals == ([(123, module.signal.SIGTERM)] if running else [])
    assert computer.frame is None
    with pytest.raises(Rejected, match="not found"):
        computer.dispatch("stop_process", {"process_id": "unowned"})
