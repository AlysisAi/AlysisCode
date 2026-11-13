"""Bounded file-only workspace snapshots for a trusted, quiescent controller.

Callers fence new human/agent actions and exclusively control the archive and
destination parents. Existing application writes are rejected by two stable
manifest passes; applications need not close for a human handback. Targets must be new files; restore replaces an existing
empty private directory atomically. No processes or browser profiles are added
by this module: only the explicitly supplied workspace tree is copied.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 4096
# Bound descriptor nesting and make partial-tree cleanup safe under stdlib's
# recursive rmtree implementation, even for a hostile chain of directories.
MAX_DEPTH = 64
CHUNK_BYTES = 64 * 1024


class WorkspaceArchiveError(RuntimeError):
    pass


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _directory(path):
    """Open every path component without following symlinks."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise WorkspaceArchiveError("Workspace paths must not traverse parent directories.")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in path.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _name(value):
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or len(value.split("/")) > MAX_DEPTH
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or len(value.encode("utf-8")) > 4096
    ):
        raise WorkspaceArchiveError("Archive paths must be bounded relative UTF-8 paths.")
    return value


def _walk(directory, prefix=""):
    """Descriptors stay open until the consumer finishes each entry."""
    before = os.fstat(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            name = _name(prefix + entry.name)
            observed = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            if not (stat.S_ISDIR(observed.st_mode) or stat.S_ISREG(observed.st_mode)):
                raise WorkspaceArchiveError("Workspace contains an unsupported file type.")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if stat.S_ISDIR(observed.st_mode):
                flags |= os.O_DIRECTORY
            fd = os.open(entry.name, flags, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if _stamp(info) != _stamp(observed):
                    raise WorkspaceArchiveError("Workspace changed while opening an entry.")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise WorkspaceArchiveError("Workspace hard links are not supported.")
                yield name, fd, info
                if stat.S_ISDIR(info.st_mode):
                    yield from _walk(fd, name + "/")
                if _stamp(os.fstat(fd)) != _stamp(info) or _stamp(
                    os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                ) != _stamp(info):
                    raise WorkspaceArchiveError("Workspace changed during snapshot.")
            finally:
                os.close(fd)
    if _stamp(os.fstat(directory)) != _stamp(before):
        raise WorkspaceArchiveError("Workspace directory changed during snapshot.")


class _BoundedWriter:
    def __init__(self, stream):
        self.stream = stream

    def tell(self):
        return self.stream.tell()

    def write(self, data):
        if self.tell() + len(data) > MAX_ARCHIVE_BYTES:
            raise WorkspaceArchiveError("Workspace archive exceeds 50 MiB including overhead.")
        return self.stream.write(data)


def pack_workspace(workspace: Path, target: Path) -> dict:
    """Return archive bytes, SHA-256 and regular-file count; never overwrite."""
    target = Path(target).absolute()
    stage = ".alysis-snapshot-" + uuid4().hex
    try:
        with _directory(workspace) as root, _directory(target.parent) as parent:
            root_stamp = _stamp(os.fstat(root))
            # Staging inside the source would snapshot its own growing archive.
            current = Path(target.parent)
            while True:
                with _directory(current) as directory:
                    if _stamp(os.fstat(directory))[:2] == root_stamp[:2]:
                        raise WorkspaceArchiveError("Archive target must be outside its workspace.")
                if current == current.parent:
                    break
                current = current.parent
            fd = os.open(
                stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            try:
                with os.fdopen(fd, "w+b") as output:
                    manifest, files = {}, 0
                    with (
                        closing(_walk(root)) as entries,
                        tarfile.open(
                            fileobj=_BoundedWriter(output), mode="w", format=tarfile.PAX_FORMAT
                        ) as tar,
                    ):
                        for name, entry_fd, info in entries:
                            if len(manifest) >= MAX_ENTRIES:
                                raise WorkspaceArchiveError("Workspace exceeds 4096 entries.")
                            manifest[name] = _stamp(info)
                            member = tarfile.TarInfo(name)
                            member.mode = 0o700
                            if stat.S_ISDIR(info.st_mode):
                                member.type = tarfile.DIRTYPE
                                tar.addfile(member)
                            else:
                                member.size = info.st_size
                                member.mode = 0o700 if info.st_mode & 0o100 else 0o600
                                # Reject oversized files before reading their content.
                                if member.size > MAX_ARCHIVE_BYTES:
                                    raise WorkspaceArchiveError(
                                        "Workspace file exceeds archive allowance."
                                    )
                                with os.fdopen(os.dup(entry_fd), "rb") as payload:
                                    tar.addfile(member, payload)
                                files += 1
                    observed = {}
                    with closing(_walk(root)) as entries:
                        for name, _, info in entries:
                            if name not in manifest or _stamp(info) != manifest[name]:
                                raise WorkspaceArchiveError(
                                    "Workspace changed before snapshot publication."
                                )
                            observed[name] = _stamp(info)
                    if observed != manifest or _stamp(os.fstat(root)) != root_stamp:
                        raise WorkspaceArchiveError(
                            "Workspace changed before snapshot publication."
                        )
                    output.flush()
                    os.fsync(output.fileno())
                    size = output.tell()
                    output.seek(0)
                    digest = hashlib.sha256()
                    while chunk := output.read(CHUNK_BYTES):
                        digest.update(chunk)
                    os.link(
                        stage,
                        target.name,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                    return {"bytes": size, "sha256": digest.hexdigest(), "files": files}
            finally:
                os.unlink(stage, dir_fd=parent)
    except WorkspaceArchiveError:
        raise
    except (OSError, ValueError, tarfile.TarError, RecursionError, OverflowError, EOFError):
        raise WorkspaceArchiveError(
            "Workspace snapshot failed; no new archive was published."
        ) from None


def _validate(tar, source, archive_size):
    members, paths, explicit = [], {}, set()
    payload_bytes = 0
    for member in tar:
        name = _name(member.name)
        if (
            name in explicit
            or not (member.isreg() or member.isdir())
            or member.sparse is not None
            or member.size < 0
            or (member.isdir() and member.size != 0)
        ):
            raise WorkspaceArchiveError("Archive has duplicate paths or unsupported entries.")
        explicit.add(name)
        kind = "directory" if member.isdir() else "file"
        if name in paths and (kind != "directory" or paths[name] != "directory"):
            raise WorkspaceArchiveError("Archive paths collide.")
        paths[name] = kind
        parts = name.split("/")
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            if paths.get(ancestor, "directory") != "directory":
                raise WorkspaceArchiveError("Archive file conflicts with a directory.")
            paths[ancestor] = "directory"
        if len(paths) > MAX_ENTRIES:
            raise WorkspaceArchiveError(
                "Archive exceeds 4096 entries including parent directories."
            )
        payload_bytes += member.size
        if payload_bytes > MAX_ARCHIVE_BYTES or member.offset_data + member.size > archive_size:
            raise WorkspaceArchiveError("Archive payload exceeds its bounded contents.")
        members.append(member)
    # tarfile accepts missing end records and ignores trailing data by default.
    # Require the normal two zero blocks and reject hidden concatenated archives.
    source.seek(tar.offset)
    tail = 0
    while data := source.read(CHUNK_BYTES):
        tail += len(data)
        if any(data):
            raise WorkspaceArchiveError("Archive has data after its end record.")
    if tail < 1024 or archive_size % 512:
        raise WorkspaceArchiveError("Archive end record is incomplete.")
    return members, payload_bytes


@contextmanager
def _parents(directory, parts, seen):
    fd = os.dup(directory)
    try:
        for index, name in enumerate(parts):
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            key, path = (info.st_dev, info.st_ino), "/".join(parts[: index + 1])
            if key in seen and seen[key] != path:
                raise WorkspaceArchiveError("Archive directory names collide on this filesystem.")
            seen[key] = path
        yield fd
    finally:
        os.close(fd)


def restore_workspace(archive: Path, destination: Path) -> dict:
    """Validate, stage and atomically replace an empty owned 0700 directory."""
    archive, destination = Path(archive).absolute(), Path(destination).absolute()
    stage = ".alysis-restore-" + uuid4().hex
    created = False
    try:
        with _directory(archive.parent) as archive_parent, _directory(destination.parent) as parent:
            with _directory(destination) as dest:
                original = os.fstat(dest)
                if (
                    original.st_uid != os.geteuid()
                    or stat.S_IMODE(original.st_mode) != 0o700
                    or os.listdir(dest)
                ):
                    raise WorkspaceArchiveError(
                        "Restore requires an empty private owned directory."
                    )
                fd = os.open(
                    archive.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=archive_parent
                )
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or before.st_size > MAX_ARCHIVE_BYTES
                    ):
                        raise WorkspaceArchiveError(
                            "Restore requires a regular archive of at most 50 MiB."
                        )
                    with tarfile.open(fileobj=source, mode="r:") as tar:
                        members, payload_bytes = _validate(tar, source, before.st_size)
                        if _stamp(os.fstat(source.fileno())) != _stamp(before):
                            raise WorkspaceArchiveError("Archive changed during validation.")
                        os.mkdir(stage, 0o700, dir_fd=parent)
                        created = True
                        stage_fd = os.open(
                            stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                        )
                        try:
                            files, directories = 0, {}
                            for member in members:
                                parts = member.name.split("/")
                                with _parents(
                                    stage_fd, parts if member.isdir() else parts[:-1], directories
                                ) as directory:
                                    if member.isdir():
                                        continue
                                    fd = os.open(
                                        parts[-1],
                                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600,
                                        dir_fd=directory,
                                    )
                                    with (
                                        os.fdopen(fd, "wb") as output,
                                        tar.extractfile(member) as payload,
                                    ):
                                        remaining = member.size
                                        while remaining:
                                            chunk = payload.read(min(CHUNK_BYTES, remaining))
                                            if not chunk:
                                                raise WorkspaceArchiveError(
                                                    "Archive payload is truncated."
                                                )
                                            output.write(chunk)
                                            remaining -= len(chunk)
                                        output.flush()
                                        os.fchmod(
                                            output.fileno(), 0o700 if member.mode & 0o100 else 0o600
                                        )
                                        os.fsync(output.fileno())
                                    files += 1
                            if (
                                _stamp(os.fstat(source.fileno())) != _stamp(before)
                                or _stamp(os.fstat(dest)) != _stamp(original)
                                or _stamp(
                                    os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
                                )
                                != _stamp(original)
                                or os.listdir(dest)
                            ):
                                raise WorkspaceArchiveError(
                                    "Archive or destination changed before publication."
                                )
                            os.rename(stage, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
                            created = False
                            return {"files": files, "bytes": payload_bytes}
                        finally:
                            os.close(stage_fd)
            # No writes have touched the caller's destination on any failure.
    except WorkspaceArchiveError:
        raise
    except (OSError, ValueError, tarfile.TarError, RecursionError, OverflowError, EOFError):
        raise WorkspaceArchiveError(
            "Workspace restore failed; destination was not published."
        ) from None
    finally:
        if created:
            with _directory(destination.parent) as parent:
                shutil.rmtree(stage, dir_fd=parent)
