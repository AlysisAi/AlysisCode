from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from bots_task_state import task_state
from test_bots_runtime import ScriptedClient, response, run_claimed, setup_task

from alysis_code.bots.models import (
    BotInput,
    FileInput,
    HistoryQuery,
    MemoryInput,
    NoteInput,
    TaskInput,
)
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.reply_style import (
    FOLLOW_UPS_GUIDANCE,
    PROGRESS_NOTE_GUIDANCE,
    REPLY_STYLE,
)
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, NotFound, Store, encode
from alysis_code.llm.types import ToolCall


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "context.db")


def completed_turn(store, bot, text, answer="Recorded"):
    chat = store.create_chat("alice", bot["id"], "Source conversation")
    task = store.enqueue("alice", chat["id"], TaskInput(message=text, request_id=uuid4().hex))
    store.finish("alice", task["id"], "completed", content=answer)
    return chat, task


@pytest.mark.parametrize("memory_enabled", [True, False])
def test_approval_pins_original_constraints_once_outside_evictable_history(store, memory_enabled):
    original = (
        "Work in the background; preserve 東京.csv unchanged and return the exact Decimal total."
    )
    bot, chat, source = setup_task(store, message=original, memory_enabled=memory_enabled)
    store.claim("alice")
    proposals = ProposalStore(store, "alice")
    draft = proposals.propose(
        source,
        ActionProposal(question="Compute the total?", instructions="Calculate the amount sum."),
    )
    store.finish("alice", source["id"], "completed", content="Please review this suggestion.")
    accepted = proposals.decide(
        bot["id"], draft["id"], ActionDecision(action="accept", expected_revision=1)
    )
    task = store.claim("alice")
    assert task["id"] == accepted["task_id"]
    runtime = BotRuntime(store, "alice", lambda _: None, limits=RuntimeLimits(history_tokens=1))
    messages = runtime._context(task, mark_history=True)
    assert original in task_state(messages)
    assert sum(original in m["content"] for m in messages) == 1
    assert messages[-1]["content"] == "Calculate the amount sum."
    assert not any(m.get("_history_task_id") == source["id"] for m in messages)
    assert original in encode(runtime._fit_context(task, messages, []))
    assert not proposals.offline_computer_allowed(task)

    store.finish("alice", task["id"], "completed", content="Finished the approved work.")
    next_task = store.enqueue(
        "alice",
        chat["id"],
        TaskInput(message="Now perform a different computer task.", request_id=uuid4().hex),
    )
    context = task_state(runtime._context(next_task))
    suggestions = (
        context.split("\nFollow-up suggestions ", 1)[1].split(": ", 1)[1].split("\n", 1)[0]
    )
    previous = json.loads(suggestions)[0]
    assert previous["source_task_id"] == source["id"]
    assert previous["task_id"] == task["id"]
    assert previous["status"] == "accepted"
    assert previous["task_id"] != next_task["id"]
    assert proposals.accepted_source(next_task) is None
    assert not proposals.offline_computer_allowed(next_task)


