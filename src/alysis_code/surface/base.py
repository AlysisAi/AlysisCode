from __future__ import annotations

from typing import Any, Protocol

from .events import (
    ApprovalReviewed,
    ConfigFormRequest,
    ErrorRaised,
    Event,
    InfoEmitted,
    MessageDelta,
    MessageEnd,
    ModeChanged,
    PersonaChanged,
    PlanNodeUpdated,
    PromptForInput,
    ReviewGateDecision,
    StatusUpdate,
    SwarmWorkerStateChanged,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
    VerifyGateResult,
    WarningEmitted,
)
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


class Surface(Protocol):
    def on_status_update(self, status: StatusEvent) -> None: ...

    def on_user_message(self, text: str) -> None: ...

    def on_progress_update(self, message: str) -> None: ...

    def on_assistant_token(self, delta: str) -> None: ...

    def on_assistant_message_done(self, text: str) -> None: ...

    def on_subagent_start(self, event: SubagentStartEvent) -> None: ...

    def on_subagent_end(self, event: SubagentEndEvent) -> None: ...

    def on_tool_start(self, event: ToolStartEvent) -> None: ...

    def on_tool_output(self, event: ToolOutputEvent) -> None: ...

    def on_tool_end(self, event: ToolEndEvent) -> None: ...

    def on_patch_generated(self, event: PatchEvent) -> None: ...

    def on_warning(self, warning: str) -> None: ...

    def on_error(self, err: str) -> None: ...

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision: ...

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
        if event.type == MessageDelta.type:
            self.emit_message_delta(event.text, worker_id=event.worker_id, role=event.role)
        elif event.type == MessageEnd.type:
            self.emit_message_end(event.text, worker_id=event.worker_id, role=event.role)
        elif event.type == ToolCallStarted.type:
            self.emit_tool_call_started(
                event.call_id,
                event.name,
                event.arguments_preview,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ToolCallProgress.type:
            self.emit_tool_call_progress(
                event.call_id,
                event.text,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ToolCallCompleted.type:
            self.emit_tool_call_completed(
                event.call_id,
                event.success,
                event.result_preview,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == StatusUpdate.type:
            self.emit_status_update(
                tokens_in=event.tokens_in,
                tokens_out=event.tokens_out,
                cached_tokens=event.cached_tokens,
                cost_usd=event.cost_usd,
                mode=event.mode,
                model=event.model,
                step=event.step,
                step_budget=event.step_budget,
            )
        elif event.type == ModeChanged.type:
            self.emit_mode_changed(event.mode)
        elif event.type == PersonaChanged.type:
            self.emit_persona_changed(event.persona, event.effective_mode, event.source)
        elif event.type == PlanNodeUpdated.type:
            self.emit_plan_node_updated(
                event.node_id,
                event.state,
                event.summary,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == SwarmWorkerStateChanged.type:
            self.emit_swarm_worker_state_changed(event.worker_id, event.state, role=event.role)
        elif event.type == VerifyGateResult.type:
            self.emit_verify_gate_result(
                event.command,
                event.success,
                event.summary,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ApprovalReviewed.type:
            self.emit_approval_reviewed(
                event.outcome,
                event.subject,
                event.reason,
                risk=event.risk,
                approval_kind=event.approval_kind,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ReviewGateDecision.type:
            self.emit_review_gate_decision(
                event.decision,
                event.summary,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ErrorRaised.type:
            self.emit_error(
                event.code,
                event.message,
                event.recoverable,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == WarningEmitted.type:
            self.emit_warning(event.message, worker_id=event.worker_id, role=event.role)
        elif event.type == InfoEmitted.type:
            self.emit_info(event.message, worker_id=event.worker_id, role=event.role)
        elif event.type == PromptForInput.type:
            self.emit_prompt_for_input(event.prompt_id, event.prompt_text, event.kind)
        elif event.type == ConfigFormRequest.type:
            self.emit_config_form_request(event.form_id, event.schema)
