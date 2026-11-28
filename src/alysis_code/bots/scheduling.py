"""Explicit schedules and civil-time calculation, independent of model wording."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, Field, model_serializer, model_validator
from pydantic.json_schema import SkipJsonSchema

from .models import Input
from .routine_access import RoutineNeeds

CLOCK_TIME = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
# A routine that repeats inside a daily window starts at most this often, so a short
# interval cannot spend a plan's usage on runs nobody reads.
WINDOW_MIN_MINUTES = 15


def valid_timezone(value: str) -> str:
    """The one check every routine's time zone passes: an IANA name, such as Europe/Athens."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use a valid IANA timezone, such as Europe/Athens.") from None
    return value


TimeZoneName = Annotated[
    str,
    Field(min_length=1, max_length=100, description="IANA time zone, such as Europe/Athens."),
    AfterValidator(valid_timezone),
]


def _minutes(clock: str) -> int:
    hour, minute = map(int, clock.split(":"))
    return hour * 60 + minute


class OnceTiming(Input):
    kind: Literal["once"]
    at: AwareDatetime


class IntervalTiming(Input):
    kind: Literal["interval"]
    every_minutes: int = Field(ge=5, le=10080)


class CalendarTiming(Input):
    kind: Literal["daily", "weekly"]
    time: str = Field(pattern=CLOCK_TIME)
    timezone: TimeZoneName
    # One weekday, as routines made before multi-day weeks were saved; tools and apps give
    # weekdays, so it is accepted but not offered.
    weekday: SkipJsonSchema[int | None] = Field(default=None, ge=0, le=6)
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(
        default=None,
        min_length=1,
        max_length=7,
        description="Days a weekly schedule runs, Monday=0 to Sunday=6: [0, 1, 2, 3, 4] for every weekday.",
    )

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


class MonthlyTiming(Input):
    kind: Literal["monthly"]
    day: int = Field(
        ge=1,
        le=31,
        description="Day of the month, 1 to 31; a shorter month runs on its last day (31: on 30 April, and 28 or 29 February).",
    )
    time: str = Field(pattern=CLOCK_TIME)
    timezone: TimeZoneName


class WindowTiming(Input):
    kind: Literal["window"]
    every_minutes: int = Field(
        ge=WINDOW_MIN_MINUTES,
        le=720,
        description=f"Minutes between runs inside the window, at least {WINDOW_MIN_MINUTES}: 30 for every half hour, 120 for every two hours.",
    )
    start: str = Field(pattern=CLOCK_TIME, description="The first run of each chosen day, HH:MM.")
    end: str = Field(
        pattern=CLOCK_TIME,
        description="The latest a run may start that day, HH:MM, after start: 09:00 to 17:00 every 30 minutes runs at 09:00, 09:30 and so on through 17:00.",
    )
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(
        min_length=1,
        max_length=7,
        description="Days it runs, Monday=0 to Sunday=6: [0, 1, 2, 3, 4] for every weekday.",
    )
    timezone: TimeZoneName

    @model_validator(mode="after")
    def window_within_a_day(self):
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("List each weekday once.")
        if _minutes(self.end) <= _minutes(self.start):
            raise ValueError("The window must end later on the same day than it starts.")
        return self

    @property
    def days(self) -> tuple[int, ...]:
        return tuple(sorted(self.weekdays))

    def clock_minutes(self) -> range:
        """Each run's wall-clock time on a chosen day, in minutes after midnight."""
        return range(_minutes(self.start), _minutes(self.end) + 1, self.every_minutes)


Timing = Annotated[
    OnceTiming | IntervalTiming | CalendarTiming | MonthlyTiming | WindowTiming,
    Field(discriminator="kind"),
]


class ScheduleProposal(Input):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=8000)
    timing: Timing = Field(
        description="A JSON object matching one timing variant, not a JSON-encoded string.",
        json_schema_extra={"type": "object"},
    )
    # Left out: a new routine needs nothing beyond chat, and an edit keeps what it had.
    needs: RoutineNeeds | None = Field(
        default=None,
        description="What each run needs beyond chat: the owl's offline computer, its online browser, or exact website origins to act on. The person allows these once for this routine; runs get only that, and a run needing more pauses the routine until they allow it. Leave out when runs only reply, read files or memory, or use web_search/web_fetch, and in an edit to keep its current needs.",
    )


# The model a routine's runs think with (capability "routine_models"): the id of one of the models
# the owl's place offers (GET /v1/models), bounded like the owl's own (models.BotInput.model).
RoutineModelId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")]

ROUTINE_MODEL_DESCRIPTION = (
    "The model each run thinks with, only when the person asks for one: an id from "
    "models.offered in list_schedules. Leave it out for your own model (in an edit, to keep the "
    "routine's); null sets an edited routine back to your own."
)


class ScheduleInput(ScheduleProposal):
    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    enabled: bool = True
    expected_revision: int = Field(default=0, ge=0)
    # The model its runs think with. Null: the owl's own, as for routines made before. Left out
    # of an edit, it stays as it was.
    model: RoutineModelId | None = None


