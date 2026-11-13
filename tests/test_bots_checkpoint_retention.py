"""Checkpoint retention semantics with real SQLite/local bytes, no model calls.

Task execution is a fixture: public store/workspace methods create the records,
then explicit timestamps, tie-break IDs and active states model retention ages.
"""

import time
from contextlib import contextmanager
from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.retention import ObjectRetention, RetentionPolicy
from alysis_code.bots.store import ACTIVE, Conflict, Store
from alysis_code.bots.workspace_objects import WorkspaceObjects

POLICY = RetentionPolicy(300, 300, 2, 300)


@pytest.fixture
def lab(tmp_path):
    objects = LocalObjectStorage(tmp_path / "objects")
    return Store(tmp_path / "db", objects=objects), objects, time.time()


def bot(store, owner="owner"):
    return store.save_bot(owner, BotInput(name="Checkpoints", purpose="Synthetic retention"))["id"]


def checkpoint(store, bot_id, label, status, created_at, *, owner="owner"):
    data = f"{owner}/{label}: Αθήνα 東京".encode()
    workspace = WorkspaceObjects(store)
    task_id = None
    if status is None:
        ref = workspace.import_baseline(owner, bot_id, [data], len(data))
    else:
        chat = store.create_chat(owner, bot_id, "Continuous")
        task_id = store.enqueue(
            owner, chat["id"], TaskInput(message="Fixture checkpoint", request_id=uuid4().hex)
        )["id"]
        assert store.claim(owner)["id"] == task_id
        ref = workspace.stage(owner, bot_id, task_id, [data], len(data))
        store.finish(
            owner, task_id, "completed" if status in ACTIVE else status, publish_reply=False
        )
    row_id = bot_id + "-" + label
    with store.connect(write=True) as db:
        # Fixture age/order only, not production timestamp manipulation.
        db.execute(
            "UPDATE workspace_objects SET id=?,created_at=? WHERE ref=?",
            (row_id, created_at, ref.encode()),
        )
        if status in ACTIVE:
            db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    return {"id": row_id, "ref": ref, "data": data, "task_id": task_id}


def pointers(store, bot_id):
    with store.connect() as db:
        return {
            row[0]
            for row in db.execute("SELECT id FROM workspace_objects WHERE bot_id=?", (bot_id,))
        }


def assert_bytes(objects, items):
    for item in items:
        assert b"".join(objects.read(item["ref"])) == item["data"]


@pytest.mark.parametrize("baseline_first", [True, False])
def test_timestamp_ties_rank_completed_and_baseline_without_counting_failed_tasks(
    lab, baseline_first
):
    store, objects, now = lab
    target = bot(store)
    baseline = checkpoint(
        store, target, "z-baseline" if baseline_first else "a-baseline", None, now - 1000
    )
    newest = checkpoint(store, target, "y-completed", "completed", now - 1000)
    older = checkpoint(store, target, "x-completed", "completed", now - 1000)
    failed = [
        checkpoint(store, target, "zz-" + status, status, now - 1000)
        for status in ("failed", "cancelled", "interrupted")
    ]
    retained = [baseline, newest] if baseline_first else [newest, older]
    removed = [older if baseline_first else baseline, *failed]
    cleaner = ObjectRetention(store, "owner", POLICY)
    assert cleaner.sweep(now=now) == {"deleted": 0, "pending": 0}
    assert pointers(store, target) == {item["id"] for item in retained}
    assert WorkspaceObjects(store).current("owner", target) == retained[0]["ref"]
    assert_bytes(objects, retained + removed)
    assert cleaner.sweep(now=now + 300) == {"deleted": len(removed), "pending": 0}
    assert_bytes(objects, retained)
    assert all(not (objects.root / item["ref"].key).exists() for item in removed)


@pytest.mark.parametrize("status", ACTIVE)
def test_every_active_checkpoint_is_preserved_without_consuming_success_slots(lab, status):
    store, objects, now = lab
    target = bot(store)
    completed = [
        checkpoint(store, target, label, "completed", now - 1000)
        for label in ("z-newest", "y-previous", "x-oldest")
    ]
    active = checkpoint(store, target, "zz-active", status, now - 1000)
    cleaner = ObjectRetention(store, "owner", POLICY)
    assert cleaner.sweep(now=now) == {"deleted": 0, "pending": 0}
    assert pointers(store, target) == {item["id"] for item in [*completed[:2], active]}
    with pytest.raises(Conflict, match="finalizing"):
        WorkspaceObjects(store).current("owner", target)
    assert cleaner.sweep(now=now + 300) == {"deleted": 1, "pending": 0}
    assert store.task("owner", active["task_id"])["status"] == status
    assert_bytes(objects, [*completed[:2], active])


