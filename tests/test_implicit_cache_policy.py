from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from alysis_code.cli_impl import config_menu
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.compaction.conversation_compactor import _cache_prefix_compaction_shape
from alysis_code.compaction.settings import CompactionSettings
from alysis_code.config import AppConfig
from alysis_code.llm.cache_capabilities import (
    CacheCapabilitySpec,
    resolve_effective_cache_capability,
)
from alysis_code.llm.cache_policy import resolve_prompt_cache_policy
from alysis_code.llm.factory import make_llm_client
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth import ProviderRequestAuthorization, openai_codex
from alysis_code.provider_diagnostics import build_provider_diagnostics
from alysis_code.provider_telemetry import last_provider_call_summary
from alysis_code.request_estimation import RequestTokenBreakdown
from alysis_code.usage_tracker import usage_context_from_client_response


@pytest.fixture(autouse=True)
def isolated_offline_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))

    def forbidden(*args, **kwargs):
        raise AssertionError("This policy test must not access accounts or the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(openai_codex, "load_provider_token", lambda *_args, **_kwargs: None)
    for name in ("account_status", "authorization_headers", "list_models", "login"):
        monkeypatch.setattr(openai_codex.OpenAICodexSubscriptionAuth, name, forbidden)


def _config(profile: ProfileSpec, *, mode: str = "auto") -> AppConfig:
    cfg = AppConfig(model=profile.default_model, prompt_cache_mode=mode, web_search_mode="off")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def _subscription_profile(*, override: CacheCapabilitySpec | None = None) -> ProfileSpec:
    return ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        auth_provider="openai-codex",
        default_model="gpt-5.6-luna",
        reasoning_effort="high",
        cache_capability=override,
    )


def _prefix_shape(metadata):
    return _cache_prefix_compaction_shape(
        settings=CompactionSettings(cache_aware_compaction=True),
        messages=[
            {"role": "system", "content": "stable instructions"},
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "retained answer " * 1500},
            {"role": "user", "content": "current request"},
            {"role": "assistant", "content": "new output " * 2000},
        ],
        tool_list=[],
        request_breakdown=RequestTokenBreakdown(live_conversation_history_tokens=12000),
        pinned_prefix_len=1,
        cache_policy=metadata,
    )


def test_subscription_factory_preview_and_diagnostics_agree_on_implicit_caching() -> None:
    cfg = _config(_subscription_profile())
    client = make_llm_client(cfg=cfg, api_key="", model=cfg.model)
    preview = config_menu._resolved_cache_policy_for_state(
        config_menu.ConfigMenuState.from_cfg(cfg)
    )
    diagnostics = build_provider_diagnostics(cfg)

    assert preview is not None
    policies = [
        client.prompt_cache_policy_metadata,
        preview.telemetry_metadata(),
        diagnostics.cache_policy.telemetry_metadata(),
    ]
    for policy in policies:
        assert policy["strategy"] == "implicit_provider"
        assert policy["status"] == "enabled"
        assert policy["enabled"] is True
        assert policy["emitted_fields"] == []
        assert policy["emits_request_fields"] is False
        assert policy["trusted_usage_fields"] == ["cache_read_input_tokens"]
        assert _prefix_shape(policy).protected_prefix_message_count == 3
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None


def test_declared_implicit_cache_reaches_compactor_without_request_fields() -> None:
    cfg = _config(
        ProfileSpec(
            name="zai-coding-plan",
            protocol="openai_compat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            default_model="synthetic-model",
        )
    )
    client = make_llm_client(cfg=cfg, api_key="synthetic-key", model=cfg.model)

    metadata = client.prompt_cache_policy_metadata
    assert metadata["strategy"] == "implicit_provider"
    assert metadata["emitted_fields"] == []
    assert metadata["status"] == "enabled"
    assert _prefix_shape(metadata).protected_prefix_message_count == 3


