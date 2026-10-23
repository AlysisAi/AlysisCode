from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .command_text import command_text
from .file_classification import classify_path, is_generated_or_vendor_path
from .git_ops import GitOpsError
from .git_safe import build_git_cmd, build_git_process_env, configured_git_filter_drivers
from .runtime_artifacts import is_runtime_artifact_path

PatchCaptureStatus = Literal["complete", "partial", "failed"]
CandidatePatchCompleteness = Literal["complete", "partial", "failed"]
CandidateDeltaPathKind = Literal[
    "committed",
    "staged",
    "unstaged",
    "deleted",
    "untracked_material",
    "untracked_generated",
    "unknown_binary_or_large",
    "ignored_generated",
    "ignored_material",
]


@dataclass(frozen=True)
class CandidateDeltaPath:
    path: str
    kind: CandidateDeltaPathKind
    tracked: bool
    classification: str = "unknown"
    size_bytes: int | None = None
    sha256: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "tracked": self.tracked,
            "classification": self.classification,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CandidateDeltaPath:
        return cls(
            path=str(payload.get("path") or ""),
            kind=_delta_path_kind(payload.get("kind")),
            tracked=bool(payload.get("tracked")),
            classification=str(payload.get("classification") or "unknown"),
            size_bytes=_optional_int(payload.get("size_bytes")),
            sha256=str(payload.get("sha256")) if payload.get("sha256") is not None else None,
            reason=str(payload.get("reason")) if payload.get("reason") is not None else None,
        )


@dataclass(frozen=True)
class CandidateEvidenceExclusion:
    path: str
    reason: str
    tracked: bool = False
    classification: str = "runtime_artifact"
    size_bytes: int | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reason": self.reason,
            "tracked": self.tracked,
            "classification": self.classification,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CandidateEvidenceExclusion:
        return cls(
            path=str(payload.get("path") or ""),
            reason=str(payload.get("reason") or "excluded"),
            tracked=bool(payload.get("tracked")),
            classification=str(payload.get("classification") or "runtime_artifact"),
            size_bytes=_optional_int(payload.get("size_bytes")),
            sha256=str(payload.get("sha256")) if payload.get("sha256") is not None else None,
        )


@dataclass(frozen=True)
class CandidatePatchCaptureResult:
    status: PatchCaptureStatus
    material_patch_complete: bool
    patch_text_sha256: str
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "material_patch_complete": self.material_patch_complete,
            "patch_text_sha256": self.patch_text_sha256,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CandidatePatchCaptureResult:
        status = _patch_status(payload.get("status"))
        return cls(
            status=status,
            material_patch_complete=bool(payload.get("material_patch_complete")),
            patch_text_sha256=str(payload.get("patch_text_sha256") or ""),
            reason_codes=_tuple_strs(payload.get("reason_codes")),
        )


@dataclass(frozen=True)
class CandidateWorkingTreeDelta:
    base_revision: str
    final_head_revision: str | None
    head_changed_from_base: bool = False
    commits_ahead_count: int = 0
    raw_changed_paths: tuple[str, ...] = ()
    material_changed_paths: tuple[str, ...] = ()
    excluded_generated_paths: tuple[str, ...] = ()
    omitted_material_paths: tuple[str, ...] = ()
    tracked_changed_paths: tuple[str, ...] = ()
    staged_paths: tuple[str, ...] = ()
    unstaged_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    untracked_material_paths: tuple[str, ...] = ()
    untracked_generated_paths: tuple[str, ...] = ()
    ignored_generated_paths: tuple[str, ...] = ()
    unknown_binary_or_large_paths: tuple[str, ...] = ()
    path_entries: tuple[CandidateDeltaPath, ...] = ()
    excluded_paths: tuple[CandidateEvidenceExclusion, ...] = ()
    omitted_material_entries: tuple[CandidateEvidenceExclusion, ...] = ()
    patch_capture_status: PatchCaptureStatus = "complete"
    material_patch_complete: bool = True
    patch_reason_codes: tuple[str, ...] = ()
    state_descriptors: tuple[str, ...] = ()
    patch_text_sha256: str = ""
    material_changed_file_count: int = 0
    raw_changed_file_count: int = 0
    evidence_filter_policy_version: str = "candidate-working-tree-delta-v1"

    def __post_init__(self) -> None:
        material = _sorted_unique(self.material_changed_paths)
        raw = _sorted_unique(self.raw_changed_paths)
        object.__setattr__(self, "material_changed_paths", material)
        object.__setattr__(self, "raw_changed_paths", raw)
        object.__setattr__(self, "material_changed_file_count", len(material))
        object.__setattr__(self, "raw_changed_file_count", len(raw))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "base_revision": self.base_revision,
            "final_head_revision": self.final_head_revision,
            "head_changed_from_base": self.head_changed_from_base,
            "commits_ahead_count": self.commits_ahead_count,
            "raw_changed_paths": list(self.raw_changed_paths),
            "material_changed_paths": list(self.material_changed_paths),
            "excluded_generated_paths": list(self.excluded_generated_paths),
            "omitted_material_paths": list(self.omitted_material_paths),
            "tracked_changed_paths": list(self.tracked_changed_paths),
            "staged_paths": list(self.staged_paths),
            "unstaged_paths": list(self.unstaged_paths),
            "deleted_paths": list(self.deleted_paths),
            "untracked_material_paths": list(self.untracked_material_paths),
            "untracked_generated_paths": list(self.untracked_generated_paths),
            "ignored_generated_paths": list(self.ignored_generated_paths),
            "unknown_binary_or_large_paths": list(self.unknown_binary_or_large_paths),
            "path_entries": [item.to_dict() for item in self.path_entries],
            "excluded_paths": [item.to_dict() for item in self.excluded_paths],
            "omitted_material_entries": [item.to_dict() for item in self.omitted_material_entries],
            "patch_capture_status": self.patch_capture_status,
            "material_patch_complete": self.material_patch_complete,
            "patch_reason_codes": list(self.patch_reason_codes),
            "state_descriptors": list(self.state_descriptors),
            "patch_text_sha256": self.patch_text_sha256,
            "material_changed_file_count": self.material_changed_file_count,
            "raw_changed_file_count": self.raw_changed_file_count,
            "evidence_filter_policy_version": self.evidence_filter_policy_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CandidateWorkingTreeDelta:
        return cls(
            base_revision=str(payload.get("base_revision") or ""),
            final_head_revision=(
                str(payload.get("final_head_revision"))
                if payload.get("final_head_revision") is not None
                else None
            ),
            head_changed_from_base=bool(payload.get("head_changed_from_base")),
            commits_ahead_count=_int(payload.get("commits_ahead_count")),
            raw_changed_paths=_tuple_strs(payload.get("raw_changed_paths")),
            material_changed_paths=_tuple_strs(payload.get("material_changed_paths")),
            excluded_generated_paths=_tuple_strs(payload.get("excluded_generated_paths")),
            omitted_material_paths=_tuple_strs(payload.get("omitted_material_paths")),
            tracked_changed_paths=_tuple_strs(payload.get("tracked_changed_paths")),
            staged_paths=_tuple_strs(payload.get("staged_paths")),
            unstaged_paths=_tuple_strs(payload.get("unstaged_paths")),
            deleted_paths=_tuple_strs(payload.get("deleted_paths")),
            untracked_material_paths=_tuple_strs(payload.get("untracked_material_paths")),
            untracked_generated_paths=_tuple_strs(payload.get("untracked_generated_paths")),
            ignored_generated_paths=_tuple_strs(payload.get("ignored_generated_paths")),
            unknown_binary_or_large_paths=_tuple_strs(payload.get("unknown_binary_or_large_paths")),
            path_entries=tuple(
                CandidateDeltaPath.from_dict(item)
                for item in _tuple_dicts(payload.get("path_entries"))
            ),
            excluded_paths=tuple(
                CandidateEvidenceExclusion.from_dict(item)
                for item in _tuple_dicts(payload.get("excluded_paths"))
            ),
            omitted_material_entries=tuple(
                CandidateEvidenceExclusion.from_dict(item)
                for item in _tuple_dicts(payload.get("omitted_material_entries"))
            ),
            patch_capture_status=_patch_status(payload.get("patch_capture_status")),
            material_patch_complete=bool(payload.get("material_patch_complete")),
            patch_reason_codes=_tuple_strs(payload.get("patch_reason_codes")),
            state_descriptors=_tuple_strs(payload.get("state_descriptors")),
            patch_text_sha256=str(payload.get("patch_text_sha256") or ""),
        )


