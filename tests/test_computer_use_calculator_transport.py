"""Synthetic wire/host continuity check; not a physical Calculator acceptance test."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_computer_use_serialization import _stream_response, _SyntheticSubscriptionAuth

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.base import (
    require_omitted_reasoning_display,
    require_serial_tool_calls,
    require_stateless_responses,
    require_tool_calling,
)
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


def test_native_keypad_sequence_preserves_goal_labels_values_and_fresh_ids_on_subscription_wire(
    tmp_path: Path,
) -> None:
    goal = "Open Calculator and calculate 12345 × 6789."
    labels = ["All Clear", *"0123456789", "Multiply", "Equals"]
    plan = ["All Clear", *"12345", "Multiply", *"6789", "Equals"]
    displays = [
        "0",
        "1",
        "12",
        "123",
        "1.234",
        "12.345",
        "12.345",
        "6",
        "67",
        "678",
        "6.789",
        "83.810.205",
    ]
    pressed: list[str] = []
    calls = 0
    latest: dict[str, Any] = {}

    def observation() -> dict[str, Any]:
        revision = len(pressed)
        return {
            "snapshot_id": f"synthetic-snapshot-{revision}",
            "target": {"bundle_id": "com.apple.calculator"},
            "elements": [
                {
                    "id": f"display-{revision}",
                    "role": "AXStaticText",
                    "label": "Edit field",
                    "value": displays[revision - 1] if revision else "731",
                },
                *[
                    {
                        "id": f"element_{ordinal}_0{revision}abc",
                        "role": "AXButton",
                        "label": label,
                        "value": "",
                        "actions": ["AXPress"],
                        "secure": False,
                    }
                    for ordinal, label in enumerate(labels)
                ],
            ],
        }

    def host(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal latest
        if name == "computer_state":
            latest = observation()
            return latest
        assert name == "computer_action"
        assert arguments["action"] == "press"
        assert arguments["snapshot_id"] == latest["snapshot_id"]
        target = next(node for node in latest["elements"] if node["id"] == arguments["element_id"])
        assert target["label"] == plan[len(pressed)]
        pressed.append(target["label"])
        latest = observation()
        return {
            "receipt": {
                "action": "press",
                "performed": True,
                "input_mode": "background",
                "frontmost_before": False,
                "frontmost_after": False,
            },
            "state": latest,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["parallel_tool_calls"] is False
        assert "previous_response_id" not in body
        user_content = json.dumps(
            [item for item in body["input"] if item.get("role") == "user"], ensure_ascii=False
        )
        assert goal in user_content, "Original user goal must remain intact after numeric UI taints"
        if not latest:
            return _stream_response(calls, [("computer_state", {})])
        outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
        result = json.loads(outputs[-1]["output"])
        state = result.get("state", result)
        assert state == latest, "Latest native observation must reach the provider byte-exact"
        if len(pressed) == len(plan):
            assert state["elements"][0]["value"] == "83.810.205"
            return _stream_response(calls, [])
        target = next(node for node in state["elements"] if node.get("label") == plan[len(pressed)])
        return _stream_response(
            calls, [("computer_action", {"action": "press", "element_id": target["id"]})]
        )

    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=20,
        no_log=True,
        api_key_override="synthetic-fixture",
        surface=NoopSurface(),
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host,
    )
    client = OpenAIResponsesClient(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-fixture",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_auth=_SyntheticSubscriptionAuth(),  # type: ignore[arg-type]
    )
    require_tool_calling(client, ("computer_state", "computer_action", "computer_wait"))
    require_omitted_reasoning_display(client)
    require_serial_tool_calls(client)
    require_stateless_responses(client)
    session.client = client
    try:
        assert session.run_turn(goal) == 0
        assert pressed == plan
        assert calls == len(plan) + 2
        durable = json.dumps(session.messages, ensure_ascii=False)
        assert "synthetic-snapshot-" not in durable
        assert "83.810.205" not in durable
    finally:
        session.close()
