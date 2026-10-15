# ruff: noqa: F821
# Dependencies are injected at runtime from alysis_code.cli to preserve monkeypatch surfaces.
from __future__ import annotations

import builtins
import copy
import dis
import hashlib
import hmac
import inspect
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
import types
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import typer
from click import get_current_context
from click.core import ParameterSource

from ..assets import AssetError
from ..assets.budget_allocator import (
    TaskAssetAllocation,
    allocate_task_assets,
    write_task_asset_allocation,
)
from ..assets.surface import build_asset_surface
from ..assets.usage_logger import AssetUsageLogger
from ..assets.worker_mirror import TaskAssetMirror, mirror_task_assets
from ..assets.worker_section import render_relevant_assets_section
from ..assets.worker_tools import build_worker_asset_mcp_manager, compose_worker_asset_mcp_manager
from ..atomic_io import atomic_write_text
from ..branding import env_get
from ..command_text import command_text
from ..config import resolve_api_key as _host_resolve_api_key
from ..error_text import sanitize_error_summary, sanitize_optional_error_summary
from ..execution_deadline import ExecutionDeadline
from ..execution_shared import build_task_acceptance_instruction
from ..execution_shared import safe_task_file_component as _host_safe_task_file_component
from ..failure_category import FailureCategory
from ..forge_events import (
    EVENT_PLAN_INVALID,
    EVENT_PLAN_SAVED,
    EVENT_REVIEW_RESULT,
    EVENT_SCOPE_AMENDED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_STARTED,
    EVENT_VERIFICATION_RESULT,
    EVENT_VERIFICATION_UNAVAILABLE,
    EXIT_ERROR,
    EXIT_NOT_ACCEPTED,
    EXIT_OK,
    ForgeEventEmitter,
)
from ..forge_host_verification import (
    HostVerificationBinding,
    HostVerificationResult,
    retained_artifact_matches,
    run_host_verification,
)
from ..forge_sequential_engine import (
    BranchFacts,
    ExecutionTiming,
    IntegrationCommitProof,
    NativeRunStatePreimage,
    PreparedIntegration,
    PreparedIntegrationFacts,
    PreparedIntegrationRollbackError,
    SequentialRunDependencies,
    SequentialRunProgress,
    SequentialRunRequest,
    SequentialTaskExecutionError,
    SequentialTaskRequest,
    TaskExecutionOutcome,
    bind_retained_artifact,
    restore_native_run_state_payload,
    run_sequential_forge,
    usage_from_session_logs,
    validate_native_run_state_matches_payload,
    validate_native_run_state_payload,
)
from ..git_ops import (
    DEFAULT_COMMIT_AUTHOR_EMAIL,
    DEFAULT_COMMIT_AUTHOR_NAME,
    GitOpsError,
    generate_task_branch_name,
)
from ..git_safe import build_git_cmd, build_git_process_env, resolve_disabled_hooks_dir
from ..model_registry import ModelRegistry
from ..model_registry import resolve_model_provider_key as _host_resolve_model_provider_key
from ..plan_repair import (
    PLAN_STATUS_DRAFT,
    apply_plan_status,
    plan_repair_event_payload,
)
from ..plan_validation import PlannerFailedError, raise_for_execution_ready_plan
from ..profiles import apply_runtime_base_url_override
from ..profiles import (
    connection_fingerprint as _host_connection_fingerprint,
)
from ..profiles import (
    get_active_profile as _host_get_active_profile,
)
from ..profiles import (
    resolve_effective_base_url as _host_resolve_effective_base_url,
)
from ..replanning import resolve_replanning_mode
from ..run_state import RUN_STATUS_COMPLETED, RUN_STATUS_FAILED, RUN_STATUS_RUNNING
from ..runtime_kind import RuntimeKind
from ..swarm_orchestrator import acquire_swarm_mutation_guard
from ..swarm_scheduler import (
    SUCCESSFUL_TERMINAL_STATUSES,
    TaskCandidate,
    canonical_task_status,
    select_task_candidates,
)
from ..task_readiness import is_clearly_non_mutating_task
from ..task_scope import (
    apply_scope_amendments,
    assess_scope_changes,
    describe_scope_violations,
    is_non_material_untracked_path,
    normalize_scope_patterns,
    relocate_known_scratch_artifacts,
    scope_path_matches_pattern,
)
from ..verification_repair import (
    TASK_STATUS_COMPLETED_UNVERIFIED,
    RepairAttemptExecution,
    build_repair_instruction,
    resolve_repair_attempt_budget,
    run_verification_repair_loop,
    verification_failure_excerpts,
)
from ..verify_gate import verify_run_status

_PROTECTED_GLOBAL_NAMES: set[str] = set()
_HOST_TASK_EXECUTOR_BOOTSTRAP_LOCK = threading.Lock()
_HOST_TASK_EXECUTOR_BOOTSTRAP_MODULE: Any | None = None


def _events_or_null(events: Any) -> ForgeEventEmitter:
    """Return the caller's emitter, or an inert one so call sites stay unconditional."""
    if isinstance(events, ForgeEventEmitter):
        return events
    return ForgeEventEmitter(command="forge", enabled=False)


class ForgeTaskExecutionError(SequentialTaskExecutionError):
    """The task could not be executed at all -- a command error, not a failed task.

    A task that ran and was rejected comes back as an outcome with
    ``success=False`` (exit 1). This is the other case: bad git state, missing
    branch context, nothing to execute against. Callers report it as exit 2, and
    a sequential run stops rather than moving to the next task.
    """


