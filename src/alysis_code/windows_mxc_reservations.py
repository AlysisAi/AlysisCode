"""Taking back the names a background command reserved in a project.

The Windows sandbox can protect only a name that exists, so an empty entry is
made for every protected name a project does not have (``AGENTS.md``, ``.git``,
...). A foreground command removes its entries when it ends. A background
command could not: another command in the same project may rely on the same
entries, in this engine process or in another one, and nothing recorded who
made what. Its entries therefore stayed for good. In a live run of the
desktop app a task that started a development server left eleven empty
entries in the person's project.

Each background command now leaves a record: one file per command and
project, outside the project, listing the entries the command made with the
identity each was created with. The command holds the file open for as long
as it runs, and Windows refuses to rename a file that is held open, so a
record that can be renamed belongs to a command that has ended, or to an
engine process that is gone. When no record in a project is held any more,
every recorded entry that is still the empty object that was made is removed.

Everything here runs while the caller holds the project's lease
(``GitLease``), the mutex every command takes before it makes or removes
entries, so no command is starting or ending in the project meanwhile.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

from .config import ConfigError

_FOLDER = ("alysis-code", "reservations")
_Entry = tuple[Path, tuple[int, bytes], bool]


def records_folder(workspace: Path) -> Path:
    """Where the records of ``workspace`` are kept. Nothing the sandbox can reach."""
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    if not base.is_absolute():
        raise ConfigError("Windows sandbox requires an absolute LOCALAPPDATA")
    name = hashlib.sha256(os.path.normcase(str(workspace)).encode()).hexdigest()[:32]
    return base.joinpath(*_FOLDER, name)


class BackgroundRecord:
    """The entries one running background command made in one project."""

    def __init__(self, workspace: Path, entries: list[_Entry]) -> None:
        self.workspace = workspace
        folder = records_folder(workspace)
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (secrets.token_hex(8) + ".json")
        # Held open until the command has stopped: that is what marks it as running.
        self._handle: int | None = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(self._handle, _encode(workspace, entries))
            os.fsync(self._handle)
        except BaseException:
            # A record that may be incomplete must not be acted on later.
            self.discard()
            raise

    def let_go(self) -> None:
        """The command has stopped; its record may now be acted on."""
        if self._handle is not None:
            try:
                os.close(self._handle)
            except OSError:
                pass
            self._handle = None

    def discard(self) -> None:
        """Take the record away unread: the command it was written for never relied on it."""
        self.let_go()
        try:
            self.path.unlink()
        except OSError:
            pass


def _encode(workspace: Path, entries: list[_Entry]) -> bytes:
    return json.dumps(
        {
            "workspace": str(workspace),
            "entries": [
                [str(path), identity[0], identity[1].hex(), directory]
                for path, identity, directory in entries
            ],
        }
    ).encode("utf-8")


def _decode(path: Path, workspace: Path) -> list[_Entry]:
    """The entries of a record, or none when it cannot be read as one of this project."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if os.path.normcase(record["workspace"]) != os.path.normcase(str(workspace)):
            return []
        entries: list[_Entry] = []
        for raw, volume, index, directory in record["entries"]:
            entry = Path(raw)
            # A record never names anything but an entry inside its project.
            if (
                type(volume) is not int
                or type(directory) is not bool
                or not entry.is_absolute()
                or entry == workspace
                or not entry.is_relative_to(workspace)
                or ".." in entry.parts
            ):
                return []
            entries.append((entry, (volume, bytes.fromhex(index)), directory))
        return entries
    except (OSError, ValueError, KeyError, TypeError):
        return []


