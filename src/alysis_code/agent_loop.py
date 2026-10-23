from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import ConfigError, resolve_run_deadline
from .execution_deadline import ExecutionDeadline
from .managed_host_deadline import (
    MANAGED_HOST_DEADLINE_UNIX_ENV,
    ManagedHostDeadlineError,
    clamp_to_managed_host_deadline,
)

if TYPE_CHECKING:
    from .config import AppConfig
    from .crash_diagnostics import CrashDiagnosticLogger
    from .mcp.manager import ForgeTaskScopedMcpManager, McpManager
    from .runtime_kind import RuntimeKind
    from .surface.base import Surface
    from .workspace_binding import WorkspaceBinding

# isort: off
# errors re-exports
from .agent.errors import (  # noqa: F401
    AgentRuntimeError,
    ApprovalDeclinedError,
    SessionWorkdirError,
)

# prompt_context re-exports
from .agent.prompt_context import (  # noqa: F401
    _IMAGE_ATTACHMENT_TURN_SYSTEM_HINT,
    _INLINE_CODE_SPAN_RE,
    _MAX_ROUTE_CONTEXT_ANCHORS,
    _MAX_ROUTE_CONTEXT_HINTS,
    _MAX_ROUTE_CONTEXT_VERIFY_COMMANDS,
    _MODE_FULLACCESS,
    _NON_REPO_MAX_RECENT_VISIBLE_HISTORY_CHARS,
    _NON_REPO_MAX_RECENT_VISIBLE_HISTORY_MESSAGES,
    _NON_REPO_MAX_RECENT_VISIBLE_HISTORY_TOTAL_CHARS,
    _REPO_REL_PATH_TOKEN_RE,
    _SYSTEM_PROMPT_ONE_SHOT_SECTION,
    _SYSTEM_PROMPT_SKILL_DISCOVERY_SECTION,
    _SYSTEM_PROMPT_SKILL_LIFECYCLE_SECTION,
    _SYSTEM_PROMPT_SUBAGENT_SECTION,
    _SYSTEM_PROMPT_WRITE_SECTION,
    ALWAYS_PROTECTED_WRITE_PREFIXES,
    CONVENTIONS_FILENAME,
    MAX_CONVENTIONS_CHARS,
    MAX_IMAGE_BYTES,
    MAX_POST_EXPLORE_ANCHOR_PATHS,
    MAX_SUBAGENT_CONTEXT_CHARS,
    MAX_SUBAGENT_CONTEXT_ITEMS,
    SYSTEM_PROMPT,
    PreparedSessionPromptContext,
    _build_plugin_activation_index,
    _build_user_message,
    _build_workspace_grounding_descriptor,
    _clean_workspace_hint,
    _component_plugin_allowed,
    _compose_session_system_prompt,
    _environment_context_message,
    _extract_repo_relative_paths_from_text,
    _extract_workspace_relation_paths_from_text,
    _filter_discovered_skills_for_plugins,
    _image_attachment_instruction_text,
    _is_host_managed_user_context_message,
    _merge_dropped_counts,
    _message_text_content,
    _normalize_rel_match_path,
    _normalize_repo_relative_hint_path,
    _normalize_scope_list,
    _normalize_workspace_relpath,
    _normalized_authoritative_verify_commands,
    _normalized_verify_commands,
    _paths_require_verification,
    _PluginActivationIndex,
    _read_workspace_hint_text,
    _recent_visible_non_repo_history,
    _repo_conventions_context,
    _repo_summary_data,
    _RepoSummaryData,
    _resolve_effective_verification_selection,
    _resolve_one_shot_repo_bootstrap_context,
    _resolve_requested_workdir_within_workspace,
    _resolve_session_pinned_prefix_len,
    _session_focus_dir_path,
    _session_focus_relpath,
    _session_repo_scan,
    _session_verify_command_selection,
    _session_workspace_binding_context_message,
    _session_workspace_grounding,
    _set_session_pinned_prefix_len,
    _should_prepare_repo_scan,
    _skill_plugin_id,
    _subagent_context_message,
    _truncate_non_repo_history_content,
    _untrusted_prompt_prelude_message,
    _workspace_binding_context_message,
    _workspace_hint_from_manifest_path,
    _workspace_hint_from_repo_scan,
    _workspace_hint_from_text,
    _workspace_hint_from_top_level_metadata,
    _workspace_kind_is_plain_dir,
    _workspace_kind_is_repo_backed,
    _workspace_relpath_for_path,
    _WorkspaceGroundingDescriptor,
    prepare_session_prompt_context,
    refresh_session_environment_context_message,
    refresh_session_workspace_binding_context_message,
    resolve_session_active_workdir_path,
    resolve_session_active_workdir_relpath,
    resolve_workdir_relpath_within_workspace,
    set_session_active_workdir,
)

