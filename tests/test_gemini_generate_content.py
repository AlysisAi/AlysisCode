from __future__ import annotations

import json
import logging

import httpx
import pytest

from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.metadata import (
    PROVIDER_METADATA_KEY,
    attach_provider_metadata_to_assistant_message,
    stamp_provider_metadata_for_route,
    strip_provider_metadata_from_message,
)
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    provider_call_history_snapshot,
    reset_provider_telemetry_for_tests,
)


def _client(transport: httpx.BaseTransport) -> GeminiGenerateContentClient:
    return GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        transport=transport,
    )


def _web_search_function_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Standalone Alysis Code web search.",
            "parameters": {"type": "object"},
        },
    }


def _fs_read_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "fs_read",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }


class _TruncatedGeminiSummaryStream(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        event = {
            "responseId": "resp_partial",
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Safe partial summary.", "thought": True}],
                    }
                }
            ],
        }
        yield f"data: {json.dumps(event)}\n\n".encode()
        raise httpx.RemoteProtocolError("stream closed after partial summary")


def test_stream_does_not_retry_after_public_reasoning_summary() -> None:
    attempts = 0
    summaries: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_TruncatedGeminiSummaryStream(),
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _seconds: None,
    )

    with pytest.raises(LLMError):
        client.chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
            on_reasoning_delta=summaries.append,
        )

    assert attempts == 1
    assert summaries == ["Safe partial summary."]


def _sse_event(data: dict[str, object]) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _sse_response(*events: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream("".join(events).encode("utf-8")),
    )


def test_count_input_tokens_uses_gemini_count_tokens_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-3-flash-preview:countTokens"
        )
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"totalTokens": 789})

    result = _client(httpx.MockTransport(handler)).count_input_tokens(
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello"},
        ],
        tools=[_fs_read_tool()],
    )

    assert result is not None
    assert result.input_tokens == 789
    assert result.source.value == "provider_count"
    assert result.confidence.value == "authoritative"
    request = captured["generateContentRequest"]
    assert isinstance(request, dict)
    assert request["model"] == "models/gemini-3-flash-preview"
    assert isinstance(request.get("tools"), list)


def test_parse_usage_folds_gemini_thoughts_into_completion_and_reasoning() -> None:
    from alysis_code.llm.gemini_generate_content import _parse_usage

    usage = _parse_usage(
        {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 200,
            "thoughtsTokenCount": 1500,
            "totalTokenCount": 2700,
        }
    )
    assert usage is not None
    # Thinking tokens are billed at the output rate, so they belong in completion.
    assert usage.completion_tokens == 200 + 1500
    assert usage.reasoning_tokens == 1500
    assert usage.prompt_tokens == 1000
    assert usage.total_tokens == 2700
    # prompt + completion now reconciles with the provider total.
    assert usage.prompt_tokens + usage.completion_tokens == usage.total_tokens


def test_parse_usage_without_thoughts_is_unchanged() -> None:
    from alysis_code.llm.gemini_generate_content import _parse_usage

    usage = _parse_usage({"promptTokenCount": 7, "candidatesTokenCount": 5})
    assert usage is not None
    assert usage.completion_tokens == 5
    assert usage.reasoning_tokens is None
    assert usage.total_tokens == 12


def test_parse_usage_includes_tool_use_prompt_tokens_on_input_side() -> None:
    from alysis_code.llm.gemini_generate_content import _parse_usage

    raw_usage = {
        "promptTokenCount": 7,
        "toolUsePromptTokenCount": 11,
        "candidatesTokenCount": 5,
        "thoughtsTokenCount": 3,
        "totalTokenCount": 26,
    }
    usage = _parse_usage(raw_usage)

    assert usage is not None
    assert usage.prompt_tokens == 18
    assert usage.completion_tokens == 8
    assert usage.reasoning_tokens == 3
    assert usage.total_tokens == 26
    assert usage.raw_provider_usage == raw_usage


def test_chat_maps_messages_tools_tool_choice_response_format_reasoning_and_usage() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-3.1-pro-preview:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "test-key"
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "responseId": "resp_text",
                "modelVersion": "gemini-3.1-pro-preview",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "Done."}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 14,
                    "cachedContentTokenCount": 5,
                },
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="test-key",
        model="gemini-3.1-pro-preview",
        temperature=0.4,
        reasoning_effort="low",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "system", "content": "System prompt."},
            {"role": "developer", "content": "Developer prompt."},
            {"role": "user", "content": "Read it."},
            {
                "role": "assistant",
                "content": "I will read.",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {
                            "name": "fs_read",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_read", "content": "README contents"},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "gs://bucket/image.png"}},
                    {"type": "text", "text": "Describe the image."},
                ],
            },
        ],
        tools=[_fs_read_tool()],
        tool_choice={"type": "function", "function": {"name": "fs_read"}},
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "result",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                "strict": True,
            },
        },
        max_tokens=123,
    )

    assert captured["systemInstruction"] == {
        "parts": [{"text": "System prompt.\n\nDeveloper prompt."}]
    }
    assert captured["contents"] == [
        {"role": "user", "parts": [{"text": "Read it."}]},
        {
            "role": "model",
            "parts": [
                {"text": "I will read."},
                {
                    "functionCall": {
                        "name": "fs_read",
                        "args": {"path": "README.md"},
                        "id": "call_read",
                    },
                    "thoughtSignature": "skip_thought_signature_validator",
                },
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_read",
                        "name": "fs_read",
                        "response": {"result": "README contents"},
                    }
                }
            ],
        },
        {
            "role": "user",
            "parts": [
                {"fileData": {"fileUri": "gs://bucket/image.png"}},
                {"text": "Describe the image."},
            ],
        },
    ]
    assert captured["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "fs_read",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ]
        }
    ]
    assert captured["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["fs_read"]}
    }
    assert captured["generationConfig"] == {
        "maxOutputTokens": 123,
        "responseMimeType": "application/json",
        "responseSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        "thinkingConfig": {"thinkingLevel": "low"},
    }
    assert response.content == "Done."
    assert response.response_model == "gemini-3.1-pro-preview"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 3
    assert response.usage.total_tokens == 14
    assert response.usage.cached_prompt_tokens == 5
    assert response.usage.cache_read_input_tokens == 5
    assert response.usage.input_tokens_uncached == 6
    assert response.provider_metadata is not None
    assert response.provider_metadata["gemini_generate_content"]["response_id"] == "resp_text"
    request_plan = response.provider_metadata["gemini_generate_content"]["request_plan"]
    assert request_plan["temperature_omitted"] is True
    assert request_plan["temperature_omit_reason"] == "gemini_3_default_temperature"


def test_chat_requests_and_separates_provider_thought_summaries() -> None:
    captured: dict[str, object] = {}
    reasoning_deltas: list[str] = []
    original_parts = [
        {
            "text": "Check the constraints first.",
            "thought": True,
            "thoughtSignature": "opaque-provider-signature",
        },
        {"text": "Done."},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "responseId": "resp_thought_summary",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": original_parts},
                        "finishReason": "STOP",
                    }
                ],
            },
        )

    response = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        reasoning_effort="low",
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "hello"}],
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert captured["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "low", "includeThoughts": True}
    }
    assert reasoning_deltas == ["Check the constraints first."]
    assert response.content == "Done."
    assert [item.text for item in response.reasoning] == ["Check the constraints first."]
    assert "opaque-provider-signature" not in "".join(reasoning_deltas)
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    assert metadata["content"]["parts"] == original_parts


