from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import failures
from .assistant_profile import ASSISTANT_PROFILE
from .browser_tools import STEP_VARIANTS
from .file_revisions import migrate as migrate_file_revisions
from .files import FILE_CHUNK_BYTES, INLINE_FILE_BYTES, MAX_FILE_BYTES, FileQuery
from .models import (
    ASSIGNMENT_FILES,
    INTERACTIVE_HTML_BYTES,
    AssignmentInput,
    AssignmentVerification,
    AssistantAssignmentApproval,
    AssistantOwlCreateInput,
    AssistantOwlDraftInput,
    AssistantScreenRequest,
    BotCreateInput,
    BotDuplicateInput,
    BotInput,
    BotOrganizeInput,
    ConversationPageQuery,
    FileInput,
    HistoryQuery,
    InteractiveStateWrite,
    InteractiveTarget,
    IntroInput,
    MemoryInput,
    NoteConsolidation,
    NoteInput,
    OwlAssignmentProposal,
    RegenerateInput,
    ReplyReviewSettings,
    TaskInput,
    TimeZoneSettings,
)
from .object_storage import ObjectRef, ObjectStorageError
from .regenerate import persons_request, regenerates
from .routine_access import RoutineAccessDecision, RoutineNeeds, approval_reason, parse_needs
from .scheduling import (
    CATCH_UP_SECONDS,
    FAILED_RUNS_BEFORE_PAUSE,
    NEEDS_PERSON,
    ON_TIME_SECONDS,
    PAUSE_REASONS,
    RETRY_DELAYS_SECONDS,
    AssignmentScheduleInput,
    RunFailure,
    ScheduleInput,
    ScheduleModelChoice,
    ScheduleProposal,
    Timing,
    advance_run,
    latest_run,
    next_run,
    retry_time,
    upcoming_runs,
)
from .schema import SCHEMA_VERSION, STEER_COLUMNS, STEER_INDEX, schema
from .task_events import DRAFT_SETTLED_BY, REPLY_DRAFT, REPLY_DRAFT_WITHDRAWN, TaskSignals
from .text_search import excerpt, loose_contains, loose_fold

ACTIVE = ("queued", "running", "waiting", "cancelling")
# What a reserved model call was for, as its model_started event says, so a turn's time and
# spend can be split by part: the turn's own steps, the checks around them and the work beside
# them. Fixed tokens, never model or person text.
CALL_PURPOSES = frozenset(
    {
        "turn",  # a step of the owl's own turn (runtime.py)
        "action_review",  # the automatic action reviewer (action_review.py)
        "reply_check",  # the reply checker (reply_review.py)
        "history_summary",  # the summary of earlier chats (context_summary.py)
        "progress_summary",  # the progress record of a long turn (context_summary.py)
        "memory_index",  # sorting saved memories for retrieval (memory_applicability.py)
        "room_routing",
        "research_helper",  # a research helper's own step (research_helpers.py)
        "image",  # making or editing a picture (owl_images.py)
        "computer_work",  # a step of work on the owl's computer (execution.py)
        "work_compaction",  # compacting that work's context (execution.py)
        "coding",  # a coding job's step (coding.py)
        "peer",  # another owl answering a peer request (cooperation.py)
        "assignment",  # an assigned owl working on a task the person approved (runtime.py)
    }
)
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
ASSIGNMENT_PAGE_SIZE = 20
ASSIGNMENT_PAGE_BYTES = 2 * 1024 * 1024
ASSIGNMENT_RESULT_BYTES = 128 * 1024

_BOT_SELECT = (
    "SELECT b.*, a.bot_id IS NOT NULL AS managed_assistant, c.bot_id=b.id AS assistant_enabled "
    "FROM bots b LEFT JOIN owl_assistants a ON a.owner=b.owner AND a.bot_id=b.id "
    "LEFT JOIN owl_coordinators c ON c.owner=a.owner "
)
# SQL on ``schedule_runs r``: when the run checked and found nothing new to report (its task's
# nothing_new event), or NULL.
NOTHING_NEW_AT = (
    "(SELECT max(q.created_at) FROM events q WHERE q.task_id=r.task_id "
    "AND q.kind='nothing_new') AS nothing_new_at"
)
MAX_OWLS = 30
# Room chats (rooms.py): rows of rooms, whose ids are their chats' ids.
_ROOM_CHATS = "SELECT id FROM rooms"


@dataclass(frozen=True)
class ChatScope:
    """The messages and turns one conversation reads: an owl's own (all its chats but rooms), or a
    room's (its one chat, with every owl's turns in it). ``value`` binds the SQL's one parameter:
    the owl's id, or the room's."""

    value: str
    room: bool = False
    thread_bot: str | None = None
    default_chat: bool = False

    @property
    def chats(self) -> str:
        """SQL on ``chats c``."""
        if self.room or self.thread_bot:
            return "c.id=?"
        clause = f"c.bot_id=? AND c.id NOT IN ({_ROOM_CHATS})"
        return (
            clause + " AND c.id NOT IN (SELECT chat_id FROM owl_chat_threads)"
            if self.default_chat
            else clause
        )

    @property
    def tasks(self) -> str:
        """SQL on ``tasks t``. A room's turns are its owner's: those of an owl deleted since are
        tombstones."""
        if self.room:
            return "t.chat_id=? AND t.owner=(SELECT r.owner FROM rooms r WHERE r.id=t.chat_id)"
        if self.thread_bot:
            return "t.chat_id=?"
        clause = f"t.bot_id=? AND t.chat_id NOT IN ({_ROOM_CHATS})"
        return (
            clause + " AND t.chat_id NOT IN (SELECT chat_id FROM owl_chat_threads)"
            if self.default_chat
            else clause
        )


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BackgroundInstructionsPending(Conflict):
    """A read-only model step was not admitted; refresh exact user updates."""


def _access_needs(access: dict) -> RoutineNeeds:
    """The computer, browser and websites a computer task's card lists, as a routine's needs."""
    return RoutineNeeds(
        offline_computer=bool(access.get("offline_computer")),
        online_browser=bool(access.get("online_browser")),
        sites=access.get("sites") or [],
    )


class AssignmentRoutineBlocked(Conflict):
    def __init__(self, code: str):
        self.code = code
        super().__init__(PAUSE_REASONS[code])


class LimitReached(Exception):
    pass


class LibraryCapacityExceeded(LimitReached):
    """A definite admission refusal: no Library file was published."""


# One Library file is at most MAX_FILE_BYTES (50 MiB).
FILE_TOO_LARGE = (
    "Nothing was saved: this file is larger than 50 MiB, the most one Library file can hold. "
    "Save a smaller file, or split the content into several files."
)


def uid() -> str:
    return uuid4().hex


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def public_task(task: dict) -> dict:
    """Drop the worker's DOM locator and geometry when projecting its task for chat."""
    if "interactive_target" in task.get("config", {}):
        return {
            **task,
            "config": {k: v for k, v in task["config"].items() if k != "interactive_target"},
        }
    return task


# The owl reads the brief; steps the person taught travel at its end, numbered in order. The
# desktop app finds them by this heading to show them as a list, so the wording stays as it is.
TAUGHT_STEPS_HEADING = "Steps the person showed or described, in order:"


def brief_with_steps(brief: str, steps: list[str]) -> str:
    """The brief an owl receives: the approved text, then any taught steps as a numbered list."""
    if not steps:
        return brief
    lines = "\n".join(f"{number}. {' '.join(step.split())}" for number, step in enumerate(steps, 1))
    return f"{brief}\n\n{TAUGHT_STEPS_HEADING}\n{lines}"


class _Connection(sqlite3.Connection):
    """A store connection. ``event_tasks``: the tasks its transaction wrote events for, whose
    followers wake once it commits (task_events.py)."""

    event_tasks: set[str]


