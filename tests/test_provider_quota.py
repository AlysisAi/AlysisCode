from __future__ import annotations

import threading
from dataclasses import replace
from email.utils import formatdate

import httpx
import pytest

from alysis_code.agent.session import (
    _protected_computer_use_failure,
    _scrub_sensitive_exception_chain,
)
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.computer_use_diagnostics import (
    DIAGNOSTIC_ATTRIBUTE,
    computer_use_provider_diagnostic,
    protected_provider_diagnostic_marker,
)
from alysis_code.ide.stdio_bridge import _redact_bridge_error
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings, run_provider_limited_call
from alysis_code.llm.rate_limit_metadata import (
    MAX_RETRY_SECONDS,
    bounded_seconds,
    retry_metadata,
)
from alysis_code.llm.types import LLMError


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "code", ["usage_limit_reached", "insufficient_quota", "billing_hard_limit_reached"]
)
def test_quota_rejection_makes_one_http_request_and_survives_protected_bridge(stream, code):
    requests = []
    sleeps = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            429,
            json={
                "error": {
                    "type": code,
                    "message": "private-page-canary",
                    "resets_at": 1_788_888_286,
                    "resets_in_seconds": 3168,
                }
            },
        )

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_sleep_fn=sleeps.append,
        provider_retry_settings=ProviderRetrySettings(max_retries=5),
    )
    with pytest.raises(LLMError) as rejected:
        client.chat(messages=[{"role": "user", "content": "synthetic prompt"}], stream=stream)
    assert len(requests) == 1
    assert sleeps == []
    original = rejected.value
    protected = _protected_computer_use_failure(original)
    _scrub_sensitive_exception_chain(original)
    diagnostic = computer_use_provider_diagnostic(protected)
    assert (diagnostic.status, diagnostic.code) == (429, code)
    assert diagnostic.reset_at >= 1_788_888_286
    assert diagnostic.retry_after >= 3168
    assert f"code={code}" in _redact_bridge_error(None, protected)
    assert f"reset_at={diagnostic.reset_at}" in _redact_bridge_error(None, protected)
    assert "private-page-canary" not in _redact_bridge_error(None, protected)


@pytest.mark.parametrize(
    "code", [None, "private-code-canary", ["malformed"], "rate_limit_exceeded"]
)
def test_subscription_type_cannot_be_masked_by_unknown_or_generic_code(code):
    error = OpenAIResponsesClient._llm_error_from_response(
        httpx.Response(429, json={"error": {"code": code, "type": "usage_limit_reached"}})
    )
    assert error.provider_error_code == "usage_limit_reached"


@pytest.mark.parametrize("header", ["7", "6.1", formatdate(1007, usegmt=True)])
def test_retry_after_and_subscription_reset_choose_longest_wait(header):
    assert retry_metadata(
        httpx.Response(429, headers={"Retry-After": header}),
        {"resets_at": 1005, "resets_in_seconds": 3},
        now=1000,
    ) == (7, 1007)
    assert retry_metadata(httpx.Response(429, headers={"Retry-After": header}), {}, now=1000) == (
        7,
        1007,
    )


@pytest.mark.parametrize("invalid", [True, -1, "private", {}, [], float("inf"), float("nan")])
def test_bad_provider_timing_is_ignored(invalid):
    assert bounded_seconds(invalid) == 0
    assert retry_metadata(
        httpx.Response(429), {"resets_at": invalid, "resets_in_seconds": invalid}, now=1000
    ) == (0, 0)


def test_huge_wait_is_not_converted_to_immediate_retry():
    assert bounded_seconds(10**1000) == MAX_RETRY_SECONDS
    assert (
        retry_metadata(httpx.Response(429), {"resets_in_seconds": 10**1000}, now=1000)[0]
        == MAX_RETRY_SECONDS
    )


def test_non_429_timing_is_not_retained():
    assert retry_metadata(
        httpx.Response(400, headers={"Retry-After": "7"}), {"resets_at": 1005}, now=1000
    ) == (0, 0)


def test_temporary_rate_limit_recovers_after_minimum_wait_without_replaying_actions():
    attempts, actions, sleeps = [], [], []
    error = OpenAIResponsesClient._llm_error_from_response(
        httpx.Response(429, headers={"Retry-After": "7"})
    )

    def provider_request():
        attempts.append(1)
        if len(attempts) == 1:
            raise error
        return {"tool": "click"}

    action = run_provider_limited_call(
        call=provider_request,
        provider_key=None,
        operation="fixture",
        retry_settings=ProviderRetrySettings(base_delay_seconds=1),
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )
    actions.append(action)
    assert sleeps == [7]
    assert len(attempts) == 2
    assert actions == [{"tool": "click"}]


@pytest.mark.parametrize(("wait", "cap"), [(121, None), (7, 5), (MAX_RETRY_SECONDS, None)])
def test_wait_outside_turn_budget_returns_to_ui_without_retry(wait, cap):
    error = OpenAIResponsesClient._llm_error_from_response(
        httpx.Response(429, headers={"Retry-After": str(wait)})
    )
    attempts, sleeps = [], []

    def call():
        attempts.append(1)
        raise error

    with pytest.raises(LLMError):
        run_provider_limited_call(
            call=call,
            provider_key=None,
            operation="fixture",
            sleep_fn=sleeps.append,
            retry_wall_clock_cap_seconds=cap,
            clock_fn=lambda: 0,
            random_fn=lambda: 0.5,
        )
    assert attempts == [1]
    assert sleeps == []


def test_cancelling_rate_limit_backoff_does_not_issue_another_request():
    event = threading.Event()
    attempts = []
    error = OpenAIResponsesClient._llm_error_from_response(
        httpx.Response(429, headers={"Retry-After": "7"})
    )

    def call():
        attempts.append(1)
        raise error

    with pytest.raises(CooperativeCancellationError):
        run_provider_limited_call(
            call=call,
            provider_key=None,
            operation="fixture",
            cancellation_token=EventCancellationToken(event),
            on_retry=lambda *_: event.set(),
        )
    assert attempts == [1]


@pytest.mark.parametrize(
    "fields",
    [
        {"retry_after": True},
        {"retry_after": -1},
        {"retry_after": 2_678_401},
        {"reset_at": "private"},
        {"reset_at": 4_102_444_801},
        {"reset_at": float("nan")},
        {"status": 400, "reset_at": 1000},
    ],
)
def test_forged_timing_cannot_cross_protected_bridge(fields):
    error = OpenAIResponsesClient._llm_error_from_response(httpx.Response(429))
    protected = _protected_computer_use_failure(error)
    diagnostic = computer_use_provider_diagnostic(protected)
    setattr(protected, DIAGNOSTIC_ATTRIBUTE, replace(diagnostic, **fields))
    assert protected_provider_diagnostic_marker(protected) is None
