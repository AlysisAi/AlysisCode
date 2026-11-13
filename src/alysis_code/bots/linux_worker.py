"""Trusted Linux controller, with per-bot workspaces and private session state.

Only this controller receives its API capability through stdin. Each command runs
in a networkless bubblewrap namespace containing its one workspace. No Docker
socket, host project directory or provider credential enters this service.
"""

from __future__ import annotations

import fcntl
import json
import re
import signal
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from .coding import WORKER_TIMEOUT
from .coding_worker import Broker, ScopedLinuxRunner, execute


def workspace_paths(root: Path, bot_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[a-f0-9]{32}", bot_id):
        raise ValueError("Invalid managed bot identity")
    paths = root / "workspaces" / bot_id, root / "sessions" / bot_id
    for path in paths:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Managed workspace/state roots cannot be links")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Managed path escaped its volume")
    return paths


def main():
    raw = sys.stdin.buffer.readline(16385)
    if len(raw) > 16384:
        raise SystemExit("Invalid Linux worker bootstrap")
    settings = json.loads(raw)
    broker = Broker(settings["url"], settings.pop("token"), uuid4().hex)
    root = Path("/var/lib/alysis/coding")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    known: dict[str, tuple[Path, Path]] = {}
    known_lock = threading.Lock()
    failed = threading.Event()

    def provision_and_heartbeat():
        pending_since: dict[str, float] = {}
        while not stop.is_set():
            try:
                for bot in broker.post("managed-bots"):
                    with known_lock:
                        exists = bot["id"] in known
                    if not exists:
                        paths = workspace_paths(root, bot["id"])
                        response = broker.client.post(
                            f"/internal/coding/managed-bots/{bot['id']}/register",
                            json={
                                "worker_id": broker.worker_id,
                                "id": bot["id"],
                                "name": bot["name"],
                            },
                            timeout=3,
                        )
                        if not response.is_success:
                            started = pending_since.setdefault(bot["id"], time.monotonic())
                            if (
                                response.status_code == 409
                                and time.monotonic() - started <= WORKER_TIMEOUT + 2
                            ):
                                continue  # Only registration may wait for a dead owner's lease.
                            raise RuntimeError("Managed workspace registration failed")
                        with known_lock:
                            known[bot["id"]] = paths
                        pending_since.pop(bot["id"], None)
                with known_lock:
                    ids = list(known)
                for bot_id in ids:
                    broker.post(f"workspaces/{bot_id}/heartbeat")
            except Exception:
                failed.set()
                stop.set()  # Loss of ownership stops work, never silently reconnects.
            stop.wait(1)

    with (root / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        beat = threading.Thread(target=provision_and_heartbeat, daemon=True)
        beat.start()
        print("Linux coding controller ready; isolated persistent workspaces.", flush=True)
        try:
            while not stop.is_set():
                with known_lock:
                    bots = list(known)
                for bot_id in bots:
                    if stop.is_set():
                        break
                    job = broker.post(f"workspaces/{bot_id}/claim")
                    if not job:
                        continue
                    workspace, state = known[bot_id]
                    runner = ScopedLinuxRunner(workspace=workspace, close_stdin=True)
                    result = execute(
                        job,
                        workspace=workspace,
                        state=state,
                        broker=broker,
                        settings=settings,
                        stop=stop,
                        runner=runner,
                    )
                    broker.post(f"jobs/{job['id']}/result", **result)
                stop.wait(0.5)
        finally:
            stop.set()
            beat.join(timeout=4)
            broker.client.close()
        if failed.is_set():
            raise SystemExit(
                "Linux coding controller lost its service connection or workspace lease; work stopped."
            )


if __name__ == "__main__":
    main()
