"""Post-D071-v2 follow-up: endpoint capability, independent from response usage."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm.base import count_input_tokens_if_supported
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import BillingMode, UsageConfidence, UsageContract
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth import ProviderRequestAuthorization, openai_codex
from alysis_code.provider_telemetry import (
    provider_call_history_snapshot,
    reset_provider_telemetry_for_tests,
)


def _denied(*_args: Any, **_kwargs: Any) -> Any:
    pytest.fail("Unsupported preflight must not reach credentials, auth headers or HTTP.")


@pytest.fixture(autouse=True)
def _offline_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(socket, "create_connection", _denied)
    monkeypatch.setattr(socket, "getaddrinfo", _denied)
    monkeypatch.setattr(socket.socket, "connect", _denied)
    monkeypatch.setattr(socket.socket, "connect_ex", _denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _denied)
    monkeypatch.setattr(openai_codex, "load_provider_token", _denied)
    reset_provider_telemetry_for_tests()
    yield
    reset_provider_telemetry_for_tests()


@pytest.mark.parametrize(
    "contract",
    [
        None,
        UsageContract(
            response_usage_confidence=UsageConfidence.REPORTED,
            normalized_output_includes_reasoning=False,
            input_token_count_strategy="openai_responses",
            billing_mode=BillingMode.SUBSCRIPTION,
        ),
    ],
    ids=["default-contract", "passed-contract"],
)
def test_real_subscription_adapter_skips_counting_before_auth_or_http(
    monkeypatch: pytest.MonkeyPatch, contract: UsageContract | None
) -> None:
    adapter = openai_codex.OpenAICodexSubscriptionAuth()
    # Construction binds a route once; unsupported preflight must do no further
    # credential lookup or request authorization.
    monkeypatch.setattr(adapter, "route_credential_scope", lambda: "offline-account-scope")
    monkeypatch.setattr(adapter, "authorization_headers", _denied)
    monkeypatch.setattr(adapter, "authorize_request", _denied)
    original = contract or OpenAIResponsesClient.usage_contract
    cache_metadata = {"enabled": True, "strategy": "implicit", "mode": "automatic"}
    client = OpenAIResponsesClient(
        base_url=adapter.base_url,
        api_key="",
        model="test-model",
        provider_auth=adapter,
        transport=httpx.MockTransport(_denied),
        usage_contract=contract,
        prompt_cache_policy_metadata=cache_metadata,
    )

    for _ in range(2):
        assert client.count_input_tokens(messages=[{"role": "user", "content": "hello"}]) is None
        assert (
            count_input_tokens_if_supported(
                client=client, messages=[{"role": "user", "content": "hello"}]
            )
            is None
        )
    assert client.usage_contract == replace(original, input_token_count_strategy="none")
    assert client.usage_counts_authoritative is original.response_usage_authoritative
    assert client.prompt_cache_policy_metadata == cache_metadata
    assert adapter.cache_capability.enabled
    assert adapter.cache_capability.reports_cache_read_tokens


def test_unknown_auth_adapter_does_not_implicitly_authorize_count_endpoint() -> None:
    adapter = SimpleNamespace(
        authorization_headers=_denied,
        adapt_responses_payload=_denied,
    )
    client = OpenAIResponsesClient(
        base_url="https://auth.example/v1",
        api_key="",
        model="test-model",
        provider_auth=adapter,
        transport=httpx.MockTransport(_denied),
    )

    assert client.count_input_tokens(messages=[{"role": "user", "content": "hello"}]) is None
    assert not client.usage_contract.supports_input_token_count


def test_public_api_still_counts_through_shared_and_direct_entry_points() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://api.openai.com/v1/responses/input_tokens"
        return httpx.Response(200, json={"input_tokens": 64})

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="offline-placeholder",
        model="test-model",
        transport=httpx.MockTransport(handle),
    )
    messages = [{"role": "user", "content": "hello"}]
    measured = count_input_tokens_if_supported(client=client, messages=messages)
    assert measured is not None
    assert measured.input_tokens == 64
    assert measured.confidence is UsageConfidence.AUTHORITATIVE
    assert client.count_input_tokens(messages=messages) == measured
    assert len(requests) == 2


def test_unavailable_preflight_does_not_change_subscription_response_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    adapter = openai_codex.OpenAICodexSubscriptionAuth()

    scope = "offline-account-scope"
    monkeypatch.setattr(adapter, "route_credential_scope", lambda: scope)

    def authorize(url: str, *, expected_credential_scope: str, **_kwargs: Any):
        assert url == f"{adapter.base_url}/responses"
        assert expected_credential_scope == scope
        return ProviderRequestAuthorization(
            credential_scope=scope, headers={"Authorization": "Bearer offline-placeholder"}
        )

    monkeypatch.setattr(adapter, "authorize_request", authorize)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/responses")
        assert json.loads(request.content)["stream"] is True
        completed = {
            "type": "response.completed",
            "response": {
                "id": "offline-response",
                "model": "test-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done"}],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 80},
                    "output_tokens": 12,
                    "output_tokens_details": {"reasoning_tokens": 5},
                    "total_tokens": 112,
                },
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: response.completed\ndata: {json.dumps(completed)}\n\n",
        )

    contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        input_token_count_strategy="openai_responses",
        billing_mode=BillingMode.SUBSCRIPTION,
    )
    client = OpenAIResponsesClient(
        base_url=adapter.base_url,
        api_key="",
        model="test-model",
        provider_auth=adapter,
        transport=httpx.MockTransport(handle),
        usage_contract=contract,
    )
    messages = [{"role": "user", "content": "hello"}]
    assert count_input_tokens_if_supported(client=client, messages=messages) is None
    response = client.chat(messages=messages, stream=False)

    assert len(requests) == 1
    assert response.content == "Done"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 100
    assert response.usage.cached_prompt_tokens == 80
    assert response.usage.completion_tokens == 12
    assert response.usage.reasoning_tokens == 5
    assert response.usage.total_tokens == 112
    calls = provider_call_history_snapshot()
    assert len(calls) == 1
    assert calls[0]["usage"]["prompt_tokens"] == 100
    assert calls[0]["usage"]["cached_prompt_tokens"] == 80
    assert calls[0]["usage"]["completion_tokens"] == 12
    assert calls[0]["usage"]["reasoning_tokens"] == 5
    assert client.usage_contract.billing_mode is BillingMode.SUBSCRIPTION


@pytest.mark.parametrize("auth_enabled", [False, True])
def test_explicitly_disabled_contract_cannot_be_reenabled(auth_enabled: bool) -> None:
    adapter = (
        SimpleNamespace(
            supports_input_token_count=True,
            authorization_headers=_denied,
            adapt_responses_payload=_denied,
        )
        if auth_enabled
        else None
    )
    contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        input_token_count_strategy="none",
    )
    client = OpenAIResponsesClient(
        base_url="https://api.example/v1",
        api_key="offline-placeholder",
        model="test-model",
        provider_auth=adapter,
        transport=httpx.MockTransport(_denied),
        usage_contract=contract,
    )
    assert client.count_input_tokens(messages=[{"role": "user", "content": "hello"}]) is None
    assert client.usage_contract == contract


def test_factory_subscription_contract_keeps_billing_and_response_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = openai_codex.OpenAICodexSubscriptionAuth()
    monkeypatch.setattr(adapter, "route_credential_scope", lambda: "offline-account-scope")
    monkeypatch.setattr(adapter, "authorization_headers", _denied)
    monkeypatch.setattr(adapter, "authorize_request", _denied)
    monkeypatch.setattr(
        "alysis_code.llm.factory.create_provider_auth", lambda *_args, **_kwargs: adapter
    )
    # Supply catalog routing offline; this test exercises the factory's real
    # usage-contract path without discovering models from a personal account.
    monkeypatch.setattr(
        "alysis_code.llm.factory.resolve_model_provider_key", lambda **_kwargs: "openai"
    )
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol=adapter.protocol,
        base_url=adapter.base_url,
        auth_provider=adapter.provider_id,
        default_model="test-model",
        reasoning_effort="high",
    )
    cfg = AppConfig(model="test-model", prompt_cache_mode="auto")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    client = make_llm_client(
        cfg=cfg, api_key="", model="test-model", transport=httpx.MockTransport(_denied)
    )

    assert not client.usage_contract.supports_input_token_count
    assert client.usage_contract.billing_mode is BillingMode.SUBSCRIPTION
    assert client.usage_contract.response_usage_authoritative
    assert client.usage_contract.normalized_output_includes_reasoning
    assert client.reasoning_effort == "high"
    assert client.prompt_cache_policy_metadata["enabled"] is True
    assert count_input_tokens_if_supported(client=client, messages=[]) is None
