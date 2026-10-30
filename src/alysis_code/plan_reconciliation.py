from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .direction_change import detect_direction_change, filter_obsolete_direction_paths
from .file_classification import (
    CODE_SCAN_SKIP_DIR_NAMES,
    is_code_implementation_path,
    is_symbol_scannable_path,
    symbol_definition_regex,
)
from .planning_constraints import (
    filter_scope_entries_for_planning_constraints,
    planning_constraints_from_plan,
    update_plan_planning_constraints,
)
from .swarm_scheduler import canonical_task_status
from .task_readiness import (
    TASK_KIND_ANALYSIS_ONLY,
    classify_task_lifecycle,
    has_runnable_local_file_scope,
    task_readiness_warning,
    task_requires_runnable_file_scope,
)
from .task_scope import (
    extract_forbidden_repo_path_hints,
    extract_repo_path_hints,
    is_internal_alysis_path,
    scope_path_matches_pattern,
    split_normalized_repo_path_list,
)

_GLOB_CHARS = ("*", "?")
_TASK_ID_HINT_RE = re.compile(r"\bT\d+\b", re.IGNORECASE)
_NON_EXECUTABLE_OBSOLETE_STATUSES = frozenset({"superseded", "invalidated"})
_FRAMEWORK_DYNAMIC_ROUTE_SEGMENT_RE = re.compile(
    r"^(?:\[\[?\.\.\.[A-Za-z0-9_-]+\]?\]|\[[A-Za-z0-9_-]+\]|\([A-Za-z0-9_.-]+\))$"
)
_SYMBOL_CANDIDATE_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_SYMBOL_CANDIDATE_STOPWORDS = frozenset(
    {
        "add",
        "and",
        "blank",
        "blanks",
        "bug",
        "class",
        "code",
        "def",
        "description",
        "file",
        "fix",
        "for",
        "from",
        "function",
        "implement",
        "module",
        "old",
        "output",
        "return",
        "test",
        "tests",
        "the",
        "update",
        "when",
        "with",
    }
)
_FILENAME_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_]{2,}\b", re.IGNORECASE)
_EXPLICIT_MISSING_PATH_ACTION_RE = re.compile(
    r"\b(?:add|create|creating|generate|generating|introduce|new|scaffold|write|writing)\b",
    re.IGNORECASE,
)
_MAX_SYMBOL_SCAN_FILES = 1500
_MAX_SYMBOL_SCAN_BYTES = 512_000


@dataclass(frozen=True)
class PlanReconciliationResult:
    changed: bool
    warnings: list[str]
    task_updates: dict[str, dict[str, Any]]

    @property
    def updated_task_ids(self) -> list[str]:
        return list(self.task_updates)


@dataclass(frozen=True)
class _PathAnchorGroup:
    task_ids: tuple[str, ...]
    paths: tuple[str, ...]


def summarize_plan_reconciliation(result: PlanReconciliationResult) -> str:
    if result.task_updates:
        return "reconciled tasks: " + ", ".join(result.updated_task_ids)
    if result.warnings:
        return f"reconciliation warnings: {len(result.warnings)}"
    return "reconciliation made no changes"


