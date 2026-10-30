from __future__ import annotations

import json
import logging
import ssl
import threading

import httpx
import pytest

from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.execution_deadline import DeadlineExhausted
from alysis_code.llm import openai_compat as openai_compat_mod
from alysis_code.llm import types as shared_types
from alysis_code.llm.base import effective_tools_for_client
from alysis_code.llm.cache_capabilities import (
    CACHE_CONTROL_FIELD,
    OPENROUTER_SESSION_ID_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
)
from alysis_code.llm.metadata import (
    endpoint_descriptor,
    endpoint_label,
    stamp_provider_metadata_for_route,
)
from alysis_code.llm.openai_compat import (
    PROVIDER_METADATA_KEY,
    LLMError,
    LLMResponse,
    LLMUsage,
    OpenAICompatClient,
    ToolCall,
    _httpx_request_timeout,
    _provider_key_from_base_url,
    alysis_trial_error_message,
    attach_provider_metadata_to_assistant_message,
)
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMStreamNoProgressError
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)
from alysis_code.request_estimation import estimate_provider_payload_tokens
from alysis_code.session_store import SessionStore

_ALYSIS_TRIAL_BASE_URL = "https://api.alysiscode.com/v1"


def test_openai_compat_reexports_shared_llm_types() -> None:
    assert LLMError is shared_types.LLMError
    assert ToolCall is shared_types.ToolCall
    assert LLMUsage is shared_types.LLMUsage
    assert LLMResponse is shared_types.LLMResponse


def test_malformed_response_error_never_echoes_raw_reasoning_payload() -> None:
    sentinel = "PRIVATE_RAW_REASONING_SENTINEL"
    response = httpx.Response(
        200,
        json={
            "choices": [],
            "reasoning_content": sentinel,
            "reasoning_details": [{"type": "reasoning.text", "text": sentinel}],
        },
    )

    with pytest.raises(LLMError) as exc_info:
        OpenAICompatClient._parse_non_stream_response(
            response,
            provider_key="deepseek",
        )

    assert "missing choices[0]" in str(exc_info.value)
    assert sentinel not in str(exc_info.value)


def test_count_input_tokens_measures_provider_shaped_multilingual_payload() -> None:
    client = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen3.7-plus",
        provider_key="qwen",
        prompt_cache_request_field_values={CACHE_CONTROL_FIELD: "ephemeral"},
        prompt_cache_policy_metadata={
            "strategy": "qwen_cache_control_blocks",
            "enabled": True,
            "status": "enabled",
        },
    )
    measured = client.count_input_tokens(
        messages=[
            {"role": "system", "content": "Απάντησε με ακρίβεια."},
            {"role": "user", "content": "中文 العربية 👩🏽‍💻"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ],
    )

    assert measured.input_tokens > 0
    assert measured.source.value == "local_estimate"
    assert measured.confidence.value == "estimated"
    assert measured.raw_provider_usage == {
        "estimator": "cl100k_base",
        "estimate_basis": "provider_prompt_payload",
        "provider_key": "qwen",
        "protocol": "openai_compat",
        "model": "qwen3.7-plus",
        "message_count": 2,
        "tool_count": 1,
    }


def test_alysis_trial_error_message_maps_known_codes() -> None:
    err = LLMError(
        "LLM error 402: "
        + json.dumps({"error": {"message": "Trial window passed.", "code": "trial_expired"}})
    )
    msg = alysis_trial_error_message(err)
    assert msg is not None
    assert "trial has ended" in msg
    # Derived, so moving the product site does not require editing this test.
    from alysis_code.alysis_cloud import account_url

    assert account_url() in msg
    assert "{account_url}" not in msg, "placeholder was not substituted"


def test_alysis_trial_error_message_handles_each_proxy_code() -> None:
    cases = {
        "invalid_key": "alysis login",
        "quota_exhausted": "trial tokens",
        "email_not_verified": "confirm your email",
        "plan_inactive": "not active",
        "rate_limit_exceeded": "wait a moment",
        "hosted_capacity_exceeded": "unavailable or at capacity",
        "hosted_routing_unavailable": "routing is temporarily unavailable",
        "global_budget_exceeded": "at capacity",
        "proxy_unconfigured": "temporarily unavailable",
    }
    for code, needle in cases.items():
        err = LLMError("LLM error 400: " + json.dumps({"error": {"code": code}}))
        msg = alysis_trial_error_message(err)
        assert msg is not None, code
        assert needle in msg, code


def test_alysis_trial_error_message_ignores_non_proxy_errors() -> None:
    # Upstream OpenRouter error: numeric code, not one of ours.
    upstream = LLMError(
        "LLM error 401: " + json.dumps({"error": {"message": "User not found.", "code": 401}})
    )
    assert alysis_trial_error_message(upstream) is None

    # Unknown string code.
    unknown = LLMError("LLM error 500: " + json.dumps({"error": {"code": "kaboom"}}))
    assert alysis_trial_error_message(unknown) is None

    # Non-JSON body (e.g. a plain-text 500 from an edge/CDN layer).
    plain = LLMError("LLM error 500: Internal Server Error")
    assert alysis_trial_error_message(plain) is None


def test_alysis_hosted_limits_preserve_the_actual_reason() -> None:
    messages = [
        "5-hour fair-use limit reached (25 credits). Retry as usage ages out.",
        "Weekly fair-use limit reached (50 credits in 7 days).",
        "Four hosted requests are already running for your account. Retry shortly.",
        "Available credits cannot cover this request while other usage or reservations apply. Reduce context or retry later.",
    ]
    for message in messages:
        error = LLMError(
            "LLM error 429: "
            + json.dumps(
                {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": message,
                    }
                }
            )
        )
        assert alysis_trial_error_message(error) == message


def _surrogate_escaped_text(text: str) -> str:
    return text.encode("utf-8").decode("ascii", errors="surrogateescape")


def test_parses_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "test-model"
        assert body["temperature"] == 1.0
        data = {
            "choices": [
                {
                    "message": {
                        "content": "ok",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "fs_read",
                                    "arguments": json.dumps({"path": "README.md", "max_bytes": 10}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "fs_read"
    assert tc.arguments["path"] == "README.md"


def test_sends_custom_temperature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["temperature"] == 0.2
        data = {"choices": [{"message": {"content": "ok"}}]}
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        temperature=0.2,
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"


def test_sends_extra_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        extra_headers={"anthropic-version": "2023-06-01"},
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"


def test_request_timeout_uses_short_connect_timeout() -> None:
    timeout = _httpx_request_timeout(60.0)
    assert timeout.connect == 2.0
    assert timeout.read == 60.0
    assert timeout.write == 60.0
    assert timeout.pool == 60.0

    tiny_timeout = _httpx_request_timeout(0.5)
    assert tiny_timeout.connect == 0.5
    assert tiny_timeout.read == 0.5


def test_connect_errors_retry_same_request_with_provider_backoff_and_telemetry() -> None:
    reset_provider_telemetry_for_tests()
    attempts = 0
    sleeps: list[float] = []
    request_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request_bodies.append(request.content)
        if attempts <= 2:
            cause = ssl.SSLError("_ssl.c:1015: The handshake operation timed out")
            raise httpx.ConnectError("_ssl.c:1015: The handshake operation timed out") from cause
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
        provider_sleep_fn=sleeps.append,
        provider_random_fn=lambda: 0.5,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.content == "ok"
    assert attempts == 3
    assert request_bodies == [request_bodies[0]] * 3
    assert sleeps == [10.0, 20.0]
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["retry_count"] == 2
    assert summary["retry_reasons"] == ["provider_unavailable"]


def test_read_timeout_retries_instead_of_ending_the_agent_step() -> None:
    attempts = 0
    request_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request_bodies.append(request.content)
        if attempts == 1:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=1,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
        ),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
    )

    response = client.chat(messages=[{"role": "user", "content": "keep this step"}])

    assert response.content == "ok"
    assert attempts == 2
    assert request_bodies == [request_bodies[0], request_bodies[0]]


def test_auth_errors_still_do_not_retry_provider_backoff() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, text="invalid api key")

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=5),
        provider_sleep_fn=lambda _seconds: (_ for _ in ()).throw(
            AssertionError("auth errors must not retry")
        ),
    )

    with pytest.raises(LLMError, match="LLM error 401"):
        client.chat(messages=[{"role": "user", "content": "hi"}])

    assert attempts == 1


def test_connect_errors_exhaust_retries_with_same_final_llm_error() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        cause = ssl.SSLError("_ssl.c:1015: The handshake operation timed out")
        raise httpx.ConnectError("_ssl.c:1015: The handshake operation timed out") from cause

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=2,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
        ),
        provider_sleep_fn=sleeps.append,
        provider_random_fn=lambda: 0.5,
    )
    with pytest.raises(LLMError) as excinfo:
        client.chat(messages=[{"role": "user", "content": "hi"}])

    error_text = str(excinfo.value)
    assert error_text.startswith("LLM request failed for example.com (endpoint ")
    assert error_text.endswith(": _ssl.c:1015: The handshake operation timed out")
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


def test_network_error_and_persisted_event_never_include_secret_endpoint(
    tmp_path,
) -> None:
    sentinel = "PRIVATE_ENDPOINT_SENTINEL"
    secret_base_url = (
        f"https://route-user:route-password@example.com/private/{sentinel}?token={sentinel}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"connection failed for {secret_base_url}",
            request=request,
        )

    client = OpenAICompatClient(
        base_url=secret_base_url,
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
    )

    with pytest.raises(LLMError) as exc_info:
        client.chat(messages=[{"role": "user", "content": "hi"}])

    error_text = str(exc_info.value)
    assert endpoint_label(secret_base_url) in error_text
    assert sentinel not in error_text
    assert "route-user" not in error_text
    assert "route-password" not in error_text

    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id="safe-network-error",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )
    try:
        store.append("error", {"error": error_text})
    finally:
        store.close()
    persisted = (tmp_path / "safe-network-error.jsonl").read_text(encoding="utf-8")
    assert sentinel not in persisted
    assert "route-user" not in persisted
    assert "route-password" not in persisted


def test_chat_decompression_error_is_explicit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=b'{"choices":[{"message":{"content":"ok"}}]}',
        )

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMError, match="response decompression failed"):
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])


def test_chat_temperature_override_takes_precedence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["temperature"] == 0.9
        data = {"choices": [{"message": {"content": "ok"}}]}
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        temperature=0.2,
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[], temperature=0.9)
    assert resp.content == "ok"


def test_retries_with_default_temperature_when_provider_accepts_only_default() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body["temperature"] == 0.0
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Unsupported value: 'temperature' does not support 0 with "
                            "this model. Only the default (1) value is supported."
                        ),
                        "type": "invalid_request_error",
                        "param": "temperature",
                        "code": "unsupported_value",
                    }
                },
            )
        assert body["temperature"] == 1.0
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="gpt-5.5",
        temperature=0.0,
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "hi"}])
    second = client.chat(messages=[{"role": "user", "content": "hi again"}])

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 3
    assert requests[0]["temperature"] == 0.0
    assert requests[1]["temperature"] == 1.0
    assert requests[2]["temperature"] == 1.0
    assert first.provider_metadata is not None
    assert first.provider_metadata["transport"] == {
        "temperature_adjusted": True,
        "temperature_adjustment": "default_temperature",
        "temperature_adjustment_reason": "provider_rejected_parameter",
        "temperature_retry_used": True,
        "temperature_retry_count": 1,
    }
    assert first.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"
    assert second.provider_metadata is not None
    assert second.provider_metadata["transport"] == {
        "temperature_adjusted": True,
        "temperature_adjustment": "default_temperature",
        "temperature_adjustment_reason": "cached_provider_rejection",
    }
    assert second.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"


def test_omits_temperature_when_default_temperature_is_also_rejected() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) <= 2:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "temperature is not supported for this model.",
                        "type": "invalid_request_error",
                        "param": "temperature",
                        "code": "unsupported_parameter",
                    }
                },
            )
        assert "temperature" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="no-temperature-model",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "hi"}])
    second = client.chat(messages=[{"role": "user", "content": "hi again"}])

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 4
    assert requests[0]["temperature"] == 0.2
    assert requests[1]["temperature"] == 1.0
    assert "temperature" not in requests[2]
    assert "temperature" not in requests[3]
    assert first.provider_metadata is not None
    assert first.provider_metadata["transport"] == {
        "temperature_adjusted": True,
        "temperature_adjustment": "omit_temperature",
        "temperature_adjustment_reason": "provider_rejected_parameter",
        "temperature_retry_used": True,
        "temperature_retry_count": 2,
        "temperature_omitted": True,
        "temperature_omit_reason": "provider_rejected_parameter",
    }
    assert first.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"
    assert second.provider_metadata is not None
    assert second.provider_metadata["transport"] == {
        "temperature_adjusted": True,
        "temperature_adjustment": "omit_temperature",
        "temperature_adjustment_reason": "cached_provider_rejection",
        "temperature_omitted": True,
        "temperature_omit_reason": "cached_provider_rejection",
    }
    assert second.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"


