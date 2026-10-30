from __future__ import annotations

import codecs
import ctypes
import errno
import hashlib
import io
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, BinaryIO

from ..file_classification import derived_artifact_reason
from ..host_file_handles import (
    admit_host_path,
    chmod_host_fd,
    guard_host_mutation,
    guard_host_resolved_read,
    mkdir_host_path,
    open_host_file_read,
    rename_host_file_no_replace,
)
from ..project_folders import containing_root
from ..runtime_artifacts import RUNTIME_ARTIFACT_DIR_NAMES
from ..sensitive_paths import sensitive_path_category


class FsError(RuntimeError):
    pass


class StaleFileError(FsError):
    """A guarded mutation no longer matches the file state it was prepared from."""

    code = "stale_file"

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"stale_file: {path} changed after the operation was prepared; "
            "the file was not modified"
        )


_DEFAULT_IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
    ".idea",
    ".vscode",
} | set(RUNTIME_ARTIFACT_DIR_NAMES)
_DEFAULT_READ_WINDOW_MAX_LINES = 200
_READ_CHUNK_BYTES = 8_192
_PHYSICAL_NEWLINES = re.compile(rb"\r\n|\r|\n")
# Derived artifacts smaller than this are returned whole; the stub would not
# save anything meaningful.
_DERIVED_ARTIFACT_STUB_MIN_BYTES = 2_048
_DERIVED_ARTIFACT_HEAD_BYTES = 1_000
_DERIVED_ARTIFACT_NOTE = (
    "Content withheld by default: this is a machine-generated artifact whose "
    "full text rarely informs a task relative to its size. The head sample "
    "above is provided for orientation. If this artifact's contents are "
    "genuinely the subject of the task, re-call fs_read with "
    "allow_derived=true, or set start_line on fs_read for a bounded range."
)
_FS_EDIT_OPERATIONS = {
    "replace_exact",
    "insert_before_exact",
    "insert_after_exact",
    "replace_lines",
    "insert_before_line",
    "insert_after_line",
    "append",
    "prepend",
}
_FS_EDIT_OPERATION_ALIASES = {
    "replace": "replace_exact",
}
_DEFAULT_FS_READ_MAX_BYTES = 12_000
_DEFAULT_FS_LIST_MAX_RESULTS = 150
_GIT_PROBE_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class SensitivePathClassification:
    sensitive: bool
    category: str | None = None


def classify_sensitive_path(path: str | os.PathLike[str]) -> SensitivePathClassification:
    """Classify credential-bearing paths without opening or inspecting their content."""
    category = sensitive_path_category(path)
    return SensitivePathClassification(category is not None, category)


@dataclass(frozen=True, slots=True)
class FilePrecondition:
    path: str
    exists: bool
    content_sha256: str | None
    identity_sha256: str | None
    mode: int | None


@dataclass(frozen=True, slots=True)
class PreparedFsWrite:
    root_obj: Path
    path: str
    path_obj: Path
    content: str
    precondition: FilePrecondition


@dataclass(frozen=True)
class PreparedFsEdit:
    root_obj: Path
    path: str
    path_obj: Path
    original_content: str
    updated_content: str
    applied_edits: int
    precondition: FilePrecondition

    @property
    def original_content_sha256(self) -> str:
        return str(self.precondition.content_sha256 or "")

    @property
    def original_file_identity_sha256(self) -> str:
        return str(self.precondition.identity_sha256 or "")


def _content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity_sha256(path_stat: os.stat_result) -> str:
    identity = ":".join(
        str(value)
        for value in (
            getattr(path_stat, "st_dev", 0),
            getattr(path_stat, "st_ino", 0),
            path_stat.st_size,
            getattr(path_stat, "st_mtime_ns", int(path_stat.st_mtime * 1_000_000_000)),
            getattr(path_stat, "st_ctime_ns", int(path_stat.st_ctime * 1_000_000_000)),
        )
    )
    return hashlib.sha256(identity.encode("ascii", errors="strict")).hexdigest()


def _snapshot_existing_file(path_obj: Path, user_path: str) -> tuple[bytes, FilePrecondition]:
    try:
        with open_host_file_read(path_obj) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise FsError(f"Is not a regular file: {user_path}")
            data = handle.read()
            after = os.fstat(handle.fileno())
    except FileNotFoundError as exc:
        raise FsError(f"Not found: {user_path}") from exc
    if _identity_sha256(before) != _identity_sha256(after):
        raise StaleFileError(user_path)
    return data, FilePrecondition(
        path=user_path,
        exists=True,
        content_sha256=_content_sha256(data),
        identity_sha256=_identity_sha256(after),
        mode=stat.S_IMODE(after.st_mode),
    )


def capture_file_precondition(*, root: Path, path: str) -> FilePrecondition:
    path_obj = _resolve_under_root(root, path)
    try:
        data, precondition = _snapshot_existing_file(path_obj, path)
    except FsError:
        if path_obj.exists():
            raise
        return FilePrecondition(
            path=path,
            exists=False,
            content_sha256=None,
            identity_sha256=None,
            mode=None,
        )
    _ = data
    return precondition


def assert_file_precondition(*, root: Path, precondition: FilePrecondition) -> None:
    path_obj = _resolve_under_root(root, precondition.path)
    if not precondition.exists:
        if path_obj.exists():
            raise StaleFileError(precondition.path)
        return
    try:
        _data, current = _snapshot_existing_file(path_obj, precondition.path)
    except FsError as exc:
        if isinstance(exc, StaleFileError):
            raise
        raise StaleFileError(precondition.path) from exc
    if (
        current.content_sha256 != precondition.content_sha256
        or current.identity_sha256 != precondition.identity_sha256
    ):
        raise StaleFileError(precondition.path)


def _resolve_under_root(root: Path, user_path: str) -> Path:
    admit_host_path(root / user_path)
    root_abs = root.resolve()
    p = (root_abs / user_path).resolve()
    try:
        containing_root(root_abs, p)
    except ValueError as e:
        raise FsError(f"Path escapes root: {user_path}") from e
    guard_host_resolved_read(p)
    return p


