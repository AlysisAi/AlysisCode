"""Playbooks: short named procedures an owl keeps for jobs that come again.

Without them every repeat of a job starts from zero: a routine plans its whole job again on each
run. After a run that worked, the owl may write a playbook from that run (``save_playbook``): when
to use it, the steps that worked, the pitfalls it met and one check that proves the job is done.
The service adds the tools the run actually used, from the run's own receipts, and refuses a
playbook from a run that has done nothing yet.

Nothing is kept from a run that did not succeed. What the owl writes waits with its run ("held")
and is kept only when the run finishes completed, its reply passed the reply checker (not a plain
status given after withheld drafts), and nothing from the run still waits for the person (a
question or an approval card). Any other end drops it. A kept playbook shows under the reply,
where the person can undo it, and in the owl's settings, where they can open, undo or delete it.

Later turns and routines of the owl see a short index (name and when to use) in their task state
and read a whole playbook with ``read_playbook`` when one fits. After a better run the owl may
save a new version of it (``replaces_playbook_id``); Undo brings the version before back.

Limits, per owl:

- ``MAX_PLAYBOOKS`` current playbooks. Every turn carries the index; 20 entries of a name and a
  when-to-use line stay under about 6,000 characters (some 1,500 tokens) even when every field is
  full, and 20 different recurring jobs is more than one owl's work tends to have. At the limit
  the owl improves one that covers the job, or the person deletes one.
- Each playbook is short: a name, one when-to-use line, at most 12 steps and 6 pitfalls, and one
  done check, at most about 6,000 characters, so reading one costs at most some 1,500 tokens.
- ``MAX_EARLIER_VERSIONS`` versions before the current one are kept, so Undo can step back over a
  few worse updates; older ones are erased. A playbook holds at most four versions.
- One playbook per run: saving again in the same run corrects what that run wrote.

Names are compared Unicode-normalized and case-folded, and without accents only when nothing
matches exactly, so one owl never has two playbooks of the same name. Nothing here reads the
words for meaning.
"""

from __future__ import annotations

import json
import time
import unicodedata
from typing import Annotated

from pydantic import Field, StringConstraints

from .models import Input
from .store import Conflict, NotFound, Store, encode, uid

MAX_PLAYBOOKS = 20
MAX_EARLIER_VERSIONS = 3
NAME_CHARACTERS = 80
WHEN_CHARACTERS = 200
LINE_CHARACTERS = 300
MAX_STEPS = 12
MAX_PITFALLS = 6
# The tools a run used, as its receipts name them; a run that used more lists its first ones.
MAX_TOOLS = 30
PLAYBOOK_TOOLS = frozenset({"save_playbook", "read_playbook"})
# A run whose question or approval card still waits for the person has not finished its job.
_WAITING_ON_THE_PERSON = (
    "SELECT 1 FROM clarifications WHERE source_task_id=? AND status='pending' UNION ALL "
    "SELECT 1 FROM action_proposals WHERE source_task_id=? AND status='pending'"
)

Line = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LINE_CHARACTERS)
]


class PlaybookInput(Input):
    """What the owl writes: a short procedure for a job that worked."""

    name: str = Field(min_length=1, max_length=NAME_CHARACTERS)
    when_to_use: str = Field(min_length=1, max_length=WHEN_CHARACTERS)
    steps: list[Line] = Field(min_length=1, max_length=MAX_STEPS)
    pitfalls: list[Line] = Field(default_factory=list, max_length=MAX_PITFALLS)
    done_check: str = Field(min_length=1, max_length=LINE_CHARACTERS)
    replaces_playbook_id: str | None = Field(default=None, min_length=1, max_length=100)


class ReadPlaybook(Input):
    playbook_id: str = Field(min_length=1, max_length=100)


class PlaybookUndo(Input):
    """The version the person saw, so Undo never takes back a newer one they haven't seen."""

    version: int | None = Field(default=None, ge=1)


