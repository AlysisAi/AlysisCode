from __future__ import annotations

import copy
import inspect
import json
import os
import re
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import BoundedSemaphore, Event, RLock
from time import perf_counter
from typing import Any, Literal

from ..atomic_io import atomic_write_text
from ..budget_policy import is_budget_stop, is_clean_stop
from ..cancellation import CooperativeCancellationError
from ..capabilities import get_capability_definition
from ..config import AppConfig, ConfigError, resolve_role_temperature
from ..crash_diagnostics import CrashDiagnosticLogger
from ..execution_deadline import (
    MINIMUM_SUBAGENT_START_SECONDS,
    DeadlineOperation,
    ExecutionDeadline,
    derive_subagent_deadline,
)
from ..internal_artifacts import (
    INTERNAL_FALLBACK_SOURCE,
    SUBAGENT_INCOMPLETE_ERROR_CODE,
    SUBAGENT_PARTIAL_REPORT_MAX_CHARS,
    SubagentIncompleteStatus,
    resolve_incomplete_reason,
    subagent_report_is_internal,
)
from ..model_router import ROLE_CODING, resolve_model_for_role
from ..run_outcome import task_outcome_fields
from ..runtime_kind import RuntimeKind
from ..safety.subagent_report import sanitize_subagent_report, subagent_report_evidence_text
from ..session_store import SessionStore, read_session_events
from ..step_budget import StepBudgetRequest, resolve_step_budget
from ..subagent_labels import subagent_task_label
from ..subagents import (
    _MODE_PERMISSIVENESS_ORDER as _MODE_PERMISSIVENESS_ORDER,
)
from ..subagents import _MODE_PERMISSIVENESS_RANK as _MODE_PERMISSIVENESS_RANK
from ..subagents import (
    EDIT_CAPABLE_SUBAGENT_TOOL_NAMES,
    SUBAGENT_MODES,
    SubagentDefinition,
    available_subagent_names,
    canonical_subagent_name,
    clamp_subagent_mode,
    normalize_subagent_mode,
    required_subagent_tool_names,
    resolve_subagent_model_role,
    resolve_subagent_tool_scope,
    smallest_sufficient_subagent_mode,
    subagent_unavailability,
)
from ..surface import NestedSubagentSurface, SubagentEndEvent, SubagentStartEvent
from ..surface.base import Surface
from ..tools.registry import (
    built_in_subagent_tool_names,
    get_builtin_tool_metadata,
    summarize_tool_output_chunk,
    tool_display_name,
    tool_input_preview,
)
from ..usage_tracker import UsageRecord, UsageSummary
from . import _patchable
from .errors import AgentRuntimeError
from .prompt_context import (
    _extract_repo_relative_paths_from_text,
)
from .steering import SteerInbox, wait_signal_digest
from .subagent_workspace import SubagentWorkspaceProvider
from .turn.snapshot import (
    _detect_command_mutation_paths,
    _normalize_snapshot_ignore_paths,
    _path_matches_snapshot_ignore,
    _snapshot_workspace_for_command_mutation_detection,
)

_AUTHORITATIVE_SUBAGENT_FINAL_TEXT_SOURCES = frozenset({"store_final", "surface_assistant_done"})
_FORCED_FINAL_TERMINATION_EVENT_TYPES = frozenset(
    {"forced_final_summary_completed", "forced_final_summary_fallback"}
)
_EXHAUSTION_TERMINATION_KINDS = frozenset(
    {"deadline_exhausted", "run_budget_exhausted", "step_budget_exhausted"}
)
_SUBAGENT_REPORT_EXPECTATIONS = """Child result expectations:
- Answer the assigned task with relevant findings or completed work, not just an acknowledgement.
- Include source references or observed checks when they support the result. State uncertainty,
  remaining work, and blockers honestly; do not invent evidence or claim checks you did not run.
- A final child report is evidence for the parent to assess and integrate, not independent proof
  that the user's whole request is complete. Keep the report focused on your assignment."""


_COMPLETION_NOTIFICATION_REPORT_MAX_CHARS = 4000


def _latest_subagent_store_final_text(sub_session: Any) -> tuple[str, bool, dict[str, Any]]:
    """Last recorded final answer of a child session, plus its ``final`` payload.

    The payload is returned so the caller can tell a real final report from a
    locally generated stop report using the recorded ``internal_fallback`` fact,
    rather than by inspecting the text.
    """
    child_store = getattr(sub_session, "store", None)
    events_snapshot = getattr(child_store, "events_snapshot", None)
    if not callable(events_snapshot):
        return "", False, {}
    try:
        events = events_snapshot()
    except Exception:  # noqa: BLE001 - result capture should fall back instead of failing.
        return "", False, {}
    for event in reversed(events if isinstance(events, list) else []):
        if not isinstance(event, dict) or str(event.get("type") or "") != "final":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("content") or "").strip()
        if text:
            return text, True, payload
    return "", True, {}


def _latest_subagent_message_text(sub_session: Any) -> str:
    for message in reversed(getattr(sub_session, "messages", [])):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        text = str(message.get("content") or "").strip()
        if text:
            return text
    return ""


def _resolve_subagent_final_text(
    *,
    sub_session: Any,
    subagent_surface: NestedSubagentSurface,
) -> tuple[str, str]:
    store_text, store_checked, store_payload = _latest_subagent_store_final_text(sub_session)
    if store_text:
        if bool(store_payload.get("internal_fallback")):
            # The child ran out of deadline or steps and the runtime wrote a local
            # stop report from its own transcript. That is internal state, not a
            # deliverable; the caller converts it into a structured incomplete
            # status instead of passing the prose up.
            return store_text, INTERNAL_FALLBACK_SOURCE
        return store_text, "store_final"
    if not store_checked:
        surface_text = str(subagent_surface.last_assistant_message_done or "").strip()
        if surface_text:
            return surface_text, "surface_assistant_done"
    message_text = _latest_subagent_message_text(sub_session)
    if message_text:
        return message_text, "assistant_message"
    return "", "missing"


def _subagent_termination_kind(sub_session: Any) -> str:
    """Return the child's recorded forced-final termination kind."""
    child_store = getattr(sub_session, "store", None)
    events_snapshot = getattr(child_store, "events_snapshot", None)
    if not callable(events_snapshot):
        return ""
    try:
        events = events_snapshot()
    except Exception:  # noqa: BLE001 - diagnostics must not break result handling
        return ""
    for event in reversed(events if isinstance(events, list) else []):
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") not in _FORCED_FINAL_TERMINATION_EVENT_TYPES:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("termination_kind") or "").strip()
    return ""


def _subagent_stop_reason(sub_session: Any, *, deadline_exhausted: bool) -> str:
    """Return the host-recorded reason that actually prevented the next step."""
    stop_reason = str(getattr(sub_session, "stop_reason", "") or "").strip()
    if is_clean_stop(stop_reason) and not is_budget_stop(stop_reason):
        # Model-response limits produce a local final report directly, without
        # forced-summary events. Preserve their cause instead of mislabeling
        # them as an exhausted step budget. Deadline stops still use the finer
        # blocked-operation diagnostics below.
        return stop_reason
    child_store = getattr(sub_session, "store", None)
    events_snapshot = getattr(child_store, "events_snapshot", None)
    if not callable(events_snapshot):
        return resolve_incomplete_reason(
            termination_kind="",
            deadline_exhausted=deadline_exhausted,
        )
    try:
        events = events_snapshot()
    except Exception:  # noqa: BLE001 - diagnostics must not break result handling
        events = []
    event_list = events if isinstance(events, list) else []
    termination_kind = ""
    fallback_index = len(event_list)
    for index in range(len(event_list) - 1, -1, -1):
        event = event_list[index]
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") not in _FORCED_FINAL_TERMINATION_EVENT_TYPES:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            termination_kind = str(payload.get("termination_kind") or "").strip()
            fallback_index = index
        break
    if termination_kind in {"deadline_exhausted", "run_budget_exhausted"}:
        for event in reversed(event_list[:fallback_index]):
            if not isinstance(event, dict):
                continue
            if str(event.get("type") or "") != "deadline_operation_blocked":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            decision = payload.get("decision")
            reason = str(decision.get("reason") or "").strip() if isinstance(decision, dict) else ""
            reason = reason or str(payload.get("reason") or "").strip()
            if reason:
                return reason
    return resolve_incomplete_reason(
        termination_kind=termination_kind,
        deadline_exhausted=deadline_exhausted,
    )


def _subagent_deadline_event_summary(sub_session: Any) -> tuple[int, bool]:
    """Return blocked-operation count and whether terminal exhaustion was recorded."""
    child_store = getattr(sub_session, "store", None)
    events_snapshot = getattr(child_store, "events_snapshot", None)
    if not callable(events_snapshot):
        return 0, False
    try:
        events = events_snapshot()
    except Exception:  # noqa: BLE001 - diagnostics must not break result handling
        return 0, False
    event_list = events if isinstance(events, list) else []
    blocked_operations = sum(
        isinstance(event, dict) and str(event.get("type") or "") == "deadline_operation_blocked"
        for event in event_list
    )
    exhausted_recorded = any(
        isinstance(event, dict) and str(event.get("type") or "") == "deadline_exhausted"
        for event in event_list
    )
    return blocked_operations, exhausted_recorded


def _persist_internal_subagent_report(
    *,
    store: Any,
    subagent_session_id: str,
    report_text: str,
) -> str:
    """Write the child's stop report to the session store as an internal artifact.

    Returns the artifact locator, or ``""`` when artifacts are not being
    persisted (``--no-log``). The raw report remains internal; callers may
    separately return a bounded excerpt only after applying the report sanitizer.
    """
    if not report_text.strip():
        return ""
    if not bool(getattr(store, "artifact_persistence_enabled", False)):
        return ""
    try:
        layout = store.session_artifact_layout
        session_key = subagent_session_id or "unknown"
        artifact_path = layout.artifact_fs_path("subagent_incomplete", f"{session_key}.md")
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(artifact_path, report_text)
        return layout.locator_for_path(artifact_path)
    except Exception:  # noqa: BLE001 - a missing artifact must not break containment
        return ""


def _screen_partial_subagent_report(report_text: str) -> dict[str, Any]:
    partial_report = sanitize_subagent_report(report_text)
    excerpt = partial_report.text[:SUBAGENT_PARTIAL_REPORT_MAX_CHARS]
    if not excerpt.strip():
        return {
            "status": "unavailable",
            "reason": "no_screenable_content",
            "excerpt": "",
            "truncated": False,
            "max_chars": SUBAGENT_PARTIAL_REPORT_MAX_CHARS,
            "safety": partial_report.metadata(),
        }
    return {
        "status": "partial",
        "excerpt": excerpt,
        "truncated": len(partial_report.text) > SUBAGENT_PARTIAL_REPORT_MAX_CHARS,
        "max_chars": SUBAGENT_PARTIAL_REPORT_MAX_CHARS,
        "safety": partial_report.metadata(),
    }


def _subagent_final_report_problem(*, text: str, source: str) -> str | None:
    if not str(text or "").strip():
        return "missing_final_report"
    if source not in _AUTHORITATIVE_SUBAGENT_FINAL_TEXT_SOURCES:
        return "missing_final_report_signal"
    evidence_text = subagent_report_evidence_text(text)
    # The host can establish that a final report exists and survives screening.
    # Its usefulness depends on the assignment and language; that judgement
    # belongs to the child/parent models, not a vocabulary of acknowledgements.
    if not evidence_text.strip() or (
        evidence_text != text and not any(character.isalnum() for character in evidence_text)
    ):
        return "non_substantive_final_report"
    return None


def _subagent_exact_tool_catalog_message(
    tools: dict[str, Any],
    *,
    max_tools: int = 80,
    max_chars: int = 8000,
) -> str:
    lines = [
        "<available_tool_catalog>",
        "Use only these exact tool names. Do not invent aliases or call unavailable tools.",
        "If a tool call returns unknown_tool, inspect its structured recovery payload and retry once with an exact listed name.",
        "tools:",
    ]
    for index, name in enumerate(sorted(tools)):
        if index >= max_tools:
            lines.append("- ...(truncated)")
            break
        tool = tools[name]
        required = tool.parameters.get("required") if isinstance(tool.parameters, dict) else []
        required_args = [
            str(item)
            for item in (required if isinstance(required, list) else [])
            if str(item).strip()
        ]
        purpose = " ".join(
            str(tool.metadata.get("model_description") or tool.description or "").split()
        )
        if len(purpose) > 140:
            purpose = purpose[:137].rstrip() + "..."
        required_text = ", ".join(required_args) if required_args else "(none)"
        candidate = f"- {name}: {purpose or '-'} required_args={required_text}"
        projected = "\n".join([*lines, candidate, "</available_tool_catalog>\n"])
        if len(projected) > max_chars:
            lines.append("- ...(truncated)")
            break
        lines.append(candidate)
    lines.append("</available_tool_catalog>\n")
    return "\n".join(lines)


def _subagent_helper_catalog_message(
    *,
    registry: dict[str, SubagentDefinition] | None,
    names: tuple[str, ...],
    remaining: int,
) -> str:
    entries: list[str] = []
    for name in names:
        definition = (registry or {}).get(name)
        purpose = " ".join(str(getattr(definition, "description", "") or "").split())
        purpose = purpose.split(".", 1)[0].strip() or "Read-only consultation"
        if len(purpose) > 96:
            purpose = purpose[:93].rstrip() + "..."
        entries.append(f"{name}: {purpose}")
    return "\n".join(
        (
            f"Helper subagents (remaining budget: {max(0, int(remaining))}):",
            "; ".join(entries),
            "Use subagent_run for advisory investigation, review, or verification; verify your own change.",
        )
    )


def _subagent_artifact_requirement(
    definition: SubagentDefinition,
) -> dict[str, Any] | None:
    capability_names: list[str] = []
    required_tools: list[str] = []
    success_event_types: list[str] = []
    success_tool_names: list[str] = []
    minimum_success_tool_events = 0
    materializes_artifacts = False
    for raw_name in definition.required_capabilities:
        capability = get_capability_definition(raw_name)
        if capability is None or not (
            capability.materializes_artifacts
            or capability.success_event_types
            or capability.success_tool_names
        ):
            continue
        capability_names.append(capability.name)
        required_tools.extend(capability.required_tool_names)
        success_event_types.extend(capability.success_event_types)
        success_tool_names.extend(capability.success_tool_names)
        minimum_success_tool_events = max(
            minimum_success_tool_events,
            capability.minimum_success_tool_events,
        )
        materializes_artifacts = materializes_artifacts or capability.materializes_artifacts
    if not capability_names:
        return None
    return {
        "required_capabilities": list(dict.fromkeys(capability_names)),
        "required_tools": list(dict.fromkeys(required_tools)),
        "success_event_types": list(dict.fromkeys(success_event_types)),
        "success_tool_names": list(dict.fromkeys(success_tool_names)),
        "minimum_success_tool_events": minimum_success_tool_events,
        "materializes_artifacts": materializes_artifacts,
    }


def _subagent_success_event_types(sub_session: Any) -> set[str]:
    store = getattr(sub_session, "store", None)
    snapshot = getattr(store, "events_snapshot", None)
    if not callable(snapshot):
        return set()
    try:
        events = snapshot()
    except Exception:  # noqa: BLE001 - absent evidence must degrade, not crash cleanup
        return set()
    if not isinstance(events, list):
        return set()
    return {
        str(event.get("type") or "").strip()
        for event in events
        if isinstance(event, dict) and str(event.get("type") or "").strip()
    }


def _subagent_successful_tool_names(sub_session: Any) -> set[str]:
    store = getattr(sub_session, "store", None)
    snapshot = getattr(store, "events_snapshot", None)
    if not callable(snapshot):
        return set()
    try:
        events = snapshot()
    except Exception:  # noqa: BLE001 - absent evidence must degrade, not crash cleanup
        return set()
    successful: set[str] = set()
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or str(event.get("type") or "") != "tool_result":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        result = payload.get("result")
        if isinstance(result, dict) and (
            result.get("error")
            or result.get("ok") is False
            or str(result.get("status") or "").strip().lower() in {"error", "failed"}
        ):
            continue
        name = str(payload.get("executed_tool_name") or payload.get("name") or "").strip()
        if name:
            successful.add(name)
    return successful


def _child_resume_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []
    answered_tool_ids: set[str] = set()

    def _finish_pending_tools() -> None:
        nonlocal pending_tool_calls, answered_tool_ids
        for tool_call in pending_tool_calls:
            tool_call_id = str(tool_call.get("id") or "").strip()
            if not tool_call_id or tool_call_id in answered_tool_ids:
                continue
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "[tool result unavailable after subagent resume]",
                }
            )
        pending_tool_calls = []
        answered_tool_ids = set()

    def _append_conversation_message(message: dict[str, Any]) -> None:
        nonlocal pending_tool_calls, answered_tool_ids
        role = str(message.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            return
        _finish_pending_tools()
        copied = copy.deepcopy(message)
        messages.append(copied)
        raw_tool_calls = copied.get("tool_calls")
        pending_tool_calls = (
            [item for item in raw_tool_calls if isinstance(item, dict)]
            if role == "assistant" and isinstance(raw_tool_calls, list)
            else []
        )
        answered_tool_ids = set()

    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event_type == "user_message":
            content = payload.get("display_content") or payload.get("content")
            if isinstance(content, str) and content.strip():
                _append_conversation_message({"role": "user", "content": content})
        elif event_type == "subagent_resume_notice":
            # Replay only this typed host context, never old system/bootstrap
            # messages. It is not an actual user request or permission event.
            notice = payload.get("message")
            if (
                isinstance(notice, dict)
                and set(notice) == {"role", "content"}
                and notice.get("role") == "user"
                and isinstance(notice.get("content"), str)
                and notice["content"].strip()
            ):
                _append_conversation_message(notice)
        elif event_type == "assistant_message":
            stored_message = payload.get("message")
            if isinstance(stored_message, dict):
                _append_conversation_message(stored_message)
            else:
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    _append_conversation_message({"role": "assistant", "content": content})
        elif event_type == "tool_result" and pending_tool_calls:
            tool_call_id = str(payload.get("tool_call_id") or "").strip()
            pending_ids = {
                str(item.get("id") or "").strip()
                for item in pending_tool_calls
                if str(item.get("id") or "").strip()
            }
            if not tool_call_id or (pending_ids and tool_call_id not in pending_ids):
                continue
            raw_content = payload.get("content")
            if not isinstance(raw_content, str):
                try:
                    raw_content = json.dumps(
                        payload.get("result"),
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    raw_content = str(payload.get("result") or "")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": raw_content,
                }
            )
            answered_tool_ids.add(tool_call_id)
    _finish_pending_tools()
    return messages


def _create_session_for_subagent(
    *,
    create_session_factory: Callable[..., Any] | None,
    **kwargs: Any,
) -> Any:
    create_session = _patchable("create_session", create_session_factory)
    if callable(create_session):
        return create_session(**kwargs)
    raise AgentRuntimeError("create_session is unavailable for subagent execution")


_SUBAGENT_CANCELLATION_TOKEN_ARG = object()
_SUBAGENT_PREASSIGNED_RUN_ID_ARG = object()
_ROUTING_MODE_CODE_ONLY = "code_only"
_EXTERNAL_RESEARCH_CAPABILITY = "external_research"
_EXTERNAL_RESEARCH_WEB_TOOLS = frozenset({"web_fetch", "web_search"})
_DEFAULT_CANCEL_JOIN_TIMEOUT_S = 1.0
ChildRunState = Literal[
    "spawned",
    "waiting",
    "queued",
    "running",
    "joined",
    "cancelled",
]
_CHILD_STATE_SUMMARY_ORDER = (
    "running",
    "queued",
    "waiting",
    "spawned",
    "joined",
    "cancelled",
)


_SUBAGENT_PARENT_RESULT_KEYS = frozenset(
    {
        # Identity and outcome.
        "run_id",
        "subagent",
        "subagent_session_id",
        "status",
        "state",
        "exit_code",
        "wait_interrupted",
        "wake_reason",
        "wake_reasons",
        "wake_run_id",
        "pending_run_ids",
        # Reports and screened continuation material.
        "result",
        "result_source",
        "final_text",
        "final_text_source",
        "final_report_problem",
        "partial_report",
        "report_safety",
        "report_artifact",
        "report_artifact_reader",
        "incomplete_reason",
        "stop_reason",
        "resume_affordance",
        "resumed_from",
        # Actionable failures and recovery choices.
        "error",
        "error_code",
        "failure_category",
        "batch_failure",
        "deferred_call_not_started",
        "offending_runs",
        "message",
        "guidance",
        "resolution",
        "requires_new_session",
        "available_subagents",
        "unavailable_reason",
        "unavailable_allowed_tools",
        "missing_required_tools",
        "smallest_sufficient_mode",
        "requested_mode",
        "resolved_mode",
        "effective_mode",
        "mode_clamped",
        "use_tool",
        # Effects, retained workspace material and evidence.
        "effects",
        "touched_repo_paths",
        "material_touched_repo_paths",
        "workspace",
        "workspace_view",
        "workspace_run_id",
        "patch",
        "patch_artifact",
        "patch_summary",
        "material_identity",
        "material_identity_sha256",
        "patch_capture_status",
        "material_patch_complete",
        "capture_reason_codes",
        "omitted_material_paths",
        "retained_ignored_paths",
        "ignored_outputs_integrated",
        "retention_note",
        "semantic_no_progress",
        "duplicate_of",
        "canonical_run_id",
        "canonical_state",
        "already_integrated",
        "candidate_worktree_retained",
        "physical_worktree_removed",
        "cleanup_pending",
        "cleanup_pending_run_ids",
        "cleanup_warning",
        "artifact_evidence",
        "capability_evidence",
        "artifact_requirement_problem",
        "observed_success_event_types",
        "missing_success_event_types",
        "observed_success_tool_names",
        # Cost-free progress facts useful to the parent.
        "elapsed_ms",
        "steps_completed",
        "steps_used",
    }
)

_SUBAGENT_WAIT_PARENT_ENVELOPE_KEYS = frozenset(
    {
        "results",
        "pending_run_ids",
        "pending_children",
        "wait_pending",
        "summary",
        "message",
        "status",
        "wait_interrupted",
        "selected_run_ids",
        "wake_reason",
        "wake_reasons",
        "wake_run_id",
        "run_id",
        "error",
        "error_code",
    }
)


def _project_single_subagent_parent_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, compact result that a parent model needs.

    Child execution and lifecycle APIs retain the complete result.  This
    projection is deliberately a protocol boundary: transport, accounting,
    sandbox and helper diagnostics stay in durable child events instead of
    being copied into the parent prompt.
    """

    return {
        key: copy.deepcopy(value)
        for key, value in result.items()
        if key in _SUBAGENT_PARENT_RESULT_KEYS
    }


def project_subagent_parent_result(
    tool_name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Project raw subagent tool output into the parent transcript protocol."""

    normalized_name = str(tool_name or "").strip().casefold()
    if normalized_name == "subagent_run":
        return _project_single_subagent_parent_result(result)
    if normalized_name != "subagent_wait":
        return result
    projected = {
        key: copy.deepcopy(value)
        for key, value in result.items()
        if key in _SUBAGENT_WAIT_PARENT_ENVELOPE_KEYS
    }
    raw_results = result.get("results")
    if isinstance(raw_results, dict):
        projected["results"] = {
            str(run_id): (
                _project_single_subagent_parent_result(child_result)
                if isinstance(child_result, dict)
                else copy.deepcopy(child_result)
            )
            for run_id, child_result in raw_results.items()
        }
    return projected


def _children_state_summary(children: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for child in children:
        state = str(child.get("state") or "unknown").strip().lower() or "unknown"
        counts[state] = counts.get(state, 0) + 1
    total = len(children)
    noun = "child" if total == 1 else "children"
    if not counts:
        return f"{total} {noun}"
    order = {state: index for index, state in enumerate(_CHILD_STATE_SUMMARY_ORDER)}
    parts = [
        f"{count} {state}"
        for state, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], order.get(item[0], len(order)), item[0]),
        )
    ]
    return f"{total} {noun}: {', '.join(parts)}"


def _invalid_tool_mode_payload(raw_mode: Any) -> dict[str, Any] | None:
    requested_mode = str(raw_mode or "").strip()
    if not requested_mode or requested_mode.lower() in SUBAGENT_MODES:
        return None
    return {
        "error": f"Invalid subagent mode: {requested_mode}",
        "error_code": "invalid_subagent_mode",
        "requested_mode": requested_mode,
        "valid_modes": list(SUBAGENT_MODES),
    }


def _isolated_readonly_error(
    *,
    definition: SubagentDefinition,
    requested_mode: str,
    resolved_mode: str,
) -> dict[str, Any]:
    return {
        "error": (
            f"Isolated subagent '{definition.name}' resolved to readonly and cannot write "
            "its worktree. Use a write-capable mode or a non-editing subagent role."
        ),
        "error_code": "isolated_workspace_requires_write_mode",
        "subagent": definition.name,
        "requested_mode": requested_mode,
        "resolved_mode": resolved_mode,
        "mode_clamped": requested_mode != resolved_mode,
    }


