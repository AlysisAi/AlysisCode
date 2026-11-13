"""Durable bounded uploads. Partial bytes never appear in the bot's library."""

from __future__ import annotations

import base64
import hashlib
import time

from pydantic import Field

from .files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from .models import FileInput, Input
from .store import Conflict, NotFound, Store, uid


class UploadInput(Input):
    previous_file_id: str | None = Field(default=None, min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    size: int = Field(ge=0, le=MAX_FILE_BYTES, strict=True)
    request_id: str = Field(min_length=8, max_length=128)


class UploadChunk(Input):
    offset: int = Field(ge=0, le=MAX_FILE_BYTES, strict=True)
    data_base64: str = Field(min_length=1, max_length=4 * ((FILE_CHUNK_BYTES + 2) // 3))


class FileTransfers:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner
        self.expire()

    def expire(self):
        with self.store.connect(write=True) as db:
            db.execute(
                "DELETE FROM file_uploads WHERE expires<? AND bot_id IN (SELECT id FROM bots WHERE owner=?)",
                (time.time(), self.owner),
            )

    @staticmethod
    def receipt(row):
        return {
            k: row[k] for k in ("id", "name", "size", "received", "status", "expires", "file_id")
        }

    def _upload(self, db, bot_id, upload_id):
        self.store._bot(db, self.owner, bot_id)
        row = db.execute(
            "SELECT rowid AS payload_row,* FROM file_uploads WHERE id=? AND bot_id=?",
            (upload_id, bot_id),
        ).fetchone()
        if row is None or row["expires"] < time.time():
            raise NotFound("Upload expired or was not found. Start a new upload.")
        return row

    def read(self, bot_id, upload_id):
        with self.store.connect() as db:
            return self.receipt(self._upload(db, bot_id, upload_id))

    def begin(self, bot_id, data: UploadInput, *, authorize=None):
        name = FileInput(name=data.name, content="").name
        with self.store.connect(write=True) as db:
            if authorize:
                authorize(db)
            self.store._bot(db, self.owner, bot_id)
            db.execute(
                "DELETE FROM file_uploads WHERE expires<? AND bot_id IN (SELECT id FROM bots WHERE owner=?)",
                (time.time(), self.owner),
            )
            old = db.execute(
                "SELECT * FROM file_uploads WHERE bot_id=? AND request_id=?",
                (bot_id, data.request_id),
            ).fetchone()
            if old:
                if (
                    old["name"] != name
                    or old["size"] != data.size
                    or old["previous_file_id"] != data.previous_file_id
                ):
                    raise Conflict("This upload request already identifies a different file.")
                return self.receipt(old)
            self.store._revision_base(db, bot_id, name, data.previous_file_id)
            if db.execute(
                "SELECT 1 FROM file_uploads WHERE bot_id=? AND name=? AND status='pending'",
                (bot_id, name),
            ).fetchone():
                raise Conflict("A file or pending upload already has this name.")
            # Same write transaction covers admission and allocation. All bots
            # of this owner share the allowance; memories/notes are excluded.
            self.store._check_file_write(
                db,
                self.owner,
                bot_id,
                FileInput(name=name, content="", previous_file_id=data.previous_file_id),
                None,
                data.size,
                None,
            )
            upload_id = uid()
            self.store.allocate_upload(
                db,
                (
                    upload_id,
                    bot_id,
                    name,
                    data.size,
                    0,
                    time.time() + 3600,
                    data.request_id,
                    data.size,
                    data.previous_file_id,
                ),
            )
            return self.receipt(self._upload(db, bot_id, upload_id))

    def append(self, bot_id, upload_id, data: UploadChunk, *, authorize=None):
        try:
            chunk = base64.b64decode(data.data_base64, validate=True)
        except ValueError:
            raise ValueError("Invalid upload chunk encoding.") from None
        if not 0 < len(chunk) <= FILE_CHUNK_BYTES:
            raise ValueError("Upload chunks must contain at most 256 KiB.")
        with self.store.connect(write=True) as db:
            if authorize:
                authorize(db)
            row = self._upload(db, bot_id, upload_id)
            if row["status"] != "pending":
                raise Conflict("Upload no longer accepts data. Check its saved status.")
            end = data.offset + len(chunk)
            if end > row["size"] or data.offset > row["received"]:
                raise Conflict("Chunk exceeds the expected size or skips unwritten bytes.")
            with self.store.upload_blob(db, row) as blob:
                blob.seek(data.offset)
                if data.offset < row["received"]:
                    if end > row["received"] or blob.read(len(chunk)) != chunk:
                        raise Conflict("Retry bytes differ from the already accepted chunk.")
                else:
                    blob.write(chunk)
            db.execute(
                "UPDATE file_uploads SET received=CASE WHEN received>? THEN received ELSE ? END,expires=? WHERE id=?",
                (end, end, time.time() + 3600, upload_id),
            )
            return self.receipt(self._upload(db, bot_id, upload_id))

    def complete(self, bot_id, upload_id, *, authorize=None):
        object_ref = None
        if self.store.objects is not None:
            # Hold only a read snapshot while uploading. Stop/cancel and other
            # writes remain available; publication rechecks the upload below.
            with self.store.connect() as db:
                if authorize:
                    authorize(db)
                row = self._upload(db, bot_id, upload_id)
                if row["status"] == "complete":
                    return self.receipt(row)
                if row["status"] != "pending" or row["received"] != row["size"]:
                    raise Conflict("Upload is incomplete or cancelled; no file was published.")

                def source():
                    with self.store.upload_blob(db, row, readonly=True) as blob:
                        while chunk := blob.read(FILE_CHUNK_BYTES):
                            yield chunk

                object_ref = self.store.stage_file_object(
                    self.owner,
                    bot_id,
                    FileInput(
                        name=row["name"], content="", previous_file_id=row["previous_file_id"]
                    ),
                    source(),
                    row["size"],
                    exclude_upload=upload_id,
                )
        with self.store.connect(write=True) as db:
            if authorize:
                authorize(db)
            row = self._upload(db, bot_id, upload_id)
            if row["status"] == "complete":
                return self.receipt(row)
            if row["status"] != "pending" or row["received"] != row["size"]:
                raise Conflict("Upload is incomplete or cancelled; no file was published.")

            def chunks():
                with self.store.upload_blob(db, row, readonly=True) as blob:
                    while chunk := blob.read(FILE_CHUNK_BYTES):
                        yield chunk

            saved = self.store._save_file(
                self.owner,
                bot_id,
                FileInput(name=row["name"], content="", previous_file_id=row["previous_file_id"]),
                chunks=chunks(),
                binary_size=row["size"],
                db=db,
                exclude_upload=upload_id,
                object_ref=object_ref,
            )
            self.store.release_upload(db, upload_id)
            db.execute(
                "UPDATE file_uploads SET data=NULL,status='complete',file_id=?,expires=? WHERE id=?",
                (saved["id"], time.time() + 3600, upload_id),
            )
            return self.receipt(self._upload(db, bot_id, upload_id))

    def cancel(self, bot_id, upload_id):
        with self.store.connect(write=True) as db:
            row = self._upload(db, bot_id, upload_id)
            if row["status"] == "pending":
                self.store.release_upload(db, upload_id)
                db.execute(
                    "UPDATE file_uploads SET data=NULL,status='cancelled' WHERE id=?", (upload_id,)
                )
            return self.receipt(self._upload(db, bot_id, upload_id))

    def chunk(self, bot_id, file_id, offset):
        from contextlib import closing

        file = self.store.file(self.owner, bot_id, file_id)
        with closing(self.store.file_chunks(self.owner, bot_id, file_id, offset=offset)) as chunks:
            data = next(chunks, b"")
        return {
            "offset": offset,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "next_offset": offset + len(data),
            "size": file["size_bytes"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "file_sha256": file["sha256"],
        }