def _physical_line_chunks(handle: BinaryIO) -> Iterator[tuple[bytes, bool]]:
    """Yield bounded byte fragments and physical line endings, without joining long lines."""
    pending_cr = b""
    in_line = False
    while chunk := handle.read(_READ_CHUNK_BYTES):
        data = pending_cr + chunk
        pending_cr = b""
        if data.endswith(b"\r"):
            # Keep CRLF atomic even when it crosses a read boundary.
            pending_cr, data = b"\r", data[:-1]
        offset = 0
        for boundary in _PHYSICAL_NEWLINES.finditer(data):
            yield data[offset : boundary.end()], True
            in_line = False
            offset = boundary.end()
        if offset < len(data):
            yield data[offset:], False
            in_line = True
    if pending_cr:
        yield pending_cr, True
    elif in_line:
        # Finalize the UTF-8 decoder and the unterminated last line at EOF.
        yield b"", True


def _head_line_metadata(text: str) -> tuple[int, bool]:
    count = sum(1 for _line in io.StringIO(text, newline=""))
    # A trailing CR may be the first half of CRLF; repeat that line safely.
    return count, not text.endswith("\n")


def _truncated_line_ranges(
    *,
    start_line: int,
    end_line: int,
    total_lines: int | None,
    next_max_lines: int,
    line_clipped: bool = False,
    requested_end_line: int | None = None,
) -> dict[str, Any]:
    # A clipped line has not been read in full. Keep it in the continuation
    # instead of silently skipping its unseen suffix.
    next_start = max(start_line, end_line if line_clipped else end_line + 1)
    next_range = None
    last_requested = next_start + max(1, next_max_lines) - 1
    if total_lines is not None:
        last_requested = min(last_requested, total_lines)
    if requested_end_line is not None:
        last_requested = min(last_requested, requested_end_line)
    if next_start <= last_requested:
        next_range = {
            "start_line": next_start,
            "end_line": min(last_requested, next_start + max(1, next_max_lines) - 1),
        }
    return {
        "total_lines": total_lines,
        "returned_range": {"start_line": start_line, "end_line": end_line},
        "next_range": next_range,
    }


def fs_read_uses_line_range(arguments: dict[str, Any]) -> bool:
    """An explicit line option selects the bounded, optionally numbered view."""
    return any(
        arguments.get(key) is not None
        for key in ("start_line", "end_line", "max_lines", "include_line_numbers")
    )


def fs_read(
    *,
    root: Path,
    path: str,
    max_bytes: int = _DEFAULT_FS_READ_MAX_BYTES,
    allow_derived: bool = False,
    start_line: int | None = None,
    end_line: int | None = None,
    max_lines: int | None = None,
    include_line_numbers: bool | None = None,
) -> dict[str, Any]:
    if max_bytes < 1:
        raise FsError(f"Invalid max_bytes: {max_bytes} (must be >= 1)")
    if fs_read_uses_line_range(
        {
            "start_line": start_line,
            "end_line": end_line,
            "max_lines": max_lines,
            "include_line_numbers": include_line_numbers,
        }
    ):
        return _read_line_window(
            root=root,
            path=path,
            start_line=1 if start_line is None else start_line,
            end_line=end_line,
            max_lines=_DEFAULT_READ_WINDOW_MAX_LINES if max_lines is None else max_lines,
            include_line_numbers=True if include_line_numbers is None else include_line_numbers,
            max_bytes=max_bytes,
        )
    p = _resolve_under_root(root, path)
    if not p.exists():
        raise FsError(f"Not found: {path}")
    if p.is_dir():
        raise FsError(f"Is a directory: {path}")

    if not allow_derived:
        derived_reason = derived_artifact_reason(path)
        if derived_reason is not None:
            try:
                size_bytes: int | None = p.stat().st_size
            except OSError:
                size_bytes = None
            if size_bytes is None or size_bytes > _DERIVED_ARTIFACT_STUB_MIN_BYTES:
                # The head sample honors an explicit smaller max_bytes: the
                # caller's ceiling bounds every read shape, stub included.
                with open_host_file_read(p) as fh:
                    head = fh.read(min(_DERIVED_ARTIFACT_HEAD_BYTES, max(0, max_bytes)))
                content = head.decode("utf-8", errors="ignore")
                returned_end_line, line_clipped = _head_line_metadata(content)
                # ``truncated`` is always True for a stub so downstream caches
                # never treat the head sample as the complete file content.
                result = {
                    "path": path,
                    "content": content,
                    "truncated": True,
                    "bytes_read": len(head),
                    "max_bytes": max_bytes,
                    "derived_artifact": True,
                    "derived_artifact_reason": derived_reason,
                    "size_bytes": size_bytes,
                    "note": _DERIVED_ARTIFACT_NOTE,
                }
                if line_clipped:
                    result["line_clipped"] = True
                result.update(
                    _truncated_line_ranges(
                        start_line=1,
                        end_line=returned_end_line,
                        total_lines=None,
                        next_max_lines=_DEFAULT_READ_WINDOW_MAX_LINES,
                        line_clipped=bool(result.get("line_clipped")),
                    )
                )
                return result

    # Read only what we need (+1 byte lookahead for truncation detection).
    with open_host_file_read(p) as fh:
        data = fh.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    # Replacement glyphs and a split multibyte character must not expand the
    # returned UTF-8 text past the advertised byte ceiling.
    encoded_text = text.encode("utf-8")
    truncated = truncated or len(encoded_text) > max_bytes
    text = encoded_text[:max_bytes].decode("utf-8", errors="ignore")
    result = {
        "path": path,
        "content": text,
        "truncated": truncated,
        "bytes_read": len(data),
        "max_bytes": max_bytes,
    }
    if truncated:
        returned_end_line, line_clipped = _head_line_metadata(text)
        if line_clipped:
            result["line_clipped"] = True
            result["note"] = (
                "Byte ceiling cut a line. next_range includes that line; increase "
                "max_bytes if a window read cannot return it completely."
            )
        result.update(
            _truncated_line_ranges(
                start_line=1,
                end_line=returned_end_line,
                total_lines=None,
                next_max_lines=_DEFAULT_READ_WINDOW_MAX_LINES,
                line_clipped=bool(result.get("line_clipped")),
            )
        )
    return result


