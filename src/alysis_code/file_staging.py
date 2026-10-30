"""One named-file copy through the ordinary admitted shell command, never a grant.

No search, external mutation, overwrite, subprocess, or permission fallback.
POSIX descriptor-relative operations pin directories and the one source opened
after command admission. Publication uses a new exclusive file in the actual execution workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
COPY_SECONDS = 15
CHUNK_BYTES = 64 * 1024


class StagingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def capability_status() -> dict:
    return {
        "state": "ready" if os.name == "posix" else "unavailable",
        "command": shlex.join([sys.executable, "-m", "alysis_code.file_staging"]),
        "max_bytes": MAX_BYTES,
        "guidance": "Only the exact user-named file: --source <absolute-or-tilde-path> --output <new-workspace-filename>. "
        "Use shell_run under existing permissions. No shell/import in readonly; already-provided attachment text remains usable. "
        "No overwrite, source symlinks, traversal, external changes or parent-directory grant. Stop on denial; do not retry by another technique.",
    }


def _identity(info) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _same_file(left, right) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


@contextmanager
def _open_regular_source(source: Path, *, max_bytes: int = MAX_BYTES):
    """Open each component without following symlinks; retain the chain to recheck.

    The approved command establishes identity when it runs, not before an approval
    dialog. All reads use this one descriptor; no post-check reopen by pathname.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise StagingError(
            "unsupported_platform", "Identity-checked file staging is unavailable on this platform."
        )
    if not source.is_absolute() or ".." in source.parts or source.name in {"", "."}:
        raise StagingError(
            "invalid_source", "Name one exact absolute source file without traversal."
        )
    with ExitStack() as stack:

        def opened(name, flags, parent=None):
            fd = os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        try:
            parent = opened(source.anchor, os.O_RDONLY | os.O_DIRECTORY)
            chain = []
            for part in source.parts[1:-1]:
                child = opened(part, os.O_RDONLY | os.O_DIRECTORY, parent)
                chain.append((parent, part, child))
                parent = child
            fd = opened(source.name, os.O_RDONLY | os.O_NONBLOCK, parent)
        except PermissionError:
            raise
        except OSError as exc:
            raise StagingError(
                "invalid_source", "Source must be an existing regular file without symbolic links."
            ) from exc
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise StagingError("invalid_source", "Source must be a regular file.")
        if before.st_size > max_bytes:
            raise StagingError("size_limit", "Source exceeds the 50 MiB byte limit.")

        def verify():
            try:
                for directory, name, child in chain:
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISDIR(current.st_mode) or not _same_file(
                        current, os.fstat(child)
                    ):
                        raise StagingError(
                            "stale_source",
                            "Source directory identity changed; no verified copy was produced.",
                        )
                current = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
                if _identity(current) != _identity(before) or _identity(os.fstat(fd)) != _identity(
                    before
                ):
                    raise StagingError(
                        "stale_source",
                        "Source changed while reading; no verified copy was produced.",
                    )
            except OSError as exc:
                raise StagingError(
                    "stale_source", "Source identity is no longer available."
                ) from exc

        verify()
        yield fd, before, verify


def _check_deadline(started: float) -> None:
    if time.monotonic() - started > COPY_SECONDS:
        raise StagingError(
            "timeout", "File staging deadline exceeded; no verified copy was produced."
        )