def test_v1_migration_preserves_original_records(tmp_path):
    path = tmp_path / "v1.db"
    # Actual v1 column layouts, with pre-existing data and no v2 tables.
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE bots(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
            purpose TEXT NOT NULL,instructions TEXT NOT NULL,memory_enabled INTEGER NOT NULL,
            files_enabled INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
          INSERT INTO bots VALUES('legacy','alice','Legacy','Keep context','',1,1,1,1);
          CREATE TABLE memories(id TEXT PRIMARY KEY,bot_id TEXT NOT NULL,title TEXT NOT NULL,
            content TEXT NOT NULL,status TEXT NOT NULL,source_task_id TEXT,
            created_at REAL NOT NULL,updated_at REAL NOT NULL);
          INSERT INTO memories VALUES('old','legacy','Language','Ελληνικά','active',NULL,1,1);
          PRAGMA user_version=1;
        """)
    store = Store(path)
    assert store.bot("alice", "legacy")["name"] == "Legacy"
    assert store.list_memories("alice", "legacy")[0]["content"] == "Ελληνικά"
    note = store.save_note("alice", "legacy", NoteInput(title="Pilot", content="日本語"))
    assert Store(path).note("alice", "legacy", note["id"])["revision"] == 1
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize(
    "original,query",
    [
        ("Καλημέρα κόσμε", "ΚΑΛΗΜΈΡΑ"),
        ("Zo\u0065\u0308", "ZOË"),
        ("Straße", "STRASSE"),
        ("明日の会議", "会議"),
        ("literal %_'", "%_'"),
    ],
)
def test_history_search_is_unicode_and_literal(store, original, query):
    bot = store.save_bot("alice", BotInput(name="Context", purpose="Research"))
    _, task = completed_turn(store, bot, original)
    completed_turn(store, bot, "unrelated")
    results = store.search_history("alice", bot["id"], HistoryQuery(query=query))["messages"]
    assert len(results) == 1
    found = results[0]
    assert found["task_id"] == task["id"]
    assert found["task_status"] == "completed"
    full = store.read_message("alice", bot["id"], found["id"], 0, 2)
    assert full["content"] == original[:2]
    assert full["next_offset"] == 2
    assert store.read_message("alice", bot["id"], found["id"], 2, 16000)["content"] == original[2:]


def test_history_pagination_and_scope(store):
    bot = store.save_bot("alice", BotInput(name="Context", purpose="Research"))
    other = store.save_bot("alice", BotInput(name="Other", purpose="Private"))
    for n in range(3):
        completed_turn(store, bot, f"Source {n}")
    _, private = completed_turn(store, other, "PRIVATE")
    seen, cursor = [], None
    while True:
        page = store.search_history("alice", bot["id"], HistoryQuery(limit=2, before=cursor))
        seen.extend(m["id"] for m in page["messages"])
        cursor = page["next_before"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 6
    private_message = store.conversation("alice", private["chat_id"])["messages"][0]
    with pytest.raises(NotFound):
        store.read_message("alice", bot["id"], private_message["id"], 0, 100)
    with pytest.raises(NotFound):
        store.search_history("mallory", bot["id"], HistoryQuery())


def test_note_revision_conflict_source_and_history_paging(store):
    bot, _, task = setup_task(store)
    store.claim("alice")
    note = store.save_note(
        "alice", bot["id"], NoteInput(title="Pilot", content="Designers"), source_task_id=task["id"]
    )

    def change(content):
        try:
            return store.save_note(
                "alice",
                bot["id"],
                NoteInput(title="Pilot", content=content, expected_revision=1),
                note_id=note["id"],
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        changed = list(pool.map(change, ["Translators", "Editors"]))
    assert len([n for n in changed if n]) == 1
    for revision in range(2, 24):
        store.save_note(
            "alice",
            bot["id"],
            NoteInput(title="Pilot", content=f"Decision {revision}", expected_revision=revision),
            note_id=note["id"],
        )
    latest = store.note("alice", bot["id"], note["id"])
    assert latest["revision"] == 24
    assert [r["revision"] for r in latest["revisions"]] == list(range(24, 4, -1))
    older = store.note(
        "alice", bot["id"], note["id"], before_revision=latest["next_before_revision"]
    )
    assert [r["revision"] for r in older["revisions"]] == [4, 3, 2, 1]
    assert older["next_before_revision"] is None
    assert older["revisions"][-1]["source_task_id"] == task["id"]
    assert latest["source_task_id"] is None
    with pytest.raises(NotFound):
        store.note("mallory", bot["id"], note["id"])
    other = store.save_bot("alice", BotInput(name="Other", purpose="Separate"))
    with pytest.raises(NotFound):
        store.note("alice", other["id"], note["id"])


@pytest.mark.parametrize("target_change", ["none", "edit", "delete"])
def test_personal_memory_correction_requires_review_and_current_target(store, target_change):
    bot, _, task = setup_task(store)
    old = store.save_memory("alice", bot["id"], MemoryInput(title="Style", content="Long replies"))
    store.claim("alice")
    pending = store.save_memory(
        "alice",
        bot["id"],
        MemoryInput(title="Style", content="Short replies"),
        source_task_id=task["id"],
        replaces_memory_id=old["id"],
    )
    assert [
        m["content"] for m in store.list_memories("alice", bot["id"]) if m["status"] == "active"
    ] == ["Long replies"]
    if target_change == "edit":
        store.save_memory(
            "alice",
            bot["id"],
            MemoryInput(title="Style", content="User's newer choice"),
            memory_id=old["id"],
        )
    elif target_change == "delete":
        store.delete_item("alice", bot["id"], old["id"], kind="memories")
    if target_change != "none":
        with pytest.raises(Conflict):
            store.decide_memory("alice", bot["id"], pending["id"], "active")
        assert not any(
            m["content"] == "Short replies" and m["status"] == "active"
            for m in store.list_memories("alice", bot["id"])
        )
    else:
        store.decide_memory("alice", bot["id"], pending["id"], "active")
        assert [m["content"] for m in store.list_memories("alice", bot["id"])] == ["Short replies"]
        # A stale review/editor must not bring the superseded fact back.
        with pytest.raises(Conflict):
            store.decide_memory("alice", bot["id"], old["id"], "active")
        with pytest.raises(NotFound):
            store.save_memory(
                "alice",
                bot["id"],
                MemoryInput(title="Stale", content="Old text"),
                memory_id=old["id"],
            )


def test_model_notes_and_historical_source_events(store):
    bot, _, first = setup_task(store, message="An earlier decision")
    store.finish("alice", first["id"], "completed", content="Preserve this original")
    original = store.conversation("alice", first["chat_id"])["messages"][0]
    chat = store.create_chat("alice", bot["id"], "Continue project")
    store.enqueue(
        "alice", chat["id"], TaskInput(message="Recall then correct", request_id="continue")
    )
    client = ScriptedClient(
        [
            response(calls=[ToolCall("read", "read_message", {"message_id": original["id"]})]),
            response(
                calls=[
                    ToolCall(
                        "note",
                        "save_working_note",
                        {"title": "Pilot", "content": "Current corrected decision"},
                    )
                ]
            ),
            response("Updated"),
        ]
    )
    result = run_claimed(store, client)
    assert result["status"] == "completed"
    note = store.list_notes("alice", bot["id"])[0]
    assert note["source_task_id"] == result["id"]
    assert any(e["data"].get("source_task_id") == first["id"] for e in result["events"])
    receipt = next(m for m in reversed(client.requests[1]) if m["role"] == "tool")
    assert original["content"] in receipt["content"]


@pytest.mark.parametrize("decision", ["approve", "edit", "reject", "remove"])
def test_memory_review_context_tracks_user_changes_without_rewriting_history(store, decision):
    bot, chat, source = setup_task(store)
    store.claim("alice")
    proposal = store.save_memory(
        "alice",
        bot["id"],
        MemoryInput(title="Style", content="Unapproved preference"),
        source_task_id=source["id"],
    )
    historical_reply = "Your preference is pending review in Memory."
    store.finish("alice", source["id"], "completed", content=historical_reply)
    current = store.enqueue(
        "alice",
        chat["id"],
        TaskInput(message="What is my current preference?", request_id="recall-memory"),
    )
    runtime = BotRuntime(store, "alice", lambda _: None)
    label = "Memory review state at task start (current, not historical): "

    def review_state(context):
        return json.loads(task_state(context).split(label, 1)[1].split("\n", 1)[0])

    pending = runtime._context(current)
    assert review_state(pending) == {"pending_count": 1}
    assert "Unapproved preference" not in task_state(pending)
    if decision == "edit":
        store.save_memory(
            "alice",
            bot["id"],
            MemoryInput(title="Style", content="Corrected preference"),
            memory_id=proposal["id"],
        )
    elif decision == "remove":
        store.delete_item("alice", bot["id"], proposal["id"], kind="memories")
    else:
        store.decide_memory(
            "alice", bot["id"], proposal["id"], "active" if decision == "approve" else "rejected"
        )
    updated = BotRuntime(Store(store.path), "alice", lambda _: None)._context(current)
    assert review_state(updated) == {"pending_count": 0}
    assert historical_reply in encode(updated)
    assert store.conversation("alice", chat["id"])["messages"][1]["content"] == historical_reply
    if decision == "edit":
        assert "Corrected preference" in task_state(updated)
        assert "Unapproved preference" not in task_state(updated)
    elif decision == "approve":
        assert "Unapproved preference" in task_state(updated)
    else:
        assert "Unapproved preference" not in task_state(updated)
    disabled = {**current, "config": {**current["config"], "memory_enabled": False}}
    assert review_state(runtime._context(disabled)) is None


def _instructions_and_state(context):
    """What a request holds whatever the conversation: the instructions and the task's state."""
    return [message for message in context if message["role"] == "system"]


def test_context_trims_complete_turns_but_keeps_history_retrievable(store):
    bot, chat, first = setup_task(store, message="ARCHIVED_ORIGINAL " + "a" * 18000)
    store.finish("alice", first["id"], "completed", content="old response")
    current = store.enqueue(
        "alice", chat["id"], TaskInput(message="Continue", request_id="next-turn")
    )
    runtime = BotRuntime(store, "alice", lambda _: None)
    # Test eviction of history, not an accidental fixed system-prompt length.
    # Reserve enough for the current prompt and short recent turns, but not the
    # 18k historical request that must remain retrievable through history tools.
    ceiling = len(encode(_instructions_and_state(runtime._context(current)))) + 1500
    runtime.limits = RuntimeLimits(context_characters=ceiling)
    context = runtime._context(current)
    assert len(encode(context)) <= ceiling
    assert "ARCHIVED_ORIGINAL" not in encode(context)
    source = store.search_history("alice", bot["id"], HistoryQuery(query="ARCHIVED_ORIGINAL"))[
        "messages"
    ][0]
    assert source["task_id"] == first["id"]
    assert store.read_message("alice", bot["id"], source["id"], 0, 100)["content"].startswith(
        "ARCHIVED_ORIGINAL"
    )


def test_history_retrieval_can_make_room_without_losing_current_request_or_tool_pair(store):
    bot, chat, first = setup_task(store, message="Original docket 東京-42")
    store.finish("alice", first["id"], "completed", content="Archived")
    for index in range(12):
        old = store.enqueue(
            "alice",
            chat["id"],
            TaskInput(
                message=f"Earlier turn {index}: " + "Δ" * 1500,
                request_id=uuid4().hex,
            ),
        )
        store.finish("alice", old["id"], "completed", content="Prior receipt")
    current = store.enqueue(
        "alice",
        chat["id"],
        TaskInput(
            message="Find the original docket",
            request_id=uuid4().hex,
        ),
    )
    probe = BotRuntime(store, "alice", lambda _: None)
    protected = [
        m for m in probe._context(current, mark_history=True) if "_history_task_id" not in m
    ]
    # Budget the history room independently of evolving mandatory instructions.
    # It fits an old turn OR the retrieved result, but not both together.
    ceiling = len(encode(probe._fit_context(current, protected))) + 3300
    runtime = BotRuntime(
        store,
        "alice",
        lambda _: None,
        limits=RuntimeLimits(context_characters=ceiling, history_tokens=50000),
    )
    messages = runtime._context(current, mark_history=True)
    assert "東京-42" not in encode(messages)
    assert any("_history_task_id" in m for m in messages)
    messages.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "find",
                        "type": "function",
                        "function": {"name": "search_history", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "find",
                "content": "東京-42 " + "retrieved source " * 160,
            },
        ]
    )
    # A persisted background checkpoint must retain the same eviction boundary.
    restored = json.loads(encode(messages))
    wire = runtime._fit_context(current, restored)
    assert len(encode(wire)) <= ceiling
    assert all("_history_task_id" not in item for item in wire)
    assert any(m.get("content") == current["message"] for m in wire)
    exchange = [m for m in wire if m["role"] != "system"]
    assert exchange[-2]["tool_calls"][0]["id"] == exchange[-1]["tool_call_id"] == "find"
    assert "東京-42" in exchange[-1]["content"]
    assert len(restored) < len(messages)
    assert (
        store.read_message(
            "alice",
            bot["id"],
            store.search_history("alice", bot["id"], HistoryQuery(query="東京-42"))["messages"][0][
                "id"
            ],
            0,
            100,
        )["content"]
        == first["message"]
    )


