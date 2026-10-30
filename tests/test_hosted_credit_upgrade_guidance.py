"""Exhausted allowance guidance must not turn every temporary limit into an upsell."""

from __future__ import annotations

import httpx
import pytest

from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm_error_display import friendly_llm_error_message


@pytest.mark.parametrize(
    "make_error",
    [
        OpenAICompatClient._error_from_response,
        OpenAIResponsesClient._llm_error_from_response,
        AnthropicMessagesClient._llm_error_from_response,
    ],
)
@pytest.mark.parametrize("persisted", [False, True])
def test_exhausted_credit_allowance_keeps_refill_message_and_offers_plan_review(
    make_error, persisted
):
    message = "Your available credit allowance is exhausted. Retry when it refills."
    error = make_error(
        httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": message}})
    )
    displayed = friendly_llm_error_message(str(error) if persisted else error)
    assert displayed.startswith(message)
    assert "Run `alysis upgrade` to review plans on your Alysis account page." in displayed
    assert "API key was rejected" not in displayed


def test_exhausted_credit_allowance_does_not_duplicate_gateway_upgrade_guidance():
    message = (
        "Your available credit allowance is exhausted. "
        "Run `alysis upgrade` to review Beta or Alpha, or wait for the next refill."
    )
    error = OpenAICompatClient._error_from_response(
        httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": message}})
    )
    assert friendly_llm_error_message(error) == message


@pytest.mark.parametrize(
    "status,code,message",
    [
        (
            429,
            "rate_limit_exceeded",
            "5-hour fair-use limit reached (25 credits). Retry as usage ages out.",
        ),
        (429, "rate_limit_exceeded", "Weekly fair-use limit reached (50 credits in 7 days)."),
        (
            429,
            "rate_limit_exceeded",
            "Available credits cannot cover this request while other usage or reservations apply.",
        ),
        (
            429,
            "rate_limit_exceeded",
            "Four hosted requests are already running for your account. Retry shortly.",
        ),
        (
            503,
            "hosted_model_paused",
            "Mistral Large 4 is paused because its provider reached a spending limit.",
        ),
        (403, "plan_unavailable", "Your plan needs attention. Contact support."),
    ],
)
def test_temporary_or_provider_limits_keep_gateway_guidance_without_upgrade(status, code, message):
    error = OpenAICompatClient._error_from_response(
        httpx.Response(status, json={"error": {"code": code, "message": message}})
    )
    assert friendly_llm_error_message(error) == message
    assert "alysis upgrade" not in friendly_llm_error_message(error)


def test_revoked_key_keeps_reconnect_guidance_without_upgrade():
    error = OpenAICompatClient._error_from_response(
        httpx.Response(401, json={"error": {"code": "invalid_key", "message": "Revoked key."}})
    )
    displayed = friendly_llm_error_message(error)
    assert "Run `alysis login` to reconnect your account." in displayed
    assert "alysis upgrade" not in displayed


def test_exhausted_wording_with_an_unrecognized_error_code_does_not_trigger_upgrade():
    error = OpenAICompatClient._error_from_response(
        httpx.Response(
            429,
            json={
                "error": {
                    "code": "other_provider_limit",
                    "message": "Your available credit allowance is exhausted.",
                }
            },
        )
    )
    assert "alysis upgrade" not in friendly_llm_error_message(error)
