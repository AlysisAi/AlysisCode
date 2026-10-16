"""The request body a MiMo host receives when thinking is on.

One turn that reasons and calls a tool, then the follow-up request: the body
must carry MiMo's documented thinking switch and send the tool-call turn's
``reasoning_content`` back, on both the non-streaming and the streaming path.
"""

from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.openai_compat import OpenAICompatClient, assistant_message_from_response

_REASONING_PIECES = ("I should ", "look this ", "up first.")
_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look something up.",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
}


def _tool_turn_json() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "".join(_REASONING_PIECES),
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q": "x"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _tool_turn_sse() -> str:
    events = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": piece}}]}
        for piece in _REASONING_PIECES
    ]
    events += [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"q": '},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]},
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _answer_sse() -> str:
    events = [
        {"choices": [{"index": 0, "delta": {"content": "done"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


@pytest.mark.parametrize("stream", [False, True], ids=["non_stream", "stream"])
@pytest.mark.parametrize(
    "base_url",
    ["https://api.xiaomimimo.com/v1", "https://token-plan-ams.xiaomimimo.com/v1"],
    ids=["api", "token_plan_ams"],
)
def test_mimo_follow_up_body_carries_thinking_and_echoes_prior_reasoning(
    base_url: str, stream: bool
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        first_turn = len(bodies) == 1
        if body.get("stream"):
            text = _tool_turn_sse() if first_turn else _answer_sse()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=text.encode()
            )
        if first_turn:
            return httpx.Response(200, json=_tool_turn_json())
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model="mimo-v2.5",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    )
    messages: list[dict] = [{"role": "user", "content": "go"}]

    first = client.chat(messages=messages, tools=[_TOOL], stream=stream)
    messages.append(assistant_message_from_response(first))
    messages.append({"role": "tool", "tool_call_id": "call_1", "content": "result"})
    client.chat(messages=messages, tools=[_TOOL], stream=stream)

    assert len(bodies) == 2
    assert [bool(body.get("stream")) for body in bodies] == [stream, stream]
    for body in bodies:
        assert body["thinking"] == {"type": "enabled"}
    follow_up_assistant = [m for m in bodies[1]["messages"] if m["role"] == "assistant"]
    assert len(follow_up_assistant) == 1
    assert follow_up_assistant[0]["reasoning_content"] == "I should look this up first."
    assert follow_up_assistant[0]["tool_calls"][0]["function"]["name"] == "lookup"
    assert json.loads(follow_up_assistant[0]["tool_calls"][0]["function"]["arguments"]) == {
        "q": "x"
    }
