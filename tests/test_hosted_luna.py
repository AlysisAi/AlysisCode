from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_compat import attach_provider_metadata_to_assistant_message
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import LLMError
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.tools.registry import iter_builtin_tool_metadata

pytestmark = pytest.mark.usefixtures("authorized_hosted_models")


def hosted_config():
    preset = get_preset("alysis")
    profile = replace(make_profile_from_preset(preset), default_model="gpt-6-luna")
    cfg = AppConfig(model=profile.default_model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_luna_authorized_picker_and_metadata():
    cfg = hosted_config()
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert "gpt-6-luna" in [r.value for r in flow.screen().rows]
    flow.choose("gpt-6-luna")
    flow.submit_input("")
    assert [r.value for r in flow.screen().rows] == [
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "auto",
    ]
    flow.choose("medium")
    assert flow.state.commit_to(cfg).saved
    meta = ModelRegistry(cfg=cfg).get("gpt-6-luna")
    assert meta.supports_vision is True
    assert meta.supports_reasoning is True
    assert meta.context_window_tokens == 1_050_000
    assert meta.max_output_tokens == 128_000
    assert meta.cache_creation_input_cost_per_token == 0.000000125


@pytest.mark.parametrize("effort", ["minimal", "ultra"])
def test_luna_picker_clears_unsupported_saved_effort(effort):
    cfg = hosted_config()
    cfg.llm_reasoning_effort = effort
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    flow.choose("gpt-6-luna")
    flow.submit_input("")
    assert [r.value for r in flow.screen().rows] == [
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "auto",
    ]
    assert flow.state.thinking_label == "auto"
    assert flow.state.commit_to(cfg).saved
    assert cfg.llm_reasoning_effort is None


@pytest.mark.parametrize("source", ["config", "env"])
@pytest.mark.parametrize("effort, expected", [("minimal", "low"), ("ultra", "max")])
@pytest.mark.parametrize("stream", [False, True])
def test_luna_normalizes_legacy_efforts_at_request_boundary(
    monkeypatch, source, effort, expected, stream
):
    cfg = hosted_config()
    if source == "env":
        monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", effort)
    else:
        cfg.llm_reasoning_effort = effort

    def handle(request):
        body = json.loads(request.content)
        assert body["reasoning"]["effort"] == expected
        result = {"id": "resp_effort", "status": "completed", "output": []}
        if stream:
            return httpx.Response(
                200,
                text="event: response.completed\ndata: "
                + json.dumps({"type": "response.completed", "response": result})
                + "\n\n",
            )
        return httpx.Response(200, json=result)

    client = make_llm_client(
        cfg=cfg, api_key="slk_test", model=cfg.model, transport=httpx.MockTransport(handle)
    )
    client.chat(messages=[{"role": "user", "content": "hi"}], stream=stream)


@pytest.mark.parametrize("effort", ["minimal", "ultra"])
def test_luna_legacy_effort_compatibility_does_not_change_other_responses_providers(effort):
    from alysis_code.llm.openai_responses import _responses_reasoning

    for provider, model in [("openai", "gpt-6-luna"), ("alysis", "other-model")]:
        assert _responses_reasoning(
            enable_thinking=True, reasoning_effort=effort, provider_key=provider, model=model
        ) == {"effort": effort}


@pytest.mark.parametrize("stream", [False, True])
def test_luna_uses_managed_responses_and_stateless_tool_reasoning_replay(stream):
    captured = []
    output = [
        {"id": "rs_test", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
        {
            "id": "fc_test",
            "type": "function_call",
            "call_id": "call_test",
            "name": "read_file",
            "arguments": "{}",
        },
    ]

    def handle(request):
        body = json.loads(request.content)
        captured.append(body)
        assert request.url.path.endswith("/llm/v1/responses")
        assert request.headers["authorization"] == "Bearer slk_test"
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning"]["effort"] == "medium"
        assert body["store"] is False
        assert "reasoning.encrypted_content" in body["include"]
        assert "previous_response_id" not in body
        assert "temperature" not in body
        assert all(tool["type"] == "function" for tool in body["tools"])
        assert any(tool["name"] == "web_search" for tool in body["tools"])
        assert "web_search_call.action.sources" not in body["include"]
        if len(captured) == 1:
            result = {"id": "resp_test", "status": "completed", "output": output}
        else:
            assert any(i.get("encrypted_content") == "opaque" for i in body["input"])
            assert any(i.get("type") == "function_call_output" for i in body["input"])
            assert any(i.get("role") == "user" for i in body["input"])
            result = {
                "id": "resp_next",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done."}],
                    }
                ],
            }
        if body.get("stream"):
            event = {"type": "response.completed", "response": result}
            return httpx.Response(
                200,
                text=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=result)

    client = make_llm_client(
        cfg=hosted_config(),
        api_key="slk_test",
        model="gpt-6-luna",
        reasoning_effort="medium",
        transport=httpx.MockTransport(handle),
    )
    assert isinstance(client, OpenAIResponsesClient)
    history = [{"role": "user", "content": "Read README"}]
    tools = [
        {
            "type": "function",
            "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}},
        },
        {
            "type": "function",
            "function": {"name": "web_search", "parameters": {"type": "object", "properties": {}}},
        },
    ]
    first = client.chat(messages=history, tools=tools, stream=stream)
    assistant = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content}, first
    )
    second = client.chat(
        messages=[
            *history,
            assistant,
            {"role": "tool", "tool_call_id": "call_test", "content": "file"},
        ],
        tools=tools,
        stream=stream,
    )
    assert second.content == "Done."


