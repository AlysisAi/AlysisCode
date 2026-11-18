"""The person's time zone, and how an owl is told the local date and time.

The person's app tells the service their computer's IANA time zone (PUT /v1/settings/time-zone).
Each turn reads it when it runs, so a zone that changes with travel applies from the next turn,
and daylight saving time follows from the zone itself. Until a zone has been set, owls work in UTC
and say so: a zone is never guessed from the person's language or anything else.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Area/Location names (Europe/Athens, America/Argentina/Buenos_Aires) and single names (UTC).
ZONE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*")
# Entries of the time zone database that stand for the host's own zone or for no place at all.
NOT_A_PLACE = frozenset({"localtime", "posixrules", "Factory"})

# Written out, so the text is the same whatever locale the service runs in.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def valid_time_zone(value: str) -> str:
    """The IANA name when the time zone database knows it as a place; ValueError otherwise."""
    if len(value) > 100 or not ZONE_NAME.fullmatch(value) or value in NOT_A_PLACE:
        raise ValueError("Use a valid IANA time zone, such as Europe/Athens.")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use a valid IANA time zone, such as Europe/Athens.") from None
    return value


def loaded_zone(name: str | None) -> ZoneInfo | None:
    """The zone to read times in, or None when there is none or it can't be loaded here."""
    if not name:
        return None
    try:
        return ZoneInfo(valid_time_zone(name))
    except ValueError:
        return None


def _offset(moment: datetime) -> str:
    seconds = int(moment.utcoffset().total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, minutes = divmod(abs(seconds) // 60, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def local_time(timestamp: float, zone_name: str | None) -> str | None:
    """'Tuesday 17 November 2026, 20:55 (Europe/Athens, UTC+02:00)'; None without a usable zone."""
    zone = loaded_zone(zone_name)
    if zone is None:
        return None
    moment = datetime.fromtimestamp(timestamp, UTC).astimezone(zone)
    return (
        f"{WEEKDAYS[moment.weekday()]} {moment.day} {MONTHS[moment.month - 1]} {moment.year}, "
        f"{moment:%H:%M} ({zone_name}, {_offset(moment)})"
    )


def time_context(timestamp: float, person_zone: str | None, routine_zone: str | None = None) -> str:
    """What a turn is told about the date and time, at the time the turn started.

    A routine run reads dates on its own clock: the zone saved with its timing. Otherwise it is
    the person's zone, and UTC, said plainly, while no zone is known.
    """
    person = local_time(timestamp, person_zone)
    routine = local_time(timestamp, routine_zone)
    lines = []
    if routine is not None:
        lines.append(
            "Now in this routine's time zone, when this run started: "
            + routine
            + ". The routine runs on this clock: read today, tomorrow, weekdays and clock times "
            "in this run in this time zone."
        )
        if person is not None and person_zone != routine_zone:
            lines.append("Now for the person, in their own time zone: " + person + ".")
        return "\n".join(lines)
    if person is not None:
        return (
            "Now for the person, when this task started: "
            + person
            + ". Read today, tomorrow, weekdays and clock times the person mentions in this time "
            "zone unless they name another one."
        )
    utc = datetime.fromtimestamp(timestamp, UTC)
    return (
        "Now in UTC, when this task started: "
        f"{WEEKDAYS[utc.weekday()]} {utc.day} {MONTHS[utc.month - 1]} {utc.year}, {utc:%H:%M} (UTC). "
        "The person's time zone is not known, so this is UTC and may not be their local date or "
        "time. When an answer depends on their local date or time, ask them or say that you "
        "used UTC."
    )
