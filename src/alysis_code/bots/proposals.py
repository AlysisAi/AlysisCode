"""Durable, explicitly approved follow-ups in the bot's continuous conversation."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator

from .models import Input, TaskInput
from .store import Conflict, LimitReached, NotFound, Store, uid


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
        if value is None:
            return None
        if not isinstance(value, str) or any(ord(char) < 33 for char in value):
            raise ValueError("Use an HTTP(S) website origin without whitespace.")
        url = AnyHttpUrl(value)
        if "*" in url.host:
            raise ValueError("Use one exact website origin, without wildcards.")
        if url.username is not None or url.password is not None or url.path not in (None, "/"):
            raise ValueError("Use a website origin without credentials or a page path.")
        if url.query is not None or url.fragment is not None:
            raise ValueError("Use a website origin without a query or fragment.")
        return str(url).rstrip("/")


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
