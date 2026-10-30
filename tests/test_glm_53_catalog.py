"""GLM 5.3 has exact effort controls and retains its hosted account boundary."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code.cli import app
from alysis_code.cli_impl import config_menu
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.model_reasoning import hosted_model_capabilities, invocation_reasoning_effort
from alysis_code.model_registry import canonical_model_id, catalog_model_capabilities
from alysis_code.profile_presets import (
    get_preset,
    make_profile_from_preset,
    model_options_for_preset,
)
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.provider_auth.base import ProviderAuthError
from alysis_code.reasoning_contracts import ALWAYS_ON, UNKNOWN_CONTRACT, reasoning_contract_for

EFFORTS = ("low", "high", "max")
ROUTES = (("alysis", "glm-5.3", 16_384), ("together", "zai-org/GLM-5.3", 131_072))


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", "https://backend.example.invalid/functions/v1/llm/v1")
    for key in ("ALYSIS_API_KEY", "TOGETHER_API_KEY", "ALYSIS_RUN_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)


def config(preset, model):
    profile = replace(make_profile_from_preset(get_preset(preset)), default_model=model)
    cfg = AppConfig(model=model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_glm_53_is_present_but_requires_the_authenticated_alpha_inventory():
    preset = get_preset("alysis")
    assert "glm-5.3" in preset.suggested_models
    assert "glm-5.3" in preset.paid_models
    for inventory in (None, set(), {"glm-5.3-flash"}):
        rows = model_options_for_preset(preset, available_models=inventory)
        assert "glm-5.3" not in {row[0] for row in rows}
    rows = model_options_for_preset(preset, available_models={"glm-5.3"})
    assert [row[0] for row in rows] == ["glm-5.3"]
    assert rows[0][1] == "GLM 5.3"
    assert "1M context" in rows[0][2] and "Alpha" in rows[0][2]


@pytest.mark.parametrize("preset,model,output", ROUTES)
def test_glm_53_catalog_capacities_and_exact_efforts(preset, model, output):
    provider = get_preset(preset).provider_key
    assert canonical_model_id(model, provider) == "glm-5.3"
    metadata = catalog_model_capabilities(model, provider)
    assert metadata["context_window_tokens"] == 1_000_000
    assert metadata["max_output_tokens"] == output
    assert metadata["supports_vision"] is False
    assert metadata["supports_reasoning"] is True
    contract = reasoning_contract_for(provider, model)
    assert contract.mode == ALWAYS_ON and not contract.toggleable
    assert contract.values == EFFORTS and contract.default == "max"
    assert contract.emits_flat_reasoning_effort and contract.replay_reasoning_content
    labels = config_menu._thinking_labels_for_state(ConfigFlow(cfg=config(preset, model)).state)
    assert set(labels) == {"auto", *EFFORTS}
    for invalid in ("none", "medium", "xhigh", "ultra"):
        with pytest.raises(ProviderAuthError, match="not supported"):
            invocation_reasoning_effort(config(preset, model), invalid)


@pytest.mark.parametrize("preset,model,output", ROUTES)
@pytest.mark.parametrize("effort", (*EFFORTS, "auto"))
def test_glm_53_forwards_selected_effort_and_replays_reasoning(preset, model, output, effort):
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
                            "content": "OK",
                            "reasoning_content": "opaque original reasoning",
                        }
                    }
                ]
            },
        )

    cfg = config(preset, model)
    client = make_llm_client(
        cfg=cfg,
        api_key="synthetic-test-only",
        model=model,
        reasoning_effort=invocation_reasoning_effort(cfg, effort),
        transport=httpx.MockTransport(handle),
    )
    history = [{"role": "user", "content": "fixture"}]
    answer = client.chat(messages=history)
    if effort == "auto":
        assert "reasoning_effort" not in captured[0]
    else:
        assert captured[0]["reasoning_effort"] == effort
    assert "thinking" not in captured[0]
    assert "reasoning" not in captured[0]
    client.chat(
        messages=[
            *history,
            assistant_message_from_response(answer),
            {"role": "user", "content": "follow up"},
        ]
    )
    assert captured[1]["messages"][1]["reasoning_content"] == "opaque original reasoning"


def test_json_export_exposes_together_efforts_and_preserves_flash_contract():
    result = CliRunner().invoke(app, ["profile", "presets", "--json"])
    assert result.exit_code == 0, result.exception
    together = next(row for row in json.loads(result.stdout) if row["key"] == "together")
    row = next(model for model in together["models"] if model["id"] == "zai-org/GLM-5.3")
    assert row["context_window_tokens"] == 1_000_000
    assert row["max_output_tokens"] == 131_072
    assert tuple(item["id"] for item in row["reasoning_efforts"]) == EFFORTS
    assert row["default_reasoning_effort"] == "max"
    # The specific Alysis Flash rule remains distinct and precedes the base prefix.
    for model in ("glm-5.3", "glm-5.3-flash"):
        capabilities = hosted_model_capabilities(model)
        assert tuple(item["id"] for item in capabilities["reasoning_efforts"]) == EFFORTS
        assert capabilities["default_reasoning_effort"] == "max"
    assert hosted_model_capabilities("glm-5.3-flash")["max_output_tokens"] == 131_072
    assert reasoning_contract_for("together", "zai-org/GLM-5.3-Flash") is UNKNOWN_CONTRACT
