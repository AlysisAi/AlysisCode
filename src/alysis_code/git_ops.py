from __future__ import annotations

import subprocess
from pathlib import Path

from .command_text import command_text
from .git_safe import build_git_cmd, build_git_process_env
from .runtime_artifacts import runtime_artifact_git_exclude_entries


class GitOpsError(RuntimeError):
    pass


_RUNTIME_ARTIFACT_EXCLUDE_BEGIN = "# BEGIN alysis runtime artifacts"
_RUNTIME_ARTIFACT_EXCLUDE_END = "# END alysis runtime artifacts"
_LEGACY_BROAD_TARGET_EXCLUDE_ENTRY = "target/"
_READ_ONLY_GIT_TIMEOUT_S = 5.0


def _run_git(
    root: Path,
    args: list[str],
    *,
    input_text: str | None = None,
    extra_config: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = build_git_cmd(root, args, extra_config=extra_config)
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            **command_text(),
            input=input_text,
            env=build_git_process_env(),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr="git command timed out",
        )
    except OSError as e:
        raise GitOpsError("failed to run git") from e


def _run_git_checked(
    root: Path,
    args: list[str],
    *,
    input_text: str | None = None,
    extra_config: dict[str, str] | None = None,
    error_message: str,
    timeout_s: float | None = None,
) -> subprocess.CompletedProcess[str]:
    cp = _run_git(
        root,
        args,
        input_text=input_text,
        extra_config=extra_config,
        timeout_s=timeout_s,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout).strip()
        raise GitOpsError(f"{error_message}: {detail or 'unknown error'}")
    return cp


def ensure_git_repo(root: Path) -> None:
    _run_git_checked(
        root,
        ["rev-parse", "--git-dir"],
        error_message="not a git repository",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )


def current_branch(root: Path) -> str:
    symbolic = _run_git(
        root, ["symbolic-ref", "--short", "HEAD"], timeout_s=_READ_ONLY_GIT_TIMEOUT_S
    )
    if symbolic.returncode == 0:
        branch = symbolic.stdout.strip()
        if branch:
            return branch

    cp = _run_git_checked(
        root,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        error_message="failed to determine current branch",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return cp.stdout.strip()


def has_head_commit(root: Path) -> bool:
    cp = _run_git(root, ["rev-parse", "--verify", "HEAD"], timeout_s=_READ_ONLY_GIT_TIMEOUT_S)
    return cp.returncode == 0


def git_path(root: Path, pathspec: str) -> Path:
    cp = _run_git_checked(
        root,
        ["rev-parse", "--git-path", pathspec],
        error_message=f"failed to resolve git path for {pathspec}",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    resolved = (cp.stdout or "").strip()
    if not resolved:
        raise GitOpsError(f"failed to resolve git path for {pathspec}: empty result")
    path = Path(resolved)
    if not path.is_absolute():
        path = root / path
    return path


def ensure_info_exclude_entries(root: Path, entries: list[str]) -> Path:
    exclude_path = git_path(root, "info/exclude")
    exclude_path.parent.mkdir(parents=True, exist_ok=True)

    normalized_entries = [entry.strip() for entry in entries if entry.strip()]
    if not normalized_entries:
        return exclude_path

    if exclude_path.exists():
        raw_lines = exclude_path.read_text(encoding="utf-8").splitlines()
    else:
        raw_lines = []

    existing = {line.strip() for line in raw_lines if line.strip()}
    to_append = [entry for entry in normalized_entries if entry not in existing]
    if not to_append:
        return exclude_path

    with exclude_path.open("a", encoding="utf-8", newline="\n") as fh:
        for entry in to_append:
            fh.write(entry + "\n")
    return exclude_path


def ensure_runtime_artifact_excludes(root: Path) -> Path:
    exclude_path = git_path(root, "info/exclude")
    exclude_path.parent.mkdir(parents=True, exist_ok=True)

    desired_entries = list(runtime_artifact_git_exclude_entries(root))
    if exclude_path.exists():
        raw_lines = exclude_path.read_text(encoding="utf-8").splitlines()
    else:
        raw_lines = []

    preserved_lines: list[str] = []
    inside_managed_block = False
    for line in raw_lines:
        stripped = line.strip()
        if stripped == _RUNTIME_ARTIFACT_EXCLUDE_BEGIN:
            inside_managed_block = True
            continue
        if stripped == _RUNTIME_ARTIFACT_EXCLUDE_END:
            inside_managed_block = False
            continue
        if inside_managed_block:
            continue
        if stripped == _LEGACY_BROAD_TARGET_EXCLUDE_ENTRY:
            continue
        preserved_lines.append(line)

    while preserved_lines and not preserved_lines[-1].strip():
        preserved_lines.pop()
    if preserved_lines:
        preserved_lines.append("")
    preserved_lines.extend(
        [
            _RUNTIME_ARTIFACT_EXCLUDE_BEGIN,
            *desired_entries,
            _RUNTIME_ARTIFACT_EXCLUDE_END,
        ]
    )
    # LF on every platform: Git reads a CRLF blank line here as an empty pattern that
    # matches every path ending in "/", so `git check-ignore --no-index dir/` (the @ file
    # index) reported every folder as ignored. Rewriting with LF also repairs old files.
    exclude_path.write_text("\n".join(preserved_lines) + "\n", encoding="utf-8", newline="\n")
    return exclude_path


def status_porcelain(root: Path) -> list[str]:
    cp = _run_git_checked(
        root,
        ["status", "--porcelain=v1", "--untracked-files=normal"],
        error_message="failed to inspect worktree status",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return [line.rstrip() for line in cp.stdout.splitlines() if line.strip()]


def untracked_files(root: Path) -> list[str]:
    ensure_runtime_artifact_excludes(root)
    cp = _run_git_checked(
        root,
        ["ls-files", "--others", "--exclude-standard"],
        error_message="failed to inspect untracked files",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def staged_files(root: Path) -> list[str]:
    cp = _run_git_checked(
        root,
        ["diff", "--cached", "--name-only"],
        error_message="failed to list staged files",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]
