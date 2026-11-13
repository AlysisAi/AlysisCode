from __future__ import annotations

import base64
import json

import pytest

from alysis_code.bots.models import BotInput, FileInput, MemoryInput, NoteInput, TaskInput
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, NotFound, Store


def schedule():
    return ScheduleInput.model_validate(
        {
            "name": "Review",
            "prompt": "Review current project notes.",
            "timing": {"kind": "interval", "every_minutes": 5},
        }
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bots.sqlite3")


def test_deleted_bot_leaves_the_account_with_its_files_memories_and_routines(store):
    bot = store.save_bot("alice", BotInput(name="Atlas", purpose="Notes"))
    kept = store.save_bot("alice", BotInput(name="Kept", purpose="Stays"))
    chat = store.create_chat("alice", bot["id"], "Conversation")
    store.save_file("alice", bot["id"], FileInput(name="plan.md", content="Private plan"))
    store.save_file("alice", kept["id"], FileInput(name="other.md", content="Other bot"))
    store.save_memory("alice", bot["id"], MemoryInput(title="Tone", content="Short answers"))
    store.save_schedule("alice", bot["id"], schedule(), now=1000)
    before = store.library_usage("alice")

    store.delete_bot("alice", bot["id"])

    assert [item["id"] for item in store.list_bots("alice")] == [kept["id"]]
    for read in (
        lambda: store.conversation("alice", chat["id"]),
        lambda: store.list_files("alice", bot["id"]),
        lambda: store.list_memories("alice", bot["id"]),
        lambda: store.save_bot("alice", BotInput(name="Back", purpose="Again"), bot["id"]),
        lambda: store.delete_bot("alice", bot["id"]),
    ):
        with pytest.raises(NotFound):
            read()
    with store.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM files WHERE bot_id=?", (bot["id"],)).fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT count(*) FROM memories WHERE bot_id=?", (bot["id"],)).fetchone()[0]
            == 0
        )
    assert store.enqueue_due_schedules("alice", max_calls=20, now=1301) == 0
    assert store.library_usage("alice")["file_count"] == before["file_count"] - 1
    assert [f["name"] for f in store.list_files("alice", kept["id"])] == ["other.md"]


def test_a_bot_with_running_work_is_not_deleted(store):
    bot = store.save_bot("alice", BotInput(name="Busy", purpose="Working"))
    chat = store.create_chat("alice", bot["id"], "Conversation")
    store.enqueue("alice", chat["id"], TaskInput(message="Work", request_id="request_busy"))
    with pytest.raises(Conflict, match="Stop this bot's work"):
        store.delete_bot("alice", bot["id"])
    assert [item["id"] for item in store.list_bots("alice")] == [bot["id"]]


def test_only_the_owner_can_delete_and_the_slot_is_freed(store):
    bot = store.save_bot("alice", BotInput(name="Mine", purpose="Owner check"))
    with pytest.raises(NotFound):
        store.delete_bot("bob", bot["id"])
    for n in range(29):
        store.save_bot("alice", BotInput(name=f"Bot {n}", purpose="Fill"))
    store.delete_bot("alice", bot["id"])
    assert (
        store.save_bot("alice", BotInput(name="Replacement", purpose="Fits again"))["name"]
        == "Replacement"
    )


