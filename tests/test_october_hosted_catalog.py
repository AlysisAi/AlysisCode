"""October catalog releases: selectable efforts survive settings and transport."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.cli_impl import config_menu
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig, load_config, save_persisted_profile_key
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.model_reasoning import (
    hosted_model_capabilities,
    invocation_reasoning_effort,
    model_reasoning_capabilities,
)
from alysis_code.model_registry import ModelRegistry, canonical_model_id
from alysis_code.profile_presets import (
    get_preset,
    make_profile_from_preset,
    model_options_for_preset,
)
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.provider_auth.base import ProviderAuthError
from alysis_code.reasoning_contracts import reasoning_contract_for

LADDER = ("low", "medium", "high", "xhigh", "max")
MODELS = (
    ("minimax-m3.1-flash", "MiniMaxAI/MiniMax-M3.1-Flash", "MiniMax M3.1 Flash", LADDER, 1_000_000),
    ("reflection-beam", "reflection/Beam-1.0-501B-A23B", "Reflection Beam", LADDER, 1_000_000),
    (
        "nemotron-3.5-super-vl",
        "nvidia/NVIDIA-Nemotron-3.5-Super-VL",
        "NVIDIA Nemotron 3.5 Super VL",
        ("low", "medium", "high"),
        262_144,
    ),
)
ROUTES = tuple(
    (preset, hosted if preset == "alysis" else upstream, efforts)
    for hosted, upstream, _label, efforts, _context in MODELS
    for preset in ("alysis", "together")
) + (("minimax", "MiniMax-M3.1-Flash", LADDER),)


@pytest.fixture(autouse=True)
def disposable_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", "https://backend.example.invalid/functions/v1/llm/v1")
    for key in (
        "ALYSIS_API_KEY",
        "TOGETHER_API_KEY",
        "MINIMAX_API_KEY",
        "ALYSIS_RUN_REASONING_EFFORT",
    ):
        monkeypatch.delenv(key, raising=False)


def config(preset, model, *, effort=None):
    profile = replace(make_profile_from_preset(get_preset(preset)), default_model=model)
    cfg = AppConfig(model=model, base_url=profile.base_url, llm_reasoning_effort=effort)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


@pytest.mark.parametrize("hosted,upstream,label,efforts,context", MODELS)
def test_catalog_identity_names_and_route_limits(hosted, upstream, label, efforts, context):
    for preset, model in (("alysis", hosted), ("together", upstream)):
        rows = model_options_for_preset(get_preset(preset), available_models={hosted})
        assert [row[0] for row in rows].count(model) == 1
        assert next(row[1] for row in rows if row[0] == model) == label
        assert canonical_model_id(model, preset) == hosted
        metadata = ModelRegistry(cfg=config(preset, model)).get(model, include_provider_auth=False)
        assert metadata.context_window_tokens == context
        assert metadata.max_output_tokens == 16_384
        assert metadata.supports_reasoning is True
        assert metadata.supports_vision is (preset == "together" and hosted != "reflection-beam")
        assert (
            "canonical_model_catalog" in metadata.field_sources["supports_reasoning"]
            or preset == "alysis"
        )
    assert [row["id"] for row in hosted_model_capabilities(hosted)["reasoning_efforts"]] == list(
        efforts
    )
    # A gateway's output caps, modalities and prices must not contaminate custom endpoints.
    metadata = ModelRegistry(
        cfg=AppConfig(model=upstream, base_url="http://localhost:8123/v1")
    ).get(upstream, include_provider_auth=False)
    assert metadata.context_window_tokens == (
        1_000_000 if hosted.startswith("minimax") else 1_048_576
    )
    assert metadata.input_cost_per_token is None


@pytest.mark.parametrize(
    "preset,model,effort",
    [(preset, model, effort) for preset, model, efforts in ROUTES for effort in (*efforts, "auto")],
)
def test_effort_picker_persists_and_reaches_request(monkeypatch, preset, model, effort):
    cfg = config(preset, model, effort="ultra")
    expected = reasoning_contract_for(preset, model).values
    monkeypatch.setattr(
        config_menu,
        "discover_provider_models",
        lambda **kwargs: (config_menu.ProviderModelOption(id=model, label=model),),
    )
    save_persisted_profile_key(preset, "synthetic-disposable-test-only")
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert model in {row.value for row in flow.screen().rows}
    flow.choose(model)
    if flow.stage == "model_base_url":
        flow.submit_input("")
    assert {row.value for row in flow.screen().rows} == {"auto", *expected}
    flow.choose(effort)
    flow.choose("__save__")
    flow.run_busy()
    assert flow.saved, flow.status
    cfg = load_config()
    assert invocation_reasoning_effort(cfg, effort) == (None if effort == "auto" else effort)
    assert [row["id"] for row in model_reasoning_capabilities(cfg)["reasoning_efforts"]] == list(
        expected
    )
    reopened = ConfigFlow(cfg=cfg)
    reopened.choose("default")
    reopened.choose(model)
    if reopened.stage == "model_base_url":
        reopened.submit_input("")
    assert next(row for row in reopened.screen().rows if row.value == effort).current
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
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

    client = make_llm_client(
        cfg=cfg, api_key="synthetic-test-only", model=model, transport=httpx.MockTransport(handle)
    )
    answer = client.chat(messages=[{"role": "user", "content": "fixture"}])
    if effort == "auto":
        assert "reasoning_effort" not in sent[0]
    else:
        assert sent[0]["reasoning_effort"] == effort
    assert "thinking" not in sent[0]
    assert "reasoning" not in sent[0]
    client.chat(
        messages=[
            {"role": "system", "content": "initial instruction"},
            {"role": "user", "content": "fixture"},
            assistant_message_from_response(answer),
            {"role": "system", "content": "final instruction"},
            {"role": "user", "content": "follow up"},
        ]
    )
    replay = next(row for row in sent[1]["messages"] if row["role"] == "assistant")
    assert replay["reasoning_content"] == "opaque original reasoning"
    if model in {"reflection-beam", "reflection/Beam-1.0-501B-A23B"}:
        assert sent[1]["messages"][0]["content"] == "initial instruction\n\nfinal instruction"
        assert not client.preserves_late_system_message_position


@pytest.mark.parametrize("preset,model,efforts", ROUTES)
def test_invalid_efforts_and_off_are_rejected_before_request(preset, model, efforts):
    for effort in ("none", "minimal", "ultra", *(item for item in LADDER if item not in efforts)):
        with pytest.raises(ProviderAuthError, match="not supported"):
            invocation_reasoning_effort(config(preset, model), effort)


def test_flash_does_not_inherit_older_minimax_toggle_contract():
    native = reasoning_contract_for("minimax", "MiniMax-M3.1-Flash")
    assert native.values == LADDER and native.default == "max"
    assert native.replay_reasoning_content
    assert not native.toggleable
    assert reasoning_contract_for("minimax", "MiniMax-M3").toggleable
    assert reasoning_contract_for("alysis", "minimax-m3.1-flash").default == ""
