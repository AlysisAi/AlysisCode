from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..command_text import command_text
from ..diff_paths import iter_patch_paths
from .fs import (
    FilePrecondition,
    FsError,
    StaleFileError,
    assert_file_precondition,
    capture_file_precondition,
)


class GitError(RuntimeError):
    pass


class GitStaleFileError(GitError, StaleFileError):
    """A patch precondition conflict recognizable by both tool error contracts."""

    def __init__(self, path: str) -> None:
        StaleFileError.__init__(self, path)


_GIT_HISTORY_DEFAULT_LOG_LIMIT = 10
_GIT_HISTORY_MAX_LOG_LIMIT = 50
_GIT_HISTORY_LOG_BODY_MAX_CHARS = 240
_GIT_HISTORY_SHOW_BODY_MAX_CHARS = 1200
_GIT_HISTORY_SHOW_PATCH_MAX_CHARS = 6000
_GIT_HISTORY_BLAME_MAX_LINES = 200
_GIT_HISTORY_BLAME_LINE_MAX_CHARS = 240
_VALID_UNIFIED_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?$")
_PATCH_LOCKS_GUARD = threading.Lock()
_PATCH_LOCKS: dict[str, threading.RLock] = {}


def _run_git(
    root: Path,
    args: list[str],
    *,
    input_s: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_s,
            check=False,
            capture_output=True,
            **command_text(),
            env=env,
        )
    except OSError as e:
        raise GitError("git not available") from e


def _normalize_git_error(stderr: str, *, fallback: str) -> str:
    message = stderr.strip()
    lowered = message.lower()
    if "not a git repository" in lowered:
        return "not a git repository"
    if "unknown revision or path not in the working tree" in lowered:
        return "invalid revision or path"
    if "bad revision" in lowered or "bad object" in lowered:
        return "invalid revision"
    if "no such path" in lowered or "no such file" in lowered:
        return "invalid path"
    return message or fallback


def _run_git_checked(
    *,
    root: Path,
    args: list[str],
    input_s: str | None = None,
    env: dict[str, str] | None = None,
    fallback: str,
) -> subprocess.CompletedProcess[str]:
    cp = _run_git(root, args, input_s=input_s, env=env)
    if cp.returncode != 0:
        raise GitError(_normalize_git_error(cp.stderr, fallback=fallback))
    return cp


def _normalize_patch_text(patch: str) -> str:
    normalized = patch.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _validate_patch_shape(patch_text: str) -> None:
    if not patch_text.strip():
        raise GitError("malformed patch: patch is empty")

    patch_paths = iter_patch_paths(patch_text)
    if not patch_paths:
        raise GitError("malformed patch: no file paths found in patch headers")

    lines = patch_text.splitlines()
    has_old_header = any(line.startswith("--- ") for line in lines)
    has_new_header = any(line.startswith("+++ ") for line in lines)
    if has_old_header != has_new_header:
        raise GitError("malformed patch: missing paired ---/+++ file headers")

    invalid_hunk_header = next(
        (
            line
            for line in lines
            if line.startswith("@@") and not _VALID_UNIFIED_HUNK_RE.match(line)
        ),
        None,
    )
    if invalid_hunk_header is None:
        return
    if invalid_hunk_header.startswith("@@ ..."):
        raise GitError("malformed patch: placeholder hunk header '@@ ...' is not allowed")
    raise GitError(f"malformed patch: invalid hunk header: {invalid_hunk_header}")


def _patch_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(os.fspath(root.resolve()))
    with _PATCH_LOCKS_GUARD:
        lock = _PATCH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATCH_LOCKS[key] = lock
        return lock


def _capture_patch_preconditions(root: Path, patch_text: str) -> tuple[FilePrecondition, ...]:
    preconditions: list[FilePrecondition] = []
    for raw_path in iter_patch_paths(patch_text):
        normalized_path = _resolve_git_path(root, raw_path)
        try:
            preconditions.append(capture_file_precondition(root=root, path=normalized_path))
        except StaleFileError as exc:
            raise GitStaleFileError(exc.path) from exc
        except FsError as exc:
            raise GitError(f"Cannot safely prepare patch path {normalized_path}: {exc}") from exc
    return tuple(preconditions)


