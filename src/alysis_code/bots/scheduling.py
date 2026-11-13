"""Explicit schedules and civil-time calculation, independent of model wording."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_serializer, model_validator

from .models import Input


class OnceTiming(Input):
    kind: Literal["once"]
    at: AwareDatetime


class IntervalTiming(Input):
    kind: Literal["interval"]
    every_minutes: int = Field(ge=5, le=10080)


class CalendarTiming(Input):
    kind: Literal["daily", "weekly"]
    time: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(min_length=1, max_length=100)
    weekday: int | None = Field(default=None, ge=0, le=6)
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(
        default=None,
        min_length=1,
        max_length=7,
        description="Days a weekly schedule runs, Monday=0 to Sunday=6: [0, 1, 2, 3, 4] for every weekday.",
    )

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a valid IANA timezone, such as Europe/Athens.") from None
        return value

    @model_validator(mode="after")
    def weekday_matches_kind(self):
        if self.weekday is not None and self.weekdays is not None:
            raise ValueError("Give weekdays as one list, Monday=0.")
        if self.weekdays is not None and len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("List each weekday once.")
        if (self.kind == "weekly") != bool(self.days):
            raise ValueError(
                "Only weekly schedules take weekdays: a list such as [0, 1, 2, 3, 4] for every weekday (Monday=0)."
            )
        return self

    @model_serializer(mode="wrap")
    def _without_unset_weekdays(self, handler):
        # Routines saved before multi-day weeks have no weekdays key: keep it that way,
        # so a stored routine reads the same and an earlier engine can still load it.
        data = handler(self)
        if self.weekdays is None:
            data.pop("weekdays", None)
        return data

    @property
    def days(self) -> tuple[int, ...]:
        """The weekdays a weekly schedule runs; one saved weekday is a list of one."""
        if self.weekdays is not None:
            return tuple(sorted(self.weekdays))
        return () if self.weekday is None else (self.weekday,)


Timing = Annotated[OnceTiming | IntervalTiming | CalendarTiming, Field(discriminator="kind")]


class ScheduleProposal(Input):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=8000)
    timing: Timing = Field(
        description="A JSON object matching one timing variant, not a JSON-encoded string.",
        json_schema_extra={"type": "object"},
    )


class ScheduleInput(ScheduleProposal):
    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    enabled: bool = True
    expected_revision: int = Field(default=0, ge=0)


class DelayTiming(Input):
    kind: Literal["after"]
    delay_seconds: int = Field(
        ge=1,
        le=31536000,
        description="The user's requested duration expressed in seconds. The service adds it to the original message time; do not compute a clock timestamp.",
    )


class ScheduleRequest(ScheduleProposal):
    """Tool input; relative requests become fixed deadlines before persistence."""

    timing: Annotated[
        DelayTiming | OnceTiming | IntervalTiming | CalendarTiming,
        Field(discriminator="kind"),
    ] = Field(
        description="For a one-time relative delay use kind=after and delay_seconds; the service calculates the deadline from the user's message time. For a specific clock time use kind=once and at with an explicit offset. Other variants recur. Supply a JSON object, not a JSON-encoded string.",
        json_schema_extra={"type": "object"},
    )

    def absolute_timing(self, requested_at: float) -> Timing:
        if isinstance(self.timing, DelayTiming):
            return OnceTiming(
                kind="once",
                at=datetime.fromtimestamp(requested_at + self.timing.delay_seconds, UTC),
            )
        return self.timing


class ScheduleEdit(ScheduleRequest):
    schedule_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    enabled: bool


class ScheduleControl(Input):
    schedule_id: str = Field(min_length=1, max_length=100)
    state: Literal["active", "paused"]
    expected_revision: int = Field(ge=1)


class ScheduleDecision(Input):
    state: Literal["active", "paused"]
    expected_revision: int = Field(ge=1)


class ScheduleRunsQuery(Input):
    before: int | None = Field(default=None, ge=1)


def next_run(timing: Timing, after: float) -> float | None:
    """First occurrence strictly after UTC timestamp.

    Nonexistent civil times are skipped. Ambiguous times run on the first fold
    only, so a daily schedule never executes twice during a clock rollback.
    """
    if isinstance(timing, OnceTiming):
        stamp = timing.at.timestamp()
        return stamp if stamp > after else None
    if isinstance(timing, IntervalTiming):
        return after + timing.every_minutes * 60
    zone = ZoneInfo(timing.timezone)
    local = datetime.fromtimestamp(after, UTC).astimezone(zone)
    hour, minute = map(int, timing.time.split(":"))
    # A weekly occurrence may be skipped at the daylight-saving transition.
    for offset in range(15):
        day = local.date() + timedelta(days=offset)
        if timing.kind == "weekly" and day.weekday() not in timing.days:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        stamp = candidate.timestamp()
        roundtrip = datetime.fromtimestamp(stamp, UTC).astimezone(zone)
        if roundtrip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
            continue
        if stamp > after:
            return stamp
    raise ValueError("No valid occurrence found in the next two weeks.")


def advance_run(timing: Timing, due: float, now: float) -> float | None:
    if isinstance(timing, IntervalTiming):
        seconds = timing.every_minutes * 60
        return due + (int((now - due) // seconds) + 1) * seconds
    return next_run(timing, now)
