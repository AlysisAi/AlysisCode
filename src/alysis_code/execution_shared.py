from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .agent.prompt_context import _build_user_message
from .branding import env_get
from .command_text import command_text
from .compaction.settings import resolve_compaction_settings
from .config import AppConfig
from .execution_budget import (
    DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS,
    DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS,
    DEFAULT_EXECUTION_SAFETY_MARGIN_TOKENS,
    DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS,
    ExecutionPromptBudget,
    compute_execution_prompt_budget_inputs,
)
from .execution_context import (
    ExecutionContextBudgetError,
    build_task_context_pack,
    build_task_context_pack_result,
    select_relevant_assets,
    select_relevant_image_paths,
)
from .forge import RunPaths, ensure_execution_dirs
from .git_ops import GitOpsError, changed_files_since, diff_text_since
from .git_safe import build_git_process_env
from .knowledge_librarian import MaterializedKnowledgeSelection, prepare_relevant_knowledge
from .model_registry import ModelRegistry
from .request_estimation import estimate_request_tokens
from .runtime_artifacts import (
    ROOT_RUNTIME_ARTIFACT_DIR_NAMES,
    RUNTIME_ARTIFACT_DIR_NAMES,
    is_runtime_artifact_path,
)
from .serialized_paths import safe_serialized_path
from .session_store import resolve_sessions_dir, sanitize_session_id
from .step_budget import (
    AUTONOMOUS_STEP_BUDGET_POLICY,
    DEFAULT_TASK_MAX_STEPS,
    StepBudgetRequest,
    StepBudgetResolution,
    resolve_step_budget,
)
from .task_scope import is_non_material_untracked_path
from .token_budget import compute_input_budget

_SNAPSHOT_HASH_MAX_BYTES = 1 * 1024 * 1024
# Legacy env gate. The new asset system supersedes this and is enabled by
# default via cfg.assets.worker.inline_images. The env var continues to control
# the legacy plan["assets"][]-based image path injection until those code paths
# are removed in a future major version.
_TASK_IMAGES_ENV = "ALYSIS_TASK_IMAGES"
_DEFAULT_SNAPSHOT_EXCLUDES = RUNTIME_ARTIFACT_DIR_NAMES
_READ_ONLY_GIT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class PreparedTaskExecutionInstruction:
    instruction: str
    artifact_text: str
    budget: ExecutionPromptBudget
    final_instruction_token_estimate: int
    truncated: bool
    truncation_strategy: str
    subagents_enabled: bool
    image_paths: tuple[str, ...]
    startup_headroom: StartupHeadroomTelemetry | None = None

    def to_budget_artifact_payload(self) -> dict[str, Any]:
        payload = dict(self.budget.to_payload())
        payload["final_instruction_token_estimate"] = self.final_instruction_token_estimate
        payload["truncated"] = self.truncated
        payload["truncation_strategy"] = self.truncation_strategy
        if self.startup_headroom is not None:
            payload.update(self.startup_headroom.to_payload())
        return payload


@dataclass(frozen=True)
class StartupHeadroomTelemetry:
    initial_request_token_estimate: int
    initial_request_token_estimate_before_adjustment: int | None
    compaction_budget_tokens: int
    compaction_trigger_tokens: int
    startup_target_tokens: int
    startup_headroom_tokens: int
    startup_headroom_adjustment_applied: bool
    startup_headroom_adjustment_reason: str | None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "initial_request_token_estimate": self.initial_request_token_estimate,
            "compaction_budget_tokens": self.compaction_budget_tokens,
            "compaction_trigger_tokens": self.compaction_trigger_tokens,
            "startup_target_tokens": self.startup_target_tokens,
            "startup_headroom_tokens": self.startup_headroom_tokens,
            "startup_headroom_adjustment_applied": self.startup_headroom_adjustment_applied,
            "startup_headroom_adjustment_reason": self.startup_headroom_adjustment_reason,
        }
        if self.initial_request_token_estimate_before_adjustment is not None:
            payload["initial_request_token_estimate_before_adjustment"] = (
                self.initial_request_token_estimate_before_adjustment
            )
        return payload


@dataclass(frozen=True)
class PreparedExecutionKnowledge:
    materialized: MaterializedKnowledgeSelection
    prompt_section: str

    @property
    def manifest_path(self) -> Path:
        return self.materialized.manifest_path

    @property
    def selected_dir(self) -> Path:
        return self.materialized.selected_dir


def _task_execution_text(task: dict[str, Any] | None) -> str:
    if not isinstance(task, dict):
        return ""
    parts: list[str] = []
    for key in ("title", "description"):
        value = task.get(key)
        if value:
            parts.append(str(value))
    for key in ("acceptance_criteria", "estimated_files", "write_scope"):
        value = task.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
    return "\n".join(parts)


def _scope_patterns_are_test_only(patterns: list[str] | None, task: dict[str, Any] | None) -> bool:
    raw_patterns = list(patterns or [])
    if not raw_patterns and isinstance(task, dict):
        raw_patterns = [str(item) for item in task.get("write_scope") or []]
    normalized = [pattern.strip().replace("\\", "/").lower() for pattern in raw_patterns]
    normalized = [pattern for pattern in normalized if pattern]
    if not normalized:
        return False

    def _is_test_pattern(pattern: str) -> bool:
        leaf = pattern.rstrip("/").rsplit("/", 1)[-1]
        return (
            pattern.startswith("tests/")
            or pattern.startswith("test/")
            or "/tests/" in pattern
            or "/test/" in pattern
            or leaf.startswith("test_")
            or leaf.endswith("_test.py")
            or leaf.endswith(".test.js")
            or leaf.endswith(".spec.js")
            or leaf.endswith(".test.ts")
            or leaf.endswith(".spec.ts")
        )

    return all(_is_test_pattern(pattern) for pattern in normalized)


def _scope_patterns_are_docs_only(patterns: list[str] | None, task: dict[str, Any] | None) -> bool:
    raw_patterns = list(patterns or [])
    if not raw_patterns and isinstance(task, dict):
        raw_patterns = [str(item) for item in task.get("write_scope") or []]
    normalized = [pattern.strip().replace("\\", "/").lower() for pattern in raw_patterns]
    normalized = [pattern for pattern in normalized if pattern]
    if not normalized:
        return False

    def _is_docs_pattern(pattern: str) -> bool:
        leaf = pattern.rstrip("/").rsplit("/", 1)[-1]
        return (
            pattern in {"readme", "readme.md"}
            or pattern.startswith("docs/")
            or pattern.startswith("doc/")
            or leaf.endswith(".md")
            or leaf.endswith(".rst")
            or leaf.endswith(".txt")
        )

    return all(_is_docs_pattern(pattern) for pattern in normalized)