def _read_line_window(
    *,
    root: Path,
    path: str,
    start_line: int,
    end_line: int | None = None,
    max_lines: int = _DEFAULT_READ_WINDOW_MAX_LINES,
    include_line_numbers: bool = True,
    max_bytes: int,
) -> dict[str, Any]:
    if start_line < 1:
        raise FsError(f"Invalid start_line: {start_line} (must be >= 1)")
    if end_line is not None and end_line < start_line:
        raise FsError(
            f"Invalid line range: end_line ({end_line}) must be >= start_line ({start_line})"
        )
    if max_lines < 1:
        raise FsError(f"Invalid max_lines: {max_lines} (must be >= 1)")
    if max_bytes < 1:
        raise FsError(f"Invalid max_bytes: {max_bytes} (must be >= 1)")

    p = _resolve_under_root(root, path)
    if not p.exists():
        raise FsError(f"Not found: {path}")
    if p.is_dir():
        raise FsError(f"Is a directory: {path}")

    requested_end_line = end_line
    effective_end_line = start_line + max_lines - 1
    if requested_end_line is not None:
        effective_end_line = min(effective_end_line, requested_end_line)

    content_lines: list[str] = []
    actual_end_line = start_line - 1
    total_lines: int | None = None
    lineno = 1
    truncated = False
    bytes_used = 0
    byte_truncated = False
    line_clipped = False
    line_buffer = bytearray()
    decoder = None

    # Locating a later line still scans its prefix, but skipped and minified
    # lines never grow memory beyond a chunk. Selected lines are accumulated
    # only up to the remaining output budget. At most one chunk is read ahead.
    with open_host_file_read(p) as fh:
        for fragment, ends_line in _physical_line_chunks(fh):
            if lineno > effective_end_line:
                truncated = requested_end_line is None or lineno <= requested_end_line
                break
            if lineno < start_line:
                if ends_line:
                    lineno += 1
                continue
            if decoder is None:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                if include_line_numbers:
                    line_buffer.extend(f"{lineno}: ".encode())
            decoded = decoder.decode(fragment, final=ends_line).encode("utf-8")
            remaining = max_bytes - bytes_used
            if len(line_buffer) + len(decoded) > remaining:
                if not content_lines:
                    # Keep a clipped first line visible, but do not claim its
                    # unseen suffix in the read ledger or skip it on resume.
                    line_buffer.extend(decoded[: max(0, remaining - len(line_buffer))])
                    content_lines.append(bytes(line_buffer[:remaining]).decode("utf-8", "ignore"))
                    actual_end_line = lineno
                    line_clipped = True
                byte_truncated = truncated = True
                break
            line_buffer.extend(decoded)
            if ends_line:
                content_lines.append(line_buffer.decode("utf-8"))
                bytes_used += len(line_buffer)
                actual_end_line = lineno
                lineno += 1
                line_buffer.clear()
                decoder = None
        else:
            total_lines = lineno - 1

    if total_lines is not None and total_lines < start_line:
        raise FsError(
            f"Start line {start_line} is beyond end of file ({total_lines} lines): {path}"
        )

    result: dict[str, Any] = {
        "path": path,
        "start_line": start_line,
        "end_line": actual_end_line,
        "total_lines": total_lines,
        "content": "".join(content_lines),
        "truncated": truncated,
    }
    if byte_truncated:
        result["byte_truncated"] = True
        result["max_bytes"] = max_bytes
        result["note"] = (
            "Byte ceiling reached before the requested line range completed. "
            "Request a narrower range, or raise max_bytes if the full range is "
            "genuinely required."
        )
    if line_clipped:
        result["line_clipped"] = True
    if truncated:
        result.update(
            _truncated_line_ranges(
                start_line=start_line,
                end_line=actual_end_line,
                total_lines=total_lines,
                next_max_lines=max_lines,
                line_clipped=line_clipped,
                requested_end_line=requested_end_line,
            )
        )
    return result