class ScheduleModelChoice(Input):
    """The person picks the model a routine's runs think with; null: the owl's own."""

    model: RoutineModelId | None
    expected_revision: int = Field(ge=1)


class AssignmentScheduleInput(Input):
    name: str = Field(min_length=1, max_length=80)
    timing: Timing
    request_id: str = Field(min_length=8, max_length=100)


class DelayTiming(Input):
    kind: Literal["after"]
    delay_seconds: int = Field(
        ge=1,
        le=31536000,
        description="The requested duration in seconds. The service adds it to the message's time; never work out a clock time.",
    )


class ScheduleRequest(ScheduleProposal):
    """Tool input; relative requests become fixed deadlines before persistence."""

    timing: Annotated[
        DelayTiming | OnceTiming | IntervalTiming | CalendarTiming | MonthlyTiming | WindowTiming,
        Field(discriminator="kind"),
    ] = Field(
        description="A JSON object, not a JSON-encoded string. Once after a delay: kind=after with delay_seconds (the service counts from the message's time). Once at a stated time: kind=once, at with its UTC offset. Recurring: interval (every N minutes around the clock), daily, weekly, monthly (a day of the month) or window (every N minutes from start to end on chosen weekdays, such as every 30 minutes 09:00-17:00 Monday to Friday).",
        json_schema_extra={"type": "object"},
    )

    def absolute_timing(self, requested_at: float) -> Timing:
        if isinstance(self.timing, DelayTiming):
            return OnceTiming(
                kind="once",
                at=datetime.fromtimestamp(requested_at + self.timing.delay_seconds, UTC),
            )
        return self.timing


class RoutineRequest(ScheduleRequest):
    """create_schedule and propose_schedule: a routine, and the model its runs think with."""

    model: RoutineModelId | None = Field(default=None, description=ROUTINE_MODEL_DESCRIPTION)


class ScheduleEdit(RoutineRequest):
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


class ScheduleRunNow(Input):
    """Run a routine once now; the same request_id never starts a second run."""

    request_id: str = Field(min_length=8, max_length=100)


class NothingToReport(Input):
    """A routine's run ends without a reply: it checked and found nothing to tell the person."""

    reason: str = Field(min_length=1, max_length=300)


class NothingNew(Exception):
    """Raised by the nothing_to_report step that ends a routine's run quietly (BotRuntime)."""

    def __init__(self, reason: str):
        super().__init__("This routine run found nothing new to report.")
        self.reason = reason


def _civil(day: date, minutes: int, zone: ZoneInfo, *, shift_gap: bool = False) -> float | None:
    """The instant a wall-clock time happens on a day in a zone.

    A time the clocks skip that day is None, or with shift_gap the instant it lands on
    once the clocks have moved (02:30 becomes 03:30). A repeated time is its first one.
    """
    candidate = datetime(
        day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=zone, fold=0
    )
    stamp = candidate.timestamp()
    if shift_gap:
        return stamp
    roundtrip = datetime.fromtimestamp(stamp, UTC).astimezone(zone)
    return stamp if roundtrip.replace(tzinfo=None) == candidate.replace(tzinfo=None) else None


def next_run(timing: Timing, after: float) -> float | None:
    """First occurrence strictly after UTC timestamp.

    Nonexistent civil times are skipped for daily, weekly and window routines; a monthly
    routine runs once the clocks have moved instead, so its month is not lost. Ambiguous
    times run on the first fold only, so a routine never executes twice during a clock
    rollback.
    """
    if isinstance(timing, OnceTiming):
        stamp = timing.at.timestamp()
        return stamp if stamp > after else None
    if isinstance(timing, IntervalTiming):
        return after + timing.every_minutes * 60
    zone = ZoneInfo(timing.timezone)
    local = datetime.fromtimestamp(after, UTC).astimezone(zone)
    if isinstance(timing, MonthlyTiming):
        year, month = local.year, local.month
        for _ in range(14):
            day = date(year, month, min(timing.day, calendar.monthrange(year, month)[1]))
            stamp = _civil(day, _minutes(timing.time), zone, shift_gap=True)
            if stamp > after:
                return stamp
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        raise ValueError("No valid occurrence found in the next year.")
    times = timing.clock_minutes() if isinstance(timing, WindowTiming) else (_minutes(timing.time),)
    # A weekly occurrence may be skipped at the daylight-saving transition.
    for offset in range(15):
        day = local.date() + timedelta(days=offset)
        if timing.kind != "daily" and day.weekday() not in timing.days:
            continue
        for minutes in times:
            stamp = _civil(day, minutes, zone)
            if stamp is not None and stamp > after:
                return stamp
    raise ValueError("No valid occurrence found in the next two weeks.")


def upcoming_runs(timing: Timing, first: float | None, count: int = 3) -> list[float]:
    """The routine's next `count` run times, starting with its next due time."""
    runs: list[float] = []
    while first is not None and len(runs) < count:
        runs.append(first)
        first = next_run(timing, first)
    return runs


