"""Steering: a waiting message the person sends now, read by the turn their owl is running.

A message sent while the owl works waits its turn as a queued task (queued_messages.py). The
person can send one now instead (PATCH /v1/tasks/{id}/queued with action "steer", capability
"queued_message_steer"): it steers the turn running in the owl's own conversation, when that turn
is the person's own request (not a routine run, an activation, a first hello, the report of work
that went on elsewhere, or a room's turn).

A steer waits as "pending" until the turn reaches its next model round, never inside a batch of
tool calls. There the turn reads the person's words as their own message (user role, never system
text), and the message becomes part of that turn ("applied"): its own task ends without a reply,
and the conversation keeps it, in place, in the turn it steered (``steered_turns``). A response
the model was writing when it arrived is neither acted on nor published (no tool from it has run
yet), so a reply that was about to go out is held and the turn goes on with the person's words.

A turn that ends before it reads a steer (its reply already being published, a failure, Stop, or
work that goes on in the background) can't read it any more: the message stays queued and runs as
the next turn ("queued"), as it would have without the steer. Until the turn reads it, the person
can still change or cancel the message; the turn reads its words as they are then.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from .message_files import with_attached_files
from .store import ChatScope, Conflict, Store

PENDING, APPLIED, QUEUED = "pending", "applied", "queued"
# Messages one turn may have still to read; more wait their turn as usual.
MAX_PENDING = 5

_PENDING_FOR = (
    "SELECT t.id,t.message FROM queued_messages q JOIN tasks t ON t.id=q.task_id "
    "WHERE q.steer_target=? AND q.steer_state='pending' AND q.withdrawn_at IS NULL "
    "AND t.status='queued' ORDER BY t.created_at,t.id"
)


def steer(db, owner: str, task: dict) -> tuple[str, str | None]:
    """Send the person's waiting message ``task`` to the turn running in its owl's conversation.

    Runs in the caller's write transaction, which also checked that the message still waits.
    Returns PENDING when that turn will read it at its next model round, or QUEUED with why it
    waits its turn instead (nothing changes then): "not_working" (no turn runs there now),
    "not_steerable" (the running turn isn't the person's own request, or the message is in a room)
    or "full" (that turn has MAX_PENDING messages to read already).
    """
    # A selection grants access only in its own revision turn. Folding it into a
    # different running turn would lose the immutable file and saved-state checks.
    if task["config"].get("interactive_target") or Store._room_row(db, task["chat_id"]) is not None:
        return QUEUED, "not_steerable"
    _, scope = Store._scope(db, owner, task["chat_id"])
    target = db.execute(
        f"SELECT t.id FROM tasks t WHERE {scope.tasks} AND t.status='running' AND t.id<>? LIMIT 1",
        (scope.value, task["id"]),
    ).fetchone()
    if target is None:
        return QUEUED, "not_working"
    try:
        running = Store.direct_request(db, owner, task["bot_id"], target["id"])
    except Conflict:
        return QUEUED, "not_steerable"
    if running["config"].get("interactive_target"):
        return QUEUED, "not_steerable"
    if len(db.execute(_PENDING_FOR, (target["id"],)).fetchall()) >= MAX_PENDING:
        return QUEUED, "full"
    db.execute(
        "UPDATE queued_messages SET steer_target=?,steer_state=?,updated_at=? WHERE task_id=?",
        (target["id"], PENDING, time.time(), task["id"]),
    )
    Store._event(db, task["id"], "steer_received", {"task_id": target["id"]})
    Store._event(db, target["id"], "steer_received", {"message_task_id": task["id"]})
    return PENDING, None


def release_ended(db, owner: str) -> None:
    """Steers whose turn stopped running before it read them stay queued, to run as next turns."""
    rows = db.execute(
        "SELECT q.task_id,q.steer_target FROM queued_messages q JOIN tasks t "
        "ON t.id=q.steer_target WHERE q.steer_state='pending' AND t.owner=? "
        "AND t.status<>'running'",
        (owner,),
    ).fetchall()
    now = time.time()
    for row in rows:
        db.execute(
            "UPDATE queued_messages SET steer_state=?,updated_at=? WHERE task_id=?",
            (QUEUED, now, row["task_id"]),
        )
        Store._event(db, row["task_id"], "steer_queued", {"task_id": row["steer_target"]})


def steered_turns(tasks: Iterable[dict]) -> dict[str, str]:
    """The turn each message that steered one is part of, by the message's task, from tasks as a
    conversation shows them (their ``queued_message``)."""
    steered = {}
    for task in tasks:
        sent = (task.get("queued_message") or {}).get("steer") or {}
        if sent.get("state") == APPLIED:
            steered[task["id"]] = sent["task_id"]
    return steered


class Steering:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    def take(self, task: dict) -> list[dict]:
        """The messages that arrived for this running turn since it last looked, oldest first, as
        the person's own messages for its next model round; each is part of this turn from now.
        Empty when none arrived, or when the turn no longer runs."""
        with self.store.connect() as db:
            if db.execute(_PENDING_FOR + " LIMIT 1", (task["id"],)).fetchone() is None:
                return []
        with self.store.connect(write=True) as db:
            turn = self.store._task(db, self.owner, task["id"])
            if turn["status"] != "running":
                return []
            rows = db.execute(_PENDING_FOR, (task["id"],)).fetchall()
            if not rows:
                return []
            attached = self.store._message_files(
                db, ChatScope(turn["bot_id"]), only=[row["id"] for row in rows]
            )
            now = time.time()
            arrived = []
            for row in rows:
                db.execute(
                    "UPDATE tasks SET status='completed',updated_at=? WHERE id=? AND status='queued'",
                    (now, row["id"]),
                )
                db.execute(
                    "UPDATE queued_messages SET steer_state=?,updated_at=? WHERE task_id=?",
                    (APPLIED, now, row["id"]),
                )
                self.store._event(db, row["id"], "steer_applied", {"task_id": task["id"]})
                self.store._event(db, task["id"], "steer_applied", {"message_task_id": row["id"]})
                arrived.append(
                    {
                        "role": "user",
                        # As the conversation keeps it: the words, then the files they attached.
                        "content": with_attached_files(
                            row["message"],
                            attached.get(row["id"]),
                            can_open=bool(turn["config"]["files_enabled"]),
                        ),
                    }
                )
            return arrived

    def words(self, task_id: str) -> list[str]:
        """The words of the messages that steered this turn, in the order the person sent them."""
        with self.store.connect() as db:
            return [
                row["message"]
                for row in db.execute(
                    "SELECT t.message FROM queued_messages q JOIN tasks t ON t.id=q.task_id "
                    "WHERE q.steer_target=? AND q.steer_state=? ORDER BY t.created_at,t.id",
                    (task_id, APPLIED),
                )
            ]
