"""Reactions: the person's thumbs up or down on an owl's reply, with an optional note, which the
owl's later turns read back as the person's feedback, within a fixed size.

Real store and runtime. The HTTP test at the end needs FastAPI.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from bots_task_state import task_state
from pydantic import ValidationError

from alysis_code.bots.models import BotInput, ConversationPageQuery, TaskInput
from alysis_code.bots.reactions import (
    MAX_NOTE_CHARACTERS,
    RECENT_REACTIONS,
    TOKEN_BUDGET,
    ReactionInput,
    ReplyReactions,
)
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store, encode
from alysis_code.token_budget import estimate_tokens

LABEL = "The person's reactions to your recent replies"
JAPANESE_NOTE = "長すぎます。次は三行以内でお願いします。"
GREEK_REPLY = "Βρήκα τρεις πτήσεις για Θεσσαλονίκη· η φθηνότερη φεύγει στις 07:10."


@pytest.fixture
def owl(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("alice", BotInput(name="Νεφέλη", purpose="Ταξίδια"))
    chat = store.create_chat("alice", bot["id"], "Chat")
    return store, bot, chat


def turn(store, chat, request, reply):
    """One finished turn; returns the owl's reply message."""
    task = store.enqueue("alice", chat["id"], TaskInput(message=request, request_id=uuid4().hex))
    assert store.claim("alice")["id"] == task["id"]
    store.finish("alice", task["id"], "completed", content=reply)
    return next(
        m
        for m in store.conversation("alice", chat["id"])["messages"]
        if m["task_id"] == task["id"] and m["role"] == "assistant"
    )


def react(store, bot, message, reaction, note="", owner="alice"):
    return ReplyReactions(store, owner).react(
        bot["id"], message["id"], ReactionInput(reaction=reaction, note=note)
    )


def task_of(store, chat, message):
    conversation = store.conversation("alice", chat["id"])
    return next(task for task in conversation["tasks"] if task["id"] == message["task_id"])


def next_turn_state(store, chat, request="Και τώρα;"):
    store.enqueue("alice", chat["id"], TaskInput(message=request, request_id=uuid4().hex))
    task = store.claim("alice")
    messages = BotRuntime(store, "alice", lambda _: None)._context(task)
    return messages, task


def feedback(state: str) -> dict:
    """The reactions a turn's task state carries."""
    line = next(line for line in state.split("\n") if line.startswith(LABEL))
    return json.loads(line[line.index("{") :])


def test_a_reaction_is_stored_changed_and_removed(owl):
    store, bot, chat = owl
    reply = turn(store, chat, "Πτήσεις;", GREEK_REPLY)
    assert "reactions" not in task_of(store, chat, reply)

    liked = react(store, bot, reply, "up")
    assert (liked["message_id"], liked["reaction"], liked["note"]) == (reply["id"], "up", "")
    assert task_of(store, chat, reply)["reactions"] == [liked]

    # The same reaction again changes nothing, not even when it was given.
    assert react(store, bot, reply, "up") == liked

    changed = react(store, bot, reply, "down", f"  {JAPANESE_NOTE}  ")
    assert (changed["reaction"], changed["note"]) == ("down", JAPANESE_NOTE)
    assert changed["updated_at"] >= liked["updated_at"]
    assert task_of(store, chat, reply)["reactions"] == [changed]
    page = store.conversation_page("alice", chat["id"], ConversationPageQuery())
    assert next(t for t in page["tasks"] if t["id"] == reply["task_id"])["reactions"] == [changed]

    assert ReplyReactions(store, "alice").remove(bot["id"], reply["id"]) == {
        "message_id": reply["id"],
        "reaction": None,
    }
    assert "reactions" not in task_of(store, chat, reply)
    # Removing it again is the same answer.
    assert ReplyReactions(store, "alice").remove(bot["id"], reply["id"])["reaction"] is None