def test_delete_route_returns_no_content(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "delete.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    bot = app.state.store.save_bot(settings.owner, BotInput(name="Gone", purpose="Route"))
    with TestClient(app) as client:
        assert client.delete(f"/v1/bots/{bot['id']}").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.delete(f"/v1/bots/{bot['id']}").status_code == 204
        assert client.delete(f"/v1/bots/{bot['id']}").status_code == 404
        assert client.get("/v1/bots").json() == []


def test_deleted_bot_leaves_no_content_behind(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.triggers import TriggerInput

    settings = Settings(
        tmp_path / "erase.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    store, owner = app.state.store, settings.owner
    bot = store.save_bot(owner, BotInput(name="Private", purpose="Erase check"))
    chat = store.create_chat(owner, bot["id"], "Conversation")
    task = store.enqueue(
        owner, chat["id"], TaskInput(message="My private question", request_id="request_private")
    )
    store.claim(owner)
    store.event(owner, task["id"], "progress_note", {"text": "Looking now."})
    store.finish(owner, task["id"], "completed", content="My private answer")
    store.save_note(owner, bot["id"], NoteInput(title="Plan", content="Private note"))
    store.save_schedule(owner, bot["id"], schedule(), now=1000)
    app.state.runtime.triggers.create(
        bot["id"], TriggerInput(name="On file", instructions="Review new files")
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        upload = client.post(
            f"/v1/bots/{bot['id']}/uploads",
            json={"name": "half.bin", "size": 6, "request_id": "upload_request_1"},
        )
        assert upload.status_code in (200, 201), upload.text
        client.put(
            f"/v1/bots/{bot['id']}/uploads/{upload.json()['id']}",
            json={"offset": 0, "data_base64": base64.b64encode(b"abc").decode()},
        )
        assert client.delete(f"/v1/bots/{bot['id']}").status_code == 204
        assert client.get(f"/v1/tasks/{task['id']}").status_code == 404
        assert client.post(f"/v1/tasks/{task['id']}/cancel").status_code == 404
        assert client.get(f"/v1/chats/{chat['id']}").status_code == 404
    with store.connect() as db:
        for table, column in (
            ("messages", "chat_id"),
            ("events", "task_id"),
            ("working_notes", "bot_id"),
            ("schedules", "bot_id"),
            ("bot_triggers", "bot_id"),
            ("file_uploads", "bot_id"),
        ):
            key = (
                chat["id"]
                if column == "chat_id"
                else task["id"]
                if column == "task_id"
                else bot["id"]
            )
            count = db.execute(f"SELECT count(*) FROM {table} WHERE {column}=?", (key,)).fetchone()[
                0
            ]
            assert count == 0, table
        row = db.execute("SELECT name,purpose,owner FROM bots WHERE id=?", (bot["id"],)).fetchone()
        assert (row[0], row[1]) == ("", "") and row[2].startswith("deleted:")
        kept = db.execute(
            "SELECT message,output,owner FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
        assert (kept[0], kept[1]) == ("", "") and kept[2] == row[2]


def test_a_bot_can_be_renamed_while_it_works_but_not_reconfigured(store):
    bot = store.save_bot("alice", BotInput(name="Busy", purpose="Working"))
    chat = store.create_chat("alice", bot["id"], "Conversation")
    store.enqueue("alice", chat["id"], TaskInput(message="Work", request_id="request_rename"))
    renamed = store.save_bot("alice", BotInput(name="Busy Bee", purpose="Working"), bot["id"])
    assert renamed["name"] == "Busy Bee" and renamed["updated_at"] == bot["updated_at"]
    for change in ({"purpose": "Something else"}, {"instructions": "New rules"}):
        with pytest.raises(Conflict, match="purpose, instructions, memory or files"):
            store.save_bot(
                "alice", BotInput(**{"name": "Busy Bee", "purpose": "Working", **change}), bot["id"]
            )


def test_a_large_delete_body_is_refused(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "body.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    bot = app.state.store.save_bot(settings.owner, BotInput(name="Kept", purpose="Body"))
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        response = client.request(
            "DELETE",
            f"/v1/bots/{bot['id']}",
            content=b"x" * 600000,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert [item["id"] for item in client.get("/v1/bots").json()] == [bot["id"]]


@pytest.mark.parametrize("mapping_rows", [False, True])
def test_delete_checks_allocation_receipts_with_sqlite_and_postgres_row_contracts(
    tmp_path, monkeypatch, mapping_rows
):
    from contextlib import contextmanager

    from alysis_code.bots.postgres import Row

    store = Store(tmp_path / "allocation-delete.db")
    connect = store.connect

    @contextmanager
    def connection(*, write=False):
        with connect(write=write) as db:
            if mapping_rows:
                db.row_factory = lambda cursor, values: Row(
                    [column[0] for column in cursor.description], values
                )
            yield db

    monkeypatch.setattr(store, "connect", connection)
    bot = store.save_bot("alice", BotInput(name="Retired", purpose="Delete after cleanup"))
    kept = store.save_bot("alice", BotInput(name="Kept", purpose="Other computer"))
    with store.connect(write=True) as db:
        for identity in (bot["id"], kept["id"]):
            db.execute(
                "INSERT INTO computer_allocations VALUES(?,?,?,?)",
                ("alice", identity, json.dumps({"state": "sleeping"}), 0),
            )
    with pytest.raises(Conflict, match="computer to shut down"):
        store.delete_bot("alice", bot["id"])
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE computer_allocations SET record=? WHERE bot_id=?",
            (json.dumps({"state": "deleted"}), bot["id"]),
        )
    store.delete_bot("alice", bot["id"])
    assert [item["id"] for item in store.list_bots("alice")] == [kept["id"]]
    with store.connect() as db:
        record = db.execute(
            "SELECT record FROM computer_allocations WHERE bot_id=?", (kept["id"],)
        ).fetchone()
        assert json.loads(record["record"])["state"] == "sleeping"
