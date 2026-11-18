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
from ...custom_tools import (
    CustomToolCatalogEntry,
    build_custom_tool_session_state,
    global_custom_tools_root,
    project_custom_tools_root,
    trust_project_tool,
    untrust_project_tool,
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
    current_branch,
    ensure_git_repo,
    staged_files,
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
from ...litellm_static_provider import (
    BUNDLED_MODEL_CATALOG_SOURCE,
    get_bundled_model_catalog_provenance,
)
from ...llm_error_display import classify_llm_error_display
from ...mcp.config import load_resolved_mcp_config
from ...mcp.manager import (
    McpManager,
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
from ...model_metadata_utils import parse_non_negative_float, parse_positive_int
from ...model_router import ROLE_CODING, resolve_model_for_role
from ...repo_scan import scan_workspace
from ...request_estimation import estimate_request_token_breakdown
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
from ...task_scope import (
    is_non_material_untracked_path,
)
from ...token_budget import compute_input_budget, estimate_tokens
from ...tools.availability import get_tool_availability, is_tool_unavailable_result
from ...tools.registry import (
    iter_builtin_tool_metadata,
    summarize_tool_output_chunk,
    tool_input_preview,
)
from ...tools.web_search_runtime import resolve_web_search_runtime_status
from ...usage_tracker import aggregate_usage_from_session_logs, format_context_percent, format_usd
from ...verify_gate import (
    VerifyError,
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
    from ...cli_run_deadline import finish_expired_managed_launch

    if not args:
        expired = finish_expired_managed_launch(**kwargs)
        if expired is not None:
            return expired
    return _agent_loop_module().run_agent(*args, **kwargs)


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
    "5": "quit",
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
    "/image",
    "/report",
    "/feedback",
    "/skill",
    "/exit",
]
_CHAT_COMMANDS = _ordered_unique_strings(
    _CHAT_GLOBAL_VISIBLE_COMMANDS
    + [
        "/",
        "/cd",
        "/ctx",
        "/terminals list",
        "/terminals show",
        "/terminals kill",
        "/terminals help",
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