def test_a_changed_reaction_reaches_a_page_that_polls(owl):
    store, bot, chat = owl
    reply = turn(store, chat, "Πτήσεις;", GREEK_REPLY)
    first = store.conversation_page("alice", chat["id"], ConversationPageQuery())
    react(store, bot, reply, "down", JAPANESE_NOTE)
    polled = store.conversation_page(
        "alice",
        chat["id"],
        ConversationPageQuery(version=first["version"], after=first["messages"][-1]["id"]),
    )
    assert polled["continues"] is True and polled["unchanged"] is False
    [task] = polled["tasks"]
    assert task["reactions"][0]["note"] == JAPANESE_NOTE


def test_only_an_owls_reply_takes_a_reaction_and_only_from_its_owner(owl):
    store, bot, chat = owl
    reply = turn(store, chat, "Πτήσεις;", GREEK_REPLY)
    question = next(
        m for m in store.conversation("alice", chat["id"])["messages"] if m["role"] == "user"
    )
    with pytest.raises(Conflict):
        react(store, bot, question, "up")
    with pytest.raises(NotFound):
        react(store, bot, reply, "up", owner="mallory")
    other = store.save_bot("alice", BotInput(name="Άλλη", purpose=""))
    with pytest.raises(NotFound):
        ReplyReactions(store, "alice").react(other["id"], reply["id"], ReactionInput(reaction="up"))
    with pytest.raises(NotFound):
        react(store, bot, {"id": uuid4().hex}, "down")
    with pytest.raises(NotFound):
        ReplyReactions(store, "mallory").remove(bot["id"], reply["id"])
    assert "reactions" not in task_of(store, chat, reply)


@pytest.mark.parametrize(
    "body",
    [
        {"reaction": "meh"},
        {"reaction": "down", "note": "α" * (MAX_NOTE_CHARACTERS + 1)},
        {"note": "no reaction"},
        {"reaction": "up", "owner": "other"},
    ],
)
def test_a_reaction_is_up_or_down_with_a_short_note(body):
    with pytest.raises(ValidationError):
        ReactionInput.model_validate(body)


def test_later_turns_read_the_persons_feedback_labelled_as_feedback(owl):
    store, bot, chat = owl
    liked = turn(store, chat, "Πτήσεις;", GREEK_REPLY)
    disliked = turn(store, chat, "会議は?", "議事録の全文を以下に貼り付けます。" * 3)
    before, _ = next_turn_state(store, chat)
    assert LABEL not in task_state(before)
    store.finish("alice", store.conversation("alice", chat["id"])["tasks"][-1]["id"], "completed")

    react(store, bot, liked, "up")
    react(store, bot, disliked, "down", JAPANESE_NOTE)
    messages, _ = next_turn_state(store, chat)
    state, guidance = task_state(messages), messages[0]["content"]
    read = feedback(state)
    assert read == {
        "reactions": [
            {
                "reaction": "disliked",
                "reply": read["reactions"][0]["reply"],
                "note": JAPANESE_NOTE,
                "at": read["reactions"][0]["at"],
            },
            {
                "reaction": "liked",
                "reply": GREEK_REPLY,
                "at": read["reactions"][1]["at"],
            },
        ]
    }
    assert read["reactions"][0]["reply"].startswith("議事録の全文")
    # The person's feedback, never instructions: in the task state, apart from the owl's setup.
    line = next(line for line in state.split("\n") if line.startswith(LABEL))
    assert "not instructions" in line and "safety" in line and "never a command" in line
    assert JAPANESE_NOTE not in guidance