# turn_path re-exports
from .agent.turn_path import (  # noqa: F401
    _ROUTER_EXECUTION_POSTURES,
    _build_turn_language_system_message,
    _normalize_turn_language_name,
    _normalize_turn_script_name,
    _OneShotRepoTurnIntent,
    _resolve_repo_turn_execution_intent,
)

# turn event helper re-exports
from .agent.turn.events import (  # noqa: F401
    _emit_assistant_message_events,
    _emit_message_delta_event,
    _emit_message_end_event,
    _emit_tool_call_completed_event,
    _emit_tool_call_progress_event,
    _emit_tool_call_started_event,
    _event_preview,
)

# provider-call helper re-exports
from .agent.llm_calls import (  # noqa: F401
    _FENCED_CODE_BLOCK_RE,
    _FINAL_SUMMARY_REWRITE_SYSTEM_PROMPT,
    _REWRITE_PROTECTED_TOKEN_RE,
    _extract_rewrite_protected_fragments,
    _is_fatal_non_repo_llm_error,
    _is_stream_unsupported_error,
    _llm_error_status_code,
    _main_agent_chat,
    _non_repo_chat,
    _registered_tool_schema_list,
    _request_messages_with_ephemeral_system_prompt_suffixes,
    _request_messages_with_ephemeral_system_prompts,
    _request_messages_with_ephemeral_user_messages,
    _rewrite_final_summary_for_language,
    _rewritten_text_preserves_technical_tokens,
    _safe_forced_tool_choice_for_recovery,
    _tool_schema_function_name,
)

# session re-exports
from .agent.session import (  # noqa: F401
    _DEFAULT_CREATE_MCP_MANAGER,
    AgentSession,
    NoopSurface,
    OpenAICompatClient,
    _build_workspace_trust_prompt,
    _filter_hooks_config_for_plugins,
    _git_branch,
    _git_is_dirty,
    _hook_plugin_id,
    _make_session_llm_client,
    _meaningful_surface_legacy_warning_handler,
    _meaningful_surface_warning_handler,
    _repo_summary,
    _surface_needs_startup_git_status,
    build_shell_runner,
    build_shell_runner_from_settings,
    build_background_shell_runner_from_settings,
    create_mcp_manager,
    create_session,
    scan_workspace,
)

# tools_assembly re-exports
from .agent.tools_assembly import (  # noqa: F401
    _BUILTIN_MODEL_DESCRIPTIONS,
    _MODE_PERMISSIVENESS_ORDER,
    _MODE_PERMISSIVENESS_RANK,
    _READONLY_MAIN_SESSION_BUILTIN_TOOL_NAMES,
    _READONLY_TOP_LEVEL_WEB_TOOL_NAMES,
    _ROUTING_MODE_CODE_ONLY,
    _SHELL_MUTATION_SNAPSHOT_METADATA_PREFIX,
    ToolDef,
    _built_in_tool_exposed_in_mode,
    _custom_tool_capability_summary,
    _custom_tool_plugin_id,
    _custom_tools_write_scope_restricted,
    _detect_command_mutation_paths,
    _drop_schema_descriptions,
    _filter_custom_tool_session_state_for_plugins,
    _filter_mcp_config_for_plugins,
    _list_git_workspace_snapshot_paths,
    _mcp_server_plugin_id,
    _mcp_tool_exposed_in_mode,
    _normalize_snapshot_ignore_paths,
    _path_matches_snapshot_ignore,
    _run_with_command_mutation_detection,
    _snapshot_workspace_for_command_mutation_detection,
    _tool_event_metadata,
    _unified_diff,
    _walk_workspace_snapshot_paths,
    _workspace_snapshot_signature,
    build_tools,
)

