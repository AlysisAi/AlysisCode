from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.anthropic_messages import (
    AnthropicMessagesClient,
    _anthropic_messages_from_messages,
)
from alysis_code.llm.base import require_omitted_reasoning_display
from alysis_code.llm.metadata import (
    PROVIDER_METADATA_KEY,
    assistant_message_from_response,
    attach_provider_metadata_to_assistant_message,
    stamp_provider_metadata_for_route,
    strip_provider_metadata_from_message,
)
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.request_plan import LLMRequestPlan, RequestCachePlan
from alysis_code.llm.types import LLMError
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)


def _client(transport: httpx.BaseTransport) -> AnthropicMessagesClient:
    return AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        transport=transport,
    )


@pytest.mark.parametrize("role", ["system", "developer"])
def test_controller_continuation_is_not_sent_as_assistant_prefill(role):
    messages = [
        {"role": "user", "content": "Fix the issue."},
        {"role": "assistant", "content": "The change is ready."},
        {"role": role, "content": "Verify the change before finalizing."},
    ]
    system, wire = _anthropic_messages_from_messages(messages)
    assert system == "Verify the change before finalizing."
    assert wire[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "Continue following the instructions above."}],
    }
    assert wire[-2]["content"] == [{"type": "text", "text": "The change is ready."}]
    assert len(messages) == 3  # Wire normalization must not change durable history.


def test_plain_assistant_prefill_is_not_rewritten_as_controller_continuation():
    _, wire = _anthropic_messages_from_messages(
        [
            {"role": "system", "content": "Reply in JSON."},
            {"role": "user", "content": "List two colors."},
            {"role": "assistant", "content": '{"colors": ['},
        ]
    )
    assert wire[-1]["role"] == "assistant"


def test_controller_nudge_does_not_insert_user_before_pending_tool_result():
    _, wire = _anthropic_messages_from_messages(
        [
            {"role": "user", "content": "Read a file."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fs_read", "arguments": '{"path":"a.py"}'},
                    }
                ],
            },
            {"role": "system", "content": "Wait for the tool result."},
        ]
    )
    assert wire[-1]["role"] == "assistant"


def _web_search_function_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Standalone Alysis Code web search.",
            "parameters": {"type": "object"},
        },
    }


def _sse_event(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _sse_response(*events: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream("".join(events).encode("utf-8")),
    )


class _TruncatedAnthropicSummaryStream(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        yield (
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_partial",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [],
                    },
                },
            )
            + _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
            + _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "Safe partial summary.",
                    },
                },
            )
        ).encode("utf-8")
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
            stream=_TruncatedAnthropicSummaryStream(),
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        enable_thinking=True,
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


def test_count_input_tokens_uses_anthropic_count_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.anthropic.com/v1/messages/count_tokens"
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"input_tokens": 456})

    result = _client(httpx.MockTransport(handler)).count_input_tokens(
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello"},
        ]
    )

    assert result is not None
    assert result.input_tokens == 456
    assert result.source.value == "provider_count"
    assert result.confidence.value == "authoritative"
    assert captured["model"] == "claude-sonnet-4-6"
    assert captured["system"] == "Be concise."
    assert captured["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
    ]


