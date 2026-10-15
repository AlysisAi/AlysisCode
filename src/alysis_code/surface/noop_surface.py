from __future__ import annotations

from typing import Any

from .events import Event
from .types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)


class NoopSurface:
    def on_status_update(self, status: StatusEvent) -> None:
        _ = status

    def on_user_message(self, text: str) -> None:
        _ = text

    def on_progress_update(self, message: str) -> None:
        _ = message

    def on_assistant_token(self, delta: str) -> None:
        _ = delta

    def on_assistant_message_done(self, text: str) -> None:
        _ = text

    def on_subagent_start(self, event: SubagentStartEvent) -> None:
        _ = event

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        _ = event

    def on_tool_start(self, event: ToolStartEvent) -> None:
        _ = event

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        _ = event

    def on_tool_end(self, event: ToolEndEvent) -> None:
        _ = event

    def on_patch_generated(self, event: PatchEvent) -> None:
        _ = event

    def on_warning(self, warning: str) -> None:
        _ = warning

    def on_error(self, err: str) -> None:
        _ = err

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        _ = request
        return ApprovalDecision(allow=False)

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
        _ = (outcome, subject, reason, risk, approval_kind, worker_id, role)

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