def test_thought_summary_rejection_retries_without_visibility_field_and_caches() -> None:
    requests: list[dict[str, object]] = []
    summaries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"error": {"message": 'Unknown name "includeThoughts"'}},
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "Opaque fallback thought.", "thought": True},
                                {"text": "Done."},
                            ],
                        }
                    }
                ]
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        reasoning_effort="low",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        on_reasoning_delta=summaries.append,
    )
    second = client.chat(
        messages=[{"role": "user", "content": "again"}],
        on_reasoning_delta=summaries.append,
    )

    assert requests[0]["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "low", "includeThoughts": True}
    }
    assert requests[1]["generationConfig"] == {"thinkingConfig": {"thinkingLevel": "low"}}
    assert requests[2]["generationConfig"] == {"thinkingConfig": {"thinkingLevel": "low"}}
    assert first.content == second.content == "Done."
    assert first.reasoning == second.reasoning == ()
    assert summaries == []


def test_stream_thought_summary_rejection_preserves_budget_and_suppresses_fallback_raw() -> None:
    requests: list[dict[str, object]] = []
    summaries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        if len(requests) == 1:
            return httpx.Response(
                422,
                json={"error": {"message": "includeThoughts is not supported"}},
            )
        return _sse_response(
            _sse_event(
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"text": "Opaque fallback thought.", "thought": True},
                                    {"text": "Done."},
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ]
                }
            )
        )

    response = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-2.5-flash",
        thinking_budget=256,
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        on_reasoning_delta=summaries.append,
    )

    assert requests[0]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 256,
        "includeThoughts": True,
    }
    assert requests[1]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 256,
    }
    assert response.content == "Done."
    assert response.reasoning == ()
    assert summaries == []


def test_gemini_2_5_keeps_explicit_temperature() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-2.5-flash",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "Done."}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 3,
                },
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-2.5-flash",
        temperature=0.4,
        transport=httpx.MockTransport(handler),
    )

    assert client.chat(messages=[{"role": "user", "content": "hello"}]).content == "Done."
    generation_config = captured["generationConfig"]
    assert isinstance(generation_config, dict)
    assert generation_config["temperature"] == 0.4


def test_chat_imported_parallel_function_calls_get_dummy_thought_signatures() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    _client(httpx.MockTransport(handler)).chat(
        messages=[
            {"role": "user", "content": "Inspect."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {
                            "name": "fs_read",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    },
                    {
                        "id": "call_shell",
                        "type": "function",
                        "function": {
                            "name": "shell_run",
                            "arguments": json.dumps({"cmd": "pytest -q"}),
                        },
                    },
                ],
            },
        ],
        tools=[
            _fs_read_tool(),
            {
                "type": "function",
                "function": {"name": "shell_run", "parameters": {"type": "object"}},
            },
        ],
    )

    assert captured["contents"][1]["parts"] == [  # type: ignore[index]
        {
            "functionCall": {
                "name": "fs_read",
                "args": {"path": "README.md"},
                "id": "call_read",
            },
            "thoughtSignature": "skip_thought_signature_validator",
        },
        {
            "functionCall": {
                "name": "shell_run",
                "args": {"cmd": "pytest -q"},
                "id": "call_shell",
            },
            "thoughtSignature": "skip_thought_signature_validator",
        },
    ]


def test_chat_parses_parallel_function_calls_exact_ids_and_thought_signatures() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "responseId": "resp_tools",
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "id": "call_read",
                                        "name": "fs_read",
                                        "args": {"path": "README.md"},
                                    },
                                    "thoughtSignature": "thought-read",
                                },
                                {
                                    "functionCall": {
                                        "id": "call_shell",
                                        "name": "shell_run",
                                        "args": {"cmd": "pytest -q"},
                                    },
                                    "thought_signature": "thought-shell",
                                },
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 5},
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "Inspect."}],
        tools=[
            _fs_read_tool(),
            {
                "type": "function",
                "function": {"name": "shell_run", "parameters": {"type": "object"}},
            },
        ],
    )

    assert response.content == ""
    assert [(tc.id, tc.name, tc.arguments) for tc in response.tool_calls] == [
        ("call_read", "fs_read", {"path": "README.md"}),
        ("call_shell", "shell_run", {"cmd": "pytest -q"}),
    ]
    assert response.tool_calls[0].provider_metadata == {
        "gemini_generate_content": {"part_index": 0, "thoughtSignature": "thought-read"}
    }
    assert response.tool_calls[1].provider_metadata == {
        "gemini_generate_content": {"part_index": 1, "thought_signature": "thought-shell"}
    }
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["gemini_generate_content"]
    assert metadata["content"]["parts"][0]["thoughtSignature"] == "thought-read"
    assert response.usage is not None
    assert response.usage.total_tokens == 12


def test_chat_function_call_and_function_response_round_trip_replays_metadata() -> None:
    calls: list[dict[str, object]] = []
    function_call_part = {
        "functionCall": {
            "id": "call_exact_123",
            "name": "fs_read",
            "args": {"path": "README.md"},
        },
        "thoughtSignature": "thought-exact",
    }
    provider_content = {"role": "model", "parts": [function_call_part]}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "responseId": "resp_call",
                    "candidates": [{"content": provider_content, "finishReason": "STOP"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": "resp_done",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Read it."}]}}],
            },
        )

    client = _client(httpx.MockTransport(handler))
    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[_fs_read_tool()],
    )
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_exact_123", "content": "file contents"},
        ],
    )

    assert calls[1]["contents"] == [
        {"role": "user", "parts": [{"text": "read"}]},
        provider_content,
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_exact_123",
                        "name": "fs_read",
                        "response": {"result": "file contents"},
                    }
                }
            ],
        },
    ]


def test_chat_replays_snake_case_thought_signature_as_gemini_wire_key() -> None:
    calls: list[dict[str, object]] = []
    provider_content = {
        "role": "model",
        "parts": [
            {
                "functionCall": {
                    "id": "call_snake_sig",
                    "name": "fs_read",
                    "args": {"path": "README.md"},
                },
                "thought_signature": "thought-from-provider",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "responseId": "resp_call",
                    "candidates": [{"content": provider_content, "finishReason": "STOP"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": "resp_done",
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            },
        )

    client = _client(httpx.MockTransport(handler))
    first = client.chat(messages=[{"role": "user", "content": "read"}], tools=[_fs_read_tool()])
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_snake_sig", "content": "README"},
        ]
    )

    replay_part = calls[1]["contents"][1]["parts"][0]  # type: ignore[index]
    assert replay_part["thoughtSignature"] == "thought-from-provider"
    assert "thought_signature" not in replay_part


def test_chat_text_only_response_preserves_provider_metadata_for_next_turn() -> None:
    calls: list[dict[str, object]] = []
    provider_content = {"role": "model", "parts": [{"text": "First answer."}]}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "responseId": "resp_text_metadata",
                    "candidates": [{"content": provider_content, "finishReason": "STOP"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": "resp_second",
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "Second answer."}]}}
                ],
            },
        )

    client = _client(httpx.MockTransport(handler))
    first = client.chat(messages=[{"role": "user", "content": "first"}])
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )

    assert PROVIDER_METADATA_KEY in assistant_message
    assert strip_provider_metadata_from_message(assistant_message) == {
        "role": "assistant",
        "content": "First answer.",
    }

    second = client.chat(
        messages=[
            {"role": "user", "content": "first"},
            assistant_message,
            {"role": "user", "content": "second"},
        ],
    )

    assert second.content == "Second answer."
    assert calls[1]["contents"] == [
        {"role": "user", "parts": [{"text": "first"}]},
        provider_content,
        {"role": "user", "parts": [{"text": "second"}]},
    ]