def _atomic_replace_text(
    path_obj: Path,
    content: str,
    *,
    root: Path,
    precondition: FilePrecondition,
) -> None:
    """Durably stage text and commit it without clobbering a racing writer.

    ``os.replace`` is atomic, but it is not compare-and-swap: a writer can
    change the destination after our final comparison and before the replace.
    For an existing file we therefore move the destination to a private sibling,
    verify the *displaced* bytes, and install the staged inode with a no-clobber
    publication. A racing version is either restored or left at the public path;
    it is never overwritten by the prepared content.
    """

    guard_host_mutation(path_obj)
    mkdir_host_path(path_obj.parent)
    fd = -1
    temp_path: Path | None = None
    for _attempt in range(20):
        candidate = path_obj.parent / f".{path_obj.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        temp_path = candidate
        break
    if temp_path is None or fd < 0:
        raise FsError(f"Could not create temporary file for: {precondition.path}")

    try:
        if precondition.mode is not None:
            chmod_host_fd(fd, precondition.mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Keep the cheap early comparison for a useful stale error before any
        # namespace mutation.  The displaced-file comparison in
        # ``_commit_staged_regular_file`` closes the check/replace race.
        assert_file_precondition(root=root, precondition=precondition)
        current_path = _resolve_under_root(root, precondition.path)
        if os.path.normcase(os.fspath(current_path)) != os.path.normcase(os.fspath(path_obj)):
            raise StaleFileError(precondition.path)
        _commit_staged_regular_file(
            staged_path=temp_path,
            target_path=path_obj,
            precondition=precondition,
        )
        temp_path = None
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _commit_staged_regular_file(
    *,
    staged_path: Path,
    target_path: Path,
    precondition: FilePrecondition,
) -> None:
    """Install ``staged_path`` iff ``target_path`` still matches ``precondition``."""

    displaced_path = _displace_regular_file_if_matches(
        target_path=target_path,
        precondition=precondition,
    )
    if displaced_path is None:
        try:
            _publish_regular_file_no_replace(staged_path, target_path)
        except FileExistsError as exc:
            raise StaleFileError(precondition.path) from exc
        return

    try:
        _publish_regular_file_no_replace(staged_path, target_path)
    except FileExistsError as exc:
        # Another writer claimed the public name after we moved aside the
        # expected version.  Their version wins; the prepared write fails.
        displaced_path.unlink(missing_ok=True)
        raise StaleFileError(precondition.path) from exc
    except OSError:
        if not _restore_displaced_no_replace(displaced_path, target_path):
            raise FsError(
                f"Unable to install or safely restore {precondition.path}; "
                f"the previous version remains at {displaced_path.name}"
            ) from None
        raise
    displaced_path.unlink()


def _displace_regular_file_if_matches(
    *, target_path: Path, precondition: FilePrecondition
) -> Path | None:
    """Move a matching file aside, restoring it when the displaced bytes are stale."""

    if not precondition.exists:
        if os.path.lexists(target_path):
            raise StaleFileError(precondition.path)
        return None

    displaced_path: Path | None = None
    target_was_displaced = False
    try:
        try:
            if os.name == "nt":
                # A sibling rename through the opened source preserves MXC's
                # directory pins. Never replace or remove a colliding name.
                for _attempt in range(20):
                    candidate = target_path.parent / (
                        f".{target_path.name}.{secrets.token_hex(8)}.displaced"
                    )
                    try:
                        rename_host_file_no_replace(target_path, candidate)
                    except FileExistsError:
                        continue
                    displaced_path = candidate
                    break
                if displaced_path is None:
                    raise FsError(f"Could not displace file safely: {precondition.path}")
            else:
                displaced_path = _reserve_displaced_path(target_path)
                os.replace(target_path, displaced_path)
            target_was_displaced = True
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise StaleFileError(precondition.path) from exc
        if _displaced_file_matches(displaced_path, precondition):
            return displaced_path
        if not _restore_displaced_no_replace(displaced_path, target_path):
            raise FsError(
                f"A concurrent edit was preserved at {precondition.path}; "
                f"the displaced version remains at {displaced_path.name}"
            )
        raise StaleFileError(precondition.path)
    finally:
        if not target_was_displaced and displaced_path is not None:
            displaced_path.unlink(missing_ok=True)


def _reserve_displaced_path(target_path: Path) -> Path:
    for _attempt in range(20):
        candidate = target_path.parent / (f".{target_path.name}.{secrets.token_hex(8)}.displaced")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FsError(f"Could not reserve a displaced-file path for: {target_path.name}")


def _displaced_file_matches(path: Path, precondition: FilePrecondition) -> bool:
    try:
        data, current = _snapshot_existing_file(path, precondition.path)
    except (FsError, OSError):
        return False
    # Rename metadata is not stable across all supported filesystems, so compare
    # the durable semantics that matter: exact bytes, regular-file type, and mode.
    return (
        current.content_sha256 == precondition.content_sha256
        and current.mode == precondition.mode
        and _content_sha256(data) == precondition.content_sha256
    )


def _publish_regular_file_no_replace(source: Path, destination: Path) -> None:
    """Publish without replacement, consuming the staged source on success."""

    if os.name == "nt":
        # Use the opened source's parent, retaining MXC's directory protections.
        rename_host_file_no_replace(source, destination)
    elif sys.platform == "darwin":
        # Seatbelt can refuse cross-directory hardlinks even when both folders
        # permit writes. Use macOS's atomic no-replace rename instead of a
        # link/unlink publication; collisions still preserve both files.
        rename = ctypes.CDLL(None, use_errno=True).renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(destination), 4):  # RENAME_EXCL
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(source), None, str(destination))
    else:
        os.link(source, destination, follow_symlinks=False)
        source.unlink()


def _stage_bytes_sibling(target: Path, data: bytes, *, mode: int | None) -> Path:
    guard_host_mutation(target)
    mkdir_host_path(target.parent)
    fd = -1
    staged: Path | None = None
    for _attempt in range(20):
        candidate = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        staged = candidate
        break
    if staged is None or fd < 0:
        raise FsError(f"Could not create temporary file for: {target.name}")
    try:
        if mode is not None:
            chmod_host_fd(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return staged
    except Exception:
        if fd >= 0:
            os.close(fd)
        staged.unlink(missing_ok=True)
        raise


def _publish_regular_move_no_replace(source: Path, destination: Path, *, mode: int | None) -> None:
    try:
        _publish_regular_file_no_replace(source, destination)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Cross-device moves, and Windows moves between pinned directories,
        # stage a verified byte copy beside the destination before publication.
        with open_host_file_read(source) as stream:
            data = stream.read()
        staged = _stage_bytes_sibling(destination, data, mode=mode)
        try:
            _publish_regular_file_no_replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
    source.unlink()


def _restore_displaced_no_replace(displaced: Path, target: Path) -> bool:
    """Restore a displaced entry only while the public name remains unclaimed."""

    try:
        info = displaced.lstat()
        if stat.S_ISREG(info.st_mode):
            _publish_regular_file_no_replace(displaced, target)
        elif stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(displaced), target)
            displaced.unlink()
        else:
            return False
    except FileExistsError:
        return False
    except OSError:
        return False
    return True


def prepare_fs_write(*, root: Path, path: str, content: str) -> PreparedFsWrite:
    path_obj = _resolve_under_root(root, path)
    if path_obj.exists() and path_obj.is_dir():
        raise FsError(f"Is a directory: {path}")
    return PreparedFsWrite(
        root_obj=root.resolve(),
        path=path,
        path_obj=path_obj,
        content=content,
        precondition=capture_file_precondition(root=root, path=path),
    )


