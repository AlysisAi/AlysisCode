"""Independent exact-frame/cancellation review; synthetic pixels, real child IPC."""

import base64
import time
from pathlib import Path

import pytest
from test_bots_desktop_linux import pipe_computer as pipe_computer
from test_bots_desktop_linux import worker as worker

from alysis_code.bots.desktop_worker import Computer, Rejected
from alysis_code.cancellation import CooperativeCancellationError


@pytest.mark.parametrize("invalid", ["wrong_id", "expired"])
def test_invalid_frame_is_consumed_without_capture_or_input(worker, invalid):
    if invalid == "expired":
        worker.frame = ("frame", time.monotonic() - 61, b"pixels")

    def capture():
        raise AssertionError("Invalid frame reached capture")

    worker.capture = capture
    with pytest.raises(Rejected, match="expired"):
        worker.dispatch(
            "type", {"frame_id": "wrong" if invalid == "wrong_id" else "frame", "text": "text"}
        )
    assert worker.calls == [] and worker.frame is None


def test_nonreturning_pixels_reject_in_bounded_time_and_cannot_replay(worker, monkeypatch):
    now = [100.0]
    worker.frame = ("frame", 99.0, b"pixels")
    worker.capture = lambda: b"different"
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(Rejected, match="pixels changed"):
        worker.dispatch("type", {"frame_id": "frame", "text": "text"})
    assert 1.2 <= now[0] - 100 < 1.3
    worker.capture = lambda: b"pixels"
    with pytest.raises(Rejected, match="expired"):
        worker.dispatch("type", {"frame_id": "frame", "text": "text"})
    assert worker.calls == [] and worker.frame is None


def test_continuously_animated_observation_returns_last_actual_capture_within_bound(
    worker, monkeypatch
):
    now = [100.0]
    samples = []

    def capture():
        now[0] += 0.01
        samples.append(str(len(samples)).encode())
        return samples[-1]

    worker.capture = capture
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    result = Computer.observe(worker)
    assert 0.3 <= now[0] - 100 < 0.4
    assert base64.b64decode(result["png"]) == worker.frame[2] == samples[-1]


@pytest.mark.parametrize("cancelled", [False, True])
def test_parent_cancellation_reaps_child_while_exact_frame_return_is_pending(
    pipe_computer, tmp_path, cancelled
):
    ready = tmp_path / "waiting"
    dispatched = tmp_path / "dispatched"
    source = Path(__file__).parents[1] / "src"
    script = f"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, {str(source)!r})
from alysis_code.bots.desktop_worker import Computer
request = json.loads(sys.stdin.readline())
computer = Computer.__new__(Computer)
started = time.monotonic()
computer.frame = ('frame', started, b'original')
def capture():
    Path({str(ready)!r}).touch()
    return b'original' if time.monotonic() - started >= 0.5 else b'changed'
computer.capture = capture
computer.paste_text = lambda text: Path({str(dispatched)!r}).write_text(text)
computer.observe = lambda: {{'frame_id': 'next'}}
result = computer.dispatch(request['operation'], request['arguments'])
print(json.dumps({{'id': request['id'], 'result': result}}), flush=True)
time.sleep(30)
"""
    computer = pipe_computer(script)
    arguments = {"frame_id": "frame", "text": "actual fixture dispatch"}
    started = time.monotonic()
    if cancelled:
        with pytest.raises(CooperativeCancellationError):
            computer.call("owner", "type", arguments, cancel=lambda: ready.exists())
        assert ready.exists() and not dispatched.exists()
        assert computer._process.poll() is not None and computer._cleaned
        assert time.monotonic() - started < 2
    else:
        result = computer.call("owner", "type", arguments)
        assert result["dispatched"] and ready.exists()
        assert dispatched.read_text() == arguments["text"]
        assert time.monotonic() - started >= 0.5
    computer.close()
