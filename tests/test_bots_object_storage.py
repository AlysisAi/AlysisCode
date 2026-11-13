import base64
import hashlib
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_bots_cloud_desktop import CheckpointProvider

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.files import FILE_CHUNK_BYTES
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage, ObjectRef, ObjectStorageError
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput
from alysis_code.bots.workspace_objects import WorkspaceObjects


def chunks(data):
    return (data[i : i + FILE_CHUNK_BYTES] for i in range(0, len(data), FILE_CHUNK_BYTES))


def setup(tmp_path):
    objects = LocalObjectStorage(tmp_path / "objects")
    store = Store(tmp_path / "db", objects=objects)
    bot = store.save_bot("owner", BotInput(name="Storage", purpose="Verify durable files"))
    return objects, store, bot["id"]


def task(store, bot):
    chat = store.create_chat("owner", bot, "Storage")
    queued = store.enqueue("owner", chat["id"], TaskInput(message="Verify", request_id=uuid4().hex))
    assert store.claim("owner")["id"] == queued["id"]
    return queued["id"]


@pytest.mark.parametrize("size", [0, 17, FILE_CHUNK_BYTES * 3 + 29])
def test_exact_stream_and_offset_across_restart(tmp_path, size):
    objects, store, bot = setup(tmp_path)
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    saved = store.save_stream("owner", bot, "東京.bin", chunks(data), size)
    assert saved["sha256"] == hashlib.sha256(data).hexdigest()
    store = Store(store.path, objects=LocalObjectStorage(objects.root))
    assert b"".join(store.file_chunks("owner", bot, saved["id"])) == data
    assert (
        b"".join(store.file_chunks("owner", bot, saved["id"], offset=size // 2))
        == data[size // 2 :]
    )
    assert not {"ref", "key", "chunks", "content"}.intersection(store.list_files("owner", bot)[0])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM file_payloads").fetchone()[0] == 0
    with pytest.raises(ObjectStorageError, match="requires"):
        Store(store.path)


def test_corrupt_or_missing_data_is_never_returned(tmp_path):
    objects, store, bot = setup(tmp_path)
    saved = store.save_bytes("owner", bot, "data.bin", b"good")
    with store.connect() as db:
        ref = ObjectRef.decode(db.execute("SELECT ref FROM file_objects").fetchone()[0])
    (objects.root / ref.key).write_bytes(b"evil")
    with pytest.raises(ObjectStorageError, match="integrity"):
        store.file_data("owner", bot, saved["id"])
    (objects.root / ref.key).unlink()
    with pytest.raises(ObjectStorageError, match="unavailable"):
        store.file_data("owner", bot, saved["id"])
    with pytest.raises(ValueError):
        replace(ref, key="../../escape")


def test_owner_and_bot_denied_before_storage_read(tmp_path, monkeypatch):
    objects, store, bot = setup(tmp_path)
    saved = store.save_bytes("owner", bot, "data.bin", b"good")
    peer = store.save_bot("owner", BotInput(name="Peer", purpose="Separate data"))["id"]
    monkeypatch.setattr(objects, "read", lambda *a, **k: pytest.fail("unauthorized object read"))
    for owner, target in [("other", bot), ("owner", peer)]:
        with pytest.raises(NotFound):
            store.file_data(owner, target, saved["id"])


def test_failed_upload_does_not_publish_and_quota_counts_external_bytes(tmp_path, monkeypatch):
    objects, store, bot = setup(tmp_path)
    with pytest.raises(ValueError, match="Incomplete"):
        store.save_stream("owner", bot, "short", [b"x"], 2)
    assert store.list_files("owner", bot) == []
    store.set_library_limit("owner", 5)
    store.save_bytes("owner", bot, "one", b"1234")
    with pytest.raises(LimitReached):
        store.save_bytes("owner", bot, "two", b"56")


def test_stop_during_network_write_can_commit_and_prevents_publication(tmp_path, monkeypatch):
    objects, store, bot = setup(tmp_path)
    task_id = task(store, bot)
    original = objects._upload

    def upload(ref, stream):
        # This writes through a second connection. Upload must not hold the
        # writer lock or block Stop until the network request completes.
        store.cancel("owner", task_id)
        original(ref, stream)

    monkeypatch.setattr(objects, "_upload", upload)
    with pytest.raises(Conflict):
        store.save_bytes("owner", bot, "cancelled.bin", b"pending", source_task_id=task_id)
    assert store.list_files("owner", bot) == []


def test_chunked_upload_idempotency_and_cancel_during_object_stage(tmp_path, monkeypatch):
    objects, store, bot = setup(tmp_path)
    transfers = FileTransfers(store, "owner")
    upload = transfers.begin(bot, UploadInput(name="good.bin", size=3, request_id=uuid4().hex))
    transfers.append(
        bot, upload["id"], UploadChunk(offset=0, data_base64=base64.b64encode(b"abc").decode())
    )
    result = transfers.complete(bot, upload["id"])
    assert transfers.complete(bot, upload["id"]) == result
    assert store.file_data("owner", bot, result["file_id"]) == b"abc"
    pending = transfers.begin(bot, UploadInput(name="bad.bin", size=0, request_id=uuid4().hex))
    original = objects._upload

    def upload(ref, stream):
        transfers.cancel(bot, pending["id"])
        original(ref, stream)

    monkeypatch.setattr(objects, "_upload", upload)
    with pytest.raises(Conflict):
        transfers.complete(bot, pending["id"])
    assert len(store.list_files("owner", bot)) == 1


def test_legacy_migration_preserves_ids_text_and_restarts(tmp_path):
    store = Store(tmp_path / "legacy")
    bot = store.save_bot("owner", BotInput(name="Legacy", purpose="Keep files"))["id"]
    old = [
        store.save_file("owner", bot, FileInput(name="日本語.txt", content="Αθήνα 東京")),
        store.save_bytes("owner", bot, "binary", bytes(range(256))),
    ]
    objects = LocalObjectStorage(tmp_path / "remote")
    store = Store(store.path, objects=objects)
    for saved in old:
        assert store.externalize_file("owner", bot, saved["id"]) == saved
        assert store.externalize_file("owner", bot, saved["id"]) == saved
    store = Store(store.path, objects=objects)
    assert store.file_data("owner", bot, old[0]["id"]).decode() == "Αθήνα 東京"
    assert store.file_data("owner", bot, old[1]["id"]) == bytes(range(256))


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "interrupted"])
def test_checkpoint_publication_follows_terminal_task_transaction(tmp_path, status):
    objects, store, bot = setup(tmp_path)
    checkpoints = WorkspaceObjects(store)
    baseline = checkpoints.import_baseline("owner", bot, [b"original"], 8)
    task_id = task(store, bot)
    candidate = checkpoints.stage("owner", bot, task_id, [b"next"], 4)
    with pytest.raises(Conflict, match="finalizing"):
        checkpoints.current("owner", bot)
    store.finish("owner", task_id, status)
    assert checkpoints.current("owner", bot) == (candidate if status == "completed" else baseline)


def test_controller_replacement_restores_without_local_ledger_or_archive(tmp_path):
    objects, store, bot = setup(tmp_path)
    provider = CheckpointProvider()
    factory = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("image"),
        tmp_path / "host-a",
        persist_workspace=True,
        store=store,
    )
    task_id = task(store, bot)
    computer = factory(bot, tmp_path, cancel=lambda: False)
    computer.bind_task(task_id)
    computer.finish()
    store.finish("owner", task_id, "completed")
    # Database backup models a moved controller, not a replicated production DB.
    moved = tmp_path / "other-host" / "db"
    moved.parent.mkdir()
    with sqlite3.connect(store.path) as source, sqlite3.connect(moved) as target:
        source.backup(target)
    replacement = Store(moved, objects=objects)
    factory = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("image"),
        tmp_path / "host-b",
        persist_workspace=True,
        store=replacement,
    )
    restored = factory(bot, tmp_path, cancel=lambda: False)
    assert restored.workspace_reopened
    assert provider.restored == [provider.checkpoint]
    assert not restored._snapshot.exists()
    restored.close()
    assert all(state == "missing" for _, state in provider.allocations.values())


