"""Durable, explicitly approved follow-ups in the bot's continuous conversation."""

from __future__ import annotations

import json
import time
import unicodedata
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, field_validator, model_validator

from .models import BotInput, Input, TaskInput
from .routine_access import website_origin
from .scheduling import ScheduleInput, ScheduleRequest, next_run
from .store import Conflict, LimitReached, NotFound, Store, encode, uid


class ActionProposal(Input):
    question: str = Field(min_length=1, max_length=500)
    instructions: str = Field(
        min_length=1,
        max_length=4000,
        description="Complete work to perform after approval. Preserve the user's exact source values, constraints, requested outputs and execution preferences, including a request to work in the background. Do not shorten or drop supplied values to fit an assumed field mapping; ask for clarification first when the mapping is unclear.",
    )
    offline_computer: bool = Field(
        default=False,
        strict=True,
        description="Set true when the work requires commands, applications or generated binary files on the private offline computer. False requests no computer permission, regardless of the question text. No internet or personal account access.",
    )
    browser_origin: str | None = Field(
        default=None,
        max_length=2048,
        description="Exact website origin (scheme://host[:port], no path/query) only when website interaction is needed. Otherwise omit this field or use JSON null, never a string or invented website. User approval permits interaction on this site for this task only.",
    )

    @field_validator("browser_origin", mode="before")
    @classmethod
    def validate_origin(cls, value: str | None) -> str | None:
        # One rule for every website origin a person approves, here and for routines.
        return None if value is None else website_origin(value)


class ActionRequest(ActionProposal):
    """Model calls must explicitly choose whether execution permission is needed."""

    offline_computer: bool = Field(
        strict=True, description=ActionProposal.model_fields["offline_computer"].description
    )


class ActionDecision(Input):
    action: Literal["accept", "dismiss", "later"]
    expected_revision: int = Field(ge=1)
    browser_origin: str | None = Field(default=None, max_length=2048)
    offline_computer: bool = Field(default=False, strict=True)