def test_retries_with_default_temperature_for_plain_text_temperature_error() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(400, text="temperature is not supported for this model")
        assert body["temperature"] == 1.0
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="plain-text-error-model",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.content == "ok"
    assert len(requests) == 2
    assert requests[0]["temperature"] == 0.2
    assert requests[1]["temperature"] == 1.0


def test_retries_with_default_temperature_for_temperature_range_error() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "temperature must be in range (0.0, 1.0]",
                        "type": "invalid_request_error",
                        "param": "temperature",
                        "code": "invalid_parameter",
                    }
                },
            )
        assert body["temperature"] == 1.0
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="temperature-range-model",
        temperature=0.0,
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.content == "ok"
    assert len(requests) == 2
    assert requests[0]["temperature"] == 0.0
    assert requests[1]["temperature"] == 1.0


def test_omits_temperature_when_provider_allows_only_non_default_temperature() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) <= 2:
            return httpx.Response(400, text="invalid temperature: only 0.6 is allowed")
        assert "temperature" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="fixed-temperature-model",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.content == "ok"
    assert len(requests) == 3
    assert requests[0]["temperature"] == 0.2
    assert requests[1]["temperature"] == 1.0
    assert "temperature" not in requests[2]


def test_retries_without_temperature_for_deprecated_temperature_error_without_param() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body["temperature"] == 0.2
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_request_error",
                        "message": "`temperature` is deprecated for this model.",
                        "type": "invalid_request_error",
                        "param": None,
                    }
                },
            )
        assert "temperature" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test",
        model="claude-future-model",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.content == "ok"
    assert len(requests) == 2


def test_temperature_retry_does_not_hide_unrelated_provider_errors() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "not_found_error",
                    "message": "model: missing-model",
                    "type": "invalid_request_error",
                    "param": None,
                }
            },
        )

    client = OpenAICompatClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test",
        model="missing-model",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMError, match="not_found_error"):
        client.chat(messages=[{"role": "user", "content": "hi"}])

    assert len(requests) == 1
    assert requests[0]["temperature"] == 0.2


def test_stream_retries_with_default_temperature_when_provider_accepts_only_default() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Unsupported value: 'temperature' does not support 0 with "
                            "this model. Only the default (1) value is supported."
                        ),
                        "type": "invalid_request_error",
                        "param": "temperature",
                        "code": "unsupported_value",
                    }
                },
            )
        assert body["temperature"] == 1.0
        assert body["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"o"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"k"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="gpt-5.5",
        temperature=0.0,
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)

    assert resp.content == "ok"
    assert len(requests) == 2
    assert requests[0]["temperature"] == 0.0
    assert requests[1]["temperature"] == 1.0


def test_stream_cancellation_token_interrupts_midstream() -> None:
    # A cancel mid-stream must raise KeyboardInterrupt (so the TUI shows
    # "Interrupted.") and stop consuming further deltas — not finish the response.
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"c"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="m",
        transport=httpx.MockTransport(handler),
    )

    class _Tok:
        def __init__(self) -> None:
            self._cancelled = False
            self._abort = None
            self.clear_calls = 0

        def set_abort_callback(self, fn):
            self._abort = fn

        def clear_abort_callback(self):
            self.clear_calls += 1
            self._abort = None

        @property
        def is_cancelled(self) -> bool:
            return self._cancelled

        def cancel(self) -> None:
            self._cancelled = True
            if self._abort is not None:
                self._abort()

    tok = _Tok()
    seen: list[str] = []

    def on_text(delta: str) -> None:
        seen.append(delta)
        tok.cancel()  # user interrupts right after the first delta

    with pytest.raises(KeyboardInterrupt):
        client.chat(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            on_text_delta=on_text,
            cancellation_token=tok,
        )
    assert seen == ["a"]  # stopped immediately, did not consume "b"/"c"
    assert tok.clear_calls == 1
    assert tok._abort is None


def test_stream_cancellation_aborts_request_blocked_before_response_headers() -> None:
    class _BlockedBeforeHeadersTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.request_started = threading.Event()
            self.close_called = threading.Event()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.request_started.set()
            assert self.close_called.wait(timeout=2), "live HTTP client was not aborted"
            raise httpx.ReadError("request closed before response headers", request=request)

        def close(self) -> None:
            self.close_called.set()

    transport = _BlockedBeforeHeadersTransport()
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
    )
    token = InteractiveCancellationToken()
    outcome: list[BaseException | LLMResponse] = []

    def _chat() -> None:
        try:
            outcome.append(
                client.chat(
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                    cancellation_token=token,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - cancellation is the assertion
            outcome.append(exc)

    worker = threading.Thread(target=_chat, daemon=True)
    worker.start()
    assert transport.request_started.wait(timeout=1)

    token.cancel()
    worker.join(timeout=1)

    assert transport.close_called.is_set()
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], KeyboardInterrupt)
    assert "cancelled_by_user" in str(outcome[0])
    assert token._abort is None  # noqa: SLF001


def test_chat_sends_tool_choice_and_response_format() -> None:
    tool_choice = {"type": "function", "function": {"name": "emit_json"}}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "emit_json",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["tools"] == tools
        assert body["tool_choice"] == tool_choice
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice=tool_choice,
        response_format={"type": "json_object"},
    )

    assert resp.content == "{}"


def test_tools_omit_default_tool_choice_auto() -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": "fs_read", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=tools)

    assert captured["tools"] == tools
    assert "tool_choice" not in captured
    assert resp.content == "ok"


def test_retries_without_tool_choice_when_provider_rejects_param() -> None:
    forced = {"type": "function", "function": {"name": "fs_read"}}
    tools = [
        {
            "type": "function",
            "function": {"name": "fs_read", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body["tool_choice"] == forced
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Thinking mode does not support this tool_choice",
                        "param": "tool_choice",
                    }
                },
            )
        assert "tool_choice" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com/v1",
        api_key="test",
        model="deepseek-v4-pro",
        enable_thinking=False,
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice=forced,
    )
    second = client.chat(
        messages=[{"role": "user", "content": "hi again"}],
        tools=tools,
        tool_choice=forced,
    )

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 3
    assert requests[0]["tool_choice"] == forced
    assert "tool_choice" not in requests[1]
    assert "tool_choice" not in requests[2]
    assert first.provider_metadata is not None
    assert set(first.provider_metadata) <= {"transport", "openai_compat", "_route_identity"}
    assert first.provider_metadata["transport"] == {
        "tool_choice_omitted": True,
        "tool_choice_omit_reason": "provider_rejected_parameter",
        "tool_choice_retry_used": True,
    }
    assert second.provider_metadata is not None
    assert set(second.provider_metadata) <= {"transport", "openai_compat", "_route_identity"}
    assert second.provider_metadata["transport"] == {
        "tool_choice_omitted": True,
        "tool_choice_omit_reason": "cached_provider_rejection",
    }


def test_retries_without_tools_when_full_error_body_rejects_tool_calling(caplog) -> None:
    reset_provider_telemetry_for_tests()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "diagnostic_echo",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            ("x" * 1100)
                            + " tools are not supported by this model for function calling"
                        )
                    }
                },
            )
        assert "tools" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    secret_base_url = "https://route-user:route-password@example.com/private/route-token"
    client = OpenAICompatClient(
        base_url=secret_base_url,
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level(logging.INFO, logger=openai_compat_mod.__name__):
        response = client.chat(messages=[{"role": "user", "content": "hi"}], tools=tools)

    assert response.content == "ok"
    assert len(requests) == 2
    assert "tools" in requests[0]
    assert "tools" not in requests[1]
    assert response.provider_metadata is not None
    assert set(response.provider_metadata) <= {
        "transport",
        "openai_compat",
        "_route_identity",
    }
    assert response.provider_metadata["transport"] == {
        "tools_omitted": True,
        "tools_omit_reason": "provider_rejected_tool_calling",
        "tools_retry_used": True,
    }
    request_plan = response.provider_metadata["openai_compat"]["request_plan"]
    assert request_plan["input_mode"] == "tool_calling_fallback"
    assert request_plan["tool_count"] == 0
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_shape"]["input_mode"] == "tool_calling_fallback"
    assert summary["request_shape"]["tool_count"] == 0
    assert summary["token_reconciliation"]["input_mode"] == "tool_calling_fallback"
    assert (
        summary["token_reconciliation"]["input_estimate_tokens"]
        == summary["token_reconciliation"]["sent_input_estimate_tokens"]
    )
    assert effective_tools_for_client(client, tools) is None
    retry_record = next(
        record
        for record in caplog.records
        if record.message == "llm_tool_calling_rejected_retrying_without_tools"
    )
    assert retry_record.base_url_descriptor == endpoint_descriptor(secret_base_url)
    serialized_record = json.dumps(retry_record.__dict__, default=str, sort_keys=True)
    assert "route-user" not in serialized_record
    assert "route-password" not in serialized_record
    assert "private/route-token" not in serialized_record
    measured = client.count_input_tokens(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
    )
    assert measured.raw_provider_usage is not None
    assert measured.raw_provider_usage["tool_count"] == 0


def test_forced_tool_choice_omitted_in_openrouter_thinking_mode() -> None:
    # Regression: a reasoning model (Xiaomi MiMo via the OpenRouter shape) returns
    # 400 "Thinking mode does not support this tool_choice" when the agent forces a
    # specific tool (recovery / completion gate) while thinking is on. The client
    # must omit the parameter so the turn keeps moving instead of crashing the
    # whole run.
    forced = {"type": "function", "function": {"name": "fs_read"}}
    tools = [
        {
            "type": "function",
            "function": {"name": "fs_read", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="xiaomi/mimo",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    )
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice=forced,
    )

    assert captured["reasoning"] == {"enabled": True}  # thinking is on
    assert "tool_choice" not in captured
    assert captured["tools"] == tools  # tools still offered
    assert resp.content == "ok"


def test_forced_tool_choice_preserved_when_thinking_disabled() -> None:
    # The downgrade is scoped to thinking mode: with reasoning off, the same
    # provider must still forward a forced tool_choice unchanged.
    forced = {"type": "function", "function": {"name": "fs_read"}}
    tools = [
        {
            "type": "function",
            "function": {"name": "fs_read", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="xiaomi/mimo",
        enable_thinking=False,
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice=forced,
    )

    assert captured["tool_choice"] == forced


def test_required_tool_choice_omitted_in_deepseek_thinking_mode() -> None:
    # The string forms ("required"/"any") force a call too, and omission also
    # covers the DeepSeek "thinking" payload branch.
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test",
        model="deepseek-v4-pro",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
    )

    assert captured["thinking"] == {"type": "enabled"}
    assert "tool_choice" not in captured


@pytest.mark.parametrize(
    "tool_choice",
    [
        "auto",
        "none",
        "required",
        {"type": "function", "function": {"name": "fs_read"}},
    ],
    ids=("auto", "none", "required", "forced-function"),
)
def test_deepseek_default_thinking_omits_every_tool_choice(tool_choice: object) -> None:
    captured: dict[str, object] = {}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "fs_read",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test",
        model="deepseek-v4-pro",
        transport=httpx.MockTransport(handler),
    )

    assert client.reasoning_active is True
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice=tool_choice,
    )

    assert "thinking" not in captured
    assert "reasoning_effort" not in captured
    assert "tool_choice" not in captured
    assert captured["tools"] == tools


def test_deepseek_explicitly_disabled_thinking_preserves_tool_choice() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test",
        model="deepseek-v4-pro",
        enable_thinking=False,
        transport=httpx.MockTransport(handler),
    )
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
    )

    assert client.reasoning_active is False
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["tool_choice"] == "required"


def test_tool_call_arguments_non_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        data = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "shell_run", "arguments": "not-json"},
                            }
                        ],
                    }
                }
            ]
        }
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.tool_calls[0].arguments["_raw_arguments"] == "not-json"


def test_normalizes_non_stream_content_arrays_to_text() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Hello"},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "https://example.com/image.png"},
                                },
                                {"type": "output_text", "text": " world"},
                            ]
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "Hello world"


