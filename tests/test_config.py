from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import alysis_code.atomic_io as atomic_io_mod
from alysis_code.config import (
    DEFAULT_SUBAGENT_TIMEOUT_S,
    DEFAULT_VERIFY_COMMANDS,
    AppConfig,
    ConfigError,
    clear_persisted_api_key,
    clone_cfg,
    config_path,
    config_transaction,
    credentials_path,
    get_api_key,
    is_generic_configured_verify_preset,
    is_generic_verify_command_fallback,
    load_config,
    load_persisted_api_key,
    normalize_verify_command_list,
    resolve_anthropic_prompt_cache_enabled,
    resolve_anthropic_prompt_cache_ttl,
    resolve_api_key,
    resolve_crash_diagnostic_log_path,
    resolve_feedback_github_enabled,
    resolve_feedback_github_repo,
    resolve_feedback_open_browser,
    resolve_llm_enable_thinking,
    resolve_llm_reasoning_effort,
    resolve_llm_timeout_s,
    resolve_model_metadata_policy,
    resolve_prompt_cache_key,
    resolve_prompt_cache_mode,
    resolve_prompt_cache_retention,
    resolve_run_deadline,
    resolve_run_deadline_seconds,
    resolve_web_search_adapter,
    resolve_web_search_base_url,
    resolve_web_search_mode,
    resolve_web_search_model,
    resolve_web_search_policy,
    resolve_web_search_timeout_s,
    save_config,
    save_persisted_api_key,
    save_persisted_profile_key,
    set_config_value,
)
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    get_active_profile,
    get_profile,
    resolve_effective_base_url,
    set_active_profile,
)
from alysis_code.step_budget import (
    DEFAULT_CHAT_MAX_STEPS,
    DEFAULT_SUBAGENT_MAX_STEPS,
    DEFAULT_TASK_MAX_STEPS,
)


def test_get_api_key_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        get_api_key()


def test_get_api_key_uses_persisted_key_when_env_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    save_persisted_api_key("stored-key")

    assert load_persisted_api_key() == "stored-key"
    assert get_api_key() == "stored-key"
    resolved = resolve_api_key()
    assert resolved.key == "stored-key"
    assert resolved.source == "stored:legacy"
    assert credentials_path().name == "credentials.json"


def test_get_api_key_prefers_alysis_env_over_persisted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "env-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    save_persisted_api_key("stored-key")

    resolved = resolve_api_key()
    assert resolved.key == "env-key"
    assert resolved.source == "env:ALYSIS_API_KEY"
    assert get_api_key() == "env-key"


def test_get_api_key_prefers_persisted_key_over_openai_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
    save_persisted_api_key("stored-key")

    resolved = resolve_api_key()
    assert resolved.key == "stored-key"
    assert resolved.source == "stored:legacy"
    assert get_api_key() == "stored-key"


def test_non_openai_profile_prefers_profile_env_over_alysis_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    cfg = AppConfig(model="claude-sonnet-4-6")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1/",
            api_key_env="ANTHROPIC_API_KEY",
        ),
    )
    set_active_profile(cfg, "anthropic")
    save_config(cfg)

    resolved = resolve_api_key()

    assert resolved.key == "sk-ant"
    assert resolved.source == "env:ANTHROPIC_API_KEY"


def test_non_openai_profile_prefers_stored_profile_key_over_alysis_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-openai")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = AppConfig(model="claude-sonnet-4-6")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1/",
            api_key_env="ANTHROPIC_API_KEY",
        ),
    )
    set_active_profile(cfg, "anthropic")
    save_config(cfg)
    save_persisted_profile_key("anthropic", "sk-ant-stored")

    resolved = resolve_api_key()

    assert resolved.key == "sk-ant-stored"
    assert resolved.source == "stored:profile=anthropic"


def test_stored_profile_key_overrides_provider_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-env")
    cfg = AppConfig(model="deepseek-v4-flash")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="deepseek",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    )
    set_active_profile(cfg, "deepseek")
    save_config(cfg)
    save_persisted_profile_key("deepseek", "sk-deepseek-config")

    resolved = resolve_api_key()

    assert resolved.key == "sk-deepseek-config"
    assert resolved.source == "stored:profile=deepseek"


def test_non_openai_profile_does_not_use_generic_alysis_env_without_profile_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-openai-generic")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = AppConfig(model="deepseek-v4-flash")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="deepseek",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    )
    set_active_profile(cfg, "deepseek")
    save_config(cfg)

    resolved = resolve_api_key()

    assert resolved.key is None
    assert resolved.source == "missing"


def test_stored_openai_profile_key_overrides_generic_alysis_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-generic")
    cfg = load_config()
    save_config(cfg)
    save_persisted_profile_key("default", "sk-openai-config")

    resolved = resolve_api_key(load_config())

    assert resolved.key == "sk-openai-config"
    assert resolved.source == "stored:profile=default"


def test_default_profile_custom_base_url_prefers_stored_profile_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.setenv("ALYSIS_API_KEY", "sk-generic")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = load_config()
    set_config_value(cfg, "base_url", "https://custom.example/v1")
    save_config(cfg)
    save_persisted_profile_key("default", "sk-profile")

    resolved = resolve_api_key(load_config())

    assert resolved.key == "sk-profile"
    assert resolved.source == "stored:profile=default"


def test_default_profile_custom_base_url_does_not_fall_back_to_openai_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    cfg = load_config()
    set_config_value(cfg, "base_url", "https://custom.example/v1")
    save_config(cfg)

    resolved = resolve_api_key(load_config())

    assert resolved.key is None
    assert resolved.source == "missing"
    with pytest.raises(ConfigError) as exc_info:
        get_api_key(load_config())
    assert "OPENAI_API_KEY" not in str(exc_info.value)


def test_set_model_and_base_url_syncs_active_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()

    set_config_value(cfg, "base_url", "https://api.anthropic.com/v1")
    set_config_value(cfg, "model", "claude-sonnet-4-6")
    save_config(cfg)

    loaded = load_config()
    profile = get_active_profile(loaded)
    assert loaded.base_url == "https://api.anthropic.com/v1"
    assert loaded.model == "claude-sonnet-5"
    assert profile.base_url == "https://api.anthropic.com/v1"
    assert profile.default_model == "claude-sonnet-5"


def test_set_model_alias_switches_to_matching_provider_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1/",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-opus-4-7",
        ),
    )
    set_active_profile(cfg, "anthropic")

    set_config_value(cfg, "model", "gpt-5-6-terra")

    profile = get_active_profile(cfg)
    assert profile.name == "default"
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.model == "gpt-5.6-terra"
    assert profile.default_model == "gpt-5.6-terra"


def test_set_model_alias_keeps_active_gateway_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    set_config_value(cfg, "base_url", "https://openrouter.ai/api/v1")

    set_config_value(cfg, "model", "gpt-5-6-terra")

    profile = get_active_profile(cfg)
    assert profile.name == "openrouter"
    assert cfg.base_url == "https://openrouter.ai/api/v1"
    assert cfg.model == "openai/gpt-5.6-terra"
    assert profile.default_model == "openai/gpt-5.6-terra"


def test_set_model_canonicalizes_active_provider_numeric_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1/",
            api_key_env="ANTHROPIC_API_KEY",
        ),
    )
    set_active_profile(cfg, "anthropic")

    set_config_value(cfg, "model", "claude-opus-4.7")

    profile = get_active_profile(cfg)
    assert profile.name == "anthropic"
    assert cfg.model == "claude-opus-4-7"
    assert profile.default_model == "claude-opus-4-7"


def test_set_model_canonicalizes_legacy_numeric_separator_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    set_config_value(cfg, "base_url", "https://api.mistral.ai/v1")

    set_config_value(cfg, "model", "mistral-medium-3.5")

    profile = get_active_profile(cfg)
    assert profile.name == "mistral"
    assert cfg.model == "mistral-medium-3-5"
    assert profile.default_model == "mistral-medium-3-5"


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("gemini-3.1-preview", "gemini-3.1-pro-preview"),
        ("gemini-3-pro-preview", "gemini-3.1-pro-preview"),
        ("gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite"),
    ],
)
def test_set_model_canonicalizes_gemini_stale_preview_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str, expected: str
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    set_config_value(cfg, "base_url", "https://generativelanguage.googleapis.com/v1beta/openai/")

    set_config_value(cfg, "model", alias)

    profile = get_active_profile(cfg)
    assert profile.name == "gemini-compat"
    assert profile.protocol == "openai_compat"
    assert cfg.model == expected
    assert profile.default_model == expected


def test_set_known_base_url_switches_provider_profile_and_default_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1/",
            api_key_env="ANTHROPIC_API_KEY",
            default_model="claude-opus-4-7",
        ),
    )
    set_active_profile(cfg, "anthropic")

    set_config_value(cfg, "base_url", "https://api.deepseek.com")

    profile = get_active_profile(cfg)
    assert profile.name == "deepseek"
    assert profile.api_key_env == "DEEPSEEK_API_KEY"
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-flash"
    assert profile.default_model == "deepseek-flash"


