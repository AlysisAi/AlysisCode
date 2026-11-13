"""Real transactional admission and expiry; no cloud allocation or model calls."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.hosted_maintenance import MaintenanceLease
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import Store


def queued(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Αθήνα 東京", purpose="Protect active work"))
    chat = store.create_chat("owner", bot["id"], "Maintenance")
    task = store.enqueue("owner", chat["id"], TaskInput(message="Do work", request_id=uuid4().hex))
    return store, bot, task


def test_queued_work_is_preserved_and_claims_resume_after_maintenance(tmp_path):
    store, _, task = queued(tmp_path)
    lease = MaintenanceLease(store)
    token = lease.acquire()
    assert token and store.claim("owner") is None
    assert store.task("owner", task["id"])["status"] == "queued"
    assert lease.acquire() is None
    lease.release("another-lease")
    assert store.claim("owner") is None
    lease.release(token)
    assert store.claim("owner")["id"] == task["id"]
    assert lease.acquire() is None


def test_background_work_and_uncertain_or_human_computers_defer_backup(tmp_path):
    store, bot, task = queued(tmp_path)
    active = store.claim("owner")
    background = BackgroundStore(store, "owner")
    background.submit(active, "Continue", [])
    lease = MaintenanceLease(store)
    token = lease.acquire()
    assert token and background.claim() is None
    lease.release(token)
    assert background.claim()["id"] == task["id"]
    assert lease.acquire() is None
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE background_turns SET state='computer_control' WHERE task_id=?", (task["id"],)
        )
        db.execute(
            "INSERT INTO computer_allocations VALUES(?,?,?,?)",
            (
                "owner",
                bot["id"],
                json.dumps({"state": "unknown"}),
                time.time(),
            ),
        )
    assert lease.acquire() is None
    with store.connect(write=True) as db:
        db.execute("UPDATE computer_allocations SET record=?", (json.dumps({"state": "deleted"}),))
    assert lease.acquire()


def test_expired_lease_cannot_clear_a_new_lease(tmp_path):
    store, _, _ = queued(tmp_path)
    now = time.time()
    lease = MaintenanceLease(store, clock=lambda: now)
    first = lease.acquire(seconds=1)
    now += 2
    second = lease.acquire(seconds=100)
    assert first != second and second
    lease.release(first)
    assert store.claim("owner") is None
    lease.release(second)
    assert store.claim("owner")


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_finished_background_queue_records_do_not_block_maintenance(tmp_path, status):
    store, _, task = queued(tmp_path)
    background = BackgroundStore(store, "owner")
    background.submit(store.claim("owner"), "Continue", [])
    assert background.claim()["id"] == task["id"]
    assert MaintenanceLease(store).acquire() is None
    # Background queue state is retained after task completion and recovery.
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task["id"]))
    assert MaintenanceLease(store).acquire()


def test_task_claim_and_maintenance_are_mutually_exclusive_under_contention(tmp_path):
    for index in range(12):
        store, _, _ = queued(tmp_path / str(index))
        barrier = threading.Barrier(2)

        def claim(store=store, barrier=barrier):
            barrier.wait()
            return store.claim("owner")

        def maintenance(store=store, barrier=barrier):
            barrier.wait()
            return MaintenanceLease(store).acquire()

        with ThreadPoolExecutor(max_workers=2) as pool:
            task = pool.submit(claim)
            backup = pool.submit(maintenance)
            assert bool(task.result()) != bool(backup.result())


@pytest.mark.parametrize("seconds", [0, -1, 2101, True, float("nan"), float("inf")])
def test_unbounded_or_invalid_maintenance_lease_is_rejected(tmp_path, seconds):
    store, _, _ = queued(tmp_path)
    with pytest.raises(ValueError):
        MaintenanceLease(store).acquire(seconds=seconds)
