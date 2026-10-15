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
