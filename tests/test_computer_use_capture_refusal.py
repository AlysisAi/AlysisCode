"""W1b terminal capture refusals through production tools and protected turns."""

import json
import threading
from copy import deepcopy

import pytest
from test_computer_interaction_v2 import call, done
from test_computer_use_completion_display import _Surface
from test_computer_use_recovery import _ScriptedComputerUseClient
from test_computer_use_window_selection import INVENTORY, REF, VECTORS, inventory
from test_embedded_browser_visual import visual_cfg

from alysis_code.agent.computer_use_tools import (
    build_computer_use_tools,
    reset_computer_use_snapshot_binding,
)
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.config import AppConfig
from alysis_code.embedded_browser_visual import ComputerUseViewer
from alysis_code.llm.types import LLMResponse

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

LIMIT = VECTORS["capture_refusal_limit_observation"]["visual"]
QUESTION = "I cannot inspect the image. Can you describe the four boxes?"


def observation(n=3, *, terminal=True):
    value = deepcopy(VECTORS["capture_refusal_limit_observation"])
    value["snapshot_id"] = f"snapshot-{n}"
    value["surface_ref"] = value["selected_surface"] = f"surface-{n}"
    value["surfaces"][0]["surface_ref"] = f"surface-{n}"
    if not terminal:
        value.pop("visual")
    return value


def report(outcome="blocked", text=QUESTION):
    return LLMResponse(
        content=json.dumps({"outcome": outcome, "summary": text}), tool_calls=[], raw={}
    )


def tools_for(tmp_path, host, *, images=True):
    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg()) if images else None
    return build_computer_use_tools(
        host,
        interaction_v2=True,
        observation_available=True,
        window_selection_v1=True,
        viewer=viewer,
    )


@pytest.mark.parametrize("images", [False, True])
def test_terminal_visual_projection_is_exact_even_without_model_images(tmp_path, images):
    calls = []
    value = deepcopy(VECTORS["capture_refusal_limit_observation"])
    tools = tools_for(tmp_path, lambda *a: calls.append(a) or deepcopy(value), images=images)
    result = tools["computer_state"].run({"include_image": True})
    assert result["visual"] == LIMIT
    assert not any(k in result for k in ("image", "image_frame", "visual_delivery_id"))
    assert tools["computer_state"].run({"include_image": True})["error"] == "capture_refusal_limit"
    assert len(calls) == 1


def test_guard_survives_ax_reads_inventory_surface_churn_and_stale_recovery(tmp_path):
    calls = []

    def host(name, args):
        calls.append((name, args))
        if len(calls) == 1:
            return observation()
        if args.get("list_windows"):
            return inventory()
        if args.get("surface_ref") == "stale-surface":
            return {
                "error": "stale_surface",
                "interaction_feedback": {
                    "dispatch": "not_dispatched",
                    "authority": "valid_for_reobserve",
                    "effect": "unknown",
                    "cause": "stale_surface",
                    "phase": "validation",
                },
            }
        value = observation(len(calls) + 3, terminal=False)
        value["elements"] = [{"id": "ax-label", "role": "AXStaticText", "name": "Accessible text"}]
        if "window_ref" in args:
            value["window_selection"] = {
                "window_ref": args["window_ref"],
                "inventory_id": INVENTORY,
            }
        return value

    tools = tools_for(tmp_path, host)
    tools["computer_state"].run({"include_image": True})
    for read in (
        {"max_elements": 10},
        {"surface_ref": "surface-4"},
        {"list_windows": True},
        {"window_ref": REF},
        {"surface_ref": "stale-surface"},
        {},
    ):
        result = tools["computer_state"].run(read)
        if "elements" in result:
            assert result["elements"][0]["name"] == "Accessible text"
        count = len(calls)
        for image_args in (
            {"include_image": True},
            {"include_image": True, "max_elements": 300},
            {"include_image": True, "surface_ref": "fresh-surface"},
        ):
            refused = tools["computer_state"].run(image_args)
            assert refused["error"] == "capture_refusal_limit" and refused["visual"] == LIMIT
            assert len(calls) == count


@pytest.mark.parametrize(
    "field", ["bundle_id", "pid", "window_id", "window_title_digest", "bounds"]
)
def test_authoritative_changed_lineage_allows_a_new_image_request(tmp_path, field):
    changed = observation(4, terminal=False)
    changed["target"][field] = (
        {"x": 21, "y": 30, "width": 800, "height": 600}
        if field == "bounds"
        else 4243
        if field == "pid"
        else changed["target"][field] + "-changed"
    )
    calls = []

    def host(name, args):
        calls.append((name, args))
        return observation() if len(calls) == 1 else deepcopy(changed)

    tools = tools_for(tmp_path, host)
    tools["computer_state"].run({"include_image": True})
    assert tools["computer_state"].run({"include_image": True})["error"] == "capture_refusal_limit"
    assert len(calls) == 1
    tools["computer_state"].run({})  # Only a trusted observation establishes the change.
    result = tools["computer_state"].run({"include_image": True})
    assert "error" not in result and len(calls) == 3 and calls[-1][1]["include_image"] is True


