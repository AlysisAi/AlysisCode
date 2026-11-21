"""An owl coming back to its chat later by itself: check_back.

An owl that has to wait (a page to change, a file to arrive, its other work to end, a time the
person named) never waits inside its turn, paying a model call for every look and holding the
chat. It calls check_back and ends the turn. The service keeps the check-back and, when it is due,
starts one new turn in the same chat whose request is a note from the app ("You asked to check
back: ..."), never the person's words (``check_back_request``).

A check-back is a time, 1 minute to 30 days ahead (relative, counted by the service, or an
absolute time with its offset), a short note for the future turn, and optionally a condition the
service checks by itself, with no model call and with backoff, which brings the turn sooner:
a public web page contains, or no longer contains, a short text (read through the owl's own web
lookup and its address rules, and matched Unicode-normalised, case-folded and
whitespace-collapsed, without accents only when nothing matches exactly: text_search.py); a file
whose name matches a pattern arrives in the owl's Library; or the owl's own background, coding or
peer work ends. With a condition the time is a deadline, and the turn is told which came first.

Only the person's own message, or a turn a check-back started, may set one (the tool is offered to
no other turn, and setting it checks again); at most MAX_PENDING wait in a chat, and never in a
room. It fires once: its start is saved with the new turn in one transaction, so a restart never
starts it twice, and when the app was closed or asleep at its time it starts once, late, saying
how late. The new turn is an ordinary turn, under the owl's plan limits and approvals, and it
qualifies for nothing only the person's own message gives (Store.direct_request). The person sees
each pending one in the chat with Cancel; pending ones travel with a moved owl (owl_move.py) and go
with a deleted one (Store.delete_bot).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from .models import Input
from .regenerate import persons_request
from .store import TERMINAL, Conflict, LimitReached, NotFound, Store, encode, uid
from .text_search import exact_fold, loose_fold
from .time_zone import local_time

MIN_SECONDS = 60
MAX_SECONDS = 30 * 24 * 3600
MAX_PENDING = 10
MAX_NOTE_CHARACTERS = 500
MAX_TEXT_CHARACTERS = 200
# Starting later than this after its time, the turn is told how late (the app was closed or asleep).
LATE_SECONDS = 120
# A condition is first checked this long after it is set, then the wait doubles up to the cap:
# reading the owl's own records costs nothing, a web page is someone else's server.
LOCAL_BACKOFF = (5.0, 60.0)
PAGE_BACKOFF = (60.0, 1800.0)
PAGE_KINDS = ("page_contains", "page_lacks")
REQUEST_PREFIX = "check_back_"
# A page is read up to this many characters (web_fetch's own limit).
PAGE_CHARACTERS = 50_000
_FIELDS = {
    "page_contains": {"url", "text"},
    "page_lacks": {"url", "text"},
    "file_appears": {"name"},
    "work_ends": {"work_id"},
}
# Peer work that is still going (cooperation.py).
_PEER_GOING = ("queued", "running", "waiting")


class CheckBackCondition(Input):
    kind: Literal["page_contains", "page_lacks", "file_appears", "work_ends"] = Field(
        description="page_contains: the page shows text. page_lacks: it no longer shows text. "
        "file_appears: a file whose name matches name is added to your Library. work_ends: the "
        "background, coding or peer work named by work_id ends."
    )
    url: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="page_contains and page_lacks: the public http(s) address of the page.",
    )
    text: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_TEXT_CHARACTERS,
        description="page_contains and page_lacks: a short text as the page writes it.",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="file_appears: the file name; * stands for any characters, ? for one.",
    )
    work_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="work_ends: the task_id of your background or coding work, or the "
        "exchange_id of your request to another owl.",
    )

    @model_validator(mode="after")
    def _fields_of_its_kind(self):
        given = {
            key for key in ("url", "text", "name", "work_id") if getattr(self, key) is not None
        }
        if given != _FIELDS[self.kind]:
            raise ValueError(
                f"{self.kind} takes {' and '.join(sorted(_FIELDS[self.kind]))}, and nothing else."
            )
        return self


class CheckBackRequest(Input):
    after_seconds: int | None = Field(
        default=None,
        ge=MIN_SECONDS,
        le=MAX_SECONDS,
        description="How long from now, in seconds. The service counts it; never work out a "
        "clock time yourself for a relative wait.",
    )
    at: AwareDatetime | None = Field(
        default=None,
        description="A stated date and time, with its UTC offset, such as 2026-05-04T09:00:00+03:00.",
    )
    note: str = Field(
        min_length=1,
        max_length=MAX_NOTE_CHARACTERS,
        description="For your future turn: what to look at and what to do then, complete on its "
        "own, in the person's language.",
    )
    condition: CheckBackCondition | None = Field(
        default=None,
        description="Come back sooner, as soon as this holds; the time is then the deadline.",
    )

    @model_validator(mode="after")
    def _one_time(self):
        if (self.after_seconds is None) == (self.at is None):
            raise ValueError("Give either after_seconds or at.")
        return self

    def due(self, now: float) -> float:
        if self.after_seconds is not None:
            return now + self.after_seconds
        assert self.at is not None
        return self.at.timestamp()


DESCRIPTION = (
    "Come back to this chat later by yourself whenever the work has to wait (a page to change, a "
    "file to arrive, your other work to end, a time the person named), instead of sleeping or "
    "checking again and again within a turn. Give after_seconds or at, 1 minute to 30 days "
    "ahead, and a note for your future turn. An optional condition, checked by the service "
    "without you, brings that turn sooner once it holds (page text is matched ignoring case, "
    "spacing and, when nothing else matches, accents, in a page's first "
    f"{PAGE_CHARACTERS:,} characters; file_appears counts files added from now on); the time is "
    "then the deadline, and the turn is told which came first. It runs once, as a turn in this "
    "chat with the usual limits and approvals, or once the app is back if it was closed. At most "
    f"{MAX_PENDING} wait in a chat, each with Cancel for the person. Then end your turn with one "
    "short line saying when you will check back."
)


def first_check(kind: str) -> float:
    """How long after setting a condition of this kind its first check comes."""
    return (PAGE_BACKOFF if kind in PAGE_KINDS else LOCAL_BACKOFF)[0]


# ---------------------------------------------------------------------------------------------
# Matching, the same way for every language and script


def _flat(text: str) -> str:
    return " ".join(text.split())


def page_shows(page: str, text: str) -> bool:
    """Whether a page shows a text: Unicode-normalised, case-folded and whitespace-collapsed,
    and without accents or compatibility forms only when that finds nothing."""
    needle = _flat(exact_fold(text))
    if needle and needle in _flat(exact_fold(page)):
        return True
    loose = loose_fold(text)
    return bool(loose) and loose in loose_fold(page)


def _wildcards(pattern: str) -> re.Pattern:
    """A name pattern as a regular expression: * any characters, ? one, the rest literal."""
    return re.compile(
        "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern),
        re.S,
    )


def name_matches(name: str, pattern: str) -> bool:
    """Whether a file name matches a pattern, folded like ``page_shows``."""

    def exact(value: str) -> str:
        return _flat(exact_fold(value))

    if _wildcards(exact(pattern)).fullmatch(exact(name)):
        return True
    return bool(_wildcards(loose_fold(pattern)).fullmatch(loose_fold(name)))


# ---------------------------------------------------------------------------------------------
# What the new turn is told


CHECK_BACK_REQUEST = (
    "This message comes from the Alysis app, not from the person. You asked to check back: "
    "{note}\n{why}Look at the current state again rather than relying on what you saw before, do "
    "what your note says, then tell the person what you found, briefly, in the language they have "
    "been using with you. To wait again, call check_back and end your turn."
)


def describe_condition(condition: dict[str, Any]) -> str:
    """A condition in plain words, for the turn it brings."""
    kind = condition.get("kind")
    text = json.dumps(str(condition.get("text") or ""), ensure_ascii=False)
    if kind == "page_contains":
        return f"the page {condition.get('url')} shows {text}"
    if kind == "page_lacks":
        return f"the page {condition.get('url')} no longer shows {text}"
    if kind == "file_appears":
        name = json.dumps(str(condition.get("name") or ""), ensure_ascii=False)
        return f"a file named like {name} was added to your Library"
    return f"your work {condition.get('work_id')} ended"


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 120:
        return f"{minutes} minutes"
    hours = round(minutes / 60)
    return f"{hours} hours" if hours < 48 else f"{round(hours / 24)} days"


def check_back_request(config: dict) -> str:
    """The app's request for a turn a check-back started (task config ``check_back``)."""
    start = config.get("check_back") or {}
    condition = start.get("condition")
    outcome = start.get("outcome")
    why = ""
    if isinstance(condition, dict) and outcome == "met":
        why = f"What you were waiting for happened: {describe_condition(condition)}. "
    elif isinstance(condition, dict) and outcome == "deadline":
        why = (
            "Your time ran out before what you were waiting for happened: "
            f"{describe_condition(condition)}. Say so plainly. "
        )
        if start.get("detail"):
            why += f"The last check could not tell: {start['detail']} "
    late = start.get("late_by_seconds")
    if isinstance(late, int | float) and late > 0:
        why += (
            f"This is {_duration(late)} later than you planned: the app was closed or asleep "
            "at that time. "
        )
    return CHECK_BACK_REQUEST.format(
        note=json.dumps(str(start.get("note") or ""), ensure_ascii=False), why=why
    )


