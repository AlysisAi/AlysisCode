from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import warnings
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .failure_category import is_provider_throttling_error, is_provider_unavailable_error
from .llm.types import LLMResponse, LLMStreamNoProgressError, LLMUsage
from .logging_redaction import redact_log_text
from .run_provenance import (
    extract_system_fingerprint,
    fingerprint_drift_payload,
    resolve_response_header_allowlist,
    response_fingerprint_payload,
    select_response_headers,
)

_LOGGER = logging.getLogger(__name__)
_MAX_HISTORY = 50
_HISTORY_LOCK = threading.Lock()
_PROVIDER_CALL_HISTORY: deque[dict[str, Any]] = deque(maxlen=_MAX_HISTORY)
_WEB_SEARCH_HISTORY: deque[dict[str, Any]] = deque(maxlen=_MAX_HISTORY)

# Durable, process-wide JSONL sink for provider/web-search telemetry. The in-memory
# deques above evaporate on process exit -- exactly when a crashed run is being
# investigated -- so a registered sink persists each already-redacted summary to disk
# for post-mortem reconstruction. Guarded by its own lock so concurrent provider calls
# append complete lines.
_SINK_LOCK = threading.Lock()
_TELEMETRY_SINK_PATH: Path | None = None
_SINK_WRITE_FAILURES = 0
# Resolved once per process: the allowlist is environment-driven and constant
# for a run, and re-parsing it on every provider call would be pure overhead.
_RESPONSE_HEADER_LOCK = threading.Lock()
_RESPONSE_HEADER_ALLOWLIST: Any = None
_ALYSIS_WEB_SEARCH_TOOL_NAME = "web_search"
_PROVIDER_OPERATION_OVERRIDE: ContextVar[str | None] = ContextVar(
    "provider_operation_override",
    default=None,
)
_PROVIDER_RECORD_COUNT: ContextVar[int] = ContextVar(
    "provider_record_count",
    default=0,
)
_CACHE_USAGE_TOTAL_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_prompt_tokens",
    "input_tokens_uncached",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
)
_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "bearer_token",
    "credential",
    "id_token",
    "password",
    "refresh_token",
    "secret",
    "token",
    "x_api_key",
    "x_goog_api_key",
}
_SENSITIVE_KEY_FRAGMENTS = ("client_secret", "private_key")
_SAFE_REDACTION_KEYS = {"api_key_present", "api_key_source"}
_HIDDEN_PAYLOAD_KEYS = {
    "arguments",
    "body",
    "content",
    "contents",
    "headers",
    "input",
    "messages",
    "parameters",
    "provider_metadata",
    "raw",
    "tool_calls",
    "tools",
}
_ATTEMPT_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_ATTEMPT_USAGE_SOURCES = frozenset({"provider", "estimated", "none"})
# Attempt terminal reasons that name the call-level failure category directly.
# A budget abort keeps the category it always had ("deadline"); timeouts keep
# "no_progress" only when the stream watchdog raised them, as before.
_ATTEMPT_REASON_FAILURE_CATEGORIES = {"cancelled": "cancelled", "budget_abort": "deadline"}
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:sk|tvly|ghp|github_pat|xoxb|xapp|ya29|AIza|key|token)[-_A-Za-z0-9]{10,}\b"
)


def telemetry_clock_ms() -> float:
    return time.monotonic() * 1000.0


@contextmanager
def provider_telemetry_operation(operation: str):
    """Label provider calls made by a non-turn control-plane operation."""
    token = _PROVIDER_OPERATION_OVERRIDE.set(str(operation or "").strip() or None)
    try:
        yield
    finally:
        _PROVIDER_OPERATION_OVERRIDE.reset(token)


def provider_telemetry_record_count() -> int:
    """Return this execution context's provider-record count."""
    return _PROVIDER_RECORD_COUNT.get()


def base_url_host(base_url: str | None) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return ""
    host = (parsed.hostname or "").rstrip(".").casefold()
    return host


def reset_provider_telemetry_for_tests() -> None:
    global _TELEMETRY_SINK_PATH, _SINK_WRITE_FAILURES, _RESPONSE_HEADER_ALLOWLIST
    with _HISTORY_LOCK:
        _PROVIDER_CALL_HISTORY.clear()
        _WEB_SEARCH_HISTORY.clear()
    with _SINK_LOCK:
        _TELEMETRY_SINK_PATH = None
        _SINK_WRITE_FAILURES = 0
    with _RESPONSE_HEADER_LOCK:
        _RESPONSE_HEADER_ALLOWLIST = None


def response_header_allowlist():
    """The process's response-header allowlist, resolved once from the environment."""
    global _RESPONSE_HEADER_ALLOWLIST
    with _RESPONSE_HEADER_LOCK:
        if _RESPONSE_HEADER_ALLOWLIST is None:
            _RESPONSE_HEADER_ALLOWLIST = resolve_response_header_allowlist()
        return _RESPONSE_HEADER_ALLOWLIST


def capture_response_headers(headers: Any) -> dict[str, str]:
    """Select the allowlisted response headers and redact each value.

    Redaction is belt-and-braces: the allowlist is already bounded by a deny
    list that keeps credential-bearing headers out, and both sinks redact the
    serialized line on write. This third pass covers the in-memory history and
    the structured log record, which are not write-path sinks and so are not
    otherwise covered by PR1's boundary.
    """
    selected = select_response_headers(headers, allowlist=response_header_allowlist())
    return {name: redact_log_text(value) for name, value in selected.items()}


def set_provider_telemetry_sink(path: str | Path | None) -> None:
    """Route each recorded provider/web-search summary to a durable JSONL file.

    Process-wide and idempotent (last writer wins); pass ``None`` to disable. The
    persisted payload is already secret-redacted. This is what lets an autonomous
    fix-loop recover the retry/throttle/latency history of a run whose process has
    already exited.
    """
    global _TELEMETRY_SINK_PATH
    with _SINK_LOCK:
        _TELEMETRY_SINK_PATH = Path(path) if path else None


def provider_telemetry_sink_path() -> Path | None:
    with _SINK_LOCK:
        return _TELEMETRY_SINK_PATH


def _append_to_sink(payload: Mapping[str, Any]) -> None:
    global _SINK_WRITE_FAILURES
    with _SINK_LOCK:
        path = _TELEMETRY_SINK_PATH
    if path is None:
        return
    record = {"recorded_at_epoch": round(time.time(), 3), **dict(payload)}
    line = redact_log_text(
        json.dumps(record, ensure_ascii=True, sort_keys=True, default=str) + "\n"
    )
    with _SINK_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:  # noqa: BLE001 - telemetry must not crash a provider call
            # Non-silent: a broken telemetry sink during an autonomous run must be
            # observable rather than quietly capturing nothing.
            _SINK_WRITE_FAILURES += 1
            if _SINK_WRITE_FAILURES == 1:
                warnings.warn(
                    f"provider telemetry sink write to {path} failed "
                    f"({type(exc).__name__}: {exc}); subsequent failures are counted but "
                    "not re-warned.",
                    RuntimeWarning,
                    stacklevel=2,
                )


def provider_call_history_snapshot(*, limit: int | None = None) -> list[dict[str, Any]]:
    with _HISTORY_LOCK:
        items = list(_PROVIDER_CALL_HISTORY)
    if limit is not None:
        items = items[-max(0, int(limit)) :]
    return [copy.deepcopy(item) for item in items]


def web_search_history_snapshot(*, limit: int | None = None) -> list[dict[str, Any]]:
    with _HISTORY_LOCK:
        items = list(_WEB_SEARCH_HISTORY)
    if limit is not None:
        items = items[-max(0, int(limit)) :]
    return [copy.deepcopy(item) for item in items]


def last_provider_call_summary() -> dict[str, Any] | None:
    with _HISTORY_LOCK:
        item = _PROVIDER_CALL_HISTORY[-1] if _PROVIDER_CALL_HISTORY else None
    return copy.deepcopy(item) if item is not None else None


def last_web_search_summary() -> dict[str, Any] | None:
    with _HISTORY_LOCK:
        item = _WEB_SEARCH_HISTORY[-1] if _WEB_SEARCH_HISTORY else None
    return copy.deepcopy(item) if item is not None else None