def test_non_stream_content_array_keeps_tool_calls() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [{"type": "text", "text": "ok"}],
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "fs_read",
                                        "arguments": json.dumps({"path": "README.md"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "fs_read"


def test_deepseek_reasoning_content_round_trips_for_tool_calls() -> None:
    calls: list[dict[str, object]] = []
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            assert body["thinking"] == {"type": "enabled"}
            assert "enable_thinking" not in body
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": "hidden reasoning state",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": json.dumps({"path": "README.md"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        assistant_message = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert assistant_message["reasoning_content"] == "hidden reasoning state"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test",
        model="deepseek-v4-pro",
        enable_thinking=True,
        transport=transport,
    )

    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[],
        on_reasoning_delta=reasoning_deltas.append,
    )
    assert first.content == ""
    assert reasoning_deltas == ["hidden reasoning state"]
    assert first.provider_metadata is not None
    assert first.provider_metadata["deepseek"] == {"reasoning_content": "hidden reasoning state"}
    assert first.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )
    assert PROVIDER_METADATA_KEY in assistant_message

    second = client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_1", "content": '{"content":"x"}'},
        ],
        tools=[],
    )
    assert second.content == "ok"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("base_url", "provider_key"),
    [
        ("https://api.deepseek.com", None),
        (_ALYSIS_TRIAL_BASE_URL, "alysis"),
    ],
    ids=("first-party", "hosted-proxy"),
)
def test_deepseek_reasoning_round_trips_for_normal_assistant_turns(
    stream: bool,
    base_url: str,
    provider_key: str | None,
) -> None:
    calls: list[dict[str, object]] = []
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            if stream:
                events = [
                    {"choices": [{"delta": {"reasoning_content": "private deepseek state"}}]},
                    {"choices": [{"delta": {"content": "Public answer."}}]},
                ]
                body_sse = "".join(f"data: {json.dumps(event)}\n" for event in events)
                return httpx.Response(200, text=body_sse + "data: [DONE]\n")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "private deepseek state",
                                "content": "Public answer.",
                            }
                        }
                    ]
                },
            )

        assistant_wire = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_wire
        assert assistant_wire["content"] == "Public answer."
        assert assistant_wire["reasoning_content"] == "private deepseek state"
        assert body["tools"] == tools
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model="deepseek-v4-pro",
        provider_key=provider_key,
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "reason"}],
        tools=tools,
        stream=stream,
    )
    assert first.content == "Public answer."
    assert first.provider_metadata is not None
    assert first.provider_metadata["deepseek"] == {"reasoning_content": "private deepseek state"}
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )
    assert PROVIDER_METADATA_KEY in assistant_message
    assert "reasoning_content" not in assistant_message

    second = client.chat(
        messages=[
            {"role": "user", "content": "reason"},
            assistant_message,
            {"role": "user", "content": "continue"},
        ],
        tools=tools,
    )
    assert second.content == "ok"


@pytest.mark.parametrize("stream", [False, True])
def test_together_deepseek_reasoning_round_trips_for_tool_calls(stream: bool) -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        assert body["reasoning_effort"] == "max"
        assert "thinking" not in body
        assert "enable_thinking" not in body
        assert "reasoning" not in body
        if len(calls) == 1:
            if stream:
                event = {
                    "choices": [
                        {
                            "delta": {
                                "reasoning": "opaque together state",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": json.dumps({"path": "README.md"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
                return httpx.Response(
                    200,
                    text=f"data: {json.dumps(event)}\ndata: [DONE]\n",
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning": "opaque together state",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": json.dumps({"path": "README.md"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        assistant_message = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert assistant_message["reasoning"] == "opaque together state"
        assert "reasoning_content" not in assistant_message
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.together.ai/v1",
        api_key="test",
        model="deepseek-ai/DeepSeek-V4-Pro-0813",
        provider_key="together",
        reasoning_effort="max",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[],
        stream=stream,
    )
    assert first.provider_metadata is not None
    assert first.provider_metadata["deepseek"] == {"reasoning_content": "opaque together state"}
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    second = client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        tools=[],
    )
    assert second.content == "ok"


@pytest.mark.parametrize(
    ("base_url", "expected_field"),
    [
        # OpenAI's gpt-5 / o-series reject max_tokens outright (live 400 on
        # 2026-09-04: "Unsupported parameter: 'max_tokens' is not supported
        # with this model. Use 'max_completion_tokens' instead.").
        ("https://api.openai.com/v1", "max_completion_tokens"),
        # Third-party gateways keep the legacy spelling many of them require —
        # even when the model name would make the provider look like OpenAI.
        ("https://openrouter.ai/api/v1", "max_tokens"),
        ("https://gateway.example/v1", "max_tokens"),
    ],
)
def test_output_limit_field_follows_the_transport_host(base_url: str, expected_field: str) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="k",
        model="gpt-5.6-terra",
        transport=httpx.MockTransport(handler),
    )
    client.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=64)

    assert captured[expected_field] == 64
    other = "max_tokens" if expected_field == "max_completion_tokens" else "max_completion_tokens"
    assert other not in captured


def test_kimi_k3_uses_documented_request_shape_and_round_trips_reasoning() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        assert "temperature" not in body
        assert "thinking" not in body
        assert "enable_thinking" not in body
        assert body["reasoning_effort"] == "high"
        assert body["prompt_cache_key"] == "stable-session"
        if len(calls) == 1:
            assert body["tool_choice"] == "required"
            assert body["max_completion_tokens"] == 4096
            assert "max_tokens" not in body
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "Public answer.",
                                "reasoning_content": "opaque kimi continuation state",
                            }
                        }
                    ]
                },
            )

        assistant_wire = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_wire
        assert assistant_wire["reasoning_content"] == "opaque kimi continuation state"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.moonshot.ai/v1",
        api_key="test",
        model="kimi-k3",
        prompt_cache_key="stable-session",
        enable_thinking=False,
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "reason"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="required",
        max_tokens=4096,
    )
    assert first.provider_metadata is not None
    assert first.provider_metadata["moonshot"] == {
        "reasoning_content": "opaque kimi continuation state"
    }
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )

    second = client.chat(
        messages=[
            {"role": "user", "content": "reason"},
            assistant_message,
            {"role": "user", "content": "continue"},
        ],
        tools=[],
    )
    assert second.content == "ok"


def test_kimi_k27_omits_forced_tool_choice_because_thinking_is_always_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "temperature" not in body
        assert "reasoning_effort" not in body
        assert "thinking" not in body
        assert "tool_choice" not in body
        assert body["tools"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.moonshot.cn/v1",
        api_key="test",
        model="kimi-k2.7-code",
        enable_thinking=False,
        reasoning_trace_adapter="none",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "use a tool"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="required",
    )
    assert response.content == "ok"


def test_kimi_code_k3_uses_membership_surface_reasoning_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.kimi.com/coding/v1",
        api_key="test",
        model="k3",
        enable_thinking=False,
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["reasoning_effort"] == "high"
    assert "thinking" not in captured
    assert "enable_thinking" not in captured


@pytest.mark.parametrize(
    ("enable_thinking", "expected_thinking", "expects_tool_choice"),
    [
        (None, None, False),
        (True, {"type": "enabled"}, False),
        (False, {"type": "disabled"}, True),
    ],
)
def test_kimi_k26_tool_choice_tracks_optional_thinking_mode(
    enable_thinking: bool | None,
    expected_thinking: dict[str, str] | None,
    expects_tool_choice: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if expected_thinking is None:
            assert "thinking" not in body
        else:
            assert body["thinking"] == expected_thinking
        assert ("tool_choice" in body) is expects_tool_choice
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.moonshot.ai/v1",
        api_key="test",
        model="kimi-k2.6",
        enable_thinking=enable_thinking,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "use a tool"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="required",
    )
    assert response.content == "ok"


@pytest.mark.parametrize("stream", [False, True])
def test_kimi_top_level_cached_tokens_are_normalized(stream: bool) -> None:
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 10,
        "total_tokens": 130,
        "cached_tokens": 80,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        if not stream:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": usage,
                },
            )
        body = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "opaque stream state",
                                "content": "ok",
                            }
                        }
                    ]
                }
            )
            + "\n\ndata: "
            + json.dumps({"choices": [], "usage": usage})
            + "\n\ndata: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = OpenAICompatClient(
        base_url="https://api.moonshot.ai/v1",
        api_key="test",
        model="kimi-k3",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=stream,
    )

    assert response.usage is not None
    assert response.usage.cache_read_input_tokens == 80
    assert response.usage.cached_prompt_tokens == 80
    assert response.usage.input_tokens_uncached == 40
    if stream:
        assert response.provider_metadata is not None
        assert response.provider_metadata["moonshot"] == {
            "reasoning_content": "opaque stream state"
        }


@pytest.mark.parametrize("stream", [False, True])
def test_qwen38_reasoning_round_trips_for_normal_assistant_turns(stream: bool) -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            if stream:
                events = [
                    {"choices": [{"delta": {"reasoning_content": "private qwen state"}}]},
                    {"choices": [{"delta": {"content": "Public answer."}}]},
                ]
                body_sse = "".join(f"data: {json.dumps(event)}\n" for event in events)
                return httpx.Response(200, text=body_sse + "data: [DONE]\n")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": "private qwen state",
                                "content": "Public answer.",
                            }
                        }
                    ]
                },
            )

        assistant_wire = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_wire
        assert assistant_wire["content"] == "Public answer."
        assert assistant_wire["reasoning_content"] == "private qwen state"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen3.8-max",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "reason"}],
        tools=[],
        stream=stream,
    )
    assert first.content == "Public answer."
    assert first.provider_metadata is not None
    assert first.provider_metadata["qwen"] == {"reasoning_content": "private qwen state"}
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )
    assert PROVIDER_METADATA_KEY in assistant_message
    assert "reasoning_content" not in assistant_message

    second = client.chat(
        messages=[
            {"role": "user", "content": "reason"},
            assistant_message,
            {"role": "user", "content": "continue"},
        ],
        tools=[],
    )
    assert second.content == "ok"


def test_qwen_streamed_reasoning_round_trips_opaquely_for_tool_calls() -> None:
    calls: list[dict[str, object]] = []
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            events = [
                {"choices": [{"delta": {"reasoning_content": "private "}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "qwen state",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": '{"path":"README.md"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ]
            body_sse = "".join(f"data: {json.dumps(event)}\n" for event in events)
            return httpx.Response(200, text=body_sse + "data: [DONE]\n")

        assistant_message = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert assistant_message["reasoning_content"] == "private qwen state"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen3.7-plus",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[],
        stream=True,
        on_reasoning_delta=reasoning_deltas.append,
    )
    assert first.content == ""
    assert reasoning_deltas == ["private ", "qwen state"]
    assert first.provider_metadata is not None
    assert first.provider_metadata["qwen"] == {"reasoning_content": "private qwen state"}
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    second = client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        tools=[],
    )
    assert second.content == "ok"


def test_qwen38_normal_reasoning_never_crosses_credential_routes() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    producer = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="credential-a",
        model="qwen3.8-max",
    )
    consumer = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="credential-b",
        model="qwen3.8-max",
        transport=httpx.MockTransport(handler),
    )
    assistant = {
        "role": "assistant",
        "content": "public answer",
        PROVIDER_METADATA_KEY: stamp_provider_metadata_for_route(
            {"qwen": {"reasoning_content": "private qwen state"}},
            producer.route_identity,
        ),
    }

    consumer.chat(
        messages=[
            {"role": "user", "content": "reason"},
            assistant,
            {"role": "user", "content": "continue"},
        ]
    )

    assistant_wire = captured["messages"][1]
    assert assistant_wire == {"role": "assistant", "content": "public answer"}


def test_provider_metadata_is_stripped_for_non_deepseek_transports() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assistant_message = body["messages"][0]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert "reasoning_content" not in assistant_message
        assert "reasoning" not in assistant_message
        assert "reasoning_details" not in assistant_message
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [],
                "reasoning_content": "must not leak",
                "reasoning": "must not leak",
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "must not leak"}],
                PROVIDER_METADATA_KEY: {
                    "deepseek": {"reasoning_content": "must not leak"},
                    "qwen": {"reasoning_content": "must not leak"},
                    "mistral": {
                        "content_chunks": [
                            {
                                "type": "thinking",
                                "thinking": [{"type": "text", "text": "must not leak"}],
                                "signature": "must not leak",
                            },
                            {"type": "text", "text": "safe answer"},
                        ]
                    },
                },
            }
        ],
        tools=[],
    )
    assert resp.content == "ok"


