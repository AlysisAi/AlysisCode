"""A home and a temp folder for one command in the Windows sandbox.

A macOS command gets a scratch folder with a home in it, outside the project,
removed when the command ends. A Windows command had neither. Its temp folder
was the project folder, so temporary files, caches and "throwaway"
environments landed among the person's files. It had no home at all, so ``~``
was empty and ``Path.home()`` raised. This module gives it the same thing
macOS has: one folder per command under the person's local application data,
the only place outside the approved folders the command can write.

Nothing in the folder is trusted. It is not a boundary either: if it cannot
be prepared, the command runs with the previous arrangement.
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from pathlib import Path

from .config import ConfigError

# Short names: package managers nest deeply below the home and temp folders,
# and many Windows programs still stop at 260 characters.
_FOLDER = ("alysis-code", "scratch")
_ABANDONED_AFTER_S = 24 * 60 * 60
# The open lock file of every scratch folder this process owns. Windows
# refuses to delete a file another process holds open, which is how a later
# process tells a folder in use from one left behind by a crash.
_locks: dict[Path, int] = {}
# Folders of this process that could not be deleted when their command ended.
_left_behind: set[Path] = set()
_swept = False


def scratch_base() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    if not base.is_absolute():
        raise ConfigError("Windows sandbox requires an absolute LOCALAPPDATA")
    return base.joinpath(*_FOLDER)


def _folders(scratch: Path) -> tuple[Path, Path, Path, Path]:
    """Home, roaming data, local data and temp: where Windows itself puts them."""
    home = scratch / "home"
    local = home / "AppData" / "Local"
    return home, home / "AppData" / "Roaming", local, local / "Temp"


def scratch_environment(scratch: Path) -> dict[str, str]:
    """The variables that point a command at its scratch home and temp folder."""
    home, roaming, local, temp = _folders(scratch)
    return {
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive) :],
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
    }


def create_scratch() -> Path | None:
    """Make the scratch folder for one command, or ``None`` when that is not possible."""
    from .windows_sandbox import _local_path

    try:
        base = scratch_base()
        base.mkdir(parents=True, exist_ok=True)
        # The sandbox refuses a writable folder behind a link or on a volume
        # it cannot contain; find that out here, not as a failed command.
        base = _local_path(base)
        _remove_abandoned(base)
        for earlier in tuple(_left_behind):
            remove_scratch(earlier)
        name = secrets.token_hex(5)
        scratch = base / name
        lock = os.open(base / (name + ".lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            for folder in _folders(scratch):
                folder.mkdir(parents=True, exist_ok=True)
        except BaseException:
            os.close(lock)
            raise
        _locks[scratch] = lock
        return scratch
    except (OSError, ConfigError):
        return None


def remove_scratch(scratch: Path | None, *, wait_s: float = 0.0) -> None:
    """Delete a command's scratch folder once nothing of the command is running.

    A foreground command has ended completely when this is called. A stopped
    background command may still have a process on its way out that holds a
    file open, so its caller allows a short ``wait_s``. A folder that is still
    there afterwards gives up its lock, so that this process tries again before
    its next command and any later process may sweep it.
    """
    if scratch is None:
        return
    lock = _locks.pop(scratch, None)
    deadline = time.monotonic() + wait_s
    while True:
        # A link the command made inside is removed as a link; what it points
        # at is never opened.
        shutil.rmtree(scratch, ignore_errors=True)
        if not scratch.exists() or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if lock is not None:
        try:
            os.close(lock)
        except OSError:
            pass
    if scratch.exists():
        _left_behind.add(scratch)
        return
    _left_behind.discard(scratch)
    try:
        os.unlink(scratch.parent / (scratch.name + ".lock"))
    except OSError:
        pass


def _remove_abandoned(base: Path) -> None:
    """Once per process, delete scratch folders whose engine process is gone."""
    global _swept
    if _swept:
        return
    _swept = True
    try:
        entries = list(os.scandir(base))
    except OSError:
        return
    held = {scratch.name for scratch in _locks}
    locked = set()
    for entry in entries:
        if not entry.name.endswith(".lock") or entry.name[: -len(".lock")] in held:
            continue
        name = entry.name[: -len(".lock")]
        folder = base / name
        try:
            if folder.exists():
                # Deleting the lock fails while its owner is alive. Take it
                # over first, so two sweeps cannot both delete one folder.
                os.replace(entry.path, entry.path + ".old")
                shutil.rmtree(folder, ignore_errors=True)
                os.unlink(entry.path + ".old")
            else:
                os.unlink(entry.path)
        except OSError:
            locked.add(name)
    cutoff = time.time() - _ABANDONED_AFTER_S
    for entry in entries:
        # A folder without a lock is what a failed removal leaves behind.
        try:
            if (
                entry.is_dir(follow_symlinks=False)
                and entry.name not in locked
                and entry.name not in held
                and not (base / (entry.name + ".lock")).exists()
                and entry.stat(follow_symlinks=False).st_mtime < cutoff
            ):
                shutil.rmtree(entry.path, ignore_errors=True)
            elif entry.name.endswith(".lock.old") and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue
