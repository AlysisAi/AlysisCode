from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .command_text import command_text
from .git_ops import (
    GitOpsError,
    branch_exists,
    ensure_git_available,
    ensure_git_repo,
    ensure_runtime_artifact_excludes,
    status_porcelain,
)
from .git_safe import build_git_cmd


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str
    base_branch: str
    created_branch: bool
    reused_existing_worktree: bool


def _run_git_checked(
    root: Path,
    args: list[str],
    *,
    error_message: str,
) -> subprocess.CompletedProcess[str]:
    try:
        cp = subprocess.run(
            build_git_cmd(root, args),
            check=False,
            capture_output=True,
            **command_text(),
        )
    except OSError as e:
        raise GitOpsError("failed to run git") from e
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout).strip()
        raise GitOpsError(f"{error_message}: {detail or 'unknown error'}")
    return cp


def _is_valid_existing_worktree(
    *,
    worktree_repo_path: Path,
    expected_branch: str,
) -> tuple[bool, str]:
    try:
        inside = subprocess.run(
            build_git_cmd(worktree_repo_path, ["rev-parse", "--is-inside-work-tree"]),
            check=False,
            capture_output=True,
            **command_text(),
        )
    except OSError as e:
        raise GitOpsError("failed to run git") from e
    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        return False, "path exists but is not a valid git worktree"

    try:
        branch_cp = subprocess.run(
            build_git_cmd(worktree_repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]),
            check=False,
            capture_output=True,
            **command_text(),
        )
    except OSError as e:
        raise GitOpsError("failed to run git") from e
    if branch_cp.returncode != 0:
        return False, "failed to resolve worktree branch"
    current = branch_cp.stdout.strip()
    if current != expected_branch:
        return (
            False,
            f"worktree branch mismatch (expected {expected_branch}, got {current or '(empty)'})",
        )
    ensure_runtime_artifact_excludes(worktree_repo_path)
    status_lines = status_porcelain(worktree_repo_path)
    if status_lines:
        preview = ", ".join(status_lines[:5])
        if len(status_lines) > 5:
            preview += ", ..."
        return False, f"worktree has stale filesystem state ({preview})"
    return True, ""


def _cleanup_existing_worktree_path(*, root: Path, worktree_repo_path: Path) -> None:
    remove_cp = subprocess.run(
        build_git_cmd(root, ["worktree", "remove", str(worktree_repo_path), "--force"]),
        check=False,
        capture_output=True,
        **command_text(),
    )
    # Best-effort cleanup via git first; path cleanup below handles leftovers.
    _ = remove_cp
    if worktree_repo_path.exists():
        if worktree_repo_path.is_dir():
            shutil.rmtree(worktree_repo_path, ignore_errors=True)
        else:
            try:
                worktree_repo_path.unlink()
            except OSError:
                pass
    if worktree_repo_path.exists():
        raise GitOpsError(f"failed to cleanup existing worktree path {worktree_repo_path}")


def ensure_task_worktree(
    *,
    root: Path,
    worktree_repo_path: Path,
    branch: str,
    base_branch: str,
) -> WorktreeInfo:
    ensure_git_available()
    ensure_git_repo(root)
    worktree_repo_path = worktree_repo_path.resolve()
    worktree_repo_path.parent.mkdir(parents=True, exist_ok=True)

    if worktree_repo_path.exists():
        valid, _reason = _is_valid_existing_worktree(
            worktree_repo_path=worktree_repo_path,
            expected_branch=branch,
        )
        if valid:
            return WorktreeInfo(
                path=worktree_repo_path,
                branch=branch,
                base_branch=base_branch,
                created_branch=False,
                reused_existing_worktree=True,
            )
        _cleanup_existing_worktree_path(root=root, worktree_repo_path=worktree_repo_path)

    if branch_exists(root, branch):
        _run_git_checked(
            root,
            ["worktree", "add", str(worktree_repo_path), branch],
            error_message=f"failed to add worktree for branch {branch}",
        )
        return WorktreeInfo(
            path=worktree_repo_path,
            branch=branch,
            base_branch=base_branch,
            created_branch=False,
            reused_existing_worktree=False,
        )

    _run_git_checked(
        root,
        ["worktree", "add", "-b", branch, str(worktree_repo_path), base_branch],
        error_message=f"failed to create worktree branch {branch} from {base_branch}",
    )
    return WorktreeInfo(
        path=worktree_repo_path,
        branch=branch,
        base_branch=base_branch,
        created_branch=True,
        reused_existing_worktree=False,
    )


def remove_task_worktree(
    *,
    root: Path,
    worktree_repo_path: Path,
    force: bool = True,
) -> None:
    args = ["worktree", "remove", str(worktree_repo_path)]
    if force:
        args.append("--force")
    _run_git_checked(
        root,
        args,
        error_message=f"failed to remove worktree {worktree_repo_path}",
    )


def prune_worktrees(root: Path) -> None:
    _run_git_checked(
        root,
        ["worktree", "prune"],
        error_message="failed to prune worktrees",
    )
