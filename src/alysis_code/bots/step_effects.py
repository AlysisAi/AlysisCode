"""What a turn's steps did beyond reading, from its step receipts, never from words.

Every step a turn takes leaves receipts in its events: ``tool_started`` when it starts, then
``tool_completed`` or ``tool_failed``. A step either reads, keeps the owl's own records (its
Library, working notes, memory and playbooks, which a later run sees and can check), or may
have changed something outside them: a website, a message or payment sent, a plugin's write, a
command or input on a computer, a file moved to or from the person's computer, a card or question
for the person, another owl's or a coding job's work. Only the steps listed here count as reading
or as keeping the owl's records, and a plugin step only when its own effect label says it reads:
every other step, a tool added later included, may have changed something. A step that failed,
or started and never finished, may have happened: its outcome is unknown.

A routine's run may end without a reply only when no step changed anything outside the owl's
records (BotRuntime._nothing_to_report), and a turn a restart cut off starts again only then
(Store._cut_by_restart).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .browser_tools import NO_WEBSITE_APPROVAL_TOOLS
from .research_helpers import RESEARCH_TOOL

# The tool that ends a routine's run when it found nothing new (scheduling.NothingToReport).
NOTHING_TO_REPORT = "nothing_to_report"

# Steps that only read: the web, pages and the owl's computer, its Library, memory, notes and
# history, and the state of its own work. Browser steps are those that need no website permission.
READING = frozenset(
    {
        "web_search",
        "web_fetch",
        RESEARCH_TOOL,
        "list_local_computers",
        "read_tool_result",
        "read_browser_draft",
        "list_schedules",
        "list_triggers",
        "read_background_task",
        "read_coding_result",
        "list_peer_bots",
        "read_peer_exchange",
        "search_history",
        "read_message",
        "list_memories",
        "read_memory",
        "memory_history",
        "memory_source_read",
        "read_working_note",
        "search_working_notes",
        "list_files",
        "read_file",
        # One read-only SQL query over Library tables, in a database that lives for the call.
        "query_tables",
        "read_playbook",
        "describe_document_format",
        "inspect_document",
        "check_document",
        "tool_details",
        "computer_observe",
        "computer_process",
        "computer_check_file",
        # The clipboard's text, read as data (desktop.py).
        "computer_read_clipboard",
        *NO_WEBSITE_APPROVAL_TOOLS,
    }
)
# Steps that change only the owl's own records, which a later run of the same turn sees.
OWN_RECORDS = frozenset(
    {
        "save_file",
        "create_document",
        "edit_document",
        # A picture made or changed by the image service, kept as a new Library file or version
        # (owl_images.py).
        "create_image",
        "edit_image",
        "computer_save_file",
        "save_working_note",
        "consolidate_working_notes",
        "propose_memory",
        "save_playbook",
    }
)
# Steps that only move the turn itself: carrying on off the reply loop, or ending a routine's
# run that found nothing new.
TURN_CONTROL = frozenset({"continue_in_background", NOTHING_TO_REPORT})
CHANGES_NOTHING_OUTSIDE = READING | OWN_RECORDS | TURN_CONTROL

STEP_RECEIPTS = ("tool_started", "tool_completed", "tool_failed")
# A turn a restart cut off is started again at most this many times (Store.recover).
RESTART_RERUNS = 1
# The newest steps a turn started again is told it took before the restart.
NOTED_STEPS = 20


@dataclass(frozen=True)
class OutwardStep:
    """A step that changed something outside the owl's own records, or may have."""

    name: str
    # True: its receipt says it finished. False: it failed or never finished, so it may have
    # happened or not.
    finished: bool


def first_outward_step(db: sqlite3.Connection, task_id: str) -> OutwardStep | None:
    """The turn's first step that changed, or may have changed, something outside the owl's own
    records; None when every step read or kept its records."""
    started: str | None = None
    for row in db.execute(
        "SELECT kind,data FROM events WHERE task_id=? AND kind IN (?,?,?) ORDER BY seq",
        (task_id, *STEP_RECEIPTS),
    ):
        data = json.loads(row["data"])
        name = data.get("name") if isinstance(data.get("name"), str) else ""
        if row["kind"] == "tool_started":
            if started is not None and started not in CHANGES_NOTHING_OUTSIDE:
                # The step before never said how it ended.
                return OutwardStep(started, finished=False)
            started = name
            continue
        started = None
        if name in CHANGES_NOTHING_OUTSIDE:
            continue
        if row["kind"] == "tool_completed" and data.get("effect") == "read":
            continue  # A plugin step its own label says only reads.
        return OutwardStep(name, finished=row["kind"] == "tool_completed")
    if started is not None and started not in CHANGES_NOTHING_OUTSIDE:
        return OutwardStep(started, finished=False)
    return None


def steps_taken(db: sqlite3.Connection, task_id: str, *, before: int | None = None) -> list[dict]:
    """The turn's newest steps (before event ``before``), oldest first, as their tool_started
    receipts name them: the tool, and the topic, file name or site it named (step_started)."""
    rows = db.execute(
        "SELECT data FROM events WHERE task_id=? AND kind='tool_started' "
        "AND (CAST(? AS BIGINT) IS NULL OR seq<?) ORDER BY seq DESC LIMIT ?",
        (task_id, before, before, NOTED_STEPS),
    ).fetchall()
    return [json.loads(row["data"]) for row in reversed(rows)]


def restart_note(steps: list[dict]) -> str:
    """What a turn started again after a restart is told before it carries on."""
    return (
        "This turn was interrupted by a restart and started again. Check what you already did "
        "before acting: nothing it did reached outside your own records, and the files, notes "
        "and memories it saved are there. Its steps before the restart, oldest first: "
        + json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
    )
