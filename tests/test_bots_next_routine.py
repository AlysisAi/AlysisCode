"""When an owl's next routine run is due, as the owl list tells the desktop app.

The app keeps the computer awake shortly before it, so only runs that will really start count:
routines that are on, retries of failed runs, never paused routines or an archived owl's.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from alysis_code.bots.models import BotInput, BotOrganizeInput
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store

NOW = datetime.fromisoformat("2026-11-18T08:00:00+00:00").timestamp()


def routine(name, timing, **overrides):
    return ScheduleInput.model_validate(
        {"name": name, "prompt": "Γράψε μια σύντομη σύνοψη των νέων σημειώσεων.", "timing": timing}
        | overrides
    )


def hourly(name="Κάθε ώρα", **overrides):
    return routine(name, {"kind": "interval", "every_minutes": 60}, **overrides)


def next_routine(store, owl, now=NOW):
    return {item["id"]: item for item in store.list_bots("alice", now=now)}[owl["id"]][
        "next_routine_at"
    ]


def save_health(store, schedule, **health):
    with store.connect(write=True) as db:
        Store._save_schedule_health(
            db, schedule["id"], schedule["revision"], NOW, failures=1, attempts=1, **health
        )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bots.sqlite3")


def test_the_earliest_routine_that_is_on_counts_and_paused_ones_never(store):
    owl = store.save_bot("alice", BotInput(name="Αθηνά", purpose="Πρωινή σύνοψη"))
    other = store.save_bot("alice", BotInput(name="花子", purpose="調べもの"))
    assert next_routine(store, owl) is None

    store.save_schedule("alice", owl["id"], hourly(), now=NOW)
    # 10:30 in Athens is 08:30 UTC in November: before the hourly one.
    daily = store.save_schedule(
        "alice",
        owl["id"],
        routine("Πρωινά", {"kind": "daily", "time": "10:30", "timezone": "Europe/Athens"}),
        now=NOW,
    )
    # Switched off from the start, and due sooner than either: it never counts.
    store.save_schedule(
        "alice",
        owl["id"],
        routine("Ανενεργή", {"kind": "interval", "every_minutes": 5}, enabled=False),
        now=NOW,
    )
    # Another owl's routine is that owl's.
    store.save_schedule(
        "alice", other["id"], routine("毎時", {"kind": "interval", "every_minutes": 15}), now=NOW
    )
    assert next_routine(store, owl) == NOW + 1800
    assert next_routine(store, other) == NOW + 900

    store.decide_schedule("alice", owl["id"], daily["id"], "paused", daily["revision"], now=NOW)
    assert next_routine(store, owl) == NOW + 3600

    # A time already past still counts until the run starts: it starts late.
    assert next_routine(store, owl, now=NOW + 4000) == NOW + 3600


def test_an_archived_owls_routines_rest_until_it_is_back(store):
    owl = store.save_bot("alice", BotInput(name="Νεφέλη", purpose="Έρευνα"))
    store.save_schedule("alice", owl["id"], hourly(), now=NOW)
    assert next_routine(store, owl) == NOW + 3600

    store.organize_bot("alice", owl["id"], BotOrganizeInput(archived=True), now=NOW + 60)
    assert next_routine(store, owl) is None

    store.organize_bot("alice", owl["id"], BotOrganizeInput(archived=False), now=NOW + 120)
    assert next_routine(store, owl) == NOW + 3600


def test_a_failed_runs_retry_counts_and_a_limit_holds_the_runs_after_it(store):
    owl = store.save_bot("alice", BotInput(name="Ερμής", purpose="Ειδήσεις"))
    schedule = store.save_schedule("alice", owl["id"], hourly(), now=NOW)

    # A run that failed for a moment is tried again before the next one.
    save_health(store, schedule, retry_at=NOW + 300, retry_for=NOW, retry_reason="temporary")
    assert next_routine(store, owl) == NOW + 300

    # A plan or usage limit with nothing to replay only holds the routine's runs: the run at
    # NOW + 3600 waits, and the first one after the limit frees up at NOW + 7200 is next.
    save_health(store, schedule, retry_at=NOW + 7200, retry_for=None, retry_reason="limit")
    assert next_routine(store, owl) == NOW + 10800

    # A run held by a limit, to be replayed once it frees up, is due then.
    save_health(store, schedule, retry_at=NOW + 7200, retry_for=NOW, retry_reason="limit")
    assert next_routine(store, owl) == NOW + 7200


def test_a_routine_that_ended_counts_only_for_its_retry(store):
    owl = store.save_bot("alice", BotInput(name="Δήμητρα", purpose="Υπενθυμίσεις"))
    once = store.save_schedule(
        "alice",
        owl["id"],
        routine("Μία φορά", {"kind": "once", "at": "2026-11-18T08:10:00+00:00"}),
        now=NOW,
    )
    assert next_routine(store, owl) == NOW + 600
    # Its one run happened and failed: nothing more is scheduled, but its retry will run.
    with store.connect(write=True) as db:
        db.execute("UPDATE schedules SET state='ended',next_run_at=NULL WHERE id=?", (once["id"],))
    assert next_routine(store, owl) is None
    save_health(store, once, retry_at=NOW + 900, retry_for=NOW + 600, retry_reason="temporary")
    assert next_routine(store, owl) == NOW + 900