def test_count_input_tokens_applies_enabled_cache_control_plan() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"input_tokens": 456})

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        transport=httpx.MockTransport(handler),
        prompt_cache_control_enabled=True,
        prompt_cache_control_ttl="1h",
    )
    result = client.count_input_tokens(
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello"},
        ]
    )

    assert result is not None
    assert captured["system"] == [
        {
            "type": "text",
            "text": "Be concise.",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]
    assert captured["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_count_input_tokens_mirrors_enabled_thinking_mode() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"input_tokens": 456})

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    )
    result = client.count_input_tokens(
        messages=[{"role": "user", "content": "Hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice="required",
    )

    assert result is not None
    assert captured["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert "tool_choice" not in captured


def test_chat_maps_messages_tools_tool_choice_and_usage() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_text",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 3,
                },
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        temperature=0.4,
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
                        "id": "toolu_read",
                        "type": "function",
                        "function": {
                            "name": "fs_read",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_read", "content": "README contents"},
        ],
        tools=[
            {
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
        ],
        tool_choice={"type": "function", "function": {"name": "fs_read"}},
        max_tokens=123,
    )

    assert captured["model"] == "claude-sonnet-4-6"
    assert captured["temperature"] == 0.4
    assert captured["max_tokens"] == 123
    assert captured["system"] == "System prompt.\n\nDeveloper prompt."
    assert captured["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Read it."}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will read."},
                {
                    "type": "tool_use",
                    "id": "toolu_read",
                    "name": "fs_read",
                    "input": {"path": "README.md"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_read",
                    "content": "README contents",
                }
            ],
        },
    ]
    assert captured["tools"] == [
        {
            "name": "fs_read",
            "description": "Read a file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    assert captured["tool_choice"] == {"type": "tool", "name": "fs_read"}
    assert response.content == "Done."
    assert response.usage is not None
    assert response.usage.prompt_tokens == 15
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 19
    assert response.usage.cached_prompt_tokens == 3
    assert response.usage.cache_read_input_tokens == 3
    assert response.usage.input_tokens_uncached == 12


def test_chat_omits_temperature_for_anthropic_models_that_deprecated_sampling() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_opus",
                "model": "claude-opus-4-8",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-opus-4-8",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "Done."
    assert "temperature" not in captured
    request_plan = response.provider_metadata["anthropic_messages"]["request_plan"]  # type: ignore[index]
    assert request_plan["temperature_omitted"] is True
    assert request_plan["temperature_omit_reason"] == ("anthropic_sampling_parameters_deprecated")


def test_chat_retries_without_temperature_when_anthropic_rejects_unknown_model() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if "temperature" in body:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "temperature is not supported for this model",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_future",
                "model": "claude-future-model",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-future-model",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    assert client.chat(messages=[{"role": "user", "content": "hello"}]).content == "Done."
    assert len(requests) == 2
    assert requests[0]["temperature"] == 0.2
    assert "temperature" not in requests[1]

    assert client.chat(messages=[{"role": "user", "content": "again"}]).content == "Done."
    assert len(requests) == 3
    assert "temperature" not in requests[2]


def test_chat_parses_anthropic_cache_write_usage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_write",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 12,
                        "ephemeral_1h_input_tokens": 28,
                    },
                },
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.usage is not None
    assert response.usage.prompt_tokens == 80
    assert response.usage.completion_tokens == 2
    assert response.usage.total_tokens == 82
    assert response.usage.cached_prompt_tokens == 30
    assert response.usage.cache_read_input_tokens == 30
    assert response.usage.cache_creation_input_tokens == 40
    assert response.usage.cache_creation_5m_input_tokens == 12
    assert response.usage.cache_creation_1h_input_tokens == 28
    assert response.usage.input_tokens_uncached == 10


def test_chat_omits_anthropic_cache_control_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "cache_control" not in body
        return httpx.Response(
            200,
            json={
                "id": "msg_no_cache",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.content == "Done."


def test_chat_sends_anthropic_cache_control_when_enabled_default_ttl() -> None:
    reset_provider_telemetry_for_tests()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_default",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(messages=[{"role": "user", "content": "hello"}])

    assert captured["cache_control"] == {"type": "ephemeral"}
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["cache_policy"] == {
        "status": "enabled",
        "strategy": "anthropic_cache_control",
        "enabled": True,
        "used": True,
        "top_level_cache_control_used": True,
        "explicit_block_used": False,
        "explicit_block_count": 0,
        "ttl": "5m",
        "mode": "automatic",
    }
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"] == metadata["cache_policy"]
    assert summary["request_shape"]["cache_control_block_count"] == 0
    assert summary["request_shape"]["explicit_cache_control_block_count"] == 0
    assert summary["request_shape"]["top_level_cache_control_present"] is True
    assert summary["request_shape"]["cache_used"] is True
    assert summary["token_reconciliation"]["input_estimate_tokens"] > 0
    assert summary["token_reconciliation"]["sent_input_estimate_tokens"] > 0
    assert summary["token_reconciliation"]["reported_prompt_tokens"] is None


def test_chat_sends_anthropic_cache_control_one_hour_ttl_when_enabled() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_1h",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        prompt_cache_control_ttl="1h",
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hello"}])

    assert captured["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_chat_sends_anthropic_cache_control_from_request_plan() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_plan",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    plan = LLMRequestPlan.from_chat_args(
        messages=[{"role": "user", "content": "hello"}],
        cache=RequestCachePlan(
            strategy="anthropic_cache_control",
            mode="automatic",
            anthropic_cache_control_enabled=True,
            anthropic_cache_control_ttl="1h",
        ),
    )

    response = client.chat(messages=[], request_plan=plan)

    assert captured["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["cache_policy"] == {
        "status": "enabled",
        "strategy": "anthropic_cache_control",
        "enabled": True,
        "used": True,
        "top_level_cache_control_used": True,
        "explicit_block_used": False,
        "explicit_block_count": 0,
        "ttl": "1h",
        "mode": "automatic",
    }


def test_chat_adds_native_system_cache_control_block_when_enabled() -> None:
    reset_provider_telemetry_for_tests()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_system",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        prompt_cache_control_ttl="1h",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "system", "content": "Stable system prompt."},
            {"role": "user", "content": "hello"},
        ]
    )

    system = captured["system"]
    assert isinstance(system, list)
    assert system == [
        {
            "type": "text",
            "text": "Stable system prompt.",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]
    assert captured["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["cache_policy"]["explicit_block_used"] is True
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"]["explicit_block_used"] is True
    assert summary["request_shape"]["cache_control_block_count"] == 1
    assert summary["request_shape"]["explicit_cache_control_block_count"] == 1
    assert summary["request_shape"]["top_level_cache_control_present"] is True


def test_chat_preserves_existing_text_block_cache_control() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_existing",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "cached user prefix",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    )

    sent_content = captured["messages"][0]["content"]  # type: ignore[index]
    assert sent_content == [
        {
            "type": "text",
            "text": "cached user prefix",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert response.content == "Done."


def test_chat_preserves_system_cache_control_and_skips_conflicting_top_level_ttl() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_ttl_conflict",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        prompt_cache_control_ttl="5m",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "Stable system prompt.",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
            },
            {"role": "user", "content": "hello"},
        ]
    )

    assert "cache_control" not in captured
    assert captured["system"] == [
        {
            "type": "text",
            "text": "Stable system prompt.",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]
    metadata = response.provider_metadata["anthropic_messages"]["cache_policy"]  # type: ignore[index]
    assert metadata["used"] is True
    assert metadata["top_level_cache_control_used"] is False
    assert metadata["explicit_block_count"] == 1
    assert "anthropic_top_level_cache_control_skipped_ttl_conflict" in metadata["warnings"]


def test_chat_skips_top_level_cache_control_at_breakpoint_limit() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_breakpoint_limit",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        transport=httpx.MockTransport(handler),
    )
    blocks = [
        {
            "type": "text",
            "text": f"cached block {index}",
            "cache_control": {"type": "ephemeral"},
        }
        for index in range(4)
    ]

    response = client.chat(messages=[{"role": "user", "content": blocks}])

    assert "cache_control" not in captured
    metadata = response.provider_metadata["anthropic_messages"]["cache_policy"]  # type: ignore[index]
    assert metadata["used"] is True
    assert metadata["top_level_cache_control_used"] is False
    assert metadata["explicit_block_count"] == 4
    assert "anthropic_top_level_cache_control_skipped_breakpoint_limit" in metadata["warnings"]


def test_chat_retries_without_cache_control_when_anthropic_rejects_it() -> None:
    reset_provider_telemetry_for_tests()
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={"error": {"type": "invalid_request_error", "message": "bad cache_control"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_fallback",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "system", "content": "Stable system prompt."},
            {"role": "user", "content": "hello"},
        ]
    )

    assert len(calls) == 2
    assert "cache_control" in calls[0]
    assert "cache_control" not in calls[1]
    assert all("cache_control" not in json.dumps(value, sort_keys=True) for value in [calls[1]])
    metadata = response.provider_metadata["anthropic_messages"]["cache_policy"]  # type: ignore[index]
    assert metadata["status"] == "fallback"
    assert metadata["used"] is False
    assert metadata["fallback"] == "anthropic_cache_control_rejected"
    assert metadata["disabled_fields"] == ["cache_control"]
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_shape"]["cache_used"] is False
    assert summary["request_shape"]["top_level_cache_control_present"] is False
    assert summary["request_shape"]["explicit_cache_control_block_count"] == 0


def _cache_config_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "cache_config",
            "description": "Configure caching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cache_control": {"type": "string", "description": "Cache mode."},
                },
                "required": ["cache_control"],
            },
        },
    }