def test_gemini_tool_call_extra_content_round_trips_for_tool_calls() -> None:
    calls: list[dict[str, object]] = []
    extra_content = {"google": {"thought_signature": "sig-1"}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "extra_content": extra_content,
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": json.dumps({"path": "README.md"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        assistant_message = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert assistant_message["tool_calls"][0]["extra_content"] == extra_content
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test",
        model="gemini-3.1-pro-preview",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "read"}], tools=[])
    assert first.tool_calls[0].provider_metadata == {"gemini": {"extra_content": extra_content}}
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )
    assert PROVIDER_METADATA_KEY in assistant_message
    assert "extra_content" not in assistant_message["tool_calls"][0]

    second = client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_1", "content": '{"content":"x"}'},
        ],
        tools=[],
    )
    assert second.content == "ok"


def test_gemini_tool_call_extra_content_is_stripped_for_other_transports() -> None:
    extra_content = {"google": {"thought_signature": "sig-1"}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assistant_message = body["messages"][0]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert "extra_content" not in assistant_message["tool_calls"][0]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "extra_content": extra_content,
                        "function": {
                            "name": "fs_read",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    }
                ],
                PROVIDER_METADATA_KEY: {
                    "_tool_calls": [
                        {
                            "id": "call_1",
                            "index": 0,
                            "metadata": {"gemini": {"extra_content": extra_content}},
                        }
                    ]
                },
            }
        ],
        tools=[],
    )
    assert resp.content == "ok"


@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("https://api.openai.com/v1", "gpt-5"),
        ("https://example-resource.openai.azure.com/openai/deployments/main", "gpt-5"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.1-pro-preview"),
        ("https://api.mistral.ai/v1", "mistral-large-latest"),
    ],
)
def test_reasoning_effort_is_sent_for_supported_openai_style_providers(
    base_url: str,
    model: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["reasoning_effort"] == "high"
        assert "enable_thinking" not in body
        assert "thinking" not in body
        assert "reasoning" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model=model,
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"


def test_mistral_thinking_chunks_round_trip_opaquely_on_text_turns() -> None:
    calls: list[dict[str, object]] = []
    reasoning_deltas: list[str] = []
    content_chunks = [
        {
            "type": "thinking",
            "thinking": [{"type": "text", "text": "private mistral reasoning"}],
            "signature": "opaque-mistral-signature",
            "closed": True,
        },
        {"type": "text", "text": "Public answer."},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content_chunks}}]},
            )

        assistant_message = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert assistant_message["content"] == content_chunks
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Continued answer."}}]},
        )

    client = OpenAICompatClient(
        base_url="https://api.mistral.ai/v1",
        api_key="test",
        model="mistral-medium-3-5",
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "reason"}],
        tools=[],
        on_reasoning_delta=reasoning_deltas.append,
    )
    assert first.content == "Public answer."
    assert reasoning_deltas == ["private mistral reasoning"]
    assert [item.text for item in first.reasoning] == ["private mistral reasoning"]
    assert first.provider_metadata is not None
    assert first.provider_metadata["mistral"] == {"content_chunks": content_chunks}

    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )
    assert assistant_message["content"] == "Public answer."
    assert PROVIDER_METADATA_KEY in assistant_message

    second = client.chat(
        messages=[
            {"role": "user", "content": "reason"},
            assistant_message,
            {"role": "user", "content": "continue"},
        ],
        tools=[],
    )
    assert second.content == "Continued answer."


def test_mistral_stream_reconstructs_signed_thinking_chunks_for_opaque_replay() -> None:
    calls: list[dict[str, object]] = []
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            events = [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": [
                                    {
                                        "type": "thinking",
                                        "thinking": [{"type": "text", "text": "private "}],
                                        "closed": False,
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "content": [
                                    {
                                        "type": "thinking",
                                        "thinking": [{"type": "text", "text": "state"}],
                                        "signature": "opaque-stream-signature",
                                        "closed": True,
                                    },
                                    {"type": "text", "text": "Public "},
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {"content": "answer."}}]},
            ]
            body_sse = "".join(f"data: {json.dumps(event)}\n" for event in events)
            return httpx.Response(200, text=body_sse + "data: [DONE]\n")

        assistant_message = body["messages"][1]
        assert assistant_message["content"] == [
            {
                "type": "thinking",
                "thinking": [
                    {"type": "text", "text": "private "},
                    {"type": "text", "text": "state"},
                ],
                "closed": True,
                "signature": "opaque-stream-signature",
            },
            {"type": "text", "text": "Public answer."},
        ]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.mistral.ai/v1",
        api_key="test",
        model="mistral-medium-3-5",
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "reason"}],
        tools=[],
        stream=True,
        on_reasoning_delta=reasoning_deltas.append,
    )
    assert first.content == "Public answer."
    assert reasoning_deltas == ["private ", "state"]
    assert first.provider_metadata is not None
    assistant_message = attach_provider_metadata_to_assistant_message(
        {"role": "assistant", "content": first.content},
        first,
    )

    second = client.chat(
        messages=[
            {"role": "user", "content": "reason"},
            assistant_message,
            {"role": "user", "content": "continue"},
        ],
        tools=[],
    )
    assert second.content == "ok"


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gemini-3.7-flash", "minimal", "medium"),
        ("gemini-3.7-flash", "none", "medium"),
        ("gemini-3.7-flash", "medium", "medium"),
        ("gemini-3.1-pro-preview", "minimal", None),
        ("gemini-3.1-pro-preview", "medium", "medium"),
        ("gemini-3.1-pro-preview", "high", "high"),
        ("gemini-3.1-pro-preview", "none", None),
        ("gemini-3-flash-preview", "minimal", "minimal"),
        ("gemini-3-flash-preview", "medium", "medium"),
        ("gemini-2.5-flash", "none", "none"),
        ("gemini-2.5-flash-lite", "none", "none"),
        ("gemini-2.5-pro", "none", None),
        ("gemini-3.1-pro-preview", "xhigh", None),
    ],
)
def test_gemini_reasoning_effort_is_model_safe(
    model: str,
    effort: str,
    expected: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if expected is None:
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == expected
        assert "enable_thinking" not in body
        assert "thinking" not in body
        assert "reasoning" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test",
        model=model,
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"


@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("https://api.x.ai/v1", "grok-4"),
        ("https://example.com/v1", "unknown-reasoning-model"),
    ],
)
def test_reasoning_effort_is_omitted_for_unsupported_transports(
    base_url: str,
    model: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "reasoning_effort" not in body
        assert "enable_thinking" not in body
        assert "thinking" not in body
        assert "reasoning" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model=model,
        enable_thinking=True,
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"


@pytest.mark.parametrize(
    "adapter",
    [
        "deepseek_reasoning",
        "dashscope_thinking",
        "openrouter_reasoning",
        "mistral_thinking",
        "nvidia_reasoning",
    ],
)
def test_explicit_custom_reasoning_adapter_controls_stream_parse_replay_and_route_gate(
    adapter: str,
) -> None:
    sentinel = f"PRIVATE_{adapter.upper()}_STATE"
    reasoning_deltas: list[str] = []
    calls: list[dict[str, object]] = []
    tool_call = {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "fs_read",
            "arguments": json.dumps({"path": "README.md"}),
        },
    }
    if adapter == "mistral_thinking":
        mistral_content = [
            {
                "type": "thinking",
                "thinking": [{"type": "text", "text": sentinel}],
                "signature": f"opaque-{adapter}-signature",
                "closed": True,
            },
            {"type": "text", "text": "Public answer."},
        ]
        delta: dict[str, object] = {
            "content": mistral_content,
            "tool_calls": [tool_call],
        }
        metadata_namespace = "mistral"
    elif adapter == "openrouter_reasoning":
        reasoning_details = [{"type": "reasoning.encrypted", "data": sentinel}]
        delta = {
            "content": "Public answer.",
            "reasoning": sentinel,
            "reasoning_details": reasoning_details,
            "tool_calls": [tool_call],
        }
        metadata_namespace = "openrouter"
    else:
        delta = {
            "content": "Public answer.",
            "reasoning_content": sentinel,
            "tool_calls": [tool_call],
        }
        metadata_namespace = {
            "deepseek_reasoning": "deepseek",
            "dashscope_thinking": "qwen",
            "nvidia_reasoning": "nvidia",
        }[adapter]

    stream_body = "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\ndata: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            reasoning_fields = {
                key
                for key in (
                    "enable_thinking",
                    "thinking",
                    "reasoning",
                    "reasoning_effort",
                    "chat_template_kwargs",
                )
                if key in body
            }
            expected_fields = {
                "deepseek_reasoning": {"thinking"},
                "dashscope_thinking": {"enable_thinking"},
                "openrouter_reasoning": {"reasoning"},
                "mistral_thinking": {"reasoning_effort"},
                "nvidia_reasoning": set(),
            }
            expected_payloads = {
                "deepseek_reasoning": {"thinking": {"type": "enabled"}},
                "dashscope_thinking": {"enable_thinking": True},
                "openrouter_reasoning": {"reasoning": {"effort": "high"}},
                "mistral_thinking": {"reasoning_effort": "high"},
                "nvidia_reasoning": {},
            }
            assert reasoning_fields == expected_fields[adapter]
            assert all(body[key] == value for key, value in expected_payloads[adapter].items())
            return httpx.Response(
                200,
                text=stream_body,
                headers={"content-type": "text/event-stream"},
            )

        assistant_wire = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_wire
        assert sentinel in json.dumps(assistant_wire, sort_keys=True)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://custom.example.test/v1",
        api_key="same-route-secret",
        model="custom-reasoning-model",
        provider_key="custom-provider",
        reasoning_trace_adapter=adapter,
        enable_thinking=True,
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "reason"}],
        tools=[],
        stream=True,
        on_reasoning_delta=reasoning_deltas.append,
    )

    assert first.content == "Public answer."
    assert first.provider_metadata is not None
    assert metadata_namespace in first.provider_metadata
    assert sentinel in json.dumps(first.provider_metadata[metadata_namespace], sort_keys=True)
    assert [item.text for item in first.reasoning] == [sentinel]
    assert reasoning_deltas == [sentinel]
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )
    continuation = [
        {"role": "user", "content": "reason"},
        assistant_message,
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    second = client.chat(messages=continuation, tools=[])
    assert second.content == "ok"

    mismatched_calls: list[dict[str, object]] = []

    def mismatched_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        mismatched_calls.append(body)
        assert sentinel not in json.dumps(body, sort_keys=True)
        assert PROVIDER_METADATA_KEY not in body["messages"][1]
        return httpx.Response(200, json={"choices": [{"message": {"content": "safe"}}]})

    mismatched_client = OpenAICompatClient(
        base_url="https://custom.example.test/v1",
        api_key="different-route-secret",
        model="custom-reasoning-model",
        provider_key="custom-provider",
        reasoning_trace_adapter=adapter,
        enable_thinking=True,
        reasoning_effort="high",
        transport=httpx.MockTransport(mismatched_handler),
    )
    assert mismatched_client.route_identity.fingerprint != client.route_identity.fingerprint
    mismatched = mismatched_client.chat(messages=continuation, tools=[])
    assert mismatched.content == "safe"
    assert len(mismatched_calls) == 1


def test_auto_custom_reasoning_route_is_passive_and_does_not_guess_wire_fields() -> None:
    sentinel = "PRIVATE_UNKNOWN_PROVIDER_REASONING"
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        assert not {
            "enable_thinking",
            "thinking",
            "reasoning",
            "reasoning_effort",
        }.intersection(body)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "Public answer.",
                                "reasoning_content": sentinel,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": '{"path":"README.md"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        assert sentinel not in json.dumps(body, sort_keys=True)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://custom.example.test/v1",
        api_key="test",
        model="custom-reasoning-model",
        provider_key="custom-provider",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(messages=[{"role": "user", "content": "reason"}], tools=[])
    assert first.provider_metadata is not None
    assert sentinel not in json.dumps(first.provider_metadata, sort_keys=True)
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )
    second = client.chat(
        messages=[
            {"role": "user", "content": "reason"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        tools=[],
    )
    assert second.content == "ok"


def test_openrouter_reasoning_round_trips_for_tool_calls() -> None:
    calls: list[dict[str, object]] = []
    reasoning_deltas: list[str] = []
    reasoning_details = [{"type": "reasoning.encrypted", "data": "encrypted-state"}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            assert body["reasoning"] == {"effort": "high"}
            assert "reasoning_effort" not in body
            assert "enable_thinking" not in body
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning": "hidden openrouter reasoning",
                                "reasoning_details": reasoning_details,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": json.dumps({"path": "README.md"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        assistant_message = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert assistant_message["reasoning"] == "hidden openrouter reasoning"
        assert assistant_message["reasoning_details"] == reasoning_details
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="openai/gpt-5",
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[{"role": "user", "content": "read"}],
        tools=[],
        on_reasoning_delta=reasoning_deltas.append,
    )
    assert reasoning_deltas == ["hidden openrouter reasoning"]
    assert first.provider_metadata is not None
    assert first.provider_metadata["openrouter"] == {
        "reasoning": "hidden openrouter reasoning",
        "reasoning_details": reasoning_details,
    }
    assert first.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )
    assert PROVIDER_METADATA_KEY in assistant_message

    second = client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_1", "content": '{"content":"x"}'},
        ],
        tools=[],
    )
    assert second.content == "ok"


