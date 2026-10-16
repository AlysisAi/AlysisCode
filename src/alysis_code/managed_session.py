"""Race-safe filesystem boundary for App-managed named session logs.

The Desktop App supplies both a session identifier and a session directory to
``alysis run``.  Treating those values as ordinary ``Path`` objects leaves a
check/open race and lets a log-file symlink redirect private conversation data.
This module opens the directory as a retained capability and opens the leaf
relative to it without following links. POSIX uses ``O_NOFOLLOW``; Windows uses
native directory handles and reparse-point checks. The resulting file descriptor
is the only object handed to the session store.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, TextIO

from .config import ConfigError

_MANAGED_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class ManagedSessionError(ConfigError):
    """An App-managed session path could not be admitted safely."""


if os.name == "nt":
    from . import _windows_session_files as _windows


def normalize_managed_session_id(raw_value: Any) -> str:
    """Return an exact App-compatible session id or fail closed.

    Windows treats reserved device basenames as devices even when an extension
    is present (for example ``CON.jsonl``).  Rejecting them on every platform
    keeps a session id portable and prevents platform-dependent path meaning.
    """

    if not isinstance(raw_value, str) or not _MANAGED_SESSION_ID_RE.fullmatch(raw_value):
        raise ManagedSessionError(
            "--session must be a safe session identifier matching "
            "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
        )
    windows_basename = raw_value.split(".", 1)[0].upper()
    if windows_basename in _WINDOWS_RESERVED_DEVICE_NAMES:
        raise ManagedSessionError("--session must not use a Windows reserved device name")
    return raw_value


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _close_quietly(fd: int | None) -> None:
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int) or value == 0:
        raise ManagedSessionError(
            "This platform cannot provide the required no-follow session-file boundary"
        )
    return value


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | _required_open_flag("O_NOFOLLOW")
    flags |= int(getattr(os, "O_DIRECTORY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    return flags


def _file_open_flags(*, writable: bool) -> int:
    flags = (os.O_RDWR | os.O_APPEND) if writable else os.O_RDONLY
    flags |= _required_open_flag("O_NOFOLLOW")
    if not writable:
        # Admission must reach fstat even when an attacker substituted a FIFO.
        # O_RDONLY alone can block forever waiting for a writer before the
        # regular-file check gets a chance to reject the leaf.
        flags |= _required_open_flag("O_NONBLOCK")
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_BINARY", 0))
    return flags


def _assert_same_regular_leaf(*, directory_fd: int, filename: str, file_fd: int) -> None:
    if os.name == "nt":
        _windows.assert_same_regular_leaf(directory_fd, filename, file_fd)
        return
    opened = os.fstat(file_fd)
    if not stat.S_ISREG(opened.st_mode):
        raise ManagedSessionError("Managed session log must be a regular file")
    # A multiply-linked file can also name an object outside the admitted
    # directory.  Refuse it rather than append private data through a hardlink.
    if int(getattr(opened, "st_nlink", 1)) != 1:
        raise ManagedSessionError("Managed session log must not be hard-linked")
    try:
        current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ManagedSessionError("Managed session log changed while it was being opened") from exc
    if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(opened):
        raise ManagedSessionError("Managed session log changed while it was being opened")


def _read_events_from_fd(file_fd: int) -> tuple[dict[str, Any], ...]:
    """Read the already-admitted descriptor without reopening its pathname."""

    events: list[dict[str, Any]] = []
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
        pending = b""
        while True:
            chunk = os.read(file_fd, 64 * 1024)
            if not chunk:
                break
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    events.append(value)
        if pending.strip():
            try:
                value = json.loads(pending.strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                value = None
            if isinstance(value, dict):
                events.append(value)
    finally:
        # O_APPEND makes every later write target EOF, but leave the shared
        # offset at EOF as well so a transferred text writer starts cleanly.
        os.lseek(file_fd, 0, os.SEEK_END)
    return tuple(events)


class ManagedSessionPersistence:
    """Retained directory/file capabilities for one named session."""

    def __init__(
        self,
        *,
        sessions_dir: Path,
        session_id: str,
        directory_fd: int,
        file_fd: int | None,
        append_enabled: bool,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        self.sessions_dir = sessions_dir
        self.session_id = session_id
        self.path = sessions_dir / f"{session_id}.jsonl"
        self.append_enabled = append_enabled
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._events = events
        self._sealed = False
        self._claimed = False
        self._claimed_writer: TextIO | None = None

    def events_snapshot(self) -> list[dict[str, Any]]:
        # JSON round-tripping matches SessionStore's existing defensive copy
        # and prevents callers from mutating the retained hydration snapshot.
        return json.loads(json.dumps(self._events, ensure_ascii=True))

    def validate_bound_path(self) -> None:
        if os.name == "nt":
            _windows.assert_same_directory(self._directory_fd, self.sessions_dir)
            if self._file_fd is not None:
                _assert_same_regular_leaf(
                    directory_fd=self._directory_fd,
                    filename=self.path.name,
                    file_fd=self._file_fd,
                )
            return
        try:
            opened_directory = os.fstat(self._directory_fd)
            current_directory = os.stat(self.sessions_dir, follow_symlinks=False)
        except OSError as exc:
            raise ManagedSessionError("Managed session directory changed after admission") from exc
        if not stat.S_ISDIR(current_directory.st_mode) or _identity(current_directory) != _identity(
            opened_directory
        ):
            raise ManagedSessionError("Managed session directory changed after admission")
        if self._file_fd is None:
            return
        _assert_same_regular_leaf(
            directory_fd=self._directory_fd,
            filename=self.path.name,
            file_fd=self._file_fd,
        )

    def seal(self) -> None:
        """Finalize the retained capability before provider setup.

        For append-enabled sessions this performs the last pathname identity
        check and converts the already-admitted descriptor into the exact text
        writer that ``SessionStore`` will later claim.  Once sealed, pathname
        replacement is irrelevant: replay came from this descriptor and all
        later JSONL writes keep using the same open file description.
        """

        if self._sealed:
            return
        self.validate_bound_path()
        if not self.append_enabled:
            self._sealed = True
            return
        if self._file_fd is None:
            raise ManagedSessionError("Managed session log is not open for append")
        fd = self._file_fd
        self._file_fd = None
        try:
            self._claimed_writer = os.fdopen(fd, "a", encoding="utf-8", newline="\n")
        except Exception as exc:
            _close_quietly(fd)
            raise ManagedSessionError(
                "Managed session log writer could not be prepared safely"
            ) from exc
        self._sealed = True

    def require_sealed(self) -> None:
        if not self._sealed:
            raise ManagedSessionError(
                "Managed session filesystem boundary was not sealed before provider setup"
            )

    def claim_text_writer(self) -> TextIO:
        if not self.append_enabled:
            raise ManagedSessionError("Managed session log is not open for append")
        if self._claimed:
            raise ManagedSessionError("Managed session log writer was already claimed")
        self.seal()
        writer = self._claimed_writer
        if writer is None or writer.closed:
            raise ManagedSessionError("Managed session log writer is unavailable")
        self._claimed = True
        return writer

    def close(self) -> None:
        if self._claimed_writer is not None and not self._claimed_writer.closed:
            try:
                self._claimed_writer.close()
            except OSError:
                pass
        self._claimed_writer = None
        _close_quietly(self._file_fd)
        self._file_fd = None
        _close_quietly(self._directory_fd)
        self._directory_fd = -1


def _prepare_sessions_directory(sessions_dir: Path, *, create: bool) -> tuple[Path, int]:
    requested = Path(sessions_dir).expanduser()
    try:
        before = requested.lstat()
    except FileNotFoundError:
        if not create:
            raise ManagedSessionError(
                f"Managed session directory does not exist: {requested}"
            ) from None
        try:
            if os.name == "nt":
                _windows.create_private_directory(requested)
            else:
                requested.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            # A concurrent creator is admitted only by the same lstat/open
            # validation below.
            pass
        except OSError as exc:
            raise ManagedSessionError(
                f"Managed session directory could not be created: {requested}"
            ) from exc
        try:
            before = requested.lstat()
        except OSError as exc:
            raise ManagedSessionError(
                f"Managed session directory is unavailable: {requested}"
            ) from exc
    except OSError as exc:
        raise ManagedSessionError(f"Managed session directory is unavailable: {requested}") from exc

    if stat.S_ISLNK(before.st_mode) or (
        os.name == "nt" and before.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ManagedSessionError("Managed session directory must not be a symlink")
    if not stat.S_ISDIR(before.st_mode):
        raise ManagedSessionError("Managed session destination must be a directory")

    try:
        resolved = requested.resolve(strict=True)
        directory_fd = (
            _windows.open_directory(resolved)
            if os.name == "nt"
            else os.open(resolved, _directory_open_flags())
        )
    except OSError as exc:
        raise ManagedSessionError(
            f"Managed session directory could not be opened safely: {requested}"
        ) from exc
    try:
        opened = os.fstat(directory_fd)
        after = requested.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(after.st_mode)
            or _identity(opened) != _identity(before)
            or _identity(after) != _identity(before)
        ):
            raise ManagedSessionError("Managed session directory changed while it was being opened")
    except Exception:
        _close_quietly(directory_fd)
        raise
    return resolved, directory_fd


def _open_session_leaf(
    *,
    directory_fd: int,
    filename: str,
    append: bool,
) -> tuple[int | None, bool]:
    if os.name == "nt":
        return _windows.open_session_leaf(directory_fd, filename, append=append)
    base_flags = _file_open_flags(writable=append)
    try:
        return os.open(filename, base_flags, dir_fd=directory_fd), False
    except FileNotFoundError:
        if not append:
            return None, False
    except OSError as exc:
        raise ManagedSessionError("Managed session log could not be opened safely") from exc

    try:
        return (
            os.open(
                filename,
                base_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            ),
            True,
        )
    except FileExistsError:
        # A racing creator may be legitimate.  Reopen through O_NOFOLLOW; a
        # racing symlink or non-regular object is rejected by the same boundary.
        try:
            return os.open(filename, base_flags, dir_fd=directory_fd), False
        except OSError as exc:
            raise ManagedSessionError("Managed session log could not be opened safely") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            raise ManagedSessionError("Managed session log must be a regular file") from exc
        raise ManagedSessionError("Managed session log could not be created") from exc


def _enforce_owner_private_file(file_fd: int) -> None:
    """Restrict an admitted append target before private events can reach it."""

    if os.name == "nt":
        _windows.enforce_owner_private_file(file_fd)
        return
    if os.name != "posix":
        return
    try:
        os.fchmod(file_fd, 0o600)
        mode = stat.S_IMODE(os.fstat(file_fd).st_mode)
    except OSError as exc:
        raise ManagedSessionError(
            "Managed session log could not be restricted to its owner"
        ) from exc
    if mode != 0o600:
        raise ManagedSessionError("Managed session log could not be restricted to its owner")


def prepare_managed_session_persistence(
    *,
    sessions_dir: Path,
    session_id: str,
    append: bool = True,
) -> ManagedSessionPersistence:
    """Validate and retain a safe named-session filesystem boundary.

    This function performs all filesystem admission before provider setup.  It
    creates missing session directories and logs with owner-only permissions.
    Existing logs are read from the same descriptor later used for append.
    """

    normalized_id = normalize_managed_session_id(session_id)
    resolved_dir, directory_fd = _prepare_sessions_directory(
        sessions_dir,
        create=append,
    )
    file_fd: int | None = None
    try:
        if append:
            if os.name == "posix" and os.fstat(directory_fd).st_mode & 0o222 == 0:
                raise ManagedSessionError("Managed session directory is not writable")
            if not os.access(resolved_dir, os.W_OK):
                raise ManagedSessionError("Managed session directory is not writable")
        file_fd, _created = _open_session_leaf(
            directory_fd=directory_fd,
            filename=f"{normalized_id}.jsonl",
            append=append,
        )
        events: tuple[dict[str, Any], ...] = ()
        if file_fd is not None:
            _assert_same_regular_leaf(
                directory_fd=directory_fd,
                filename=f"{normalized_id}.jsonl",
                file_fd=file_fd,
            )
            # Existing logs may predate the managed boundary and may have
            # inherited a permissive umask.  Normalize the retained regular,
            # single-link descriptor itself before replay, even for --no-log.
            _enforce_owner_private_file(file_fd)
            events = _read_events_from_fd(file_fd)
        return ManagedSessionPersistence(
            sessions_dir=resolved_dir,
            session_id=normalized_id,
            directory_fd=directory_fd,
            file_fd=file_fd,
            append_enabled=append,
            events=events,
        )
    except Exception:
        _close_quietly(file_fd)
        _close_quietly(directory_fd)
        raise


__all__ = [
    "ManagedSessionError",
    "ManagedSessionPersistence",
    "normalize_managed_session_id",
    "prepare_managed_session_persistence",
]
