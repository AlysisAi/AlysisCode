from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import failures
from .file_revisions import migrate as migrate_file_revisions
from .files import FILE_CHUNK_BYTES, INLINE_FILE_BYTES, MAX_FILE_BYTES, FileQuery
from .models import (
    BotInput,
    ConversationPageQuery,
    FileInput,
    HistoryQuery,
    IntroInput,
    MemoryInput,
    NoteConsolidation,
    NoteInput,
    ReplyReviewSettings,
    TaskInput,
    TimeZoneSettings,
)
from .object_storage import ObjectRef, ObjectStorageError
from .routine_access import RoutineAccessDecision, RoutineNeeds, approval_reason, parse_needs
from .scheduling import (
    CATCH_UP_SECONDS,
    FAILED_RUNS_BEFORE_PAUSE,
    NEEDS_PERSON,
    ON_TIME_SECONDS,
    PAUSE_REASONS,
    RETRY_DELAYS_SECONDS,
    RunFailure,
    ScheduleInput,
    ScheduleProposal,
    Timing,
    advance_run,
    latest_run,
    next_run,
    retry_time,
    upcoming_runs,
)
from .schema import SCHEMA_VERSION, schema
from .text_search import excerpt, loose_contains, loose_fold

ACTIVE = ("queued", "running", "waiting", "cancelling")
TERMINAL = ("completed", "failed", "cancelled", "interrupted")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BackgroundInstructionsPending(Conflict):
    """A read-only model step was not admitted; refresh exact user updates."""


class LimitReached(Exception):
    pass


class LibraryCapacityExceeded(LimitReached):
    """A definite admission refusal: no Library file was published."""


