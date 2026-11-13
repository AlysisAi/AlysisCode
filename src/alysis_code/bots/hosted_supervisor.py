"""Bounded process recovery for an already bootstrapped private controller.

Credentials remain in the parent process and reach each child through stdin.
This recovers child-process failure, not loss of this supervisor or a host reboot.
Worker epochs and uncertain-task recovery remain the controller's responsibility.
"""

from __future__ import annotations

import io
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress

from .hosted_azure import bootstrap


def _signal_group(process, signum):
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signum)


def supervise(
    command,
    payload,
    *,
    expires_at,
    stop,
    emit,
    max_restarts=3,
    restart_delay=65,
    shutdown_grace=30,
):
    """Run a private process with bounded retries, never overlapping children.

    The default retry delay outlasts the controller's 60-second worker lease.
    A clean exit, requested stop, expired deadline or exhausted allowance stops
    the supervisor. Exceptions and child output are not copied into receipts.
    """
    if (
        (
            expires_at is not None
            and (type(expires_at) not in (int, float) or not math.isfinite(expires_at))
        )
        or type(max_restarts) is not int
        or not 0 <= max_restarts <= 10
        or not 0 < restart_delay <= 300
        or not 0 < shutdown_grace <= 60
    ):
        raise ValueError("Invalid bounded process recovery settings.")
    for attempt in range(max_restarts + 1):
        if stop.is_set() or (expires_at is not None and time.time() >= expires_at):
            return 0
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            emit({"event": "controller_started", "pid": process.pid, "attempt": attempt})
            pending_input = payload
            while True:
                if stop.is_set() or (expires_at is not None and time.time() >= expires_at):
                    _signal_group(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=shutdown_grace)
                    except subprocess.TimeoutExpired:
                        _signal_group(process, signal.SIGKILL)
                        process.wait(timeout=5)
                    emit({"event": "controller_stopped", "attempt": attempt})
                    return 0
                try:
                    # communicate handles backpressure even if bootstrap exceeds
                    # pipe capacity or a faulty child never reads its stdin.
                    process.communicate(input=pending_input, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
        finally:
            # The child owns its process group. Reap it before another attempt,
            # including descendants left behind by an abruptly terminated child.
            _signal_group(process, signal.SIGKILL)
            process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
        emit({"event": "controller_exited", "attempt": attempt, "returncode": process.returncode})
        if process.returncode == 0:
            return 0
        if attempt == max_restarts:
            emit({"event": "restart_allowance_exhausted"})
            return 1
        delay = (
            restart_delay
            if expires_at is None
            else min(restart_delay, max(0, expires_at - time.time()))
        )
        if stop.wait(delay):
            return 0
    return 1


def main():
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        payload = sys.stdin.buffer.readline(16385)
        _, hosted = bootstrap(io.BytesIO(payload))
        result = supervise(
            [sys.executable, "-m", "alysis_code.bots.hosted_azure"],
            payload,
            expires_at=hosted.expires_at,
            stop=stop,
            emit=lambda event: print(json.dumps(event), flush=True),
        )
    except Exception:
        # Do not include raw configuration, upstream errors or child output.
        print('{"event":"supervisor_failed"}', flush=True)
        result = 1
    raise SystemExit(result)


if __name__ == "__main__":
    main()
