"""Exercise server retry advice through real adapters and their retry owners."""

from __future__ import annotations

import json
import threading
from email.utils import formatdate

import httpx
import pytest

from alysis_code.agent.session import _protected_computer_use_failure
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.computer_use_diagnostics import computer_use_provider_diagnostic
from alysis_code.llm import rate_limit_metadata
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError


@pytest.fixture(params=[OpenAICompatClient, OpenAIResponsesClient])
def client_class(request):
    return request.param


def success_response(client_class, stream):
    if client_class is OpenAICompatClient:
        if not stream:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Recovered"}}]})
        data = {"choices": [{"delta": {"content": "Recovered"}, "finish_reason": "stop"}]}
        body = f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n"
    else:
        response = {
            "id": "response-test",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Recovered"}],
                }
            ],
        }
        if not stream:
            return httpx.Response(200, json=response)
        data = {"type": "response.completed", "response": response}
        body = f"event: response.completed\ndata: {json.dumps(data)}\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def error_event(client_class, detail):
    kind = "error" if client_class is OpenAICompatClient else "response.failed"
    data = (
        {"error": detail}
        if client_class is OpenAICompatClient
        else {"type": kind, "response": {"status": "failed", "error": detail}}
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"event: {kind}\ndata: {json.dumps(data)}\n\n",
    )


def make_client(client_class, handler, sleeps):
    return client_class(
        base_url="https://provider.invalid/v1",
        api_key="synthetic-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1, base_delay_seconds=1),
        provider_sleep_fn=sleeps.append,
        provider_random_fn=lambda: 0.5,
    )


def close_client(client):
    # Responses currently owns its transports per request; Compat has a pool.
    close = getattr(client, "close", None)
    if close is not None:
        close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [408, 429, 500, 503])
@pytest.mark.parametrize("header", ["7", formatdate(1007, usegmt=True)])
def test_http_retry_advice_reaches_transport_and_preserves_request(
    client_class, stream, status, header, monkeypatch
):
    monkeypatch.setattr(rate_limit_metadata.time, "time", lambda: 1000)
    requests, sleeps = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            # A non-JSON body must not hide the standard header.
            return httpx.Response(status, headers={"Retry-After": header}, text="Temporary outage")
        return success_response(client_class, stream)

    client = make_client(client_class, handler, sleeps)
    try:
        answer = client.chat(messages=[{"role": "user", "content": "Hello"}], stream=stream)
    finally:
        close_client(client)
    assert answer.content == "Recovered"
    assert sleeps == [7]
    assert len(requests) == 2
    assert requests[0] == requests[1]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("stream", [False, True])
def test_wait_longer_than_retry_budget_is_returned_without_another_request(
    client_class, status, stream
):
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "121"}, text="private-page-canary")

    client = make_client(client_class, handler, sleeps)
    try:
        with pytest.raises(LLMError) as caught:
            client.chat(messages=[{"role": "user", "content": "Hello"}], stream=stream)
    finally:
        close_client(client)
    assert len(requests) == 1
    assert sleeps == []
    protected = _protected_computer_use_failure(caught.value)
    diagnostic = computer_use_provider_diagnostic(protected)
    assert diagnostic.retry_after == 121
    assert diagnostic.status == status
    assert "private-page-canary" not in repr(vars(protected))


@pytest.mark.parametrize("code", ["rate_limit_exceeded", "overloaded_error"])
def test_in_stream_retry_advice_is_honored(client_class, code):
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return error_event(client_class, {"code": code, "retry_after_seconds": 7})
        return success_response(client_class, True)

    client = make_client(client_class, handler, sleeps)
    try:
        answer = client.chat(messages=[{"role": "user", "content": "Hello"}], stream=True)
    finally:
        close_client(client)
    assert answer.content == "Recovered"
    assert len(requests) == 2
    assert sleeps == [7]


@pytest.mark.parametrize("field", ["code", "type"])
@pytest.mark.parametrize("code", ["insufficient_quota", "billing_hard_limit_reached"])
def test_in_stream_permanent_quota_does_not_retry(client_class, field, code):
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return error_event(client_class, {field: code, "message": "Quota exhausted"})

    client = make_client(client_class, handler, sleeps)
    try:
        with pytest.raises(LLMError) as caught:
            client.chat(messages=[{"role": "user", "content": "Hello"}], stream=True)
    finally:
        close_client(client)
    assert len(requests) == 1
    assert sleeps == []
    assert caught.value.provider_error_code == code


def test_cancellation_during_server_wait_stops_before_second_request(client_class):
    requests = []
    cancelled = threading.Event()

    def handler(request):
        requests.append(request)
        return httpx.Response(503, headers={"Retry-After": "7"}, text="Unavailable")

    client = make_client(client_class, handler, [])
    # Use actual cancellable waiting; observer cancels before the sleep starts.
    client._provider_sleep_fn = None
    client._provider_retry_event_observer = lambda _: cancelled.set()
    try:
        with pytest.raises(CooperativeCancellationError):
            client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                cancellation_token=EventCancellationToken(cancelled),
            )
    finally:
        close_client(client)
    assert len(requests) == 1
