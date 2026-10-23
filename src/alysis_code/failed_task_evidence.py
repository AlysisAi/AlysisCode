"""Capture a failed task's work before its workspace is cleaned up.

Swarm failure cleanup removes the task worktree and branch so the next run is not
blocked by leftover state. That is correct housekeeping and wrong data handling: the
worktree is the only place some of the work exists. An uncommitted edit, or a new
file the worker never got to commit, disappeared with the directory.

So every cleanup path first calls :func:`preserve_failed_task_evidence`, which
writes the task's full diff (committed *and* uncommitted, plus untracked file
contents) and its verification log into ``<run>/execution/evidence/<task>/``. That
directory lives in the run, not in the worktree, so cleanup can then proceed exactly
as configured.

Capture is best effort by construction: it must never raise, never mutate the
workspace it is reading (no ``git add``, no index writes), and never be the reason a
cleanup is skipped. Whatever it could not capture is recorded in ``errors`` and in
``evidence.json`` rather than silently dropped.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .command_text import command_text
from .execution_shared import safe_task_file_component
from .git_safe import build_git_cmd

EVIDENCE_DIR_NAME = "evidence"
PATCH_FILE_NAME = "patch.diff"
VERIFICATION_LOG_FILE_NAME = "verification.log"
METADATA_FILE_NAME = "evidence.json"

# Untracked files are the work most at risk (a new module the worker never
# committed), so their contents are captured, but a stray build directory must not
# turn evidence capture into a disk-filling copy.
MAX_UNTRACKED_FILES = 200
MAX_UNTRACKED_FILE_BYTES = 256 * 1024
MAX_UNTRACKED_TOTAL_BYTES = 8 * 1024 * 1024
_GIT_TIMEOUT_S = 120


@dataclass(frozen=True)
class PreservedTaskEvidence:
    """Where the evidence landed, and what could not be captured."""

    task_id: str
    directory: Path | None
    patch_path: Path | None = None
    verification_log_path: Path | None = None
    metadata_path: Path | None = None
    captured_untracked_files: tuple[str, ...] = ()
    truncated: bool = False
    errors: tuple[str, ...] = ()

    @property
    def captured(self) -> bool:
        return self.patch_path is not None or self.verification_log_path is not None

    def to_payload(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "directory": os.fspath(self.directory) if self.directory else None,
            "patch_path": os.fspath(self.patch_path) if self.patch_path else None,
            "verification_log_path": (
                os.fspath(self.verification_log_path) if self.verification_log_path else None
            ),
            "captured_untracked_files": list(self.captured_untracked_files),
            "truncated": bool(self.truncated),
            "errors": list(self.errors),
        }


GitRunner = Callable[[Path, list[str]], "subprocess.CompletedProcess[str]"]


def _default_git_runner(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_git_cmd(root, args),
        check=False,
        capture_output=True,
        **command_text(),
        timeout=_GIT_TIMEOUT_S,
    )


def evidence_dir_for(execution_dir: Path, task_id: str) -> Path:
    return Path(execution_dir) / EVIDENCE_DIR_NAME / safe_task_file_component(task_id)


def preserve_failed_task_evidence(
    *,
    execution_dir: Path,
    task_id: str,
    worktree_path: Path | None,
    base_branch: str | None = None,
    branch: str | None = None,
    reason: str = "",
    verification_log_path: Path | None = None,
    verification_summary: str | None = None,
    extra: dict[str, object] | None = None,
    git_runner: GitRunner | None = None,
) -> PreservedTaskEvidence:
    """Write a failed task's diff and verification log into the run's artifacts.

    Returns what was captured. Never raises: a capture failure is reported through
    :attr:`PreservedTaskEvidence.errors` so the caller can warn and still clean up.
    """
    errors: list[str] = []
    directory: Path | None = None
    try:
        directory = evidence_dir_for(execution_dir, task_id)
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return PreservedTaskEvidence(
            task_id=str(task_id),
            directory=None,
            errors=(f"could not create evidence directory: {e}",),
        )

    patch_path: Path | None = None
    captured_untracked: tuple[str, ...] = ()
    truncated = False
    runner = git_runner or _default_git_runner
    resolved_worktree = Path(worktree_path) if worktree_path is not None else None

    if resolved_worktree is not None and resolved_worktree.exists():
        capture = _capture_worktree_patch(
            worktree_path=resolved_worktree,
            base_branch=base_branch,
            runner=runner,
        )
        errors.extend(capture.errors)
        captured_untracked = capture.untracked_files
        truncated = capture.truncated
        try:
            target = directory / PATCH_FILE_NAME
            target.write_text(capture.text, encoding="utf-8")
            patch_path = target
        except OSError as e:
            errors.append(f"could not write evidence patch: {e}")
    elif resolved_worktree is None:
        errors.append("no task workspace path was available to capture a diff from")
    else:
        errors.append(f"task workspace no longer exists: {os.fspath(resolved_worktree)}")

    stored_log: Path | None = None
    try:
        stored_log = _write_verification_log(
            directory=directory,
            source=verification_log_path,
            summary=verification_summary,
        )
    except OSError as e:
        errors.append(f"could not write verification log: {e}")

    metadata_path: Path | None = None
    payload: dict[str, object] = {
        "schema_version": 1,
        "task_id": str(task_id),
        "captured_at": datetime.now(UTC).isoformat(),
        "reason": str(reason or ""),
        "branch": str(branch or ""),
        "base_branch": str(base_branch or ""),
        "worktree_path": os.fspath(resolved_worktree) if resolved_worktree else None,
        "patch_file": PATCH_FILE_NAME if patch_path is not None else None,
        "verification_log_file": (VERIFICATION_LOG_FILE_NAME if stored_log is not None else None),
        "verification_summary": verification_summary,
        "captured_untracked_files": list(captured_untracked),
        "truncated": bool(truncated),
        "errors": list(errors),
    }
    if extra:
        payload["extra"] = dict(extra)
    try:
        metadata_path = directory / METADATA_FILE_NAME
        metadata_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        metadata_path = None
        errors.append(f"could not write evidence metadata: {e}")

    return PreservedTaskEvidence(
        task_id=str(task_id),
        directory=directory,
        patch_path=patch_path,
        verification_log_path=stored_log,
        metadata_path=metadata_path,
        captured_untracked_files=captured_untracked,
        truncated=truncated,
        errors=tuple(errors),
    )


@dataclass
class _PatchCapture:
    text: str
    untracked_files: tuple[str, ...] = ()
    truncated: bool = False
    errors: list[str] = field(default_factory=list)


def _capture_worktree_patch(
    *,
    worktree_path: Path,
    base_branch: str | None,
    runner: GitRunner,
) -> _PatchCapture:
    """Build one text artifact holding everything the worktree still has.

    ``git diff <base>`` is used rather than ``format-patch``: it compares the working
    tree against the base, so committed and uncommitted changes are both in the
    output without staging anything.
    """
    sections: list[str] = [
        "# Preserved Task Evidence",
        f"# worktree: {os.fspath(worktree_path)}",
        f"# base branch: {base_branch or '(unknown)'}",
        "",
    ]
    errors: list[str] = []

    status_ok, status_text = _git_text(runner, worktree_path, ["status", "--porcelain"])
    if status_ok:
        sections.extend(
            ["## git status --porcelain", "", status_text.rstrip("\n") or "(clean)", ""]
        )
    else:
        errors.append(f"git status failed: {status_text.strip() or 'unknown error'}")

    if base_branch:
        diff_ok, diff_text = _git_text(runner, worktree_path, ["diff", str(base_branch)])
        label = f"## git diff {base_branch} (tracked changes vs base, committed and uncommitted)"
        if diff_ok:
            sections.extend([label, "", diff_text.rstrip("\n") or "(no tracked changes)", ""])
        else:
            errors.append(f"git diff against {base_branch} failed: {diff_text.strip()}")

    # Always capture the working-tree delta too: when the base ref is unknown or the
    # diff above failed, this is the only record of uncommitted work.
    head_ok, head_text = _git_text(runner, worktree_path, ["diff", "HEAD"])
    if head_ok:
        sections.extend(
            [
                "## git diff HEAD (uncommitted tracked changes)",
                "",
                head_text.rstrip("\n") or "(none)",
                "",
            ]
        )
    else:
        errors.append(f"git diff HEAD failed: {head_text.strip() or 'unknown error'}")

    untracked_ok, untracked_text = _git_text(
        runner,
        worktree_path,
        ["ls-files", "--others", "--exclude-standard"],
    )
    captured: list[str] = []
    truncated = False
    if untracked_ok:
        candidates = [line.strip() for line in untracked_text.splitlines() if line.strip()]
        if len(candidates) > MAX_UNTRACKED_FILES:
            truncated = True
            sections.append(
                f"## NOTE: {len(candidates)} untracked files found; "
                f"capturing the first {MAX_UNTRACKED_FILES}."
            )
            sections.append("")
            candidates = candidates[:MAX_UNTRACKED_FILES]
        total_bytes = 0
        for relative in candidates:
            path = worktree_path / relative
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError as e:
                errors.append(f"could not stat untracked file {relative}: {e}")
                continue
            if size > MAX_UNTRACKED_FILE_BYTES or total_bytes + size > MAX_UNTRACKED_TOTAL_BYTES:
                truncated = True
                sections.extend(
                    [f"## untracked file: {relative}", "", f"(skipped: {size} bytes)", ""]
                )
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                errors.append(f"could not read untracked file {relative}: {e}")
                continue
            total_bytes += size
            captured.append(relative)
            sections.extend([f"## untracked file: {relative}", "", content.rstrip("\n"), ""])
    else:
        errors.append(f"git ls-files failed: {untracked_text.strip() or 'unknown error'}")

    if errors:
        sections.extend(["## capture warnings", "", *[f"- {item}" for item in errors], ""])

    return _PatchCapture(
        text="\n".join(sections) + "\n",
        untracked_files=tuple(captured),
        truncated=truncated,
        errors=errors,
    )


def _git_text(runner: GitRunner, root: Path, args: list[str]) -> tuple[bool, str]:
    try:
        cp = runner(root, args)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    stdout = cp.stdout or ""
    if cp.returncode != 0:
        return False, (cp.stderr or stdout or "").strip()
    return True, stdout


def _write_verification_log(
    *,
    directory: Path,
    source: Path | None,
    summary: str | None,
) -> Path | None:
    target = directory / VERIFICATION_LOG_FILE_NAME
    if source is not None:
        resolved = Path(source)
        if resolved.exists():
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                text = f"(could not read verification artifact {os.fspath(resolved)}: {e})\n"
            header = f"# Verification log copied from {os.fspath(resolved)}\n\n"
            target.write_text(header + text, encoding="utf-8")
            return target
    if summary and str(summary).strip():
        target.write_text(
            "# Verification log\n\n"
            "No verification artifact file was produced for this task.\n\n"
            f"Summary: {summary}\n",
            encoding="utf-8",
        )
        return target
    target.write_text(
        "# Verification log\n\nVerification produced no artifact and no summary for this task.\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "EVIDENCE_DIR_NAME",
    "MAX_UNTRACKED_FILES",
    "MAX_UNTRACKED_FILE_BYTES",
    "MAX_UNTRACKED_TOTAL_BYTES",
    "METADATA_FILE_NAME",
    "PATCH_FILE_NAME",
    "VERIFICATION_LOG_FILE_NAME",
    "PreservedTaskEvidence",
    "evidence_dir_for",
    "preserve_failed_task_evidence",
]
