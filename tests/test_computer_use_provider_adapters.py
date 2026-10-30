from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_SYSTEM_PROMPT,
    COMPUTER_USE_TOOL_NAMES,
    build_computer_use_tools,
)
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.base import (
    RequiredToolCallingUnsupportedError,
    effective_tools_for_client,
    require_serial_tool_calls,
    require_stateless_responses,
    require_tool_calling,
)
from alysis_code.llm.gemini_generate_content import (
    GeminiGenerateContentClient,
    _gemini_schema,
)
from alysis_code.llm.metadata import (
    PROVIDER_METADATA_KEY,
    assistant_message_from_response,
    strip_provider_metadata_from_message,
)
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import LLMError

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

_PROVIDERS = (
    "openai_responses",
    "openai_compat",
    "anthropic_messages",
    "gemini_generate_content",
)
_CALL_ID = "cu_call_0007"
_CALL_ARGUMENTS = {
    "snapshot_id": "snapshot-7",
    "action": "click",
    "element_id": "ax-button-4",
}
_TOOL_RESULT = {"ok": True, "snapshot_id": "snapshot-8"}
_TOOL_RESULT_TEXT = json.dumps(_TOOL_RESULT, separators=(",", ":"), sort_keys=True)


def _computer_use_tools() -> list[dict[str, Any]]:
    definitions = build_computer_use_tools(lambda _name, _arguments: {})
    return [definitions[name].as_openai_tool() for name in COMPUTER_USE_TOOL_NAMES]


def _assert_fixed_computer_use_contract(tools: list[dict[str, Any]]) -> None:
    functions = [tool["function"] for tool in tools]
    assert tuple(function["name"] for function in functions) == COMPUTER_USE_TOOL_NAMES

    schemas = {function["name"]: function["parameters"] for function in functions}
    assert schemas["computer_state"] == {
        "type": "object",
        "properties": {
            "max_elements": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
            },
            "page_cursor": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    "Exact opaque page.next_cursor from a prior chrome-dom observation. "
                    "Omit to start from the first document page."
                ),
            },
        },
        "additionalProperties": False,
    }
    assert schemas["computer_action"]["required"] == ["action"]
    assert "snapshot_id" not in schemas["computer_action"]["properties"]
    assert len(schemas["computer_action"]["oneOf"]) == 9
    text_branch = schemas["computer_action"]["oneOf"][1]
    assert text_branch["properties"]["action"]["enum"] == ["set_value"]
    assert text_branch["required"] == ["element_id", "text"]
    for branch_index, payload in [(2, "text"), (3, "keys")]:
        branch = schemas["computer_action"]["oneOf"][branch_index]
        assert branch["required"] == [payload, "element_id"]
        assert "oneOf" not in branch
    assert "target" not in schemas["computer_action"]["properties"]
    assert "input_mode" not in schemas["computer_action"]["properties"]
    assert {"cmd", "meta", "backspace", "arrowleft"} <= set(
        schemas["computer_action"]["properties"]["keys"]["items"]["enum"]
    )
    assert schemas["computer_action"]["additionalProperties"] is False
    assert schemas["computer_action"]["properties"]["action"]["enum"] == [
        "press",
        "focus",
        "click",
        "type",
        "set_value",
        "key",
        "scroll",
        "drag",
        "history_back",
        "activate_application",
    ]
    action_function = next(
        function for function in functions if function["name"] == "computer_action"
    )
    action_description = action_function["description"]
    element_description = schemas["computer_action"]["properties"]["element_id"]["description"]
    assert (
        "Native legacy background supports semantic press, focus, set_value" in action_description
    )
    assert (
        "No focused-target, input_mode, v2, quit or reopen route is available" in action_description
    )
    assert "Fresh exact observed element ID" in element_description
    assert "set_value" in element_description
    assert "Human input in the controlled application yields control" in COMPUTER_USE_SYSTEM_PROMPT
    history_branch = schemas["computer_action"]["oneOf"][7]
    assert history_branch == {
        "properties": {
            "action": {"type": "string", "enum": ["history_back"]},
        },
        "additionalProperties": False,
    }
    activate_branch = schemas["computer_action"]["oneOf"][8]
    assert activate_branch["properties"]["action"]["enum"] == ["activate_application"]
    assert activate_branch["required"] == ["application"]
    assert schemas["computer_wait"] == {
        "type": "object",
        "properties": {
            "duration_ms": {
                "type": "integer",
                "minimum": 50,
                "maximum": 10000,
            }
        },
        "required": ["duration_ms"],
        "additionalProperties": False,
    }

    assert "The initial application is authorized by the host" in COMPUTER_USE_SYSTEM_PROMPT
    assert "its window may still require selection" in COMPUTER_USE_SYSTEM_PROMPT
    assert "exact host-issued surface_ref" in COMPUTER_USE_SYSTEM_PROMPT
    assert "activate_application" in COMPUTER_USE_SYSTEM_PROMPT
    assert 'action "history_back"' in COMPUTER_USE_SYSTEM_PROMPT
    assert "Never use history_back for a native application target" in COMPUTER_USE_SYSTEM_PROMPT
    assert "background semantic Accessibility actions" in COMPUTER_USE_SYSTEM_PROMPT
    assert "no verified host-owned foreground handoff" in COMPUTER_USE_SYSTEM_PROMPT
    assert "Never repeat an uncertain action blindly" in COMPUTER_USE_SYSTEM_PROMPT
    assert "semantic-DOM behavior is unchanged" in COMPUTER_USE_SYSTEM_PROMPT
    assert "Never copy, guess, or send a" in COMPUTER_USE_SYSTEM_PROMPT
    assert "snapshot_id" in COMPUTER_USE_SYSTEM_PROMPT
    assert "page.next_cursor" in COMPUTER_USE_SYSTEM_PROMPT
    assert "page.complete" in COMPUTER_USE_SYSTEM_PROMPT


