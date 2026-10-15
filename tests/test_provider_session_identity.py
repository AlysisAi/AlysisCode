"""Logical provider conversations survive child log rotation, not route changes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.chat import loop as chat_loop
from alysis_code.config import AppConfig, save_persisted_profile_key
from alysis_code.llm.metadata import (
    assistant_message_from_response,
    build_provider_route_identity,
    credential_scope_fingerprint,
    gate_messages_for_provider_route,
)
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth import ProviderRequestAuthorization
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.surface.noop_surface import NoopSurface


@pytest.fixture(autouse=True)
def _forbid_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_request(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Identity tests must use MockTransport, never a provider.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_request)


def _cfg(*, model: str = "gpt-test", endpoint: str = "https://api.openai.com/v1") -> AppConfig:
    cfg = AppConfig(model=model, skills_enabled=False)
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(
        cfg,
        ProfileSpec(
            name="provider-identity-test",
            protocol="openai_responses",
            base_url=endpoint,
            default_model=model,
        ),
    )
    set_active_profile(cfg, "provider-identity-test")
    return cfg


def _session(root: Path, *, provider_session_id: str | None = None, **kwargs: Any):
    return create_session(
        cfg=kwargs.pop("cfg", _cfg()),
        root=root,
        mode="readonly",
        runtime_kind=RuntimeKind.SUBAGENT,
        subagent_depth=1,
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override=kwargs.pop("key", "fixture-key"),
        enable_compaction=False,
        subagents_enabled=False,
        surface=NoopSurface(),
        prompt_cache_parent_session_id="parent-stream-fixture",
        provider_session_id=provider_session_id,
        **kwargs,
    )


def test_new_logs_replay_only_the_same_logical_provider_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    requests: list[dict[str, Any]] = []
    header_sessions: list[str] = []
    # Model a stateless auth adapter while using the real client serializer,
    # exact route gate and HTTP header construction through MockTransport.
    scope = credential_scope_fingerprint("fixture-key")

    def authorize(_url, *, session_id, expected_credential_scope, **_kwargs):
        assert expected_credential_scope == scope
        return ProviderRequestAuthorization(
            credential_scope=scope, headers={"session-id": session_id}
        )

    auth = SimpleNamespace(
        supports_previous_response_id=False,
        requires_streaming=False,
        supports_temperature=False,
        adapt_responses_payload=dict,
        authorize_request=authorize,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        header_sessions.append(request.headers["session-id"])
        return httpx.Response(
            200,
            json={
                "id": "response-fixture",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "opaque-fixture-id",
                        "summary": [],
                        "encrypted_content": "opaque-fixture-state",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Reviewed."}],
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    sessions = []
    try:
        original = _session(tmp_path)
        sessions.append(original)
        original.client._transport = httpx.MockTransport(handler)
        original.client.provider_auth = auth
        first = original.client.chat(messages=[{"role": "user", "content": "Review."}])
        history = [
            {"role": "user", "content": "Review."},
            assistant_message_from_response(first),
            {"role": "user", "content": "Continue."},
        ]
        resumed = _session(tmp_path, provider_session_id=original.provider_session_id)
        sessions.append(resumed)
        sibling = _session(tmp_path)
        sessions.append(sibling)
        for session in (resumed, sibling):
            session.client._transport = httpx.MockTransport(handler)
            session.client.provider_auth = auth
            session.client.chat(messages=history)

        assert len({session.store.session_id for session in sessions}) == 3
        assert original.provider_session_id == original.store.session_id
        assert resumed.provider_session_id == original.provider_session_id
        assert resumed.store.session_id != resumed.provider_session_id
        assert sibling.provider_session_id == sibling.store.session_id
        assert resumed.client.session_id == original.client.session_id
        assert resumed.client.route_identity == original.client.route_identity
        assert resumed.prompt_cache_stream_key == original.prompt_cache_stream_key
        assert sibling.client.route_identity != original.client.route_identity
        assert header_sessions == [
            original.provider_session_id,
            original.provider_session_id,
            sibling.provider_session_id,
        ]
        assert any(
            item.get("encrypted_content") == "opaque-fixture-state" for item in requests[1]["input"]
        )
        assert not any(item.get("type") == "reasoning" for item in requests[2]["input"])
        assert any(item.get("role") == "assistant" for item in requests[2]["input"])
    finally:
        for session in sessions:
            session.close()


@pytest.mark.parametrize("change", ["model", "endpoint", "credential"])
def test_same_logical_identity_does_not_allow_state_across_changed_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    original = _session(tmp_path)
    kwargs: dict[str, Any] = {}
    if change == "model":
        kwargs["cfg"] = _cfg(model="other-model")
    elif change == "endpoint":
        kwargs["cfg"] = _cfg(endpoint="https://other.example/v1")
    else:
        kwargs["key"] = "different-fixture-key"
    resumed = _session(tmp_path, provider_session_id=original.provider_session_id, **kwargs)
    try:
        state = {
            "role": "assistant",
            "content": "Public review.",
            "_alysis_provider_metadata": {
                "_route_identity": original.client.route_identity.as_metadata(),
                "openai_responses": {
                    "output": [{"type": "reasoning", "encrypted_content": "fixture"}]
                },
            },
        }
        gated = gate_messages_for_provider_route([state], resumed.client.route_identity)
        assert resumed.provider_session_id == original.provider_session_id
        assert resumed.client.route_identity != original.client.route_identity
        assert gated == [{"role": "assistant", "content": "Public review."}]
    finally:
        resumed.close()
        original.close()


def test_config_reload_keeps_logical_scope_and_still_rejects_changed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(chat_loop, name, lambda *args, **kwargs: None, raising=False)
    save_persisted_profile_key("provider-identity-test", "fixture-key")
    session = _session(tmp_path, provider_session_id="original-child-fixture")
    try:
        before = session.client.route_identity
        session.client._reasoning_summary_supported = False
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)
        assert session.client.route_identity == before
        assert session.client.session_id == "original-child-fixture"
        assert session.client._reasoning_summary_supported is False
        assert before.session_scope == credential_scope_fingerprint("original-child-fixture")
        assert session.store.session_id != session.provider_session_id

        session.cfg.model = "changed-model"
        chat_loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)
        assert session.client.route_identity != before
        assert session.client.route_identity.session_scope == before.session_scope
        assert session.client._reasoning_summary_supported is None
    finally:
        session.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auth_provider", "other-auth-adapter"),
        ("credential_scope", "other-account-fingerprint"),
        ("provider_key", "other-provider"),
        ("protocol", "anthropic_messages"),
        ("profile_name", "other-profile"),
        ("routing_headers", {"x-tenant": "other-tenant"}),
        ("routing_fields", {"session_id": "other-affinity"}),
        ("protocol_revision", "other-revision"),
    ],
)
def test_logical_continuation_still_strips_incompatible_provider_state(
    field: str, value: Any
) -> None:
    fields = {
        "protocol": "openai_responses",
        "base_url": "https://provider.example/v1",
        "provider_key": "fixture-provider",
        "model": "fixture-model",
        "profile_name": "fixture-profile",
        "auth_provider": "fixture-auth",
        "credential_scope": "fixture-account-fingerprint",
        "session_scope": credential_scope_fingerprint("same-child-logical-identity"),
    }
    original = build_provider_route_identity(**fields)
    resumed = build_provider_route_identity(**{**fields, field: value})
    message = {
        "role": "assistant",
        "content": "Public review.",
        "_alysis_provider_metadata": {
            "_route_identity": original.as_metadata(),
            "openai_responses": {
                "output_items": [{"type": "reasoning", "encrypted_content": "fixture-state"}]
            },
        },
    }
    assert resumed.session_scope == original.session_scope
    assert gate_messages_for_provider_route([message], resumed) == [
        {"role": "assistant", "content": "Public review."}
    ]
