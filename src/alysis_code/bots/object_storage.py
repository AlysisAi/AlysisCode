"""Immutable bot payloads, independent of the computer and cloud provider.

The trusted database owns references and authorization. Keys contain no user
filenames. Each bounded range is verified before it can leave this layer, so
resumable downloads do not need to reread an entire object for every chunk.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ..cancellation import CooperativeCancellationError
from .files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from .workspaces import sync_directory


class ObjectStorageError(RuntimeError):
    """Sanitized failure: never include credentials, URLs or upstream bodies."""


@dataclass(frozen=True)
class ObjectRef:
    key: str
    size: int
    sha256: str
    chunks: tuple[str, ...]

    def __post_init__(self):
        parts = self.key.split("/")
        if len(parts) != 3 or parts[0] != "bots-v1":
            raise ValueError("Invalid object key.")
        if len(parts[1]) != 64 or any(c not in "0123456789abcdef" for c in parts[1]):
            raise ValueError("Invalid object namespace.")
        if UUID(parts[2]).hex != parts[2]:
            raise ValueError("Invalid object identity.")
        if type(self.size) is not int or not 0 <= self.size <= MAX_FILE_BYTES:
            raise ValueError("Invalid object size.")
        if len(self.chunks) != (self.size + FILE_CHUNK_BYTES - 1) // FILE_CHUNK_BYTES:
            raise ValueError("Invalid object chunk count.")
        for digest in (self.sha256, *self.chunks):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid object digest.")

    def encode(self):
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def decode(cls, value):
        data = json.loads(value)
        return cls(data["key"], data["size"], data["sha256"], tuple(data["chunks"]))


def check_transfer(cancel, deadline):
    if cancel():
        raise CooperativeCancellationError("Object transfer cancelled.")
    if time.monotonic() >= deadline:
        raise ObjectStorageError("Object transfer exceeded its deadline.")


class _CheckedReader:
    def __init__(self, stream, cancel, deadline):
        self.stream, self.cancel, self.deadline = stream, cancel, deadline

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, *args):
        check_transfer(self.cancel, self.deadline)
        return self.stream.read(*args)


class ObjectStorage:
    """Adapters implement immutable uploads, bounded reads and idempotent deletion.

    A failed upload publishes no database reference. It may leave an unreachable
    object; retention/reconciliation is a separate controller responsibility.
    """

    def put(self, owner, bot_id, chunks, size, *, cancel=lambda: False, before_upload=None):
        if not owner or not bot_id or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("Explicit owner, bot and bounded object size are required.")
        scope = hashlib.sha256(json.dumps([owner, bot_id]).encode()).hexdigest()
        key = f"bots-v1/{scope}/{uuid4().hex}"
        deadline = time.monotonic() + 120
        digest, received = hashlib.sha256(), 0
        with tempfile.TemporaryFile() as stream:
            for chunk in chunks:
                check_transfer(cancel, deadline)
                if (
                    not isinstance(chunk, bytes)
                    or len(chunk) > FILE_CHUNK_BYTES
                    or received + len(chunk) > size
                ):
                    raise ValueError("Invalid object stream; no file was published.")
                stream.write(chunk)
                digest.update(chunk)
                received += len(chunk)
            if received != size:
                raise ValueError("Incomplete object stream; no file was published.")
            stream.seek(0)
            hashes = tuple(
                hashlib.sha256(chunk).hexdigest()
                for chunk in iter(lambda: stream.read(FILE_CHUNK_BYTES), b"")
            )
            ref = ObjectRef(key, size, digest.hexdigest(), hashes)
            stream.seek(0)
            check_transfer(cancel, deadline)
            if before_upload is not None:
                before_upload(ref)
            self._upload(ref, _CheckedReader(stream, cancel, deadline))
            check_transfer(cancel, deadline)
            return ref

    def read(self, ref, *, offset=0, cancel=lambda: False):
        if type(offset) is not int or not 0 <= offset <= ref.size:
            raise ValueError("Invalid object read offset.")
        deadline = time.monotonic() + 120
        if ref.size == 0:
            check_transfer(cancel, deadline)
            if self._range(ref, 0, 0) != b"":
                raise ObjectStorageError("Stored empty object failed its integrity check.")
        for index in range(offset // FILE_CHUNK_BYTES, len(ref.chunks)):
            check_transfer(cancel, deadline)
            start = index * FILE_CHUNK_BYTES
            length = min(FILE_CHUNK_BYTES, ref.size - start)
            data = self._range(ref, start, length)
            check_transfer(cancel, deadline)
            if len(data) != length or hashlib.sha256(data).hexdigest() != ref.chunks[index]:
                raise ObjectStorageError("Stored object failed its integrity check.")
            yield data[max(0, offset - start) :]

    def delete(self, ref):
        """Idempotently remove one registered immutable object, never a prefix."""
        raise NotImplementedError

    def _upload(self, ref, stream):
        raise NotImplementedError

    def _range(self, ref, offset, length):
        raise NotImplementedError

    def copy_from(self, source, ref, *, cancel=lambda: False):
        """Copy to an empty destination namespace, preserving database references.

        The operator switches a stopped controller only after every referenced
        object is copied and verified. This is not live replication or failover.
        """
        digest = hashlib.sha256()
        deadline = time.monotonic() + 120
        with tempfile.TemporaryFile() as stream:
            for chunk in source.read(ref, cancel=cancel):
                digest.update(chunk)
                stream.write(chunk)
            if stream.tell() != ref.size or digest.hexdigest() != ref.sha256:
                raise ObjectStorageError("Migration source failed its integrity check.")
            stream.seek(0)
            check_transfer(cancel, deadline)
            self._upload(ref, _CheckedReader(stream, cancel, deadline))
        for _ in self.read(ref, cancel=cancel):
            pass


class LocalObjectStorage(ObjectStorage):
    """Private local adapter for development and storage migration checks."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _upload(self, ref, stream):
        path = self.root / ref.key
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
                pending = Path(out.name)
                shutil.copyfileobj(stream, out, FILE_CHUNK_BYTES)
                out.flush()
                os.fsync(out.fileno())
            os.link(pending, path)  # create only; never replace an existing object
            sync_directory(path.parent)
        except OSError:
            raise ObjectStorageError("Object upload failed; no file was published.") from None
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def delete(self, ref):
        try:
            (self.root / ref.key).unlink(missing_ok=True)
        except OSError:
            raise ObjectStorageError("Object deletion could not be confirmed.") from None

    def _range(self, ref, offset, length):
        try:
            with (self.root / ref.key).open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != ref.size:
                    raise ObjectStorageError("Stored object size changed.")
                stream.seek(offset)
                return stream.read(length)
        except OSError:
            raise ObjectStorageError("Stored object is unavailable.") from None