def reconcile_plan_with_workspace(
    plan: dict[str, Any],
    *,
    workspace_root: Path,
    workspace_context: dict[str, Any] | None = None,
    user_text: str | None = None,
    transcript_tail: list[dict[str, Any]] | None = None,
    target_task_ids: set[str] | None = None,
) -> PlanReconciliationResult:
    tasks_raw = plan.get("tasks")
    if not isinstance(tasks_raw, list):
        return PlanReconciliationResult(
            changed=False,
            warnings=["Plan reconciliation skipped: tasks field is missing or invalid."],
            task_updates={},
        )

    root = workspace_root.expanduser().resolve()
    known_paths = _known_workspace_paths(workspace_context)
    greenfield = _workspace_context_is_greenfield(workspace_context)
    normalized_target_task_ids = (
        {str(task_id).strip() for task_id in target_task_ids if str(task_id).strip()}
        if target_task_ids is not None
        else None
    )
    anchor_groups = _latest_path_anchor_groups(
        transcript_tail=transcript_tail,
        user_text=user_text,
    )
    changed = False
    warnings: list[str] = []
    task_updates: dict[str, dict[str, Any]] = {}
    planning_constraints = None
    constraints_changed = False
    candidate_constraint_messages = _candidate_user_messages(
        transcript_tail=transcript_tail,
        user_text=user_text,
    )[:4]
    for constraint_text in reversed(candidate_constraint_messages):
        planning_constraints, message_changed = update_plan_planning_constraints(
            plan,
            text=constraint_text,
            workspace_context=workspace_context,
            direction_change=detect_direction_change(constraint_text),
        )
        constraints_changed = constraints_changed or message_changed
    if planning_constraints is None:
        planning_constraints = planning_constraints_from_plan(plan)
    if constraints_changed:
        changed = True
        warnings.append("Recorded planning scope constraints from latest user direction.")

    task_entries = [(index, task) for index, task in enumerate(tasks_raw) if isinstance(task, dict)]
    task_count = len(task_entries)

    for task_position, (index, task) in enumerate(task_entries):
        task_id = _task_label(task=task, index=index)
        if normalized_target_task_ids is not None and task_id not in normalized_target_task_ids:
            continue
        if (
            canonical_task_status(str(task.get("status") or ""))
            in _NON_EXECUTABLE_OBSOLETE_STATUSES
        ):
            continue
        current_estimated = _string_list(task.get("estimated_files"))
        current_write_scope = _string_list(task.get("write_scope"))
        allow_create_targets = _task_allows_create_targets(task=task, greenfield=greenfield)
        forbidden_path_identities = _task_forbidden_path_identities(
            task=task,
            user_text=user_text,
        )
        task_explicit_path_hints = [
            path
            for path in _task_explicit_path_hints(
                task=task,
                latest_user_text=str(user_text or ""),
            )
            if _path_identity_key(path) not in forbidden_path_identities
        ]
        task_anchor_paths = [
            path
            for path in _select_task_anchor_paths(
                task=task,
                task_position=task_position,
                task_count=task_count,
                anchor_groups=anchor_groups,
            )
            if _path_identity_key(path) not in forbidden_path_identities
        ]
        task_anchor_set = set(task_anchor_paths)
        protected_symbol_remap_path_identities = _missing_path_identities(
            [*task_explicit_path_hints, *task_anchor_paths],
            workspace_root=root,
            known_paths=known_paths,
        )

        normalized_estimated, dropped_estimated = split_normalized_repo_path_list(
            task.get("estimated_files"),
            root=root,
        )
        normalized_write_scope, dropped_write_scope = split_normalized_repo_path_list(
            task.get("write_scope"),
            root=root,
        )

        if dropped_estimated:
            warnings.append(
                f"Task {task_id}: dropped invalid estimated_files entries: "
                + ", ".join(dropped_estimated)
            )
        if dropped_write_scope:
            warnings.append(
                f"Task {task_id}: dropped invalid write_scope entries: "
                + ", ".join(dropped_write_scope)
            )

        estimated_files, dropped_internal_estimated = _drop_protected_paths(normalized_estimated)
        write_scope, dropped_internal_write_scope = _drop_protected_paths(normalized_write_scope)
        if dropped_internal_estimated:
            warnings.append(
                f"Task {task_id}: dropped protected estimated_files entries: "
                + ", ".join(dropped_internal_estimated)
            )
        if dropped_internal_write_scope:
            warnings.append(
                f"Task {task_id}: dropped protected write_scope entries: "
                + ", ".join(dropped_internal_write_scope)
            )
        estimated_files, dropped_forbidden_estimated = _drop_forbidden_paths(
            estimated_files,
            forbidden_path_identities,
        )
        if dropped_forbidden_estimated:
            warnings.append(
                f"Task {task_id}: dropped forbidden estimated_files entries: "
                + ", ".join(dropped_forbidden_estimated)
            )
        write_scope, dropped_forbidden_write_scope = _drop_forbidden_paths(
            write_scope,
            forbidden_path_identities,
        )
        if dropped_forbidden_write_scope:
            warnings.append(
                f"Task {task_id}: dropped forbidden write_scope entries: "
                + ", ".join(dropped_forbidden_write_scope)
            )

        if not estimated_files:
            inferred_paths, _ = _infer_estimated_files(
                task=task,
                workspace_root=root,
                known_paths=known_paths,
                allowed_missing_paths=task_anchor_set,
                latest_user_text=str(user_text or ""),
            )
            if inferred_paths:
                estimated_files = inferred_paths
                warnings.append(
                    f"Task {task_id}: inferred estimated_files from task text: "
                    + ", ".join(inferred_paths)
                )

        estimated_files, dropped_ungrounded_estimated = _drop_ungrounded_suspicious_paths(
            paths=estimated_files,
            workspace_root=root,
            known_paths=known_paths,
            allowed_missing_paths=task_anchor_set,
        )
        if dropped_ungrounded_estimated:
            warnings.append(
                f"Task {task_id}: dropped suspicious estimated_files entries not grounded in the "
                "latest user request: " + ", ".join(dropped_ungrounded_estimated)
            )
            if not estimated_files:
                inferred_paths, _ = _infer_estimated_files(
                    task=task,
                    workspace_root=root,
                    known_paths=known_paths,
                    allowed_missing_paths=task_anchor_set,
                    latest_user_text=str(user_text or ""),
                )
                if inferred_paths:
                    estimated_files = inferred_paths
                    warnings.append(
                        f"Task {task_id}: restored grounded estimated_files from task text: "
                        + ", ".join(inferred_paths)
                    )
        explicit_task_paths, _ = _infer_estimated_files(
            task=task,
            workspace_root=root,
            known_paths=known_paths,
            allowed_missing_paths=task_anchor_set,
            latest_user_text=str(user_text or ""),
        )
        added_explicit_estimated = [
            path for path in explicit_task_paths if path not in estimated_files
        ]
        if added_explicit_estimated:
            estimated_files = _dedupe_keep_order([*estimated_files, *added_explicit_estimated])
            warnings.append(
                f"Task {task_id}: added explicit task path hints to estimated_files: "
                + ", ".join(added_explicit_estimated)
            )

        estimated_files, estimated_warning = _apply_task_anchor_paths(
            current_paths=estimated_files,
            anchor_paths=task_anchor_paths,
            field_name="estimated_files",
        )
        if estimated_warning:
            warnings.append(f"Task {task_id}: {estimated_warning}")
        estimated_files, dropped_forbidden_estimated = _drop_forbidden_paths(
            estimated_files,
            forbidden_path_identities,
        )
        if dropped_forbidden_estimated:
            warnings.append(
                f"Task {task_id}: dropped forbidden estimated_files entries: "
                + ", ".join(dropped_forbidden_estimated)
            )

        for path in estimated_files:
            if path in task_anchor_set:
                continue
            if not allow_create_targets and _is_suspicious_path(
                path=path,
                workspace_root=root,
                known_paths=known_paths,
            ):
                warnings.append(
                    f"Task {task_id}: estimated_files entry may be suspicious or missing: {path}"
                )

        if not write_scope and estimated_files:
            write_scope = list(estimated_files)
            warnings.append(
                f"Task {task_id}: seeded write_scope from estimated_files: "
                + ", ".join(estimated_files)
            )

        write_scope, dropped_ungrounded_write_scope = _drop_ungrounded_suspicious_paths(
            paths=write_scope,
            workspace_root=root,
            known_paths=known_paths,
            allowed_missing_paths=task_anchor_set,
        )
        if dropped_ungrounded_write_scope:
            warnings.append(
                f"Task {task_id}: dropped suspicious write_scope entries not grounded in the "
                "latest user request: " + ", ".join(dropped_ungrounded_write_scope)
            )
            if not write_scope and estimated_files:
                write_scope = list(estimated_files)
                warnings.append(
                    f"Task {task_id}: reseeded write_scope from grounded estimated_files: "
                    + ", ".join(estimated_files)
                )
        added_explicit_write_scope = [
            path
            for path in explicit_task_paths
            if path in estimated_files and path not in write_scope
        ]
        if added_explicit_write_scope:
            write_scope = _dedupe_keep_order([*write_scope, *added_explicit_write_scope])
            warnings.append(
                f"Task {task_id}: added explicit task path hints to write_scope: "
                + ", ".join(added_explicit_write_scope)
            )

        write_scope, write_scope_warning = _apply_task_anchor_paths(
            current_paths=write_scope,
            anchor_paths=task_anchor_paths,
            field_name="write_scope",
        )
        if write_scope_warning:
            warnings.append(f"Task {task_id}: {write_scope_warning}")
        write_scope, dropped_forbidden_write_scope = _drop_forbidden_paths(
            write_scope,
            forbidden_path_identities,
        )
        if dropped_forbidden_write_scope:
            warnings.append(
                f"Task {task_id}: dropped forbidden write_scope entries: "
                + ", ".join(dropped_forbidden_write_scope)
            )

        if not task_anchor_paths:
            symbol_paths = _resolve_symbol_grounded_paths(
                task=task,
                workspace_root=root,
                latest_user_text=str(user_text or ""),
            )
            grounded_paths = symbol_paths or _resolve_named_code_file_paths(
                task=task,
                workspace_root=root,
                latest_user_text=str(user_text or ""),
            )
            if grounded_paths:
                estimated_files, estimated_symbol_warning = _apply_symbol_grounded_paths(
                    current_paths=estimated_files,
                    symbol_paths=grounded_paths,
                    workspace_root=root,
                    task=task,
                    field_name="estimated_files",
                    protected_missing_path_identities=protected_symbol_remap_path_identities,
                )
                if estimated_symbol_warning:
                    warnings.append(f"Task {task_id}: {estimated_symbol_warning}")
                write_scope, write_symbol_warning = _apply_symbol_grounded_paths(
                    current_paths=write_scope,
                    symbol_paths=grounded_paths,
                    workspace_root=root,
                    task=task,
                    field_name="write_scope",
                    protected_missing_path_identities=protected_symbol_remap_path_identities,
                )
                if write_symbol_warning:
                    warnings.append(f"Task {task_id}: {write_symbol_warning}")

        estimated_files, estimated_constraint_violations = (
            filter_scope_entries_for_planning_constraints(
                estimated_files,
                task={
                    **task,
                    "estimated_files": estimated_files,
                    "write_scope": estimated_files,
                },
                constraints=planning_constraints,
            )
        )
        if estimated_constraint_violations:
            warnings.append(
                f"Task {task_id}: dropped estimated_files outside planning constraints: "
                + ", ".join(
                    f"{item.path} ({item.classification}; {item.reason_code})"
                    for item in estimated_constraint_violations[:6]
                )
            )
        write_scope, write_constraint_violations = filter_scope_entries_for_planning_constraints(
            write_scope,
            task={**task, "estimated_files": write_scope, "write_scope": write_scope},
            constraints=planning_constraints,
        )
        if write_constraint_violations:
            warnings.append(
                f"Task {task_id}: dropped write_scope outside planning constraints: "
                + ", ".join(
                    f"{item.path} ({item.classification}; {item.reason_code})"
                    for item in write_constraint_violations[:6]
                )
            )

        for path in write_scope:
            if path in task_anchor_set:
                continue
            if not allow_create_targets and _is_suspicious_path(
                path=path,
                workspace_root=root,
                known_paths=known_paths,
            ):
                warnings.append(
                    f"Task {task_id}: write_scope entry may be suspicious or missing: {path}"
                )

        lifecycle = classify_task_lifecycle(
            title=str(task.get("title") or "").strip(),
            description=str(task.get("description") or "").strip(),
            acceptance_criteria=_string_list(task.get("acceptance_criteria")),
            estimated_files=estimated_files,
            write_scope=write_scope,
            explicit_analysis_only=True if task.get("analysis_only") is True else None,
        )
        if lifecycle.kind == TASK_KIND_ANALYSIS_ONLY:
            if estimated_files or write_scope:
                warnings.append(
                    f"Task {task_id}: cleared file mutation scope for analysis-only/report-only task"
                )
            estimated_files = []
            write_scope = []

        if task_requires_runnable_file_scope(
            title=str(task.get("title") or "").strip(),
            description=str(task.get("description") or "").strip(),
            acceptance_criteria=_string_list(task.get("acceptance_criteria")),
            estimated_files=estimated_files,
            write_scope=write_scope,
        ) and not has_runnable_local_file_scope(
            estimated_files=estimated_files,
            write_scope=write_scope,
        ):
            warnings.append(
                task_readiness_warning(
                    task_id=task_id,
                    title=str(task.get("title") or "").strip(),
                )
            )

        patch: dict[str, Any] = {}
        if estimated_files != current_estimated:
            task["estimated_files"] = estimated_files
            patch["estimated_files"] = estimated_files
        if write_scope != current_write_scope:
            task["write_scope"] = write_scope
            patch["write_scope"] = write_scope
        scope_changed = "estimated_files" in patch or "write_scope" in patch
        should_persist_lifecycle = (
            scope_changed
            or "task_kind" in task
            or "task_kind_reason" in task
            or lifecycle.kind == TASK_KIND_ANALYSIS_ONLY
        )
        if should_persist_lifecycle and task.get("task_kind") != lifecycle.kind:
            task["task_kind"] = lifecycle.kind
            patch["task_kind"] = lifecycle.kind
        if should_persist_lifecycle and task.get("task_kind_reason") != lifecycle.reason_code:
            task["task_kind_reason"] = lifecycle.reason_code
            patch["task_kind_reason"] = lifecycle.reason_code
        if lifecycle.kind == TASK_KIND_ANALYSIS_ONLY and task.get("analysis_only") is not True:
            task["analysis_only"] = True
            patch["analysis_only"] = True
        elif (
            lifecycle.kind != TASK_KIND_ANALYSIS_ONLY
            and task.get("analysis_only") is True
            and task.get("task_kind") != TASK_KIND_ANALYSIS_ONLY
        ):
            task.pop("analysis_only", None)
            patch["analysis_only"] = None
        if patch:
            task_updates[task_id] = patch
            changed = True

    return PlanReconciliationResult(
        changed=changed,
        warnings=_dedupe_keep_order(warnings),
        task_updates=task_updates,
    )


