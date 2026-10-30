from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_computer_use_recovery import _ScriptedComputerUseClient

from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent_loop import create_session
from alysis_code.computer_use_call_diagnostics import (
    ComputerUseCallTrace,
    bind_computer_use_provider_evidence,
    note_computer_use_provider_evidence,
    sanitize_call_diagnostic,
    sanitize_computer_use_evidence,
)
from alysis_code.config import AppConfig
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


class CaptureSurface(NoopSurface):
    def __init__(self):
        self.records = []
        self.evidence = []

    def emit_computer_use_call_diagnostic(self, payload):
        self.records.append(sanitize_call_diagnostic(payload))

    def emit_computer_use_evidence(self, payload):
        self.evidence.append(payload)


def test_exception_chain_and_last_local_boundary_do_not_retain_prose():
    surface = CaptureSurface()

    def host(_name, _args):
        try:
            raise ValueError("secret cause /Users/private/target")
        except ValueError as exc:
            raise RuntimeError("secret outer prompt") from exc

    tool = build_computer_use_tools(host)["computer_state"]
    trace = ComputerUseCallTrace(surface, 3, "private-wire-call", "computer_state")
    with pytest.raises(RuntimeError):
        trace.run(tool.run, {})
    errors = [r for r in surface.records if r["callPhase"] == "exception"]
    assert [r["exceptionClass"] for r in errors] == ["RuntimeError", "ValueError"]
    assert [r["chainRelation"] for r in errors] == ["root", "cause"]
    assert all(r["boundary"] == "host_request" for r in errors)
    assert errors[0]["exceptionSite"] == "computer_use_tools"
    assert errors[0]["exceptionLine"] > 0
    assert "secret" not in json.dumps(surface.records)
    assert "private" not in json.dumps(surface.records)
    # Context was reset even though the tool threw.
    before = len(surface.records)
    build_computer_use_tools(lambda *_: {"snapshot_id": "next"})["computer_state"].run({})
    assert len(surface.records) == before


@pytest.mark.parametrize("failure", ["arguments", "exception"])
def test_successful_input_then_local_failure_has_distinct_correlated_evidence(
    tmp_path: Path, failure
):
    surface = CaptureSurface()
    calls = []

    def host(name, arguments):
        calls.append(name)
        if name == "computer_action":
            return {
                "receipt": {"action": "set_value", "performed": True},
                "state": {"snapshot_id": "after"},
            }
        if len(calls) == 3:
            raise ValueError("private terminal cause")
        return {"snapshot_id": "before"}

    def call(name, args, index):
        return LLMResponse(
            content="", raw={}, tool_calls=[ToolCall(id=str(index), name=name, arguments=args)]
        )

    client = _ScriptedComputerUseClient(
        [
            call("computer_state", {}, 1),
            call(
                "computer_action",
                {"action": "set_value", "element_id": "private-id", "text": "private-text"},
                2,
            ),
            call(
                "computer_state",
                {"assertions": "private-invalid"} if failure == "arguments" else {},
                3,
            ),
            # A local argument refusal sends nothing and earns two bounded corrections.
            # Distinct invalid reads avoid the identical-call guard; all three are refused.
            call("computer_state", {"assertions": ["private-invalid-2"]}, 4),
            call("computer_state", {"assertions": [{"role": "private-invalid-3"}]}, 5),
        ]
    )
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host,
    )
    session.client = client
    try:
        assert session.run_turn("Enter the diagnostic draft and leave it unsent.") == 1
    finally:
        session.close()
    results = [r for r in surface.records if r["callPhase"] == "result"]
    refused = 3 if failure == "arguments" else 1
    assert [(r["callOrdinal"], r["tool"], r["result"]) for r in results] == [
        (1, "computer_state", "returned"),
        (2, "computer_action", "returned"),
        *[(3 + i, "computer_state", "failed") for i in range(refused)],
    ]
    terminal = [r for r in surface.records if r["callPhase"] == "turn_failure"]
    assert len(terminal) == 1 and terminal[0]["callOrdinal"] == 2 + refused
    assert results[-1]["boundary"] == (
        "local_arguments" if failure == "arguments" else "host_request"
    )
    assert results[-1]["resultCode"] == (
        "invalid_observation_assertions" if failure == "arguments" else "other"
    )
    assert calls == (
        ["computer_state", "computer_action"]
        if failure == "arguments"
        else ["computer_state", "computer_action", "computer_state"]
    )
    assert "private" not in json.dumps(surface.records)