def by_project(workspaces: list[Path], entries: list[_Entry]) -> dict[Path, list[_Entry]]:
    """Each entry under the project it was made in: the innermost one that contains it.

    One command can have a project and a folder inside it as two projects.
    An entry of the inner one belongs in the inner one's record only, where
    every command that relies on it looks.
    """
    result: dict[Path, list[_Entry]] = {workspace: [] for workspace in workspaces}
    for entry in entries:
        inside = [workspace for workspace in workspaces if entry[0].is_relative_to(workspace)]
        if inside:
            result[max(inside, key=lambda workspace: len(workspace.parts))].append(entry)
    return result


def recorded_files(workspace: Path) -> list[Path]:
    """The reserved files of ``workspace`` that are on record and still empty.

    For a reader that must not take them for the person's files, such as Git
    adding everything. A file that was written to since is the person's.
    """
    try:
        files = list(records_folder(workspace).iterdir())
    except (OSError, ConfigError):
        return []
    found: list[Path] = []
    for file in files:
        if file.suffix not in {".json", ".ended"}:
            continue
        for path, _, directory in _decode(file, workspace):
            try:
                if not directory and path not in found and path.stat().st_size == 0:
                    found.append(path)
            except OSError:
                continue
    return found


def _added_to_git(workspace: Path, files: list[Path]) -> set[Path]:
    """The files among ``files`` that Git has staged or tracks as the empty files they are.

    The person's Git works while a command runs, and "git add ." takes the
    empty reserved files along. From then on they are the person's: removing
    one would show up as a deletion in the repository. The repository may lie
    above the folder, as in a repository of several projects. An entry with
    content is something else: a tracked file that was deleted, whose name
    the reservation then took.
    """
    if not files:
        return set()
    try:
        from dulwich.repo import Repo

        repository = Repo.discover(str(workspace))
        try:
            top = Path(repository.path).resolve()
            index = repository.open_index()
            added = set()
            for file in files:
                try:
                    name = file.resolve().relative_to(top).as_posix().encode("utf-8")
                    if name in index and getattr(index[name], "size", 0) == 0:
                        added.add(file)
                except (ValueError, OSError, KeyError):
                    continue  # one file that cannot be judged does not decide the others
            return added
        finally:
            repository.close()
    except Exception:  # noqa: BLE001 - no repository here, or one that cannot be read
        return set()


def remove_unheld_reservations(workspace: Path) -> list[Path]:
    """Remove the recorded entries of ``workspace`` once no background command there runs.

    The caller holds the project's lease. Returns the paths that were removed.
    A record that is still held open means a command still relies on the
    entries: nothing is removed, and the records of the commands that have
    ended wait, renamed, for the last one.
    """
    try:
        folder = records_folder(workspace)
        files = sorted(folder.iterdir())
    except (OSError, ConfigError):
        return []
    ended: list[Path] = []
    running = False
    for file in files:
        if file.suffix == ".json":
            waiting = file.with_suffix(".ended")
            try:
                os.replace(file, waiting)
            except OSError:
                running = True
                continue
            file = waiting
        if file.suffix == ".ended":
            ended.append(file)
    if running:
        return []
    entries: list[_Entry] = []
    for file in ended:
        for entry in _decode(file, workspace):
            if entry not in entries:
                entries.append(entry)
    removed: list[Path] = []
    busy: list[_Entry] = []
    theirs = _added_to_git(workspace, [path for path, _, directory in entries if not directory])
    entries = [entry for entry in entries if entry[0] not in theirs]
    if entries:
        from ._windows_git_reservations import remove_reservations

        # Parents first, as they were made; removal takes them in reverse.
        entries.sort(key=lambda entry: len(entry[0].parts))
        removed, busy = remove_reservations(entries, announce=True)
    if busy:
        # Something still has an entry open, a process of the stopped command
        # on its way out for one. What is left is tried again by the next
        # command in the project.
        try:
            (folder / (secrets.token_hex(8) + ".ended")).write_bytes(
                _encode(workspace, sorted(busy, key=lambda entry: len(entry[0].parts)))
            )
        except OSError:
            return removed  # keep the records as they are
    for file in ended:
        try:
            file.unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass
    return removed