def test_chat_ignores_tool_schema_property_named_cache_control() -> None:
    reset_provider_telemetry_for_tests()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_schema_property",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        prompt_cache_control_ttl="1h",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "system", "content": "Stable system prompt."},
            {"role": "user", "content": "hello"},
        ],
        tools=[_cache_config_tool()],
    )

    assert captured["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert captured["system"] == [
        {
            "type": "text",
            "text": "Stable system prompt.",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]
    assert captured["tools"][0]["input_schema"]["properties"]["cache_control"] == {  # type: ignore[index]
        "type": "string",
        "description": "Cache mode.",
    }
    metadata = response.provider_metadata["anthropic_messages"]["cache_policy"]  # type: ignore[index]
    assert metadata["used"] is True
    assert metadata["top_level_cache_control_used"] is True
    assert metadata["explicit_block_count"] == 1
    assert "warnings" not in metadata
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_shape"]["explicit_cache_control_block_count"] == 1


def test_cache_control_downgrade_keeps_tool_schema_property_named_cache_control() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={"error": {"type": "invalid_request_error", "message": "bad cache_control"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_cache_schema_property_fallback",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        prompt_cache_control_enabled=True,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "system", "content": "Stable system prompt."},
            {"role": "user", "content": "hello"},
        ],
        tools=[_cache_config_tool()],
    )

    assert len(calls) == 2
    assert "cache_control" in calls[0]
    assert "cache_control" not in calls[1]
    assert calls[1]["system"] == [{"type": "text", "text": "Stable system prompt."}]
    assert calls[1]["tools"][0]["input_schema"] == {  # type: ignore[index]
        "type": "object",
        "properties": {
            "cache_control": {"type": "string", "description": "Cache mode."},
        },
        "required": ["cache_control"],
    }
    metadata = response.provider_metadata["anthropic_messages"]["cache_policy"]  # type: ignore[index]
    assert metadata["status"] == "fallback"
    assert metadata["used"] is False
    assert metadata["fallback"] == "anthropic_cache_control_rejected"


def test_chat_parses_multiple_tool_use_blocks() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_tools",
                "model": "claude-sonnet-4-6",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "fs_read",
                        "input": {"path": "README.md"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "shell_run",
                        "input": {"cmd": "pytest -q"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 7, "output_tokens": 5},
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "Inspect."}],
        tools=[
            {"type": "function", "function": {"name": "fs_read", "parameters": {"type": "object"}}},
            {
                "type": "function",
                "function": {"name": "shell_run", "parameters": {"type": "object"}},
            },
        ],
    )

    assert response.content == ""
    assert [(tc.id, tc.name, tc.arguments) for tc in response.tool_calls] == [
        ("toolu_1", "fs_read", {"path": "README.md"}),
        ("toolu_2", "shell_run", {"cmd": "pytest -q"}),
    ]
    assert response.provider_metadata is not None
    assert response.provider_metadata["anthropic_messages"]["stop_reason"] == "tool_use"