def test_chat_native_mode_uses_only_gemini_google_search_grounding() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "responseId": "resp_search",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="native",
        web_search_adapter="gemini_grounding",
        transport=httpx.MockTransport(handler),
    )
    response = client.chat(
        messages=[{"role": "user", "content": "find docs"}],
        tools=[_web_search_function_tool(), _fs_read_tool()],
    )

    assert captured["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "fs_read",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ]
        },
        {"google_search": {}},
    ]
    assert captured["toolConfig"] == {"includeServerSideToolInvocations": True}
    assert response.content == "ok"


def test_chat_auto_mode_prefers_google_search_grounding_for_auto_adapter() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="auto",
        web_search_adapter="auto",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}], tools=[_web_search_function_tool()]
    )

    assert captured["tools"] == [{"google_search": {}}]
    assert "toolConfig" not in captured


def test_chat_combined_grounding_and_tool_choice_merges_tool_config() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="native",
        web_search_adapter="gemini_grounding",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}],
        tools=[_web_search_function_tool(), _fs_read_tool()],
        tool_choice="auto",
    )

    assert captured["toolConfig"] == {
        "functionCallingConfig": {"mode": "AUTO"},
        "includeServerSideToolInvocations": True,
    }


def test_chat_external_mode_uses_only_alysis_web_search_function() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="external",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}], tools=[_web_search_function_tool()]
    )

    assert captured["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "web_search",
                    "description": "Standalone Alysis Code web search.",
                    "parameters": {"type": "object"},
                }
            ]
        }
    ]


def test_chat_auto_mode_with_external_adapter_uses_alysis_web_search_function() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="auto",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}], tools=[_web_search_function_tool()]
    )

    assert captured["tools"][0]["functionDeclarations"][0]["name"] == "web_search"


def test_chat_off_mode_removes_all_web_search_tools() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="off",
        web_search_adapter="gemini_grounding",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}],
        tools=[_web_search_function_tool(), {"google_search": {}}],
    )

    assert "tools" not in captured


def test_chat_native_mode_rejects_non_gemini_search_adapter() -> None:
    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="native",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(LLMError, match="web_search_mode=native.*gemini_grounding"):
        client.chat(
            messages=[{"role": "user", "content": "find docs"}],
            tools=[_web_search_function_tool()],
        )


def test_chat_rejects_forced_google_search_tool_choice() -> None:
    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="native",
        web_search_adapter="gemini_grounding",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(LLMError, match="cannot force Google Search grounding"):
        client.chat(
            messages=[{"role": "user", "content": "find docs"}],
            tools=[_web_search_function_tool()],
            tool_choice={"type": "function", "function": {"name": "web_search"}},
        )


def test_chat_thinking_level_and_budget_are_emitted_for_the_model_contract() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        thinking_level="high",
        transport=httpx.MockTransport(handler),
    ).chat(messages=[{"role": "user", "content": "hello"}])
    assert captured[-1]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}

    GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-2.5-pro",
        thinking_budget=-1,
        transport=httpx.MockTransport(handler),
    ).chat(messages=[{"role": "user", "content": "hello"}])
    assert captured[-1]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": -1}

    with pytest.raises(LLMError, match="thinking_level requires a Gemini 3 model"):
        GeminiGenerateContentClient(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="test-key",
            model="gemini-2.5-pro",
            thinking_level="low",
            transport=httpx.MockTransport(handler),
        ).chat(messages=[{"role": "user", "content": "hello"}])

    with pytest.raises(LLMError, match="cannot set both thinking_level and thinking_budget"):
        GeminiGenerateContentClient(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="test-key",
            model="gemini-3-flash-preview",
            thinking_level="low",
            thinking_budget=1024,
            transport=httpx.MockTransport(handler),
        ).chat(messages=[{"role": "user", "content": "hello"}])


@pytest.mark.parametrize(
    ("client_kwargs", "expected_level"),
    [
        ({"thinking_level": "medium"}, "medium"),
        ({"reasoning_effort": "medium"}, "medium"),
        ({"reasoning_effort": "minimal"}, "medium"),
        ({"enable_thinking": False}, "medium"),
    ],
)
def test_gemini37_native_thinking_uses_the_documented_contract(
    client_kwargs: dict[str, object],
    expected_level: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3.7-flash",
        transport=httpx.MockTransport(handler),
        **client_kwargs,
    )

    assert client.chat(messages=[{"role": "user", "content": "hello"}]).content == "ok"
    assert captured["generationConfig"]["thinkingConfig"] == {"thinkingLevel": expected_level}


def test_gemini37_native_rejects_explicit_minimal_thinking_level() -> None:
    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3.7-flash",
        thinking_level="minimal",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(LLMError, match="thinking_level must be one of: low, medium, high"):
        client.chat(messages=[{"role": "user", "content": "hello"}])


def test_chat_extracts_grounding_metadata_citations_sources_queries_and_tool_calls() -> None:
    grounding = {
        "webSearchQueries": ["gemini docs"],
        "groundingChunks": [
            {"web": {"uri": "https://ai.google.dev/gemini-api/docs", "title": "Gemini Docs"}}
        ],
        "groundingSupports": [
            {
                "segment": {"startIndex": 0, "endIndex": 8, "text": "Use docs"},
                "groundingChunkIndices": [0],
            }
        ],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "responseId": "resp_grounded",
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "Use docs."},
                                {
                                    "functionCall": {
                                        "id": "call_read",
                                        "name": "fs_read",
                                        "args": {"path": "README.md"},
                                    }
                                },
                            ],
                        },
                        "groundingMetadata": grounding,
                        "finishReason": "STOP",
                    }
                ],
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "find docs"}],
        tools=[_fs_read_tool()],
    )

    assert response.content == "Use docs."
    assert response.tool_calls[0].id == "call_read"
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["gemini_generate_content"]
    assert metadata["content"]["parts"][1]["functionCall"]["id"] == "call_read"
    assert metadata["groundingMetadata"] == grounding
    assert metadata["queries"] == ["gemini docs"]
    assert metadata["sources"] == [
        {"url": "https://ai.google.dev/gemini-api/docs", "title": "Gemini Docs"}
    ]
    assert metadata["citations"] == [
        {
            "title": "Gemini Docs",
            "url": "https://ai.google.dev/gemini-api/docs",
            "start_index": 0,
            "end_index": 8,
            "text": "Use docs",
        }
    ]


def test_chat_grounded_function_call_round_trip_replays_provider_content() -> None:
    calls: list[dict[str, object]] = []
    grounding = {
        "webSearchQueries": ["gemini docs"],
        "groundingChunks": [
            {"web": {"uri": "https://ai.google.dev/gemini-api/docs", "title": "Gemini Docs"}}
        ],
    }
    provider_content = {
        "role": "model",
        "parts": [
            {"text": "I found docs."},
            {
                "functionCall": {
                    "id": "call_grounded_read",
                    "name": "fs_read",
                    "args": {"path": "README.md"},
                },
                "thoughtSignature": "grounded-thought",
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "responseId": "resp_grounded_call",
                    "candidates": [
                        {
                            "content": provider_content,
                            "groundingMetadata": grounding,
                            "finishReason": "STOP",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]},
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="native",
        web_search_adapter="gemini_grounding",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "find and read"}],
        tools=[_web_search_function_tool(), _fs_read_tool()],
    )
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    assert first.provider_metadata is not None
    metadata = first.provider_metadata["gemini_generate_content"]
    assert metadata["groundingMetadata"] == grounding
    assert metadata["content"] == provider_content

    client.chat(
        messages=[
            {"role": "user", "content": "find and read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_grounded_read", "content": "file contents"},
        ],
        tools=[_web_search_function_tool(), _fs_read_tool()],
    )

    assert calls[0]["toolConfig"] == {"includeServerSideToolInvocations": True}
    assert calls[1]["toolConfig"] == {"includeServerSideToolInvocations": True}
    assert calls[1]["contents"] == [
        {"role": "user", "parts": [{"text": "find and read"}]},
        provider_content,
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_grounded_read",
                        "name": "fs_read",
                        "response": {"result": "file contents"},
                    }
                }
            ],
        },
    ]


