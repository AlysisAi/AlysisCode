"""Scheduling contracts and real worker lifecycle; provider fixtures never call a model."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from bots_task_state import task_state
from pydantic import ValidationError

from alysis_code.bots.models import BotInput, NoteInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput, advance_run, next_run
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def data(**overrides):
    return ScheduleInput.model_validate(
        {
            "name": "Review",
            "prompt": "Review current project notes; explain any unresolved decisions.",
            "timing": {"kind": "interval", "every_minutes": 5},
            **overrides,
        }
    )


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "schedules.db")
    bot = store.save_bot("alice", BotInput(name="Έλενα", purpose="Research notes"))
    return store, bot["id"]


@pytest.mark.parametrize(
    "timing,after,expected",
    [
        (
            {"kind": "once", "at": "2026-10-19T09:00:00+03:00"},
            "2026-10-18T00:00:00+00:00",
            "2026-10-19T06:00:00+00:00",
        ),
        (
            {"kind": "daily", "time": "09:00", "timezone": "Asia/Kathmandu"},
            "2026-10-18T03:15:00+00:00",
            "2026-10-19T03:15:00+00:00",
        ),
        (
            {"kind": "daily", "time": "02:30", "timezone": "America/New_York"},
            "2026-03-08T00:00:00+00:00",
            "2026-03-09T06:30:00+00:00",
        ),
        (
            {"kind": "weekly", "time": "02:30", "timezone": "America/New_York", "weekday": 6},
            "2026-03-08T00:00:00+00:00",
            "2026-03-15T06:30:00+00:00",
        ),
        (
            # Every weekday: Friday 2026-11-13 10:00 Athens moves on to Monday 09:00.
            {
                "kind": "weekly",
                "time": "09:00",
                "timezone": "Europe/Athens",
                "weekdays": [0, 1, 2, 3, 4],
            },
            "2026-11-13T08:00:00+00:00",
            "2026-11-16T07:00:00+00:00",
        ),
        (
            # Monday, Wednesday and Friday, listed in any order.
            {"kind": "weekly", "time": "09:00", "timezone": "UTC", "weekdays": [4, 0, 2]},
            "2026-11-16T09:00:00+00:00",
            "2026-11-18T09:00:00+00:00",
        ),
        (
            {"kind": "daily", "time": "01:30", "timezone": "America/New_York"},
            "2026-11-01T00:00:00+00:00",
            "2026-11-01T05:30:00+00:00",
        ),
        (
            {"kind": "daily", "time": "01:30", "timezone": "America/New_York"},
            "2026-11-01T05:30:00+00:00",
            "2026-11-02T06:30:00+00:00",
        ),
        (
            {"kind": "daily", "time": "03:30", "timezone": "Europe/Athens"},
            "2026-03-29T00:00:00+00:00",
            "2026-03-30T00:30:00+00:00",
        ),
    ],
)
def test_real_timezone_boundaries(timing, after, expected):
    assert next_run(data(timing=timing).timing, stamp(after)) == stamp(expected)


@pytest.mark.parametrize(
    "timing",
    [
        {"kind": "once", "at": "2026-10-19T09:00:00"},
        {"kind": "daily", "time": "25:00", "timezone": "UTC"},
        {"kind": "daily", "time": "09:00", "timezone": "not/a/timezone"},
        {"kind": "daily", "time": "09:00", "timezone": "UTC", "weekday": 1},
        {"kind": "weekly", "time": "09:00", "timezone": "UTC"},
        {"kind": "weekly", "time": "09:00", "timezone": "UTC", "weekdays": []},
        {"kind": "weekly", "time": "09:00", "timezone": "UTC", "weekdays": [0, 0]},
        {"kind": "weekly", "time": "09:00", "timezone": "UTC", "weekdays": [7]},
        {"kind": "weekly", "time": "09:00", "timezone": "UTC", "weekday": 0, "weekdays": [1]},
        {"kind": "daily", "time": "09:00", "timezone": "UTC", "weekdays": [0, 1]},
        {"kind": "interval", "every_minutes": 1},
        {"kind": "interval", "every_minutes": 5, "unknown": "ignore"},
    ],
)
def test_invalid_or_ambiguous_timing_rejected(timing):
    with pytest.raises(ValidationError):
        data(timing=timing)


def test_saved_routines_keep_their_shape():
    # A routine saved before multi-day weeks, and one saved now, serialize as written.
    single = data(timing={"kind": "weekly", "time": "09:00", "timezone": "UTC", "weekday": 6})
    daily = data(timing={"kind": "daily", "time": "09:00", "timezone": "UTC"})
    several = data(
        timing={"kind": "weekly", "time": "09:00", "timezone": "UTC", "weekdays": [0, 1]}
    )
    assert "weekdays" not in single.timing.model_dump(mode="json")
    assert "weekdays" not in daily.timing.model_dump(mode="json")
    assert several.timing.model_dump(mode="json")["weekdays"] == [0, 1]
    assert single.timing.days == (6,) and several.timing.days == (0, 1) and daily.timing.days == ()


def test_the_schedule_tool_tells_the_model_how_to_say_weekdays():
    schema = ScheduleInput.model_json_schema()
    assert "[0, 1, 2, 3, 4] for every weekday" in str(schema)


def test_interval_advance_keeps_anchor_without_backlog():
    timing = data().timing
    assert next_run(timing, 1000) == 1300
    assert advance_run(timing, 1300, 1913) == 2200


def test_occurrence_transaction_survives_restart_and_concurrent_ticks(setup):
    store, bot = setup
    schedule = store.save_schedule("alice", bot, data(), now=1000)
    with ThreadPoolExecutor(max_workers=6) as pool:
        counts = list(
            pool.map(
                lambda _: store.enqueue_due_schedules("alice", max_calls=20, now=1301), range(12)
            )
        )
    assert sum(counts) == 1
    reopened = Store(store.path)
    assert reopened.enqueue_due_schedules("alice", max_calls=20, now=1302) == 0
    runs = reopened.schedule_runs("alice", bot, schedule["id"])["runs"]
    assert len(runs) == 1 and runs[0]["current_status"] == "queued"
    task = reopened.task("alice", runs[0]["task_id"])
    assert task["message"] == data().prompt
    assert len(reopened.list_chats("alice", bot)) == 1
    assert any(e["kind"] == "scheduled" for e in task["events"])
    reopened.claim("alice")
    reopened.finish("alice", task["id"], "completed", content="Actual result")
    assert (
        reopened.schedule_runs("alice", bot, schedule["id"])["runs"][0]["current_status"]
        == "completed"
    )
    assert reopened.list_schedules("alice", bot)[0]["last_run"]["status"] == "completed"


def test_failed_enqueue_rolls_back_due_time_run_and_chat(setup, monkeypatch):
    store, bot = setup
    store.save_schedule("alice", bot, data(), now=1000)
    original = store._enqueue

    def failing(*args):
        original(*args)
        raise RuntimeError("Injected transaction fault after task creation")

    monkeypatch.setattr(store, "_enqueue", failing)
    with pytest.raises(RuntimeError):
        store.enqueue_due_schedules("alice", max_calls=10, now=1300)
    assert store.list_schedules("alice", bot)[0]["next_run_at"] == 1300
    assert store.list_chats("alice", bot) == []
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM schedule_runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    monkeypatch.setattr(store, "_enqueue", original)
    assert store.enqueue_due_schedules("alice", max_calls=10, now=1300) == 1


def test_missed_backlog_is_recorded_once_and_never_replayed(setup):
    store, bot = setup
    schedule = store.save_schedule("alice", bot, data(), now=1000)
    assert store.enqueue_due_schedules("alice", max_calls=10, now=10000) == 0
    assert store.enqueue_due_schedules("alice", max_calls=10, now=10000) == 0
    runs = store.schedule_runs("alice", bot, schedule["id"])["runs"]
    assert len(runs) == 1 and runs[0]["current_status"] == "missed"
    assert store.list_schedules("alice", bot)[0]["next_run_at"] == 10300


def test_overlap_deferred_and_pause_does_not_cancel_existing_task(setup):
    store, bot = setup
    s = store.save_schedule("alice", bot, data(), now=1000)
    assert store.enqueue_due_schedules("alice", max_calls=10, now=1300) == 1
    assert store.enqueue_due_schedules("alice", max_calls=10, now=1600) == 0
    runs = store.schedule_runs("alice", bot, s["id"])["runs"]
    assert len(runs) == 1 and runs[0]["current_status"] == "queued"
    assert store.list_schedules("alice", bot)[0]["next_run_at"] == 1600
    store.decide_schedule("alice", bot, s["id"], "paused", 1, now=1600)
    assert store.enqueue_due_schedules("alice", max_calls=10, now=1900) == 0
    assert store.task("alice", runs[0]["task_id"])["status"] == "queued"
    resumed = store.decide_schedule("alice", bot, s["id"], "active", 2, now=2500)
    assert resumed["next_run_at"] == 2800
    with pytest.raises(Conflict):
        store.save_schedule("alice", bot, data(expected_revision=2), schedule_id=s["id"], now=2600)
    assert store.list_schedules("alice", bot)[0]["revision"] == 3


def test_budget_exhaustion_pauses_without_creating_task(setup):
    store, bot = setup
    s = store.save_schedule("alice", bot, data(), now=1000)
    assert store.enqueue_due_schedules("alice", max_calls=0, now=1300) == 0
    current = store.list_schedules("alice", bot)[0]
    assert current["state"] == "paused" and current["next_run_at"] is None
    assert "allowance" in current["pause_reason"]
    assert store.list_chats("alice", bot) == []
    assert store.schedule_runs("alice", bot, s["id"])["runs"][0]["current_status"] == "skipped"


def test_queue_full_retains_due_time_without_extra_chat(setup):
    store, bot = setup
    s = store.save_schedule("alice", bot, data(), now=1000)
    for i in range(20):
        other = store.save_bot("alice", BotInput(name=f"Busy {i}", purpose="Queue occupancy"))
        chat = store.create_chat("alice", other["id"], "Occupied")
        store.enqueue(
            "alice", chat["id"], TaskInput(message="Waiting", request_id=f"queued_{i:03}")
        )
    assert store.enqueue_due_schedules("alice", max_calls=30, now=1300) == 0
    assert store.list_chats("alice", bot) == []
    assert store.schedule_runs("alice", bot, s["id"])["runs"] == []
    assert store.list_schedules("alice", bot)[0]["next_run_at"] == 1300


def test_drafts_require_user_action_and_are_isolated_from_other_bots(setup):
    store, bot = setup
    chat = store.create_chat("alice", bot, "Proposal")
    store.enqueue("alice", chat["id"], TaskInput(message="Later", request_id="proposal_1"))
    task = store.claim("alice")
    rt = BotRuntime(store, "alice", lambda _: None)
    proposed = rt._tool(
        task,
        "propose_schedule",
        data().model_dump(exclude={"enabled", "expected_revision", "request_id"}),
        ["propose_schedule"],
    )
    assert proposed["state"] == "draft"
    s = store.list_schedules("alice", bot)[0]
    assert s["next_run_at"] is None and s["source_task_id"] == task["id"]
    assert store.enqueue_due_schedules("alice", max_calls=10, now=time.time() + 600) == 0
    with pytest.raises(ValidationError):
        rt._tool(
            task, "propose_schedule", {**data().model_dump(), "enabled": True}, ["propose_schedule"]
        )
    other = store.save_bot("alice", BotInput(name="Other", purpose="Separate"))["id"]
    for owner, identity in [("bob", bot), ("alice", other)]:
        with pytest.raises(NotFound):
            store.decide_schedule(owner, identity, s["id"], "active", 1)
        with pytest.raises(NotFound):
            store.schedule_runs(owner, identity, s["id"])
    assert store.decide_schedule("alice", bot, s["id"], "active", 1)["state"] == "active"


@pytest.mark.parametrize("state", ["failed", "interrupted"])
def test_failed_run_pauses_only_the_unchanged_schedule(setup, state):
    store, bot = setup
    s = store.save_schedule("alice", bot, data(), now=1000)
    store.enqueue_due_schedules("alice", max_calls=10, now=1300)
    task = store.claim("alice")
    store.finish("alice", task["id"], state, error="Test failure")
    assert store.list_schedules("alice", bot)[0]["state"] == "paused"
    store.decide_schedule("alice", bot, s["id"], "active", 2, now=1600)
    store.enqueue_due_schedules("alice", max_calls=10, now=1900)
    next_task = store.claim("alice")
    edited = store.save_schedule(
        "alice", bot, data(name="Updated", expected_revision=3), schedule_id=s["id"], now=1901
    )
    store.finish("alice", next_task["id"], state, error="Old run failed")
    current = store.list_schedules("alice", bot)[0]
    assert current["state"] == "active" and current["revision"] == edited["revision"]
    assert store.schedule_runs("alice", bot, s["id"])["runs"][0]["config"]["name"] == "Review"


def test_restart_interrupts_active_run_and_pauses_schedule_without_replay(setup):
    store, bot = setup
    s = store.save_schedule("alice", bot, data(), now=1000)
    store.enqueue_due_schedules("alice", max_calls=10, now=1300)
    task = store.claim("alice")
    restarted = Store(store.path)
    restarted.recover("alice")
    assert restarted.task("alice", task["id"])["status"] == "interrupted"
    assert restarted.list_schedules("alice", bot)[0]["state"] == "paused"
    assert restarted.enqueue_due_schedules("alice", max_calls=10, now=1600) == 0
    assert len(restarted.schedule_runs("alice", bot, s["id"])["runs"]) == 1


def test_one_time_completion_and_past_time_require_edit(setup):
    store, bot = setup
    s = store.save_schedule(
        "alice",
        bot,
        data(timing={"kind": "once", "at": datetime.fromtimestamp(1300, UTC)}),
        now=1000,
    )
    assert store.enqueue_due_schedules("alice", max_calls=10, now=1300) == 1
    assert store.list_schedules("alice", bot)[0]["state"] == "ended"
    with pytest.raises(Conflict):
        store.decide_schedule("alice", bot, s["id"], "active", 1, now=1400)


def test_run_history_pagination_and_current_permissions(setup):
    store, bot = setup
    s = store.save_schedule("alice", bot, data(), now=1000)
    for i in range(24):
        store.enqueue_due_schedules("alice", max_calls=30, now=1300 + i * 300)
        task = store.claim("alice")
        store.finish("alice", task["id"], "completed", content="Done")
    first = store.schedule_runs("alice", bot, s["id"])
    second = store.schedule_runs("alice", bot, s["id"], first["next_before"])
    assert len(first["runs"]) == 20 and len(second["runs"]) == 4
    assert len({r["id"] for r in first["runs"] + second["runs"]}) == 24
    store.save_bot(
        "alice",
        BotInput(name="Changed", purpose="Updated", memory_enabled=False, files_enabled=False),
        bot,
    )
    store.enqueue_due_schedules("alice", max_calls=30, now=8500)
    task = store.claim("alice")
    assert task["config"]["name"] == "Changed"
    assert not task["config"]["memory_enabled"] and not task["config"]["files_enabled"]


def test_v2_migration_preserves_working_notes(setup):
    store, bot = setup
    note = store.save_note(
        "alice", bot, NoteInput(title="Pilot", content="Unicode 日本語 Ελληνικά")
    )
    # Remove only the new tables in this disposable test database to reproduce v2.
    with sqlite3.connect(store.path) as db:
        db.executescript(
            "DROP TABLE schedule_runs; DROP TABLE schedule_requests; DROP TABLE schedules; PRAGMA user_version=2;"
        )
    reopened = Store(store.path)
    assert reopened.note("alice", bot, note["id"])["content"] == note["content"]
    assert reopened.list_schedules("alice", bot) == []


def wait_for(predicate):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for worker")


def test_real_scheduler_thread_runs_without_any_desktop_and_uses_notes(setup):
    store, bot = setup
    store.save_note("alice", bot, NoteInput(title="Pilot", content="Open decision: offline import"))
    s = store.save_schedule(
        "alice",
        bot,
        data(timing={"kind": "once", "at": datetime.fromtimestamp(time.time() + 0.2, UTC)}),
    )
    seen = []

    class Fixture:
        model = "schedule-test-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            seen.append(kwargs["messages"])
            return LLMResponse("Fixture finished", [], {})

    rt = BotRuntime(Store(store.path), "alice", lambda _: Fixture())
    rt.start()
    try:
        wait_for(lambda: store.list_schedules("alice", bot)[0]["last_run"] is not None)
        task_id = store.schedule_runs("alice", bot, s["id"])["runs"][0]["task_id"]
        wait_for(lambda: store.task("alice", task_id)["status"] == "completed")
        assert rt.is_running and len(seen) == 1
        assert "offline import" in task_state(seen[0])
        assert (
            store.schedule_runs("alice", bot, s["id"])["runs"][0]["current_status"] == "completed"
        )
    finally:
        rt.stop()
    assert not rt.is_running


@pytest.mark.parametrize("same_bot", [True, False])
def test_scheduler_keeps_ticking_while_provider_is_busy(setup, same_bot, monkeypatch):
    store, bot = setup
    release = threading.Event()
    entered = threading.Event()

    class BlockingFixture:
        model = "blocking-test-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            entered.set()
            assert release.wait(timeout=5)
            return LLMResponse("Done", [], {})

    chat = store.create_chat("alice", bot, "Long task")
    store.enqueue("alice", chat["id"], TaskInput(message="Work", request_id="blocking_request"))
    rt = BotRuntime(store, "alice", lambda _: BlockingFixture())
    rt.start()
    try:
        assert entered.wait(timeout=2)
        target = (
            bot
            if same_bot
            else store.save_bot("alice", BotInput(name="Peer", purpose="Independent work"))["id"]
        )
        s = store.save_schedule(
            "alice",
            target,
            data(timing={"kind": "once", "at": datetime.fromtimestamp(time.time() + 0.2, UTC)}),
        )
        due_tick = threading.Event()
        original = store.enqueue_due_schedules

        def observe_tick(*args, **kwargs):
            result = original(*args, **kwargs)
            if time.time() >= s["next_run_at"]:
                due_tick.set()
            return result

        monkeypatch.setattr(store, "enqueue_due_schedules", observe_tick)
        assert due_tick.wait(3)
        runs = store.schedule_runs("alice", target, s["id"])["runs"]
        if same_bot:
            assert runs == []
            assert store.list_schedules("alice", target)[0]["next_run_at"] == s["next_run_at"]
        else:
            assert len(runs) == 1 and runs[0]["current_status"] == "queued"
        release.set()
        wait_for(lambda: bool(store.schedule_runs("alice", target, s["id"])["runs"]))
        task_id = store.schedule_runs("alice", target, s["id"])["runs"][0]["task_id"]
        wait_for(lambda: store.task("alice", task_id)["status"] == "completed")
        assert len(store.schedule_runs("alice", target, s["id"])["runs"]) == 1
    finally:
        release.set()
        rt.stop()


def test_provider_failure_does_not_retry_or_repeat_schedule(setup):
    store, bot = setup
    s = store.save_schedule("alice", bot, data(), now=1000)
    store.enqueue_due_schedules("alice", max_calls=5, now=1300)
    calls = []

    class FailedFixture:
        model = "auth-failure-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            calls.append(1)
            raise RuntimeError("Sensitive provider exception, must not be stored")

    rt = BotRuntime(store, "alice", lambda _: FailedFixture(), limits=RuntimeLimits(max_calls=5))
    rt.run(store.claim("alice"), EventCancellationToken(threading.Event()))
    assert len(calls) == 1
    assert store.list_schedules("alice", bot)[0]["state"] == "paused"
    run = store.schedule_runs("alice", bot, s["id"])["runs"][0]
    assert "Sensitive" not in run["task_error"]
    assert store.enqueue_due_schedules("alice", max_calls=5, now=1600) == 0


def test_creation_retry_returns_original_even_after_one_time_due_date(setup):
    store, bot = setup
    request = data(
        request_id="stable-create-request", timing={"kind": "once", "at": "2026-10-20T00:00:00Z"}
    )
    with ThreadPoolExecutor(max_workers=5) as pool:
        schedules = list(
            pool.map(lambda _: store.save_schedule("alice", bot, request, now=1000), range(8))
        )
    assert len({s["id"] for s in schedules}) == 1
    assert len(store.list_schedules("alice", bot)) == 1
    assert (
        store.save_schedule("alice", bot, request, now=stamp("2027-01-01T00:00:00Z"))["id"]
        == schedules[0]["id"]
    )
    with pytest.raises(Conflict):
        store.save_schedule(
            "alice",
            bot,
            data(request_id="stable-create-request", prompt="A different task"),
            now=1000,
        )


def test_tool_shape_feedback_allows_model_to_correct_json_string(setup):
    import json

    from alysis_code.bots.runtime import tool_schema
    from alysis_code.llm.types import ToolCall

    store, bot = setup
    chat = store.create_chat("alice", bot, "Draft")
    store.enqueue(
        "alice", chat["id"], TaskInput(message="Schedule later", request_id="shape_feedback")
    )
    timing = {"kind": "daily", "time": "09:00", "timezone": "Europe/Athens"}
    bad = {"name": "Review", "prompt": "Review notes", "timing": json.dumps(timing)}
    good = {**bad, "timing": timing}
    requests = []
    responses = iter(
        [
            LLMResponse("", [ToolCall("bad", "propose_schedule", bad)], {}),
            LLMResponse("", [ToolCall("good", "propose_schedule", good)], {}),
            LLMResponse("Draft ready for review", [], {}),
        ]
    )

    class CorrectingFixture:
        model = "schema-feedback-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            requests.append(json.loads(json.dumps(kwargs["messages"])))
            return next(responses)

    task = store.claim("alice")
    BotRuntime(store, "alice", lambda _: CorrectingFixture()).run(
        task, EventCancellationToken(threading.Event())
    )
    result = store.task("alice", task["id"])
    assert result["status"] == "completed" and result["calls"] == 3
    feedback = json.loads(next(m for m in reversed(requests[1]) if m["role"] == "tool")["content"])
    assert feedback["validation_errors"][0]["loc"] == ["timing"]
    assert "input" not in feedback["validation_errors"][0]
    assert "Review notes" not in json.dumps(
        [e for e in result["events"] if e["kind"] == "tool_failed"]
    )
    assert store.list_schedules("alice", bot)[0]["state"] == "draft"
    assert (
        tool_schema("propose_schedule")["function"]["parameters"]["properties"]["timing"]["type"]
        == "object"
    )


def test_scheduled_brief_preserves_exact_request_and_user_edit_supersedes_it(setup):
    store, bot = setup
    chat = store.create_chat("alice", bot, "Original request")
    original = "Schedule a review including the release identifier and all launch exclusions."
    store.enqueue(
        "alice", chat["id"], TaskInput(message=original, request_id="source_brief_request")
    )
    source = store.claim("alice")
    schedule = store.save_schedule(
        "alice", bot, data(prompt="Review the launch"), source_task_id=source["id"], now=1000
    )
    store.finish("alice", source["id"], "completed", content="Draft ready")
    store.decide_schedule("alice", bot, schedule["id"], "active", 1, now=1000)
    store.enqueue_due_schedules("alice", max_calls=10, now=1300)
    task = store.claim("alice")
    assert store.schedule_context("alice", task["id"])["original_request"] == original
    assert original in task_state(BotRuntime(store, "alice", lambda _: None)._context(task))
    store.finish("alice", task["id"], "completed", content="Done")
    store.save_schedule(
        "alice",
        bot,
        data(prompt="New explicit user request", expected_revision=2),
        schedule_id=schedule["id"],
        now=1400,
    )
    store.enqueue_due_schedules("alice", max_calls=10, now=1700)
    updated = store.claim("alice")
    assert "original_request" not in store.schedule_context("alice", updated["id"])
    assert store.schedule_context("alice", task["id"])["original_request"] == original
    with pytest.raises(NotFound):
        store.schedule_context("bob", task["id"])
