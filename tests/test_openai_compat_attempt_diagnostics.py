from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from alysis_code.llm import openai_compat as compat
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.provider_telemetry import (
    ProviderCallTelemetryRecorder,
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)


def _client(handler=None, **kwargs):
    reset_provider_telemetry_for_tests()
    return compat.OpenAICompatClient(
        base_url=kwargs.pop("base_url", "https://example.invalid/v1"),
        api_key="test-not-a-real-key",
        model="test-model",
        transport=httpx.MockTransport(handler) if handler else None,
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _: None,
        provider_random_fn=lambda: 0.5,
        **kwargs,
    )


def _chat(client, **kwargs):
    return client.chat(messages=[{"role": "user", "content": "test"}], stream=True, **kwargs)


def _summary():
    summary = last_provider_call_summary()
    assert summary is not None
    return summary


@pytest.mark.parametrize("bad_event", [b"data: not-json\n\n", b"data: []\n\n"])
def test_malformed_complete_event_cannot_be_accepted_or_retried(bad_event):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=bad_event + b"data: [DONE]\n\n")

    with pytest.raises(LLMError, match="malformed JSON"):
        _chat(_client(handler))
    assert len(calls) == 1
    summary = _summary()
    assert summary["status_category"] == "invalid_response"
    assert summary["attempts"][0]["terminal_reason"] == "malformed_event"
    assert summary["usage"]["total_tokens"] is None


def test_multiline_and_legacy_events_keep_unicode_and_tool_arguments():
    body = (
        'data: {\r\ndata: "choices": [{"delta": {"content": "Γειά 世界",\r\n'
        'data: "tool_calls": [{"index": 0, "id": "call-1", "function": '
        '{"name": "lookup", "arguments": "{}"}}]}}]}\r\n\r\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n'
        'data: {"usage":{"prompt_tokens":4,"completion_tokens":3,"total_tokens":7}}\n'
        "data: [DONE]\n"
    ).encode()
    response = _chat(_client(lambda _: httpx.Response(200, content=body)))
    assert response.content == "Γειά 世界"
    assert response.tool_calls[0].arguments == {}
    assert response.usage.total_tokens == 7
    attempt = _summary()["attempts"][0]
    assert attempt["body_bytes_received"] == len(body)
    assert attempt["event_count"] == 3
    assert attempt["text_delta_count"] == attempt["tool_delta_count"] == 1
    assert attempt["done_received"] and attempt["usage_observed"]
    assert attempt["finish_reason_observed"]
    assert attempt["first_byte_ms"] <= attempt["first_event_ms"] <= attempt["latency_ms"]


@pytest.mark.parametrize("first_body", [b"", b": ping\n\n", b'data: {"choices":'])
def test_clean_eof_attempt_evidence_survives_retry_without_inventing_usage(first_body):
    calls = []

    def handler(request):
        calls.append(request)
        body = (
            first_body
            if len(calls) == 1
            else b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, content=body)

    assert _chat(_client(handler)).content == "ok"
    summary = _summary()
    assert len(calls) == summary["attempt_count"] == 2
    first, second = summary["attempts"]
    assert first["terminal_reason"] == "stream_truncated"
    assert first["body_bytes_received"] == len(first_body)
    assert first["event_count"] == 0
    assert not first["done_received"] and not first["usage_observed"]
    assert second["terminal_reason"] == "done"
    assert summary["usage"]["total_tokens"] is None


def test_http_compatibility_retry_has_distinct_attempt_and_status():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "stream_options unsupported"}})
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    _chat(_client(handler))
    attempts = _summary()["attempts"]
    assert [a["status_code"] for a in attempts] == [400, 200]
    assert [a["terminal_reason"] for a in attempts] == ["http_error", "done"]