def _task_label(*, task: dict[str, Any], index: int) -> str:
    task_id = str(task.get("id") or "").strip()
    if task_id:
        return task_id
    title = str(task.get("title") or "").strip()
    if title:
        return title
    return f"task[{index}]"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _path_identity_key(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").rstrip("/")
    if normalized.casefold() in {"readme", "readme.md"}:
        return "__readme_alias__"
    return normalized.casefold()


def _drop_forbidden_paths(
    paths: list[str], forbidden_paths: set[str]
) -> tuple[list[str], list[str]]:
    if not forbidden_paths:
        return _dedupe_keep_order(paths), []
    kept: list[str] = []
    dropped: list[str] = []
    for path in _dedupe_keep_order(paths):
        if _path_identity_key(path) in forbidden_paths:
            dropped.append(path)
            continue
        kept.append(path)
    return kept, dropped


def _task_forbidden_path_identities(*, task: dict[str, Any], user_text: str | None) -> set[str]:
    acceptance = _string_list(task.get("acceptance_criteria"))
    text = "\n".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            *acceptance,
            str(user_text or ""),
        ]
    )
    return {_path_identity_key(path) for path in extract_forbidden_repo_path_hints(text)}


def _task_explicit_path_hints(
    *,
    task: dict[str, Any],
    latest_user_text: str = "",
) -> list[str]:
    task_text = "\n".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            *[str(item) for item in _string_list(task.get("acceptance_criteria"))],
        ]
    )
    hints, _obsolete_hints = filter_obsolete_direction_paths(
        extract_repo_path_hints(task_text),
        latest_user_text=latest_user_text,
        task_text=task_text,
    )
    out: list[str] = []
    for hint in hints:
        if _has_glob(hint):
            continue
        if is_internal_alysis_path(hint) or hint == ".git" or hint.startswith(".git/"):
            continue
        out.append(hint)
    return _dedupe_keep_order(out)


