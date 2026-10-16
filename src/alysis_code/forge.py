from __future__ import annotations

import json
import logging
import os
import re
import shutil
import warnings
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from .atomic_io import atomic_write_json, atomic_write_text
from .branding import PROJECT_DIR_NAME, resolve_project_dir
from .error_text import sanitize_optional_error_summary
from .mcp.forge_scope import describe_task_mcp_scope, normalize_task_mcp_scope
from .project_data import ProjectDataError, project_artifact_path
from .repo_scan import (
    REPO_SCAN_SCHEMA_VERSION,
    RepoScanResult,
    render_repo_scan_markdown,
    render_repo_scan_summary_lines,
    scan_workspace,
)
from .run_state import (
    RUN_STATUS_APPROVED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_RUNNING,
    apply_run_status,
    plan_fingerprint,
    pointer_status,
    reconcile_status_after_crash,
)
from .runtime_artifacts import is_runtime_artifact_path
from .serialized_paths import safe_serialized_path
from .task_dependencies import infer_ordered_predecessor_dependency
from .task_readiness import (
    TASK_KIND_ANALYSIS_ONLY,
    has_runnable_local_file_scope,
    manual_task_scope_error_message,
    normalize_task_file_fields,
)
from .verify_gate import compact_verification_payload
from .workspace_binding import (
    WorkspaceAction,
    WorkspaceBinding,
    WorkspaceBindingError,
    WorkspaceRiskLevel,
    ensure_workspace_policy,
    resolve_workspace_binding,
)
from .workspace_context import (
    WorkspaceContext,
    WorkspaceContextError,
    resolve_workspace_context,
)

if TYPE_CHECKING:
    from .execution_shared import ExecutionLogArtifactsResult

MAX_TEXT_ASSET_BYTES = 200 * 1024
# 5 added the run lifecycle block: ``status`` (see :mod:`alysis_code.run_state`),
# ``status_updated_at``, ``status_history``, ``run_owner``, and ``plan_fingerprint``.
# Readers must keep accepting older pointers; they simply read as ``draft``.
CURRENT_RUN_POINTER_SCHEMA_VERSION = 5
PLAN_SCHEMA_VERSION = 2
LOGGER = logging.getLogger(__name__)


class ForgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunPaths:
    root: Path
    run_id: str
    runtime_dir: Path
    runs_dir: Path
    run_dir: Path
    plan_dir: Path
    plan_json_path: Path
    plan_md_path: Path
    decisions_path: Path
    risks_path: Path
    assets_dir: Path
    assets_text_dir: Path
    asset_store_dir: Path
    assets_index_path: Path
    assets_index_lock_path: Path
    assets_raw_dir: Path
    assets_extracted_dir: Path
    assets_comprehensions_dir: Path
    notes_dir: Path
    notes_path: Path
    planner_chat_path: Path
    planner_summary_path: Path
    plan_replans_dir: Path
    execution_dir: Path
    execution_reports_dir: Path
    execution_patches_dir: Path
    execution_logs_dir: Path
    execution_sessions_dir: Path
    execution_reviews_dir: Path
    execution_verify_dir: Path
    execution_context_dir: Path
    execution_budgets_dir: Path
    execution_asset_briefings_dir: Path
    execution_asset_usage_dir: Path
    execution_knowledge_capture_dir: Path
    execution_integration_dir: Path
    execution_integration_issues_path: Path
    knowledge_dir: Path
    knowledge_index_path: Path
    knowledge_task_attempts_dir: Path
    knowledge_issues_dir: Path
    knowledge_facts_dir: Path
    knowledge_decisions_dir: Path
    knowledge_selected_dir: Path
    plan_context_dir: Path | None = None
    workspace_context_json_path: Path | None = None
    workspace_summary_md_path: Path | None = None
    focus_path: Path | None = None
    focus_relpath: str = "."
    workspace_kind: str = "plain_dir"
    git_root: Path | None = None
    has_head_commit: bool = False
    greenfield: bool = False
    current_branch: str | None = None
    binding_requested_path: Path | None = None
    binding_source: str = "explicit_path"
    workspace_created_at_startup: bool = False
    binding_risk_level: str = WorkspaceRiskLevel.HEALTHY
    binding_risk_reasons: tuple[str, ...] = ()
    binding_broad_workspace_override_used: bool = False


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# Words for counter-based run ids: legendary smiths, bright stars, and smithing
# terms. Order is load-bearing — the word is a pure function of the counter, so
# reordering or removing entries renames future runs at existing counters.
RUN_NAME_WORDS: tuple[str, ...] = (
    "vulcan",
    "vega",
    "wayland",
    "sirius",
    "brigid",
    "altair",
    "sindri",
    "rigel",
    "brokkr",
    "deneb",
    "ilmarinen",
    "atlas",
    "svarog",
    "mira",
    "goibniu",
    "lyra",
    "kalvis",
    "orion",
    "seppo",
    "castor",
    "anvil",
    "pollux",
    "ember",
    "capella",
    "ingot",
    "antares",
    "crucible",
    "spica",
    "hearth",
    "mizar",
    "kiln",
    "alcor",
    "rivet",
    "procyon",
    "alloy",
    "canopus",
    "temper",
    "arcturus",
    "bellows",
    "regulus",
    "tongs",
    "thuban",
    "flux",
    "kochab",
    "forgefire",
    "alkaid",
    "quench",
    "hamal",
    "smelt",
    "ankaa",
    "brazier",
    "acrux",
    "mantle",
    "alphard",
    "gilder",
    "sabik",
    "solder",
    "shaula",
    "chisel",
    "nunki",
    "sledge",
    "markab",
    "patina",
    "polaris",
)

_RUN_ID_COUNTER_RE = re.compile(r"^(\d{3,6})-[a-z]+$")


def format_run_id(counter: int) -> str:
    word = RUN_NAME_WORDS[(counter - 1) % len(RUN_NAME_WORDS)]
    return f"{counter:03d}-{word}"


def _next_run_counter(runs_dir: Path) -> int:
    highest = 0
    try:
        entries = list(runs_dir.iterdir())
    except OSError:
        return 1
    for entry in entries:
        match = _RUN_ID_COUNTER_RE.match(entry.name)
        if match is not None:
            highest = max(highest, int(match.group(1)))
    return highest + 1


_RUN_COUNTER_MARK_NAME = ".run_counter"


def _legacy_run_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    import uuid

    return f"{ts}_{uuid.uuid4().hex[:8]}"


def _stored_run_counter(runs_dir: Path) -> int:
    try:
        raw = (runs_dir / _RUN_COUNTER_MARK_NAME).read_text(encoding="utf-8")
        return max(0, int(raw.strip()))
    except (OSError, ValueError):
        return 0


def _store_run_counter(runs_dir: Path, counter: int) -> None:
    value = max(counter, _stored_run_counter(runs_dir))
    try:
        atomic_write_text(runs_dir / _RUN_COUNTER_MARK_NAME, f"{value}\n")
    except OSError:
        # The mark only guards against re-minting ids of deleted runs; the
        # mkdir claim stays the uniqueness authority when it cannot be written.
        pass


def make_run_id(
    root: Path | None = None,
    *,
    runtime_dir_name: str = ".alysis",
) -> str:
    if root is None:
        # Without a workspace there is no run counter to scan; fall back to the
        # legacy globally-unique id so uniqueness never depends on shared state.
        return _legacy_run_id()
    resolved = root.expanduser().resolve()
    runs_dir = _runs_dir(resolved, runtime_dir_name=runtime_dir_name)
    counter = max(_next_run_counter(runs_dir), _stored_run_counter(runs_dir) + 1)
    if not resolved.is_dir():
        # Workspace existence is validated later by make_run_paths; never create
        # directories under a root that does not exist yet.
        return format_run_id(counter)
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # The runs dir cannot be materialized (e.g. a stray file occupies the
        # path). Hand out a legacy globally-unique id instead of an unclaimed
        # counter so uniqueness never degrades; downstream directory creation
        # surfaces the underlying error.
        return _legacy_run_id()
    while True:
        candidate = format_run_id(counter)
        try:
            # mkdir is the atomic claim: concurrent creators that computed the
            # same counter collide here and retry with the next one. The parent
            # was materialized above, so FileExistsError can only mean the
            # candidate itself is taken.
            (runs_dir / candidate).mkdir(exist_ok=False)
        except FileExistsError:
            counter += 1
            continue
        except OSError:
            return _legacy_run_id()
        _store_run_counter(runs_dir, counter)
        return candidate


