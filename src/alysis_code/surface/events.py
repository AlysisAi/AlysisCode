"""
Engine event inventory (migration targets; current call sites are intentionally unchanged):
- message_delta: assistant streaming tokens from agent_loop.py and plan_assistant.py via
  on_text_delta callbacks, including Forge planner streaming in cli_impl/chat.py.
- message_end: final assistant messages from agent_loop.py and Forge planner completion
  flows in cli_impl/chat.py.
- reasoning_started / reasoning_delta / reasoning_ended: provider reasoning display text
  (safe summaries or provider-returned reasoning) from the agent core's reasoning sink.
  Only the IDE bridge surface emits them, and only for sessions whose host opted in with
  session.create `reasoning_trace` while the trace level is not `off`.
- tool_call_started: tool lifecycle starts from agent_loop.py for built-ins, custom tools,
  non-repo tool-assisted turns, and nested subagent tool forwarding through hidden_surface.py.
- tool_call_progress: tool output chunks/previews from agent_loop.py and summarization helpers
  in tools/registry.py.
- tool_call_completed: tool lifecycle completion/failure from agent_loop.py, including hook
  blocks, repeated-call guards, verify_run, shell, git, web, MCP/custom tool failures.
- status_update: startup/session status in agent_loop.py plus command/status HUD text in
  cli_impl/chat.py.
- mode_changed: /permissions, persona clamps, /ask one-turn overrides, and Forge UI
  transitions in cli_impl/chat.py.
- persona_changed: persona-mode transitions (user-initiated /persona switches and
  approved model switch_mode proposals) applied through the chat loop's persona primitive.
- plan_node_updated: task creation/status/validation/reconciliation updates in forge.py,
  cli_impl/forge.py, cli_impl/chat.py, plan_assistant.py, and plan_reconciliation.py.
- swarm_worker_state_changed: scheduler batches, worker task starts/ends, merge/retry state,
  and dry-run output in swarm_orchestrator.py.
- verify_gate_result: verify_run tool payloads in agent_loop.py and command execution/results
  in verify_gate.py and cli_impl/forge.py.
- review_gate_decision: review approvals, blocking issues, and changes_requested status in
  review_gate.py and cli_impl/forge.py.
- error_raised: agent/runtime/LLM/tool/config/Forge errors surfaced through on_error,
  console.print red paths, and raised domain errors in agent_loop.py, cli_impl/chat.py,
  cli_impl/forge.py, tools/*.py, verify_gate.py, and review_gate.py.
- warning_emitted: model metadata warnings, plan validation/reconciliation warnings, hook
  notices, unknown command/usage warnings, repeated tool guards, and verify/review warnings.
- info_emitted: progress/thinking lines, planner assistant meta text, saved artifact paths,
  subagent summaries, usage/context/HUD lines, dry-run summaries, and feedback bundle paths.
- prompt_for_input: approval prompts in RichSurface.request_approval, Forge pickers in
  cli_impl/chat.py, workspace/model/menu selectors, and interactive chat input.
- config_form_request: future structured menus/forms replacing /config, setup, model metadata,
  toolbar, usage HUD, subagents, skills, and workspace binding command surfaces.
"""

from __future__ import annotations

import math
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, ClassVar, Literal, TypeAlias, get_args

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
MAX_ARGUMENTS_PREVIEW_CHARS = 500