# mutation classification re-exports
from .agent.mutation_classification import (  # noqa: F401
    MutationPathCategory,
    MutationPathClassification,
    benign_runtime_mutation_paths,
    classify_mutation_path,
    classify_mutation_paths,
    material_mutation_paths,
)

# run budget policy re-exports
from .budget_policy import (  # noqa: F401
    STOP_REASON_RUN_BUDGET_EXHAUSTED,
    BudgetCancellationToken,
    BudgetWatchdog,
    resolve_budget_grace_seconds,
    resolve_run_budget_seconds,
)
from .cancellation import CombinedCancellationToken, CooperativeCancellationError

# execution deadline re-exports
from .execution_deadline import (  # noqa: F401
    DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS,
    DEFAULT_RUN_DEADLINE_SECONDS,
    MINIMUM_FORCED_SUMMARY_SECONDS,
    MINIMUM_LLM_START_SECONDS,
    MINIMUM_OPERATION_TIMEOUT_SECONDS,
    MINIMUM_SUBAGENT_START_SECONDS,
    MINIMUM_TOOL_START_SECONDS,
    DeadlineExhausted,
    deadline_timeout_or_raise,
    resolve_deadline_degradation_policy,
    temporarily_clamp_client_timeout,
    validate_deadline_seconds,
)

# completion gate controller re-exports
from .agent.completion_gate import (  # noqa: F401
    NON_FINAL_PROGRESS_PROBLEM,
    NON_FINAL_PROGRESS_STAGE,
    CompletionGateControllerState,
    CompletionGateDecision,
    CompletionGateDecisionKind,
    CompletionGateEvidenceSnapshot,
    build_completion_gate_snapshot,
    completion_gate_decision_payload,
    decide_completion_gate,
    normalize_completion_gate_failure_signature,
    record_completion_gate_decision,
)

# turn re-exports
from .agent.turn import (  # noqa: F401
    _ACTION_PROGRESS_FALLBACK_TOOL_NAMES,
    _ACTION_PROGRESS_TOOL_CATEGORIES,
    _EXPLORATION_FALLBACK_TOOL_NAMES,
    _EXPLORATION_TOOL_CATEGORIES,
    _FAILED_EDIT_STAGNATION_TOOL_NAMES,
    _FINAL_TOOL_ENABLED_STEP_SYSTEM_PROMPT,
    _FORCED_FINAL_SUMMARY_SYSTEM_PROMPT_TEMPLATE,
    _LOW_STEP_BUDGET_SYSTEM_PROMPT_TEMPLATE,
    _PHASE_BUDGET_EXPLORATION_SYSTEM_PROMPT_TEMPLATE,
    _PHASE_BUDGET_VERIFICATION_SYSTEM_PROMPT_TEMPLATE,
    _SUBAGENT_REQUIRED_NUDGE_TEMPLATE,
    _SAME_BATCH_FS_READ_DEFAULT_MAX_BYTES,
    _SAME_BATCH_READ_WINDOW_DEFAULT_MAX_LINES,
    _SAME_BATCH_READ_CACHE_SAFE_TOOL_NAMES,
    _UNEXECUTED_TOOL_CALL_MARKUP_MARKERS,
    MAX_EDIT_NUDGES_PER_TURN,
    MAX_EXPLORATION_NUDGES_PER_TURN,
    MAX_EXPLORATION_ONLY_STEPS_BEFORE_NUDGE,
    MAX_FAILED_EDIT_STEPS_BEFORE_NUDGE,
    MAX_IDENTICAL_EXPLORATION_ATTEMPTS,
    MAX_IDENTICAL_FAILED_EDIT_ATTEMPTS,
    MAX_IDENTICAL_TOOL_CALL_FAILURES,
    MAX_NON_FINAL_CONTINUATIONS_PER_TURN,
    MAX_POST_EXPLORE_BOOTSTRAP_NUDGES_PER_TURN,
    MAX_RECENT_EXPLORATION_PATHS,
    MAX_SUBAGENT_REQUIRED_NUDGES_PER_TURN,
    _append_recent_exploration_path,
    _build_read_window_result_from_cached_range,
    _build_read_window_result_from_full_read,
    _build_post_explore_bootstrap_nudge,
    _coerce_read_window_request,
    _coerce_fs_read_request,
    _edit_similarity_key,
    _emit_surface_error,
    _exploration_attempt_outcome,
    _exploration_similarity_key,
    _extract_successful_exploration_paths,
    _has_invalid_tool_call_json,
    _is_action_progress_tool,
    _is_exploration_only_tool,
    _is_failed_edit_stagnation_tool,
    _is_successful_subagent_run,
    _looks_like_unexecuted_tool_call_markup,
    _maybe_reuse_same_batch_read_result,
    _one_shot_progress_fingerprint,
    _remember_same_batch_read_result,
    _same_batch_read_cache_should_invalidate,
    _same_batch_read_path_key,
    _SameBatchReadWindowRecord,
    _SameBatchFsReadRecord,
    _SameBatchReadReuseCache,
    _SubagentTurnPolicy,
    _SubagentTurnPolicyLevel,
    _split_text_preserving_lines,
    _tool_call_retry_key,
    _tool_categories,
    _resolve_subagent_turn_policy,
    _subagent_names_preview,
    _subagent_required_nudge_message,
    _subagent_turn_context_message,
)