def test_http_error_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])


def test_parses_usage_and_response_model() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-5-nano-2026-01-01",
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="gpt-5-nano",
        transport=transport,
    )
    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.response_model == "gpt-5-nano-2026-01-01"
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 12
    assert resp.usage.completion_tokens == 7
    assert resp.usage.total_tokens == 19


def test_parses_cached_prompt_tokens_from_usage_details() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-5-nano-2026-01-01",
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "total_tokens": 24,
                    "prompt_tokens_details": {
                        "cached_tokens": 14,
                    },
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="gpt-5-nano",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.usage is not None
    assert resp.usage.cached_prompt_tokens == 14
    assert resp.usage.cache_read_input_tokens == 14
    assert resp.usage.input_tokens_uncached == 6


def test_omits_prompt_cache_fields_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "prompt_cache_key" not in body
        assert "prompt_cache_retention" not in body
        assert "enable_thinking" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"


def test_includes_prompt_cache_fields_when_configured() -> None:
    reset_provider_telemetry_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["prompt_cache_key"] == "repo-main"
        assert body["prompt_cache_retention"] == "24h"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "openai_prompt_cache",
            "mode": "manual",
            "enabled": True,
            "capability_source": "profile",
            "source": "profile",
            "allowed_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "emitted_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "openai",
        },
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"] == {
        "status": "enabled",
        "strategy": "openai_prompt_cache",
        "mode": "automatic",
        "retention": "24h",
        "enabled": True,
        "capability_source": "profile",
        "source": "profile",
        "allowed_fields": ["prompt_cache_key", "prompt_cache_retention"],
        "emitted_fields": ["prompt_cache_key", "prompt_cache_retention"],
        "trusted_usage_fields": ["cache_read_input_tokens"],
        "usage_schema": "openai",
    }
    assert summary["token_reconciliation"]["input_estimate_tokens"] > 0
    assert summary["token_reconciliation"]["sent_input_estimate_tokens"] > 0
    assert summary["token_reconciliation"]["reported_prompt_tokens"] is None
    assert "repo-main" not in json.dumps(summary, sort_keys=True)


def test_includes_openrouter_session_id_when_configured() -> None:
    reset_provider_telemetry_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body[OPENROUTER_SESSION_ID_FIELD] == "or-session"
        assert "prompt_cache_key" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="qwen/qwen3.7-plus",
        prompt_cache_request_field_values={OPENROUTER_SESSION_ID_FIELD: "or-session"},
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "openrouter_sticky_session",
            "mode": "automatic",
            "enabled": True,
            "allowed_fields": [OPENROUTER_SESSION_ID_FIELD],
            "emitted_fields": [OPENROUTER_SESSION_ID_FIELD],
            "trusted_usage_fields": ["cache_read_input_tokens", "cache_creation_input_tokens"],
            "usage_schema": "provider",
        },
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert resp.content == "ok"
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"]["strategy"] == "openrouter_sticky_session"
    assert summary["cache_policy"]["emitted_fields"] == [OPENROUTER_SESSION_ID_FIELD]
    assert "or-session" not in json.dumps(summary, sort_keys=True)


def test_includes_xai_conversation_id_header_when_configured() -> None:
    reset_provider_telemetry_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert XAI_CONVERSATION_ID_HEADER_FIELD not in body
        assert request.headers[XAI_CONVERSATION_ID_HEADER_FIELD] == "conv-abc"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.x.ai/v1",
        api_key="test",
        model="grok-4.3",
        prompt_cache_request_field_values={XAI_CONVERSATION_ID_HEADER_FIELD: "conv-abc"},
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "xai_conversation_header",
            "mode": "automatic",
            "enabled": True,
            "allowed_fields": [XAI_CONVERSATION_ID_HEADER_FIELD],
            "emitted_fields": [XAI_CONVERSATION_ID_HEADER_FIELD],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "provider",
        },
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert resp.content == "ok"
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"]["strategy"] == "xai_conversation_header"
    assert summary["cache_policy"]["emitted_fields"] == [XAI_CONVERSATION_ID_HEADER_FIELD]
    assert "conv-abc" not in json.dumps(summary, sort_keys=True)


def test_includes_cache_control_block_when_profile_capability_emits_field() -> None:
    reset_provider_telemetry_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        content = body["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert body["messages"][1]["content"] == "current request"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen3-coder-plus",
        prompt_cache_request_field_values={CACHE_CONTROL_FIELD: "ephemeral"},
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "qwen_cache_control_blocks",
            "mode": "automatic",
            "enabled": True,
            "allowed_fields": [CACHE_CONTROL_FIELD],
            "emitted_fields": [CACHE_CONTROL_FIELD],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "provider",
            "min_tokens": 1,
        },
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(
        messages=[
            {"role": "system", "content": "stable prefix"},
            {"role": "user", "content": "current request"},
        ],
        tools=[],
    )

    assert resp.content == "ok"
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"]["used"] is True
    assert summary["cache_policy"]["cacheable_prefix_estimated_tokens"] > 0
    assert summary["request_shape"]["cache_control_block_count"] == 1
    assert summary["request_shape"]["cache_eligible"] is True
    rendered = json.dumps(summary, sort_keys=True)
    assert "stable prefix" not in rendered
    assert "current request" not in rendered


def test_cache_param_rejection_circuit_disables_cache_control_blocks() -> None:
    reset_provider_telemetry_for_tests()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            content = body["messages"][0]["content"]
            assert isinstance(content, list)
            assert content[0]["cache_control"] == {"type": "ephemeral"}
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "messages.0.content.0.cache_control",
                        "message": "cache_control is not supported by this route",
                    }
                },
            )
        rendered = json.dumps(body, sort_keys=True)
        assert "cache_control" not in rendered
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen3-coder-plus",
        prompt_cache_request_field_values={CACHE_CONTROL_FIELD: "ephemeral"},
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "qwen_cache_control_blocks",
            "mode": "automatic",
            "enabled": True,
            "allowed_fields": [CACHE_CONTROL_FIELD],
            "emitted_fields": [CACHE_CONTROL_FIELD],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "provider",
            "min_tokens": 1,
        },
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(
        messages=[
            {"role": "system", "content": "stable prefix"},
            {"role": "user", "content": "current request"},
        ],
        tools=[],
    )
    first_summary = last_provider_call_summary()
    second = client.chat(
        messages=[
            {"role": "system", "content": "stable prefix"},
            {"role": "user", "content": "next request"},
        ],
        tools=[],
    )

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 3
    assert "cache_control" not in json.dumps(requests[1], sort_keys=True)
    assert "cache_control" not in json.dumps(requests[2], sort_keys=True)
    assert first_summary is not None
    assert first_summary["request_shape"]["input_mode"] == "cache_param_fallback"
    assert first_summary["request_shape"]["cache_control_block_count"] == 0
    expected_estimate = estimate_provider_payload_tokens({"messages": requests[1]["messages"]})
    assert first_summary["token_reconciliation"]["input_mode"] == "cache_param_fallback"
    assert first_summary["token_reconciliation"]["input_estimate_tokens"] == expected_estimate
    assert first_summary["token_reconciliation"]["sent_input_estimate_tokens"] == expected_estimate


def test_cache_param_rejection_circuit_disables_openrouter_session_id() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body[OPENROUTER_SESSION_ID_FIELD] == "or-session"
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": OPENROUTER_SESSION_ID_FIELD,
                        "message": "session_id is not supported by this route",
                    }
                },
            )
        assert OPENROUTER_SESSION_ID_FIELD not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="qwen/qwen3.7-plus",
        prompt_cache_request_field_values={OPENROUTER_SESSION_ID_FIELD: "or-session"},
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "openrouter_sticky_session",
            "mode": "automatic",
            "enabled": True,
            "allowed_fields": [OPENROUTER_SESSION_ID_FIELD],
            "emitted_fields": [OPENROUTER_SESSION_ID_FIELD],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "provider",
        },
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    second = client.chat(messages=[{"role": "user", "content": "again"}], tools=[])

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 3
    assert OPENROUTER_SESSION_ID_FIELD not in requests[1]
    assert OPENROUTER_SESSION_ID_FIELD not in requests[2]


def test_cache_param_rejection_circuit_handles_fastapi_detail_list_body() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body[OPENROUTER_SESSION_ID_FIELD] == "or-session"
            return httpx.Response(
                422,
                json={
                    "detail": [
                        {
                            "loc": ["body", "session_id"],
                            "msg": "extra fields not permitted",
                            "type": "value_error.extra",
                        }
                    ]
                },
            )
        assert OPENROUTER_SESSION_ID_FIELD not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="qwen/qwen3.7-plus",
        prompt_cache_request_field_values={OPENROUTER_SESSION_ID_FIELD: "or-session"},
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "openrouter_sticky_session",
            "mode": "automatic",
            "enabled": True,
            "allowed_fields": [OPENROUTER_SESSION_ID_FIELD],
            "emitted_fields": [OPENROUTER_SESSION_ID_FIELD],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "provider",
        },
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    second = client.chat(messages=[{"role": "user", "content": "again"}], tools=[])

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 3
    assert OPENROUTER_SESSION_ID_FIELD not in requests[1]
    assert OPENROUTER_SESSION_ID_FIELD not in requests[2]


def test_cache_param_rejection_circuit_handles_string_error_body() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body["prompt_cache_key"] == "repo-main"
            return httpx.Response(
                400,
                json={"error": "Unsupported parameter: prompt_cache_key"},
            )
        assert "prompt_cache_key" not in body
        assert "prompt_cache_retention" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "openai_prompt_cache",
            "mode": "manual",
            "enabled": True,
            "allowed_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "emitted_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "openai",
        },
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    second = client.chat(messages=[{"role": "user", "content": "again"}], tools=[])

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 3
    assert "prompt_cache_key" not in requests[1]
    assert "prompt_cache_key" not in requests[2]


def test_cache_param_rejection_circuit_disables_xai_header() -> None:
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get(XAI_CONVERSATION_ID_HEADER_FIELD))
        if len(seen_headers) == 1:
            assert seen_headers[-1] == "conv-abc"
            return httpx.Response(
                422,
                json={
                    "error": {
                        "message": "x-grok-conv-id is not supported by this endpoint",
                    }
                },
            )
        assert seen_headers[-1] is None
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.x.ai/v1",
        api_key="test",
        model="grok-4.3",
        prompt_cache_request_field_values={XAI_CONVERSATION_ID_HEADER_FIELD: "conv-abc"},
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "xai_conversation_header",
            "mode": "automatic",
            "enabled": True,
            "allowed_fields": [XAI_CONVERSATION_ID_HEADER_FIELD],
            "emitted_fields": [XAI_CONVERSATION_ID_HEADER_FIELD],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "provider",
        },
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    second = client.chat(messages=[{"role": "user", "content": "again"}], tools=[])

    assert first.content == "ok"
    assert second.content == "ok"
    assert seen_headers == ["conv-abc", None, None]


def test_retries_without_rejected_prompt_cache_retention_only() -> None:
    reset_provider_telemetry_for_tests()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body["prompt_cache_key"] == "repo-main"
            assert body["prompt_cache_retention"] == "24h"
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "prompt_cache_retention",
                        "message": "Unsupported parameter: prompt_cache_retention",
                    }
                },
            )
        assert body["prompt_cache_key"] == "repo-main"
        assert "prompt_cache_retention" not in body
        return httpx.Response(
            200,
            json={
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 5},
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "openai_prompt_cache",
            "mode": "manual",
            "enabled": True,
            "allowed_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "emitted_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "openai",
        },
        transport=httpx.MockTransport(handler),
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert resp.content == "ok"
    assert len(requests) == 2
    assert resp.usage is not None
    assert resp.usage.cache_read_input_tokens == 5
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"]["fallback"] == "stripped_rejected_cache_fields"
    assert summary["cache_policy"]["disabled_fields"] == ["prompt_cache_retention"]
    assert summary["cache_policy"]["emitted_fields"] == ["prompt_cache_key"]
    assert "repo-main" not in json.dumps(summary, sort_keys=True)