class Store:
    """Single-host transactional store. Each operation owns its connection.

    Ownership is checked inside the same transaction as every mutation. A single
    service process holds an OS lock for the worker lifetime; SQLite transactions
    also make request deduplication and task claiming safe across API threads.
    """

    shared = False

    def __init__(self, path: Path, *, objects=None) -> None:
        self.path = path
        self.objects = objects
        self.task_signals = TaskSignals()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(path) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
            if setup.execute("PRAGMA user_version").fetchone()[0] not in range(SCHEMA_VERSION + 1):
                raise RuntimeError("Unsupported Bots database version; use a compatible runtime.")
            migrate_file_revisions(setup)
            from .checkpoint_migration import migrate_checkpoints

            migrate_checkpoints(setup)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(SCHEMA_VERSION + 1):
                raise RuntimeError("Unsupported Bots database version; use a compatible runtime.")
            db.executescript(schema())
            if "work_scope" not in {
                row[1] for row in db.execute("PRAGMA table_info(owl_assignments)")
            }:
                db.execute(
                    "ALTER TABLE owl_assignments ADD COLUMN work_scope TEXT NOT NULL DEFAULT 'text'"
                )
            if any(
                row["name"] == "max_active" and row["notnull"]
                for row in db.execute("PRAGMA table_info(computer_capacity)")
            ):
                db.execute("BEGIN IMMEDIATE")
                db.execute("ALTER TABLE computer_capacity RENAME TO previous_computer_capacity")
                db.execute(
                    "CREATE TABLE computer_capacity (scope TEXT PRIMARY KEY, max_active INTEGER CHECK(max_active > 0))"
                )
                db.execute("INSERT INTO computer_capacity SELECT * FROM previous_computer_capacity")
                db.execute("DROP TABLE previous_computer_capacity")
            db.execute(
                "CREATE INDEX IF NOT EXISTS files_series ON files(bot_id,series_id,revision)"
            )
            upload_columns = {row["name"] for row in db.execute("PRAGMA table_info(file_uploads)")}
            if "previous_file_id" not in upload_columns:
                db.execute("ALTER TABLE file_uploads ADD COLUMN previous_file_id TEXT")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(action_proposals)")}
            if "browser_origin" not in columns:
                db.execute("ALTER TABLE action_proposals ADD COLUMN browser_origin TEXT")
            if "offline_computer" not in columns:
                db.execute(
                    "ALTER TABLE action_proposals ADD COLUMN offline_computer INTEGER NOT NULL DEFAULT 0"
                )
            # How a routine's run started: NULL for its schedule, 'retry' or 'manual' (Run now).
            if "origin" not in {
                row["name"] for row in db.execute("PRAGMA table_info(schedule_runs)")
            }:
                db.execute("ALTER TABLE schedule_runs ADD COLUMN origin TEXT")
            # The model a routine's runs think with; NULL: the owl's own.
            if "model" not in {row["name"] for row in db.execute("PRAGMA table_info(schedules)")}:
                db.execute("ALTER TABLE schedules ADD COLUMN model TEXT")
            for table in ("workspace_objects", "browser_states"):
                if "settled" not in {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})")
                }:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1))"
                    )
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "failure" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN failure TEXT")
            if "conversation_visible" not in task_columns:
                db.execute(
                    "ALTER TABLE tasks ADD COLUMN conversation_visible "
                    "INTEGER NOT NULL DEFAULT 1 CHECK(conversation_visible IN (0,1))"
                )
                db.execute(
                    "UPDATE tasks SET conversation_visible=0 "
                    "WHERE id IN (SELECT child_task_id FROM owl_assignments)"
                )
            # How many times a turn a restart cut off was started again (Store.recover).
            if "restarts" not in task_columns:
                db.execute("ALTER TABLE tasks ADD COLUMN restarts INTEGER NOT NULL DEFAULT 0")
            # What a step waiting for the person would do (action_review.py), NULL: not named.
            if "effect" not in {
                row["name"] for row in db.execute("PRAGMA table_info(bot_action_reviews)")
            }:
                db.execute("ALTER TABLE bot_action_reviews ADD COLUMN effect TEXT")
            bot_columns = {row["name"] for row in db.execute("PRAGMA table_info(bots)")}
            for part in ("color", "background", "model", "reasoning_effort"):
                if part not in bot_columns:
                    db.execute(f"ALTER TABLE bots ADD COLUMN {part} TEXT")
            # When the person pinned the owl, and when they archived it; NULL: not now.
            for part in ("pinned_at", "archived_at"):
                if part not in bot_columns:
                    db.execute(f"ALTER TABLE bots ADD COLUMN {part} REAL")
            # A waiting message sent now to the running turn, and how it went (steering.py).
            queued_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(queued_messages)")
            }
            for part, kind in STEER_COLUMNS:
                if part not in queued_columns:
                    db.execute(f"ALTER TABLE queued_messages ADD COLUMN {part} {kind}")
            db.execute(STEER_INDEX)
            if objects is not None:
                db.execute("INSERT INTO object_storage_enabled VALUES(1) ON CONFLICT DO NOTHING")
            elif db.execute("SELECT 1 FROM object_storage_enabled").fetchone():
                raise ObjectStorageError("This database requires its configured object storage.")
        if version < SCHEMA_VERSION:
            with self.connect(write=True) as db:
                if version < 56:
                    self._publish_existing_assistant_handoffs(db)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        path.chmod(0o600)

    @staticmethod
    def _publish_existing_assistant_handoffs(db) -> None:
        """Show approved Assistant handoffs in the recipient's chat, including older replies."""
        db.execute(
            "UPDATE tasks SET conversation_visible=1 WHERE conversation_visible=0 AND id IN ("
            "SELECT a.child_task_id FROM owl_assignments a "
            "JOIN tasks source ON source.id=a.source_task_id "
            "JOIN owl_assistants assistant ON assistant.bot_id=source.bot_id AND assistant.owner=source.owner "
            "WHERE a.decision='approved' "
            "AND NOT EXISTS(SELECT 1 FROM schedule_runs r WHERE r.task_id=a.child_task_id))"
        )
        db.execute(
            "INSERT INTO messages(id,chat_id,task_id,role,content,created_at) "
            "SELECT t.id,t.chat_id,t.id,'assistant',t.output,t.updated_at FROM tasks t "
            "JOIN owl_assignments a ON a.child_task_id=t.id "
            "WHERE t.conversation_visible=1 AND t.status='completed' AND t.output<>'' "
            "AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.task_id=t.id AND m.role='assistant') "
            "ON CONFLICT(id) DO NOTHING"
        )

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None, factory=_Connection)
        db.event_tasks = set()
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.create_function(
            "byte_length", 1, lambda text: len(text.encode("utf-8")), deterministic=True
        )
        db.create_function(
            "unicode_fold",
            1,
            lambda text: unicodedata.normalize("NFC", text).casefold(),
            deterministic=True,
        )
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        self.task_signals.notify(db.event_tasks)

    @contextmanager
    def _joined(self, within=None) -> Iterator:
        """The caller's open write transaction, or a new one, so several changes land together."""
        if within is not None:
            yield within
            return
        with self.connect(write=True) as db:
            yield db

    @staticmethod
    def executing(db, task: dict) -> bool:
        if task["status"] == "running":
            return True
        return task["status"] == "waiting" and bool(
            db.execute(
                """SELECT 1 FROM background_turns j WHERE j.task_id=? AND j.state='running'
            AND NOT EXISTS (SELECT 1 FROM background_instructions i WHERE i.task_id=j.task_id AND i.seq>j.applied_seq)""",
                (task["id"],),
            ).fetchone()
        )

    @classmethod
    def direct_request(
        cls, db, owner: str, bot_id: str, task_id: str, *, background: bool = False
    ) -> dict:
        """The task, when it is the person's own current request in this bot's chat.

        With ``background``, that request may also be continuing off the reply loop (its
        running background turn); routine runs, activations, an owl's first hello, peer turns,
        turns an owl started by checking back (check_backs.py) and turns waiting for an answer
        never qualify.
        """
        task = cls._task(db, owner, task_id)
        if (
            task["bot_id"] != bot_id
            or not (cls.executing(db, task) if background else task["status"] == "running")
            or task["config"].get("activation")
            or task["config"].get("intro")
            or task["config"].get("assignment_id")
            or task["config"].get("check_back")
            or db.execute("SELECT 1 FROM schedule_runs WHERE task_id=?", (task_id,)).fetchone()
            or db.execute("SELECT 1 FROM task_waits WHERE task_id=?", (task_id,)).fetchone()
            or db.execute(
                "SELECT 1 FROM peer_exchanges WHERE source_task_id=?", (task_id,)
            ).fetchone()
        ):
            raise Conflict("Routine changes require a current user request in this bot's chat.")
        return task

    @staticmethod
    def _bot_view(row) -> dict:
        result = dict(row)
        result.pop("owner")
        managed = result.pop("managed_assistant")
        enabled = result.pop("assistant_enabled")
        if managed:
            result.update(
                ASSISTANT_PROFILE, managed_assistant=True, assistant_enabled=bool(enabled)
            )
        for key in ("memory_enabled", "files_enabled"):
            result[key] = bool(result[key])
        return result

    @classmethod
    def _bot(cls, db: sqlite3.Connection, owner: str, bot_id: str) -> dict:
        row = db.execute(_BOT_SELECT + "WHERE b.id=? AND b.owner=?", (bot_id, owner)).fetchone()
        if row is None:
            raise NotFound("Bot not found.")
        return cls._bot_view(row)

    @staticmethod
    def _task(db: sqlite3.Connection, owner: str, task_id: str) -> dict:
        row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone()
        if row is None:
            raise NotFound("Task not found.")
        result = dict(row)
        result.pop("owner")
        result["config"] = json.loads(result["config"])
        result.pop("conversation_visible")
        # Why a failed or interrupted turn stopped (failures.py); None for older rows.
        result["failure"] = json.loads(result["failure"]) if result.get("failure") else None
        if result["status"] == "waiting":
            wait = db.execute(
                "SELECT state FROM background_turns WHERE task_id=?", (task_id,)
            ).fetchone()
            if wait:
                result["wait_reason"] = wait[0]
                if wait[0] == "local_computer":
                    request = db.execute(
                        "SELECT status FROM bot_local_requests WHERE task_id=? AND status IN ('pending','claimed') ORDER BY created_at DESC,id DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if request:
                        result["wait_reason"] = (
                            "local_approval" if request[0] == "pending" else "local_result"
                        )
        return result

    @classmethod
    def _chat(cls, db: sqlite3.Connection, owner: str, chat_id: str) -> dict:
        row = db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if row is None:
            raise NotFound("Conversation not found.")
        # A room's chat belongs to the room's owner, whichever owl it was made with.
        room = cls._room_row(db, chat_id)
        if room is not None:
            if room["owner"] != owner:
                raise NotFound("Conversation not found.")
        else:
            cls._bot(db, owner, row["bot_id"])
        return dict(row)

    @staticmethod
    def _room_row(db, chat_id: str):
        """The room whose conversation this chat is (rooms.py), or None for an owl's own chat."""
        return db.execute("SELECT * FROM rooms WHERE id=?", (chat_id,)).fetchone()

    @staticmethod
    def _room_member(db, room_id: str, bot_id: str) -> bool:
        return bool(
            db.execute(
                "SELECT 1 FROM room_members WHERE room_id=? AND bot_id=?", (room_id, bot_id)
            ).fetchone()
        )

    def _reply_chat(self, db, owner: str, source: dict) -> str:
        """Where the person's answer to a turn's card carries on: in the room the turn was taken
        in while its owl is still there, otherwise in the owl's own chat."""
        if self._room_row(db, source["chat_id"]) is not None and self._room_member(
            db, source["chat_id"], source["bot_id"]
        ):
            return source["chat_id"]
        if self._is_thread(db, source["chat_id"]):
            self._chat(db, owner, source["chat_id"])
            return source["chat_id"]
        bot = self._bot(db, owner, source["bot_id"])
        return self._ensure_chat(db, owner, bot["id"], bot["name"])["id"]

    @staticmethod
    def _event(db: sqlite3.Connection, task_id: str, kind: str, data: dict) -> None:
        if kind in DRAFT_SETTLED_BY:
            # A withdrawal, or the turn's next state, settles a draft still being checked.
            Store._settle_reply_drafts(db, task_id)
        db.execute(
            "INSERT INTO events(task_id,kind,data,created_at) VALUES(?,?,?,?)",
            (task_id, kind, encode(data), time.time()),
        )
        # Whoever follows the task (task_events.py) wakes once this transaction commits.
        written = getattr(db, "event_tasks", None)
        if written is not None:
            written.add(task_id)

    # An owl still arriving from the person's other place (owl_move.py) stays out of every list
    # until all of it has arrived and been checked.
    ARRIVING = "SELECT bot_id FROM owl_moves WHERE state='receiving' AND bot_id IS NOT NULL"
    # A room's conversation (rooms.py) is a chat of its own, which its owls share: an owl's own
    # conversation leaves out room chats and the turns it took in rooms.
    ROOM_CHATS = _ROOM_CHATS

    def list_bots(self, owner: str, *, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self.connect() as db:
            bots = [
                self._bot(db, owner, row[0])
                for row in db.execute(
                    f"SELECT id FROM bots WHERE owner=? AND id NOT IN ({self.ARRIVING}) "
                    "ORDER BY created_at,id",
                    (owner,),
                )
            ]
            for bot in bots:
                bot["active_tasks"] = db.execute(
                    "SELECT count(*) FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling')",
                    (bot["id"],),
                ).fetchone()[0]
                bot["waiting_tasks"] = db.execute(
                    "SELECT count(*) FROM tasks t JOIN background_turns j ON j.task_id=t.id "
                    "WHERE t.bot_id=? AND t.status='waiting' "
                    "AND j.state IN ('approval','local_approval','local_result','computer_control')",
                    (bot["id"],),
                ).fetchone()[0]
                bot["pending_memories"] = db.execute(
                    "SELECT count(*) FROM memories WHERE bot_id=? AND status='pending'",
                    (bot["id"],),
                ).fetchone()[0]
                activities = self._chat_activities(db, bot["id"])
                bot["unread_chats"] = sum(activity["unread"] for activity in activities)
                latest = max(activities, key=lambda activity: activity["latest_seq"], default=None)
                bot["latest_activity"] = latest["latest"] if latest else None
                bot["now_doing"] = self._now_doing(db, bot["id"])
                bot["asks_you"] = self._asks_you(db, bot)
                bot["next_routine_at"] = self._next_routine_at(db, bot, now)
            return bots

    def _next_routine_at(self, db: sqlite3.Connection, bot: dict, now: float) -> float | None:
        """When the owl's next routine run is due: the earliest next time of a routine that is
        on, or of a failed run's retry. None for an owl without one, and for an archived owl,
        whose routines rest. The desktop app keeps the computer awake for it.

        A time already past still counts: the run starts late, once the scheduler gets to it.
        """
        if bot.get("archived_at") is not None:
            return None
        due: list[float] = []
        for row in db.execute(
            "SELECT s.*,h.retry_at,h.retry_for,h.retry_reason FROM schedules s "
            "LEFT JOIN schedule_health h ON h.schedule_id=s.id AND h.revision=s.revision "
            "WHERE s.bot_id=? AND s.state IN ('active','ended')",
            (bot["id"],),
        ).fetchall():
            schedule = dict(row)
            schedule["timing"] = json.loads(schedule["timing"])
            health = schedule if schedule["retry_at"] is not None else None
            first = self._next_scheduled_run(schedule, health, now)
            if first is not None:
                due.append(first)
            # A retry with nothing to replay is only a hold on the routine's runs.
            if health is not None and health["retry_for"] is not None:
                due.append(health["retry_at"])
        return min(due, default=None)

    @classmethod
    def _next_scheduled_run(
        cls, schedule: dict, health: dict | None, now: float, timing: Timing | None = None
    ) -> float | None:
        """The routine's next scheduled run, if it is on. While a plan or usage limit holds,
        its runs wait: the first one after the limit frees up is next."""
        first = schedule["next_run_at"] if schedule["state"] == "active" else None
        retry_at = health["retry_at"] if health else None
        if (
            first is not None
            and retry_at is not None
            and health["retry_reason"] == "limit"
            and first < retry_at
            and retry_at > now
        ):
            first = advance_run(timing or cls._timing(schedule), first, retry_at)
        return first

    @staticmethod
    def _now_doing(db: sqlite3.Connection, bot_id: str) -> dict | None:
        """What the owl's newest working task is doing now, for one live line in the owl list.

        A task works while it runs in chat or as background work that is queued or running.
        step is the tool now running (the app words it in the person's language) with its
        search topic or file name when it has one, and, only when it names one, variant: which
        kind of step a browser tool that does several took (browser_tools.STEP_VARIANTS). note is
        the owl's latest short update, host the last web address the task opened.
        """
        task = db.execute(
            "SELECT t.id FROM tasks t LEFT JOIN background_turns j ON j.task_id=t.id "
            "WHERE t.bot_id=? AND (t.status='running' OR "
            "(t.status='waiting' AND j.state IN ('queued','running'))) "
            "ORDER BY t.created_at DESC,t.id DESC LIMIT 1",
            (bot_id,),
        ).fetchone()
        if task is None:
            return None
        result = {
            "task_id": task["id"],
            "step": None,
            "topic": None,
            "file": None,
            "note": None,
            "host": None,
        }
        for row in db.execute(
            "SELECT kind,data FROM events WHERE task_id=? AND kind IN ('tool_started','progress_note') "
            "ORDER BY seq DESC LIMIT 50",
            (task["id"],),
        ):
            data = json.loads(row["data"])
            if row["kind"] == "tool_started":
                if result["step"] is None:
                    result["step"] = data.get("name")
                    result["topic"] = data.get("topic")
                    result["file"] = data.get("file")
                    if isinstance(data.get("app_action"), dict):
                        result["app_action"] = data["app_action"]
                    if data.get("variant") in STEP_VARIANTS:
                        result["variant"] = data["variant"]
                if result["host"] is None and data.get("host"):
                    result["host"] = data["host"]
            elif result["note"] is None:
                result["note"] = data.get("text")
        return result

    def update_kinds(self, owner: str, task_id: str) -> list[str]:
        """The kinds of short update this task already sent, oldest first."""
        with self.connect() as db:
            self._task(db, owner, task_id)
            return [
                json.loads(row[0]).get("kind") or "ack"
                for row in db.execute(
                    "SELECT data FROM events WHERE task_id=? AND kind='progress_note' ORDER BY seq",
                    (task_id,),
                )
            ]

    def turn_routines(self, owner: str, task_id: str) -> list[dict]:
        """How the routines this task saved, changed or suggested are now, oldest first.

        The person can turn one on, pause, change or delete it in Routines while the task still
        works, so this is read again, never remembered from the task's own tool results. A
        routine no longer there is "deleted"; a suggestion the person said no to, "declined".
        """
        with self.connect() as db:
            task = self._task(db, owner, task_id)
            ids = [
                json.loads(row[0]).get("id")
                for row in db.execute(
                    "SELECT data FROM events WHERE task_id=? AND kind='routine_changed' "
                    "ORDER BY seq",
                    (task_id,),
                )
            ]
            suggested = {
                row["schedule_id"] or row["id"]: row
                for row in db.execute(
                    "SELECT id,schedule_id,name,status FROM routine_suggestions "
                    "WHERE source_task_id=? AND bot_id=?",
                    (task_id, task["bot_id"]),
                )
            }
            proposed = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM bot_triggers WHERE source_task_id=? AND bot_id=?",
                    (task_id, task["bot_id"]),
                )
            ]
            result = []
            for routine_id in dict.fromkeys([*ids, *suggested, *proposed]):
                if not routine_id:
                    continue
                schedule = db.execute(
                    "SELECT id,name,state,timing FROM schedules WHERE id=? AND bot_id=?",
                    (routine_id, task["bot_id"]),
                ).fetchone()
                if schedule is not None:
                    result.append(
                        {
                            "id": schedule["id"],
                            "kind": "schedule",
                            "name": schedule["name"],
                            "state": schedule["state"],
                            "timing": json.loads(schedule["timing"]),
                        }
                    )
                    continue
                trigger = db.execute(
                    "SELECT id,name,state FROM bot_triggers WHERE id=? AND bot_id=?",
                    (routine_id, task["bot_id"]),
                ).fetchone()
                if trigger is not None:
                    result.append({**dict(trigger), "kind": "trigger"})
                    continue
                card = suggested.get(routine_id)
                result.append(
                    {
                        "id": routine_id,
                        "name": card["name"] if card is not None else None,
                        "state": "declined"
                        if card is not None and card["status"] == "declined"
                        else "deleted",
                    }
                )
            return result

    @staticmethod
    def _asks_you(db: sqlite3.Connection, bot: dict) -> dict | None:
        """The newest question or approval card the owl is waiting on the person for.

        A question counts once the owl's message carrying it is out: while the turn that asked
        it is still writing in the chat, the person has not been asked yet. Background work
        asks at once.
        """
        stopped = "('cancelled','cancelling','failed','interrupted','running')"
        question = db.execute(
            "SELECT c.question,c.updated_at FROM clarifications c JOIN tasks t ON t.id=c.source_task_id "
            f"WHERE c.bot_id=? AND c.status='pending' AND t.status NOT IN {stopped} "
            "ORDER BY c.updated_at DESC,c.id DESC LIMIT 1",
            (bot["id"],),
        ).fetchone()
        approval = db.execute(
            "SELECT p.question,p.updated_at FROM action_proposals p JOIN tasks t ON t.id=p.source_task_id "
            "WHERE p.bot_id=? AND p.status='pending' AND p.visible_after<=? AND p.bot_updated_at=? "
            "AND t.status NOT IN ('cancelled','cancelling','failed','interrupted') "
            "ORDER BY p.updated_at DESC,p.id DESC LIMIT 1",
            (bot["id"], time.time(), bot["updated_at"]),
        ).fetchone()
        # The owl's own setup card, once its reply is out.
        setup = db.execute(
            "SELECT p.purpose AS question,p.updated_at FROM setup_proposals p "
            "JOIN tasks t ON t.id=p.source_task_id WHERE p.bot_id=? AND p.status='pending' "
            "AND p.bot_updated_at=? AND t.status='completed' "
            "ORDER BY p.updated_at DESC,p.id DESC LIMIT 1",
            (bot["id"], bot["updated_at"]),
        ).fetchone()
        newest = max(
            (
                (row["updated_at"], kind, row["question"])
                for kind, row in (
                    ("question", question),
                    ("approval", approval),
                    ("approval", setup),
                )
                if row
            ),
            default=None,
        )
        return {"kind": newest[1], "text": newest[2]} if newest else None

    def bot(self, owner: str, bot_id: str) -> dict:
        with self.connect() as db:
            return self._bot(db, owner, bot_id)

    @staticmethod
    def _insert_bot(db, owner: str, data: BotInput, now: float) -> str:
        bot_id = uid()
        db.execute(
            "INSERT INTO bots(id,owner,name,purpose,instructions,memory_enabled,"
            "files_enabled,created_at,updated_at,color,background,model,reasoning_effort) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                owner,
                data.name,
                data.purpose,
                data.instructions,
                data.memory_enabled,
                data.files_enabled,
                now,
                now,
                data.color,
                data.background,
                data.model,
                data.reasoning_effort,
            ),
        )
        return bot_id

    def save_bot(
        self, owner: str, data: BotInput, bot_id: str | None = None, *, within=None
    ) -> dict:
        with self._joined(within) as db:
            current = self._bot(db, owner, bot_id) if bot_id is not None else None
            if current and current.get("managed_assistant"):
                if any(
                    field in data.model_fields_set and getattr(data, field) != value
                    for field, value in ASSISTANT_PROFILE.items()
                ):
                    raise Conflict("Alysis manages this owl's name, purpose and instructions.")
                data = data.model_copy(
                    update={
                        **ASSISTANT_PROFILE,
                        **{
                            field: current[field]
                            for field in ("memory_enabled", "files_enabled")
                            if field not in data.model_fields_set
                        },
                    }
                )
            now = time.time()
            values = (
                data.name,
                data.purpose,
                data.instructions,
                data.memory_enabled,
                data.files_enabled,
            )
            if current is None:
                request_id = getattr(data, "request_id", None)
                # Only a fingerprint of the settings is kept: the owl itself holds the text.
                setup_hash = hashlib.sha256(encode(values).encode()).hexdigest()
                if request_id:
                    made = db.execute(
                        "SELECT bot_id,setup_hash FROM bot_create_requests WHERE owner=? AND request_id=?",
                        (owner, request_id),
                    ).fetchone()
                    if made:
                        if made["setup_hash"] != setup_hash:
                            raise Conflict(
                                "This request already made an owl with other details. "
                                "Find it in your owl list, or create the owl again."
                            )
                        # The answer to the first create was lost: the same owl, not a second.
                        return self._bot(db, owner, made["bot_id"])
                # Archived owls count: they keep their chats, memories and files.
                if (
                    db.execute(
                        "SELECT count(*) FROM bots b LEFT JOIN owl_assistants a "
                        "ON a.owner=b.owner AND a.bot_id=b.id WHERE b.owner=? AND a.bot_id IS NULL",
                        (owner,),
                    ).fetchone()[0]
                    >= MAX_OWLS
                ):
                    raise LimitReached(
                        f"You can have up to {MAX_OWLS} owls, archived ones included. "
                        "Delete one to make room for a new one."
                    )
                bot_id = self._insert_bot(db, owner, data, now)
                if request_id:
                    db.execute(
                        "INSERT INTO bot_create_requests VALUES(?,?,?,?,?)",
                        (owner, request_id, bot_id, setup_hash, now),
                    )
            else:
                # Its look, like its name, is only a label: it applies at once. A change that
                # leaves a part out (an app from before looks) keeps the one the person picked.
                # Its model applies the same way, from the next turn it starts: work already
                # thinking keeps the model it started with.
                if (
                    "model" in data.model_fields_set
                    and data.model != current["model"]
                    and "reasoning_effort" not in data.model_fields_set
                ):
                    db.execute("UPDATE bots SET reasoning_effort=NULL WHERE id=?", (bot_id,))
                for part in ("color", "background", "model", "reasoning_effort"):
                    if part in data.model_fields_set:
                        db.execute(
                            f"UPDATE bots SET {part}=? WHERE id=?", (getattr(data, part), bot_id)
                        )
                # A new name is only a label, so it applies at once, even while the bot
                # works, and leaves its setup (and anything approved against it) current.
                setup_changed = (
                    current["purpose"],
                    current["instructions"],
                    bool(current["memory_enabled"]),
                    bool(current["files_enabled"]),
                ) != (
                    data.purpose,
                    data.instructions,
                    bool(data.memory_enabled),
                    bool(data.files_enabled),
                )
                if not setup_changed:
                    db.execute("UPDATE bots SET name=? WHERE id=?", (data.name, bot_id))
                    return self._bot(db, owner, bot_id)
                if db.execute(
                    "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling')",
                    (bot_id,),
                ).fetchone():
                    raise Conflict(
                        "Wait for this bot's work to stop before changing its purpose, instructions, memory or files."
                    )
                db.execute(
                    "UPDATE bots SET name=?,purpose=?,instructions=?,memory_enabled=?,files_enabled=?,updated_at=? WHERE id=?",
                    (*values, now, bot_id),
                )
            return self._bot(db, owner, bot_id)

    @staticmethod
    def _check_bot_deletion(db, bot_id: str) -> None:
        if db.execute(
            "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling')",
            (bot_id,),
        ).fetchone():
            raise Conflict("Stop this bot's work before deleting it.")
        if db.execute(
            "SELECT 1 FROM owl_assignments a JOIN tasks parent ON parent.id=a.source_task_id "
            "JOIN tasks child ON child.id=a.child_task_id WHERE parent.bot_id=? "
            "AND child.status IN ('queued','running','waiting','cancelling')",
            (bot_id,),
        ).fetchone():
            raise Conflict("Stop this owl's active assignments before deleting it.")

    def check_bot_deletion(self, owner: str, bot_id: str) -> None:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            self._check_bot_deletion(db, bot_id)

    def delete_bot(self, owner: str, bot_id: str, *, check=None) -> None:
        """Remove a bot and what it holds from its owner's account.

        It leaves its rooms (rooms.py): its turns there are erased like its own chat, and a
        message the person sent it there with other owls stays with theirs.
        Its conversation (messages, their attachments and reactions, activity, tool results),
        files, uploads, memories, playbooks, working notes, questions, proposals, About me
        suggestions, approvals, routines (and those it suggested), check-backs and file events are
        erased now;
        erasing its checkpoint rows lets retention reclaim their stored objects. The bot, chat and
        task rows stay as empty records under a tombstone owner that no account can resolve, because
        model-call accounting and other history still reference them; every
        owner-scoped read (lists, tasks, conversations, routines, peers, limits
        and Library usage) stops seeing them at once. Running work must stop
        first, so nothing keeps acting for a bot that is gone. ``check(db)`` runs first in the
        same transaction and may refuse (a move deletes the original only if it didn't change).
        """
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            if check is not None:
                check(db)
            self._check_bot_deletion(db, bot_id)
            for row in db.execute(
                "SELECT record FROM computer_allocations WHERE bot_id=?", (bot_id,)
            ).fetchall():
                if json.loads(row["record"]).get("state") != "deleted":
                    raise Conflict("Wait for this bot's computer to shut down, then delete it.")
            assistant = self._assistant_record(db, owner)
            if assistant and assistant["bot_id"] == bot_id:
                self._disable_assistant(db, owner, assistant)
                db.execute("DELETE FROM owl_assistants WHERE owner=?", (owner,))
            elif self._is_coordinator(db, owner, bot_id):
                self._change_coordinator(db, owner, None)
            if assistant and assistant["previous_coordinator_id"] == bot_id:
                db.execute(
                    "UPDATE owl_assistants SET previous_coordinator_id=NULL WHERE owner=?", (owner,)
                )
            for routine in db.execute(
                "SELECT s.* FROM schedules s JOIN owl_assignment_routines r ON r.schedule_id=s.id "
                "JOIN owl_assignments a ON a.id=r.assignment_id "
                "WHERE a.recipient_bot_id=? AND s.state='active'",
                (bot_id,),
            ).fetchall():
                self._pause_schedule(
                    db,
                    dict(routine),
                    "assignment_missing",
                    time.time(),
                )
            tasks = "SELECT id FROM tasks WHERE bot_id=?"
            assigned_tasks = (
                f"SELECT child_task_id FROM owl_assignments WHERE source_task_id IN ({tasks})"
            )
            # Its own chats; a room it was made with keeps its chat (rooms.py).
            chats = f"SELECT id FROM chats WHERE bot_id=? AND id NOT IN ({self.ROOM_CHATS})"
            notes = "SELECT id FROM working_notes WHERE bot_id=?"
            from .rooms import leave_rooms

            # It leaves its rooms: its turns there go with it, the person's messages it shared
            # with other owls stay (rooms.py).
            leave_rooms(db, bot_id)
            # Children before parents, so foreign keys hold at every step.
            for statement, count in (
                ("DELETE FROM reply_reactions WHERE bot_id=?", 1),
                (f"DELETE FROM messages WHERE chat_id IN ({chats})", 1),
                (f"DELETE FROM messages WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM room_turns WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM chat_read_cursors WHERE chat_id IN ({chats})", 1),
                (f"DELETE FROM events WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM events WHERE task_id IN ({assigned_tasks})", 1),
                (
                    f"UPDATE tasks SET output='',error=NULL,failure=NULL WHERE id IN ({assigned_tasks})",
                    1,
                ),
                (f"DELETE FROM task_tool_results WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM task_files WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM queued_messages WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM browser_task_drafts WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM task_waits WHERE task_id IN ({tasks})", 1),
                (
                    f"DELETE FROM background_instructions WHERE task_id IN ({tasks}) "
                    f"OR source_task_id IN ({tasks})",
                    2,
                ),
                (f"DELETE FROM background_turns WHERE task_id IN ({tasks})", 1),
                ("DELETE FROM clarifications WHERE bot_id=?", 1),
                ("DELETE FROM bot_app_needs WHERE bot_id=?", 1),
                ("DELETE FROM bot_plugin_grants WHERE bot_id=?", 1),
                ("DELETE FROM action_proposals WHERE bot_id=?", 1),
                ("DELETE FROM about_me_suggestions WHERE bot_id=?", 1),
                ("DELETE FROM setup_proposals WHERE bot_id=?", 1),
                ("DELETE FROM routine_suggestions WHERE bot_id=?", 1),
                ("DELETE FROM bot_action_reviews WHERE bot_id=?", 1),
                ("DELETE FROM bot_action_rules WHERE bot_id=?", 1),
                ("DELETE FROM bot_review_settings WHERE bot_id=?", 1),
                ("DELETE FROM bot_context_summaries WHERE bot_id=?", 1),
                ("DELETE FROM memory_capture_state WHERE bot_id=?", 1),
                (
                    f"DELETE FROM note_consolidations WHERE source_note_id IN ({notes}) "
                    f"OR target_note_id IN ({notes})",
                    2,
                ),
                (f"DELETE FROM note_revisions WHERE note_id IN ({notes})", 1),
                ("DELETE FROM working_notes WHERE bot_id=?", 1),
                ("DELETE FROM memory_changes WHERE bot_id=?", 1),
                ("DELETE FROM memories WHERE bot_id=?", 1),
                ("DELETE FROM playbooks WHERE bot_id=?", 1),
                ("DELETE FROM check_backs WHERE bot_id=?", 1),
                ("DELETE FROM files WHERE bot_id=?", 1),
                ("DELETE FROM file_uploads WHERE bot_id=?", 1),
                (
                    "DELETE FROM schedule_runs WHERE schedule_id IN "
                    "(SELECT id FROM schedules WHERE bot_id=?)",
                    1,
                ),
                ("DELETE FROM schedule_requests WHERE bot_id=?", 1),
                ("DELETE FROM schedule_run_requests WHERE bot_id=?", 1),
                (
                    "DELETE FROM schedule_health WHERE schedule_id IN "
                    "(SELECT id FROM schedules WHERE bot_id=?)",
                    1,
                ),
                (
                    "DELETE FROM routine_access WHERE schedule_id IN "
                    "(SELECT id FROM schedules WHERE bot_id=?)",
                    1,
                ),
                ("DELETE FROM schedules WHERE bot_id=?", 1),
                (
                    "DELETE FROM trigger_deliveries WHERE trigger_id IN "
                    "(SELECT id FROM bot_triggers WHERE bot_id=?) OR event_id IN "
                    "(SELECT id FROM activation_events WHERE bot_id=?)",
                    2,
                ),
                ("DELETE FROM bot_triggers WHERE bot_id=?", 1),
                ("DELETE FROM activation_events WHERE bot_id=?", 1),
                (
                    "DELETE FROM bot_local_admissions WHERE request_id IN "
                    "(SELECT id FROM bot_local_requests WHERE bot_id=?)",
                    1,
                ),
                ("DELETE FROM bot_local_requests WHERE bot_id=?", 1),
                ("DELETE FROM bot_computer_control WHERE bot_id=?", 1),
                ("DELETE FROM workspace_objects WHERE bot_id=?", 1),
                ("DELETE FROM browser_states WHERE bot_id=?", 1),
                ("DELETE FROM browser_baselines WHERE bot_id=?", 1),
                (
                    "DELETE FROM coding_calls WHERE job_id IN (SELECT id FROM coding_jobs "
                    f"WHERE task_id IN ({tasks}))",
                    1,
                ),
                (
                    "DELETE FROM coding_instructions WHERE job_id IN (SELECT id FROM coding_jobs "
                    f"WHERE task_id IN ({tasks}))",
                    1,
                ),
                (f"DELETE FROM coding_jobs WHERE task_id IN ({tasks})", 1),
                (
                    "DELETE FROM peer_calls WHERE exchange_id IN "
                    "(SELECT id FROM peer_exchanges WHERE source_bot_id=?)",
                    1,
                ),
                ("DELETE FROM peer_exchanges WHERE source_bot_id=?", 1),
                (
                    f"DELETE FROM owl_assignments WHERE source_task_id IN ({tasks}) "
                    "OR recipient_bot_id=?",
                    2,
                ),
                ("DELETE FROM bot_create_requests WHERE bot_id=?", 1),
            ):
                db.execute(statement, (bot_id,) * count)
            tombstone, now = "deleted:" + uid(), time.time()
            # Another bot's pending request to this one ends now, with a reason it can report.
            db.execute(
                "UPDATE peer_exchanges SET status='failed',error=?,revision=revision+1,updated_at=? "
                "WHERE recipient_bot_id=? AND status IN ('queued','running','waiting')",
                ("The bot it asked was deleted.", now, bot_id),
            )
            db.execute(
                "UPDATE coding_workspaces SET bot_id=NULL,session_id=NULL WHERE bot_id=?",
                (bot_id,),
            )
            db.execute(
                "UPDATE tasks SET owner=?,message='',output='',error=NULL,failure=NULL,config='{}',updated_at=? "
                "WHERE bot_id=?",
                (tombstone, now, bot_id),
            )
            db.execute(
                f"UPDATE chats SET title='' WHERE bot_id=? AND id NOT IN ({self.ROOM_CHATS})",
                (bot_id,),
            )
            db.execute(
                "UPDATE bots SET owner=?,name='',purpose='',instructions='',color=NULL,background=NULL,"
                "model=NULL,pinned_at=NULL,archived_at=NULL,updated_at=? WHERE id=?",
                (tombstone, now, bot_id),
            )

    def organize_bot(
        self, owner: str, bot_id: str, data: BotOrganizeInput, *, now: float | None = None
    ) -> dict:
        """Pin an owl to the top of the list or archive it, and back; a part left out stays.

        Both are places in the person's list, not its setup: updated_at stays, so nothing
        approved against the owl's setup goes out of date. Pinning again keeps the time it
        was first pinned, so the pinned owls keep their order. An archived owl keeps
        everything, but its routines and file events rest: it can't be archived while it
        works, and file events waiting to start are dropped, never replayed. When it comes
        back, its routines go on from their next time (_bring_back).
        """
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            bot = self._bot(db, owner, bot_id)
            if data.pinned is not None:
                db.execute(
                    "UPDATE bots SET pinned_at=? WHERE id=?",
                    ((bot["pinned_at"] or now) if data.pinned else None, bot_id),
                )
            if data.archived and bot["archived_at"] is None:
                if db.execute(
                    "SELECT 1 FROM tasks WHERE bot_id=? AND status IN "
                    "('queued','running','waiting','cancelling') LIMIT 1",
                    (bot_id,),
                ).fetchone():
                    raise Conflict("Stop this owl's work before archiving it.")
                db.execute(
                    "UPDATE trigger_deliveries SET state='skipped',reason=? WHERE state='pending' "
                    "AND trigger_id IN (SELECT id FROM bot_triggers WHERE bot_id=?)",
                    ("The owl was archived; files added before will not start it.", bot_id),
                )
                db.execute("UPDATE bots SET archived_at=? WHERE id=?", (now, bot_id))
            elif data.archived is False:
                self._bring_back(db, bot_id, now)
            return self._bot(db, owner, bot_id)

    def _bring_back(self, db, bot_id: str, now: float) -> None:
        """An archived owl back in the list: its routines go on from their next time.

        Runs that came due while it was archived, and retries that came due then, are not
        replayed: the person archived it so it would rest. Nothing else changes, so routines
        the person paused stay paused. Work the person starts for an archived owl (a message,
        an answer, an approval, Run now) brings it back, so it never works out of sight.
        """
        if not db.execute(
            "UPDATE bots SET archived_at=NULL WHERE id=? AND archived_at IS NOT NULL", (bot_id,)
        ).rowcount:
            return
        db.execute(
            "UPDATE app_event_watches SET cursor_at=?,next_poll=?,window_end=NULL,page_token='' "
            "WHERE trigger_id IN (SELECT id FROM bot_triggers WHERE bot_id=? AND state='active')",
            (now, now + 900, bot_id),
        )
        for row in db.execute(
            "SELECT id FROM schedules WHERE bot_id=? AND state='active' AND next_run_at<=?",
            (bot_id, now),
        ).fetchall():
            schedule = self._schedule(db, bot_id, row["id"])
            following = advance_run(self._timing(schedule), schedule["next_run_at"], now)
            db.execute(
                "UPDATE schedules SET next_run_at=?,state=?,updated_at=? WHERE id=?",
                (following, "active" if following else "ended", now, schedule["id"]),
            )
        for row in db.execute(
            "SELECT s.id FROM schedule_health h JOIN schedules s ON s.id=h.schedule_id "
            "WHERE s.bot_id=? AND h.revision=s.revision AND s.state IN ('active','ended') "
            "AND h.retry_at<=?",
            (bot_id, now),
        ).fetchall():
            schedule = self._schedule(db, bot_id, row["id"])
            health = self._schedule_health(db, schedule)
            self._save_schedule_health(
                db,
                schedule["id"],
                schedule["revision"],
                now,
                failures=health["failures"],
                pause_code=health["pause_code"],
            )

    def duplicate_bot(self, owner: str, bot_id: str, data: BotDuplicateInput) -> dict:
        """A new owl like this one, made through the create path in one transaction.

        It gets the same purpose, instructions, memory and files switches, look, apps it may
        use and approval mode, and the approved memories when asked. Never its chats, files,
        routines, sign-ins or list place: it starts unpinned in the main list. The same
        request_id returns the copy that request made, so a retried duplicate makes one owl.
        """
        with self.connect(write=True) as db:
            source = self._bot(db, owner, bot_id)
            made = db.execute(
                "SELECT 1 FROM bot_create_requests WHERE owner=? AND request_id=?",
                (owner, data.request_id),
            ).fetchone()
            copy = self.save_bot(
                owner,
                BotCreateInput(
                    name=data.name,
                    purpose=source["purpose"],
                    instructions=source["instructions"],
                    memory_enabled=source["memory_enabled"],
                    files_enabled=source["files_enabled"],
                    color=source["color"],
                    background=source["background"],
                    model=source["model"],
                    reasoning_effort=source["reasoning_effort"],
                    request_id=data.request_id,
                ),
                within=db,
            )
            if made:
                # The answer to the first duplicate was lost: the copy it made, as it is now.
                return copy
            now = time.time()
            for row in db.execute(
                "SELECT plugin_id,enabled FROM bot_plugin_grants WHERE bot_id=?", (bot_id,)
            ).fetchall():
                db.execute(
                    "INSERT INTO bot_plugin_grants(bot_id,plugin_id,enabled,revision,updated_at) "
                    "VALUES(?,?,?,1,?)",
                    (copy["id"], row["plugin_id"], row["enabled"], now),
                )
            mode = db.execute(
                "SELECT mode FROM bot_review_settings WHERE bot_id=?", (bot_id,)
            ).fetchone()
            if mode:
                db.execute(
                    "INSERT INTO bot_review_settings VALUES(?,?)", (copy["id"], mode["mode"])
                )
            if data.memories:
                # Approved memories only, in their order; suggestions wait for review on the
                # original. They are the person's own in the copy: no task of its own made them.
                for row in db.execute(
                    "SELECT title,content,created_at FROM memories WHERE bot_id=? "
                    "AND status='active' ORDER BY created_at,id",
                    (bot_id,),
                ).fetchall():
                    memory_id = uid()
                    db.execute(
                        "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)",
                        (
                            memory_id,
                            copy["id"],
                            row["title"],
                            row["content"],
                            "active",
                            None,
                            row["created_at"],
                            now,
                        ),
                    )
                    self._memory_change(db, copy["id"], memory_id, "created", None, "active")
            return self._bot(db, owner, copy["id"])

    def create_chat(
        self, owner: str, bot_id: str, title: str, *, thread_request_id: str | None = None
    ) -> dict:
        with self.connect(write=True) as db:
            default = self._ensure_chat(
                db, owner, bot_id, title if not thread_request_id else "New conversation"
            )
            if not thread_request_id:
                return default
            existing = db.execute(
                "SELECT chat_id FROM owl_chat_threads WHERE bot_id=? AND request_id=?",
                (bot_id, thread_request_id),
            ).fetchone()
            if existing:
                return self._chat(db, owner, existing[0])
            chat_id = uid()
            db.execute("INSERT INTO chats VALUES(?,?,?,?)", (chat_id, bot_id, title, time.time()))
            db.execute(
                "INSERT INTO owl_chat_threads VALUES(?,?,?)", (chat_id, bot_id, thread_request_id)
            )
            return self._chat(db, owner, chat_id)

    @staticmethod
    def _is_thread(db, chat_id: str) -> bool:
        return (
            db.execute("SELECT 1 FROM owl_chat_threads WHERE chat_id=?", (chat_id,)).fetchone()
            is not None
        )

    def separate_thread(self, chat_id: str) -> bool:
        with self.connect() as db:
            return self._is_thread(db, chat_id)

    @classmethod
    def _ensure_chat(cls, db: sqlite3.Connection, owner: str, bot_id: str, title: str) -> dict:
        cls._bot(db, owner, bot_id)
        # One stable conversation per bot. Keep legacy chat rows and source IDs;
        # reads project their messages into this same timeline without rewriting data.
        row = cls._own_chat(db, bot_id)
        if row is not None:
            return dict(row)
        chat_id = uid()
        db.execute("INSERT INTO chats VALUES(?,?,?,?)", (chat_id, bot_id, title, time.time()))
        return cls._chat(db, owner, chat_id)

    @classmethod
    def _own_chat(cls, db, bot_id: str):
        """The first chat of the owl's own conversation (never a room's), or None."""
        return db.execute(
            f"SELECT * FROM chats WHERE bot_id=? AND id NOT IN ({cls.ROOM_CHATS}) "
            "AND id NOT IN (SELECT chat_id FROM owl_chat_threads) "
            "ORDER BY created_at,id LIMIT 1",
            (bot_id,),
        ).fetchone()

    @classmethod
    def _scope(cls, db, owner: str, chat_id: str) -> tuple[dict, ChatScope]:
        """The chat, checked to be the owner's, and the conversation it belongs to."""
        chat = cls._chat(db, owner, chat_id)
        if cls._room_row(db, chat_id) is not None:
            return chat, ChatScope(chat_id, room=True)
        if cls._is_thread(db, chat_id):
            return chat, ChatScope(chat_id, thread_bot=chat["bot_id"])
        return chat, ChatScope(chat["bot_id"], default_chat=True)

    @classmethod
    def _chat_activities(cls, db: sqlite3.Connection, bot_id: str) -> list[dict]:
        """Use event order, not client clocks, for receipts and unread state."""
        chat = cls._own_chat(db, bot_id)
        if chat is None:
            return []
        activities = [cls._activity(db, dict(chat), ChatScope(bot_id, default_chat=True))]
        for thread in db.execute(
            "SELECT c.* FROM chats c JOIN owl_chat_threads x ON x.chat_id=c.id WHERE x.bot_id=? ORDER BY c.created_at DESC,c.id",
            (bot_id,),
        ):
            activities.append(
                cls._activity(db, dict(thread), ChatScope(thread["id"], thread_bot=bot_id))
            )
        return activities

    @staticmethod
    def _activity(db, chat: dict, scope: ChatScope) -> dict:
        """A conversation's newest activity, what the person read of it, and its last line."""
        from .queued_messages import not_withdrawn

        # Per task: its newest event and its newest finishing event, each one index lookup. The
        # owl list asks this for every owl every few seconds, so it must not read the events
        # themselves, which grow with every step an owl ever took. A message the person
        # cancelled before it started leaves the activity as it was before it was sent.
        row = db.execute(
            f"""SELECT coalesce(max(x.latest),0) AS latest_seq,
                      coalesce(max(x.finished),0) AS activity_seq,
                      coalesce(max(CASE WHEN x.finished>coalesce(r.event_seq,0) THEN 1 ELSE 0 END),0)
                        AS unread
               FROM (SELECT t.chat_id,
                            (SELECT max(e.seq) FROM events e WHERE e.task_id=t.id) AS latest,
                            (SELECT max(e.seq) FROM events e WHERE e.task_id=t.id AND e.kind IN
                              ('completed','failed','cancelled','interrupted','assignment_review')) AS finished
                     FROM tasks t WHERE {scope.tasks} AND {not_withdrawn("t")} AND t.conversation_visible=1) x
               LEFT JOIN chat_read_cursors r ON r.chat_id=x.chat_id""",
            (scope.value,),
        ).fetchone()
        item = {**chat, **dict(row)}
        latest = db.execute(
            """SELECT e.seq,e.created_at,t.id AS task_id,t.chat_id,t.status,t.bot_id,
                      EXISTS(SELECT 1 FROM owl_assignments a WHERE a.child_task_id=t.id)
                        AS assistant_handoff,
                      substr(CASE WHEN t.status='completed' THEN t.output ELSE t.message END,1,240) AS preview
               FROM events e JOIN tasks t ON t.id=e.task_id WHERE e.seq=?""",
            (item["latest_seq"],),
        ).fetchone()
        latest = dict(latest) if latest else None
        if latest and latest["status"] == "completed":
            # A routine's run that found nothing new has no reply to show as the last line.
            quiet = db.execute(
                "SELECT max(created_at) FROM events WHERE task_id=? AND kind='nothing_new'",
                (latest["task_id"],),
            ).fetchone()[0]
            if quiet is not None:
                latest["nothing_new_at"] = quiet
        # In a room the last line says which owl it is from; an owl's own chat is all its own.
        if latest and not scope.room:
            latest.pop("bot_id")
        handoff = latest.pop("assistant_handoff") if latest else False
        item["latest"] = (
            {
                **latest,
                "title": item["title"],
                **({"assistant_handoff": True} if handoff else {}),
            }
            if latest
            else None
        )
        item["unread"] = bool(item["unread"])
        return item

    @staticmethod
    def _unread(db, chat_id: str) -> bool:
        """Whether a conversation has a finished turn after what the person last read in it."""
        from .queued_messages import not_withdrawn

        return bool(
            db.execute(
                f"""SELECT 1 FROM tasks t JOIN events e ON e.task_id=t.id
                LEFT JOIN chat_read_cursors r ON r.chat_id=t.chat_id
                WHERE t.chat_id=? AND {not_withdrawn("t")}
                AND e.kind IN ('completed','failed','cancelled','interrupted','assignment_review')
                AND e.seq>coalesce(r.event_seq,0) LIMIT 1""",
                (chat_id,),
            ).fetchone()
        )

    def list_chats(self, owner: str, bot_id: str) -> list[dict]:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            chats = []
            for activity in self._chat_activities(db, bot_id):
                chat = {key: activity[key] for key in ("id", "bot_id", "title", "created_at")}
                latest = activity["latest"]
                chat.update(
                    last_task_status=latest["status"] if latest else None,
                    activity_seq=activity["activity_seq"],
                    unread=activity["unread"],
                    last_activity_at=latest["created_at"] if latest else chat["created_at"],
                )
                chats.append(chat)
            return chats

    def mark_chat_read(self, owner: str, chat_id: str, activity_seq: int) -> dict:
        with self.connect(write=True) as db:
            _chat, scope = self._scope(db, owner, chat_id)
            observed = db.execute(
                f"""SELECT 1 FROM events e JOIN tasks t ON t.id=e.task_id
                   WHERE e.seq=? AND {scope.tasks} AND t.conversation_visible=1 AND e.kind IN
                     ('completed','failed','cancelled','interrupted','assignment_review')""",
                (activity_seq, scope.value),
            ).fetchone()
            if not observed:
                raise NotFound("Conversation activity not found.")
            db.execute(
                f"""INSERT INTO chat_read_cursors SELECT c.id,? FROM chats c WHERE {scope.chats} ON CONFLICT(chat_id)
                   DO UPDATE SET event_seq=CASE WHEN chat_read_cursors.event_seq>excluded.event_seq THEN chat_read_cursors.event_seq ELSE excluded.event_seq END""",
                (activity_seq, scope.value),
            )
            return {
                "read_seq": db.execute(
                    "SELECT event_seq FROM chat_read_cursors WHERE chat_id=?", (chat_id,)
                ).fetchone()[0]
            }

    def conversation(self, owner: str, chat_id: str) -> dict:
        with self.connect() as db:
            chat, scope = self._scope(db, owner, chat_id)
            if scope.default_chat:
                chat = dict(self._own_chat(db, chat["bot_id"]))
            chat["messages"] = [
                dict(row)
                for row in db.execute(
                    f"""SELECT m.* FROM messages m JOIN chats c ON c.id=m.chat_id
                    WHERE {scope.chats} ORDER BY m.created_at,m.rowid""",
                    (scope.value,),
                )
            ]
            # The person's own message lists the files they attached to it.
            self._message_file_lists(db, scope, chat["messages"])
            chat["tasks"] = [
                self._task(db, owner, row[0])
                for row in db.execute(
                    f"SELECT t.id FROM tasks t WHERE {scope.tasks} AND t.conversation_visible=1 "
                    "ORDER BY t.created_at,t.id",
                    (scope.value,),
                )
            ]
            self._decorate_scope(db, scope, chat["tasks"])
            activity = self._activity(
                db, {key: chat[key] for key in ("id", "bot_id", "title", "created_at")}, scope
            )
            chat["activity_seq"] = activity["activity_seq"]
            chat["unread"] = activity["unread"]
            chat["read_at"] = self._read_at(db, chat["id"])
            return chat

    @staticmethod
    def _read_at(db, chat_id: str) -> float | None:
        """When the activity the person last read the conversation up to happened, in the same
        clock as its messages, so the app can mark the messages that came after it; None before
        their first read."""
        row = db.execute(
            "SELECT e.created_at FROM chat_read_cursors r JOIN events e ON e.seq=r.event_seq "
            "WHERE r.chat_id=?",
            (chat_id,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _decided_actions(db, bot_id: str, *, only=None) -> dict[str, list[dict]]:
        """The actions a person approved or declined, by task, for one quiet line each in chat.

        Each keeps its tool, arguments and what the owl saw, without the screenshot the review
        card showed: the line describes the action, it does not show the screen again.
        """
        decided: dict[str, list[dict]] = {}
        # ``only`` limits the read to those task ids, as for the other per-task reads.
        scope, values = "", (bot_id,)
        if only is not None:
            scope = f" AND task_id IN ({','.join('?' * len(only))})"
            values = (bot_id, *only)
        for row in db.execute(
            "SELECT id,task_id,status,action,decided_at FROM bot_action_reviews "
            "WHERE bot_id=? AND status IN ('approved','consumed','denied') "
            f"AND decided_at IS NOT NULL{scope} ORDER BY decided_at,id",
            values,
        ):
            action = json.loads(row["action"])
            observed = {
                key: value
                for key, value in (action.get("observed_context") or {}).items()
                if key != "screenshot"
            }
            decided.setdefault(row["task_id"], []).append(
                {
                    "id": row["id"],
                    # "consumed" is an approval that has run; to the person both read "Approved".
                    "status": "denied" if row["status"] == "denied" else "approved",
                    "decided_at": row["decided_at"],
                    "action": {
                        "tool": action.get("tool"),
                        "arguments": action.get("arguments") or {},
                        "observed_context": observed,
                    },
                }
            )
        return decided

    @classmethod
    def _message_file_lists(cls, db, scope: ChatScope, messages: list[dict], *, only=None) -> None:
        attached = cls._message_files(db, scope, only=only)
        selections = {}
        if attached and not scope.room:
            within = "" if only is None else f" AND t.id IN ({','.join('?' * len(only))})"
            for row in db.execute(
                f"SELECT t.id,t.config FROM tasks t WHERE {scope.tasks}{within} AND t.config LIKE ?",
                (scope.value, *(only or ()), '%"interactive_target":{%'),
            ):
                target = json.loads(row["config"]).get("interactive_target")
                if target and any(
                    file["id"] == target["file_id"] for file in attached.get(row["id"], ())
                ):
                    label = " ".join((target.get("element") or {}).get("label", "").split())
                    selections[row["id"]] = {
                        "file_id": target["file_id"],
                        "revision": target["revision"],
                        "label": "" if re.search(r"<[/!A-Za-z]", label) else label[:240],
                    }
                    if marks := target.get("marks"):
                        selections[row["id"]]["labels"] = [
                            ""
                            if re.search(r"<[/!A-Za-z]", mark["element"]["label"])
                            else mark["element"]["label"][:240]
                            for mark in marks
                        ]
        for message in messages:
            if message["role"] == "user":
                message["files"] = attached.get(message["task_id"], [])
                if selection := selections.get(message["task_id"]):
                    message["interactive_selection"] = selection

    @classmethod
    def _decorate_scope(cls, db, scope: ChatScope, tasks: list[dict], *, only=None) -> None:
        """What a conversation shows with each of its tasks (_decorate_tasks). A room's turns
        belong to several owls, each read with its own owl, and carry the cards that wait for
        the person there (rooms.py)."""
        tasks[:] = [public_task(task) for task in tasks]
        if not scope.room:
            cls._decorate_tasks(db, scope.thread_bot or scope.value, tasks, only=only)
            return
        by_owl: dict[str, list[dict]] = {}
        for task in tasks:
            by_owl.setdefault(task["bot_id"], []).append(task)
        for bot_id, owned in by_owl.items():
            cls._decorate_tasks(db, bot_id, owned, only=[task["id"] for task in owned])
        from .rooms import room_cards

        room_cards(db, tasks)

    @classmethod
    def _decorate_tasks(cls, db, bot_id: str, tasks: list[dict], *, only=None) -> None:
        """Each task's coding job, file count, short updates, decided actions, the setup card
        the owl proposed in it (proposals.py), when it started, its message's state while it
        waits and the reactions to its reply, as the conversation shows them.

        ``only`` limits the reads to those task ids, so a page reads only its own tasks.
        """
        scope, values = "t.bot_id=?", (bot_id,)
        if only is not None:
            if not only:
                return
            scope = f"t.bot_id=? AND t.id IN ({','.join('?' * len(only))})"
            values = (bot_id, *only)
        jobs = {
            row["task_id"]: row["id"]
            for row in db.execute(
                f"SELECT j.id,j.task_id FROM coding_jobs j JOIN tasks t ON t.id=j.task_id WHERE {scope}",
                values,
            )
        }
        file_counts = {
            row["source_task_id"]: row["count"]
            for row in db.execute(
                "SELECT f.source_task_id,count(*) AS count FROM files f "
                f"JOIN tasks t ON t.id=f.source_task_id WHERE f.bot_id=? AND {scope} "
                "AND NOT EXISTS (SELECT 1 FROM owl_withdrawn_results w WHERE w.file_id=f.id) "
                "GROUP BY f.source_task_id",
                (bot_id, *values),
            )
        }
        # A collapsed record can be offered without loading every turn's event payloads.
        # The (task_id, kind, seq) index also covers conversations from earlier releases.
        worked = {
            row["id"]
            for row in db.execute(
                f"SELECT t.id FROM tasks t WHERE {scope} AND EXISTS "
                "(SELECT 1 FROM events e WHERE e.task_id=t.id "
                "AND e.kind IN ('tool_completed','tool_failed'))",
                values,
            )
        }
        # Progress and proposals stay with the turn that produced them.
        progress_notes: dict[str, list[str]] = {}
        owl_drafts: dict[str, dict] = {}
        screen_requests: dict[str, dict] = {}
        for row in db.execute(
            "SELECT e.task_id,e.kind,e.data FROM events e JOIN tasks t ON t.id=e.task_id "
            f"WHERE {scope} AND (e.kind='progress_note' "
            "OR (e.kind IN ('assistant_owl_draft','assistant_screen_request') AND t.status='completed')) ORDER BY e.seq",
            values,
        ):
            data = json.loads(row["data"])
            if row["kind"] == "assistant_owl_draft":
                owl_drafts[row["task_id"]] = data
            elif row["kind"] == "assistant_screen_request":
                screen_requests[row["task_id"]] = data
            elif text := data.get("text"):
                progress_notes.setdefault(row["task_id"], []).append(text)
        # A routine's run that checked and found nothing new, posting no reply: when it did.
        nothing_new = {
            row["task_id"]: row["at"]
            for row in db.execute(
                "SELECT e.task_id,max(e.created_at) AS at FROM events e JOIN tasks t ON "
                f"t.id=e.task_id WHERE {scope} AND e.kind='nothing_new' GROUP BY e.task_id",
                values,
            )
        }
        for task in tasks:
            task["coding_job_id"] = jobs.get(task["id"])
            task["file_count"] = file_counts.get(task["id"], 0)
            task["has_tool_steps"] = task["id"] in worked
            notes = progress_notes.get(task["id"], [])
            task["progress_notes"] = notes
            # Apps released before progress_notes show one line: the latest.
            task["progress_note"] = notes[-1] if notes else None
            if task["id"] in owl_drafts:
                task["assistant_owl_draft"] = owl_drafts[task["id"]]
            if task["id"] in screen_requests:
                task["assistant_screen_request"] = screen_requests[task["id"]]
            # Only such a run carries it, so other turns read as before.
            if task["id"] in nothing_new:
                task["nothing_new_at"] = nothing_new[task["id"]]
        if any(task["config"].get("assignment_id") for task in tasks):
            handoffs = {
                row["task_id"]: {
                    "brief": row["brief"],
                    "expected_result": row["expected_result"],
                    "verification": json.loads(row["verification"])
                    if row["verification"]
                    else None,
                }
                for row in db.execute(
                    "SELECT t.id AS task_id,a.brief,a.expected_result,a.verification "
                    "FROM owl_assignments a JOIN tasks t ON t.id=a.child_task_id "
                    f"WHERE {scope} AND t.conversation_visible=1",
                    values,
                )
            }
            for task in tasks:
                if task["id"] in handoffs:
                    task["assistant_handoff"] = handoffs[task["id"]]
        # A proposal belongs to its originating turn even after another message arrives.
        # The unique source_task_id allows at most one pending receipt on each turn.
        pending = {
            row["source_task_id"]: {"id": row["id"], "recipient_name": row["recipient_name"]}
            for row in db.execute(
                "SELECT a.source_task_id,a.id,a.recipient_name FROM owl_assignments a "
                "JOIN tasks t ON t.id=a.source_task_id "
                "JOIN owl_coordinators c ON c.owner=t.owner AND c.bot_id=t.bot_id "
                f"WHERE {scope} AND t.conversation_visible=1 AND t.status='completed' "
                "AND a.decision='proposed'",
                values,
            )
        }
        for task in tasks:
            receipt = pending.get(task["id"])
            task["pending_assignments"] = [receipt] if receipt else []
        # A withheld workbook has no coordinator return task. Its original chat turn can
        # show the actual finding without publishing the specialist's draft.
        by_id = {task["id"]: task for task in tasks}
        for row in db.execute(
            "SELECT a.id,a.source_task_id,a.recipient_name,a.recipient_bot_id,a.child_task_id,a.verification "
            "FROM owl_assignments a JOIN tasks t ON t.id=a.source_task_id "
            "JOIN tasks child ON child.id=a.child_task_id "
            "JOIN owl_assignment_files af ON af.assignment_id=a.id "
            f"WHERE {scope} AND t.conversation_visible=1 AND child.owner=t.owner "
            "AND child.status='completed' AND a.verification IS NOT NULL "
            "AND (af.inputs<>'[]' OR af.checkpoint_ids<>'[]')",
            values,
        ):
            verdict = json.loads(row["verification"])
            if verdict["status"] != "met" and (parent := by_id.get(row["source_task_id"])):
                parent["assignment_reviews"] = [
                    {
                        **{
                            key: row[key]
                            for key in ("id", "recipient_name", "recipient_bot_id", "child_task_id")
                        },
                        "status": verdict["status"],
                        "detail": verdict.get("detail"),
                    }
                ]
        decided = cls._decided_actions(db, bot_id, only=only)
        from .proposals import setup_cards
        from .routine_suggestions import suggestion_cards

        setups = setup_cards(db, bot_id, only=only)
        # A routine the owl suggested in that turn (routine_suggestions.py).
        routines = suggestion_cards(db, bot_id, only=only)
        for task in tasks:
            task["decided_actions"] = decided.get(task["id"], [])
            # Only a turn with a card carries one, so other turns read as before.
            if task["id"] in setups:
                task["setup_proposal"] = setups[task["id"]]
            if task["id"] in routines:
                task["routine_suggestion"] = routines[task["id"]]
        # The playbook a turn kept (playbooks.py), shown under its reply with Undo.
        from .playbooks import task_playbooks

        kept = task_playbooks(db, bot_id, only=only)
        for task in tasks:
            if task["id"] in kept:
                task["playbook"] = kept[task["id"]]
        # The check-backs a turn set, under its reply (pending ones with Cancel), and what a turn
        # a check-back started shows above its reply (check_backs.py).
        from .check_backs import checked_back, task_check_backs

        set_here = task_check_backs(db, bot_id, only=only)
        for task in tasks:
            if task["id"] in set_here:
                task["check_backs"] = set_here[task["id"]]
            started = checked_back(task)
            if started:
                task["checked_back"] = started
        # The facts a reply offered to add to About me (about_me.py), when it offered any.
        from .about_me import task_suggestions

        suggested = task_suggestions(db, bot_id, only=only)
        for task in tasks:
            if task["id"] in suggested:
                task["about_me_suggestions"] = suggested[task["id"]]
        # Apps the turn asked the person to turn on or connect (desktop_connectors.py).
        needs: dict[str, list[dict]] = {}
        for row in db.execute(
            "SELECT n.id,n.source_task_id,n.plugin_id,n.name,n.reason,n.state,n.status,n.revision "
            f"FROM bot_app_needs n JOIN tasks t ON t.id=n.source_task_id WHERE {scope} "
            "ORDER BY n.created_at,n.id",
            values,
        ):
            needs.setdefault(row["source_task_id"], []).append(
                {key: row[key] for key in row.keys() if key != "source_task_id"}
            )
        for task in tasks:
            task["app_needs"] = needs.get(task["id"], [])
        # When the owl started each turn (its first claim), so a message that waited behind
        # earlier work reads after what that work said meanwhile; None while it waits. A message
        # that steered a running turn reads from when that turn read it (steering.py).
        started = {
            row["task_id"]: row["started_at"]
            for row in db.execute(
                "SELECT e.task_id,min(e.created_at) AS started_at FROM events e "
                f"JOIN tasks t ON t.id=e.task_id WHERE {scope} "
                "AND e.kind IN ('running','steer_applied') GROUP BY e.task_id",
                values,
            )
        }
        # The person's own message: whether it still waits, so it can be changed or cancelled
        # (queued_messages.py); and the reactions to the owl's replies (reactions.py).
        from .queued_messages import task_states
        from .reactions import task_reactions

        sent = task_states(db, bot_id, only=only)
        reactions = task_reactions(db, bot_id, only=only)
        for task in tasks:
            task["started_at"] = started.get(task["id"])
            if task["id"] in sent:
                task["queued_message"] = sent[task["id"]]
            if task["id"] in reactions:
                task["reactions"] = reactions[task["id"]]

    # The messages of one conversation (an owl's own, or a room's), in the order it shows them.
    _PAGE_MESSAGES = (
        "SELECT m.rowid AS position,m.id,m.chat_id,m.task_id,m.role,{content},m.created_at "
        "FROM messages m JOIN chats c ON c.id=m.chat_id WHERE {scope}{where} "
        "ORDER BY m.created_at {order},m.rowid {order}{limit}"
    )
    _OLDER = " AND (m.created_at<? OR (m.created_at=? AND m.rowid<?))"
    _NEWER = " AND (m.created_at>? OR (m.created_at=? AND m.rowid>?))"
    _SINCE = " AND (m.created_at>? OR (m.created_at=? AND m.rowid>=?))"

    @classmethod
    def _page_messages(
        cls,
        db,
        scope: ChatScope,
        *,
        where="",
        values=(),
        newest_first=False,
        limit=None,
        content=True,
    ):
        # Without content: only where the page starts and which turns it holds, for a poll.
        rows = db.execute(
            cls._PAGE_MESSAGES.format(
                content="m.content" if content else "'' AS content",
                scope=scope.chats,
                where=where,
                order="DESC" if newest_first else "ASC",
                limit=" LIMIT ?" if limit is not None else "",
            ),
            (scope.value, *values, *((limit,) if limit is not None else ())),
        ).fetchall()
        messages = [dict(row) for row in rows]
        return messages[::-1] if newest_first else messages

    @staticmethod
    def _message_key(db, scope: ChatScope, message_id: str) -> tuple | None:
        row = db.execute(
            "SELECT m.created_at,m.rowid AS position FROM messages m JOIN chats c ON c.id=m.chat_id "
            f"WHERE m.id=? AND {scope.chats}",
            (message_id, scope.value),
        ).fetchone()
        return (row["created_at"], row["created_at"], row["position"]) if row else None

    @staticmethod
    def _key(message: dict) -> tuple:
        return (message["created_at"], message["created_at"], message["position"])

    def _page_tasks(self, db, owner, scope: ChatScope, messages, *, since, until, latest, also=()):
        """The tasks a page shows: those its messages belong to, those started within its span
        (a turn with no message yet, like one still being written), every task still working
        (latest page only), and ``also``.
        """
        clauses, values = [], []
        span = []
        if since is not None:
            span.append("t.created_at>=?")
            values.append(since)
        if until is not None:
            span.append("t.created_at<?")
            values.append(until)
        clauses.append("(" + " AND ".join(span) + ")" if span else "1=1")
        named = sorted({message["task_id"] for message in messages} | set(also))
        if named:
            clauses.append(f"t.id IN ({','.join('?' * len(named))})")
            values.extend(named)
        if latest:
            clauses.append("t.status IN ('queued','running','waiting','cancelling')")
        ids = [
            row[0]
            for row in db.execute(
                f"SELECT t.id FROM tasks t WHERE {scope.tasks} AND t.conversation_visible=1 "
                f"AND ({' OR '.join(clauses)}) "
                "ORDER BY t.created_at,t.id",
                (scope.value, *values),
            )
        ]
        tasks = [self._task(db, owner, task_id) for task_id in ids]
        self._decorate_scope(db, scope, tasks, only=ids)
        return tasks

    @staticmethod
    def _fingerprint(task: dict) -> str:
        return hashlib.sha256(
            json.dumps(task, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()[:16]

    @staticmethod
    def _read_version(version: str | None) -> tuple[str, dict[str, str]]:
        """A version is the chat's activity, then each shown task's fingerprint."""
        if not version:
            return "", {}
        head, _, body = version.partition(":")
        known = {}
        for part in body.split(","):
            task_id, _, fingerprint = part.partition(".")
            if task_id and fingerprint:
                known[task_id] = fingerprint
        return head, known

    def conversation_page(self, owner: str, chat_id: str, query: ConversationPageQuery) -> dict:
        """One page of the owl's conversation, or of a room's (see ``ConversationPageQuery``).

        The latest page always carries every task still working and every task started since
        its first message, so a message the person just sent finds its task on it. With a
        version, ``continues`` says the messages follow the newest one the client has and the
        tasks are only those that changed; otherwise the page replaces what the client had.
        ``earlier`` is the message to read the page before from, or None at the start.
        """
        with self.connect() as db:
            chat, scope = self._scope(db, owner, chat_id)
            first = dict(self._own_chat(db, chat["bot_id"])) if scope.default_chat else chat
            result = {key: first[key] for key in ("id", "bot_id", "title", "created_at")}
            activity = self._activity(db, result, scope)
            result.update(
                activity_seq=activity["activity_seq"],
                unread=activity["unread"],
                read_at=self._read_at(db, first["id"]),
            )
            head = f"{activity['activity_seq']}.{int(activity['unread'])}"
            limit = query.limit
            if query.before is not None:
                before = self._message_key(db, scope, query.before)
                if before is None:
                    raise NotFound("Message not found.")
                messages = self._page_messages(
                    db, scope, where=self._OLDER, values=before, newest_first=True, limit=limit
                )
                messages = self._reach_back(db, owner, scope, messages, query, before)
                earlier = self._earlier(db, scope, messages)
                tasks = self._page_tasks(
                    db,
                    owner,
                    scope,
                    messages,
                    since=messages[0]["created_at"] if messages and earlier else None,
                    until=before[0],
                    latest=False,
                )
                return self._page(
                    db, scope, result, messages, tasks, earlier=earlier, continues=False
                )
            latest = self._page_messages(
                db, scope, newest_first=True, limit=limit, content=query.version is None
            )
            seen, known = self._read_version(query.version)
            new = None
            if query.version is not None:
                after = self._message_key(db, scope, query.after) if query.after else None
                if query.after is None or after is not None:
                    new = self._page_messages(
                        db,
                        scope,
                        where=self._NEWER if after else "",
                        values=after or (),
                        newest_first=True,
                        limit=limit + 1,
                    )
                    if len(new) > limit:
                        new = None
            if new is None:
                if query.version is not None:
                    latest = self._page_messages(db, scope, newest_first=True, limit=limit)
                latest = self._reach_back(db, owner, scope, latest, query, None)
            # New messages are the newest, so the latest page holds them too.
            window = latest
            earlier = self._earlier(db, scope, latest)
            tasks = self._page_tasks(
                db,
                owner,
                scope,
                window,
                since=latest[0]["created_at"] if latest and earlier else None,
                until=None,
                latest=True,
                also=known if new is not None else (),
            )
            prints = {task["id"]: self._fingerprint(task) for task in tasks}
            shown = {message["task_id"] for message in window}
            # Keep already-seen pending cards in the polling version after they scroll off
            # this page, so the decision removes the correct card. Older pages read their
            # own receipts afresh. Bound the extra fingerprint list like the assignment inbox.
            pending_ids = [task["id"] for task in tasks if task["pending_assignments"]]
            tracked_pending = set(pending_ids[-ASSIGNMENT_PAGE_SIZE:])
            current = [
                task
                for task in tasks
                if task["id"] in shown
                or task["id"] in tracked_pending
                or task["status"] in ACTIVE
                or not earlier
                or task["created_at"] >= latest[0]["created_at"]
            ]
            result["version"] = (
                head + ":" + ",".join(f"{task['id']}.{prints[task['id']]}" for task in current)
            )
            if new is None:
                return self._page(
                    db, scope, result, latest, current, earlier=earlier, continues=False
                )
            changed = [task for task in tasks if known.get(task["id"]) != prints[task["id"]]]
            if not new and not changed and seen == head:
                return self._page(
                    db, scope, result, [], [], earlier=None, continues=True, unchanged=True
                )
            return self._page(db, scope, result, new, changed, earlier=None, continues=True)

    def _reach_back(self, db, owner, scope: ChatScope, messages, query, before):
        """Extend a page back to the first message of ``query.through_task``."""
        if query.through_task is None:
            return messages
        task = self._task(db, owner, query.through_task)
        if not db.execute(
            f"SELECT 1 FROM tasks t WHERE t.id=? AND {scope.tasks}", (task["id"], scope.value)
        ).fetchone():
            raise NotFound("Task not found.")
        start = db.execute(
            "SELECT m.created_at,m.rowid AS position FROM messages m WHERE m.task_id=? "
            "ORDER BY m.created_at,m.rowid LIMIT 1",
            (query.through_task,),
        ).fetchone()
        if start is None:
            return messages
        key = (start["created_at"], start["created_at"], start["position"])
        if messages and (messages[0]["created_at"], messages[0]["position"]) <= (
            start["created_at"],
            start["position"],
        ):
            return messages
        if before is not None and (before[0], before[2]) <= (key[0], key[2]):
            return messages
        return self._page_messages(
            db,
            scope,
            where=self._SINCE + (self._OLDER if before is not None else ""),
            values=key + (before or ()),
        )

    def _earlier(self, db, scope: ChatScope, messages) -> str | None:
        if not messages:
            return None
        older = db.execute(
            f"SELECT 1 FROM messages m JOIN chats c ON c.id=m.chat_id WHERE {scope.chats}"
            + self._OLDER
            + " LIMIT 1",
            (scope.value, *self._key(messages[0])),
        ).fetchone()
        return messages[0]["id"] if older else None

    @classmethod
    def _page(
        cls, db, scope: ChatScope, result, messages, tasks, *, earlier, continues, unchanged=False
    ) -> dict:
        cls._message_file_lists(db, scope, messages, only=sorted({m["task_id"] for m in messages}))
        for message in messages:
            message.pop("position", None)
        return {
            **result,
            "messages": messages,
            "tasks": tasks,
            "earlier": earlier,
            "continues": continues,
            "unchanged": unchanged,
        }

    def search_history(
        self, owner: str, bot_id: str, data: HistoryQuery, *, excerpts: bool = False
    ) -> dict:
        """Unicode phrase search, newest first, with cursor browsing when query is empty.

        This is source retrieval, not a semantic ranker. The model chooses queries
        and can page through actual records rather than relying on a word map. Matching is
        Unicode-normalised and case-folded; only when nothing in the owl's history matches
        exactly does it also ignore accents, compatibility forms and spacing (``match`` is
        then "loose"). ``excerpts`` adds the words around each match, for people.
        """
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            columns = """m.rowid AS position,m.id,m.chat_id,m.task_id,m.role,
                          substr(m.content,1,700) AS preview,length(m.content) AS characters,
                          m.created_at,c.title AS conversation_title,t.status AS task_status"""
            if excerpts:
                columns += ",EXISTS(SELECT 1 FROM owl_chat_threads x WHERE x.chat_id=c.id) AS separate_thread"
            # The owl's own conversation: rooms keep their messages to themselves (rooms.py).
            source = f"""FROM messages m JOIN chats c ON c.id=m.chat_id JOIN tasks t ON t.id=m.task_id
                   WHERE {ChatScope(bot_id).chats} AND (CAST(? AS BIGINT) IS NULL OR m.rowid<?)"""
            rows = db.execute(
                f"""SELECT {columns} {source}
                     AND instr(unicode_fold(m.content),unicode_fold(?))>0
                   ORDER BY m.rowid DESC LIMIT ?""",
                (bot_id, data.before, data.before, data.query, data.limit + 1),
            ).fetchall()
            loose = (
                not rows
                and bool(data.query)
                and not (
                    data.before is not None
                    and db.execute(
                        "SELECT 1 FROM messages m JOIN chats c ON c.id=m.chat_id "
                        f"WHERE {ChatScope(bot_id).chats} "
                        "AND instr(unicode_fold(m.content),unicode_fold(?))>0 LIMIT 1",
                        (bot_id, data.query),
                    ).fetchone()
                )
            )
            if loose:
                needle = loose_fold(data.query)
                rows = []
                for row in db.execute(
                    f"SELECT {columns},m.content {source} ORDER BY m.rowid DESC",
                    (bot_id, data.before, data.before),
                ):
                    if loose_contains(row["content"], needle):
                        rows.append(row)
                        if len(rows) > data.limit:
                            break
            page = []
            default_chat = self._own_chat(db, bot_id) if excerpts else None
            for row in rows[: data.limit]:
                item = dict(row)
                content = item.pop("content", None)
                if excerpts:
                    separate_thread = item.pop("separate_thread")
                    if default_chat and not separate_thread:
                        item["chat_id"] = default_chat["id"]
                    if content is None:
                        content = db.execute(
                            "SELECT content FROM messages WHERE id=?", (item["id"],)
                        ).fetchone()[0]
                    item["excerpt"] = excerpt(content, data.query, loose=loose)
                page.append(item)
            return {
                "messages": page,
                "next_before": page[-1]["position"] if len(rows) > data.limit else None,
                "match": "loose" if loose else "exact",
            }

    NEEDS_YOU_LIMIT = 100

    def needs_you(self, owner: str) -> dict:
        """Everything waiting for the person across their owls, newest first, in one read.

        Each item names its owl and kind: a question the owl asked, an app it asked to have
        turned on or connected, an action waiting for approval, a next step it suggests,
        approval on this computer, help on its computer, a routine that paused itself or waits
        to be turned on, and the owl's latest turn when it failed or was interrupted and the
        person hasn't seen it. ``text`` is the owl's own words where there are some (an app's
        name for an app); the app words everything else. Help on its computer carries ``help``
        ({kind, site, site_name}) when the owl said what it needs. Archived owls are left out:
        the person put them away, and they start no work of their own.
        """
        now = time.time()
        with self.connect() as db:
            owls = {
                row["id"]: row
                for row in db.execute(
                    f"SELECT id,name,color,background FROM bots WHERE owner=? "
                    f"AND archived_at IS NULL AND id NOT IN ({self.ARRIVING})",
                    (owner,),
                )
            }
            items: list[dict] = []
            rooms: dict[str, dict | None] = {}

            def room_of(task_id):
                """The room a turn was taken in, so the app opens the room (rooms.py)."""
                if task_id is None:
                    return None
                if task_id not in rooms:
                    row = db.execute(
                        "SELECT r.id,r.name FROM tasks t JOIN rooms r ON r.id=t.chat_id "
                        "WHERE t.id=? AND r.owner=?",
                        (task_id, owner),
                    ).fetchone()
                    rooms[task_id] = dict(row) if row else None
                return rooms[task_id]

            def add(kind, row, **extra):
                if row["bot_id"] not in owls:
                    return
                room = room_of(row["task_id"])
                items.append(
                    {
                        "kind": kind,
                        "id": row["id"],
                        "bot_id": row["bot_id"],
                        "bot_name": owls[row["bot_id"]]["name"],
                        "bot_color": owls[row["bot_id"]]["color"],
                        "bot_background": owls[row["bot_id"]]["background"],
                        "task_id": row["task_id"],
                        "text": None,
                        "at": row["at"],
                        # Only a turn taken in a room names it, so other items read as before.
                        **({"room_id": room["id"], "room_name": room["name"]} if room else {}),
                        **extra,
                    }
                )

            # Asked once the owl's message carrying it is out (see _asks_you).
            for row in db.execute(
                "SELECT c.id,c.bot_id,c.source_task_id AS task_id,c.question,c.updated_at AS at "
                "FROM clarifications c JOIN tasks t ON t.id=c.source_task_id "
                "JOIN bots b ON b.id=c.bot_id WHERE b.owner=? AND c.status='pending' "
                "AND t.status NOT IN ('cancelled','cancelling','failed','interrupted','running')",
                (owner,),
            ):
                add("question", row, text=row["question"])
            # An app the owl asked for, off for it or not connected (desktop_connectors.py).
            for row in db.execute(
                "SELECT n.id,n.bot_id,n.source_task_id AS task_id,n.name,n.state,"
                "n.updated_at AS at FROM bot_app_needs n JOIN tasks t ON t.id=n.source_task_id "
                "JOIN bots b ON b.id=n.bot_id WHERE b.owner=? "
                "AND n.status IN ('pending','connecting') "
                "AND t.status NOT IN ('cancelled','cancelling','failed','interrupted','running')",
                (owner,),
            ):
                add("app", row, text=row["name"], state=row["state"])
            for row in db.execute(
                "SELECT r.id,r.bot_id,r.task_id,r.action,r.effect,r.created_at AS at "
                "FROM bot_action_reviews r JOIN tasks t ON t.id=r.task_id "
                "JOIN bots b ON b.id=r.bot_id WHERE b.owner=? AND r.status='pending' "
                "AND t.status IN ('queued','running','waiting')",
                (owner,),
            ):
                action = json.loads(row["action"])
                observed = {
                    key: value
                    for key, value in (action.get("observed_context") or {}).items()
                    if key != "screenshot"
                }
                add(
                    "approval",
                    row,
                    action={
                        "tool": action.get("tool"),
                        "arguments": action.get("arguments") or {},
                        "observed_context": observed,
                    },
                    # What the step would do that made it wait (action_review.request).
                    effect=row["effect"],
                )
            # A suggestion shows under the owl's reply, under the owl's current setup.
            for row in db.execute(
                "SELECT p.id,p.bot_id,p.source_task_id AS task_id,p.question,p.updated_at AS at "
                "FROM action_proposals p JOIN tasks t ON t.id=p.source_task_id "
                "JOIN bots b ON b.id=p.bot_id WHERE b.owner=? AND p.status='pending' "
                "AND p.visible_after<=? AND p.bot_updated_at=b.updated_at "
                "AND t.status NOT IN ('cancelled','cancelling','failed','interrupted') "
                "AND EXISTS(SELECT 1 FROM messages m WHERE m.task_id=p.source_task_id "
                "AND m.role='assistant')",
                (owner, now),
            ):
                add("proposal", row, text=row["question"])
            # The owl's own setup waits under its reply, under the setup it was proposed for.
            # It is a suggestion the reply carries, so apps without setup cards still list it.
            for row in db.execute(
                "SELECT p.id,p.bot_id,p.source_task_id AS task_id,p.purpose,p.updated_at AS at "
                "FROM setup_proposals p JOIN tasks t ON t.id=p.source_task_id "
                "JOIN bots b ON b.id=p.bot_id WHERE b.owner=? AND p.status='pending' "
                "AND p.bot_updated_at=b.updated_at AND t.status='completed' "
                "AND EXISTS(SELECT 1 FROM messages m WHERE m.task_id=p.source_task_id "
                "AND m.role='assistant')",
                (owner,),
            ):
                add("proposal", row, text=row["purpose"], setup=True)
            for row in db.execute(
                "SELECT t.id,t.bot_id,t.id AS task_id,j.state,t.updated_at AS at FROM tasks t "
                "JOIN background_turns j ON j.task_id=t.id JOIN bots b ON b.id=t.bot_id "
                "WHERE b.owner=? AND t.status='waiting' "
                "AND j.state IN ('local_computer','computer_control')",
                (owner,),
            ):
                if row["state"] == "computer_control":
                    note = db.execute(
                        "SELECT data,created_at FROM events WHERE task_id=? "
                        "AND kind='computer_control_requested' ORDER BY seq DESC LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    data = json.loads(note["data"]) if note else {}
                    add(
                        "computer_control",
                        {**dict(row), "at": note["created_at"] if note else row["at"]},
                        text=data.get("message") or None,
                        # What the owl asked the person for, by kind and site, when it said.
                        **({"help": data["help"]} if isinstance(data.get("help"), dict) else {}),
                    )
                elif self._task(db, owner, row["id"]).get("wait_reason") == "local_approval":
                    add("local_approval", row)
            # A routine paused by itself (a failed run, a used-up allowance) carries a reason;
            # one the person paused doesn't. A draft is one an owl set up, not yet turned on. A
            # routine the owl only suggested (routine_suggestions.py) blocks nothing: its card
            # under the reply is where the person answers it, if they want it at all.
            for row in db.execute(
                "SELECT s.id,s.bot_id,s.source_task_id AS task_id,s.name,s.state,"
                "s.updated_at AS at,h.pause_code FROM schedules s JOIN bots b ON b.id=s.bot_id "
                "LEFT JOIN tasks t ON t.id=s.source_task_id "
                "LEFT JOIN schedule_health h ON h.schedule_id=s.id AND h.revision=s.revision "
                "WHERE b.owner=? AND ((s.state='paused' AND s.pause_reason IS NOT NULL) OR "
                "(s.state='draft' AND (t.status IS NULL OR t.status!='running') AND NOT EXISTS("
                "SELECT 1 FROM routine_suggestions g WHERE g.schedule_id=s.id)))",
                (owner,),
            ):
                # pause_code says why it paused, such as "access": a run needs the person to
                # allow something for it.
                add(
                    "routine",
                    row,
                    text=row["name"],
                    routine="schedule",
                    state=row["state"],
                    pause_code=row["pause_code"] if row["state"] == "paused" else None,
                )
            for row in db.execute(
                "SELECT r.id,r.bot_id,r.source_task_id AS task_id,r.name,r.updated_at AS at "
                "FROM bot_triggers r JOIN bots b ON b.id=r.bot_id "
                "LEFT JOIN tasks t ON t.id=r.source_task_id WHERE b.owner=? "
                "AND r.state='draft' AND (t.status IS NULL OR t.status!='running')",
                (owner,),
            ):
                add("routine", row, text=row["name"], routine="trigger", state="draft")
            # The owl's newest turn ended without its work, and the person hasn't seen it yet.
            # A routine's own run pauses its routine instead, which is listed above. A message the
            # person cancelled before it started is not a newer turn.
            from .queued_messages import not_withdrawn

            # Newer in the same conversation: the owl's own chat, or the room the turn was in.
            rooms_sql = self.ROOM_CHATS
            for row in db.execute(
                "SELECT t.id,t.bot_id,t.chat_id,t.id AS task_id,t.status,t.message FROM tasks t "
                "JOIN bots b ON b.id=t.bot_id WHERE b.owner=? "
                "AND t.status IN ('failed','interrupted') "
                "AND t.conversation_visible=1 "
                "AND NOT EXISTS(SELECT 1 FROM tasks n WHERE n.owner=t.owner AND "
                "n.conversation_visible=1 AND "
                f"((t.chat_id IN ({rooms_sql}) AND n.chat_id=t.chat_id) OR "
                f"(t.chat_id NOT IN ({rooms_sql}) AND n.bot_id=t.bot_id "
                f"AND n.chat_id NOT IN ({rooms_sql}))) AND "
                "(n.created_at>t.created_at OR (n.created_at=t.created_at AND n.id>t.id)) "
                f"AND {not_withdrawn('n')}) "
                "AND NOT EXISTS(SELECT 1 FROM schedule_runs r WHERE r.task_id=t.id)",
                (owner,),
            ):
                ended = db.execute(
                    "SELECT seq,created_at FROM events WHERE task_id=? AND kind=? "
                    "ORDER BY seq DESC LIMIT 1",
                    (row["id"], row["status"]),
                ).fetchone()
                _, scope = self._scope(db, owner, row["chat_id"])
                read = db.execute(
                    "SELECT coalesce(max(r.event_seq),0) FROM chat_read_cursors r "
                    f"JOIN chats c ON c.id=r.chat_id WHERE {scope.chats}",
                    (scope.value,),
                ).fetchone()[0]
                if ended and ended["seq"] > read:
                    add(
                        "failed",
                        {**dict(row), "at": ended["created_at"]},
                        text=row["message"] or None,
                        state=row["status"],
                    )
            items.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
            return {"items": items[: self.NEEDS_YOU_LIMIT], "count": len(items)}

    def read_message(
        self, owner: str, bot_id: str, message_id: str, offset: int, length: int
    ) -> dict:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                f"""SELECT m.*,t.status AS task_status FROM messages m
                   JOIN chats c ON c.id=m.chat_id JOIN tasks t ON t.id=m.task_id
                   WHERE m.id=? AND {ChatScope(bot_id).chats}""",
                (message_id, bot_id),
            ).fetchone()
            if row is None:
                raise NotFound("Message not found.")
            result = dict(row)
            total = len(result["content"])
            result["content"] = result["content"][offset : offset + length]
            result.update(
                total_characters=total,
                next_offset=offset + length if offset + length < total else None,
            )
            return result

    @staticmethod
    def _notes(db, bot_id, *, include_archived=False):
        return [
            dict(row)
            for row in db.execute(
                "SELECT n.*,a.target_note_id AS consolidated_into,a.source_revision AS archived_revision,"
                "a.created_at AS archived_at FROM working_notes n LEFT JOIN note_consolidations a "
                "ON a.source_note_id=n.id AND a.restored_at IS NULL WHERE n.bot_id=? "
                + ("" if include_archived else "AND a.source_note_id IS NULL ")
                + "ORDER BY n.updated_at DESC,n.id",
                (bot_id,),
            )
        ]

    def list_notes(self, owner: str, bot_id: str, *, include_archived=False) -> list[dict]:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            return self._notes(db, bot_id, include_archived=include_archived)

    def note(self, owner, bot_id, note_id, *, include_history=True, before_revision=None):
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                "SELECT n.*,a.target_note_id AS consolidated_into,a.source_revision AS archived_revision,"
                "a.created_at AS archived_at FROM working_notes n LEFT JOIN note_consolidations a "
                "ON a.source_note_id=n.id AND a.restored_at IS NULL WHERE n.id=? AND n.bot_id=?",
                (note_id, bot_id),
            ).fetchone()
            if row is None:
                raise NotFound("Working note not found.")
            result = dict(row)
            result["consolidated_sources"] = [
                dict(r)
                for r in db.execute(
                    "SELECT source_note_id,source_revision,target_revision,reason,created_at,restored_at "
                    "FROM note_consolidations WHERE target_note_id=? ORDER BY seq",
                    (note_id,),
                )
            ]
            if include_history:
                rows = db.execute(
                    "SELECT * FROM note_revisions WHERE note_id=? AND (CAST(? AS BIGINT) IS NULL OR revision<?) ORDER BY revision DESC LIMIT 21",
                    (note_id, before_revision, before_revision),
                ).fetchall()
                result["revisions"] = [dict(r) for r in rows[:20]]
                result["next_before_revision"] = rows[19]["revision"] if len(rows) > 20 else None
            return result

    def _note_writer(self, db, owner, bot_id, source_task_id):
        bot = self._bot(db, owner, bot_id)
        if source_task_id:
            task = self._task(db, owner, source_task_id)
            notes_enabled = (
                bot["assistant_enabled"] if bot.get("managed_assistant") else bot["memory_enabled"]
            )
            if task["bot_id"] != bot_id or not self.executing(db, task) or not notes_enabled:
                raise Conflict("This task cannot update working notes for this bot.")

    def _save_note(self, db, bot_id, data, *, note_id=None, source_task_id=None):
        now = time.time()
        if note_id:
            current = db.execute(
                "SELECT * FROM working_notes WHERE id=? AND bot_id=?", (note_id, bot_id)
            ).fetchone()
            if current is None:
                raise NotFound("Working note not found.")
            if db.execute(
                "SELECT 1 FROM note_consolidations WHERE source_note_id=? AND restored_at IS NULL",
                (note_id,),
            ).fetchone():
                raise Conflict(
                    "This note is archived. Read its current consolidated note or explicitly restore it before editing."
                )
            if current["revision"] != data.expected_revision:
                raise Conflict(
                    "This note changed. Read the current version before saving your correction."
                )
            revision = current["revision"] + 1
            db.execute(
                "UPDATE working_notes SET title=?,content=?,revision=?,source_task_id=?,updated_at=? WHERE id=?",
                (data.title, data.content, revision, source_task_id, now, note_id),
            )
        else:
            if data.expected_revision != 0:
                raise Conflict("A new note starts at revision zero.")
            note_id, revision = uid(), 1
            db.execute(
                "INSERT INTO working_notes VALUES(?,?,?,?,?,?,?,?)",
                (note_id, bot_id, data.title, data.content, revision, source_task_id, now, now),
            )
        db.execute(
            "INSERT INTO note_revisions VALUES(?,?,?,?,?,?)",
            (note_id, revision, data.title, data.content, source_task_id, now),
        )
        return dict(db.execute("SELECT * FROM working_notes WHERE id=?", (note_id,)).fetchone())

    def save_note(self, owner, bot_id, data: NoteInput, *, note_id=None, source_task_id=None):
        with self.connect(write=True) as db:
            self._note_writer(db, owner, bot_id, source_task_id)
            return self._save_note(db, bot_id, data, note_id=note_id, source_task_id=source_task_id)

    def consolidate_notes(self, owner, bot_id, data: NoteConsolidation, *, source_task_id=None):
        with self.connect(write=True) as db:
            self._note_writer(db, owner, bot_id, source_task_id)
            if len({source.note_id for source in data.sources}) != len(data.sources):
                raise Conflict("Consolidation sources must be distinct notes.")
            for source in data.sources:
                row = db.execute(
                    "SELECT * FROM working_notes WHERE id=? AND bot_id=?", (source.note_id, bot_id)
                ).fetchone()
                if row is None:
                    raise NotFound("Working note not found.")
                if (
                    row["revision"] != source.expected_revision
                    or db.execute(
                        "SELECT 1 FROM note_consolidations WHERE source_note_id=? AND restored_at IS NULL",
                        (source.note_id,),
                    ).fetchone()
                ):
                    raise Conflict(
                        "A source changed or was archived. Read the current notes before consolidating."
                    )
            result = self._save_note(
                db,
                bot_id,
                NoteInput(title=data.title, content=data.content),
                source_task_id=source_task_id,
            )
            now = time.time()
            for source in data.sources:
                db.execute(
                    "INSERT INTO note_consolidations(source_note_id,source_revision,target_note_id,target_revision,reason,source_task_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        source.note_id,
                        source.expected_revision,
                        result["id"],
                        result["revision"],
                        data.reason,
                        source_task_id,
                        now,
                    ),
                )
            return {**result, "archived_sources": [s.model_dump() for s in data.sources]}

    def restore_note(self, owner, bot_id, note_id, expected_revision):
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                "SELECT * FROM working_notes WHERE id=? AND bot_id=?", (note_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("Working note not found.")
            if row["revision"] != expected_revision:
                raise Conflict("The note changed. Read it before restoring.")
            now = time.time()
            db.execute(
                "UPDATE note_consolidations SET restored_at=? WHERE source_note_id=? AND restored_at IS NULL",
                (now, note_id),
            )
            # Restore visibility, not content chronology. An archived old
            # decision must not acquire a newer content timestamp than its correction.
            return {**dict(row), "consolidated_into": None}

    def reply_review_settings(self, owner: str, data: ReplyReviewSettings | None = None) -> dict:
        with self.connect(write=data is not None) as db:
            if data is not None:
                db.execute(
                    "INSERT INTO bot_reply_review_settings(owner,enabled) VALUES(?,?) "
                    "ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled",
                    (owner, int(data.enabled)),
                )
            return {"enabled": self._reply_review_enabled(db, owner)}

    @staticmethod
    def _reply_review_enabled(db, owner: str) -> bool:
        row = db.execute(
            "SELECT enabled FROM bot_reply_review_settings WHERE owner=?", (owner,)
        ).fetchone()
        return bool(row[0]) if row is not None else True

    def time_zone_settings(self, owner: str, data: TimeZoneSettings | None = None) -> dict:
        """The person's IANA time zone; null until their app has sent one."""
        with self.connect(write=data is not None) as db:
            if data is not None:
                db.execute(
                    "INSERT INTO bot_time_zone_settings(owner,time_zone,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(owner) DO UPDATE SET time_zone=excluded.time_zone,"
                    "updated_at=excluded.updated_at",
                    (owner, data.time_zone, time.time()),
                )
            return {"time_zone": self._time_zone(db, owner)}

    def time_zone(self, owner: str) -> str | None:
        with self.connect() as db:
            return self._time_zone(db, owner)

    @staticmethod
    def _time_zone(db, owner: str) -> str | None:
        row = db.execute(
            "SELECT time_zone FROM bot_time_zone_settings WHERE owner=?", (owner,)
        ).fetchone()
        return row[0] if row is not None else None

    def routine_time_zone(self, owner: str, task_id: str) -> str | None:
        """The time zone saved with the timing of the routine this task is a run of, if any."""
        with self.connect() as db:
            self._task(db, owner, task_id)
            row = db.execute(
                "SELECT config FROM schedule_runs WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        timing = json.loads(row["config"]).get("timing")
        zone = timing.get("timezone") if isinstance(timing, dict) else None
        return zone if isinstance(zone, str) else None

    def start_intro(self, owner: str, bot_id: str, data: IntroInput) -> dict | None:
        """Queue a new owl's first message, written by the owl itself; None once its chat has begun.

        The turn has no request from the person: the chat shows only the owl's reply, and
        the instruction for it is built at run time from the task's config. Only a
        conversation nobody has spoken in yet gets one, so a retry or a second window never
        adds a second hello or talks over the person.
        """
        with self.connect(write=True) as db:
            bot = self._bot(db, owner, bot_id)
            if bot.get("managed_assistant"):
                return None
            previous = db.execute(
                "SELECT id,bot_id FROM tasks WHERE owner=? AND request_id=?",
                (owner, data.request_id),
            ).fetchone()
            if previous:
                if previous["bot_id"] != bot_id:
                    raise Conflict("This request ID was already used for a different message.")
                return self._task(db, owner, previous["id"])
            # Its own chat: talking to it in a room first doesn't take its hello away.
            if db.execute(
                f"SELECT 1 FROM tasks t WHERE {ChatScope(bot_id).tasks} LIMIT 1", (bot_id,)
            ).fetchone():
                return None
            chat = self._ensure_chat(db, owner, bot_id, bot["name"])
            task_id, now = uid(), time.time()
            db.execute(
                "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    chat["id"],
                    bot_id,
                    owner,
                    data.request_id,
                    "",
                    "queued",
                    encode(
                        {**bot, "reply_review_enabled": False, "intro": {"language": data.language}}
                    ),
                    now,
                    now,
                ),
            )
            self._event(db, task_id, "queued", {})
            return self._task(db, owner, task_id)

    def enqueue(self, owner: str, chat_id: str, data: TaskInput) -> dict:
        """Queue a message the person sent in chat; until it starts they may change or cancel it."""
        from .queued_messages import record

        with self.connect(write=True) as db:
            task = self._enqueue(db, owner, chat_id, data, require_active_assistant=True)
            record(db, task["id"], data.message, task["created_at"])
            return task

    @classmethod
    def _assistant_state(cls, db, owner: str) -> dict:
        row = db.execute(_BOT_SELECT + "WHERE a.owner=?", (owner,)).fetchone()
        bot = cls._bot_view(row) if row else None
        return {"enabled": bool(bot and bot["assistant_enabled"]), "bot": bot}

    @staticmethod
    def _assistant_record(db, owner: str):
        return db.execute(
            "SELECT bot_id,previous_coordinator_id FROM owl_assistants WHERE owner=?", (owner,)
        ).fetchone()

    def assistant(self, owner: str) -> dict:
        with self.connect() as db:
            return self._assistant_state(db, owner)

    def set_assistant(self, owner: str, enabled: bool) -> dict:
        with self.connect(write=True) as db:
            assistant = self._assistant_record(db, owner)
            if enabled:
                previous_id = self._coordinator(db, owner)
                if assistant is None:
                    bot_id = self._insert_bot(
                        db,
                        owner,
                        BotInput(**ASSISTANT_PROFILE, memory_enabled=False, files_enabled=False),
                        time.time(),
                    )
                    db.execute(
                        "INSERT INTO owl_assistants(owner,bot_id,previous_coordinator_id) VALUES(?,?,?)",
                        (owner, bot_id, previous_id),
                    )
                else:
                    bot_id = assistant["bot_id"]
                    if previous_id != bot_id:
                        db.execute(
                            "UPDATE owl_assistants SET previous_coordinator_id=? WHERE owner=?",
                            (previous_id, owner),
                        )
                self._change_coordinator(db, owner, bot_id)
            elif assistant is not None:
                self._disable_assistant(db, owner, assistant)
            return self._assistant_state(db, owner)

    def _disable_assistant(self, db, owner: str, assistant) -> None:
        if self._is_coordinator(db, owner, assistant["bot_id"]):
            previous_id = assistant["previous_coordinator_id"]
            if (
                previous_id
                and not db.execute(
                    "SELECT 1 FROM bots WHERE id=? AND owner=?", (previous_id, owner)
                ).fetchone()
            ):
                previous_id = None
            self._change_coordinator(db, owner, previous_id)
            db.execute(
                "UPDATE owl_assistants SET previous_coordinator_id=NULL WHERE owner=?", (owner,)
            )

    @staticmethod
    def _coordinator(db, owner: str) -> str | None:
        row = db.execute("SELECT bot_id FROM owl_coordinators WHERE owner=?", (owner,)).fetchone()
        return row["bot_id"] if row else None

    @classmethod
    def _is_coordinator(cls, db, owner: str, bot_id: str) -> bool:
        return cls._coordinator(db, owner) == bot_id

    def is_coordinator(self, owner: str, bot_id: str) -> bool:
        with self.connect() as db:
            return self._is_coordinator(db, owner, bot_id)

    def set_coordinator(self, owner: str, bot_id: str, enabled: bool) -> dict:
        with self.connect(write=True) as db:
            bot = self._bot(db, owner, bot_id)
            if bot.get("managed_assistant"):
                raise Conflict("Use Assist to change whether Alysis Assistant coordinates work.")
            current_id = self._coordinator(db, owner)
            assistant = self._assistant_record(db, owner)
            if enabled and assistant and current_id == assistant["bot_id"]:
                raise Conflict("Turn off Assist before choosing another coordinator.")
            if enabled or current_id == bot_id:
                self._change_coordinator(db, owner, bot_id if enabled else None)
            return {"coordinator": enabled}

    def _change_coordinator(self, db, owner: str, bot_id: str | None) -> None:
        previous_id = self._coordinator(db, owner)
        if previous_id == bot_id:
            return
        if bot_id is None:
            db.execute("DELETE FROM owl_coordinators WHERE owner=?", (owner,))
        else:
            db.execute(
                "INSERT INTO owl_coordinators(owner,bot_id) VALUES(?,?) "
                "ON CONFLICT(owner) DO UPDATE SET bot_id=excluded.bot_id",
                (owner, bot_id),
            )
        if previous_id:
            db.execute(
                "UPDATE owl_assignments SET decision='cancelled' WHERE decision='proposed' "
                "AND source_task_id IN (SELECT id FROM tasks WHERE owner=? AND bot_id=?)",
                (owner, previous_id),
            )
            for routine in db.execute(
                "SELECT s.* FROM schedules s JOIN owl_assignment_routines r ON r.schedule_id=s.id "
                "WHERE s.bot_id=? AND s.state='active'",
                (previous_id,),
            ).fetchall():
                self._pause_schedule(db, dict(routine), "assignment_coordinator", time.time())

    @staticmethod
    def _assignment_rows(
        db,
        owner: str,
        bot_id: str,
        *,
        assignment_id: str | None = None,
        before: tuple[float, str] | None = None,
    ):
        scope, values = "", (owner, bot_id)
        if assignment_id is not None:
            scope, values = "AND a.id=?", (*values, assignment_id)
        elif before is not None:
            scope, values = "AND (a.created_at,a.id)<(?,?)", (*values, *before)
        return db.execute(
            "SELECT a.*,parent.status AS parent_status,parent.config AS parent_config,"
            "af.assignment_id AS manifest_id,af.proposed AS proposed_files,af.inputs AS shared_files,"
            "af.outputs AS returned_files,af.recipient_artifact_id,"
            "child.status AS child_status,"
            "child.output AS result,child.config AS child_config,"
            "(SELECT e.data FROM events e WHERE e.task_id=a.source_task_id "
            "AND e.kind='assignment_access' ORDER BY e.seq DESC LIMIT 1) AS proposed_access,"
            "(SELECT e.data FROM events e WHERE e.task_id=a.source_task_id "
            "AND e.kind='assignment_repeats' ORDER BY e.seq DESC LIMIT 1) AS repeats,"
            "(SELECT e.data FROM events e WHERE e.task_id=a.child_task_id "
            "AND e.kind='assignment_step' ORDER BY e.seq DESC LIMIT 1) AS current_step,"
            "(SELECT f.id FROM files f WHERE f.source_task_id=a.child_task_id "
            "AND f.bot_id=a.recipient_bot_id ORDER BY f.created_at,f.id LIMIT 1) AS artifact_id "
            "FROM owl_assignments a JOIN tasks parent ON parent.id=a.source_task_id "
            "LEFT JOIN owl_assignment_files af ON af.assignment_id=a.id "
            "LEFT JOIN tasks child ON child.id=a.child_task_id "
            f"WHERE parent.owner=? AND parent.bot_id=? {scope} "
            "ORDER BY a.created_at DESC,a.id DESC LIMIT ?",
            (*values, ASSIGNMENT_PAGE_SIZE + 1),
        )

    @staticmethod
    def _assignment_item(row) -> dict:
        status = row["decision"]
        parent_config = json.loads(row["parent_config"])
        routine = parent_config.get("assignment_routine")
        if row["child_task_id"]:
            status = row["child_status"]
        elif status == "proposed" and row["parent_status"] in (
            "failed",
            "cancelled",
            "interrupted",
        ):
            status = "cancelled"
        returned = json.loads(row["returned_files"] or "[]") if status == "completed" else []
        if row["decision"] == "approved":
            inputs = [
                {
                    **f,
                    "id": f["source_file_id"],
                    "name": f["source_name"],
                    "revision": f["source_revision"],
                }
                for f in json.loads(row["shared_files"] or "[]")
            ]
        else:
            inputs = json.loads(row["proposed_files"] or "[]")
        # Old assignments kept a single child-side artifact. New receipts identify only returned results.
        artifact = (
            (returned[0]["source_file_id"] if returned else row["recipient_artifact_id"])
            if row["manifest_id"]
            else row["artifact_id"]
        )
        return {
            **{
                key: row[key]
                for key in (
                    "id",
                    "source_task_id",
                    "recipient_bot_id",
                    "recipient_name",
                    "brief",
                    "expected_result",
                    "child_task_id",
                )
            },
            "status": status,
            "ready": status == "proposed" and row["parent_status"] == "completed",
            "result": row["result"] or None,
            **({"work_scope": row["work_scope"]} if row["work_scope"] != "text" else {}),
            **({"artifact_id": artifact} if status == "completed" and artifact else {}),
            **({"input_files": inputs} if inputs else {}),
            **({"returned_files": returned} if returned else {}),
            "verification": json.loads(row["verification"]) if row["verification"] else None,
            **({"from_assist_offer": True} if parent_config.get("approved_owl_setup") else {}),
            # The earlier finished assignment this one repeats, as the coordinator proposed it.
            **({"repeats": json.loads(row["repeats"])["assignment_id"]} if row["repeats"] else {}),
            # A computer task: what its card lets the owl use, and the numbered step it is on.
            **(
                {
                    "access": json.loads(row["proposed_access"])
                    if row["proposed_access"]
                    else json.loads(row["child_config"] or "{}").get("assignment_access")
                }
                if row["work_scope"] == "computer"
                else {}
            ),
            **(
                {"step": json.loads(row["current_step"])["step"]}
                if row["current_step"] and status in ("queued", "running", "waiting")
                else {}
            ),
            **(
                {"source_task_id": routine["source_task_id"], "routine_id": routine["schedule_id"]}
                if routine
                else {}
            ),
        }

    def list_assignments(self, owner: str, bot_id: str, *, before: str | None = None) -> dict:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            cursor = None
            if before is not None:
                row = db.execute(
                    "SELECT a.created_at,a.id FROM owl_assignments a "
                    "JOIN tasks parent ON parent.id=a.source_task_id "
                    "WHERE parent.owner=? AND parent.bot_id=? AND a.id=?",
                    (owner, bot_id, before),
                ).fetchone()
                if row is None:
                    raise NotFound("Assignment page not found.")
                cursor = (row["created_at"], row["id"])
            items, encoded_bytes, next_before = [], 0, None
            for row in self._assignment_rows(db, owner, bot_id, before=cursor):
                item = self._assignment_item(row)
                encoded_bytes += len(encode(item).encode("utf-8")) + 1
                if items and (
                    len(items) == ASSIGNMENT_PAGE_SIZE or encoded_bytes > ASSIGNMENT_PAGE_BYTES
                ):
                    next_before = items[-1]["id"]
                    break
                items.append(item)
            coordinator = db.execute(
                "SELECT c.bot_id, a.bot_id IS NOT NULL AS assistant_coordinator "
                "FROM owl_coordinators c LEFT JOIN owl_assistants a "
                "ON a.owner=c.owner AND a.bot_id=c.bot_id WHERE c.owner=?",
                (owner,),
            ).fetchone()
            return {
                "coordinator": bool(coordinator and coordinator["bot_id"] == bot_id),
                "items": items,
                "next_before": next_before,
                **(
                    {"assistant_coordinator": True}
                    if coordinator and coordinator["assistant_coordinator"]
                    else {}
                ),
            }

    def read_assignment(self, owner: str, bot_id: str, assignment_id: str) -> dict:
        with self.connect() as db:
            row = self._assignment_rows(db, owner, bot_id, assignment_id=assignment_id).fetchone()
            if row is None:
                raise NotFound("Assignment not found.")
            return self._assignment_item(row)

    def assignment_proposal_for_task(self, owner: str, source_task_id: str) -> dict | None:
        """The pending card on this specific coordinator turn, including before its reply lands."""
        with self.connect() as db:
            row = db.execute(
                "SELECT a.id,a.recipient_name,a.work_scope,a.decision,t.status AS parent_status "
                "FROM owl_assignments a JOIN tasks t ON t.id=a.source_task_id "
                "JOIN owl_coordinators c ON c.owner=t.owner AND c.bot_id=t.bot_id "
                "WHERE t.owner=? AND t.id=? AND t.conversation_visible=1 "
                "AND t.status IN ('queued','running','waiting','completed') "
                "AND a.decision='proposed' AND a.child_task_id IS NULL",
                (owner, source_task_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "recipient_name": row["recipient_name"],
            "work_scope": row["work_scope"],
            "status": row["decision"],
            "approval_availability": "ready"
            if row["parent_status"] == "completed"
            else "when_reply_completes",
        }

    def schedule_assignment(
        self, owner: str, bot_id: str, assignment_id: str, data: AssignmentScheduleInput
    ) -> dict:
        """Create a paused routine from a completed, directly approved assignment."""
        with self.connect(write=True) as db:
            source = self._assignment_rows(
                db, owner, bot_id, assignment_id=assignment_id
            ).fetchone()
            if source is None:
                raise NotFound("Assignment not found.")
            item = self._assignment_item(source)
            if not self._is_coordinator(db, owner, bot_id):
                raise Conflict("Choose this owl as coordinator before scheduling its assignments.")
            if item["status"] != "completed" or item["result"] is None or item.get("routine_id"):
                raise Conflict("Choose an original assignment with a completed result.")
            recipient = self._bot(db, owner, item["recipient_bot_id"])
            access = item.get("access")
            if item.get("work_scope") == "computer":
                # Its runs use what the person approved on the task's card, on the owl's own
                # computer. The computer, browser and websites are allowed again for the routine,
                # by the person, before it runs (allow_routine_access).
                if not access:
                    raise Conflict("Choose an original assignment with a completed result.")
                self._check_computer_access(db, recipient, access)
            return self.save_schedule(
                owner,
                bot_id,
                ScheduleInput(
                    name=data.name,
                    prompt=item["expected_result"],
                    timing=data.timing,
                    request_id=data.request_id,
                    enabled=False,
                    needs=_access_needs(access) if access else None,
                ),
                within=db,
                assignment_id=assignment_id,
            )

    @staticmethod
    def _assistant_owl_draft(db, task_id: str) -> dict | None:
        row = db.execute(
            "SELECT data FROM events WHERE task_id=? AND kind='assistant_owl_draft' ORDER BY seq LIMIT 1",
            (task_id,),
        ).fetchone()
        return json.loads(row["data"]) if row is not None else None

    @staticmethod
    def _assignment_access(db, task_id: str) -> dict | None:
        """What a computer task proposed from this turn may use (models.AssignmentAccess)."""
        row = db.execute(
            "SELECT data FROM events WHERE task_id=? AND kind='assignment_access' "
            "ORDER BY seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return json.loads(row["data"]) if row is not None else None

    @staticmethod
    def _check_computer_access(db, recipient: dict, access: dict | None) -> None:
        """A computer task's files are already in its owl's library, which keeps files."""
        if not access or not access.get("file_ids"):
            return
        if not recipient["files_enabled"]:
            raise Conflict("Turn on files for the owl before handing it these files.")
        ids = access["file_ids"]
        found = db.execute(
            f"SELECT count(*) FROM files WHERE bot_id=? AND id IN ({','.join('?' * len(ids))})",
            (recipient["id"], *ids),
        ).fetchone()[0]
        if found != len(ids):
            raise NotFound("A file for this task is not in the owl's library.")

    @classmethod
    def has_assistant_owl_draft(cls, db, task_id: str) -> bool:
        return cls._assistant_owl_draft(db, task_id) is not None

    @staticmethod
    def _require_no_pending_clarification(db, task_id: str) -> None:
        if db.execute(
            "SELECT 1 FROM clarifications WHERE source_task_id=? AND status='pending'",
            (task_id,),
        ).fetchone():
            raise Conflict("Wait for the person's clarification before proposing work.")

    @staticmethod
    def _assistant_screen_request(db, task_id: str) -> dict | None:
        row = db.execute(
            "SELECT data FROM events WHERE task_id=? AND kind='assistant_screen_request' ORDER BY seq LIMIT 1",
            (task_id,),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def request_assistant_screen(
        self, owner: str, source_task_id: str, data: AssistantScreenRequest
    ) -> dict:
        """Save a request for the desktop to display. The engine never captures a screen."""
        with self.connect(write=True) as db:
            task = self._task(db, owner, source_task_id)
            bot = self._bot(db, owner, task["bot_id"])
            if not (
                bot.get("assistant_enabled")
                and task["config"].get("assistant_screen_requests")
                and task["status"] == "running"
            ):
                raise Conflict("This turn cannot ask the desktop for a screenshot.")
            self.direct_request(db, owner, bot["id"], source_task_id)
            prior = self._assistant_screen_request(db, source_task_id)
            if prior:
                return prior
            self._require_no_pending_clarification(db, source_task_id)
            if (
                self.has_assistant_owl_draft(db, source_task_id)
                or db.execute(
                    "SELECT 1 FROM owl_assignments WHERE source_task_id=?", (source_task_id,)
                ).fetchone()
            ):
                raise Conflict("This turn already has a handoff to review.")
            proposal = data.model_dump()
            self._event(db, source_task_id, "assistant_screen_request", proposal)
            return proposal

    def propose_assistant_owl(
        self, owner: str, source_task_id: str, data: AssistantOwlDraftInput
    ) -> dict:
        """Record the draft the person can review after this turn completes."""
        with self.connect(write=True) as db:
            source = self._task(db, owner, source_task_id)
            assistant = self._bot(db, owner, source["bot_id"])
            if (
                not assistant.get("assistant_enabled")
                or source["config"].get("skipped_question")
                or not db.execute(
                    "SELECT 1 FROM messages WHERE task_id=? AND role='user'", (source_task_id,)
                ).fetchone()
            ):
                raise Conflict(
                    "Only Alysis Assistant can propose a new owl from your current request."
                )
            self.direct_request(db, owner, source["bot_id"], source_task_id)
            self._require_no_pending_clarification(db, source_task_id)
            if self._assistant_screen_request(db, source_task_id):
                raise Conflict("Wait for the person's screen approval before proposing work.")
            if db.execute(
                "SELECT 1 FROM owl_assignments WHERE source_task_id=?", (source_task_id,)
            ).fetchone():
                raise Conflict("This request already has an assignment.")
            proposal = data.model_dump(exclude_defaults=True)
            prior = self._assistant_owl_draft(db, source_task_id)
            if prior is None:
                if data.input_file_ids:
                    self._handoff_inputs(
                        db, owner, source["bot_id"], data.work_scope, data.input_file_ids
                    )
                self._event(db, source_task_id, "assistant_owl_draft", proposal)
            elif AssistantOwlDraftInput.model_validate(prior) != data:
                raise Conflict("This request already has a different owl draft.")
            return proposal

    def propose_assignment(self, owner: str, source_task_id: str, data: AssignmentInput) -> dict:
        with self.connect(write=True) as db:
            source = self._task(db, owner, source_task_id)
            if (
                not self._is_coordinator(db, owner, source["bot_id"])
                or source["config"].get("skipped_question")
                or not db.execute(
                    "SELECT 1 FROM messages WHERE task_id=? AND role='user'", (source_task_id,)
                ).fetchone()
            ):
                raise Conflict(
                    "Only the coordinating owl can propose work from your current request."
                )
            self.direct_request(db, owner, source["bot_id"], source_task_id)
            if self._bot(db, owner, source["bot_id"]).get("managed_assistant"):
                self._require_no_pending_clarification(db, source_task_id)
                if self._assistant_screen_request(db, source_task_id):
                    raise Conflict("Wait for the person's screen approval before proposing work.")
                if self.has_assistant_owl_draft(db, source_task_id):
                    raise Conflict("This request already has a new owl draft.")
            recipient = self._bot(db, owner, data.recipient_bot_id)
            if recipient["id"] == source["bot_id"]:
                raise Conflict("Assign the work to another owl.")
            if recipient.get("managed_assistant"):
                raise Conflict(
                    "Alysis Assistant coordinates assignments. Choose another owl to do the work."
                )
            prior = db.execute(
                "SELECT id,recipient_bot_id,brief,expected_result,work_scope FROM owl_assignments "
                "WHERE source_task_id=?",
                (source_task_id,),
            ).fetchone()
            brief = brief_with_steps(data.brief, data.steps)
            proposed = {
                **data.model_dump(exclude={"steps", "access", "repeats", "input_file_ids"}),
                "brief": brief,
            }
            access = data.access.model_dump() if data.access else None
            if prior:
                manifest = db.execute(
                    "SELECT proposed FROM owl_assignment_files WHERE assignment_id=?",
                    (prior["id"],),
                ).fetchone()
                if [
                    f["id"] for f in json.loads(manifest["proposed"] if manifest else "[]")
                ] != data.input_file_ids:
                    raise Conflict("This request already has a different file selection.")
                if any(prior[key] != value for key, value in proposed.items()) or (
                    self._assignment_access(db, source_task_id) != access
                ):
                    raise Conflict("This request already has a different assignment.")
                assignment_id = prior["id"]
            else:
                assignment_id = uid()
                db.execute(
                    "INSERT INTO owl_assignments(id,source_task_id,recipient_bot_id,recipient_name,"
                    "brief,expected_result,work_scope,decision,created_at) VALUES(?,?,?,?,?,?,?,'proposed',?)",
                    (
                        assignment_id,
                        source_task_id,
                        recipient["id"],
                        recipient["name"],
                        brief,
                        data.expected_result,
                        data.work_scope,
                        time.time(),
                    ),
                )
                if access is not None:
                    self._check_computer_access(db, recipient, access)
                    self._event(db, source_task_id, "assignment_access", access)
                if data.input_file_ids:
                    files = self._handoff_inputs(
                        db, owner, source["bot_id"], data.work_scope, data.input_file_ids
                    )
                    db.execute(
                        "INSERT INTO owl_assignment_files(assignment_id,proposed) VALUES(?,?)",
                        (assignment_id, encode(files)),
                    )
                repeats = self._repeated_assignment(
                    db,
                    owner,
                    source["bot_id"],
                    recipient["id"],
                    data.repeats if isinstance(data, OwlAssignmentProposal) else None,
                )
                if repeats:
                    self._event(
                        db, source_task_id, "assignment_repeats", {"assignment_id": repeats}
                    )
            return self._assignment_item(
                self._assignment_rows(
                    db, owner, source["bot_id"], assignment_id=assignment_id
                ).fetchone()
            )

    @staticmethod
    def _repeated_assignment(
        db, owner: str, coordinator_id: str, recipient_id: str, assignment_id: str | None
    ) -> str | None:
        """The earlier assignment a proposal repeats, when it names one this coordinator gave the
        same owl and that owl finished; anything else is no repeat."""
        if not assignment_id:
            return None
        row = db.execute(
            "SELECT a.id FROM owl_assignments a JOIN tasks parent ON parent.id=a.source_task_id "
            "JOIN tasks child ON child.id=a.child_task_id "
            "WHERE a.id=? AND parent.owner=? AND parent.bot_id=? AND a.recipient_bot_id=? "
            "AND child.status='completed'",
            (assignment_id, owner, coordinator_id, recipient_id),
        ).fetchone()
        return row["id"] if row else None

    def decide_assignment(
        self,
        owner: str,
        bot_id: str,
        assignment_id: str,
        decision: str,
        work_scope: str | None = None,
        input_file_ids: list[str] | None = None,
    ) -> dict:
        if decision not in ("approve", "decline"):
            raise ValueError("Invalid assignment decision.")
        with self.connect(write=True) as db:
            sender = self._bot(db, owner, bot_id)
            row = self._assignment_rows(db, owner, bot_id, assignment_id=assignment_id).fetchone()
            if row is None:
                raise NotFound("Assignment not found.")
            if decision == "approve" and row["work_scope"] != (work_scope or "text"):
                raise Conflict("Review the assignment's access before approving it.")
            item = self._assignment_item(row)
            if decision == "approve":
                if (
                    input_file_ids is None
                    and row["proposed_files"]
                    and json.loads(row["proposed_files"])
                ):
                    raise Conflict(
                        "Review the selected Library revisions before approving this handoff."
                    )
                if row["decision"] == "approved" and [
                    f["id"] for f in item.get("input_files", [])
                ] != (input_file_ids or []):
                    raise Conflict("This assignment was approved with different file revisions.")
            if (decision == "approve" and row["decision"] == "approved") or (
                decision == "decline" and row["decision"] == "declined"
            ):
                return item
            if item["status"] != "proposed":
                raise Conflict("This assignment has already been decided or stopped.")
            if decision == "decline":
                db.execute(
                    "UPDATE owl_assignments SET decision='declined' WHERE id=?", (assignment_id,)
                )
                return {**item, "status": "declined", "ready": False}
            if not self._is_coordinator(db, owner, bot_id):
                raise Conflict("This owl is no longer coordinating assignments.")
            if row["parent_status"] != "completed":
                raise Conflict("Wait for the coordinating owl to finish proposing this assignment.")
            recipient = self._bot(db, owner, row["recipient_bot_id"])
            if row["work_scope"] == "workbook" and not recipient["files_enabled"]:
                raise Conflict("Turn on files for the assigned owl before approving this workbook.")
            access = (
                self._assignment_access(db, row["source_task_id"])
                if row["work_scope"] == "computer"
                else None
            )
            if row["work_scope"] == "computer":
                if access is None:
                    raise Conflict("This computer task doesn't say what the owl may use.")
                self._check_computer_access(db, recipient, access)
            self._check_task_queue_capacity(db, owner)
            self._enqueue_assignment_child(
                db,
                owner,
                recipient,
                assignment_id,
                time.time(),
                visible=bool(sender.get("managed_assistant") or input_file_ids),
                access=access,
                input_file_ids=input_file_ids,
            )
            return self._assignment_item(
                self._assignment_rows(db, owner, bot_id, assignment_id=assignment_id).fetchone()
            )

    def approve_assistant_owl(
        self, owner: str, bot_id: str, data: AssistantOwlCreateInput | AssistantAssignmentApproval
    ) -> dict:
        """Commit the approved text assignment and any new owl together."""
        existing = isinstance(data, AssistantAssignmentApproval)
        start_task = existing or data.start_task
        if not start_task and data.input_file_ids:
            raise Conflict("Select Library revisions when starting an assignment.")
        request_id = ("assist-assignment-" if existing else "assist-owl-") + hashlib.sha256(
            data.request_id.encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(
            encode(data.model_dump(exclude={"request_id"}, exclude_defaults=True)).encode()
        ).hexdigest()
        with self.connect(write=True) as db:
            assistant = self._bot(db, owner, bot_id)
            if not assistant.get("managed_assistant"):
                raise Conflict("Only Alysis Assistant can offer this handoff.")
            prior = db.execute(
                "SELECT t.bot_id,t.config,a.id AS assignment_id FROM tasks t "
                "LEFT JOIN owl_assignments a ON a.source_task_id=t.id "
                "WHERE t.owner=? AND t.request_id=?",
                (owner, request_id),
            ).fetchone()
            if prior:
                receipt = json.loads(prior["config"]).get("approved_owl_setup", {})
                if prior["bot_id"] != bot_id or receipt.get("fingerprint") != fingerprint:
                    raise Conflict("This request already approved a different owl handoff.")
                bot = self._bot(db, owner, receipt["bot_id"])
                row = (
                    self._assignment_rows(
                        db, owner, bot_id, assignment_id=prior["assignment_id"]
                    ).fetchone()
                    if prior["assignment_id"]
                    else None
                )
                return {
                    "bot": bot,
                    "assignment": self._assignment_item(row) if row else None,
                    **({"playbook": receipt["playbook"]} if "playbook" in receipt else {}),
                }
            if not self._is_coordinator(db, owner, bot_id):
                raise Conflict("Turn on Alysis Assistant before approving the handoff.")
            if start_task:
                self._check_task_queue_capacity(db, owner)
            if existing:
                bot = self._bot(db, owner, data.recipient_bot_id)
                if bot.get("managed_assistant"):
                    raise Conflict("Choose another owl for this assignment.")
                if data.work_scope == "workbook" and not bot["files_enabled"]:
                    raise Conflict(
                        "Turn on files for the assigned owl before approving this workbook."
                    )
                self._check_computer_access(
                    db, bot, data.access.model_dump() if data.access else None
                )
            else:
                if data.access is not None and data.access.file_ids:
                    raise Conflict(
                        "A new owl has no files yet. Create it first, then hand it the files."
                    )
                bot = self.save_bot(
                    owner,
                    BotInput(name=data.name, purpose=data.purpose, instructions=data.instructions),
                    within=db,
                )
            chat = self._ensure_chat(db, owner, bot_id, assistant["name"])
            receipt_id, now = uid(), time.time()
            brief = brief_with_steps(data.brief, data.steps)
            # Steps the person confirmed stay with the owl when they asked to keep them.
            playbook = None
            if data.playbook is not None:
                from .playbooks import teach

                playbook = teach(db, bot, data.steps, data.playbook, now)
            setup = {"fingerprint": fingerprint, "bot_id": bot["id"]}
            if playbook is not None:
                setup["playbook"] = playbook
            db.execute(
                "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,"
                "updated_at,conversation_visible) VALUES(?,?,?,?,?,?,'completed',?,?,?,0)",
                (
                    receipt_id,
                    chat["id"],
                    bot_id,
                    owner,
                    request_id,
                    brief,
                    encode({"approved_owl_setup": setup}),
                    now,
                    now,
                ),
            )
            assignment = None
            if start_task:
                assignment_id = uid()
                db.execute(
                    "INSERT INTO owl_assignments(id,source_task_id,recipient_bot_id,recipient_name,"
                    "brief,expected_result,work_scope,decision,created_at) VALUES(?,?,?,?,?,?,?,'proposed',?)",
                    (
                        assignment_id,
                        receipt_id,
                        bot["id"],
                        bot["name"],
                        brief,
                        data.expected_result,
                        data.work_scope,
                        now,
                    ),
                )
                self._enqueue_assignment_child(
                    db,
                    owner,
                    bot,
                    assignment_id,
                    now,
                    visible=True,
                    access=data.access.model_dump() if data.access else None,
                    input_file_ids=data.input_file_ids,
                )
                assignment = self._assignment_item(
                    self._assignment_rows(db, owner, bot_id, assignment_id=assignment_id).fetchone()
                )
            return {
                "bot": bot,
                "assignment": assignment,
                **({"playbook": playbook} if playbook is not None else {}),
            }

    def _enqueue_assignment_child(
        self,
        db,
        owner,
        recipient,
        assignment_id,
        now,
        *,
        visible=False,
        access=None,
        input_file_ids=None,
    ) -> str:
        chat = self._ensure_chat(db, owner, recipient["id"], recipient["name"])
        child_id = uid()
        config = {**recipient, "assignment_id": assignment_id}
        if input_file_ids:
            row = db.execute(
                "SELECT parent.bot_id,a.work_scope FROM owl_assignments a JOIN tasks parent ON parent.id=a.source_task_id "
                "WHERE a.id=? AND parent.owner=?",
                (assignment_id, owner),
            ).fetchone()
            files = self._handoff_inputs(
                db, owner, row["bot_id"], row["work_scope"], input_file_ids
            )
            shared = self._copy_assignment_files(db, owner, recipient, files)
            db.execute(
                "INSERT INTO owl_assignment_files(assignment_id,inputs) VALUES(?,?) "
                "ON CONFLICT(assignment_id) DO UPDATE SET inputs=excluded.inputs",
                (assignment_id, encode(shared)),
            )
            if access is not None:
                ids = [*access.get("file_ids", []), *(f["id"] for f in shared)]
                if len(ids) > ASSIGNMENT_FILES:
                    raise Conflict("A computer handoff can include up to 20 Library revisions.")
                access = {**access, "file_ids": ids}
        if access is not None:
            # A computer task runs as the owl's own turn, limited to what the card listed, with
            # the approved brief as its request (assignment_computer.py).
            approved = db.execute(
                "SELECT brief,expected_result FROM owl_assignments WHERE id=?", (assignment_id,)
            ).fetchone()
            config["assignment_access"] = access
            config["assignment_brief"] = {
                "brief": approved["brief"],
                "expected_result": approved["expected_result"],
            }
        db.execute(
            "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,"
            "updated_at,conversation_visible) VALUES(?,?,?,?,?,'','queued',?,?,?,?)",
            (
                child_id,
                chat["id"],
                recipient["id"],
                owner,
                "assignment_" + assignment_id,
                encode(config),
                now,
                now,
                int(visible),
            ),
        )
        db.execute(
            "UPDATE owl_assignments SET decision='approved',child_task_id=? WHERE id=?",
            (child_id, assignment_id),
        )
        self._event(db, child_id, "queued", {})
        return child_id

    @staticmethod
    def _handoff_file_ids(ids):
        if (
            not isinstance(ids, list)
            or len(ids) > ASSIGNMENT_FILES
            or any(not isinstance(value, str) or not 1 <= len(value) <= 100 for value in ids)
            or len(set(ids)) != len(ids)
        ):
            raise Conflict("Select up to 20 distinct Library revisions for this handoff.")
        return ids

    @staticmethod
    def _handoff_metadata(file):
        return {
            **{
                k: file[k] for k in ("id", "name", "revision", "size_bytes", "media_type", "sha256")
            },
            "source_file_id": file["id"],
            "source_name": file["name"],
            "source_revision": file["revision"],
            "source_bot_id": file["bot_id"],
        }

    def _handoff_inputs(self, db, owner, bot_id, scope, ids):
        if ids and scope not in {"workbook", "computer"}:
            raise Conflict("Choose a file-capable assignment before sharing Library files.")
        bot = self._bot(db, owner, bot_id)
        return [
            self._handoff_metadata(f)
            for f in self._attachable_files(db, bot, self._handoff_file_ids(ids))
        ]

    def _handoff_outputs(self, db, owner, child, ids):
        files = self._attachable_files(
            db, self._bot(db, owner, child["bot_id"]), self._handoff_file_ids(ids)
        )
        if any(f["source_task_id"] != child["id"] for f in files):
            raise Conflict("An assignment may return only revisions created by this assigned task.")
        if any(f.get("withdrawn_from_reply") for f in files):
            raise Conflict("A withdrawn result cannot be returned from an assignment.")
        return [self._handoff_metadata(f) for f in files]

    def _copy_assignment_files(self, db, owner, destination, files, *, task_id=None):
        """Copy exact immutable revisions inside the task transition. A new Library entry consumes
        logical quota. External payloads gain another live reference to the same immutable object."""
        if not files:
            return []
        if not destination["files_enabled"]:
            raise Conflict("Turn on Library files for the owl receiving this handoff.")
        self._check_library_capacity(db, owner, sum(f["size_bytes"] for f in files))
        copied = []
        for f in files:
            file_id, now = uid(), time.time()
            name = f["name"]
            name = self._handoff_copy_name(db, destination["id"], name, now)
            db.execute(
                "INSERT INTO files(id,bot_id,name,content,source_task_id,created_at,updated_at,series_id,revision) "
                "SELECT ?,?,?,content,?,?,?,?,1 FROM files WHERE id=? AND bot_id=?",
                (
                    file_id,
                    destination["id"],
                    name,
                    task_id,
                    now,
                    now,
                    file_id,
                    f["source_file_id"],
                    f["source_bot_id"],
                ),
            )
            db.execute(
                "INSERT INTO file_payloads(file_id,data,media_type,sha256) "
                "SELECT ?,data,media_type,sha256 FROM file_payloads WHERE file_id=?",
                (file_id, f["source_file_id"]),
            )
            db.execute(
                'INSERT INTO file_objects(file_id,ref,size_bytes,"binary",media_type) '
                'SELECT ?,ref,size_bytes,"binary",media_type FROM file_objects WHERE file_id=?',
                (file_id, f["source_file_id"]),
            )
            copied.append({**f, "id": file_id, "name": name, "revision": 1})
        return copied

    @staticmethod
    def _handoff_copy_name(db, bot_id, original, now):
        """Reserve the first available numbered name, including outstanding uploads."""
        names = (
            "SELECT name FROM files WHERE bot_id=? UNION ALL SELECT name FROM file_uploads "
            "WHERE bot_id=? AND status='pending' AND expires>?"
        )
        values = (bot_id, bot_id, now)
        if not db.execute(
            f"SELECT 1 FROM ({names}) n WHERE unicode_fold(name)=unicode_fold(?) LIMIT 1",
            (*values, original),
        ).fetchone():
            return original
        extension = Path(original).suffix
        if len(extension) > 24:
            extension = ""
        stem = original[: -len(extension)] if extension else original
        # A long stem may be truncated before a combining mark when numbering it.
        # In this uncommon case a folded prefix is not a safe filter.
        prefix = stem if len(stem) + len(extension) + 22 <= 160 else ""
        rows = db.execute(
            f"SELECT DISTINCT unicode_fold(name) AS folded FROM ({names}) n "
            "WHERE instr(unicode_fold(name),unicode_fold(?))=1",
            (*values, prefix),
        )
        occupied = {row["folded"] for row in rows}
        # Use the database's own Unicode folding (SQLite and PostgreSQL can ship
        # different Unicode versions). Only truncation boundaries vary with digit count.
        number = 2
        folded_stems = {}
        folded_extension = db.execute("SELECT unicode_fold(?)", (extension,)).fetchone()[0]
        while True:
            marker = f" ({number})"
            clipped = stem[: 160 - len(marker) - len(extension)]
            if clipped not in folded_stems:
                folded_stems[clipped] = db.execute("SELECT unicode_fold(?)", (clipped,)).fetchone()[
                    0
                ]
            if folded_stems[clipped] + marker + folded_extension not in occupied:
                return clipped + marker + extension
            number += 1

    def assignment_context(self, owner: str, child_task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT a.recipient_name,a.brief,a.expected_result,a.work_scope,r.scheduled_for,"
                "r.config AS routine_config,parent.bot_id AS coordinator_bot_id,af.inputs,af.checkpoint_ids,"
                "af.repair,child.output AS checkpoint_result FROM owl_assignments a "
                "JOIN tasks parent ON parent.id=a.source_task_id "
                "JOIN tasks child ON child.id=a.child_task_id "
                "LEFT JOIN owl_assignment_files af ON af.assignment_id=a.id "
                "LEFT JOIN schedule_runs r ON r.task_id=child.id "
                "WHERE a.child_task_id=? AND parent.owner=? AND child.owner=?",
                (child_task_id, owner, owner),
            ).fetchone()
            if row is None:
                raise NotFound("Assignment not found.")
            scope = {key: row[key] for key in ("recipient_name", "brief", "expected_result")}
            scope.update(
                coordinator_bot_id=row["coordinator_bot_id"],
                input_files=json.loads(row["inputs"] or "[]"),
                checkpoint_file_ids=json.loads(row["checkpoint_ids"] or "[]"),
                checkpoint_result=row["checkpoint_result"] or "",
                repair=json.loads(row["repair"]) if row["repair"] else None,
            )
            if repair := scope["repair"]:
                successor = self._assignment_repair_successor(db, child_task_id, repair["file_id"])
                if successor:
                    repair["saved_file_id"] = successor
            selection = db.execute(
                "SELECT data FROM events WHERE task_id=? AND kind='assignment_outputs_selected' "
                "ORDER BY seq DESC LIMIT 1",
                (child_task_id,),
            ).fetchone()
            scope["selected_output_file_ids"] = (
                json.loads(selection["data"]).get("file_ids", []) if selection else []
            )
            if scope["selected_output_file_ids"]:
                withheld = {
                    r[0]
                    for r in db.execute(
                        "SELECT w.file_id FROM owl_withdrawn_results w JOIN files f ON f.id=w.file_id WHERE f.source_task_id=?",
                        (child_task_id,),
                    )
                }
                scope["selected_output_file_ids"] = [
                    f for f in scope["selected_output_file_ids"] if f not in withheld
                ]
            if row["work_scope"] != "text":
                scope["work_scope"] = row["work_scope"]
            if row["routine_config"] is not None:
                timing = json.loads(row["routine_config"])["timing"]
                scope["occurrence"] = {
                    "scheduled_for": row["scheduled_for"],
                    "time_zone": timing.get("timezone"),
                    "person_zone": self._time_zone(db, owner),
                }
            return scope

    def _assignment_input_bot(self, owner, child_task_id, file_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT af.inputs,b.id,b.files_enabled FROM owl_assignment_files af "
                "JOIN owl_assignments a ON a.id=af.assignment_id "
                "JOIN tasks child ON child.id=a.child_task_id JOIN bots b ON b.id=child.bot_id "
                "WHERE child.id=? AND child.owner=? AND b.owner=? AND a.decision='approved'",
                (child_task_id, owner, owner),
            ).fetchone()
            if not row or not any(f["id"] == file_id for f in json.loads(row["inputs"])):
                raise NotFound("This revision was not shared with the assignment.")
            if not row["files_enabled"]:
                raise Conflict("Library file access is off for this owl.")
            return row["id"]

    def assignment_input_chunks(self, owner: str, child_task_id: str, file_id: str, *, offset=0):
        bot_id = self._assignment_input_bot(owner, child_task_id, file_id)
        return self.file_chunks(owner, bot_id, file_id, offset=offset)

    def assignment_input_data(self, owner: str, child_task_id: str, file_id: str) -> bytes:
        bot_id = self._assignment_input_bot(owner, child_task_id, file_id)
        return self.file_data(owner, bot_id, file_id)

    def checkpoint_assignment_result(
        self,
        owner: str,
        child_task_id: str,
        result: str,
        *,
        output_file_ids: list[str] | None = None,
    ) -> None:
        """Keep the child's actual output before a separate verification call."""
        self._check_assignment_result_size(result)
        with self.connect(write=True) as db:
            task = self._task(db, owner, child_task_id)
            if task["status"] != "running" or not task["config"].get("assignment_id"):
                raise Conflict("The assigned task is no longer running.")
            if output_file_ids is not None:
                self._handoff_outputs(db, owner, task, output_file_ids)
                db.execute(
                    "INSERT INTO owl_assignment_files(assignment_id,checkpoint_ids) VALUES(?,?) "
                    "ON CONFLICT(assignment_id) DO UPDATE SET checkpoint_ids=excluded.checkpoint_ids",
                    (task["config"]["assignment_id"], encode(output_file_ids)),
                )
            db.execute(
                "UPDATE tasks SET output=?,updated_at=? WHERE id=?",
                (result, time.time(), child_task_id),
            )

    @staticmethod
    def _assignment_repair_successor(db, child_task_id, base_file_id):
        row = db.execute(
            "SELECT next.id FROM files base JOIN files next ON next.bot_id=base.bot_id "
            "AND next.series_id=base.series_id AND next.revision=base.revision+1 "
            "WHERE base.id=? AND base.source_task_id=? AND next.source_task_id=?",
            (base_file_id, child_task_id, child_task_id),
        ).fetchone()
        return row["id"] if row else None

    def begin_assignment_repair(
        self, owner: str, child_task_id: str, file_id: str, reason: str
    ) -> bool:
        """Claim the workbook's single repair before asking its specialist to author again."""
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise Conflict("A workbook repair needs the reviewer's bounded reason.")
        with self.connect(write=True) as db:
            child = self._task(db, owner, child_task_id)
            assignment_id = child["config"].get("assignment_id")
            if child["status"] != "running" or not assignment_id:
                raise Conflict("The assigned task is no longer running.")
            receipt = db.execute(
                "SELECT af.checkpoint_ids,af.repair FROM owl_assignment_files af "
                "JOIN owl_assignments a ON a.id=af.assignment_id "
                "WHERE a.id=? AND a.child_task_id=? AND a.work_scope='workbook'",
                (assignment_id, child_task_id),
            ).fetchone()
            if receipt and receipt["repair"] is not None:
                return False
            if not receipt or json.loads(receipt["checkpoint_ids"] or "[]") != [file_id]:
                raise Conflict("Only the exact workbook submitted for review can be repaired.")
            self._handoff_outputs(db, owner, child, [file_id])
            return bool(
                db.execute(
                    "UPDATE owl_assignment_files SET repair=? WHERE assignment_id=? AND repair IS NULL",
                    (encode({"file_id": file_id, "reason": reason.strip()}), assignment_id),
                ).rowcount
            )

    @staticmethod
    def _check_assignment_result_size(result: str) -> None:
        if len(encode(result).encode("utf-8")) > ASSIGNMENT_RESULT_BYTES:
            raise LimitReached("The assignment result exceeds the size limit for saved text.")

    def _return_assignment_files(self, db, owner, child, assignment_id, result, verdict, ids):
        receipt = db.execute(
            "SELECT checkpoint_ids,inputs,repair FROM owl_assignment_files WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        checkpoint = (
            json.loads(receipt["checkpoint_ids"])
            if receipt and receipt["checkpoint_ids"] is not None
            else None
        )
        if ids is None:
            ids = checkpoint
            if not ids:
                return
        elif checkpoint is not None and ids != checkpoint:
            raise Conflict("The returned revisions differ from the result submitted for review.")
        elif ids and checkpoint is None:
            raise Conflict("Save these exact revisions for review before releasing the result.")
        db.execute(
            "INSERT INTO owl_assignment_files(assignment_id) VALUES(?) ON CONFLICT(assignment_id) DO NOTHING",
            (assignment_id,),
        )
        if (
            verdict
            and verdict.status != "met"
            and (ids or (receipt and json.loads(receipt["inputs"])))
        ):
            source = db.execute(
                "SELECT source_task_id FROM owl_assignments WHERE id=?", (assignment_id,)
            ).fetchone()
            self._event(
                db, source["source_task_id"], "assignment_review", {"assignment_id": assignment_id}
            )
        if not ids or verdict is None or verdict.status != "met":
            return
        if receipt and receipt["repair"]:
            repaired_id = self._assignment_repair_successor(
                db, child["id"], json.loads(receipt["repair"])["file_id"]
            )
            if ids != [repaired_id]:
                raise Conflict("Only the repaired workbook revision can be released as corrected.")
        files = self._handoff_outputs(db, owner, child, ids)
        parent = db.execute(
            "SELECT t.bot_id,t.chat_id,a.recipient_name,a.brief,a.expected_result FROM owl_assignments a "
            "JOIN tasks t ON t.id=a.source_task_id WHERE a.id=? AND a.child_task_id=? AND t.owner=?",
            (assignment_id, child["id"], owner),
        ).fetchone()
        lead = self._bot(db, owner, parent["bot_id"])
        if not lead["files_enabled"] and not (receipt and json.loads(receipt["inputs"])):
            # A Library-disabled coordinator can still ask for a workbook in the recipient's
            # Library. Keep its exact reviewed link there, without writing into the disabled Library.
            db.execute(
                "UPDATE owl_assignment_files SET recipient_artifact_id=? WHERE assignment_id=?",
                (files[0]["id"], assignment_id),
            )
            return
        resume_id, now = uid(), time.time()
        db.execute(
            "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'','queued',?,?,?)",
            (
                resume_id,
                parent["chat_id"],
                lead["id"],
                owner,
                "assignment-return-" + assignment_id,
                encode(lead),
                now,
                now,
            ),
        )
        returned = self._copy_assignment_files(db, owner, lead, files, task_id=resume_id)
        db.execute(
            "UPDATE owl_assignment_files SET outputs=? WHERE assignment_id=?",
            (encode(returned), assignment_id),
        )
        delivery = {
            "assignment_id": assignment_id,
            "child_task_id": child["id"],
            **{k: parent[k] for k in ("recipient_name", "brief", "expected_result")},
            "result": result,
            "verification": verdict.model_dump(),
            "returned_files": returned,
        }
        db.execute(
            "UPDATE tasks SET config=? WHERE id=?",
            (encode({**lead, "assignment_return": delivery}), resume_id),
        )
        for ordinal, file in enumerate(returned):
            db.execute(
                "INSERT INTO task_files(task_id,ordinal,file_id,name,media_type,size_bytes) VALUES(?,?,?,?,?,?)",
                (
                    resume_id,
                    ordinal,
                    file["id"],
                    file["name"],
                    file["media_type"],
                    file["size_bytes"],
                ),
            )
        self._event(db, resume_id, "queued", {})

    @staticmethod
    def _check_task_queue_capacity(db, owner: str) -> None:
        if (
            db.execute(
                "SELECT count(*) FROM tasks WHERE owner=? AND status IN ('queued','running','waiting','cancelling')",
                (owner,),
            ).fetchone()[0]
            >= 20
        ):
            raise LimitReached("The task queue is full. Wait for tasks to finish.")

    def continue_without_answer(self, db, owner: str, source: dict, question) -> dict | None:
        """Queue the turn that carries ``source`` on after the person skipped its question.

        Only a finished turn that ended on the question, with nothing asked of the owl since in
        its chat, goes on: a newer request already moved the conversation on. Like a first hello
        the turn has no message from the person; its request is built at run time from its
        config (reply_style.skipped_question_request). None when nothing should go on.
        """
        request_id = "clarification_skip_" + question["id"]
        previous = db.execute(
            "SELECT id FROM tasks WHERE owner=? AND request_id=?", (owner, request_id)
        ).fetchone()
        if previous:
            return self._task(db, owner, previous["id"])
        from .queued_messages import not_withdrawn

        if (
            source["status"] != "completed"
            or db.execute(
                "SELECT 1 FROM tasks t WHERE t.chat_id=? AND t.id<>? AND t.created_at>=? "
                f"AND {not_withdrawn('t')} LIMIT 1",
                (source["chat_id"], source["id"], source["created_at"]),
            ).fetchone()
        ):
            return None
        self._check_task_queue_capacity(db, owner)
        bot = self._bot(db, owner, source["bot_id"])
        task_id, now = uid(), time.time()
        # The person moved its conversation on: an archived owl comes back (_bring_back).
        if bot["archived_at"] is not None:
            self._bring_back(db, bot["id"], now)
            bot = self._bot(db, owner, bot["id"])
        db.execute(
            "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                source["chat_id"],
                bot["id"],
                owner,
                request_id,
                "",
                "queued",
                encode(
                    {
                        **bot,
                        "reply_review_enabled": self._reply_review_enabled(db, owner),
                        "skipped_question": {"question": question["question"]},
                    }
                ),
                now,
                now,
            ),
        )
        self._event(db, task_id, "queued", {})
        return self._task(db, owner, task_id)

    def regenerate(self, owner: str, task_id: str, data: RegenerateInput) -> dict:
        """Queue another answer to the person's message that ``task_id`` answered (regenerate.py).

        Only the owl's latest turn can be answered again, once it finished and while nothing of
        this owl is working: a finished answer to the person's own message, or an answer asked
        this way that didn't finish (trying it again). Like a first hello the turn has no message
        of its own; its request is the person's message, read at run time. The same request id
        returns the turn already queued.
        """
        from .queued_messages import not_withdrawn

        with self.connect(write=True) as db:
            source = self._task(db, owner, task_id)
            previous = db.execute(
                "SELECT id,config FROM tasks WHERE owner=? AND request_id=?",
                (owner, data.request_id),
            ).fetchone()
            if previous:
                asked = json.loads(previous["config"]).get("regenerate") or {}
                if asked.get("source") != task_id:
                    raise Conflict("This request ID was already used for a different message.")
                return self._task(db, owner, previous["id"])
            bot = self._bot(db, owner, source["bot_id"])
            _, scope = self._scope(db, owner, source["chat_id"])
            if scope.room:
                raise Conflict("A reply in a room can't get a new answer yet.")
            # A message the person cancelled before it started is not in the chat (queued_messages).
            latest = db.execute(
                f"SELECT id FROM tasks t WHERE {scope.tasks} AND {not_withdrawn('t')} "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (scope.value,),
            ).fetchone()
            if latest is None or latest["id"] != task_id:
                raise Conflict("Only the latest reply can get a new answer.")
            if db.execute(
                "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling') LIMIT 1",
                (bot["id"],),
            ).fetchone():
                raise Conflict("Your owl is still working. Ask for a new answer once it's done.")
            earlier = regenerates(source)
            if earlier:
                # Trying again an answer asked this way: it replaces what that one replaced,
                # unless it finished, and then it is the answer to replace.
                asked = source["config"]["regenerate"]
                original = self._task(db, owner, earlier)
                replaces = task_id if source["status"] == "completed" else asked["replaces"]
            else:
                if source["status"] != "completed":
                    raise Conflict("Only a finished reply can get a new answer.")
                original, replaces = source, task_id
            spoken = db.execute(
                "SELECT 1 FROM messages WHERE task_id=? AND role='user' LIMIT 1", (original["id"],)
            ).fetchone()
            if not spoken or not persons_request(original):
                raise Conflict("Only a reply to your own message can get a new answer.")
            target = original["config"].get("interactive_target")
            if target:
                self._interactive_task_base(db, owner, original, target)
            if (
                db.execute(
                    "SELECT count(*) FROM tasks WHERE owner=? AND status IN ('queued','running','waiting','cancelling')",
                    (owner,),
                ).fetchone()[0]
                >= 20
            ):
                raise LimitReached("The task queue is full. Wait for tasks to finish.")
            new_id, now = uid(), time.time()
            # The person asked for it, so an archived owl comes back (_bring_back).
            if bot["archived_at"] is not None:
                self._bring_back(db, bot["id"], now)
                bot = self._bot(db, owner, bot["id"])
            db.execute(
                "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id,
                    source["chat_id"],
                    bot["id"],
                    owner,
                    data.request_id,
                    # The person's words, for previews and lists; the turn reads the message.
                    original["message"],
                    "queued",
                    encode(
                        {
                            **bot,
                            "reply_review_enabled": self._reply_review_enabled(db, owner),
                            **({"interactive_target": target} if target else {}),
                            "regenerate": {
                                "task_id": original["id"],
                                "replaces": replaces,
                                "source": task_id,
                            },
                        }
                    ),
                    now,
                    now,
                ),
            )
            if target:
                db.execute(
                    "INSERT INTO task_files(task_id,ordinal,file_id,name,media_type,size_bytes) "
                    "SELECT ?,ordinal,file_id,name,media_type,size_bytes FROM task_files WHERE task_id=?",
                    (new_id, original["id"]),
                )
            self._event(db, new_id, "queued", {})
            return self._task(db, owner, new_id)

    def _enqueue(
        self,
        db: sqlite3.Connection,
        owner: str,
        chat_id: str,
        data: TaskInput,
        *,
        bot_id: str | None = None,
        require_active_assistant: bool = False,
    ) -> dict:
        """Queue a message for an owl. In a room's chat, ``bot_id`` names the owl it is for (the
        room's own route sends the person's messages: rooms.py)."""
        chat = self._chat(db, owner, chat_id)
        room = self._room_row(db, chat_id) is not None
        if room:
            if bot_id is None:
                raise Conflict("Send messages in a room through the room.")
            bot = self._bot(db, owner, bot_id)
            if not self._room_member(db, chat_id, bot_id):
                raise Conflict(f"{bot['name']} isn't in this room any more.")
        else:
            bot = self._bot(db, owner, chat["bot_id"])
            if bot_id is not None and bot_id != bot["id"]:
                raise Conflict("This conversation belongs to another owl.")
        previous = db.execute(
            "SELECT id,chat_id,bot_id,message,config FROM tasks WHERE owner=? AND request_id=?",
            (owner, data.request_id),
        ).fetchone()
        if previous:
            from .queued_messages import sent_as

            saved_target = json.loads(previous["config"]).get("interactive_target")
            original_target = (
                {k: v for k, v in saved_target.items() if k in InteractiveTarget.model_fields}
                if saved_target
                else None
            )
            proposed_target = (
                data.interactive_target.model_dump(exclude_none=True)
                if data.interactive_target
                else None
            )
            if (
                previous["bot_id"] != bot["id"]
                or original_target != proposed_target
                or (saved_target and previous["chat_id"] != chat_id)
                or (
                    previous["chat_id"] != chat_id
                    and (self._is_thread(db, chat_id) or self._is_thread(db, previous["chat_id"]))
                )
                # A message changed or cancelled while it waited is still the one first sent.
                or (
                    previous["message"] != data.message
                    and not sent_as(db, previous["id"], data.message)
                )
                or self._attached_file_ids(db, previous["id"]) != data.file_ids
            ):
                raise Conflict("This request ID was already used for a different message.")
            return self._task(db, owner, previous["id"])
        if (
            require_active_assistant
            and bot.get("managed_assistant")
            and not bot["assistant_enabled"]
        ):
            raise Conflict("Turn on Assist before sending a new message to Alysis Assistant.")
        attached = self._attachable_files(db, bot, data.file_ids)
        interactive_target = None
        if target := data.interactive_target:
            if room:
                raise Conflict("Open the interactive result in its owl's original chat.")
            file = self._interactive_file(db, owner, bot["id"], target.file_id)
            if file["source_chat_id"] is not None and file["source_chat_id"] != chat_id:
                raise Conflict("Open the interactive result in its original chat.")
            self._check_interactive_version(
                file, target.revision, target.sha256, target.state_version
            )
            interactive_target = {
                **target.model_dump(exclude_none=True),
                "series_id": file["series_id"],
                "name": file["name"],
            }
        elif not room and not self._is_thread(db, chat_id):
            chat = self._ensure_chat(db, owner, bot["id"], bot["name"])
        chat_id = chat["id"]
        self._check_task_queue_capacity(db, owner)
        task_id, now = uid(), time.time()
        # Routines and file events never reach an archived owl; anything else here is work the
        # person started for it (a message, an answer, an approval, Run now), so it comes back.
        if bot["archived_at"] is not None:
            self._bring_back(db, bot["id"], now)
            bot = self._bot(db, owner, bot["id"])
        db.execute(
            "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                chat_id,
                bot["id"],
                owner,
                data.request_id,
                data.message,
                "queued",
                # Keep the choice stable through approvals, reconnects and recovery.
                encode(
                    {
                        **bot,
                        "reply_review_enabled": self._reply_review_enabled(db, owner),
                        **(
                            {"interactive_target": interactive_target} if interactive_target else {}
                        ),
                        **(
                            {"assistant_screen_requests": True}
                            if bot.get("managed_assistant") and data.assistant_screen_requests
                            else {}
                        ),
                    }
                ),
                now,
                now,
            ),
        )
        message_id = uid()
        db.execute(
            "INSERT INTO messages(id,chat_id,task_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
            (message_id, chat_id, task_id, "user", data.message, now),
        )
        if room:
            db.execute(
                "INSERT INTO room_turns(task_id,message_id) VALUES(?,?)", (task_id, message_id)
            )
        for ordinal, file in enumerate(attached):
            db.execute(
                "INSERT INTO task_files(task_id,ordinal,file_id,name,media_type,size_bytes) "
                "VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    ordinal,
                    file["id"],
                    file["name"],
                    file["media_type"],
                    file["size_bytes"],
                ),
            )
        if not room and chat["title"] == "New conversation":
            title = data.message.strip() or (attached[0]["name"] if attached else "")
            db.execute("UPDATE chats SET title=? WHERE id=?", (title[:80], chat_id))
        self._event(db, task_id, "queued", {})
        return self._task(db, owner, task_id)

    @staticmethod
    def _attached_file_ids(db, task_id: str) -> list[str]:
        return [
            row[0]
            for row in db.execute(
                "SELECT file_id FROM task_files WHERE task_id=? ORDER BY ordinal", (task_id,)
            )
        ]

    @classmethod
    def _attachable_files(cls, db, bot: dict, file_ids: list[str]) -> list[dict]:
        """The files a person attached, each one checked to be in this owl's Library now."""
        if not file_ids:
            return []
        if not bot["files_enabled"]:
            raise Conflict(
                "File access is off for this owl, so it can't open attached files. "
                "Turn on file access in the owl's settings, or send the message without files."
            )
        files = []
        for file_id in file_ids:
            row = db.execute(
                "SELECT * FROM files WHERE id=? AND bot_id=?", (file_id, bot["id"])
            ).fetchone()
            if row is None:
                raise Conflict(
                    "An attached file isn't in this owl's Library anymore. "
                    "Remove it from the message and attach it again."
                )
            files.append(cls._file_metadata(db, row))
        return files

    @staticmethod
    def _message_files(db, scope: ChatScope, *, only=None) -> dict[str, list[dict]]:
        """Each task's attached files as sent, and whether each is still in the Library."""
        attached: dict[str, list[dict]] = {}
        if only is not None and not only:
            return attached
        within = "" if only is None else f" AND t.id IN ({','.join('?' * len(only))})"
        for row in db.execute(
            "SELECT a.task_id,a.file_id,a.name,a.media_type,a.size_bytes,"
            "EXISTS(SELECT 1 FROM files f WHERE f.id=a.file_id AND f.bot_id=t.bot_id) AS available "
            f"FROM task_files a JOIN tasks t ON t.id=a.task_id WHERE {scope.tasks}{within} "
            "ORDER BY a.task_id,a.ordinal",
            (scope.value, *(only or ())),
        ):
            attached.setdefault(row["task_id"], []).append(
                {
                    "id": row["file_id"],
                    "name": row["name"],
                    "media_type": row["media_type"],
                    "size_bytes": row["size_bytes"],
                    "available": bool(row["available"]),
                }
            )
        return attached

    def task(self, owner: str, task_id: str) -> dict:
        with self.connect() as db:
            task = self._task(db, owner, task_id)
            task["events"] = [
                {**dict(row), "data": json.loads(row["data"])}
                for row in db.execute(
                    "SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,)
                )
            ]
            task["usage"] = [
                {**dict(row), "usage": json.loads(row["usage"]) if row["usage"] else None}
                for row in db.execute(
                    "SELECT * FROM provider_calls WHERE task_id=? ORDER BY created_at", (task_id,)
                )
            ]
            return task

    def claim(self, owner: str, *, admit: Callable[[], bool] | None = None) -> dict | None:
        """The next turn to run: one at a time per owl, and in a room one at a time, in the order
        the turns were asked for (rooms.py), so each owl reads the answers before its own.

        ``admit`` is asked once a turn is found, inside the claim's transaction: False leaves
        it as it is. A runtime that is stopping passes it, so a turn it has just queued to
        start again after the restart (restart_cut) is never taken by another of its workers.
        """
        from .hosted_maintenance import MaintenanceLease
        from .steering import release_ended

        with self.connect(write=True) as db:
            if MaintenanceLease.pending(db):
                return None
            # A message sent to a turn that stopped before reading it waits its turn, as below.
            release_ended(db, owner)
            row = db.execute(
                f"""SELECT t.id FROM tasks t WHERE t.owner=?
                AND NOT EXISTS (SELECT 1 FROM tasks active WHERE active.bot_id=t.bot_id
                    AND active.status IN ('running','cancelling'))
                AND (t.chat_id NOT IN ({self.ROOM_CHATS}) OR NOT EXISTS (SELECT 1 FROM tasks e
                    WHERE e.chat_id=t.chat_id AND e.id<>t.id AND (e.status IN ('running','cancelling')
                    OR (e.status='queued' AND (e.created_at<t.created_at
                        OR (e.created_at=t.created_at AND e.id<t.id))))))
                AND (t.status='queued' OR (t.status='waiting' AND EXISTS
                    (SELECT 1 FROM coding_jobs j WHERE j.task_id=t.id
                     AND j.status NOT IN ('queued','running')))
                    OR (t.status='waiting' AND EXISTS
                    (SELECT 1 FROM peer_exchanges p WHERE p.source_task_id=t.id
                     AND p.status IN ('completed','failed','interrupted'))))
                ORDER BY CASE WHEN t.status='queued' AND NOT EXISTS
                    (SELECT 1 FROM schedule_runs sr WHERE sr.task_id=t.id) AND NOT EXISTS
                    (SELECT 1 FROM trigger_deliveries td WHERE td.task_id=t.id) THEN 0 ELSE 1 END,
                    t.created_at,t.id LIMIT 1""",
                (owner,),
            ).fetchone()
            if row is None or (admit is not None and not admit()):
                return None
            db.execute(
                "UPDATE tasks SET status='running',updated_at=? WHERE id=?", (time.time(), row[0])
            )
            self._event(db, row[0], "running", {})
            return self._task(db, owner, row[0])

    def continuation(self, owner: str, task_id: str) -> dict | None:
        with self.connect() as db:
            self._task(db, owner, task_id)
            row = db.execute(
                "SELECT continuation FROM task_waits WHERE task_id=?", (task_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def waiting_tasks(self, owner: str) -> list[str]:
        with self.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT id FROM tasks WHERE owner=? AND status='waiting'", (owner,)
                )
            ]

    def cancel(self, owner: str, task_id: str) -> dict:
        with self.connect(write=True) as db:
            task = self._task(db, owner, task_id)
            if task["status"] in ("queued", "running", "waiting"):
                local_active = db.execute(
                    "SELECT 1 FROM bot_local_requests WHERE task_id=? AND status='claimed'",
                    (task_id,),
                ).fetchone()
                state = "cancelling" if task["status"] == "running" or local_active else "cancelled"
                db.execute(
                    "UPDATE tasks SET status=?,updated_at=? WHERE id=?",
                    (state, time.time(), task_id),
                )
                self._event(db, task_id, state, {})
                db.execute(
                    "UPDATE peer_exchanges SET status='cancelled',updated_at=? "
                    "WHERE source_task_id=? AND reported_at IS NULL",
                    (time.time(), task_id),
                )
            return self._task(db, owner, task_id)

    def finish(
        self,
        owner: str,
        task_id: str,
        status: str,
        *,
        content: str = "",
        error: str | None = None,
        publish_reply: bool = True,
        failure: dict | None = None,
        routine_failure: RunFailure | None = None,
        assignment_verification: dict | None = None,
        assignment_output_file_ids: list[str] | None = None,
        checked: bool = False,
        nothing_new: str | None = None,
    ) -> None:
        """End a task. ``failure`` (failures.py) says why a failed or interrupted turn stopped, for
        the person; ``routine_failure`` says what a failed routine run means for its routine (retry,
        wait for a limit, or pause). ``checked``: the reply passed the reply checker as a reply
        (not a plain status), so a playbook the run wrote may be kept (playbooks.py), and the
        person's chat is read for what to remember once it goes quiet (memory_capture.py).

        ``nothing_new``: a routine's run completed having found nothing new, for this short
        reason of the owl's own. It posts no reply, and a conversation the person had read up
        to here stays read, so nothing calls them to it (the nothing_new event says it checked).
        """
        if status not in TERMINAL:
            raise ValueError("Invalid terminal task state.")
        with self.connect(write=True) as db:
            task = self._task(db, owner, task_id)
            if task["status"] in TERMINAL:
                return
            if (
                nothing_new is not None
                and not db.execute(
                    "SELECT 1 FROM schedule_runs WHERE task_id=?", (task_id,)
                ).fetchone()
            ):
                raise ValueError("Only a routine's run ends with nothing new.")
            if task["status"] == "cancelling":
                status, content, error = "cancelled", "", None
            assignment_id = task["config"].get("assignment_id")
            if assignment_id:
                publish_reply = bool(
                    db.execute(
                        "SELECT 1 FROM tasks WHERE id=? AND conversation_visible=1", (task_id,)
                    ).fetchone()
                )
                content = content or task["output"]
                self._check_assignment_result_size(content)
            if status == "completed" and self._defer_peer_report(db, task):
                return
            if (
                status == "completed"
                and task["status"] == "waiting"
                and not self.executing(db, task)
            ):
                status, content, error = (
                    "failed",
                    "",
                    "A correction arrived before completion. Review saved results before continuing.",
                )
                failure = failures.failure(failures.UNKNOWN)
            if status not in ("failed", "interrupted"):
                failure = None
            if assignment_id and status == "completed":
                verdict = (
                    AssignmentVerification.model_validate(assignment_verification)
                    if assignment_verification is not None
                    else None
                )
                if verdict is not None:
                    db.execute(
                        "UPDATE owl_assignments SET verification=? WHERE id=? AND child_task_id=?",
                        (verdict.model_dump_json(), assignment_id, task_id),
                    )
                if nothing_new is None:
                    self._return_assignment_files(
                        db, owner, task, assignment_id, content, verdict, assignment_output_file_ids
                    )
                # A routine's run that checked and found nothing new has no result to review.
                if (verdict is None or verdict.status != "met") and nothing_new is None:
                    routine_failure = RunFailure("review")
            quiet = status == "completed" and nothing_new is not None
            if quiet:
                content, publish_reply = "", False
                read_before = not self._unread(db, task["chat_id"])
            now = time.time()
            db.execute(
                "UPDATE tasks SET status=?,output=?,error=?,failure=?,updated_at=? WHERE id=?",
                (status, content, error, encode(failure) if failure else None, now, task_id),
            )
            if status == "completed" and publish_reply:
                db.execute(
                    "INSERT INTO messages(id,chat_id,task_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
                    (uid(), task["chat_id"], task_id, "assistant", content, now),
                )
            from .playbooks import settle_run

            # A playbook the run wrote is kept only with a published, checked reply.
            settle_run(
                db,
                task_id,
                succeeded=status == "completed" and publish_reply and bool(content) and checked,
                now=now,
            )
            if quiet:
                self._event(db, task_id, "nothing_new", {"reason": nothing_new})
            if status == "completed":
                from .memory_capture import note_turn

                # The person's own turn with a checked reply: their chat is read for what to
                # remember once it has been quiet a while (memory_capture.py).
                note_turn(
                    db, task, published=publish_reply and bool(content), checked=checked, now=now
                )
            self._event(
                db,
                task_id,
                status,
                {
                    **({"error": error} if error else {}),
                    **({"failure": failure} if failure else {}),
                    **({"nothing_new": True} if quiet else {}),
                },
            )
            if quiet and read_before:
                # Nothing new to read: the conversation stays read through this run's end.
                ended = db.execute(
                    "SELECT max(seq) FROM events WHERE task_id=? AND kind='completed'", (task_id,)
                ).fetchone()[0]
                db.execute(
                    """INSERT INTO chat_read_cursors(chat_id,event_seq) VALUES(?,?)
                    ON CONFLICT(chat_id) DO UPDATE SET event_seq=CASE
                    WHEN chat_read_cursors.event_seq>excluded.event_seq
                    THEN chat_read_cursors.event_seq ELSE excluded.event_seq END""",
                    (task["chat_id"], ended),
                )
            if status == "completed" and content and publish_reply:
                db.execute(
                    "UPDATE peer_exchanges SET reported_at=?,updated_at=? WHERE source_task_id=?",
                    (now, now, task_id),
                )
            self._settle_schedule_run(db, task_id, status, routine_failure)

    @classmethod
    def _defer_peer_report(cls, db, task: dict) -> bool:
        if (
            task["status"] != "running"
            or not db.execute(
                "SELECT 1 FROM peer_exchanges WHERE source_task_id=? AND reported_at IS NULL",
                (task["id"],),
            ).fetchone()
        ):
            return False
        if not db.execute(
            "SELECT 1 FROM tasks t WHERE t.bot_id=? AND t.status='queued' "
            "AND NOT EXISTS (SELECT 1 FROM schedule_runs sr WHERE sr.task_id=t.id) "
            "AND NOT EXISTS (SELECT 1 FROM trigger_deliveries td WHERE td.task_id=t.id)",
            (task["bot_id"],),
        ).fetchone():
            return False
        # A new user message may be a correction or Stop. Let its ordinary
        # structured control turn run before publishing the older peer report.
        db.execute(
            "UPDATE tasks SET status='waiting',updated_at=? WHERE id=?",
            (time.time(), task["id"]),
        )
        cls._event(
            db,
            task["id"],
            "peer_report_deferred",
            {"message": "Checking your new message before reporting the peer review."},
        )
        return True

    def defer_peer_report(self, owner: str, task_id: str) -> bool:
        with self.connect(write=True) as db:
            return self._defer_peer_report(db, self._task(db, owner, task_id))

    def recover(self, owner: str) -> int:
        """After a crash, an update or sleep: settle every turn the runtime was working on.

        Never replay a task that might have already performed an action: a turn starts again
        once only when nothing it did reached outside the owl's own records (_cut_by_restart).
        """
        with self.connect(write=True) as db:
            peer_rows = db.execute(
                "SELECT p.id,p.source_task_id FROM peer_exchanges p JOIN tasks t ON t.id=p.source_task_id "
                "WHERE t.owner=? AND p.status='running'",
                (owner,),
            ).fetchall()
            for peer in peer_rows:
                reason = (
                    "The runtime stopped during peer work. Its uncertain response was not replayed."
                )
                db.execute(
                    "UPDATE peer_exchanges SET status='interrupted',error=?,updated_at=? WHERE id=?",
                    (reason, time.time(), peer["id"]),
                )
                self._event(
                    db,
                    peer["source_task_id"],
                    "peer_interrupted",
                    {"exchange_id": peer["id"], "message": reason},
                )
            rows = db.execute(
                "SELECT id,status FROM tasks WHERE owner=? AND (status IN ('running','cancelling') OR (status='waiting' AND id IN (SELECT task_id FROM background_turns WHERE state='running')))",
                (owner,),
            ).fetchall()
            for row in rows:
                if row["status"] == "cancelling":
                    reason = "The runtime stopped before this task finished. Review saved results before sending a new task."
                    db.execute(
                        "UPDATE tasks SET status='cancelled',error=?,failure=NULL,updated_at=? "
                        "WHERE id=?",
                        (reason, time.time(), row["id"]),
                    )
                    self._event(db, row["id"], "cancelled", {"error": reason})
                    from .playbooks import settle_run

                    settle_run(db, row["id"], succeeded=False, now=time.time())
                else:
                    self._cut_by_restart(db, self._task(db, owner, row["id"]))
            db.execute(
                "UPDATE provider_calls SET status='unknown',updated_at=? WHERE status='started' AND task_id IN (SELECT id FROM tasks WHERE owner=?)",
                (time.time(), owner),
            )
            return len(rows)

    def event(self, owner: str, task_id: str, kind: str, data: dict) -> None:
        with self.connect(write=True) as db:
            self._task(db, owner, task_id)
            self._event(db, task_id, kind, data)

    def show_reply_draft(self, owner: str, task_id: str, text: str) -> int | None:
        """Record the owl's draft reply as the reply checker starts on it, for the owner's live
        view of the turn (task_events.REPLY_DRAFT). Returns its number in the task, or None when
        there is nothing to show: no text, or a turn that is stopping or has ended."""
        with self.connect(write=True) as db:
            task = self._task(db, owner, task_id)
            if not text.strip() or task["status"] not in ("running", "waiting"):
                return None
            # A draft still open is replaced by this one.
            self._settle_reply_drafts(db, task_id)
            number = (
                db.execute(
                    "SELECT count(*) FROM events WHERE task_id=? AND kind=?", (task_id, REPLY_DRAFT)
                ).fetchone()[0]
                + 1
            )
            self._event(db, task_id, REPLY_DRAFT, {"draft": number, "text": text})
            return number

    def withdraw_reply_draft(self, owner: str, task_id: str) -> None:
        """The draft being checked will not be published (the checker withheld it, its check
        failed, or the turn set it aside): its text goes, and reply_draft_withdrawn says so. A
        turn with no draft open is left as it is."""
        with self.connect(write=True) as db:
            self._task(db, owner, task_id)
            draft = self._open_reply_draft(db, task_id)
            if draft is not None:
                self._event(db, task_id, REPLY_DRAFT_WITHDRAWN, {"draft": draft["draft"]})

    @staticmethod
    def _open_reply_draft(db, task_id: str) -> dict | None:
        """The task's latest draft while it still carries its text, i.e. while it is checked."""
        row = db.execute(
            "SELECT data FROM events WHERE task_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
            (task_id, REPLY_DRAFT),
        ).fetchone()
        data = json.loads(row["data"]) if row is not None else None
        return data if isinstance(data, dict) and "text" in data else None

    @staticmethod
    def _settle_reply_drafts(db, task_id: str) -> None:
        """Remove the text of the task's drafts: a draft's text lasts only while it is checked."""
        for row in db.execute(
            "SELECT seq,data FROM events WHERE task_id=? AND kind=?", (task_id, REPLY_DRAFT)
        ).fetchall():
            data = json.loads(row["data"])
            if isinstance(data, dict) and "text" in data:
                del data["text"]
                db.execute("UPDATE events SET data=? WHERE seq=?", (encode(data), row["seq"]))

    def restart_cut(self, owner: str, task_id: str) -> bool:
        """The runtime is stopping under this turn (an app update or quit): settle it now as a
        restart would (_cut_by_restart). False when it is not a turn being worked on (a turn the
        person stopped, or one already settled), for the caller to finish as before."""
        with self.connect(write=True) as db:
            task = self._task(db, owner, task_id)
            if task["status"] != "running" and not (
                task["status"] == "waiting"
                and db.execute(
                    "SELECT 1 FROM background_turns WHERE task_id=? AND state='running'",
                    (task_id,),
                ).fetchone()
            ):
                return False
            self._cut_by_restart(db, task)
            return True

    def _cut_by_restart(self, db, task: dict) -> None:
        """A restart cut this turn off. It starts again once, told so, when its step receipts
        show nothing it did reached outside the owl's own records (step_effects.py): a turn in
        the chat queues again and reads the conversation afresh; work off the reply loop carries
        on from its saved checkpoint. Otherwise, or the second time, it is interrupted, and its
        failure says why it was not started again (failures.restart_failure)."""
        from .playbooks import settle_run
        from .step_effects import RESTART_RERUNS, first_outward_step, restart_note, steps_taken

        task_id, now = task["id"], time.time()
        step = first_outward_step(db, task_id)
        if step is None and task["restarts"] < RESTART_RERUNS:
            steps = steps_taken(db, task_id)
            if task["status"] == "waiting":
                checkpoint = db.execute(
                    "SELECT messages FROM background_turns WHERE task_id=?", (task_id,)
                ).fetchone()
                # A current state transition, as when the person gives the computer back
                # (computer_control.py): the execution reads it as one, not as old reference.
                messages = [
                    *json.loads(checkpoint["messages"]),
                    {
                        "role": "system",
                        "_bot_control_return": task_id,
                        "content": restart_note(steps),
                    },
                ]
                db.execute(
                    "UPDATE background_turns SET state='queued',messages=? WHERE task_id=?",
                    (encode(messages), task_id),
                )
            else:
                db.execute("UPDATE tasks SET status='queued' WHERE id=?", (task_id,))
            db.execute(
                "UPDATE tasks SET restarts=restarts+1,updated_at=? WHERE id=?", (now, task_id)
            )
            # Nobody holds the owl's computer for a turn that starts again.
            db.execute("DELETE FROM bot_computer_control WHERE task_id=?", (task_id,))
            self._event(db, task_id, "restarted", {"restarts": task["restarts"] + 1})
            return
        # The service restarted under the task: a service cause, never the person's setup.
        if step is None:
            failure = failures.restart_failure("already_rerun")
            reason = (
                "The runtime stopped before this task finished, after it had already started "
                "again once. Review saved results before sending a new task."
            )
        else:
            failure = failures.restart_failure(
                "acted" if step.finished else "unconfirmed", step.name
            )
            reason = (
                "The runtime stopped before this task finished. It did not start again on its "
                "own because a step it took "
                + ("changed something" if step.finished else "may have changed something")
                + ". Review saved results before sending a new task."
            )
        db.execute(
            "UPDATE tasks SET status='interrupted',error=?,failure=?,updated_at=? WHERE id=?",
            (reason, encode(failure), now, task_id),
        )
        self._event(db, task_id, "interrupted", {"error": reason, "failure": failure})
        self._settle_schedule_run(db, task_id, "interrupted", RunFailure("interrupted"))
        settle_run(db, task_id, succeeded=False, now=now)

    def restart_steps(self, owner: str, task_id: str) -> list[dict] | None:
        """For a turn started again after a restart, its newest steps before that; else None."""
        from .step_effects import steps_taken

        with self.connect() as db:
            task = self._task(db, owner, task_id)
            restarted = db.execute(
                "SELECT max(seq) FROM events WHERE task_id=? AND kind='restarted'", (task_id,)
            ).fetchone()[0]
            if not task["restarts"] or restarted is None:
                return None
            return steps_taken(db, task_id, before=restarted)

    def has_model_calls(self, owner, task_id, task_limit, workspace_limit, needed):
        """May optional work spend a call and still leave the requested room for the answer?"""
        if task_limit is None and workspace_limit is None:
            return True
        with self.connect() as db:
            if task_limit is not None:
                calls = db.execute(
                    "SELECT calls FROM tasks WHERE id=? AND owner=?", (task_id, owner)
                ).fetchone()
                if not calls or task_limit - calls[0] < needed:
                    return False
            if workspace_limit is not None:
                used = db.execute(
                    "SELECT count(*) FROM provider_calls p JOIN tasks t ON t.id=p.task_id WHERE t.owner=?",
                    (owner,),
                ).fetchone()[0]
                if workspace_limit - used < needed:
                    return False
        return True

    def reserve_call(
        self,
        owner: str,
        task_id: str,
        model: str,
        max_calls: int | None,
        *,
        background: bool = False,
        purpose: str = "turn",
    ) -> str:
        with self.connect(write=True) as db:
            if background:
                task = self._task(db, owner, task_id)
                if not self.executing(db, task):
                    if (
                        task["status"] == "waiting"
                        and db.execute(
                            "SELECT 1 FROM background_turns WHERE task_id=? AND state='running'",
                            (task_id,),
                        ).fetchone()
                    ):
                        raise BackgroundInstructionsPending(
                            "New instructions arrived before this model step."
                        )
                    raise Conflict("Background work changed before this model step.")
            return self._reserve_call(
                db, owner, task_id, model, max_calls, background=background, purpose=purpose
            )

    @classmethod
    def _reserve_call(
        cls,
        db,
        owner: str,
        task_id: str,
        model: str,
        max_calls: int | None,
        *,
        background: bool = False,
        purpose: str = "turn",
    ) -> str:
        if purpose not in CALL_PURPOSES:
            raise ValueError(f"Unknown model call purpose: {purpose}")
        task = cls._task(db, owner, task_id)
        if task["status"] not in (("running", "waiting") if background else ("running",)):
            raise Conflict("Task is no longer running.")
        count = db.execute(
            "SELECT count(*) FROM provider_calls p JOIN tasks t ON t.id=p.task_id WHERE t.owner=?",
            (owner,),
        ).fetchone()[0]
        if max_calls is not None and count >= max_calls:
            raise LimitReached(
                "This workspace has used its model-call allowance, so new work can't start. "
                "Your messages are saved; raise the allowance to continue."
            )
        call_id, now = uid(), time.time()
        db.execute(
            "INSERT INTO provider_calls VALUES(?,?,?,?,?,?,?)",
            (call_id, task_id, model, "started", None, now, now),
        )
        db.execute("UPDATE tasks SET calls=calls+1 WHERE id=?", (task_id,))
        cls._event(
            db, task_id, "model_started", {"model": model, "call_id": call_id, "purpose": purpose}
        )
        return call_id

    def finish_call(self, call_id: str, status: str, usage: dict | None) -> None:
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE provider_calls SET status=?,usage=?,updated_at=? WHERE id=?",
                (status, encode(usage) if usage is not None else None, time.time(), call_id),
            )

    def usage(self, owner: str) -> dict:
        with self.connect() as db:
            rows = db.execute(
                "SELECT p.status,p.usage FROM provider_calls p JOIN tasks t ON t.id=p.task_id WHERE t.owner=?",
                (owner,),
            ).fetchall()
        usages = [json.loads(row["usage"]) for row in rows if row["usage"]]
        costs = [u.get("provider_cost_usd") for u in usages]
        return {
            "calls": len(rows),
            "unconfirmed_calls": sum(r["status"] != "completed" for r in rows),
            "reported_input_tokens": sum(u.get("prompt_tokens") or 0 for u in usages),
            "reported_output_tokens": sum(u.get("completion_tokens") or 0 for u in usages),
            "reported_cost_usd": sum(costs)
            if len(costs) == len(rows) and all(c is not None for c in costs)
            else None,
        }

    @staticmethod
    def _memories(db, bot_id):
        from .memory_applicability import source_hash

        records = [
            dict(row)
            for row in db.execute(
                "SELECT m.*,r.target_id AS replaces_memory_id,a.source_hash,a.scope FROM memories m "
                "LEFT JOIN memory_replacements r ON r.proposal_id=m.id "
                "LEFT JOIN memory_applicability a ON a.memory_id=m.id "
                "WHERE m.bot_id=? AND m.status NOT IN ('rejected','superseded') ORDER BY m.created_at,m.id",
                (bot_id,),
            )
        ]
        for record in records:
            saved_hash, scope = record.pop("source_hash"), record.pop("scope")
            record["context_scope"] = scope if saved_hash == source_hash(record) else None
        return records

    def list_memories(self, owner: str, bot_id: str, *, for_person: bool = False) -> list[dict]:
        """The current memories. for_person adds can_undo for the app; the owl never sees it."""
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            records = self._memories(db, bot_id)
            if for_person:
                saved = {
                    row[0]: row[1]
                    for row in db.execute(
                        "SELECT DISTINCT memory_id,action FROM memory_changes WHERE bot_id=? "
                        "AND action IN ('saved','captured')",
                        (bot_id,),
                    )
                }
                for record in records:
                    record["can_undo"] = (
                        record["status"] == "active"
                        and bool(record["source_task_id"])
                        and record["id"] in saved
                    )
                    # Kept after the chat went quiet, from the person's own words (memory_capture.py).
                    record["captured"] = saved.get(record["id"]) == "captured"
            return records

    def cache_memory_applicability(self, owner, bot_id, task_id, sources, scopes):
        from .memory_applicability import source_hash

        with self.connect(write=True) as db:
            bot = self._bot(db, owner, bot_id)
            task = self._task(db, owner, task_id)
            if (
                not bot["memory_enabled"]
                or task["bot_id"] != bot_id
                or not self.executing(db, task)
            ):
                raise Conflict("Memory index task is no longer current.")
            current = {r["id"]: r for r in self._memories(db, bot_id)}
            saved = 0
            for original in sources:
                record = current.get(original["id"])
                if (
                    not record
                    or record["status"] != "active"
                    or source_hash(record) != source_hash(original)
                ):
                    continue  # In-flight edits/deletions never inherit stale classification.
                db.execute(
                    "INSERT INTO memory_applicability VALUES(?,?,?) ON CONFLICT(memory_id) "
                    "DO UPDATE SET source_hash=excluded.source_hash,scope=excluded.scope",
                    (record["id"], source_hash(record), scopes[record["id"]]),
                )
                saved += 1
            self._event(db, task_id, "memory_index_updated", {"records": saved})
            return saved

    @staticmethod
    def _memory_change(
        db,
        bot_id,
        memory_id,
        action,
        previous_status,
        status,
        source_task_id=None,
        related_memory_id=None,
    ):
        # Keep lifecycle evidence without copying titles or preference bodies.
        # No FK to the memory: a deletion receipt must survive deleting its row.
        db.execute(
            "INSERT INTO memory_changes(bot_id,memory_id,action,previous_status,status,"
            "source_task_id,related_memory_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                bot_id,
                memory_id,
                action,
                previous_status,
                status,
                source_task_id,
                related_memory_id,
                time.time(),
            ),
        )

    @staticmethod
    def _memory_changes(db, bot_id, before_seq=None):
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT seq,memory_id,action,previous_status,status,source_task_id,"
                "related_memory_id,created_at,(SELECT id FROM messages "
                "WHERE task_id=memory_changes.source_task_id AND role='user' "
                "ORDER BY created_at,id LIMIT 1) AS source_message_id "
                "FROM memory_changes WHERE bot_id=? "
                + ("AND seq<? " if before_seq is not None else "")
                + "ORDER BY seq DESC LIMIT 51",
                (bot_id, before_seq) if before_seq is not None else (bot_id,),
            )
        ]
        return {
            "events": rows[:50],
            "next_before_seq": rows[49]["seq"] if len(rows) > 50 else None,
            "coverage": "Recorded changes only; pre-upgrade events are unknown. Missing events do not prove a memory was never approved. Events contain no preference text and never reinstate saved memory.",
        }

    def memory_changes(self, owner: str, bot_id: str, before_seq=None):
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            return self._memory_changes(db, bot_id, before_seq)

    def record_memory_delivery(self, owner, task_id, inputs, receipt):
        """Commit source observations and their delivery link in one transaction."""
        from ..memory_sources import EVENT_PREFIX

        with self.connect(write=True) as db:
            self._task(db, owner, task_id)
            for reference, payload in getattr(inputs, "observations", {}).items():
                kind = EVENT_PREFIX + reference
                # Deduplicate within the owning task. Keeping each task's sources
                # avoids dangling links when another conversation is deleted.
                if not db.execute(
                    "SELECT 1 FROM events WHERE task_id=? AND kind=? LIMIT 1",
                    (task_id, kind),
                ).fetchone():
                    self._event(db, task_id, kind, payload)
            self._event(db, task_id, "memory_delivery", receipt)

    def memory_source_read(self, owner, bot_id, *, receipt_id, **options):
        from ..memory_sources import EVENT_PREFIX, read_receipt

        with self.connect() as db:
            bot = self._bot(db, owner, bot_id)
            if not bot["memory_enabled"]:
                raise Conflict("Memory is disabled.")
            rows = db.execute(
                "SELECT e.task_id,e.data FROM events e JOIN tasks t ON t.id=e.task_id "
                "WHERE t.bot_id=? AND e.kind='memory_delivery' ORDER BY e.seq DESC",
                (bot_id,),
            )
            found = next(
                (
                    (row["task_id"], value)
                    for row in rows
                    if (value := json.loads(row["data"])).get("id") == receipt_id
                ),
                None,
            )
            if found is None:
                raise NotFound("Memory receipt is not available in this bot.")
            task_id, receipt = found

            def load(reference):
                row = db.execute(
                    "SELECT data FROM events WHERE task_id=? AND kind=? ORDER BY seq LIMIT 1",
                    (task_id, EVENT_PREFIX + reference),
                ).fetchone()
                return json.loads(row["data"]) if row else None

            return read_receipt(receipt, scope="bot:" + bot_id, load=load, **options)

    def memory_source_context(self, owner, bot_id, **options):
        """Optional context uses the same owner and live enablement checks as tools."""
        try:
            return self.memory_source_read(owner, bot_id, **options)
        except (NotFound, Conflict):
            return {}

    def memory_deliveries(self, owner: str, bot_id: str, *, turn_id=None):
        """Recent input receipts from this bot's existing task event ledger."""
        from ..memory_evidence import MAX_RECEIPTS

        with self.connect() as db:
            self._bot(db, owner, bot_id)
            rows = db.execute(
                "SELECT e.task_id,e.data FROM events e JOIN tasks t ON t.id=e.task_id "
                "WHERE t.bot_id=? AND e.kind='memory_delivery' "
                + ("AND e.task_id=? " if turn_id is not None else "")
                + "ORDER BY e.seq DESC LIMIT ?",
                (bot_id, turn_id, MAX_RECEIPTS) if turn_id is not None else (bot_id, MAX_RECEIPTS),
            ).fetchall()
            return [{**json.loads(r["data"]), "task_id": r["task_id"]} for r in reversed(rows)]

    def task_memory_deliveries(self, owner: str, bot_id: str, task_ids, *, receipt_ids=None):
        """Input receipts of these tasks of the bot, by task, oldest first.

        By task: the recent window of the whole bot moves on, while what a
        task was given stays with the task. With ``receipt_ids``, exactly
        those receipts, however many the task has; otherwise the newest of
        each task.
        """
        from ..memory_evidence import MAX_RECEIPTS

        wanted = list(dict.fromkeys(task_ids))
        ids = None if receipt_ids is None else set(receipt_ids)
        found: dict[str, list[dict]] = {}
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            for start in range(0, len(wanted), 400):
                part = wanted[start : start + 400]
                rows = db.execute(
                    "SELECT e.task_id,e.data FROM events e JOIN tasks t ON t.id=e.task_id "
                    "WHERE t.bot_id=? AND e.kind='memory_delivery' "
                    f"AND e.task_id IN ({','.join('?' * len(part))}) ORDER BY e.seq DESC",
                    (bot_id, *part),
                )
                for row in rows:
                    receipts = found.setdefault(row["task_id"], [])
                    data = row["data"]
                    if ids is None:
                        if len(receipts) < MAX_RECEIPTS:
                            receipts.append({**json.loads(data), "task_id": row["task_id"]})
                    # A stored receipt begins with its id: the others need not be read.
                    elif not data.startswith('{"id":"') or data[7 : data.find('"', 7)] in ids:
                        receipt = json.loads(data)
                        if receipt.get("id") in ids:
                            receipts.append({**receipt, "task_id": row["task_id"]})
        return {task_id: receipts[::-1] for task_id, receipts in found.items()}

    def memory_delivery_page(self, owner: str, bot_id: str, **filters):
        """Page metadata in the existing owner-scoped ledger, without reading memory bodies."""
        from ..memory_evidence import receipt_page

        with self.connect() as db:
            self._bot(db, owner, bot_id)
            rows = db.execute(
                "SELECT e.data FROM events e JOIN tasks t ON t.id=e.task_id "
                "WHERE t.bot_id=? AND e.kind='memory_delivery' ORDER BY e.seq DESC",
                (bot_id,),
            )
            return receipt_page(
                (json.loads(r["data"]) for r in rows), scope="bot:" + bot_id, **filters
            )

    def memory_changes_since_reply(self, owner: str, task_id: str):
        """Locate UI changes in the visible conversation's actual chronology."""
        with self.connect() as db:
            task = self._task(db, owner, task_id)
            # The owl's own previous reply: in a room, other owls reply too.
            previous = db.execute(
                "SELECT id,task_id,created_at FROM messages WHERE chat_id=? "
                "AND role='assistant' AND task_id!=? AND created_at<=? "
                "AND task_id IN (SELECT id FROM tasks WHERE bot_id=?) "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (task["chat_id"], task_id, task["created_at"], task["bot_id"]),
            ).fetchone()
            changes = self._memory_changes(db, task["bot_id"])
            return {
                "previous_reply": dict(previous) if previous else None,
                "later_changes": [
                    e
                    for e in changes["events"]
                    if previous and e["created_at"] > previous["created_at"]
                ],
                "next_before_seq": changes["next_before_seq"],
            }

    def note_changes_since_reply(self, owner: str, task_id: str):
        """Bounded revision metadata, locating note edits after the last reply.

        Full bodies/history stay in the note tools. Absence is not evidence that
        a note never existed, and a later edit does not retroactively falsify a reply.
        """
        with self.connect() as db:
            task = self._task(db, owner, task_id)
            # The owl's own previous reply: in a room, other owls reply too.
            previous = db.execute(
                "SELECT id,task_id,created_at FROM messages WHERE chat_id=? "
                "AND role='assistant' AND task_id!=? AND created_at<=? "
                "AND task_id IN (SELECT id FROM tasks WHERE bot_id=?) "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (task["chat_id"], task_id, task["created_at"], task["bot_id"]),
            ).fetchone()
            changes = (
                db.execute(
                    "SELECT r.note_id,r.revision,r.created_at,r.source_task_id "
                    "FROM note_revisions r JOIN working_notes n ON n.id=r.note_id "
                    "WHERE n.bot_id=? AND r.created_at>? "
                    "ORDER BY r.created_at DESC,r.note_id,r.revision DESC LIMIT 21",
                    (task["bot_id"], previous["created_at"]),
                ).fetchall()
                if previous
                else []
            )
            return {
                "previous_reply": dict(previous) if previous else None,
                "later_revisions": [dict(row) for row in changes[:20]],
                "selection_partial": len(changes) > 20,
            }

    def save_memory(
        self,
        owner: str,
        bot_id: str,
        data: MemoryInput,
        *,
        memory_id: str | None = None,
        source_task_id: str | None = None,
        replaces_memory_id: str | None = None,
        saved_by_owl: bool = False,
    ) -> dict:
        """Save a memory: the person's own, an owl's suggestion, or what an owl saved by itself.

        With saved_by_owl the memory is kept at once, and a correction replaces the old
        memory at once; the old one is superseded, not erased, so undo_memory can bring it
        back. Without it, an owl's memory is a pending suggestion, as before.
        """
        if saved_by_owl and not source_task_id:
            raise ValueError("An owl saves memory from one of its tasks.")
        with self.connect(write=True) as db:
            bot = self._bot(db, owner, bot_id)
            if source_task_id:
                task = self._task(db, owner, source_task_id)
                if (
                    task["bot_id"] != bot_id
                    or not self.executing(db, task)
                    or not bot["memory_enabled"]
                ):
                    raise Conflict("This task cannot propose memory for this bot.")
            now = time.time()
            target = None
            if replaces_memory_id:
                target = db.execute(
                    "SELECT * FROM memories WHERE id=? AND bot_id=? AND status='active'",
                    (replaces_memory_id, bot_id),
                ).fetchone()
                if target is None or not source_task_id or memory_id:
                    raise Conflict(
                        "A correction must propose a replacement for a current approved memory."
                    )
            if memory_id:
                row = db.execute(
                    "SELECT * FROM memories WHERE id=? AND bot_id=? "
                    "AND status NOT IN ('rejected','superseded')",
                    (memory_id, bot_id),
                ).fetchone()
                if row is None:
                    raise NotFound("Memory not found.")
                self._activate_memory(db, bot_id, memory_id)
                db.execute(
                    "UPDATE memories SET title=?,content=?,status='active',source_task_id=NULL,updated_at=? WHERE id=?",
                    (data.title, data.content, now, memory_id),
                )
                # The person's edit makes it theirs: there is nothing left to undo.
                self._retire_superseded(db, bot_id, memory_id)
                self._memory_change(
                    db, bot_id, memory_id, "edited", row["status"], "active", row["source_task_id"]
                )
            elif saved_by_owl:
                return self._keep_owl_memory(
                    db, bot_id, data.title, data.content, source_task_id, target, now
                )
            else:
                memory_id = uid()
                status = "pending" if source_task_id else "active"
                db.execute(
                    "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)",
                    (memory_id, bot_id, data.title, data.content, status, source_task_id, now, now),
                )
                if target is not None:
                    db.execute(
                        "INSERT INTO memory_replacements VALUES(?,?,?)",
                        (memory_id, target["id"], target["updated_at"]),
                    )
                self._memory_change(
                    db,
                    bot_id,
                    memory_id,
                    "proposed" if source_task_id else "created",
                    None,
                    status,
                    source_task_id,
                    replaces_memory_id,
                )
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    @classmethod
    def _keep_owl_memory(
        cls, db, bot_id, title, content, source_task_id, target, now, *, action="saved"
    ) -> dict:
        """What an owl keeps by itself, from one of its turns: kept at once, and a correction
        replaces ``target`` at once. The old memory is superseded, not erased, so undo_memory can
        bring it back. ``action`` is the lifecycle receipt: ``saved`` during a turn, ``captured``
        after the chat went quiet (memory_capture.py)."""
        memory_id = uid()
        db.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)",
            (memory_id, bot_id, title, content, "active", source_task_id, now, now),
        )
        if target is not None:
            db.execute(
                "INSERT INTO memory_replacements VALUES(?,?,?)",
                (memory_id, target["id"], target["updated_at"]),
            )
        cls._memory_change(
            db,
            bot_id,
            memory_id,
            action,
            None,
            "active",
            source_task_id,
            target["id"] if target is not None else None,
        )
        if target is not None:
            # The old memory stops being used now and stays only so Undo can restore it.
            db.execute(
                "UPDATE memories SET status='superseded',updated_at=? WHERE id=?",
                (now, target["id"]),
            )
            cls._memory_change(
                db,
                bot_id,
                target["id"],
                "superseded",
                "active",
                "superseded",
                target["source_task_id"],
                memory_id,
            )
        return dict(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    @staticmethod
    def _activate_memory(db: sqlite3.Connection, bot_id: str, memory_id: str) -> None:
        replacement = db.execute(
            "SELECT * FROM memory_replacements WHERE proposal_id=?", (memory_id,)
        ).fetchone()
        if replacement is None:
            return
        proposal = db.execute(
            "SELECT status FROM memories WHERE id=? AND bot_id=?", (memory_id, bot_id)
        ).fetchone()
        if proposal["status"] == "active":
            return
        target = db.execute(
            "SELECT * FROM memories WHERE id=? AND bot_id=?", (replacement["target_id"], bot_id)
        ).fetchone()
        if (
            target is None
            or target["status"] != "active"
            or target["updated_at"] != replacement["expected_updated_at"]
        ):
            raise Conflict(
                "The original memory changed or was removed. Review the current memory and propose a fresh correction."
            )
        # A retired memory is never read again, so its words are erased; receipts stay.
        db.execute(
            "UPDATE memories SET status='rejected',title='',content='',updated_at=? WHERE id=?",
            (time.time(), target["id"]),
        )
        Store._memory_change(
            db,
            bot_id,
            target["id"],
            "replaced",
            "active",
            "rejected",
            target["source_task_id"],
            memory_id,
        )

    @staticmethod
    def _retire_superseded(db: sqlite3.Connection, bot_id: str, replacement_id: str) -> None:
        """When a replacement goes, Undo no longer applies: erase what it superseded, and what
        that one superseded in turn, which only its Undo could have brought back."""
        while True:
            row = db.execute(
                "SELECT m.* FROM memory_replacements r JOIN memories m ON m.id=r.target_id "
                "WHERE r.proposal_id=? AND m.bot_id=? AND m.status='superseded'",
                (replacement_id, bot_id),
            ).fetchone()
            if row is None:
                return
            db.execute(
                "UPDATE memories SET status='rejected',title='',content='',updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            Store._memory_change(
                db,
                bot_id,
                row["id"],
                "retired",
                "superseded",
                "rejected",
                row["source_task_id"],
                replacement_id,
            )
            replacement_id = row["id"]

    def undo_memory(self, owner: str, bot_id: str, memory_id: str) -> dict:
        """Undo a memory an owl saved by itself: forget it, and restore what it replaced.

        Only an owl's own save that the person has not edited can be undone; the person's
        own memories and their edits are changed or deleted directly.
        """
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                "SELECT * FROM memories WHERE id=? AND bot_id=?", (memory_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("Memory not found.")
            saved = db.execute(
                "SELECT 1 FROM memory_changes WHERE bot_id=? AND memory_id=? "
                "AND action IN ('saved','captured')",
                (bot_id, memory_id),
            ).fetchone()
            if row["status"] != "active" or not row["source_task_id"] or saved is None:
                raise Conflict("Only a memory an owl saved by itself can be undone.")
            now = time.time()
            restored = db.execute(
                "SELECT m.* FROM memory_replacements r JOIN memories m ON m.id=r.target_id "
                "WHERE r.proposal_id=? AND m.bot_id=? AND m.status='superseded'",
                (memory_id, bot_id),
            ).fetchone()
            if restored is not None:
                db.execute(
                    "UPDATE memories SET status='active',updated_at=? WHERE id=?",
                    (now, restored["id"]),
                )
                self._memory_change(
                    db,
                    bot_id,
                    restored["id"],
                    "restored",
                    "superseded",
                    "active",
                    restored["source_task_id"],
                    memory_id,
                )
            # Forgotten words are erased, as when the person deletes a memory; receipts stay.
            db.execute(
                "UPDATE memories SET status='rejected',title='',content='',updated_at=? WHERE id=?",
                (now, memory_id),
            )
            self._memory_change(
                db,
                bot_id,
                memory_id,
                "undone",
                "active",
                "rejected",
                row["source_task_id"],
                restored["id"] if restored is not None else None,
            )
            return {
                "id": memory_id,
                "restored_memory_id": restored["id"] if restored is not None else None,
            }

    def decide_memory(self, owner: str, bot_id: str, memory_id: str, status: str) -> dict:
        if status not in ("active", "rejected"):
            raise ValueError("Invalid memory status.")
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                "SELECT * FROM memories WHERE id=? AND bot_id=?", (memory_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("Memory not found.")
            if row["status"] == "rejected":
                raise Conflict("This memory was retired. Review the current memories instead.")
            if row["status"] == "superseded":
                raise Conflict("This memory was replaced. Undo the newer memory to bring it back.")
            if status == "active":
                self._activate_memory(db, bot_id, memory_id)
            db.execute(
                "UPDATE memories SET status=?,updated_at=? WHERE id=?",
                (status, time.time(), memory_id),
            )
            if status == "rejected":
                db.execute("UPDATE memories SET title='',content='' WHERE id=?", (memory_id,))
                self._retire_superseded(db, bot_id, memory_id)
            if row["status"] != status:
                self._memory_change(
                    db,
                    bot_id,
                    memory_id,
                    "approved" if status == "active" else "rejected",
                    row["status"],
                    status,
                    row["source_task_id"],
                )
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    def list_files(self, owner: str, bot_id: str, *, limit=None, latest_only=False) -> list[dict]:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            return [
                {
                    key: value
                    for key, value in self._file_metadata(db, row).items()
                    if key != "content"
                }
                for row in db.execute(
                    "SELECT * FROM files f WHERE bot_id=?"
                    + (
                        " AND NOT EXISTS (SELECT 1 FROM files n WHERE n.bot_id=f.bot_id AND n.series_id=f.series_id AND n.revision>f.revision)"
                        if latest_only
                        else ""
                    )
                    + " ORDER BY updated_at DESC,id"
                    + (" LIMIT ?" if limit is not None else ""),
                    (bot_id, limit) if limit is not None else (bot_id,),
                )
            ]

    def file_name_exists(self, owner, bot_id, name):
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            return (
                db.execute(
                    "SELECT 1 FROM files WHERE bot_id=? AND name=?", (bot_id, name)
                ).fetchone()
                is not None
            )

    def visible_file_name(self, owner: str, bot_id: str, file_id: str) -> str | None:
        """Read a Library label for the activity feed without reading or hashing the file."""
        with self.connect() as db:
            row = db.execute(
                "SELECT f.name FROM files f JOIN bots b ON b.id=f.bot_id "
                "WHERE f.id=? AND f.bot_id=? AND b.owner=? AND b.files_enabled=1",
                (file_id, bot_id, owner),
            ).fetchone()
            return row["name"] if row else None

    def files_page(self, owner, bot_id, data: FileQuery):
        """Current immutable file metadata, with deletion-safe keyset paging.

        Search is a literal Unicode filename substring, not content/semantic search.
        New files appear on a fresh first page; deleting the cursor file does not
        invalidate browsing older files. Only the selected page loads metadata.
        """
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            if data.source_task_id:
                task = self._task(db, owner, data.source_task_id)
                if task["bot_id"] != bot_id:
                    raise NotFound("Task not found for this bot.")
            where, values = "bot_id=?", [bot_id]
            if data.series_id:
                where += " AND series_id=?"
                values.append(data.series_id)
            elif not data.include_versions and not data.source_task_id:
                where += " AND NOT EXISTS (SELECT 1 FROM files n WHERE n.bot_id=files.bot_id AND n.series_id=files.series_id AND n.revision>files.revision)"
            if data.source_task_id:
                where += " AND source_task_id=? AND NOT EXISTS (SELECT 1 FROM owl_withdrawn_results w WHERE w.file_id=files.id)"
                values.append(data.source_task_id)
            total = db.execute("SELECT count(*) FROM files WHERE " + where, values).fetchone()[0]
            if data.query:
                where += " AND instr(unicode_fold(name),unicode_fold(?))>0"
                values.append(data.query)
            matched = db.execute("SELECT count(*) FROM files WHERE " + where, values).fetchone()[0]
            if data.before:
                where += " AND (created_at<? OR (created_at=? AND id<?))"
                values.extend([data.before.created_at, data.before.created_at, data.before.id])
            rows = db.execute(
                "SELECT * FROM files WHERE " + where + " ORDER BY created_at DESC,id DESC LIMIT ?",
                [*values, data.limit + 1],
            ).fetchall()
            items = [
                {k: v for k, v in self._file_metadata(db, row).items() if k != "content"}
                for row in rows[: data.limit]
            ]
            return {
                "items": items,
                "total_count": total,
                "matched_count": matched,
                "next_before": {k: items[-1][k] for k in ("created_at", "id")}
                if len(rows) > data.limit
                else None,
                "retrieval": "filename_unicode_substring" if data.query else "browse_recent",
            }

    def file(self, owner: str, bot_id: str, file_id: str) -> dict:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                "SELECT * FROM files WHERE id=? AND bot_id=?", (file_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("File not found.")
            return self._file_metadata(db, row)

    @staticmethod
    def _file_metadata(db, row):
        item = dict(row)
        if item["name"].endswith(".owlquery.json"):
            binding = db.execute(
                "SELECT sources FROM owl_query_files WHERE file_id=?", (item["id"],)
            ).fetchone()
            if binding:
                item["query_result"] = True
                descriptor = json.loads(binding[0])
                if isinstance(descriptor, dict):
                    item["query_title"] = descriptor.get("title")
        versions = db.execute(
            "SELECT max(revision),count(*),EXISTS(SELECT 1 FROM owl_withdrawn_results WHERE file_id=?) FROM files WHERE bot_id=? AND series_id=?",
            (item["id"], item["bot_id"], item["series_id"]),
        ).fetchone()
        item.update(is_latest=item["revision"] == versions[0], version_count=versions[1])
        if versions[2]:
            item["withdrawn_from_reply"] = True
        external = db.execute(
            "SELECT * FROM file_objects WHERE file_id=?", (item["id"],)
        ).fetchone()
        if external:
            ref = ObjectRef.decode(external["ref"])
            binary = bool(external["binary"])
            item.update(
                binary=binary,
                content=None if binary else item["content"],
                size=ref.size if binary else len(item["content"]),
                size_bytes=ref.size,
                media_type=external["media_type"],
                sha256=ref.sha256,
            )
            return item
        payload = db.execute(
            "SELECT length(data) AS size_bytes,media_type,sha256 FROM file_payloads WHERE file_id=?",
            (item["id"],),
        ).fetchone()
        if payload:
            item.update(dict(payload), binary=True, content=None, size=payload["size_bytes"])
        else:
            data = item["content"].encode("utf-8")
            item.update(
                binary=False,
                size=len(item["content"]),
                size_bytes=len(data),
                media_type="text/plain",
                sha256=hashlib.sha256(data).hexdigest(),
            )
        return item

    def _interactive_file(self, db, owner: str, bot_id: str, file_id: str) -> dict:
        bot = self._bot(db, owner, bot_id)
        row = db.execute(
            "SELECT * FROM files WHERE id=? AND bot_id=?", (file_id, bot_id)
        ).fetchone()
        if row is None:
            raise NotFound("Interactive file not found.")
        if not bot["files_enabled"]:
            raise Conflict("Library files are disabled for this owl.")
        file = self._file_metadata(db, row)
        # The chat's latest delivered revision remains a valid editing target if a newer
        # working revision was withheld. The ordinary Library still reports the physical head.
        if not file["is_latest"]:
            file["is_latest"] = self._delivered_head(db, bot_id, file["series_id"]) == file_id
        if Path(file["name"]).suffix.lower() not in {".html", ".htm"} or (
            file["binary"] and file["media_type"].split(";", 1)[0].lower() != "text/html"
        ):
            raise Conflict("Choose an HTML file for an interactive result.")
        if file["size_bytes"] > INTERACTIVE_HTML_BYTES:
            raise LimitReached("Interactive HTML files are limited to 8 MiB.")
        source_chat_id = None
        if file["source_task_id"]:
            source = db.execute(
                "SELECT chat_id FROM tasks WHERE id=? AND bot_id=? AND owner=?",
                (file["source_task_id"], bot_id, owner),
            ).fetchone()
            if not source:
                raise NotFound("The source chat of this interactive result is unavailable.")
            source_chat_id = source["chat_id"]
        state = db.execute(
            "SELECT version,data FROM interactive_result_states WHERE series_id=?",
            (file["series_id"],),
        ).fetchone()
        return {
            **{key: file[key] for key in ("name", "series_id", "revision", "sha256", "is_latest")},
            "file_id": file_id,
            "source_chat_id": source_chat_id,
            "version": state["version"] if state else 0,
            "data": json.loads(state["data"]) if state else None,
        }

    @staticmethod
    def _check_interactive_version(file, revision, sha256, version):
        if not file["is_latest"] or file["revision"] != revision or file["sha256"] != sha256:
            raise Conflict("This HTML revision has changed. Open the latest file and select again.")
        if file["version"] != version:
            raise Conflict("The interactive result state changed. Reload it before continuing.")

    def interactive_state(
        self, owner: str, bot_id: str, file_id: str, update: InteractiveStateWrite | None = None
    ) -> dict:
        with self.connect(write=update is not None) as db:
            file = self._interactive_file(db, owner, bot_id, file_id)
            if update is not None:
                self._check_interactive_version(
                    file, update.revision, update.sha256, update.expected_version
                )
                if file["version"] == 2**53 - 1:
                    raise LimitReached(
                        "The interactive result has reached its state version limit."
                    )
                version = file["version"] + 1
                db.execute(
                    "INSERT INTO interactive_result_states(series_id,version,data) VALUES(?,?,?) "
                    "ON CONFLICT(series_id) DO UPDATE SET version=excluded.version,data=excluded.data",
                    (file["series_id"], version, encode(update.data)),
                )
                file.update(version=version, data=update.data)
            return {**file, "max_selections": 8}

    def _interactive_task_base(self, db, owner, task, target):
        file = self._interactive_file(db, owner, task["bot_id"], target["file_id"])
        if (
            file["series_id"] != target["series_id"]
            or file["source_chat_id"] not in (None, task["chat_id"])
            or target["file_id"] not in self._attached_file_ids(db, task["id"])
        ):
            raise Conflict("This selection does not belong to the attached result in this chat.")
        self._check_interactive_version(
            file, target["revision"], target["sha256"], target["state_version"]
        )
        return file

    def interactive_revision_context(self, owner: str, task_id: str) -> dict:
        """Only an exact, still-current selection can supply its state to a revision turn."""
        with self.connect() as db:
            task = self._task(db, owner, task_id)
            target = task["config"].get("interactive_target")
            if not target:
                raise NotFound("This task has no interactive selection.")
            file = self._interactive_task_base(db, owner, task, target)
            return {**target, "state": file["data"]}

    def file_data(self, owner: str, bot_id: str, file_id: str) -> bytes:
        if self.file(owner, bot_id, file_id)["size_bytes"] > INLINE_FILE_BYTES:
            raise LimitReached("Use chunked transfers for files larger than 2 MiB.")
        return b"".join(self.file_chunks(owner, bot_id, file_id))

    def file_chunks(self, owner, bot_id, file_id, *, offset=0):
        """Owned snapshot read with at most one binary chunk in Python memory."""
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid file offset.")
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                "SELECT f.content,p.rowid AS payload FROM files f LEFT JOIN file_payloads p ON p.file_id=f.id WHERE f.id=? AND f.bot_id=?",
                (file_id, bot_id),
            ).fetchone()
            if row is None:
                raise NotFound("File not found.")
            external = db.execute(
                "SELECT ref FROM file_objects WHERE file_id=?", (file_id,)
            ).fetchone()
            if external:
                if self.objects is None:
                    raise ObjectStorageError("Object storage is not configured.")
                yield from self.objects.read(ObjectRef.decode(external["ref"]), offset=offset)
            elif row["payload"] is None:
                data = row["content"].encode("utf-8")
                if offset > len(data):
                    raise ValueError("File offset exceeds its length.")
                for start in range(offset, len(data), FILE_CHUNK_BYTES):
                    yield data[start : start + FILE_CHUNK_BYTES]
            else:
                with db.blobopen("file_payloads", "data", row["payload"], readonly=True) as blob:
                    if offset > len(blob):
                        raise ValueError("File offset exceeds its length.")
                    blob.seek(offset)
                    while data := blob.read(FILE_CHUNK_BYTES):
                        yield data

    def save_stream(
        self, owner, bot_id, name, chunks, size, *, source_task_id=None, previous_file_id=None
    ):
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise LibraryCapacityExceeded(FILE_TOO_LARGE)
        return self._save_file(
            owner,
            bot_id,
            FileInput(name=name, content="", previous_file_id=previous_file_id),
            source_task_id=source_task_id,
            chunks=chunks,
            binary_size=size,
        )

    def save_bytes(
        self,
        owner: str,
        bot_id: str,
        name: str,
        payload: bytes,
        *,
        source_task_id: str | None = None,
        previous_file_id: str | None = None,
    ) -> dict:
        if not isinstance(payload, bytes) or len(payload) > MAX_FILE_BYTES:
            raise LibraryCapacityExceeded(FILE_TOO_LARGE)
        return self._save_file(
            owner,
            bot_id,
            FileInput(name=name, content="", previous_file_id=previous_file_id),
            source_task_id=source_task_id,
            payload=payload,
        )

    def save_file(
        self,
        owner: str,
        bot_id: str,
        data: FileInput,
        *,
        source_task_id: str | None = None,
        payload: bytes | None = None,
    ) -> dict:
        return self._save_file(owner, bot_id, data, source_task_id=source_task_id, payload=payload)

    @staticmethod
    def _library_usage(db, owner, *, exclude_upload=None, exclude_reservation=None):
        saved = db.execute(
            "SELECT count(*),COALESCE(sum(COALESCE(o.size_bytes,length(p.data),byte_length(f.content))),0) "
            "FROM files f JOIN bots b ON b.id=f.bot_id LEFT JOIN file_payloads p ON f.id=p.file_id "
            "LEFT JOIN file_objects o ON o.file_id=f.id WHERE b.owner=?",
            (owner,),
        ).fetchone()
        pending = db.execute(
            "SELECT COALESCE(sum(u.size),0) FROM file_uploads u JOIN bots b ON b.id=u.bot_id "
            "WHERE b.owner=? AND u.status='pending' AND u.expires>? AND u.id IS DISTINCT FROM ?",
            (owner, time.time(), exclude_upload),
        ).fetchone()[0]
        staged = db.execute(
            "SELECT COALESCE(sum(size_bytes),0) FROM library_reservations "
            "WHERE owner=? AND expires>? AND id IS DISTINCT FROM ?",
            (owner, time.time(), exclude_reservation),
        ).fetchone()[0]
        row = db.execute(
            "SELECT byte_limit FROM library_allowances WHERE owner=?", (owner,)
        ).fetchone()
        limit = int(row[0]) if row and row[0] is not None else None
        pending, staged = int(pending), int(staged)
        return {
            "scope": "account_library",
            "file_count": int(saved[0]),
            "used_bytes": int(saved[1]),
            "reserved_bytes": pending + staged,
            "limit_bytes": limit,
            "available_bytes": max(0, limit - int(saved[1]) - pending - staged)
            if limit is not None
            else None,
        }

    def library_usage(self, owner):
        with self.connect() as db:
            return self._library_usage(db, owner)

    def set_library_limit(self, owner, byte_limit):
        """Operator/billing configuration, not a user API or a selected price plan.

        Lowering below existing usage preserves every file; only new writes wait
        for capacity. None means no configured byte allowance, not infinite disk.
        """
        if byte_limit is not None and (
            type(byte_limit) is not int or not 0 <= byte_limit <= 2**63 - 1
        ):
            raise ValueError("Library allowance must be nonnegative bytes or None.")
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO library_allowances VALUES(?,?,?) ON CONFLICT(owner) "
                "DO UPDATE SET byte_limit=excluded.byte_limit,updated_at=excluded.updated_at",
                (owner, byte_limit, time.time()),
            )
            return self._library_usage(db, owner)

    def _check_library_capacity(
        self, db, owner, size, *, exclude_upload=None, exclude_reservation=None
    ):
        usage = self._library_usage(
            db, owner, exclude_upload=exclude_upload, exclude_reservation=exclude_reservation
        )
        if (
            usage["limit_bytes"] is not None
            and usage["used_bytes"] + usage["reserved_bytes"] + size > usage["limit_bytes"]
        ):
            remedy = (
                "The file alone exceeds the total allowance; deleting other Library files "
                "cannot make it fit. Increase the allowance or choose an acceptable smaller output."
                if size > usage["limit_bytes"]
                else "Free Library space or increase the allowance before retrying the same file."
            )
            raise LibraryCapacityExceeded(
                "This file exceeds the account's shared Library allowance: "
                f"requires {size} bytes, but {usage['available_bytes']} bytes are available "
                f"({usage['used_bytes']} saved and {usage['reserved_bytes']} reserved "
                f"of {usage['limit_bytes']} bytes). No Library file was added. " + remedy
            )

    def _release_library_reservation(self, owner, key):
        with self.connect(write=True) as db:
            db.execute("DELETE FROM library_reservations WHERE owner=? AND id=?", (owner, key))

    def _check_file_write(
        self,
        db,
        owner,
        bot_id,
        data,
        source_task_id,
        binary_size,
        exclude_upload,
        exclude_reservation=None,
    ):
        bot = self._bot(db, owner, bot_id)
        if source_task_id:
            task = self._task(db, owner, source_task_id)
            if task["bot_id"] != bot_id or not self.executing(db, task) or not bot["files_enabled"]:
                raise Conflict("This task cannot write files for this bot.")
            if target := task["config"].get("interactive_target"):
                self._interactive_task_base(db, owner, task, target)
                if data.name != target["name"] or data.previous_file_id != target["file_id"]:
                    raise Conflict("Revise only the exact HTML file selected in this turn.")
                size = binary_size if binary_size is not None else len(data.content.encode("utf-8"))
                if size > INTERACTIVE_HTML_BYTES:
                    raise LimitReached("Interactive HTML files are limited to 8 MiB.")
        self._revision_base(
            db, bot_id, data.name, data.previous_file_id, delivering=bool(source_task_id)
        )
        if db.execute(
            "SELECT 1 FROM file_uploads WHERE bot_id=? AND name=? AND status='pending' AND expires>? AND id IS DISTINCT FROM ?",
            (bot_id, data.name, time.time(), exclude_upload),
        ).fetchone():
            raise Conflict("An upload already reserves this file name.")
        self._check_library_capacity(
            db,
            owner,
            binary_size if binary_size is not None else len(data.content.encode("utf-8")),
            exclude_upload=exclude_upload,
            exclude_reservation=exclude_reservation,
        )

    def allocate_upload(self, db, values):
        db.execute(
            "INSERT INTO file_uploads VALUES(?,?,?,?,?,'pending',?,?,NULL,zeroblob(?),?)",
            values,
        )

    def upload_blob(self, db, row, *, readonly=False):
        return db.blobopen("file_uploads", "data", row["payload_row"], readonly=readonly)

    def release_upload(self, db, upload_id):
        pass  # SQLite staging is cleared by setting data=NULL in the same transaction.

    def put_object(self, owner, bot_id, chunks, size, *, cancel=lambda: False):
        from .retention import register_upload

        return self.objects.put(
            owner,
            bot_id,
            chunks,
            size,
            cancel=cancel,
            before_upload=lambda ref: register_upload(self, owner, bot_id, ref),
        )

    @staticmethod
    def publish_object(db, owner, bot_id, ref):
        from .retention import publish_object

        publish_object(db, owner, bot_id, ref)

    def stage_file_object(
        self, owner, bot_id, data, chunks, binary_size, *, source_task_id=None, exclude_upload=None
    ):
        # Reserve logical Library bytes transactionally before network I/O;
        # chunked transfers already hold a durable reservation in file_uploads.
        from .retention import register_upload

        with self.connect() as check:
            self._check_file_write(
                check, owner, bot_id, data, source_task_id, binary_size, exclude_upload
            )
        reserved = None

        def before_upload(ref):
            nonlocal reserved
            with self.connect(write=True) as db:
                self._check_file_write(
                    db, owner, bot_id, data, source_task_id, binary_size, exclude_upload
                )
                if exclude_upload is None:
                    db.execute(
                        "DELETE FROM library_reservations WHERE owner=? AND expires<?",
                        (owner, time.time()),
                    )
                    db.execute(
                        "INSERT INTO library_reservations VALUES(?,?,?,?,?)",
                        (ref.key, owner, bot_id, ref.size, time.time() + 3600),
                    )
                    reserved = ref.key
            register_upload(self, owner, bot_id, ref)

        if binary_size is None:
            payload = data.content.encode("utf-8")
            size = len(payload)
            chunks = (payload[i : i + FILE_CHUNK_BYTES] for i in range(0, size, FILE_CHUNK_BYTES))
        else:
            size = binary_size
        try:
            return self.objects.put(owner, bot_id, chunks, size, before_upload=before_upload)
        except BaseException:
            if reserved:
                self._release_library_reservation(owner, reserved)
            raise

    def externalize_file(self, owner, bot_id, file_id):
        """Explicit, restartable migration; publish only after verified readback.

        Existing API IDs, source links and text previews are preserved. This is
        an operator action; no implicit bulk migration during service startup.
        """
        if self.objects is None:
            raise ObjectStorageError("Object storage is not configured.")
        original = self.file(owner, bot_id, file_id)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM file_objects WHERE file_id=?", (file_id,)).fetchone():
                return original
        ref = self.put_object(
            owner, bot_id, self.file_chunks(owner, bot_id, file_id), original["size_bytes"]
        )
        digest = hashlib.sha256()
        for chunk in self.objects.read(ref):
            digest.update(chunk)
        if digest.hexdigest() != original["sha256"]:
            raise ObjectStorageError("Migrated file did not match the original.")
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            row = db.execute(
                "SELECT * FROM files WHERE id=? AND bot_id=?", (file_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("File no longer exists.")
            if db.execute("SELECT 1 FROM file_objects WHERE file_id=?", (file_id,)).fetchone():
                return self._file_metadata(db, row)
            self.publish_object(db, owner, bot_id, ref)
            db.execute(
                "INSERT INTO file_objects VALUES(?,?,?,?,?)",
                (
                    file_id,
                    ref.encode(),
                    ref.size,
                    int(original["binary"]),
                    original["media_type"],
                ),
            )
            db.execute("DELETE FROM file_payloads WHERE file_id=?", (file_id,))
            return self._file_metadata(db, row)

    def _save_file(
        self,
        owner,
        bot_id,
        data,
        *,
        source_task_id=None,
        payload=None,
        chunks=None,
        binary_size=None,
        db=None,
        exclude_upload=None,
        object_ref=None,
        query_sources=None,
    ):
        if payload is not None:
            chunks = (
                payload[start : start + FILE_CHUNK_BYTES]
                for start in range(0, len(payload), FILE_CHUNK_BYTES)
            )
            binary_size = len(payload)
        if self.objects is not None and object_ref is None:
            if db is not None:
                raise RuntimeError("Stage object bytes before opening a write transaction.")
            object_ref = self.stage_file_object(
                owner,
                bot_id,
                data,
                chunks,
                binary_size,
                source_task_id=source_task_id,
                exclude_upload=exclude_upload,
            )
        try:
            return self._publish_file(
                owner,
                bot_id,
                data,
                source_task_id=source_task_id,
                chunks=chunks,
                binary_size=binary_size,
                db=db,
                exclude_upload=exclude_upload,
                object_ref=object_ref,
                query_sources=query_sources,
            )
        except BaseException:
            if object_ref is not None and exclude_upload is None:
                self._release_library_reservation(owner, object_ref.key)
            raise

    def _publish_file(
        self,
        owner,
        bot_id,
        data,
        *,
        source_task_id,
        chunks,
        binary_size,
        db,
        exclude_upload,
        object_ref,
        query_sources=None,
    ):
        with nullcontext(db) if db is not None else self.connect(write=True) as db:
            self._check_file_write(
                db,
                owner,
                bot_id,
                data,
                source_task_id,
                object_ref.size if object_ref else binary_size,
                exclude_upload,
                exclude_reservation=object_ref.key if object_ref else None,
            )
            if object_ref is not None and exclude_upload is None:
                reservation = db.execute(
                    "SELECT 1 FROM library_reservations WHERE id=? AND owner=? AND bot_id=? AND expires>?",
                    (object_ref.key, owner, bot_id, time.time()),
                ).fetchone()
                if reservation is None:
                    # Reusing an already published immutable object needs no
                    # network reservation, but still consumes logical Library
                    # bytes above. Unpublished/expired stages cannot use this.
                    published = db.execute(
                        "SELECT 1 FROM stored_objects WHERE key=? AND owner=? AND bot_id=? "
                        "AND ref=? AND state='live' AND (EXISTS(SELECT 1 FROM file_objects WHERE ref=?) "
                        "OR EXISTS(SELECT 1 FROM workspace_objects WHERE ref=?))",
                        (
                            object_ref.key,
                            owner,
                            bot_id,
                            object_ref.encode(),
                            object_ref.encode(),
                            object_ref.encode(),
                        ),
                    ).fetchone()
                    if published is None:
                        raise Conflict("Library reservation expired; retry this file operation.")
                db.execute(
                    "DELETE FROM library_reservations WHERE id=? AND owner=?",
                    (object_ref.key, owner),
                )
            previous = self._revision_base(
                db, bot_id, data.name, data.previous_file_id, delivering=bool(source_task_id)
            )
            if (
                previous
                and query_sources is None
                and db.execute(
                    "SELECT 1 FROM owl_query_files WHERE file_id=?", (previous["id"],)
                ).fetchone()
            ):
                raise Conflict(
                    "Revise this data result with present_table to keep its source binding."
                )
            file_id, now = uid(), time.time()
            db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    file_id,
                    bot_id,
                    data.name,
                    data.content,
                    source_task_id,
                    now,
                    now,
                    previous["series_id"] if previous else file_id,
                    previous["revision"] + 1 if previous else 1,
                ),
            )
            if object_ref is not None:
                self.publish_object(db, owner, bot_id, object_ref)
                db.execute(
                    "INSERT INTO file_objects VALUES(?,?,?,?,?)",
                    (
                        file_id,
                        object_ref.encode(),
                        object_ref.size,
                        int(binary_size is not None),
                        (mimetypes.guess_type(data.name)[0] or "application/octet-stream")
                        if binary_size is not None
                        else "text/plain",
                    ),
                )
            elif binary_size is not None:
                db.execute(
                    "INSERT INTO file_payloads VALUES(?,zeroblob(?),?,?)",
                    (
                        file_id,
                        binary_size,
                        mimetypes.guess_type(data.name)[0] or "application/octet-stream",
                        "",
                    ),
                )
                rowid = db.execute(
                    "SELECT rowid FROM file_payloads WHERE file_id=?", (file_id,)
                ).fetchone()[0]
                received, digest = 0, hashlib.sha256()
                with db.blobopen("file_payloads", "data", rowid) as blob:
                    for chunk in chunks:
                        if (
                            not isinstance(chunk, bytes)
                            or len(chunk) > FILE_CHUNK_BYTES
                            or received + len(chunk) > binary_size
                        ):
                            raise ValueError("Invalid file stream; nothing was saved.")
                        blob.write(chunk)
                        received += len(chunk)
                        digest.update(chunk)
                if received != binary_size:
                    raise ValueError("File stream ended early; nothing was saved.")
                db.execute(
                    "UPDATE file_payloads SET sha256=? WHERE file_id=?",
                    (digest.hexdigest(), file_id),
                )
            if source_task_id is None:
                from .triggers import file_added

                file_added(db, bot_id, file_id, data.name, now)
            if query_sources is not None:
                db.execute(
                    "INSERT INTO owl_query_files VALUES(?,?)", (file_id, encode(query_sources))
                )
            return self._file_metadata(
                db, db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            )

    def withdraw_task_result(self, owner, task_id, file_id):
        with self.connect(write=True) as db:
            task = self._task(db, owner, task_id)
            if not self.executing(db, task):
                raise Conflict("Only an active turn may withdraw its own output.")
            if not self._bot(db, owner, task["bot_id"])["files_enabled"]:
                raise Conflict("Files are turned off for this owl.")
            file = db.execute(
                "SELECT id,series_id,revision FROM files WHERE id=? AND bot_id=? AND source_task_id=?",
                (file_id, task["bot_id"], task_id),
            ).fetchone()
            if not file:
                raise NotFound("This active turn did not create that file revision.")
            receipt = {
                "file_id": file["id"],
                "series_id": file["series_id"],
                "revision": file["revision"],
            }
            if db.execute(
                "INSERT INTO owl_withdrawn_results(file_id) VALUES(?) ON CONFLICT(file_id) DO NOTHING",
                (file_id,),
            ).rowcount:
                self._event(db, task_id, "result_withdrawn", receipt)
            return {**receipt, "withdrawn": True}

    def query_file_sources(self, owner, bot_id, file_id):
        with self.connect() as db:
            bot = self._bot(db, owner, bot_id)
            if not bot["files_enabled"]:
                raise Conflict("Files are turned off for this owl.")
            row = db.execute(
                "SELECT q.sources FROM owl_query_files q JOIN files f ON f.id=q.file_id WHERE f.bot_id=? AND f.id=?",
                (bot_id, file_id),
            ).fetchone()
            if row is None:
                raise NotFound("Data result not found.")
            descriptor = json.loads(row[0])
            return descriptor["file_ids"] if isinstance(descriptor, dict) else descriptor

    @staticmethod
    def _delivered_head(db, bot_id, series_id):
        row = db.execute(
            "SELECT f.id FROM files f WHERE f.bot_id=? AND f.series_id=? "
            "AND NOT EXISTS (SELECT 1 FROM owl_withdrawn_results w WHERE w.file_id=f.id) "
            "ORDER BY f.revision DESC LIMIT 1",
            (bot_id, series_id),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _revision_base(db, bot_id, name, previous_file_id, *, delivering=False):
        current = db.execute(
            "SELECT * FROM files WHERE bot_id=? AND name=? ORDER BY revision DESC LIMIT 1",
            (bot_id, name),
        ).fetchone()
        if previous_file_id:
            if not current or current["id"] != previous_file_id:
                if (
                    not current
                    or not delivering
                    or Store._delivered_head(db, bot_id, current["series_id"]) != previous_file_id
                ):
                    raise Conflict(
                        "This file changed or is unavailable. Read the latest version before saving."
                    )
        elif current:
            raise Conflict(
                "A file with this name already exists. Supply its latest previous_file_id to save a new version."
            )
        return current

    def delete_item(self, owner: str, bot_id: str, item_id: str, *, kind: str) -> None:
        if kind not in ("files", "memories"):
            raise ValueError("Unsupported item kind.")
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            if kind == "memories":
                row = db.execute(
                    "SELECT * FROM memories WHERE id=? AND bot_id=?", (item_id, bot_id)
                ).fetchone()
                if row is not None:
                    self._memory_change(
                        db,
                        bot_id,
                        item_id,
                        "deleted",
                        row["status"],
                        "deleted",
                        row["source_task_id"],
                    )
                    self._retire_superseded(db, bot_id, item_id)
            if kind == "files":
                file = db.execute(
                    "SELECT * FROM files WHERE id=? AND bot_id=?", (item_id, bot_id)
                ).fetchone()
                if file is None:
                    raise NotFound("File not found.")
                self._revision_base(db, bot_id, file["name"], item_id)
                result = db.execute(
                    "DELETE FROM files WHERE bot_id=? AND series_id=?", (bot_id, file["series_id"])
                )
            else:
                result = db.execute(
                    f"DELETE FROM {kind} WHERE id=? AND bot_id=?", (item_id, bot_id)
                )
            if not result.rowcount:
                raise NotFound("Item not found.")

    @staticmethod
    def _schedule(db: sqlite3.Connection, bot_id: str, schedule_id: str) -> dict:
        row = db.execute(
            "SELECT * FROM schedules WHERE id=? AND bot_id=?", (schedule_id, bot_id)
        ).fetchone()
        if row is None:
            raise NotFound("Schedule not found.")
        result = dict(row)
        result["timing"] = json.loads(result["timing"])
        result.update(Store._routine_access(db, schedule_id))
        assignment = db.execute(
            "SELECT r.assignment_id AS source_assignment_id,a.recipient_name,a.work_scope,"
            "a.recipient_bot_id,a.source_task_id,child.config AS child_config "
            "FROM owl_assignment_routines r LEFT JOIN owl_assignments a ON a.id=r.assignment_id "
            "LEFT JOIN tasks child ON child.id=a.child_task_id WHERE r.schedule_id=?",
            (schedule_id,),
        ).fetchone()
        if assignment:
            result["assignment"] = {
                "source_assignment_id": assignment["source_assignment_id"],
                "recipient_name": assignment["recipient_name"] or "",
            }
            if assignment["work_scope"] == "computer":
                # What each run may use: the task's card, as the person approved it.
                access = Store._assignment_access(db, assignment["source_task_id"]) or json.loads(
                    assignment["child_config"] or "{}"
                ).get("assignment_access")
                if access:
                    result["assignment"]["access"] = access
                    result["assignment"]["recipient_bot_id"] = assignment["recipient_bot_id"]
        return result

    def _assignment_for_routine(self, db, owner, schedule) -> dict | None:
        binding = schedule.get("assignment")
        if binding is None:
            return None
        if not self._is_coordinator(db, owner, schedule["bot_id"]):
            raise AssignmentRoutineBlocked("assignment_coordinator")
        row = self._assignment_rows(
            db, owner, schedule["bot_id"], assignment_id=binding["source_assignment_id"]
        ).fetchone()
        if row is None or row["child_status"] != "completed":
            raise AssignmentRoutineBlocked("assignment_missing")
        item = self._assignment_item(row)
        try:
            recipient = self._bot(db, owner, item["recipient_bot_id"])
        except NotFound as exc:
            raise AssignmentRoutineBlocked("assignment_missing") from exc
        shared = json.loads(row["shared_files"] or "[]")
        if shared:
            try:
                self._attachable_files(db, recipient, [file["id"] for file in shared])
            except Conflict as exc:
                raise AssignmentRoutineBlocked("assignment_missing") from exc
        if item.get("work_scope") == "computer":
            access = item.get("access")
            try:
                if not access:
                    raise NotFound("Assignment not found.")
                self._check_computer_access(db, recipient, access)
            except (Conflict, NotFound) as exc:
                raise AssignmentRoutineBlocked("assignment_missing") from exc
            allowed = parse_needs(schedule["access"]["allowed"])
            needs = _access_needs(access)
            if not needs.empty and not (allowed and allowed.covers(needs)):
                raise AssignmentRoutineBlocked("access")
        return item

    @staticmethod
    def _routine_access(db: sqlite3.Connection, schedule_id: str) -> dict:
        """A routine's needs and what the person allowed for it, as its listing shows them.

        ``to_allow`` is what the person is asked to allow now: the routine's needs and anything
        a run asked for beyond them, together with what they already allowed. None when all of
        it is allowed (or nothing is needed).
        """
        row = db.execute(
            "SELECT needs,allowed,allowed_at,requested FROM routine_access WHERE schedule_id=?",
            (schedule_id,),
        ).fetchone()
        needs = (parse_needs(row["needs"]) if row else None) or RoutineNeeds()
        allowed = parse_needs(row["allowed"]) if row else None
        requested = parse_needs(row["requested"]) if row else None
        wanted = needs.union(requested)
        to_allow = (
            None
            if wanted.empty or (allowed is not None and allowed.covers(wanted))
            else wanted.union(allowed)
        )
        return {
            "needs": needs.model_dump(),
            "access": {
                "allowed": allowed.model_dump() if allowed else None,
                "allowed_at": row["allowed_at"] if allowed else None,
                "requested": requested.model_dump() if requested else None,
                "to_allow": to_allow.model_dump() if to_allow else None,
            },
        }

    @staticmethod
    def _reset_routine_access(
        db: sqlite3.Connection, schedule_id: str, needs: RoutineNeeds | None, now: float
    ) -> None:
        """Save a routine's needs as new: what the person allowed was for its earlier job."""
        if needs is None or needs.empty:
            db.execute("DELETE FROM routine_access WHERE schedule_id=?", (schedule_id,))
            return
        db.execute(
            """INSERT INTO routine_access(schedule_id,needs,allowed,allowed_at,requested,updated_at)
            VALUES(?,?,NULL,NULL,NULL,?) ON CONFLICT(schedule_id) DO UPDATE SET
            needs=excluded.needs,allowed=NULL,allowed_at=NULL,requested=NULL,
            updated_at=excluded.updated_at""",
            (schedule_id, encode(needs.model_dump()), now),
        )

    @staticmethod
    def _timing(schedule: dict) -> Timing:
        return ScheduleProposal.model_validate(
            {key: schedule[key] for key in ("name", "prompt", "timing")}
        ).timing

    @staticmethod
    def _schedule_health(db: sqlite3.Connection, schedule: dict) -> dict | None:
        """The routine's failures and pending retry, for its current revision only."""
        row = db.execute(
            "SELECT * FROM schedule_health WHERE schedule_id=? AND revision=?",
            (schedule["id"], schedule["revision"]),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _save_schedule_health(
        db: sqlite3.Connection,
        schedule_id: str,
        revision: int,
        now: float,
        *,
        failures: int = 0,
        retry_at: float | None = None,
        retry_for: float | None = None,
        attempts: int = 0,
        retry_reason: str | None = None,
        pause_code: str | None = None,
    ) -> None:
        db.execute(
            """INSERT INTO schedule_health(schedule_id,revision,failures,retry_at,retry_for,
            attempts,retry_reason,pause_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(schedule_id) DO UPDATE SET revision=excluded.revision,
            failures=excluded.failures,retry_at=excluded.retry_at,retry_for=excluded.retry_for,
            attempts=excluded.attempts,retry_reason=excluded.retry_reason,
            pause_code=excluded.pause_code,updated_at=excluded.updated_at""",
            (
                schedule_id,
                revision,
                failures,
                retry_at,
                retry_for,
                attempts,
                retry_reason,
                pause_code,
                now,
            ),
        )

    @staticmethod
    def _active_schedule_run(db: sqlite3.Connection, schedule_id: str):
        return db.execute(
            "SELECT r.task_id,t.status FROM schedule_runs r JOIN tasks t ON t.id=r.task_id "
            "WHERE r.schedule_id=? AND t.status IN ('queued','running','waiting','cancelling') "
            "ORDER BY r.id DESC LIMIT 1",
            (schedule_id,),
        ).fetchone()

    def _schedule_outlook(self, db: sqlite3.Connection, schedule: dict, now: float) -> dict:
        """What the routine will do next: its next runs, a run in progress, a waiting retry."""
        health = self._schedule_health(db, schedule)
        timing = self._timing(schedule)
        first = self._next_scheduled_run(schedule, health, now, timing)
        retry_at = health["retry_at"] if health else None
        active = self._active_schedule_run(db, schedule["id"])
        return {
            "upcoming_runs": upcoming_runs(timing, first),
            "active_run": dict(active) if active else None,
            "retry": {
                "at": retry_at,
                "reason": health["retry_reason"],
                "attempt": health["attempts"],
                "max_attempts": len(RETRY_DELAYS_SECONDS)
                if health["retry_reason"] == "temporary"
                else None,
            }
            if retry_at is not None
            else None,
            "failed_runs": health["failures"] if health else 0,
            "pause_code": health["pause_code"]
            if health and schedule["state"] == "paused"
            else None,
        }

    def list_schedules(self, owner: str, bot_id: str) -> list[dict]:
        now = time.time()
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            results = []
            for row in db.execute(
                "SELECT id FROM schedules WHERE bot_id=? ORDER BY created_at,id", (bot_id,)
            ).fetchall():
                schedule = self._schedule(db, bot_id, row[0])
                last = db.execute(
                    f"""SELECT r.id,r.scheduled_for,r.task_id,r.reason,r.origin,
                    COALESCE(t.status,r.status) AS status,t.error AS task_error,{NOTHING_NEW_AT}
                    FROM schedule_runs r LEFT JOIN tasks t ON t.id=r.task_id
                    WHERE r.schedule_id=? ORDER BY r.id DESC LIMIT 1""",
                    (row[0],),
                ).fetchone()
                schedule["last_run"] = dict(last) if last else None
                schedule.update(self._schedule_outlook(db, schedule, now))
                results.append(schedule)
            return results

    def save_schedule(
        self,
        owner: str,
        bot_id: str,
        data: ScheduleInput,
        *,
        schedule_id: str | None = None,
        source_task_id: str | None = None,
        now: float | None = None,
        requested: bool = False,
        accepted_card: bool = False,
        within=None,
        assignment_id: str | None = None,
    ) -> dict:
        """Save a routine. With ``accepted_card``, the person accepted it on a card the finished
        ``source_task_id`` showed (an owl's setup): it is that turn's draft, turned on as any
        routine the owl suggests."""
        now = time.time() if now is None else now
        with self._joined(within) as db:
            self._bot(db, owner, bot_id)
            if requested:
                self.direct_request(db, owner, bot_id, source_task_id)
            if source_task_id:
                task = self._task(db, owner, source_task_id)
                if (
                    task["bot_id"] != bot_id
                    or task["status"] != ("completed" if accepted_card else "running")
                    or (schedule_id and not requested)
                ):
                    raise Conflict("This task cannot propose a schedule here.")
                if not schedule_id:
                    previous = db.execute(
                        "SELECT id,name,prompt,timing,model FROM schedules WHERE source_task_id=?",
                        (source_task_id,),
                    ).fetchone()
                    if previous:
                        if (
                            previous["name"] != data.name
                            or previous["prompt"] != data.prompt
                            or json.loads(previous["timing"]) != data.timing.model_dump(mode="json")
                            or self._routine_access(db, previous["id"])["needs"]
                            != (data.needs or RoutineNeeds()).model_dump()
                            or previous["model"] != data.model
                        ):
                            raise Conflict(
                                "This request already created a different schedule. Read the existing routine before editing it."
                            )
                        return self._schedule(db, bot_id, previous["id"])
            # Without needs or a model, the payload reads as it did before routines had them, so
            # a request resent across an upgrade still matches.
            creation = data.model_dump(
                mode="json",
                exclude={"request_id"}
                | ({"needs"} if data.needs is None else set())
                | ({"model"} if data.model is None else set()),
            )
            if assignment_id is not None:
                creation["assignment_id"] = assignment_id
            payload = encode(creation)
            if data.request_id:
                if schedule_id or source_task_id:
                    raise Conflict("Request IDs apply only to new user-created schedules.")
                previous = db.execute(
                    "SELECT schedule_id,payload FROM schedule_requests WHERE bot_id=? AND request_id=?",
                    (bot_id, data.request_id),
                ).fetchone()
                if previous:
                    if previous["payload"] != payload:
                        raise Conflict(
                            "This request already created a different schedule. Refresh your schedules before creating another."
                        )
                    return self._schedule(db, bot_id, previous["schedule_id"])
            due = next_run(data.timing, now)
            if due is None:
                raise Conflict("Choose a future time for this one-time schedule.")
            state = (
                "draft"
                if source_task_id and not requested
                else ("active" if data.enabled else "paused")
            )
            if schedule_id:
                current = self._schedule(db, bot_id, schedule_id)
                if current.get("assignment"):
                    raise Conflict(
                        "This routine uses an approved assignment. Create a new routine from an assignment to change its instructions."
                    )
                if current["revision"] != data.expected_revision:
                    raise Conflict(
                        "This schedule changed. Reopen its latest version before saving."
                    )
                revision = current["revision"] + 1
                db.execute(
                    """UPDATE schedules SET name=?,prompt=?,timing=?,state=?,revision=?,
                    next_run_at=?,pause_reason=NULL,source_task_id=?,updated_at=? WHERE id=?""",
                    (
                        data.name,
                        data.prompt,
                        encode(data.timing.model_dump(mode="json")),
                        state,
                        revision,
                        due if state == "active" else None,
                        source_task_id,
                        now,
                        schedule_id,
                    ),
                )
                # Its model, like its needs, stays as it was when an edit leaves it out.
                if "model" in data.model_fields_set:
                    db.execute("UPDATE schedules SET model=? WHERE id=?", (data.model, schedule_id))
                # An edited routine starts afresh: earlier failures and retries were for the old one.
                db.execute("DELETE FROM schedule_health WHERE schedule_id=?", (schedule_id,))
                # A new job or new needs asks the person again. Left out, needs stay as they were.
                if data.prompt != current["prompt"] or (
                    data.needs is not None and data.needs.model_dump() != current["needs"]
                ):
                    self._reset_routine_access(
                        db,
                        schedule_id,
                        data.needs
                        if data.needs is not None
                        else RoutineNeeds.model_validate(current["needs"]),
                        now,
                    )
            else:
                if data.expected_revision != 0:
                    raise Conflict("A new schedule starts at revision zero.")
                if (
                    db.execute(
                        "SELECT count(*) FROM schedules WHERE bot_id=?", (bot_id,)
                    ).fetchone()[0]
                    >= 20
                ):
                    raise LimitReached("This bot has 20 schedules. Edit an existing schedule.")
                schedule_id = uid()
                db.execute(
                    "INSERT INTO schedules(id,bot_id,name,prompt,timing,state,revision,next_run_at,"
                    "pause_reason,source_task_id,created_at,updated_at,model) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        schedule_id,
                        bot_id,
                        data.name,
                        data.prompt,
                        encode(data.timing.model_dump(mode="json")),
                        state,
                        1,
                        due if state == "active" else None,
                        None,
                        source_task_id,
                        now,
                        now,
                        data.model,
                    ),
                )
                self._reset_routine_access(db, schedule_id, data.needs, now)
                if assignment_id is not None:
                    db.execute(
                        "INSERT INTO owl_assignment_routines(schedule_id,assignment_id) VALUES(?,?)",
                        (schedule_id, assignment_id),
                    )
            if data.request_id:
                db.execute(
                    "INSERT INTO schedule_requests VALUES(?,?,?,?)",
                    (bot_id, data.request_id, schedule_id, payload),
                )
            return self._schedule(db, bot_id, schedule_id)

    def delete_schedule(self, owner: str, bot_id: str, schedule_id: str) -> None:
        """Remove a routine and its run history; a run in progress must stop first."""
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            self._schedule(db, bot_id, schedule_id)
            if db.execute(
                "SELECT 1 FROM schedule_runs r JOIN tasks t ON t.id=r.task_id WHERE r.schedule_id=? "
                "AND t.status IN ('queued','running','waiting','cancelling')",
                (schedule_id,),
            ).fetchone():
                raise Conflict("Stop this routine's current run before deleting it.")
            self._erase_schedule(db, schedule_id)

    @staticmethod
    def _erase_schedule(db: sqlite3.Connection, schedule_id: str) -> None:
        """A routine's rows, run history included; also a suggested draft the person declined."""
        db.execute("DELETE FROM schedule_runs WHERE schedule_id=?", (schedule_id,))
        db.execute("DELETE FROM schedule_requests WHERE schedule_id=?", (schedule_id,))
        db.execute("DELETE FROM schedule_run_requests WHERE schedule_id=?", (schedule_id,))
        db.execute("DELETE FROM schedule_health WHERE schedule_id=?", (schedule_id,))
        # What the person allowed was for this routine only: it goes with it.
        db.execute("DELETE FROM routine_access WHERE schedule_id=?", (schedule_id,))
        db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))

    def decide_schedule(
        self,
        owner: str,
        bot_id: str,
        schedule_id: str,
        state: str,
        expected_revision: int,
        *,
        now: float | None = None,
        source_task_id: str | None = None,
    ) -> dict:
        if state not in ("active", "paused"):
            raise ValueError("Invalid schedule state.")
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            if source_task_id:
                self.direct_request(db, owner, bot_id, source_task_id)
            current = self._schedule(db, bot_id, schedule_id)
            if current["revision"] != expected_revision:
                raise Conflict("This schedule changed. Refresh it before changing its state.")
            if state == "active":
                if source_task_id and current.get("assignment"):
                    raise Conflict("Turn on this assignment routine yourself in Routines.")
                try:
                    self._assignment_for_routine(db, owner, current)
                except AssignmentRoutineBlocked as exc:
                    if exc.code != "access":
                        raise
                    raise Conflict("Allow what this routine uses before turning it on.") from exc
            self._set_schedule_state(db, current, state, now)
            return self._schedule(db, bot_id, schedule_id)

    def set_schedule_model(
        self,
        owner: str,
        bot_id: str,
        schedule_id: str,
        data: ScheduleModelChoice,
        *,
        now: float | None = None,
    ) -> dict:
        """The person picks the model a routine's runs think with; null: its owl's own.

        Only the model changes: the routine keeps its state (a suggested draft stays one), what
        it was allowed and its failures or retry. A run already queued or under way keeps the
        model it started with. The same choice again changes nothing.
        """
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            current = self._schedule(db, bot_id, schedule_id)
            if current["model"] == data.model:
                return current
            if current["revision"] != data.expected_revision:
                raise Conflict("This routine changed. Reopen it before changing its model.")
            db.execute(
                "UPDATE schedules SET model=?,revision=revision+1,updated_at=? WHERE id=?",
                (data.model, now, schedule_id),
            )
            db.execute(
                "UPDATE schedule_health SET revision=revision+1 WHERE schedule_id=? AND revision=?",
                (schedule_id, current["revision"]),
            )
            return self._schedule(db, bot_id, schedule_id)

    def _set_schedule_state(
        self, db: sqlite3.Connection, current: dict, state: str, now: float
    ) -> None:
        """The person turns a routine on or pauses it."""
        due = next_run(self._timing(current), now) if state == "active" else None
        if state == "active" and due is None:
            raise Conflict(
                "This one-time schedule is in the past. Edit its time before enabling it."
            )
        db.execute(
            "UPDATE schedules SET state=?,revision=revision+1,next_run_at=?,pause_reason=NULL,updated_at=? WHERE id=?",
            (state, due, now, current["id"]),
        )
        # The person decided: a resumed routine starts afresh, a paused one keeps no retry.
        db.execute("DELETE FROM schedule_health WHERE schedule_id=?", (current["id"],))

    def allow_routine_access(
        self,
        owner: str,
        bot_id: str,
        schedule_id: str,
        data: RoutineAccessDecision,
        *,
        now: float | None = None,
        within=None,
    ) -> dict:
        """The person allows what a routine needs, for that routine only.

        They allow exactly what their app showed (``access.to_allow``), so a routine that
        changed meanwhile is never allowed more than they saw. With turn_on the routine is
        turned on too. Sending the same decision again after it applied changes nothing.
        ``within``: the caller's transaction (Turn on under a suggested routine's card).
        """
        now = time.time() if now is None else now
        with self._joined(within) as db:
            self._bot(db, owner, bot_id)
            current = self._schedule(db, bot_id, schedule_id)
            if current.get("assignment") and not current["assignment"].get("access"):
                raise Conflict("This assignment routine runs on its approved text only.")
            access = current["access"]
            allowed = parse_needs(access["allowed"])
            done = access["to_allow"] is None and allowed is not None and allowed.covers(data.allow)
            if done and (not data.turn_on or current["state"] == "active"):
                return current
            if current["revision"] != data.expected_revision:
                raise Conflict("This routine changed. Review it again before allowing its access.")
            if not done:
                if access["to_allow"] != data.allow.model_dump():
                    raise Conflict(
                        "This routine's access changed. Review it again before allowing it."
                    )
                db.execute(
                    "UPDATE routine_access SET allowed=?,allowed_at=?,requested=NULL,updated_at=? "
                    "WHERE schedule_id=?",
                    (encode(data.allow.model_dump()), now, now, schedule_id),
                )
            if data.turn_on and current["state"] != "active":
                if current.get("assignment"):
                    self._assignment_for_routine(db, owner, self._schedule(db, bot_id, schedule_id))
                self._set_schedule_state(db, current, "active", now)
            return self._schedule(db, bot_id, schedule_id)

    def revoke_routine_access(
        self, owner: str, bot_id: str, schedule_id: str, *, now: float | None = None
    ) -> dict:
        """Take back what the person allowed a routine; from its next run on, it asks again.

        A run already queued or under way keeps what it started with.
        """
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            self._schedule(db, bot_id, schedule_id)
            db.execute(
                "UPDATE routine_access SET allowed=NULL,allowed_at=NULL,updated_at=? "
                "WHERE schedule_id=?",
                (now, schedule_id),
            )
            return self._schedule(db, bot_id, schedule_id)

    def routine_run_access(self, owner: str, task_id: str) -> RoutineNeeds | None:
        """What the person allowed this routine run to use, fixed when the run was queued.

        None for any other turn, a routine run with nothing allowed, or a run no longer working.
        """
        with self.connect() as db:
            task = self._task(db, owner, task_id)
            if not self.executing(db, task):
                return None
            row = db.execute(
                "SELECT r.config FROM schedule_runs r JOIN schedules s ON s.id=r.schedule_id "
                "WHERE r.task_id=? AND s.bot_id=?",
                (task_id, task["bot_id"]),
            ).fetchone()
            return parse_needs(json.loads(row["config"]).get("access")) if row else None

    def routine_run_model(self, owner: str, task_id: str, bot_id: str) -> str | None:
        """The model a run of one of this owl's routines thinks with, fixed when it was queued.

        None for any other turn (another owl's included, such as a peer answering it) and for a
        run of a routine without a model of its own: it thinks with its owl's.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT r.config FROM schedule_runs r JOIN schedules s ON s.id=r.schedule_id "
                "JOIN bots b ON b.id=s.bot_id WHERE r.task_id=? AND s.bot_id=? AND b.owner=?",
                (task_id, bot_id, owner),
            ).fetchone()
            return json.loads(row["config"]).get("model") if row else None

    def routine_needs_more(self, owner: str, task_id: str) -> bool:
        """Whether this routine run asked for something its routine doesn't allow."""
        with self.connect() as db:
            self._task(db, owner, task_id)
            return self._run_access_need(db, task_id) is not None

    def outward_step(self, owner: str, task_id: str):
        """The task's first step that changed something outside the owl's own records, or may
        have (step_effects.OutwardStep); None when every step read or kept its records."""
        from .step_effects import first_outward_step

        with self.connect() as db:
            self._task(db, owner, task_id)
            return first_outward_step(db, task_id)

    def schedule_runs(
        self, owner: str, bot_id: str, schedule_id: str, before: int | None = None
    ) -> dict:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            self._schedule(db, bot_id, schedule_id)
            rows = db.execute(
                f"""SELECT r.*,COALESCE(t.status,r.status) AS current_status,t.error AS task_error,
                {NOTHING_NEW_AT} FROM schedule_runs r LEFT JOIN tasks t ON t.id=r.task_id
                WHERE r.schedule_id=? AND (CAST(? AS BIGINT) IS NULL OR r.id<?) ORDER BY r.id DESC LIMIT 21""",
                (schedule_id, before, before),
            ).fetchall()
            return {
                "runs": [{**dict(row), "config": json.loads(row["config"])} for row in rows[:20]],
                "next_before": rows[19]["id"] if len(rows) > 20 else None,
            }

    def schedule_context(self, owner: str, task_id: str) -> dict | None:
        """Preserve the originating request as evidence for a model-drafted task.

        User edits clear the proposal source, so their revised task replaces this
        briefing. This is the scheduled task's own provenance, not general recall.
        """
        with self.connect() as db:
            task = self._task(db, owner, task_id)
            row = db.execute(
                "SELECT config,scheduled_for,created_at FROM schedule_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            config = json.loads(row["config"])
            result = {"name": config["name"], "scheduled_for": row["scheduled_for"]}
            if row["created_at"] - row["scheduled_for"] > ON_TIME_SECONDS:
                # Caught up after the runtime was unavailable: the person should know.
                result["started_late_minutes"] = int(
                    (row["created_at"] - row["scheduled_for"]) // 60
                )
                result["late_start"] = (
                    "This run started after its time because the runtime was unavailable or "
                    "busy when it was due. Say so in a few words when timing matters to the result."
                )
            if source_id := config.get("source_task_id"):
                source = self._task(db, owner, source_id)
                if source["bot_id"] != task["bot_id"]:
                    raise Conflict("Scheduled source belongs to another bot.")
                result.update(source_task_id=source_id, original_request=source["message"])
            return result

    def _pause_schedule(
        self,
        db: sqlite3.Connection,
        schedule: dict,
        code: str,
        now: float,
        *,
        failures: int = 0,
        reason: str | None = None,
    ) -> None:
        """Pause a routine for something only the person can resolve, saying what to do.

        Only the revision the caller read is paused: a newer edit is never paused by an
        older run's outcome.
        """
        changed = db.execute(
            "UPDATE schedules SET state='paused',next_run_at=NULL,pause_reason=?,"
            "revision=revision+1,updated_at=? WHERE id=? AND revision=?",
            (reason or PAUSE_REASONS[code], now, schedule["id"], schedule["revision"]),
        ).rowcount
        if changed:
            self._save_schedule_health(
                db,
                schedule["id"],
                schedule["revision"] + 1,
                now,
                failures=failures,
                pause_code=code,
            )

    def _settle_schedule_run(
        self, db: sqlite3.Connection, task_id: str, status: str, failure: RunFailure | None
    ) -> None:
        """What a finished routine run means for its routine.

        A completed run clears earlier failures. A failure that passes is tried again: a
        busy service soon, a limit once it frees up. One only the person can fix pauses the
        routine, saying what to do. Anything else lets the next run go ahead, and several
        failed runs in a row pause it. A run the person stopped changes nothing.
        """
        run = db.execute(
            "SELECT schedule_id,schedule_revision,scheduled_for,origin FROM schedule_runs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if run is None:
            return
        row = db.execute("SELECT * FROM schedules WHERE id=?", (run["schedule_id"],)).fetchone()
        # Do not replace a newer user edit with an older run's outcome.
        if row is None or row["revision"] != run["schedule_revision"]:
            return
        schedule = {**dict(row), "timing": json.loads(row["timing"])}
        now = time.time()
        health = self._schedule_health(db, schedule)
        need = self._run_access_need(db, task_id) if status != "cancelled" else None
        if need is not None:
            # The run needed something the person hasn't allowed for this routine. However the
            # turn ended, only they can allow it: the routine waits for them, saying what.
            failure = RunFailure("access")
        if status == "completed" and need is None and not (failure and failure.kind == "review"):
            db.execute("DELETE FROM schedule_health WHERE schedule_id=?", (schedule["id"],))
            return
        if status not in ("completed", "failed", "interrupted"):
            return
        failure = failure or RunFailure("interrupted" if status == "interrupted" else "failed")
        failures = health["failures"] if health else 0
        if failure.kind in NEEDS_PERSON:
            reason = None
            pause_code = failure.kind
            if (
                failure.kind == "review"
                and db.execute(
                    "SELECT 1 FROM owl_assignment_routines WHERE schedule_id=?", (schedule["id"],)
                ).fetchone()
            ):
                pause_code = "assignment_review"
            if need is not None:
                requested = parse_needs(
                    self._routine_access(db, schedule["id"])["access"]["requested"]
                )
                need = need.union(requested)
                reason = approval_reason(need)
                db.execute(
                    """INSERT INTO routine_access(schedule_id,needs,requested,updated_at)
                    VALUES(?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET
                    requested=excluded.requested,updated_at=excluded.updated_at""",
                    (
                        schedule["id"],
                        encode(RoutineNeeds().model_dump()),
                        encode(need.model_dump()),
                        now,
                    ),
                )
            # A finished one-time routine has nothing left to pause; the run says what failed.
            if schedule["state"] == "active":
                self._pause_schedule(
                    db, schedule, pause_code, now, failures=failures, reason=reason
                )
            return
        if run["origin"] == "manual" or failure.kind == "interrupted":
            # The person started this run and sees its result. An interrupted run may have
            # acted already, so it is never replayed: the next scheduled run goes ahead.
            return
        # A run that already took a step may have acted, so it is never replayed. A limit
        # still holds the routine's next runs until it frees up, without the replay.
        replay = not db.execute(
            "SELECT 1 FROM events WHERE task_id=? AND kind='tool_started' LIMIT 1", (task_id,)
        ).fetchone()
        if failure.kind == "temporary" and not replay:
            failure = RunFailure("failed")
        retrying = (
            run["origin"] == "retry" and health is not None and health["retry_for"] is not None
        )
        attempt = health["attempts"] + 1 if retrying else 1
        at = retry_time(failure, attempt, now, schedule["next_run_at"])
        if at is not None:
            self._save_schedule_health(
                db,
                schedule["id"],
                schedule["revision"],
                now,
                failures=failures,
                retry_at=at,
                retry_for=(health["retry_for"] if retrying else run["scheduled_for"])
                if replay
                else None,
                attempts=attempt,
                retry_reason=failure.kind,
            )
            return
        failures += 1
        if failures >= FAILED_RUNS_BEFORE_PAUSE and schedule["state"] == "active":
            self._pause_schedule(db, schedule, "repeated", now, failures=failures)
        else:
            self._save_schedule_health(
                db, schedule["id"], schedule["revision"], now, failures=failures
            )

    @staticmethod
    def _run_access_need(db: sqlite3.Connection, task_id: str) -> RoutineNeeds | None:
        """What a routine run asked for beyond what its routine allows (runtime.py records it)."""
        need = None
        for row in db.execute(
            "SELECT data FROM events WHERE task_id=? AND kind='routine_access_needed' ORDER BY seq",
            (task_id,),
        ):
            asked = parse_needs(json.loads(row["data"]).get("need"))
            need = asked.union(need) if asked else need
        return need

    @staticmethod
    def _schedule_run_recorded(db: sqlite3.Connection, schedule_id: str, scheduled_for: float):
        return db.execute(
            "SELECT 1 FROM schedule_runs WHERE schedule_id=? AND scheduled_for=?",
            (schedule_id, scheduled_for),
        ).fetchone()

    @staticmethod
    def _record_schedule_run(
        db: sqlite3.Connection,
        schedule: dict,
        scheduled_for: float,
        task_id: str | None,
        status: str,
        reason: str | None,
        now: float,
        origin: str | None = None,
    ) -> None:
        config = {key: schedule[key] for key in ("name", "prompt", "timing", "source_task_id")}
        if schedule.get("assignment"):
            config["assignment"] = schedule["assignment"]
        if task_id is not None:
            # The run uses what was allowed when it was queued: a change takes effect next run.
            allowed = Store._routine_access(db, schedule["id"])["access"]["allowed"]
            if allowed:
                config["access"] = allowed
            # So does the model it thinks with; without one, it thinks with its owl's.
            if schedule.get("model"):
                config["model"] = schedule["model"]
        db.execute(
            "INSERT INTO schedule_runs(schedule_id,schedule_revision,scheduled_for,task_id,status,reason,config,created_at,origin) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                schedule["id"],
                schedule["revision"],
                scheduled_for,
                task_id,
                status,
                reason,
                encode(config),
                now,
                origin,
            ),
        )

    @staticmethod
    def _routine_blocked(db: sqlite3.Connection, owner: str, schedule: dict) -> bool:
        """Runs never overlap for one owl or one routine, and wait while the queue is full."""
        if schedule.get("assignment") and Store._active_schedule_run(db, schedule["id"]):
            return True
        busy = db.execute(
            "SELECT 1 FROM tasks t WHERE t.bot_id=? AND "
            "(t.status IN ('queued','running','cancelling') OR "
            "(t.status='waiting' AND EXISTS (SELECT 1 FROM schedule_runs r "
            "WHERE r.task_id=t.id AND r.schedule_id=?)))",
            (schedule["bot_id"], schedule["id"]),
        ).fetchone()
        return bool(busy) or Store._queue_full(db, owner)

    @staticmethod
    def _queue_full(db: sqlite3.Connection, owner: str) -> bool:
        return (
            db.execute(
                "SELECT count(*) FROM tasks WHERE owner=? AND "
                "status IN ('queued','running','waiting','cancelling')",
                (owner,),
            ).fetchone()[0]
            >= 20
        )

    @staticmethod
    def _waiting_for_person(db: sqlite3.Connection, schedule_id: str) -> bool:
        """A run of this routine is waiting for the person: an approval or their computer."""
        return bool(
            db.execute(
                "SELECT 1 FROM schedule_runs r JOIN tasks t ON t.id=r.task_id "
                "JOIN background_turns b ON b.task_id=t.id WHERE r.schedule_id=? "
                "AND t.status='waiting' AND (b.state IN ('approval','computer_control') OR "
                "(b.state='local_computer' AND EXISTS (SELECT 1 FROM bot_local_requests q "
                "WHERE q.task_id=t.id AND q.status='pending')))",
                (schedule_id,),
            ).fetchone()
        )

    @staticmethod
    def _allowance_used(db: sqlite3.Connection, owner: str, max_calls: int | None) -> bool:
        return (
            max_calls is not None
            and db.execute(
                "SELECT count(*) FROM provider_calls p JOIN tasks t ON t.id=p.task_id WHERE t.owner=?",
                (owner,),
            ).fetchone()[0]
            >= max_calls
        )

    def _start_routine_run(
        self,
        db: sqlite3.Connection,
        owner: str,
        schedule: dict,
        scheduled_for: float,
        now: float,
        origin: str | None = None,
        *,
        assignment: dict | None = None,
    ) -> str:
        """Queue one routine run. Its receipt belongs to the schedule that started it."""
        if assignment is None:
            chat = self._ensure_chat(db, owner, schedule["bot_id"], schedule["name"])
            task_id = self._enqueue(
                db,
                owner,
                chat["id"],
                TaskInput(message=schedule["prompt"], request_id="schedule_" + uid()),
            )["id"]
        else:
            task_id = self._start_routine_assignment(db, owner, schedule, assignment, now)
        self._event(
            db,
            task_id,
            "scheduled",
            {
                "schedule_id": schedule["id"],
                "name": schedule["name"],
                "scheduled_for": scheduled_for,
                **({"origin": origin} if origin else {}),
                **(
                    {"late_by_seconds": int(now - scheduled_for)}
                    if now - scheduled_for > ON_TIME_SECONDS
                    else {}
                ),
            },
        )
        return task_id

    def _start_routine_assignment(self, db, owner, schedule, assignment, now) -> str:
        """Record the coordinator's scheduled handoff and queue the real recipient task."""
        chat = self._ensure_chat(db, owner, schedule["bot_id"], schedule["name"])
        parent_id, assignment_id = uid(), uid()
        db.execute(
            "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,created_at,"
            "updated_at,conversation_visible) VALUES(?,?,?,?,?,'','completed',?,?,?,0)",
            (
                parent_id,
                chat["id"],
                schedule["bot_id"],
                owner,
                "assignment_routine_" + parent_id,
                encode(
                    {
                        "assignment_routine": {
                            "schedule_id": schedule["id"],
                            "source_task_id": assignment["source_task_id"],
                        }
                    }
                ),
                now,
                now,
            ),
        )
        recipient = self._bot(db, owner, assignment["recipient_bot_id"])
        db.execute(
            "INSERT INTO owl_assignments(id,source_task_id,recipient_bot_id,recipient_name,"
            "brief,expected_result,work_scope,decision,created_at) VALUES(?,?,?,?,?,?,?,'approved',?)",
            (
                assignment_id,
                parent_id,
                recipient["id"],
                recipient["name"],
                assignment["brief"],
                assignment["expected_result"],
                assignment.get("work_scope", "text"),
                now,
            ),
        )
        access = assignment.get("access") if assignment.get("work_scope") == "computer" else None
        prior = db.execute(
            "SELECT inputs FROM owl_assignment_files WHERE assignment_id=?", (assignment["id"],)
        ).fetchone()
        if prior and (shared := json.loads(prior["inputs"])):
            # The recurring job can use the exact revisions already delivered to this owl.
            # It never reads the coordinator's Library again or copies a newer revision.
            db.execute(
                "INSERT INTO owl_assignment_files(assignment_id,inputs) VALUES(?,?)",
                (assignment_id, prior["inputs"]),
            )
            if access is not None:
                access = {
                    **access,
                    "file_ids": list(
                        dict.fromkeys([*access.get("file_ids", []), *(f["id"] for f in shared)])
                    ),
                }
        return self._enqueue_assignment_child(
            db,
            owner,
            recipient,
            assignment_id,
            now,
            access=access,
        )

    def run_schedule_now(
        self,
        owner: str,
        bot_id: str,
        schedule_id: str,
        request_id: str,
        *,
        max_calls: int | None,
        now: float | None = None,
    ) -> dict:
        """Run a routine once now, without changing its schedule.

        The same request_id answers with the run it started, so a double click or a resent
        request never starts two runs; a routine with a run in progress is not started again.
        """
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            schedule = self._schedule(db, bot_id, schedule_id)
            previous = db.execute(
                "SELECT schedule_id,task_id FROM schedule_run_requests WHERE bot_id=? AND request_id=?",
                (bot_id, request_id),
            ).fetchone()
            if previous:
                if previous["schedule_id"] != schedule_id:
                    raise Conflict(
                        "This request already ran a different routine. Refresh your routines and try again."
                    )
                return {"task_id": previous["task_id"], "schedule_id": schedule_id}
            if schedule["state"] == "draft":
                raise Conflict("Review this suggested routine and turn it on before running it.")
            if self._active_schedule_run(db, schedule_id) or self._schedule_run_recorded(
                db, schedule_id, now
            ):
                raise Conflict("This routine is already running. Wait for that run to finish.")
            if self._queue_full(db, owner):
                raise LimitReached("Too much work is waiting to run. Try again when some finishes.")
            if self._allowance_used(db, owner, max_calls):
                raise LimitReached(
                    "This workspace has used its model-call allowance, so the routine can't run now."
                )
            assignment = self._assignment_for_routine(db, owner, schedule)
            task_id = self._start_routine_run(
                db, owner, schedule, now, now, "manual", assignment=assignment
            )
            self._record_schedule_run(db, schedule, now, task_id, "queued", None, now, "manual")
            db.execute(
                "INSERT INTO schedule_run_requests(bot_id,request_id,schedule_id,task_id,created_at) VALUES(?,?,?,?,?)",
                (bot_id, request_id, schedule_id, task_id, now),
            )
            return {"task_id": task_id, "schedule_id": schedule_id}

    def enqueue_due_schedules(
        self, owner: str, *, max_calls: int | None, now: float | None = None
    ) -> int:
        """Advance occurrence and enqueue its task in one transaction.

        A late routine runs its latest occurrence once, late, while that occurrence is
        within CATCH_UP_SECONDS; older occurrences are recorded as missed, never
        replayed. Existing task records are the execution receipts; the desktop never
        acknowledges or starts these runs. Temporary foreground/queue pressure retains
        the deadline until admission or the end of the catch-up window. Unrelated background work does
        not block the conversation lane, but a routine cannot overlap itself.
        Retries of failed runs come due here too; a scheduled run supersedes them. An archived
        owl's routines rest: none of its runs or retries start until it comes back.
        """
        now = time.time() if now is None else now
        queued = 0
        with self.connect(write=True) as db:
            due_rows = db.execute(
                """SELECT s.id,s.bot_id FROM schedules s JOIN bots b ON b.id=s.bot_id
                WHERE b.owner=? AND b.archived_at IS NULL AND s.state='active' AND s.next_run_at<=?
                ORDER BY s.next_run_at,s.id""",
                (owner, now),
            ).fetchall()
            for row in due_rows:
                schedule = self._schedule(db, row["bot_id"], row["id"])
                try:
                    assignment = self._assignment_for_routine(db, owner, schedule)
                except AssignmentRoutineBlocked as exc:
                    self._pause_schedule(db, schedule, exc.code, now)
                    continue
                timing = self._timing(schedule)
                due = schedule["next_run_at"]
                health = self._schedule_health(db, schedule)
                if (
                    health
                    and health["retry_reason"] == "limit"
                    and health["retry_at"] is not None
                    and health["retry_at"] > now
                ):
                    # A plan or usage limit holds: runs wait for its retry, which comes first.
                    following = advance_run(timing, due, health["retry_at"])
                    db.execute(
                        "UPDATE schedules SET next_run_at=?,state=?,updated_at=? WHERE id=?",
                        (following, "active" if following else "ended", now, schedule["id"]),
                    )
                    if not self._schedule_run_recorded(db, schedule["id"], due):
                        self._record_schedule_run(
                            db,
                            schedule,
                            due,
                            None,
                            "skipped",
                            "Skipped while a plan or usage limit holds. The routine runs again "
                            "once the limit frees up.",
                            now,
                        )
                    continue
                # The occurrence to run now: the due one while it is on time, otherwise the
                # latest one still recent enough to catch up. None: everything is too old.
                run_for = (
                    due
                    if now - due <= ON_TIME_SECONDS
                    else latest_run(timing, due, now, within=CATCH_UP_SECONDS)
                )
                recorded = run_for is not None and self._schedule_run_recorded(
                    db, schedule["id"], run_for
                )
                if run_for is not None and not recorded:
                    if self._waiting_for_person(db, schedule["id"]):
                        # Its last run still waits for the person: say so instead of
                        # quietly holding every run after it.
                        self._pause_schedule(
                            db,
                            schedule,
                            "approval",
                            now,
                            failures=health["failures"] if health else 0,
                        )
                        continue
                    if self._routine_blocked(db, owner, schedule):
                        continue
                following = advance_run(timing, due, now)
                db.execute(
                    "UPDATE schedules SET next_run_at=?,state=?,updated_at=? WHERE id=?",
                    (following, "active" if following else "ended", now, schedule["id"]),
                )
                if run_for != due and not self._schedule_run_recorded(db, schedule["id"], due):
                    self._record_schedule_run(
                        db,
                        schedule,
                        due,
                        None,
                        "missed",
                        "The runtime was unavailable or busy when this was due. Missed occurrences "
                        "were not replayed; the latest one ran late instead."
                        if run_for is not None
                        else f"The runtime was unavailable or busy for more than "
                        f"{CATCH_UP_SECONDS // 3600} hours after this was due. Missed occurrences "
                        "through this check were not replayed.",
                        now,
                    )
                if run_for is None or recorded:
                    continue
                status, reason, task_id = "queued", None, None
                if self._allowance_used(db, owner, max_calls):
                    status, reason = (
                        "skipped",
                        "Skipped because this workspace has used its model-call allowance.",
                    )
                    self._pause_schedule(
                        db,
                        schedule,
                        "allowance",
                        now,
                        failures=health["failures"] if health else 0,
                    )
                else:
                    task_id = self._start_routine_run(
                        db, owner, schedule, run_for, now, assignment=assignment
                    )
                    queued += 1
                    if health and health["retry_at"] is not None:
                        # This run does the work a waiting retry would have done.
                        self._save_schedule_health(
                            db,
                            schedule["id"],
                            schedule["revision"],
                            now,
                            failures=health["failures"],
                        )
                self._record_schedule_run(db, schedule, run_for, task_id, status, reason, now)
            queued += self._enqueue_due_retries(db, owner, max_calls=max_calls, now=now)
        return queued

    def _enqueue_due_retries(
        self, db: sqlite3.Connection, owner: str, *, max_calls: int | None, now: float
    ) -> int:
        """Start the retries of failed runs whose time has come, when the owl is free."""
        queued = 0
        rows = db.execute(
            """SELECT s.id,s.bot_id FROM schedule_health h JOIN schedules s ON s.id=h.schedule_id
            JOIN bots b ON b.id=s.bot_id WHERE b.owner=? AND b.archived_at IS NULL
            AND h.revision=s.revision AND s.state IN ('active','ended') AND h.retry_at<=?
            ORDER BY h.retry_at,s.id""",
            (owner, now),
        ).fetchall()
        for row in rows:
            schedule = self._schedule(db, row["bot_id"], row["id"])
            try:
                assignment = self._assignment_for_routine(db, owner, schedule)
            except AssignmentRoutineBlocked as exc:
                self._pause_schedule(db, schedule, exc.code, now)
                continue
            health = self._schedule_health(db, schedule)
            at = health["retry_at"]
            superseded = (
                schedule["state"] == "active"
                and schedule["next_run_at"] is not None
                and schedule["next_run_at"] <= at
            )
            if (
                superseded
                or health["retry_for"] is None
                or now - at > CATCH_UP_SECONDS
                or self._schedule_run_recorded(db, schedule["id"], at)
            ):
                # A scheduled run does the same work, the wait was only a hold on the
                # routine's runs, or the retry is too old to replay.
                self._save_schedule_health(
                    db, schedule["id"], schedule["revision"], now, failures=health["failures"]
                )
                continue
            if self._routine_blocked(db, owner, schedule):
                continue
            if self._allowance_used(db, owner, max_calls):
                self._record_schedule_run(
                    db,
                    schedule,
                    at,
                    None,
                    "skipped",
                    "Skipped because this workspace has used its model-call allowance.",
                    now,
                    "retry",
                )
                if schedule["state"] == "active":
                    self._pause_schedule(
                        db, schedule, "allowance", now, failures=health["failures"]
                    )
                else:
                    self._save_schedule_health(
                        db, schedule["id"], schedule["revision"], now, failures=health["failures"]
                    )
                continue
            task_id = self._start_routine_run(
                db, owner, schedule, at, now, "retry", assignment=assignment
            )
            self._record_schedule_run(db, schedule, at, task_id, "queued", None, now, "retry")
            # The retry is under way: keep which run it retries and how many tries it took.
            self._save_schedule_health(
                db,
                schedule["id"],
                schedule["revision"],
                now,
                failures=health["failures"],
                retry_for=health["retry_for"],
                attempts=health["attempts"],
                retry_reason=health["retry_reason"],
            )
            queued += 1
        return queued