def _authoritative_verification_context_section(
    commands: list[str] | None,
    *,
    task: dict[str, Any] | None = None,
    allow_write_globs: list[str] | None = None,
) -> str:
    cleaned = [str(command).strip() for command in commands or [] if str(command).strip()]
    if not cleaned:
        return ""
    command_text = "\n".join(cleaned).casefold()
    task_text = _task_execution_text(task).casefold()
    test_only_scope = _scope_patterns_are_test_only(allow_write_globs, task)
    docs_only_scope = _scope_patterns_are_docs_only(allow_write_globs, task)
    doctest_related = "doctest" in command_text or "doctest" in task_text
    packaging_related = any(
        marker in task_text
        for marker in (
            "console script",
            "entry point",
            "entrypoint",
            "installable",
            "pip install",
            "pyproject",
            "setuptools",
            "wheel",
            "packaging",
        )
    ) or any(marker in command_text for marker in ("pip ", "python -m build", "build ", "wheel"))
    lines = [
        "## Authoritative Verification",
        "",
        "The managed task has locked verification commands. Before the final response,",
        "run `verify_run` with no arguments or run every command exactly, and make sure",
        "every listed command passes. Do not claim success from an alternate command",
        "while any listed command is still failing; fix the task or report a concrete",
        "infrastructure blocker.",
        "",
        "If a verification failure points toward changing files outside the write scope,",
        "first look for an in-scope repair that makes the allowed change self-contained",
        "and executable. Report a blocker only after ruling out an in-scope fix.",
        "",
        "Do not leave temporary command-output files in the repository root. Use `/tmp`",
        "or the command output shown by the tool, and remove scratch logs before finalizing.",
        "",
    ]
    if test_only_scope:
        lines.extend(
            [
                "For test-only tasks, the tests are part of the deliverable. If verification",
                "fails because the generated tests or test harness are wrong, fix the test",
                "code within scope before reporting a blocker. Prefer pytest capture fixtures",
                "such as `capsys`/`monkeypatch` over patching builtins or global stdout.",
                "",
            ]
        )
    if doctest_related and docs_only_scope:
        lines.extend(
            [
                "For docs/doctest-only tasks, a local import failure from a locked doctest",
                "command is still actionable when the editable document can provide setup.",
                "Use in-document doctest setup/import-path lines or another docs-only repair",
                "so every locked command passes before declaring infrastructure blocked.",
                "",
            ]
        )
    if packaging_related:
        lines.extend(
            [
                "For packaging/install tasks, prefer in-scope project metadata repairs",
                "such as `pyproject.toml` configuration. Do not create setup.cfg/setup.py",
                "unless those files are already in scope; inspect build/install output",
                "without committing diagnostic logs.",
                "",
            ]
        )
    lines.extend(f"- `{command}`" for command in cleaned)
    return "\n".join(lines)


@dataclass(frozen=True)
class ExecutionReportingDiff:
    changed_files: tuple[str, ...]
    patch_text: str
    inspection_error: str | None = None


@dataclass(frozen=True)
class TaskLocalWorkspaceBaseline:
    before_commit: str | None
    before_snapshot: dict[str, str]
    preexisting_changed_files: tuple[str, ...] = ()
    snapshot_root: Path | None = None

    @property
    def had_preexisting_workspace_dirt(self) -> bool:
        return bool(self.preexisting_changed_files)


@dataclass(frozen=True)
class _GitStatusEntry:
    status: str
    path: str
    orig_path: str | None = None


def _workspace_snapshot_signature(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    # Small files use content hashing so snapshot-based task-local deltas do not report
    # touched-and-restored paths as real changes. Large files keep the cheaper metadata fallback.
    if stat.st_size <= _SNAPSHOT_HASH_MAX_BYTES:
        try:
            digest = sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        if digest:
            return f"sha256:{digest}"
    ctime_ns = getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))
    return f"meta:{stat.st_size}:{stat.st_mtime_ns}:{ctime_ns}"


def _workspace_snapshot_git_paths(root: Path) -> set[str] | None:
    if shutil.which("git") is None:
        return None
    git_dir = root / ".git"
    if not git_dir.exists():
        return None
    paths: set[str] = set()
    for item in [*_git_cached_files(root), *_git_untracked_files(root)]:
        normalized = _normalize_reporting_changed_path(root=root, path=item)
        if normalized:
            paths.add(normalized)
    return paths


def _workspace_snapshot_walk_paths(root: Path) -> set[str]:
    root_resolved = root.resolve()
    paths: set[str] = set()
    for current_root, dirnames, filenames in os.walk(root_resolved):
        current_path = Path(current_root)
        resolved_current = current_path.resolve()
        rel_dir = (
            resolved_current.relative_to(root_resolved).as_posix()
            if resolved_current != root_resolved
            else ""
        )
        kept_dirnames: list[str] = []
        for dirname in dirnames:
            rel_candidate = dirname if not rel_dir else f"{rel_dir}/{dirname}"
            normalized = _normalize_reporting_changed_path(root=root_resolved, path=rel_candidate)
            if normalized and is_runtime_artifact_path(normalized, root=root_resolved):
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in filenames:
            rel_candidate = filename if not rel_dir else f"{rel_dir}/{filename}"
            normalized = _normalize_reporting_changed_path(root=root_resolved, path=rel_candidate)
            if not normalized:
                continue
            paths.add(normalized)
    return paths


def snapshot_workspace_tree(root: Path) -> dict[str, str]:
    candidate_paths = _workspace_snapshot_git_paths(root)
    if candidate_paths is None:
        candidate_paths = _workspace_snapshot_walk_paths(root)

    snapshot: dict[str, str] = {}
    for rel_path in sorted(candidate_paths):
        signature = _workspace_snapshot_signature(
            _path_under_root(root=root.resolve(), rel_path=rel_path)
        )
        if signature is None:
            continue
        snapshot[rel_path] = signature
    return snapshot


def _workspace_snapshot_changed_files(
    *,
    before: dict[str, str],
    after: dict[str, str],
) -> tuple[str, ...]:
    changed = {
        rel_path
        for rel_path in (set(before) | set(after))
        if before.get(rel_path) != after.get(rel_path)
    }
    return tuple(sorted(changed))


def safe_task_file_component(task_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in task_id)
    return safe or "task"


def build_task_execution_instruction(
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    cfg: AppConfig | None = None,
    role_model: str | None = None,
    instruction_token_budget: int | None = None,
    leading_sections: list[str] | None = None,
    relevant_assets_section: str | None = None,
) -> str:
    effective_model = (role_model or "").strip()
    effective_cfg = cfg or AppConfig(model=effective_model)
    if not effective_model:
        effective_model = (effective_cfg.model or "").strip() or "unknown-model"
    return build_task_context_pack(
        cfg=effective_cfg,
        plan=plan,
        task=task,
        role_model=effective_model,
        instruction_token_budget=instruction_token_budget,
        leading_sections=leading_sections,
        relevant_assets_section=relevant_assets_section,
    )


def _estimate_initial_execution_request_tokens(
    *,
    root: Path,
    prefix_messages: tuple[dict[str, Any], ...],
    tool_list: tuple[dict[str, Any], ...],
    instruction: str,
    image_paths: tuple[str, ...],
) -> int:
    user_message, _log_payload = _build_user_message(
        root=root,
        instruction=instruction,
        image_paths=list(image_paths) or None,
    )
    return estimate_request_tokens(
        [*prefix_messages, user_message],
        list(tool_list),
    )


def _resolve_startup_headroom_target(
    *,
    cfg: AppConfig,
    role_model: str,
    model_registry: ModelRegistry | None,
) -> tuple[int, int, int]:
    registry = model_registry or ModelRegistry(cfg=cfg)
    meta = registry.get(role_model)
    compaction_settings = resolve_compaction_settings(cfg)
    compaction_budget_tokens = compute_input_budget(
        meta,
        safety_margin=compaction_settings.safety_margin_tokens,
    )
    compaction_trigger_tokens = max(
        0,
        int(compaction_budget_tokens * compaction_settings.trigger_ratio),
    )
    compaction_target_tokens = max(
        0,
        int(compaction_budget_tokens * compaction_settings.target_ratio),
    )

    # Keep the first managed-exec request below the real execution compaction
    # trigger with enough slack for a meaningful future compaction bundle.
    removable_buffer_tokens = max(
        256,
        int(compaction_settings.execution_min_removable_tokens),
    )
    startup_target_tokens = max(
        compaction_target_tokens,
        compaction_trigger_tokens - removable_buffer_tokens,
    )
    if compaction_trigger_tokens > 0:
        startup_target_tokens = min(
            startup_target_tokens,
            compaction_trigger_tokens - 1,
        )
    else:
        startup_target_tokens = 0
    return (
        compaction_budget_tokens,
        compaction_trigger_tokens,
        max(0, startup_target_tokens),
    )


