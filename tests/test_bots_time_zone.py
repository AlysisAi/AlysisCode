"""Owls read dates and clock times in the person's time zone; real store and runtime, no model."""

from __future__ import annotations

import json
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from bots_task_state import task_state
from packaging.requirements import Requirement
from pydantic import ValidationError

from alysis_code.bots.cooperation import PeerRequest, PeerStore
from alysis_code.bots.models import BotInput, TaskInput, TimeZoneSettings
from alysis_code.bots.runtime import BotRuntime, tool_schema
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, Store
from alysis_code.bots.time_zone import local_time, time_context


def stamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize(
    "zone,moment,expected",
    [
        # Across midnight, in two zones on opposite sides of the date line.
        (
            "Pacific/Auckland",
            "2026-11-17T10:30:00+00:00",
            "Tuesday 17 November 2026, 23:30 (Pacific/Auckland, UTC+13:00)",
        ),
        (
            "Pacific/Auckland",
            "2026-11-17T11:30:00+00:00",
            "Wednesday 18 November 2026, 00:30 (Pacific/Auckland, UTC+13:00)",
        ),
        (
            "America/New_York",
            "2026-11-18T04:30:00+00:00",
            "Tuesday 17 November 2026, 23:30 (America/New_York, UTC-05:00)",
        ),
        (
            "America/New_York",
            "2026-11-18T05:30:00+00:00",
            "Wednesday 18 November 2026, 00:30 (America/New_York, UTC-05:00)",
        ),
        # Daylight saving time starts: the clock skips an hour.
        (
            "Pacific/Auckland",
            "2026-09-26T13:30:00+00:00",
            "Sunday 27 September 2026, 01:30 (Pacific/Auckland, UTC+12:00)",
        ),
        (
            "Pacific/Auckland",
            "2026-09-26T14:30:00+00:00",
            "Sunday 27 September 2026, 03:30 (Pacific/Auckland, UTC+13:00)",
        ),
        (
            "America/New_York",
            "2026-03-08T06:30:00+00:00",
            "Sunday 8 March 2026, 01:30 (America/New_York, UTC-05:00)",
        ),
        (
            "America/New_York",
            "2026-03-08T07:30:00+00:00",
            "Sunday 8 March 2026, 03:30 (America/New_York, UTC-04:00)",
        ),
        # Daylight saving time ends: the same clock time twice, told apart by its offset.
        (
            "America/New_York",
            "2026-11-01T05:30:00+00:00",
            "Sunday 1 November 2026, 01:30 (America/New_York, UTC-04:00)",
        ),
        (
            "America/New_York",
            "2026-11-01T06:30:00+00:00",
            "Sunday 1 November 2026, 01:30 (America/New_York, UTC-05:00)",
        ),
        # Offsets that are not whole hours, east and west.
        (
            "Asia/Kathmandu",
            "2026-11-17T18:15:00+00:00",
            "Wednesday 18 November 2026, 00:00 (Asia/Kathmandu, UTC+05:45)",
        ),
        (
            "America/St_Johns",
            "2026-11-18T03:00:00+00:00",
            "Tuesday 17 November 2026, 23:30 (America/St_Johns, UTC-03:30)",
        ),
    ],
)
def test_local_date_weekday_and_offset_follow_the_zone(zone, moment, expected):
    assert local_time(stamp(moment), zone) == expected
    assert time_context(stamp(moment), zone).startswith(
        "Now for the person, when this task started: " + expected + "."
    )


@pytest.mark.parametrize("zone", [None, "", "Mars/Olympus_Mons", "../../etc/passwd", "localtime"])
def test_without_a_usable_zone_the_owl_is_told_it_is_utc(zone):
    text = time_context(stamp("2026-11-17T18:55:00+00:00"), zone)
    assert text.startswith(
        "Now in UTC, when this task started: Tuesday 17 November 2026, 18:55 (UTC)."
    )
    assert "time zone is not known" in text and "Now for the person" not in text


@pytest.mark.parametrize(
    "zone",
    ["Europe/Athens", "Pacific/Auckland", "America/Argentina/Buenos_Aires", "Asia/Tokyo", "UTC"],
)
def test_iana_zones_are_accepted(zone):
    assert TimeZoneSettings(time_zone=zone).time_zone == zone


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"time_zone": ""},
        {"time_zone": "Mars/Olympus_Mons"},
        {"time_zone": "Europe Athens"},
        {"time_zone": "../../etc/passwd"},
        {"time_zone": "/etc/localtime"},
        {"time_zone": "localtime"},
        {"time_zone": "Ευρώπη/Αθήνα"},
        {"time_zone": "Europe/" + "A" * 120},
        {"time_zone": 3},
        {"time_zone": "Europe/Athens", "owner": "other"},
    ],
)
def test_other_values_are_refused(body):
    with pytest.raises(ValidationError):
        TimeZoneSettings.model_validate(body)