def test_protocol_surface_binds_wire_identity_and_scrubs_extras():
    events = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="session", run_id=None, job_id="job"), emit=events.append
    )
    surface.emit_tool_call_started("call", "computer_state", "{}")
    payload = {
        "version": 1,
        "callOrdinal": 4,
        "call_id": "call",
        "tool": "computer_state",
        "callPhase": "begin",
        "boundary": "turn_admission",
        "message": "private",
        "arguments": {"secret": "text"},
    }
    surface.emit_computer_use_call_diagnostic({**payload, "call_id": "other"})
    surface.emit_computer_use_call_diagnostic(payload)
    surface.emit_tool_call_completed("call", False, "{}")
    surface.emit_computer_use_call_diagnostic({**payload, "callPhase": "turn_failure"})
    diagnostic = [e for e in events if e["type"] == "computer_use_call_diagnostic"]
    assert len(diagnostic) == 2
    assert all(e["session_id"] == "session" and e["job_id"] == "job" for e in diagnostic)
    assert all(e["payload"]["call_id"] == "call" for e in diagnostic)
    assert "private" not in json.dumps(diagnostic) and "secret" not in json.dumps(diagnostic)


def test_diagnostic_sink_failure_does_not_change_task_results():
    class BrokenSurface:
        def emit_computer_use_call_diagnostic(self, _payload):
            raise RuntimeError("sink unavailable")

    trace = ComputerUseCallTrace(BrokenSurface(), 1, "call", "computer_state")
    result = trace.run(lambda _: {"snapshot_id": "normal"}, {})
    trace.finish(result)
    assert result == {"snapshot_id": "normal"}


@pytest.mark.parametrize(
    "result, code",
    [
        (
            {"error": "computer_use_route_changed", "message": "private"},
            "computer_use_route_changed",
        ),
        ({"error": "computer_use_lease_expired"}, "computer_use_lease_expired"),
        ({"error": "computer_use_broker_changed"}, "computer_use_broker_changed"),
        (
            {"error": "private prose", "error_code": "invalid_tool_arguments_json"},
            "invalid_tool_arguments_json",
        ),
        (
            {"error": "private prose", "error_code": "repeated_tool_failure_guard"},
            "repeated_tool_failure_guard",
        ),
        ({"error": "private prose", "error_code": "private_unknown"}, "other"),
        ({"error": "private_unknown_host_code"}, "other"),
    ],
)
def test_result_keeps_closed_host_and_guard_codes_only(result, code):
    surface = CaptureSurface()
    trace = ComputerUseCallTrace(surface, 1, "call", "computer_state")
    trace.finish(result)
    final = surface.records[-1]
    assert (final["result"], final["resultCode"]) == ("failed", code)
    assert "private" not in json.dumps(surface.records)


def _observation_evidence(**extra):
    return {
        "version": 1,
        "kind": "observation",
        "source": "engine_projection",
        "observationOrdinal": 1,
        "tool": "computer_state",
        "projectionState": "returned",
        "imageRequested": False,
        **extra,
    }


@pytest.mark.parametrize(
    "record",
    [
        _observation_evidence(),
        _observation_evidence(
            elementCount=3,
            ordinaryCount=1,
            protectedCount=1,
            unknownCount=1,
            pressEligibleCount=1,
            writableTextEligibleCount=0,
        ),
        {
            "version": 1,
            "kind": "provider",
            "providerOrdinal": 1,
            "attemptOrdinal": 0,
            "generationRole": "protected",
            "phase": "context_projected",
            "projectedImageCount": 1,
        },
        {
            "version": 1,
            "kind": "provider",
            "providerOrdinal": 1,
            "attemptOrdinal": 1,
            "generationRole": "protected",
            "phase": "transport_attempted",
            "transportImageCount": 1,
        },
        {
            "version": 1,
            "kind": "terminal",
            "terminalOrigin": "model_report",
            "reportedOutcome": "blocked",
            "stateCalls": 2,
            "actionCalls": 0,
            "knownHostFailure": False,
        },
        {
            "version": 1,
            "kind": "timing",
            "spanOrdinal": 1,
            "timingScope": "engine",
            "generationRole": "protected",
            "span": "provider_call",
            "phase": "end",
            "durationMs": 4,
            "outcome": "returned",
        },
    ],
)
def test_general_evidence_strict_valid_union_copies_scalars(record):
    copied = sanitize_computer_use_evidence(record)
    assert copied == record and copied is not record