def _assert_patch_preconditions(
    root: Path,
    preconditions: tuple[FilePrecondition, ...],
) -> None:
    for precondition in preconditions:
        try:
            assert_file_precondition(root=root, precondition=precondition)
        except StaleFileError as exc:
            raise GitStaleFileError(exc.path) from exc


def _resolve_git_path(root: Path, user_path: str) -> str:
    root_abs = root.resolve()
    target = (root_abs / user_path).resolve()
    try:
        rel = target.relative_to(root_abs)
    except ValueError as e:
        raise GitError(f"Path escapes root: {user_path}") from e
    return os.fspath(rel).replace("\\", "/")


def _clip_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized, False
    return normalized[:max_chars].rstrip() + "...(truncated)", True


def _parse_log_records(stdout: str) -> list[dict[str, str]]:
    commits: list[dict[str, str]] = []
    for raw_record in stdout.split("\x1e"):
        record = raw_record.strip("\x1e\r\n")
        if not record:
            continue
        fields = record.split("\x1f", 6)
        if len(fields) != 7:
            continue
        commit, short_commit, author_name, author_email, authored_at, subject, body = fields
        body_excerpt, body_truncated = _clip_text(body, max_chars=_GIT_HISTORY_LOG_BODY_MAX_CHARS)
        commits.append(
            {
                "commit": commit,
                "short_commit": short_commit,
                "author_name": author_name,
                "author_email": author_email,
                "authored_at": authored_at,
                "subject": subject,
                "body_excerpt": body_excerpt,
                "body_truncated": body_truncated,
            }
        )
    return commits


def _git_review_args(
    root: Path, args: list[str], *, staged: bool = False, path: str | None = None
) -> list[str]:
    # Native review cannot execute arbitrary repository programs. Turning off a
    # clean filter also changes Git's comparison semantics, so refuse affected
    # worktree reads instead of inventing modifications. Index-only diffs are safe.
    filters = _run_git(
        root,
        [
            "config",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|process|required)$",
        ],
    )
    if filters.returncode not in {0, 1}:
        raise GitError("Could not inspect Git filters before reading repository changes.")
    prefix = ["--no-optional-locks", "-c", "core.fsmonitor=false"]
    names = sorted(set(filters.stdout.split("\0")) - {""})
    tracked_paths = ""
    if not staged:
        tracked = _run_git_checked(
            root=root,
            args=[
                *prefix,
                "--literal-pathspecs",
                "ls-files",
                "--stage",
                "-z",
                *(["--", path] if path is not None else []),
            ],
            fallback="Could not inspect tracked paths before Git review.",
        )
        entries = [entry for entry in tracked.stdout.split("\0") if entry]
        if any(entry.startswith("160000 ") for entry in entries):
            raise GitError(
                "git_submodule_review_required: This review includes submodules, which can "
                "run their own Git helpers. Use shell_run with git status or git diff through "
                "the configured command runner. Staged git_diff and other scoped paths remain available."
            )
        tracked_paths = "".join(entry.split("\t", 1)[1] + "\0" for entry in entries)
    if names and not staged:
        attributes = _run_git_checked(
            root=root,
            args=[*prefix, "check-attr", "-z", "--stdin", "filter"],
            input_s=tracked_paths,
            fallback="Could not inspect Git attributes before reading repository changes.",
        ).stdout.split("\0")
        configured = {name[7:].rsplit(".", 1)[0] for name in names}
        if any(value in configured for value in attributes[2::3]):
            raise GitError(
                "git_content_filter_required: This repository uses content filters. "
                "Native review cannot compare these files accurately without running those "
                "programs. Use shell_run with git status or git diff through the configured "
                "command runner. Staged git_diff remains available."
            )
    for name in names:
        value = "false" if name.endswith(".required") else ""
        prefix.extend(["-c", f"{name}={value}"])
    return [*prefix, *args]


def git_status(*, root: Path) -> dict[str, Any]:
    cp = _run_git_checked(
        root=root,
        args=_git_review_args(root, ["status", "--porcelain=v1", "-b"]),
        fallback="git status failed",
    )
    return {"status": cp.stdout}


