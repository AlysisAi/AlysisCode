from __future__ import annotations

import io
import json
from typing import get_args

from rich.console import Console

from alysis_code.surface.events import (
    EVENT_REGISTRY,
    MAX_ARGUMENTS_PREVIEW_CHARS,
    ApprovalReviewed,
    ConfigFormRequest,
    ErrorRaised,
    Event,
    InfoEmitted,
    MessageDelta,
    MessageEnd,
    ModeChanged,
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
    event_from_dict,
)
from alysis_code.surface.hidden_surface import HiddenApprovalSurface
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.surface.rich_surface import RichSurface


def _sample_events() -> list[Event]:
    return [
        MessageDelta(text="hello", worker_id="w1", role="coder"),
        MessageEnd(worker_id="w1", role="coder"),
        ToolCallStarted(
            call_id="call-1",
            name="fs_read",
            arguments_preview='{"path":"README.md"}',
            worker_id="w1",
            role="coder",
        ),
        ToolCallProgress(call_id="call-1", text="read 10 lines", worker_id="w1", role="coder"),
        ToolCallCompleted(
            call_id="call-1",
            success=True,
            result_preview='{"path":"README.md"}',
            worker_id="w1",
            role="coder",
        ),
        StatusUpdate(
            tokens_in=10,
            tokens_out=5,
            cached_tokens=2,
            cost_usd=0.001,
            mode="review",
            model="test-model",
            step=1,
            step_budget=4,
        ),
        ModeChanged(mode="auto"),
        PlanNodeUpdated(node_id="T1", state="planned", summary="scope ready"),
        SwarmWorkerStateChanged(worker_id="w1", state="running", role="coder"),
        VerifyGateResult(command="pytest -q", success=True, summary="passed"),
        ReviewGateDecision(decision="approved", summary="looks good"),
        ApprovalReviewed(
            outcome="allow",
            subject="npm install",
            reason="Installs the project's dependencies.",
            risk="low",
            approval_kind="shell_run",
        ),
        ErrorRaised(code="config_error", message="missing model", recoverable=True),
        WarningEmitted(message="validation warning"),
        InfoEmitted(message="planner ready"),
        PromptForInput(prompt_id="approval-1", prompt_text="Approve?", kind="confirm"),
        ConfigFormRequest(
            form_id="model-settings",
            schema={"title": "Model settings", "fields": [{"name": "model", "type": "text"}]},
        ),
    ]


def _call_all_emit_methods(surface: object) -> None:
    surface.emit_message_delta("hello", worker_id="w1", role="coder")  # type: ignore[attr-defined]
    surface.emit_message_end(worker_id="w1", role="coder")  # type: ignore[attr-defined]
    surface.emit_tool_call_started(  # type: ignore[attr-defined]
        "call-1",
        "fs_read",
        '{"path":"README.md"}',
        worker_id="w1",
        role="coder",
    )
    surface.emit_tool_call_progress(  # type: ignore[attr-defined]
        "call-1",
        '{"path":"README.md","content":"ok"}',
        worker_id="w1",
        role="coder",
    )
    surface.emit_tool_call_completed(  # type: ignore[attr-defined]
        "call-1",
        True,
        '{"path":"README.md","content":"ok"}',
        worker_id="w1",
        role="coder",
    )
    surface.emit_status_update(  # type: ignore[attr-defined]
        tokens_in=10,
        tokens_out=5,
        cached_tokens=2,
        cost_usd=0.001,
        mode="review",
        model="test-model",
        step=1,
        step_budget=4,
    )
    surface.emit_mode_changed("auto")  # type: ignore[attr-defined]
    surface.emit_plan_node_updated(  # type: ignore[attr-defined]
        "T1",
        "planned",
        "scope ready",
        worker_id="w1",
        role="coder",
    )
    surface.emit_swarm_worker_state_changed(  # type: ignore[attr-defined]
        "w1",
        "running",
        role="coder",
    )
    surface.emit_verify_gate_result(  # type: ignore[attr-defined]
        "pytest -q",
        True,
        "passed",
        worker_id="w1",
        role="coder",
    )
    surface.emit_review_gate_decision(  # type: ignore[attr-defined]
        "approved",
        "looks good",
        worker_id="w1",
        role="coder",
    )
    surface.emit_approval_reviewed(  # type: ignore[attr-defined]
        "deny",
        "git push --force",
        "The user did not ask to rewrite remote history.",
        risk="high",
        approval_kind="shell_run",
        worker_id="w1",
        role="coder",
    )
    surface.emit_error(  # type: ignore[attr-defined]
        "config_error",
        "missing model",
        True,
        worker_id="w1",
        role="coder",
    )
    surface.emit_warning("validation warning", worker_id="w1", role="coder")  # type: ignore[attr-defined]
    surface.emit_info("planner ready", worker_id="w1", role="coder")  # type: ignore[attr-defined]
    surface.emit_prompt_for_input("approval-1", "Approve?", "confirm")  # type: ignore[attr-defined]
    surface.emit_config_form_request(  # type: ignore[attr-defined]
        "model-settings",
        {"title": "Model settings", "fields": [{"name": "model", "type": "text"}]},
    )
    for event in _sample_events():
        surface.emit(event)  # type: ignore[attr-defined]


