from __future__ import annotations

import logging
import os
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .knowledge_capture import render_execution_knowledge_capture_rules
from .model_registry import ModelRegistry
from .project_data import ProjectDataError, project_artifact_path
from .token_budget import compute_input_budget, estimate_tokens

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_ASSET_FALLBACK_COUNT = 6
_IMAGE_FALLBACK_COUNT = 4
_DEFAULT_EXECUTION_PLAN_REQUIREMENTS_COUNT = 12
_DEFAULT_EXECUTION_PLAN_TASK_COUNT = 16
_DEFAULT_EXECUTION_ASSET_COUNT = 12
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExecutionPackReductionState:
    requirement_count: int
    task_list_count: int
    asset_count: int
    project_goal_chars: int
    summary_chars: int
    relevant_assets_full_inline_count: int
    relevant_assets_focused_count: int
    relevant_assets_reference_count: int
    include_may_need_section: bool
    include_pinned_section: bool
    strategy: str


@dataclass(frozen=True)
class TaskContextPackResult:
    content: str
    artifact_text: str
    instruction_token_budget: int
    instruction_token_estimate: int
    truncated: bool
    truncation_strategy: str


class ExecutionContextBudgetError(RuntimeError):
    """No complete execution instruction can fit the declared instruction budget."""

    error_code = "execution_context_budget_exceeded"

    def __init__(self, *, available_tokens: int, required_tokens: int) -> None:
        self.available_tokens = max(0, int(available_tokens))
        self.required_tokens = max(0, int(required_tokens))
        super().__init__(
            "Execution context budget is too small: the complete task, scope, and execution "
            f"instructions need {self.required_tokens} estimated tokens; "
            f"only {self.available_tokens} are available. "
            "Increase the context allowance or shorten the task/required context before retrying. "
            "No shortened execution instruction was produced."
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "available_instruction_tokens": self.available_tokens,
            "required_instruction_tokens": self.required_tokens,
            "error": str(self),
        }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _task_text_blob(task: dict[str, Any]) -> str:
    text_parts = [
        str(task.get("title") or ""),
        str(task.get("description") or ""),
        *[str(x) for x in _string_list(task.get("acceptance_criteria"))],
    ]
    return " ".join(text_parts).strip().lower()


def _scope_entries(task: dict[str, Any]) -> list[str]:
    return [
        item.lower()
        for item in (
            _string_list(task.get("estimated_files")) + _string_list(task.get("write_scope"))
        )
    ]


def compact_plan_for_execution(plan: dict[str, Any]) -> dict[str, Any]:
    tasks_compact: list[dict[str, Any]] = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tasks_compact.append(
            {
                "id": str(task.get("id") or "").strip(),
                "title": str(task.get("title") or "").strip(),
                "deps": _string_list(task.get("dependencies")),
            }
        )

    requirements = _string_list(plan.get("requirements"))
    assets_compact: list[dict[str, str]] = []
    assets_raw = plan.get("assets") or []
    uses_legacy_assets = _uses_legacy_assets(plan)
    if uses_legacy_assets and isinstance(assets_raw, list):
        for asset in assets_raw[:25]:
            if not isinstance(asset, dict):
                continue
            assets_compact.append(
                {
                    "stored_path": str(asset.get("stored_path") or "").strip(),
                    "text_copy_path": str(asset.get("text_copy_path") or "").strip(),
                }
            )

    return {
        "schema_version": _plan_schema_version(plan),
        "project_goal": str(plan.get("project_goal") or "").strip(),
        "summary": str(plan.get("summary") or "").strip(),
        "requirements_tail": requirements[-20:],
        "tasks": tasks_compact,
        "assets": assets_compact,
        "uses_legacy_assets": uses_legacy_assets,
    }