def _required_tool_launch_error(
    *,
    definition: SubagentDefinition,
    resolved_mode: str,
    available_tool_names: set[str] | None = None,
) -> dict[str, Any] | None:
    required_tools = required_subagent_tool_names(definition)
    if not required_tools:
        return None
    if available_tool_names is None:
        if resolved_mode != "readonly":
            return None
        available = set(built_in_subagent_tool_names(exposure="readonly"))
    else:
        available = available_tool_names
    missing = sorted(set(required_tools) - available)
    if not missing:
        return None
    smallest_sufficient_mode = smallest_sufficient_subagent_mode(definition)
    tool_label = ", ".join(missing)
    return {
        "error": (
            f"Subagent '{definition.name}' requires {tool_label}, but mode "
            f"'{resolved_mode}' does not expose it. Use at least "
            f"mode='{smallest_sufficient_mode}'."
        ),
        "error_code": "required_subagent_tool_unavailable",
        "failure_category": "tool_scope",
        "subagent": definition.name,
        "missing_required_tools": missing,
        "resolved_mode": resolved_mode,
        "smallest_sufficient_mode": smallest_sufficient_mode,
    }


@dataclass(frozen=True)
class ChildRunRecord:
    definition_name: str
    child_session_id: str | None
    state: ChildRunState
    started_monotonic: float
    deadline_snapshot: dict[str, Any]
    usage_cursor: int
    execution_started_monotonic: float | None = None
    finished_monotonic: float | None = None
    depends_on: tuple[str, ...] = ()
    resumed_from: str | None = None


class ChildRunRegistry:
    def __init__(self) -> None:
        self._records: dict[str, ChildRunRecord] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        run_id: str,
        definition_name: str,
        started_monotonic: float,
        deadline_snapshot: dict[str, Any],
        depends_on: tuple[str, ...] = (),
        resumed_from: str | None = None,
    ) -> ChildRunRecord:
        record = ChildRunRecord(
            definition_name=definition_name,
            child_session_id=None,
            state="spawned",
            started_monotonic=started_monotonic,
            deadline_snapshot=dict(deadline_snapshot),
            usage_cursor=0,
            depends_on=tuple(depends_on),
            resumed_from=resumed_from,
        )
        with self._lock:
            if run_id in self._records:
                raise ValueError(f"Child run already registered: {run_id}")
            self._records[run_id] = record
        return record

    def transition(
        self,
        run_id: str,
        *,
        state: ChildRunState,
        child_session_id: str | None = None,
    ) -> ChildRunRecord:
        allowed_transitions: dict[ChildRunState, set[ChildRunState]] = {
            "spawned": {"waiting", "queued", "running", "joined", "cancelled"},
            "waiting": {"queued", "cancelled"},
            "queued": {"running", "joined", "cancelled"},
            "running": {"joined", "cancelled"},
            "joined": set(),
            "cancelled": set(),
        }
        with self._lock:
            current = self._records[run_id]
            if state not in allowed_transitions[current.state]:
                raise ValueError(
                    f"Invalid child run transition for {run_id}: {current.state} -> {state}"
                )
            record = replace(
                current,
                state=state,
                child_session_id=(
                    child_session_id if child_session_id is not None else current.child_session_id
                ),
            )
            self._records[run_id] = record
        return record

    def update_usage_cursor(self, run_id: str, usage_cursor: int) -> ChildRunRecord:
        with self._lock:
            current = self._records[run_id]
            record = replace(current, usage_cursor=max(0, int(usage_cursor)))
            self._records[run_id] = record
        return record

    def mark_execution_started(
        self,
        run_id: str,
        *,
        execution_started_monotonic: float,
    ) -> ChildRunRecord:
        """Stamp execution start once, independently of registration or queuing."""
        with self._lock:
            current = self._records[run_id]
            if current.execution_started_monotonic is not None:
                return current
            record = replace(
                current,
                execution_started_monotonic=max(
                    current.started_monotonic,
                    float(execution_started_monotonic),
                ),
            )
            self._records[run_id] = record
        return record

    def mark_finished(
        self,
        run_id: str,
        *,
        finished_monotonic: float,
    ) -> ChildRunRecord:
        """Stamp execution completion once, independently of later collection."""
        with self._lock:
            current = self._records[run_id]
            if current.finished_monotonic is not None:
                return current
            record = replace(
                current,
                finished_monotonic=max(
                    current.execution_started_monotonic
                    if current.execution_started_monotonic is not None
                    else current.started_monotonic,
                    float(finished_monotonic),
                ),
            )
            self._records[run_id] = record
        return record

    def get(self, run_id: str) -> ChildRunRecord | None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return None
            return replace(record, deadline_snapshot=dict(record.deadline_snapshot))

    def snapshot(self) -> dict[str, ChildRunRecord]:
        with self._lock:
            return {
                run_id: replace(record, deadline_snapshot=dict(record.deadline_snapshot))
                for run_id, record in self._records.items()
            }


@dataclass
class _ChildRunTracker:
    run_id: str | None = None
    child_session_id: str | None = None
    pinned_source_run_id: str | None = None
    helper_runs_provider: Callable[[], dict[str, Any]] | None = None


@dataclass(frozen=True)
class _BackgroundSpawnPreflight:
    definition: SubagentDefinition
    resolved_mode: str
    deadline_snapshot: dict[str, Any]


def _resume_launch_args(source_args: dict[str, Any]) -> dict[str, Any]:
    """Return the launch arguments a linked background resume will validate."""
    resumed_args = dict(source_args)
    for key in ("run_id", "depends_on", "workspace_from_run", "max_steps"):
        resumed_args.pop(key, None)
    if str(source_args.get("workspace_from_run") or "").strip():
        resumed_args["workspace_view"] = "shared"
    return resumed_args


@dataclass(frozen=True)
class _ChildResumeContext:
    resumed_from: str
    history_messages: tuple[dict[str, Any], ...]
    provider_session_id: str | None = None
    workspace_run_id: str | None = None
    patch_artifact: str = ""
    read_ledger_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_events: tuple[dict[str, Any], ...] = ()
    source_session_id: str | None = None


