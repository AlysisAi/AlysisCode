from __future__ import annotations

from dataclasses import replace

import pytest

from alysis_code import profile_presets
from alysis_code.agent.prompt_families import FAMILY_EXPANSIONS, FAMILY_WORKFLOWS
from alysis_code.agent.prompt_guidance import render_guidance
from alysis_code.chatgpt_codex_static_provider import load_chatgpt_codex_static_models
from alysis_code.config import AppConfig, resolve_prompt_guidance_profile
from alysis_code.profile_presets import PROFILE_PRESETS, ProfilePreset, get_preset
from alysis_code.prompt_guidance_catalog import (
    GUIDANCE_PROFILES,
    PROMPT_FAMILIES,
    ReviewedModel,
    build_model_prompt_catalog,
    get_model_prompt_catalog,
    profile_family,
)


def test_every_shipped_model_choice_has_a_reviewed_prompt_assignment() -> None:
    selectable = {model for preset in PROFILE_PRESETS for model in preset.suggested_models}
    selectable.update(model.id for model in load_chatgpt_codex_static_models())
    assert selectable <= get_model_prompt_catalog().keys()
    assert set(get_model_prompt_catalog().values()) <= set(GUIDANCE_PROFILES)


def test_model_metadata_rename_updates_picker_and_parent_and_child_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ReviewedModel("private/release-one", "qwen-normal")

    def declare(choice: ReviewedModel) -> ProfilePreset:
        return ProfilePreset(
            key="test-provider",
            label="Test provider",
            protocol="openai_compat",
            base_url="https://provider.example/v1",
            api_key_env=None,
            model_choices=(choice,),
        )

    original = declare(model)
    monkeypatch.setattr(profile_presets, "PROFILE_PRESETS", (original,))
    assert original.suggested_models == (model.id,)
    assert resolve_prompt_guidance_profile(AppConfig(model=model.id)) == "qwen-normal"

    renamed = replace(model, id="部署/next-release")
    updated = declare(renamed)
    monkeypatch.setattr(profile_presets, "PROFILE_PRESETS", (updated,))
    assert updated.suggested_models == (renamed.id,)
    cfg = AppConfig(model=renamed.id)
    assert resolve_prompt_guidance_profile(cfg) == "qwen-normal"
    assert resolve_prompt_guidance_profile(cfg, subagent=True) == "qwen-subagent"
    assert resolve_prompt_guidance_profile(AppConfig(model=model.id)) == "expanded"


def test_hosted_default_selects_reviewed_glm_parent_and_child_guidance() -> None:
    preset = get_preset("alysis")
    assert preset is not None
    assert preset.suggested_models[0] == "glm-5.3-flash"
    cfg = AppConfig(model=preset.suggested_models[0])
    assert resolve_prompt_guidance_profile(cfg) == "glm-normal"
    assert resolve_prompt_guidance_profile(cfg, subagent=True) == "glm-subagent"


def test_shared_model_identity_requires_consistent_prompt_assignment() -> None:
    model = ReviewedModel("shared/model", "qwen-normal")
    assert build_model_prompt_catalog([model, model])[model.id] == "qwen-normal"
    with pytest.raises(ValueError, match="Conflicting prompt assignments"):
        build_model_prompt_catalog([model, replace(model, prompt_profile="glm-normal")])


def test_shipped_model_without_reviewed_metadata_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset = ProfilePreset(
        key="test-provider",
        label="Test provider",
        protocol="openai_compat",
        base_url="https://provider.example/v1",
        api_key_env=None,
        suggested_models=("unreviewed-shipped-model",),
    )
    monkeypatch.setattr(profile_presets, "PROFILE_PRESETS", (preset,))
    with pytest.raises(ValueError, match="without reviewed prompt metadata"):
        get_model_prompt_catalog()


@pytest.mark.parametrize(
    ("model_id", "prompt_profile"),
    [("", "qwen-normal"), (" model ", "qwen-normal"), ("model", "qwen-subagent")],
)
def test_invalid_reviewed_model_metadata_is_rejected(model_id: str, prompt_profile: str) -> None:
    with pytest.raises(ValueError):
        ReviewedModel(model_id, prompt_profile)


def test_hosted_retirement_alias_does_not_reassign_direct_model_family() -> None:
    cfg = AppConfig(model="mimo-v2.5-pro")
    assert resolve_prompt_guidance_profile(cfg) == "mimo-normal"
    assert resolve_prompt_guidance_profile(cfg, subagent=True) == "mimo-subagent"


def test_default_prompt_assignments_are_independent_between_configs() -> None:
    first = AppConfig(model="deepseek-flash")
    first.prompt_guidance.model_profiles["deepseek-flash"] = "compact"
    second = AppConfig(model="deepseek-flash")
    assert resolve_prompt_guidance_profile(first) == "compact"
    assert resolve_prompt_guidance_profile(second) == "deepseek-expanded"


def test_family_templates_cover_the_complete_registered_family_set() -> None:
    assert set(FAMILY_WORKFLOWS) == set(FAMILY_EXPANSIONS) == set(PROMPT_FAMILIES)
    assert {profile_family(profile) for profile in get_model_prompt_catalog().values()} == set(
        PROMPT_FAMILIES
    )


@pytest.mark.parametrize("family", PROMPT_FAMILIES)
def test_expanded_adds_execution_help_without_replacing_family_or_evidence_contract(
    family: str,
) -> None:
    normal = render_guidance("workflow", f"{family}-normal")
    expanded = render_guidance("workflow", f"{family}-expanded")
    child = render_guidance("workflow", f"{family}-subagent")
    for prompt in (normal, expanded, child):
        assert prompt.count(FAMILY_WORKFLOWS[family]) == 1
        assert prompt.count("Evidence and delivery\n") == 1
    assert (
        normal.split("Evidence and delivery\n")[1] == expanded.split("Evidence and delivery\n")[1]
    )
    assert FAMILY_EXPANSIONS[family] in expanded
    assert FAMILY_EXPANSIONS[family] not in normal
    assert FAMILY_EXPANSIONS[family] not in child
    assert "Scoped assignment" in child
    assert "Scoped assignment" not in normal
    assert len(expanded) > len(normal)


@pytest.mark.parametrize(
    "model",
    [
        "private-unreviewed-model",
        "my-proxy/claude-sonnet-5",
        "new-gpt-5.6-sol-finetune",
        "openai/container",
        "vercel_ai_gateway/cohere/embed-v4.0",
    ],
)
def test_opaque_or_service_ids_do_not_infer_family_from_provider_or_name(model: str) -> None:
    cfg = AppConfig(model=model)
    assert resolve_prompt_guidance_profile(cfg) == "expanded"
    assert resolve_prompt_guidance_profile(cfg, subagent=True) == "general-subagent"
