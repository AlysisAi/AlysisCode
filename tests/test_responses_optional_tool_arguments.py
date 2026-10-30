from __future__ import annotations

import copy
import json

import httpx
import pytest

from alysis_code.agent.computer_use_tools import build_computer_use_tools
from alysis_code.llm.base import require_serial_tool_calls, require_stateless_responses
from alysis_code.llm.openai_responses import OpenAIResponsesClient, _responses_tool_from_chat_tool

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("strict", [None, False, True])
def test_responses_preserves_optional_arguments_and_explicit_strict_choice(wrapped, strict):
    function = {
        "name": "observe",
        "parameters": {
            "type": "object",
            "properties": {"cursor": {"type": "string"}},
            "additionalProperties": False,
        },
    }
    if strict is not None:
        function["strict"] = strict
    source = (
        {"type": "function", "function": function} if wrapped else {"type": "function", **function}
    )
    original = copy.deepcopy(source)
    mapped = _responses_tool_from_chat_tool(source)
    assert mapped["strict"] is (False if strict is None else strict)
    assert mapped["parameters"] == function["parameters"]
    assert source == original


@pytest.mark.parametrize("stream", [False, True])
def test_hosted_computer_state_does_not_require_an_unobserved_selector(stream):
    host_calls = []
    definitions = build_computer_use_tools(
        lambda name, args: host_calls.append((name, args)) or {"snapshot_id": "fresh-1"},
        observation_available=True,
    )
    tools = [tool.as_openai_tool() for tool in definitions.values()]

    def respond(request):
        body = json.loads(request.content)
        state = next(tool for tool in body["tools"] if tool["name"] == "computer_state")
        # Responses otherwise normalizes every optional selector into a required
        # field, making the initial observation impossible without inventing IDs.
        assert state["strict"] is False
        assert "required" not in state["parameters"]
        assert state["parameters"]["additionalProperties"] is False
        assert state["parameters"]["properties"]["element_id"]["minLength"] == 1
        assert body["parallel_tool_calls"] is False
        assert body["store"] is False
        assert body["tool_choice"] == {"type": "function", "name": "computer_state"}
        result = {
            "id": "resp_observe",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "observe-1",
                    "name": "computer_state",
                    "arguments": "{}",
                }
            ],
        }
        if stream:
            return httpx.Response(
                200,
                text="event: response.completed\ndata: "
                + json.dumps({"type": "response.completed", "response": result})
                + "\n\n",
            )
        return httpx.Response(200, json=result)

    client = OpenAIResponsesClient(
        base_url="https://hosted.test/v1",
        api_key="test-only",
        model="gpt-6-luna",
        provider_key="alysis",
        transport=httpx.MockTransport(respond),
    )
    require_serial_tool_calls(client)
    require_stateless_responses(client)
    response = client.chat(
        messages=[{"role": "user", "content": "Read the connected webpage"}],
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "computer_state"}},
        stream=stream,
    )
    call = response.tool_calls[0]
    assert definitions[call.name].run(call.arguments)["snapshot_id"] == "fresh-1"
    assert host_calls == [("computer_state", {})]
    # Fixing wire optionality must not permit an action without a fresh read.
    untouched = build_computer_use_tools(lambda *_: pytest.fail("must not dispatch"))
    assert (
        untouched["computer_action"].run({"action": "press", "element_id": "invented"})["error"]
        == "computer_use_fresh_observation_required"
    )