def git_diff(
    *,
    root: Path,
    path: str | None = None,
    staged: bool = False,
    offset: int = 0,
    diff_id: str | None = None,
) -> dict[str, Any]:
    if type(staged) is not bool:
        raise GitError("staged must be a boolean")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise GitError("offset must be a non-negative integer")
    if offset and not diff_id:
        raise GitError("Pass the previous page's diff_id when continuing a diff.")
    # Reviews must never invoke repository-configured diff programs or text filters.
    args = [
        "--literal-pathspecs",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
    ]
    if staged:
        args.append("--cached")
    if path is not None:
        args.extend(["--", _resolve_git_path(root, path)])
    cp = _run_git_checked(
        root=root,
        args=_git_review_args(
            root,
            args,
            staged=staged,
            path=_resolve_git_path(root, path) if path is not None else None,
        ),
        fallback="git diff failed",
    )
    diff = cp.stdout
    current_id = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    if diff_id is not None and diff_id != current_id:
        raise GitError("The diff changed between pages. Restart with offset=0 and no diff_id.")
    end = min(offset + 20000, len(diff))
    truncated = end < len(diff)
    return {
        "view": "staged" if staged else "unstaged",
        "diff_id": current_id,
        "offset": offset,
        "next_offset": end if truncated else None,
        "total_chars": len(diff),
        "truncated": truncated,
        "diff": diff[offset:end],
    }


def git_history(
    *,
    root: Path,
    mode: str,
    path: str | None = None,
    limit: int = _GIT_HISTORY_DEFAULT_LOG_LIMIT,
    ref: str | None = None,
    grep: str | None = None,
    author: str | None = None,
    commit: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"log", "show", "blame"}:
        raise GitError(f"Unsupported git_history mode: {mode!r}")

    normalized_path = None
    if path is not None and str(path).strip():
        normalized_path = _resolve_git_path(root, str(path))

    if normalized_mode == "log":
        safe_limit = max(1, min(int(limit), _GIT_HISTORY_MAX_LOG_LIMIT))
        ref_value = str(ref or "HEAD").strip() or "HEAD"
        args = [
            "log",
            f"-n{safe_limit + 1}",
            "--date=iso-strict",
            "--format=format:%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1e",
        ]
        if grep:
            args.extend(["--grep", str(grep)])
        if author:
            args.extend(["--author", str(author)])
        args.append(ref_value)
        if normalized_path is not None:
            args.extend(["--", normalized_path])
        cp = _run_git_checked(root=root, args=args, fallback="git log failed")
        commits = _parse_log_records(cp.stdout)
        truncated = len(commits) > safe_limit
        return {
            "mode": "log",
            "path": normalized_path,
            "limit": safe_limit,
            "ref": ref_value,
            "grep": str(grep) if grep else None,
            "author": str(author) if author else None,
            "commits": commits[:safe_limit],
            "truncated": truncated,
        }

    if normalized_mode == "show":
        explicit_commit = str(commit or "").strip()
        explicit_ref = str(ref or "").strip()
        if explicit_commit and explicit_ref and explicit_commit != explicit_ref:
            raise GitError("show mode commit and ref must name the same revision")
        commit_value = explicit_commit or explicit_ref
        if not commit_value:
            raise GitError("show mode requires commit or ref")
        meta_cp = _run_git_checked(
            root=root,
            args=[
                "show",
                "--quiet",
                "--no-patch",
                "--date=iso-strict",
                "--format=format:%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b",
                commit_value,
            ],
            fallback="git show failed",
        )
        fields = meta_cp.stdout.split("\x1f", 6)
        if len(fields) != 7:
            raise GitError("git show returned an unexpected format")
        body_excerpt, body_truncated = _clip_text(
            fields[6], max_chars=_GIT_HISTORY_SHOW_BODY_MAX_CHARS
        )

        patch_args = ["show", "--format=", "--patch", "--no-ext-diff", "--no-color", commit_value]
        if normalized_path is not None:
            patch_args.extend(["--", normalized_path])
        patch_cp = _run_git_checked(root=root, args=patch_args, fallback="git show failed")
        patch_excerpt, patch_truncated = _clip_text(
            patch_cp.stdout, max_chars=_GIT_HISTORY_SHOW_PATCH_MAX_CHARS
        )

        return {
            "mode": "show",
            "path": normalized_path,
            "commit": {
                "commit": fields[0],
                "short_commit": fields[1],
                "author_name": fields[2],
                "author_email": fields[3],
                "authored_at": fields[4],
                "subject": fields[5],
                "body_excerpt": body_excerpt,
                "body_truncated": body_truncated,
            },
            "patch_excerpt": patch_excerpt,
            "patch_truncated": patch_truncated,
        }

    if normalized_path is None:
        raise GitError("blame mode requires path")
    if start_line is None or end_line is None:
        raise GitError("blame mode requires start_line and end_line")
    if start_line < 1:
        raise GitError(f"Invalid start_line: {start_line} (must be >= 1)")
    if end_line < start_line:
        raise GitError(
            f"Invalid line range: end_line ({end_line}) must be >= start_line ({start_line})"
        )
    requested_lines = end_line - start_line + 1
    if requested_lines > _GIT_HISTORY_BLAME_MAX_LINES:
        raise GitError(
            "Requested blame range too large: "
            f"{requested_lines} lines (max {_GIT_HISTORY_BLAME_MAX_LINES})"
        )

    cp = _run_git_checked(
        root=root,
        args=[
            "blame",
            "--line-porcelain",
            f"-L{start_line},{end_line}",
            "--",
            normalized_path,
        ],
        fallback="git blame failed",
    )

    lines: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in cp.stdout.splitlines():
        if raw_line.startswith("\t"):
            if current is None:
                continue
            line_text, line_truncated = _clip_text(
                raw_line[1:], max_chars=_GIT_HISTORY_BLAME_LINE_MAX_CHARS
            )
            lines.append(
                {
                    "line_number": int(current["line_number"]),
                    "commit": str(current["commit"]),
                    "short_commit": str(current["commit"])[:12],
                    "author_name": str(current.get("author") or ""),
                    "author_email": str(current.get("author-mail") or "").strip("<>"),
                    "summary": str(current.get("summary") or ""),
                    "content": line_text,
                    "content_truncated": line_truncated,
                }
            )
            current = None
            continue

        parts = raw_line.split()
        if len(parts) >= 4 and len(parts[0]) >= 8:
            try:
                final_lineno = int(parts[2])
            except ValueError:
                final_lineno = 0
            current = {"commit": parts[0], "line_number": final_lineno}
            continue

        if current is None:
            continue
        key, _, value = raw_line.partition(" ")
        current[key] = value

    actual_end_line = end_line if not lines else int(lines[-1]["line_number"])
    return {
        "mode": "blame",
        "path": normalized_path,
        "start_line": start_line,
        "end_line": actual_end_line,
        "lines": lines,
        "truncated": False,
    }


