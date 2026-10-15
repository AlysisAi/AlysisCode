"""The provider retry wall-clock cap is measured from the first failure.

An endpoint that closes a non-streaming request at 900 s fails it with "peer
closed connection without sending complete message body (incomplete chunked
read)". That failure is retryable and connection drops have a budget of five
retries, yet no retry ever started: the 60 s cap was measured from the start of
the first attempt, so a 900 s attempt had spent it before failing. Telemetry
recorded attempt_count=1 and retry_count=0 while the console said
"Infrastructure error after retries".

Tests are lettered (a)-(e). Time is a fake clock throughout;
nothing sleeps.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig
from alysis_code.llm import provider_limits as provider_limits_mod
from alysis_code.llm.openai_compat import LLMError, OpenAICompatClient
from alysis_code.llm.provider_limits import ProviderRetrySettings, run_provider_limited_call
from alysis_code.provider_telemetry import (
    last_provider_call_summary,
    reset_provider_telemetry_for_tests,
)
from alysis_code.run_outcome import INFRASTRUCTURE_FAILURE_EXIT_CODE
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.transport_retry import is_connection_drop_error

# The exception text such a drop produces, verbatim.
DROP_TEXT = "peer closed connection without sending complete message body (incomplete chunked read)"
CAP_SECONDS = 60.0


class _FakeTime:
    """A clock that moves only when an attempt runs or a backoff sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _run(
    call: Callable[[], str],
    fake: _FakeTime,
    *,
    settings: ProviderRetrySettings | None = None,
) -> str:
    return run_provider_limited_call(
        call=call,
        provider_key=None,
        provider_concurrency_caps={},
        retry_settings=settings or ProviderRetrySettings(),
        operation="test_retry_cap_first_failure",
        sleep_fn=fake.sleep,
        random_fn=lambda: 0.5,
        retry_wall_clock_cap_seconds=CAP_SECONDS,
        clock_fn=fake.clock,
    )


def _connection_drop_error() -> LLMError:
    return LLMError(f"LLM request failed: {DROP_TEXT}")


# --- (a) --------------------------------------------------------------------


def test_a_first_attempt_failing_at_900s_gets_exactly_one_retry_started_at_900s() -> None:
    fake = _FakeTime()
    starts: list[float] = []

    def call() -> str:
        starts.append(fake.now)
        fake.now += 900.0
        raise _connection_drop_error()

    with pytest.raises(LLMError, match="incomplete chunked read"):
        _run(call, fake)

    # The retry starts 2 s (the first connection-drop backoff) after the
    # 900 s failure. It also runs 900 s, by which time the window that opened
    # at the first failure has closed, so no third attempt starts.
    assert starts == [0.0, 902.0]
    assert fake.sleeps == [2.0]


def test_a_the_retry_after_a_900s_failure_can_succeed() -> None:
    fake = _FakeTime()
    starts: list[float] = []

    def call() -> str:
        starts.append(fake.now)
        if len(starts) == 1:
            fake.now += 900.0
            raise _connection_drop_error()
        fake.now += 30.0
        return "ok"

    assert _run(call, fake) == "ok"
    assert starts == [0.0, 902.0]


# --- (b) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "expected_starts", "expected_sleeps"),
    [
        pytest.param(
            ProviderRetrySettings(max_retries=3, base_delay_seconds=1.0, max_delay_seconds=30.0),
            [0.0, 2.0, 5.0, 10.0],
            [1.0, 2.0, 4.0],
            id="max-retries-first",
        ),
        pytest.param(
            ProviderRetrySettings(max_retries=10, base_delay_seconds=10.0, max_delay_seconds=10.0),
            [0.0, 11.0, 22.0, 33.0, 44.0, 55.0],
            [10.0] * 5,
            id="cap-first",
        ),
    ],
)
def test_b_fast_failures_retry_until_max_retries_or_the_cap_whichever_comes_first(
    settings: ProviderRetrySettings,
    expected_starts: list[float],
    expected_sleeps: list[float],
) -> None:
    fake = _FakeTime()
    starts: list[float] = []

    def call() -> str:
        starts.append(fake.now)
        fake.now += 1.0
        raise LLMError("LLM error 503: service unavailable")

    with pytest.raises(LLMError, match="503"):
        _run(call, fake, settings=settings)

    assert starts == expected_starts
    assert fake.sleeps == expected_sleeps


# --- (c) --------------------------------------------------------------------


def test_c_a_retry_in_flight_when_the_cap_expires_completes_and_is_not_retried() -> None:
    fake = _FakeTime()
    starts: list[float] = []
    finished: list[float] = []

    def call() -> str:
        starts.append(fake.now)
        if len(starts) == 1:
            # First failure at t=900: the window runs from 900 to 960.
            fake.now += 900.0
        else:
            # The retry starts at 902 and is still running at 960.
            fake.now += 100.0
            finished.append(fake.now)
        raise _connection_drop_error()

    with pytest.raises(LLMError):
        _run(call, fake)

    assert starts == [0.0, 902.0]
    assert finished == [1002.0]
    assert fake.sleeps == [2.0]


