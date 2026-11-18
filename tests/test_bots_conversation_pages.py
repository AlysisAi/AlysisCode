"""A long owl conversation reads a page at a time, and polling brings back only what changed."""

from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.models import BotInput, ConversationPageQuery, TaskInput
from alysis_code.bots.store import NotFound, Store


def setup(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Έλενα", purpose="Plan projects"))
    chat = store.create_chat("alice", bot["id"], "Σχέδια 日本語")
    return store, bot, chat


def send(store, chat, text):
    return store.enqueue(
        "alice", chat["id"], TaskInput(message=text, request_id="req_" + uuid4().hex)
    )


def turn(store, chat, number, *, status="completed"):
    task = send(store, chat, f"Ερώτηση {number} 質問")
    assert store.claim("alice")["id"] == task["id"]
    store.finish(
        "alice",
        task["id"],
        status,
        content=f"Απάντηση {number} 答え" if status == "completed" else "",
        error="Example failure" if status == "failed" else None,
    )
    return task


def page(store, chat, **options):
    return store.conversation_page("alice", chat["id"], ConversationPageQuery(**options))


def poll(store, chat, last, **options):
    newest = last["messages"][-1]["id"] if last["messages"] else None
    return page(store, chat, after=newest, version=last["version"], **options)


def test_the_latest_page_holds_the_newest_messages_and_their_tasks(tmp_path):
    store, _, chat = setup(tmp_path)
    tasks = [turn(store, chat, number) for number in range(6)]
    latest = page(store, chat, limit=4)
    whole = store.conversation("alice", chat["id"])
    assert [m["id"] for m in latest["messages"]] == [m["id"] for m in whole["messages"][-4:]]
    assert [t["id"] for t in latest["tasks"]] == [t["id"] for t in tasks[-2:]]
    assert latest["earlier"] == latest["messages"][0]["id"]
    assert latest["continues"] is False and latest["unchanged"] is False
    # Each message and task has the same shape the whole conversation gives.
    assert latest["messages"][-1] == whole["messages"][-1]
    assert latest["tasks"][-1] == whole["tasks"][-1]
    assert latest["messages"][0]["files"] == []
    assert {latest[key] for key in ("activity_seq", "unread")} == {
        whole[key] for key in ("activity_seq", "unread")
    }


def test_earlier_pages_reach_the_start_with_every_message_and_task_once(tmp_path):
    store, _, chat = setup(tmp_path)
    for number in range(7):
        turn(store, chat, number, status="failed" if number == 3 else "completed")
    whole = store.conversation("alice", chat["id"])
    current = page(store, chat, limit=3)
    messages, tasks = current["messages"], {t["id"]: t for t in current["tasks"]}
    while current["earlier"]:
        current = page(store, chat, limit=3, before=current["earlier"])
        messages = current["messages"] + messages
        tasks.update({t["id"]: t for t in current["tasks"]})
    assert messages == whole["messages"]
    # The failed turn has no reply; its task still comes with the page of its message.
    assert sorted(tasks) == sorted(t["id"] for t in whole["tasks"])
    assert all(tasks[t["id"]] == t for t in whole["tasks"])


def test_polling_returns_nothing_until_something_changes_then_only_the_change(tmp_path):
    store, _, chat = setup(tmp_path)
    for number in range(5):
        turn(store, chat, number)
    first = page(store, chat, limit=4)
    quiet = poll(store, chat, first, limit=4)
    assert quiet["unchanged"] is True
    assert quiet["messages"] == [] and quiet["tasks"] == []
    assert quiet["version"] == first["version"]

    task = send(store, chat, "Νέο μήνυμα 新しい")
    sent = poll(store, chat, first, limit=4)
    assert sent["continues"] is True and sent["unchanged"] is False
    assert [m["content"] for m in sent["messages"]] == ["Νέο μήνυμα 新しい"]
    assert [t["id"] for t in sent["tasks"]] == [task["id"]]
    shown = {"messages": first["messages"] + sent["messages"], "version": sent["version"]}

    store.claim("alice")
    store.event("alice", task["id"], "progress_note", {"text": "Ψάχνω 探しています"})
    working = poll(store, chat, shown, limit=4)
    assert working["messages"] == []
    assert [t["id"] for t in working["tasks"]] == [task["id"]]
    assert working["tasks"][0]["status"] == "running"
    assert working["tasks"][0]["progress_notes"] == ["Ψάχνω 探しています"]
    shown["version"] = working["version"]

    store.finish("alice", task["id"], "completed", content="Έτοιμο 完了")
    done = poll(store, chat, shown, limit=4)
    assert [m["content"] for m in done["messages"]] == ["Έτοιμο 完了"]
    assert [(t["id"], t["status"]) for t in done["tasks"]] == [(task["id"], "completed")]
    assert done["activity_seq"] > first["activity_seq"] and done["unread"]


def test_reading_the_chat_changes_the_version(tmp_path):
    store, _, chat = setup(tmp_path)
    turn(store, chat, 1)
    first = page(store, chat)
    store.mark_chat_read("alice", chat["id"], first["activity_seq"])
    read = poll(store, chat, first)
    assert read["unchanged"] is False and read["unread"] is False
    assert read["messages"] == [] and read["tasks"] == []
    assert poll(store, chat, {**first, "version": read["version"]})["unchanged"] is True


def test_work_that_started_long_ago_still_reports_how_it_ended(tmp_path):
    store, _, chat = setup(tmp_path)
    background = send(store, chat, "Μεγάλη δουλειά 長い仕事")
    store.claim("alice")
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (background["id"],))
        db.execute(
            "INSERT INTO background_turns VALUES(?,'running','work','[]',0)",
            (background["id"],),
        )
    for number in range(4):
        turn(store, chat, number)
    first = page(store, chat, limit=2)
    assert background["id"] not in {m["task_id"] for m in first["messages"]}
    assert background["id"] in {t["id"] for t in first["tasks"]}

    store.finish("alice", background["id"], "failed", error="Stopped 停止")
    ended = poll(store, chat, first, limit=2)
    assert ended["continues"] is True and ended["messages"] == []
    assert [(t["id"], t["status"]) for t in ended["tasks"]] == [(background["id"], "failed")]
    # Finished and out of the page, it no longer changes, so later reads leave it out.
    assert background["id"] not in ended["version"]
    later = poll(store, chat, {**first, "version": ended["version"]}, limit=2)
    assert later["unchanged"] is True


