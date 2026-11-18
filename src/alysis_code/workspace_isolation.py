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