def test_returning_task_keeps_original_request_when_later_turns_fill_context(store):
    _, chat, current = setup_task(store, message="Original constraints " + "Ω" * 1500)
    store.finish("alice", current["id"], "completed", content="Original receipt")
    protected = BotRuntime(store, "alice", lambda _: None)._context(current)
    ceiling = len(encode(protected)) + 1000
    later = store.enqueue(
        "alice", chat["id"], TaskInput(message="Later " + "Δ" * 10000, request_id=uuid4().hex)
    )
    store.finish("alice", later["id"], "completed", content="Later receipt")
    runtime = BotRuntime(
        store, "alice", lambda _: None, limits=RuntimeLimits(context_characters=ceiling)
    )
    context = runtime._context(current)
    assert len(encode(context)) <= ceiling
    assert not any(m.get("content") == later["message"] for m in context)
    assert any(m.get("content") == current["message"] for m in context)


def test_memory_index_refreshes_user_decisions_during_a_turn_and_stays_scoped(store):
    bot, _, task = setup_task(store)
    store.claim("alice")
    other = store.save_bot("alice", BotInput(name="Other", purpose="Separate"))
    store.save_memory("alice", other["id"], MemoryInput(title="Private", content="Other bot"))
    proposal = store.save_memory(
        "alice",
        bot["id"],
        MemoryInput(title="Style", content="Unapproved content"),
        source_task_id=task["id"],
    )
    runtime = BotRuntime(store, "alice", lambda _: None)
    runtime._context(task)

    def index():
        return runtime._tool(task, "list_memories", {"include_changes": True}, ["list_memories"])

    pending = index()
    assert pending["pending_count"] == 1
    assert [m["id"] for m in pending["memories"]] == [proposal["id"]]
    assert pending["memories"][0]["status"] == "pending"
    assert "Unapproved content" not in encode(pending)
    store.decide_memory("alice", bot["id"], proposal["id"], "active")
    approved = index()
    assert approved["pending_count"] == 0
    assert approved["memories"][0]["status"] == "active"
    assert approved["memories"][0]["updated_at"] >= pending["memories"][0]["updated_at"]
    store.delete_item("alice", bot["id"], proposal["id"], kind="memories")
    removed = index()
    assert removed["pending_count"] == 0 and removed["memories"] == []
    assert [e["action"] for e in removed["change_receipts"]["events"]] == [
        "deleted",
        "approved",
        "proposed",
    ]
    assert "Unapproved content" not in encode(removed)


