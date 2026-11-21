"""Messages the person sent that wait for their owl: changed or cancelled before they start.

A message the person sends while their owl works waits its turn as a queued task. Each message
they send in chat (POST /v1/chats/{id}/tasks) keeps a row here from the moment it is sent
(``record``). Until the owl starts it, the person can change its words, keeping its files, or
cancel it (PATCH /v1/tasks/{id}/queued, capability "queued_message_edits"). Each change needs
the revision the person saw, so two windows never overwrite each other unseen.

A change and the worker starting the task are both write transactions, which the store runs one
at a time (SQLite's immediate transactions, the hosted database's write lock). So they never
both happen: either the change lands before the task starts and the owl reads the new words, or
it finds the task started and says so (outcome "started") without changing anything. Asking
again for the same change gives the same answer.

A cancelled message's task ends as cancelled and its message leaves the conversation: the owl
never reads it, and the chat's activity is as it was before it was sent.

While the owl works, the person can also send a waiting message now (action "steer", capability
"queued_message_steer"): the running turn reads it at its next model round (steering.py). The
answer says whether it will ("steering"), already did ("steered"), or the message waits its turn
because nothing it can steer runs now ("queued", with a ``reason``).
"""

from __future__ import annotations

import hashlib
import time
from typing import Literal

from pydantic import Field, model_validator

from . import steering
from .models import Input
from .store import Conflict, Store

# What a change found, and so what the person's message is now: "edited" (it has the new words),
# "cancelled" (it is gone), "started" (the owl started it, so nothing changed) or "changed" (it
# still waits but changed since the revision sent, so nothing changed). A steer: "steering" (the
# running turn reads it at its next model round), "steered" (that turn read it already) or
# "queued" (it waits its turn, as before).
EDITED, CANCELLED, STARTED, CHANGED = "edited", "cancelled", "started", "changed"
STEERING, STEERED, QUEUED = "steering", "steered", "queued"


class QueuedMessageChange(Input):
    action: Literal["edit", "cancel", "steer"]
    # The revision the person saw (task queued_message.revision).
    expected_revision: int = Field(ge=0)
    # The new words, for an edit; its files stay as they were sent.
    message: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def _words_only_for_an_edit(self) -> QueuedMessageChange:
        if self.action == "edit" and self.message is None:
            raise ValueError("An edit carries the message's new words.")
        if self.action != "edit" and self.message is not None:
            raise ValueError("Only an edit carries words.")
        return self


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(db, task_id: str, message: str, now: float) -> None:
    """Note a message the person sent in chat; sending it again changes nothing."""
    db.execute(
        "INSERT INTO queued_messages(task_id,revision,sent_sha256,withdrawn_at,updated_at) "
        "VALUES(?,0,?,NULL,?) ON CONFLICT(task_id) DO NOTHING",
        (task_id, _digest(message), now),
    )


def sent_as(db, task_id: str, message: str) -> bool:
    """Whether the person first sent this task's message with these words (before any change)."""
    row = db.execute(
        "SELECT sent_sha256 FROM queued_messages WHERE task_id=?", (task_id,)
    ).fetchone()
    return row is not None and row["sent_sha256"] == _digest(message)


def not_withdrawn(task: str) -> str:
    """SQL: the task ``task`` names is a turn of its own: not a message the person cancelled before
    it started, nor one that became part of the turn it steered (steering.py)."""
    return (
        "NOT EXISTS(SELECT 1 FROM queued_messages w "
        f"WHERE w.task_id={task}.id AND (w.withdrawn_at IS NOT NULL OR w.steer_state='applied'))"
    )