def test_chat_streams_text_usage_and_metadata() -> None:
    captured: dict[str, object] = {}
    deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-3-flash-preview:streamGenerateContent?alt=sse"
        )
        captured.update(json.loads(request.content.decode("utf-8")))
        return _sse_response(
            _sse_event(
                {
                    "responseId": "resp_stream_text",
                    "modelVersion": "gemini-3-flash-preview",
                    "candidates": [{"content": {"role": "model", "parts": [{"text": "Hello "}]}}],
                }
            ),
            _sse_event(
                {
                    "responseId": "resp_stream_text",
                    "modelVersion": "gemini-3-flash-preview",
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "world."}]},
                            "finishReason": "STOP",
                            "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT"}],
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 3,
                        "totalTokenCount": 13,
                    },
                }
            ),
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        on_text_delta=deltas.append,
    )

    assert captured["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert response.content == "Hello world."
    assert deltas == ["Hello ", "world."]
    assert response.response_model == "gemini-3-flash-preview"
    assert response.usage is not None
    assert response.usage.total_tokens == 13
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    assert metadata["response_id"] == "resp_stream_text"
    assert metadata["model_version"] == "gemini-3-flash-preview"
    assert metadata["finish_reason"] == "STOP"
    assert metadata["safety_ratings"] == [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT"}]
    assert metadata["content"]["parts"] == [{"text": "Hello "}, {"text": "world."}]


def test_chat_streams_thought_summaries_separately_and_preserves_metadata() -> None:
    captured: dict[str, object] = {}
    text_deltas: list[str] = []
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return _sse_response(
            _sse_event(
                {
                    "responseId": "resp_stream_thought",
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "text": "Inspect the request.",
                                        "thought": True,
                                        "thoughtSignature": "opaque-stream-signature",
                                    }
                                ],
                            }
                        }
                    ],
                }
            ),
            _sse_event(
                {
                    "responseId": "resp_stream_thought",
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "Done."}]},
                            "finishReason": "STOP",
                        }
                    ],
                }
            ),
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        on_text_delta=text_deltas.append,
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert captured["generationConfig"] == {"thinkingConfig": {"includeThoughts": True}}
    assert reasoning_deltas == ["Inspect the request."]
    assert text_deltas == ["Done."]
    assert response.content == "Done."
    assert [item.text for item in response.reasoning] == ["Inspect the request."]
    assert "opaque-stream-signature" not in "".join(reasoning_deltas)
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    assert metadata["content"]["parts"] == [
        {
            "text": "Inspect the request.",
            "thought": True,
            "thoughtSignature": "opaque-stream-signature",
        },
        {"text": "Done."},
    ]


def test_chat_streams_single_function_call_with_id_and_thought_signature() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                {
                    "responseId": "resp_stream_call",
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "call_stream_read",
                                            "name": "fs_read",
                                            "args": {"path": "README.md"},
                                        },
                                        "thoughtSignature": "stream-thought",
                                    }
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                }
            )
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[_fs_read_tool()],
        stream=True,
    )

    assert response.content == ""
    assert [(tool.id, tool.name, tool.arguments) for tool in response.tool_calls] == [
        ("call_stream_read", "fs_read", {"path": "README.md"})
    ]
    assert response.tool_calls[0].provider_metadata == {
        "gemini_generate_content": {"part_index": 0, "thoughtSignature": "stream-thought"}
    }
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    assert metadata["content"]["parts"][0]["thoughtSignature"] == "stream-thought"


def test_chat_streams_function_call_without_provider_id_keeps_native_part_idless() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                {
                    "responseId": "resp_stream_call_without_id",
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"text": "I will read."},
                                    {
                                        "functionCall": {
                                            "name": "fs_read",
                                            "args": {"path": "README.md"},
                                        },
                                        "thoughtSignature": "stream-thought",
                                    },
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                }
            )
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[_fs_read_tool()],
        stream=True,
    )

    assert response.content == "I will read."
    assert len(response.tool_calls) == 1
    tool_call = response.tool_calls[0]
    assert tool_call.id.startswith("alysis_gemini_idless_")
    assert len(tool_call.id) == len("alysis_gemini_idless_") + 32
    assert (tool_call.name, tool_call.arguments) == ("fs_read", {"path": "README.md"})
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    function_call = metadata["content"]["parts"][1]["functionCall"]
    assert "id" not in function_call
    assert response.tool_calls[0].provider_metadata == {
        "gemini_generate_content": {"part_index": 1, "thoughtSignature": "stream-thought"}
    }


def test_chat_streams_parallel_function_calls_in_exact_part_order() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "call_read",
                                            "name": "fs_read",
                                            "args": {"path": "README.md"},
                                        },
                                        "thoughtSignature": "thought-read",
                                    },
                                    {
                                        "functionCall": {
                                            "id": "call_shell",
                                            "name": "shell_run",
                                            "args": {"cmd": "pytest -q"},
                                        },
                                        "thoughtSignature": "thought-shell",
                                    },
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ]
                }
            )
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "inspect"}],
        tools=[
            _fs_read_tool(),
            {
                "type": "function",
                "function": {"name": "shell_run", "parameters": {"type": "object"}},
            },
        ],
        stream=True,
    )

    assert [(tool.id, tool.name, tool.arguments) for tool in response.tool_calls] == [
        ("call_read", "fs_read", {"path": "README.md"}),
        ("call_shell", "shell_run", {"cmd": "pytest -q"}),
    ]
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    assert [part["functionCall"]["id"] for part in metadata["content"]["parts"]] == [
        "call_read",
        "call_shell",
    ]
    assert metadata["content"]["parts"][1]["thoughtSignature"] == "thought-shell"


def test_chat_streams_mixed_text_function_call_and_replays_function_response() -> None:
    calls: list[dict[str, object]] = []
    provider_content = {
        "role": "model",
        "parts": [
            {"text": "I will read."},
            {
                "functionCall": {
                    "id": "call_stream_mixed",
                    "name": "fs_read",
                    "args": {"path": "README.md"},
                },
                "thoughtSignature": "mixed-thought",
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        if len(calls) == 1:
            return _sse_response(
                _sse_event(
                    {
                        "candidates": [
                            {"content": {"role": "model", "parts": [{"text": "I will read."}]}}
                        ]
                    }
                ),
                _sse_event(
                    {
                        "responseId": "resp_stream_mixed",
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "id": "call_stream_mixed",
                                                "name": "fs_read",
                                                "args": {"path": "README.md"},
                                            },
                                            "thoughtSignature": "mixed-thought",
                                        }
                                    ],
                                },
                                "finishReason": "STOP",
                            }
                        ],
                    }
                ),
            )
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}]},
        )

    client = _client(httpx.MockTransport(handler))
    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[_fs_read_tool()],
        stream=True,
    )
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_stream_mixed", "content": "file contents"},
        ],
        tools=[_fs_read_tool()],
    )

    assert calls[1]["contents"] == [
        {"role": "user", "parts": [{"text": "read"}]},
        provider_content,
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_stream_mixed",
                        "name": "fs_read",
                        "response": {"result": "file contents"},
                    }
                }
            ],
        },
    ]


