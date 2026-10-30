"""Workspace authority captured by a trusted launcher, shared by shell entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .project_folders import allowed_roots
from .sandbox_settings import ShellSandboxSettings


def runner_workspace_roots(runner: Any) -> tuple[str, ...]:
    settings = getattr(runner, "execution_settings", None)
    if not isinstance(settings, ShellSandboxSettings):
        settings = getattr(runner, "settings", None)
    if isinstance(settings, ShellSandboxSettings):
        return (*settings.additional_workspace_roots, *settings.additional_read_roots)
    values = getattr(runner, "additional_workspace_roots", ())
    reads = getattr(runner, "additional_read_roots", ())
    values = (*values, *reads) if isinstance(values, tuple) and isinstance(reads, tuple) else ()
    return values if all(isinstance(v, str) for v in values) else ()


def contains_workspace_path(root: Path, path: Path, additional: tuple[str, ...] = ()) -> bool:
    resolved = path.resolve()
    return any(
        resolved.is_relative_to(candidate.resolve())
        for candidate in (*allowed_roots(root), *(Path(p) for p in additional))
    )