# verification re-exports
from .agent.verification import (  # noqa: F401
    _COMMAND_LIKE_MUTATION_TOOL_NAMES,
    _COMPLETION_GATE_PROBLEM_LABELS,
    _MATERIAL_EDIT_TOOL_NAMES,
    _ONE_SHOT_COMPLETION_GATE_NUDGE_PREFIX,
    _RUNTIME_DEFAULT_LANGUAGE,
    _RUNTIME_MESSAGE_CATALOG,
    _VERIFICATION_SHELL_MARKERS,
    TurnExecutionState,
    _build_interactive_turn_verify_task,
    _completion_gate_blocker_allows_final,
    _completion_gate_nudge_message,
    _completion_gate_problem_summary,
    _completion_gate_problems,
    _completion_gate_repair_stage,
    _extract_touched_repo_paths,
    _fresh_executed_evidence_for_claim,
    _record_shell_verification_command_outcome,
    _record_tool_effect,
    _record_verify_run_command_outcomes,
    _refresh_execute_turn_verification_selection,
    _refresh_interactive_turn_verification_selection,
    _runtime_message,
    _runtime_message_locale,
    _sorted_missing_verification_commands,
    _successful_verification_claim_kind,
    _verification_attempt_passed,
    _verification_command_result_passed,
    _verification_command_result_snippet,
    _verification_expected_for_turn,
    _verification_failure_category_for_tool_result,
    extract_verification_failure_snippet,
    run_task_verification,
)

# verification evidence re-exports
from .agent.verification_evidence import (  # noqa: F401
    VerificationEvidence,
    VerificationEvidenceCategory,
    classify_verification_evidence,
)

# verification_commands re-exports
from .agent.verification_commands import (  # noqa: F401
    _CARGO_TEST_NON_EXECUTING_OPTIONS,
    _DISALLOWED_VERIFICATION_SHELL_TOKENS,
    _GO_TEST_NON_EXECUTING_OPTIONS,
    _MYPY_NON_EXECUTING_OPTIONS,
    _NON_VERIFICATION_META_OPTIONS,
    _PYTEST_NON_EXECUTING_OPTIONS,
    _PYTEST_NON_VERIFICATION_OPTIONS,
    _PYTEST_REPORTER_OPTIONS,
    _RUFF_CHECK_NON_EXECUTING_OPTIONS,
    _VERIFICATION_ENV_ASSIGNMENT_RE,
    VerificationCommandShape,
    _canonicalize_verification_command_for_match,
    _command_options_include,
    _effective_verification_command_matches,
    _has_disallowed_shell_control_flow,
    _looks_like_env_assignment_token,
    _looks_like_verification_entrypoint,
    _marker_fallback_is_verification_attempt,
    _matching_effective_verification_commands,
    _normalize_and_unwrap_verification_command,
    _normalize_shell_command_for_match,
    _parse_verification_command_shape,
    _pytest_option_is_reporter_variant,
    _shell_command_is_verification_attempt,
    _split_verification_shape_args,
    _strip_verification_env_prefix,
    _strip_verification_runner_prefix,
    _unwrap_shell_wrapper_command,
    _verification_command_shapes_match,
    _verification_shape_is_real_execution_mode,
    _verify_run_commands_match_effective_contract,
)

