from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_compat import attach_provider_metadata_to_assistant_message
from alysis_code.model_reasoning import hosted_model_capabilities, model_reasoning_capabilities
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.reasoning_contracts import ALWAYS_ON, reasoning_contract_for


@pytest.fixture(autouse=True)
def configured_hosted_endpoint(monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api")


def hosted_config():
    preset = get_preset("alysis")
    cfg = AppConfig(model="glm-5.3-flash", base_url=preset.base_url)
    profile = replace(
        make_profile_from_preset(preset, name="alysis"), default_model="glm-5.3-flash"
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, "alysis")
    return cfg


def test_hosted_glm_metadata_and_reasoning_match_the_gateway():
    cfg = hosted_config()
    meta = ModelRegistry(cfg=cfg).get("glm-5.3-flash")
    assert meta.context_window_tokens == 1_000_000
    assert meta.max_output_tokens == 131_072
    assert meta.supports_vision is False
    assert meta.input_cost_per_token == 0.00000015
    assert meta.cache_read_input_cost_per_token == 0.00000003
    assert meta.output_cost_per_token == 0.0000005
    contract = reasoning_contract_for("alysis", "glm-5.3-flash")
    assert contract.mode == ALWAYS_ON
    assert contract.replay_reasoning_content
    assert contract.values == ("low", "high", "max")
    assert contract.default == "max"


@pytest.mark.parametrize(
    "model,values,default",
    [
        ("glm-5.3-flash", ("low", "high", "max"), "max"),
        ("glm-5.3", ("low", "high", "max"), "max"),
    ],
)
def test_overlapping_hosted_glm_ids_keep_their_exact_account_and_picker_efforts(
    model, values, default
):
    cfg = hosted_config()
    cfg.model = model
    for capabilities in (hosted_model_capabilities(model), model_reasoning_capabilities(cfg)):
        assert tuple(row["id"] for row in capabilities["reasoning_efforts"]) == values
        assert capabilities["default_reasoning_effort"] == default


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_hosted_glm_factory_round_trips_tool_reasoning_without_provider_key(stream, effort):
    captured = []
    tool = {
        "id": "call_one",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
    }

    def handle(request):
        body = json.loads(request.content)
        captured.append(body)
        assert request.headers["authorization"] == "Bearer slk_test"
        assert body["model"] == "glm-5.3-flash"
        assert body["reasoning_effort"] == effort
        assert body.get("thinking", {}).get("type") != "disabled"
        if len(captured) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Inspect the file.",
                "tool_calls": [tool],
            }
            if stream:
                message["tool_calls"][0] = {"index": 0, **tool}
                event = {"choices": [{"delta": message, "finish_reason": "tool_calls"}]}
                return httpx.Response(200, text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")
            return httpx.Response(
                200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]}
            )
        previous = body["messages"][1]
        assert previous["reasoning_content"] == "Inspect the file."
        assert previous["tool_calls"][0]["id"] == "call_one"
        assert "_alysis_provider_metadata" not in previous
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done."}}]})

    client = make_llm_client(
        cfg=hosted_config(),
        api_key="slk_test",
        model="glm-5.3-flash",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handle),
    )
    history = [{"role": "user", "content": "Read README.md"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    first = client.chat(messages=history, tools=tools, stream=stream)
    assistant = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content, "tool_calls": [tool]}, first
    )
    assert "reasoning_content" not in assistant
    second = client.chat(
        messages=[
            *history,
            assistant,
            {"role": "tool", "tool_call_id": "call_one", "content": "README content"},
        ],
        tools=tools,
    )
    assert second.content == "Done."
