from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.factory import make_llm_client, resolve_model_protocol
from alysis_code.llm.openai_compat import attach_provider_metadata_to_assistant_message
from alysis_code.llm.types import LLMError
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.tools.registry import iter_builtin_tool_metadata

MODEL = "claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def configured_hosted_endpoint(monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api")


def hosted_config():
    profile = replace(make_profile_from_preset(get_preset("alysis")), default_model=MODEL)
    cfg = AppConfig(model=MODEL, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_sonnet_is_excluded_from_free_picker_but_retains_native_protocol():
    cfg = hosted_config()
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert MODEL not in [r.value for r in flow.screen().rows]
    meta = ModelRegistry(cfg=cfg).get(MODEL)
    assert meta.context_window_tokens == 1_000_000
    assert meta.max_output_tokens == 128_000
    assert meta.supports_reasoning is True
    assert meta.supports_vision is False
    assert meta.input_cost_per_token == 0.000002
    assert meta.output_cost_per_token == 0.00001
    assert meta.cache_read_input_cost_per_token == 0.0000002
    assert meta.cache_creation_input_cost_per_token == 0.0000025
    assert (
        resolve_model_protocol(provider_key="alysis", model=MODEL, protocol="openai_compat")
        == "anthropic_messages"
    )


@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high", "xhigh", "max"])
def test_python_tool_catalog_payload_uses_native_messages(effort):
    cfg = hosted_config()
    cfg.llm_reasoning_effort = effort
    cfg.web_search_mode = "auto"
    captured = []

    def handle(request):
        assert request.url.path.endswith("/v1/messages")
        assert request.headers["authorization"] == "Bearer slk_test"
        assert "x-api-key" not in request.headers
        payload = json.loads(request.content)
        captured.append(payload)
        assert "temperature" not in payload
        assert payload.get("tool_choice", {}).get("type") not in {"tool", "any"}
        if effort == "none":
            assert payload["thinking"] == {"type": "between_tools"}
            assert payload["output_config"]["effort"] == "high"
        result = {
            "id": "msg_test",
            "model": MODEL,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 1},
        }
        return httpx.Response(200, json=result)

    client = make_llm_client(
        cfg=cfg, api_key="slk_test", model=MODEL, transport=httpx.MockTransport(handle)
    )
    assert isinstance(client, AnthropicMessagesClient)
    assert client.supports_forced_tool_choice is False
    assert client.usage_contract.billing_mode.value == "included"
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
    response = client.chat(
        messages=[{"role": "user", "content": "Fix the bug."}], tools=tools, tool_choice="required"
    )
    assert response.content == "Done."