# tools.availability re-exports
from .tools.availability import (  # noqa: F401
    is_tool_unavailable_result,
    mark_available,
    mark_unavailable,
    register_tool_availability,
    unavailable_tool_result,
)

# tools.fs re-exports
from .tools.fs import fs_list, fs_read  # noqa: F401

# tools.search re-exports
from .tools.search import search_rg  # noqa: F401

# tools.shell re-exports
from .tools.shell import shell_run  # noqa: F401

# tools.symbols re-exports
from .tools.symbols import symbol_search  # noqa: F401

# tools.web re-exports
from .tools.web import web_fetch  # noqa: F401

# isort: on


def _run_turn_accepts_keyword(run_turn: Callable[..., Any], keyword: str) -> bool:
    """Whether a session's ``run_turn`` takes ``keyword`` (legacy embedders may not)."""

    try:
        signature = inspect.signature(run_turn)
    except (TypeError, ValueError):
        # Opaque callables (C wrappers, mocks without signatures) get the
        # capable path, matching the runtime session they normally wrap.
        return True
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            and parameter.name == keyword
        ):
            return True
    return False


def _emit_required_run_deadline_missing(
    *,
    crash_diagnostic_log_path: str | Path | None,
    crash_diagnostic_logger: CrashDiagnosticLogger | None,
    runtime_kind_text: str,
    deadline_config_source: str,
) -> None:
    logger = crash_diagnostic_logger
    if logger is None:
        from .crash_diagnostics import build_crash_diagnostic_logger

        logger = build_crash_diagnostic_logger(
            path=crash_diagnostic_log_path,
            run_id="pre_session",
            session_id="pre_session",
            runtime_kind=runtime_kind_text,
        )
    logger.event(
        "required_run_deadline_missing",
        {
            "status": "blocked",
            "reason": "required_deadline_absent",
            "deadline_config_source": deadline_config_source,
            "runtime_kind": runtime_kind_text,
        },
        durable=True,
    )


