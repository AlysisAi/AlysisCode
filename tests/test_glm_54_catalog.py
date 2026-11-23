"""GLM-5.4 is a reviewed GLM Coding Plan model that reads images and keeps the plan's effort rules."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from alysis_code.assets.local_tools import LocalAssetViewer
from alysis_code.bots.browser_images import declared_image_support
from alysis_code.cli import app
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.model_reasoning import invocation_reasoning_effort
from alysis_code.model_registry import canonical_model_id, catalog_model_capabilities
from alysis_code.profile_presets import (
    get_preset,
    make_profile_from_preset,
    model_options_for_preset,
)
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.provider_auth.base import ProviderAuthError
from alysis_code.reasoning_contracts import ALWAYS_ON, OFF_IMPOSSIBLE, reasoning_contract_for

EFFORTS = ("low", "high", "max")
PLAN = "zai-coding-plan"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    for key in ("ZAI_API_KEY", "ALYSIS_RUN_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)


def config(preset_key, model):
    profile = replace(make_profile_from_preset(get_preset(preset_key)), default_model=model)
    cfg = AppConfig(model=model, base_url=profile.base_url, stream=False)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_coding_plan_offers_glm_54_first_and_new_profiles_default_to_it():
    preset = get_preset(PLAN)
    assert preset.suggested_models == ("glm-5.4", "glm-5.3", "glm-5.3-flash")
    assert make_profile_from_preset(preset).default_model == "glm-5.4"
    assert "reads images" in preset.suggested_model_descriptions["glm-5.4"]
    # The cheapest plan model still checks a new key.
    assert preset.validation_model == "glm-5.3-flash"


def test_glm_54_stays_on_the_plan_and_is_not_offered_on_metered_apis():
    for key in ("zai", "zhipu"):
        assert "glm-5.4" not in get_preset(key).suggested_models


def test_glm_54_capacity_and_vision_come_from_the_catalog():
    provider = get_preset(PLAN).provider_key
    assert canonical_model_id("glm-5.4", provider) == "glm-5.4"
    metadata = catalog_model_capabilities("glm-5.4", provider)
    assert metadata["context_window_tokens"] == 1_000_000
    assert metadata["max_output_tokens"] == 131_072
    assert metadata["supports_vision"] is True
    assert metadata["supports_reasoning"] is True
    # The previous plan model keeps its text-only declaration.
    assert catalog_model_capabilities("glm-5.3", provider)["supports_vision"] is False


@pytest.mark.parametrize("model,delivered", (("glm-5.4", True), ("glm-5.3", False)))
def test_images_reach_the_model_only_when_it_declares_vision(tmp_path, model, delivered):
    Image.new("RGB", (6, 4), "red").save(tmp_path / "diagram.png")
    viewer = LocalAssetViewer(root=tmp_path, cfg=config(PLAN, model))
    result = viewer.view({"path": "diagram.png"})
    if delivered:
        message = viewer.take_visual_message(result["visual_delivery_id"])
        assert message["content"][0]["image_url"]["url"].startswith("data:image/")
    else:
        assert result["status"] == "tool_unavailable"


def test_glm_54_reasons_unconditionally_with_the_plan_effort_levels():
    contract = reasoning_contract_for("zai_coding_plan", "glm-5.4")
    assert contract.mode == ALWAYS_ON and contract.off == OFF_IMPOSSIBLE
    assert contract.values == EFFORTS and contract.default == "max"
    assert contract.emits_flat_reasoning_effort and contract.replay_reasoning_content
    for invalid in ("none", "medium", "xhigh"):
        with pytest.raises(ProviderAuthError, match="not supported"):
            invocation_reasoning_effort(config(PLAN, "glm-5.4"), invalid)


@pytest.mark.parametrize("effort", (*EFFORTS, "auto"))
def test_glm_54_forwards_the_effort_and_replays_its_reasoning(effort):
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Γεια",
                            "reasoning_content": "opaque original reasoning",
                        }
                    }
                ]
            },
        )

    cfg = config(PLAN, "glm-5.4")
    client = make_llm_client(
        cfg=cfg,
        api_key="synthetic-test-only",
        model="glm-5.4",
        reasoning_effort=invocation_reasoning_effort(cfg, effort),
        transport=httpx.MockTransport(handle),
    )
    history = [{"role": "user", "content": "Πες γεια"}]
    answer = client.chat(messages=history)
    assert captured[0]["model"] == "glm-5.4"
    if effort == "auto":
        assert "reasoning_effort" not in captured[0]
    else:
        assert captured[0]["reasoning_effort"] == effort
    assert "thinking" not in captured[0]
    client.chat(
        messages=[
            *history,
            assistant_message_from_response(answer),
            {"role": "user", "content": "follow up"},
        ]
    )
    assert captured[1]["messages"][1]["reasoning_content"] == "opaque original reasoning"


def test_the_app_preset_list_carries_glm_54_with_its_capacities_and_efforts():
    result = CliRunner().invoke(app, ["profile", "presets", "--json"])
    assert result.exit_code == 0, result.exception
    plan = next(row for row in json.loads(result.stdout) if row["key"] == PLAN)
    assert plan["default_model"] == "glm-5.4"
    row = next(model for model in plan["models"] if model["id"] == "glm-5.4")
    assert row["label"] == "GLM 5.4"
    assert row["context_window_tokens"] == 1_000_000
    assert row["max_output_tokens"] == 131_072
    assert row["supports_vision"] is True
    assert tuple(item["id"] for item in row["reasoning_efforts"]) == EFFORTS
    assert row["default_reasoning_effort"] == "max"


def test_owls_on_a_coding_plan_connection_can_pick_glm_54_and_show_it_pages():
    preset = get_preset(PLAN)
    assert "glm-5.4" in {model for model, _label, _description in model_options_for_preset(preset)}
    # Owls send browser screenshots only to models the catalog declares as seeing images.
    assert declared_image_support("glm-5.4", preset.base_url) is True
    assert declared_image_support("glm-5.3", preset.base_url) is False