def _run_host_git_checked(
    root: Path,
    args: list[str],
    *,
    execution_deadline: ExecutionDeadline | None,
    error_message: str,
    extra_config: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout: float | None = None
    if execution_deadline is not None:
        timeout = execution_deadline.remaining_seconds()
        if timeout is not None and timeout <= 0:
            raise GitOpsError(f"{error_message}: run deadline exhausted")
    try:
        forced_config = dict(extra_config or {})
        # Host Phase 0 never delegates authority to repository hooks, even when
        # the legacy opt-in environment variable is present in the parent.
        forced_config["core.hooksPath"] = os.fspath(resolve_disabled_hooks_dir(root, {}))
        completed = subprocess.run(
            build_git_cmd(root, args, extra_config=forced_config, env={}),
            check=False,
            capture_output=True,
            **command_text(),
            env=build_git_process_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitOpsError(f"{error_message}: run deadline exhausted") from exc
    except OSError as exc:
        raise GitOpsError(error_message) from exc
    if completed.returncode != 0:
        detail = sanitize_error_summary(
            (completed.stderr or completed.stdout or "Git rejected the operation").strip()
        )
        raise GitOpsError(f"{error_message}: {detail}")
    return completed


def _resolve_host_git_commit(
    root: Path,
    revision: str,
    *,
    execution_deadline: ExecutionDeadline | None,
) -> str:
    completed = _run_host_git_checked(
        root,
        ["rev-parse", "--verify", f"{revision}^{{commit}}"],
        execution_deadline=execution_deadline,
        error_message="failed to resolve pinned Git identity",
    )
    return completed.stdout.strip()


def _checkout_new_host_task_branch(
    root: Path,
    *,
    branch: str,
    base_branch: str,
    base_head: str,
    execution_deadline: ExecutionDeadline | None,
) -> None:
    """Atomically require creation of a fresh Phase-0 task ref.

    Legacy ``checkout_branch`` intentionally reuses an existing branch.  That
    behavior is useful interactively but would let a ref created after Host
    approval silently replace the approved branch materialization.  ``git
    checkout -b`` performs the existence check and ref creation in one Git
    operation and fails if the ref already exists.
    """

    observed_base = _resolve_host_git_commit(
        root,
        f"refs/heads/{base_branch}",
        execution_deadline=execution_deadline,
    )
    if observed_base != base_head:
        raise GitOpsError("approved base branch changed before task branch creation")
    _run_host_git_checked(
        root,
        ["checkout", "-b", branch, base_head],
        execution_deadline=execution_deadline,
        error_message=f"failed to create new Host task branch {branch}",
    )
    if (
        _resolve_host_git_commit(
            root,
            "HEAD",
            execution_deadline=execution_deadline,
        )
        != base_head
    ):
        raise GitOpsError("new Host task branch did not start at the approved base HEAD")


def _host_git_metadata_snapshot(
    root: Path,
    *,
    task_branch: str,
    base_branch: str,
    execution_deadline: ExecutionDeadline | None,
    expected_metadata_digest: str | None = None,
) -> tuple[str, str, str, str, str, str]:
    metadata_digest = _host_git_metadata_digest(
        root,
        execution_deadline=execution_deadline,
    )
    if expected_metadata_digest is not None and metadata_digest != expected_metadata_digest:
        raise GitOpsError("exact Host verification changed local Git metadata")
    symbolic_head = _run_host_git_checked(
        root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        execution_deadline=execution_deadline,
        error_message="failed to inspect Host verification branch",
    ).stdout.strip()
    head = _resolve_host_git_commit(root, "HEAD", execution_deadline=execution_deadline)
    task_head = _resolve_host_git_commit(
        root,
        f"refs/heads/{task_branch}",
        execution_deadline=execution_deadline,
    )
    base_head = _resolve_host_git_commit(
        root,
        f"refs/heads/{base_branch}",
        execution_deadline=execution_deadline,
    )
    status = _run_host_git_checked(
        root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        execution_deadline=execution_deadline,
        error_message="failed to inspect Host verification Git status",
    ).stdout
    metadata_digest_after_inspection = _host_git_metadata_digest(
        root,
        execution_deadline=execution_deadline,
    )
    if metadata_digest_after_inspection != metadata_digest:
        raise GitOpsError("Host Git inspection changed local repository metadata")
    return (
        symbolic_head,
        head,
        task_head,
        base_head,
        status,
        metadata_digest_after_inspection,
    )


def _check_host_snapshot_deadline(
    execution_deadline: ExecutionDeadline | None,
) -> None:
    if execution_deadline is not None and execution_deadline.is_exhausted():
        raise GitOpsError("run deadline exhausted while snapshotting repository state")


def _host_hash_regular_file(
    path: Path,
    *,
    before: os.stat_result,
    execution_deadline: ExecutionDeadline | None,
) -> str:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                _check_host_snapshot_deadline(execution_deadline)
                hasher.update(chunk)
        after = path.lstat()
    except OSError as exc:
        raise GitOpsError("repository file changed during snapshot") from exc
    stable_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    stable_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    if stable_before != stable_after:
        raise GitOpsError("repository file changed during snapshot")
    return hasher.hexdigest()


def _host_path_record(
    path: Path,
    *,
    execution_deadline: ExecutionDeadline | None,
) -> tuple[str, int, int, str]:
    _check_host_snapshot_deadline(execution_deadline)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return ("missing", 0, 0, "")
    except OSError as exc:
        raise GitOpsError("repository state changed during snapshot") from exc
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISLNK(before.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise GitOpsError("repository symlink changed during snapshot") from exc
        return (
            "symlink",
            mode,
            int(before.st_size),
            hashlib.sha256(os.fsencode(target)).hexdigest(),
        )
    if stat.S_ISDIR(before.st_mode):
        return ("directory", mode, 0, "")
    if stat.S_ISREG(before.st_mode):
        return (
            "file",
            mode,
            int(before.st_size),
            _host_hash_regular_file(
                path,
                before=before,
                execution_deadline=execution_deadline,
            ),
        )
    return ("special", mode, int(before.st_size), "")


def _host_bounded_control_tree(
    root: Path,
    *,
    execution_deadline: ExecutionDeadline | None,
    max_entries: int = 10_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> dict[str, tuple[str, int, int, str]]:
    snapshot_root = root.resolve(strict=True)
    records: dict[str, tuple[str, int, int, str]] = {}
    total_bytes = 0

    def visit(directory: Path) -> None:
        nonlocal total_bytes
        _check_host_snapshot_deadline(execution_deadline)
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise GitOpsError("failed to enumerate Git control metadata") from exc
        for entry in entries:
            if len(records) >= max_entries:
                raise GitOpsError("Git control metadata exceeds the Host snapshot entry limit")
            path = Path(entry.path)
            relative = path.relative_to(snapshot_root).as_posix()
            record = _host_path_record(
                path,
                execution_deadline=execution_deadline,
            )
            total_bytes += record[2] if record[0] == "file" else 0
            if total_bytes > max_bytes:
                raise GitOpsError("Git control metadata exceeds the Host snapshot byte limit")
            records[relative] = record
            if record[0] == "directory":
                visit(path)

    visit(snapshot_root)
    return records


def _bounded_host_git_output(
    root: Path,
    args: list[str],
    *,
    execution_deadline: ExecutionDeadline | None,
    error_message: str,
    max_bytes: int = 32 * 1024 * 1024,
    max_records: int = 100_000,
) -> str:
    output = _run_host_git_checked(
        root,
        args,
        execution_deadline=execution_deadline,
        error_message=error_message,
    ).stdout
    if len(output.encode("utf-8", errors="surrogatepass")) > max_bytes:
        raise GitOpsError(f"{error_message}: output exceeds the Host byte limit")
    if output.count("\0") > max_records:
        raise GitOpsError(f"{error_message}: output exceeds the Host entry limit")
    return output


def _host_git_metadata_digest(
    root: Path,
    *,
    execution_deadline: ExecutionDeadline | None,
) -> str:
    git_dir = Path(
        _run_host_git_checked(
            root,
            ["rev-parse", "--absolute-git-dir"],
            execution_deadline=execution_deadline,
            error_message="failed to locate local Git metadata",
        ).stdout.strip()
    ).resolve(strict=True)
    records: dict[str, tuple[str, int, int, str]] = {}
    try:
        top_level_entries = sorted(os.scandir(git_dir), key=lambda item: item.name)
    except OSError as exc:
        raise GitOpsError("failed to enumerate local Git metadata") from exc
    for entry in top_level_entries:
        if entry.name == "objects" or entry.name.endswith(".lock"):
            continue
        path = Path(entry.path)
        record = _host_path_record(path, execution_deadline=execution_deadline)
        records[entry.name] = record
        if entry.name in {"hooks", "info", "logs"} and record[0] == "directory":
            for relative, nested_record in _host_bounded_control_tree(
                path,
                execution_deadline=execution_deadline,
            ).items():
                records[f"{entry.name}/{relative}"] = nested_record
    refs = _bounded_host_git_output(
        root,
        ["for-each-ref", "--sort=refname", "--format=%(refname)%00%(objectname)%00"],
        execution_deadline=execution_deadline,
        error_message="failed to inventory local Git refs",
    )
    records["@semantic-refs"] = (
        "projection",
        0,
        len(refs.encode("utf-8", errors="surrogatepass")),
        hashlib.sha256(refs.encode("utf-8", errors="surrogatepass")).hexdigest(),
    )
    encoded = json.dumps(
        sorted(records.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _host_ref_snapshot(
    root: Path,
    *,
    execution_deadline: ExecutionDeadline | None,
) -> dict[str, str]:
    """Return the complete bounded local ref namespace as exact OIDs."""

    output = _bounded_host_git_output(
        root,
        ["for-each-ref", "--sort=refname", "--format=%(refname)%00%(objectname)%00"],
        execution_deadline=execution_deadline,
        error_message="failed to inventory Host transaction refs",
    )
    records: dict[str, str] = {}
    for raw in output.splitlines():
        fields = raw.split("\0")
        if len(fields) != 3 or fields[-1] or not fields[0] or not fields[1]:
            raise GitOpsError("Host transaction ref inventory is malformed")
        ref_name, object_name = fields[:2]
        if ref_name in records:
            raise GitOpsError("Host transaction ref inventory contains duplicates")
        records[ref_name] = object_name
    return records


def _host_stable_git_control_digest(
    root: Path,
    *,
    execution_deadline: ExecutionDeadline | None,
) -> str:
    """Hash bounded Git controls that the transaction never legitimately changes."""

    git_dir = Path(
        _run_host_git_checked(
            root,
            ["rev-parse", "--absolute-git-dir"],
            execution_deadline=execution_deadline,
            error_message="failed to locate Host transaction Git metadata",
        ).stdout.strip()
    ).resolve(strict=True)
    mutable_names = frozenset({"HEAD", "index", "logs", "objects", "refs"})
    records: dict[str, tuple[str, int, int, str]] = {}
    total_entries = 0
    total_bytes = 0
    try:
        entries = sorted(os.scandir(git_dir), key=lambda item: item.name)
    except OSError as exc:
        raise GitOpsError("failed to enumerate Host transaction Git controls") from exc
    for entry in entries:
        if entry.name in mutable_names or entry.name.endswith(".lock"):
            continue
        path = Path(entry.path)
        record = _host_path_record(path, execution_deadline=execution_deadline)
        records[entry.name] = record
        total_entries += 1
        total_bytes += record[2] if record[0] == "file" else 0
        if record[0] == "directory":
            nested = _host_bounded_control_tree(
                path,
                execution_deadline=execution_deadline,
                max_entries=max(1, 10_000 - total_entries),
                max_bytes=max(1, 64 * 1024 * 1024 - total_bytes),
            )
            for relative, nested_record in nested.items():
                records[f"{entry.name}/{relative}"] = nested_record
                total_entries += 1
                total_bytes += nested_record[2] if nested_record[0] == "file" else 0
        if total_entries > 10_000 or total_bytes > 64 * 1024 * 1024:
            raise GitOpsError("Host transaction Git controls exceed snapshot limits")
    encoded = json.dumps(
        sorted(records.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _host_workspace_snapshot(
    root: Path,
    *,
    execution_deadline: ExecutionDeadline | None,
    allowed_artifact_paths: tuple[Path, ...],
) -> dict[str, tuple[str, int, int, str]]:
    snapshot_root = root.resolve(strict=True)
    inventory_commands = (
        ["ls-files", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    )
    inventory: set[str] = set()
    inventory_bytes = 0
    for command in inventory_commands:
        output = _bounded_host_git_output(
            snapshot_root,
            command,
            execution_deadline=execution_deadline,
            error_message="failed to inventory Host workspace paths",
        )
        inventory_bytes += len(output.encode("utf-8", errors="surrogatepass"))
        if inventory_bytes > 32 * 1024 * 1024:
            raise GitOpsError("Host workspace inventory exceeds the byte limit")
        inventory.update(item for item in output.split("\0") if item)
        if len(inventory) > 100_000:
            raise GitOpsError("Host workspace inventory exceeds the entry limit")

    allowed = frozenset(path.resolve(strict=False) for path in allowed_artifact_paths)
    records: dict[str, tuple[str, int, int, str]] = {}
    total_bytes = 0
    for relative in sorted(inventory):
        candidate = snapshot_root / relative
        try:
            candidate.relative_to(snapshot_root)
        except ValueError as exc:
            raise GitOpsError("Host workspace inventory escaped the repository root") from exc
        if candidate.resolve(strict=False) in allowed:
            continue
        record = _host_path_record(
            candidate,
            execution_deadline=execution_deadline,
        )
        if record[0] == "directory":
            raise GitOpsError("Host workspace inventory contains an unsupported directory entry")
        total_bytes += record[2] if record[0] == "file" else 0
        if total_bytes > 512 * 1024 * 1024:
            raise GitOpsError("Host workspace snapshot exceeds the content byte limit")
        records[relative] = record
    return records


def _host_nonruntime_ignored_paths(
    root: Path,
    *,
    execution_deadline: ExecutionDeadline | None,
) -> tuple[str, ...]:
    """Inventory ignored material that Phase 0 cannot attribute to Git diffs."""

    output = _bounded_host_git_output(
        root,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        execution_deadline=execution_deadline,
        error_message="failed to inventory ignored Host workspace paths",
    )
    ignored = {
        item
        for item in output.split("\0")
        if item and item not in {".alysis"} and not item.startswith((".alysis/", ".alysis/"))
    }
    return tuple(sorted(ignored))


def _host_snapshot_changed_paths(
    before: dict[str, tuple[str, int, int, str]],
    after: dict[str, tuple[str, int, int, str]],
) -> list[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _try_resolve_host_git_commit(
    root: Path,
    revision: str,
    *,
    execution_deadline: ExecutionDeadline | None,
) -> str | None:
    try:
        return _resolve_host_git_commit(
            root,
            revision,
            execution_deadline=execution_deadline,
        )
    except GitOpsError:
        return None


def _prepare_new_host_task_integration(
    root: Path,
    *,
    native_run_id: str,
    task_id: str,
    journal_dir: Path,
    base_branch: str,
    base_head: str,
    task_branch: str,
    task_commit: str,
    message: str,
    execution_deadline: ExecutionDeadline,
    mutable_runtime_paths: tuple[Path, ...],
) -> PreparedIntegration:
    """Prepare a merge commit and private ref without changing the approved base."""

    observed_base = _resolve_host_git_commit(
        root,
        f"refs/heads/{base_branch}",
        execution_deadline=execution_deadline,
    )
    if observed_base != base_head:
        raise GitOpsError("approved base branch changed before merge")
    observed_task = _resolve_host_git_commit(
        root,
        f"refs/heads/{task_branch}",
        execution_deadline=execution_deadline,
    )
    if observed_task != task_commit:
        raise GitOpsError("task branch changed after exact Host verification")
    transaction_id = "integration_" + uuid.uuid4().hex[:24]
    prepared_ref = f"refs/alysis/forge-host-v2/{transaction_id}"
    zero_oid = "0" * len(base_head)
    merge_commit: str | None = None
    bound_workspace_snapshot: dict[str, tuple[str, int, int, str]] | None = None
    bound_refs: dict[str, str] | None = None
    bound_stable_control_digest: str | None = None

    try:
        merge_tree = _run_host_git_checked(
            root,
            ["merge-tree", "--write-tree", "--no-messages", base_head, task_commit],
            execution_deadline=execution_deadline,
            error_message="failed to prepare the Host merge tree",
        ).stdout.strip()
        if not merge_tree:
            raise GitOpsError("prepared Host merge tree is empty")
        merge_commit = _run_host_git_checked(
            root,
            [
                "commit-tree",
                merge_tree,
                "-p",
                base_head,
                "-p",
                task_commit,
                "-m",
                message,
            ],
            execution_deadline=execution_deadline,
            error_message="failed to create the prepared Host merge commit",
            extra_config={
                "user.name": DEFAULT_COMMIT_AUTHOR_NAME,
                "user.email": DEFAULT_COMMIT_AUTHOR_EMAIL,
            },
        ).stdout.strip()
        first_parent = _resolve_host_git_commit(
            root,
            f"{merge_commit}^1",
            execution_deadline=execution_deadline,
        )
        second_parent = _resolve_host_git_commit(
            root,
            f"{merge_commit}^2",
            execution_deadline=execution_deadline,
        )
        if first_parent != base_head or second_parent != task_commit:
            raise GitOpsError("Host merge parents did not match the approved commits")
        _run_host_git_checked(
            root,
            ["update-ref", prepared_ref, merge_commit, zero_oid],
            execution_deadline=execution_deadline,
            error_message="failed to retain the prepared Host merge",
        )
        if (
            _resolve_host_git_commit(
                root,
                f"refs/heads/{base_branch}",
                execution_deadline=execution_deadline,
            )
            != base_head
        ):
            raise GitOpsError("approved base branch changed during merge preparation")
        symbolic_head = _run_host_git_checked(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            execution_deadline=execution_deadline,
            error_message="failed to verify the prepared Host task checkout",
        ).stdout.strip()
        current_head = _resolve_host_git_commit(
            root,
            "HEAD",
            execution_deadline=execution_deadline,
        )
        worktree_status = _run_host_git_checked(
            root,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            execution_deadline=execution_deadline,
            error_message="failed to verify the prepared Host workspace",
        ).stdout
        if symbolic_head != task_branch or current_head != task_commit or worktree_status:
            raise GitOpsError("merge preparation changed the Host task checkout or index")
        prepared_elapsed_ms = max(0, round(execution_deadline.elapsed_seconds() * 1000))
    except Exception:
        cleanup_deadline = ExecutionDeadline.from_duration(10.0)
        if merge_commit is not None:
            current_prepared = _try_resolve_host_git_commit(
                root,
                prepared_ref,
                execution_deadline=cleanup_deadline,
            )
            if current_prepared == merge_commit:
                try:
                    _run_host_git_checked(
                        root,
                        ["update-ref", "-d", prepared_ref, merge_commit],
                        execution_deadline=cleanup_deadline,
                        error_message="failed to delete an incomplete prepared Host ref",
                    )
                except GitOpsError:
                    pass
        raise

    assert merge_commit is not None
    facts = PreparedIntegrationFacts(
        transaction_id=transaction_id,
        native_run_id=native_run_id,
        task_id=task_id,
        base_branch=base_branch,
        approved_base_head=base_head,
        task_branch=task_branch,
        task_commit=task_commit,
        prepared_merge_commit=merge_commit,
        prepared_ref=prepared_ref,
        prepared_elapsed_ms=prepared_elapsed_ms,
        journal_path=journal_dir / f"{transaction_id}.json",
    )

    allowed_transaction_paths = (*mutable_runtime_paths, facts.journal_path)

    def _assert_clean_task_checkout(
        *,
        deadline: ExecutionDeadline,
        expected_base: str,
        phase: str,
    ) -> None:
        symbolic_head = _run_host_git_checked(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            execution_deadline=deadline,
            error_message=f"failed to inspect the Host task checkout during {phase}",
        ).stdout.strip()
        current_head = _resolve_host_git_commit(
            root,
            "HEAD",
            execution_deadline=deadline,
        )
        observed_base = _resolve_host_git_commit(
            root,
            f"refs/heads/{base_branch}",
            execution_deadline=deadline,
        )
        observed_task = _resolve_host_git_commit(
            root,
            f"refs/heads/{task_branch}",
            execution_deadline=deadline,
        )
        observed_prepared = _resolve_host_git_commit(
            root,
            prepared_ref,
            execution_deadline=deadline,
        )
        worktree_status = _run_host_git_checked(
            root,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            execution_deadline=deadline,
            error_message=f"failed to inspect the Host workspace during {phase}",
        ).stdout
        if (
            symbolic_head != task_branch
            or current_head != task_commit
            or observed_base != expected_base
            or observed_task != task_commit
            or observed_prepared != merge_commit
            or worktree_status
        ):
            raise GitOpsError(f"Host Git authority changed during {phase}")

    def bind_guarded_state() -> None:
        nonlocal bound_refs, bound_stable_control_digest, bound_workspace_snapshot
        _assert_clean_task_checkout(
            deadline=execution_deadline,
            expected_base=base_head,
            phase="transaction adoption",
        )
        bound_workspace_snapshot = _host_workspace_snapshot(
            root,
            execution_deadline=execution_deadline,
            allowed_artifact_paths=allowed_transaction_paths,
        )
        bound_refs = _host_ref_snapshot(
            root,
            execution_deadline=execution_deadline,
        )
        bound_stable_control_digest = _host_stable_git_control_digest(
            root,
            execution_deadline=execution_deadline,
        )
        if (
            bound_refs.get(f"refs/heads/{base_branch}") != base_head
            or bound_refs.get(f"refs/heads/{task_branch}") != task_commit
            or bound_refs.get(prepared_ref) != merge_commit
        ):
            raise GitOpsError("Host transaction ref authority changed during adoption")

    def rollback_git(_proof: IntegrationCommitProof | None) -> None:
        rollback_deadline = ExecutionDeadline.from_duration(10.0)
        if (
            bound_workspace_snapshot is not None
            and bound_refs is not None
            and bound_stable_control_digest is not None
        ):
            current_refs = _host_ref_snapshot(
                root,
                execution_deadline=rollback_deadline,
            )
            allowed_refs = dict(bound_refs)
            current_base_for_projection = current_refs.get(f"refs/heads/{base_branch}")
            current_prepared_for_projection = current_refs.get(prepared_ref)
            if current_base_for_projection == merge_commit:
                allowed_refs[f"refs/heads/{base_branch}"] = merge_commit
            if current_prepared_for_projection is None:
                allowed_refs.pop(prepared_ref, None)
            if current_refs != allowed_refs:
                raise GitOpsError("Host ref namespace changed before transaction rollback")
            if (
                _host_stable_git_control_digest(
                    root,
                    execution_deadline=rollback_deadline,
                )
                != bound_stable_control_digest
            ):
                raise GitOpsError("Host Git controls changed before transaction rollback")
            if (
                _host_workspace_snapshot(
                    root,
                    execution_deadline=rollback_deadline,
                    allowed_artifact_paths=allowed_transaction_paths,
                )
                != bound_workspace_snapshot
            ):
                raise GitOpsError("Host workspace changed before transaction rollback")
        current_base = _resolve_host_git_commit(
            root,
            f"refs/heads/{base_branch}",
            execution_deadline=rollback_deadline,
        )
        if current_base == merge_commit:
            _run_host_git_checked(
                root,
                ["update-ref", f"refs/heads/{base_branch}", base_head, merge_commit],
                execution_deadline=rollback_deadline,
                error_message="failed to roll back the reversible Host base ref",
            )
        elif current_base != base_head:
            raise GitOpsError("Host base ref cannot be rolled back from an unknown identity")
        current_task = _resolve_host_git_commit(
            root,
            f"refs/heads/{task_branch}",
            execution_deadline=rollback_deadline,
        )
        if current_task != task_commit:
            raise GitOpsError("Host task ref changed before transaction rollback")
        symbolic_head = _run_host_git_checked(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            execution_deadline=rollback_deadline,
            error_message="failed to inspect the Host checkout during rollback",
        ).stdout.strip()
        if symbolic_head == base_branch:
            _run_host_git_checked(
                root,
                ["checkout", task_branch],
                execution_deadline=rollback_deadline,
                error_message="failed to restore the Host task branch during rollback",
            )
        elif symbolic_head != task_branch:
            raise GitOpsError("Host checkout changed before transaction rollback")
        if (
            _resolve_host_git_commit(
                root,
                "HEAD",
                execution_deadline=rollback_deadline,
            )
            != task_commit
        ):
            raise GitOpsError("Host task checkout could not be restored during rollback")
        current_prepared = _try_resolve_host_git_commit(
            root,
            prepared_ref,
            execution_deadline=rollback_deadline,
        )
        if current_prepared == merge_commit:
            _run_host_git_checked(
                root,
                ["update-ref", "-d", prepared_ref, merge_commit],
                execution_deadline=rollback_deadline,
                error_message="failed to delete the prepared Host integration ref",
            )
        elif current_prepared is not None:
            raise GitOpsError("prepared Host ref changed before transaction rollback")

    def commit_git() -> IntegrationCommitProof:
        if bound_workspace_snapshot is None:
            raise GitOpsError("Host transaction was not bound under the mutation guard")
        _check_host_snapshot_deadline(execution_deadline)
        _assert_clean_task_checkout(
            deadline=execution_deadline,
            expected_base=base_head,
            phase="reversible commit",
        )
        if (
            _host_workspace_snapshot(
                root,
                execution_deadline=execution_deadline,
                allowed_artifact_paths=allowed_transaction_paths,
            )
            != bound_workspace_snapshot
        ):
            raise GitOpsError("Host workspace changed before reversible commit")
        _run_host_git_checked(
            root,
            ["update-ref", f"refs/heads/{base_branch}", merge_commit, base_head],
            execution_deadline=execution_deadline,
            error_message="approved base branch changed during reversible commit",
        )
        committed_elapsed_seconds = execution_deadline.elapsed_seconds()
        deadline_span = (
            execution_deadline.deadline_monotonic - execution_deadline.started_at_monotonic
            if execution_deadline.deadline_monotonic is not None
            else None
        )
        if deadline_span is not None and committed_elapsed_seconds >= deadline_span:
            raise GitOpsError("run deadline exhausted at the Host integration commit point")
        if (
            _resolve_host_git_commit(
                root,
                f"refs/heads/{base_branch}",
                execution_deadline=ExecutionDeadline.from_duration(10.0),
            )
            != merge_commit
        ):
            raise GitOpsError("reversible Host integration identity changed after CAS")
        return IntegrationCommitProof(
            base_branch=base_branch,
            approved_base_head=base_head,
            task_commit=task_commit,
            merge_commit=merge_commit,
            integration_committed_elapsed_ms=max(
                0,
                round(committed_elapsed_seconds * 1000),
            ),
        )

    def cleanup_prepared_git() -> None:
        if bound_workspace_snapshot is None:
            raise GitOpsError("Host transaction was not bound under the mutation guard")
        cleanup_deadline = ExecutionDeadline.from_duration(10.0)
        _assert_clean_task_checkout(
            deadline=cleanup_deadline,
            expected_base=merge_commit,
            phase="transaction seal",
        )
        if (
            _host_workspace_snapshot(
                root,
                execution_deadline=cleanup_deadline,
                allowed_artifact_paths=allowed_transaction_paths,
            )
            != bound_workspace_snapshot
        ):
            raise GitOpsError("Host workspace changed before transaction seal")
        _run_host_git_checked(
            root,
            ["checkout", base_branch],
            execution_deadline=cleanup_deadline,
            error_message="failed to publish the sealed Host base checkout",
        )
        if (
            _resolve_host_git_commit(root, "HEAD", execution_deadline=cleanup_deadline)
            != merge_commit
            or _run_host_git_checked(
                root,
                ["status", "--porcelain=v1", "--untracked-files=all"],
                execution_deadline=cleanup_deadline,
                error_message="failed to verify the sealed Host checkout",
            ).stdout
        ):
            raise GitOpsError("sealed Host checkout did not match the prepared merge")
        _run_host_git_checked(
            root,
            ["update-ref", "-d", prepared_ref, merge_commit],
            execution_deadline=cleanup_deadline,
            error_message="failed to delete the sealed Host integration ref",
        )

    def validate_final_authority() -> None:
        if (
            bound_workspace_snapshot is None
            or bound_refs is None
            or bound_stable_control_digest is None
        ):
            raise GitOpsError("Host transaction was not bound under the mutation guard")
        _check_host_snapshot_deadline(execution_deadline)
        symbolic_head = _run_host_git_checked(
            root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            execution_deadline=execution_deadline,
            error_message="failed to inspect final Host checkout authority",
        ).stdout.strip()
        if (
            symbolic_head != base_branch
            or _resolve_host_git_commit(
                root,
                "HEAD",
                execution_deadline=execution_deadline,
            )
            != merge_commit
            or _resolve_host_git_commit(
                root,
                f"{merge_commit}^1",
                execution_deadline=execution_deadline,
            )
            != base_head
            or _resolve_host_git_commit(
                root,
                f"{merge_commit}^2",
                execution_deadline=execution_deadline,
            )
            != task_commit
        ):
            raise GitOpsError("final Host merge authority changed before publication")
        expected_refs = dict(bound_refs)
        expected_refs[f"refs/heads/{base_branch}"] = merge_commit
        expected_refs.pop(prepared_ref, None)
        if (
            _host_ref_snapshot(
                root,
                execution_deadline=execution_deadline,
            )
            != expected_refs
        ):
            raise GitOpsError("final Host ref namespace changed before publication")
        if (
            _host_stable_git_control_digest(
                root,
                execution_deadline=execution_deadline,
            )
            != bound_stable_control_digest
        ):
            raise GitOpsError("final Host Git controls changed before publication")
        _run_host_git_checked(
            root,
            ["diff", "--quiet", merge_commit, "--"],
            execution_deadline=execution_deadline,
            error_message="final Host tracked workspace changed before publication",
        )
        _run_host_git_checked(
            root,
            ["diff", "--cached", "--quiet", merge_commit, "--"],
            execution_deadline=execution_deadline,
            error_message="final Host index changed before publication",
        )
        if (
            _host_workspace_snapshot(
                root,
                execution_deadline=execution_deadline,
                allowed_artifact_paths=allowed_transaction_paths,
            )
            != bound_workspace_snapshot
        ):
            raise GitOpsError("final Host workspace changed before publication")

    return PreparedIntegration(
        facts=facts,
        execution_deadline=execution_deadline,
        bind_guarded_state=bind_guarded_state,
        commit_git=commit_git,
        rollback_git=rollback_git,
        cleanup_prepared_git=cleanup_prepared_git,
        validate_final_authority=validate_final_authority,
    )


_HOST_TRANSACTION_SCHEMA = "alysis.forge-host.integration-transaction/v1"
_HOST_TRANSACTION_STATES = frozenset(
    {
        "PREPARED",
        "COMMITTED_REVERSIBLE",
        "PUBLISH_READY",
        "SEALED",
        "ROLLED_BACK",
        "ROLLBACK_FAILED",
    }
)
_HOST_TRANSACTION_MAX_JOURNALS = 128
_HOST_TRANSACTION_MAX_JOURNAL_BYTES = 4 * 1024 * 1024


def _secure_host_transaction_journal_dir(paths: Any) -> Path | None:
    root = paths.root.resolve(strict=True)
    candidate = paths.execution_integration_dir / "transactions"
    try:
        relative = candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise GitOpsError("Host transaction journal directory escaped the workspace") from exc
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GitOpsError("Host transaction journal directory is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GitOpsError("Host transaction journal directory changed type")
    if current != candidate:
        raise GitOpsError("Host transaction journal directory is not canonical")
    return current


def _load_host_transaction_journal(
    path: Path,
    *,
    paths: Any,
) -> dict[str, Any]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise GitOpsError("Host transaction journal is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise GitOpsError("Host transaction journal changed type")
    if before.st_size > _HOST_TRANSACTION_MAX_JOURNAL_BYTES:
        raise GitOpsError("Host transaction journal exceeds the recovery byte limit")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        after = path.lstat()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GitOpsError("Host transaction journal could not be decoded") from exc
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
        raise GitOpsError("Host transaction journal changed while being read")
    if not isinstance(payload, dict):
        raise GitOpsError("Host transaction journal payload is invalid")
    transaction_id = payload.get("transactionId")
    suffix = transaction_id.removeprefix("integration_") if isinstance(transaction_id, str) else ""
    state = payload.get("state")
    prepared_ref = payload.get("preparedRef")
    journal_path = payload.get("journalPath")
    native_run_id = payload.get("nativeRunId")
    if (
        payload.get("schema") != _HOST_TRANSACTION_SCHEMA
        or state not in _HOST_TRANSACTION_STATES
        or len(suffix) != 24
        or any(character not in "0123456789abcdef" for character in suffix)
        or path.name != f"{transaction_id}.json"
        or prepared_ref != f"refs/alysis/forge-host-v2/{transaction_id}"
        or journal_path != os.fspath(path)
        or native_run_id != paths.run_id
    ):
        raise GitOpsError("Host transaction journal authority is invalid")
    preimage = payload.get("nativePreimage")
    if not isinstance(preimage, Mapping):
        raise GitOpsError("Host transaction journal lacks a native preimage")
    resolved_root = paths.root.resolve(strict=True)
    root_stat = resolved_root.stat()
    try:
        parsed_preimage = NativeRunStatePreimage.from_payload(preimage)
    except PreparedIntegrationRollbackError as exc:
        raise GitOpsError("Host transaction journal native preimage is invalid") from exc
    if (
        parsed_preimage.workspace_root != resolved_root
        or parsed_preimage.workspace_device != root_stat.st_dev
        or parsed_preimage.workspace_inode != root_stat.st_ino
    ):
        raise GitOpsError("Host transaction journal workspace identity changed")
    lexical_root = Path(os.path.abspath(paths.root))
    try:
        expected_native_paths = {
            Path(os.path.abspath(candidate)).relative_to(lexical_root).as_posix()
            for candidate in (
                paths.plan_json_path,
                paths.plan_md_path,
                paths.runtime_dir / "current_run.json",
                paths.execution_dir / "sequential_summary.json",
                paths.execution_dir / "sequential_run.jsonl",
            )
        }
    except ValueError as exc:
        raise GitOpsError("canonical Host native state paths escaped the workspace") from exc
    if {item.relative_path for item in parsed_preimage.files} != expected_native_paths:
        raise GitOpsError("Host transaction journal native preimage paths are not canonical")
    if state in {"PREPARED", "COMMITTED_REVERSIBLE", "PUBLISH_READY"}:
        try:
            validate_native_run_state_payload(preimage)
        except PreparedIntegrationRollbackError as exc:
            raise GitOpsError("Host transaction native recovery targets changed") from exc
    postimage = payload.get("nativePostimage")
    if state in {"PUBLISH_READY", "SEALED"} and not isinstance(postimage, Mapping):
        raise GitOpsError("Host transaction journal lacks its native postimage")
    if state in {"PREPARED", "COMMITTED_REVERSIBLE"} and postimage is not None:
        raise GitOpsError("unfinished Host transaction carries a native postimage")
    if isinstance(postimage, Mapping):
        try:
            parsed_postimage = NativeRunStatePreimage.from_payload(postimage)
        except PreparedIntegrationRollbackError as exc:
            raise GitOpsError("Host transaction journal native postimage is invalid") from exc
        if (
            parsed_postimage.workspace_root != parsed_preimage.workspace_root
            or parsed_postimage.workspace_device != parsed_preimage.workspace_device
            or parsed_postimage.workspace_inode != parsed_preimage.workspace_inode
            or parsed_postimage.ancestors != parsed_preimage.ancestors
            or {item.relative_path for item in parsed_postimage.files} != expected_native_paths
        ):
            raise GitOpsError("Host transaction journal native postimage is not canonical")
        if state == "PUBLISH_READY":
            try:
                validate_native_run_state_matches_payload(postimage)
            except PreparedIntegrationRollbackError as exc:
                raise GitOpsError("Host transaction native postimage changed") from exc
    task_id = payload.get("taskId")
    prepared_elapsed_ms = payload.get("preparedElapsedMs")
    if (
        not isinstance(task_id, str)
        or not task_id.strip()
        or task_id != task_id.strip()
        or isinstance(prepared_elapsed_ms, bool)
        or not isinstance(prepared_elapsed_ms, int)
        or prepared_elapsed_ms < 0
    ):
        raise GitOpsError("Host transaction journal task authority is invalid")
    plan_relative = Path(os.path.abspath(paths.plan_json_path)).relative_to(lexical_root).as_posix()
    plan_preimages = [item for item in parsed_preimage.files if item.relative_path == plan_relative]
    if len(plan_preimages) != 1 or not plan_preimages[0].existed:
        raise GitOpsError("Host transaction journal lacks the approved plan preimage")
    try:
        approved_plan = json.loads(plan_preimages[0].text)
    except json.JSONDecodeError as exc:
        raise GitOpsError("Host transaction approved plan preimage is invalid") from exc
    if not isinstance(approved_plan, dict):
        raise GitOpsError("Host transaction approved plan preimage is invalid")
    approved_tasks = [
        item
        for item in approved_plan.get("tasks", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip() == task_id
    ]
    if len(approved_tasks) != 1:
        raise GitOpsError("Host transaction task is not bound to the approved plan")
    for key in (
        "baseBranch",
        "approvedBaseHead",
        "taskBranch",
        "taskCommit",
        "preparedMergeCommit",
    ):
        if not isinstance(payload.get(key), str) or not str(payload[key]).strip():
            raise GitOpsError("Host transaction journal Git authority is invalid")
    for key in ("approvedBaseHead", "taskCommit", "preparedMergeCommit"):
        commit_id = str(payload[key])
        if (
            len(commit_id) not in {40, 64}
            or commit_id != commit_id.lower()
            or any(character not in "0123456789abcdef" for character in commit_id)
        ):
            raise GitOpsError("Host transaction journal commit identity is invalid")
    base_branch = str(payload["baseBranch"])
    task_branch = str(payload["taskBranch"])
    if base_branch == task_branch:
        raise GitOpsError("Host transaction journal branch authority is invalid")
    for branch in (base_branch, task_branch):
        checked = _run_host_git_checked(
            paths.root,
            ["check-ref-format", "--branch", branch],
            execution_deadline=ExecutionDeadline.from_duration(10.0),
            error_message="Host transaction journal branch authority is invalid",
        ).stdout.strip()
        if checked != branch:
            raise GitOpsError("Host transaction journal branch authority is invalid")
    approved_task = approved_tasks[0]
    approved_branch = str(approved_task.get("branch") or "").strip()
    if not approved_branch:
        approved_branch = generate_task_branch_name(
            task_id,
            str(approved_task.get("title") or ""),
        )
    if task_branch != approved_branch:
        raise GitOpsError("Host transaction task branch is not bound to the approved plan")

    proof = payload.get("integrationProof")
    finalize = payload.get("finalizeProof")
    publication = payload.get("authoritativePublication")
    requires_proof = state in {"COMMITTED_REVERSIBLE", "PUBLISH_READY", "SEALED"}
    requires_finalize = state in {"PUBLISH_READY", "SEALED"}
    if requires_proof and not isinstance(proof, Mapping):
        raise GitOpsError("Host transaction journal lacks its integration proof")
    if requires_finalize and not isinstance(finalize, Mapping):
        raise GitOpsError("Host transaction journal lacks its finalization proof")
    if isinstance(proof, Mapping):
        committed_elapsed = proof.get("integrationCommittedElapsedMs")
        if (
            proof.get("baseBranch") != base_branch
            or proof.get("approvedBaseHead") != payload["approvedBaseHead"]
            or proof.get("taskCommit") != payload["taskCommit"]
            or proof.get("mergeCommit") != payload["preparedMergeCommit"]
            or isinstance(committed_elapsed, bool)
            or not isinstance(committed_elapsed, int)
            or committed_elapsed < prepared_elapsed_ms
        ):
            raise GitOpsError("Host transaction journal integration proof is invalid")
    if isinstance(finalize, Mapping):
        final_elapsed = finalize.get("finalElapsedMs")
        committed_elapsed = (
            proof.get("integrationCommittedElapsedMs") if isinstance(proof, Mapping) else None
        )
        if (
            isinstance(final_elapsed, bool)
            or not isinstance(final_elapsed, int)
            or not isinstance(committed_elapsed, int)
            or final_elapsed < committed_elapsed
        ):
            raise GitOpsError("Host transaction journal finalization proof is invalid")
    if state == "SEALED":
        if not isinstance(publication, Mapping):
            raise GitOpsError("sealed Host transaction lacks publication authority")
        digest = publication.get("eventBytesDigest")
        event_bytes = publication.get("eventBytes")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(event_bytes, bool)
            or not isinstance(event_bytes, int)
            or event_bytes < 1
        ):
            raise GitOpsError("sealed Host transaction publication authority is invalid")
    elif publication is not None:
        raise GitOpsError("unsealed Host transaction carries publication authority")
    return payload


def _write_host_recovery_journal(
    path: Path,
    payload: dict[str, Any],
    *,
    state: str,
    reason: str,
    rollback_error: str | None = None,
) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GitOpsError("Host transaction recovery journal changed type")
    updated = dict(payload)
    updated["state"] = state
    updated["manualAttention"] = state == "ROLLBACK_FAILED"
    updated["reason"] = sanitize_error_summary(reason, max_chars=320)
    if rollback_error:
        updated["rollbackError"] = sanitize_error_summary(
            rollback_error,
            max_chars=320,
        )
    else:
        updated.pop("rollbackError", None)
    atomic_write_text(path, json.dumps(updated, indent=2, sort_keys=True) + "\n")


def _validate_host_recovery_git_authority(
    *,
    paths: Any,
    payload: Mapping[str, Any],
    live_refs: Mapping[str, str],
    execution_deadline: ExecutionDeadline,
) -> None:
    """Fail closed before recovery can restore a file or move a ref."""

    state = str(payload["state"])
    base_branch = str(payload["baseBranch"])
    base_head = str(payload["approvedBaseHead"])
    task_branch = str(payload["taskBranch"])
    task_commit = str(payload["taskCommit"])
    merge_commit = str(payload["preparedMergeCommit"])
    prepared_ref = str(payload["preparedRef"])
    for commit in (base_head, task_commit, merge_commit):
        if (
            _resolve_host_git_commit(
                paths.root,
                commit,
                execution_deadline=execution_deadline,
            )
            != commit
        ):
            raise GitOpsError("Host transaction recovery commit authority changed")
    parent_line = (
        _run_host_git_checked(
            paths.root,
            ["rev-list", "--parents", "-n", "1", merge_commit],
            execution_deadline=execution_deadline,
            error_message="failed to inspect Host transaction merge parents",
        )
        .stdout.strip()
        .split()
    )
    if parent_line != [merge_commit, base_head, task_commit]:
        raise GitOpsError("Host transaction recovery merge parents are invalid")
    merge_tree = _run_host_git_checked(
        paths.root,
        ["show", "-s", "--format=%T", merge_commit],
        execution_deadline=execution_deadline,
        error_message="failed to inspect Host transaction merge tree",
    ).stdout.strip()
    task_tree = _run_host_git_checked(
        paths.root,
        ["show", "-s", "--format=%T", task_commit],
        execution_deadline=execution_deadline,
        error_message="failed to inspect Host transaction task tree",
    ).stdout.strip()
    if merge_tree != task_tree:
        raise GitOpsError("Host transaction recovery merge tree is invalid")
    _run_host_git_checked(
        paths.root,
        ["merge-base", "--is-ancestor", base_head, task_commit],
        execution_deadline=execution_deadline,
        error_message="Host transaction task is not based on the approved commit",
    )
    current_base = _resolve_host_git_commit(
        paths.root,
        f"refs/heads/{base_branch}",
        execution_deadline=execution_deadline,
    )
    current_task = _resolve_host_git_commit(
        paths.root,
        f"refs/heads/{task_branch}",
        execution_deadline=execution_deadline,
    )
    current_prepared = live_refs.get(prepared_ref)
    if (
        current_base not in {base_head, merge_commit}
        or current_task != task_commit
        or current_prepared not in {None, merge_commit}
    ):
        raise GitOpsError("Host transaction recovery ref authority changed")
    if state == "SEALED" and (current_base != merge_commit or current_prepared is not None):
        raise GitOpsError("sealed Host transaction Git authority is inconsistent")
    if state == "ROLLED_BACK" and (current_base != base_head or current_prepared is not None):
        raise GitOpsError("rolled-back Host transaction Git authority is inconsistent")
    symbolic_head = _run_host_git_checked(
        paths.root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        execution_deadline=execution_deadline,
        error_message="failed to inspect Host transaction recovery checkout",
    ).stdout.strip()
    if symbolic_head not in {base_branch, task_branch}:
        raise GitOpsError("Host transaction recovery checkout authority changed")
    expected_head = current_base if symbolic_head == base_branch else task_commit
    if (
        _resolve_host_git_commit(
            paths.root,
            "HEAD",
            execution_deadline=execution_deadline,
        )
        != expected_head
    ):
        raise GitOpsError("Host transaction recovery HEAD authority changed")
    status = _run_host_git_checked(
        paths.root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        execution_deadline=execution_deadline,
        error_message="failed to inspect Host transaction recovery workspace",
    ).stdout
    if status or _host_nonruntime_ignored_paths(
        paths.root,
        execution_deadline=execution_deadline,
    ):
        raise GitOpsError("Host transaction recovery workspace is not clean")


def recover_host_integration_transactions(paths: Any) -> None:
    """Roll back every unsealed run-local Host integration under the caller's guard."""

    journal_dir = _secure_host_transaction_journal_dir(paths)
    cleanup_deadline = ExecutionDeadline.from_duration(30.0)
    journals: dict[str, tuple[Path, dict[str, Any]]] = {}
    if journal_dir is not None:
        try:
            entries = sorted(os.scandir(journal_dir), key=lambda item: item.name)
        except OSError as exc:
            raise GitOpsError("failed to enumerate Host transaction journals") from exc
        if len(entries) > _HOST_TRANSACTION_MAX_JOURNALS:
            raise GitOpsError("Host transaction journal count exceeds the recovery limit")
        for entry in entries:
            if not entry.name.endswith(".json"):
                raise GitOpsError("Host transaction journal directory contains an unknown entry")
            path = Path(entry.path)
            payload = _load_host_transaction_journal(path, paths=paths)
            transaction_id = str(payload["transactionId"])
            if transaction_id in journals:
                raise GitOpsError("Host transaction journal identity is duplicated")
            journals[transaction_id] = (path, payload)

    live_refs_output = _bounded_host_git_output(
        paths.root,
        [
            "for-each-ref",
            "--sort=refname",
            "--format=%(refname)%00%(objectname)",
            "refs/alysis/forge-host-v2/",
        ],
        execution_deadline=cleanup_deadline,
        error_message="failed to inventory prepared Host integration refs",
        max_records=_HOST_TRANSACTION_MAX_JOURNALS * 2,
    )
    live_refs: dict[str, str] = {}
    for record in live_refs_output.splitlines():
        parts = record.split("\0")
        if len(parts) != 2 or not all(parts) or parts[0] in live_refs:
            raise GitOpsError("prepared Host integration ref inventory is malformed")
        live_refs[parts[0]] = parts[1]

    for _transaction_id, (journal_path, payload) in journals.items():
        if payload["state"] in {"SEALED", "ROLLED_BACK"}:
            if str(payload["preparedRef"]) in live_refs:
                raise GitOpsError("terminal Host transaction retained an impossible private ref")
            continue
        if payload["state"] == "ROLLBACK_FAILED":
            raise GitOpsError("Host transaction recovery requires manual attention")
        _validate_host_recovery_git_authority(
            paths=paths,
            payload=payload,
            live_refs=live_refs,
            execution_deadline=cleanup_deadline,
        )
        base_branch = str(payload["baseBranch"])
        base_head = str(payload["approvedBaseHead"])
        task_branch = str(payload["taskBranch"])
        task_commit = str(payload["taskCommit"])
        merge_commit = str(payload["preparedMergeCommit"])
        prepared_ref = str(payload["preparedRef"])
        try:
            current_base = _resolve_host_git_commit(
                paths.root,
                f"refs/heads/{base_branch}",
                execution_deadline=cleanup_deadline,
            )
            if current_base == merge_commit:
                _run_host_git_checked(
                    paths.root,
                    ["update-ref", f"refs/heads/{base_branch}", base_head, merge_commit],
                    execution_deadline=cleanup_deadline,
                    error_message="failed to recover the approved Host base ref",
                )
            elif current_base != base_head:
                raise GitOpsError("Host base ref has an unknown recovery identity")
            if (
                _resolve_host_git_commit(
                    paths.root,
                    f"refs/heads/{task_branch}",
                    execution_deadline=cleanup_deadline,
                )
                != task_commit
            ):
                raise GitOpsError("Host task ref changed before crash recovery")
            symbolic_head = _run_host_git_checked(
                paths.root,
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                execution_deadline=cleanup_deadline,
                error_message="failed to inspect the Host crash-recovery checkout",
            ).stdout.strip()
            if symbolic_head == base_branch:
                _run_host_git_checked(
                    paths.root,
                    ["checkout", task_branch],
                    execution_deadline=cleanup_deadline,
                    error_message="failed to restore the Host task checkout after a crash",
                )
            elif symbolic_head != task_branch:
                raise GitOpsError("Host checkout changed before crash recovery")
            if (
                _resolve_host_git_commit(
                    paths.root,
                    "HEAD",
                    execution_deadline=cleanup_deadline,
                )
                != task_commit
            ):
                raise GitOpsError("Host task checkout was not restored after a crash")
            current_prepared = live_refs.get(prepared_ref)
            if current_prepared == merge_commit:
                _run_host_git_checked(
                    paths.root,
                    ["update-ref", "-d", prepared_ref, merge_commit],
                    execution_deadline=cleanup_deadline,
                    error_message="failed to remove the recovered Host prepared ref",
                )
                live_refs.pop(prepared_ref, None)
            elif current_prepared is not None:
                raise GitOpsError("prepared Host ref changed before crash recovery")
            preimage = payload["nativePreimage"]
            assert isinstance(preimage, Mapping)
            restore_native_run_state_payload(preimage)
            _write_host_recovery_journal(
                journal_path,
                payload,
                state="ROLLED_BACK",
                reason="unsealed Host integration rolled back during guarded startup recovery",
            )
        except Exception as exc:
            error = sanitize_error_summary(str(exc) or exc.__class__.__name__)
            try:
                _write_host_recovery_journal(
                    journal_path,
                    payload,
                    state="ROLLBACK_FAILED",
                    reason="guarded startup recovery failed",
                    rollback_error=error,
                )
            except Exception:
                pass
            raise GitOpsError(
                "Host transaction recovery failed; manual attention is required"
            ) from exc

    # A crash can happen after the private ref is created but before the first
    # journal is durably adopted.  The namespace is exclusively Host-owned, so
    # any ref with no journal is an orphan and is safely deleted by exact OID.
    journal_refs = {str(payload["preparedRef"]) for _path, payload in journals.values()}
    for ref_name, ref_oid in sorted(live_refs.items()):
        if ref_name in journal_refs:
            continue
        _run_host_git_checked(
            paths.root,
            ["update-ref", "-d", ref_name, ref_oid],
            execution_deadline=cleanup_deadline,
            error_message="failed to remove an orphaned Host prepared ref",
        )


def finalize_host_prepared_success(
    paths: Any,
    task_id: str,
    proof: IntegrationCommitProof,
) -> None:
    """Strictly persist native success only inside ``PreparedIntegration.seal``."""

    from .. import forge as native_forge

    cleanup_deadline = ExecutionDeadline.from_duration(10.0)
    if (
        _resolve_host_git_commit(
            paths.root,
            f"refs/heads/{proof.base_branch}",
            execution_deadline=cleanup_deadline,
        )
        != proof.merge_commit
        or _resolve_host_git_commit(
            paths.root,
            "HEAD",
            execution_deadline=cleanup_deadline,
        )
        != proof.merge_commit
    ):
        raise GitOpsError("sealed Host integration Git identity is not publishable")

    plan = native_forge.load_plan(paths)
    matching_tasks = [
        item for item in _plan_task_dicts(plan) if str(item.get("id") or "").strip() == task_id
    ]
    if len(matching_tasks) != 1 or str(matching_tasks[0].get("status") or "") != "prepared":
        raise GitOpsError("prepared native task status changed before Host seal")
    native_forge.set_task_status(plan, task_id, "done")
    native_forge.save_plan(paths, plan)
    persisted = native_forge.load_plan(paths)
    persisted_tasks = [
        item for item in _plan_task_dicts(persisted) if str(item.get("id") or "").strip() == task_id
    ]
    if (
        len(persisted_tasks) != 1
        or canonical_task_status(str(persisted_tasks[0].get("status") or ""))
        not in SUCCESSFUL_TERMINAL_STATUSES
    ):
        raise GitOpsError("native task success did not persist at Host seal")
    native_forge.set_current_run_status(
        paths,
        RUN_STATUS_COMPLETED,
        reason="authoritative Forge Host integration sealed",
        plan=persisted,
    )
    if native_forge.current_run_status(paths.root) != RUN_STATUS_COMPLETED:
        raise GitOpsError("native run success did not persist at Host seal")

    summary_path = paths.execution_dir / "sequential_summary.json"
    run_log_path = paths.execution_dir / "sequential_run.jsonl"
    for native_path, label in (
        (summary_path, "sequential summary"),
        (run_log_path, "sequential run log"),
    ):
        try:
            metadata = native_path.lstat()
        except OSError as exc:
            raise GitOpsError(f"prepared native {label} is unavailable at Host seal") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise GitOpsError(f"prepared native {label} changed type at Host seal")
        if metadata.st_size > 64 * 1024 * 1024:
            raise GitOpsError(f"prepared native {label} exceeds the Host byte limit")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GitOpsError("prepared native sequential summary is invalid") from exc
    executed = summary.get("executed") if isinstance(summary, dict) else None
    if (
        not isinstance(summary, dict)
        or summary.get("run_id") != paths.run_id
        or summary.get("status") != RUN_STATUS_RUNNING
        or summary.get("stopped_reason") != "prepared_for_commit"
        or summary.get("completion_ready") is not True
        or not isinstance(executed, list)
        or len(executed) != 1
        or not isinstance(executed[0], dict)
        or executed[0].get("task_id") != task_id
    ):
        raise GitOpsError("prepared native sequential summary authority changed at Host seal")
    sealed_at = datetime.now(timezone.utc).isoformat()
    sealed_task = dict(executed[0])
    sealed_task.update(
        {
            "status": "done",
            "success": True,
            "merge_result": "merged",
            "completion_consistent": True,
        }
    )
    summary.update(
        {
            "finished_at": sealed_at,
            "stopped_reason": "completed",
            "status": RUN_STATUS_COMPLETED,
            "success": True,
            "exit_code": 0,
            "timed_out": False,
            "executed": [sealed_task],
            "counts": {"executed": 1, "succeeded": 1, "failed": 0},
            "remaining": [],
            "completion_ready": False,
            "completion_consistent": True,
            "integration_commit_proof": {
                "base_branch": proof.base_branch,
                "approved_base_head": proof.approved_base_head,
                "task_commit": proof.task_commit,
                "merge_commit": proof.merge_commit,
                "integration_committed_elapsed_ms": (proof.integration_committed_elapsed_ms),
            },
        }
    )
    summary.pop("error", None)
    atomic_write_text(
        summary_path,
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
    )
    try:
        persisted_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GitOpsError("sealed native sequential summary could not be read back") from exc
    if persisted_summary != summary:
        raise GitOpsError("sealed native sequential summary did not persist exactly")

    try:
        existing_log = run_log_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GitOpsError("prepared native sequential run log could not be read") from exc
    sealed_record = {
        "ts": sealed_at,
        "event": "host_sealed",
        "run_id": paths.run_id,
        "task_id": task_id,
        "status": RUN_STATUS_COMPLETED,
        "base_branch": proof.base_branch,
        "approved_base_head": proof.approved_base_head,
        "task_commit": proof.task_commit,
        "merge_commit": proof.merge_commit,
        "integration_committed_elapsed_ms": proof.integration_committed_elapsed_ms,
    }
    sealed_line = json.dumps(sealed_record, ensure_ascii=True, sort_keys=True) + "\n"
    atomic_write_text(run_log_path, existing_log + sealed_line)
    try:
        if not run_log_path.read_text(encoding="utf-8").endswith(sealed_line):
            raise GitOpsError("sealed native sequential run log did not persist exactly")
    except (OSError, UnicodeError) as exc:
        raise GitOpsError("sealed native sequential run log could not be read back") from exc


def _host_scope_violations(
    paths: list[str],
    *,
    allowed_scope: list[str],
    root: Path,
) -> list[str]:
    """Return changed paths not matched by the immutable Host approval scope."""

    return [
        path
        for path in paths
        if not any(
            scope_path_matches_pattern(path, pattern, root=root) for pattern in allowed_scope
        )
    ]


def _sequential_conflict_report_lines(
    *,
    root: Path,
    task_id: str,
    base_branch: str | None,
    task_branch: str | None,
    review_path: Path | None,
) -> list[str]:
    """Describe an unresolved merge conflict and how to finish it.

    The sequential path stops at a conflict on purpose instead of starting the
    worktree-based resolver agent, so this report has to carry everything the
    next step needs: where the conflict is, how to land it by hand, and how to
    resume the run.
    """
    base = base_branch or "the base branch"
    branch = task_branch or "the task branch"
    workspace = os.fspath(root)
    lines = [
        f"Merge conflict: {branch} did not merge into {base}. The sequential run "
        "stopped here and started no resolver agent.",
    ]
    if review_path is not None:
        lines.append(f"Conflict review: {os.fspath(review_path)}")
    lines.append(
        f"Finish it by hand: `git -C {workspace} checkout {base}` then "
        f"`git -C {workspace} merge --no-ff {branch}`, resolve the conflicted "
        "files, and commit."
    )
    lines.append(f"Then continue the remaining tasks with `alysis forge run --path {workspace}`.")
    lines.append(
        "To let an agent attempt the resolution instead, re-run this task with "
        f"`alysis forge exec {task_id} --pr --auto-resolve-conflicts "
        f"--path {workspace}`."
    )
    return lines


# --- sequential run scheduling -------------------------------------------------
#
# `forge run` reuses the swarm scheduler's readiness rules (dependencies, retry
# eligibility, attempt limits) but never its batching: the next task is simply the
# first ready one, recomputed after each task so a task that just finished unblocks
# its dependents.

_NON_EXECUTABLE_TASK_STATUSES = frozenset({"superseded", "invalidated"})


def _parse_only_task_ids(only: str | None) -> set[str] | None:
    if not isinstance(only, str):
        return None
    ids = {part.strip() for part in only.split(",") if part.strip()}
    return ids or None


def _plan_task_dicts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def _sequential_candidates(
    plan: dict[str, Any],
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
    max_attempts: int | None,
    only_ids: set[str] | None,
    exclude_ids: set[str] | None = None,
) -> list[TaskCandidate]:
    runnable, _ready_for_merge, _skipped = select_task_candidates(
        tasks=_plan_task_dicts(plan),
        retry_failed=retry_failed,
        retry_changes_requested=retry_changes_requested,
        max_attempts=max_attempts,
        only_ids=only_ids,
    )
    excluded = exclude_ids or set()
    return [candidate for candidate in runnable if candidate.task_id not in excluded]


def _next_sequential_task(
    plan: dict[str, Any],
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
    max_attempts: int | None,
    only_ids: set[str] | None,
    exclude_ids: set[str] | None = None,
) -> TaskCandidate | None:
    """First task whose dependencies are satisfied, or None when none are ready.

    ``exclude_ids`` holds what this run already attempted. Without it a retried
    task that fails again would be re-selected forever, because failing is exactly
    the status ``--retry-failed`` asks the scheduler to accept.
    """
    candidates = _sequential_candidates(
        plan,
        retry_failed=retry_failed,
        retry_changes_requested=retry_changes_requested,
        max_attempts=max_attempts,
        only_ids=only_ids,
        exclude_ids=exclude_ids,
    )
    return candidates[0] if candidates else None


def _projected_sequential_order(
    plan: dict[str, Any],
    *,
    retry_failed: bool,
    retry_changes_requested: bool,
    max_attempts: int | None,
    only_ids: set[str] | None,
    max_tasks: int | None,
) -> list[str]:
    """The order tasks would run in, assuming each one succeeds.

    Dependencies only unblock as tasks finish, so the order has to be simulated
    rather than read off a single scheduling pass. Shallow task copies are enough:
    the simulation only rewrites ``status``.
    """
    tasks = [dict(task) for task in _plan_task_dicts(plan)]
    simulated = {"tasks": tasks}
    order: list[str] = []
    seen: set[str] = set()
    while max_tasks is None or len(order) < max_tasks:
        candidate = _next_sequential_task(
            simulated,
            retry_failed=retry_failed,
            retry_changes_requested=retry_changes_requested,
            max_attempts=max_attempts,
            only_ids=only_ids,
            exclude_ids=seen,
        )
        if candidate is None:
            break
        order.append(candidate.task_id)
        seen.add(candidate.task_id)
        for task in tasks:
            if str(task.get("id") or "").strip() == candidate.task_id:
                task["status"] = "done"
                break
    return order


def _unfinished_task_ids(plan: dict[str, Any]) -> list[str]:
    """Tasks that still represent outstanding work after the run."""
    unfinished: list[str] = []
    for task in _plan_task_dicts(plan):
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        if status in SUCCESSFUL_TERMINAL_STATUSES or status in _NON_EXECUTABLE_TASK_STATUSES:
            continue
        unfinished.append(task_id)
    return unfinished


def _bump_task_attempt(paths: Any, plan: dict[str, Any], task: dict[str, Any]) -> int:
    """Record that this run is about to try the task, so --max-attempts can bite."""
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
    """One appended line per lifecycle step of a sequential run.

    Per-task agent logs stay where they are; this is the run-level narrative, in
    order, in one file -- the thing a swarm run cannot have because its tasks
    interleave.
    """

    def __init__(self) -> None:
        self._path: Path | None = None

    def bind(self, paths: Any) -> None:
        ensure_execution_dirs(paths)
        self._path = paths.execution_dir / "sequential_run.jsonl"

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        record: dict[str, Any] = {"ts": now_iso(), "event": str(event)}
        record.update(fields)
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
        except OSError:
            # A run log that cannot be written must not take the run down with it.
            return


def _sequential_outcome_payload(outcome: TaskExecutionOutcome) -> dict[str, Any]:
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
        "branch": outcome.payload.get("branch"),
        "commit": outcome.payload.get("commit"),
        "merge_result": outcome.payload.get("merge_result"),
    }


def _write_sequential_summary(
    *,
    paths: Any,
    run_id: str,
    started_at: str,
    outcomes: list[TaskExecutionOutcome],
    stopped_reason: str,
    exit_code: int,
    pr: bool,
    verify_mode: str,
    scope_mode: str,
    remaining: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Persist the run outcome next to the swarm's, in the same execution dir."""
    executed = [_sequential_outcome_payload(item) for item in outcomes]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "command": "forge.run",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": now_iso(),
        "engine": "sequential",
        "worktrees_used": False,
        "parallel": 1,
        "git_flow": bool(pr),
        "verify": verify_mode,
        "scope": scope_mode,
        "stopped_reason": stopped_reason,
        "exit_code": int(exit_code),
        "executed": executed,
        "counts": {
            "executed": len(executed),
            "succeeded": sum(1 for item in executed if item["success"]),
            "failed": sum(1 for item in executed if not item["success"]),
        },
        "remaining": list(remaining or []),
    }
    if error:
        payload["error"] = error
    summary_path = paths.execution_dir / "sequential_summary.json"
    try:
        ensure_execution_dirs(paths)
        summary_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        payload["summary_path"] = os.fspath(summary_path)
    except OSError:
        # Reporting the run must not be able to fail the run.
        pass
    return payload


def _print_sequential_order(console: Any, plan: dict[str, Any], order: list[str]) -> None:
    table = _Table(title="Execution order")
    table.add_column("#")
    table.add_column("task")
    table.add_column("title")
    if not order:
        console.print("[yellow]No ready tasks to execute.[/yellow]")
        return
    titles = {
        str(task.get("id") or "").strip(): str(task.get("title") or "").strip()
        for task in _plan_task_dicts(plan)
    }
    for index, task_id in enumerate(order, start=1):
        table.add_row(str(index), task_id, titles.get(task_id, ""))
    console.print(table)


def _print_sequential_summary(console: Any, payload: dict[str, Any]) -> None:
    counts = payload.get("counts") or {}
    table = _Table(title="forge run")
    table.add_column("task")
    table.add_column("status")
    table.add_column("report")
    for item in payload.get("executed") or []:
        table.add_row(
            str(item.get("task_id") or ""),
            str(item.get("status") or ""),
            str(item.get("report") or ""),
        )
    console.print(table)
    console.print(
        f"Executed: {counts.get('executed', 0)} | "
        f"succeeded: {counts.get('succeeded', 0)} | "
        f"failed: {counts.get('failed', 0)} | "
        f"stopped: {payload.get('stopped_reason')}"
    )
    remaining = payload.get("remaining") or []
    if remaining:
        preview = ", ".join(str(item) for item in remaining[:10])
        if len(remaining) > 10:
            preview += ", ..."
        console.print(f"[yellow]Unfinished tasks:[/yellow] {preview}")
    # A stopped run leaves the checkout wherever the failing task left it, which is
    # the first thing you need to know before touching the repository again.
    if payload.get("stopped_reason") == "task_failed":
        failed = next(
            (item for item in reversed(payload.get("executed") or []) if not item.get("success")),
            None,
        )
        if failed is not None:
            branch = str(failed.get("branch") or "").strip()
            location = f" on branch {branch}" if branch else ""
            console.print(
                f"[yellow]Run stopped at {failed.get('task_id')}[/yellow]"
                f"{location}; nothing after it was started. "
                "Fix it and re-run `forge run` to continue, or pass --keep-going to skip "
                "past failures next time."
            )
    summary_path = payload.get("summary_path")
    if summary_path:
        console.print(f"Summary: {summary_path}")


def _exec_error_exit(
    console: Any,
    events: ForgeEventEmitter,
    message: str,
    *,
    task_id: str | None = None,
    kind: str = "forge_error",
) -> typer.Exit:
    """Report a `forge exec` command error on both surfaces and build the Exit."""
    console.print(f"[red]Forge error:[/red] {message}")
    events.error(
        message=str(message),
        kind=kind,
        exit_code=EXIT_ERROR,
        data={"task_id": task_id} if task_id else None,
    )
    return typer.Exit(code=EXIT_ERROR)


def _emit_verification_unavailable(
    events: ForgeEventEmitter,
    *,
    task_id: str,
    policy: str,
    reason: str,
    blocking: bool,
    outcome: str | None = None,
) -> None:
    """Report that verification did not run, and whether that blocked the task.

    ``outcome`` names the task status this produced, so a machine consumer can tell
    "kept, but nothing checked it" (``completed_unverified``) apart from the merely
    informational unavailable notices that leave the outcome untouched.
    """
    payload: dict[str, Any] = {
        "scope": "task",
        "task_id": task_id,
        "policy": policy,
        "reason": reason,
        "blocking": bool(blocking),
    }
    if outcome:
        payload["outcome"] = str(outcome)
    events.emit(EVENT_VERIFICATION_UNAVAILABLE, payload)


def _attempt_verification_repair(
    *,
    attempt: int,
    failing_result: Any,
    max_attempts: int,
    task_id: str,
    base_instruction: str,
    verify_artifact_path: Path,
    run_repair_agent: Any,
    run_verification: Any,
    root: Path,
    commit_message: str,
) -> RepairAttemptExecution:
    """Run one repair attempt: prompt with the failure, fix, commit, re-verify.

    Nothing here decides the task's outcome -- it reports what happened and lets
    :func:`run_verification_repair_loop` decide whether to keep going.
    """
    instruction = build_repair_instruction(
        base_instruction=base_instruction,
        task_id=task_id,
        attempt=attempt,
        max_attempts=max_attempts,
        verify_summary=str(getattr(failing_result, "summary", "")),
        excerpts=verification_failure_excerpts(failing_result),
        artifact_path=os.fspath(verify_artifact_path),
    )
    try:
        exit_code = int(run_repair_agent(instruction, attempt))
    except Exception as e:  # noqa: BLE001
        return RepairAttemptExecution(
            agent_exit_code=1,
            verify_result=None,
            error=f"repair agent raised: {sanitize_error_summary(str(e))}",
        )
    if exit_code != 0:
        # A crashed repair agent leaves the tree in an unknown state; re-verifying it
        # would attribute its mess to the task's own work.
        return RepairAttemptExecution(
            agent_exit_code=exit_code,
            verify_result=None,
            error=f"repair agent exited non-zero ({exit_code})",
        )

    committed = False
    try:
        commit_hash = _stage_and_commit_task_changes(root, message=commit_message)
        committed = commit_hash is not None
    except GitOpsError as e:
        return RepairAttemptExecution(
            agent_exit_code=exit_code,
            verify_result=None,
            error=f"could not commit the repair attempt: {e}",
        )
    if not committed:
        # The agent changed nothing, so re-running the same commands would produce
        # the same failure. Stop rather than burn the rest of the budget.
        return RepairAttemptExecution(
            agent_exit_code=exit_code,
            verify_result=None,
            error="the repair attempt made no repository changes",
        )

    changed_files: tuple[str, ...] = ()
    try:
        changed_files = tuple(changed_files_between(root, revspec="HEAD~1..HEAD"))
    except GitOpsError:
        changed_files = ()

    return RepairAttemptExecution(
        agent_exit_code=exit_code,
        verify_result=run_verification(),
        committed=True,
        changed_files=changed_files,
    )


def _stage_and_commit_task_changes(root: Path, *, message: str) -> str | None:
    """Stage the workspace and commit it, skipping protected and non-material paths.

    Returns the new commit hash, or ``None`` when there was nothing to commit. Used
    by verification repair attempts, which run after the task's own commit and must
    land their fix on the same branch before verification is re-run.
    """
    non_material_untracked_paths = list_untracked_packaging_metadata_paths(root)
    stage_all(root)
    unstage_staged_prefixes(root, [".alysis", ".alysis_images", "alysis-feedback"])
    ensure_not_staged_prefixes(root, [".alysis", ".alysis_images", "alysis-feedback"])
    if non_material_untracked_paths:
        unstage_staged_paths(root, non_material_untracked_paths)
        ensure_not_staged_paths(root, non_material_untracked_paths)
    staged_now = staged_files(root)
    if staged_now and has_grounded_rust_target_runtime_artifacts(root):
        unstage_staged_runtime_artifacts(root, current_paths=staged_now)
        staged_now = staged_files(root)
        ensure_not_staged_runtime_artifacts(root, current_paths=staged_now)
    if not staged_now:
        return None
    return commit_all(root, message=message)


def _plan_execution_readiness(plan: dict[str, Any]) -> str | None:
    """Return why the plan cannot be executed yet, or None when it is ready."""
    try:
        return _forge_no_execution_ready_tasks_message(plan)
    except Exception:  # noqa: BLE001
        # Readiness is reporting metadata; never let it break the command it describes.
        return None


def _sync_cli_globals(cli_mod: Any) -> None:
    module_globals = globals()
    if not _PROTECTED_GLOBAL_NAMES:
        for local_name, local_value in module_globals.items():
            if callable(local_value):
                _PROTECTED_GLOBAL_NAMES.add(local_name)
    for name, value in cli_mod.__dict__.items():
        if name.startswith("__") or name in _PROTECTED_GLOBAL_NAMES:
            continue
        module_globals[name] = value


def _host_executor_loaded_global_names(code: types.CodeType) -> set[str]:
    names = {
        str(instruction.argval)
        for instruction in dis.get_instructions(code)
        if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}
    }
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            names.update(_host_executor_loaded_global_names(constant))
    return names


def _missing_host_executor_globals() -> tuple[str, ...]:
    module_globals = globals()
    return tuple(
        sorted(
            name
            for name in _host_executor_loaded_global_names(execute_forge_task.__code__)
            if name not in module_globals and not hasattr(builtins, name)
        )
    )


def build_host_task_executor(cli_mod: Any) -> Callable[..., TaskExecutionOutcome]:
    """Bootstrap and return only the native result-returning task executor.

    The legacy implementation module still receives its execution primitives
    from the composed CLI surface.  Host code calls this narrow factory once;
    it never invokes a Typer command.  Reusing the same module object is
    idempotent, while attempting to rebind a live process to another module
    object fails closed.
    """

    global _HOST_TASK_EXECUTOR_BOOTSTRAP_MODULE
    with _HOST_TASK_EXECUTOR_BOOTSTRAP_LOCK:
        if (
            _HOST_TASK_EXECUTOR_BOOTSTRAP_MODULE is not None
            and _HOST_TASK_EXECUTOR_BOOTSTRAP_MODULE is not cli_mod
        ):
            raise RuntimeError(
                "Host task executor is already bound to a different CLI module object"
            )
        expected_module_name = "alysis_code.cli"
        if (
            getattr(cli_mod, "__name__", None) != expected_module_name
            or sys.modules.get(expected_module_name) is not cli_mod
        ):
            raise RuntimeError("Host task executor requires the canonical CLI module object")
        if _HOST_TASK_EXECUTOR_BOOTSTRAP_MODULE is None:
            _sync_cli_globals(cli_mod)
            missing = _missing_host_executor_globals()
            if missing:
                raise RuntimeError(
                    "Host task executor bootstrap left unresolved dependencies: "
                    + ", ".join(missing)
                )
            _HOST_TASK_EXECUTOR_BOOTSTRAP_MODULE = cli_mod
        else:
            missing = _missing_host_executor_globals()
            if missing:
                raise RuntimeError(
                    "Host task executor dependencies changed after bootstrap: " + ", ".join(missing)
                )
    return execute_forge_task


@dataclass(frozen=True, slots=True, repr=False)
class HostProviderRuntimeIdentity:
    """Secret-free provider route and credential authority for one Host run."""

    provider: str
    model: str
    profile_name: str
    protocol: str
    normalized_base_url: str
    auth_provider: str | None
    profile_api_key_env: str | None
    execution_backend: str
    execution_runtime: str | None
    connection_fingerprint: tuple[str, ...]
    extra_headers_digest: str
    credential_source: str
    credential_fingerprint: str | None
    account_fingerprint: str | None

    def __repr__(self) -> str:
        return "HostProviderRuntimeIdentity(<opaque>)"


def _host_credential_fingerprint(secret: str | None) -> str | None:
    if secret is None:
        return None
    return hashlib.sha256(
        b"alysis.forge-host-v2.credential\x00" + secret.encode("utf-8")
    ).hexdigest()


def derive_host_provider_runtime_identity(
    effective_config: Any,
    *,
    account_fingerprint: str | None,
    credential_value: str | None = None,
    credential_source: str | None = None,
) -> HostProviderRuntimeIdentity:
    """Derive the concrete, secret-free route identity used by execution.

    Root captures this value when it creates the opaque provider binding and
    passes that exact object back at execution.  The factory derives it again
    from a private config copy, so matching provider/model labels cannot hide a
    changed profile, endpoint, runtime, header set, credential source, or key.
    """

    if account_fingerprint is not None and (
        not isinstance(account_fingerprint, str)
        or not account_fingerprint
        or account_fingerprint != account_fingerprint.strip()
    ):
        raise RuntimeError("Host provider account fingerprint is invalid")
    try:
        cfg = copy.deepcopy(effective_config)
        profile = _host_get_active_profile(cfg)
        model = str(getattr(cfg, "model", "") or "").strip()
        profile_name = str(profile.name or "").strip()
        protocol = str(profile.protocol or "").strip()
        normalized_base_url = _host_resolve_effective_base_url(
            cfg=cfg,
            profile=profile,
        ).rstrip("/")
        parsed_base_url = urlsplit(normalized_base_url)
        if parsed_base_url.username is not None or parsed_base_url.password is not None:
            raise RuntimeError("Host provider route cannot contain URL user information")
        auth_provider = str(profile.auth_provider or "").strip() or None
        profile_api_key_env = str(profile.api_key_env or "").strip() or None
        provider = (
            str(
                _host_resolve_model_provider_key(
                    cfg=cfg,
                    model_name=model,
                    base_url=normalized_base_url,
                    profile_name=profile_name,
                )
                or ""
            )
            .strip()
            .casefold()
        )
        connection = tuple(str(item) for item in _host_connection_fingerprint(cfg))
        execution = getattr(cfg, "execution", None)
        execution_backend = str(getattr(execution, "backend", "native") or "native").strip()
        execution_runtime = str(getattr(execution, "runtime", None) or "").strip() or None
        extra_headers = dict(getattr(profile, "extra_headers", {}) or {})
        encoded_headers = json.dumps(
            dict(sorted(extra_headers.items())),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        extra_headers_digest = hashlib.sha256(encoded_headers).hexdigest()
        if auth_provider is not None:
            if credential_value is not None or credential_source is not None:
                raise RuntimeError(
                    "subscription Host provider identity cannot use an API-key override"
                )
            credential_source = "subscription:" + auth_provider
            credential_fingerprint = None
        else:
            if (credential_value is None) != (credential_source is None):
                raise RuntimeError(
                    "Host provider credential value and source must be supplied together"
                )
            if credential_value is None:
                credential = _host_resolve_api_key(cfg, profile_name=profile_name)
                resolved_credential = credential.key
                resolved_source = str(credential.source or "").strip()
            else:
                if not credential_value or not credential_source:
                    raise RuntimeError("Host provider explicit credential is invalid")
                resolved_credential = credential_value
                resolved_source = credential_source.strip()
                if resolved_source != credential_source:
                    raise RuntimeError("Host provider credential source is invalid")
            credential_source = resolved_source
            credential_fingerprint = _host_credential_fingerprint(resolved_credential)
    except Exception as exc:
        raise RuntimeError("Host provider runtime identity could not be resolved") from exc
    if not all((provider, model, profile_name, protocol, normalized_base_url, credential_source)):
        raise RuntimeError("Host provider runtime identity is incomplete")
    return HostProviderRuntimeIdentity(
        provider=provider,
        model=model,
        profile_name=profile_name,
        protocol=protocol,
        normalized_base_url=normalized_base_url,
        auth_provider=auth_provider,
        profile_api_key_env=profile_api_key_env,
        execution_backend=execution_backend,
        execution_runtime=execution_runtime,
        connection_fingerprint=connection,
        extra_headers_digest=extra_headers_digest,
        credential_source=credential_source,
        credential_fingerprint=credential_fingerprint,
        account_fingerprint=account_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class HostExecutionRuntimeBinding:
    """Concrete provider/config inputs bound to one Host execution request."""

    effective_config: Any = field(repr=False)
    console: Any
    provider_binding_ref: str
    provider: str
    model: str
    account_fingerprint: str | None
    admitted_identity: HostProviderRuntimeIdentity = field(repr=False)
    api_key_override: str | None = field(default=None, repr=False)
    base_environment: Mapping[str, str] | None = field(default=None, repr=False)


def build_forge_host_dependency_factory(
    cli_mod: Any,
    *,
    runtime_binding_for: Callable[[Any], HostExecutionRuntimeBinding],
) -> Callable[
    [Any, tuple[HostVerificationBinding, ...]],
    SequentialRunDependencies,
]:
    """Build the production Phase-0 dependencies without invoking Typer.

    The returned callable is import-neutral with respect to the Host service:
    it consumes the request structurally, pins the admitted provider/config,
    executes only the exact Host verifier under the shared deadline, and fixes
    every deferred Phase-0 capability off.
    """

    task_executor = build_host_task_executor(cli_mod)

    def dependency_factory(
        request: Any,
        bindings: tuple[HostVerificationBinding, ...],
    ) -> SequentialRunDependencies:
        runtime = runtime_binding_for(request)
        if not isinstance(runtime, HostExecutionRuntimeBinding):
            raise RuntimeError("Host execution runtime binding is invalid")
        if not callable(getattr(runtime.console, "print", None)):
            raise RuntimeError("Host execution console binding is invalid")
        # Take one private config snapshot up front.  Provider authority and the
        # later executor config must be derived from the same object, even if a
        # caller still holds a mutable AppConfig reference.
        runtime_config_snapshot = copy.deepcopy(runtime.effective_config)
        native_admission = request.native_admission
        provider_binding = native_admission.provider_binding
        admitted_provider = str(provider_binding.provider or "").strip().casefold()
        runtime_provider = str(runtime.provider or "").strip().casefold()
        if (
            runtime.provider_binding_ref != provider_binding.provider_binding_ref
            or runtime_provider != admitted_provider
            or runtime.model != provider_binding.model
            or runtime.account_fingerprint != provider_binding.account_fingerprint
            or not runtime.provider_binding_ref.strip()
            or not runtime_provider
            or not runtime.model.strip()
        ):
            raise RuntimeError("Host execution provider binding changed after admission")
        admitted_identity = runtime.admitted_identity
        if (
            not isinstance(admitted_identity, HostProviderRuntimeIdentity)
            or admitted_identity.provider != admitted_provider
            or admitted_identity.model != provider_binding.model
            or admitted_identity.account_fingerprint != provider_binding.account_fingerprint
        ):
            raise RuntimeError("Host execution provider identity contradicts admission")
        try:
            runtime_profile = _host_get_active_profile(runtime_config_snapshot)
        except Exception as exc:
            raise RuntimeError("Host execution provider profile is invalid") from exc
        if str(runtime_profile.auth_provider or "").strip():
            # Phase 0 has no immutable, generation-bound subscription session
            # handle yet.  A label/account fingerprint alone cannot prevent an
            # account swap between admission and client creation.
            raise RuntimeError("Host Phase 0 subscription provider execution is unavailable")
        if runtime.api_key_override is None:
            try:
                pinned_credential = _host_resolve_api_key(
                    runtime_config_snapshot,
                    profile_name=str(runtime_profile.name or "").strip(),
                )
                pinned_api_key = str(pinned_credential.key or "")
                credential_source = str(pinned_credential.source or "").strip()
            except Exception as exc:
                raise RuntimeError("Host execution credential could not be pinned") from exc
        else:
            pinned_api_key = runtime.api_key_override
            credential_source = admitted_identity.credential_source
        if not pinned_api_key or not credential_source:
            raise RuntimeError("Host execution credential could not be pinned")
        actual_identity = derive_host_provider_runtime_identity(
            runtime_config_snapshot,
            account_fingerprint=runtime.account_fingerprint,
            credential_value=pinned_api_key,
            credential_source=credential_source,
        )
        expected_credential = admitted_identity.credential_fingerprint
        actual_credential = actual_identity.credential_fingerprint
        credentials_match = expected_credential is None and actual_credential is None
        if expected_credential is not None and actual_credential is not None:
            credentials_match = hmac.compare_digest(expected_credential, actual_credential)
        actual_nonsecret_identity = replace(
            actual_identity,
            credential_fingerprint=None,
        )
        admitted_nonsecret_identity = replace(
            admitted_identity,
            credential_fingerprint=None,
        )
        if actual_nonsecret_identity != admitted_nonsecret_identity or not credentials_match:
            raise RuntimeError("Host execution provider route or account changed after admission")
        if request.native_paths != native_admission.paths:
            raise RuntimeError("Host execution paths changed after admission")
        if request.execution_deadline is None or not request.execution_deadline.enabled:
            raise RuntimeError("Host execution requires the shared bounded deadline")
        if not bindings or len({item.command_id for item in bindings}) != len(bindings):
            raise RuntimeError("Host execution verification bindings are invalid")

        admitted_commands = {
            command.command_id: command
            for command in request.plan_ready.verification_contract.commands
        }
        specs = {spec.command_id: spec for spec in native_admission.verification_specs}
        if len(admitted_commands) != len(request.plan_ready.verification_contract.commands) or len(
            specs
        ) != len(native_admission.verification_specs):
            raise RuntimeError("Host execution verification authority is duplicated")
        for binding in bindings:
            command = admitted_commands.get(binding.command_id)
            spec = specs.get(binding.command_id)
            if (
                command is None
                or spec is None
                or tuple(command.argv) != binding.argv
                or tuple(spec.argv) != binding.argv
                or str(spec.working_directory) != "."
                or binding.argv not in {("pytest",), ("pytest", "-q")}
            ):
                raise RuntimeError("Host execution verification authority drifted")

        artifact_paths = tuple(
            request.native_paths.execution_verify_dir
            / (
                "forge_host_v2_"
                + f"{position:02d}_"
                + _host_safe_task_file_component(binding.command_id)
                + ".json"
            )
            for position, binding in enumerate(bindings, start=1)
        )
        if len(set(artifact_paths)) != len(artifact_paths):
            raise RuntimeError("Host execution verification artifact paths collided")
        controlled_environment = (
            dict(runtime.base_environment) if runtime.base_environment is not None else None
        )

        def exact_verifier() -> tuple[HostVerificationResult, ...]:
            results: list[HostVerificationResult] = []
            for binding, artifact_path in zip(bindings, artifact_paths, strict=True):
                results.append(
                    run_host_verification(
                        binding.command_id,
                        admitted_command=admitted_commands[binding.command_id],
                        workspace_root=request.native_paths.root,
                        pinned_cwd=request.native_paths.root,
                        artifact_path=artifact_path,
                        execution_deadline=request.execution_deadline,
                        pytest_executable=request.pytest_executable,
                        base_environment=controlled_environment,
                    )
                )
            return tuple(results)

        effective = copy.deepcopy(runtime_config_snapshot)
        effective.model = runtime.model
        effective.assets.enabled = False
        max_steps = request.plan_ready.execution_admission.per_managed_task_max_steps
        events = ForgeEventEmitter(command="forge-host-v2", enabled=False)

        def execute_task(task_request: SequentialTaskRequest) -> TaskExecutionOutcome:
            if (
                task_request.native_run_id != request.native_run_id
                or task_request.native_paths != request.native_paths
                or task_request.execution_deadline is not request.execution_deadline
                or task_request.base_branch != request.base_branch
                or task_request.base_head != request.base_head
                or not task_request.phase0
                or not task_request.phase0_controls.is_phase0_safe()
            ):
                raise ForgeTaskExecutionError(
                    "Phase 0 task request contradicted Host execution authority",
                    kind="phase0_policy",
                    task_id=task_request.task_id,
                )
            run_cfg = copy.deepcopy(effective)
            run_cfg.model = runtime.model
            run_cfg.max_steps = max_steps
            run_cfg.assets.enabled = False
            return task_executor(
                console=runtime.console,
                events=events,
                paths=task_request.native_paths,
                plan=task_request.plan,
                task=task_request.task,
                task_id=task_request.task_id,
                effective=effective,
                run_cfg=run_cfg,
                effective_mode="auto",
                scope_mode="strict",
                verify_mode="strict",
                verify_commands=[shlex.join(item.argv) for item in bindings],
                verify_command_source="forge_host_v2_admitted",
                verify_repair_budget=0,
                # Always pass the credential pinned by this dependency-factory
                # invocation.  The agent/client must never resolve ambient or
                # stored credentials a second time after Host admission.
                api_key_override=pinned_api_key,
                yes=True,
                no_log=False,
                max_steps_provided=True,
                pr=True,
                review=False,
                base_branch=request.base_branch,
                keep_branch=True,
                auto_resolve_conflicts=False,
                execution_deadline=request.execution_deadline,
                strict_verification_executor=exact_verifier,
                strict_verification_bindings=bindings,
                strict_verification_artifact_paths=artifact_paths,
                strict_base_head=request.base_head,
                strict_per_task_max_steps=max_steps,
                strict_disable_remotes=True,
            )

        return SequentialRunDependencies(
            load_plan=lambda paths: load_plan(paths),
            save_plan=lambda paths, plan: save_plan(paths, plan),
            ensure_execution_dirs=lambda paths: ensure_execution_dirs(paths),
            acquire_mutation_guard=lambda paths: acquire_swarm_mutation_guard(
                paths,
                mode="forge_host_v2",
            ),
            # ForgeHostEngineRunner replaces this with its one-shot guarded
            # admission reload before the engine can execute a task.
            reconcile_and_validate=lambda _paths, plan: plan,
            execute_task=execute_task,
            mark_run_status=lambda paths, status, reason, plan: _mark_run_status(
                paths,
                status,
                reason=reason,
                plan=plan,
                mode="forge_host_v2" if status == RUN_STATUS_RUNNING else None,
            ),
            mark_interrupted_if_running=lambda paths, reason: (
                _mark_run_interrupted_if_still_running(paths, reason=reason)
            ),
            now_iso=lambda: datetime.now(timezone.utc).isoformat(),
            recover_unsealed_integrations=recover_host_integration_transactions,
            finalize_prepared_success=finalize_host_prepared_success,
        )

    return dependency_factory


def _path_binding_source(ctx: Any = None, path: Path | None = None) -> str:
    current_ctx = ctx if ctx is not None else get_current_context(silent=True)
    path_source = current_ctx.get_parameter_source("path") if current_ctx is not None else None
    if path_source is not None and path_source is not ParameterSource.DEFAULT:
        return "explicit_path"
    if path is None and current_ctx is not None:
        raw_path = current_ctx.params.get("path")
        path = Path(raw_path) if raw_path is not None else None
    if path is None:
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        raw_path = caller.f_locals.get("path") if caller is not None else None
        path = Path(raw_path) if raw_path is not None else None
    if path is not None and Path(path) != Path("."):
        return "explicit_path"

    if path_source is None or path_source is ParameterSource.DEFAULT:
        return "cwd"
    return "explicit_path"


def _mark_run_status(
    paths: Any,
    status: str,
    *,
    reason: str = "",
    plan: dict[str, Any] | None = None,
    mode: str | None = None,
) -> None:
    """Record a run lifecycle transition on the current-run pointer.

    Best-effort by design: a pointer that cannot be written is a stale status line
    in a UI, never a reason to fail the execution it describes.
    """
    from ..forge import set_current_run_status
    from ..run_state import RUN_STATUS_RUNNING, build_run_owner

    owner = None
    if status == RUN_STATUS_RUNNING:
        owner = build_run_owner(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            mode=mode or "forge",
        )
    try:
        set_current_run_status(paths, status, reason=reason, owner=owner, plan=plan)
    except Exception:  # noqa: BLE001 - see docstring
        return


def _mark_run_interrupted_if_still_running(paths: Any, *, reason: str) -> None:
    """Close out a run whose process is leaving without having recorded an outcome.

    Every normal exit path stamps a terminal status first, so reaching here means
    an exception, a ``KeyboardInterrupt``, or a code path that forgot -- all of
    which leave resumable work behind, which is what ``interrupted`` means.
    """
    from ..forge import current_run_status
    from ..run_state import RUN_STATUS_INTERRUPTED, RUN_STATUS_RUNNING

    try:
        if current_run_status(paths.root) != RUN_STATUS_RUNNING:
            return
    except Exception:  # noqa: BLE001
        return
    _mark_run_status(paths, RUN_STATUS_INTERRUPTED, reason=reason)


def _terminal_run_status_for(*, plan: dict[str, Any], any_failure: bool) -> tuple[str, str]:
    """Map an execution's result onto the run lifecycle enum.

    Unfinished tasks left by ``--only`` or ``--max-tasks`` are a scope the caller
    chose, not lost work, so they land back on ``approved`` (executable, idle)
    rather than ``interrupted`` (something went wrong).
    """
    from ..run_state import RUN_STATUS_APPROVED, RUN_STATUS_COMPLETED

    if any_failure:
        return RUN_STATUS_FAILED, "execution finished with at least one task not accepted"
    remaining = _unfinished_task_ids(plan)
    if remaining:
        return (
            RUN_STATUS_APPROVED,
            f"execution finished; {len(remaining)} task(s) still unexecuted",
        )
    return RUN_STATUS_COMPLETED, "every plan task reached a successful terminal status"


def _missing_swarm_run_error(*, binding: WorkspaceBinding) -> ForgeError:
    requested = os.fspath(binding.requested_path)
    return ForgeError(
        "No current forge run was found for this workspace. "
        f"Start with `alysis forge plan --path {requested}` or enter Forge "
        "from chat after starting alysis inside a project folder."
    )


def _print_forge_lock_wait_notice(console: Any, info: dict[str, Any]) -> None:
    diagnostic = str(info.get("diagnostic") or "").strip()
    console.print(
        "[yellow]Forge execution queued:[/yellow] another execution is mutating this workspace; waiting for it to finish."
    )
    if diagnostic:
        console.print(f"[dim]{diagnostic}[/dim]")


def _render_planner_reply(
    *, console: Any, message: str, questions: list[str] | None = None
) -> None:
    console.print("[bold]Planner:[/bold]")
    console.print(message)
    if questions:
        console.print("[dim]Planner questions[/dim]")
        for question in questions:
            console.print(f"- {question}")


def _merge_changed_files(*path_groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in path_groups:
        for raw in group:
            value = str(raw).strip().replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


def _runtime_snapshot_changed_files(
    before_snapshot: dict[str, str],
    after_snapshot: dict[str, str],
) -> list[str]:
    changed: list[str] = []
    for path in sorted(set(before_snapshot) | set(after_snapshot)):
        if before_snapshot.get(path) != after_snapshot.get(path):
            changed.append(_normalize_changed_file_path(path))
    return [path for path in changed if path]


def _normalize_changed_file_path(path: str) -> str:
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _authorized_custom_tool_runtime_side_effects(
    *,
    sessions_dir: Path,
    session_id: str,
) -> set[str]:
    session_log = sessions_dir / f"{session_id}.jsonl"
    if not session_log.exists():
        return set()
    authorized: set[str] = set()
    try:
        lines = session_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "tool_result":
            continue
        payload = event.get("payload")
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or result.get("success") is not True:
            continue
        side_effects = result.get("side_effects")
        if not isinstance(side_effects, dict):
            continue
        writes = side_effects.get("workspace_writes")
        if not isinstance(writes, list):
            continue
        for item in writes:
            if not isinstance(item, dict):
                continue
            if str(item.get("scope") or "") != "tool_dir":
                continue
            rel_path = _normalize_changed_file_path(str(item.get("path") or ""))
            if rel_path == ".alysis/tools" or rel_path.startswith(".alysis/tools/"):
                authorized.add(rel_path)
    return authorized


def _path_matches_authorized_runtime_prefix(path: str, prefixes: set[str]) -> bool:
    normalized = _normalize_changed_file_path(path)
    return any(
        normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    )


def _drop_parent_directory_placeholders(paths: list[str]) -> list[str]:
    concrete_paths = [path for path in paths if path and not path.endswith("/")]
    filtered: list[str] = []
    for path in paths:
        if path.endswith("/") and any(other.startswith(path) for other in concrete_paths):
            continue
        filtered.append(path)
    return filtered


def _task_declares_explicit_write_scope(task: dict[str, Any]) -> bool:
    raw = task.get("write_scope")
    if isinstance(raw, list):
        return any(str(item or "").strip() for item in raw)
    if isinstance(raw, str):
        return bool(raw.strip())
    return False


def _task_is_analysis_only(task: dict[str, Any]) -> bool:
    raw_flag = task.get("analysis_only")
    if isinstance(raw_flag, bool):
        return raw_flag
    return is_clearly_non_mutating_task(
        title=str(task.get("title") or "").strip(),
        description=str(task.get("description") or "").strip(),
        acceptance_criteria=[
            str(item or "")
            for item in (task.get("acceptance_criteria") or [])
            if str(item or "").strip()
        ],
    )


def _task_string_list(task: dict[str, Any], key: str) -> list[str]:
    raw = task.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if not isinstance(raw, (list, tuple)):
        text = str(raw).strip()
        return [text] if text else []
    return [str(item).strip() for item in raw if str(item).strip()]


def _empty_forge_exec_task_asset_mirror(
    workspace_path: Path,
    *,
    task_id: str = "",
) -> TaskAssetMirror:
    workspace = workspace_path.resolve()
    return TaskAssetMirror(
        workspace_path=workspace,
        manifest_path=workspace / ".alysis" / "task_assets" / "manifest.json",
        primary=[],
        may_need=[],
        pinned=[],
        task_id=task_id,
    )


def _combined_forge_exec_image_paths(
    *,
    legacy_paths: list[str],
    mirror: TaskAssetMirror,
    allocation: TaskAssetAllocation | None,
    cfg: Any,
    role_model: str,
    model_registry: ModelRegistry,
    usage_logger: AssetUsageLogger,
) -> list[str] | None:
    combined: list[str] = []
    seen: set[str] = set()

    def _append(path: str) -> None:
        try:
            normalized = os.fspath(Path(path).resolve())
        except OSError:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        combined.append(normalized)

    for path in legacy_paths:
        _append(path)
    if cfg.assets.worker.inline_images and model_registry.get(role_model).supports_vision:
        decision_by_id = {
            decision.asset_id: decision.mode
            for decision in (allocation.decisions if allocation else [])
        }
        max_new = max(0, int(cfg.assets.worker.max_inline_images))
        added_new = 0
        for entry in mirror.primary:
            if entry.kind != "image" or entry.status != "mirrored":
                continue
            if decision_by_id.get(entry.asset_id) not in {"full_inline", "focused_extract"}:
                continue
            if entry.raw_workspace_path is None or not entry.raw_workspace_path.exists():
                continue
            before = len(combined)
            _append(os.fspath(entry.raw_workspace_path))
            if len(combined) > before:
                usage_logger.inline_injection(asset_id=entry.asset_id, kind=entry.kind)
                added_new += 1
                if added_new >= max_new:
                    break
    max_total = max(0, int(cfg.assets.worker.max_inline_images))
    if max_total > 0:
        combined = combined[:max_total]
    return combined or None


def _append_patch_debug_section(patch_path: Path, *, title: str, patch_text: str) -> None:
    existing = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
    parts: list[str] = []
    if existing:
        parts.append(existing if existing.endswith("\n") else existing + "\n")
    parts.append(f"# {title}\n")
    if patch_text:
        parts.append(patch_text if patch_text.endswith("\n") else patch_text + "\n")
    patch_path.write_text("\n".join(part.rstrip("\n") for part in parts if part), encoding="utf-8")


def forge_plan(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    create_path: bool = typer.Option(
        False,
        "--create-path",
        help="Create --path if it does not exist before binding the workspace.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of choosing a narrower project folder.",
    ),
    cli_ctx: Any = None,
    events: Any = None,
) -> None:
    events = _events_or_null(events)
    console = _console()
    try:
        binding = _resolve_startup_workspace_binding(
            requested_path=path,
            console=console,
            interactive=not _is_non_interactive_terminal(),
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            source=_path_binding_source(cli_ctx),
            action=WorkspaceAction.FORGE_PLAN,
        )
        paths = create_plan_run(
            path,
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            workspace_binding=binding,
        )
        events.set_run_id(paths.run_id)
        plan = load_plan(paths)
        workspace_scan = ensure_workspace_context_artifacts(paths)
    except (ForgeError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(message=str(e), kind="forge_error", exit_code=EXIT_ERROR)
        raise typer.Exit(code=EXIT_ERROR) from e

    console.rule("[bold cyan]forge plan[/bold cyan]")
    console.print(f"Run ID: {paths.run_id}")
    console.print(f"Plan directory: {paths.plan_dir}")
    for line in format_workspace_context_summary_lines(workspace_scan):
        console.print(line)
    console.print("Planning loop started. Type /help for commands. Type /done to finish.")
    assistant_enabled = False
    planning_suggested: set[str] = set()
    planner_state = _ForgePlannerSessionState(
        workspace_context=(
            _workspace_context_payload_for_paths(paths=paths)
            or {
                **workspace_scan.to_dict(),
                "greenfield": bool(getattr(paths, "greenfield", False)),
            }
        )
    )

    def _emit_planner_meta(message: str) -> None:
        console.print(message)

    def _emit_planner_warning_group(label: str, warnings: list[str]) -> None:
        for warning in warnings:
            console.print(f"[yellow]{label}:[/yellow] {warning}")

    while True:
        try:
            # In machine mode the prompt goes to stderr so stdout stays pure NDJSON;
            # planning input is still read from stdin exactly as before.
            line = typer.prompt("plan", err=events.enabled)
        except (EOFError, KeyboardInterrupt):
            console.print("")
            break
        text = line.strip()
        if not text:
            continue

        append_transcript_note(paths, role="user", message=text)
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"/done", "done", "/exit", "exit", "/quit", "quit"}:
            append_transcript_note(paths, role="system", message="Planning loop finished.")
            break
        if cmd in {"/help", "help"}:
            console.print(_planning_help_panel())
            append_transcript_note(paths, role="system", message="Displayed planning help.")
            continue
        if cmd == "/assistant":
            assistant_cmd = arg.lower()
            if not assistant_cmd:
                assistant_cmd, picker_available = _select_forge_assistant_interactive(
                    enabled=assistant_enabled,
                    console=console,
                )
                if not picker_available:
                    console.print("[yellow]Usage:[/yellow] /assistant on|off|status")
                    append_transcript_note(
                        paths,
                        role="system",
                        message="Rejected invalid /assistant usage.",
                    )
                    continue
                if assistant_cmd is None:
                    continue
            if assistant_cmd == "on":
                assistant_enabled = True
                console.print("Planner assistant: ON")
                append_transcript_note(paths, role="system", message="Planner assistant enabled.")
                continue
            if assistant_cmd == "off":
                assistant_enabled = False
                _set_forge_planner_follow_up_state(
                    planner_state=planner_state,
                    questions=[],
                    awaiting_clarification=False,
                )
                console.print("Planner assistant: OFF")
                append_transcript_note(paths, role="system", message="Planner assistant disabled.")
                continue
            if assistant_cmd == "status":
                state = "ON" if assistant_enabled else "OFF"
                console.print(f"Planner assistant: {state}")
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Planner assistant status requested ({state}).",
                )
                continue

            console.print("[yellow]Usage:[/yellow] /assistant on|off|status")
            append_transcript_note(
                paths,
                role="system",
                message="Rejected invalid /assistant usage.",
            )
            continue

        if assistant_enabled:
            _run_forge_planner_turn_controller(
                console=console,
                paths=paths,
                plan=plan,
                planner_state=planner_state,
                user_text=text,
                cfg_loader=load_config,
                unavailable_message_builder=(
                    lambda error: (
                        "Planner assistant is unavailable because config could not be loaded: "
                        f"{error}"
                    )
                ),
                emit_meta=_emit_planner_meta,
                emit_warning_group=_emit_planner_warning_group,
                api_key_override=None,
                render_reply=lambda message, questions: _render_planner_reply(
                    console=console,
                    message=message,
                    questions=questions,
                ),
                selection_label="planner",
                planning_relevant=True,
            )
            continue

        if cmd == "/goal":
            if not arg:
                console.print("[yellow]Usage:[/yellow] /goal <text>")
                append_transcript_note(paths, role="system", message="Rejected empty /goal.")
                continue
            goal = arg
            plan["project_goal"] = goal
            if not str(plan.get("summary") or "").strip():
                plan["summary"] = goal
            save_plan(paths, plan)
            console.print("Project goal updated.")
            if "goal" not in planning_suggested:
                planning_suggested.add("goal")
                console.print(
                    "[dim]Next: add tasks with /task <title>, or describe the work and "
                    "let the planner draft them.[/dim]"
                )
            append_transcript_note(paths, role="system", message="Updated project goal.")
            continue
        if cmd == "/task":
            if not arg:
                console.print("[yellow]Usage:[/yellow] /task <title>")
                append_transcript_note(paths, role="system", message="Rejected empty /task.")
                continue
            title = arg
            try:
                task = add_task(
                    plan,
                    title=title,
                    description=f"Manual planning chat task: {title}",
                )
            except ForgeError as e:
                console.print(f"[yellow]Task rejected:[/yellow] {e}")
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Rejected /task because it lacked runnable file scope: {e}",
                )
                continue
            save_plan(paths, plan)
            console.print(f"Added task: {task['id']} - {task['title']}")
            if "task" not in planning_suggested:
                planning_suggested.add("task")
                console.print(
                    "[dim]Next: add more tasks, or /done to save and validate the plan.[/dim]"
                )
            append_transcript_note(paths, role="system", message=f"Added task {task['id']}.")
            continue

        add_requirement(plan, text)
        save_plan(paths, plan)
        console.print("Captured requirement note.")
        append_transcript_note(paths, role="system", message="Captured requirement note.")

    finalize_plan(plan)
    reconciliation_result, _ = _reconcile_plan_for_paths(
        paths=paths,
        plan=plan,
        refresh_if_stale=True,
        transcript_tail=planner_state.transcript,
    )
    save_plan(paths, plan)
    validation_warnings = _validate_forge_plan_for_paths(paths, plan)
    if reconciliation_result.warnings:
        console.print("[yellow]Plan reconciliation warnings:[/yellow]")
        for warning in reconciliation_result.warnings:
            console.print(f"- {warning}")
            append_transcript_note(
                paths,
                role="system",
                message=f"Plan reconciliation warning: {warning}",
            )
    _write_plan_validation_artifact(
        paths=paths,
        reconciliation_result=reconciliation_result,
        validation_warnings=validation_warnings,
    )
    if validation_warnings:
        console.print("[yellow]Plan validation warnings:[/yellow]")
        for warning in validation_warnings:
            console.print(f"- {warning}")
            append_transcript_note(
                paths,
                role="system",
                message=f"Plan validation warning: {warning}",
            )

    # Stamp the plan with an explicit status so a plan that only earned warnings
    # is stored as a draft rather than looking ready until exec rejects it.
    status_assessment = apply_plan_status(plan, validation_warnings=validation_warnings)
    save_plan(paths, plan)
    if status_assessment.status == PLAN_STATUS_DRAFT:
        console.print("[yellow]Plan status:[/yellow] draft (not execution-ready)")
        for reason in status_assessment.blocking_reasons[:5]:
            console.print(f"- {reason}")
        append_transcript_note(
            paths,
            role="system",
            message="Plan saved as draft: " + "; ".join(status_assessment.blocking_reasons[:5]),
        )
    else:
        console.print("Plan status: execution_ready")

    console.print(f"Plan saved: {paths.plan_md_path}")
    console.print(f"Structured plan: {paths.plan_json_path}")

    tasks = plan.get("tasks") or []
    not_ready_reason = _plan_execution_readiness(plan)
    repair_payload = plan_repair_event_payload(plan)
    plan_saved_payload = {
        "run_id": paths.run_id,
        "plan_md": os.fspath(paths.plan_md_path),
        "plan_json": os.fspath(paths.plan_json_path),
        "run_dir": os.fspath(paths.run_dir),
        "project_goal": str(plan.get("project_goal") or "").strip() or None,
        "task_count": len(tasks),
        "task_ids": [str(task.get("id", "")) for task in tasks],
        "execution_ready": not_ready_reason is None,
        "warnings": list(validation_warnings),
        "reconciliation_warnings": list(reconciliation_result.warnings),
        **repair_payload,
    }
    events.emit(EVENT_PLAN_SAVED, plan_saved_payload)
    if not_ready_reason is not None:
        events.emit(
            EVENT_PLAN_INVALID,
            {
                "reason": not_ready_reason,
                "source": "execution_readiness",
                "warnings": list(validation_warnings),
                **repair_payload,
            },
        )
    events.run_completed(ok=True, exit_code=EXIT_OK, data={"plan": plan_saved_payload})


_SEQUENTIAL_DELEGATE_ENV = "ALYSIS_SWARM_SEQUENTIAL_DELEGATE"


def _swarm_sequential_delegation_blocker(
    *,
    cfg: Any,
    parallel: int,
    dry_run: Any,
    keep_worktrees: Any,
    retry_merge_conflicts: Any,
    replan: str | None,
    integration_verify: str | None,
    integration_verify_cmd: list[str] | None,
) -> str | None:
    """Why ``--parallel 1`` must stay on the swarm engine, or None to delegate.

    Delegation is only safe when nothing swarm-specific was requested. Every
    blocker below names a capability the sequential engine genuinely does not
    have, so the answer is always "keep the swarm", never "quietly drop it".
    """
    if not isinstance(parallel, int) or parallel != 1:
        return "parallel > 1"
    if str(env_get(_SEQUENTIAL_DELEGATE_ENV, "")).strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return f"{_SEQUENTIAL_DELEGATE_ENV} disables delegation"
    if dry_run is True:
        return "--dry-run reports the swarm schedule"
    if keep_worktrees is True:
        return "--keep-worktrees is worktree-specific"
    if retry_merge_conflicts is True:
        return "--retry-merge-conflicts re-merges existing task branches"
    if integration_verify is not None or integration_verify_cmd:
        return "an explicit integration verification gate was requested"
    try:
        replanning_mode = resolve_replanning_mode(cfg=cfg, replanning_mode=replan)
    except Exception:  # noqa: BLE001
        # Failing to classify replanning must not decide the engine; keep the
        # swarm, which is what the caller literally typed.
        return "between-batch replanning mode could not be resolved"
    if str(replanning_mode) != "off":
        return f"between-batch replanning is {replanning_mode}"
    return None


def _load_swarm_summary_payload(paths: Any) -> dict[str, Any] | None:
    """Read the structured swarm outcome the orchestrator already writes."""
    summary_path = paths.execution_dir / "swarm_summary.json"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _emit_swarm_outcome_events(
    *,
    events: ForgeEventEmitter,
    paths: Any,
    exit_code: int,
    dry_run: bool,
) -> None:
    """Translate the swarm's own outcome artifact into protocol events."""
    if not events.enabled:
        return

    summary = _load_swarm_summary_payload(paths)
    data: dict[str, Any] = {"dry_run": bool(dry_run)}
    if summary is not None:
        verification_status = str(summary.get("verification_status") or "")
        integration = summary.get("integration")
        integration_payload = integration if isinstance(integration, dict) else {}
        if verification_status == "not_run":
            events.emit(
                EVENT_VERIFICATION_UNAVAILABLE,
                {
                    "scope": "swarm",
                    "status": verification_status,
                    "reason": "no integration verification ran for this swarm run",
                },
            )
        elif verification_status:
            events.emit(
                EVENT_VERIFICATION_RESULT,
                {
                    "scope": "swarm",
                    "status": verification_status,
                    "passed": verification_status == "passed",
                    "integration": integration_payload,
                },
            )
        data["outcome"] = summary
    elif not dry_run:
        events.emit(
            EVENT_VERIFICATION_UNAVAILABLE,
            {
                "scope": "swarm",
                "status": "unknown",
                "reason": "swarm summary artifact was not written",
            },
        )

    events.run_completed(ok=exit_code == EXIT_OK, exit_code=exit_code, data=data)


def forge_swarm(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of requiring a narrower project path.",
    ),
    parallel: int = typer.Option(2, "--parallel", min=1, help="Parallel workers per batch."),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch (defaults to current checked out branch).",
    ),
    max_tasks: int | None = typer.Option(
        None,
        "--max-tasks",
        min=1,
        help="Maximum number of tasks to execute in this swarm run.",
    ),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        min=1,
        help="Maximum attempts allowed per task before scheduler skips it.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print schedule and exit."),
    keep_worktrees: bool = typer.Option(
        False,
        "--keep-worktrees",
        help=(
            "Keep failed task worktrees and branches for debugging; default cleanup removes "
            "successful worktrees and rejected failed branch state."
        ),
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Include tasks currently marked failed.",
    ),
    retry_changes_requested: bool = typer.Option(
        False,
        "--retry-changes-requested",
        help="Include tasks currently marked changes_requested.",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated task ids to execute (still enforces dependencies).",
    ),
    retry_merge_conflicts: bool = typer.Option(
        False,
        "--retry-merge-conflicts",
        help="Retry tasks marked merge_conflict during merge phase.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Verification policy: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    integration_verify: str | None = typer.Option(
        None,
        "--integration-verify",
        help="Batch integration verification policy: off, warn, or strict (defaults to config: warn).",
    ),
    integration_verify_cmd: list[str] | None = typer.Option(
        None,
        "--integration-verify-cmd",
        help="Override integration verify command for this swarm run (repeatable).",
    ),
    replan: str | None = typer.Option(
        None,
        "--replan",
        help="Between-batch replanning mode: off, suggest, or apply.",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run automated PR review gate before merging task branches.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    cli_ctx: Any = None,
    events: Any = None,
) -> None:
    events = _events_or_null(events)
    console = _console()
    cfg = load_config()

    # A swarm of one is a sequential run wearing worktrees. Hand it to the sequential
    # engine so the blessed path is what actually executes, unless the invocation asked
    # for machinery only the swarm has.
    delegation_blocker = _swarm_sequential_delegation_blocker(
        cfg=cfg,
        parallel=parallel,
        dry_run=dry_run,
        keep_worktrees=keep_worktrees,
        retry_merge_conflicts=retry_merge_conflicts,
        replan=replan,
        integration_verify=integration_verify,
        integration_verify_cmd=integration_verify_cmd,
    )
    if isinstance(parallel, int) and parallel == 1:
        if delegation_blocker is None:
            console.print(
                "[dim]forge swarm --parallel 1 has no parallelism to exploit; running the "
                "sequential engine (`forge run`) in this checkout instead of provisioning "
                "worktrees.[/dim]"
            )
            return forge_run(
                path=path,
                allow_broad_workspace=allow_broad_workspace,
                only=only,
                max_tasks=max_tasks,
                max_attempts=max_attempts,
                retry_failed=retry_failed,
                retry_changes_requested=retry_changes_requested,
                keep_going=False,
                dry_run=False,
                scope=scope,
                verify=verify,
                verify_cmd=verify_cmd,
                verify_repair_attempts=None,
                pr=True,
                review=review,
                base_branch=base_branch,
                keep_branch=False,
                auto_resolve_conflicts=False,
                mode=mode,
                model=model,
                base_url=base_url,
                temperature=temperature,
                stream=stream,
                max_steps=max_steps,
                no_log=no_log,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
                api_key=api_key,
                yes=yes,
                cli_ctx=cli_ctx,
                events=events,
            )
        console.print(
            f"[dim]forge swarm --parallel 1 kept the swarm engine: {delegation_blocker}. "
            "Use `forge run` for the sequential path.[/dim]"
        )

    effective = clone_cfg(cfg)
    current_ctx = get_current_context(silent=True)
    max_steps_source = (
        current_ctx.get_parameter_source("max_steps") if current_ctx is not None else None
    )
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        max_steps_provided = (
            max_steps_source is not None and max_steps_source is not ParameterSource.DEFAULT
        )
    if base_url is not None:
        apply_runtime_base_url_override(effective, base_url)
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    if stream is not None:
        effective.stream = stream
    if max_steps is not None:
        effective.max_steps = max_steps
    swarm_max_steps = effective.max_steps if max_steps_provided else None

    effective_mode = (mode.value if mode else effective.default_mode) or "review"
    scope_mode = "strict"
    verify_mode = "warn"
    integration_verify_mode = None
    replanning_mode = None

    try:
        scope_mode = _normalize_scope_mode(scope)
        verify_mode = _normalize_verify_mode(verify)
        integration_verify_mode = integration_verify
        replanning_mode = replan
        api_key_override = _resolve_api_key_override(
            api_key=api_key,
            api_key_env=api_key_env,
            api_key_stdin=api_key_stdin,
        )
        binding = resolve_workspace_binding(
            path,
            create_if_missing=False,
            allow_broad_workspace=allow_broad_workspace,
            source=_path_binding_source(cli_ctx),
        )
        ensure_workspace_policy(
            binding,
            action=WorkspaceAction.SWARM,
            allow_broad_workspace=allow_broad_workspace,
        )
        try:
            paths = load_current_run_paths(binding.workspace_context.focus_path)
        except ForgeError as e:
            if "current_run.json" in str(e):
                raise _missing_swarm_run_error(binding=binding) from e
            raise
        events.set_run_id(paths.run_id)
        plan = load_plan(paths)
        run_mutation_guard = acquire_swarm_mutation_guard(
            paths,
            mode="forge_swarm:cli",
            on_wait=lambda info: _print_forge_lock_wait_notice(console, info),
        )
        try:
            if bool(getattr(run_mutation_guard, "acquired_after_wait", False)):
                plan = load_plan(paths)
            reconciliation_result, _ = _reconcile_plan_for_paths(
                paths=paths,
                plan=plan,
                refresh_if_stale=True,
            )
            if reconciliation_result.changed:
                save_plan(paths, plan)
            if reconciliation_result.warnings:
                console.print("[yellow]Plan reconciliation warnings:[/yellow]")
                for warning in reconciliation_result.warnings:
                    console.print(f"- {warning}")
            validation_warnings = _validate_forge_plan_for_paths(paths, plan)
            _write_plan_validation_artifact(
                paths=paths,
                reconciliation_result=reconciliation_result,
                validation_warnings=validation_warnings,
            )
            repair_payload = plan_repair_event_payload(plan)
            no_execution_ready_tasks_message = _forge_no_execution_ready_tasks_message(plan)
            if no_execution_ready_tasks_message is not None:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": no_execution_ready_tasks_message,
                        "source": "execution_readiness",
                        "warnings": list(validation_warnings),
                        **repair_payload,
                    },
                )
                raise ForgeError(no_execution_ready_tasks_message)
            try:
                raise_for_execution_ready_plan(
                    plan,
                    retry_failed=retry_failed,
                    retry_changes_requested=retry_changes_requested,
                    retry_merge_conflicts=retry_merge_conflicts,
                    only=only,
                )
            except PlannerFailedError as e:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": str(e),
                        "source": "plan_validation",
                        "failure_category": getattr(e, "failure_category", None),
                        **repair_payload,
                    },
                )
                err = ForgeError(str(e))
                err.failure_category = e.failure_category  # type: ignore[attr-defined]
                raise err from e
            resolve_model_for_role(
                cfg=effective,
                role=ROLE_CODING,
                plan=plan,
                prefer_context="forge",
            )
            if not dry_run:
                _mark_run_status(
                    paths,
                    RUN_STATUS_RUNNING,
                    reason=f"forge swarm started (parallel={parallel})",
                    plan=plan,
                    mode="forge_swarm:cli",
                )
            code = run_swarm(
                paths=paths,
                plan=plan,
                cfg=effective,
                mode=effective_mode,
                yes=yes,
                max_steps=swarm_max_steps,
                api_key_override=api_key_override,
                no_log=no_log,
                parallel=parallel,
                base_branch=base_branch,
                max_tasks=max_tasks,
                max_attempts=max_attempts,
                dry_run=dry_run,
                keep_worktrees=keep_worktrees,
                retry_failed=retry_failed,
                retry_changes_requested=retry_changes_requested,
                only=only,
                retry_merge_conflicts=retry_merge_conflicts,
                scope_mode=scope_mode,
                verify_mode=verify_mode,
                verify_cmd=verify_cmd,
                integration_mode=integration_verify_mode,
                integration_verify_cmd=integration_verify_cmd,
                replanning_mode=replanning_mode,
                review=review,
                console=console,
                workspace_binding=binding,
                run_mutation_guard=run_mutation_guard,
            )
            if not dry_run:
                # The swarm reloads and rewrites the plan as it merges batches, so the
                # in-memory copy is stale by now; the terminal status has to be decided
                # from what actually landed on disk.
                terminal_status, terminal_reason = _terminal_run_status_for(
                    plan=load_plan(paths),
                    any_failure=code != EXIT_OK,
                )
                _mark_run_status(paths, terminal_status, reason=terminal_reason)
        finally:
            _mark_run_interrupted_if_still_running(
                paths,
                reason="forge swarm exited without recording an outcome",
            )
            run_mutation_guard.release()
    except (ConfigError, ForgeError, GitOpsError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e),
            kind="forge_error",
            exit_code=EXIT_ERROR,
            data={"failure_category": getattr(e, "failure_category", None)},
        )
        raise typer.Exit(code=EXIT_ERROR) from e
    except Exception as e:  # noqa: BLE001
        # An unexpected exception is a command error, not "the swarm ran and some tasks
        # failed" -- exit 2 keeps exit 1 meaning only the latter.
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e) or e.__class__.__name__,
            kind="exception",
            exit_code=EXIT_ERROR,
            data={"exception_type": e.__class__.__name__},
        )
        raise typer.Exit(code=EXIT_ERROR) from e

    log_paths = sorted(paths.execution_logs_dir.glob("*.jsonl"))
    _print_usage_summary_from_logs(
        console=console,
        title="Swarm Usage Summary",
        log_paths=log_paths,
    )
    _emit_swarm_outcome_events(events=events, paths=paths, exit_code=code, dry_run=dry_run)
    raise typer.Exit(code=code)


def execute_forge_task(
    *,
    console: Any,
    events: ForgeEventEmitter,
    paths: Any,
    plan: dict[str, Any],
    task: dict[str, Any],
    task_id: str,
    effective: Any,
    run_cfg: Any,
    effective_mode: str,
    scope_mode: str,
    verify_mode: str,
    verify_commands: list[str],
    verify_command_source: str | None,
    verify_repair_budget: int,
    api_key_override: str | None,
    yes: bool,
    no_log: bool,
    max_steps_provided: bool,
    pr: bool,
    review: bool,
    base_branch: str | None,
    keep_branch: bool,
    auto_resolve_conflicts: bool = False,
    execution_deadline: ExecutionDeadline | None = None,
    strict_verification_executor: (Callable[[], tuple[HostVerificationResult, ...]] | None) = None,
    strict_verification_bindings: tuple[HostVerificationBinding, ...] = (),
    strict_verification_artifact_paths: tuple[Path, ...] = (),
    strict_base_head: str | None = None,
    strict_per_task_max_steps: int | None = None,
    strict_disable_remotes: bool = False,
) -> TaskExecutionOutcome:
    """Execute exactly one plan task in the main checkout, with no worktrees.

    This is the sequential execution core. ``forge exec`` calls it once and turns
    the outcome into an exit code; ``forge run`` calls it per ready task in
    dependency order. Sharing it is the point: scope triage, verification, the
    repair loop, the review gate and the PR flow behave identically either way,
    so a fix to one is a fix to both.

    The caller owns the run mutation guard, the plan reload and the terminal
    event. Raising :class:`ForgeTaskExecutionError` means the task could not be
    executed at all (a command error); returning an outcome with
    ``success=False`` means it ran and was not accepted.
    """
    task_monotonic_started = (
        execution_deadline.elapsed_seconds() if execution_deadline is not None else time.monotonic()
    )
    pr_base_branch: str | None = None
    pr_task_branch: str | None = None
    commit_hash: str | None = None
    merge_commit_hash: str | None = None
    merge_first_parent: str | None = None
    merge_second_parent: str | None = None
    base_ref_after_merge: str | None = None
    integration_commit_proof: IntegrationCommitProof | None = None
    prepared_integration: PreparedIntegration | None = None
    merge_result: str | None = None
    allowed_scope = normalize_scope_patterns(task, root=paths.root) if scope_mode != "off" else []
    scope_warnings: list[str] = []
    review_blocked = False
    verify_blocked = False
    # Set when the work landed but strict verification had no authoritative
    # command to hold it against: a completion, not a failure, and never merged.
    verification_unavailable_completion = False
    verification_repair_payload: dict[str, Any] | None = None
    verify_summary: str | None = None
    verify_payload: dict[str, Any] | None = None
    verify_result: Any | None = None
    structured_verification: tuple[HostVerificationResult, ...] = ()
    merge_conflict_detected = False
    conflict_review_path: Path | None = None
    verify_path = paths.execution_verify_dir / f"{_safe_task_file_component(task_id)}.txt"
    remote_settings = load_remote_settings_from_env({} if strict_disable_remotes else None)
    remote_record: dict[str, Any] | None = None
    conflict_auto_settings = load_conflict_auto_resolve_settings(cfg=effective)
    strict_expected_artifact_paths: tuple[Path, ...] = ()
    strict_ignored_workspace_paths: tuple[str, ...] = ()

    if strict_verification_executor is None and (
        strict_verification_bindings or strict_verification_artifact_paths
    ):
        raise ForgeTaskExecutionError(
            "exact verification authority requires the strict verification executor",
            kind="phase0_policy",
            task_id=task_id,
        )
    if strict_verification_executor is not None:
        phase0_policy_violations: list[str] = []
        if not strict_verification_bindings:
            phase0_policy_violations.append("exact verification bindings are required")
        elif len({item.command_id for item in strict_verification_bindings}) != len(
            strict_verification_bindings
        ):
            phase0_policy_violations.append("exact verification bindings must be unique")
        elif any(
            not item.command_id.strip() or item.argv not in {("pytest",), ("pytest", "-q")}
            for item in strict_verification_bindings
        ):
            phase0_policy_violations.append("exact verification bindings are not admitted")
        verification_root = paths.execution_verify_dir.resolve(strict=False)
        canonical_artifact_paths: list[Path] = []
        if len(strict_verification_artifact_paths) != len(strict_verification_bindings):
            phase0_policy_violations.append(
                "one predeclared retained artifact path is required per verification command"
            )
        else:
            for raw_artifact_path in strict_verification_artifact_paths:
                candidate = Path(raw_artifact_path)
                canonical = candidate.resolve(strict=False)
                if (
                    not candidate.is_absolute()
                    or canonical.parent != verification_root
                    or candidate.is_symlink()
                    or (candidate.exists() and not candidate.is_file())
                ):
                    phase0_policy_violations.append(
                        "exact verification artifact paths must be direct regular files "
                        "under the execution verification directory"
                    )
                    break
                canonical_artifact_paths.append(canonical)
            if len(set(canonical_artifact_paths)) != len(canonical_artifact_paths):
                phase0_policy_violations.append("exact verification artifact paths must be unique")
        strict_expected_artifact_paths = tuple(canonical_artifact_paths)
        expected_verify_commands = [shlex.join(item.argv) for item in strict_verification_bindings]
        if verify_commands != expected_verify_commands:
            phase0_policy_violations.append(
                "prompt verification commands must match exact bindings"
            )
        if verify_command_source != "forge_host_v2_admitted":
            phase0_policy_violations.append("verification command source is not Host admission")
        if execution_deadline is None or not execution_deadline.enabled:
            phase0_policy_violations.append("a bounded execution deadline is required")
        if (
            strict_base_head is None
            or len(strict_base_head) not in {40, 64}
            or strict_base_head != strict_base_head.lower()
            or any(character not in "0123456789abcdef" for character in strict_base_head)
        ):
            phase0_policy_violations.append("a pinned base HEAD is required")
        if (
            not isinstance(strict_per_task_max_steps, int)
            or isinstance(strict_per_task_max_steps, bool)
            or strict_per_task_max_steps < 1
        ):
            phase0_policy_violations.append("an admitted per-task max_steps is required")
        if not max_steps_provided:
            phase0_policy_violations.append("max_steps must be explicitly provided")
        if no_log:
            phase0_policy_violations.append("session logging must remain enabled")
        if not pr:
            phase0_policy_violations.append("git flow must be enabled")
        if verify_mode != "strict":
            phase0_policy_violations.append("verification must be strict")
        if verify_repair_budget != 0:
            phase0_policy_violations.append("verification repair must be disabled")
        if review:
            phase0_policy_violations.append("review must be disabled")
        if auto_resolve_conflicts:
            phase0_policy_violations.append("conflict auto-resolution must be disabled")
        if not strict_disable_remotes:
            phase0_policy_violations.append("remote operations must be fixed off")
        if remote_settings.enabled:
            phase0_policy_violations.append("remote operations must be disabled")
        if bool(getattr(getattr(effective, "assets", None), "enabled", False)) or bool(
            getattr(getattr(run_cfg, "assets", None), "enabled", False)
        ):
            phase0_policy_violations.append("assets must be disabled")
        raw_mcp_scope = task.get("mcp_scope")
        if isinstance(raw_mcp_scope, (list, tuple, dict, set)) and raw_mcp_scope:
            phase0_policy_violations.append("task MCP scope must be empty")
        elif isinstance(raw_mcp_scope, str) and raw_mcp_scope.strip():
            phase0_policy_violations.append("task MCP scope must be empty")
        if phase0_policy_violations:
            raise ForgeTaskExecutionError(
                "Phase 0 execution policy rejected: " + "; ".join(phase0_policy_violations),
                kind="phase0_policy",
                task_id=task_id,
            )

        try:
            preexisting_ignored = _host_nonruntime_ignored_paths(
                paths.root,
                execution_deadline=execution_deadline,
            )
        except GitOpsError as exc:
            raise ForgeTaskExecutionError(
                "Phase 0 ignored-workspace preflight failed",
                kind="phase0_policy",
                task_id=task_id,
            ) from exc
        if preexisting_ignored:
            preview = ", ".join(preexisting_ignored[:20])
            if len(preexisting_ignored) > 20:
                preview += ", ..."
            raise ForgeTaskExecutionError(
                "Phase 0 rejects preexisting ignored workspace material: " + preview,
                kind="phase0_policy",
                task_id=task_id,
            )

        task_attempts_raw = task.get("attempts")
        try:
            strict_attempt_count = (
                max(0, int(task_attempts_raw if task_attempts_raw is not None else 0)) + 1
            )
        except (TypeError, ValueError):
            strict_attempt_count = 1
        strict_preflight_step_budget = _resolve_managed_task_step_budget(
            cfg=run_cfg,
            plan=plan,
            task=task,
            kind="managed_task",
            mode=effective_mode,
            verification_enabled=True,
            max_steps_override=strict_per_task_max_steps,
            attempt_count=strict_attempt_count,
            image_count=0,
        )
        if strict_preflight_step_budget.resolved_max_steps != strict_per_task_max_steps:
            raise ForgeTaskExecutionError(
                "Phase 0 execution policy rejected: resolved max_steps does not match admission",
                kind="phase0_policy",
                task_id=task_id,
            )

    if pr:
        try:
            ensure_git_available()
            ensure_git_repo(paths.root)
            ensure_clean_for_pr(paths.root)
            pr_base_branch = base_branch.strip() if base_branch else current_branch(paths.root)
            if not pr_base_branch:
                raise GitOpsError("base branch is empty")
            pr_task_branch = str(task.get("branch") or "").strip()
            if not pr_task_branch:
                pr_task_branch = generate_task_branch_name(
                    task_id,
                    str(task.get("title") or ""),
                )
                task["branch"] = pr_task_branch
                save_plan(paths, plan)
            if strict_verification_executor is not None:
                assert strict_base_head is not None
                _checkout_new_host_task_branch(
                    paths.root,
                    branch=pr_task_branch,
                    base_branch=pr_base_branch,
                    base_head=strict_base_head,
                    execution_deadline=execution_deadline,
                )
            else:
                checkout_branch(paths.root, pr_task_branch, base_branch=pr_base_branch)
            merge_result = "not merged"
        except GitOpsError as e:
            raise ForgeTaskExecutionError(
                str(e),
                kind="git_error",
                task_id=task_id,
            ) from e

    ensure_execution_dirs(paths)
    set_task_status(plan, task_id, "in_progress")
    save_plan(paths, plan)
    events.emit(
        EVENT_TASK_STARTED,
        {
            "task_id": task_id,
            "title": str(task.get("title", "")),
            "status": "in_progress",
            "source": "exec",
            "pr": bool(pr),
            "branch": pr_task_branch,
        },
    )

    started_at = now_iso()
    run_non_interactive = _is_non_interactive_terminal()
    # Host v2 runs the admitted argv once at the exact post-commit/pre-merge
    # gate.  Exposing the legacy in-agent ``verify_run`` path would duplicate
    # execution under a different sandbox/profile and can manufacture a false
    # repair loop when that optional sandbox is unavailable.  The outer exact
    # verifier remains strict and is the sole Phase-0 verification authority.
    prompt_verification_enabled = (
        strict_verification_executor is None and verify_mode != "off" and bool(verify_commands)
    )
    prepared_knowledge = _prepare_task_execution_knowledge(
        run_paths=paths,
        task=task,
        selection_label="execution",
    )
    runtime_session_id = _safe_task_file_component(task_id)
    task_mcp_scope, task_mcp_scope_warnings = normalize_task_mcp_scope(
        task.get("mcp_scope"),
        warning_prefix=f"Task {task_id}",
    )
    scope_warnings.extend(task_mcp_scope_warnings)
    instruction = ""
    task_image_paths: list[str] | None = None
    budget_artifact_path = paths.execution_budgets_dir / f"{runtime_session_id}.json"
    runtime_sessions_dir = _execution_private_sessions_dir(
        cfg=run_cfg,
        run_id=paths.run_id,
        task_id=task_id,
        workspace_root=paths.root,
    )
    _cleanup_execution_private_sessions_dir(runtime_sessions_dir)
    task_attempts_raw = task.get("attempts")
    try:
        task_attempt_count = (
            max(0, int(task_attempts_raw if task_attempts_raw is not None else 0)) + 1
        )
    except (TypeError, ValueError):
        task_attempt_count = 1
    task_step_budget = _resolve_managed_task_step_budget(
        cfg=run_cfg,
        plan=plan,
        task=task,
        kind="managed_task",
        mode=effective_mode,
        verification_enabled=verify_mode != "off",
        max_steps_override=(
            strict_per_task_max_steps
            if strict_verification_executor is not None
            else (effective.max_steps if max_steps_provided else None)
        ),
        attempt_count=task_attempt_count,
        image_count=0,
    )
    head_before_run = head_commit(paths.root) if paths.has_head_commit else None
    before_runtime_snapshot: dict[str, str] | None = None
    reporting_baseline: Any | None = None
    recording_surface = RecordingSurface(_make_rich_surface(console=console))
    asset_setup_error: str | None = None
    asset_setup_warnings: list[str] = []
    asset_usage_logger = AssetUsageLogger(run_paths=paths, task_id=task_id)
    asset_model_registry = ModelRegistry(cfg=run_cfg)
    asset_surface = (
        build_asset_surface(
            cfg=run_cfg,
            run_paths=paths,
            model_registry=asset_model_registry,
        )
        if run_cfg.assets.enabled
        else None
    )
    task_asset_mirror = _empty_forge_exec_task_asset_mirror(paths.root, task_id=task_id)
    if asset_surface is not None:
        try:
            task_asset_mirror = mirror_task_assets(
                task=task,
                plan=plan,
                surface=asset_surface,
                workspace_path=paths.root,
            )
        except AssetError as exc:
            if run_cfg.assets.worker.fail_on_mirror_error:
                asset_setup_error = (
                    f"forge exec asset mirror failed: {sanitize_error_summary(str(exc))}"
                )
            else:
                asset_setup_warnings.append(
                    f"forge exec asset mirror skipped: {sanitize_optional_error_summary(str(exc))}"
                )
    scope_warnings.extend(asset_setup_warnings)
    for entry in [
        *task_asset_mirror.primary,
        *task_asset_mirror.may_need,
        *task_asset_mirror.pinned,
    ]:
        asset_usage_logger.mirror(
            asset_id=entry.asset_id,
            kind=entry.kind,
            status=entry.status,
        )
    has_mirrored_task_assets = bool(
        task_asset_mirror.primary or task_asset_mirror.may_need or task_asset_mirror.pinned
    )

    run_code = 1
    run_err: str | None = asset_setup_error
    task_mcp_manager: Any | None = None
    asset_allocation: TaskAssetAllocation | None = None
    try:
        if strict_verification_executor is None:
            task_mcp_manager = _build_forge_task_scoped_mcp_manager(
                workspace_root=paths.root,
                session_id=runtime_session_id,
                task_scope=task_mcp_scope,
            )
            mcp_context_section = _build_forge_mcp_execution_context_section(
                task_scope=task_mcp_scope,
                mcp_manager=task_mcp_manager,
            )
        else:
            mcp_context_section = ""
        instruction_bundle = _build_forge_exec_instruction_bundle(
            plan=plan,
            task=task,
            root=paths.root,
            cfg=run_cfg,
            role_model=run_cfg.model,
            mode=effective_mode,
            yes=yes,
            deny_write_prefixes=[".alysis"],
            allow_write_globs=allowed_scope if scope_mode == "strict" else None,
            non_interactive=run_non_interactive,
            verification_enabled=prompt_verification_enabled,
            authoritative_verification_commands=(
                verify_commands if prompt_verification_enabled else None
            ),
            api_key=api_key_override,
            subagents_enabled=False,
            leading_sections=[prepared_knowledge.prompt_section, mcp_context_section],
        )
        relevant_assets_section = ""
        if asset_surface is not None and task_asset_mirror.primary:
            asset_allocation = allocate_task_assets(
                task=task,
                plan=plan,
                mirror=task_asset_mirror,
                cfg=run_cfg,
                model_registry=asset_model_registry,
                instruction_token_budget=instruction_bundle.budget.final_instruction_budget,
                api_key=api_key_override,
            )
            for decision in asset_allocation.decisions:
                asset_usage_logger.allocation_decision(
                    asset_id=decision.asset_id,
                    mode=decision.mode,
                )
            relevant_assets_section = render_relevant_assets_section(
                mirror=task_asset_mirror,
                allocation=asset_allocation,
                cfg=run_cfg,
                surface=asset_surface,
                model_registry=asset_model_registry,
                api_key=api_key_override,
            )
        elif asset_surface is not None and (task_asset_mirror.may_need or task_asset_mirror.pinned):
            asset_allocation = TaskAssetAllocation(
                task_id=task_id,
                decisions=[],
                elapsed_ms=0,
                model=None,
                tokens_used={},
                fallback_used=False,
                fallback_reason=None,
            )
            relevant_assets_section = render_relevant_assets_section(
                mirror=task_asset_mirror,
                allocation=asset_allocation,
                cfg=run_cfg,
                surface=asset_surface,
                model_registry=asset_model_registry,
                api_key=api_key_override,
            )
        if relevant_assets_section:
            instruction_bundle = _build_forge_exec_instruction_bundle(
                plan=plan,
                task=task,
                root=paths.root,
                cfg=run_cfg,
                role_model=run_cfg.model,
                mode=effective_mode,
                yes=yes,
                deny_write_prefixes=[".alysis"],
                allow_write_globs=allowed_scope if scope_mode == "strict" else None,
                non_interactive=run_non_interactive,
                verification_enabled=prompt_verification_enabled,
                authoritative_verification_commands=(
                    verify_commands if prompt_verification_enabled else None
                ),
                api_key=api_key_override,
                subagents_enabled=False,
                leading_sections=[prepared_knowledge.prompt_section, mcp_context_section],
                relevant_assets_section=relevant_assets_section,
            )
        instruction = instruction_bundle.instruction
        _write_execution_context_artifact(
            paths=paths,
            task_id=task_id,
            context_text=instruction_bundle.artifact_text,
        )
        task_image_paths = _combined_forge_exec_image_paths(
            legacy_paths=list(instruction_bundle.image_paths),
            mirror=task_asset_mirror,
            allocation=asset_allocation,
            cfg=run_cfg,
            role_model=run_cfg.model,
            model_registry=asset_model_registry,
            usage_logger=asset_usage_logger,
        )
        task_step_budget = _resolve_managed_task_step_budget(
            cfg=run_cfg,
            plan=plan,
            task=task,
            kind="managed_task",
            mode=effective_mode,
            verification_enabled=verify_mode != "off",
            max_steps_override=(
                strict_per_task_max_steps
                if strict_verification_executor is not None
                else (effective.max_steps if max_steps_provided else None)
            ),
            attempt_count=task_attempt_count,
            image_count=len(task_image_paths or []),
        )
        if (
            strict_verification_executor is not None
            and task_step_budget.resolved_max_steps != strict_per_task_max_steps
        ):
            raise ForgeTaskExecutionError(
                "Phase 0 execution policy rejected: runtime max_steps drifted from admission",
                kind="phase0_policy",
                task_id=task_id,
            )
        budget_artifact_payload = instruction_bundle.to_budget_artifact_payload()
        budget_artifact_payload["step_budget"] = task_step_budget.to_payload()
        budget_artifact_path = _write_execution_budget_artifact(
            paths=paths,
            task_id=task_id,
            payload=budget_artifact_payload,
        )
        before_runtime_snapshot = _snapshot_runtime_tree(paths.root)
        reporting_baseline = _capture_task_local_workspace_baseline(
            paths.root,
            before_commit=head_before_run,
        )
        if run_err is None:
            if asset_surface is not None and has_mirrored_task_assets:
                task_mcp_manager = compose_worker_asset_mcp_manager(
                    base_manager=task_mcp_manager,
                    asset_manager=build_worker_asset_mcp_manager(
                        cfg=run_cfg,
                        surface=asset_surface,
                        model_registry=asset_model_registry,
                        mirror=task_asset_mirror,
                        usage_logger=asset_usage_logger,
                        api_key=api_key_override,
                    ),
                )
            agent_instruction = instruction
            agent_ephemeral_user_messages: list[str] | None = None
            if strict_verification_executor is not None:
                # Keep runner-generated metadata in the user role while
                # preventing it from becoming semantic acceptance input.  The
                # turn machinery inserts ephemeral user context immediately
                # before the persistent instruction, so the model sees the
                # complete pack first and the admitted task prose last.
                agent_instruction = _host_authored_task_instruction(task)
                agent_ephemeral_user_messages = [instruction]
            run_code = run_agent(
                cfg=run_cfg,
                root=paths.root,
                instruction=agent_instruction,
                acceptance_instruction=build_task_acceptance_instruction(task),
                mode=effective_mode,
                runtime_kind=RuntimeKind.FORGE_EXEC,
                yes=yes,
                max_steps=task_step_budget.resolved_max_steps,
                no_log=no_log,
                api_key_override=api_key_override,
                console=console,
                surface=recording_surface,
                image_paths=task_image_paths,
                deny_write_prefixes=[".alysis"],
                allow_write_globs=allowed_scope if scope_mode == "strict" else None,
                non_interactive=run_non_interactive,
                session_log_dir_override=runtime_sessions_dir,
                session_id_override=runtime_session_id,
                usage_role=f"forge_exec:{task_id}",
                enable_compaction=False,
                enable_tool_output_offload=True,
                enable_conversation_summarization=True,
                compaction_profile="execution",
                ephemeral_user_messages=agent_ephemeral_user_messages,
                enable_chat_turn_step_budget=False,
                one_shot_execution=True,
                verification_enabled=prompt_verification_enabled,
                authoritative_verification_commands=(
                    verify_commands if prompt_verification_enabled else None
                ),
                subagents_enabled=False,
                enforce_explicit_subagent_requests=False,
                mcp_manager=task_mcp_manager,
                execution_deadline=execution_deadline,
                session_source_metadata={
                    "surface": "forge_exec",
                    "run_id": paths.run_id,
                    "task_id": str(task_id),
                },
            )
    except Exception as e:  # noqa: BLE001
        run_code = 1
        run_err = str(e)
        if not instruction:
            _write_execution_context_artifact(
                paths=paths,
                task_id=task_id,
                context_text=(
                    "# Task Context Pack\n\n"
                    "Task execution setup failed before the agent started.\n\n"
                    f"- Error: {run_err}\n"
                ),
            )
        if not budget_artifact_path.exists():
            _write_execution_budget_artifact(
                paths=paths,
                task_id=task_id,
                payload={
                    "error": run_err,
                    "step_budget": task_step_budget.to_payload(),
                },
            )
        if before_runtime_snapshot is None:
            before_runtime_snapshot = _snapshot_runtime_tree(paths.root)
        if reporting_baseline is None:
            reporting_baseline = _capture_task_local_workspace_baseline(
                paths.root,
                before_commit=head_before_run,
            )
    finally:
        if task_mcp_manager is not None:
            task_mcp_manager.close()

    assert before_runtime_snapshot is not None
    assert reporting_baseline is not None
    after_runtime_snapshot = _snapshot_runtime_tree(paths.root)
    runtime_artifact_changes = _runtime_snapshot_changed_files(
        before_runtime_snapshot,
        after_runtime_snapshot,
    )
    authorized_runtime_side_effects = _authorized_custom_tool_runtime_side_effects(
        sessions_dir=runtime_sessions_dir,
        session_id=runtime_session_id,
    )
    runtime_artifact_changes = [
        path for path in runtime_artifact_changes if path not in authorized_runtime_side_effects
    ]
    runtime_artifacts_changed = bool(runtime_artifact_changes)
    if asset_allocation is not None:
        write_task_asset_allocation(
            run_paths=paths,
            allocation=asset_allocation,
            started_at=started_at,
        )
    asset_usage_logger.summary(
        primary_count=len(task_asset_mirror.primary),
        may_need_count=len(task_asset_mirror.may_need),
        pinned_count=len(task_asset_mirror.pinned),
    )
    try:
        exec_artifacts = _write_exec_log_artifacts(
            paths=paths,
            task_id=task_id,
            cfg=run_cfg,
            no_log=no_log,
            before_logs=None,
            sessions_dir=runtime_sessions_dir,
            expected_session_id=runtime_session_id,
        )
    finally:
        _cleanup_execution_private_sessions_dir(runtime_sessions_dir)
    execution_retained_log_paths = (
        (exec_artifacts.log_copy_path,)
        if exec_artifacts.log_retained and exec_artifacts.log_copy_path.is_file()
        else ()
    )
    execution_usage = usage_from_session_logs(execution_retained_log_paths)
    strict_log_binding = (
        bind_retained_artifact(exec_artifacts.log_copy_path)
        if strict_verification_executor is not None and execution_retained_log_paths
        else None
    )

    safe_task_component = _safe_task_file_component(task_id)
    patch_path = paths.execution_patches_dir / f"{safe_task_component}.diff"
    scratch_artifact_dir = paths.execution_dir / "scratch" / safe_task_component
    scratch_artifact_dir.mkdir(parents=True, exist_ok=True)
    success = run_code == 0 and not runtime_artifacts_changed
    if strict_verification_executor is not None:
        try:
            strict_ignored_workspace_paths = _host_nonruntime_ignored_paths(
                paths.root,
                execution_deadline=execution_deadline,
            )
        except GitOpsError as exc:
            success = False
            run_err = (run_err + "; " if run_err else "") + sanitize_error_summary(str(exc))
        if strict_ignored_workspace_paths:
            success = False
            preview = ", ".join(strict_ignored_workspace_paths[:20])
            if len(strict_ignored_workspace_paths) > 20:
                preview += ", ..."
            run_err = (run_err + "; " if run_err else "") + (
                "ignored workspace mutation is forbidden in Phase 0: " + preview
            )
    pr_report_state_upgraded = False
    head_after_run = head_commit(paths.root) if paths.has_head_commit else None
    # Captured before cleanup so scope triage can tell a file the task created from an
    # existing neighbour it merely edited.
    baseline_workspace_paths = set(reporting_baseline.before_snapshot)
    try:
        report_diff = _build_task_local_workspace_reporting_diff(
            paths.root,
            baseline=reporting_baseline,
            after_commit=head_after_run,
        )
    finally:
        _cleanup_task_local_workspace_baseline(reporting_baseline)
    patch_path.write_text(report_diff.patch_text, encoding="utf-8")
    scratch_scope_diagnostics = relocate_known_scratch_artifacts(
        root=paths.root,
        artifact_dir=scratch_artifact_dir,
    )
    relocated_scratch_paths = {item.path for item in scratch_scope_diagnostics}
    changed_files = list(report_diff.changed_files)
    if relocated_scratch_paths:
        changed_files = [path for path in changed_files if path not in relocated_scratch_paths]
    agent_added_non_material_paths: list[str] = []
    if head_before_run and head_after_run and head_after_run != head_before_run:
        agent_added_non_material_paths = [
            path
            for path in added_files_since(
                paths.root,
                before_commit=head_before_run,
                after_commit=head_after_run,
            )
            if is_non_material_untracked_path(path)
        ]
        if agent_added_non_material_paths:
            changed_files = [
                path for path in changed_files if path not in agent_added_non_material_paths
            ]
    pr_material_changed_files = (
        _drop_parent_directory_placeholders(
            _merge_changed_files(
                list(changed_files),
                list_changed_files_including_untracked(paths.root),
            )
        )
        if pr
        else list(changed_files)
    )
    scope_changed_files = pr_material_changed_files if pr else changed_files
    scope_inspection_error = report_diff.inspection_error
    scope_violation_files: list[str] = []
    scope_diagnostics = [item.to_payload() for item in scratch_scope_diagnostics]
    for diagnostic in scratch_scope_diagnostics:
        scope_warnings.append(
            "Scope recovery: "
            f"{diagnostic.classification} for {diagnostic.path} "
            f"({diagnostic.reason_code}; action={diagnostic.recommended_action})."
        )
    material_changes_detected = bool(pr_material_changed_files)
    nonzero_agent_exit = run_code != 0 and run_err is None
    scope_amendment_payloads: list[dict[str, Any]] = []
    scope_amended_patterns: list[str] = []
    scope_in_scope_changes = False
    adjacent_only_changes = False
    strict_scope_blocked = False
    can_attempt_pr_flow = False
    pr_nonzero_salvage_allowed = False
    pr_nonzero_salvage_attempted = False
    no_material_changes_blocked = False
    result_kind: str | None = None
    noop_reason: str | None = None
    analysis_only_noop_accepted = False
    if strict_ignored_workspace_paths:
        strict_scope_blocked = True
        scope_violation_files = list(strict_ignored_workspace_paths)
        scope_changed_files = _merge_changed_files(
            list(scope_changed_files),
            list(strict_ignored_workspace_paths),
        )
    if scope_mode in {"warn", "strict"}:
        if scope_inspection_error:
            if scope_mode == "strict":
                strict_scope_blocked = True
                success = False
                run_err = (run_err + "; " if run_err else "") + scope_inspection_error
            else:
                scope_warnings.append(scope_inspection_error)
        # Adjacent changes are triaged into scope amendments only in strict mode: warn
        # mode has nothing to unblock, so amending there would silently rewrite the plan
        # for a mode whose whole contract is "report, change nothing".
        scope_assessment = assess_scope_changes(
            scope_changed_files,
            allowed_scope,
            task=task,
            root=paths.root,
            extra_diagnostics=scratch_scope_diagnostics,
            amend_adjacent=(scope_mode == "strict" and strict_verification_executor is None),
            new_paths=[
                path
                for path in scope_changed_files
                if _normalize_changed_file_path(path) not in baseline_workspace_paths
            ],
        )
        scope_changed_files = list(scope_assessment.effective_changed_files)
        scope_diagnostics = [item.to_payload() for item in scope_assessment.diagnostics]
        scope_in_scope_changes = bool(scope_assessment.in_scope_paths)
        for diagnostic in scope_assessment.diagnostics:
            if diagnostic.allowed:
                warning = (
                    "Scope recovery: "
                    f"{diagnostic.classification} for {diagnostic.path} "
                    f"({diagnostic.reason_code}; action={diagnostic.recommended_action})."
                )
                if warning not in scope_warnings:
                    scope_warnings.append(warning)
        if scope_assessment.amendments and strict_verification_executor is None:
            scope_amendment_payloads = [item.to_payload() for item in scope_assessment.amendments]
            scope_amended_patterns = apply_scope_amendments(
                task,
                scope_assessment.amendments,
            )
            adjacent_only_changes = not scope_in_scope_changes
            amendment_preview = ", ".join(
                f"{item.path} ({item.reason_code})" for item in scope_assessment.amendments[:20]
            )
            if len(scope_assessment.amendments) > 20:
                amendment_preview += ", ..."
            scope_warnings.append(
                f"Scope amended: {len(scope_assessment.amendments)} adjacent change(s) "
                f"accepted and added to write_scope: {amendment_preview}."
            )
            events.emit(
                EVENT_SCOPE_AMENDED,
                {
                    "task_id": task_id,
                    "scope_mode": scope_mode,
                    "allowed_scope": list(allowed_scope),
                    "added_patterns": list(scope_amended_patterns),
                    "amendments": scope_amendment_payloads,
                    "adjacent_paths": list(scope_assessment.adjacent_paths),
                    "adjacent_only": bool(adjacent_only_changes),
                },
            )
        elif scope_assessment.amendments:
            # Defensive fail-closed branch: the Host path asks the assessor not
            # to produce amendments, but an implementation drift in that helper
            # must still never widen the frozen approval.
            strict_scope_blocked = True
            success = False
            amendment_paths = [item.path for item in scope_assessment.amendments]
            scope_violation_files = _merge_changed_files(
                scope_violation_files,
                amendment_paths,
            )
            run_err = (run_err + "; " if run_err else "") + (
                "Host approval scope is immutable; scope amendments were rejected"
            )
        host_scope_violations = (
            _host_scope_violations(
                list(scope_assessment.effective_changed_files),
                allowed_scope=allowed_scope,
                root=paths.root,
            )
            if strict_verification_executor is not None
            else []
        )
        if not scope_assessment.ok or host_scope_violations:
            violations = _merge_changed_files(
                list(scope_assessment.blocking_paths),
                host_scope_violations,
            )
            scope_violation_files = list(violations)
            preview = ", ".join(violations[:20])
            if len(violations) > 20:
                preview += ", ..."
            classes = sorted(
                {
                    str(item.get("classification") or "unknown")
                    for item in scope_diagnostics
                    if not bool(item.get("allowed"))
                }
            )
            scope_msg = (
                f"Out-of-scope file changes detected ({len(violations)}): {preview}. "
                f"Allowed scope: {allowed_scope or ['(none)']}."
            )
            if classes:
                scope_msg += f" Scope classifications: {', '.join(classes)}."
            # The full triage plus a ready-to-paste scope patch, so a human or the
            # replanner can fix the plan in one step instead of re-deriving it.
            violation_lines = describe_scope_violations(scope_assessment.diagnostics)
            if violation_lines:
                scope_msg += " Classified changes: " + " | ".join(violation_lines) + "."
            if scope_assessment.protected_paths:
                scope_msg += (
                    " Protected paths (never amendable): "
                    + ", ".join(scope_assessment.protected_paths)
                    + "."
                )
            if scope_assessment.suggested_scope_patterns:
                scope_msg += (
                    " Suggested write_scope additions: "
                    + ", ".join(scope_assessment.suggested_scope_patterns)
                    + "."
                )
            if scope_mode == "strict":
                strict_scope_blocked = True
                success = False
                run_err = (
                    (run_err + "; " if run_err else "")
                    + scope_msg
                    + " Task was blocked due to strict scope isolation."
                )
            else:
                scope_warnings.append(scope_msg)

    if pr and not runtime_artifacts_changed and not material_changes_detected:
        success = False

    if run_code == 0 and not runtime_artifacts_changed and not material_changes_detected:
        if _task_is_analysis_only(task):
            if pr:
                if not pr_base_branch:
                    success = False
                    run_err = (run_err + "; " if run_err else "") + (
                        "missing PR base branch context for analysis-only no-op cleanup"
                    )
                    merge_result = "not merged: analysis-only no-op cleanup failed"
                else:
                    try:
                        checkout_branch(
                            paths.root,
                            pr_base_branch,
                            base_branch=pr_base_branch,
                        )
                        if keep_branch or not pr_task_branch or pr_task_branch == pr_base_branch:
                            merge_result = "no merge required (analysis-only no-op; branch kept)"
                        else:
                            try:
                                delete_branch(paths.root, pr_task_branch)
                                merge_result = (
                                    "no merge required (analysis-only no-op; branch deleted)"
                                )
                            except GitOpsError as cleanup_err:
                                scope_warnings.append(
                                    "Branch cleanup warning: "
                                    f"failed to delete {pr_task_branch}: {cleanup_err}"
                                )
                                merge_result = (
                                    "no merge required (analysis-only no-op; "
                                    f"branch delete warning: {cleanup_err})"
                                )
                        success = True
                    except GitOpsError as e:
                        success = False
                        run_err = (run_err + "; " if run_err else "") + (
                            f"PR no-op cleanup failed: {e}"
                        )
                        merge_result = f"not merged: analysis-only no-op cleanup failed: {e}"
            else:
                success = True
            if success:
                result_kind = "success_noop"
                noop_reason = "analysis_only"
                analysis_only_noop_accepted = True
                verify_summary = "verification skipped: analysis-only task made no changes"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                )
        elif _task_declares_explicit_write_scope(task):
            no_material_changes_blocked = True
            success = False
            run_err = (run_err + "; " if run_err else "") + (
                "No material file changes were detected for a task with explicit write_scope. "
                "The task produced no file changes at all -- not in the declared scope and "
                "not adjacent to it -- so its expected local file update was not produced."
            )
    elif (
        run_code == 0
        and not runtime_artifacts_changed
        and adjacent_only_changes
        and not strict_scope_blocked
    ):
        # No declared scope path changed, but the task did do work: every change was
        # adjacent and has been amended into write_scope. That is a pass with an
        # amendment, not the "task did nothing" rejection above.
        result_kind = result_kind or "success_scope_amended"
        scope_warnings.append(
            "No declared write_scope path changed; every change was adjacent to the "
            "declared scope and write_scope was amended to cover it."
        )

    pr_nonzero_salvage_allowed = (
        pr
        and nonzero_agent_exit
        and strict_verification_executor is None
        and verify_mode == "strict"
        and bool(verify_commands)
        and not runtime_artifacts_changed
        and material_changes_detected
        and not strict_scope_blocked
    )

    if nonzero_agent_exit and pr_nonzero_salvage_allowed:
        pr_nonzero_salvage_attempted = True
        scope_warnings.append(
            f"PR flow attempted to salvage a non-zero agent exit ({run_code}); "
            "acceptance requires strict verification and PR gates."
        )
    elif nonzero_agent_exit:
        success = False
        run_err = (run_err + "; " if run_err else "") + (
            f"agent exited non-zero ({run_code}); refusing to accept partial task result"
        )

    strict_execution_evidence_complete = strict_verification_executor is None or (
        execution_usage.model_calls >= 1 and execution_usage.usage_complete
    )
    if not strict_execution_evidence_complete:
        success = False
        usage_reason = (
            "no model call was retained"
            if execution_usage.model_calls < 1
            else "model-call usage classification is incomplete"
        )
        run_err = (run_err + "; " if run_err else "") + (
            "Phase 0 execution evidence is incomplete: " + usage_reason
        )
        merge_result = "not merged: incomplete model-call evidence"

    can_attempt_pr_flow = (
        pr
        and not runtime_artifacts_changed
        and material_changes_detected
        and not strict_scope_blocked
        and (run_code == 0 or pr_nonzero_salvage_allowed)
        and strict_execution_evidence_complete
    )

    if can_attempt_pr_flow:
        success = True
        if not pr_base_branch or not pr_task_branch:
            success = False
            run_err = (run_err + "; " if run_err else "") + "missing PR branch context"
            merge_result = "not merged"
        else:
            try:
                non_material_untracked_paths = list_untracked_packaging_metadata_paths(paths.root)
                stage_all(paths.root)
                unstage_staged_prefixes(
                    paths.root,
                    [".alysis", ".alysis_images", "alysis-feedback"],
                )
                ensure_not_staged_prefixes(
                    paths.root,
                    [".alysis", ".alysis_images", "alysis-feedback"],
                )
                if non_material_untracked_paths:
                    unstage_staged_paths(paths.root, non_material_untracked_paths)
                    ensure_not_staged_paths(paths.root, non_material_untracked_paths)
                if has_grounded_rust_target_runtime_artifacts(paths.root):
                    staged_now = staged_files(paths.root)
                    unstage_staged_runtime_artifacts(
                        paths.root,
                        current_paths=staged_now,
                    )
                    staged_now = staged_files(paths.root)
                    ensure_not_staged_runtime_artifacts(
                        paths.root,
                        current_paths=staged_now,
                    )
                commit_title = str(task.get("title") or "").strip() or "task update"
                commit_hash = commit_all(
                    paths.root,
                    message=f"{task_id}: {commit_title}",
                )
                report_base_revision = (
                    strict_base_head if strict_verification_executor is not None else pr_base_branch
                )
                assert report_base_revision is not None
                patch_text = format_patch_stdout(
                    paths.root,
                    base_branch=report_base_revision,
                )
                patch_path.write_text(
                    patch_text if patch_text else "(empty format-patch output)\n",
                    encoding="utf-8",
                )
                changed_files = changed_files_between(
                    paths.root,
                    revspec=f"{report_base_revision}..HEAD",
                )
                if strict_verification_executor is not None:
                    committed_scope_violations = _host_scope_violations(
                        changed_files,
                        allowed_scope=allowed_scope,
                        root=paths.root,
                    )
                    if committed_scope_violations:
                        strict_scope_blocked = True
                        success = False
                        scope_violation_files = _merge_changed_files(
                            scope_violation_files,
                            committed_scope_violations,
                        )
                        run_err = (run_err + "; " if run_err else "") + (
                            "committed changes exceeded immutable Host approval scope: "
                            + ", ".join(committed_scope_violations[:20])
                        )
                        merge_result = "not merged: immutable Host scope was exceeded"
                pr_report_state_upgraded = True
            except GitOpsError as e:
                success = False
                run_err = (run_err + "; " if run_err else "") + f"PR flow failed: {e}"
                merge_result = f"not merged: {e}"

            if success and remote_settings.enabled:
                if not pr_task_branch:
                    success = False
                    run_err = (
                        run_err + "; " if run_err else ""
                    ) + "missing task branch for remote sync"
                    merge_result = "not merged: remote sync branch context missing"
                else:
                    remote_name = remote_settings.remote_name
                    provider = "unknown"
                    remote_record = init_remote_record(
                        task_id=task_id,
                        remote=remote_name,
                        provider=provider,
                    )
                    remote_errors = remote_record["errors"]
                    assert isinstance(remote_errors, list)
                    try:
                        remote_url = get_remote_url(paths.root, remote_name)
                        provider = resolve_provider(
                            settings_provider=remote_settings.provider,
                            remote_url=remote_url,
                        )
                        remote_record["provider"] = provider
                    except RemoteSyncError as e:
                        msg = f"remote discovery failed: {e}"
                        remote_errors.append(msg)
                        if remote_settings.strict:
                            success = False
                            run_err = (run_err + "; " if run_err else "") + msg
                            merge_result = f"not merged: {msg}"
                        else:
                            scope_warnings.append(msg)

                    if success:
                        pushed_branch, branch_output = push_branch(
                            paths.root,
                            remote=remote_name,
                            branch=pr_task_branch,
                        )
                        remote_record["pushed_branch"] = pushed_branch
                        remote_record["branch_push_output"] = truncate_output(branch_output)
                        if not pushed_branch:
                            msg = f"remote branch push failed: {branch_output or 'unknown error'}"
                            remote_errors.append(msg)
                            if remote_settings.strict:
                                success = False
                                run_err = (run_err + "; " if run_err else "") + msg
                                merge_result = f"not merged: {msg}"
                            else:
                                scope_warnings.append(msg)

                    if success and remote_settings.create_pr and remote_record is not None:
                        created_pr, pr_url, pr_id, pr_output = ensure_pr_or_mr(
                            paths.root,
                            provider=str(remote_record.get("provider") or "unknown"),
                            base_branch=pr_base_branch,
                            head_branch=pr_task_branch,
                            title=(
                                f"{task_id}: "
                                f"{str(task.get('title') or '').strip() or 'task update'}"
                            ),
                            body=instruction[:4000],
                        )
                        remote_record["created_pr"] = created_pr
                        remote_record["pr_url"] = pr_url
                        remote_record["pr_number_or_iid"] = pr_id
                        remote_record["pr_output"] = truncate_output(pr_output)
                        if created_pr and pr_url:
                            task["remote_pr_url"] = pr_url
                            task["remote_provider"] = str(
                                remote_record.get("provider") or "unknown"
                            )
                            save_plan(paths, plan)
                        if not created_pr:
                            msg = f"remote PR/MR creation failed: {pr_output or 'unknown error'}"
                            remote_errors.append(msg)
                            if remote_settings.strict:
                                success = False
                                run_err = (run_err + "; " if run_err else "") + msg
                                merge_result = f"not merged: {msg}"
                            else:
                                scope_warnings.append(msg)

                    if remote_record is not None:
                        write_remote_record(
                            execution_dir=paths.execution_dir,
                            task_id=task_id,
                            record=remote_record,
                        )

            def _run_repair_agent(repair_instruction: str, attempt: int) -> int:
                """Re-run the executing agent on this task with a repair prompt.

                Same machinery as the original invocation -- same config, mode,
                write guard, budget and MCP scope -- only the instruction and the
                session id differ, so a repair is not a weaker kind of run.
                """
                repair_session_id = f"{runtime_session_id}-repair{attempt}"
                repair_sessions_dir = _execution_private_sessions_dir(
                    cfg=run_cfg,
                    run_id=paths.run_id,
                    task_id=f"{task_id}-repair{attempt}",
                    workspace_root=paths.root,
                )
                _cleanup_execution_private_sessions_dir(repair_sessions_dir)
                repair_mcp_manager: Any | None = None
                try:
                    repair_mcp_manager = _build_forge_task_scoped_mcp_manager(
                        workspace_root=paths.root,
                        session_id=repair_session_id,
                        task_scope=task_mcp_scope,
                    )
                    if asset_surface is not None and has_mirrored_task_assets:
                        repair_mcp_manager = compose_worker_asset_mcp_manager(
                            base_manager=repair_mcp_manager,
                            asset_manager=build_worker_asset_mcp_manager(
                                cfg=run_cfg,
                                surface=asset_surface,
                                model_registry=asset_model_registry,
                                mirror=task_asset_mirror,
                                usage_logger=asset_usage_logger,
                                api_key=api_key_override,
                            ),
                        )
                    return run_agent(
                        cfg=run_cfg,
                        root=paths.root,
                        instruction=repair_instruction,
                        acceptance_instruction=build_task_acceptance_instruction(task),
                        mode=effective_mode,
                        runtime_kind=RuntimeKind.FORGE_EXEC,
                        yes=yes,
                        max_steps=task_step_budget.resolved_max_steps,
                        no_log=no_log,
                        api_key_override=api_key_override,
                        console=console,
                        surface=recording_surface,
                        image_paths=task_image_paths,
                        deny_write_prefixes=[".alysis"],
                        allow_write_globs=allowed_scope if scope_mode == "strict" else None,
                        non_interactive=run_non_interactive,
                        session_log_dir_override=repair_sessions_dir,
                        session_id_override=repair_session_id,
                        usage_role=f"forge_exec:{task_id}:repair{attempt}",
                        enable_compaction=False,
                        enable_tool_output_offload=True,
                        enable_conversation_summarization=True,
                        compaction_profile="execution",
                        enable_chat_turn_step_budget=False,
                        one_shot_execution=True,
                        verification_enabled=prompt_verification_enabled,
                        authoritative_verification_commands=(
                            verify_commands if prompt_verification_enabled else None
                        ),
                        subagents_enabled=False,
                        enforce_explicit_subagent_requests=False,
                        mcp_manager=repair_mcp_manager,
                        execution_deadline=execution_deadline,
                        session_source_metadata={
                            "surface": "forge_exec",
                            "run_id": paths.run_id,
                            "task_id": str(task_id),
                            "verification_repair_attempt": int(attempt),
                        },
                    )
                finally:
                    if repair_mcp_manager is not None:
                        repair_mcp_manager.close()
                    _cleanup_execution_private_sessions_dir(repair_sessions_dir)

            if success and strict_verification_executor is not None:
                # Host Phase 0 executes its immutable admitted argv here, after
                # the task commit exists but before review/merge.  This branch
                # has no legacy verifier or repair fallback: exact evidence is
                # the only fact that may keep ``success`` true.
                before_verify_snapshot = _snapshot_workspace_tree(paths.root)
                before_host_workspace: dict[str, tuple[str, int, int, str]] | None = None
                after_host_workspace: dict[str, tuple[str, int, int, str]] | None = None
                before_verify_git: tuple[str, str, str, str, str, str] | None = None
                after_verify_git: tuple[str, str, str, str, str, str] | None = None
                expected_verify_git: tuple[str, str, str, str, str, str] | None = None
                exact_error: str | None = None
                try:
                    if not pr_task_branch or not commit_hash or not strict_base_head:
                        raise GitOpsError("exact Host verification lacks pinned Git identities")
                    before_host_workspace = _host_workspace_snapshot(
                        paths.root,
                        execution_deadline=execution_deadline,
                        allowed_artifact_paths=strict_expected_artifact_paths,
                    )
                    before_verify_git = _host_git_metadata_snapshot(
                        paths.root,
                        task_branch=pr_task_branch,
                        base_branch=pr_base_branch,
                        execution_deadline=execution_deadline,
                    )
                    expected_verify_git = (
                        pr_task_branch,
                        commit_hash,
                        commit_hash,
                        strict_base_head,
                        "",
                        before_verify_git[-1],
                    )
                    if before_verify_git != expected_verify_git:
                        raise GitOpsError(
                            "repository identity changed before exact Host verification"
                        )
                    structured_verification = tuple(strict_verification_executor())
                except Exception as exc:  # noqa: BLE001 - retain a rejected task outcome
                    exact_error = sanitize_error_summary(str(exc) or exc.__class__.__name__)
                if before_verify_git is not None and pr_task_branch and strict_base_head:
                    try:
                        after_verify_git = _host_git_metadata_snapshot(
                            paths.root,
                            task_branch=pr_task_branch,
                            base_branch=pr_base_branch,
                            execution_deadline=execution_deadline,
                            expected_metadata_digest=before_verify_git[-1],
                        )
                    except Exception as exc:  # noqa: BLE001 - fail closed on Git mutation
                        exact_error = exact_error or sanitize_error_summary(
                            str(exc) or exc.__class__.__name__
                        )
                if before_host_workspace is not None:
                    try:
                        after_host_workspace = _host_workspace_snapshot(
                            paths.root,
                            execution_deadline=execution_deadline,
                            allowed_artifact_paths=strict_expected_artifact_paths,
                        )
                    except Exception as exc:  # noqa: BLE001 - fail closed on snapshot races
                        exact_error = exact_error or sanitize_error_summary(
                            str(exc) or exc.__class__.__name__
                        )
                after_verify_snapshot = _snapshot_workspace_tree(paths.root)
                exact_verify_diff = _build_workspace_snapshot_reporting_diff(
                    paths.root,
                    before_snapshot=before_verify_snapshot,
                    after_snapshot=after_verify_snapshot,
                )
                exact_passed = bool(structured_verification) and all(
                    result.passed for result in structured_verification
                )
                expected_bindings = tuple(
                    (item.command_id, item.argv) for item in strict_verification_bindings
                )
                actual_bindings = tuple(
                    (item.command_id, item.argv) for item in structured_verification
                )
                exact_binding_match = actual_bindings == expected_bindings
                actual_artifact_paths = tuple(
                    result.artifact.path.resolve(strict=False) for result in structured_verification
                )
                exact_artifact_path_match = actual_artifact_paths == strict_expected_artifact_paths
                exact_artifacts_match = bool(structured_verification) and all(
                    retained_artifact_matches(
                        result.artifact,
                        containment_root=paths.execution_verify_dir,
                    )
                    for result in structured_verification
                )
                exact_git_unchanged = (
                    expected_verify_git is not None
                    and before_verify_git == expected_verify_git
                    and after_verify_git == expected_verify_git
                )
                exact_workspace_unchanged = (
                    before_host_workspace is not None
                    and before_host_workspace == after_host_workspace
                )
                exact_workspace_changes = _host_snapshot_changed_paths(
                    before_host_workspace or {},
                    after_host_workspace or {},
                )
                exact_log_current = strict_log_binding is not None and retained_artifact_matches(
                    strict_log_binding,
                    containment_root=paths.execution_logs_dir,
                )
                exact_passed = (
                    exact_passed
                    and exact_binding_match
                    and exact_artifact_path_match
                    and exact_artifacts_match
                    and exact_git_unchanged
                    and exact_workspace_unchanged
                    and exact_log_current
                )
                if exact_error is not None:
                    verify_summary = f"exact Host verification failed internally: {exact_error}"
                elif not exact_git_unchanged:
                    verify_summary = "exact Host verification changed repository identity"
                elif not exact_workspace_unchanged:
                    verify_summary = "exact Host verification changed workspace state"
                elif not exact_log_current:
                    verify_summary = "exact Host verification changed retained execution evidence"
                elif structured_verification and not exact_binding_match:
                    verify_summary = "exact Host verification evidence did not match approval"
                elif structured_verification and not exact_artifact_path_match:
                    verify_summary = "exact Host verification artifact path did not match approval"
                elif structured_verification and not exact_artifacts_match:
                    verify_summary = "exact Host verification artifact binding is unavailable"
                elif structured_verification:
                    passed_count = sum(result.passed for result in structured_verification)
                    verify_summary = (
                        f"exact Host verification passed ({passed_count}/"
                        f"{len(structured_verification)})"
                        if exact_passed
                        else (
                            f"exact Host verification failed ({passed_count}/"
                            f"{len(structured_verification)} passed)"
                        )
                    )
                else:
                    verify_summary = "exact Host verification returned no command evidence"
                verify_payload = {
                    "kind": "forge_host_v2_exact",
                    "all_passed": exact_passed,
                    "artifacts_current": exact_artifacts_match,
                    "artifact_paths_current": exact_artifact_path_match,
                    "git_identity_current": exact_git_unchanged,
                    "workspace_state_current": exact_workspace_unchanged,
                    "execution_log_current": exact_log_current,
                    "command_results": [
                        {
                            "command_id": result.command_id,
                            "argv": list(result.argv),
                            "status": result.status,
                            "real_execution": result.real_execution,
                            "exit_code": result.exit_code,
                            "timed_out": result.timed_out,
                            "artifact": os.fspath(result.artifact.path),
                            "artifact_digest": result.artifact.digest,
                            "artifact_size_bytes": result.artifact.size_bytes,
                        }
                        for result in structured_verification
                    ],
                }
                events.emit(
                    EVENT_VERIFICATION_RESULT,
                    {
                        "scope": "task",
                        "task_id": task_id,
                        "passed": exact_passed,
                        "policy": "strict",
                        "pass": "host_v2_exact",
                        "summary": verify_summary,
                        "commands": [shlex.join(result.argv) for result in structured_verification],
                        "command_source": "forge_host_v2_admitted",
                        "result": verify_payload,
                    },
                )
                if not exact_git_unchanged:
                    success = False
                    verify_blocked = True
                    merge_result = "not merged: exact verification changed Git identity"
                    run_err = (run_err + "; " if run_err else "") + (
                        "exact Host verification changed repository identity"
                    )
                elif not exact_workspace_unchanged:
                    success = False
                    verify_blocked = True
                    merge_result = "not merged: exact verification changed workspace state"
                    changed_preview = ", ".join(exact_workspace_changes[:20])
                    run_err = (run_err + "; " if run_err else "") + (
                        "exact Host verification modified workspace state: " + changed_preview
                    )
                    changed_files = _merge_changed_files(
                        changed_files,
                        exact_workspace_changes,
                    )
                    _append_patch_debug_section(
                        patch_path,
                        title="Post-verification workspace diff",
                        patch_text=exact_verify_diff.patch_text,
                    )
                elif not exact_log_current:
                    success = False
                    verify_blocked = True
                    merge_result = "not merged: exact verification changed execution evidence"
                    run_err = (run_err + "; " if run_err else "") + (
                        "exact Host verification changed retained execution evidence"
                    )
                elif not exact_passed:
                    success = False
                    verify_blocked = True
                    merge_result = "not merged: exact Host verification failed"
                    run_err = (run_err + "; " if run_err else "") + verify_summary
            elif success and verify_mode != "off" and verify_commands:
                verify_mutation_paths: list[str] = []
                verify_mutation_patch_sections: list[str] = []

                def _verification_pass(pass_label: str) -> Any:
                    """Run the gate once, recording anything it changed on disk.

                    Each pass is snapshotted separately so a repair attempt's own
                    (committed) edits between passes are never mistaken for a
                    verification command mutating the repository.
                    """
                    before_verify_snapshot = _snapshot_workspace_tree(paths.root)
                    result = run_task_verification(
                        root=paths.root,
                        commands=verify_commands,
                        artifact_path=verify_path,
                        cfg=effective,
                    )
                    events.emit(
                        EVENT_VERIFICATION_RESULT,
                        {
                            "scope": "task",
                            "task_id": task_id,
                            "passed": bool(result.all_passed),
                            "policy": verify_mode,
                            "pass": pass_label,
                            "summary": result.summary,
                            "failure_category": result.failure_category_value,
                            "commands": list(verify_commands),
                            "command_source": verify_command_source,
                            "artifact": os.fspath(verify_path),
                            "result": verify_run_result_to_payload(
                                root=paths.root,
                                result=result,
                            ),
                        },
                    )
                    after_verify_snapshot = _snapshot_workspace_tree(paths.root)
                    pass_diff = _build_workspace_snapshot_reporting_diff(
                        paths.root,
                        before_snapshot=before_verify_snapshot,
                        after_snapshot=after_verify_snapshot,
                    )
                    for path in pass_diff.changed_files:
                        if path not in verify_mutation_paths:
                            verify_mutation_paths.append(path)
                    if pass_diff.patch_text:
                        verify_mutation_patch_sections.append(pass_diff.patch_text)
                    return result

                verify_result = _verification_pass("initial")

                # A failing gate is a signal about the work, not a verdict on it:
                # hand the failing output back to the same agent and let it fix
                # what it broke before the task is called failed. Only in strict
                # mode, because that is the only mode where the failure blocks.
                repair_outcome = None
                if (
                    verify_run_status(verify_result) == "failed"
                    and verify_mode == "strict"
                    and verify_repair_budget > 0
                ):
                    repair_outcome = run_verification_repair_loop(
                        initial_result=verify_result,
                        max_attempts=verify_repair_budget,
                        repairable=lambda result: (
                            result.failure_category_value != FailureCategory.INFRA_UNAVAILABLE.value
                        ),
                        attempt_repair=lambda attempt, failing: _attempt_verification_repair(
                            attempt=attempt,
                            failing_result=failing,
                            max_attempts=verify_repair_budget,
                            task_id=task_id,
                            base_instruction=instruction,
                            verify_artifact_path=verify_path,
                            run_verification=lambda: _verification_pass(f"repair.{attempt}"),
                            run_repair_agent=_run_repair_agent,
                            root=paths.root,
                            commit_message=(f"{task_id}: verification repair attempt {attempt}"),
                        ),
                    )
                    verify_result = repair_outcome.final_result
                    verification_repair_payload = repair_outcome.to_payload()
                    for line in repair_outcome.report_lines():
                        scope_warnings.append(line)
                    if repair_outcome.attempts:
                        try:
                            patch_text = format_patch_stdout(
                                paths.root,
                                base_branch=pr_base_branch,
                            )
                            patch_path.write_text(
                                patch_text if patch_text else "(empty format-patch output)\n",
                                encoding="utf-8",
                            )
                            changed_files = changed_files_between(
                                paths.root,
                                revspec=f"{pr_base_branch}..HEAD",
                            )
                        except GitOpsError as e:
                            scope_warnings.append(
                                f"Could not refresh the patch after verification repair: {e}"
                            )

                verify_summary = verify_result.summary
                verify_payload = verify_run_result_to_payload(
                    root=paths.root,
                    result=verify_result,
                )
                if repair_outcome is not None and repair_outcome.attempts:
                    verify_summary = (
                        f"{verify_summary} "
                        f"(after {repair_outcome.attempts_used} repair attempt"
                        f"{'s' if repair_outcome.attempts_used != 1 else ''})"
                    )
                verify_mutation_diff_text = "\n".join(verify_mutation_patch_sections)
                if verify_mutation_paths:
                    preview = ", ".join(verify_mutation_paths[:20])
                    if len(verify_mutation_paths) > 20:
                        preview += ", ..."
                    verify_mutation_msg = (
                        "Verification commands modified repository state after the task commit "
                        f"({len(verify_mutation_paths)}): {preview}."
                    )
                    if scope_mode in {"warn", "strict"}:
                        scope_assessment = assess_scope_changes(
                            verify_mutation_paths,
                            allowed_scope,
                            task=task,
                            root=paths.root,
                        )
                        scope_diagnostics.extend(
                            item.to_payload() for item in scope_assessment.diagnostics
                        )
                        if not scope_assessment.ok:
                            verify_scope_violations = scope_assessment.blocking_paths
                            scope_violation_files = _merge_changed_files(
                                scope_violation_files,
                                verify_scope_violations,
                            )
                            classes = sorted(
                                {
                                    item.classification
                                    for item in scope_assessment.diagnostics
                                    if not item.allowed
                                }
                            )
                            scope_msg = (
                                f"Out-of-scope file changes detected ({len(verify_scope_violations)}): "
                                f"{', '.join(verify_scope_violations[:20])}"
                            )
                            if len(verify_scope_violations) > 20:
                                scope_msg += ", ..."
                            scope_msg += (
                                f". Allowed scope: {allowed_scope or ['(none)']}."
                                " Task was blocked due to strict scope isolation."
                                " Verification commands modified repository state after the task commit."
                            )
                            if classes:
                                scope_msg += f" Scope classifications: {', '.join(classes)}."
                            if scope_mode == "strict":
                                success = False
                                commit_hash = None
                                merge_result = "not merged: strict scope isolation blocked verification-time writes"
                                run_err = (run_err + "; " if run_err else "") + scope_msg
                                changed_files = _merge_changed_files(
                                    changed_files,
                                    verify_mutation_paths,
                                )
                                _append_patch_debug_section(
                                    patch_path,
                                    title="Post-verification workspace diff",
                                    patch_text=verify_mutation_diff_text,
                                )
                            else:
                                scope_warnings.append(scope_msg)
                        elif scope_mode == "strict":
                            success = False
                            commit_hash = None
                            merge_result = (
                                "not merged: verification commands modified repository state"
                            )
                            run_err = (run_err + "; " if run_err else "") + verify_mutation_msg
                            changed_files = _merge_changed_files(
                                changed_files, verify_mutation_paths
                            )
                            _append_patch_debug_section(
                                patch_path,
                                title="Post-verification workspace diff",
                                patch_text=verify_mutation_diff_text,
                            )
                        else:
                            scope_warnings.append(verify_mutation_msg)
                if verify_run_status(verify_result) == "not_run":
                    # Tri-state: every command was benignly skipped, so
                    # nothing checked this merge. Not a pass (the pre-fix
                    # vacuous True treated it as one) and not a failure either:
                    # warn honestly and proceed, matching the no-authoritative-
                    # commands stance below.
                    scope_warnings.append(
                        f"Verification did not execute: {verify_result.summary} -- "
                        "nothing checked this task's merge."
                    )
                elif not verify_result.all_passed and verify_mode == "strict":
                    success = False
                    verify_blocked = True
                    merge_result = "not merged: strict verification failed"
                    run_err = (run_err + "; " if run_err else "") + (
                        f"verification failed: {verify_result.summary}"
                    )
                elif not verify_result.all_passed:
                    warning_prefix = (
                        "Verification infrastructure warning"
                        if verify_result.failure_category_value
                        == FailureCategory.INFRA_UNAVAILABLE.value
                        else "Verification warning"
                    )
                    scope_warnings.append(f"{warning_prefix}: {verify_result.summary}")
            elif success and verify_mode == "strict":
                # The work is committed on the task branch and no authoritative
                # command exists to check it -- a property of the workspace, not
                # a defect in the work. Failing here used to discard a completed
                # task over missing tooling, so the task now completes unverified:
                # nothing is merged, the branch stays intact for review, and the
                # honest outcome is recorded instead of a fabricated failure.
                verification_unavailable_completion = True
                verify_summary = (
                    "verification skipped: no authoritative commands available; "
                    "task kept as completed_unverified"
                )
                merge_result = (
                    "not merged: verification unavailable "
                    f"(branch {pr_task_branch} kept for review)"
                )
                scope_warnings.append(
                    "Strict verification found no authoritative command for this task. "
                    "The work was kept and committed, but nothing checked it -- review "
                    "the branch before merging, or provide --verify-cmd."
                )
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                    outcome=TASK_STATUS_COMPLETED_UNVERIFIED,
                )
            elif verify_mode == "strict":
                # Reached only when the task already failed for another reason;
                # strict verification simply had nothing to add.
                success = False
                verify_blocked = True
                merge_result = "not merged: strict verification unavailable"
                run_err = (run_err + "; " if run_err else "") + (
                    "strict verification requires authoritative commands, but none were available"
                )
                verify_summary = "verification skipped: no authoritative commands available"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=True,
                )
            elif verify_mode != "off":
                verify_summary = "verification skipped: no authoritative commands available"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                )
            elif verify_mode == "off":
                verify_summary = "verification disabled (--verify off)"
                _emit_verification_unavailable(
                    events,
                    task_id=task_id,
                    policy=verify_mode,
                    reason=verify_summary,
                    blocking=False,
                )

            if success and review:
                try:
                    review_outcome = review_task(
                        paths=paths,
                        plan=plan,
                        task=task,
                        cfg=effective,
                        api_key_override=api_key_override,
                        verification_payload_override=verify_payload,
                    )
                    events.emit(
                        EVENT_REVIEW_RESULT,
                        {
                            "task_id": task_id,
                            "approved": bool(review_outcome.approved),
                            "confidence": review_outcome.confidence,
                            "summary": review_outcome.summary,
                            "blocking_issues": review_outcome.blocking_issues_count,
                            "non_blocking_issues": review_outcome.non_blocking_issues_count,
                            "review_json": os.fspath(review_outcome.json_path),
                            "review_markdown": os.fspath(review_outcome.markdown_path),
                        },
                    )
                    if not review_outcome.approved:
                        success = False
                        review_blocked = True
                        merge_result = "not merged: review requested changes"
                except ReviewError as e:
                    success = False
                    run_err = (run_err + "; " if run_err else "") + f"review failed: {e}"
                    merge_result = f"not merged: review failed: {e}"
                    events.emit(
                        EVENT_REVIEW_RESULT,
                        {
                            "task_id": task_id,
                            "approved": False,
                            "available": False,
                            "error": str(e),
                        },
                    )

            if success and verification_unavailable_completion:
                # Nothing merges without a check behind it. Return the worktree to
                # the base branch so the run leaves the repository where it found
                # it, and leave the task branch in place for a human to review.
                try:
                    checkout_branch(
                        paths.root,
                        pr_base_branch,
                        base_branch=pr_base_branch,
                    )
                except GitOpsError as e:
                    scope_warnings.append(
                        "Could not return to "
                        f"{pr_base_branch} after an unverified completion: {e}. "
                        f"The work is committed on {pr_task_branch}."
                    )
            elif success:
                try:
                    merge_title = str(task.get("title") or "").strip()
                    merge_message = (
                        f"Merge {task_id}: {merge_title}" if merge_title else f"Merge {task_id}"
                    )
                    if strict_verification_executor is not None:
                        if not strict_base_head or not commit_hash or execution_deadline is None:
                            raise GitOpsError("Host merge lacks pinned Git identities")
                        prepared_integration = _prepare_new_host_task_integration(
                            paths.root,
                            native_run_id=paths.run_id,
                            task_id=task_id,
                            journal_dir=paths.execution_integration_dir / "transactions",
                            base_branch=pr_base_branch,
                            base_head=strict_base_head,
                            task_branch=pr_task_branch,
                            task_commit=commit_hash,
                            message=merge_message,
                            execution_deadline=execution_deadline,
                            mutable_runtime_paths=(
                                paths.plan_json_path,
                                paths.plan_md_path,
                                paths.runtime_dir / "current_run.json",
                                paths.execution_dir / "sequential_summary.json",
                                paths.execution_dir / "sequential_run.jsonl",
                            ),
                        )
                        merge_commit_hash = prepared_integration.facts.prepared_merge_commit
                        merge_first_parent = strict_base_head
                        merge_second_parent = commit_hash
                        base_ref_after_merge = strict_base_head
                        merge_result = (
                            f"prepared for atomic integration into {pr_base_branch} "
                            "(base ref unchanged)"
                        )
                    else:
                        merge_commit_hash = merge_no_ff(
                            paths.root,
                            base_branch=pr_base_branch,
                            task_branch=pr_task_branch,
                            message=merge_message,
                        )
                    if strict_verification_executor is not None:
                        pass
                    elif keep_branch:
                        merge_result = f"merged into {pr_base_branch} (branch kept)"
                    else:
                        try:
                            delete_branch(paths.root, pr_task_branch)
                            merge_result = f"merged into {pr_base_branch} (branch deleted)"
                        except GitOpsError as cleanup_err:
                            scope_warnings.append(
                                "Branch cleanup warning: "
                                f"failed to delete {pr_task_branch}: {cleanup_err}"
                            )
                            merge_result = (
                                f"merged into {pr_base_branch} "
                                f"(branch delete warning: {cleanup_err})"
                            )
                except GitOpsError as e:
                    success = False
                    run_err = (run_err + "; " if run_err else "") + f"PR flow failed: {e}"
                    unmerged = list_unmerged_files(paths.root)
                    if unmerged and pr_base_branch and pr_task_branch:
                        merge_conflict_detected = True
                        context = capture_merge_conflict_context(
                            paths.root,
                            base_branch=pr_base_branch,
                            task_branch=pr_task_branch,
                            merge_error=str(e),
                        )
                        review_outcome = (
                            None
                            if strict_verification_executor is not None
                            else review_merge_conflict(
                                paths=paths,
                                task=task,
                                cfg=effective,
                                api_key_override=api_key_override,
                                context=context,
                                plan=plan,
                            )
                        )
                        cleanup_ok, cleanup_log = try_abort_merge(
                            paths.root,
                            base_branch=pr_base_branch,
                        )
                        conflict_artifacts = write_conflict_artifacts(
                            paths=paths,
                            task_id=task_id,
                            context=context,
                            review_json=(review_outcome.review_json if review_outcome else None),
                            review_md=(
                                review_outcome.review_markdown
                                if review_outcome
                                else "Host Phase 0 disabled model-based conflict review."
                            ),
                            cleanup_log=cleanup_log,
                        )
                        conflict_review_path = conflict_artifacts.review_md_path
                        merge_result = (
                            f"not merged: conflict while merging {pr_task_branch} into "
                            f"{pr_base_branch}"
                        )
                        if review_outcome is not None and review_outcome.skipped_reason:
                            scope_warnings.append(
                                f"Conflict review note: {review_outcome.skipped_reason}"
                            )
                        if not cleanup_ok:
                            scope_warnings.append(
                                "Merge cleanup warning: repository state may need manual recovery. "
                                f"See {conflict_artifacts.cleanup_log_path}"
                            )
                        if auto_resolve_conflicts and can_attempt_conflict_auto_resolve(
                            task=task,
                            settings=conflict_auto_settings,
                        ):
                            bump_conflict_attempt(task)
                            save_plan(paths, plan)
                            auto_outcome = attempt_auto_resolve_conflict(
                                paths=paths,
                                plan=plan,
                                task=task,
                                cfg=effective,
                                api_key_override=api_key_override,
                                base_branch=pr_base_branch,
                                task_branch=pr_task_branch,
                                keep_worktrees=False,
                                settings=conflict_auto_settings,
                                verify_commands=(verify_commands if verify_mode != "off" else []),
                            )
                            if auto_outcome.success:
                                success = True
                                run_err = None
                                merge_conflict_detected = False
                                merge_commit_hash = auto_outcome.merge_commit_hash
                                merge_result = f"auto-resolved and merged into {pr_base_branch}"
                                if auto_outcome.warnings:
                                    scope_warnings.extend(auto_outcome.warnings)
                                conflict_review_path = auto_outcome.report_path
                            else:
                                scope_warnings.append(
                                    "Conflict auto-resolve failed: "
                                    f"{auto_outcome.error or 'unknown error'}"
                                )
                        else:
                            # The sequential path stops at a conflict instead of
                            # starting a resolver agent in its own worktree. That
                            # is swarm machinery, and reaching for it here turned
                            # "one task did not merge" into a second opaque agent
                            # run. Report the conflict and how to finish it.
                            scope_warnings.extend(
                                _sequential_conflict_report_lines(
                                    root=paths.root,
                                    task_id=task_id,
                                    base_branch=pr_base_branch,
                                    task_branch=pr_task_branch,
                                    review_path=conflict_review_path,
                                )
                            )
                    else:
                        merge_result = f"not merged: {e}"

            if success and remote_settings.enabled and remote_record is not None and pr_base_branch:
                pushed_base, base_output = push_base(
                    paths.root,
                    remote=str(remote_record.get("remote") or remote_settings.remote_name),
                    base_branch=pr_base_branch,
                )
                remote_record["pushed_base"] = pushed_base
                remote_record["base_push_output"] = truncate_output(base_output)
                if not pushed_base:
                    msg = f"remote base push failed: {base_output or 'unknown error'}"
                    raw_errors = remote_record.get("errors")
                    if isinstance(raw_errors, list):
                        raw_errors.append(msg)
                    # Local merge already happened; keep success and record warning.
                    scope_warnings.append(msg)
                write_remote_record(
                    execution_dir=paths.execution_dir,
                    task_id=task_id,
                    record=remote_record,
                )

    if pr and merge_result is None:
        merge_result = "not merged"

    if pr and not pr_report_state_upgraded:
        recovered_pr_report_state = False
        if commit_hash is not None and pr_base_branch:
            try:
                report_base_revision = (
                    strict_base_head if strict_verification_executor is not None else pr_base_branch
                )
                assert report_base_revision is not None
                patch_text = format_patch_stdout(
                    paths.root,
                    base_branch=report_base_revision,
                )
                patch_path.write_text(
                    patch_text if patch_text else "(empty format-patch output)\n",
                    encoding="utf-8",
                )
                changed_files = changed_files_between(
                    paths.root,
                    revspec=f"{report_base_revision}..HEAD",
                )
                recovered_pr_report_state = True
            except GitOpsError:
                recovered_pr_report_state = False
        if not recovered_pr_report_state:
            patch_path.write_text(report_diff.patch_text, encoding="utf-8")
            changed_files = list(report_diff.changed_files)

    if runtime_artifacts_changed:
        summary = (
            "Task failed: agent modified files under .alysis/ which is not allowed. "
            "Changed paths: " + ", ".join(runtime_artifact_changes[:10])
        )
    elif scope_violation_files:
        summary = "Task blocked due to strict scope isolation."
    elif no_material_changes_blocked:
        summary = "Task failed: no material file changes were detected."
    elif verify_blocked:
        summary = "Task blocked by strict verification gate."
    elif review_blocked:
        summary = "Task blocked by review gate (changes requested)."
    elif analysis_only_noop_accepted:
        summary = "Analysis-only task completed successfully with no repository changes."
    elif success and verification_unavailable_completion:
        summary = (
            "Task completed, but nothing verified it: no authoritative verification "
            f"command exists for this workspace. The work is committed on "
            f"{pr_task_branch or 'the task branch'} and was deliberately not merged."
        )
    elif success and prepared_integration is not None:
        summary = "Task execution is verified and prepared for atomic Host integration."
    elif run_code == 0 and success:
        summary = "Task execution completed successfully."
    elif pr and can_attempt_pr_flow and run_code == 0:
        summary = "Task execution failed during PR flow."
    else:
        summary = "Task execution failed."
    if run_err:
        summary += f" Error: {run_err}"
    if scope_warnings:
        summary += " Warnings: " + " | ".join(scope_warnings)
    if conflict_review_path is not None:
        summary += f" Conflict review: {conflict_review_path}"

    finished_at = now_iso()
    report_verify_commands = verify_commands if pr and verify_mode != "off" else []
    report_path = write_task_report(
        paths=paths,
        task=task,
        result=(
            "prepared"
            if success and prepared_integration is not None
            else ("success" if success else "failure")
        ),
        result_kind=result_kind,
        summary=summary,
        started_at=started_at,
        finished_at=finished_at,
        changed_files=changed_files,
        verify_commands=report_verify_commands,
        patch_path=patch_path,
        budget_artifact_path=budget_artifact_path,
        execution_log_artifacts=exec_artifacts,
        verify_artifact_path=verify_path if verify_path.exists() else None,
        verify_summary=verify_summary,
        verify_payload=verify_payload,
        verify_command_source=verify_command_source,
        base_branch=pr_base_branch,
        task_branch=pr_task_branch,
        commit_hash=commit_hash,
        merge_commit_hash=merge_commit_hash,
        merge_result=merge_result,
        salvaged_nonzero_exit=bool(pr_nonzero_salvage_attempted and success),
        noop_reason=noop_reason,
        remote_lines=_remote_report_lines(remote_record),
        scope_amendments=scope_amendment_payloads,
        scope_amended_patterns=scope_amended_patterns,
    )
    persisted_capture = persist_execution_knowledge_capture(
        paths=paths,
        task=task,
        source="forge_exec",
        assistant_message=recording_surface.final_assistant_message,
        artifact_dir=(
            paths.execution_knowledge_capture_dir
            / _safe_task_file_component(task_id)
            / _safe_task_file_component(started_at)
        ),
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_path if verify_path.exists() else None,
        budget_artifact_path=budget_artifact_path,
        session_artifact_dir=exec_artifacts.session_artifact_dir,
    )
    if success and verification_unavailable_completion:
        # The work is kept, but "validated" would be a lie: nothing ran against it.
        mark_knowledge_capture_promotion_skipped(
            artifact_dir=persisted_capture.artifact_dir,
            reason="task completed without any authoritative verification",
        )
    elif success and prepared_integration is not None:
        mark_knowledge_capture_promotion_skipped(
            artifact_dir=persisted_capture.artifact_dir,
            reason="task is awaiting authoritative Host integration seal",
        )
    elif success:
        promote_validated_knowledge_capture(
            paths=paths,
            task=task,
            artifact_dir=persisted_capture.artifact_dir,
        )
    else:
        mark_knowledge_capture_promotion_skipped(
            artifact_dir=persisted_capture.artifact_dir,
            reason="task execution outcome was not accepted",
        )

    write_task_attempt_entry(
        paths=paths,
        task=task,
        source="forge_exec",
        result=(
            "prepared"
            if success and prepared_integration is not None
            else ("success" if success else "failure")
        ),
        summary=summary,
        changed_files=changed_files,
        verify_summary=verify_summary,
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_path if verify_path.exists() else None,
        budget_artifact_path=budget_artifact_path,
        session_artifact_dir=exec_artifacts.session_artifact_dir,
        acceptance_state=(
            "prepared"
            if success and prepared_integration is not None
            else ("accepted" if success else "rejected")
        ),
        extra_tags=[
            "execution",
            "sequential",
        ],
    )
    issue_paths = changed_files or list(allowed_scope)
    if runtime_artifacts_changed:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: protected .alysis mutation attempt",
            summary="Engineer execution attempted to modify protected .alysis runtime state.",
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["protected_runtime_mutation"],
        )
    elif verify_blocked:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: verification failed",
            summary=verify_summary or "Verification blocked task completion.",
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["verification_failure"],
        )
    elif review_blocked:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: review requested changes",
            summary=summary,
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["review_blocked"],
        )
    elif merge_conflict_detected:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: merge conflict remains unresolved",
            summary=summary,
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=["merge_conflict"],
        )
    elif not success:
        write_issue_entry(
            paths=paths,
            task=task,
            source="forge_exec",
            title=f"{task_id}: task execution failed",
            summary=summary,
            paths_in_scope=issue_paths,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_path if verify_path.exists() else None,
            budget_artifact_path=budget_artifact_path,
            session_artifact_dir=exec_artifacts.session_artifact_dir,
            tags=(
                ["execution_failure", "scope_violation"]
                if scope_violation_files
                else ["execution_failure"]
            ),
        )
    rebuild_knowledge_index(paths)

    if success and prepared_integration is not None:
        status = "prepared"
    elif success and verification_unavailable_completion:
        status = TASK_STATUS_COMPLETED_UNVERIFIED
    elif success:
        status = "done"
    elif merge_conflict_detected:
        status = "merge_conflict"
    elif verify_blocked:
        status = "verify_failed"
    elif review_blocked:
        status = "changes_requested"
    else:
        status = "failed"
    set_task_status(plan, task_id, status)
    save_plan(paths, plan)

    console.print(f"Task: {task_id} ({task.get('title', '')})")
    if success and prepared_integration is not None:
        console.print("Result: prepared (awaiting authoritative Host integration seal)")
    elif success and verification_unavailable_completion:
        console.print("Result: completed_unverified (kept, but nothing verified it)")
    else:
        console.print(f"Result: {'success' if success else 'failure'}")
    console.print(f"Report: {report_path}")
    console.print(f"Patch: {patch_path}")
    _print_usage_summary_from_logs(
        console=console,
        title=f"Usage Summary ({task_id})",
        log_paths=([exec_artifacts.log_copy_path] if exec_artifacts.log_retained else []),
    )
    if conflict_review_path is not None:
        console.print(f"Conflict Review: {conflict_review_path}")

    task_payload: dict[str, Any] = {
        "task_id": task_id,
        "title": str(task.get("title", "")),
        "status": status,
        "source": "exec",
        "success": bool(success and prepared_integration is None),
        "completion_ready": bool(success and prepared_integration is not None),
        "report": os.fspath(report_path),
        "patch": os.fspath(patch_path),
        "verify_summary": verify_summary,
        "verify_blocked": bool(verify_blocked),
        "verification_unavailable": bool(verification_unavailable_completion),
        "verification_repair": (
            verification_repair_payload if verification_repair_payload else None
        ),
        "review_blocked": bool(review_blocked),
        "merge_conflict": bool(merge_conflict_detected),
        "merge_result": merge_result,
        "branch": pr_task_branch,
        "base_branch": pr_base_branch,
        "base_head": strict_base_head if strict_verification_executor is not None else None,
        "commit": commit_hash,
        "merge_commit": merge_commit_hash,
        "merge_first_parent": merge_first_parent,
        "merge_second_parent": merge_second_parent,
        "base_ref_after_merge": base_ref_after_merge,
        "scope_warnings": list(scope_warnings),
        "scope_amendments": list(scope_amendment_payloads),
        "scope_amended_patterns": list(scope_amended_patterns),
    }
    if conflict_review_path is not None:
        task_payload["conflict_review"] = os.fspath(conflict_review_path)
    if prepared_integration is None:
        events.emit(
            EVENT_TASK_COMPLETED if success else EVENT_TASK_FAILED,
            task_payload,
        )
    retained_log_paths = execution_retained_log_paths
    retained_artifacts = []
    artifact_binding_failures: list[str] = []
    for artifact_path in (
        report_path,
        patch_path,
        verify_path if verify_path.exists() else None,
        *retained_log_paths,
    ):
        if artifact_path is None:
            continue
        try:
            retained_artifacts.append(bind_retained_artifact(artifact_path))
        except OSError:
            artifact_binding_failures.append(artifact_path.name)
    for verification_result in structured_verification:
        if verification_result.artifact not in retained_artifacts:
            retained_artifacts.append(verification_result.artifact)

    if prepared_integration is not None and success:
        task_elapsed_seconds = prepared_integration.facts.prepared_elapsed_ms / 1000.0
        task_timed_out = False
    elif integration_commit_proof is not None and success:
        task_elapsed_seconds = integration_commit_proof.integration_committed_elapsed_ms / 1000.0
        task_timed_out = False
    elif execution_deadline is not None:
        task_elapsed_seconds = max(
            0.0,
            execution_deadline.elapsed_seconds() - task_monotonic_started,
        )
        task_timed_out = execution_deadline.is_exhausted()
    else:
        task_elapsed_seconds = max(0.0, time.monotonic() - task_monotonic_started)
        task_timed_out = False

    completion_issues: list[str] = []
    completion_candidate = bool(success and prepared_integration is not None)
    outcome_success = bool(success and prepared_integration is None)
    canonical_status = canonical_task_status(status)
    if outcome_success != (canonical_status in SUCCESSFUL_TERMINAL_STATUSES):
        completion_issues.append("task success does not match terminal status")
    if (outcome_success or completion_candidate) and merge_conflict_detected:
        completion_issues.append("successful task reports a merge conflict")
    if (
        (outcome_success or completion_candidate)
        and verify_mode == "strict"
        and (verify_commands or strict_verification_executor is not None)
    ):
        if strict_verification_executor is not None:
            if not structured_verification or not all(
                result.passed for result in structured_verification
            ):
                completion_issues.append("successful task lacks passing exact verification")
            if merge_first_parent != strict_base_head:
                completion_issues.append(
                    "successful Host task merge first parent differs from approved base HEAD"
                )
            if merge_second_parent != commit_hash:
                completion_issues.append(
                    "successful Host task merge second parent differs from task commit"
                )
            if completion_candidate:
                if prepared_integration is None:
                    completion_issues.append(
                        "completion-ready Host task lacks a prepared integration"
                    )
                else:
                    prepared = prepared_integration.facts
                    if (
                        prepared.native_run_id != paths.run_id
                        or prepared.task_id != task_id
                        or prepared.base_branch != pr_base_branch
                        or prepared.approved_base_head != strict_base_head
                        or prepared.task_branch != pr_task_branch
                        or prepared.task_commit != commit_hash
                        or prepared.prepared_merge_commit != merge_commit_hash
                        or prepared.prepared_elapsed_ms < 0
                    ):
                        completion_issues.append(
                            "completion-ready Host integration facts contradict execution"
                        )
                    if base_ref_after_merge != strict_base_head:
                        completion_issues.append(
                            "completion-ready Host base ref changed before commit"
                        )
                    if prepared_integration.state != "PREPARED":
                        completion_issues.append(
                            "completion-ready Host integration is not prepared"
                        )
                if integration_commit_proof is not None:
                    completion_issues.append(
                        "completion-ready Host task already has an integration proof"
                    )
            else:
                if base_ref_after_merge != merge_commit_hash:
                    completion_issues.append(
                        "successful Host task base ref differs from merge commit"
                    )
                if integration_commit_proof is None:
                    completion_issues.append(
                        "successful Host task lacks an integration commit proof"
                    )
                elif integration_commit_proof.base_branch != pr_base_branch:
                    completion_issues.append(
                        "successful Host task proof base branch differs from outcome"
                    )
                elif integration_commit_proof.approved_base_head != strict_base_head:
                    completion_issues.append(
                        "successful Host task proof base HEAD differs from approval"
                    )
                elif integration_commit_proof.task_commit != commit_hash:
                    completion_issues.append(
                        "successful Host task proof task commit differs from outcome"
                    )
                elif integration_commit_proof.merge_commit != merge_commit_hash:
                    completion_issues.append(
                        "successful Host task proof merge commit differs from outcome"
                    )
                elif integration_commit_proof.integration_committed_elapsed_ms < 0:
                    completion_issues.append("successful Host task proof timing is invalid")
        elif verify_result is None:
            completion_issues.append("strict verification result is missing")
        elif not bool(getattr(verify_result, "all_passed", False)):
            completion_issues.append("successful task has failing strict verification")
    if artifact_binding_failures:
        completion_issues.append(
            "retained artifacts could not be bound: " + ", ".join(artifact_binding_failures)
        )
    if task_timed_out and (outcome_success or completion_candidate):
        completion_issues.append("timed-out task reports success")

    completion_ready = completion_candidate and not completion_issues
    merged = bool(outcome_success and merge_commit_hash is not None)
    outcome_finished_at = now_iso()
    branch_retained = bool(
        pr_task_branch
        and (
            not merged
            or keep_branch
            or (merge_result is not None and "branch kept" in merge_result)
            or (merge_result is not None and "delete warning" in merge_result)
        )
    )
    # A task that ran and was not accepted is a genuine execution failure, so exit 1
    # stays. Exit 2 is reserved for the command itself failing, which leaves this
    # function as ForgeTaskExecutionError rather than as an outcome.
    return TaskExecutionOutcome(
        task_id=task_id,
        title=str(task.get("title", "")),
        status=status,
        success=outcome_success,
        exit_code=EXIT_OK if outcome_success else EXIT_NOT_ACCEPTED,
        summary=summary,
        report_path=report_path,
        patch_path=patch_path,
        merge_conflict=bool(merge_conflict_detected),
        conflict_review_path=conflict_review_path,
        payload=task_payload,
        verification=structured_verification,
        artifacts=tuple(retained_artifacts),
        retained_log_paths=retained_log_paths,
        usage=execution_usage,
        timing=ExecutionTiming(
            started_at=started_at,
            finished_at=outcome_finished_at,
            elapsed_ms=max(0, round(task_elapsed_seconds * 1000)),
            timed_out=task_timed_out,
        ),
        branch_facts=BranchFacts(
            task_branch=pr_task_branch,
            base_branch=pr_base_branch,
            base_head=(strict_base_head if strict_verification_executor is not None else None),
            commit=commit_hash,
            merge_commit=merge_commit_hash,
            merge_first_parent=merge_first_parent,
            merge_second_parent=merge_second_parent,
            base_ref_after_merge=base_ref_after_merge,
            merge_result=merge_result,
            merged=merged,
            branch_retained=branch_retained,
        ),
        integration_commit_proof=(integration_commit_proof if success else None),
        completion_ready=completion_ready,
        prepared_integration=prepared_integration,
        completion_consistent=not completion_issues,
        completion_issues=tuple(completion_issues),
    )