def run_agent(
    *,
    cfg: AppConfig,
    root: Path,
    instruction: str,
    acceptance_instruction: str | None = None,
    image_paths: list[str] | None = None,
    mode: str,
    yes: bool,
    max_steps: int | None,
    no_log: bool,
    api_key_override: str | None = None,
    console: Any | None = None,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    persona_allow_write_globs: list[str] | None = None,
    non_interactive: bool = False,
    one_shot_execution: bool = False,
    ephemeral_system_messages: list[str] | tuple[str, ...] | None = None,
    ephemeral_user_messages: list[str] | tuple[str, ...] | None = None,
    enable_chat_turn_step_budget: bool = False,
    chat_turn_fixed_override: int | None = None,
    session_log_dir_override: Path | None = None,
    session_id_override: str | None = None,
    seed_messages: list[dict[str, Any]] | None = None,
    managed_session_persistence: Any | None = None,
    surface: Surface | None = None,
    usage_role: str = "main",
    trusted_system_prompt_override: str | None = None,
    trusted_system_prompt_append: str | None = None,
    untrusted_prompt_prelude: str | None = None,
    enable_compaction: bool = True,
    enable_tool_output_offload: bool | None = None,
    enable_conversation_summarization: bool | None = None,
    compaction_profile: str = "chat",
    verification_enabled: bool = True,
    authoritative_verification_commands: list[str] | None = None,
    verify_cmd: list[str] | None = None,
    subagents_enabled: bool | None = None,
    enforce_explicit_subagent_requests: bool = True,
    workspace_binding: WorkspaceBinding | None = None,
    runtime_kind: RuntimeKind | str | None = None,
    mcp_manager: McpManager | ForgeTaskScopedMcpManager | None = None,
    execution_deadline: ExecutionDeadline | None = None,
    run_deadline_seconds: float | None = None,
    no_run_deadline: bool = False,
    require_run_deadline: bool = False,
    crash_diagnostic_log_path: str | Path | None = None,
    crash_diagnostic_logger: CrashDiagnosticLogger | None = None,
    cancellation_token: Any | None = None,
    session_source: str = "startup",
    session_source_metadata: dict[str, Any] | None = None,
    tool_dispatch_guard: Any | None = None,
) -> int:
    runtime_kind_text = str(getattr(runtime_kind, "value", runtime_kind) or "").strip()
    if execution_deadline is not None and run_deadline_seconds is not None:
        raise ConfigError("run_deadline_seconds cannot be combined with execution_deadline")

    if execution_deadline is None:
        deadline_has_one_shot_semantics = (
            one_shot_execution
            or runtime_kind_text in {"one_shot", "forge_exec", "swarm_worker"}
            or run_deadline_seconds is not None
            or no_run_deadline
        )
        if deadline_has_one_shot_semantics or require_run_deadline:
            # A non-interactive run gets a generous default budget so it cannot
            # grind for hours unattended. Two exclusions: a managed host that
            # asked to fail closed (silently defaulting would defeat the check
            # it opted into), and an explicit --no-deadline. Interactive chat
            # never reaches here.
            default_run_deadline_seconds = (
                # Sized by ALYSIS_RUN_BUDGET_SECONDS, defaulting to the same
                # 3600 this replaced. Only the default rung moves: an explicit
                # --deadline-seconds, ALYSIS_RUN_DEADLINE_SECONDS, or a
                # configured run_deadline_seconds still wins, as before.
                resolve_run_budget_seconds()
                if (
                    deadline_has_one_shot_semantics
                    and not require_run_deadline
                    and not no_run_deadline
                )
                else None
            )
            resolved_deadline = resolve_run_deadline(
                cfg,
                cli_deadline_seconds=run_deadline_seconds,
                cli_no_deadline=no_run_deadline,
                default_seconds=default_run_deadline_seconds,
            )
            if resolved_deadline.seconds is None:
                if require_run_deadline:
                    _emit_required_run_deadline_missing(
                        crash_diagnostic_log_path=crash_diagnostic_log_path,
                        crash_diagnostic_logger=crash_diagnostic_logger,
                        runtime_kind_text=runtime_kind_text,
                        deadline_config_source=resolved_deadline.source,
                    )
                    raise ConfigError(
                        "Managed-host run requires a finite run deadline. "
                        "Pass --deadline-seconds, set ALYSIS_RUN_DEADLINE_SECONDS, "
                        "or configure run_deadline_seconds."
                    )
            else:
                execution_deadline = ExecutionDeadline.from_duration(
                    resolved_deadline.seconds,
                    source=resolved_deadline.source,
                    degradation_policy=resolve_deadline_degradation_policy(cfg),
                )
    elif require_run_deadline and not execution_deadline.enabled:
        _emit_required_run_deadline_missing(
            crash_diagnostic_log_path=crash_diagnostic_log_path,
            crash_diagnostic_logger=crash_diagnostic_logger,
            runtime_kind_text=runtime_kind_text,
            deadline_config_source=str(getattr(execution_deadline, "source", "execution_deadline")),
        )
        raise ConfigError(
            "Managed-host run requires a finite run deadline. "
            "Pass --deadline-seconds, set ALYSIS_RUN_DEADLINE_SECONDS, "
            "or configure run_deadline_seconds."
        )

    managed_host_anchor_record: dict[str, Any] | None = None
    if require_run_deadline and execution_deadline is not None:
        host_anchor = os.environ.get(MANAGED_HOST_DEADLINE_UNIX_ENV)
        if host_anchor is not None:
            try:
                managed_host_anchor_record = clamp_to_managed_host_deadline(
                    execution_deadline, host_anchor
                )
            except ManagedHostDeadlineError as exc:
                raise ConfigError(str(exc)) from exc

    session = create_session(
        cfg=cfg,
        root=root,
        mode=mode,
        runtime_kind=runtime_kind,
        yes=yes,
        max_steps=max_steps,
        no_log=no_log,
        api_key_override=api_key_override,
        console=console,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        persona_allow_write_globs=persona_allow_write_globs,
        non_interactive=non_interactive,
        one_shot_execution=one_shot_execution,
        enable_chat_turn_step_budget=enable_chat_turn_step_budget,
        chat_turn_fixed_override=chat_turn_fixed_override,
        session_log_dir_override=session_log_dir_override,
        session_id_override=session_id_override,
        managed_session_persistence=managed_session_persistence,
        surface=surface,
        usage_role=usage_role,
        trusted_system_prompt_override=trusted_system_prompt_override,
        trusted_system_prompt_append=trusted_system_prompt_append,
        untrusted_prompt_prelude=untrusted_prompt_prelude,
        enable_compaction=enable_compaction,
        enable_tool_output_offload=enable_tool_output_offload,
        enable_conversation_summarization=enable_conversation_summarization,
        compaction_profile=compaction_profile,
        verification_enabled=verification_enabled,
        authoritative_verification_commands=authoritative_verification_commands,
        verify_cmd=verify_cmd,
        subagents_enabled=subagents_enabled,
        enforce_explicit_subagent_requests=enforce_explicit_subagent_requests,
        workspace_binding=workspace_binding,
        mcp_manager=mcp_manager,
        execution_deadline=execution_deadline,
        crash_diagnostic_log_path=crash_diagnostic_log_path,
        crash_diagnostic_logger=crash_diagnostic_logger,
        session_source=session_source,
        session_source_metadata=session_source_metadata,
        tool_dispatch_guard=tool_dispatch_guard,
    )
    if managed_host_anchor_record is not None:
        session.store.append("managed_host_deadline_anchor", managed_host_anchor_record)
        if session.crash_diagnostics is not None:
            session.crash_diagnostics.event(
                "managed_host_deadline_anchor", managed_host_anchor_record, durable=True
            )
    if acceptance_instruction is not None:
        session.acceptance_instruction = acceptance_instruction
    # Arm the run-budget stop gate. Every deadline check inside the engine is a
    # *start* gate evaluated between operations; nothing re-reads the clock
    # while an operation is in flight, so a blocking call that does not bound
    # itself by the deadline can outlive the budget indefinitely -- which is
    # how runs that had already recorded exhausted:true kept going for hours.
    # This daemon timer trips a cancellation event at the hard deadline, which
    # unblocks the cooperative checkpoints (step loop, mid-stream LLM read,
    # subagent joins) no matter where the run is parked. Caller cancellation is
    # composed with the deadline: supplying a UI token must not disable it.
    watchdog: BudgetWatchdog | None = None
    if execution_deadline is not None and execution_deadline.enabled:

        def _record_budget_watchdog_fired() -> None:
            payload = {
                "reason": STOP_REASON_RUN_BUDGET_EXHAUSTED,
                "grace_seconds": 0.0,
                "requested_grace_seconds": resolve_budget_grace_seconds(),
                "deadline": execution_deadline.telemetry_snapshot(),
            }
            session.store.append("run_budget_watchdog_fired", payload)
            if session.crash_diagnostics is not None:
                session.crash_diagnostics.event(
                    "run_budget_watchdog_fired",
                    payload,
                    durable=True,
                )

        watchdog = BudgetWatchdog(
            # Remaining, not configured: session construction happens after the
            # clock starts. Grace must not extend the effective host deadline.
            budget_seconds=float(execution_deadline.remaining_seconds() or 0.0),
            grace_seconds=0.0,
            on_fire=_record_budget_watchdog_fired,
        )
        budget_token = BudgetCancellationToken(
            watchdog.event,
            # The engine's existing handlers catch CooperativeCancellationError;
            # the token attributes the stop to the budget via its reason, which
            # is what makes it finalize as a clean exit instead of an abort.
            error_class=CooperativeCancellationError,
        )
        cancellation_token = (
            budget_token
            if cancellation_token is None
            else CombinedCancellationToken(cancellation_token, budget_token)
        )
        watchdog.arm()
    try:
        if seed_messages:
            session.messages.extend(seed_messages)
        turn_kwargs: dict[str, Any] = {
            "image_paths": image_paths,
            "cancellation_token": cancellation_token,
        }
        if ephemeral_system_messages:
            turn_kwargs["ephemeral_system_messages"] = list(ephemeral_system_messages)
        if ephemeral_user_messages:
            turn_kwargs["ephemeral_user_messages"] = list(ephemeral_user_messages)
        return session.run_turn(instruction, **turn_kwargs)
    finally:
        if watchdog is not None:
            watchdog.disarm()
        # close() is left with its default reason on purpose: `status` feeds
        # the agentbox error flag, and a budget stop is not an error. The
        # machine-readable marker travels as session.stop_reason instead.
        session.close()
