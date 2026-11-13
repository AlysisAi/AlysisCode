"""Account Library accounting, atomic admission and crash/retry release.

Real SQL stores and local immutable object adapter; not cloud capacity evidence.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_bots_memory_changes import case as shared_case

from alysis_code.bots.models import BotInput, FileInput, MemoryInput, NoteInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Conflict, LibraryCapacityExceeded, LimitReached, Store
from alysis_code.bots.transfers import FileTransfers, UploadInput

case = shared_case


def test_sqlite_capacity_upgrade_keeps_old_policy_before_explicit_uncapped_selection(tmp_path):
    store = Store(tmp_path / "upgrade.db")
    bot = store.save_bot("alpha", BotInput(name="Kept", purpose="Existing account"))
    with store.connect(write=True) as db:
        db.execute("DROP TABLE computer_capacity")
        db.execute(
            "CREATE TABLE computer_capacity (scope TEXT PRIMARY KEY,max_active INTEGER NOT NULL CHECK(max_active>0))"
        )
        db.execute("INSERT INTO computer_capacity VALUES('kept-scope',2)")
        db.execute("PRAGMA user_version=37")
    reopened = Store(store.path)
    assert reopened.bot("alpha", bot["id"])["name"] == "Kept"
    with reopened.connect(write=True) as db:
        assert db.execute("SELECT max_active FROM computer_capacity").fetchone()[0] == 2
        db.execute("UPDATE computer_capacity SET max_active=NULL")
    assert reopened.library_usage("alpha")["limit_bytes"] is None


def test_owner_allowance_combines_bots_but_excludes_memory_notes_and_other_accounts(case):
    store, owner, bot, _ = case
    second = store.save_bot(owner, BotInput(name="Second", purpose="Shared allowance"))
    separate = store.save_bot(
        "separate-" + owner, BotInput(name="Other account", purpose="Private")
    )
    assert store.library_usage(owner)["limit_bytes"] is None
    store.set_library_limit(owner, 12)
    store.save_file(owner, bot["id"], FileInput(name="one.txt", content="東京"))  # 6 UTF-8 bytes
    store.save_bytes(owner, second["id"], "two.bin", b"abcd")
    store.save_memory(
        owner, bot["id"], MemoryInput(title="Preference", content="Long memory " * 100)
    )
    store.save_note(owner, bot["id"], NoteInput(title="Note", content="Project context " * 100))
    store.save_bytes("separate-" + owner, separate["id"], "other.bin", b"x" * 100)
    usage = store.library_usage(owner)
    assert usage == dict(
        scope="account_library",
        file_count=2,
        used_bytes=10,
        reserved_bytes=0,
        limit_bytes=12,
        available_bytes=2,
    )
    with pytest.raises(LibraryCapacityExceeded, match="shared Library") as rejected:
        store.save_bytes(owner, second["id"], "overflow.bin", b"123")
    assert "requires 3 bytes, but 2 bytes are available" in str(rejected.value)
    assert "10 saved and 0 reserved of 12 bytes" in str(rejected.value)
    assert "Free Library space" in str(rejected.value)
    assert store.library_usage(owner) == usage
    with pytest.raises(LibraryCapacityExceeded, match="deleting other Library files cannot"):
        store.save_bytes(owner, second["id"], "alone-too-large.bin", b"x" * 13)
    assert store.library_usage(owner) == usage
    before = store.list_files(owner, bot["id"])
    store.set_library_limit(owner, 0)
    assert store.list_files(owner, bot["id"]) == before
    assert store.library_usage(owner)["available_bytes"] == 0
    store.set_library_limit(owner, None)
    saved = store.save_bytes(owner, second["id"], "no-plan.bin", b"123")
    assert store.library_usage(owner)["used_bytes"] == 13
    store.delete_item(owner, second["id"], saved["id"], kind="files")
    assert store.library_usage(owner)["used_bytes"] == 10
    assert store.library_usage("separate-" + owner)["used_bytes"] == 100


def test_concurrent_uploads_from_two_bots_reserve_one_shared_allowance(case):
    store, owner, bot, _ = case
    second = store.save_bot(owner, BotInput(name="Second", purpose="Share"))
    store.set_library_limit(owner, 10)
    transfers = FileTransfers(store, owner)

    def begin(item):
        try:
            return (
                item,
                transfers.begin(
                    item, UploadInput(name="data.bin", size=7, request_id="shared-request")
                ),
            )
        except LimitReached:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, [bot["id"], second["id"]]))
    assert sum(r is not None for r in results) == 1
    winner = next(r for r in results if r)
    assert store.library_usage(owner)["reserved_bytes"] == 7
    assert (
        transfers.begin(
            winner[0], UploadInput(name="data.bin", size=7, request_id="shared-request")
        )["id"]
        == winner[1]["id"]
    )
    assert store.library_usage(owner)["reserved_bytes"] == 7
    transfers.cancel(winner[0], winner[1]["id"])
    assert store.library_usage(owner)["reserved_bytes"] == 0
    retry = transfers.begin(
        second["id"], UploadInput(name="retry.bin", size=10, request_id="retry-request")
    )
    with store.connect(write=True) as db:
        db.execute("UPDATE file_uploads SET expires=0 WHERE id=?", (retry["id"],))
    assert store.library_usage(owner)["reserved_bytes"] == 0
    store.save_bytes(owner, bot["id"], "full.bin", b"1234567890")
    assert store.library_usage(owner)["used_bytes"] == 10


def test_object_network_stage_reserves_across_bots_and_releases_on_failure(tmp_path):
    started, finish = Event(), Event()

    class BlockingObjects(LocalObjectStorage):
        fail = False

        def _upload(self, ref, stream):
            started.set()
            assert finish.wait(10)
            if self.fail:
                raise RuntimeError("Synthetic object outage")
            return super()._upload(ref, stream)

    objects = BlockingObjects(tmp_path / "objects")
    store = Store(tmp_path / "store.db", objects=objects)
    bot = store.save_bot("owner", BotInput(name="First", purpose="Export"))
    other = store.save_bot("owner", BotInput(name="Second", purpose="Upload"))
    store.set_library_limit("owner", 10)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.save_bytes, "owner", bot["id"], "export.bin", b"12345678")
        try:
            assert started.wait(10)
            usage = store.library_usage("owner")
            assert usage["used_bytes"] == 0 and usage["reserved_bytes"] == 8
            with pytest.raises(LimitReached):
                FileTransfers(store, "owner").begin(
                    other["id"], UploadInput(name="data", size=3, request_id="concurrent-upload")
                )
        finally:
            finish.set()
        saved = future.result()
    assert store.library_usage("owner")["used_bytes"] == 8
    assert store.library_usage("owner")["reserved_bytes"] == 0
    store.delete_item("owner", bot["id"], saved["id"], kind="files")
    assert (
        store.library_usage("owner")["used_bytes"] == 0
    )  # logical deletion, physical retention separate
    objects.fail = True
    with pytest.raises(RuntimeError, match="Synthetic"):
        store.save_bytes("owner", bot["id"], "failed", b"12345678")
    assert store.library_usage("owner")["reserved_bytes"] == 0
    assert store.library_usage("owner")["used_bytes"] == 0


def test_staged_reservation_survives_restart_and_expiry_cannot_publish(tmp_path):
    objects = LocalObjectStorage(tmp_path / "objects")
    store = Store(tmp_path / "store.db", objects=objects)
    bot = store.save_bot("owner", BotInput(name="Files", purpose="Test"))
    store.set_library_limit("owner", 10)
    data = FileInput(name="staged.txt", content="123456")
    ref = store.stage_file_object("owner", bot["id"], data, None, None)
    restored = Store(store.path, objects=objects)
    assert restored.library_usage("owner")["reserved_bytes"] == 6
    with restored.connect(write=True) as db:
        db.execute("UPDATE library_reservations SET expires=0 WHERE id=?", (ref.key,))
    assert restored.library_usage("owner")["reserved_bytes"] == 0
    with pytest.raises(Conflict, match="expired"):
        restored._save_file("owner", bot["id"], data, object_ref=ref)
    assert restored.list_files("owner", bot["id"]) == []
    assert store.library_usage("owner")["limit_bytes"] == 10


def test_library_usage_api_is_account_scoped_and_cannot_change_allowance(tmp_path):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    store = Store(tmp_path / "api.db")
    own = store.save_bot("owner", BotInput(name="My bot", purpose="Store"))
    other = store.save_bot("other", BotInput(name="Their bot", purpose="Store"))
    store.save_bytes("owner", own["id"], "one", b"123")
    store.save_bytes("other", other["id"], "two", b"1234567")
    settings = Settings(
        tmp_path / "api.db",
        "synthetic-library-token-for-disposable-api-tests",
        "unused",
        "https://fixture.invalid",
        "fixture",
        owner="owner",
    )
    with TestClient(
        create_app(settings, task_store=store, client_factory=lambda _: None)
    ) as client:
        assert client.get("/v1/storage/library").status_code == 401
        headers = {"Authorization": "Bearer synthetic-library-token-for-disposable-api-tests"}
        response = client.get("/v1/storage/library", headers=headers)
        assert response.status_code == 200 and response.json()["used_bytes"] == 3
        assert response.json()["limit_bytes"] is None
        assert (
            client.post(
                "/v1/storage/library", headers=headers, json={"byte_limit": 1000000}
            ).status_code
            == 405
        )