class ProposalStore:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    @staticmethod
    def _latest_user_task(db, bot_id: str):
        row = db.execute(
            "SELECT id FROM tasks t WHERE bot_id=? AND NOT EXISTS (SELECT 1 FROM schedule_runs r WHERE r.task_id=t.id) AND NOT EXISTS (SELECT 1 FROM trigger_deliveries d WHERE d.task_id=t.id) ORDER BY created_at DESC,id DESC LIMIT 1",
            (bot_id,),
        ).fetchone()
        return row[0] if row else None

    def _refresh(self, db, bot: dict):
        latest = self._latest_user_task(db, bot["id"])
        db.execute(
            """UPDATE action_proposals SET status='outdated',revision=revision+1
            WHERE bot_id=? AND status='pending' AND
            (anchor_task_id IS DISTINCT FROM ? OR bot_updated_at!=? OR source_task_id IN
             (SELECT id FROM tasks WHERE status IN ('cancelled','failed','interrupted')))""",
            (bot["id"], latest, bot["updated_at"]),
        )

    def list(self, bot_id: str) -> list[dict]:
        with self.store.connect(write=True) as db:
            bot = self.store._bot(db, self.owner, bot_id)
            self._refresh(db, bot)
            return [
                dict(r)
                for r in db.execute(
                    """SELECT p.*,t.status='completed' AS ready FROM action_proposals p
                JOIN tasks t ON t.id=p.source_task_id WHERE p.bot_id=? ORDER BY p.created_at,p.id""",
                    (bot_id,),
                )
            ]

    def propose(self, task: dict, data: ActionProposal) -> dict:
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            bot = self.store._bot(db, self.owner, source["bot_id"])
            if not self.store.executing(db, source):
                raise Conflict("This task is no longer preparing a reply.")
            self._refresh(db, bot)
            old = db.execute(
                "SELECT * FROM action_proposals WHERE source_task_id=?", (source["id"],)
            ).fetchone()
            if old:
                # A withdrawn, unpublished draft is still editable by its own
                # running turn. Never revive a user's decision or a draft made
                # stale by newer instructions/configuration. The unique source
                # task owns both this row and its withdrawal receipt.
                restore = (
                    old["status"] == "outdated"
                    and source["status"] == "running"
                    and old["anchor_task_id"] == self._latest_user_task(db, bot["id"])
                    and old["bot_updated_at"] == bot["updated_at"]
                    and db.execute(
                        "SELECT 1 FROM events WHERE task_id=? AND kind='action_proposal_withdrawn' LIMIT 1",
                        (source["id"],),
                    ).fetchone()
                    is not None
                )
                if (
                    restore
                    and db.execute(
                        "SELECT count(*) FROM action_proposals WHERE bot_id=? AND status='pending'",
                        (bot["id"],),
                    ).fetchone()[0]
                    >= 3
                ):
                    raise LimitReached("This bot already has three pending suggestions.")
                if (
                    restore
                    or old["question"] != data.question
                    or old["instructions"] != data.instructions
                    or old["browser_origin"] != data.browser_origin
                    or bool(old["offline_computer"]) != data.offline_computer
                ):
                    if (old["status"] != "pending" and not restore) or source[
                        "status"
                    ] != "running":
                        raise Conflict(
                            "This task's card can no longer be changed: it was already decided or "
                            "withdrawn, or this task is now working in the background. Do not call "
                            "propose_action again in this task. Reply to the person with what is "
                            "done and what still waits for them."
                        )
                    # The UI cannot accept a proposal until its source turn
                    # completes. Keep one card and advance its revision so even
                    # a stale client cannot approve earlier text or scopes.
                    db.execute(
                        """UPDATE action_proposals SET question=?,instructions=?,browser_origin=?,
                        offline_computer=?,status='pending',revision=revision+1,updated_at=? WHERE id=?""",
                        (
                            data.question,
                            data.instructions,
                            data.browser_origin,
                            data.offline_computer,
                            time.time(),
                            old["id"],
                        ),
                    )
                    self.store._event(
                        db,
                        source["id"],
                        "action_proposal_restored" if restore else "action_proposal_corrected",
                        {"proposal_id": old["id"]},
                    )
                    return dict(
                        db.execute(
                            "SELECT * FROM action_proposals WHERE id=?", (old["id"],)
                        ).fetchone()
                    )
                return dict(old)
            if db.execute(
                "SELECT 1 FROM action_proposals WHERE bot_id=? AND instructions=? AND status IN ('pending','dismissed')",
                (bot["id"], data.instructions),
            ).fetchone():
                raise Conflict(
                    "That suggestion is already pending or was dismissed. Do not repeat it."
                )
            if (
                db.execute(
                    "SELECT count(*) FROM action_proposals WHERE bot_id=? AND status='pending'",
                    (bot["id"],),
                ).fetchone()[0]
                >= 3
            ):
                raise LimitReached("This bot already has three pending suggestions.")
            proposal_id, now = uid(), time.time()
            db.execute(
                """INSERT INTO action_proposals
                (id,bot_id,source_task_id,anchor_task_id,question,instructions,status,revision,
                 bot_updated_at,visible_after,task_id,created_at,updated_at,browser_origin,offline_computer)
                VALUES(?,?,?,?,?,?,'pending',1,?,?,NULL,?,?,?,?)""",
                (
                    proposal_id,
                    bot["id"],
                    source["id"],
                    self._latest_user_task(db, bot["id"]),
                    data.question,
                    data.instructions,
                    bot["updated_at"],
                    now,
                    now,
                    now,
                    data.browser_origin,
                    data.offline_computer,
                ),
            )
            self.store._event(db, source["id"], "action_proposed", {"proposal_id": proposal_id})
            return dict(
                db.execute("SELECT * FROM action_proposals WHERE id=?", (proposal_id,)).fetchone()
            )

    def for_task(self, task: dict) -> dict | None:
        with self.store.connect() as db:
            source = self.store._task(db, self.owner, task["id"])
            row = db.execute(
                "SELECT * FROM action_proposals WHERE source_task_id=? AND bot_id=?",
                (source["id"], source["bot_id"]),
            ).fetchone()
            return dict(row) if row else None

    def withdraw(self, task: dict) -> dict:
        """Retract this executing turn's own draft, never a user's decision."""
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, source):
                raise Conflict("This task is no longer preparing a reply.")
            row = db.execute(
                "SELECT * FROM action_proposals WHERE source_task_id=? AND bot_id=?",
                (source["id"], source["bot_id"]),
            ).fetchone()
            if row is None:
                return {"state": "none", "message": "This turn has no proposal to withdraw."}
            if row["status"] not in {"pending", "outdated"}:
                raise Conflict("A decided proposal cannot be withdrawn.")
            if row["status"] == "pending":
                db.execute(
                    "UPDATE action_proposals SET status='outdated',revision=revision+1,updated_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
                self.store._event(
                    db, source["id"], "action_proposal_withdrawn", {"proposal_id": row["id"]}
                )
            current = db.execute(
                "SELECT * FROM action_proposals WHERE id=?", (row["id"],)
            ).fetchone()
            return {
                "id": current["id"],
                "state": current["status"],
                "revision": current["revision"],
                "message": "This proposal is no longer actionable. No work was executed or cancelled.",
            }

    def browser_origins(self, task: dict) -> tuple[str, ...]:
        """Authority comes only from the user's accepted, task-bound proposal.

        Recheck execution/cancellation and bot ownership on every action. Grants
        do not survive task completion or transfer to another task/peer/routine.
        This does not relax the browser's private-network destination policy.
        """
        with self.store.connect() as db:
            current = self.store._task(db, self.owner, task["id"])
            if current["bot_id"] != task["bot_id"] or not self.store.executing(db, current):
                return ()
            row = db.execute(
                """SELECT browser_origin FROM action_proposals
                WHERE task_id=? AND bot_id=? AND status='accepted'""",
                (current["id"], current["bot_id"]),
            ).fetchone()
            return (row["browser_origin"],) if row and row["browser_origin"] else ()

    def offline_computer_allowed(self, task: dict) -> bool:
        """Task-scoped authority never comes from model arguments or peer text."""
        with self.store.connect() as db:
            current = self.store._task(db, self.owner, task["id"])
            if current["bot_id"] != task["bot_id"] or not self.store.executing(db, current):
                return False
            row = db.execute(
                "SELECT offline_computer FROM action_proposals WHERE task_id=? AND bot_id=? AND status='accepted'",
                (current["id"], current["bot_id"]),
            ).fetchone()
            return bool(row and row["offline_computer"])

    def approve_by_request(self, task: dict, data: ActionProposal) -> dict:
        """Let the person's own request approve the owl's private offline computer for it.

        A request the person makes in this owl's chat is their permission for the work it needs
        inside the owl's private, offline computer: no internet, personal accounts or messages to
        anyone. Asking again with a Do it card adds a step without adding safety. Only a current
        direct request qualifies, including while it continues in the background
        (``Store.direct_request``: not a routine run, activation, peer exchange or a turn waiting
        for an answer); website input still needs a card, because it acts outside.

        The grant is an accepted proposal bound to this same task, so ``offline_computer_allowed``,
        Stop and task end apply unchanged, and the chat keeps a record of what the owl used.
        """
        if data.browser_origin is not None or not data.offline_computer:
            raise ValueError("Only the private offline computer is approved by the request itself.")
        with self.store.connect(write=True) as db:
            source = self.store.direct_request(
                db, self.owner, task["bot_id"], task["id"], background=True
            )
            bot = self.store._bot(db, self.owner, source["bot_id"])
            now = time.time()
            old = db.execute(
                "SELECT * FROM action_proposals WHERE source_task_id=?", (source["id"],)
            ).fetchone()
            if old:
                # Only this turn's own undecided draft becomes the grant; never a decision the
                # person made on a card.
                if old["status"] not in {"pending", "outdated"}:
                    raise Conflict(
                        "The person already decided on this task's card. Follow that decision."
                    )
                proposal_id = old["id"]
                db.execute(
                    """UPDATE action_proposals SET question=?,instructions=?,browser_origin=NULL,
                    offline_computer=1,status='accepted',task_id=?,revision=revision+1,updated_at=?
                    WHERE id=?""",
                    (data.question, data.instructions, source["id"], now, proposal_id),
                )
            else:
                proposal_id = uid()
                db.execute(
                    """INSERT INTO action_proposals
                    (id,bot_id,source_task_id,anchor_task_id,question,instructions,status,revision,
                     bot_updated_at,visible_after,task_id,created_at,updated_at,browser_origin,
                     offline_computer)
                    VALUES(?,?,?,?,?,?,'accepted',1,?,?,?,?,?,NULL,1)""",
                    (
                        proposal_id,
                        bot["id"],
                        source["id"],
                        self._latest_user_task(db, bot["id"]),
                        data.question,
                        data.instructions,
                        bot["updated_at"],
                        now,
                        source["id"],
                        now,
                        now,
                    ),
                )
            self.store._event(
                db, source["id"], "action_approved_by_request", {"proposal_id": proposal_id}
            )
            return dict(
                db.execute("SELECT * FROM action_proposals WHERE id=?", (proposal_id,)).fetchone()
            )

    def already_granted(self, task: dict, data: ActionProposal) -> bool:
        """True when this task already holds every permission the proposal asks for.

        After Do it, the follow-up task carries the accepted website or offline-computer grant.
        A model that proposes the same permission again would put a second card in front of
        the person for what they just approved. Nothing is granted here; only existing grants
        are read, with the same checks as browser_origins and offline_computer_allowed.
        """
        wants_site, wants_computer = data.browser_origin, bool(data.offline_computer)
        if not wants_site and not wants_computer:
            return False
        return (not wants_site or wants_site in self.browser_origins(task)) and (
            not wants_computer or self.offline_computer_allowed(task)
        )

    def accepted_source(self, task: dict) -> dict | None:
        """Retain the source request independently of compacted chat history.

        This is task provenance, never a grant. Execution authority still comes
        only from the accepted proposal and the separate checks above.
        """
        with self.store.connect() as db:
            current = self.store._task(db, self.owner, task["id"])
            if current["bot_id"] != task["bot_id"]:
                return None
            row = db.execute(
                """SELECT p.source_task_id,t.message AS original_request
                   FROM action_proposals p JOIN tasks t ON t.id=p.source_task_id
                   WHERE p.task_id=? AND p.bot_id=? AND p.status='accepted'
                   AND t.owner=? AND t.bot_id=?""",
                (current["id"], current["bot_id"], self.owner, current["bot_id"]),
            ).fetchone()
            return dict(row) if row else None

    def decide(self, bot_id: str, proposal_id: str, data: ActionDecision) -> dict:
        with self.store.connect(write=True) as db:
            bot = self.store._bot(db, self.owner, bot_id)
            self._refresh(db, bot)
            row = db.execute(
                "SELECT * FROM action_proposals WHERE id=? AND bot_id=?", (proposal_id, bot_id)
            ).fetchone()
            if not row:
                raise NotFound("Suggestion not found.")
            if (row["status"] == "accepted" and data.action == "accept") or (
                row["status"] == "dismissed" and data.action == "dismiss"
            ):
                return dict(row)  # Uncertain client delivery cannot start a second task.
            if row["status"] != "pending":
                raise Conflict(
                    "This suggestion is no longer current. Ask the bot to reconsider it."
                )
            if row["revision"] != data.expected_revision:
                raise Conflict("This suggestion changed. Review its current state first.")
            if data.action == "accept" and row["browser_origin"] != data.browser_origin:
                raise Conflict("Review the website permission in a current app before approving.")
            if data.action == "accept" and bool(row["offline_computer"]) != data.offline_computer:
                raise Conflict(
                    "Review the offline computer permission in a current app before approving."
                )
            source = self.store._task(db, self.owner, row["source_task_id"])
            if source["status"] != "completed":
                raise Conflict("Wait for the bot to finish explaining this suggestion.")
            now = time.time()
            task_id = None
            status = "pending"
            visible_after = row["visible_after"]
            if data.action == "accept":
                chat = self.store._ensure_chat(db, self.owner, bot_id, bot["name"])
                task = self.store._enqueue(
                    db,
                    self.owner,
                    chat["id"],
                    TaskInput(message=row["instructions"], request_id="proposal_" + proposal_id),
                )
                task_id, status = task["id"], "accepted"
            elif data.action == "dismiss":
                status = "dismissed"
            else:
                visible_after = now + 3600
            db.execute(
                "UPDATE action_proposals SET status=?,revision=revision+1,task_id=?,visible_after=?,updated_at=? WHERE id=?",
                (status, task_id, visible_after, now, proposal_id),
            )
            self.store._event(
                db, row["source_task_id"], "action_" + data.action, {"proposal_id": proposal_id}
            )
            return dict(
                db.execute("SELECT * FROM action_proposals WHERE id=?", (proposal_id,)).fetchone()
            )


def _one_line(value: str) -> str:
    if any(char in value for char in "\r\n\u2028\u2029"):
        raise ValueError("Write the purpose as one line.")
    return value


OneLine = Annotated[str, AfterValidator(_one_line)]


class SetupProposal(Input):
    """An owl's own setup, from the person's description of the job, for them to apply."""

    purpose: OneLine = Field(
        min_length=1,
        max_length=300,
        description="One line in the person's language: what this owl is for.",
    )
    instructions: str = Field(
        min_length=1,
        max_length=3000,
        description="A few short lines in the person's language: how you do the job each time, "
        "keeping every requirement, source, output and name the person gave.",
    )
    routine: ScheduleRequest | None = Field(
        default=None,
        description="Only when the job recurs on its own: when it runs and what each run does, as "
        "for propose_schedule. A JSON object, not a JSON-encoded string. Omit it (or null) "
        "otherwise.",
    )


class SetupDecision(Input):
    """The person's answer to a setup card: Apply (with their own changes) or Not now."""

    action: Literal["apply", "decline"]
    expected_revision: int = Field(ge=1)
    # Change…: the person's own wording, applied instead of the owl's.
    purpose: OneLine | None = Field(default=None, min_length=1, max_length=1000)
    instructions: str | None = Field(default=None, max_length=6000)
    include_routine: bool = True
    routine_prompt: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def _changes_only_apply(self) -> SetupDecision:
        if self.action == "decline" and (
            self.purpose is not None
            or self.instructions is not None
            or self.routine_prompt is not None
            or not self.include_routine
        ):
            raise ValueError("Only Apply takes changes.")
        return self


# A proposal whose turn stopped never became a card the person could answer.
_STOPPED = ("cancelled", "cancelling", "failed", "interrupted")


def _comparable(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _same_setup(row, purpose: str, instructions: str, routine: dict | None) -> bool:
    return (
        _comparable(row["purpose"]) == _comparable(purpose)
        and _comparable(row["instructions"]) == _comparable(instructions)
        and (json.loads(row["routine"]) if row["routine"] else None) == routine
    )


def _card(row, bot_updated_at: float) -> dict:
    """A setup proposal as its card shows it.

    A pending card is out of date once the owl's setup changed after it was made (the person
    edited it, or applied another), or once its turn stopped: applying it would overwrite newer
    edits, so it can only be read.
    """
    status = row["status"]
    if status == "pending" and (
        row["bot_updated_at"] != bot_updated_at or row["source_status"] in _STOPPED
    ):
        status = "outdated"
    return {
        "id": row["id"],
        "source_task_id": row["source_task_id"],
        "revision": row["revision"],
        "status": status,
        "ready": row["source_status"] == "completed",
        "purpose": row["purpose"],
        "instructions": row["instructions"],
        "routine": json.loads(row["routine"]) if row["routine"] else None,
        "before": {"purpose": row["before_purpose"], "instructions": row["before_instructions"]},
        "applied": json.loads(row["applied"]) if row["applied"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_CARDS = (
    "SELECT p.*,t.status AS source_status,b.updated_at AS current_bot_updated_at "
    "FROM setup_proposals p JOIN tasks t ON t.id=p.source_task_id JOIN bots b ON b.id=p.bot_id "
    "WHERE p.bot_id=?"
)


def setup_cards(db, bot_id: str, *, only=None) -> dict[str, dict]:
    """Each turn's setup card by its task id, for the conversation's shared task decoration."""
    scope, values = "", (bot_id,)
    if only is not None:
        if not only:
            return {}
        scope = f" AND p.source_task_id IN ({','.join('?' * len(only))})"
        values = (bot_id, *only)
    return {
        row["source_task_id"]: _card(row, row["current_bot_updated_at"])
        for row in db.execute(_CARDS + scope, values)
    }


class SetupProposals:
    """An owl's own setup: proposed by the owl in its chat, changed only by the person's Apply."""

    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    @staticmethod
    def _read(db, bot_id: str, proposal_id: str) -> dict:
        row = db.execute(_CARDS + " AND p.id=?", (bot_id, proposal_id)).fetchone()
        return _card(row, row["current_bot_updated_at"])

    def recent(self, bot_id: str, limit: int = 5) -> list[dict]:
        """This owl's latest setup proposals and what the person did with them, newest first."""
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            rows = db.execute(
                _CARDS + " ORDER BY p.created_at DESC,p.id DESC LIMIT ?", (bot_id, limit)
            ).fetchall()
            return [_card(row, row["current_bot_updated_at"]) for row in rows]

    def propose(self, task: dict, data: SetupProposal) -> dict:
        """Save the owl's setup as one card under its reply; nothing about the owl changes yet.

        Only the person's own request in this chat can lead to one: never a routine run, an
        activation, a first hello or a peer. A turn may correct its own card while it works.
        A newer card replaces one still waiting, and a setup the person said Not now to is
        never put in front of them again.
        """
        with self.store.connect(write=True) as db:
            try:
                source = self.store.direct_request(db, self.owner, task["bot_id"], task["id"])
            except Conflict:
                raise Conflict(
                    "Only the person's own request in this chat, while you answer it, can lead "
                    "to a setup suggestion."
                ) from None
            bot = self.store._bot(db, self.owner, source["bot_id"])
            now = time.time()
            routine = None
            if data.routine is not None:
                # A relative delay counts from the person's message, as for any routine.
                timing = data.routine.absolute_timing(source["created_at"])
                if next_run(timing, now) is None:
                    raise Conflict("Choose a future time for this one-time routine.")
                routine = {
                    "name": data.routine.name,
                    "prompt": data.routine.prompt,
                    "timing": timing.model_dump(mode="json"),
                }
            if (
                routine is None
                and _comparable(data.purpose) == _comparable(bot["purpose"])
                and _comparable(data.instructions) == _comparable(bot["instructions"])
            ):
                raise Conflict("This is already your setup, so there is nothing to apply.")
            for declined in db.execute(
                "SELECT purpose,instructions,routine FROM setup_proposals "
                "WHERE bot_id=? AND status='declined'",
                (bot["id"],),
            ).fetchall():
                if _same_setup(declined, data.purpose, data.instructions, routine):
                    raise Conflict(
                        "The person chose Not now for this same setup. Do not suggest it again; "
                        "carry on with their request."
                    )
            values = (
                data.purpose,
                data.instructions,
                encode(routine) if routine else None,
                bot["purpose"],
                bot["instructions"],
                bot["updated_at"],
                now,
            )
            old = db.execute(
                "SELECT * FROM setup_proposals WHERE source_task_id=?", (source["id"],)
            ).fetchone()
            if old:
                if old["status"] != "pending":
                    raise Conflict(
                        "This turn's setup card was already answered. Do not call propose_setup "
                        "again in this turn."
                    )
                if not _same_setup(old, data.purpose, data.instructions, routine) or (
                    old["bot_updated_at"] != bot["updated_at"]
                ):
                    # One card per turn: its revision moves, so a card read earlier can't apply.
                    db.execute(
                        "UPDATE setup_proposals SET purpose=?,instructions=?,routine=?,"
                        "before_purpose=?,before_instructions=?,bot_updated_at=?,updated_at=?,"
                        "revision=revision+1 WHERE id=?",
                        (*values, old["id"]),
                    )
                    self.store._event(
                        db, source["id"], "setup_proposal_corrected", {"proposal_id": old["id"]}
                    )
                return self._read(db, bot["id"], old["id"])
            db.execute(
                "UPDATE setup_proposals SET status='outdated',revision=revision+1,updated_at=? "
                "WHERE bot_id=? AND status='pending'",
                (now, bot["id"]),
            )
            proposal_id = uid()
            db.execute(
                "INSERT INTO setup_proposals(id,bot_id,source_task_id,purpose,instructions,routine,"
                "before_purpose,before_instructions,bot_updated_at,created_at,status,revision,"
                "applied,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',1,NULL,?)",
                (proposal_id, bot["id"], source["id"], *values, now),
            )
            self.store._event(db, source["id"], "setup_proposed", {"proposal_id": proposal_id})
            return self._read(db, bot["id"], proposal_id)

    def decide(self, bot_id: str, proposal_id: str, data: SetupDecision) -> dict:
        """Apply the setup in one step, or set it aside, only from the card the person saw.

        Apply changes the purpose and instructions through the owl's own update path and saves
        the routine as a draft from that turn, which the person turns on as any routine the owl
        suggests; all of it lands together or not at all. The same answer sent twice (a double
        click, a second window) returns the change already made. A card whose revision moved, or
        made before the owl's setup last changed, is refused, so it never overwrites newer edits.
        """
        with self.store.connect(write=True) as db:
            bot = self.store._bot(db, self.owner, bot_id)
            row = db.execute(_CARDS + " AND p.id=?", (bot_id, proposal_id)).fetchone()
            if row is None:
                raise NotFound("Setup suggestion not found.")
            done = "applied" if data.action == "apply" else "declined"
            if row["status"] == done:
                return _card(row, bot["updated_at"])
            if _card(row, bot["updated_at"])["status"] != "pending":
                raise Conflict(
                    "This setup suggestion is no longer current: the owl's setup changed since, "
                    "or a newer suggestion replaced it."
                )
            if row["revision"] != data.expected_revision:
                raise Conflict("This setup suggestion changed. Review it again before deciding.")
            if row["source_status"] != "completed":
                raise Conflict("Wait for the owl to finish its reply.")
            now = time.time()
            applied = None
            if data.action == "apply":
                purpose = row["purpose"] if data.purpose is None else data.purpose
                instructions = (
                    row["instructions"] if data.instructions is None else data.instructions
                )
                # Every other setting stays as it is, including ones added after this card.
                current = {key: bot[key] for key in BotInput.model_fields if key in bot}
                self.store.save_bot(
                    self.owner,
                    BotInput(**{**current, "purpose": purpose, "instructions": instructions}),
                    bot_id,
                    within=db,
                )
                schedule_id = None
                routine = json.loads(row["routine"]) if row["routine"] else None
                if routine and data.include_routine:
                    schedule = self.store.save_schedule(
                        self.owner,
                        bot_id,
                        ScheduleInput.model_validate(
                            {
                                **routine,
                                "prompt": data.routine_prompt or routine["prompt"],
                                "enabled": False,
                            }
                        ),
                        source_task_id=row["source_task_id"],
                        accepted_card=True,
                        now=now,
                        within=db,
                    )
                    schedule_id = schedule["id"]
                applied = {
                    "purpose": purpose,
                    "instructions": instructions,
                    "schedule_id": schedule_id,
                }
            db.execute(
                "UPDATE setup_proposals SET status=?,applied=?,revision=revision+1,updated_at=? "
                "WHERE id=?",
                (done, encode(applied) if applied else None, now, proposal_id),
            )
            self.store._event(
                db, row["source_task_id"], "setup_" + done, {"proposal_id": proposal_id}
            )
            return self._read(db, bot_id, proposal_id)
