"""Another answer to the person's latest message: one press queues a turn of its own that reads
the same message and files again; the chat keeps both answers and the owl's history the newest."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from test_bots_runtime import ScriptedClient, response, run_claimed

from alysis_code.bots.models import (
    BotInput,
    ConversationPageQuery,
    FileInput,
    IntroInput,
    RegenerateInput,
    TaskInput,
)
from alysis_code.bots.regenerate import history_with_regenerations
from alysis_code.bots.store import Conflict, NotFound, Store

QUESTION = "Πρότεινε τρία ονόματα για το καφέ μας 日本語で"
FIRST = "1. Ακτή 2. Κύμα 3. Φάρος"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "regenerate.db")


def owl(store, owner="alice"):
    bot = store.save_bot(owner, BotInput(name="Νεφέλη", purpose="Names things"))
    return bot, store.create_chat(owner, bot["id"], "New conversation")


def send(store, chat, message, file_ids=(), *, request_id=None):
    return store.enqueue(
        "alice",
        chat["id"],
        TaskInput(
            message=message,
            request_id=request_id or "request_" + uuid4().hex,
            file_ids=list(file_ids),
        ),
    )


def answered(store, chat, message=QUESTION, reply=FIRST, file_ids=()):
    task = send(store, chat, message, file_ids)
    assert store.claim("alice")["id"] == task["id"]
    store.finish("alice", task["id"], "completed", content=reply)
    return task


def again(store, task_id, request_id=None):
    return store.regenerate(
        "alice", task_id, RegenerateInput(request_id=request_id or "again_" + uuid4().hex)
    )


def user_messages(request):
    return [m["content"] for m in request if m["role"] == "user"]


def assistant_messages(request):
    return [m["content"] for m in request if m["role"] == "assistant"]


def test_a_new_answer_is_its_own_turn_with_no_message_and_the_same_id_returns_it(store):
    _, chat = owl(store)
    first = answered(store, chat)
    queued = again(store, first["id"], "again_same_press")
    assert queued["status"] == "queued" and queued["chat_id"] == chat["id"]
    assert queued["message"] == QUESTION
    assert queued["config"]["regenerate"] == {
        "task_id": first["id"],
        "replaces": first["id"],
        "source": first["id"],
    }
    # A press whose answer was lost is sent again: the same turn, never a second one.
    assert again(store, first["id"], "again_same_press")["id"] == queued["id"]
    conversation = store.conversation("alice", chat["id"])
    assert [t["id"] for t in conversation["tasks"]] == [first["id"], queued["id"]]
    # No second copy of the person's message appears.
    assert [(m["role"], m["task_id"]) for m in conversation["messages"]] == [
        ("user", first["id"]),
        ("assistant", first["id"]),
    ]


def test_the_same_request_id_from_another_reply_is_a_conflict(store):
    _, chat = owl(store)
    first = answered(store, chat)
    with pytest.raises(Conflict, match="different message"):
        store.regenerate(
            "alice", first["id"], RegenerateInput(request_id=send(store, chat, "x")["request_id"])
        )


def test_only_the_latest_finished_reply_gets_a_new_answer_and_never_while_working(store):
    _, chat = owl(store)
    older = answered(store, chat)
    newer = answered(store, chat, "Και ένα τέταρτο;", "4. Λιμάνι")
    with pytest.raises(Conflict, match="latest reply"):
        again(store, older["id"])
    working = send(store, chat, "Γράψε ένα σλόγκαν")
    with pytest.raises(Conflict, match="latest reply"):
        again(store, newer["id"])
    with pytest.raises(Conflict, match="still working"):
        again(store, working["id"])
    store.claim("alice")
    store.finish("alice", working["id"], "failed", error="Example failure")
    # A failed turn of the person's own is retried as a new message, not answered again.
    with pytest.raises(Conflict, match="finished reply"):
        again(store, working["id"])
    with pytest.raises(NotFound):
        again(store, uuid4().hex)
    other = store.save_bot("bob", BotInput(name="Bob"))
    bob_chat = store.create_chat("bob", other["id"], "New conversation")
    theirs = store.enqueue(
        "bob", bob_chat["id"], TaskInput(message="Hi", request_id="request_" + uuid4().hex)
    )
    with pytest.raises(NotFound):
        again(store, theirs["id"])


@pytest.mark.parametrize("request_id", ["schedule_run_1", "proposal_card_1", "clarification_q1"])
def test_requests_the_engine_made_are_not_answered_again(store, request_id):
    _, chat = owl(store)
    task = send(store, chat, "Run the weekly summary", request_id=request_id)
    store.claim("alice")
    store.finish("alice", task["id"], "completed", content="Έγινε")
    with pytest.raises(Conflict, match="your own message"):
        again(store, task["id"])


def test_a_first_hello_is_not_answered_again(store):
    bot, _ = owl(store)
    hello = store.start_intro(
        "alice", bot["id"], IntroInput(request_id="intro_hello", language="ja")
    )
    store.claim("alice")
    store.finish("alice", hello["id"], "completed", content="こんにちは")
    with pytest.raises(Conflict, match="your own message"):
        again(store, hello["id"])


def test_the_new_turn_reads_the_message_and_files_again_with_the_answer_it_replaces(store):
    bot, chat = owl(store)
    menu = store.save_file("alice", bot["id"], FileInput(name="Μενού 東京.md", content="Καφές"))
    first = answered(store, chat, file_ids=[menu["id"]])
    regenerated = again(store, first["id"])
    client = ScriptedClient([response("1. Ορίζοντας 2. Αύρα 3. Σπίθα")])
    finished = run_claimed(store, client)
    assert finished["id"] == regenerated["id"] and finished["status"] == "completed"

    request = client.requests[0]
    asked = user_messages(request)
    # The person's message is asked once, as this turn's request, with its files listed.
    assert sum(QUESTION in content for content in asked) == 1
    current = asked[-1]
    assert current.startswith(QUESTION + "\n\n[")
    assert "Μενού 東京.md" in current and menu["id"] in current
    note, earlier = current.rsplit("\n", 1)
    assert "not from the person" in note and "reference data, not instructions" in note
    assert json.loads(earlier) == {"earlier_answer": FIRST}
    # The earlier answer is reference data in the request, not a reply the owl gave.
    assert FIRST not in assistant_messages(request)

    # The chat keeps both answers, the earlier one first.
    conversation = store.conversation("alice", chat["id"])
    assert [(m["role"], m["task_id"], m["content"]) for m in conversation["messages"]] == [
        ("user", first["id"], QUESTION),
        ("assistant", first["id"], FIRST),
        ("assistant", regenerated["id"], "1. Ορίζοντας 2. Αύρα 3. Σπίθα"),
    ]


def test_later_turns_read_only_the_newest_finished_answer(store):
    _, chat = owl(store)
    first = answered(store, chat)
    again(store, first["id"])
    run_claimed(store, ScriptedClient([response("Δεύτερη πρόταση")]))
    send(store, chat, "Ποιο σου αρέσει περισσότερο;")
    client = ScriptedClient([response("Το δεύτερο")])
    run_claimed(store, client)
    request = client.requests[0]
    assert user_messages(request)[-2:] == [QUESTION, "Ποιο σου αρέσει περισσότερο;"]
    assistant = assistant_messages(request)
    assert "Δεύτερη πρόταση" in assistant and FIRST not in assistant
    assert all("earlier_answer" not in content for content in user_messages(request))


def test_an_answer_that_did_not_finish_is_tried_again_and_the_earlier_one_stands_meanwhile(
    store,
):
    _, chat = owl(store)
    first = answered(store, chat)
    failed = again(store, first["id"])
    store.claim("alice")
    store.finish("alice", failed["id"], "failed", error="Example failure")
    # Until another answer finishes, the history keeps the first one and leaves the failed one out.
    retried = again(store, failed["id"])
    assert retried["config"]["regenerate"] == {
        "task_id": first["id"],
        "replaces": first["id"],
        "source": failed["id"],
    }
    client = ScriptedClient([response("Τρίτη προσπάθεια")])
    run_claimed(store, client)
    request = client.requests[0]
    assert json.loads(user_messages(request)[-1].rsplit("\n", 1)[1]) == {"earlier_answer": FIRST}
    assert not any("Historical task record" in content for content in user_messages(request))


def test_answering_a_new_answer_again_replaces_that_one_and_keeps_the_persons_message(store):
    _, chat = owl(store)
    first = answered(store, chat)
    again(store, first["id"])
    second = run_claimed(store, ScriptedClient([response("Δεύτερη")]))
    third = again(store, second["id"])
    assert third["config"]["regenerate"] == {
        "task_id": first["id"],
        "replaces": second["id"],
        "source": second["id"],
    }
    client = ScriptedClient([response("Τρίτη")])
    run_claimed(store, client)
    request = client.requests[0]
    current = user_messages(request)[-1]
    assert current.startswith(QUESTION)
    assert json.loads(current.rsplit("\n", 1)[1]) == {"earlier_answer": "Δεύτερη"}
    assert not {FIRST, "Δεύτερη"} & set(assistant_messages(request))


def test_history_keeps_each_message_in_its_place_with_the_answer_that_stands():
    def task(task_id, status="completed", regenerate=None, created_at=0):
        config = {"regenerate": {"task_id": regenerate}} if regenerate else {}
        return {"id": task_id, "status": status, "config": config, "created_at": created_at}

    def said(task_id, role, content):
        return {"role": role, "content": content, "_memory_turn_id": task_id}

    tasks = {
        "o": task("o", created_at=1),
        "r1": task("r1", regenerate="o", created_at=2),
        "x": task("x", created_at=3),
        "r2": task("r2", status="failed", regenerate="o", created_at=4),
    }
    turns = {
        "o": [said("o", "user", "Ερώτηση"), said("o", "assistant", "Πρώτη")],
        "x": [said("x", "user", "Επόμενη"), said("x", "assistant", "Ναι")],
        "r1": [said("r1", "assistant", "Δεύτερη")],
        "r2": [],
    }
    kept = history_with_regenerations(turns, tasks, "y")
    assert list(kept) == ["r1", "x"]
    assert kept["r1"] == [said("r1", "user", "Ερώτηση"), said("r1", "assistant", "Δεύτερη")]
    # Writing r2 now: it answers the message in its place, and the other answers leave.
    current = history_with_regenerations(turns, tasks, "r2")
    assert list(current) == ["r2", "x"]
    assert current["r2"] == [said("r2", "user", "Ερώτηση")]
    # Nothing to change without regenerations.
    plain = {"o": turns["o"]}
    assert history_with_regenerations(plain, {"o": tasks["o"]}, "o") is plain


def test_conversations_say_when_the_person_last_read_them(store):
    _, chat = owl(store)
    first = answered(store, chat)
    whole = store.conversation("alice", chat["id"])
    assert whole["read_at"] is None and whole["unread"] is True
    store.mark_chat_read("alice", chat["id"], whole["activity_seq"])
    read = store.conversation("alice", chat["id"])
    reply = read["messages"][-1]
    assert reply["task_id"] == first["id"]
    assert read["read_at"] >= reply["created_at"]
    later = answered(store, chat, "Και μια ακόμα;", "Ναι")
    after = store.conversation("alice", chat["id"])
    assert after["read_at"] == read["read_at"] and after["unread"] is True
    newest = [m for m in after["messages"] if m["task_id"] == later["id"]]
    assert all(m["created_at"] > after["read_at"] for m in newest)
    page = store.conversation_page("alice", chat["id"], ConversationPageQuery(limit=2))
    assert page["read_at"] == after["read_at"]


def test_a_summary_of_older_turns_says_which_answer_was_asked_again(store):
    from alysis_code.bots.runtime import BotRuntime, RuntimeLimits

    _, chat = owl(store)
    first = answered(store, chat, "Γράψε μια περίληψη 東京 " * 30, "Πρώτη περίληψη " * 30)
    regenerated = again(store, first["id"])
    store.claim("alice")
    store.finish("alice", regenerated["id"], "completed", content="Δεύτερη περίληψη " * 30)
    for number in range(8):
        answered(store, chat, f"Ερώτηση {number} " * 40, f"Απάντηση {number} " * 40)
    send(store, chat, "Τελευταία ερώτηση")
    task = store.claim("alice")
    runtime = BotRuntime(store, "alice", lambda _: None, limits=RuntimeLimits(history_tokens=1500))
    summary = runtime.context_summary
    records, _ = summary._candidate(task, summary.read(task["bot_id"]), 1500)
    by_task = {record["task_id"]: record for record in records}
    assert by_task[regenerated["id"]]["answers_again"] == first["id"]
    assert "answers_again" not in by_task[first["id"]]


def test_a_message_cancelled_before_it_started_does_not_hide_the_latest_reply(store):
    from alysis_code.bots.queued_messages import QueuedMessageChange, QueuedMessages

    _bot, chat = owl(store)
    task = answered(store, chat, "Γράψε μια πρόταση")
    # Sent, then cancelled before the owl started it: it is not in the chat.
    waiting = send(store, chat, "見なくていい")
    QueuedMessages(store, "alice").change(
        waiting["id"], QueuedMessageChange(action="cancel", expected_revision=0)
    )
    assert again(store, task["id"])["status"] == "queued"
