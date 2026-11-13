"""Durable event delivery. Event data never grants authority to execute actions."""

from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import Field

from .models import Input, TaskInput
from .store import Conflict, NotFound, Store, encode, uid


class TriggerInput(Input):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["file_added"] = "file_added"
    instructions: str = Field(min_length=1, max_length=4000)


class TriggerEdit(TriggerInput):
    trigger_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class TriggerControl(Input):
    trigger_id: str = Field(min_length=1, max_length=100)
    state: Literal["active", "paused"]
    expected_revision: int = Field(ge=1)


class TriggerDecision(Input):
    state: Literal["active", "paused"]
    expected_revision: int = Field(ge=1)


def file_added(db, bot_id: str, file_id: str, name: str, now: float) -> None:
    """Called in the upload transaction, only for user-added files.

    The trusted producer supplies identity and scope, never a model or public
    webhook. Future connector producers must authenticate their own deliveries.
    """
    event_id = "file_" + file_id
    inserted = db.execute(
        "INSERT INTO activation_events VALUES(?,?,?, ?,?,?) ON CONFLICT DO NOTHING",
        (event_id, bot_id, "file_added", file_id, encode({"file_id": file_id, "name": name}), now),
    ).rowcount
    if not inserted:
        return
    for rule in db.execute(
        "SELECT id,revision FROM bot_triggers WHERE bot_id=? AND kind='file_added' AND state='active'",
        (bot_id,),
    ).fetchall():
        db.execute(
            "INSERT INTO trigger_deliveries VALUES(?,?,?, ?,NULL,'pending',NULL,?)",
            (uid(), rule["id"], rule["revision"], event_id, now),
        )


def revoke(db, trigger_id: str, reason: str) -> None:
    db.execute(
        "UPDATE trigger_deliveries SET state='skipped',reason=? WHERE trigger_id=? AND state='pending'",
        (reason, trigger_id),
    )
    for row in db.execute(
        """SELECT DISTINCT t.id,t.status FROM tasks t JOIN trigger_deliveries d ON d.task_id=t.id
        WHERE d.trigger_id=? AND t.status IN ('queued','running')""",
        (trigger_id,),
    ).fetchall():
        status = "cancelled" if row["status"] == "queued" else "cancelling"
        db.execute(
            "UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, time.time(), row["id"])
        )
        Store._event(db, row["id"], status, {"message": reason})
    db.execute(
        """UPDATE action_proposals SET status='outdated',revision=revision+1 WHERE status='pending'
        AND source_task_id IN (SELECT task_id FROM trigger_deliveries WHERE trigger_id=?)""",
        (trigger_id,),
    )


