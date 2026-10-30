"""HTTP-200 Responses failures retain diagnostics and retry only model requests."""

import json

import httpx
import pytest

from alysis_code.agent.session import _protected_computer_use_failure
from alysis_code.computer_use_diagnostics import computer_use_provider_diagnostic
from alysis_code.llm.openai_responses import OpenAIResponsesClient, _responses_stream_error
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError


def event(kind, data):
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()


COMPLETE = event(
    "response.completed",
    {
        "response": {
            "id": "test",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "complete"}],
                }
            ],
        }
    },
)
PARTIAL = event(
    "response.output_text.delta", {"output_index": 0, "content_index": 0, "delta": "partial"}
)


def client_for(body):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body if len(requests) == 1 else COMPLETE,
        )

    return OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="search-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _: None,
    ), requests


@pytest.mark.parametrize("kind", ["error", "response.failed"])
@pytest.mark.parametrize("code", ["rate_limit_exceeded", "server_error", "overloaded_error"])
def test_transient_sse_failure_retries_and_discards_partial_response(kind, code):
    detail = {"code": code, "message": "temporary failure"}
    payload = detail if kind == "error" else {"response": {"error": detail}}
    client, requests = client_for(PARTIAL + event(kind, payload))
    response = client.chat(messages=[{"role": "user", "content": "hello"}], stream=True)
    assert response.content == "complete"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "detail",
    [
        {"code": "unsupported_parameter", "param": "tools"},
        {"code": "insufficient_quota"},
        {"type": "usage_limit_reached"},
        {"message": "unknown failure"},
        {"status_code": 401},
    ],
)
def test_permanent_quota_and_unknown_failures_are_not_retried(detail):
    client, requests = client_for(event("error", detail))
    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hello"}], stream=True)
    assert len(requests) == 1


def test_sse_failure_after_published_text_is_not_replayed():
    client, requests = client_for(PARTIAL + event("error", {"code": "server_error"}))
    deltas = []
    with pytest.raises(LLMError):
        client.chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
            on_text_delta=deltas.append,
        )
    assert deltas == ["partial"]
    assert len(requests) == 1


def test_protected_sse_diagnostics_do_not_retain_provider_payload():
    error = _responses_stream_error(
        {
            "type": "error",
            "code": "unsupported_parameter",
            "param": "tools",
            "message": "private document canary",
        },
        "error",
    )
    protected = _protected_computer_use_failure(error)
    diagnostic = computer_use_provider_diagnostic(protected)
    assert diagnostic.code == "unsupported_parameter"
    assert diagnostic.parameter == "tools"
    assert "private document canary" not in repr(vars(protected))
    unknown = _responses_stream_error({"code": "private-code", "param": "private-param"}, "error")
    assert computer_use_provider_diagnostic(unknown).code == "none"
    assert computer_use_provider_diagnostic(unknown).parameter == "none"