def test_set_base_url_rejects_malformed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()

    with pytest.raises(ConfigError):
        set_config_value(cfg, "base_url", "not-a-url")


def test_load_config_syncs_active_profile_over_stale_top_level_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "base_url": "https://api.deepseek.com]",
                "model": "DeepSeek-V4-Flash",
                "active_profile": "deepseek",
                "profiles": {
                    "deepseek": {
                        "protocol": "openai_compat",
                        "base_url": "https://api.deepseek.com",
                        "extra_headers": {},
                        "default_model": "",
                        "web_search_adapter": "auto",
                        "web_search_model": "",
                        "notes": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-flash"
    profile = get_active_profile(cfg)
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.default_model == "deepseek-flash"


def test_load_config_repairs_known_model_provider_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "base_url": "https://api.anthropic.com/v1/",
                "model": "gpt-5-6-terra",
                "active_profile": "anthropic",
                "profiles": {
                    "anthropic": {
                        "protocol": "openai_compat",
                        "base_url": "https://api.anthropic.com/v1/",
                        "api_key_env": "ANTHROPIC_API_KEY",
                        "extra_headers": {},
                        "default_model": "gpt-5-6-terra",
                        "web_search_adapter": "auto",
                        "web_search_model": "",
                        "notes": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.extra_fields["active_profile"] == "openai"
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.model == "gpt-5.6-terra"
    profile = get_profile(cfg, "openai")
    assert profile is not None
    assert profile.default_model == "gpt-5.6-terra"


def test_load_config_keeps_explicit_custom_profile_with_preset_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    direct_base_url = "https://token-plan-ams.xiaomimimo.com/v1"
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "base_url": direct_base_url,
                "model": "mimo-v2.5-pro",
                "active_profile": "mimo",
                "profiles": {
                    "alysis": {
                        "protocol": "openai_compat",
                        "base_url": ("https://api.alysiscode.com/v1"),
                        "extra_headers": {},
                        "default_model": "mimo-v2.5-pro",
                        "web_search_adapter": "openrouter",
                        "web_search_model": "",
                        "notes": "Hosted MiMo trial.",
                    },
                    "mimo": {
                        "protocol": "openai_compat",
                        "base_url": direct_base_url,
                        "extra_headers": {},
                        "default_model": "mimo-v2.5-pro",
                        "web_search_adapter": "auto",
                        "web_search_model": "",
                        "notes": "Direct Xiaomi endpoint.",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.extra_fields["active_profile"] == "mimo"
    assert cfg.model == "mimo-v2.5-pro"
    profile = get_active_profile(cfg)
    assert profile.name == "mimo"
    assert resolve_effective_base_url(cfg=cfg, profile=profile) == direct_base_url


def test_load_config_preset_active_profile_model_autoalign_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "base_url": "https://api.openai.com/v1",
                "model": "claude-sonnet-4-6",
                "active_profile": "openai",
                "profiles": {
                    "openai": {
                        "protocol": "openai_compat",
                        "base_url": "https://api.openai.com/v1",
                        "api_key_env": "OPENAI_API_KEY",
                        "extra_headers": {},
                        "default_model": "claude-sonnet-4-6",
                        "web_search_adapter": "auto",
                        "web_search_model": "",
                        "notes": "",
                    },
                    "anthropic": {
                        "protocol": "anthropic_messages",
                        "base_url": "https://api.anthropic.com/v1",
                        "api_key_env": "ANTHROPIC_API_KEY",
                        "extra_headers": {},
                        "default_model": "claude-sonnet-4-6",
                        "web_search_adapter": "anthropic_messages",
                        "web_search_model": "claude-sonnet-4-6",
                        "notes": "",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.extra_fields["active_profile"] == "anthropic"
    assert cfg.model == "claude-sonnet-5"
    profile = get_active_profile(cfg)
    assert profile.name == "anthropic"
    assert profile.base_url == "https://api.anthropic.com/v1"


def test_clear_persisted_api_key_removes_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    save_persisted_api_key("stored-key")

    assert credentials_path().exists()
    assert clear_persisted_api_key() is True
    assert credentials_path().exists() is False
    assert clear_persisted_api_key() is False


def test_config_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    cfg = load_config()
    cfg.model = "test-model"
    cfg.llm_reasoning_effort = "high"
    cfg.temperature = 0.2
    cfg.stream = True
    cfg.step_budget_policy = "fixed"
    cfg.task_max_steps = 144
    cfg.subagent_max_steps = 12
    cfg.subagent_timeout_s = 321.5
    cfg.subagent_orchestration.max_background_children = 5
    cfg.subagent_orchestration.turn_end_policy = "cancel"
    cfg.subagent_orchestration.workspace_isolation_enabled = False
    cfg.subagent_orchestration.parallel_nonwriting_shared = True
    cfg.web_search_mode = "auto"
    cfg.web_search_adapter = "openrouter_web"
    cfg.web_search_base_url = "https://api.openai.com/v1"
    cfg.web_search_model = "gpt-4.1-mini"
    cfg.web_search_timeout_s = 25.0
    cfg.skills_enabled = False
    cfg.skills_auto_invoke = True
    cfg.custom_tools_enabled = False
    cfg.assets.enabled = False
    cfg.assets.comprehension.role = "asset_reader"
    cfg.assets.comprehension.vision_fallback_profile = "vision-heavy"
    cfg.assets.comprehension.vision_with_ocr_when_available = False
    cfg.assets.comprehension.ocr_enabled = "always"
    cfg.assets.comprehension.ocr_provider = "tesseract"
    cfg.assets.comprehension.ocr_timeout_seconds = 45
    cfg.assets.comprehension.image_max_edge_pixels = 1536
    cfg.assets.comprehension.questioning_mode = "assertive"
    cfg.assets.comprehension.schema_version = 2
    save_config(cfg)

    cfg2 = load_config()
    assert cfg2.model == "test-model"
    assert cfg2.llm_reasoning_effort == "high"
    assert cfg2.temperature == 0.2
    assert cfg2.stream is True
    assert cfg2.step_budget_policy == "limited"
    assert cfg2.task_max_steps == 144
    assert cfg2.subagent_max_steps == 12
    assert cfg2.subagent_timeout_s == 321.5
    assert cfg2.subagent_orchestration.max_background_children == 5
    assert cfg2.subagent_orchestration.turn_end_policy == "cancel"
    assert cfg2.subagent_orchestration.workspace_isolation_enabled is False
    assert cfg2.subagent_orchestration.parallel_nonwriting_shared is True
    assert cfg2.web_search_mode == "auto"
    assert cfg2.web_search_adapter == "openrouter_web"
    assert cfg2.web_search_base_url == "https://api.openai.com/v1"
    assert cfg2.web_search_model == "gpt-4.1-mini"
    assert cfg2.web_search_timeout_s == 25.0
    assert cfg2.skills_enabled is False
    assert cfg2.skills_auto_invoke is True
    assert cfg2.custom_tools_enabled is False
    assert cfg2.assets.enabled is False
    assert cfg2.assets.comprehension.role == "asset_reader"
    assert cfg2.assets.comprehension.vision_fallback_profile == "vision-heavy"
    assert cfg2.assets.comprehension.vision_with_ocr_when_available is False
    assert cfg2.assets.comprehension.ocr_enabled == "always"
    assert cfg2.assets.comprehension.ocr_provider == "tesseract"
    assert cfg2.assets.comprehension.ocr_timeout_seconds == 45
    assert cfg2.assets.comprehension.image_max_edge_pixels == 1536
    assert cfg2.assets.comprehension.questioning_mode == "assertive"
    assert cfg2.assets.comprehension.schema_version == 2
    assert config_path().name == "config.json"


def test_save_config_replace_failure_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    save_config(AppConfig(model="before"))
    path = config_path()
    previous = path.read_bytes()

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(atomic_io_mod.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_config(AppConfig(model="after"))

    assert path.read_bytes() == previous
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_config_transaction_saves_once_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    loaded = AppConfig(model="before")
    saved: list[AppConfig] = []

    with config_transaction(
        loader=lambda: loaded.model_copy(deep=True),
        saver=lambda cfg: saved.append(cfg.model_copy(deep=True)),
    ) as cfg:
        assert saved == []
        cfg.model = "after"

    assert [cfg.model for cfg in saved] == ["after"]


def test_nested_config_transaction_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    save_config(AppConfig(model="before"))

    with config_transaction() as cfg:
        cfg.extra_fields["outer_update"] = "kept"
        with pytest.raises(ConfigError, match="Nested config transactions"):
            with config_transaction():
                pytest.fail("nested transaction unexpectedly entered")

    assert load_config().extra_fields["outer_update"] == "kept"


def test_config_transaction_exception_leaves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    save_config(AppConfig(model="before"))
    path = config_path()
    previous = path.read_bytes()

    with pytest.raises(RuntimeError, match="mutation failed"):
        with config_transaction() as cfg:
            cfg.model = "after"
            raise RuntimeError("mutation failed")

    assert path.read_bytes() == previous


def test_config_transaction_serializes_fresh_reads_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    save_config(AppConfig(model="before"))
    entered_path = tmp_path / "child-entered"
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (os.fspath(source_root), env.get("PYTHONPATH")) if value
    )
    env["CONFIG_TRANSACTION_ENTERED_PATH"] = os.fspath(entered_path)
    script = """
import os
from pathlib import Path

from alysis_code.config import config_transaction

print("ready", flush=True)
with config_transaction() as cfg:
    Path(os.environ["CONFIG_TRANSACTION_ENTERED_PATH"]).write_text("entered", encoding="utf-8")
    cfg.extra_fields["child_update"] = "kept"
print("done", flush=True)
"""
    process: subprocess.Popen[str] | None = None
    try:
        with config_transaction() as cfg:
            cfg.extra_fields["parent_update"] = "kept"
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
            assert entered_path.exists() is False

        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "done"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()

    persisted = load_config()
    assert persisted.extra_fields["parent_update"] == "kept"
    assert persisted.extra_fields["child_update"] == "kept"


def test_app_config_uses_shared_step_budget_defaults() -> None:
    cfg = AppConfig()

    assert cfg.step_budget_policy == "autonomous"
    assert cfg.max_steps == DEFAULT_CHAT_MAX_STEPS
    assert cfg.task_max_steps == DEFAULT_TASK_MAX_STEPS
    assert cfg.subagent_max_steps == DEFAULT_SUBAGENT_MAX_STEPS
    assert DEFAULT_SUBAGENT_TIMEOUT_S is None
    assert cfg.subagent_timeout_s is None
    assert cfg.subagent_orchestration.max_background_children == 3
    assert cfg.subagent_orchestration.turn_end_policy == "wait"
    assert cfg.subagent_orchestration.workspace_isolation_enabled is True
    assert cfg.subagent_orchestration.parallel_nonwriting_shared is False
    assert cfg.subagent_orchestration.helpers_enabled is True
    assert cfg.subagent_orchestration.helper_max_total_per_child == 2
    assert cfg.subagent_orchestration.helper_timeout_s is None
    assert cfg.subagent_orchestration.helper_max_steps is None
    assert cfg.subagent_orchestration.repetition_signal_threshold == 3
    assert cfg.subagent_orchestration.repetition_nudge_occurrence_threshold == 2
    assert cfg.subagent_orchestration.repetition_occurrence_threshold == 5
    assert cfg.subagent_orchestration.repetition_backstop_threshold == 30
    assert cfg.subagent_orchestration.model_response_activity_after_s == 15.0
    assert cfg.subagent_orchestration.inactivity_signal_after_s == 180.0
    assert cfg.subagent_orchestration.inflight_deadline_grace_s == 10.0
    assert cfg.llm_stream_no_progress_timeout_s == 240.0
    assert cfg.provider_retry_max_retries == 1


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_app_config_rejects_invalid_subagent_timeout(value: float) -> None:
    with pytest.raises(ValueError):
        AppConfig(subagent_timeout_s=value)


def test_set_subagent_timeout_value_validation() -> None:
    cfg = AppConfig()

    cfg = set_config_value(cfg, "subagent_timeout_s", "321.5")
    assert cfg.subagent_timeout_s == 321.5
    for value in ("0", "-1", "nan", "inf", "-inf", "not-a-number"):
        with pytest.raises(ConfigError):
            set_config_value(cfg, "subagent_timeout_s", value)


@pytest.mark.parametrize("value", ["off", "unlimited", "none", "never"])
def test_set_subagent_timeout_accepts_unlimited_spellings(value: str) -> None:
    cfg = AppConfig(subagent_timeout_s=123.0)

    set_config_value(cfg, "subagent_timeout_s", value)

    assert cfg.subagent_timeout_s is None


@pytest.mark.parametrize("value", [0, -1])
def test_app_config_rejects_invalid_background_child_limit(value: int) -> None:
    with pytest.raises(ValueError):
        AppConfig(subagent_orchestration={"max_background_children": value})


def test_app_config_rejects_invalid_subagent_turn_end_policy() -> None:
    with pytest.raises(ValueError):
        AppConfig(subagent_orchestration={"turn_end_policy": "detach"})


def test_set_subagent_orchestration_values() -> None:
    cfg = AppConfig()

    cfg = set_config_value(cfg, "subagent_orchestration.max_background_children", "7")
    cfg = set_config_value(cfg, "subagent_orchestration.turn_end_policy", "cancel")
    cfg = set_config_value(cfg, "subagent_orchestration.workspace_isolation_enabled", "false")
    cfg = set_config_value(cfg, "subagent_orchestration.parallel_nonwriting_shared", "true")
    cfg = set_config_value(cfg, "subagent_orchestration.helpers_enabled", "false")
    cfg = set_config_value(cfg, "subagent_orchestration.helper_max_total_per_child", "4")
    cfg = set_config_value(cfg, "subagent_orchestration.helper_timeout_s", "45.5")
    cfg = set_config_value(cfg, "subagent_orchestration.helper_max_steps", "12")
    cfg = set_config_value(
        cfg,
        "subagent_orchestration.repetition_signal_threshold",
        "4",
    )
    cfg = set_config_value(
        cfg,
        "subagent_orchestration.repetition_nudge_occurrence_threshold",
        "3",
    )
    cfg = set_config_value(
        cfg,
        "subagent_orchestration.repetition_occurrence_threshold",
        "8",
    )
    cfg = set_config_value(
        cfg,
        "subagent_orchestration.repetition_backstop_threshold",
        "40",
    )
    cfg = set_config_value(
        cfg,
        "subagent_orchestration.model_response_activity_after_s",
        "2.5",
    )
    cfg = set_config_value(
        cfg,
        "subagent_orchestration.inactivity_signal_after_s",
        "7.5",
    )
    cfg = set_config_value(
        cfg,
        "subagent_orchestration.inflight_deadline_grace_s",
        "0.25",
    )
    assert cfg.subagent_orchestration.max_background_children == 7
    assert cfg.subagent_orchestration.turn_end_policy == "cancel"
    assert cfg.subagent_orchestration.workspace_isolation_enabled is False
    assert cfg.subagent_orchestration.parallel_nonwriting_shared is True
    assert cfg.subagent_orchestration.helpers_enabled is False
    assert cfg.subagent_orchestration.helper_max_total_per_child == 4
    assert cfg.subagent_orchestration.helper_timeout_s == 45.5
    assert cfg.subagent_orchestration.helper_max_steps == 12
    assert cfg.subagent_orchestration.repetition_signal_threshold == 4
    assert cfg.subagent_orchestration.repetition_nudge_occurrence_threshold == 3
    assert cfg.subagent_orchestration.repetition_occurrence_threshold == 8
    assert cfg.subagent_orchestration.repetition_backstop_threshold == 40
    assert cfg.subagent_orchestration.model_response_activity_after_s == 2.5
    assert cfg.subagent_orchestration.inactivity_signal_after_s == 7.5
    assert cfg.subagent_orchestration.inflight_deadline_grace_s == 0.25

    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.max_background_children", "0")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.turn_end_policy", "detach")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.workspace_isolation_enabled", "maybe")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.parallel_nonwriting_shared", "maybe")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.helpers_enabled", "maybe")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.helper_max_total_per_child", "-1")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.helper_timeout_s", "0")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.helper_max_steps", "0")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.repetition_signal_threshold", "1")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.repetition_signal_threshold", "40")
    with pytest.raises(ConfigError):
        set_config_value(
            cfg,
            "subagent_orchestration.repetition_nudge_occurrence_threshold",
            "8",
        )
    with pytest.raises(ConfigError):
        set_config_value(
            cfg,
            "subagent_orchestration.repetition_occurrence_threshold",
            "3",
        )
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_orchestration.repetition_backstop_threshold", "4")


@pytest.mark.parametrize("value", ["off", "unlimited", "none", "never"])
def test_set_helper_limits_accept_unlimited_spellings(value: str) -> None:
    cfg = AppConfig(
        subagent_orchestration={
            "helper_timeout_s": 45.0,
            "helper_max_steps": 12,
        }
    )

    set_config_value(cfg, "subagent_orchestration.helper_timeout_s", value)
    set_config_value(cfg, "subagent_orchestration.helper_max_steps", value)

    assert cfg.subagent_orchestration.helper_timeout_s is None
    assert cfg.subagent_orchestration.helper_max_steps is None


@pytest.mark.parametrize(
    "key",
    [
        "subagent_orchestration.model_response_activity_after_s",
        "subagent_orchestration.inactivity_signal_after_s",
        "llm_stream_no_progress_timeout_s",
    ],
)
def test_set_progress_watchdog_thresholds_reject_non_positive_values(key: str) -> None:
    configured = set_config_value(AppConfig(), key, "2.5")
    if key.startswith("subagent_orchestration."):
        assert getattr(configured.subagent_orchestration, key.rsplit(".", 1)[1]) == 2.5
    else:
        assert getattr(configured, key) == 2.5
    for value in ("0", "-1", "nan", "inf"):
        with pytest.raises(ConfigError):
            set_config_value(AppConfig(), key, value)


def test_set_inflight_deadline_grace_accepts_zero_and_rejects_invalid_values() -> None:
    key = "subagent_orchestration.inflight_deadline_grace_s"
    assert (
        set_config_value(AppConfig(), key, "0").subagent_orchestration.inflight_deadline_grace_s
        == 0
    )
    for value in ("-1", "nan", "inf", "not-a-number"):
        with pytest.raises(ConfigError):
            set_config_value(AppConfig(), key, value)


def test_stream_defaults_on() -> None:
    assert AppConfig().stream is True


def test_set_stream_value_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "stream", "true")
    assert cfg.stream is True
    cfg = set_config_value(cfg, "stream", "false")
    assert cfg.stream is False
    with pytest.raises(ConfigError):
        set_config_value(cfg, "stream", "maybe")


def test_set_default_mode_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "default_mode", "review")
    assert cfg.default_mode == "review"
    cfg = set_config_value(cfg, "default_mode", "auto")
    assert cfg.default_mode == "auto"
    cfg = set_config_value(cfg, "default_mode", "readonly")
    assert cfg.default_mode == "readonly"
    cfg = set_config_value(cfg, "default_mode", "fullaccess")
    assert cfg.default_mode == "fullaccess"
    with pytest.raises(ConfigError):
        set_config_value(cfg, "default_mode", "safe")


def test_set_routing_mode_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "routing_mode", "auto")
    assert cfg.routing_mode == "auto"
    cfg = set_config_value(cfg, "routing_mode", "code_only")
    assert cfg.routing_mode == "code_only"
    with pytest.raises(ConfigError):
        set_config_value(cfg, "routing_mode", "maybe")


