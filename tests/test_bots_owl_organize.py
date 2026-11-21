"""Pinning, archiving and duplicating owls: places in the person's list, kept with each owl."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from alysis_code.bots.models import (
    BotCreateInput,
    BotDuplicateInput,
    BotInput,
    BotOrganizeInput,
    FileInput,
    MemoryInput,
    RegenerateInput,
    TaskInput,
)
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.bots.triggers import TriggerDecision, TriggerInput, TriggerStore


def routine(**overrides):
    return ScheduleInput.model_validate(
        {
            "name": "Σύνοψη",
            "prompt": "Summarise the new project notes.",
            "timing": {"kind": "interval", "every_minutes": 5},
            **overrides,
        }
    )


def organize(store, bot, *, now=None, **parts):
    return store.organize_bot("alice", bot["id"], BotOrganizeInput(**parts), now=now)


def send(store, bot, text="Γεια"):
    chat = store.create_chat("alice", bot["id"], bot["name"])
    return store.enqueue(
        "alice", chat["id"], TaskInput(message=text, request_id="req_" + uuid4().hex)
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bots.sqlite3")


def test_pinning_keeps_its_first_time_and_unpinning_clears_it(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη", purpose="Έρευνα"))
    assert (owl["pinned_at"], owl["archived_at"]) == (None, None)
    pinned = organize(store, owl, pinned=True, now=100)
    assert pinned["pinned_at"] == 100
    # Pinning again keeps the order the pinned owls already have.
    assert organize(store, owl, pinned=True, now=200)["pinned_at"] == 100
    # A place in the list is not the owl's setup: anything approved against it stays current.
    assert pinned["updated_at"] == owl["updated_at"]
    listed = {bot["id"]: bot for bot in store.list_bots("alice")}
    assert listed[owl["id"]]["pinned_at"] == 100
    assert organize(store, owl, pinned=False)["pinned_at"] is None


def test_a_change_that_leaves_the_list_place_out_keeps_it(store):
    owl = store.save_bot("alice", BotInput(name="花子", purpose="調べもの"))
    organize(store, owl, pinned=True, archived=True, now=50)
    renamed = store.save_bot("alice", BotInput(name="花子さん", purpose="調べもの"), owl["id"])
    edited = store.save_bot("alice", BotInput(name="花子さん", purpose="新しい"), owl["id"])
    assert (renamed["pinned_at"], renamed["archived_at"]) == (50, 50)
    assert (edited["pinned_at"], edited["archived_at"]) == (50, 50)
    # Leaving one part out of a change keeps that part too.
    assert organize(store, owl, archived=False)["pinned_at"] == 50


def test_an_organize_change_names_something(store):
    with pytest.raises(ValidationError):
        BotOrganizeInput()
    with pytest.raises(ValidationError):
        BotOrganizeInput(pinned="yes")
    with pytest.raises(ValidationError):
        BotOrganizeInput(pinned=True, folder="x")


def test_only_the_owner_organizes_or_duplicates(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη"))
    with pytest.raises(NotFound):
        store.organize_bot("bob", owl["id"], BotOrganizeInput(pinned=True))
    with pytest.raises(NotFound):
        store.duplicate_bot(
            "bob",
            owl["id"],
            BotDuplicateInput(name="Νεφέλη (αντίγραφο)", request_id="dup_" + "a" * 8),
        )
    assert store.bot("alice", owl["id"])["pinned_at"] is None


def test_an_owl_at_work_is_not_archived(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη"))
    task = send(store, owl)
    with pytest.raises(Conflict):
        organize(store, owl, archived=True)
    assert store.bot("alice", owl["id"])["archived_at"] is None
    store.claim("alice")
    store.finish("alice", task["id"], "completed", content="Έτοιμο")
    assert organize(store, owl, archived=True, now=10)["archived_at"] == 10


def test_routines_rest_while_archived_and_go_on_from_their_next_time(store):
    owl = store.save_bot("alice", BotInput(name="Έλενα"))
    schedule = store.save_schedule("alice", owl["id"], routine(), now=1000)
    paused = store.save_schedule("alice", owl["id"], routine(name="Άλλη"), now=1000)
    store.decide_schedule("alice", owl["id"], paused["id"], "paused", paused["revision"], now=1000)
    organize(store, owl, archived=True, now=1100)
    # Due at 1300 and every five minutes after: nothing runs while it is archived.
    for now in (1300, 1301, 5000, 40000):
        assert store.enqueue_due_schedules("alice", max_calls=20, now=now) == 0
    assert store.schedule_runs("alice", owl["id"], schedule["id"])["runs"] == []
    back = organize(store, owl, archived=False, now=40010)
    assert back["archived_at"] is None
    listed = {item["id"]: item for item in store.list_schedules("alice", owl["id"])}
    # What it missed is not replayed: it goes on from its next time, and a paused one stays paused.
    assert listed[schedule["id"]]["next_run_at"] == 40300
    assert listed[schedule["id"]]["state"] == "active"
    assert listed[paused["id"]]["state"] == "paused"
    assert store.enqueue_due_schedules("alice", max_calls=20, now=40010) == 0
    assert store.enqueue_due_schedules("alice", max_calls=20, now=40300) == 1
    runs = store.schedule_runs("alice", owl["id"], schedule["id"])["runs"]
    assert [(run["scheduled_for"], run["current_status"]) for run in runs] == [(40300, "queued")]


def test_a_retry_due_while_archived_is_not_replayed(store):
    owl = store.save_bot("alice", BotInput(name="Έλενα"))
    schedule = store.save_schedule(
        "alice", owl["id"], routine(timing={"kind": "interval", "every_minutes": 60}), now=1000
    )
    with store.connect(write=True) as db:
        Store._save_schedule_health(
            db,
            schedule["id"],
            schedule["revision"],
            1000,
            failures=1,
            retry_at=1200,
            retry_for=1000,
            attempts=1,
            retry_reason="temporary",
        )
    organize(store, owl, archived=True, now=1100)
    assert store.enqueue_due_schedules("alice", max_calls=20, now=1250) == 0
    organize(store, owl, archived=False, now=1300)
    assert store.enqueue_due_schedules("alice", max_calls=20, now=1301) == 0
    with store.connect() as db:
        health = db.execute(
            "SELECT failures,retry_at FROM schedule_health WHERE schedule_id=?", (schedule["id"],)
        ).fetchone()
    assert (health["failures"], health["retry_at"]) == (1, None)


def test_file_events_rest_while_archived_and_old_ones_never_replay(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη", purpose="Διάβασε αρχεία"))
    triggers = TriggerStore(store, "alice")
    rule = triggers.create(
        owl["id"], TriggerInput(name="Νέα αρχεία", instructions="Summarise each new file.")
    )
    triggers.decide(
        owl["id"], rule["id"], TriggerDecision(state="active", expected_revision=rule["revision"])
    )
    # A file added just before archiving is still being collected: archiving drops it.
    store.save_file("alice", owl["id"], FileInput(name="πριν.txt", content="πριν"))
    organize(store, owl, archived=True)
    store.save_file("alice", owl["id"], FileInput(name="ノート.txt", content="メモ"))
    assert triggers.dispatch(max_calls=20, now=time.time() + 120) == 0
    organize(store, owl, archived=False)
    assert triggers.dispatch(max_calls=20, now=time.time() + 120) == 0
    deliveries = triggers.list(owl["id"])[0]["recent_deliveries"]
    assert [delivery["state"] for delivery in deliveries] == ["skipped"]
    # Back in the list, a new file starts it again.
    store.save_file("alice", owl["id"], FileInput(name="μετά.txt", content="μετά"))
    assert triggers.dispatch(max_calls=20, now=time.time() + 120) == 1


def test_needs_you_leaves_archived_owls_out(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη"))
    other = store.save_bot("alice", BotInput(name="花子"))
    for bot in (owl, other):
        task = send(store, bot, "Κάνε κάτι")
        store.claim("alice")
        store.finish("alice", task["id"], "failed", error="Model unavailable")
    assert {item["bot_name"] for item in store.needs_you("alice")["items"]} == {"Νεφέλη", "花子"}
    organize(store, owl, archived=True)
    listed = store.needs_you("alice")
    assert [item["bot_name"] for item in listed["items"]] == ["花子"]
    assert listed["count"] == 1
    organize(store, owl, archived=False)
    assert store.needs_you("alice")["count"] == 2


def test_a_message_brings_an_archived_owl_back(store):
    owl = store.save_bot("alice", BotInput(name="Έλενα"))
    schedule = store.save_schedule("alice", owl["id"], routine(), now=time.time() - 3600)
    organize(store, owl, archived=True)
    task = send(store, owl, "Είσαι εκεί;")
    assert store.bot("alice", owl["id"])["archived_at"] is None
    assert task["status"] == "queued"
    # Its routine goes on from its next time, not from what it missed.
    after = store.list_schedules("alice", owl["id"])[0]["next_run_at"]
    assert after > time.time() - 1
    assert store.schedule_runs("alice", owl["id"], schedule["id"])["runs"] == []


def test_asking_for_another_answer_brings_an_archived_owl_back(store):
    owl = store.save_bot("alice", BotInput(name="Νίκος"))
    send(store, owl, "Γράψε ένα χαϊκού")
    task = store.claim("alice")
    store.finish("alice", task["id"], "completed", content="一つ目")
    organize(store, owl, archived=True)
    again = store.regenerate("alice", task["id"], RegenerateInput(request_id="req_" + uuid4().hex))
    assert again["status"] == "queued"
    assert store.bot("alice", owl["id"])["archived_at"] is None


def test_duplicate_copies_the_setup_but_not_the_owls_history(store):
    owl = store.save_bot(
        "alice",
        BotCreateInput(
            name="Νεφέλη",
            purpose="Έρευνα αγοράς",
            instructions="Γράφε σύντομα.",
            memory_enabled=False,
            files_enabled=True,
            color="purple",
            background="stars",
        ),
    )
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO bot_plugin_grants VALUES(?,?,?,?,?)", (owl["id"], "github", 0, 3, 1.0)
        )
        db.execute("INSERT INTO bot_review_settings VALUES(?,?)", (owl["id"], "review"))
    store.save_memory("alice", owl["id"], MemoryInput(title="Γλώσσα", content="Ελληνικά"))
    store.save_file("alice", owl["id"], FileInput(name="σημειώσεις.txt", content="x"))
    store.save_schedule("alice", owl["id"], routine(), now=1000)
    send(store, owl)
    organize(store, owl, pinned=True)
    copy = store.duplicate_bot(
        "alice",
        owl["id"],
        BotDuplicateInput(name="Νεφέλη (αντίγραφο)", request_id="dup_" + uuid4().hex),
    )
    assert copy["id"] != owl["id"]
    assert {
        key: copy[key]
        for key in (
            "name",
            "purpose",
            "instructions",
            "memory_enabled",
            "files_enabled",
            "color",
            "background",
            "pinned_at",
            "archived_at",
        )
    } == {
        "name": "Νεφέλη (αντίγραφο)",
        "purpose": "Έρευνα αγοράς",
        "instructions": "Γράφε σύντομα.",
        "memory_enabled": False,
        "files_enabled": True,
        "color": "purple",
        "background": "stars",
        "pinned_at": None,
        "archived_at": None,
    }
    with store.connect() as db:
        grants = db.execute(
            "SELECT plugin_id,enabled FROM bot_plugin_grants WHERE bot_id=?", (copy["id"],)
        ).fetchall()
        mode = db.execute(
            "SELECT mode FROM bot_review_settings WHERE bot_id=?", (copy["id"],)
        ).fetchone()
    assert [tuple(row) for row in grants] == [("github", 0)]
    assert mode["mode"] == "review"
    # Memories are off by default; chats, files and routines never come along.
    assert store.list_memories("alice", copy["id"]) == []
    assert store.list_chats("alice", copy["id"]) == []
    assert store.list_files("alice", copy["id"]) == []
    assert store.list_schedules("alice", copy["id"]) == []


def test_duplicate_can_bring_the_approved_memories(store):
    owl = store.save_bot("alice", BotInput(name="花子", purpose="調べもの"))
    first = store.save_memory(
        "alice", owl["id"], MemoryInput(title="言語", content="日本語で答える")
    )
    store.save_memory("alice", owl["id"], MemoryInput(title="形式", content="箇条書き"))
    task = send(store, owl)
    store.claim("alice")
    # A suggestion still waiting for review stays with the original.
    store.save_memory(
        "alice",
        owl["id"],
        MemoryInput(title="提案", content="未確認"),
        source_task_id=task["id"],
    )
    copy = store.duplicate_bot(
        "alice",
        owl["id"],
        BotDuplicateInput(name="花子（コピー）", request_id="dup_" + uuid4().hex, memories=True),
    )
    copied = store.list_memories("alice", copy["id"])
    assert [(m["title"], m["content"], m["status"]) for m in copied] == [
        ("言語", "日本語で答える", "active"),
        ("形式", "箇条書き", "active"),
    ]
    assert all(m["source_task_id"] is None for m in copied)
    assert first["id"] not in {m["id"] for m in copied}
    assert len(store.list_memories("alice", owl["id"])) == 3


def test_a_retried_duplicate_makes_one_owl(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη", purpose="Έρευνα"))
    store.save_memory("alice", owl["id"], MemoryInput(title="Τόνος", content="Φιλικός"))
    request = BotDuplicateInput(
        name="Νεφέλη (αντίγραφο)", request_id="dup_" + uuid4().hex, memories=True
    )
    first = store.duplicate_bot("alice", owl["id"], request)
    again = store.duplicate_bot("alice", owl["id"], request)
    assert again["id"] == first["id"]
    assert len(store.list_bots("alice")) == 2
    assert len(store.list_memories("alice", first["id"])) == 1
    # The same id for another copy is refused rather than answered with the wrong owl.
    with pytest.raises(Conflict):
        store.duplicate_bot(
            "alice",
            owl["id"],
            BotDuplicateInput(name="Άλλο όνομα", request_id=request.request_id),
        )


def test_archived_owls_count_toward_the_owl_limit(store):
    owls = [store.save_bot("alice", BotInput(name=f"Κουκουβάγια {n}")) for n in range(30)]
    for owl in owls[:10]:
        organize(store, owl, archived=True)
    with pytest.raises(LimitReached):
        store.save_bot("alice", BotInput(name="Μία ακόμη"))
    with pytest.raises(LimitReached):
        store.duplicate_bot(
            "alice",
            owls[0]["id"],
            BotDuplicateInput(name="Κουκουβάγια 0 (αντίγραφο)", request_id="dup_" + uuid4().hex),
        )


def test_a_deleted_owl_keeps_no_list_place(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη"))
    organize(store, owl, pinned=True, archived=True)
    store.delete_bot("alice", owl["id"])
    with store.connect() as db:
        row = db.execute(
            "SELECT pinned_at,archived_at FROM bots WHERE id=?", (owl["id"],)
        ).fetchone()
    assert tuple(row) == (None, None)


def test_an_older_database_gains_the_list_place(tmp_path):
    import sqlite3

    path = tmp_path / "bots.sqlite3"
    Store(path).save_bot("alice", BotInput(name="Νεφέλη"))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE bots DROP COLUMN pinned_at")
        db.execute("ALTER TABLE bots DROP COLUMN archived_at")
        db.execute("PRAGMA user_version=42")
    reopened = Store(path)
    (owl,) = reopened.list_bots("alice")
    assert (owl["pinned_at"], owl["archived_at"]) == (None, None)


def test_duplicate_thinks_with_the_same_model(store):
    owl = store.save_bot("alice", BotCreateInput(name="花子", model="fast-model"))
    copy = store.duplicate_bot(
        "alice",
        owl["id"],
        BotDuplicateInput(name="花子 (αντίγραφο)", request_id="dup_" + uuid4().hex),
    )
    assert copy["model"] == "fast-model"
