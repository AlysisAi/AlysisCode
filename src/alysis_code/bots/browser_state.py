"""Encrypted, account/bot-bound clean browser snapshots, outside the Library."""

import json
import os
import time
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from .object_storage import ObjectRef, ObjectStorageError
from .store import ACTIVE, Conflict


class BrowserStates:
    def __init__(self, store, key: bytes):
        if store is None or store.objects is None:
            raise ValueError("Persistent browser storage requires object storage.")
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("Browser storage requires a stable 32-byte encryption key.")
        self.store, self._cipher = store, AESGCM(key)

    @staticmethod
    def _aad(owner, bot_id):
        return json.dumps(["alysis-browser-v1", owner, bot_id], separators=(",", ":")).encode()

    @staticmethod
    def _select(db, bot_id, task_id=None, *, strict=True):
        """The saved storage a browser starts from: the newest settled or completed copy, the
        baseline (storage that belongs to no task), or a resuming task's own latest copy."""
        rows = db.execute(
            "SELECT b.ref,b.settled,b.task_id,b.created_at,t.status FROM browser_states b "
            "JOIN tasks t ON t.id=b.task_id WHERE b.bot_id=? ORDER BY b.created_at DESC,b.id DESC",
            (bot_id,),
        ).fetchall()
        if strict and any(
            row["status"] in ACTIVE
            and not row["settled"]
            and (task_id is None or row["task_id"] != task_id)
            for row in rows
        ):
            raise Conflict("Previous browser task is still finalizing; try again later.")
        baseline = db.execute(
            "SELECT ref,created_at FROM browser_baselines WHERE bot_id=?", (bot_id,)
        ).fetchone()
        for row in rows:
            if baseline is not None and baseline["created_at"] >= row["created_at"]:
                break
            if (
                row["settled"]
                or row["status"] == "completed"
                or (task_id is not None and row["task_id"] == task_id)
            ):
                return ObjectRef.decode(row["ref"])
        return ObjectRef.decode(baseline["ref"]) if baseline is not None else None

    def read(self, owner, bot_id, ref, *, cancel=lambda: False):
        encrypted = b"".join(self.store.objects.read(ref, cancel=cancel))
        try:
            if encrypted[:4] != b"ABS1":
                raise ValueError("version")
            return self._cipher.decrypt(encrypted[4:16], encrypted[16:], self._aad(owner, bot_id))
        except (InvalidTag, ValueError):
            raise ObjectStorageError(
                "Browser storage could not be decrypted; the existing state was retained."
            ) from None

    def current(self, owner, bot_id, *, cancel=lambda: False, task_id=None):
        """Saved website storage to start from; a resuming task gets its own latest state."""
        with self.store.connect() as db:
            self.store._bot(db, owner, bot_id)
            ref = self._select(db, bot_id, task_id)
        return None if ref is None else self.read(owner, bot_id, ref, cancel=cancel)

    def saved(self, owner, bot_id):
        """The storage the owl's next browser starts from, without waiting for running work."""
        with self.store.connect() as db:
            self.store._bot(db, owner, bot_id)
            return self._select(db, bot_id, strict=False)

    @classmethod
    def _starts(cls, db, bot_id):
        """What new work starts from, and what each waiting task resumes from when that is its
        own copy."""
        shared = cls._select(db, bot_id, strict=False)
        own = {}
        for row in db.execute(
            "SELECT DISTINCT b.task_id FROM browser_states b JOIN tasks t ON t.id=b.task_id "
            "WHERE b.bot_id=? AND b.settled=0 AND t.status IN (?,?,?,?)",
            (bot_id, *ACTIVE),
        ):
            ref = cls._select(db, bot_id, row["task_id"], strict=False)
            if ref is not None and ref != shared:
                own[row["task_id"]] = ref
        return shared, own

    def edit(self, owner, bot_id, change, *, cancel=lambda: False):
        """Apply ``change`` (archive bytes in; new bytes, or None when unchanged, out) to every
        copy a browser could start from: the one new work starts from, which becomes the
        baseline, and each waiting task's own copy. Older copies, which still hold what was
        removed, are dropped. The caller keeps the owl's computer closed meanwhile.
        """
        with self.store.connect() as db:
            self.store._bot(db, owner, bot_id)
            shared, own = self._starts(db, bot_id)
        sources = {None: shared, **own} if shared is not None else dict(own)
        replaced = {}
        for task_id, ref in sources.items():
            data = change(self.read(owner, bot_id, ref, cancel=cancel))
            if data is not None:
                replaced[task_id] = self.prepare(owner, bot_id, data, cancel=cancel)
        if not replaced:
            return False
        with self.store.connect(write=True) as db:
            self.store._bot(db, owner, bot_id)
            if cancel():
                raise Conflict("Browser storage change cancelled; the saved storage is unchanged.")
            # A browser that ran meanwhile saved a copy this change does not cover.
            if self._starts(db, bot_id) != (shared, own):
                raise Conflict("The owl's browser storage changed meanwhile; try again.")
            now = time.time()
            baseline = replaced.get(None, shared)
            db.execute("DELETE FROM browser_states WHERE bot_id=?", (bot_id,))
            db.execute("DELETE FROM browser_baselines WHERE bot_id=?", (bot_id,))
            if baseline is not None:
                self.stage_baseline(db, owner, bot_id, baseline, now)
            # A waiting task resumes from its own copy, so each stays newer than the baseline.
            for task_id, ref in own.items():
                ref = replaced.get(task_id, ref)
                self.store.publish_object(db, owner, bot_id, ref)
                db.execute(
                    "INSERT INTO browser_states(id,task_id,bot_id,ref,created_at,settled) "
                    "VALUES(?,?,?,?,?,0)",
                    (uuid4().hex, task_id, bot_id, ref.encode(), now + 0.001),
                )
        return True

    def clear(self, owner, bot_id):
        """Forget all of the owl's saved browser storage: its next browser starts empty."""
        with self.store.connect(write=True) as db:
            self.store._bot(db, owner, bot_id)
            db.execute("DELETE FROM browser_states WHERE bot_id=?", (bot_id,))
            db.execute("DELETE FROM browser_baselines WHERE bot_id=?", (bot_id,))

    def stage_baseline(self, db, owner, bot_id, ref, at=None):
        """Inside the caller's transaction: storage saved with no task becomes the baseline."""
        self.store.publish_object(db, owner, bot_id, ref)
        db.execute(
            "INSERT INTO browser_baselines(bot_id,ref,created_at) VALUES(?,?,?) "
            "ON CONFLICT(bot_id) DO UPDATE SET ref=excluded.ref,created_at=excluded.created_at",
            (bot_id, ref.encode(), time.time() if at is None else at),
        )

    def prepare(self, owner, bot_id, data: bytes, *, cancel=lambda: False):
        nonce = os.urandom(12)
        encrypted = b"ABS1" + nonce + self._cipher.encrypt(nonce, data, self._aad(owner, bot_id))
        if len(encrypted) > MAX_FILE_BYTES:
            raise ObjectStorageError(
                "Browser storage exceeds the configured object allowance; the previous state was retained."
            )
        chunks = (
            encrypted[index : index + FILE_CHUNK_BYTES]
            for index in range(0, len(encrypted), FILE_CHUNK_BYTES)
        )
        return self.store.put_object(owner, bot_id, chunks, len(encrypted), cancel=cancel)

    def stage(self, owner, bot_id, task_id, data: bytes, *, cancel=lambda: False):
        from .workspace_objects import WorkspaceObjects

        with self.store.connect() as db:
            WorkspaceObjects(self.store)._check_task(db, owner, bot_id, task_id)
        ref = self.prepare(owner, bot_id, data, cancel=cancel)
        with self.store.connect(write=True) as db:
            WorkspaceObjects(self.store)._check_task(db, owner, bot_id, task_id)
            if cancel():
                raise Conflict("Browser checkpoint cancelled; previous state retained.")
            # Paused and resumed: this task's newer storage replaces its own earlier copy.
            db.execute("DELETE FROM browser_states WHERE task_id=? AND settled=0", (task_id,))
            self.store.publish_object(db, owner, bot_id, ref)
            db.execute(
                "INSERT INTO browser_states(id,task_id,bot_id,ref,created_at,settled) VALUES(?,?,?,?,?,?) "
                "",
                (uuid4().hex, task_id, bot_id, ref.encode(), time.time(), 0),
            )
        return ref