def test_set_step_budget_policy_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "step_budget_policy", "fixed")
    assert cfg.step_budget_policy == "limited"
    cfg = set_config_value(cfg, "step_budget_policy", "adaptive")
    assert cfg.step_budget_policy == "autonomous"
    cfg = set_config_value(cfg, "step_budget_policy", "limited")
    assert cfg.step_budget_policy == "limited"
    cfg = set_config_value(cfg, "step_budget_policy", "autonomous")
    assert cfg.step_budget_policy == "autonomous"
    with pytest.raises(ConfigError):
        set_config_value(cfg, "step_budget_policy", "maybe")


def test_set_task_and_subagent_max_steps_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "task_max_steps", "120")
    cfg = set_config_value(cfg, "subagent_max_steps", "9")
    assert cfg.task_max_steps == 120
    assert cfg.subagent_max_steps == 9
    with pytest.raises(ConfigError):
        set_config_value(cfg, "task_max_steps", "0")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagent_max_steps", "-1")


def test_set_subagents_enabled_validation() -> None:
    cfg = AppConfig()
    assert cfg.subagents_enabled is True

    cfg = set_config_value(cfg, "subagents_enabled", "true")
    assert cfg.subagents_enabled is True
    cfg = set_config_value(cfg, "subagents_enabled", "off")
    assert cfg.subagents_enabled is False

    with pytest.raises(ConfigError):
        set_config_value(cfg, "subagents_enabled", "maybe")