def _truncate_preview(text: str, *, max_chars: int = MAX_ARGUMENTS_PREVIEW_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def _json_safe(value: Any) -> JsonValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event values must be finite JSON numbers")
        return value
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("event dict keys must be strings")
            out[key] = _json_safe(item)
        return out
    raise ValueError(f"event value is not JSON-safe: {type(value).__name__}")


def _to_dict(event: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"type": event.type}
    for event_field in fields(event):
        data[event_field.name] = _json_safe(getattr(event, event_field.name))
    return data


@dataclass(frozen=True, slots=True)
class MessageDelta:
    type: ClassVar[Literal["message_delta"]] = "message_delta"
    text: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class MessageEnd:
    type: ClassVar[Literal["message_end"]] = "message_end"
    text: str = ""
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ReasoningStarted:
    type: ClassVar[Literal["reasoning_started"]] = "reasoning_started"
    block_id: str

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    type: ClassVar[Literal["reasoning_delta"]] = "reasoning_delta"
    block_id: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ReasoningEnded:
    type: ClassVar[Literal["reasoning_ended"]] = "reasoning_ended"
    block_id: str

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    type: ClassVar[Literal["tool_call_started"]] = "tool_call_started"
    call_id: str
    name: str
    arguments_preview: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = _to_dict(self)
        data["arguments_preview"] = _truncate_preview(str(data["arguments_preview"]))
        return data


@dataclass(frozen=True, slots=True)
class ToolCallProgress:
    type: ClassVar[Literal["tool_call_progress"]] = "tool_call_progress"
    call_id: str
    text: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    type: ClassVar[Literal["tool_call_completed"]] = "tool_call_completed"
    call_id: str
    success: bool
    result_preview: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    type: ClassVar[Literal["status_update"]] = "status_update"
    tokens_in: int | None = None
    tokens_out: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    mode: str | None = None
    model: str | None = None
    step: int | None = None
    step_budget: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ModeChanged:
    type: ClassVar[Literal["mode_changed"]] = "mode_changed"
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class PersonaChanged:
    type: ClassVar[Literal["persona_changed"]] = "persona_changed"
    persona: str
    effective_mode: str
    source: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class PlanNodeUpdated:
    type: ClassVar[Literal["plan_node_updated"]] = "plan_node_updated"
    node_id: str
    state: str
    summary: str | None = None
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class SwarmWorkerStateChanged:
    type: ClassVar[Literal["swarm_worker_state_changed"]] = "swarm_worker_state_changed"
    worker_id: str
    state: str
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class SubagentStateChanged:
    """Read-only lifecycle projection for one model-managed child turn.

    ``subagent_run_id`` is correlation only, never an authority token.  The
    owning IDE job remains the sole cancellation/approval boundary.
    """

    type: ClassVar[Literal["subagent_state_changed"]] = "subagent_state_changed"
    subagent_run_id: str
    name: str
    mode: str
    state: str
    label: str = ""
    subagent_session_id: str | None = None
    description: str = ""
    elapsed_ms: int | None = None
    steps_completed: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class VerifyGateResult:
    type: ClassVar[Literal["verify_gate_result"]] = "verify_gate_result"
    command: str
    success: bool
    summary: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ApprovalReviewed:
    """The approval reviewer ("Approve for me") answered an approval instead of a person.

    ``outcome`` is ``allow``, ``deny`` or ``ask_person`` (it handed the approval to the
    person, whose prompt follows as a normal ``prompt_for_input``).
    """

    type: ClassVar[Literal["approval_reviewed"]] = "approval_reviewed"
    outcome: str
    subject: str
    reason: str = ""
    risk: str = ""
    approval_kind: str = ""
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ReviewGateDecision:
    type: ClassVar[Literal["review_gate_decision"]] = "review_gate_decision"
    decision: str
    summary: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ErrorRaised:
    type: ClassVar[Literal["error_raised"]] = "error_raised"
    code: str
    message: str
    recoverable: bool
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class WarningEmitted:
    type: ClassVar[Literal["warning_emitted"]] = "warning_emitted"
    message: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class InfoEmitted:
    type: ClassVar[Literal["info_emitted"]] = "info_emitted"
    message: str
    worker_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class PromptForInput:
    type: ClassVar[Literal["prompt_for_input"]] = "prompt_for_input"
    prompt_id: str
    prompt_text: str
    kind: str
    approval_kind: str | None = None
    approval_id: str | None = None
    reason: str | None = None
    preview: str | None = None
    files: list[str] = field(default_factory=list)
    command: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None
    allow_for_session_supported: bool | None = None
    allow_for_session_scope: dict[str, Any] | None = None
    allow_for_session_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ConfigFormRequest:
    type: ClassVar[Literal["config_form_request"]] = "config_form_request"
    form_id: str
    schema: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


Event: TypeAlias = (
    MessageDelta
    | MessageEnd
    | ReasoningStarted
    | ReasoningDelta
    | ReasoningEnded
    | ToolCallStarted
    | ToolCallProgress
    | ToolCallCompleted
    | StatusUpdate
    | ModeChanged
    | PersonaChanged
    | PlanNodeUpdated
    | SwarmWorkerStateChanged
    | SubagentStateChanged
    | VerifyGateResult
    | ReviewGateDecision
    | ApprovalReviewed
    | ErrorRaised
    | WarningEmitted
    | InfoEmitted
    | PromptForInput
    | ConfigFormRequest
)

EVENT_REGISTRY: dict[str, type] = {event_cls.type: event_cls for event_cls in get_args(Event)}


def event_from_dict(data: dict[str, Any]) -> Event:
    event_type = str(data.get("type") or "")
    event_cls = EVENT_REGISTRY.get(event_type)
    if event_cls is None:
        raise ValueError(f"Unknown surface event type: {event_type or '(missing)'}")

    event_fields = fields(event_cls)
    allowed_keys = {field.name for field in event_fields}
    extra_keys = sorted(set(data) - allowed_keys - {"type"})
    if extra_keys:
        raise ValueError(
            f"Unexpected field(s) for surface event {event_type}: {', '.join(extra_keys)}"
        )

    missing_keys = [
        field.name
        for field in event_fields
        if field.default is MISSING and field.default_factory is MISSING and field.name not in data
    ]
    if missing_keys:
        raise ValueError(
            f"Missing field(s) for surface event {event_type}: {', '.join(missing_keys)}"
        )

    kwargs = {field.name: data[field.name] for field in event_fields if field.name in data}
    return event_cls(**kwargs)
