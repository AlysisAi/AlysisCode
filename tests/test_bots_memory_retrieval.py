"""Reference retrieval bounds and source semantics; no model-quality claims."""

import pytest
from test_bots_runtime import setup_task

from alysis_code.bots.context_summary import current_memory_state
from alysis_code.bots.memory_retrieval import change_snapshot, page, ranked, selected, serialized
from alysis_code.bots.models import BotInput, MemoryInput, NoteInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store


def records(count):
    return [
        dict(
            id=str(i),
            title=f"Project {i}",
            content=f"Project reference {i}",
            updated_at=i,
            status="active",
        )
        for i in range(count)
    ]


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_selection_bounds_indexes_and_bodies_without_mutating_records(count):
    saved = records(count)
    saved[0]["content"] = "Keep the original CEDAR-7301 decision."
    original = serialized(saved)
    result = selected(saved, query="CEDAR-7301", budget=2000)
    assert len(serialized(result["items"])) <= 2000
    assert result["items"][0]["id"] == "0"
    assert result["total_count"] == count
    assert result["omitted_count"] == count - len(result["items"])
    assert result["index_complete"] is False
    assert serialized(saved) == original


@pytest.mark.parametrize(
    "text,query",
    [
        ("Καλημέρα κόσμε", "ΚΑΛΗΜΈΡΑ"),
        ("Zo\u0065\u0308", "ZOË"),
        ("Straße", "STRASSE"),
        ("明日の会議", "会議"),
        ("東京", "東"),
        ("مراجعة المشروع", "المشروع"),
        ("नया प्रस्ताव", "प्रस्ताव"),
        ("literal %_'", "%_'"),
    ],
)
def test_lexical_retrieval_handles_unicode_and_literal_symbols(text, query):
    saved = records(100)
    saved[0]["content"] = text
    assert ranked(saved, query)[0]["id"] == "0"


def test_pages_cover_collection_and_reject_stale_generation():
    saved = records(79)
    seen, offset, generation = [], 0, None
    while True:
        result = page(saved, offset=offset, limit=7, generation=generation)
        seen.extend(r["id"] for r in result["items"])
        generation = result["generation"]
        offset = result["next_offset"]
        if offset is None:
            break
    assert len(set(seen)) == len(seen) == 79
    saved[0]["content"] = "Correction"
    with pytest.raises(ValueError, match="changed"):
        page(saved, generation=generation)


def test_partial_empty_selection_is_not_empty_memory_and_pending_is_not_approved():
    saved = records(100)
    saved[0].update(status="pending", content="UNAPPROVED")
    state = current_memory_state(saved, content_budget=0)
    assert state["approved_memories"] == []
    assert state["approved_count"] == 99
    assert state["pending_count"] == 1
    assert state["index_complete"] is False
    assert "UNAPPROVED" not in serialized(state)
    empty = current_memory_state([])
    assert empty["approved_count"] == 0 and empty["index_complete"]


def test_live_store_retrieval_checks_bot_owner_current_state_and_page_generation(tmp_path):
    store = Store(tmp_path / "references.db")
    bot, _, task = setup_task(store)
    store.claim("alice")
    other = store.save_bot("alice", BotInput(name="Other", purpose="Private"))
    store.save_memory("alice", other["id"], MemoryInput(title="Secret", content="CROSS_BOT"))
    for n in range(25):
        store.save_memory(
            "alice", bot["id"], MemoryInput(title=f"Reference {n}", content=f"Value {n}")
        )
    pending = store.save_memory(
        "alice",
        bot["id"],
        MemoryInput(title="Pending", content="NOT_APPROVED"),
        source_task_id=task["id"],
    )
    note = store.save_note("alice", bot["id"], NoteInput(title="東京", content="会議は明日"))
    runtime = BotRuntime(store, "alice", lambda _: None)
    result = runtime._tool(task, "list_memories", {}, ["list_memories"])
    assert result["total_count"] == 26 and result["approved_count"] == 25
    assert result["pending_count"] == 1 and result["next_offset"] == 20
    assert "NOT_APPROVED" not in serialized(result) and "CROSS_BOT" not in serialized(result)
    assert (
        runtime._tool(task, "list_memories", {"query": "NOT_APPROVED"}, ["list_memories"])[
            "matched_count"
        ]
        == 0
    )
    with pytest.raises(NotFound):
        runtime._tool(task, "read_memory", {"memory_id": pending["id"]}, ["read_memory"])
    store.decide_memory("alice", bot["id"], pending["id"], "active")
    with pytest.raises(Conflict, match="changed"):
        runtime._tool(
            task,
            "list_memories",
            {"offset": 20, "generation": result["generation"]},
            ["list_memories"],
        )
    found = runtime._tool(task, "search_working_notes", {"query": "会議"}, ["search_working_notes"])
    assert found["items"][0]["id"] == note["id"]
    assert "content" not in found["items"][0]
    with pytest.raises(NotFound):
        BotRuntime(store, "mallory", lambda _: None)._tool(
            task, "search_working_notes", {}, ["search_working_notes"]
        )


def test_store_retains_more_than_prototype_count_without_automatic_deletion(tmp_path):
    store = Store(tmp_path / "many.db")
    bot, _, task = setup_task(store)
    for n in range(120):
        store.save_memory(
            "alice", bot["id"], MemoryInput(title=f"Preference {n}", content=f"Original {n}")
        )
        store.save_note(
            "alice", bot["id"], NoteInput(title=f"Project {n}", content=f"Reference {n}")
        )
    restored = Store(store.path)
    assert len(restored.list_memories("alice", bot["id"])) == 120
    assert len(restored.list_notes("alice", bot["id"])) == 120
    context = serialized(BotRuntime(restored, "alice", lambda _: None)._context(task))
    assert '"approved_count"' in context.replace('\\"', '"')
    assert '"index_complete":false' in context.replace('\\"', '"')


def test_audit_projection_keeps_originals_and_a_real_cursor():
    receipt = {
        "events": [
            {"seq": n, "action": "edited", "memory_id": str(n), "created_at": n}
            for n in range(100, 0, -1)
        ],
        "next_before_seq": None,
    }
    snapshot = change_snapshot(receipt, budget=400)
    assert len(serialized(snapshot)) < 500
    assert snapshot["selection_partial"] is True
    assert snapshot["next_before_seq"] == snapshot["events"][-1]["seq"]
    assert len(receipt["events"]) == 100


def test_retrieved_preference_membership_survives_selection_and_deletion():
    from alysis_code.bots.memory_retrieval import read_memory_ids

    saved = records(120)
    saved[0].update(title="Writing preference", content="Use Greek for all replies.")
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "read_memory", "arguments": '{"memory_id":"0"}'}}],
        }
    ]
    identities = read_memory_ids(messages)
    state = current_memory_state(
        saved, query="Unrelated topic", content_budget=500, referenced_ids=identities
    )
    assert state["approved_memories"][0]["id"] == "0"
    assert state["referenced_memory_status"] == [{"id": "0", "status": "active"}]
    saved[0]["content"] = "Now use French."
    corrected = current_memory_state(
        saved, query="Unrelated topic", content_budget=500, referenced_ids=identities
    )
    assert corrected["approved_memories"][0]["content"] == "Now use French."
    removed = current_memory_state(
        saved[1:], query="Unrelated topic", content_budget=500, referenced_ids=identities
    )
    assert removed["referenced_memory_status"] == [{"id": "0", "status": "not_current"}]
    assert "Now use French" not in serialized(removed)
