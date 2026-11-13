from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.llm_error_display import friendly_llm_error_message


@pytest.mark.parametrize(
    "status, code, expected",
    [
        (403, "account_restricted", "account requires review"),
        (403, "email_verification_required", "Confirm your Alysis account email"),
        (403, "free_mailbox_conflict", "already linked to another account for this mailbox"),
        (403, "model_not_allowed", "not available on your Alysis hosted plan"),
        (409, "billing_review_required", "needs a billing review"),
        (502, "billing_pending", "Billing reconciliation is pending"),
    ],
)
def test_hosted_error_explains_account_or_plan_denial(status, code, expected):
    body = json.dumps(
        {
            "error": {
                "code": code,
                "message": "Account policy denied this request. Use your own API key meanwhile.",
            }
        }
    )
    error = LLMError(f"LLM error {status}: {body}")
    error.provider_status_code = status
    error.provider_error_body = body
    rendered = friendly_llm_error_message(error)
    assert friendly_llm_error_message(str(error)) == rendered
    assert expected in rendered
    assert "API key was rejected" not in rendered
    assert "Update it with /config" not in rendered


@pytest.mark.parametrize(
    "make_error",
    [
        OpenAICompatClient._error_from_response,
        OpenAIResponsesClient._llm_error_from_response,
        AnthropicMessagesClient._llm_error_from_response,
    ],
)
@pytest.mark.parametrize(
    "status, code, expected",
    [
        (403, "account_restricted", "account requires review"),
        (409, "billing_review_required", "needs a billing review"),
    ],
)
def test_provider_error_factories_preserve_hosted_denial(status, code, expected, make_error):
    payload = {"error": {"code": code, "message": "Account policy denied this request."}}
    error = make_error(httpx.Response(status, json=payload))
    assert error.provider_status_code == status
    assert json.loads(error.provider_error_body) == payload
    rendered = friendly_llm_error_message(error)
    assert expected in rendered
    assert friendly_llm_error_message(str(error)) == rendered


def test_direct_provider_invalid_key_still_uses_byok_guidance():
    error = OpenAIResponsesClient._llm_error_from_response(
        httpx.Response(
            401,
            json={"error": {"code": "invalid_api_key", "message": "Incorrect API key."}},
        )
    )
    assert "Your API key was rejected (401). Update it with /config." in (
        friendly_llm_error_message(error)
    )


@pytest.mark.parametrize(
    "code, message, expected",
    [
        (
            "rate_limit_exceeded",
            "Your account has reached the 20 requests per minute limit. Retry in 42 seconds. No generation was started or credits charged.",
            "Your account has reached the 20 requests per minute limit. Retry in 42 seconds.",
        ),
        (
            "hosted_capacity_exceeded",
            "Hosted model capacity is busy. Retry in 43 seconds. No generation was started or credits charged.",
            "Hosted model capacity is busy. Retry in 43 seconds.",
        ),
        (
            "hosted_capacity_exceeded",
            "Hosted models are at capacity. Please retry in a few seconds.",
            "The Alysis hosted model is unavailable or at capacity.",
        ),
        (
            "rate_limit_exceeded",
            "Your account has reached its hosted request limit. Retry shortly.",
            "Your account has reached its hosted request limit. Retry shortly.",
        ),
        (
            "rate_limit_exceeded",
            "Your available credit allowance is exhausted. Retry when it refills.",
            "Your available credit allowance is exhausted. Retry when it refills.",
        ),
    ],
)
def test_hosted_capacity_and_credit_messages(code, message, expected):
    payload = {"error": {"code": code, "message": message, "type": code}}
    body = json.dumps(payload)
    error = LLMError(f"LLM error 429: {body}")
    error.provider_status_code = 429
    error.provider_error_body = body
    displayed = friendly_llm_error_message(error)
    assert displayed.startswith(expected)
    assert friendly_llm_error_message(str(error)) == displayed
    assert "rate-limiting this session" not in displayed


@pytest.mark.parametrize(
    "client_type, model",
    [(OpenAIResponsesClient, "gpt-6-luna"), (AnthropicMessagesClient, "claude-sonnet-5-5")],
)
@pytest.mark.parametrize("stream", [False, True])
def test_hosted_clients_preserve_capacity_error_across_transports(client_type, model, stream):
    payload = {
        "error": {
            "code": "hosted_capacity_exceeded",
            "type": "hosted_capacity_exceeded",
            "message": "Hosted model capacity is busy. Retry in 43 seconds. No generation was started or credits charged.",
        }
    }
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, json=payload, headers={"Retry-After": "5"})

    client = client_type(
        base_url="https://gateway.example.test/v1",
        api_key="slk_test",
        model=model,
        provider_key="alysis",
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(LLMError) as raised:
        client.chat(messages=[{"role": "user", "content": "hi"}], stream=stream)
    assert len(requests) == 1
    assert raised.value.provider_status_code == 429
    assert json.loads(raised.value.provider_error_body) == payload
    displayed = friendly_llm_error_message(raised.value)
    assert displayed.startswith("Hosted model capacity is busy. Retry in 43 seconds.")
    assert "rate-limiting this session" not in displayed
    assert friendly_llm_error_message(str(raised.value)) == displayed
