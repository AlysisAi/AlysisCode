from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.factory import make_llm_client, resolve_model_protocol
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.reasoning_contracts import reasoning_contract_for
from alysis_code.tools.registry import iter_builtin_tool_metadata

MODEL = "claude-haiku-5-5"
pytestmark = pytest.mark.usefixtures("configured_hosted_api")


def config(provider="alysis"):
    profile = replace(make_profile_from_preset(get_preset(provider)), default_model=MODEL)
    cfg = AppConfig(model=MODEL, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_free_picker_metadata_and_efforts(monkeypatch):
    from alysis_code.cli_impl import config_menu

    monkeypatch.setattr(
        config_menu,
        "discover_provider_models",
        lambda **kwargs: (config_menu.ProviderModelOption(id=MODEL, label=MODEL),),
    )
    cfg = config()
    flow = ConfigFlow(cfg=cfg)
    flow.state.set_field("new_api_key", "slk_synthetic_free_catalog")
    flow.choose("default")
    assert MODEL in [r.value for r in flow.screen().rows]
    assert "claude-sonnet-5-5" not in [r.value for r in flow.screen().rows]
    for provider in ("alysis", "anthropic"):
        meta = ModelRegistry(cfg=config(provider)).get(MODEL)
        assert (meta.context_window_tokens, meta.max_output_tokens) == (1_000_000, 128_000)
        assert meta.supports_vision is (provider == "anthropic")
        assert meta.supports_reasoning is True
        assert meta.input_cost_per_token == 0.0000001
        assert meta.output_cost_per_token == 0.0000005
        assert meta.cache_read_input_cost_per_token == 0.00000001
        assert meta.cache_creation_input_cost_per_token == 0.000000125
        contract = reasoning_contract_for(provider, MODEL)
        assert contract.values == ("low", "medium", "high", "xhigh", "max")
        assert contract.default == "medium"
        assert contract.toggleable
    assert (
        resolve_model_protocol(provider_key="alysis", model=MODEL, protocol="openai_compat")
        == "anthropic_messages"
    )


@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high", "xhigh", "max"])
def test_all_efforts_and_full_coding_tools_use_native_messages(effort):
    cfg = config()
    cfg.llm_reasoning_effort = effort
    cfg.prompt_cache_mode = "auto"
    captured = []

    def handle(request):
        assert request.url.path.endswith("/v1/messages")
        assert request.headers["authorization"] == "Bearer slk_test"
        assert "x-api-key" not in request.headers
        body = json.loads(request.content)
        captured.append(body)
        assert "temperature" not in body
        assert "budget_tokens" not in json.dumps(body)
        assert body["cache_control"] == {"type": "ephemeral"}
        assert body["tool_choice"] == {"type": "any"}
        if effort == "none":
            assert body["thinking"] == {"type": "disabled"}
            assert body.get("output_config", {}).get("effort", "medium") not in {"xhigh", "max"}
        elif effort:
            assert body["thinking"]["type"] == "adaptive"
            assert body["output_config"]["effort"] == effort
        return httpx.Response(
            200,
            json={
                "type": "message",
                "model": MODEL,
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 1},
            },
        )

    client = make_llm_client(
        cfg=cfg, api_key="slk_test", model=MODEL, transport=httpx.MockTransport(handle)
    )
    assert isinstance(client, AnthropicMessagesClient)
    tools = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.copied_parameters(),
            },
        }
        for t in iter_builtin_tool_metadata()
    ]
    assert (
        client.chat(
            messages=[{"role": "user", "content": "Fix the bug."}],
            tools=tools,
            tool_choice="required",
        ).content
        == "Done."
    )


@pytest.mark.parametrize("label", ["auto", "off", "low", "medium", "high", "xhigh", "max"])
def test_picker_selection_round_trips_to_exact_effort(label):
    cfg = config()
    cfg.llm_reasoning_effort = "max"
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    flow.choose(MODEL)
    assert {r.value for r in flow.screen().rows} == {
        "auto",
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }
    flow.choose(label)
    flow.state.commit_to(cfg)
    restored = AppConfig.model_validate(cfg.model_dump())

    def handle(request):
        body = json.loads(request.content)
        effort = body.get("output_config", {}).get("effort")
        if label == "off":
            assert body["thinking"] == {"type": "disabled"}
            assert effort is None
        elif label == "auto":
            assert body.get("thinking", {}).get("type") in {None, "adaptive"}
            assert effort is None
        else:
            assert body["thinking"]["type"] == "adaptive"
            assert effort == label
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )

    client = make_llm_client(
        cfg=restored, api_key="slk_test", model=MODEL, transport=httpx.MockTransport(handle)
    )
    assert client.chat(messages=[{"role": "user", "content": "Hello"}]).content == "OK"


def test_direct_counting_preserves_haiku_forced_tool_choice():
    cfg = config("anthropic")
    cfg.llm_reasoning_effort = "high"

    def handle(request):
        assert request.url.path.endswith("/messages/count_tokens")
        body = json.loads(request.content)
        assert body["tool_choice"] == {"type": "any"}
        assert body["thinking"]["type"] == "adaptive"
        return httpx.Response(200, json={"input_tokens": 100})

    client = make_llm_client(
        cfg=cfg, api_key="test-key", model=MODEL, transport=httpx.MockTransport(handle)
    )
    count = client.count_input_tokens(
        messages=[{"role": "user", "content": "Read a file."}],
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="required",
    )
    assert count is not None