def write_prepared_fs_write(prepared: PreparedFsWrite, *, root: Path) -> dict[str, Any]:
    _atomic_replace_text(
        prepared.path_obj,
        prepared.content,
        root=root,
        precondition=prepared.precondition,
    )
    return {
        "path": prepared.path,
        "bytes": len(prepared.content.encode("utf-8")),
        "created": not prepared.precondition.exists,
    }


def fs_write(*, root: Path, path: str, content: str) -> dict[str, Any]:
    prepared = prepare_fs_write(root=root, path=path, content=content)
    result = write_prepared_fs_write(prepared, root=root)
    # ``created`` distinguishes a brand-new file from an overwrite. Regression
    # attribution uses it to mark a failing test the agent just authored this
    # turn as signal (agent_authored) rather than a regression.
    return result


def fs_mkdir(
    *,
    root: Path,
    path: str,
    parents: bool = True,
    exist_ok: bool = True,
) -> dict[str, Any]:
    p = _resolve_under_root(root, path)
    guard_host_mutation(p)
    if p.exists():
        if not p.is_dir():
            raise FsError(f"Target exists as a file: {path}")
        if not exist_ok:
            raise FsError(f"Directory already exists and exist_ok is false: {path}")
        return {
            "path": path,
            "created": False,
            "already_exists": True,
            "parents": bool(parents),
            "exist_ok": bool(exist_ok),
        }

    try:
        mkdir_host_path(p, parents=bool(parents), exist_ok=bool(exist_ok))
    except FileExistsError as e:
        raise FsError(f"Directory already exists and exist_ok is false: {path}") from e
    except OSError as e:
        raise FsError(str(e)) from e

    return {
        "path": path,
        "created": True,
        "already_exists": False,
        "parents": bool(parents),
        "exist_ok": bool(exist_ok),
    }


def _require_edit_string(edit: dict[str, Any], key: str, *, index: int, op: str) -> str:
    value = edit.get(key)
    if not isinstance(value, str):
        raise FsError(f"Edit {index} ({op}) requires string field: {key}")
    return value


def _require_edit_line_number(edit: dict[str, Any], key: str, *, index: int, op: str) -> int:
    value = edit.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FsError(f"Edit {index} ({op}) requires integer field: {key}")
    if value < 1:
        raise FsError(f"Edit {index} ({op}) {key} must be >= 1")
    return value


def _optional_expected_match_count(edit: dict[str, Any], *, index: int, op: str) -> int | None:
    value = edit.get("expected_match_count")
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise FsError(f"Edit {index} ({op}) expected_match_count must be a non-negative integer")
    return value


def _count_matches(content: str, target: str, *, index: int, op: str) -> int:
    if not target:
        raise FsError(f"Edit {index} ({op}) requires a non-empty target")
    return content.count(target)


def _validate_match_count(
    *,
    count: int,
    expected_count: int | None,
    index: int,
    op: str,
) -> None:
    if expected_count is None:
        if count == 1:
            return
        if count == 0:
            raise FsError(f"Edit {index} ({op}) target matched 0 times; expected exactly 1")
        raise FsError(
            f"Edit {index} ({op}) target matched {count} times; expected exactly 1. "
            "Set expected_match_count to allow this."
        )
    if count != expected_count:
        raise FsError(
            f"Edit {index} ({op}) target matched {count} times; expected {expected_count}"
        )


def _content_lines(content: str) -> list[str]:
    return content.splitlines(keepends=True)


def _validate_line_range(
    *,
    lines: list[str],
    start_line: int,
    end_line: int,
    index: int,
    op: str,
) -> None:
    total_lines = len(lines)
    if end_line < start_line:
        raise FsError(
            f"Edit {index} ({op}) end_line ({end_line}) must be >= start_line ({start_line})"
        )
    if start_line > total_lines:
        raise FsError(
            f"Edit {index} ({op}) start_line {start_line} is beyond end of file "
            f"({total_lines} lines)"
        )
    if end_line > total_lines:
        raise FsError(
            f"Edit {index} ({op}) end_line {end_line} is beyond end of file ({total_lines} lines)"
        )


def _line_selection(lines: list[str], *, start_line: int, end_line: int) -> str:
    return "".join(lines[start_line - 1 : end_line])


def _canonical_line_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _validate_expected_old(
    *,
    selected: str,
    edit: dict[str, Any],
    index: int,
    op: str,
) -> None:
    expected_old = edit.get("expected_old")
    if expected_old is None:
        return
    if not isinstance(expected_old, str):
        raise FsError(f"Edit {index} ({op}) expected_old must be a string when provided")
    if selected != expected_old and _canonical_line_text(selected) != _canonical_line_text(
        expected_old
    ):
        selected_preview = selected[:500].replace("\n", "\\n")
        expected_preview = expected_old[:500].replace("\n", "\\n")
        raise FsError(
            f"Edit {index} ({op}) selected line text did not match expected_old. "
            f"selected={selected_preview!r} expected={expected_preview!r}"
        )


def _apply_line_edit(content: str, edit: dict[str, Any], *, index: int, op: str) -> str:
    lines = _content_lines(content)
    total_lines = len(lines)

    if op == "replace_lines":
        start_line = _require_edit_line_number(edit, "start_line", index=index, op=op)
        end_line = _require_edit_line_number(edit, "end_line", index=index, op=op)
        _validate_line_range(
            lines=lines,
            start_line=start_line,
            end_line=end_line,
            index=index,
            op=op,
        )
        selected = _line_selection(lines, start_line=start_line, end_line=end_line)
        _validate_expected_old(selected=selected, edit=edit, index=index, op=op)
        replacement = _require_edit_string(edit, "replacement", index=index, op=op)
        return "".join(lines[: start_line - 1]) + replacement + "".join(lines[end_line:])

    line = _require_edit_line_number(edit, "line", index=index, op=op)
    if line > total_lines:
        raise FsError(
            f"Edit {index} ({op}) line {line} is beyond end of file ({total_lines} lines)"
        )
    insert_content = _require_edit_string(edit, "content", index=index, op=op)
    if op == "insert_before_line":
        return "".join(lines[: line - 1]) + insert_content + "".join(lines[line - 1 :])
    return "".join(lines[:line]) + insert_content + "".join(lines[line:])


