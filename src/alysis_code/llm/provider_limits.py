from __future__ import annotations

import errno
import logging
import random
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlsplit

from ..cancellation import CooperativeCancellationError
from ..failure_category import is_provider_throttling_error, provider_unavailable_retry_reason
from ..provider_url import known_provider_key_from_base_url
from ..transport_retry import (
    RETRY_REASON_TRANSPORT_CONNECTION_DROP,
    connection_drop_budget_exhausted,
    connection_drop_delay_seconds,
    connection_drop_retry_budget,
    is_connection_drop_error,
    mark_transport_connection_failure,
)
from .http_cancellation import raise_if_cancelled
from .provider_defaults import (
    DEFAULT_PROVIDER_CONCURRENCY_CAPS as DEFAULT_PROVIDER_CONCURRENCY_CAPS,
)
from .provider_defaults import (
    DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS as DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS,
)
from .provider_defaults import (
    DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS as DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS,
)
from .provider_defaults import (
    DEFAULT_PROVIDER_RETRY_MAX_RETRIES as DEFAULT_PROVIDER_RETRY_MAX_RETRIES,
)
from .provider_defaults import (
    DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS as DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS,
)
from .rate_limit_metadata import HARD_QUOTA_CODES, MAX_RETRY_SECONDS, bounded_seconds
from .types import LLMStreamNoProgressError

T = TypeVar("T")

_LOGGER = logging.getLogger(__name__)
_JITTER_RATIO = 0.25
_QWEN_CANONICAL_KEY = "qwen"
_QWEN_PROVIDER_ALIASES = frozenset(
    {
        "aliyun",
        "aliyuncs",
        "dashscope",
        "dashscopechat",
        "qwen",
        "qwen2",
        "qwen25",
        "qwen3",
        "qwen35",
        "qwenmax",
        "tongyi",
    }
)
_GENERIC_HOST_PARTS = frozenset(
    {
        "api",
        "ai",
        "chat",
        "coding",
        "compatible",
        "com",
        "cn",
        "intl",
        "mode",
        "v1",
        "v1beta",
        "www",
    }
)
_KNOWN_TRANSPORT_PROVIDER_KEYS = frozenset(
    {
        "qwen",
        "openrouter",
        "openai",
        "azure",
        "deepseek",
        "gemini",
        "mistral",
        "nvidia",
        "zai_coding_plan",
        "xai",
    }
)
_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[tuple[str, int], _ProviderAdmissionGate] = {}


class _ProviderAdmissionGate:
    """A concurrency gate that an interactive cancellation can wake.

    ``threading.Semaphore.acquire`` cannot wait on a cancellation event at the
    same time. A condition gives each token a one-shot wake subscription, so a
    saturated provider cap remains interruptible without imposing a timeout or
    changing the cap's fairness semantics. Minimal legacy tokens without a
    subscription use bounded observations solely to notice cancellation; the
    wait has no overall time limit.
    """

    def __init__(self, permits: int) -> None:
        self._condition = threading.Condition()
        self._available = permits

    def acquire(self, cancellation_token: Any | None = None) -> None:
        subscribe = getattr(cancellation_token, "subscribe", None)

        def _wake() -> None:
            with self._condition:
                self._condition.notify_all()

        unsubscribe = subscribe(_wake) if callable(subscribe) else None
        has_subscription = callable(unsubscribe)
        try:
            with self._condition:
                while self._available <= 0:
                    raise_if_cancelled(cancellation_token)
                    self._condition.wait(
                        timeout=None if cancellation_token is None or has_subscription else 0.1
                    )
                raise_if_cancelled(cancellation_token)
                self._available -= 1
        finally:
            if callable(unsubscribe):
                unsubscribe()

    def release(self) -> None:
        with self._condition:
            self._available += 1
            self._condition.notify()


@dataclass(frozen=True)
class ProviderRetrySettings:
    max_retries: int = DEFAULT_PROVIDER_RETRY_MAX_RETRIES
    base_delay_seconds: float = DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS
    # Optional control-plane calls can prefer immediate caller-level fallback
    # over every retry class, including the separate connection-drop budget.
    disable_retries: bool = False


