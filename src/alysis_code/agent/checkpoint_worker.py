"""Run optional checkpoint filesystem work in a bounded, owned process.

A thread cannot cancel a blocked filesystem read or fsync. This worker has no
model, tools, or permission to execute workspace programs; it only runs the
checkpoint manager's snapshot/metadata operation. Its parent owns its lifetime.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from ..command_text import command_text
from ..process_reaping import start_windows_command_cleanup


class CheckpointWorkerExpired(TimeoutError):
    def __init__(self, *, cleanup_pending: bool = False):
        super().__init__("checkpoint_time_budget_exhausted")
        self.cleanup_pending = cleanup_pending


def worker_command() -> list[str]:
    # This child needs only the standard library and this exact installation.
    # Use the running interpreter's base executable to avoid a Windows venv
    # redirector process, and disable site initialization with -S. -I also
    # excludes workspace/PYTHONPATH injection. No PATH lookup or user-selected
    # interpreter participates in the worker's bootstrap.
    source = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from alysis_code.agent.checkpoint_io import main; main()"
    )
    executable = getattr(sys, "_base_executable", None) or sys.executable
    return [executable, "-I", "-S", "-c", bootstrap, source]


def _kill_owned_worker(process: subprocess.Popen[str], *, stop_at: float) -> bool:
    if os.name == "nt":
        cleanup = start_windows_command_cleanup(process)
        # The shared owner also closes reader-held pipes. Avoid competing with
        # it below, including when a failed thread start leaves cleanup pending.
        process._alysis_checkpoint_cleanup = cleanup
        if not cleanup.thread_started:
            return False
        return cleanup.finished.wait(max(0.0, stop_at - time.monotonic())) and cleanup.clean
    children: list[psutil.Process] = []
    inventory_complete = True
    try:
        running = process.poll() is None
    except OSError:
        running = True
        inventory_complete = False
    if running:
        try:
            leader = psutil.Process(process.pid)
            children = leader.children(recursive=True)
        except (psutil.Error, OSError):
            inventory_complete = False
        for child in reversed(children):
            try:
                child.kill()
            except (psutil.Error, OSError):
                pass  # Only the final wait can establish whether it exited.
        try:
            process.kill()
        except OSError:
            pass
    else:
        # An incomplete communicate can outlive the launcher when a descendant
        # retains its pipes. A vanished launcher is not a complete inventory.
        inventory_complete = False
    try:
        process.wait(timeout=max(0.0, stop_at - time.monotonic()))
    except (subprocess.TimeoutExpired, OSError):
        return False
    if children:
        try:
            _, pending = psutil.wait_procs(children, timeout=max(0.0, stop_at - time.monotonic()))
        except (psutil.Error, OSError):
            return False
        return inventory_complete and not pending
    return inventory_complete


def _close_worker_streams(process: subprocess.Popen[str], *, deferred: bool) -> None:
    if getattr(process, "_alysis_checkpoint_cleanup", None) is not None:
        return  # Ownership has transferred to the retained Windows cleanup task.

    def close() -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    if deferred:
        # On Windows communicate owns reader threads holding buffered stream
        # locks. Closing from this thread can block until an unkillable worker
        # or pipe-owning descendant exits. Retain the process/streams in a
        # daemon closer; this cleanup must not extend the host's deadline.
        threading.Thread(target=close, name="alysis-checkpoint-pipes", daemon=True).start()
    else:
        close()


def _communicate_worker(
    process: subprocess.Popen[str], payload: str, *, work_stop_at: float
) -> tuple[str, str]:
    if os.name != "nt":
        return process.communicate(payload, timeout=max(0.001, work_stop_at - time.monotonic()))

    # Windows communicate() writes stdin synchronously before checking its
    # timeout. A child stalled before reading a full pipe would therefore block
    # the caller and prevent deadline cleanup. Transfer this stream to an owned
    # writer while communicate manages only the output readers. The writer
    # retains and closes its stream even if cancellation outlives this call.
    stream = process.stdin
    if stream is None:
        raise ValueError("checkpoint_worker_missing_input_stream")
    finished = threading.Event()
    errors: list[BaseException] = []

    def write_input() -> None:
        try:
            for operation in (lambda: stream.write(payload), stream.close):
                try:
                    operation()
                except BrokenPipeError:
                    pass  # communicate tolerates an early closed child stdin.
                except OSError as exc:
                    if exc.errno != errno.EINVAL:
                        errors.append(exc)
                except BaseException as exc:
                    errors.append(exc)
        finally:
            finished.set()

    writer = threading.Thread(target=write_input, name="alysis-checkpoint-input", daemon=True)
    process.stdin = None
    try:
        writer.start()
    except BaseException:
        process.stdin = stream
        raise
    output, error = process.communicate(timeout=max(0.001, work_stop_at - time.monotonic()))
    # A real successful worker reads JSON through EOF before responding. Keep
    # this invariant explicit and bound even the writer's final scheduling delay.
    if not finished.wait(timeout=max(0.0, work_stop_at - time.monotonic())):
        raise subprocess.TimeoutExpired(process.args, timeout=0)
    if errors and process.returncode == 0:
        raise errors[0]
    return output, error


def run_checkpoint_worker(payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    if timeout <= 0:
        raise CheckpointWorkerExpired()
    stop_at = time.monotonic() + timeout
    # Cleanup consumes this operation's allowance, rather than extending the
    # user's remaining deadline with an additional shutdown grace period.
    reserve = min(0.15, timeout * 0.2)
    work_stop_at = stop_at - reserve
    payload = {**payload, "stop_at": work_stop_at}
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(
        worker_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **command_text(),
        **options,
    )
    communication_finished = False
    try:
        output, error = _communicate_worker(
            process,
            json.dumps(payload, ensure_ascii=True),
            work_stop_at=work_stop_at,
        )
        communication_finished = True
        if time.monotonic() >= work_stop_at:
            raise CheckpointWorkerExpired()
        if process.returncode:
            raise ValueError(f"checkpoint_worker_failed: {error[-1000:]}")
        result = json.loads(output)
        if not isinstance(result, dict) or "state" not in result:
            raise ValueError("checkpoint_worker_invalid_result")
        return result
    except subprocess.TimeoutExpired:
        clean = _kill_owned_worker(process, stop_at=stop_at)
        raise CheckpointWorkerExpired(cleanup_pending=not clean) from None
    except BaseException as exc:
        if not communication_finished:
            clean = _kill_owned_worker(process, stop_at=stop_at)
            if not clean:
                exc.cleanup_pending = True
        raise
    finally:
        _close_worker_streams(process, deferred=not communication_finished)


def main() -> None:
    # Compatibility entrypoint; the owned child imports only the I/O module.
    from .checkpoint_io import main as run

    run()


if __name__ == "__main__":
    main()