# The fixed reply guidance is the same for every bot; these bounds are about reference bodies.
REPLY_GUIDANCE = PROGRESS_NOTE_GUIDANCE + REPLY_STYLE + FOLLOW_UPS_GUIDANCE


def test_reference_refresh_replaces_old_snapshots_without_losing_receipts(store):
    bot, _, task = setup_task(store)
    runtime = BotRuntime(store, "alice", lambda _: None)
    messages = runtime._context(task, mark_history=True)
    receipt = {"role": "tool", "tool_call_id": "saved", "content": "Actual earlier saved result"}
    messages.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "saved",
                        "type": "function",
                        "function": {
                            "name": "save_file",
                            "arguments": '{"name":"kept.txt","content":"original"}',
                        },
                    }
                ],
            },
            receipt,
        ]
    )
    original_messages = json.loads(encode(messages))
    original_count = len(messages)
    state = None
    note = None
    for revision in range(20):
        note = store.save_note(
            "alice",
            bot["id"],
            NoteInput(
                title="Current project",
                content=f"Decision {revision}: " + "λ" * 1800,
                expected_revision=note["revision"] if note else 0,
            ),
            note_id=note["id"] if note else None,
        )
        state, changed = runtime._refresh_memory_context(task, messages, state)
        assert changed
        assert len(messages) == original_count + 1
        assert receipt in messages
        assert f"Decision {revision}:" in messages[-1]["content"]
        if revision:
            assert f"Decision {revision - 1}:" not in encode(messages)
        # Internal metadata survives durable background checkpoint serialization.
        messages = json.loads(encode(messages))
    wire = runtime._fit_context(task, messages)
    assert all("_bot_reference_state" not in m for m in wire)
    # Twenty replacements must produce the same request as one fresh refresh
    # of the final state, independent of unrelated fixed prompt wording.
    runtime._refresh_memory_context(task, original_messages, None)
    assert wire == runtime._fit_context(task, original_messages)
    assert store.note("alice", bot["id"], note["id"])["revision"] == 20


