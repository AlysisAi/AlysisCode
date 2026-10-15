"""Subscription wire regressions; fake credentials and HTTP, no live provider or apps."""

from __future__ import annotations

import copy
import json
import uuid

import httpx
import pytest
from test_computer_discovery_recovery import _inventory, _selection, _selection_session
from test_openai_codex_subscription_auth import _FakeStreamingCodexAuth

from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.llm.base import (
    require_serial_tool_calls,
    require_stateless_responses,
    require_tool_calling,
)
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.provider_auth.base import ProviderAuthError
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth


def _function(name):
    return {"type": "function", "name": name, "parameters": {"type": "object"}}


@pytest.mark.parametrize(
    "name", ["computer_start", "computer_discover", "computer_state", "read_file"]
)
def test_forced_subscription_function_has_one_callable_in_its_original_namespace(name):
    original = {
        "tools": [_function(name), _function("unrelated"), {"type": "web_search"}],
        "tool_choice": {"type": "function", "name": name},
        "parallel_tool_calls": True,
    }
    before = copy.deepcopy(original)
    adapter = OpenAICodexSubscriptionAuth()
    result = adapter.adapt_responses_payload(original)
    assert result["tool_choice"] == "required"
    assert result["parallel_tool_calls"] is False
    assert len(result["tools"]) == 1
    assert result["tools"][0]["name"] == "alysis"
    assert [t["name"] for t in result["tools"][0]["tools"]] == [name]
    assert original == before
    assert adapter.adapt_responses_payload(result) == result


def test_explicit_namespace_is_preserved_and_custom_tool_choice_is_narrowed():
    chosen = {"type": "custom", "name": "edit", "format": {"type": "text"}}
    result = OpenAICodexSubscriptionAuth().adapt_responses_payload(
        {
            "tools": [
                {
                    "type": "namespace",
                    "name": "edits",
                    "description": "Editor",
                    "tools": [chosen, _function("read")],
                },
                _function("other"),
            ],
            "tool_choice": {"type": "custom", "name": "edit", "namespace": "edits"},
        }
    )
    assert result["tools"] == [
        {"type": "namespace", "name": "edits", "description": "Editor", "tools": [chosen]}
    ]
    assert result["tool_choice"] == "required"
    assert result["parallel_tool_calls"] is False


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [_function("other")],
        [
            _function("computer_start"),
            {"type": "namespace", "name": "other", "tools": [_function("computer_start")]},
        ],
    ],
)
def test_missing_or_ambiguous_forced_tool_fails_before_sending_a_request(tools):
    with pytest.raises(ProviderAuthError, match="not uniquely available"):
        OpenAICodexSubscriptionAuth().adapt_responses_payload(
            {
                "tools": tools,
                "tool_choice": {"type": "function", "name": "computer_start"},
            }
        )


@pytest.mark.parametrize("choice", [None, "auto", "none", "required", {"type": "web_search"}])
def test_other_tool_choices_keep_the_available_tools(choice):
    result = OpenAICodexSubscriptionAuth().adapt_responses_payload(
        {
            "tools": [
                _function("computer_start"),
                _function("computer_discover"),
                {"type": "web_search"},
            ],
            "tool_choice": choice,
        }
    )
    assert result["tool_choice"] == choice
    assert len(result["tools"]) == 2
    assert len(result["tools"][1]["tools"]) == 2


@pytest.mark.parametrize("empty_count", [0, 1, 2])
def test_invalid_selection_recovers_through_real_subscription_request_adapter(
    tmp_path, empty_count
):
    inventory = _inventory(128)
    good = _selection(inventory, 127, ["activate_application", "press"])
    goal = "Open the selected app and read its value. Do not change anything else."
    good["goal"] = goal
    bad = {**good, "target_ids": [str(uuid.uuid4())]}
    requests = []
    host_calls = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        choice = payload.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function":
            # Reproduce the actual provider rejection: bare forced names only
            # identify top-level functions, not a member of a namespace.
            if not any(
                t.get("type") == "function" and t.get("name") == choice["name"]
                for t in payload.get("tools", [])
            ):
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Tool choice 'computer_start' not found in 'tools' parameter."
                        }
                    },
                )
        number = len(requests)
        assert number <= 3 + empty_count, (
            "only bounded empty retries, discovery and corrected selection"
        )
        name, arguments = "computer_start", bad
        if 2 <= number <= 2 + empty_count:
            assert choice == "required"
            assert [tool["name"] for group in payload["tools"] for tool in group["tools"]] == [
                "computer_discover"
            ]
            name, arguments = "computer_discover", {}
        elif number == 3 + empty_count:
            # Choose from the discovery result actually delivered through the
            # agent and subscription transport, never a pre-scripted fresh ID.
            output = [
                item for item in payload["input"] if item.get("type") == "function_call_output"
            ][-1]
            current = json.loads(output["output"])
            assert current == inventory
            target = next(
                target
                for target in current["targets"]
                if target["name"] == inventory["targets"][-1]["name"]
            )
            arguments = {**bad, "target_ids": [target["target_id"]]}
        response = {
            "id": f"response-{number}",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": f"fc-{number}",
                    "call_id": f"call-{number}",
                    "namespace": "alysis",
                    "name": name,
                    "status": "completed",
                    "arguments": json.dumps(arguments),
                }
            ],
        }
        if 2 <= number < 2 + empty_count:
            response["output"] = []
        event = {"type": "response.completed", "response": response}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\n",
        )

    session, _ = _selection_session(
        tmp_path,
        inventory,
        [],
        lambda args: host_calls.append(copy.deepcopy(args)) or BrowserStartHandoff("bound"),
    )
    old_inventory = copy.deepcopy(inventory)
    old_inventory["targets"][-1]["target_id"] = bad["target_ids"][0]
    session.messages.extend(
        [
            {"role": "user", "content": "Find the app for the earlier request."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "prior-discovery",
                        "type": "function",
                        "function": {"name": "computer_discover", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "prior-discovery",
                "name": "computer_discover",
                "content": json.dumps(old_inventory),
            },
            {"role": "assistant", "content": "The earlier app list was found."},
        ]
    )
    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="",
        model="fixture-model",
        provider_auth=_FakeStreamingCodexAuth(),
        transport=httpx.MockTransport(handler),
    )
    require_tool_calling(client, ("computer_start", "computer_discover"))
    require_serial_tool_calls(client)
    require_stateless_responses(client)
    session.client = client
    try:
        assert session.run_turn(goal) == 0
    finally:
        session.close()
    assert len(requests) == 3 + empty_count
    assert host_calls == [good], "only the corrected selection reaches desktop admission"
    retry = requests[1]
    assert retry["tool_choice"] == "required"
    assert retry["parallel_tool_calls"] is False
    assert retry["store"] is False and "previous_response_id" not in retry
    assert len(retry["tools"]) == 1
    namespace = retry["tools"][0]
    assert namespace["name"] == "alysis" and len(namespace["tools"]) == 1
    tool = namespace["tools"][0]
    assert tool["name"] == "computer_discover"
    assert tool["parameters"]["properties"] == {}
    assert bad["target_ids"][0] in json.dumps(requests[0]["input"])
    assert all(goal in json.dumps(request["input"], ensure_ascii=False) for request in requests)
    assert session.tools["computer_start"].parameters["properties"]["target_ids"]["items"][
        "enum"
    ] == [t["target_id"] for t in inventory["targets"]]
    assert any(
        item.get("type") == "function_call" and item.get("namespace") == "alysis"
        for item in retry["input"]
    )
