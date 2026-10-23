from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .command_text import command_text
from .git_safe import build_git_cmd, build_git_process_env
from .runtime_artifacts import is_runtime_artifact_path, runtime_artifact_git_exclude_entries


class GitOpsError(RuntimeError):
    pass


DEFAULT_COMMIT_AUTHOR_NAME = "alysis-agent"
DEFAULT_COMMIT_AUTHOR_EMAIL = "alysis-agent@local"
_EMPTY_TREE_HASH = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_RUNTIME_ARTIFACT_EXCLUDE_BEGIN = "# BEGIN alysis runtime artifacts"
_RUNTIME_ARTIFACT_EXCLUDE_END = "# END alysis runtime artifacts"
_LEGACY_BROAD_TARGET_EXCLUDE_ENTRY = "target/"
_READ_ONLY_GIT_TIMEOUT_S = 5.0


def _git_identity_config(
    *,
    author_name: str = DEFAULT_COMMIT_AUTHOR_NAME,
    author_email: str = DEFAULT_COMMIT_AUTHOR_EMAIL,
) -> dict[str, str]:
    return {"user.name": author_name, "user.email": author_email}


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        raise GitOpsError("git is not available on PATH")


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


def head_commit(root: Path) -> str | None:
    cp = _run_git(root, ["rev-parse", "HEAD"], timeout_s=_READ_ONLY_GIT_TIMEOUT_S)
    if cp.returncode != 0:
        return None
    value = (cp.stdout or "").strip()
    return value or None


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


def ensure_clean_for_pr(root: Path) -> None:
    staged = _run_git_checked(
        root,
        ["diff", "--cached", "--name-only"],
        error_message="failed to inspect staged changes",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    ).stdout.strip()
    if staged:
        raise GitOpsError("working tree is not clean: staged changes exist")

    unstaged = _run_git_checked(
        root,
        ["diff", "--name-only"],
        error_message="failed to inspect unstaged changes",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    ).stdout.strip()
    if unstaged:
        raise GitOpsError("working tree is not clean: unstaged tracked changes exist")

    # Only untracked-and-not-ignored files appear with --exclude-standard.
    ensure_runtime_artifact_excludes(root)
    untracked = _run_git_checked(
        root,
        ["ls-files", "--others", "--exclude-standard"],
        error_message="failed to inspect untracked files",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    ).stdout.strip()
    if untracked:
        raise GitOpsError(
            "working tree has untracked files not ignored by git; please clean them first"
        )


