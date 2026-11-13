"""Durable activity receipts: these tests exercise storage, not a model fixture."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import NotFound, Store


def setup(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Έλενα", purpose="Plan projects"))
    chat = store.create_chat("alice", bot["id"], "Decisions 日本語")
    return store, bot, chat


def finish(store, chat, *, status="completed", text="Updated plan 日本語"):
    task = store.enqueue(
        "alice",
        chat["id"],
        TaskInput(
            message="Review current decisions",
            request_id="req_" + uuid4().hex,
        ),
    )
    claimed = store.claim("alice")
    assert claimed["id"] == task["id"]
    store.finish(
        "alice",
        task["id"],
        status,
        content=text,
        error="Example failure" if status == "failed" else None,
    )
    return task, store.conversation("alice", chat["id"])["activity_seq"]


def test_activity_is_unread_until_the_observed_receipt_is_acknowledged(tmp_path):
    store, bot, chat = setup(tmp_path)
    assert store.list_bots("alice")[0]["latest_activity"] is None
    assert store.list_bots("alice")[0]["unread_chats"] == 0
    task, seq = finish(store, chat)
    listed = store.list_bots("alice")[0]
    assert listed["unread_chats"] == 1
    assert listed["latest_activity"]["task_id"] == task["id"]
    assert listed["latest_activity"]["preview"] == "Updated plan 日本語"
    assert listed["latest_activity"]["title"] == chat["title"]
    assert store.list_chats("alice", bot["id"])[0]["unread"]
    assert store.conversation("alice", chat["id"])["unread"]
    assert store.mark_chat_read("alice", chat["id"], seq) == {"read_seq": seq}
    reopened = Store(store.path)
    assert reopened.list_bots("alice")[0]["unread_chats"] == 0
    assert not reopened.conversation("alice", chat["id"])["unread"]


def test_delayed_and_out_of_order_receipts_do_not_hide_new_work(tmp_path):
    store, bot, chat = setup(tmp_path)
    _, first = finish(store, chat)
    _, second = finish(store, chat)
    store.mark_chat_read("alice", chat["id"], first)
    assert store.conversation("alice", chat["id"])["unread"]
    store.mark_chat_read("alice", chat["id"], second)
    assert store.mark_chat_read("alice", chat["id"], first)["read_seq"] == second
    assert not store.conversation("alice", chat["id"])["unread"]
    peer = store.create_chat("alice", bot["id"], "Background result")
    finish(store, peer)
    assert store.list_bots("alice")[0]["unread_chats"] == 1
    assert store.list_chats("alice", bot["id"])[0]["id"] == peer["id"]
    assert peer["id"] == chat["id"]
    assert store.conversation("alice", chat["id"])["unread"]


def test_receipt_must_belong_to_the_owned_bot(tmp_path):
    store, bot, chat = setup(tmp_path)
    _, seq = finish(store, chat)
    other = store.save_bot("alice", BotInput(name="Other", purpose="Separate bot"))
    peer = store.create_chat("alice", other["id"], "Other conversation")
    for owner, chat_id, receipt in [
        ("bob", chat["id"], seq),
        ("alice", peer["id"], seq),
        ("alice", chat["id"], seq + 100),
    ]:
        with pytest.raises(NotFound):
            store.mark_chat_read(owner, chat_id, receipt)
    assert store.list_bots("bob") == []
    assert store.list_bots("alice")[0]["unread_chats"] == 1


def test_running_events_are_not_unread_results_but_failures_are(tmp_path):
    store, bot, chat = setup(tmp_path)
    task = store.enqueue(
        "alice", chat["id"], TaskInput(message="Check context", request_id="running_request")
    )
    store.claim("alice")
    running = store.list_bots("alice")[0]
    assert running["active_tasks"] == 1
    assert running["unread_chats"] == 0
    assert running["latest_activity"]["preview"] == "Check context"
    with pytest.raises(NotFound):
        store.mark_chat_read("alice", chat["id"], running["latest_activity"]["seq"])
    store.finish("alice", task["id"], "failed", error="No provider allowance")
    assert store.list_bots("alice")[0]["unread_chats"] == 1
    assert store.list_chats("alice", bot["id"])[0]["last_task_status"] == "failed"


def test_v3_database_migrates_without_losing_work(tmp_path):
    store, bot, chat = setup(tmp_path)
    finish(store, chat)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE chat_read_cursors")
        db.execute("PRAGMA user_version=3")
    migrated = Store(store.path)
    assert (
        migrated.conversation("alice", chat["id"])["messages"][-1]["content"]
        == "Updated plan 日本語"
    )
    assert migrated.list_bots("alice")[0]["unread_chats"] == 1
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