@pytest.mark.parametrize("mode,disabled", [("off", False), ("auto", True), ("manual", True)])
def test_subscription_cache_disable_precedence_in_all_policy_surfaces(mode, disabled) -> None:
    override = CacheCapabilitySpec(enabled=False) if disabled else None
    cfg = _config(_subscription_profile(override=override), mode=mode)
    client = make_llm_client(cfg=cfg, api_key="", model=cfg.model)
    preview = config_menu._resolved_cache_policy_for_state(
        config_menu.ConfigMenuState.from_cfg(cfg)
    )
    assert preview is not None
    policies = [
        client.prompt_cache_policy_metadata,
        preview.telemetry_metadata(),
        build_provider_diagnostics(cfg).cache_policy.telemetry_metadata(),
    ]
    for policy in policies:
        assert policy["enabled"] is False
        assert policy["implicit_cache_enabled"] is False
        assert policy["emitted_fields"] == []
        assert _prefix_shape(policy).protected_prefix_message_count == 1


@pytest.mark.parametrize("strategy", ["implicit_provider", "gemini_implicit"])
@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_implicit_policy_uses_declared_capability_for_unknown_provider(strategy, mode) -> None:
    capability = resolve_effective_cache_capability(
        provider_key="unlisted-provider",
        protocol="openai_responses",
        model="unlisted-model",
        transport_capabilities=None,
        auth_cache_capability=CacheCapabilitySpec(
            strategy=strategy,
            enabled=True,
            reports_cache_read_tokens=True,
            emits_request_fields=False,
        ),
    )
    policy = resolve_prompt_cache_policy(
        cfg=AppConfig(prompt_cache_mode=mode),
        capabilities=None,
        provider_key=capability.provider_key,
        protocol=capability.protocol,
        model=capability.model,
        prompt_cache_key="not-a-supported-field",
        prompt_cache_retention="24h",
        cache_capability=capability,
    )

    assert capability.source == "auth_adapter"
    assert policy.status == "enabled"
    assert policy.implicit_cache_enabled is True
    assert policy.request_field_values == ()
    assert policy.prompt_cache_key is None
    assert policy.prompt_cache_retention is None


def test_profile_override_keeps_precedence_over_auth_and_preset() -> None:
    capability = resolve_effective_cache_capability(
        provider_key="unlisted-provider",
        protocol="openai_responses",
        model="unlisted-model",
        transport_capabilities=None,
        auth_cache_capability=CacheCapabilitySpec(strategy="implicit_provider", enabled=True),
        preset_cache_capability=CacheCapabilitySpec(enabled=True),
        profile_cache_capability=CacheCapabilitySpec(enabled=False),
    )
    assert capability.enabled is False
    assert capability.source == "profile"
    assert capability.emitted_fields == ()


@pytest.mark.parametrize(
    "profile,mode",
    [
        (
            ProfileSpec(
                name="qwen-intl",
                protocol="openai_compat",
                base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                default_model="synthetic-model",
            ),
            "auto",
        ),
        (
            ProfileSpec(
                name="gemini-native",
                protocol="gemini_generate_content",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                default_model="synthetic-model",
            ),
            "manual",
        ),
    ],
)
def test_explicit_cache_availability_does_not_activate_prefix_protection(profile, mode) -> None:
    cfg = _config(profile, mode=mode)
    client = make_llm_client(cfg=cfg, api_key="synthetic-key", model=cfg.model)
    metadata = client.prompt_cache_policy_metadata

    assert metadata["status"] == "available"
    assert metadata["enabled"] is False
    assert metadata["implicit_cache_enabled"] is False
    assert metadata["emitted_fields"] == []
    assert _prefix_shape(metadata).protected_prefix_message_count == 1