@pytest.mark.parametrize("change", ["edit", "remove", "approve_replacement"])
@pytest.mark.parametrize("stale_action", [False, True])
def test_memory_change_during_provider_call_discards_stale_answer_or_action(
    store, change, stale_action
):
    bot, chat, task = setup_task(store)
    memory = store.save_memory(
        "alice", bot["id"], MemoryInput(title="Format", content="Old preference: five bullets")
    )

    class ChangingClient(ScriptedClient):
        def chat(self, **kwargs):
            if not self.requests:
                if change == "edit":
                    store.save_memory(
                        "alice",
                        bot["id"],
                        MemoryInput(title="Format", content="Current: three bullets"),
                        memory_id=memory["id"],
                    )
                elif change == "remove":
                    store.delete_item("alice", bot["id"], memory["id"], kind="memories")
                else:
                    replacement = store.save_memory(
                        "alice",
                        bot["id"],
                        MemoryInput(title="Format", content="Current: three bullets"),
                        source_task_id=task["id"],
                        replaces_memory_id=memory["id"],
                    )
                    store.decide_memory("alice", bot["id"], replacement["id"], "active")
            return super().chat(**kwargs)

    stale = response(
        "Old preference is still five bullets",
        [ToolCall("stale-write", "save_file", {"name": "stale.txt", "content": "Old preference"})]
        if stale_action
        else [],
    )
    client = ChangingClient([stale, response("Current state checked")])
    result = run_claimed(store, client)
    assert result["status"] == "completed" and result["calls"] == 2
    assert store.list_files("alice", bot["id"]) == []
    assert (
        store.conversation("alice", chat["id"])["messages"][-1]["content"]
        == "Current state checked"
    )
    refreshed = next(
        m["content"]
        for m in reversed(client.requests[1])
        if m["role"] == "system"
        and m["content"].startswith("Saved memory and working notes were refreshed.")
    )
    assert "Old preference: five bullets" not in refreshed
    if change == "remove":
        assert '"approved_memories":[]' in refreshed
    else:
        assert "Current: three bullets" in refreshed
    assert '"pending_count":0' in refreshed