def forge_exec(
    task_id: str = typer.Argument(..., help="Task id from plan.json (for example T01)."),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    pr: bool = typer.Option(
        False,
        "--pr/--no-pr",
        help="Run task in PR-like git flow (branch, commit, patch, merge).",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run automated PR review gate before merge (requires --pr).",
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch for --pr mode (defaults to current branch).",
    ),
    keep_branch: bool = typer.Option(
        False,
        "--keep-branch",
        help="Keep task branch after successful merge in --pr mode.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Verification policy for PR flow: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    # Appended after ``yes`` on purpose: `forge_exec_impl` forwards these positionally,
    # so a new parameter inserted mid-list would silently shift every later argument.
    verify_repair_attempts: int | None = typer.Option(
        None,
        "--verify-repair-attempts",
        help=(
            "How many times a failing strict verification is fed back to the agent "
            "to repair before the task fails (default 2; 0 disables)."
        ),
    ),
    auto_resolve_conflicts: bool = typer.Option(
        False,
        "--auto-resolve-conflicts",
        help=(
            "On merge conflict, start a resolver agent in a dedicated worktree instead "
            "of stopping with a conflict report. Off by default: the sequential path "
            "reports the conflict and tells you how to land it."
        ),
    ),
    events: Any = None,
) -> None:
    events = _events_or_null(events)
    console = _console()
    cfg = load_config()
    effective = clone_cfg(cfg)
    current_ctx = get_current_context(silent=True)
    max_steps_source = (
        current_ctx.get_parameter_source("max_steps") if current_ctx is not None else None
    )
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        max_steps_provided = (
            max_steps_source is not None and max_steps_source is not ParameterSource.DEFAULT
        )
    if base_url is not None:
        apply_runtime_base_url_override(effective, base_url)
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    if stream is not None:
        effective.stream = stream
    if max_steps is not None:
        effective.max_steps = max_steps
    effective_mode = (mode.value if mode else effective.default_mode) or "review"
    scope_mode = "strict"
    verify_mode = "warn"
    verify_commands: list[str] = []
    verify_command_source: str | None = None
    run_cfg = clone_cfg(effective)
    # Called as a plain function (not through Typer) this arrives as an OptionInfo,
    # which is neither an int nor None -- normalize before it reaches the budget.
    repair_attempts_override = (
        verify_repair_attempts
        if isinstance(verify_repair_attempts, int) and not isinstance(verify_repair_attempts, bool)
        else None
    )
    verify_repair_budget = resolve_repair_attempt_budget(repair_attempts_override)

    try:
        scope_mode = _normalize_scope_mode(scope)
        verify_mode = _normalize_verify_mode(verify)
        api_key_override = _resolve_api_key_override(
            api_key=api_key,
            api_key_env=api_key_env,
            api_key_stdin=api_key_stdin,
        )
        paths = load_current_run_paths(path)
        events.set_run_id(paths.run_id)
        plan = load_plan(paths)
        run_cfg.model = resolve_model_for_role(
            cfg=effective,
            role=ROLE_CODING,
            plan=plan,
            prefer_context="forge",
        )
    except (ConfigError, ForgeError) as e:
        raise _exec_error_exit(console, events, str(e), task_id=task_id) from e

    task = find_task(plan, task_id)
    if task is None:
        message = f"Task not found: {task_id}"
        events.emit(
            EVENT_PLAN_INVALID,
            {"reason": message, "source": "task_lookup", "task_id": task_id},
        )
        raise _exec_error_exit(console, events, message, task_id=task_id)
    task_status = canonical_task_status(str(task.get("status") or ""))
    if task_status in {"superseded", "invalidated"}:
        message = (
            f"Task is non-executable obsolete work ({task_status}): {task_id}. "
            "Use an active planned replacement task instead."
        )
        events.emit(
            EVENT_PLAN_INVALID,
            {
                "reason": message,
                "source": "task_status",
                "task_id": task_id,
                "status": task_status,
            },
        )
        raise _exec_error_exit(console, events, message, task_id=task_id)
    if verify_mode != "off":
        verify_selection = resolve_authoritative_task_verify_command_selection(
            cfg=effective,
            verify_cmd=verify_cmd,
            task=task,
            root=paths.root,
            plan_requirements=[
                str(item).strip() for item in (plan.get("requirements") or []) if str(item).strip()
            ],
        )
        verify_commands = list(verify_selection.commands)
        verify_command_source = verify_selection.source
        run_cfg.verify_commands = list(verify_commands)

    blockers = _task_dependency_blockers(plan, task)
    if blockers:
        message = "Dependencies are not done: " + ", ".join(blockers)
        events.emit(
            EVENT_PLAN_INVALID,
            {
                "reason": message,
                "source": "dependencies",
                "task_id": task_id,
                "blockers": list(blockers),
            },
        )
        raise _exec_error_exit(console, events, message, task_id=task_id)
    if review and not pr:
        raise _exec_error_exit(
            console,
            events,
            "--review requires --pr.",
            task_id=task_id,
            kind="usage_error",
        )

    try:
        run_mutation_guard = acquire_swarm_mutation_guard(
            paths,
            mode=f"forge_exec:{task_id}",
            on_wait=lambda info: _print_forge_lock_wait_notice(console, info),
        )
    except ForgeError as e:
        raise _exec_error_exit(console, events, str(e), task_id=task_id) from e

    try:
        if bool(getattr(run_mutation_guard, "acquired_after_wait", False)):
            plan = load_plan(paths)
            task = find_task(plan, task_id)
            if task is None:
                message = (
                    "Queued Forge exec revalidated the current plan "
                    f"and task no longer exists: {task_id}"
                )
                events.emit(
                    EVENT_PLAN_INVALID,
                    {"reason": message, "source": "task_lookup", "task_id": task_id},
                )
                raise _exec_error_exit(console, events, message, task_id=task_id)
            task_status = canonical_task_status(str(task.get("status") or ""))
            if task_status in {"done", "superseded", "invalidated"}:
                message = (
                    "Queued Forge exec revalidated the current plan "
                    f"and task is no longer executable ({task_status}): {task_id}."
                )
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": message,
                        "source": "task_status",
                        "task_id": task_id,
                        "status": task_status,
                    },
                )
                raise _exec_error_exit(console, events, message, task_id=task_id)
            run_cfg.model = resolve_model_for_role(
                cfg=effective,
                role=ROLE_CODING,
                plan=plan,
                prefer_context="forge",
            )
            verify_commands = []
            verify_command_source = None
            if verify_mode != "off":
                verify_selection = resolve_authoritative_task_verify_command_selection(
                    cfg=effective,
                    verify_cmd=verify_cmd,
                    task=task,
                    root=paths.root,
                    plan_requirements=[
                        str(item).strip()
                        for item in (plan.get("requirements") or [])
                        if str(item).strip()
                    ],
                )
                verify_commands = list(verify_selection.commands)
                verify_command_source = verify_selection.source
                run_cfg.verify_commands = list(verify_commands)
            blockers = _task_dependency_blockers(plan, task)
            if blockers:
                message = "Dependencies are not done: " + ", ".join(blockers)
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": message,
                        "source": "dependencies",
                        "task_id": task_id,
                        "blockers": list(blockers),
                    },
                )
                raise _exec_error_exit(console, events, message, task_id=task_id)

        _mark_run_status(
            paths,
            RUN_STATUS_RUNNING,
            reason=f"forge exec started for {task_id}",
            plan=plan,
            mode=f"forge_exec:{task_id}",
        )
        outcome = execute_forge_task(
            console=console,
            events=events,
            paths=paths,
            plan=plan,
            task=task,
            task_id=task_id,
            effective=effective,
            run_cfg=run_cfg,
            effective_mode=effective_mode,
            scope_mode=scope_mode,
            verify_mode=verify_mode,
            verify_commands=verify_commands,
            verify_command_source=verify_command_source,
            verify_repair_budget=verify_repair_budget,
            api_key_override=api_key_override,
            yes=yes,
            no_log=no_log,
            max_steps_provided=max_steps_provided,
            pr=pr,
            review=review,
            base_branch=base_branch,
            keep_branch=keep_branch,
            auto_resolve_conflicts=auto_resolve_conflicts is True,
        )
        terminal_status, terminal_reason = _terminal_run_status_for(
            plan=plan,
            any_failure=not outcome.success,
        )
        _mark_run_status(paths, terminal_status, reason=terminal_reason)
    except ForgeTaskExecutionError as e:
        raise _exec_error_exit(
            console,
            events,
            str(e),
            task_id=e.task_id or task_id,
            kind=e.kind,
        ) from e
    finally:
        _mark_run_interrupted_if_still_running(
            paths,
            reason=f"forge exec for {task_id} exited without recording an outcome",
        )
        run_mutation_guard.release()

    events.run_completed(
        ok=outcome.success,
        exit_code=outcome.exit_code,
        data={"task": outcome.payload},
    )
    raise typer.Exit(code=outcome.exit_code)


