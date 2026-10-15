from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.cli_impl.config_menu import ConfigMenuState, _resolved_cache_policy_for_state
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig, config_path, load_config, save_config, set_config_value
from alysis_code.llm.factory import make_llm_client
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile, sync_active_profile_to_config
from alysis_code.provider_diagnostics import build_provider_diagnostics

MODELS = ("deepseek-flash", "glm-5.3-flash", "gpt-6-luna", "claude-sonnet-5-5")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("alysis_code.config.load_persisted_profile_keys", lambda: {})
    for prefix in ("ALYSIS", "ALYSIS"):
        for key in (
            "PROMPT_CACHE_MODE",
            "PROMPT_CACHE_KEY",
            "PROMPT_CACHE_RETENTION",
            "ANTHROPIC_PROMPT_CACHE_ENABLED",
            "ANTHROPIC_PROMPT_CACHE_TTL",
        ):
            monkeypatch.delenv(f"{prefix}_{key}", raising=False)
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda *args: pytest.fail("Use MockTransport; these tests must stay offline."),
    )


def hosted_profile(model="claude-sonnet-5-5"):
    return replace(make_profile_from_preset(get_preset("alysis")), default_model=model)


def legacy_config(**overrides):
    profile = hosted_profile()
    # Match old save_config: defaults were serialized, including Manual/false.
    raw = AppConfig(model=profile.default_model, base_url=profile.base_url).model_dump()
    raw.pop("hosted_prompt_cache_defaults_applied")
    raw.update(profiles={profile.name: profile.to_dict()}, active_profile=profile.name)
    raw.update(overrides)
    config_path().write_text(json.dumps(raw), encoding="utf-8")
    return raw


@pytest.mark.parametrize("model", MODELS)
def test_new_hosted_connection_defaults_to_auto(model):
    cfg = AppConfig()
    profile = hosted_profile(model)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    assert cfg.prompt_cache_mode == "auto"
    assert cfg.anthropic_prompt_cache_ttl == "5m"
    assert cfg.hosted_prompt_cache_defaults_applied


@pytest.mark.parametrize("missing_mode", [False, True])
def test_legacy_hosted_defaults_migrate_without_writing_during_load(missing_mode):
    raw = legacy_config(unknown_setting={"preserve": True})
    if missing_mode:
        raw.pop("prompt_cache_mode")
        config_path().write_text(json.dumps(raw), encoding="utf-8")
    original = config_path().read_bytes()
    cfg = load_config()
    assert cfg.prompt_cache_mode == "auto"
    assert cfg.hosted_prompt_cache_defaults_applied
    assert config_path().read_bytes() == original
    save_config(cfg)
    assert load_config().prompt_cache_mode == "auto"
    assert load_config().extra_fields["unknown_setting"] == {"preserve": True}


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt_cache_mode": "off"},
        {"prompt_cache_mode": "auto"},
        {"prompt_cache_key": "chosen-key"},
        {"prompt_cache_retention": "24h"},
        {"anthropic_prompt_cache_enabled": True},
        {"anthropic_prompt_cache_ttl": "1h"},
        {"cache": {"prompt_cache_key_enabled": False}},
        {"cache": {"keepalive_enabled": True}},
        {"hosted_prompt_cache_defaults_applied": True},
    ],
)
def test_migration_preserves_off_and_customized_cache_settings(overrides):
    raw = legacy_config(**overrides)
    cfg = load_config()
    assert cfg.prompt_cache_mode == raw["prompt_cache_mode"]
    for key, expected in overrides.items():
        if key != "cache":
            assert getattr(cfg, key) == expected
    save_config(cfg)
    assert load_config().prompt_cache_mode == raw["prompt_cache_mode"]


def test_profile_cache_override_is_preserved():
    raw = legacy_config()
    raw["profiles"]["alysis"]["cache_capability"] = {"enabled": False}
    config_path().write_text(json.dumps(raw), encoding="utf-8")
    cfg = load_config()
    assert cfg.prompt_cache_mode == "manual"
    client = make_llm_client(cfg=cfg, api_key="slk_fake", model=cfg.model)
    assert client.prompt_cache_control_enabled is False


@pytest.mark.parametrize("mode", ["manual", "off", "auto"])
def test_explicit_cli_choice_survives_first_hosted_login_and_reload(mode):
    cfg = set_config_value(AppConfig(), "prompt_cache_mode", mode)
    profile = hosted_profile()
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    save_config(cfg)
    assert load_config().prompt_cache_mode == mode


