"""Atomic, reversible project-note consolidation on both portable stores."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from bots_task_state import task_state
from test_bots_memory_changes import case as shared_case

from alysis_code.bots.context_summary import BotContextSummary
from alysis_code.bots.models import BotInput, NoteConsolidation, NoteInput, NoteSource, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, encode

case = shared_case


def seed(case):
    store, owner, bot, task = case
    notes = [
        store.save_note(
            owner,
            bot["id"],
            NoteInput(title=f"東京 {n}", content=f"Original {n}"),
            source_task_id=task["id"],
        )
        for n in range(3)
    ]
    data = NoteConsolidation(
        title="Current",
        content="Current decisions; unresolved questions.",
        reason="Overlapping project context",
        sources=[NoteSource(note_id=n["id"], expected_revision=n["revision"]) for n in notes[:2]],
    )
    return notes, data


def test_originals_revisions_and_links_survive_consolidation_restore(case):
    store, owner, bot, task = case
    notes, data = seed(case)
    result = store.consolidate_notes(owner, bot["id"], data, source_task_id=task["id"])
    assert len(store.list_notes(owner, bot["id"])) == 2
    assert len(store.list_notes(owner, bot["id"], include_archived=True)) == 4
    saved = store.note(owner, bot["id"], notes[0]["id"])
    assert saved["content"] == "Original 0" and saved["revision"] == 1
    assert saved["consolidated_into"] == result["id"]
    assert saved["revisions"][0]["content"] == "Original 0"
    target = store.note(owner, bot["id"], result["id"])
    assert len(target["consolidated_sources"]) == 2
    assert target["consolidated_sources"][0]["source_revision"] == 1
    with pytest.raises(Conflict, match="archived"):
        store.save_note(
            owner,
            bot["id"],
            NoteInput(title="No", content="Cannot silently replace", expected_revision=1),
            note_id=saved["id"],
        )
    restored = store.restore_note(owner, bot["id"], saved["id"], 1)
    assert restored["updated_at"] == saved["updated_at"]
    assert restored["revision"] == saved["revision"]
    assert len(store.list_notes(owner, bot["id"])) == 3
    assert store.note(owner, bot["id"], saved["id"])["consolidated_into"] is None
    assert store.note(owner, bot["id"], result["id"])["consolidated_sources"][0]["restored_at"]
    store.save_note(
        owner,
        bot["id"],
        NoteInput(title="Restored", content="Explicit correction", expected_revision=1),
        note_id=saved["id"],
    )
    assert store.note(owner, bot["id"], saved["id"])["revisions"][-1]["content"] == "Original 0"


def test_stale_cross_bot_cancelled_and_duplicate_sources_roll_back(case):
    store, owner, bot, task = case
    notes, data = seed(case)
    other = store.save_bot(owner, BotInput(name="Private", purpose="Separate"))
    for owner_id, bot_id in [("mallory", bot["id"]), (owner, other["id"])]:
        with pytest.raises(NotFound):
            store.consolidate_notes(owner_id, bot_id, data)
    duplicate = data.model_copy(update={"sources": [data.sources[0], data.sources[0]]})
    with pytest.raises(Conflict, match="distinct"):
        store.consolidate_notes(owner, bot["id"], duplicate)
    store.save_note(
        owner,
        bot["id"],
        NoteInput(title="Changed", content="New", expected_revision=1),
        note_id=notes[0]["id"],
    )
    with pytest.raises(Conflict, match="changed"):
        store.consolidate_notes(owner, bot["id"], data)
    assert len(store.list_notes(owner, bot["id"])) == 3
    store.finish(owner, task["id"], "cancelled")
    with pytest.raises(Conflict, match="cannot"):
        store.consolidate_notes(owner, bot["id"], data, source_task_id=task["id"])


def test_concurrent_consolidations_have_one_winner(case):
    store, owner, bot, task = case
    _, data = seed(case)

    def run():
        try:
            return store.consolidate_notes(owner, bot["id"], data)["id"]
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(store.list_notes(owner, bot["id"], include_archived=True)) == 4


def test_note_edits_and_archives_invalidate_summary_and_refresh_background_context(case):
    store, owner, bot, task = case
    notes, data = seed(case)
    summaries = BotContextSummary(store, owner)
    initial = summaries.read(bot["id"])["memory_version"]
    runtime = BotRuntime(store, owner, lambda _: None)
    messages = []
    fingerprint, _ = runtime._refresh_memory_context(task, messages, None)
    store.save_note(
        owner,
        bot["id"],
        NoteInput(title="New", content="CORRECTED", expected_revision=1),
        note_id=notes[2]["id"],
    )
    assert summaries.read(bot["id"])["memory_version"] != initial
    updated, changed = runtime._refresh_memory_context(task, messages, fingerprint)
    assert changed and "CORRECTED" in messages[-1]["content"]
    result = store.consolidate_notes(owner, bot["id"], data)
    _, changed = runtime._refresh_memory_context(task, messages, updated)
    assert changed and result["id"] in messages[-1]["content"]
    assert "Original 0" not in messages[-1]["content"]
    context = encode(runtime._context(task))
    assert "Original 0" not in context
    found = runtime._tool(
        task,
        "search_working_notes",
        {"query": "Original", "include_archived": True},
        ["search_working_notes"],
    )
    assert found["total_count"] == 4
    source = runtime._tool(
        task, "read_working_note", {"note_id": notes[0]["id"]}, ["read_working_note"]
    )
    assert source["consolidated_into"] == result["id"]


def test_note_revision_chronology_distinguishes_later_corrections_and_stays_bounded(case):
    store, owner, bot, task = case
    notes, _ = seed(case)
    assert store.note_changes_since_reply(owner, task["id"])["previous_reply"] is None
    store.finish(owner, task["id"], "completed", content="Reported the original notes.")
    for n in range(22):
        store.save_note(
            owner,
            bot["id"],
            NoteInput(title="Edited", content=f"Private content {n}", expected_revision=n + 1),
            note_id=notes[0]["id"],
        )
    other = store.save_bot(owner, BotInput(name="Other", purpose="Separate"))
    store.save_note(owner, other["id"], NoteInput(title="Other note", content="Never exposed"))
    current = store.enqueue(
        owner, task["chat_id"], TaskInput(message="What changed?", request_id=uuid4().hex)
    )
    receipt = store.note_changes_since_reply(owner, current["id"])
    assert receipt["previous_reply"]["task_id"] == task["id"]
    assert receipt["selection_partial"] and len(receipt["later_revisions"]) == 20
    assert {r["note_id"] for r in receipt["later_revisions"]} == {notes[0]["id"]}
    assert [r["revision"] for r in receipt["later_revisions"]] == list(range(23, 3, -1))
    assert all(
        r["created_at"] > receipt["previous_reply"]["created_at"]
        for r in receipt["later_revisions"]
    )
    assert "Private" not in encode(receipt) and "Never" not in encode(receipt)
    with pytest.raises(NotFound):
        store.note_changes_since_reply("another-account", current["id"])
    context = task_state(BotRuntime(store, owner, lambda _: None)._context(current))
    assert '"later_revisions"' in context
