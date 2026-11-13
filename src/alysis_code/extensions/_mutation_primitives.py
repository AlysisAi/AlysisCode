"""Private, UNWIRED filesystem/lock primitives for the P4 design.

POSIX descriptor-relative operations only. No installer, reader, transaction,
recovery, migration or trust path uses these helpers. Snapshot bytes are sealed
in memory; this is not an atomic multi-file snapshot or crash-durability claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
import unicodedata
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MAX_LOCK_SECONDS = 5.0
POLL_SECONDS = 0.05
PUBLICATION_SECONDS = 30.0
MAX_COMPONENTS = 256
MAX_RESOURCES = 1024
MAX_CONFIG_BYTES = 4 * 1024**2
MAX_JOURNAL_BYTES = 1024**2
MAX_RECOVERY_BYTES = 256 * 1024**2
_REASONS = frozenset(
    {
        "busy",
        "cancelled",
        "stale",
        "unsafe_path",
        "unsupported",
        "resource_limit",
        "unavailable",
        "recovery_required",
    }
)


class MutationError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason if reason in _REASONS else "unavailable"
        super().__init__(f"Plugin mutation refused: {self.reason}.")


def _supported() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.scandir in os.supports_fd
    )


def _parts(parts: tuple[str, ...], *, empty: bool = False) -> tuple[str, ...]:
    if (
        type(parts) is not tuple
        or (not parts and not empty)
        or any(
            type(s) is not str or s in {"", ".", ".."} or "/" in s or "\\" in s or "\x00" in s
            for s in parts
        )
    ):
        raise MutationError("unsafe_path")
    try:
        for part in parts:
            part.encode("utf-8", errors="strict")
    except UnicodeError:
        raise MutationError("unsafe_path") from None
    if len(parts) > 32:
        raise MutationError("resource_limit")
    return parts


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _version(st: os.stat_result) -> tuple[int, ...]:
    return (*_identity(st), st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _regular(st: os.stat_result) -> None:
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise MutationError("unsafe_path")


@dataclass(frozen=True)
class SnapshotLimits:
    # Count the root, every directory and every regular file; root depth is0.
    entries: int = 4096
    depth: int = 32
    file_bytes: int = 8 * 1024**2
    total_bytes: int = 64 * 1024**2

    def __post_init__(self):
        for field, maximum in [
            ("entries", 4096),
            ("depth", 32),
            ("file_bytes", 8 * 1024**2),
            ("total_bytes", 64 * 1024**2),
        ]:
            value = getattr(self, field)
            if (
                type(value) is not int
                or value < (1 if field == "entries" else 0)
                or value > maximum
            ):
                raise MutationError("unsupported")


@dataclass(frozen=True)
class SealedEntry:
    parts: tuple[str, ...]
    identity: tuple[int, int]
    content: bytes | None  # None denotes directory. No mutable buffers escape.


@dataclass(frozen=True)
class SealedTree:
    root_identity: tuple[int, int]
    entries: tuple[SealedEntry, ...]
    total_bytes: int

    @property
    def entry_count(self) -> int:
        return len(self.entries)


class RootHandle:
    """Owned handle to one explicitly selected absolute root, until close.

    Traverses even root ancestors with O_NOFOLLOW. Callers may canonicalize a
    legitimate OS root once; this API never resolves untrusted descendants.
    """

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd = fd
        self.identity = _identity(os.fstat(fd))

    @classmethod
    def open(cls, path: Path) -> RootHandle:
        if not _supported():
            raise MutationError("unsupported")
        path = Path(path)
        if not path.is_absolute():
            raise MutationError("unsafe_path")
        parts = _parts(tuple(path.parts[1:]), empty=True)
        fd = -1
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return cls(path, fd)
        except OSError:
            if fd >= 0:
                os.close(fd)
            raise MutationError("unsafe_path") from None

    def close(self) -> None:
        if self._fd >= 0:
            fd, self._fd = self._fd, -1
            os.close(fd)

    def __enter__(self) -> RootHandle:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _live(self) -> int:
        if self._fd < 0:
            raise MutationError("unavailable")
        return self._fd

    @contextmanager
    def _open(self, parts: tuple[str, ...], *, directory: bool = False) -> Iterator[int]:
        parts = _parts(parts, empty=directory)
        fd = -1
        try:
            fd = os.dup(self._live())
            for i, part in enumerate(parts):
                is_dir = directory or i < len(parts) - 1
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if is_dir else 0),
                    dir_fd=fd,
                )
                os.close(fd)
                fd = child
                if os.fstat(fd).st_dev != self.identity[0]:
                    raise MutationError("unsafe_path")
            yield fd
        except OSError:
            raise MutationError("unsafe_path") from None
        finally:
            if fd >= 0:
                os.close(fd)

    def read_regular(self, parts: tuple[str, ...], *, limit: int = 8 * 1024**2) -> bytes:
        if type(limit) is not int or not 0 <= limit <= 8 * 1024**2:
            raise MutationError("unsupported")
        with self._open(parts) as fd:
            before = os.fstat(fd)
            _regular(before)
            if before.st_size > limit:
                raise MutationError("resource_limit")
            chunks = []
            size = 0
            while True:
                block = os.read(fd, min(65536, limit - size + 1))
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise MutationError("resource_limit")
                chunks.append(block)
            if _version(before) != _version(os.fstat(fd)):
                raise MutationError("stale")
            return b"".join(chunks)

    def snapshot(self, limits: SnapshotLimits = SnapshotLimits()) -> SealedTree:
        if type(limits) is not SnapshotLimits:
            raise MutationError("unsupported")
        entries = []
        total = 0
        discovered = 1  # Include names observed but not yet visited in sibling subtrees.

        def walk(parts):
            nonlocal total, discovered
            if len(parts) > limits.depth:
                raise MutationError("resource_limit")
            with self._open(parts, directory=True) as fd:
                before = os.fstat(fd)
                entries.append(SealedEntry(parts, _identity(before), None))
                if len(entries) > limits.entries:
                    raise MutationError("resource_limit")
                names = []
                canonical = set()
                with os.scandir(fd) as scan:
                    for item in scan:
                        discovered += 1
                        if discovered > limits.entries:
                            raise MutationError("resource_limit")
                        _parts((item.name,))
                        key = unicodedata.normalize("NFC", item.name).casefold()
                        if key in canonical:
                            raise MutationError("unsafe_path")
                        canonical.add(key)
                        names.append(item.name)
                for name in sorted(names):
                    child = (*parts, name)
                    if len(child) > limits.depth:
                        raise MutationError("resource_limit")
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        walk(child)
                    else:
                        _regular(st)
                        if len(entries) >= limits.entries:
                            raise MutationError("resource_limit")
                        content = self.read_regular(
                            child, limit=min(limits.file_bytes, limits.total_bytes - total)
                        )
                        # A same-name replacement between lstat and anchored read
                        # is refused even when both leaves are regular files.
                        if _version(st) != _version(
                            os.stat(name, dir_fd=fd, follow_symlinks=False)
                        ):
                            raise MutationError("stale")
                        total += len(content)
                        entries.append(SealedEntry(child, _identity(st), content))
                if _version(before) != _version(os.fstat(fd)):
                    raise MutationError("stale")

        try:
            walk(())
        except OSError:
            raise MutationError("unsafe_path") from None
        return SealedTree(self.identity, tuple(entries), total)


@dataclass(frozen=True, order=True)
class LockKey:
    # 0=user coordinator;1=physical project;2=resource. Full set, no upgrades.
    rank: int
    root_identity: tuple[int, int]
    parts: tuple[str, ...]

    def __post_init__(self):
        if type(self.rank) is not int or self.rank not in (0, 1, 2):
            raise MutationError("unsupported")
        if (
            type(self.root_identity) is not tuple
            or len(self.root_identity) != 2
            or any(type(v) is not int or v < 0 for v in self.root_identity)
        ):
            raise MutationError("unsupported")
        _parts(self.parts, empty=self.rank != 2)


def lock_filename(key: LockKey) -> str:
    raw = json.dumps([key.rank, key.root_identity, key.parts], separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest() + ".lock"


@dataclass(frozen=True)
class HeldLocks:
    keys: tuple[LockKey, ...]


_mutex_guard = threading.Lock()
_mutexes: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def _mutex(identity):
    with _mutex_guard:
        lock = _mutexes.get(identity)
        if lock is None:
            lock = threading.Lock()
            _mutexes[identity] = lock
        return lock


@contextmanager
def ordered_locks(
    root: RootHandle,
    keys: list[LockKey],
    *,
    timeout: float = MAX_LOCK_SECONDS,
    cancelled: Callable[[], bool] = lambda: False,
) -> Iterator[HeldLocks]:
    """No nesting/reacquisition: declare complete set, release before prompts/I/O.

    Persistent private lock files are never unlinked or stolen. OS flock plus
    in-process mutex; PID files are not ownership. Caller closes root afterwards.
    """
    if not _supported():
        raise MutationError("unsupported")
    import fcntl

    if (
        type(timeout) not in (int, float)
        or not 0 <= timeout <= MAX_LOCK_SECONDS
        or type(keys) is not list
        or not keys
        or len(keys) > MAX_RESOURCES
        or any(type(k) is not LockKey for k in keys)
        or len(set(keys)) != len(keys)
    ):
        raise MutationError("unsupported")
    ordered = tuple(sorted(keys))
    deadline = time.monotonic() + timeout
    held = []

    def check():
        if cancelled():
            raise MutationError("cancelled")
        if time.monotonic() >= deadline:
            raise MutationError("busy")

    def pause():
        check()
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))

    try:
        for key in ordered:
            check()
            name = lock_filename(key)
            mutex = _mutex((root.identity, name))
            while not mutex.acquire(blocking=False):
                pause()
            fd = -1
            try:
                fd = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=root._live(),
                )
                st = os.fstat(fd)
                _regular(st)
                if st.st_mode & 0o077:
                    raise MutationError("unsafe_path")
                while True:
                    check()
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pause()
                if _identity(st) != _identity(
                    os.stat(name, dir_fd=root._live(), follow_symlinks=False)
                ):
                    raise MutationError("stale")
                held.append((fd, mutex))
                fd = -1
            except BaseException:
                if fd >= 0:
                    os.close(fd)
                mutex.release()
                raise
        check()
        yield HeldLocks(ordered)
    except OSError:
        raise MutationError("unsafe_path") from None
    finally:
        for fd, mutex in reversed(held):
            try:
                os.close(fd)  # closes OS lock even if cleanup is interrupted
            finally:
                mutex.release()
