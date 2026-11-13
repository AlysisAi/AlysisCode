"""Real child-process recovery; synthetic credentials, no cloud or model calls."""

import hashlib
import json
import os
import signal
import sys
import threading
import time

import pytest

from alysis_code.bots.hosted_supervisor import supervise


@pytest.mark.parametrize("first_exit", [0, 7, -signal.SIGKILL])
@pytest.mark.parametrize("continuous", [False, True])
def test_actual_process_restart_reuses_private_bootstrap_without_logging_it(
    tmp_path, first_exit, continuous
):
    receipt = tmp_path / "receipts.jsonl"
    payload = b"synthetic-secret-bootstrap\n"
    program = """
import hashlib,json,os,signal,sys
from pathlib import Path
path=Path(sys.argv[1])
attempt=len(path.read_text().splitlines()) if path.exists() else 0
data=sys.stdin.buffer.readline()
with path.open('a') as out:
 out.write(json.dumps({'attempt':attempt,'hash':hashlib.sha256(data).hexdigest()})+'\\n')
if attempt==0:
 code=int(sys.argv[2])
 if code<0: os.kill(os.getpid(),-code)
 sys.exit(code)
"""
    events = []
    result = supervise(
        [sys.executable, "-c", program, str(receipt), str(first_exit)],
        payload,
        expires_at=None if continuous else time.time() + 5,
        stop=threading.Event(),
        emit=events.append,
        restart_delay=0.01,
    )
    assert result == 0
    rows = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert len(rows) == (1 if first_exit == 0 else 2)
    assert all(row["hash"] == hashlib.sha256(payload).hexdigest() for row in rows)
    assert payload.decode().strip() not in json.dumps(events)
    assert events[-1]["returncode"] == 0


@pytest.mark.parametrize("continuous", [False, True])
def test_crash_loop_exhausts_allowance_without_unbounded_restarts(continuous):
    events = []
    result = supervise(
        [sys.executable, "-c", "raise SystemExit(9)"],
        b"",
        expires_at=None if continuous else time.time() + 5,
        stop=threading.Event(),
        emit=events.append,
        restart_delay=0.01,
        max_restarts=2,
    )
    assert result == 1
    assert sum(e["event"] == "controller_started" for e in events) == 3
    assert events[-1]["event"] == "restart_allowance_exhausted"


@pytest.mark.parametrize(
    "request_stop,continuous",
    [(True, False), (False, False), (True, True)],
    ids=["pilot-stop", "pilot-expiry", "continuous-stop"],
)
def test_stop_and_expiry_kill_unresponsive_child_without_retry(request_stop, continuous):
    stop = threading.Event()
    events = []

    def record(event):
        events.append(event)
        if request_stop and event["event"] == "controller_started":
            stop.set()

    result = supervise(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        b"x" * 16000,  # A child that never reads must not block shutdown.
        expires_at=None if continuous else time.time() + 0.5,
        stop=stop,
        emit=record,
        restart_delay=0.01,
        shutdown_grace=0.1,
    )
    assert result == 0
    assert [e["event"] for e in events] == ["controller_started", "controller_stopped"]
    with pytest.raises(ProcessLookupError):
        os.kill(events[0]["pid"], 0)


def test_requested_stop_during_backoff_does_not_start_another_child():
    stop = threading.Event()
    events = []

    def record(event):
        events.append(event)
        if event["event"] == "controller_exited":
            stop.set()

    assert (
        supervise(
            [sys.executable, "-c", "raise SystemExit(1)"],
            b"",
            expires_at=time.time() + 5,
            stop=stop,
            emit=record,
            restart_delay=3,
        )
        == 0
    )
    assert len(events) == 2
