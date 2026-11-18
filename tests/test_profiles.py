from __future__ import annotations

import os
from pathlib import Path

import pytest

from alysis_code.config import AppConfig, ConfigError, load_config, save_config
from alysis_code.llm.cache_capabilities import (
    CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    OPENROUTER_SESSION_ID_FIELD,
    CacheCapabilitySpec,
)
from alysis_code.llm.protocols import SUPPORTED_LLM_PROTOCOLS
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    connection_fingerprint,
    get_active_profile,
    list_profiles,
    remove_profile,
    set_active_profile,
    sync_active_profile_to_config,
    update_active_profile_defaults,
    update_profile,
)


def test_profile_spec_roundtrip_to_dict() -> None:
    profile = ProfileSpec(
        name="anthropic",
        base_url="https://api.anthropic.com/v1/openai",
        api_key_env="ANTHROPIC_API_KEY",
        extra_headers={"anthropic-version": "2023-06-01"},
        default_model="claude-sonnet-4-6",
        web_search_adapter="anthropic_messages",
        web_search_model="claude-sonnet-4-6",
        notes="compat",
    )

    assert ProfileSpec.from_dict("anthropic", profile.to_dict()) == profile


def test_profile_extra_headers_are_canonical_and_case_duplicates_are_rejected() -> None:
    profile = ProfileSpec(
        name="gateway",
        base_url="https://gateway.example/v1",
        extra_headers={" X-Tenant-ID ": "  tenant-a  "},
    )

    assert profile.extra_headers == {"x-tenant-id": "tenant-a"}
    with pytest.raises(ConfigError, match="Duplicate extra header name"):
        ProfileSpec(
            name="duplicate",
            base_url="https://gateway.example/v1",
            extra_headers={"X-Tenant-ID": "tenant-a", "x-tenant-id": "tenant-b"},
        )


def test_profile_spec_roundtrip_preserves_cache_capability_override() -> None:
    profile = ProfileSpec(
        name="custom",
        base_url="https://gateway.example/v1",
        cache_capability=CacheCapabilitySpec(
            strategy=CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
            enabled=True,
            supports_prompt_cache_key=True,
            reports_cache_read_tokens=True,
            request_fields=(OPENROUTER_SESSION_ID_FIELD,),
        ),
    )

    data = profile.to_dict()
    loaded = ProfileSpec.from_dict("custom", data)

    assert data["cache_capability"]["strategy"] == CACHE_STRATEGY_OPENAI_PROMPT_CACHE
    assert data["cache_capability"]["request_fields"] == [OPENROUTER_SESSION_ID_FIELD]
    assert loaded.cache_capability is not None
    assert loaded.cache_capability.to_dict() == profile.cache_capability.to_dict()


def test_profile_spec_roundtrip_preserves_reasoning_trace_adapter_override() -> None:
    profile = ProfileSpec(
        name="custom",
        base_url="https://gateway.example/v1",
        reasoning_trace_adapter="openrouter_reasoning",
    )

    data = profile.to_dict()
    loaded = ProfileSpec.from_dict("custom", data)

    assert data["reasoning_trace_adapter"] == "openrouter_reasoning"
    assert loaded.reasoning_trace_adapter == "openrouter_reasoning"


def test_profile_spec_omits_auto_reasoning_trace_adapter_and_rejects_unknown() -> None:
    profile = ProfileSpec(name="custom", base_url="https://gateway.example/v1")
    assert "reasoning_trace_adapter" not in profile.to_dict()

    with pytest.raises(ConfigError, match="reasoning_trace_adapter"):
        ProfileSpec.from_dict(
            "custom",
            {
                "base_url": "https://gateway.example/v1",
                "reasoning_trace_adapter": "guess_every_field",
            },
        )

    with pytest.raises(ConfigError, match="not valid for protocol"):
        ProfileSpec(
            name="custom",
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            reasoning_trace_adapter="openrouter_reasoning",
        )


def test_profile_cache_capability_rejects_unknown_strategy() -> None:
    with pytest.raises(ConfigError, match="cache strategy"):
        ProfileSpec.from_dict(
            "custom",
            {
                "base_url": "https://gateway.example/v1",
                "cache_capability": {"strategy": "magic_cache"},
            },
        )


def test_profile_name_validation_rejects_invalid_chars() -> None:
    with pytest.raises(ConfigError):
        ProfileSpec(name="Bad Name", base_url="https://example.com/v1")


def test_protocol_validation_rejects_unknown() -> None:
    with pytest.raises(ConfigError):
        ProfileSpec(name="test", protocol="anthropic_native", base_url="https://example.com/v1")


@pytest.mark.parametrize("protocol", sorted(SUPPORTED_LLM_PROTOCOLS))
def test_profile_spec_accepts_supported_llm_protocols(protocol: str) -> None:
    profile = ProfileSpec(name="test", protocol=protocol, base_url="https://example.com/v1")

    assert profile.protocol == protocol


def test_profile_from_dict_defaults_missing_protocol_to_openai_compat() -> None:
    profile = ProfileSpec.from_dict(
        "legacy",
        {
            "base_url": "https://example.com/v1",
            "default_model": "model",
        },
    )

    assert profile.protocol == "openai_compat"


