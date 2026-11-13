"""Human ownership of one Bot computer; input never enters task history.

The durable pause survives service restarts. Connection grants do not. Agent tool
admission is fenced before requesting a checkpoint; input starts only after that
checkpoint and all admitted operations have settled. Connection expiry revokes
input, never resumes a task. It is not a task execution deadline.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .computer import ComputerControlPause, ComputerLocks
from .models import Input
from .store import Conflict, encode


class ControlRequest(Input):
    action: Literal["take", "heartbeat", "input", "disconnect", "resume"]
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$", repr=False)
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(default=0, ge=0, strict=True)
    event: dict | None = Field(default=None, repr=False)


class ControlHandoff(Input):
    reason: str = Field(min_length=1, max_length=300)


class ComputerRecovery(Input):
    confirm: Literal[True]


@dataclass
class Grant:
    client_id: str
    session_id: str
    seen: float
    sequence: int = 0
    input_pending: bool = False


class ComputerControls:
    def __init__(
        self,
        store,
        owner,
        views,
        *,
        clock=time.monotonic,
        wall_clock=time.time,
        idle_seconds=300,
        memory_sleep=False,
    ):
        self.store, self.owner, self.views, self.clock = store, owner, views, clock
        self._lock = threading.RLock()
        self._operations = ComputerLocks()
        self._grants = {}
        self._stopped = False
        self.wall_clock, self.idle_seconds = wall_clock, idle_seconds
        self.memory_sleep = memory_sleep
        self.adopt_computer = lambda task: None

    def open(self, bot_id, manager, cancel):
        """An explicit viewer gesture may create a desktop, never a model turn."""
        from .hosted_maintenance import MaintenanceLease

        with self._operations.hold(bot_id):
            if self._stopped or cancel():
                raise Conflict("The computer service is stopping.")
            view, _, active = self.views.current(self.owner, bot_id)
            if (view and view.alive()) or active:
                return  # Watching must not change a running task or input grant.
            previous = self._row(bot_id)
            task = None
            if previous and previous["task_id"]:
                task = self.store.task(self.owner, previous["task_id"])
                if task["status"] != "waiting":
                    raise Conflict("Wait for the current task to finish before reopening.")
            with self.store.connect(write=True) as db:
                self.store._bot(db, self.owner, bot_id)
                if MaintenanceLease.pending(db):
                    raise Conflict("Cloud maintenance is in progress. Try again shortly.")
                db.execute(
                    "INSERT INTO bot_computer_control VALUES(?,?,?,?,?) "
                    "ON CONFLICT(bot_id) DO UPDATE SET state='opening'",
                    (
                        bot_id,
                        task["id"] if task else None,
                        uuid4().hex,
                        "opening",
                        self.wall_clock(),
                    ),
                )
            try:
                generation = manager.open(bot_id, cancel=cancel, task=task)
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE bot_computer_control SET task_id=?,session_id=?,state=?,updated_at=? WHERE bot_id=?",
                        (
                            task["id"] if task else None,
                            generation,
                            "paused" if task else "viewing",
                            self.wall_clock(),
                            bot_id,
                        ),
                    )
                self._grants.pop(bot_id, None)
                self.views.fence(self.owner, bot_id, bool(task))
                self.views.set_lifecycle(self.owner, bot_id, "ready")
            except BaseException:
                with self.store.connect(write=True) as db:
                    if previous:
                        db.execute(
                            "UPDATE bot_computer_control SET task_id=?,session_id=?,state=?,updated_at=? WHERE bot_id=?",
                            (
                                previous["task_id"],
                                previous["session_id"],
                                previous["state"],
                                previous["updated_at"],
                                bot_id,
                            ),
                        )
                    else:
                        db.execute("DELETE FROM bot_computer_control WHERE bot_id=?", (bot_id,))
                raise

    def _row(self, bot_id):
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            row = db.execute(
                "SELECT * FROM bot_computer_control WHERE bot_id=?", (bot_id,)
            ).fetchone()
            return dict(row) if row else None

    def recover_saved(self, bot_id, factory):
        """Only an explicit user recovery gesture may replace a sleeping desktop."""
        with self._operations.hold(bot_id):
            view, _, active = self.views.current(self.owner, bot_id)
            if view is not None or active or self._stopped:
                raise Conflict("The computer is busy. Wait before recovering it.")
            if not callable(getattr(factory, "recover", None)):
                raise Conflict("Computer recovery is unavailable on this connection.")
            previous = self._row(bot_id)
            with self.store.connect(write=True) as db:
                self.store._bot(db, self.owner, bot_id)
                from .hosted_maintenance import MaintenanceLease

                if MaintenanceLease.pending(db):
                    raise Conflict("Cloud maintenance is in progress. Try again shortly.")
                if db.execute(
                    "SELECT 1 FROM tasks WHERE bot_id=? AND status IN ('running','cancelling') LIMIT 1",
                    (bot_id,),
                ).fetchone():
                    raise Conflict(
                        "Wait for this Owl's work to stop before recovering its computer."
                    )
                db.execute(
                    "INSERT INTO bot_computer_control VALUES(?,?,?,?,?) ON CONFLICT(bot_id) DO UPDATE SET state='opening'",
                    (bot_id, None, uuid4().hex, "opening", self.wall_clock()),
                )
            try:
                factory.recover(bot_id)
            finally:
                with self.store.connect(write=True) as db:
                    if previous:
                        db.execute(
                            "UPDATE bot_computer_control SET task_id=?,session_id=?,state=?,updated_at=? WHERE bot_id=?",
                            (
                                previous["task_id"],
                                previous["session_id"],
                                previous["state"],
                                previous["updated_at"],
                                bot_id,
                            ),
                        )
                    else:
                        db.execute("DELETE FROM bot_computer_control WHERE bot_id=?", (bot_id,))

    def _delete(self, bot_id):
        self._grants.pop(bot_id, None)
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            db.execute("DELETE FROM bot_computer_control WHERE bot_id=?", (bot_id,))
        self.views.fence(self.owner, bot_id, False)

    def recover(self):
        # Called only after runtime ownership and old-compute reconciliation.
        with self._lock, self.store.connect(write=True) as db:
            self._grants.clear()
            self._stopped = False
            rows = db.execute(
                "SELECT c.* FROM bot_computer_control c JOIN bots b ON b.id=c.bot_id WHERE b.owner=?",
                (self.owner,),
            ).fetchall()
            for row in rows:
                if (
                    row["task_id"]
                    and self.store._task(db, self.owner, row["task_id"])["status"] == "waiting"
                ):
                    db.execute(
                        "UPDATE bot_computer_control SET state=CASE WHEN state='idle' THEN 'idle' ELSE 'paused' END WHERE bot_id=?",
                        (row["bot_id"],),
                    )
                    self.views.fence(self.owner, row["bot_id"], True)
                else:
                    db.execute("DELETE FROM bot_computer_control WHERE bot_id=?", (row["bot_id"],))

    def reap(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT c.bot_id FROM bot_computer_control c JOIN bots b ON b.id=c.bot_id WHERE b.owner=?",
                (self.owner,),
            ).fetchall()
        for row in rows:
            with self._operations.hold(row["bot_id"], blocking=False) as admitted:
                if admitted:
                    self._status(row["bot_id"])

    def stop(self):
        with self._lock:
            self._stopped = True
            self._grants.clear()

    def expire_idle(self, suspend):
        """Per-Bot checkpointing never holds the shared control lock during I/O."""
        with self.store.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT c.* FROM bot_computer_control c JOIN bots b ON b.id=c.bot_id "
                    "WHERE b.owner=? AND c.state IN ('paused','viewing')",
                    (self.owner,),
                )
            ]
        for row in rows:
            with self._operations.hold(row["bot_id"]):
                if self._stopped:
                    return
                current = self._row(row["bot_id"])
                view, task_id, active = self.views.current(self.owner, row["bot_id"])
                if (
                    not current
                    or current["state"] not in {"paused", "viewing"}
                    or not view
                    or active
                    or view.generation != current["session_id"]
                    or task_id != current["task_id"]
                    or self.wall_clock() - current["updated_at"] < self.idle_seconds
                ):
                    continue
                self._grants.pop(row["bot_id"], None)
                # Disposal closes the private display and its writers. Requiring
                # another guest gesture here could strand the paid computer if
                # the input channel is unavailable. Give-back still verifies
                # input cleanup before reusing a running computer.
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE bot_computer_control SET state='suspending' WHERE bot_id=? AND session_id=? AND state IN ('paused','viewing')",
                        (row["bot_id"], current["session_id"]),
                    )
                self.views.fence(self.owner, row["bot_id"], True)
                success = False
                try:
                    suspend(current["task_id"], current["session_id"])
                    success = True
                except Exception:
                    pass  # Preserve the pause and previous checkpoint; never resume or replay.
                with self.store.connect(write=True) as db:
                    if self._stopped:
                        continue
                    changed = db.execute(
                        "UPDATE bot_computer_control SET state=? WHERE bot_id=? AND session_id=? AND state='suspending' "
                        "AND (task_id IS NULL OR EXISTS(SELECT 1 FROM tasks WHERE id=? AND status='waiting'))",
                        (
                            "idle" if success else "paused",
                            row["bot_id"],
                            current["session_id"],
                            current["task_id"],
                        ),
                    )
                    if not current["task_id"]:
                        self.views.fence(self.owner, row["bot_id"], False)
                    if changed.rowcount and current["task_id"]:
                        self.store._event(
                            db,
                            current["task_id"],
                            "computer_idle_suspended"
                            if success
                            else "computer_idle_checkpoint_failed",
                            {
                                "message": (
                                    "The computer is sleeping. The task stays paused; open its computer to resume the desktop."
                                    if success
                                    else "Computer sleep could not be confirmed. The task remains paused and its computer is still owned."
                                )
                                if self.memory_sleep
                                else (
                                    "The idle computer was released. The task is paused; resume from saved work. Unsaved windows are not retained."
                                    if success
                                    else "The idle computer checkpoint could not be confirmed. The task remains paused; previous saved work is retained."
                                )
                            },
                        )

    def check(self, task):
        with self._operations.hold(task["bot_id"]):
            row = self._row(task["bot_id"])
            view, _, _ = self.views.current(self.owner, task["bot_id"])
            if row and row["task_id"] is None and view is None:
                self._delete(task["bot_id"])
                return
            if row and row["task_id"] is None and view and row["state"] in {"viewing", "paused"}:
                self.adopt_computer(task)
                with self.store.connect(write=True) as db:
                    if row["state"] == "viewing":
                        db.execute(
                            "DELETE FROM bot_computer_control WHERE bot_id=?", (task["bot_id"],)
                        )
                    else:
                        db.execute(
                            "UPDATE bot_computer_control SET task_id=? WHERE bot_id=?",
                            (task["id"], task["bot_id"]),
                        )
        if not self.views.fenced(self.owner, task["bot_id"]):
            return
        row = self._row(task["bot_id"])
        if row and row["task_id"] == task["id"]:
            raise ComputerControlPause()

    def _status(self, bot_id, *, settle=True):
        row = self._row(bot_id)
        view, _, active = self.views.current(self.owner, bot_id)
        available = bool(view and view.human_input and view.alive())
        if not row:
            return {"state": "agent", "available": available}
        if row["state"] in {"opening", "viewing"}:
            return {"state": "agent", "available": available}
        if row["task_id"]:
            task = self.store.task(self.owner, row["task_id"])
            if task["status"] not in {"running", "waiting"}:
                if settle:
                    self._delete(bot_id)
                return {"state": "stopped", "available": False}
        grant = self._grants.get(bot_id)
        if grant and (
            (not grant.input_pending and self.clock() - grant.seen > 15)
            or self._stopped
            or (view and view.input_ready and not view.input_ready())
        ):
            if settle:
                self._grants.pop(bot_id, None)
                if view and view.human_input and view.generation == row["session_id"]:
                    try:
                        view.human_input({"kind": "release"})
                    except Exception:
                        pass  # Resume performs verified cleanup again; never resume here.
            grant = None
        if grant and grant.session_id != row["session_id"]:
            grant = None
        current = available and view.generation == row["session_id"]
        ready = row["state"] in {"paused", "idle"} and active == 0
        return {
            "state": "suspending"
            if row["state"] == "suspending"
            else "controlling"
            if ready and current and grant
            else "pausing"
            if not ready
            else "paused",
            "available": bool(current) and row["state"] != "suspending",
            "session_id": row["session_id"],
            "task_id": row["task_id"],
            "computer_lost": not current,
            "idle_suspended": row["state"] == "idle",
            "idle_timeout_seconds": self.idle_seconds,
            "idle_remaining_seconds": max(
                0, self.idle_seconds - (self.wall_clock() - row["updated_at"])
            ),
        }

    def status(self, bot_id):
        row = self._row(bot_id)
        if row and row["state"] in {"opening", "suspending", "returning"}:
            return {
                "state": row["state"] if row["state"] in {"suspending", "returning"} else "agent",
                "available": False,
            }
        # A screen read must not wait behind queued gestures and expire a fresh
        # frame. If another operation owns the computer, report a read-only
        # snapshot; grant cleanup and all input admission stay serialized.
        with self._operations.hold(bot_id, blocking=False) as admitted:
            return self._status(bot_id, settle=admitted)

    def request(self, bot_id, data):
        row = self._row(bot_id)
        if row and row["state"] in {"opening", "suspending", "returning"}:
            raise Conflict("The computer is changing state. Wait before sending input.")
        # A status check or admitted input can already own this computer's lock.
        # Recheck the current session, grant and expiry after it settles instead
        # of rejecting a valid heartbeat. Different computers remain independent.
        with self._operations.hold(bot_id):
            try:
                result = self._request(bot_id, data)
            finally:
                if self._stopped:
                    self._grants.pop(bot_id, None)
            if self._stopped:
                raise Conflict("The service is stopping. Computer control is unavailable.")
            return result

    def begin_return(self, bot_id, data):
        """Durably fence input before admitting a bounded background checkpoint."""
        with self._operations.hold(bot_id, blocking=False) as admitted:
            if not admitted:
                raise Conflict(
                    "The computer is finishing an input. Try giving control back shortly."
                )
            if self._stopped:
                raise Conflict("The computer service is stopping.")
            row = self._row(bot_id)
            view, _, active = self.views.current(self.owner, bot_id)
            grant = self._grants.get(bot_id)
            if grant and self.clock() - grant.seen > 15 and not grant.input_pending:
                grant = None
            own = (
                grant and grant.client_id == data.client_id and grant.session_id == data.session_id
            )
            self._begin_return(bot_id, data, row, view, active, grant, own)
        return {
            "state": "returning",
            "available": False,
            "session_id": data.session_id,
            "computer_lost": view is None,
        }

    def _begin_return(self, bot_id, data, row, view, active, grant, own):
        if not row or row["session_id"] != data.session_id:
            raise Conflict("This control session has ended. Refresh the computer.")
        if grant and not own:
            raise Conflict("Another window controls this computer.")
        if row["state"] not in {"paused", "idle"} or active:
            raise Conflict("The Owl is still changing state. Wait before giving control back.")
        if view and (view.generation != data.session_id or not view.alive()):
            raise Conflict("The computer changed. Reconnect before resuming.")
        with self.store.connect(write=True) as db:
            from .hosted_maintenance import MaintenanceLease

            if MaintenanceLease.pending(db):
                raise Conflict("Cloud maintenance is in progress. Try again shortly.")
            if row["task_id"]:
                task = self.store._task(db, self.owner, row["task_id"])
                if (
                    task["status"] != "waiting"
                    or not db.execute(
                        "SELECT 1 FROM background_turns WHERE task_id=? AND state='computer_control'",
                        (row["task_id"],),
                    ).fetchone()
                ):
                    raise Conflict("The task is not paused for computer control.")
            db.execute(
                "UPDATE bot_computer_control SET state='returning' WHERE bot_id=? AND session_id=?",
                (bot_id, data.session_id),
            )
        self._grants.pop(bot_id, None)
        self.views.fence(self.owner, bot_id, True)

    def complete_return(self, bot_id, data, *, require_live=False):
        with self._operations.hold(bot_id):
            row = self._row(bot_id)
            if (
                self._stopped
                or not row
                or row["state"] != "returning"
                or row["session_id"] != data.session_id
            ):
                raise Conflict("This computer return has ended.")
            view, _, _ = self.views.current(self.owner, bot_id)
            try:
                if require_live and view is None:
                    raise Conflict(
                        "The computer disconnected before saving. Reconnect before giving control back."
                    )
                return self._complete_return(bot_id, data, row, view)
            except BaseException:
                self.abort_return(bot_id, data.session_id)
                raise

    def abort_return(self, bot_id, session):
        with self._operations.hold(bot_id), self.store.connect(write=True) as db:
            db.execute(
                "UPDATE bot_computer_control SET state='paused',updated_at=? WHERE bot_id=? AND session_id=? AND state='returning'",
                (self.wall_clock(), bot_id, session),
            )

    def _complete_return(self, bot_id, data, row, view):
        # No stale screen/permission may authorize the continuation.
        if view:
            if view.generation != data.session_id or not view.alive():
                raise Conflict("The computer changed. Reconnect before resuming.")
            try:
                # Watching uses this same lock. Drain its admitted capture
                # before cleanup and a fresh frame; otherwise a cloud
                # executor's nonblocking capture rejects a valid return.
                with view.lock:
                    view.human_input({"kind": "release"})
                    view.capture()
                    if view.checkpoint:
                        view.checkpoint(data.session_id)
            except Exception:
                self._grants.pop(bot_id, None)
                raise Conflict(
                    "Saved work or the fresh screen could not be confirmed. The Owl remains paused; try giving control back again."
                ) from None
        self.views.invalidate(self.owner, bot_id)
        if self._stopped:
            raise Conflict("The service is stopping. Computer control is unavailable.")
        try:
            with self.store.connect(write=True) as db:
                current = db.execute(
                    "SELECT * FROM bot_computer_control WHERE bot_id=?", (bot_id,)
                ).fetchone()
                if (
                    self._stopped
                    or not current
                    or current["session_id"] != data.session_id
                    or current["state"] != "returning"
                ):
                    raise Conflict("This computer return has ended. The Owl was not resumed.")
                if row["task_id"]:
                    task = self.store._task(db, self.owner, row["task_id"])
                    if self._stopped or task["status"] != "waiting":
                        raise Conflict("This task has stopped.")
                    saved = db.execute(
                        "SELECT messages FROM background_turns WHERE task_id=? AND state='computer_control'",
                        (task["id"],),
                    ).fetchone()
                    if not saved:
                        raise Conflict("The task has not saved its pause yet.")
                    messages = json.loads(saved[0])
                    messages.append(
                        {
                            "role": "system",
                            "_bot_control_return": task["id"],
                            "content": "The user explicitly gave computer control back. The preceding human handoff is complete; continue this task from the current state instead of requesting that same handoff again. Human actions may have changed the page, files or sign-in state. Observe current state before any action; discard old action arguments and observations. Do not repeat a submission the user may already have completed. Request another handoff only if newly observed state needs further human help. Existing task scope and approval rules still apply. "
                            + (
                                "The previous computer was lost; reconstruct only from saved files and verified current state, never claim unsaved applications survived."
                                if view is None
                                else ""
                            ),
                        }
                    )
                    db.execute(
                        "UPDATE background_turns SET state='queued',messages=? WHERE task_id=?",
                        (encode(messages), task["id"]),
                    )
                    db.execute(
                        "UPDATE bot_action_reviews SET status='expired',revision=revision+1 WHERE task_id=? AND status IN ('pending','approved')",
                        (task["id"],),
                    )
                    self.store._event(
                        db,
                        task["id"],
                        "computer_control_returned",
                        {
                            "message": "You gave control back. The Bot will inspect the current state."
                        },
                    )
                if row["task_id"] is None:
                    db.execute(
                        "UPDATE bot_computer_control SET state='viewing',updated_at=? WHERE bot_id=?",
                        (self.wall_clock(), bot_id),
                    )
                else:
                    db.execute("DELETE FROM bot_computer_control WHERE bot_id=?", (bot_id,))
        except BaseException:
            self.views.fence(self.owner, bot_id, True)
            raise
        self._grants.pop(bot_id, None)
        self.views.fence(self.owner, bot_id, False)
        return {"state": "agent", "available": bool(view)}

    def _request(self, bot_id, data):
        if self._stopped:
            raise Conflict("The service is stopping. Computer control is unavailable.")
        status = self._status(bot_id)
        if self._stopped:
            raise Conflict("The service is stopping. Computer control is unavailable.")
        row = self._row(bot_id)
        if row and row["state"] in {"suspending", "returning"}:
            raise Conflict("The idle computer is saving its checkpoint. Wait before resuming.")
        view, task_id, active = self.views.current(self.owner, bot_id)
        grant = self._grants.get(bot_id)
        own = grant and grant.client_id == data.client_id and grant.session_id == data.session_id
        if data.action == "take":
            if (
                not view
                or not view.human_input
                or not view.alive()
                or view.generation != data.session_id
            ):
                raise Conflict("This computer session is no longer available. Refresh its screen.")
            if grant and not own:
                raise Conflict(
                    "Another window controls this computer. Give control back there first."
                )
            if self._stopped:
                raise Conflict("The service is stopping. Computer control is unavailable.")
            if row and row["state"] == "viewing":
                self._delete(bot_id)
                row = None
            if not row:
                self.views.fence(self.owner, bot_id, True)
                try:
                    with self.store.connect(write=True) as db:
                        if task_id:
                            task = self.store._task(db, self.owner, task_id)
                            if task["status"] not in {"running", "waiting"}:
                                raise Conflict("This task already stopped.")
                        db.execute(
                            "INSERT INTO bot_computer_control VALUES(?,?,?,?,?)",
                            (
                                bot_id,
                                task_id,
                                view.generation,
                                "pausing" if task_id else "paused",
                                self.wall_clock(),
                            ),
                        )
                except BaseException:
                    self.views.fence(self.owner, bot_id, False)
                    raise
            elif row["session_id"] != data.session_id:
                raise Conflict(
                    "The computer changed. Resume from saved work before taking control again."
                )
            if view.prepare_input:
                view.prepare_input()
            if not own:
                self._grants[bot_id] = Grant(data.client_id, data.session_id, self.clock())
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE bot_computer_control SET updated_at=? WHERE bot_id=?",
                    (self.wall_clock(), bot_id),
                )
            return self._status(bot_id)
        if not row or row["session_id"] != data.session_id:
            raise Conflict("This control session has ended. Refresh the computer.")
        if data.action == "disconnect":
            if own:
                self._grants.pop(bot_id, None)
                if view and view.human_input and view.generation == data.session_id:
                    try:
                        view.human_input({"kind": "release"})
                    except Exception:
                        pass  # The Bot stays paused; give-back verifies cleanup.
            return self._status(bot_id)
        if data.action == "resume":
            self._begin_return(bot_id, data, row, view, active, grant, own)
            return self.complete_return(bot_id, data, require_live=view is not None)
        if not own:
            raise Conflict(
                "Your input connection expired. Take control again; the Bot remains paused."
            )
        if data.action == "heartbeat":
            grant.seen = self.clock()
            return self._status(bot_id)
        if (
            status["state"] != "controlling"
            or active
            or not view
            or view.generation != data.session_id
        ):
            raise Conflict("Input is unavailable until the Bot is paused on this computer.")
        if data.sequence != grant.sequence + 1 or data.event is None:
            raise Conflict("Input order changed. Reconnect without repeating the last input.")
        grant.sequence = data.sequence  # Consume before dispatch, including uncertain failures.
        grant.seen = self.clock()
        if self._stopped:
            raise Conflict("The service is stopping. Computer control is unavailable.")
        grant.input_pending = True
        try:
            view.human_input(data.event)
            # Publish activity before allowing a concurrent snapshot to expire
            # the acknowledged gesture. Never renew an unconfirmed receipt.
            grant.seen = self.clock()
        except Exception:
            self._grants.pop(bot_id, None)
            raise Conflict(
                "Input could not be confirmed. Inspect the screen before taking control again; it was not retried."
            ) from None
        finally:
            grant.input_pending = False
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE bot_computer_control SET updated_at=? WHERE bot_id=? AND session_id=?",
                (self.wall_clock(), bot_id, data.session_id),
            )
        return {"state": "controlling", "sequence": grant.sequence}

    def offer(self, task, reason):
        """A Bot can request a handoff, but cannot grant itself human input."""
        with self._operations.hold(task["bot_id"]):
            view, claimed, _ = self.views.current(self.owner, task["bot_id"])
            if not view or not view.human_input or not view.alive() or claimed != task["id"]:
                raise ValueError(
                    "Open this task's supported computer before requesting human control."
                )
            if not self._row(task["bot_id"]):
                self.views.fence(self.owner, task["bot_id"], True)
                try:
                    with self.store.connect(write=True) as db:
                        current = self.store._task(db, self.owner, task["id"])
                        if not self.store.executing(db, current):
                            raise Conflict("The task stopped before the handoff.")
                        db.execute(
                            "INSERT INTO bot_computer_control VALUES(?,?,?,?,?)",
                            (
                                task["bot_id"],
                                task["id"],
                                view.generation,
                                "pausing",
                                self.wall_clock(),
                            ),
                        )
                        self.store._event(
                            db, task["id"], "computer_control_requested", {"message": reason}
                        )
                except BaseException:
                    self.views.fence(self.owner, task["bot_id"], False)
                    raise
            raise ComputerControlPause()

    def park(self, task, messages):
        with self._operations.hold(task["bot_id"]):
            row = self._row(task["bot_id"])
            if not row or row["task_id"] != task["id"]:
                raise Conflict("Another task owns the paused computer.")
            checkpoint = json.loads(encode(messages))
            last = next(
                (i for i in range(len(checkpoint) - 1, -1, -1) if checkpoint[i].get("tool_calls")),
                None,
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
                                "content": encode({"executed": False, "status": "human_control"}),
                            }
                        )
            checkpoint.append(
                {
                    "role": "system",
                    "content": "The user took computer control. Unexecuted actions were discarded. Preserve successful work and inspect fresh state after the user gives control back. Human keyboard input is not part of this conversation.",
                }
            )
            with self.store.connect(write=True) as db:
                current = self.store._task(db, self.owner, task["id"])
                if not self.store.executing(db, current):
                    raise Conflict("This task stopped before computer control was ready.")
                db.execute(
                    "INSERT INTO background_turns VALUES(?,'computer_control',?,?,0) ON CONFLICT(task_id) DO UPDATE SET state='computer_control',messages=excluded.messages",
                    (task["id"], "Waiting while you control the computer", encode(checkpoint)),
                )
                db.execute(
                    "UPDATE tasks SET status='waiting',updated_at=? WHERE id=?",
                    (time.time(), task["id"]),
                )
                db.execute(
                    "UPDATE bot_computer_control SET state='paused',updated_at=? WHERE bot_id=?",
                    (self.wall_clock(), task["bot_id"]),
                )
                self.store._event(
                    db,
                    task["id"],
                    "computer_control_taken",
                    {"message": "The Bot is paused while you control its computer."},
                )
