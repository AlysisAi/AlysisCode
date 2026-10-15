from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import LLMError
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import ProfileSpec, add_profile, get_active_profile, set_active_profile
from alysis_code.provider_auth.base import ProviderRequestAuthorization
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth


@pytest.mark.parametrize(
    ("preset_key", "model", "context", "input_price", "output_price", "cache_read", "cache_write"),
    [
        (preset, model, 1_050_000, inp, out, read, write)
        for preset in ("openai", "openai-responses")
        for model, inp, out, read, write in (
            ("gpt-6-sol", 2, 10, 0.2, 2.5),
            ("gpt-6-luna", 0.1, 0.5, 0.01, 0.125),
        )
    ]
    + [
        (preset, "claude-opus-5-5", 1_128_000, 4, 20, 0.2, 5)
        for preset in ("anthropic", "anthropic-native", "anthropic-compat")
    ]
    + [
        ("openrouter", "openai/gpt-6-sol", 1_050_000, 2, 10, 0.2, 2.5),
        ("openrouter", "openai/gpt-6-luna", 1_050_000, 0.1, 0.5, 0.01, 0.125),
        ("openrouter", "anthropic/claude-opus-5.5", 1_000_000, 4, 20, 0.2, 5),
    ],
)
def test_release_models_can_be_selected_saved_and_resolved(
    preset_key, model, context, input_price, output_price, cache_read, cache_write
):
    preset = get_preset(preset_key)
    assert preset is not None
    profile = make_profile_from_preset(preset)
    cfg = AppConfig(model=profile.default_model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert model in [row.value for row in flow.screen().rows]
    flow.choose(model)
    flow.submit_input("")
    efforts = [row.value for row in flow.screen().rows]
    assert "max" in efforts
    if preset_key != "openrouter":
        assert "minimal" not in efforts and "ultra" not in efforts
    if model in {"claude-opus-5-5", "anthropic/claude-opus-5.5"}:
        assert "off" not in efforts
    flow.choose("max")
    assert flow.state.commit_to(cfg).saved
    assert cfg.model == model
    assert get_active_profile(cfg).reasoning_effort == "max"
    meta = ModelRegistry(cfg=cfg).get(model)
    assert meta.context_window_tokens == context
    assert meta.max_output_tokens == 128_000
    assert meta.supports_reasoning is True and meta.supports_vision is True
    assert meta.input_cost_per_token == pytest.approx(input_price / 1_000_000)
    assert meta.output_cost_per_token == pytest.approx(output_price / 1_000_000)
    assert meta.cache_read_input_cost_per_token == pytest.approx(cache_read / 1_000_000)
    assert meta.cache_creation_input_cost_per_token == pytest.approx(cache_write / 1_000_000)
    assert meta.field_sources["context_window_tokens"] == (
        f"official_provider_model_catalog:{preset.provider_key}"
    )


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna"])
def test_codex_new_models_use_subscription_efforts_and_capacity(monkeypatch, model):
    monkeypatch.setattr(
        OpenAICodexSubscriptionAuth,
        "authorize_request",
        lambda *a, **k: ProviderRequestAuthorization("fixture-scope", {}),
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": [{"slug": model}]})
        )
    )
    monkeypatch.setattr("alysis_code.provider_auth.create_provider_auth", lambda _id: adapter)
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-5.6-sol",
        reasoning_effort="low",
    )
    cfg = AppConfig(model=profile.default_model)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    # A Sol catalog gains the reviewed ChatGPT models; a Luna-only one is not broadened.
    expected = (
        [
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
        ]
        if model == "gpt-6-sol"
        else [model]
    )
    assert [row.value for row in flow.screen().rows] == expected
    flow.choose(model)
    efforts = ["auto", "off", "low", "medium", "high", "xhigh", "max"]
    assert [row.value for row in flow.screen().rows] == efforts
    flow.choose(efforts[-1])
    assert flow.state.commit_to(cfg).saved
    assert cfg.model == model
    meta = ModelRegistry(cfg=cfg).get(model)
    assert meta.context_window_tokens == 272_000
    assert meta.input_cost_per_token == 0.0


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna"])
def test_gpt6_responses_requests_keep_selected_model_effort_and_tools(model):
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp_test", "status": "completed", "output": []})

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model=model,
        reasoning_effort="max",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "List files"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    assert captured["model"] == model
    assert captured["reasoning"]["effort"] == "max"
    assert captured["tools"][0]["name"] == "list_files"


def test_opus55_auto_thinking_preserves_progress_and_omits_forced_tool_choice():
    captured = {}
    deltas = []

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "content": [
                    {"type": "thinking", "thinking": "Checking files.", "signature": "opaque"},
                    {"type": "text", "text": "Done."},
                ],
            },
        )

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-opus-5-5",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "List files"}],
        on_reasoning_delta=deltas.append,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
    )
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "tool_choice" not in captured
    assert "temperature" not in captured
    assert deltas == ["Checking files."]


@pytest.mark.parametrize("settings", [{"enable_thinking": False}, {"reasoning_effort": "none"}])
def test_opus55_rejects_thinking_off_before_sending(settings):
    def handler(request):
        pytest.fail("Invalid Opus 5.5 thinking settings must not reach the provider")

    client = AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-opus-5-5",
        transport=httpx.MockTransport(handler),
        **settings,
    )
    with pytest.raises(LLMError, match="does not support disabling thinking"):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_switching_from_opus5_with_thinking_off_hides_off_for_opus55():
    preset = get_preset("anthropic")
    profile = replace(
        make_profile_from_preset(preset), default_model="claude-opus-5", reasoning_effort="none"
    )
    cfg = AppConfig(model=profile.default_model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    flow.choose("claude-opus-5-5")
    flow.submit_input("")
    assert "off" not in [row.value for row in flow.screen().rows]
    flow.choose("auto")
    assert flow.state.commit_to(cfg).saved
    assert cfg.llm_enable_thinking is None


def test_openrouter_opus55_auto_mode_omits_forced_tool_choice():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}]})

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model="anthropic/claude-opus-5.5",
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "List files"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
    )
    assert "tool_choice" not in captured
    assert "reasoning" not in captured
    client.enable_thinking = False
    with pytest.raises(LLMError, match="does not support disabling thinking"):
        client.chat(messages=[{"role": "user", "content": "hi"}])