def test_c_a_retry_in_flight_when_the_cap_expires_may_still_succeed() -> None:
    fake = _FakeTime()
    starts: list[float] = []

    def call() -> str:
        starts.append(fake.now)
        if len(starts) == 1:
            fake.now += 900.0
            raise _connection_drop_error()
        fake.now += 100.0
        return "ok"

    assert _run(call, fake) == "ok"
    assert starts == [0.0, 902.0]


# --- (d) --------------------------------------------------------------------


def _invoke_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_run_agent: Callable[..., int],
):
    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path),
    }
    return CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            os.fspath(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "k",
            "hi",
        ],
        env=env,
    )


def test_d_retry_count_attempt_count_and_message_agree_with_the_attempts_made(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_provider_telemetry_for_tests()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, text="service unavailable")

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
    )

    def fake_run_agent(**_kwargs: object) -> int:
        client.chat(messages=[{"role": "user", "content": "hi"}])
        return 0

    result = _invoke_run(tmp_path, monkeypatch, fake_run_agent)

    summary = last_provider_call_summary()
    assert requests == 3
    assert summary is not None
    assert summary["attempt_count"] == 3
    assert summary["retry_count"] == 2
    assert result.exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE
    assert "Infrastructure error after 3 attempts (2 retries):" in result.output


def test_d_a_failure_that_was_never_retried_is_not_reported_as_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_agent(**_kwargs: object) -> int:
        def call() -> str:
            raise _connection_drop_error()

        run_provider_limited_call(
            call=call,
            provider_key=None,
            provider_concurrency_caps={},
            retry_settings=ProviderRetrySettings(disable_retries=True),
            operation="test_never_retried",
        )
        return 0

    result = _invoke_run(tmp_path, monkeypatch, fake_run_agent)

    assert result.exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE
    assert "after retries" not in result.output
    assert "Infrastructure error after 1 attempt (0 retries):" in result.output


def test_d_a_retry_cancelled_during_its_backoff_is_not_counted_as_a_retry() -> None:
    reset_provider_telemetry_for_tests()
    token = InteractiveCancellationToken()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, text="service unavailable")

    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
        # The user cancels while the first retry is still backing off.
        provider_sleep_fn=lambda _seconds: token.cancel(),
        provider_random_fn=lambda: 0.5,
    )

    # The interactive token surfaces cancellation as KeyboardInterrupt, the
    # established Ctrl-C control flow.
    with pytest.raises(KeyboardInterrupt):
        client.chat(messages=[{"role": "user", "content": "hi"}], cancellation_token=token)

    summary = last_provider_call_summary()
    assert requests == 1
    assert summary is not None
    assert summary["attempt_count"] == 1
    assert summary["retry_count"] == 0


# --- (e) --------------------------------------------------------------------


def test_e_the_dropped_connection_text_is_a_retryable_connection_drop() -> None:
    error = _connection_drop_error()

    assert is_connection_drop_error(error)
    assert provider_limits_mod._provider_retry_reason(error) is not None


def test_e_a_failure_after_900s_gets_its_retry_through_the_real_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeTime()
    # The client passes no clock of its own, so the retry loop reads the
    # module clock; the fake replaces it for the whole call.
    monkeypatch.setattr(
        provider_limits_mod,
        "time",
        SimpleNamespace(monotonic=fake.clock, sleep=fake.sleep),
    )
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(fake.now)
        if len(starts) == 1:
            fake.now += 900.0
            raise httpx.RemoteProtocolError(DROP_TEXT, request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://token-plan-ams.xiaomimimo.com/v1",
        api_key="test",
        model="mimo-v2.5",
        transport=httpx.MockTransport(handler),
        provider_sleep_fn=fake.sleep,
        provider_random_fn=lambda: 0.5,
    )

    response = client.chat(messages=[{"role": "user", "content": "hi"}])

    # Production defaults: 60 s cap, general budget 1, connection drops 5.
    assert client._provider_retry_wall_clock_cap_seconds == CAP_SECONDS
    assert client.provider_retry_settings.max_retries == 1
    assert response.content == "ok"
    assert starts == [0.0, 902.0]
    assert fake.sleeps == [2.0]


# --- snapshot ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected_general", "expected_connection_drop"),
    [
        pytest.param(None, 1, 5, id="defaults"),
        pytest.param(7, 7, 7, id="larger-general-budget-wins"),
    ],
)
def test_config_snapshot_records_both_retry_budgets_and_where_the_cap_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: int | None,
    expected_general: int,
    expected_connection_drop: int,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    cfg = AppConfig(model="test-model")
    if configured is not None:
        cfg.provider_retry_max_retries = configured

    session = create_session(
        cfg=cfg,
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="test-key",
        surface=NoopSurface(),
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        snapshots = [
            event
            for event in read_session_events(session.store.path)
            if event.get("type") == "config_snapshot"
        ]
    finally:
        session.close()

    assert len(snapshots) == 1
    effective = snapshots[0]["payload"]["effective"]
    assert effective["provider_retry_max_retries"] == expected_general
    assert effective["connection_drop_retry_max_retries"] == expected_connection_drop
    assert effective["provider_retry_wall_clock_cap_seconds"] == CAP_SECONDS
    assert effective["provider_retry_wall_clock_cap_starts_at"] == "first_failure"
