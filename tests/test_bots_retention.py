import time
from contextlib import contextmanager

import pytest
from test_bots_object_storage import setup, task

from alysis_code.bots.models import FileInput
from alysis_code.bots.object_storage import ObjectRef, ObjectStorageError
from alysis_code.bots.retention import ObjectRetention, RetentionPolicy
from alysis_code.bots.store import Conflict, Store
from alysis_code.bots.workspace_objects import WorkspaceObjects


def cleaner(store):
    return ObjectRetention(store, "owner", RetentionPolicy(300, 300, 2, 300))


def test_deleted_file_grace_active_file_and_unknown_objects(tmp_path):
    objects, store, bot = setup(tmp_path)
    active = store.save_bytes("owner", bot, "active", b"active")
    removed = store.save_bytes("owner", bot, "removed", b"removed")
    unknown = objects.put("owner", bot, [b"unknown"], 7)
    store.delete_item("owner", bot, removed["id"], kind="files")
    now = time.time()
    assert cleaner(store).sweep(now=now)["deleted"] == 0
    assert cleaner(store).sweep(now=now + 301)["deleted"] == 1
    assert store.file_data("owner", bot, active["id"]) == b"active"
    assert b"".join(objects.read(unknown)) == b"unknown"


def test_lost_upload_response_and_late_publication_cannot_resurrect(tmp_path, monkeypatch):
    objects, store, bot = setup(tmp_path)
    real = objects._upload
    uploaded = []

    def lost(ref, stream):
        real(ref, stream)
        uploaded.append(ref)
        raise ObjectStorageError("Lost response")

    monkeypatch.setattr(objects, "_upload", lost)
    with pytest.raises(ObjectStorageError):
        store.save_bytes("owner", bot, "lost", b"lost")
    assert not store.list_files("owner", bot)
    assert cleaner(store).sweep(now=time.time() + 301)["deleted"] == 1
    with store.connect(write=True) as db, pytest.raises(Conflict):
        store.publish_object(db, "owner", bot, uploaded[0])
    # A late network write to a deleted key is removed by tombstone reconciliation.
    import io

    real(uploaded[0], io.BytesIO(b"lost"))
    assert cleaner(store).sweep(now=time.time() + 602)["deleted"] == 1
    assert not (objects.root / uploaded[0].key).exists()


def test_delete_failure_survives_restart(tmp_path, monkeypatch):
    objects, store, bot = setup(tmp_path)
    ref = store.put_object("owner", bot, [b"orphan"], 6)
    real = objects.delete
    monkeypatch.setattr(objects, "delete", lambda ref: (_ for _ in ()).throw(ObjectStorageError()))
    assert cleaner(store).sweep(now=time.time() + 301) == {"deleted": 0, "pending": 1}
    monkeypatch.setattr(objects, "delete", real)
    store = Store(store.path, objects=objects)
    assert cleaner(store).sweep()["deleted"] == 1
    assert not (objects.root / ref.key).exists()


def test_retains_two_successes_and_active_candidate(tmp_path):
    objects, store, bot = setup(tmp_path)
    ws = WorkspaceObjects(store)
    refs = []
    for i in range(4):
        tid = task(store, bot)
        refs.append(ws.stage("owner", bot, tid, [bytes([i])], 1))
        if i < 3:
            store.finish("owner", tid, "completed", content="done")
    now = time.time() + 301
    assert cleaner(store).sweep(now=now)["deleted"] == 0
    assert cleaner(store).sweep(now=now + 301)["deleted"] == 1
    assert not (objects.root / refs[0].key).exists()
    for ref in refs[1:]:
        assert (objects.root / ref.key).exists()


