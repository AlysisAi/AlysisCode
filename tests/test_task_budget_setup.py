from __future__ import annotations

import io
import time

import httpx
import pytest

import alysis_code.ide.stdio_bridge as stdio
import alysis_code.provider_auth.openai_codex as codex
from alysis_code.agent.computer_use_tools import COMPUTER_USE_TOOL_NAMES
from alysis_code.agent.session import AgentSession
from alysis_code.config import AppConfig
from alysis_code.ide.protocol import ProtocolRequest
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth.store import ProviderTokenRecord
from alysis_code.task_budget import TaskBudgetRefused, iso, task_budget_setup

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.mark.parametrize("expired", [False, True])
def test_actual_budget_session_create_and_later_model_info_never_use_auth_http(
    tmp_path, monkeypatch, expired
):
    requests = []
    record = ProviderTokenRecord(
        client_id="test-client",
        subject="test-subject",
        scopes=("chatgpt.tokens.use.direct",),
        access_token="generated-access",
        refresh_token="generated-refresh",
        expires_at=time.time() + (-1 if expired else 3600),
        account_id="generated-account",
    )
    monkeypatch.setattr(codex, "load_provider_token", lambda _provider: record)
    original_client = codex.OpenAICodexSubscriptionAuth._client

    def intercepted_client(self, *, timeout):
        # Invoke the actual production guard before using an intercepted actual
        # HTTPX boundary. No real account or network data enters this fixture.
        client = original_client(self, timeout=timeout)
        client.close()
        return httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200, json={"models": []})
            )
        )

    monkeypatch.setattr(codex.OpenAICodexSubscriptionAuth, "_client", intercepted_client)
    cfg = AppConfig(model="gpt-5.4", routing_mode="code_only")
    endpoint = "https://api.openai.com/v1"
    add_profile(
        cfg,
        ProfileSpec(
            name="generated-subscription",
            protocol="openai_responses",
            base_url=endpoint,
            default_model=cfg.model,
            reasoning_effort="medium",
            auth_provider="openai-codex",
        ),
    )
    set_active_profile(cfg, "generated-subscription")
    monkeypatch.setattr(stdio, "load_config", lambda: cfg)
    destination = stdio._computer_use_destination(
        model=cfg.model,
        profile="generated-subscription",
        auth_source="provider-auth:openai-codex",
        account_id=codex._credential_scope(record),
        endpoint_digest=stdio._computer_use_endpoint_digest(endpoint),
    )
    binding = dict(
        version=1,
        taskId="task",
        envelopeId="envelope",
        authorityGeneration=1,
        deadlineAt=iso(time.time() + 60),
        leaseId="lease",
        leaseExpiresAt=iso(time.time() + 30),
    )
    params = dict(
        workspace=str(tmp_path),
        mode="review",
        session_id="generated-budget",
        sensitive_media=True,
        model=cfg.model,
        base_url=endpoint,
        computer_use_destination=destination.to_protocol(),
        computer_use_host=dict(
            version=1,
            operation_id="operation",
            broker_fingerprint="sha256:" + "a" * 64,
            authority_epoch=1,
            scope_digest="sha256:" + "b" * 64,
            tools=list(COMPUTER_USE_TOOL_NAMES),
            task_budget=binding,
        ),
    )
    bridge = stdio.StdioBridge(stdout=io.StringIO())
    try:
        result = bridge._session_create(
            ProtocolRequest(id="create", method="session.create", params=params)
        )
        session = bridge._sessions[result["session_id"]]
        assert type(session.agent_session) is AgentSession
        assert not session.agent_session.model_registry._provider_auth_metadata_allowed
        # No active job/context here: the stored registry still stays offline.
        info = bridge._session_model_info(
            ProtocolRequest(
                id="info", method="session.modelInfo", params={"session_id": result["session_id"]}
            )
        )
        assert info["model"] == cfg.model
        assert session.task_budget_channel is None
        assert requests == []
    finally:
        for session in list(bridge._sessions.values()):
            bridge._close_session(session)


def test_budget_bound_setup_cannot_refresh_or_open_subscription_auth_transport(monkeypatch):
    auth = codex.OpenAICodexSubscriptionAuth()
    record = ProviderTokenRecord(
        client_id="test-client",
        subject="test-subject",
        scopes=("chatgpt.tokens.use.direct",),
        access_token="generated",
        refresh_token="generated",
        expires_at=time.time() - 1,
    )
    monkeypatch.setattr(auth, "_refresh", lambda *_args: pytest.fail("refresh must not start"))
    with task_budget_setup(True):
        with pytest.raises(TaskBudgetRefused):
            auth._valid_record(record)
        with pytest.raises(TaskBudgetRefused):
            auth._client(timeout=1)