# A run that starts within this long of its time is on time.
ON_TIME_SECONDS = 15 * 60
# A routine that came due while its runtime was unavailable (the computer asleep or off,
# the app closed, the queue full) still runs once, late, if its latest occurrence is at
# most this old. Anything older is recorded as missed and never replayed.
CATCH_UP_SECONDS = 12 * 60 * 60


def latest_run(timing: Timing, due: float, now: float, *, within: float) -> float | None:
    """The most recent occurrence from due through now that is at most `within` seconds old."""
    if isinstance(timing, IntervalTiming):
        seconds = timing.every_minutes * 60
        latest = due + int((now - due) // seconds) * seconds
        return latest if now - latest <= within else None
    latest = due if due >= now - within else next_run(timing, now - within)
    if latest is None or latest > now:
        return None
    while (following := next_run(timing, latest)) is not None and following <= now:
        latest = following
    return latest


def advance_run(timing: Timing, due: float, now: float) -> float | None:
    if isinstance(timing, IntervalTiming):
        seconds = timing.every_minutes * 60
        return due + (int((now - due) // seconds) + 1) * seconds
    return next_run(timing, now)


FailureKind = Literal[
    "temporary", "limit", "interrupted", "failed", "sign_in", "setting", "review", "access"
]
# Failures only the person can fix: the routine pauses at once, saying what to do.
NEEDS_PERSON: frozenset[str] = frozenset({"sign_in", "setting", "review", "access"})


@dataclass(frozen=True)
class RunFailure:
    """Why a routine's run did not finish, decided from statuses and error types.

    temporary: the model service or storage was busy or unreachable; tried again soon.
    limit: a plan or usage limit; the routine waits until it frees up (retry_at, when the
    service said when). interrupted: the runtime stopped mid-run; never replayed, the next
    run goes ahead. failed: anything else; the next run goes ahead, and repeated failures
    pause the routine. sign_in, setting, review: the routine pauses until the person acts.
    access: the run needed something the person hasn't allowed for this routine (a website,
    the owl's computer or its browser); the routine pauses until they allow it.
    """

    kind: FailureKind
    retry_at: float | None = None


# A temporarily failed run is tried again after these pauses, while that is still before
# the routine's next scheduled run; after the last one the run counts as failed.
RETRY_DELAYS_SECONDS = (5 * 60, 15 * 60, 45 * 60)
# A limit that did not say when it frees up is tried again after an hour, then after
# twice as long each time, at most a day apart. A stated reset time is used as given.
LIMIT_RETRY_SECONDS = 60 * 60
LIMIT_RETRY_MAX_SECONDS = 24 * 60 * 60
LIMIT_WAIT_MAX_SECONDS = 31 * 24 * 60 * 60
# This many scheduled runs failing in a row pause the routine: something needs the person.
FAILED_RUNS_BEFORE_PAUSE = 3

# Why a routine paused and what the person can do. The app shows its own translation
# of each code; this text is for older apps and for the owl reading its routines.
PAUSE_REASONS = {
    "assignment_coordinator": "Choose this owl as coordinator again, then turn this routine back on.",
    "assignment_missing": "The original assignment or its owl is unavailable. Approve a new assignment to set up a replacement routine.",
    "assignment_review": "A result needs review. Open Owl assignments before turning this routine back on.",
    "sign_in": "The model service didn't accept this owl's sign-in. Sign in again or check the model connection, then turn this routine back on.",
    "setting": "The model service refused the run because of a setting or permission. Check the owl's model and connection settings, then turn this routine back on.",
    "review": "A run stopped before it could confirm an action. Check what it did in the chat, then turn this routine back on.",
    "approval": "A run is still waiting for your approval. Answer it in the chat, then turn this routine back on.",
    "repeated": f"The last {FAILED_RUNS_BEFORE_PAUSE} runs failed. Open the latest run in the chat to see why, then turn this routine back on.",
    "allowance": "This workspace has used its model-call allowance, so runs can't start. Turn this routine back on once more calls are allowed.",
    # Stored with the run's own request in its words (routine_access.approval_reason).
    "access": "A run needs something this routine isn't allowed to use. Allow it for this routine in the app, then run it again.",
}


def retry_time(
    failure: RunFailure, attempt: int, now: float, next_due: float | None
) -> float | None:
    """When to try a failed scheduled run again, or None to leave it to the next run.

    `attempt` counts this retry: 1 for the first. A temporary failure is retried only
    before the next scheduled run, which would do the same work. A limit always waits.
    """
    if failure.kind == "temporary":
        if attempt > len(RETRY_DELAYS_SECONDS):
            return None
        at = now + RETRY_DELAYS_SECONDS[attempt - 1]
        return at if next_due is None or at < next_due else None
    if failure.kind == "limit":
        if failure.retry_at is not None and failure.retry_at > now:
            return min(failure.retry_at, now + LIMIT_WAIT_MAX_SECONDS)
        return now + min(LIMIT_RETRY_SECONDS * 2 ** min(attempt - 1, 8), LIMIT_RETRY_MAX_SECONDS)
    return None
