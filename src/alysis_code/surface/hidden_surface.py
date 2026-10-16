from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..safety.subagent_report import sanitize_subagent_report
from .base import Surface
from .events import Event
from .noop_surface import NoopSurface
from .types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StandingApproval,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)

_PARENT_SURFACE_FORWARD_LOCK = threading.RLock()


def _tool_activity_label(name: str) -> str:
    try:
        from ..tools.registry import tool_display_name

        return str(tool_display_name(name) or name)
    except Exception:  # noqa: BLE001 - status reporting cannot break a child
        return str(name or "Working")


class HiddenApprovalSurface(NoopSurface):
    def __init__(
        self,
        parent_surface: Surface | None,
        *,
        approval_lock: Any | None = None,
    ) -> None:
        self._parent_surface = parent_surface or NoopSurface()
        self._approval_lock = approval_lock

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._approval_lock is None:
            return self._parent_surface.request_approval(request)
        with self._approval_lock:
            return self._parent_surface.request_approval(request)

    def standing_approval(self, request: ApprovalRequest) -> StandingApproval | None:
        standing = getattr(self._parent_surface, "standing_approval", None)
        return standing(request) if callable(standing) else None

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (text, worker_id, role)

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (text, worker_id, role)

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (call_id, name, arguments_preview, worker_id, role)

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (call_id, text, worker_id, role)

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (call_id, success, result_preview, worker_id, role)

    def emit_status_update(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cached_tokens: int | None = None,
        cost_usd: float | None = None,
        mode: str | None = None,
        model: str | None = None,
        step: int | None = None,
        step_budget: int | None = None,
    ) -> None:
        _ = (
            tokens_in,
            tokens_out,
            cached_tokens,
            cost_usd,
            mode,
            model,
            step,
            step_budget,
        )

    def emit_mode_changed(self, mode: str) -> None:
        _ = mode

    def emit_persona_changed(self, persona: str, effective_mode: str, source: str = "user") -> None:
        _ = (persona, effective_mode, source)

    def emit_plan_node_updated(
        self,
        node_id: str,
        state: str,
        summary: str | None = None,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (node_id, state, summary, worker_id, role)

    def emit_swarm_worker_state_changed(
        self,
        worker_id: str,
        state: str,
        *,
        role: str | None = None,
    ) -> None:
        _ = (worker_id, state, role)

    def emit_verify_gate_result(
        self,
        command: str,
        success: bool,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (command, success, summary, worker_id, role)

    def emit_approval_reviewed(
        self,
        outcome: str,
        subject: str,
        reason: str = "",
        *,
        risk: str = "",
        approval_kind: str = "",
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        # Approvals are forwarded to the parent, so their reviews are too: the person
        # sees what was approved on their behalf, whichever agent asked.
        forward = getattr(self._parent_surface, "emit_approval_reviewed", None)
        if callable(forward):
            forward(
                outcome,
                subject,
                reason,
                risk=risk,
                approval_kind=approval_kind,
                worker_id=worker_id,
                role=role,
            )

    def emit_review_gate_decision(
        self,
        decision: str,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (decision, summary, worker_id, role)

    def emit_error(
        self,
        code: str,
        message: str,
        recoverable: bool,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (code, message, recoverable, worker_id, role)

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (message, worker_id, role)

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (message, worker_id, role)

    def emit_prompt_for_input(self, prompt_id: str, prompt_text: str, kind: str) -> None:
        _ = (prompt_id, prompt_text, kind)

    def emit_config_form_request(self, form_id: str, schema: dict[str, Any]) -> None:
        _ = (form_id, schema)

    def emit(self, event: Event) -> None:
        _ = event


class NestedSubagentSurface(HiddenApprovalSurface):
    def __init__(
        self,
        parent_surface: Surface | None,
        *,
        subagent_name: str,
        subagent_mode: str,
        subagent_run_id: str | None = None,
        subagent_label: str = "",
        workspace_view: str = "shared",
        approval_lock: Any | None = None,
        event_observer: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent_surface, approval_lock=approval_lock)
        self._subagent_name = subagent_name
        self._subagent_mode = subagent_mode
        self._subagent_run_id = subagent_run_id
        self._subagent_label = str(subagent_label or "")
        self._workspace_view = workspace_view
        # Providers commonly reuse child-local call ids (for example ``call_1``).
        # Include the lifecycle run id so concurrent invocations of the same role
        # cannot overwrite one another in parent UI/protocol tool state. Preserve
        # the historical prefix for wrappers created without a run id.
        self._tool_call_prefix = (
            f"subagent:{subagent_name}:{subagent_run_id}:"
            if subagent_run_id
            else f"subagent:{subagent_name}:"
        )
        self._steps_completed = 0
        self._current_activity = "Starting."
        self._tool_activity_names: dict[str, str] = {}
        self._assistant_messages_done: list[str] = []
        self._assistant_message_chunks: dict[tuple[str | None, str | None], list[str]] = {}
        self._event_observer = event_observer

    def set_event_observer(self, observer: Callable[[str], None] | None) -> None:
        self._event_observer = observer

    def _observe_event(self, kind: str) -> None:
        if self._event_observer is not None:
            self._event_observer(kind)

    def set_model_retry_activity(self, attempt: int) -> None:
        self._current_activity = f"retrying model call, attempt {max(1, int(attempt))}"
        self._observe_event("llm_retry")

    @property
    def canonical_message_tool_events(self) -> bool:
        """Preserve the parent's single-delivery protocol contract through this wrapper."""
        return bool(getattr(self._parent_surface, "canonical_message_tool_events", False))

    @property
    def steps_completed(self) -> int:
        return self._steps_completed

    @property
    def current_activity(self) -> str:
        return self._current_activity

    @property
    def last_assistant_message_done(self) -> str:
        return self._assistant_messages_done[-1] if self._assistant_messages_done else ""

    def _scoped_worker_id(self, worker_id: str | None) -> str:
        return worker_id or self._subagent_name

    def _scoped_role(self, role: str | None) -> str:
        return role or self._subagent_mode

    def _scoped_tool_call_id(self, call_id: str) -> str:
        return f"{self._tool_call_prefix}{call_id}"

    def _call_parent_emit(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        handler = getattr(self._parent_surface, method_name, None)
        if callable(handler):
            with _PARENT_SURFACE_FORWARD_LOCK:
                handler(*args, **kwargs)

    def on_assistant_message_done(self, text: str) -> None:
        clean = str(text or "").strip()
        if clean:
            self._observe_event("model_progress")
            self._assistant_messages_done.append(clean)

    def on_subagent_start(self, event: SubagentStartEvent) -> None:
        self._observe_event("lifecycle")
        handler = getattr(self._parent_surface, "on_subagent_start", None)
        if callable(handler):
            with _PARENT_SURFACE_FORWARD_LOCK:
                handler(
                    replace(
                        event,
                        subagent_run_id=event.subagent_run_id or self._subagent_run_id,
                        label=event.label or self._subagent_label,
                        workspace_view=self._workspace_view,
                    )
                )

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        self._observe_event("lifecycle")
        handler = getattr(self._parent_surface, "on_subagent_end", None)
        if callable(handler):
            with _PARENT_SURFACE_FORWARD_LOCK:
                handler(
                    replace(
                        event,
                        subagent_run_id=event.subagent_run_id or self._subagent_run_id,
                        label=event.label or self._subagent_label,
                        workspace_view=self._workspace_view,
                    )
                )

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        key = (worker_id, role)
        self._assistant_message_chunks.setdefault(key, []).append(str(text or ""))
        if text:
            self._observe_event("model_progress")

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        key = (worker_id, role)
        buffered_text = "".join(self._assistant_message_chunks.pop(key, []))
        complete_text = str(text or "") or buffered_text
        safe_text = sanitize_subagent_report(complete_text).text
        if safe_text:
            self._call_parent_emit(
                "emit_message_delta",
                safe_text,
                worker_id=self._scoped_worker_id(worker_id),
                role=self._scoped_role(role),
            )
        self._call_parent_emit(
            "emit_message_end",
            safe_text,
            worker_id=self._scoped_worker_id(worker_id),
            role=self._scoped_role(role),
        )

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._observe_event("tool_started")
        self._tool_activity_names[call_id] = name
        self._current_activity = _tool_activity_label(name)
        self._call_parent_emit(
            "emit_tool_call_started",
            self._scoped_tool_call_id(call_id),
            name,
            sanitize_subagent_report(arguments_preview).text,
            worker_id=self._scoped_worker_id(worker_id),
            role=self._scoped_role(role),
        )

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._observe_event("tool_progress")
        self._call_parent_emit(
            "emit_tool_call_progress",
            self._scoped_tool_call_id(call_id),
            sanitize_subagent_report(text).text,
            worker_id=self._scoped_worker_id(worker_id),
            role=self._scoped_role(role),
        )

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._observe_event("tool_completed")
        name = self._tool_activity_names.pop(call_id, "")
        outcome = "complete" if success else "failed"
        self._current_activity = f"{_tool_activity_label(name)} {outcome}."
        self._call_parent_emit(
            "emit_tool_call_completed",
            self._scoped_tool_call_id(call_id),
            success,
            sanitize_subagent_report(result_preview).text,
            worker_id=self._scoped_worker_id(worker_id),
            role=self._scoped_role(role),
        )

    def emit_status_update(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cached_tokens: int | None = None,
        cost_usd: float | None = None,
        mode: str | None = None,
        model: str | None = None,
        step: int | None = None,
        step_budget: int | None = None,
    ) -> None:
        self._call_parent_emit(
            "emit_status_update",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens=cached_tokens,
            cost_usd=cost_usd,
            mode=mode,
            model=model,
            step=step,
            step_budget=step_budget,
        )

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._call_parent_emit(
            "emit_warning",
            sanitize_subagent_report(message).text,
            worker_id=self._scoped_worker_id(worker_id),
            role=self._scoped_role(role),
        )

    def emit_error(
        self,
        code: str,
        message: str,
        recoverable: bool,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._call_parent_emit(
            "emit_error",
            code,
            sanitize_subagent_report(message).text,
            recoverable,
            worker_id=self._scoped_worker_id(worker_id),
            role=self._scoped_role(role),
        )

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._call_parent_emit(
            "emit_info",
            sanitize_subagent_report(message).text,
            worker_id=self._scoped_worker_id(worker_id),
            role=self._scoped_role(role),
        )

    def on_tool_start(self, event: ToolStartEvent) -> None:
        self._observe_event("tool_started")
        self._steps_completed = max(self._steps_completed, int(event.step))
        self._tool_activity_names[event.tool_call_id] = event.name
        self._current_activity = _tool_activity_label(event.name)
        self._parent_surface.on_tool_start(
            replace(
                event,
                tool_call_id=self._scoped_tool_call_id(event.tool_call_id),
                subagent_name=self._subagent_name,
                subagent_mode=self._subagent_mode,
                nesting_depth=max(int(event.nesting_depth), 0) + 1,
            )
        )

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        self._observe_event("tool_progress")
        self._parent_surface.on_tool_output(
            replace(
                event,
                tool_call_id=self._scoped_tool_call_id(event.tool_call_id),
                chunk=sanitize_subagent_report(event.chunk).text,
                subagent_name=self._subagent_name,
                subagent_mode=self._subagent_mode,
                nesting_depth=max(int(event.nesting_depth), 0) + 1,
            )
        )

    def on_patch_generated(self, event: PatchEvent) -> None:
        with _PARENT_SURFACE_FORWARD_LOCK:
            self._parent_surface.on_patch_generated(
                replace(
                    event,
                    subagent_name=self._subagent_label or self._subagent_name,
                    workspace_view=self._workspace_view,
                )
            )

    def on_tool_end(self, event: ToolEndEvent) -> None:
        self._observe_event("tool_completed")
        self._tool_activity_names.pop(event.tool_call_id, None)
        outcome = "complete" if event.status == "done" else "failed"
        self._current_activity = f"{_tool_activity_label(event.name)} {outcome}."
        self._parent_surface.on_tool_end(
            replace(
                event,
                tool_call_id=self._scoped_tool_call_id(event.tool_call_id),
                subagent_name=self._subagent_name,
                subagent_mode=self._subagent_mode,
                nesting_depth=max(int(event.nesting_depth), 0) + 1,
            )
        )