def _missing_path_identities(
    paths: list[str],
    *,
    workspace_root: Path,
    known_paths: set[str],
) -> set[str]:
    identities: set[str] = set()
    for path in _dedupe_keep_order(paths):
        if _has_glob(path) or path in known_paths:
            continue
        try:
            exists = (workspace_root / path).resolve().exists()
        except OSError:
            exists = False
        if exists:
            continue
        identities.add(_path_identity_key(path))
    return identities


def _candidate_user_messages(
    *,
    transcript_tail: list[dict[str, Any]] | None,
    user_text: str | None,
) -> list[str]:
    messages: list[str] = []
    latest = str(user_text or "").strip()
    if latest:
        messages.append(latest)
    for item in reversed(transcript_tail or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role != "user":
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if messages and content == messages[-1]:
            continue
        messages.append(content)
    return messages


def _extract_task_ids(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    ids: list[str] = []
    for match in _TASK_ID_HINT_RE.findall(text or ""):
        task_id = match.upper()
        if task_id in seen:
            continue
        seen.add(task_id)
        ids.append(task_id)
    return tuple(ids)


def _extract_path_anchor_groups(text: str) -> list[_PathAnchorGroup]:
    groups: list[_PathAnchorGroup] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        forbidden = {_path_identity_key(path) for path in extract_forbidden_repo_path_hints(line)}
        paths = tuple(
            path
            for path in filter_obsolete_direction_paths(
                extract_repo_path_hints(line), latest_user_text=text, task_text=line
            )[0]
            if _path_identity_key(path) not in forbidden
        )
        if not paths:
            continue
        groups.append(
            _PathAnchorGroup(
                task_ids=_extract_task_ids(line),
                paths=paths,
            )
        )
    if groups:
        return groups
    forbidden = {_path_identity_key(path) for path in extract_forbidden_repo_path_hints(text or "")}
    global_paths = tuple(
        path
        for path in filter_obsolete_direction_paths(
            extract_repo_path_hints(text or ""), latest_user_text=text, task_text=text
        )[0]
        if _path_identity_key(path) not in forbidden
    )
    if not global_paths:
        return []
    return [_PathAnchorGroup(task_ids=(), paths=global_paths)]


def _latest_path_anchor_groups(
    *,
    transcript_tail: list[dict[str, Any]] | None,
    user_text: str | None,
) -> list[_PathAnchorGroup]:
    for message in _candidate_user_messages(
        transcript_tail=transcript_tail,
        user_text=user_text,
    ):
        groups = _extract_path_anchor_groups(message)
        if groups:
            return groups
    return []


def _select_task_anchor_paths(
    *,
    task: dict[str, Any],
    task_position: int,
    task_count: int,
    anchor_groups: list[_PathAnchorGroup],
) -> list[str]:
    if not anchor_groups:
        return []
    task_id = str(task.get("id") or "").strip().upper()
    if task_id:
        matched = [
            path for group in anchor_groups if task_id in group.task_ids for path in group.paths
        ]
        if matched:
            return _dedupe_keep_order(matched)

    if task_count == 1:
        return _dedupe_keep_order([path for group in anchor_groups for path in group.paths])

    anonymous_groups = [group for group in anchor_groups if not group.task_ids]
    if len(anonymous_groups) == task_count:
        return list(anonymous_groups[task_position].paths)
    return []


def _drop_protected_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    dropped: list[str] = []
    for path in paths:
        if is_internal_alysis_path(path) or path == ".git" or path.startswith(".git/"):
            dropped.append(path)
            continue
        kept.append(path)
    return _dedupe_keep_order(kept), dropped


def _workspace_context_is_greenfield(workspace_context: dict[str, Any] | None) -> bool:
    return bool(isinstance(workspace_context, dict) and workspace_context.get("greenfield"))


def _task_allows_create_targets(*, task: dict[str, Any], greenfield: bool) -> bool:
    if greenfield:
        return True
    text = "\n".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            *[str(item) for item in _string_list(task.get("acceptance_criteria"))],
        ]
    )
    return bool(_EXPLICIT_MISSING_PATH_ACTION_RE.search(text))