def test_file_http_storage_failure_is_honest_and_sanitized(tmp_path, monkeypatch):
    objects = LocalObjectStorage(tmp_path / "objects")
    settings = Settings(
        tmp_path / "db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    app = create_app(settings, object_storage=objects)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Files", "purpose": "Storage"}).json()["id"]
        path = f"/v1/bots/{bot}/files"
        file = client.post(path + "/binary", json={"name": "one", "data_base64": "eA=="}).json()

        def unavailable(*args, **kwargs):
            raise ObjectStorageError("upstream private detail")

        monkeypatch.setattr(objects, "_range", unavailable)
        response = client.get(path + f"/{file['id']}/download")
        assert response.status_code == 503
        assert "private" not in response.text


def test_migration_readback_failure_keeps_original_payload(tmp_path, monkeypatch):
    store = Store(tmp_path / "legacy")
    bot = store.save_bot("owner", BotInput(name="Legacy", purpose="Keep files"))["id"]
    old = store.save_bytes("owner", bot, "original", b"keep me")
    objects = LocalObjectStorage(tmp_path / "objects")
    store = Store(store.path, objects=objects)
    monkeypatch.setattr(objects, "_range", lambda *args: b"bad")
    with pytest.raises(ObjectStorageError):
        store.externalize_file("owner", bot, old["id"])
    assert store.file_data("owner", bot, old["id"]) == b"keep me"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM file_objects").fetchone()[0] == 0


def test_range_fetch_is_bounded_and_does_not_reread_prefix(tmp_path, monkeypatch):
    objects = LocalObjectStorage(tmp_path / "objects")
    data = b"a" * (FILE_CHUNK_BYTES * 3)
    ref = objects.put("owner", "bot", chunks(data), len(data))
    original = objects._range
    reads = []

    def read(ref, offset, length):
        reads.append((offset, length))
        return original(ref, offset, length)

    monkeypatch.setattr(objects, "_range", read)
    assert next(objects.read(ref, offset=FILE_CHUNK_BYTES + 1)) == b"a" * (FILE_CHUNK_BYTES - 1)
    assert reads == [(FILE_CHUNK_BYTES, FILE_CHUNK_BYTES)]


def test_provider_change_requires_old_computer_to_be_deleted(tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.desktop_linux import DesktopError

    provider = Provider()
    factory = CloudDesktopFactory("owner", provider, ComputerSpec("image"), tmp_path / "ledger")
    first = factory("bot", tmp_path, cancel=lambda: False)
    first.close()
    replacement_provider = Provider()
    replacement_provider.scope = "other-cloud-scope"
    factory = CloudDesktopFactory(
        "owner", replacement_provider, ComputerSpec("other-image"), tmp_path / "ledger"
    )
    second = factory("bot", tmp_path, cancel=lambda: False)
    second._lease.cleanup(clean=False)  # simulated controller process loss
    conflicting = CloudDesktopFactory("owner", provider, ComputerSpec("image"), tmp_path / "ledger")
    with pytest.raises(DesktopError, match="another provider scope"):
        conflicting("bot", tmp_path, cancel=lambda: False)
    second.close()
