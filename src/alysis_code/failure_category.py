from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

import httpx

from .run_outcome import AGENT_FAILURE_EXIT_CODE, INFRASTRUCTURE_FAILURE_EXIT_CODE


class FailureCategory(StrEnum):
    INFRA_UNAVAILABLE = "infra_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_THROTTLED = "provider_throttled"
    PROVIDER_ERROR = "provider_error"
    PLANNER_FAILED = "planner_failed"
    IMPLEMENTATION_FAILED = "implementation_failed"
    VERIFICATION_FAILED = "verification_failed"


_THROTTLE_MESSAGE_MARKERS = (
    "too many requests",
    "rate limit",
    "rate-limit",
    "rate_limit",
    "quota",
    "concurrency quota",
    "concurrent request",
    "requests per minute",
    "requests/minute",
    "tokens per minute",
    "tpm",
    "rpm",
)
_QUOTA_CONTEXT_MARKERS = (
    "exceeded",
    "exhausted",
    "insufficient",
    "limit",
    "rate",
    "throttle",
)
_HTTP_429_RE = re.compile(r"\b(?:status[_ -]?code|http|error)?\s*429\b", re.IGNORECASE)
_INFRA_UNAVAILABLE_MARKERS = (
    "docker daemon",
    "docker.sock",
    "/var/run/docker.sock",
    "bwrap",
    "bubblewrap",
    "sandbox unavailable",
    "verify sandbox unavailable",
    "no usable backend",
    "unable to launch verify environment",
)
_PROVIDER_UNAVAILABLE_MESSAGE_MARKERS = (
    "connection aborted",
    "connection refused",
    "connection reset",
    "connect timeout",
    "connecttimeout",
    "dns",
    "incomplete chunked read",
    "name or service not known",
    "name resolution",
    "network is unreachable",
    "nodename nor servname",
    "peer closed connection",
    "read operation timed out",
    "read error",
    "read timeout",
    "readtimeout",
    "remote protocol error",
    "server disconnected",
    "server disconnect",
    "temporary failure in name resolution",
    "timed out",
)
_PROVIDER_STREAM_TRUNCATED_MESSAGE_MARKERS = (
    "incomplete chunked read",
    "peer closed connection",
    "remote protocol error",
    "server disconnected without sending complete message body",
    "stream truncated",
    "response body ended early",
)
_PROVIDER_UNAVAILABLE_STATUS_CODES = frozenset({408, *range(500, 600)})
_PROVIDER_PERMANENT_4XX_STATUS_CODES = frozenset(range(400, 500)) - {408, 429}
_CONTEXT_WINDOW_ERROR_MARKERS = (
    "context_length_exceeded",
    "hosted_request_too_large",
    "context length exceeded",
    "maximum context length",
    "context window exceeded",
    "exceeds the context window",
    "exceeds this model's context",
    "exceeds the model's context",
    "prompt is too long",
    "input is too long",
    "input token count exceeds",
    "too many input tokens",
    "input token limit exceeded",
)


def failure_category_value(category: FailureCategory | str | None) -> str | None:
    parsed = parse_failure_category(category)
    return parsed.value if parsed is not None else None


def parse_failure_category(category: FailureCategory | str | None) -> FailureCategory | None:
    if isinstance(category, FailureCategory):
        return category
    if category is None:
        return None
    try:
        return FailureCategory(str(category).strip())
    except ValueError:
        return None


def empty_failure_category_counts() -> dict[str, int]:
    return {category.value: 0 for category in FailureCategory}


def increment_failure_category_count(
    counts: dict[str, int],
    category: FailureCategory | str | None,
) -> None:
    parsed = parse_failure_category(category)
    if parsed is None:
        return
    counts[parsed.value] = int(counts.get(parsed.value, 0) or 0) + 1


def is_provider_throttling_error(error: Any) -> bool:
    status_code = _extract_status_code(error)
    if status_code == 429:
        return True

    message = str(error or "").casefold()
    if not message:
        return False
    if _HTTP_429_RE.search(message):
        return True
    if any(marker in message for marker in _THROTTLE_MESSAGE_MARKERS):
        return True
    if "quota" in message and any(marker in message for marker in _QUOTA_CONTEXT_MARKERS):
        return True
    if "concurrency" in message and any(marker in message for marker in _QUOTA_CONTEXT_MARKERS):
        return True
    return False


def is_provider_unavailable_error(error: Any) -> bool:
    return provider_unavailable_retry_reason(error) is not None