# ---------------------------------------------------------------------------------------------
# What the chat shows


def public(row) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "source_task_id": row["source_task_id"],
        "due_at": row["due_at"],
        "note": row["note"],
        "condition": json.loads(row["condition"]) if row["condition"] else None,
        "state": row["state"],
        "task_id": row["task_id"],
        "outcome": row["outcome"],
        "created_at": row["created_at"],
    }


def task_check_backs(db, bot_id: str, *, only=None) -> dict[str, list[dict]]:
    """The check-backs each turn set, by the turn's id, so the chat shows them under its reply
    (pending ones with Cancel). ``only`` limits the read to those turns."""
    scope, values = "c.bot_id=?", (bot_id,)
    if only is not None:
        if not only:
            return {}
        scope += f" AND c.source_task_id IN ({','.join('?' * len(only))})"
        values = (bot_id, *only)
    found: dict[str, list[dict]] = {}
    for row in db.execute(
        f"SELECT * FROM check_backs c WHERE {scope} ORDER BY c.created_at,c.id", values
    ):
        found.setdefault(row["source_task_id"], []).append(public(row))
    return found


def checked_back(task: dict) -> dict | None:
    """What a turn a check-back started shows above its reply, from its own config."""
    start = (task.get("config") or {}).get("check_back")
    if not isinstance(start, dict):
        return None
    return {
        key: start[key]
        for key in ("id", "note", "outcome", "late_by_seconds")
        if start.get(key) is not None
    }