@pytest.mark.parametrize("state", ["failed", "cancelled", "interrupted"])
def test_incomplete_turn_keeps_original_constraints_and_actual_status(store, state):
    bot, chat, first = setup_task(
        store, message="Project Δάφνη / 日本語: offline only. Make a draft.", memory_enabled=False
    )
    store.claim("alice")
    file = store.save_file(
        "alice",
        bot["id"],
        FileInput(name="partial.md", content="Unfinished draft"),
        source_task_id=first["id"],
    )
    if state == "interrupted":
        assert store.recover("alice") == 1
    else:
        store.finish("alice", first["id"], state, error="Stopped before verification")
    current = store.enqueue(
        "alice",
        chat["id"],
        TaskInput(message="What happened? Don't restart anything.", request_id="explain-request"),
    )
    # Reopening the actual durable store must retain failed-turn context even
    # when personal memory and cross-conversation retrieval are switched off.
    reopened = Store(store.path)
    before = reopened.conversation("alice", chat["id"])
    context = BotRuntime(reopened, "alice", lambda _: None)._context(current)
    assert context[1]["role"] == "user"
    record = json.loads(context[1]["content"].partition("\n")[2])
    assert record["request"] == first["message"]
    assert record["status"] == state
    assert record["task_id"] == first["id"]
    assert file["id"] in task_state(context)
    assert f'"source_task_id":"{first["id"]}"' in task_state(context)
    assert context[-1]["content"] == current["message"]
    assert not any(m["role"] == "assistant" for m in context)
    assert reopened.conversation("alice", chat["id"])["messages"] == before["messages"]
    assert reopened.task("alice", first["id"])["status"] == state
    assert reopened.usage("alice")["calls"] == 0
    # Actual Anthropic projection lifts system messages to the front. The
    # request and outcome must still travel together with their original data.
    from alysis_code.llm.anthropic_messages import _anthropic_messages_from_messages

    system, wire = _anthropic_messages_from_messages(context)
    assert first["id"] not in encode(system).split("Recent Library page")[0]
    assert wire[0]["role"] == "user"
    projected = wire[0]["content"][0]["text"]
    assert json.loads(projected.partition("\n")[2]) == record