def provider_unavailable_retry_reason(error: Any) -> str | None:
    status_code = _extract_status_code_from_chain(error)
    if status_code in _PROVIDER_PERMANENT_4XX_STATUS_CODES:
        return None
    if status_code in _PROVIDER_UNAVAILABLE_STATUS_CODES:
        return "provider_unavailable"

    for exc in _iter_exception_chain(error):
        if isinstance(exc, httpx.RemoteProtocolError):
            return "provider_stream_truncated"
        if isinstance(exc, httpx.ReadError | httpx.ReadTimeout):
            if _message_has_stream_truncation_marker(str(exc)):
                return "provider_stream_truncated"
            return "provider_unavailable"
        if isinstance(
            exc,
            httpx.ConnectError
            | httpx.ConnectTimeout
            | httpx.NetworkError
            | httpx.PoolTimeout
            | httpx.TransportError,
        ):
            return "provider_unavailable"

    message = _exception_chain_message(error)
    if not message:
        return None
    if _message_has_stream_truncation_marker(message):
        return "provider_stream_truncated"
    if any(marker in message for marker in _PROVIDER_UNAVAILABLE_MESSAGE_MARKERS):
        return "provider_unavailable"
    return None


def is_infra_unavailable_error(error: Any) -> bool:
    message = str(error or "").casefold()
    if not message:
        return False
    return any(marker in message for marker in _INFRA_UNAVAILABLE_MARKERS)


def is_context_window_exceeded_error(error: Any) -> bool:
    """Recognize provider context-capacity failures across protocol adapters.

    Adapters currently normalize provider error objects into ``LLMError`` text,
    so this checks the full exception chain and deliberately requires
    context/input/prompt-specific phrases rather than treating every HTTP 400
    as compactable.
    """

    message = _exception_chain_message(error)
    if not message:
        return False
    return any(marker in message for marker in _CONTEXT_WINDOW_ERROR_MARKERS)


def extract_status_code(error: Any) -> int | None:
    """Best-effort HTTP status code from an error or its exception chain.

    Reads a ``status_code``/``code`` attribute, a ``response.status_code``, or a
    ``... error NNN ...`` marker in the message, walking ``__cause__``/``__context__``.
    Returns ``None`` when no status can be recovered.
    """
    return _extract_status_code_from_chain(error)


def classify_failure_category(error: Any) -> FailureCategory:
    """Map an arbitrary exception onto a :class:`FailureCategory`.

    Reuses the existing provider classifiers so the diagnostic vocabulary stays
    consistent across the chat/run path, Forge workers, and delegated agents.
    Ordering reflects specificity: throttling (HTTP 429 / rate-limit markers) and
    infra-unavailability are checked before generic provider-unavailability, and a
    readable-but-non-transient provider status (e.g. 400/401/403/404) is reported as
    :attr:`FailureCategory.PROVIDER_ERROR` rather than being mistaken for a transient
    outage or for an agent implementation failure. Errors carrying no provider signal
    fall through to :attr:`FailureCategory.IMPLEMENTATION_FAILED`.
    """
    if is_provider_throttling_error(error):
        return FailureCategory.PROVIDER_THROTTLED
    if is_infra_unavailable_error(error):
        return FailureCategory.INFRA_UNAVAILABLE
    if is_provider_unavailable_error(error):
        return FailureCategory.PROVIDER_UNAVAILABLE
    if extract_status_code(error) is not None:
        return FailureCategory.PROVIDER_ERROR
    return FailureCategory.IMPLEMENTATION_FAILED


def exit_code_for_failure(error: Any) -> int:
    """Return a process exit code that separates transient infrastructure failures."""

    category = classify_failure_category(error)
    if category in {
        FailureCategory.INFRA_UNAVAILABLE,
        FailureCategory.PROVIDER_UNAVAILABLE,
        FailureCategory.PROVIDER_THROTTLED,
    }:
        return INFRASTRUCTURE_FAILURE_EXIT_CODE
    return AGENT_FAILURE_EXIT_CODE


def _extract_status_code(error: Any) -> int | None:
    for attr_name in ("status_code", "code"):
        status_code = _coerce_status_code(getattr(error, attr_name, None))
        if status_code is not None:
            return status_code

    response = getattr(error, "response", None)
    if response is not None:
        status_code = _coerce_status_code(getattr(response, "status_code", None))
        if status_code is not None:
            return status_code

    match = re.search(r"\b(?:LLM|Responses)?\s*error\s+(\d{3})\b", str(error or ""), re.IGNORECASE)
    if match is None:
        return None
    return _coerce_status_code(match.group(1))


def _extract_status_code_from_chain(error: Any) -> int | None:
    for exc in _iter_exception_chain(error):
        status_code = _extract_status_code(exc)
        if status_code is not None:
            return status_code
    return None


def _iter_exception_chain(error: Any) -> list[Any]:
    chain: list[Any] = []
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        current = cause if cause is not None else context
    return chain


def _exception_chain_message(error: Any) -> str:
    return " ".join(str(exc or "").casefold() for exc in _iter_exception_chain(error))


def _message_has_stream_truncation_marker(message: str) -> bool:
    normalized = str(message or "").casefold()
    return any(marker in normalized for marker in _PROVIDER_STREAM_TRUNCATED_MESSAGE_MARKERS)


def _coerce_status_code(value: Any) -> int | None:
    try:
        status_code = int(value)
    except (TypeError, ValueError):
        return None
    if status_code <= 0:
        return None
    return status_code