def _apply_startup_headroom_preflight(
    *,
    root: Path,
    effective_cfg: AppConfig,
    effective_model: str,
    plan: dict[str, Any],
    task: dict[str, Any],
    model_registry: ModelRegistry | None,
    budget: ExecutionPromptBudget,
    prefix_messages: tuple[dict[str, Any], ...],
    tool_list: tuple[dict[str, Any], ...],
    image_paths: tuple[str, ...],
    leading_sections: list[str] | None,
    relevant_assets_section: str | None,
    initial_pack_result: Any,
    instruction_token_counter: Callable[[str], int],
) -> tuple[Any, StartupHeadroomTelemetry]:
    compaction_budget_tokens, compaction_trigger_tokens, startup_target_tokens = (
        _resolve_startup_headroom_target(
            cfg=effective_cfg,
            role_model=effective_model,
            model_registry=model_registry,
        )
    )
    initial_request_token_estimate = _estimate_initial_execution_request_tokens(
        root=root,
        prefix_messages=prefix_messages,
        tool_list=tool_list,
        instruction=initial_pack_result.content,
        image_paths=image_paths,
    )
    if initial_request_token_estimate <= startup_target_tokens:
        return initial_pack_result, StartupHeadroomTelemetry(
            initial_request_token_estimate=initial_request_token_estimate,
            initial_request_token_estimate_before_adjustment=None,
            compaction_budget_tokens=compaction_budget_tokens,
            compaction_trigger_tokens=compaction_trigger_tokens,
            startup_target_tokens=startup_target_tokens,
            startup_headroom_tokens=startup_target_tokens - initial_request_token_estimate,
            startup_headroom_adjustment_applied=False,
            startup_headroom_adjustment_reason=None,
        )

    target_instruction_budget = max(
        0,
        min(
            budget.final_instruction_budget,
            startup_target_tokens
            - budget.pinned_prefix_token_estimate
            - budget.tool_schema_token_estimate,
        ),
    )
    try:
        adjusted_pack_result = build_task_context_pack_result(
            cfg=effective_cfg,
            plan=plan,
            task=task,
            role_model=effective_model,
            model_registry=model_registry,
            instruction_token_budget=target_instruction_budget,
            leading_sections=leading_sections,
            relevant_assets_section=relevant_assets_section,
            prefer_startup_headroom_reduction=True,
            instruction_token_counter=instruction_token_counter,
        )
    except ExecutionContextBudgetError:
        # The startup target is softer than the hard context limit. Never
        # discard a complete, fitting instruction to create compaction slack.
        return initial_pack_result, StartupHeadroomTelemetry(
            initial_request_token_estimate=initial_request_token_estimate,
            initial_request_token_estimate_before_adjustment=None,
            compaction_budget_tokens=compaction_budget_tokens,
            compaction_trigger_tokens=compaction_trigger_tokens,
            startup_target_tokens=startup_target_tokens,
            startup_headroom_tokens=startup_target_tokens - initial_request_token_estimate,
            startup_headroom_adjustment_applied=False,
            startup_headroom_adjustment_reason=(
                "the startup headroom target cannot fit the required instructions and fixed "
                "overhead; retained complete instructions within the hard context budget"
            ),
        )
    adjusted_request_token_estimate = _estimate_initial_execution_request_tokens(
        root=root,
        prefix_messages=prefix_messages,
        tool_list=tool_list,
        instruction=adjusted_pack_result.content,
        image_paths=image_paths,
    )
    return adjusted_pack_result, StartupHeadroomTelemetry(
        initial_request_token_estimate=adjusted_request_token_estimate,
        initial_request_token_estimate_before_adjustment=initial_request_token_estimate,
        compaction_budget_tokens=compaction_budget_tokens,
        compaction_trigger_tokens=compaction_trigger_tokens,
        startup_target_tokens=startup_target_tokens,
        startup_headroom_tokens=startup_target_tokens - adjusted_request_token_estimate,
        startup_headroom_adjustment_applied=True,
        startup_headroom_adjustment_reason=(
            "reduced supplementary context so the first request starts below the execution "
            "compaction trigger while retaining required instructions"
        ),
    )


def build_task_acceptance_instruction(task: dict[str, Any]) -> str:
    """Keep task requirements separate from generated guidance, assets, and plan context."""
    parts = [str(task.get(key) or "").strip() for key in ("title", "description")]
    criteria = task.get("acceptance_criteria") or []
    if isinstance(criteria, list):
        parts.extend(str(item).strip() for item in criteria if str(item).strip())
    return "\n\n".join(part for part in parts if part)


def build_task_execution_instruction_bundle(
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    cfg: AppConfig | None = None,
    role_model: str | None = None,
    mode: str,
    yes: bool,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    non_interactive: bool = False,
    verification_enabled: bool = True,
    authoritative_verification_commands: list[str] | None = None,
    trusted_system_prompt_override: str | None = None,
    trusted_system_prompt_append: str | None = None,
    untrusted_prompt_prelude: str | None = None,
    model_registry: ModelRegistry | None = None,
    api_key: str | None = None,
    safety_margin_tokens: int = DEFAULT_EXECUTION_SAFETY_MARGIN_TOKENS,
    execution_response_reserve_tokens: int = DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS,
    execution_headroom_reserve_tokens: int = DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS,
    minimum_instruction_budget_tokens: int = DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS,
    subagents_enabled: bool = False,
    leading_sections: list[str] | None = None,
    relevant_assets_section: str | None = None,
    managed_execution_startup_headroom: bool = False,
) -> PreparedTaskExecutionInstruction:
    effective_model = (role_model or "").strip()
    effective_cfg = cfg or AppConfig(model=effective_model)
    if not effective_model:
        effective_model = (effective_cfg.model or "").strip() or "unknown-model"
    task_image_paths = select_task_image_paths_for_execution(
        cfg=effective_cfg,
        plan=plan,
        task=task,
        root=root,
        role_model=effective_model,
        model_registry=model_registry,
    )
    budget_inputs = compute_execution_prompt_budget_inputs(
        cfg=effective_cfg,
        root=root,
        mode=mode,
        yes=yes,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        non_interactive=non_interactive,
        one_shot_execution=False,
        verification_enabled=verification_enabled,
        authoritative_verification_commands=authoritative_verification_commands,
        trusted_system_prompt_override=trusted_system_prompt_override,
        trusted_system_prompt_append=trusted_system_prompt_append,
        untrusted_prompt_prelude=untrusted_prompt_prelude,
        subagents_enabled=subagents_enabled,
        subagent_depth=0,
        subagent_registry=None,
        workspace_binding=None,
        api_key=api_key,
        model_registry=model_registry,
        safety_margin_tokens=safety_margin_tokens,
        execution_response_reserve_tokens=execution_response_reserve_tokens,
        execution_headroom_reserve_tokens=execution_headroom_reserve_tokens,
        minimum_instruction_budget_tokens=minimum_instruction_budget_tokens,
        image_count=len(task_image_paths or []),
    )
    budget = budget_inputs.budget

    def instruction_token_counter(instruction: str) -> int:
        return max(
            0,
            _estimate_initial_execution_request_tokens(
                root=root,
                prefix_messages=budget_inputs.prefix_messages,
                tool_list=budget_inputs.tool_list,
                instruction=instruction,
                image_paths=tuple(task_image_paths or ()),
            )
            - budget.pinned_prefix_token_estimate
            - budget.tool_schema_token_estimate,
        )

    effective_leading_sections = list(leading_sections or [])
    authoritative_verification_section = (
        _authoritative_verification_context_section(
            authoritative_verification_commands,
            task=task,
            allow_write_globs=allow_write_globs,
        )
        if verification_enabled
        else ""
    )
    if authoritative_verification_section:
        effective_leading_sections.insert(0, authoritative_verification_section)
    pack_result = build_task_context_pack_result(
        cfg=effective_cfg,
        plan=plan,
        task=task,
        role_model=effective_model,
        model_registry=model_registry,
        instruction_token_budget=budget.final_instruction_budget,
        leading_sections=effective_leading_sections,
        relevant_assets_section=relevant_assets_section,
        instruction_token_counter=instruction_token_counter,
    )
    startup_headroom: StartupHeadroomTelemetry | None = None
    if managed_execution_startup_headroom:
        pack_result, startup_headroom = _apply_startup_headroom_preflight(
            root=root,
            effective_cfg=effective_cfg,
            effective_model=effective_model,
            plan=plan,
            task=task,
            model_registry=model_registry,
            budget=budget,
            prefix_messages=budget_inputs.prefix_messages,
            tool_list=budget_inputs.tool_list,
            image_paths=tuple(task_image_paths or ()),
            leading_sections=effective_leading_sections,
            relevant_assets_section=relevant_assets_section,
            initial_pack_result=pack_result,
            instruction_token_counter=instruction_token_counter,
        )
    return PreparedTaskExecutionInstruction(
        instruction=pack_result.content,
        artifact_text=pack_result.artifact_text,
        budget=budget,
        final_instruction_token_estimate=pack_result.instruction_token_estimate,
        truncated=pack_result.truncated,
        truncation_strategy=pack_result.truncation_strategy,
        subagents_enabled=budget.subagents_enabled,
        image_paths=tuple(task_image_paths or ()),
        startup_headroom=startup_headroom,
    )


