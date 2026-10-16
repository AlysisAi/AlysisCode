"""Canonical project data locations and read-only artifact resolution."""

from __future__ import annotations

import stat
from pathlib import Path

PROJECT_DATA_NAME = ".alysis"


class ProjectDataError(RuntimeError):
    pass


def project_data_dir(workspace_root: Path) -> Path:
    """Return the data directory, rejecting files and symlinks without changing disk."""
    current = workspace_root.expanduser().resolve() / PROJECT_DATA_NAME
    try:
        info = current.lstat()
    except FileNotFoundError:
        return current
    if not stat.S_ISDIR(info.st_mode):
        raise ProjectDataError(
            f"Project data path must be a real directory, not a symlink or file: {current}"
        )
    return current


def project_artifact_path(workspace_root: Path, value: str | Path) -> Path:
    """Resolve a stored locator without moving data or rewriting records.

    Absolute paths outside this workspace retain their identity. Locators inside
    the canonical project directory must stay within it after symlink resolution.
    """
    expanded = workspace_root.expanduser()
    root = expanded.resolve()
    path = Path(value)
    relative = path
    if path.is_absolute():
        for base in (root, expanded.absolute()):
            try:
                relative = path.relative_to(base)
                break
            except ValueError:
                continue
        else:
            return path
    candidate = path if path.is_absolute() else root / path
    if relative.parts and relative.parts[0] == PROJECT_DATA_NAME:
        current = project_data_dir(root)
        if not candidate.resolve().is_relative_to(current.resolve()):
            raise ProjectDataError(f"Project artifact path escapes project data: {value}")
    return candidate