def task_states(db, bot_id: str, only=None) -> dict[str, dict]:
    """The person's sent messages, by task: whether each can still change and how it did."""
    where, values = "t.bot_id=?", [bot_id]
    if only is not None:
        if not only:
            return {}
        where += f" AND t.id IN ({','.join('?' * len(only))})"
        values.extend(only)
    return {
        row["task_id"]: {
            "revision": row["revision"],
            # Waiting for the owl: Edit and Cancel apply.
            "editable": row["status"] == "queued",
            "edited": row["revision"] > 0,
            "withdrawn": row["withdrawn_at"] is not None,
            # Sent now to the running turn (steering.py): its state and that turn's id.
            **(
                {"steer": {"state": row["steer_state"], "task_id": row["steer_target"]}}
                if row["steer_state"] is not None
                else {}
            ),
        }
        for row in db.execute(
            "SELECT q.task_id,q.revision,q.withdrawn_at,q.steer_target,q.steer_state,t.status "
            f"FROM queued_messages q JOIN tasks t ON t.id=q.task_id WHERE {where}",
            values,
        )
    }


class QueuedMessages:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    def change(self, task_id: str, data: QueuedMessageChange) -> dict:
        with self.store.connect(write=True) as db:
            task = self.store._task(db, self.owner, task_id)
            row = db.execute(
                "SELECT revision,withdrawn_at,steer_state FROM queued_messages WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise Conflict("Only a message you sent in chat can be changed before it starts.")
            reason = None
            if row["withdrawn_at"] is not None:
                outcome = CANCELLED
            elif data.action == "steer" and row["steer_state"] == steering.APPLIED:
                outcome = STEERED
            elif task["status"] != "queued":
                outcome = STARTED
            elif data.action == "edit" and data.message == task["message"]:
                # Already says this (the same edit sent again): nothing to change.
                outcome = EDITED
            elif data.action == "steer" and row["steer_state"] == steering.PENDING:
                # Already on its way to the running turn (the same steer sent again).
                outcome = STEERING
            elif row["revision"] != data.expected_revision:
                outcome = CHANGED
            elif data.action == "edit":
                self._edit(db, task, row["revision"], data.message or "")
                outcome = EDITED
            elif data.action == "steer":
                state, reason = steering.steer(db, self.owner, task)
                outcome = STEERING if state == steering.PENDING else QUEUED
            else:
                self._withdraw(db, task)
                outcome = CANCELLED
            current = self.store._task(db, self.owner, task_id)
            self.store._decorate_tasks(db, current["bot_id"], [current], only=[task_id])
            return {
                "outcome": outcome,
                "task": current,
                **({"reason": reason} if reason is not None else {}),
            }

    def _edit(self, db, task: dict, revision: int, message: str) -> None:
        if not message.strip() and not self.store._attached_file_ids(db, task["id"]):
            raise Conflict("Write the message, or cancel it.")
        now = time.time()
        # Inside this write transaction the task is still queued: the worker can't start it
        # until the change is in, and then starts it with these words.
        db.execute(
            "UPDATE tasks SET message=?,updated_at=? WHERE id=? AND status='queued'",
            (message, now, task["id"]),
        )
        db.execute(
            "UPDATE messages SET content=? WHERE task_id=? AND role='user'", (message, task["id"])
        )
        db.execute(
            "UPDATE queued_messages SET revision=?,updated_at=? WHERE task_id=?",
            (revision + 1, now, task["id"]),
        )
        self.store._event(db, task["id"], "message_edited", {"revision": revision + 1})

    def _withdraw(self, db, task: dict) -> None:
        now = time.time()
        # The words go with the message; sent_sha256 still recognises a repeated send.
        db.execute(
            "UPDATE tasks SET status='cancelled',message='',updated_at=? "
            "WHERE id=? AND status='queued'",
            (now, task["id"]),
        )
        db.execute("DELETE FROM messages WHERE task_id=? AND role='user'", (task["id"],))
        # A steer still on its way goes with it: the running turn never reads the message.
        db.execute(
            "UPDATE queued_messages SET withdrawn_at=?,updated_at=?,steer_target=NULL,"
            "steer_state=NULL WHERE task_id=?",
            (now, now, task["id"]),
        )
        self.store._event(db, task["id"], "cancelled", {"withdrawn": True})
