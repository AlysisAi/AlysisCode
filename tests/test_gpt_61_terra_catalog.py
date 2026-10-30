"""Terra and the October additions publish capacities and keep route-specific settings."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code.cli import app
from alysis_code.cli_impl import config_menu
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig, load_config, save_persisted_profile_key
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.model_reasoning import hosted_model_capabilities, model_reasoning_capabilities
from alysis_code.model_registry import ModelRegistry, catalog_model_capabilities
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth.base import ProviderAuthError, ProviderReasoningEffort

MODEL = "gpt-6.1-terra"
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
CAPACITIES = (
    (MODEL, 1_050_000, 128_000),
    ("minimax-m3.1-flash", 1_000_000, 16_384),
    ("reflection-beam", 1_000_000, 16_384),
    ("nemotron-3.5-super-vl", 262_144, 16_384),
    ("mistral-large-4", 1_000_000, 131_072),
)


@pytest.fixture(autouse=True)
def disposable_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", "https://backend.example.invalid/functions/v1/llm/v1")
    for key in ("ALYSIS_API_KEY", "OPENAI_API_KEY", "ALYSIS_RUN_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)


def config(preset, *, effort=None):
    profile = replace(make_profile_from_preset(get_preset(preset)), default_model=MODEL)
    cfg = AppConfig(model=MODEL, base_url=profile.base_url, llm_reasoning_effort=effort)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


@pytest.mark.parametrize("preset", ("alysis", "openai", "openai-responses"))
@pytest.mark.parametrize("effort", (*EFFORTS, "auto"))
def test_terra_settings_persist_and_send_responses_tools(monkeypatch, preset, effort):
    cfg = config(preset, effort="ultra")
    monkeypatch.setattr(
        config_menu,
        "discover_provider_models",
        lambda **kwargs: (config_menu.ProviderModelOption(id=MODEL, label="GPT-6.1 Terra"),),
    )
    save_persisted_profile_key(preset, "synthetic-disposable-test-only")
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert MODEL in {row.value for row in flow.screen().rows}
    flow.choose(MODEL)
    if flow.stage == "model_base_url":
        flow.submit_input("")
    assert {row.value for row in flow.screen().rows} == {
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "auto",
    }
    chosen = "off" if effort == "none" else effort
    flow.choose(chosen)
    flow.choose("__save__")
    flow.run_busy()
    assert flow.saved, flow.status
    cfg = load_config()
    reopened = ConfigFlow(cfg=cfg)
    reopened.choose("default")
    reopened.choose(MODEL)
    if reopened.stage == "model_base_url":
        reopened.submit_input("")
    assert next(row for row in reopened.screen().rows if row.value == chosen).current
    capabilities = model_reasoning_capabilities(cfg)
    assert [row["id"] for row in capabilities["reasoning_efforts"]] == list(EFFORTS)
    assert capabilities["default_reasoning_effort"] == "medium"
    assert capabilities["context_window_tokens"] == 1_050_000
    assert capabilities["max_output_tokens"] == 128_000
    captured = []

    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert request.url.path.endswith("/v1/responses")
        assert payload["model"] == MODEL
        assert payload["tools"][0]["name"] == "read_file"
        assert payload.get("reasoning", {}).get("effort") == (None if effort == "auto" else effort)
        assert "reasoning_effort" not in payload
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
            },
        )

    client = make_llm_client(
        cfg=cfg, api_key="synthetic-test-only", model=MODEL, transport=httpx.MockTransport(handle)
    )
    assert isinstance(client, OpenAIResponsesClient)
    assert (
        client.chat(
            messages=[{"role": "user", "content": "fixture"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
        ).content
        == "OK"
    )
    assert len(captured) == 1


@pytest.mark.parametrize("preset", ("alysis", "openai", "openai-responses"))
def test_terra_prices_limits_and_invalid_effort(preset):
    from alysis_code.model_reasoning import invocation_reasoning_effort

    metadata = ModelRegistry(cfg=config(preset)).get(MODEL, include_provider_auth=False)
    assert metadata.context_window_tokens == 1_050_000
    assert metadata.max_output_tokens == 128_000
    assert metadata.supports_vision is (preset != "alysis")
    assert metadata.input_cost_per_token == 1 / 1_000_000
    assert metadata.output_cost_per_token == 5 / 1_000_000
    assert metadata.cache_read_input_cost_per_token == pytest.approx(0.05 / 1_000_000)
    for invalid in ("minimal", "ultra"):
        with pytest.raises(ProviderAuthError, match="not supported"):
            invocation_reasoning_effort(config(preset), invalid)


@pytest.mark.parametrize("model,context,output", CAPACITIES)
def test_account_catalog_exposes_context_output_and_hosted_modalities(model, context, output):
    capabilities = hosted_model_capabilities(model)
    assert capabilities["context_window_tokens"] == context
    assert capabilities["max_output_tokens"] == output
    assert capabilities["supports_vision"] is False
    assert capabilities["input_modalities"] == ["text"]


def test_preset_json_carries_metadata_and_does_not_read_user_overrides_or_keys(monkeypatch):
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", "1234")
    monkeypatch.setenv("ALYSIS_MAX_OUTPUT_TOKENS", "12")
    monkeypatch.setenv("OPENAI_API_KEY", "catalog-secret-never-export")
    result = CliRunner().invoke(app, ["profile", "presets", "--json"])
    assert result.exit_code == 0, result.exception
    assert "catalog-secret-never-export" not in result.stdout
    catalog = {row["key"]: {m["id"]: m for m in row["models"]} for row in json.loads(result.stdout)}
    for preset in ("openai", "openai-responses"):
        terra = catalog[preset][MODEL]
        assert terra["label"] == "GPT-6.1 Terra"
        assert terra["protocol"] == "openai_responses"
        assert terra["context_window_tokens"] == 1_050_000
        assert terra["max_output_tokens"] == 128_000
        assert [row["id"] for row in terra["reasoning_efforts"]] == list(EFFORTS)
    for model, context, output in CAPACITIES[1:4]:
        upstream = {
            "minimax-m3.1-flash": "MiniMaxAI/MiniMax-M3.1-Flash",
            "reflection-beam": "reflection/Beam-1.0-501B-A23B",
            "nemotron-3.5-super-vl": "nvidia/NVIDIA-Nemotron-3.5-Super-VL",
        }[model]
        row = catalog["together"][upstream]
        assert row["context_window_tokens"] == context
        assert row["max_output_tokens"] == output
        assert row["reasoning_efforts"]
    mistral = catalog["mistral"]["mistral-large-4"]
    assert mistral["context_window_tokens"] == 1_000_000
    assert mistral["max_output_tokens"] == 32_768
    assert [row["id"] for row in mistral["reasoning_efforts"]] == ["low", "medium", "high"]
    # Static data cannot reveal paid Alysis rows without an account inventory.
    assert MODEL not in catalog["alysis"]
    unknown = catalog_model_capabilities("unreviewed-model", "alysis")
    assert unknown["context_window_tokens"] is None
    assert unknown["max_output_tokens"] is None


def test_subscription_capacity_is_not_replaced_by_larger_api_capacity(monkeypatch):
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        auth_provider="openai-codex",
        default_model=MODEL,
    )
    cfg = AppConfig(model=MODEL, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    candidate = SimpleNamespace(
        id=MODEL,
        context_window_tokens=100_000,
        max_output_tokens=12_345,
        input_modalities=("text",),
        default_reasoning_effort="low",
        reasoning_efforts=(ProviderReasoningEffort(id="low", label="Low"),),
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda *args, **kwargs: SimpleNamespace(list_models=lambda: (candidate,)),
    )
    capabilities = model_reasoning_capabilities(cfg)
    assert capabilities["context_window_tokens"] == 100_000
    assert capabilities["max_output_tokens"] == 12_345
    assert capabilities["input_modalities"] == ["text"]