def test_surface_events_round_trip() -> None:
    for event in _sample_events():
        data = event.to_dict()
        json.dumps(data)
        assert event_from_dict(data) == event


def test_tool_call_started_to_dict_truncates_arguments_preview() -> None:
    event = ToolCallStarted(
        call_id="call-big",
        name="fs_write",
        arguments_preview="x" * (MAX_ARGUMENTS_PREVIEW_CHARS + 50),
    )

    data = event.to_dict()
    round_tripped = event_from_dict(data)

    assert len(data["arguments_preview"]) == MAX_ARGUMENTS_PREVIEW_CHARS
    assert str(data["arguments_preview"]).endswith("...")
    assert round_tripped == ToolCallStarted(
        call_id="call-big",
        name="fs_write",
        arguments_preview=str(data["arguments_preview"]),
    )


def test_surface_event_registry_is_complete() -> None:
    event_classes = set(get_args(Event))

    assert set(EVENT_REGISTRY.values()) == event_classes
    for event_cls in event_classes:
        assert EVENT_REGISTRY[event_cls.type] is event_cls


def test_rich_surface_accepts_all_structured_event_methods() -> None:
    buffer = io.StringIO()
    surface = RichSurface(console=Console(file=buffer, force_terminal=False))

    _call_all_emit_methods(surface)


def test_noop_and_hidden_surfaces_accept_all_structured_event_methods() -> None:
    _call_all_emit_methods(NoopSurface())
    _call_all_emit_methods(HiddenApprovalSurface(None))


def _rich_surface_with_buffer() -> tuple[RichSurface, io.StringIO]:
    buffer = io.StringIO()
    return RichSurface(console=Console(file=buffer, force_terminal=False)), buffer


def test_rich_surface_status_update_renders_dedicated_trace_line() -> None:
    surface, buffer = _rich_surface_with_buffer()

    surface.emit_status_update(
        tokens_in=10,
        tokens_out=5,
        cached_tokens=2,
        cost_usd=0.001,
        mode="review",
        model="test-model",
        step=1,
        step_budget=4,
    )

    rendered = buffer.getvalue()
    assert "Status: " in rendered
    assert "model=test-model" in rendered
    assert "mode=review" in rendered
    assert "step=1/4" in rendered
    assert "in=10" in rendered
    assert "out=5" in rendered
    assert "cached=2" in rendered
    assert "cost=$0.001000" in rendered


def test_rich_surface_status_update_without_fields_renders_updated() -> None:
    surface, buffer = _rich_surface_with_buffer()

    surface.emit_status_update()

    assert "Status: updated" in buffer.getvalue()


def test_rich_surface_mode_changed_renders_dedicated_trace_line() -> None:
    surface, buffer = _rich_surface_with_buffer()

    surface.emit_mode_changed("auto")

    assert "Permissions: auto" in buffer.getvalue()


def test_rich_surface_status_and_mode_suppressed_when_trace_off() -> None:
    surface, buffer = _rich_surface_with_buffer()
    surface.set_trace_level("off")

    surface.emit_status_update(model="test-model")
    surface.emit_mode_changed("auto")

    assert buffer.getvalue() == ""
