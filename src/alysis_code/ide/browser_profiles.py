"""Private, clean-shutdown Chromium profiles for the local Linux executor.

The caller owns browser shutdown verification. A lease never infers a clean
shutdown from file contents or a released lock. Unclean reopening requires proof
that the recorded browser process group stopped; Chromium recovers its own data.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from contextlib import suppress
from pathlib import Path


class BrowserProfileError(RuntimeError):
    """A safe profile ownership or durability boundary could not be established."""


def _identity(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
    ):
        raise BrowserProfileError("Browser profile identity is invalid.")
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise BrowserProfileError("Browser profile identity is invalid.") from None


def _private(info: os.stat_result, *, directory: bool) -> None:
    correct_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    mode = 0o700 if directory else 0o600
    if (
        not correct_kind
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise BrowserProfileError("Browser profile storage is not private and owned.")


def _directory(parent: int, name: str, *, create: bool) -> tuple[int, bool]:
    created = False
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            created = True
            os.fsync(parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _private(os.fstat(fd), directory=True)
        return fd, created
    except BaseException:
        os.close(fd)
        raise


def _root(path: Path, *, create: bool) -> int:
    """Traverse every ancestor without following links; only our root is private."""
    if os.name != "posix" or not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise BrowserProfileError("Browser profile root must be an absolute private POSIX path.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _private(os.fstat(fd), directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _metadata(entry: int, expected: dict[str, object]) -> dict[str, object]:
    fd = os.open("metadata.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=entry)
    try:
        _private(os.fstat(fd), directory=False)
        data = os.read(fd, 4097)
        if len(data) > 4096:
            raise ValueError("metadata size")
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or set(value) != {*expected, "state"}
            or type(value.get("version")) is not int
            or any(value.get(key) != item for key, item in expected.items())
            or value.get("state") not in ("clean", "dirty")
        ):
            raise ValueError("metadata schema")
        return value
    finally:
        os.close(fd)


def _write_metadata(entry: int, value: dict[str, object], name="metadata.json") -> None:
    temporary = ".metadata-" + secrets.token_hex(16) + ".tmp"
    fd = None
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=entry
        )
        _private(os.fstat(fd), directory=False)
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError("incomplete metadata write")
            data = data[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, name, src_dir_fd=entry, dst_dir_fd=entry)
        os.fsync(entry)
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=entry)


def _process_context():
    return {
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "namespace": os.readlink("/proc/self/ns/pid"),
    }


def _stopped(entry):
    """A free lease alone does not prove that browser descendants are gone."""
    try:
        fd = os.open("process.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=entry)
        try:
            _private(os.fstat(fd), directory=False)
            value = json.loads(os.read(fd, 4097))
        finally:
            os.close(fd)
        if value == {"stopped": True}:
            return True  # Trusted managed-browser cleanup attested this while leased.
        if (
            set(value) != {"pid", "boot", "namespace"}
            or type(value["pid"]) is not int
            or not 1 < value["pid"] <= 2**31 - 1
        ):
            return False
        if {k: value[k] for k in ("boot", "namespace")} != _process_context():
            return False  # Cannot prove ownership across hosts/PID namespaces.
        try:
            os.killpg(value["pid"], 0)
        except ProcessLookupError:
            return True
    except (OSError, ValueError, TypeError):
        pass
    return False


class BrowserProfileStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        try:
            os.close(_root(self.root, create=True))
        except (OSError, ValueError):
            raise BrowserProfileError("Browser profile root could not be opened safely.") from None

    def acquire(self, account_id: str, owner_id: str) -> BrowserProfileLease:
        import fcntl

        account, owner = _identity(account_id), _identity(owner_id)
        key = hashlib.sha256((account + owner).encode("ascii")).hexdigest()
        expected = {"version": 1, "account": account, "owner": owner}
        root_fd = entry = lock = profile = None
        try:
            root_fd = _root(self.root, create=False)
            entry, created = _directory(root_fd, key, create=True)
            lock = os.open(
                "lease.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=entry,
            )
            _private(os.fstat(lock), directory=False)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BrowserProfileError("Browser profile is already in use.") from None
            recovered = False
            if created:
                profile, _ = _directory(entry, "profile", create=True)
                _write_metadata(entry, {**expected, "state": "clean"})
            else:
                state = _metadata(entry, expected)
                recovered = state["state"] == "dirty" and _stopped(entry)
                if state["state"] != "clean" and not recovered:
                    raise BrowserProfileError(
                        "Browser profile is dirty; explicit recovery is required."
                    )
                profile, _ = _directory(entry, "profile", create=False)
            lease = BrowserProfileLease(
                self.root, key, owner_id, expected, root_fd, entry, lock, profile
            )
            lease.recovered = recovered
            root_fd = entry = lock = profile = None
            return lease
        except (OSError, ValueError, TypeError):
            raise BrowserProfileError("Browser profile could not be acquired safely.") from None
        finally:
            for fd in (profile, lock, entry, root_fd):
                if fd is not None:
                    os.close(fd)


class BrowserProfileLease:
    def __init__(self, root, key, owner_id, expected, root_fd, entry, lock, profile):
        self._root, self._key, self._owner_id, self._expected = root, key, owner_id, expected
        self._root_fd, self._entry, self._lock_fd, self._profile = root_fd, entry, lock, profile
        self._guard = threading.RLock()
        self._released = self._attempted = self._begun = self._clean = False
        self._clean_pending = False
        self.recovered = False

    @property
    def path(self) -> Path:
        return self._root / self._key / "profile"

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def _validate(self) -> None:
        if self._released:
            raise BrowserProfileError("Browser profile lease was released.")
        root_fd = entry = profile = lock = None
        try:
            root_fd = _root(self._root, create=False)
            entry, _ = _directory(root_fd, self._key, create=False)
            profile, _ = _directory(entry, "profile", create=False)
            lock = os.open("lease.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=entry)
            _private(os.fstat(lock), directory=False)
            for current, held in (
                (root_fd, self._root_fd),
                (entry, self._entry),
                (profile, self._profile),
                (lock, self._lock_fd),
            ):
                a, b = os.fstat(current), os.fstat(held)
                if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                    raise BrowserProfileError("Browser profile storage changed while leased.")
        finally:
            for fd in (lock, profile, entry, root_fd):
                if fd is not None:
                    os.close(fd)

    def begin(self, owner_id: str) -> Path:
        with self._guard:
            _identity(owner_id)
            if owner_id != self.owner_id:
                raise BrowserProfileError("Browser profile is not owned by this caller.")
            if self._attempted:
                raise BrowserProfileError("Browser profile lease has already begun.")
            try:
                self._validate()
                self._attempted = True
                if _metadata(self._entry, self._expected)["state"] != "clean" and not (
                    self.recovered and _stopped(self._entry)
                ):
                    raise BrowserProfileError(
                        "Browser profile is dirty; explicit recovery is required."
                    )
                try:
                    endpoint = os.stat(
                        "DevToolsActivePort", dir_fd=self._profile, follow_symlinks=False
                    )
                except FileNotFoundError:
                    endpoint = None
                if endpoint is not None and (
                    not stat.S_ISREG(endpoint.st_mode)
                    or endpoint.st_uid != os.geteuid()
                    or endpoint.st_nlink != 1
                ):
                    raise BrowserProfileError(
                        "Browser profile endpoint metadata is not a regular owned file."
                    )
                with suppress(FileNotFoundError):
                    os.unlink("process.json", dir_fd=self._entry)
                    os.fsync(self._entry)
                _write_metadata(self._entry, {**self._expected, "state": "dirty"})
                if endpoint is not None:
                    os.unlink("DevToolsActivePort", dir_fd=self._profile)
                    os.fsync(self._profile)
                self._begun = True
                return self.path
            except (OSError, ValueError, TypeError):
                raise BrowserProfileError("Browser profile could not begin safely.") from None

    def record_process(self, pid: int) -> None:
        """Bind recovery to our Linux process group, never to a reused PID alone."""
        with self._guard:
            self._validate()
            if not self._begun:
                raise BrowserProfileError("Browser profile has not begun.")
            try:
                context = _process_context()
                if type(pid) is not int or pid <= 1 or os.getpgid(pid) != pid:
                    return
            except OSError:
                return  # Unsupported platform/startup race: dirty recovery stays blocked.
            _write_metadata(self._entry, {"pid": pid, **context}, "process.json")

    def mark_stopped(self) -> None:
        """Caller attests that the owned process group and transport have stopped."""
        with self._guard:
            self._validate()
            if not self._begun:
                raise BrowserProfileError("Browser profile has not begun.")
            _write_metadata(self._entry, {"stopped": True}, "process.json")

    def mark_clean(self) -> None:
        """Caller attests that graceful browser exit and owned-process cleanup were verified."""
        with self._guard:
            try:
                self._validate()
                if not self._begun:
                    raise BrowserProfileError("Browser profile lease has not begun.")
                state = _metadata(self._entry, self._expected)["state"]
                if self._clean:
                    if state != "clean":
                        raise BrowserProfileError(
                            "Browser profile clean state changed unexpectedly."
                        )
                    return
                if self._clean_pending and state == "clean":
                    # Atomic replacement may have succeeded before its parent
                    # directory fsync failed. Only this attesting lease can
                    # finish that transition without rewriting dirty state.
                    os.fsync(self._entry)
                    self._clean = True
                    return
                if state != "dirty":
                    raise BrowserProfileError("Browser profile dirty state changed unexpectedly.")
                self._clean_pending = True
                _write_metadata(self._entry, {**self._expected, "state": "clean"})
                self._clean = True
            except (OSError, ValueError, TypeError):
                raise BrowserProfileError(
                    "Browser profile clean state could not be saved safely."
                ) from None

    def release(self) -> None:
        """Relinquish ownership; releasing an active profile never marks it clean."""
        with self._guard:
            if self._released:
                return
            self._released = True
            for fd in (self._profile, self._lock_fd, self._entry, self._root_fd):
                os.close(fd)

    def __del__(self):
        with suppress(Exception):
            self.release()