@dataclass(frozen=True)
class CandidateGitState:
    base_ref: str
    head_ref: str | None
    base_ref_available: bool = False
    git_commit_created: bool = False
    status_porcelain: str = ""
    changed_files: tuple[str, ...] = ()
    committed_files: tuple[str, ...] = ()
    staged_files: tuple[str, ...] = ()
    unstaged_files: tuple[str, ...] = ()
    untracked_files: tuple[str, ...] = ()
    patch_text: str = ""
    patch_capture_status: PatchCaptureStatus = "complete"
    reason_codes: tuple[str, ...] = ()
    state_descriptors: tuple[str, ...] = ()
    raw_changed_paths: tuple[str, ...] = ()
    material_changed_paths: tuple[str, ...] = ()
    excluded_generated_paths: tuple[str, ...] = ()
    omitted_material_paths: tuple[str, ...] = ()
    tracked_changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    untracked_material_paths: tuple[str, ...] = ()
    untracked_generated_paths: tuple[str, ...] = ()
    unknown_binary_or_large_paths: tuple[str, ...] = ()
    material_patch_complete: bool = True
    patch_text_sha256: str = ""
    material_changed_file_count: int = 0
    raw_changed_file_count: int = 0
    evidence_filter_policy_version: str = "candidate-working-tree-delta-v1"
    working_tree_delta: CandidateWorkingTreeDelta | None = None
    retained_ignored_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        material = _sorted_unique(self.material_changed_paths or self.changed_files)
        raw = _sorted_unique(self.raw_changed_paths or self.changed_files)
        object.__setattr__(self, "changed_files", material)
        object.__setattr__(self, "material_changed_paths", material)
        object.__setattr__(self, "raw_changed_paths", raw)
        object.__setattr__(self, "committed_files", _sorted_unique(self.committed_files))
        object.__setattr__(self, "staged_files", _sorted_unique(self.staged_files))
        object.__setattr__(self, "unstaged_files", _sorted_unique(self.unstaged_files))
        object.__setattr__(self, "untracked_files", _sorted_unique(self.untracked_files))
        object.__setattr__(self, "material_changed_file_count", len(material))
        object.__setattr__(self, "raw_changed_file_count", len(raw))
        if not self.patch_text_sha256:
            object.__setattr__(self, "patch_text_sha256", _sha256_text(self.patch_text))
        if self.working_tree_delta is None:
            object.__setattr__(
                self,
                "working_tree_delta",
                CandidateWorkingTreeDelta(
                    base_revision=self.base_ref,
                    final_head_revision=self.head_ref,
                    head_changed_from_base=self.git_commit_created,
                    raw_changed_paths=raw,
                    material_changed_paths=material,
                    excluded_generated_paths=self.excluded_generated_paths,
                    omitted_material_paths=self.omitted_material_paths,
                    tracked_changed_paths=self.tracked_changed_paths,
                    staged_paths=self.staged_files,
                    unstaged_paths=self.unstaged_files,
                    deleted_paths=self.deleted_paths,
                    untracked_material_paths=self.untracked_material_paths,
                    untracked_generated_paths=self.untracked_generated_paths,
                    unknown_binary_or_large_paths=self.unknown_binary_or_large_paths,
                    patch_capture_status=self.patch_capture_status,
                    material_patch_complete=self.material_patch_complete,
                    patch_reason_codes=self.reason_codes,
                    state_descriptors=self.state_descriptors,
                    patch_text_sha256=self.patch_text_sha256,
                ),
            )

    @property
    def git_status_after(self) -> str:
        return self.status_porcelain

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "base_ref_available": self.base_ref_available,
            "git_commit_created": self.git_commit_created,
            "status_porcelain": self.status_porcelain,
            "git_status_after": self.status_porcelain,
            "changed_files": list(self.changed_files),
            "committed_files": list(self.committed_files),
            "staged_files": list(self.staged_files),
            "unstaged_files": list(self.unstaged_files),
            "untracked_files": list(self.untracked_files),
            "patch_text": self.patch_text,
            "patch_capture_status": self.patch_capture_status,
            "reason_codes": list(self.reason_codes),
            "state_descriptors": list(self.state_descriptors),
            "raw_changed_paths": list(self.raw_changed_paths),
            "material_changed_paths": list(self.material_changed_paths),
            "excluded_generated_paths": list(self.excluded_generated_paths),
            "omitted_material_paths": list(self.omitted_material_paths),
            "tracked_changed_paths": list(self.tracked_changed_paths),
            "deleted_paths": list(self.deleted_paths),
            "untracked_material_paths": list(self.untracked_material_paths),
            "untracked_generated_paths": list(self.untracked_generated_paths),
            "unknown_binary_or_large_paths": list(self.unknown_binary_or_large_paths),
            "material_patch_complete": self.material_patch_complete,
            "retained_ignored_paths": list(self.retained_ignored_paths),
            "patch_text_sha256": self.patch_text_sha256,
            "material_changed_file_count": self.material_changed_file_count,
            "raw_changed_file_count": self.raw_changed_file_count,
            "evidence_filter_policy_version": self.evidence_filter_policy_version,
            "working_tree_delta": (
                self.working_tree_delta.to_dict() if self.working_tree_delta is not None else None
            ),
        }

    def patch_capture_artifact(self) -> dict[str, Any]:
        delta = self.working_tree_delta
        return {
            "schema_version": 1,
            "base_revision": self.base_ref,
            "final_head_revision": self.head_ref,
            "raw_git_state": {
                "status_porcelain": self.status_porcelain,
                "committed_files": list(self.committed_files),
                "staged_files": list(self.staged_files),
                "unstaged_files": list(self.unstaged_files),
                "untracked_files": list(self.untracked_files),
            },
            "material_delta": delta.to_dict() if delta is not None else {},
            "raw_changed_paths": list(self.raw_changed_paths),
            "material_changed_paths": list(self.material_changed_paths),
            "excluded_paths": (
                [item.to_dict() for item in delta.excluded_paths] if delta is not None else []
            ),
            "omitted_material_paths": (
                [item.to_dict() for item in delta.omitted_material_entries]
                if delta is not None
                else []
            ),
            "patch_capture_status": self.patch_capture_status,
            "material_patch_complete": self.material_patch_complete,
            "retained_ignored_paths": list(self.retained_ignored_paths),
            "state_descriptors": list(self.state_descriptors),
            "reason_codes": list(self.reason_codes),
            "patch_text_sha256": self.patch_text_sha256,
            "evidence_filter_policy_version": self.evidence_filter_policy_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CandidateGitState:
        status = str(
            payload.get("status_porcelain")
            if payload.get("status_porcelain") is not None
            else payload.get("git_status_after") or ""
        )
        delta_payload = payload.get("working_tree_delta")
        delta = (
            CandidateWorkingTreeDelta.from_dict(delta_payload)
            if isinstance(delta_payload, dict)
            else None
        )
        patch_status = _patch_status(payload.get("patch_capture_status"))
        return cls(
            base_ref=str(payload.get("base_ref") or ""),
            head_ref=str(payload.get("head_ref")) if payload.get("head_ref") is not None else None,
            base_ref_available=bool(payload.get("base_ref_available") or payload.get("base_ref")),
            git_commit_created=bool(payload.get("git_commit_created")),
            status_porcelain=status,
            changed_files=_tuple_strs(
                payload.get("changed_files")
                if payload.get("changed_files") is not None
                else payload.get("material_changed_paths")
            ),
            committed_files=_tuple_strs(payload.get("committed_files")),
            staged_files=_tuple_strs(payload.get("staged_files")),
            unstaged_files=_tuple_strs(payload.get("unstaged_files")),
            untracked_files=_tuple_strs(payload.get("untracked_files")),
            patch_text=str(payload.get("patch_text") or ""),
            patch_capture_status=patch_status,
            reason_codes=_tuple_strs(payload.get("reason_codes")),
            state_descriptors=_tuple_strs(payload.get("state_descriptors")),
            raw_changed_paths=_tuple_strs(payload.get("raw_changed_paths")),
            material_changed_paths=_tuple_strs(payload.get("material_changed_paths")),
            excluded_generated_paths=_tuple_strs(payload.get("excluded_generated_paths")),
            omitted_material_paths=_tuple_strs(payload.get("omitted_material_paths")),
            tracked_changed_paths=_tuple_strs(payload.get("tracked_changed_paths")),
            deleted_paths=_tuple_strs(payload.get("deleted_paths")),
            untracked_material_paths=_tuple_strs(payload.get("untracked_material_paths")),
            untracked_generated_paths=_tuple_strs(payload.get("untracked_generated_paths")),
            unknown_binary_or_large_paths=_tuple_strs(payload.get("unknown_binary_or_large_paths")),
            material_patch_complete=bool(payload.get("material_patch_complete", True)),
            patch_text_sha256=str(payload.get("patch_text_sha256") or ""),
            evidence_filter_policy_version=str(
                payload.get("evidence_filter_policy_version") or "candidate-working-tree-delta-v1"
            ),
            working_tree_delta=delta,
            retained_ignored_paths=_tuple_strs(payload.get("retained_ignored_paths")),
        )


class WorkspaceSnapshotError(GitOpsError):
    def __init__(self, reason: str, *, paths: tuple[str, ...] = (), detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.paths = paths


def _snapshot_git(
    root: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            build_git_cmd(root, args, env=env, disable_filters=True),
            env=build_git_process_env(env),
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            check=False,
        )
        output = result.stdout.decode("utf-8")
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceSnapshotError("workspace_snapshot_unreadable") from exc
    if result.returncode:
        raise WorkspaceSnapshotError(
            "workspace_snapshot_git_failed",
            detail=(
                "Git snapshot failed (external Git filters are disabled): "
                + result.stderr.decode("utf-8", errors="replace").strip()
            ),
        )
    return output


def ignored_workspace_paths(root: Path) -> tuple[str, ...]:
    return tuple(
        path.rstrip("/")
        for path in _snapshot_git(
            root, ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"]
        ).split("\0")
        if path
    )


def _check_snapshot_files(root: Path, *, excluded_paths: tuple[str, ...]) -> None:
    """Do not let Git's silent omission of special files prove an empty delta."""
    ignored = ignored_workspace_paths(root)
    excluded = {path.rstrip("/") for path in (*ignored, *excluded_paths) if path}
    # Git's own administrative namespace is not candidate output.
    excluded.add(".git")
    unsupported: list[str] = []
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(root).as_posix()
                    if relative in excluded:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif not (entry.is_file(follow_symlinks=False) or entry.is_symlink()):
                        unsupported.append(relative)
    except OSError as exc:
        raise WorkspaceSnapshotError("workspace_snapshot_unreadable") from exc
    if unsupported:
        raise WorkspaceSnapshotError("unsupported_workspace_file", paths=tuple(sorted(unsupported)))


def _check_snapshot_filters(root: Path, *, pathspecs: list[str]) -> None:
    # Git status may recognize a size change without converting file content.
    # Resolve attributes against the actual index before a HEAD-based temporary
    # index can lose an index-only .gitattributes fallback.
    try:
        drivers = configured_git_filter_drivers(root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceSnapshotError("workspace_snapshot_unreadable") from exc
    if not drivers:
        return
    paths = _snapshot_git(
        root,
        [
            "--no-optional-locks",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *pathspecs,
        ],
    )
    attributes = _snapshot_git(
        root, ["check-attr", "-z", "--stdin", "filter"], input_text=paths
    ).split("\0")
    if attributes[-1] != "" or (len(attributes) - 1) % 3:
        raise WorkspaceSnapshotError("workspace_snapshot_unreadable")
    affected = tuple(
        sorted(
            {
                attributes[index]
                for index in range(0, len(attributes) - 1, 3)
                if attributes[index + 2] in drivers
            }
        )
    )
    if affected:
        raise WorkspaceSnapshotError(
            "workspace_snapshot_git_failed",
            paths=affected,
            detail="Git snapshot refused: external Git filters are disabled for isolated workspaces",
        )


def _working_tree_snapshot(
    root: Path, *, base_ref: str, excluded_paths: tuple[str, ...] = ()
) -> str:
    _check_snapshot_files(root, excluded_paths=excluded_paths)
    pathspecs = [".", *(f":(top,exclude,literal){path}" for path in excluded_paths)]
    _check_snapshot_filters(root, pathspecs=pathspecs)
    # A separate index preserves both staged and unstaged parent work. Git handles
    # binary data, modes, names and ignored paths; no filename classifier is used.
    with tempfile.TemporaryDirectory(prefix="alysis-git-snapshot-") as scratch:
        env = build_git_process_env()
        env["GIT_INDEX_FILE"] = os.fspath(Path(scratch) / "index")
        _snapshot_git(root, ["read-tree", base_ref], env=env)
        _snapshot_git(root, ["add", "--all", "--", *pathspecs], env=env)
        tree = _snapshot_git(root, ["write-tree"], env=env).strip()
        gitlinks = tuple(
            entry.split("\t", 1)[1]
            for entry in _snapshot_git(root, ["ls-tree", "-r", "-z", tree]).split("\0")
            if entry.startswith("160000 ") and "\t" in entry
        )
        if gitlinks:
            # A gitlink cannot preserve nested working files in a detached
            # worktree. Refuse instead of advertising an incomplete snapshot.
            raise WorkspaceSnapshotError("unsupported_workspace_submodule", paths=gitlinks)
        return tree


def snapshot_workspace_baseline(
    *, root: Path, parent_head: str, excluded_paths: tuple[str, ...] = ()
) -> str:
    """Return a private commit for a stable parent working state, without moving refs."""
    tree = _working_tree_snapshot(root, base_ref=parent_head, excluded_paths=excluded_paths)
    if tree != _working_tree_snapshot(root, base_ref=parent_head, excluded_paths=excluded_paths):
        raise WorkspaceSnapshotError("workspace_changed_during_snapshot")
    if _snapshot_git(root, ["rev-parse", "HEAD"]).strip() != parent_head:
        raise WorkspaceSnapshotError("workspace_changed_during_snapshot")
    if tree == _snapshot_git(root, ["rev-parse", f"{parent_head}^{{tree}}"]).strip():
        return parent_head
    env = build_git_process_env()
    # Stable metadata makes equal working states against the same HEAD share a
    # baseline identity. commit-tree creates an object, not a parent commit/ref.
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = "Alysis workspace snapshot"
        env[f"GIT_{role}_EMAIL"] = "snapshot@alysis.invalid"
        env[f"GIT_{role}_DATE"] = "2000-01-01T00:00:00+0000"
    return _snapshot_git(
        root,
        [
            "commit-tree",
            "--no-gpg-sign",
            tree,
            "-p",
            parent_head,
            "-m",
            "Isolated workspace baseline",
        ],
        env=env,
    ).strip()


def capture_isolated_workspace_git_state(
    *, worktree_path: Path, base_ref: str
) -> CandidateGitState:
    """Capture the net child delta, including new binary files, from its baseline."""
    head_ref: str | None = None
    paths: tuple[str, ...] = ()
    try:
        head_ref = _snapshot_git(worktree_path, ["rev-parse", "HEAD"]).strip()
        tree = _working_tree_snapshot(worktree_path, base_ref=head_ref)
        if tree != _working_tree_snapshot(worktree_path, base_ref=head_ref):
            raise WorkspaceSnapshotError("workspace_changed_during_snapshot")
        paths = tuple(
            path
            for path in _snapshot_git(
                worktree_path, ["diff", "--no-renames", "--name-only", "-z", base_ref, tree]
            ).split("\0")
            if path
        )
        patch = _snapshot_git(
            worktree_path,
            [
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--no-color",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                base_ref,
                tree,
            ],
        )
        # Ignored parent setup is absent from the initial child worktree. Any
        # ignored child output could be the requested deliverable; preserve it
        # without guessing intent from filenames or embedding it in a patch.
        ignored_paths = ignored_workspace_paths(worktree_path)
    except WorkspaceSnapshotError as exc:
        return CandidateGitState(
            base_ref=base_ref,
            head_ref=head_ref,
            patch_capture_status="failed",
            material_patch_complete=False,
            raw_changed_paths=exc.paths or paths,
            omitted_material_paths=exc.paths or paths,
            reason_codes=(exc.reason,),
        )
    return CandidateGitState(
        base_ref=base_ref,
        head_ref=head_ref,
        base_ref_available=True,
        git_commit_created=head_ref != base_ref,
        patch_text=patch,
        material_changed_paths=paths,
        raw_changed_paths=tuple(sorted(set(paths) | set(ignored_paths))),
        retained_ignored_paths=ignored_paths,
        patch_capture_status="complete" if patch or not paths else "failed",
        material_patch_complete=bool(patch) or not paths,
        evidence_filter_policy_version="isolated-workspace-git-snapshot-v1",
    )


def capture_candidate_git_state(
    *,
    worktree_path: Path,
    base_ref: str,
    untracked_text_size_limit: int = 512_000,
) -> CandidateGitState:
    if not worktree_path.exists():
        return CandidateGitState(
            base_ref=base_ref,
            head_ref=None,
            base_ref_available=bool(base_ref),
            patch_capture_status="failed",
            material_patch_complete=False,
            reason_codes=("worktree_missing",),
        )

    head_ref = _git_stdout(worktree_path, ["rev-parse", "HEAD"])
    base_available = bool(
        base_ref and _git_success(worktree_path, ["cat-file", "-e", f"{base_ref}^{{commit}}"])
    )
    status = _candidate_git_status(worktree_path)
    git_commit_created = bool(base_available and head_ref and head_ref != base_ref)
    commits_ahead = _commits_ahead(worktree_path, base_ref=base_ref) if base_available else 0
    status_sets = _status_file_sets(status)

    committed_files, committed_patch, committed_reasons = _committed_capture(
        worktree_path,
        base_ref=base_ref,
        head_ref=head_ref,
        base_ref_available=base_available,
    )
    staged_files, staged_patch, staged_reasons = _diff_capture(
        worktree_path,
        patch_args=["diff", "--binary", "--cached"],
        name_args=["diff", "--name-only", "--cached"],
        failed_reason="staged_patch_capture_failed",
    )
    unstaged_files, unstaged_patch, unstaged_reasons = _diff_capture(
        worktree_path,
        patch_args=["diff", "--binary"],
        name_args=["diff", "--name-only"],
        failed_reason="unstaged_patch_capture_failed",
    )

    tracked_changed_paths = _sorted_unique(
        set(committed_files) | set(staged_files) | set(unstaged_files)
    )
    tracked_deleted_paths = _deleted_paths(
        worktree_path, base_ref=base_ref, base_ref_available=base_available
    )
    material_tracked_paths = tracked_changed_paths

    untracked_capture = _capture_untracked_paths(
        worktree_path=worktree_path,
        untracked_paths=status_sets.untracked_files,
        ignored_paths=status_sets.ignored_files,
        size_limit=untracked_text_size_limit,
    )

    material_paths = _sorted_unique(
        set(material_tracked_paths) | set(untracked_capture.material_paths)
    )
    raw_paths = _sorted_unique(
        set(material_tracked_paths)
        | set(status_sets.untracked_files)
        | set(status_sets.ignored_files)
        | set(untracked_capture.excluded_generated_paths)
        | set(untracked_capture.omitted_material_paths)
    )
    patch_text = _join_patch_parts(
        committed_patch,
        staged_patch,
        unstaged_patch,
        untracked_capture.patch_text,
    )

    state_descriptors: list[str] = []
    if git_commit_created:
        state_descriptors.append("commits_ahead")
    if staged_files:
        state_descriptors.append("staged_changes")
    if unstaged_files:
        state_descriptors.append("unstaged_changes")
    if status_sets.untracked_files:
        state_descriptors.append("untracked_changes")
    if status_sets.ignored_files:
        state_descriptors.append("ignored_changes")

    reason_codes: list[str] = []
    if not base_ref or not base_available:
        reason_codes.append("base_revision_unavailable")
    if untracked_capture.excluded_generated_paths:
        reason_codes.append("generated_runtime_artifacts_excluded")
    reason_codes.extend(committed_reasons)
    reason_codes.extend(staged_reasons)
    reason_codes.extend(unstaged_reasons)
    reason_codes.extend(untracked_capture.reason_codes)
    if not raw_paths:
        reason_codes.append("no_changes_detected")

    patch_status = _patch_capture_status(
        material_changed_paths=material_paths,
        patch_text=patch_text,
        reason_codes=tuple(reason_codes),
    )
    material_complete = patch_status == "complete"
    if patch_status == "failed":
        reason_codes.append("patch_capture_failed")
    elif patch_status == "partial":
        reason_codes.append("patch_capture_partial")

    patch_hash = _sha256_text(patch_text)
    delta = CandidateWorkingTreeDelta(
        base_revision=base_ref,
        final_head_revision=head_ref or None,
        head_changed_from_base=git_commit_created,
        commits_ahead_count=commits_ahead,
        raw_changed_paths=raw_paths,
        material_changed_paths=material_paths,
        excluded_generated_paths=untracked_capture.excluded_generated_paths,
        omitted_material_paths=untracked_capture.omitted_material_paths,
        tracked_changed_paths=tracked_changed_paths,
        staged_paths=staged_files,
        unstaged_paths=unstaged_files,
        deleted_paths=tracked_deleted_paths,
        untracked_material_paths=untracked_capture.material_paths,
        untracked_generated_paths=untracked_capture.untracked_generated_paths,
        ignored_generated_paths=untracked_capture.ignored_generated_paths,
        unknown_binary_or_large_paths=untracked_capture.unknown_binary_or_large_paths,
        path_entries=(
            *_tracked_delta_entries(committed_files, kind="committed"),
            *_tracked_delta_entries(staged_files, kind="staged"),
            *_tracked_delta_entries(unstaged_files, kind="unstaged"),
            *_tracked_delta_entries(tracked_deleted_paths, kind="deleted"),
            *untracked_capture.path_entries,
        ),
        excluded_paths=untracked_capture.exclusions,
        omitted_material_entries=untracked_capture.omissions,
        patch_capture_status=patch_status,
        material_patch_complete=material_complete,
        patch_reason_codes=tuple(dict.fromkeys(reason_codes)),
        state_descriptors=tuple(dict.fromkeys(state_descriptors)),
        patch_text_sha256=patch_hash,
    )

    return CandidateGitState(
        base_ref=base_ref,
        head_ref=head_ref or None,
        base_ref_available=base_available,
        git_commit_created=git_commit_created,
        status_porcelain=status,
        changed_files=material_paths,
        committed_files=tuple(path for path in committed_files if path in set(material_paths)),
        staged_files=tuple(path for path in staged_files if path in set(material_paths)),
        unstaged_files=tuple(path for path in unstaged_files if path in set(material_paths)),
        untracked_files=status_sets.untracked_files,
        patch_text=patch_text,
        patch_capture_status=patch_status,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        state_descriptors=tuple(dict.fromkeys(state_descriptors)),
        raw_changed_paths=raw_paths,
        material_changed_paths=material_paths,
        excluded_generated_paths=untracked_capture.excluded_generated_paths,
        omitted_material_paths=untracked_capture.omitted_material_paths,
        tracked_changed_paths=tracked_changed_paths,
        deleted_paths=tracked_deleted_paths,
        untracked_material_paths=untracked_capture.material_paths,
        untracked_generated_paths=untracked_capture.untracked_generated_paths,
        unknown_binary_or_large_paths=untracked_capture.unknown_binary_or_large_paths,
        material_patch_complete=material_complete,
        patch_text_sha256=patch_hash,
        working_tree_delta=delta,
    )


@dataclass(frozen=True)
class _StatusFileSets:
    staged_files: tuple[str, ...] = ()
    unstaged_files: tuple[str, ...] = ()
    untracked_files: tuple[str, ...] = ()
    ignored_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class _UntrackedCapture:
    material_paths: tuple[str, ...] = ()
    untracked_generated_paths: tuple[str, ...] = ()
    ignored_generated_paths: tuple[str, ...] = ()
    excluded_generated_paths: tuple[str, ...] = ()
    omitted_material_paths: tuple[str, ...] = ()
    unknown_binary_or_large_paths: tuple[str, ...] = ()
    exclusions: tuple[CandidateEvidenceExclusion, ...] = ()
    omissions: tuple[CandidateEvidenceExclusion, ...] = ()
    path_entries: tuple[CandidateDeltaPath, ...] = ()
    patch_text: str = ""
    reason_codes: tuple[str, ...] = ()


def _committed_capture(
    worktree_path: Path,
    *,
    base_ref: str,
    head_ref: str,
    base_ref_available: bool,
) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    if not head_ref or not base_ref_available:
        return (), "", ()
    if head_ref == base_ref:
        return (), "", ()
    name_cp = _run_git(worktree_path, ["diff", "--name-only", f"{base_ref}...HEAD"])
    patch_cp = _run_git(worktree_path, ["diff", "--binary", f"{base_ref}...HEAD"])
    reason_codes: list[str] = []
    if name_cp.returncode != 0 or patch_cp.returncode != 0:
        reason_codes.append("committed_patch_capture_failed")
    files = _stdout_paths(name_cp.stdout) if name_cp.returncode == 0 else ()
    patch = _patch_stdout(patch_cp) if patch_cp.returncode == 0 else ""
    return files, patch, tuple(reason_codes)


def _diff_capture(
    worktree_path: Path,
    *,
    patch_args: list[str],
    name_args: list[str],
    failed_reason: str,
) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    name_cp = _run_git(worktree_path, name_args)
    patch_cp = _run_git(worktree_path, patch_args)
    reason_codes: list[str] = []
    if name_cp.returncode != 0 or patch_cp.returncode != 0:
        reason_codes.append(failed_reason)
    files = _stdout_paths(name_cp.stdout) if name_cp.returncode == 0 else ()
    patch = _patch_stdout(patch_cp) if patch_cp.returncode == 0 else ""
    return files, patch, tuple(reason_codes)


def _candidate_git_status(worktree_path: Path) -> str:
    cp = _run_git(
        worktree_path,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"],
    )
    if cp.returncode != 0:
        return ""
    return cp.stdout.rstrip()


def _status_file_sets(status_porcelain: str) -> _StatusFileSets:
    staged: set[str] = set()
    unstaged: set[str] = set()
    untracked: set[str] = set()
    ignored: set[str] = set()
    for line in status_porcelain.splitlines():
        if not line:
            continue
        if line.startswith("?? "):
            untracked.update(_status_line_paths(line))
            continue
        if line.startswith("!! "):
            ignored.update(_status_line_paths(line))
            continue
        if len(line) < 3:
            continue
        index_status = line[0]
        worktree_status = line[1]
        paths = _status_line_paths(line)
        if index_status not in {" ", "?"}:
            staged.update(paths)
        if worktree_status not in {" ", "?"}:
            unstaged.update(paths)
    return _StatusFileSets(
        staged_files=_sorted_unique(staged),
        unstaged_files=_sorted_unique(unstaged),
        untracked_files=_sorted_unique(untracked),
        ignored_files=_sorted_unique(ignored),
    )


def _status_line_paths(line: str) -> tuple[str, ...]:
    text = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in text:
        parts = tuple(_normalize_rel_path(part) for part in text.split(" -> ") if part.strip())
        return tuple(part for part in parts[-1:] if part)
    normalized = _normalize_rel_path(text)
    return (normalized,) if normalized else ()


def _capture_untracked_paths(
    *,
    worktree_path: Path,
    untracked_paths: tuple[str, ...],
    ignored_paths: tuple[str, ...],
    size_limit: int,
) -> _UntrackedCapture:
    material: list[str] = []
    generated_untracked: list[str] = []
    generated_ignored: list[str] = []
    unknown_binary_or_large: list[str] = []
    exclusions: list[CandidateEvidenceExclusion] = []
    omissions: list[CandidateEvidenceExclusion] = []
    entries: list[CandidateDeltaPath] = []
    patches: list[str] = []
    reason_codes: list[str] = []

    for rel_path in untracked_paths:
        classified = _classify_candidate_path(rel_path, root=worktree_path)
        if classified.is_generated:
            generated_untracked.append(rel_path)
            exclusions.append(
                _exclusion_for_path(worktree_path, rel_path, reason=classified.reason)
            )
            entries.append(
                CandidateDeltaPath(
                    path=rel_path,
                    kind="untracked_generated",
                    tracked=False,
                    classification=classified.label,
                    size_bytes=_safe_size(worktree_path / rel_path),
                    reason=classified.reason,
                )
            )
            continue
        path = worktree_path / rel_path
        try:
            content, mode = _read_untracked_material(
                path,
                rel_path=rel_path,
                root=worktree_path,
                size_limit=size_limit,
            )
        except _OmitMaterialFile as exc:
            unknown_binary_or_large.append(rel_path)
            reason = exc.reason
            omissions.append(_exclusion_for_path(worktree_path, rel_path, reason=reason))
            entries.append(
                CandidateDeltaPath(
                    path=rel_path,
                    kind="unknown_binary_or_large",
                    tracked=False,
                    classification=classified.label,
                    size_bytes=_safe_size(path),
                    sha256=_safe_sha256(path),
                    reason=reason,
                )
            )
            reason_codes.append(reason)
            continue
        except OSError:
            unknown_binary_or_large.append(rel_path)
            omissions.append(
                _exclusion_for_path(
                    worktree_path, rel_path, reason="patch_generation_failed_for_material_path"
                )
            )
            reason_codes.append("patch_generation_failed_for_material_path")
            continue
        material.append(rel_path)
        entries.append(
            CandidateDeltaPath(
                path=rel_path,
                kind="untracked_material",
                tracked=False,
                classification=classified.label,
                size_bytes=_safe_size(path),
                sha256=_sha256_text(content),
            )
        )
        patches.append(_new_file_patch(rel_path, content, mode=mode))

    for rel_path in ignored_paths:
        classified = _classify_candidate_path(rel_path.rstrip("/"), root=worktree_path)
        if classified.is_generated:
            generated_ignored.append(rel_path)
            exclusions.append(
                _exclusion_for_path(worktree_path, rel_path.rstrip("/"), reason=classified.reason)
            )
            entries.append(
                CandidateDeltaPath(
                    path=rel_path,
                    kind="ignored_generated",
                    tracked=False,
                    classification=classified.label,
                    size_bytes=_safe_size(worktree_path / rel_path.rstrip("/")),
                    reason=classified.reason,
                )
            )
        else:
            omissions.append(
                _exclusion_for_path(
                    worktree_path,
                    rel_path.rstrip("/"),
                    reason="ignored_material_path_not_captured",
                )
            )
            reason_codes.append("ignored_material_path_not_captured")

    excluded = _sorted_unique(set(generated_untracked) | set(generated_ignored))
    return _UntrackedCapture(
        material_paths=_sorted_unique(material),
        untracked_generated_paths=_sorted_unique(generated_untracked),
        ignored_generated_paths=_sorted_unique(generated_ignored),
        excluded_generated_paths=excluded,
        omitted_material_paths=_sorted_unique(
            set(unknown_binary_or_large)
            | {
                item.path
                for item in omissions
                if item.reason == "ignored_material_path_not_captured"
            }
        ),
        unknown_binary_or_large_paths=_sorted_unique(unknown_binary_or_large),
        exclusions=tuple(sorted(exclusions, key=lambda item: item.path)),
        omissions=tuple(sorted(omissions, key=lambda item: item.path)),
        path_entries=tuple(sorted(entries, key=lambda item: (item.path, item.kind))),
        patch_text=_join_patch_parts(*patches),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


@dataclass(frozen=True)
class _PathClassification:
    label: str
    is_generated: bool
    reason: str


def _classify_candidate_path(path: str, *, root: Path) -> _PathClassification:
    normalized = _normalize_rel_path(path)
    if is_runtime_artifact_path(normalized, root=root):
        return _PathClassification("runtime_artifact", True, _runtime_artifact_reason(normalized))
    if is_generated_or_vendor_path(normalized):
        return _PathClassification("generated_or_vendor", True, "generated_or_vendor_path")
    return _PathClassification(classify_path(normalized).kind, False, "material")


def _runtime_artifact_reason(path: str) -> str:
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts if part)
    leaf = parts[-1] if parts else ""
    if "__pycache__" in parts or leaf.endswith((".pyc", ".pyo")):
        return "python_runtime_artifact"
    if ".pytest_cache" in parts:
        return "pytest_runtime_artifact"
    if ".mypy_cache" in parts:
        return "mypy_runtime_artifact"
    if ".ruff_cache" in parts:
        return "ruff_runtime_artifact"
    if ".alysis" in parts:
        return "alysis_runtime_artifact"
    return "runtime_artifact"


def _read_untracked_material(
    path: Path, *, rel_path: str, root: Path, size_limit: int
) -> tuple[str, int]:
    if _normalize_rel_path(rel_path).startswith("../") or Path(rel_path).is_absolute():
        raise _OmitMaterialFile("path_outside_worktree")
    try:
        resolved_parent = path.parent.resolve()
        if not _path_within(resolved_parent, root):
            raise _OmitMaterialFile("path_outside_worktree")
    except OSError:
        raise _OmitMaterialFile("path_outside_worktree") from None
    try:
        st = path.lstat()
    except OSError as exc:
        raise OSError from exc
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(path)
        target_parts = PurePosixPath(target.replace("\\", "/")).parts
        if os.path.isabs(target) or ".." in target_parts:
            raise _OmitMaterialFile("unsupported_symlink_change")
        return target, 0o120000
    if not stat.S_ISREG(st.st_mode):
        raise _OmitMaterialFile("unsupported_file_mode_change")
    if st.st_size > max(0, int(size_limit)):
        raise _OmitMaterialFile("unknown_binary_or_large_material_file_omitted")
    data = path.read_bytes()
    if b"\x00" in data:
        raise _OmitMaterialFile("unknown_binary_or_large_material_file_omitted")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _OmitMaterialFile("unknown_binary_or_large_material_file_omitted") from exc
    mode = 0o100755 if st.st_mode & stat.S_IXUSR else 0o100644
    return content, mode


class _OmitMaterialFile(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _new_file_patch(rel_path: str, content: str, *, mode: int = 0o100644) -> str:
    safe_path = _normalize_rel_path(rel_path)
    mode_text = f"{mode:06o}"
    lines = content.splitlines()
    patch_lines = [
        f"diff --git a/{safe_path} b/{safe_path}",
        f"new file mode {mode_text}",
        "index 0000000..0000000",
        "--- /dev/null",
        f"+++ b/{safe_path}",
    ]
    if lines:
        patch_lines.append(f"@@ -0,0 +1,{len(lines)} @@")
        patch_lines.extend(f"+{line}" for line in lines)
        if not content.endswith("\n"):
            patch_lines.append("\\ No newline at end of file")
    return "\n".join(patch_lines) + "\n"


def _deleted_paths(
    worktree_path: Path, *, base_ref: str, base_ref_available: bool
) -> tuple[str, ...]:
    deleted: set[str] = set()
    for args in (
        ["diff", "--name-only", "--diff-filter=D"],
        ["diff", "--cached", "--name-only", "--diff-filter=D"],
    ):
        cp = _run_git(worktree_path, args)
        if cp.returncode == 0:
            deleted.update(_stdout_paths(cp.stdout))
    if base_ref_available:
        cp = _run_git(
            worktree_path, ["diff", "--name-only", "--diff-filter=D", f"{base_ref}...HEAD"]
        )
        if cp.returncode == 0:
            deleted.update(_stdout_paths(cp.stdout))
    return _sorted_unique(deleted)


def _tracked_delta_entries(
    paths: tuple[str, ...],
    *,
    kind: CandidateDeltaPathKind,
) -> tuple[CandidateDeltaPath, ...]:
    return tuple(
        CandidateDeltaPath(
            path=path,
            kind=kind,
            tracked=True,
            classification=classify_path(path).kind,
        )
        for path in paths
    )


def _patch_capture_status(
    *,
    material_changed_paths: tuple[str, ...],
    patch_text: str,
    reason_codes: tuple[str, ...],
) -> PatchCaptureStatus:
    partial_reasons = {
        "base_revision_unavailable",
        "committed_patch_capture_failed",
        "staged_patch_capture_failed",
        "unstaged_patch_capture_failed",
        "unknown_binary_or_large_material_file_omitted",
        "unsupported_symlink_change",
        "patch_generation_failed_for_material_path",
        "material_path_disappeared_during_capture",
        "path_outside_worktree",
        "capture_mismatch",
        "unsupported_file_mode_change",
        "ignored_material_path_not_captured",
    }
    if any(reason in partial_reasons for reason in reason_codes):
        return "partial"
    if not material_changed_paths:
        return "complete"
    return "complete" if patch_text.strip() else "failed"


def _commits_ahead(worktree_path: Path, *, base_ref: str) -> int:
    if not base_ref:
        return 0
    cp = _run_git(worktree_path, ["rev-list", "--count", f"{base_ref}..HEAD"])
    if cp.returncode != 0:
        return 0
    try:
        return int(cp.stdout.strip() or "0")
    except ValueError:
        return 0


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_git_cmd(root, args),
        check=False,
        capture_output=True,
        **command_text(),
        env={
            **os.environ,
            **build_git_process_env(),
        },
    )


def _git_stdout(root: Path, args: list[str]) -> str:
    cp = _run_git(root, args)
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def _git_success(root: Path, args: list[str]) -> bool:
    return _run_git(root, args).returncode == 0


def _stdout_paths(stdout: str) -> tuple[str, ...]:
    return _sorted_unique(line.strip() for line in stdout.splitlines() if line.strip())


def _patch_stdout(cp: subprocess.CompletedProcess[str]) -> str:
    return _ensure_trailing_newline(cp.stdout) if cp.stdout else ""


def _join_patch_parts(*parts: str) -> str:
    patch_text = ""
    for part in parts:
        if not part or not part.strip():
            continue
        if patch_text and not patch_text.endswith("\n"):
            patch_text += "\n"
        patch_text += _ensure_trailing_newline(part)
    return patch_text


def _ensure_trailing_newline(text: str) -> str:
    if not text:
        return ""
    return text if text.endswith("\n") else f"{text}\n"


def _normalize_rel_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().strip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _path_within(path: Path, root: Path | None) -> bool:
    if root is None:
        return True
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_size(path: Path) -> int | None:
    try:
        return path.lstat().st_size
    except OSError:
        return None


def _safe_sha256(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _exclusion_for_path(
    worktree_path: Path, rel_path: str, *, reason: str
) -> CandidateEvidenceExclusion:
    normalized = _normalize_rel_path(rel_path)
    path = worktree_path / normalized
    return CandidateEvidenceExclusion(
        path=normalized,
        reason=reason,
        tracked=False,
        classification=classify_path(normalized).kind,
        size_bytes=_safe_size(path),
        sha256=_safe_sha256(path),
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _patch_status(value: object) -> PatchCaptureStatus:
    raw = str(value or "complete")
    if raw == "ok":
        return "complete"
    if raw in {"complete", "partial", "failed"}:
        return raw  # type: ignore[return-value]
    return "failed"


def _delta_path_kind(value: object) -> CandidateDeltaPathKind:
    raw = str(value or "untracked_material")
    allowed = {
        "committed",
        "staged",
        "unstaged",
        "deleted",
        "untracked_material",
        "untracked_generated",
        "unknown_binary_or_large",
        "ignored_generated",
        "ignored_material",
    }
    return raw if raw in allowed else "untracked_material"  # type: ignore[return-value]


def _tuple_strs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = _normalize_rel_path(value)
        return (normalized,) if normalized else ()
    if not isinstance(value, (list, tuple, set)):
        text = _normalize_rel_path(str(value))
        return (text,) if text else ()
    return _sorted_unique(_normalize_rel_path(str(item)) for item in value if str(item).strip())


def _tuple_dicts(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _sorted_unique(values: object) -> tuple[str, ...]:
    if isinstance(values, set):
        iterable = values
    else:
        iterable = values if isinstance(values, (list, tuple, set)) else tuple(values)  # type: ignore[arg-type]
    return tuple(sorted({str(item) for item in iterable if str(item).strip()}))


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