class SubagentLauncher:
    def __init__(
        self,
        *,
        root: Path,
        surface: Surface,
        store: SessionStore,
        mode: str,
        yes: bool,
        cfg: AppConfig | None,
        api_key: str | None,
        max_steps: int | None,
        no_log: bool,
        usage_role: str,
        usage_summary: UsageSummary | None,
        deny_write_prefixes: list[str] | None,
        allow_write_globs: list[str] | None,
        persona_allow_write_globs: list[str] | None,
        non_interactive: bool,
        verification_enabled: bool,
        authoritative_verification_commands: list[str] | None,
        skills_enabled: bool,
        subagents_enabled: bool,
        subagent_depth: int,
        subagent_registry: dict[str, SubagentDefinition] | None,
        session_log_dir_override: Path | None,
        step_budget_runtime: Any | None,
        get_active_workdir_relpath: Callable[[], str] | None,
        create_session_factory: Callable[..., Any] | None,
        execution_deadline: ExecutionDeadline | None,
        crash_diagnostic_log_path: str | os.PathLike[str] | None,
        crash_diagnostics: CrashDiagnosticLogger | None,
        tools: list[Any],
        command_mutation_metadata: Callable[..., dict[str, Any]],
        prompt_cache_parent_session_id: str | None = None,
        workspace_provider: SubagentWorkspaceProvider | None = None,
        child_run_registry: ChildRunRegistry | None = None,
        helpers_enabled_for_children: bool = False,
        helper_only: bool = False,
        helper_allowed_names: tuple[str, ...] = (),
        managed_browser_service: Any | None = None,
        managed_browser_owner_id: str | None = None,
        managed_browser_cancel_check: Callable[[], bool] | None = None,
        approval_reviewer: Any | None = None,
    ) -> None:
        self.root = root
        self.surface = surface
        self.store = store
        self.mode = mode
        self.yes = yes
        self.cfg = cfg
        self.api_key = api_key
        self.max_steps = max_steps
        self.no_log = no_log
        self.usage_role = usage_role
        self.usage_summary = usage_summary
        self.deny_write_prefixes = deny_write_prefixes
        self.allow_write_globs = allow_write_globs
        self.persona_allow_write_globs = persona_allow_write_globs
        self.non_interactive = non_interactive
        self.verification_enabled = verification_enabled
        self.authoritative_verification_commands = authoritative_verification_commands
        self.skills_enabled = bool(skills_enabled)
        self.subagents_enabled = subagents_enabled
        self.subagent_depth = subagent_depth
        self.subagent_registry = subagent_registry
        self.session_log_dir_override = session_log_dir_override
        self.step_budget_runtime = step_budget_runtime
        self.get_active_workdir_relpath = get_active_workdir_relpath
        self.create_session_factory = create_session_factory
        self.execution_deadline = execution_deadline
        self.crash_diagnostic_log_path = crash_diagnostic_log_path
        self.crash_diagnostics = crash_diagnostics
        self.tools = tools
        self.command_mutation_metadata = command_mutation_metadata
        self.prompt_cache_parent_session_id = (
            str(prompt_cache_parent_session_id or "").strip()
            or str(getattr(store, "session_id", "") or "").strip()
            or None
        )
        self.workspace_provider = workspace_provider
        self.parent_mode_normalized = normalize_subagent_mode(mode)
        self.child_run_registry = child_run_registry or ChildRunRegistry()
        self.approval_lock = RLock()
        self.helpers_enabled_for_children = helpers_enabled_for_children
        self.helper_only = helper_only
        self.helper_allowed_names = tuple(helper_allowed_names)
        self.managed_browser_service = managed_browser_service
        self.managed_browser_owner_id = managed_browser_owner_id
        self.managed_browser_cancel_check = managed_browser_cancel_check
        # Children share the parent's approval reviewer: the same user messages decide, and
        # denials count against the same limits.
        self.approval_reviewer = approval_reviewer
        self._helper_run_lock = RLock()
        self._helper_runs_count = 0
        self._helper_run_names: list[str] = []
        self._helper_run_steps = 0
        self._helper_usage_totals: dict[str, int | float] = {}
        self.subagent_coordinator: SubagentCoordinator | None = None
        self.child_scheduler: ChildScheduler | None = None

    def helper_runs_summary(self) -> dict[str, Any]:
        with self._helper_run_lock:
            return {
                "count": self._helper_runs_count,
                "names": list(self._helper_run_names),
                "steps": self._helper_run_steps,
                "usage_totals": dict(self._helper_usage_totals),
            }

    def _record_child_run_state(self, *, run_id: str, record: ChildRunRecord) -> None:
        self.store.append(
            "subagent_state",
            {
                "run_id": run_id,
                "name": record.definition_name,
                "state": record.state,
                "subagent_session_id": record.child_session_id,
                "resumed_from": record.resumed_from,
            },
        )

    def _transition_child_run(
        self,
        *,
        run_id: str,
        state: ChildRunState,
        child_session_id: str | None = None,
    ) -> ChildRunRecord:
        if state == "running":
            self.child_run_registry.mark_execution_started(
                run_id,
                execution_started_monotonic=perf_counter(),
            )
        record = self.child_run_registry.transition(
            run_id,
            state=state,
            child_session_id=child_session_id,
        )
        self._record_child_run_state(run_id=run_id, record=record)
        return record

    def _clamp_subagent_mode(self, requested_mode: str) -> str:
        return clamp_subagent_mode(
            requested_mode=requested_mode,
            parent_mode=self.parent_mode_normalized,
        )

    def _replay_subagent_usage(self, *, sub_session: Any, usage_cursor: int = 0) -> int:
        child_usage_summary = getattr(sub_session, "usage_summary", None)
        records_fn = getattr(child_usage_summary, "records", None)
        if not callable(records_fn):
            return max(0, int(usage_cursor))
        raw_records = records_fn()
        records = [record for record in raw_records if isinstance(record, UsageRecord)]
        cursor = min(max(0, int(usage_cursor)), len(records))
        new_records = records[cursor:]
        if not new_records:
            return len(records)
        if self.usage_summary is not None:
            self.usage_summary.merge_records(new_records)
        for record in new_records:
            self.store.append("llm_usage", record.to_payload())
        return len(records)

    def replay_registered_usage(
        self,
        *,
        run_id: str,
        sub_session: Any,
        usage_lock: RLock | None = None,
    ) -> int:
        context = usage_lock if usage_lock is not None else nullcontext()
        with context:
            record = self.child_run_registry.get(run_id)
            usage_cursor = record.usage_cursor if record is not None else 0
            next_cursor = self._replay_subagent_usage(
                sub_session=sub_session,
                usage_cursor=usage_cursor,
            )
            if record is not None:
                self.child_run_registry.update_usage_cursor(run_id, next_cursor)
            return next_cursor

    def _resolve_subagent_definition(self, raw_name: str) -> SubagentDefinition | None:
        if not self.subagent_registry:
            return None
        normalized = canonical_subagent_name(raw_name)
        if normalized is None:
            return None
        return self.subagent_registry.get(normalized)

    @staticmethod
    def _resolve_subagent_model(
        definition: SubagentDefinition,
        cfg_copy: AppConfig,
    ) -> tuple[str, str]:
        role = resolve_subagent_model_role(definition.model_role)
        if definition.model:
            selected_model = str(definition.model).strip()
            temperature_role = role or ROLE_CODING
            return selected_model, temperature_role
        if role:
            selected_model = resolve_model_for_role(
                cfg=cfg_copy,
                role=role,
                plan=None,
            )
            return selected_model, role
        selected_model = resolve_model_for_role(
            cfg=cfg_copy,
            role=ROLE_CODING,
            plan=None,
        )
        return selected_model, ROLE_CODING

    @staticmethod
    def _resolve_subagent_config(
        definition: SubagentDefinition,
        cfg: AppConfig,
        *,
        api_key: str | None = None,
    ) -> tuple[AppConfig, str | None, str]:
        """Select an explicit saved profile without changing parent configuration.

        A model ID never chooses a provider. Cross-profile static credentials
        must belong to the selected profile; global/legacy fallback is unsafe.
        This is configuration/credential resolution only, with no model calls.
        """
        from ..config import _is_profile_scoped_api_key_source, resolve_api_key
        from ..profiles import get_active_profile, get_profile, set_active_profile

        child_cfg = cfg.model_copy(deep=True)
        child_api_key = str(api_key or "").strip() or None
        if definition.profile:
            parent_profile = get_active_profile(child_cfg)
            child_profile = get_profile(child_cfg, definition.profile)
            if child_profile is None:
                raise ConfigError(f"Profile not found: {definition.profile}")
            if child_profile.name != parent_profile.name:
                set_active_profile(child_cfg, child_profile.name)
                if not child_profile.base_url:
                    raise ConfigError(
                        f"Subagent profile '{child_profile.name}' needs an explicit base URL; "
                        "the parent endpoint is not inherited across profiles."
                    )
                # A foreign profile with unset defaults must not borrow the
                # parent's provider-specific model or reasoning configuration.
                if not child_profile.default_model:
                    child_cfg.model = ""
                if child_profile.reasoning_effort is None:
                    child_cfg.llm_reasoning_effort = None
                    child_cfg.llm_enable_thinking = None
                child_api_key = None
                if not child_profile.auth_provider:
                    resolved = resolve_api_key(child_cfg)
                    if not resolved.key or not _is_profile_scoped_api_key_source(resolved.source):
                        raise ConfigError(
                            f"Subagent profile '{child_profile.name}' needs its own stored key "
                            "or profile-scoped API-key environment variable; parent and legacy "
                            "credentials are not reused across profiles."
                        )
                    child_api_key = resolved.key
        active_child_profile = get_active_profile(child_cfg)
        if not child_api_key and not active_child_profile.auth_provider:
            raise ConfigError(
                f"Subagent profile '{active_child_profile.name}' needs an API key "
                "or provider authentication."
            )
        selected_model, temperature_role = SubagentLauncher._resolve_subagent_model(
            definition, child_cfg
        )
        child_cfg.model = selected_model
        return child_cfg, child_api_key, temperature_role

    def background_spawn_preflight(
        self,
        args: dict[str, Any],
        *,
        defer_dependency_workspace: bool = True,
        check_deadline: bool = True,
    ) -> tuple[_BackgroundSpawnPreflight | None, dict[str, Any] | None]:
        cfg = self.cfg
        if not self.subagents_enabled:
            return None, {"error": "Subagents are disabled for this session."}
        if self.subagent_depth > 0:
            return None, {"error": "Subagents cannot invoke subagents (nesting is blocked)."}
        if cfg is None:
            return None, {"error": "Subagent execution unavailable: missing session configuration."}

        raw_name = str(args.get("name", "")).strip()
        task = str(args.get("task", "")).strip()
        if not raw_name:
            return None, {"error": "Missing required argument: name"}
        if not task:
            return None, {"error": "Missing required argument: task"}

        capability_unavailability = subagent_unavailability(
            raw_name,
            registry=self.subagent_registry,
            cfg=cfg,
            available_tool_names={tool.name for tool in self.tools},
        )
        if capability_unavailability is not None:
            available = available_subagent_names(
                registry=self.subagent_registry,
                cfg=cfg,
                available_tool_names={tool.name for tool in self.tools},
            )
            return None, {
                "error": f"Subagent unavailable: {capability_unavailability.name}",
                "error_code": (
                    "subagent_disabled"
                    if capability_unavailability.reason_code == "subagent_disabled"
                    else "subagent_capability_unavailable"
                ),
                "unavailable_reason": capability_unavailability.reason,
                "resolution": capability_unavailability.resolution,
                "requires_new_session": capability_unavailability.requires_new_session,
                "available_subagents": available,
            }
        definition = self._resolve_subagent_definition(raw_name)
        if definition is None:
            available = available_subagent_names(
                registry=self.subagent_registry,
                cfg=cfg,
                available_tool_names={tool.name for tool in self.tools},
            )
            return None, {
                "error": f"Unknown subagent: {raw_name}",
                "error_code": "unknown_subagent",
                "available_subagents": available,
            }

        try:
            self._resolve_subagent_config(definition, cfg, api_key=self.api_key)
        except ConfigError as exc:
            return None, {
                "error": f"Subagent profile/model resolution failed: {exc}",
                "error_code": "subagent_profile_resolution_failed",
            }

        mode_override = str(args.get("mode", "") or "").strip()
        invalid_mode = _invalid_tool_mode_payload(mode_override)
        if invalid_mode is not None:
            return None, invalid_mode
        requested_mode = normalize_subagent_mode(definition.mode)
        if mode_override:
            requested_mode = normalize_subagent_mode(mode_override)
        resolved_mode = self._clamp_subagent_mode(requested_mode)
        required_tool_error = _required_tool_launch_error(
            definition=definition,
            resolved_mode=resolved_mode,
        )
        if required_tool_error is not None:
            return None, required_tool_error
        workspace_view = str(args.get("workspace_view") or "shared").strip().lower()
        if workspace_view not in {"shared", "isolated"}:
            return None, {
                "error": "workspace_view must be one of: shared, isolated",
                "error_code": "invalid_workspace_view",
            }
        if (
            workspace_view == "isolated"
            and resolved_mode == "readonly"
            and definition.allow_workspace_writes is not False
        ):
            return None, _isolated_readonly_error(
                definition=definition,
                requested_mode=requested_mode,
                resolved_mode=resolved_mode,
            )
        if workspace_view == "isolated" and not bool(
            getattr(
                getattr(cfg, "subagent_orchestration", None),
                "workspace_isolation_enabled",
                True,
            )
        ):
            return None, {
                "error": "Isolated subagent workspaces are disabled for this session.",
                "error_code": "workspace_isolation_disabled",
                "subagent": definition.name,
            }
        if workspace_view == "isolated" and self.workspace_provider is None:
            return None, {
                "error": "Isolated subagent workspace storage is unavailable.",
                "error_code": "workspace_isolation_unavailable",
                "subagent": definition.name,
            }
        workspace_from_run = str(args.get("workspace_from_run") or "").strip()
        raw_dependencies = args.get("depends_on")
        dependency_ids = {
            str(candidate).strip()
            for candidate in (raw_dependencies if isinstance(raw_dependencies, list) else [])
            if str(candidate).strip()
        }
        if workspace_from_run and workspace_view == "isolated":
            return None, {
                "error": "workspace_from_run cannot be combined with workspace_view=isolated",
                "error_code": "conflicting_workspace_views",
            }
        if workspace_from_run and definition.allow_workspace_writes:
            return None, {
                "error": (
                    "workspace_from_run requires a subagent role that disallows workspace writes."
                ),
                "error_code": "workspace_from_run_requires_readonly_role",
                "subagent": definition.name,
            }
        if workspace_from_run and not (
            defer_dependency_workspace and workspace_from_run in dependency_ids
        ):
            if self.workspace_provider is None:
                return None, {
                    "error": "Pinned subagent workspace storage is unavailable.",
                    "error_code": "workspace_isolation_unavailable",
                }
            inspected = self.workspace_provider.inspect_pin_source(workspace_from_run)
            if not bool(inspected.get("ok")):
                return None, inspected
        if not workspace_from_run and workspace_view == "shared" and resolved_mode != "readonly":
            return None, {
                "error": (
                    "Shared-view background subagents must resolve to readonly; this "
                    f"invocation resolves to '{resolved_mode}'. Use workspace_view=isolated "
                    "for background write-capable work. Use subagent_run synchronously otherwise."
                ),
                "error_code": "background_subagent_requires_readonly",
                "subagent": definition.name,
                "resolved_mode": resolved_mode,
                "use_tool": "subagent_run",
            }

        child_deadline = derive_subagent_deadline(
            self.execution_deadline,
            cfg.subagent_timeout_s,
        )
        if not check_deadline:
            return (
                _BackgroundSpawnPreflight(
                    definition=definition,
                    resolved_mode=resolved_mode,
                    deadline_snapshot={},
                ),
                None,
            )
        deadline_decision = child_deadline.start_decision(
            DeadlineOperation.SUBAGENT,
            minimum_remaining_seconds=MINIMUM_SUBAGENT_START_SECONDS,
        ).telemetry_snapshot()
        if not bool(deadline_decision.get("allowed")):
            return None, {
                "error": (
                    "Subagent launch skipped because the run deadline has too little "
                    "remaining time."
                ),
                "error_code": "subagent_deadline_prevented_launch",
                "failure_category": "deadline",
                "deadline_exhausted": child_deadline.is_exhausted(),
                "deadline_prevented_launch": True,
                "remaining_seconds": child_deadline.remaining_seconds(),
                "deadline_start_decision": deadline_decision,
                "subagent_timeout_s": cfg.subagent_timeout_s,
                "resolved_timeout_s": child_deadline.remaining_seconds(),
                "resolved_deadline_source": str(
                    child_deadline.telemetry_snapshot().get("source") or "unknown"
                ),
                "deadline": child_deadline.telemetry_snapshot(),
                "subagent": definition.name,
                "subagent_session_id": None,
                "steps_completed": 0,
                "elapsed_ms": 0,
            }
        return (
            _BackgroundSpawnPreflight(
                definition=definition,
                resolved_mode=resolved_mode,
                deadline_snapshot=child_deadline.telemetry_snapshot(),
            ),
            None,
        )

    def _incomplete_resume_affordance(
        self,
        *,
        args: dict[str, Any],
        definition: SubagentDefinition,
        run_id: str,
        preserved_state: str,
        report_artifact: str,
    ) -> str:
        resumed_args = _resume_launch_args(args)
        plain_preflight, plain_error = self.background_spawn_preflight(
            resumed_args,
            check_deadline=False,
        )
        if plain_preflight is not None:
            return (
                "This run can be continued with "
                f"subagent_resume(run_id={run_id}) preserving its {preserved_state}."
            )

        isolated_args = {**resumed_args, "workspace_view": "isolated"}
        isolated_preflight, _isolated_error = self.background_spawn_preflight(
            isolated_args,
            check_deadline=False,
        )
        if isolated_preflight is not None:
            return (
                "This run can be continued with "
                f'subagent_resume(run_id={run_id}, workspace_view="isolated") '
                "preserving its transcript in a fresh isolated workspace."
            )

        reason = str((plain_error or {}).get("error") or "runtime launch constraints")
        artifact_direction = (
            f" Continue from retained partial report {report_artifact}."
            if report_artifact
            else " Continue from the screened partial report in this result."
        )
        return (
            f"Resume is not available for this run: {reason} "
            "Start a fresh synchronous "
            f'subagent_run(name="{definition.name}", task="Continue the retained '
            f'partial work.").{artifact_direction}'
        )

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.helper_only:
            with self._helper_run_lock:
                canonical_name = canonical_subagent_name(str(args.get("name") or ""))
                task = str(args.get("task") or "").strip()
                if canonical_name in self.helper_allowed_names and task:
                    maximum = max(
                        0,
                        int(
                            getattr(
                                getattr(self.cfg, "subagent_orchestration", None),
                                "helper_max_total_per_child",
                                2,
                            )
                        ),
                    )
                    if self._helper_runs_count >= maximum:
                        return {
                            "error": (
                                "helper budget exhausted: "
                                f"{self._helper_runs_count} of {maximum} used"
                            ),
                            "error_code": "helper_budget_exhausted",
                            "helper_runs": self.helper_runs_summary(),
                        }
                    self._helper_runs_count += 1
                    self._helper_run_names.append(canonical_name)
                result = self._run(args)
                if canonical_name in self.helper_allowed_names and task:
                    self._helper_run_steps += max(
                        0,
                        int(result.get("steps_completed") or 0),
                    )
                    usage = result.get("usage")
                    if isinstance(usage, dict):
                        for key, value in usage.items():
                            if isinstance(value, bool) or not isinstance(value, (int, float)):
                                continue
                            self._helper_usage_totals[key] = (
                                self._helper_usage_totals.get(key, 0) + value
                            )
                return result
        coordinator = self.subagent_coordinator
        if coordinator is not None:
            return coordinator.run_foreground(args, launcher=self)
        return self._run(args)

    def _run(self, args: dict[str, Any]) -> dict[str, Any]:
        tracker = _ChildRunTracker()
        completed_result: dict[str, Any] | None = None
        execution_error: BaseException | None = None
        preassigned_run_id = str(args.get(_SUBAGENT_PREASSIGNED_RUN_ID_ARG) or "").strip()
        parent_cancellation_token = args.get(_SUBAGENT_CANCELLATION_TOKEN_ARG)
        child_cancellation_token = (
            parent_cancellation_token
            if isinstance(parent_cancellation_token, _ChildCancellationToken)
            else (
                _ChildCancellationToken(parent=parent_cancellation_token)
                if parent_cancellation_token is not None
                else None
            )
        )
        public_args = {
            key: value for key, value in args.items() if key is not _SUBAGENT_PREASSIGNED_RUN_ID_ARG
        }
        if child_cancellation_token is not None:
            public_args[_SUBAGENT_CANCELLATION_TOKEN_ARG] = child_cancellation_token
        try:
            result = self._run_sync(
                public_args,
                child_run_tracker=tracker,
                preassigned_run_id=preassigned_run_id or None,
            )
            result = self._annotate_helper_runs_result(tracker=tracker, result=result)
            result = self._capture_isolated_workspace_result(
                args=public_args,
                tracker=tracker,
                result=result,
            )
            completed_result = self._annotate_pinned_workspace_result(
                args=public_args,
                tracker=tracker,
                result=result,
            )
            return completed_result
        except BaseException as exc:
            execution_error = exc
            raise
        finally:
            self._release_pinned_workspace(tracker)
            if tracker.run_id is not None:
                try:
                    self._transition_child_run(
                        run_id=tracker.run_id,
                        state="joined",
                        child_session_id=tracker.child_session_id,
                    )
                finally:
                    if self.child_scheduler is not None:
                        self.child_scheduler.complete_synchronous_child(
                            run_id=tracker.run_id,
                            result=completed_result,
                            error=execution_error,
                        )

    def run_registered(
        self,
        args: dict[str, Any],
        *,
        run_id: str,
        cancellation_token: Any,
        usage_lock: RLock,
        parent_message_provider: Callable[[], list[str]] | None,
        parent_message_delivery_observer: Callable[[int], None] | None,
        on_session_started: Callable[[Any, NestedSubagentSurface], None],
        on_terminal_event: Callable[[], None] | None = None,
        resume_context: _ChildResumeContext | None = None,
    ) -> dict[str, Any]:
        tracker = _ChildRunTracker(run_id=run_id)
        dispatch_args: dict[Any, Any] = dict(args)
        dispatch_args[_SUBAGENT_CANCELLATION_TOKEN_ARG] = cancellation_token
        try:
            result = self._run_sync(
                dispatch_args,
                child_run_tracker=tracker,
                registered_run_id=run_id,
                usage_replay_lock=usage_lock,
                parent_message_provider=parent_message_provider,
                parent_message_delivery_observer=parent_message_delivery_observer,
                on_session_started=on_session_started,
                on_terminal_event=on_terminal_event,
                resume_context=resume_context,
            )
            result = self._annotate_helper_runs_result(tracker=tracker, result=result)
            result = self._capture_isolated_workspace_result(
                args=args,
                tracker=tracker,
                result=result,
                workspace_run_id=(
                    resume_context.workspace_run_id if resume_context is not None else None
                ),
            )
            result = self._annotate_pinned_workspace_result(
                args=args,
                tracker=tracker,
                result=result,
            )
            if resume_context is not None:
                result = {**result, "resumed_from": resume_context.resumed_from}
            return result
        finally:
            self._release_pinned_workspace(tracker)

    def _release_pinned_workspace(self, tracker: _ChildRunTracker) -> None:
        if tracker.pinned_source_run_id and tracker.run_id and self.workspace_provider is not None:
            self.workspace_provider.release_pin(
                tracker.pinned_source_run_id,
                consumer_run_id=tracker.run_id,
            )

    @staticmethod
    def _annotate_helper_runs_result(
        *,
        tracker: _ChildRunTracker,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if tracker.helper_runs_provider is None:
            return result
        return {**result, "helper_runs": tracker.helper_runs_provider()}

    @staticmethod
    def _annotate_pinned_workspace_result(
        *,
        args: dict[str, Any],
        tracker: _ChildRunTracker,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        source_run_id = str(args.get("workspace_from_run") or "").strip()
        if not source_run_id or tracker.pinned_source_run_id is None:
            return result
        return {
            **result,
            "workspace": {
                "view": "pinned",
                "source_run_id": source_run_id,
            },
        }

    def _capture_isolated_workspace_result(
        self,
        *,
        args: dict[str, Any],
        tracker: _ChildRunTracker,
        result: dict[str, Any],
        workspace_run_id: str | None = None,
    ) -> dict[str, Any]:
        if str(args.get("workspace_view") or "shared").strip().lower() != "isolated":
            return result
        existing_workspace = result.get("workspace")
        if (
            isinstance(existing_workspace, dict)
            and existing_workspace.get("view") == "isolated"
            and "no_changes" in existing_workspace
        ):
            return result
        run_id = tracker.run_id
        capture_run_id = workspace_run_id or run_id
        provider = self.workspace_provider
        if not run_id or not capture_run_id or provider is None:
            return result
        record = provider.get(capture_run_id)
        if record is None:
            return result
        captured = provider.capture(capture_run_id)
        enriched = dict(result)
        enriched["run_id"] = run_id
        enriched["workspace"] = {
            "view": "isolated",
            "base_commit": record.base_commit,
            "parent_head_commit": record.parent_head_commit,
            "parent_dirty_paths": list(record.parent_dirty_paths),
            **({"resumed_workspace_from": capture_run_id} if capture_run_id != run_id else {}),
        }
        if not bool(captured.get("ok")):
            if "error" not in enriched:
                enriched.update(
                    {
                        "error": str(captured.get("error") or "Workspace capture failed."),
                        "status": "degraded",
                        "failure_category": "workspace_capture",
                        "error_code": str(captured.get("error_code") or "workspace_capture_failed"),
                    }
                )
            return enriched
        no_changes = bool(captured.get("no_changes"))
        enriched["workspace"]["no_changes"] = no_changes
        for key in (
            "material_identity",
            "patch_capture_status",
            "material_patch_complete",
            "capture_reason_codes",
            "omitted_material_paths",
            "retained_ignored_paths",
            "ignored_outputs_integrated",
            "retention_note",
            "material_identity_sha256",
            "semantic_no_progress",
            "duplicate_of",
            "canonical_run_id",
            "canonical_state",
            "already_integrated",
            "candidate_worktree_retained",
            "physical_worktree_removed",
            "cleanup_pending",
            "cleanup_pending_run_ids",
            "cleanup_warning",
        ):
            if key not in captured:
                continue
            value = copy.deepcopy(captured[key])
            enriched[key] = value
            enriched["workspace"][key] = value
        if captured.get("material_patch_complete") is False and "error" not in enriched:
            enriched.update(
                {
                    "status": "degraded",
                    "error": "The isolated workspace contains changes without a complete patch; inspect the retained candidate before integration.",
                    "failure_category": "workspace_capture",
                    "error_code": "incomplete_workspace_patch",
                }
            )
        enriched["patch_summary"] = {
            "files": list(captured.get("paths") or []),
            "insertions": int(captured.get("insertions") or 0),
            "deletions": int(captured.get("deletions") or 0),
            "patch_artifact": str(captured.get("patch_artifact") or ""),
            "sha256": str(captured.get("sha256") or ""),
            **{
                key: copy.deepcopy(captured[key])
                for key in (
                    "retained_ignored_paths",
                    "ignored_outputs_integrated",
                    "retention_note",
                )
                if key in captured
            },
            **(
                {"material_identity_sha256": captured["material_identity_sha256"]}
                if captured.get("material_identity_sha256")
                else {}
            ),
        }
        definition = self._resolve_subagent_definition(str(args.get("name") or ""))
        if (
            no_changes
            and definition is not None
            and definition.allow_workspace_writes is not False
            and "error" not in enriched
        ):
            # The workspace is authoritative. No model-authored phrase is
            # required to make a no-delta result safe and machine-readable.
            enriched["status"] = "no_changes"
        return enriched

    def _run_sync(
        self,
        args: dict[str, Any],
        *,
        child_run_tracker: _ChildRunTracker,
        registered_run_id: str | None = None,
        preassigned_run_id: str | None = None,
        usage_replay_lock: RLock | None = None,
        parent_message_provider: Callable[[], list[str]] | None = None,
        parent_message_delivery_observer: Callable[[int], None] | None = None,
        on_session_started: Callable[[Any, NestedSubagentSurface], None] | None = None,
        on_terminal_event: Callable[[], None] | None = None,
        resume_context: _ChildResumeContext | None = None,
    ) -> dict[str, Any]:
        allow_write_globs = self.allow_write_globs
        api_key = self.api_key
        authoritative_verification_commands = self.authoritative_verification_commands
        cfg = self.cfg
        crash_diagnostic_log_path = self.crash_diagnostic_log_path
        crash_diagnostics = self.crash_diagnostics
        create_session_factory = self.create_session_factory
        deny_write_prefixes = self.deny_write_prefixes
        execution_deadline = self.execution_deadline
        get_active_workdir_relpath = self.get_active_workdir_relpath
        max_steps = self.max_steps
        no_log = self.no_log
        non_interactive = self.non_interactive
        persona_allow_write_globs = self.persona_allow_write_globs
        root = self.root
        session_log_dir_override = self.session_log_dir_override
        step_budget_runtime = self.step_budget_runtime
        store = self.store
        subagent_depth = self.subagent_depth
        subagent_registry = self.subagent_registry
        subagents_enabled = self.subagents_enabled
        surface = self.surface
        tools = self.tools
        usage_role = self.usage_role
        verification_enabled = self.verification_enabled
        yes = self.yes
        managed_browser_service = self.managed_browser_service
        managed_browser_owner_id = self.managed_browser_owner_id
        managed_browser_cancel_check = self.managed_browser_cancel_check
        child_route_metadata: dict[str, Any] = {}

        def _helper_runs_fields() -> dict[str, Any]:
            provider = child_run_tracker.helper_runs_provider
            if provider is None:
                return {}
            return {"helper_runs": provider()}

        def _record_subagent_end(payload: dict[str, Any]) -> None:
            store.append("subagent_end", {**child_route_metadata, **payload})
            if on_terminal_event is not None:
                on_terminal_event()

        if self.helper_only:
            unexpected = sorted(
                key
                for key in args
                if isinstance(key, str) and key not in {"name", "task", "max_steps"}
            )
            if unexpected:
                return {
                    "error": ("Helper subagent_run accepts only name, task, and max_steps."),
                    "error_code": "invalid_helper_arguments",
                    "unexpected_arguments": unexpected,
                }
        workspace_view = str(args.get("workspace_view") or "shared").strip().lower()
        if workspace_view not in {"shared", "isolated"}:
            return {
                "error": "workspace_view must be one of: shared, isolated",
                "error_code": "invalid_workspace_view",
            }
        workspace_from_run = str(args.get("workspace_from_run") or "").strip()
        if workspace_from_run and workspace_view == "isolated":
            return {
                "error": "workspace_from_run cannot be combined with workspace_view=isolated",
                "error_code": "conflicting_workspace_views",
            }
        cancellation_token = args.get(_SUBAGENT_CANCELLATION_TOKEN_ARG)
        if cancellation_token is not None:
            throw_if_cancelled = getattr(cancellation_token, "throw_if_cancelled", None)
            if callable(throw_if_cancelled):
                throw_if_cancelled("cancelled_by_user")
            elif bool(getattr(cancellation_token, "is_cancelled", False)):
                raise RuntimeError("cancelled_by_user")
        if not subagents_enabled:
            return {"error": "Subagents are disabled for this session."}
        if subagent_depth > 0 and not (self.helper_only and subagent_depth == 1):
            return {"error": "Subagents cannot invoke subagents (nesting is blocked)."}
        if cfg is None:
            return {"error": "Subagent execution unavailable: missing session configuration."}

        raw_name = str(args.get("name", "")).strip()
        task = str(args.get("task", "")).strip()
        if not raw_name:
            return {"error": "Missing required argument: name"}
        if not task:
            return {"error": "Missing required argument: task"}

        canonical_name = canonical_subagent_name(raw_name)
        if self.helper_only and canonical_name not in self.helper_allowed_names:
            return {
                "error": f"Unknown helper subagent: {raw_name}",
                "error_code": "unknown_helper_subagent",
                "available_subagents": list(self.helper_allowed_names),
            }

        definition = self._resolve_subagent_definition(raw_name)
        capability_unavailability = subagent_unavailability(
            raw_name,
            registry=subagent_registry,
            cfg=cfg,
            available_tool_names={tool.name for tool in tools},
        )
        if capability_unavailability is not None:
            available = available_subagent_names(
                registry=subagent_registry,
                cfg=cfg,
                available_tool_names={tool.name for tool in tools},
            )
            return {
                "error": f"Subagent unavailable: {capability_unavailability.name}",
                "error_code": (
                    "subagent_disabled"
                    if capability_unavailability.reason_code == "subagent_disabled"
                    else "subagent_capability_unavailable"
                ),
                "unavailable_reason": capability_unavailability.reason,
                "resolution": capability_unavailability.resolution,
                "requires_new_session": capability_unavailability.requires_new_session,
                "available_subagents": available,
            }
        if definition is None:
            available = available_subagent_names(
                registry=subagent_registry,
                cfg=cfg,
                available_tool_names={tool.name for tool in tools},
            )
            return {
                "error": f"Unknown subagent: {raw_name}",
                "error_code": "unknown_subagent",
                "available_subagents": available,
            }
        mode_override = "" if self.helper_only else str(args.get("mode", "") or "").strip()
        invalid_mode = _invalid_tool_mode_payload(mode_override)
        if invalid_mode is not None:
            return invalid_mode
        requested_mode = normalize_subagent_mode(definition.mode)
        if mode_override:
            requested_mode = normalize_subagent_mode(mode_override)
        resolved_mode = self._clamp_subagent_mode(requested_mode)
        required_tool_error = _required_tool_launch_error(
            definition=definition,
            resolved_mode=resolved_mode,
        )
        if required_tool_error is not None:
            return required_tool_error
        if (
            workspace_view == "isolated"
            and resolved_mode == "readonly"
            and definition.allow_workspace_writes is not False
        ):
            return _isolated_readonly_error(
                definition=definition,
                requested_mode=requested_mode,
                resolved_mode=resolved_mode,
            )

        def _sandbox_payload(tool_names: Any) -> dict[str, Any]:
            return {
                "requested_mode": requested_mode,
                "mode": resolved_mode,
                "mode_clamped": requested_mode != resolved_mode,
                "tools": list(tool_names),
            }

        configured_subagent_timeout_s = (
            cfg.subagent_orchestration.helper_timeout_s
            if self.helper_only
            else cfg.subagent_timeout_s
        )
        child_execution_deadline = derive_subagent_deadline(
            execution_deadline,
            configured_subagent_timeout_s,
        )
        parent_call_estimate = (
            execution_deadline.robust_llm_estimate_snapshot()
            if self.helper_only and execution_deadline is not None
            else None
        )
        parent_call_estimate_seconds = (
            float(parent_call_estimate["estimated_seconds"])
            if parent_call_estimate is not None
            else 0.0
        )
        nested_minimum_remaining_seconds = max(
            MINIMUM_SUBAGENT_START_SECONDS,
            parent_call_estimate_seconds * 2.0,
        )
        resolved_timeout_s = child_execution_deadline.remaining_seconds()
        resolved_deadline_source = str(
            child_execution_deadline.telemetry_snapshot().get("source") or "unknown"
        )

        def _child_deadline_telemetry_fields() -> dict[str, Any]:
            return {
                "subagent_timeout_s": configured_subagent_timeout_s,
                "resolved_timeout_s": resolved_timeout_s,
                "resolved_deadline_source": resolved_deadline_source,
                "deadline": child_execution_deadline.telemetry_snapshot(),
                **(
                    {
                        "parent_call_estimate_seconds": parent_call_estimate_seconds,
                        "nested_minimum_remaining_seconds": (nested_minimum_remaining_seconds),
                        "parent_call_estimate": parent_call_estimate,
                    }
                    if self.helper_only
                    else {}
                ),
            }

        deadline_decision = child_execution_deadline.start_decision(
            DeadlineOperation.SUBAGENT,
            minimum_remaining_seconds=nested_minimum_remaining_seconds,
        ).telemetry_snapshot()
        if not bool(deadline_decision.get("allowed")):
            error_message = (
                "Subagent launch skipped because the run deadline has too little remaining time."
            )
            if self.helper_only and parent_call_estimate_seconds > 0:
                error_message = (
                    "Nested helper launch skipped: the parent child's robust model-call "
                    f"estimate is {parent_call_estimate_seconds:.3f}s, so two calls need "
                    f"{nested_minimum_remaining_seconds:.3f}s; only "
                    f"{child_execution_deadline.remaining_seconds():.3f}s remain."
                )
            payload = {
                "error": error_message,
                "failure_category": "deadline",
                "error_code": "subagent_deadline_prevented_launch",
                "deadline_exhausted": child_execution_deadline.is_exhausted(),
                "deadline_prevented_launch": True,
                "remaining_seconds": child_execution_deadline.remaining_seconds(),
                "deadline_start_decision": deadline_decision,
                **_child_deadline_telemetry_fields(),
            }
            payload.update(
                {
                    "subagent": definition.name,
                    "subagent_session_id": None,
                    "steps_completed": 0,
                    "elapsed_ms": 0,
                }
            )
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "deadline_exhausted",
                    {
                        "operation": DeadlineOperation.SUBAGENT.value,
                        "deadline_exhausted": payload["deadline_exhausted"],
                        "remaining_seconds": payload["remaining_seconds"],
                        "deadline": payload["deadline"],
                        "deadline_start_decision": deadline_decision,
                    },
                    durable=True,
                )
            _record_subagent_end(
                {
                    "name": definition.name,
                    "subagent_session_id": None,
                    "status": "failed",
                    "failure_category": "deadline",
                    "deadline_exhausted": payload["deadline_exhausted"],
                    "deadline_prevented_launch": True,
                    "remaining_seconds": payload["remaining_seconds"],
                    "steps_completed": 0,
                    "elapsed_ms": 0,
                    **_child_deadline_telemetry_fields(),
                },
            )
            return payload

        child_helpers_enabled = bool(
            self.helpers_enabled_for_children
            and not self.helper_only
            and definition.allow_workspace_writes is not False
            and getattr(
                getattr(cfg, "subagent_orchestration", None),
                "helpers_enabled",
                True,
            )
        )

        try:
            subagent_cfg, child_api_key, temperature_role = self._resolve_subagent_config(
                definition, cfg, api_key=api_key
            )
        except ConfigError as e:
            return {
                "error": f"Subagent profile/model resolution failed: {e}",
                "error_code": "subagent_profile_resolution_failed",
            }
        selected_model = subagent_cfg.model
        from ..profiles import get_active_profile

        child_profile = get_active_profile(subagent_cfg)
        child_route_metadata = {
            "model": selected_model,
            "profile_name": child_profile.name,
            "protocol": child_profile.protocol,
            "auth_provider": child_profile.auth_provider,
        }
        subagent_cfg.routing_mode = _ROUTING_MODE_CODE_ONLY
        # Skills are an independent session capability, not an implicit part of
        # delegation. Preserve the parent's resolved master switch explicitly;
        # bundled/custom discovery in the child continues to follow normal skill
        # configuration only when that switch is enabled.
        subagent_cfg.skills_enabled = self.skills_enabled
        resolved_temperature = resolve_role_temperature(subagent_cfg, role=temperature_role)
        subagent_cfg.temperature = resolved_temperature
        subagent_cfg.coding_temperature = resolved_temperature

        active_turn_budget = getattr(step_budget_runtime, "active_turn_budget", None)
        if type(active_turn_budget) is int and active_turn_budget > 0:
            parent_turn_budget = active_turn_budget
        elif type(max_steps) is int and max_steps > 0:
            parent_turn_budget = max_steps
        else:
            parent_turn_budget = max(1, int(cfg.max_steps))
        explicit_subagent_max_steps = args.get("max_steps") if "max_steps" in args else None
        if self.helper_only:
            helper_max_steps = cfg.subagent_orchestration.helper_max_steps
            if helper_max_steps is not None:
                if explicit_subagent_max_steps is None:
                    explicit_subagent_max_steps = helper_max_steps
                else:
                    explicit_subagent_max_steps = min(
                        max(1, int(explicit_subagent_max_steps)),
                        helper_max_steps,
                    )
        resolution = resolve_step_budget(
            StepBudgetRequest(
                kind="subagent",
                policy=cfg.step_budget_policy,
                hard_cap=(
                    cfg.subagent_orchestration.helper_max_steps
                    if self.helper_only
                    else cfg.subagent_max_steps
                ),
                fixed_override=(
                    explicit_subagent_max_steps if explicit_subagent_max_steps is not None else None
                ),
                mode=resolved_mode,
                subagent_name=definition.name,
                parent_turn_budget=parent_turn_budget,
                explicit_path_count=len(
                    _extract_repo_relative_paths_from_text(root=root, text=task)
                ),
            )
        )
        effective_subagent_max_steps = resolution.resolved_max_steps

        def _record_subagent_start(subagent_session_id: str | None) -> None:
            store.append(
                "subagent_start",
                {
                    "name": definition.name,
                    "subagent_session_id": subagent_session_id,
                    "mode": resolved_mode,
                    "model": selected_model,
                    **child_route_metadata,
                    "temperature_role": temperature_role,
                    "temperature": resolved_temperature,
                    "max_steps": effective_subagent_max_steps,
                    "parent_turn_budget": parent_turn_budget,
                    "step_budget": resolution.to_payload(),
                    "task": task,
                    **(
                        {"resumed_from": resume_context.resumed_from}
                        if resume_context is not None
                        else {}
                    ),
                    **_child_deadline_telemetry_fields(),
                },
            )
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "subagent_started",
                    {
                        "subagent": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "subagent_role": temperature_role,
                        "model": selected_model,
                        "max_steps": effective_subagent_max_steps,
                        **_child_deadline_telemetry_fields(),
                    },
                )

        inherited_active_workdir_relpath = (
            get_active_workdir_relpath() if callable(get_active_workdir_relpath) else "."
        )
        subagent_run_id = registered_run_id or preassigned_run_id or uuid.uuid4().hex
        subagent_label = subagent_task_label(
            task,
            requested_run_id=args.get("run_id"),
        )
        subagent_surface = NestedSubagentSurface(
            surface,
            subagent_name=definition.name,
            subagent_mode=resolved_mode,
            subagent_run_id=subagent_run_id,
            subagent_label=subagent_label,
            workspace_view=("pinned" if workspace_from_run else workspace_view),
            approval_lock=self.approval_lock,
        )
        subagent_started_at = perf_counter()
        child_run_tracker.run_id = subagent_run_id
        if registered_run_id is None:
            spawned_record = self.child_run_registry.register(
                run_id=subagent_run_id,
                definition_name=definition.name,
                started_monotonic=subagent_started_at,
                deadline_snapshot=child_execution_deadline.telemetry_snapshot(),
            )
            self._record_child_run_state(run_id=subagent_run_id, record=spawned_record)
            if self.child_scheduler is not None:
                self.child_scheduler.track_synchronous_child(
                    run_id=subagent_run_id,
                    definition_name=definition.name,
                    args=args,
                    launcher=self,
                    cancellation_token=cancellation_token,
                )
        child_root = root
        if resume_context is not None and resume_context.workspace_run_id:
            if self.workspace_provider is None:
                return {
                    "error": "Retained subagent workspace storage is unavailable.",
                    "error_code": "workspace_isolation_unavailable",
                    "subagent": definition.name,
                    "resumed_from": resume_context.resumed_from,
                }
            retained = self.workspace_provider.get(resume_context.workspace_run_id)
            if retained is None or retained.state in {"applied", "discarded", "duplicate"}:
                return {
                    "error": "The retained subagent worktree was already released.",
                    "error_code": "subagent_resume_worktree_released",
                    "subagent": definition.name,
                    "resumed_from": resume_context.resumed_from,
                }
            child_root = retained.worktree_path
        elif workspace_from_run:
            if definition.allow_workspace_writes:
                return {
                    "error": (
                        "workspace_from_run requires a subagent role that disallows "
                        "workspace writes."
                    ),
                    "error_code": "workspace_from_run_requires_readonly_role",
                    "subagent": definition.name,
                }
            if self.workspace_provider is None:
                return {
                    "error": "Pinned subagent workspace storage is unavailable.",
                    "error_code": "workspace_isolation_unavailable",
                    "subagent": definition.name,
                }
            pinned = self.workspace_provider.acquire_pin(
                workspace_from_run,
                consumer_run_id=subagent_run_id,
            )
            if not bool(pinned.get("ok")):
                return pinned
            child_run_tracker.pinned_source_run_id = str(pinned.get("run_id") or workspace_from_run)
            child_root = Path(str(pinned["worktree_path"]))
        elif workspace_view == "isolated":
            isolation_enabled = bool(
                getattr(
                    getattr(cfg, "subagent_orchestration", None),
                    "workspace_isolation_enabled",
                    True,
                )
            )
            if not isolation_enabled:
                return {
                    "error": "Isolated subagent workspaces are disabled for this session.",
                    "error_code": "workspace_isolation_disabled",
                    "subagent": definition.name,
                }
            if self.workspace_provider is None:
                return {
                    "error": "Isolated subagent workspace storage is unavailable.",
                    "error_code": "workspace_isolation_unavailable",
                    "subagent": definition.name,
                }
            prepared = self.workspace_provider.prepare(subagent_run_id)
            if not bool(prepared.get("ok")):
                return {
                    "error": str(prepared.get("error") or "Workspace preparation failed."),
                    "error_code": str(prepared.get("error_code") or "workspace_prepare_failed"),
                    "subagent": definition.name,
                    "workspace": {"view": "isolated"},
                }
            child_root = Path(str(prepared["worktree_path"]))
        subagent_surface.on_subagent_start(
            SubagentStartEvent(
                name=definition.name,
                mode=resolved_mode,
                description=str(definition.description or ""),
            )
        )
        child_deny_write_prefixes = deny_write_prefixes
        trusted_prompt_parts: list[str] = [_SUBAGENT_REPORT_EXPECTATIONS]
        if definition.prompt_trust == "trusted":
            trusted_prompt_parts.append(definition.system_prompt)
        trusted_prompt_append = "\n\n".join(trusted_prompt_parts) or None
        readonly_child_web_tool_names: tuple[str, ...] = ()
        if (
            subagent_depth == 0
            and _EXTERNAL_RESEARCH_CAPABILITY in definition.required_capabilities
        ):
            readonly_child_web_tool_names = tuple(
                name for name in definition.allow_tools if name in _EXTERNAL_RESEARCH_WEB_TOOLS
            )
        child_managed_browser_tool_names: tuple[str, ...] = ()
        if managed_browser_service is not None and subagent_depth == 0:
            child_managed_browser_tool_names = tuple(
                name
                for name in definition.allow_tools
                if name.startswith("browser_") and name != "browser_close"
            )

        try:
            sub_session = _create_session_for_subagent(
                create_session_factory=create_session_factory,
                cfg=subagent_cfg,
                root=child_root,
                mode=resolved_mode,
                runtime_kind=RuntimeKind.SUBAGENT,
                yes=yes,
                max_steps=effective_subagent_max_steps,
                no_log=no_log,
                api_key_override=child_api_key,
                console=None,
                deny_write_prefixes=child_deny_write_prefixes,
                allow_write_globs=allow_write_globs,
                persona_allow_write_globs=persona_allow_write_globs,
                non_interactive=non_interactive,
                session_log_dir_override=session_log_dir_override,
                prompt_cache_parent_session_id=self.prompt_cache_parent_session_id,
                **(
                    {"provider_session_id": resume_context.provider_session_id}
                    if resume_context is not None
                    else {}
                ),
                surface=subagent_surface,
                usage_role=f"{usage_role}:subagent:{definition.name}",
                trusted_system_prompt_append=trusted_prompt_append,
                untrusted_prompt_prelude=(
                    definition.system_prompt if definition.prompt_trust != "trusted" else None
                ),
                enable_compaction=False,
                enable_chat_turn_step_budget=False,
                verification_enabled=verification_enabled,
                authoritative_verification_commands=authoritative_verification_commands,
                one_shot_execution=False,
                subagents_enabled=False,
                helper_subagents_enabled=child_helpers_enabled,
                subagent_depth=subagent_depth + 1,
                subagent_registry=subagent_registry,
                active_workdir_relpath_override=inherited_active_workdir_relpath,
                execution_deadline=child_execution_deadline,
                crash_diagnostic_log_path=crash_diagnostic_log_path,
                readonly_child_web_tool_names=readonly_child_web_tool_names,
                managed_browser_service=managed_browser_service,
                managed_browser_owner_id=managed_browser_owner_id,
                managed_browser_cancel_check=managed_browser_cancel_check,
                child_managed_browser_tool_names=child_managed_browser_tool_names,
                **(
                    {"approval_reviewer": self.approval_reviewer}
                    if self.approval_reviewer is not None
                    else {}
                ),
            )
            subagent_session_id = str(
                getattr(getattr(sub_session, "store", None), "session_id", "") or ""
            )
            parent_session_id = str(getattr(self.store, "session_id", "") or "").strip() or None
            try:
                sub_session.evidence_parent_session_id = parent_session_id
            except Exception:  # noqa: BLE001 - lightweight child doubles may be read-only
                pass
            if parent_message_provider is not None:
                sub_session.step_system_message_provider = parent_message_provider
            if parent_message_delivery_observer is not None:
                sub_session.step_system_message_delivery_observer = parent_message_delivery_observer
            if resume_context is not None:
                target_ledger = getattr(sub_session, "read_ledger", None)
                seed_ledger = getattr(target_ledger, "seed_from_snapshot", None)
                if callable(seed_ledger):
                    seed_ledger(
                        resume_context.read_ledger_snapshot,
                        retained_messages=list(resume_context.history_messages),
                    )
                from .task_evidence import restore_conversation_evidence

                restore_conversation_evidence(
                    sub_session,
                    resume_context.evidence_events,
                    source_session_id=resume_context.source_session_id,
                )
            if not subagent_session_id:
                try:
                    sub_session.close()
                except Exception:
                    pass
                raise AgentRuntimeError("Created subagent session has no session ID.")
            scheduler = self.child_scheduler
            if scheduler is not None:
                sub_session.child_repetition_signal = (
                    lambda payload, child_run_id=subagent_run_id, owner=scheduler: (
                        owner.signal_parent_repetition(
                            run_id=child_run_id,
                            payload=payload,
                        )
                    )
                )
        except Exception as e:  # noqa: BLE001
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            _record_subagent_start(None)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="failed",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    error=f"Failed to initialize subagent session: {e}",
                )
            )
            _record_subagent_end(
                {
                    "name": definition.name,
                    "subagent_session_id": None,
                    "status": "failed",
                    "error": f"Failed to initialize subagent session: {e}",
                    "elapsed_ms": elapsed_ms,
                    "steps_completed": subagent_surface.steps_completed,
                    **_child_deadline_telemetry_fields(),
                },
            )
            return {
                "error": f"Failed to initialize subagent session: {e}",
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
            }

        main_tool_names = list(sub_session.tools.keys())
        tool_scope = resolve_subagent_tool_scope(
            tool_names=main_tool_names,
            allow_tools=definition.allow_tools,
            deny_tools=definition.deny_tools,
        )
        required_tool_error = _required_tool_launch_error(
            definition=definition,
            resolved_mode=resolved_mode,
            available_tool_names=set(tool_scope.allowed_names),
        )
        if required_tool_error is not None:
            sub_session.close()
            return {
                **required_tool_error,
                "subagent_session_id": None,
                "steps_completed": 0,
                "elapsed_ms": int((perf_counter() - subagent_started_at) * 1000),
            }

        child_run_tracker.child_session_id = subagent_session_id
        self._transition_child_run(
            run_id=subagent_run_id,
            state="running",
            child_session_id=subagent_session_id,
        )
        if on_session_started is not None:
            on_session_started(sub_session, subagent_surface)
        elif self.child_scheduler is not None:
            self.child_scheduler.attach_synchronous_session(
                run_id=subagent_run_id,
                sub_session=sub_session,
                subagent_surface=subagent_surface,
            )
        _record_subagent_start(subagent_session_id)
        allowed_names = list(tool_scope.allowed_names)
        unavailable_allowed_tools = list(tool_scope.unavailable_allowed_tools)
        if definition.allow_tools_explicit and unavailable_allowed_tools:
            sub_session.close()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            error_message = (
                f"Subagent '{definition.name}' requested unavailable allowlist tools: "
                f"{', '.join(unavailable_allowed_tools)}. "
                "Correct the configured allowlist or the child's tool availability before retrying."
            )
            requested_allowed_tools = [
                str(name).strip() for name in definition.allow_tools if str(name).strip()
            ]
            available_tool_names = sorted(
                name for name in main_tool_names if name != "subagent_run"
            )
            failure_payload = {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "failed",
                "failure_category": "tool_scope",
                "error_code": "subagent_allowlist_unavailable",
                "error": error_message,
                "effective_mode": resolved_mode,
                "requested_allowed_tools": requested_allowed_tools,
                "resolved_allowed_tools": allowed_names,
                "unavailable_allowed_tools": unavailable_allowed_tools,
                "available_tool_names": available_tool_names,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                **_child_deadline_telemetry_fields(),
            }
            _record_subagent_end(failure_payload)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="failed",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=error_message,
                )
            )
            return {
                "error": error_message,
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "failed",
                "failure_category": "tool_scope",
                "error_code": "subagent_allowlist_unavailable",
                "effective_mode": resolved_mode,
                "requested_allowed_tools": requested_allowed_tools,
                "resolved_allowed_tools": allowed_names,
                "unavailable_allowed_tools": unavailable_allowed_tools,
                "available_tool_names": available_tool_names,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
            }
        filtered_tools = {
            name: sub_session.tools[name] for name in allowed_names if name in sub_session.tools
        }
        sub_session.workspace_writes_allowed = bool(
            definition.allow_workspace_writes is not False
            and EDIT_CAPABLE_SUBAGENT_TOOL_NAMES.intersection(filtered_tools)
        )
        helper_tool = sub_session.tools.get("subagent_run")
        if (
            child_helpers_enabled
            and helper_tool is not None
            and EDIT_CAPABLE_SUBAGENT_TOOL_NAMES.intersection(filtered_tools)
        ):
            filtered_tools["subagent_run"] = helper_tool
            helper_launcher = getattr(helper_tool.run, "__self__", None)
            helper_summary = getattr(helper_launcher, "helper_runs_summary", None)
            if callable(helper_summary):
                child_run_tracker.helper_runs_provider = helper_summary
                helper_maximum = int(cfg.subagent_orchestration.helper_max_total_per_child)
                helper_catalog = _subagent_helper_catalog_message(
                    registry=subagent_registry,
                    names=tuple(getattr(helper_launcher, "helper_allowed_names", ())),
                    remaining=helper_maximum,
                )
                if isinstance(getattr(sub_session, "messages", None), list):
                    sub_session.messages.append({"role": "system", "content": helper_catalog})
        artifact_requirement = _subagent_artifact_requirement(definition)
        sub_session.tools = filtered_tools
        sub_session.tool_list = [tool.as_openai_tool() for tool in filtered_tools.values()]
        if not filtered_tools:
            sub_session.close()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="failed",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error="No tools available after allow/deny sandboxing.",
                )
            )
            _record_subagent_end(
                {
                    "name": definition.name,
                    "subagent_session_id": subagent_session_id,
                    "status": "failed",
                    "error": "No tools available after allow/deny sandboxing.",
                    "elapsed_ms": elapsed_ms,
                    "steps_completed": subagent_surface.steps_completed,
                    **_child_deadline_telemetry_fields(),
                },
            )
            return {
                "error": f"Subagent '{definition.name}' has no available tools after sandboxing.",
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
            }

        tool_catalog_message = _subagent_exact_tool_catalog_message(filtered_tools)
        if isinstance(getattr(sub_session, "messages", None), list):
            sub_session.messages.append({"role": "system", "content": tool_catalog_message})
            if resume_context is not None:
                # Finish rebuilding the current startup context before adding
                # retained dialogue. Keeping the catalog before history matches
                # the fresh-child layout without restoring stale permissions
                # or tools from an earlier run.
                sub_session.messages.extend(copy.deepcopy(list(resume_context.history_messages)))
                context_lines = [
                    "Host continuation context, not a new user request.",
                    f"This child resumes background run {resume_context.resumed_from}.",
                    "Continue from the restored conversation; re-check stale assumptions.",
                ]
                if resume_context.patch_artifact:
                    context_lines.append(
                        "Original patch artifact: " + resume_context.patch_artifact
                    )
                # Continuation metadata belongs with its dialogue. System-role
                # notices are hoisted by some adapters and discarded on replay.
                notice = {"role": "user", "content": "\n".join(context_lines)}
                sub_session.messages.append(notice)
                append_notice = getattr(getattr(sub_session, "store", None), "append", None)
                if callable(append_notice):
                    append_notice("subagent_resume_notice", {"message": notice})
        store.append(
            "subagent_tool_catalog",
            {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "tool_names": sorted(filtered_tools),
                "tool_count": len(filtered_tools),
            },
        )
        required_artifact_tools = set(
            artifact_requirement.get("required_tools", []) if artifact_requirement else []
        )
        missing_required_tools = sorted(required_artifact_tools - set(filtered_tools))
        if missing_required_tools:
            sub_session.close()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            error_message = (
                f"Subagent '{definition.name}' cannot produce its required artifact because "
                f"its sandbox is missing: {', '.join(missing_required_tools)}."
            )
            degraded_payload = {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": "artifact_capability",
                "error_code": "required_artifact_tool_unavailable",
                "artifact_requirement_problem": "required_tool_not_exposed",
                **(artifact_requirement or {}),
                "missing_required_tools": missing_required_tools,
                "error": error_message,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                **_child_deadline_telemetry_fields(),
                "sandbox": _sandbox_payload(filtered_tools),
                **_helper_runs_fields(),
            }
            _record_subagent_end(degraded_payload)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="degraded",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=error_message,
                )
            )
            return {
                "error": error_message,
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": "artifact_capability",
                "error_code": "required_artifact_tool_unavailable",
                "artifact_requirement_problem": "required_tool_not_exposed",
                **(artifact_requirement or {}),
                "missing_required_tools": missing_required_tools,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "sandbox": _sandbox_payload(filtered_tools),
            }

        final_text = ""
        final_text_source = "missing"
        internal_report_text = ""
        report_safety_payload = sanitize_subagent_report("").metadata()
        usage_payload: dict[str, Any] = {}
        artifact_success_event_types: set[str] = set()
        successful_tool_names: set[str] = set()
        exit_code = 1
        usage_replayed = False

        child_workspace_reconciliation_enabled = resolved_mode != "readonly"
        child_workspace_ignored_paths = [
            candidate
            for candidate in [
                getattr(store, "path", None),
                getattr(store, "session_artifact_root", None),
                getattr(getattr(sub_session, "store", None), "path", None),
                getattr(getattr(sub_session, "store", None), "session_artifact_root", None),
                session_log_dir_override,
            ]
            if isinstance(candidate, Path)
        ]
        child_workspace_ignored = _normalize_snapshot_ignore_paths(
            child_root,
            child_workspace_ignored_paths,
        )

        def _child_workspace_snapshot() -> dict[str, str]:
            if not child_workspace_reconciliation_enabled:
                return {}
            return {
                rel_path: signature
                for rel_path, signature in _snapshot_workspace_for_command_mutation_detection(
                    child_root
                ).items()
                if not _path_matches_snapshot_ignore(
                    rel_path,
                    ignored_paths=child_workspace_ignored,
                )
            }

        child_workspace_before_snapshot = _child_workspace_snapshot()
        child_workspace_effect_payload: dict[str, Any] | None = None

        def _reconcile_child_workspace_effects() -> dict[str, Any]:
            nonlocal child_workspace_effect_payload
            if child_workspace_effect_payload is not None:
                return dict(child_workspace_effect_payload)

            raw_child_touched_paths = getattr(sub_session, "workspace_touched_paths", set())
            reported_paths = {
                str(path or "").strip()
                for path in (
                    raw_child_touched_paths
                    if isinstance(raw_child_touched_paths, (set, list, tuple))
                    else ()
                )
                if str(path or "").strip()
            }
            reconciled_paths = set(reported_paths)
            if child_workspace_reconciliation_enabled:
                reconciled_paths.update(
                    _detect_command_mutation_paths(
                        before=child_workspace_before_snapshot,
                        after=_child_workspace_snapshot(),
                    )
                )
            touched_repo_paths = sorted(reconciled_paths)
            mutation_metadata = (
                self.command_mutation_metadata(
                    root=child_root,
                    touched_repo_paths=touched_repo_paths,
                )
                if touched_repo_paths
                else {}
            )
            material_paths = list(mutation_metadata.get("material_touched_repo_paths") or [])
            if touched_repo_paths:
                mutation_metadata["material_touched_repo_paths"] = material_paths
            child_workspace_effect_payload = {
                **task_outcome_fields(
                    getattr(sub_session, "last_turn_outcome", None), exit_code=exit_code
                ),
                "effects": [
                    "delegate",
                    "write_workspace" if material_paths else "read_workspace",
                ],
                "touched_repo_paths": touched_repo_paths,
                **mutation_metadata,
            }
            return dict(child_workspace_effect_payload)

        def _try_replay_subagent_usage_once() -> None:
            nonlocal usage_replayed
            if usage_replayed:
                return
            usage_replayed = True
            try:
                self.replay_registered_usage(
                    run_id=subagent_run_id,
                    sub_session=sub_session,
                    usage_lock=usage_replay_lock,
                )
            except Exception as e:  # noqa: BLE001
                store.append(
                    "warning",
                    {
                        "warning": "subagent_usage_replay_failed",
                        "name": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "error": str(e),
                    },
                )

        def _cancellation_requested(exc: BaseException | None = None) -> bool:
            if cancellation_token is not None and bool(
                getattr(cancellation_token, "is_cancelled", False)
            ):
                return True
            marker = str(exc or "").strip().casefold()
            return "cancelled_by_user" in marker or "canceled_by_user" in marker

        def _cancelled_subagent_result(exc: BaseException) -> dict[str, Any]:
            nonlocal usage_payload
            usage_payload = sub_session.usage_summary.totals()
            _try_replay_subagent_usage_once()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            error_message = "Subagent cancelled by the parent turn."
            workspace_effects = _reconcile_child_workspace_effects()
            workspace_effects["task_outcome"] = {
                **workspace_effects["task_outcome"],
                "outcome": "cancelled",
                "verified_success": False,
                "reason": "cancelled",
                "exit_code": 130,
            }
            workspace_effects["verified_success"] = False
            cancellation_report_text, _report_source = _resolve_subagent_final_text(
                sub_session=sub_session,
                subagent_surface=subagent_surface,
            )
            report_artifact = _persist_internal_subagent_report(
                store=store,
                subagent_session_id=subagent_session_id,
                report_text=cancellation_report_text,
            )
            partial_report = _screen_partial_subagent_report(cancellation_report_text)
            report_payload: dict[str, Any] = {"partial_report": partial_report}
            if report_artifact:
                report_payload.update(
                    {
                        "report_artifact": report_artifact,
                        "report_artifact_reader": "session_artifact_read",
                    }
                )
            cancelled_payload = {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "cancelled",
                "failure_category": "cancelled",
                "error_code": "subagent_cancelled",
                "exit_code": exit_code,
                "error": error_message,
                "cancellation_reason": str(exc or "cancelled_by_user"),
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                **_child_deadline_telemetry_fields(),
                **workspace_effects,
                **report_payload,
                **_helper_runs_fields(),
            }
            _record_subagent_end(cancelled_payload)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="cancelled",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=error_message,
                )
            )
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "subagent_completed",
                    {
                        "subagent": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "status": "cancelled",
                        "failure_category": "cancelled",
                        "error_code": "subagent_cancelled",
                        "exit_code": exit_code,
                        "duration_ms": elapsed_ms,
                        "steps_completed": subagent_surface.steps_completed,
                        **_child_deadline_telemetry_fields(),
                    },
                )
            return {
                "error": error_message,
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "cancelled",
                "failure_category": "cancelled",
                "error_code": "subagent_cancelled",
                "exit_code": exit_code,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                **workspace_effects,
                **report_payload,
            }

        try:
            try:
                run_turn_signature = inspect.signature(sub_session.run_turn)
                run_turn_parameters = run_turn_signature.parameters.values()
                accepts_var_keyword = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in run_turn_parameters
                )
                accepts_cancellation_token = (
                    "cancellation_token" in run_turn_signature.parameters or accepts_var_keyword
                )
            except (TypeError, ValueError):
                # Runtime session methods support cancellation; opaque wrappers
                # that cannot be inspected get the capable path as well.
                accepts_cancellation_token = True
            child_turn_kwargs: dict[str, Any] = {}
            if cancellation_token is not None and accepts_cancellation_token:
                child_turn_kwargs["cancellation_token"] = cancellation_token
            exit_code = sub_session.run_turn(task, **child_turn_kwargs)
            if _cancellation_requested():
                raise RuntimeError("cancelled_by_user")
            raw_final_text, final_text_source = _resolve_subagent_final_text(
                sub_session=sub_session,
                subagent_surface=subagent_surface,
            )
            internal_report_text = (
                raw_final_text if subagent_report_is_internal(final_text_source) else ""
            )
            sanitized_report = sanitize_subagent_report(raw_final_text)
            final_text = sanitized_report.text
            report_safety_payload = sanitized_report.metadata()
            usage_payload = sub_session.usage_summary.totals()
            artifact_success_event_types = _subagent_success_event_types(sub_session)
            successful_tool_names = _subagent_successful_tool_names(sub_session)
        except KeyboardInterrupt as e:
            if not _cancellation_requested(e):
                raise
            return _cancelled_subagent_result(e)
        except Exception as e:  # noqa: BLE001
            if _cancellation_requested(e):
                return _cancelled_subagent_result(e)
            usage_payload = sub_session.usage_summary.totals()
            _try_replay_subagent_usage_once()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            safe_execution_error = sanitize_subagent_report(f"Subagent execution failed: {e}")
            report_safety_payload = safe_execution_error.metadata()
            workspace_effects = _reconcile_child_workspace_effects()
            _record_subagent_end(
                {
                    "name": definition.name,
                    "subagent_session_id": subagent_session_id,
                    "status": "failed",
                    "exit_code": exit_code,
                    "error": safe_execution_error.text,
                    "report_safety": report_safety_payload,
                    "usage": usage_payload,
                    "elapsed_ms": elapsed_ms,
                    "steps_completed": subagent_surface.steps_completed,
                    **_child_deadline_telemetry_fields(),
                    **workspace_effects,
                    **_helper_runs_fields(),
                },
            )
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="failed",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=safe_execution_error.text,
                )
            )
            return {
                "error": f"Subagent '{definition.name}' execution failed: "
                f"{safe_execution_error.text.removeprefix('Subagent execution failed: ')}",
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "report_safety": report_safety_payload,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                **workspace_effects,
            }
        finally:
            try:
                sub_session.close()
            except Exception as e:  # noqa: BLE001 - cleanup failure must not duplicate lifecycle end
                store.append(
                    "warning",
                    {
                        "warning": "subagent_close_failed",
                        "name": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "error": str(e),
                    },
                )

        workspace_effects = _reconcile_child_workspace_effects()
        termination_kind = _subagent_termination_kind(sub_session)
        deadline_blocked_operations, deadline_exhaustion_recorded = (
            _subagent_deadline_event_summary(sub_session)
        )
        deadline_exhausted = bool(
            child_execution_deadline.is_exhausted()
            or deadline_exhaustion_recorded
            or termination_kind in {"deadline_exhausted", "run_budget_exhausted"}
        )
        deadline_observability = {
            "deadline_blocked_operations": deadline_blocked_operations,
        }
        stopped_for_exhaustion = bool(
            deadline_exhausted or termination_kind in _EXHAUSTION_TERMINATION_KINDS
        )
        incomplete_report_text = raw_final_text if stopped_for_exhaustion else internal_report_text

        if internal_report_text or stopped_for_exhaustion:
            outcome = workspace_effects["task_outcome"]
            if deadline_exhausted or outcome["outcome"] in {
                "verified_success",
                "completed_unverified",
            }:
                workspace_effects["task_outcome"] = {
                    **outcome,
                    "outcome": "deadline_exceeded" if deadline_exhausted else "incomplete",
                    "verified_success": False,
                    "reason": "deadline_exhausted" if deadline_exhausted else "subagent_incomplete",
                }
                workspace_effects["verified_success"] = False
            # The child produced a runtime stop report, not a deliverable. The
            # report is kept as an internal artifact and the parent is handed a
            # structured status to act on, so half-finished internal state can
            # never present itself as finished work.
            _try_replay_subagent_usage_once()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            stop_reason = _subagent_stop_reason(
                sub_session,
                deadline_exhausted=deadline_exhausted,
            )
            incomplete_result = self._capture_isolated_workspace_result(
                args=args,
                tracker=child_run_tracker,
                result={
                    "error": f"Subagent '{definition.name}' stopped before finishing.",
                    "subagent": definition.name,
                    "subagent_session_id": subagent_session_id,
                    "status": "incomplete",
                    "failure_category": "incomplete",
                    "error_code": SUBAGENT_INCOMPLETE_ERROR_CODE,
                    "usage": usage_payload,
                    "elapsed_ms": elapsed_ms,
                    "steps_completed": subagent_surface.steps_completed,
                    "deadline_exhausted": deadline_exhausted,
                    "deadline_prevented_launch": False,
                    **deadline_observability,
                    "report_safety": report_safety_payload,
                    **workspace_effects,
                },
                workspace_run_id=(
                    resume_context.workspace_run_id if resume_context is not None else None
                ),
            )
            retained_worktree_run_id = ""
            if workspace_view == "isolated" and self.workspace_provider is not None:
                captured_run_id = (
                    resume_context.workspace_run_id
                    if resume_context is not None and resume_context.workspace_run_id
                    else subagent_run_id
                )
                workspace_record = self.workspace_provider.get(captured_run_id)
                if (
                    workspace_record is not None
                    and workspace_record.state == "captured"
                    and not workspace_record.no_changes
                ):
                    retained_worktree_run_id = subagent_run_id
            resumable_run_id = subagent_run_id
            resume_affordance = ""
            report_artifact = _persist_internal_subagent_report(
                store=store,
                subagent_session_id=subagent_session_id,
                report_text=incomplete_report_text,
            )
            if resumable_run_id:
                preserved_state = (
                    "transcript and worktree" if retained_worktree_run_id else "transcript"
                )
                resume_affordance = self._incomplete_resume_affordance(
                    args=args,
                    definition=definition,
                    run_id=resumable_run_id,
                    preserved_state=preserved_state,
                    report_artifact=report_artifact,
                )
            partial_report_excerpt = ""
            partial_report_truncated = False
            partial_report_safety: dict[str, Any] | None = None
            if report_artifact and incomplete_report_text.strip():
                partial_report = _screen_partial_subagent_report(incomplete_report_text)
                if partial_report["status"] == "partial":
                    partial_report_excerpt = str(partial_report["excerpt"])
                    partial_report_truncated = bool(partial_report["truncated"])
                    partial_report_safety = dict(partial_report["safety"])

            incomplete_status = SubagentIncompleteStatus(
                subagent=definition.name,
                reason=stop_reason,
                steps_used=subagent_surface.steps_completed,
                # The budget the child was given, not what happened to be left when
                # the parent looked: a record that says "step_budget_exhausted,
                # deadline_s: 899.9" reads as a contradiction.
                deadline_s=child_execution_deadline.configured_duration_seconds,
                resolved_step_ceiling=effective_subagent_max_steps,
                deadline_remaining_s=child_execution_deadline.remaining_seconds(),
                report_artifact=report_artifact,
                subagent_session_id=subagent_session_id,
                elapsed_ms=elapsed_ms,
                run_id=subagent_run_id,
                retained_worktree_run_id=retained_worktree_run_id,
                resume_affordance=resume_affordance,
                partial_report_excerpt=partial_report_excerpt,
                partial_report_truncated=partial_report_truncated,
                partial_report_safety=partial_report_safety,
            )
            store.append("subagent_incomplete", incomplete_status.telemetry_payload())
            _record_subagent_end(
                {
                    "name": definition.name,
                    "subagent_session_id": subagent_session_id,
                    "status": "incomplete",
                    "failure_category": "incomplete",
                    "error_code": SUBAGENT_INCOMPLETE_ERROR_CODE,
                    "error": incomplete_status.message,
                    "exit_code": exit_code,
                    "usage": usage_payload,
                    "elapsed_ms": elapsed_ms,
                    "steps_completed": subagent_surface.steps_completed,
                    "deadline_exhausted": deadline_exhausted,
                    "deadline_prevented_launch": False,
                    **deadline_observability,
                    **incomplete_status.diagnostics_payload(),
                    **_child_deadline_telemetry_fields(),
                    "report_safety": report_safety_payload,
                    **workspace_effects,
                    **(
                        {"workspace": incomplete_result["workspace"]}
                        if "workspace" in incomplete_result
                        else {}
                    ),
                    **(
                        {"patch_summary": incomplete_result["patch_summary"]}
                        if "patch_summary" in incomplete_result
                        else {}
                    ),
                    **_helper_runs_fields(),
                },
            )
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="incomplete",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=incomplete_status.message,
                )
            )
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "subagent_completed",
                    {
                        "subagent": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "status": "incomplete",
                        "failure_category": "incomplete",
                        "error_code": SUBAGENT_INCOMPLETE_ERROR_CODE,
                        "exit_code": exit_code,
                        "duration_ms": elapsed_ms,
                        "steps_completed": subagent_surface.steps_completed,
                        **incomplete_status.diagnostics_payload(),
                        **_child_deadline_telemetry_fields(),
                    },
                )
            return {
                **incomplete_result,
                **incomplete_status.tool_result(),
                "usage": usage_payload,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": deadline_exhausted,
                "deadline_prevented_launch": False,
                **deadline_observability,
                "report_safety": report_safety_payload,
                **workspace_effects,
            }

        if exit_code != 0:
            _try_replay_subagent_usage_once()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            error_payload = {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "failed",
                "exit_code": exit_code,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": deadline_exhausted,
                "deadline_prevented_launch": False,
                **deadline_observability,
                **_child_deadline_telemetry_fields(),
                "report_safety": report_safety_payload,
                **workspace_effects,
            }
            if final_text:
                error_payload["final_text"] = final_text
                error_payload["final_text_source"] = final_text_source
            error_payload.update(_helper_runs_fields())
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="failed",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=(final_text or f"Subagent '{definition.name}' failed."),
                )
            )
            _record_subagent_end(error_payload)
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "subagent_completed",
                    {
                        "subagent": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "status": "failed",
                        "exit_code": exit_code,
                        "duration_ms": elapsed_ms,
                        "steps_completed": subagent_surface.steps_completed,
                        **_child_deadline_telemetry_fields(),
                    },
                )
            failure_result = {
                "error": f"Subagent '{definition.name}' failed.",
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "exit_code": exit_code,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": error_payload["deadline_exhausted"],
                "deadline_prevented_launch": False,
                **deadline_observability,
                "report_safety": report_safety_payload,
                **workspace_effects,
            }
            failure_result["final_text"] = final_text
            failure_result["final_text_source"] = final_text_source
            return failure_result

        unexpected_mutation_paths = list(workspace_effects.get("material_touched_repo_paths") or [])
        if unexpected_mutation_paths and not definition.allow_workspace_writes:
            _try_replay_subagent_usage_once()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            displayed_paths = ", ".join(unexpected_mutation_paths[:20])
            if len(unexpected_mutation_paths) > 20:
                displayed_paths += f", and {len(unexpected_mutation_paths) - 20} more"
            error_message = (
                f"Subagent '{definition.name}' modified the workspace despite its "
                f"non-editing role contract: {displayed_paths}."
            )
            degraded_payload = {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": "workspace_mutation",
                "error_code": "unexpected_workspace_mutation",
                "error": error_message,
                "final_text": final_text,
                "final_text_source": final_text_source,
                "exit_code": exit_code,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": deadline_exhausted,
                "deadline_prevented_launch": False,
                **deadline_observability,
                **_child_deadline_telemetry_fields(),
                "report_safety": report_safety_payload,
                "sandbox": _sandbox_payload(filtered_tools),
                **workspace_effects,
                **_helper_runs_fields(),
            }
            _record_subagent_end(degraded_payload)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="degraded",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=error_message,
                )
            )
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "subagent_completed",
                    {
                        "subagent": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "status": "degraded",
                        "failure_category": "workspace_mutation",
                        "error_code": "unexpected_workspace_mutation",
                        "exit_code": exit_code,
                        "duration_ms": elapsed_ms,
                        "steps_completed": subagent_surface.steps_completed,
                        "touched_repo_paths": unexpected_mutation_paths,
                        **_child_deadline_telemetry_fields(),
                    },
                )
            return {
                "error": error_message,
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": "workspace_mutation",
                "error_code": "unexpected_workspace_mutation",
                "usage": usage_payload,
                "final_text": final_text,
                "final_text_source": final_text_source,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": degraded_payload["deadline_exhausted"],
                "deadline_prevented_launch": False,
                **deadline_observability,
                "report_safety": report_safety_payload,
                "sandbox": _sandbox_payload(filtered_tools),
                **workspace_effects,
            }

        final_report_problem = _subagent_final_report_problem(
            text=final_text,
            source=final_text_source,
        )
        if final_report_problem is not None:
            _try_replay_subagent_usage_once()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            error_message = (
                f"Subagent '{definition.name}' did not produce a substantive final "
                f"report ({final_report_problem})."
            )
            degraded_payload: dict[str, Any] = {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": "final_report",
                "final_report_problem": final_report_problem,
                "final_text_source": final_text_source,
                "exit_code": exit_code,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": deadline_exhausted,
                "deadline_prevented_launch": False,
                **deadline_observability,
                "error": error_message,
                **_child_deadline_telemetry_fields(),
                "report_safety": report_safety_payload,
                **workspace_effects,
                **_helper_runs_fields(),
            }
            if final_text:
                degraded_payload["final_text"] = final_text
            _record_subagent_end(degraded_payload)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="degraded",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=error_message,
                )
            )
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "subagent_completed",
                    {
                        "subagent": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "status": "degraded",
                        "exit_code": exit_code,
                        "duration_ms": elapsed_ms,
                        "steps_completed": subagent_surface.steps_completed,
                        "final_report_problem": final_report_problem,
                        **_child_deadline_telemetry_fields(),
                    },
                )
            degraded_result = {
                "error": error_message,
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": "final_report",
                "final_report_problem": final_report_problem,
                "usage": usage_payload,
                "final_text": final_text,
                "final_text_source": final_text_source,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": degraded_payload["deadline_exhausted"],
                "deadline_prevented_launch": False,
                **deadline_observability,
                "report_safety": report_safety_payload,
                "sandbox": _sandbox_payload(filtered_tools.keys()),
                **workspace_effects,
                **_helper_runs_fields(),
            }
            return degraded_result

        required_success_event_types = set(
            artifact_requirement.get("success_event_types", []) if artifact_requirement else []
        )
        observed_success_event_types = sorted(
            required_success_event_types & artifact_success_event_types
        )
        missing_success_event_types = sorted(
            required_success_event_types - artifact_success_event_types
        )
        required_success_tool_names = set(
            artifact_requirement.get("success_tool_names", []) if artifact_requirement else []
        )
        observed_success_tool_names = sorted(required_success_tool_names & successful_tool_names)
        minimum_success_tool_events = int(
            artifact_requirement.get("minimum_success_tool_events", 0)
            if artifact_requirement
            else 0
        )
        missing_success_tool_evidence = (
            len(observed_success_tool_names) < minimum_success_tool_events
        )
        materializes_artifacts = bool(
            artifact_requirement.get("materializes_artifacts") if artifact_requirement else False
        )
        if missing_success_event_types or missing_success_tool_evidence:
            _try_replay_subagent_usage_once()
            elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)
            if materializes_artifacts:
                error_message = (
                    f"Subagent '{definition.name}' returned a report without evidence that its "
                    "required artifact was generated successfully."
                )
                failure_category = "artifact_capability"
                error_code = "required_artifact_evidence_missing"
                requirement_problem = "successful_generation_not_evidenced"
            else:
                error_message = (
                    f"Subagent '{definition.name}' returned a report without successful "
                    "external research evidence."
                )
                failure_category = "capability_evidence"
                error_code = "required_capability_evidence_missing"
                requirement_problem = "successful_tool_not_evidenced"
            degraded_payload = {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": failure_category,
                "error_code": error_code,
                "artifact_requirement_problem": requirement_problem,
                **(artifact_requirement or {}),
                "observed_success_event_types": observed_success_event_types,
                "missing_success_event_types": missing_success_event_types,
                "observed_success_tool_names": observed_success_tool_names,
                "final_text": final_text,
                "final_text_source": final_text_source,
                "exit_code": exit_code,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": deadline_exhausted,
                "deadline_prevented_launch": False,
                **deadline_observability,
                "error": error_message,
                **_child_deadline_telemetry_fields(),
                "report_safety": report_safety_payload,
                "sandbox": _sandbox_payload(filtered_tools),
                **workspace_effects,
                **_helper_runs_fields(),
            }
            _record_subagent_end(degraded_payload)
            subagent_surface.on_subagent_end(
                SubagentEndEvent(
                    name=definition.name,
                    mode=resolved_mode,
                    status="degraded",
                    elapsed_ms=elapsed_ms,
                    steps_completed=subagent_surface.steps_completed,
                    subagent_session_id=subagent_session_id,
                    error=error_message,
                )
            )
            if crash_diagnostics is not None:
                crash_diagnostics.event(
                    "subagent_completed",
                    {
                        "subagent": definition.name,
                        "subagent_session_id": subagent_session_id,
                        "status": "degraded",
                        "failure_category": failure_category,
                        "error_code": error_code,
                        "exit_code": exit_code,
                        "duration_ms": elapsed_ms,
                        "steps_completed": subagent_surface.steps_completed,
                        "missing_success_event_types": missing_success_event_types,
                        **_child_deadline_telemetry_fields(),
                    },
                )
            return {
                "error": error_message,
                "subagent": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": "degraded",
                "failure_category": failure_category,
                "error_code": error_code,
                "artifact_requirement_problem": requirement_problem,
                **(artifact_requirement or {}),
                "observed_success_event_types": observed_success_event_types,
                "missing_success_event_types": missing_success_event_types,
                "observed_success_tool_names": observed_success_tool_names,
                "usage": usage_payload,
                "final_text": final_text,
                "final_text_source": final_text_source,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                "deadline_exhausted": degraded_payload["deadline_exhausted"],
                "deadline_prevented_launch": False,
                **deadline_observability,
                "report_safety": report_safety_payload,
                "sandbox": _sandbox_payload(filtered_tools),
                **workspace_effects,
            }

        _try_replay_subagent_usage_once()
        elapsed_ms = int((perf_counter() - subagent_started_at) * 1000)

        capability_evidence = (
            {
                **(artifact_requirement or {}),
                "observed_success_event_types": observed_success_event_types,
                "observed_success_tool_names": observed_success_tool_names,
            }
            if artifact_requirement is not None
            else None
        )
        evidence_result_key = (
            "artifact_evidence" if materializes_artifacts else "capability_evidence"
        )

        success_result = {
            "subagent": definition.name,
            "model": selected_model,
            **child_route_metadata,
            "subagent_session_id": subagent_session_id,
            "status": "success",
            "result": final_text,
            "result_source": final_text_source,
            "usage": usage_payload,
            "elapsed_ms": elapsed_ms,
            "steps_completed": subagent_surface.steps_completed,
            **workspace_effects,
            "deadline_exhausted": deadline_exhausted,
            "deadline_prevented_launch": False,
            **deadline_observability,
            "report_safety": report_safety_payload,
            "sandbox": _sandbox_payload(filtered_tools.keys()),
        }
        if capability_evidence is not None:
            success_result[evidence_result_key] = capability_evidence
        success_result = self._capture_isolated_workspace_result(
            args=args,
            tracker=child_run_tracker,
            result=success_result,
            workspace_run_id=(resume_context.workspace_run_id if resume_context else None),
        )
        terminal_status = str(success_result["status"])
        workspace_capture_evidence = {
            key: success_result[key]
            for key in (
                "workspace",
                "patch_summary",
                "patch_capture_status",
                "material_patch_complete",
                "capture_reason_codes",
                "omitted_material_paths",
                "retained_ignored_paths",
                "ignored_outputs_integrated",
                "retention_note",
                "candidate_worktree_retained",
                "failure_category",
                "error_code",
                "error",
            )
            if key in success_result
        }
        _record_subagent_end(
            {
                "name": definition.name,
                "subagent_session_id": subagent_session_id,
                "status": terminal_status,
                "exit_code": exit_code,
                "usage": usage_payload,
                "elapsed_ms": elapsed_ms,
                "steps_completed": subagent_surface.steps_completed,
                **workspace_effects,
                **workspace_capture_evidence,
                "final_text_source": final_text_source,
                "deadline_exhausted": deadline_exhausted,
                "deadline_prevented_launch": False,
                **deadline_observability,
                **_child_deadline_telemetry_fields(),
                "report_safety": report_safety_payload,
                **({evidence_result_key: capability_evidence} if capability_evidence else {}),
                **_helper_runs_fields(),
            },
        )
        subagent_surface.on_subagent_end(
            SubagentEndEvent(
                name=definition.name,
                mode=resolved_mode,
                status=terminal_status,
                elapsed_ms=elapsed_ms,
                steps_completed=subagent_surface.steps_completed,
                subagent_session_id=subagent_session_id,
            )
        )
        if crash_diagnostics is not None:
            crash_diagnostics.event(
                "subagent_completed",
                {
                    "subagent": definition.name,
                    "subagent_session_id": subagent_session_id,
                    "status": terminal_status,
                    "exit_code": exit_code,
                    "duration_ms": elapsed_ms,
                    "steps_completed": subagent_surface.steps_completed,
                    **_child_deadline_telemetry_fields(),
                },
            )
        return success_result