def test_recent_lower_rank_success_and_failure_wait_for_age_then_full_grace(lab):
    store, objects, now = lab
    target = bot(store)
    retained = [
        checkpoint(store, target, "newest", "completed", now - 10),
        checkpoint(store, target, "previous", "completed", now - 20),
    ]
    recent = [
        checkpoint(store, target, "third", "completed", now - 299),
        checkpoint(store, target, "failed", "failed", now - 299),
    ]
    cleaner = ObjectRetention(store, "owner", POLICY)
    assert cleaner.sweep(now=now) == {"deleted": 0, "pending": 0}
    assert pointers(store, target) == {item["id"] for item in retained + recent}
    assert cleaner.sweep(now=now + 1) == {"deleted": 0, "pending": 0}
    assert pointers(store, target) == {item["id"] for item in retained}
    assert_bytes(objects, retained + recent)
    assert cleaner.sweep(now=now + 300) == {"deleted": 0, "pending": 0}
    assert cleaner.sweep(now=now + 301) == {"deleted": 2, "pending": 0}
    assert_bytes(objects, retained)


def test_pruning_and_object_cleanup_do_not_change_another_owner(lab):
    store, objects, now = lab
    local, foreign = bot(store), bot(store, "foreign")
    for target, owner in ((local, "owner"), (foreign, "foreign")):
        items = [
            checkpoint(store, target, label, "completed", now - 1000, owner=owner)
            for label in ("z-newest", "y-previous", "x-oldest")
        ]
        if owner == "foreign":
            foreign_items = items
    with store.connect() as db:
        before = [
            tuple(row)
            for row in db.execute("SELECT * FROM stored_objects WHERE owner='foreign' ORDER BY key")
        ]
    cleaner = ObjectRetention(store, "owner", POLICY)
    assert cleaner.sweep(now=now) == {"deleted": 0, "pending": 0}
    assert cleaner.sweep(now=now + 300) == {"deleted": 1, "pending": 0}
    assert len(pointers(store, local)) == 2
    assert pointers(store, foreign) == {item["id"] for item in foreign_items}
    with store.connect() as db:
        assert before == [
            tuple(row)
            for row in db.execute("SELECT * FROM stored_objects WHERE owner='foreign' ORDER BY key")
        ]
    assert_bytes(objects, foreign_items)


def test_legacy_checkpoint_is_registered_before_pruning_and_receives_full_grace(lab):
    store, objects, now = lab
    target = bot(store)
    items = [
        checkpoint(store, target, label, "completed", now - 1000)
        for label in ("z-newest", "y-previous", "x-oldest")
    ]
    oldest = items[-1]
    with store.connect(write=True) as db:
        db.execute("DELETE FROM stored_objects WHERE key=?", (oldest["ref"].key,))
    cleaner = ObjectRetention(store, "owner", POLICY)
    assert cleaner.sweep(now=now) == {"deleted": 0, "pending": 0}
    assert oldest["id"] not in pointers(store, target)
    with store.connect() as db:
        row = db.execute(
            "SELECT state,unreferenced_at FROM stored_objects WHERE key=?", (oldest["ref"].key,)
        ).fetchone()
        assert tuple(row) == ("live", now)
    assert cleaner.sweep(now=now + 299) == {"deleted": 0, "pending": 0}
    assert_bytes(objects, items)
    assert cleaner.sweep(now=now + 300) == {"deleted": 1, "pending": 0}
    assert_bytes(objects, items[:2])
    assert not (objects.root / oldest["ref"].key).exists()