def test_chat_function_tool_use_and_tool_result_round_trip_exact_id() -> None:
    calls: list[dict[str, object]] = []
    tool_use_block = {
        "type": "tool_use",
        "id": "toolu_exact_123",
        "name": "fs_read",
        "input": {"path": "README.md"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg_tool_roundtrip",
                    "model": "claude-sonnet-4-6",
                    "content": [tool_use_block],
                    "stop_reason": "tool_use",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_done",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Read it."}],
                "stop_reason": "end_turn",
            },
        )

    client = _client(httpx.MockTransport(handler))
    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[
            {"type": "function", "function": {"name": "fs_read", "parameters": {"type": "object"}}}
        ],
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
            {"role": "tool", "tool_call_id": "toolu_exact_123", "content": "file contents"},
        ],
    )

    assert calls[1]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "read"}]},
        {"role": "assistant", "content": [tool_use_block]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_exact_123",
                    "content": "file contents",
                }
            ],
        },
    ]


def test_chat_text_only_response_preserves_provider_metadata_for_next_turn() -> None:
    calls: list[dict[str, object]] = []
    content_blocks = [{"type": "text", "text": "First answer."}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg_text_metadata",
                    "model": "claude-sonnet-4-6",
                    "content": content_blocks,
                    "stop_reason": "end_turn",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_second",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Second answer."}],
                "stop_reason": "end_turn",
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

    client.chat(
        messages=[
            {"role": "user", "content": "first"},
            assistant_message,
            {"role": "user", "content": "second"},
        ],
    )

    assert calls[1]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": content_blocks},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    ]


def test_chat_native_mode_uses_only_anthropic_hosted_web_search() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "id": "msg_search",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        web_search_mode="native",
        web_search_adapter="anthropic_messages",
        transport=httpx.MockTransport(handler),
    )
    response = client.chat(
        messages=[{"role": "user", "content": "find docs"}],
        tools=[_web_search_function_tool()],
    )

    assert captured["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    ]
    assert response.content == "ok"


def test_chat_auto_mode_prefers_anthropic_hosted_web_search_for_auto_adapter() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={"id": "msg_auto", "content": [{"type": "text", "text": "ok"}]},
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        web_search_mode="auto",
        web_search_adapter="auto",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}], tools=[_web_search_function_tool()]
    )

    assert captured["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    ]


def test_chat_external_mode_uses_only_alysis_web_search_function() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={"id": "msg_external", "content": [{"type": "text", "text": "ok"}]},
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        web_search_mode="external",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}], tools=[_web_search_function_tool()]
    )

    assert captured["tools"] == [
        {
            "name": "web_search",
            "description": "Standalone Alysis Code web search.",
            "input_schema": {"type": "object"},
        }
    ]


def test_chat_off_mode_removes_all_web_search_tools() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200, json={"id": "msg_off", "content": [{"type": "text", "text": "ok"}]}
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        web_search_mode="off",
        web_search_adapter="anthropic_messages",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}],
        tools=[_web_search_function_tool(), {"type": "web_search_20260209", "name": "web_search"}],
    )

    assert "tools" not in captured


def test_chat_auto_mode_with_external_adapter_uses_alysis_web_search_function() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.update(body)
        return httpx.Response(
            200,
            json={"id": "msg_auto_external", "content": [{"type": "text", "text": "ok"}]},
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        web_search_mode="auto",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "find docs"}], tools=[_web_search_function_tool()]
    )

    assert captured["tools"][0]["name"] == "web_search"
    assert "type" not in captured["tools"][0]


def test_chat_native_mode_rejects_non_anthropic_search_adapter() -> None:
    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        web_search_mode="native",
        web_search_adapter="tavily",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )

    with pytest.raises(LLMError, match="web_search_mode=native.*anthropic_messages"):
        client.chat(
            messages=[{"role": "user", "content": "find docs"}],
            tools=[_web_search_function_tool()],
        )


