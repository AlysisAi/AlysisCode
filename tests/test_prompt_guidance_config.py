from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from alysis_code.config import (
    AppConfig,
    ConfigError,
    PromptGuidanceConfig,
    clone_cfg,
    load_config,
    resolve_prompt_guidance_profile,
    save_config,
    set_config_value,
)
from alysis_code.prompt_guidance_catalog import GUIDANCE_PROFILES, get_model_prompt_catalog


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4"])
def test_child_family_is_independent_of_parent_density(model: str) -> None:
    assert resolve_prompt_guidance_profile(AppConfig(model=model), subagent=True) == "gpt-subagent"


def test_explicit_family_for_private_model_applies_to_both_scopes() -> None:
    cfg = AppConfig(
        model="private-deployment",
        prompt_guidance={"model_profiles": {"private-deployment": "qwen-normal"}},
    )
    assert resolve_prompt_guidance_profile(cfg) == "qwen-normal"
    assert resolve_prompt_guidance_profile(cfg, subagent=True) == "qwen-subagent"
    set_config_value(cfg, "prompt_guidance.model_profiles.private-deployment", "qwen-expanded")
    assert resolve_prompt_guidance_profile(cfg) == "qwen-expanded"
    assert resolve_prompt_guidance_profile(cfg, subagent=True) == "qwen-subagent"


@pytest.mark.parametrize("profile", ["compact", "balanced", "expanded"])
def test_general_parent_overrides_keep_one_general_child_prompt(profile: str) -> None:
    cfg = AppConfig(model="opaque", prompt_guidance={"default": profile, "model_profiles": {}})
    assert resolve_prompt_guidance_profile(cfg) == profile
    assert resolve_prompt_guidance_profile(cfg, subagent=True) == "general-subagent"


def test_child_profile_cannot_be_selected_as_a_parent_configuration() -> None:
    with pytest.raises(ValidationError):
        PromptGuidanceConfig(default="gpt-subagent")
    with pytest.raises(ValidationError):
        PromptGuidanceConfig(model_profiles={"model": "gpt-subagent"})


def test_default_configuration_uses_the_exact_catalog_and_expanded_fallback() -> None:
    guidance = PromptGuidanceConfig()
    assert guidance.default == "expanded"
    assert guidance.model_profiles == get_model_prompt_catalog()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-6-sol", "gpt-normal"),
        ("gpt-6-luna", "gpt-expanded"),
        ("gpt-5.6-luna", "gpt-expanded"),
        ("gpt-5.6-terra", "gpt-normal"),
        ("gpt-5.6-sol", "gpt-normal"),
        ("gpt-5.6", "gpt-normal"),
        ("gpt-5.5", "gpt-expanded"),
        ("gpt-6-astra", "gpt-normal"),
        ("unknown-model", "expanded"),
        ("gpt-5.6-luna-next", "expanded"),
        ("gpt-6-astra-next", "expanded"),
        ("openai/gpt-5.6-luna", "gpt-expanded"),
        ("unreviewed-gateway/gpt-5.6-luna", "expanded"),
        ("CLAUDE-FABLE-5", "expanded"),
    ],
)
def test_profile_selection_uses_only_exact_configured_model_ids(model: str, expected: str) -> None:
    assert resolve_prompt_guidance_profile(AppConfig(model=model)) == expected


def test_profile_override_uses_effective_model_and_does_not_mutate_base_model() -> None:
    cfg = AppConfig(model="gpt-5.6-luna")
    assert resolve_prompt_guidance_profile(cfg, model="gpt-6-astra") == "gpt-normal"
    assert cfg.model == "gpt-5.6-luna"
    assert resolve_prompt_guidance_profile(None) == "expanded"


