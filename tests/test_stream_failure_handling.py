"""What a streamed reply that fails part-way leaves behind.

An error event after HTTP 200, a stream that ends without ``[DONE]``, a secret
inside a chunk, and a run deadline that passes while a stream is open. In each
case the partial reply must not become a response, and what is logged must be
classified and redacted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from alysis_code.agent_loop import create_session
from alysis_code.budget_policy import STOP_REASON_RUN_BUDGET_EXHAUSTED
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.logging_redaction import reset_default_redactor
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)
from alysis_code.session_store import read_session_events

_MIMO = "https://token-plan-ams.xiaomimimo.com/v1"


def _event(delta: dict) -> bytes:
    return f"data: {json.dumps({'choices': [{'index': 0, 'delta': delta}]})}\n\n".encode()


_DONE = b"data: [DONE]\n\n"
_FULL_REPLY = _event({"content": "complete "}) + _event({"content": "answer"}) + _DONE


def _client(bodies: list[bytes], requests: list[httpx.Request], **kwargs) -> OpenAICompatClient:
    reset_provider_telemetry_for_tests()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = bodies[min(len(requests), len(bodies)) - 1]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    return OpenAICompatClient(
        base_url=_MIMO,
        api_key="test",
        model="mimo-v2.5",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
        **kwargs,
    )


def _chat(client: OpenAICompatClient):
    return client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)


def _attempt_reasons() -> list[str]:
    summary = last_provider_call_summary()
    assert summary is not None
    return [attempt["terminal_reason"] for attempt in summary["attempts"]]


# --- an error event after HTTP 200 ---------------------------------------------


@pytest.mark.parametrize(
    ("error", "retry_reason"),
    [
        pytest.param({"code": 429, "message": "Too many requests"}, "provider_throttled", id="429"),
        pytest.param({"code": "503", "message": "busy"}, "provider_unavailable", id="503-string"),
        pytest.param({"status": 500, "message": "boom"}, "provider_unavailable", id="status-500"),
        pytest.param(
            {"type": "server_error", "message": "boom"}, "provider_unavailable", id="server_error"
        ),
        pytest.param(
            {"code": "rate_limit_exceeded", "message": "slow down"},
            "provider_throttled",
            id="rate_limit_exceeded",
        ),
    ],
)
def test_a_retryable_error_event_is_retried_and_its_partial_reply_discarded(
    error: dict, retry_reason: str
) -> None:
    requests: list[httpx.Request] = []
    failing = _event({"content": "PARTIAL "}) + f"data: {json.dumps({'error': error})}\n\n".encode()
    client = _client([failing, _FULL_REPLY], requests)

    response = _chat(client)

    assert response.content == "complete answer"
    assert len(requests) == 2
    assert _attempt_reasons() == ["provider_error", "done"]
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["retry_reasons"] == [retry_reason]


@pytest.mark.parametrize(
    ("error", "match"),
    [
        pytest.param({"code": 401, "message": "invalid api key"}, "LLM error 401", id="401"),
        pytest.param(
            {"code": "invalid_request_error", "message": "context_length_exceeded"},
            "error event",
            id="permanent-string-code",
        ),
        pytest.param({"message": "no code at all"}, "error event", id="no-code"),
    ],
)
def test_a_permanent_error_event_fails_the_call_without_a_retry(error: dict, match: str) -> None:
    requests: list[httpx.Request] = []
    failing = _event({"content": "PARTIAL "}) + f"data: {json.dumps({'error': error})}\n\n".encode()
    client = _client([failing, _FULL_REPLY], requests)

    with pytest.raises(LLMError, match=match):
        _chat(client)

    assert len(requests) == 1
    assert _attempt_reasons() == ["provider_error"]


# --- a stream that ends without [DONE] ------------------------------------------


def test_a_stream_without_done_is_stream_truncated_retried_and_discarded() -> None:
    requests: list[httpx.Request] = []
    truncated = _event({"content": "PARTIAL "}) + _event({"content": "reply"})
    client = _client([truncated, _FULL_REPLY], requests)

    response = _chat(client)

    assert response.content == "complete answer"
    assert len(requests) == 2
    assert _attempt_reasons() == ["stream_truncated", "done"]
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["retry_reasons"] == ["provider_stream_truncated"]


def test_a_stream_that_never_finishes_fails_as_truncated_after_its_retry() -> None:
    requests: list[httpx.Request] = []
    truncated = _event({"content": "PARTIAL"})
    client = _client([truncated, truncated], requests)

    with pytest.raises(LLMError, match="stream truncated"):
        _chat(client)

    assert len(requests) == 2
    assert _attempt_reasons() == ["stream_truncated", "stream_truncated"]


# --- a secret inside a streamed chunk --------------------------------------------

_PLANTED_KEY = "sk-plantedStream7Qm4Zr9Xw2Lp8Kd3Vn6Ht1Bc5"


def _session(tmp_path: Path, **kwargs):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return create_session(
        cfg=AppConfig(model="mimo-v2.5", base_url=_MIMO, routing_mode="code_only", stream=True),
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=True,
        verification_enabled=False,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
        crash_diagnostic_log_path=tmp_path / "crash" / "diagnostics.jsonl",
        **kwargs,
    )


def _streaming_client(stream: httpx.SyncByteStream | bytes) -> OpenAICompatClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(stream, bytes):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=stream
            )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    return OpenAICompatClient(
        base_url=_MIMO,
        api_key="test-key",
        model="mimo-v2.5",
        transport=httpx.MockTransport(handler),
        provider_sleep_fn=lambda _seconds: None,
    )


def test_a_key_inside_a_streamed_chunk_never_reaches_a_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", _PLANTED_KEY)
    reset_default_redactor()
    caplog.set_level(logging.DEBUG)
    reply = (
        _event({"reasoning_content": f"The environment shows {_PLANTED_KEY} here."})
        + _event({"content": "Done. The key I found is "})
        + _event({"content": _PLANTED_KEY})
        + _event({"content": "."})
        + _DONE
    )
    session = _session(tmp_path)
    session.client = _streaming_client(reply)  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Report the key you can see.")
        log_path = session.store.path
    finally:
        session.close()
        reset_default_redactor()

    written = {
        path: path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert exit_code == 0
    assert log_path in written
    assert "redacted:" in written[log_path], "the reply must reach the session log, redacted"
    leaked = [str(path) for path, text in written.items() if _PLANTED_KEY in text]
    assert leaked == []
    assert _PLANTED_KEY not in caplog.text


# --- a run deadline that passes while the stream is open -------------------------


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _DeadlinePassesMidStream(httpx.SyncByteStream):
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def __iter__(self):
        yield _event({"reasoning_content": "Thinking about it."})
        yield _event({"content": "PARTIAL-ANSWER-TEXT "})
        self._clock.now += 10_000.0  # the run budget runs out while the stream is open
        yield _event({"content": "that never arrives"})
        yield _DONE


def test_a_run_deadline_firing_mid_stream_stops_cleanly_without_a_partial_turn(
    tmp_path: Path,
) -> None:
    reset_provider_telemetry_for_tests()
    clock = _Clock(100.0)
    deadline = ExecutionDeadline.from_duration(600.0, clock=clock, source="explicit_cli")
    session = _session(tmp_path, execution_deadline=deadline)
    session.client = _streaming_client(_DeadlinePassesMidStream(clock))  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Answer the question.")
        log_path = session.store.path
        transcript = json.dumps(session.messages)
        stop_reason = session.stop_reason
    finally:
        session.close()

    events = list(read_session_events(log_path))
    assert exit_code == 0
    assert stop_reason == STOP_REASON_RUN_BUDGET_EXHAUSTED
    assert [e for e in events if e.get("type") == "deadline_exhausted"]
    assert _attempt_reasons() == ["budget_abort"]
    assert "PARTIAL-ANSWER-TEXT" not in transcript
    finals = [e["payload"] for e in events if e.get("type") == "final"]
    assert finals, "a budget stop still reports a final summary"
    assert all("PARTIAL-ANSWER-TEXT" not in json.dumps(final) for final in finals)
