"""Consolidated revisions retain paged retrieval, shared accounting and both stores."""

import base64
import hashlib
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_bots_backup import databases as backup_databases

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, QuestionDecision
from alysis_code.bots.cloud_desktop import CloudDesktopFiles
from alysis_code.bots.files import FileCursor, FileQuery
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, Store
from alysis_code.bots.transfers import FileTransfers, UploadChunk, UploadInput
from alysis_code.cancellation import EventCancellationToken

databases = backup_databases


@pytest.mark.parametrize(
    "payload", [b"new text", b"\x00binary", b"\x00" * 110000], ids=["text", "binary", "stream"]
)
def test_cloud_export_preserves_revision_identity_for_all_transfer_paths(tmp_path, payload):
    store = Store(tmp_path / "state.db")
    bot = store.save_bot("owner", BotInput(name="Export", purpose="Keep old bytes"))["id"]
    first = store.save_bytes("owner", bot, "result.dat", b"original")
    allocation = uuid4().hex
    executor = SimpleNamespace(
        identity=SimpleNamespace(allocation_id=allocation),
        ref=None,
        provider=SimpleNamespace(download=lambda *args, **kwargs: (part for part in [payload])),
        _request=lambda *args, **kwargs: {
            "path": f"/tmp/alysis-cloud-{allocation}/export-{uuid4().hex}",
            "name": first["name"],
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    saved = CloudDesktopFiles().export_file(
        executor,
        store,
        "owner",
        {"id": None, "bot_id": bot},
        first["name"],
        EventCancellationToken(threading.Event()),
        previous_file_id=first["id"],
    )
    assert saved["revision"] == 2
    assert store.file_data("owner", bot, first["id"]) == b"original"
    assert store.file_data("owner", bot, saved["id"]) == payload


def exercise(store):
    owner = "revision-integration"
    bot = store.save_bot(owner, BotInput(name="Editor", purpose="Keep exact revisions"))["id"]
    first = store.save_bytes(owner, bot, "東京.bin", b"old\x00")
    transfer = FileTransfers(store, owner)
    upload = transfer.begin(
        bot,
        UploadInput(
            name=first["name"],
            size=4,
            previous_file_id=first["id"],
            request_id="revision-integration-upload",
        ),
    )
    transfer.append(
        bot, upload["id"], UploadChunk(offset=0, data_base64=base64.b64encode(b"new\x00").decode())
    )
    result = transfer.complete(bot, upload["id"])
    second = store.file(owner, bot, result["file_id"])
    assert second["is_latest"] and second["revision"] == 2
    assert store.file_data(owner, bot, first["id"]) == b"old\x00"
    assert store.file_data(owner, bot, second["id"]) == b"new\x00"
    assert store.library_usage(owner)["used_bytes"] == 8
    assert store.library_usage(owner)["reserved_bytes"] == 0
    assert [f["id"] for f in store.files_page(owner, bot, FileQuery())["items"]] == [second["id"]]
    page = store.files_page(owner, bot, FileQuery(series_id=first["id"], limit=1))
    older = store.files_page(
        owner,
        bot,
        FileQuery(series_id=first["id"], limit=1, before=FileCursor(**page["next_before"])),
    )
    assert [f["id"] for f in older["items"]] == [first["id"]]
    with pytest.raises(Conflict):
        store.delete_item(owner, bot, first["id"], kind="files")

    chat = store.create_chat(owner, bot, "Continuous")
    store.enqueue(
        owner, chat["id"], TaskInput(message="Prepare a draft", request_id="revision-question")
    )
    task = store.claim(owner)
    questions = ClarificationStore(store, owner)
    question = questions.ask(task, AskQuestion(question="Which date?"))
    BackgroundStore(store, owner).submit(task, "Prepare independent sections", [])
    answered = questions.decide(
        bot,
        question["id"],
        QuestionDecision(action="answer", expected_revision=1, answer="6 November 東京"),
    )
    assert answered["delivered_to_source"]
    with store.connect() as db:
        rows = db.execute(
            "SELECT content FROM background_instructions WHERE task_id=?", (task["id"],)
        ).fetchall()
        assert [r[0] for r in rows] == ["6 November 東京"]
    store.delete_item(owner, bot, second["id"], kind="files")
    assert store.library_usage(owner)["used_bytes"] == 0


@pytest.mark.parametrize("external", [False, True])
def test_sqlite_revision_accounting_and_question_delivery(tmp_path, external):
    objects = LocalObjectStorage(tmp_path / "objects") if external else None
    exercise(Store(tmp_path / "state.db", objects=objects))


def test_postgres_revision_accounting_and_question_delivery(databases):
    exercise(databases())


def test_postgres_v25_upgrade_preserves_objects_and_pending_uploads(databases):
    store = databases()
    bot = store.save_bot("owner", BotInput(name="Legacy", purpose="Preserve files"))["id"]
    first = store.save_file("owner", bot, FileInput(name="legacy.txt", content="Αθήνα"))
    transfer = FileTransfers(store, "owner")
    pending = transfer.begin(
        bot, UploadInput(name="pending.bin", size=1, request_id="legacy-pending")
    )
    with store.connect(write=True) as db:
        db.execute("DROP INDEX files_series")
        db.execute("ALTER TABLE files DROP COLUMN series_id")
        db.execute("ALTER TABLE files DROP COLUMN revision")
        db.execute("ALTER TABLE files ADD UNIQUE(bot_id,name)")
        db.execute("ALTER TABLE file_uploads DROP COLUMN previous_file_id")
        db.execute("DROP TABLE clarifications")
        db.execute("UPDATE bots_schema SET version=25")
    reopened = PostgresStore(
        store._dsn, objects=store.objects, state_directory=store.path.parent / "reopened"
    )
    try:
        assert reopened.file("owner", bot, first["id"])["content"] == "Αθήνα"
        revised = reopened.save_file(
            "owner",
            bot,
            FileInput(name=first["name"], content="東京", previous_file_id=first["id"]),
        )
        assert revised["revision"] == 2
        transfer = FileTransfers(reopened, "owner")
        transfer.append(bot, pending["id"], UploadChunk(offset=0, data_base64="YQ=="))
        assert transfer.complete(bot, pending["id"])["status"] == "complete"
        with reopened.connect() as db:
            assert db.execute("SELECT version FROM bots_schema").fetchone()[0] == SCHEMA_VERSION
    finally:
        reopened.close()


def test_sqlite_v25_upgrade_preserves_external_files(tmp_path):
    objects = LocalObjectStorage(tmp_path / "objects")
    store = Store(tmp_path / "state.db", objects=objects)
    bot = store.save_bot("owner", BotInput(name="Legacy", purpose="Preserve files"))["id"]
    original = store.save_bytes("owner", bot, "legacy.bin", b"\x00legacy")
    with sqlite3.connect(store.path) as db:
        db.executescript("""
            CREATE TABLE files_old (id TEXT PRIMARY KEY, bot_id TEXT NOT NULL REFERENCES bots(id),
                name TEXT NOT NULL, content TEXT NOT NULL, source_task_id TEXT REFERENCES tasks(id),
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(bot_id,name));
            INSERT INTO files_old SELECT id,bot_id,name,content,source_task_id,created_at,updated_at FROM files;
            DROP TABLE files;
            ALTER TABLE files_old RENAME TO files;
            ALTER TABLE file_uploads DROP COLUMN previous_file_id;
            DROP TABLE clarifications;
            PRAGMA user_version=25;
        """)
    reopened = Store(store.path, objects=objects)
    assert reopened.file_data("owner", bot, original["id"]) == b"\x00legacy"
    assert (
        reopened.save_bytes(
            "owner", bot, original["name"], b"next", previous_file_id=original["id"]
        )["revision"]
        == 2
    )
    with reopened.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