@pytest.mark.parametrize("missing", ["window_id", "window_title_digest", "bounds"])
def test_incomplete_identity_cannot_lift_capture_refusal(tmp_path, missing):
    incomplete = observation(4, terminal=False)
    incomplete["target"].pop(missing)
    calls = []

    def host(*args):
        calls.append(args)
        return observation() if len(calls) == 1 else incomplete

    tools = tools_for(tmp_path, host)
    tools["computer_state"].run({"include_image": True})
    tools["computer_state"].run({})
    assert tools["computer_state"].run({"include_image": True})["error"] == "capture_refusal_limit"
    assert len(calls) == 2


@pytest.mark.parametrize("retryable", [None, True, "false"])
def test_only_explicit_nonretryable_capture_limit_latches_the_guard(tmp_path, retryable):
    value = observation()
    if retryable is None:
        value["visual"].pop("retryable")
    else:
        value["visual"]["retryable"] = retryable
    calls = []
    tools = tools_for(tmp_path, lambda *a: calls.append(a) or value)
    tools["computer_state"].run({"include_image": True})
    result = tools["computer_state"].run({"include_image": True})
    assert "error" not in result and len(calls) == 2


def test_full_operation_reset_clears_capture_guard(tmp_path):
    calls = []
    tools = tools_for(tmp_path, lambda *a: calls.append(a) or observation())
    tools["computer_state"].run({"include_image": True})
    assert tools["computer_state"].run({"include_image": True})["error"] == "capture_refusal_limit"
    reset_computer_use_snapshot_binding(tools)
    tools["computer_state"].run({"include_image": True})
    assert len(calls) == 2


@pytest.mark.parametrize("field", ["image", "image_frame", "visual_delivery_id"])
def test_terminal_capture_refusal_cannot_carry_pixels_or_frame_authority(tmp_path, field):
    value = observation()
    value[field] = {"invalid": "payload"} if field != "visual_delivery_id" else "invented"
    tools = tools_for(tmp_path, lambda *_: value)
    result = tools["computer_state"].run({"include_image": True})
    assert result["error"] == "invalid_host_response"
    assert not any(k in result for k in ("image", "image_frame", "visual_delivery_id"))


def run_capture_turn(
    tmp_path,
    tail,
    *,
    max_steps=16,
    cancel=None,
    takeover=False,
    limit=True,
    cancel_after_report=False,
    image_requests=True,
):
    surface = _Surface()
    host_calls = []
    capture_calls = []
    responses = [
        call("computer_state", {"include_image": image_requests, "max_elements": 20 + n}, n)
        for n in range(3)
    ]
    responses += tail

    def host(name, args):
        host_calls.append((name, args))
        n = len(host_calls)
        value = observation(n, terminal=limit and n >= 3)
        if args.get("include_image") is not True:
            value["elements"] = [
                {"id": "ax-label", "role": "AXStaticText", "name": "Accessible text"}
            ]
        if n < 3 and args.get("include_image") is True:
            capture_calls.append(n)
            value["visual"] = {"status": "unavailable", "reason": "target_window_changed"}
        if n == 3 and cancel is not None and not cancel_after_report:
            cancel.set()
        if n == 3 and takeover:
            value.update(
                error="user_takeover",
                interaction_feedback={
                    "dispatch": "not_dispatched",
                    "authority": "takeover",
                    "effect": "unknown",
                    "cause": "user_takeover",
                    "phase": "validation",
                },
            )
        return value

    cfg = AppConfig(model="test-model", stream=False)
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {
            "test-model": {
                "supports_vision": True,
                "context_window_tokens": 128000,
                "max_output_tokens": 1024,
            }
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=max_steps,
        no_log=True,
        api_key_override="fixture-key",
        surface=surface,
        sensitive_media=True,
        verification_enabled=False,
        computer_use_host_request=host,
        computer_use_response_mode="host_sanitized_text",
        computer_interaction_v2=True,
        computer_observation_available=True,
        computer_window_selection_v1=True,
    )

    class RecordingClient(_ScriptedComputerUseClient):
        def chat(
            self,
            *,
            messages,
            tools=None,
            tool_choice=None,
            stream=False,
            on_text_delta=None,
            temperature=None,
        ):
            result = super().chat(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                stream=stream,
                on_text_delta=on_text_delta,
                temperature=temperature,
            )
            self.call_records[-1]["messages"] = deepcopy(messages)
            if cancel is not None and cancel_after_report and self.calls == 4:
                cancel.set()
            return result

    client = RecordingClient(responses)
    session.client = client
    try:
        code = session.run_turn(
            "Read the four boxes in the permitted image; do not edit anything."
            if image_requests
            else "Read the accessible text in the permitted window; do not edit anything.",
            cancellation_token=EventCancellationToken(cancel) if cancel is not None else None,
        )
        return (
            code,
            surface,
            dict(session.last_turn_outcome),
            client,
            host_calls,
            capture_calls,
            json.dumps(session.messages),
        )
    finally:
        session.close()