def test_manual_selected_after_migration_is_not_migrated_again():
    legacy_config()
    cfg = load_config()
    flow = ConfigFlow(cfg=cfg)
    flow.choose("cache")
    flow.choose("manual")
    assert flow.state.commit_to(cfg).saved
    save_config(cfg)
    cfg = load_config()
    assert cfg.prompt_cache_mode == "manual"
    assert sync_active_profile_to_config(cfg) is False
    assert (
        make_llm_client(cfg=cfg, api_key="slk_fake", model=cfg.model).prompt_cache_control_enabled
        is False
    )


def test_tui_reselecting_manual_before_hosted_login_records_explicit_choice():
    cfg = AppConfig(model="gpt-test")
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("prompt_cache_mode", "manual")
    assert state.dirty
    profile = hosted_profile()
    state.add_profile_spec(profile)
    assert state.fields["prompt_cache_mode"] == "manual"
    assert state.commit_to(cfg).saved
    save_config(cfg)
    assert load_config().prompt_cache_mode == "manual"


def test_tui_hosted_profile_switch_previews_and_saves_auto():
    cfg = AppConfig(model="gpt-test")
    state = ConfigMenuState.from_cfg(cfg)
    state.add_profile_spec(hosted_profile())
    assert state.fields["prompt_cache_mode"] == "auto"
    assert _resolved_cache_policy_for_state(state).anthropic_cache_control_enabled
    assert state.commit_to(cfg).saved
    save_config(cfg)
    assert load_config().prompt_cache_mode == "auto"


def test_other_provider_defaults_are_unchanged():
    cfg = AppConfig()
    profile = make_profile_from_preset(get_preset("anthropic-native"))
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    save_config(cfg)
    cfg = load_config()
    assert cfg.prompt_cache_mode == "manual"
    assert not cfg.hosted_prompt_cache_defaults_applied


@pytest.mark.parametrize("model", MODELS)
def test_hosted_auto_wire_fields_and_policy_surfaces_agree(model):
    legacy_config()
    cfg = load_config()
    profile = hosted_profile(model)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        if model == "claude-sonnet-5-5":
            body = {"type": "message", "content": [{"type": "text", "text": "OK"}]}
        elif model == "gpt-6-luna":
            body = {
                "id": "resp_test",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
            }
        else:
            body = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        return httpx.Response(200, json=body)

    client = make_llm_client(
        cfg=cfg,
        api_key="slk_fake",
        model=model,
        prompt_cache_namespace="session-test",
        transport=httpx.MockTransport(respond),
    )
    client.chat(
        messages=[
            {"role": "system", "content": "Stable instructions."},
            {"role": "user", "content": "hello"},
        ]
    )
    policies = [
        client.prompt_cache_policy_metadata,
        _resolved_cache_policy_for_state(ConfigMenuState.from_cfg(cfg)).telemetry_metadata(),
        build_provider_diagnostics(cfg).cache_policy.telemetry_metadata(),
    ]
    for policy in policies:
        assert policy["status"] == "enabled"
        assert policy["mode"] == "automatic"
        assert policy["strategy"] == (
            "anthropic_cache_control" if model == "claude-sonnet-5-5" else "implicit_provider"
        )
    payload = captured[0]
    assert "prompt_cache_key" not in payload
    assert "prompt_cache_retention" not in payload
    if model == "claude-sonnet-5-5":
        assert payload["cache_control"] == {"type": "ephemeral"}
    else:
        assert "cache_control" not in payload


def test_environment_off_wins_over_migrated_auto(monkeypatch):
    legacy_config()
    monkeypatch.setenv("ALYSIS_PROMPT_CACHE_MODE", "off")
    cfg = load_config()
    assert cfg.prompt_cache_mode == "auto"
    client = make_llm_client(cfg=cfg, api_key="slk_fake", model=cfg.model)
    assert client.prompt_cache_control_enabled is False
    assert client.prompt_cache_policy_metadata["status"] == "disabled"


def test_hosted_sonnet_uses_supported_five_minute_ttl():
    legacy_config(prompt_cache_mode="auto", anthropic_prompt_cache_ttl="1h")
    cfg = load_config()
    client = make_llm_client(cfg=cfg, api_key="slk_fake", model=cfg.model)
    assert cfg.anthropic_prompt_cache_ttl == "1h"  # Preserve preference for other providers.
    assert client.prompt_cache_control_ttl == "5m"
    assert client.prompt_cache_policy_metadata["warnings"]
