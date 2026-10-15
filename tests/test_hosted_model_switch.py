from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.types import BillingMode
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile


@pytest.fixture(autouse=True)
def offline_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", "https://cloud.example.invalid/api")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    for prefix in ("ALYSIS", "ALYSIS"):
        for field in ("MODEL_COMPACTOR", "REASONING_EFFORT", "ENABLE_THINKING"):
            monkeypatch.delenv(f"{prefix}_{field}", raising=False)
    monkeypatch.setattr(
        "alysis_code.config.resolve_api_key",
        lambda _cfg: SimpleNamespace(key="slk_test", source="test"),
    )

    def deny_real_http(*args, **kwargs):
        pytest.fail("Model-switch tests must use MockTransport.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_real_http)
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(chat_loop, name, lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(
        "alysis_code.agent.prompt_context.refresh_session_prompt_guidance",
        lambda _session: False,
    )


def hosted_config(model, compactor_model=None):
    profile = replace(
        make_profile_from_preset(get_preset("alysis")),
        default_model=model,
        reasoning_effort="medium",
    )
    cfg = AppConfig(model=model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    if compactor_model:
        cfg.extra_fields["role_models"] = {"compactor": compactor_model}
    return cfg


def make_session(cfg):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append((request, body))
        assert request.headers["authorization"] == "Bearer slk_test"
        if body["model"] == "gpt-6-luna":
            # Reproduce the gateway rejection if a model switch reuses Chat Completions.
            assert request.url.path.endswith("/llm/v1/responses")
            assert body["reasoning"]["effort"] == "medium"
            assert body["store"] is False
            assert "previous_response_id" not in body
            assert "temperature" not in body
            assert "reasoning.encrypted_content" in body["include"]
            assert all(tool["type"] == "function" for tool in body["tools"])
            assert any(tool["name"] == "web_search" for tool in body["tools"])
            result = {
                "id": "resp_switch",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
            }
            event = {"type": "response.completed", "response": result}
            stream = f"event: response.completed\ndata: {json.dumps(event)}\n\n"
        else:
            assert request.url.path.endswith("/llm/v1/chat/completions")
            if body["model"] == "glm-5.3-flash":
                assert body.get("reasoning_effort") in (None, "low", "high", "max")
                assert body.get("thinking", {}).get("type") != "disabled"
            else:
                assert body["model"] == "deepseek-flash"
                assert "tool_stream" not in body
            result = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
            event = {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}
            stream = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n"
        if body.get("stream"):
            return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=result)

    clients = [
        make_llm_client(
            cfg=cfg,
            api_key="slk_test",
            model=model,
            transport=httpx.MockTransport(handle),
            session_id="retained-session",
        )
        for model in (
            cfg.model,
            cfg.extra_fields.get("role_models", {}).get("compactor", cfg.model),
        )
    ]
    session = SimpleNamespace(
        cfg=cfg,
        client=clients[0],
        conversation_compactor=SimpleNamespace(compactor_client=clients[1], summary="Keep summary"),
        provider_session_id="retained-session",
        store=SimpleNamespace(session_id="retained-session"),
        mode="readonly",
        messages=[{"role": "user", "content": "Keep conversation"}],
    )
    return session, requests


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("other_model", ["deepseek-flash", "glm-5.3-flash"])
@pytest.mark.parametrize("separate_compactor", [False, True])
def test_switch_to_luna_and_back_uses_each_models_protocol(stream, other_model, separate_compactor):
    session, requests = make_session(hosted_config(other_model))
    messages = session.messages
    store = session.store
    for model in ("gpt-6-luna", other_model, "gpt-6-luna"):
        cfg = hosted_config(model, other_model if separate_compactor else None)
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        clients = (session.client, session.conversation_compactor.compactor_client)
        for client in clients:
            expected = "openai_responses" if client.model == "gpt-6-luna" else "openai_compat"
            assert client.route_identity.protocol == expected
            assert client.usage_contract.billing_mode == BillingMode.INCLUDED
            assert client.provider_key == "alysis"
            assert (
                client.chat(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[
                        {
                            "type": "function",
                            "function": {"name": "web_search", "parameters": {"type": "object"}},
                        }
                    ],
                    stream=stream,
                ).content
                == "OK"
            )
        # An unchanged reload must retain the chosen transport and route identity.
        routes = [client.route_identity for client in clients]
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        assert session.client is clients[0]
        assert session.conversation_compactor.compactor_client is clients[1]
        assert [client.route_identity for client in clients] == routes
        assert session.messages is messages
        assert session.store is store
        assert session.conversation_compactor.summary == "Keep summary"
    assert len(requests) == 6


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "first,second",
    [
        ("deepseek-flash", "glm-5.3-flash"),
        ("glm-5.3-flash", "deepseek-flash"),
    ],
)
def test_switch_between_chat_models_retains_valid_wire_fields(stream, first, second):
    session, requests = make_session(hosted_config(first))
    for model in (first, second, first):
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=hosted_config(model))
        assert (
            session.client.chat(
                messages=[{"role": "user", "content": "Reply OK"}], stream=stream
            ).content
            == "OK"
        )
    assert [body["model"] for _request, body in requests] == [first, second, first]


def test_failed_switch_restores_original_clients_and_compactor(monkeypatch):
    cfg = hosted_config("deepseek-flash")
    session, _requests = make_session(cfg)
    client = session.client
    compactor = session.conversation_compactor
    routes = (client.route_identity, compactor.compactor_client.route_identity)

    def fail_after_refresh(**kwargs):
        assert session.client.route_identity.protocol == "openai_responses"
        assert (
            session.conversation_compactor.compactor_client.route_identity.protocol
            == "openai_responses"
        )
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(chat_loop, "_rebuild_session_tools_for_mode", fail_after_refresh)
    with pytest.raises(RuntimeError, match="refresh failed"):
        chat_loop._apply_config_menu_changes_to_session(
            session=session, cfg=hosted_config("gpt-6-luna")
        )
    assert session.cfg is cfg
    assert session.client is client
    assert session.conversation_compactor is compactor
    assert (client.route_identity, compactor.compactor_client.route_identity) == routes
    assert client.model == compactor.compactor_client.model == "deepseek-flash"