def _apply_task_anchor_paths(
    *,
    current_paths: list[str],
    anchor_paths: list[str],
    field_name: str,
) -> tuple[list[str], str | None]:
    normalized_current = _dedupe_keep_order(current_paths)
    normalized_anchor = _dedupe_keep_order(anchor_paths)
    if not normalized_anchor:
        return normalized_current, None
    if not normalized_current:
        return normalized_anchor, (
            f"restored {field_name} from explicit user grounding: " + ", ".join(normalized_anchor)
        )
    if any(path in normalized_current for path in normalized_anchor):
        missing = [path for path in normalized_anchor if path not in normalized_current]
        if not missing:
            return normalized_current, None
        return normalized_current + missing, (
            f"added explicit user-grounded {field_name} entries: " + ", ".join(missing)
        )
    return normalized_anchor, (
        f"replaced {field_name} with explicit user-grounded paths: "
        + ", ".join(normalized_current)
        + " -> "
        + ", ".join(normalized_anchor)
    )


def _has_glob(path: str) -> bool:
    return any(char in path for char in _GLOB_CHARS)


def _has_framework_dynamic_route_segment(path: str) -> bool:
    try:
        parts = PurePosixPath(path.replace("\\", "/")).parts
    except ValueError:
        return False
    return any(_FRAMEWORK_DYNAMIC_ROUTE_SEGMENT_RE.fullmatch(part) for part in parts)