def uid() -> str:
    return uuid4().hex


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
            for table in ("workspace_objects", "browser_states"):
                if "settled" not in {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})")
                }:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1))"
                    )
            if "failure" not in {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN failure TEXT")
            bot_columns = {row["name"] for row in db.execute("PRAGMA table_info(bots)")}
            for part in ("color", "background"):
                if part not in bot_columns:
                    db.execute(f"ALTER TABLE bots ADD COLUMN {part} TEXT")
            if objects is not None:
                db.execute("INSERT INTO object_storage_enabled VALUES(1) ON CONFLICT DO NOTHING")
            elif db.execute("SELECT 1 FROM object_storage_enabled").fetchone():
                raise ObjectStorageError("This database requires its configured object storage.")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        path.chmod(0o600)

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
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
        running background turn); routine runs, activations, an owl's first hello, peer turns
        and turns waiting for an answer never qualify.
        """
        task = cls._task(db, owner, task_id)
        if (
            task["bot_id"] != bot_id
            or not (cls.executing(db, task) if background else task["status"] == "running")
            or task["config"].get("activation")
            or task["config"].get("intro")
            or db.execute("SELECT 1 FROM schedule_runs WHERE task_id=?", (task_id,)).fetchone()
            or db.execute("SELECT 1 FROM task_waits WHERE task_id=?", (task_id,)).fetchone()
            or db.execute(
                "SELECT 1 FROM peer_exchanges WHERE source_task_id=?", (task_id,)
            ).fetchone()
        ):
            raise Conflict("Routine changes require a current user request in this bot's chat.")
        return task

    @staticmethod
    def _bot(db: sqlite3.Connection, owner: str, bot_id: str) -> dict:
        row = db.execute("SELECT * FROM bots WHERE id=? AND owner=?", (bot_id, owner)).fetchone()
        if row is None:
            raise NotFound("Bot not found.")
        result = dict(row)
        result.pop("owner")
        for key in ("memory_enabled", "files_enabled"):
            result[key] = bool(result[key])
        return result

    @staticmethod
    def _task(db: sqlite3.Connection, owner: str, task_id: str) -> dict:
        row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone()
        if row is None:
            raise NotFound("Task not found.")
        result = dict(row)
        result.pop("owner")
        result["config"] = json.loads(result["config"])
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
        cls._bot(db, owner, row["bot_id"])
        return dict(row)

    @staticmethod
    def _event(db: sqlite3.Connection, task_id: str, kind: str, data: dict) -> None:
        db.execute(
            "INSERT INTO events(task_id,kind,data,created_at) VALUES(?,?,?,?)",
            (task_id, kind, encode(data), time.time()),
        )

    def list_bots(self, owner: str) -> list[dict]:
        with self.connect() as db:
            bots = [
                self._bot(db, owner, row[0])
                for row in db.execute(
                    "SELECT id FROM bots WHERE owner=? ORDER BY created_at,id", (owner,)
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
            return bots

    @staticmethod
    def _now_doing(db: sqlite3.Connection, bot_id: str) -> dict | None:
        """What the owl's newest working task is doing now, for one live line in the owl list.

        A task works while it runs in chat or as background work that is queued or running.
        step is the tool now running (the app words it in the person's language) with its
        search topic or file name when it has one, note the owl's latest short update, host
        the last web address the task opened.
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

    def save_bot(
        self, owner: str, data: BotInput, bot_id: str | None = None, *, within=None
    ) -> dict:
        with self._joined(within) as db:
            now = time.time()
            values = (
                data.name,
                data.purpose,
                data.instructions,
                data.memory_enabled,
                data.files_enabled,
            )
            if bot_id is None:
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
                if (
                    db.execute("SELECT count(*) FROM bots WHERE owner=?", (owner,)).fetchone()[0]
                    >= 30
                ):
                    raise LimitReached(
                        "You can have up to 30 bots. Delete one to make room for a new one."
                    )
                bot_id = uid()
                db.execute(
                    "INSERT INTO bots(id,owner,name,purpose,instructions,memory_enabled,"
                    "files_enabled,created_at,updated_at,color,background) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (bot_id, owner, *values, now, now, data.color, data.background),
                )
                if request_id:
                    db.execute(
                        "INSERT INTO bot_create_requests VALUES(?,?,?,?,?)",
                        (owner, request_id, bot_id, setup_hash, now),
                    )
            else:
                current = self._bot(db, owner, bot_id)
                # Its look, like its name, is only a label: it applies at once. A change that
                # leaves a part out (an app from before looks) keeps the one the person picked.
                for part in ("color", "background"):
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

    def delete_bot(self, owner: str, bot_id: str) -> None:
        """Remove a bot and what it holds from its owner's account.

        Its conversation (messages, their attachments, activity, tool results), files, uploads,
        memories, working notes, questions, proposals, About me suggestions, approvals, routines
        and file events are erased now; erasing its checkpoint rows lets retention
        reclaim their stored objects. The bot, chat and task rows stay as empty
        records under a tombstone owner that no account can resolve, because
        model-call accounting and other history still reference them; every
        owner-scoped read (lists, tasks, conversations, routines, peers, limits
        and Library usage) stops seeing them at once. Running work must stop
        first, so nothing keeps acting for a bot that is gone.
        """
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            if db.execute(
                "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling')",
                (bot_id,),
            ).fetchone():
                raise Conflict("Stop this bot's work before deleting it.")
            for row in db.execute(
                "SELECT record FROM computer_allocations WHERE bot_id=?", (bot_id,)
            ).fetchall():
                if json.loads(row["record"]).get("state") != "deleted":
                    raise Conflict("Wait for this bot's computer to shut down, then delete it.")
            tasks = "SELECT id FROM tasks WHERE bot_id=?"
            chats = "SELECT id FROM chats WHERE bot_id=?"
            notes = "SELECT id FROM working_notes WHERE bot_id=?"
            # Children before parents, so foreign keys hold at every step.
            for statement, count in (
                (f"DELETE FROM messages WHERE chat_id IN ({chats})", 1),
                (f"DELETE FROM chat_read_cursors WHERE chat_id IN ({chats})", 1),
                (f"DELETE FROM events WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM task_tool_results WHERE task_id IN ({tasks})", 1),
                (f"DELETE FROM task_files WHERE task_id IN ({tasks})", 1),
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
                ("DELETE FROM bot_action_reviews WHERE bot_id=?", 1),
                ("DELETE FROM bot_action_rules WHERE bot_id=?", 1),
                ("DELETE FROM bot_review_settings WHERE bot_id=?", 1),
                ("DELETE FROM bot_context_summaries WHERE bot_id=?", 1),
                (
                    f"DELETE FROM note_consolidations WHERE source_note_id IN ({notes}) "
                    f"OR target_note_id IN ({notes})",
                    2,
                ),
                (f"DELETE FROM note_revisions WHERE note_id IN ({notes})", 1),
                ("DELETE FROM working_notes WHERE bot_id=?", 1),
                ("DELETE FROM memory_changes WHERE bot_id=?", 1),
                ("DELETE FROM memories WHERE bot_id=?", 1),
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
            db.execute("UPDATE chats SET title='' WHERE bot_id=?", (bot_id,))
            db.execute(
                "UPDATE bots SET owner=?,name='',purpose='',instructions='',color=NULL,background=NULL,updated_at=? WHERE id=?",
                (tombstone, now, bot_id),
            )

    def create_chat(self, owner: str, bot_id: str, title: str) -> dict:
        with self.connect(write=True) as db:
            return self._ensure_chat(db, owner, bot_id, title)

    @classmethod
    def _ensure_chat(cls, db: sqlite3.Connection, owner: str, bot_id: str, title: str) -> dict:
        cls._bot(db, owner, bot_id)
        # One stable conversation per bot. Keep legacy chat rows and source IDs;
        # reads project their messages into this same timeline without rewriting data.
        row = db.execute(
            "SELECT * FROM chats WHERE bot_id=? ORDER BY created_at,id LIMIT 1", (bot_id,)
        ).fetchone()
        if row is not None:
            return dict(row)
        chat_id = uid()
        db.execute("INSERT INTO chats VALUES(?,?,?,?)", (chat_id, bot_id, title, time.time()))
        return cls._chat(db, owner, chat_id)

    @staticmethod
    def _chat_activities(db: sqlite3.Connection, bot_id: str) -> list[dict]:
        """Use event order, not client clocks, for receipts and unread state."""
        chat = db.execute(
            "SELECT * FROM chats WHERE bot_id=? ORDER BY created_at,id LIMIT 1", (bot_id,)
        ).fetchone()
        if chat is None:
            return []
        # Per task: its newest event and its newest finishing event, each one index lookup. The
        # owl list asks this for every owl every few seconds, so it must not read the events
        # themselves, which grow with every step an owl ever took.
        row = db.execute(
            """SELECT coalesce(max(x.latest),0) AS latest_seq,
                      coalesce(max(x.finished),0) AS activity_seq,
                      coalesce(max(CASE WHEN x.finished>coalesce(r.event_seq,0) THEN 1 ELSE 0 END),0)
                        AS unread
               FROM (SELECT t.chat_id,
                            (SELECT max(e.seq) FROM events e WHERE e.task_id=t.id) AS latest,
                            (SELECT max(e.seq) FROM events e WHERE e.task_id=t.id AND e.kind IN
                              ('completed','failed','cancelled','interrupted')) AS finished
                     FROM tasks t WHERE t.bot_id=?) x
               LEFT JOIN chat_read_cursors r ON r.chat_id=x.chat_id""",
            (bot_id,),
        ).fetchone()
        item = {**dict(chat), **dict(row)}
        latest = db.execute(
            """SELECT e.seq,e.created_at,t.id AS task_id,t.chat_id,t.status,
                      substr(CASE WHEN t.status='completed' THEN t.output ELSE t.message END,1,240) AS preview
               FROM events e JOIN tasks t ON t.id=e.task_id WHERE e.seq=?""",
            (item["latest_seq"],),
        ).fetchone()
        item["latest"] = {**dict(latest), "title": item["title"]} if latest else None
        item["unread"] = bool(item["unread"])
        return [item]

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
            chat = self._chat(db, owner, chat_id)
            observed = db.execute(
                """SELECT 1 FROM events e JOIN tasks t ON t.id=e.task_id
                   WHERE e.seq=? AND t.bot_id=? AND e.kind IN
                     ('completed','failed','cancelled','interrupted')""",
                (activity_seq, chat["bot_id"]),
            ).fetchone()
            if not observed:
                raise NotFound("Conversation activity not found.")
            db.execute(
                """INSERT INTO chat_read_cursors SELECT id,? FROM chats WHERE bot_id=? ON CONFLICT(chat_id)
                   DO UPDATE SET event_seq=CASE WHEN chat_read_cursors.event_seq>excluded.event_seq THEN chat_read_cursors.event_seq ELSE excluded.event_seq END""",
                (activity_seq, chat["bot_id"]),
            )
            return {
                "read_seq": db.execute(
                    "SELECT event_seq FROM chat_read_cursors WHERE chat_id=?", (chat_id,)
                ).fetchone()[0]
            }

    def conversation(self, owner: str, chat_id: str) -> dict:
        with self.connect() as db:
            chat = self._chat(db, owner, chat_id)
            chat = dict(
                db.execute(
                    "SELECT * FROM chats WHERE bot_id=? ORDER BY created_at,id LIMIT 1",
                    (chat["bot_id"],),
                ).fetchone()
            )
            chat["messages"] = [
                dict(row)
                for row in db.execute(
                    """SELECT m.* FROM messages m JOIN chats c ON c.id=m.chat_id
                    WHERE c.bot_id=? ORDER BY m.created_at,m.rowid""",
                    (chat["bot_id"],),
                )
            ]
            # The person's own message lists the files they attached to it.
            self._message_file_lists(db, chat["bot_id"], chat["messages"])
            chat["tasks"] = [
                self._task(db, owner, row[0])
                for row in db.execute(
                    "SELECT id FROM tasks WHERE bot_id=? ORDER BY created_at,id", (chat["bot_id"],)
                )
            ]
            self._decorate_tasks(db, chat["bot_id"], chat["tasks"])
            activity = self._chat_activities(db, chat["bot_id"])[0]
            chat["activity_seq"] = activity["activity_seq"]
            chat["unread"] = activity["unread"]
            return chat

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
    def _message_file_lists(cls, db, bot_id: str, messages: list[dict], *, only=None) -> None:
        attached = cls._message_files(db, bot_id, only=only)
        for message in messages:
            if message["role"] == "user":
                message["files"] = attached.get(message["task_id"], [])

    @classmethod
    def _decorate_tasks(cls, db, bot_id: str, tasks: list[dict], *, only=None) -> None:
        """Each task's coding job, file count, short updates, decided actions and the setup card
        the owl proposed in it (proposals.py), as the conversation shows them.

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
                "GROUP BY f.source_task_id",
                (bot_id, *values),
            )
        }
        # The short updates a bot sent while working stay part of that turn's history.
        progress_notes: dict[str, list[str]] = {}
        for row in db.execute(
            "SELECT e.task_id,e.data FROM events e JOIN tasks t ON t.id=e.task_id "
            f"WHERE {scope} AND e.kind='progress_note' ORDER BY e.seq",
            values,
        ):
            text = json.loads(row["data"]).get("text")
            if text:
                progress_notes.setdefault(row["task_id"], []).append(text)
        for task in tasks:
            task["coding_job_id"] = jobs.get(task["id"])
            task["file_count"] = file_counts.get(task["id"], 0)
            notes = progress_notes.get(task["id"], [])
            task["progress_notes"] = notes
            # Apps released before progress_notes show one line: the latest.
            task["progress_note"] = notes[-1] if notes else None
        decided = cls._decided_actions(db, bot_id, only=only)
        from .proposals import setup_cards

        setups = setup_cards(db, bot_id, only=only)
        for task in tasks:
            task["decided_actions"] = decided.get(task["id"], [])
            # Only a turn with a card carries one, so other turns read as before.
            if task["id"] in setups:
                task["setup_proposal"] = setups[task["id"]]
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

    # The messages of an owl's one conversation, in the order the conversation shows them.
    _PAGE_MESSAGES = (
        "SELECT m.rowid AS position,m.id,m.chat_id,m.task_id,m.role,{content},m.created_at "
        "FROM messages m JOIN chats c ON c.id=m.chat_id WHERE c.bot_id=?{where} "
        "ORDER BY m.created_at {order},m.rowid {order}{limit}"
    )
    _OLDER = " AND (m.created_at<? OR (m.created_at=? AND m.rowid<?))"
    _NEWER = " AND (m.created_at>? OR (m.created_at=? AND m.rowid>?))"
    _SINCE = " AND (m.created_at>? OR (m.created_at=? AND m.rowid>=?))"

    @classmethod
    def _page_messages(
        cls, db, bot_id, *, where="", values=(), newest_first=False, limit=None, content=True
    ):
        # Without content: only where the page starts and which turns it holds, for a poll.
        rows = db.execute(
            cls._PAGE_MESSAGES.format(
                content="m.content" if content else "'' AS content",
                where=where,
                order="DESC" if newest_first else "ASC",
                limit=" LIMIT ?" if limit is not None else "",
            ),
            (bot_id, *values, *((limit,) if limit is not None else ())),
        ).fetchall()
        messages = [dict(row) for row in rows]
        return messages[::-1] if newest_first else messages

    @staticmethod
    def _message_key(db, bot_id: str, message_id: str) -> tuple | None:
        row = db.execute(
            "SELECT m.created_at,m.rowid AS position FROM messages m JOIN chats c ON c.id=m.chat_id "
            "WHERE m.id=? AND c.bot_id=?",
            (message_id, bot_id),
        ).fetchone()
        return (row["created_at"], row["created_at"], row["position"]) if row else None

    @staticmethod
    def _key(message: dict) -> tuple:
        return (message["created_at"], message["created_at"], message["position"])

    def _page_tasks(self, db, owner, bot_id, messages, *, since, until, latest, also=()):
        """The tasks a page shows: those its messages belong to, those started within its span
        (a turn with no message yet, like one still being written), every task still working
        (latest page only), and ``also``.
        """
        clauses, values = [], []
        span = []
        if since is not None:
            span.append("created_at>=?")
            values.append(since)
        if until is not None:
            span.append("created_at<?")
            values.append(until)
        clauses.append("(" + " AND ".join(span) + ")" if span else "1=1")
        named = sorted({message["task_id"] for message in messages} | set(also))
        if named:
            clauses.append(f"id IN ({','.join('?' * len(named))})")
            values.extend(named)
        if latest:
            clauses.append("status IN ('queued','running','waiting','cancelling')")
        ids = [
            row[0]
            for row in db.execute(
                f"SELECT id FROM tasks WHERE bot_id=? AND ({' OR '.join(clauses)}) "
                "ORDER BY created_at,id",
                (bot_id, *values),
            )
        ]
        tasks = [self._task(db, owner, task_id) for task_id in ids]
        self._decorate_tasks(db, bot_id, tasks, only=ids)
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
        """One page of the owl's conversation (see ``ConversationPageQuery``).

        The latest page always carries every task still working and every task started since
        its first message, so a message the person just sent finds its task on it. With a
        version, ``continues`` says the messages follow the newest one the client has and the
        tasks are only those that changed; otherwise the page replaces what the client had.
        ``earlier`` is the message to read the page before from, or None at the start.
        """
        with self.connect() as db:
            chat = self._chat(db, owner, chat_id)
            bot_id = chat["bot_id"]
            first = db.execute(
                "SELECT * FROM chats WHERE bot_id=? ORDER BY created_at,id LIMIT 1", (bot_id,)
            ).fetchone()
            result = {key: first[key] for key in ("id", "bot_id", "title", "created_at")}
            activity = self._chat_activities(db, bot_id)[0]
            result.update(activity_seq=activity["activity_seq"], unread=activity["unread"])
            head = f"{activity['activity_seq']}.{int(activity['unread'])}"
            limit = query.limit
            if query.before is not None:
                before = self._message_key(db, bot_id, query.before)
                if before is None:
                    raise NotFound("Message not found.")
                messages = self._page_messages(
                    db, bot_id, where=self._OLDER, values=before, newest_first=True, limit=limit
                )
                messages = self._reach_back(db, owner, bot_id, messages, query, before)
                earlier = self._earlier(db, bot_id, messages)
                tasks = self._page_tasks(
                    db,
                    owner,
                    bot_id,
                    messages,
                    since=messages[0]["created_at"] if messages and earlier else None,
                    until=before[0],
                    latest=False,
                )
                return self._page(db, result, messages, tasks, earlier=earlier, continues=False)
            latest = self._page_messages(
                db, bot_id, newest_first=True, limit=limit, content=query.version is None
            )
            seen, known = self._read_version(query.version)
            new = None
            if query.version is not None:
                after = self._message_key(db, bot_id, query.after) if query.after else None
                if query.after is None or after is not None:
                    new = self._page_messages(
                        db,
                        bot_id,
                        where=self._NEWER if after else "",
                        values=after or (),
                        newest_first=True,
                        limit=limit + 1,
                    )
                    if len(new) > limit:
                        new = None
            if new is None:
                if query.version is not None:
                    latest = self._page_messages(db, bot_id, newest_first=True, limit=limit)
                latest = self._reach_back(db, owner, bot_id, latest, query, None)
            # New messages are the newest, so the latest page holds them too.
            window = latest
            earlier = self._earlier(db, bot_id, latest)
            tasks = self._page_tasks(
                db,
                owner,
                bot_id,
                window,
                since=latest[0]["created_at"] if latest and earlier else None,
                until=None,
                latest=True,
                also=known if new is not None else (),
            )
            prints = {task["id"]: self._fingerprint(task) for task in tasks}
            shown = {message["task_id"] for message in window}
            # A task the client had that left the page keeps its last state there; once it is
            # neither working nor new it doesn't change again, so the version drops it.
            current = [
                task
                for task in tasks
                if task["id"] in shown
                or task["status"] in ACTIVE
                or not earlier
                or task["created_at"] >= latest[0]["created_at"]
            ]
            result["version"] = (
                head + ":" + ",".join(f"{task['id']}.{prints[task['id']]}" for task in current)
            )
            if new is None:
                return self._page(db, result, latest, current, earlier=earlier, continues=False)
            changed = [task for task in tasks if known.get(task["id"]) != prints[task["id"]]]
            if not new and not changed and seen == head:
                return self._page(db, result, [], [], earlier=None, continues=True, unchanged=True)
            return self._page(db, result, new, changed, earlier=None, continues=True)

    def _reach_back(self, db, owner, bot_id, messages, query, before):
        """Extend a page back to the first message of ``query.through_task``."""
        if query.through_task is None:
            return messages
        task = self._task(db, owner, query.through_task)
        if task["bot_id"] != bot_id:
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
            bot_id,
            where=self._SINCE + (self._OLDER if before is not None else ""),
            values=key + (before or ()),
        )

    def _earlier(self, db, bot_id, messages) -> str | None:
        if not messages:
            return None
        older = db.execute(
            "SELECT 1 FROM messages m JOIN chats c ON c.id=m.chat_id WHERE c.bot_id=?"
            + self._OLDER
            + " LIMIT 1",
            (bot_id, *self._key(messages[0])),
        ).fetchone()
        return messages[0]["id"] if older else None

    @classmethod
    def _page(cls, db, result, messages, tasks, *, earlier, continues, unchanged=False) -> dict:
        cls._message_file_lists(
            db, result["bot_id"], messages, only=sorted({m["task_id"] for m in messages})
        )
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
            source = """FROM messages m JOIN chats c ON c.id=m.chat_id JOIN tasks t ON t.id=m.task_id
                   WHERE c.bot_id=? AND (CAST(? AS BIGINT) IS NULL OR m.rowid<?)"""
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
                        "SELECT 1 FROM messages m JOIN chats c ON c.id=m.chat_id WHERE c.bot_id=? "
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
            for row in rows[: data.limit]:
                item = dict(row)
                content = item.pop("content", None)
                if excerpts:
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
        ({kind, site, site_name}) when the owl said what it needs.
        """
        now = time.time()
        with self.connect() as db:
            owls = {
                row["id"]: row
                for row in db.execute(
                    "SELECT id,name,color,background FROM bots WHERE owner=?", (owner,)
                )
            }
            items: list[dict] = []

            def add(kind, row, **extra):
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
                "SELECT r.id,r.bot_id,r.task_id,r.action,r.created_at AS at "
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
            # one the person paused doesn't. A draft is one an owl suggested, not yet turned on.
            for row in db.execute(
                "SELECT s.id,s.bot_id,s.source_task_id AS task_id,s.name,s.state,"
                "s.updated_at AS at,h.pause_code FROM schedules s JOIN bots b ON b.id=s.bot_id "
                "LEFT JOIN tasks t ON t.id=s.source_task_id "
                "LEFT JOIN schedule_health h ON h.schedule_id=s.id AND h.revision=s.revision "
                "WHERE b.owner=? AND ((s.state='paused' AND s.pause_reason IS NOT NULL) OR "
                "(s.state='draft' AND (t.status IS NULL OR t.status!='running')))",
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
            # A routine's own run pauses its routine instead, which is listed above.
            for row in db.execute(
                "SELECT t.id,t.bot_id,t.id AS task_id,t.status,t.message FROM tasks t "
                "JOIN bots b ON b.id=t.bot_id WHERE b.owner=? "
                "AND t.status IN ('failed','interrupted') "
                "AND NOT EXISTS(SELECT 1 FROM tasks n WHERE n.bot_id=t.bot_id AND "
                "(n.created_at>t.created_at OR (n.created_at=t.created_at AND n.id>t.id))) "
                "AND NOT EXISTS(SELECT 1 FROM schedule_runs r WHERE r.task_id=t.id)",
                (owner,),
            ):
                ended = db.execute(
                    "SELECT seq,created_at FROM events WHERE task_id=? AND kind=? "
                    "ORDER BY seq DESC LIMIT 1",
                    (row["id"], row["status"]),
                ).fetchone()
                read = db.execute(
                    "SELECT coalesce(max(r.event_seq),0) FROM chat_read_cursors r "
                    "JOIN chats c ON c.id=r.chat_id WHERE c.bot_id=?",
                    (row["bot_id"],),
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
                """SELECT m.*,t.status AS task_status FROM messages m
                   JOIN chats c ON c.id=m.chat_id JOIN tasks t ON t.id=m.task_id
                   WHERE m.id=? AND c.bot_id=?""",
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
            if (
                task["bot_id"] != bot_id
                or not self.executing(db, task)
                or not bot["memory_enabled"]
            ):
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
            previous = db.execute(
                "SELECT id,bot_id FROM tasks WHERE owner=? AND request_id=?",
                (owner, data.request_id),
            ).fetchone()
            if previous:
                if previous["bot_id"] != bot_id:
                    raise Conflict("This request ID was already used for a different message.")
                return self._task(db, owner, previous["id"])
            if db.execute("SELECT 1 FROM tasks WHERE bot_id=? LIMIT 1", (bot_id,)).fetchone():
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
        with self.connect(write=True) as db:
            return self._enqueue(db, owner, chat_id, data)

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
        if (
            source["status"] != "completed"
            or db.execute(
                "SELECT 1 FROM tasks WHERE chat_id=? AND id<>? AND created_at>=? LIMIT 1",
                (source["chat_id"], source["id"], source["created_at"]),
            ).fetchone()
        ):
            return None
        if (
            db.execute(
                "SELECT count(*) FROM tasks WHERE owner=? AND status IN ('queued','running','waiting','cancelling')",
                (owner,),
            ).fetchone()[0]
            >= 20
        ):
            raise LimitReached("The task queue is full. Wait for tasks to finish.")
        bot = self._bot(db, owner, source["bot_id"])
        task_id, now = uid(), time.time()
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

    def _enqueue(self, db: sqlite3.Connection, owner: str, chat_id: str, data: TaskInput) -> dict:
        chat = self._chat(db, owner, chat_id)
        bot = self._bot(db, owner, chat["bot_id"])
        previous = db.execute(
            "SELECT id,bot_id,message FROM tasks WHERE owner=? AND request_id=?",
            (owner, data.request_id),
        ).fetchone()
        if previous:
            if (
                previous["bot_id"] != bot["id"]
                or previous["message"] != data.message
                or self._attached_file_ids(db, previous["id"]) != data.file_ids
            ):
                raise Conflict("This request ID was already used for a different message.")
            return self._task(db, owner, previous["id"])
        attached = self._attachable_files(db, bot, data.file_ids)
        chat = self._ensure_chat(db, owner, bot["id"], bot["name"])
        chat_id = chat["id"]
        if (
            db.execute(
                "SELECT count(*) FROM tasks WHERE owner=? AND status IN ('queued','running','waiting','cancelling')",
                (owner,),
            ).fetchone()[0]
            >= 20
        ):
            raise LimitReached("The task queue is full. Wait for tasks to finish.")
        task_id, now = uid(), time.time()
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
                encode({**bot, "reply_review_enabled": self._reply_review_enabled(db, owner)}),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO messages(id,chat_id,task_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
            (uid(), chat_id, task_id, "user", data.message, now),
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
        if chat["title"] == "New conversation":
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
    def _message_files(db, bot_id: str, *, only=None) -> dict[str, list[dict]]:
        """Each task's attached files as sent, and whether each is still in the Library."""
        attached: dict[str, list[dict]] = {}
        if only is not None and not only:
            return attached
        scope = "" if only is None else f" AND t.id IN ({','.join('?' * len(only))})"
        for row in db.execute(
            "SELECT a.task_id,a.file_id,a.name,a.media_type,a.size_bytes,"
            "EXISTS(SELECT 1 FROM files f WHERE f.id=a.file_id AND f.bot_id=t.bot_id) AS available "
            f"FROM task_files a JOIN tasks t ON t.id=a.task_id WHERE t.bot_id=?{scope} "
            "ORDER BY a.task_id,a.ordinal",
            (bot_id, *(only or ())),
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

    def claim(self, owner: str) -> dict | None:
        from .hosted_maintenance import MaintenanceLease

        with self.connect(write=True) as db:
            if MaintenanceLease.pending(db):
                return None
            row = db.execute(
                """SELECT t.id FROM tasks t WHERE t.owner=?
                AND NOT EXISTS (SELECT 1 FROM tasks active WHERE active.bot_id=t.bot_id
                    AND active.status IN ('running','cancelling'))
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
            if row is None:
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
    ) -> None:
        """End a task. ``failure`` (failures.py) says why a failed or interrupted turn stopped, for
        the person; ``routine_failure`` says what a failed routine run means for its routine (retry,
        wait for a limit, or pause).
        """
        if status not in TERMINAL:
            raise ValueError("Invalid terminal task state.")
        with self.connect(write=True) as db:
            task = self._task(db, owner, task_id)
            if task["status"] in TERMINAL:
                return
            if task["status"] == "cancelling":
                status, content, error = "cancelled", "", None
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
            self._event(
                db,
                task_id,
                status,
                {
                    **({"error": error} if error else {}),
                    **({"failure": failure} if failure else {}),
                },
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
        """Never replay a task that might have already performed an action."""
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
                state = "cancelled" if row["status"] == "cancelling" else "interrupted"
                reason = "The runtime stopped before this task finished. Review saved results before sending a new task."
                # The service restarted under the task: a service cause, never the person's setup.
                failure = failures.failure(failures.SERVICE) if state == "interrupted" else None
                db.execute(
                    "UPDATE tasks SET status=?,error=?,failure=?,updated_at=? WHERE id=?",
                    (state, reason, encode(failure) if failure else None, time.time(), row["id"]),
                )
                self._event(
                    db,
                    row["id"],
                    state,
                    {"error": reason, **({"failure": failure} if failure else {})},
                )
                if state == "interrupted":
                    self._settle_schedule_run(db, row["id"], state, RunFailure("interrupted"))
            db.execute(
                "UPDATE provider_calls SET status='unknown',updated_at=? WHERE status='started' AND task_id IN (SELECT id FROM tasks WHERE owner=?)",
                (time.time(), owner),
            )
            return len(rows)

    def event(self, owner: str, task_id: str, kind: str, data: dict) -> None:
        with self.connect(write=True) as db:
            self._task(db, owner, task_id)
            self._event(db, task_id, kind, data)

    def reserve_call(
        self,
        owner: str,
        task_id: str,
        model: str,
        max_calls: int | None,
        *,
        background: bool = False,
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
            return self._reserve_call(db, owner, task_id, model, max_calls, background=background)

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
    ) -> str:
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
        cls._event(db, task_id, "model_started", {"model": model})
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
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT memory_id FROM memory_changes WHERE bot_id=? AND action='saved'",
                        (bot_id,),
                    )
                }
                for record in records:
                    record["can_undo"] = (
                        record["status"] == "active"
                        and bool(record["source_task_id"])
                        and record["id"] in saved
                    )
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
            previous = db.execute(
                "SELECT id,task_id,created_at FROM messages WHERE chat_id=? "
                "AND role='assistant' AND task_id!=? AND created_at<=? "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (task["chat_id"], task_id, task["created_at"]),
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
            previous = db.execute(
                "SELECT id,task_id,created_at FROM messages WHERE chat_id=? "
                "AND role='assistant' AND task_id!=? AND created_at<=? "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (task["chat_id"], task_id, task["created_at"]),
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
            else:
                memory_id = uid()
                status = "pending" if source_task_id and not saved_by_owl else "active"
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
                    "saved" if saved_by_owl else "proposed" if source_task_id else "created",
                    None,
                    status,
                    source_task_id,
                    replaces_memory_id,
                )
                if saved_by_owl and target is not None:
                    # The old memory stops being used now and stays only so Undo can restore it.
                    db.execute(
                        "UPDATE memories SET status='superseded',updated_at=? WHERE id=?",
                        (now, target["id"]),
                    )
                    self._memory_change(
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
        """When a replacement goes, Undo no longer applies: erase what it superseded."""
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
                "SELECT 1 FROM memory_changes WHERE bot_id=? AND memory_id=? AND action='saved'",
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
                where += " AND source_task_id=?"
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
        versions = db.execute(
            "SELECT max(revision),count(*) FROM files WHERE bot_id=? AND series_id=?",
            (item["bot_id"], item["series_id"]),
        ).fetchone()
        item.update(is_latest=item["revision"] == versions[0], version_count=versions[1])
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
            raise LimitReached("File exceeds the 50 MiB transfer limit.")
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
            raise LimitReached("File exceeds the 50 MiB transfer limit.")
        return self._save_file(
            owner,
            bot_id,
            FileInput(name=name, content="", previous_file_id=previous_file_id),
            source_task_id=source_task_id,
            payload=payload,
        )

    def save_file(
        self, owner: str, bot_id: str, data: FileInput, *, source_task_id: str | None = None
    ) -> dict:
        return self._save_file(owner, bot_id, data, source_task_id=source_task_id)

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
        self._revision_base(db, bot_id, data.name, data.previous_file_id)
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
            previous = self._revision_base(db, bot_id, data.name, data.previous_file_id)
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
            return self._file_metadata(
                db, db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            )

    @staticmethod
    def _revision_base(db, bot_id, name, previous_file_id):
        current = db.execute(
            "SELECT * FROM files WHERE bot_id=? AND name=? ORDER BY revision DESC LIMIT 1",
            (bot_id, name),
        ).fetchone()
        if previous_file_id:
            if not current or current["id"] != previous_file_id:
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
        return result

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
        first = schedule["next_run_at"] if schedule["state"] == "active" else None
        retry_at = health["retry_at"] if health else None
        if (
            first is not None
            and retry_at is not None
            and health["retry_reason"] == "limit"
            and first < retry_at
            and retry_at > now
        ):
            # Runs wait while a plan or usage limit holds; the first one after it is next.
            first = advance_run(timing, first, retry_at)
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
                    """SELECT r.id,r.scheduled_for,r.task_id,r.reason,r.origin,
                    COALESCE(t.status,r.status) AS status,t.error AS task_error FROM schedule_runs r
                    LEFT JOIN tasks t ON t.id=r.task_id WHERE r.schedule_id=? ORDER BY r.id DESC LIMIT 1""",
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
                        "SELECT id,name,prompt,timing FROM schedules WHERE source_task_id=?",
                        (source_task_id,),
                    ).fetchone()
                    if previous:
                        if (
                            previous["name"] != data.name
                            or previous["prompt"] != data.prompt
                            or json.loads(previous["timing"]) != data.timing.model_dump(mode="json")
                            or self._routine_access(db, previous["id"])["needs"]
                            != (data.needs or RoutineNeeds()).model_dump()
                        ):
                            raise Conflict(
                                "This request already created a different schedule. Read the existing routine before editing it."
                            )
                        return self._schedule(db, bot_id, previous["id"])
            # Without needs, the payload reads as it did before routines had them, so a request
            # resent across an upgrade still matches.
            payload = encode(
                data.model_dump(
                    mode="json",
                    exclude={"request_id"} if data.needs is not None else {"request_id", "needs"},
                )
            )
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
                    "INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    ),
                )
                self._reset_routine_access(db, schedule_id, data.needs, now)
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
            self._set_schedule_state(db, current, state, now)
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
    ) -> dict:
        """The person allows what a routine needs, for that routine only.

        They allow exactly what their app showed (``access.to_allow``), so a routine that
        changed meanwhile is never allowed more than they saw. With turn_on the routine is
        turned on too. Sending the same decision again after it applied changes nothing.
        """
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            self._bot(db, owner, bot_id)
            current = self._schedule(db, bot_id, schedule_id)
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

    def schedule_runs(
        self, owner: str, bot_id: str, schedule_id: str, before: int | None = None
    ) -> dict:
        with self.connect() as db:
            self._bot(db, owner, bot_id)
            self._schedule(db, bot_id, schedule_id)
            rows = db.execute(
                """SELECT r.*,COALESCE(t.status,r.status) AS current_status,t.error AS task_error
                FROM schedule_runs r LEFT JOIN tasks t ON t.id=r.task_id
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
        if status == "completed" and need is None:
            db.execute("DELETE FROM schedule_health WHERE schedule_id=?", (schedule["id"],))
            return
        if status not in ("completed", "failed", "interrupted"):
            return
        failure = failure or RunFailure("interrupted" if status == "interrupted" else "failed")
        failures = health["failures"] if health else 0
        if failure.kind in NEEDS_PERSON:
            reason = None
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
                    db, schedule, failure.kind, now, failures=failures, reason=reason
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
        if task_id is not None:
            # The run uses what was allowed when it was queued: a change takes effect next run.
            allowed = Store._routine_access(db, schedule["id"])["access"]["allowed"]
            if allowed:
                config["access"] = allowed
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
    ) -> str:
        """Queue one run of a routine in its owl's conversation; returns the task ID."""
        chat = self._ensure_chat(db, owner, schedule["bot_id"], schedule["name"])
        task = self._enqueue(
            db,
            owner,
            chat["id"],
            TaskInput(message=schedule["prompt"], request_id="schedule_" + uid()),
        )
        self._event(
            db,
            task["id"],
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
        return task["id"]

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
            task_id = self._start_routine_run(db, owner, schedule, now, now, "manual")
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
        Retries of failed runs come due here too; a scheduled run supersedes them.
        """
        now = time.time() if now is None else now
        queued = 0
        with self.connect(write=True) as db:
            due_rows = db.execute(
                """SELECT s.id,s.bot_id FROM schedules s JOIN bots b ON b.id=s.bot_id
                WHERE b.owner=? AND s.state='active' AND s.next_run_at<=? ORDER BY s.next_run_at,s.id""",
                (owner, now),
            ).fetchall()
            for row in due_rows:
                schedule = self._schedule(db, row["bot_id"], row["id"])
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
                    task_id = self._start_routine_run(db, owner, schedule, run_for, now)
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
            JOIN bots b ON b.id=s.bot_id WHERE b.owner=? AND h.revision=s.revision
            AND s.state IN ('active','ended') AND h.retry_at<=? ORDER BY h.retry_at,s.id""",
            (owner, now),
        ).fetchall()
        for row in rows:
            schedule = self._schedule(db, row["bot_id"], row["id"])
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
            task_id = self._start_routine_run(db, owner, schedule, at, now, "retry")
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