def _expected_wire_tools(
    provider: str,
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    functions = [copy.deepcopy(tool["function"]) for tool in tools]
    if provider == "openai_compat":
        return copy.deepcopy(tools)
    if provider == "openai_responses":
        return [{"type": "function", "strict": False, **function} for function in functions]
    if provider == "anthropic_messages":
        return [
            {
                "name": function["name"],
                "description": function["description"],
                "input_schema": function["parameters"],
            }
            for function in functions
        ]
    if provider == "gemini_generate_content":
        for function in functions:
            function["parameters"] = _gemini_schema(function["parameters"])
        return [{"functionDeclarations": functions}]
    raise AssertionError(f"unsupported provider: {provider}")


def _assert_wire_tools(
    provider: str,
    body: dict[str, Any],
    tools: list[dict[str, Any]],
) -> None:
    assert body["tools"] == _expected_wire_tools(provider, tools)
    if provider == "openai_responses":
        assert body["parallel_tool_calls"] is False
        assert body["store"] is False
    elif provider == "anthropic_messages":
        assert body["tool_choice"] == {
            "type": "auto",
            "disable_parallel_tool_use": True,
        }


def _success_response(provider: str, *, first_turn: bool) -> httpx.Response:
    if not first_turn:
        if provider == "openai_responses":
            return httpx.Response(
                200,
                json={"id": "resp-cu-done", "output_text": "done", "output": []},
            )
        if provider == "openai_compat":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
            )
        if provider == "anthropic_messages":
            return httpx.Response(
                200,
                json={
                    "id": "msg-cu-done",
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": "gemini-cu-done",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}],
            },
        )

    if provider == "openai_responses":
        return httpx.Response(
            200,
            json={
                "id": "resp-cu-tools",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs-cu-tools",
                        "encrypted_content": "opaque-cu-reasoning",
                        "summary": [],
                        "status": "completed",
                    },
                    {
                        "type": "function_call",
                        "id": "fc-cu-item",
                        "call_id": _CALL_ID,
                        "name": "computer_action",
                        "arguments": json.dumps(_CALL_ARGUMENTS),
                        "status": "completed",
                    },
                ],
            },
        )
    if provider == "openai_compat":
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-cu-tools",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": _CALL_ID,
                                    "type": "function",
                                    "function": {
                                        "name": "computer_action",
                                        "arguments": json.dumps(_CALL_ARGUMENTS),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )
    if provider == "anthropic_messages":
        return httpx.Response(
            200,
            json={
                "id": "msg-cu-tools",
                "content": [
                    {
                        "type": "tool_use",
                        "id": _CALL_ID,
                        "name": "computer_action",
                        "input": _CALL_ARGUMENTS,
                    }
                ],
                "stop_reason": "tool_use",
            },
        )
    return httpx.Response(
        200,
        json={
            "responseId": "gemini-cu-tools",
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "computer_action",
                                    "args": _CALL_ARGUMENTS,
                                },
                                "thoughtSignature": "cu-thought-signature",
                            }
                        ],
                    }
                }
            ],
        },
    )


def _rejection_response(provider: str) -> httpx.Response:
    if provider in {"openai_responses", "openai_compat"}:
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "param": "tools",
                    "message": "tools are not supported by this model",
                }
            },
        )
    if provider == "anthropic_messages":
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "tools are not supported by this model",
                },
            },
        )
    return httpx.Response(
        400,
        json={
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "function calling is not supported by this model",
            }
        },
    )


