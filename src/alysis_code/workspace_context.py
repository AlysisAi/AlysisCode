from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .command_text import command_text
from .git_safe import build_git_process_env

_HEAD_REF_PREFIX = "ref:"
_HEADS_PREFIX = "refs/heads/"
_OID_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_GIT_PROBE_TIMEOUT_S = 2.0

WORKSPACE_KIND_GIT_REPO = "git_repo"
WORKSPACE_KIND_GIT_REPO_NO_HEAD = "git_repo_no_head"
WORKSPACE_KIND_PLAIN_DIR = "plain_dir"


class WorkspaceContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceContext:
    input_path: Path
    focus_path: Path
    workspace_root: Path
    git_root: Path | None
    focus_relpath: str
    workspace_kind: str
    has_head_commit: bool
    current_branch: str | None


@dataclass(frozen=True)
class _GitResolution:
    workspace_root: Path
    git_root: Path
    has_head_commit: bool
    current_branch: str | None


def resolve_workspace_context(
    path: Path,
    *,
    allow_subprocess: bool = True,
) -> WorkspaceContext:
    """Resolve workspace metadata for an existing directory without mutating the filesystem."""
    input_path = _resolve_input_path(path)
    git_resolution = _resolve_git_resolution(
        input_path,
        allow_subprocess=allow_subprocess,
    )
    if git_resolution is None:
        return WorkspaceContext(
            input_path=input_path,
            focus_path=input_path,
            workspace_root=input_path,
            git_root=None,
            focus_relpath=".",
            workspace_kind=WORKSPACE_KIND_PLAIN_DIR,
            has_head_commit=False,
            current_branch=None,
        )

    workspace_root = git_resolution.workspace_root
    focus_relpath = "."
    if input_path != workspace_root:
        focus_relpath = input_path.relative_to(workspace_root).as_posix()
    return WorkspaceContext(
        input_path=input_path,
        focus_path=input_path,
        workspace_root=workspace_root,
        git_root=git_resolution.git_root,
        focus_relpath=focus_relpath,
        workspace_kind=(
            WORKSPACE_KIND_GIT_REPO
            if git_resolution.has_head_commit
            else WORKSPACE_KIND_GIT_REPO_NO_HEAD
        ),
        has_head_commit=git_resolution.has_head_commit,
        current_branch=git_resolution.current_branch,
    )


def _resolve_input_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise WorkspaceContextError(f"Workspace path does not exist: {resolved}")
    if not resolved.is_dir():
        raise WorkspaceContextError(f"Workspace path is not a directory: {resolved}")
    return resolved


def _resolve_git_resolution(
    path: Path,
    *,
    allow_subprocess: bool = True,
) -> _GitResolution | None:
    fs_root = _find_git_root_from_filesystem(path)
    if fs_root is None:
        return None
    if not allow_subprocess:
        current_branch, has_head_commit = _filesystem_head_state(fs_root)
        return _GitResolution(
            workspace_root=fs_root,
            git_root=fs_root,
            has_head_commit=has_head_commit,
            current_branch=current_branch,
        )
    git_root = _git_show_toplevel(path)
    if git_root is not None:
        return _git_resolution_from_root(git_root)
    return _git_resolution_from_root(fs_root)


def _git_show_toplevel(path: Path) -> Path | None:
    cp = _run_git(path, ["rev-parse", "--show-toplevel"])
    if cp is None or cp.returncode != 0:
        return None
    value = (cp.stdout or "").strip()
    if not value:
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


def _run_git(path: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", os.fspath(path), *args],
            check=False,
            capture_output=True,
            **command_text(),
            env=_git_probe_env(),
            timeout=_GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_probe_env() -> dict[str, str]:
    return build_git_process_env()


def _git_resolution_from_root(workspace_root: Path) -> _GitResolution:
    current_branch = _git_current_branch(workspace_root)
    has_head_commit = _git_has_head_commit(workspace_root)
    if current_branch is None and not has_head_commit:
        current_branch, has_head_commit = _filesystem_head_state(workspace_root)
    return _GitResolution(
        workspace_root=workspace_root,
        git_root=workspace_root,
        has_head_commit=has_head_commit,
        current_branch=current_branch,
    )


def _git_current_branch(workspace_root: Path) -> str | None:
    symbolic = _run_git(workspace_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if symbolic is not None and symbolic.returncode == 0:
        branch = (symbolic.stdout or "").strip()
        if branch:
            return branch

    cp = _run_git(workspace_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if cp is None or cp.returncode != 0:
        return None
    branch = (cp.stdout or "").strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _git_has_head_commit(workspace_root: Path) -> bool:
    cp = _run_git(workspace_root, ["rev-parse", "--verify", "HEAD"])
    return cp is not None and cp.returncode == 0


def _find_git_root_from_filesystem(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        if marker.exists() and _is_valid_filesystem_git_root(candidate):
            return candidate
    return None


def _is_valid_filesystem_git_root(workspace_root: Path) -> bool:
    git_dir = _resolve_git_dir(workspace_root)
    if git_dir is None:
        return False
    return (git_dir / "HEAD").is_file()


def _filesystem_head_state(workspace_root: Path) -> tuple[str | None, bool]:
    git_dir = _resolve_git_dir(workspace_root)
    if git_dir is None:
        return None, False
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, False
    if not head:
        return None, False
    if head.startswith(_HEAD_REF_PREFIX):
        ref_name = head[len(_HEAD_REF_PREFIX) :].strip()
        branch = _branch_name_from_ref(ref_name)
        return branch, _git_ref_exists(git_dir, ref_name)
    return None, bool(_OID_RE.fullmatch(head))


def _resolve_git_dir(workspace_root: Path) -> Path | None:
    marker = workspace_root / ".git"
    if marker.is_dir():
        return marker.resolve()
    if not marker.is_file():
        return None
    try:
        first_line = marker.read_text(encoding="utf-8").splitlines()[0].strip()
    except (IndexError, OSError, UnicodeDecodeError):
        return None
    if not first_line.lower().startswith("gitdir:"):
        return None
    value = first_line.split(":", 1)[1].strip()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def _git_ref_exists(git_dir: Path, ref_name: str) -> bool:
    for base_dir in _git_state_dirs(git_dir):
        ref_path = base_dir / ref_name
        try:
            value = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if _OID_RE.fullmatch(value):
            return True

        packed_refs = base_dir / "packed-refs"
        try:
            for line in packed_refs.read_text(encoding="utf-8").splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#") or entry.startswith("^"):
                    continue
                oid, sep, packed_ref = entry.partition(" ")
                if sep and packed_ref == ref_name and _OID_RE.fullmatch(oid):
                    return True
        except OSError:
            continue
    return False


def _git_state_dirs(git_dir: Path) -> list[Path]:
    dirs = [git_dir]
    commondir_path = git_dir / "commondir"
    try:
        commondir_value = commondir_path.read_text(encoding="utf-8").strip()
    except OSError:
        commondir_value = ""
    if not commondir_value:
        return dirs

    candidate = Path(commondir_value)
    if not candidate.is_absolute():
        candidate = git_dir / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return dirs
    if resolved not in dirs:
        dirs.append(resolved)
    return dirs


def _branch_name_from_ref(ref_name: str) -> str | None:
    ref_name = ref_name.strip()
    if not ref_name:
        return None
    if ref_name.startswith(_HEADS_PREFIX):
        branch = ref_name[len(_HEADS_PREFIX) :].strip()
        return branch or None
    return ref_name
