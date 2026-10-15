from __future__ import annotations

import asyncio
import copy
import json
import threading
import time

import httpx
import pytest

from alysis_code.execution_deadline import temporarily_clamp_client_timeout
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.cancellable_http import (
    cancellation_aware_httpx_client,
    provider_httpx_client,
)
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.gemini_interactions import GeminiInteractionsClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient, ResponsesError
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.task_budget import (
    TaskBudgetAuthority,
    TaskBudgetRefused,
    iso,
    task_budget_purpose,
)


def authority(*, exchange=None, duration=60, lease=30):
    records = []

    def admit(kind, value, binding):
        records.append((kind, copy.deepcopy(value), binding))
        return exchange(kind, value, binding) if exchange else {"ok": True}

    binding = dict(
        version=1,
        taskId="task",
        envelopeId="envelope",
        authorityGeneration=1,
        deadlineAt=iso(time.time() + duration),
        leaseId="lease",
        leaseExpiresAt=iso(time.time() + lease),
    )
    gate = TaskBudgetAuthority(
        binding, exchange=admit, assert_current=lambda: None, cancelled=lambda: False
    )
    return gate, records


def test_actual_responses_internal_fallback_has_distinct_prepared_ack_before_each_transport():
    gate, records = authority()
    sent = []

    def send(request):
        assert records[-1][0] == "attempt"
        assert len([r for r in records if r[0] == "attempt"]) == len(sent) + 1
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported parameter: temperature",
                        "param": "temperature",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "r",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "generated"}],
                    }
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://fixture.invalid/v1",
        api_key="generated-test-key",
        model="fixture-budget-model",
        transport=httpx.MockTransport(send),
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
    )
    with gate.activate(), task_budget_purpose("main"):
        assert (
            client.chat(
                messages=[{"role": "user", "content": "generated-canary"}], tools=[]
            ).content
            == "generated"
        )
    assert [row[0] for row in records] == ["attempt", "settled", "attempt", "settled"]
    assert records[0][1]["requestDigest"] != records[2][1]["requestDigest"]
    assert records[0][1]["purpose"] == records[2][1]["purpose"] == "main"
    assert "generated-canary" not in json.dumps(records) and "generated-test-key" not in json.dumps(
        records
    )


@pytest.mark.parametrize("failure", ["refused", "lost", "revoked"])
def test_no_http_after_refused_lost_or_revoked_ack_and_no_retry(failure):
    sent = []
    gate = None

    def exchange(kind, _value, _binding):
        if failure == "lost":
            raise TaskBudgetRefused()
        if failure == "revoked":
            gate.close()
            return {"ok": True}
        return {"ok": False, "code": "budget_exhausted"}

    gate, records = authority(exchange=exchange)
    with gate.activate():
        for _ in range(2):
            with pytest.raises(TaskBudgetRefused):
                with provider_httpx_client(
                    timeout=1, transport=httpx.MockTransport(lambda request: sent.append(request))
                ) as client:
                    client.post("https://fixture.invalid/v1", json={"canary": "generated"})
    assert sent == [] and len(records) == 1


def test_held_response_settles_only_after_body_and_original_deadline_cancels_transport():
    gate, records = authority(duration=0.15, lease=0.1)
    closed = threading.Event()

    class Hold(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert records[-1][0] == "attempt"
            try:
                await asyncio.Future()
            finally:
                closed.set()

        async def aclose(self):
            closed.set()

    started = time.monotonic()
    with gate.activate(), pytest.raises(TaskBudgetRefused):
        with provider_httpx_client(timeout=30, transport=Hold()) as client:
            client.post("https://fixture.invalid/v1", content=b"generated")
    assert closed.is_set() and time.monotonic() - started < 1
    assert [r[0] for r in records] == ["attempt"]


def test_repeated_authority_activation_and_clock_regression_refuse():
    gate, _ = authority()
    with gate.activate():
        gate.check()
        gate._last_wall = time.time() + 1
        with pytest.raises(TaskBudgetRefused):
            gate.check()
    with pytest.raises(TaskBudgetRefused):
        with gate.activate():
            pass


@pytest.mark.parametrize(
    "cls,response",
    [
        (
            OpenAICompatClient,
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "generated"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ),
        (
            AnthropicMessagesClient,
            {
                "id": "generated",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "generated"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        (
            GeminiGenerateContentClient,
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "generated"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        ),
        (
            GeminiInteractionsClient,
            {"id": "generated", "outputs": [{"type": "text", "text": "generated"}]},
        ),
    ],
)
def test_every_other_builtin_actual_adapter_uses_same_transport_gate(cls, response):
    gate, records = authority()

    def send(request):
        assert records[-1][0] == "attempt"
        assert json.loads(request.content)
        return httpx.Response(200, json=response)

    client = cls(
        base_url="https://fixture.invalid/v1",
        api_key="generated-test-key",
        model="generated-model",
        transport=httpx.MockTransport(send),
    )
    with (
        gate.activate(),
        temporarily_clamp_client_timeout(
            client, gate.execution_deadline(None), operation="compaction_llm"
        ),
    ):
        assert (
            client.chat(messages=[{"role": "user", "content": "generated"}], tools=[]).content
            == "generated"
        )
    assert [r[0] for r in records] == ["attempt", "settled"]
    assert records[0][1]["purpose"] == "compaction"


def test_responses_token_count_and_web_search_cannot_bypass_auxiliary_permit():
    gate, records = authority()

    def send(request):
        assert records[-1][0] == "attempt"
        if str(request.url).endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 7})
        return httpx.Response(
            200,
            json={
                "id": "generated",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "generated", "annotations": []}
                        ],
                    }
                ],
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://fixture.invalid/v1",
        api_key="generated-test-key",
        model="generated-model",
        transport=httpx.MockTransport(send),
    )
    with gate.activate():
        count = client.count_input_tokens(messages=[{"role": "user", "content": "generated"}])
        assert count is not None
        with pytest.raises(ResponsesError, match="did not return sources"):
            client.web_search(query="generated")
    assert [r[0] for r in records] == ["attempt", "settled", "attempt", "settled"]
    assert [r[1]["purpose"] for r in records if r[0] == "attempt"] == ["auxiliary", "auxiliary"]


