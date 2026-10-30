"""Hosted Terra uses the verified Responses tool path without live HTTP."""

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_compat import attach_provider_metadata_to_assistant_message
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.model_reasoning import hosted_model_capabilities
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import (
    get_preset,
    make_profile_from_preset,
    model_options_for_preset,
)
from alysis_code.profiles import add_profile, set_active_profile


def test_portable_hosted_endpoint_is_materialized_and_an_unrelated_same_name_is_not_trusted(
    monkeypatch,
):
    from alysis_code.profile_presets import find_preset_for_profile

    endpoint = "https://paid-test.example.invalid/functions/v1/llm/v1"
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", endpoint)
    preset = get_preset("alysis")
    profile = make_profile_from_preset(preset)
    assert profile.base_url == endpoint
    assert find_preset_for_profile(profile).provider_key == "alysis"
    assert (
        find_preset_for_profile(replace(profile, base_url="https://unrelated.example.invalid/v1"))
        is None
    )


def config():
    profile = replace(make_profile_from_preset(get_preset("alysis")), default_model="gpt-6.1-terra")
    result = AppConfig(model=profile.default_model, base_url=profile.base_url)
    add_profile(result, profile)
    set_active_profile(result, profile.name)
    return result


def test_terra_remains_catalog_gated_and_exposes_the_verified_reasoning_contract():
    preset = get_preset("alysis")
    assert model_options_for_preset(preset, available_models=set()) == ()
    assert model_options_for_preset(preset, available_models={"gpt-6.1-terra"})[0][:2] == (
        "gpt-6.1-terra",
        "GPT-6.1 Terra",
    )
    capabilities = hosted_model_capabilities("gpt-6.1-terra")
    assert [row["id"] for row in capabilities["reasoning_efforts"]] == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert capabilities["default_reasoning_effort"] == "medium"
    meta = ModelRegistry(cfg=config()).get("gpt-6.1-terra")
    assert meta.context_window_tokens == 1_050_000
    assert meta.max_output_tokens == 128_000
    assert meta.supports_vision is False


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", ["none", "medium"])
def test_terra_sends_managed_responses_and_replays_reasoning_and_tools(stream, effort):
    captured = []

    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert request.url.path.endswith("/llm/v1/responses")
        assert request.headers["authorization"] == "Bearer slk_fixture"
        assert payload["model"] == "gpt-6.1-terra"
        assert payload["reasoning"]["effort"] == effort
        assert payload["store"] is False
        assert "previous_response_id" not in payload
        assert all(tool["type"] == "function" for tool in payload["tools"])
        if len(captured) == 1:
            output = [
                {
                    "id": "rs_fixture",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "opaque",
                },
                {
                    "id": "fc_fixture",
                    "type": "function_call",
                    "call_id": "call_fixture",
                    "name": "read_file",
                    "arguments": "{}",
                },
            ]
        else:
            assert any(item.get("encrypted_content") == "opaque" for item in payload["input"])
            assert any(item.get("type") == "function_call_output" for item in payload["input"])
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."}],
                }
            ]
        result = {"id": "resp_fixture", "status": "completed", "output": output}
        if payload.get("stream"):
            event = {"type": "response.completed", "response": result}
            return httpx.Response(
                200,
                text=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=result)

    client = make_llm_client(
        cfg=config(),
        api_key="slk_fixture",
        model="gpt-6.1-terra",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handle),
    )
    assert isinstance(client, OpenAIResponsesClient)
    history = [{"role": "user", "content": "Read README"}]
    tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    first = client.chat(messages=history, tools=tools, stream=stream)
    assistant = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content}, first
    )
    second = client.chat(
        messages=[
            *history,
            assistant,
            {"role": "tool", "tool_call_id": "call_fixture", "content": "file"},
        ],
        tools=tools,
        stream=stream,
    )
    assert second.content == "Done."