def forge_run(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of requiring a narrower project path.",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated task ids to execute (dependencies are still enforced).",
    ),
    max_tasks: int | None = typer.Option(
        None,
        "--max-tasks",
        min=1,
        help="Stop after this many tasks have been executed.",
    ),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        min=1,
        help="Skip tasks that already reached this many recorded attempts.",
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Include tasks currently marked failed.",
    ),
    retry_changes_requested: bool = typer.Option(
        False,
        "--retry-changes-requested",
        help="Include tasks currently marked changes_requested.",
    ),
    keep_going: bool = typer.Option(
        False,
        "--keep-going",
        help=(
            "Continue with the next independent task after a failure instead of stopping. "
            "Default is to stop, because a failed task usually leaves later tasks unsound."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the execution order and exit without running anything.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Per-task verification policy: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    verify_repair_attempts: int | None = typer.Option(
        None,
        "--verify-repair-attempts",
        help=(
            "How many times a failing strict verification is fed back to the agent "
            "to repair before the task fails (default 2; 0 disables)."
        ),
    ),
    pr: bool = typer.Option(
        True,
        "--pr/--no-pr",
        help=(
            "Run each task as a branch/commit/verify/merge cycle in the main checkout "
            "(default). --no-pr leaves the changes uncommitted and runs no verification "
            "gate, so use it only where git flow is unavailable."
        ),
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run the automated review gate before merging each task (requires --pr).",
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch for --pr mode (defaults to the current branch).",
    ),
    keep_branch: bool = typer.Option(
        False,
        "--keep-branch",
        help="Keep each task branch after a successful merge.",
    ),
    auto_resolve_conflicts: bool = typer.Option(
        False,
        "--auto-resolve-conflicts",
        help=(
            "On merge conflict, start a resolver agent in a dedicated worktree instead of "
            "stopping with a conflict report. Off by default."
        ),
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    cli_ctx: Any = None,
    events: Any = None,
) -> None:
    """Execute every ready task sequentially, in dependency order, in this checkout.

    No worktrees, no parallelism, no batch integration gate: one task at a time
    through :func:`execute_forge_task`, the same core `forge exec` uses. The run
    stops at the first failure unless ``--keep-going`` says otherwise.
    """
    events = _events_or_null(events)
    console = _console()
    cfg = load_config()
    effective = clone_cfg(cfg)
    # The public Typer command forwards its own context explicitly. Prefer it
    # over Click's ambient lookup: this implementation is called through a
    # delegation layer, where the ambient context may be the parent ``forge``
    # group and cannot report whether run-specific options were supplied.
    current_ctx = cli_ctx if cli_ctx is not None else get_current_context(silent=True)
    max_steps_provided = max_steps is not None
    if current_ctx is not None:
        source = current_ctx.get_parameter_source("max_steps")
        # Typer may provide its vendored Click enum here, so compare the stable
        # semantic name rather than enum identity across package boundaries.
        max_steps_provided = (
            source is not None and getattr(source, "name", None) != ParameterSource.DEFAULT.name
        )
    pr_explicit = False
    if current_ctx is not None:
        pr_source = current_ctx.get_parameter_source("pr")
        pr_explicit = (
            pr_source is not None
            and getattr(pr_source, "name", None) != ParameterSource.DEFAULT.name
        )

    if base_url is not None:
        apply_runtime_base_url_override(effective, base_url)
    if model is not None:
        effective.model = model
    if temperature is not None:
        _apply_temperature_override(effective, temperature)
    if stream is not None:
        effective.stream = stream
    if max_steps is not None:
        effective.max_steps = max_steps
    effective_mode = (mode.value if mode else effective.default_mode) or "review"

    # Called as a plain function these arrive as truthy typer OptionInfo objects,
    # so every boolean is normalized before it can silently flip a policy.
    pr_requested = pr is True
    review_requested = review is True
    keep_branch_requested = keep_branch is True
    keep_going_requested = keep_going is True
    dry_run_requested = dry_run is True
    retry_failed_requested = retry_failed is True
    retry_changes_requested_requested = retry_changes_requested is True
    auto_resolve_requested = auto_resolve_conflicts is True
    repair_attempts_override = (
        verify_repair_attempts
        if isinstance(verify_repair_attempts, int) and not isinstance(verify_repair_attempts, bool)
        else None
    )
    verify_repair_budget = resolve_repair_attempt_budget(repair_attempts_override)
    only_ids = _parse_only_task_ids(only)
    task_limit = (
        max_tasks if isinstance(max_tasks, int) and not isinstance(max_tasks, bool) else None
    )
    attempt_limit = (
        max_attempts
        if isinstance(max_attempts, int) and not isinstance(max_attempts, bool)
        else None
    )

    started_at = now_iso()
    paths: Any = None
    sequential_result: Any = None

    try:
        scope_mode = _normalize_scope_mode(scope)
        verify_mode = _normalize_verify_mode(verify)
        if review_requested and not pr_requested:
            raise ForgeError("--review requires --pr.")
        api_key_override = _resolve_api_key_override(
            api_key=api_key,
            api_key_env=api_key_env,
            api_key_stdin=api_key_stdin,
        )
        binding = resolve_workspace_binding(
            path,
            create_if_missing=False,
            allow_broad_workspace=allow_broad_workspace,
            source=_path_binding_source(cli_ctx, path),
        )
        ensure_workspace_policy(
            binding,
            action=WorkspaceAction.FORGE_RUN,
            allow_broad_workspace=allow_broad_workspace,
        )
        try:
            paths = load_current_run_paths(binding.workspace_context.focus_path)
        except ForgeError as e:
            if "current_run.json" in str(e):
                raise _missing_swarm_run_error(binding=binding) from e
            raise
        events.set_run_id(paths.run_id)
        plan = load_plan(paths)

        # PR flow is the default because it is what makes per-task verification and a
        # per-task resume point possible. A workspace without a git HEAD cannot do it,
        # so an unstated default degrades loudly instead of failing; an explicit --pr
        # still fails, because silently ignoring a flag the user typed is worse.
        git_flow_available = bool(paths.has_head_commit)
        if pr_requested and not git_flow_available:
            # --review only exists inside git flow, so degrading would drop it too.
            # Both count as "the user typed it", and neither gets dropped silently.
            explicit_flag = "--pr" if pr_explicit else ("--review" if review_requested else None)
            if explicit_flag is not None:
                raise ForgeError(
                    f"{explicit_flag} needs a git repository with at least one commit; this "
                    f"workspace ({os.fspath(paths.root)}) has none. Commit once, or drop "
                    f"{explicit_flag}."
                )
            pr_requested = False
            review_requested = False
            console.print(
                "[yellow]No git HEAD in this workspace:[/yellow] running without git flow. "
                "Tasks execute in place, nothing is committed, and per-task verification "
                "does not run."
            )
            events.emit(
                EVENT_VERIFICATION_UNAVAILABLE,
                {
                    "scope": "run",
                    "policy": verify_mode,
                    "reason": (
                        "sequential run degraded to --no-pr: workspace has no git HEAD, so no "
                        "per-task verification gate runs"
                    ),
                    "blocking": False,
                },
            )

        # The base branch is pinned once for the whole run rather than re-derived per
        # task from the current branch. A failed task leaves the checkout on its own
        # branch, so re-deriving would make the next task branch off rejected work --
        # which is exactly what --keep-going would otherwise do.
        resolved_base_branch: str | None = (
            base_branch.strip() if isinstance(base_branch, str) and base_branch.strip() else None
        )
        if pr_requested and resolved_base_branch is None:
            try:
                resolved_base_branch = (current_branch(paths.root) or "").strip() or None
            except GitOpsError as e:
                raise ForgeError(f"could not determine the base branch for git flow: {e}") from e
            if resolved_base_branch is None:
                raise ForgeError(
                    "could not determine the base branch for git flow; pass --base-branch."
                )

        validated_plan: dict[str, Any] = plan

        def _reconcile_and_validate_sequential_plan(
            run_paths: Any,
            candidate_plan: dict[str, Any],
        ) -> dict[str, Any]:
            nonlocal validated_plan
            reconciliation_result, _ = _reconcile_plan_for_paths(
                paths=run_paths,
                plan=candidate_plan,
                refresh_if_stale=True,
            )
            if reconciliation_result.changed:
                save_plan(run_paths, candidate_plan)
            if reconciliation_result.warnings:
                console.print("[yellow]Plan reconciliation warnings:[/yellow]")
                for warning in reconciliation_result.warnings:
                    console.print(f"- {warning}")
            validation_warnings = _validate_forge_plan_for_paths(run_paths, candidate_plan)
            _write_plan_validation_artifact(
                paths=run_paths,
                reconciliation_result=reconciliation_result,
                validation_warnings=validation_warnings,
            )
            repair_payload = plan_repair_event_payload(candidate_plan)
            readiness_message = _forge_no_execution_ready_tasks_message(candidate_plan)
            if readiness_message is not None:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": readiness_message,
                        "source": "execution_readiness",
                        "warnings": list(validation_warnings),
                        **repair_payload,
                    },
                )
                raise ForgeError(readiness_message)
            try:
                raise_for_execution_ready_plan(
                    candidate_plan,
                    retry_failed=retry_failed_requested,
                    retry_changes_requested=retry_changes_requested_requested,
                    retry_merge_conflicts=False,
                    only=only,
                )
            except PlannerFailedError as e:
                events.emit(
                    EVENT_PLAN_INVALID,
                    {
                        "reason": str(e),
                        "source": "plan_validation",
                        "failure_category": getattr(e, "failure_category", None),
                        **repair_payload,
                    },
                )
                err = ForgeError(str(e))
                err.failure_category = e.failure_category  # type: ignore[attr-defined]
                raise err from e
            validated_plan = candidate_plan
            return candidate_plan

        def _execute_sequential_task(request: SequentialTaskRequest) -> TaskExecutionOutcome:
            task_plan = request.plan
            task = request.task
            task_id = request.task_id
            run_cfg = clone_cfg(effective)
            run_cfg.model = resolve_model_for_role(
                cfg=effective,
                role=ROLE_CODING,
                plan=task_plan,
                prefer_context="forge",
            )
            task_verify_commands: list[str] = []
            task_verify_command_source: str | None = None
            if verify_mode != "off":
                selection = resolve_authoritative_task_verify_command_selection(
                    cfg=effective,
                    verify_cmd=verify_cmd,
                    task=task,
                    root=request.native_paths.root,
                    plan_requirements=[
                        str(item).strip()
                        for item in (task_plan.get("requirements") or [])
                        if str(item).strip()
                    ],
                )
                task_verify_commands = list(selection.commands)
                task_verify_command_source = selection.source
                run_cfg.verify_commands = list(task_verify_commands)
            return execute_forge_task(
                console=console,
                events=events,
                paths=request.native_paths,
                plan=task_plan,
                task=task,
                task_id=task_id,
                effective=effective,
                run_cfg=run_cfg,
                effective_mode=effective_mode,
                scope_mode=scope_mode,
                verify_mode=verify_mode,
                verify_commands=task_verify_commands,
                verify_command_source=task_verify_command_source,
                verify_repair_budget=verify_repair_budget,
                api_key_override=api_key_override,
                yes=yes is True,
                no_log=no_log is True,
                max_steps_provided=max_steps_provided,
                pr=pr_requested,
                review=review_requested,
                base_branch=resolved_base_branch,
                keep_branch=keep_branch_requested,
                auto_resolve_conflicts=auto_resolve_requested,
                execution_deadline=request.execution_deadline,
            )

        projected_order: tuple[str, ...] = ()

        def _observe_sequential(progress: SequentialRunProgress) -> None:
            nonlocal projected_order
            if progress.kind == "run_started":
                projected_order = progress.projected_order
                console.rule("[bold]forge run[/bold]")
                console.print(f"Run ID: {paths.run_id}")
                console.print(f"Workspace: {os.fspath(paths.root)}")
                console.print(
                    f"Mode: sequential, in-checkout (no worktrees) | "
                    f"git flow: {'on' if pr_requested else 'off'} | verify: {verify_mode} | "
                    f"scope: {scope_mode}"
                )
                if pr_requested and resolved_base_branch:
                    console.print(f"Base branch: {resolved_base_branch}")
                _print_sequential_order(console, validated_plan, list(projected_order))
            elif progress.kind == "task_started" and progress.task_id is not None:
                title = next(
                    (
                        str(item.get("title") or "").strip()
                        for item in _plan_task_dicts(validated_plan)
                        if str(item.get("id") or "").strip() == progress.task_id
                    ),
                    "",
                )
                console.rule(f"[bold]{progress.position}. {progress.task_id}[/bold] {title}")

        sequential_result = run_sequential_forge(
            SequentialRunRequest(
                native_run_id=paths.run_id,
                native_paths=paths,
                retry_failed=retry_failed_requested,
                retry_changes_requested=retry_changes_requested_requested,
                max_attempts=attempt_limit,
                only_ids=frozenset(only_ids) if only_ids is not None else None,
                max_tasks=task_limit,
                keep_going=keep_going_requested,
                git_flow=pr_requested,
                verify_mode=verify_mode,
                scope_mode=scope_mode,
                started_at=started_at,
                dry_run=dry_run_requested,
            ),
            dependencies=SequentialRunDependencies(
                load_plan=lambda run_paths: load_plan(run_paths),
                save_plan=lambda run_paths, value: save_plan(run_paths, value),
                ensure_execution_dirs=lambda run_paths: ensure_execution_dirs(run_paths),
                acquire_mutation_guard=lambda run_paths: acquire_swarm_mutation_guard(
                    run_paths,
                    mode="forge_run:cli",
                    on_wait=lambda info: _print_forge_lock_wait_notice(console, info),
                ),
                reconcile_and_validate=_reconcile_and_validate_sequential_plan,
                execute_task=_execute_sequential_task,
                mark_run_status=lambda run_paths, status, reason, value: _mark_run_status(
                    run_paths,
                    status,
                    reason=reason,
                    plan=value,
                    mode="forge_run:cli" if status == RUN_STATUS_RUNNING else None,
                ),
                mark_interrupted_if_running=lambda run_paths, reason: (
                    _mark_run_interrupted_if_still_running(run_paths, reason=reason)
                ),
                now_iso=now_iso,
            ),
            observer=_observe_sequential,
        )
        if sequential_result.stopped_reason == "dry_run":
            data = {
                "dry_run": True,
                "order": list(projected_order),
                "task_count": len(projected_order),
            }
            events.run_completed(ok=True, exit_code=EXIT_OK, data=data)
            raise typer.Exit(code=EXIT_OK)
        if sequential_result.exit_code == EXIT_ERROR:
            message = sequential_result.error_message or "sequential execution failed"
            console.print(f"[red]Forge error:[/red] {message}")
            events.error(
                message=message,
                kind=sequential_result.error_kind or "forge_error",
                exit_code=EXIT_ERROR,
            )
            raise typer.Exit(code=EXIT_ERROR)
    except (ConfigError, ForgeError, GitOpsError, WorkspaceBindingError) as e:
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e),
            kind="forge_error",
            exit_code=EXIT_ERROR,
            data={"failure_category": getattr(e, "failure_category", None)},
        )
        raise typer.Exit(code=EXIT_ERROR) from e
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001
        # An unexpected exception is the command failing, not "tasks were rejected".
        console.print(f"[red]Forge error:[/red] {e}")
        events.error(
            message=str(e) or e.__class__.__name__,
            kind="exception",
            exit_code=EXIT_ERROR,
            data={"exception_type": e.__class__.__name__},
        )
        raise typer.Exit(code=EXIT_ERROR) from e

    # Engine ``success`` means the whole plan reached accepted completion; CLI
    # exit 0 deliberately remains valid for scoped partial runs such as --only
    # and --max-tasks, so terminal CLI behavior follows the returned exit code.
    exit_code = int(sequential_result.exit_code)
    summary_payload = dict(sequential_result.summary_payload)
    _print_sequential_summary(console, summary_payload)
    _print_usage_summary_from_logs(
        console=console,
        title="Sequential Run Usage Summary",
        log_paths=sorted(paths.execution_logs_dir.glob("*.jsonl")),
    )
    events.run_completed(
        ok=exit_code == EXIT_OK, exit_code=exit_code, data={"outcome": summary_payload}
    )
    raise typer.Exit(code=exit_code)


