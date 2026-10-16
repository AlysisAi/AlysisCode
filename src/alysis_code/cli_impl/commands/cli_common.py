# ruff: noqa: F401,I001
# Many imports are intentionally kept as monkeypatch surfaces for tests/cli_impl.
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from click import get_current_context
from click.core import ParameterSource
from platformdirs import user_data_dir

from ... import __version__
from ...assets.index import AssetIndex
from ...branding import env_get
from ...clipboard import ClipboardError, paste_clipboard_image
from ...config import (
    _DEFAULT_TOOLBAR_ITEMS,
    _VALID_TOOLBAR_ITEMS,
    ApiKeyResolution,
    AppConfig,
    ConfigError,
    clear_persisted_api_key,
    clear_persisted_profile_key,
    clone_cfg,
    config_path,
    credentials_path,
    default_chat_history_path,
    load_config,
    load_persisted_profile_keys,
    rename_persisted_profile_key,
    resolve_api_key,
    resolve_llm_timeout_s,
    resolve_profile_api_key,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
    save_config,
    save_persisted_api_key,
    save_persisted_profile_key,
    set_config_value,
)
from ...conflict_auto_resolver import (
    attempt_auto_resolve_conflict,
    bump_conflict_attempt,
    can_attempt_conflict_auto_resolve,
    load_conflict_auto_resolve_settings,
)
from ...custom_tools import (
    CustomToolCatalogEntry,
    build_custom_tool_session_state,
    global_custom_tools_root,
    project_custom_tools_root,
    trust_project_tool,
    untrust_project_tool,
)
from ...execution_shared import (
    build_execution_reporting_diff as _shared_build_execution_reporting_diff,
)
from ...execution_shared import (
    build_execution_reporting_diff_with_commit_range as _shared_build_execution_reporting_diff_with_commit_range,
)
from ...execution_shared import (
    build_task_execution_instruction as _shared_build_task_execution_instruction,
)
from ...execution_shared import (
    build_task_execution_instruction_bundle as _shared_build_task_execution_instruction_bundle,
)
from ...execution_shared import (
    build_task_local_workspace_reporting_diff as _shared_build_task_local_workspace_reporting_diff,
)
from ...execution_shared import (
    build_workspace_snapshot_reporting_diff as _shared_build_workspace_snapshot_reporting_diff,
)
from ...execution_shared import (
    capture_task_local_workspace_baseline as _shared_capture_task_local_workspace_baseline,
)
from ...execution_shared import (
    cleanup_execution_private_sessions_dir as _shared_cleanup_execution_private_sessions_dir,
)
from ...execution_shared import (
    cleanup_task_local_workspace_baseline as _shared_cleanup_task_local_workspace_baseline,
)
from ...execution_shared import (
    execution_private_sessions_dir as _shared_execution_private_sessions_dir,
)
from ...execution_shared import (
    git_changed_files as _shared_git_changed_files,
)
from ...execution_shared import (
    mirror_selected_knowledge_into_worktree as _shared_mirror_selected_knowledge_into_worktree,
)
from ...execution_shared import (
    prepare_task_execution_knowledge as _shared_prepare_task_execution_knowledge,
)
from ...execution_shared import (
    resolve_managed_task_step_budget as _shared_resolve_managed_task_step_budget,
)
from ...execution_shared import (
    safe_task_file_component as _shared_safe_task_file_component,
)
from ...execution_shared import (
    select_task_image_paths_for_execution as _shared_select_task_image_paths_for_execution,
)
from ...execution_shared import (
    snapshot_runtime_tree as _shared_snapshot_runtime_tree,
)
from ...execution_shared import (
    snapshot_session_logs as _shared_snapshot_session_logs,
)
from ...execution_shared import (
    snapshot_workspace_tree as _shared_snapshot_workspace_tree,
)
from ...execution_shared import (
    write_exec_log_artifacts as _shared_write_exec_log_artifacts,
)
from ...execution_shared import (
    write_execution_budget_artifact as _shared_write_execution_budget_artifact,
)
from ...execution_shared import (
    write_execution_context_artifact as _shared_write_execution_context_artifact,
)
from ...execution_shared import (
    write_patch_from_diff as _shared_write_patch_from_diff,
)
from ...extensions.install import (
    EnableResult,
    PluginInstallError,
    TrustPromptRequest,
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from ...extensions.models import normalize_extension_id
from ...extensions.paths import project_extensions_path
from ...extensions.registry import find_by_id, load_registry
from ...extensions.registry import search as search_extensions
from ...extensions.state import (
    compute_effective_enabled,
    load_global_state,
    load_project_overrides,
    load_project_state,
)
from ...extensions.workspace_trust import is_workspace_trusted
from ...surface.styles import (
    STYLE_CHROME,
    STYLE_CONTENT,
    STYLE_DESELECTED_DESC,
    STYLE_DESELECTED_LABEL,
    STYLE_DIM,
    STYLE_EMPHASIS,
    STYLE_ERROR,
    STYLE_SELECTED_DESC,
    STYLE_SELECTED_LABEL,
    STYLE_SUBAGENT,
    STYLE_SUCCESS,
    STYLE_WARN,
)
from ...feedback_report import (
    FeedbackReportError,
    create_feedback_bundle,
    create_feedback_github_issue_draft,
    feedback_github_issue_status_lines,
    resolve_feedback_workspace_root,
)
from ...git_ops import (
    GitOpsError,
    added_files_since,
    changed_files_between,
    checkout_branch,
    commit_all,
    current_branch,
    delete_branch,
    ensure_clean_for_pr,
    ensure_git_available,
    ensure_git_repo,
    ensure_not_staged_paths,
    ensure_not_staged_prefixes,
    ensure_not_staged_runtime_artifacts,
    format_patch_stdout,
    generate_task_branch_name,
    head_commit,
    merge_no_ff,
    stage_all,
    staged_files,
    unstage_staged_paths,
    unstage_staged_prefixes,
    unstage_staged_runtime_artifacts,
)
from ...forge import (
    ForgeError,
    RunPaths,
    add_requirement,
    add_task,
    append_planner_chat,
    append_planner_summary,
    append_transcript_note,
    attach_asset,
    create_plan_run,
    ensure_execution_dirs,
    ensure_workspace_context_artifacts,
    finalize_plan,
    find_task,
    format_workspace_context_summary_lines,
    load_current_run_paths,
    load_plan,
    now_iso,
    rebind_run_paths_to_workspace_binding,
    refresh_current_run_pointer_if_tracking_same_run,
    refresh_workspace_context_artifacts,
    requirement_is_execution_ready,
    requirement_text,
    save_plan,
    set_task_status,
    write_task_report,
)
from ...hooks import (
    HookDispatcher,
    canonicalize_hook_event_name,
    hook_audit_artifact_path,
    is_project_hooks_config_trusted,
    load_hook_config_file,
    load_resolved_hooks_config,
    load_trust_state,
    project_hooks_config_path,
    project_local_hooks_config_path,
    read_hook_audit_events,
    trust_project_hooks_config,
    untrust_project_hooks_config,
    user_hooks_config_path,
)
from ...interactive_input_guard import (
    is_interactive_prompt_active,
    is_interactive_prompt_terminal_owner,
)
from ...knowledge_base import rebuild_knowledge_index, write_issue_entry, write_task_attempt_entry
from ...knowledge_capture import (
    RecordingSurface,
    mark_knowledge_capture_promotion_skipped,
    persist_execution_knowledge_capture,
    promote_validated_knowledge_capture,
)
from ...knowledge_librarian import (
    prepare_planner_knowledge as _prepare_planner_knowledge,
)
from ...knowledge_librarian import (
    resolve_knowledge_workspace_root as _resolve_knowledge_workspace_root,
)
from ...litellm_static_provider import (
    BUNDLED_MODEL_CATALOG_SOURCE,
    get_bundled_model_catalog_provenance,
)
from ...llm_error_display import classify_llm_error_display
from ...mcp.config import load_resolved_mcp_config
from ...mcp.forge_scope import (
    ForgeTaskMcpScope,
    describe_task_mcp_scope,
    normalize_task_mcp_scope,
)
from ...mcp.manager import (
    ForgeTaskScopedMcpManager,
    McpManager,
    build_mcp_execution_context_summary,
    create_forge_task_scoped_mcp_manager,
    create_mcp_manager,
)
from ...mcp.models import ResolvedMcpServer
from ...mcp.oauth import (
    McpOAuthError,
    discover_authorization_server_metadata,
    discover_authorization_server_metadata_from_url,
    discover_protected_resource_metadata,
    resolve_requested_scopes,
)
from ...mcp.oauth_runtime import perform_authorization_code_login
from ...mcp.oauth_store import (
    McpOAuthTokenStoreError,
    delete_oauth_token_record,
    load_oauth_token_record,
)
from ...merge_conflict_reviewer import (
    capture_merge_conflict_context,
    list_unmerged_files,
    review_merge_conflict,
    try_abort_merge,
    write_conflict_artifacts,
)
from ...model_metadata_utils import parse_non_negative_float, parse_positive_int
from ...model_router import ROLE_CODING, resolve_model_for_role
from ...plan_repair import (
    PlannerRepairReport,
    apply_plan_status,
    clarification_goal_key,
    plan_repair_event_payload,
    plan_status,
    record_plan_repair,
    resolve_plan_repair_policy,
)
from ...plan_validation import validate_plan, validate_plan_against_assets
from ...remote_sync import (
    RemoteSyncError,
    ensure_pr_or_mr,
    get_remote_url,
    init_remote_record,
    load_remote_settings_from_env,
    push_base,
    push_branch,
    resolve_provider,
    truncate_output,
    write_remote_record,
)
from ...repo_scan import scan_workspace
from ...request_estimation import estimate_request_token_breakdown
from ...review_gate import ReviewError, review_task
from ...runtime_artifacts import has_grounded_rust_target_runtime_artifacts
from ...runtime_kind import RuntimeKind, normalize_runtime_kind
from ...sandbox_doctor import (
    SandboxDiagnostic,
    configured_sandbox_images,
    diagnose_sandbox,
    format_sandbox_problem_message,
    pull_sandbox_images,
    sandbox_env_summary,
)
from ...session_metrics import score_session_log
from ...session_store import (
    SessionInfo,
    canonical_workspace_path,
    list_sessions,
    local_session_owner,
    read_session_events,
    resolve_sessions_dir,
    session_belongs_to_owner,
    session_belongs_to_workspace,
)
from ...skills import (
    build_explicit_skill_context_message,
    discover_skills,
    install_skill_bundle,
    load_global_skill_state,
    load_project_skill_state,
    load_repo_conventions,
    project_skill_root_for_family,
    remove_managed_skill,
    render_repo_conventions_context,
    render_skill_info_text,
    resolve_skill_by_name,
    resolve_skill_catalog,
    resolve_skill_catalog_entry,
    resolve_skills_enabled,
    save_global_skill_state,
    save_project_skill_state,
    scaffold_skill_bundle,
    set_global_skill_disabled,
    set_project_skill_override,
    validate_skill_bundle,
)
from ...step_budget import DEFAULT_CHAT_MAX_STEPS
from ...swarm_orchestrator import run_swarm
from ...swarm_trace import SerializedSwarmTraceSink
from ...task_readiness import EXECUTION_UNREADY_SCOPE_WARNING
from ...task_scope import (
    check_scope,
    is_non_material_untracked_path,
    list_changed_files_including_untracked,
    list_untracked_packaging_metadata_paths,
    normalize_scope_patterns,
)
from ...token_budget import compute_input_budget, estimate_tokens
from ...tools.availability import get_tool_availability, is_tool_unavailable_result
from ...tools.registry import (
    iter_builtin_tool_metadata,
    summarize_tool_output_chunk,
    tool_input_preview,
)
from ...tools.web_search import resolve_web_search_runtime_status
from ...usage_tracker import aggregate_usage_from_session_logs, format_context_percent, format_usd
from ...verification_repair import TASK_STATUS_COMPLETED_UNVERIFIED
from ...verify_gate import (
    VerifyError,
    normalize_verify_mode,
    resolve_authoritative_task_verify_command_selection,
    resolve_verify_commands,
    run_task_verification,
    verify_run_result_to_payload,
)
from ...workspace_binding import (
    WorkspaceAction,
    WorkspaceBinding,
    WorkspaceBindingError,
    WorkspaceCandidate,
    ensure_workspace_policy,
    resolve_workspace_binding,
    workspace_policy_violation_message,
)
from ...workspace_binding_ui import (
    guarded_workspace_action_rows,
    workspace_candidate_rows,
)
from ...workspace_binding_ui import (
    resolve_startup_workspace_binding as _resolve_startup_workspace_binding_impl,
)
from ...workspace_context import resolve_workspace_context
from ..assets_cli import assets_app as forge_assets_app
from . import _patchable
from ._shared import (
    Mode,
    _console,
    _resolve_tool_workspace_root,
    _Table,
    _terminal_width,
)
from .config import (
    config_clear_api_key,
    config_menu_cmd,
    config_set,
    config_set_api_key,
    config_show,
)
from .conventions import conventions_list, conventions_render
from .extensions import (
    _confirm_extension_state_change,
    _ext_component_list,
    _ext_trust_prompt,
    _installed_record_for_id,
    _print_enable_result,
    _print_plugin_trust_request,
    _workspace_trust_status,
    ext_disable,
    ext_enable,
    ext_info,
    ext_install,
    ext_list,
    ext_search,
    ext_uninstall,
)
from .forge import (
    forge_attach,
    forge_exec,
    forge_plan,
    forge_review,
    forge_run,
    forge_show,
    forge_status,
    forge_swarm,
)
from .hooks import (
    _HOOK_SESSION_SOURCES,
    _HOOK_TOOL_EVENTS,
    _HOOKS_INIT_TEMPLATE,
    _collect_hook_source_statuses,
    _find_hook_in_layer,
    _hook_test_match_result,
    _hooks_config_path_for_layer,
    _HookSourceStatus,
    _project_hooks_config_or_exit,
    _set_hook_enabled,
    hooks_disable,
    hooks_doctor,
    hooks_effective,
    hooks_enable,
    hooks_init,
    hooks_list,
    hooks_test,
    hooks_trace,
    hooks_trust,
    hooks_untrust,
    hooks_watch,
)
from .profile import (
    _parse_profile_headers,
    _profile_api_key_status,
    profile_add,
    profile_clear_key,
    profile_list,
    profile_preset,
    profile_presets,
    profile_remove,
    profile_rename,
    profile_set_key,
    profile_show,
    profile_use,
)
from .report import report_create
from .root import (
    _default_session_api_key,
    _doctor_table,
    _tool_availability_rows,
    _ToolAvailabilityRow,
    _tools_table,
    chat,
    auth_app,
    config_app,
    conventions_app,
    doctor,
    ext_app,
    forge_app,
    hooks_app,
    main,
    mcp_app,
    mcp_auth_app,
    mcp_prompts_app,
    profile_app,
    report_app,
    run,
    sandbox_app,
    server_app,
    sessions_app,
    setup,
    skill_app,
    tool_app,
)
from .root import (
    app as _root_app,
)
from .root import (
    tools as _root_tools,
)
from .sandbox import (
    _print_sandbox_diagnostic,
    _run_sandbox_doctor_command,
    _run_sandbox_pull_command,
    _run_sandbox_setup_command,
    _sandbox_doctor_table,
    sandbox_doctor,
    sandbox_pull,
    sandbox_setup,
)
from .server import server_start
from .sessions import (
    sessions_list,
    sessions_score,
    sessions_show,
    sessions_usage,
)
from .skills import (
    _discover_skills_for_path,
    _print_skill_catalog_issues,
    _render_skill_validation_summary,
    _resolve_skill_catalog_for_path,
    _set_skill_enabled_state,
    _skills_table,
    skill_disable,
    skill_enable,
    skill_info,
    skill_init,
    skill_install,
    skill_list,
    skill_remove,
    skill_validate,
)
from .tools import (
    _CUSTOM_TOOLS_WIDE_TABLE_MIN_WIDTH,
    _custom_tool_source_filename,
    _custom_tool_source_location,
    _custom_tools_list_renderable,
    _custom_tools_stacked_list,
    _custom_tools_table,
    _discover_custom_tools_for_path,
    _resolve_custom_tool_entries_by_name,
    _select_project_tool_entry_or_exit,
    tool_info,
    tool_list,
    tool_trust,
    tool_untrust,
)

tools = _root_tools
app = _root_app

if TYPE_CHECKING:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table


def _Panel(*args: Any, **kwargs: Any) -> Any:
    from rich.panel import Panel

    return Panel(*args, **kwargs)


def _table_grid(*args: Any, **kwargs: Any) -> Any:
    from rich.table import Table

    return Table.grid(*args, **kwargs)


def _forge_bar_text(
    *,
    text: str,
    style: str = "dim",
    bar_style: str = "bright_black",
) -> Any:
    from rich.text import Text

    return Text.assemble(("│ ", bar_style), (str(text or ""), style))


def _print_forge_meta(*, console: Console, message: str, style: str = "dim") -> None:
    console.print(_forge_bar_text(text=message, style=style), highlight=False)


def _forge_supports_unicode_glyphs(console: Console) -> bool:
    # Legacy Windows consoles cannot render box-drawing/bullet glyphs; everything
    # else is UTF-capable (the CLI reconfigures stdout to UTF-8 on startup), so we
    # fall back to ASCII only when Rich flags a legacy console or a non-UTF encoding.
    if bool(getattr(console, "legacy_windows", False)):
        return False
    encoding = str(getattr(console, "encoding", "") or "").lower()
    return "utf" in encoding


def _forge_phase_rule(*, console: Console, label: str) -> Any:
    from rich.rule import Rule

    rule_char = "─" if _forge_supports_unicode_glyphs(console) else "-"
    title = str(label or "").strip()
    return Rule(title=title, characters=rule_char, style="bright_black", align="left")


def _print_forge_warning_messages(
    *,
    console: Console,
    label: str,
    warnings: list[str],
) -> None:
    for warning in warnings:
        console.print(
            _forge_bar_text(
                text=f"{label}: {warning}",
                style="yellow",
                bar_style="yellow",
            ),
            highlight=False,
        )


def _print_forge_error(*, console: Console, message: str) -> None:
    console.print(
        _forge_bar_text(
            text=message,
            style="red",
            bar_style="red",
        ),
        highlight=False,
    )


def _forge_task_status_counts(plan: dict[str, Any]) -> tuple[int, int, int]:
    from ...swarm_scheduler import canonical_task_status

    tasks = plan.get("tasks") or []
    if not isinstance(tasks, list):
        return 0, 0, 0

    done = 0
    failed = 0
    remaining = 0
    done_states = {"done", "already_satisfied", TASK_STATUS_COMPLETED_UNVERIFIED}
    failure_states = {
        "failed",
        "verify_failed",
        "candidate_rejected",
        "changes_requested",
        "merge_conflict",
        "blocked_integration",
        "blocked",
        "interrupted",
        "cancelled",
    }
    non_executable_obsolete_states = {"superseded", "invalidated"}
    for task in tasks:
        if not isinstance(task, dict):
            remaining += 1
            continue
        status = canonical_task_status(str(task.get("status") or ""))
        if status in done_states:
            done += 1
        elif status in failure_states:
            failed += 1
        elif status in non_executable_obsolete_states:
            continue
        else:
            remaining += 1
    return done, failed, remaining


def _forge_task_status_markup(status: str) -> str:
    from ...swarm_scheduler import canonical_task_status

    raw = str(status or "").strip()
    canonical = canonical_task_status(raw)
    display = raw or canonical or "planned"
    if canonical in {"done", "completed", "already_satisfied"}:
        return f"[bold]{display}[/bold]"
    if canonical == TASK_STATUS_COMPLETED_UNVERIFIED:
        # Completed, but nothing authoritative checked it — bold like the other
        # completions, dimmed so the table still shows it is a weaker result.
        return f"[bold bright_black]{display}[/bold bright_black]"
    if canonical in {
        "failed",
        "verify_failed",
        "candidate_rejected",
        "changes_requested",
        "merge_conflict",
        "blocked_integration",
        "blocked",
        "interrupted",
        "cancelled",
    }:
        return f"[red]{display}[/red]"
    if canonical in {"in_progress", "running"}:
        return display
    if canonical in {"superseded", "invalidated"}:
        return f"[bright_black]{display}[/bright_black]"
    return f"[dim]{display}[/dim]"


def _forge_task_table(*, title: str = "Forge Tasks") -> Any:
    from rich import box as rich_box

    return _Table(
        title=title,
        box=rich_box.SIMPLE,
        border_style=STYLE_CHROME,
        header_style=STYLE_EMPHASIS,
        title_style="dim",
        expand=True,
        padding=(0, 1),
    )


def _forge_task_mcp_scope(task: dict[str, Any]) -> ForgeTaskMcpScope | None:
    return normalize_task_mcp_scope(
        task.get("mcp_scope"),
        warning_prefix=f"Task {str(task.get('id') or '(unknown)')}",
    )[0]


def _forge_task_mcp_summary_label(task: dict[str, Any]) -> str:
    scope = _forge_task_mcp_scope(task)
    if scope is None:
        return "off"
    if scope.allow_resources and scope.allowed_tools:
        return "resources+tools"
    if scope.allow_resources:
        return "resources"
    return f"{len(scope.allowed_tools)} tool(s)"


def _build_forge_task_scoped_mcp_manager(
    *,
    workspace_root: Path,
    session_id: str,
    task_scope: ForgeTaskMcpScope | None,
) -> McpManager | ForgeTaskScopedMcpManager:
    return create_forge_task_scoped_mcp_manager(
        workspace_root=workspace_root,
        session_id=session_id,
        task_scope=task_scope,
    )


def _build_forge_mcp_execution_context_section(
    *,
    task_scope: ForgeTaskMcpScope | None,
    mcp_manager: McpManager | ForgeTaskScopedMcpManager,
) -> str:
    summary = mcp_manager.execution_context_summary()
    lines = [
        "## MCP Execution Context",
        "",
        f"- Task MCP Scope: {describe_task_mcp_scope(task_scope)}",
        "- write_scope governs local file mutation. mcp_scope governs remote MCP actions.",
    ]
    servers = summary.get("servers")
    if isinstance(servers, list) and servers:
        lines.append("- Available MCP Servers:")
        for raw_entry in servers:
            if not isinstance(raw_entry, dict):
                continue
            server_id = str(raw_entry.get("server_id") or "").strip()
            if not server_id:
                continue
            tool_names = [
                str(name).strip() for name in raw_entry.get("tool_names") or [] if str(name).strip()
            ]
            tool_label = ", ".join(tool_names) if tool_names else "(none)"
            resources_label = "yes" if raw_entry.get("resources_available") else "no"
            lines.append(
                f"- {server_id}: live_tools={tool_label}; generic_resources_available={resources_label}"
            )
    else:
        lines.append("- Available MCP Servers: (none)")
    return "\n".join(lines)


def _augment_workspace_context_with_mcp_execution_context(
    *,
    workspace_context: dict[str, Any] | None,
    workspace_root: Path,
) -> dict[str, Any] | None:
    if workspace_context is None:
        return None
    payload = dict(workspace_context)
    mcp_execution_context = build_mcp_execution_context_summary(
        workspace_root=workspace_root,
        runtime_kind=RuntimeKind.FORGE_EXEC,
    )
    if mcp_execution_context is not None:
        payload["mcp_execution_context"] = mcp_execution_context
    return payload


def _prompt_ask(*args: Any, **kwargs: Any) -> Any:
    from rich.prompt import Prompt

    return Prompt.ask(*args, **kwargs)


def _Live(*args: Any, **kwargs: Any) -> Any:
    from rich.live import Live

    return Live(*args, **kwargs)


def _cli_module_for_legacy_impl() -> Any:
    return sys.modules.get("alysis_code.cli") or sys.modules[__name__]


def _agent_loop_module() -> Any:
    from ... import agent_loop

    return agent_loop


def build_tools(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().build_tools(*args, **kwargs)


def create_session(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().create_session(*args, **kwargs)


def refresh_session_environment_context_message(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().refresh_session_environment_context_message(*args, **kwargs)


def refresh_session_workspace_binding_context_message(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().refresh_session_workspace_binding_context_message(*args, **kwargs)


def resolve_session_active_workdir_path(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().resolve_session_active_workdir_path(*args, **kwargs)


def resolve_workdir_relpath_within_workspace(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().resolve_workdir_relpath_within_workspace(*args, **kwargs)


def resolve_session_active_workdir_relpath(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().resolve_session_active_workdir_relpath(*args, **kwargs)


def set_session_active_workdir(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().set_session_active_workdir(*args, **kwargs)


def run_agent(*args: Any, **kwargs: Any) -> Any:
    return _agent_loop_module().run_agent(*args, **kwargs)


def _plan_assistant_module() -> Any:
    from ... import plan_assistant

    return plan_assistant


def run_planner_turn(*args: Any, **kwargs: Any) -> Any:
    return _plan_assistant_module().run_planner_turn(*args, **kwargs)


def prepare_planner_knowledge(*args: Any, **kwargs: Any) -> Any:
    return _prepare_planner_knowledge(*args, **kwargs)


def resolve_knowledge_workspace_root(*args: Any, **kwargs: Any) -> Any:
    return _resolve_knowledge_workspace_root(*args, **kwargs)


def apply_plan_update(*args: Any, **kwargs: Any) -> Any:
    return _plan_assistant_module().apply_plan_update(*args, **kwargs)


def apply_guarded_planner_plan_update(*args: Any, **kwargs: Any) -> Any:
    return _plan_assistant_module().apply_guarded_planner_plan_update(*args, **kwargs)


def summarize_plan_update(*args: Any, **kwargs: Any) -> Any:
    return _plan_assistant_module().summarize_plan_update(*args, **kwargs)


def _plan_reconciliation_module() -> Any:
    from ... import plan_reconciliation

    return plan_reconciliation


def reconcile_plan_with_workspace(*args: Any, **kwargs: Any) -> Any:
    return _plan_reconciliation_module().reconcile_plan_with_workspace(*args, **kwargs)


def summarize_plan_reconciliation(*args: Any, **kwargs: Any) -> Any:
    return _plan_reconciliation_module().summarize_plan_reconciliation(*args, **kwargs)


def _compaction_conversation_module() -> Any:
    from ...compaction import conversation_compactor

    return conversation_compactor


def _memory_marker() -> str:
    return str(_compaction_conversation_module().MEMORY_MARKER)


def _pins_marker() -> str:
    return str(_compaction_conversation_module().PINS_MARKER)


def _sanitize_messages_for_estimation(*args: Any, **kwargs: Any) -> Any:
    return _compaction_conversation_module().sanitize_messages_for_estimation(*args, **kwargs)


def _estimate_request_tokens(*args: Any, **kwargs: Any) -> Any:
    return _compaction_conversation_module().estimate_request_tokens(*args, **kwargs)


def _resolve_compaction_settings(*args: Any, **kwargs: Any) -> Any:
    from ...compaction.settings import resolve_compaction_settings

    return resolve_compaction_settings(*args, **kwargs)


def _make_rich_surface(*, console: Console, show_status_line: bool = True) -> Any:
    from ...surface.rich_surface import RichSurface

    return RichSurface(console=console, show_status_line=show_status_line)


_SCOPE_MODES = {"off", "warn", "strict"}
_CHAT_MODES = {m.value for m in Mode}
_CHAT_TRACE_LEVELS = {"off", "compact", "full"}
_CHAT_RESUME_MAX_CANDIDATES = 50
_CHAT_RESUME_CONTEXT_MARKER = "<resume_context>"
_CHAT_RESUME_CONTEXT_MAX_CHARS = 20_000
_CHAT_RESUME_CONTEXT_MAX_CONVERSATION_MESSAGES = 10
_CHAT_RESUME_CONTEXT_MAX_TOOL_EVENTS = 80
_CHAT_RESUME_CONTEXT_MAX_PATHS = 80
_CHAT_RESUME_CONTEXT_MAX_COMMANDS = 40
_CHAT_RESUME_CONTEXT_MAX_VERIFY = 24
_CHAT_RESUME_CONTEXT_MAX_WARNINGS = 24
_CHAT_RESUME_CONTEXT_MAX_EVENT_TYPES = 80
_CHAT_RESUME_CONTEXT_MAX_VALUE_CHARS = 600
_CHAT_RESUME_CONTEXT_MAX_PATH_CANDIDATE_SCAN = 2_000
_CHAT_RESUME_CONTEXT_PATH_KEY_FRAGMENTS = ("path", "file", "cwd")
_CHAT_RESUME_SESSION_ID_MAX_CHARS = 200
_MIN_TERMINAL_COLUMNS = 40
_MIN_TERMINAL_ROWS = 10
_SESSION_SUMMARY_MIN_MESSAGES = 4
_SESSION_SUMMARY_MAX_MESSAGES = 12
_SESSION_SUMMARY_MAX_TRANSCRIPT_CHARS = 2400
_SESSION_SUMMARY_MAX_TITLE_WORDS = 6
_CHAT_MODE_ALIASES = {
    "1": "review",
    "safe": "review",
    "review": "review",
    "2": "auto",
    "fast": "auto",
    "auto": "auto",
    "3": "readonly",
    "read": "readonly",
    "readonly": "readonly",
    "ro": "readonly",
    "4": "fullaccess",
    "full": "fullaccess",
    "fullaccess": "fullaccess",
    "full-access": "fullaccess",
    "full_access": "fullaccess",
}
_HOME_ACTION_ALIASES = {
    "1": "chat",
    "2": "run",
    "3": "setup",
    "4": "doctor",
    "5": "plan",
    "6": "quit",
}


def _ordered_unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


_CHAT_RETIRED_COMMANDS = {"/keys", "/tour", "/examples", "/chat", "/mode"}
_CHAT_GLOBAL_VISIBLE_COMMANDS = [
    "/help",
    "/login",
    "/permissions",
    "/persona",
    "/ask",
    "/status",
    "/subagents",
    "/terminals",
    "/pwd",
    "/usage",
    "/context",
    "/compact",
    "/clear",
    "/resume",
    "/stream",
    "/trace",
    "/config",
    "/toolbar",
    "/assets",
    "/image",
    "/forge",
    "/report",
    "/feedback",
    "/skill",
    "/exit",
]
_FORGE_COMMAND_TOKENS = [
    "/assistant",
    "/execute",
    "/goal",
    "/task",
    "/show",
    "/plan",
    "/done",
    "/back",
]
_FORGE_VISIBLE_COMMAND_TOKENS = [
    "/execute",
    "/goal",
    "/task",
    "/show",
    "/plan",
    "/done",
    "/back",
]
_FORGE_SHARED_CHAT_COMMANDS = [
    "/help",
    "/login",
    "/permissions",
    "/status",
    "/subagents",
    "/terminals",
    "/pwd",
    "/usage",
    "/context",
    "/compact",
    "/resume",
    "/stream",
    "/trace",
    "/config",
    "/toolbar",
    "/assets",
    "/image",
    "/report",
    "/feedback",
    "/skill",
    "/exit",
]
_FORGE_SUGGESTION_COMMANDS = _ordered_unique_strings(
    _FORGE_SHARED_CHAT_COMMANDS + _FORGE_VISIBLE_COMMAND_TOKENS
)
_FORGE_COMPLETER_COMMANDS = _ordered_unique_strings(
    _FORGE_SHARED_CHAT_COMMANDS
    + [
        "/usage hud",
        "/usage hud on",
        "/usage hud off",
        "/usage hud status",
        "/terminals list",
        "/terminals show",
        "/terminals kill",
        "/terminals help",
        "/execute plan",
        "/goal",
        "/task",
        "/show",
        "/done",
        "/back",
        "/plan tasks",
        "/plan markdown",
        "/plan md",
        "/plan edit",
    ]
)
_CHAT_COMMANDS = _ordered_unique_strings(
    _CHAT_GLOBAL_VISIBLE_COMMANDS
    + _FORGE_COMMAND_TOKENS
    + [
        "/",
        "/cd",
        "/forge",
        "/ctx",
        "/terminals list",
        "/terminals show",
        "/terminals kill",
        "/terminals help",
        "/forge resume",
        "/model-info",
        "/model",
        "/stream on",
        "/stream off",
        "/stream status",
        "/usage hud",
        "/usage hud on",
        "/usage hud off",
        "/usage hud status",
        "/terminals list",
        "/terminals show",
        "/terminals kill",
        "/terminals help",
        "/skill",
        "/paste-image",
        "/images",
        "/clear-images",
        "/clear",
    ]
)
_CHAT_PROMPT_TEXT = "> "
_CHAT_PROMPT_FALLBACK_LABEL = ">"
_CHAT_TOOLBAR_ITEM_ORDER = [
    "mode",
    "model",
    "stream",
    "trace",
    "images",
    "temp",
    "ctx",
    "subagents",
    "tokens",
    "cost",
    "forge",
]
_CHAT_LLM_ERROR_MAX_CHARS = 520
_CHAT_LLM_ERROR_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"(Authorization\s*:\s*)(.+)", re.IGNORECASE),
]
_CHAT_RESUME_SECRET_KEY_PATTERN = (
    r"[A-Z0-9_\-]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN|SECRET|PASSWORD|PASSWD|PWD)"
    r"[A-Z0-9_\-]*"
)
_CHAT_RESUME_SECRET_JSON_VALUE_RE = re.compile(
    rf"(?i)([\"']?{_CHAT_RESUME_SECRET_KEY_PATTERN}[\"']?\s*:\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_CHAT_RESUME_SECRET_VALUE_RE = re.compile(
    rf"(?i)\b({_CHAT_RESUME_SECRET_KEY_PATTERN})"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_CHAT_RESUME_SECRET_ENV_RE = re.compile(rf"(?i)\b({_CHAT_RESUME_SECRET_KEY_PATTERN})=([^\s,;]+)")


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