def test_setting_is_saved_per_owner_and_survives_reopening(tmp_path):
    store = Store(tmp_path / "bots.db")
    assert store.time_zone_settings("owner") == {"time_zone": None}
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Europe/Athens"))
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="America/New_York"))
    store.time_zone_settings("other", TimeZoneSettings(time_zone="Asia/Tokyo"))
    reopened = Store(tmp_path / "bots.db")
    assert reopened.time_zone_settings("owner") == {"time_zone": "America/New_York"}
    assert reopened.time_zone("other") == "Asia/Tokyo"
    assert reopened.time_zone("nobody") is None


def test_a_database_from_before_the_setting_opens_without_a_zone(tmp_path):
    store = Store(tmp_path / "bots.db")
    with store.connect(write=True) as db:
        db.execute("DROP TABLE bot_time_zone_settings")
        db.execute("PRAGMA user_version=40")
    store = Store(tmp_path / "bots.db")
    assert store.time_zone("owner") is None
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Pacific/Auckland"))
    assert store.time_zone("owner") == "Pacific/Auckland"


@pytest.fixture
def chat(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Ήλιος", purpose="Plan the day"))
    return store, bot, store.create_chat("owner", bot["id"], "Chat")


def claim(store, chat, message="Τι μέρα είναι αύριο;"):
    store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=uuid4().hex))
    return store.claim("owner")


def test_each_turn_states_the_persons_current_zone_and_keeps_utc(chat):
    store, _, conversation = chat
    runtime = BotRuntime(store, "owner", lambda _: None)
    task = dict(claim(store, conversation), updated_at=stamp("2026-11-17T18:55:00+00:00"))

    unknown = runtime._context(task)
    state = task_state(unknown)
    assert "Task start UTC time: 2026-11-17T18:55:00+00:00" in state
    assert "Tuesday 17 November 2026, 18:55 (UTC)" in state
    assert "time zone is not known" in state

    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Europe/Athens"))
    athens = runtime._context(task)
    state = task_state(athens)
    assert "Task start UTC time: 2026-11-17T18:55:00+00:00" in state
    assert (
        "Now for the person, when this task started: "
        "Tuesday 17 November 2026, 20:55 (Europe/Athens, UTC+02:00)." in state
    )

    # The person travels: their app sends the new zone and the next turn reads it.
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Pacific/Auckland"))
    state = task_state(runtime._context(task))
    assert "Wednesday 18 November 2026, 07:55 (Pacific/Auckland, UTC+13:00)" in state
    assert "Europe/Athens" not in state

    # The instructions before the conversation stay the same for every zone, so a provider can
    # keep caching them.
    assert athens[0]["content"] == unknown[0]["content"]
    assert "Europe/Athens" not in athens[0]["content"]


def test_a_routine_run_reads_today_on_its_own_clock(chat):
    store, bot, _ = chat
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Europe/Athens"))
    due = stamp("2026-11-17T14:00:00+00:00")
    store.save_schedule(
        "owner",
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινή σύνοψη",
                "prompt": "Summarise today's plan.",
                "timing": {"kind": "daily", "time": "09:00", "timezone": "America/New_York"},
            }
        ),
        now=due - 3600,
    )
    assert store.enqueue_due_schedules("owner", max_calls=10, now=due) == 1
    run = dict(store.claim("owner"), updated_at=due)
    assert store.routine_time_zone("owner", run["id"]) == "America/New_York"
    state = task_state(BotRuntime(store, "owner", lambda _: None)._context(run))
    assert (
        "Now in this routine's time zone, when this run started: "
        "Tuesday 17 November 2026, 09:00 (America/New_York, UTC-05:00)." in state
    )
    assert (
        "Now for the person, in their own time zone: "
        "Tuesday 17 November 2026, 16:00 (Europe/Athens, UTC+02:00)." in state
    )