def test_cache_param_rejection_circuit_disables_key_and_retention_for_later_calls() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        if len(requests) == 1:
            assert body["prompt_cache_key"] == "repo-main"
            assert body["prompt_cache_retention"] == "24h"
            return httpx.Response(
                422,
                json={
                    "error": {
                        "param": "prompt_cache_key",
                        "message": "prompt_cache_key is not supported by this provider",
                    }
                },
            )
        assert "prompt_cache_key" not in body
        assert "prompt_cache_retention" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_policy_metadata={
            "status": "enabled",
            "strategy": "openai_prompt_cache",
            "mode": "manual",
            "enabled": True,
            "allowed_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "emitted_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "usage_schema": "openai",
        },
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    second = client.chat(messages=[{"role": "user", "content": "again"}], tools=[])

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(requests) == 3
    assert "prompt_cache_key" not in requests[1]
    assert "prompt_cache_retention" not in requests[1]
    assert "prompt_cache_key" not in requests[2]
    assert "prompt_cache_retention" not in requests[2]


def test_includes_enable_thinking_when_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["enable_thinking"] is False
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen3.5-plus",
        enable_thinking=False,
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert resp.content == "ok"


@pytest.mark.parametrize("enable_thinking", [None, True, False])
def test_nvidia_super_uses_flat_reasoning_contract_without_parser_rescue(
    enable_thinking: bool | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "chat_template_kwargs" not in body
        assert "enable_thinking" not in body
        if enable_thinking is False:
            assert body["reasoning_effort"] == "none"
        elif enable_thinking is True:
            assert body["reasoning_effort"] == "high"
        else:
            assert "reasoning_effort" not in body
        assert "reasoning_budget" not in body
        # NVIDIA documents tool use with reasoning; do not inherit another
        # provider's forced-tool restriction without evidence.
        assert body["tool_choice"] == "required"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="test",
        model="nvidia/nemotron-3-super-120b-a12b",
        enable_thinking=enable_thinking,
        reasoning_effort="high" if enable_thinking is True else None,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "use a tool"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="required",
    )
    assert response.content == "ok"


def test_nvidia_super_stream_keeps_structured_reasoning_out_of_tool_call_content() -> None:
    private_reasoning = "PRIVATE_NVIDIA_REASONING"
    public_deltas: list[str] = []
    tool_call = {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"query":"status"}'},
    }
    stream_body = (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": private_reasoning,
                            "content": None,
                            "tool_calls": [tool_call],
                        }
                    }
                ]
            }
        )
        + "\n\ndata: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "chat_template_kwargs" not in body
        return httpx.Response(
            200,
            text=stream_body,
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="test",
        model="nvidia/nemotron-3-super-120b-a12b",
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "use a tool"}],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        stream=True,
        on_text_delta=public_deltas.append,
    )

    assert response.content == ""
    assert public_deltas == []
    assert response.tool_calls[0].name == "lookup"
    assert response.provider_metadata is not None
    assert response.provider_metadata["nvidia"]["reasoning_content"] == private_reasoning


def test_nvidia_nim_omits_thinking_controls_when_unconfigured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert "chat_template_kwargs" not in body
        assert "reasoning_budget" not in body
        assert "reasoning_effort" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="test",
        model="nvidia/nemotron-3-super-120b-a12b",
        transport=httpx.MockTransport(handler),
    )

    assert client.chat(messages=[{"role": "user", "content": "hi"}], tools=[]).content == "ok"


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("nvidia/nemotron-3-super-120b-a12b", "low"),
        ("nvidia/nemotron-3-super-120b-a12b", "high"),
        ("nvidia/nemotron-3-ultra-550b-a55b", "medium"),
        ("nvidia/nemotron-3-ultra-550b-a55b", "high"),
        ("deepseek-ai/deepseek-v4-pro", "high"),
        ("deepseek-ai/deepseek-v4-pro", "max"),
        ("deepseek-ai/deepseek-v4-flash-0731", "high"),
    ],
)
def test_nvidia_hosted_models_emit_only_their_documented_flat_effort(
    model: str,
    effort: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="test",
        model=model,
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["reasoning_effort"] == effort
    assert "chat_template_kwargs" not in captured


def test_nvidia_nano_uses_only_its_documented_nested_toggle() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="test",
        model="nvidia/nemotron-3-nano-30b-a3b",
        enable_thinking=False,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in captured


def test_unknown_nvidia_hosted_model_never_inherits_nemotron_controls() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="test",
        model="third-party/new-model",
        enable_thinking=True,
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )

    client.chat(
        messages=[{"role": "user", "content": "use a tool"}],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
    )

    assert "reasoning_effort" not in captured
    assert "chat_template_kwargs" not in captured


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_zai_coding_plan_emits_only_documented_reasoning_efforts(effort: str) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.z.ai/api/coding/paas/v4",
        api_key="test",
        model="glm-5.3",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["reasoning_effort"] == effort
    assert "thinking" not in captured
    assert "enable_thinking" not in captured


@pytest.mark.parametrize("effort", ["none", "minimal", "medium", "xhigh"])
def test_zai_coding_plan_omits_unsupported_reasoning_efforts(effort: str) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.z.ai/api/coding/paas/v4",
        api_key="test",
        model="glm-5.3",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert "reasoning_effort" not in captured
    assert "thinking" not in captured
    assert "enable_thinking" not in captured


@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
def test_qwen38_emits_only_documented_reasoning_efforts(effort: str) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="test",
        model="qwen3.8-max",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["enable_thinking"] is True
    assert captured["reasoning_effort"] == effort


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_deepseek_v4_emits_only_documented_reasoning_efforts(effort: str) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test",
        model="deepseek-v4-pro",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == effort


@pytest.mark.parametrize("effort", ["high", "max"])
def test_together_deepseek_v4_pro_emits_only_documented_reasoning_efforts(
    effort: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.together.ai/v1",
        api_key="test",
        model="deepseek-ai/DeepSeek-V4-Pro-0813",
        provider_key="together",
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["reasoning_effort"] == effort
    assert "reasoning" not in captured
    assert "thinking" not in captured


def test_together_deepseek_v4_flash_omits_unverified_reasoning_controls() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.together.ai/v1",
        api_key="test",
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        provider_key="together",
        enable_thinking=False,
        reasoning_effort="max",
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert "reasoning_effort" not in captured
    assert "reasoning" not in captured
    assert "thinking" not in captured


def test_together_deepseek_v4_uses_its_documented_reasoning_disable_object() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.together.ai/v1",
        api_key="test",
        model="deepseek-ai/DeepSeek-V4-Pro-0813",
        provider_key="together",
        enable_thinking=False,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in captured
    assert "thinking" not in captured


@pytest.mark.parametrize(
    ("base_url", "model", "effort", "toggle_field"),
    [
        (
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "qwen3.8-max",
            "high",
            "enable_thinking",
        ),
        ("https://api.deepseek.com", "deepseek-v4-pro", "medium", "thinking"),
        ("https://api.deepseek.com", "deepseek-v4-pro", "xhigh", "thinking"),
    ],
)
def test_provider_reasoning_contract_omits_undocumented_effort_values(
    base_url: str,
    model: str,
    effort: str,
    toggle_field: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model=model,
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert toggle_field in captured
    assert "reasoning_effort" not in captured


@pytest.mark.parametrize(
    ("base_url", "provider_key", "model", "effort"),
    [
        ("https://api.groq.com/openai/v1", "groq", "openai/gpt-oss-120b", "high"),
        ("https://api.groq.com/openai/v1", "groq", "qwen/qwen3.6-27b", "default"),
        ("https://api.cerebras.ai/v1", "cerebras", "gpt-oss-120b", "medium"),
        (
            "https://api.cohere.ai/compatibility/v1",
            "cohere",
            "command-a-reasoning-08-2025",
            "high",
        ),
        (
            "https://api.fireworks.ai/inference/v1",
            "fireworks",
            "accounts/fireworks/models/deepseek-v4-pro-0813",
            "max",
        ),
    ],
)
def test_contract_opted_in_providers_emit_exact_flat_reasoning_effort(
    base_url: str,
    provider_key: str,
    model: str,
    effort: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model=model,
        provider_key=provider_key,
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["reasoning_effort"] == effort
    assert "thinking" not in captured
    assert "enable_thinking" not in captured
    assert "reasoning" not in captured


@pytest.mark.parametrize(
    ("base_url", "provider_key", "model"),
    [
        ("https://api.cerebras.ai/v1", "cerebras", "zai-glm-4.7"),
        (
            "https://api.cohere.ai/compatibility/v1",
            "cohere",
            "command-a-reasoning-08-2025",
        ),
        (
            "https://api.fireworks.ai/inference/v1",
            "fireworks",
            "accounts/fireworks/models/deepseek-v4-flash-0731",
        ),
    ],
)
def test_contract_opted_in_providers_emit_documented_explicit_off(
    base_url: str,
    provider_key: str,
    model: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model=model,
        provider_key=provider_key,
        enable_thinking=False,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured["reasoning_effort"] == "none"


@pytest.mark.parametrize(
    ("base_url", "provider_key", "model", "effort"),
    [
        ("https://api.cerebras.ai/v1", "cerebras", "zai-glm-4.7", "high"),
        (
            "https://api.fireworks.ai/inference/v1",
            "fireworks",
            "accounts/fireworks/models/minimax-m3",
            "high",
        ),
        (
            "https://api.fireworks.ai/inference/v1",
            "fireworks",
            "accounts/fireworks/models/future",
            "high",
        ),
    ],
)
def test_unverified_flat_effort_values_are_not_emitted(
    base_url: str,
    provider_key: str,
    model: str,
    effort: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model=model,
        provider_key=provider_key,
        reasoning_effort=effort,
        transport=httpx.MockTransport(handler),
    )

    client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert "reasoning_effort" not in captured


def test_chat_sanitizes_surrogate_escaped_message_content() -> None:
    original = "θελω να φτιαξουμε ενα website με AI news"
    surrogate_text = _surrogate_escaped_text(original)
    assert surrogate_text != original
    assert any(0xD800 <= ord(ch) <= 0xDFFF for ch in surrogate_text)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["messages"][0]["content"] == original
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": surrogate_text}], tools=[])
    assert resp.content == "ok"


def test_stream_parses_content_and_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}

        events = [
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "fs_read", "arguments": '{"path":"REA'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": 'DME.md","max_bytes":10}'}}
                            ]
                        }
                    }
                ]
            },
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == "Hello"
    assert chunks == ["Hel", "lo"]
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "fs_read"
    assert tc.arguments == {"path": "README.md", "max_bytes": 10}


def test_stream_gemini_tool_call_extra_content_round_trips() -> None:
    calls: list[dict[str, object]] = []
    extra_content = {"google": {"thought_signature": "sig-stream"}}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            event = {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "extra_content": extra_content,
                                    "function": {
                                        "name": "fs_read",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
            body_sse = f"data: {json.dumps(event)}\n" + "data: [DONE]\n"
            return httpx.Response(200, content=body_sse.encode("utf-8"))

        assistant_message = body["messages"][1]
        assert PROVIDER_METADATA_KEY not in assistant_message
        assert assistant_message["tool_calls"][0]["extra_content"] == extra_content
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test",
        model="gemini-3.1-pro-preview",
        transport=httpx.MockTransport(handler),
    )

    first = client.chat(messages=[{"role": "user", "content": "read"}], tools=[], stream=True)
    assert first.tool_calls[0].provider_metadata == {"gemini": {"extra_content": extra_content}}
    assistant_message = attach_provider_metadata_to_assistant_message(
        {
            "role": "assistant",
            "content": first.content,
            "tool_calls": [
                {
                    "id": first.tool_calls[0].id,
                    "type": "function",
                    "function": {
                        "name": first.tool_calls[0].name,
                        "arguments": json.dumps(first.tool_calls[0].arguments),
                    },
                }
            ],
        },
        first,
    )

    second = client.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant_message,
            {"role": "tool", "tool_call_id": "call_1", "content": '{"content":"x"}'},
        ],
        tools=[],
    )
    assert second.content == "ok"


def test_stream_normalizes_content_arrays_and_ignores_non_text_parts() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "content": [
                                {"type": "text", "text": "Hel"},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "https://example.com/image.png"},
                                },
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "content": [
                                {"type": "output_text", "text": "lo"},
                            ]
                        }
                    }
                ]
            },
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == "Hello"
    assert chunks == ["Hel", "lo"]


@pytest.fixture
def cumulative_stream_client():
    """Build a client opted in to cumulative stream assembly.

    The de-duplication these tests pin is for a provider that re-sends the
    text already streamed; it is off by default because it corrupts ordinary
    incremental streams, so each test opts in the way such a provider must.
    """

    def build(transport: httpx.BaseTransport) -> OpenAICompatClient:
        return OpenAICompatClient(
            base_url="https://example.com/v1",
            api_key="test",
            model="test-model",
            transport=transport,
            stream_assembly="cumulative",
        )

    return build


