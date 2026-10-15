"""Import-safe sequential Forge execution.

This module owns the state machine shared by the legacy ``forge run`` command
and the Forge Host adapter.  It deliberately has no Typer, Click, Rich, or CLI
global dependencies: callers provide every workspace mutation and task
execution operation through :class:`SequentialRunDependencies`.

The engine is sequential by construction.  It selects one scheduler candidate,
executes it, persists the updated attempt count, then reloads scheduler state
before selecting another candidate.  Host Protocol v2 Phase 0 additionally
passes an explicit capability set in which repair, review, remotes, assets,
MCP, subagents, and parallelism are all disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from .atomic_io import atomic_write_text
from .error_text import sanitize_error_summary
from .execution_deadline import ExecutionDeadline
from .forge import RunPaths
from .forge_host_verification import (
    HostVerificationBinding,
    HostVerificationResult,
    RetainedArtifact,
    retained_artifact_matches,
)
from .run_state import (
    RUN_STATUS_APPROVED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_INTERRUPTED,
    RUN_STATUS_RUNNING,
)
from .swarm_scheduler import (
    SUCCESSFUL_TERMINAL_STATUSES,
    TaskCandidate,
    canonical_task_status,
    select_task_candidates,
)
from .usage_tracker import aggregate_usage_from_session_logs

_NON_EXECUTABLE_TASK_STATUSES = frozenset({"superseded", "invalidated"})

SequentialStoppedReason = Literal[
    "completed",
    "prepared_for_commit",
    "dry_run",
    "max_tasks",
    "task_failed",
    "task_error",
    "deadline_exhausted",
    "blocked",
    "engine_error",
]

IntegrationTransactionState = Literal[
    "PREPARED",
    "COMMITTED_REVERSIBLE",
    "PUBLISH_READY",
    "SEALED",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
]


@dataclass(frozen=True, slots=True)
class ExecutionUsage:
    """Provider usage facts without inventing a cost when any call is unknown."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_known: bool = True
    total_cost_micros: int | None = 0
    usage_complete: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionTiming:
    """Wall timestamps for audit plus monotonic elapsed/timeout truth."""

    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: int = 0
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class BranchFacts:
    """Git facts observed by native execution, never inferred by the Host."""

    task_branch: str | None = None
    base_branch: str | None = None
    base_head: str | None = None
    commit: str | None = None
    merge_commit: str | None = None
    merge_first_parent: str | None = None
    merge_second_parent: str | None = None
    base_ref_after_merge: str | None = None
    merge_result: str | None = None
    merged: bool = False
    branch_retained: bool = False


@dataclass(frozen=True, slots=True)
class IntegrationCommitProof:
    """Internal proof that the approved base ref crossed the atomic commit point."""

    base_branch: str
    approved_base_head: str
    task_commit: str
    merge_commit: str
    integration_committed_elapsed_ms: int


@dataclass(frozen=True, slots=True)
class IntegrationFinalizeProof:
    """Private proof that reversible native finalization reached publication."""

    commit_proof: IntegrationCommitProof
    final_elapsed_ms: int


@dataclass(frozen=True, slots=True)
class PreparedIntegrationFacts:
    """Immutable authority for a merge prepared but not yet published."""

    transaction_id: str
    native_run_id: str
    task_id: str
    base_branch: str
    approved_base_head: str
    task_branch: str
    task_commit: str
    prepared_merge_commit: str
    prepared_ref: str
    prepared_elapsed_ms: int
    journal_path: Path