def test_a_routine_without_its_own_zone_uses_the_persons(chat):
    store, bot, _ = chat
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Asia/Tokyo"))
    store.save_schedule(
        "owner",
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Check",
                "prompt": "Check the notes.",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("owner", max_calls=10, now=4600) == 1
    run = dict(store.claim("owner"), updated_at=stamp("2026-11-17T15:30:00+00:00"))
    assert store.routine_time_zone("owner", run["id"]) is None
    state = task_state(BotRuntime(store, "owner", lambda _: None)._context(run))
    assert "Wednesday 18 November 2026, 00:30 (Asia/Tokyo, UTC+09:00)" in state
    assert "routine's time zone" not in state


def schedule_args(**timing):
    return {"name": "Morning brief", "prompt": "Σύνοψη ειδήσεων", "timing": timing}


def test_a_routine_without_a_named_zone_follows_the_person(chat):
    store, bot, conversation = chat
    runtime = BotRuntime(store, "owner", lambda _: None)
    tools = ["create_schedule", "edit_schedule", "propose_schedule"]

    def request(message, arguments, tool="create_schedule"):
        task = claim(store, conversation, message)
        try:
            return runtime._tool(task, tool, arguments, tools)
        finally:
            store.finish("owner", task["id"], "completed", content="Saved")

    with pytest.raises(Conflict, match="time zone is not known"):
        request(
            "Κάθε πρωί στις 9 στείλε μου σύνοψη",
            schedule_args(kind="daily", time="09:00"),
        )

    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Asia/Tokyo"))
    created = request(
        "Κάθε πρωί στις 9 στείλε μου σύνοψη", schedule_args(kind="daily", time="09:00")
    )
    assert created["timing"]["timezone"] == "Asia/Tokyo"
    named = request(
        "Every evening at 6, New York time",
        schedule_args(kind="daily", time="18:00", timezone="America/New_York")
        | {"name": "Evening brief"},
    )
    assert named["timing"]["timezone"] == "America/New_York"
    proposed = request(
        "Τι θα πρότεινες;",
        schedule_args(kind="weekly", time="08:30", weekdays=[0, 2]) | {"name": "Weekly"},
        tool="propose_schedule",
    )
    saved = {item["id"]: item for item in store.list_schedules("owner", bot["id"])}
    assert saved[proposed["id"]]["timing"]["timezone"] == "Asia/Tokyo"
    # Timings that carry no zone are left as they are. A one-time routine must lie ahead.
    ahead = (datetime.now(timezone(timedelta(hours=2))) + timedelta(days=30)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    once = request(
        "Μία φορά, τον άλλο μήνα στις 9",
        schedule_args(kind="once", at=ahead.isoformat()) | {"name": "Once"},
    )
    assert "timezone" not in once["timing"]

    # The person moves; an edit that names no zone keeps the routine's own.
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Europe/Athens"))
    edited = request(
        "Κάνε το στις 10",
        schedule_args(kind="daily", time="10:00", timezone=None)
        | {"schedule_id": created["id"], "expected_revision": created["revision"], "enabled": True},
        tool="edit_schedule",
    )
    assert (edited["timing"]["time"], edited["timing"]["timezone"]) == ("10:00", "Asia/Tokyo")
    # A routine that had no zone before takes the person's current one.
    daily = request(
        "Κάθε μέρα στις 7 αντί γι' αυτό",
        schedule_args(kind="daily", time="07:00")
        | {
            "name": "Once",
            "schedule_id": once["id"],
            "expected_revision": once["revision"],
            "enabled": True,
        },
        tool="edit_schedule",
    )
    assert (daily["timing"]["kind"], daily["timing"]["timezone"]) == ("daily", "Europe/Athens")


@pytest.mark.parametrize("name", ["create_schedule", "edit_schedule", "propose_schedule"])
def test_tool_schema_lets_the_zone_be_left_out_and_says_whose_it_is(chat, name):
    store, _, _ = chat
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Asia/Tokyo"))
    schema = tool_schema(name)["function"]
    zoned = [
        definition
        for definition in schema["parameters"]["$defs"].values()
        if "timezone" in definition.get("properties", {})
    ]
    assert zoned and all("timezone" not in d.get("required", []) for d in zoned)
    assert "person's own time zone" in schema["description"]
    if name == "edit_schedule":
        assert "keeps its current time zone" in schema["description"]
    # The same for every person, so it stays cacheable.
    assert "Asia/Tokyo" not in json.dumps(schema)


def test_an_owl_answering_another_owl_knows_the_persons_date(tmp_path):
    store = Store(tmp_path / "peers.db")
    a = store.save_bot("owner", BotInput(name="Coordinator", purpose="Own the result"))
    b = store.save_bot("owner", BotInput(name="Νεφέλη", purpose="Review the dates"))
    conversation = store.create_chat("owner", a["id"], "Chat")
    task = claim(store, conversation, "Ρώτα τη Νεφέλη αν η Παρασκευή είναι ελεύθερη.")
    peers = PeerStore(store, "owner")
    item = peers.submit(task, PeerRequest(recipient_bot_id=b["id"], brief="Is Friday free?"), {}, 8)
    system = peers.context(item["id"])[1][0]["content"]
    assert "time zone is not known" in system
    store.time_zone_settings("owner", TimeZoneSettings(time_zone="Pacific/Auckland"))
    exchange, messages, _ = peers.context(item["id"])
    system = messages[0]["content"]
    assert "Now for the person, when this task started: " in system
    assert local_time(exchange["created_at"], "Pacific/Auckland") in system


def test_windows_installs_carry_the_time_zone_database():
    # zoneinfo reads the system's time zone database, and Windows has none: without the tzdata
    # package every zone is refused there and owls fall back to UTC.
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    found = [
        requirement
        for requirement in map(Requirement, project["dependencies"])
        if requirement.name == "tzdata"
    ]
    assert len(found) == 1 and found[0].marker is not None
    assert found[0].marker.evaluate({"sys_platform": "win32"})
    assert not found[0].marker.evaluate({"sys_platform": "linux"})