class _ChildCancellationToken:
    def __init__(self, *, parent: Any | None = None) -> None:
        self._event = Event()
        self._parent = parent
        self._abort_lock = RLock()
        self._abort_callback: Callable[[], None] | None = None
        self._unsubscribe_parent: Callable[[], None] | None = None
        subscribe = getattr(parent, "subscribe", None)
        if callable(subscribe):
            unsubscribe = subscribe(self.cancel)
            with self._abort_lock:
                if self._event.is_set():
                    unsubscribe_now = unsubscribe
                else:
                    self._unsubscribe_parent = unsubscribe
                    unsubscribe_now = None
            if unsubscribe_now is not None:
                unsubscribe_now()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set() or bool(getattr(self._parent, "is_cancelled", False))

    def belongs_to_parent(self, parent: Any) -> bool:
        """Return whether this child was accepted by ``parent``'s turn."""

        return self._parent is parent

    def cancel(self) -> None:
        with self._abort_lock:
            if self._event.is_set():
                return
            self._event.set()
            abort_callback = self._abort_callback
            self._abort_callback = None
            unsubscribe_parent = self._unsubscribe_parent
            self._unsubscribe_parent = None
        if unsubscribe_parent is not None:
            unsubscribe_parent()
        if abort_callback is not None:
            try:
                abort_callback()
            except Exception:
                # Cancellation is best-effort and must remain observable even
                # when a transport's close hook has already failed or closed.
                pass

    def set_abort_callback(self, callback: Callable[[], None]) -> None:
        abort_now = False
        with self._abort_lock:
            if self.is_cancelled:
                abort_now = True
            else:
                self._abort_callback = callback
        if abort_now:
            try:
                callback()
            except Exception:
                pass

    def clear_abort_callback(self) -> None:
        with self._abort_lock:
            self._abort_callback = None

    def close(self) -> None:
        """Detach a completed child from its parent turn token."""

        with self._abort_lock:
            unsubscribe_parent = self._unsubscribe_parent
            self._unsubscribe_parent = None
            self._abort_callback = None
        if unsubscribe_parent is not None:
            unsubscribe_parent()

    def throw_if_cancelled(self, reason: str = "cancelled_by_user") -> None:
        parent_throw = getattr(self._parent, "throw_if_cancelled", None)
        if callable(parent_throw):
            parent_throw(reason)
        if self.is_cancelled:
            raise CooperativeCancellationError(reason)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until this child is cancelled without polling."""

        if bool(getattr(self._parent, "is_cancelled", False)):
            self.cancel()
        return self._event.wait(timeout)


@dataclass
class _ScheduledChild:
    # A launch context is immutable for the lifetime of a queued/running child.
    # Permission/tool-surface rebuilds bind a new launcher for future children
    # without changing the authority or configuration of work already accepted.
    launcher: SubagentLauncher
    run_id: str
    definition_name: str
    args: dict[str, Any]
    label: str
    cancellation_token: Any
    completion: Future[dict[str, Any]]
    usage_lock: RLock
    background: bool
    depends_on: tuple[str, ...] = ()
    sub_session: Any | None = None
    subagent_surface: NestedSubagentSurface | None = None
    executor_future: Future[Any] | None = None
    # Executor futures become ``done`` before their callbacks necessarily
    # finish. Cleanup waits on this signal so terminal state and rollup writes
    # complete before the workspace provider and parent store are closed.
    worker_bookkeeping_completion: Future[None] = field(default_factory=Future)
    inbox: deque[str] = field(default_factory=deque)
    inbox_lock: RLock = field(default_factory=RLock)
    pending_delivery_batches: deque[tuple[int, int]] = field(default_factory=deque)
    messages_queued: int = 0
    messages_delivered: int = 0
    collected: bool = False
    resume_context: _ChildResumeContext | None = None
    continuation_run_id: str | None = None
    last_event_monotonic: float = field(default_factory=perf_counter)
    last_event_kind: str = "lifecycle"
    inactivity_signal_sent: bool = False
    launcher_terminal_event_emitted: bool = False

    def drain_parent_messages(self) -> list[str]:
        with self.inbox_lock:
            messages = list(self.inbox)
            self.inbox.clear()
            if messages:
                self.pending_delivery_batches.append(
                    (len(messages), sum(len(message) for message in messages))
                )
        return [f"Message from the parent agent: {message}" for message in messages]

    def record_parent_message_delivery(self, step: int) -> None:
        with self.inbox_lock:
            if not self.pending_delivery_batches:
                return
            delivered_count, delivered_chars = self.pending_delivery_batches.popleft()
            self.messages_delivered += delivered_count
        append_event = getattr(getattr(self.sub_session, "store", None), "append", None)
        if callable(append_event):
            append_event(
                "subagent_message_delivered",
                {
                    "run_id": self.run_id,
                    "chars": delivered_chars,
                    "step": int(step),
                },
            )


@dataclass
class _ChildRollup:
    run_ids: tuple[str, ...]
    started_monotonic: float
    kind: Literal["batch", "chain"]


_BACKGROUND_OWNERSHIP_NOTE = (
    "Keep the assigned work with this child and advance different useful work. "
    "An independent review should test a distinct uncertainty. "
    "Completed reports arrive automatically at a parent boundary; wait when the result "
    "is a dependency. Avoid progress polling and filler work."
)


class SubagentCoordinator:
    """Session-owned executor and lifecycle manager for subagent children.

    The coordinator, registry, workspace provider and executor belong to one
    logical parent session.  A tool-surface rebuild may bind a fresh launcher
    for future submissions, while each accepted child retains the launcher
    context it was validated with.
    """

    def __init__(
        self,
        *,
        launcher: SubagentLauncher,
        max_background_children: int,
        batch_parallel_cap: int = 4,
        parent_steer_inbox: SteerInbox | None = None,
    ) -> None:
        self.launcher = launcher
        self.registry = launcher.child_run_registry
        self.max_background_children = max(1, int(max_background_children))
        self.batch_parallel_cap = max(1, int(batch_parallel_cap))
        self.parent_steer_inbox = parent_steer_inbox
        self._executor = ThreadPoolExecutor(
            max_workers=max(self.max_background_children, self.batch_parallel_cap),
            thread_name_prefix="subagent-child",
        )
        self._executor_worker_capacity = max(
            self.max_background_children,
            self.batch_parallel_cap,
        )
        self._retired_executors: list[ThreadPoolExecutor] = []
        self._children: dict[str, _ScheduledChild] = {}
        self._background_queue: deque[str] = deque()
        self._active_background = 0
        self._batch_slots = BoundedSemaphore(self.batch_parallel_cap)
        self._lock = RLock()
        self._closed = False
        self._workspace_cleanup_attempt_in_progress = False
        self._workspace_cleanup_attempted = False
        self._workspace_cleanup_succeeded = False
        self._workspace_cleanup_attempt_count = 0
        self._workspace_cleanup_summary: dict[str, Any] | None = None
        self._workspace_cleanup_callbacks: list[Callable[[], None]] = []
        self._rollups: dict[str, _ChildRollup] = {}
        self._rollup_sequence = 0
        self._lifecycle_listener: Callable[[dict[str, Any]], None] | None = None
        self._lifecycle_last: dict[str, tuple[str, bool, str]] = {}
        # A parent token is unique to one turn.  Its cancellation grace is a
        # boundary, not a retry budget: subagent_wait, the next turn checkpoint,
        # and session finalization may all observe the same request, but only the
        # first observer is allowed to join children.  Children retain their
        # parent token, so keeping identity keys here cannot collide with a later
        # turn while an old child remains registered.
        self._parent_cancellation_wait_claimed: set[int] = set()
        launcher.subagent_coordinator = self
        launcher.child_scheduler = self

    @property
    def scheduler(self) -> SubagentCoordinator:
        """Return the stable engine under the legacy scheduler-facing API."""

        return self

    @property
    def workspace_provider(self) -> SubagentWorkspaceProvider | None:
        return self.launcher.workspace_provider

    def bind_launcher(
        self,
        launcher: SubagentLauncher,
        *,
        max_background_children: int,
        parent_steer_inbox: SteerInbox | None,
    ) -> None:
        """Bind launch policy for future children without replacing runtime state."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Subagent coordinator is closed.")
            if launcher.child_run_registry is not self.registry:
                raise ValueError("A rebound launcher must reuse the session child registry.")
            if launcher.workspace_provider is not self.launcher.workspace_provider:
                raise ValueError("A rebound launcher must reuse the session workspace provider.")
            if launcher.store is not self.launcher.store:
                raise ValueError("A rebound launcher must belong to the same logical session.")
            resolved_background_cap = max(1, int(max_background_children))
            required_worker_capacity = max(
                resolved_background_cap,
                self.batch_parallel_cap,
            )
            if required_worker_capacity > self._executor_worker_capacity:
                replacement_executor = ThreadPoolExecutor(
                    max_workers=required_worker_capacity,
                    thread_name_prefix="subagent-child",
                )
                self._retired_executors.append(self._executor)
                self._executor = replacement_executor
                self._executor_worker_capacity = required_worker_capacity
            self.launcher = launcher
            self.max_background_children = resolved_background_cap
            self.parent_steer_inbox = parent_steer_inbox
            launcher.subagent_coordinator = self
            launcher.child_scheduler = self
            self._start_queued_background_locked()

    def set_parent_steer_inbox(self, inbox: SteerInbox | None) -> None:
        """Bind the parent wake channel after a live tool-surface rebuild."""
        self.parent_steer_inbox = inbox

    def _track_completion_clock(self, child: _ScheduledChild) -> None:
        def _record_completion(_future: Future[dict[str, Any]]) -> None:
            self.registry.mark_finished(
                child.run_id,
                finished_monotonic=perf_counter(),
            )
            close_cancellation = getattr(child.cancellation_token, "close", None)
            if callable(close_cancellation):
                close_cancellation()

        child.completion.add_done_callback(_record_completion)

    def _consume_parent_wait_signals(self) -> list[dict[str, str]]:
        inbox = self.parent_steer_inbox
        if inbox is None or not inbox.wake_event.is_set():
            return []
        return inbox.consume_wait_signals()

    def signal_parent_repetition(
        self,
        *,
        run_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Notify the parent that a running child has stopped producing information."""
        with self._lock:
            child = self._children.get(run_id)
            record = self.registry.get(run_id)
            active = bool(
                child is not None
                and record is not None
                and record.state not in {"joined", "cancelled"}
                and not child.completion.done()
            )
        event_payload = {
            "run_id": run_id,
            "subagent": child.definition_name if child is not None else "",
            "active": active,
            "reason": str(payload.get("reason") or "child_repetition"),
            "tool_name": str(payload.get("tool_name") or ""),
            "consecutive_identical_outcomes": int(
                payload.get("consecutive_identical_outcomes") or 0
            ),
            "threshold": int(payload.get("threshold") or 0),
            "fingerprint_prefix": str(payload.get("fingerprint_prefix") or ""),
            "occurrences": int(payload.get("occurrences") or 0),
            "window": int(payload.get("window") or 0),
            "distinct_recent_outcomes": int(payload.get("distinct_recent_outcomes") or 0),
            "step": int(payload.get("step") or 0),
            "elapsed_ms": int(payload.get("elapsed_ms") or 0),
            "total_tokens": int(payload.get("total_tokens") or 0),
        }
        launch_context = child.launcher if child is not None else self.launcher
        launch_context.store.append("subagent_repetition_signal", event_payload)
        inbox = self.parent_steer_inbox
        if not active or inbox is None:
            return False
        inbox.signal_waiters(reason=event_payload["reason"], run_id=run_id)
        return True

    def _signal_inactive_children(self, children: list[_ScheduledChild]) -> None:
        now = perf_counter()
        signals: list[dict[str, Any]] = []
        with self._lock:
            for child in children:
                record = self.registry.get(child.run_id)
                if (
                    child.completion.done()
                    or child.inactivity_signal_sent
                    or record is None
                    or record.state != "running"
                ):
                    continue
                orchestration = getattr(child.launcher.cfg, "subagent_orchestration", None)
                threshold_s = max(
                    0.001,
                    float(getattr(orchestration, "inactivity_signal_after_s", 180.0)),
                )
                age_s = max(0.0, now - child.last_event_monotonic)
                if age_s < threshold_s:
                    continue
                child.inactivity_signal_sent = True
                signals.append(
                    {
                        "run_id": child.run_id,
                        "subagent": child.definition_name,
                        "last_event_age_s": round(age_s, 3),
                        "step": int(getattr(child.subagent_surface, "steps_completed", 0) or 0),
                    }
                )
        inbox = self.parent_steer_inbox
        for payload in signals:
            child = self._children.get(str(payload["run_id"]))
            launch_context = child.launcher if child is not None else self.launcher
            launch_context.store.append("subagent_inactivity_signal", payload)
            if inbox is not None:
                inbox.signal_waiters(
                    reason="child_inactive",
                    run_id=str(payload["run_id"]),
                )

    def set_lifecycle_listener(
        self,
        listener: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Install an optional observer for child state/collection changes."""
        with self._lock:
            self._lifecycle_listener = listener
            self._lifecycle_last.clear()

    @property
    def lifecycle_listener(self) -> Callable[[dict[str, Any]], None] | None:
        """Return the installed observer so a live tool rebuild can preserve it."""
        with self._lock:
            return self._lifecycle_listener

    def _notify_lifecycle(self, run_id: str) -> None:
        with self._lock:
            child = self._children.get(run_id)
            record = self.registry.get(run_id)
            if child is not None:
                child.last_event_monotonic = perf_counter()
                child.last_event_kind = "lifecycle"
            listener = self._lifecycle_listener
            if listener is None or child is None or record is None:
                return
            outcome = self._lifecycle_outcome(child)
            signature = (str(record.state), bool(child.collected), outcome)
            if self._lifecycle_last.get(run_id) == signature:
                return
            self._lifecycle_last[run_id] = signature
            payload = {
                "run_id": run_id,
                "subagent": child.definition_name,
                "label": child.label,
                "state": record.state,
                "collected": bool(child.collected),
            }
            if outcome:
                payload["outcome"] = outcome
        try:
            listener(payload)
        except Exception:
            pass

    def track_synchronous_child(
        self,
        *,
        run_id: str,
        definition_name: str,
        args: dict[str, Any],
        launcher: SubagentLauncher | None = None,
        cancellation_token: Any | None = None,
    ) -> None:
        """Expose an already-registered synchronous run through scheduler tools."""
        launch_context = launcher or self.launcher
        child = _ScheduledChild(
            launcher=launch_context,
            run_id=run_id,
            definition_name=definition_name,
            args={key: value for key, value in args.items() if isinstance(key, str)},
            label=subagent_task_label(
                args.get("task"),
                requested_run_id=args.get("run_id"),
            ),
            cancellation_token=(cancellation_token or _ChildCancellationToken()),
            completion=Future(),
            usage_lock=RLock(),
            background=False,
        )
        with self._lock:
            if run_id in self._children:
                raise ValueError(f"Child run already tracked: {run_id}")
            self._children[run_id] = child
            self._track_completion_clock(child)

    def run_foreground(
        self,
        args: dict[str, Any],
        *,
        launcher: SubagentLauncher | None = None,
    ) -> dict[str, Any]:
        """Run one direct child through the session-owned lifecycle engine.

        Return the synchronous outcome with its stable run ID so the parent can
        follow up using the same child context. Raised exceptions stay unchanged.
        """

        launch_context = launcher or self.launcher
        parent_cancellation_token = args.get(_SUBAGENT_CANCELLATION_TOKEN_ARG)
        requested_run_id = str(args.get(_SUBAGENT_PREASSIGNED_RUN_ID_ARG) or "").strip()
        dispatch_args = {key: value for key, value in args.items() if isinstance(key, str)}
        raw_name = str(dispatch_args.get("name") or "").strip()
        definition = launch_context._resolve_subagent_definition(raw_name)
        definition_name = definition.name if definition is not None else raw_name
        deadline_snapshot: dict[str, Any] = {}
        if launch_context.cfg is not None:
            deadline_snapshot = derive_subagent_deadline(
                launch_context.execution_deadline,
                launch_context.cfg.subagent_timeout_s,
            ).telemetry_snapshot()
        child = _ScheduledChild(
            launcher=launch_context,
            run_id=requested_run_id or uuid.uuid4().hex,
            definition_name=definition_name,
            args=dispatch_args,
            label=subagent_task_label(dispatch_args.get("task")),
            cancellation_token=_ChildCancellationToken(parent=parent_cancellation_token),
            completion=Future(),
            usage_lock=RLock(),
            background=False,
        )
        # Direct calls use a one-shot worker: the coordinator can observe
        # parent cancellation while the provider is blocked, and callers that
        # only retain a build_tools result do not accumulate a persistent
        # foreground executor after the synchronous call returns.
        foreground_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="subagent-foreground",
        )
        with self._lock:
            if self._closed:
                foreground_executor.shutdown(wait=False, cancel_futures=True)
                return {"error": "Subagent coordinator is closed."}
            if child.run_id in self._children:
                foreground_executor.shutdown(wait=False, cancel_futures=True)
                return {
                    "error": f"Subagent run already exists: {child.run_id}",
                    "error_code": "duplicate_subagent_run_id",
                }
            self._record_registered_child(
                run_id=child.run_id,
                definition_name=definition_name,
                deadline_snapshot=deadline_snapshot,
                launcher=launch_context,
            )
            self._children[child.run_id] = child
            self._track_completion_clock(child)
            child.executor_future = foreground_executor.submit(
                self._execute_child,
                child,
            )
            child.executor_future.add_done_callback(
                lambda future, child_run_id=child.run_id: self._worker_done(
                    child_run_id,
                    background=False,
                    worker_future=future,
                )
            )

        try:
            while not (
                child.completion.done()
                and child.executor_future is not None
                and child.executor_future.done()
            ):
                if bool(getattr(parent_cancellation_token, "is_cancelled", False)):
                    child.cancellation_token.cancel()
                # This is only an observation interval; it never terminates the
                # child. The child owns its configured deadline, if any.
                observed_future = (
                    child.executor_future
                    if child.completion.done() and child.executor_future is not None
                    else child.completion
                )
                wait((observed_future,), timeout=0.05)
        except BaseException:
            child.cancellation_token.cancel()
            foreground_executor.shutdown(wait=False, cancel_futures=True)
            raise
        foreground_executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._complete_child_state_locked(child)
            child.collected = True
        self._notify_lifecycle(child.run_id)
        return {**child.completion.result(), "run_id": child.run_id}

    def attach_synchronous_session(
        self,
        *,
        run_id: str,
        sub_session: Any,
        subagent_surface: NestedSubagentSurface,
    ) -> None:
        self._on_session_started(run_id, sub_session, subagent_surface)

    def complete_synchronous_child(
        self,
        *,
        run_id: str,
        result: dict[str, Any] | None,
        error: BaseException | None,
    ) -> None:
        completed = False
        with self._lock:
            child = self._children.get(run_id)
            if child is None or child.completion.done():
                return
            if error is not None:
                child.completion.set_exception(error)
                completed = True
            elif result is not None:
                child.completion.set_result(result)
                completed = True
            if completed:
                child.collected = True
        if completed:
            self._notify_lifecycle(run_id)

    def _record_registered_child(
        self,
        *,
        run_id: str,
        definition_name: str,
        deadline_snapshot: dict[str, Any],
        depends_on: tuple[str, ...] = (),
        resumed_from: str | None = None,
        launcher: SubagentLauncher | None = None,
    ) -> None:
        record = self.registry.register(
            run_id=run_id,
            definition_name=definition_name,
            started_monotonic=perf_counter(),
            deadline_snapshot=deadline_snapshot,
            depends_on=depends_on,
            resumed_from=resumed_from,
        )
        (launcher or self.launcher)._record_child_run_state(run_id=run_id, record=record)

    def _transition(
        self,
        run_id: str,
        state: ChildRunState,
        *,
        child_session_id: str | None = None,
    ) -> ChildRunRecord:
        child = self._children.get(run_id)
        launch_context = child.launcher if child is not None else self.launcher
        record = launch_context._transition_child_run(
            run_id=run_id,
            state=state,
            child_session_id=child_session_id,
        )
        self._notify_lifecycle(run_id)
        return record

    def _on_session_started(
        self,
        run_id: str,
        sub_session: Any,
        subagent_surface: NestedSubagentSurface,
    ) -> None:
        with self._lock:
            child = self._children.get(run_id)
            if child is None:
                return
            child.sub_session = sub_session
            child.subagent_surface = subagent_surface
            child.last_event_monotonic = perf_counter()
            subagent_surface.set_event_observer(
                lambda kind, scheduled_child=child: self._record_surface_event(
                    scheduled_child,
                    kind,
                )
            )

    @staticmethod
    def _record_surface_event(child: _ScheduledChild, kind: str) -> None:
        child.last_event_monotonic = perf_counter()
        child.last_event_kind = str(kind or "activity")

    @staticmethod
    def _wait_decision_message(*, pending_count: int, interrupted: bool) -> str:
        subject = "a selected child" if pending_count == 1 else "selected children"
        opening = (
            f"Wait was interrupted while {subject} " if interrupted else f"{subject.capitalize()} "
        )
        verb = "is" if pending_count == 1 else "are"
        return (
            f"{opening}{verb} still running. Decide whether to wait again with a "
            "timeout, check status, steer the child, cancel it, or synthesize now "
            "from completed results while disclosing the gap. Cancellation preserves "
            "any screened partial evidence and artifact already produced."
        )

    def _pending_children_payload(
        self,
        children: list[_ScheduledChild],
    ) -> list[dict[str, Any]]:
        now = perf_counter()
        payload: list[dict[str, Any]] = []
        with self._lock:
            for child in children:
                if child.completion.done():
                    continue
                record = self.registry.get(child.run_id)
                payload.append(
                    {
                        "run_id": child.run_id,
                        "subagent": child.definition_name,
                        "label": child.label,
                        "state": record.state if record is not None else "running",
                        "last_event_age_s": max(
                            0,
                            int(now - child.last_event_monotonic),
                        ),
                    }
                )
        return payload

    def _execute_child(self, child: _ScheduledChild) -> None:
        try:
            context = nullcontext() if child.background else self._batch_slots
            with context:
                result = child.launcher.run_registered(
                    child.args,
                    run_id=child.run_id,
                    cancellation_token=child.cancellation_token,
                    usage_lock=child.usage_lock,
                    parent_message_provider=child.drain_parent_messages,
                    parent_message_delivery_observer=(child.record_parent_message_delivery),
                    on_session_started=lambda session, surface: self._on_session_started(
                        child.run_id,
                        session,
                        surface,
                    ),
                    on_terminal_event=lambda: self._mark_launcher_terminal_event(child),
                    resume_context=child.resume_context,
                )
        except BaseException as exc:
            if child.cancellation_token.is_cancelled:
                with self._lock:
                    cancelled_result = self._queued_cancelled_result(child)
                    if child.launcher_terminal_event_emitted:
                        if not child.completion.done():
                            child.completion.set_result(cancelled_result)
                    else:
                        self._complete_synthetic_result_locked(
                            child,
                            cancelled_result,
                            source="execution_cancelled",
                        )
            elif not child.completion.done():
                child.completion.set_exception(exc)
        else:
            if not child.completion.done():
                if child.launcher_terminal_event_emitted:
                    child.completion.set_result(result)
                else:
                    with self._lock:
                        self._complete_synthetic_result_locked(
                            child,
                            result,
                            source="launcher_result_without_terminal_event",
                        )
        finally:
            self._release_resume_workspace_pin(child)

    def _mark_launcher_terminal_event(self, child: _ScheduledChild) -> None:
        # The launcher worker is the sole writer; readers only need to know
        # whether its terminal append happened before the launcher returned.
        child.launcher_terminal_event_emitted = True

    def _complete_synthetic_result_locked(
        self,
        child: _ScheduledChild,
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        """Durably record and complete one coordinator-fabricated result.

        Launcher-executed children already write their own terminal event, so
        this helper is intentionally reserved for work that never produced a
        normal launcher result (dependency rejection, queued cancellation, or
        executor cancellation).
        """

        if child.completion.done():
            return False
        terminal_payload = copy.deepcopy(result)
        terminal_payload.setdefault("name", child.definition_name)
        terminal_payload["run_id"] = child.run_id
        terminal_payload["coordinator_synthesized"] = True
        terminal_payload["terminal_source"] = source
        try:
            child.launcher.store.append("subagent_end", terminal_payload)
        except Exception:
            # Lifecycle persistence must not strand the completion future when
            # logging itself is unavailable during teardown.
            pass
        child.completion.set_result(result)
        return True

    def _release_resume_workspace_pin(self, child: _ScheduledChild) -> None:
        resume_context = child.resume_context
        provider = child.launcher.workspace_provider
        if resume_context is None or not resume_context.workspace_run_id or provider is None:
            return
        provider.release_pin(
            resume_context.workspace_run_id,
            consumer_run_id=child.run_id,
        )

    @staticmethod
    def _result_succeeded(result: dict[str, Any]) -> bool:
        status = str(result.get("status") or "").strip().lower()
        if status:
            return status in {"success", "no_changes"}
        return not bool(result.get("error"))

    def _complete_child_state_locked(self, child: _ScheduledChild) -> bool:
        if not child.completion.done():
            return False
        try:
            result = child.completion.result()
        except BaseException:
            record = self.registry.get(child.run_id)
            if record is not None and record.state not in {"joined", "cancelled"}:
                self._transition(child.run_id, "joined")
            return False
        self._finish_state_from_result(child=child, result=result)
        return self._result_succeeded(result)

    def _dependency_failure_result(
        self,
        *,
        child: _ScheduledChild,
        failed_dependency: str,
    ) -> dict[str, Any]:
        record = self.registry.get(child.run_id)
        elapsed_ms = (
            int((perf_counter() - record.started_monotonic) * 1000) if record is not None else 0
        )
        return {
            "error": f"Dependency did not succeed: {failed_dependency}",
            "subagent": child.definition_name,
            "subagent_session_id": None,
            "status": "cancelled",
            "failure_category": "dependency",
            "error_code": "dependency_failed",
            "failed_dependency": failed_dependency,
            "usage": {},
            "elapsed_ms": elapsed_ms,
            "steps_completed": 0,
            "effects": ["delegate", "read_workspace"],
            "touched_repo_paths": [],
        }

    def _deferred_launch_error_result(
        self,
        *,
        child: _ScheduledChild,
        error: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **error,
            "subagent": child.definition_name,
            "subagent_session_id": None,
            "status": "cancelled",
            "error_code": str(error.get("error_code") or "subagent_deferred_launch_rejected"),
            "usage": {},
            "steps_completed": int(error.get("steps_completed") or 0),
        }

    def _resolve_waiting_children_locked(self) -> None:
        made_progress = True
        while made_progress:
            made_progress = False
            for child in list(self._children.values()):
                record = self.registry.get(child.run_id)
                if record is None or record.state != "waiting":
                    continue
                failed_dependency: str | None = None
                all_succeeded = True
                for dependency_id in child.depends_on:
                    dependency = self._children.get(dependency_id)
                    if dependency is None or not dependency.completion.done():
                        all_succeeded = False
                        break
                    if not self._complete_child_state_locked(dependency):
                        failed_dependency = dependency_id
                        break
                if failed_dependency is not None:
                    self._complete_synthetic_result_locked(
                        child,
                        self._dependency_failure_result(
                            child=child,
                            failed_dependency=failed_dependency,
                        ),
                        source="dependency_failed",
                    )
                    self._transition(child.run_id, "cancelled")
                    made_progress = True
                    continue
                if not all_succeeded:
                    continue
                _preflight, launch_error = child.launcher.background_spawn_preflight(
                    child.args,
                    defer_dependency_workspace=False,
                )
                if launch_error is not None:
                    self._complete_synthetic_result_locked(
                        child,
                        self._deferred_launch_error_result(
                            child=child,
                            error=launch_error,
                        ),
                        source="deferred_launch_rejected",
                    )
                    self._transition(child.run_id, "cancelled")
                    made_progress = True
                    continue
                self._transition(child.run_id, "queued")
                self._background_queue.append(child.run_id)
                made_progress = True

    def _next_rollup_id_locked(self, prefix: str) -> str:
        self._rollup_sequence += 1
        return f"{prefix}-{self._rollup_sequence}"

    def _dependency_ancestors_locked(self, run_ids: tuple[str, ...]) -> set[str]:
        ancestors = set(run_ids)
        pending = list(run_ids)
        while pending:
            run_id = pending.pop()
            child = self._children.get(run_id)
            if child is None:
                continue
            for dependency_id in child.depends_on:
                if dependency_id not in ancestors:
                    ancestors.add(dependency_id)
                    pending.append(dependency_id)
        return ancestors

    def _register_chain_rollup_locked(self, child: _ScheduledChild) -> None:
        run_ids = self._dependency_ancestors_locked((child.run_id,))
        merge_ids = [
            rollup_id
            for rollup_id, rollup in self._rollups.items()
            if rollup.kind == "chain" and run_ids.intersection(rollup.run_ids)
        ]
        for rollup_id in merge_ids:
            run_ids.update(self._rollups.pop(rollup_id).run_ids)
        ordered_run_ids = tuple(run_id for run_id in self._children if run_id in run_ids)
        started = min(
            (
                record.started_monotonic
                for run_id in ordered_run_ids
                if (record := self.registry.get(run_id)) is not None
            ),
            default=perf_counter(),
        )
        self._rollups[self._next_rollup_id_locked("chain")] = _ChildRollup(
            run_ids=ordered_run_ids,
            started_monotonic=started,
            kind="chain",
        )

    @classmethod
    def _result_status(cls, child: _ScheduledChild) -> str:
        try:
            result = child.completion.result()
        except BaseException:
            return "failed"
        status = str(result.get("status") or "").strip().lower()
        if status:
            return status
        return "success" if cls._result_succeeded(result) else "failed"

    @classmethod
    def _lifecycle_outcome(cls, child: _ScheduledChild) -> str:
        """Return the user-facing terminal outcome once the child has completed."""
        if not child.completion.done():
            return ""
        status = cls._result_status(child)
        if status in {"success", "no_changes"}:
            return "finished"
        if status == "cancelled":
            return "cancelled"
        if status == "incomplete":
            return "incomplete"
        return "failed"

    def _rollup_usage_totals(self, run_ids: tuple[str, ...]) -> dict[str, int | float]:
        totals: dict[str, int | float] = {}
        for run_id in run_ids:
            child = self._children[run_id]
            try:
                result = child.completion.result()
            except BaseException:
                continue
            usage = result.get("usage")
            if not isinstance(usage, dict):
                continue
            for key, value in usage.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                totals[str(key)] = totals.get(str(key), 0) + value
        return totals

    @staticmethod
    def _child_workspace_view(child: _ScheduledChild) -> str:
        if str(child.args.get("workspace_from_run") or "").strip():
            return "pinned"
        return str(child.args.get("workspace_view") or "shared").strip().lower()

    def _emit_completed_rollups_locked(self) -> None:
        completed_ids = [
            rollup_id
            for rollup_id, rollup in self._rollups.items()
            if all(self._children[run_id].completion.done() for run_id in rollup.run_ids)
        ]
        for rollup_id in completed_ids:
            rollup = self._rollups.pop(rollup_id)
            self.launcher.store.append(
                "subagent_batch_summary",
                {
                    "run_ids": list(rollup.run_ids),
                    "statuses": [
                        self._result_status(self._children[run_id]) for run_id in rollup.run_ids
                    ],
                    "wall_ms": int((perf_counter() - rollup.started_monotonic) * 1000),
                    "usage_totals": self._rollup_usage_totals(rollup.run_ids),
                    "workspace_views": [
                        self._child_workspace_view(self._children[run_id])
                        for run_id in rollup.run_ids
                    ],
                },
            )

    def _child_done(self, run_id: str, *, background: bool) -> None:
        with self._lock:
            if background:
                self._active_background = max(0, self._active_background - 1)
            child = self._children.get(run_id)
            if child is not None:
                self._complete_child_state_locked(child)
            self._resolve_waiting_children_locked()
            self._emit_completed_rollups_locked()
            self._start_queued_background_locked()

    def _worker_done(
        self,
        run_id: str,
        *,
        background: bool,
        worker_future: Future[Any],
    ) -> None:
        with self._lock:
            child = self._children.get(run_id)
        try:
            if worker_future.cancelled():
                with self._lock:
                    if child is not None:
                        self._complete_synthetic_result_locked(
                            child,
                            self._queued_cancelled_result(child),
                            source="executor_cancelled",
                        )
                        self._release_resume_workspace_pin(child)
            self._child_done(run_id, background=background)
        finally:
            if child is not None and not child.worker_bookkeeping_completion.done():
                child.worker_bookkeeping_completion.set_result(None)

    def _submit_background_locked(self, child: _ScheduledChild) -> None:
        self._active_background += 1
        child.executor_future = self._executor.submit(self._execute_child, child)
        child.executor_future.add_done_callback(
            lambda future, run_id=child.run_id: self._worker_done(
                run_id,
                background=True,
                worker_future=future,
            )
        )

    def _start_queued_background_locked(self) -> None:
        while self._background_queue and self._active_background < self.max_background_children:
            run_id = self._background_queue.popleft()
            child = self._children.get(run_id)
            record = self.registry.get(run_id)
            if child is None or record is None or record.state == "cancelled":
                continue
            self._submit_background_locked(child)

    def _dependency_reaches_locked(self, start_run_id: str, target_run_id: str) -> bool:
        pending = [start_run_id]
        visited: set[str] = set()
        while pending:
            candidate = pending.pop()
            if candidate == target_run_id:
                return True
            if candidate in visited:
                continue
            visited.add(candidate)
            child = self._children.get(candidate)
            if child is not None:
                pending.extend(child.depends_on)
        return False

    def _validated_dependencies_locked(
        self,
        *,
        run_id: str,
        raw_dependencies: Any,
    ) -> tuple[tuple[str, ...], dict[str, Any] | None]:
        if raw_dependencies is None:
            return (), None
        if not isinstance(raw_dependencies, list):
            return (), {
                "error": "depends_on must be a list of background run ids.",
                "error_code": "invalid_subagent_dependencies",
            }
        dependencies = tuple(str(candidate).strip() for candidate in raw_dependencies)
        if any(not dependency for dependency in dependencies):
            return (), {
                "error": "depends_on entries must be non-empty background run ids.",
                "error_code": "invalid_subagent_dependencies",
            }
        if len(set(dependencies)) != len(dependencies):
            return (), {
                "error": "depends_on contains a duplicate run id.",
                "error_code": "duplicate_subagent_dependency",
            }
        if run_id in dependencies:
            return (), {
                "error": "A background subagent cannot depend on itself.",
                "error_code": "subagent_dependency_self_reference",
                "run_id": run_id,
            }
        for dependency_id in dependencies:
            if dependency_id not in self._children:
                return (), {
                    "error": f"Unknown background subagent run: {dependency_id}",
                    "error_code": "unknown_background_subagent_run",
                    "run_id": dependency_id,
                }
            if self._dependency_reaches_locked(dependency_id, run_id):
                return (), {
                    "error": "Subagent dependency would create a cycle.",
                    "error_code": "subagent_dependency_cycle",
                    "run_id": dependency_id,
                }
        return dependencies, None

    def spawn(
        self,
        args: dict[str, Any],
        *,
        parent_cancellation_token: Any | None = None,
    ) -> dict[str, Any]:
        requested_run_id = str(args.get("run_id") or "").strip()
        if (
            requested_run_id
            and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
                requested_run_id,
            )
            is None
        ):
            return {
                "error": ("run_id must be 1-64 ASCII letters, digits, underscores, or hyphens."),
                "error_code": "invalid_background_subagent_run_id",
            }
        run_id = requested_run_id or uuid.uuid4().hex
        with self._lock:
            if self._closed:
                return {"error": "Background subagent scheduler is closed."}
            launch_context = self.launcher
            if run_id in self._children:
                return {
                    "error": f"Background subagent run already exists: {run_id}",
                    "error_code": "duplicate_background_subagent_run_id",
                    "run_id": run_id,
                }
            dependencies, dependency_error = self._validated_dependencies_locked(
                run_id=run_id,
                raw_dependencies=args.get("depends_on"),
            )
        if dependency_error is not None:
            return dependency_error
        preflight, error = launch_context.background_spawn_preflight(args)
        if error is not None:
            return error
        if preflight is None:
            return {"error": "Background subagent preflight failed."}
        child = _ScheduledChild(
            launcher=launch_context,
            run_id=run_id,
            definition_name=preflight.definition.name,
            args=dict(args),
            label=subagent_task_label(
                args.get("task"),
                requested_run_id=requested_run_id,
            ),
            cancellation_token=_ChildCancellationToken(parent=parent_cancellation_token),
            completion=Future(),
            usage_lock=RLock(),
            background=True,
            depends_on=dependencies,
        )
        with self._lock:
            if self._closed:
                return {"error": "Background subagent scheduler is closed."}
            self._record_registered_child(
                run_id=run_id,
                definition_name=preflight.definition.name,
                deadline_snapshot=preflight.deadline_snapshot,
                depends_on=dependencies,
                launcher=launch_context,
            )
            self._children[run_id] = child
            self._track_completion_clock(child)
            if dependencies:
                self._transition(run_id, "waiting")
                self._register_chain_rollup_locked(child)
                self._resolve_waiting_children_locked()
                self._emit_completed_rollups_locked()
                self._start_queued_background_locked()
            elif self._active_background < self.max_background_children:
                self._submit_background_locked(child)
            else:
                self._transition(run_id, "queued")
                self._background_queue.append(run_id)
            record = self.registry.get(run_id)
            state = record.state if record is not None else "spawned"
            child_session_id = record.child_session_id if record is not None else None
            summary = _children_state_summary(
                [
                    {"state": candidate_record.state}
                    for candidate in self._children
                    if (candidate_record := self.registry.get(candidate)) is not None
                ]
            )
        return {
            "run_id": run_id,
            "subagent": preflight.definition.name,
            "label": child.label,
            "subagent_session_id": child_session_id,
            "state": state,
            "summary": summary,
            "orchestration_note": _BACKGROUND_OWNERSHIP_NOTE,
        }

    def _resume_source_events(self, child: _ScheduledChild) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        child_store = getattr(getattr(child.sub_session, "store", None), "events_snapshot", None)
        if callable(child_store):
            try:
                snapshot = child_store()
                if isinstance(snapshot, list):
                    events = [event for event in snapshot if isinstance(event, dict)]
            except Exception:  # noqa: BLE001 - history can fall back to in-memory messages
                events = []
        record = self.registry.get(child.run_id)
        if not events and record is not None and record.child_session_id:
            log_path = child.launcher.store.sessions_dir / f"{record.child_session_id}.jsonl"
            try:
                events = [event for event in read_session_events(log_path)]
            except (OSError, UnicodeDecodeError, ValueError):
                events = []
        return events

    def _resume_source_session_id(self, child: _ScheduledChild) -> str | None:
        record = self.registry.get(child.run_id)
        recorded = str(getattr(record, "child_session_id", "") or "").strip()
        if recorded:
            return recorded
        live = str(getattr(getattr(child.sub_session, "store", None), "session_id", "") or "")
        return live.strip() or None

    def _resume_history(
        self,
        child: _ScheduledChild,
        events: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if events is None:
            events = self._resume_source_events(child)
        restored = _child_resume_messages(events)
        if not restored:
            session_messages = getattr(child.sub_session, "messages", [])
            prefix_len = getattr(child.sub_session, "pinned_prefix_len", 0)
            if not isinstance(prefix_len, int) or prefix_len < 0:
                prefix_len = 0
            restored = [
                copy.deepcopy(message)
                for message in session_messages[prefix_len:]
                if isinstance(message, dict)
                and str(message.get("role") or "").strip() in {"user", "assistant", "tool"}
            ]
            if child.resume_context is not None:
                retained = list(child.resume_context.history_messages)
                if restored[: len(retained)] == retained:
                    return tuple(restored)
        if child.resume_context is not None:
            # A fresh child log contains only its new turn. Carry prior history
            # forward once, including when initialization fails before history
            # can be seeded into the new session.
            restored = [*copy.deepcopy(child.resume_context.history_messages), *restored]
        return tuple(restored)

    @staticmethod
    def _resumable_status(
        *,
        record: ChildRunRecord,
        result: dict[str, Any],
    ) -> str:
        raw_status = str(result.get("status") or "").strip().lower()
        if record.state == "cancelled" or raw_status == "cancelled":
            return "cancelled"
        if (
            raw_status == "incomplete"
            or str(result.get("error_code") or "") == SUBAGENT_INCOMPLETE_ERROR_CODE
        ):
            return "incomplete"
        if raw_status:
            return raw_status
        return "failed" if result.get("error") else "success"

    def resume(
        self,
        args: dict[str, Any],
        *,
        parent_cancellation_token: Any | None = None,
    ) -> dict[str, Any]:
        source_run_id = str(args.get("run_id") or "").strip()
        task_override = str(args.get("task") or "").strip()
        with self._lock:
            source = self._children.get(source_run_id)
            source_record = self.registry.get(source_run_id)
            if source is None or source_record is None:
                return {
                    "error": f"Unknown background subagent run: {source_run_id}",
                    "error_code": "unknown_background_subagent_run",
                    "run_id": source_run_id,
                }
            if source_record.state not in {"joined", "cancelled"} or not source.completion.done():
                return {
                    "error": f"Background subagent run is not terminal: {source_run_id}",
                    "error_code": "subagent_resume_requires_terminal",
                    "run_id": source_run_id,
                    "state": source_record.state,
                }
            try:
                source_result = source.completion.result()
            except BaseException as exc:
                source_result = {"error": str(exc), "status": "failed"}
            source_status = self._resumable_status(
                record=source_record,
                result=source_result,
            )
            if source_status not in {
                "success",
                "no_changes",
                "degraded",
                "failed",
                "incomplete",
                "cancelled",
            }:
                return {
                    "error": (
                        "Only terminal successful, degraded, failed, incomplete, or cancelled runs can be continued."
                    ),
                    "error_code": "subagent_resume_not_allowed",
                    "run_id": source_run_id,
                    "status": source_status,
                }
            if source.continuation_run_id:
                return self._already_continued_result(source)
            if source_status in {"success", "no_changes", "degraded"} and not task_override:
                return {
                    "error": "A successful or degraded child needs an explicit follow-up task.",
                    "error_code": "subagent_followup_requires_task",
                    "run_id": source_run_id,
                    "status": source_status,
                }

        resumed_args = _resume_launch_args(source.args)
        if task_override:
            resumed_args["task"] = task_override
        workspace_view_override = str(args.get("workspace_view") or "").strip().lower()
        if workspace_view_override:
            resumed_args["workspace_view"] = workspace_view_override
        original_workspace_view = self._child_workspace_view(source)

        provider = self.launcher.workspace_provider
        workspace_record = provider.get(source_run_id) if provider is not None else None
        patch_artifact = ""
        patch_summary = source_result.get("patch_summary")
        if isinstance(patch_summary, dict):
            patch_artifact = str(patch_summary.get("patch_artifact") or "")
        if not patch_artifact and workspace_record is not None:
            patch_artifact = workspace_record.patch_artifact

        reattach_requested = bool(args.get("reattach_workspace", True))
        workspace_run_id: str | None = None
        source_workspace = source_result.get("workspace")
        if (
            reattach_requested
            and original_workspace_view == "isolated"
            and workspace_record is None
            and isinstance(source_workspace, dict)
            and source_workspace.get("view") == "isolated"
        ):
            return {
                "error": "The source isolated workspace record is unavailable; refusing to silently start from a fresh tree.",
                "error_code": "subagent_resume_worktree_unavailable",
                "run_id": source_run_id,
                "patch_artifact": patch_artifact or None,
            }
        if original_workspace_view == "isolated" and workspace_record is not None:
            if reattach_requested and workspace_record.state in {
                "applied",
                "discarded",
                "duplicate",
            }:
                return {
                    "error": (
                        f"Isolated workspace {source_run_id} was already {workspace_record.state}."
                    ),
                    "error_code": "subagent_resume_worktree_released",
                    "run_id": source_run_id,
                    "workspace_state": workspace_record.state,
                    "patch_artifact": patch_artifact or None,
                }
            if reattach_requested:
                workspace_run_id = source_run_id

        with self._lock:
            launch_context = self.launcher
        preflight, preflight_error = launch_context.background_spawn_preflight(resumed_args)
        if preflight_error is not None:
            if preflight_error.get("error_code") == "background_subagent_requires_readonly":
                isolated_args = {**resumed_args, "workspace_view": "isolated"}
                isolated_preflight, _isolated_error = launch_context.background_spawn_preflight(
                    isolated_args,
                    check_deadline=False,
                )
                report_artifact = str(source_result.get("report_artifact") or "").strip()
                if isolated_preflight is not None:
                    return {
                        **preflight_error,
                        "error": (
                            f"{preflight_error['error']} This resume can instead use "
                            f"subagent_resume(run_id={source_run_id}, "
                            'workspace_view="isolated"). A fresh synchronous '
                            "subagent_run is also valid."
                        ),
                        "run_id": source_run_id,
                        "resume_alternative": {
                            "tool": "subagent_resume",
                            "run_id": source_run_id,
                            "workspace_view": "isolated",
                        },
                        "fresh_run_report_artifact": report_artifact or None,
                    }
            return preflight_error
        if preflight is None:
            return {"error": "Background subagent resume preflight failed."}

        new_run_id = uuid.uuid4().hex
        source_events = self._resume_source_events(source)
        source_session_id = self._resume_source_session_id(source)
        resume_context = _ChildResumeContext(
            resumed_from=source_run_id,
            history_messages=self._resume_history(source, source_events),
            provider_session_id=(
                getattr(source.sub_session, "provider_session_id", None)
                or (
                    source.resume_context.provider_session_id
                    if source.resume_context is not None
                    else None
                )
                or getattr(getattr(source.sub_session, "store", None), "session_id", None)
            ),
            workspace_run_id=workspace_run_id,
            patch_artifact=patch_artifact,
            evidence_events=tuple(source_events),
            source_session_id=source_session_id,
            read_ledger_snapshot=(
                source.sub_session.read_ledger.snapshot()
                if callable(
                    getattr(getattr(source.sub_session, "read_ledger", None), "snapshot", None)
                )
                else (
                    copy.deepcopy(source.resume_context.read_ledger_snapshot)
                    if source.resume_context is not None
                    else {}
                )
            ),
        )
        with self._lock:
            if self._closed:
                return {"error": "Background subagent scheduler is closed."}
            # A continuation is linear, not a fork. Check again after preflight
            # so concurrent requests cannot transfer the same retained candidate
            # twice or silently restart from a fresh tree after it has moved.
            if source.continuation_run_id:
                return self._already_continued_result(source)
            child = _ScheduledChild(
                launcher=launch_context,
                run_id=new_run_id,
                definition_name=preflight.definition.name,
                args=resumed_args,
                label=subagent_task_label(resumed_args.get("task")),
                cancellation_token=_ChildCancellationToken(parent=parent_cancellation_token),
                completion=Future(),
                usage_lock=RLock(),
                background=True,
                resume_context=resume_context,
            )
            try:
                if workspace_run_id and provider is not None:
                    reattached = provider.reattach_for_resume(workspace_run_id, new_run_id)
                    if not bool(reattached.get("ok")):
                        child.cancellation_token.close()
                        return {**reattached, "resumed_from": source_run_id}
                    child.resume_context = replace(resume_context, workspace_run_id=new_run_id)
                self._record_registered_child(
                    run_id=new_run_id,
                    definition_name=preflight.definition.name,
                    deadline_snapshot=preflight.deadline_snapshot,
                    resumed_from=source_run_id,
                    launcher=launch_context,
                )
            except Exception as exc:
                # Neither workspace transfer nor durable registration launches a
                # worker. Restore ownership before allowing a retry of the source.
                rollback_error = ""
                if (
                    workspace_run_id
                    and provider is not None
                    and provider.get(new_run_id) is not None
                ):
                    try:
                        rolled_back = provider.reattach_for_resume(new_run_id, source_run_id)
                        if not rolled_back.get("ok"):
                            rollback_error = str(
                                rolled_back.get("error") or "Workspace rollback failed."
                            )
                    except Exception as rollback_exc:
                        # An event append can fail after an in-memory transfer;
                        # inspect ownership instead of assuming work was lost.
                        rollback_error = str(rollback_exc)
                child.cancellation_token.close()
                failed_record = self.registry.get(new_run_id)
                if failed_record is not None and failed_record.state == "spawned":
                    # The durable append just failed, so do not retry that same
                    # sink while containing the unlaunched registration.
                    self.registry.transition(new_run_id, state="cancelled")
                retained_id = (
                    source_run_id
                    if provider is not None and provider.get(source_run_id) is not None
                    else new_run_id
                    if provider is not None and provider.get(new_run_id) is not None
                    else None
                )
                return {
                    "error": f"Subagent continuation setup failed: {sanitize_subagent_report(str(exc)).text}",
                    "error_code": "subagent_resume_setup_failed",
                    "run_id": source_run_id,
                    "retained_worktree_run_id": retained_id,
                    "source_workspace_restored": retained_id == source_run_id,
                    **(
                        {"workspace_rollback_error": sanitize_subagent_report(rollback_error).text}
                        if rollback_error
                        else {}
                    ),
                }
            self._children[new_run_id] = child
            source.continuation_run_id = new_run_id
            self._track_completion_clock(child)
            if self._active_background < self.max_background_children:
                self._submit_background_locked(child)
            else:
                self._transition(new_run_id, "queued")
                self._background_queue.append(new_run_id)
            record = self.registry.get(new_run_id)
        return {
            "run_id": new_run_id,
            "subagent": preflight.definition.name,
            "label": child.label,
            "subagent_session_id": (record.child_session_id if record is not None else None),
            "state": record.state if record is not None else "spawned",
            "resumed_from": source_run_id,
            "orchestration_note": _BACKGROUND_OWNERSHIP_NOTE,
        }

    @staticmethod
    def _already_continued_result(source: _ScheduledChild) -> dict[str, Any]:
        return {
            "error": "This child already has a continuation; use its current run ID.",
            "error_code": "subagent_already_continued",
            "run_id": source.run_id,
            "continuation_run_id": source.continuation_run_id,
        }

    def _register_batch_child(
        self,
        args: dict[str, Any],
        *,
        parent_cancellation_token: Any | None,
    ) -> _ScheduledChild:
        launch_context = self.launcher
        raw_name = str(args.get("name") or "").strip()
        definition = launch_context._resolve_subagent_definition(raw_name)
        definition_name = definition.name if definition is not None else raw_name
        run_id = uuid.uuid4().hex
        deadline_snapshot: dict[str, Any] = {}
        if launch_context.cfg is not None:
            deadline_snapshot = derive_subagent_deadline(
                launch_context.execution_deadline,
                launch_context.cfg.subagent_timeout_s,
            ).telemetry_snapshot()
        child = _ScheduledChild(
            launcher=launch_context,
            run_id=run_id,
            definition_name=definition_name,
            args=dict(args),
            label=subagent_task_label(args.get("task")),
            cancellation_token=_ChildCancellationToken(parent=parent_cancellation_token),
            completion=Future(),
            usage_lock=RLock(),
            background=False,
        )
        self._record_registered_child(
            run_id=run_id,
            definition_name=definition_name,
            deadline_snapshot=deadline_snapshot,
            launcher=launch_context,
        )
        self._children[run_id] = child
        self._track_completion_clock(child)
        child.executor_future = self._executor.submit(self._execute_child, child)
        child.executor_future.add_done_callback(
            lambda future, child_run_id=run_id: self._worker_done(
                child_run_id,
                background=False,
                worker_future=future,
            )
        )
        return child

    def run_readonly_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        parent_cancellation_token: Any | None = None,
    ) -> list[dict[str, Any]]:
        run_ids = self.submit_parallel_batch(
            calls,
            parent_cancellation_token=parent_cancellation_token,
        )
        results: list[dict[str, Any]] = []
        for index, run_id in enumerate(run_ids):
            collected = self.collect(run_id=run_id, timeout_s=None)
            if collected.get("wait_interrupted") is True:
                for pending_run_id in run_ids[index:]:
                    results.append(
                        {
                            "status": "running",
                            "wait_interrupted": True,
                            "wake_reason": str(collected.get("wake_reason") or "parent_wake"),
                            "wake_reasons": list(collected.get("wake_reasons") or []),
                            "run_id": pending_run_id,
                            "pending_run_ids": [pending_run_id],
                            "message": str(collected.get("message") or "Wait interrupted."),
                            **(
                                {"wake_run_id": str(collected["wake_run_id"])}
                                if collected.get("wake_run_id")
                                else {}
                            ),
                        }
                    )
                break
            result = collected["results"][run_id]
            results.append({**result, "run_id": run_id})
        return results

    def submit_readonly_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        parent_cancellation_token: Any | None = None,
    ) -> list[str]:
        return self.submit_parallel_batch(
            calls,
            parent_cancellation_token=parent_cancellation_token,
        )

    def submit_parallel_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        parent_cancellation_token: Any | None = None,
    ) -> list[str]:
        batch_started = perf_counter()
        with self._lock:
            if self._closed:
                return []
            children = [
                self._register_batch_child(
                    args,
                    parent_cancellation_token=parent_cancellation_token,
                )
                for args in calls
            ]
            if len(children) > 1:
                self._rollups[self._next_rollup_id_locked("batch")] = _ChildRollup(
                    run_ids=tuple(child.run_id for child in children),
                    started_monotonic=batch_started,
                    kind="batch",
                )
                self._emit_completed_rollups_locked()
        return [child.run_id for child in children]

    def _selected_run_ids(self, run_id: str | list[str] | None) -> list[str]:
        with self._lock:
            if run_id is None or run_id == "all":
                return list(self._children)
            requested = [run_id] if isinstance(run_id, str) else list(run_id)
            return [candidate for candidate in requested if candidate in self._children]

    def _refresh_usage(self, child: _ScheduledChild) -> None:
        if child.sub_session is None:
            return
        try:
            child.launcher.replay_registered_usage(
                run_id=child.run_id,
                sub_session=child.sub_session,
                usage_lock=child.usage_lock,
            )
        except Exception as exc:  # noqa: BLE001 - status must remain best-effort
            child.launcher.store.append(
                "warning",
                {
                    "warning": "subagent_usage_replay_failed",
                    "name": child.definition_name,
                    "subagent_session_id": (
                        self.registry.get(child.run_id).child_session_id
                        if self.registry.get(child.run_id) is not None
                        else None
                    ),
                    "error": str(exc),
                },
            )

    def _finish_state_from_result(
        self,
        *,
        child: _ScheduledChild,
        result: dict[str, Any],
    ) -> None:
        record = self.registry.get(child.run_id)
        if record is None or record.state in {"joined", "cancelled"}:
            return
        terminal_state: ChildRunState = (
            "cancelled" if str(result.get("status") or "") == "cancelled" else "joined"
        )
        self._transition(
            child.run_id,
            terminal_state,
            child_session_id=str(result.get("subagent_session_id") or "") or None,
        )

    @staticmethod
    def _completed_child_result(child: _ScheduledChild) -> dict[str, Any]:
        try:
            return child.completion.result()
        except BaseException as exc:
            return {
                "status": "failed",
                "subagent": child.definition_name,
                "error_code": "subagent_execution_failed",
                "error": sanitize_subagent_report(str(exc)).text,
            }

    def pending_completion_notifications(self, *, max_items: int = 8) -> list[dict[str, Any]]:
        """Peek at finished background results without consuming their delivery.

        The parent persists these bounded, untrusted reports at a safe model
        boundary, then acknowledges the returned run IDs. A failed append can
        retry this read without losing a result. Explicit wait/collection also
        satisfies delivery; the full result remains retrievable in either case.
        Only the session's parent turn consumes notifications.
        """
        limit = max(0, int(max_items))
        with self._lock:
            children = [
                child
                for child in self._children.values()
                if child.background and not child.collected and child.completion.done()
            ][:limit]
        notifications: list[dict[str, Any]] = []
        for child in children:
            self._refresh_usage(child)
            result = self._completed_child_result(child)
            partial = result.get("partial_report")
            report_text = str(result.get("result") or result.get("final_text") or "")
            if not report_text and isinstance(partial, dict):
                report_text = str(partial.get("excerpt") or "")
            report = sanitize_subagent_report(report_text)
            notification: dict[str, Any] = {
                "run_id": child.run_id,
                "subagent": child.definition_name,
                "status": self._result_status(child),
                "status_scope": "child_execution",
                "report": report.text[:_COMPLETION_NOTIFICATION_REPORT_MAX_CHARS],
                "report_truncated": (
                    len(report.text) > _COMPLETION_NOTIFICATION_REPORT_MAX_CHARS
                    or (isinstance(partial, dict) and bool(partial.get("truncated")))
                ),
                "report_safety": report.metadata(),
                "full_result": {
                    "tool": "subagent_wait",
                    "arguments": {"run_id": child.run_id},
                },
            }
            for key in ("error", "error_code", "stop_reason", "report_artifact", "resumed_from"):
                value = result.get(key)
                if value:
                    notification[key] = sanitize_subagent_report(str(value)).text[:1000]
            workspace = result.get("workspace")
            if isinstance(workspace, dict):
                notification["workspace_view"] = workspace.get("view")
                notification["candidate_worktree_retained"] = bool(
                    result.get("candidate_worktree_retained")
                    or workspace.get("candidate_worktree_retained")
                )
            with self._lock:
                if not child.collected:
                    notifications.append(notification)
        return notifications

    def acknowledge_completion_notifications(self, run_ids: list[str]) -> None:
        """Acknowledge reports already persisted in the parent's transcript."""
        acknowledged: list[str] = []
        with self._lock:
            for run_id in dict.fromkeys(run_ids):
                child = self._children.get(run_id)
                if child is None or child.collected or not child.completion.done():
                    continue
                result = self._completed_child_result(child)
                self._finish_state_from_result(child=child, result=result)
                child.collected = True
                acknowledged.append(run_id)
        for run_id in acknowledged:
            self._notify_lifecycle(run_id)

    def collect(
        self,
        *,
        run_id: str | list[str] | None = "all",
        timeout_s: float | None = None,
        cancellation_token: Any | None = None,
        consume_delivery: bool = True,
    ) -> dict[str, Any]:
        """Join selected children, optionally leaving their reports pending delivery.

        Explicit waits return reports to the parent and consume delivery by default.
        Host cleanup instead persists completion notifications before acknowledging
        them, so a failed transcript write can retry without losing the reports.
        """
        selected = self._selected_run_ids(run_id)
        if isinstance(run_id, str) and run_id != "all" and not selected:
            return {
                "error": f"Unknown background subagent run: {run_id}",
                "error_code": "unknown_background_subagent_run",
                "run_id": run_id,
            }
        children = [self._children[candidate] for candidate in selected]
        for child in children:
            self._refresh_usage(child)
        pending_futures = [child.completion for child in children if not child.completion.done()]
        wait_signals: list[dict[str, str]] = []
        if pending_futures:
            wait_deadline = (
                None if timeout_s is None else perf_counter() + max(0.0, float(timeout_s))
            )
            remaining_futures = set(pending_futures)
            while remaining_futures:
                if bool(getattr(cancellation_token, "is_cancelled", False)):
                    self.cancel_parent_turn(
                        parent_cancellation_token=cancellation_token,
                        wait_for_running=True,
                    )
                    # Cooperative children now have terminal cancellation results.
                    # Return those to the turn loop so every assistant tool call is
                    # paired with a tool result before its next cancellation
                    # checkpoint raises at the parent boundary.
                    break
                self._signal_inactive_children(children)
                wait_signals = self._consume_parent_wait_signals()
                if wait_signals:
                    break
                remaining_timeout = (
                    None if wait_deadline is None else max(0.0, wait_deadline - perf_counter())
                )
                if remaining_timeout == 0.0:
                    break
                completed, remaining_futures = wait(
                    remaining_futures,
                    timeout=(0.05 if remaining_timeout is None else min(0.05, remaining_timeout)),
                )
                if completed:
                    continue
        results: dict[str, dict[str, Any]] = {}
        pending_run_ids: list[str] = []
        for child in children:
            self._refresh_usage(child)
            if not child.completion.done():
                pending_run_ids.append(child.run_id)
                continue
            result = self._completed_child_result(child)
            if consume_delivery:
                with self._lock:
                    self._finish_state_from_result(child=child, result=result)
                    child.collected = True
                self._notify_lifecycle(child.run_id)
            results[child.run_id] = {"run_id": child.run_id, **result}
        payload: dict[str, Any] = {
            "results": results,
            "pending_run_ids": pending_run_ids,
            "pending_children": self._pending_children_payload(children),
            "wait_pending": bool(pending_run_ids),
            "summary": self.status(run_id=selected)["summary"],
            "message": (
                self._wait_decision_message(
                    pending_count=len(pending_run_ids),
                    interrupted=False,
                )
                if pending_run_ids
                else (
                    "All selected background children are joined."
                    if consume_delivery
                    else "All selected background children completed; reports await delivery."
                )
            ),
        }
        if wait_signals and pending_run_ids:
            payload.update(
                {
                    "status": "running",
                    "wait_interrupted": True,
                    "selected_run_ids": list(selected),
                    "message": self._wait_decision_message(
                        pending_count=len(pending_run_ids),
                        interrupted=True,
                    ),
                    **wait_signal_digest(wait_signals),
                }
            )
            if len(selected) == 1:
                payload["run_id"] = selected[0]
        return payload

    def send(self, *, run_id: str, message: str) -> dict[str, Any]:
        if len(message) > 4_000:
            return {
                "error": "Subagent message exceeds the 4,000 character limit.",
                "error_code": "subagent_message_too_large",
                "run_id": run_id,
                "max_chars": 4_000,
            }
        with self._lock:
            child = self._children.get(run_id)
            record = self.registry.get(run_id)
            if child is None or record is None:
                return {
                    "error": f"Unknown background subagent run: {run_id}",
                    "error_code": "unknown_background_subagent_run",
                    "run_id": run_id,
                }
            if record.state in {"joined", "cancelled"} or child.completion.done():
                return {
                    "error": f"Background subagent run is not running: {run_id}",
                    "error_code": "subagent_not_running",
                    "run_id": run_id,
                    "state": record.state,
                }
            with child.inbox_lock:
                child.inbox.append(message)
                child.messages_queued += 1
            state_at_send = record.state
            subagent_surface = child.subagent_surface
        child.launcher.store.append(
            "subagent_message",
            {
                "run_id": run_id,
                "chars": len(message),
                "state_at_send": state_at_send,
            },
        )
        emit_info = getattr(subagent_surface or child.launcher.surface, "emit_info", None)
        if callable(emit_info):
            if subagent_surface is not None:
                emit_info("Message sent.")
            else:
                emit_info(
                    "Message sent.",
                    worker_id=child.definition_name,
                    role="subagent",
                )
        return {
            "run_id": run_id,
            "state": state_at_send,
            "chars": len(message),
            "message_sent": True,
        }

    def _queued_cancelled_result(self, child: _ScheduledChild) -> dict[str, Any]:
        record = self.registry.get(child.run_id)
        elapsed_ms = (
            int((perf_counter() - record.started_monotonic) * 1000) if record is not None else 0
        )
        return {
            "error": "Subagent cancelled by the parent turn.",
            "subagent": child.definition_name,
            "subagent_session_id": None,
            "status": "cancelled",
            "failure_category": "cancelled",
            "error_code": "subagent_cancelled",
            "exit_code": 1,
            "usage": {},
            "elapsed_ms": elapsed_ms,
            "steps_completed": 0,
            "partial_report": _screen_partial_subagent_report(""),
            "effects": ["delegate", "read_workspace"],
            "touched_repo_paths": [],
        }

    def cancel(
        self,
        *,
        run_id: str | list[str] | None = "all",
        wait_for_running: bool = True,
        wait_timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Cancel children, optionally joining the ones already running.

        ``wait_timeout_s`` bounds that join. ``None`` uses a short default
        bound, so cooperative cleanup gets a chance to finish without ever
        turning cancellation into an unbounded wait. Callers that need an
        immediate return pass ``wait_for_running=False``.
        """
        with self._lock:
            if run_id is None or run_id == "all":
                requested = list(self._children)
            else:
                raw_requested = [run_id] if isinstance(run_id, str) else list(run_id)
                requested = list(dict.fromkeys(str(candidate) for candidate in raw_requested))
            selected = [candidate for candidate in requested if candidate in self._children]
            unknown_run_ids = [
                candidate for candidate in requested if candidate not in self._children
            ]
        running: list[_ScheduledChild] = []
        already_finished_run_ids: list[str] = []
        with self._lock:
            for candidate in selected:
                child = self._children[candidate]
                record = self.registry.get(candidate)
                if record is None or record.state in {"joined", "cancelled"}:
                    already_finished_run_ids.append(candidate)
                    continue
                if child.completion.done():
                    try:
                        result = child.completion.result()
                    except BaseException:
                        self._transition(candidate, "joined")
                    else:
                        self._finish_state_from_result(child=child, result=result)
                    already_finished_run_ids.append(candidate)
                    continue
                cancel_child = getattr(child.cancellation_token, "cancel", None)
                if callable(cancel_child):
                    cancel_child()
                if record.state in {"waiting", "queued"}:
                    result = self._queued_cancelled_result(child)
                    self._complete_synthetic_result_locked(
                        child,
                        result,
                        source="queued_cancelled",
                    )
                    self._transition(candidate, "cancelled")
                    self._release_resume_workspace_pin(child)
                else:
                    running.append(child)
            self._resolve_waiting_children_locked()
            self._emit_completed_rollups_locked()
            self._start_queued_background_locked()
        if wait_for_running:
            join_timeout_s = (
                _DEFAULT_CANCEL_JOIN_TIMEOUT_S
                if wait_timeout_s is None
                else max(0.0, float(wait_timeout_s))
            )
            join_deadline = perf_counter() + join_timeout_s
            for child in running:
                try:
                    result = child.completion.result(
                        timeout=max(0.0, join_deadline - perf_counter())
                    )
                except TimeoutError:
                    # The child was told to cancel and did not unwind in the
                    # window. Abandon it rather than block: it stays marked
                    # as running, and session teardown reaps the process.
                    continue
                self._refresh_usage(child)
                record = self.registry.get(child.run_id)
                if record is not None and record.state not in {"joined", "cancelled"}:
                    self._transition(
                        child.run_id,
                        "cancelled",
                        child_session_id=str(result.get("subagent_session_id") or "") or None,
                    )
        with self._lock:
            completed_run_ids: list[str] = []
            for candidate in selected:
                child = self._children[candidate]
                if child.completion.done():
                    # Cancellation returns status, not the child's report. Keep
                    # its evidence pending explicit collection or durable delivery.
                    completed_run_ids.append(candidate)
            cancelled_run_ids: list[str] = []
            for candidate in selected:
                if candidate in already_finished_run_ids:
                    continue
                record = self.registry.get(candidate)
                if record is not None and record.state == "cancelled":
                    cancelled_run_ids.append(candidate)
                elif record is not None and record.state == "joined":
                    already_finished_run_ids.append(candidate)
        for candidate in completed_run_ids:
            self._notify_lifecycle(candidate)
        states = self.status(run_id=selected)
        return {
            "cancelled_run_ids": cancelled_run_ids,
            "cancellation_requested_run_ids": [
                child.run_id for child in running if child.run_id not in cancelled_run_ids
            ],
            "already_finished_run_ids": already_finished_run_ids,
            "unknown_run_ids": unknown_run_ids,
            "children": states["children"],
        }

    def cancel_parent_turn(
        self,
        *,
        parent_cancellation_token: Any,
        wait_for_running: bool = True,
        wait_timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Cancel only children accepted under one parent-turn token.

        A cancelled wait returns to the turn loop before its next cancellation
        checkpoint.  Both sites therefore observe the same request.  Claim the
        bounded join once per parent token while still making every observation
        signal all children owned by that turn.  This prevents a second caller
        from renewing the default join grace and avoids session-wide cancellation
        of children accepted by another turn.
        """

        with self._lock:
            selected: list[str] = []
            for run_id, child in self._children.items():
                child_token = child.cancellation_token
                belongs_to_parent = child_token is parent_cancellation_token or (
                    isinstance(child_token, _ChildCancellationToken)
                    and child_token.belongs_to_parent(parent_cancellation_token)
                )
                if not belongs_to_parent:
                    continue
                record = self.registry.get(run_id)
                if record is None or record.state in {"joined", "cancelled"}:
                    continue
                if child.completion.done():
                    continue
                selected.append(run_id)

            token_identity = id(parent_cancellation_token)
            claimed = getattr(self, "_parent_cancellation_wait_claimed", None)
            if claimed is None:
                # Compatibility for narrowly constructed coordinator test doubles.
                claimed = set()
                self._parent_cancellation_wait_claimed = claimed
            cancellation_wait_started = bool(
                wait_for_running and selected and token_identity not in claimed
            )
            if cancellation_wait_started:
                claimed.add(token_identity)

        result = self.cancel(
            run_id=selected,
            wait_for_running=cancellation_wait_started,
            wait_timeout_s=(wait_timeout_s if cancellation_wait_started else None),
        )
        return {
            **result,
            "parent_scoped_run_ids": selected,
            "cancellation_wait_started": cancellation_wait_started,
        }

    def status(self, *, run_id: str | list[str] | None = None) -> dict[str, Any]:
        selected = self._selected_run_ids(run_id)
        if isinstance(run_id, str) and run_id != "all" and not selected:
            return {
                "error": f"Unknown background subagent run: {run_id}",
                "error_code": "unknown_background_subagent_run",
                "run_id": run_id,
            }
        children_payload: list[dict[str, Any]] = []
        for candidate in selected:
            child = self._children[candidate]
            self._refresh_usage(child)
            record = self.registry.get(candidate)
            if record is None:
                continue
            steps_completed = int(getattr(child.subagent_surface, "steps_completed", 0) or 0)
            workspace_view = (
                "pinned"
                if str(child.args.get("workspace_from_run") or "").strip()
                else str(child.args.get("workspace_view") or "shared").strip().lower()
            )
            last_event_age_s = max(
                0.0,
                perf_counter() - child.last_event_monotonic,
            )
            activity_threshold_s = max(
                0.001,
                float(
                    getattr(
                        getattr(child.launcher.cfg, "subagent_orchestration", None),
                        "model_response_activity_after_s",
                        15.0,
                    )
                ),
            )
            if record.state == "waiting":
                activity = "Waiting for dependencies."
            elif record.state == "queued":
                activity = "Queued for a scheduler slot."
            elif child.completion.done() and record.state not in {"joined", "cancelled"}:
                activity = "Completed; awaiting collection."
            elif record.state == "running":
                if (
                    child.last_event_kind == "tool_completed"
                    and last_event_age_s >= activity_threshold_s
                ):
                    activity = f"waiting for model response ({max(1, int(last_event_age_s))}s)"
                else:
                    activity = str(
                        getattr(child.subagent_surface, "current_activity", "") or "Running."
                    )
            elif record.state == "spawned":
                activity = "Starting."
            elif record.state == "cancelled":
                activity = "Cancelled."
            else:
                activity = "Joined."
            lifecycle_end = (
                record.finished_monotonic
                if record.finished_monotonic is not None
                else perf_counter()
            )
            lifecycle_elapsed_ms = int((lifecycle_end - record.started_monotonic) * 1000)
            execution_started = record.execution_started_monotonic
            elapsed_ms = (
                int((lifecycle_end - execution_started) * 1000)
                if execution_started is not None
                else 0
            )
            with child.inbox_lock:
                messages_queued = child.messages_queued
                messages_delivered = child.messages_delivered
            children_payload.append(
                {
                    "run_id": candidate,
                    "subagent": record.definition_name,
                    "label": child.label,
                    "workspace_view": workspace_view,
                    "state": record.state,
                    "depends_on": list(record.depends_on),
                    "resumed_from": record.resumed_from,
                    "subagent_session_id": record.child_session_id,
                    "steps_completed": steps_completed,
                    "elapsed_ms": elapsed_ms,
                    "lifecycle_elapsed_ms": lifecycle_elapsed_ms,
                    "messages_queued": messages_queued,
                    "messages_delivered": messages_delivered,
                    "activity": activity,
                    "last_event_age_s": round(last_event_age_s, 3),
                    "activity_threshold_s": activity_threshold_s,
                }
            )
        return {
            "children": children_payload,
            "summary": _children_state_summary(children_payload),
            "unapplied_isolated_results": self.unapplied_isolated_results(),
        }

    def candidate_apply_preflight(
        self,
        *,
        run_id: str,
        acknowledge_incomplete: bool,
    ) -> dict[str, Any]:
        """Require an explicit decision before applying non-successful work."""
        with self._lock:
            child = self._children.get(run_id)
            if child is None or not child.completion.done():
                return {"allowed": True}
            try:
                result = child.completion.result()
            except BaseException as exc:
                status = "failed"
                result = {"error": str(exc)}
            else:
                status = self._result_status(child)
        if status in {"success", "no_changes"}:
            return {"allowed": True}

        stop_reason = str(
            result.get("stop_reason") or result.get("incomplete_reason") or ""
        ).strip()
        report_artifact = str(result.get("report_artifact") or "").strip()
        unfinished_work = {
            "summary": str(result.get("error") or "Run did not finish successfully."),
            "report_artifact": report_artifact or None,
        }
        context = {
            "run_status": status,
            "stop_reason": stop_reason or None,
            "unfinished_work": unfinished_work,
        }
        if acknowledge_incomplete:
            return {
                "allowed": True,
                "incomplete_acknowledged": True,
                **context,
            }
        detail = f" Stop reason: {stop_reason}." if stop_reason else ""
        artifact = (
            f" Unfinished work is recorded in {report_artifact}."
            if report_artifact
            else " No detailed unfinished-work report was persisted."
        )
        return {
            "allowed": False,
            "error": (
                f"Subagent run {run_id} ended with status {status}.{detail}{artifact} "
                "Pass acknowledge_incomplete=true to apply this partial candidate."
            ),
            "error_code": "incomplete_candidate_requires_acknowledgement",
            "run_id": run_id,
            **context,
        }

    @staticmethod
    def _transcript_summary(value: Any, *, max_chars: int = 180) -> str:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=True, sort_keys=True)
            except (TypeError, ValueError):
                text = str(value)
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 3].rstrip() + "..."

    @classmethod
    def _tool_call_summary(cls, name: str, arguments: Any) -> str:
        display_name = tool_display_name(name)
        try:
            spec = get_builtin_tool_metadata(name)
            if spec is not None and "shell" in spec.categories:
                detail = ""
            else:
                detail = tool_input_preview(
                    name,
                    arguments if isinstance(arguments, dict) else {},
                )
        except Exception:  # noqa: BLE001 - malformed previews must not break the panel
            detail = ""
        compact_detail = " ".join(str(detail or "").split())
        if compact_detail in {"", "-"}:
            return display_name
        return f"{display_name} \u00b7 {cls._transcript_summary(compact_detail, max_chars=96)}"

    @classmethod
    def _tool_result_summary(cls, name: str, result: Any) -> str:
        display_name = tool_display_name(name)
        try:
            chunk = (
                result
                if isinstance(result, str)
                else json.dumps(result, ensure_ascii=True, sort_keys=True)
            )
            detail = summarize_tool_output_chunk(name, chunk)
        except Exception:  # noqa: BLE001 - result formatting is best-effort UI data
            detail = "Result available."
        compact_detail = cls._transcript_summary(detail, max_chars=160)
        if not compact_detail:
            compact_detail = "Result available."
        return f"{display_name} \u00b7 {compact_detail}"

    @staticmethod
    def _tool_result_failed(payload: dict[str, Any]) -> bool:
        result = payload.get("result")
        if not isinstance(result, dict):
            return False
        status = str(result.get("status") or "").strip().lower()
        return bool(
            result.get("error")
            or result.get("ok") is False
            or status in {"error", "failed", "failure"}
        )

    @classmethod
    def _transcript_tail(cls, events: list[dict[str, Any]]) -> list[dict[str, str]]:
        summarized: list[dict[str, str]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "").strip()
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event_type == "tool_call":
                name = str(payload.get("name") or "tool").strip()
                summarized.append(
                    {
                        "kind": "tool",
                        "summary": cls._tool_call_summary(
                            name,
                            payload.get("arguments") or {},
                        ),
                    }
                )
            elif event_type == "tool_result":
                name = str(
                    payload.get("executed_tool_name") or payload.get("name") or "tool"
                ).strip()
                kind = "tool_error" if cls._tool_result_failed(payload) else "tool_result"
                summarized.append(
                    {
                        "kind": kind,
                        "summary": cls._tool_result_summary(name, payload.get("result")),
                    }
                )
            elif event_type in {"assistant_message", "final"}:
                content = payload.get("content")
                if not isinstance(content, str):
                    message = payload.get("message")
                    content = message.get("content") if isinstance(message, dict) else ""
                detail = cls._transcript_summary(content)
                if detail:
                    summarized.append({"kind": "assistant", "summary": detail})
            elif event_type == "user_message":
                detail = cls._transcript_summary(payload.get("content"))
                if detail:
                    summarized.append({"kind": "user", "summary": detail})
        return summarized[-20:]

    def view_since(self, *, run_id: str, cursor: int) -> dict[str, Any]:
        """Return one child's status and transcript entries after ``cursor``."""
        status = self.status(run_id=run_id)
        if status.get("error"):
            return status
        children = status.get("children")
        if not isinstance(children, list) or not children:
            return {
                "error": f"Unknown background subagent run: {run_id}",
                "error_code": "unknown_background_subagent_run",
                "run_id": run_id,
            }
        with self._lock:
            child = self._children.get(run_id)
            record = self.registry.get(run_id)
        start = max(0, int(cursor))
        next_cursor = start
        events: list[dict[str, Any]] = []
        child_store = getattr(getattr(child, "sub_session", None), "store", None)
        incremental_read = getattr(child_store, "events_since", None)
        if callable(incremental_read):
            try:
                raw_events, raw_next_cursor = incremental_read(start)
                if isinstance(raw_events, list):
                    events = [event for event in raw_events if isinstance(event, dict)]
                next_cursor = max(0, int(raw_next_cursor))
            except Exception:  # noqa: BLE001 - inspection is best-effort
                events = []
                next_cursor = start
        elif record is not None and record.child_session_id:
            log_path = self.launcher.store.sessions_dir / f"{record.child_session_id}.jsonl"
            try:
                all_events = [event for event in read_session_events(log_path)]
            except (OSError, UnicodeDecodeError, ValueError):
                all_events = []
            next_cursor = len(all_events)
            events = all_events[start:]
        return {
            **children[0],
            "transcript_tail": self._transcript_tail(events),
            "next_cursor": next_cursor,
        }

    def unapplied_isolated_results(self) -> list[dict[str, Any]]:
        provider = self.launcher.workspace_provider
        return provider.unapplied_summaries() if provider is not None else []

    def pending_run_ids(self) -> list[str]:
        with self._lock:
            pending: list[str] = []
            for run_id, child in self._children.items():
                if not child.background or child.collected:
                    continue
                if child.completion.done():
                    try:
                        terminal_status = (
                            str(child.completion.result().get("status") or "").strip().lower()
                        )
                    except BaseException:
                        terminal_status = "failed"
                    if terminal_status == "cancelled":
                        continue
                pending.append(run_id)
            return pending

    def shutdown(
        self,
        *,
        cancel_pending: bool = True,
        wait_for_running: bool = True,
    ) -> None:
        """Close the coordinator under an explicit child-join policy."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            executors = [
                executor
                for executor in (
                    self._executor,
                    *self._retired_executors,
                )
                if executor is not None
            ]
            self._retired_executors = []
        try:
            if cancel_pending:
                self.cancel(run_id="all", wait_for_running=wait_for_running)
        finally:
            try:
                for executor in executors:
                    executor.shutdown(wait=wait_for_running, cancel_futures=True)
            finally:
                self._close_workspace_provider_after_workers()

    def run_after_shutdown_cleanup_attempt(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after workers exit and cleanup has been attempted.

        Readiness and success are deliberately separate: a failed filesystem
        cleanup must not keep the parent store open forever, and it must not be
        reported as successful either. Callers can inspect
        :meth:`workspace_cleanup_status` and explicitly retry when appropriate.
        """

        with self._lock:
            if getattr(self, "_workspace_cleanup_attempted", False):
                run_now = True
            else:
                callbacks = getattr(self, "_workspace_cleanup_callbacks", None)
                if callbacks is None:
                    callbacks = []
                    self._workspace_cleanup_callbacks = callbacks
                callbacks.append(callback)
                run_now = False
        if run_now:
            callback()

    def run_after_shutdown_cleanup(self, callback: Callable[[], None]) -> None:
        """Compatibility alias for cleanup-attempt completion callbacks."""

        self.run_after_shutdown_cleanup_attempt(callback)

    def workspace_cleanup_status(self) -> dict[str, Any]:
        """Return cleanup readiness, success and the latest provider summary."""

        with self._lock:
            return {
                "attempted": bool(getattr(self, "_workspace_cleanup_attempted", False)),
                "succeeded": bool(getattr(self, "_workspace_cleanup_succeeded", False)),
                "in_progress": bool(getattr(self, "_workspace_cleanup_attempt_in_progress", False)),
                "attempt_count": int(getattr(self, "_workspace_cleanup_attempt_count", 0)),
                "summary": copy.deepcopy(getattr(self, "_workspace_cleanup_summary", None)),
            }

    def retry_workspace_cleanup(self) -> dict[str, Any]:
        """Explicitly retry one failed cleanup after every worker has exited."""

        with self._lock:
            if not self._closed:
                return {
                    "ok": False,
                    "error_code": "workspace_cleanup_retry_before_shutdown",
                    "error": "Workspace cleanup can be retried only after shutdown begins.",
                }
            if not self._workspace_workers_idle_locked():
                return {
                    "ok": False,
                    "error_code": "workspace_cleanup_retry_not_ready",
                    "error": "Workspace cleanup cannot be retried while a child is running.",
                }
            if getattr(self, "_workspace_cleanup_attempt_in_progress", False):
                return {
                    "ok": False,
                    "error_code": "workspace_cleanup_in_progress",
                    "error": "Workspace cleanup is already in progress.",
                }
            if getattr(self, "_workspace_cleanup_succeeded", False):
                summary = copy.deepcopy(getattr(self, "_workspace_cleanup_summary", None))
                return summary if isinstance(summary, dict) else {"ok": True}
        return self._attempt_workspace_provider_close(explicit_retry=True)

    def _close_workspace_provider_after_workers(self) -> None:
        """Close workspaces after workers and their terminal bookkeeping finish."""

        with self._lock:
            waiters: list[Future[Any]] = []
            for child in self._children.values():
                worker_future = child.executor_future
                if worker_future is not None:
                    bookkeeping_completion = getattr(
                        child,
                        "worker_bookkeeping_completion",
                        worker_future,
                    )
                    if not bookkeeping_completion.done():
                        waiters.append(bookkeeping_completion)
                    continue
                if not child.background and not child.completion.done():
                    waiters.append(child.completion)
        if not waiters:
            self._finalize_workspace_provider_close()
            return
        for future in waiters:
            future.add_done_callback(
                lambda _future: self._finalize_workspace_provider_close_if_idle()
            )
        # A worker may have completed between the snapshot and callback
        # registration. Recheck once so that race cannot strand cleanup.
        self._finalize_workspace_provider_close_if_idle()

    def _finalize_workspace_provider_close_if_idle(self) -> None:
        with self._lock:
            if not self._workspace_workers_idle_locked():
                return
        self._finalize_workspace_provider_close()

    def _workspace_workers_idle_locked(self) -> bool:
        for child in self._children.values():
            worker_future = child.executor_future
            if worker_future is not None:
                bookkeeping_completion = getattr(
                    child,
                    "worker_bookkeeping_completion",
                    worker_future,
                )
                if not bookkeeping_completion.done():
                    return False
            if worker_future is None and not child.background and not child.completion.done():
                return False
        return True

    def _finalize_workspace_provider_close(self) -> None:
        self._attempt_workspace_provider_close(explicit_retry=False)

    def _attempt_workspace_provider_close(
        self,
        *,
        explicit_retry: bool,
    ) -> dict[str, Any]:
        with self._lock:
            if getattr(self, "_workspace_cleanup_attempt_in_progress", False):
                status = self.workspace_cleanup_status()
                return {
                    "ok": False,
                    "error_code": "workspace_cleanup_in_progress",
                    "error": "Workspace cleanup is already in progress.",
                    **status,
                }
            if getattr(self, "_workspace_cleanup_attempted", False) and not explicit_retry:
                summary = copy.deepcopy(getattr(self, "_workspace_cleanup_summary", None))
                return summary if isinstance(summary, dict) else {"ok": True}
            self._workspace_cleanup_attempt_in_progress = True
            workspace_provider = self.launcher.workspace_provider
            attempt = int(getattr(self, "_workspace_cleanup_attempt_count", 0)) + 1
            self._workspace_cleanup_attempt_count = attempt
        summary: dict[str, Any]
        try:
            if workspace_provider is not None:
                raw_summary = workspace_provider.close()
                summary = (
                    copy.deepcopy(raw_summary) if isinstance(raw_summary, dict) else {"ok": True}
                )
            else:
                summary = {"ok": True}
        except Exception as exc:  # noqa: BLE001 - teardown must release store ownership
            summary = {
                "ok": False,
                "cleanup_pending_run_ids": [],
                "failures": [
                    {
                        "error": str(exc),
                        "error_code": "workspace_cleanup_exception",
                    }
                ],
            }
        succeeded = bool(summary.get("ok"))
        if not succeeded:
            try:
                self.launcher.store.append(
                    "warning",
                    {
                        "warning": "subagent_workspace_cleanup_incomplete",
                        "attempt": attempt,
                        "cleanup_pending_run_ids": list(
                            summary.get("cleanup_pending_run_ids") or []
                        ),
                        "failures": copy.deepcopy(summary.get("failures") or []),
                    },
                )
            except Exception:
                pass
        with self._lock:
            first_attempt = not getattr(self, "_workspace_cleanup_attempted", False)
            self._workspace_cleanup_attempt_in_progress = False
            self._workspace_cleanup_attempted = True
            self._workspace_cleanup_succeeded = succeeded
            self._workspace_cleanup_summary = copy.deepcopy(summary)
            callbacks = (
                list(getattr(self, "_workspace_cleanup_callbacks", [])) if first_attempt else []
            )
            if first_attempt:
                self._workspace_cleanup_callbacks = []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass
        return summary


# Compatibility name for callers and extensions that still describe the stable
# session coordinator by its original scheduling responsibility.  Both names
# intentionally identify the same object; there is no proxy or dynamic dispatch.
ChildScheduler = SubagentCoordinator