def test_chat_stream_preserves_google_search_grounding_metadata() -> None:
    captured: dict[str, object] = {}
    grounding = {
        "webSearchQueries": ["gemini stream grounding"],
        "groundingChunks": [
            {"web": {"uri": "https://ai.google.dev/gemini-api/docs", "title": "Gemini Docs"}}
        ],
        "groundingSupports": [
            {
                "segment": {"startIndex": 0, "endIndex": 8, "text": "Use docs"},
                "groundingChunkIndices": [0],
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return _sse_response(
            _sse_event(
                {
                    "responseId": "resp_stream_grounded",
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "Use docs."}]},
                            "groundingMetadata": grounding,
                            "finishReason": "STOP",
                        }
                    ],
                }
            )
        )

    response = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        web_search_mode="native",
        web_search_adapter="gemini_grounding",
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "find docs"}],
        tools=[_web_search_function_tool()],
        stream=True,
    )

    assert captured["tools"] == [{"google_search": {}}]
    assert response.content == "Use docs."
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    assert metadata["groundingMetadata"] == grounding
    assert metadata["queries"] == ["gemini stream grounding"]
    assert metadata["sources"] == [
        {"url": "https://ai.google.dev/gemini-api/docs", "title": "Gemini Docs"}
    ]
    assert metadata["citations"] == [
        {
            "title": "Gemini Docs",
            "url": "https://ai.google.dev/gemini-api/docs",
            "start_index": 0,
            "end_index": 8,
            "text": "Use docs",
        }
    ]
    assert "groundingMetadata" not in response.content


def test_chat_stream_preserves_grounding_and_function_call_metadata_together() -> None:
    grounding = {
        "webSearchQueries": ["gemini docs"],
        "groundingChunks": [
            {"web": {"uri": "https://ai.google.dev/gemini-api/docs", "title": "Gemini Docs"}}
        ],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                {
                    "responseId": "resp_grounded_stream_call",
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"text": "I found docs."},
                                    {
                                        "functionCall": {
                                            "id": "call_grounded_stream",
                                            "name": "fs_read",
                                            "args": {"path": "README.md"},
                                        },
                                        "thoughtSignature": "grounded-stream-thought",
                                    },
                                ],
                            },
                            "groundingMetadata": grounding,
                            "finishReason": "STOP",
                        }
                    ],
                }
            )
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "find and read"}],
        tools=[_web_search_function_tool(), _fs_read_tool()],
        stream=True,
    )

    assert response.content == "I found docs."
    assert response.tool_calls[0].id == "call_grounded_stream"
    metadata = response.provider_metadata["gemini_generate_content"]  # type: ignore[index]
    assert metadata["groundingMetadata"] == grounding
    assert metadata["content"]["parts"][1]["thoughtSignature"] == "grounded-stream-thought"


def test_chat_stream_surfaces_malformed_error_and_empty_streams() -> None:
    with pytest.raises(LLMError, match="LLM error 400: INVALID_ARGUMENT: bad stream request"):
        _client(
            httpx.MockTransport(
                lambda _request: httpx.Response(
                    400,
                    json={
                        "error": {
                            "status": "INVALID_ARGUMENT",
                            "message": "bad stream request",
                        }
                    },
                )
            )
        ).chat(messages=[{"role": "user", "content": "hello"}], stream=True)

    with pytest.raises(LLMError, match="malformed JSON"):
        _client(
            httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=httpx.ByteStream(b"data: {bad\n\n"),
                )
            )
        ).chat(messages=[{"role": "user", "content": "hello"}], stream=True)

    with pytest.raises(LLMError, match="INVALID_ARGUMENT: bad stream"):
        _client(
            httpx.MockTransport(
                lambda _request: _sse_response(
                    _sse_event({"error": {"status": "INVALID_ARGUMENT", "message": "bad stream"}})
                )
            )
        ).chat(messages=[{"role": "user", "content": "hello"}], stream=True)

    with pytest.raises(LLMError, match="returned no chunks"):
        _client(httpx.MockTransport(lambda _request: _sse_response())).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )

    with pytest.raises(LLMError, match="returned no candidate chunks"):
        _client(httpx.MockTransport(lambda _request: _sse_response(_sse_event({})))).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )


def test_chat_surfaces_provider_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"status": "INVALID_ARGUMENT", "message": "bad request"}},
        )

    with pytest.raises(LLMError, match="LLM error 400: INVALID_ARGUMENT: bad request"):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )


def test_chat_uses_explicit_cached_content_when_enabled_and_reuses_cache() -> None:
    reset_provider_telemetry_for_tests()
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            return httpx.Response(
                200,
                json={
                    "name": "cachedContents/cache_1",
                    "usageMetadata": {"totalTokenCount": 42},
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{len(calls)}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 105,
                    "cachedContentTokenCount": 80,
                },
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_ttl="60s",
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )
    prefix = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]

    first = client.chat(messages=[*prefix, {"role": "user", "content": "second"}])
    second = client.chat(messages=[*prefix, {"role": "user", "content": "third"}])

    cache_calls = [body for path, body in calls if path.endswith("/cachedContents")]
    generate_calls = [body for path, body in calls if path.endswith(":generateContent")]
    assert len(cache_calls) == 1
    assert cache_calls[0]["model"] == "models/gemini-3-flash-preview"
    assert cache_calls[0]["ttl"] == "60s"
    assert "systemInstruction" in cache_calls[0]
    assert len(cache_calls[0]["contents"]) == 2
    assert len(generate_calls) == 2
    assert generate_calls[0]["cachedContent"] == "cachedContents/cache_1"
    assert generate_calls[0]["contents"] == [{"role": "user", "parts": [{"text": "second"}]}]
    assert "systemInstruction" not in generate_calls[0]
    assert generate_calls[1]["cachedContent"] == "cachedContents/cache_1"
    assert generate_calls[1]["contents"] == [{"role": "user", "parts": [{"text": "third"}]}]
    assert first.provider_metadata is not None
    assert first.provider_metadata["gemini_generate_content"]["cache_policy"]["status"] == (
        "created"
    )
    assert first.usage is not None
    assert first.usage.cache_creation_input_tokens == 42
    assert first.usage.prompt_tokens == 100
    assert second.provider_metadata is not None
    assert second.provider_metadata["gemini_generate_content"]["cache_policy"]["status"] == (
        "reused"
    )
    assert second.usage is not None
    assert second.usage.cache_creation_input_tokens is None
    summaries = provider_call_history_snapshot(limit=2)
    assert [item["cache_policy"]["status"] for item in summaries] == ["created", "reused"]
    assert [item["cache_policy"]["used"] for item in summaries] == [True, True]
    assert [item["usage"]["cache_creation_input_tokens"] for item in summaries] == [42, None]
    assert summaries[0]["token_reconciliation"]["input_mode"] == "cached_content"
    assert summaries[1]["token_reconciliation"]["input_mode"] == "cached_content"
    assert (
        summaries[1]["token_reconciliation"]["input_estimate_tokens"]
        > (summaries[1]["token_reconciliation"]["sent_input_estimate_tokens"])
    )
    assert summaries[1]["token_reconciliation"]["reported_prompt_tokens"] == 100


