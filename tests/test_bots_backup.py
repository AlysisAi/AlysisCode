"""Real pg_dump/pg_restore qualification against isolated PostgreSQL databases.

File/task contents are synthetic. No model/cloud calls. Only uniquely created
test databases are dropped; the shared PostgreSQL server is never restarted.
"""

import base64
import hashlib
import json
import os
import subprocess
from uuid import uuid4

import pytest

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.backup import (
    BackupError,
    create_backup,
    dump_database,
    postgres_environment,
    restore_backup,
    restore_database,
    verify_backup,
)
from alysis_code.bots.models import (
    BotInput,
    MemoryInput,
    NoteConsolidation,
    NoteInput,
    NoteSource,
    TaskInput,
)
from alysis_code.bots.object_storage import LocalObjectStorage, ObjectRef, ObjectStorageError
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import LibraryCapacityExceeded
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput
from alysis_code.bots.worker_lease import WorkerLease


def postgres_tool(command, dsn, **streams):
    """Use the actual PostgreSQL 18 client in the explicitly selected local DB."""
    from psycopg.conninfo import make_conninfo

    container = os.environ["ALYSIS_BOTS_TEST_POSTGRES_CONTAINER"]
    inside = make_conninfo(dsn, host="127.0.0.1", port=5432)
    environment = postgres_environment(inside)
    flags = [value for key in environment for value in ("--env", key)]
    result = subprocess.run(
        ["docker", "exec", "--interactive", *flags, container, *command],
        env={**os.environ, **environment},
        stderr=subprocess.DEVNULL,
        timeout=60,
        **streams,
    )
    if result.returncode:
        raise BackupError("Test PostgreSQL client failed.")


def dump(dsn, snapshot, path):
    dump_database(dsn, snapshot, path, run=postgres_tool)


def restore(dsn, path):
    restore_database(dsn, path, run=postgres_tool)