def test_chat_extracts_web_search_citations_sources_and_queries_metadata() -> None:
    content_blocks = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "anthropic docs"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [
                {
                    "type": "web_search_result",
                    "title": "Anthropic Docs",
                    "url": "https://docs.anthropic.com/",
                    "encrypted_content": "encrypted-content",
                    "page_age": "May 21, 2026",
                }
            ],
        },
        {
            "type": "text",
            "text": "Use the docs.",
            "citations": [
                {
                    "type": "web_search_result_location",
                    "title": "Anthropic Docs",
                    "url": "https://docs.anthropic.com/",
                    "encrypted_index": "encrypted-index",
                    "cited_text": "Anthropic documentation excerpt",
                }
            ],
        },
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_search_metadata",
                "model": "claude-sonnet-4-6",
                "content": content_blocks,
                "stop_reason": "end_turn",
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "find docs"}]
    )

    assert response.content == "Use the docs."
    assert response.provider_metadata is not None
    metadata = response.provider_metadata["anthropic_messages"]
    assert metadata["content_blocks"] == content_blocks
    assert metadata["server_tool_uses"][0]["id"] == "srvtoolu_1"
    assert metadata["queries"] == ["anthropic docs"]
    assert metadata["sources"][0]["encrypted_content"] == "encrypted-content"
    assert metadata["citations"][0]["encrypted_index"] == "encrypted-index"


def test_chat_streams_text_and_provider_summarized_thinking_deltas() -> None:
    captured: dict[str, object] = {}
    text_deltas: list[str] = []
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_stream_text",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [],
                        "usage": {"input_tokens": 11, "output_tokens": 0},
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "provider summary"},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "sig-abc"},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "Hello "},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "world."},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 1}),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )

    response = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        on_text_delta=text_deltas.append,
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert captured["stream"] is True
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in captured
    assert response.content == "Hello world."
    assert text_deltas == ["Hello ", "world."]
    assert reasoning_deltas == ["provider summary"]
    assert [item.text for item in response.reasoning] == ["provider summary"]
    assert response.usage is not None
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 15
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["content_blocks"][0]["thinking"] == "provider summary"
    assert metadata["content_blocks"][0]["signature"] == "sig-abc"
    assert "provider summary" not in response.content


def test_trace_callback_does_not_enable_anthropic_thinking_in_auto_mode() -> None:
    captured: dict[str, object] = {}
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_auto",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert response.content == "Done."
    assert reasoning_deltas == []
    assert response.reasoning == ()
    assert "thinking" not in captured
    assert "output_config" not in captured
    assert captured["temperature"] == 1.0


def test_unrequested_thinking_block_stays_opaque_with_trace_callback() -> None:
    captured: dict[str, object] = {}
    reasoning_deltas: list[str] = []
    opaque_block = {
        "type": "thinking",
        "thinking": "Legacy proxy raw thinking that must not be displayed.",
        "signature": "opaque-signature",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_legacy_raw_thinking",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [opaque_block, {"type": "text", "text": "Done."}],
            },
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert "thinking" not in captured
    assert reasoning_deltas == []
    assert response.reasoning == ()
    assert response.content == "Done."
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["content_blocks"][0] == opaque_block


def test_trace_callback_only_changes_visibility_when_thinking_is_on_by_default() -> None:
    captured: dict[str, object] = {}
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_default_thinking",
                "model": "claude-sonnet-5",
                "role": "assistant",
                "type": "message",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Provider summary.",
                        "signature": "opaque-signature",
                    },
                    {"type": "text", "text": "Done."},
                ],
            },
        )

    response = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-5",
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "hello"}],
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "output_config" not in captured
    assert "temperature" not in captured
    assert reasoning_deltas == ["Provider summary."]
    assert [item.text for item in response.reasoning] == ["Provider summary."]


def test_opus_5_thinks_by_default_unlike_opus_4_8() -> None:
    # Omitting `thinking` runs adaptive on Opus 5 but nothing at all on Opus 4.8,
    # so auto mode must only ask for summary visibility on the former.
    captured: dict[str, dict[str, object]] = {}

    def handler_for(model: str):
        def handler(request: httpx.Request) -> httpx.Response:
            captured[model] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "id": f"msg_{model}",
                    "model": model,
                    "role": "assistant",
                    "type": "message",
                    "content": [{"type": "text", "text": "Done."}],
                },
            )

        return handler

    for model in ("claude-opus-5", "claude-opus-4-8"):
        AnthropicMessagesClient(
            base_url="https://api.anthropic.com/v1",
            api_key="test-key",
            model=model,
            transport=httpx.MockTransport(handler_for(model)),
        ).chat(
            messages=[{"role": "user", "content": "hello"}],
            on_reasoning_delta=lambda _text: None,
        )

    assert captured["claude-opus-5"]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "thinking" not in captured["claude-opus-4-8"]
    # Effort stays provider-owned in auto mode on both.
    assert "output_config" not in captured["claude-opus-5"]
    assert "output_config" not in captured["claude-opus-4-8"]


def test_opus_5_thinking_off_never_ships_an_effort_that_would_400() -> None:
    # Opus 5 rejects disabled thinking at xhigh/max. Asking for both must fall
    # back to the server default effort rather than emitting the illegal pair.
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_opus5_off",
                "model": "claude-opus-5",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-opus-5",
        enable_thinking=False,
        reasoning_effort="xhigh",
        transport=httpx.MockTransport(handler),
    ).chat(messages=[{"role": "user", "content": "hello"}])

    assert captured["thinking"] == {"type": "disabled"}
    assert "output_config" not in captured
    assert "temperature" not in captured  # sampling params are a 400 on Opus 5