def _asset_relevance_score(asset: dict[str, Any], *, task: dict[str, Any]) -> int:
    stored_path = str(asset.get("stored_path") or "").strip()
    text_copy_path = str(asset.get("text_copy_path") or "").strip()
    basename = Path(stored_path).name.lower()
    text_basename = Path(text_copy_path).name.lower()
    stem = Path(stored_path).stem.lower()
    task_text = _task_text_blob(task)
    scopes = _scope_entries(task)

    score = 0
    if basename and basename in task_text:
        score += 6
    if text_basename and text_basename in task_text:
        score += 5
    if stem and stem in task_text:
        score += 4

    for scope in scopes:
        if not scope:
            continue
        if basename and (basename in scope or scope in basename):
            score += 3
        if stem and stem in scope:
            score += 2
        if text_basename and text_basename in scope:
            score += 2

    return score


def select_relevant_assets(plan: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    if not _uses_legacy_assets(plan):
        return []
    warnings.warn(
        "select_relevant_assets() is deprecated. Use task asset_briefing and the "
        "first-class AssetIndex-based asset system.",
        DeprecationWarning,
        stacklevel=2,
    )
    LOGGER.warning("select_relevant_assets deprecated legacy_flow=true")
    assets = [asset for asset in (plan.get("assets") or []) if isinstance(asset, dict)]
    if not assets:
        return []

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, asset in enumerate(assets):
        score = _asset_relevance_score(asset, task=task)
        scored.append((score, idx, asset))

    matches = [item for item in scored if item[0] > 0]
    if matches:
        matches.sort(key=lambda item: (-item[0], item[1]))
        return [asset for _score, _idx, asset in matches]

    return assets[-_ASSET_FALLBACK_COUNT:]


def _is_image_asset(asset: dict[str, Any]) -> bool:
    stored_path = str(asset.get("stored_path") or "").strip()
    if not stored_path:
        return False
    return Path(stored_path).suffix.lower() in _IMAGE_EXTENSIONS


def select_relevant_image_paths(
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    max_images: int = _IMAGE_FALLBACK_COUNT,
) -> list[str]:
    if not _uses_legacy_assets(plan):
        return []
    warnings.warn(
        "select_relevant_image_paths() is deprecated. Use cfg.assets.worker.inline_images "
        "with first-class task asset bindings.",
        DeprecationWarning,
        stacklevel=2,
    )
    LOGGER.warning("select_relevant_image_paths deprecated legacy_flow=true")
    selected_assets = [
        asset for asset in select_relevant_assets(plan, task) if _is_image_asset(asset)
    ]
    if not selected_assets:
        all_images = [
            asset
            for asset in (plan.get("assets") or [])
            if isinstance(asset, dict) and _is_image_asset(asset)
        ]
        selected_assets = all_images[-max_images:]

    paths: list[str] = []
    seen: set[str] = set()
    root_resolved = root.resolve()
    for asset in selected_assets:
        stored_path = str(asset.get("stored_path") or "").strip()
        if not stored_path:
            continue
        try:
            candidate = project_artifact_path(root, stored_path).resolve()
            candidate.relative_to(root_resolved)
        except (ValueError, ProjectDataError):
            continue
        if not candidate.exists() or not candidate.is_file():
            continue
        path_s = os.fspath(candidate)
        if path_s in seen:
            continue
        seen.add(path_s)
        paths.append(path_s)
        if len(paths) >= max_images:
            break
    return paths


def _truncate_value(text: str, max_chars: int) -> str:
    raw = text.strip()
    if not raw:
        return ""
    if max_chars <= 0:
        return "(omitted to fit budget)"
    if len(raw) <= max_chars:
        return raw
    if max_chars <= 3:
        return raw[:max_chars]
    return raw[: max_chars - 3].rstrip() + "..."


def _render_plan_lines(
    *,
    compact: dict[str, Any],
    state: _ExecutionPackReductionState,
) -> list[str]:
    plan_lines: list[str] = [
        "## Plan Summary",
        "",
        f"- Project Goal: {_truncate_value(str(compact.get('project_goal') or ''), state.project_goal_chars) or '(not set)'}",
        f"- Summary: {_truncate_value(str(compact.get('summary') or ''), state.summary_chars) or '(not set)'}",
        "- Recent Requirements:",
    ]
    requirements_tail = list(compact.get("requirements_tail") or [])
    if state.requirement_count > 0 and requirements_tail:
        selected_requirements = requirements_tail[-state.requirement_count :]
        for req in selected_requirements:
            plan_lines.append(f"  - {req}")
        omitted_requirements = len(requirements_tail) - len(selected_requirements)
        if omitted_requirements > 0:
            plan_lines.append(f"  - ... ({omitted_requirements} earlier requirements omitted)")
    else:
        plan_lines.append("  - (omitted to fit budget)" if requirements_tail else "  - (none)")

    plan_lines.append("- Task List:")
    tasks = list(compact.get("tasks") or [])
    if state.task_list_count > 0 and tasks:
        selected_tasks = tasks[: state.task_list_count]
        for item in selected_tasks:
            deps = ", ".join(item.get("deps") or []) or "(none)"
            plan_lines.append(f"  - {item.get('id', '')}: {item.get('title', '')} (deps: {deps})")
        omitted_tasks = len(tasks) - len(selected_tasks)
        if omitted_tasks > 0:
            plan_lines.append(f"  - ... ({omitted_tasks} additional tasks omitted)")
    else:
        plan_lines.append("  - (omitted to fit budget)" if tasks else "  - (none)")
    return plan_lines


def _render_assets_lines(
    *,
    selected_assets: list[dict[str, Any]],
    asset_count: int,
    plan_schema_version: int,
    uses_legacy_assets: bool,
) -> list[str]:
    if not uses_legacy_assets or plan_schema_version >= 2:
        if selected_assets:
            LOGGER.warning(
                "legacy_selected_assets suppressed for schema_version=%s", plan_schema_version
            )
        return []
    assets_lines: list[str] = ["## Selected Assets", ""]
    if not selected_assets:
        assets_lines.append("- (none)")
        assets_lines.append(
            "- If text_copy is available, read that file for extracted text details."
        )
        return assets_lines

    effective_count = max(1, min(max(0, int(asset_count)), len(selected_assets)))
    shown_assets = selected_assets[:effective_count]
    for asset in shown_assets:
        stored = str(asset.get("stored_path") or "").strip() or "(missing stored_path)"
        text_copy = str(asset.get("text_copy_path") or "").strip() or "(none)"
        assets_lines.append(f"- stored={stored}; text_copy={text_copy}")
    omitted_assets = len(selected_assets) - len(shown_assets)
    if omitted_assets > 0:
        assets_lines.append(f"- ... ({omitted_assets} additional assets omitted)")
    assets_lines.append("- If text_copy is available, read that file for extracted text details.")
    return assets_lines


def _plan_schema_version(plan: dict[str, Any]) -> int:
    try:
        return int(plan.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _uses_legacy_assets(plan: dict[str, Any]) -> bool:
    if _plan_schema_version(plan) >= 2:
        return False
    for task in plan.get("tasks") or []:
        if isinstance(task, dict) and "asset_briefing" in task:
            return False
    return True


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _render_task_lines(
    *,
    task: dict[str, Any],
) -> list[str]:
    task_lines: list[str] = [
        "## Task Specification",
        "",
        f"- ID: `{str(task.get('id') or '').strip()}`",
        f"- Title: {str(task.get('title') or '').strip() or '(none)'}",
        "- Description: " + (str(task.get("description") or "").strip() or "(none)"),
        (
            "- Acceptance Criteria: "
            + (", ".join(_string_list(task.get("acceptance_criteria"))) or "(none)")
        ),
        "- Dependencies: " + (", ".join(_string_list(task.get("dependencies"))) or "(none)"),
        (
            "- Estimated Files: "
            + (", ".join(_string_list(task.get("estimated_files"))) or "(none)")
        ),
        ("- Write Scope: " + (", ".join(_string_list(task.get("write_scope"))) or "(none)")),
        f"- Branch: `{str(task.get('branch') or '').strip() or '(not set)'}`",
        f"- Status: `{str(task.get('status') or '').strip() or '(unknown)'}`",
    ]
    return task_lines


def _reduce_relevant_assets_section(
    section: str,
    *,
    state: _ExecutionPackReductionState,
) -> list[str]:
    text = str(section or "").strip()
    if not text:
        return []
    if not state.include_may_need_section:
        text = _drop_named_section(text, "May-need assets")
    if not state.include_pinned_section:
        text = _drop_named_section(text, "Pinned assets")
    if state.relevant_assets_full_inline_count <= 0 and state.relevant_assets_focused_count <= 0:
        text = _drop_asset_content_blocks(text)
    text = _reduce_relevant_asset_blocks(text, state=state)
    return [line.rstrip() for line in text.splitlines()]


def _drop_named_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip().startswith(heading):
            skipping = True
            continue
        if skipping and line.startswith("### "):
            continue
        if skipping and line.endswith(":") and not line.startswith("- "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).strip()


def _drop_asset_content_blocks(text: str) -> str:
    return re.sub(
        r"\n?<asset_content\b[^>]*>.*?</asset_content>\n?",
        "\n",
        text,
        flags=re.DOTALL,
    ).strip()


def _reduce_relevant_asset_blocks(text: str, *, state: _ExecutionPackReductionState) -> str:
    prelude, blocks = _split_relevant_asset_blocks(text)
    if not blocks:
        return text
    remaining_blocks = max(0, int(state.relevant_assets_reference_count))
    if remaining_blocks <= 0:
        return ""
    remaining_full = max(0, int(state.relevant_assets_full_inline_count))
    remaining_focused = max(0, int(state.relevant_assets_focused_count))
    out: list[str] = list(prelude)
    for block in blocks:
        if remaining_blocks <= 0:
            continue
        remaining_blocks -= 1
        mode = _asset_block_mode(block)
        block_text = "\n".join(block).strip()
        if mode == "full_inline":
            if remaining_full > 0:
                remaining_full -= 1
            else:
                block_text = _downgrade_asset_block_to_reference(block_text)
        elif mode == "focused_extract":
            if remaining_focused > 0:
                remaining_focused -= 1
            else:
                block_text = _downgrade_asset_block_to_reference(block_text)
        if block_text:
            if out and out[-1].strip():
                out.append("")
            out.extend(block_text.splitlines())
    return "\n".join(out).strip()


def _split_relevant_asset_blocks(text: str) -> tuple[list[str], list[list[str]]]:
    prelude: list[str] = []
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("### "):
            if current is not None:
                blocks.append(current)
            current = [line]
            continue
        if current is None:
            prelude.append(line)
        else:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return prelude, blocks


def _asset_block_mode(block: list[str]) -> str:
    for line in block:
        if line.startswith("- Mode: full_inline"):
            return "full_inline"
        if line.startswith("- Mode: focused_extract"):
            return "focused_extract"
        if line.startswith("- Mode: reference_only"):
            return "reference_only"
    return "summary"


def _downgrade_asset_block_to_reference(block_text: str) -> str:
    without_content = _drop_asset_content_blocks(block_text)
    lines: list[str] = []
    replaced = False
    for line in without_content.splitlines():
        if line.startswith("- Mode: "):
            lines.append(
                "- Mode: reference_only - inline content omitted by budget reduction; "
                "consult asset_read if needed"
            )
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.insert(
            1 if lines else 0,
            "- Mode: reference_only - inline content omitted by budget reduction; consult asset_read if needed",
        )
    return "\n".join(lines).strip()


def _relevant_asset_counts(section: str | None) -> tuple[int, int, int]:
    text = str(section or "")
    if not text.strip():
        return 0, 0, 0
    blocks = len(re.findall(r"^### ", text, flags=re.MULTILINE))
    full = len(re.findall(r"^- Mode: full_inline", text, flags=re.MULTILINE))
    focused = len(re.findall(r"^- Mode: focused_extract", text, flags=re.MULTILINE))
    return blocks, full, focused


def _render_execution_pack(
    *,
    role_model: str,
    resolved_budget: int,
    compact: dict[str, Any],
    selected_assets: list[dict[str, Any]],
    task: dict[str, Any],
    state: _ExecutionPackReductionState,
    note: str | None,
    leading_sections: list[str] | None = None,
    relevant_assets_section: str | None = None,
    include_supplementary_context: bool = True,
) -> str:
    assets_lines = _render_assets_lines(
        selected_assets=selected_assets,
        asset_count=state.asset_count,
        plan_schema_version=_safe_int(compact.get("schema_version"), default=1),
        uses_legacy_assets=bool(compact.get("uses_legacy_assets")),
    )
    task_lines = _render_task_lines(task=task)
    plan_lines = _render_plan_lines(compact=compact, state=state)
    rules_lines: list[str] = [
        "## Execution Rules",
        "",
        "- Implement ONLY this task; do not start other tasks.",
        "- Read files before making claims.",
        "- Prefer minimal coherent changes.",
        "- If tests exist, run them.",
        "- Do not modify anything under .alysis/ (plan artifacts and run state are read-only).",
        "- You may read attached plan assets as needed; do NOT modify anything under .alysis/.",
        "- Do not run git branch/checkout/add/commit/reset commands; branch management and commits are handled by the swarm runner.",
        *render_execution_knowledge_capture_rules(),
    ]
    header_lines = [
        "# Task Context Pack",
        "",
        f"- Model: `{role_model}`",
        f"- Instruction budget (tokens): `{resolved_budget}`",
    ]
    if note:
        header_lines.extend(["", note])
    leading_lines: list[str] = []
    for section in leading_sections or []:
        section_text = section.strip()
        if not section_text:
            continue
        if leading_lines:
            leading_lines.append("")
        leading_lines.extend(section_text.splitlines())
    relevant_assets_lines = _reduce_relevant_assets_section(
        relevant_assets_section or "",
        state=state,
    )
    if not include_supplementary_context:
        assets_lines = []
        plan_lines = []
        relevant_assets_lines = []

    pack = (
        "\n".join(
            [
                *header_lines,
                *(["", *leading_lines] if leading_lines else []),
                *(["", *relevant_assets_lines] if relevant_assets_lines else []),
                "",
                *assets_lines,
                "",
                *task_lines,
                "",
                *plan_lines,
                "",
                *rules_lines,
                "",
            ]
        ).rstrip()
        + "\n"
    )
    return pack


def _reduction_candidates(
    *,
    compact: dict[str, Any],
    selected_assets: list[dict[str, Any]],
    relevant_assets_section: str | None = None,
    prefer_startup_headroom_reduction: bool = False,
) -> list[_ExecutionPackReductionState]:
    requirements = list(compact.get("requirements_tail") or [])
    tasks = list(compact.get("tasks") or [])
    assets_count = len(selected_assets)
    relevant_asset_count, relevant_full_count, relevant_focused_count = _relevant_asset_counts(
        relevant_assets_section
    )

    def _cap(total: int, wanted: int, *, minimum_when_nonempty: int = 0) -> int:
        if total <= 0:
            return 0
        capped = min(total, max(0, int(wanted)))
        if capped <= 0:
            return minimum_when_nonempty
        return capped

    candidates = [
        _ExecutionPackReductionState(
            requirement_count=_cap(len(requirements), _DEFAULT_EXECUTION_PLAN_REQUIREMENTS_COUNT),
            task_list_count=_cap(len(tasks), _DEFAULT_EXECUTION_PLAN_TASK_COUNT),
            asset_count=_cap(assets_count, _DEFAULT_EXECUTION_ASSET_COUNT, minimum_when_nonempty=1),
            project_goal_chars=240,
            summary_chars=600,
            relevant_assets_full_inline_count=relevant_full_count,
            relevant_assets_focused_count=relevant_focused_count,
            relevant_assets_reference_count=relevant_asset_count,
            include_may_need_section=True,
            include_pinned_section=True,
            strategy="execution_priority",
        ),
        _ExecutionPackReductionState(
            requirement_count=_cap(len(requirements), 8),
            task_list_count=_cap(len(tasks), 10),
            asset_count=_cap(assets_count, _DEFAULT_EXECUTION_ASSET_COUNT, minimum_when_nonempty=1),
            project_goal_chars=160,
            summary_chars=320,
            relevant_assets_full_inline_count=relevant_full_count,
            relevant_assets_focused_count=relevant_focused_count,
            relevant_assets_reference_count=relevant_asset_count,
            include_may_need_section=True,
            include_pinned_section=True,
            strategy="execution_priority_reduced_plan",
        ),
        _ExecutionPackReductionState(
            requirement_count=_cap(len(requirements), 4),
            task_list_count=_cap(len(tasks), 5),
            asset_count=_cap(assets_count, 8, minimum_when_nonempty=1),
            project_goal_chars=96,
            summary_chars=160,
            relevant_assets_full_inline_count=relevant_full_count,
            relevant_assets_focused_count=relevant_focused_count,
            relevant_assets_reference_count=relevant_asset_count,
            include_may_need_section=False,
            include_pinned_section=True,
            strategy="execution_priority_reduced_plan_detail",
        ),
        _ExecutionPackReductionState(
            requirement_count=_cap(len(requirements), 2),
            task_list_count=_cap(len(tasks), 3),
            asset_count=_cap(assets_count, 4, minimum_when_nonempty=1),
            project_goal_chars=64,
            summary_chars=96,
            relevant_assets_full_inline_count=0,
            relevant_assets_focused_count=relevant_focused_count,
            relevant_assets_reference_count=relevant_asset_count,
            include_may_need_section=False,
            include_pinned_section=True,
            strategy="execution_priority_reduced_plan_and_assets",
        ),
        _ExecutionPackReductionState(
            requirement_count=0,
            task_list_count=0,
            asset_count=_cap(assets_count, 2, minimum_when_nonempty=1),
            project_goal_chars=0,
            summary_chars=0,
            relevant_assets_full_inline_count=0,
            relevant_assets_focused_count=0,
            relevant_assets_reference_count=relevant_asset_count,
            include_may_need_section=False,
            include_pinned_section=False,
            strategy="execution_priority_minimal_plan",
        ),
        _ExecutionPackReductionState(
            requirement_count=0,
            task_list_count=0,
            asset_count=_cap(assets_count, 1, minimum_when_nonempty=1),
            project_goal_chars=0,
            summary_chars=0,
            relevant_assets_full_inline_count=0,
            relevant_assets_focused_count=0,
            relevant_assets_reference_count=min(relevant_asset_count, 1),
            include_may_need_section=False,
            include_pinned_section=False,
            strategy="execution_priority_minimal",
        ),
    ]
    if prefer_startup_headroom_reduction:
        # Managed execution startup headroom can require a smaller first-step
        # pack than the normal hard-limit budget path, so append stricter
        # candidates instead of widening the default reduction for all callers.
        candidates.extend(
            [
                _ExecutionPackReductionState(
                    requirement_count=0,
                    task_list_count=0,
                    asset_count=_cap(assets_count, 1, minimum_when_nonempty=1),
                    project_goal_chars=0,
                    summary_chars=0,
                    relevant_assets_full_inline_count=0,
                    relevant_assets_focused_count=0,
                    relevant_assets_reference_count=min(relevant_asset_count, 1),
                    include_may_need_section=False,
                    include_pinned_section=False,
                    strategy="execution_priority_startup_headroom",
                ),
                _ExecutionPackReductionState(
                    requirement_count=0,
                    task_list_count=0,
                    asset_count=0,
                    project_goal_chars=0,
                    summary_chars=0,
                    relevant_assets_full_inline_count=0,
                    relevant_assets_focused_count=0,
                    relevant_assets_reference_count=0,
                    include_may_need_section=False,
                    include_pinned_section=False,
                    strategy="execution_priority_startup_headroom_no_assets",
                ),
                _ExecutionPackReductionState(
                    requirement_count=0,
                    task_list_count=0,
                    asset_count=0,
                    project_goal_chars=0,
                    summary_chars=0,
                    relevant_assets_full_inline_count=0,
                    relevant_assets_focused_count=0,
                    relevant_assets_reference_count=0,
                    include_may_need_section=False,
                    include_pinned_section=False,
                    strategy="execution_priority_startup_headroom_minimal",
                ),
            ]
        )
    return candidates


def build_task_context_pack(
    *,
    cfg: AppConfig,
    plan: dict[str, Any],
    task: dict[str, Any],
    role_model: str,
    model_registry: ModelRegistry | None = None,
    instruction_token_budget: int | None = None,
    leading_sections: list[str] | None = None,
    relevant_assets_section: str | None = None,
    prefer_startup_headroom_reduction: bool = False,
) -> str:
    return build_task_context_pack_result(
        cfg=cfg,
        plan=plan,
        task=task,
        role_model=role_model,
        model_registry=model_registry,
        instruction_token_budget=instruction_token_budget,
        leading_sections=leading_sections,
        relevant_assets_section=relevant_assets_section,
        prefer_startup_headroom_reduction=prefer_startup_headroom_reduction,
    ).content


def build_task_context_pack_result(
    *,
    cfg: AppConfig,
    plan: dict[str, Any],
    task: dict[str, Any],
    role_model: str,
    model_registry: ModelRegistry | None = None,
    instruction_token_budget: int | None = None,
    leading_sections: list[str] | None = None,
    relevant_assets_section: str | None = None,
    prefer_startup_headroom_reduction: bool = False,
    instruction_token_counter: Callable[[str], int] | None = None,
) -> TaskContextPackResult:
    if instruction_token_budget is None:
        registry = model_registry or ModelRegistry(cfg=cfg)
        meta = registry.get(role_model)
        resolved_budget = compute_input_budget(meta)
    else:
        resolved_budget = max(0, int(instruction_token_budget))
    # Bundled execution supplies the existing prepared-request estimator so
    # message serialization overhead is charged to the instruction allowance.
    count_tokens = instruction_token_counter or estimate_tokens
    compact = compact_plan_for_execution(plan)
    selected_assets = select_relevant_assets(plan, task)

    reduction_candidates = _reduction_candidates(
        compact=compact,
        selected_assets=selected_assets,
        relevant_assets_section=relevant_assets_section,
        prefer_startup_headroom_reduction=prefer_startup_headroom_reduction,
    )
    for idx, state in enumerate(reduction_candidates):
        note = None
        if idx > 0:
            note = (
                "> NOTE: Context pack TRUNCATED using execution-priority reduction to preserve "
                "complete Task Specification, required context, and Execution Rules."
            )
        candidate = _render_execution_pack(
            role_model=role_model,
            resolved_budget=resolved_budget,
            compact=compact,
            selected_assets=selected_assets,
            task=task,
            state=state,
            note=note,
            leading_sections=leading_sections,
            relevant_assets_section=relevant_assets_section,
        )
        candidate_tokens = count_tokens(candidate)
        if candidate_tokens <= resolved_budget:
            return TaskContextPackResult(
                content=candidate,
                artifact_text=candidate,
                instruction_token_budget=resolved_budget,
                instruction_token_estimate=candidate_tokens,
                truncated=idx > 0,
                truncation_strategy=state.strategy,
            )

    # Task/permission constraints cannot be reduced by their text or position.
    # The last fallback drops whole supplementary sections, never required text.
    mandatory_pack = _render_execution_pack(
        role_model=role_model,
        resolved_budget=resolved_budget,
        compact=compact,
        selected_assets=selected_assets,
        task=task,
        state=reduction_candidates[-1],
        note=(
            "> NOTE: Supplementary plan and asset context TRUNCATED to fit budget; "
            "the task specification, required context, and execution rules are complete."
        ),
        leading_sections=leading_sections,
        include_supplementary_context=False,
    )
    required_tokens = count_tokens(mandatory_pack)
    if required_tokens > resolved_budget:
        raise ExecutionContextBudgetError(
            available_tokens=resolved_budget,
            required_tokens=required_tokens,
        )

    return TaskContextPackResult(
        content=mandatory_pack,
        artifact_text=mandatory_pack,
        instruction_token_budget=resolved_budget,
        instruction_token_estimate=required_tokens,
        truncated=True,
        truncation_strategy="execution_priority_mandatory_only",
    )