def _apply_single_fs_edit(content: str, edit: dict[str, Any], *, index: int) -> str:
    raw_op = edit.get("op")
    if not isinstance(raw_op, str):
        raise FsError(f"Edit {index} is missing required string field: op")
    op = _FS_EDIT_OPERATION_ALIASES.get(raw_op.strip(), raw_op.strip())
    if op not in _FS_EDIT_OPERATIONS:
        allowed = ", ".join(sorted(_FS_EDIT_OPERATIONS))
        raise FsError(f"Edit {index} has unsupported op: {op!r}. Expected one of: {allowed}")

    if op == "append":
        return content + _require_edit_string(edit, "content", index=index, op=op)
    if op == "prepend":
        return _require_edit_string(edit, "content", index=index, op=op) + content
    if op in {"replace_lines", "insert_before_line", "insert_after_line"}:
        return _apply_line_edit(content, edit, index=index, op=op)

    target = _require_edit_string(edit, "target", index=index, op=op)
    replacement: str | None = None
    insert_content: str | None = None
    if op == "replace_exact":
        replacement = _require_edit_string(edit, "replacement", index=index, op=op)
    else:
        insert_content = _require_edit_string(edit, "content", index=index, op=op)
    expected_count = _optional_expected_match_count(edit, index=index, op=op)
    count = _count_matches(content, target, index=index, op=op)
    _validate_match_count(count=count, expected_count=expected_count, index=index, op=op)
    if count == 0:
        return content

    if op == "replace_exact":
        assert replacement is not None
        return content.replace(target, replacement)

    if op == "insert_before_exact":
        assert insert_content is not None
        return content.replace(target, insert_content + target)
    assert insert_content is not None
    return content.replace(target, target + insert_content)


def prepare_fs_edit(*, root: Path, path: str, edits: list[dict[str, Any]]) -> PreparedFsEdit:
    if not isinstance(edits, list) or not edits:
        raise FsError("edits must be a non-empty array of edit objects")

    path_obj = _resolve_under_root(root, path)
    if not path_obj.exists():
        raise FsError(f"Not found: {path}")
    if path_obj.is_dir():
        raise FsError(f"Is a directory: {path}")

    original_bytes, precondition = _snapshot_existing_file(path_obj, path)
    original_content = original_bytes.decode("utf-8", errors="replace")
    updated_content = original_content
    for index, raw_edit in enumerate(edits, start=1):
        if not isinstance(raw_edit, dict):
            raise FsError(f"Edit {index} must be an object")
        updated_content = _apply_single_fs_edit(updated_content, raw_edit, index=index)

    return PreparedFsEdit(
        root_obj=root.resolve(),
        path=path,
        path_obj=path_obj,
        original_content=original_content,
        updated_content=updated_content,
        applied_edits=len(edits),
        precondition=precondition,
    )


def write_prepared_fs_edit(prepared: PreparedFsEdit, *, root: Path | None = None) -> dict[str, Any]:
    effective_root = (root or prepared.root_obj).resolve()
    _atomic_replace_text(
        prepared.path_obj,
        prepared.updated_content,
        root=effective_root,
        precondition=prepared.precondition,
    )
    return {
        "path": prepared.path,
        "applied_edits": prepared.applied_edits,
        "changed": prepared.updated_content != prepared.original_content,
        "bytes": len(prepared.updated_content.encode("utf-8")),
    }


def fs_edit(*, root: Path, path: str, edits: list[dict[str, Any]]) -> dict[str, Any]:
    prepared = prepare_fs_edit(root=root, path=path, edits=edits)
    return write_prepared_fs_edit(prepared, root=root)


def _require_existing_file(path_obj: Path, user_path: str) -> None:
    if not path_obj.exists():
        raise FsError(f"Not found: {user_path}")
    if path_obj.is_dir():
        raise FsError(f"Is a directory: {user_path}")


def _prepare_destination_file(
    destination_obj: Path, destination_path: str, *, overwrite: bool
) -> bool:
    overwritten = False
    if destination_obj.exists():
        if destination_obj.is_dir():
            raise FsError(f"Destination is a directory: {destination_path}")
        if not overwrite:
            raise FsError(f"Destination exists and overwrite is false: {destination_path}")
        overwritten = True
    mkdir_host_path(destination_obj.parent)
    return overwritten


def fs_move(
    *,
    root: Path,
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
    source_precondition: FilePrecondition | None = None,
    destination_precondition: FilePrecondition | None = None,
) -> dict[str, Any]:
    source_obj = _resolve_under_root(root, source_path)
    destination_obj = _resolve_under_root(root, destination_path)
    guard_host_mutation(source_obj)
    guard_host_mutation(destination_obj)
    if source_obj == destination_obj:
        raise FsError(f"Source and destination are the same: {source_path}")
    if source_precondition is not None:
        assert_file_precondition(root=root, precondition=source_precondition)
    if destination_precondition is not None:
        assert_file_precondition(root=root, precondition=destination_precondition)
    _require_existing_file(source_obj, source_path)
    overwritten = _prepare_destination_file(
        destination_obj,
        destination_path,
        overwrite=overwrite,
    )
    size = source_obj.stat().st_size
    # Parent creation and destination validation can take observable time.
    # These checks reject the common approval race cheaply; the displacement
    # transaction below verifies the actual inode removed from each name.
    if source_precondition is not None:
        assert_file_precondition(root=root, precondition=source_precondition)
    if destination_precondition is not None:
        assert_file_precondition(root=root, precondition=destination_precondition)
    effective_source = source_precondition or capture_file_precondition(root=root, path=source_path)
    effective_destination = destination_precondition or capture_file_precondition(
        root=root, path=destination_path
    )
    source_displaced = _displace_regular_file_if_matches(
        target_path=source_obj,
        precondition=effective_source,
    )
    assert source_displaced is not None
    destination_displaced: Path | None = None
    try:
        destination_displaced = _displace_regular_file_if_matches(
            target_path=destination_obj,
            precondition=effective_destination,
        )
        # Detect writes through an already-open source handle before publishing
        # the displaced inode at its new name.
        if not _displaced_file_matches(source_displaced, effective_source):
            raise StaleFileError(source_path)
        _publish_regular_move_no_replace(
            source_displaced,
            destination_obj,
            mode=effective_source.mode,
        )
    except Exception as exc:
        source_restored = _restore_displaced_no_replace(source_displaced, source_obj)
        destination_restored = True
        if destination_displaced is not None:
            destination_restored = _restore_displaced_no_replace(
                destination_displaced, destination_obj
            )
        if not source_restored or not destination_restored:
            raise FsError(
                "The move encountered concurrent changes; newer public content was preserved "
                "and a displaced recovery copy remains beside the affected file."
            ) from exc
        raise
    if destination_displaced is not None:
        destination_displaced.unlink(missing_ok=True)
    return {
        "source_path": source_path,
        "destination_path": destination_path,
        "moved": True,
        "overwritten": overwritten,
        "bytes": size,
    }