def _known_workspace_paths(workspace_context: dict[str, Any] | None) -> set[str]:
    if not isinstance(workspace_context, dict):
        return set()
    known: set[str] = set()
    for rel_path in _string_list(workspace_context.get("readme_paths")):
        known.add(rel_path)
    for rel_path in _string_list(workspace_context.get("observed_paths")):
        known.add(rel_path)
    conventions_path = str(workspace_context.get("conventions_path") or "").strip()
    if conventions_path:
        known.add(conventions_path)
    for entry in workspace_context.get("manifests") or []:
        if not isinstance(entry, dict):
            continue
        rel_path = str(entry.get("path") or "").strip()
        if rel_path:
            known.add(rel_path)
    for entry in workspace_context.get("top_level_entries") or []:
        if not isinstance(entry, dict):
            continue
        rel_path = str(entry.get("path") or "").strip()
        if rel_path:
            known.add(rel_path)
    return known


def _is_suspicious_path(
    *,
    path: str,
    workspace_root: Path,
    known_paths: set[str],
) -> bool:
    if path in known_paths:
        return False
    if _has_glob(path) or _has_framework_dynamic_route_segment(path):
        return False
    candidate = (workspace_root / path).resolve()
    return not candidate.exists() and not candidate.parent.exists()


def _explicit_missing_path_is_grounded_in_task_text(*, task_text: str, path: str) -> bool:
    normalized_path = str(path or "").strip().replace("\\", "/")
    if not normalized_path:
        return False
    tail = normalized_path.rsplit("/", 1)[-1]
    if "." not in tail:
        return False
    for raw_line in (task_text or "").splitlines():
        line = raw_line.strip()
        if not line or normalized_path.casefold() not in line.casefold():
            continue
        if _EXPLICIT_MISSING_PATH_ACTION_RE.search(line):
            return True
    return False


def _infer_estimated_files(
    *,
    task: dict[str, Any],
    workspace_root: Path,
    known_paths: set[str],
    allowed_missing_paths: set[str] | None = None,
    latest_user_text: str = "",
) -> tuple[list[str], list[str]]:
    title = str(task.get("title") or "").strip()
    description = str(task.get("description") or "").strip()
    acceptance = _string_list(task.get("acceptance_criteria"))
    task_text = "\n".join([title, description, *acceptance]).strip()
    hints = extract_repo_path_hints(task_text)
    forbidden_hints = {
        _path_identity_key(path)
        for path in extract_forbidden_repo_path_hints(
            "\n".join([task_text, str(latest_user_text or "")])
        )
    }
    hints, obsolete_hints = filter_obsolete_direction_paths(
        hints,
        latest_user_text=latest_user_text,
        task_text=task_text,
    )
    allowed_missing = {path for path in (allowed_missing_paths or set()) if path}
    inferred: list[str] = []
    ignored: list[str] = list(obsolete_hints)
    for hint in hints:
        if _has_glob(hint):
            continue
        if _path_identity_key(hint) in forbidden_hints:
            ignored.append(hint)
            continue
        if is_internal_alysis_path(hint) or hint == ".git" or hint.startswith(".git/"):
            ignored.append(hint)
            continue
        if hint in allowed_missing:
            inferred.append(hint)
            continue
        if _is_suspicious_path(path=hint, workspace_root=workspace_root, known_paths=known_paths):
            if _explicit_missing_path_is_grounded_in_task_text(
                task_text=task_text,
                path=hint,
            ):
                inferred.append(hint)
                continue
            ignored.append(hint)
            continue
        inferred.append(hint)
    return _dedupe_keep_order(inferred), _dedupe_keep_order(ignored)


