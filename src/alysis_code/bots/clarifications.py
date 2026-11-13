"""Persistent questions independent of the conversation's foreground work."""

from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .models import Input, TaskInput
from .store import Conflict, LimitReached, NotFound, Store, encode, uid


class AskQuestion(Input):
    question: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("options")
    @classmethod
    def valid_options(cls, values):
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("Options must contain 1–300 characters.")
        if len(set(values)) != len(values):
            raise ValueError("Options must be distinct.")
        return values


class QuestionReference(Input):
    question_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class ReadBrowserDraft(Input):
    question_id: str | None = Field(default=None, min_length=1, max_length=100)
    source_task_id: str | None = Field(default=None, min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def one_reference(self):
        if bool(self.question_id) == bool(self.source_task_id):
            raise ValueError("Supply either question_id or source_task_id.")
        return self


class QuestionDecision(Input):
    action: Literal["answer", "close"]
    expected_revision: int = Field(ge=1)
    answer: str = Field(default="", max_length=20000)

    @model_validator(mode="after")
    def validate_answer(self):
        if self.action == "answer" and not self.answer:
            raise ValueError("An answer is required.")
        if self.action == "close" and self.answer:
            raise ValueError("Closing a question does not supply an answer.")
        return self


class ClarificationStore:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    @staticmethod
    def public(row):
        return {**dict(row), "options": json.loads(row["options"])}

    @staticmethod
    def refresh(db, bot_id):
        # Finished drafts can still await answers; stopped/uncertain work cannot
        # be restarted by a stale question card. Unrelated messages do nothing.
        db.execute(
            "UPDATE clarifications SET status='closed',revision=revision+1,updated_at=? "
            "WHERE bot_id=? AND status='pending' AND source_task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('cancelled','cancelling','failed','interrupted'))",
            (time.time(), bot_id),
        )

    def list(self, bot_id):
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            self.refresh(db, bot_id)
            return [
                self.public(r)
                for r in db.execute(
                    "SELECT * FROM clarifications WHERE bot_id=? ORDER BY created_at,id", (bot_id,)
                )
            ]

    def handoff_approved(self, task):
        """The accepted continuation owns its original request's pending question."""
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, current):
                return
            proposal = db.execute(
                "SELECT source_task_id FROM action_proposals WHERE task_id=? AND bot_id=? AND status='accepted'",
                (task["id"], task["bot_id"]),
            ).fetchone()
            if proposal:
                db.execute(
                    "INSERT INTO browser_task_drafts SELECT ?,content,captured_at FROM browser_task_drafts WHERE task_id=? ON CONFLICT(task_id) DO NOTHING",
                    (task["id"], proposal["source_task_id"]),
                )
                db.execute(
                    "UPDATE clarifications SET source_task_id=?,revision=revision+1,updated_at=? WHERE source_task_id=? AND bot_id=? AND status='pending'",
                    (task["id"], time.time(), proposal["source_task_id"], task["bot_id"]),
                )

    def ask(self, task, data: AskQuestion):
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, source):
                raise Conflict("This task is no longer asking a question.")
            self.refresh(db, source["bot_id"])
            old = db.execute(
                "SELECT * FROM clarifications WHERE source_task_id=?", (source["id"],)
            ).fetchone()
            if old:
                if old["question"] != data.question or json.loads(old["options"]) != data.options:
                    raise Conflict(
                        "This task already asked a question. Use its saved answer or leave it pending."
                    )
                return self.public(old)
            if (
                db.execute(
                    "SELECT count(*) FROM clarifications WHERE bot_id=? AND status='pending'",
                    (source["bot_id"],),
                ).fetchone()[0]
                >= 3
            ):
                raise LimitReached(
                    "Three questions are already pending. Use those before asking another."
                )
            question_id, now = uid(), time.time()
            db.execute(
                "INSERT INTO clarifications VALUES(?,?,?,?,?,'pending',1,NULL,NULL,0,?,?)",
                (
                    question_id,
                    source["bot_id"],
                    source["id"],
                    data.question,
                    encode(data.options),
                    now,
                    now,
                ),
            )
            self.store._event(
                db, source["id"], "clarification_requested", {"question_id": question_id}
            )
            return self.public(
                db.execute("SELECT * FROM clarifications WHERE id=?", (question_id,)).fetchone()
            )

    def decide(self, bot_id, question_id, data: QuestionDecision, *, answering_task=None):
        with self.store.connect(write=True) as db:
            bot = self.store._bot(db, self.owner, bot_id)
            self.refresh(db, bot_id)
            row = db.execute(
                "SELECT * FROM clarifications WHERE id=? AND bot_id=?", (question_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("Question not found.")
            # Re-delivering an identical answer never creates another task.
            if (
                row["status"] == "answered"
                and data.action == "answer"
                and row["answer"] == data.answer
            ):
                return self.public(row)
            if row["status"] == "closed" and data.action == "close":
                return self.public(row)
            if row["status"] != "pending" or row["revision"] != data.expected_revision:
                raise Conflict("This question changed or was closed. Review its current state.")
            source = self.store._task(db, self.owner, row["source_task_id"])
            task_id = None
            delivered = source["status"] in ("queued", "running", "waiting")
            if data.action == "answer":
                if answering_task:
                    self.store.direct_request(db, self.owner, bot_id, answering_task["id"])
                    task_id = answering_task["id"]
                    if task_id == source["id"]:
                        raise Conflict("A bot cannot answer its own pending question.")
                else:
                    chat = self.store._ensure_chat(db, self.owner, bot_id, bot["name"])
                    task_id = self.store._enqueue(
                        db,
                        self.owner,
                        chat["id"],
                        TaskInput(
                            message=data.answer,
                            request_id="clarification_" + question_id,
                        ),
                    )["id"]
            if data.action == "answer" and delivered:
                # The same background task consumes this exact answer before
                # any further writes. Its existing instruction fence handles a
                # correction arriving between a model response and dispatch.
                if db.execute(
                    "SELECT 1 FROM background_turns WHERE task_id=?", (source["id"],)
                ).fetchone():
                    db.execute(
                        "INSERT INTO background_instructions(task_id,source_task_id,content) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                        (source["id"], task_id, data.answer),
                    )
                job = db.execute(
                    "SELECT id FROM coding_jobs WHERE task_id=? AND status IN ('queued','running')",
                    (source["id"],),
                ).fetchone()
                if job:
                    revision = (
                        db.execute(
                            "SELECT count(*) FROM coding_instructions WHERE job_id=?", (job["id"],)
                        ).fetchone()[0]
                        + 1
                    )
                    if revision > 16:
                        raise LimitReached(
                            "The task's correction allowance is full. Stop it before continuing with a new task."
                        )
                    db.execute(
                        "INSERT INTO coding_instructions VALUES(?,?,?,?,?)",
                        (job["id"], revision, task_id, data.answer, time.time()),
                    )
                    self.store._event(
                        db,
                        source["id"],
                        "instructions_queued",
                        {"revision": revision, "source_task_id": task_id},
                    )
            db.execute(
                "UPDATE clarifications SET status=?,revision=revision+1,answer=?,answer_task_id=?,"
                "delivered_to_source=?,updated_at=? WHERE id=?",
                (
                    "answered" if data.action == "answer" else "closed",
                    data.answer or None,
                    task_id,
                    delivered,
                    time.time(),
                    question_id,
                ),
            )
            self.store._event(
                db, source["id"], "clarification_" + data.action, {"question_id": question_id}
            )
            return self.public(
                db.execute("SELECT * FROM clarifications WHERE id=?", (question_id,)).fetchone()
            )

    def context(self, task):
        from .proposals import ProposalStore

        proposals = ProposalStore(self.store, self.owner)
        accepted = proposals.accepted_source(task)
        request_ids = {task["id"]}
        if accepted:
            request_ids.add(accepted["source_task_id"])
        questions = self.list(task["bot_id"])
        relevant = [
            q
            for q in questions
            if q["status"] == "pending"
            or request_ids.intersection((q["source_task_id"], q["answer_task_id"]))
        ]
        for q in relevant:
            source = self.store.task(self.owner, q["source_task_id"])
            q["source_status"] = source["status"]
            draft = self.browser_draft_reference(source)
            if draft and q["status"] != "closed":
                q["browser_draft"] = dict(draft)
            if q["answer_task_id"] in request_ids:
                accepted = proposals.accepted_source(source)
                q["original_request"] = (
                    accepted["original_request"] if accepted else source["message"]
                )
                q["report_only"] = bool(q["delivered_to_source"]) or source["status"] in (
                    "cancelled",
                    "cancelling",
                    "failed",
                    "interrupted",
                )
        return relevant

    def browser_draft_reference(self, task):
        with self.store.connect() as db:
            source = self.store._task(db, self.owner, task["id"])
            if source["bot_id"] != task["bot_id"]:
                raise NotFound("Browser draft not found.")
            row = db.execute(
                "SELECT task_id AS source_task_id,byte_length(content) AS bytes,captured_at FROM browser_task_drafts WHERE task_id=?",
                (source["id"],),
            ).fetchone()
            return dict(row) if row else None

    def needs_browser_draft(self, task):
        with self.store.connect() as db:
            self.store._task(db, self.owner, task["id"])
            return bool(
                db.execute(
                    "SELECT 1 FROM clarifications WHERE source_task_id=? AND status='pending' UNION ALL "
                    "SELECT 1 FROM action_proposals WHERE source_task_id=? AND status='pending' UNION ALL "
                    "SELECT 1 FROM bot_action_reviews WHERE task_id=? AND status='pending'",
                    (task["id"], task["id"], task["id"]),
                ).fetchone()
            )

    def save_browser_draft(self, task, draft):
        content = encode(draft)
        # A bounded observation envelope, not a per-user storage allowance.
        if len(content.encode()) > 1024 * 1024:
            raise LimitReached("Browser draft is too large to checkpoint safely.")
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, source):
                raise Conflict("This task no longer owns the browser draft.")
            db.execute(
                "INSERT INTO browser_task_drafts VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET content=excluded.content,captured_at=excluded.captured_at",
                (source["id"], content, time.time()),
            )
            return {"saved": True, "source_task_id": source["id"], "complete": draft["complete"]}

    def read_browser_draft(self, task, args):
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, task["bot_id"])
            self.refresh(db, task["bot_id"])
            source_id = args.source_task_id
            if args.question_id:
                question = db.execute(
                    "SELECT source_task_id FROM clarifications WHERE id=? AND bot_id=? AND status IN ('pending','answered')",
                    (args.question_id, task["bot_id"]),
                ).fetchone()
                if question is None:
                    raise NotFound("No available browser draft for this question and bot.")
                source_id = question["source_task_id"]
            source = self.store._task(db, self.owner, source_id)
            if source["bot_id"] != task["bot_id"] or source["status"] in (
                "cancelled",
                "cancelling",
                "failed",
                "interrupted",
            ):
                raise NotFound("No available browser draft for this task and bot.")
            row = db.execute(
                "SELECT content,captured_at FROM browser_task_drafts WHERE task_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise NotFound("No available browser draft for this question and bot.")
            raw = row["content"]
            if args.offset > len(raw):
                raise ValueError("Offset exceeds the saved draft length.")
            end = min(args.offset + 4000, len(raw))
            return {
                "content": raw[args.offset : end],
                "offset": args.offset,
                "next_offset": end if end < len(raw) else None,
                "characters": len(raw),
                "captured_at": row["captured_at"],
                "untrusted_page_data": True,
                "message": "Historical form data, not a live page or permission. Read all needed chunks. Obtain current website approval, inspect the current page, and restore only still-relevant fields using fresh observations. Do not repeat a submission or assume omitted state survived.",
            }

    def mark_browser_draft_stale(self, task):
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            row = db.execute(
                "SELECT task_id,content FROM browser_task_drafts WHERE task_id=?",
                (source["id"],),
            ).fetchone()
            if row:
                saved = json.loads(row["content"])
                saved.update(
                    complete=False,
                    refresh_failed=True,
                    warning="The final page state could not be checkpointed. These earlier captured values may be stale; verify before restoring.",
                )
                db.execute(
                    "UPDATE browser_task_drafts SET content=? WHERE task_id=?",
                    (encode(saved), row["task_id"]),
                )