def test_stream_dedupes_cumulative_content_and_tool_argument_deltas(
    cumulative_stream_client,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": "I'll help you build an AI news website."}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "content": (
                                "I'll help you build an AI news website. "
                                "Let me start by reviewing the existing web assets."
                            )
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "fs_read",
                                        "arguments": '{"path":"README.md"',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": '{"path":"README.md","max_bytes":10}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = cumulative_stream_client(transport)

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == (
        "I'll help you build an AI news website. Let me start by reviewing the existing web assets."
    )
    assert chunks == [
        "I'll help you build an AI news website.",
        " Let me start by reviewing the existing web assets.",
    ]
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].arguments == {"path": "README.md", "max_bytes": 10}


def test_stream_dedupes_cumulative_content_with_trailing_whitespace_drift(
    cumulative_stream_client,
) -> None:
    first = (
        "Now I understand the repository structure. It's a simple content repository "
        "with Markdown files. Let me "
    )
    second = (
        "Now I understand the repository structure. It's a simple content repository "
        "with Markdown files. Let me\ncreate the AI news website section following the approved plan."
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": first}}]},
            {"choices": [{"delta": {"content": second}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = cumulative_stream_client(transport)

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == (
        "Now I understand the repository structure. It's a simple content repository "
        "with Markdown files. Let me \ncreate the AI news website section following the approved plan."
    )
    assert chunks == [
        first,
        "\ncreate the AI news website section following the approved plan.",
    ]


def test_stream_dedupes_cumulative_content_restarted_after_leading_newline(
    cumulative_stream_client,
) -> None:
    first = "Let me verify the website renders correctly by checking the generated HTML export:"
    second = (
        "\nLet me verify the website renders correctly by checking the generated HTML export:\n"
        "Step 11: Read File"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": first}}]},
            {"choices": [{"delta": {"content": second}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = cumulative_stream_client(transport)

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == first + "\nStep 11: Read File"
    assert chunks == [first, "\nStep 11: Read File"]


def test_stream_dedupes_tail_overlap_followed_by_restart(cumulative_stream_client) -> None:
    first = (
        "Now I understand the repository structure. It's a simple content repository "
        "with Markdown files. Let me"
    )
    second = (
        "Let meNow I understand the repository structure. It's a simple content repository "
        "with Markdown files. Let me\ncreate the AI news website section following the approved plan."
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": first}}]},
            {"choices": [{"delta": {"content": second}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = cumulative_stream_client(transport)

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == (
        "Now I understand the repository structure. It's a simple content repository "
        "with Markdown files. Let me\ncreate the AI news website section following the approved plan."
    )
    assert chunks == [
        first,
        "\ncreate the AI news website section following the approved plan.",
    ]


def test_stream_dedupes_duplicate_full_sentence_inside_cumulative_chunk(
    cumulative_stream_client,
) -> None:
    first = (
        "Both directories are empty. Now I'll create the AI news website structure. Let me create:"
    )
    second = (
        first
        + "\n"
        + first
        + "\n\n1 Sample AI news markdown files\n2 An HTML file to display the news"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": first}}]},
            {"choices": [{"delta": {"content": second}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = cumulative_stream_client(transport)

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == (
        first + "\n\n1 Sample AI news markdown files\n2 An HTML file to display the news"
    )
    assert chunks == [
        first,
        "\n\n1 Sample AI news markdown files\n2 An HTML file to display the news",
    ]


def test_stream_dedupes_exact_duplicate_progress_sentence_from_cumulative_chunk(
    cumulative_stream_client,
) -> None:
    first = "I found the issues! Let me fix both:"
    second = first + "\n" + first

    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": first}}]},
            {"choices": [{"delta": {"content": second}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = cumulative_stream_client(transport)

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == first
    assert chunks == [first]


def test_stream_suppresses_alternate_full_answer_restart(cumulative_stream_client) -> None:
    first = (
        "Yes, I can see the image. It shows the **Recycle Bin** icon with a blue "
        'recycling symbol and the label "Recycle Bin" below it.\n\n'
        "Do you need help with this image, or should we work in the repository?"
    )
    second = (
        'Yes, I can see the image. It shows the "Recycle Bin" icon with the blue '
        "recycling symbol.\n\n"
        "Is there something specific you want me to do with it, or should we work "
        "in the repository?"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"content": first}}]},
            {"choices": [{"delta": {"content": second}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = cumulative_stream_client(transport)

    chunks: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
        on_text_delta=chunks.append,
    )
    assert resp.content == first
    assert chunks == [first]


def test_stream_http_error_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="stream unsupported")

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    with pytest.raises(LLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=[], stream=True)


def test_stream_http_error_handles_unread_stream_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="stream body unread")

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    with pytest.raises(LLMError) as exc:
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=[], stream=True)
    assert "stream body unread" in str(exc.value)
    assert "without having called `read()`" not in str(exc.value)


def test_stream_retries_without_stream_options_when_unsupported() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if len(calls) == 1:
            assert body.get("stream_options") == {"include_usage": True}
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported field: stream_options",
                    }
                },
            )
        assert "stream_options" not in body
        events = [
            {
                "model": "gpt-5-nano",
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
                "choices": [{"delta": {"content": "ok"}}],
            }
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[], stream=True)
    assert resp.content == "ok"
    assert resp.usage is not None
    assert resp.usage.total_tokens == 3
    assert len(calls) == 2


class _TruncatedSseStream(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


class _KeepaliveOnlySseStream(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        while True:
            yield b": keepalive\n\n"


class _StepClock:
    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class _TrackedCompatSseStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], *, fail_on_exhaustion: bool = False) -> None:
        self.chunks = chunks
        self.chunk_count = 0
        self.exhausted = False
        self.fail_on_exhaustion = fail_on_exhaustion

    def __iter__(self):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            self.chunk_count += 1
            yield chunk
        self.exhausted = True
        if self.fail_on_exhaustion:
            raise AssertionError("stream parser failed to stop before transport exhaustion")


def _compat_sse_chunk(event: object) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def test_stream_parser_clears_abort_callback_when_parsing_fails() -> None:
    class _Token:
        is_cancelled = False

        def __init__(self) -> None:
            self.abort_callback = None
            self.clear_calls = 0

        def set_abort_callback(self, callback):  # type: ignore[no-untyped-def]
            self.abort_callback = callback

        def clear_abort_callback(self) -> None:
            self.clear_calls += 1
            self.abort_callback = None

    token = _Token()
    response = httpx.Response(200, content=b": keepalive\n\n")
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
    )

    with pytest.raises(LLMError, match="truncated"):
        client._parse_stream_response(
            response,
            on_text_delta=None,
            provider_key=None,
            cancellation_token=token,
        )

    assert token.abort_callback is None
    assert token.clear_calls == 1


@pytest.mark.parametrize(
    ("chunks", "timeout", "stream_assembly"),
    [
        pytest.param([b"x"] * 20, 2.0, "auto", id="newline-free-trickle"),
        pytest.param(
            [
                b": keepalive\n\n",
                b"data: not-json\n\n",
                b"data: {}\n\n",
                _compat_sse_chunk(
                    {
                        "id": "chatcmpl-stalled",
                        "object": "chat.completion.chunk",
                        "model": "test-model",
                        "choices": [],
                    }
                ),
                _compat_sse_chunk({"choices": [{"delta": {}}]}),
            ]
            * 10,
            10.0,
            "auto",
            id="comments-malformed-and-empty-events",
        ),
        pytest.param(
            [
                _compat_sse_chunk(
                    {
                        "model": "test-model",
                        "choices": [
                            {
                                "delta": {
                                    "content": "same",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "function": {
                                                "name": "lookup",
                                                "arguments": '{"q":"same"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ],
                    }
                )
            ]
            * 50,
            10.0,
            # Re-sent state adds nothing only under cumulative assembly; on an
            # incremental stream each repeat is new text and counts as progress.
            "cumulative",
            id="duplicate-content-and-tool-state",
        ),
    ],
)
def test_stream_watchdog_rejects_nonprogress_traffic(
    chunks: list[bytes],
    timeout: float,
    stream_assembly: str,
) -> None:
    clock = _StepClock()
    stream = _TrackedCompatSseStream(chunks, fail_on_exhaustion=True)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        provider_retry_settings=ProviderRetrySettings(max_retries=0),
        stream_no_progress_timeout_s=timeout,
        stream_progress_clock=clock,
        stream_assembly=stream_assembly,
    )

    with pytest.raises(LLMStreamNoProgressError, match="meaningful payload"):
        client._parse_stream_response(
            httpx.Response(200, stream=stream),
            on_text_delta=None,
            provider_key=None,
        )

    assert not stream.exhausted


def test_stream_parser_reconstructs_split_crlf_and_utf8_chunks() -> None:
    event = json.dumps(
        {
            "model": "test-model",
            "choices": [{"delta": {"content": "café"}}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    utf8_split = event.index("é".encode()) + 1
    response = httpx.Response(
        200,
        stream=_TrackedCompatSseStream(
            [
                b"data: " + event[:utf8_split],
                event[utf8_split:] + b"\r",
                b"\n\r",
                b"\ndata: [DONE]\r",
                b"\n\r",
                b"\n",
            ]
        ),
    )

    parsed = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
    )._parse_stream_response(
        response,
        on_text_delta=None,
        provider_key=None,
    )

    assert parsed.content == "café"


def test_stream_watchdog_checks_cancellation_before_buffered_newline() -> None:
    token = InteractiveCancellationToken()
    token.cancel()
    stream = _TrackedCompatSseStream([b"x"], fail_on_exhaustion=True)
    response = httpx.Response(200, stream=stream)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
    )

    with pytest.raises(KeyboardInterrupt, match="cancelled_by_user"):
        client._parse_stream_response(
            response,
            on_text_delta=None,
            provider_key=None,
            cancellation_token=token,
        )

    assert not stream.exhausted


def test_stream_watchdog_checks_deadline_before_buffered_newline() -> None:
    stream = _TrackedCompatSseStream([b"x"], fail_on_exhaustion=True)
    response = httpx.Response(200, stream=stream)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
    )
    client._stream_deadline_exhausted = lambda: True

    with pytest.raises(DeadlineExhausted, match="run deadline"):
        client._parse_stream_response(
            response,
            on_text_delta=None,
            provider_key=None,
        )

    assert not stream.exhausted


def test_stream_watchdog_resets_for_reasoning_and_tool_progress() -> None:
    clock = _StepClock()
    reasoning_delta = {
        "reasoning_content": "inspect",
        "reasoning_details": [
            {
                "id": "summary_1",
                "type": "reasoning.summary",
                "summary": "checked",
            }
        ],
    }
    events = [
        {"choices": [{"delta": reasoning_delta}]},
        {"choices": [{"delta": reasoning_delta}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "lookup", "arguments": '{"q":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"skills"}'}}]}}
            ]
        },
    ]
    chunks = [
        *(f"data: {json.dumps(event)}\n\n".encode() for event in events),
        b"data: [DONE]\n\n",
    ]

    response = httpx.Response(200, stream=_TrackedCompatSseStream(chunks))
    client = OpenAICompatClient(
        base_url="https://api.deepseek.com/v1",
        api_key="test",
        model="deepseek-chat",
        stream_no_progress_timeout_s=2.5,
        stream_progress_clock=clock,
    )

    parsed = client._parse_stream_response(
        response,
        on_text_delta=None,
        provider_key="deepseek",
    )

    assert parsed.tool_calls[0].name == "lookup"
    assert parsed.tool_calls[0].arguments == {"q": "skills"}
    assert parsed.provider_metadata is not None
    assert parsed.provider_metadata["deepseek"]["reasoning_content"] == "inspectinspect"
    # Native reasoning retains legitimate repeated chunks just like answer text.
    assert [output.text for output in parsed.reasoning] == ["inspectinspect"]
    assert clock.now > client.stream_no_progress_timeout_s


def test_openai_compat_retries_are_bounded_by_default_wall_clock_cap() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=5,
            base_delay_seconds=61.0,
            max_delay_seconds=61.0,
        ),
        provider_sleep_fn=sleeps.append,
        provider_random_fn=lambda: 0.5,
    )

    with pytest.raises(LLMError, match="read timed out"):
        client.chat(messages=[{"role": "user", "content": "hello"}])

    assert client._provider_retry_wall_clock_cap_seconds == 60.0
    assert attempts == 1
    assert sleeps == []


def test_stream_meaningful_progress_watchdog_retries_then_fails_at_cap() -> None:
    attempts = 0
    events: list[dict[str, object]] = []
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += 1.1
        return now

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, stream=_KeepaliveOnlySseStream())

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
        stream_no_progress_timeout_s=2.0,
        stream_progress_clock=clock,
    )
    client._provider_retry_event_observer = events.append

    with pytest.raises(LLMError, match="meaningful payload"):
        client.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            stream=True,
        )

    assert attempts == 2
    assert events[0]["attempt"] == 2
    assert events[0]["reason"] == "provider_no_progress"


def test_stream_meaningful_progress_watchdog_accepts_slow_payloads() -> None:
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += 1.0
        return now

    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            ": keepalive\n\n"
            'data: {"choices":[{"delta":{"content":"o"}}]}\n\n'
            ": keepalive\n\n"
            'data: {"choices":[{"delta":{"content":"k"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode("utf-8"))

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        stream_no_progress_timeout_s=4.5,
        stream_progress_clock=clock,
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        stream=True,
    )
    assert response.content == "ok"


def test_stream_transport_truncation_does_not_replay_after_public_output() -> None:
    attempts = 0
    chunks: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, stream=_TruncatedSseStream())
        body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body.encode("utf-8"))

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
    )

    with pytest.raises(LLMError, match="stream interrupted after partial output"):
        client.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            stream=True,
            on_text_delta=chunks.append,
        )

    assert attempts == 1
    assert chunks == ["partial"]