def _client(
    provider: str,
    handler: Callable[[httpx.Request], httpx.Response],
) -> Any:
    transport = httpx.MockTransport(handler)
    if provider == "openai_responses":
        return OpenAIResponsesClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-5.5",
            transport=transport,
        )
    if provider == "openai_compat":
        return OpenAICompatClient(
            base_url="https://compatible.example/v1",
            api_key="test-key",
            model="tool-model",
            transport=transport,
        )
    if provider == "anthropic_messages":
        return AnthropicMessagesClient(
            base_url="https://api.anthropic.com/v1",
            api_key="test-key",
            model="claude-sonnet-4-6",
            transport=transport,
        )
    return GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        transport=transport,
    )


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_computer_use_loop_round_trips_exact_contract(provider: str) -> None:
    requests: list[dict[str, Any]] = []
    tools = _computer_use_tools()
    _assert_fixed_computer_use_contract(tools)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return _success_response(provider, first_turn=len(requests) == 1)

    client = _client(provider, handler)
    require_tool_calling(client, COMPUTER_USE_TOOL_NAMES)
    require_serial_tool_calls(client)
    require_stateless_responses(client)
    first = client.chat(
        messages=[{"role": "user", "content": "Click the approved button."}],
        tools=tools,
    )

    _assert_wire_tools(provider, requests[0], tools)
    call_id = first.tool_calls[0].id
    if provider == "gemini_generate_content":
        assert re.fullmatch(r"alysis_gemini_idless_[0-9a-f]{32}", call_id)
    else:
        assert call_id == _CALL_ID
    assert [(call.id, call.name, call.arguments) for call in first.tool_calls] == [
        (call_id, "computer_action", _CALL_ARGUMENTS)
    ]
    assistant = assistant_message_from_response(first)
    assert PROVIDER_METADATA_KEY in assistant
    assert strip_provider_metadata_from_message(assistant) == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "computer_action",
                    "arguments": json.dumps(_CALL_ARGUMENTS),
                },
            }
        ],
    }

    client.chat(
        messages=[
            {"role": "user", "content": "Click the approved button."},
            assistant,
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _TOOL_RESULT_TEXT,
            },
        ],
        tools=tools,
    )

    assert len(requests) == 2
    _assert_wire_tools(provider, requests[1], tools)
    if provider == "openai_responses":
        assert "previous_response_id" not in requests[1]
        reasoning = next(item for item in requests[1]["input"] if item.get("type") == "reasoning")
        assert reasoning == {
            "type": "reasoning",
            "id": "rs-cu-tools",
            "encrypted_content": "opaque-cu-reasoning",
            "summary": [],
        }
        function_call = next(
            item for item in requests[1]["input"] if item.get("type") == "function_call"
        )
        function_output = next(
            item for item in requests[1]["input"] if item.get("type") == "function_call_output"
        )
        assert function_call["call_id"] == call_id
        assert function_output == {
            "type": "function_call_output",
            "call_id": call_id,
            "output": _TOOL_RESULT_TEXT,
        }
        assert all("status" not in item for item in requests[1]["input"])
    elif provider == "openai_compat":
        assert PROVIDER_METADATA_KEY not in requests[1]["messages"][-2]
        assert requests[1]["messages"][-2]["tool_calls"][0]["id"] == call_id
        assert requests[1]["messages"][-1] == {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _TOOL_RESULT_TEXT,
        }
    elif provider == "anthropic_messages":
        assert requests[1]["messages"][-2]["content"][0]["id"] == call_id
        assert requests[1]["messages"][-1] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _TOOL_RESULT_TEXT,
                }
            ],
        }
    else:
        assert "id" not in requests[1]["contents"][-2]["parts"][0]["functionCall"]
        assert requests[1]["contents"][-1] == {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "computer_action",
                        "response": _TOOL_RESULT,
                    }
                }
            ],
        }