def test_collection_prevents_delayed_file_commit_and_other_owner_cleanup(tmp_path):
    objects, store, bot = setup(tmp_path)
    data = FileInput(name="delayed", content="x")
    ref = store.stage_file_object("owner", bot, data, None, None)
    assert ObjectRetention(store, "other").sweep(now=time.time() + 999999)["deleted"] == 0
    cleaner(store).sweep(now=time.time() + 301)
    with pytest.raises(Conflict):
        store._save_file("owner", bot, data, object_ref=ref)
    assert not store.list_files("owner", bot)


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_or_interrupted_deletion_does_not_starve_next_batch(
    tmp_path, monkeypatch, interrupted
):
    objects, store, bot = setup(tmp_path)
    clock = [time.time()]
    monkeypatch.setattr("alysis_code.bots.retention.time.time", lambda: clock[0])
    blocked = store.put_object("owner", bot, [b"blocked"], 7)
    clock[0] += 1
    removable = store.put_object("owner", bot, [b"removable"], 9)
    clock[0] += 301
    real = objects.delete
    attempts = []

    def delete(ref):
        attempts.append(ref)
        if ref == blocked:
            if interrupted:
                # Fixture interruption after the database claim, before any
                # acknowledgement. Actual process-death QA is separate.
                raise RuntimeError("Synthetic sweep interruption")
            raise ObjectStorageError("Synthetic object remains unavailable")
        real(ref)

    monkeypatch.setattr(objects, "delete", delete)
    if interrupted:
        with pytest.raises(RuntimeError, match="Synthetic sweep interruption"):
            cleaner(store).sweep(limit=1)
    else:
        assert cleaner(store).sweep(limit=1) == {"deleted": 0, "pending": 1}
    assert attempts == [blocked]

    # Reopen the database immediately: no timeout/age jump may be required for
    # either progress on the other orphan or retrying the unfinished deletion.
    restarted = Store(store.path, objects=objects)
    assert cleaner(restarted).sweep(limit=1) == {"deleted": 1, "pending": 0}
    assert attempts == [blocked, removable]
    assert (objects.root / blocked.key).exists()
    assert not (objects.root / removable.key).exists()
    monkeypatch.setattr(objects, "delete", real)
    assert cleaner(restarted).sweep(limit=1) == {"deleted": 1, "pending": 0}
    assert not (objects.root / blocked.key).exists()


def test_legacy_adoption_then_stable_sweep_makes_no_database_writes(tmp_path, monkeypatch):
    objects, store, bot = setup(tmp_path)
    saved = store.save_bytes("owner", bot, "資料.txt", "Αθήνα 東京".encode())
    tid = task(store, bot)
    checkpoint = WorkspaceObjects(store).stage("owner", bot, tid, [b"checkpoint"], 10)
    # Model pre-inventory persisted references without changing actual bytes.
    with store.connect(write=True) as db:
        db.execute("DELETE FROM stored_objects")
    orphan = store.put_object("owner", bot, [b"staged"], 6)
    now = time.time()
    assert cleaner(store).sweep(now=now) == {"deleted": 0, "pending": 0}
    with store.connect() as db:
        original = [tuple(row) for row in db.execute("SELECT * FROM stored_objects ORDER BY key")]
    changed = []
    connect = store.connect

    @contextmanager
    def measured(*, write=False):
        with connect(write=write) as db:
            before = db.total_changes
            yield db
            changed.append(db.total_changes - before)

    monkeypatch.setattr(store, "connect", measured)
    assert cleaner(store).sweep(now=now + 1) == {"deleted": 0, "pending": 0}
    assert sum(changed) == 0, "Unchanged inventory must not rewrite every live/staged row"
    with store.connect() as db:
        assert original == [
            tuple(row) for row in db.execute("SELECT * FROM stored_objects ORDER BY key")
        ]
    assert len(original) == 3
    assert store.file_data("owner", bot, saved["id"]) == "Αθήνα 東京".encode()
    assert b"".join(objects.read(checkpoint)) == b"checkpoint"
    assert b"".join(objects.read(orphan)) == b"staged"


def test_reference_reappearance_clears_grace_but_never_revives_tombstone(tmp_path):
    objects, store, bot = setup(tmp_path)
    file = store.save_bytes("owner", bot, "live", b"live")
    now = time.time()
    with store.connect(write=True) as db:
        row = db.execute("SELECT * FROM file_objects WHERE file_id=?", (file["id"],)).fetchone()
        # Synthetic metadata disappearance and reappearance; no byte deletion.
        db.execute("DELETE FROM file_objects WHERE file_id=?", (file["id"],))
    assert cleaner(store).sweep(now=now)["deleted"] == 0
    with store.connect() as db:
        assert db.execute("SELECT unreferenced_at FROM stored_objects").fetchone()[0] == now
    assert cleaner(store).sweep(now=now + 1)["deleted"] == 0
    with store.connect(write=True) as db:
        assert db.execute("SELECT unreferenced_at FROM stored_objects").fetchone()[0] == now
        db.execute("INSERT INTO file_objects VALUES(?,?,?,?,?)", tuple(row))
    assert cleaner(store).sweep(now=now + 2)["deleted"] == 0
    with store.connect(write=True) as db:
        assert db.execute("SELECT unreferenced_at FROM stored_objects").fetchone()[0] is None
        # A forbidden late publication cannot reset a durable deleted intent.
        # Inject the inconsistent reference to verify adoption is conservative.
        db.execute("UPDATE stored_objects SET state='deleted',checked_at=?", (now,))
    assert cleaner(store).sweep(now=now + 3)["deleted"] == 0
    with store.connect() as db:
        assert db.execute("SELECT state FROM stored_objects").fetchone()[0] == "deleted"
    assert b"".join(objects.read(ObjectRef.decode(row["ref"]))) == b"live"