def canonical_provider_key(provider_key: str | None) -> str | None:
    normalized = _normalize_provider_key(provider_key)
    if not normalized:
        return None
    tokens = _provider_tokens(normalized)
    if tokens & _QWEN_PROVIDER_ALIASES:
        return _QWEN_CANONICAL_KEY
    return normalized


def best_effort_provider_key(*, base_url: str | None, model: str | None) -> str | None:
    url_key = _provider_key_from_base_url(base_url)
    if url_key in _KNOWN_TRANSPORT_PROVIDER_KEYS:
        return url_key
    model_key = _provider_key_from_model(model)
    if model_key is not None:
        return model_key
    return url_key


def resolve_provider_retry_settings(cfg: object | None) -> ProviderRetrySettings:
    return ProviderRetrySettings(
        max_retries=_coerce_non_negative_int(
            getattr(cfg, "provider_retry_max_retries", DEFAULT_PROVIDER_RETRY_MAX_RETRIES),
            default=DEFAULT_PROVIDER_RETRY_MAX_RETRIES,
        ),
        base_delay_seconds=_coerce_positive_float(
            getattr(
                cfg,
                "provider_retry_base_delay_seconds",
                DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS,
            ),
            default=DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS,
        ),
        max_delay_seconds=_coerce_positive_float(
            getattr(
                cfg,
                "provider_retry_max_delay_seconds",
                DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS,
            ),
            default=DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS,
        ),
    )


def provider_retry_snapshot_values(cfg: object | None) -> dict[str, Any]:
    """The provider retry policy as the session enforces it, for the snapshot.

    Both budgets are recorded because a failure report cannot otherwise say
    which one governed it: every retryable class gets the general budget, and
    a connection dropped mid-response gets ``max(general, dedicated)`` (see
    ``transport_retry.connection_drop_retry_budget``). The value recorded for
    connection drops is that effective maximum.
    """
    settings = resolve_provider_retry_settings(cfg)
    return {
        "provider_retry_max_retries": settings.max_retries,
        "connection_drop_retry_max_retries": connection_drop_retry_budget(settings.max_retries),
        "provider_retry_wall_clock_cap_seconds": DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS,
        "provider_retry_wall_clock_cap_starts_at": "first_failure",
    }


def resolve_provider_concurrency_cap(
    provider_concurrency_caps: Mapping[str, int | None] | None,
    provider_key: str | None,
) -> int | None:
    canonical_key = canonical_provider_key(provider_key)
    if canonical_key is None:
        return None
    caps = provider_concurrency_caps or DEFAULT_PROVIDER_CONCURRENCY_CAPS
    exact_found, exact_cap = _lookup_cap(caps, canonical_key)
    if exact_found:
        return exact_cap

    alias_caps: list[int] = []
    for raw_key, raw_cap in caps.items():
        if canonical_provider_key(str(raw_key)) != canonical_key:
            continue
        cap = _coerce_cap(raw_cap)
        if cap is not None:
            alias_caps.append(cap)
    if alias_caps:
        return min(alias_caps)
    return None


