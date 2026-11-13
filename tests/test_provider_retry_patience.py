"""The default retry policy rides out a rate limit or a short outage.

One retry made a single ``429`` or a brief provider outage fatal for the whole
turn. These tests use a simulated clock: nothing sleeps and nothing is sent.
"""

from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.config import AppConfig, config_path, load_config, save_config, set_config_value
from alysis_code.llm.openai_compat import LLMError
from alysis_code.llm.provider_limits import (
    ProviderRetrySettings,
    resolve_provider_retry_settings,
    run_provider_limited_call,
)
from alysis_code.llm.types import LLMStreamNoProgressError


class _Timeline:
    """A clock that only advances when the call or the backoff says so."""

    def __init__(self, failures: list[tuple[Exception, float]]):
        self.now = 0.0
        self.failures = list(failures)
        self.attempt_started_at: list[float] = []
        self.sleeps: list[float] = []

    def call(self) -> str:
        self.attempt_started_at.append(self.now)
        if not self.failures:
            return "ok"
        error, seconds = self.failures.pop(0)
        self.now += seconds
        raise error

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, settings: ProviderRetrySettings | None = None, **overrides):
        return run_provider_limited_call(
            call=self.call,
            provider_key="zai",
            provider_concurrency_caps={},
            retry_settings=settings if settings is not None else ProviderRetrySettings(),
            operation="chat_completions",
            sleep_fn=self.sleep,
            random_fn=lambda: 0.5,
            clock_fn=lambda: self.now,
            **overrides,
        )


def _rate_limited() -> LLMError:
    return LLMError('LLM error 429: {"error":{"code":"1302","message":"Rate limit reached"}}')


def test_rate_limit_that_clears_within_a_minute_is_survived() -> None:
    timeline = _Timeline([(_rate_limited(), 0.3)] * 5)
    assert timeline.run() == "ok"
    assert timeline.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert timeline.attempt_started_at[-1] > 60.0  # past a per-minute window


def test_a_provider_that_stays_down_fails_after_about_two_minutes() -> None:
    timeline = _Timeline([(LLMError("LLM error 503: servers are currently overloaded"), 0.2)] * 20)
    with pytest.raises(LLMError, match="503"):
        timeline.run()
    assert timeline.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    assert len(timeline.attempt_started_at) == 7
    assert 120.0 < timeline.now < 180.0


def test_slow_failures_get_fewer_attempts_from_the_same_window() -> None:
    # Each attempt hangs for the 60s request timeout before failing.
    timeline = _Timeline(
        [(LLMError("LLM request failed: The read operation timed out"), 60.0)] * 20
    )
    with pytest.raises(LLMError, match="timed out"):
        timeline.run()
    assert len(timeline.attempt_started_at) == 4
    # No attempt starts more than three minutes after the first failure.
    assert timeline.attempt_started_at[-1] - 60.0 <= 180.0
    assert timeline.now < 5 * 60


def test_a_stalled_stream_still_gets_exactly_one_more_attempt() -> None:
    # A 240s no-progress watchdog uses up the window by itself.
    timeline = _Timeline([(LLMStreamNoProgressError("no meaningful payload"), 240.0)] * 20)
    with pytest.raises(LLMStreamNoProgressError):
        timeline.run()
    assert len(timeline.attempt_started_at) == 2


def test_the_window_applies_without_the_caller_asking_for_it() -> None:
    # Adapters that pass no cap of their own are bounded too.
    timeline = _Timeline([(LLMError("LLM error 503: unavailable"), 100.0)] * 20)
    with pytest.raises(LLMError):
        timeline.run()
    assert len(timeline.attempt_started_at) == 3
    unbounded = _Timeline([(LLMError("LLM error 503: unavailable"), 100.0)] * 20)
    with pytest.raises(LLMError):
        unbounded.run(retry_wall_clock_cap_seconds=None)
    assert len(unbounded.attempt_started_at) == 7


def test_a_network_that_comes_back_within_seconds_is_survived() -> None:
    failures = [(httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"), 0.0)]
    timeline = _Timeline(failures * 6)
    assert timeline.run() == "ok"
    assert timeline.sleeps == [0.2, 0.4, 0.8, 1.6, 3.2, 6.4]


def test_server_stated_wait_is_never_shortened() -> None:
    error = _rate_limited()
    error.provider_retry_after = 45
    timeline = _Timeline([(error, 0.1)])
    assert timeline.run() == "ok"
    assert timeline.sleeps == [45.0]


@pytest.mark.parametrize("retries", [0, 1])
def test_an_explicit_smaller_budget_is_kept(retries: int) -> None:
    cfg = AppConfig(provider_retry_max_retries=retries)
    timeline = _Timeline([(_rate_limited(), 0.1)] * 20)
    with pytest.raises(LLMError):
        timeline.run(resolve_provider_retry_settings(cfg))
    assert len(timeline.attempt_started_at) == retries + 1


def test_defaults_reach_the_session_configuration() -> None:
    settings = resolve_provider_retry_settings(AppConfig())
    assert (settings.max_retries, settings.base_delay_seconds) == (6, 2.0)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("alysis_code.config.load_persisted_profile_keys", lambda: {})

    def write(**retry):
        # Match save_config in earlier releases: every field, no migration marker.
        raw = AppConfig(model="glm-5.3-flash").model_dump()
        raw.pop("provider_retry_defaults_applied")
        raw.update(retry)
        config_path().write_text(json.dumps(raw), encoding="utf-8")

    return write


def _retry(cfg: AppConfig) -> tuple[int, float, float]:
    return (
        cfg.provider_retry_max_retries,
        cfg.provider_retry_base_delay_seconds,
        cfg.provider_retry_max_delay_seconds,
    )


LEGACY = {
    "provider_retry_max_retries": 1,
    "provider_retry_base_delay_seconds": 10.0,
    "provider_retry_max_delay_seconds": 120.0,
}


def test_old_built_in_values_saved_by_earlier_releases_are_replaced(config_file) -> None:
    config_file(**LEGACY)
    cfg = load_config()
    assert _retry(cfg) == (6, 2.0, 120.0)
    assert cfg.provider_retry_defaults_applied
    # Reading never writes; the next ordinary save records it.
    assert "provider_retry_defaults_applied" not in json.loads(config_path().read_text())
    save_config(cfg)
    assert _retry(load_config()) == (6, 2.0, 120.0)
    assert json.loads(config_path().read_text())["provider_retry_defaults_applied"] is True


@pytest.mark.parametrize(
    "custom",
    [
        {**LEGACY, "provider_retry_max_retries": 0},
        {**LEGACY, "provider_retry_max_retries": 3},
        {**LEGACY, "provider_retry_base_delay_seconds": 5.0},
        {**LEGACY, "provider_retry_max_delay_seconds": 30.0},
    ],
)
def test_customised_retry_settings_are_kept(config_file, custom) -> None:
    config_file(**custom)
    cfg = load_config()
    assert _retry(cfg) == tuple(custom.values())
    assert cfg.provider_retry_defaults_applied


def test_choosing_the_old_values_after_the_migration_is_kept(config_file) -> None:
    config_file(**LEGACY)
    cfg = load_config()
    for key, value in LEGACY.items():
        cfg = set_config_value(cfg, key, str(value))
    save_config(cfg)
    reloaded = load_config()
    assert _retry(reloaded) == (1, 10.0, 120.0)
    assert reloaded.provider_retry_defaults_applied


def test_a_config_saved_with_the_new_defaults_is_left_alone(config_file) -> None:
    config_file()
    assert _retry(load_config()) == (6, 2.0, 120.0)