def test_chat_retries_full_payload_when_reused_cached_content_is_stale() -> None:
    reset_provider_telemetry_for_tests()
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            calls.append((request.url.path, {}))
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            return httpx.Response(200, json={"name": "cachedContents/cache_1"})
        generate_calls = [path for path, _body in calls if path.endswith(":generateContent")]
        if body.get("cachedContent") == "cachedContents/cache_1" and len(generate_calls) == 2:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "status": "NOT_FOUND",
                        "message": "Cached content cachedContents/cache_1 not found",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{len(calls)}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 105,
                    "cachedContentTokenCount": 80 if "cachedContent" in body else 0,
                },
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_ttl="60s",
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )
    prefix = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]

    first = client.chat(messages=[*prefix, {"role": "user", "content": "second"}])
    second = client.chat(messages=[*prefix, {"role": "user", "content": "third"}])

    generate_calls = [body for path, body in calls if path.endswith(":generateContent")]
    assert len(generate_calls) == 3
    assert generate_calls[0]["cachedContent"] == "cachedContents/cache_1"
    assert generate_calls[1]["cachedContent"] == "cachedContents/cache_1"
    assert "cachedContent" not in generate_calls[2]
    assert "systemInstruction" in generate_calls[2]
    assert len(generate_calls[2]["contents"]) == 3
    assert first.provider_metadata is not None
    assert first.provider_metadata["gemini_generate_content"]["cache_policy"]["status"] == (
        "created"
    )
    assert second.provider_metadata is not None
    metadata = second.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert metadata["status"] == "stale_retry"
    assert metadata["used"] is False
    assert metadata["fallback"] == "full_payload"
    assert metadata["delete_attempt_count"] == 1
    assert metadata["delete_success_count"] == 1
    assert metadata["delete_status"] == "already_absent"
    assert client._cached_content_by_signature == {}
    delete_calls = [path for path, _body in calls if path.endswith("/cachedContents/cache_1")]
    assert len(delete_calls) == 1
    summaries = provider_call_history_snapshot(limit=2)
    assert [item["cache_policy"]["status"] for item in summaries] == [
        "created",
        "stale_retry",
    ]
    assert summaries[-1]["cache_policy"]["used"] is False
    assert summaries[-1]["cache_policy"]["fallback"] == "full_payload"
    assert summaries[-1]["token_reconciliation"]["input_mode"] == (
        "full_retry_after_cached_content_rejected"
    )
    assert (
        summaries[-1]["token_reconciliation"]["input_estimate_tokens"]
        == (summaries[-1]["token_reconciliation"]["sent_input_estimate_tokens"])
    )
    assert last_provider_call_summary() == summaries[-1]


@pytest.mark.parametrize("stream", [False, True])
def test_summary_rejection_then_stale_cache_fallback_keeps_summary_field_removed(
    stream: bool,
) -> None:
    generate_bodies: list[dict[str, object]] = []

    def success_response() -> httpx.Response:
        payload = {
            "responseId": "resp_combined_fallback",
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "Done."}]},
                    "finishReason": "STOP",
                }
            ],
        }
        return _sse_response(_sse_event(payload)) if stream else httpx.Response(200, json=payload)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})
        body = json.loads(request.content.decode())
        if request.url.path.endswith("/cachedContents"):
            return httpx.Response(200, json={"name": "cachedContents/cache_1"})
        generate_bodies.append(body)
        if len(generate_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"role": "model", "parts": [{"text": "Primed."}]}}]
                },
            )
        if len(generate_bodies) == 2:
            return httpx.Response(
                400,
                json={"error": {"message": "includeThoughts is not supported"}},
            )
        if len(generate_bodies) == 3:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "status": "NOT_FOUND",
                        "message": "Cached content cachedContents/cache_1 not found",
                    }
                },
            )
        return success_response()

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "system", "content": "Stable system prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
    ]
    client.chat(messages=messages)

    response = client.chat(
        messages=messages,
        stream=stream,
        on_reasoning_delta=lambda _delta: None,
    )

    assert len(generate_bodies) == 4
    assert generate_bodies[1]["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
    assert generate_bodies[2].get("cachedContent") == "cachedContents/cache_1"
    assert "thinkingConfig" not in generate_bodies[2]["generationConfig"]
    assert "cachedContent" not in generate_bodies[3]
    assert "thinkingConfig" not in generate_bodies[3]["generationConfig"]
    assert response.content == "Done."


def test_chat_refreshes_explicit_cached_content_before_ttl_expiry() -> None:
    reset_provider_telemetry_for_tests()
    current_time = 100.0
    cache_index = 0
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cache_index
        if request.method == "DELETE":
            calls.append((request.url.path, {}))
            return httpx.Response(200, json={})
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            cache_index += 1
            return httpx.Response(200, json={"name": f"cachedContents/cache_{cache_index}"})
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{len(calls)}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 105,
                    "cachedContentTokenCount": 80,
                },
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_ttl="2s",
        cached_content_min_tokens=0,
        cached_content_time_fn=lambda: current_time,
        transport=httpx.MockTransport(handler),
    )
    prefix = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]

    first = client.chat(messages=[*prefix, {"role": "user", "content": "second"}])
    current_time = 100.4
    second = client.chat(messages=[*prefix, {"role": "user", "content": "third"}])
    current_time = 101.2
    third = client.chat(messages=[*prefix, {"role": "user", "content": "fourth"}])

    generate_calls = [body for path, body in calls if path.endswith(":generateContent")]
    cache_calls = [body for path, body in calls if path.endswith("/cachedContents")]
    delete_calls = [path for path, _body in calls if path.endswith("/cachedContents/cache_1")]
    assert len(cache_calls) == 2
    assert len(delete_calls) == 1
    assert generate_calls[0]["cachedContent"] == "cachedContents/cache_1"
    assert generate_calls[1]["cachedContent"] == "cachedContents/cache_1"
    assert generate_calls[2]["cachedContent"] == "cachedContents/cache_2"
    assert first.provider_metadata is not None
    assert first.provider_metadata["gemini_generate_content"]["cache_policy"]["status"] == "created"
    assert second.provider_metadata is not None
    assert second.provider_metadata["gemini_generate_content"]["cache_policy"]["status"] == "reused"
    assert third.provider_metadata is not None
    metadata = third.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert metadata["status"] == "created"
    assert metadata["refresh_reason"] == "ttl_refresh_due"
    assert metadata["evicted_entry_count"] == 1
    assert metadata["delete_success_count"] == 1
    summaries = provider_call_history_snapshot(limit=3)
    assert [item["cache_policy"]["status"] for item in summaries] == [
        "created",
        "reused",
        "created",
    ]
    assert summaries[-1]["cache_policy"]["refresh_reason"] == "ttl_refresh_due"