def test_set_skills_flags_validation() -> None:
    cfg = AppConfig()
    assert cfg.skills_enabled is True
    assert cfg.skills_auto_invoke is True
    assert cfg.bundled_skills_enabled is True

    cfg = set_config_value(cfg, "skills_enabled", "false")
    assert cfg.skills_enabled is False
    cfg = set_config_value(cfg, "skills_auto_invoke", "on")
    assert cfg.skills_auto_invoke is True
    cfg = set_config_value(cfg, "bundled_skills_enabled", "off")
    assert cfg.bundled_skills_enabled is False

    with pytest.raises(ConfigError):
        set_config_value(cfg, "skills_enabled", "maybe")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "skills_auto_invoke", "maybe")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "bundled_skills_enabled", "maybe")


def test_load_config_defaults_missing_skills_auto_invoke_to_true_and_preserves_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(json.dumps({"model": "test-model"}), encoding="utf-8")
    assert load_config().skills_auto_invoke is True

    path.write_text(
        json.dumps({"model": "test-model", "skills_auto_invoke": False}),
        encoding="utf-8",
    )
    assert load_config().skills_auto_invoke is False


def test_set_custom_tools_enabled_validation() -> None:
    cfg = AppConfig()
    assert cfg.custom_tools_enabled is True

    cfg = set_config_value(cfg, "custom_tools_enabled", "false")
    assert cfg.custom_tools_enabled is False
    cfg = set_config_value(cfg, "custom_tools_enabled", "on")
    assert cfg.custom_tools_enabled is True

    with pytest.raises(ConfigError):
        set_config_value(cfg, "custom_tools_enabled", "maybe")


def test_set_assets_config_validation() -> None:
    cfg = AppConfig()

    cfg = set_config_value(cfg, "assets.enabled", "false")
    cfg = set_config_value(cfg, "assets.comprehension.role", "asset_reader")
    cfg = set_config_value(cfg, "assets.comprehension.vision_fallback_profile", "vision-heavy")
    cfg = set_config_value(cfg, "assets.comprehension.vision_with_ocr_when_available", "off")
    cfg = set_config_value(cfg, "assets.comprehension.ocr_enabled", "always")
    cfg = set_config_value(cfg, "assets.comprehension.ocr_provider", "tesseract")
    cfg = set_config_value(cfg, "assets.comprehension.ocr_timeout_seconds", "45")
    cfg = set_config_value(cfg, "assets.comprehension.image_max_edge_pixels", "1536")
    cfg = set_config_value(cfg, "assets.comprehension.questioning_mode", "assumption_friendly")
    cfg = set_config_value(cfg, "assets.comprehension.schema_version", "2")

    assert cfg.assets.enabled is False
    assert cfg.assets.comprehension.role == "asset_reader"
    assert cfg.assets.comprehension.vision_fallback_profile == "vision-heavy"
    assert cfg.assets.comprehension.vision_with_ocr_when_available is False
    assert cfg.assets.comprehension.ocr_enabled == "always"
    assert cfg.assets.comprehension.ocr_provider == "tesseract"
    assert cfg.assets.comprehension.ocr_timeout_seconds == 45
    assert cfg.assets.comprehension.image_max_edge_pixels == 1536
    assert cfg.assets.comprehension.questioning_mode == "assumption_friendly"
    assert cfg.assets.comprehension.schema_version == 2

    cfg = set_config_value(cfg, "assets.comprehension.vision_fallback_profile", "")
    assert cfg.assets.comprehension.vision_fallback_profile is None

    with pytest.raises(ConfigError):
        set_config_value(cfg, "assets.enabled", "maybe")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "assets.comprehension.role", "")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "assets.comprehension.ocr_enabled", "enabled")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "assets.comprehension.questioning_mode", "reckless")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "assets.comprehension.ocr_timeout_seconds", "0")


def test_default_web_search_mode_is_auto() -> None:
    assert AppConfig().web_search_mode == "auto"
    assert resolve_web_search_mode(AppConfig()) == "auto"
    assert AppConfig().web_search_policy == "auto"
    assert resolve_web_search_policy(AppConfig()) == "auto"
    assert AppConfig().web_search_adapter == "auto"
    assert resolve_web_search_adapter(AppConfig()) == "auto"


def test_set_and_resolve_web_search_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_POLICY", raising=False)
    cfg = AppConfig()

    cfg = set_config_value(cfg, "web_search_policy", "always")
    assert cfg.web_search_policy == "auto"
    assert resolve_web_search_policy(cfg) == "auto"

    cfg = set_config_value(cfg, "web_search_policy", "off")
    assert cfg.web_search_policy == "off"
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_POLICY", "auto")
    assert resolve_web_search_policy(cfg) == "auto"

    with pytest.raises(ConfigError, match="web_search_policy"):
        set_config_value(cfg, "web_search_policy", "sometimes")


def test_default_web_search_timeout_allows_slower_search_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_TIMEOUT_S", raising=False)
    assert AppConfig().web_search_timeout_s == 45.0
    assert resolve_web_search_timeout_s(AppConfig()) == 45.0


def test_set_web_search_mode_validation_and_deprecated_alias() -> None:
    cfg = AppConfig()
    assert resolve_web_search_mode(cfg) == "auto"

    cfg = set_config_value(cfg, "web_search_mode", "auto")
    assert cfg.web_search_mode == "auto"
    cfg = set_config_value(cfg, "web_search_mode", "off")
    assert cfg.web_search_mode == "off"
    cfg = set_config_value(cfg, "web_search_mode", "native")
    assert cfg.web_search_mode == "native"
    cfg = set_config_value(cfg, "web_search_mode", "external")
    assert cfg.web_search_mode == "external"
    cfg = set_config_value(cfg, "web_search_enabled", "true")
    assert cfg.web_search_mode == "auto"
    cfg = set_config_value(cfg, "web_search_enabled", "off")
    assert cfg.web_search_mode == "off"

    with pytest.raises(ConfigError):
        set_config_value(cfg, "web_search_mode", "on")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "web_search_mode", "maybe")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "web_search_enabled", "maybe")


