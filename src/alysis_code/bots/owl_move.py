"""Moving an owl between the person's two places: this computer and Cloud.

Both places run this engine; the desktop app's main process drives a move between them:

1. It reads the owl from the place it is in, a page at a time (``export_page``): a versioned
   stream of records, oldest first within each kind, bounded per page. The owl must not be
   working. ``fingerprint`` sums up everything a move carries, so a change while it moves is seen.
2. The other place takes the pages in order (``OwlMoves.receive``) into a copy that stays out of
   every list. Ids are new there (derived from the move and the original's ids), so a page sent
   again lands on the same rows, and an owl that moves back never meets its old, deleted rows.
3. Library files travel through the ordinary resumable uploads into that copy, in revision order.
4. ``complete`` checks every file by name, revision, size and SHA-256, restores text files as text,
   puts each file back with its chat, and only then shows the copy.
5. The app deletes the original through the ordinary delete path, which refuses if it changed
   (``require_unchanged``) or is working. Only after that is the move done.

What moves: the owl's setup and look, its approval mode and rules, which apps it may use, its
chats with their messages, replies and steps (with the person's reactions to replies, and which
messages they changed or cancelled before the owl started them), memories, working notes (with their history),
routines and file events (they arrive off, so a routine never runs in both places), its
Library, the routines it suggested in its chat with what the person chose (so one they
declined is never offered again there), its playbooks with the versions Undo can bring back,
the check-backs still waiting in its chat (check_backs.py), due when they were due here, and,
where both places take it, its model and each routine's when the other place offers them
(otherwise the copy thinks with that place's own, and a routine with its owl's). What stays:
its computer and sign-ins, run histories,
cards still waiting for an answer, except a suggested routine's, which travels with its draft,
and its rooms: rooms don't move, so it leaves them with its turns there when the original is
deleted (rooms.py). Everything a page carries is untrusted input: kinds, sizes, counts, ids and order are
checked before anything is written, and nothing outside the owl's own owner is ever touched.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from contextlib import closing
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .check_backs import MAX_NOTE_CHARACTERS as MAX_CHECK_BACK_NOTE
from .check_backs import MAX_PENDING as MAX_CHECK_BACKS
from .check_backs import PAGE_KINDS as CHECK_BACK_PAGES
from .check_backs import CheckBackCondition
from .check_backs import first_check as check_back_first_check
from .desktop_connectors import PLUGIN_ID
from .files import MAX_FILE_BYTES
from .memory_capture import reset_moved
from .models import BotLookName, FileInput, Input
from .owl_models import PlaceModels
from .playbooks import MAX_EARLIER_VERSIONS, MAX_PLAYBOOKS, MAX_TOOLS, PlaybookInput, same_name
from .reactions import MAX_NOTE_CHARACTERS
from .routine_access import parse_needs
from .routine_suggestions import fingerprints
from .scheduling import ScheduleProposal
from .store import (
    ACTIVE,
    TERMINAL,
    ChatScope,
    Conflict,
    LimitReached,
    NotFound,
    Store,
    encode,
    uid,
)

FORMAT = "alysis-owl"
VERSION = 1
# What one page carries at most, and one record at most. A page holds whole records only.
PAGE_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
# A page as the app sends it on: its records plus a little wrapping.
MAX_PAGE_REQUEST_BYTES = MAX_RECORD_BYTES + PAGE_BYTES + 65536
# A move nobody finished or cancelled is cleared after a day; its copy was never shown.
EXPIRY_SECONDS = 24 * 3600
MAX_EVENT_DATA_BYTES = 16 * 1024
MAX_OWLS = 30
# A text file holds at most 100,000 characters (FileInput), so at most four bytes each.
MAX_TEXT_FILE_BYTES = 400_000
PAGE_PATH = re.compile(r"^/v1/owl-moves/[a-f0-9]{32}/pages$")
MOVED_PAUSE_CODE = "moved"
MOVED_PAUSE_REASON = (
    "This owl moved here, so this routine is off. Turn it on when you're ready; it runs only here."
)

# Kinds in the order they travel: each refers only to kinds before it. A kind added later goes
# last, and the app sends it only to a place that lists its capability (suggestion:
# routine_suggestions; playbook: owl_playbooks; check_back: check_back, which also keeps a turn's
# config.check_back), since an earlier engine refuses a kind or a field it doesn't know.
SECTIONS = (
    "owl",
    "rule",
    "app",
    "turn",
    "memory",
    "note",
    "archive",
    "routine",
    "trigger",
    "file",
    "suggestion",
    "playbook",
    "check_back",
)
MAX_COUNTS = {
    "owl": 1,
    "rule": 500,
    "app": 500,
    "turn": 100_000,
    "memory": 5000,
    "note": 5000,
    "archive": 5000,
    "routine": 20,
    "trigger": 100,
    "file": 20_000,
    # At most one per turn.
    "suggestion": 100_000,
    "playbook": MAX_PLAYBOOKS,
    # Those waiting in the owl's one chat.
    "check_back": MAX_CHECK_BACKS,
}


class InvalidMove(ValueError):
    """A move's part is not something this engine accepts; nothing from it was written."""


# ---------------------------------------------------------------------------------------------
# Records: what a move carries, validated as untrusted input. Text keeps every character as sent.


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


Time = Annotated[float, Field(ge=0, le=1e11, allow_inf_nan=False)]
ModelId = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
]
SourceId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
Text = Annotated[str, StringConstraints(max_length=1_000_000)]


def _bounded_json(value: Any, limit: int, what: str) -> Any:
    if len(encode(value).encode()) > limit:
        raise ValueError(f"{what} is too large.")
    return value


class ChatPart(_Record):
    title: str = Field(max_length=160)
    created_at: Time


class ThreadPart(ChatPart):
    id: SourceId


class OwlRecord(_Record):
    kind: Literal["owl"]
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(max_length=1000)
    instructions: str = Field(max_length=6000)
    memory_enabled: StrictBool
    files_enabled: StrictBool
    color: BotLookName | None
    background: BotLookName | None
    # Where it sits in the person's list: pinned to the top, or archived (its routines and file
    # events arrive off either way).
    pinned_at: Time | None = None
    archived_at: Time | None = None
    created_at: Time
    chat: ChatPart | None
    threads: list[ThreadPart] = Field(default_factory=list)
    review_mode: Literal["auto", "review"] | None
    # The model it thinks with, only when the app asked (MoveExport.model, capability
    # "owl_move_model"): its id where it was and the model that id names there. Model ids belong
    # to a place, so the copy keeps it only where it is offered (PlaceModels.keep).
    model: ModelId | None = None
    model_name: ModelId | None = None
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=40)


class RuleRecord(_Record):
    kind: Literal["rule"]
    id: SourceId
    effect: Literal["allow", "ask"]
    description: str = Field(min_length=1, max_length=4000)
    action: dict[str, Any] | None
    created_at: Time
    updated_at: Time

    @field_validator("action")
    @classmethod
    def _action(cls, value):
        if value is None:
            return value
        if not isinstance(value.get("tool"), str) or not isinstance(
            value.get("arguments", {}), dict
        ):
            raise ValueError("An approval rule's action names a tool and its arguments.")
        return _bounded_json(value, 16384, "An approval rule's action")


class AppRecord(_Record):
    kind: Literal["app"]
    plugin_id: str = Field(pattern=PLUGIN_ID)
    enabled: StrictBool


class ActivationEvent(_Record):
    file_id: SourceId
    name: str = Field(max_length=160)
    occurred_at: Time | None = None


class MailActivationEvent(_Record):
    message_id: str = Field(max_length=256, pattern=r"^[a-zA-Z0-9_.@-]+$")
    sender: str | None = Field(default=None, max_length=256)
    name: str = Field(max_length=160)
    account: str = Field(max_length=256)
    occurred_at: Time | None = None


class CalendarActivationEvent(_Record):
    calendar_event_id: str = Field(max_length=256, pattern=r"^[a-zA-Z0-9_.@-]+$")
    name: str = Field(max_length=160)
    account: str = Field(max_length=256)
    status: str = Field(max_length=32)
    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=256)
    occurred_at: Time | None = None


class Activation(_Record):
    trigger_id: SourceId | None = None
    revision: StrictInt | None = Field(default=None, ge=1)
    name: str = Field(max_length=100)
    kind: Literal["file_added", "gmail_received", "calendar_changed"] = "file_added"
    mode: str = Field(default="review", max_length=40)
    events: list[ActivationEvent | MailActivationEvent | CalendarActivationEvent] = Field(
        max_length=100
    )


class Regeneration(_Record):
    """Another answer to an earlier turn's message (regenerate.py), by the moved turns' ids."""

    task_id: SourceId
    replaces: SourceId
    source: SourceId


class TurnConfig(_Record):
    """What a past turn's settings keep: its owl as it was then, and what started it."""

    name: str | None = Field(default=None, max_length=80)
    purpose: str | None = Field(default=None, max_length=1000)
    instructions: str | None = Field(default=None, max_length=6000)
    memory_enabled: StrictBool = True
    files_enabled: StrictBool = True
    reply_review_enabled: StrictBool | None = None
    intro: dict[str, str] | None = None
    skipped_question: dict[str, str] | None = None
    app_ready: dict[str, str] | None = None
    activation: Activation | None = None
    regenerate: Regeneration | None = None
    # The check-back that started this turn (check_backs.py), as the turn reads it.
    check_back: dict[str, Any] | None = None

    @field_validator("intro", "skipped_question", "app_ready", "check_back")
    @classmethod
    def _small(cls, value):
        return value if value is None else _bounded_json(value, 8192, "A turn's start")


class ReactionPart(_Record):
    """The person's thumbs up or down on a reply, with their note (reactions.py)."""

    reaction: Literal["up", "down"]
    note: str = Field(max_length=MAX_NOTE_CHARACTERS)
    created_at: Time
    updated_at: Time