def run_provider_limited_call(
    *,
    call: Callable[[], T],
    provider_key: str | None,
    provider_concurrency_caps: Mapping[str, int | None] | None = None,
    retry_settings: ProviderRetrySettings | None = None,
    operation: str,
    sleep_fn: Callable[[float], None] | None = None,
    random_fn: Callable[[], float] | None = None,
    on_retry: Callable[[int, str, float], None] | None = None,
    on_retry_event: Callable[[dict[str, object]], None] | None = None,
    retry_deadline_allows: Callable[[float], bool] | None = None,
    retry_wall_clock_cap_seconds: float | None = None,
    clock_fn: Callable[[], float] | None = None,
    cancellation_token: Any | None = None,
) -> T:
    """Run ``call`` under the provider concurrency cap, retrying transient failures.

    Retry budgets: every retryable class gets ``retry_settings.max_retries``
    (the general budget, default 1). A connection dropped mid-response gets
    ``max(general, DEFAULT_CONNECTION_DROP_MAX_RETRIES)`` -- 5 unless a caller
    already configured more. Both maxima are recorded in the config snapshot
    (``provider_retry_snapshot_values``).

    ``retry_wall_clock_cap_seconds`` bounds how long the no-progress,
    unavailable, truncated-stream and connection-drop classes may keep
    retrying. Its window opens at the call's first failure, not at the start
    of the first attempt: a request that failed after 900 s still gets its
    retry, while fast failures keep retrying only until the window closes. A
    retry may start only if it would begin, after its backoff, inside the
    window. An attempt already in flight when the window closes is never
    interrupted; the cap is consulted only once that attempt has failed.

    The exception that finally escapes carries the attempts made and the
    retries started (``provider_attempt_counts``), so a caller can report what
    happened instead of assuming that retries took place.
    """
    settings = retry_settings or ProviderRetrySettings()
    canonical_key = canonical_provider_key(provider_key)
    cap = resolve_provider_concurrency_cap(provider_concurrency_caps, canonical_key)
    semaphore = _semaphore_for(canonical_key, cap) if canonical_key and cap else None
    sleep = sleep_fn or time.sleep
    jitter = random_fn or random.random
    clock = clock_fn or time.monotonic
    retry_started_at = clock()
    # Opened by the first failure (see the docstring). ``retry_started_at``
    # stays the call's start and only feeds the observer's ``elapsed_ms``.
    cap_window_started_at: float | None = None
    retries_used = 0

    while True:
        raise_if_cancelled(cancellation_token)
        if semaphore is not None:
            semaphore.acquire(cancellation_token)
        try:
            raise_if_cancelled(cancellation_token)
            result = call()
            # Close the small race between a provider's final transport check
            # and returning control to the turn. A cancellation already
            # published for this attempt still owns the outcome.
            raise_if_cancelled(cancellation_token)
            return result
        except CooperativeCancellationError:
            # Cancellation is terminal, and protected transports have already
            # removed exception chains that could retain request material.
            raise
        except Exception as exc:
            raise_if_cancelled(cancellation_token)
            _tag_provider_attempt_counts(exc, retries_started=retries_used)
            retry_reason = _provider_retry_reason(exc)
            if settings.disable_retries:
                raise
            # A connection dropped mid-response gets a budget of its own. It is
            # the one failure class that costs nothing to retry -- the body
            # never arrived, so no work is repeated -- and costs the whole task
            # to give up on. Observed deterministically: regex-chess died at
            # its first LLM call, the same way, in two consecutive trials.
            #
            # Every other reason keeps the configured budget untouched, and a
            # campaign that already raised max_retries keeps its larger value.
            connection_drop = is_connection_drop_error(exc)
            retries_allowed = (
                connection_drop_retry_budget(settings.max_retries)
                if connection_drop
                else settings.max_retries
            )
            if retry_reason is None or _is_provider_call_non_retryable(exc):
                raise
            if connection_drop_budget_exhausted(retries_used, retries_allowed):
                if connection_drop:
                    # Still a genuine error -- nothing was accomplished -- but
                    # a named one, so a campaign can tell a dead route from a
                    # model that failed the task on its merits.
                    mark_transport_connection_failure(exc)
                raise
            if connection_drop:
                retry_reason = RETRY_REASON_TRANSPORT_CONNECTION_DROP
                wait_seconds = connection_drop_delay_seconds(retries_used, jitter_sample=jitter())
            else:
                wait_seconds = _retry_delay_seconds(settings, retries_used, jitter)
            failed_at = clock() if retry_wall_clock_cap_seconds is not None else None
            if cap_window_started_at is None:
                cap_window_started_at = failed_at
            minimum_wait = bounded_seconds(getattr(exc, "provider_retry_after", 0))
            # Never shorten the server's minimum delay to fit our retry cap.
            # Return the timing to the caller if the wait exceeds this turn's budget.
            if minimum_wait >= MAX_RETRY_SECONDS or minimum_wait > settings.max_delay_seconds:
                raise
            wait_seconds = max(wait_seconds, minimum_wait)
            if _retry_wall_clock_cap_blocks(
                retry_reason=retry_reason,
                cap_window_started_at=cap_window_started_at,
                now=failed_at,
                wait_seconds=wait_seconds,
                cap_seconds=retry_wall_clock_cap_seconds,
            ):
                _LOGGER.info(
                    "provider_retry_wall_clock_cap_blocked",
                    extra={
                        "operation": operation,
                        "provider_key": canonical_key,
                        "retry_attempt": retries_used + 1,
                        "wait_seconds": wait_seconds,
                        "retry_reason": retry_reason,
                    },
                )
                raise
            if retry_deadline_allows is not None and not retry_deadline_allows(wait_seconds):
                _LOGGER.info(
                    "provider_retry_deadline_blocked",
                    extra={
                        "operation": operation,
                        "provider_key": canonical_key,
                        "retry_attempt": retries_used + 1,
                        "wait_seconds": wait_seconds,
                        "retry_reason": retry_reason,
                    },
                )
                raise
            if on_retry is not None:
                try:
                    on_retry(retries_used + 1, retry_reason, wait_seconds)
                except Exception:  # noqa: BLE001 - observers must not change retry behavior.
                    _LOGGER.debug("provider_retry_observer_failed", exc_info=True)
            if on_retry_event is not None:
                try:
                    on_retry_event(
                        {
                            "provider": canonical_key or "unknown",
                            "attempt": retries_used + 2,
                            "reason": retry_reason,
                            "elapsed_ms": max(
                                0,
                                int((clock() - retry_started_at) * 1000),
                            ),
                        }
                    )
                except Exception:  # noqa: BLE001 - observers cannot change retries.
                    _LOGGER.debug("provider_retry_event_observer_failed", exc_info=True)
            _LOGGER.info(
                f"{retry_reason}, retrying",
                extra={
                    "operation": operation,
                    "provider_key": canonical_key,
                    "retry_attempt": retries_used + 1,
                    "wait_seconds": wait_seconds,
                },
            )
        finally:
            if semaphore is not None:
                semaphore.release()
        retries_used += 1
        raise_if_cancelled(cancellation_token)
        if sleep_fn is not None:
            # Injected sleepers are an intentional test/host compatibility
            # boundary. They retain ownership of how time advances, with
            # cancellation checked on both sides.
            sleep(wait_seconds)
        else:
            subscribe = getattr(cancellation_token, "subscribe", None)
            if callable(subscribe):
                wake = threading.Event()
                unsubscribe = subscribe(wake.set)
                try:
                    wake.wait(wait_seconds)
                finally:
                    if callable(unsubscribe):
                        unsubscribe()
            else:
                wait_for_cancel = getattr(cancellation_token, "wait", None)
                if callable(wait_for_cancel):
                    wait_for_cancel(wait_seconds)
                else:
                    sleep(wait_seconds)
        raise_if_cancelled(cancellation_token)