def test_web_fetch_trusted_domains_defaults_and_set_and_env() -> None:
    from alysis_code.config import (
        DEFAULT_WEB_FETCH_TRUSTED_DOMAINS,
        resolve_web_fetch_trusted_domains,
    )

    cfg = AppConfig()
    # Curated registry defaults are on by default.
    assert "registry.npmjs.org" in cfg.web_fetch_trusted_domains
    assert resolve_web_fetch_trusted_domains(cfg) == tuple(cfg.web_fetch_trusted_domains)
    assert resolve_web_fetch_trusted_domains(None) == tuple(DEFAULT_WEB_FETCH_TRUSTED_DOMAINS)

    cfg = set_config_value(cfg, "web_fetch_trusted_domains", "internal.example.com, pypi.org")
    assert cfg.web_fetch_trusted_domains == ["internal.example.com", "pypi.org"]
    cfg = set_config_value(cfg, "web_fetch_trusted_domains", "none")
    assert cfg.web_fetch_trusted_domains == []
    assert resolve_web_fetch_trusted_domains(cfg) == ()
    cfg = set_config_value(cfg, "web_fetch_trusted_domains", "default")
    assert cfg.web_fetch_trusted_domains == list(DEFAULT_WEB_FETCH_TRUSTED_DOMAINS)

    with pytest.raises(ConfigError, match="web_fetch_trusted_domains"):
        set_config_value(cfg, "web_fetch_trusted_domains", "not a host, pypi.org")


def test_resolve_web_fetch_trusted_domains_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code.config import resolve_web_fetch_trusted_domains

    cfg = AppConfig()
    monkeypatch.setenv("ALYSIS_WEB_FETCH_TRUSTED_DOMAINS", "a.example, b.example")
    assert resolve_web_fetch_trusted_domains(cfg) == ("a.example", "b.example")
    monkeypatch.setenv("ALYSIS_WEB_FETCH_TRUSTED_DOMAINS", "none")
    assert resolve_web_fetch_trusted_domains(cfg) == ()
    monkeypatch.delenv("ALYSIS_WEB_FETCH_TRUSTED_DOMAINS")
    assert resolve_web_fetch_trusted_domains(cfg) == tuple(cfg.web_fetch_trusted_domains)


def test_set_web_search_timeout_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "web_search_timeout_s", "12.5")
    assert cfg.web_search_timeout_s == 12.5

    with pytest.raises(ConfigError):
        set_config_value(cfg, "web_search_timeout_s", "0")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "web_search_timeout_s", "not-a-number")


def test_set_web_search_model_and_base_url_supports_round_trip() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "web_search_adapter", "anthropic_messages")
    cfg = set_config_value(cfg, "web_search_base_url", "https://api.openai.com/v1")
    cfg = set_config_value(cfg, "web_search_model", "gpt-4.1-mini")

    assert cfg.web_search_adapter == "anthropic_messages"
    assert cfg.web_search_base_url == "https://api.openai.com/v1"
    assert cfg.web_search_model == "gpt-4.1-mini"

    cfg = set_config_value(cfg, "web_search_adapter", "")
    cfg = set_config_value(cfg, "web_search_base_url", "")
    cfg = set_config_value(cfg, "web_search_model", "")
    assert cfg.web_search_adapter == "auto"
    assert cfg.web_search_base_url is None
    assert cfg.web_search_model is None

    with pytest.raises(ConfigError, match="web_search_adapter"):
        set_config_value(cfg, "web_search_adapter", "bogus")


def test_set_prompt_cache_knobs_support_round_trip_and_clear() -> None:
    cfg = AppConfig()
    assert cfg.cache.prompt_cache_key_enabled is True
    cfg = set_config_value(cfg, "cache.prompt_cache_key_enabled", "false")
    cfg = set_config_value(cfg, "prompt_cache_mode", "auto")
    cfg = set_config_value(cfg, "prompt_cache_key", "repo-main")
    cfg = set_config_value(cfg, "prompt_cache_retention", "24h")

    assert cfg.cache.prompt_cache_key_enabled is False
    assert cfg.prompt_cache_mode == "auto"
    assert cfg.prompt_cache_key == "repo-main"
    assert cfg.prompt_cache_retention == "24h"

    cfg = set_config_value(cfg, "prompt_cache_mode", "")
    cfg = set_config_value(cfg, "prompt_cache_key", "")
    cfg = set_config_value(cfg, "prompt_cache_retention", "")
    assert cfg.prompt_cache_mode == "manual"
    assert cfg.prompt_cache_key is None
    assert cfg.prompt_cache_retention is None

    with pytest.raises(ConfigError, match="prompt_cache_mode"):
        set_config_value(cfg, "prompt_cache_mode", "always")
    with pytest.raises(ConfigError, match="cache.prompt_cache_key_enabled"):
        set_config_value(cfg, "cache.prompt_cache_key_enabled", "maybe")


def test_set_anthropic_prompt_cache_knobs_validate_and_round_trip() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "anthropic_prompt_cache_enabled", "true")
    cfg = set_config_value(cfg, "anthropic_prompt_cache_ttl", "1h")

    assert cfg.anthropic_prompt_cache_enabled is True
    assert cfg.anthropic_prompt_cache_ttl == "1h"

    cfg = set_config_value(cfg, "anthropic_prompt_cache_enabled", "false")
    cfg = set_config_value(cfg, "anthropic_prompt_cache_ttl", "")
    assert cfg.anthropic_prompt_cache_enabled is False
    assert cfg.anthropic_prompt_cache_ttl == "5m"

    with pytest.raises(ConfigError, match="anthropic_prompt_cache_ttl"):
        set_config_value(cfg, "anthropic_prompt_cache_ttl", "24h")


def test_set_verify_commands_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "verify_commands", '["pytest -q", "ruff check ."]')
    assert cfg.verify_commands == ["pytest -q", "ruff check ."]
    with pytest.raises(ConfigError):
        set_config_value(cfg, "verify_commands", "not-json")


def test_verify_command_fallback_helpers_distinguish_default_from_explicit_lists() -> None:
    assert DEFAULT_VERIFY_COMMANDS == ("pytest -q",)
    assert normalize_verify_command_list([" pytest -q ", ""]) == ("pytest -q",)
    assert is_generic_verify_command_fallback(["pytest -q"]) is True
    assert is_generic_verify_command_fallback(["npm test"]) is False
    assert is_generic_verify_command_fallback([]) is False


