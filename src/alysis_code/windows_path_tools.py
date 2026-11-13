"""The programs on the person's PATH, for a command in the Windows sandbox.

On macOS and Linux a sandboxed command can run what is on the person's PATH
(``posix_runtime_paths``). On Windows it saw Node.js from Program Files, the
engine's own Git and a fallback Python. Everything else the person had
installed was "not recognized": their Python with its packages, uv, the
GitHub CLI, ripgrep. A project's own virtual environment could not start
either, because the Python it was made from was unreadable.

Every folder on the PATH is made readable, never writable, and goes on the
command's PATH in the person's order. What is left out is listed where it is
decided, in ``host_path_folders``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

from .branding import env_get
from .config import ConfigError

_PROGRAM_EXTENSIONS = {"", ".exe", ".com", ".bat", ".cmd", ".ps1"}


def _plain(path: Path) -> bool:
    """Whether the sandbox helper accepts ``path`` in a policy (its ``validate_path``).

    One path it refuses fails the whole command, so nothing is offered to it
    that it would not take.
    """
    text = str(path)
    return (
        len(text) >= 3
        and text[0].isascii()
        and text[0].isalpha()
        and text[1] == ":"
        and text[2] in "\\/"
        and "\0" not in text
        and ":" not in text[2:]
        and not any(part in {".", ".."} or part.endswith((".", " ")) for part in path.parts[1:])
    )


def _overlaps(path: Path, others: Iterable[Path]) -> bool:
    return any(path.is_relative_to(other) or other.is_relative_to(path) for other in others)


def _misleads(folder: Path) -> bool:
    """Whether putting ``folder`` on the PATH would hide a program that works.

    Two kinds of folder do. One holds links instead of programs (the Store's
    app aliases in ``WindowsApps``): the sandbox cannot start them. The other
    offers ``python`` without being a Python installation: the script folder
    of a virtual environment, or the shim folder of a version manager. Its
    ``python`` starts a Python somewhere else, which is unreadable, and it
    would be found before the person's real Python or the engine's fallback.
    """
    offers_python = False
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                if entry.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return True
                stem, extension = os.path.splitext(entry.name.lower())
                if stem in {"python", "python3"} and extension in _PROGRAM_EXTENSIONS:
                    offers_python = True
    except OSError:
        return True
    return offers_python and not (
        (folder / "python.exe").is_file() and (folder / "Lib" / "os.py").is_file()
    )


def private_and_runtime_folders() -> list[Path]:
    """What no PATH folder may contain or lie inside: the application's and the engine's own.

    A program in the engine's virtual environment would run with the engine's
    packages, and a folder holding the application's settings is private.
    """
    from . import config
    from .host_runtime_policy import desktop_runtime_roots
    from .local_memory import memory_home

    folders = [
        config.config_path().parent,
        config._data_dir(),
        memory_home(),
        Path(__file__).parent,
        *(Path(prefix) for prefix in (sys.prefix, sys.exec_prefix)),
        *desktop_runtime_roots(),
    ]
    desktop = env_get("ALYSIS_DESKTOP_USER_DATA")
    if desktop:
        folders.append(Path(desktop))
    try:
        control = json.loads(env_get("ALYSIS_DESKTOP_CONTROL_PATHS") or "[]")
    except ValueError:
        control = []  # the private-state check refuses the command with the reason
    if isinstance(control, list):
        folders.extend(Path(value) for value in control if isinstance(value, str) and value)
    resolved = []
    for folder in folders:
        try:
            resolved.append(folder.resolve())
        except (OSError, RuntimeError):
            resolved.append(folder)
    return resolved


def host_path_folders(
    path_value: str | None,
    *,
    readable: Iterable[Path],
    apart: Iterable[Path],
    program_files: Iterable[Path],
) -> tuple[list[Path], list[Path]]:
    """The folders to make readable and the folders to put on the command's PATH.

    ``readable`` is what the command can read already; a PATH folder inside
    it needs no grant. ``apart`` is what a grant may neither contain nor lie
    inside: the approved folders, the scratch folder, the Windows directory,
    and the application's and engine's own folders.

    A folder under Program Files is granted as its whole installation (the
    folder directly below Program Files), because a program there needs the
    files beside its ``bin`` or ``cmd`` folder and nothing personal is kept
    there. Any other folder is granted as itself.

    Left out: anything that is not an existing folder on a local drive, a
    folder reached through a link, a folder that contains the person's home
    or application data, and a folder that would mislead (``_misleads``).
    A folder with a problem is skipped; it never fails the command.
    """
    from .windows_sandbox import _local_path

    readable = [Path(path) for path in readable]
    apart = [Path(path) for path in apart]
    program_files = [Path(path) for path in program_files]
    home = Path.home()
    personal = [
        home,
        *(Path(value) for name in ("APPDATA", "LOCALAPPDATA") if (value := os.environ.get(name))),
    ]
    grants: list[Path] = []
    entries: list[Path] = []
    seen: set[str] = set()
    for raw in (path_value or "").split(os.pathsep):
        raw = raw.strip().strip('"')
        if not raw or not Path(raw).is_absolute():
            continue
        try:
            folder = _local_path(Path(raw))
        except (ConfigError, OSError, RuntimeError, ValueError):
            continue
        key = str(folder).casefold()
        if key in seen or not _plain(folder):
            continue
        seen.add(key)
        if any(folder.is_relative_to(root) for root in readable):
            entries.append(folder)
            continue
        grant = folder
        for root in program_files:
            if folder != root and folder.is_relative_to(root):
                grant = root / folder.relative_to(root).parts[0]
        if (
            grant == Path(grant.anchor)
            or _overlaps(grant, apart)
            or any(place.is_relative_to(grant) for place in personal)
            or _misleads(folder)
        ):
            continue
        if not any(grant.is_relative_to(existing) for existing in grants):
            grants = [existing for existing in grants if not existing.is_relative_to(grant)]
            grants.append(grant)
        entries.append(folder)
    return grants, entries
