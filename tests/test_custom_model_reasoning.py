"""Catalog-backed custom routes expose only options that reach the actual request."""

import json

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.model_reasoning import model_reasoning_capabilities
from alysis_code.model_registry import ModelRegistry
from alysis_code.profiles import ProfileSpec
from alysis_code.provider_auth.base import ProviderAuthError


def config(model="glm-5.3-flash", url="https://custom.example/v1", **kwargs):
    profile = ProfileSpec(name="custom", base_url=url, default_model=model, **kwargs)
    cfg = AppConfig(model=model)
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    return cfg


def reply():
    return httpx.Response(
        200,
        json={
            "id": "fixture",
            "status": "completed",
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
        },
    )


@pytest.mark.parametrize("model", ["glm-5.3-flash", "z-ai/glm-5.3-flash", "zai-org/GLM-5.3-Flash"])
@pytest.mark.parametrize("effort", ["low", "high", "max", "auto"])
def test_glm_catalog_options_reach_custom_endpoint(monkeypatch, model, effort):
    cfg = config(model)
    before = cfg.model_dump(mode="json")
    caps = model_reasoning_capabilities(cfg)
    assert [item["id"] for item in caps["reasoning_efforts"]] == ["low", "high", "max"]
    assert caps["default_reasoning_effort"] == "max"
    meta = ModelRegistry(cfg=cfg).get(model)
    assert (meta.context_window_tokens, meta.max_output_tokens) == (1_000_000, 131_072)
    assert meta.supports_vision and meta.supports_reasoning
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", effort)
    requests = []

    def handler(request):
        requests.append(request)
        return reply()

    make_llm_client(
        cfg=cfg, model=model, api_key="fixture", transport=httpx.MockTransport(handler)
    ).chat(messages=[{"role": "user", "content": "fixture"}])
    payload = json.loads(requests[-1].content)
    assert payload["model"] == model
    assert str(requests[-1].url) == "https://custom.example/v1/chat/completions"
    assert payload.get("reasoning_effort") == (None if effort == "auto" else effort)
    assert "thinking" not in payload
    assert cfg.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "model,protocol,effort,expected",
    [
        (
            "qwen3.8-flash",
            "openai_compat",
            "xhigh",
            {"reasoning_effort": "xhigh", "enable_thinking": True},
        ),
        ("kimi-k3", "openai_compat", "low", {"reasoning_effort": "low"}),
        ("gpt-6-astra", "openai_responses", "high", {"reasoning": {"effort": "high"}}),
    ],
)
def test_other_catalog_models_use_their_own_wire(monkeypatch, model, protocol, effort, expected):
    cfg = config(model, protocol=protocol)
    assert effort in {item["id"] for item in model_reasoning_capabilities(cfg)["reasoning_efforts"]}
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", effort)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return reply()

    make_llm_client(
        cfg=cfg, model=model, api_key="fixture", transport=httpx.MockTransport(handler)
    ).chat(messages=[{"role": "user", "content": "fixture"}])
    for key, value in expected.items():
        assert requests[-1][key] == value


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        {},
        {"supported_efforts": ["high"], "mandatory": True},
        {"supported_efforts": [], "mandatory": True},
    ],
)
def test_openrouter_model_defaults_and_explicit_overrides(monkeypatch, catalog):
    cfg = config("z-ai/glm-5.3-flash", url="https://openrouter.ai/api/v1")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/models"):
            row = {"id": cfg.model}
            if catalog is not None:
                row["reasoning"] = catalog
            return httpx.Response(200, json={"data": [row]})
        return reply()

    transport = httpx.MockTransport(handler)
    caps = model_reasoning_capabilities(cfg, transport=transport)
    expected = (
        ["low", "high", "max"] if catalog is None or catalog == {} else catalog["supported_efforts"]
    )
    assert [item["id"] for item in caps["reasoning_efforts"]] == expected
    meta = ModelRegistry(cfg=cfg).get(cfg.model)
    assert meta.context_window_tokens == 1_310_720  # Existing OpenRouter route override.
    assert meta.max_output_tokens == 131_072
    if expected:
        monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", expected[0])
        make_llm_client(cfg=cfg, model=cfg.model, api_key="fixture", transport=transport).chat(
            messages=[{"role": "user", "content": "fixture"}]
        )
        payload = json.loads(requests[-1].content)
        assert payload["reasoning"] == {"effort": expected[0]}
        assert "reasoning_effort" not in payload


@pytest.mark.parametrize("adapter", ["none", "openai_compat_passive"])
def test_explicit_disabled_adapter_stays_disabled(adapter):
    assert (
        model_reasoning_capabilities(config(reasoning_trace_adapter=adapter))["reasoning_efforts"]
        == []
    )


@pytest.mark.parametrize("effort", ["medium", "none"])
def test_invalid_glm_choice_rejected_before_request(monkeypatch, effort):
    cfg = config()
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", effort)
    with pytest.raises(ProviderAuthError, match="not supported"):
        make_llm_client(
            cfg=cfg,
            model=cfg.model,
            api_key="fixture",
            transport=httpx.MockTransport(lambda request: pytest.fail("network call")),
        )


def test_cli_picker_uses_model_catalog_for_custom_endpoint():
    from alysis_code.cli_impl.config_menu import ConfigMenuState, _thinking_labels_for_state

    state = ConfigMenuState(
        fields={"model": "glm-5.3-flash", "base_url": "https://custom.example/v1"},
        thinking_label="auto",
        role_models={},
        forge_role_models={},
        role_temperatures={},
        profiles={},
        active_profile="",
    )
    assert _thinking_labels_for_state(state) == ("low", "high", "max", "auto")


def test_openrouter_partial_metadata_preserves_mandatory_reasoning():
    from alysis_code.provider_model_catalog import openrouter_reasoning_contract

    contract = openrouter_reasoning_contract(
        {"mandatory": True, "default_effort": "low"}, model="qwen/qwen3.8-flash"
    )
    assert contract.values == ("low", "medium", "xhigh")
    assert contract.default == "low"
    assert not contract.toggleable


def test_openrouter_catalog_outage_keeps_known_model_options():
    cfg = config("z-ai/glm-5.3-flash", url="https://openrouter.ai/api/v1")
    caps = model_reasoning_capabilities(
        cfg, transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    assert [item["id"] for item in caps["reasoning_efforts"]] == ["low", "high", "max"]


def test_known_route_restrictions_are_not_replaced_with_native_model_contract():
    # NVIDIA's researched Kimi deployment offers only high, despite the native model's levels.
    cfg = config("moonshotai/kimi-k3", url="https://integrate.api.nvidia.com/v1")
    caps = model_reasoning_capabilities(cfg)
    assert "low" not in {item["id"] for item in caps["reasoning_efforts"]}


def test_unknown_custom_model_still_has_no_invented_options():
    assert model_reasoning_capabilities(config("unknown-model"))["reasoning_efforts"] == []
