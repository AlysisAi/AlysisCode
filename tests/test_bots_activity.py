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


def test_activity_counts_only_finishing_events_after_the_read_receipt(tmp_path):
    store, bot, chat = setup(tmp_path)
    first, first_seq = finish(store, chat)
    store.mark_chat_read("alice", chat["id"], first_seq)
    # Steps after a finished task (and a running one) are activity, but not unread work.
    store.event("alice", first["id"], "reply_suggestions", {"items": ["Συνέχεια"]})
    running = store.enqueue(
        "alice", chat["id"], TaskInput(message="Next step", request_id="req_" + uuid4().hex)
    )
    store.claim("alice")
    store.event("alice", running["id"], "tool_started", {"name": "browser"})
    listed = store.list_bots("alice")[0]
    assert listed["unread_chats"] == 0
    assert listed["latest_activity"]["task_id"] == running["id"]
    assert store.conversation("alice", chat["id"])["activity_seq"] == first_seq
    store.finish("alice", running["id"], "failed", error="Example failure")
    assert store.list_bots("alice")[0]["unread_chats"] == 1
    assert store.conversation("alice", chat["id"])["activity_seq"] > first_seq


def test_owl_list_reads_indexes_not_every_event(tmp_path, monkeypatch):
    """The list is read every few seconds for every owl; its cost must not grow with
    every step an owl ever took, so events are reached only through indexes."""
    store, _bot, chat = setup(tmp_path)
    finish(store, chat)
    statements: list[str] = []
    connect = sqlite3.connect

    def tracing(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("alysis_code.bots.store.sqlite3.connect", tracing)
    store.list_bots("alice")
    monkeypatch.setattr("alysis_code.bots.store.sqlite3.connect", connect)
    reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT") and "events" in statement
    ]
    assert reads
    with connect(store.path) as db:
        for statement in reads:
            plan = [row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + statement)]
            for step in plan:
                assert not step.startswith("SCAN"), (statement, plan)
                if step.startswith("SEARCH e "):
                    # One event by its key, or an index that holds what is read.
                    assert "COVERING INDEX" in step or "PRIMARY KEY" in step, (statement, plan)


def test_the_owl_list_says_what_a_running_owl_is_doing_now(tmp_path):
    from alysis_code.bots.runtime import step_started

    store, bot, chat = setup(tmp_path)
    assert store.list_bots("alice")[0]["now_doing"] is None
    task = store.enqueue(
        "alice", chat["id"], TaskInput(message="Σύγκρινε τιμές", request_id="req_" + uuid4().hex)
    )
    store.claim("alice")
    store.event("alice", task["id"], "progress_note", {"text": "Ελέγχω τρία καταστήματα τώρα."})
    store.event(
        "alice",
        task["id"],
        "tool_started",
        step_started("browser_navigate", {"url": "https://例え.jp/mugs?q=secret#top"}),
    )
    store.event("alice", task["id"], "tool_started", step_started("browser_observe", {}))
    now = store.list_bots("alice")[0]["now_doing"]
    assert now == {
        "task_id": task["id"],
        "step": "browser_observe",
        "topic": None,
        "file": None,
        "note": "Ελέγχω τρία καταστήματα τώρα.",
        "host": "例え.jp",  # the site only, never the path or query
    }
    store.event(
        "alice",
        task["id"],
        "tool_started",
        step_started("web_search", {"query": "  τιμή  Kobo\nClara Colour  "}),
    )
    now = store.list_bots("alice")[0]["now_doing"]
    assert (now["step"], now["topic"]) == ("web_search", "τιμή Kobo Clara Colour")
    store.finish("alice", task["id"], "completed", content="Έτοιμο.")
    assert store.list_bots("alice")[0]["now_doing"] is None


def test_the_owl_list_follows_work_that_continues_in_the_background(tmp_path):
    from alysis_code.bots.background import BackgroundStore
    from alysis_code.bots.runtime import step_started

    store, bot, chat = setup(tmp_path)
    task = store.enqueue(
        "alice", chat["id"], TaskInput(message="Research flights", request_id="req_" + uuid4().hex)
    )
    claimed = store.claim("alice")
    background = BackgroundStore(store, "alice")
    background.submit(claimed, "Research flights", [])
    assert store.list_bots("alice")[0]["now_doing"]["task_id"] == task["id"]  # queued
    background.claim()
    store.event(
        "alice", task["id"], "tool_started", step_started("save_file", {"name": "Πτήσεις.md"})
    )
    now = store.list_bots("alice")[0]["now_doing"]
    assert (now["step"], now["file"]) == ("save_file", "Πτήσεις.md")
    store.finish("alice", task["id"], "completed", content="Done.")
    assert store.list_bots("alice")[0]["now_doing"] is None


def test_a_long_search_topic_is_cut_at_a_word():
    from alysis_code.bots.runtime import STEP_DETAIL_CHARACTERS, step_started

    topic = step_started("web_search", {"query": "word " * 40})["topic"]
    assert len(topic) <= STEP_DETAIL_CHARACTERS + 1
    assert topic.endswith("word…")
    assert "topic" not in step_started("web_search", {"query": "   "})
    assert "file" not in step_started("web_fetch", {"name": "not a file"})


@pytest.mark.parametrize(
    "arguments, host",
    [
        ({"url": "http://Example.ORG:8080/a"}, "example.org"),
        ({"url": "file:///etc/hosts"}, None),
        ({"url": "not a url"}, None),
        ({"url": 7}, None),
        ({}, None),
        (None, None),
    ],
)
def test_a_step_keeps_only_the_host_of_a_web_address(arguments, host):
    from alysis_code.bots.runtime import step_started

    assert step_started("web_fetch", arguments).get("host") == host


def test_the_owl_list_shows_the_question_waiting_for_the_person(tmp_path):
    from alysis_code.bots.clarifications import AskQuestion, ClarificationStore

    store, bot, chat = setup(tmp_path)
    assert store.list_bots("alice")[0]["asks_you"] is None
    task = store.enqueue(
        "alice", chat["id"], TaskInput(message="Γράψε το email", request_id="req_" + uuid4().hex)
    )
    claimed = store.claim("alice")
    ClarificationStore(store, "alice").ask(
        claimed, AskQuestion(question="Ποιο θέμα να μπει;", options=["Α", "Β"])
    )
    # While the owl is still writing the message that asks it, nobody has been asked yet.
    assert store.list_bots("alice")[0]["asks_you"] is None
    store.finish("alice", task["id"], "completed", content="Ποιο θέμα να μπει;")
    assert store.list_bots("alice")[0]["asks_you"] == {
        "kind": "question",
        "text": "Ποιο θέμα να μπει;",
    }
    # A question from work that stopped no longer waits on anyone.
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task["id"],))
    assert store.list_bots("alice")[0]["asks_you"] is None
