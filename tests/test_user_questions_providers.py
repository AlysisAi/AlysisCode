"""Exercise real provider serialization/parsing using deterministic HTTP transports."""

from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.tools.registry import require_builtin_tool_metadata
from alysis_code.user_questions import UserQuestionBroker

ARGS = {
    "questions": [
        {
            "id": "look",
            "question": "Which look?",
            "kind": "single",
            "options": ["Simple", "Detailed"],
        },
        {
            "id": "features",
            "question": "Which features?",
            "kind": "multiple",
            "options": ["History", "Search"],
        },
        {"id": "notes", "question": "Anything else?", "kind": "text", "options": []},
    ]
}


@pytest.mark.parametrize("provider", ["compat", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("status", ["answered", "timed_out"])
def test_question_schema_call_and_answer_roundtrip(provider, status):
    clients = {
        "compat": OpenAICompatClient,
        "responses": OpenAIResponsesClient,
        "anthropic": AnthropicMessagesClient,
        "gemini": GeminiGenerateContentClient,
    }
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        name = "request_user_input"
        if provider == "compat":
            body = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call1",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(ARGS)},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        elif provider == "responses":
            body = {
                "id": "resp1",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call1",
                        "name": name,
                        "arguments": json.dumps(ARGS),
                    }
                ],
            }
        elif provider == "anthropic":
            body = {
                "id": "msg1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call1", "name": name, "input": ARGS}],
                "stop_reason": "tool_use",
            }
        else:
            body = {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"functionCall": {"id": "call1", "name": name, "args": ARGS}}
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
        return httpx.Response(200, json=body)

    client = clients[provider](
        base_url="https://provider.example/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    tools = []
    for name in ("request_user_input", "wait_user_input"):
        metadata = require_builtin_tool_metadata(name)
        tools.append(
            ToolDef(
                name, metadata.description, metadata.copied_parameters(), lambda _: {}
            ).as_openai_tool()
        )
    messages = [{"role": "user", "content": "Help me choose."}]
    response = client.chat(messages=messages, tools=tools)
    assert response.tool_calls[0].name == "request_user_input"
    assert response.tool_calls[0].arguments == ARGS
    wire = json.dumps(payloads[0])
    assert "request_user_input" in wire and "wait_user_input" in wire
    assert '"multiple"' in wire and '"single"' in wire
    messages.append(assistant_message_from_response(response))
    now = [0.0]
    broker = UserQuestionBroker(clock=lambda: now[0])
    pending = broker.request(ARGS)
    if status == "timed_out":
        now[0] = 60.0
        result = broker.collect(pending["request_id"])
    else:
        result = {
            "status": "answered",
            "answers": [{"id": "notes", "text": "Μπλε · custom-answer-42", "selected": []}],
        }
    messages.append(
        {
            "role": "tool",
            "tool_call_id": response.tool_calls[0].id,
            "name": "request_user_input",
            "content": json.dumps(result),
        }
    )
    client.chat(messages=messages, tools=tools)
    serialized = json.dumps(payloads[1], ensure_ascii=False)
    assert ("custom-answer-42" if status == "answered" else "timed_out") in serialized
    if status == "timed_out":
        assert "skipped_question_ids" in serialized