def test_exact_model_config_set_remove_and_whole_mapping_replacement() -> None:
    cfg = AppConfig(model="vendor/model.v2")
    set_config_value(cfg, "prompt_guidance.default", "compact")
    set_config_value(cfg, "prompt_guidance.model_profiles.vendor/model.v2", " expanded ")
    assert resolve_prompt_guidance_profile(cfg) == "expanded"
    assert resolve_prompt_guidance_profile(cfg, model="gpt-5.6-luna") == "gpt-expanded"
    set_config_value(cfg, "prompt_guidance.model_profiles.vendor/model.v2", "")
    assert resolve_prompt_guidance_profile(cfg) == "compact"
    set_config_value(cfg, "prompt_guidance.model_profiles", "{}")
    assert resolve_prompt_guidance_profile(cfg, model="gpt-5.6-luna") == "compact"


@pytest.mark.parametrize("profile", GUIDANCE_PROFILES)
def test_named_and_generic_profiles_are_explicit_configuration_overrides(profile: str) -> None:
    cfg = AppConfig(model="private-model")
    set_config_value(cfg, "prompt_guidance.default", profile)
    assert resolve_prompt_guidance_profile(cfg) == profile
    set_config_value(cfg, "prompt_guidance.model_profiles", json.dumps({"private-model": profile}))
    assert cfg.prompt_guidance.model_profiles == {"private-model": profile}
    assert resolve_prompt_guidance_profile(cfg) == profile


def test_supplied_mapping_replaces_catalog_including_known_model_assignments() -> None:
    cfg = AppConfig(
        model="gpt-6-astra",
        prompt_guidance={"default": "balanced", "model_profiles": {"private-model": "gpt-normal"}},
    )
    assert cfg.prompt_guidance.model_profiles == {"private-model": "gpt-normal"}
    assert resolve_prompt_guidance_profile(cfg) == "balanced"
    assert resolve_prompt_guidance_profile(cfg, model="private-model") == "gpt-normal"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("prompt_guidance.default", "automatic"),
        ("prompt_guidance.model_profiles", "[]"),
        ("prompt_guidance.model_profiles", "not-json"),
        ("prompt_guidance.model_profiles", '{"model": "large"}'),
        ("prompt_guidance.model_profiles.", "compact"),
        ("prompt_guidance.model_profiles. model", "compact"),
        ("prompt_guidance.model_profiles.model", "large"),
        ("prompt_guidance.model_profiles.model", "gpt-5"),
        ("prompt_guidance.guess_model_tier", "true"),
    ],
)
def test_invalid_profile_settings_fail_without_partial_config_changes(key: str, value: str) -> None:
    cfg = AppConfig(model="test-model")
    original = cfg.model_dump()
    with pytest.raises(ConfigError):
        set_config_value(cfg, key, value)
    assert cfg.model_dump() == original


def test_profile_mapping_is_independent_and_round_trips_without_saved_config_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    cfg = AppConfig(model="test-model")
    set_config_value(cfg, "prompt_guidance.default", "expanded")
    set_config_value(cfg, "prompt_guidance.model_profiles.private-model", "compact")
    save_config(cfg)
    saved = (tmp_path / "config.json").read_bytes()
    loaded = load_config()
    assert loaded.prompt_guidance == cfg.prompt_guidance
    copy = clone_cfg(loaded)
    copy.prompt_guidance.model_profiles["private-model"] = "balanced"
    assert loaded.prompt_guidance.model_profiles["private-model"] == "compact"
    assert "private-model" not in AppConfig().prompt_guidance.model_profiles
    assert (tmp_path / "config.json").read_bytes() == saved
    assert json.loads(saved)["prompt_guidance"]["default"] == "expanded"


@pytest.mark.parametrize(
    "values",
    [
        {"default": "automatic"},
        {"model_profiles": {"": "balanced"}},
        {"model_profiles": {"model ": "balanced"}},
        {"model_profiles": {"model": "large"}},
        {"defaults": "balanced"},
    ],
)
def test_profile_config_rejects_invalid_file_values(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PromptGuidanceConfig.model_validate(values)