def forge_plan_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_plan(*args, **kwargs)


def forge_swarm_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_swarm(*args, **kwargs)


def forge_exec_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_exec(*args, **kwargs)


def forge_run_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return forge_run(*args, **kwargs)


def _host_authored_task_instruction(task: dict[str, Any]) -> str:
    """Render only admitted task prose for the agent's semantic user turn.

    The full execution Context Pack contains trusted runner metadata (including
    backtick-delimited budgets) that must remain visible to the model, but that
    metadata is not authored task intent and therefore must not feed the turn's
    acceptance/expectation extractor.  Host Phase 0 supplies the full pack as a
    separate user-role context message and uses this narrow text as the actual
    turn instruction.
    """

    title = str(task.get("title") or "").strip()
    description = str(task.get("description") or "").strip()
    raw_acceptance = task.get("acceptance_criteria")
    acceptance = (
        [str(item or "").strip() for item in raw_acceptance]
        if isinstance(raw_acceptance, list)
        else []
    )
    acceptance = [item for item in acceptance if item]

    lines = ["Implement the admitted Forge task described below."]
    if title:
        lines.extend(["", "Title:", title])
    if description:
        lines.extend(["", "Description:", description])
    if acceptance:
        lines.extend(["", "Acceptance criteria:"])
        lines.extend(f"- {item}" for item in acceptance)
    return "\n".join(lines).strip()