def test_chat_deletes_lru_explicit_cached_content_when_entry_limit_is_reached() -> None:
    reset_provider_telemetry_for_tests()
    cache_index = 0
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cache_index
        if request.method == "DELETE":
            calls.append((request.url.path, {}))
            return httpx.Response(200, json={})
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            cache_index += 1
            return httpx.Response(200, json={"name": f"cachedContents/cache_{cache_index}"})
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{len(calls)}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 105,
                    "cachedContentTokenCount": 80,
                },
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_ttl="60s",
        cached_content_min_tokens=0,
        cached_content_max_entries=1,
        transport=httpx.MockTransport(handler),
    )
    first_prefix = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]
    second_prefix = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "different first"},
        {"role": "assistant", "content": "different first answer"},
    ]

    client.chat(messages=[*first_prefix, {"role": "user", "content": "second"}])
    second = client.chat(messages=[*second_prefix, {"role": "user", "content": "second"}])

    cache_calls = [body for path, body in calls if path.endswith("/cachedContents")]
    delete_calls = [path for path, _body in calls if path.endswith("/cachedContents/cache_1")]
    assert len(cache_calls) == 2
    assert len(delete_calls) == 1
    assert second.provider_metadata is not None
    metadata = second.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert metadata["status"] == "created"
    assert metadata["eviction_reasons"] == ["max_entries_exceeded"]
    assert metadata["evicted_entry_count"] == 1
    assert metadata["entry_count"] == 1
    assert metadata["max_entries"] == 1
    assert metadata["delete_status"] == "deleted"


def test_chat_falls_back_to_full_payload_when_explicit_cached_content_is_rejected() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            return httpx.Response(
                400,
                json={"error": {"status": "INVALID_ARGUMENT", "message": "cache rejected"}},
            )
        return httpx.Response(
            200,
            json={
                "responseId": "resp_fallback",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
    ]

    response = client.chat(messages=messages)

    generate_body = calls[-1][1]
    assert "cachedContent" not in generate_body
    assert "systemInstruction" in generate_body
    assert len(generate_body["contents"]) == 3
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert metadata["status"] == "create_rejected"
    assert metadata["used"] is False
    assert metadata["fallback"] == "full_payload"
    assert "400" in metadata["create_error"]
    assert "cache rejected" in metadata["create_error"]

    second = client.chat(messages=messages)

    create_calls = [path for path, _body in calls if path.endswith("/cachedContents")]
    assert len(create_calls) == 1
    assert second.provider_metadata is not None
    second_metadata = second.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert second_metadata["status"] == "create_disabled"
    assert second_metadata["used"] is False
    assert second_metadata["fallback"] == "full_payload"
    assert "create_rejected" in second_metadata["create_disabled_reason"]

    client.apply_cache_settings(ttl="120s")
    third = client.chat(messages=messages)

    create_calls = [path for path, _body in calls if path.endswith("/cachedContents")]
    assert len(create_calls) == 2
    assert third.provider_metadata is not None
    third_metadata = third.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert third_metadata["status"] == "create_rejected"


def test_apply_cache_settings_recomputes_ttl_and_evicts_entries_server_side() -> None:
    cache_index = 0
    calls: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cache_index
        if request.method == "DELETE":
            calls.append((request.method, request.url.path, {}))
            return httpx.Response(200, json={})
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            cache_index += 1
            return httpx.Response(200, json={"name": f"cachedContents/cache_{cache_index}"})
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{len(calls)}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_ttl="3600s",
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
    ]

    client.chat(messages=messages)
    assert client._cached_content_by_signature != {}

    client.apply_cache_settings(ttl="120s")

    assert client.cached_content_ttl == "120s"
    assert client.cached_content_ttl_seconds == 120.0
    assert client.cached_content_refresh_margin_seconds == 12.0
    assert client._cached_content_by_signature == {}
    delete_calls = [path for method, path, _body in calls if method == "DELETE"]
    assert delete_calls == ["/v1beta/cachedContents/cache_1"]

    client.apply_cache_settings(ttl="120s")
    delete_calls = [path for method, path, _body in calls if method == "DELETE"]
    assert delete_calls == ["/v1beta/cachedContents/cache_1"]

    client.chat(messages=messages)

    create_bodies = [
        body
        for method, path, body in calls
        if method == "POST" and path.endswith("/cachedContents")
    ]
    assert len(create_bodies) == 2
    assert create_bodies[0]["ttl"] == "3600s"
    assert create_bodies[1]["ttl"] == "120s"


def test_chat_transient_create_failure_disables_only_after_three_consecutive() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/cachedContents"):
            return httpx.Response(
                500,
                json={"error": {"status": "INTERNAL", "message": "transient blip"}},
            )
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{len(calls)}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
    ]

    counts: list[int] = []
    for expected_disabled in (False, False, True):
        response = client.chat(messages=messages)
        assert response.provider_metadata is not None
        metadata = response.provider_metadata["gemini_generate_content"]["cache_policy"]
        assert metadata["status"] == "create_rejected"
        assert "500" in metadata["create_error"]
        counts.append(metadata["create_transient_failure_count"])
        assert (client._cached_content_create_disabled_reason is not None) == expected_disabled

    assert counts == [1, 2, 3]
    create_calls = [path for path in calls if path.endswith("/cachedContents")]
    assert len(create_calls) == 3
    assert "3 consecutive transient failures" in client._cached_content_create_disabled_reason

    fourth = client.chat(messages=messages)

    create_calls = [path for path in calls if path.endswith("/cachedContents")]
    assert len(create_calls) == 3
    assert fourth.provider_metadata is not None
    fourth_metadata = fourth.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert fourth_metadata["status"] == "create_disabled"
    assert "create_rejected" in fourth_metadata["create_disabled_reason"]


def test_chat_successful_create_resets_transient_create_failure_counter() -> None:
    create_attempts = 0
    # 500 twice, then a success, then three more 500s: without the reset the
    # fifth create attempt would already trip the consecutive-failure limit.
    create_statuses = [500, 500, 200, 500, 500, 500]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_attempts
        if request.url.path.endswith("/cachedContents"):
            create_attempts += 1
            status = create_statuses[create_attempts - 1]
            if status == 200:
                return httpx.Response(
                    200,
                    json={"name": f"cachedContents/cache_{create_attempts}"},
                )
            return httpx.Response(
                status,
                json={"error": {"status": "INTERNAL", "message": "transient blip"}},
            )
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{create_attempts}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )

    def _messages(prefix: str) -> list[dict[str, str]]:
        # Distinct prefixes force a fresh cachedContents create per chat call.
        return [
            {"role": "system", "content": f"System prompt {prefix}."},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second"},
        ]

    for prefix in ("a", "b", "c", "d", "e"):
        client.chat(messages=_messages(prefix))
        assert client._cached_content_create_disabled_reason is None, prefix

    client.chat(messages=_messages("f"))

    assert create_attempts == 6
    assert client._cached_content_create_disabled_reason is not None

    seventh = client.chat(messages=_messages("g"))

    assert create_attempts == 6
    assert seventh.provider_metadata is not None
    seventh_metadata = seventh.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert seventh_metadata["status"] == "create_disabled"


def test_apply_cache_settings_eviction_stops_after_first_delete_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache_index = 0
    calls: list[tuple[str, str]] = []
    delete_timeouts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cache_index
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            delete_timeouts.append(request.extensions.get("timeout"))
            return httpx.Response(500, json={"error": {"status": "INTERNAL"}})
        if request.url.path.endswith("/cachedContents"):
            cache_index += 1
            return httpx.Response(200, json={"name": f"cachedContents/cache_{cache_index}"})
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{len(calls)}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        timeout_s=20.0,
        explicit_cached_content_enabled=True,
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )
    for prefix in ("a", "b", "c"):
        client.chat(
            messages=[
                {"role": "system", "content": f"System prompt {prefix}."},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second"},
            ]
        )
    assert len(client._cached_content_by_signature) == 3

    with caplog.at_level(
        logging.DEBUG,
        logger="alysis_code.llm.gemini_generate_content",
    ):
        client.apply_cache_settings(ttl="120s")

    delete_calls = [path for method, path in calls if method == "DELETE"]
    # The first failed delete stops the sweep; the rest expire via TTL.
    assert delete_calls == ["/v1beta/cachedContents/cache_1"]
    assert client._cached_content_by_signature == {}
    assert delete_timeouts and delete_timeouts[0]["read"] == 5.0
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage() == "gemini_cached_content_evict_delete_failed"
    ]
    assert len(warnings) == 1
    assert warnings[0].delete_status == "delete_rejected"
    assert warnings[0].remaining_entry_count == 2