def branch_exists(root: Path, branch: str) -> bool:
    cp = _run_git(
        root,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return cp.returncode == 0


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


def reset_hard(root: Path, *, target: str = "HEAD") -> None:
    _run_git_checked(
        root,
        ["reset", "--hard", target],
        error_message=f"failed to reset worktree to {target}",
    )


def clean_untracked(root: Path) -> None:
    _run_git_checked(
        root,
        ["clean", "-fd"],
        error_message="failed to clean untracked files",
    )


def checkout_branch(root: Path, branch: str, *, base_branch: str) -> None:
    if branch_exists(root, branch):
        _run_git_checked(root, ["checkout", branch], error_message=f"failed to checkout {branch}")
        return
    _run_git_checked(
        root,
        ["checkout", "-b", branch, base_branch],
        error_message=f"failed to create branch {branch}",
    )


def stage_all(root: Path) -> None:
    _run_git_checked(root, ["add", "-A"], error_message="failed to stage changes")


def staged_files(root: Path) -> list[str]:
    cp = _run_git_checked(
        root,
        ["diff", "--cached", "--name-only"],
        error_message="failed to list staged files",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def _staged_runtime_artifact_paths(
    root: Path,
    *,
    current_paths: list[str] | None = None,
) -> list[str]:
    staged = current_paths if current_paths is not None else staged_files(root)
    return [path for path in staged if is_runtime_artifact_path(path, root=root)]


def _normalize_rel_prefix(value: str) -> str:
    cleaned = value.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def _matches_prefix(rel_path: str, prefix: str) -> bool:
    return rel_path == prefix or rel_path.startswith(prefix + "/")


def unstage_staged_prefixes(root: Path, prefixes: list[str]) -> list[str]:
    current = staged_files(root)
    normalized = [_normalize_rel_prefix(p) for p in prefixes if p.strip()]
    if not normalized:
        return []

    to_unstage: list[str] = []
    for path in current:
        rel = _normalize_rel_prefix(path)
        if any(_matches_prefix(rel, pref) for pref in normalized):
            to_unstage.append(path)

    if not to_unstage:
        return []

    _run_git_checked(
        root,
        ["reset", "HEAD", "--", *to_unstage],
        error_message="failed to unstage protected paths",
    )
    return to_unstage


def unstage_staged_paths(root: Path, paths: list[str]) -> list[str]:
    normalized = {_normalize_rel_prefix(path) for path in paths if path.strip()}
    if not normalized:
        return []

    current = staged_files(root)
    to_unstage = [path for path in current if _normalize_rel_prefix(path) in normalized]
    if not to_unstage:
        return []

    _run_git_checked(
        root,
        ["reset", "HEAD", "--", *to_unstage],
        error_message="failed to unstage filtered files",
    )
    return to_unstage


def unstage_staged_runtime_artifacts(
    root: Path,
    *,
    current_paths: list[str] | None = None,
) -> list[str]:
    return unstage_staged_paths(
        root,
        _staged_runtime_artifact_paths(root, current_paths=current_paths),
    )


def ensure_not_staged_prefixes(root: Path, prefixes: list[str]) -> None:
    current = staged_files(root)
    normalized = [_normalize_rel_prefix(p) for p in prefixes if p.strip()]
    for path in current:
        rel = _normalize_rel_prefix(path)
        for pref in normalized:
            if _matches_prefix(rel, pref):
                raise GitOpsError(f"staged file under protected path: {path}")


def ensure_not_staged_paths(root: Path, paths: list[str]) -> None:
    current = staged_files(root)
    normalized = {_normalize_rel_prefix(path) for path in paths if path.strip()}
    for path in current:
        if _normalize_rel_prefix(path) in normalized:
            raise GitOpsError(f"staged filtered file: {path}")


def ensure_not_staged_runtime_artifacts(
    root: Path,
    *,
    current_paths: list[str] | None = None,
) -> None:
    current = _staged_runtime_artifact_paths(root, current_paths=current_paths)
    if current:
        preview = ", ".join(current[:20])
        if len(current) > 20:
            preview += ", ..."
        raise GitOpsError(f"staged runtime artifact path: {preview}")


def commit_all(
    root: Path,
    *,
    message: str,
    author_name: str = DEFAULT_COMMIT_AUTHOR_NAME,
    author_email: str = DEFAULT_COMMIT_AUTHOR_EMAIL,
) -> str:
    _run_git_checked(
        root,
        ["commit", "-m", message],
        extra_config=_git_identity_config(
            author_name=author_name,
            author_email=author_email,
        ),
        error_message="failed to create commit",
    )
    cp = _run_git_checked(
        root,
        ["rev-parse", "HEAD"],
        error_message="failed to resolve commit hash",
    )
    return cp.stdout.strip()


def format_patch_stdout(root: Path, *, base_branch: str) -> str:
    cp = _run_git_checked(
        root,
        ["format-patch", f"{base_branch}..HEAD", "--stdout"],
        error_message="failed to generate patch",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return cp.stdout


def diff_text_since(
    root: Path,
    *,
    before_commit: str | None,
    after_commit: str | None,
) -> str:
    if not after_commit or after_commit == before_commit:
        return ""
    base = before_commit or _EMPTY_TREE_HASH
    cp = _run_git_checked(
        root,
        ["diff", "--binary", "-M", base, after_commit],
        error_message="failed to generate committed diff",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return cp.stdout


def changed_files_between(root: Path, *, revspec: str) -> list[str]:
    cp = _run_git_checked(
        root,
        ["diff", "--name-only", revspec],
        error_message=f"failed to list changed files for {revspec}",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def changed_files_since(
    root: Path,
    *,
    before_commit: str | None,
    after_commit: str | None,
) -> list[str]:
    if not after_commit or after_commit == before_commit:
        return []
    base = before_commit or _EMPTY_TREE_HASH
    cp = _run_git_checked(
        root,
        ["diff", "--name-only", base, after_commit],
        error_message="failed to list committed changed files",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def added_files_since(
    root: Path,
    *,
    before_commit: str | None,
    after_commit: str | None,
) -> list[str]:
    if not after_commit or after_commit == before_commit:
        return []
    base = before_commit or _EMPTY_TREE_HASH
    cp = _run_git_checked(
        root,
        ["diff", "--diff-filter=A", "--name-only", base, after_commit],
        error_message="failed to list added committed files",
        timeout_s=_READ_ONLY_GIT_TIMEOUT_S,
    )
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def reset_mixed(root: Path, *, target: str = "HEAD") -> None:
    _run_git_checked(
        root,
        ["reset", "--mixed", target],
        error_message=f"failed to reset worktree to {target} while preserving changes",
    )


def merge_no_ff(root: Path, *, base_branch: str, task_branch: str, message: str) -> str:
    _run_git_checked(
        root,
        ["checkout", base_branch],
        error_message=f"failed to checkout base branch {base_branch}",
    )
    _run_git_checked(
        root,
        ["merge", "--no-ff", task_branch, "-m", message],
        extra_config=_git_identity_config(),
        error_message=f"failed to merge branch {task_branch}",
    )
    cp = _run_git_checked(
        root,
        ["rev-parse", "HEAD"],
        error_message="failed to resolve merge commit hash",
    )
    return cp.stdout.strip()


def delete_branch(root: Path, branch: str) -> None:
    _run_git_checked(
        root,
        ["branch", "-d", branch],
        error_message=f"failed to delete branch {branch}",
    )


def generate_task_branch_name(task_id: str, title: str) -> str:
    tid = task_id.strip().lower() or "task"
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower())
    slug = slug.strip("-")
    if not slug:
        slug = "task"
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")
    return f"feat/{tid}-{slug}"