def git_apply_patch(*, root: Path, patch: str) -> dict[str, Any]:
    normalized_patch = _normalize_patch_text(patch)
    _validate_patch_shape(normalized_patch)
    with _patch_lock(root):
        preconditions = _capture_patch_preconditions(root, normalized_patch)
        with tempfile.TemporaryDirectory(prefix="alysis-patch-index-") as temporary:
            patch_env = dict(os.environ)
            patch_env["GIT_INDEX_FILE"] = os.fspath(Path(temporary) / "index")
            no_filters = ["-c", "core.autocrlf=false"]
            _run_git_checked(
                root=root,
                args=[*no_filters, "read-tree", "--empty"],
                env=patch_env,
                fallback="git apply preflight failed",
            )
            existing_paths = [item.path for item in preconditions if item.exists]
            if existing_paths:
                _run_git_checked(
                    root=root,
                    args=[*no_filters, "add", "-f", "--", *existing_paths],
                    env=patch_env,
                    fallback="git apply preflight failed",
                )
            _assert_patch_preconditions(root, preconditions)
            try:
                _run_git_checked(
                    root=root,
                    args=[
                        *no_filters,
                        "apply",
                        "--check",
                        "--index",
                        "--whitespace=nowarn",
                    ],
                    input_s=normalized_patch,
                    env=patch_env,
                    fallback="git apply preflight failed",
                )
            except GitError as e:
                raise GitError(f"git apply preflight failed: {e}") from e
            _assert_patch_preconditions(root, preconditions)
            try:
                # The private index records the exact whole-file preconditions.
                # ``--index`` rechecks those blobs inside the mutating Git process,
                # without reading or changing the developer's real index.
                _run_git_checked(
                    root=root,
                    args=[*no_filters, "apply", "--index", "--whitespace=nowarn"],
                    input_s=normalized_patch,
                    env=patch_env,
                    fallback="git apply failed",
                )
            except GitError as exc:
                # If another writer won after our final user-space check, surface
                # the deterministic stale-file contract rather than a generic
                # index mismatch.
                _assert_patch_preconditions(root, preconditions)
                raise exc
    return {"applied": True}
