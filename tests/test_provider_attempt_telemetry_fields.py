"""Per-attempt provider telemetry: when an attempt started and how it ended.

Attempt records could not answer the basic questions about a failed attempt:
when it was sent (only monotonic offsets existed), which retry it was, whether
its token counts came from the provider, and whether a timeout meant the
provider never answered or stopped answering. A run-budget stop was recorded
as a generic "deadline" and a watchdog stall as "no_progress".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from alysis_code.execution_deadline import (
    DeadlineExhausted,
    ExecutionDeadline,
    temporarily_clamp_client_timeout,
)
from alysis_code.llm import openai_compat as compat
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)

_ISO_UTC_MS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_OK_STREAM = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'


def _client(handler, **kwargs):
    reset_provider_telemetry_for_tests()
    return compat.OpenAICompatClient(
        base_url="https://example.invalid/v1",
        api_key="test-not-a-real-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _: None,
        provider_random_fn=lambda: 0.5,
        **kwargs,
    )


def _chat(client, *, stream: bool = True):
    return client.chat(messages=[{"role": "user", "content": "test"}], stream=stream)


def _attempts():
    summary = last_provider_call_summary()
    assert summary is not None
    return summary["attempts"]


def test_each_attempt_records_its_wall_clock_start_retry_index_and_usage_source() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b'data: {"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    before = datetime.now(timezone.utc)
    assert _chat(_client(handler)).content == "ok"
    after = datetime.now(timezone.utc)

    first, second = _attempts()
    for attempt in (first, second):
        assert _ISO_UTC_MS.fullmatch(attempt["request_started_at"])
        started = datetime.fromisoformat(attempt["request_started_at"].replace("Z", "+00:00"))
        assert before - timedelta(seconds=1) <= started <= after + timedelta(seconds=1)
    assert first["request_started_at"] <= second["request_started_at"]
    assert [first["retry_index"], second["retry_index"]] == [0, 1]
    assert first["usage_source"] == "none"
    assert second["usage_source"] == "provider"


@pytest.mark.parametrize(
    ("body", "expected_source"),
    [
        pytest.param(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
            "provider",
            id="usage-reported",
        ),
        pytest.param({"choices": [{"message": {"content": "ok"}}]}, "none", id="no-usage"),
    ],
)
def test_non_streaming_attempt_records_where_its_usage_came_from(body, expected_source) -> None:
    client = _client(lambda _request: httpx.Response(200, json=body))

    assert _chat(client, stream=False).content == "ok"

    (attempt,) = _attempts()
    assert attempt["stream"] is False
    assert attempt["retry_index"] == 0
    assert attempt["usage_source"] == expected_source
    assert _ISO_UTC_MS.fullmatch(attempt["request_started_at"])


def test_a_compatibility_resend_keeps_the_retry_index_of_its_provider_attempt() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "stream_options unsupported"}})
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    _chat(_client(handler))

    assert [attempt["retry_index"] for attempt in _attempts()] == [0, 0]


def test_a_read_timeout_before_the_first_event_is_timeout_first_event() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, content=_OK_STREAM)

    assert _chat(_client(handler)).content == "ok"

    first, second = _attempts()
    assert first["terminal_reason"] == "timeout_first_event"
    assert second["terminal_reason"] == "done"


class _EventThenReadTimeout(httpx.SyncByteStream):
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"par"}}]}\n\n'
        raise httpx.ReadTimeout("The read operation timed out")


def test_a_read_timeout_after_the_first_event_is_timeout_stall() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=_EventThenReadTimeout())
        return httpx.Response(200, content=_OK_STREAM)

    assert _chat(_client(handler)).content == "ok"

    first, second = _attempts()
    assert first["terminal_reason"] == "timeout_stall"
    assert first["event_count"] == 1
    assert second["terminal_reason"] == "done"


class _KeepaliveOnly(httpx.SyncByteStream):
    def __iter__(self):
        for _ in range(20):
            yield b": keepalive\n\n"


class _EventThenKeepalive(httpx.SyncByteStream):
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"par"}}]}\n\n'
        for _ in range(20):
            yield b": keepalive\n\n"


@pytest.mark.parametrize(
    ("stream_cls", "expected_reason"),
    [
        pytest.param(_KeepaliveOnly, "timeout_first_event", id="before-first-event"),
        pytest.param(_EventThenKeepalive, "timeout_stall", id="after-first-event"),
    ],
)
def test_the_no_progress_watchdog_is_split_by_whether_an_event_arrived(
    stream_cls, expected_reason
) -> None:
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += 1.1
        return now

    client = _client(
        lambda _request: httpx.Response(200, stream=stream_cls()),
        stream_no_progress_timeout_s=2.0,
        stream_progress_clock=clock,
    )

    with pytest.raises(LLMError, match="meaningful payload"):
        _chat(client)

    summary = last_provider_call_summary()
    assert summary is not None
    assert [attempt["terminal_reason"] for attempt in summary["attempts"]] == [
        expected_reason,
        expected_reason,
    ]
    # The call-level category is unchanged: a watchdog stop is still no_progress.
    assert summary["status_category"] == "no_progress"


class _TwoEventsThenDone(httpx.SyncByteStream):
    def __init__(self, state: dict[str, bool]) -> None:
        self._state = state

    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"half "}}]}\n\n'
        self._state["deadline_passed"] = True
        yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
        yield b"data: [DONE]\n\n"


def test_a_run_deadline_that_fires_mid_stream_is_recorded_as_budget_abort() -> None:
    state = {"deadline_passed": False}
    client = _client(lambda _request: httpx.Response(200, stream=_TwoEventsThenDone(state)))
    client._stream_deadline_exhausted = lambda: state["deadline_passed"]  # type: ignore[attr-defined]

    with pytest.raises(DeadlineExhausted):
        _chat(client)

    summary = last_provider_call_summary()
    assert summary is not None
    assert [attempt["terminal_reason"] for attempt in summary["attempts"]] == ["budget_abort"]
    # A budget stop is not retried, and its call-level category is unchanged.
    assert summary["retry_count"] == 0
    assert summary["status_category"] == "deadline"


def test_a_timeout_of_the_budget_narrowed_attempt_is_budget_abort_not_a_stall() -> None:
    def handler(request):
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    client = _client(handler)
    client._run_deadline_limited_timeout_elapsed = lambda: True  # type: ignore[attr-defined]

    with pytest.raises(LLMError, match="timed out"):
        _chat(client)

    reasons = {attempt["terminal_reason"] for attempt in _attempts()}
    assert reasons == {"budget_abort"}


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_the_deadline_clamp_exposes_when_a_timeout_belongs_to_the_run_budget() -> None:
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(5.0, clock=clock)
    client = type("Client", (), {"timeout_s": 60.0})()

    with temporarily_clamp_client_timeout(client, deadline, reserve_seconds=1.0):
        # 5.0 remaining - 1.0 cleanup - 1.0 finalization reserve: the attempt
        # is narrowed to 3.0 s, so the allowance ends at t=103.
        assert client._run_deadline_limited_timeout_elapsed() is False  # type: ignore[attr-defined]
        clock.advance(3.0)
        assert client._run_deadline_limited_timeout_elapsed() is True  # type: ignore[attr-defined]

    assert not hasattr(client, "_run_deadline_limited_timeout_elapsed")
