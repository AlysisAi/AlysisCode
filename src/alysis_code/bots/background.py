"""Continue the same bot turn off the reply loop, without a second agent prompt."""

from __future__ import annotations

import json
import time

from pydantic import Field

from .models import Input
from .store import Conflict, NotFound, Store, encode


class BackgroundRequest(Input):
    summary: str = Field(min_length=1, max_length=300)


class BackgroundStore:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    def submit(self, task: dict, summary: str, messages: list[dict]) -> None:
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            if source["status"] != "running" or source["config"].get("activation"):
                raise Conflict("This turn cannot start background work.")
            if db.execute(
                "SELECT 1 FROM background_turns WHERE task_id=?", (task["id"],)
            ).fetchone():
                raise Conflict("This turn already moved to the background.")
            if (
                db.execute(
                    "SELECT 1 FROM background_turns j JOIN tasks t ON t.id=j.task_id WHERE t.bot_id=? AND t.status='waiting'",
                    (source["bot_id"],),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM coding_jobs j JOIN tasks t ON t.id=j.task_id "
                    "WHERE t.bot_id=? AND j.status IN ('queued','running')",
                    (source["bot_id"],),
                ).fetchone()
            ):
                raise Conflict(
                    "This bot already has background work. Update it or wait for its result."
                )
            db.execute(
                "INSERT INTO background_turns VALUES(?,'queued',?,?,0)",
                (task["id"], summary, encode(messages)),
            )
            db.execute(
                "UPDATE tasks SET status='waiting',updated_at=? WHERE id=?",
                (time.time(), task["id"]),
            )
            self.store._event(db, task["id"], "background_started", {"message": summary})

    def resume(self, task: dict, summary: str, messages: list[dict]) -> None:
        """Restart this task's running background turn from a new checkpoint.

        Used when the turn gains a permission mid-way (its person's request approved the
        private computer): the tools a turn is offered are chosen when it starts, so the same
        turn goes back in the queue with its full context and starts again with them.
        """
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            row = db.execute(
                "SELECT state FROM background_turns WHERE task_id=?", (task["id"],)
            ).fetchone()
            if source["status"] != "waiting" or row is None or row["state"] != "running":
                raise Conflict("This background work is no longer running.")
            db.execute(
                "UPDATE background_turns SET state='queued',summary=?,messages=? WHERE task_id=?",
                (summary, encode(messages), task["id"]),
            )
            self.store._event(db, task["id"], "background_started", {"message": summary})

    def claim(self) -> dict | None:
        from .hosted_maintenance import MaintenanceLease

        with self.store.connect(write=True) as db:
            if MaintenanceLease.pending(db):
                return None
            row = db.execute(
                "SELECT j.task_id FROM background_turns j JOIN tasks t ON t.id=j.task_id WHERE t.owner=? AND t.status='waiting' AND j.state='queued' ORDER BY t.created_at LIMIT 1",
                (self.owner,),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE background_turns SET state='running' WHERE task_id=?", (row[0],))
            return self.store._task(db, self.owner, row[0])

    def checkpoint(self, task_id: str) -> list[dict]:
        with self.store.connect() as db:
            self.store._task(db, self.owner, task_id)
            row = db.execute(
                "SELECT messages FROM background_turns WHERE task_id=? AND state='running'",
                (task_id,),
            ).fetchone()
            if row is None:
                raise Conflict("Background work is not running.")
            return json.loads(row[0])

    def active(self, bot_id: str) -> list[dict]:
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT t.id AS task_id,t.message AS request,j.summary,j.state FROM background_turns j JOIN tasks t ON t.id=j.task_id WHERE t.bot_id=? AND t.status='waiting'",
                    (bot_id,),
                )
            ]

    def read(self, bot_id: str, task_id: str) -> dict:
        with self.store.connect() as db:
            task = self.store._task(db, self.owner, task_id)
            if (
                task["bot_id"] != bot_id
                or not db.execute(
                    "SELECT 1 FROM background_turns WHERE task_id=?", (task_id,)
                ).fetchone()
            ):
                raise NotFound("Background work not found for this bot.")
            return {k: task[k] for k in ("id", "message", "status", "output", "error", "calls")}

    def correct(self, source: dict, task_id: str) -> dict:
        with self.store.connect(write=True) as db:
            request = self.store.direct_request(db, self.owner, source["bot_id"], source["id"])
            target = self.store._task(db, self.owner, task_id)
            if target["bot_id"] != request["bot_id"]:
                raise NotFound("Work not found for this bot.")
            if (
                target["status"] != "waiting"
                or not db.execute(
                    "SELECT 1 FROM background_turns WHERE task_id=?", (task_id,)
                ).fetchone()
            ):
                raise Conflict("This work has stopped; the correction was not applied.")
            db.execute(
                "INSERT INTO background_instructions(task_id,source_task_id,content) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (task_id, request["id"], request["message"]),
            )
            return {
                "task_id": task_id,
                "state": "queued",
                "message": "The exact user correction will be read before the next model step; it has not yet been carried out.",
            }

    def updates(self, task_id: str) -> list[dict]:
        with self.store.connect(write=True) as db:
            self.store._task(db, self.owner, task_id)
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT seq,content FROM background_instructions WHERE task_id=? AND seq>(SELECT applied_seq FROM background_turns WHERE task_id=?) ORDER BY seq",
                    (task_id, task_id),
                )
            ]
            if rows:
                db.execute(
                    "UPDATE background_turns SET applied_seq=? WHERE task_id=?",
                    (rows[-1]["seq"], task_id),
                )
            return rows

    def assert_current(self, task_id: str) -> None:
        with self.store.connect() as db:
            task = self.store._task(db, self.owner, task_id)
            if not self.store.executing(db, task):
                raise Conflict(
                    "Work stopped or a new correction arrived. Review saved results before continuing."
                )
