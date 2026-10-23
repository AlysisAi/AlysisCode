"""Run optional checkpoint filesystem work in a bounded, owned process.

A thread cannot cancel a blocked filesystem read or fsync. This worker has no
model, tools, or permission to execute workspace programs; it only runs the
checkpoint manager's snapshot/metadata operation. Its parent owns its lifetime.
"""

from __future__ import annotations

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


class CheckpointWorkerExpired(TimeoutError):
    def __init__(self, *, cleanup_pending: bool = False):
        super().__init__("checkpoint_time_budget_exhausted")
        self.cleanup_pending = cleanup_pending


def worker_command() -> list[str]:
    # -I excludes workspace/sitecustomize/PYTHONPATH injection. Explicitly select
    # this installation (also works with a source checkout or fresh wheel target).
    source = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from alysis_code.agent.checkpoint_worker import main; main()"
    )
    return [sys.executable, "-I", "-c", bootstrap, source]


def _kill_owned_worker(process: subprocess.Popen[str], *, stop_at: float) -> bool:
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
        output, error = process.communicate(
            json.dumps(payload, ensure_ascii=True),
            timeout=max(0.001, work_stop_at - time.monotonic()),
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
    # The JSON command comes only from the owning host process. Never accept a
    # function/module name or executable supplied by a task or workspace file.
    from ..checkpoint_settings import CheckpointSettings
    from ..session_artifacts import SessionArtifactLayout
    from .anytime_checkpoint import AnytimeCheckpointManager

    payload = json.load(sys.stdin)
    manager = AnytimeCheckpointManager(
        root=Path(payload["root"]),
        layout=SessionArtifactLayout(Path(payload["artifact_root"]), payload["locator_prefix"]),
        config=CheckpointSettings(**payload["config"]),
        excluded_paths=tuple(Path(item) for item in payload["excluded_paths"]),
    )
    manager._restore_worker_state(payload["state"])
    manager._io_stop_at = float(payload["stop_at"])
    manager._check_io_deadline()
    action = payload["action"]
    if action == "select_task":
        event = manager._select_task_inline(**payload["arguments"])
    elif action == "consider":
        arguments = payload["arguments"]
        arguments["accepted_commands"] = set(arguments["accepted_commands"])
        event = manager._consider_inline(**arguments)
    else:
        raise ValueError("unsupported_checkpoint_operation")
    manager._check_io_deadline()
    print(json.dumps({"state": manager._worker_state(), "event": event}, ensure_ascii=True))


if __name__ == "__main__":
    main()
