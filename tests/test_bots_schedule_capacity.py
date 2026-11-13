"""Actual SQL admission with controlled task states/time; no model/live claim."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from test_bots_memory_changes import case  # noqa: F401

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.scheduling import ScheduleInput


def once(store, owner, bot, *, due=1100):
    return store.save_schedule(
        owner,
        bot["id"],
        ScheduleInput(
            name="Due reminder",
            prompt="Report the current project note.",
            timing={"kind": "once", "at": datetime.fromtimestamp(due, UTC)},
        ),
        now=1000,
    )


def test_foreground_busy_defers_original_occurrence_then_enqueues_once(case):  # noqa: F811
    store, owner, bot, human = case
    schedule = once(store, owner, bot)
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1100) == 0
    assert store.list_schedules(owner, bot["id"])[0]["next_run_at"] == 1100
    assert store.schedule_runs(owner, bot["id"], schedule["id"])["runs"] == []
    store.finish(owner, human["id"], "completed", content="Finished the conversation")
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(
            pool.map(
                lambda _: store.enqueue_due_schedules(owner, max_calls=None, now=1110), range(8)
            )
        )
    assert sum(counts) == 1
    runs = store.schedule_runs(owner, bot["id"], schedule["id"])["runs"]
    assert len(runs) == 1 and runs[0]["current_status"] == "queued"
    assert runs[0]["scheduled_for"] == 1100


def test_unrelated_background_wait_does_not_discard_reminder(case):  # noqa: F811
    store, owner, bot, human = case
    schedule = once(store, owner, bot)
    # Only the scheduling occupancy fixture is synthetic; admission is actual SQL.
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (human["id"],))
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1100) == 1
    run = store.schedule_runs(owner, bot["id"], schedule["id"])["runs"][0]
    assert store.claim(owner)["id"] == run["task_id"]
    assert store.task(owner, human["id"])["status"] == "waiting"


def test_same_routine_wait_cannot_overlap_and_retains_due_time(case):  # noqa: F811
    store, owner, bot, human = case
    store.finish(owner, human["id"], "completed", content="Done")
    schedule = store.save_schedule(
        owner,
        bot["id"],
        ScheduleInput(
            name="Review",
            prompt="Review the note.",
            timing={"kind": "interval", "every_minutes": 5},
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1300) == 1
    original = store.claim(owner)
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (original["id"],))
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1600) == 0
    assert store.list_schedules(owner, bot["id"])[0]["next_run_at"] == 1600
    assert len(store.schedule_runs(owner, bot["id"], schedule["id"])["runs"]) == 1
    store.finish(owner, original["id"], "cancelled")
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1660) == 1
    assert store.list_schedules(owner, bot["id"])[0]["next_run_at"] == 1900


def test_full_queue_defers_and_admits_after_capacity_returns(case):  # noqa: F811
    store, owner, bot, human = case
    other = store.save_bot(owner, BotInput(name="Other", purpose="Synthetic queue occupancy"))
    chat = store.create_chat(owner, other["id"], "Queue")
    for _ in range(19):
        store.enqueue(owner, chat["id"], TaskInput(message="Pending", request_id=uuid4().hex))
    schedule = once(store, owner, bot)
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1100) == 0
    assert store.list_schedules(owner, bot["id"])[0]["next_run_at"] == 1100
    assert not store.schedule_runs(owner, bot["id"], schedule["id"])["runs"]
    store.finish(owner, human["id"], "completed", content="Capacity released")
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1120) == 1


def test_busy_deadline_retains_existing_lateness_policy(case):  # noqa: F811
    store, owner, bot, _ = case
    schedule = once(store, owner, bot)
    assert store.enqueue_due_schedules(owner, max_calls=None, now=1100) == 0
    assert store.enqueue_due_schedules(owner, max_calls=None, now=2001) == 0
    runs = store.schedule_runs(owner, bot["id"], schedule["id"])["runs"]
    assert len(runs) == 1 and runs[0]["current_status"] == "missed"
    assert store.list_schedules(owner, bot["id"])[0]["state"] == "ended"