def test_buffered_thinking_uses_summarized_display_and_preserves_opaque_blocks() -> None:
    captured: dict[str, object] = {}
    reasoning_deltas: list[str] = []
    content_blocks = [
        {
            "type": "thinking",
            "thinking": "Provider-produced summary.",
            "signature": "sig-summary",
        },
        {
            "type": "redacted_thinking",
            "data": "opaque-redacted-state",
        },
        {"type": "text", "text": "Final answer."},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_buffered_thinking",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": content_blocks,
                "usage": {"input_tokens": 3, "output_tokens": 7},
            },
        )

    response = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        reasoning_effort="medium",
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "think"}],
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert captured["output_config"] == {"effort": "medium"}
    assert "temperature" not in captured
    assert reasoning_deltas == ["Provider-produced summary."]
    assert [item.text for item in response.reasoning] == ["Provider-produced summary."]
    assert response.content == "Final answer."
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["content_blocks"] == content_blocks


def test_buffered_summary_display_rejection_falls_back_once_and_caches() -> None:
    payloads: list[dict[str, object]] = []
    reasoning_deltas: list[str] = []
    opaque_block = {
        "type": "thinking",
        "thinking": "Raw thinking after summary display was rejected.",
        "signature": "opaque-signature",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        payloads.append(payload)
        thinking = payload.get("thinking")
        if isinstance(thinking, dict) and thinking.get("display") == "summarized":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking.display summarized is not supported",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_summary_fallback",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [opaque_block, {"type": "text", "text": "Done."}],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        reasoning_effort="medium",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "think"}],
        on_reasoning_delta=reasoning_deltas.append,
    )
    second = client.chat(
        messages=[{"role": "user", "content": "think again"}],
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert len(payloads) == 3
    assert payloads[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert payloads[0]["output_config"] == {"effort": "medium"}
    for payload in payloads[1:]:
        assert payload["thinking"] == {"type": "adaptive"}
        assert payload["output_config"] == {"effort": "medium"}
    assert reasoning_deltas == []
    assert first.reasoning == ()
    assert second.reasoning == ()
    first_metadata = first.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert first_metadata["content_blocks"][0] == opaque_block


def test_streamed_summary_display_rejection_keeps_raw_thinking_opaque() -> None:
    payloads: list[dict[str, object]] = []
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        payloads.append(payload)
        thinking = payload.get("thinking")
        if isinstance(thinking, dict) and thinking.get("display") == "summarized":
            return httpx.Response(
                422,
                json={"error": {"message": "thinking display is an unexpected field"}},
            )
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_stream_summary_fallback",
                        "role": "assistant",
                        "model": "claude-haiku-4-5-20251001",
                        "content": [],
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "Raw thinking that must remain opaque.",
                    },
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "Done."},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 1}),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 4},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )

    response = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-haiku-4-5-20251001",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "think"}],
        stream=True,
        on_reasoning_delta=reasoning_deltas.append,
        max_tokens=4096,
    )

    assert len(payloads) == 2
    assert payloads[0]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 3072,
        "display": "summarized",
    }
    assert payloads[1]["thinking"] == {"type": "enabled", "budget_tokens": 3072}
    assert reasoning_deltas == []
    assert response.reasoning == ()
    assert response.content == "Done."
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["content_blocks"][0]["thinking"] == ("Raw thinking that must remain opaque.")