class MessagePart(_Record):
    role: Literal["user", "assistant"]
    content: Text
    created_at: Time
    reaction: ReactionPart | None = None

    @model_validator(mode="after")
    def _reaction_on_a_reply(self):
        if self.reaction is not None and self.role != "assistant":
            raise ValueError("Only an owl's reply carries a reaction.")
        return self


class QueuedPart(_Record):
    """A message the person sent in chat, and how they changed it before it started."""

    revision: StrictInt = Field(ge=0, le=1_000_000)
    sent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    withdrawn_at: Time | None


class EventPart(_Record):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    data: dict[str, Any]
    created_at: Time

    @field_validator("data")
    @classmethod
    def _data(cls, value):
        return _bounded_json(value, MAX_EVENT_DATA_BYTES, "A step's details")


class AttachmentPart(_Record):
    file_id: SourceId
    name: str = Field(min_length=1, max_length=160)
    media_type: str = Field(min_length=1, max_length=200)
    size_bytes: StrictInt = Field(ge=0, le=MAX_FILE_BYTES)


class TurnRecord(_Record):
    thread: SourceId | None = None
    kind: Literal["turn"]
    id: SourceId
    request_id: str = Field(min_length=1, max_length=200)
    message: str = Field(max_length=20000)
    status: Literal["completed", "failed", "cancelled", "interrupted"]
    config: TurnConfig
    created_at: Time
    updated_at: Time
    error: str | None = Field(max_length=20000)
    calls: StrictInt = Field(ge=0, le=1_000_000)
    output: Text
    failure: dict[str, Any] | None
    read: StrictBool
    messages: list[MessagePart] = Field(max_length=200)
    events: list[EventPart] = Field(max_length=5000)
    files: list[AttachmentPart] = Field(max_length=10)
    queued: QueuedPart | None = None

    @model_validator(mode="after")
    def _withdrawn_never_started(self):
        if self.queued is not None and self.queued.withdrawn_at is not None:
            if self.status != "cancelled" or any(m.role == "user" for m in self.messages):
                raise ValueError("A cancelled message leaves its turn cancelled and without it.")
        return self

    @field_validator("failure")
    @classmethod
    def _failure(cls, value):
        if value is None:
            return value
        if not isinstance(value.get("kind"), str):
            raise ValueError("A failure names its kind.")
        return _bounded_json(value, 4096, "A turn's failure")


class ReplacesPart(_Record):
    id: SourceId
    expected_updated_at: Time


class MemoryRecord(_Record):
    kind: Literal["memory"]
    id: SourceId
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2000)
    status: Literal["active", "pending"]
    source_task_id: SourceId | None
    replaces: ReplacesPart | None
    created_at: Time
    updated_at: Time


class NoteRevisionPart(_Record):
    revision: StrictInt = Field(ge=1, le=100_000)
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    source_task_id: SourceId | None
    created_at: Time


class NoteRecord(_Record):
    kind: Literal["note"]
    id: SourceId
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    revision: StrictInt = Field(ge=1, le=100_000)
    source_task_id: SourceId | None
    created_at: Time
    updated_at: Time
    revisions: list[NoteRevisionPart] = Field(max_length=1000)

    @model_validator(mode="after")
    def _history(self):
        numbers = [item.revision for item in self.revisions]
        if len(set(numbers)) != len(numbers) or any(n > self.revision for n in numbers):
            raise ValueError("A note's history repeats or runs past its current revision.")
        return self


class ArchiveRecord(_Record):
    kind: Literal["archive"]
    source_note_id: SourceId
    source_revision: StrictInt = Field(ge=1, le=100_000)
    target_note_id: SourceId
    target_revision: StrictInt = Field(ge=1, le=100_000)
    reason: str = Field(min_length=1, max_length=1000)
    source_task_id: SourceId | None
    created_at: Time


class RoutineRecord(_Record):
    kind: Literal["routine"]
    id: SourceId
    name: str
    prompt: str
    timing: dict[str, Any]
    needs: dict[str, Any] | None
    state: Literal["draft", "active", "paused", "ended"]
    pause_reason: str | None = Field(max_length=2000)
    source_task_id: SourceId | None
    created_at: Time
    updated_at: Time
    # The model its runs think with, only when the app asked (MoveExport.routine_models,
    # capability "routine_models"): its id where it was and the model that id names there. The
    # copy keeps it only where it is offered (PlaceModels.keep); otherwise it thinks with its owl's.
    model: ModelId | None = None
    model_name: ModelId | None = None

    @model_validator(mode="after")
    def _routine(self):
        # The same checks as a routine the person or the owl saves here.
        proposal = ScheduleProposal.model_validate(
            {
                "name": self.name,
                "prompt": self.prompt,
                "timing": self.timing,
                **({"needs": self.needs} if self.needs is not None else {}),
            }
        )
        self.timing = proposal.timing.model_dump(mode="json")
        self.needs = proposal.needs.model_dump() if proposal.needs is not None else None
        return self


class TriggerRecord(_Record):
    kind: Literal["trigger"]
    id: SourceId
    name: str = Field(min_length=1, max_length=100)
    trigger_kind: Literal["file_added", "gmail_received", "calendar_changed"]
    gmail_sender: str | None = Field(
        default=None, max_length=254, pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$"
    )
    instructions: str = Field(min_length=1, max_length=4000)
    state: Literal["draft", "active", "paused"]
    source_task_id: SourceId | None
    last_run_at: Time | None
    created_at: Time
    updated_at: Time


class FileRecord(_Record):
    kind: Literal["file"]
    id: SourceId
    name: str
    revision: StrictInt = Field(ge=1, le=100_000)
    size_bytes: StrictInt = Field(ge=0, le=MAX_FILE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    binary: StrictBool
    media_type: str = Field(min_length=1, max_length=200)
    source_task_id: SourceId | None
    withdrawn_from_reply: StrictBool = False
    created_at: Time
    updated_at: Time

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        # Names are labels, never paths: the same rule as every Library file.
        return FileInput(name=value, content="").name

    @model_validator(mode="after")
    def _text(self):
        if not self.binary and self.size_bytes > MAX_TEXT_FILE_BYTES:
            raise ValueError("A text file is larger than text files can be.")
        return self


class SuggestionRecord(_Record):
    """A routine the owl suggested under a reply (routine_suggestions.py), and what became of it."""

    kind: Literal["suggestion"]
    id: SourceId
    source_task_id: SourceId
    # Its routine, when it still has one: the draft while open, the routine once turned on.
    routine_id: SourceId | None
    name: str
    prompt: str
    timing: dict[str, Any]
    needs: dict[str, Any]
    status: Literal["open", "on", "declined", "closed"]
    revision: StrictInt = Field(ge=1, le=1_000_000)
    created_at: Time
    updated_at: Time
    decided_at: Time | None

    @model_validator(mode="after")
    def _routine(self):
        # The same checks as a routine the owl suggests here.
        proposal = ScheduleProposal.model_validate(
            {"name": self.name, "prompt": self.prompt, "timing": self.timing, "needs": self.needs}
        )
        self.timing = proposal.timing.model_dump(mode="json")
        self.needs = proposal.needs.model_dump()
        return self


ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]


class PlaybookVersionPart(_Record):
    version: StrictInt = Field(ge=1, le=1_000_000)
    current: StrictBool
    name: str
    when_to_use: str
    steps: list[str]
    pitfalls: list[str]
    done_check: str
    tools: list[ToolName] = Field(max_length=MAX_TOOLS)
    source_task_id: SourceId | None
    created_at: Time
    updated_at: Time

    @model_validator(mode="after")
    def _playbook(self):
        # The same checks as a playbook the owl writes here.
        checked = PlaybookInput.model_validate(
            {
                "name": self.name,
                "when_to_use": self.when_to_use,
                "steps": self.steps,
                "pitfalls": self.pitfalls,
                "done_check": self.done_check,
            }
        )
        self.name, self.when_to_use = checked.name, checked.when_to_use
        self.steps, self.pitfalls = checked.steps, checked.pitfalls
        self.done_check = checked.done_check
        return self


class CheckBackRecord(_Record):
    """A check-back still waiting in the owl's chat (check_backs.py)."""

    kind: Literal["check_back"]
    id: SourceId
    source_task_id: SourceId | None
    due_at: Time
    note: str = Field(min_length=1, max_length=MAX_CHECK_BACK_NOTE)
    condition: dict[str, Any] | None
    created_at: Time

    @field_validator("condition")
    @classmethod
    def _condition(cls, value):
        if value is None:
            return value
        # The same checks as one the owl sets here.
        condition = CheckBackCondition.model_validate(value)
        if condition.kind in CHECK_BACK_PAGES:
            from .web_lookup import public_page_address

            try:
                public_page_address(condition.url or "")
            except Conflict as exc:
                raise ValueError(str(exc)) from None
        return condition.model_dump(exclude_none=True)


class PlaybookRecord(_Record):
    """A playbook the owl kept (playbooks.py): its current version and those Undo brings back."""

    kind: Literal["playbook"]
    id: SourceId
    versions: list[PlaybookVersionPart] = Field(min_length=1, max_length=MAX_EARLIER_VERSIONS + 1)

    @model_validator(mode="after")
    def _one_current(self):
        numbers = [part.version for part in self.versions]
        current = [part for part in self.versions if part.current]
        if len(set(numbers)) != len(numbers):
            raise ValueError("A playbook's versions repeat.")
        if len(current) != 1 or current[0].version != max(numbers):
            raise ValueError("A playbook has one current version, its newest.")
        return self

    @property
    def current(self) -> PlaybookVersionPart:
        return next(part for part in self.versions if part.current)


RECORD = TypeAdapter(
    Annotated[
        OwlRecord
        | RuleRecord
        | AppRecord
        | TurnRecord
        | MemoryRecord
        | NoteRecord
        | ArchiveRecord
        | RoutineRecord
        | TriggerRecord
        | FileRecord
        | SuggestionRecord
        | PlaybookRecord
        | CheckBackRecord,
        Field(discriminator="kind"),
    ]
)