def fs_copy(
    *,
    root: Path,
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
    source_precondition: FilePrecondition | None = None,
    destination_precondition: FilePrecondition | None = None,
) -> dict[str, Any]:
    source_obj = _resolve_under_root(root, source_path)
    destination_obj = _resolve_under_root(root, destination_path)
    guard_host_mutation(destination_obj)
    if source_obj == destination_obj:
        raise FsError(f"Source and destination are the same: {source_path}")
    if source_precondition is not None:
        assert_file_precondition(root=root, precondition=source_precondition)
    if destination_precondition is not None:
        assert_file_precondition(root=root, precondition=destination_precondition)
    _require_existing_file(source_obj, source_path)
    overwritten = _prepare_destination_file(
        destination_obj,
        destination_path,
        overwrite=overwrite,
    )
    source_data, current_source = _snapshot_existing_file(source_obj, source_path)
    effective_source = source_precondition or current_source
    if (
        current_source.content_sha256 != effective_source.content_sha256
        or current_source.identity_sha256 != effective_source.identity_sha256
    ):
        raise StaleFileError(source_path)
    effective_destination = destination_precondition or capture_file_precondition(
        root=root, path=destination_path
    )
    if not overwrite and effective_destination.exists:
        raise FsError(f"Destination exists and overwrite is false: {destination_path}")
    # The copied bytes are a coherent snapshot. Re-check the source before the
    # destination commit so an approval-time source edit is reported as stale.
    assert_file_precondition(root=root, precondition=effective_source)
    staged = _stage_bytes_sibling(
        destination_obj,
        source_data,
        mode=effective_source.mode,
    )
    try:
        assert_file_precondition(root=root, precondition=effective_destination)
        _commit_staged_regular_file(
            staged_path=staged,
            target_path=destination_obj,
            precondition=effective_destination,
        )
    finally:
        staged.unlink(missing_ok=True)
    return {
        "source_path": source_path,
        "destination_path": destination_path,
        "copied": True,
        "overwritten": overwritten,
        "bytes": len(source_data),
    }


def fs_delete(
    *,
    root: Path,
    path: str,
    precondition: FilePrecondition | None = None,
) -> dict[str, Any]:
    path_obj = _resolve_under_root(root, path)
    guard_host_mutation(path_obj)
    if precondition is not None:
        assert_file_precondition(root=root, precondition=precondition)
    _require_existing_file(path_obj, path)
    size = path_obj.stat().st_size
    effective_precondition = precondition or capture_file_precondition(root=root, path=path)
    displaced = _displace_regular_file_if_matches(
        target_path=path_obj,
        precondition=effective_precondition,
    )
    assert displaced is not None
    if not _displaced_file_matches(displaced, effective_precondition):
        if not _restore_displaced_no_replace(displaced, path_obj):
            raise FsError(
                f"A concurrent edit was preserved at {path}; "
                f"the displaced version remains at {displaced.name}"
            )
        raise StaleFileError(path)
    if os.path.lexists(path_obj):
        # A writer recreated the name after the matching version was removed.
        # Preserve the new file and fail rather than reporting a clean delete.
        displaced.unlink(missing_ok=True)
        raise StaleFileError(path)
    displaced.unlink()
    return {
        "path": path,
        "deleted": True,
        "bytes": size,
    }


def _find_git_marker_root(path: Path, *, boundary: Path) -> Path | None:
    boundary_abs = boundary.resolve()
    for candidate in (path, *path.parents):
        candidate_abs = candidate.resolve()
        try:
            candidate_abs.relative_to(boundary_abs)
        except ValueError:
            return None
        if (candidate_abs / ".git").exists():
            return candidate_abs
    return None


def _git_repo_root(root: Path, *, boundary: Path) -> Path | None:
    from ..git_safe import build_git_process_env

    marker_root = _find_git_marker_root(root, boundary=boundary)
    if marker_root is None:
        return None
    try:
        cp = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            env=build_git_process_env(),
            timeout=_GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    raw_root = os.fsdecode(cp.stdout.removesuffix(b"\n"))
    if not raw_root:
        return marker_root
    try:
        repo_root = Path(raw_root).resolve()
        repo_root.relative_to(boundary.resolve())
    except (OSError, ValueError):
        return marker_root
    return repo_root


def _git_check_ignored(
    repo_root: Path,
    rel_paths: list[str],
    *,
    evidence_sources: set[str] | None = None,
) -> set[str]:
    from ..git_safe import build_git_process_env

    if not rel_paths:
        return set()
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "--stdin", "-z"],
            input=b"\0".join(os.fsencode(path) for path in rel_paths) + b"\0",
            check=False,
            capture_output=True,
            env=build_git_process_env(),
            timeout=_GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        cp = None
    if cp is None or cp.returncode not in (0, 1):
        if evidence_sources is not None:
            evidence_sources.add("approximate_root_gitignore_fallback")
        return _fallback_gitignore_ignored_untracked(repo_root, rel_paths)
    if evidence_sources is not None:
        evidence_sources.add("git_check_ignore")
    # Exit 1 is definitive negative evidence. The approximate fallback must
    # never override Git's own matching semantics, including nested paths.
    if cp.returncode == 1:
        return set()
    return {Path(os.fsdecode(path)).as_posix() for path in cp.stdout.split(b"\0") if path}


