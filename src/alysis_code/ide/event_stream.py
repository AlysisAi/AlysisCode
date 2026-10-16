from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..surface.events import (
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
    ReasoningDelta,
    ReasoningEnded,
    ReasoningStarted,
    ReviewGateDecision,
    StatusUpdate,
    SubagentStateChanged,
    SwarmWorkerStateChanged,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
    VerifyGateResult,
    WarningEmitted,
)
from ..surface.noop_surface import NoopSurface
from ..surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StandingApproval,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)
from .activity_events import (
    ACTIVITY_EVENT_TYPE,
    ActivityEvent,
    patch_activity,
    sanitize_activity_metadata,
    semantic_tool_name,
    tool_activity,
)
from .protocol import PROTOCOL_VERSION, redact_secrets

ApprovalEmitter = Callable[[Any], None]
ApprovalHandler = Callable[[ApprovalRequest, ApprovalEmitter], ApprovalDecision]
StandingApprovalHandler = Callable[[ApprovalRequest], StandingApproval | None]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _arguments_from_preview(arguments_preview: str) -> dict[str, Any]:
    """Recover structured tool arguments from the canonical JSON preview, best effort.

    The canonical emit path only carries the serialized preview. It is the same JSON the
    core produced from the tool arguments, so it round-trips unless it was truncated, in
    which case the activity simply has no target to show.
    """
    try:
        parsed = json.loads(arguments_preview)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bounded_lifecycle_text(value: object, *, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= maximum else f"{text[: maximum - 3].rstrip()}..."


@dataclass(frozen=True, slots=True)
class EventContext:
    session_id: str
    run_id: str | None = None
    job_id: str | None = None


class EventSequencer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sequence = 0

    def envelope(
        self,
        event: Event,
        *,
        context: EventContext,
        redact_payload: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        data = event.to_dict()
        event_type = str(data.pop("type"))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "job_id": context.job_id,
            "sequence": sequence,
            "timestamp": _now_iso(),
            "type": event_type,
            "payload": redact_secrets(data) if redact_payload else data,
        }


@dataclass(frozen=True, slots=True)
class ProtocolPayloadEvent:
    event_type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.event_type, **self.payload}


