from __future__ import annotations

import httpx
import pytest

from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
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
@pytest.mark.parametrize(
    "make_error",
    [
        OpenAICompatClient._error_from_response,
        OpenAIResponsesClient._llm_error_from_response,
        AnthropicMessagesClient._llm_error_from_response,
    ],
)
@pytest.mark.parametrize("as_text", [False, True], ids=["exception", "persisted-text"])
def test_hosted_error_explains_account_or_plan_denial(status, code, expected, make_error, as_text):
    response = httpx.Response(
        status,
        json={
            "error": {
                "code": code,
                "message": "Account policy denied this request. Use your own API key meanwhile.",
            }
        },
    )
    error = make_error(response)
    rendered = friendly_llm_error_message(str(error) if as_text else error)
    assert expected in rendered
    assert "API key was rejected" not in rendered
    assert "Update it with /config" not in rendered


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


def test_uncoded_billing_error_does_not_treat_byok_advice_as_rejected_credentials():
    rendered = friendly_llm_error_message(
        "LLM error 409: Hosted billing requires review. Use your own API key meanwhile."
    )
    assert "billing requires review" in rendered
    assert "API key was rejected" not in rendered


@pytest.mark.parametrize("as_text", [False, True])
def test_native_haiku_capacity_error_preserves_retry_and_no_charge_guidance(as_text):
    message = (
        "Hosted model capacity is busy. Retry in 5 seconds. "
        "No generation was started or credits charged."
    )
    error = AnthropicMessagesClient._llm_error_from_response(
        httpx.Response(
            429, json={"error": {"code": "hosted_capacity_exceeded", "message": message}}
        )
    )
    assert friendly_llm_error_message(str(error) if as_text else error) == message