def _parse_bool_env(value: str | None) -> bool:
    if value is None:
        return False
    raw = value.strip().lower()
    return raw in {"1", "true", "yes", "on"}


def select_task_image_paths_for_execution(
    *,
    cfg: AppConfig,
    plan: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    role_model: str,
    model_registry: ModelRegistry | None = None,
) -> list[str] | None:
    if not _parse_bool_env(env_get(_TASK_IMAGES_ENV)):
        return None

    registry = model_registry or ModelRegistry(cfg=cfg)
    meta = registry.get(role_model)
    if not meta.supports_vision:
        return None

    paths = select_relevant_image_paths(
        plan=plan,
        task=task,
        root=root,
        max_images=4,
    )
    return paths or None


def write_execution_context_artifact(
    *,
    run_paths: RunPaths,
    task_id: str,
    context_text: str,
) -> Path:
    ensure_execution_dirs(run_paths)
    safe_task = safe_task_file_component(task_id)
    context_path = run_paths.execution_context_dir / f"{safe_task}_context.md"
    context_path.write_text(context_text, encoding="utf-8")
    return context_path


def write_execution_budget_artifact(
    *,
    run_paths: RunPaths,
    task_id: str,
    payload: dict[str, Any],
) -> Path:
    ensure_execution_dirs(run_paths)
    safe_task = safe_task_file_component(task_id)
    budget_path = run_paths.execution_budgets_dir / f"{safe_task}.json"
    budget_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return budget_path


def resolve_managed_task_step_budget(
    *,
    cfg: AppConfig,
    plan: dict[str, Any],
    task: dict[str, Any],
    kind: str = "managed_task",
    mode: str | None = None,
    verification_enabled: bool,
    max_steps_override: int | None = None,
    attempt_count: int | None = None,
    image_count: int = 0,
    conflict_file_count: int = 0,
) -> StepBudgetResolution:
    acceptance_criteria = task.get("acceptance_criteria") or []
    estimated_files = task.get("estimated_files") or []
    write_scope = task.get("write_scope") or []
    dependencies = task.get("dependencies") or []
    selected_assets = select_relevant_assets(plan, task)
    request = StepBudgetRequest(
        kind=str(kind or "managed_task").strip() or "managed_task",
        policy=str(
            getattr(cfg, "step_budget_policy", AUTONOMOUS_STEP_BUDGET_POLICY)
            or AUTONOMOUS_STEP_BUDGET_POLICY
        ),
        hard_cap=int(
            max_steps_override
            if max_steps_override is not None
            else getattr(cfg, "task_max_steps", DEFAULT_TASK_MAX_STEPS)
        ),
        fixed_override=max_steps_override,
        mode=mode,
        verification_enabled=bool(verification_enabled),
        attempt_count=int(attempt_count or 1),
        image_count=int(image_count or 0),
        acceptance_criteria_count=len(acceptance_criteria)
        if isinstance(acceptance_criteria, list)
        else 0,
        estimated_files_count=len(estimated_files) if isinstance(estimated_files, list) else 0,
        write_scope_count=len(write_scope) if isinstance(write_scope, list) else 0,
        dependency_count=len(dependencies) if isinstance(dependencies, list) else 0,
        asset_count=len(selected_assets),
        conflict_file_count=int(conflict_file_count or 0),
    )
    return resolve_step_budget(request)


def prepare_task_execution_knowledge(
    *,
    run_paths: RunPaths,
    task: dict[str, Any],
    selection_label: str,
    extra_paths: list[str] | None = None,
    limit: int = 4,
) -> PreparedExecutionKnowledge:
    materialized = prepare_relevant_knowledge(
        paths=run_paths,
        task=task,
        selection_label=selection_label,
        extra_paths=extra_paths,
        limit=limit,
    )
    return PreparedExecutionKnowledge(
        materialized=materialized,
        prompt_section=materialized.render_prompt_section(workspace_root=run_paths.root),
    )


def mirror_plan_into_worktree(*, run_paths: RunPaths, worktree_repo_path: Path) -> None:
    src_plan_dir = run_paths.plan_dir.resolve()
    if not src_plan_dir.exists():
        return
    dest_plan_dir = worktree_repo_path.resolve() / ".alysis" / "runs" / run_paths.run_id / "plan"
    dest_plan_dir.parent.mkdir(parents=True, exist_ok=True)
    _sync_tree_lightweight(src=src_plan_dir, dest=dest_plan_dir)


def mirror_selected_knowledge_into_worktree(
    *,
    materialized: PreparedExecutionKnowledge,
    run_paths: RunPaths,
    worktree_repo_path: Path,
) -> None:
    src_selected_dir = materialized.selected_dir.resolve()
    if not src_selected_dir.exists():
        return
    rel = src_selected_dir.relative_to(run_paths.root.resolve())
    dest_selected_dir = worktree_repo_path.resolve() / rel
    dest_selected_dir.parent.mkdir(parents=True, exist_ok=True)
    _sync_tree_lightweight(src=src_selected_dir, dest=dest_selected_dir)


