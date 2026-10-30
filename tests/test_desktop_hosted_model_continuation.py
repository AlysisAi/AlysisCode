"""Hosted desktop requests must survive wide tool catalogs and model changes."""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from alysis_code.agent.tool_discovery import install_tool_discovery, model_visible
from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.llm.openai_compat import OpenAICompatClient


def client(model, handler, *, provider="alysis"):
    return OpenAICompatClient(
        base_url="https://gateway.example.invalid/v1",
        api_key="synthetic-test-only",
        model=model,
        provider_key=provider,
        transport=httpx.MockTransport(handler),
    )


def success(request):
    if json.loads(request.content).get("stream"):
        delta = {"content": "ORCHID", "reasoning_content": "synthetic state"}
        event = {"choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        )
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "ORCHID",
                        "reasoning_content": "synthetic state",
                    }
                }
            ]
        },
    )


@pytest.mark.parametrize(
    "provider,model",
    [
        ("alysis", "reflection-beam"),
        ("together", "reflection/Beam-1.0-501B-A23B"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_reflection_switch_preserves_public_history_without_foreign_reasoning(
    provider, model, stream
):
    producer = client("minimax-m3.1-flash", success)
    old = assistant_message_from_response(
        producer.chat(messages=[{"role": "user", "content": "Hi"}])
    )
    old["tool_calls"] = [
        {
            "id": "old-read",
            "type": "function",
            "function": {
                "name": "fs_read",
                "arguments": '{"path":"note.txt"}',
            },
        }
    ]
    messages = [
        {"role": "system", "content": "Preserve the host rules."},
        {"role": "user", "content": "Remember ORCHID."},
        old,
        {"role": "tool", "tool_call_id": "old-read", "content": "Historical file result."},
        {"role": "developer", "content": "Preserve this later host instruction too."},
        {"role": "user", "content": "What was the word?"},
    ]
    original = copy.deepcopy(messages)
    captured = []

    def strict_reflection(request):
        body = json.loads(request.content)
        captured.append(body)
        for m in body["messages"]:
            if m["role"] == "assistant" and not m.get("reasoning_content", "").strip():
                return httpx.Response(400, json={"error": "assistant turn missing reasoning"})
        return success(request)

    consumer = client(model, strict_reflection, provider=provider)
    assert consumer.chat(messages=messages, stream=stream).content == "ORCHID"
    assert messages == original
    wire = captured[0]["messages"]
    assert "Preserve the host rules." in wire[0]["content"]
    assert "later host instruction" in wire[0]["content"]
    assert wire[-1] == messages[-1]
    history = wire[1]["content"]
    assert "Remember ORCHID." in history
    assert "Historical file result." in history
    assert "old-read" in history and "note.txt" in history
    assert "synthetic state" not in json.dumps(captured[0])
    assert not any(m["role"] == "tool" for m in wire)
    assert "reasoning_content" not in history
    # Capacity and counting must see the same adapted history as generation.
    estimated, _ = consumer._prompt_payload_for_estimation(messages, None)
    assert estimated["messages"] == wire

    native = assistant_message_from_response(consumer.chat(messages=messages, stream=stream))
    continued = messages + [native, {"role": "user", "content": "Continue."}]
    consumer.chat(messages=continued, stream=stream)
    last_native = next(m for m in reversed(captured[-1]["messages"]) if m["role"] == "assistant")
    assert last_native["reasoning_content"] == "synthetic state"


def test_reflection_same_route_tool_continuation_keeps_native_reasoning_and_roles():
    captured = []

    def record(request):
        captured.append(json.loads(request.content))
        return success(request)

    c = client("reflection-beam", record)
    messages = [{"role": "user", "content": "Remember ORCHID."}]
    assistant = assistant_message_from_response(c.chat(messages=messages))
    assistant["tool_calls"] = [
        {
            "id": "native",
            "type": "function",
            "function": {
                "name": "fs_read",
                "arguments": "{}",
            },
        }
    ]
    messages += [assistant, {"role": "tool", "tool_call_id": "native", "content": "OK"}]
    c.chat(messages=messages)
    wire = captured[-1]["messages"]
    assert [m["role"] for m in wire] == ["user", "assistant", "tool"]
    assert wire[1]["reasoning_content"] == "synthetic state"
    assert wire[1]["tool_calls"] == assistant["tool_calls"]


def test_small_hosted_context_uses_discovery_without_disabling_registered_tools():
    c = client("nemotron-3.5-super-vl", success)
    assert c.prefers_deferred_tool_schemas is True
    assert client("glm-5.3-flash", success).prefers_deferred_tool_schemas is False
    assert (
        client("nemotron-3.5-super-vl", success, provider="nvidia").prefers_deferred_tool_schemas
        is False
    )
    calls = []
    tools = {
        name: ToolDef(
            name=name,
            description="Operation reference. " * 25,
            parameters={"type": "object", "properties": {}},
            run=lambda args: calls.append(args) or {"ok": True},
            metadata={"tool_type": "builtin"},
        )
        for name in ["fs_read", "shell_run", *[f"optional_operation_{i}" for i in range(88)]]
    }
    messages = [
        {"role": "system", "content": "Host instructions. " * 2800},
        {"role": "user", "content": "Reply OK."},
    ]
    full = [t.as_openai_tool() for t in tools.values()]
    assert c.request_capacity_ratio(messages=messages, tools=full) > 1
    install_tool_discovery(tools, defer_builtin_schemas=c.prefers_deferred_tool_schemas)

    def visible():
        return [t.as_openai_tool() for t in tools.values() if model_visible(t)]

    assert c.request_capacity_ratio(messages=messages, tools=visible()) < 1
    assert model_visible(tools["fs_read"]) and model_visible(tools["shell_run"])
    search = tools["tool_search"]
    first = search.run({"names": ["optional_operation_0"]})
    assert first["tools"][0]["function"]["name"] == "optional_operation_0"
    assert model_visible(tools["optional_operation_0"])
    search.run({"names": ["optional_operation_1"]})
    assert not model_visible(tools["optional_operation_0"])
    assert model_visible(tools["optional_operation_1"])
    assert tools["optional_operation_0"].run({"test": "guarded handler retained"}) == {"ok": True}
    assert calls == [{"test": "guarded handler retained"}]
    assert c.request_capacity_ratio(messages=messages, tools=visible()) < 1
    with pytest.raises(ValueError, match="Unknown or unavailable"):
        search.run({"names": ["not_registered"]})