def _task_grounding_text(task: dict[str, Any], *, latest_user_text: str = "") -> str:
    user_text = str(latest_user_text or "").strip()
    title = str(task.get("title") or "").strip()
    description = str(task.get("description") or "").strip()
    if (
        user_text
        and title == "Implement requested repository change"
        and "Use local search/read tools to locate" in description
    ):
        return user_text
    return "\n".join(
        [
            title,
            description,
            *[str(item) for item in _string_list(task.get("acceptance_criteria"))],
        ]
    )


def _task_symbol_candidates(
    task: dict[str, Any],
    *,
    latest_user_text: str = "",
) -> list[str]:
    text = _task_grounding_text(task, latest_user_text=latest_user_text)
    candidates: list[str] = []
    for raw in _SYMBOL_CANDIDATE_RE.findall(text):
        symbol = raw.strip()
        if not symbol:
            continue
        lowered = symbol.casefold()
        if lowered in _SYMBOL_CANDIDATE_STOPWORDS:
            continue
        if symbol.isupper() and "_" not in symbol:
            continue
        if "_" not in symbol and not any(char.isupper() for char in symbol[1:]):
            continue
        candidates.append(symbol)
    return _dedupe_keep_order(candidates)[:6]


def _task_filename_candidates(
    task: dict[str, Any],
    *,
    latest_user_text: str = "",
) -> list[str]:
    text = _task_grounding_text(task, latest_user_text=latest_user_text)
    candidates: list[str] = []
    for raw in _FILENAME_TOKEN_RE.findall(text):
        token = raw.strip().casefold()
        if not token or token in _SYMBOL_CANDIDATE_STOPWORDS:
            continue
        candidates.append(token)
    return _dedupe_keep_order(candidates)[:10]


def _iter_symbol_scan_files(workspace_root: Path) -> list[Path]:
    files: list[Path] = []

    def relative_parts(path: Path) -> tuple[str, ...] | None:
        try:
            parts = path.resolve().relative_to(workspace_root).parts
        except (OSError, ValueError):
            return None
        if any(part.casefold() in CODE_SCAN_SKIP_DIR_NAMES for part in parts):
            return None
        return parts

    # Filter before descent: post-filtering rglob still walks every generated
    # artifact and dependency tree. Resolve retained directories as well as
    # files so junctions cannot turn a permitted name into an outside scan.
    for directory_name, directory_names, file_names in os.walk(workspace_root, followlinks=False):
        directory = Path(directory_name)
        directory_names[:] = [
            name
            for name in directory_names
            if name.casefold() not in CODE_SCAN_SKIP_DIR_NAMES
            and relative_parts(directory / name) is not None
        ]
        for name in file_names:
            if len(files) >= _MAX_SYMBOL_SCAN_FILES:
                return files
            path = directory / name
            parts = relative_parts(path)
            if parts is None or not path.is_file():
                continue
            if not is_symbol_scannable_path("/".join(parts)):
                continue
            files.append(path)
        if len(files) >= _MAX_SYMBOL_SCAN_FILES:
            break
    return files


def _symbol_definition_regex(symbol: str, suffix: str) -> re.Pattern[str]:
    return symbol_definition_regex(symbol, suffix)