def _retry_wall_clock_cap_blocks(
    *,
    retry_reason: str,
    cap_window_started_at: float | None,
    now: float | None,
    wait_seconds: float,
    cap_seconds: float | None,
) -> bool:
    """Whether the wall-clock cap forbids starting another attempt.

    The window runs for ``cap_seconds`` from ``cap_window_started_at`` (the
    call's first failure). A retry is allowed only if it would start, after
    its backoff, inside that window.
    """
    if (
        retry_reason
        not in {
            "provider_no_progress",
            "provider_unavailable",
            "provider_stream_truncated",
            "provider_throttled",
            # The larger connection-drop budget is still subject to any
            # wall-clock cap a caller set. A route that caps retry time meant
            # it, and a bigger attempt budget must not quietly escape it.
            RETRY_REASON_TRANSPORT_CONNECTION_DROP,
        }
        or cap_seconds is None
    ):
        return False
    try:
        cap = float(cap_seconds)
    except (TypeError, ValueError):
        return False
    if cap <= 0:
        return True
    if cap_window_started_at is None or now is None:
        return False
    elapsed = max(0.0, float(now) - float(cap_window_started_at))
    return elapsed + max(0.0, float(wait_seconds)) > cap


def _provider_retry_reason(exc: Exception) -> str | None:
    if isinstance(exc, LLMStreamNoProgressError):
        return "provider_no_progress"
    if is_provider_throttling_error(exc):
        return "provider_throttled"
    transport_reason = _transient_transport_retry_reason(exc)
    if transport_reason is not None:
        return transport_reason
    return provider_unavailable_retry_reason(exc)


