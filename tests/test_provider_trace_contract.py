"""Provider trace visibility must agree at the request and callback boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.gemini_interactions import GeminiInteractionsClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.protocols import resolve_reasoning_trace_capability
from alysis_code.profile_presets import PROFILE_PRESETS, make_profile_from_preset

_SUMMARY = "Checked the constraints."
_ANSWER = "Done."
_MESSAGES = [{"role": "user", "content": "Hello"}]
_ROUTES = [
    ("openai_compat", "openrouter", OpenAICompatClient, "https://openrouter.ai/api/v1", "test"),
    ("openai_responses", "openai", OpenAIResponsesClient, "https://api.openai.com/v1", "gpt-5.5"),
    (
        "anthropic_messages",
        "anthropic",
        AnthropicMessagesClient,
        "https://api.anthropic.com/v1",
        "claude-sonnet-4-6",
    ),
    (
        "gemini_generate_content",
        "gemini",
        GeminiGenerateContentClient,
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3-flash-preview",
    ),
    (
        "gemini_interactions",
        "gemini",
        GeminiInteractionsClient,
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3-flash-preview",
    ),
]


def _response(
    protocol: str, stream: bool, *, compatible_adapter: str = "openrouter_reasoning"
) -> httpx.Response:
    if protocol == "openai_compat":
        detail = {"type": "reasoning.summary", "index": 0, "summary": _SUMMARY}
        if compatible_adapter == "mistral_thinking":
            fields = {
                "content": [{"type": "thinking", "thinking": [{"type": "text", "text": _SUMMARY}]}]
            }
            answer = {"content": [*fields["content"], {"type": "text", "text": _ANSWER}]}
        elif compatible_adapter in {
            "deepseek_reasoning",
            "dashscope_thinking",
            "moonshot_reasoning",
            "nvidia_reasoning",
            "glm_reasoning",
        }:
            fields = {"reasoning_content": _SUMMARY}
            answer = {**fields, "content": _ANSWER}
        else:
            fields = {"reasoning_details": [detail]}
            answer = {**fields, "content": _ANSWER}
        message = {"role": "assistant", **answer}
        body = {"choices": [{"message": message, "finish_reason": "stop"}]}
        events = [
            {"choices": [{"delta": fields}]},
            {"choices": [{"delta": {"content": _ANSWER}, "finish_reason": "stop"}]},
        ]
    elif protocol == "openai_responses":
        body = {
            "id": "resp_trace",
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": _SUMMARY}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": _ANSWER}],
                },
            ],
        }
        events = [
            {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "summary_index": 0,
                "delta": _SUMMARY,
            },
            {
                "type": "response.output_text.delta",
                "output_index": 1,
                "content_index": 0,
                "delta": _ANSWER,
            },
            {"type": "response.completed", "response": body},
        ]
    elif protocol == "anthropic_messages":
        body = {
            "id": "msg_trace",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": _SUMMARY, "signature": "opaque"},
                {"type": "text", "text": _ANSWER},
            ],
            "stop_reason": "end_turn",
        }
        events = [
            {"type": "message_start", "message": {"id": "msg_trace", "content": []}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": _SUMMARY},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": _ANSWER},
            },
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
    elif protocol == "gemini_generate_content":
        body = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"thought": True, "text": _SUMMARY, "thoughtSignature": "opaque"},
                            {"text": _ANSWER},
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        events = [body]
    else:
        body = {
            "id": "interaction_trace",
            "steps": [
                {
                    "type": "thought",
                    "signature": "opaque",
                    "summary": [{"type": "text", "text": _SUMMARY}],
                },
                {"type": "model_output", "content": [{"type": "text", "text": _ANSWER}]},
            ],
        }
        events = []
    if not stream:
        return httpx.Response(200, json=body)
    sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    if protocol == "openai_compat":
        sse += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)


def _client(route: tuple, requests: list[dict[str, Any]], *, adapter: str = "auto"):
    protocol, provider, client_type, base_url, model = route

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        stream = bool(payload.get("stream")) or "streamGenerateContent" in request.url.path
        return _response(protocol, stream)

    kwargs = {} if protocol == "gemini_interactions" else {"reasoning_effort": "low"}
    client = client_type(
        base_url=base_url,
        api_key="test-key",
        model=model,
        provider_key=provider,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    client.reasoning_trace_capability = resolve_reasoning_trace_capability(
        provider_key=provider,
        protocol=protocol,
        adapter_override=adapter,
    )
    return client


@pytest.mark.parametrize("route", _ROUTES, ids=lambda route: route[0])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("adapter", ["auto", "none"])
@pytest.mark.parametrize("trace_enabled", [False, True])
def test_trace_request_and_display_follow_opt_in_for_every_protocol(
    route: tuple,
    stream: bool,
    adapter: str,
    trace_enabled: bool,
) -> None:
    protocol = route[0]
    if stream and protocol == "gemini_interactions":
        pytest.skip("Interactions explicitly supports buffered requests only")
    requests: list[dict[str, Any]] = []
    displayed: list[str] = []
    client = _client(route, requests, adapter=adapter)
    response = client.chat(
        messages=_MESSAGES,
        stream=stream,
        on_reasoning_delta=displayed.append if trace_enabled else None,
    )
    visible = trace_enabled and adapter != "none"
    assert response.content == _ANSWER
    assert displayed == ([_SUMMARY] if visible else [])
    assert len(requests) == 1
    payload = requests[0]
    if protocol == "openai_responses":
        assert payload["reasoning"] == {"effort": "low", **({"summary": "auto"} if visible else {})}
    elif protocol == "anthropic_messages":
        assert payload["thinking"]["display"] == ("summarized" if visible else "omitted")
        assert payload["output_config"]["effort"] == "low"
    elif protocol == "gemini_generate_content":
        assert payload["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "low",
            **({"includeThoughts": True} if visible else {}),
        }
    elif protocol == "gemini_interactions":
        assert payload.get("generation_config", {}).get("thinking_summaries") == (
            "auto" if visible else None
        )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("trace_level", ["off", "compact", "full"])
def test_chat_only_turn_uses_the_same_trace_lifecycle(tmp_path, stream, trace_level):
    from test_agent_loop_provider_behaviors import (
        _ReasoningCaptureSurface,
        _session,
        _StreamingReplyClient,
    )

    from alysis_code.config import AppConfig

    surface = _ReasoningCaptureSurface()
    surface.reasoning_trace_enabled = trace_level != "off"
    client = _StreamingReplyClient(reply=_ANSWER, reasoning_deltas=[_SUMMARY])
    session = _session(tmp_path, cfg=AppConfig(model="test-model", stream=stream), surface=surface)
    session.client = client
    try:
        assert session.run_turn("Hello", chat_only=True) == 0
    finally:
        session.close()
    assert client.summary_callback_supplied is (trace_level != "off")
    assert surface.reasoning_deltas == ([_SUMMARY] if trace_level != "off" else [])
    assert len(surface.reasoning_starts) == int(trace_level != "off")
    assert surface.reasoning_ends == surface.reasoning_starts
    assert client.call_tools == [None]


@pytest.mark.parametrize("carrier", ["response.output_item.done", "response.completed"])
def test_responses_completed_summary_blocks_are_displayed_once(carrier: str) -> None:
    route = next(route for route in _ROUTES if route[0] == "openai_responses")
    client = _client(route, [])
    body = _response("openai_responses", False).json()
    events = []
    if carrier == "response.output_item.done":
        event = {"type": carrier, "output_index": 0, "item": body["output"][0]}
        events.extend([event, event])
    events.append({"type": "response.completed", "response": body})
    client._transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )
    )
    displayed: list[str] = []
    response = client.chat(messages=_MESSAGES, stream=True, on_reasoning_delta=displayed.append)
    assert displayed == [_SUMMARY]
    assert [item.text for item in response.reasoning] == [_SUMMARY]
    assert response.content == _ANSWER


def test_anthropic_initial_summary_text_is_not_lost() -> None:
    route = next(route for route in _ROUTES if route[0] == "anthropic_messages")
    client = _client(route, [])
    sse = _response("anthropic_messages", True).text
    sse = sse.replace('"thinking": ""', '"thinking": "Initial. "', 1)
    client._transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse,
        )
    )
    displayed: list[str] = []
    response = client.chat(messages=_MESSAGES, stream=True, on_reasoning_delta=displayed.append)
    assert "".join(displayed) == "Initial. " + _SUMMARY
    assert [item.text for item in response.reasoning] == ["Initial. " + _SUMMARY]
    assert response.content == _ANSWER


@pytest.mark.parametrize("assembly", ["concat", "cumulative"])
def test_compatible_stream_keeps_complete_summary_blocks_and_zero_index(assembly: str) -> None:
    route = next(route for route in _ROUTES if route[0] == "openai_compat")
    client = _client(route, [])
    client.stream_assembly = assembly
    details = [
        [{"type": "reasoning.summary", "index": 0, "summary": "Check "}],
        [
            {"type": "reasoning.encrypted", "index": 2, "data": "opaque"},
            {
                "type": "reasoning.summary",
                "index": 0,
                "summary": "Check " if assembly == "concat" else "Check Check ",
            },
        ],
        [
            {
                "type": "reasoning.summary",
                "index": 0,
                "summary": "again." if assembly == "concat" else "Check Check again.",
            }
        ],
    ]
    events = [{"choices": [{"delta": {"reasoning_details": chunk}}]} for chunk in details]
    events.append({"choices": [{"delta": {"content": _ANSWER}, "finish_reason": "stop"}]})
    sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    client._transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse,
        )
    )
    displayed: list[str] = []
    response = client.chat(messages=_MESSAGES, stream=True, on_reasoning_delta=displayed.append)
    assert "".join(displayed) == "Check Check again."
    assert [item.text for item in response.reasoning] == ["Check Check again."]
    # Normalizing visible text must not modify the provider's replay blocks.
    assert response.provider_metadata["openrouter"]["reasoning_details"] == [
        detail for chunk in details for detail in chunk
    ]


def test_compatible_summary_stream_preserves_whitespace_deltas() -> None:
    client = _client(_ROUTES[0], [])
    pieces = ["Check", " ", "constraints", "\n", "again."]
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {"type": "reasoning.summary", "index": 0, "summary": piece},
                        ]
                    }
                }
            ]
        }
        for piece in pieces
    ]
    events.append({"choices": [{"delta": {"content": _ANSWER}, "finish_reason": "stop"}]})
    sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    client._transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse,
        )
    )
    displayed: list[str] = []
    response = client.chat(messages=_MESSAGES, stream=True, on_reasoning_delta=displayed.append)
    assert displayed == pieces
    assert [item.text for item in response.reasoning] == ["".join(pieces)]


@pytest.mark.parametrize("preset", PROFILE_PRESETS, ids=lambda preset: preset.key)
@pytest.mark.parametrize("stream", [False, True])
def test_every_provider_preset_uses_its_resolved_trace_policy(preset, stream) -> None:
    from test_llm_factory_profiles import _cfg_with_profile

    from alysis_code.llm.factory import make_llm_client, resolve_model_protocol

    profile = make_profile_from_preset(preset)
    if not profile.base_url:
        profile = replace(profile, base_url="https://custom.example/v1")
    model = profile.default_model or "test-model"
    protocol = resolve_model_protocol(
        provider_key=preset.provider_key,
        protocol=profile.protocol,
        model=model,
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(
            protocol, stream, compatible_adapter=client.reasoning_trace_capability.adapter
        )

    client = make_llm_client(
        cfg=_cfg_with_profile(profile),
        api_key="test-key",
        model=model,
        reasoning_effort="low",
        transport=httpx.MockTransport(handler),
    )
    displayed: list[str] = []
    response = client.chat(messages=_MESSAGES, stream=stream, on_reasoning_delta=displayed.append)
    assert response.content == _ANSWER
    assert len(requests) == 1
    assert displayed == (
        [_SUMMARY] if client.reasoning_trace_capability.has_visible_reasoning else []
    )