def _verify_staged_bytes(fd: int, size: int, sha256: str, started: float):
    """Hash the retained inode, bounded by the expected copy size, with stable metadata."""
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
        raise StagingError("stale_destination", "Staged file identity or size changed.")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        _check_deadline(started)
        chunk = os.read(fd, min(CHUNK_BYTES, size - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > size:
            raise StagingError("stale_destination", "Staged bytes changed during validation.")
        digest.update(chunk)
    after = os.fstat(fd)
    if total != size or digest.hexdigest() != sha256 or _identity(before) != _identity(after):
        raise StagingError(
            "stale_destination", "Staged bytes changed; no verified copy was produced."
        )
    return after


def _verify_entry(root_fd: int, name: str, expected) -> None:
    try:
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise StagingError(
            "stale_destination", "Staged file entry is no longer available."
        ) from exc
    if _identity(current) != _identity(expected):
        raise StagingError(
            "stale_destination", "Staged file entry changed; no verified copy was produced."
        )


def _retain_cleanup_entry(root_fd: int, name: str) -> dict:
    # An observation cannot authorize a later pathname unlink: the entry may be
    # replaced immediately afterwards. Retain it without unlink/rename/truncate.
    # This diagnostic is explicitly momentary, never an ownership assertion.
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        observed = "present"
    except FileNotFoundError:
        observed = "absent"
    except OSError:
        observed = "unavailable"
    return {"path": name, "observed": observed}


def stage_file(*, source: str, workspace: Path | None = None, output: str) -> dict:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise StagingError(
            "unsupported_platform", "Identity-checked file staging is unavailable on this platform."
        )
    # --workspace is only a compatibility assertion, never destination authority.
    # Bind to the admitted command's actual cwd before creating any file.
    root = Path.cwd().resolve(strict=True)
    if workspace is not None and workspace.resolve(strict=True) != root:
        raise StagingError("invalid_destination", "Staging cannot rebind the execution workspace.")
    if source.startswith("~") and not source.startswith("~/"):
        raise StagingError("invalid_source", "Only the current user's ~/ source form is supported.")
    source_path = Path(source).expanduser()
    if Path(output).name != output or output in {"", ".", ".."} or "/" in output or "\\" in output:
        raise StagingError(
            "invalid_destination", "Choose one new filename directly inside the workspace."
        )
    started = time.monotonic()
    with ExitStack() as stack:
        # Opening '.' pins the real execution directory, even if the path is
        # replaced between getcwd and open. verify_root checks the path binding.
        root_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, root_fd)
        root_identity = os.fstat(root_fd)

        def verify_root():
            try:
                current = root.stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or not _same_file(current, root_identity)
                    or root.resolve(strict=True) != root
                ):
                    raise StagingError(
                        "stale_destination", "Workspace identity changed; staging was refused."
                    )
            except OSError as exc:
                raise StagingError(
                    "stale_destination", "Workspace identity is no longer available."
                ) from exc

        verify_root()
        with _open_regular_source(source_path) as (source_fd, before, verify_source):
            try:
                os.stat(output, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StagingError(
                    "destination_conflict", "Destination exists; choose a new filename."
                )
            verify_root()
            try:
                # One new output, no intermediate pathname or duplicate. All
                # bytes come from the single validated source descriptor.
                destination_fd = os.open(
                    output,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError as exc:
                raise StagingError(
                    "destination_conflict", "Destination appeared; nothing was overwritten."
                ) from exc
            stack.callback(os.close, destination_fd)
            total = 0
            digest = hashlib.sha256()
            try:
                _verify_entry(root_fd, output, os.fstat(destination_fd))
                verify_root()
                while True:
                    _check_deadline(started)
                    chunk = os.read(source_fd, min(CHUNK_BYTES, MAX_BYTES - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise StagingError(
                            "size_limit", "Source exceeded the 50 MiB limit during copying."
                        )
                    digest.update(chunk)
                    remaining = memoryview(chunk)
                    while remaining:
                        _check_deadline(started)
                        written = os.write(destination_fd, remaining)
                        if written <= 0:
                            raise OSError("File staging write did not progress.")
                        remaining = remaining[written:]
                verify_source()
                if total != before.st_size:
                    raise StagingError(
                        "stale_source", "Source size changed; no verified copy was produced."
                    )
                os.fsync(destination_fd)
                staged = _verify_staged_bytes(destination_fd, total, digest.hexdigest(), started)
                verify_source()
                verify_root()
                _verify_entry(root_fd, output, staged)
                verify_root()
                return {
                    "status": "staged",
                    "source": source_path.name,
                    "path": output,
                    "bytes": total,
                    "sha256": digest.hexdigest(),
                    "source_identity": {
                        "device": before.st_dev,
                        "inode": before.st_ino,
                        "size": before.st_size,
                        "mtime_ns": before.st_mtime_ns,
                    },
                    "basis": "saved file copied through one validated source descriptor; unsaved app state is unknown",
                }
            except (ValueError, OSError) as exc:
                # Even a known created output may have been replaced since our
                # last observation. Report it without mutating the pathname.
                exc.cleanup = {
                    "status": "retained_unverified",
                    "entries": [_retain_cleanup_entry(root_fd, output)],
                    "workspace_at_start": str(root),
                    "message": (
                        "No cleanup deletion was attempted. Listed entries are unverified and may remain "
                        "in the original execution directory, whose path may have changed. Observations "
                        "are momentary, not ownership evidence. Do not use, overwrite or delete these "
                        "entries automatically."
                    ),
                }
                raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage one exactly named file under existing shell permissions; no new grant."
    )
    parser.add_argument(
        "--workspace",
        help="Optional assertion of the execution workspace; cannot rebind the destination.",
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = stage_file(
            source=args.source,
            workspace=Path(args.workspace) if args.workspace is not None else None,
            output=args.output,
        )
    except (ValueError, OSError) as exc:
        code = (
            "permission_denied"
            if isinstance(exc, PermissionError)
            else getattr(exc, "code", "file_staging_failed")
        )
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": code,
                    "error": str(exc),
                    "retryable": False,
                    **({"cleanup": exc.cleanup} if hasattr(exc, "cleanup") else {}),
                }
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
