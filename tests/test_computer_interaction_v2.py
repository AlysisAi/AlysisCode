"""Production v2 tools, turn recovery and provider-image serialization, with synthetic UI data."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random
from copy import deepcopy

import httpx
import jsonschema
import pytest
from PIL import Image
from test_computer_use_recovery import _ScriptedComputerUseClient
from test_embedded_browser_visual import visual_cfg
from test_ide_computer_use_host import _lab_state, _RecordingSurface

from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.embedded_browser_visual import (
    MAX_COMPUTER_USE_IMAGE_BYTES,
    ComputerUseViewer,
    EmbeddedBrowserViewer,
)
from alysis_code.ide.protocol import MAX_REQUEST_BYTES, parse_request_line
from alysis_code.llm.openai_responses import _responses_input_from_messages
from alysis_code.llm.types import LLMResponse, ToolCall

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def run_general_evidence_full_turn_fixture(root, host_request=None, *, variant="positive"):
    """Executable contract fixture, also usable by Pilot's joined JS controller bridge.

    The model responses are scripted HTTP bytes. Tools, core, viewer, serializer,
    real subscription adaptation and the selected MockTransport are production.
    """
    from alysis_code.cancellation import CooperativeCancellationError, InteractiveCancellationToken
    from alysis_code.computer_use_call_diagnostics import sanitize_computer_use_evidence
    from alysis_code.llm.openai_responses import OpenAIResponsesClient
    from alysis_code.provider_auth import ProviderRequestAuthorization
    from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth

    class Surface(_RecordingSurface):
        def __init__(self):
            super().__init__()
            self.evidence = []
            self.computer_use_evidence_version = 1

        def emit_computer_use_evidence(self, value):
            safe = sanitize_computer_use_evidence(value)
            if safe is not None:
                self.evidence.append(safe)

        def bind_computer_use_evidence_emitter(self):
            return self.emit_computer_use_evidence

    class DropImagesAuth(OpenAICodexSubscriptionAuth):
        def adapt_responses_payload(self, value):
            adapted = super().adapt_responses_payload(value)
            for item in adapted.get("input", []):
                if isinstance(item, dict) and isinstance(item.get("content"), list):
                    item["content"] = [
                        part for part in item["content"] if part.get("type") != "input_image"
                    ]
            return adapted

    surface = Surface()
    token = InteractiveCancellationToken()
    expected_pixels = []
    image_counts = []
    tool_requests = []
    pixel_matches = []

    def default_host(name, args):
        assert name == "computer_state"
        result = image_state() if args.get("include_image") else state(len(tool_requests))
        result["snapshot_id"] = f"snapshot-{len(tool_requests)}"
        if "image_frame" in result:
            result["image_frame"]["snapshot_id"] = result["snapshot_id"]
        return result

    def host(name, args):
        tool_requests.append((name, dict(args)))
        result = (host_request or default_host)(name, args)
        if isinstance(result, dict) and isinstance(result.get("image"), dict):
            expected_pixels.append(result["image"]["data"])
            if variant == "cancel_before_transport":
                token.cancel()
        return result

    def handler(request):
        payload = json.loads(request.content)
        images = [
            part
            for item in payload.get("input", [])
            if isinstance(item, dict)
            for part in (item.get("content") if isinstance(item.get("content"), list) else [])
            if part.get("type") == "input_image"
        ]
        image_counts.append(len(images))
        for part in images:
            # Observe exact bytes at the actual transport without retaining Request/body.
            pixel_matches.append(
                any(part.get("image_url", "").endswith(data) for data in expected_pixels)
            )
        number = len(image_counts)
        if number <= 2:
            args = {} if number == 1 else {"include_image": True, "max_elements": 150}
            output = [
                {
                    "type": "function_call",
                    "call_id": f"fixture-{number}",
                    "name": "computer_state",
                    "arguments": json.dumps(args),
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "outcome": "blocked",
                                    "summary": "The fixture reported an unresolved prerequisite.",
                                }
                            ),
                        }
                    ],
                }
            ]
            if variant == "cancel_after_transport":
                token.cancel()
        event = {
            "type": "response.completed",
            "response": {
                "id": f"response-{number}",
                "status": "completed",
                "model": "gpt-6.1-sol",
                "output": output,
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode(),
        )

    transport = httpx.MockTransport(handler)
    auth = (DropImagesAuth if variant == "adapter_drop" else OpenAICodexSubscriptionAuth)(
        transport=transport
    )
    # Real payload adaptation, mock credentials only; never consult a live account/vault.
    auth.route_credential_scope = lambda: "fixture-scope"
    auth.authorize_request = lambda *a, **k: ProviderRequestAuthorization(
        credential_scope="fixture-scope", headers={}
    )
    cfg = AppConfig(
        model="gpt-6.1-sol",
        base_url="https://api.openai.com/v1",
        routing_mode="code_only",
        stream=False,
    )
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {
            "gpt-6.1-sol": {
                "supports_vision": variant != "unsupported",
                "context_window_tokens": 128000,
                "max_output_tokens": 1024,
            }
        }
    }
    session = create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=True,
        api_key_override="fixture-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host,
        computer_interaction_v2=True,
        computer_observation_available=True,
    )
    session.client = OpenAIResponsesClient(
        base_url=cfg.base_url, api_key="", model=cfg.model, provider_auth=auth, transport=transport
    )
    outcome = "returned"
    exit_code = None
    try:
        try:
            exit_code = session.run_turn(
                "Observe the permitted target and report the actual limitation.",
                cancellation_token=token,
            )
        except (CooperativeCancellationError, KeyboardInterrupt):
            outcome = "cancelled"
        viewer = session.tools["computer_state"].visual_delivery
        scrubbed = (
            not viewer._pending
            and not viewer._sensitive_inflight
            and all(data not in json.dumps(session.messages) for data in expected_pixels)
        )
        if token.is_cancelled:
            outcome = "cancelled"
    finally:
        session.close()
    return {
        "exitCode": exit_code,
        "outcome": outcome,
        "imageCounts": image_counts,
        "exactPixelsMatched": all(pixel_matches) and bool(pixel_matches),
        "scrubbed": scrubbed,
        "hostCalls": [name for name, _ in tool_requests],
        "forwardedImage": [args.get("include_image") is True for _, args in tool_requests],
        "evidence": surface.evidence,
    }


@pytest.mark.parametrize(
    "variant",
    [
        "positive",
        "adapter_drop",
        "unsupported",
        "cancel_before_transport",
        "cancel_after_transport",
    ],
)
def test_real_turn_native_pixels_reach_real_auth_mock_transport_and_are_scrubbed(tmp_path, variant):
    result = run_general_evidence_full_turn_fixture(tmp_path, variant=variant)
    assert result["scrubbed"]
    records = result["evidence"]
    provider = [r for r in records if r["kind"] == "provider"]
    assert any(r["phase"] == "serialized" for r in provider)
    if variant == "positive":
        assert result["imageCounts"] == [0, 0, 1] and result["exactPixelsMatched"]
        assert any(r.get("transportImageCount") == 1 for r in provider)
        terminal = [r for r in records if r["kind"] == "terminal"]
        assert len(terminal) == 1 and terminal[0]["terminalOrigin"] == "model_report"
        assert (
            terminal[0]["stateCalls"],
            terminal[0]["actionCalls"],
            terminal[0]["knownHostFailure"],
        ) == (2, 0, False)
    elif variant == "adapter_drop":
        assert result["imageCounts"] == [0, 0, 0]
        assert any(r.get("serializedImageCount") == 1 for r in provider)
        assert not any(r.get("transportImageCount", 0) > 0 for r in provider)
    elif variant == "unsupported":
        assert result["forwardedImage"] == [False, False]
        assert result["imageCounts"] == [0, 0, 0]
        assert any(
            r.get("imageUnavailableReason") == "model_capability_unavailable" for r in records
        )
    elif variant == "cancel_before_transport":
        assert result["imageCounts"] == [0, 0]
    else:
        assert result["imageCounts"] == [0, 0, 1]
        assert result["outcome"] == "cancelled"
    assert all(type(value) in {str, int, bool} for record in records for value in record.values())
    assert "data:image" not in json.dumps(records)


def state(n=1):
    return {
        **_lab_state(f"snapshot-{n}", "READY"),
        "interaction_version": 2,
        "observed_at": "2026-10-15T12:00:00Z",
        "expires_at": "2026-10-15T12:01:00Z",
        "target": {**_lab_state("unused", "READY")["target"], "window_id": "window-1"},
        "surface_ref": f"surface-{n}",
        "selected_surface": f"surface-{n}",
        "focused_surface": f"surface-{n}",
        "surfaces": [{"surface_ref": f"surface-{n}", "role": "AXWindow"}],
        "focused_target": {
            "available": True,
            "key": True,
            "type": True,
            "recipient_ref": f"recipient-{n}",
        },
    }


def call(name, args, n):
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=f"call-{n}", name=name, arguments=args)], raw={}
    )


def done():
    return LLMResponse(
        content=json.dumps({"outcome": "completed_unverified", "summary": "Observed result."}),
        tool_calls=[],
        raw={},
    )


def test_refusal_projection_churn_stops_before_fifth_host_read():
    calls = []

    def host(name, args):
        calls.append((name, args))
        if name == "computer_action":
            return {
                "error": "action_capabilities_unavailable",
                "interaction_feedback": {
                    "dispatch": "not_dispatched",
                    "authority": "valid_for_reobserve",
                    "effect": "unknown",
                },
            }
        return state(len(calls))

    tools = build_computer_use_tools(host, observation_available=True, interaction_v2=True)
    tools["computer_state"].run({})
    tools["computer_action"].run(
        {"action": "press", "target_type": "semantic", "element_id": "one"}
    )
    for args in [
        {"max_elements": 1},
        {"max_elements": 2},
        {"include_image": True},
        {"surface_ref": "surface-new"},
    ]:
        assert "error" not in tools["computer_state"].run(args)
    stopped = tools["computer_wait"].run({"duration_ms": 50})
    assert stopped["error"] == "computer_use_refusal_reads_exhausted"
    assert stopped["interaction_feedback"]["authority"] == "invalid"
    assert len(calls) == 6


def lifecycle_state(n, previous, terminated=True):
    return {
        **state(n),
        "target": {
            **previous["target"],
            **({"application_status": "terminated"} if terminated else {"window_status": "closed"}),
        },
        "application_lifecycle_version": 1,
        "elements": [],
        "element_count": 0,
        "surfaces": [],
        "selection_required": False,
        "focused_target": {"available": False},
        "truncated": False,
        "surface_ref": None,
        "selected_surface": None,
        "focused_surface": None,
        "coverage": {
            "scope": "terminated_application" if terminated else "closed_window",
            "complete": True,
        },
    }


@pytest.mark.parametrize(
    "tamper", [None, "pid", "receipt", "active_frame", "ordinary_action", "boolean_version"]
)
def test_lifecycle_exit_requires_exact_trusted_receipt_and_revokes_input(tamper):
    initial = state(1)
    calls = []

    def host(name, args):
        calls.append((name, args))
        if name == "computer_state":
            return initial
        after = lifecycle_state(2, initial)
        receipt = {
            "action": args["action"],
            "previous_snapshot_id": initial["snapshot_id"],
            "target_bundle_id": initial["target"]["bundle_id"],
            "target_pid": initial["target"]["pid"],
            "lifecycle_status": "application_terminated",
        }
        if tamper == "pid":
            after["target"]["pid"] += 1
        if tamper == "receipt":
            receipt["previous_snapshot_id"] = "wrong"
        if tamper == "active_frame":
            after["image_frame"] = {"frame_id": "old"}
        if tamper == "boolean_version":
            after["application_lifecycle_version"] = True
        return {
            "state": after,
            "receipt": receipt,
            "interaction_feedback": {
                "dispatch": "dispatched",
                "authority": "invalid",
                "effect": "observed_change",
                "cause": "target_application_terminated",
                "phase": "post_observation",
            },
        }

    tools = build_computer_use_tools(host, interaction_v2=True, application_lifecycle_v1=True)
    tools["computer_state"].run({})
    args = (
        {"action": "quit_application"}
        if tamper != "ordinary_action"
        else {"action": "press", "target_type": "semantic", "element_id": "one"}
    )
    result = tools["computer_action"].run(args)
    assert ("error" in result) == (tamper is not None)
    if tamper is None:
        assert calls[-1][1] == {
            "action": "quit_application",
            "application_lifecycle_version": 1,
            "interaction_version": 2,
            "snapshot_id": initial["snapshot_id"],
        }
    repeated = tools["computer_action"].run({"action": "quit_application"})
    if tamper is None:
        assert repeated == result
    else:
        assert repeated["error"] == "computer_use_fresh_observation_required"
    assert len(calls) == 2


def test_quit_schema_and_dispatch_require_negotiated_lifecycle_and_no_target_override():
    calls = []
    for enabled in [False, True]:
        tools = build_computer_use_tools(
            lambda *args: calls.append(args) or state(1),
            interaction_v2=True,
            application_lifecycle_v1=enabled,
        )
        schema = tools["computer_action"].parameters
        assert ("quit_application" in schema["properties"]["action"]["enum"]) is enabled
        tools["computer_state"].run({})
        for extra in [
            {"application": "Other"},
            {"target_type": "semantic"},
            {"pid": 33},
            {"force": True},
            {"application_lifecycle_version": 1},
        ]:
            assert "error" in tools["computer_action"].run({"action": "quit_application", **extra})
    assert len(calls) == 2


def test_real_turn_reports_verified_exit_without_reading_or_replaying(tmp_path):
    initial = state(1)
    calls = []

    def host(name, args):
        calls.append((name, args))
        if name == "computer_state":
            return initial
        return {
            "state": lifecycle_state(2, initial),
            "receipt": {
                "action": "quit_application",
                "previous_snapshot_id": initial["snapshot_id"],
                "target_bundle_id": initial["target"]["bundle_id"],
                "target_pid": initial["target"]["pid"],
                "lifecycle_status": "application_terminated",
            },
            "interaction_feedback": {
                "dispatch": "dispatched",
                "authority": "invalid",
                "effect": "observed_change",
                "cause": "target_application_terminated",
                "phase": "post_observation",
            },
        }

    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=5,
        no_log=True,
        api_key_override="test-key",
        surface=_RecordingSurface(),
        sensitive_media=True,
        computer_use_host_request=host,
        computer_interaction_v2=True,
        computer_application_lifecycle_v1=True,
    )
    session.client = _ScriptedComputerUseClient(
        [
            call("computer_state", {}, 1),
            call("computer_action", {"action": "quit_application"}, 2),
            done(),
        ]
    )
    try:
        assert session.run_turn("Close the selected application normally.") == 0
    finally:
        session.close()
    assert [name for name, _ in calls] == ["computer_state", "computer_action"]


def test_real_turn_refusal_read_limit_stops_without_provider_retry(tmp_path):
    calls = []

    def host(name, args):
        calls.append((name, args))
        if name == "computer_action":
            return {
                "error": "stale_snapshot",
                "interaction_feedback": {
                    "dispatch": "not_dispatched",
                    "authority": "valid_for_reobserve",
                    "effect": "unknown",
                    "cause": "stale_snapshot",
                    "phase": "validation",
                },
            }
        return state(len(calls))

    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=12,
        no_log=True,
        api_key_override="test-key",
        surface=_RecordingSurface(),
        sensitive_media=True,
        computer_use_host_request=host,
        computer_interaction_v2=True,
    )
    client = _ScriptedComputerUseClient(
        [
            call("computer_state", {}, 1),
            call(
                "computer_action",
                {"action": "press", "target_type": "semantic", "element_id": "one"},
                2,
            ),
            *[call("computer_state", {"max_elements": i}, i + 2) for i in range(1, 6)],
            done(),
        ]
    )
    session.client = client
    try:
        assert session.run_turn("Press the selected control.") == 1
    finally:
        session.close()
    assert len(calls) == 6
    assert client.calls == 7


def test_closed_window_retains_only_an_authorized_transition_token():
    initial = state(1)
    calls = []

    def host(name, args):
        calls.append((name, args))
        if len(calls) == 1:
            return initial
        if len(calls) == 2:
            return {
                "state": lifecycle_state(2, initial, terminated=False),
                "receipt": {
                    "action": "press",
                    "previous_snapshot_id": initial["snapshot_id"],
                    "target_bundle_id": initial["target"]["bundle_id"],
                    "target_pid": initial["target"]["pid"],
                    "lifecycle_status": "window_closed",
                },
                "interaction_feedback": {
                    "dispatch": "dispatched",
                    "authority": "valid_for_reobserve",
                    "effect": "observed_change",
                    "cause": None,
                    "phase": "post_observation",
                },
            }
        assert args["action"] == "activate_application"
        assert args["snapshot_id"] == "snapshot-2"
        return {"state": state(3), "receipt": {"action": "activate_application"}}

    tools = build_computer_use_tools(host, interaction_v2=True)
    tools["computer_state"].run({})
    assert "error" not in tools["computer_action"].run(
        {"action": "press", "target_type": "semantic", "element_id": "close"}
    )
    assert (
        tools["computer_action"].run(
            {"action": "press", "target_type": "semantic", "element_id": "old"}
        )["error"]
        == "computer_use_fresh_observation_required"
    )
    assert "error" not in tools["computer_action"].run(
        {"action": "activate_application", "application": "Allowed Fixture"}
    )
    assert len(calls) == 3


def test_negotiation_selects_explicit_schema_and_trusted_snapshot():
    calls = []

    def host(name, args):
        calls.append((name, args))
        return (
            {"state": state(3), "receipt": {"action": "key"}}
            if name == "computer_action"
            else state(len(calls))
        )

    tools = build_computer_use_tools(host, observation_available=True, interaction_v2=True)
    schema = tools["computer_action"].as_openai_tool()["function"]["parameters"]
    assert set(schema["properties"]["target_type"]["enum"]) == {
        "semantic",
        "visual_point",
        "focused_input",
    }
    assert "target" not in schema["properties"]
    assert "surface_ref" in tools["computer_state"].parameters["properties"]
    tools["computer_state"].run({})
    tools["computer_state"].run({"surface_ref": "surface-1", "snapshot_id": "forged"})
    assert calls[1] == (
        "computer_state",
        {"surface_ref": "surface-1", "interaction_version": 2, "snapshot_id": "snapshot-1"},
    )
    tools["computer_action"].run(
        {
            "target_type": "focused_input",
            "action": "key",
            "input_mode": "background",
            "recipient_ref": "recipient-2",
            "keys": ["cmd", "n"],
            "snapshot_id": "forged",
        }
    )
    assert calls[-1][1]["snapshot_id"] == "snapshot-2"
    assert calls[-1][1]["interaction_version"] == 2


def test_v2_fields_are_refused_by_legacy_without_host_call():
    calls = []
    tools = build_computer_use_tools(lambda *args: calls.append(args))
    assert (
        tools["computer_state"].run({"surface_ref": "surface"})["error"]
        == "interaction_version_unsupported"
    )
    assert not calls


def test_v2_refuses_a_legacy_post_observation():
    tools = build_computer_use_tools(
        lambda *args: _lab_state("snapshot-old", "READY"), interaction_v2=True
    )
    assert tools["computer_state"].run({})["error"] == "interaction_version_unsupported"


@pytest.mark.parametrize(
    "authority,dispatch,recover",
    [
        ("valid_for_reobserve", "not_dispatched", True),
        ("invalid", "not_dispatched", False),
        ("takeover", "not_dispatched", False),
        ("protected", "not_dispatched", False),
        ("valid_for_reobserve", "uncertain", False),
        ("invalid", "dispatched", False),
    ],
)
def test_real_turn_uses_independent_feedback_without_replaying(
    tmp_path, authority, dispatch, recover
):
    calls, actions = [], []

    def host(name, args):
        calls.append(name)
        if name != "computer_action":
            return state(len(calls))
        actions.append(args)
        if len(actions) == 1:
            return {
                "error": "stale_element",
                "interaction_feedback": {
                    "dispatch": dispatch,
                    "authority": authority,
                    "effect": "unknown",
                    "cause": "stale_element",
                    "phase": "native_validation",
                },
            }
        return {
            "state": state(len(calls)),
            "receipt": {"action": "press"},
            "interaction_feedback": {
                "dispatch": "dispatched",
                "authority": "valid_for_reobserve",
                "effect": "observed_change",
                "cause": None,
                "phase": "post_observation",
            },
        }

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=7,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host,
        computer_interaction_v2=True,
    )
    client = _ScriptedComputerUseClient(
        [
            call("computer_state", {}, 1),
            call(
                "computer_action",
                {"action": "press", "target_type": "semantic", "element_id": "old"},
                2,
            ),
            call("computer_state", {}, 3),
            call(
                "computer_action",
                {"action": "press", "target_type": "semantic", "element_id": "new"},
                4,
            ),
            done(),
        ]
    )
    session.client = client
    try:
        assert session.run_turn("Change the selected control and inspect the result.") == (
            1 if authority == "takeover" else 0
        )
    finally:
        session.close()
    assert [a["element_id"] for a in actions] == (["old", "new"] if recover else ["old"])
    assert calls == (
        ["computer_state", "computer_action", "computer_state", "computer_action"]
        if recover
        else ["computer_state", "computer_action"]
    )


def image_state():
    output = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(output, "PNG")
    data = output.getvalue()
    result = state()
    bounds = {"x": -800, "y": 100, "width": 800, "height": 400}
    result["target"]["bounds"] = bounds
    result["coordinate_transform"] = {
        "coordinateSpace": "global_display_points",
        "logicalBounds": bounds,
    }
    result["image"] = {
        "mime_type": "image/png",
        "data": base64.b64encode(data).decode(),
        "width": 80,
        "height": 40,
    }
    result["image_frame"] = {
        "frame_id": "frame_test",
        "snapshot_id": result["snapshot_id"],
        "surface_ref": result["surface_ref"],
        "width": 80,
        "height": 40,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return result


def test_native_detailed_image_and_cursor_metadata_survive_real_tool_and_responses_projection(
    tmp_path,
):
    raw = image_state()
    output = io.BytesIO()
    Image.frombytes("RGB", (320, 160), random.Random(42).randbytes(320 * 160 * 3)).save(
        output, "PNG"
    )
    pixels = output.getvalue()
    assert 40 * 1024 < len(pixels) <= MAX_COMPUTER_USE_IMAGE_BYTES
    raw["image"] = {
        "mime_type": "image/png",
        "data": base64.b64encode(pixels).decode(),
        "width": 320,
        "height": 160,
    }
    raw["image_frame"].update(width=320, height=160, sha256=hashlib.sha256(pixels).hexdigest())
    raw["pointer"] = {
        "available": True,
        "position": {"x": -400, "y": 300},
        "coordinate_space": "global_display_points",
        "inside_target": True,
    }
    raw["focused_target"].update(
        element_id="element_editor",
        bounds={"x": -750, "y": 150, "width": 300, "height": 200},
        text_selection={
            "available": True,
            "range": {"location": 12, "length": 0},
            "caret_bounds": {"x": -700, "y": 200, "width": 0, "height": 18},
        },
    )
    # Native observations use computer.respond, not the embedded-browser host-action envelope.
    wire = json.dumps(
        {
            "protocol_version": "1",
            "id": "image",
            "method": "computer.respond",
            "params": {"result": raw},
        }
    )
    assert 64 * 1024 < len(wire.encode()) < MAX_REQUEST_BYTES
    assert parse_request_line(wire).params["result"] == raw
    with pytest.raises(ValueError, match="byte limit"):
        EmbeddedBrowserViewer(root=tmp_path, cfg=visual_cfg()).capture(raw)
    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg())
    tools = build_computer_use_tools(
        lambda *args: deepcopy(raw), interaction_v2=True, observation_available=True, viewer=viewer
    )
    result = tools["computer_state"].run({"include_image": True})
    assert "error" not in result
    assert result["pointer"] == raw["pointer"]
    assert result["focused_target"] == raw["focused_target"]
    assert raw["image"]["data"] not in json.dumps(result)
    message = viewer.take_visual_message(result["visual_delivery_id"])
    metadata = json.loads(message["content"][1]["text"].split("\n", 1)[1])
    assert metadata["pointer"] == raw["pointer"]
    assert metadata["focused_target"] == raw["focused_target"]
    parts = [
        part
        for item in _responses_input_from_messages([message])
        for part in item.get("content", [])
    ]
    assert next(p for p in parts if p["type"] == "input_image")["image_url"].endswith(
        raw["image"]["data"]
    )
    assert viewer.take_visual_message(result["visual_delivery_id"]) is None


def test_native_image_budget_still_refuses_oversized_payload_before_decoding(tmp_path):
    raw = image_state()
    raw["image"]["data"] = base64.b64encode(b"x" * (MAX_COMPUTER_USE_IMAGE_BYTES + 1)).decode()
    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg())
    with pytest.raises(ValueError, match="byte limit"):
        viewer.capture(raw)
    assert not viewer._pending


def test_production_tools_deliver_exact_frame_to_responses_request(tmp_path):
    raw = image_state()
    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg())
    tools = build_computer_use_tools(
        lambda *args: deepcopy(raw), interaction_v2=True, observation_available=True, viewer=viewer
    )
    result = tools["computer_state"].run({"include_image": True})
    assert "error" not in result
    message = viewer.take_visual_message(result["visual_delivery_id"])
    request_input = _responses_input_from_messages([message])
    parts = [part for item in request_input for part in item.get("content", [])]
    image = next(p for p in parts if p["type"] == "input_image")
    assert image["image_url"].endswith(raw["image"]["data"])
    metadata = json.loads(
        next(p["text"] for p in parts if p["type"] == "input_text").split("\n", 1)[1]
    )
    assert metadata["image_frame"] == raw["image_frame"]
    assert metadata["rendered_sha256"] == raw["image_frame"]["sha256"]
    assert metadata["rendered_dimensions"] == [80, 40]
    assert raw["image"]["data"] not in json.dumps(result)
    assert viewer.take_visual_message(result["visual_delivery_id"]) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_id", "bad"),
        ("snapshot_id", "stale"),
        ("surface_ref", "other"),
        ("width", 81),
        ("height", 41),
        ("sha256", "0" * 64),
    ],
)
def test_frame_delivery_rejects_mismatched_identity_dimensions_and_pixels(tmp_path, field, value):
    raw = image_state()
    raw["image_frame"][field] = value
    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg())
    tools = build_computer_use_tools(
        lambda *args: raw, interaction_v2=True, observation_available=True, viewer=viewer
    )
    assert tools["computer_state"].run({"include_image": True})["error"] == "invalid_host_image"
    assert not viewer._pending


def action_result(args, post, *, effect="unknown", terminated=False):
    target = post["target"]
    receipt = {
        "action": args["action"],
        "previous_snapshot_id": args["snapshot_id"],
        "target_bundle_id": target["bundle_id"],
    }
    if terminated:
        receipt.update(target_pid=target["pid"], lifecycle_status="application_terminated")
    return {
        "state": post,
        "receipt": receipt,
        "interaction_feedback": {
            "dispatch": "dispatched",
            "authority": "invalid" if terminated else "valid_for_reobserve",
            "effect": "observed_change" if terminated else effect,
            "cause": "target_application_terminated" if terminated else None,
            "phase": "post_observation",
        },
    }


def refusal(cause="element_not_found"):
    return {
        "error": cause,
        "interaction_feedback": {
            "dispatch": "not_dispatched",
            "authority": "valid_for_reobserve",
            "effect": "unknown",
            "cause": cause,
            "phase": "validation",
        },
    }


def run_v2_turn(
    tmp_path, host, responses, *, response_mode="status_only", ephemeral_system_messages=None
):
    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=30,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_response_mode=response_mode,
        computer_use_host_request=host,
        computer_interaction_v2=True,
        computer_application_lifecycle_v1=True,
        computer_observation_available=True,
    )
    client = _ScriptedComputerUseClient(responses)
    session.client = client
    try:
        code = session.run_turn(
            "Perform the requested action and report only established outcomes.",
            ephemeral_system_messages=ephemeral_system_messages,
        )
    finally:
        session.close()
    return code, surface, client


@pytest.mark.parametrize(
    "host_code,feedback_cause,expected",
    [
        (
            "computer_use_action_budget_exhausted",
            "computer_use_action_budget_exhausted",
            "computer_use_action_budget_exhausted",
        ),
        (
            "computer_use_timeout",
            "computer_use_action_budget_exhausted",
            "computer_use_turn_failed",
        ),
        ("computer_use_timeout", "computer_use_timeout", None),
        ("PRIVATE", "PRIVATE", "computer_use_turn_failed"),
    ],
)
def test_partial_work_then_budget_refusal_preserves_current_host_cause_without_replay(
    tmp_path, host_code, feedback_cause, expected
):
    class Surface(_RecordingSurface):
        computer_use_evidence_version = 1

        def __init__(self):
            super().__init__()
            self.evidence = []
            self.calls = []

        def bind_computer_use_evidence_emitter(self):
            return self.evidence.append

        def emit_computer_use_call_diagnostic(self, value):
            self.calls.append(value)

    surface, requests, delivered = Surface(), [], []

    def host(name, args):
        requests.append(name)
        if name == "computer_state":
            return state(1)
        if not delivered:
            delivered.append("first mutation remains")
            return action_result(args, state(2), effect="observed_change")
        return {
            "error": host_code,
            "message": "PRIVATE /Users/private/recipient",
            "interaction_feedback": {
                "dispatch": "not_dispatched",
                "authority": "invalid",
                "effect": "unknown",
                "cause": feedback_cause,
                "phase": "authority_validation",
            },
        }

    action = {"action": "press", "target_type": "semantic", "element_id": "element-button"}
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host,
        computer_interaction_v2=True,
        computer_observation_available=True,
    )
    session.client = _ScriptedComputerUseClient(
        [
            call("computer_state", {}, 1),
            call("computer_action", action, 2),
            call("computer_action", action, 3),
            call("computer_action", action, 4),
            done(),
        ]
    )
    try:
        assert session.run_turn(
            "Continue only while authorized; retain earlier delivered work."
        ) == (0 if expected is None else 1)
        assert session.last_turn_outcome["outcome"] == (
            "blocked" if expected is None else "incomplete"
        )
    finally:
        session.close()
    assert requests == ["computer_state", "computer_action", "computer_action"]
    assert delivered == ["first mutation remains"]
    assert [error[0] for error in surface.closed_errors] == ([] if expected is None else [expected])
    terminal = [item for item in surface.evidence if item.get("kind") == "terminal"]
    assert len(terminal) == 1 and terminal[0]["reportedOutcome"] == (
        "blocked" if expected is None else "failed"
    )
    assert terminal[0]["hostFailureCode"] == ("other" if host_code == "PRIVATE" else host_code)
    assert terminal[0]["terminalOrigin"] == (
        "host_error" if host_code == feedback_cause and host_code != "PRIVATE" else "validation"
    )
    if expected == "computer_use_action_budget_exhausted":
        assert terminal[0]["terminalOrigin"] == "host_error"
        assert any(item.get("resultCode") == expected for item in surface.calls)
    assert "PRIVATE" not in json.dumps(surface.evidence + surface.calls + surface.closed_errors)


POST_EXIT_CALLS = [
    ("computer_state", {}),
    ("computer_wait", {"duration_ms": 50}),
    ("computer_action", {"action": "quit_application"}),
    ("computer_action", {"action": "press", "target_type": "semantic", "element_id": "never-send"}),
    ("computer_state", {"include_image": True}),
    (
        "computer_state",
        {
            "assertions": [
                {"role": "AXButton", "name": "Close", "property": "enabled", "expected": False}
            ]
        },
    ),
]


@pytest.mark.parametrize("next_name,next_args", POST_EXIT_CALLS)
@pytest.mark.parametrize("response_mode", ["status_only", "host_sanitized_text"])
def test_real_turn_verified_exit_then_state_keeps_model_answer(
    tmp_path, next_name, next_args, response_mode
):
    calls, initial = [], state()

    def host(name, args):
        calls.append((name, args))
        assert len(calls) <= 2, "Verified terminal history must not reach the host again"
        return (
            initial
            if name == "computer_state"
            else action_result(args, lifecycle_state(2, initial), terminated=True)
        )

    answer = "The exact selected application exited; earlier work remains established."
    code, surface, _ = run_v2_turn(
        tmp_path,
        host,
        [
            call("computer_state", {}, 1),
            call("computer_action", {"action": "quit_application"}, 2),
            call(next_name, next_args, 3),
            LLMResponse(
                content=json.dumps({"outcome": "completed_unverified", "summary": answer}),
                tool_calls=[],
                raw={},
            ),
        ],
        response_mode=response_mode,
    )
    assert code == 0 and len(calls) == 2
    assert any(answer in message for message in surface.final_messages) is (
        response_mode == "host_sanitized_text"
    )
    if response_mode == "status_only":
        assert surface.final_messages == [
            "Computer Use run ended. The reported result has not been independently verified."
        ]
    assert not surface.closed_errors


@pytest.mark.parametrize(
    "tamper",
    [
        "dispatch",
        "authority",
        "effect",
        "cause",
        "phase",
        "performed",
        "status",
        "target_pid_bool",
        "window",
        "bounds",
        "time",
        "snapshot",
        "top_image",
        "error",
    ],
)
@pytest.mark.parametrize("through_turn", [False, True])
def test_terminal_cache_rejects_tampered_evidence(tmp_path, tamper, through_turn):
    calls, initial = [], state()

    def host(name, args):
        calls.append(name)
        if name == "computer_state":
            return initial
        result = action_result(args, lifecycle_state(2, initial), terminated=True)
        if tamper in {"dispatch", "authority", "effect", "cause", "phase"}:
            result["interaction_feedback"][tamper] = "wrong"
        elif tamper == "performed":
            result["receipt"]["performed"] = False
        elif tamper == "status":
            result["receipt"]["status"] = "not_dispatched"
        elif tamper == "target_pid_bool":
            result["receipt"]["target_pid"] = True
        elif tamper == "window":
            result["state"]["target"]["window_id"] = "other"
        elif tamper == "bounds":
            result["state"]["target"]["bounds"] = {}
        elif tamper == "time":
            result["state"]["expires_at"] = "invalid"
        elif tamper == "snapshot":
            result["state"]["snapshot_id"] = args["snapshot_id"]
        elif tamper == "top_image":
            result["image"] = {"data": "must-not-be-cached"}
        else:
            result["error"] = "computer_use_cancelled"
        return result

    if through_turn:
        code, surface, client = run_v2_turn(
            tmp_path,
            host,
            [
                call("computer_state", {}, 1),
                call("computer_action", {"action": "quit_application"}, 2),
                call("computer_state", {}, 3),
                done(),
            ],
        )
        assert code == 1 and len(calls) == client.calls == 2
        assert any("unverified" in text or "cancelled" in text for text in surface.final_messages)
        return
    tools = build_computer_use_tools(host, interaction_v2=True, application_lifecycle_v1=True)
    tools["computer_state"].run({})
    assert "error" in tools["computer_action"].run({"action": "quit_application"})
    assert "error" in tools["computer_action"].run({"action": "quit_application"})
    assert len(calls) == 2


def test_terminal_history_is_copied_and_isolated_from_new_operations():
    initial, calls = state(), []

    def host(name, args):
        calls.append(name)
        return (
            initial
            if name == "computer_state"
            else action_result(args, lifecycle_state(2, initial), terminated=True)
        )

    def new_tools():
        return build_computer_use_tools(
            host, interaction_v2=True, application_lifecycle_v1=True, observation_available=True
        )

    tools = new_tools()
    tools["computer_state"].run({})
    terminal = tools["computer_action"].run({"action": "quit_application"})
    original = deepcopy(terminal)
    terminal["state"]["target"]["pid"] = 999
    binding = tools["computer_state"].metadata["_computer_use_snapshot_binding"]
    binding.pending_read_code = "earlier_local_refusal"
    for name, args in POST_EXIT_CALLS:
        result = tools[name].run(args)
        assert result == original
        assert "goal_evidence" not in result and "visual_delivery_id" not in result
    assert len(calls) == 2
    assert new_tools()["computer_state"].run({}) == initial
    assert len(calls) == 3


def test_real_turn_stale_element_reobserves_then_succeeds(tmp_path):
    calls, dispatched = [], []

    def host(name, args):
        calls.append(name)
        if name != "computer_action":
            return state(len(calls))
        if args["element_id"] == "old":
            return refusal()
        dispatched.append(args["element_id"])
        return action_result(args, state(len(calls)), effect="observed_change")

    code, surface, client = run_v2_turn(
        tmp_path,
        host,
        [
            call("computer_state", {}, 1),
            call(
                "computer_action",
                {"action": "press", "target_type": "semantic", "element_id": "old"},
                2,
            ),
            call("computer_state", {}, 3),
            call(
                "computer_action",
                {"action": "press", "target_type": "semantic", "element_id": "current"},
                4,
            ),
            done(),
        ],
        ephemeral_system_messages=["Keep the unrelated user task constraint intact."],
    )
    assert code == 0 and dispatched == ["current"]
    assert calls == ["computer_state", "computer_action", "computer_state", "computer_action"]
    assert "not dispatched" in json.dumps(client.call_records[2]["messages"])
    recovery = "The requested operation was not dispatched. The host permits a fresh observation and a new plan."
    assert recovery in json.dumps(client.call_records[2]["messages"])
    assert recovery not in json.dumps(client.call_records[3]["messages"])
    assert client.call_records[2]["tool_choice"] == {
        "type": "function",
        "function": {"name": "computer_state"},
    }
    assert client.call_records[3]["tool_choice"] is None
    assert "Keep the unrelated user task constraint intact." in json.dumps(
        client.call_records[3]["messages"]
    )
    assert not surface.closed_errors


@pytest.mark.parametrize("unavailable", [True, False])
def test_v2_recovery_directive_is_consumed_only_by_a_validated_available_read(
    tmp_path, monkeypatch, unavailable
):
    from alysis_code.agent.turn import core

    removed, calls = [], []
    drop = core._drop_selection_directive

    def track_drop(prompts, directive):
        if directive is not None and directive.startswith(
            "The requested operation was not dispatched."
        ):
            removed.append(directive)
        drop(prompts, directive)

    monkeypatch.setattr(core, "_drop_selection_directive", track_drop)

    def host(name, args):
        calls.append(name)
        if name == "computer_action":
            return refusal()
        if len(calls) > 2 and unavailable:
            return {"status": "tool_unavailable", "reason": "computer_observation_unavailable"}
        return state(len(calls))

    responses = [
        call("computer_state", {}, 1),
        call(
            "computer_action",
            {"action": "press", "target_type": "semantic", "element_id": "old"},
            2,
        ),
        call("computer_state", {}, 3),
        LLMResponse(
            content=json.dumps({"outcome": "blocked", "summary": "The goal is unresolved."}),
            tool_calls=[],
            raw={},
        ),
    ]
    code, surface, client = run_v2_turn(tmp_path, host, responses)
    assert calls == ["computer_state", "computer_action", "computer_state"]
    assert len(removed) == (0 if unavailable else 1)
    if not unavailable:
        assert len(client.call_records) == 4, (
            "A validated read must release the stale-read latch for a truthful unresolved outcome."
        )
        assert not surface.closed_errors
    else:
        assert code == 1 and len(client.call_records) == 3


@pytest.mark.parametrize("cycles", [1, 6])
def test_six_validated_unknown_effect_cycles_keep_reads_admitted(cycles):
    calls = []

    def host(name, args):
        calls.append((name, args))
        if name != "computer_action":
            return state(len(calls))
        if args.get("element_id") == "old":
            return refusal()
        return action_result(args, state(len(calls)))

    tools = build_computer_use_tools(host, interaction_v2=True)
    tools["computer_state"].run({})
    tools["computer_action"].run(
        {"action": "press", "target_type": "semantic", "element_id": "old"}
    )
    for _ in range(cycles):
        assert "error" not in tools["computer_state"].run({})
        assert "error" not in tools["computer_action"].run(
            {"action": "press", "target_type": "semantic", "element_id": "current"}
        )
    for _ in range(4):
        assert "error" not in tools["computer_state"].run({})
    count = len(calls)
    assert tools["computer_wait"].run({})["error"] == "computer_use_refusal_reads_exhausted"
    assert len(calls) == count == 6 + 2 * cycles


@pytest.mark.parametrize(
    "invalid",
    [
        "receipt",
        "process",
        "previous_snapshot",
        "action",
        "phase",
        "authority",
        "no_change",
        "focus",
        "activation",
        "error",
        "uncertain",
        "performed",
        "missing_time",
        "reversed_time",
    ],
)
def test_unvalidated_actions_cannot_refill_four_read_allowance(invalid):
    calls = []

    def host(name, args):
        calls.append((name, args))
        if name != "computer_action":
            return state(len(calls))
        if args.get("element_id") == "old":
            return refusal()
        result = action_result(args, state(len(calls)))
        if invalid == "receipt":
            del result["receipt"]["target_bundle_id"]
        elif invalid == "process":
            result["state"]["target"]["pid"] += 1
        elif invalid == "previous_snapshot":
            result["receipt"]["previous_snapshot_id"] = "wrong"
        elif invalid == "action":
            result["receipt"]["action"] = "other"
        elif invalid in {"phase", "authority"}:
            result["interaction_feedback"][invalid] = "wrong"
        elif invalid == "no_change":
            result["interaction_feedback"]["effect"] = "observed_no_change"
        elif invalid == "error":
            result["error"] = "refused"
        elif invalid == "uncertain":
            result["interaction_feedback"]["dispatch"] = "uncertain"
        elif invalid == "performed":
            result["receipt"]["performed"] = False
        elif invalid == "missing_time":
            del result["state"]["observed_at"]
        elif invalid == "reversed_time":
            result["state"]["expires_at"] = "2026-10-14T12:00:00Z"
        return result

    tools = build_computer_use_tools(host, interaction_v2=True)
    tools["computer_state"].run({})
    tools["computer_action"].run(
        {"action": "press", "target_type": "semantic", "element_id": "old"}
    )
    for _ in range(4):
        assert "error" not in tools["computer_state"].run({})
    action = {"action": "press", "target_type": "semantic", "element_id": "current"}
    if invalid == "focus":
        action["action"] = "focus"
    if invalid == "activation":
        action = {"action": "activate_application", "application": "Allowed Fixture"}
    tools["computer_action"].run(action)
    count = len(calls)
    assert tools["computer_state"].run({})["error"] == "computer_use_refusal_reads_exhausted"
    assert len(calls) == count


@pytest.mark.parametrize("read_args", [{}, {"include_image": True}])
def test_real_turn_stop_during_read_reports_cancelled_and_preserves_prior_outcome(
    tmp_path, read_args
):
    calls = []

    def host(name, args):
        calls.append((name, args))
        if len(calls) == 1:
            return state()
        if name == "computer_action":
            return action_result(args, state(2), effect="observed_change")
        # The controller discards a late native result/pixels before this boundary.
        return {
            "error": "computer_use_cancelled",
            "interaction_feedback": {
                "dispatch": "not_dispatched",
                "authority": "invalid",
                "effect": "unknown",
                "cause": "computer_use_cancelled",
                "phase": "post_observation",
            },
        }

    code, surface, _ = run_v2_turn(
        tmp_path,
        host,
        [
            call("computer_state", {}, 1),
            call(
                "computer_action",
                {"action": "press", "target_type": "semantic", "element_id": "current"},
                2,
            ),
            call("computer_state", read_args, 3),
            done(),
        ],
    )
    text = " ".join(surface.final_messages)
    assert code == 1 and len(calls) == 3
    assert "cancelled" in text and "Earlier established outcomes remain valid" in text
    assert "route" not in text and "no longer authorized" not in text


@pytest.mark.parametrize(
    "required,next_application,valid",
    [
        (False, None, True),
        (True, None, False),
        (False, "Other App", False),
    ],
)
def test_optional_app_visit_hints_do_not_reopen_completed_work(required, next_application, valid):
    result = state()
    result["alysis_workflow"] = {
        "version": 1,
        "required": required,
        "complete": True,
        "next_application": next_application,
        "remaining_applications": ["Other App"],
    }
    tools = build_computer_use_tools(lambda name, args: result, interaction_v2=True)
    assert ("error" not in tools["computer_state"].run({})) is valid


@pytest.mark.parametrize("failure", ["unsent", "uncertain", "later_read"])
def test_real_turn_quit_failure_text_distinguishes_dispatch_and_unverified_exit(tmp_path, failure):
    calls = []

    def host(name, args):
        calls.append(name)
        if len(calls) == 1:
            return state()
        if failure == "later_read" and len(calls) == 2:
            result = action_result(args, state(2))
            result["receipt"].update(
                target_pid=result["state"]["target"]["pid"], lifecycle_status="request_accepted"
            )
            return result
        return {
            "error": "target_changed",
            "interaction_feedback": {
                "dispatch": "uncertain" if failure == "uncertain" else "not_dispatched",
                "authority": "invalid",
                "effect": "unknown",
                "cause": "target_changed",
                "phase": "post_observation",
            },
        }

    responses = [
        call("computer_state", {}, 1),
        call("computer_action", {"action": "quit_application"}, 2),
    ]
    if failure == "later_read":
        responses.append(call("computer_state", {}, 3))
    responses.append(done())
    code, surface, _ = run_v2_turn(tmp_path, host, responses)
    text = " ".join(surface.final_messages)
    assert code == 0 and len(calls) == (3 if failure == "later_read" else 2)
    assert not surface.closed_errors
    assert ("refused before it was sent" in text) is (failure == "unsent")
    assert ("process exit is unverified" in text) is (failure != "unsent")


def test_focused_unicode_custom_recipient_preserves_literal_content_and_unknown_selection():
    calls = []

    def custom_state(n):
        value = state(n)
        value["elements"] = []
        value["element_count"] = 0
        value["truncated"] = True
        value["coverage"] = {"scope": "window", "complete": False}
        value["focused_target"].update(
            role="AXGroup",
            type_semantics="unicode_events",
            text_selection={"available": False, "reason": "not_text"},
        )
        return value

    def host(name, arguments):
        calls.append((name, deepcopy(arguments)))
        if name == "computer_action":
            return {"state": custom_state(2), "receipt": {"action": "type"}}
        return custom_state(1)

    tools = build_computer_use_tools(host, observation_available=True, interaction_v2=True)
    observed = tools["computer_state"].run({})
    focus = observed["focused_target"]
    assert focus["type_semantics"] == "unicode_events"
    assert focus["text_selection"] == {"available": False, "reason": "not_text"}
    assert "value_settable" not in focus and "caret_bounds" not in focus
    literal = "  Αθήνα α\u0301 日本語 👩🏽‍💻\n"
    result = tools["computer_action"].run(
        {
            "action": "type",
            "target_type": "focused_input",
            "input_mode": "background",
            "recipient_ref": focus["recipient_ref"],
            "text": literal,
            "snapshot_id": "provider-forged",
        }
    )
    assert "error" not in result
    assert calls[-1][1]["text"] == literal
    assert calls[-1][1]["snapshot_id"] == "snapshot-1"
    assert result["state"]["focused_target"]["text_selection"]["available"] is False
    assert "does not establish editability" in tools["computer_action"].description


@pytest.mark.parametrize("action", ["key", "type", "click", "scroll", "drag"])
@pytest.mark.parametrize("mode", ["background", "foreground"])
def test_v2_schema_offers_background_and_rejects_unowned_foreground_without_changing_legacy(
    action, mode
):
    tools = build_computer_use_tools(lambda *_: state(), interaction_v2=True)
    schema = tools["computer_action"].parameters
    assert (
        "no host-owned foreground handoff is available"
        in schema["properties"]["input_mode"]["description"]
    )
    assert "foreground mode takes" not in schema["properties"]["input_mode"]["description"].lower()
    args = {"action": action, "input_mode": mode}
    if action in {"key", "type"}:
        args.update(target_type="focused_input", recipient_ref="recipient-1")
        args.update({"text": "Exact text"} if action == "type" else {"keys": ["left"]})
    else:
        args.update(
            target_type="visual_point", frame_id="frame-1", surface_ref="surface-1", x=20, y=30
        )
        if action == "scroll":
            args["delta_y"] = 60
        if action == "drag":
            args.update(to_x=40, to_y=50)
    if mode == "background":
        jsonschema.validate(args, schema)
    else:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(args, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**args, "input_mode": "automatic"}, schema)
    legacy = build_computer_use_tools(lambda *_: state())["computer_action"].parameters
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args, legacy)


@pytest.mark.parametrize("literal", ["English background input", "  Αθήνα α\u0301 日本語 👩🏽‍💻\n"])
def test_background_focused_unicode_preserves_route_literal_and_trusted_binding(literal):
    calls = []

    def observed(n):
        value = state(n)
        value["focused_target"].update(
            role="AXGroup",
            type_semantics="unicode_events",
            text_selection={"available": False, "reason": "not_text"},
        )
        value["input_capabilities"] = {
            "background_events_version": 1,
            "background_actions": ["key", "type"],
        }
        return value

    def host(name, args):
        calls.append((name, deepcopy(args)))
        return (
            action_result(args, observed(2), effect="unknown")
            if name == "computer_action"
            else observed(1)
        )

    tools = build_computer_use_tools(host, interaction_v2=True)
    initial = tools["computer_state"].run({})
    result = tools["computer_action"].run(
        {
            "action": "type",
            "target_type": "focused_input",
            "input_mode": "background",
            "recipient_ref": initial["focused_target"]["recipient_ref"],
            "text": literal,
            "snapshot_id": "provider-forged",
        }
    )
    assert "error" not in result
    assert calls[-1] == (
        "computer_action",
        {
            "action": "type",
            "target_type": "focused_input",
            "input_mode": "background",
            "recipient_ref": "recipient-1",
            "text": literal,
            "snapshot_id": "snapshot-1",
            "interaction_version": 2,
        },
    )
    assert result["interaction_feedback"]["effect"] == "unknown"
    assert result["state"]["focused_target"]["text_selection"]["available"] is False


def test_background_click_only_observation_can_progress_to_new_focused_typing_binding(tmp_path):
    literal = "  English · Αθήνα α\u0301 日本語 👩🏽‍💻\n"
    captured = image_state()
    captured["snapshot_id"] = "snapshot-2"
    captured["image_frame"]["snapshot_id"] = "snapshot-2"
    captured["focused_target"] = {"available": False, "reason": "not_focused"}
    captured["input_capabilities"] = {
        "background_events_version": 1,
        "background_actions": ["click", "drag"],
    }
    initial = deepcopy(captured)
    initial["snapshot_id"] = "snapshot-1"
    initial.pop("image")
    initial.pop("image_frame")
    focused = state(3)
    focused["target"] = deepcopy(captured["target"])
    focused["focused_target"].update(
        role="AXGroup",
        type_semantics="unicode_events",
        text_selection={"available": False, "reason": "not_text"},
    )
    focused["input_capabilities"] = {
        "background_events_version": 1,
        "background_actions": ["key", "type", "click", "drag"],
    }
    typed = deepcopy(focused)
    typed["snapshot_id"] = "snapshot-4"
    typed["focused_target"]["recipient_ref"] = "recipient-4"
    calls = []

    def host(name, args):
        calls.append((name, deepcopy(args)))
        if name == "computer_state":
            return deepcopy(captured if args.get("include_image") else initial)
        assert args["input_mode"] == "background", "no foreground route may be substituted"
        if args["action"] == "click":
            assert args["snapshot_id"] == "snapshot-2"
            assert args["frame_id"] == captured["image_frame"]["frame_id"]
            return action_result(args, deepcopy(focused), effect="observed_change")
        assert args["action"] == "type"
        assert args["snapshot_id"] == "snapshot-3" and args["recipient_ref"] == "recipient-3"
        assert args["text"] == literal
        return action_result(args, deepcopy(typed), effect="unknown")

    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg())
    tools = build_computer_use_tools(
        host, observation_available=True, interaction_v2=True, viewer=viewer
    )
    before = tools["computer_state"].run({})
    assert before["focused_target"]["available"] is False
    assert before["input_capabilities"]["background_actions"] == ["click", "drag"]
    observed = tools["computer_state"].run({"include_image": True})
    message = viewer.take_visual_message(observed["visual_delivery_id"])
    metadata = json.loads(message["content"][1]["text"].split("\n", 1)[1])
    assert metadata["focused_target"]["available"] is False
    provider_input = _responses_input_from_messages(
        [
            {
                "role": "tool",
                "tool_call_id": "state-image",
                "name": "computer_state",
                "content": json.dumps(observed),
            },
            message,
        ]
    )
    projected = json.loads(
        next(
            item["output"] for item in provider_input if item.get("type") == "function_call_output"
        )
    )
    assert projected["input_capabilities"]["background_actions"] == ["click", "drag"]
    assert any(
        part.get("type") == "input_image"
        for item in provider_input
        for part in item.get("content", [])
    )
    frame = observed["image_frame"]
    click = {
        "action": "click",
        "input_mode": "background",
        "target_type": "visual_point",
        "frame_id": frame["frame_id"],
        "surface_ref": frame["surface_ref"],
        "x": 20,
        "y": 10,
    }
    jsonschema.validate(click, tools["computer_action"].parameters)
    after = tools["computer_action"].run(click)
    assert "error" not in after
    current = after["state"]
    assert current["focused_target"]["recipient_ref"] == "recipient-3"
    assert "type" in current["input_capabilities"]["background_actions"]
    typing = {
        "action": "type",
        "input_mode": "background",
        "target_type": "focused_input",
        "recipient_ref": current["focused_target"]["recipient_ref"],
        "text": literal,
    }
    jsonschema.validate(typing, tools["computer_action"].parameters)
    result = tools["computer_action"].run({**typing, "snapshot_id": "provider-old-snapshot"})
    assert "error" not in result and result["interaction_feedback"]["effect"] == "unknown"
    assert result["state"]["focused_target"]["text_selection"]["available"] is False
    assert [name for name, _ in calls] == [
        "computer_state",
        "computer_state",
        "computer_action",
        "computer_action",
    ]
    assert [args["action"] for name, args in calls if name == "computer_action"] == [
        "click",
        "type",
    ]
    assert "later type need not be advertised" in tools["computer_action"].description


def test_background_host_refusal_does_not_trigger_hidden_foreground_fallback():
    calls = []

    def host(name, args):
        calls.append((name, deepcopy(args)))
        return (
            refusal("background_synthetic_input_unsupported")
            if name == "computer_action"
            else state(1)
        )

    tools = build_computer_use_tools(host, interaction_v2=True)
    tools["computer_state"].run({})
    result = tools["computer_action"].run(
        {
            "action": "key",
            "target_type": "focused_input",
            "input_mode": "background",
            "recipient_ref": "recipient-1",
            "keys": ["left"],
        }
    )
    assert result["error"] == "background_synthetic_input_unsupported"
    assert result["interaction_feedback"]["dispatch"] == "not_dispatched"
    assert len(calls) == 2
    assert calls[-1][1]["input_mode"] == "background"
    assert "handoff" in tools["computer_action"].description
