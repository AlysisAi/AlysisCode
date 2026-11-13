"""Immutable revisions, real database migration and resumable transport evidence."""

import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from alysis_code.bots.models import BotInput, FileInput
from alysis_code.bots.store import Conflict, LimitReached, Store
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput


def setup(tmp_path):
    store = Store(tmp_path / "revisions.db")
    bot = store.save_bot("owner", BotInput(name="Editor", purpose="Revise files"))
    return store, bot["id"]


def test_versions_keep_old_bytes_and_one_concurrent_edit_wins(tmp_path):
    store, bot = setup(tmp_path)
    first = store.save_file("owner", bot, FileInput(name="Νεφέλη-東京.md", content="[DATE TBD]\n"))

    def save(text):
        try:
            return store.save_file(
                "owner",
                bot,
                FileInput(name=first["name"], content=text, previous_file_id=first["id"]),
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["6 November\n", "7 November\n"]))
    second = next(r for r in results if r)
    assert sum(r is not None for r in results) == 1
    assert second["revision"] == 2 and second["series_id"] == first["id"]
    reopened = Store(store.path)
    assert reopened.file("owner", bot, first["id"])["content"] == "[DATE TBD]\n"
    assert reopened.file("owner", bot, second["id"])["content"] in ["6 November\n", "7 November\n"]
    assert len(reopened.list_files("owner", bot)) == 2
    assert [r["id"] for r in reopened.list_files("owner", bot, latest_only=True)] == [second["id"]]
    other = store.save_bot("owner", BotInput(name="Other", purpose="Separate"))
    with pytest.raises(Conflict):
        store.save_file(
            "owner",
            other["id"],
            FileInput(name=first["name"], content="no", previous_file_id=second["id"]),
        )
    with pytest.raises(Conflict):
        store.save_file(
            "owner", bot, FileInput(name="renamed.md", content="no", previous_file_id=second["id"])
        )


def test_revision_upload_is_atomic_resumable_and_idempotent(tmp_path):
    store, bot = setup(tmp_path)
    first = store.save_bytes("owner", bot, "work.bin", b"old\0bytes")
    transfers = FileTransfers(store, "owner")
    data = UploadInput(
        name="work.bin", size=9, request_id="revision-upload", previous_file_id=first["id"]
    )
    upload = transfers.begin(bot, data)
    transfers.append(
        bot, upload["id"], UploadChunk(offset=0, data_base64=base64.b64encode(b"new\0").decode())
    )
    assert len(store.list_files("owner", bot)) == 1
    transfers = FileTransfers(Store(store.path), "owner")
    assert transfers.begin(bot, data)["received"] == 4
    transfers.append(
        bot, upload["id"], UploadChunk(offset=4, data_base64=base64.b64encode(b"bytes").decode())
    )
    result = transfers.complete(bot, upload["id"])
    assert transfers.complete(bot, upload["id"]) == result
    assert store.file_data("owner", bot, first["id"]) == b"old\0bytes"
    assert store.file_data("owner", bot, result["file_id"]) == b"new\0bytes"
    with pytest.raises(Conflict):
        transfers.begin(bot, data.model_copy(update={"request_id": "different-upload"}))
    assert len(store.list_files("owner", bot)) == 2


def test_failed_revision_and_quota_preserve_all_saved_versions(tmp_path):
    store, bot = setup(tmp_path)
    first = store.save_bytes("owner", bot, "work.bin", b"1234")

    def broken():
        yield b"12"
        raise RuntimeError("lost source")

    with pytest.raises(RuntimeError):
        store.save_stream("owner", bot, first["name"], broken(), 4, previous_file_id=first["id"])
    assert len(store.list_files("owner", bot)) == 1
    store.set_library_limit("owner", 7)
    with pytest.raises(LimitReached):
        store.save_bytes("owner", bot, first["name"], b"5678", previous_file_id=first["id"])
    assert store.file_data("owner", bot, first["id"]) == b"1234"


def test_legacy_name_unique_table_migrates_without_losing_payloads_or_references(tmp_path):
    store, bot = setup(tmp_path)
    first = store.save_bytes("owner", bot, "original.bin", b"\0legacy\xff")
    # Reproduce the actual v15 table, including its auto-created unique index.
    with sqlite3.connect(store.path) as db:
        db.executescript("""
            CREATE TABLE files_old (
                id TEXT PRIMARY KEY, bot_id TEXT NOT NULL REFERENCES bots(id),
                name TEXT NOT NULL, content TEXT NOT NULL, source_task_id TEXT REFERENCES tasks(id),
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(bot_id,name));
            INSERT INTO files_old SELECT id,bot_id,name,content,source_task_id,created_at,updated_at FROM files;
            DROP TABLE files;
            ALTER TABLE files_old RENAME TO files;
            PRAGMA user_version=15;
        """)
    reopened = Store(store.path)
    assert reopened.file_data("owner", bot, first["id"]) == b"\0legacy\xff"
    second = reopened.save_bytes("owner", bot, first["name"], b"new", previous_file_id=first["id"])
    assert second["revision"] == 2
    with reopened.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(Conflict):
        reopened.delete_item("owner", bot, first["id"], kind="files")
    reopened.delete_item("owner", bot, second["id"], kind="files")
    assert reopened.list_files("owner", bot) == []
    with reopened.connect() as db:
        assert db.execute("SELECT count(*) FROM file_payloads").fetchone()[0] == 0


def test_model_can_list_old_revisions_without_changing_latest_context(tmp_path):
    from alysis_code.bots.models import TaskInput
    from alysis_code.bots.runtime import BotRuntime

    store, bot = setup(tmp_path)
    first = store.save_file("owner", bot, FileInput(name="guide.md", content="draft"))
    second = store.save_file(
        "owner", bot, FileInput(name="guide.md", content="final", previous_file_id=first["id"])
    )
    chat = store.create_chat("owner", bot, "History")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Compare versions", request_id="compare-versions")
    )
    task = store.claim("owner")
    runtime = BotRuntime(store, "owner", lambda _: None)
    assert [f["id"] for f in runtime._tool(task, "list_files", {}, ["list_files"])["items"]] == [
        second["id"]
    ]
    assert (
        len(runtime._tool(task, "list_files", {"include_versions": True}, ["list_files"])["items"])
        == 2
    )