def test_sonnet_preserves_signed_thinking_through_tool_continuation():
    cfg = hosted_config()
    sent = []
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "signed-reasoning"},
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
    ]

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "type": "message",
                "id": "msg_test",
                "model": MODEL,
                "role": "assistant",
                "content": blocks if len(sent) == 1 else [{"type": "text", "text": "Done."}],
                "stop_reason": "tool_use" if len(sent) == 1 else "end_turn",
            },
        )

    client = make_llm_client(
        cfg=cfg, api_key="slk_test", model=MODEL, transport=httpx.MockTransport(handle)
    )
    history = [{"role": "user", "content": "Read a.py"}]
    tools = [
        {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}
    ]
    response = client.chat(messages=history, tools=tools)
    assistant = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": response.content}, response
    )
    assert (
        client.chat(
            messages=[
                *history,
                assistant,
                {"role": "tool", "tool_call_id": "toolu_1", "content": "file"},
            ],
            tools=tools,
        ).content
        == "Done."
    )
    assert sent[1]["messages"][1]["content"] == blocks
    assert sent[1]["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"


def test_hosted_sonnet_rejects_native_search_before_http():
    cfg = hosted_config()
    cfg.web_search_mode = "native"
    client = make_llm_client(
        cfg=cfg,
        api_key="slk_test",
        model=MODEL,
        transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected provider request")),
    )
    with pytest.raises(LLMError, match="does not support built-in web search"):
        client.chat(messages=[{"role": "user", "content": "Search"}])


@pytest.mark.parametrize(
    "mode, expected_cache", [("auto", True), ("off", False), ("manual", False)]
)
def test_hosted_sonnet_cache_policy_reaches_messages_api(mode, expected_cache):
    cfg = hosted_config()
    cfg.prompt_cache_mode = mode
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = make_llm_client(
        cfg=cfg, api_key="slk_test", model=MODEL, transport=httpx.MockTransport(handle)
    )
    client.chat(
        messages=[
            {"role": "system", "content": "Stable coding instructions."},
            {"role": "user", "content": "hi"},
        ]
    )
    payload = captured[0]
    if expected_cache:
        assert payload["cache_control"] == {"type": "ephemeral"}  # Anthropic defaults to 5m.
        assert payload["system"][-1]["cache_control"] == payload["cache_control"]
    else:
        assert "cache_control" not in json.dumps(payload)


def test_tui_sonnet_cache_preview_uses_native_model_protocol():
    cfg = hosted_config()
    cfg.prompt_cache_mode = "auto"
    flow = ConfigFlow(cfg=cfg)
    flow.choose("cache")
    assert "enabled anthropic_cache_control" in flow.screen().subtitle
    assert "does not support" not in flow.screen().subtitle


@pytest.mark.parametrize("stream", [False, True])
def test_hosted_sonnet_reports_reasoning_usage_without_double_counting(stream):
    usage = {
        "input_tokens": 10,
        "output_tokens": 12,
        "output_tokens_details": {"thinking_tokens": 7},
    }

    def handle(request):
        if not stream:
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "content": [{"type": "text", "text": "OK"}],
                    "usage": usage,
                },
            )
        events = [
            {
                "type": "message_start",
                "message": {
                    "type": "message",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "OK"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage},
            {"type": "message_stop"},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
            ),
        )

    client = make_llm_client(
        cfg=hosted_config(), api_key="slk_test", model=MODEL, transport=httpx.MockTransport(handle)
    )
    response = client.chat(messages=[{"role": "user", "content": "hi"}], stream=stream)
    assert response.content == "OK"
    assert response.usage.reasoning_tokens == 7
    assert response.usage.normalized().completion_tokens == 12
    assert response.usage.normalized().total_tokens == 22


@pytest.mark.parametrize("other_model", ["deepseek-flash", "glm-5.3-flash", "gpt-6-luna"])
def test_hosted_sonnet_switch_refreshes_chat_and_compactor(other_model, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from alysis_code.cli_impl.chat import loop as chat_loop
    from alysis_code.llm.types import BillingMode

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "alysis_code.config.resolve_api_key",
        lambda cfg: SimpleNamespace(key="slk_test", source="test"),
    )
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(chat_loop, name, lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(
        "alysis_code.agent.prompt_context.refresh_session_prompt_guidance", lambda session: False
    )

    def handle(request):
        assert request.headers["authorization"] == "Bearer slk_test"
        assert request.url.path.endswith("/llm/v1/messages")
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    cfg = hosted_config()
    initial = make_llm_client(
        cfg=cfg, api_key="slk_test", model=MODEL, transport=httpx.MockTransport(handle)
    )
    session = SimpleNamespace(
        cfg=cfg,
        client=initial,
        conversation_compactor=SimpleNamespace(compactor_client=initial, summary="Keep summary"),
        provider_session_id="retained-session",
        store=SimpleNamespace(session_id="retained-session"),
        mode="readonly",
        messages=[{"role": "user", "content": "Keep conversation"}],
    )
    messages = session.messages
    for model in (other_model, MODEL):
        next_cfg = hosted_config()
        next_cfg.model = model
        next_cfg.extra_fields["profiles"]["alysis"]["default_model"] = model
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=next_cfg)
        for active in (session.client, session.conversation_compactor.compactor_client):
            expected = (
                "anthropic_messages"
                if model == MODEL
                else "openai_responses"
                if model == "gpt-6-luna"
                else "openai_compat"
            )
            assert active.model == model
            assert active.route_identity.protocol == expected
            assert active.usage_contract.billing_mode == BillingMode.INCLUDED
        assert session.messages is messages
        assert session.conversation_compactor.summary == "Keep summary"
    assert session.client.chat(messages=[{"role": "user", "content": "hi"}]).content == "OK"