def test_callback_bug_is_not_retried_as_a_network_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"secret-text"}}]}\n\ndata: [DONE]\n\n',
        )

    def callback(_text):
        raise ValueError("connection timed out PRIVATE_CALLBACK_SECRET")

    callback.stream_restart = lambda: None
    with pytest.raises(LLMError, match="callback failed"):
        _chat(_client(handler), on_text_delta=callback)
    summary = _summary()
    assert len(calls) == 1
    assert summary["status_category"] == "client_error"
    assert summary["attempts"][0]["terminal_reason"] == "callback_error"
    assert "PRIVATE_CALLBACK_SECRET" not in json.dumps(summary)
    assert "secret-text" not in json.dumps(summary)


def test_unexpected_parser_bug_is_not_retried_as_a_transport_error(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    def broken(*_args, **_kwargs):
        raise ValueError("connection timed out PRIVATE_PARSER_SECRET")

    monkeypatch.setattr(compat, "_parse_stream_tool_calls", broken)
    with pytest.raises(LLMError, match="processing failed in the client"):
        _chat(_client(handler))
    summary = _summary()
    assert len(calls) == 1
    assert summary["attempts"][0]["terminal_reason"] == "client_error"
    assert "PRIVATE_PARSER_SECRET" not in json.dumps(summary)


def test_attempt_record_projection_is_bounded_and_discards_untrusted_values():
    recorder = ProviderCallTelemetryRecorder(
        provider_key=None,
        protocol="openai_compat",
        model="test",
        base_url="https://example.invalid",
        stream=True,
        tools=None,
    )
    for _ in range(66):
        recorder.record_attempt(
            {"terminal_reason": "PRIVATE", "body_bytes_received": "PRIVATE", "raw": "PRIVATE"}
        )
    recorder.record_error(LLMError("failed"))
    summary = _summary()
    assert summary["attempt_count"] == 66
    assert len(summary["attempts"]) == 64
    assert summary["attempts_omitted"] == 2
    assert summary["attempts"][0]["terminal_reason"] == "unknown"
    assert summary["attempts"][0]["body_bytes_received"] is None
    assert "PRIVATE" not in json.dumps(summary)


def test_local_http_sse_eof_and_retry_have_separate_numeric_diagnostics():
    """Exercise real httpx/socket framing on loopback; no external provider."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            body = (
                b": keepalive\n\n"
                if len(requests) == 1
                else b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = _client(base_url=f"http://127.0.0.1:{server.server_port}/v1")
        assert _chat(client).content == "ok"
        attempts = _summary()["attempts"]
        assert len(requests) == len(attempts) == 2
        assert attempts[0]["event_count"] == 0
        assert attempts[0]["body_bytes_received"] == len(b": keepalive\n\n")
        assert attempts[0]["terminal_reason"] == "stream_truncated"
        assert attempts[1]["terminal_reason"] == "done"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_retry_usage_is_counted_once_per_attempt_and_not_merged_into_logical_response():
    calls = []

    def handler(request):
        calls.append(request)
        usage = {
            "prompt_tokens": 40,
            "completion_tokens": 3,
            "total_tokens": 43,
            "prompt_tokens_details": {"cached_tokens": 10},
            "private": "HIDDEN",
        }
        if len(calls) == 2:
            usage = {
                "prompt_tokens": 7,
                "completion_tokens": 4,
                "total_tokens": 11,
                "prompt_tokens_details": {"cached_tokens": 2},
            }
        body = f"data: {json.dumps({'usage': usage})}\n\n".encode()
        # Repeat a cumulative usage frame: this must not double the attempt cost.
        return httpx.Response(
            200, content=body + body + (b"data: [DONE]\n\n" if len(calls) == 2 else b"")
        )

    response = _chat(_client(handler))
    summary = _summary()
    assert response.usage.prompt_tokens == summary["usage"]["prompt_tokens"] == 7
    assert summary["attempt_usage_totals"]["prompt_tokens"] == 47
    assert summary["attempt_usage_totals"]["completion_tokens"] == 7
    assert summary["attempt_usage_totals"]["cached_prompt_tokens"] == 12
    assert summary["attempt_usage_totals"]["input_tokens_uncached"] == 35
    assert summary["attempt_usage_missing_count"] == 0
    assert summary["attempt_usage_missing_counts"]["prompt_tokens"] == 0
    assert summary["attempt_usage_missing_counts"]["cache_creation_input_tokens"] == 2
    assert "HIDDEN" not in json.dumps(summary)


def test_known_attempt_usage_survives_detail_limit_and_missing_fields_stay_unknown():
    from alysis_code.llm.types import LLMUsage

    recorder = ProviderCallTelemetryRecorder(
        provider_key=None,
        protocol="openai_compat",
        model="test",
        base_url="https://example.invalid",
        stream=True,
        tools=None,
    )
    for _ in range(65):
        recorder.record_attempt(
            {"usage": LLMUsage(prompt_tokens=4, completion_tokens=None, total_tokens=None)}
        )
    recorder.record_error(LLMError("failed"))
    summary = _summary()
    assert summary["attempt_usage_totals"]["prompt_tokens"] == 260
    assert summary["attempt_usage_totals"]["completion_tokens"] is None
    assert summary["attempt_usage_totals"]["cached_prompt_tokens"] is None
    assert summary["attempt_usage_missing_counts"]["prompt_tokens"] == 0
    assert summary["attempt_usage_missing_counts"]["completion_tokens"] == 65
    assert summary["attempt_usage_missing_count"] == 65
    assert summary["attempts_omitted"] == 1


def test_cancelled_attempt_keeps_usage_observed_before_keyboard_interrupt():
    class CancelledStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
            raise KeyboardInterrupt()

    client = _client(lambda _: httpx.Response(200, stream=CancelledStream()))
    with pytest.raises(KeyboardInterrupt):
        _chat(client)
    summary = _summary()
    assert summary["attempt_count"] == 1
    assert summary["attempts"][0]["terminal_reason"] == "cancelled"
    assert summary["attempt_usage_totals"]["prompt_tokens"] == 5
    assert summary["usage"]["prompt_tokens"] is None


def test_stream_error_event_cannot_be_delivered_as_success():
    client = _client(
        lambda _: httpx.Response(
            200, content=b'data: {"error":{"message":"PRIVATE ERROR"}}\n\ndata: [DONE]\n\n'
        )
    )
    with pytest.raises(LLMError, match="error event"):
        _chat(client)
    summary = _summary()
    assert summary["attempt_count"] == 1
    assert summary["attempts"][0]["terminal_reason"] == "provider_error"
    assert "PRIVATE ERROR" not in json.dumps(summary)


def test_repeated_stream_options_rejection_cannot_loop_after_field_was_removed():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "compatibility fallback repeated without changing its request"
        return httpx.Response(400, json={"error": {"message": "stream_options unsupported"}})

    with pytest.raises(LLMError, match="stream_options"):
        _chat(_client(handler))
    assert len(requests) == 2
    assert "stream_options" in requests[0]
    assert "stream_options" not in requests[1]
    assert _summary()["attempt_count"] == 2


def test_wire_diagnostics_compare_actual_final_messages_across_calls():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    client = _client(handler)
    messages = [{"role": "user", "content": "PRIVATE_USER_CONTENT"}]
    client.chat(messages=messages, stream=True)
    first = _summary()["request_plan"]
    messages += [{"role": "assistant", "content": "previous"}, {"role": "user", "content": "next"}]
    client.chat(messages=messages, stream=True)
    second = _summary()["request_plan"]
    assert "wire_previous_history_items" not in first
    assert second["wire_history_items"] == len(requests[-1]["messages"])
    assert second["wire_previous_history_items"] == len(requests[0]["messages"])
    assert second["wire_previous_history_prefix_preserved"]
    assert second["wire_previous_tools_unchanged"]
    assert second["wire_history_sha256"] != first["wire_history_sha256"]
    assert "PRIVATE_USER_CONTENT" not in json.dumps(second)