@dataclass(frozen=True, slots=True)
class NativeFilePreimage:
    """Exact text-file state restored when a reversible integration aborts."""

    relative_path: str
    existed: bool
    text: str
    mode: int

    def to_payload(self) -> dict[str, object]:
        return {
            "relativePath": self.relative_path,
            "existed": self.existed,
            "text": self.text,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class NativeAncestorPreimage:
    """Identity of a directory ancestor protecting a native state file."""

    relative_path: str
    device: int
    inode: int
    mode: int

    def to_payload(self) -> dict[str, object]:
        return {
            "relativePath": self.relative_path,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class NativeRunStatePreimage:
    """Approval-time native plan and lifecycle files held by the transaction."""

    workspace_root: Path
    workspace_device: int
    workspace_inode: int
    ancestors: tuple[NativeAncestorPreimage, ...]
    files: tuple[NativeFilePreimage, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "workspaceRoot": os.fspath(self.workspace_root),
            "workspaceDevice": self.workspace_device,
            "workspaceInode": self.workspace_inode,
            "ancestors": [item.to_payload() for item in self.ancestors],
            "files": [item.to_payload() for item in self.files],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> NativeRunStatePreimage:
        """Rebuild a journaled preimage without accepting loose JSON coercions."""

        workspace_raw = payload.get("workspaceRoot")
        device = payload.get("workspaceDevice")
        inode = payload.get("workspaceInode")
        ancestors_raw = payload.get("ancestors")
        files_raw = payload.get("files")
        if (
            not isinstance(workspace_raw, str)
            or not workspace_raw
            or not Path(workspace_raw).is_absolute()
            or isinstance(device, bool)
            or not isinstance(device, int)
            or device < 0
            or isinstance(inode, bool)
            or not isinstance(inode, int)
            or inode < 0
            or not isinstance(ancestors_raw, list)
            or not isinstance(files_raw, list)
        ):
            raise PreparedIntegrationRollbackError("transaction journal native preimage is invalid")
        ancestors: list[NativeAncestorPreimage] = []
        ancestor_seen: set[str] = set()
        for raw in ancestors_raw:
            if not isinstance(raw, Mapping):
                raise PreparedIntegrationRollbackError(
                    "transaction journal native ancestor preimage is invalid"
                )
            relative = raw.get("relativePath")
            ancestor_device = raw.get("device")
            ancestor_inode = raw.get("inode")
            mode = raw.get("mode")
            if (
                not isinstance(relative, str)
                or not relative
                or relative in ancestor_seen
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or isinstance(ancestor_device, bool)
                or not isinstance(ancestor_device, int)
                or ancestor_device < 0
                or isinstance(ancestor_inode, bool)
                or not isinstance(ancestor_inode, int)
                or ancestor_inode < 0
                or isinstance(mode, bool)
                or not isinstance(mode, int)
                or mode < 0
                or mode > 0o7777
            ):
                raise PreparedIntegrationRollbackError(
                    "transaction journal native ancestor preimage is invalid"
                )
            ancestor_seen.add(relative)
            ancestors.append(
                NativeAncestorPreimage(
                    relative_path=relative,
                    device=ancestor_device,
                    inode=ancestor_inode,
                    mode=mode,
                )
            )
        files: list[NativeFilePreimage] = []
        seen: set[str] = set()
        for raw in files_raw:
            if not isinstance(raw, Mapping):
                raise PreparedIntegrationRollbackError(
                    "transaction journal native file preimage is invalid"
                )
            relative = raw.get("relativePath")
            existed = raw.get("existed")
            text = raw.get("text")
            mode = raw.get("mode")
            if (
                not isinstance(relative, str)
                or not relative
                or relative in seen
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(existed, bool)
                or not isinstance(text, str)
                or isinstance(mode, bool)
                or not isinstance(mode, int)
                or mode < 0
                or mode > 0o7777
            ):
                raise PreparedIntegrationRollbackError(
                    "transaction journal native file preimage is invalid"
                )
            seen.add(relative)
            files.append(
                NativeFilePreimage(
                    relative_path=relative,
                    existed=existed,
                    text=text,
                    mode=mode,
                )
            )
        required_ancestors: set[str] = set()
        for item in files:
            parent = Path()
            for part in Path(item.relative_path).parts[:-1]:
                parent = parent / part
                required_ancestors.add(parent.as_posix())
        if ancestor_seen != required_ancestors:
            raise PreparedIntegrationRollbackError(
                "transaction journal native ancestor coverage is invalid"
            )
        return cls(
            workspace_root=Path(workspace_raw),
            workspace_device=device,
            workspace_inode=inode,
            ancestors=tuple(ancestors),
            files=tuple(files),
        )


class PreparedIntegrationError(RuntimeError):
    """A prepared integration could not transition safely."""


class PreparedIntegrationRollbackError(PreparedIntegrationError):
    """Rollback did not restore every authoritative native/Git fact."""


def _capture_native_run_state(paths: RunPaths) -> NativeRunStatePreimage:
    root = paths.root.resolve(strict=True)
    root_stat = root.stat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise PreparedIntegrationError("native transaction workspace is not a directory")
    candidates = (
        paths.plan_json_path,
        paths.plan_md_path,
        paths.runtime_dir / "current_run.json",
        paths.execution_dir / "sequential_summary.json",
        paths.execution_dir / "sequential_run.jsonl",
    )
    entries: list[NativeFilePreimage] = []
    ancestor_entries: dict[str, NativeAncestorPreimage] = {}
    lexical_root = Path(os.path.abspath(paths.root))
    for raw_path in candidates:
        lexical_path = Path(os.path.abspath(raw_path))
        try:
            relative = lexical_path.relative_to(lexical_root).as_posix()
            path = _secure_native_target(root, relative)
        except (OSError, ValueError) as exc:
            raise PreparedIntegrationError(
                "native transaction preimage escaped the workspace"
            ) from exc
        ancestor = root
        relative_ancestor = Path()
        for part in Path(relative).parts[:-1]:
            ancestor = ancestor / part
            relative_ancestor = relative_ancestor / part
            try:
                ancestor_stat = ancestor.lstat()
            except OSError as exc:
                raise PreparedIntegrationError(
                    "native transaction preimage ancestor is unavailable"
                ) from exc
            if stat.S_ISLNK(ancestor_stat.st_mode) or not stat.S_ISDIR(ancestor_stat.st_mode):
                raise PreparedIntegrationError("native transaction preimage ancestor changed type")
            ancestor_relative = relative_ancestor.as_posix()
            current = NativeAncestorPreimage(
                relative_path=ancestor_relative,
                device=ancestor_stat.st_dev,
                inode=ancestor_stat.st_ino,
                mode=stat.S_IMODE(ancestor_stat.st_mode),
            )
            previous = ancestor_entries.setdefault(ancestor_relative, current)
            if previous != current:
                raise PreparedIntegrationError(
                    "native transaction preimage ancestor identity changed"
                )
        if path.is_symlink():
            raise PreparedIntegrationError("native transaction preimage contains a symlink")
        try:
            before = path.stat()
        except FileNotFoundError:
            entries.append(
                NativeFilePreimage(
                    relative_path=relative,
                    existed=False,
                    text="",
                    mode=0,
                )
            )
            continue
        except OSError as exc:
            raise PreparedIntegrationError("native transaction preimage is unavailable") from exc
        if not stat.S_ISREG(before.st_mode):
            raise PreparedIntegrationError("native transaction preimage is not a regular file")
        try:
            text = path.read_text(encoding="utf-8")
            after = path.stat()
        except (OSError, UnicodeError) as exc:
            raise PreparedIntegrationError("native transaction preimage could not be read") from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PreparedIntegrationError("native transaction preimage changed while being read")
        entries.append(
            NativeFilePreimage(
                relative_path=relative,
                existed=True,
                text=text,
                mode=stat.S_IMODE(after.st_mode),
            )
        )
    return NativeRunStatePreimage(
        workspace_root=root,
        workspace_device=root_stat.st_dev,
        workspace_inode=root_stat.st_ino,
        ancestors=tuple(ancestor_entries[key] for key in sorted(ancestor_entries)),
        files=tuple(entries),
    )


def _secure_native_target(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PreparedIntegrationRollbackError("native rollback path is not relative")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise PreparedIntegrationRollbackError(
                "native rollback ancestor is unavailable"
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise PreparedIntegrationRollbackError("native rollback ancestor changed type")
    return current / relative.parts[-1]


def _validate_native_run_state_targets(preimage: NativeRunStatePreimage) -> Path:
    """Prove rollback targets still name the captured workspace objects."""

    root = preimage.workspace_root.resolve(strict=True)
    root_stat = root.stat()
    if (root_stat.st_dev, root_stat.st_ino) != (
        preimage.workspace_device,
        preimage.workspace_inode,
    ):
        raise PreparedIntegrationRollbackError("native rollback workspace identity changed")
    for ancestor in preimage.ancestors:
        candidate = _secure_native_target(
            root,
            f"{ancestor.relative_path}/.alysis-ancestor-probe",
        ).parent
        try:
            current = candidate.lstat()
        except OSError as exc:
            raise PreparedIntegrationRollbackError(
                "native rollback ancestor is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino, stat.S_IMODE(current.st_mode))
            != (ancestor.device, ancestor.inode, ancestor.mode)
        ):
            raise PreparedIntegrationRollbackError("native rollback ancestor identity changed")
    for item in preimage.files:
        candidate = _secure_native_target(root, item.relative_path)
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PreparedIntegrationRollbackError("native rollback target is unavailable") from exc
        if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISREG(candidate_stat.st_mode):
            raise PreparedIntegrationRollbackError("native rollback target changed type")
    return root


def _restore_native_run_state(preimage: NativeRunStatePreimage) -> None:
    root = _validate_native_run_state_targets(preimage)
    for item in preimage.files:
        candidate = _secure_native_target(root, item.relative_path)
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            candidate_stat = None
        except OSError as exc:
            raise PreparedIntegrationRollbackError("native rollback target is unavailable") from exc
        if candidate_stat is not None and stat.S_ISLNK(candidate_stat.st_mode):
            raise PreparedIntegrationRollbackError("native rollback target became a symlink")
        if item.existed:
            if candidate_stat is not None and not stat.S_ISREG(candidate_stat.st_mode):
                raise PreparedIntegrationRollbackError(
                    "native rollback target is no longer a regular file"
                )
            atomic_write_text(candidate, item.text)
            candidate.chmod(item.mode)
        elif candidate_stat is not None:
            if not stat.S_ISREG(candidate_stat.st_mode):
                raise PreparedIntegrationRollbackError(
                    "native rollback cannot remove a non-file target"
                )
            candidate.unlink()


def _capture_bound_native_run_state(
    template: NativeRunStatePreimage,
) -> NativeRunStatePreimage:
    """Capture exact current bytes at the already-authorized native paths."""

    root = _validate_native_run_state_targets(template)
    entries: list[NativeFilePreimage] = []
    for item in template.files:
        candidate = _secure_native_target(root, item.relative_path)
        try:
            before = candidate.lstat()
        except FileNotFoundError:
            entries.append(
                NativeFilePreimage(
                    relative_path=item.relative_path,
                    existed=False,
                    text="",
                    mode=0,
                )
            )
            continue
        except OSError as exc:
            raise PreparedIntegrationError("native transaction postimage is unavailable") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PreparedIntegrationError("native transaction postimage changed type")
        try:
            text = candidate.read_text(encoding="utf-8")
            after = candidate.lstat()
        except (OSError, UnicodeError) as exc:
            raise PreparedIntegrationError(
                "native transaction postimage could not be read"
            ) from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PreparedIntegrationError("native transaction postimage changed while being read")
        entries.append(
            NativeFilePreimage(
                relative_path=item.relative_path,
                existed=True,
                text=text,
                mode=stat.S_IMODE(after.st_mode),
            )
        )
    return NativeRunStatePreimage(
        workspace_root=template.workspace_root,
        workspace_device=template.workspace_device,
        workspace_inode=template.workspace_inode,
        ancestors=template.ancestors,
        files=tuple(entries),
    )


def restore_native_run_state_payload(payload: Mapping[str, object]) -> None:
    """Strictly restore the native preimage carried by a recovery journal."""

    _restore_native_run_state(NativeRunStatePreimage.from_payload(payload))


def validate_native_run_state_payload(payload: Mapping[str, object]) -> None:
    """Validate every recovery target without mutating Git or native state."""

    _validate_native_run_state_targets(NativeRunStatePreimage.from_payload(payload))


def validate_native_run_state_matches_payload(payload: Mapping[str, object]) -> None:
    """Require current native bytes/types to equal a journaled exact postimage."""

    expected = NativeRunStatePreimage.from_payload(payload)
    if _capture_bound_native_run_state(expected) != expected:
        raise PreparedIntegrationRollbackError(
            "native transaction postimage changed before publication"
        )


class PreparedIntegration:
    """Opaque journaled transaction spanning engine, bridge, and Host service."""

    def __init__(
        self,
        *,
        facts: PreparedIntegrationFacts,
        execution_deadline: ExecutionDeadline,
        bind_guarded_state: Callable[[], None],
        commit_git: Callable[[], IntegrationCommitProof],
        rollback_git: Callable[[IntegrationCommitProof | None], None],
        cleanup_prepared_git: Callable[[], None],
        validate_final_authority: Callable[[], None],
    ) -> None:
        self._facts = facts
        self._execution_deadline = execution_deadline
        self._bind_guarded_state = bind_guarded_state
        self._commit_git = commit_git
        self._rollback_git = rollback_git
        self._cleanup_prepared_git = cleanup_prepared_git
        self._validate_final_authority = validate_final_authority
        self._state: IntegrationTransactionState = "PREPARED"
        self._proof: IntegrationCommitProof | None = None
        self._finalize_proof: IntegrationFinalizeProof | None = None
        self._guard: MutationGuard | None = None
        self._native_preimage: NativeRunStatePreimage | None = None
        self._native_postimage: NativeRunStatePreimage | None = None
        self._finalize_native: Callable[[IntegrationCommitProof], None] | None = None
        self._lock = threading.RLock()
        self._journal_adopted = False
        self._journal_root: Path | None = None
        self._advisory_warnings: list[str] = []
        transaction_suffix = facts.transaction_id.removeprefix("integration_")
        if (
            len(transaction_suffix) != 24
            or any(character not in "0123456789abcdef" for character in transaction_suffix)
            or facts.prepared_ref != f"refs/alysis/forge-host-v2/{facts.transaction_id}"
            or facts.journal_path.name != f"{facts.transaction_id}.json"
            or not facts.journal_path.is_absolute()
            or facts.prepared_elapsed_ms < 0
            or not execution_deadline.enabled
            or execution_deadline.deadline_monotonic is None
            or facts.prepared_elapsed_ms
            >= round(
                (execution_deadline.deadline_monotonic - execution_deadline.started_at_monotonic)
                * 1000
            )
        ):
            raise PreparedIntegrationError("prepared integration authority is invalid")

    @property
    def facts(self) -> PreparedIntegrationFacts:
        return self._facts

    @property
    def state(self) -> IntegrationTransactionState:
        with self._lock:
            return self._state

    @property
    def integration_proof(self) -> IntegrationCommitProof | None:
        with self._lock:
            return self._proof

    @property
    def advisory_warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._advisory_warnings)

    @property
    def guard_release_pending(self) -> bool:
        """Whether a sealed transaction still owns a guard needing retry."""

        with self._lock:
            return self._state == "SEALED" and self._guard is not None

    def _journal_payload(
        self,
        *,
        state: IntegrationTransactionState,
        reason: str | None = None,
        rollback_error: str | None = None,
    ) -> dict[str, object]:
        proof = self._proof
        payload: dict[str, object] = {
            "schema": "alysis.forge-host.integration-transaction/v1",
            "state": state,
            "transactionId": self._facts.transaction_id,
            "nativeRunId": self._facts.native_run_id,
            "taskId": self._facts.task_id,
            "baseBranch": self._facts.base_branch,
            "approvedBaseHead": self._facts.approved_base_head,
            "taskBranch": self._facts.task_branch,
            "taskCommit": self._facts.task_commit,
            "preparedMergeCommit": self._facts.prepared_merge_commit,
            "preparedRef": self._facts.prepared_ref,
            "preparedElapsedMs": self._facts.prepared_elapsed_ms,
            "journalPath": os.fspath(self._facts.journal_path),
            "manualAttention": state == "ROLLBACK_FAILED",
        }
        if self._native_preimage is not None:
            payload["nativePreimage"] = self._native_preimage.to_payload()
        if self._native_postimage is not None:
            payload["nativePostimage"] = self._native_postimage.to_payload()
        if proof is not None:
            payload["integrationProof"] = {
                "baseBranch": proof.base_branch,
                "approvedBaseHead": proof.approved_base_head,
                "taskCommit": proof.task_commit,
                "mergeCommit": proof.merge_commit,
                "integrationCommittedElapsedMs": proof.integration_committed_elapsed_ms,
            }
        if self._finalize_proof is not None:
            payload["finalizeProof"] = {
                "finalElapsedMs": self._finalize_proof.final_elapsed_ms,
            }
        if reason:
            payload["reason"] = sanitize_error_summary(reason, max_chars=320)
        if rollback_error:
            payload["rollbackError"] = sanitize_error_summary(
                rollback_error,
                max_chars=320,
            )
        return payload

    def _write_journal(
        self,
        state: IntegrationTransactionState,
        *,
        reason: str | None = None,
        rollback_error: str | None = None,
    ) -> None:
        self._validate_journal_target()
        atomic_write_text(
            self._facts.journal_path,
            json.dumps(
                self._journal_payload(
                    state=state,
                    reason=reason,
                    rollback_error=rollback_error,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    def _validate_journal_target(self) -> None:
        if self._native_preimage is None:
            raise PreparedIntegrationError("transaction journal lacks a native root binding")
        root = self._native_preimage.workspace_root.resolve(strict=True)
        root_stat = root.stat()
        if (root_stat.st_dev, root_stat.st_ino) != (
            self._native_preimage.workspace_device,
            self._native_preimage.workspace_inode,
        ):
            raise PreparedIntegrationError("transaction journal workspace identity changed")
        journal_root = self._journal_root
        if journal_root is None:
            raise PreparedIntegrationError("transaction journal root is not bound")
        journal = self._facts.journal_path
        if not journal.is_absolute():
            raise PreparedIntegrationError("transaction journal path is not absolute")
        if journal != journal_root / f"{self._facts.transaction_id}.json":
            raise PreparedIntegrationError("transaction journal path is not canonical")
        try:
            relative = journal.relative_to(root)
        except ValueError as exc:
            raise PreparedIntegrationError(
                "transaction journal path escaped the workspace"
            ) from exc
        if not relative.parts or ".." in relative.parts:
            raise PreparedIntegrationError("transaction journal path is not canonical")
        parent = root
        for part in relative.parts[:-1]:
            parent = parent / part
            try:
                parent_stat = parent.lstat()
            except FileNotFoundError:
                try:
                    parent.mkdir()
                    parent_stat = parent.lstat()
                except OSError as exc:
                    raise PreparedIntegrationError(
                        "transaction journal directory could not be created"
                    ) from exc
            except OSError as exc:
                raise PreparedIntegrationError(
                    "transaction journal directory is unavailable"
                ) from exc
            if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                raise PreparedIntegrationError("transaction journal directory changed type")
        try:
            journal_stat = journal.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PreparedIntegrationError("transaction journal is unavailable") from exc
        if stat.S_ISLNK(journal_stat.st_mode) or not stat.S_ISREG(journal_stat.st_mode):
            raise PreparedIntegrationError("transaction journal changed type")

    def _adopt_engine_authority(
        self,
        *,
        guard: MutationGuard,
        native_preimage: NativeRunStatePreimage,
        journal_root: Path,
        finalize_native: Callable[[IntegrationCommitProof], None],
    ) -> None:
        with self._lock:
            if self._journal_adopted or self._guard is not None:
                raise PreparedIntegrationError("prepared integration engine authority is one-shot")
            if self._state != "PREPARED":
                raise PreparedIntegrationError(
                    "only a prepared integration can adopt engine authority"
                )
            self._guard = guard
            self._native_preimage = native_preimage
            self._journal_root = journal_root
            self._finalize_native = finalize_native
            try:
                self._bind_guarded_state()
                self._write_journal("PREPARED")
            except Exception as exc:
                try:
                    self._rollback_git(None)
                    _restore_native_run_state(native_preimage)
                    self._write_journal(
                        "ROLLED_BACK",
                        reason="prepared integration adoption failed",
                    )
                    self._state = "ROLLED_BACK"
                    self._release_guard_after_terminal_state(advisory=False)
                except Exception as rollback_exc:  # noqa: BLE001
                    self._state = "ROLLBACK_FAILED"
                    try:
                        self._write_journal(
                            "ROLLBACK_FAILED",
                            reason="prepared integration adoption failed",
                            rollback_error=sanitize_error_summary(
                                str(rollback_exc) or rollback_exc.__class__.__name__
                            ),
                        )
                    except Exception:
                        pass
                    raise PreparedIntegrationRollbackError(
                        "prepared integration adoption cleanup failed"
                    ) from rollback_exc
                raise PreparedIntegrationError("prepared integration adoption failed") from exc
            self._journal_adopted = True

    def _require_adopted(self) -> None:
        if (
            not self._journal_adopted
            or self._guard is None
            or self._native_preimage is None
            or self._finalize_native is None
        ):
            raise PreparedIntegrationError(
                "prepared integration has not adopted guarded engine authority"
            )

    def _check_deadline(self, phase: str) -> None:
        if self._execution_deadline.is_exhausted():
            raise PreparedIntegrationError(
                f"run deadline exhausted before Host integration {phase}"
            )

    def _elapsed_before_deadline_ms(self, phase: str) -> int:
        elapsed = self._execution_deadline.elapsed_seconds()
        deadline = self._execution_deadline.deadline_monotonic
        if deadline is None:
            raise PreparedIntegrationError("Host integration deadline is unbounded")
        span = deadline - self._execution_deadline.started_at_monotonic
        if elapsed >= span:
            raise PreparedIntegrationError(
                f"run deadline exhausted before Host integration {phase}"
            )
        return max(0, round(elapsed * 1000))

    def _release_guard_after_terminal_state(self, *, advisory: bool) -> None:
        guard = self._guard
        if guard is None:
            return
        try:
            guard.release()
        except Exception as exc:  # noqa: BLE001 - sealed release is advisory
            message = sanitize_error_summary(str(exc) or exc.__class__.__name__)
            self._advisory_warnings.append(
                "mutation guard release failed after terminal transaction state: " + message
            )
            if not advisory:
                raise PreparedIntegrationRollbackError(
                    "mutation guard release failed after rollback"
                ) from exc
        else:
            self._guard = None

    def _mark_rollback_failed(self, *, reason: str, exc: Exception) -> None:
        self._state = "ROLLBACK_FAILED"
        error = sanitize_error_summary(str(exc) or exc.__class__.__name__)
        try:
            self._write_journal(
                "ROLLBACK_FAILED",
                reason=reason,
                rollback_error=error,
            )
        except Exception as journal_exc:  # noqa: BLE001 - preserve both failures
            error += "; rollback-failure journal could not be written: " + sanitize_error_summary(
                str(journal_exc) or journal_exc.__class__.__name__
            )
        raise PreparedIntegrationRollbackError(
            "Host integration rollback failed; manual attention is required: " + error
        ) from exc

    def _rollback_locked(self, *, reason: str) -> None:
        if self._state in {"ROLLED_BACK", "SEALED"}:
            if self._state == "SEALED":
                raise PreparedIntegrationError("a sealed integration cannot be rolled back")
            return
        try:
            self._rollback_git(self._proof)
            if self._native_preimage is not None:
                _restore_native_run_state(self._native_preimage)
            self._write_journal("ROLLED_BACK", reason=reason)
            self._state = "ROLLED_BACK"
            self._release_guard_after_terminal_state(advisory=False)
        except Exception as exc:  # noqa: BLE001 - durable manual-attention state
            self._mark_rollback_failed(reason=reason, exc=exc)

    def commit_reversible(self) -> IntegrationCommitProof:
        with self._lock:
            self._require_adopted()
            if self._state != "PREPARED":
                raise PreparedIntegrationError(
                    "only a prepared integration can be committed reversibly"
                )
            try:
                self._check_deadline("commit")
                proof = self._commit_git()
                if (
                    proof.base_branch != self._facts.base_branch
                    or proof.approved_base_head != self._facts.approved_base_head
                    or proof.task_commit != self._facts.task_commit
                    or proof.merge_commit != self._facts.prepared_merge_commit
                    or proof.integration_committed_elapsed_ms < 0
                ):
                    raise PreparedIntegrationError(
                        "prepared integration commit proof contradicted its authority"
                    )
                self._proof = proof
                self._check_deadline("post-CAS validation")
                self._write_journal("COMMITTED_REVERSIBLE")
                self._state = "COMMITTED_REVERSIBLE"
                return proof
            except Exception as exc:  # noqa: BLE001 - every failed commit rolls back
                self._rollback_locked(
                    reason="reversible Host integration commit failed: "
                    + sanitize_error_summary(str(exc) or exc.__class__.__name__)
                )
                if isinstance(exc, PreparedIntegrationError):
                    raise
                raise PreparedIntegrationError("reversible Host integration commit failed") from exc

    def rollback(self, reason: str) -> None:
        with self._lock:
            self._require_adopted()
            self._rollback_locked(reason=reason)

    def abort(self, reason: str) -> None:
        with self._lock:
            if self._state != "PREPARED":
                raise PreparedIntegrationError("abort is only valid before the reversible commit")
            if not self._journal_adopted:
                try:
                    self._rollback_git(None)
                except Exception as exc:  # noqa: BLE001
                    self._state = "ROLLBACK_FAILED"
                    raise PreparedIntegrationRollbackError(
                        "unadopted prepared integration cleanup failed"
                    ) from exc
                self._state = "ROLLED_BACK"
                return
            self._rollback_locked(reason=reason)

    def finalize_reversible(self) -> IntegrationFinalizeProof:
        with self._lock:
            self._require_adopted()
            if self._state != "COMMITTED_REVERSIBLE" or self._proof is None:
                raise PreparedIntegrationError(
                    "only a reversible committed integration can be finalized"
                )
            try:
                self._check_deadline("native finalization")
                self._cleanup_prepared_git()
                self._finalize_native(self._proof)
                assert self._native_preimage is not None
                self._native_postimage = _capture_bound_native_run_state(self._native_preimage)
                self._validate_final_authority()
                final_elapsed_ms = self._elapsed_before_deadline_ms(
                    "authoritative publication staging"
                )
                self._finalize_proof = IntegrationFinalizeProof(
                    commit_proof=self._proof,
                    final_elapsed_ms=final_elapsed_ms,
                )
                self._write_journal("PUBLISH_READY")
                self._elapsed_before_deadline_ms("publication-ready journal finalization")
                self._state = "PUBLISH_READY"
                return self._finalize_proof
            except Exception as exc:  # noqa: BLE001 - still reversible
                self._rollback_locked(
                    reason="Host integration reversible finalization failed: "
                    + sanitize_error_summary(str(exc) or exc.__class__.__name__)
                )
                if isinstance(exc, PreparedIntegrationError):
                    raise
                raise PreparedIntegrationError(
                    "Host integration reversible finalization failed"
                ) from exc

    def complete_publish(
        self,
        *,
        event_bytes_digest: str,
        event_bytes: int,
    ) -> IntegrationFinalizeProof:
        with self._lock:
            self._require_adopted()
            if self._state != "PUBLISH_READY" or self._finalize_proof is None:
                raise PreparedIntegrationError("only a publication-ready integration can be sealed")
            if (
                len(event_bytes_digest) != 64
                or event_bytes_digest != event_bytes_digest.lower()
                or any(character not in "0123456789abcdef" for character in event_bytes_digest)
                or isinstance(event_bytes, bool)
                or not isinstance(event_bytes, int)
                or event_bytes < 1
            ):
                raise PreparedIntegrationError("authoritative publication byte binding is invalid")
            self._check_deadline("authoritative publication")
            original_proof = self._finalize_proof
            try:
                if self._native_postimage is None:
                    raise PreparedIntegrationError(
                        "authoritative publication lacks a native postimage"
                    )
                if (
                    _capture_bound_native_run_state(self._native_postimage)
                    != self._native_postimage
                ):
                    raise PreparedIntegrationError(
                        "native final state changed before authoritative publication"
                    )
                self._validate_final_authority()
                self._check_deadline("authoritative publication journal write")
                payload = self._journal_payload(state="SEALED")
                payload["authoritativePublication"] = {
                    "eventBytesDigest": event_bytes_digest,
                    "eventBytes": event_bytes,
                }
                self._validate_journal_target()
                atomic_write_text(
                    self._facts.journal_path,
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                )
                # PUBLISH_READY timing is already bound into the staged event
                # bytes.  This post-fsync sample is only a publication gate:
                # crossing the shared deadline leaves the transaction
                # reversible instead of inventing a later terminal elapsed.
                self._elapsed_before_deadline_ms("authoritative publication journal finalization")
            except Exception as exc:  # noqa: BLE001 - restore reversible truth
                try:
                    self._state = "PUBLISH_READY"
                    self._finalize_proof = original_proof
                    self._write_journal(
                        "PUBLISH_READY",
                        reason="authoritative Host publication seal was rejected",
                    )
                except Exception as recovery_exc:  # noqa: BLE001
                    self._mark_rollback_failed(
                        reason="authoritative Host publication seal recovery failed",
                        exc=recovery_exc,
                    )
                raise PreparedIntegrationError(
                    "authoritative Host publication could not be sealed"
                ) from exc
            self._state = "SEALED"
            # Once the durable SEALED state exists, guard release is advisory;
            # it must never cause service publication to roll back or fail.
            self._release_guard_after_terminal_state(advisory=True)
            return original_proof

    def retry_guard_release(self) -> bool:
        """Retry an advisory guard release after durable Host publication."""

        with self._lock:
            if self._state != "SEALED":
                raise PreparedIntegrationError(
                    "mutation guard release retry requires a sealed integration"
                )
            self._release_guard_after_terminal_state(advisory=True)
            return self._guard is None

    def seal(self) -> IntegrationCommitProof:
        """Fail closed for callers that have not adopted two-phase publication."""

        raise PreparedIntegrationError(
            "seal() is unavailable; use finalize_reversible() and complete_publish()"
        )


@dataclass
class TaskExecutionOutcome:
    """Structured result of one native Forge task execution.

    The leading fields intentionally retain the original ``forge exec`` result
    constructor.  New fields bind the evidence a Host adapter needs without
    forcing the adapter to scrape terminal output or mutable plan state.
    """

    task_id: str
    title: str
    status: str
    success: bool
    exit_code: int
    summary: str
    report_path: Path
    patch_path: Path
    merge_conflict: bool
    conflict_review_path: Path | None
    payload: dict[str, Any] = field(default_factory=dict)
    verification: tuple[HostVerificationResult, ...] = ()
    artifacts: tuple[RetainedArtifact, ...] = ()
    retained_log_paths: tuple[Path, ...] = ()
    usage: ExecutionUsage = field(default_factory=ExecutionUsage)
    timing: ExecutionTiming = field(default_factory=ExecutionTiming)
    branch_facts: BranchFacts = field(default_factory=BranchFacts)
    integration_commit_proof: IntegrationCommitProof | None = None
    completion_ready: bool = False
    prepared_integration: PreparedIntegration | None = None
    completion_consistent: bool = False
    completion_issues: tuple[str, ...] = ()


class SequentialTaskExecutionError(Exception):
    """A task could not execute at all (as distinct from rejected work)."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "forge_error",
        task_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = str(kind)
        self.task_id = task_id


@dataclass(frozen=True, slots=True)
class Phase0ExecutionControls:
    """Closed Phase-0 capability surface passed to the native executor."""

    repair_enabled: bool = False
    review_enabled: bool = False
    remotes_enabled: bool = False
    assets_enabled: bool = False
    mcp_enabled: bool = False
    subagents_enabled: bool = False
    parallelism: int = 1

    def is_phase0_safe(self) -> bool:
        return (
            not self.repair_enabled
            and not self.review_enabled
            and not self.remotes_enabled
            and not self.assets_enabled
            and not self.mcp_enabled
            and not self.subagents_enabled
            and self.parallelism == 1
        )


@dataclass(frozen=True, slots=True)
class SequentialRunRequest:
    """Immutable authority and scheduling inputs for one sequential run."""

    native_run_id: str
    native_paths: RunPaths
    retry_failed: bool = False
    retry_changes_requested: bool = False
    max_attempts: int | None = None
    only_ids: frozenset[str] | None = None
    max_tasks: int | None = None
    keep_going: bool = False
    git_flow: bool = True
    verify_mode: str = "warn"
    scope_mode: str = "strict"
    base_branch: str | None = None
    base_head: str | None = None
    started_at: str | None = None
    execution_deadline: ExecutionDeadline | None = None
    dry_run: bool = False
    phase0: bool = False
    phase0_controls: Phase0ExecutionControls = field(default_factory=Phase0ExecutionControls)
    required_verification_bindings: tuple[HostVerificationBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class SequentialTaskRequest:
    """One scheduler admission handed to the explicitly bound executor."""

    native_run_id: str
    native_paths: RunPaths
    plan: dict[str, Any]
    task: dict[str, Any]
    task_id: str
    position: int
    base_branch: str | None
    base_head: str | None
    execution_deadline: ExecutionDeadline | None
    phase0: bool
    phase0_controls: Phase0ExecutionControls


@dataclass(frozen=True, slots=True)
class SequentialRunProgress:
    """Protocol-neutral progress emitted synchronously in causal order."""

    kind: Literal["run_started", "task_started", "task_finished", "run_finished"]
    native_run_id: str
    task_id: str | None = None
    position: int | None = None
    projected_order: tuple[str, ...] = ()
    outcome: TaskExecutionOutcome | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SequentialRunOutcome:
    """Neutral, fully returned result of the sequential state machine."""

    native_run_id: str
    status: str
    success: bool
    exit_code: int
    stopped_reason: SequentialStoppedReason
    task_outcomes: tuple[TaskExecutionOutcome, ...]
    remaining_task_ids: tuple[str, ...]
    summary_path: Path | None
    run_log_path: Path | None
    started_at: str
    finished_at: str
    elapsed_ms: int
    timed_out: bool
    usage: ExecutionUsage
    retained_log_paths: tuple[Path, ...]
    completion_consistent: bool
    integration_commit_proof: IntegrationCommitProof | None = None
    completion_ready: bool = False
    prepared_integration: PreparedIntegration | None = None
    error_kind: str | None = None
    error_message: str | None = None
    summary_payload: Mapping[str, Any] = field(default_factory=dict)


class MutationGuard(Protocol):
    acquired_after_wait: bool

    def release(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SequentialRunDependencies:
    """All side effects required by :func:`run_sequential_forge`."""

    load_plan: Callable[[RunPaths], dict[str, Any]]
    save_plan: Callable[[RunPaths, dict[str, Any]], None]
    ensure_execution_dirs: Callable[[RunPaths], None]
    acquire_mutation_guard: Callable[[RunPaths], MutationGuard]
    reconcile_and_validate: Callable[[RunPaths, dict[str, Any]], dict[str, Any]]
    execute_task: Callable[[SequentialTaskRequest], TaskExecutionOutcome]
    mark_run_status: Callable[[RunPaths, str, str, dict[str, Any] | None], None]
    mark_interrupted_if_running: Callable[[RunPaths, str], None]
    now_iso: Callable[[], str]
    monotonic: Callable[[], float] = time.monotonic
    recover_unsealed_integrations: Callable[[RunPaths], None] | None = None
    finalize_prepared_success: Callable[[RunPaths, str, IntegrationCommitProof], None] | None = None


def bind_retained_artifact(path: Path) -> RetainedArtifact:
    """Hash an already-retained regular file for immutable outcome evidence."""
    resolved = Path(path)
    data = resolved.read_bytes()
    return RetainedArtifact(
        path=resolved,
        digest=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def usage_from_session_logs(paths: tuple[Path, ...] | list[Path]) -> ExecutionUsage:
    """Aggregate native JSONL usage while preserving unknown-cost truth."""
    summary = aggregate_usage_from_session_logs(list(paths))
    totals = summary.totals()
    calls = max(0, int(totals.get("calls") or 0))
    unknown = max(0, int(totals.get("unknown_cost_calls") or 0))
    known = max(0, int(totals.get("known_cost_calls") or 0))
    subscription = max(0, int(totals.get("subscription_calls") or 0))
    included = max(0, int(totals.get("included_calls") or 0))
    local = max(0, int(totals.get("local_calls") or 0))
    classified = known + unknown + subscription + included + local
    # Subscription, included, and local calls legitimately have no dollar cost;
    # only an explicitly unknown provider cost makes aggregate cost unknowable.
    cost_known = unknown == 0
    known_cost_usd = float(totals.get("known_cost_usd") or 0.0)
    total_cost_micros = round(known_cost_usd * 1_000_000) if cost_known else None
    return ExecutionUsage(
        model_calls=calls,
        input_tokens=max(0, int(totals.get("prompt_tokens") or 0)),
        output_tokens=max(0, int(totals.get("completion_tokens") or 0)),
        cost_known=cost_known,
        total_cost_micros=total_cost_micros,
        usage_complete=classified == calls,
    )


def _plan_tasks(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def _candidates(
    plan: Mapping[str, Any],
    *,
    request: SequentialRunRequest,
    exclude_ids: set[str],
) -> list[TaskCandidate]:
    runnable, _ready_for_merge, _skipped = select_task_candidates(
        tasks=_plan_tasks(plan),
        retry_failed=request.retry_failed,
        retry_changes_requested=request.retry_changes_requested,
        max_attempts=request.max_attempts,
        only_ids=set(request.only_ids) if request.only_ids is not None else None,
    )
    return [item for item in runnable if item.task_id not in exclude_ids]


def _next_candidate(
    plan: Mapping[str, Any],
    *,
    request: SequentialRunRequest,
    exclude_ids: set[str],
) -> TaskCandidate | None:
    candidates = _candidates(plan, request=request, exclude_ids=exclude_ids)
    return candidates[0] if candidates else None


def projected_sequential_order(
    plan: Mapping[str, Any],
    *,
    request: SequentialRunRequest,
) -> tuple[str, ...]:
    """Simulate success after each task to reveal dependency order."""
    tasks = [dict(task) for task in _plan_tasks(plan)]
    simulated: dict[str, Any] = {"tasks": tasks}
    order: list[str] = []
    seen: set[str] = set()
    while request.max_tasks is None or len(order) < request.max_tasks:
        candidate = _next_candidate(simulated, request=request, exclude_ids=seen)
        if candidate is None:
            break
        order.append(candidate.task_id)
        seen.add(candidate.task_id)
        for task in tasks:
            if str(task.get("id") or "").strip() == candidate.task_id:
                task["status"] = "done"
                break
    return tuple(order)


def unfinished_task_ids(plan: Mapping[str, Any]) -> tuple[str, ...]:
    """Return plan tasks that still represent executable or blocked work."""
    unfinished: list[str] = []
    for task in _plan_tasks(plan):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        if status in SUCCESSFUL_TERMINAL_STATUSES or status in _NON_EXECUTABLE_TASK_STATUSES:
            continue
        unfinished.append(task_id)
    return tuple(unfinished)


def _bump_attempt(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    task: dict[str, Any],
    save_plan: Callable[[RunPaths, dict[str, Any]], None],
) -> int:
    raw = task.get("attempts")
    try:
        attempts = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        attempts = 0
    attempts = max(0, attempts) + 1
    task["attempts"] = attempts
    save_plan(paths, plan)
    return attempts


class _SequentialRunLog:
    def __init__(
        self,
        *,
        paths: RunPaths,
        ensure_execution_dirs: Callable[[RunPaths], None],
        now_iso: Callable[[], str],
    ) -> None:
        self._path: Path | None = None
        self._now_iso = now_iso
        try:
            ensure_execution_dirs(paths)
            self._path = paths.execution_dir / "sequential_run.jsonl"
        except OSError:
            self._path = None

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        try:
            record: dict[str, Any] = {"ts": self._now_iso(), "event": str(event)}
            record.update(fields)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
        except Exception:  # noqa: BLE001 - diagnostic logging is best effort
            return


def _safe_now_iso(now_iso: Callable[[], str], *, fallback: str = "") -> str:
    try:
        return str(now_iso())
    except Exception:  # noqa: BLE001 - wall audit cannot erase execution truth
        return fallback


def _task_payload(outcome: TaskExecutionOutcome) -> dict[str, Any]:
    return {
        "task_id": outcome.task_id,
        "title": outcome.title,
        "status": outcome.status,
        "success": bool(outcome.success),
        "summary": outcome.summary,
        "report": os.fspath(outcome.report_path),
        "patch": os.fspath(outcome.patch_path),
        "merge_conflict": bool(outcome.merge_conflict),
        "verify_summary": outcome.payload.get("verify_summary"),
        "branch": outcome.branch_facts.task_branch or outcome.payload.get("branch"),
        "commit": outcome.branch_facts.commit or outcome.payload.get("commit"),
        "merge_result": outcome.branch_facts.merge_result or outcome.payload.get("merge_result"),
        "completion_consistent": bool(outcome.completion_consistent),
        "model_calls": outcome.usage.model_calls,
        "cost_known": outcome.usage.cost_known,
    }


def _aggregate_usage(outcomes: list[TaskExecutionOutcome]) -> ExecutionUsage:
    calls = sum(item.usage.model_calls for item in outcomes)
    input_tokens = sum(item.usage.input_tokens for item in outcomes)
    output_tokens = sum(item.usage.output_tokens for item in outcomes)
    cost_known = all(item.usage.cost_known for item in outcomes)
    usage_complete = all(item.usage.usage_complete for item in outcomes)
    total_cost = (
        sum(int(item.usage.total_cost_micros or 0) for item in outcomes) if cost_known else None
    )
    return ExecutionUsage(
        model_calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_known=cost_known,
        total_cost_micros=total_cost,
        usage_complete=usage_complete,
    )


def _emit(
    observer: Callable[[SequentialRunProgress], None] | None,
    progress: SequentialRunProgress,
) -> None:
    if observer is None:
        return
    try:
        observer(progress)
    except Exception:  # noqa: BLE001 - telemetry must never own execution
        return


def _validate_request(request: SequentialRunRequest) -> None:
    if not str(request.native_run_id).strip():
        raise ValueError("native_run_id must not be empty")
    if request.native_run_id != request.native_paths.run_id:
        raise ValueError("native_run_id does not match native_paths.run_id")
    if request.max_tasks is not None and request.max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    if request.max_attempts is not None and request.max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if request.phase0 and not request.phase0_controls.is_phase0_safe():
        raise ValueError("Phase 0 execution capabilities must remain disabled and sequential")
    binding_ids = tuple(item.command_id for item in request.required_verification_bindings)
    if len(binding_ids) != len(set(binding_ids)):
        raise ValueError("required verification command bindings must be unique")
    if any(
        not item.command_id.strip()
        or item.command_id != item.command_id.strip()
        or item.argv not in {("pytest",), ("pytest", "-q")}
        for item in request.required_verification_bindings
    ):
        raise ValueError("required verification command bindings are not Phase 0 admitted")
    if request.phase0 and not request.required_verification_bindings:
        raise ValueError("Phase 0 requires exact verification command bindings")
    if request.phase0:
        policy_violations: list[str] = []
        if not request.git_flow:
            policy_violations.append("git_flow must be enabled")
        if request.verify_mode != "strict":
            policy_violations.append("verify_mode must be strict")
        if request.scope_mode != "strict":
            policy_violations.append("scope_mode must be strict")
        if request.execution_deadline is None or not request.execution_deadline.enabled:
            policy_violations.append("a bounded execution deadline is required")
        if request.dry_run:
            policy_violations.append("dry_run must be disabled")
        if request.retry_failed or request.retry_changes_requested:
            policy_violations.append("retry modes must be disabled")
        if request.keep_going:
            policy_violations.append("keep_going must be disabled")
        if request.max_tasks != 1:
            policy_violations.append("max_tasks must equal 1")
        if request.max_attempts != 1:
            policy_violations.append("max_attempts must equal 1")
        if not str(request.base_branch or "").strip():
            policy_violations.append("a pinned base_branch is required")
        base_head = str(request.base_head or "")
        if (
            len(base_head) not in {40, 64}
            or base_head != base_head.lower()
            or any(character not in "0123456789abcdef" for character in base_head)
        ):
            policy_violations.append("a pinned base_head is required")
        if policy_violations:
            raise ValueError("Phase 0 execution policy rejected: " + "; ".join(policy_violations))


def _phase0_verification_issues(
    *,
    request: SequentialRunRequest,
    outcome: TaskExecutionOutcome,
) -> tuple[str, ...]:
    """Deep-check the exact verifier facts before Phase-0 success can proceed."""
    issues: list[str] = []
    if outcome.usage.model_calls < 1:
        issues.append("Phase 0 task has no observed model call")
    if not outcome.usage.usage_complete:
        issues.append("Phase 0 task model usage classification is incomplete")
    if outcome.timing.timed_out:
        issues.append("Phase 0 task timed out before integration preparation")
    if outcome.success:
        issues.append("Phase 0 task reported success before authoritative Host seal")
    if not outcome.completion_ready:
        issues.append("Phase 0 task is not ready for authoritative Host integration")
    if not outcome.completion_consistent:
        issues.append("Phase 0 task preparation is internally inconsistent")
    branch_facts = outcome.branch_facts
    if branch_facts.merged:
        issues.append("Phase 0 task was merged before authoritative Host seal")
    if branch_facts.base_branch != request.base_branch:
        issues.append("Phase 0 task merge base does not match approval")
    if branch_facts.base_head != request.base_head:
        issues.append("Phase 0 task base HEAD does not match approval")
    if not branch_facts.task_branch:
        issues.append("Phase 0 task branch is missing")
    if not branch_facts.commit:
        issues.append("Phase 0 task commit is missing")
    if not branch_facts.merge_commit:
        issues.append("Phase 0 merge commit is missing")
    if branch_facts.merge_first_parent != request.base_head:
        issues.append("Phase 0 merge first parent does not match approved base HEAD")
    if branch_facts.merge_second_parent != branch_facts.commit:
        issues.append("Phase 0 merge second parent does not match task commit")
    if branch_facts.base_ref_after_merge != request.base_head:
        issues.append("Phase 0 base ref changed before authoritative Host seal")
    proof = outcome.integration_commit_proof
    if proof is not None:
        issues.append("Phase 0 task has an integration proof before authoritative Host seal")
    prepared = outcome.prepared_integration
    if prepared is None:
        issues.append("Phase 0 task has no prepared integration transaction")
    else:
        facts = prepared.facts
        expected_journal = (
            request.native_paths.execution_integration_dir
            / "transactions"
            / f"{facts.transaction_id}.json"
        )
        if prepared.state != "PREPARED":
            issues.append("Phase 0 integration transaction is not prepared")
        if facts.native_run_id != request.native_run_id:
            issues.append("Phase 0 integration native run does not match approval")
        if facts.task_id != outcome.task_id:
            issues.append("Phase 0 integration task does not match execution")
        if facts.base_branch != request.base_branch:
            issues.append("Phase 0 integration base branch does not match approval")
        if facts.approved_base_head != request.base_head:
            issues.append("Phase 0 integration base HEAD does not match approval")
        if facts.task_branch != branch_facts.task_branch:
            issues.append("Phase 0 integration task branch does not match outcome")
        if facts.task_commit != branch_facts.commit:
            issues.append("Phase 0 integration task commit does not match outcome")
        if facts.prepared_merge_commit != branch_facts.merge_commit:
            issues.append("Phase 0 prepared merge does not match outcome")
        if facts.prepared_elapsed_ms < 0:
            issues.append("Phase 0 integration preparation timing is invalid")
        if facts.prepared_elapsed_ms != outcome.timing.elapsed_ms:
            issues.append("Phase 0 integration preparation timing does not match outcome")
        if facts.journal_path != expected_journal:
            issues.append("Phase 0 integration journal path does not match run authority")
    results = outcome.verification
    if not results:
        issues.append("Phase 0 task has no exact verification result")
        return tuple(issues)
    actual_bindings = tuple((result.command_id, result.argv) for result in results)
    actual_ids = tuple(item[0] for item in actual_bindings)
    if len(actual_bindings) != len(set(actual_bindings)) or len(actual_ids) != len(set(actual_ids)):
        issues.append("Phase 0 verification command IDs are duplicated")
    expected_bindings = tuple(
        (item.command_id, item.argv) for item in request.required_verification_bindings
    )
    if actual_bindings != expected_bindings:
        issues.append("Phase 0 verification command coverage does not match approval")
    for result in results:
        if not result.passed:
            issues.append(f"Phase 0 verification did not pass: {result.command_id}")
        artifact = result.artifact
        if not retained_artifact_matches(
            artifact,
            containment_root=request.native_paths.execution_verify_dir,
        ):
            issues.append(
                f"Phase 0 verification artifact is unavailable or changed: {result.command_id}"
            )
    return tuple(issues)


def _elapsed_ms(
    *,
    request: SequentialRunRequest,
    monotonic_started: float,
    monotonic: Callable[[], float],
) -> int:
    if request.execution_deadline is not None:
        # The shared deadline begins at the operation boundary, before this
        # engine may be entered.  Report its full monotonic elapsed time so time
        # consumed by admission/approval cannot disappear from terminal facts.
        elapsed = request.execution_deadline.elapsed_seconds()
    else:
        elapsed = monotonic() - monotonic_started
    return max(0, round(elapsed * 1000))


def _summary_payload(
    *,
    request: SequentialRunRequest,
    started_at: str,
    finished_at: str,
    outcomes: list[TaskExecutionOutcome],
    stopped_reason: SequentialStoppedReason,
    status: str,
    success: bool,
    exit_code: int,
    remaining: tuple[str, ...],
    elapsed_ms: int,
    timed_out: bool,
    usage: ExecutionUsage,
    error: str | None,
) -> dict[str, Any]:
    executed = [_task_payload(item) for item in outcomes]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "command": "forge.run",
        "run_id": request.native_run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "engine": "sequential",
        "worktrees_used": False,
        "parallel": 1,
        "git_flow": bool(request.git_flow),
        "verify": request.verify_mode,
        "scope": request.scope_mode,
        "stopped_reason": stopped_reason,
        "status": status,
        "success": success,
        "exit_code": int(exit_code),
        "elapsed_ms": elapsed_ms,
        "timed_out": timed_out,
        "executed": executed,
        "counts": {
            "executed": len(executed),
            "succeeded": sum(1 for item in executed if item["success"]),
            "failed": sum(1 for item in executed if not item["success"]),
        },
        "remaining": list(remaining),
        "usage": {
            "model_calls": usage.model_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_known": usage.cost_known,
            "total_cost_micros": usage.total_cost_micros,
            "usage_complete": usage.usage_complete,
        },
    }
    if error:
        payload["error"] = error
    return payload


def _write_summary(
    *,
    paths: RunPaths,
    ensure_execution_dirs: Callable[[RunPaths], None],
    payload: dict[str, Any],
) -> Path | None:
    try:
        ensure_execution_dirs(paths)
        path = paths.execution_dir / "sequential_summary.json"
        atomic_write_text(
            path,
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        )
    except Exception:  # noqa: BLE001 - reporting is best-effort at this boundary
        return None
    return path


def _terminal_status(
    *,
    plan: Mapping[str, Any],
    any_failure: bool,
) -> tuple[str, str]:
    if any_failure:
        return RUN_STATUS_FAILED, "execution finished with at least one task not accepted"
    remaining = unfinished_task_ids(plan)
    if remaining:
        return (
            RUN_STATUS_APPROVED,
            f"execution finished; {len(remaining)} task(s) still unexecuted",
        )
    return RUN_STATUS_COMPLETED, "every plan task reached a successful terminal status"


def run_sequential_forge(
    request: SequentialRunRequest,
    *,
    dependencies: SequentialRunDependencies,
    observer: Callable[[SequentialRunProgress], None] | None = None,
    prepared_integration_sink: Callable[[PreparedIntegration], None] | None = None,
) -> SequentialRunOutcome:
    """Run one guarded Forge plan and return every terminal fact.

    Expected task-execution failures are converted to a neutral outcome.  The
    mutation guard is always released, and a run that had entered ``running``
    but failed to record a terminal state is marked interrupted before return.
    """
    _validate_request(request)
    if request.phase0:
        missing_dependencies: list[str] = []
        if dependencies.recover_unsealed_integrations is None:
            missing_dependencies.append("unsealed integration recovery")
        if dependencies.finalize_prepared_success is None:
            missing_dependencies.append("strict native success finalization")
        if prepared_integration_sink is None:
            missing_dependencies.append("prepared integration registration")
        if missing_dependencies:
            raise ValueError(
                "Phase 0 engine dependencies are incomplete: " + ", ".join(missing_dependencies)
            )
    paths = request.native_paths
    started_at = request.started_at or _safe_now_iso(dependencies.now_iso)
    if request.execution_deadline is not None:
        monotonic_started = request.execution_deadline.elapsed_seconds()
    else:
        monotonic_started = dependencies.monotonic()

    outcomes: list[TaskExecutionOutcome] = []
    attempted: set[str] = set()
    plan: dict[str, Any] = {}
    projected: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    stopped_reason: SequentialStoppedReason = "completed"
    error_kind: str | None = None
    error_message: str | None = None
    exit_code = 0
    status = RUN_STATUS_APPROVED
    entered_running = False
    terminal_recorded = False
    transaction_transferred = False
    guard: MutationGuard | None = None
    run_log: _SequentialRunLog | None = None
    native_preimage: NativeRunStatePreimage | None = None
    prepared_integration: PreparedIntegration | None = None

    try:
        run_log = _SequentialRunLog(
            paths=paths,
            ensure_execution_dirs=dependencies.ensure_execution_dirs,
            now_iso=dependencies.now_iso,
        )
        guard = dependencies.acquire_mutation_guard(paths)
        if request.phase0:
            assert dependencies.recover_unsealed_integrations is not None
            dependencies.recover_unsealed_integrations(paths)
        plan = dependencies.load_plan(paths)
        if bool(getattr(guard, "acquired_after_wait", False)):
            # Preserve the CLI's post-wait reload invariant even when a custom
            # loader caches its first read.
            plan = dependencies.load_plan(paths)
        plan = dependencies.reconcile_and_validate(paths, plan)
        if request.phase0 and len(unfinished_task_ids(plan)) != 1:
            raise ValueError("Phase 0 requires exactly one unfinished native task")
        if request.phase0:
            native_preimage = _capture_native_run_state(paths)
        projected = projected_sequential_order(plan, request=request)
        run_log.append(
            "run_started",
            run_id=request.native_run_id,
            order=list(projected),
            git_flow=request.git_flow,
            verify=request.verify_mode,
            scope=request.scope_mode,
            dry_run=request.dry_run,
        )
        _emit(
            observer,
            SequentialRunProgress(
                kind="run_started",
                native_run_id=request.native_run_id,
                projected_order=projected,
            ),
        )

        if request.dry_run:
            stopped_reason = "dry_run"
            remaining = unfinished_task_ids(plan)
        else:
            dependencies.mark_run_status(
                paths,
                RUN_STATUS_RUNNING,
                f"forge run started ({len(projected)} task(s) projected)",
                plan,
            )
            entered_running = True
            while True:
                deadline = request.execution_deadline
                if deadline is not None and deadline.is_exhausted():
                    stopped_reason = "deadline_exhausted"
                    error_kind = "deadline_exhausted"
                    error_message = "execution deadline exhausted before the next task"
                    exit_code = 2
                    status = RUN_STATUS_INTERRUPTED
                    break
                if request.max_tasks is not None and len(outcomes) >= request.max_tasks:
                    if unfinished_task_ids(plan):
                        stopped_reason = "max_tasks"
                    break
                candidate = _next_candidate(plan, request=request, exclude_ids=attempted)
                if candidate is None:
                    break

                task = candidate.task
                task_id = candidate.task_id
                attempted.add(task_id)
                position = len(outcomes) + 1
                run_log.append(
                    "task_started",
                    task_id=task_id,
                    title=str(task.get("title") or ""),
                    position=position,
                )
                _emit(
                    observer,
                    SequentialRunProgress(
                        kind="task_started",
                        native_run_id=request.native_run_id,
                        task_id=task_id,
                        position=position,
                    ),
                )
                try:
                    outcome = dependencies.execute_task(
                        SequentialTaskRequest(
                            native_run_id=request.native_run_id,
                            native_paths=paths,
                            plan=plan,
                            task=task,
                            task_id=task_id,
                            position=position,
                            base_branch=request.base_branch,
                            base_head=request.base_head,
                            execution_deadline=deadline,
                            phase0=request.phase0,
                            phase0_controls=request.phase0_controls,
                        )
                    )
                except SequentialTaskExecutionError as exc:
                    stopped_reason = "task_error"
                    error_kind = exc.kind
                    error_message = sanitize_error_summary(str(exc))
                    exit_code = 2
                    status = RUN_STATUS_INTERRUPTED
                    run_log.append(
                        "task_error",
                        task_id=task_id,
                        error=error_message,
                        kind=error_kind,
                    )
                    break

                if outcome.task_id != task_id:
                    raise ValueError("task executor returned an outcome for a different task")
                if request.phase0:
                    verification_issues = _phase0_verification_issues(
                        request=request,
                        outcome=outcome,
                    )
                    if verification_issues:
                        # Missing, failed, mismatched, or mutable evidence can
                        # never be translated to Host success.  Persist the
                        # native rejection before the scheduler advances.
                        task["status"] = "verify_failed"
                        if outcome.prepared_integration is not None:
                            outcome.prepared_integration.abort(
                                "Phase 0 preparation evidence was rejected"
                            )
                        outcome = replace(
                            outcome,
                            status="verify_failed",
                            success=False,
                            exit_code=1,
                            integration_commit_proof=None,
                            completion_ready=False,
                            prepared_integration=None,
                            completion_consistent=False,
                            completion_issues=(
                                *outcome.completion_issues,
                                *verification_issues,
                            ),
                        )
                    else:
                        prepared_integration = outcome.prepared_integration
                        assert prepared_integration is not None
                        assert guard is not None
                        assert native_preimage is not None
                        assert dependencies.finalize_prepared_success is not None
                        assert prepared_integration_sink is not None

                        def _finalize_native(
                            proof: IntegrationCommitProof,
                            *,
                            task_identity: str = task_id,
                        ) -> None:
                            assert dependencies.finalize_prepared_success is not None
                            dependencies.finalize_prepared_success(
                                paths,
                                task_identity,
                                proof,
                            )

                        adoption_guard = guard
                        # Authority transfers at adoption entry.  The
                        # transaction itself performs the single release on
                        # every adoption-failure convergence path.
                        guard = None
                        prepared_integration._adopt_engine_authority(
                            guard=adoption_guard,
                            native_preimage=native_preimage,
                            journal_root=paths.execution_integration_dir / "transactions",
                            finalize_native=_finalize_native,
                        )
                        # Ownership moves before the registration callback.  If
                        # registration itself rejects, abort restores the exact
                        # native/Git preimage and releases the transferred guard.
                        try:
                            prepared_integration_sink(prepared_integration)
                        except Exception:
                            prepared_integration.abort(
                                "prepared integration registration was rejected"
                            )
                            prepared_integration = None
                            raise
                        transaction_transferred = True
                outcomes.append(outcome)
                attempts = _bump_attempt(
                    paths=paths,
                    plan=plan,
                    task=task,
                    save_plan=dependencies.save_plan,
                )
                run_log.append(
                    "task_finished",
                    task_id=task_id,
                    status=outcome.status,
                    success=outcome.success,
                    summary=outcome.summary,
                    report=os.fspath(outcome.report_path),
                    attempts=attempts,
                )
                _emit(
                    observer,
                    SequentialRunProgress(
                        kind="task_finished",
                        native_run_id=request.native_run_id,
                        task_id=task_id,
                        position=position,
                        outcome=outcome,
                    ),
                )
                # Execution and attempt persistence may both update the plan.
                # Always schedule the next task from the authoritative reload.
                plan = dependencies.load_plan(paths)
                if request.phase0 and outcome.completion_ready:
                    stopped_reason = "prepared_for_commit"
                    status = RUN_STATUS_RUNNING
                    exit_code = 0
                    break
                if not outcome.success and not request.keep_going:
                    stopped_reason = "task_failed"
                    break

            remaining = unfinished_task_ids(plan)
            if transaction_transferred:
                # The one admitted task is execution-complete but deliberately
                # remains native ``prepared`` until the Host seals it.
                remaining = ()
            if not outcomes and remaining and stopped_reason == "completed":
                stopped_reason = "blocked"
            if not transaction_transferred and stopped_reason not in {
                "task_error",
                "deadline_exhausted",
            }:
                any_failure = any(not item.success for item in outcomes)
                status, reason = _terminal_status(plan=plan, any_failure=any_failure)
                dependencies.mark_run_status(paths, status, reason, None)
                terminal_recorded = True
                exit_code = 1 if any_failure else 0
    except Exception as exc:  # noqa: BLE001 - result-returning engine boundary
        stopped_reason = "engine_error"
        error_kind = getattr(exc, "kind", None) or exc.__class__.__name__
        error_message = sanitize_error_summary(str(exc) or exc.__class__.__name__)
        exit_code = 2
        status = RUN_STATUS_INTERRUPTED if entered_running else RUN_STATUS_APPROVED
        if plan:
            remaining = unfinished_task_ids(plan)
        if run_log is not None:
            run_log.append("run_error", kind=error_kind, error=error_message)
    finally:
        cleanup_error: Exception | None = None
        try:
            if entered_running and not terminal_recorded and not transaction_transferred:
                dependencies.mark_interrupted_if_running(
                    paths,
                    "forge run exited without recording an outcome",
                )
        except Exception as exc:  # noqa: BLE001 - release must still run
            cleanup_error = exc
        finally:
            if guard is not None:
                try:
                    guard.release()
                except Exception as exc:  # noqa: BLE001 - return cleanup failures
                    cleanup_error = cleanup_error or exc
        if cleanup_error is not None and error_message is None:
            stopped_reason = "engine_error"
            error_kind = cleanup_error.__class__.__name__
            error_message = sanitize_error_summary(
                str(cleanup_error) or cleanup_error.__class__.__name__
            )
            exit_code = 2
            status = RUN_STATUS_INTERRUPTED if entered_running else RUN_STATUS_APPROVED

    finished_at = _safe_now_iso(dependencies.now_iso, fallback=started_at)
    elapsed_ms = _elapsed_ms(
        request=request,
        monotonic_started=monotonic_started,
        monotonic=dependencies.monotonic,
    )
    if transaction_transferred and prepared_integration is not None:
        elapsed_ms = prepared_integration.facts.prepared_elapsed_ms
        timed_out = False
    else:
        timed_out = bool(
            stopped_reason == "deadline_exhausted"
            or (
                request.execution_deadline is not None and request.execution_deadline.is_exhausted()
            )
            or any(item.timing.timed_out for item in outcomes)
        )
    usage = _aggregate_usage(outcomes)
    completion_ready = bool(
        request.phase0
        and transaction_transferred
        and prepared_integration is not None
        and prepared_integration.state == "PREPARED"
        and len(outcomes) == 1
        and outcomes[0].completion_ready
        and outcomes[0].completion_consistent
        and error_message is None
    )
    accepted_completed = (
        status == RUN_STATUS_COMPLETED
        and not remaining
        and bool(outcomes)
        and all(item.success for item in outcomes)
        and (not request.phase0 or all(item.completion_consistent for item in outcomes))
        and not timed_out
        and error_message is None
    )
    success = accepted_completed
    exact_phase0_coverage = not request.phase0 or len(outcomes) == 1
    completion_consistent = bool(
        (accepted_completed or completion_ready)
        and exact_phase0_coverage
        and all(item.completion_consistent for item in outcomes)
    )
    payload = _summary_payload(
        request=request,
        started_at=started_at,
        finished_at=finished_at,
        outcomes=outcomes,
        stopped_reason=stopped_reason,
        status=status,
        success=success,
        exit_code=exit_code,
        remaining=remaining,
        elapsed_ms=elapsed_ms,
        timed_out=timed_out,
        usage=usage,
        error=error_message,
    )
    payload["completion_ready"] = completion_ready
    summary_path = None
    if not request.dry_run:
        summary_path = _write_summary(
            paths=paths,
            ensure_execution_dirs=dependencies.ensure_execution_dirs,
            payload=payload,
        )
        if summary_path is not None:
            payload["summary_path"] = os.fspath(summary_path)
    retained_logs: list[Path] = []
    if run_log is not None and run_log.path is not None and run_log.path.is_file():
        retained_logs.append(run_log.path)
    for outcome in outcomes:
        for path in outcome.retained_log_paths:
            if path.is_file() and path not in retained_logs:
                retained_logs.append(path)

    result = SequentialRunOutcome(
        native_run_id=request.native_run_id,
        status=status,
        success=success,
        exit_code=exit_code,
        stopped_reason=stopped_reason,
        task_outcomes=tuple(outcomes),
        remaining_task_ids=remaining,
        summary_path=summary_path,
        run_log_path=run_log.path if run_log is not None else None,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        timed_out=timed_out,
        usage=usage,
        retained_log_paths=tuple(retained_logs),
        completion_consistent=completion_consistent,
        integration_commit_proof=None,
        completion_ready=completion_ready,
        prepared_integration=(prepared_integration if transaction_transferred else None),
        error_kind=str(error_kind) if error_kind is not None else None,
        error_message=error_message,
        summary_payload=payload,
    )
    if run_log is not None:
        run_log.append(
            "run_finished",
            status=status,
            stopped_reason=stopped_reason,
            exit_code=exit_code,
            timed_out=timed_out,
        )
    _emit(
        observer,
        SequentialRunProgress(
            kind="run_finished",
            native_run_id=request.native_run_id,
            message=stopped_reason,
        ),
    )
    return result


__all__ = [
    "BranchFacts",
    "ExecutionTiming",
    "ExecutionUsage",
    "HostVerificationBinding",
    "IntegrationCommitProof",
    "IntegrationFinalizeProof",
    "IntegrationTransactionState",
    "MutationGuard",
    "NativeAncestorPreimage",
    "NativeFilePreimage",
    "NativeRunStatePreimage",
    "Phase0ExecutionControls",
    "PreparedIntegration",
    "PreparedIntegrationError",
    "PreparedIntegrationFacts",
    "PreparedIntegrationRollbackError",
    "SequentialRunDependencies",
    "SequentialRunOutcome",
    "SequentialRunProgress",
    "SequentialRunRequest",
    "SequentialTaskExecutionError",
    "SequentialTaskRequest",
    "TaskExecutionOutcome",
    "bind_retained_artifact",
    "projected_sequential_order",
    "run_sequential_forge",
    "restore_native_run_state_payload",
    "unfinished_task_ids",
    "usage_from_session_logs",
    "validate_native_run_state_payload",
]