def test_real_turn_two_refusals_then_limit_gets_one_truthful_blocked_question(tmp_path):
    code, surface, final, client, host_calls, capture_calls, durable = run_capture_turn(
        tmp_path, [report()]
    )
    assert code == 0 and final["outcome"] == "blocked"
    assert client.calls == 4 and len(host_calls) == 3 and capture_calls == [1, 2]
    assert all(args["include_image"] is True for _, args in host_calls)
    assert QUESTION in surface.messages[-1]
    last_request = json.dumps(client.call_records[-1]["messages"])
    delivered = [
        json.loads(m["content"])
        for m in client.call_records[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert any(value.get("visual") == LIMIT for value in delivered)
    assert any(
        "Do not retry" in str(message) and "capture" in str(message)
        for message in client.call_records[-1]["messages"]
    )
    terminal = next(e for e in surface.evidence if e.get("kind") == "terminal")
    assert terminal["reportedOutcome"] == "blocked"
    assert "image_url" not in last_request and "data:image" not in last_request
    assert "window_00000000" not in durable


@pytest.mark.parametrize(
    "tail",
    [
        call("computer_state", {"include_image": True, "surface_ref": "new-surface"}, 4),
        call("computer_state", {"list_windows": True}, 4),
        call("computer_state", {"max_elements": 50}, 4),
        call("computer_action", {"action": "activate_application", "application": "Preview"}, 4),
        LLMResponse(
            content="",
            tool_calls=[
                *call("computer_state", {"include_image": True}, 4).tool_calls,
                *call("computer_action", {"action": "press", "element_id": "button"}, 5).tool_calls,
            ],
            raw={},
        ),
        LLMResponse(content="", tool_calls=[], raw={}),
        LLMResponse(content='{"outcome":', tool_calls=[], raw={}),
        report("completed_unverified", "I saw the image and finished the task."),
    ],
)
def test_real_turn_limit_never_grants_another_tool_or_model_retry(tmp_path, tail):
    code, surface, final, client, calls, _, _ = run_capture_turn(tmp_path, [tail, report()])
    assert code == 0 and final["outcome"] == "blocked"
    assert client.calls == 4 and len(calls) == 3
    assert "I saw the image" not in " ".join(surface.messages)
    assert "image" in surface.messages[-1].lower() and "unavailable" in surface.messages[-1].lower()


def test_real_turn_limit_at_step_boundary_finishes_blocked_without_an_extra_request(tmp_path):
    code, _, final, client, calls, _, _ = run_capture_turn(tmp_path, [], max_steps=3)
    assert code == 0 and final["outcome"] == "blocked"
    assert client.calls == len(calls) == 3


def test_real_turn_stop_and_takeover_win_over_capture_limit(tmp_path):
    with pytest.raises(CooperativeCancellationError):
        run_capture_turn(tmp_path, [report()], cancel=threading.Event())
    with pytest.raises(CooperativeCancellationError):
        run_capture_turn(tmp_path, [report()], cancel=threading.Event(), cancel_after_report=True)
    code, surface, final, client, calls, _, _ = run_capture_turn(
        tmp_path, [report()], takeover=True
    )
    assert code != 0 and final["outcome"] != "blocked"
    assert client.calls == len(calls) == 3
    assert QUESTION not in surface.messages


def test_ax_only_nonvisual_completion_without_terminal_capture_limit_is_preserved(tmp_path):
    code, _, final, client, calls, captures, _ = run_capture_turn(
        tmp_path, [done()], limit=False, image_requests=False
    )
    assert not captures and all(args["include_image"] is False for _, args in calls)
    assert code == 0 and final["outcome"] == "completed_unverified"
    assert client.calls == 4 and len(calls) == 3


def test_failed_projection_cannot_clear_the_lineage_guard(tmp_path):
    changed = observation(4, terminal=False)
    changed["target"]["window_id"] = "changed-window"
    changed["image"] = {"unrequested": "pixels"}
    calls = []
    tools = tools_for(
        tmp_path, lambda *a: calls.append(a) or (observation() if len(calls) == 1 else changed)
    )
    tools["computer_state"].run({"include_image": True})
    assert tools["computer_state"].run({})["error"] == "unexpected_host_image"
    assert tools["computer_state"].run({"include_image": True})["error"] == "capture_refusal_limit"
    assert len(calls) == 2
