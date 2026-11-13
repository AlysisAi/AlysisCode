"""Shared provider defaults, independent of request execution and transports."""

DEFAULT_PROVIDER_CONCURRENCY_CAPS: dict[str, int] = {"qwen": 4}
# Six retries on a doubling backoff (2, 4, 8, 16, 32, 64s): a quick second try
# for a blip, and about two minutes of patience for a per-minute rate limit or
# a short outage. One retry made a single 429 fatal for the whole turn.
DEFAULT_PROVIDER_RETRY_MAX_RETRIES = 6
DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS = 2.0
DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS = 120.0
# The retry window opens at a call's first failure (run_provider_limited_call)
# and no retry starts outside it. Failures that return at once fit the whole
# schedule; a request that hangs to its timeout uses the window up and gets
# fewer attempts, and a stalled stream gets one.
DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS = 180.0