def test_many_new_messages_replace_the_page_instead_of_leaving_a_gap(tmp_path):
    store, _, chat = setup(tmp_path)
    turn(store, chat, 0)
    first = page(store, chat, limit=3)
    for number in range(1, 4):
        turn(store, chat, number)
    caught_up = poll(store, chat, first, limit=3)
    assert caught_up["continues"] is False
    whole = store.conversation("alice", chat["id"])
    assert caught_up["messages"] == whole["messages"][-3:]
    assert caught_up["earlier"] == caught_up["messages"][0]["id"]
    # A message the service doesn't know (another owl's, or from before a reset) starts again.
    stranger = page(store, chat, limit=3, after="m" * 32, version=first["version"])
    assert stranger["continues"] is False
    assert stranger["messages"] == whole["messages"][-3:]


def test_a_page_reaches_back_to_a_turn_found_by_search(tmp_path):
    store, bot, chat = setup(tmp_path)
    tasks = [turn(store, chat, number) for number in range(6)]
    whole = store.conversation("alice", chat["id"])
    reached = page(store, chat, limit=2, through_task=tasks[1]["id"])
    assert reached["messages"] == whole["messages"][2:]
    assert reached["earlier"] == reached["messages"][0]["id"]
    assert [t["id"] for t in reached["tasks"]] == [t["id"] for t in tasks[1:]]
    # From an earlier page too: only what lies before the oldest message the client has.
    latest = page(store, chat, limit=2)
    earlier = page(store, chat, limit=1, before=latest["earlier"], through_task=tasks[0]["id"])
    assert earlier["messages"] == whole["messages"][:-2]
    assert earlier["earlier"] is None
    # Already on screen: nothing more is read.
    same = page(store, chat, limit=2, through_task=tasks[-1]["id"])
    assert same["messages"] == whole["messages"][-2:]
    other = store.save_bot("alice", BotInput(name="Άλλη"))
    other_chat = store.create_chat("alice", other["id"], "x")
    foreign = send(store, other_chat, "Άλλο")
    with pytest.raises(NotFound):
        page(store, chat, through_task=foreign["id"])
    with pytest.raises(NotFound):
        page(store, chat, before="m" * 32)


def test_a_new_conversation_and_a_waiting_message_show_on_the_latest_page(tmp_path):
    store, _, chat = setup(tmp_path)
    empty = page(store, chat)
    assert empty["messages"] == [] and empty["tasks"] == [] and empty["earlier"] is None
    task = send(store, chat, "Γεια 你好")
    first = poll(store, chat, empty)
    assert first["continues"] is True
    assert [m["task_id"] for m in first["messages"]] == [task["id"]]
    assert [t["request_id"] for t in first["tasks"]] == [task["request_id"]]


def test_page_messages_follow_the_conversation_order_when_times_tie(tmp_path):
    store, _, chat = setup(tmp_path)
    for number in range(4):
        turn(store, chat, number)
    with store.connect(write=True) as db:
        db.execute("UPDATE messages SET created_at=?", (time.time(),))
    whole = store.conversation("alice", chat["id"])
    current = page(store, chat, limit=3)
    messages = current["messages"]
    while current["earlier"]:
        current = page(store, chat, limit=3, before=current["earlier"])
        messages = current["messages"] + messages
    assert messages == whole["messages"]


def test_page_requests_say_which_way_they_read():
    with pytest.raises(ValidationError):
        ConversationPageQuery(after="a" * 32)
    with pytest.raises(ValidationError):
        ConversationPageQuery(version="1.0:", before="a" * 32)
    with pytest.raises(ValidationError):
        ConversationPageQuery(version="1.0:", through_task="a" * 32)
    with pytest.raises(ValidationError):
        ConversationPageQuery(limit=0)
    with pytest.raises(ValidationError):
        ConversationPageQuery(extra=True)
    assert ConversationPageQuery(version="1.0:").after is None


def test_a_page_reads_only_its_own_tasks_updates(tmp_path):
    store, _, chat = setup(tmp_path)
    old = turn(store, chat, 0)
    store.event("alice", old["id"], "progress_note", {"text": "Παλιό"})
    for number in range(1, 4):
        turn(store, chat, number)
    latest = page(store, chat, limit=2)
    assert old["id"] not in {t["id"] for t in latest["tasks"]}
    assert all(t["progress_notes"] == [] for t in latest["tasks"])
    whole = {t["id"]: t for t in store.conversation("alice", chat["id"])["tasks"]}
    assert whole[old["id"]]["progress_notes"] == ["Παλιό"]
    assert json.dumps(latest, ensure_ascii=False).count("Παλιό") == 0