def test_thinking_without_trace_requests_omitted_display() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_omitted_thinking",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "type": "message",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "sig-omitted"},
                    {"type": "text", "text": "Done."},
                ],
            },
        )

    response = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    ).chat(messages=[{"role": "user", "content": "think"}])

    assert captured["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert "temperature" not in captured
    assert response.reasoning == ()
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["content_blocks"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig-omitted",
    }


def test_computer_use_marker_forces_omitted_display_in_default_thinking_mode() -> None:
    captured: dict[str, object] = {}
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_default_computer_use_thinking",
                "model": "claude-sonnet-5",
                "role": "assistant",
                "type": "message",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "",
                        "signature": "sig-default-computer-use",
                    },
                    {"type": "text", "text": "Done."},
                ],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-5",
        transport=httpx.MockTransport(handler),
    )
    require_omitted_reasoning_display(client)

    response = client.chat(
        messages=[{"role": "user", "content": "Inspect the approved UI."}],
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert captured["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert response.content == "Done."
    assert reasoning_deltas == []
    assert response.reasoning == ()


def test_computer_use_anthropic_tool_loop_replays_empty_signed_thinking_exactly() -> None:
    requests: list[dict[str, object]] = []
    empty_thinking_block = {
        "type": "thinking",
        "thinking": "",
        "signature": "sig-empty:AA+/=_Ω",
    }
    native_tool_block = {
        "type": "tool_use",
        "id": "toolu_computer_state_strict",
        "name": "computer_state",
        "input": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        assert body["thinking"] == {"type": "adaptive", "display": "omitted"}
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg_computer_use_observe",
                    "model": "claude-sonnet-5",
                    "role": "assistant",
                    "type": "message",
                    "content": [empty_thinking_block, native_tool_block],
                    "stop_reason": "tool_use",
                },
            )

        replayed_assistant = body["messages"][1]
        assert replayed_assistant["role"] == "assistant"
        replayed_thinking = replayed_assistant["content"][0]
        assert replayed_thinking == empty_thinking_block
        assert json.dumps(
            replayed_thinking,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") == json.dumps(
            empty_thinking_block,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assert replayed_assistant["content"][1] == native_tool_block
        assert body["messages"][2] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_computer_state_strict",
                    "content": '{"snapshot_id":"snapshot-strict","elements":[]}',
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "id": "msg_computer_use_done",
                "model": "claude-sonnet-5",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-5",
        transport=httpx.MockTransport(handler),
    )
    require_omitted_reasoning_display(client)
    messages = [{"role": "user", "content": "Observe the approved UI."}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "computer_state",
                "description": "Observe the approved UI.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
    ]

    observed = client.chat(messages=messages, tools=tools)
    assert len(observed.tool_calls) == 1
    assert observed.tool_calls[0].id == "toolu_computer_state_strict"
    messages.extend(
        [
            assistant_message_from_response(observed),
            {
                "role": "tool",
                "tool_call_id": observed.tool_calls[0].id,
                "name": observed.tool_calls[0].name,
                "content": '{"snapshot_id":"snapshot-strict","elements":[]}',
            },
        ]
    )
    completed = client.chat(messages=messages, tools=tools)

    assert completed.content == "Done."
    assert len(requests) == 2


def test_computer_use_anthropic_rejects_nonempty_thinking_under_omitted_contract() -> None:
    captured: dict[str, object] = {}
    private_summary = "PRIVATE_DESKTOP_SUMMARY_MUST_NOT_ESCAPE"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_computer_use_violated_omission",
                "model": "claude-sonnet-5",
                "role": "assistant",
                "type": "message",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": private_summary,
                        "signature": "sig-violated-omission",
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_violated_omission",
                        "name": "computer_state",
                        "input": {},
                    },
                ],
                "stop_reason": "tool_use",
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-5",
        transport=httpx.MockTransport(handler),
    )
    require_omitted_reasoning_display(client)

    with pytest.raises(LLMError) as raised:
        client.chat(
            messages=[{"role": "user", "content": "Observe the approved UI."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "computer_state",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )

    assert captured["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert "omitted" in str(raised.value).casefold()
    assert private_summary not in str(raised.value)


def test_manual_thinking_uses_safe_budget_and_omits_forced_tool_choice() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "msg_manual_thinking",
                "model": "claude-haiku-4-5-20251001",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "Done."}],
            },
        )

    response = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-haiku-4-5-20251001",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "fs_read"}},
        on_reasoning_delta=lambda _delta: None,
        max_tokens=4096,
    )

    assert response.content == "Done."
    assert captured["thinking"] == {
        "type": "enabled",
        "budget_tokens": 3072,
        "display": "summarized",
    }
    assert "temperature" not in captured
    assert "tool_choice" not in captured


def test_chat_streams_tool_use_from_partial_json_deltas() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_stream_tool",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "content": [],
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_stream_1",
                        "name": "fs_read",
                        "input": {},
                    },
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path": '},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"README.md"}'},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[
            {"type": "function", "function": {"name": "fs_read", "parameters": {"type": "object"}}}
        ],
        stream=True,
    )

    assert response.content == ""
    assert [(tool.id, tool.name, tool.arguments) for tool in response.tool_calls] == [
        ("toolu_stream_1", "fs_read", {"path": "README.md"})
    ]
    assert response.provider_metadata is not None
    assert response.provider_metadata["anthropic_messages"]["content_blocks"][0]["input"] == {
        "path": "README.md"
    }


def test_chat_streams_parallel_tool_use_blocks_in_index_order() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "msg_parallel_tools", "role": "assistant", "content": []},
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_read",
                        "name": "fs_read",
                        "input": {},
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_shell",
                        "name": "shell_run",
                        "input": {},
                    },
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"cmd":"pytest -q"}'},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":"README.md"}'},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 1}),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "inspect"}],
        tools=[
            {"type": "function", "function": {"name": "fs_read", "parameters": {"type": "object"}}},
            {
                "type": "function",
                "function": {"name": "shell_run", "parameters": {"type": "object"}},
            },
        ],
        stream=True,
    )

    assert [(tool.id, tool.name, tool.arguments) for tool in response.tool_calls] == [
        ("toolu_read", "fs_read", {"path": "README.md"}),
        ("toolu_shell", "shell_run", {"cmd": "pytest -q"}),
    ]


