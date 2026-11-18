"""Routines that don't quietly stop: retries, limit waits, pauses that say why, and Run now.

Real persistence with a controlled clock; failures are given as the structured outcome the
runtime records, and the classifier is checked on provider errors' own fields.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest

from alysis_code.bots import store as store_module
from alysis_code.bots.models import BotInput
from alysis_code.bots.runtime import routine_failure
from alysis_code.bots.scheduling import (
    FAILED_RUNS_BEFORE_PAUSE,
    LIMIT_RETRY_SECONDS,
    PAUSE_REASONS,
    RETRY_DELAYS_SECONDS,
    RunFailure,
    ScheduleInput,
)
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.llm.types import LLMError

HOUR = 3600


class Clock:
    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = Clock(1000.0)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=clock.time))
    store = Store(tmp_path / "routines.db")
    bot = store.save_bot("alice", BotInput(name="Ήρα", purpose="Ημερήσια σύνοψη"))["id"]
    return store, bot, clock


def routine(store, bot, *, every=120, name="Σύνοψη", **timing):
    return store.save_schedule(
        "alice",
        bot,
        ScheduleInput.model_validate(
            {
                "name": name,
                "prompt": "Γράψε τη σύνοψη της ημέρας.",
                "timing": timing or {"kind": "interval", "every_minutes": every},
            }
        ),
        now=1000,
    )


def run_and_fail(store, clock, at, failure, *, acted=False):
    """The run due at `at` starts and fails with `failure`; returns its task ID."""
    clock.now = at
    assert store.enqueue_due_schedules("alice", max_calls=None, now=at) == 1
    task = store.claim("alice")
    if acted:
        store.event("alice", task["id"], "tool_started", {"name": "save_file"})
    clock.now = at + 60
    store.finish("alice", task["id"], "failed", error="Δεν ολοκληρώθηκε.", routine_failure=failure)
    return task["id"]


def current(store, bot):
    return store.list_schedules("alice", bot)[0]


def test_a_busy_service_is_retried_with_growing_gaps_then_counts_as_one_failed_run(setup):
    store, bot, clock = setup
    item = routine(store, bot, every=240)  # first run 1000 + 4h
    due = item["next_run_at"]
    run_and_fail(store, clock, due, RunFailure("temporary"))
    failed_at = due + 60
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        retry = current(store, bot)["retry"]
        assert retry == {
            "at": failed_at + delay,
            "reason": "temporary",
            "attempt": attempt,
            "max_attempts": len(RETRY_DELAYS_SECONDS),
        }
        assert current(store, bot)["state"] == "active"
        # Not before its time.
        assert store.enqueue_due_schedules("alice", max_calls=None, now=retry["at"] - 1) == 0
        run_and_fail(store, clock, retry["at"], RunFailure("temporary"))
        failed_at = retry["at"] + 60
    after = current(store, bot)
    assert after["retry"] is None and after["failed_runs"] == 1
    assert after["state"] == "active" and after["next_run_at"] == due + 4 * HOUR
    runs = store.schedule_runs("alice", bot, item["id"])["runs"]
    assert [r["origin"] for r in runs] == ["retry", "retry", "retry", None]
    # The next scheduled run goes ahead as usual.
    clock.now = due + 4 * HOUR
    assert store.enqueue_due_schedules("alice", max_calls=None, now=clock.now) == 1


def test_a_retry_is_never_later_than_the_next_scheduled_run(setup):
    store, bot, clock = setup
    item = routine(store, bot, every=15)
    run_and_fail(store, clock, item["next_run_at"], RunFailure("temporary"))
    first = current(store, bot)["retry"]
    assert first["at"] == item["next_run_at"] + 60 + RETRY_DELAYS_SECONDS[0]
    run_and_fail(store, clock, first["at"], RunFailure("temporary"))
    # 15 more minutes would reach the next run, which does the same work.
    assert current(store, bot)["retry"] is None
    assert current(store, bot)["failed_runs"] == 1


def test_a_run_that_already_took_a_step_is_not_replayed(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    run_and_fail(store, clock, item["next_run_at"], RunFailure("temporary"), acted=True)
    after = current(store, bot)
    assert after["retry"] is None and after["failed_runs"] == 1 and after["state"] == "active"


def test_a_completed_run_clears_earlier_failures(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    run_and_fail(store, clock, item["next_run_at"], RunFailure("failed"))
    assert current(store, bot)["failed_runs"] == 1
    clock.now = item["next_run_at"] + 2 * HOUR
    store.enqueue_due_schedules("alice", max_calls=None, now=clock.now)
    store.finish("alice", store.claim("alice")["id"], "completed", content="Έτοιμο")
    assert current(store, bot)["failed_runs"] == 0


def test_failed_runs_in_a_row_pause_the_routine_saying_what_to_do(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    for index in range(FAILED_RUNS_BEFORE_PAUSE):
        run_and_fail(store, clock, item["next_run_at"] + index * 2 * HOUR, RunFailure("failed"))
    paused = current(store, bot)
    assert paused["state"] == "paused" and paused["next_run_at"] is None
    assert paused["pause_code"] == "repeated"
    assert paused["pause_reason"] == PAUSE_REASONS["repeated"]
    assert paused["upcoming_runs"] == []
    resumed = store.decide_schedule("alice", bot, item["id"], "active", paused["revision"])
    assert resumed["state"] == "active"
    again = current(store, bot)
    assert again["failed_runs"] == 0 and again["pause_code"] is None


def test_a_limit_with_a_known_reset_holds_runs_until_it_frees_up(setup):
    store, bot, clock = setup
    item = routine(store, bot, every=60)
    due = item["next_run_at"]
    reset = due + 3 * HOUR + 600
    run_and_fail(store, clock, due, RunFailure("limit", retry_at=reset))
    waiting = current(store, bot)
    assert waiting["state"] == "active" and waiting["pause_code"] is None
    assert waiting["retry"]["at"] == reset and waiting["retry"]["reason"] == "limit"
    # The next runs shown are the ones after the limit frees up, on the routine's own beat.
    assert waiting["upcoming_runs"] == [due + 4 * HOUR, due + 5 * HOUR, due + 6 * HOUR]
    # Runs that come due meanwhile are skipped once, with the reason, not run.
    clock.now = due + HOUR
    assert store.enqueue_due_schedules("alice", max_calls=None, now=clock.now) == 0
    assert current(store, bot)["next_run_at"] == due + 4 * HOUR
    skipped = store.schedule_runs("alice", bot, item["id"])["runs"][0]
    assert skipped["current_status"] == "skipped" and "limit" in skipped["reason"]
    # At the reset time the routine runs again, and success clears the wait.
    clock.now = reset
    assert store.enqueue_due_schedules("alice", max_calls=None, now=reset) == 1
    retry = store.schedule_runs("alice", bot, item["id"])["runs"][0]
    assert retry["origin"] == "retry" and retry["scheduled_for"] == reset
    store.finish("alice", store.claim("alice")["id"], "completed", content="Έγινε")
    done = current(store, bot)
    assert done["retry"] is None and done["failed_runs"] == 0


def test_a_limit_without_a_reset_time_is_tried_again_later_and_later(setup):
    store, bot, clock = setup
    item = routine(store, bot, every=24 * 60)
    run_and_fail(store, clock, item["next_run_at"], RunFailure("limit"))
    first = current(store, bot)["retry"]
    assert first["at"] == clock.now + LIMIT_RETRY_SECONDS and first["max_attempts"] is None
    run_and_fail(store, clock, first["at"], RunFailure("limit"))
    second = current(store, bot)["retry"]
    assert second["at"] == clock.now + 2 * LIMIT_RETRY_SECONDS and second["attempt"] == 2
    assert current(store, bot)["state"] == "active" and current(store, bot)["failed_runs"] == 0


def test_a_limit_hit_after_a_step_holds_runs_without_replaying_that_run(setup):
    store, bot, clock = setup
    item = routine(store, bot, every=60)
    due = item["next_run_at"]
    run_and_fail(store, clock, due, RunFailure("limit", retry_at=due + 90 * 60), acted=True)
    assert current(store, bot)["retry"]["reason"] == "limit"
    clock.now = due + HOUR
    assert store.enqueue_due_schedules("alice", max_calls=None, now=clock.now) == 0
    clock.now = due + 90 * 60
    assert store.enqueue_due_schedules("alice", max_calls=None, now=clock.now) == 0
    assert current(store, bot)["retry"] is None
    clock.now = due + 2 * HOUR
    assert store.enqueue_due_schedules("alice", max_calls=None, now=clock.now) == 1


def test_a_scheduled_run_due_by_then_supersedes_a_waiting_retry(setup):
    store, bot, clock = setup
    item = routine(store, bot, every=30)
    due = item["next_run_at"]
    run_and_fail(store, clock, due, RunFailure("temporary"))
    assert current(store, bot)["retry"] is not None
    # The computer slept through the retry and the next run: only the latest runs.
    clock.now = due + 31 * 60
    assert store.enqueue_due_schedules("alice", max_calls=None, now=clock.now) == 1
    assert current(store, bot)["retry"] is None
    runs = store.schedule_runs("alice", bot, item["id"])["runs"]
    assert [(r["scheduled_for"], r["origin"]) for r in runs] == [(due + 30 * 60, None), (due, None)]


def test_a_run_still_waiting_for_approval_pauses_the_routine_when_the_next_is_due(setup):
    store, bot, clock = setup
    item = routine(store, bot, every=60)
    clock.now = item["next_run_at"]
    store.enqueue_due_schedules("alice", max_calls=None, now=clock.now)
    task = store.claim("alice")
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
        db.execute(
            "INSERT INTO background_turns VALUES(?,'approval',?,?,0)", (task["id"], "", "[]")
        )
    clock.now += HOUR
    assert store.enqueue_due_schedules("alice", max_calls=None, now=clock.now) == 0
    paused = current(store, bot)
    assert paused["state"] == "paused" and paused["pause_code"] == "approval"
    assert paused["active_run"] == {"task_id": task["id"], "status": "waiting"}
    # The waiting run itself is untouched.
    assert store.task("alice", task["id"])["status"] == "waiting"


def test_a_cancelled_run_changes_nothing(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    clock.now = item["next_run_at"]
    store.enqueue_due_schedules("alice", max_calls=None, now=clock.now)
    task = store.claim("alice")
    store.cancel("alice", task["id"])
    store.finish("alice", task["id"], "cancelled")
    after = current(store, bot)
    assert after["state"] == "active" and after["failed_runs"] == 0 and after["retry"] is None


def test_run_now_runs_once_without_changing_the_schedule(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    clock.now = 2000
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: store.run_schedule_now(
                    "alice", bot, item["id"], "run-now-request-1", max_calls=None, now=2000
                ),
                range(8),
            )
        )
    assert len({r["task_id"] for r in results}) == 1
    (run,) = store.schedule_runs("alice", bot, item["id"])["runs"]
    assert run["origin"] == "manual" and run["scheduled_for"] == 2000
    listed = current(store, bot)
    assert listed["next_run_at"] == item["next_run_at"]
    assert listed["active_run"] == {"task_id": results[0]["task_id"], "status": "queued"}
    # A second click while it runs starts nothing; the same request still answers.
    with pytest.raises(Conflict, match="already running"):
        store.run_schedule_now("alice", bot, item["id"], "run-now-request-2", max_calls=None)
    assert (
        store.run_schedule_now("alice", bot, item["id"], "run-now-request-1", max_calls=None)[
            "task_id"
        ]
        == results[0]["task_id"]
    )
    task = store.claim("alice")
    assert task["id"] == results[0]["task_id"] and task["message"] == item["prompt"]
    assert store.schedule_context("alice", task["id"])["name"] == "Σύνοψη"
    store.finish("alice", task["id"], "completed", content="Έτοιμο")
    second = store.run_schedule_now(
        "alice", bot, item["id"], "run-now-request-2", max_calls=None, now=2500
    )
    assert second["task_id"] != results[0]["task_id"]


def test_run_now_failures_need_no_retry_but_a_sign_in_problem_still_pauses(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    clock.now = 2000
    task_id = store.run_schedule_now("alice", bot, item["id"], "manual-temporary", max_calls=None)[
        "task_id"
    ]
    store.claim("alice")
    store.finish("alice", task_id, "failed", routine_failure=RunFailure("temporary"))
    assert current(store, bot)["retry"] is None and current(store, bot)["failed_runs"] == 0
    clock.now = 2100
    task_id = store.run_schedule_now("alice", bot, item["id"], "manual-sign-in", max_calls=None)[
        "task_id"
    ]
    store.claim("alice")
    store.finish("alice", task_id, "failed", routine_failure=RunFailure("sign_in"))
    assert current(store, bot)["pause_code"] == "sign_in"


def test_run_now_works_on_a_paused_routine_and_refuses_drafts_and_other_routines(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    paused = store.decide_schedule("alice", bot, item["id"], "paused", 1)
    store.run_schedule_now("alice", bot, item["id"], "paused-run-1", max_calls=None)
    assert current(store, bot)["state"] == "paused" and current(store, bot)["next_run_at"] is None
    assert current(store, bot)["revision"] == paused["revision"]
    other = routine(store, bot, name="Άλλη")
    with pytest.raises(Conflict, match="different routine"):
        store.run_schedule_now("alice", bot, other["id"], "paused-run-1", max_calls=None)
    with pytest.raises(NotFound):
        store.run_schedule_now("bob", bot, item["id"], "foreign-run-1", max_calls=None)
    with store.connect(write=True) as db:
        db.execute("UPDATE schedules SET state='draft' WHERE id=?", (other["id"],))
    with pytest.raises(Conflict, match="turn it on"):
        store.run_schedule_now("alice", bot, other["id"], "draft-run-1", max_calls=None)


def test_run_now_respects_the_model_call_allowance(setup):
    store, bot, _ = setup
    item = routine(store, bot)
    with pytest.raises(LimitReached, match="allowance"):
        store.run_schedule_now("alice", bot, item["id"], "allowance-run", max_calls=0)
    assert store.schedule_runs("alice", bot, item["id"])["runs"] == []


def test_deleting_a_routine_or_its_owl_removes_its_retries_and_requests(setup):
    store, bot, clock = setup
    item = routine(store, bot)
    run_and_fail(store, clock, item["next_run_at"], RunFailure("temporary"))
    store.run_schedule_now("alice", bot, item["id"], "before-delete", max_calls=None)
    store.finish("alice", store.claim("alice")["id"], "completed", content="")
    store.delete_schedule("alice", bot, item["id"])
    with store.connect() as db:
        for table in ("schedule_health", "schedule_run_requests"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    second = routine(store, bot, name="Δεύτερη")
    run_and_fail(store, clock, clock.now + 3 * HOUR, RunFailure("temporary"))
    store.delete_bot("alice", bot)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM schedule_health").fetchone()[0] == 0
        assert db.execute("SELECT 1 FROM schedules WHERE id=?", (second["id"],)).fetchone() is None


def test_an_older_database_gains_the_run_origin_and_retry_tables(tmp_path):
    import sqlite3

    store = Store(tmp_path / "old.db")
    bot = store.save_bot("alice", BotInput(name="Παλιά", purpose="Αναβάθμιση"))["id"]
    item = routine(store, bot)
    store.enqueue_due_schedules("alice", max_calls=None, now=item["next_run_at"])
    with sqlite3.connect(store.path) as db:
        db.executescript(
            "DROP TABLE schedule_health; DROP TABLE schedule_run_requests;"
            "ALTER TABLE schedule_runs DROP COLUMN origin; PRAGMA user_version=40;"
        )
    upgraded = Store(store.path)
    (run,) = upgraded.schedule_runs("alice", bot, item["id"])["runs"]
    assert run["origin"] is None and run["current_status"] == "queued"
    assert upgraded.list_schedules("alice", bot)[0]["failed_runs"] == 0


def provider_error(*, status=None, code=None, reset_at=0, body=None):
    error = LLMError("LLM error")
    if status:
        error.status_code = status
    if code:
        error.provider_error_code = code
    if reset_at:
        error.provider_reset_at = reset_at
    if body:
        error.provider_error_body = json.dumps(body, ensure_ascii=False)
    return error


def test_failures_are_classified_from_status_codes_and_error_types_not_wording():
    future = int(datetime(2099, 1, 1).timestamp())
    assert routine_failure(provider_error(status=503)).kind == "temporary"
    assert routine_failure(provider_error(status=429)).kind == "temporary"
    assert routine_failure(TimeoutError()).kind == "temporary"
    assert routine_failure(ConnectionError()).kind == "temporary"
    assert routine_failure(provider_error(status=429, reset_at=future)) == RunFailure(
        "limit", retry_at=float(future)
    )
    assert routine_failure(provider_error(status=429, code="insufficient_quota")).kind == "limit"
    # The hosted plan's own limit, in whatever language its message is written.
    plan = provider_error(
        status=429,
        reset_at=future,
        body={"error": {"code": "plan_limit_reached", "message": "上限に達しました"}},
    )
    assert routine_failure(plan) == RunFailure("limit", retry_at=float(future))
    assert routine_failure(provider_error(status=401)).kind == "sign_in"
    assert routine_failure(provider_error(status=400, code="invalid_api_key")).kind == "sign_in"
    assert routine_failure(provider_error(status=403)).kind == "setting"
    assert routine_failure(provider_error(status=400)).kind == "failed"
    # Wording that sounds like a sign-in problem changes nothing without the status.
    assert routine_failure(RuntimeError("401 Unauthorized: sign in again")).kind == "failed"
    wrapped = RuntimeError("Η κλήση απέτυχε")
    wrapped.__cause__ = provider_error(status=401)
    assert routine_failure(wrapped).kind == "sign_in"


def test_the_runtime_records_a_provider_sign_in_failure_so_the_routine_pauses(setup):
    from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
    from alysis_code.cancellation import EventCancellationToken

    store, bot, clock = setup
    item = routine(store, bot)
    clock.now = item["next_run_at"]
    store.enqueue_due_schedules("alice", max_calls=None, now=clock.now)

    class Rejected:
        model = "sign-in-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            raise provider_error(status=401)

    BotRuntime(store, "alice", lambda _: Rejected(), limits=RuntimeLimits(max_calls=5)).run(
        store.claim("alice"), EventCancellationToken(threading.Event())
    )
    paused = current(store, bot)
    assert paused["state"] == "paused" and paused["pause_code"] == "sign_in"


def test_run_now_route_capabilities_and_new_timings_over_http(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "run-now.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    store = app.state.store
    bot = store.save_bot(settings.owner, BotInput(name="Μίρα", purpose="Routes"))["id"]
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        capabilities = set(client.get("/v1/status").json()["capabilities"])
        assert {
            "schedule_run_now",
            "schedule_next_runs",
            "schedule_monthly",
            "schedule_windows",
            "schedule_retries",
        } <= capabilities
        created = client.post(
            f"/v1/bots/{bot}/schedules",
            json={
                "name": "Γραφείο",
                "prompt": "Έλεγξε τα νέα.",
                "timing": {
                    "kind": "window",
                    "every_minutes": 30,
                    "start": "09:00",
                    "end": "17:00",
                    "weekdays": [0, 1, 2, 3, 4],
                    "timezone": "Europe/Athens",
                },
                "request_id": "route-create-window",
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()
        path = f"/v1/bots/{bot}/schedules/{item['id']}/run"
        first = client.post(path, json={"request_id": "route-run-now-1"})
        assert first.status_code == 202, first.text
        again = client.post(path, json={"request_id": "route-run-now-1"})
        assert again.status_code == 202 and again.json() == first.json()
        assert client.post(path, json={"request_id": "short"}).status_code == 422
        assert (
            client.post(path, json={"request_id": "route-run-now-2", "now": 1}).status_code == 422
        )
        listed = client.get(f"/v1/bots/{bot}/schedules").json()[0]
        assert len(listed["upcoming_runs"]) == 3 and listed["next_run_at"] == item["next_run_at"]
        assert {"active_run", "retry", "failed_runs", "pause_code"} <= set(listed)
        monthly = client.post(
            f"/v1/bots/{bot}/schedules",
            json={
                "name": "Μήνας",
                "prompt": "Σύνοψη.",
                "timing": {"kind": "monthly", "day": 31, "time": "09:00", "timezone": "UTC"},
                "request_id": "route-create-monthly",
            },
        )
        assert monthly.status_code == 201, monthly.text
        with store.connect(write=True) as db:
            db.execute("UPDATE schedules SET state='draft' WHERE id=?", (monthly.json()["id"],))
        draft = client.post(
            f"/v1/bots/{bot}/schedules/{monthly.json()['id']}/run",
            json={"request_id": "route-run-draft"},
        )
        assert draft.status_code == 409