def ensure_repo_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if not resolved.exists():
        raise ForgeError(f"Workspace path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ForgeError(f"Workspace path is not a directory: {resolved}")
    return resolved


def _ensure_under_root(root: Path, path: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as e:
        raise ForgeError(f"Path escapes workspace root: {path}") from e


def _runtime_dir(root: Path, *, runtime_dir_name: str = PROJECT_DIR_NAME) -> Path:
    if runtime_dir_name == PROJECT_DIR_NAME:
        # Canonical project storage is resolved without modifying the repository.
        return resolve_project_dir(root)
    return root / runtime_dir_name


def _runtime_state_dir(root: Path) -> Path:
    return _runtime_dir(root)


def _runs_dir(root: Path, *, runtime_dir_name: str = ".alysis") -> Path:
    return _runtime_dir(root, runtime_dir_name=runtime_dir_name) / "runs"


def current_run_pointer_path(root: Path) -> Path:
    return _runtime_state_dir(root) / "current_run.json"


def make_run_paths(
    *,
    root: Path,
    run_id: str,
    focus_path: Path | None = None,
    focus_relpath: str = ".",
    workspace_kind: str = "plain_dir",
    git_root: Path | None = None,
    has_head_commit: bool = False,
    greenfield: bool | None = None,
    current_branch: str | None = None,
    binding_requested_path: Path | None = None,
    binding_source: str = "explicit_path",
    workspace_created_at_startup: bool = False,
    binding_risk_level: str = WorkspaceRiskLevel.HEALTHY,
    binding_risk_reasons: tuple[str, ...] | list[str] = (),
    binding_broad_workspace_override_used: bool = False,
    runtime_dir_name: str = ".alysis",
) -> RunPaths:
    root = ensure_repo_root(root)
    resolved_focus_path = root if focus_path is None else focus_path.expanduser().resolve()
    _ensure_under_root(root, resolved_focus_path)
    normalized_focus_relpath = _normalize_stored_relpath(focus_relpath)
    resolved_git_root = git_root.expanduser().resolve() if git_root is not None else None
    resolved_requested_path = (
        resolved_focus_path
        if binding_requested_path is None
        else binding_requested_path.expanduser().resolve(strict=False)
    )
    _ensure_under_root(root, resolved_requested_path)
    runtime_dir = _runtime_dir(root, runtime_dir_name=runtime_dir_name)
    runs_dir = _runs_dir(root, runtime_dir_name=runtime_dir_name)
    run_dir = runs_dir / run_id
    plan_dir = run_dir / "plan"
    execution_dir = run_dir / "execution"
    plan_context_dir = plan_dir / "context"
    asset_store_dir = run_dir / "assets"
    resolved_greenfield = (
        bool(greenfield)
        if greenfield is not None
        else derive_greenfield_workspace(
            root=root,
            workspace_kind=workspace_kind or "plain_dir",
            has_head_commit=has_head_commit,
            workspace_created_at_startup=workspace_created_at_startup,
        )
    )
    return RunPaths(
        root=root,
        run_id=run_id,
        runtime_dir=runtime_dir,
        runs_dir=runs_dir,
        run_dir=run_dir,
        plan_dir=plan_dir,
        plan_json_path=plan_dir / "plan.json",
        plan_md_path=plan_dir / "PLAN.md",
        decisions_path=plan_dir / "DECISIONS.md",
        risks_path=plan_dir / "RISKS.md",
        assets_dir=plan_dir / "assets",
        assets_text_dir=plan_dir / "assets_text",
        asset_store_dir=asset_store_dir,
        assets_index_path=asset_store_dir / "index.json",
        assets_index_lock_path=asset_store_dir / "index.lock",
        assets_raw_dir=asset_store_dir / "raw",
        assets_extracted_dir=asset_store_dir / "extracted",
        assets_comprehensions_dir=asset_store_dir / "comprehensions",
        notes_dir=plan_dir / "notes",
        notes_path=plan_dir / "notes" / "user_notes.md",
        planner_chat_path=plan_dir / "notes" / "planner_chat.md",
        planner_summary_path=plan_dir / "notes" / "planner_summary.md",
        plan_replans_dir=plan_dir / "replans",
        execution_dir=execution_dir,
        execution_reports_dir=execution_dir / "reports",
        execution_patches_dir=execution_dir / "patches",
        execution_logs_dir=execution_dir / "logs",
        execution_sessions_dir=execution_dir / "sessions",
        execution_reviews_dir=execution_dir / "reviews",
        execution_verify_dir=execution_dir / "verify",
        execution_context_dir=execution_dir / "context",
        execution_budgets_dir=execution_dir / "budgets",
        execution_asset_briefings_dir=execution_dir / "asset_briefings",
        execution_asset_usage_dir=execution_dir / "asset_usage",
        execution_knowledge_capture_dir=execution_dir / "knowledge_capture",
        execution_integration_dir=execution_dir / "integration",
        execution_integration_issues_path=execution_dir / "integration" / "integration_issues.md",
        knowledge_dir=run_dir / "knowledge",
        knowledge_index_path=run_dir / "knowledge" / "index.json",
        knowledge_task_attempts_dir=run_dir / "knowledge" / "task_attempts",
        knowledge_issues_dir=run_dir / "knowledge" / "issues",
        knowledge_facts_dir=run_dir / "knowledge" / "facts",
        knowledge_decisions_dir=run_dir / "knowledge" / "decisions",
        knowledge_selected_dir=run_dir / "knowledge" / "selected",
        plan_context_dir=plan_context_dir,
        workspace_context_json_path=plan_context_dir / "workspace_context.json",
        workspace_summary_md_path=plan_context_dir / "workspace_summary.md",
        focus_path=resolved_focus_path,
        focus_relpath=normalized_focus_relpath,
        workspace_kind=workspace_kind or "plain_dir",
        git_root=resolved_git_root,
        has_head_commit=has_head_commit,
        greenfield=resolved_greenfield,
        current_branch=current_branch.strip() if current_branch else None,
        binding_requested_path=resolved_requested_path,
        binding_source=binding_source.strip() or "explicit_path",
        workspace_created_at_startup=workspace_created_at_startup,
        binding_risk_level=binding_risk_level.strip() or WorkspaceRiskLevel.HEALTHY,
        binding_risk_reasons=tuple(
            str(reason).strip() for reason in binding_risk_reasons if str(reason).strip()
        ),
        binding_broad_workspace_override_used=binding_broad_workspace_override_used,
    )


def derive_greenfield_workspace(
    *,
    root: Path,
    workspace_kind: str,
    has_head_commit: bool,
    workspace_created_at_startup: bool = False,
) -> bool:
    """Classify workspaces where declared plan paths are usually create targets."""
    normalized_kind = str(workspace_kind or "").strip()
    if normalized_kind == "plain_dir":
        return True
    if not has_head_commit:
        return True
    if workspace_created_at_startup:
        return True
    return _workspace_tree_is_empty_or_near_empty(root)


def _workspace_tree_is_empty_or_near_empty(root: Path) -> bool:
    try:
        root_resolved = root.resolve()
    except OSError:
        return False
    visited_dirs = 0
    for current_dir, dirnames, filenames in os.walk(root_resolved):
        visited_dirs += 1
        if visited_dirs > 128:
            return False
        current_path = Path(current_dir)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            rel_path = (current_path / dirname).relative_to(root_resolved).as_posix()
            if not is_runtime_artifact_path(rel_path, root=root_resolved):
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            rel_path = (current_path / filename).relative_to(root_resolved).as_posix()
            if not is_runtime_artifact_path(rel_path, root=root_resolved):
                return False
    return True


def _repo_rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _normalize_stored_relpath(value: str | None) -> str:
    raw = str(value or ".").strip() or "."
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ForgeError("workspace relative path must be relative")
    normalized = PurePosixPath(raw.replace("\\", "/")).as_posix()
    return "." if normalized in {"", "."} else normalized


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ForgeError(f"Missing file: {path}") from e
    except json.JSONDecodeError as e:
        raise ForgeError(f"Invalid JSON file: {path}") from e
    if not isinstance(raw, dict):
        raise ForgeError(f"Invalid JSON structure in {path}")
    return raw


def _next_task_id(tasks: list[dict[str, Any]]) -> str:
    highest = 0
    for task in tasks:
        tid = str(task.get("id") or "")
        if tid.startswith("T"):
            tail = tid[1:]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return f"T{highest + 1:02d}"


def add_task(
    plan: dict[str, Any],
    *,
    title: str,
    description: str = "",
    acceptance_criteria: list[str] | None = None,
    dependencies: list[str] | None = None,
    estimated_files: list[str] | None = None,
    write_scope: list[str] | None = None,
    branch: str = "",
    status: str = "planned",
    attempts: int = 0,
    mcp_scope: dict[str, Any] | None = None,
    _allow_execution_unready: bool = False,
) -> dict[str, Any]:
    tasks = plan.setdefault("tasks", [])
    if not isinstance(tasks, list):
        raise ForgeError("plan.json field 'tasks' must be an array")

    title_text = title.strip()
    description_text = description.strip()
    acceptance = acceptance_criteria or []
    scope = normalize_task_file_fields(
        title=title_text,
        description=description_text,
        acceptance_criteria=acceptance,
        estimated_files=estimated_files,
        write_scope=write_scope,
        warning_prefix=f"Task '{title_text}'",
    )
    if (
        scope.requires_runnable_scope
        and not has_runnable_local_file_scope(
            estimated_files=scope.estimated_files,
            write_scope=scope.write_scope,
        )
        and not _allow_execution_unready
    ):
        raise ForgeError(manual_task_scope_error_message(title=title_text))

    task = {
        "id": _next_task_id(tasks),
        "title": title_text,
        "description": description_text,
        "acceptance_criteria": acceptance,
        "dependencies": dependencies or [],
        "estimated_files": scope.estimated_files,
        "write_scope": scope.write_scope,
        "branch": branch,
        "status": status,
        "attempts": attempts,
        "task_kind": scope.task_kind,
        "task_kind_reason": scope.task_kind_reason,
    }
    if isinstance(mcp_scope, dict):
        task["mcp_scope"] = dict(mcp_scope)
    if scope.task_kind == TASK_KIND_ANALYSIS_ONLY:
        task["analysis_only"] = True
    tasks.append(task)
    if not dependencies:
        inferred_dependency = infer_ordered_predecessor_dependency(tasks=tasks, task=task)
        if inferred_dependency is not None:
            task["dependencies"] = [inferred_dependency.depends_on]
    plan["updated_at"] = now_iso()
    return task


def find_task(plan: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    tasks = plan.get("tasks") or []
    if not isinstance(tasks, list):
        raise ForgeError("plan.json field 'tasks' must be an array")
    target = task_id.strip().lower()
    for task in tasks:
        tid = str(task.get("id") or "").strip().lower()
        if tid == target:
            return task
    return None


def set_task_status(plan: dict[str, Any], task_id: str, status: str) -> dict[str, Any]:
    task = find_task(plan, task_id)
    if not task:
        raise ForgeError(f"Task not found: {task_id}")
    task["status"] = status
    plan["updated_at"] = now_iso()
    return task


def requirement_text(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("text", "requirement", "title", "description", "content"):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return ""
    return str(item).strip()


def requirement_is_execution_ready(item: Any) -> bool:
    if isinstance(item, dict):
        return item.get("execution_ready") is not False
    return True


def add_requirement(
    plan: dict[str, Any],
    text: str,
    *,
    execution_ready: bool = True,
    source: str | None = None,
) -> None:
    reqs = plan.setdefault("requirements", [])
    if not isinstance(reqs, list):
        raise ForgeError("plan.json field 'requirements' must be an array")
    clean_text = text.strip()
    if not clean_text:
        return
    if execution_ready and not source:
        reqs.append(clean_text)
    else:
        requirement: dict[str, Any] = {
            "text": clean_text,
            "execution_ready": bool(execution_ready),
        }
        if source:
            requirement["source"] = source
        reqs.append(requirement)
    plan["updated_at"] = now_iso()


def _default_plan_json(run_id: str) -> dict[str, Any]:
    ts = now_iso()
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": ts,
        "updated_at": ts,
        "project_goal": "",
        "summary": "",
        "requirements": [],
        "tasks": [],
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [],
            "forbidden_roots": [],
            "decoy_roots": [],
            "unrelated_roots": [],
        },
        "assets": [],
    }


def _resolve_workspace_context_or_error(path: Path) -> WorkspaceContext:
    try:
        return resolve_workspace_context(path)
    except WorkspaceContextError as e:
        raise ForgeError(str(e)) from e


def _run_paths_from_workspace_context(
    *,
    context: WorkspaceContext,
    run_id: str,
    binding_requested_path: Path | None = None,
    binding_source: str = "explicit_path",
    workspace_created_at_startup: bool = False,
    binding_risk_level: str = WorkspaceRiskLevel.HEALTHY,
    binding_risk_reasons: tuple[str, ...] | list[str] = (),
    binding_broad_workspace_override_used: bool = False,
    runtime_dir_name: str = ".alysis",
) -> RunPaths:
    return make_run_paths(
        root=context.workspace_root,
        run_id=run_id,
        focus_path=context.focus_path,
        focus_relpath=context.focus_relpath,
        workspace_kind=context.workspace_kind,
        git_root=context.git_root,
        has_head_commit=context.has_head_commit,
        current_branch=context.current_branch,
        binding_requested_path=binding_requested_path or context.input_path,
        binding_source=binding_source,
        workspace_created_at_startup=workspace_created_at_startup,
        binding_risk_level=binding_risk_level,
        binding_risk_reasons=binding_risk_reasons,
        binding_broad_workspace_override_used=binding_broad_workspace_override_used,
        runtime_dir_name=runtime_dir_name,
    )


def rebind_run_paths_to_workspace_binding(
    *,
    paths: RunPaths,
    workspace_binding: WorkspaceBinding,
) -> RunPaths:
    stored_workspace_root = paths.root.resolve()
    current_workspace_root = workspace_binding.workspace_context.workspace_root.resolve()
    if stored_workspace_root != current_workspace_root:
        raise ForgeError(
            "Cannot resume a Forge run across workspace roots without creating a fresh run."
        )

    # Preserve the existing run directory/runtime layout while rebinding focus and
    # workspace metadata to the chat's current workspace binding.
    return _run_paths_from_workspace_context(
        context=workspace_binding.workspace_context,
        run_id=paths.run_id,
        binding_requested_path=workspace_binding.requested_path,
        binding_source=workspace_binding.binding_source,
        workspace_created_at_startup=workspace_binding.created_path,
        binding_risk_level=workspace_binding.risk_level,
        binding_risk_reasons=workspace_binding.risk_reasons,
        binding_broad_workspace_override_used=workspace_binding.broad_workspace_override_used,
        runtime_dir_name=paths.runtime_dir.name,
    )


def _workspace_binding_error(binding: WorkspaceBinding) -> ForgeError:
    detail = "; ".join(binding.risk_reasons) or "workspace selection is not allowed"
    if binding.risk_level == WorkspaceRiskLevel.BLOCKED:
        return ForgeError(f"Workspace binding blocked for {binding.requested_path}: {detail}.")
    if binding.risk_level == WorkspaceRiskLevel.GUARDED:
        return ForgeError(
            f"Workspace binding is guarded for {binding.requested_path}: {detail}. "
            "Pass allow_broad_workspace=True to continue."
        )
    return ForgeError(detail)


def _candidate_pointer_roots(*, input_path: Path, workspace_root: Path) -> list[Path]:
    candidates: list[Path] = [workspace_root]
    current = input_path
    while True:
        if current not in candidates:
            candidates.append(current)
        if current == workspace_root or current.parent == current:
            break
        current = current.parent
    return candidates


def _load_current_run_pointer(
    *,
    pointer_root: Path,
    pointer_path: Path,
) -> tuple[dict[str, Any], str, str]:
    pointer = _read_json(pointer_path)
    run_id = str(pointer.get("run_id") or "").strip()
    if not run_id:
        raise ForgeError(f"Invalid current run pointer: {pointer_path}")
    run_path_raw = str(pointer.get("run_path") or "").strip()
    if not run_path_raw:
        raise ForgeError(f"Invalid current run pointer (missing run_path): {pointer_path}")

    run_path = Path(run_path_raw)
    if run_path.is_absolute():
        raise ForgeError("current run pointer must use workspace-relative run_path")

    # Resolve the stored locator read-only, enforcing canonical project containment.
    try:
        run_dir = project_artifact_path(pointer_root, run_path).resolve()
    except ProjectDataError as e:
        raise ForgeError(f"Invalid current run pointer: {e}") from e
    _ensure_under_root(pointer_root, run_dir)
    runtime_dir_name = pointer_path.parent.name
    expected_run_dir = (
        _runs_dir(pointer_root, runtime_dir_name=runtime_dir_name) / run_id
    ).resolve()
    _ensure_under_root(pointer_root, expected_run_dir)
    if run_dir != expected_run_dir:
        raise ForgeError(
            "Invalid current run pointer: run_id and run_path disagree "
            f"(run_id={run_id}, run_path={run_path_raw})."
        )
    if not run_dir.exists():
        raise ForgeError(
            f"Current run directory does not exist: {run_dir}. Start a new run with "
            "'alysis forge plan'."
        )
    return pointer, run_id, runtime_dir_name


def _pointer_focus_relpath(pointer: dict[str, Any]) -> str:
    try:
        return _normalize_stored_relpath(str(pointer.get("focus_relpath") or "."))
    except ForgeError as e:
        raise ForgeError("current run pointer focus_relpath must be relative") from e


def _focus_path_from_rel(*, root: Path, focus_relpath: str) -> Path:
    if focus_relpath == ".":
        return root
    focus_path = (root / focus_relpath).resolve()
    _ensure_under_root(root, focus_path)
    return focus_path


def _pointer_requested_path(
    *,
    pointer_root: Path,
    pointer: dict[str, Any],
    default_path: Path,
) -> Path:
    raw = str(pointer.get("binding_requested_path") or "").strip()
    if not raw:
        return default_path
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = pointer_root / candidate
    return candidate.resolve(strict=False)


def _pointer_binding_source(pointer: dict[str, Any]) -> str:
    raw = str(pointer.get("binding_source") or "").strip()
    if raw:
        return raw
    return "current_run_pointer"


def _pointer_binding_risk_reasons(pointer: dict[str, Any]) -> tuple[str, ...]:
    raw = pointer.get("binding_risk_reasons")
    if not isinstance(raw, list):
        return ()
    return tuple(str(reason).strip() for reason in raw if str(reason).strip())


def _pointer_broad_workspace_override_used(pointer: dict[str, Any]) -> bool:
    return bool(pointer.get("binding_broad_workspace_override_used", False))


def _run_paths_from_pointer(
    *,
    pointer_root: Path,
    pointer_path: Path,
    canonical_workspace_root: Path,
) -> RunPaths:
    pointer, run_id, runtime_dir_name = _load_current_run_pointer(
        pointer_root=pointer_root, pointer_path=pointer_path
    )
    metadata_context = _resolve_workspace_context_or_error(pointer_root)
    if pointer_root == canonical_workspace_root:
        focus_relpath = _pointer_focus_relpath(pointer)
        focus_path = _focus_path_from_rel(root=pointer_root, focus_relpath=focus_relpath)
        requested_path = _pointer_requested_path(
            pointer_root=pointer_root,
            pointer=pointer,
            default_path=focus_path,
        )
        return make_run_paths(
            root=pointer_root,
            run_id=run_id,
            focus_path=focus_path,
            focus_relpath=focus_relpath,
            workspace_kind=metadata_context.workspace_kind,
            git_root=metadata_context.git_root,
            has_head_commit=metadata_context.has_head_commit,
            current_branch=metadata_context.current_branch,
            binding_requested_path=requested_path,
            binding_source=_pointer_binding_source(pointer),
            workspace_created_at_startup=bool(pointer.get("workspace_created_at_startup", False)),
            binding_risk_level=(
                str(pointer.get("binding_risk_level") or "").strip() or WorkspaceRiskLevel.HEALTHY
            ),
            binding_risk_reasons=_pointer_binding_risk_reasons(pointer),
            binding_broad_workspace_override_used=_pointer_broad_workspace_override_used(pointer),
            runtime_dir_name=runtime_dir_name,
        )
    requested_path = _pointer_requested_path(
        pointer_root=pointer_root,
        pointer=pointer,
        default_path=pointer_root,
    )
    return make_run_paths(
        root=pointer_root,
        run_id=run_id,
        focus_path=pointer_root,
        focus_relpath=".",
        workspace_kind=metadata_context.workspace_kind,
        git_root=metadata_context.git_root,
        has_head_commit=metadata_context.has_head_commit,
        current_branch=metadata_context.current_branch,
        binding_requested_path=requested_path,
        binding_source=_pointer_binding_source(pointer),
        workspace_created_at_startup=bool(pointer.get("workspace_created_at_startup", False)),
        binding_risk_level=(
            str(pointer.get("binding_risk_level") or "").strip() or WorkspaceRiskLevel.HEALTHY
        ),
        binding_risk_reasons=_pointer_binding_risk_reasons(pointer),
        binding_broad_workspace_override_used=_pointer_broad_workspace_override_used(pointer),
        runtime_dir_name=runtime_dir_name,
    )


def render_plan_markdown(plan: dict[str, Any], *, asset_index: Any | None = None) -> str:
    goal = str(plan.get("project_goal") or "").strip() or "(not set yet)"
    summary = str(plan.get("summary") or "").strip() or "(not set yet)"
    requirements = plan.get("requirements") or []
    superseded_requirements = plan.get("superseded_requirements") or []
    tasks = plan.get("tasks") or []
    assets = plan.get("assets") or []
    asset_titles, pinned_assets = _asset_markdown_metadata(asset_index)

    lines: list[str] = [
        "# PLAN",
        "",
        f"- Run ID: `{plan.get('run_id', '')}`",
        f"- Created: `{plan.get('created_at', '')}`",
        f"- Updated: `{plan.get('updated_at', '')}`",
        "",
        "## Project Goal",
        "",
        goal,
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Requirements",
        "",
    ]

    if requirements:
        for req in requirements:
            text = requirement_text(req)
            if not text:
                continue
            suffix = "" if requirement_is_execution_ready(req) else " (not execution-ready)"
            lines.append(f"- {text}{suffix}")
    else:
        lines.append("- (none)")

    if superseded_requirements:
        lines.extend(["", "## Superseded Requirements", ""])
        for item in superseded_requirements:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                reason = str(item.get("reason") or "").strip()
            else:
                text = str(item).strip()
                reason = ""
            if not text:
                continue
            suffix = f" ({reason})" if reason else ""
            lines.append(f"- {text}{suffix}")

    planning_constraints = plan.get("planning_constraints")
    if isinstance(planning_constraints, dict):
        constraint_lines: list[str] = []
        for label, key in (
            ("Target roots", "target_roots"),
            ("Forbidden roots", "forbidden_roots"),
            ("Decoy roots", "decoy_roots"),
            ("Unrelated roots", "unrelated_roots"),
        ):
            entries = [
                str(entry.get("path") or "").strip()
                for entry in planning_constraints.get(key) or []
                if isinstance(entry, dict) and str(entry.get("path") or "").strip()
            ]
            if entries:
                constraint_lines.append(f"- {label}: " + ", ".join(f"`{path}`" for path in entries))
        if constraint_lines:
            lines.extend(["", "## Planning Constraints", "", *constraint_lines])

    def _md_cell(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        return text.replace("|", r"\|").replace("\n", " ")

    lines.extend(["", "## Tasks Overview", ""])
    if tasks:
        status_counts: dict[str, int] = {}
        for task in tasks:
            status = str(task.get("status") or "planned").strip() or "planned"
            status_counts[status] = status_counts.get(status, 0) + 1
        counts_label = ", ".join(
            f"{status}: {count}" for status, count in sorted(status_counts.items())
        )
        if counts_label:
            lines.append(f"Status counts: {counts_label}")
            lines.append("")
    lines.append("| ID | Status | Title | Dependencies |")
    lines.append("|----|--------|-------|--------------|")
    if tasks:
        for task in tasks:
            tid = _md_cell(task.get("id"))
            status = _md_cell(task.get("status") or "planned")
            title = _md_cell(task.get("title"))
            deps = task.get("dependencies") or []
            deps_label = ", ".join(str(dep) for dep in deps) if deps else "-"
            lines.append(f"| {tid} | {status} | {title} | {_md_cell(deps_label)} |")
    else:
        lines.append("| - | - | (none) | - |")

    lines.extend(["", "## Tasks", ""])
    if tasks:
        for task in tasks:
            tid = task.get("id", "")
            title = task.get("title", "")
            status = task.get("status", "")
            lines.append(f"### {tid} - {title}")
            lines.append(f"- Status: `{status}`")
            attempts = int(task.get("attempts") or 0)
            lines.append(f"- Attempts: `{attempts}`")
            desc = str(task.get("description") or "").strip()
            lines.append(f"- Description: {desc or '(none)'}")
            deps = task.get("dependencies") or []
            lines.append(f"- Dependencies: {', '.join(deps) if deps else '(none)'}")
            asset_lines = _task_asset_markdown_lines(task, asset_titles)
            if asset_lines:
                lines.append("- Assets:")
                lines.extend(asset_lines)
            acc = task.get("acceptance_criteria") or []
            if acc:
                lines.append("- Acceptance Criteria:")
                for c in acc:
                    lines.append(f"  - {c}")
            else:
                lines.append("- Acceptance Criteria: (none)")
            files = task.get("estimated_files") or []
            lines.append(f"- Estimated Files: {', '.join(files) if files else '(none)'}")
            write_scope = task.get("write_scope") or []
            lines.append(f"- Write Scope: {', '.join(write_scope) if write_scope else '(derived)'}")
            lines.append(
                "- MCP Scope: "
                + describe_task_mcp_scope(
                    normalize_task_mcp_scope(
                        task.get("mcp_scope"),
                        warning_prefix=f"Task {tid or '(unknown)'}",
                    )[0]
                )
            )
            branch = str(task.get("branch") or "")
            lines.append(f"- Branch: {branch or '(not set)'}")
            lines.append("")
    else:
        lines.append("- (none)")
        lines.append("")

    if pinned_assets:
        lines.extend(["## Pinned Assets", ""])
        lines.append("These assets are available to all tasks regardless of binding:")
        for asset_id, title in pinned_assets:
            lines.append(f'- "{title}" ({asset_id})')
        lines.append("")

    lines.extend(["## Assets", ""])
    if assets:
        for asset in assets:
            stored = asset.get("stored_path", "")
            size = asset.get("size_bytes", 0)
            lines.append(f"- `{stored}` ({size} bytes)")
    else:
        lines.append("- (none)")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Transcript: `plan/notes/user_notes.md`",
            "- Planner Chat: `plan/notes/planner_chat.md`",
            "- Planner Summary: `plan/notes/planner_summary.md`",
            "- Plan Validation: `plan/notes/plan_validation.md`",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


def _asset_markdown_metadata(
    asset_index: Any | None,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    if asset_index is None:
        return {}, []
    records_method = getattr(asset_index, "records", None)
    if not callable(records_method):
        return {}, []
    try:
        records = records_method(include_deleted=True)
    except Exception:
        return {}, []
    titles: dict[str, str] = {}
    pinned: list[tuple[str, str]] = []
    for record in records:
        asset_id = str(getattr(record, "id", "") or "").strip()
        if not asset_id:
            continue
        title = str(getattr(record, "title", "") or "").strip() or "<unknown>"
        titles[asset_id] = title
        if bool(getattr(record, "pinned", False)) and getattr(record, "deleted_at", None) is None:
            pinned.append((asset_id, title))
    return titles, sorted(pinned)


def _task_asset_markdown_lines(task: dict[str, Any], asset_titles: dict[str, str]) -> list[str]:
    briefing = task.get("asset_briefing")
    if not isinstance(briefing, dict):
        return []
    lines: list[str] = []
    for group in ("primary", "may_need"):
        entries = briefing.get(group)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            asset_id = str(entry.get("asset_id") or "").strip()
            if not asset_id:
                continue
            title = asset_titles.get(asset_id, "<unknown>")
            rationale = str(entry.get("rationale") or "").strip()
            lines.append(f'  - "{title}" ({asset_id}) - {group} - {rationale}')
    return lines


def _asset_index_for_markdown(paths: RunPaths) -> Any | None:
    if not hasattr(paths, "assets_index_path"):
        return None
    try:
        from .assets.index import AssetIndex

        return AssetIndex(paths)
    except Exception:
        return None


def load_plan(paths: RunPaths, *, migrate_legacy: bool = True) -> dict[str, Any]:
    plan = _read_json(paths.plan_json_path)
    if migrate_legacy and _plan_schema_version(plan) < PLAN_SCHEMA_VERSION:
        plan = _migrate_legacy_assets_on_load(paths, plan)
    tasks = plan.get("tasks")
    assets = plan.get("assets")
    requirements = plan.get("requirements")
    if not isinstance(tasks, list):
        raise ForgeError("Invalid plan.json: 'tasks' must be an array")
    if not isinstance(assets, list):
        raise ForgeError("Invalid plan.json: 'assets' must be an array")
    if not isinstance(requirements, list):
        raise ForgeError("Invalid plan.json: 'requirements' must be an array")
    return plan


def _plan_schema_version(plan: dict[str, Any]) -> int:
    try:
        return int(plan.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _migrate_legacy_assets_on_load(paths: RunPaths, plan: dict[str, Any]) -> dict[str, Any]:
    try:
        from .assets.legacy_migration import migrate_legacy_assets
        from .assets.surface import build_asset_surface
        from .config import load_config

        cfg = load_config()
        surface = build_asset_surface(cfg=cfg, run_paths=paths)
        result = migrate_legacy_assets(
            cfg=cfg,
            run_paths=paths,
            surface=surface,
            comprehend_mode="async",
        )
    except Exception as exc:  # noqa: BLE001 - plan loading must remain recoverable
        LOGGER.warning(
            "legacy_asset_migration load_plan failed run_id=%s: %s",
            paths.run_id,
            exc,
        )
        return plan
    if result.plan_v2_written:
        try:
            return _read_json(paths.plan_json_path)
        except ForgeError:
            return plan
    return plan


def _plan_semantic_fingerprint(plan: dict[str, Any]) -> str:
    semantic_plan = dict(plan)
    semantic_plan.pop("updated_at", None)
    return json.dumps(
        semantic_plan,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def save_plan(paths: RunPaths, plan: dict[str, Any]) -> None:
    persisted_plan: dict[str, Any] | None
    try:
        persisted_plan = _read_json(paths.plan_json_path)
    except ForgeError:
        persisted_plan = None

    # Keep no-op persistence byte-stable: only semantic plan changes should refresh updated_at.
    semantic_changed = persisted_plan is None or (
        _plan_semantic_fingerprint(persisted_plan) != _plan_semantic_fingerprint(plan)
    )
    if semantic_changed:
        plan["updated_at"] = now_iso()
    elif persisted_plan is not None and "updated_at" in persisted_plan:
        plan["updated_at"] = persisted_plan["updated_at"]

    rendered_plan_md = render_plan_markdown(plan, asset_index=_asset_index_for_markdown(paths))
    if semantic_changed or not paths.plan_json_path.exists():
        _write_json(paths.plan_json_path, plan)

    write_plan_md = semantic_changed or not paths.plan_md_path.exists()
    if not write_plan_md:
        try:
            write_plan_md = paths.plan_md_path.read_text(encoding="utf-8") != rendered_plan_md
        except OSError:
            write_plan_md = True
    if write_plan_md:
        atomic_write_text(paths.plan_md_path, rendered_plan_md)

    if semantic_changed:
        # Best-effort: the plan is already on disk, and a pointer that cannot be
        # updated is a stale status line, not a lost plan.
        with suppress(Exception):
            sync_current_run_plan_status(paths, plan)


def load_workspace_context_artifact(paths: RunPaths) -> RepoScanResult | None:
    artifact_path = paths.workspace_context_json_path
    if artifact_path is None or not artifact_path.exists():
        return None
    raw = _read_json(artifact_path)
    if int(raw.get("schema_version") or 0) != REPO_SCAN_SCHEMA_VERSION:
        return None
    scan = RepoScanResult.from_dict(raw)
    if not scan.workspace_root:
        return None
    return scan


def format_workspace_context_summary_lines(scan: RepoScanResult) -> list[str]:
    return render_repo_scan_summary_lines(scan)


def refresh_workspace_context_artifacts(paths: RunPaths) -> RepoScanResult:
    plan_context_dir = paths.plan_context_dir
    artifact_path = paths.workspace_context_json_path
    summary_path = paths.workspace_summary_md_path
    if plan_context_dir is None or artifact_path is None or summary_path is None:
        raise ForgeError("Run paths are missing workspace context artifact locations.")
    plan_context_dir.mkdir(parents=True, exist_ok=True)
    context = WorkspaceContext(
        input_path=paths.focus_path or paths.root,
        focus_path=paths.focus_path or paths.root,
        workspace_root=paths.root,
        git_root=paths.git_root,
        focus_relpath=paths.focus_relpath or ".",
        workspace_kind=paths.workspace_kind or "plain_dir",
        has_head_commit=paths.has_head_commit,
        current_branch=paths.current_branch,
    )
    scan = scan_workspace(context=context)
    _write_json(artifact_path, scan.to_dict())
    atomic_write_text(summary_path, render_repo_scan_markdown(scan))
    return scan


def ensure_workspace_context_artifacts(
    paths: RunPaths,
    *,
    refresh_if_stale: bool = False,
) -> RepoScanResult:
    scan = load_workspace_context_artifact(paths)
    if scan is None:
        return refresh_workspace_context_artifacts(paths)
    if refresh_if_stale and _workspace_context_artifacts_stale(paths, scan):
        return refresh_workspace_context_artifacts(paths)
    if paths.workspace_summary_md_path is None or not paths.workspace_summary_md_path.exists():
        return refresh_workspace_context_artifacts(paths)
    return scan


def _workspace_context_artifacts_stale(paths: RunPaths, scan: RepoScanResult) -> bool:
    artifact_path = paths.workspace_context_json_path
    summary_path = paths.workspace_summary_md_path
    if artifact_path is None or summary_path is None:
        return True
    if not artifact_path.exists() or not summary_path.exists():
        return True
    if scan.workspace_root != os.fspath(paths.root):
        return True
    if scan.focus_relpath != (paths.focus_relpath or "."):
        return True
    if scan.workspace_kind != (paths.workspace_kind or "plain_dir"):
        return True
    if (scan.git_root or None) != (
        os.fspath(paths.git_root) if paths.git_root is not None else None
    ):
        return True
    if bool(scan.has_head_commit) != bool(paths.has_head_commit):
        return True
    if (scan.current_branch or None) != (paths.current_branch or None):
        return True

    json_mtime_ns = artifact_path.stat().st_mtime_ns
    focus_path = paths.focus_path or paths.root
    tracked_paths = [paths.root, focus_path]
    tracked_paths.extend((paths.root / rel_path).resolve() for rel_path in scan.observed_paths)
    for candidate in tracked_paths:
        try:
            stat = candidate.stat()
        except OSError:
            return True
        if stat.st_mtime_ns > json_mtime_ns:
            return True
    return False


def _sanitize_note_text(value: str) -> str:
    # Prevent UnicodeEncodeError for invalid surrogate code points from terminal input.
    return value.encode("utf-8", errors="replace").decode("utf-8")


def append_transcript_note(paths: RunPaths, *, role: str, message: str) -> None:
    paths.notes_dir.mkdir(parents=True, exist_ok=True)
    if not paths.notes_path.exists():
        paths.notes_path.write_text("# Planning Notes\n\n", encoding="utf-8")
    ts = now_iso()
    with paths.notes_path.open("a", encoding="utf-8") as fh:
        safe_role = _sanitize_note_text(role).strip()
        safe_message = _sanitize_note_text(message).strip()
        fh.write(f"- [{ts}] {safe_role}: {safe_message}\n")


def append_planner_chat(paths: RunPaths, *, role: str, message: str) -> None:
    paths.notes_dir.mkdir(parents=True, exist_ok=True)
    if not paths.planner_chat_path.exists():
        paths.planner_chat_path.write_text("# Planner Chat\n\n", encoding="utf-8")
    ts = now_iso()
    with paths.planner_chat_path.open("a", encoding="utf-8") as fh:
        safe_role = _sanitize_note_text(role).strip()
        safe_message = _sanitize_note_text(message).strip()
        fh.write(f"- [{ts}] {safe_role}: {safe_message}\n")


def append_planner_summary(paths: RunPaths, summary_line: str) -> None:
    paths.notes_dir.mkdir(parents=True, exist_ok=True)
    if not paths.planner_summary_path.exists():
        paths.planner_summary_path.write_text("# Planner Summary\n\n", encoding="utf-8")
    ts = now_iso()
    with paths.planner_summary_path.open("a", encoding="utf-8") as fh:
        safe_summary = _sanitize_note_text(summary_line).strip()
        fh.write(f"- [{ts}] {safe_summary}\n")


def finalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    raw_requirements = plan.get("requirements") or []
    requirements = [requirement_text(x) for x in raw_requirements if requirement_text(x)]
    execution_ready_requirements = [
        requirement_text(x)
        for x in raw_requirements
        if requirement_text(x) and requirement_is_execution_ready(x)
    ]
    if not plan.get("project_goal"):
        if requirements:
            plan["project_goal"] = requirements[0]
        else:
            plan["project_goal"] = "Define the project goal and implementation scope."

    if not plan.get("summary"):
        if requirements:
            plan["summary"] = " ".join(requirements[:3])
        else:
            plan["summary"] = "Initial planning scaffold created."

    tasks = plan.get("tasks") or []
    if not tasks:
        if execution_ready_requirements:
            for req in execution_ready_requirements[:8]:
                title = req if len(req) <= 80 else req[:77] + "..."
                add_task(
                    plan,
                    title=title,
                    description=req,
                    _allow_execution_unready=True,
                )
    plan["updated_at"] = now_iso()
    return plan


_LIFECYCLE_POINTER_KEYS: tuple[str, ...] = (
    "status",
    "status_updated_at",
    "status_reason",
    "status_history",
    "run_owner",
    "plan_fingerprint",
)


def read_current_run_pointer(root: Path) -> dict[str, Any] | None:
    """The raw pointer payload for ``root``, or ``None`` when there is none."""
    try:
        return _read_json(current_run_pointer_path(root))
    except ForgeError:
        return None


def write_current_run_pointer(paths: RunPaths) -> None:
    pointer = {
        "schema_version": CURRENT_RUN_POINTER_SCHEMA_VERSION,
        "run_id": paths.run_id,
        "run_path": _repo_rel(paths.root, paths.run_dir),
        "updated_at": now_iso(),
        "workspace_root": os.fspath(paths.root),
        "focus_path": os.fspath(paths.focus_path or paths.root),
        "focus_relpath": paths.focus_relpath or ".",
        "workspace_kind": paths.workspace_kind or "plain_dir",
        "has_head_commit": paths.has_head_commit,
        "greenfield": paths.greenfield,
        "binding_requested_path": os.fspath(
            paths.binding_requested_path or paths.focus_path or paths.root
        ),
        "binding_source": paths.binding_source or "explicit_path",
        "workspace_created_at_startup": paths.workspace_created_at_startup,
        "binding_risk_level": paths.binding_risk_level or WorkspaceRiskLevel.HEALTHY,
        "binding_risk_reasons": list(paths.binding_risk_reasons),
        "binding_broad_workspace_override_used": paths.binding_broad_workspace_override_used,
    }
    if paths.git_root is not None:
        pointer["git_root"] = os.fspath(paths.git_root)
    if paths.current_branch:
        pointer["current_branch"] = paths.current_branch

    # Rebinding a workspace must not silently reset the run's lifecycle. The binding
    # fields above are recomputed every time; the lifecycle block belongs to the run
    # and only ``set_current_run_status`` may move it.
    existing = read_current_run_pointer(paths.root)
    if existing is not None and str(existing.get("run_id") or "").strip() == paths.run_id:
        for key in _LIFECYCLE_POINTER_KEYS:
            if key in existing:
                pointer[key] = existing[key]
    else:
        pointer["status"] = RUN_STATUS_DRAFT
        pointer["status_updated_at"] = now_iso()
        pointer["status_history"] = [
            {"status": RUN_STATUS_DRAFT, "at": pointer["status_updated_at"]}
        ]
    _write_json(current_run_pointer_path(paths.root), pointer)


def set_current_run_status(
    paths: RunPaths,
    status: str,
    *,
    reason: str = "",
    owner: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Move the current-run pointer to ``status``, if it still tracks this run.

    Returns the written payload, or ``None`` when the pointer is missing or has
    moved on to a different run -- a finishing run must never stamp its outcome
    onto whatever run became current in the meantime.
    """
    pointer = read_current_run_pointer(paths.root)
    if pointer is None or str(pointer.get("run_id") or "").strip() != paths.run_id:
        return None
    updated = apply_run_status(
        pointer,
        status,
        reason=reason,
        owner=owner,
        plan_fingerprint=plan_fingerprint(plan) if plan is not None else None,
    )
    _write_json(current_run_pointer_path(paths.root), updated)
    return updated


def current_run_status(root: Path) -> str:
    """The recorded lifecycle status for the workspace's current run."""
    return pointer_status(read_current_run_pointer(root))


def sync_current_run_plan_status(paths: RunPaths, plan: dict[str, Any]) -> None:
    """Keep the pointer's draft/approved status and fingerprint in step with the plan.

    Called from :func:`save_plan`, which is the one choke point every plan edit goes
    through. Two things happen here:

    * A plan crossing the execution-readiness gate flips ``draft`` <-> ``approved``,
      so a UI can show "ready to run" without re-deriving the gate itself.
    * While a run is ``running``, its own saves refresh the recorded fingerprint.
      That is what makes drift detection mean "changed while nobody was running it":
      in-run edits (scope amendments, replanning) are the run observing its own
      plan, whereas an edit made after the run died is exactly what `forge resume`
      must stop and surface.
    """
    from .plan_repair import PLAN_STATUS_EXECUTION_READY, plan_status

    pointer = read_current_run_pointer(paths.root)
    if pointer is None or str(pointer.get("run_id") or "").strip() != paths.run_id:
        return
    status = pointer_status(pointer)
    if status == RUN_STATUS_RUNNING:
        fingerprint = plan_fingerprint(plan)
        # Most in-run saves only move task status/attempts, which the fingerprint
        # deliberately ignores. Rewriting an identical pointer on every one of them
        # would be pure churn.
        if pointer.get("plan_fingerprint") != fingerprint:
            _write_json(
                current_run_pointer_path(paths.root),
                {**pointer, "plan_fingerprint": fingerprint},
            )
        return
    if status not in {RUN_STATUS_DRAFT, RUN_STATUS_APPROVED}:
        return
    try:
        ready = plan_status(plan) == PLAN_STATUS_EXECUTION_READY
    except Exception:  # noqa: BLE001 - a plan we cannot grade is not a status change
        return
    desired = RUN_STATUS_APPROVED if ready else RUN_STATUS_DRAFT
    if desired == status:
        return
    _write_json(
        current_run_pointer_path(paths.root),
        apply_run_status(
            pointer,
            desired,
            reason=(
                "plan passes the execution-readiness gate"
                if ready
                else "plan no longer passes the execution-readiness gate"
            ),
            plan_fingerprint=plan_fingerprint(plan) if ready else None,
        ),
    )


def reconcile_current_run_status(root: Path) -> str | None:
    """Demote a crashed ``running`` pointer to ``interrupted``. Returns the new status.

    A run marked ``running`` holds the workspace mutation lock for its whole life,
    so ``running`` with no live lock means the owning process died without ever
    reaching a terminal state. Rewriting it here is what removes the need for
    outside tools to patch ``current_run.json`` themselves to un-stick a workspace.
    """
    # Imported lazily: run_lock imports this module, so a top-level import would
    # close the cycle.
    from .run_lock import lock_is_live

    pointer = read_current_run_pointer(root)
    if pointer is None or pointer_status(pointer) != RUN_STATUS_RUNNING:
        return None
    workspace_lock_dir = _runtime_dir(root) / "workspace_execution"
    run_path_raw = str(pointer.get("run_path") or "").strip()
    run_lock_dirs = [workspace_lock_dir]
    if run_path_raw and not Path(run_path_raw).is_absolute():
        try:
            run_lock_dirs.append(project_artifact_path(root, run_path_raw))
        except ProjectDataError:
            run_lock_dirs.append(root / run_path_raw)
    if any(lock_is_live(candidate) for candidate in run_lock_dirs):
        return None
    updated = reconcile_status_after_crash(pointer, lock_is_live=False)
    if updated is None:
        return None
    _write_json(current_run_pointer_path(root), updated)
    return pointer_status(updated)


def refresh_current_run_pointer_if_tracking_same_run(paths: RunPaths) -> bool:
    pointer_path = current_run_pointer_path(paths.root)
    try:
        pointer = _read_json(pointer_path)
    except ForgeError:
        return False
    if str(pointer.get("run_id") or "").strip() != paths.run_id:
        return False
    write_current_run_pointer(paths)
    return True


def load_current_run_paths(root: Path) -> RunPaths:
    input_path = ensure_repo_root(root)
    workspace_context = _resolve_workspace_context_or_error(input_path)
    candidates = _candidate_pointer_roots(
        input_path=input_path,
        workspace_root=workspace_context.workspace_root,
    )
    for pointer_root in candidates:
        pointer_path = current_run_pointer_path(pointer_root)
        if not pointer_path.exists():
            continue
        # Every surface that shows a run reaches it through here, so this is where a
        # crash leftover stops being shown as active. Best-effort on purpose: an
        # unwritable pointer must not make reading the run fail.
        with suppress(Exception):
            reconcile_current_run_status(pointer_root)
        return _run_paths_from_pointer(
            pointer_root=pointer_root,
            pointer_path=pointer_path,
            canonical_workspace_root=workspace_context.workspace_root,
        )
    raise ForgeError(f"Missing file: {current_run_pointer_path(workspace_context.workspace_root)}")


def create_plan_run(
    root: Path,
    *,
    create_if_missing: bool = False,
    allow_broad_workspace: bool = False,
    workspace_binding: WorkspaceBinding | None = None,
) -> RunPaths:
    if workspace_binding is None:
        try:
            binding = resolve_workspace_binding(
                root,
                create_if_missing=create_if_missing,
                allow_broad_workspace=allow_broad_workspace,
                source="explicit_path",
            )
        except WorkspaceBindingError as e:
            raise ForgeError(str(e)) from e
    else:
        binding = workspace_binding
    try:
        ensure_workspace_policy(
            binding,
            action=WorkspaceAction.FORGE_PLAN,
            allow_broad_workspace=allow_broad_workspace,
        )
    except WorkspaceBindingError:
        raise _workspace_binding_error(binding) from None
    paths = _run_paths_from_workspace_context(
        context=binding.workspace_context,
        run_id=make_run_id(binding.workspace_context.workspace_root),
        binding_requested_path=binding.requested_path,
        binding_source=binding.binding_source,
        workspace_created_at_startup=binding.created_path,
        binding_risk_level=binding.risk_level,
        binding_risk_reasons=binding.risk_reasons,
        binding_broad_workspace_override_used=binding.broad_workspace_override_used,
    )
    _ensure_under_root(paths.root, paths.run_dir)

    paths.assets_dir.mkdir(parents=True, exist_ok=True)
    paths.asset_store_dir.mkdir(parents=True, exist_ok=True)
    paths.assets_raw_dir.mkdir(parents=True, exist_ok=True)
    paths.assets_extracted_dir.mkdir(parents=True, exist_ok=True)
    paths.assets_comprehensions_dir.mkdir(parents=True, exist_ok=True)
    paths.notes_dir.mkdir(parents=True, exist_ok=True)
    paths.plan_replans_dir.mkdir(parents=True, exist_ok=True)
    paths.decisions_path.parent.mkdir(parents=True, exist_ok=True)
    paths.knowledge_task_attempts_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_issues_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_facts_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_decisions_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_selected_dir.mkdir(parents=True, exist_ok=True)

    plan = _default_plan_json(paths.run_id)
    _write_json(paths.plan_json_path, plan)
    atomic_write_text(
        paths.plan_md_path,
        render_plan_markdown(plan, asset_index=_asset_index_for_markdown(paths)),
    )
    paths.decisions_path.write_text("", encoding="utf-8")
    paths.risks_path.write_text("", encoding="utf-8")
    paths.notes_path.write_text("# Planning Notes\n\n", encoding="utf-8")
    paths.planner_chat_path.write_text("# Planner Chat\n\n", encoding="utf-8")
    paths.planner_summary_path.write_text("# Planner Summary\n\n", encoding="utf-8")
    refresh_workspace_context_artifacts(paths)

    write_current_run_pointer(paths)
    return paths


def _safe_unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base_name = Path(filename).name
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    candidate = directory / base_name
    i = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{i}{suffix}"
        i += 1
    return candidate


def _try_extract_text(source: Path) -> str | None:
    size = source.stat().st_size
    if size > MAX_TEXT_ASSET_BYTES:
        return None
    raw = source.read_bytes()
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def attach_asset(root: Path, source_path: Path) -> tuple[RunPaths, dict[str, Any]]:
    warnings.warn(
        "attach_asset() is deprecated. Use AssetSurface.add_asset() or the /assets modal / "
        "`alysis forge assets add` CLI subcommand. Legacy attached assets are "
        "auto-migrated on next plan load.",
        DeprecationWarning,
        stacklevel=2,
    )
    LOGGER.warning("attach_asset deprecated legacy_flow=true")
    input_path = ensure_repo_root(root)
    paths = load_current_run_paths(input_path)
    source = source_path.expanduser().resolve()
    if not source.exists():
        raise ForgeError(f"Attachment file does not exist: {source}")
    if not source.is_file():
        raise ForgeError(f"Attachment path is not a file: {source}")

    dest = _safe_unique_path(paths.assets_dir, source.name)
    _ensure_under_root(paths.root, dest)
    shutil.copy2(source, dest)

    plan = load_plan(paths, migrate_legacy=False)
    metadata: dict[str, Any] = {
        "original_path": os.fspath(source),
        "stored_path": _repo_rel(paths.root, dest),
        "size_bytes": dest.stat().st_size,
        "added_at": now_iso(),
    }

    extracted = _try_extract_text(source)
    if extracted is not None:
        text_name = source.stem + ".txt"
        text_dest = _safe_unique_path(paths.assets_text_dir, text_name)
        _ensure_under_root(paths.root, text_dest)
        text_dest.write_text(extracted, encoding="utf-8")
        metadata["text_copy_path"] = _repo_rel(paths.root, text_dest)

    assets = plan.setdefault("assets", [])
    if not isinstance(assets, list):
        raise ForgeError("Invalid plan.json: 'assets' must be an array")
    assets.append(metadata)
    plan["schema_version"] = 1
    plan.pop("legacy_assets_migrated_at", None)
    save_plan(paths, plan)
    return paths, metadata


def ensure_execution_dirs(paths: RunPaths) -> None:
    paths.execution_reports_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_patches_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_logs_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_sessions_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_reviews_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_verify_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_context_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_budgets_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_asset_briefings_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_asset_usage_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_knowledge_capture_dir.mkdir(parents=True, exist_ok=True)
    paths.execution_integration_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_task_attempts_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_issues_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_facts_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_decisions_dir.mkdir(parents=True, exist_ok=True)
    paths.knowledge_selected_dir.mkdir(parents=True, exist_ok=True)


def _safe_task_filename(task_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in task_id)
    return safe or "task"


def _real_execution_label(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _verification_results_lines(
    verify_payload: dict[str, Any] | None,
) -> list[str]:
    compact_payload = compact_verification_payload(
        verify_payload,
        max_command_results=10,
        output_preview_chars=240,
    )
    if compact_payload is None:
        return ["- Structured verification details unavailable."]

    lines: list[str] = [
        f"- Summary: {str(compact_payload.get('summary') or '(not available)')}",
        (
            f"- All Passed: {'yes' if compact_payload.get('all_passed') else 'no'}"
            if compact_payload.get("all_passed") is not None
            else "- All Passed: unknown"
        ),
    ]
    failed_commands = compact_payload.get("failed_commands")
    if isinstance(failed_commands, list) and failed_commands:
        lines.append(
            "- Failed Commands: "
            + ", ".join(str(item).strip() for item in failed_commands if str(item).strip())
        )
    else:
        lines.append("- Failed Commands: (none)")

    command_results = compact_payload.get("command_results")
    if not isinstance(command_results, list) or not command_results:
        lines.append("- Command Results: (none)")
        return lines

    command_results_total = compact_payload.get("command_results_total")
    if isinstance(command_results_total, int):
        total_results = command_results_total
    elif isinstance(command_results_total, str) and command_results_total.strip():
        try:
            total_results = int(command_results_total)
        except ValueError:
            total_results = len(command_results)
    else:
        total_results = len(command_results)
    if bool(compact_payload.get("command_results_truncated")):
        lines.append(
            f"- Command Results: showing {len(command_results)} of {total_results} commands"
        )
    else:
        lines.append(f"- Command Results: {len(command_results)} command(s)")

    for idx, item in enumerate(command_results, start=1):
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or "").strip() or "(unknown)"
        effective_command = str(item.get("effective_command") or command).strip() or command
        lines.extend(
            [
                "",
                f"### Verification Command {idx}",
                f"- Command: `{command}`",
                f"- Effective Command: `{effective_command}`",
                f"- Exit Code: {item.get('exit_code')}",
                f"- OK: {'yes' if item.get('ok') else 'no'}",
                f"- Real Execution: {_real_execution_label(item.get('real_execution'))}",
                f"- Fallback Used: {'yes' if item.get('fallback_used') else 'no'}",
            ]
        )
        if item.get("fallback_reason") is not None:
            lines.append(f"- Fallback Reason: {str(item.get('fallback_reason') or '')}")
        if item.get("non_execution_reason") is not None:
            lines.append(f"- Non-Execution Reason: {str(item.get('non_execution_reason') or '')}")
        preview = str(item.get("output_preview") or "").rstrip()
        lines.extend(
            [
                "- Output Preview:",
                "",
                "```text",
                preview or "(empty)",
                "```",
            ]
        )
    return lines


def write_task_report(
    *,
    paths: RunPaths,
    task: dict[str, Any],
    result: str,
    result_kind: str | None = None,
    summary: str,
    started_at: str,
    finished_at: str,
    changed_files: list[str],
    verify_commands: list[str],
    patch_path: Path,
    budget_artifact_path: Path | None = None,
    execution_log_artifacts: ExecutionLogArtifactsResult | None = None,
    verify_artifact_path: Path | None = None,
    verify_summary: str | None = None,
    verify_payload: dict[str, Any] | None = None,
    verify_command_source: str | None = None,
    base_branch: str | None = None,
    task_branch: str | None = None,
    commit_hash: str | None = None,
    merge_commit_hash: str | None = None,
    merge_result: str | None = None,
    salvaged_nonzero_exit: bool = False,
    salvaged_agent_exception: bool = False,
    agent_exception_summary: str | None = None,
    noop_reason: str | None = None,
    task_kind: str | None = None,
    task_lifecycle_reason: str | None = None,
    remote_lines: list[str] | None = None,
    scope_amendments: list[dict[str, Any]] | None = None,
    scope_amended_patterns: list[str] | None = None,
) -> Path:
    ensure_execution_dirs(paths)
    task_id = str(task.get("id") or "")
    task_title = str(task.get("title") or "")
    report_path = paths.execution_reports_dir / f"{_safe_task_filename(task_id)}.md"
    clean_agent_exception_summary = sanitize_optional_error_summary(agent_exception_summary)
    lines: list[str] = [
        f"# Task Execution Report: {task_id}",
        "",
        f"- Task Title: {task_title}",
        f"- Started At: {started_at}",
        f"- Finished At: {finished_at}",
        f"- Result: {result}",
        f"- Result Kind: {result_kind or '(n/a)'}",
        f"- Task Kind: {task_kind or str(task.get('task_kind') or '(n/a)')}",
        f"- Task Lifecycle Reason: {task_lifecycle_reason or str(task.get('task_kind_reason') or '(n/a)')}",
        f"- Salvaged Non-Zero Exit: {'yes' if salvaged_nonzero_exit else 'no'}",
        f"- Salvaged Agent Exception: {'yes' if salvaged_agent_exception else 'no'}",
        f"- Agent Exception Summary: {clean_agent_exception_summary or '(none)'}",
        f"- No-Op Reason: {noop_reason or '(n/a)'}",
        f"- Patch: `{_repo_rel(paths.root, patch_path)}`",
        (
            f"- Budget Artifact: `{_repo_rel(paths.root, budget_artifact_path)}`"
            if budget_artifact_path is not None
            else "- Budget Artifact: (none)"
        ),
        (
            f"- Verify Artifact: `{_repo_rel(paths.root, verify_artifact_path)}`"
            if verify_artifact_path is not None
            else "- Verify Artifact: (none)"
        ),
        (
            "- Session Logging: enabled"
            if execution_log_artifacts is None or execution_log_artifacts.logging_enabled
            else "- Session Logging: disabled (--no-log)"
        ),
        (
            f"- Execution Log: `{_repo_rel(paths.root, execution_log_artifacts.copied_log_path)}`"
            if execution_log_artifacts is not None
            and execution_log_artifacts.log_retained
            and execution_log_artifacts.copied_log_path is not None
            else "- Execution Log: (not retained)"
        ),
        (
            f"- Session Artifacts: `{_repo_rel(paths.root, execution_log_artifacts.session_artifact_dir)}`"
            if execution_log_artifacts is not None
            and execution_log_artifacts.session_artifacts_retained
            and execution_log_artifacts.session_artifact_dir is not None
            else "- Session Artifacts: (none retained)"
        ),
        f"- Verify Summary: {verify_summary or '(not run)'}",
        f"- Verify Command Source: {verify_command_source or '(n/a)'}",
        f"- Base Branch: {base_branch or '(n/a)'}",
        f"- Task Branch: {task_branch or '(n/a)'}",
        f"- Commit: {commit_hash or '(none)'}",
        f"- Merge Commit: {merge_commit_hash or '(none)'}",
        f"- Merge Result: {merge_result or '(not applicable)'}",
        "",
        "## Summary",
        "",
        summary or "(no summary)",
        "",
        "## Runtime Artifacts",
        "",
    ]
    if execution_log_artifacts is None:
        lines.append("- Session logging/artifact retention details unavailable.")
    else:
        lines.extend(
            [
                f"- Logging Enabled: {'yes' if execution_log_artifacts.logging_enabled else 'no'}",
                f"- Log Retained: {'yes' if execution_log_artifacts.log_retained else 'no'}",
                (
                    f"- Source Log Path: `"
                    f"{safe_serialized_path(execution_log_artifacts.source_log_path, workspace_root=paths.root)}`"
                    if execution_log_artifacts.source_log_path is not None
                    else "- Source Log Path: (none)"
                ),
                (
                    "- Session Artifacts Retained: yes"
                    if execution_log_artifacts.session_artifacts_retained
                    else "- Session Artifacts Retained: no"
                ),
            ]
        )
        if execution_log_artifacts.note is not None:
            lines.append(f"- Note: {execution_log_artifacts.note}")
        if execution_log_artifacts.cleanup_note is not None:
            lines.append(f"- Cleanup Note: {execution_log_artifacts.cleanup_note}")
    lines.extend(
        [
            "",
            "## Changed Files",
            "",
        ]
    )
    if changed_files:
        for item in changed_files:
            lines.append(f"- `{item}`")
    else:
        lines.append("- (none detected)")

    lines.extend(["", "## Scope Amendments", ""])
    if scope_amendments:
        for item in scope_amendments:
            path = str(item.get("path") or "")
            reason_code = str(item.get("reason_code") or "unknown")
            evidence = str(item.get("evidence") or "")
            lines.append(f"- `{path}` ({reason_code}): {evidence}")
        added = [str(pattern) for pattern in (scope_amended_patterns or []) if str(pattern).strip()]
        lines.append(
            "- write_scope additions: "
            + (", ".join(f"`{pattern}`" for pattern in added) if added else "(already covered)")
        )
    else:
        lines.append("- (none)")

    lines.extend(["", "## Verification Results", ""])
    lines.extend(_verification_results_lines(verify_payload))

    lines.extend(["", "## How To Verify", ""])
    if verify_commands:
        for cmd in verify_commands:
            lines.append(f"- `{cmd}`")
    elif verify_command_source == "task_refinement.no_authoritative_commands":
        lines.append("- No authoritative verification commands were available for this task yet.")
    else:
        lines.append("- Run the task-specific tests and checks relevant to the changed files.")

    lines.extend(["", "## Remote Sync", ""])
    if remote_lines:
        for line in remote_lines:
            lines.append(f"- {line}")
    else:
        lines.append("- (not used)")

    atomic_write_text(report_path, "\n".join(lines).rstrip() + "\n")
    return report_path