_TRANSIENT_TRANSPORT_ERRNOS = {
    value
    for value in (
        getattr(socket, "EAI_AGAIN", None),
        getattr(socket, "EAI_FAIL", None),
        getattr(socket, "EAI_NONAME", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "ETIMEDOUT", None),
    )
    if value is not None
}
_TRANSIENT_TRANSPORT_MARKERS = (
    "connection aborted",
    "connection refused",
    "connection reset",
    "getaddrinfo failed",
    "handshake operation timed out",
    "name or service not known",
    "name resolution",
    "network is unreachable",
    "nodename nor servname",
    "temporary failure in name resolution",
    "timed out",
)


def _transient_transport_retry_reason(exc: BaseException) -> str | None:
    for item in _iter_exception_chain(exc):
        if isinstance(item, (socket.timeout, TimeoutError)):
            return "provider_unavailable"
        if isinstance(item, ssl.SSLError) and _has_transient_transport_marker(item):
            return "provider_unavailable"
        if isinstance(
            item,
            (ConnectionAbortedError, ConnectionRefusedError, ConnectionResetError),
        ):
            return "provider_unavailable"
        if isinstance(item, OSError):
            if getattr(item, "errno", None) in _TRANSIENT_TRANSPORT_ERRNOS:
                return "provider_unavailable"
            if _has_transient_transport_marker(item):
                return "provider_unavailable"
    return None


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        cause = current.__cause__
        context = current.__context__
        current = cause if cause is not None else context
    return chain


def _has_transient_transport_marker(exc: BaseException) -> bool:
    message = str(exc or "").casefold()
    return any(marker in message for marker in _TRANSIENT_TRANSPORT_MARKERS)


PROVIDER_NON_RETRYABLE_ATTR = "_provider_call_non_retryable"


def mark_provider_call_non_retryable(exc: BaseException) -> None:
    """Tag an exception so :func:`run_provider_limited_call` will not retry it.

    Some failures match the retryable heuristics by message (e.g. an httpx read
    timeout contains "timed out") yet an immediate retry with the same budget is
    pointless — it will fail the same way and only double the dead air. A caller
    that knows a failure is not worth retrying marks it here so the retry loop
    skips it while still retrying genuinely transient failures.
    """
    try:
        setattr(exc, PROVIDER_NON_RETRYABLE_ATTR, True)
    except Exception:  # noqa: BLE001 - never let tagging crash the caller
        pass


def _is_provider_call_non_retryable(exc: BaseException) -> bool:
    code = getattr(exc, "provider_error_code", None)
    return bool(getattr(exc, PROVIDER_NON_RETRYABLE_ATTR, False)) or (
        type(code) is str and code in HARD_QUOTA_CODES
    )


PROVIDER_ATTEMPT_COUNT_ATTR = "_provider_attempt_count"
PROVIDER_RETRY_COUNT_ATTR = "_provider_retry_count"


def _tag_provider_attempt_counts(exc: BaseException, *, retries_started: int) -> None:
    """Stamp a failure with the attempts made and the retries started so far.

    Re-stamped on every failure, so the exception that finally escapes the
    retry loop carries the final numbers. Never raises.
    """
    try:
        setattr(exc, PROVIDER_ATTEMPT_COUNT_ATTR, int(retries_started) + 1)
        setattr(exc, PROVIDER_RETRY_COUNT_ATTR, int(retries_started))
    except Exception:  # noqa: BLE001 - never let tagging crash the caller
        pass


def provider_attempt_counts(exc: BaseException | None) -> tuple[int, int] | None:
    """``(attempts, retries)`` a provider call really made before it failed.

    Walks the cause/context chain because callers often re-raise the provider
    error wrapped. ``None`` means the failure never went through
    :func:`run_provider_limited_call`, which is not the same as zero retries.
    """
    if exc is None:
        return None
    for item in _iter_exception_chain(exc):
        attempts = getattr(item, PROVIDER_ATTEMPT_COUNT_ATTR, None)
        retries = getattr(item, PROVIDER_RETRY_COUNT_ATTR, None)
        if type(attempts) is int and type(retries) is int:
            return attempts, retries
    return None