def test_subscription_config_reload_preserves_cache_policy_and_route(monkeypatch, tmp_path) -> None:
    cfg = _config(_subscription_profile())
    client = make_llm_client(cfg=cfg, api_key="", model=cfg.model)
    route = client.route_identity
    policy = client.prompt_cache_policy_metadata.copy()
    session = SimpleNamespace(
        cfg=cfg,
        client=client,
        router_client=None,
        conversation_compactor=None,
        mode="review",
        root=tmp_path,
    )
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(chat_loop, name, lambda *args, **kwargs: None, raising=False)

    cfg.max_steps += 1
    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)

    assert client.prompt_cache_policy_metadata == policy
    assert client.route_identity == route
    assert client.prompt_cache_policy_metadata["enabled"] is True


def test_subscription_prepared_request_emits_no_cache_fields_and_keeps_reported_hits(
    monkeypatch,
) -> None:
    captured = []

    def fake_send(_client, request, **kwargs):
        captured.append(json.loads(request.content))
        completed = {
            "type": "response.completed",
            "response": {
                "id": "synthetic-response",
                "model": "gpt-5.6-luna",
                "status": "completed",
                "output_text": "Done.",
                "output": [],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 800},
                },
            },
        }
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            content=("event: response.completed\ndata: " + json.dumps(completed) + "\n\n").encode(),
        )

    monkeypatch.setattr(httpx.Client, "send", fake_send)
    monkeypatch.setattr(
        openai_codex.OpenAICodexSubscriptionAuth,
        "route_credential_scope",
        lambda self: "offline-account-scope",
    )
    monkeypatch.setattr(
        openai_codex.OpenAICodexSubscriptionAuth,
        "authorize_request",
        lambda self, url, *, expected_credential_scope, **kwargs: ProviderRequestAuthorization(
            credential_scope=expected_credential_scope, headers={}
        ),
    )
    cfg = _config(_subscription_profile())
    client = make_llm_client(
        cfg=cfg,
        api_key="",
        model=cfg.model,
        prompt_cache_key="synthetic-key",
        prompt_cache_retention="24h",
    )
    response = client.chat(messages=[{"role": "user", "content": "synthetic request"}])

    assert len(captured) == 1
    assert (
        not {"prompt_cache_key", "prompt_cache_retention", "cache_control", "cached_content"}
        & captured[0].keys()
    )
    assert response.usage is not None
    assert response.usage.cache_read_input_tokens == 800
    assert client.prompt_cache_policy_metadata["emitted_fields"] == []
    _assert_request_strategy_preserved(client, response)


def _assert_request_strategy_preserved(client, response) -> None:
    expected = client.prompt_cache_policy_metadata["strategy"]
    usage_context = usage_context_from_client_response(
        client=client, response=response, operation="main_llm"
    )
    assert usage_context["cache_strategy"] == expected
    assert usage_context["request_plan"]["cache_strategy"] == expected
    telemetry = last_provider_call_summary()
    assert telemetry["cache_policy"]["strategy"] == expected
    assert telemetry["cache_policy"]["enabled"] is True
    assert telemetry["cache_policy"]["emitted_fields"] == []
    assert telemetry["request_plan"]["cache_strategy"] == expected


def test_compatible_implicit_cache_survives_actual_request_telemetry_and_usage(monkeypatch) -> None:
    captured = []

    def fake_send(_client, request, **kwargs):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "synthetic-response",
                "model": "synthetic-model",
                "choices": [
                    {"message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 800},
                },
            },
        )

    monkeypatch.setattr(httpx.Client, "send", fake_send)
    cfg = _config(
        ProfileSpec(
            name="zai-coding-plan",
            protocol="openai_compat",
            base_url="https://api.z.ai/api/coding/paas/v4",
            default_model="synthetic-model",
        )
    )
    client = make_llm_client(cfg=cfg, api_key="synthetic-key", model=cfg.model)
    response = client.chat(messages=[{"role": "user", "content": "synthetic request"}])

    assert len(captured) == 1
    assert (
        not {"prompt_cache_key", "prompt_cache_retention", "cache_control", "cached_content"}
        & captured[0].keys()
    )
    assert response.usage is not None
    assert response.usage.cache_read_input_tokens == 800
    _assert_request_strategy_preserved(client, response)