def test_generic_configured_verify_preset_classifier_is_conservative() -> None:
    assert is_generic_configured_verify_preset(["pytest -q", "ruff check ."]) is True
    assert is_generic_configured_verify_preset(["pytest", "ruff check src"]) is True
    assert is_generic_configured_verify_preset(["python -m pytest -q"]) is True
    assert is_generic_configured_verify_preset([r"C:\Python311\python.exe -m pytest -q"]) is True
    assert is_generic_configured_verify_preset([r"C:\Python311\python.exe -m ruff check ."]) is True
    assert (
        is_generic_configured_verify_preset(
            [r'"C:\Program Files\Python311\python.exe" -m pytest -q']
        )
        is True
    )
    assert (
        is_generic_configured_verify_preset(
            [r'"C:\Program Files\Python311\python.exe" -m ruff check .']
        )
        is True
    )
    assert (
        is_generic_configured_verify_preset([r"C:\Python311\python3.exe -m ruff check ."]) is True
    )
    assert is_generic_configured_verify_preset([r"C:\Python311\py.exe -m pytest -q"]) is True
    assert is_generic_configured_verify_preset([r"C:/Python311/python.exe -m pytest -q"]) is True
    assert is_generic_configured_verify_preset([r"C:/Python311/python.exe -m ruff check ."]) is True
    assert (
        is_generic_configured_verify_preset(
            [r'"C:/Program Files/Python311/python.exe" -m pytest -q']
        )
        is True
    )
    assert (
        is_generic_configured_verify_preset(
            [r'"C:/Program Files/Python311/python.exe" -m ruff check .']
        )
        is True
    )
    assert is_generic_configured_verify_preset(["py -m pytest"]) is True
    assert is_generic_configured_verify_preset(["py -m pytest -q"]) is True
    assert is_generic_configured_verify_preset(["ruff check"]) is True
    assert is_generic_configured_verify_preset(["ruff check ./"]) is True
    assert is_generic_configured_verify_preset(["ruff check src/"]) is True
    assert is_generic_configured_verify_preset(["ruff check ./src"]) is True
    assert is_generic_configured_verify_preset(["python -m ruff check"]) is True
    assert is_generic_configured_verify_preset(["python -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["python3 -m ruff check"]) is True
    assert is_generic_configured_verify_preset(["python3 -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["py -m ruff check"]) is True
    assert is_generic_configured_verify_preset(["py -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["pytest", "ruff check"]) is True
    assert is_generic_configured_verify_preset(["pytest -q", "ruff check"]) is True
    assert is_generic_configured_verify_preset(["python -m pytest -q", "ruff check"]) is True
    assert (
        is_generic_configured_verify_preset(["python -m pytest -q", "python -m ruff check ."])
        is True
    )
    assert is_generic_configured_verify_preset(["py -m pytest -q", "py -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["uv run pytest"]) is True
    assert is_generic_configured_verify_preset(["uv run pytest -q"]) is True
    assert is_generic_configured_verify_preset(["poetry run pytest"]) is True
    assert is_generic_configured_verify_preset(["poetry run pytest -q"]) is True
    assert is_generic_configured_verify_preset(["pipenv run pytest"]) is True
    assert is_generic_configured_verify_preset(["pipenv run pytest -q"]) is True
    assert is_generic_configured_verify_preset(["uv run python -m pytest -q"]) is True
    assert is_generic_configured_verify_preset(["poetry run python -m pytest -q"]) is True
    assert is_generic_configured_verify_preset(["pipenv run python -m pytest -q"]) is True
    assert (
        is_generic_configured_verify_preset([r"uv run C:\Python311\python.exe -m pytest -q"])
        is True
    )
    assert (
        is_generic_configured_verify_preset([r"uv run C:\Python311\python.exe -m ruff check ."])
        is True
    )
    assert (
        is_generic_configured_verify_preset(
            [r'uv run "C:\Program Files\Python311\python.exe" -m pytest -q']
        )
        is True
    )
    assert (
        is_generic_configured_verify_preset(
            [r'uv run "C:\Program Files\Python311\python.exe" -m ruff check .']
        )
        is True
    )
    assert is_generic_configured_verify_preset(["uv run py -m pytest -q"]) is True
    assert is_generic_configured_verify_preset(["uv run ruff check"]) is True
    assert is_generic_configured_verify_preset(["poetry run ruff check ."]) is True
    assert is_generic_configured_verify_preset(["pipenv run ruff check src"]) is True
    assert is_generic_configured_verify_preset(["uv run python -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["poetry run python -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["pipenv run python -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["uv run py -m ruff check ."]) is True
    assert is_generic_configured_verify_preset(["uv run pytest -q", "ruff check"]) is True
    assert is_generic_configured_verify_preset(["pytest -q", "uv run ruff check"]) is True
    assert is_generic_configured_verify_preset(["uv run pytest -q", "uv run ruff check"]) is True
    assert (
        is_generic_configured_verify_preset(
            ["uv run python -m pytest -q", "uv run python -m ruff check ."]
        )
        is True
    )
    assert (
        is_generic_configured_verify_preset(
            [
                r"uv run C:\Python311\python.exe -m pytest -q",
                r"uv run C:\Python311\python.exe -m ruff check .",
            ]
        )
        is True
    )
    assert is_generic_configured_verify_preset(["poetry run pytest -q", "ruff check ."]) is True
    assert (
        is_generic_configured_verify_preset(["poetry run pytest -q", "poetry run ruff check ."])
        is True
    )
    assert (
        is_generic_configured_verify_preset(["pipenv run python -m pytest -q", "ruff check src"])
        is True
    )
    assert (
        is_generic_configured_verify_preset(
            ["pipenv run python -m pytest -q", "pipenv run ruff check src"]
        )
        is True
    )

    assert is_generic_configured_verify_preset(["make verify"]) is False
    assert is_generic_configured_verify_preset(["pnpm --dir packages/web test"]) is False
    assert is_generic_configured_verify_preset(["PYTHONPATH=src pytest -q"]) is False
    assert is_generic_configured_verify_preset(["pytest tests/custom.py -q"]) is False
    assert is_generic_configured_verify_preset(["pytest -m smoke -q"]) is False
    assert (
        is_generic_configured_verify_preset(["python -m pytest tests/api/test_users.py -q"])
        is False
    )
    assert (
        is_generic_configured_verify_preset(
            [r"C:\Python311\python.exe -m pytest tests/api/test_users.py -q"]
        )
        is False
    )
    assert (
        is_generic_configured_verify_preset(
            [r'"C:\Program Files\Python311\python.exe" -m pytest -m smoke -q']
        )
        is False
    )
    assert is_generic_configured_verify_preset(["py -m pytest tests/custom.py -q"]) is False
    assert is_generic_configured_verify_preset(["python -m pytest -m smoke -q"]) is False
    assert is_generic_configured_verify_preset(["ruff check app.py"]) is False
    assert is_generic_configured_verify_preset(["ruff check src tests"]) is False
    assert is_generic_configured_verify_preset(["ruff check --config pyproject.toml ."]) is False
    assert is_generic_configured_verify_preset(["ruff check --select F401 ."]) is False
    assert is_generic_configured_verify_preset(["ruff check src/app.py"]) is False
    assert is_generic_configured_verify_preset(["python -m ruff check src/app.py"]) is False
    assert (
        is_generic_configured_verify_preset([r"C:\Python311\python.exe -m ruff check src/app.py"])
        is False
    )
    assert (
        is_generic_configured_verify_preset([r"C:\Python311\python.exe -m ruff check src tests"])
        is False
    )
    assert (
        is_generic_configured_verify_preset(
            [r"C:\Python311\python.exe -m ruff check --config pyproject.toml ."]
        )
        is False
    )
    assert (
        is_generic_configured_verify_preset(
            [r'uv run "C:\Program Files\Python311\python.exe" -m pytest tests/custom.py -q']
        )
        is False
    )
    assert is_generic_configured_verify_preset(["python -m ruff check src tests"]) is False
    assert (
        is_generic_configured_verify_preset(["python -m ruff check --config pyproject.toml ."])
        is False
    )
    assert is_generic_configured_verify_preset(["python -m ruff check --select F401 ."]) is False
    assert (
        is_generic_configured_verify_preset(["uv run pytest tests/api/test_users.py -q"]) is False
    )
    assert is_generic_configured_verify_preset(["poetry run pytest -m smoke -q"]) is False
    assert is_generic_configured_verify_preset(["pipenv run pytest tests/custom.py -q"]) is False
    assert is_generic_configured_verify_preset(["uv run PYTHONPATH=src pytest -q"]) is False
    assert (
        is_generic_configured_verify_preset(["uv run py -m pytest tests/api/test_users.py -q"])
        is False
    )
    assert is_generic_configured_verify_preset(["uv run ruff check src/app.py"]) is False
    assert is_generic_configured_verify_preset(["poetry run ruff check src tests"]) is False
    assert (
        is_generic_configured_verify_preset(["pipenv run ruff check --config pyproject.toml ."])
        is False
    )
    assert is_generic_configured_verify_preset(["uv run ruff check --select F401 ."]) is False
    assert is_generic_configured_verify_preset(["uv run python -m ruff check src/app.py"]) is False
    assert is_generic_configured_verify_preset(["cargo test"]) is False
    assert is_generic_configured_verify_preset(["go test ./..."]) is False


def test_default_integration_verify_mode_is_warn() -> None:
    assert AppConfig().integration_verify_mode == "warn"


def test_set_integration_verify_mode_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "integration_verify_mode", "strict")
    assert cfg.integration_verify_mode == "strict"
    cfg = set_config_value(cfg, "integration_verify_mode", "off")
    assert cfg.integration_verify_mode == "off"
    with pytest.raises(ConfigError):
        set_config_value(cfg, "integration_verify_mode", "maybe")


def test_set_llm_timeout_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "llm_timeout_s", "12.5")
    assert cfg.llm_timeout_s == 12.5
    with pytest.raises(ConfigError):
        set_config_value(cfg, "llm_timeout_s", "0")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "llm_timeout_s", "not-a-number")


def test_set_llm_enable_thinking_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "llm_enable_thinking", "false")
    assert cfg.llm_enable_thinking is False
    cfg = set_config_value(cfg, "llm_enable_thinking", "true")
    assert cfg.llm_enable_thinking is True
    cfg = set_config_value(cfg, "llm_enable_thinking", "auto")
    assert cfg.llm_enable_thinking is None
    with pytest.raises(ConfigError):
        set_config_value(cfg, "llm_enable_thinking", "maybe")


def test_set_llm_reasoning_effort_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "llm_reasoning_effort", "minimal")
    assert cfg.llm_reasoning_effort == "minimal"
    cfg = set_config_value(cfg, "llm_reasoning_effort", "xhigh")
    assert cfg.llm_reasoning_effort == "xhigh"
    cfg = set_config_value(cfg, "llm_reasoning_effort", "auto")
    assert cfg.llm_reasoning_effort is None
    with pytest.raises(ConfigError):
        set_config_value(cfg, "llm_reasoning_effort", "extreme")


def test_set_feedback_github_settings_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "feedback_github_enabled", "false")
    cfg = set_config_value(cfg, "feedback_open_browser", "false")
    cfg = set_config_value(cfg, "feedback_github_repo", "https://github.com/acme/alysis.git")

    assert cfg.feedback_github_enabled is False
    assert cfg.feedback_open_browser is False
    assert cfg.feedback_github_repo == "acme/alysis"

    with pytest.raises(ConfigError):
        set_config_value(cfg, "feedback_github_repo", "https://example.com/acme/alysis")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "feedback_github_enabled", "maybe")


def test_set_provider_limit_settings_validation() -> None:
    cfg = AppConfig()
    assert cfg.provider_concurrency_caps == {"qwen": 4}

    cfg = set_config_value(cfg, "provider_concurrency_caps", '{"dashscope": 2, "openai": 0}')
    assert cfg.provider_concurrency_caps == {"dashscope": 2, "openai": 0}

    cfg = set_config_value(cfg, "provider_retry_max_retries", "7")
    assert cfg.provider_retry_max_retries == 7
    cfg = set_config_value(cfg, "provider_retry_base_delay_seconds", "1.5")
    assert cfg.provider_retry_base_delay_seconds == 1.5
    cfg = set_config_value(cfg, "provider_retry_max_delay_seconds", "12.5")
    assert cfg.provider_retry_max_delay_seconds == 12.5

    with pytest.raises(ConfigError):
        set_config_value(cfg, "provider_concurrency_caps", "[]")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "provider_concurrency_caps", '{"qwen": -1}')
    with pytest.raises(ConfigError):
        set_config_value(cfg, "provider_retry_max_retries", "-1")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "provider_retry_base_delay_seconds", "0")


def test_resolve_llm_enable_thinking_defaults_off_for_dashscope_qwen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_LLM_ENABLE_THINKING", raising=False)

    assert (
        resolve_llm_enable_thinking(
            AppConfig(
                base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                model="qwen3.5-plus",
            )
        )
        is False
    )
    assert (
        resolve_llm_enable_thinking(
            AppConfig(
                base_url="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
                model="qwen3.5-plus",
            )
        )
        is False
    )
    assert resolve_llm_enable_thinking(AppConfig(model="gpt-5-mini")) is None

    cfg = AppConfig(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        llm_enable_thinking=True,
    )
    assert resolve_llm_enable_thinking(cfg) is True

    monkeypatch.setenv("ALYSIS_LLM_ENABLE_THINKING", "false")
    assert resolve_llm_enable_thinking(cfg) is False


def test_resolve_llm_reasoning_effort_prefers_env_config_then_legacy_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_LLM_REASONING_EFFORT", raising=False)

    cfg = AppConfig(llm_reasoning_effort="low")
    cfg.extra_fields["llm_thinking_label"] = "high"
    assert resolve_llm_reasoning_effort(cfg) == "low"

    cfg.llm_reasoning_effort = None
    assert resolve_llm_reasoning_effort(cfg) == "high"

    monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", "minimal")
    assert resolve_llm_reasoning_effort(cfg) == "minimal"

    monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", "auto")
    assert resolve_llm_reasoning_effort(cfg) is None

    monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", "extreme")
    with pytest.raises(ConfigError):
        resolve_llm_reasoning_effort(cfg)


def test_set_model_metadata_policy_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "model_metadata_policy", "strict")
    assert cfg.model_metadata_policy == "strict"
    cfg = set_config_value(cfg, "model_metadata_policy", "warn")
    assert cfg.model_metadata_policy == "warn"
    with pytest.raises(ConfigError):
        set_config_value(cfg, "model_metadata_policy", "maybe")


def test_set_toolbar_items_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "toolbar_items", '["mode", "ctx", "tokens"]')
    assert cfg.toolbar_items == ["mode", "ctx", "tokens"]

    cfg = set_config_value(cfg, "toolbar_items", "[]")
    assert cfg.toolbar_items == []

    with pytest.raises(ConfigError):
        set_config_value(cfg, "toolbar_items", "not-json")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "toolbar_items", '["mode", "unknown"]')
    with pytest.raises(ConfigError):
        set_config_value(cfg, "toolbar_items", '["mode", 123]')


