"""Monthly and daily-window routines, their next runs, and time zone changes."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from alysis_code.bots.models import BotInput
from alysis_code.bots.runtime import TOOL_DEFINITIONS, tool_schema
from alysis_code.bots.scheduling import (
    WINDOW_MIN_MINUTES,
    ScheduleInput,
    ScheduleRequest,
    latest_run,
    next_run,
    upcoming_runs,
)
from alysis_code.bots.store import Store


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def timing(value):
    return ScheduleInput.model_validate({"name": "R", "prompt": "P", "timing": value}).timing


def monthly(day, at="09:00", zone="Europe/Athens"):
    return timing({"kind": "monthly", "day": day, "time": at, "timezone": zone})


def window(start="09:00", end="17:00", every=30, days=(0, 1, 2, 3, 4), zone="Europe/Athens"):
    return timing(
        {
            "kind": "window",
            "every_minutes": every,
            "start": start,
            "end": end,
            "weekdays": list(days),
            "timezone": zone,
        }
    )


@pytest.mark.parametrize(
    "rule,after,expected",
    [
        # The 31st runs on the last day of a shorter month, then on the 31st again.
        (monthly(31), "2026-04-01T00:00:00+00:00", "2026-04-30T09:00:00+03:00"),
        (monthly(31), "2026-04-30T06:00:00+00:00", "2026-05-31T09:00:00+03:00"),
        (monthly(30, zone="America/New_York"), "2027-02-01T00:00:00Z", "2027-02-28T09:00-05:00"),
        (monthly(30, zone="America/New_York"), "2028-02-01T00:00:00Z", "2028-02-29T09:00-05:00"),
        (monthly(15, "10:00", "UTC"), "2026-12-20T00:00:00+00:00", "2027-01-15T10:00:00+00:00"),
        # A time the clocks skip runs once they have moved, so the month is not lost.
        (monthly(29, "03:30"), "2026-03-01T00:00:00+00:00", "2026-03-29T01:30:00+00:00"),
        (
            monthly(8, "02:30", "America/New_York"),
            "2026-03-01T00:00:00+00:00",
            "2026-03-08T07:30:00+00:00",
        ),
        # A repeated time runs once, at its first occurrence.
        (
            monthly(1, "01:30", "America/New_York"),
            "2026-10-15T00:00:00+00:00",
            "2026-11-01T05:30:00+00:00",
        ),
        (
            monthly(1, "01:30", "America/New_York"),
            "2026-11-01T05:30:00+00:00",
            "2026-12-01T06:30:00+00:00",
        ),
        # Every 30 minutes, Monday to Friday, 09:00-17:00 in Athens.
        (window(), "2026-11-20T16:45:00+02:00", "2026-11-20T17:00:00+02:00"),
        (window(), "2026-11-20T17:00:00+02:00", "2026-11-23T09:00:00+02:00"),
        (window(), "2026-11-21T12:00:00+02:00", "2026-11-23T09:00:00+02:00"),
        (window(), "2026-11-23T08:00:00+02:00", "2026-11-23T09:00:00+02:00"),
        (window(), "2026-11-23T09:10:00+02:00", "2026-11-23T09:30:00+02:00"),
        (window(every=120), "2026-11-23T15:00:00+02:00", "2026-11-23T17:00:00+02:00"),
        (window(every=180), "2026-11-23T15:00:00+02:00", "2026-11-24T09:00:00+02:00"),
    ],
)
def test_monthly_and_window_occurrences(rule, after, expected):
    assert next_run(rule, stamp(after)) == stamp(expected)


def test_a_window_across_the_autumn_clock_change_runs_each_time_once():
    # Athens, Sunday 25 October 2026: 04:00 summer time becomes 03:00, so 03:00 happens twice.
    rule = window("02:00", "05:00", 60, days=(6,))
    first = next_run(rule, stamp("2026-10-24T12:00:00+00:00"))
    assert upcoming_runs(rule, first, 4) == [
        stamp("2026-10-25T02:00:00+03:00"),
        stamp("2026-10-25T03:00:00+03:00"),
        stamp("2026-10-25T04:00:00+02:00"),
        stamp("2026-10-25T05:00:00+02:00"),
    ]


def test_a_window_across_the_spring_clock_change_skips_the_missing_hour():
    # New York, Sunday 8 March 2026: 02:00 becomes 03:00.
    rule = window("01:00", "04:00", 60, days=(6,), zone="America/New_York")
    first = next_run(rule, stamp("2026-03-07T12:00:00+00:00"))
    assert upcoming_runs(rule, first, 4) == [
        stamp("2026-03-08T01:00:00-05:00"),
        stamp("2026-03-08T03:00:00-04:00"),
        stamp("2026-03-08T04:00:00-04:00"),
        stamp("2026-03-15T01:00:00-04:00"),
    ]


def test_next_runs_keep_the_routines_own_wall_clock_across_a_clock_change():
    daily = timing({"kind": "daily", "time": "09:00", "timezone": "Europe/Athens"})
    first = stamp("2026-10-24T09:00:00+03:00")
    assert upcoming_runs(daily, first) == [
        first,
        stamp("2026-10-25T09:00:00+02:00"),
        stamp("2026-10-26T09:00:00+02:00"),
    ]
    interval = timing({"kind": "interval", "every_minutes": 90})
    assert upcoming_runs(interval, 1000) == [1000, 6400, 11800]
    once = timing({"kind": "once", "at": "2026-11-20T09:00:00+02:00"})
    assert upcoming_runs(once, stamp("2026-11-20T09:00:00+02:00")) == [
        stamp("2026-11-20T09:00:00+02:00")
    ]
    assert upcoming_runs(daily, None) == []


def test_a_late_window_routine_catches_up_with_its_latest_run():
    rule = window()
    due = stamp("2026-11-23T09:00:00+02:00")
    now = stamp("2026-11-23T11:10:00+02:00")
    assert latest_run(rule, due, now, within=12 * 3600) == stamp("2026-11-23T11:00:00+02:00")


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "monthly", "day": 0, "time": "09:00", "timezone": "UTC"},
        {"kind": "monthly", "day": 32, "time": "09:00", "timezone": "UTC"},
        {"kind": "monthly", "day": 1, "time": "24:00", "timezone": "UTC"},
        {"kind": "monthly", "day": 1, "time": "09:00", "timezone": "Mars/Base"},
        {"kind": "monthly", "day": 1, "time": "09:00"},
        {"kind": "monthly", "day": 1, "time": "09:00", "timezone": "UTC", "weekdays": [0]},
        {
            "kind": "window",
            "every_minutes": WINDOW_MIN_MINUTES - 1,
            "start": "09:00",
            "end": "17:00",
            "weekdays": [0],
            "timezone": "UTC",
        },
        {
            "kind": "window",
            "every_minutes": 30,
            "start": "17:00",
            "end": "09:00",
            "weekdays": [0],
            "timezone": "UTC",
        },
        {
            "kind": "window",
            "every_minutes": 30,
            "start": "09:00",
            "end": "09:00",
            "weekdays": [0],
            "timezone": "UTC",
        },
        {
            "kind": "window",
            "every_minutes": 30,
            "start": "09:00",
            "end": "17:00",
            "weekdays": [0, 0],
            "timezone": "UTC",
        },
        {
            "kind": "window",
            "every_minutes": 30,
            "start": "09:00",
            "end": "17:00",
            "weekdays": [],
            "timezone": "UTC",
        },
        {
            "kind": "window",
            "every_minutes": 30,
            "start": "09:00",
            "end": "17:00",
            "timezone": "UTC",
        },
    ],
)
def test_unclear_monthly_and_window_timings_are_refused(value):
    with pytest.raises(ValidationError):
        timing(value)


def test_saved_monthly_and_window_routines_list_their_next_three_runs(tmp_path):
    store = Store(tmp_path / "timings.db")
    bot = store.save_bot("alice", BotInput(name="Ελένη", purpose="Μηνιαία αναφορά"))["id"]
    now = stamp("2026-01-15T00:00:00+00:00")
    store.save_schedule(
        "alice",
        bot,
        ScheduleInput.model_validate(
            {
                "name": "Μηνιαία",
                "prompt": "Σύνοψη του μήνα.",
                "timing": {"kind": "monthly", "day": 31, "time": "08:00", "timezone": "UTC"},
            }
        ),
        now=now,
    )
    store.save_schedule(
        "alice",
        bot,
        ScheduleInput.model_validate(
            {
                "name": "Ωριαία",
                "prompt": "Έλεγχος.",
                "timing": {
                    "kind": "window",
                    "every_minutes": 240,
                    "start": "09:00",
                    "end": "17:00",
                    "weekdays": [3],
                    "timezone": "Asia/Tokyo",
                },
            }
        ),
        now=now,
    )
    listed = {item["timing"]["kind"]: item for item in store.list_schedules("alice", bot)}
    saved_monthly, saved_window = listed["monthly"], listed["window"]
    assert saved_monthly["timing"] == {
        "kind": "monthly",
        "day": 31,
        "time": "08:00",
        "timezone": "UTC",
    }
    assert saved_monthly["upcoming_runs"] == [
        stamp("2026-01-31T08:00:00+00:00"),
        stamp("2026-02-28T08:00:00+00:00"),
        stamp("2026-03-31T08:00:00+00:00"),
    ]
    # It is 09:00 on Thursday 15 January in Tokyo: that run is now, the next ones follow.
    assert saved_window["upcoming_runs"] == [
        stamp("2026-01-15T13:00:00+09:00"),
        stamp("2026-01-15T17:00:00+09:00"),
        stamp("2026-01-22T09:00:00+09:00"),
    ]


def test_the_owl_can_set_up_monthly_and_window_routines_with_a_provider_safe_schema():
    for name in ("create_schedule", "edit_schedule", "propose_schedule"):
        parameters = tool_schema(name)["function"]["parameters"]
        assert parameters["type"] == "object"
        assert not {"anyOf", "oneOf", "allOf"} & set(parameters)
        text = str(parameters)
        assert "'monthly'" in text and "'window'" in text
        assert "Monday=0" in text and "last day" in text
    assert "create_schedule" in TOOL_DEFINITIONS
    request = ScheduleRequest.model_validate(
        {
            "name": "Ώρες γραφείου",
            "prompt": "Έλεγξε τα νέα μηνύματα.",
            "timing": {
                "kind": "window",
                "every_minutes": 30,
                "start": "09:00",
                "end": "17:00",
                "weekdays": [0, 1, 2, 3, 4],
                "timezone": "Europe/Athens",
            },
        }
    )
    assert request.absolute_timing(0).kind == "window"