def test_context_evicts_incomplete_request_and_receipt_together(store):
    _, chat, first = setup_task(store, message="OLD_REQUEST " + "x" * 18000)
    store.finish("alice", first["id"], "failed", error="OLD_RECEIPT")
    second = store.enqueue(
        "alice", chat["id"], TaskInput(message="New constraint", request_id="second-request")
    )
    store.finish("alice", second["id"], "completed", content="Acknowledged")
    current = store.enqueue(
        "alice", chat["id"], TaskInput(message="Current", request_id="third-request")
    )
    runtime = BotRuntime(store, "alice", lambda _: None)
    # Test eviction of history, not an accidental fixed system-prompt length.
    # Reserve enough for the current prompt and short recent turns, but not the
    # 18k historical request that must remain retrievable through history tools.
    ceiling = len(encode(_instructions_and_state(runtime._context(current)))) + 1500
    runtime.limits = RuntimeLimits(context_characters=ceiling)
    context = runtime._context(current)
    assert len(encode(context)) <= ceiling
    assert "OLD_REQUEST" not in encode(context) and "OLD_RECEIPT" not in encode(context)
    assert [m["role"] for m in context] == ["system", "user", "assistant", "system", "user"]
    assert context[1]["content"] == "New constraint"
    assert context[3]["content"] == task_state(context)  # just before the task's own request


def test_reference_bodies_are_bounded_and_disabled_memory_blocks_tools(store):
    bot, _, task = setup_task(store)
    for n in range(12):
        store.save_note(
            "alice",
            bot["id"],
            NoteInput(title=f"Project {n}", content=f"Private {n}:" + "x" * 3900),
        )
    runtime = BotRuntime(store, "alice", lambda _: None)
    messages = runtime._context(task)
    context = messages[0]["content"] + task_state(messages)
    assert len(context) < 23000 + len(REPLY_GUIDANCE)
    assert "content_available_via_tool" in context
    assert all(note["id"] in context for note in store.list_notes("alice", bot["id"]))
    off, _, _ = setup_task(store, memory_enabled=False)
    store.save_note("alice", off["id"], NoteInput(title="Hidden", content="SHOULD_NOT_APPEAR"))
    # Finish the first bot's queued task, then claim the disabled bot's task.
    store.finish("alice", task["id"], "completed", content="done")
    client = ScriptedClient(
        [
            response(
                calls=[
                    ToolCall("search", "search_history", {"query": "Private"}),
                    ToolCall("note", "save_working_note", {"title": "No", "content": "No"}),
                    ToolCall("memories", "list_memories", {}),
                ]
            ),
            response("Memory is disabled"),
        ]
    )
    result = run_claimed(store, client)
    assert "SHOULD_NOT_APPEAR" not in encode(client.requests)
    assert len([e for e in result["events"] if e["kind"] == "tool_failed"]) == 3
    assert len(store.list_notes("alice", off["id"])) == 1


def test_note_tool_reads_current_revision_and_rejects_other_bots_or_stopped_tasks(store):
    bot, _, task = setup_task(store)
    store.claim("alice")
    note = store.save_note(
        "alice", bot["id"], NoteInput(title="Decision", content="Old"), source_task_id=task["id"]
    )
    store.save_note(
        "alice",
        bot["id"],
        NoteInput(title="Decision", content="Current", expected_revision=1),
        note_id=note["id"],
    )
    runtime = BotRuntime(store, "alice", lambda _: None)
    result = runtime._tool(
        task, "read_working_note", {"note_id": note["id"]}, ["read_working_note"]
    )
    assert result["content"] == "Current"
    assert "revisions" not in result
    other = store.save_bot("alice", BotInput(name="Other", purpose="Isolated"))
    with pytest.raises(Conflict):
        store.save_note(
            "alice",
            other["id"],
            NoteInput(title="Wrong bot", content="No"),
            source_task_id=task["id"],
        )
    store.cancel("alice", task["id"])
    with pytest.raises(Conflict):
        store.save_note(
            "alice", bot["id"], NoteInput(title="Stopped", content="No"), source_task_id=task["id"]
        )
    assert len(store.list_notes("alice", bot["id"])) == 1
