from __future__ import annotations

import copy
import io
from types import SimpleNamespace

import pytest

from alysis_code.cli_impl.chat import loop
from alysis_code.config import AppConfig, load_config, save_config
from alysis_code.ide import provider_switch, stdio_bridge
from alysis_code.llm.factory import make_llm_client
from alysis_code.profiles import ProfileSpec, add_profile, get_active_profile, set_active_profile


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("QA_PROVIDER_A_KEY", "qa-only-provider-a")
    monkeypatch.setenv("QA_PROVIDER_B_KEY", "qa-only-provider-b")
    cfg = AppConfig(model="model-a", max_steps=7)
    for name, protocol, port in [("qa_a", "openai_compat", 9), ("qa_b", "anthropic_messages", 10)]:
        add_profile(
            cfg,
            ProfileSpec(
                name=name,
                protocol=protocol,
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key_env=f"QA_PROVIDER_{name[-1].upper()}_KEY",
                default_model=f"model-{name[-1]}",
            ),
        )
    set_active_profile(cfg, "qa_a")
    save_config(cfg)
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "refresh_session_workspace_binding_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setitem(loop.__dict__, name, lambda *args, **kwargs: None)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    def create(**kwargs):
        current_cfg = kwargs["cfg"]
        client = make_llm_client(cfg=current_cfg, api_key="qa-only-provider-a", model="model-a")
        return SimpleNamespace(
            cfg=current_cfg,
            mode=kwargs["mode"],
            surface=kwargs["surface"],
            store=SimpleNamespace(session_artifact_root=artifacts, append=lambda *a, **k: None),
            messages=[{"role": "user", "content": "Remember this repository"}],
            client=client,
            router_client=client,
            _provisioned_router_client=client,
            conversation_compactor=SimpleNamespace(
                compactor_client=client, state={"summary": "keep"}
            ),
            persona_client_cache={("model-a", 0.2): client},
            persona_client_key=("model-a", 0.2),
            close=lambda: None,
        )

    bridge = stdio_bridge.StdioBridge(stdout=io.StringIO(), create_session_fn=create)

    def dispatch(method, **params):
        return bridge._dispatch(
            stdio_bridge.ProtocolRequest(
                id="qa", protocol_version="1", method=method, params=params
            )
        )[0]

    created = dispatch(
        "session.create", workspace=str(workspace), mode="readonly", workspace_trusted=True
    )
    session_id = created["session_id"]
    session = bridge._require_session(session_id, request_id="qa")

    def switch(**params):
        return dispatch(
            "session.setProfile",
            **{"session_id": session_id, "name": "qa_b", "workspace_trusted": True, **params},
        )

    yield session, switch, monkeypatch
    bridge._sessions.clear()


def test_switch_changes_transport_credentials_and_default_preserving_conversation(conversation):
    session, switch, _ = conversation
    agent = session.agent_session
    messages = copy.deepcopy(agent.messages)
    old_client = agent.client
    root, session_id, store = session.root, session.session_id, agent.store
    result = switch()
    assert result["session_id"] == session_id
    assert session.root == root and agent.store is store
    assert agent.messages == messages and agent.mode == "readonly"
    assert agent.cfg.max_steps == 7
    assert get_active_profile(load_config()).name == "qa_b"
    assert get_active_profile(agent.cfg).name == "qa_b"
    assert type(agent.client).__name__ == "AnthropicMessagesClient"
    for client in (
        agent.client,
        agent.router_client,
        agent._provisioned_router_client,
        agent.conversation_compactor.compactor_client,
    ):
        assert client.api_key == "qa-only-provider-b"
        assert client.base_url == "http://127.0.0.1:10/v1"
        assert client.model == "model-b"
    assert old_client.api_key == "qa-only-provider-a"
    assert agent.persona_client_cache == {} and agent.persona_client_key is None
    assert agent.conversation_compactor.state == {"summary": "keep"}
    switch(name="qa_a")
    assert type(agent.client) is type(old_client)
    assert agent.messages == messages


@pytest.mark.parametrize("failure", ["factory", "refresh", "save"])
def test_rejected_switch_keeps_previous_clients_history_and_saved_default(conversation, failure):
    session, switch, monkeypatch = conversation
    agent = session.agent_session
    before = vars(agent).copy()

    def reject(*args, **kwargs):
        raise RuntimeError("deliberate switch failure")

    if failure == "factory":
        monkeypatch.setattr(provider_switch, "make_llm_client", reject)
    elif failure == "refresh":
        monkeypatch.setitem(loop.__dict__, "_rebuild_session_tools_for_mode", reject)
    else:
        monkeypatch.setattr(stdio_bridge, "save_config", reject)
    with pytest.raises(RuntimeError, match="deliberate switch failure"):
        switch()
    for field in (
        "cfg",
        "client",
        "router_client",
        "messages",
        "conversation_compactor",
        "persona_client_cache",
    ):
        assert getattr(agent, field) is before[field]
    assert agent.client.api_key == "qa-only-provider-a"
    assert get_active_profile(load_config()).name == "qa_a"


@pytest.mark.parametrize("case", ["busy", "untrusted", "unknown"])
def test_invalid_switch_is_rejected_before_mutation(conversation, case):
    session, switch, _ = conversation
    client = session.agent_session.client
    params = {}
    if case == "busy":
        session.active_job = stdio_bridge.BridgeJob(
            job_id="busy-original",
            session_id=session.session_id,
            created_at="2026-10-26T00:00:00Z",
            status="running",
            worker_reserved=True,
        )
    elif case == "untrusted":
        params["workspace_trusted"] = False
    else:
        params["name"] = "missing"
    with pytest.raises(stdio_bridge.ProtocolError):
        switch(**params)
    assert session.agent_session.client is client
    assert get_active_profile(load_config()).name == "qa_a"


def test_connect_model_override_applies_to_live_and_saved_profile(conversation):
    session, switch, _ = conversation
    switch(model="selected-model")
    assert session.agent_session.client.model == "selected-model"
    assert get_active_profile(load_config()).default_model == "selected-model"