def _same_file_by_size_and_mtime(*, src: Path, dest: Path) -> bool:
    if not dest.exists() or not dest.is_file():
        return False
    src_stat = src.stat()
    dest_stat = dest.stat()
    if src_stat.st_size != dest_stat.st_size:
        return False
    if src_stat.st_mtime_ns == dest_stat.st_mtime_ns:
        return True
    if src_stat.st_size > _SNAPSHOT_HASH_MAX_BYTES:
        return False
    return sha256(src.read_bytes()).digest() == sha256(dest.read_bytes()).digest()


def _windows_long_path(path: Path) -> str:
    raw = os.path.abspath(os.fspath(path))
    if os.name != "nt" or raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw.lstrip("\\")
    return "\\\\?\\" + raw


def _mkdir_existing(path: Path) -> None:
    os.makedirs(_windows_long_path(path), exist_ok=True)


def _sync_tree_lightweight(*, src: Path, dest: Path) -> None:
    _mkdir_existing(dest)
    src_files: dict[Path, Path] = {}
    src_dirs: set[Path] = {Path(".")}

    for src_path in sorted(src.rglob("*")):
        rel = src_path.relative_to(src)
        if src_path.is_dir():
            src_dirs.add(rel)
            _mkdir_existing(dest / rel)
            continue
        src_files[rel] = src_path
        dest_path = dest / rel
        _mkdir_existing(dest_path.parent)
        if _same_file_by_size_and_mtime(src=src_path, dest=dest_path):
            continue
        shutil.copy2(_windows_long_path(src_path), _windows_long_path(dest_path))

    for dest_path in sorted(dest.rglob("*"), reverse=True):
        rel = dest_path.relative_to(dest)
        if dest_path.is_file():
            if rel not in src_files:
                dest_path.unlink()
            continue
        if rel not in src_dirs:
            try:
                dest_path.rmdir()
            except OSError:
                shutil.rmtree(dest_path, ignore_errors=True)


def _normalize_workspace_relpath(rel_path: str) -> str:
    cleaned = rel_path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _path_under_root(*, root: Path, rel_path: str) -> Path:
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as e:
        raise ValueError(f"path escapes workspace root: {rel_path}") from e
    return candidate


def _ignored_snapshot_path(*, rel_parts: tuple[str, ...], excluded_names: frozenset[str]) -> bool:
    if any(part in excluded_names for part in rel_parts):
        return True
    return False


def copy_workspace_snapshot(
    *,
    src_root: Path,
    dest_root: Path,
    excluded_names: frozenset[str] | None = None,
) -> None:
    src_root = src_root.resolve()
    dest_root = dest_root.resolve()
    excluded = excluded_names or _DEFAULT_SNAPSHOT_EXCLUDES

    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)

    for src_path in sorted(src_root.rglob("*")):
        rel = src_path.relative_to(src_root)
        if _ignored_snapshot_path(
            rel_parts=rel.parts, excluded_names=excluded
        ) or is_runtime_artifact_path(
            rel.as_posix(),
            root=src_root,
        ):
            continue
        dest_path = dest_root / rel
        if src_path.is_dir():
            dest_path.mkdir(parents=True, exist_ok=True)
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)


def sync_snapshot_changed_files(
    *,
    snapshot_root: Path,
    workspace_root: Path,
    changed_files: list[str],
    protected_prefixes: tuple[str, ...] = tuple(sorted(ROOT_RUNTIME_ARTIFACT_DIR_NAMES)),
) -> list[str]:
    snapshot_root = snapshot_root.resolve()
    workspace_root = workspace_root.resolve()
    applied: list[str] = []
    normalized_prefixes = tuple(
        _normalize_workspace_relpath(prefix) for prefix in protected_prefixes if prefix.strip()
    )

    for raw_path in changed_files:
        rel_path = _normalize_workspace_relpath(raw_path)
        if not rel_path:
            continue
        if is_runtime_artifact_path(rel_path, root=snapshot_root):
            continue
        if any(
            rel_path == prefix or rel_path.startswith(prefix + "/")
            for prefix in normalized_prefixes
            if prefix
        ):
            continue

        src_path = _path_under_root(root=snapshot_root, rel_path=rel_path)
        dest_path = _path_under_root(root=workspace_root, rel_path=rel_path)
        if src_path.exists():
            if src_path.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
        elif dest_path.exists():
            if dest_path.is_dir():
                shutil.rmtree(dest_path)
            else:
                dest_path.unlink()
            parent = dest_path.parent
            while parent != workspace_root and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        applied.append(rel_path)

    return applied


