from __future__ import annotations

import pytest

from alysis_code.alysis_cloud import gateway_base_url
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
)
from alysis_code.profile_presets import PROFILE_PRESETS, ProfilePreset
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile

EXPECTED_PROVIDER_KEYS = {
    "alysis": "alysis",
    "openai": "openai",
    "openai-responses": "openai",
    "anthropic": "anthropic",
    "anthropic-compat": "anthropic",
    "anthropic-native": "anthropic",
    "gemini": "gemini",
    "gemini-compat": "gemini",
    "gemini-native": "gemini",
    "deepseek": "deepseek",
    "nvidia": "nvidia",
    "qwen-intl": "qwen",
    "qwen-us": "qwen",
    "qwen-cn": "qwen",
    "zhipu": "zhipu",
    "zai": "zai",
    "zai-coding-plan": "zai_coding_plan",
    "moonshot": "moonshot",
    "kimi-code": "moonshot",
    "moonshot-cn": "moonshot",
    "minimax": "minimax",
    "xiaomi-mimo": "xiaomi",
    "bytedance": "bytedance",
    "groq": "groq",
    "cerebras": "cerebras",
    "mistral": "mistral",
    "xai": "xai",
    "cohere": "cohere",
    "openrouter": "openrouter",
    "perplexity": "perplexity",
    "together": "together",
    "fireworks": "fireworks",
    "ollama": "ollama",
    "lm-studio": "lm-studio",
    "vllm": "vllm",
    "custom": "",
}

EXPECTED_COUNT_STRATEGY = {
    OPENAI_COMPAT_PROTOCOL: "openai_compat_provider_payload",
    OPENAI_RESPONSES_PROTOCOL: "openai_responses",
    ANTHROPIC_MESSAGES_PROTOCOL: "anthropic_messages",
    GEMINI_GENERATE_CONTENT_PROTOCOL: "gemini_count_tokens",
}

AUTHORITATIVE_RESPONSE_PRESETS = {
    "openai",
    "openai-responses",
    "anthropic",
    "anthropic-compat",
    "anthropic-native",
    "gemini",
    "gemini-compat",
    "gemini-native",
    "moonshot",
    "kimi-code",
    "moonshot-cn",
}


def _client_for_preset(preset_key: str):
    preset = next(item for item in PROFILE_PRESETS if item.key == preset_key)
    base_url = (
        gateway_base_url() if preset.key == "alysis" else preset.base_url
    ) or "https://custom.example/v1"
    model = preset.validation_model or next(iter(preset.suggested_models), "local-model")
    profile = ProfileSpec(
        name=preset.key,
        protocol=preset.protocol,
        base_url=base_url,
        api_key_env=preset.api_key_env,
        default_model=model,
        cache_capability=preset.cache_capability,
    )
    cfg = AppConfig(model=model, base_url=base_url)
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return make_llm_client(cfg=cfg, api_key="test-key", model=model)


def test_provider_accounting_matrix_is_exhaustive() -> None:
    assert set(EXPECTED_PROVIDER_KEYS) == {preset.key for preset in PROFILE_PRESETS}
    for preset in PROFILE_PRESETS:
        assert preset.provider_key == EXPECTED_PROVIDER_KEYS[preset.key]


def test_profile_preset_preserves_legacy_positional_optional_fields() -> None:
    headers = {"X-Custom-Header": "value"}
    preset = ProfilePreset(
        "legacy-extension",
        "Legacy extension",
        OPENAI_COMPAT_PROTOCOL,
        "https://provider.example/v1",
        None,
        headers,
    )

    assert preset.extra_headers == headers
    assert preset.provider_key == ""


@pytest.mark.parametrize(
    "preset_key",
    [preset.key for preset in PROFILE_PRESETS],
)
def test_every_preset_has_an_explicit_usage_and_preflight_contract(
    preset_key: str,
    configured_hosted_api: str,
) -> None:
    preset = next(item for item in PROFILE_PRESETS if item.key == preset_key)
    client = _client_for_preset(preset_key)

    if preset_key != "custom":
        assert client.provider_key == EXPECTED_PROVIDER_KEYS[preset_key]
    assert (
        client.usage_contract.input_token_count_strategy == EXPECTED_COUNT_STRATEGY[preset.protocol]
    )
    assert client.usage_contract.supports_input_token_count is True
    assert client.usage_contract.response_usage_authoritative is (
        preset_key in AUTHORITATIVE_RESPONSE_PRESETS
    )

    if preset.protocol == OPENAI_COMPAT_PROTOCOL:
        measured = client.count_input_tokens(
            messages=[
                {"role": "system", "content": "Απάντησε με ακρίβεια."},
                {"role": "user", "content": "中文 العربية 👩🏽‍💻"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                }
            ],
        )
        assert measured.input_tokens > 0
        assert measured.source.value == "local_estimate"
        assert measured.confidence.value == "estimated"


@pytest.mark.parametrize("preset_key", [preset.key for preset in PROFILE_PRESETS])
@pytest.mark.parametrize("stream", [False, True])
def test_every_preset_delivers_provider_usage_to_context(preset_key, stream):
    """Offline transport contract: response fields, not live provider availability."""
    import json

    import httpx

    from alysis_code.provider_usage import capture_provider_usage

    preset = next(item for item in PROFILE_PRESETS if item.key == preset_key)
    client = _client_for_preset(preset_key)
    expected_cached = 60
    expected_output = 20
    if preset.protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        usage = {
            "input_tokens": 30,
            "output_tokens": 20,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 10,
        }
        body = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": client.model,
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": usage,
        }
        events = [
            {
                "type": "message_start",
                "message": {
                    **body,
                    "content": [],
                    "stop_reason": None,
                    "usage": {**usage, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "done"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 20},
            },
            {"type": "message_stop"},
        ]
    elif preset.protocol == GEMINI_GENERATE_CONTENT_PROTOCOL:
        body = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "done"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 15,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 120,
                "cachedContentTokenCount": 60,
            },
        }
        events = [body]
    elif preset.protocol == OPENAI_RESPONSES_PROTOCOL:
        body = {
            "id": "resp_test",
            "status": "completed",
            "model": client.model,
            "output": [
                {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 60},
                "output_tokens_details": {"reasoning_tokens": 5},
            },
        }
        events = [{"type": "response.completed", "response": body}]
    else:
        body = {
            "id": "chat_test",
            "model": client.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 60},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }
        events = [
            {
                **body,
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
            }
        ]

    def handler(request):
        assert request.method == "POST"
        if not stream:
            return httpx.Response(200, json=body)
        content = "".join(
            (f"event: {event['type']}\n" if "type" in event else "")
            + f"data: {json.dumps(event)}\n\n"
            for event in events
        )
        if preset.protocol == OPENAI_COMPAT_PROTOCOL:
            content += "data: [DONE]\n\n"
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})

    client._transport = httpx.MockTransport(handler)
    response = client.chat(messages=[{"role": "user", "content": "hello"}], stream=stream)
    reading = capture_provider_usage(response.usage, client=client)
    assert reading is not None
    assert reading["input_tokens"] == 100
    assert reading["output_tokens"] == expected_output
    assert reading["cached_tokens"] == expected_cached
    assert reading["total_tokens"] == 120
    assert reading["input_scope"] == "request"
    if preset.protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        assert reading["cache_write_tokens"] == 10
        assert reading["uncached_tokens"] == 30
        assert reading["uncached_derived"] is False
    else:
        assert reading["reasoning_tokens"] == 5
        assert reading["cache_write_tokens"] is None
        assert reading["uncached_tokens"] == 40
        assert reading["uncached_derived"] is True