def _file_text(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_SYMBOL_SCAN_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _resolve_symbol_grounded_paths(
    *,
    task: dict[str, Any],
    workspace_root: Path,
    latest_user_text: str = "",
) -> list[str]:
    symbols = _task_symbol_candidates(task, latest_user_text=latest_user_text)
    if not symbols:
        return []

    paths_by_symbol: dict[str, list[str]] = {symbol: [] for symbol in symbols}
    for path in _iter_symbol_scan_files(workspace_root):
        text = _file_text(path)
        if not text:
            continue
        suffix = path.suffix.casefold()
        for symbol in symbols:
            if not _symbol_definition_regex(symbol, suffix).search(text):
                continue
            try:
                rel_path = path.resolve().relative_to(workspace_root).as_posix()
            except (OSError, ValueError):
                continue
            paths_by_symbol[symbol].append(rel_path)

    grounded: list[str] = []
    for symbol in symbols:
        symbol_paths = _dedupe_keep_order(paths_by_symbol.get(symbol, []))
        if len(symbol_paths) == 1:
            grounded.extend(symbol_paths)
    return _dedupe_keep_order(grounded)[:3]


def _resolve_named_code_file_paths(
    *,
    task: dict[str, Any],
    workspace_root: Path,
    latest_user_text: str = "",
) -> list[str]:
    candidates = _task_filename_candidates(task, latest_user_text=latest_user_text)
    if not candidates:
        return []

    paths_by_stem: dict[str, list[str]] = {candidate: [] for candidate in candidates}
    for path in _iter_symbol_scan_files(workspace_root):
        stem = path.stem.casefold()
        if stem not in paths_by_stem:
            continue
        try:
            rel_path = path.resolve().relative_to(workspace_root).as_posix()
        except (OSError, ValueError):
            continue
        if not is_code_implementation_path(rel_path):
            continue
        paths_by_stem[stem].append(rel_path)

    grounded: list[str] = []
    for candidate in candidates:
        candidate_paths = _dedupe_keep_order(paths_by_stem.get(candidate, []))
        if len(candidate_paths) == 1:
            grounded.extend(candidate_paths)
    return _dedupe_keep_order(grounded)[:3]


def _is_concrete_code_implementation_path(path: str) -> bool:
    return not _has_glob(path) and is_code_implementation_path(path)


def _is_code_implementation_path(path: str) -> bool:
    return is_code_implementation_path(path)


def _path_contains_any_task_symbol(
    *,
    workspace_root: Path,
    path: str,
    task: dict[str, Any],
) -> bool:
    symbols = _task_symbol_candidates(task)
    if not symbols:
        return False
    candidate = workspace_root / path
    text = _file_text(candidate)
    if not text:
        return False
    return any(re.search(rf"\b{re.escape(symbol)}\b", text) for symbol in symbols)


def _apply_symbol_grounded_paths(
    *,
    current_paths: list[str],
    symbol_paths: list[str],
    workspace_root: Path,
    task: dict[str, Any],
    field_name: str,
    protected_missing_path_identities: set[str] | None = None,
) -> tuple[list[str], str | None]:
    normalized_current = _dedupe_keep_order(current_paths)
    normalized_symbol_paths = _dedupe_keep_order(symbol_paths)
    if not normalized_symbol_paths:
        return normalized_current, None
    if any(path in normalized_current for path in normalized_symbol_paths):
        return normalized_current, None
    if all(
        any(
            scope_path_matches_pattern(symbol_path, current_path, root=workspace_root)
            for current_path in normalized_current
        )
        for symbol_path in normalized_symbol_paths
    ):
        return normalized_current, None

    implementation_paths = [
        path for path in normalized_current if _is_concrete_code_implementation_path(path)
    ]
    protected_identities = protected_missing_path_identities or set()
    protected_missing_paths = [
        path for path in implementation_paths if _path_identity_key(path) in protected_identities
    ]
    if protected_missing_paths:
        return normalized_current, (
            f"kept explicit missing {field_name} path(s) and did not retarget them to "
            "repository symbol definition path(s): "
            + ", ".join(protected_missing_paths)
            + " (candidate: "
            + ", ".join(normalized_symbol_paths)
            + ")"
        )
    if implementation_paths and any(
        _path_contains_any_task_symbol(
            workspace_root=workspace_root,
            path=path,
            task=task,
        )
        for path in implementation_paths
    ):
        return normalized_current, None

    if implementation_paths:
        replacement: list[str] = []
        inserted_symbols = False
        for path in normalized_current:
            if _is_concrete_code_implementation_path(path):
                if not inserted_symbols:
                    replacement.extend(normalized_symbol_paths)
                    inserted_symbols = True
                continue
            replacement.append(path)
        replacement = _dedupe_keep_order(replacement)
        return replacement, (
            f"replaced symbol-mismatched {field_name} entries with repository symbol "
            "definition path(s): "
            + ", ".join(implementation_paths)
            + " -> "
            + ", ".join(normalized_symbol_paths)
        )

    replacement = _dedupe_keep_order([*normalized_current, *normalized_symbol_paths])
    return replacement, (
        f"added repository symbol definition path(s) to {field_name}: "
        + ", ".join(normalized_symbol_paths)
    )


def _drop_ungrounded_suspicious_paths(
    *,
    paths: list[str],
    workspace_root: Path,
    known_paths: set[str],
    allowed_missing_paths: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    allowed_missing = {path for path in (allowed_missing_paths or set()) if path}
    if not allowed_missing:
        return _dedupe_keep_order(paths), []

    kept: list[str] = []
    dropped: list[str] = []
    for path in _dedupe_keep_order(paths):
        if path in allowed_missing:
            kept.append(path)
            continue
        if _is_suspicious_path(path=path, workspace_root=workspace_root, known_paths=known_paths):
            dropped.append(path)
            continue
        kept.append(path)
    return kept, dropped
