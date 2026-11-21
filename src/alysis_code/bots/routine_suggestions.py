"""Routines an owl suggests from a need the person shows repeats, decided under its reply.

The owl decides when to offer one, from the conversation, and says what in structured fields
(``propose_schedule``: name, what each run does, timing, needs and, when the person asked for
one, the model its runs think with). The suggestion is saved as a draft routine, which nothing
runs, and as one card under the reply that made it: Turn on (the person's own yes; what the
routine needs from the owl's computer, browser or websites is allowed in the same step, exactly
as the card showed it), Change… (their own name, job or timing first) or Not now. The Routines
tab keeps listing the draft until the person answers.

Only the person's own message, while the owl answers it, can lead to a suggestion: never a
routine's run, a file event, a peer exchange, a first hello or another answer. One suggestion
waits at a time: a newer one replaces it. What the person said Not now to is remembered as a
fingerprint of its job and timing (Unicode-normalized and case-folded, so the same words in
another letter case or form match), and the owl is never let to offer it again; nor a routine
the owl already has. Nothing here reads the words themselves for meaning.
"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from typing import Literal

from pydantic import Field, model_validator

from .models import Input
from .regenerate import regenerates
from .routine_access import RoutineAccessDecision, RoutineNeeds, parse_needs
from .scheduling import RoutineRequest, ScheduleInput, ScheduleProposal, Timing
from .store import Conflict, NotFound, Store, encode, uid

# A suggestion whose turn stopped never became a card the person could answer.
_STOPPED = ("cancelled", "cancelling", "failed", "interrupted")

ONLY_FROM_THE_PERSON = (
    "Only the person's own message in this chat, while you answer it, can lead to a routine "
    "suggestion: not a routine's run, a file event, a peer, a first hello or another answer."
)


class RoutineSuggestionDecision(Input):
    """The person's answer to a suggested routine's card.

    Turn on carries a request id, so sending it again (a double click, a lost answer) returns the
    routine already turned on. Change… sends the person's own name, job or timing with Turn on.
    ``allow`` is what the routine uses, exactly as the card showed it, when it uses anything.
    """

    action: Literal["turn_on", "decline"]
    expected_revision: int = Field(ge=1)
    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    timing: Timing | None = Field(default=None, json_schema_extra={"type": "object"})
    allow: RoutineNeeds | None = None

    @model_validator(mode="after")
    def _turn_on_carries_the_rest(self) -> RoutineSuggestionDecision:
        if self.action == "turn_on" and self.request_id is None:
            raise ValueError("Turn on takes a request_id.")
        if self.action == "decline" and (
            self.request_id is not None
            or self.name is not None
            or self.prompt is not None
            or self.timing is not None
            or self.allow is not None
        ):
            raise ValueError("Not now takes no changes.")
        return self


def _normalized(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(unicodedata.normalize("NFKC", folded).split())


def _timing_key(timing: dict) -> dict:
    """One way to write a timing, so the same schedule written two ways compares equal."""
    parsed = ScheduleProposal.model_validate({"name": "-", "prompt": "-", "timing": timing}).timing
    key = parsed.model_dump(mode="json")
    if parsed.kind in ("weekly", "window"):
        key.pop("weekday", None)
        key["weekdays"] = list(parsed.days)
    if parsed.kind == "once":
        key["at"] = parsed.at.timestamp()
    return key


def fingerprints(name: str, prompt: str, timing: dict) -> tuple[str, str]:
    """The job (what each run does) and the title, each with the timing, as digests.

    A suggestion matches an earlier one when either agrees, so rewording only one still matches.
    """

    def digest(text: str) -> str:
        body = encode([_normalized(text), _timing_key(timing)])
        return hashlib.sha256(body.encode()).hexdigest()

    return digest(prompt), digest(name)


_CARDS = (
    "SELECT g.*,t.status AS source_status,s.state AS schedule_state "
    "FROM routine_suggestions g JOIN tasks t ON t.id=g.source_task_id "
    "LEFT JOIN schedules s ON s.id=g.schedule_id AND s.bot_id=g.bot_id WHERE g.bot_id=?"
)


def _status(row) -> str:
    """A card's status as the person sees it.

    The person decided on the card (on, declined), or it was closed (replaced). An open card
    follows its draft: turned on in Routines is on; deleted or changed there, or a turn that
    stopped, closes it.
    """
    if row["status"] != "open":
        return row["status"]
    if row["source_status"] in _STOPPED:
        return "closed"
    state = row["schedule_state"]
    if state in ("active", "ended"):
        return "on"
    return "open" if state == "draft" else "closed"


def _card(db, row) -> dict:
    status = _status(row)
    to_allow = None
    if status == "open":
        to_allow = Store._routine_access(db, row["schedule_id"])["access"]["to_allow"]
    needs = parse_needs(row["needs"]) or RoutineNeeds()
    return {
        "id": row["id"],
        "source_task_id": row["source_task_id"],
        "schedule_id": row["schedule_id"],
        "revision": row["revision"],
        "status": status,
        # The owl's reply is out, so the person can answer.
        "ready": row["source_status"] == "completed",
        "name": row["name"],
        "prompt": row["prompt"],
        "timing": json.loads(row["timing"]),
        "needs": needs.model_dump(),
        # What Turn on asks the person to allow first; None when it uses nothing beyond chat.
        "to_allow": to_allow,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "decided_at": row["decided_at"],
    }


def suggestion_cards(db, bot_id: str, *, only=None) -> dict[str, dict]:
    """Each turn's suggested routine by its task id, for the conversation's task decoration."""
    scope, values = "", (bot_id,)
    if only is not None:
        if not only:
            return {}
        scope = f" AND g.source_task_id IN ({','.join('?' * len(only))})"
        values = (bot_id, *only)
    return {row["source_task_id"]: _card(db, row) for row in db.execute(_CARDS + scope, values)}


class RoutineSuggestions:
    """Routines an owl offers in its chat; only the person's Turn on switches one on."""

    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    def _read(self, db, bot_id: str, suggestion_id: str) -> dict:
        row = db.execute(_CARDS + " AND g.id=?", (bot_id, suggestion_id)).fetchone()
        if row is None:
            raise NotFound("Routine suggestion not found.")
        return _card(db, row)

    def _settle(self, db, bot_id: str, now: float) -> None:
        """Save what became of open cards, so one waits at a time and a decision is current."""
        for row in db.execute(_CARDS + " AND g.status='open'", (bot_id,)).fetchall():
            status = _status(row)
            if status != "open":
                db.execute(
                    "UPDATE routine_suggestions SET status=?,revision=revision+1,updated_at=? "
                    "WHERE id=?",
                    (status, now, row["id"]),
                )

    def _remove_draft(self, db, bot_id: str, schedule_id: str | None) -> None:
        """A suggestion's draft leaves Routines with it, unless the person changed it there."""
        if (
            schedule_id
            and db.execute(
                "SELECT 1 FROM schedules WHERE id=? AND bot_id=? AND state='draft'",
                (schedule_id, bot_id),
            ).fetchone()
        ):
            Store._erase_schedule(db, schedule_id)

    def recent(self, bot_id: str, limit: int = 8) -> list[dict]:
        """This owl's latest suggested routines and what became of them, newest first."""
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            rows = db.execute(
                _CARDS + " ORDER BY g.created_at DESC,g.id DESC LIMIT ?", (bot_id, limit)
            ).fetchall()
            return [_card(db, row) for row in rows]

    def suggest(self, task: dict, data: RoutineRequest) -> dict:
        """Save the owl's suggestion as a draft routine and one card under its reply.

        The same suggestion sent again by its turn returns the card it made. A suggestion the
        person declined, or a routine this owl already has, is refused with what to do instead.
        """
        with self.store.connect(write=True) as db:
            try:
                source = self.store.direct_request(db, self.owner, task["bot_id"], task["id"])
            except Conflict:
                raise Conflict(ONLY_FROM_THE_PERSON) from None
            if regenerates(source):
                raise Conflict(ONLY_FROM_THE_PERSON)
            bot_id = source["bot_id"]
            now = time.time()
            item = ScheduleInput(
                **{**data.model_dump(), "timing": data.absolute_timing(source["created_at"])}
            )
            timing = item.timing.model_dump(mode="json")
            needs = (item.needs or RoutineNeeds()).model_dump()
            job, title = fingerprints(item.name, item.prompt, timing)
            own = db.execute(_CARDS + " AND g.source_task_id=?", (bot_id, source["id"])).fetchone()
            if own is not None:
                if (own["fingerprint"], own["title_fingerprint"]) == (job, title) and (
                    parse_needs(own["needs"]) or RoutineNeeds()
                ).model_dump() == needs:
                    return _card(db, own)
                raise Conflict(
                    "You already suggested a routine in this turn; the person sees it under your "
                    "reply. Do not suggest another in this turn."
                )
            self._settle(db, bot_id, now)
            declined = db.execute(
                "SELECT name FROM routine_suggestions WHERE bot_id=? AND status='declined' "
                "AND (fingerprint=? OR title_fingerprint=?) LIMIT 1",
                (bot_id, job, title),
            ).fetchone()
            if declined is not None:
                raise Conflict(
                    f"The person chose Not now for this routine ({declined['name']}). Do not "
                    "suggest it again unless they ask for it; carry on with their request."
                )
            for routine in db.execute(
                "SELECT name,prompt,timing FROM schedules WHERE bot_id=?", (bot_id,)
            ).fetchall():
                if {job, title} & set(
                    fingerprints(routine["name"], routine["prompt"], json.loads(routine["timing"]))
                ):
                    raise Conflict(
                        f"This owl already has this routine ({routine['name']}); see "
                        "list_schedules. Do not suggest it again."
                    )
            # One suggestion waits at a time: the newer one replaces it, and its draft goes.
            for waiting in db.execute(
                "SELECT id,schedule_id FROM routine_suggestions WHERE bot_id=? AND status='open'",
                (bot_id,),
            ).fetchall():
                db.execute(
                    "UPDATE routine_suggestions SET status='closed',revision=revision+1,"
                    "updated_at=? WHERE id=?",
                    (now, waiting["id"]),
                )
                self._remove_draft(db, bot_id, waiting["schedule_id"])
            schedule = self.store.save_schedule(
                self.owner, bot_id, item, source_task_id=source["id"], within=db
            )
            suggestion_id = uid()
            db.execute(
                "INSERT INTO routine_suggestions(id,bot_id,source_task_id,schedule_id,name,prompt,"
                "timing,needs,fingerprint,title_fingerprint,status,revision,request_id,created_at,"
                "updated_at,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,'open',1,NULL,?,?,NULL)",
                (
                    suggestion_id,
                    bot_id,
                    source["id"],
                    schedule["id"],
                    item.name,
                    item.prompt,
                    encode(timing),
                    encode(needs),
                    job,
                    title,
                    now,
                    now,
                ),
            )
            self.store._event(
                db,
                source["id"],
                "routine_suggested",
                {"suggestion_id": suggestion_id, "schedule_id": schedule["id"]},
            )
            return self._read(db, bot_id, suggestion_id)

    def decide(self, bot_id: str, suggestion_id: str, data: RoutineSuggestionDecision) -> dict:
        """Turn the suggested routine on, or set it aside, only from the card the person saw.

        Turn on applies the person's changes, allows what the routine uses (exactly what the
        card showed) and switches it on, all together or not at all. Not now removes the draft
        and remembers the decline. A decided card stays decided.
        """
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            now = time.time()
            self._settle(db, bot_id, now)
            row = db.execute(_CARDS + " AND g.id=?", (bot_id, suggestion_id)).fetchone()
            if row is None:
                raise NotFound("Routine suggestion not found.")
            turn_on = data.action == "turn_on"
            if row["status"] == ("on" if turn_on else "declined"):
                if not turn_on or row["request_id"] == data.request_id:
                    return _card(db, row)
                raise Conflict("This routine was already turned on.")
            if row["status"] != "open":
                raise Conflict(
                    "This suggestion is no longer open: the person answered it, it changed or "
                    "was removed in Routines, or a newer suggestion replaced it."
                )
            if row["revision"] != data.expected_revision:
                raise Conflict("This suggestion changed. Review it again before answering.")
            if row["source_status"] != "completed":
                raise Conflict("Wait for the owl to finish its reply.")
            if not turn_on:
                self._remove_draft(db, bot_id, row["schedule_id"])
                db.execute(
                    "UPDATE routine_suggestions SET status='declined',schedule_id=NULL,"
                    "revision=revision+1,decided_at=?,updated_at=? WHERE id=?",
                    (now, now, suggestion_id),
                )
                self.store._event(
                    db,
                    row["source_task_id"],
                    "routine_suggestion_declined",
                    {"suggestion_id": suggestion_id},
                )
                return self._read(db, bot_id, suggestion_id)
            schedule = Store._schedule(db, bot_id, row["schedule_id"])
            if data.name is not None or data.prompt is not None or data.timing is not None:
                # The person's own version: saved as their edit, off until the steps below.
                schedule = self.store.save_schedule(
                    self.owner,
                    bot_id,
                    ScheduleInput.model_validate(
                        {
                            "name": schedule["name"] if data.name is None else data.name,
                            "prompt": schedule["prompt"] if data.prompt is None else data.prompt,
                            "timing": schedule["timing"]
                            if data.timing is None
                            else data.timing.model_dump(mode="json"),
                            "enabled": False,
                            "expected_revision": schedule["revision"],
                        }
                    ),
                    schedule_id=schedule["id"],
                    within=db,
                )
            to_allow = schedule["access"]["to_allow"]
            if to_allow is not None:
                if data.allow is None or data.allow.model_dump() != to_allow:
                    raise Conflict(
                        "Review what this routine uses (the owl's computer, browser or websites) "
                        "and allow it to turn the routine on."
                    )
                schedule = self.store.allow_routine_access(
                    self.owner,
                    bot_id,
                    schedule["id"],
                    RoutineAccessDecision(
                        expected_revision=schedule["revision"], allow=data.allow, turn_on=True
                    ),
                    within=db,
                )
            else:
                self.store._set_schedule_state(db, schedule, "active", now)
                schedule = Store._schedule(db, bot_id, schedule["id"])
            db.execute(
                "UPDATE routine_suggestions SET status='on',request_id=?,name=?,prompt=?,timing=?,"
                "revision=revision+1,decided_at=?,updated_at=? WHERE id=?",
                (
                    data.request_id,
                    schedule["name"],
                    schedule["prompt"],
                    encode(schedule["timing"]),
                    now,
                    now,
                    suggestion_id,
                ),
            )
            self.store._event(
                db,
                row["source_task_id"],
                "routine_suggestion_turned_on",
                {"suggestion_id": suggestion_id, "schedule_id": schedule["id"]},
            )
            return self._read(db, bot_id, suggestion_id)