@pytest.mark.parametrize(
    "extra",
    [
        {"image": "data:image/png;base64,PRIVATE"},
        {"endpoint": "https://private.invalid"},
        {"snapshot_id": "private"},
        {"observationOrdinal": True},
        {"observationOrdinal": 0},
        {"elementCount": 1},
        {"imageRequested": 1},
        {"preparedImageCount": float("nan")},
        {"preparedImageCount": 0.5},
        {"preparedImageCount": 100001},
        {"focusedType": "private text"},
        {"projectionState": "withheld", "focusedKey": "unknown"},
        {"projectionState": "historical", "captureState": "present"},
        {"projectionState": "historical", "focusedKey": "available"},
        {"projectionState": "historical", "preparedImageCount": 1},
        {"projectionState": "historical", "preparedImageCount": 0},
        {"projectionState": "historical", "captureState": "absent"},
        {"projectionState": "historical", "imageReadiness": "ready"},
        {"projectionState": "withheld", "imageReadiness": "ready"},
        {
            "elementCount": 1,
            "ordinaryCount": 0,
            "protectedCount": 0,
            "unknownCount": 0,
            "pressEligibleCount": 0,
            "writableTextEligibleCount": 0,
        },
        {
            "elementCount": 1,
            "ordinaryCount": 0,
            "protectedCount": 0,
            "unknownCount": 1,
            "pressEligibleCount": 1,
            "writableTextEligibleCount": 0,
        },
    ],
)
def test_general_evidence_rejects_private_malformed_and_cross_field_values(extra):
    assert sanitize_computer_use_evidence(_observation_evidence(**extra)) is None


@pytest.mark.parametrize("phase", [[], {}])
@pytest.mark.parametrize("attempt", [0, 1])
def test_general_evidence_rejects_unhashable_provider_phase(phase, attempt):
    assert (
        sanitize_computer_use_evidence(
            {
                "version": 1,
                "kind": "provider",
                "providerOrdinal": 1,
                "attemptOrdinal": attempt,
                "generationRole": "protected",
                "phase": phase,
            }
        )
        is None
    )


@pytest.mark.parametrize("value", [[], {}])
def test_general_evidence_rejects_unhashable_enums_in_each_union_branch(value):
    records = [
        _observation_evidence(projectionState=value),
        _observation_evidence(surfaceState=value),
        {
            "version": 1,
            "kind": "provider",
            "providerOrdinal": 1,
            "attemptOrdinal": 0,
            "generationRole": value,
            "phase": "context_projected",
        },
        {
            "version": 1,
            "kind": "terminal",
            "terminalOrigin": value,
            "reportedOutcome": "blocked",
            "stateCalls": 2,
            "actionCalls": 0,
            "knownHostFailure": False,
        },
        {
            "version": 1,
            "kind": "timing",
            "spanOrdinal": 1,
            "timingScope": "engine",
            "generationRole": "protected",
            "span": "provider_call",
            "phase": value,
        },
    ]
    for record in records:
        assert sanitize_computer_use_evidence(record) is None


@pytest.mark.parametrize("value", [[], {}])
def test_private_evidence_helpers_treat_unhashable_enums_as_unknown(value):
    from alysis_code.computer_use_call_diagnostics import (
        _count_provider_images,
        _readiness_evidence,
        note_observation_projection,
    )

    assert _readiness_evidence({"state": value})["imageReadiness"] == "unknown"
    assert _count_provider_images({"input": [{"content": [{"type": value}]}]}) is None
    surface = CaptureSurface()
    state = {
        "elements": [
            {
                "security_state": "ordinary",
                "secure": False,
                "content_redacted": False,
                "enabled": True,
                "role": value,
                "value_settable": True,
            }
        ]
    }
    trace = ComputerUseCallTrace(surface, 1, "private", "computer_state")
    trace.run(
        lambda _: note_observation_projection(
            tool="computer_state", result=state, image_requested=False
        ),
        {},
    )
    assert len(surface.evidence) == 1
    assert surface.evidence[0]["ordinaryCount"] == 1
    assert surface.evidence[0]["writableTextEligibleCount"] == 0


def test_general_evidence_provider_partial_scope_is_private_bounded_and_noop():
    records = []
    good = {"phase": "transport_attempted", "attemptOrdinal": 1, "transportImageCount": 1}
    note_computer_use_provider_evidence(good)
    with bind_computer_use_provider_evidence(records.append):
        note_computer_use_provider_evidence(good)
        note_computer_use_provider_evidence({**good, "request": "private"})
        note_computer_use_provider_evidence({**good, "providerOrdinal": 2})
        note_computer_use_provider_evidence({**good, "attemptOrdinal": True})
        with bind_computer_use_provider_evidence(None):
            note_computer_use_provider_evidence(good)
    assert records == [good] and records[0] is not good

    def broken(_record):
        raise RuntimeError("private emitter failure")

    with bind_computer_use_provider_evidence(broken):
        note_computer_use_provider_evidence(good)