def test_base_url_validation_rejects_malformed_url() -> None:
    with pytest.raises(ConfigError, match="valid http:// or https:// URL"):
        ProfileSpec(name="bad", base_url="https://api.deepseek.com]")


def test_web_search_adapter_validation_rejects_unknown() -> None:
    with pytest.raises(ConfigError):
        ProfileSpec(name="test", web_search_adapter="unknown")


def test_add_profile_persists_in_extra_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    add_profile(cfg, ProfileSpec(name="test", base_url="https://example.com/v1"))
    save_config(cfg)

    profiles = {profile.name: profile for profile in list_profiles(load_config())}
    assert profiles["test"].base_url == "https://example.com/v1"
    assert profiles["test"].web_search_adapter == "auto"


def test_update_active_profile_defaults_preserves_web_search_fields() -> None:
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-sonnet-4-6",
            web_search_adapter="anthropic_messages",
            web_search_model="claude-sonnet-4-6",
        ),
    )
    set_active_profile(cfg, "anthropic")

    changed = update_active_profile_defaults(cfg, default_model="claude-opus-4-7")

    assert changed is True
    profile = get_active_profile(cfg)
    assert profile.default_model == "claude-opus-4-7"
    assert profile.web_search_adapter == "anthropic_messages"
    assert profile.web_search_model == "claude-sonnet-4-6"


def test_remove_active_profile_switches_to_first_remaining() -> None:
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="b", base_url="https://b.example/v1"))
    add_profile(cfg, ProfileSpec(name="a", base_url="https://a.example/v1"))
    set_active_profile(cfg, "b")

    remove_profile(cfg, "b")

    assert get_active_profile(cfg).name == "a"


def test_switching_active_profile_clears_only_router_overrides() -> None:
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="gemini", base_url="https://gemini.example/v1"))
    add_profile(cfg, ProfileSpec(name="anthropic", base_url="https://anthropic.example/v1"))
    set_active_profile(cfg, "gemini")
    cfg.extra_fields["role_models"] = {
        "router": "gemini-router",
        "coding": "shared-coder",
    }

    set_active_profile(cfg, "anthropic")

    assert cfg.extra_fields["role_models"] == {"coding": "shared-coder"}


def test_remove_only_profile_clears_active() -> None:
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="only", base_url="https://example.com/v1"))
    set_active_profile(cfg, "only")

    remove_profile(cfg, "only")

    assert cfg.extra_fields.get("active_profile") is None


def test_get_active_profile_raises_if_none_configured() -> None:
    cfg = AppConfig()
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}

    with pytest.raises(ConfigError):
        get_active_profile(cfg)


def test_sync_active_profile_to_config_repairs_stale_top_level_base_url() -> None:
    cfg = AppConfig(model="DeepSeek-V4-Flash", base_url="https://api.deepseek.com]")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="deepseek",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ),
    )
    set_active_profile(cfg, "deepseek")
    cfg.base_url = "https://api.deepseek.com]"

    changed = sync_active_profile_to_config(cfg)

    assert changed is True
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-v4-flash"


def test_subscription_model_and_effort_change_connection_fingerprint() -> None:
    cfg = AppConfig(model="gpt-codex-a", llm_reasoning_effort="high")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol="openai_responses",
            base_url="https://chatgpt.com/backend-api/codex",
            auth_provider="openai-codex",
            default_model="gpt-codex-a",
            reasoning_effort="high",
        ),
    )
    set_active_profile(cfg, "chatgpt-codex")
    original = connection_fingerprint(cfg)

    update_profile(
        cfg,
        "chatgpt-codex",
        default_model="gpt-codex-b",
        reasoning_effort="high",
        allow_subscription_selection=True,
    )
    model_changed = connection_fingerprint(cfg)

    update_profile(
        cfg,
        "chatgpt-codex",
        default_model="gpt-codex-b",
        reasoning_effort="xhigh",
        allow_subscription_selection=True,
    )
    effort_changed = connection_fingerprint(cfg)

    assert model_changed != original
    assert effort_changed != model_changed


def test_api_profile_model_change_does_not_change_connection_fingerprint() -> None:
    cfg = AppConfig(model="gpt-a")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-a",
        ),
    )
    set_active_profile(cfg, "openai")
    original = connection_fingerprint(cfg)

    update_profile(cfg, "openai", default_model="gpt-b")

    assert connection_fingerprint(cfg) == original


def test_reasoning_trace_adapter_change_rebuilds_connection() -> None:
    cfg = AppConfig(model="custom-model")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="custom",
            base_url="https://gateway.example/v1",
            default_model="custom-model",
        ),
    )
    set_active_profile(cfg, "custom")
    original = connection_fingerprint(cfg)

    update_profile(cfg, "custom", reasoning_trace_adapter="openrouter_reasoning")

    assert connection_fingerprint(cfg) != original


def test_update_active_profile_defaults_persists_model_and_base_url() -> None:
    cfg = AppConfig(model="old", base_url="https://old.example/v1")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="deepseek", base_url="https://api.deepseek.com"))
    set_active_profile(cfg, "deepseek")

    assert update_active_profile_defaults(
        cfg,
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
    )

    profile = get_active_profile(cfg)
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-v4-flash"
    assert profile.default_model == "deepseek-v4-flash"
