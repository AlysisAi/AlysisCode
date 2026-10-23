from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

import alysis_code.provider_auth as provider_auth_mod
from alysis_code.chatgpt_codex_static_provider import (
    CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE,
    load_chatgpt_codex_static_models,
    resolve_chatgpt_codex_static_model,
)
from alysis_code.config import AppConfig
from alysis_code.litellm_static_provider import resolve_litellm_static_metadata
from alysis_code.model_registry import ModelRegistry
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile


def _subscription_cfg(model: str) -> AppConfig:
    cfg = AppConfig(model=model)
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model=model,
        reasoning_effort="medium",
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_subscription_snapshot_is_distinct_from_api_catalog() -> None:
    subscription = resolve_chatgpt_codex_static_model("gpt-5.5")
    api = resolve_litellm_static_metadata(
        "gpt-5.5",
        base_url="https://api.openai.com/v1",
    )

    assert subscription is not None
    assert subscription.context_window_tokens == 272_000
    assert api.context_window_tokens is not None
    assert api.context_window_tokens > subscription.context_window_tokens
    assert [effort for effort, _description in subscription.reasoning_efforts] == [
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_subscription_snapshot_contains_current_visible_capacity_tiers() -> None:
    models = {model.id: model for model in load_chatgpt_codex_static_models()}

    assert set(models) == {
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    }
    astra = models["gpt-6-astra"]
    assert astra.context_window_tokens == 272_000
    assert astra.default_reasoning_effort == "medium"
    assert astra.input_modalities == ("text", "image")
    assert [effort for effort, _description in astra.reasoning_efforts] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert models["gpt-5.6-sol"].context_window_tokens == 272_000
    assert models["gpt-5.6-sol"].default_reasoning_effort == "low"
    assert models["gpt-5.6-terra"].context_window_tokens == 272_000
    assert models["gpt-5.6-luna"].context_window_tokens == 272_000
    assert models["gpt-5.5"].context_window_tokens == 272_000
    for model in ("gpt-6-sol", "gpt-6-luna"):
        assert models[model].context_window_tokens == 272_000
        assert models[model].default_reasoning_effort == "medium"
        efforts = [effort for effort, _ in models[model].reasoning_efforts]
        assert efforts == ["low", "medium", "high", "xhigh", "max"] + (
            ["ultra"] if model == "gpt-6-sol" else []
        )


class _OfflineSubscriptionAdapter:
    def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
        _ = refresh
        raise RuntimeError("catalog offline")


@pytest.mark.parametrize(
    "model", ["gpt-6.1-sol", "gpt-5.6-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]
)
def test_registry_uses_subscription_snapshot_when_live_catalog_is_offline(
    monkeypatch, model
) -> None:
    monkeypatch.setattr(
        provider_auth_mod,
        "create_provider_auth",
        lambda _provider_id: _OfflineSubscriptionAdapter(),
    )

    meta = ModelRegistry(cfg=_subscription_cfg(model)).get(model)

    assert meta.context_window_tokens == 272_000
    assert meta.max_output_tokens == 32_000
    assert meta.supports_vision is True
    assert meta.input_cost_per_token == 0.0
    assert meta.field_sources["context_window_tokens"] == (
        f"provider_auth:openai-codex:{CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE}"
    )
    assert "bundled_litellm_snapshot" not in meta.field_sources["context_window_tokens"]


def test_registry_never_uses_api_capacity_for_unknown_subscription_model(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_auth_mod,
        "create_provider_auth",
        lambda _provider_id: _OfflineSubscriptionAdapter(),
    )

    meta = ModelRegistry(cfg=_subscription_cfg("gpt-unknown-subscription")).get(
        "gpt-unknown-subscription"
    )

    assert meta.context_window_tokens == 128_000
    assert meta.max_output_tokens == 32_000
    assert meta.field_sources["context_window_tokens"] == (
        "provider_auth:openai-codex:conservative-default"
    )


def _load_refresh_script() -> object:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "refresh_chatgpt_codex_model_catalog.py"
    spec = importlib.util.spec_from_file_location(
        "refresh_chatgpt_codex_model_catalog",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_subscription_refresh_script_writes_sanitized_deterministic_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_refresh_script()
    output_path = tmp_path / "snapshot.json"
    input_path = tmp_path / "raw.json"
    input_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "hidden-model",
                        "visibility": "hide",
                        "context_window": 999_999,
                    },
                    {
                        "slug": "subscription-model",
                        "display_name": "Subscription Model",
                        "priority": 2,
                        "context_window": 272_000,
                        "input_modalities": ["text", "image"],
                        "default_reasoning_level": "high",
                        "supported_reasoning_levels": [
                            {"effort": "low", "description": "Fast"},
                            {"effort": "high", "description": "Deep"},
                        ],
                        "unreviewed_server_field": "must not persist",
                    },
                ],
                "account": {"email": "must-not-persist@example.test"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_snapshot_path", lambda: output_path)

    result = module.main(
        [
            "--input",
            str(input_path),
            "--client-version",
            "0.144.6",
            "--fetched-at",
            "2026-07-11T12:00:00Z",
        ]
    )

    assert result == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["client_version"] == "0.144.6"
    assert payload["source"] == CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE
    assert payload["usage"] == "capacity_and_capability_fallback_only_not_entitlement"
    assert payload["input_sha256"] == hashlib.sha256(input_path.read_bytes()).hexdigest()
    assert [model["id"] for model in payload["models"]] == ["subscription-model"]
    assert payload["models"][0]["context_window_tokens"] == 272_000
    assert payload["models"][0]["reasoning_efforts"] == [
        {"description": "Fast", "id": "low"},
        {"description": "Deep", "id": "high"},
    ]
    serialized = output_path.read_text(encoding="utf-8")
    assert "must-not-persist" not in serialized
    assert "example.test" not in serialized


def test_live_adapter_fills_missing_fields_from_subscription_snapshot(monkeypatch) -> None:
    from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
    from alysis_code.provider_auth.store import ProviderTokenRecord

    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=9_999_999_999,
        account_id="account-123",
        client_id="oaiapp_fixture",
        subject="fixture-subject",
        scopes=("chatgpt.tokens.use.direct", "resource.invoke"),
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: record,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "gpt-5.5",
                        "display_name": "GPT-5.5",
                        "priority": 1,
                    }
                ]
            },
        )

    models = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler)).list_models(
        refresh=True
    )
    model = next(item for item in models if item.id == "gpt-5.5")

    assert model.context_window_tokens == 272_000
    assert model.input_modalities == ("text", "image")
    assert [effort.id for effort in model.reasoning_efforts] == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_live_subscription_fields_remain_authoritative_over_snapshot(monkeypatch) -> None:
    from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
    from alysis_code.provider_auth.store import ProviderTokenRecord

    record = ProviderTokenRecord(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=9_999_999_999,
        account_id="account-123",
        client_id="oaiapp_fixture",
        subject="fixture-subject",
        scopes=("chatgpt.tokens.use.direct", "resource.invoke"),
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token",
        lambda _provider_id: record,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "gpt-5.5",
                        "display_name": "GPT-5.5",
                        "priority": 1,
                        "context_window": 200_000,
                        "input_modalities": ["text"],
                        "default_reasoning_level": "max",
                        "supported_reasoning_levels": [
                            {"effort": "max", "description": "Live maximum"},
                        ],
                    }
                ]
            },
        )

    models = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler)).list_models(
        refresh=True
    )
    model = next(item for item in models if item.id == "gpt-5.5")

    assert model.context_window_tokens == 200_000
    assert model.input_modalities == ("text",)
    assert model.default_reasoning_effort == "max"
    assert [effort.id for effort in model.reasoning_efforts] == ["max"]