def provider_cache_effectiveness_snapshot(*, limit: int | None = None) -> dict[str, Any]:
    return _cache_effectiveness_payload(provider_call_history_snapshot(limit=limit))


def provider_cache_diagnostics_snapshot(*, limit: int | None = None) -> dict[str, Any]:
    return _cache_diagnostics_payload(provider_call_history_snapshot(limit=limit))


def provider_token_reconciliation_snapshot(*, limit: int | None = None) -> dict[str, Any]:
    return _token_reconciliation_aggregate_payload(provider_call_history_snapshot(limit=limit))


def provider_fingerprint_drift_snapshot(*, limit: int | None = None) -> dict[str, Any]:
    """Did one requested model come back as more than one thing this run?"""
    return fingerprint_drift_payload(provider_call_history_snapshot(limit=limit))


class ProviderCallTelemetryRecorder:
    """Accumulates one safe provider-call summary.

    The recorder stores only derived fields: counts, booleans, host names, latency,
    and token totals. It never stores request bodies, tool arguments, raw provider
    payloads, or hidden provider metadata.
    """

    def __init__(
        self,
        *,
        provider_key: str | None,
        protocol: str,
        model: str,
        base_url: str,
        stream: bool,
        tools: list[dict[str, Any]] | None,
        web_search_mode: str | None = None,
        web_search_adapter: str | None = None,
        native_web_search: bool = False,
        cache_policy: Mapping[str, Any] | None = None,
        request_plan: Mapping[str, Any] | None = None,
        request_shape: Mapping[str, Any] | None = None,
        token_reconciliation: Mapping[str, Any] | None = None,
        operation: str = "chat",
        sampling: Any | None = None,
    ) -> None:
        self.provider_key = _safe_label(provider_key)
        self.protocol = _safe_label(protocol)
        self.model = _safe_label(model)
        self.base_url_host = base_url_host(base_url)
        self.stream = bool(stream)
        self.operation = _safe_label(_PROVIDER_OPERATION_OVERRIDE.get() or operation)
        self.tool_count = len([tool for tool in tools or [] if isinstance(tool, dict)])
        self.web_search_exposed = tools_expose_web_search(tools) or bool(native_web_search)
        self.web_search_mode = _safe_label(web_search_mode or "off")
        self.web_search_adapter = _safe_label(web_search_adapter or "")
        self.provider_hosted_search = bool(native_web_search)
        self.cache_policy = _safe_cache_policy(cache_policy)
        self.request_plan = _safe_request_plan(request_plan)
        self.request_shape = _safe_request_shape(request_shape)
        self.token_reconciliation = _safe_token_reconciliation(token_reconciliation)
        self.external_search_provider = _external_search_provider(
            exposed=self.web_search_exposed,
            native=native_web_search,
            adapter=web_search_adapter,
        )
        self.web_search_backend_kind = _chat_web_search_backend_kind(
            exposed=self.web_search_exposed,
            native=native_web_search,
            mode=web_search_mode,
        )
        self.sampling = _safe_sampling(sampling)
        self._started_ms = telemetry_clock_ms()
        self._first_text_delta_ms: float | None = None
        self._text_delta_count = 0
        self._first_reasoning_delta_ms: float | None = None
        self._reasoning_delta_count = 0
        self._retry_count = 0
        # Set only by clients that report when an attempt really begins; see
        # note_attempt_started.
        self._retries_started: int | None = None
        self._retry_reasons: list[str] = []
        # Captured from the live HTTP response, so it survives into the error
        # payload too: the request id on a 429 is often the only handle a
        # provider will accept when asked what happened.
        self._response_headers: dict[str, str] = {}
        self._attempts: list[dict[str, Any]] = []
        self._attempt_count = 0
        self._attempt_usage_totals = _usage_payload(None)
        self._attempt_usage_missing_counts = dict.fromkeys(self._attempt_usage_totals, 0)
        self._attempt_usage_missing_count = 0
        self._last_attempt_reason: str | None = None

    def record_attempt(self, attempt: Mapping[str, Any]) -> None:
        """Keep bounded, derived HTTP-attempt evidence, never response content.

        Attempts are nested within the logical call, not additional usage records.
        Byte counts describe decoded HTTP body bytes, not network/billing usage.
        """
        self._attempt_count += 1
        safe: dict[str, Any] = {"attempt": self._attempt_count}
        for key in (
            "status_code",
            "latency_ms",
            "first_byte_ms",
            "first_event_ms",
            "body_bytes_received",
            "byte_chunk_count",
            "line_count",
            "data_line_count",
            "event_count",
            "text_delta_count",
            "reasoning_delta_count",
            "tool_delta_count",
        ):
            value = attempt.get(key)
            safe[key] = max(0, value) if type(value) is int else None
        for key in ("stream", "done_received", "usage_observed", "finish_reason_observed"):
            safe[key] = attempt.get(key) is True
        started_at = attempt.get("request_started_at")
        safe["request_started_at"] = (
            started_at
            if isinstance(started_at, str) and _ATTEMPT_TIMESTAMP_RE.fullmatch(started_at)
            else None
        )
        retry_index = attempt.get("retry_index")
        safe["retry_index"] = retry_index if type(retry_index) is int and retry_index >= 0 else None
        usage_source = attempt.get("usage_source")
        safe["usage_source"] = usage_source if usage_source in _ATTEMPT_USAGE_SOURCES else None
        reason = attempt.get("terminal_reason")
        safe["terminal_reason"] = (
            reason
            if reason
            in {
                "done",
                "response",
                "stream_truncated",
                "http_error",
                "transport_error",
                "timeout_first_event",
                "timeout_stall",
                "budget_abort",
                "cancelled",
                "malformed_event",
                "callback_error",
                "client_error",
                "invalid_response",
                "provider_error",
            }
            else "unknown"
        )
        self._last_attempt_reason = safe["terminal_reason"]
        usage = attempt.get("usage")
        safe["usage"] = _usage_payload(usage if isinstance(usage, LLMUsage) else None)
        for key, value in list(safe["usage"].items()):
            if type(value) is int and value >= 0:
                self._attempt_usage_totals[key] = (self._attempt_usage_totals[key] or 0) + value
            else:
                safe["usage"][key] = None
                self._attempt_usage_missing_counts[key] += 1
        if any(
            safe["usage"][key] is None for key in ("input_tokens_uncached", "completion_tokens")
        ):
            self._attempt_usage_missing_count += 1
        if len(self._attempts) < 64:
            self._attempts.append(safe)

    def wrap_text_delta(
        self,
        callback: Callable[[str], None] | None,
    ) -> Callable[[str], None] | None:
        if not self.stream:
            return callback

        def _wrapped(delta: str) -> None:
            if delta:
                self._text_delta_count += 1
                if self._first_text_delta_ms is None:
                    self._first_text_delta_ms = telemetry_clock_ms()
            if callback is not None:
                callback(delta)

        return _wrapped

    def wrap_reasoning_delta(
        self,
        callback: Callable[[str], None] | None,
    ) -> Callable[[str], None] | None:
        if not self.stream:
            return callback

        def _wrapped(delta: str) -> None:
            if delta:
                self._reasoning_delta_count += 1
                if self._first_reasoning_delta_ms is None:
                    self._first_reasoning_delta_ms = telemetry_clock_ms()
            if callback is not None:
                callback(delta)

        return _wrapped

    def set_cache_policy(self, cache_policy: Mapping[str, Any] | None) -> None:
        self.cache_policy = _safe_cache_policy(cache_policy)

    def set_request_plan(self, request_plan: Mapping[str, Any] | None) -> None:
        self.request_plan = _safe_request_plan(request_plan)

    def set_request_shape(self, request_shape: Mapping[str, Any] | None) -> None:
        self.request_shape = _safe_request_shape(request_shape)

    def set_token_reconciliation(self, token_reconciliation: Mapping[str, Any] | None) -> None:
        self.token_reconciliation = _safe_token_reconciliation(token_reconciliation)

    def set_response_headers(self, headers: Any) -> None:
        """Record the allowlisted provenance headers from a live HTTP response.

        Never raises: an unreadable header mapping yields an empty capture, and
        a provider call must not fail because provenance could not be collected.
        """
        try:
            self._response_headers = capture_response_headers(headers)
        except Exception:  # noqa: BLE001 - provenance must not break a call
            self._response_headers = {}

    def note_attempt_started(self, *, retry_index: int) -> None:
        """Record that a provider-level attempt really began.

        ``on_retry`` fires when a retry is scheduled, before its backoff; a
        retry cancelled during that backoff never ran. For clients that call
        this, ``retry_count`` counts only retries that started.
        """
        try:
            index = max(0, int(retry_index))
        except (TypeError, ValueError):
            return
        self._retries_started = max(self._retries_started or 0, index)

    def on_retry(self, attempt: int, reason: str, _wait_seconds: float) -> None:
        try:
            self._retry_count = max(self._retry_count, int(attempt))
        except (TypeError, ValueError):
            self._retry_count += 1
        normalized_reason = _safe_label(reason)
        if normalized_reason and normalized_reason not in self._retry_reasons:
            self._retry_reasons.append(normalized_reason)

    def run(self, call: Callable[[], LLMResponse]) -> LLMResponse:
        try:
            response = call()
        except (Exception, KeyboardInterrupt) as exc:
            self.record_error(exc)
            raise
        self.record_success(response)
        return response

    def record_success(self, response: LLMResponse) -> None:
        latency_ms = _duration_ms(self._started_ms)
        payload = self._base_payload(
            latency_ms=latency_ms,
            status_category="success",
        )
        payload["usage"] = _usage_payload(response.usage)
        payload["token_reconciliation"] = _token_reconciliation_payload(
            self.token_reconciliation,
            response.usage,
        )
        payload["provider_metadata_present"] = bool(response.provider_metadata)
        payload["tool_call_provider_metadata_count"] = sum(
            1 for tool_call in response.tool_calls if tool_call.provider_metadata
        )
        payload["streaming"] = self._streaming_payload(
            raw=response.raw,
            final_latency_ms=latency_ms,
        )
        payload["response_fingerprint"] = self._response_fingerprint(response)
        payload["web_search"].update(_provider_metadata_web_search_counts(response))
        record_provider_call(payload)

    def record_error(self, exc: BaseException) -> None:
        latency_ms = _duration_ms(self._started_ms)
        failure_category = _status_category(exc)
        if self._last_attempt_reason:
            terminal_reason = self._last_attempt_reason
            if terminal_reason in {"client_error", "callback_error"}:
                failure_category = "client_error"
            elif terminal_reason == "malformed_event":
                failure_category = "invalid_response"
            elif terminal_reason in _ATTEMPT_REASON_FAILURE_CATEGORIES:
                failure_category = _ATTEMPT_REASON_FAILURE_CATEGORIES[terminal_reason]
            elif terminal_reason in {"timeout_first_event", "timeout_stall"} and isinstance(
                exc, LLMStreamNoProgressError
            ):
                failure_category = "no_progress"
        payload = self._base_payload(
            latency_ms=latency_ms,
            status_category=("failed" if self.operation == "cache_keepalive" else failure_category),
        )
        if self.operation == "cache_keepalive":
            payload["failure_category"] = failure_category
        payload["error_type"] = type(exc).__name__
        payload["usage"] = _usage_payload(None)
        payload["token_reconciliation"] = _token_reconciliation_payload(
            self.token_reconciliation,
            None,
        )
        payload["provider_metadata_present"] = False
        payload["tool_call_provider_metadata_count"] = 0
        payload["streaming"] = self._streaming_payload(
            raw=None,
            final_latency_ms=latency_ms,
            error_type=type(exc).__name__ if self.stream else None,
        )
        record_provider_call(payload)

    def _base_payload(self, *, latency_ms: int, status_category: str) -> dict[str, Any]:
        return {
            "kind": "provider_call",
            "operation": self.operation,
            "provider_key": self.provider_key,
            "protocol": self.protocol,
            "model": self.model,
            "base_url_host": self.base_url_host,
            "stream": self.stream,
            "tool_count": self.tool_count,
            "web_search_exposed": self.web_search_exposed,
            "web_search": {
                "web_search_mode": self.web_search_mode,
                "web_search_adapter": self.web_search_adapter,
                "backend_kind": self.web_search_backend_kind,
                "provider_hosted_search": self.provider_hosted_search,
                "external_provider_name": self.external_search_provider,
                "source_count": 0,
                "citation_count": 0,
                "query_count": 0,
                "fallback_occurred": False,
            },
            "cache_policy": copy.deepcopy(self.cache_policy),
            "request_plan": copy.deepcopy(self.request_plan),
            "request_shape": copy.deepcopy(self.request_shape),
            "token_reconciliation": _token_reconciliation_payload(
                self.token_reconciliation,
                None,
            ),
            "retry_count": (
                self._retry_count if self._retries_started is None else self._retries_started
            ),
            "retry_reasons": list(self._retry_reasons),
            "attempts": copy.deepcopy(self._attempts),
            "attempt_count": self._attempt_count,
            "attempts_omitted": self._attempt_count - len(self._attempts),
            "attempt_usage_totals": dict(self._attempt_usage_totals),
            "attempt_usage_missing_count": self._attempt_usage_missing_count,
            "attempt_usage_missing_counts": dict(self._attempt_usage_missing_counts),
            "attempt_usage_scope": "latest_observed_per_http_attempt",
            "status_category": status_category,
            "latency_ms": latency_ms,
            "sampling": copy.deepcopy(self.sampling),
            "response_fingerprint": self._response_fingerprint(),
        }

    def _response_fingerprint(self, response: LLMResponse | None = None) -> dict[str, Any]:
        """Which model actually answered, as far as the wire can tell.

        Recorded on every call, successful or not, and with every field always
        present: a run where ``system_fingerprint`` was never sent is a
        different finding from one where it changed mid-run, and only an
        always-present field distinguishes them.
        """
        response_model = None
        system_fingerprint = None
        if response is not None:
            response_model = getattr(response, "response_model", None)
            system_fingerprint = extract_system_fingerprint(response.raw)
        return response_fingerprint_payload(
            response_model=_safe_label(response_model) or None,
            system_fingerprint=_safe_label(system_fingerprint) or None,
            headers=dict(self._response_headers),
        )

    def _streaming_payload(
        self,
        *,
        raw: Mapping[str, Any] | None,
        final_latency_ms: int,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        if not self.stream:
            return {
                "enabled": False,
                "event_count": 0,
                "text_delta_count": 0,
                "reasoning_delta_count": 0,
                "first_token_latency_ms": None,
                "first_reasoning_latency_ms": None,
                "final_latency_ms": final_latency_ms,
                "stream_error_type": None,
                "unknown_event_count": 0,
                "stream_restart_count": 0,
                "stream_restart_reason": "",
            }
        return {
            "enabled": True,
            "event_count": _stream_event_count(raw),
            "text_delta_count": self._text_delta_count,
            "reasoning_delta_count": self._reasoning_delta_count,
            "first_token_latency_ms": _first_token_latency_ms(
                started_ms=self._started_ms,
                first_delta_ms=self._first_text_delta_ms,
            ),
            "first_reasoning_latency_ms": _first_token_latency_ms(
                started_ms=self._started_ms,
                first_delta_ms=self._first_reasoning_delta_ms,
            ),
            "final_latency_ms": final_latency_ms,
            "stream_error_type": error_type,
            "unknown_event_count": _stream_unknown_event_count(raw),
            "stream_restart_count": _stream_restart_count(raw),
            "stream_restart_reason": _stream_restart_reason(raw),
        }


def record_provider_call(payload: Mapping[str, Any]) -> None:
    safe_payload = redact_telemetry_payload(payload)
    _PROVIDER_RECORD_COUNT.set(_PROVIDER_RECORD_COUNT.get() + 1)
    with _HISTORY_LOCK:
        _PROVIDER_CALL_HISTORY.append(safe_payload)
    _LOGGER.info("provider_call", extra={"alysis_provider_call": safe_payload})
    _append_to_sink(safe_payload)


def record_web_search_call(
    *,
    protocol: str | None,
    provider_key: str | None,
    model: str | None,
    web_search_mode: str,
    web_search_adapter: str,
    provider_hosted_search: bool,
    external_provider_name: str | None,
    source_count: int,
    citation_count: int,
    query_count: int,
    fallback_occurred: bool,
    status_category: str = "success",
) -> None:
    payload = redact_telemetry_payload(
        {
            "kind": "web_search",
            "protocol": _safe_label(protocol),
            "provider_key": _safe_label(provider_key),
            "model": _safe_label(model),
            "web_search_mode": _safe_label(web_search_mode),
            "web_search_adapter": _safe_label(web_search_adapter),
            "provider_hosted_search": bool(provider_hosted_search),
            "external_provider_name": _safe_label(external_provider_name),
            "source_count": max(0, int(source_count)),
            "citation_count": max(0, int(citation_count)),
            "query_count": max(0, int(query_count)),
            "fallback_occurred": bool(fallback_occurred),
            "status_category": _safe_label(status_category),
        }
    )
    with _HISTORY_LOCK:
        _WEB_SEARCH_HISTORY.append(payload)
    _LOGGER.info("web_search_call", extra={"alysis_web_search": payload})
    _append_to_sink(payload)


def redact_telemetry_payload(payload: Any) -> Any:
    return _redact_value(payload)


def tools_expose_web_search(tools: list[dict[str, Any]] | None) -> bool:
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if _tool_name(tool) == _ALYSIS_WEB_SEARCH_TOOL_NAME:
            return True
    return False


def diagnostic_bundle_payload(*, provider_diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    return redact_telemetry_payload(
        {
            "redacted": True,
            "provider_diagnostics": dict(provider_diagnostics),
            "last_provider_call": last_provider_call_summary(),
            "recent_provider_calls": provider_call_history_snapshot(limit=10),
            "cache_effectiveness": provider_cache_effectiveness_snapshot(limit=_MAX_HISTORY),
            "cache_diagnostics": provider_cache_diagnostics_snapshot(limit=_MAX_HISTORY),
            "token_reconciliation": provider_token_reconciliation_snapshot(limit=_MAX_HISTORY),
            "fingerprint_drift": provider_fingerprint_drift_snapshot(limit=_MAX_HISTORY),
            "last_web_search": last_web_search_summary(),
            "recent_web_search_calls": web_search_history_snapshot(limit=10),
            "notes": [
                "Request bodies, tool arguments, secrets, raw provider payloads, and hidden provider metadata are excluded.",
                "Provider call history is process-local and may be empty in a fresh CLI process.",
            ],
        }
    )


def _tool_name(tool: Mapping[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return str(tool.get("name") or "").strip()


def _chat_web_search_backend_kind(
    *,
    exposed: bool,
    native: bool,
    mode: str | None,
) -> str:
    if native:
        return "native/provider-hosted"
    if not exposed:
        return "off"
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "off":
        return "off"
    return "external"


def _external_search_provider(
    *,
    exposed: bool,
    native: bool,
    adapter: str | None,
) -> str | None:
    if native or not exposed:
        return None
    normalized_adapter = str(adapter or "").strip().lower()
    if normalized_adapter and normalized_adapter != "auto":
        return normalized_adapter
    return "alysis_web_search_tool"


def _duration_ms(started_ms: float) -> int:
    return max(0, int(round(telemetry_clock_ms() - started_ms)))


def _provider_route_group_key(call: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        _safe_label(call.get("provider_key")),
        _safe_label(call.get("protocol")),
        _safe_label(call.get("model")),
        _safe_label(call.get("base_url_host")),
        _safe_label(call.get("operation")),
    )


def _cache_diagnostics_payload(calls: list[dict[str, Any]]) -> dict[str, Any]:
    total = _empty_cache_diagnostics_bucket()
    groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        group_key = _provider_route_group_key(call)
        group = groups.setdefault(
            group_key,
            {
                "provider_key": group_key[0],
                "protocol": group_key[1],
                "model": group_key[2],
                "base_url_host": group_key[3],
                "operation": group_key[4],
                **_empty_cache_diagnostics_bucket(),
            },
        )
        _accumulate_cache_diagnostics(total, call)
        _accumulate_cache_diagnostics(group, call)
    return {
        "window_call_count": len(calls),
        "totals": _finalize_cache_diagnostics_bucket(total),
        "by_route": [
            _finalize_cache_diagnostics_bucket(group) for _key, group in sorted(groups.items())
        ],
    }


def _empty_cache_diagnostics_bucket() -> dict[str, Any]:
    return {
        "provider_call_count": 0,
        "cache_policy_call_count": 0,
        "cache_enabled_call_count": 0,
        "cache_field_emitted_call_count": 0,
        "cache_read_call_count": 0,
        "cache_write_call_count": 0,
        "cache_read_usage_sample_count": 0,
        "cache_read_usage_missing_call_count": 0,
        "cache_write_usage_sample_count": 0,
        "cache_write_usage_missing_call_count": 0,
        "cache_fallback_call_count": 0,
        "provider_rejection_or_downgrade_call_count": 0,
        "strategy_counts": {},
        "status_counts": {},
        "fallback_counts": {},
        "provider_rejection_reason_counts": {},
        "cache_risk_reason_counts": {},
        "compaction_trigger_reason_counts": {},
        "tool_schema_share_sample_count": 0,
        "tool_schema_share_total": 0.0,
        "tool_schema_share_max": 0.0,
        "inline_tool_transcript_share_sample_count": 0,
        "inline_tool_transcript_share_total": 0.0,
        "inline_tool_transcript_share_max": 0.0,
        "token_estimate_error_sample_count": 0,
        "sent_token_estimate_error_sample_count": 0,
        "input_estimate_abs_error_tokens_total": 0,
        "input_estimate_error_tokens_total": 0,
        "sent_input_estimate_abs_error_tokens_total": 0,
        "max_input_estimate_abs_error_tokens": 0,
        "cache_field_emitted_rate": None,
        "cache_read_rate": None,
        "cache_write_rate": None,
        "cache_fallback_rate": None,
        "provider_rejection_or_downgrade_rate": None,
        "tool_schema_share_average": None,
        "inline_tool_transcript_share_average": None,
        "mean_input_estimate_abs_error_tokens": None,
        "mean_sent_input_estimate_abs_error_tokens": None,
    }


def _accumulate_cache_diagnostics(bucket: dict[str, Any], call: Mapping[str, Any]) -> None:
    bucket["provider_call_count"] += 1
    cache_policy = call.get("cache_policy")
    request_shape = call.get("request_shape")
    request_plan = call.get("request_plan")
    usage = call.get("usage") if isinstance(call.get("usage"), Mapping) else {}
    reconciliation = (
        call.get("token_reconciliation")
        if isinstance(call.get("token_reconciliation"), Mapping)
        else {}
    )

    if isinstance(cache_policy, Mapping):
        bucket["cache_policy_call_count"] += 1
        if bool(cache_policy.get("enabled")):
            bucket["cache_enabled_call_count"] += 1
        strategy = _safe_label(cache_policy.get("strategy"))
        status = _safe_label(cache_policy.get("status"))
        fallback = _safe_label(cache_policy.get("fallback"))
        if strategy:
            _increment_count(bucket["strategy_counts"], strategy)
        if status:
            _increment_count(bucket["status_counts"], status)
        if fallback:
            bucket["cache_fallback_call_count"] += 1
            _increment_count(bucket["fallback_counts"], fallback)
        if _cache_policy_provider_rejection_or_downgrade(cache_policy):
            bucket["provider_rejection_or_downgrade_call_count"] += 1
            for reason in _cache_policy_rejection_reasons(cache_policy):
                _increment_count(bucket["provider_rejection_reason_counts"], reason)

    if _cache_field_emitted(cache_policy, request_shape):
        bucket["cache_field_emitted_call_count"] += 1
    for kind, observed in (
        ("read", _observed_cache_read_tokens(usage)),
        ("write", _observed_cache_write_tokens(usage)),
    ):
        if observed is None:
            bucket[f"cache_{kind}_usage_missing_call_count"] += 1
        else:
            bucket[f"cache_{kind}_usage_sample_count"] += 1
            if observed > 0:
                bucket[f"cache_{kind}_call_count"] += 1

    if isinstance(request_shape, Mapping):
        for reason in _safe_label_list(request_shape.get("risk_reasons")):
            _increment_count(bucket["cache_risk_reason_counts"], reason)
        for reason in _safe_label_list(request_shape.get("compaction_trigger_reasons")):
            _increment_count(bucket["compaction_trigger_reason_counts"], reason)
        trigger_reason = _safe_label(request_shape.get("compaction_trigger_reason"))
        if trigger_reason:
            _increment_count(bucket["compaction_trigger_reason_counts"], trigger_reason)
        _accumulate_share(
            bucket,
            value=request_shape.get("tool_schema_share"),
            sample_key="tool_schema_share_sample_count",
            total_key="tool_schema_share_total",
            max_key="tool_schema_share_max",
        )
        _accumulate_share(
            bucket,
            value=request_shape.get("inline_tool_transcript_share"),
            sample_key="inline_tool_transcript_share_sample_count",
            total_key="inline_tool_transcript_share_total",
            max_key="inline_tool_transcript_share_max",
        )

    if isinstance(request_plan, Mapping):
        for reason in _safe_label_list(request_plan.get("compaction_trigger_reasons")):
            _increment_count(bucket["compaction_trigger_reason_counts"], reason)
        trigger_reason = _safe_label(request_plan.get("compaction_trigger_reason"))
        if trigger_reason:
            _increment_count(bucket["compaction_trigger_reason_counts"], trigger_reason)

    if isinstance(reconciliation, Mapping):
        abs_error = _optional_non_negative_int(
            reconciliation.get("input_estimate_abs_error_tokens")
        )
        sent_abs_error = _optional_non_negative_int(
            reconciliation.get("sent_input_estimate_abs_error_tokens")
        )
        if abs_error is not None:
            bucket["token_estimate_error_sample_count"] += 1
            bucket["input_estimate_abs_error_tokens_total"] += abs_error
            bucket["input_estimate_error_tokens_total"] += _int_value(
                reconciliation.get("input_estimate_error_tokens")
            )
            bucket["max_input_estimate_abs_error_tokens"] = max(
                bucket["max_input_estimate_abs_error_tokens"],
                abs_error,
            )
        if sent_abs_error is not None:
            bucket["sent_token_estimate_error_sample_count"] += 1
            bucket["sent_input_estimate_abs_error_tokens_total"] += sent_abs_error


def _finalize_cache_diagnostics_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    finalized = copy.deepcopy(bucket)
    provider_calls = _non_negative_int_value(finalized.get("provider_call_count"))
    finalized["cache_field_emitted_rate"] = _rate(
        finalized.get("cache_field_emitted_call_count"),
        provider_calls,
    )
    for kind in ("read", "write"):
        finalized[f"cache_{kind}_rate"] = _rate(
            finalized.get(f"cache_{kind}_call_count"),
            finalized.get(f"cache_{kind}_usage_sample_count"),
        )
    finalized["cache_fallback_rate"] = _rate(
        finalized.get("cache_fallback_call_count"),
        provider_calls,
    )
    finalized["provider_rejection_or_downgrade_rate"] = _rate(
        finalized.get("provider_rejection_or_downgrade_call_count"),
        provider_calls,
    )
    finalized["tool_schema_share_average"] = _average(
        finalized.get("tool_schema_share_total"),
        finalized.get("tool_schema_share_sample_count"),
    )
    finalized["inline_tool_transcript_share_average"] = _average(
        finalized.get("inline_tool_transcript_share_total"),
        finalized.get("inline_tool_transcript_share_sample_count"),
    )
    estimate_samples = _non_negative_int_value(finalized.get("token_estimate_error_sample_count"))
    finalized["mean_input_estimate_abs_error_tokens"] = _average(
        finalized.get("input_estimate_abs_error_tokens_total"),
        estimate_samples,
        digits=2,
    )
    finalized["mean_sent_input_estimate_abs_error_tokens"] = _average(
        finalized.get("sent_input_estimate_abs_error_tokens_total"),
        finalized.get("sent_token_estimate_error_sample_count"),
        digits=2,
    )
    for key in (
        "strategy_counts",
        "status_counts",
        "fallback_counts",
        "provider_rejection_reason_counts",
        "cache_risk_reason_counts",
        "compaction_trigger_reason_counts",
    ):
        finalized[key] = dict(sorted(finalized[key].items()))
    return finalized


def _cache_field_emitted(
    cache_policy: Any,
    request_shape: Any,
) -> bool:
    if isinstance(request_shape, Mapping) and bool(request_shape.get("cache_fields_emitted")):
        return True
    if not isinstance(cache_policy, Mapping):
        return False
    emitted = cache_policy.get("emitted_fields")
    return isinstance(emitted, (list, tuple)) and bool(emitted)


def _cache_policy_provider_rejection_or_downgrade(cache_policy: Mapping[str, Any]) -> bool:
    if _safe_label(cache_policy.get("capability_downgrade")):
        return True
    return bool(_cache_policy_rejection_reasons(cache_policy))


def _cache_policy_rejection_reasons(cache_policy: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in ("fallback", "status"):
        value = _safe_label(cache_policy.get(key))
        if not value:
            continue
        lowered = value.casefold()
        if any(
            marker in lowered
            for marker in (
                "reject",
                "unsupported",
                "stripped",
                "runtime_disabled",
                "provider_disabled",
                "not_supported",
                "failed",
            )
        ):
            reasons.append(value)
    downgrade = _safe_label(cache_policy.get("capability_downgrade"))
    if downgrade:
        reasons.append(downgrade)
    return list(dict.fromkeys(reasons))


def _accumulate_share(
    bucket: dict[str, Any],
    *,
    value: Any,
    sample_key: str,
    total_key: str,
    max_key: str,
) -> None:
    parsed = _optional_non_negative_float(value)
    if parsed is None:
        return
    bucket[sample_key] += 1
    bucket[total_key] += parsed
    bucket[max_key] = max(bucket[max_key], parsed)


def _rate(numerator: Any, denominator: Any) -> float | None:
    denominator_int = _non_negative_int_value(denominator)
    if denominator_int <= 0:
        return None
    return round(_non_negative_int_value(numerator) / denominator_int, 4)


def _average(total: Any, count: Any, *, digits: int = 4) -> float | None:
    count_int = _non_negative_int_value(count)
    if count_int <= 0:
        return None
    try:
        total_float = float(total)
    except (TypeError, ValueError):
        return None
    return round(total_float / count_int, digits)


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_label_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_safe_label(item) for item in value if _safe_label(item)]


def _cache_effectiveness_payload(calls: list[dict[str, Any]]) -> dict[str, Any]:
    total = _empty_cache_effectiveness_bucket()
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        group_key = (
            _safe_label(call.get("provider_key")),
            _safe_label(call.get("protocol")),
            _safe_label(call.get("model")),
            _safe_label(call.get("base_url_host")),
        )
        group = groups.setdefault(
            group_key,
            {
                "provider_key": group_key[0],
                "protocol": group_key[1],
                "model": group_key[2],
                "base_url_host": group_key[3],
                **_empty_cache_effectiveness_bucket(),
            },
        )
        _accumulate_cache_effectiveness(total, call)
        _accumulate_cache_effectiveness(group, call)
    return {
        "window_call_count": len(calls),
        "totals": _finalize_cache_effectiveness_bucket(total),
        "by_provider_model": [
            _finalize_cache_effectiveness_bucket(group) for _key, group in sorted(groups.items())
        ],
    }


def _empty_cache_effectiveness_bucket() -> dict[str, Any]:
    return {
        "provider_call_count": 0,
        "cache_policy_call_count": 0,
        "cache_enabled_call_count": 0,
        "cache_eligible_call_count": 0,
        "cache_used_call_count": 0,
        "cache_read_call_count": 0,
        "cache_write_call_count": 0,
        "cache_read_usage_sample_count": 0,
        "cache_read_usage_missing_call_count": 0,
        "cache_write_usage_sample_count": 0,
        "cache_write_usage_missing_call_count": 0,
        "cache_read_ratio_sample_count": 0,
        "cache_read_ratio_prompt_tokens": 0,
        "cache_read_ratio_cached_tokens": 0,
        "cache_fallback_call_count": 0,
        "cache_miss_call_count": 0,
        "strategy_counts": {},
        "status_counts": {},
        "fallback_counts": {},
        "token_totals": {
            **{field: 0 for field in _CACHE_USAGE_TOTAL_FIELDS},
            "effective_cache_read_input_tokens": 0,
            "effective_cache_write_input_tokens": 0,
        },
        "cache_read_ratio": None,
    }


def _accumulate_cache_effectiveness(bucket: dict[str, Any], call: Mapping[str, Any]) -> None:
    bucket["provider_call_count"] += 1
    cache_policy = call.get("cache_policy")
    usage = call.get("usage") if isinstance(call.get("usage"), Mapping) else {}
    observed_read = _observed_cache_read_tokens(usage)
    observed_write = _observed_cache_write_tokens(usage)
    effective_read = observed_read or 0
    effective_write = observed_write or 0
    for kind, observed in (("read", observed_read), ("write", observed_write)):
        if observed is None:
            bucket[f"cache_{kind}_usage_missing_call_count"] += 1
        else:
            bucket[f"cache_{kind}_usage_sample_count"] += 1
    prompt = _observed_cache_token_count(usage.get("prompt_tokens"))
    if prompt is not None and observed_read is not None and observed_read <= prompt:
        # Only compare counts reported for the same calls. Otherwise missing
        # cache counters would dilute the ratio as if they were reported zero.
        bucket["cache_read_ratio_sample_count"] += 1
        bucket["cache_read_ratio_prompt_tokens"] += prompt
        bucket["cache_read_ratio_cached_tokens"] += observed_read

    if isinstance(cache_policy, Mapping):
        bucket["cache_policy_call_count"] += 1
        enabled = bool(cache_policy.get("enabled"))
        eligible = bool(cache_policy.get("eligible"))
        used = bool(cache_policy.get("used"))
        fallback = _safe_label(cache_policy.get("fallback"))
        strategy = _safe_label(cache_policy.get("strategy"))
        status = _safe_label(cache_policy.get("status"))
        if enabled:
            bucket["cache_enabled_call_count"] += 1
        if eligible:
            bucket["cache_eligible_call_count"] += 1
        if used:
            bucket["cache_used_call_count"] += 1
        if fallback:
            bucket["cache_fallback_call_count"] += 1
            _increment_count(bucket["fallback_counts"], fallback)
        if strategy:
            _increment_count(bucket["strategy_counts"], strategy)
        if status:
            _increment_count(bucket["status_counts"], status)
        if enabled and not used and observed_read == 0 and effective_write == 0:
            bucket["cache_miss_call_count"] += 1

    if effective_read > 0:
        bucket["cache_read_call_count"] += 1
    if effective_write > 0:
        bucket["cache_write_call_count"] += 1

    token_totals = bucket["token_totals"]
    for field in _CACHE_USAGE_TOTAL_FIELDS:
        token_totals[field] += _non_negative_int_value(usage.get(field))
    token_totals["effective_cache_read_input_tokens"] += effective_read
    token_totals["effective_cache_write_input_tokens"] += effective_write


def _finalize_cache_effectiveness_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    finalized = copy.deepcopy(bucket)
    prompt_tokens = finalized["cache_read_ratio_prompt_tokens"]
    cached_tokens = finalized["cache_read_ratio_cached_tokens"]
    finalized["cache_read_ratio"] = (
        round(cached_tokens / prompt_tokens, 4) if prompt_tokens > 0 else None
    )
    finalized["strategy_counts"] = dict(sorted(finalized["strategy_counts"].items()))
    finalized["status_counts"] = dict(sorted(finalized["status_counts"].items()))
    finalized["fallback_counts"] = dict(sorted(finalized["fallback_counts"].items()))
    return finalized


def _observed_cache_token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _observed_cache_read_tokens(usage: Mapping[str, Any]) -> int | None:
    for field in ("cache_read_input_tokens", "cached_prompt_tokens"):
        value = _observed_cache_token_count(usage.get(field))
        if value is not None:
            return value
    return None


def _observed_cache_write_tokens(usage: Mapping[str, Any]) -> int | None:
    total = _observed_cache_token_count(usage.get("cache_creation_input_tokens"))
    if total is not None:
        return total
    parts = [
        _observed_cache_token_count(usage.get(field))
        for field in ("cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens")
    ]
    reported = [value for value in parts if value is not None]
    # A positive part proves a write; a zero part alone does not prove that an
    # unreported part was also zero. Totals remain observed lower bounds.
    return sum(reported) if len(reported) == len(parts) or any(reported) else None


def _non_negative_int_value(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _increment_count(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _token_reconciliation_aggregate_payload(calls: list[dict[str, Any]]) -> dict[str, Any]:
    total = _empty_token_reconciliation_bucket()
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        group_key = (
            _safe_label(call.get("provider_key")),
            _safe_label(call.get("protocol")),
            _safe_label(call.get("model")),
            _safe_label(call.get("base_url_host")),
        )
        group = groups.setdefault(
            group_key,
            {
                "provider_key": group_key[0],
                "protocol": group_key[1],
                "model": group_key[2],
                "base_url_host": group_key[3],
                **_empty_token_reconciliation_bucket(),
            },
        )
        reconciliation = call.get("token_reconciliation")
        if not isinstance(reconciliation, Mapping):
            continue
        _accumulate_token_reconciliation(total, reconciliation)
        _accumulate_token_reconciliation(group, reconciliation)
    return {
        "window_call_count": len(calls),
        "totals": _finalize_token_reconciliation_bucket(total),
        "by_provider_model": [
            _finalize_token_reconciliation_bucket(group) for _key, group in sorted(groups.items())
        ],
    }


def _empty_token_reconciliation_bucket() -> dict[str, Any]:
    return {
        "reconciliation_call_count": 0,
        "reported_prompt_call_count": 0,
        "undercount_call_count": 0,
        "overcount_call_count": 0,
        "exact_count_call_count": 0,
        "input_estimate_tokens_total": 0,
        "sent_input_estimate_tokens_total": 0,
        "reported_input_estimate_tokens_total": 0,
        "reported_prompt_tokens_total": 0,
        "cached_prompt_tokens_total": 0,
        "input_tokens_uncached_total": 0,
        "input_estimate_error_tokens_total": 0,
        "input_estimate_abs_error_tokens_total": 0,
        "max_abs_error_tokens": 0,
        "mean_abs_error_tokens": None,
        "reported_to_estimate_ratio": None,
        "estimator_counts": {},
        "estimate_basis_counts": {},
    }


def _accumulate_token_reconciliation(
    bucket: dict[str, Any],
    reconciliation: Mapping[str, Any],
) -> None:
    input_estimate = _optional_non_negative_int(reconciliation.get("input_estimate_tokens"))
    if input_estimate is None:
        return
    bucket["reconciliation_call_count"] += 1
    bucket["input_estimate_tokens_total"] += input_estimate
    bucket["sent_input_estimate_tokens_total"] += _non_negative_int_value(
        reconciliation.get("sent_input_estimate_tokens")
    )
    estimator = _safe_label(reconciliation.get("estimator"))
    if estimator:
        _increment_count(bucket["estimator_counts"], estimator)
    estimate_basis = _safe_label(reconciliation.get("estimate_basis"))
    if estimate_basis:
        _increment_count(bucket["estimate_basis_counts"], estimate_basis)

    reported = _optional_non_negative_int(reconciliation.get("reported_prompt_tokens"))
    if reported is None:
        return
    bucket["reported_prompt_call_count"] += 1
    bucket["reported_input_estimate_tokens_total"] += input_estimate
    bucket["reported_prompt_tokens_total"] += reported
    bucket["cached_prompt_tokens_total"] += _non_negative_int_value(
        reconciliation.get("cached_prompt_tokens")
    )
    bucket["input_tokens_uncached_total"] += _non_negative_int_value(
        reconciliation.get("input_tokens_uncached")
    )
    error = reported - input_estimate
    abs_error = abs(error)
    bucket["input_estimate_error_tokens_total"] += error
    bucket["input_estimate_abs_error_tokens_total"] += abs_error
    bucket["max_abs_error_tokens"] = max(bucket["max_abs_error_tokens"], abs_error)
    if error > 0:
        bucket["undercount_call_count"] += 1
    elif error < 0:
        bucket["overcount_call_count"] += 1
    else:
        bucket["exact_count_call_count"] += 1


def _finalize_token_reconciliation_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    finalized = copy.deepcopy(bucket)
    reported_calls = _non_negative_int_value(finalized.get("reported_prompt_call_count"))
    reported_estimate_total = _non_negative_int_value(
        finalized.get("reported_input_estimate_tokens_total")
    )
    reported_total = _non_negative_int_value(finalized.get("reported_prompt_tokens_total"))
    abs_error_total = _non_negative_int_value(
        finalized.get("input_estimate_abs_error_tokens_total")
    )
    finalized["mean_abs_error_tokens"] = (
        round(abs_error_total / reported_calls, 2) if reported_calls > 0 else None
    )
    finalized["reported_to_estimate_ratio"] = (
        round(reported_total / reported_estimate_total, 4) if reported_estimate_total > 0 else None
    )
    finalized["estimator_counts"] = dict(sorted(finalized["estimator_counts"].items()))
    finalized["estimate_basis_counts"] = dict(sorted(finalized["estimate_basis_counts"].items()))
    return finalized


def _token_reconciliation_payload(
    reconciliation: Mapping[str, Any] | None,
    usage: LLMUsage | None,
) -> dict[str, Any] | None:
    safe = _safe_token_reconciliation(reconciliation)
    if safe is None and usage is None:
        return None
    payload = copy.deepcopy(safe or {})
    reported_prompt_tokens = usage.prompt_tokens if usage is not None else None
    cached_prompt_tokens = usage.cached_prompt_tokens if usage is not None else None
    input_tokens_uncached = usage.input_tokens_uncached if usage is not None else None
    cache_read_input_tokens = usage.cache_read_input_tokens if usage is not None else None
    payload["reported_prompt_tokens"] = reported_prompt_tokens
    payload["cached_prompt_tokens"] = cached_prompt_tokens
    payload["input_tokens_uncached"] = input_tokens_uncached
    payload["cache_read_input_tokens"] = cache_read_input_tokens
    input_estimate = _optional_non_negative_int(payload.get("input_estimate_tokens"))
    sent_input_estimate = _optional_non_negative_int(payload.get("sent_input_estimate_tokens"))
    reported = _optional_non_negative_int(reported_prompt_tokens)
    if input_estimate is not None and reported is not None:
        error = reported - input_estimate
        payload["input_estimate_error_tokens"] = error
        payload["input_estimate_abs_error_tokens"] = abs(error)
        payload["input_estimate_error_ratio"] = (
            round(reported / input_estimate, 4) if input_estimate > 0 else None
        )
    if sent_input_estimate is not None and reported is not None:
        error = reported - sent_input_estimate
        payload["sent_input_estimate_error_tokens"] = error
        payload["sent_input_estimate_abs_error_tokens"] = abs(error)
        payload["sent_input_estimate_error_ratio"] = (
            round(reported / sent_input_estimate, 4) if sent_input_estimate > 0 else None
        )
    return payload or None


def _safe_token_reconciliation(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    safe: dict[str, Any] = {}
    for key in ("estimator", "estimate_basis", "input_mode"):
        value = payload.get(key)
        if value is not None:
            safe[key] = _safe_label(str(value))
    for key in ("input_estimate_tokens", "sent_input_estimate_tokens"):
        value = _optional_non_negative_int(payload.get(key))
        if value is not None:
            safe[key] = value
    return safe or None


def _optional_non_negative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _optional_non_negative_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _safe_sampling(sampling: Any) -> dict[str, Any]:
    """Normalize a ``SamplingSettings`` (or a mapping, or nothing) into a record.

    Unlike the other ``_safe_*`` helpers this never returns ``None``. "No
    sampling controls were configured" is precisely the finding when two runs
    of one build diverge, so it belongs in every provider-call row as an
    explicit ``configured: false`` rather than as an absent key that a later
    reader has to interpret.
    """
    payload: Any = None
    if sampling is not None:
        getter = getattr(sampling, "telemetry_payload", None)
        if callable(getter):
            try:
                payload = getter()
            except Exception:  # noqa: BLE001 - telemetry must not break a call
                payload = None
        elif isinstance(sampling, Mapping):
            payload = dict(sampling)
    if not isinstance(payload, Mapping):
        payload = {
            "configured": False,
            "temperature": None,
            "top_p": None,
            "seed": None,
            "sources": {},
        }
    return dict(payload)


def _safe_cache_policy(policy: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(policy, Mapping):
        return None
    safe: dict[str, Any] = {}
    for key in (
        "strategy",
        "mode",
        "ttl",
        "retention",
        "status",
        "fallback",
        "capability_downgrade",
        "source",
        "capability_source",
        "usage_schema",
        "refresh_reason",
        "delete_status",
        "prompt_cache_key_hash",
    ):
        value = policy.get(key)
        if value is not None:
            safe[key] = _safe_label(str(value))
    for key in (
        "allowed_fields",
        "emitted_fields",
        "trusted_usage_fields",
        "warnings",
        "disabled_fields",
        "runtime_disabled_fields",
        "eviction_reasons",
    ):
        value = policy.get(key)
        if isinstance(value, (list, tuple)):
            safe[key] = [_safe_label(str(item)) for item in value if str(item).strip()]
    for key in (
        "enabled",
        "eligible",
        "used",
        "emits_request_fields",
        "explicit_block_used",
        "top_level_cache_control_used",
    ):
        if key in policy:
            safe[key] = bool(policy.get(key))
    for key in (
        "min_tokens",
        "cacheable_prefix_estimated_tokens",
        "explicit_block_count",
        "entry_count",
        "max_entries",
        "ttl_seconds",
        "refresh_margin_seconds",
        "refresh_in_seconds",
        "expires_in_seconds",
        "cache_age_seconds",
        "cached_content_estimated_tokens",
        "created_entry_count",
        "reused_entry_count",
        "evicted_entry_count",
        "delete_attempt_count",
        "delete_success_count",
        "delete_failure_count",
    ):
        value = policy.get(key)
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            safe[key] = number
    return safe or None


def _safe_request_shape(shape: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(shape, Mapping):
        return None
    safe: dict[str, Any] = {}
    for key in ("input_mode", "cache_strategy", "cache_status", "compaction_trigger_reason"):
        value = shape.get(key)
        if value is not None:
            safe[key] = _safe_label(str(value))
    for key in (
        "cache_enabled",
        "cache_eligible",
        "cache_used",
        "cache_fields_emitted",
        "top_level_cache_control_present",
        "cached_content_attached",
        "affinity_field_emitted",
        "cacheable_prefix_present",
    ):
        if key in shape:
            safe[key] = bool(shape.get(key))
    for key in (
        "schema_version",
        "message_count",
        "tool_count",
        "system_message_count",
        "developer_message_count",
        "user_message_count",
        "assistant_message_count",
        "tool_message_count",
        "tool_call_message_count",
        "content_block_count",
        "cache_control_block_count",
        "explicit_cache_control_block_count",
        "cacheable_prefix_message_count",
        "cacheable_prefix_estimated_tokens",
        "cacheable_surface_estimated_tokens",
        "min_cacheable_tokens",
        "total_estimated_tokens",
    ):
        value = _optional_non_negative_int(shape.get(key))
        if value is not None:
            safe[key] = value
    for key in ("tool_schema_share", "inline_tool_transcript_share"):
        value = _optional_non_negative_float(shape.get(key))
        if value is not None:
            safe[key] = round(value, 4)
    for key in ("emitted_cache_fields", "risk_reasons", "compaction_trigger_reasons"):
        value = shape.get(key)
        if isinstance(value, (list, tuple)):
            safe[key] = [_safe_label(str(item)) for item in value if str(item).strip()]
    breakdown = shape.get("token_breakdown")
    if isinstance(breakdown, Mapping):
        safe_breakdown: dict[str, int] = {}
        for key in (
            "bootstrap_prompt_tokens",
            "tool_schema_tokens",
            "live_conversation_history_tokens",
            "inline_tool_transcript_tokens",
            "memory_summary_tokens",
            "pins_tokens",
            "total_tokens",
        ):
            value = _optional_non_negative_int(breakdown.get(key))
            if value is not None:
                safe_breakdown[key] = value
        if safe_breakdown:
            safe["token_breakdown"] = safe_breakdown
    return safe or None


def _safe_request_plan(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(plan, Mapping):
        return None
    safe: dict[str, Any] = {}
    for key, allowed in (
        (
            "wire_tool_choice",
            {"auto", "required", "none", "function", "custom", "tool", "any", "omitted", "other"},
        ),
        (
            "wire_reasoning_effort",
            {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
                "ultra",
                "omitted",
                "other",
            },
        ),
    ):
        value = plan.get(key)
        if isinstance(value, str) and value in allowed:
            safe[key] = value
    for key in ("wire_history_sha256", "wire_instructions_sha256", "wire_tools_sha256"):
        value = plan.get(key)
        if (
            isinstance(value, str)
            and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)
        ):
            safe[key] = value
    for key in (
        "wire_previous_history_prefix_preserved",
        "wire_previous_instructions_unchanged",
        "wire_previous_tools_unchanged",
    ):
        if type(plan.get(key)) is bool:
            safe[key] = plan[key]
    for key in (
        "wire_schema_version",
        "wire_history_bytes",
        "wire_history_items",
        "wire_instructions_bytes",
        "wire_tools_bytes",
        "wire_previous_history_items",
        "wire_shared_prefix_items",
        "wire_shared_prefix_bytes",
        "wire_image_parts",
        "wire_media_parts",
        "wire_text_parts",
        "wire_tool_count",
    ):
        value = plan.get(key)
        if type(value) is int and value >= 0:
            safe[key] = value
    for key in (
        "input_mode",
        "status",
        "fallback",
        "continuation_strategy",
        "cache_strategy",
        "cache_mode",
        "cacheable_prefix_hash",
        "request_messages_signature",
        "tool_schema_hash",
        "compaction_trigger_reason",
    ):
        value = plan.get(key)
        if value is not None:
            safe[key] = _safe_label(str(value))
    for key in ("compaction_trigger_reasons",):
        value = plan.get(key)
        if isinstance(value, (list, tuple)):
            safe[key] = [_safe_label(str(item)) for item in value if str(item).strip()]
    for key in ("previous_response_id_used", "fallback_used", "stream"):
        if key in plan:
            safe[key] = bool(plan.get(key))
    for key in (
        "schema_version",
        "message_count",
        "request_message_count",
        "tool_count",
        "stable_prefix_message_count",
        "dynamic_suffix_message_count",
        "provider_metadata_message_count",
        "stable_prefix_estimated_tokens",
        "dynamic_suffix_estimated_tokens",
        "tool_schema_tokens",
        "total_estimated_tokens",
        "serialized_request_estimate_tokens",
        "sent_serialized_request_estimate_tokens",
        "full_input_item_count",
        "sent_input_item_count",
        "continuation_anchor_index",
        "resent_stable_instruction_count",
    ):
        value = plan.get(key)
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            safe[key] = number
    return safe or None


def _first_token_latency_ms(
    *,
    started_ms: float,
    first_delta_ms: float | None,
) -> int | None:
    if first_delta_ms is None:
        return None
    return max(0, int(round(first_delta_ms - started_ms)))


def _usage_payload(usage: LLMUsage | None) -> dict[str, int | None]:
    return {
        "prompt_tokens": usage.prompt_tokens if usage is not None else None,
        "completion_tokens": usage.completion_tokens if usage is not None else None,
        "total_tokens": usage.total_tokens if usage is not None else None,
        "cached_prompt_tokens": usage.cached_prompt_tokens if usage is not None else None,
        "input_tokens_uncached": usage.input_tokens_uncached if usage is not None else None,
        "cache_read_input_tokens": usage.cache_read_input_tokens if usage is not None else None,
        "cache_creation_input_tokens": (
            usage.cache_creation_input_tokens if usage is not None else None
        ),
        "cache_creation_5m_input_tokens": (
            usage.cache_creation_5m_input_tokens if usage is not None else None
        ),
        "cache_creation_1h_input_tokens": (
            usage.cache_creation_1h_input_tokens if usage is not None else None
        ),
        "reasoning_tokens": usage.reasoning_tokens if usage is not None else None,
    }


def _status_category(exc: BaseException) -> str:
    if is_provider_throttling_error(exc):
        return "rate_limited"
    if is_provider_unavailable_error(exc):
        return "provider_unavailable"
    lowered = str(exc).lower()
    if any(token in lowered for token in ("timeout", "connect", "network", "dns")):
        return "network_error"
    return "provider_error"


def _stream_event_count(raw: Mapping[str, Any] | None) -> int:
    if not isinstance(raw, Mapping):
        return 0
    direct = raw.get("events")
    if isinstance(direct, int):
        return max(0, direct)
    stream_metadata = raw.get("stream_metadata")
    if isinstance(stream_metadata, Mapping):
        events = stream_metadata.get("events")
        if isinstance(events, int):
            return max(0, events)
    gemini_metadata = raw.get("streamMetadata")
    if isinstance(gemini_metadata, Mapping):
        chunks = gemini_metadata.get("chunks")
        if isinstance(chunks, int):
            return max(0, chunks)
    return 0


def _stream_unknown_event_count(raw: Mapping[str, Any] | None) -> int:
    if not isinstance(raw, Mapping):
        return 0
    stream_metadata = raw.get("stream_metadata")
    if isinstance(stream_metadata, Mapping):
        unknown = stream_metadata.get("unknown_events")
        if isinstance(unknown, list):
            return len(unknown)
    gemini_metadata = raw.get("streamMetadata")
    if isinstance(gemini_metadata, Mapping):
        unknown_chunks = gemini_metadata.get("unknown_chunks")
        if isinstance(unknown_chunks, list):
            return len(unknown_chunks)
    return 0


def _stream_restart_count(raw: Mapping[str, Any] | None) -> int:
    if not isinstance(raw, Mapping):
        return 0
    try:
        return max(0, int(raw.get("stream_restart_count") or 0))
    except (TypeError, ValueError):
        return 0


def _stream_restart_reason(raw: Mapping[str, Any] | None) -> str:
    if not isinstance(raw, Mapping):
        return ""
    return _safe_label(raw.get("stream_restart_reason"))


def _provider_metadata_web_search_counts(response: LLMResponse) -> dict[str, int]:
    metadata = response.provider_metadata if isinstance(response.provider_metadata, Mapping) else {}
    return {
        "source_count": _count_list_keys(metadata, {"sources", "groundingChunks"}),
        "citation_count": _count_list_keys(
            metadata,
            {"citations", "groundingSupports", "citationMetadata"},
        ),
        "query_count": _count_list_keys(metadata, {"queries", "webSearchQueries"}),
    }


def _count_list_keys(value: Any, keys: set[str]) -> int:
    if isinstance(value, Mapping):
        count = 0
        for key, item in value.items():
            if str(key) in keys and isinstance(item, list):
                count += len(item)
            else:
                count += _count_list_keys(item, keys)
        return count
    if isinstance(value, list):
        return sum(_count_list_keys(item, keys) for item in value)
    return 0


def _safe_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _redact_string(text)


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    normalized_key = str(key or "").strip().casefold().replace("-", "_")
    if normalized_key:
        if normalized_key not in _SAFE_REDACTION_KEYS and (
            normalized_key in _SENSITIVE_EXACT_KEYS
            or normalized_key.endswith("_api_key")
            or any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS)
        ):
            return "[redacted]"
        if normalized_key in _HIDDEN_PAYLOAD_KEYS:
            return "[omitted]"
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    try:
        json.dumps(value)
    except TypeError:
        return _redact_string(repr(value))
    return value


def _redact_string(value: str) -> str:
    text = str(value)
    if not text:
        return text
    return _SECRET_VALUE_RE.sub("[redacted]", text)
