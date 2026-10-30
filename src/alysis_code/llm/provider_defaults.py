"""Shared provider defaults, independent of request execution and transports."""

DEFAULT_PROVIDER_CONCURRENCY_CAPS: dict[str, int] = {"qwen": 4}
DEFAULT_PROVIDER_RETRY_MAX_RETRIES = 1
DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS = 10.0
DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS = 120.0
# The retry window opens at a call's first failure (run_provider_limited_call).
DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS = 60.0
