"""Durable workspace pointers; terminal task state decides publication.

No provider-specific snapshot IDs enter this format. The caller must hold its
workspace lease and, for shared storage, its fenced worker epoch.
"""

import time
from uuid import uuid4

from .object_storage import ObjectRef, ObjectStorageError
from .store import ACTIVE, Conflict


class WorkspaceObjects:
    def __init__(self, store):
        if store.objects is None:
            raise ObjectStorageError("Workspace object storage is not configured.")
        self.store = store

    def current(self, owner, bot_id, task_id=None):
        """The workspace to start from. A task resuming after its own pause gets its own state."""
        with self.store.connect() as db:
            self.store._bot(db, owner, bot_id)
            rows = db.execute(
                "SELECT w.ref,w.settled,w.task_id,t.status FROM workspace_objects w LEFT JOIN tasks t ON t.id=w.task_id "
                "WHERE w.bot_id=? ORDER BY w.created_at DESC,w.id DESC",
                (bot_id,),
            ).fetchall()
            if any(
                row["status"] in ACTIVE
                and not row["settled"]
                and (task_id is None or row["task_id"] != task_id)
                for row in rows
            ):
                raise Conflict("Previous workspace task is still finalizing; try again later.")
            return next(
                (
                    ObjectRef.decode(row["ref"])
                    for row in rows
                    if row["settled"]
                    or row["status"] in (None, "completed")
                    or (task_id is not None and row["task_id"] == task_id)
                ),
                None,
            )

    def stage(self, owner, bot_id, task_id, chunks, size, *, cancel=lambda: False):
        with self.store.connect() as db:
            self._check_task(db, owner, bot_id, task_id)
        ref = self.store.put_object(owner, bot_id, chunks, size, cancel=cancel)
        with self.store.connect(write=True) as db:
            self._check_task(db, owner, bot_id, task_id)
            if cancel():
                raise Conflict("Workspace task was cancelled; previous checkpoint retained.")
            # A task that paused and resumed checkpoints again: its newer state replaces its
            # own earlier, still unpublished one. Retention removes the unreferenced object.
            db.execute("DELETE FROM workspace_objects WHERE task_id=? AND settled=0", (task_id,))
            self.store.publish_object(db, owner, bot_id, ref)
            db.execute(
                "INSERT INTO workspace_objects(id,task_id,bot_id,ref,created_at,settled) VALUES(?,?,?,?,?,?) "
                "",
                (
                    uuid4().hex,
                    task_id,
                    bot_id,
                    ref.encode(),
                    time.time(),
                    0,
                ),
            )
        return ref

    def stage_pause(
        self,
        owner,
        bot_id,
        task_id,
        session,
        chunks,
        size,
        *,
        browser_states=None,
        browser_data=None,
        cancel=lambda: False,
    ):
        """Publish a quiescent workspace and browser state in one transaction."""
        with self.store.connect() as db:
            self.check_checkpoint(db, owner, bot_id, task_id, session)
        workspace = self.store.put_object(owner, bot_id, chunks, size, cancel=cancel)
        browser = (
            browser_states.prepare(owner, bot_id, browser_data, cancel=cancel)
            if browser_data is not None
            else None
        )
        with self.store.connect(write=True) as db:
            self.check_checkpoint(db, owner, bot_id, task_id, session)
            if cancel():
                raise Conflict("Idle checkpoint cancelled; previous state retained.")
            for table, ref in (("workspace_objects", workspace), ("browser_states", browser)):
                if ref is None:
                    continue
                self.store.publish_object(db, owner, bot_id, ref)
                db.execute(
                    f"INSERT INTO {table}(id,task_id,bot_id,ref,created_at,settled) VALUES(?,?,?,?,?,1) "
                    "",
                    (uuid4().hex, task_id, bot_id, ref.encode(), time.time()),
                )
        return workspace

    def check_checkpoint(self, db, owner, bot_id, task_id, pause_session=None):
        return check_checkpoint(self.store, db, owner, bot_id, task_id, pause_session)

    def _check_task(self, db, owner, bot_id, task_id):
        self.store._bot(db, owner, bot_id)
        task = self.store._task(db, owner, task_id)
        if task["bot_id"] != bot_id or not self.store.executing(db, task):
            raise Conflict("This task cannot checkpoint this bot's workspace.")

    def import_baseline(self, owner, bot_id, chunks, size):
        """Import a settled legacy archive while the caller holds its bot lease."""
        if self.current(owner, bot_id) is not None:
            raise Conflict("A durable workspace already exists.")
        ref = self.store.put_object(owner, bot_id, chunks, size)
        # Check the durable copy before switching the pointer. Retain the old
        # local archive so an operator has an independent rollback source.
        for _ in self.store.objects.read(ref):
            pass
        with self.store.connect(write=True) as db:
            self.store._bot(db, owner, bot_id)
            if db.execute("SELECT 1 FROM workspace_objects WHERE bot_id=?", (bot_id,)).fetchone():
                raise Conflict("Workspace changed while importing its baseline.")
            self.store.publish_object(db, owner, bot_id, ref)
            db.execute(
                "INSERT INTO workspace_objects(id,task_id,bot_id,ref,created_at) VALUES(?,NULL,?,?,?)",
                (
                    uuid4().hex,
                    bot_id,
                    ref.encode(),
                    time.time(),
                ),
            )
        return ref


def check_checkpoint(store, db, owner, bot_id, task_id, pause_session=None):
    if pause_session is None:
        store._bot(db, owner, bot_id)
        task = store._task(db, owner, task_id)
        if task["bot_id"] != bot_id or not store.executing(db, task):
            raise Conflict("This task cannot checkpoint this bot's workspace.")
        return
    store._bot(db, owner, bot_id)
    if task_id is None:
        if not db.execute(
            "SELECT 1 FROM bot_computer_control WHERE bot_id=? AND task_id IS NULL "
            "AND session_id=? AND state IN ('suspending','returning')",
            (bot_id, pause_session),
        ).fetchone():
            raise Conflict("This viewing session cannot publish a checkpoint.")
        return
    task = store._task(db, owner, task_id)
    if (
        task["bot_id"] != bot_id
        or task["status"] != "waiting"
        or not db.execute(
            "SELECT 1 FROM bot_computer_control c JOIN background_turns j ON j.task_id=c.task_id "
            "WHERE c.bot_id=? AND c.task_id=? AND c.session_id=? AND c.state IN ('suspending','returning') "
            "AND j.state='computer_control'",
            (bot_id, task_id, pause_session),
        ).fetchone()
    ):
        raise Conflict("This paused computer cannot publish a checkpoint.")