def _git_tracked_paths(repo_root: Path, rel_paths: list[str]) -> set[str]:
    from ..git_safe import build_git_process_env

    if not rel_paths:
        return set()
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", *rel_paths],
            check=False,
            capture_output=True,
            env=build_git_process_env(),
            timeout=_GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if cp.returncode != 0:
        return set()
    return {Path(os.fsdecode(item)).as_posix() for item in cp.stdout.split(b"\0") if item}


def _fallback_gitignore_ignored_untracked(repo_root: Path, rel_paths: list[str]) -> set[str]:
    ignored = _fallback_gitignore_ignored(repo_root, rel_paths)
    if ignored:
        ignored.difference_update(_git_tracked_paths(repo_root, rel_paths))
    return ignored


def _fallback_gitignore_ignored(repo_root: Path, rel_paths: list[str]) -> set[str]:
    gitignore = repo_root / ".gitignore"
    try:
        raw_patterns = gitignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()

    ignored: set[str] = set()
    normalized_paths = [Path(rel).as_posix() for rel in rel_paths]
    for raw in raw_patterns:
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        if negated:
            pattern = pattern[1:].strip()
        if not pattern or pattern.endswith("/"):
            continue
        pattern = pattern.lstrip("/").replace("\\", "/")
        for rel in normalized_paths:
            name = rel.rsplit("/", 1)[-1]
            matched = fnmatch(rel, pattern) or ("/" not in pattern and fnmatch(name, pattern))
            if not matched:
                continue
            if negated:
                ignored.discard(rel)
            else:
                ignored.add(rel)
    return ignored


def fs_list(
    *,
    root: Path,
    root_path: str = ".",
    globs: list[str] | None = None,
    ignore: list[str] | None = None,
    max_results: int = _DEFAULT_FS_LIST_MAX_RESULTS,
) -> dict[str, Any]:
    base = _resolve_under_root(root, root_path)
    patterns = globs or ["**/*"]
    ignore_set = set(ignore or [])

    repo_root = _git_repo_root(base, boundary=root)
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    gitignore_evidence_sources: set[str] = set()
    truncated = False
    batch_size = max(256, max_results or 0)
    pending: list[tuple[Path, str, str | None]] = []

    def _flush_pending() -> bool:
        nonlocal truncated
        ignored_by_git: set[str] = set()
        if repo_root:
            rels = [rel_git for _, _, rel_git in pending if rel_git]
            ignored_by_git = _git_check_ignored(
                repo_root, rels, evidence_sources=gitignore_evidence_sources
            )

        # Count visibility after gitignore filtering so returned entries fill the
        # visible result window and `truncated` only reflects hidden extra visible files.
        for path_obj, rel, rel_git in pending:
            if rel_git and rel_git in ignored_by_git:
                continue
            if rel in seen_paths:
                continue
            if len(entries) >= max_results:
                truncated = True
                return True
            seen_paths.add(rel)
            try:
                size = path_obj.stat().st_size
            except OSError:
                size = None
            entries.append({"path": rel, "size": size})
        return False

    for pat in patterns:
        for p in base.glob(pat):
            rel_path = p.relative_to(base)
            try:
                resolved_path = p.resolve()
                resolved_rel = resolved_path.relative_to(base)
            except (OSError, RuntimeError, ValueError):
                continue
            # Glob traversal and symlink targets must obey the same selected-root
            # boundary. Preserve ordinary symlink names; canonicalize traversal
            # aliases so their reported names remain usable and count only once.
            if os.pardir in rel_path.parts:
                p = resolved_path
                rel_path = resolved_rel
            rel_parts = rel_path.parts
            if set(rel_parts) & _DEFAULT_IGNORE_DIRS:
                continue
            if any(seg in ignore_set for seg in rel_parts):
                continue
            if p.is_dir():
                continue

            rel = rel_path.as_posix()
            rel_git: str | None = None
            if repo_root:
                try:
                    rel_git_path = os.path.relpath(p, repo_root)
                except ValueError:
                    rel_git = None
                else:
                    if rel_git_path in {os.curdir, os.pardir} or rel_git_path.startswith(
                        os.pardir + os.sep
                    ):
                        rel_git = None
                    else:
                        rel_git = Path(rel_git_path).as_posix()

            pending.append((p, rel, rel_git))
            if len(pending) >= batch_size:
                if _flush_pending():
                    pending.clear()
                    break
                pending.clear()
        if truncated:
            break

    if not truncated and pending:
        _flush_pending()

    return {
        "root": os.fspath(base),
        "entries": entries,
        "truncated": truncated,
        "returned_count": len(entries),
        "max_results": max_results,
        "scope": {
            "globs": list(patterns),
            "entry_kind": "files",
            "path_boundary": "resolved targets stay within root_path",
            "ignored_path_components": sorted(ignore_set),
            "default_ignored_path_components": sorted(_DEFAULT_IGNORE_DIRS),
            "gitignore": {
                "policy": "best_effort",
                "git_repository_resolved": repo_root is not None,
                "evidence_sources": sorted(gitignore_evidence_sources),
                **(
                    {
                        "limitation": (
                            "Git evidence was unavailable; the root .gitignore fallback is "
                            "approximate and does not implement all directory/nested rules."
                        )
                    }
                    if "approximate_root_gitignore_fallback" in gitignore_evidence_sources
                    else {}
                ),
            },
            "completeness": (
                "truncated applies only to matching files after these exclusions; "
                "it does not establish repository-wide absence."
            ),
        },
    }