def enable_reviewed_synthetic_database(dsn):
    """Explicit operator step for test-only data after inventory/work review."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as db:
        assert db.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        # This test's original deployment is stopped. No post-backup actions or
        # provider resources exist; its preserved uncertain intent stays fenced.
        db.execute("SET default_transaction_read_only=off")
        name = db.execute("SELECT current_database()").fetchone()[0]
        db.execute(
            sql.SQL("ALTER DATABASE {} SET default_transaction_read_only=off").format(
                sql.Identifier(name)
            )
        )


@pytest.fixture
def databases(tmp_path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    base = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
    if not base or not os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_CONTAINER"):
        pytest.skip("Explicit disposable PostgreSQL admin DSN and local container required")
    names, stores = [], []

    def make(*, empty=False, objects=None):
        name = "bots_backup_" + uuid4().hex
        with psycopg.connect(base, autocommit=True) as db:
            db.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(name)))
        names.append(name)
        dsn = make_conninfo(base, dbname=name)
        if empty:
            return dsn
        store = PostgresStore(
            dsn,
            objects=objects or LocalObjectStorage(tmp_path / name / "objects"),
            state_directory=tmp_path / name / "scratch",
        )
        stores.append(store)
        return store

    try:
        yield make
    finally:
        for store in stores:
            store.close()
        with psycopg.connect(base, autocommit=True) as db:
            for name in names:
                db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def seed(store):
    bot = store.save_bot("owner", BotInput(name="Αθήνα 東京", purpose="Preserve real state"))
    bot_id = bot["id"]
    chat = store.create_chat("owner", bot_id, "Continuous")
    store.save_memory("owner", bot_id, MemoryInput(title="Language", content="Ελληνικά 日本語"))
    store.save_schedule(
        "owner",
        bot_id,
        ScheduleInput(
            name="Reminder",
            prompt="Review my files",
            timing={"kind": "interval", "every_minutes": 60},
        ),
    )
    payload = os.urandom(800003)
    file = store.save_stream(
        "owner",
        bot_id,
        "資料 Δ.bin",
        (payload[i : i + 262144] for i in range(0, len(payload), 262144)),
        len(payload),
    )
    store.save_stream("owner", bot_id, "empty.bin", iter(()), 0)
    # An uncertain task and allocation must survive backup. Restoring metadata
    # must not invent confirmed deletion or silently release its budget.
    lease = WorkerLease(store, "owner")
    worker = lease.acquire()
    task = worker.enqueue(
        "owner", chat["id"], TaskInput(message="Uncertain work", request_id=uuid4().hex)
    )
    worker.claim("owner")
    record = {
        "state": "creating",
        "scope": "synthetic-retained-scope",
        "allocation_id": uuid4().hex,
    }
    AllocationLedger(worker, "owner").reserve(bot_id, record, max_active=1)
    lease.close()
    return bot_id, file["id"], hashlib.sha256(payload).hexdigest(), task["id"], record


def test_real_database_and_objects_restore_preserves_uncertain_work(databases, tmp_path):
    import psycopg

    source = databases()
    bot, file_id, digest, task_id, record = seed(source)
    path = tmp_path / "backup"
    manifest = create_backup(source._dsn, source.objects, path, dump=dump)
    assert len(manifest["objects"]) == 2
    assert verify_backup(path) == manifest
    assert source._dsn not in (path / "manifest.json").read_text()
    destination = databases(empty=True)
    objects = LocalObjectStorage(tmp_path / "restored-objects")
    assert restore_backup(destination, objects, path, restore=restore)["verified"]
    with pytest.raises(RuntimeError, match="no success"):
        PostgresStore(destination, objects=objects, state_directory=tmp_path / "blocked-controller")
    enable_reviewed_synthetic_database(destination)
    restored = PostgresStore(destination, objects=objects, state_directory=tmp_path / "fresh")
    lease = None
    try:
        assert restored.bot("owner", bot)["name"] == "Αθήνα 東京"
        assert restored.list_memories("owner", bot)[0]["content"] == "Ελληνικά 日本語"
        assert len(restored.list_schedules("owner", bot)) == 1
        assert hashlib.sha256(restored.file_data("owner", bot, file_id)).hexdigest() == digest
        assert restored.task("owner", task_id)["status"] == "running"
        lease = WorkerLease(restored, "owner")
        worker = lease.acquire()
        assert AllocationLedger(worker, "owner").get(bot) == record
        with restored.connect() as db:
            assert db.execute("SELECT max_active FROM computer_capacity").fetchone()[0] == 1
        worker.recover("owner")
        assert worker.task("owner", task_id)["status"] == "interrupted"
        assert worker.claim("owner") is None
        assert AllocationLedger(worker, "owner").get(bot) == record
        # Source is still independent and unchanged by target recovery.
        assert source.task("owner", task_id)["status"] == "running"
        with psycopg.connect(destination) as db:
            assert (
                db.execute("SELECT nextval(pg_get_serial_sequence('events','seq'))").fetchone()[0]
                > 1
            )
    finally:
        if lease:
            lease.close()
        restored.close()


def test_backup_restores_memory_note_provenance_and_partial_library_upload(databases, tmp_path):
    source = databases()
    owner = "restored-reference-owner"
    bot = source.save_bot(owner, BotInput(name="Reference", purpose="Synthetic restore"))
    peer = source.save_bot(owner, BotInput(name="Files", purpose="Shared allowance"))
    chat = source.create_chat(owner, bot["id"], "Current")
    source.enqueue(owner, chat["id"], TaskInput(message="Prepare", request_id=uuid4().hex))
    task = source.claim(owner)
    memory = source.save_memory(
        owner, bot["id"], MemoryInput(title="応答", content="短い段落で答える")
    )
    assert (
        source.cache_memory_applicability(
            owner, bot["id"], task["id"], [memory], {memory["id"]: "general"}
        )
        == 1
    )
    source.save_memory(
        owner,
        bot["id"],
        MemoryInput(title="Pending", content="Not approved"),
        source_task_id=task["id"],
    )
    source.finish(owner, task["id"], "completed", publish_reply=False)
    original = source.save_note(owner, bot["id"], NoteInput(title="Project", content="Old plan"))
    updated = source.save_note(
        owner,
        bot["id"],
        NoteInput(title="Project", content="Corrected plan", expected_revision=1),
        note_id=original["id"],
    )
    related = source.save_note(owner, bot["id"], NoteInput(title="Decision", content="No date set"))
    summary = source.consolidate_notes(
        owner,
        bot["id"],
        NoteConsolidation(
            title="Current project",
            content="Corrected plan; no date set",
            reason="Synthetic overlap",
            sources=[
                NoteSource(note_id=n["id"], expected_revision=n["revision"])
                for n in (updated, related)
            ],
        ),
    )
    source.set_library_limit(owner, 20)
    saved = source.save_bytes(owner, bot["id"], "retained.bin", b"retained")
    transfers = FileTransfers(source, owner)
    upload = transfers.begin(
        peer["id"], UploadInput(name="pending.bin", size=7, request_id=uuid4().hex)
    )
    transfers.append(
        peer["id"],
        upload["id"],
        UploadChunk(offset=0, data_base64=base64.b64encode(b"abc").decode()),
    )
    usage = source.library_usage(owner)
    assert usage["used_bytes"] == 8 and usage["reserved_bytes"] == 7
    backup = tmp_path / "reference-backup"
    create_backup(source._dsn, source.objects, backup, dump=dump)
    destination = databases(empty=True)
    objects = LocalObjectStorage(tmp_path / "reference-restored-objects")
    assert restore_backup(destination, objects, backup, restore=restore)["verified"]
    enable_reviewed_synthetic_database(destination)
    restored = PostgresStore(
        destination, objects=objects, state_directory=tmp_path / "reference-restored"
    )
    try:
        assert restored.list_memories(owner, bot["id"]) == source.list_memories(owner, bot["id"])
        memories = restored.list_memories(owner, bot["id"])
        assert {record["status"] for record in memories} == {"active", "pending"}
        assert (
            next(record for record in memories if record["id"] == memory["id"])["context_scope"]
            == "general"
        )
        assert restored.note(owner, bot["id"], original["id"]) == source.note(
            owner, bot["id"], original["id"]
        )
        assert restored.note(owner, bot["id"], summary["id"]) == source.note(
            owner, bot["id"], summary["id"]
        )
        assert restored.library_usage(owner) == usage
        assert restored.file_data(owner, bot["id"], saved["id"]) == b"retained"
        with pytest.raises(LibraryCapacityExceeded):
            restored.save_bytes(owner, peer["id"], "too-large.bin", b"123456")
        assert restored.library_usage(owner) == usage
        resumed = FileTransfers(restored, owner)
        assert resumed.read(peer["id"], upload["id"])["received"] == 3
        resumed.append(
            peer["id"],
            upload["id"],
            UploadChunk(offset=3, data_base64=base64.b64encode(b"defg").decode()),
        )
        complete = resumed.complete(peer["id"], upload["id"])
        assert restored.file_data(owner, peer["id"], complete["file_id"]) == b"abcdefg"
        assert restored.library_usage(owner)["used_bytes"] == 15
        assert restored.library_usage(owner)["reserved_bytes"] == 0
        restored.restore_note(owner, bot["id"], original["id"], updated["revision"])
        assert source.note(owner, bot["id"], original["id"])["consolidated_into"] == summary["id"]
        assert transfers.read(peer["id"], upload["id"])["received"] == 3
    finally:
        restored.close()


def test_running_worker_blocks_backup_before_archive(databases, tmp_path):
    source = databases()
    lease = WorkerLease(source, "still-running")
    lease.acquire()
    try:
        with pytest.raises(BackupError, match="Stop all account workers"):
            create_backup(source._dsn, source.objects, tmp_path / "blocked", dump=dump)
        assert not (tmp_path / "blocked" / "database.dump").exists()
        assert not (tmp_path / "blocked" / "manifest.json").exists()
    finally:
        lease.close()


def test_backup_lock_blocks_worker_start_and_concurrent_write(databases, tmp_path):
    source = databases()
    seed(source)

    def checked_dump(dsn, snapshot, path):
        lease = WorkerLease(source, "racing-worker")
        with pytest.raises(RuntimeError, match="no success"):
            lease.acquire()
        with pytest.raises(RuntimeError, match="no success"):
            source.save_bot("owner", BotInput(name="Must not appear", purpose="Race"))
        dump(dsn, snapshot, path)

    create_backup(source._dsn, source.objects, tmp_path / "locked", dump=checked_dump)
    assert len(source.list_bots("owner")) == 1


@pytest.mark.parametrize("corruption", ["database", "object", "missing", "manifest"])
def test_bad_backup_never_writes_target(databases, tmp_path, corruption):
    import psycopg

    source = databases()
    seed(source)
    path = tmp_path / "backup"
    manifest = create_backup(source._dsn, source.objects, path, dump=dump)
    if corruption == "database":
        with (path / "database.dump").open("ab") as stream:
            stream.write(b"corruption")
    elif corruption in ("object", "missing"):
        ref = next(item for item in manifest["objects"] if item["size"] > 0)
        blob = path / "objects" / ref["key"]
        if corruption == "missing":
            blob.unlink()
        else:
            with blob.open("r+b") as stream:
                stream.write(b"corruption")
    else:
        (path / "manifest.json").unlink()
    target = databases(empty=True)
    objects = LocalObjectStorage(tmp_path / "target-objects")
    with pytest.raises((BackupError, ObjectStorageError)):
        restore_backup(target, objects, path, restore=restore)
    assert list(objects.root.iterdir()) == []
    with psycopg.connect(target) as db:
        assert (
            db.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
            == 0
        )


def test_existing_database_and_objects_are_not_overwritten(databases, tmp_path):
    source = databases()
    seed(source)
    path = tmp_path / "backup"
    manifest = create_backup(source._dsn, source.objects, path, dump=dump)
    with pytest.raises(FileExistsError):
        create_backup(source._dsn, source.objects, path, dump=dump)
    with pytest.raises(BackupError, match="fresh, empty"):
        restore_backup(source._dsn, source.objects, path, restore=restore)
    target = databases(empty=True)
    objects = LocalObjectStorage(tmp_path / "target-objects")
    ref = ObjectRef.decode(json.dumps(manifest["objects"][0]))
    objects.copy_from(LocalObjectStorage(path / "objects"), ref)
    with pytest.raises(ObjectStorageError):
        restore_backup(target, objects, path, restore=restore)


def test_failed_archive_or_object_copy_never_publishes_manifest(databases, tmp_path):
    source = databases()
    seed(source)

    def failed_dump(dsn, snapshot, path):
        path.write_bytes(b"PGDMPpartial")
        raise BackupError("Injected interrupted archive transport.")

    with pytest.raises(BackupError):
        create_backup(source._dsn, source.objects, tmp_path / "bad-dump", dump=failed_dump)
    assert not (tmp_path / "bad-dump" / "manifest.json").exists()
    with source.connect() as db:
        ref = ObjectRef.decode(db.execute("SELECT ref FROM file_objects LIMIT 1").fetchone()[0])
    source.objects.delete(ref)
    with pytest.raises(ObjectStorageError):
        create_backup(source._dsn, source.objects, tmp_path / "bad-object", dump=dump)
    assert not (tmp_path / "bad-object" / "manifest.json").exists()


@pytest.mark.parametrize(
    "definition", ["CREATE TYPE public.keep AS ENUM ('private')", "CREATE SCHEMA pgX"]
)
def test_restore_preserves_non_table_database_objects(databases, tmp_path, definition):
    import psycopg

    source = databases()
    path = tmp_path / "backup"
    create_backup(source._dsn, source.objects, path, dump=dump)
    target = databases(empty=True)
    with psycopg.connect(target) as db:
        db.execute(definition)
    with pytest.raises(BackupError, match="fresh, empty"):
        restore_backup(target, LocalObjectStorage(tmp_path / "objects"), path, restore=restore)


def test_failed_archive_restore_rolls_back_target_database(databases, tmp_path):
    import psycopg

    source = databases()
    seed(source)
    path = tmp_path / "backup"
    manifest = create_backup(source._dsn, source.objects, path, dump=dump)
    archive = path / "database.dump"
    # Inject a malformed archive with an internally matching manifest, so this
    # reaches the real pg_restore parser/transaction rather than the hash gate.
    with archive.open("r+b") as stream:
        stream.truncate(archive.stat().st_size // 2)
    data = archive.read_bytes()
    manifest["database"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (path / "manifest.json").write_text(json.dumps(manifest))
    target = databases(empty=True)
    with pytest.raises(BackupError):
        restore_backup(target, LocalObjectStorage(tmp_path / "new-objects"), path, restore=restore)
    with psycopg.connect(target) as db:
        assert (
            db.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
            == 0
        )
