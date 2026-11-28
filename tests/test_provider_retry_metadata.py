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
from alysis_code.failure_category import FailureCategory, classify_failure_category
from alysis_code.ide.stdio_bridge import _redact_bridge_error
from alysis_code.llm import rate_limit_metadata
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.gemini_interactions import GeminiInteractionsClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.llm_error_display import friendly_llm_error_message


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
    kind = "response.failed" if client_class is OpenAIResponsesClient else "error"
    data = (
        {"type": kind, "error": detail}
        if kind == "error"
        else {"type": kind, "response": {"status": "failed", "error": detail}}
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"event: {kind}\ndata: {json.dumps(data)}\n\n",
    )


def make_client(client_class, handler, sleeps, *, provider_key=None, model="test-model"):
    return client_class(
        base_url="https://provider.invalid/v1",
        api_key="synthetic-key",
        provider_key=provider_key,
        model=model,
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


@pytest.mark.parametrize(
    "adapter",
    [
        OpenAICompatClient,
        OpenAIResponsesClient,
        AnthropicMessagesClient,
        GeminiGenerateContentClient,
        GeminiInteractionsClient,
    ],
)
@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (
            429,
            {
                "error": {
                    "code": "1113",
                    "message": "Insufficient balance or no resource package. Please recharge.",
                }
            },
        ),
        (
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access this model.",
                }
            },
        ),
        (403, {"error": {"code": "SUBSCRIPTION_EXPIRED"}}),
        (402, {"message": "Payment required"}),
    ],
)
def test_billing_rejection_stops_first_request_across_adapters(adapter, status, payload):
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=payload)

    client = make_client(adapter, handler, sleeps)
    try:
        with pytest.raises(LLMError) as caught:
            client.chat(messages=[{"role": "user", "content": "Hello"}], stream=False)
    finally:
        close_client(client)
    assert len(requests) == 1
    assert sleeps == []
    assert caught.value.provider_error_code == "billing_hard_limit_reached"
    assert classify_failure_category(caught.value) == FailureCategory.PROVIDER_ERROR
    assert "subscription" in friendly_llm_error_message(caught.value)
    assert "configured key" in _redact_bridge_error(None, caught.value)


def test_temporary_quota_recovers_even_with_billing_terms_elsewhere(client_class):
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={
                    "error": {
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Per-minute quota exceeded.",
                        "request": {"text": "insufficient balance"},
                    }
                },
            )
        return success_response(client_class, False)

    client = make_client(client_class, handler, sleeps)
    try:
        answer = client.chat(messages=[{"role": "user", "content": "Hello"}], stream=False)
    finally:
        close_client(client)
    assert (len(requests), sleeps, answer.content) == (2, [7], "Recovered")


@pytest.mark.parametrize("wire", ["http", "stream-http", "stream-event"])
def test_localized_business_code_uses_the_provider_contract(wire):
    detail = {"code": 1113, "message": "余额不足或无可用资源包。"}
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        if wire == "stream-event":
            return error_event(OpenAICompatClient, detail)
        return httpx.Response(429, json={"error": detail})

    client = make_client(OpenAICompatClient, handler, sleeps, provider_key="zai_coding_plan")
    try:
        with pytest.raises(LLMError) as caught:
            client.chat(messages=[{"role": "user", "content": "你好"}], stream=wire != "http")
    finally:
        close_client(client)
    assert caught.value.provider_error_code == "billing_hard_limit_reached"
    assert len(requests) == 1
    assert sleeps == []


def test_same_numeric_code_from_another_provider_can_retry():
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={
                    "error": {"code": "1113", "message": "Demasiadas solicitudes."},
                },
            )
        return success_response(OpenAICompatClient, False)

    client = make_client(
        OpenAICompatClient, handler, sleeps, provider_key="custom", model="other-model"
    )
    try:
        result = client.chat(messages=[{"role": "user", "content": "Hola"}], stream=False)
    finally:
        close_client(client)
    assert (len(requests), sleeps, result.content) == (2, [7], "Recovered")


@pytest.mark.parametrize(
    "adapter", [AnthropicMessagesClient, GeminiGenerateContentClient, GeminiInteractionsClient]
)
def test_native_temporary_limit_honors_provider_retry_after(adapter):
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Too many requests per minute.",
                }
            },
        )

    client = make_client(adapter, handler, sleeps)
    try:
        with pytest.raises(LLMError) as caught:
            client.chat(messages=[{"role": "user", "content": "Hello"}], stream=False)
    finally:
        close_client(client)
    assert len(requests) == 2
    assert sleeps == [7]
    assert classify_failure_category(caught.value) == FailureCategory.PROVIDER_THROTTLED


def test_billing_reason_takes_precedence_over_generic_throttle_code():
    error = OpenAICompatClient._error_from_response(
        httpx.Response(
            429, json={"error": {"code": "rate_limit_exceeded", "message": "Insufficient credits."}}
        )
    )
    assert error.provider_error_code == "billing_hard_limit_reached"
    assert "configured key" in friendly_llm_error_message(error)
    assert "configured key" in friendly_llm_error_message(str(error))


@pytest.mark.parametrize(
    "adapter",
    [
        OpenAICompatClient,
        OpenAIResponsesClient,
        AnthropicMessagesClient,
        GeminiGenerateContentClient,
    ],
)
def test_stream_billing_message_without_standard_code_is_terminal(adapter):
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return error_event(adapter, {"code": 9999, "message": "No active subscription."})

    client = make_client(adapter, handler, sleeps)
    try:
        with pytest.raises(LLMError) as caught:
            client.chat(messages=[{"role": "user", "content": "Hello"}], stream=True)
    finally:
        close_client(client)
    assert len(requests) == 1
    assert sleeps == []
    assert caught.value.provider_error_code == "billing_hard_limit_reached"


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