class TriggerStore:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    def _get(self, db, bot_id: str, trigger_id: str) -> dict:
        self.store._bot(db, self.owner, bot_id)
        row = db.execute(
            "SELECT * FROM bot_triggers WHERE id=? AND bot_id=?", (trigger_id, bot_id)
        ).fetchone()
        if row is None:
            raise NotFound("Automatic activation not found.")
        return dict(row)

    def list(self, bot_id: str) -> list[dict]:
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            result = []
            for row in db.execute(
                "SELECT * FROM bot_triggers WHERE bot_id=? ORDER BY created_at,id", (bot_id,)
            ):
                rule = dict(row)
                rule["recent_deliveries"] = [
                    dict(r)
                    for r in db.execute(
                        """SELECT d.state,d.reason,d.task_id,d.created_at,t.status AS task_status,
                    e.payload FROM trigger_deliveries d JOIN activation_events e ON e.id=d.event_id
                    LEFT JOIN tasks t ON t.id=d.task_id WHERE d.trigger_id=?
                    ORDER BY d.created_at DESC,d.id DESC LIMIT 20""",
                        (rule["id"],),
                    )
                ]
                for delivery in rule["recent_deliveries"]:
                    delivery["event"] = json.loads(delivery.pop("payload"))
                source = rule["source_task_id"]
                rule["ready"] = (
                    rule["state"] != "draft"
                    or not source
                    or self.store._task(db, self.owner, source)["status"] == "completed"
                )
                result.append(rule)
            return result

    def create(
        self,
        bot_id: str,
        data: TriggerInput,
        *,
        source_task_id: str | None = None,
        requested: bool = False,
    ) -> dict:
        with self.store.connect(write=True) as db:
            bot = self.store._bot(db, self.owner, bot_id)
            if not bot["files_enabled"]:
                raise Conflict("Enable files for this bot before adding a file activation.")
            if requested:
                self.store.direct_request(db, self.owner, bot_id, source_task_id)
            if source_task_id:
                source = self.store._task(db, self.owner, source_task_id)
                if source["bot_id"] != bot_id or source["status"] != "running":
                    raise Conflict("This task cannot suggest an activation.")
                old = db.execute(
                    "SELECT * FROM bot_triggers WHERE source_task_id=?", (source_task_id,)
                ).fetchone()
                if old:
                    if any(old[k] != v for k, v in data.model_dump().items()):
                        raise Conflict("An activation was already suggested in this reply.")
                    return dict(old)
            if db.execute(
                "SELECT 1 FROM bot_triggers WHERE bot_id=? AND kind=? AND instructions=? AND state!='paused'",
                (bot_id, data.kind, data.instructions),
            ).fetchone():
                raise Conflict("This activation already exists. Review it before creating another.")
            trigger_id, now = uid(), time.time()
            db.execute(
                "INSERT INTO bot_triggers VALUES(?,?,?,?,?,'draft',1,?,?,NULL,?,?)",
                (
                    trigger_id,
                    bot_id,
                    data.name,
                    data.kind,
                    data.instructions,
                    source_task_id,
                    bot["updated_at"],
                    now,
                    now,
                ),
            )
            if requested:
                db.execute("UPDATE bot_triggers SET state='active' WHERE id=?", (trigger_id,))
            return self._get(db, bot_id, trigger_id)

    def edit(self, bot_id: str, data: TriggerEdit, *, source_task_id: str) -> dict:
        with self.store.connect(write=True) as db:
            self.store.direct_request(db, self.owner, bot_id, source_task_id)
            rule = self._get(db, bot_id, data.trigger_id)
            if rule["revision"] != data.expected_revision:
                raise Conflict("This routine changed. Read it again before editing.")
            revoke(db, data.trigger_id, "Routine changed; old events will not replay.")
            db.execute(
                "UPDATE bot_triggers SET name=?,instructions=?,revision=revision+1,updated_at=? WHERE id=?",
                (data.name, data.instructions, time.time(), data.trigger_id),
            )
            return self._get(db, bot_id, data.trigger_id)

    def decide(
        self,
        bot_id: str,
        trigger_id: str,
        data: TriggerDecision,
        *,
        source_task_id: str | None = None,
    ) -> dict:
        with self.store.connect(write=True) as db:
            if source_task_id:
                self.store.direct_request(db, self.owner, bot_id, source_task_id)
            rule = self._get(db, bot_id, trigger_id)
            bot = self.store._bot(db, self.owner, bot_id)
            if rule["state"] == data.state:
                return rule
            if rule["revision"] != data.expected_revision:
                raise Conflict("This activation changed. Review its current state.")
            if data.state == "active":
                if not bot["files_enabled"]:
                    raise Conflict("Files are disabled for this bot.")
                if rule["state"] == "draft" and rule["source_task_id"]:
                    source = self.store._task(db, self.owner, rule["source_task_id"])
                    if (
                        source["status"] != "completed"
                        or bot["updated_at"] != rule["bot_updated_at"]
                    ):
                        raise Conflict(
                            "The suggested activation is no longer current. Ask for a new one."
                        )
            revoke(
                db,
                trigger_id,
                "Activation paused or its approval changed; old events will not replay.",
            )
            db.execute(
                "UPDATE bot_triggers SET state=?,revision=revision+1,bot_updated_at=?,updated_at=? WHERE id=?",
                (data.state, bot["updated_at"], time.time(), trigger_id),
            )
            return self._get(db, bot_id, trigger_id)

    def delete(self, bot_id: str, trigger_id: str) -> None:
        """Remove an automatic activation; a review it started must stop first."""
        with self.store.connect(write=True) as db:
            self._get(db, bot_id, trigger_id)
            if db.execute(
                "SELECT 1 FROM trigger_deliveries d JOIN tasks t ON t.id=d.task_id "
                "WHERE d.trigger_id=? AND t.status IN ('queued','running','waiting','cancelling')",
                (trigger_id,),
            ).fetchone():
                raise Conflict("Stop this routine's current run before deleting it.")
            db.execute("DELETE FROM trigger_deliveries WHERE trigger_id=?", (trigger_id,))
            db.execute("DELETE FROM bot_triggers WHERE id=?", (trigger_id,))

    def dispatch(self, *, max_calls: int | None, now: float | None = None) -> int:
        now = time.time() if now is None else now
        count = 0
        with self.store.connect(write=True) as db:
            rules = db.execute(
                """SELECT r.* FROM bot_triggers r JOIN bots b ON b.id=r.bot_id
                WHERE b.owner=? AND r.state='active'
                ORDER BY CASE WHEN r.last_run_at IS NULL THEN 0 ELSE 1 END,
                r.last_run_at,r.created_at,r.id""",
                (self.owner,),
            ).fetchall()
            for row in rules:
                rule = dict(row)
                bot = self.store._bot(db, self.owner, rule["bot_id"])
                if not bot["files_enabled"] or bot["updated_at"] != rule["bot_updated_at"]:
                    revoke(db, rule["id"], "Bot settings changed; review and enable again.")
                    db.execute(
                        "UPDATE bot_triggers SET state='paused',revision=revision+1,updated_at=? WHERE id=?",
                        (now, rule["id"]),
                    )
                    continue
                db.execute(
                    """UPDATE trigger_deliveries SET state='skipped',reason='Source file was removed.'
                    WHERE trigger_id=? AND state='pending' AND event_id IN
                    (SELECT e.id FROM activation_events e WHERE e.kind='file_added' AND NOT EXISTS
                     (SELECT 1 FROM files f WHERE f.id=e.resource_id AND f.bot_id=e.bot_id))""",
                    (rule["id"],),
                )
                pending = db.execute(
                    "SELECT d.*,e.payload FROM trigger_deliveries d JOIN activation_events e ON e.id=d.event_id WHERE d.trigger_id=? AND d.state='pending' ORDER BY d.created_at,d.id LIMIT 20",
                    (rule["id"],),
                ).fetchall()
                if not pending:
                    continue
                reason = None
                if pending[0]["created_at"] > now - 5:
                    reason = "Collecting files added together."
                elif rule["last_run_at"] is not None and rule["last_run_at"] > now - 60:
                    reason = "Waiting for the one-minute cooldown."
                elif db.execute(
                    "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('queued','running','waiting','cancelling')",
                    (bot["id"],),
                ).fetchone():
                    reason = "Waiting for this bot's current work."
                elif (
                    max_calls is not None
                    and db.execute(
                        "SELECT count(*) FROM provider_calls p JOIN tasks t ON t.id=p.task_id WHERE t.owner=?",
                        (self.owner,),
                    ).fetchone()[0]
                    >= max_calls
                ):
                    reason = "Waiting for model-call allowance."
                elif (
                    db.execute(
                        "SELECT count(*) FROM tasks WHERE owner=? AND status IN ('queued','running','waiting','cancelling')",
                        (self.owner,),
                    ).fetchone()[0]
                    >= 20
                ):
                    reason = "Waiting for room in the task queue."
                if reason:
                    db.execute(
                        "UPDATE trigger_deliveries SET reason=? WHERE trigger_id=? AND state='pending'",
                        (reason, rule["id"]),
                    )
                    continue
                chat = self.store._ensure_chat(db, self.owner, bot["id"], bot["name"])
                task = self.store._enqueue(
                    db,
                    self.owner,
                    chat["id"],
                    TaskInput(
                        message=rule["instructions"], request_id="activation_" + pending[0]["id"]
                    ),
                )
                context = {
                    "trigger_id": rule["id"],
                    "revision": rule["revision"],
                    "name": rule["name"],
                    "kind": rule["kind"],
                    "mode": "review",
                    "events": [
                        {**json.loads(p["payload"]), "occurred_at": p["created_at"]}
                        for p in pending
                    ],
                }
                db.execute(
                    "UPDATE tasks SET config=? WHERE id=?",
                    (encode({**task["config"], "activation": context}), task["id"]),
                )
                for delivery in pending:
                    db.execute(
                        "UPDATE trigger_deliveries SET state='queued',reason=NULL,task_id=? WHERE id=?",
                        (task["id"], delivery["id"]),
                    )
                db.execute("UPDATE bot_triggers SET last_run_at=? WHERE id=?", (now, rule["id"]))
                self.store._event(
                    db,
                    task["id"],
                    "activated",
                    {"name": rule["name"], "trigger_id": rule["id"], "event_count": len(pending)},
                )
                count += 1
        return count