def test_openai_responses_iterative_computer_use_normalizes_and_pairs_long_call_ids() -> None:
    requests: list[dict[str, Any]] = []
    tools = _computer_use_tools()
    raw_call_ids = [
        "call_" + ("x" * 61),
        "call_" + ("x" * 60) + "y",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        response_index = len(requests) - 1
        if response_index < len(raw_call_ids):
            return httpx.Response(
                200,
                json={
                    "id": f"resp-cu-long-{response_index}",
                    "model": "gpt-5.5",
                    "output": [
                        {
                            "type": "function_call",
                            "id": f"fc-cu-long-{response_index}",
                            "call_id": raw_call_ids[response_index],
                            "name": "computer_state",
                            "arguments": "{}",
                            "status": "completed",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"id": "resp-cu-long-done", "output_text": "done", "output": []},
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )
    require_stateless_responses(client)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Observe twice, then finish."}]
    normalized_call_ids: list[str] = []

    for snapshot_number in (1, 2):
        response = client.chat(messages=messages, tools=tools)
        assert len(response.tool_calls) == 1
        call_id = response.tool_calls[0].id
        normalized_call_ids.append(call_id)
        assert len(call_id) == 64
        assert call_id.isascii()
        messages.append(assistant_message_from_response(response))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(
                    {"ok": True, "snapshot_id": f"snapshot-{snapshot_number}"},
                    separators=(",", ":"),
                ),
            }
        )

    final = client.chat(messages=messages, tools=tools)
    assert final.content == "done"
    assert normalized_call_ids[0] != normalized_call_ids[1]

    # Both continuations must replay normalized pairs. A server-side
    # previous_response_id still contains the provider's oversized raw ID and
    # therefore cannot safely be used.
    for request in requests[1:]:
        assert "previous_response_id" not in request
        assert request["store"] is False
        function_call_ids = [
            item["call_id"] for item in request["input"] if item.get("type") == "function_call"
        ]
        function_output_ids = [
            item["call_id"]
            for item in request["input"]
            if item.get("type") == "function_call_output"
        ]
        assert function_call_ids == function_output_ids
        assert all(len(call_id) <= 64 and call_id.isascii() for call_id in function_call_ids)

    assert [
        item["call_id"] for item in requests[-1]["input"] if item.get("type") == "function_call"
    ] == normalized_call_ids


def test_openai_responses_stateless_computer_use_survives_payload_retry() -> None:
    requests: list[dict[str, Any]] = []
    tools = _computer_use_tools()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return _success_response("openai_responses", first_turn=True)
        if len(requests) == 2:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "temperature is unsupported; only the default is supported"
                    }
                },
            )
        return _success_response("openai_responses", first_turn=False)

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="stateless-computer-use-model",
        transport=httpx.MockTransport(handler),
    )
    require_tool_calling(client, COMPUTER_USE_TOOL_NAMES)
    require_serial_tool_calls(client)
    require_stateless_responses(client)

    first = client.chat(
        messages=[{"role": "user", "content": "Use the approved app."}],
        tools=tools,
    )
    assistant = assistant_message_from_response(first)
    final = client.chat(
        messages=[
            {"role": "user", "content": "Use the approved app."},
            assistant,
            {
                "role": "tool",
                "tool_call_id": first.tool_calls[0].id,
                "content": _TOOL_RESULT_TEXT,
            },
        ],
        tools=tools,
    )

    assert final.content == "done"
    assert len(requests) == 3
    for body in requests:
        assert body["store"] is False
        assert body["parallel_tool_calls"] is False
        assert "previous_response_id" not in body
    assert requests[1]["input"] == requests[2]["input"]
    assert "temperature" in requests[1]
    assert "temperature" not in requests[2]
    request_plan = final.provider_metadata["openai_responses"]["request_plan"]
    assert request_plan["input_mode"] == "full"
    assert request_plan["previous_response_id_used"] is False
    assert request_plan["sent_input_item_count"] == request_plan["full_input_item_count"]


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_required_computer_use_rejection_never_retries_without_tools(provider: str) -> None:
    requests: list[dict[str, Any]] = []
    tools = _computer_use_tools()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return _rejection_response(provider)

    client = _client(provider, handler)
    require_tool_calling(client, COMPUTER_USE_TOOL_NAMES)
    require_serial_tool_calls(client)
    require_stateless_responses(client)

    with pytest.raises(LLMError):
        client.chat(
            messages=[{"role": "user", "content": "Use the approved app."}],
            tools=tools,
        )

    assert len(requests) == 1
    _assert_wire_tools(provider, requests[0], tools)


def test_openai_compat_cached_rejection_fails_before_a_text_only_request() -> None:
    requests: list[dict[str, Any]] = []
    tools = _computer_use_tools()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if "tools" in body:
            return _rejection_response("openai_compat")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "text only"}}]},
        )

    client = _client("openai_compat", handler)
    require_tool_calling(client, COMPUTER_USE_TOOL_NAMES)
    require_serial_tool_calls(client)
    require_stateless_responses(client)

    with pytest.raises(
        RequiredToolCallingUnsupportedError,
        match="rejected the required Computer Use tools",
    ):
        client.chat(messages=[{"role": "user", "content": "Use the app."}], tools=tools)
    assert len(requests) == 1

    with pytest.raises(
        RequiredToolCallingUnsupportedError,
        match="does not support the required Computer Use tools",
    ):
        client.chat(messages=[{"role": "user", "content": "Continue."}], tools=tools)
    assert len(requests) == 1
    with pytest.raises(RequiredToolCallingUnsupportedError):
        effective_tools_for_client(client, tools)