def test_subscription_refresh_required_refuses_before_auth_http_or_model_release(monkeypatch):
    from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
    from alysis_code.provider_auth.store import ProviderTokenRecord

    auth = OpenAICodexSubscriptionAuth()
    record = ProviderTokenRecord(
        client_id="test-client",
        subject="test-subject",
        scopes=("chatgpt.tokens.use.direct",),
        access_token="generated",
        refresh_token="generated",
        expires_at=time.time() - 1,
    )
    monkeypatch.setattr(
        auth, "_refresh", lambda *args, **kwargs: pytest.fail("must not start a refresh")
    )
    gate, records = authority()
    with gate.activate(), pytest.raises(TaskBudgetRefused):
        auth._valid_record(record)
    assert records == []


def test_prepared_bytes_are_immutable_across_ack_and_original_cancel_token_remains_effective():
    original = {"generated": "approved"}
    headers = {"x-generated": "approved"}
    sent = []

    def mutate(kind, *_):
        if kind == "attempt":
            original["generated"] = "changed"
            headers["x-generated"] = "changed"
        return {"ok": True}

    gate, _ = authority(exchange=mutate)

    def send(request):
        sent.append(request)
        assert json.loads(request.content) == {"generated": "approved"}
        assert request.headers["x-generated"] == "approved"
        return httpx.Response(200)

    with (
        gate.activate(),
        provider_httpx_client(timeout=1, transport=httpx.MockTransport(send)) as client,
    ):
        client.post("https://fixture.invalid", json=original, headers=headers)
    assert len(sent) == 1

    class OriginalToken:
        is_cancelled = False

        def throw_if_cancelled(self, _):
            raise TaskBudgetRefused()

    token = OriginalToken()

    def revoke(kind, *_):
        if kind == "attempt":
            token.is_cancelled = True
        return {"ok": True}

    gate, records = authority(exchange=revoke)
    with gate.activate(), pytest.raises(TaskBudgetRefused):
        with cancellation_aware_httpx_client(
            timeout_s=1,
            transport=httpx.MockTransport(
                lambda _: pytest.fail("cancelled original token must not send")
            ),
            cancellation_token=token,
        ) as client:
            client.post("https://fixture.invalid", content=b"generated")
    assert [r[0] for r in records] == ["attempt", "settled"]


def test_active_budget_metadata_lookup_never_performs_unmetered_subscription_discovery(monkeypatch):
    from types import SimpleNamespace

    import alysis_code.profiles as profiles
    import alysis_code.provider_auth as provider_auth
    import alysis_code.provider_auth.openai_codex as codex
    from alysis_code.config import AppConfig
    from alysis_code.model_registry import ModelRegistry
    from alysis_code.provider_auth.store import ProviderTokenRecord

    requests = []
    auth = codex.OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"models": []})
        )
    )
    token = ProviderTokenRecord(
        client_id="test-client",
        subject="test-subject",
        scopes=("chatgpt.tokens.use.direct",),
        access_token="generated-access",
        refresh_token="generated-refresh",
        expires_at=time.time() + 3600,
        account_id="generated-account",
    )
    monkeypatch.setattr(codex, "load_provider_token", lambda _provider: token)
    monkeypatch.setattr(provider_auth, "create_provider_auth", lambda _provider: auth)
    monkeypatch.setattr(
        profiles,
        "get_active_profile",
        lambda _cfg: SimpleNamespace(
            auth_provider="openai-codex",
            name="generated-profile",
            base_url="https://api.openai.com/v1",
        ),
    )
    registry = ModelRegistry(cfg=AppConfig(model="gpt-4.1"))
    gate, records = authority()
    with gate.activate():
        registry.get("gpt-4.1")
    assert requests == []
    assert records == []


def test_budget_boundary_refuses_implicit_url_auth_before_permit_or_transport():
    gate, records = authority()
    sent = []
    with gate.activate(), pytest.raises(TaskBudgetRefused):
        with provider_httpx_client(
            timeout=1,
            transport=httpx.MockTransport(
                lambda request: sent.append(request) or httpx.Response(200)
            ),
        ) as client:
            client.post(
                "https://generated-user:generated-password@fixture.invalid/v1",
                json={"generated": True},
            )
    assert records == []
    assert sent == []