def test_set_role_model_router_config_value() -> None:
    cfg = AppConfig()
    cfg.extra_fields = {"role_models": {"coding": "coding-model"}}

    cfg = set_config_value(cfg, "role_models.router", "gpt-5.4-mini")
    assert cfg.extra_fields["role_models"] == {
        "coding": "coding-model",
        "router": "gpt-5.4-mini",
    }

    cfg = set_config_value(cfg, "role_models.router", "")
    assert cfg.extra_fields["role_models"] == {"coding": "coding-model"}


def test_set_forge_role_model_router_config_value() -> None:
    cfg = AppConfig()

    cfg = set_config_value(cfg, "forge_role_models.router", "cheap-router-model")
    assert cfg.extra_fields["forge_role_models"] == {"router": "cheap-router-model"}

    cfg = set_config_value(cfg, "forge_role_models.router", "   ")
    assert "forge_role_models" not in cfg.extra_fields


def test_set_role_model_config_rejects_unknown_role() -> None:
    with pytest.raises(ConfigError, match="role_models.bogus is not supported"):
        set_config_value(AppConfig(), "role_models.bogus", "model")


def test_clone_cfg_preserves_extra_fields_deep_copy() -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {"role_models": {"coding": "test-model"}}
    cloned = clone_cfg(cfg)

    assert cloned.model == "default-model"
    assert cloned.extra_fields == {"role_models": {"coding": "test-model"}}
    assert cloned is not cfg
    assert cloned.extra_fields is not cfg.extra_fields

    cloned.extra_fields["role_models"]["coding"] = "mutated-model"
    assert cfg.extra_fields["role_models"]["coding"] == "test-model"


def test_set_legacy_temperature_updates_all_role_temperatures() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "temperature", "0.4")

    assert cfg.temperature == 0.4
    assert cfg.coding_temperature == 0.4
    assert cfg.review_temperature == 0.4
    assert cfg.planner_temperature == 0.4
    assert cfg.conflict_review_temperature == 0.4
    assert cfg.compactor_temperature == 0.4
    assert cfg.chat_temperature == 0.4


def test_set_role_temperature_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "review_temperature", "0.1")
    assert cfg.review_temperature == 0.1

    with pytest.raises(ConfigError):
        set_config_value(cfg, "planner_temperature", "not-a-number")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "coding_temperature", "-1")


def test_load_config_migrates_legacy_temperature_to_role_temperatures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    raw = {
        "model": "test-model",
        "temperature": 0.9,
    }
    (tmp_path / "config.json").write_text(json.dumps(raw), encoding="utf-8")

    cfg = load_config()
    assert cfg.temperature == 0.9
    assert cfg.coding_temperature == 0.9
    assert cfg.review_temperature == 0.9
    assert cfg.planner_temperature == 0.9
    assert cfg.conflict_review_temperature == 0.9
    assert cfg.compactor_temperature == 0.9
    assert cfg.chat_temperature == 0.9


@pytest.mark.parametrize(
    ("legacy_value", "expected_mode"),
    [
        (True, "auto"),
        (False, "off"),
    ],
)
def test_load_config_maps_legacy_web_search_enabled_to_web_search_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_value: bool,
    expected_mode: str,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"web_search_enabled": legacy_value}),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.web_search_mode == expected_mode
    save_config(cfg)
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["web_search_mode"] == expected_mode
    assert "web_search_enabled" not in saved


def test_load_config_maps_legacy_web_search_mode_on_to_auto(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"web_search_mode": "on"}),
        encoding="utf-8",
    )

    cfg = load_config()

    assert cfg.web_search_mode == "auto"
    save_config(cfg)
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["web_search_mode"] == "auto"


def test_resolve_llm_timeout_defaults_to_sixty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_LLM_TIMEOUT_S", raising=False)
    assert resolve_llm_timeout_s(AppConfig()) == 60.0


def test_resolve_llm_timeout_prefers_config_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_LLM_TIMEOUT_S", raising=False)
    assert resolve_llm_timeout_s(AppConfig(llm_timeout_s=17.5)) == 17.5


def test_resolve_llm_timeout_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_LLM_TIMEOUT_S", "42.0")
    assert resolve_llm_timeout_s(AppConfig(llm_timeout_s=17.5)) == 42.0


def test_run_deadline_config_env_and_cli_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)
    cfg = AppConfig(run_deadline_seconds=17.5)

    assert resolve_run_deadline_seconds(AppConfig()) is None
    assert resolve_run_deadline(AppConfig()).source == "absent"
    assert resolve_run_deadline_seconds(cfg) == 17.5
    assert resolve_run_deadline(cfg).source == "config"

    monkeypatch.setenv("ALYSIS_RUN_DEADLINE_SECONDS", "42.0")
    assert resolve_run_deadline_seconds(cfg) == 42.0
    assert resolve_run_deadline(cfg).source == "environment"
    assert resolve_run_deadline_seconds(cfg, cli_deadline_seconds=8.0) == 8.0
    assert resolve_run_deadline(cfg, cli_deadline_seconds=8.0).source == "explicit_cli"

    monkeypatch.setenv("ALYSIS_RUN_DEADLINE_SECONDS", "0")
    with pytest.raises(ConfigError):
        resolve_run_deadline_seconds(cfg)


