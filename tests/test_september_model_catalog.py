from __future__ import annotations

import httpx
import pytest

from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import ProfileSpec, add_profile, get_active_profile, set_active_profile
from alysis_code.provider_auth import openai_codex
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
from alysis_code.provider_auth.store import ProviderTokenRecord


def _subscription_cfg() -> AppConfig:
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        auth_provider="openai-codex",
        default_model="gpt-5.6-sol",
        reasoning_effort="low",
    )
    cfg = AppConfig(model=profile.default_model)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_astra_subscription_selection_uses_current_catalog_and_saves_reasoning(monkeypatch):
    monkeypatch.delenv("ALYSIS_OPENAI_CODEX_COMPAT_VERSION", raising=False)
    monkeypatch.setattr(
        openai_codex,
        "load_provider_token",
        lambda _provider_id: ProviderTokenRecord(
            client_id="test-client",
            subject="test-subject",
            scopes=("chatgpt.tokens.use.direct",),
            access_token="test-access",
            refresh_token="test-refresh",
            expires_at=9_999_999_999,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["User-Agent"] == "Alysis-Code"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-5.6-sol", "priority": 1},
                    # Missing capacity and efforts must use the subscription
                    # snapshot, not Astra's larger API limits.
                    {"slug": "gpt-6-astra", "display_name": "GPT-6 Astra", "priority": 2},
                    {"slug": "hidden-model", "visibility": "hide"},
                ]
            },
        )

    adapter = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("alysis_code.provider_auth.create_provider_auth", lambda _id: adapter)
    cfg = _subscription_cfg()
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert [row.value for row in flow.screen().rows] == [
        "gpt-6.1-sol",
        "gpt-5.6-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]
    flow.choose("gpt-6-astra")
    assert flow.stage == "model_thinking"
    assert [row.value for row in flow.screen().rows] == [
        "auto",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    flow.choose("max")
    assert flow.state.commit_to(cfg).saved
    profile = get_active_profile(cfg)
    assert profile is not None
    assert profile.default_model == cfg.model == "gpt-6-astra"
    assert profile.reasoning_effort == "max"
    metadata = ModelRegistry(cfg=cfg).get(cfg.model)
    assert metadata.context_window_tokens == 272_000
    assert metadata.supports_vision is True
    assert metadata.input_cost_per_token == 0.0


def test_astra_snapshot_does_not_grant_subscription_access(monkeypatch):
    monkeypatch.setattr(
        openai_codex,
        "load_provider_token",
        lambda _provider_id: ProviderTokenRecord(
            access_token="fixture-access",
            expires_at=9_999_999_999,
            client_id="test-client",
            subject="test-subject",
            scopes=("chatgpt.tokens.use.direct",),
        ),
    )
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": [{"slug": "gpt-5.6-sol"}]})
        )
    )
    monkeypatch.setattr("alysis_code.provider_auth.create_provider_auth", lambda _id: adapter)
    flow = ConfigFlow(cfg=_subscription_cfg())
    flow.choose("default")
    assert [row.value for row in flow.screen().rows] == ["gpt-5.6-sol"]


def test_legacy_codex_version_override_cannot_impersonate_codex(monkeypatch):
    monkeypatch.setenv("ALYSIS_OPENAI_CODEX_COMPAT_VERSION", "0.155.0")
    monkeypatch.setattr(
        openai_codex,
        "load_provider_token",
        lambda _id: ProviderTokenRecord(
            client_id="test-client",
            subject="test-subject",
            scopes=("chatgpt.tokens.use.direct",),
            access_token="test-access",
            refresh_token="test-refresh",
            expires_at=9_999_999_999,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["User-Agent"] == "Alysis-Code"
        return httpx.Response(200, json={"models": [{"slug": "gpt-6-astra"}]})

    adapter = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler))
    models = adapter.list_models(refresh=True)
    assert [model.id for model in models if model.is_default] == ["gpt-6-astra"]
    assert adapter.authorization_headers(f"{adapter.base_url}/responses")["User-Agent"] == (
        "Alysis-Code"
    )


@pytest.mark.parametrize(
    ("provider", "model", "context", "output", "vision", "input_price"),
    [
        ("mistral", "zai-glm-5-3", 1_000_000, 128_000, False, 0.0000014),
        ("openrouter", "x-ai/grok-4.7", 500_000, 450_000, True, 0.0000016),
        ("openrouter", "z-ai/glm-5.3-flashx", 1_048_576, 131_072, True, 0.00000037),
        ("openrouter", "deepseek/deepseek-v4.1-flash", 1_048_576, 384_000, True, 0.0000003),
        ("openrouter", "qwen/qwen3.8-max-0902", 1_000_000, 131_072, True, 0.000002),
        ("openrouter", "xiaomi/mimo-v2.6-pro", 1_048_576, 131_072, True, 0.000000435),
        ("openrouter", "xiaomi/mimo-v2.6-flash", 1_048_576, 131_072, True, 0.00000014),
        ("openrouter", "xiaomi/mimo-v2.6-pro-ultraspeed", 1_048_576, 131_072, True, 0.00000435),
        ("openrouter", "inception/mercury-2.5", 260_000, 65_536, False, 0.00000004),
    ],
)
def test_september_models_are_selectable_with_provider_specific_limits(
    provider, model, context, output, vision, input_price
):
    preset = get_preset(provider)
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
    flow.choose("auto")
    assert flow.state.commit_to(cfg).saved
    assert cfg.model == model
    metadata = ModelRegistry(cfg=cfg).get(model)
    assert metadata.context_window_tokens == context
    assert metadata.max_output_tokens == output
    assert metadata.supports_vision is vision
    assert metadata.input_cost_per_token == pytest.approx(input_price)
    assert metadata.field_sources["context_window_tokens"] == (
        f"official_provider_model_catalog:{provider}"
    )