def test_library_reference_protects_object_after_its_checkpoint_pointer_is_pruned(lab):
    store, objects, now = lab
    target = bot(store)
    items = [
        checkpoint(store, target, label, "completed", now - 1000)
        for label in ("z-newest", "y-previous", "x-oldest")
    ]
    oldest = items[-1]
    # Deliberately share an immutable reference through the real publisher.
    # Normal file uploads use new keys; this verifies both pointer types count.
    from alysis_code.bots.store import LimitReached

    store.set_library_limit("owner", oldest["ref"].size - 1)
    with pytest.raises(LimitReached, match="shared Library"):
        store._save_file(
            "owner", target, FileInput(name="shared.txt", content=""), object_ref=oldest["ref"]
        )
    store.set_library_limit("owner", oldest["ref"].size)
    saved = store._save_file(
        "owner", target, FileInput(name="shared.txt", content=""), object_ref=oldest["ref"]
    )
    assert store.library_usage("owner")["used_bytes"] == oldest["ref"].size
    cleaner = ObjectRetention(store, "owner", POLICY)
    assert cleaner.sweep(now=now) == {"deleted": 0, "pending": 0}
    assert oldest["id"] not in pointers(store, target)
    assert cleaner.sweep(now=now + 1000) == {"deleted": 0, "pending": 0}
    assert store.file_data("owner", target, saved["id"]) == oldest["data"]
    assert_bytes(objects, items)
    store.delete_item("owner", target, saved["id"], kind="files")
    assert cleaner.sweep(now=now + 1001) == {"deleted": 0, "pending": 0}
    assert cleaner.sweep(now=now + 1301) == {"deleted": 1, "pending": 0}
    assert_bytes(objects, items[:2])
    assert not (objects.root / oldest["ref"].key).exists()


def test_second_prune_batch_failure_rolls_back_every_pointer_and_inventory_change(lab, monkeypatch):
    store, objects, now = lab
    target = bot(store)
    # 501 eligible pointers exceed one 500-ID batch; two newer successes stay.
    items = [
        checkpoint(store, target, f"{index:04d}", "completed", now - 1000) for index in range(503)
    ]
    with store.connect(write=True) as db:
        # Adoption occurs before pruning and must roll back with it as well.
        db.execute("DELETE FROM stored_objects WHERE key=?", (items[0]["ref"].key,))
        before_pointers = [
            tuple(row) for row in db.execute("SELECT * FROM workspace_objects ORDER BY id")
        ]
        before_inventory = [
            tuple(row) for row in db.execute("SELECT * FROM stored_objects ORDER BY key")
        ]
    original = store.connect
    batches, deleted = [], []
    delete = objects.delete

    def record_delete(ref):
        deleted.append(ref)
        return delete(ref)

    class FailingConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, args=()):
            if sql.startswith("DELETE FROM workspace_objects"):
                batches.append(tuple(args))
                if len(batches) == 2:
                    # Verify the first batch actually changed the open SQLite
                    # transaction before injecting the second-statement failure.
                    assert (
                        self.db.execute("SELECT count(*) FROM workspace_objects").fetchone()[0] == 3
                    )
                    raise RuntimeError("Synthetic second metadata batch failure")
            return self.db.execute(sql, args)

    @contextmanager
    def failing(*, write=False):
        with original(write=write) as db:
            yield FailingConnection(db)

    monkeypatch.setattr(objects, "delete", record_delete)
    cleaner = ObjectRetention(store, "owner", POLICY)
    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", failing)
        with pytest.raises(RuntimeError, match="Synthetic second metadata batch failure"):
            cleaner.sweep(now=now)
    assert [len(batch) for batch in batches] == [500, 1]
    assert not deleted
    with store.connect() as db:
        assert before_pointers == [
            tuple(row) for row in db.execute("SELECT * FROM workspace_objects ORDER BY id")
        ]
        assert before_inventory == [
            tuple(row) for row in db.execute("SELECT * FROM stored_objects ORDER BY key")
        ]
    assert_bytes(objects, items)

    assert cleaner.sweep(now=now) == {"deleted": 0, "pending": 0}
    assert pointers(store, target) == {item["id"] for item in items[-2:]}
    with store.connect() as db:
        inventory = {
            row["key"]: (row["state"], row["unreferenced_at"])
            for row in db.execute("SELECT * FROM stored_objects")
        }
    assert len(inventory) == len(items)
    for item in items[:-2]:
        assert inventory[item["ref"].key] == ("live", now)
    for item in items[-2:]:
        assert inventory[item["ref"].key] == ("live", None)
    assert cleaner.sweep(now=now + 299, limit=1000) == {"deleted": 0, "pending": 0}
    assert not deleted
    assert_bytes(objects, items)
    assert cleaner.sweep(now=now + 300, limit=1000) == {"deleted": 501, "pending": 0}
    assert {ref.key for ref in deleted} == {item["ref"].key for item in items[:-2]}
    assert_bytes(objects, items[-2:])
