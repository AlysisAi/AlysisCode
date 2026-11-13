"""Private durable file workspaces, leased to one executor at a time.

The workspace contains untrusted user files only. The lease and recovery marker
live outside its mount, so applications cannot claim a clean shutdown. This is
local filesystem persistence, not a VM/process checkpoint or a disk quota.

POSIX checks ownership and 0700/0600 modes through directory descriptors and
locks with flock. Windows has neither: the store lives in the user's private
profile, links and junctions (reparse points) are refused instead, and the
lease is locked with msvcrt.locking (released when the handle closes).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class WorkspaceError(RuntimeError):
    pass


def sync_directory(path: Path) -> None:
    """Make a rename or new entry in ``path`` durable.

    Windows commits directory entries with the operation itself and cannot open a
    directory as a file, so there is nothing further to flush there.
    """
    if WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _plain(info, kind) -> bool:
    """Windows: the entry is of ``kind`` and not a symbolic link or junction."""
    return kind(info.st_mode) and not getattr(info, "st_file_attributes", 0) & _REPARSE_POINT


def _directory(path: Path) -> int | None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.resolve() != path.absolute():
        raise WorkspaceError("Workspace storage must not traverse symbolic links.")
    if WINDOWS:
        if not _plain(os.lstat(path), stat.S_ISDIR):
            raise WorkspaceError("Workspace storage must be a plain private directory.")
        return None
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise WorkspaceError("Workspace storage must be private and owned.")
    return fd


def _close(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)


def _open_lease(entry: Path, entry_fd: int | None) -> int:
    """Open and exclusively lock this workspace's lease file."""
    if WINDOWS:
        import msvcrt

        lease = entry / "lease"
        if os.path.lexists(lease) and not _plain(os.lstat(lease), stat.S_ISREG):
            raise WorkspaceError("Workspace lease is not private and owned.")
        fd = os.open(lease, os.O_RDWR | os.O_CREAT | os.O_BINARY | os.O_NOINHERIT, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspaceError("Workspace lease is not private and owned.")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise WorkspaceError("This bot's saved workspace is already in use.") from None
        except BaseException:
            os.close(fd)
            raise
        return fd
    import fcntl

    fd = os.open(
        "lease",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=entry_fd,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise WorkspaceError("Workspace lease is not private and owned.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkspaceError("This bot's saved workspace is already in use.") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _private_staging(info) -> bool:
    if WINDOWS:
        return _plain(info, stat.S_ISDIR)
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


class WorkspaceLease:
    def __init__(self, root: Path, owner: str, bot_id: str):
        if not owner or not bot_id:
            raise WorkspaceError("Workspace ownership is required.")
        self.workspace_id = hashlib.sha256(
            json.dumps([owner, bot_id], ensure_ascii=False).encode()
        ).hexdigest()
        self._entry = root / self.workspace_id
        self._fd = None
        self._entry_fd = _directory(self._entry)
        try:
            self._fd = _open_lease(self._entry, self._entry_fd)
            os.lseek(self._fd, 0, os.SEEK_SET)
            previous = os.read(self._fd, 32)
            if previous not in (b"", b"active", b"closed"):
                raise WorkspaceError("Workspace recovery state is invalid.")
            self.recovered = previous == b"active"
            self.existed = (self._entry / "files").exists()
            _close(_directory(self._entry / "files"))
            for staging in self._entry.glob(".alysis-transfer-*"):
                if not _private_staging(staging.lstat()):
                    raise WorkspaceError("Interrupted transfer staging is not private and owned.")
                shutil.rmtree(staging)
            self.name = str(self._entry / "files")
            self._mark(b"active")
            sync_directory(self._entry)
        except BaseException:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            _close(self._entry_fd)
            raise

    def _mark(self, value: bytes):
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, value)
        os.ftruncate(self._fd, len(value))
        os.fsync(self._fd)

    def cleanup(self, *, clean=True):
        """Release only after the executor and all its writers have stopped."""
        if self._fd is not None:
            self._mark(b"closed" if clean else b"active")
            os.close(self._fd)
            self._fd = None
            _close(self._entry_fd)


class WorkspaceStore:
    def __init__(self, root: Path):
        self.root = root.absolute()
        _close(_directory(self.root))

    def acquire(self, owner: str, bot_id: str) -> WorkspaceLease:
        return WorkspaceLease(self.root, owner, bot_id)
