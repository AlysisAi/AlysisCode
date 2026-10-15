from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile


@pytest.fixture(autouse=True)
def _offline_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    for prefix in ("ALYSIS", "ALYSIS"):
        for field in ("MAX_OUTPUT_TOKENS", "CONTEXT_WINDOW", "MODEL_COMPACTOR"):
            monkeypatch.delenv(f"{prefix}_{field}", raising=False)
    monkeypatch.setattr(
        "alysis_code.config.resolve_api_key",
        lambda _cfg: SimpleNamespace(key="test-key", source="test"),
    )

    def deny_real_http(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Output-budget refresh tests must not access a provider.")

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


def _config(
    models: tuple[str, str],
    capacities: tuple[int | None, int | None],
    *,
    protocol: str = "anthropic_messages",
) -> AppConfig:
    cfg = AppConfig(model=models[0], prompt_cache_mode="off")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="arbitrary-gateway",
            protocol=protocol,
            base_url="https://opencode.ai/zen/go/v1",
            extra_headers={"X-Route": "keep-this-header"},
        ),
    )
    set_active_profile(cfg, "arbitrary-gateway")
    cfg.extra_fields["role_models"] = {"compactor": models[1]}
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {
            model: {"context_window_tokens": 262_144, "max_output_tokens": capacity}
            for model, capacity in zip(models, capacities, strict=True)
            if capacity is not None
        }
    }
    return cfg


def _session(cfg: AppConfig, requests: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_refresh",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        )

    clients = [
        make_llm_client(
            cfg=cfg,
            api_key="test-key",
            model=model,
            transport=httpx.MockTransport(handler),
            session_id="retained-session",
        )
        for model in (cfg.model, cfg.extra_fields["role_models"]["compactor"])
    ]
    return SimpleNamespace(
        cfg=cfg,
        client=clients[0],
        conversation_compactor=SimpleNamespace(compactor_client=clients[1]),
        provider_session_id="retained-session",
        store=SimpleNamespace(session_id="local-log-id"),
        mode="readonly",
    )


@pytest.mark.parametrize("switch_models", [False, True], ids=["metadata-change", "model-switch"])
def test_refresh_recomputes_both_output_limits_without_changing_session_or_call_overrides(
    switch_models: bool,
) -> None:
    original_models = ("arbitrary-main-alpha", "arbitrary-summary-alpha")
    initial_capacities = (2048, 96_000)
    requests: list[httpx.Request] = []
    session = _session(_config(original_models, initial_capacities), requests)
    clients = (session.client, session.conversation_compactor.compactor_client)
    headers = [dict(client.extra_headers) for client in clients]
    session_scopes = [client.route_identity.session_scope for client in clients]

    for index, capacities in enumerate(
        (initial_capacities, (None, 128_000), (96_000, None), (3000, 120_000))
    ):
        models = (
            (f"arbitrary-main-{index}", f"arbitrary-summary-{index}")
            if switch_models
            else original_models
        )
        cfg = _config(models, capacities)
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)
        expected = [capacity if capacity is not None else 32_000 for capacity in capacities]
        assert [client.default_max_tokens for client in clients] == expected
        assert [client.model for client in clients] == list(models)
        assert session.client is clients[0]
        assert session.conversation_compactor.compactor_client is clients[1]
        routes = [client.route_identity for client in clients]

        # Reapplying unchanged settings preserves both route identity and budget.
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)
        assert [client.route_identity for client in clients] == routes
        assert [client.route_identity.session_scope for client in clients] == session_scopes
        assert [dict(client.extra_headers) for client in clients] == headers
        assert [client.default_max_tokens for client in clients] == expected

        for client, limit in zip(clients, expected, strict=True):
            client.chat(messages=[{"role": "user", "content": "Continue."}])
            assert json.loads(requests[-1].content)["max_tokens"] == limit
            for override in (128, 40_000):
                client.chat(
                    messages=[{"role": "user", "content": "Continue."}], max_tokens=override
                )
                assert json.loads(requests[-1].content)["max_tokens"] == override
            assert client.default_max_tokens == limit

    assert len({request.headers["x-opencode-session"] for request in requests}) == 1
    assert all(request.headers["x-route"] == "keep-this-header" for request in requests)


def test_failed_refresh_restores_main_and_compactor_output_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = ("arbitrary-main", "arbitrary-summary")
    session = _session(_config(models, (2048, 7000)), [])
    clients = (session.client, session.conversation_compactor.compactor_client)
    before = [(client.default_max_tokens, client.route_identity) for client in clients]

    def fail_after_client_refresh(**kwargs: Any) -> None:
        assert [client.default_max_tokens for client in clients] == [96_000, 1000]
        raise RuntimeError("test refresh failure")

    monkeypatch.setattr(chat_loop, "_rebuild_session_tools_for_mode", fail_after_client_refresh)
    with pytest.raises(RuntimeError, match="test refresh failure"):
        chat_loop._apply_config_menu_changes_to_session(
            session=session, cfg=_config(models, (96_000, 1000))
        )

    assert [(client.default_max_tokens, client.route_identity) for client in clients] == before


def test_other_protocol_refresh_does_not_change_output_limit() -> None:
    cfg = _config(("arbitrary-main", "arbitrary-summary"), (2048, 7000), protocol="openai_compat")
    session = _session(cfg, [])
    clients = (session.client, session.conversation_compactor.compactor_client)
    for client in clients:
        client.default_max_tokens = 777

    chat_loop._apply_config_menu_changes_to_session(session=session, cfg=cfg)

    assert [client.default_max_tokens for client in clients] == [777, 777]