def snapshot_runtime_tree(root: Path) -> dict[str, str]:
    runtime_dir = (root / ".alysis").resolve()
    if not runtime_dir.exists():
        return {}
    snapshot: dict[str, str] = {}
    root_resolved = root.resolve()
    for p in sorted(runtime_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = os.fspath(p.resolve().relative_to(root_resolved))
        rel_norm = rel.replace("\\", "/")
        stat = p.stat()
        is_plan_asset = "/plan/assets/" in f"/{rel_norm}/"
        if is_plan_asset or stat.st_size > _SNAPSHOT_HASH_MAX_BYTES:
            snapshot[rel] = f"meta:{stat.st_size}:{stat.st_mtime_ns}"
            continue
        content = p.read_bytes()
        parts = rel_norm.split("/")
        is_execution_lock = (
            rel_norm == ".alysis/workspace_execution/active_execution.lock.json"
            or (
                len(parts) == 4
                and parts[:2] == [".alysis", "runs"]
                and parts[-1] == "active_execution.lock.json"
            )
        )
        if is_execution_lock:
            try:
                lock = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                lock = None
            if (
                isinstance(lock, dict)
                and lock.get("schema_version") == 2
                and lock.get("pid") == os.getpid()
                and lock.get("hostname") == socket.gethostname()
                and lock.get("owner_token")
            ):
                # Our guard refreshes this timestamp during long tasks. Keep
                # hashing ownership and every other field so tampering is detected.
                lock.pop("last_heartbeat_at", None)
                content = json.dumps(lock, sort_keys=True).encode("utf-8")
        digest = sha256(content).hexdigest()
        snapshot[rel] = f"sha256:{digest}"
    return snapshot


def _run_git_capture(
    *,
    root: Path | None,
    args: list[str],
    text: bool = False,
) -> subprocess.CompletedProcess[Any] | None:
    cmd = ["git", *args] if root is None else ["git", "-C", os.fspath(root), *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            **command_text(text),
            check=False,
            env=build_git_process_env(),
            timeout=_READ_ONLY_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_diff_text(root: Path) -> str | None:
    if shutil.which("git") is None:
        return None
    proc = _run_git_capture(root=root, args=["diff"], text=True)
    if proc is None:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_status_entries(root: Path) -> list[_GitStatusEntry]:
    if shutil.which("git") is None:
        return []
    proc = _run_git_capture(root=root, args=["status", "--porcelain=v1", "-z"])
    if proc is None:
        return []
    if proc.returncode != 0:
        return []

    entries: list[_GitStatusEntry] = []
    stdout = proc.stdout
    raw_stdout = (
        stdout.encode("utf-8", errors="surrogateescape")
        if isinstance(stdout, str)
        else (stdout or b"")
    )
    parts = raw_stdout.split(b"\0")
    index = 0
    while index < len(parts):
        raw_entry = parts[index]
        index += 1
        if not raw_entry or len(raw_entry) < 3:
            continue
        status = raw_entry[:2].decode("utf-8", errors="replace")
        path = raw_entry[3:].decode("utf-8", errors="surrogateescape")
        orig_path: str | None = None
        if any(marker in status for marker in {"R", "C"}) and index < len(parts):
            second_raw = parts[index]
            index += 1
            if second_raw:
                orig_path = second_raw.decode("utf-8", errors="surrogateescape")
        entries.append(_GitStatusEntry(status=status, path=path, orig_path=orig_path))
    return entries


def _git_has_head_commit(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    proc = _run_git_capture(root=root, args=["rev-parse", "--verify", "HEAD"], text=True)
    if proc is None:
        return False
    return proc.returncode == 0


def _git_untracked_files(root: Path) -> list[str]:
    return _git_ls_files(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        include_non_material=False,
    )


def _git_cached_files(root: Path) -> list[str]:
    return _git_ls_files(
        root,
        ["ls-files", "--cached", "-z"],
        include_non_material=True,
    )


def _git_ls_files(
    root: Path,
    args: list[str],
    *,
    include_non_material: bool,
) -> list[str]:
    if shutil.which("git") is None:
        return []
    proc = _run_git_capture(root=root, args=args)
    if proc is None:
        return []
    if proc.returncode != 0:
        return []
    stdout = proc.stdout
    raw_stdout = (
        stdout.encode("utf-8", errors="surrogateescape")
        if isinstance(stdout, str)
        else (stdout or b"")
    )
    paths: list[str] = []
    for item in raw_stdout.split(b"\0"):
        if not item:
            continue
        decoded = item.decode("utf-8", errors="surrogateescape")
        if not include_non_material and is_non_material_untracked_path(decoded):
            continue
        paths.append(decoded)
    return paths


def _git_text_output(
    *,
    root: Path,
    args: list[str],
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str | None:
    if shutil.which("git") is None:
        return None
    proc = _run_git_capture(root=root, args=args, text=True)
    if proc is None:
        return None
    if proc.returncode not in allowed_returncodes:
        return None
    return proc.stdout


def _tracked_patch_text(root: Path) -> str | None:
    if _git_has_head_commit(root):
        combined = _git_text_output(
            root=root,
            args=["diff", "HEAD", "--binary", "-M"],
        )
        if combined is not None:
            return combined

    patch_parts: list[str] = []
    staged = _git_text_output(
        root=root,
        args=["diff", "--cached", "--binary", "-M", "--root"],
    )
    unstaged = _git_text_output(
        root=root,
        args=["diff", "--binary", "-M"],
    )
    if staged is None and unstaged is None:
        return None
    if staged:
        patch_parts.append(staged if staged.endswith("\n") else staged + "\n")
    if unstaged:
        patch_parts.append(unstaged if unstaged.endswith("\n") else unstaged + "\n")
    return "".join(patch_parts)


def _normalize_reporting_changed_path(*, root: Path, path: str) -> str | None:
    rel_path = _normalize_workspace_relpath(path.strip('"'))
    if not rel_path or is_runtime_artifact_path(rel_path, root=root):
        return None
    return rel_path


def _execution_reporting_changed_files(
    root: Path,
    *,
    status_entries: list[_GitStatusEntry] | None = None,
    untracked_files: list[str] | None = None,
) -> tuple[str, ...]:
    seen: set[str] = set()
    changed: list[str] = []
    entries = status_entries if status_entries is not None else _git_status_entries(root)

    def _append(path: str) -> None:
        normalized = _normalize_reporting_changed_path(root=root, path=path)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        changed.append(normalized)

    for entry in entries:
        if entry.status in {"!!", "??"}:
            continue
        if entry.orig_path is not None:
            _append(entry.orig_path)
        _append(entry.path)

    for path in untracked_files if untracked_files is not None else _git_untracked_files(root):
        _append(path)

    return tuple(sorted(changed))


def _untracked_file_patch_text(*, root: Path, rel_path: str) -> str | None:
    if shutil.which("git") is None:
        return None
    abs_path = _path_under_root(root=root.resolve(), rel_path=rel_path)
    if not abs_path.exists() or not abs_path.is_file():
        return None
    proc = _run_git_capture(
        root=root,
        args=["diff", "--no-index", "--", os.devnull, rel_path],
        text=True,
    )
    if proc is None:
        return None
    # git diff --no-index returns 1 when differences are found.
    if proc.returncode not in {0, 1}:
        return None
    return proc.stdout


def build_execution_reporting_diff(root: Path) -> ExecutionReportingDiff:
    # Sequential forge execution used plain `git diff` / `git diff --name-only`, which
    # made reports truthful for one slice of repo state at a time and silently dropped new
    # untracked files or staged-only changes. Build one deterministic status-based view for the
    # changed-file list, then keep tracked patch text close to normal git diff behavior.
    status_entries = _git_status_entries(root)
    tracked_patch_text = _tracked_patch_text(root)
    raw_untracked_files = _git_untracked_files(root)
    changed_files = _execution_reporting_changed_files(
        root,
        status_entries=status_entries,
        untracked_files=raw_untracked_files,
    )
    normalized_untracked = {
        normalized
        for path in raw_untracked_files
        if (normalized := _normalize_reporting_changed_path(root=root, path=path)) is not None
    }
    untracked_files = tuple(path for path in changed_files if path in normalized_untracked)

    patch_parts: list[str] = []
    if tracked_patch_text is None:
        if not changed_files:
            return ExecutionReportingDiff(
                changed_files=changed_files,
                patch_text="(no git diff available)\n",
            )
    elif tracked_patch_text:
        patch_parts.append(
            tracked_patch_text if tracked_patch_text.endswith("\n") else tracked_patch_text + "\n"
        )

    for rel_path in untracked_files:
        untracked_patch = _untracked_file_patch_text(root=root, rel_path=rel_path)
        if not untracked_patch:
            continue
        patch_parts.append(
            untracked_patch if untracked_patch.endswith("\n") else untracked_patch + "\n"
        )

    return ExecutionReportingDiff(
        changed_files=changed_files,
        patch_text="".join(patch_parts),
    )


def build_workspace_snapshot_reporting_diff(
    root: Path,
    *,
    before_snapshot: dict[str, str],
    after_snapshot: dict[str, str],
) -> ExecutionReportingDiff:
    changed_files = _workspace_snapshot_changed_files(
        before=before_snapshot,
        after=after_snapshot,
    )
    if not changed_files:
        return ExecutionReportingDiff(changed_files=(), patch_text="")

    summary_lines = ["# Workspace snapshot diff", ""]
    patch_parts: list[str] = []
    for rel_path in changed_files:
        if rel_path not in before_snapshot:
            summary_lines.append(f"added: {rel_path}")
            untracked_patch = _untracked_file_patch_text(root=root, rel_path=rel_path)
            if untracked_patch:
                patch_parts.append(
                    untracked_patch if untracked_patch.endswith("\n") else untracked_patch + "\n"
                )
        elif rel_path not in after_snapshot:
            summary_lines.append(f"deleted: {rel_path}")
        else:
            summary_lines.append(f"modified: {rel_path}")

    summary_text = "\n".join(summary_lines) + "\n"
    return ExecutionReportingDiff(
        changed_files=changed_files,
        patch_text=summary_text + ("\n" + "".join(patch_parts) if patch_parts else ""),
    )


def _git_no_index_display_path(path: Path) -> str:
    return path.resolve().as_posix().lstrip("/")


def _rewrite_no_index_patch_paths(
    patch_text: str,
    *,
    before_root: Path,
    after_root: Path,
) -> str:
    normalized = patch_text.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    normalized = "".join(
        line.replace('"', "") if line.startswith(("diff --git ", "--- ", "+++ ")) else line
        for line in normalized.splitlines(keepends=True)
    )
    replacements = (
        (f"a/{_git_no_index_display_path(before_root)}/", "a/"),
        (f"b/{_git_no_index_display_path(before_root)}/", "b/"),
        (f"a/{_git_no_index_display_path(after_root)}/", "a/"),
        (f"b/{_git_no_index_display_path(after_root)}/", "b/"),
    )
    for old, new in replacements:
        normalized = normalized.replace(old, new)
    return normalized


def _task_local_snapshot_file_patch_text(
    *,
    before_root: Path,
    after_root: Path,
    rel_path: str,
    exists_before: bool,
    exists_after: bool,
) -> str | None:
    if shutil.which("git") is None:
        return None

    before_path = (
        os.fspath(_path_under_root(root=before_root.resolve(), rel_path=rel_path))
        if exists_before
        else os.devnull
    )
    after_path = (
        os.fspath(_path_under_root(root=after_root.resolve(), rel_path=rel_path))
        if exists_after
        else os.devnull
    )
    proc = _run_git_capture(
        root=None,
        args=[
            "diff",
            "--no-index",
            "--binary",
            "-M",
            "--",
            before_path,
            after_path,
        ],
        text=True,
    )
    if proc is None:
        return None
    if proc.returncode not in {0, 1}:
        return None
    if not proc.stdout:
        return ""
    return _rewrite_no_index_patch_paths(
        proc.stdout,
        before_root=before_root,
        after_root=after_root,
    )


def _build_task_local_snapshot_reporting_diff(
    root: Path,
    *,
    baseline: TaskLocalWorkspaceBaseline,
    after_snapshot: dict[str, str],
) -> ExecutionReportingDiff:
    changed_files = _workspace_snapshot_changed_files(
        before=baseline.before_snapshot,
        after=after_snapshot,
    )
    if not changed_files:
        return ExecutionReportingDiff(changed_files=(), patch_text="")

    summary_lines = ["# Task-local workspace diff", ""]
    if baseline.preexisting_changed_files:
        summary_lines.append(
            "# Workspace was already dirty at task start; this artifact excludes "
            f"{len(baseline.preexisting_changed_files)} pre-existing change(s)."
        )
        summary_lines.append("")

    patch_parts: list[str] = []
    for rel_path in changed_files:
        exists_before = rel_path in baseline.before_snapshot
        exists_after = rel_path in after_snapshot
        if not exists_before:
            summary_lines.append(f"added: {rel_path}")
        elif not exists_after:
            summary_lines.append(f"deleted: {rel_path}")
        else:
            summary_lines.append(f"modified: {rel_path}")

        if baseline.snapshot_root is None:
            continue
        patch_text = _task_local_snapshot_file_patch_text(
            before_root=baseline.snapshot_root,
            after_root=root,
            rel_path=rel_path,
            exists_before=exists_before,
            exists_after=exists_after,
        )
        if not patch_text:
            continue
        patch_parts.append(patch_text if patch_text.endswith("\n") else patch_text + "\n")

    summary_text = "\n".join(summary_lines) + "\n"
    return ExecutionReportingDiff(
        changed_files=changed_files,
        patch_text=summary_text + ("\n" + "".join(patch_parts) if patch_parts else ""),
    )


def capture_task_local_workspace_baseline(
    root: Path,
    *,
    before_commit: str | None,
) -> TaskLocalWorkspaceBaseline:
    # Always capture a file-state snapshot so changed-file attribution stays task-local even in
    # normal git repos. Only materialize a full workspace copy when the task starts dirty, because
    # that is the case where a plain end-of-task git diff would otherwise include unrelated work.
    before_snapshot = snapshot_workspace_tree(root)
    if before_commit is None:
        return TaskLocalWorkspaceBaseline(
            before_commit=None,
            before_snapshot=before_snapshot,
        )

    status_entries = _git_status_entries(root)
    raw_untracked_files = _git_untracked_files(root)
    preexisting_changed_files = _execution_reporting_changed_files(
        root,
        status_entries=status_entries,
        untracked_files=raw_untracked_files,
    )
    if not preexisting_changed_files:
        return TaskLocalWorkspaceBaseline(
            before_commit=before_commit,
            before_snapshot=before_snapshot,
        )

    snapshot_root = Path(tempfile.mkdtemp(prefix="alysis-task-baseline-")).resolve()
    try:
        copy_workspace_snapshot(src_root=root, dest_root=snapshot_root)
    except Exception:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        raise

    return TaskLocalWorkspaceBaseline(
        before_commit=before_commit,
        before_snapshot=before_snapshot,
        preexisting_changed_files=preexisting_changed_files,
        snapshot_root=snapshot_root,
    )


def cleanup_task_local_workspace_baseline(baseline: TaskLocalWorkspaceBaseline) -> None:
    if baseline.snapshot_root is None:
        return
    shutil.rmtree(baseline.snapshot_root, ignore_errors=True)


def build_task_local_workspace_reporting_diff(
    root: Path,
    *,
    baseline: TaskLocalWorkspaceBaseline,
    after_commit: str | None,
) -> ExecutionReportingDiff:
    after_snapshot = snapshot_workspace_tree(root)
    task_local_diff = _build_task_local_snapshot_reporting_diff(
        root,
        baseline=baseline,
        after_snapshot=after_snapshot,
    )
    if baseline.before_commit is None or baseline.had_preexisting_workspace_dirt:
        return task_local_diff

    try:
        live_git_diff = build_execution_reporting_diff_with_commit_range(
            root,
            before_commit=baseline.before_commit,
            after_commit=after_commit,
        )
    except GitOpsError as e:
        return ExecutionReportingDiff(
            changed_files=task_local_diff.changed_files,
            patch_text=task_local_diff.patch_text,
            inspection_error=f"scope inspection failed: {e}",
        )

    return ExecutionReportingDiff(
        changed_files=live_git_diff.changed_files,
        patch_text=live_git_diff.patch_text,
    )


def _merge_execution_reporting_changed_files(
    root: Path,
    *path_groups: list[str],
) -> tuple[str, ...]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in path_groups:
        for path in group:
            normalized = _normalize_reporting_changed_path(root=root, path=str(path))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def build_execution_reporting_diff_with_commit_range(
    root: Path,
    *,
    before_commit: str | None,
    after_commit: str | None,
) -> ExecutionReportingDiff:
    live = build_execution_reporting_diff(root)
    if not after_commit or after_commit == before_commit:
        return live

    committed_files = changed_files_since(
        root,
        before_commit=before_commit,
        after_commit=after_commit,
    )
    committed_patch_text = diff_text_since(
        root,
        before_commit=before_commit,
        after_commit=after_commit,
    )

    patch_parts: list[str] = []
    if committed_patch_text:
        patch_parts.append(
            committed_patch_text
            if committed_patch_text.endswith("\n")
            else committed_patch_text + "\n"
        )
    if live.patch_text and live.patch_text != "(no git diff available)\n":
        patch_parts.append(
            live.patch_text if live.patch_text.endswith("\n") else live.patch_text + "\n"
        )

    return ExecutionReportingDiff(
        changed_files=_merge_execution_reporting_changed_files(
            root,
            list(live.changed_files),
            committed_files,
        ),
        patch_text="".join(patch_parts) or live.patch_text,
    )


def git_changed_files(root: Path) -> list[str]:
    if shutil.which("git") is None:
        return []
    proc = _run_git_capture(root=root, args=["diff", "--name-only"], text=True)
    if proc is None:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def write_patch_from_diff(*, root: Path, patch_path: Path) -> None:
    diff_text = git_diff_text(root)
    if diff_text is None:
        patch_path.write_text("(no git diff available)\n", encoding="utf-8")
        return
    patch_path.write_text(diff_text, encoding="utf-8")


def snapshot_session_logs(cfg: AppConfig) -> set[Path]:
    sessions_dir = resolve_sessions_dir(cfg)
    if not sessions_dir.exists():
        return set()
    return set(sessions_dir.glob("*.jsonl"))


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _execution_runtime_base_candidates() -> tuple[Path, ...]:
    return (
        resolve_sessions_dir(AppConfig()).resolve().parent / "execution_runtime_sessions",
        Path(tempfile.gettempdir()).resolve() / "alysis" / "execution_runtime_sessions",
    )


def _safe_execution_runtime_base(*, workspace_root: Path) -> Path:
    workspace_root = workspace_root.resolve()
    protected_workspace_root = (workspace_root / ".alysis").resolve()
    for candidate in _execution_runtime_base_candidates():
        candidate_resolved = candidate.resolve()
        if _path_is_relative_to(candidate_resolved, workspace_root):
            continue
        if _path_is_relative_to(candidate_resolved, protected_workspace_root):
            continue
        return candidate_resolved
    raise ValueError(
        f"unable to resolve safe execution runtime base outside workspace: {workspace_root}"
    )


def execution_private_sessions_dir(
    *, cfg: AppConfig, run_id: str, task_id: str, workspace_root: Path
) -> Path:
    del cfg
    base_dir = _safe_execution_runtime_base(workspace_root=workspace_root)
    return base_dir / sanitize_session_id(run_id) / safe_task_file_component(task_id)


def cleanup_execution_private_sessions_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def task_execution_session_artifact_dir(*, run_paths: RunPaths, task_id: str) -> Path:
    ensure_execution_dirs(run_paths)
    safe_task = safe_task_file_component(task_id)
    return run_paths.execution_sessions_dir / safe_task


@dataclass(frozen=True)
class ExecutionLogArtifactsResult:
    log_copy_path: Path
    pointer_path: Path
    logging_enabled: bool
    log_retained: bool
    session_artifacts_retained: bool
    copied_log_path: Path | None
    source_log_path: Path | None
    session_id: str | None
    session_artifact_dir: Path | None
    note: str | None = None
    cleanup_note: str | None = None

    def pointer_payload(self, *, workspace_root: Path) -> dict[str, Any]:
        # Persist repo-relative paths when the artifact lives under the workspace,
        # and redact external runtime locations so beta-facing logs do not leak
        # host directory structures.
        payload: dict[str, Any] = {
            "task_id": self.pointer_path.stem.removesuffix(".log"),
            "logging_enabled": self.logging_enabled,
            "log_retained": self.log_retained,
            "session_artifacts_retained": self.session_artifacts_retained,
            "copied_log_path": (
                safe_serialized_path(self.copied_log_path, workspace_root=workspace_root)
                if self.copied_log_path is not None
                else None
            ),
            "source_log_path": (
                safe_serialized_path(self.source_log_path, workspace_root=workspace_root)
                if self.source_log_path is not None
                else None
            ),
            "session_id": self.session_id,
            "session_artifact_dir": (
                safe_serialized_path(self.session_artifact_dir, workspace_root=workspace_root)
                if self.session_artifact_dir is not None
                else None
            ),
        }
        if self.note is not None:
            payload["note"] = self.note
        if self.cleanup_note is not None:
            payload["cleanup_note"] = self.cleanup_note
        return payload


def write_exec_log_artifacts(
    *,
    paths: RunPaths,
    task_id: str,
    cfg: AppConfig,
    no_log: bool,
    before_logs: set[Path] | None,
    sessions_dir: Path | None = None,
    expected_session_id: str | None = None,
    session_started: bool = True,
) -> ExecutionLogArtifactsResult:
    ensure_execution_dirs(paths)
    safe_task = safe_task_file_component(task_id)
    log_copy_path = paths.execution_logs_dir / f"{safe_task}.jsonl"
    pointer_path = paths.execution_logs_dir / f"{safe_task}.log.json"
    if not session_started:
        result = ExecutionLogArtifactsResult(
            log_copy_path=log_copy_path,
            pointer_path=pointer_path,
            logging_enabled=not no_log,
            log_retained=False,
            session_artifacts_retained=False,
            copied_log_path=None,
            source_log_path=None,
            session_id=None,
            session_artifact_dir=None,
            note="Execution was rejected before an agent session started; no session log was produced.",
        )
        pointer_payload = result.pointer_payload(workspace_root=paths.root)
        pointer_payload["task_id"] = task_id
        pointer_path.write_text(json.dumps(pointer_payload, indent=2) + "\n", encoding="utf-8")
        return result
    retained_session_artifact_dir = task_execution_session_artifact_dir(
        run_paths=paths,
        task_id=task_id,
    )

    resolved_sessions_dir = sessions_dir or resolve_sessions_dir(cfg)
    retained_sessions_parent = paths.execution_sessions_dir.resolve()
    selected: Path | None = None
    if expected_session_id:
        candidate = resolved_sessions_dir / f"{expected_session_id}.jsonl"
        if candidate.exists():
            selected = candidate

    after_logs = sorted(
        resolved_sessions_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
    )
    if selected is None:
        previous_logs = before_logs or set()
        new_logs = [p for p in after_logs if p not in previous_logs]
        selected = new_logs[-1] if new_logs else (after_logs[-1] if after_logs else None)

    source_session_id = selected.stem if selected is not None else expected_session_id
    source_session_artifact_dir = (
        resolved_sessions_dir / sanitize_session_id(source_session_id)
        if source_session_id
        else None
    )
    source_artifacts_exist = bool(
        source_session_artifact_dir
        and source_session_artifact_dir.exists()
        and source_session_artifact_dir.is_dir()
    )
    source_artifacts_are_run_owned = resolved_sessions_dir.resolve() == retained_sessions_parent

    log_retained = False
    copied_log: Path | None = None
    source_log: Path | None = None
    retained_artifacts = False
    session_artifact_dir: Path | None = None
    note: str | None = None
    cleanup_note: str | None = None

    if no_log:
        note = "session logging disabled (--no-log)"
        if source_artifacts_exist and source_artifacts_are_run_owned:
            retained_artifacts = True
            session_artifact_dir = source_session_artifact_dir
        elif source_artifacts_exist:
            cleanup_note = "temporary runtime session artifacts will be cleaned up"
    elif selected and selected.exists():
        shutil.copy2(selected, log_copy_path)
        log_retained = True
        copied_log = log_copy_path
        source_log = selected
        if source_artifacts_exist and source_session_artifact_dir is not None:
            if source_session_artifact_dir.resolve() != retained_session_artifact_dir.resolve():
                _sync_tree_lightweight(
                    src=source_session_artifact_dir,
                    dest=retained_session_artifact_dir,
                )
            retained_artifacts = True
            session_artifact_dir = retained_session_artifact_dir
    else:
        note = "session log not found"

    result = ExecutionLogArtifactsResult(
        log_copy_path=log_copy_path,
        pointer_path=pointer_path,
        logging_enabled=not no_log,
        log_retained=log_retained,
        session_artifacts_retained=retained_artifacts,
        copied_log_path=copied_log,
        source_log_path=source_log,
        session_id=source_session_id,
        session_artifact_dir=session_artifact_dir,
        note=note,
        cleanup_note=cleanup_note,
    )
    pointer_payload = dict(result.pointer_payload(workspace_root=paths.root))
    pointer_payload["task_id"] = task_id
    pointer_path.write_text(
        json.dumps(pointer_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