def single_retry_is_worthwhile(exc: BaseException) -> bool:
    """Whether one more attempt at the same call could plausibly succeed.

    Only transport-class failures qualify: timeouts, resets, refused
    connections, and transient SSL/OS errors. A rejection — bad credentials,
    exhausted quota, a malformed request — fails identically on the second
    attempt and only spends another round trip. Failures the provider layer has
    already given up on are never retried again on top of that.

    Exposed for callers outside this module that run their own bounded retry,
    so the transient classification stays defined in exactly one place.
    """

    if _is_provider_call_non_retryable(exc):
        return False
    # A connection dropped mid-body is transient too, but its signatures
    # ("peer closed connection", "incomplete chunked read") are absent from
    # _TRANSIENT_TRANSPORT_MARKERS, which only knows socket-level failures.
    # Without this, a caller running its own bounded retry silently declined
    # to retry the one failure most worth retrying.
    if is_connection_drop_error(exc):
        return True
    return _transient_transport_retry_reason(exc) is not None


def reset_provider_limit_state_for_tests() -> None:
    with _SEMAPHORE_LOCK:
        _SEMAPHORES.clear()


def qwen_provider_aliases() -> tuple[str, ...]:
    return tuple(sorted(_QWEN_PROVIDER_ALIASES))


def _semaphore_for(provider_key: str, cap: int) -> _ProviderAdmissionGate:
    key = (provider_key, cap)
    with _SEMAPHORE_LOCK:
        semaphore = _SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = _ProviderAdmissionGate(cap)
            _SEMAPHORES[key] = semaphore
        return semaphore


def _retry_delay_seconds(
    settings: ProviderRetrySettings,
    retry_index: int,
    random_fn: Callable[[], float],
) -> float:
    base = max(settings.base_delay_seconds, 0.0)
    max_delay = max(settings.max_delay_seconds, base)
    raw_delay = min(max_delay, base * (2**retry_index))
    jitter_sample = min(max(float(random_fn()), 0.0), 1.0)
    jitter_offset = (jitter_sample - 0.5) * 2.0 * _JITTER_RATIO * raw_delay
    return max(0.0, min(max_delay, raw_delay + jitter_offset))


def _lookup_cap(
    caps: Mapping[str, int | None],
    key: str,
) -> tuple[bool, int | None]:
    for raw_key, raw_cap in caps.items():
        if _normalize_provider_key(str(raw_key)) == key:
            return True, _coerce_cap(raw_cap)
    return False, None


def _coerce_cap(raw: int | None) -> int | None:
    try:
        cap = int(raw if raw is not None else 0)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def _coerce_non_negative_int(raw: object, *, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _coerce_positive_float(raw: object, *, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _provider_key_from_model(model: str | None) -> str | None:
    raw_model = str(model or "").strip()
    normalized = _normalize_provider_key(raw_model)
    if not normalized:
        return None
    canonical = canonical_provider_key(normalized)
    if canonical is not None and canonical != normalized:
        return canonical
    if "/" in raw_model:
        prefix = raw_model.split("/", 1)[0]
        return canonical_provider_key(prefix) or _normalize_provider_key(prefix)
    return None


def _provider_key_from_base_url(base_url: str | None) -> str | None:
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        hostname = (parts.hostname or "").rstrip(".").casefold()
    except ValueError:
        return None
    if not hostname:
        return None
    known_provider = known_provider_key_from_base_url(raw)
    if known_provider:
        return known_provider
    canonical = canonical_provider_key(hostname)
    if canonical is not None and canonical != hostname:
        return canonical
    host_parts = [part for part in hostname.replace("-", ".").split(".") if part]
    for part in host_parts:
        canonical_part = canonical_provider_key(part)
        if canonical_part is not None and canonical_part != part:
            return canonical_part
    for part in reversed(host_parts):
        if part not in _GENERIC_HOST_PARTS:
            return canonical_provider_key(part) or part
    return hostname


def _normalize_provider_key(provider_key: str | None) -> str | None:
    normalized = str(provider_key or "").strip().casefold()
    if not normalized:
        return None
    return "".join(char if char.isalnum() else "_" for char in normalized).strip("_") or None


def _provider_tokens(provider_key: str) -> set[str]:
    ordered_tokens = [
        token
        for token in provider_key.replace("-", "_").replace("/", "_").replace(".", "_").split("_")
        if token
    ]
    tokens = set(ordered_tokens)
    compact = "".join(ordered_tokens)
    if compact:
        tokens.add(compact)
    return tokens