def _folded(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(unicodedata.normalize("NFKC", folded).split())


def _without_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", _folded(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def same_name(name: str, others: list[dict]) -> dict | None:
    """The playbook among ``others`` with this name: exactly (normalized and case-folded) first,
    and only when none matches exactly, without accents."""
    key = _folded(name)
    exact = next((other for other in others if _folded(other["name"]) == key), None)
    if exact is not None:
        return exact
    loose = _without_accents(name)
    return next((other for other in others if _without_accents(other["name"]) == loose), None)


def _version(row) -> dict:
    return {
        "name": row["name"],
        "when_to_use": row["when_to_use"],
        "steps": json.loads(row["steps"]),
        "pitfalls": json.loads(row["pitfalls"]),
        "done_check": row["done_check"],
        "tools": json.loads(row["tools"]),
    }


def _current(db, bot_id: str) -> list:
    return db.execute(
        "SELECT * FROM playbooks WHERE bot_id=? AND status='current' "
        "ORDER BY updated_at DESC,playbook_id",
        (bot_id,),
    ).fetchall()


def _used_tools(db, task_id: str) -> list[str]:
    """The tools this run completed, in the order it first used them, from its receipts."""
    used: list[str] = []
    for row in db.execute(
        "SELECT data FROM events WHERE task_id=? AND kind='tool_completed' ORDER BY seq",
        (task_id,),
    ):
        name = json.loads(row["data"]).get("name")
        if isinstance(name, str) and name not in PLAYBOOK_TOOLS and name not in used:
            used.append(name)
    return used[:MAX_TOOLS]


def _drop_orphans(db, bot_id: str) -> None:
    """What runs that ended some other way left held (a stop, a restart): never kept."""
    db.execute(
        "DELETE FROM playbooks WHERE bot_id=? AND status='held' AND source_task_id IN "
        "(SELECT id FROM tasks WHERE status IN ('completed','failed','cancelled','interrupted'))",
        (bot_id,),
    )


def settle_run(db, task_id: str, *, succeeded: bool, now: float) -> None:
    """What a run held is kept once it ends, or dropped, inside the transaction that ends it.

    ``succeeded``: the run finished completed and its reply passed the reply checker. A run whose
    question or approval card still waits for the person keeps nothing either.
    """
    held = db.execute(
        "SELECT * FROM playbooks WHERE source_task_id=? AND status='held'", (task_id,)
    ).fetchone()
    if held is None:
        return
    reason = None
    if not succeeded:
        reason = "run_not_checked"
    elif db.execute(_WAITING_ON_THE_PERSON, (task_id, task_id)).fetchone():
        reason = "waiting_for_person"
    else:
        reason = _keep(db, held, now)
    if reason is None:
        Store._event(
            db,
            task_id,
            "playbook_kept",
            {"playbook_id": held["playbook_id"], "version": _kept_version(db, held["id"])},
        )
        return
    db.execute("DELETE FROM playbooks WHERE id=?", (held["id"],))
    Store._event(db, task_id, "playbook_dropped", {"reason": reason})


def _kept_version(db, version_id: str) -> int:
    return db.execute("SELECT version FROM playbooks WHERE id=?", (version_id,)).fetchone()[0]


def _keep(db, held, now: float) -> str | None:
    """Make the held version current; a reason code when the owl's playbooks changed meanwhile
    so that it can't be."""
    bot_id = held["bot_id"]
    current = _current(db, bot_id)
    target = next((row for row in current if row["playbook_id"] == held["playbook_id"]), None)
    update = held["version"] > 1
    if update and target is None:
        return "playbook_removed"  # The person deleted or undid it while the run worked.
    others = [row for row in current if row["playbook_id"] != held["playbook_id"]]
    if same_name(held["name"], others) is not None:
        return "name_taken"
    if not update and len(current) >= MAX_PLAYBOOKS:
        return "limit"
    version = 1
    if target is not None:
        version = target["version"] + 1
        db.execute("UPDATE playbooks SET status='earlier' WHERE id=?", (target["id"],))
    db.execute(
        "UPDATE playbooks SET status='current',version=?,updated_at=? WHERE id=?",
        (version, now, held["id"]),
    )
    # Undo needs the version before; a few are kept, older ones go.
    earlier = db.execute(
        "SELECT id FROM playbooks WHERE playbook_id=? AND status='earlier' ORDER BY version DESC",
        (held["playbook_id"],),
    ).fetchall()
    for row in earlier[MAX_EARLIER_VERSIONS:]:
        db.execute("DELETE FROM playbooks WHERE id=?", (row["id"],))
    return None


def task_playbooks(db, bot_id: str, *, only=None) -> dict[str, dict]:
    """The playbook each turn kept (one per run), by its task id, for the chat under its reply.

    ``current``: that version is still the playbook's current one, so Undo applies to it.
    """
    scope, values = "", (bot_id,)
    if only is not None:
        if not only:
            return {}
        scope = f" AND source_task_id IN ({','.join('?' * len(only))})"
        values = (bot_id, *only)
    return {
        row["source_task_id"]: {
            "id": row["playbook_id"],
            "name": row["name"],
            "version": row["version"],
            "current": row["status"] == "current",
        }
        for row in db.execute(
            "SELECT playbook_id,name,version,status,source_task_id FROM playbooks "
            "WHERE bot_id=? AND status IN ('current','earlier') AND source_task_id IS NOT NULL"
            + scope,
            values,
        )
    }


def erase(db, bot_id: str) -> None:
    db.execute("DELETE FROM playbooks WHERE bot_id=?", (bot_id,))


class Playbooks:
    """An owl's own playbooks: written from runs that worked, kept by the service, shown to the
    person."""

    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    # The owl's side --------------------------------------------------------------------------

    def hold(self, task: dict, data: PlaybookInput) -> dict:
        """Hold what the owl wrote with its run, until the run ends (``settle_run``)."""
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            bot = self.store._bot(db, self.owner, source["bot_id"])
            if source["bot_id"] != task["bot_id"] or not self.store.executing(db, source):
                raise Conflict("Only a run that is still working can write a playbook.")
            if not bot["memory_enabled"]:
                raise Conflict("Memory is off for this owl, so it keeps no playbooks.")
            tools = _used_tools(db, source["id"])
            if not tools:
                raise Conflict(
                    "A playbook is written from what this run did, and it has no finished steps "
                    "yet. Do the job first; save a playbook only if it worked."
                )
            _drop_orphans(db, bot["id"])
            current = _current(db, bot["id"])
            target = None
            if data.replaces_playbook_id is not None:
                target = next(
                    (row for row in current if row["playbook_id"] == data.replaces_playbook_id),
                    None,
                )
                if target is None:
                    raise NotFound(
                        "There is no playbook with that id. Use an id from the playbook index in "
                        "the task state, or leave replaces_playbook_id out for a new one."
                    )
            others = [row for row in current if target is None or row["id"] != target["id"]]
            clash = same_name(data.name, others)
            if clash is not None:
                raise Conflict(
                    f"You already have a playbook named {clash['name']} (id "
                    f"{clash['playbook_id']}). To improve it, read it and save with its id as "
                    "replaces_playbook_id; otherwise give this one another name."
                )
            # A refusal the owl reads and carries on from: never a limit that ends the run.
            if target is None and len(current) >= MAX_PLAYBOOKS:
                raise Conflict(
                    f"This owl keeps at most {MAX_PLAYBOOKS} playbooks. Improve one that covers "
                    "this job (replaces_playbook_id), or carry on without saving; the person can "
                    "delete playbooks in the owl's settings."
                )
            now = time.time()
            # One per run: saving again in the same run corrects what it wrote.
            db.execute(
                "DELETE FROM playbooks WHERE source_task_id=? AND status='held'", (source["id"],)
            )
            playbook_id = target["playbook_id"] if target is not None else uid()
            db.execute(
                "INSERT INTO playbooks(id,playbook_id,bot_id,version,name,when_to_use,steps,"
                "pitfalls,done_check,tools,source_task_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'held',?,?)",
                (
                    uid(),
                    playbook_id,
                    bot["id"],
                    target["version"] + 1 if target is not None else 1,
                    data.name,
                    data.when_to_use,
                    encode(data.steps),
                    encode(data.pitfalls),
                    data.done_check,
                    encode(tools),
                    source["id"],
                    now,
                    now,
                ),
            )
            return {
                "id": playbook_id,
                "state": "held",
                "updates": target["name"] if target is not None else None,
                "tools": tools,
                "message": "Held with this run. It is kept only when this run finishes and your "
                "reply passes its check; then it shows under your reply, where the person can "
                "undo it. You need not mention it; if you do, say you'll keep it for next time.",
            }

    def index(self, bot_id: str) -> list[dict]:
        """Name and when to use of each current playbook, newest first, for the task state."""
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            return [
                {
                    "id": row["playbook_id"],
                    "name": row["name"],
                    "when_to_use": row["when_to_use"],
                    "version": row["version"],
                }
                for row in _current(db, bot_id)
            ]

    def read(self, bot_id: str, playbook_id: str, *, for_person: bool = False) -> dict:
        """The current version in full; for the person, with its versions and source turn."""
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            row = db.execute(
                "SELECT * FROM playbooks WHERE bot_id=? AND playbook_id=? AND status='current'",
                (bot_id, playbook_id),
            ).fetchone()
            if row is None:
                raise NotFound("Playbook not found.")
            result = {
                "id": playbook_id,
                **_version(row),
                "version": row["version"],
                "updated_at": row["updated_at"],
            }
            if for_person:
                result["source_task_id"] = row["source_task_id"]
                result["versions"] = [
                    {
                        "version": version["version"],
                        "updated_at": version["updated_at"],
                        "source_task_id": version["source_task_id"],
                    }
                    for version in db.execute(
                        "SELECT version,updated_at,source_task_id FROM playbooks WHERE "
                        "playbook_id=? AND status IN ('current','earlier') ORDER BY version DESC",
                        (playbook_id,),
                    )
                ]
            return result

    # The person's side -----------------------------------------------------------------------

    def list(self, bot_id: str) -> list[dict]:
        """The owl's playbooks for its settings, last updated first."""
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            earlier = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT playbook_id,count(*) FROM playbooks WHERE bot_id=? "
                    "AND status='earlier' GROUP BY playbook_id",
                    (bot_id,),
                )
            }
            return [
                {
                    "id": row["playbook_id"],
                    "name": row["name"],
                    "when_to_use": row["when_to_use"],
                    "version": row["version"],
                    # Undo brings back the version before; with none, it forgets the playbook.
                    "earlier_versions": earlier.get(row["playbook_id"], 0),
                    "source_task_id": row["source_task_id"],
                    "updated_at": row["updated_at"],
                }
                for row in _current(db, bot_id)
            ]

    def undo(self, bot_id: str, playbook_id: str, data: PlaybookUndo | None = None) -> dict:
        """Take back the current version: the one before comes back, or the playbook goes."""
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            row = db.execute(
                "SELECT * FROM playbooks WHERE bot_id=? AND playbook_id=? AND status='current'",
                (bot_id, playbook_id),
            ).fetchone()
            if row is None:
                raise NotFound("Playbook not found.")
            if data is not None and data.version is not None and data.version != row["version"]:
                raise Conflict("This playbook changed since. Look at it again before undoing.")
            db.execute("DELETE FROM playbooks WHERE id=?", (row["id"],))
            restored = db.execute(
                "SELECT id,version FROM playbooks WHERE playbook_id=? AND status='earlier' "
                "ORDER BY version DESC LIMIT 1",
                (playbook_id,),
            ).fetchone()
            if restored is not None:
                db.execute(
                    "UPDATE playbooks SET status='current',updated_at=? WHERE id=?",
                    (time.time(), restored["id"]),
                )
            return {
                "id": playbook_id,
                "undone_version": row["version"],
                "restored_version": restored["version"] if restored is not None else None,
            }

    def delete(self, bot_id: str, playbook_id: str) -> None:
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            result = db.execute(
                "DELETE FROM playbooks WHERE bot_id=? AND playbook_id=? AND status<>'held'",
                (bot_id, playbook_id),
            )
            if not result.rowcount:
                raise NotFound("Playbook not found.")
