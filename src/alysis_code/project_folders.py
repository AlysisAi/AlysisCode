"""Host-selected folders, scoped to one turn/tool call, never process environment.

The session root still owns Git, configuration and relative-path defaults. Extra
folders permit explicit filesystem paths without granting their common parent.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any

_scope: ContextVar[tuple[Path, tuple[Path, ...]] | None] = ContextVar(
    "project_folders", default=None
)


def _folder_paths(value: Any, *, canonical: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Project folders must be an array of at most 32 directories.")
    result = []
    for raw in value:
        if not isinstance(raw, str) or "\0" in raw or not Path(raw).is_absolute():
            raise ValueError("Project folders must be absolute directories.")
        path = Path(raw).resolve(strict=True)
        if not path.is_dir() or path == Path(path.anchor):
            raise ValueError("Choose a project directory, not a filesystem root.")
        if canonical and path != Path(raw):
            raise ValueError("Contained project folders must remain canonical directories.")
        if os.fspath(path) not in result:
            result.append(os.fspath(path))
    return result


def validate_folders(value: Any) -> list[str]:
    """Admit host-selected folders before granting them to a turn or sandbox."""
    paths = _folder_paths(value)
    if paths and Path.home().resolve() in map(Path, paths):
        raise ValueError("Choose a project directory, not a home root.")
    return paths


def validate_worker_folders(value: Any) -> list[str]:
    """Decode the host's already-admitted roots inside a contained worker.

    Host admission and the OS sandbox enforce home/private-path exclusions.
    The worker has no user profile environment and must not rediscover it.
    Reject retargeted aliases rather than resolving them into a different scope.
    These descriptors route tools; they cannot add OS filesystem grants.
    """
    return _folder_paths(value, canonical=True)


@contextmanager
def folder_scope(root: Path, folders):
    # Revalidate at dispatch: a directory replaced by a symlink cannot expand access.
    roots = tuple(Path(p) for p in folders if Path(p).is_dir() and Path(p).resolve() == Path(p))
    token = _scope.set((root.resolve(), roots))
    try:
        yield
    finally:
        _scope.reset(token)


def allowed_roots(root: Path) -> tuple[Path, ...]:
    root = root.resolve()
    current = _scope.get()
    extra = current[1] if current and current[0] == root else ()
    return tuple(dict.fromkeys((root, *extra)))


def containing_root(root: Path, target: Path) -> Path:
    target = target.resolve()
    for candidate in allowed_roots(root):
        if target.is_relative_to(candidate):
            return candidate
    raise ValueError(f"Path is outside the project folders: {target}")


def relative_path(root: Path, target: Path) -> str:
    containing_root(root, target)
    target = target.resolve()
    # Additional roots retain their absolute paths in contained workers, whose
    # primary root may instead be mounted at /workspace.
    if target.is_relative_to(root.resolve()):
        return os.path.relpath(target, root.resolve())
    return os.fspath(target)


def scoped_turn(function):
    @wraps(function)
    def run(session, *args, **kwargs):
        folders = getattr(session, "project_folders", ())
        with folder_scope(session.root, folders):
            return function(session, *args, **kwargs)

    return run


def turn_tools(session, tools):
    folders = tuple(getattr(session, "project_folders", ()))
    if not folders:
        return tools, ""
    result = {}
    for name, tool in tools.items():
        # Bind each call as well: executor threads do not inherit ContextVars.
        def run(arguments, original=tool.run):
            with folder_scope(session.root, folders):
                return original(arguments)

        result[name] = replace(tool, run=run)
    prompt = (
        "Current project folders (host-selected for this turn; replaces earlier folder lists):\n"
        + json.dumps(
            {"working_directory": os.fspath(session.root), "folders": folders}, ensure_ascii=False
        )
        + "\nRelative paths and Git operations use the working directory. Use absolute paths "
        "for other attached folders with filesystem/search tools or shell cwd. These folders "
        "share the current project's instructions and memory. Inspect applicable instruction "
        "files before editing another folder. Existing permission and approval rules still apply."
    )
    return result, prompt
