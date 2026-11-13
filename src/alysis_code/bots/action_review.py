"""Bots adapter for the shared action authority, with persistent human handoff."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import Field, field_validator

from ..agent.action_approval import decide_action_approval
from ..agent.approval_review import ApprovalReviewer, approval_review_cancellation_scope
from ..agent.errors import ApprovalReviewDeniedError
from ..cancellation import raise_if_cancelled
from ..read_only_commands import is_read_only_command
from ..surface import ApprovalRequest
from .models import Input
from .store import Conflict, NotFound, encode, uid


class ApprovalRequired(Exception):
    """No action dispatched. Unwind execution and persist a resumable pause."""


class ApprovalDecision(Input):
    action: Literal["allow", "always_allow", "deny"]
    expected_revision: int = Field(ge=1)


class ReviewSettings(Input):
    mode: Literal["auto", "review"]


class RuleInput(Input):
    effect: Literal["allow", "ask"]
    description: str = Field(min_length=1, max_length=4000)

    @field_validator("description")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Describe when this rule should apply.")
        return value.strip()


class RuleRevision(Input):
    expected_revision: int = Field(ge=1, strict=True)


class RuleUpdate(RuleInput, RuleRevision):
    pass


def action_identity(value):
    """Exact action scope; observation nonces carry no authority across turns."""
    value = json.loads(encode(value))
    value["arguments"].pop("source_id", None)
    observed = value.get("observed_context", {})
    if isinstance(observed.get("screenshot"), str):
        observed["screenshot"] = {
            "sha256": hashlib.sha256(observed["screenshot"].encode()).hexdigest()
        }
    return value


class ActionReviews:
    def __init__(self, store, owner):
        self.store, self.owner = store, owner

    def settings(self, bot_id, data=None):
        with self.store.connect(write=data is not None) as db:
            self.store._bot(db, self.owner, bot_id)
            if data is not None:
                db.execute(
                    "INSERT INTO bot_review_settings VALUES(?,?) ON CONFLICT(bot_id) DO UPDATE SET mode=excluded.mode",
                    (bot_id, data.mode),
                )
            row = db.execute(
                "SELECT mode FROM bot_review_settings WHERE bot_id=?", (bot_id,)
            ).fetchone()
            return {"mode": row["mode"] if row else "auto", "rules": self._rules(db, bot_id)}

    @staticmethod
    def _rules(db, bot_id):
        return [
            {**dict(row), "action": json.loads(row["action"]) if row["action"] else None}
            for row in db.execute(
                "SELECT * FROM bot_action_rules WHERE bot_id=? AND deleted=0 ORDER BY created_at,id",
                (bot_id,),
            )
        ]

    def rules(self, bot_id):
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            return self._rules(db, bot_id)

    def save_rule(self, bot_id, data, rule_id=None):
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            now = time.time()
            if rule_id is None:
                rule_id = uid()
                db.execute(
                    "INSERT INTO bot_action_rules VALUES(?,?,?,?,NULL,NULL,1,0,?,?)",
                    (rule_id, bot_id, data.effect, data.description, now, now),
                )
            else:
                row = self._rule(db, bot_id, rule_id, data.expected_revision)
                # Editing replaces an exact-action grant with the conditions
                # explicitly written by the user. It never silently widens it.
                db.execute(
                    "UPDATE bot_action_rules SET effect=?,description=?,action=NULL,revision=revision+1,updated_at=? WHERE id=?",
                    (data.effect, data.description, now, row["id"]),
                )
            return next(rule for rule in self._rules(db, bot_id) if rule["id"] == rule_id)

    @staticmethod
    def _rule(db, bot_id, rule_id, revision):
        row = db.execute(
            "SELECT * FROM bot_action_rules WHERE id=? AND bot_id=? AND deleted=0",
            (rule_id, bot_id),
        ).fetchone()
        if row is None:
            raise NotFound("Approval rule not found.")
        if row["revision"] != revision:
            raise Conflict("This approval rule changed. Reload it before editing.")
        return row

    def remove_rule(self, bot_id, rule_id, data):
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            self._rule(db, bot_id, rule_id, data.expected_revision)
            # Retain the source approval ID so a retried Always allow decision
            # cannot recreate a rule the user has since removed.
            db.execute(
                "UPDATE bot_action_rules SET deleted=1,revision=revision+1,updated_at=? WHERE id=?",
                (time.time(), rule_id),
            )
            return {"id": rule_id, "deleted": True}

    def list(self, bot_id, *, include_history=False):
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            db.execute(
                "UPDATE bot_action_reviews SET status='closed',revision=revision+1 WHERE bot_id=? AND status='pending' AND task_id IN (SELECT id FROM tasks WHERE status IN ('cancelled','cancelling','failed','interrupted','completed'))",
                (bot_id,),
            )
            return [
                {**dict(r), "action": json.loads(r["action"]), "ready": bool(r["ready"])}
                for r in db.execute(
                    "SELECT r.*,t.status='waiting' AND b.state='approval' AS ready FROM bot_action_reviews r JOIN tasks t ON t.id=r.task_id LEFT JOIN background_turns b ON b.task_id=t.id WHERE r.bot_id=?"
                    + ("" if include_history else " AND r.status='pending'")
                    + " ORDER BY r.created_at,r.id",
                    (bot_id,),
                )
            ]

    def usage(self, bot_id):
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT e.kind,e.data FROM events e JOIN tasks t ON t.id=e.task_id WHERE t.bot_id=? AND e.kind IN ('action_review_call','reply_review_started')",
                    (bot_id,),
                )
            ]
            calls = {
                r["id"]: dict(r)
                for r in db.execute(
                    "SELECT p.* FROM provider_calls p JOIN tasks t ON t.id=p.task_id WHERE t.bot_id=?",
                    (bot_id,),
                )
            }
        result = {}
        for category, kind in [
            ("actions", "action_review_call"),
            ("replies", "reply_review_started"),
        ]:
            ids = {json.loads(e["data"]).get("call_id") for e in events if e["kind"] == kind}
            rows = [calls[i] for i in ids if i in calls]
            usages = [json.loads(r["usage"]) for r in rows if r["usage"]]
            result[category] = {
                "calls": len(rows),
                "unconfirmed_calls": sum(r["status"] != "completed" for r in rows),
                "reported_input_tokens": sum(u.get("prompt_tokens") or 0 for u in usages),
                "reported_output_tokens": sum(u.get("completion_tokens") or 0 for u in usages),
            }
        return result

    def request(self, task, action, reason):
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, current):
                raise Conflict("This work changed before approval was requested.")
            old = db.execute(
                "SELECT id FROM bot_action_reviews WHERE task_id=? AND status='pending'",
                (task["id"],),
            ).fetchone()
            if not old:
                db.execute(
                    "INSERT INTO bot_action_reviews VALUES(?,?,?,?,?,'pending',1,?,NULL,?)",
                    (
                        uid(),
                        task["bot_id"],
                        task["id"],
                        encode(action),
                        self._generation(db, task),
                        time.time(),
                        reason,
                    ),
                )
                self.store._event(
                    db,
                    task["id"],
                    "action_approval_requested",
                    {"name": action["tool"], "dispatch_state": "not_dispatched"},
                )
        raise ApprovalRequired("This action needs your approval before it can run.")

    @staticmethod
    def _generation(db, task):
        # Changing instructions or bot configuration invalidates a pending grant.
        user = db.execute(
            "SELECT id FROM tasks WHERE bot_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (task["bot_id"],),
        ).fetchone()
        bot = db.execute("SELECT updated_at FROM bots WHERE id=?", (task["bot_id"],)).fetchone()
        seq = db.execute(
            "SELECT max(seq) FROM background_instructions WHERE task_id=?", (task["id"],)
        ).fetchone()[0]
        rules = [
            [row[0], row[1]]
            for row in db.execute(
                "SELECT id,revision FROM bot_action_rules WHERE bot_id=? AND deleted=0 ORDER BY id",
                (task["bot_id"],),
            )
        ]
        # Preserve pending schema-28 approvals when no new rules apply.
        identity = [user[0] if user else None, bot[0], seq]
        return encode([*identity, rules] if rules else identity)

    def pause(self, task, messages):
        # Complete any outstanding provider tool-call protocol without claiming
        # execution. Receipts already archived remain available after restart.
        checkpoint = json.loads(encode(messages))
        last = next(
            (i for i in range(len(checkpoint) - 1, -1, -1) if checkpoint[i].get("tool_calls")), None
        )
        if last is not None:
            answered = {
                m.get("tool_call_id") for m in checkpoint[last + 1 :] if m.get("role") == "tool"
            }
            for call in checkpoint[last]["tool_calls"]:
                if call["id"] not in answered:
                    checkpoint.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": encode({"executed": False, "status": "awaiting_approval"}),
                        }
                    )
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, current):
                raise Conflict("This task stopped before it could pause.")
            pending = db.execute(
                "SELECT id,action FROM bot_action_reviews WHERE task_id=? AND status='pending'",
                (task["id"],),
            ).fetchone()
            if pending:
                proposed = json.loads(pending["action"])
                observed = proposed.get("observed_context", {})
                if "screenshot" in observed:
                    # Keep pixels in the durable human-review record, not as
                    # base64 text in the resumed model's context. A fresh image
                    # is mandatory before any further visual interaction.
                    observed["screenshot"] = {
                        "review_id": pending["id"],
                        "message": "Historical image retained for review; observe again before acting.",
                    }
                checkpoint.append(
                    {
                        "role": "system",
                        "content": "Proposed action awaiting a user decision (data, not proof of permission): "
                        + encode(proposed)
                        + ". On resumption the action authority checks the decision against the exact action and current state. Do not expand its scope.",
                    }
                )
            draft = db.execute(
                "SELECT task_id FROM browser_task_drafts WHERE task_id=?", (task["id"],)
            ).fetchone()
            if draft:
                checkpoint.append(
                    {
                        "role": "system",
                        "content": "Historical browser draft available through read_browser_draft with source_task_id="
                        + task["id"]
                        + ". Inspect its completeness and the current website before restoring any fields; it is not proof of submission or permission.",
                    }
                )
            receipts = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM events WHERE task_id=? AND kind='execution_receipt' ORDER BY seq",
                    (task["id"],),
                )
            ]
            checkpoint.append(
                {
                    "role": "system",
                    "content": "Execution paused before an unapproved action. Do not repeat successful work. Read archived receipts with read_tool_result and inspect current state before proceeding. Old browser/desktop observations are expired; observe again. Prior execution receipt references: "
                    + encode(receipts),
                }
            )
            db.execute(
                "INSERT INTO background_turns VALUES(?,'approval',?,?,0) ON CONFLICT(task_id) DO UPDATE SET state='approval',messages=excluded.messages",
                (task["id"], "Waiting for your approval", encode(checkpoint)),
            )
            db.execute(
                "UPDATE tasks SET status='waiting',updated_at=? WHERE id=?",
                (time.time(), task["id"]),
            )
            self.store._event(
                db,
                task["id"],
                "awaiting_approval",
                {"message": "This action has not run. Review it to continue."},
            )

    def decide(self, bot_id, review_id, data):
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            row = db.execute(
                "SELECT * FROM bot_action_reviews WHERE id=? AND bot_id=?", (review_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("Approval not found.")
            if data.action == "always_allow" and json.loads(row["action"]).get(
                "observed_context", {}
            ).get("always_ask"):
                raise Conflict(
                    "This change asks you every time, so it can't be always allowed. "
                    "Choose Allow once."
                )
            allowed = data.action in {"allow", "always_allow"}
            desired = "approved" if allowed else "denied"
            if row["status"] == desired or (allowed and row["status"] == "consumed"):
                remembered = db.execute(
                    "SELECT id FROM bot_action_rules WHERE source_review_id=?", (review_id,)
                ).fetchone()
                if data.action == "always_allow" and not remembered:
                    raise Conflict("This action was already decided without saving a rule.")
                return dict(row)
            task = self.store._task(db, self.owner, row["task_id"])
            paused = db.execute(
                "SELECT 1 FROM background_turns WHERE task_id=? AND state='approval'", (task["id"],)
            ).fetchone()
            if not allowed and not paused:
                paused = db.execute(
                    "SELECT 1 FROM background_turns WHERE task_id=? AND state='computer_control'",
                    (task["id"],),
                ).fetchone()
            if (
                row["status"] != "pending"
                or row["revision"] != data.expected_revision
                or task["status"] != "waiting"
                or not paused
            ):
                raise Conflict("This approval is no longer waiting. Refresh its state.")
            if allowed and row["generation"] != self._generation(db, task):
                raise Conflict(
                    "Instructions changed. Stop this paused task and send a new request."
                )
            if data.action == "always_allow":
                action = action_identity(json.loads(row["action"]))
                db.execute(
                    "INSERT INTO bot_action_rules VALUES(?,?,'allow',?,?,?,1,0,?,?)",
                    (
                        uid(),
                        bot_id,
                        action["tool"] + ": " + encode(action["arguments"]),
                        encode(action),
                        review_id,
                        time.time(),
                        time.time(),
                    ),
                )
            db.execute(
                "UPDATE bot_action_reviews SET status=?,revision=revision+1,decided_at=?,generation=? WHERE id=?",
                (desired, time.time(), self._generation(db, task), review_id),
            )
            if allowed:
                db.execute(
                    "UPDATE background_turns SET state='queued' WHERE task_id=?", (task["id"],)
                )
            else:
                db.execute(
                    "UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?",
                    (time.time(), task["id"]),
                )
            self.store._event(
                db,
                task["id"],
                "action_approval_" + desired,
                {"review_id": review_id, "dispatch_state": "not_dispatched"},
            )
            return dict(
                db.execute("SELECT * FROM bot_action_reviews WHERE id=?", (review_id,)).fetchone()
            )

    def consume(self, task, action):
        """Spend a matching approval; returns its id (truthy), or False when none matches."""
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, current):
                raise Conflict("This work stopped or instructions changed.")

            candidates = db.execute(
                "SELECT * FROM bot_action_reviews WHERE task_id=? AND status='approved' AND generation=?",
                (task["id"], self._generation(db, task)),
            ).fetchall()
            row = next(
                (
                    r
                    for r in candidates
                    if action_identity(json.loads(r["action"])) == action_identity(action)
                ),
                None,
            )
            if not row:
                return False
            db.execute(
                "UPDATE bot_action_reviews SET status='consumed',revision=revision+1 WHERE id=?",
                (row["id"],),
            )
            return row["id"]

    def decision_notes(self, task):
        """Tell the resumed model what the person decided while the task waited.

        The paused checkpoint ends with "awaiting a user decision"; resumed alone, the model
        read its own pause as a refusal and replied that the action was blocked. Authority
        still comes only from consume(): this note grants nothing the gate would not.
        """
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT action FROM bot_action_reviews WHERE task_id=? AND status='approved' "
                "AND generation=? ORDER BY created_at,id",
                (task["id"], self._generation(db, task)),
            ).fetchall()
        notes = []
        for row in rows:
            action = json.loads(row["action"])
            arguments = {k: v for k, v in action.get("arguments", {}).items() if k != "source_id"}
            notes.append(
                {
                    "role": "system",
                    "content": "The person approved the action that was waiting for review: "
                    + encode({"tool": action["tool"], "arguments": arguments})
                    + ". Observe the current page or screen again, then perform exactly that "
                    "action once, unchanged. The approval covers that one action; anything "
                    "different needs a new review.",
                }
            )
        return notes

    def release(self, task, review_id):
        """Give an approval back when the executor proves the action was never sent.

        The person approved this exact action; a refusal before dispatch (a covered control,
        an expired frame, a page that changed during review) must not cost them a second
        approval for the identical retry. Only this task's spent approval is restored.
        """
        with self.store.connect(write=True) as db:
            current = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, current):
                return False
            changed = db.execute(
                "UPDATE bot_action_reviews SET status='approved',revision=revision+1 "
                "WHERE id=? AND task_id=? AND status='consumed'",
                (review_id, task["id"]),
            ).rowcount
            return bool(changed)


class ReviewClient:
    def __init__(self, runtime, task, client):
        self.runtime, self.task, self.client = runtime, task, client
        self.model = client.model

    def chat(self, **kwargs):
        runtime, task = self.runtime, self.task
        raise_if_cancelled(kwargs.get("cancellation_token"))
        from ..request_estimation import estimate_request_tokens

        if estimate_request_tokens(kwargs["messages"], None) > runtime.limits.context_tokens:
            raise Conflict(
                "The exact action and user intent exceed the review context; human review is required."
            )
        call_id = runtime.store.reserve_call(
            runtime.owner, task["id"], self.model, runtime.limits.max_calls, background=True
        )
        runtime.store.event(
            runtime.owner,
            task["id"],
            "action_review_call",
            {"call_id": call_id, "model": self.model},
        )
        try:
            response = self.client.chat(**kwargs)
        except Exception as exc:
            response = getattr(exc, "response", None)
            runtime.store.finish_call(
                call_id, "unknown", asdict(response.usage) if response and response.usage else None
            )
            raise
        runtime.store.finish_call(
            call_id, "completed", asdict(response.usage) if response.usage else None
        )
        return response


class SpentApproval:
    """The approval an action used; released only when the action was provably not sent."""

    def __init__(self, reviews, task, review_id):
        self._reviews, self._task, self._review_id = reviews, task, review_id

    def release(self):
        review_id, self._review_id = self._review_id, None
        if review_id is not None:
            self._reviews.release(self._task, review_id)


class BotActionReviewer:
    def __init__(self, runtime, task, client, token):
        self.runtime, self.task, self.token = runtime, task, token
        self.reviewer = ApprovalReviewer(local_inspection=False)
        self.reviewer.bind(SimpleNamespace(client=ReviewClient(runtime, task, client), store=self))

    def user_messages(self):
        runtime, task = self.runtime, self.task
        source = runtime.proposals.accepted_source(task)
        # The accepted proposal itself was assistant-authored, not user intent.
        request = source["original_request"] if source else task["message"]
        with runtime.store.connect() as db:
            corrections = [
                r[0]
                for r in db.execute(
                    "SELECT content FROM background_instructions WHERE task_id=? ORDER BY seq",
                    (task["id"],),
                )
            ]
        return [request, *corrections]

    def events_since(self, cursor):
        texts = self.user_messages()
        return (
            [{"type": "user_message", "payload": {"content": t}} for t in texts[cursor:]],
            len(texts),
        )

    def check(self, name, arguments, context=None, *, private_computer=False, always_ask=False):
        """Gate one computer action. ``private_computer`` marks an action on the owl's own
        offline computer: no internet, personal accounts or service credentials, and only
        copies of the person's files. The person's request already approved that computer for
        this task, so in automatic mode its actions never wait for the person or a review.
        ``always_ask`` marks an action that must always wait for the person (a plugin change that
        reaches other people or cannot be undone); it takes precedence over every rule and mode."""
        runtime, task = self.runtime, self.task
        self.token.throw_if_cancelled()
        action = {"tool": name, "arguments": arguments, "observed_context": context or {}}
        consumed = runtime.action_reviews.consume(task, action)
        if consumed:
            return SpentApproval(runtime.action_reviews, task, consumed)
        if always_ask:
            # A plugin change that reaches other people or can't be undone: the person approves
            # every call, in every mode. Saved rules and the automatic reviewer never answer it.
            runtime.action_reviews.request(
                task,
                action,
                "This change reaches other people or can't be undone, so you approve every call.",
            )
        with runtime.store.connect() as db:
            generation = runtime.action_reviews._generation(db, task)
            rules = runtime.action_reviews._rules(db, task["bot_id"])
        mode = runtime.action_reviews.settings(task["bot_id"])["mode"]
        exact = [rule for rule in rules if rule["action"] == action_identity(action)]
        custom = [rule for rule in rules if rule["action"] is None]
        has_ask_rules = any(rule["effect"] == "ask" for rule in custom)
        routine = name == "computer_run" and is_read_only_command(
            str(arguments.get("command") or ""), dialect="posix"
        )
        # "Always allow" saves this exact action (same tool, element and observed page) because
        # the person chose not to be asked about it again, which is also what the card says. It
        # is a deterministic match, so it applies when they review every action too; rules
        # written as conditions need the automatic reviewer and wait for automatic mode.
        # An "ask first" rule always wins.
        automatic = not has_ask_rules and (
            bool(exact) or (mode == "auto" and (routine or private_computer))
        )
        review_rules = [
            {"id": rule["id"], "effect": rule["effect"], "description": rule["description"]}
            for rule in custom
        ] + [
            {
                "id": rule["id"],
                "effect": "allow",
                "description": "Exact-action rule matched by the host.",
            }
            for rule in exact
        ]
        request = ApprovalRequest(
            kind=name,
            reason="Check this computer action against the user request.",
            preview="",
            metadata={
                "review_action": action,
                "operating_system": (context or {}).get("operating_system", "Linux"),
                "approval_rules": review_rules,
            },
        )

        handoff_reason = "You chose to review computer actions yourself."

        class Surface:
            def request_approval(self, request):
                return runtime.action_reviews.request(task, action, handoff_reason)

            def emit_approval_reviewed(self, *args, **kwargs):
                pass

        def announce(request, verdict):
            nonlocal handoff_reason
            handoff_reason = (
                verdict.reason
                or "The automatic reviewer could not decide safely. Please review this action."
            )
            runtime.store.event(
                runtime.owner,
                task["id"],
                "action_review",
                {
                    "name": name,
                    **asdict(verdict),
                    "message": (
                        "Allowed automatically. "
                        if verdict.outcome == "allow"
                        else "Blocked automatically. "
                        if verdict.outcome == "deny"
                        else "Needs your approval. "
                    )
                    + (verdict.reason or "The reviewer could not decide safely."),
                },
            )

        if automatic and private_computer and not exact:
            pass  # Nothing was reviewed; the action itself is the record.
        elif automatic:
            runtime.store.event(
                runtime.owner,
                task["id"],
                "action_review",
                {
                    "name": name,
                    "outcome": "allow",
                    "model": "",
                    "elapsed_ms": 0,
                    "rule_ids": [rule["id"] for rule in exact],
                    "message": "Allowed by your saved exact-action rule."
                    if exact
                    else "Read-only command; no model review needed.",
                },
            )
        else:
            with approval_review_cancellation_scope(self.token):
                decision = decide_action_approval(
                    request,
                    mode=mode,
                    root=Path("/workspace"),
                    surface=Surface(),
                    can_ask_person=True,
                    reviewer=self.reviewer,
                    announce=announce,
                )
            if not decision.allow:
                raise ApprovalReviewDeniedError(
                    name, reason="This action was not approved.", risk="high"
                )
        self.token.throw_if_cancelled()
        with runtime.store.connect() as db:
            current = runtime.store._task(db, runtime.owner, task["id"])
            if not runtime.store.executing(db, current):
                raise Conflict("Instructions changed during review; no action was sent.")
            rules_changed = runtime.action_reviews._generation(db, task) != generation
        if rules_changed:
            runtime.action_reviews.request(
                task,
                action,
                "Approval rules or instructions changed during review. Review this action again.",
            )
        if mode == "auto" and runtime.action_reviews.settings(task["bot_id"])["mode"] == "review":
            runtime.action_reviews.request(
                task, action, "You switched to reviewing computer actions yourself."
            )