def test_outer_projection_reports_final_counts_without_private_content():
    ordinary = {
        "security_state": "ordinary",
        "secure": False,
        "content_redacted": False,
        "role": "AXTextField",
        "enabled": True,
        "actions": ["AXPress"],
        "value_settable": True,
        "id": "private-id",
        "label": "private-label",
    }
    state = {
        "interaction_version": 2,
        "snapshot_id": "private-snapshot",
        "elements": [
            ordinary,
            {"security_state": "protected", "secure": True, "content_redacted": True},
            {"security_state": "ordinary", "secure": True, "content_redacted": False},
        ],
        "focused_target": {
            "available": True,
            "key": True,
            "type": True,
            "recipient_ref": "recipient_private-snapshot",
        },
        "coverage": {"complete": False},
        "truncated": True,
        "selected_surface": "private-surface",
        "surface_ref": "private-surface",
    }
    surface = CaptureSurface()
    trace = ComputerUseCallTrace(surface, 7, "private-call", "computer_state")
    tools = build_computer_use_tools(lambda *_: state)
    trace.run(tools["computer_state"].run, {})
    assert len(surface.evidence) == 1
    actual = surface.evidence[0]
    assert (
        actual["elementCount"],
        actual["ordinaryCount"],
        actual["protectedCount"],
        actual["unknownCount"],
    ) == (3, 1, 1, 1)
    assert (actual["pressEligibleCount"], actual["writableTextEligibleCount"]) == (1, 1)
    assert actual["coverageState"] == "partial" and actual["surfaceState"] == "selected"
    assert actual["focusedType"] == "available" and actual["observationOrdinal"] == 7
    assert "private" not in json.dumps(actual)


def test_viewer_readiness_is_not_carried_to_next_non_image_observation(tmp_path):
    from copy import deepcopy

    from test_computer_interaction_v2 import image_state
    from test_embedded_browser_visual import visual_cfg

    from alysis_code.embedded_browser_visual import ComputerUseViewer

    raw = image_state()

    def host(_name, args):
        result = deepcopy(raw)
        if not args.get("include_image"):
            result.pop("image")
            result.pop("image_frame")
            result["visual"] = {"status": "unavailable", "reason": "private-host-reason"}
        return result

    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg())
    tools = build_computer_use_tools(
        host, observation_available=True, interaction_v2=True, viewer=viewer
    )
    surface = CaptureSurface()
    for ordinal, args in enumerate(({"include_image": True}, {"include_image": False}), 1):
        ComputerUseCallTrace(surface, ordinal, f"private-{ordinal}", "computer_state").run(
            tools["computer_state"].run, args
        )
    assert surface.evidence[0]["effectiveVision"] == "available"
    second = surface.evidence[1]
    assert second["imageRequested"] is False
    assert (
        not {"imageReadiness", "effectiveVision", "decoderAvailable", "capabilitySource"}
        & second.keys()
    )
    assert "private" not in json.dumps(surface.evidence)


def test_historical_projection_has_no_current_image_facts_and_focus_requires_exact_recipient():
    from alysis_code.computer_use_call_diagnostics import note_observation_projection

    surface = CaptureSurface()
    state = {
        "interaction_version": 2,
        "snapshot_id": "snapshot-x",
        "elements": [],
        "target": {"application_status": "terminated"},
        "focused_target": {"available": False},
        "coverage": {"complete": True},
        "truncated": False,
    }
    trace = ComputerUseCallTrace(surface, 1, "private", "computer_state")
    trace.run(
        lambda _: note_observation_projection(
            tool="computer_state",
            result=state,
            image_requested=True,
            readiness={"_projection_state": "historical"},
        ),
        {},
    )
    historical = surface.evidence[0]
    assert historical["projectionState"] == "historical"
    assert (
        not {"captureState", "frameBinding", "preparedImageCount", "projectedImageCount"}
        & historical.keys()
    )
    state["focused_target"] = {
        "available": True,
        "key": True,
        "type": True,
        "recipient_ref": "wrong",
    }
    state["elements"] = [
        {
            "security_state": "ordinary",
            "secure": False,
            "content_redacted": False,
            "enabled": True,
            "role": "AXSearchField",
            "value_settable": True,
        }
    ]
    trace = ComputerUseCallTrace(surface, 2, "private", "computer_state")
    trace.run(
        lambda _: note_observation_projection(
            tool="computer_state", result=state, image_requested=False
        ),
        {},
    )
    assert surface.evidence[1]["focusedType"] == "unknown"
    assert surface.evidence[1]["writableTextEligibleCount"] == 1