def test_chat_reports_cache_creation_tokens_once_across_generate_retry() -> None:
    reset_provider_telemetry_for_tests()
    calls: list[tuple[str, dict[str, object]]] = []
    generate_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generate_attempts
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            return httpx.Response(
                200,
                json={
                    "name": "cachedContents/cache_1",
                    "usageMetadata": {"totalTokenCount": 42},
                },
            )
        generate_attempts += 1
        if generate_attempts == 1:
            # The attempt that created the cache entry fails afterwards; the
            # creation spend is already billed and must survive the retry.
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
        return httpx.Response(
            200,
            json={
                "responseId": f"resp_{generate_attempts}",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 105,
                    "cachedContentTokenCount": 80,
                },
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_ttl="60s",
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=2,
            base_delay_seconds=0.001,
            max_delay_seconds=0.001,
        ),
        provider_sleep_fn=lambda _seconds: None,
    )
    prefix = [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]

    first = client.chat(messages=[*prefix, {"role": "user", "content": "second"}])
    second = client.chat(messages=[*prefix, {"role": "user", "content": "third"}])

    create_calls = [body for path, body in calls if path.endswith("/cachedContents")]
    generate_calls = [body for path, body in calls if path.endswith(":generateContent")]
    assert len(create_calls) == 1
    assert len(generate_calls) == 3
    assert all(body["cachedContent"] == "cachedContents/cache_1" for body in generate_calls)
    assert first.provider_metadata is not None
    first_metadata = first.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert first_metadata["status"] == "reused"
    assert first.usage is not None
    # Billed on the failed creating attempt, reported by the first success.
    assert first.usage.cache_creation_input_tokens == 42
    assert second.usage is not None
    assert second.usage.cache_creation_input_tokens is None


def test_chat_skips_explicit_cached_content_for_tool_requests() -> None:
    reset_provider_telemetry_for_tests()
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, body))
        if request.url.path.endswith("/cachedContents"):
            return httpx.Response(
                500,
                json={"error": {"status": "INTERNAL", "message": "should not create cache"}},
            )
        return httpx.Response(
            200,
            json={
                "responseId": "resp_tools_no_cache",
                "modelVersion": "gemini-3-flash-preview",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}],
            },
        )

    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="test-key",
        model="gemini-3-flash-preview",
        explicit_cached_content_enabled=True,
        cached_content_min_tokens=0,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second"},
        ],
        tools=[_fs_read_tool()],
    )

    assert [path for path, _body in calls] == [
        "/v1beta/models/gemini-3-flash-preview:generateContent"
    ]
    generate_body = calls[0][1]
    assert "cachedContent" not in generate_body
    assert "systemInstruction" in generate_body
    assert "tools" in generate_body
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["gemini_generate_content"]["cache_policy"]
    assert metadata["status"] == "disabled_for_request"
    assert metadata["used"] is False
    assert metadata["fallback"] == "full_payload"
    assert metadata["disabled_fields"] == ["cached_content"]
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_shape"]["cached_content_attached"] is False
    assert summary["request_shape"]["cache_fields_emitted"] is False


def test_chat_rejects_empty_malformed_and_unsupported_options() -> None:
    with pytest.raises(LLMError, match="does not support prompt_cache_key"):
        GeminiGenerateContentClient(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="test-key",
            model="gemini-3-flash-preview",
            prompt_cache_key="cache-key",
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        ).chat(messages=[{"role": "user", "content": "hello"}])

    with pytest.raises(LLMError, match="does not support response_format type"):
        _client(httpx.MockTransport(lambda _request: httpx.Response(500))).chat(
            messages=[{"role": "user", "content": "hello"}],
            response_format={"type": "xml_object"},
        )

    # Gemini 3 uses its reasoning contract for compatibility values. An unknown
    # legacy effort falls back to the contract default/omission and reaches the
    # provider instead of being rejected by the client.
    with pytest.raises(LLMError, match="LLM error 500"):
        GeminiGenerateContentClient(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="test-key",
            model="gemini-3-flash-preview",
            reasoning_effort="extreme",
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            # Keep provider retry timing out of this validation-path test.
            provider_sleep_fn=lambda _seconds: None,
        ).chat(messages=[{"role": "user", "content": "hello"}])

    def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": []}}]},
        )

    with pytest.raises(LLMError, match="returned no assistant text or tool calls"):
        _client(httpx.MockTransport(empty_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )

    reasoning_deltas: list[str] = []

    def thought_only_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "Internal summary only.", "thought": True}],
                        }
                    }
                ]
            },
        )

    with pytest.raises(LLMError, match="returned no assistant text or tool calls"):
        _client(httpx.MockTransport(thought_only_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
            on_reasoning_delta=reasoning_deltas.append,
        )
    assert reasoning_deltas == []

    def malformed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"responseId": "resp_bad"})

    with pytest.raises(LLMError, match="missing candidates"):
        _client(httpx.MockTransport(malformed_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )


def test_gemini_chat_and_count_strip_state_from_different_credential_route() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        sent.append(body)
        if str(request.url).endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 12})
        return httpx.Response(
            200,
            json={
                "responseId": "resp_new",
                "modelVersion": "route-model",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
            },
        )

    producer = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="credential-a",
        model="route-model",
    )
    consumer = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="credential-b",
        model="route-model",
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "public answer",
            PROVIDER_METADATA_KEY: stamp_provider_metadata_for_route(
                {
                    "gemini_generate_content": {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "text": "private-thinking-state",
                                    "thought": True,
                                    "thoughtSignature": "private-signature",
                                },
                                {"text": "public answer"},
                            ],
                        }
                    }
                },
                producer.route_identity,
            ),
        },
        {"role": "user", "content": "follow up"},
    ]

    assert consumer.count_input_tokens(messages=messages) is not None
    assert consumer.chat(messages=messages).content == "ok"

    assert len(sent) == 2
    assert all("private-" not in json.dumps(body) for body in sent)
    assert all("public answer" in json.dumps(body) for body in sent)


def test_gemini_extra_headers_override_defaults_case_insensitively() -> None:
    client = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="fallback-key",
        model="gemini-3-flash-preview",
        extra_headers={
            "X-Goog-Api-Key": "override-key",
            "Content-Type": "application/custom+json",
        },
    )

    headers = client._headers()

    assert headers["x-goog-api-key"] == "override-key"
    assert headers["content-type"] == "application/custom+json"
    assert len({name.casefold() for name in headers}) == len(headers)


def test_internal_tool_usage_does_not_claim_a_single_context_size():
    from alysis_code.llm.gemini_generate_content import _parse_usage

    usage = _parse_usage(
        {
            "promptTokenCount": 100,
            "toolUsePromptTokenCount": 200,
            "candidatesTokenCount": 20,
            "totalTokenCount": 320,
            "cachedContentTokenCount": 80,
        }
    )
    assert usage.prompt_tokens == 300
    assert usage.input_tokens_scope == "interaction"
    assert usage.input_tokens_uncached == 220
    assert usage.input_tokens_uncached_derived is True
