"""Real store receipts; no model-quality claims from these tests."""

import os
from uuid import uuid4

import pytest
from bots_task_state import task_state

from alysis_code.bots.models import BotInput, MemoryInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.store import Conflict, NotFound, Store, encode


@pytest.fixture(params=["sqlite", "postgres"])
def case(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set disposable PostgreSQL 18 DSN for shared receipt checks")
        store = PostgresStore(
            dsn,
            objects=LocalObjectStorage(tmp_path / "objects"),
            state_directory=tmp_path / "state",
        )
    else:
        store = Store(tmp_path / "memory.db")
    owner = uuid4().hex
    bot = store.save_bot(owner, BotInput(name="Research", purpose="Memory lifecycle"))
    chat = store.create_chat(owner, bot["id"], "Continuous")
    task = store.enqueue(
        owner, chat["id"], TaskInput(message="Remember a preference", request_id=uuid4().hex)
    )
    store.claim(owner)
    try:
        yield store, owner, bot, task
    finally:
        if store.shared:
            store.close()


def test_lifecycle_receipts_survive_deletion_without_preference_content(case):
    store, owner, bot, task = case

    def propose(target=None):
        return store.save_memory(
            owner,
            bot["id"],
            MemoryInput(title="秘密-title", content="SECRET-body"),
            source_task_id=task["id"],
            replaces_memory_id=target,
        )

    original = propose()
    store.decide_memory(owner, bot["id"], original["id"], "active")
    # An idempotent approval cannot invent an additional transition.
    store.decide_memory(owner, bot["id"], original["id"], "active")
    store.save_memory(
        owner, bot["id"], MemoryInput(title="Edited", content="NEW-body"), memory_id=original["id"]
    )
    rejected = propose(original["id"])
    store.decide_memory(owner, bot["id"], rejected["id"], "rejected")
    accepted = propose(original["id"])
    store.decide_memory(owner, bot["id"], accepted["id"], "active")
    store.delete_item(owner, bot["id"], accepted["id"], kind="memories")
    receipts = store.memory_changes(owner, bot["id"])
    events = list(reversed(receipts["events"]))
    assert [e["action"] for e in events] == [
        "proposed",
        "approved",
        "edited",
        "proposed",
        "rejected",
        "proposed",
        "replaced",
        "approved",
        "deleted",
    ]
    assert events[0]["source_task_id"] == task["id"]
    source = store.read_message(owner, bot["id"], events[0]["source_message_id"], 0, 12000)
    assert source["content"] == "Remember a preference"
    assert events[6]["related_memory_id"] == accepted["id"]
    assert events[-1]["previous_status"] == "active"
    assert events[-1]["status"] == "deleted"
    assert store.list_memories(owner, bot["id"]) == []
    assert "SECRET" not in encode(receipts) and "秘密" not in encode(receipts)
    assert "NEW-body" not in encode(receipts)
    assert receipts["next_before_seq"] is None
    # Reopening the SQLite store does not lose the approval/delete evidence.
    if not store.shared:
        assert Store(store.path).memory_changes(owner, bot["id"]) == receipts


def test_failed_or_foreign_changes_do_not_publish_receipts(case):
    store, owner, bot, task = case
    target = store.save_memory(owner, bot["id"], MemoryInput(title="Style", content="Old"))
    proposal = store.save_memory(
        owner,
        bot["id"],
        MemoryInput(title="Style", content="Proposed"),
        source_task_id=task["id"],
        replaces_memory_id=target["id"],
    )
    store.save_memory(
        owner, bot["id"], MemoryInput(title="Style", content="Edited"), memory_id=target["id"]
    )
    before = store.memory_changes(owner, bot["id"])
    with pytest.raises(Conflict):
        store.decide_memory(owner, bot["id"], proposal["id"], "active")
    with pytest.raises(NotFound):
        store.delete_item(owner, bot["id"], "unknown", kind="memories")
    with pytest.raises(NotFound):
        store.memory_changes("other-owner", bot["id"])
    with pytest.raises(NotFound):
        store.decide_memory("other-owner", bot["id"], proposal["id"], "active")
    assert store.memory_changes(owner, bot["id"]) == before
    other = store.save_bot(owner, BotInput(name="Other", purpose="Isolated"))
    assert store.memory_changes(owner, other["id"])["events"] == []


def test_receipts_page_without_loss_and_current_index_stays_current(case):
    store, owner, bot, task = case
    memory = store.save_memory(owner, bot["id"], MemoryInput(title="Style", content="Initial"))
    for n in range(55):
        store.save_memory(
            owner, bot["id"], MemoryInput(title="Style", content=str(n)), memory_id=memory["id"]
        )
    runtime = BotRuntime(store, owner, lambda _: None)
    first = runtime._tool(task, "list_memories", {"include_changes": True}, ["list_memories"])
    cursor = first["change_receipts"]["next_before_seq"]
    second = runtime._tool(task, "list_memories", {"before_event_seq": cursor}, ["list_memories"])
    assert first["memories"] == second["memories"]
    assert first["pending_count"] == second["pending_count"] == 0
    events = first["change_receipts"]["events"] + second["change_receipts"]["events"]
    assert len(events) == len({e["seq"] for e in events}) == 56
    assert events[-1]["action"] == "created"
    assert second["change_receipts"]["next_before_seq"] is None


def test_sqlite_upgrade_does_not_invent_historical_approval(tmp_path):
    store = Store(tmp_path / "old.db")
    bot = store.save_bot("owner", BotInput(name="Existing", purpose="Preserve data"))
    memory = store.save_memory("owner", bot["id"], MemoryInput(title="Style", content="Current"))
    # Reproduce the version-20 schema on this disposable test database only.
    with store.connect(write=True) as db:
        db.execute("DROP TABLE memory_changes")
        db.execute("PRAGMA user_version=20")
    reopened = Store(store.path)
    assert reopened.list_memories("owner", bot["id"])[0]["id"] == memory["id"]
    receipts = reopened.memory_changes("owner", bot["id"])
    assert receipts["events"] == []
    assert "unknown" in receipts["coverage"]
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_change_timing_is_relative_to_the_last_published_answer(case):
    store, owner, bot, task = case
    memory = store.save_memory(owner, bot["id"], MemoryInput(title="Style", content="Before"))
    store.finish(owner, task["id"], "completed", content="The saved preference is Before.")
    store.save_memory(
        owner, bot["id"], MemoryInput(title="Style", content="After"), memory_id=memory["id"]
    )
    current = store.enqueue(
        owner, task["chat_id"], TaskInput(message="What is saved now?", request_id=uuid4().hex)
    )
    timeline = store.memory_changes_since_reply(owner, current["id"])
    assert timeline["previous_reply"]["task_id"] == task["id"]
    assert [e["action"] for e in timeline["later_changes"]] == ["edited"]
    assert timeline["later_changes"][0]["created_at"] > timeline["previous_reply"]["created_at"]
    assert "Before" not in encode(timeline) and "After" not in encode(timeline)
    with pytest.raises(NotFound):
        store.memory_changes_since_reply("other-owner", current["id"])
    runtime = BotRuntime(store, owner, lambda _: None)
    assert "Changes since the previous published reply" in task_state(runtime._context(current))
    disabled = {**current, "config": {**current["config"], "memory_enabled": False}}
    assert memory["id"] not in encode(runtime._context(disabled))


def _saved(
    store, owner, bot, task, *, title="Ώρα συνάντησης", content="Πρωινά, πριν τις 10", target=None
):
    return store.save_memory(
        owner,
        bot["id"],
        MemoryInput(title=title, content=content),
        source_task_id=task["id"],
        replaces_memory_id=target,
        saved_by_owl=True,
    )


def _listed(store, owner, bot):
    return {m["id"]: (m["status"], m["content"]) for m in store.list_memories(owner, bot["id"])}


def test_what_an_owl_saves_is_kept_at_once_and_undo_forgets_it(case):
    store, owner, bot, task = case
    memory = _saved(store, owner, bot, task)
    assert memory["status"] == "active"
    own = store.save_memory(owner, bot["id"], MemoryInput(title="Mine", content="Typed by me"))
    undo = {m["id"]: m["can_undo"] for m in store.list_memories(owner, bot["id"], for_person=True)}
    assert undo == {memory["id"]: True, own["id"]: False}
    assert "can_undo" not in store.list_memories(owner, bot["id"])[0]  # the owl never sees it
    store.delete_item(owner, bot["id"], own["id"], kind="memories")
    assert _listed(store, owner, bot) == {memory["id"]: ("active", "Πρωινά, πριν τις 10")}
    assert store.undo_memory(owner, bot["id"], memory["id"]) == {
        "id": memory["id"],
        "restored_memory_id": None,
    }
    assert _listed(store, owner, bot) == {}
    # Newest first; the receipts hold no preference text.
    events = store.memory_changes(owner, bot["id"])["events"]
    assert [e["action"] for e in events if e["memory_id"] == memory["id"]] == ["undone", "saved"]
    assert "Πρωινά" not in encode(events)
    with pytest.raises(Conflict):
        store.undo_memory(owner, bot["id"], memory["id"])


def test_an_owls_correction_replaces_at_once_and_undo_brings_the_old_one_back(case):
    store, owner, bot, task = case
    old = store.save_memory(owner, bot["id"], MemoryInput(title="City", content="Lives in Lisbon"))
    new = _saved(store, owner, bot, task, title="City", content="Lives in Porto", target=old["id"])
    # The old one is no longer listed or used, but is kept for Undo.
    assert _listed(store, owner, bot) == {new["id"]: ("active", "Lives in Porto")}
    with pytest.raises(Conflict):
        store.decide_memory(owner, bot["id"], old["id"], "active")
    result = store.undo_memory(owner, bot["id"], new["id"])
    assert result == {"id": new["id"], "restored_memory_id": old["id"]}
    assert _listed(store, owner, bot) == {old["id"]: ("active", "Lives in Lisbon")}


@pytest.mark.parametrize("change", ["delete", "reject", "edit"])
def test_when_a_replacement_goes_or_becomes_the_persons_the_old_words_are_erased(case, change):
    store, owner, bot, task = case
    old = store.save_memory(owner, bot["id"], MemoryInput(title="City", content="Lives in Lisbon"))
    new = _saved(store, owner, bot, task, title="City", content="Lives in Porto", target=old["id"])
    if change == "delete":
        store.delete_item(owner, bot["id"], new["id"], kind="memories")
    elif change == "reject":
        store.decide_memory(owner, bot["id"], new["id"], "rejected")
    else:
        store.save_memory(
            owner,
            bot["id"],
            MemoryInput(title="City", content="Lives in Porto, Foz"),
            memory_id=new["id"],
        )
        # The person's edit makes it theirs: nothing is left to undo.
        with pytest.raises(Conflict):
            store.undo_memory(owner, bot["id"], new["id"])
    with store.connect() as db:
        row = db.execute(
            "SELECT status,title,content FROM memories WHERE id=?", (old["id"],)
        ).fetchone()
    assert (row["status"], row["title"], row["content"]) == ("rejected", "", "")


def test_only_what_an_owl_saved_by_itself_can_be_undone(case):
    store, owner, bot, task = case
    own = store.save_memory(owner, bot["id"], MemoryInput(title="Mine", content="Typed by me"))
    suggestion = store.save_memory(
        owner, bot["id"], MemoryInput(title="Idea", content="Suggested"), source_task_id=task["id"]
    )
    for memory in (own, suggestion):
        with pytest.raises(Conflict):
            store.undo_memory(owner, bot["id"], memory["id"])
    with pytest.raises(NotFound):
        store.undo_memory(owner, bot["id"], "missing")
    assert suggestion["status"] == "pending"