# ---------------------------------------------------------------------------------------------
# Keeping them, checking their conditions and starting their turns


def _read_page(url: str) -> tuple[str, bool]:
    from .web_lookup import page_text

    return page_text(url, PAGE_CHARACTERS)


def _in_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="alysis-bots-check-back", daemon=True).start()


class CheckBacks:
    """One owner's check-backs. ``clock`` gives the time; ``read_page(url)`` returns a page's
    readable text and whether it was read whole; ``run_check(work)`` runs a page read away from
    the scheduler (by default on a thread of its own, one read at a time)."""

    def __init__(
        self,
        store: Store,
        owner: str,
        *,
        clock: Callable[[], float] = time.time,
        read_page: Callable[[str], tuple[str, bool]] = _read_page,
        run_check: Callable[[Callable[[], None]], None] = _in_thread,
    ):
        self.store, self.owner = store, owner
        self.clock, self.read_page, self.run_check = clock, read_page, run_check
        self._lock = threading.Lock()
        self._reading: set[str] = set()

    # Setting one, from a turn ------------------------------------------------------------------

    @staticmethod
    def may_set(db, task: dict) -> bool:
        """The person's own message working now, or a turn a check-back started: never a
        routine's run, a file event, work carried on after other work, or one in a room."""
        if task["status"] != "running":
            return False
        if not (persons_request(task) or task["config"].get("check_back")):
            return False
        if db.execute("SELECT 1 FROM rooms WHERE id=?", (task["chat_id"],)).fetchone():
            return False
        return not any(
            db.execute(sql, (task["id"],)).fetchone()
            for sql in (
                "SELECT 1 FROM schedule_runs WHERE task_id=?",
                "SELECT 1 FROM task_waits WHERE task_id=?",
                "SELECT 1 FROM peer_exchanges WHERE source_task_id=?",
            )
        )

    def _check_condition(self, db, task: dict, condition: CheckBackCondition) -> None:
        if condition.kind in PAGE_KINDS:
            from .web_lookup import public_page_address, web_tools_available

            if not web_tools_available():
                raise Conflict("Looking at web pages is off here, so a page can't be watched.")
            public_page_address(condition.url or "")
        elif condition.kind == "file_appears":
            if not task["config"].get("files_enabled", True):
                raise Conflict("Files are off for this owl, so no file can be watched for.")
        else:
            work_id = condition.work_id
            if work_id == task["id"]:
                raise Conflict("This turn can't wait for itself. Wait for other work.")
            work = db.execute(
                "SELECT status FROM tasks WHERE id=? AND bot_id=?", (work_id, task["bot_id"])
            ).fetchone()
            peer = db.execute(
                "SELECT status FROM peer_exchanges WHERE id=? AND source_bot_id=?",
                (work_id, task["bot_id"]),
            ).fetchone()
            if work is None and peer is None:
                raise NotFound(
                    "No work of yours has that id. Use a task_id from read_background_task or "
                    "read_coding_result, or an exchange_id from read_peer_exchange."
                )
            if (work is not None and work["status"] in TERMINAL) or (
                peer is not None and peer["status"] not in _PEER_GOING
            ):
                raise Conflict("That work has already ended: read its result now instead.")

    def set(self, task: dict, request: CheckBackRequest) -> dict:
        """Keep a check-back for ``task``'s chat; the turn should end after it."""
        now = self.clock()
        due = request.due(now)
        if not now + MIN_SECONDS - 1 <= due <= now + MAX_SECONDS + 1:
            raise Conflict(
                "A check-back must be from 1 minute to 30 days from now. For a relative wait "
                "give after_seconds; for at, give the date and time the person named, with its "
                "offset."
            )
        condition = request.condition
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            if current["bot_id"] != task["bot_id"] or not self.may_set(db, current):
                raise Conflict(
                    "Only a turn answering the person's own message, or one you started by "
                    "checking back, can set a check-back."
                )
            waiting = db.execute(
                "SELECT count(*) FROM check_backs WHERE chat_id=? AND state='pending'",
                (current["chat_id"],),
            ).fetchone()[0]
            if waiting >= MAX_PENDING:
                raise LimitReached(
                    f"{MAX_PENDING} check-backs already wait in this chat. Wait for one, or "
                    "fold this into one of them; the person can cancel them in the chat."
                )
            if condition is not None:
                self._check_condition(db, current, condition)
            row_id = uid()
            db.execute(
                "INSERT INTO check_backs(id,bot_id,chat_id,source_task_id,due_at,note,condition,"
                "state,next_check_at,checks,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,0,?,?)",
                (
                    row_id,
                    current["bot_id"],
                    current["chat_id"],
                    current["id"],
                    due,
                    request.note,
                    encode(condition.model_dump(exclude_none=True)) if condition else None,
                    now + first_check(condition.kind) if condition else None,
                    now,
                    now,
                ),
            )
            self.store._event(
                db,
                current["id"],
                "check_back_set",
                {
                    "id": row_id,
                    "due_at": due,
                    "note": request.note,
                    **({"condition": condition.model_dump(exclude_none=True)} if condition else {}),
                },
            )
            row = db.execute("SELECT * FROM check_backs WHERE id=?", (row_id,)).fetchone()
            zone = self.store._time_zone(db, self.owner)
        when = local_time(due, zone) or local_time(due, "UTC")
        return {
            **public(row),
            "due": when,
            "message": "Saved. "
            + (
                "You get a new turn in this chat as soon as the condition holds, or at "
                f"{when} at the latest. "
                if condition
                else f"You get a new turn in this chat at {when}. "
            )
            + "The chat shows it with Cancel. End your turn now with one short line saying when "
            "you will check back; do not wait or check again in this turn.",
        }

    # The person's view -------------------------------------------------------------------------

    def list(self, chat_id: str) -> dict:
        """The check-backs still waiting in a chat, soonest first."""
        with self.store.connect() as db:
            self.store._chat(db, self.owner, chat_id)
            rows = db.execute(
                "SELECT * FROM check_backs WHERE chat_id=? AND state='pending' ORDER BY due_at,id",
                (chat_id,),
            ).fetchall()
        return {"check_backs": [public(row) for row in rows]}

    def cancel(self, chat_id: str, check_back_id: str) -> dict:
        """The person's Cancel: a check-back that hasn't started never will."""
        with self.store.connect(write=True) as db:
            self.store._chat(db, self.owner, chat_id)
            row = db.execute(
                "SELECT * FROM check_backs WHERE id=? AND chat_id=?", (check_back_id, chat_id)
            ).fetchone()
            if row is None:
                raise NotFound("This check-back isn't in this chat.")
            if row["state"] in ("starting", "done"):
                raise Conflict("It already checked back, so there is nothing to cancel.")
            if row["state"] == "pending":
                db.execute(
                    "UPDATE check_backs SET state='cancelled',updated_at=? WHERE id=?",
                    (self.clock(), check_back_id),
                )
                row = db.execute(
                    "SELECT * FROM check_backs WHERE id=?", (check_back_id,)
                ).fetchone()
            return public(row)

    def waiting(self, chat_id: str) -> list[dict]:
        """For the task state: what this chat's turns asked to come back for."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id,due_at,note,condition FROM check_backs WHERE chat_id=? "
                "AND state='pending' ORDER BY due_at,id",
                (chat_id,),
            ).fetchall()
            zone = self.store._time_zone(db, self.owner) if rows else None
        return [
            {
                "id": row["id"],
                "due": local_time(row["due_at"], zone) or local_time(row["due_at"], "UTC"),
                "note": row["note"],
                **({"condition": json.loads(row["condition"])} if row["condition"] else {}),
            }
            for row in rows
        ]

    # The scheduler -----------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> int:
        """One pass of the service's scheduler: settle turns that ended, check conditions that
        are due a look, and start the check-backs whose time came or whose condition holds.
        Returns how many turns it started."""
        now = self.clock() if now is None else now
        self._settle()
        self._check(now)
        return self._fire(now)

    _LIVE = (
        "JOIN bots b ON b.id=c.bot_id WHERE b.owner=? AND b.archived_at IS NULL "
        f"AND c.bot_id NOT IN ({Store.ARRIVING}) AND c.state='pending'"
    )

    def _settle(self) -> None:
        """A check-back whose turn ended is done."""
        ended = (
            "SELECT c.id FROM check_backs c JOIN tasks t ON t.id=c.task_id "
            f"WHERE c.state='starting' AND t.owner=? AND t.status IN {TERMINAL}"
        )
        with self.store.connect() as db:
            if not db.execute(ended + " LIMIT 1", (self.owner,)).fetchone():
                return
        with self.store.connect(write=True) as db:
            ids = [row[0] for row in db.execute(ended, (self.owner,)).fetchall()]
            for row_id in ids:
                db.execute(
                    "UPDATE check_backs SET state='done',updated_at=? WHERE id=?",
                    (self.clock(), row_id),
                )

    @staticmethod
    def _interval(kind: str, checks: int) -> float:
        """The wait before the next check, after ``checks`` checks: doubling, up to the cap."""
        first, cap = PAGE_BACKOFF if kind in PAGE_KINDS else LOCAL_BACKOFF
        return min(first * 2 ** min(checks + 1, 20), cap)

    @staticmethod
    def _local_holds(db, row, condition: dict) -> bool:
        if condition["kind"] == "file_appears":
            return any(
                name_matches(file["name"], condition["name"])
                for file in db.execute(
                    "SELECT name FROM files WHERE bot_id=? AND created_at>? "
                    "ORDER BY created_at,id LIMIT 500",
                    (row["bot_id"], row["created_at"]),
                )
            )
        work = db.execute(
            "SELECT status FROM tasks WHERE id=? AND bot_id=?",
            (condition["work_id"], row["bot_id"]),
        ).fetchone()
        if work is not None:
            return work["status"] in TERMINAL
        peer = db.execute(
            "SELECT status FROM peer_exchanges WHERE id=? AND source_bot_id=?",
            (condition["work_id"], row["bot_id"]),
        ).fetchone()
        # Work that is no longer there (its owl moved, or it was erased) has ended.
        return peer is None or peer["status"] not in _PEER_GOING

    def _check(self, now: float) -> None:
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT c.* FROM check_backs c {self._LIVE} AND c.condition IS NOT NULL "
                "AND c.met_at IS NULL AND c.next_check_at<=? AND c.due_at>? "
                "ORDER BY c.next_check_at,c.id LIMIT 50",
                (self.owner, now, now),
            ).fetchall()
            local = {
                row["id"]: self._local_holds(db, row, json.loads(row["condition"]))
                for row in rows
                if json.loads(row["condition"])["kind"] not in PAGE_KINDS
            }
        if not rows:
            return
        pages = []
        with self.store.connect(write=True) as db:
            for row in rows:
                condition = json.loads(row["condition"])
                if row["id"] not in local:
                    with self._lock:
                        if self._reading:
                            continue  # One page read at a time; this one waits its turn.
                        self._reading.add(row["id"])
                    pages.append((row["id"], condition))
                db.execute(
                    "UPDATE check_backs SET checks=checks+1,next_check_at=?,checked_at=?,"
                    "met_at=? WHERE id=? AND state='pending'",
                    (
                        now + self._interval(condition["kind"], row["checks"]),
                        now,
                        now if local.get(row["id"]) else None,
                        row["id"],
                    ),
                )
        for row_id, condition in pages:
            self.run_check(lambda row_id=row_id, condition=condition: self._read(row_id, condition))

    def _read(self, row_id: str, condition: dict) -> None:
        """Read a watched page, away from the scheduler, and keep what it showed."""
        try:
            try:
                page, whole = self.read_page(condition["url"])
            except Conflict as exc:
                met, error = False, str(exc)[:300]
            else:
                shown = page_shows(page, condition["text"])
                if condition["kind"] == "page_contains":
                    met, error = shown, None
                else:
                    # Text missing from a page read only in part may be further down.
                    met = not shown and whole
                    error = (
                        None
                        if met or shown
                        else "The page is longer than can be read whole, so the text may be "
                        "further down."
                    )
            with self.store.connect(write=True) as db:
                if met:
                    db.execute(
                        "UPDATE check_backs SET met_at=?,check_error=NULL "
                        "WHERE id=? AND state='pending'",
                        (self.clock(), row_id),
                    )
                else:
                    db.execute(
                        "UPDATE check_backs SET check_error=? WHERE id=? AND state='pending'",
                        (error, row_id),
                    )
        finally:
            with self._lock:
                self._reading.discard(row_id)

    def _fire(self, now: float) -> int:
        due = (
            f"SELECT c.* FROM check_backs c {self._LIVE} AND (c.met_at IS NOT NULL OR c.due_at<=?)"
        )
        with self.store.connect() as db:
            if not db.execute(due + " LIMIT 1", (self.owner, now)).fetchone():
                return 0
        started = 0
        with self.store.connect(write=True) as db:
            for row in db.execute(
                due + " ORDER BY coalesce(c.met_at,c.due_at),c.id", (self.owner, now)
            ).fetchall():
                if Store._queue_full(db, self.owner):
                    break  # It starts once some work finishes; nothing is lost meanwhile.
                self._start(db, row, now)
                started += 1
        return started

    def _start(self, db, row, now: float) -> None:
        """Queue the turn and save the start with it, in the caller's transaction."""
        condition = json.loads(row["condition"]) if row["condition"] else None
        met = row["met_at"] is not None or (
            condition is not None
            and condition["kind"] not in PAGE_KINDS
            and self._local_holds(db, row, condition)
        )
        outcome = "met" if met else "deadline" if condition else "time"
        late = now - row["due_at"] if not met else 0
        start = {
            "id": row["id"],
            "note": row["note"],
            "outcome": outcome,
            "set_at": row["created_at"],
            "due_at": row["due_at"],
            **({"condition": condition} if condition else {}),
            **({"late_by_seconds": int(late)} if late > LATE_SECONDS else {}),
            **(
                {"detail": row["check_error"]}
                if outcome == "deadline" and row["check_error"]
                else {}
            ),
        }
        request_id = REQUEST_PREFIX + row["id"]
        previous = db.execute(
            "SELECT id FROM tasks WHERE owner=? AND request_id=?", (self.owner, request_id)
        ).fetchone()
        if previous:
            task_id = previous["id"]
        else:
            bot = self.store._bot(db, self.owner, row["bot_id"])
            task_id = uid()
            db.execute(
                "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    row["chat_id"],
                    bot["id"],
                    self.owner,
                    request_id,
                    "",
                    "queued",
                    encode(
                        {
                            **bot,
                            "reply_review_enabled": self.store._reply_review_enabled(
                                db, self.owner
                            ),
                            "check_back": start,
                        }
                    ),
                    now,
                    now,
                ),
            )
            self.store._event(db, task_id, "queued", {})
            self.store._event(
                db, task_id, "checked_back", {k: start[k] for k in start if k != "set_at"}
            )
        db.execute(
            "UPDATE check_backs SET state='starting',task_id=?,outcome=?,updated_at=? WHERE id=?",
            (task_id, outcome, now, row["id"]),
        )