class ProtocolEventSurface(NoopSurface):
    host_managed_approvals = True
    canonical_message_tool_events = True

    def __init__(
        self,
        *,
        context: EventContext,
        emit: Callable[[dict[str, Any]], None],
        sequencer: EventSequencer | None = None,
        approval_handler: ApprovalHandler | None = None,
        standing_approval_handler: StandingApprovalHandler | None = None,
        semantic_activity_events: bool = False,
        reasoning_trace_events: bool = False,
    ) -> None:
        self._base_context = context
        self._emit_message = emit
        self._sequencer = sequencer or EventSequencer()
        self._approval_handler = approval_handler
        self._standing_approval_handler = standing_approval_handler
        self._semantic_activity_events = semantic_activity_events
        self._thread_context = threading.local()
        self._trace_level = "compact"
        # Tool names by call id for calls whose start we announced as a semantic activity, so the
        # canonical completion (which carries no tool name) can close the same activity row.
        self._activity_tool_names: dict[str, str] = {}
        self._activity_tool_names_lock = threading.Lock()
        # Provider reasoning reaches the wire only for hosts that asked for it at
        # session.create; other IDE clients keep today's behaviour and request no summaries.
        self._reasoning_trace_events = bool(reasoning_trace_events)
        self._reasoning_block_id: str | None = None
        self._reasoning_lock = threading.Lock()

    @property
    def context(self) -> EventContext:
        job_id = getattr(self._thread_context, "job_id", None)
        run_id = getattr(self._thread_context, "run_id", None)
        return EventContext(
            session_id=self._base_context.session_id,
            run_id=run_id if run_id is not None else self._base_context.run_id,
            job_id=job_id if job_id is not None else self._base_context.job_id,
        )

    def with_job(self, job_id: str | None) -> ProtocolEventSurface:
        self._thread_context.job_id = job_id
        return self

    def current_tool_call_id(self) -> str | None:
        value = getattr(self._thread_context, "tool_call_id", None)
        return str(value) if value else None

    @property
    def trace_level(self) -> str:
        return self._trace_level

    def set_trace_level(self, level: str) -> str:
        normalized = str(level or "").strip().lower()
        if normalized not in {"off", "compact", "full"}:
            normalized = "compact"
        self._trace_level = normalized
        return self._trace_level

    @property
    def reasoning_trace_events(self) -> bool:
        """Whether this session's host opted in to reasoning events at session.create."""

        return self._reasoning_trace_events

    @property
    def reasoning_trace_enabled(self) -> bool:
        """Whether the agent core should attach its reasoning sink right now.

        The trace level gates it as the terminal's ``/trace`` does: at ``off`` the core attaches
        no sink, so routes that need a request flag (Responses, Anthropic) are not asked for
        reasoning summaries at all.
        """

        return self._reasoning_trace_events and self._trace_level != "off"

    def on_reasoning_start(self, block_id: str) -> None:
        """Open one provider-call-scoped reasoning block."""

        if not self.reasoning_trace_enabled:
            return
        normalized = str(block_id or "").strip() or f"reasoning-{uuid.uuid4().hex[:12]}"
        with self._reasoning_lock:
            previous = self._reasoning_block_id
            if previous == normalized:
                return
            self._reasoning_block_id = normalized
        if previous is not None:
            self._emit_event(ReasoningEnded(block_id=previous))
        self._emit_event(ReasoningStarted(block_id=normalized))

    def on_reasoning_token(self, delta: str) -> None:
        """Stream provider reasoning display text; encrypted state never reaches this hook."""

        if not delta or not self.reasoning_trace_enabled:
            return
        with self._reasoning_lock:
            block_id = self._reasoning_block_id
            opened = block_id is None
            if block_id is None:
                block_id = f"reasoning-{uuid.uuid4().hex[:12]}"
                self._reasoning_block_id = block_id
        if opened:
            self._emit_event(ReasoningStarted(block_id=block_id))
        self._emit_event(ReasoningDelta(block_id=block_id, text=delta))

    def on_reasoning_end(self, block_id: str) -> None:
        """Close the open block; a stale or unknown id is ignored. Not gated by the level."""

        normalized = str(block_id or "").strip()
        with self._reasoning_lock:
            current = self._reasoning_block_id
            if current is None or (normalized and normalized != current):
                return
            self._reasoning_block_id = None
        self._emit_event(ReasoningEnded(block_id=current))

    def _emit_event(self, event: Event) -> None:
        self._emit_message(self._sequencer.envelope(event, context=self.context))

    def transient_envelope(self, event: Event) -> dict[str, Any]:
        """Build a sequenced event without sending it through the replay sink.

        Computer Use requests can contain typed text or target labels. They must travel to the
        trusted host but must never enter session.getEvents, trace artifacts, or normal event
        listeners. The owning bridge writes this envelope directly to its authenticated pipe.
        """

        return self._sequencer.envelope(
            event,
            context=self.context,
            redact_payload=False,
        )

    def emit(self, event: Event) -> None:
        self._emit_event(event)

    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(MessageDelta(text=text, worker_id=worker_id, role=role))

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(MessageEnd(text=text, worker_id=worker_id, role=role))

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
        arguments: Mapping[str, Any] | None = None,
        activity_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._thread_context.tool_call_id = call_id
        self._emit_event(
            ToolCallStarted(
                call_id=call_id,
                name=semantic_tool_name(name),
                arguments_preview=arguments_preview,
                worker_id=worker_id,
                role=role,
            )
        )
        # The agent core talks to this surface through the canonical emit_* path only
        # (`canonical_message_tool_events`), so the semantic activity has to be produced here —
        # not in on_tool_start, which the core never calls for this surface. Without this the IDE
        # advertised semantic activity while emitting it for patches and prompts alone, and every
        # verify_run/shell_run silently disappeared from the transcript.
        if self._semantic_activity_events:
            with self._activity_tool_names_lock:
                self._activity_tool_names[call_id] = name
            self.emit_activity(
                tool_activity(
                    call_id=call_id,
                    name=name,
                    arguments=(
                        arguments
                        if arguments is not None
                        else _arguments_from_preview(arguments_preview)
                    ),
                    status="running",
                    metadata={**dict(activity_metadata or {}), "worker_id": worker_id},
                )
            )

    def emit_activity(self, event: ActivityEvent) -> None:
        self._emit_event(ProtocolPayloadEvent(ACTIVITY_EVENT_TYPE, event.to_payload()))

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            ToolCallProgress(call_id=call_id, text=text, worker_id=worker_id, role=role)
        )

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        activity_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            self._emit_event(
                ToolCallCompleted(
                    call_id=call_id,
                    success=success,
                    result_preview=result_preview,
                    worker_id=worker_id,
                    role=role,
                )
            )
            if self._semantic_activity_events:
                with self._activity_tool_names_lock:
                    name = self._activity_tool_names.pop(call_id, None)
                if name is None:
                    # No activity row was opened for this call (start predates this surface, or the
                    # start was never announced); do not invent a terminal row without a kind.
                    return
                self.emit_activity(
                    tool_activity(
                        call_id=call_id,
                        name=name,
                        status=status or ("done" if success else "failed"),
                        duration_ms=duration_ms,
                        metadata={**dict(activity_metadata or {}), "worker_id": worker_id},
                    )
                )
        finally:
            if self.current_tool_call_id() == call_id:
                self._thread_context.tool_call_id = None

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
        self._emit_event(
            StatusUpdate(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_tokens=cached_tokens,
                cost_usd=cost_usd,
                mode=mode,
                model=model,
                step=step,
                step_budget=step_budget,
            )
        )

    def emit_mode_changed(self, mode: str) -> None:
        self._emit_event(ModeChanged(mode=mode))

    def emit_persona_changed(self, persona: str, effective_mode: str, source: str = "user") -> None:
        self._emit_event(
            PersonaChanged(persona=persona, effective_mode=effective_mode, source=source)
        )

    def emit_plan_node_updated(
        self,
        node_id: str,
        state: str,
        summary: str | None = None,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            PlanNodeUpdated(
                node_id=node_id,
                state=state,
                summary=summary,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_swarm_worker_state_changed(
        self,
        worker_id: str,
        state: str,
        *,
        role: str | None = None,
    ) -> None:
        self._emit_event(SwarmWorkerStateChanged(worker_id=worker_id, state=state, role=role))

    def on_subagent_start(self, event: SubagentStartEvent) -> None:
        run_id = str(event.subagent_run_id or "").strip() or uuid.uuid4().hex
        self._thread_context.subagent_run_id = run_id
        self._emit_event(
            SubagentStateChanged(
                subagent_run_id=run_id,
                name=event.name,
                mode=event.mode,
                state="running",
                label=event.label,
                subagent_session_id=event.subagent_session_id,
                description=_bounded_lifecycle_text(event.description, maximum=240),
            )
        )

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        run_id = (
            str(event.subagent_run_id or "").strip()
            or str(getattr(self._thread_context, "subagent_run_id", "") or "").strip()
            or uuid.uuid4().hex
        )
        self._emit_event(
            SubagentStateChanged(
                subagent_run_id=run_id,
                name=event.name,
                mode=event.mode,
                state=event.status,
                label=event.label,
                subagent_session_id=event.subagent_session_id,
                elapsed_ms=event.elapsed_ms,
                steps_completed=event.steps_completed,
                error=(
                    _bounded_lifecycle_text(event.error, maximum=600)
                    if event.error is not None
                    else None
                ),
            )
        )
        self._thread_context.subagent_run_id = None

    def emit_verify_gate_result(
        self,
        command: str,
        success: bool,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            VerifyGateResult(
                command=command,
                success=success,
                summary=summary,
                worker_id=worker_id,
                role=role,
            )
        )

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
        self._emit_event(
            ApprovalReviewed(
                outcome=outcome,
                subject=subject,
                reason=reason,
                risk=risk,
                approval_kind=approval_kind,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_review_gate_decision(
        self,
        decision: str,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(
            ReviewGateDecision(decision=decision, summary=summary, worker_id=worker_id, role=role)
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
        self._emit_event(
            ErrorRaised(
                code=code,
                message=message,
                recoverable=recoverable,
                worker_id=worker_id,
                role=role,
            )
        )

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(WarningEmitted(message=message, worker_id=worker_id, role=role))

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        self._emit_event(InfoEmitted(message=message, worker_id=worker_id, role=role))

    def emit_prompt_for_input(self, prompt_id: str, prompt_text: str, kind: str) -> None:
        self._emit_event(PromptForInput(prompt_id=prompt_id, prompt_text=prompt_text, kind=kind))

    def emit_config_form_request(self, form_id: str, schema: dict[str, Any]) -> None:
        self._emit_event(ConfigFormRequest(form_id=form_id, schema=schema))

    def on_status_update(self, status: StatusEvent) -> None:
        self.emit_status_update(mode=status.mode, model=status.model)

    def on_progress_update(self, message: str) -> None:
        self.emit_info(message)

    def on_assistant_token(self, delta: str) -> None:
        self.emit_message_delta(delta)

    def on_assistant_message_done(self, text: str) -> None:
        self.emit_message_end(text)

    def on_tool_start(self, event: ToolStartEvent) -> None:
        # Legacy delivery path (surfaces without canonical_message_tool_events). The semantic
        # activity is emitted by emit_tool_call_started itself, with the structured arguments.
        self.emit_tool_call_started(
            event.tool_call_id,
            event.name,
            json.dumps(event.args, ensure_ascii=True, sort_keys=True),
            worker_id=event.subagent_name,
            role=event.subagent_mode,
            arguments=event.args,
            activity_metadata={"step": event.step},
        )

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        self.emit_tool_call_progress(
            event.tool_call_id,
            event.chunk,
            worker_id=event.subagent_name,
            role=event.subagent_mode,
        )

    def on_tool_end(self, event: ToolEndEvent) -> None:
        # Legacy delivery path; the terminal semantic activity is emitted by emit_tool_call_completed.
        # A tool whose start this surface never saw still gets its activity row: register the name
        # so the completion can close it.
        if self._semantic_activity_events:
            with self._activity_tool_names_lock:
                self._activity_tool_names.setdefault(event.tool_call_id, event.name)
        self.emit_tool_call_completed(
            event.tool_call_id,
            str(event.status).strip().lower() in {"completed", "done", "success"},
            json.dumps(
                {
                    "status": event.status,
                    "elapsed_ms": event.elapsed_ms,
                    "meta": sanitize_activity_metadata(event.meta),
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            worker_id=event.subagent_name,
            role=event.subagent_mode,
            status=event.status,
            duration_ms=event.elapsed_ms,
            activity_metadata=dict(event.meta),
        )

    def on_patch_generated(self, event: PatchEvent) -> None:
        self.emit_activity(patch_activity(event))

    def on_warning(self, warning: str) -> None:
        self.emit_warning(warning)

    def on_error(self, err: str) -> None:
        self.emit_error("runtime_error", err, True)

    def standing_approval(self, request: ApprovalRequest) -> StandingApproval | None:
        """The host's permission rules and session grants, without showing a prompt."""

        if self._standing_approval_handler is None:
            return None
        return self._standing_approval_handler(request)

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._approval_handler is not None:
            return self._approval_handler(request, self._emit_event)
        approval_id = str(request.metadata.get("approval_id") or uuid.uuid4().hex)
        approval_kind = str(request.kind or "generic")
        prompt_text = request.preview or request.reason or f"Approval required: {approval_kind}"
        warning = "Host-managed approval handling is unavailable; request denied fail-closed."
        self._emit_event(
            ProtocolPayloadEvent(
                "prompt_for_input",
                {
                    "prompt_id": approval_id,
                    "prompt_text": prompt_text,
                    "kind": "approval",
                    "approval_id": approval_id,
                    "approval_kind": approval_kind,
                    "reason": request.reason,
                    "preview": request.preview,
                    "files": list(request.files),
                    "command": request.command,
                    "metadata": dict(request.metadata),
                    "scope": None,
                    "allow_for_session_supported": False,
                    "allow_for_session_scope": None,
                    "allow_for_session_warning": warning,
                },
            )
        )
        self._emit_event(
            ProtocolPayloadEvent(
                "prompt_for_input",
                {
                    "kind": "approval_result",
                    "approval_id": approval_id,
                    "status": "denied",
                    "allow": False,
                    "allow_for_session": False,
                    "allow_for_session_supported": False,
                    "metadata": {
                        "status": "denied",
                        "allow": False,
                        "allow_for_session": False,
                        "allow_for_session_supported": False,
                    },
                },
            )
        )
        return ApprovalDecision(allow=False)