def test_chat_stream_preserves_hosted_web_search_and_citation_metadata() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "msg_stream_search", "role": "assistant", "content": []},
                },
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "server_tool_use",
                        "id": "srvtoolu_stream",
                        "name": "web_search",
                        "input": {},
                    },
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"query":"Anthropic Messages streaming"}',
                    },
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_stream",
                        "content": [
                            {
                                "type": "web_search_result",
                                "title": "Anthropic Docs",
                                "url": "https://docs.anthropic.com/",
                                "encrypted_content": "encrypted-content",
                            }
                        ],
                    },
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 1}),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "text_delta", "text": "Use the Anthropic docs."},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {
                        "type": "citations_delta",
                        "citation": {
                            "type": "web_search_result_location",
                            "title": "Anthropic Docs",
                            "url": "https://docs.anthropic.com/",
                            "encrypted_index": "encrypted-index",
                            "cited_text": "docs excerpt",
                        },
                    },
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 2}),
            _sse_event(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "search"}],
        stream=True,
    )

    assert response.content == "Use the Anthropic docs."
    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert metadata["server_tool_uses"][0]["id"] == "srvtoolu_stream"
    assert metadata["queries"] == ["Anthropic Messages streaming"]
    assert metadata["sources"][0]["encrypted_content"] == "encrypted-content"
    assert metadata["citations"][0]["encrypted_index"] == "encrypted-index"


def test_chat_stream_preserves_unknown_events_without_crashing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "msg_unknown_event", "role": "assistant", "content": []},
                },
            ),
            _sse_event(
                "model_context_window_delta",
                {"type": "model_context_window_delta", "payload": {"remaining": 123}},
            ),
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event("message_stop", {"type": "message_stop"}),
        )

    response = _client(httpx.MockTransport(handler)).chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    metadata = response.provider_metadata["anthropic_messages"]  # type: ignore[index]
    assert response.content == "ok"
    assert metadata["stream_metadata"]["unknown_events"][0]["event"] == "model_context_window_delta"


def test_chat_stream_surfaces_error_events_and_early_termination() -> None:
    def error_handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "msg_stream_error", "role": "assistant", "content": []},
                },
            ),
            _sse_event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "provider overloaded",
                    },
                },
            ),
        )

    with pytest.raises(LLMError, match="overloaded_error: provider overloaded"):
        _client(httpx.MockTransport(error_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )

    def truncated_handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "msg_truncated", "role": "assistant", "content": []},
                },
            )
        )

    with pytest.raises(LLMError, match="ended before message_stop"):
        _client(httpx.MockTransport(truncated_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )

    with pytest.raises(LLMError, match="no message_start"):
        _client(httpx.MockTransport(lambda _request: _sse_response())).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )


def test_chat_surfaces_provider_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"type": "invalid_request_error", "message": "bad request"}},
        )

    with pytest.raises(LLMError, match="LLM error 400: invalid_request_error: bad request"):
        _client(httpx.MockTransport(handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )


def test_chat_rejects_empty_malformed_refusal_and_unsupported_options() -> None:
    with pytest.raises(LLMError, match="does not support response_format"):
        _client(httpx.MockTransport(lambda _request: httpx.Response(500))).chat(
            messages=[{"role": "user", "content": "hello"}],
            response_format={"type": "json_object"},
        )

    def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "msg_empty", "content": [], "stop_reason": "end_turn"}
        )

    with pytest.raises(LLMError, match="returned no assistant text or tool calls"):
        _client(httpx.MockTransport(empty_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )

    def malformed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "msg_bad"})

    with pytest.raises(LLMError, match="missing content list"):
        _client(httpx.MockTransport(malformed_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )

    def refusal_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_refusal",
                "content": [{"type": "refusal", "text": "I cannot help"}],
                "stop_reason": "refusal",
            },
        )

    with pytest.raises(LLMError, match="Anthropic Messages refusal"):
        _client(httpx.MockTransport(refusal_handler)).chat(
            messages=[{"role": "user", "content": "hello"}],
        )


def test_anthropic_chat_and_count_strip_state_from_different_credential_route() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        sent.append(body)
        if str(request.url).endswith("/messages/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 12})
        return httpx.Response(
            200,
            json={
                "id": "msg_new",
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    producer = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="credential-a",
        model="claude-sonnet-4-6",
    )
    consumer = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="credential-b",
        model="claude-sonnet-4-6",
        transport=httpx.MockTransport(handler),
    )
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "public answer",
            PROVIDER_METADATA_KEY: stamp_provider_metadata_for_route(
                {
                    "anthropic_messages": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "private-thinking-state",
                                "signature": "private-signature",
                            },
                            {"type": "text", "text": "public answer"},
                        ]
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


def test_anthropic_extra_headers_override_defaults_case_insensitively() -> None:
    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="fallback-key",
        model="claude-sonnet-4-6",
        extra_headers={
            "X-Api-Key": "override-key",
            "Anthropic-Version": "2099-01-01",
            "Content-Type": "application/custom+json",
        },
    )

    headers = client._headers()

    assert headers["x-api-key"] == "override-key"
    assert headers["anthropic-version"] == "2099-01-01"
    assert headers["content-type"] == "application/custom+json"
    assert len({name.casefold() for name in headers}) == len(headers)