def test_set_run_deadline_validation() -> None:
    cfg = set_config_value(AppConfig(), "run_deadline_seconds", "12.5")
    assert cfg.run_deadline_seconds == 12.5

    cfg = set_config_value(cfg, "run_deadline_seconds", "off")
    assert cfg.run_deadline_seconds is None

    with pytest.raises(ConfigError):
        set_config_value(cfg, "run_deadline_seconds", "0")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "run_deadline_seconds", "nan")


def test_crash_diagnostic_log_path_config_env_and_cli_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_CRASH_DIAGNOSTIC_LOG_PATH", raising=False)
    cfg = AppConfig(crash_diagnostic_log_path="/tmp/config-diag.jsonl")

    assert resolve_crash_diagnostic_log_path(AppConfig()) is None
    assert resolve_crash_diagnostic_log_path(cfg) == "/tmp/config-diag.jsonl"

    monkeypatch.setenv("ALYSIS_CRASH_DIAGNOSTIC_LOG_PATH", "/tmp/env-diag.jsonl")
    assert resolve_crash_diagnostic_log_path(cfg) == "/tmp/env-diag.jsonl"
    assert (
        resolve_crash_diagnostic_log_path(
            cfg,
            cli_diagnostic_log_path="/tmp/cli-diag.jsonl",
        )
        == "/tmp/cli-diag.jsonl"
    )

    cfg = set_config_value(cfg, "crash_diagnostic_log_path", "")
    assert cfg.crash_diagnostic_log_path is None


def test_resolve_web_search_timeout_prefers_env_then_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_TIMEOUT_S", raising=False)
    assert resolve_web_search_timeout_s(AppConfig(web_search_timeout_s=17.5)) == 17.5

    monkeypatch.setenv("ALYSIS_WEB_SEARCH_TIMEOUT_S", "42.0")
    assert resolve_web_search_timeout_s(AppConfig(web_search_timeout_s=17.5)) == 42.0

    monkeypatch.setenv("ALYSIS_WEB_SEARCH_TIMEOUT_S", "0")
    with pytest.raises(ConfigError):
        resolve_web_search_timeout_s(AppConfig(web_search_timeout_s=17.5))


def test_resolve_feedback_github_settings_use_config_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_FEEDBACK_GITHUB_ENABLED", raising=False)
    monkeypatch.delenv("ALYSIS_FEEDBACK_GITHUB_REPO", raising=False)
    monkeypatch.delenv("ALYSIS_FEEDBACK_OPEN_BROWSER", raising=False)

    cfg = AppConfig(
        feedback_github_enabled=False,
        feedback_github_repo="acme/alysis",
        feedback_open_browser=False,
    )
    assert resolve_feedback_github_enabled(cfg) is False
    assert resolve_feedback_github_repo(cfg) == "acme/alysis"
    assert resolve_feedback_open_browser(cfg) is False

    monkeypatch.setenv("ALYSIS_FEEDBACK_GITHUB_ENABLED", "true")
    monkeypatch.setenv("ALYSIS_FEEDBACK_GITHUB_REPO", "https://github.com/org/repo")
    monkeypatch.setenv("ALYSIS_FEEDBACK_OPEN_BROWSER", "true")
    assert resolve_feedback_github_enabled(cfg) is True
    assert resolve_feedback_github_repo(cfg) == "org/repo"
    assert resolve_feedback_open_browser(cfg) is True


def test_resolve_model_metadata_policy_defaults_to_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_MODEL_METADATA_POLICY", raising=False)
    assert resolve_model_metadata_policy(AppConfig()) == "warn"


def test_resolve_model_metadata_policy_prefers_config_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_MODEL_METADATA_POLICY", raising=False)
    assert resolve_model_metadata_policy(AppConfig(model_metadata_policy="strict")) == "strict"


def test_resolve_model_metadata_policy_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_MODEL_METADATA_POLICY", "strict")
    assert resolve_model_metadata_policy(AppConfig(model_metadata_policy="warn")) == "strict"


def test_resolve_model_metadata_policy_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_MODEL_METADATA_POLICY", raising=False)
    with pytest.raises(ConfigError):
        resolve_model_metadata_policy(AppConfig(model_metadata_policy="maybe"))
    monkeypatch.setenv("ALYSIS_MODEL_METADATA_POLICY", "invalid")
    with pytest.raises(ConfigError):
        resolve_model_metadata_policy(AppConfig(model_metadata_policy="warn"))


def test_resolve_web_search_base_url_falls_back_to_main_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_BASE_URL", raising=False)
    assert (
        resolve_web_search_base_url(AppConfig(base_url="https://api.openai.com/v1"))
        == "https://api.openai.com/v1"
    )
    assert (
        resolve_web_search_base_url(AppConfig(base_url="https://example-proxy.invalid/v1"))
        == "https://example-proxy.invalid/v1"
    )


def test_resolve_web_search_model_and_base_url_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_BASE_URL", "https://search.example.com/v1")
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_MODEL", "web-model")
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_ADAPTER", "openrouter_web")
    cfg = AppConfig(
        model="main-model",
        base_url="https://api.openai.com/v1",
        web_search_adapter="anthropic_messages",
        web_search_base_url="https://api.openai.com/v1",
        web_search_model="cfg-web-model",
    )

    assert resolve_web_search_adapter(cfg) == "openrouter_web"
    assert resolve_web_search_base_url(cfg) == "https://search.example.com/v1"
    assert resolve_web_search_model(cfg) == "web-model"


def test_resolve_web_search_adapter_and_model_can_come_from_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_ADAPTER", raising=False)
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_MODEL", raising=False)
    cfg = AppConfig(model="chat-model")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            web_search_adapter="groq_compound",
            web_search_model="groq/compound-mini",
        ),
    )
    set_active_profile(cfg, "groq")

    assert resolve_web_search_adapter(cfg) == "groq_compound"
    assert resolve_web_search_model(cfg) == "groq/compound-mini"


def test_resolve_web_search_base_url_can_come_from_active_profile_without_copied_cfg_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_BASE_URL", raising=False)
    cfg = AppConfig(base_url="https://api.openai.com/v1")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            web_search_adapter="anthropic_messages",
        ),
    )
    cfg.extra_fields["active_profile"] = "anthropic"

    assert cfg.base_url == "https://api.openai.com/v1"
    assert resolve_web_search_base_url(cfg) == "https://api.anthropic.com/v1"


def test_resolve_prompt_cache_knobs_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_PROMPT_CACHE_MODE", raising=False)
    monkeypatch.delenv("ALYSIS_PROMPT_CACHE_KEY", raising=False)
    monkeypatch.delenv("ALYSIS_PROMPT_CACHE_RETENTION", raising=False)

    cfg = AppConfig()
    assert resolve_prompt_cache_mode(cfg) == "manual"
    assert resolve_prompt_cache_key(cfg) is None
    assert resolve_prompt_cache_retention(cfg) is None


def test_resolve_prompt_cache_knobs_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_PROMPT_CACHE_MODE", "auto")
    monkeypatch.setenv("ALYSIS_PROMPT_CACHE_KEY", "env-cache-key")
    monkeypatch.setenv("ALYSIS_PROMPT_CACHE_RETENTION", "8h")

    cfg = AppConfig(
        prompt_cache_mode="manual",
        prompt_cache_key="cfg-cache-key",
        prompt_cache_retention="1h",
    )
    assert resolve_prompt_cache_mode(cfg) == "auto"
    assert resolve_prompt_cache_key(cfg) == "env-cache-key"
    assert resolve_prompt_cache_retention(cfg) == "8h"


def test_resolve_prompt_cache_mode_rejects_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_PROMPT_CACHE_MODE", "always")

    with pytest.raises(ConfigError, match="ALYSIS_PROMPT_CACHE_MODE"):
        resolve_prompt_cache_mode(AppConfig())


def test_resolve_anthropic_prompt_cache_knobs_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_ANTHROPIC_PROMPT_CACHE_ENABLED", "true")
    monkeypatch.setenv("ALYSIS_ANTHROPIC_PROMPT_CACHE_TTL", "1h")

    cfg = AppConfig(anthropic_prompt_cache_enabled=False, anthropic_prompt_cache_ttl="5m")

    assert resolve_anthropic_prompt_cache_enabled(cfg) is True
    assert resolve_anthropic_prompt_cache_ttl(cfg) == "1h"


def test_resolve_anthropic_prompt_cache_ttl_rejects_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_ANTHROPIC_PROMPT_CACHE_TTL", "24h")

    with pytest.raises(ConfigError, match="ALYSIS_ANTHROPIC_PROMPT_CACHE_TTL"):
        resolve_anthropic_prompt_cache_ttl(AppConfig())


def test_resolve_web_search_mode_rejects_invalid_values() -> None:
    with pytest.raises(ConfigError):
        resolve_web_search_mode(AppConfig(web_search_mode="maybe"))


def test_web_search_provider_is_not_a_public_config_key() -> None:
    with pytest.raises(ConfigError):
        set_config_value(AppConfig(), "web_search_provider", "tavily")
