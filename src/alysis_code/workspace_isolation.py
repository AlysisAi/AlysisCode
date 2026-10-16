from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from .command_text import command_text
from .git_ops import (
    GitOpsError,
    clean_untracked,
    ensure_runtime_artifact_excludes,
    reset_hard,
    status_porcelain,
)
from .git_safe import build_git_cmd, build_git_process_env


def _run_git_checked(
    root: Path,
    args: list[str],
    *,
    error_message: str,
    extra_config: dict[str, str] | None = None,
    disable_filters: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        cmd = build_git_cmd(root, args, extra_config=extra_config, disable_filters=disable_filters)
        cp = subprocess.run(
            cmd,
            env=build_git_process_env() if disable_filters else None,
            check=False,
            capture_output=True,
            **command_text(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitOpsError("failed to run git") from e
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout).strip()
        raise GitOpsError(f"{error_message}: {detail or 'unknown error'}")
    return cp


def _inspect_existing_git_workspace(
    *,
    worktree_path: Path,
    expected_branch: str,
) -> tuple[bool, bool, str]:
    try:
        inside = subprocess.run(
            build_git_cmd(worktree_path, ["rev-parse", "--is-inside-work-tree"]),
            check=False,
            capture_output=True,
            **command_text(),
        )
    except OSError as e:
        raise GitOpsError("failed to run git") from e
    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        return False, False, "path exists but is not a valid git worktree"

    try:
        branch_cp = subprocess.run(
            build_git_cmd(worktree_path, ["rev-parse", "--abbrev-ref", "HEAD"]),
            check=False,
            capture_output=True,
            **command_text(),
        )
    except OSError as e:
        raise GitOpsError("failed to run git") from e
    if branch_cp.returncode != 0:
        return False, False, "failed to resolve worktree branch"
    current_branch = branch_cp.stdout.strip()
    if current_branch != expected_branch:
        return (
            False,
            False,
            f"worktree branch mismatch (expected {expected_branch}, got {current_branch or '(empty)'})",
        )

    ensure_runtime_artifact_excludes(worktree_path)
    status_lines = status_porcelain(worktree_path)
    if status_lines:
        preview = ", ".join(status_lines[:5])
        if len(status_lines) > 5:
            preview += ", ..."
        return True, False, f"worktree has stale filesystem state ({preview})"
    return True, True, ""


def _sanitize_existing_git_workspace(*, worktree_path: Path) -> None:
    ensure_runtime_artifact_excludes(worktree_path)
    reset_hard(worktree_path)
    clean_untracked(worktree_path)
    status_lines = status_porcelain(worktree_path)
    if status_lines:
        preview = ", ".join(status_lines[:5])
        if len(status_lines) > 5:
            preview += ", ..."
        raise GitOpsError(f"failed to sanitize worktree state ({preview})")


def _reset_git_workspace_to_target(*, worktree_path: Path, target: str) -> None:
    ensure_runtime_artifact_excludes(worktree_path)
    reset_hard(worktree_path, target=target)
    clean_untracked(worktree_path)
    status_lines = status_porcelain(worktree_path)
    if status_lines:
        preview = ", ".join(status_lines[:5])
        if len(status_lines) > 5:
            preview += ", ..."
        raise GitOpsError(f"failed to reset workspace to {target} ({preview})")


def _cleanup_workspace_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, onerror=_retry_readonly_removal)
    else:
        try:
            path.unlink()
        except OSError:
            pass
    if path.exists():
        raise GitOpsError(f"failed to cleanup workspace path {path}")


def _retry_readonly_removal(
    remove: Any,
    path: str,
    exc_info: tuple[type[BaseException], BaseException, Any],
) -> None:
    """Retry a Windows removal after clearing Git's read-only file attribute."""

    error = exc_info[1]
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    remove(path)