class MoveBegin(Input):
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    source_bot_id: SourceId
    format: Literal["alysis-owl"] = FORMAT
    version: Literal[1] = VERSION


class MovePage(Input):
    page: int = Field(ge=0, le=1_000_000, strict=True)
    last: StrictBool
    records: list[dict[str, Any]] = Field(max_length=20000)


class MoveExport(Input):
    threads: StrictBool = False
    cursor: str | None = Field(default=None, min_length=1, max_length=1000)
    # Carry the owl's model in its owl record: only for a place that takes it ("owl_move_model").
    model: StrictBool = False
    reasoning_effort: StrictBool = False
    # Carry each routine's model in its routine record: only for a place that takes it
    # ("routine_models").
    routine_models: StrictBool = False


class MoveFinish(Input):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def page_path(path: str) -> bool:
    """A request carrying a move's page, which may be larger than other requests."""
    return bool(PAGE_PATH.fullmatch(path))


# ---------------------------------------------------------------------------------------------
# The place the owl is in: what a move would carry, page by page.


def _working(db, bot_id: str) -> bool:
    return bool(
        db.execute(
            f"SELECT 1 FROM tasks WHERE bot_id=? AND status IN {ACTIVE} LIMIT 1", (bot_id,)
        ).fetchone()
    )


def fingerprint(db, bot_id: str) -> str:
    """Everything a move carries, summed up: any change to the owl changes it.

    Reading the owl (opening its chat) leaves it as it is.
    """
    parts: list[Any] = []
    for sql in (
        "SELECT name,purpose,instructions,memory_enabled,files_enabled,color,background,"
        "pinned_at,archived_at,updated_at,model,reasoning_effort FROM bots WHERE id=?",
        "SELECT count(*),max(updated_at) FROM tasks WHERE bot_id=?",
        "SELECT count(*),max(c.created_at) FROM chats c JOIN owl_chat_threads x ON x.chat_id=c.id WHERE c.bot_id=?",
        "SELECT max(e.seq) FROM events e JOIN tasks t ON t.id=e.task_id WHERE t.bot_id=?",
        "SELECT count(*),max(updated_at) FROM memories WHERE bot_id=?",
        "SELECT count(*),max(updated_at),sum(revision) FROM working_notes WHERE bot_id=?",
        "SELECT count(*),max(c.created_at),max(c.restored_at) FROM note_consolidations c "
        "JOIN working_notes n ON n.id=c.source_note_id WHERE n.bot_id=?",
        "SELECT count(*),max(created_at),sum(revision) FROM files WHERE bot_id=?",
        "SELECT count(*),max(updated_at),sum(revision) FROM schedules WHERE bot_id=?",
        "SELECT count(*),max(updated_at),sum(revision) FROM bot_triggers WHERE bot_id=?",
        "SELECT count(*),max(updated_at),sum(enabled) FROM bot_plugin_grants WHERE bot_id=?",
        "SELECT count(*),max(updated_at),sum(revision) FROM bot_action_rules "
        "WHERE bot_id=? AND deleted=0",
        "SELECT mode FROM bot_review_settings WHERE bot_id=?",
        "SELECT count(*),max(updated_at) FROM reply_reactions WHERE bot_id=?",
        "SELECT count(*),max(q.updated_at),sum(q.revision) FROM queued_messages q "
        "JOIN tasks t ON t.id=q.task_id WHERE t.bot_id=?",
        "SELECT count(*),max(updated_at),sum(revision) FROM routine_suggestions WHERE bot_id=?",
        "SELECT count(*),max(updated_at),sum(version) FROM playbooks "
        "WHERE bot_id=? AND status<>'held'",
        # Checks of a condition change nothing a move carries; setting or cancelling one does.
        "SELECT count(*),max(created_at),sum(due_at) FROM check_backs "
        "WHERE bot_id=? AND state='pending'",
    ):
        row = db.execute(sql, (bot_id,)).fetchone()
        parts.append([None if v is None else repr(v) for v in tuple(row)] if row else None)
    withdrawn = db.execute(
        "SELECT count(*) FROM owl_withdrawn_results w JOIN files f ON f.id=w.file_id WHERE f.bot_id=?",
        (bot_id,),
    ).fetchone()[0]
    if withdrawn:
        parts.append(["withdrawn_results", withdrawn])
    return hashlib.sha256(encode(parts).encode()).hexdigest()


def require_unchanged(db, bot_id: str, expected: str) -> None:
    """Refuse when the owl worked or changed since the move read it (the delete path's check)."""
    if _working(db, bot_id):
        raise Conflict("This owl started working while it moved, so it stayed where it was.")
    if fingerprint(db, bot_id) != expected:
        raise Conflict("This owl changed while it moved, so it stayed where it was. Try again.")


def summary(store: Store, owner: str, bot_id: str) -> dict:
    """What moving this owl would carry, and whether it can move now."""
    with store.connect() as db:
        store._bot(db, owner, bot_id)
        return _summary(db, bot_id)


def _summary(db, bot_id: str) -> dict:
    files = db.execute(
        "SELECT count(*),COALESCE(sum(COALESCE(o.size_bytes,length(p.data),byte_length(f.content))),0) "
        "FROM files f LEFT JOIN file_payloads p ON f.id=p.file_id "
        "LEFT JOIN file_objects o ON o.file_id=f.id WHERE f.bot_id=?",
        (bot_id,),
    ).fetchone()
    return {
        "format": FORMAT,
        "version": VERSION,
        "fingerprint": fingerprint(db, bot_id),
        "working": _working(db, bot_id),
        "turns": int(
            db.execute(
                f"SELECT count(*) FROM tasks t WHERE {ChatScope(bot_id).tasks}", (bot_id,)
            ).fetchone()[0]
        ),
        "files": int(files[0]),
        "file_bytes": int(files[1]),
    }


def _cursor(value: str | None) -> tuple[str, int, tuple[float, str] | None] | None:
    """Where the next page starts: the owl's fingerprint, the kind, and the last record sent."""
    if value is None:
        return None
    try:
        mark, section, after = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if (
            not isinstance(mark, str)
            or type(section) is not int
            or not 0 <= section < len(SECTIONS)
        ):
            raise ValueError
        if after is not None:
            at, key = after
            if not isinstance(at, int | float) or not isinstance(key, str):
                raise ValueError
            after = (float(at), key)
        return mark, section, after
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise InvalidMove("This page marker isn't one this owl gave out.") from None


def _encode_cursor(mark: str, section: int, after: tuple[float, str] | None) -> str:
    return base64.urlsafe_b64encode(encode([mark, section, after]).encode()).decode("ascii")


_AFTER = " AND (x.created_at>? OR (x.created_at=? AND x.id>?))"


def _keyset(after):
    return (_AFTER, (after[0], after[0], after[1])) if after else ("", ())


def _rows(db, sql: str, bot_id: str, after, limit: int):
    clause, values = _keyset(after)
    return db.execute(
        sql.format(after=clause) + " ORDER BY x.created_at,x.id LIMIT ?",
        (bot_id, *values, limit),
    ).fetchall()


def _owl_records(db, bot_id, after, limit):
    if after:
        return []
    bot = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    chat = Store._own_chat(db, bot_id)
    mode = db.execute("SELECT mode FROM bot_review_settings WHERE bot_id=?", (bot_id,)).fetchone()
    record = {
        "kind": "owl",
        "name": bot["name"],
        "purpose": bot["purpose"],
        "instructions": bot["instructions"],
        "memory_enabled": bool(bot["memory_enabled"]),
        "files_enabled": bool(bot["files_enabled"]),
        "color": bot["color"],
        "background": bot["background"],
        "pinned_at": bot["pinned_at"],
        "archived_at": bot["archived_at"],
        "created_at": bot["created_at"],
        "chat": {"title": chat["title"][:160], "created_at": chat["created_at"]} if chat else None,
        "review_mode": mode["mode"] if mode and mode["mode"] in ("auto", "review") else None,
    }
    return [((0.0, ""), record)]


