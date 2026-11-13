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

    def current(self, owner, bot_id, *, cancel=lambda: False, task_id=None):
        """Saved website storage to start from; a resuming task gets its own latest state."""
        with self.store.connect() as db:
            self.store._bot(db, owner, bot_id)
            rows = db.execute(
                "SELECT b.ref,b.settled,b.task_id,t.status FROM browser_states b JOIN tasks t ON t.id=b.task_id "
                "WHERE b.bot_id=? ORDER BY b.created_at DESC,b.id DESC",
                (bot_id,),
            ).fetchall()
            if any(
                row["status"] in ACTIVE
                and not row["settled"]
                and (task_id is None or row["task_id"] != task_id)
                for row in rows
            ):
                raise Conflict("Previous browser task is still finalizing; try again later.")
            ref = next(
                (
                    ObjectRef.decode(row["ref"])
                    for row in rows
                    if row["settled"]
                    or row["status"] == "completed"
                    or (task_id is not None and row["task_id"] == task_id)
                ),
                None,
            )
        if ref is None:
            return None
        encrypted = b"".join(self.store.objects.read(ref, cancel=cancel))
        try:
            if encrypted[:4] != b"ABS1":
                raise ValueError("version")
            return self._cipher.decrypt(encrypted[4:16], encrypted[16:], self._aad(owner, bot_id))
        except (InvalidTag, ValueError):
            raise ObjectStorageError(
                "Browser storage could not be decrypted; the existing state was retained."
            ) from None

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