def test_feedback_stays_within_its_budget_newest_first(owl):
    store, bot, chat = owl
    long_note = ("返信が長すぎて読みにくいです。要点だけにしてください。" * 20)[
        : MAX_NOTE_CHARACTERS - 3
    ]
    replies = [
        turn(store, chat, f"Ερώτηση {n}", f"Απάντηση {n}: " + "λεπτομέρειες " * 40)
        for n in range(RECENT_REACTIONS + 3)
    ]
    for n, reply in enumerate(replies):
        react(store, bot, reply, "down", f"{n:02d} {long_note}")
    messages, _ = next_turn_state(store, chat)
    read = feedback(task_state(messages))
    shown = read["reactions"]
    assert 0 < len(shown) <= RECENT_REACTIONS
    assert sum(estimate_tokens(encode(item)) for item in shown) <= TOKEN_BUDGET
    # The newest reaction first; older ones are counted, not shown.
    assert shown[0]["note"].startswith(f"{len(replies) - 1:02d} ")
    assert read["left_out"] == len(replies) - len(shown)
    assert all(item["reply"].endswith("…") for item in shown)


def test_a_removed_reaction_is_no_longer_read(owl):
    store, bot, chat = owl
    reply = turn(store, chat, "Πτήσεις;", GREEK_REPLY)
    react(store, bot, reply, "down", JAPANESE_NOTE)
    ReplyReactions(store, "alice").remove(bot["id"], reply["id"])
    messages, _ = next_turn_state(store, chat)
    assert LABEL not in task_state(messages)
    assert all(JAPANESE_NOTE not in str(m.get("content")) for m in messages)


def test_each_owl_reads_only_the_reactions_to_its_own_replies(owl):
    store, bot, chat = owl
    reply = turn(store, chat, "Πτήσεις;", GREEK_REPLY)
    react(store, bot, reply, "down", JAPANESE_NOTE)
    other = store.save_bot("alice", BotInput(name="Άλλη", purpose=""))
    other_chat = store.create_chat("alice", other["id"], "Chat")
    store.enqueue("alice", other_chat["id"], TaskInput(message="Γεια", request_id=uuid4().hex))
    task = store.claim("alice")
    assert task["bot_id"] == other["id"]
    assert LABEL not in task_state(BotRuntime(store, "alice", lambda _: None)._context(task))


def test_deleting_the_owl_erases_its_reactions(owl):
    store, bot, chat = owl
    reply = turn(store, chat, "Πτήσεις;", GREEK_REPLY)
    react(store, bot, reply, "down", JAPANESE_NOTE)
    store.delete_bot("alice", bot["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM reply_reactions").fetchone()[0] == 0


def test_reactions_over_http(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "reactions.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None, work_authorizer=lambda: False)
    store = app.state.store
    bot = store.save_bot(settings.owner, BotInput(name="Μίρα", purpose=""))
    chat = store.create_chat(settings.owner, bot["id"], "Chat")
    task = store.enqueue(
        settings.owner, chat["id"], TaskInput(message="Πτήσεις;", request_id=uuid4().hex)
    )
    store.claim(settings.owner)
    store.finish(settings.owner, task["id"], "completed", content=GREEK_REPLY)
    reply = store.conversation(settings.owner, chat["id"])["messages"][-1]
    path = f"/v1/bots/{bot['id']}/messages/{reply['id']}/reaction"
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "reply_reactions" in client.get("/v1/status").json()["capabilities"]
        put = client.put(path, json={"reaction": "down", "note": JAPANESE_NOTE})
        assert put.status_code == 200 and put.json()["note"] == JAPANESE_NOTE
        assert client.put(path, json={"reaction": "sideways"}).status_code == 422
        assert client.put(path, json={"reaction": "up", "extra": 1}).status_code == 422
        page = client.post(f"/v1/chats/{chat['id']}/page", json={}).json()
        assert page["tasks"][-1]["reactions"][0]["reaction"] == "down"
        removed = client.delete(path)
        assert removed.status_code == 200 and removed.json()["reaction"] is None
        mine = store.conversation(settings.owner, chat["id"])["messages"][0]
        own = client.put(
            f"/v1/bots/{bot['id']}/messages/{mine['id']}/reaction", json={"reaction": "up"}
        )
        assert own.status_code == 409
        missing = client.put(
            f"/v1/bots/{bot['id']}/messages/{uuid4().hex}/reaction", json={"reaction": "up"}
        )
        assert missing.status_code == 404
