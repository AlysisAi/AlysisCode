"""A prompt whose owner process is gone is reclaimed at once, not after its lease.

A bridge holds its owner file (``hold_owner``) for as long as its process runs.
When the process ends, however it ends (a desktop host stopping it after a Stop
it could not confirm, or a crash), the operating system frees the lock, and the
next engine reclaims the prompt the gone one was running instead of waiting up
to ``DEFAULT_LEASE_SECONDS`` for its lease to run out.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import alysis_code
from alysis_code.ide.prompt_queue import DurablePromptQueue, PromptState

_SESSION = "session-1"
_SOURCE_DIR = Path(alysis_code.__file__).resolve().parents[1]

# Claims the session's next prompt as owner-a, says so, then waits to be killed.
_OWNER_A = """
import sys, time
from alysis_code.ide.prompt_queue import DurablePromptQueue
queue = DurablePromptQueue(sys.argv[1])
held = queue.hold_owner("owner-a")
lease = queue.claim_next(session_id=sys.argv[2], owner_id="owner-a", lease_seconds=120)
print("claimed" if held and lease is not None else "failed", flush=True)
time.sleep(60)
"""


def _queue_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "prompt-queue.sqlite3"


def _enqueue(queue: DurablePromptQueue, key: str, *, session: str = _SESSION) -> None:
    queue.enqueue(session_id=session, idempotency_key=key, payload={"message": key})


def _start_owner_a(path: Path, session: str = _SESSION) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(_SOURCE_DIR), os.environ.get("PYTHONPATH", "")]),
    }
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _OWNER_A, str(path), session],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "claimed"
    return process


def _kill(process: subprocess.Popen[str]) -> None:
    process.kill()
    process.wait(timeout=30)
    if process.stdout is not None:
        process.stdout.close()


def test_a_prompt_whose_owner_process_is_gone_is_reclaimed_at_once(tmp_path: Path) -> None:
    path = _queue_path(tmp_path)
    queue = DurablePromptQueue(path)
    _enqueue(queue, "first")
    owner_a = _start_owner_a(path)
    try:
        [running] = queue.list(session_id=_SESSION, states=[PromptState.RUNNING])
        # While owner-a runs, its lease holds.
        assert not queue.is_reclaimable(session_id=_SESSION, prompt_id=running.prompt_id)
        assert queue.claim_next(session_id=_SESSION, owner_id="owner-b") is None
    finally:
        _kill(owner_a)

    started = time.monotonic()
    assert queue.is_reclaimable(session_id=_SESSION, prompt_id=running.prompt_id)
    lease = queue.claim_next(session_id=_SESSION, owner_id="owner-b")
    assert lease is not None
    assert lease.item.prompt_id == running.prompt_id
    assert lease.item.attempts == 2
    # Well inside the 120 s lease the gone owner had.
    assert time.monotonic() - started < 10


def test_a_resumed_session_recovers_a_gone_owners_prompt(tmp_path: Path) -> None:
    path = _queue_path(tmp_path)
    queue = DurablePromptQueue(path)
    _enqueue(queue, "first", session="old-session")
    owner_a = _start_owner_a(path, session="old-session")
    _kill(owner_a)

    moved = queue.rebind_recoverable(source_session_id="old-session", target_session_id=_SESSION)
    assert moved == {"pending": 0, "recovered": 1, "active": 0}
    [pending] = queue.list(session_id=_SESSION, states=[PromptState.PENDING])
    assert pending.idempotency_key == "first"


def test_an_owner_that_holds_no_file_keeps_its_lease(tmp_path: Path) -> None:
    # An older engine never holds a file: its lease runs out as before.
    path = _queue_path(tmp_path)
    older = DurablePromptQueue(path)
    _enqueue(older, "first")
    lease = older.claim_next(session_id=_SESSION, owner_id="older-engine")
    assert lease is not None

    newer = DurablePromptQueue(path)
    assert not newer.is_reclaimable(session_id=_SESSION, prompt_id=lease.item.prompt_id)
    assert newer.claim_next(session_id=_SESSION, owner_id="owner-b") is None


def test_an_owner_held_in_this_process_is_alive_for_every_queue(tmp_path: Path) -> None:
    path = _queue_path(tmp_path)
    holder = DurablePromptQueue(path)
    assert holder.hold_owner("owner-a")
    _enqueue(holder, "first")
    lease = holder.claim_next(session_id=_SESSION, owner_id="owner-a")
    assert lease is not None

    other = DurablePromptQueue(path)
    assert not other.is_reclaimable(session_id=_SESSION, prompt_id=lease.item.prompt_id)
    assert other.claim_next(session_id=_SESSION, owner_id="owner-b") is None

    # A bridge that closes releases its owner; what it was running is reclaimable.
    holder.release_owner("owner-a")
    assert other.is_reclaimable(session_id=_SESSION, prompt_id=lease.item.prompt_id)
    reclaimed = other.claim_next(session_id=_SESSION, owner_id="owner-b")
    assert reclaimed is not None and reclaimed.item.prompt_id == lease.item.prompt_id


def test_old_files_of_gone_owners_are_removed(tmp_path: Path) -> None:
    path = _queue_path(tmp_path)
    queue = DurablePromptQueue(path)
    owners = path.parent / "prompt-queue-owners"
    owners.mkdir(parents=True, exist_ok=True)
    old = owners / "0123456789abcdef0123456789abcdef.lock"
    old.write_bytes(b"")
    two_hours_ago = time.time() - 2 * 60 * 60
    os.utime(old, (two_hours_ago, two_hours_ago))
    recent = owners / "fedcba9876543210fedcba9876543210.lock"
    recent.write_bytes(b"")

    assert queue.hold_owner("owner-a")
    assert not old.exists()
    assert recent.exists()
    queue.release_owner("owner-a")