def _rule_records(db, bot_id, after, limit):
    return [
        (
            (row["created_at"], row["id"]),
            {
                "kind": "rule",
                "id": row["id"],
                "effect": row["effect"],
                "description": row["description"],
                "action": json.loads(row["action"]) if row["action"] else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )
        for row in _rows(
            db,
            "SELECT x.* FROM bot_action_rules x WHERE x.bot_id=? AND x.deleted=0{after}",
            bot_id,
            after,
            limit,
        )
    ]


def _app_records(db, bot_id, after, limit):
    # Grants have no time of their own worth ordering by: their app's id orders them.
    rows = db.execute(
        "SELECT plugin_id,enabled FROM bot_plugin_grants WHERE bot_id=?"
        + (" AND plugin_id>?" if after else "")
        + " ORDER BY plugin_id LIMIT ?",
        (bot_id, *((after[1],) if after else ()), limit),
    ).fetchall()
    return [
        (
            (0.0, row["plugin_id"]),
            {"kind": "app", "plugin_id": row["plugin_id"], "enabled": bool(row["enabled"])},
        )
        for row in rows
    ]


_CONFIG_TEXT = ("name", "purpose", "instructions")
_CONFIG_STARTS = ("intro", "skipped_question", "app_ready")


def _turn_config(config: dict) -> dict:
    """The parts of a past turn's settings worth keeping, in the shape ``TurnConfig`` takes."""
    kept: dict[str, Any] = {}
    for key in _CONFIG_TEXT:
        if isinstance(config.get(key), str):
            kept[key] = config[key]
    for key in ("memory_enabled", "files_enabled", "reply_review_enabled"):
        if isinstance(config.get(key), bool):
            kept[key] = config[key]
    for key in _CONFIG_STARTS:
        value = config.get(key)
        if isinstance(value, dict):
            kept[key] = {k: v for k, v in value.items() if isinstance(v, str)}
    # What brought a turn a check-back started, so it reads the same in the new place.
    check_back = config.get("check_back")
    if isinstance(check_back, dict):
        kept["check_back"] = check_back
    activation = config.get("activation")
    if isinstance(activation, dict):
        kept["activation"] = {
            **{
                key: activation[key]
                for key in ("trigger_id", "name", "kind", "mode")
                if isinstance(activation.get(key), str)
            },
            **(
                {"revision": activation["revision"]}
                if type(activation.get("revision")) is int
                else {}
            ),
            "events": [
                {
                    **{
                        key: value
                        for key, value in event.items()
                        if key
                        in {
                            "file_id",
                            "message_id",
                            "calendar_event_id",
                            "name",
                            "sender",
                            "account",
                            "status",
                            "start",
                            "end",
                            "location",
                            "occurred_at",
                        }
                    },
                    "name": event.get("name") if isinstance(event.get("name"), str) else "",
                }
                for event in activation.get("events") or []
                if isinstance(event, dict)
                and any(
                    isinstance(event.get(field), str)
                    for field in ("file_id", "message_id", "calendar_event_id")
                )
            ],
        }
        kept["activation"].setdefault("name", "")
    # Which earlier answer this turn replaces, so the chat and the owl's history read it the same
    # way in the new place.
    regenerate = config.get("regenerate")
    if isinstance(regenerate, dict) and all(
        isinstance(regenerate.get(key), str) for key in ("task_id", "replaces", "source")
    ):
        kept["regenerate"] = {key: regenerate[key] for key in ("task_id", "replaces", "source")}
    return kept


def _message_parts(db, task_id: str) -> list[dict]:
    """A turn's messages in order, each reply with the person's reaction to it."""
    reactions = {
        row["message_id"]: {
            "reaction": row["reaction"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in db.execute("SELECT * FROM reply_reactions WHERE task_id=?", (task_id,))
    }
    parts = []
    for message in db.execute(
        "SELECT id,role,content,created_at FROM messages WHERE task_id=? "
        "AND role IN ('user','assistant') ORDER BY created_at,rowid",
        (task_id,),
    ):
        part = {key: message[key] for key in ("role", "content", "created_at")}
        if message["role"] == "assistant" and message["id"] in reactions:
            part["reaction"] = reactions[message["id"]]
        parts.append(part)
    return parts


def _turn_records(db, bot_id, after, limit):
    read = db.execute(
        "SELECT coalesce(max(r.event_seq),0) FROM chat_read_cursors r "
        f"JOIN chats c ON c.id=r.chat_id WHERE {ChatScope(bot_id, default_chat=True).chats}",
        (bot_id,),
    ).fetchone()[0]
    records = []
    # Its own chat's turns. Rooms don't move: its turns there stay with the room, and leave it
    # with the original (rooms.py).
    for row in _rows(
        db,
        "SELECT x.* FROM tasks x WHERE x.bot_id=? AND x.chat_id NOT IN (SELECT id FROM rooms){after}",
        bot_id,
        after,
        limit,
    ):
        finished = db.execute(
            f"SELECT max(seq) FROM events WHERE task_id=? AND kind IN {TERMINAL}", (row["id"],)
        ).fetchone()[0]
        events = []
        for event in db.execute(
            "SELECT kind,data,created_at FROM events WHERE task_id=? ORDER BY seq", (row["id"],)
        ):
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", event["kind"]):
                continue
            data = json.loads(event["data"])
            if not isinstance(data, dict) or len(event["data"].encode()) > MAX_EVENT_DATA_BYTES:
                # A step's details too large to carry: the step itself still shows.
                data = {}
            events.append({"kind": event["kind"], "data": data, "created_at": event["created_at"]})
        record = {
            "kind": "turn",
            "id": row["id"],
            "request_id": row["request_id"],
            "message": row["message"],
            "status": row["status"],
            "config": _turn_config(json.loads(row["config"])),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
            "calls": row["calls"],
            "output": row["output"],
            "failure": json.loads(row["failure"]) if row["failure"] else None,
            "read": finished is None or finished <= read,
            "messages": _message_parts(db, row["id"]),
            "events": events,
            "files": [
                dict(file)
                for file in db.execute(
                    "SELECT file_id,name,media_type,size_bytes FROM task_files WHERE task_id=? "
                    "ORDER BY ordinal",
                    (row["id"],),
                )
            ],
        }
        if Store._is_thread(db, row["chat_id"]):
            record["thread"] = row["chat_id"]
            local_read = db.execute(
                "SELECT event_seq FROM chat_read_cursors WHERE chat_id=?", (row["chat_id"],)
            ).fetchone()
            record["read"] = finished is None or finished <= (local_read[0] if local_read else 0)
        queued = db.execute(
            "SELECT revision,sent_sha256,withdrawn_at FROM queued_messages WHERE task_id=?",
            (row["id"],),
        ).fetchone()
        if queued is not None:
            record["queued"] = dict(queued)
        if len(encode(record).encode()) > MAX_RECORD_BYTES:
            # Keep the turn whole without the details of its steps.
            record["events"] = [
                {**event, "data": {}}
                for event in events
                if event["kind"] in ("progress_note", *TERMINAL)
            ]
        records.append(((row["created_at"], row["id"]), record))
    return records


def _memory_records(db, bot_id, after, limit):
    return [
        (
            (row["created_at"], row["id"]),
            {
                "kind": "memory",
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "status": row["status"],
                "source_task_id": row["source_task_id"],
                # A memory waiting for review may replace another once the person accepts it.
                "replaces": {
                    "id": row["target_id"],
                    "expected_updated_at": row["expected_updated_at"],
                }
                if row["target_id"] and row["status"] == "pending"
                else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )
        for row in _rows(
            db,
            "SELECT x.*,r.target_id,r.expected_updated_at FROM memories x "
            "LEFT JOIN memory_replacements r ON r.proposal_id=x.id "
            "WHERE x.bot_id=? AND x.status IN ('active','pending'){after}",
            bot_id,
            after,
            limit,
        )
    ]


def _note_records(db, bot_id, after, limit):
    return [
        (
            (row["created_at"], row["id"]),
            {
                "kind": "note",
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "revision": row["revision"],
                "source_task_id": row["source_task_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "revisions": [
                    dict(revision)
                    for revision in db.execute(
                        "SELECT revision,title,content,source_task_id,created_at "
                        "FROM note_revisions WHERE note_id=? ORDER BY revision",
                        (row["id"],),
                    )
                ],
            },
        )
        for row in _rows(
            db, "SELECT x.* FROM working_notes x WHERE x.bot_id=?{after}", bot_id, after, limit
        )
    ]


def _archive_records(db, bot_id, after, limit):
    # Only notes still archived; one restored reads as an ordinary note.
    return [
        (
            (row["created_at"], row["id"]),
            {
                "kind": "archive",
                "source_note_id": row["id"],
                "source_revision": row["source_revision"],
                "target_note_id": row["target_note_id"],
                "target_revision": row["target_revision"],
                "reason": row["reason"],
                "source_task_id": row["source_task_id"],
                "created_at": row["created_at"],
            },
        )
        for row in _rows(
            db,
            "SELECT * FROM (SELECT c.source_note_id AS id,c.source_revision,c.target_note_id,"
            "c.target_revision,c.reason,c.source_task_id,c.created_at FROM note_consolidations c "
            "JOIN working_notes n ON n.id=c.source_note_id "
            "WHERE n.bot_id=? AND c.restored_at IS NULL) x WHERE 1=1{after}",
            bot_id,
            after,
            limit,
        )
    ]


def _routine_records(db, bot_id, after, limit):
    records = []
    for row in _rows(
        db, "SELECT x.* FROM schedules x WHERE x.bot_id=?{after}", bot_id, after, limit
    ):
        access = db.execute(
            "SELECT needs FROM routine_access WHERE schedule_id=?", (row["id"],)
        ).fetchone()
        needs = parse_needs(access["needs"]) if access else None
        records.append(
            (
                (row["created_at"], row["id"]),
                {
                    "kind": "routine",
                    "id": row["id"],
                    "name": row["name"],
                    "prompt": row["prompt"],
                    "timing": json.loads(row["timing"]),
                    "needs": needs.model_dump() if needs else None,
                    "state": row["state"],
                    "pause_reason": row["pause_reason"],
                    "source_task_id": row["source_task_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    # Carried only when the app asks (export_page); never sent otherwise.
                    "model": row["model"],
                },
            )
        )
    return records


def _trigger_records(db, bot_id, after, limit):
    return [
        (
            (row["created_at"], row["id"]),
            {
                "kind": "trigger",
                "id": row["id"],
                "name": row["name"],
                "trigger_kind": row["kind"],
                **(
                    {"gmail_sender": row["gmail_sender"]} if row["kind"] == "gmail_received" else {}
                ),
                "instructions": row["instructions"],
                "state": row["state"],
                "source_task_id": row["source_task_id"],
                "last_run_at": row["last_run_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )
        for row in _rows(
            db,
            "SELECT x.*,w.sender AS gmail_sender FROM bot_triggers x LEFT JOIN app_event_watches w "
            "ON w.trigger_id=x.id WHERE x.bot_id=?{after}",
            bot_id,
            after,
            limit,
        )
    ]


def _file_records(db, bot_id, after, limit):
    records = []
    for row in _rows(db, "SELECT x.* FROM files x WHERE x.bot_id=?{after}", bot_id, after, limit):
        meta = Store._file_metadata(db, row)
        records.append(
            (
                (row["created_at"], row["id"]),
                {
                    "kind": "file",
                    "id": row["id"],
                    "name": row["name"],
                    "revision": row["revision"],
                    "size_bytes": meta["size_bytes"],
                    "sha256": meta["sha256"],
                    "binary": bool(meta["binary"]),
                    "media_type": meta["media_type"],
                    **({"withdrawn_from_reply": True} if meta.get("withdrawn_from_reply") else {}),
                    "source_task_id": row["source_task_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                },
            )
        )
    return records


def _suggestion_records(db, bot_id, after, limit):
    return [
        (
            (row["created_at"], row["id"]),
            {
                "kind": "suggestion",
                "id": row["id"],
                "source_task_id": row["source_task_id"],
                "routine_id": row["routine_id"],
                "name": row["name"],
                "prompt": row["prompt"],
                "timing": json.loads(row["timing"]),
                "needs": json.loads(row["needs"]),
                "status": row["status"],
                "revision": row["revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "decided_at": row["decided_at"],
            },
        )
        for row in _rows(
            db,
            # A routine the person removed since is no longer this suggestion's.
            "SELECT x.*,s.id AS routine_id FROM routine_suggestions x "
            "LEFT JOIN schedules s ON s.id=x.schedule_id AND s.bot_id=x.bot_id "
            "WHERE x.bot_id=?{after}",
            bot_id,
            after,
            limit,
        )
    ]


def _playbook_records(db, bot_id, after, limit):
    records = []
    for row in _rows(
        db,
        "SELECT x.* FROM (SELECT playbook_id AS id,created_at FROM playbooks "
        "WHERE bot_id=? AND status='current') x WHERE 1=1{after}",
        bot_id,
        after,
        limit,
    ):
        versions = [
            {
                "version": version["version"],
                "current": version["status"] == "current",
                "name": version["name"],
                "when_to_use": version["when_to_use"],
                "steps": json.loads(version["steps"]),
                "pitfalls": json.loads(version["pitfalls"]),
                "done_check": version["done_check"],
                "tools": json.loads(version["tools"]),
                "source_task_id": version["source_task_id"],
                "created_at": version["created_at"],
                "updated_at": version["updated_at"],
            }
            for version in db.execute(
                "SELECT * FROM playbooks WHERE playbook_id=? AND bot_id=? "
                "AND status IN ('current','earlier') ORDER BY version",
                (row["id"], bot_id),
            )
        ]
        records.append(
            (
                (row["created_at"], row["id"]),
                {"kind": "playbook", "id": row["id"], "versions": versions},
            )
        )
    return records


def _check_back_records(db, bot_id, after, limit):
    return [
        (
            (row["created_at"], row["id"]),
            {
                "kind": "check_back",
                "id": row["id"],
                "source_task_id": row["source_task_id"],
                "due_at": row["due_at"],
                "note": row["note"],
                "condition": json.loads(row["condition"]) if row["condition"] else None,
                "created_at": row["created_at"],
            },
        )
        for row in _rows(
            db,
            "SELECT x.* FROM check_backs x WHERE x.bot_id=? AND x.state='pending'{after}",
            bot_id,
            after,
            limit,
        )
    ]


_EXPORTERS = (
    _owl_records,
    _rule_records,
    _app_records,
    _turn_records,
    _memory_records,
    _note_records,
    _archive_records,
    _routine_records,
    _trigger_records,
    _file_records,
    _suggestion_records,
    _playbook_records,
    _check_back_records,
)


def _routine_model(record: dict, carry: bool, models: PlaceModels | None) -> dict:
    """A routine record as a move carries it: with its model (its id here and the model that
    names here) only when the app asked and the routine has one."""
    record = dict(record)
    model = record.pop("model", None)
    if carry and model:
        record["model"] = model
        record["model_name"] = models.model_of(model) if models is not None else None
    return record


def export_page(
    store: Store, owner: str, bot_id: str, query: MoveExport, *, models: PlaceModels | None = None
) -> dict:
    """One page of the owl as a move carries it, from ``query.cursor`` (None: the start).

    The next page's cursor is None after the last. An owl that works, or that changed since the
    first page, is refused: what was read so far would no longer be the whole owl. With
    ``query.model`` its owl record carries its model, and the model that names here (``models``);
    with ``query.routine_models`` each routine record its routine's the same way.
    """
    position = _cursor(query.cursor)
    with store.connect() as db:
        store._bot(db, owner, bot_id)
        if _working(db, bot_id):
            raise Conflict("Stop this owl's work before moving it.")
        mark = fingerprint(db, bot_id)
        if position is not None and position[0] != mark:
            raise Conflict("This owl changed while it moved, so it stayed where it was. Try again.")
        section, after = (0, None) if position is None else position[1:]
        records: list[dict] = []
        size = 0
        next_cursor = None
        carried = {}
        threads = db.execute(
            "SELECT c.id,c.title,c.created_at FROM chats c JOIN owl_chat_threads x ON x.chat_id=c.id WHERE x.bot_id=? "
            "ORDER BY c.created_at,c.id",
            (bot_id,),
        ).fetchall()
        if threads and not query.threads:
            raise Conflict("Update both places before moving an owl with separate conversations.")
        if query.model:
            row = db.execute(
                "SELECT model,reasoning_effort FROM bots WHERE id=?", (bot_id,)
            ).fetchone()
            model = row["model"]
            name = models.model_of(model) if model and models is not None else None
            carried = {"model": model, "model_name": name}
            if query.reasoning_effort and row["reasoning_effort"]:
                carried["reasoning_effort"] = row["reasoning_effort"]
        while section < len(SECTIONS):
            batch = _EXPORTERS[section](db, bot_id, after, 32)
            for key, record in batch:
                if record["kind"] == "owl":
                    record = {**record, **carried}
                    if threads:
                        record["threads"] = [dict(row) for row in threads]
                elif record["kind"] == "routine":
                    record = _routine_model(record, query.routine_models, models)
                record_size = len(encode(record).encode())
                if record_size > MAX_RECORD_BYTES:
                    raise LimitReached("Part of this owl is too large to move.")
                if records and size + record_size > PAGE_BYTES:
                    next_cursor = _encode_cursor(mark, section, after)
                    break
                records.append(record)
                size += record_size
                after = key
            if next_cursor:
                break
            if len(batch) < 32:
                section, after = section + 1, None
        result = {
            "format": FORMAT,
            "version": VERSION,
            "fingerprint": mark,
            "records": records,
            "cursor": next_cursor,
        }
        if position is None:
            result["summary"] = _summary(db, bot_id)
        return result


# ---------------------------------------------------------------------------------------------
# The place the owl moves to: its copy, part by part.


def _derive(move_id: str, kind: str, source_id: str) -> str:
    """The copy's id for one of the original's: the same for the same move, new for every move."""
    return hashlib.sha256(f"{move_id}/{kind}/{source_id}".encode()).hexdigest()[:32]


def _digest(page: MovePage) -> str:
    body = json.dumps(
        [page.last, page.records], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(body.encode()).hexdigest()


class OwlMoves:
    """Owls arriving here from the person's other place; ``models``: what this place offers."""

    def __init__(self, store: Store, owner: str, *, models: PlaceModels | None = None):
        self.store, self.owner, self.models = store, owner, models

    def _move(self, db, move_id: str) -> dict:
        row = db.execute(
            "SELECT * FROM owl_moves WHERE id=? AND owner=?", (move_id, self.owner)
        ).fetchone()
        if row is None:
            raise NotFound("This move was not found. Start it again.")
        return dict(row)

    def _receipt(self, db, move: dict) -> dict:
        return {
            "id": move["id"],
            "request_id": move["request_id"],
            "source_bot_id": move["source_bot_id"],
            "state": move["state"],
            "bot_id": move["bot_id"],
            "pages": len(json.loads(move["page_digests"])),
            "received_all": bool(move["received_all"]),
        }

    def receipt(self, move_id: str) -> dict:
        """Where a move stands, with each file of it that has arrived (by the original's id)."""
        with self.store.connect() as db:
            move = self._move(db, move_id)
            result = self._receipt(db, move)
            arrived = self._arrived(db, move) if move["bot_id"] else {}
            result["files"] = [
                {"source_id": row["source_id"], "file_id": arrived.get(row["source_id"])}
                for row in db.execute(
                    "SELECT source_id FROM owl_move_files WHERE move_id=? ORDER BY name,revision",
                    (move_id,),
                )
            ]
            return result

    def _arrived(self, db, move: dict) -> dict[str, str]:
        """The original's file ids whose copy is here, checked by size and SHA-256."""
        staged = {
            (row["name"], row["revision"]): row
            for row in db.execute("SELECT * FROM files WHERE bot_id=?", (move["bot_id"],))
        }
        arrived = {}
        for expected in db.execute(
            "SELECT * FROM owl_move_files WHERE move_id=?", (move["id"],)
        ).fetchall():
            row = staged.get((expected["name"], expected["revision"]))
            if row is None:
                continue
            # A text file's copy arrives as its bytes, whose hash is the text's own.
            meta = Store._file_metadata(db, row)
            if (meta["size_bytes"], meta["sha256"]) == (expected["size_bytes"], expected["sha256"]):
                arrived[expected["source_id"]] = row["id"]
        return arrived

    def begin(self, data: MoveBegin) -> dict:
        """Start a move here, or find the one this request already started."""
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM owl_moves WHERE owner=? AND request_id=?",
                (self.owner, data.request_id),
            ).fetchone()
            stale = []
            if row is None:
                if db.execute(
                    "SELECT 1 FROM owl_moves m JOIN bots b ON b.id=m.bot_id "
                    "WHERE m.owner=? AND m.source_bot_id=? AND m.state='complete' AND b.owner=?",
                    (self.owner, data.source_bot_id, self.owner),
                ).fetchone():
                    raise Conflict(
                        "This owl already has a copy here. Open it from your owl list, "
                        "or delete that copy before moving it again."
                    )
                stale = [
                    item["id"]
                    for item in db.execute(
                        "SELECT id FROM owl_moves WHERE owner=? AND source_bot_id=? "
                        "AND state='receiving'",
                        (self.owner, data.source_bot_id),
                    )
                ]
        # An earlier try for this owl that never finished gives way to this one.
        for move_id in stale:
            self.cancel(move_id)
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM owl_moves WHERE owner=? AND request_id=?",
                (self.owner, data.request_id),
            ).fetchone()
            if row is not None:
                if row["source_bot_id"] != data.source_bot_id:
                    raise Conflict("This move request already belongs to another owl.")
                return self._receipt(db, dict(row))
            move_id, now = uid(), time.time()
            db.execute(
                "INSERT INTO owl_moves(id,owner,request_id,source_bot_id,bot_id,state,section,"
                "counts,page_digests,received_all,created_at,updated_at) "
                "VALUES(?,?,?,?,NULL,'receiving',0,'{}','[]',0,?,?)",
                (move_id, self.owner, data.request_id, data.source_bot_id, now, now),
            )
            return self._receipt(db, self._move(db, move_id))

    def receive(self, move_id: str, data: MovePage) -> dict:
        """Take one page of the owl, in order; a page sent again changes nothing."""
        digest = _digest(data)
        records = []
        for index, raw in enumerate(data.records):
            try:
                records.append(RECORD.validate_python(raw))
            except ValidationError as error:
                # Say where, never echo what was sent.
                where = ".".join(str(part) for part in error.errors()[0]["loc"][:4])
                raise InvalidMove(
                    f"Record {index + 1} of this part isn't valid ({where})."
                ) from None
        # The model the copy keeps, decided before the page's transaction: reading what this
        # place offers may read a provider's catalog.
        kept = None
        effort = None
        routines: dict[str, str | None] = {}
        for record in records:
            if (
                record.kind == "owl"
                and self.models is not None
                and (record.model or record.model_name)
            ):
                kept = self.models.keep(record.model, record.model_name)
            if (
                record.kind == "owl"
                and record.reasoning_effort
                and self.models
                and self.models.supports_effort(
                    kept, record.reasoning_effort, model_name=record.model_name
                )
            ):
                effort = record.reasoning_effort
            # A routine's the same way, by id; none means its owl's, so the place's own model
            # is kept by its id too.
            if (
                record.kind == "routine"
                and self.models is not None
                and (record.model or record.model_name)
            ):
                routines[record.id] = self.models.keep(
                    record.model, record.model_name, explicit=True
                )
        with self.store.connect(write=True) as db:
            move = self._move(db, move_id)
            if move["state"] != "receiving":
                raise Conflict("This move has already ended.")
            digests = json.loads(move["page_digests"])
            if data.page < len(digests):
                if digests[data.page] != digest:
                    raise Conflict("This part of the move already arrived with other content.")
                return self._receipt(db, move)
            if data.page != len(digests) or move["received_all"]:
                raise Conflict("A part of this move is missing. Send its parts in order.")
            state = _Import(
                self, db, move, model=kept, reasoning_effort=effort, routine_models=routines
            )
            for record in records:
                state.apply(record)
            if data.last and not state.counts.get("owl"):
                raise InvalidMove("This owl's setup never arrived.")
            db.execute(
                "UPDATE owl_moves SET bot_id=?,section=?,counts=?,page_digests=?,received_all=?,"
                "updated_at=? WHERE id=?",
                (
                    state.bot_id,
                    state.section,
                    encode(state.counts),
                    encode([*digests, digest]),
                    int(data.last),
                    time.time(),
                    move_id,
                ),
            )
            return self._receipt(db, self._move(db, move_id))

    def complete(self, move_id: str) -> dict:
        """Check every file of the copy and show it: the owl is here now."""
        with self.store.connect() as db:
            move = self._move(db, move_id)
            if move["state"] == "complete":
                return {
                    **self._receipt(db, move),
                    "owl": self.store._bot(db, self.owner, move["bot_id"]),
                }
            if move["state"] != "receiving":
                raise Conflict("This move has already ended.")
            if not move["received_all"] or not move["bot_id"]:
                raise Conflict("Part of this owl hasn't arrived yet.")
            bot_id = move["bot_id"]
            expected = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM owl_move_files WHERE move_id=? ORDER BY name,revision",
                    (move_id,),
                )
            ]
            revisions: dict[str, list[int]] = {}
            for row in expected:
                revisions.setdefault(row["name"], []).append(row["revision"])
            if any(numbers != list(range(1, len(numbers) + 1)) for numbers in revisions.values()):
                raise InvalidMove("This owl's file versions don't follow one another.")
            arrived = self._arrived(db, move)
            missing = [row for row in expected if row["source_id"] not in arrived]
            if missing:
                raise Conflict("Some of this owl's files haven't arrived yet.")
            extra = db.execute("SELECT count(*) FROM files WHERE bot_id=?", (bot_id,)).fetchone()[
                0
            ] - len(expected)
            if extra:
                raise Conflict("This owl's copy has files that weren't part of the move.")
        # Text files arrive as bytes; read them back so they stay text files here.
        texts = {}
        for row in expected:
            if not row["text_file"]:
                continue
            with closing(
                self.store.file_chunks(self.owner, bot_id, arrived[row["source_id"]])
            ) as chunks:
                payload = b"".join(chunks)
            try:
                texts[row["source_id"]] = payload.decode("utf-8")
            except UnicodeDecodeError:
                raise InvalidMove("A text file of this owl isn't text.") from None
        with self.store.connect(write=True) as db:
            move = self._move(db, move_id)
            if move["state"] == "complete":
                return {**self._receipt(db, move), "owl": self.store._bot(db, self.owner, bot_id)}
            if move["state"] != "receiving" or self._arrived(db, move) != arrived:
                raise Conflict("This move changed while it finished. Try again.")
            mapping = {row["source_id"]: arrived[row["source_id"]] for row in expected}
            tasks = {row[0] for row in db.execute("SELECT id FROM tasks WHERE bot_id=?", (bot_id,))}
            for row in expected:
                file_id = mapping[row["source_id"]]
                source_task = row["source_task_id"]
                if db.execute(
                    "SELECT 1 FROM owl_move_withdrawn_results WHERE move_id=? AND source_id=?",
                    (move_id, row["source_id"]),
                ).fetchone():
                    db.execute(
                        "INSERT INTO owl_withdrawn_results(file_id) VALUES(?) ON CONFLICT(file_id) DO NOTHING",
                        (file_id,),
                    )
                db.execute(
                    "UPDATE files SET created_at=?,updated_at=?,source_task_id=? WHERE id=?",
                    (
                        row["created_at"],
                        row["updated_at"],
                        source_task if source_task in tasks else None,
                        file_id,
                    ),
                )
                if row["source_id"] in texts:
                    db.execute(
                        "UPDATE files SET content=? WHERE id=?", (texts[row["source_id"]], file_id)
                    )
                    db.execute("DELETE FROM file_payloads WHERE file_id=?", (file_id,))
                    db.execute(
                        "UPDATE file_objects SET \"binary\"=0,media_type='text/plain' "
                        "WHERE file_id=?",
                        (file_id,),
                    )
                db.execute(
                    "UPDATE task_files SET file_id=? WHERE file_id=? AND task_id IN "
                    "(SELECT id FROM tasks WHERE bot_id=?)",
                    (file_id, row["source_id"], bot_id),
                )
            self._relink(db, bot_id, mapping)
            # The copies arrived as uploads, which count as files added: they were not, here.
            db.execute(
                "DELETE FROM trigger_deliveries WHERE event_id IN "
                "(SELECT id FROM activation_events WHERE bot_id=?)",
                (bot_id,),
            )
            db.execute("DELETE FROM activation_events WHERE bot_id=?", (bot_id,))
            # Its turns from the other place are never read again for memory here.
            reset_moved(db, bot_id, time.time())
            db.execute(
                "UPDATE owl_moves SET state='complete',updated_at=? WHERE id=?",
                (time.time(), move_id),
            )
            return {
                **self._receipt(db, self._move(db, move_id)),
                "owl": self.store._bot(db, self.owner, bot_id),
            }

    @staticmethod
    def _relink(db, bot_id: str, mapping: dict[str, str]) -> None:
        """Steps and file events in the copy's chat name the copies of its files."""
        for row in db.execute(
            "SELECT e.seq,e.data FROM events e JOIN tasks t ON t.id=e.task_id "
            "WHERE t.bot_id=? AND e.kind='tool_completed'",
            (bot_id,),
        ).fetchall():
            data = json.loads(row["data"])
            if isinstance(data, dict) and data.get("id") in mapping:
                db.execute(
                    "UPDATE events SET data=? WHERE seq=?",
                    (encode({**data, "id": mapping[data["id"]]}), row["seq"]),
                )
        for row in db.execute("SELECT id,config FROM tasks WHERE bot_id=?", (bot_id,)).fetchall():
            config = json.loads(row["config"])
            activation = config.get("activation")
            if not isinstance(activation, dict):
                continue
            events = [
                {**event, "file_id": mapping.get(event["file_id"], event["file_id"])}
                if event.get("file_id")
                else event
                for event in activation.get("events") or []
            ]
            db.execute(
                "UPDATE tasks SET config=? WHERE id=?",
                (encode({**config, "activation": {**activation, "events": events}}), row["id"]),
            )

    def cancel(self, move_id: str) -> dict:
        """Give up a move here: its copy, never shown, is deleted. A finished move stays."""
        with self.store.connect() as db:
            move = self._move(db, move_id)
            if move["state"] == "complete":
                raise Conflict("This move already finished; the owl is here.")
            if move["state"] == "cancelled":
                return self._receipt(db, move)
        if move["bot_id"]:
            try:
                self.store.delete_bot(self.owner, move["bot_id"])
            except NotFound:
                pass  # Already gone with an earlier cancel.
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM owl_move_files WHERE move_id=?", (move_id,))
            db.execute(
                "UPDATE owl_moves SET state='cancelled',updated_at=? WHERE id=? AND state='receiving'",
                (time.time(), move_id),
            )
            return self._receipt(db, self._move(db, move_id))

    def expire(self, *, now: float | None = None) -> int:
        """Cancel moves nobody finished or cancelled within a day; returns how many."""
        now = time.time() if now is None else now
        with self.store.connect() as db:
            stale = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM owl_moves WHERE owner=? AND state='receiving' AND updated_at<?",
                    (self.owner, now - EXPIRY_SECONDS),
                )
            ]
        cancelled = 0
        for move_id in stale:
            try:
                self.cancel(move_id)
                cancelled += 1
            except Conflict:
                continue  # It finished meanwhile: the owl is here.
        return cancelled


class _Import:
    """One page's records written into a move's copy, inside the page's transaction."""

    def __init__(
        self,
        moves: OwlMoves,
        db,
        move: dict,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        routine_models: dict[str, str | None] | None = None,
    ):
        self.moves, self.db, self.move = moves, db, move
        # The model the owl keeps here (OwlMoves.receive); None: this place's own.
        self.model = model
        self.reasoning_effort = reasoning_effort
        # The model each routine keeps here, by the routine's id there; None: its owl's.
        self.routine_models = routine_models or {}
        self.owner = moves.owner
        self.bot_id: str | None = move["bot_id"]
        self.section: int = move["section"]
        self.counts: dict[str, int] = json.loads(move["counts"])
        self.now = time.time()
        self.chat_id = Store._own_chat(db, self.bot_id)["id"] if self.bot_id else None

    def derive(self, kind: str, source_id: str) -> str:
        return _derive(self.move["id"], kind, source_id)

    def task(self, source_id: str | None) -> str | None:
        """The copy of a turn this owl brought, or None when it brought none by that id."""
        if source_id is None:
            return None
        task_id = self.derive("turn", source_id)
        found = self.db.execute(
            "SELECT 1 FROM tasks WHERE id=? AND bot_id=?", (task_id, self.bot_id)
        ).fetchone()
        return task_id if found else None

    def fresh(self, table: str, column: str, value: str) -> None:
        if self.db.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (value,)).fetchone():
            raise InvalidMove("Part of this owl arrived twice.")

    def apply(self, record) -> None:
        section = SECTIONS.index(record.kind)
        if section < self.section:
            raise InvalidMove("This owl's parts arrived out of order.")
        self.section = section
        self.counts[record.kind] = self.counts.get(record.kind, 0) + 1
        if self.counts[record.kind] > MAX_COUNTS[record.kind]:
            raise InvalidMove("This owl brings more than an owl can hold.")
        if record.kind != "owl" and self.bot_id is None:
            raise InvalidMove("This owl's setup must arrive first.")
        getattr(self, "_" + record.kind)(record)

    def _owl(self, record: OwlRecord) -> None:
        if (
            self.db.execute("SELECT count(*) FROM bots WHERE owner=?", (self.owner,)).fetchone()[0]
            >= MAX_OWLS
        ):
            raise LimitReached(f"You can have up to {MAX_OWLS} owls. Delete one to make room.")
        self.bot_id = self.derive("owl", self.move["source_bot_id"])
        self.fresh("bots", "id", self.bot_id)
        self.db.execute(
            "INSERT INTO bots(id,owner,name,purpose,instructions,memory_enabled,files_enabled,"
            "created_at,updated_at,color,background,pinned_at,archived_at,model,reasoning_effort) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.bot_id,
                self.owner,
                record.name,
                record.purpose,
                record.instructions,
                record.memory_enabled,
                record.files_enabled,
                record.created_at,
                self.now,
                record.color,
                record.background,
                record.pinned_at,
                record.archived_at,
                self.model,
                self.reasoning_effort,
            ),
        )
        self.chat_id = self.derive("chat", self.move["source_bot_id"])
        self.db.execute(
            "INSERT INTO chats(id,bot_id,title,created_at) VALUES(?,?,?,?)",
            (
                self.chat_id,
                self.bot_id,
                record.chat.title if record.chat else record.name,
                record.chat.created_at if record.chat else record.created_at,
            ),
        )
        for thread in record.threads:
            chat_id = self.derive("thread", thread.id)
            self.fresh("chats", "id", chat_id)
            self.db.execute(
                "INSERT INTO chats VALUES(?,?,?,?)",
                (chat_id, self.bot_id, thread.title, thread.created_at),
            )
            self.db.execute(
                "INSERT INTO owl_chat_threads VALUES(?,?,?)",
                (chat_id, self.bot_id, "moved_" + chat_id),
            )
        if record.review_mode:
            self.db.execute(
                "INSERT INTO bot_review_settings(bot_id,mode) VALUES(?,?)",
                (self.bot_id, record.review_mode),
            )

    def _rule(self, record: RuleRecord) -> None:
        rule_id = self.derive("rule", record.id)
        self.fresh("bot_action_rules", "id", rule_id)
        self.db.execute(
            "INSERT INTO bot_action_rules(id,bot_id,effect,description,action,source_review_id,"
            "revision,deleted,created_at,updated_at) VALUES(?,?,?,?,?,NULL,1,0,?,?)",
            (
                rule_id,
                self.bot_id,
                record.effect,
                record.description,
                encode(record.action) if record.action is not None else None,
                record.created_at,
                record.updated_at,
            ),
        )

    def _app(self, record: AppRecord) -> None:
        if self.db.execute(
            "SELECT 1 FROM bot_plugin_grants WHERE bot_id=? AND plugin_id=?",
            (self.bot_id, record.plugin_id),
        ).fetchone():
            raise InvalidMove("Part of this owl arrived twice.")
        self.db.execute(
            "INSERT INTO bot_plugin_grants(bot_id,plugin_id,enabled,revision,updated_at) "
            "VALUES(?,?,?,1,?)",
            (self.bot_id, record.plugin_id, record.enabled, self.now),
        )

    def _turn(self, record: TurnRecord) -> None:
        task_id = self.derive("turn", record.id)
        chat_id = self.chat_id
        if record.thread:
            chat_id = self.derive("thread", record.thread)
            if not self.db.execute(
                "SELECT 1 FROM owl_chat_threads WHERE chat_id=? AND bot_id=?",
                (chat_id, self.bot_id),
            ).fetchone():
                raise Conflict("A moved turn refers to an unknown conversation.")
        self.fresh("tasks", "id", task_id)
        # The person's own request id where it is free here, so the app knows its own turns.
        request_id = record.request_id
        if (
            not re.fullmatch(r"[A-Za-z0-9_:.-]{1,200}", request_id)
            or self.db.execute(
                "SELECT 1 FROM tasks WHERE owner=? AND request_id=?", (self.owner, request_id)
            ).fetchone()
        ):
            request_id = "moved_" + task_id
        config = record.config.model_dump(exclude_none=True)
        if record.config.activation is not None:
            activation = config["activation"]
            if activation.get("trigger_id"):
                activation["trigger_id"] = self.derive("trigger", activation["trigger_id"])
        if record.config.regenerate is not None:
            # Turns arrive under ids derived from their source ids, so the links follow them.
            config["regenerate"] = {
                key: self.derive("turn", value) for key, value in config["regenerate"].items()
            }
        config["id"] = self.bot_id
        self.db.execute(
            "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,"
            "created_at,updated_at,error,calls,output,failure) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                chat_id,
                self.bot_id,
                self.owner,
                request_id,
                record.message,
                record.status,
                encode(config),
                record.created_at,
                record.updated_at,
                record.error,
                record.calls,
                record.output,
                encode(record.failure) if record.failure is not None else None,
            ),
        )
        for index, message in enumerate(sorted(record.messages, key=lambda m: m.created_at)):
            message_id = self.derive("message", f"{record.id}/{index}")
            self.db.execute(
                "INSERT INTO messages(id,chat_id,task_id,role,content,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    message_id,
                    chat_id,
                    task_id,
                    message.role,
                    message.content,
                    message.created_at,
                ),
            )
            if message.reaction is not None:
                reaction = message.reaction
                self.db.execute(
                    "INSERT INTO reply_reactions(message_id,bot_id,task_id,reaction,note,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        message_id,
                        self.bot_id,
                        task_id,
                        reaction.reaction,
                        reaction.note,
                        reaction.created_at,
                        reaction.updated_at,
                    ),
                )
        if record.queued is not None:
            self.db.execute(
                "INSERT INTO queued_messages(task_id,revision,sent_sha256,withdrawn_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    task_id,
                    record.queued.revision,
                    record.queued.sent_sha256,
                    record.queued.withdrawn_at,
                    record.updated_at,
                ),
            )
        for event in record.events:
            self.db.execute(
                "INSERT INTO events(task_id,kind,data,created_at) VALUES(?,?,?,?)",
                (task_id, event.kind, encode(event.data), event.created_at),
            )
        for ordinal, file in enumerate(record.files):
            # The original's file id until the file itself arrives (complete links it).
            self.db.execute(
                "INSERT INTO task_files(task_id,ordinal,file_id,name,media_type,size_bytes) "
                "VALUES(?,?,?,?,?,?)",
                (task_id, ordinal, file.file_id, file.name, file.media_type, file.size_bytes),
            )
        if record.read:
            finished = self.db.execute(
                f"SELECT max(seq) FROM events WHERE task_id=? AND kind IN {TERMINAL}", (task_id,)
            ).fetchone()[0]
            if finished is not None:
                self.db.execute(
                    "INSERT INTO chat_read_cursors(chat_id,event_seq) VALUES(?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET event_seq=CASE WHEN "
                    "chat_read_cursors.event_seq>excluded.event_seq THEN "
                    "chat_read_cursors.event_seq ELSE excluded.event_seq END",
                    (chat_id, finished),
                )

    def _memory(self, record: MemoryRecord) -> None:
        memory_id = self.derive("memory", record.id)
        self.fresh("memories", "id", memory_id)
        self.db.execute(
            "INSERT INTO memories(id,bot_id,title,content,status,source_task_id,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                memory_id,
                self.bot_id,
                record.title,
                record.content,
                record.status,
                self.task(record.source_task_id),
                record.created_at,
                record.updated_at,
            ),
        )
        if record.replaces is not None:
            if record.status != "pending":
                raise InvalidMove("Only a memory waiting for review replaces another.")
            self.db.execute(
                "INSERT INTO memory_replacements(proposal_id,target_id,expected_updated_at) "
                "VALUES(?,?,?)",
                (
                    memory_id,
                    self.derive("memory", record.replaces.id),
                    record.replaces.expected_updated_at,
                ),
            )

    def _note(self, record: NoteRecord) -> None:
        note_id = self.derive("note", record.id)
        self.fresh("working_notes", "id", note_id)
        self.db.execute(
            "INSERT INTO working_notes(id,bot_id,title,content,revision,source_task_id,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                note_id,
                self.bot_id,
                record.title,
                record.content,
                record.revision,
                self.task(record.source_task_id),
                record.created_at,
                record.updated_at,
            ),
        )
        for revision in record.revisions:
            self.db.execute(
                "INSERT INTO note_revisions(note_id,revision,title,content,source_task_id,"
                "created_at) VALUES(?,?,?,?,?,?)",
                (
                    note_id,
                    revision.revision,
                    revision.title,
                    revision.content,
                    self.task(revision.source_task_id),
                    revision.created_at,
                ),
            )

    def _archive(self, record: ArchiveRecord) -> None:
        source = self.derive("note", record.source_note_id)
        target = self.derive("note", record.target_note_id)
        found = {
            row[0]
            for row in self.db.execute(
                "SELECT id FROM working_notes WHERE bot_id=? AND id IN (?,?)",
                (self.bot_id, source, target),
            )
        }
        if found != {source, target} or source == target:
            raise InvalidMove("An archived note refers to a note this owl didn't bring.")
        if self.db.execute(
            "SELECT 1 FROM note_consolidations WHERE source_note_id=? AND restored_at IS NULL",
            (source,),
        ).fetchone():
            raise InvalidMove("Part of this owl arrived twice.")
        self.db.execute(
            "INSERT INTO note_consolidations(source_note_id,source_revision,target_note_id,"
            "target_revision,reason,source_task_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                source,
                record.source_revision,
                target,
                record.target_revision,
                record.reason,
                self.task(record.source_task_id),
                record.created_at,
            ),
        )

    def _routine(self, record: RoutineRecord) -> None:
        schedule_id = self.derive("routine", record.id)
        self.fresh("schedules", "id", schedule_id)
        # Routines arrive off: one that was on would otherwise run here and in its old place.
        moved = record.state == "active"
        state = "paused" if moved else record.state
        self.db.execute(
            "INSERT INTO schedules(id,bot_id,name,prompt,timing,state,revision,next_run_at,"
            "pause_reason,source_task_id,created_at,updated_at,model) "
            "VALUES(?,?,?,?,?,?,1,NULL,?,?,?,?,?)",
            (
                schedule_id,
                self.bot_id,
                record.name,
                record.prompt,
                encode(record.timing),
                state,
                MOVED_PAUSE_REASON if moved else record.pause_reason if state == "paused" else None,
                self.task(record.source_task_id),
                record.created_at,
                self.now,
                self.routine_models.get(record.id),
            ),
        )
        if moved:
            self.db.execute(
                "INSERT INTO schedule_health(schedule_id,revision,failures,retry_at,retry_for,"
                "attempts,retry_reason,pause_code,updated_at) VALUES(?,1,0,NULL,NULL,0,NULL,?,?)",
                (schedule_id, MOVED_PAUSE_CODE, self.now),
            )
        # What it needs travels; what the person allowed was for the other place's computer and
        # sign-ins, so it asks again here.
        if record.needs is not None and parse_needs(record.needs) is not None:
            self.db.execute(
                "INSERT INTO routine_access(schedule_id,needs,allowed,allowed_at,requested,"
                "updated_at) VALUES(?,?,NULL,NULL,NULL,?)",
                (schedule_id, encode(parse_needs(record.needs).model_dump()), self.now),
            )

    def _trigger(self, record: TriggerRecord) -> None:
        trigger_id = self.derive("trigger", record.id)
        self.fresh("bot_triggers", "id", trigger_id)
        source_task = self.task(record.source_task_id)
        if source_task:
            # One file event per turn that suggested it, as everywhere.
            self.fresh("bot_triggers", "source_task_id", source_task)
        updated = self.db.execute(
            "SELECT updated_at FROM bots WHERE id=?", (self.bot_id,)
        ).fetchone()[0]
        self.db.execute(
            "INSERT INTO bot_triggers(id,bot_id,name,kind,instructions,state,revision,"
            "source_task_id,bot_updated_at,last_run_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,1,?,?,?,?,?)",
            (
                trigger_id,
                self.bot_id,
                record.name,
                record.trigger_kind,
                record.instructions,
                "paused" if record.state == "active" else record.state,
                source_task,
                updated,
                record.last_run_at,
                record.created_at,
                self.now,
            ),
        )

        if record.trigger_kind != "file_added":
            # Provider account grants don't move with an Owl. Keep the requested filter for review.
            self.db.execute(
                "INSERT INTO app_event_watches VALUES(?,?, '', '',0,?,NULL,'',?,'rebind')",
                (trigger_id, record.gmail_sender, self.now, self.now),
            )

    def _suggestion(self, record: SuggestionRecord) -> None:
        suggestion_id = self.derive("suggestion", record.id)
        self.fresh("routine_suggestions", "id", suggestion_id)
        source_task = self.task(record.source_task_id)
        if source_task is None:
            raise InvalidMove("A suggested routine arrived without the turn that suggested it.")
        # One suggestion per turn, as everywhere.
        self.fresh("routine_suggestions", "source_task_id", source_task)
        schedule_id = None
        if record.routine_id is not None:
            schedule_id = self.derive("routine", record.routine_id)
            if not self.db.execute(
                "SELECT 1 FROM schedules WHERE id=? AND bot_id=?", (schedule_id, self.bot_id)
            ).fetchone():
                raise InvalidMove("A suggested routine names a routine this owl didn't bring.")
        # Worked out here from what arrived, never taken from the page.
        job, title = fingerprints(record.name, record.prompt, record.timing)
        self.db.execute(
            "INSERT INTO routine_suggestions(id,bot_id,source_task_id,schedule_id,name,prompt,"
            "timing,needs,fingerprint,title_fingerprint,status,revision,request_id,created_at,"
            "updated_at,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
            (
                suggestion_id,
                self.bot_id,
                source_task,
                schedule_id,
                record.name,
                record.prompt,
                encode(record.timing),
                encode(record.needs),
                job,
                title,
                record.status,
                record.revision,
                record.created_at,
                record.updated_at,
                record.decided_at,
            ),
        )

    def _playbook(self, record: PlaybookRecord) -> None:
        playbook_id = self.derive("playbook", record.id)
        self.fresh("playbooks", "playbook_id", playbook_id)
        current = record.current
        others = [
            dict(row)
            for row in self.db.execute(
                "SELECT name FROM playbooks WHERE bot_id=? AND status='current'", (self.bot_id,)
            )
        ]
        if same_name(current.name, others) is not None:
            raise InvalidMove("Two of this owl's playbooks have the same name.")
        for part in record.versions:
            self.db.execute(
                "INSERT INTO playbooks(id,playbook_id,bot_id,version,name,when_to_use,steps,"
                "pitfalls,done_check,tools,source_task_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.derive("playbook-version", f"{record.id}/{part.version}"),
                    playbook_id,
                    self.bot_id,
                    part.version,
                    part.name,
                    part.when_to_use,
                    encode(part.steps),
                    encode(part.pitfalls),
                    part.done_check,
                    encode(part.tools),
                    # A version keeps its run when the owl brought that turn.
                    self.task(part.source_task_id),
                    "current" if part.current else "earlier",
                    part.created_at,
                    part.updated_at,
                ),
            )

    def _check_back(self, record: CheckBackRecord) -> None:
        row_id = self.derive("check_back", record.id)
        self.fresh("check_backs", "id", row_id)
        condition = record.condition
        source = self.task(record.source_task_id)
        chat = self.db.execute(
            "SELECT chat_id FROM tasks WHERE id=? AND bot_id=?", (source, self.bot_id)
        ).fetchone()
        # An owl moves only while none of its work is going, so work it waited for has ended.
        met_at = self.now if condition and condition["kind"] == "work_ends" else None
        self.db.execute(
            "INSERT INTO check_backs(id,bot_id,chat_id,source_task_id,due_at,note,condition,"
            "state,next_check_at,checks,met_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'pending',?,0,?,?,?)",
            (
                row_id,
                self.bot_id,
                chat[0] if chat else self.chat_id,
                source,
                record.due_at,
                record.note,
                encode(condition) if condition else None,
                self.now + check_back_first_check(condition["kind"]) if condition else None,
                met_at,
                record.created_at,
                self.now,
            ),
        )

    def _file(self, record: FileRecord) -> None:
        if self.db.execute(
            "SELECT 1 FROM owl_move_files WHERE move_id=? AND (source_id=? OR "
            "(name=? AND revision=?))",
            (self.move["id"], record.id, record.name, record.revision),
        ).fetchone():
            raise InvalidMove("Part of this owl arrived twice.")
        self.db.execute(
            "INSERT INTO owl_move_files(move_id,source_id,name,revision,size_bytes,sha256,"
            "text_file,media_type,source_task_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.move["id"],
                record.id,
                record.name,
                record.revision,
                record.size_bytes,
                record.sha256,
                int(not record.binary),
                record.media_type,
                self.derive("turn", record.source_task_id) if record.source_task_id else None,
                record.created_at,
                record.updated_at,
            ),
        )
        if record.withdrawn_from_reply:
            self.db.execute(
                "INSERT INTO owl_move_withdrawn_results(move_id,source_id) VALUES(?,?)",
                (self.move["id"], record.id),
            )
