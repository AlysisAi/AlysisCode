"""Verify capability validation through the real LLM factory and HTTP payload."""

import json

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.model_reasoning import model_reasoning_capabilities
from alysis_code.profiles import ProfileSpec
from alysis_code.provider_auth.base import ProviderAuthError


def config(model="gpt-6-astra", *, url="https://api.openai.com/v1", protocol="openai_responses"):
    profile = ProfileSpec(
        name="fixture",
        protocol=protocol,
        base_url=url,
        default_model=model,
        reasoning_effort="high",
    )
    cfg = AppConfig(model=model, llm_enable_thinking=False)
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    return cfg


@pytest.mark.parametrize("effort", ["auto", "low", "medium", "high", "xhigh", "max"])
def test_exact_effort_reaches_request_without_mutating_defaults(monkeypatch, effort):
    cfg = config()
    before = cfg.model_dump(mode="json")
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", effort)
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "fixture-response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
            },
        )

    client = make_llm_client(
        cfg=cfg,
        api_key="fixture-key",
        model=cfg.model,
        transport=httpx.MockTransport(handler),
        enable_thinking=False,
        reasoning_effort="high",
    )
    client.chat(messages=[{"role": "user", "content": "offline fixture"}])
    assert captured
    reasoning = captured[0].get("reasoning", {})
    if effort == "auto":
        assert "effort" not in reasoning
    else:
        assert reasoning["effort"] == effort
    assert cfg.model_dump(mode="json") == before


def test_secondary_model_does_not_inherit_selected_models_effort(monkeypatch):
    cfg = config("gpt-6-sol")  # selected model permits none
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "none")
    secondary = make_llm_client(
        cfg=cfg,
        api_key="fixture-key",
        model="gpt-6-astra",
        enable_thinking=False,
        reasoning_effort="none",
    )
    assert secondary.reasoning_effort is None and secondary.enable_thinking is None
    client = make_llm_client(cfg=cfg, api_key="fixture-key", model="gpt-6-sol")
    assert client.reasoning_effort == "none"
    cfg.model = "gpt-6-astra"
    with pytest.raises(ProviderAuthError, match="not supported"):
        make_llm_client(cfg=cfg, api_key="fixture-key", model=cfg.model)


@pytest.mark.parametrize(
    "cfg",
    [
        config("unknown-model"),
        config(url="https://another-provider.test/v1"),
        config(protocol="openai_compat"),
    ],
)
def test_unknown_route_has_no_invented_options_and_rejects_override(monkeypatch, cfg):
    assert model_reasoning_capabilities(cfg)["reasoning_efforts"] == []
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "max")
    with pytest.raises(ProviderAuthError, match="not supported"):
        make_llm_client(cfg=cfg, api_key="fixture-key", model=cfg.model)


def test_automatic_still_works_on_unknown_route(monkeypatch):
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "auto")
    client = make_llm_client(
        cfg=config("unknown-model"), api_key="fixture-key", model="unknown-model"
    )
    assert client.reasoning_effort is None and client.enable_thinking is None