@pytest.mark.parametrize("mode", ["auto", "external", "off"])
def test_luna_search_mode_retains_function_search_or_disables_it(mode):
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp_search", "output": [], "output_text": "OK"})

    cfg = hosted_config()
    cfg.web_search_mode = mode
    client = make_llm_client(
        cfg=cfg, api_key="slk_test", model=cfg.model, transport=httpx.MockTransport(handle)
    )
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "web_search", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="auto"
        if mode == "off"
        else {"type": "function", "function": {"name": "web_search"}},
    )
    if mode == "off":
        assert "tools" not in captured[0]
    else:
        assert captured[0]["tools"][0]["type"] == "function"
        assert captured[0]["tools"][0]["name"] == "web_search"
        assert captured[0]["tool_choice"] == {"type": "function", "name": "web_search"}


@pytest.mark.parametrize(
    "mode, tool",
    [
        ("native", {"type": "function", "function": {"name": "web_search"}}),
        ("auto", {"type": "web_search"}),
        ("auto", {"type": "web_search_preview"}),
    ],
)
def test_luna_rejects_explicit_builtin_search_before_http(mode, tool):
    def unexpected_request(request):
        pytest.fail("Unsupported built-in search must not reach the gateway.")

    cfg = hosted_config()
    cfg.web_search_mode = mode
    client = make_llm_client(
        cfg=cfg,
        api_key="slk_test",
        model=cfg.model,
        transport=httpx.MockTransport(unexpected_request),
    )
    with pytest.raises(LLMError, match="does not support built-in web search"):
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=[tool])


@pytest.mark.parametrize(
    "effort", [None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
)
def test_luna_full_builtin_catalog_forms_gateway_payload(effort):
    captured = []

    def handle(request):
        body = json.loads(request.content)
        expected = {"minimal": "low", "ultra": "max"}.get(effort, effort)
        assert body.get("reasoning", {}).get("effort") == expected
        captured.append(body)
        result = {"id": "resp_catalog", "status": "completed", "output": []}
        if body.get("stream"):
            event = {"type": "response.completed", "response": result}
            return httpx.Response(
                200, text=f"event: response.completed\ndata: {json.dumps(event)}\n\n"
            )
        return httpx.Response(200, json=result)

    cfg = hosted_config()
    cfg.llm_reasoning_effort = effort
    client = make_llm_client(
        cfg=cfg, api_key="slk_test", model=cfg.model, transport=httpx.MockTransport(handle)
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.copied_parameters(),
            },
        }
        for tool in iter_builtin_tool_metadata()
    ]
    for stream in (False, True):
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=tools, stream=stream)