def test_stream_parses_cached_prompt_tokens() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {
                "model": "gpt-5-nano",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 6},
                },
                "choices": [{"delta": {"content": "ok"}}],
            }
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, content=body_sse.encode("utf-8"))

    transport = httpx.MockTransport(handler)
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=transport,
    )

    resp = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[], stream=True)
    assert resp.usage is not None
    assert resp.usage.cached_prompt_tokens == 6


def _content_handler(message: dict[str, object]):  # type: ignore[no-untyped-def]
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": message}]})

    return handler


def _trial_client(handler) -> OpenAICompatClient:  # type: ignore[no-untyped-def]
    return OpenAICompatClient(
        base_url=_ALYSIS_TRIAL_BASE_URL,
        api_key="test",
        model="mimo-v2.5-pro",
        provider_key="alysis",
        transport=httpx.MockTransport(handler),
    )


def test_non_stream_never_folds_raw_reasoning_into_empty_content() -> None:
    handler = _content_handler({"content": "", "reasoning": "Hi! How can I help?"})
    resp = _trial_client(handler).chat(messages=[{"role": "user", "content": "hi"}])
    assert resp.content == ""


def test_non_stream_never_folds_raw_reasoning_content_into_whitespace_content() -> None:
    handler = _content_handler({"content": "   ", "reasoning_content": "Hello there."})
    resp = _trial_client(handler).chat(messages=[{"role": "user", "content": "hi"}])
    assert resp.content == "   "


def test_non_stream_keeps_empty_when_no_reasoning() -> None:
    # Genuinely empty completion with no reasoning must stay empty so the
    # legitimate clarification fallback still triggers upstream.
    handler = _content_handler({"content": ""})
    resp = _trial_client(handler).chat(messages=[{"role": "user", "content": "hi"}])
    assert resp.content == ""


def test_non_stream_does_not_overwrite_real_content_with_reasoning() -> None:
    handler = _content_handler({"content": "real answer", "reasoning": "scratch work"})
    resp = _trial_client(handler).chat(messages=[{"role": "user", "content": "hi"}])
    assert resp.content == "real answer"


def test_non_stream_empty_content_with_tool_calls_is_not_folded() -> None:
    # Tool-call turns legitimately carry empty content; the fold must not run.
    message = {
        "content": "",
        "reasoning": "deciding to call a tool",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "fs_read", "arguments": json.dumps({"path": "README.md"})},
            }
        ],
    }
    resp = _trial_client(_content_handler(message)).chat(
        messages=[{"role": "user", "content": "hi"}], tools=[]
    )
    assert resp.content == ""
    assert len(resp.tool_calls) == 1


def test_stream_never_folds_raw_reasoning_into_empty_content() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"reasoning": "Hi! "}}]},
            {"choices": [{"delta": {"reasoning": "How can I help?"}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, text=body_sse)

    resp = _trial_client(handler).chat(messages=[{"role": "user", "content": "hi"}], stream=True)
    assert resp.content == ""


def test_stream_keeps_content_over_reasoning() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        events = [
            {"choices": [{"delta": {"reasoning": "thinking"}}]},
            {"choices": [{"delta": {"content": "real "}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
        ]
        body_sse = "".join(f"data: {json.dumps(e)}\n" for e in events) + "data: [DONE]\n"
        return httpx.Response(200, text=body_sse)

    resp = _trial_client(handler).chat(messages=[{"role": "user", "content": "hi"}], stream=True)
    assert resp.content == "real answer"


def test_alysis_hosted_proxy_resolves_to_deepseek_provider_key() -> None:
    # The proxy forwarded to OpenRouter only during the retired MiMo trial; it
    # now forwards to DeepSeek.
    assert _provider_key_from_base_url(_ALYSIS_TRIAL_BASE_URL) == "deepseek"
    # A plain supabase host without the LLM proxy path must NOT be captured.
    assert _provider_key_from_base_url("https://other.example.invalid/rest/v1") != "deepseek"


def test_nvidia_hosted_nim_endpoint_resolves_exactly() -> None:
    assert _provider_key_from_base_url("https://integrate.api.nvidia.com/v1") == "nvidia"
    assert _provider_key_from_base_url("https://build.nvidia.com/v1") != "nvidia"


def test_kimi_membership_endpoint_resolves_separately_from_moonshot_platform() -> None:
    assert _provider_key_from_base_url("https://api.kimi.com/coding/v1") == "kimi-code"
    assert _provider_key_from_base_url("https://api.moonshot.ai/v1") == "moonshot"


def test_zai_coding_plan_endpoint_resolves_without_capturing_general_api() -> None:
    assert _provider_key_from_base_url("https://api.z.ai/api/coding/paas/v4") == "zai_coding_plan"
    assert _provider_key_from_base_url("https://api.z.ai/api/paas/v4") != "zai_coding_plan"
    assert _provider_key_from_base_url("https://api.z.ai/api/coding/paas/v40") != (
        "zai_coding_plan"
    )


def test_hosted_proxy_captures_reasoning_into_deepseek_provider_metadata() -> None:
    # The hosted proxy is classified as deepseek (its upstream), so a
    # reasoning_content field on a normal (non-empty content) completion is
    # preserved under the deepseek provider_metadata key.
    handler = _content_handler({"content": "ok", "reasoning_content": "hidden reasoning"})
    resp = _trial_client(handler).chat(messages=[{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert resp.provider_metadata is not None
    assert resp.provider_metadata["deepseek"] == {"reasoning_content": "hidden reasoning"}
    assert resp.provider_metadata["openai_compat"]["request_plan"]["input_mode"] == "full"


@pytest.mark.parametrize(
    ("provider_key", "base_url", "provider_state"),
    [
        (
            "deepseek",
            "https://api.deepseek.com/v1",
            {"deepseek": {"reasoning_content": "private-deepseek-state"}},
        ),
        (
            "qwen",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            {"qwen": {"reasoning_content": "private-qwen-state"}},
        ),
        (
            "openrouter",
            "https://openrouter.ai/api/v1",
            {
                "openrouter": {
                    "reasoning": "private-openrouter-state",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": "opaque-openrouter-state"}
                    ],
                }
            },
        ),
        (
            "mistral",
            "https://api.mistral.ai/v1",
            {
                "mistral": {
                    "content_chunks": [
                        {
                            "type": "thinking",
                            "thinking": "private-mistral-state",
                            "signature": "opaque-mistral-signature",
                        },
                        {"type": "text", "text": "public answer"},
                    ]
                }
            },
        ),
    ],
)
def test_openai_compat_never_replays_state_from_a_different_credential_route(
    provider_key: str,
    base_url: str,
    provider_state: dict[str, object],
) -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    producer = OpenAICompatClient(
        base_url=base_url,
        api_key="credential-a",
        model="route-model",
        provider_key=provider_key,
    )
    consumer = OpenAICompatClient(
        base_url=base_url,
        api_key="credential-b",
        model="route-model",
        provider_key=provider_key,
        transport=httpx.MockTransport(handler),
    )
    assistant = {
        "role": "assistant",
        "content": "public answer",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "fs_read", "arguments": "{}"},
            }
        ],
        PROVIDER_METADATA_KEY: stamp_provider_metadata_for_route(
            provider_state,
            producer.route_identity,
        ),
    }

    consumer.chat(
        messages=[
            {"role": "user", "content": "read"},
            assistant,
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ]
    )

    serialized = json.dumps(sent[0]["messages"])
    assert "private-" not in serialized
    assert "opaque-" not in serialized
    assert sent[0]["messages"][1]["content"] == "public answer"


def test_openai_compat_count_gates_foreign_route_before_transport_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = OpenAICompatClient(
        base_url="https://api.deepseek.com/v1",
        api_key="credential-a",
        model="route-model",
        provider_key="deepseek",
    )
    consumer = OpenAICompatClient(
        base_url="https://api.deepseek.com/v1",
        api_key="credential-b",
        model="route-model",
        provider_key="deepseek",
    )
    projected_inputs: list[list[dict[str, object]]] = []
    original = openai_compat_mod._messages_for_transport

    def capture_projection(
        messages: list[dict[str, object]],
        *,
        provider_key: str | None,
        reasoning_provider_key: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, object]]:
        projected_inputs.append(messages)
        return original(
            messages,
            provider_key=provider_key,
            reasoning_provider_key=reasoning_provider_key,
            model=model,
        )

    monkeypatch.setattr(openai_compat_mod, "_messages_for_transport", capture_projection)
    consumer.count_input_tokens(
        messages=[
            {
                "role": "assistant",
                "content": "public answer",
                PROVIDER_METADATA_KEY: stamp_provider_metadata_for_route(
                    {"deepseek": {"reasoning_content": "private-count-state"}},
                    producer.route_identity,
                ),
            }
        ]
    )

    assert projected_inputs == [[{"role": "assistant", "content": "public answer"}]]


def test_openai_compat_extra_headers_use_same_canonical_form_on_wire_and_route() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        assert request.headers.get_list("authorization") == ["Bearer override"]
        assert request.headers.get_list("content-type") == ["application/custom+json"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://gateway.example/v1",
        api_key="credential",
        model="route-model",
        extra_headers={
            " X-Tenant-ID ": "  tenant-a  ",
            "Authorization": "Bearer override",
            "Content-Type": "application/custom+json",
        },
        transport=httpx.MockTransport(handler),
    )
    canonical = OpenAICompatClient(
        base_url="https://gateway.example/v1",
        api_key="credential",
        model="route-model",
        extra_headers={
            "x-tenant-id": "tenant-a",
            "authorization": "Bearer override",
            "content-type": "application/custom+json",
        },
    )

    client.chat(messages=[{"role": "user", "content": "hello"}])

    assert client.extra_headers == {
        "x-tenant-id": "tenant-a",
        "authorization": "Bearer override",
        "content-type": "application/custom+json",
    }
    assert captured["x-tenant-id"] == "tenant-a"
    assert captured["authorization"] == "Bearer override"
    assert captured["content-type"] == "application/custom+json"
    assert client.route_identity.fingerprint == canonical.route_identity.fingerprint

    with pytest.raises(ValueError, match="Duplicate extra header name"):
        OpenAICompatClient(
            base_url="https://gateway.example/v1",
            api_key="credential",
            model="route-model",
            extra_headers={"X-Tenant-ID": "tenant-a", "x-tenant-id": "tenant-b"},
        )


def test_required_computer_use_tools_never_retry_as_text_only() -> None:
    from alysis_code.agent.computer_use_tools import COMPUTER_USE_TOOL_NAMES
    from alysis_code.llm.base import require_tool_calling

    tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in COMPUTER_USE_TOOL_NAMES
    ]
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            400,
            json={"error": {"message": "tools are not supported by this model"}},
        )

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="text-only-model",
        transport=httpx.MockTransport(handler),
    )
    require_tool_calling(client, COMPUTER_USE_TOOL_NAMES)

    with pytest.raises(LLMError, match="rejected the required Computer Use tools"):
        client.chat(messages=[{"role": "user", "content": "use the app"}], tools=tools)

    assert len(requests) == 1
    assert "tools" in requests[0]
    assert client.supports_tool_calling is False
    with pytest.raises(LLMError, match="does not support the required Computer Use tools"):
        effective_tools_for_client(client, tools)
