from __future__ import annotations

import copy
import json
import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..cancellation import CooperativeCancellationError
from ..error_text import sanitize_error_text_for_output
from ..execution_deadline import DeadlineExhausted
from ..failure_category import provider_unavailable_retry_reason
from ..provider_telemetry import ProviderCallTelemetryRecorder
from ..provider_url import known_provider_key_from_base_url
from ..reasoning_contracts import (
    ALWAYS_ON,
    OFF_EXPLICIT,
    OPTIONAL,
    WIRE_CHAT_TEMPLATE_ENABLE_THINKING,
    WIRE_REASONING_EFFORT,
    WIRE_REASONING_ENABLED,
    WIRE_THINKING_LEVEL,
    WIRE_THINKING_TYPE,
    reasoning_contract_for,
)
from ..request_estimation import estimate_provider_payload_tokens
from ..run_provenance import active_sampling_settings, apply_sampling_to_payload
from ..transport_retry import RETRY_REASON_TRANSPORT_CONNECTION_DROP
from .base import RequiredToolCallingUnsupportedError, required_tool_names_for_client
from .cache_capabilities import (
    CACHE_CONTROL_FIELD,
    OPENROUTER_SESSION_ID_FIELD,
    OPENROUTER_SESSION_ID_HEADER_FIELD,
    PROMPT_CACHE_KEY_FIELD,
    PROMPT_CACHE_RETENTION_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
)
from .cache_control_blocks import (
    apply_openai_compatible_cache_control_breakpoint,
    count_cache_control_blocks,
    strip_cache_control_blocks,
)
from .cache_policy import merge_cache_policy_metadata
from .cancellable_http import cancellation_aware_httpx_client, provider_httpx_client
from .hosted_request_limits import hosted_chat_capacity_ratio, validate_hosted_chat_capacity
from .http_cancellation import cancellable_httpx_request, raise_if_cancelled
from .metadata import (
    DEEPSEEK_REASONING_CONTENT_KEY as _DEEPSEEK_REASONING_CONTENT_KEY,
)
from .metadata import MISTRAL_CONTENT_CHUNKS_KEY as _MISTRAL_CONTENT_CHUNKS_KEY
from .metadata import MISTRAL_PROVIDER_METADATA_KEY as _MISTRAL_PROVIDER_KEY
from .metadata import (
    OPENAI_COMPAT_REASONING_METADATA_KEY,
    PROVIDER_METADATA_KEY,
    QWEN_PROVIDER_METADATA_KEY,
    ProviderRouteIdentity,
    build_provider_route_identity,
    canonicalize_extra_headers,
    credential_scope_fingerprint,
    endpoint_descriptor,
    endpoint_label,
    gate_messages_for_provider_route,
    merge_canonical_headers,
    stamp_response_for_route,
    strip_provider_metadata_from_message,
)
from .metadata import (
    OPENROUTER_REASONING_DETAILS_KEY as _OPENROUTER_REASONING_DETAILS_KEY,
)
from .metadata import (
    OPENROUTER_REASONING_KEY as _OPENROUTER_REASONING_KEY,
)
from .metadata import (
    TOOL_CALL_PROVIDER_METADATA_KEY as _TOOL_CALL_PROVIDER_METADATA_KEY,
)
from .metadata import (
    assistant_message_from_response as assistant_message_from_response,
)
from .metadata import (
    attach_provider_metadata_to_assistant_message as attach_provider_metadata_to_assistant_message,
)
from .metadata import (
    merge_provider_metadata as _merge_provider_metadata,
)
from .protocols import (
    OPENAI_COMPAT_PROTOCOL,
    ReasoningTraceCapability,
    reasoning_trace_callback,
    resolve_reasoning_trace_capability,
    validate_reasoning_trace_adapter_for_protocol,
)
from .provider_limits import (
    DEFAULT_PROVIDER_CONCURRENCY_CAPS,
    DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS,
    ProviderRetrySettings,
    best_effort_provider_key,
    mark_provider_call_non_retryable,
    run_provider_limited_call,
)
from .reasoning_trace import CompatibleReasoningTrace
from .request_plan import LLMRequestPlan, RequestCachePlan, WireRequestDiagnostics
from .request_shape import build_request_shape_report
from .streaming import (
    STREAM_ASSEMBLY_AUTO,
    STREAM_ASSEMBLY_CONCAT,
    STREAM_ASSEMBLY_CUMULATIVE,
    STREAM_ASSEMBLY_SETTINGS,
    decode_text_chunks,
    iter_lines_from_text_chunks,
)
from .temperature_compat import documented_temperature_omit_reason
from .types import (
    InputTokenCount,
    LLMError,
    LLMResponse,
    LLMStreamNoProgressError,
    LLMUsage,
    ReasoningOutput,
    ToolCall,
    UsageConfidence,
    UsageContract,
    UsageSource,
)
from .usage_normalization import parse_compatible_usage

_TEXT_LIKE_CONTENT_PART_TYPES = {"text", "output_text"}
_DEEPSEEK_PROVIDER_KEY = "deepseek"
_OPENROUTER_PROVIDER_KEY = "openrouter"
_QWEN_PROVIDER_KEY = QWEN_PROVIDER_METADATA_KEY
_GEMINI_PROVIDER_KEY = "gemini"
_MOONSHOT_PROVIDER_KEY = "moonshot"
_NVIDIA_PROVIDER_KEY = "nvidia"
_TOGETHER_PROVIDER_KEY = "together"
_GEMINI_EXTRA_CONTENT_KEY = "extra_content"
_OPENAI_STYLE_REASONING_EFFORT_PROVIDERS = frozenset({"openai", "azure", "mistral"})
_REASONING_PROVIDER_BY_ADAPTER: dict[str, str] = {
    "deepseek_reasoning": _DEEPSEEK_PROVIDER_KEY,
    "openrouter_reasoning": _OPENROUTER_PROVIDER_KEY,
    "dashscope_thinking": _QWEN_PROVIDER_KEY,
    "mistral_thinking": _MISTRAL_PROVIDER_KEY,
    "moonshot_reasoning": _MOONSHOT_PROVIDER_KEY,
    "nvidia_reasoning": _NVIDIA_PROVIDER_KEY,
    "glm_reasoning": "zai_coding_plan",
}
# Hosts whose reasoning wire is the DeepSeek dialect, applied when the
# configured adapter is "auto" (an explicit adapter always wins). Xiaomi MiMo
# documents ``thinking: {"type": "enabled" | "disabled"}`` as its thinking
# switch and requires the ``reasoning_content`` of an assistant turn that made
# tool calls to be sent back on later requests ("Deep Thinking", mimo.mi.com).
_DEEPSEEK_REASONING_DIALECT_HOSTS = frozenset(
    {"api.xiaomimimo.com", "token-plan-ams.xiaomimimo.com"}
)
_GEMINI_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high"})
_DEFAULT_ACCEPT_ENCODING = "identity"
_DEFAULT_CONNECT_TIMEOUT_S = 2.0
_LOGGER = logging.getLogger(__name__)


class _StreamProtocolError(LLMError):
    """Invalid response framing/content, distinct from interrupted transport."""


class _StreamClientError(LLMError):
    """Local parsing/callback failure; retrying a provider cannot repair it."""


def _utc_timestamp_ms() -> str:
    """Wall-clock UTC time as ISO-8601 with milliseconds (``...T09:15:30.123Z``)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _HTTPAttempt:
    def __init__(self, *, stream: bool) -> None:
        self.started = monotonic()
        self.values: dict[str, Any] = {
            "stream": stream,
            # Wall clock, so an attempt lines up with provider and harness logs;
            # the *_ms fields stay monotonic offsets from this instant.
            "request_started_at": _utc_timestamp_ms(),
            "usage_source": "none",
        }
        if stream:
            self.values.update(
                dict.fromkeys(
                    (
                        "body_bytes_received",
                        "byte_chunk_count",
                        "line_count",
                        "data_line_count",
                        "event_count",
                        "text_delta_count",
                        "reasoning_delta_count",
                        "tool_delta_count",
                    ),
                    0,
                )
            )

    def elapsed_ms(self) -> int:
        return max(0, round((monotonic() - self.started) * 1000))

    def increment(self, key: str, amount: int = 1) -> None:
        self.values[key] = self.values.get(key, 0) + amount

    def first(self, key: str) -> None:
        if key not in self.values:
            self.values[key] = self.elapsed_ms()


def _stream_events(lines: Iterator[str], attempt: _HTTPAttempt) -> Iterator[dict[str, Any] | None]:
    """Decode data events, including multiline JSON and legacy unseparated events.

    Complete JSON on one data line remains accepted without an intervening blank
    line. Incomplete JSON is accumulated until the SSE frame ends; it must never
    be silently discarded, even if a later frame is a valid DONE marker.
    """
    pending: list[str] = []

    def invalid() -> _StreamProtocolError:
        attempt.values["terminal_reason"] = "malformed_event"
        error = _StreamProtocolError("LLM stream returned a malformed JSON event")
        mark_provider_call_non_retryable(error)
        return error

    for line in lines:
        attempt.increment("line_count")
        if not line:
            if pending:
                raise invalid()
            continue
        if not line.startswith("data:"):
            continue
        attempt.increment("data_line_count")
        data = line[5:].removeprefix(" ")
        if not data.strip() and not pending:
            continue
        if data.strip() == "[DONE]":
            if pending:
                raise invalid()
            attempt.values["done_received"] = True
            yield None
            return
        pending.append(data)
        try:
            event = json.loads("\n".join(pending))
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            raise invalid()
        pending.clear()
        attempt.increment("event_count")
        attempt.first("first_event_ms")
        yield event
    # An unfinished event at EOF may be a transport truncation. The caller
    # requires DONE and records that separately from a malformed complete frame.


_TEMPERATURE_DEFAULT_VALUE = 1.0
_TEMPERATURE_COMPAT_MODE_DEFAULT = "default_temperature"
_TEMPERATURE_COMPAT_MODE_OMIT = "omit_temperature"
_TEMPERATURE_COMPAT_MODES = {
    _TEMPERATURE_COMPAT_MODE_DEFAULT,
    _TEMPERATURE_COMPAT_MODE_OMIT,
}
_TEMPERATURE_UNSUPPORTED_STATUS_CODES = {400, 422}
_TEMPERATURE_UNSUPPORTED_TOKENS = (
    "allowed",
    "greater than",
    "invalid",
    "unsupported",
    "not support",
    "not supported",
    "not allowed",
    "out of range",
    "range",
    "deprecated",
    "only the default",
    "cannot be set",
    "must be omitted",
)
_CACHE_PARAM_UNSUPPORTED_STATUS_CODES = {400, 422}
_CACHE_PARAM_UNSUPPORTED_TOKENS = (
    "invalid",
    "unsupported",
    "not support",
    "not supported",
    "not allowed",
    "unknown",
    "unrecognized",
    "unexpected",
    "extra",
    "cannot be set",
    "must be omitted",
    "forbidden",
)


_CACHE_BODY_FIELDS = (
    PROMPT_CACHE_KEY_FIELD,
    PROMPT_CACHE_RETENTION_FIELD,
    CACHE_CONTROL_FIELD,
    OPENROUTER_SESSION_ID_FIELD,
)
_CACHE_HEADER_FIELDS = (
    OPENROUTER_SESSION_ID_HEADER_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
)
_PROMPT_CACHE_FIELDS = (*_CACHE_BODY_FIELDS, *_CACHE_HEADER_FIELDS)
_TOOL_CHOICE_UNSUPPORTED_STATUS_CODES = {400, 422}
_TOOL_CHOICE_UNSUPPORTED_TOKENS = (
    "invalid",
    "not allowed",
    "not support",
    "not supported",
    "unsupported",
    "unknown",
    "unrecognized",
    "unexpected",
)
_TOOL_CALLING_REJECTION_PARAMS = frozenset({"tool", "tools", "function", "functions"})
_TOOL_CALLING_REJECTION_TERMS = (
    "tool",
    "tools",
    "function",
    "functions",
    "function calling",
    "function_call",
)
_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS = DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS
_ERROR_BODY_DISPLAY_LIMIT = 1000


def _headers_with_default_accept_encoding(headers: dict[str, str]) -> dict[str, str]:
    request_headers = dict(headers)
    if not any(key.lower() == "accept-encoding" for key in request_headers):
        request_headers["accept-encoding"] = _DEFAULT_ACCEPT_ENCODING
    return request_headers


def _httpx_request_timeout(timeout_s: float) -> httpx.Timeout:
    request_timeout = max(float(timeout_s), 0.001)
    connect_timeout = min(request_timeout, _DEFAULT_CONNECT_TIMEOUT_S)
    return httpx.Timeout(request_timeout, connect=connect_timeout)


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        cause = current.__cause__
        context = current.__context__
        current = cause if cause is not None else context


def _is_connect_failure(exc: BaseException) -> bool:
    return any(
        isinstance(item, httpx.ConnectError | httpx.ConnectTimeout)
        for item in _iter_exception_chain(exc)
    )


def _is_read_timeout(exc: BaseException) -> bool:
    return any(isinstance(item, httpx.ReadTimeout) for item in _iter_exception_chain(exc))


def _common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    idx = 0
    while idx < limit and left[idx] == right[idx]:
        idx += 1
    return idx


def _suffix_prefix_overlap_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    for size in range(limit, 0, -1):
        if left[-size:] == right[:size]:
            return size
    return 0


def _strip_cumulative_restart_suffix(*, previous: str, incoming: str) -> str | None:
    candidate = incoming
    while candidate:
        trimmed = candidate.lstrip("\r\n ")
        if trimmed.startswith(previous):
            remainder = trimmed[len(previous) :]
            trimmed_remainder = remainder.lstrip("\r\n ")
            if trimmed_remainder.startswith(previous):
                candidate = trimmed_remainder
                continue
            return remainder
        if trimmed != candidate:
            candidate = trimmed
            continue
        break
    return None


def _looks_like_alternate_cumulative_restart(*, previous: str, incoming: str) -> bool:
    if len(previous) < 120 or len(incoming) < 80:
        return False
    if incoming.startswith(previous) or previous.startswith(incoming):
        return False
    common_prefix = _common_prefix_length(previous, incoming)
    if common_prefix < 24:
        return False
    return common_prefix < (min(len(previous), len(incoming)) // 2)


# Providers that need "cumulative" stream assembly when the setting is "auto".
# Empty: the de-duplication heuristics below date from April 2026, and the
# provider they were written for was never recorded. Until one is identified,
# cumulative assembly is reached only through an explicit
# ``llm_stream_assembly=cumulative``.
_CUMULATIVE_STREAM_PROVIDER_KEYS: frozenset[str] = frozenset()


def resolve_stream_assembly(setting: str | None, *, provider_key: str | None) -> str:
    """Resolve a stream assembly setting to ``concat`` or ``cumulative``.

    ``auto`` (and unset) is ``cumulative`` only for a provider listed in
    ``_CUMULATIVE_STREAM_PROVIDER_KEYS``, otherwise ``concat``. An unknown
    setting is an error rather than a silent fallback.
    """
    normalized = str(setting or STREAM_ASSEMBLY_AUTO).strip().casefold()
    if normalized not in STREAM_ASSEMBLY_SETTINGS:
        allowed = ", ".join(STREAM_ASSEMBLY_SETTINGS)
        raise ValueError(f"stream assembly must be one of: {allowed} (got {setting!r})")
    if normalized != STREAM_ASSEMBLY_AUTO:
        return normalized
    key = _normalize_provider_key(provider_key)
    if key and key in _CUMULATIVE_STREAM_PROVIDER_KEYS:
        return STREAM_ASSEMBLY_CUMULATIVE
    return STREAM_ASSEMBLY_CONCAT


def _stream_delta_suffix(*, previous: str, incoming: str) -> str:
    """The part of ``incoming`` not already in ``previous``; cumulative assembly only."""
    if not incoming:
        return ""
    if not previous:
        return incoming
    if incoming == previous:
        return ""
    if _looks_like_alternate_cumulative_restart(previous=previous, incoming=incoming):
        return ""
    cumulative_suffix = _strip_cumulative_restart_suffix(previous=previous, incoming=incoming)
    if cumulative_suffix is not None:
        return cumulative_suffix
    common_prefix = _common_prefix_length(previous, incoming)
    if common_prefix >= max(16, min(len(previous), len(incoming)) // 2):
        return incoming[common_prefix:]
    previous_restart = incoming.rfind(previous)
    if previous_restart > 0:
        prefix = incoming[:previous_restart]
        if not prefix.strip():
            return incoming[previous_restart + len(previous) :]
    overlap = _suffix_prefix_overlap_length(previous, incoming)
    if overlap > 0:
        restarted_suffix = _strip_cumulative_restart_suffix(
            previous=previous,
            incoming=incoming[overlap:],
        )
        if restarted_suffix is not None:
            return restarted_suffix
    if overlap >= max(4, len(incoming) // 2):
        return incoming[overlap:]
    return incoming


def _sanitize_transport_text(text: str) -> str:
    if not text:
        return text
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    try:
        # Recover surrogate-escaped terminal bytes when possible, and replace
        # genuinely invalid sequences so JSON transport never crashes.
        return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    except Exception:
        return text.encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_transport_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_transport_text(value)
    if isinstance(value, list):
        return [_sanitize_transport_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_transport_value(item) for item in value)
    if isinstance(value, dict):
        return {
            _sanitize_transport_text(key)
            if isinstance(key, str)
            else key: _sanitize_transport_value(item)
            for key, item in value.items()
        }
    return value


def _normalize_provider_key(provider_key: str | None) -> str:
    normalized = str(provider_key or "").strip().casefold()
    return "".join(char if char.isalnum() else "_" for char in normalized).strip("_")


def _provider_key_from_base_url(base_url: str | None) -> str | None:
    return known_provider_key_from_base_url(base_url)


def _transport_provider_key(
    *,
    base_url: str | None,
    provider_key: str | None,
    model: str | None,
) -> str:
    from_url = _provider_key_from_base_url(base_url)
    # These hosted routes use their Alysis contracts so DeepSeek's thinking
    # toggle and replay dialect cannot leak into other vendors' requests.
    if str(model or "").casefold() in {
        "glm-5.3-flash",
        "minimax-m3.1-flash",
        "gpt-oss-120b",
        "reflection-beam",
        "nemotron-3.5-super-vl",
        "glm-5.3",
        "qwen3.8-2.4t",
        "kimi-k3",
        "muse-spark-open",
    }:
        from ..provider_url import is_alysis_gateway_url

        if (
            is_alysis_gateway_url(str(base_url or ""))
            or _normalize_provider_key(provider_key) == "alysis"
        ):
            return "alysis"
    if from_url:
        return from_url
    normalized_provider = _normalize_provider_key(provider_key)
    if normalized_provider:
        if normalized_provider in {"dashscope", "qwen", "aliyun", "aliyuncs"}:
            return "qwen"
        return normalized_provider
    return _normalize_provider_key(best_effort_provider_key(base_url=base_url, model=model))


def _reasoning_transport_provider_key(
    *,
    transport_provider_key: str | None,
    reasoning_trace_adapter: str | None,
) -> str | None:
    """Resolve only the provider dialect used for reasoning state on the wire.

    Automatic selection preserves the existing provider inference. Explicit
    adapters are authoritative for custom OpenAI-compatible endpoints, while
    ``none`` and the passive adapter deliberately inject and replay nothing.
    """

    adapter = validate_reasoning_trace_adapter_for_protocol(
        protocol=OPENAI_COMPAT_PROTOCOL,
        adapter=reasoning_trace_adapter,
    )
    if adapter == "auto":
        return _normalize_provider_key(transport_provider_key) or None
    return _REASONING_PROVIDER_BY_ADAPTER.get(adapter)


def _reasoning_trace_adapter_for_route(adapter: str, *, base_url: str) -> str:
    """Resolve an ``auto`` adapter for hosts with a documented reasoning dialect."""
    if adapter != "auto":
        return adapter
    try:
        host = (urlsplit(base_url).hostname or "").rstrip(".").casefold()
    except ValueError:
        return adapter
    return "deepseek_reasoning" if host in _DEEPSEEK_REASONING_DIALECT_HOSTS else adapter


def _is_deepseek_provider(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) == _DEEPSEEK_PROVIDER_KEY


def _is_openrouter_provider(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) == _OPENROUTER_PROVIDER_KEY


def _is_gemini_provider(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) == _GEMINI_PROVIDER_KEY


def _is_dashscope_provider(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) in {"qwen", "dashscope"}


def _is_mistral_provider(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) == _MISTRAL_PROVIDER_KEY


def _is_moonshot_provider(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) in {_MOONSHOT_PROVIDER_KEY, "kimi_code"}


# Routes whose Chat Completions surface wants ``max_completion_tokens``.
# OpenAI's gpt-5 / o-series reject the legacy ``max_tokens`` outright
# ("Unsupported parameter: 'max_tokens' is not supported with this model"),
# and OpenAI accepts ``max_completion_tokens`` on every chat model, so the
# first-party host (and Azure, which mirrors it) always uses the new field.
# The decision is keyed on the endpoint host, not on a provider inferred from
# a ``gpt-*`` model name: an unknown OpenAI-compatible gateway serving such a
# model may only implement ``max_tokens``.
_MAX_COMPLETION_TOKENS_HOST_PROVIDERS = frozenset({"openai", "azure"})


def _uses_max_completion_tokens(provider_key: str | None, *, base_url: str | None) -> bool:
    if _is_moonshot_provider(provider_key):
        return True
    host_provider = _normalize_provider_key(_provider_key_from_base_url(base_url))
    return host_provider in _MAX_COMPLETION_TOKENS_HOST_PROVIDERS


def _is_nvidia_provider(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) == _NVIDIA_PROVIDER_KEY


def _is_together_deepseek_pro(provider_key: str | None, model: str | None) -> bool:
    return _normalize_provider_key(provider_key) == _TOGETHER_PROVIDER_KEY and str(
        model or ""
    ).strip().casefold().startswith("deepseek-ai/deepseek-v4-pro")


def _uses_reasoning_effort(provider_key: str | None) -> bool:
    return _normalize_provider_key(provider_key) in _OPENAI_STYLE_REASONING_EFFORT_PROVIDERS


def _model_name_parts(model: str | None) -> set[str]:
    normalized = str(model or "").strip().casefold()
    return {part for part in re.split(r"[^a-z0-9]+", normalized) if part}


def _gemini_model_allows_none_reasoning_effort(model: str | None) -> bool:
    parts = _model_name_parts(model)
    if "gemini" not in parts or "2" not in parts or "5" not in parts:
        return False
    if "pro" in parts:
        return False
    return "flash" in parts


def _gemini_reasoning_effort(
    *,
    model: str | None,
    reasoning_effort: str | None,
) -> str | None:
    effort = str(reasoning_effort or "").strip().casefold()
    if not effort:
        return None
    contract = reasoning_contract_for("gemini", model)
    if contract.wire == WIRE_THINKING_LEVEL:
        if contract.allows_value(effort):
            return effort
        return contract.default or None
    if effort in _GEMINI_REASONING_EFFORTS:
        return effort
    if effort == "none" and _gemini_model_allows_none_reasoning_effort(model):
        return effort
    return None


def _reasoning_effort_enables_thinking(reasoning_effort: str | None) -> bool | None:
    normalized = _normalize_provider_key(reasoning_effort)
    if not normalized:
        return None
    return normalized != "none"


def _documented_reasoning_effort(
    *,
    provider_key: str | None,
    model: str | None,
    reasoning_effort: str | None,
) -> str | None:
    """Return an exact provider-documented effort value, never a guessed alias."""

    normalized = str(reasoning_effort or "").strip().casefold()
    if not normalized or normalized == "none":
        return None
    contract = reasoning_contract_for(provider_key, model)
    if contract.wire != WIRE_REASONING_EFFORT or not contract.allows_value(normalized):
        return None
    return normalized


def _documented_flat_reasoning_effort(
    *,
    provider_key: str | None,
    model: str | None,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
) -> str | None:
    """Return the contract-approved flat value, including an explicit off value.

    ``_documented_reasoning_effort`` intentionally treats ``none`` as omission
    for transports with a separate thinking toggle. Generic Chat Completions
    providers may instead document ``reasoning_effort='none'`` as their only
    off wire shape, so this path preserves that structural distinction.
    """

    contract = reasoning_contract_for(provider_key, model)
    if not contract.emits_flat_reasoning_effort or contract.wire != WIRE_REASONING_EFFORT:
        return None
    if enable_thinking is False:
        return "none" if contract.off == OFF_EXPLICIT and contract.allows_value("none") else None
    normalized = str(reasoning_effort or "").strip().casefold()
    if not normalized:
        return None
    if normalized == "none" and enable_thinking is True:
        return None
    return normalized if contract.allows_value(normalized) else None


def _deepseek_reasoning_payload_enabled(
    *,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
) -> bool | None:
    if enable_thinking is not None:
        return enable_thinking
    return _reasoning_effort_enables_thinking(reasoning_effort)


def _reasoning_contract_active(
    *,
    provider_key: str | None,
    model: str | None,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
) -> bool | None:
    """Resolve reasoning activity from an explicit per-model wire contract.

    ``None`` means that the catalog does not know the model's default.  This is
    intentionally tri-state so an unknown OpenAI-compatible route never gains
    provider behavior from a name guess.
    """

    contract = reasoning_contract_for(provider_key, model)
    if contract.mode not in {ALWAYS_ON, OPTIONAL}:
        return None
    if contract.mode == ALWAYS_ON:
        return True
    if enable_thinking is not None:
        return enable_thinking
    normalized_effort = str(reasoning_effort or "").strip().casefold()
    if normalized_effort:
        return normalized_effort != "none"
    if contract.default:
        return contract.default.casefold() != "none"
    return None


def _openrouter_reasoning_payload(
    *,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
) -> dict[str, Any] | None:
    normalized_effort = str(reasoning_effort or "").strip().lower()
    if normalized_effort:
        return {"effort": normalized_effort}
    if enable_thinking is True:
        return {"enabled": True}
    if enable_thinking is False:
        return {"enabled": False}
    return None


def _tool_choice_forces_a_call(tool_choice: Any) -> bool:
    """Whether ``tool_choice`` compels the model to emit a tool call.

    A specific-function object (``{"type": "function", ...}``) or the strings
    ``"required"`` / ``"any"`` force a call. ``"auto"`` / ``"none"`` / unset do
    not. Reasoning providers (DeepSeek, OpenRouter/MiMo, DashScope/Qwen, Zhipu
    GLM) reject a *forced* choice while thinking is on -- the API returns
    ``400 "Thinking mode does not support this tool_choice"`` -- so the caller
    omits ``tool_choice`` when the request runs in thinking mode.
    """
    if isinstance(tool_choice, dict):
        return True
    if isinstance(tool_choice, str):
        return tool_choice.strip().lower() in {"required", "any"}
    return False


def _deepseek_reasoning_provider_metadata(reasoning_content: str) -> dict[str, Any] | None:
    reasoning = str(reasoning_content or "")
    if not reasoning:
        return None
    return {
        _DEEPSEEK_PROVIDER_KEY: {
            _DEEPSEEK_REASONING_CONTENT_KEY: reasoning,
        }
    }


def _qwen_reasoning_provider_metadata(reasoning_content: str) -> dict[str, Any] | None:
    reasoning = str(reasoning_content or "")
    if not reasoning:
        return None
    return {
        _QWEN_PROVIDER_KEY: {
            _DEEPSEEK_REASONING_CONTENT_KEY: reasoning,
        }
    }


def _moonshot_reasoning_provider_metadata(reasoning_content: str) -> dict[str, Any] | None:
    reasoning = str(reasoning_content or "")
    if not reasoning:
        return None
    return {
        _MOONSHOT_PROVIDER_KEY: {
            _DEEPSEEK_REASONING_CONTENT_KEY: reasoning,
        }
    }


def _nvidia_reasoning_provider_metadata(reasoning_content: str) -> dict[str, Any] | None:
    reasoning = str(reasoning_content or "")
    if not reasoning:
        return None
    return {
        _NVIDIA_PROVIDER_KEY: {
            _DEEPSEEK_REASONING_CONTENT_KEY: reasoning,
        }
    }


def _is_mistral_thinking_chunk(value: Any) -> bool:
    return isinstance(value, dict) and str(value.get("type") or "").casefold() == "thinking"


def _mistral_content_provider_metadata(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, list) or not any(
        _is_mistral_thinking_chunk(chunk) for chunk in content
    ):
        return None
    return {
        _MISTRAL_PROVIDER_KEY: {
            _MISTRAL_CONTENT_CHUNKS_KEY: copy.deepcopy(content),
        }
    }


def _openrouter_reasoning_provider_metadata(
    *,
    reasoning: str | None = None,
    reasoning_details: Any = None,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}
    reasoning_text = str(reasoning or "")
    if reasoning_text:
        payload[_OPENROUTER_REASONING_KEY] = reasoning_text
    if isinstance(reasoning_details, list) and reasoning_details:
        payload[_OPENROUTER_REASONING_DETAILS_KEY] = reasoning_details
    if not payload:
        return None
    return {_OPENROUTER_PROVIDER_KEY: payload}


def _reasoning_outputs_from_message(
    message: dict[str, Any],
    *,
    provider_key: str | None,
    trace_adapter: str | None = None,
) -> tuple[ReasoningOutput, ...]:
    if trace_adapter is None:
        trace_adapter = resolve_reasoning_trace_capability(
            provider_key=provider_key or "",
            protocol=OPENAI_COMPAT_PROTOCOL,
        ).adapter
    trace = CompatibleReasoningTrace(trace_adapter, provider=provider_key)
    trace.feed(message)
    return trace.outputs


def _provider_metadata_for_reasoning(
    *,
    provider_key: str | None,
    message: dict[str, Any],
    model: str | None = None,
    reasoning_content_message: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if _is_together_deepseek_pro(provider_key, model):
        reasoning = message.get(_OPENROUTER_REASONING_KEY)
        if not isinstance(reasoning, str):
            # Together accepts the older reasoning_content spelling on input,
            # so tolerate it in compatible proxy responses as well.
            reasoning = message.get(_DEEPSEEK_REASONING_CONTENT_KEY)
        return _deepseek_reasoning_provider_metadata(
            reasoning if isinstance(reasoning, str) else ""
        )
    if _is_deepseek_provider(provider_key):
        reasoning = message.get(_DEEPSEEK_REASONING_CONTENT_KEY)
        return _deepseek_reasoning_provider_metadata(
            reasoning if isinstance(reasoning, str) else ""
        )
    if _is_dashscope_provider(provider_key):
        reasoning = message.get(_DEEPSEEK_REASONING_CONTENT_KEY)
        return _qwen_reasoning_provider_metadata(reasoning if isinstance(reasoning, str) else "")
    if _is_moonshot_provider(provider_key):
        reasoning = message.get(_DEEPSEEK_REASONING_CONTENT_KEY)
        return _moonshot_reasoning_provider_metadata(
            reasoning if isinstance(reasoning, str) else ""
        )
    if _is_nvidia_provider(provider_key):
        reasoning = message.get(_DEEPSEEK_REASONING_CONTENT_KEY)
        return _nvidia_reasoning_provider_metadata(reasoning if isinstance(reasoning, str) else "")
    if _is_openrouter_provider(provider_key):
        reasoning = message.get(_OPENROUTER_REASONING_KEY)
        reasoning_details = message.get(_OPENROUTER_REASONING_DETAILS_KEY)
        return _openrouter_reasoning_provider_metadata(
            reasoning=reasoning if isinstance(reasoning, str) else None,
            reasoning_details=reasoning_details,
        )
    if _is_mistral_provider(provider_key):
        return _mistral_content_provider_metadata(message.get("content"))
    if reasoning_contract_for(provider_key, model).replay_reasoning_content:
        # Streaming display/legacy dialects may combine aliases. The generic
        # replay capability accepts only the actual reasoning_content field.
        source = message if reasoning_content_message is None else reasoning_content_message
        reasoning = source.get(_DEEPSEEK_REASONING_CONTENT_KEY)
        if isinstance(reasoning, str):
            return {
                OPENAI_COMPAT_REASONING_METADATA_KEY: {
                    _DEEPSEEK_REASONING_CONTENT_KEY: reasoning,
                }
            }
    return None


def _deepseek_reasoning_from_provider_metadata(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    deepseek = metadata.get(_DEEPSEEK_PROVIDER_KEY)
    if not isinstance(deepseek, dict):
        return ""
    reasoning = deepseek.get(_DEEPSEEK_REASONING_CONTENT_KEY)
    return reasoning if isinstance(reasoning, str) else ""


def _openrouter_reasoning_from_provider_metadata(metadata: Any) -> tuple[str, list[Any] | None]:
    if not isinstance(metadata, dict):
        return "", None
    openrouter = metadata.get(_OPENROUTER_PROVIDER_KEY)
    if not isinstance(openrouter, dict):
        return "", None
    reasoning = openrouter.get(_OPENROUTER_REASONING_KEY)
    reasoning_details = openrouter.get(_OPENROUTER_REASONING_DETAILS_KEY)
    return (
        reasoning if isinstance(reasoning, str) else "",
        list(reasoning_details) if isinstance(reasoning_details, list) else None,
    )


def _qwen_reasoning_from_provider_metadata(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    qwen = metadata.get(_QWEN_PROVIDER_KEY)
    if not isinstance(qwen, dict):
        return ""
    reasoning = qwen.get(_DEEPSEEK_REASONING_CONTENT_KEY)
    return reasoning if isinstance(reasoning, str) else ""


def _moonshot_reasoning_from_provider_metadata(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    moonshot = metadata.get(_MOONSHOT_PROVIDER_KEY)
    if not isinstance(moonshot, dict):
        return ""
    reasoning = moonshot.get(_DEEPSEEK_REASONING_CONTENT_KEY)
    return reasoning if isinstance(reasoning, str) else ""


def _nvidia_reasoning_from_provider_metadata(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    nvidia = metadata.get(_NVIDIA_PROVIDER_KEY)
    if not isinstance(nvidia, dict):
        return ""
    reasoning = nvidia.get(_DEEPSEEK_REASONING_CONTENT_KEY)
    return reasoning if isinstance(reasoning, str) else ""


def _mistral_content_from_provider_metadata(metadata: Any) -> list[Any] | None:
    if not isinstance(metadata, dict):
        return None
    mistral = metadata.get(_MISTRAL_PROVIDER_KEY)
    if not isinstance(mistral, dict):
        return None
    content = mistral.get(_MISTRAL_CONTENT_CHUNKS_KEY)
    if not isinstance(content, list) or not any(
        _is_mistral_thinking_chunk(chunk) for chunk in content
    ):
        return None
    return copy.deepcopy(content)


def _gemini_tool_call_provider_metadata(tool_call: dict[str, Any]) -> dict[str, Any] | None:
    extra_content = tool_call.get(_GEMINI_EXTRA_CONTENT_KEY)
    if not isinstance(extra_content, dict) or not extra_content:
        return None
    return {
        _GEMINI_PROVIDER_KEY: {
            _GEMINI_EXTRA_CONTENT_KEY: copy.deepcopy(extra_content),
        }
    }


def _copy_transport_tool_calls(
    tool_calls: Any,
    *,
    preserve_extra_content: bool,
) -> list[Any] | Any:
    if not isinstance(tool_calls, list):
        return tool_calls
    copied_tool_calls: list[Any] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            copied_tool_calls.append(tool_call)
            continue
        copied_tool_call = copy.deepcopy(tool_call)
        if not preserve_extra_content:
            copied_tool_call.pop(_GEMINI_EXTRA_CONTENT_KEY, None)
        copied_tool_calls.append(copied_tool_call)
    return copied_tool_calls


def _gemini_extra_content_indexes(metadata: Any) -> tuple[dict[str, Any], dict[int, Any]]:
    if not isinstance(metadata, dict):
        return {}, {}
    entries = metadata.get(_TOOL_CALL_PROVIDER_METADATA_KEY)
    if not isinstance(entries, list):
        return {}, {}
    by_id: dict[str, Any] = {}
    by_index: dict[int, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_metadata = entry.get("metadata")
        if not isinstance(entry_metadata, dict):
            continue
        gemini = entry_metadata.get(_GEMINI_PROVIDER_KEY)
        if not isinstance(gemini, dict):
            continue
        extra_content = gemini.get(_GEMINI_EXTRA_CONTENT_KEY)
        if not isinstance(extra_content, dict) or not extra_content:
            continue
        tool_call_id = entry.get("id")
        if isinstance(tool_call_id, str) and tool_call_id:
            by_id[tool_call_id] = copy.deepcopy(extra_content)
        index = entry.get("index")
        if isinstance(index, int):
            by_index[index] = copy.deepcopy(extra_content)
    return by_id, by_index


def _reattach_gemini_tool_call_extra_content(
    message: dict[str, Any],
    metadata: Any,
) -> None:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return
    by_id, by_index = _gemini_extra_content_indexes(metadata)
    if not by_id and not by_index:
        return
    reattached: list[Any] = []
    for index, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict):
            reattached.append(tool_call)
            continue
        copied_tool_call = dict(tool_call)
        tool_call_id = str(copied_tool_call.get("id") or "")
        extra_content = by_id.get(tool_call_id) if tool_call_id else None
        if extra_content is None:
            extra_content = by_index.get(index)
        if isinstance(extra_content, dict) and extra_content:
            copied_tool_call[_GEMINI_EXTRA_CONTENT_KEY] = copy.deepcopy(extra_content)
        reattached.append(copied_tool_call)
    message["tool_calls"] = reattached


def _message_for_transport(
    message: dict[str, Any],
    *,
    provider_key: str | None,
    reasoning_provider_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    metadata = message.get(PROVIDER_METADATA_KEY)
    copied = strip_provider_metadata_from_message(message)
    if str(copied.get("role") or "") != "assistant":
        return copied
    has_tool_calls = bool(copied.get("tool_calls"))
    if has_tool_calls:
        copied["tool_calls"] = _copy_transport_tool_calls(
            copied.get("tool_calls"),
            preserve_extra_content=_is_gemini_provider(provider_key),
        )
        if _is_gemini_provider(provider_key):
            _reattach_gemini_tool_call_extra_content(copied, metadata)
    if reasoning_contract_for(reasoning_provider_key, model).replay_reasoning_content:
        protocol_state = (
            metadata.get(OPENAI_COMPAT_REASONING_METADATA_KEY)
            if isinstance(metadata, dict)
            else None
        )
        reasoning = (
            protocol_state.get(_DEEPSEEK_REASONING_CONTENT_KEY)
            if isinstance(protocol_state, dict)
            else None
        )
        if isinstance(reasoning, str):
            copied[_DEEPSEEK_REASONING_CONTENT_KEY] = reasoning
            return copied
    together_deepseek_transport = _is_together_deepseek_pro(provider_key, model)
    deepseek_transport = _is_deepseek_provider(reasoning_provider_key)
    replay_all_deepseek_turns = reasoning_contract_for(
        reasoning_provider_key,
        model,
    ).replay_reasoning_content
    if (deepseek_transport or together_deepseek_transport) and (
        has_tool_calls or replay_all_deepseek_turns
    ):
        reasoning = _deepseek_reasoning_from_provider_metadata(metadata)
        if reasoning:
            if together_deepseek_transport:
                copied[_OPENROUTER_REASONING_KEY] = reasoning
            else:
                copied[_DEEPSEEK_REASONING_CONTENT_KEY] = reasoning
    elif _is_dashscope_provider(reasoning_provider_key) and (
        has_tool_calls or reasoning_contract_for(_QWEN_PROVIDER_KEY, model).replay_reasoning_content
    ):
        reasoning = _qwen_reasoning_from_provider_metadata(metadata)
        if reasoning:
            copied[_DEEPSEEK_REASONING_CONTENT_KEY] = reasoning
    elif has_tool_calls and _is_openrouter_provider(reasoning_provider_key):
        reasoning, reasoning_details = _openrouter_reasoning_from_provider_metadata(metadata)
        if reasoning:
            copied[_OPENROUTER_REASONING_KEY] = reasoning
        if reasoning_details:
            copied[_OPENROUTER_REASONING_DETAILS_KEY] = reasoning_details
    elif has_tool_calls and _is_nvidia_provider(reasoning_provider_key):
        reasoning = _nvidia_reasoning_from_provider_metadata(metadata)
        if reasoning:
            copied[_DEEPSEEK_REASONING_CONTENT_KEY] = reasoning
    elif _is_moonshot_provider(reasoning_provider_key):
        reasoning = _moonshot_reasoning_from_provider_metadata(metadata)
        if reasoning:
            copied[_DEEPSEEK_REASONING_CONTENT_KEY] = reasoning
    elif _is_mistral_provider(reasoning_provider_key):
        content = _mistral_content_from_provider_metadata(metadata)
        if content:
            copied["content"] = content
    return copied


def _messages_for_transport(
    messages: list[dict[str, Any]],
    *,
    provider_key: str | None,
    reasoning_provider_key: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    result = [
        _message_for_transport(
            message,
            provider_key=provider_key,
            reasoning_provider_key=reasoning_provider_key,
            model=model,
        )
        for message in messages
        if isinstance(message, dict)
    ]
    if provider_key == "alysis" and model in {"qwen3.8-2.4t", "reflection-beam"}:
        # These templates accept instructions only at the beginning (Beam can
        # silently ignore later instructions). Preserve all host instructions
        # in order, including completion checks, without changing user/tool roles.
        instructions = [m for m in result if m.get("role") in {"system", "developer"}]
        if instructions:
            result = [
                {
                    "role": "system",
                    "content": "\n\n".join(
                        _normalize_assistant_content_to_text(m.get("content")) for m in instructions
                    ),
                }
            ] + [m for m in result if m.get("role") not in {"system", "developer"}]
    return result


def _normalize_assistant_content_to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(_normalize_assistant_content_to_text(item) for item in raw)
    if isinstance(raw, dict):
        part_type = raw.get("type")
        text = raw.get("text")
        if isinstance(text, str) and (
            part_type in _TEXT_LIKE_CONTENT_PART_TYPES or part_type is None
        ):
            return text
        return ""
    return ""


def _append_mistral_stream_content(chunks: list[dict[str, Any]], raw: Any) -> bool:
    """Reconstruct Mistral replay content independently of trace presentation."""

    if isinstance(raw, str):
        incoming: list[Any] = [{"type": "text", "text": raw}] if raw else []
    elif isinstance(raw, list):
        incoming = raw
    else:
        return False

    changed = False
    for item in incoming:
        if not isinstance(item, dict):
            continue
        copied = copy.deepcopy(item)
        item_type = str(copied.get("type") or "").casefold()
        if item_type == "text" and chunks:
            previous = chunks[-1]
            previous_text = previous.get("text")
            incoming_text = copied.get("text")
            if (
                str(previous.get("type") or "").casefold() == "text"
                and isinstance(previous_text, str)
                and isinstance(incoming_text, str)
            ):
                previous["text"] = previous_text + incoming_text
                changed = bool(incoming_text) or changed
                continue
        if item_type == "thinking" and chunks:
            previous = chunks[-1]
            previous_thinking = previous.get("thinking")
            incoming_thinking = copied.get("thinking")
            if (
                str(previous.get("type") or "").casefold() == "thinking"
                and previous.get("closed") is not True
            ):
                if isinstance(previous_thinking, list) and isinstance(incoming_thinking, list):
                    previous["thinking"] = previous_thinking + incoming_thinking
                    changed = bool(incoming_thinking) or changed
                elif isinstance(previous_thinking, str) and isinstance(incoming_thinking, str):
                    previous["thinking"] = previous_thinking + incoming_thinking
                    changed = bool(incoming_thinking) or changed
                else:
                    chunks.append(copied)
                    changed = True
                    continue
                for key, value in copied.items():
                    if key == "thinking":
                        continue
                    if key == "signature" and value is None:
                        continue
                    if previous.get(key) != value:
                        changed = True
                    previous[key] = value
                continue
        chunks.append(copied)
        changed = True
    return changed


def _parse_arguments(args_s: str) -> dict[str, Any]:
    try:
        args = json.loads(args_s)
    except json.JSONDecodeError:
        return {"_raw_arguments": args_s}
    if not isinstance(args, dict):
        return {"_raw_arguments": args_s}
    return args


def _parse_tool_calls(tool_calls_raw: list[dict[str, Any]]) -> list[ToolCall]:
    tool_calls: list[ToolCall] = []
    for tc in tool_calls_raw:
        try:
            tc_id = tc["id"]
            fn = tc["function"]
            name = fn["name"]
            args_s = fn.get("arguments") or "{}"
            if not isinstance(args_s, str):
                args_s = json.dumps(args_s)
            tool_calls.append(
                ToolCall(
                    id=tc_id,
                    name=name,
                    arguments=_parse_arguments(args_s),
                    provider_metadata=_gemini_tool_call_provider_metadata(tc),
                )
            )
        except Exception:
            continue
    return tool_calls


def _parse_stream_tool_calls(tool_chunks: dict[int, dict[str, Any]]) -> list[ToolCall]:
    out: list[ToolCall] = []
    for idx in sorted(tool_chunks):
        chunk = tool_chunks[idx]
        name = chunk.get("name") or ""
        if not name:
            continue
        tc_id = chunk.get("id") or f"call_{idx}"
        args_s = chunk.get("arguments") or "{}"
        metadata = chunk.get("provider_metadata")
        out.append(
            ToolCall(
                id=tc_id,
                name=name,
                arguments=_parse_arguments(args_s),
                provider_metadata=dict(metadata)
                if isinstance(metadata, dict) and metadata
                else None,
            )
        )
    return out


def _parse_usage(raw: Any, *, provider_key: str | None = None) -> LLMUsage | None:
    return parse_compatible_usage(raw, provider_key=provider_key)


def _is_stream_options_unsupported_error(err: LLMError) -> bool:
    msg = str(err).lower()
    if "stream_options" not in msg:
        return False
    return any(token in msg for token in ("unsupported", "unknown", "invalid", "not allowed"))


def _llm_error_status_code(err: LLMError) -> int | None:
    status_code = getattr(err, "provider_status_code", None)
    if isinstance(status_code, int):
        return status_code
    match = re.match(r"LLM error\s+(\d{3}):", str(err or "").strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _llm_error_body(err: LLMError) -> str:
    full_body = getattr(err, "provider_error_body", None)
    if isinstance(full_body, str):
        return full_body.strip()
    _prefix, sep, body = str(err or "").partition(":")
    return body.strip() if sep else str(err or "").strip()


def _json_error_payload(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# Friendly, user-facing copy for the Alysis hosted gateway and legacy trial codes.
# The proxy (Supabase Edge Function) returns an OpenAI-shaped envelope
# {"error": {"message": ..., "type": ..., "code": "<reason>"}} with these string
# codes; upstream/other-provider errors use different codes and fall through.
_ALYSIS_PROXY_ERROR_MESSAGES: dict[str, str] = {
    "invalid_key": (
        "Your Alysis Code session is invalid or has been reset. "
        "Run `alysis login` to reconnect your account."
    ),
    "account_restricted": (
        "Your Alysis account requires review. Contact products@alysisai.com for help."
    ),
    "billing_review_required": (
        "A previous Alysis hosted request needs a billing review before you can continue. "
        "Contact products@alysisai.com for help."
    ),
    "billing_pending": (
        "Alysis could not confirm the usage of this hosted request. "
        "Billing reconciliation is pending; contact products@alysisai.com if it persists."
    ),
    "email_verification_required": (
        "Confirm your Alysis account email before using free hosted credits. "
        "Check your inbox for the verification link."
    ),
    "free_mailbox_conflict": (
        "Free hosted credits are already linked to another account for this mailbox. "
        "Sign in to your original Alysis account or contact products@alysisai.com."
    ),
    "model_not_allowed": (
        "This model is not available on your Alysis hosted plan. "
        "Choose an available model with /config or manage your plan at {account_url}"
    ),
    "trial_expired": ("Your 10-day free MiMo trial has ended. See your options at {account_url}"),
    "quota_exhausted": (
        "You've used all of your free MiMo trial tokens. See your options at {account_url}"
    ),
    "email_not_verified": (
        "Please confirm your email to use the MiMo trial — "
        "check your inbox for the verification link."
    ),
    "plan_inactive": ("Your Alysis Code plan is not active. Visit {account_url} to continue."),
    "rate_limit_exceeded": (
        "You're sending requests too quickly. Please wait a moment and try again."
    ),
    "hosted_capacity_exceeded": (
        "The Alysis hosted model is unavailable or at capacity. "
        "If this persists, its server route needs checking. "
        "No generation was started and no credits were charged."
    ),
    "hosted_routing_unavailable": (
        "Hosted model routing is temporarily unavailable. Please retry shortly. "
        "No generation was started and your reserved credits were released."
    ),
    "global_budget_exceeded": (
        "The free MiMo trial is at capacity right now. Please try again shortly."
    ),
    "proxy_unconfigured": ("The MiMo service is temporarily unavailable. Please try again later."),
}


def alysis_trial_error_message(err: LLMError) -> str | None:
    """Friendly message for an Alysis hosted/legacy proxy error, or None if not ours.

    Maps the proxy's known error ``code`` (trial_expired, quota_exhausted, ...) to
    human copy so a user whose trial ended sees a clear next step instead of a raw
    ``LLM error 402: {...}`` dump. Returns None for any other failure (including
    upstream OpenRouter errors, which use numeric codes), so non-proxy errors
    render unchanged.
    """
    payload = _json_error_payload(_llm_error_body(err))
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = str(error.get("code") or "").strip()
    # The current hosted gateway distinguishes account concurrency, rolling caps,
    # and reservation budget in its message, while retaining the OpenAI error code.
    # Preserve that explanation instead of mislabeling every limit as excess RPM.
    message = error.get("message")
    if (
        code == "hosted_capacity_exceeded"
        and isinstance(message, str)
        and message.startswith("Hosted model capacity is busy. Retry in ")
    ):
        return message[:2048]
    # Paid-plan answers (Plan 1 / Plan 5) are written for people and carry the
    # reset date or upgrade link, so show them as sent. A paused model names
    # itself and how long the pause lasts.
    if code in (
        "plan_credits_exhausted",
        "plan_limit_reached",
        "plan_unavailable",
        "hosted_model_paused",
    ) and isinstance(message, str):
        return message[:2048]
    if (
        code == "model_not_allowed"
        and isinstance(message, str)
        and message.startswith(
            (
                "This model is not included in ",
                # Hosted DeepSeek ended Oct 22, 2026; the gateway names replacements.
                "DeepSeek models are no longer offered on Alysis hosted plans.",
            )
        )
    ):
        return message[:2048]
    if (
        code == "rate_limit_exceeded"
        and isinstance(message, str)
        and message.startswith(
            (
                "5-hour fair-use limit reached (",
                "Weekly fair-use limit reached (",
                "Four hosted requests are already running for your account.",
                "Your account has reached its hosted request limit.",
                "Your account has reached the 20 requests per minute limit.",
                "Available credits cannot cover this request while other usage or reservations apply.",
                "Your available credit allowance is exhausted.",
            )
        )
    ):
        return message[:2048]
    template = _ALYSIS_PROXY_ERROR_MESSAGES.get(code)
    if template is None or "{account_url}" not in template:
        # str.format would also choke on any literal brace in an unrelated
        # message, so substitute only where the placeholder actually appears.
        return template
    # Resolved at render time, not import time: the account URL follows
    # ALYSIS_SITE_URL, which a staging deployment overrides per-process.
    from ..alysis_cloud import account_url

    return template.replace("{account_url}", account_url())


def _temperature_error_fields(err: LLMError) -> tuple[str, str, str] | None:
    status_code = _llm_error_status_code(err)
    if status_code not in _TEMPERATURE_UNSUPPORTED_STATUS_CODES:
        return None

    body = _llm_error_body(err)
    payload = _json_error_payload(body)
    if not payload:
        message = body.strip().casefold()
        if "temperature" in message and any(
            token in message for token in _TEMPERATURE_UNSUPPORTED_TOKENS
        ):
            return "", "", message
        return None

    raw_error = payload.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else payload
    param = str(error.get("param") or "").strip().casefold()
    code = str(error.get("code") or "").strip().casefold()
    message = str(error.get("message") or "").strip().casefold()
    combined = f"{code} {message}"
    has_unsupported_marker = any(token in combined for token in _TEMPERATURE_UNSUPPORTED_TOKENS)
    if param == "temperature" and (code or message) and has_unsupported_marker:
        return param, code, message
    if "temperature" in message and has_unsupported_marker:
        return param, code, message
    return None


def _temperature_unsupported_error(err: LLMError) -> bool:
    return _temperature_error_fields(err) is not None


def _tool_choice_unsupported_error(err: LLMError) -> bool:
    status_code = _llm_error_status_code(err)
    if status_code not in _TOOL_CHOICE_UNSUPPORTED_STATUS_CODES:
        return False

    body = _llm_error_body(err)
    payload = _json_error_payload(body)
    if not payload:
        message = body.strip().casefold()
        return "tool_choice" in message and any(
            token in message for token in _TOOL_CHOICE_UNSUPPORTED_TOKENS
        )

    raw_error = payload.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else payload
    param = str(error.get("param") or "").strip().casefold()
    code = str(error.get("code") or "").strip().casefold()
    message = str(error.get("message") or "").strip().casefold()
    combined = f"{param} {code} {message}"
    if param == "tool_choice":
        return True
    return "tool_choice" in combined and any(
        token in combined for token in _TOOL_CHOICE_UNSUPPORTED_TOKENS
    )


def _tool_calling_unsupported_error(err: LLMError) -> bool:
    status_code = _llm_error_status_code(err)
    if status_code is None or status_code < 400 or status_code >= 500 or status_code == 429:
        return False

    body = _llm_error_body(err)
    payload = _json_error_payload(body)
    if not payload:
        combined = body.strip().casefold()
    else:
        raw_error = payload.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else payload
        param = str(error.get("param") or "").strip().casefold()
        code = str(error.get("code") or "").strip().casefold()
        message = str(error.get("message") or "").strip().casefold()
        if param:
            # A rejected sampling/model field is not evidence that tools are
            # unsupported, even when the explanatory message mentions them.
            return param in _TOOL_CALLING_REJECTION_PARAMS
        combined = f"{param} {code} {message}"
    if "tool_choice" in combined:
        return False
    return any(term in combined for term in _TOOL_CALLING_REJECTION_TERMS) and any(
        token in combined for token in _TOOL_CHOICE_UNSUPPORTED_TOKENS
    )


def _is_temperature_default_value(value: Any) -> bool:
    try:
        return float(value) == _TEMPERATURE_DEFAULT_VALUE
    except (TypeError, ValueError):
        return False


def _temperature_compat_mode_for_error(
    err: LLMError,
    *,
    current_temperature: Any,
) -> str | None:
    fields = _temperature_error_fields(err)
    if fields is None:
        return None
    _param, _code, message = fields
    if "deprecated" in message:
        return _TEMPERATURE_COMPAT_MODE_OMIT
    if not _is_temperature_default_value(current_temperature):
        return _TEMPERATURE_COMPAT_MODE_DEFAULT
    return _TEMPERATURE_COMPAT_MODE_OMIT


def _safe_cache_request_field_values(values: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(values, Mapping):
        return {}
    safe: dict[str, str] = {}
    for field in _PROMPT_CACHE_FIELDS:
        value = values.get(field)
        text = str(value or "").strip()
        if not text or "\r" in text or "\n" in text:
            continue
        safe[field] = text
    return safe


def _set_header_if_absent(headers: dict[str, str], field: str, value: str) -> None:
    if not value:
        return
    lowered = field.casefold()
    if any(str(key).casefold() == lowered for key in headers):
        return
    headers[field] = value


def _strip_header_case_insensitive(headers: dict[str, str], field: str) -> None:
    lowered = field.casefold()
    for key in list(headers):
        if str(key).casefold() == lowered:
            headers.pop(key, None)


def _cache_fields_in_request(
    *,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
) -> tuple[str, ...]:
    header_keys = {str(key).casefold() for key in headers}
    fields: list[str] = []
    for field in _PROMPT_CACHE_FIELDS:
        if field in _CACHE_HEADER_FIELDS:
            if field.casefold() in header_keys:
                fields.append(field)
        elif field == CACHE_CONTROL_FIELD:
            if count_cache_control_blocks(payload) > 0:
                fields.append(field)
        elif field in payload:
            fields.append(field)
    return tuple(fields)


def _strip_cache_request_fields(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if field in _CACHE_HEADER_FIELDS:
            _strip_header_case_insensitive(headers, field)
        elif field == CACHE_CONTROL_FIELD:
            payload.pop(field, None)
            strip_cache_control_blocks(payload)
        else:
            payload.pop(field, None)


def _cache_param_rejected_fields(
    err: LLMError,
    *,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
) -> tuple[str, ...]:
    active_fields = _cache_fields_in_request(payload=payload, headers=headers)
    if not active_fields:
        return ()
    status_code = _llm_error_status_code(err)
    if status_code not in _CACHE_PARAM_UNSUPPORTED_STATUS_CODES:
        return ()

    body = _llm_error_body(err)
    payload_error = _json_error_payload(body)
    if payload_error:
        raw_error = payload_error.get("error")
        error = raw_error if isinstance(raw_error, dict) else payload_error
        param = str(error.get("param") or "").strip().casefold()
        code = str(error.get("code") or "").strip().casefold()
        message = str(error.get("message") or "").strip().casefold()
        combined = f"{param} {code} {message}"
        if not combined.strip():
            # JSON error envelopes without param/code/message (FastAPI/pydantic
            # detail lists, bare {"error": "<string>"}) still name the field in
            # the raw body.
            combined = body.strip().casefold()
    else:
        combined = body.strip().casefold()

    rejected = [field for field in active_fields if field.casefold() in combined]
    if PROMPT_CACHE_KEY_FIELD in rejected and PROMPT_CACHE_RETENTION_FIELD in active_fields:
        rejected.append(PROMPT_CACHE_RETENTION_FIELD)
    if (
        OPENROUTER_SESSION_ID_FIELD in rejected
        and OPENROUTER_SESSION_ID_HEADER_FIELD in active_fields
    ):
        rejected.append(OPENROUTER_SESSION_ID_HEADER_FIELD)
    if (
        OPENROUTER_SESSION_ID_HEADER_FIELD in rejected
        and OPENROUTER_SESSION_ID_FIELD in active_fields
    ):
        rejected.append(OPENROUTER_SESSION_ID_FIELD)
    if rejected:
        return tuple(dict.fromkeys(rejected))

    has_cache_signal = (
        "prompt cache" in combined
        or "prompt_cache" in combined
        or "cache_control" in combined
        or "cache control" in combined
        or "cache routing" in combined
        or "sticky" in combined
    )
    has_unsupported_marker = any(token in combined for token in _CACHE_PARAM_UNSUPPORTED_TOKENS)
    if has_cache_signal and has_unsupported_marker:
        return active_fields
    return ()


def _compatible_cache_policy_metadata(
    base_policy: Mapping[str, Any] | None,
    *,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
) -> dict[str, Any] | None:
    active = RequestCachePlan(
        strategy="openai_prompt_cache",
        mode="automatic",
        prompt_cache_key=prompt_cache_key,
        prompt_cache_retention=prompt_cache_retention,
    ).openai_prompt_cache_policy_metadata()
    if active is not None and isinstance(base_policy, Mapping):
        declared_strategy = str(base_policy.get("strategy") or "").strip()
        if declared_strategy and declared_strategy != "none":
            # Compatible key fields describe request activation, not provider
            # cache identity. Keep usage and static-policy calibration aligned.
            active["strategy"] = declared_strategy
    return merge_cache_policy_metadata(base_policy, active)


def _cache_policy_after_fields_disabled(
    cache_policy: Mapping[str, Any] | None,
    *,
    fields: tuple[str, ...],
    fallback: str,
) -> dict[str, Any] | None:
    if not isinstance(cache_policy, Mapping) or not fields:
        return None if cache_policy is None else dict(cache_policy)
    disabled = set(fields)
    updated = dict(cache_policy)
    for key in ("emitted_fields", "allowed_fields"):
        value = updated.get(key)
        if isinstance(value, (list, tuple)):
            updated[key] = [field for field in value if str(field) not in disabled]
    existing_disabled = updated.get("disabled_fields")
    disabled_fields = []
    if isinstance(existing_disabled, (list, tuple)):
        disabled_fields.extend(str(field) for field in existing_disabled)
    disabled_fields.extend(fields)
    unique_disabled_fields = list(dict.fromkeys(disabled_fields))
    updated["disabled_fields"] = unique_disabled_fields
    updated["runtime_disabled_fields"] = unique_disabled_fields
    updated["capability_downgrade"] = "session_local_provider_rejection"
    updated["fallback"] = fallback
    if not updated.get("emitted_fields") and updated.get("status") == "enabled":
        updated["status"] = "available"
    updated["enabled"] = bool(updated.get("emitted_fields"))
    updated["emits_request_fields"] = bool(updated.get("allowed_fields"))
    return updated


def _merge_transport_metadata(
    response: LLMResponse,
    *,
    transport_metadata: dict[str, Any] | None,
) -> LLMResponse:
    if not transport_metadata:
        return response
    provider_metadata = _merge_provider_metadata(
        response.provider_metadata,
        {"transport": transport_metadata},
    )
    return LLMResponse(
        content=response.content,
        tool_calls=list(response.tool_calls),
        raw=response.raw,
        response_model=response.response_model,
        usage=response.usage,
        provider_metadata=provider_metadata,
        reasoning=response.reasoning,
    )


def _merge_request_plan_metadata(
    response: LLMResponse,
    *,
    request_plan_metadata: dict[str, Any] | None,
) -> LLMResponse:
    if not request_plan_metadata:
        return response
    provider_metadata = _merge_provider_metadata(
        response.provider_metadata,
        {"openai_compat": {"request_plan": request_plan_metadata}},
    )
    return LLMResponse(
        content=response.content,
        tool_calls=list(response.tool_calls),
        raw=response.raw,
        response_model=response.response_model,
        usage=response.usage,
        provider_metadata=provider_metadata,
        reasoning=response.reasoning,
    )


def _display_error_body(body: str) -> str:
    if len(body) > _ERROR_BODY_DISPLAY_LIMIT:
        return body[:_ERROR_BODY_DISPLAY_LIMIT] + "...(truncated)"
    return body


def _error_from_status_body(*, status_code: int, body: str) -> LLMError:
    safe_body = sanitize_error_text_for_output(body)
    err = LLMError(f"LLM error {status_code}: {_display_error_body(safe_body)}")
    err.provider_status_code = int(status_code)
    err.provider_error_body = safe_body
    return err


# Error codes and types that providers send in the ``error`` object of an SSE
# event, mapped to the HTTP status whose retry class they share. Numeric codes
# are used as they are; any other value leaves the error to be classified from
# its text, like every other provider error.
_STREAM_ERROR_STATUS_BY_CODE = {
    "rate_limit_exceeded": 429,
    "rate_limit_error": 429,
    "server_error": 500,
    "internal_server_error": 500,
    "api_error": 500,
    "overloaded_error": 503,
    "service_unavailable": 503,
}


def _stream_error_event_status(error: Any) -> int | None:
    if not isinstance(error, Mapping):
        return None
    for key in ("code", "status", "status_code"):
        value = error.get(key)
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
            return value
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str):
            status = _STREAM_ERROR_STATUS_BY_CODE.get(value.strip().casefold())
            if status is not None:
                return status
    return None


def _error_from_stream_error_event(error: Any) -> LLMError:
    """An error a provider sent inside a stream that began with HTTP 200.

    Classified like the HTTP error with the provider's code, so a throttle or
    server error takes the same retry path as its HTTP twin and a permanent
    error stays permanent. Raising discards whatever the stream had delivered.
    """
    body = json.dumps({"error": error}, ensure_ascii=False, default=str)
    status_code = _stream_error_event_status(error)
    if status_code is not None:
        return _error_from_status_body(status_code=status_code, body=body)
    safe_body = sanitize_error_text_for_output(body)
    return LLMError(f"LLM stream returned an error event: {_display_error_body(safe_body)}")


def _response_with_stream_restart_metadata(
    response: LLMResponse,
    *,
    count: int,
    reason: str,
) -> LLMResponse:
    raw = dict(response.raw) if isinstance(response.raw, dict) else {}
    raw["stream_restart_count"] = max(0, int(count))
    if reason:
        raw["stream_restart_reason"] = str(reason)
    return LLMResponse(
        content=response.content,
        tool_calls=list(response.tool_calls),
        raw=raw,
        response_model=response.response_model,
        usage=response.usage,
        provider_metadata=response.provider_metadata,
        reasoning=response.reasoning,
    )


class OpenAICompatClient:
    supports_forced_tool_choice = True

    @property
    def preserves_late_system_message_position(self) -> bool:
        return not (
            _transport_provider_key(
                base_url=self.base_url, provider_key=self.provider_key, model=self.model
            )
            == "alysis"
            and self.model in {"qwen3.8-2.4t", "reflection-beam"}
        )

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        temperature: float = 1.0,
        prompt_cache_key: str | None = None,
        prompt_cache_retention: str | None = None,
        prompt_cache_request_field_values: Mapping[str, Any] | None = None,
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
        transport: httpx.BaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_key: str | None = None,
        reasoning_trace_adapter: str | None = "auto",
        usage_contract: UsageContract | None = None,
        usage_counts_authoritative: bool | None = None,
        provider_concurrency_caps: dict[str, int] | None = None,
        provider_retry_settings: ProviderRetrySettings | None = None,
        provider_sleep_fn: Callable[[float], None] | None = None,
        provider_random_fn: Callable[[], float] | None = None,
        prompt_cache_policy_metadata: Mapping[str, Any] | None = None,
        route_identity: ProviderRouteIdentity | None = None,
        stream_no_progress_timeout_s: float = 240.0,
        stream_progress_clock: Callable[[], float] | None = None,
        inflight_deadline_grace_s: float = 10.0,
        stream_assembly: str | None = STREAM_ASSEMBLY_AUTO,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.prompt_cache_key = str(prompt_cache_key or "").strip() or None
        self.prompt_cache_retention = str(prompt_cache_retention or "").strip() or None
        self.prompt_cache_request_field_values = _safe_cache_request_field_values(
            prompt_cache_request_field_values
        )
        self.enable_thinking = enable_thinking
        self.reasoning_effort = str(reasoning_effort or "").strip().lower() or None
        self._transport = transport
        self._wire_request_diagnostics = WireRequestDiagnostics()
        self.extra_headers = canonicalize_extra_headers(extra_headers)
        self.provider_key = str(provider_key or "").strip() or None
        self.reasoning_trace_adapter = _reasoning_trace_adapter_for_route(
            validate_reasoning_trace_adapter_for_protocol(
                protocol=OPENAI_COMPAT_PROTOCOL,
                adapter=reasoning_trace_adapter,
            ),
            base_url=self.base_url,
        )
        self.stream_assembly = resolve_stream_assembly(
            stream_assembly,
            provider_key=_transport_provider_key(
                base_url=self.base_url,
                provider_key=self.provider_key,
                model=self.model,
            ),
        )
        self.route_identity = route_identity or build_provider_route_identity(
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
            credential_scope=credential_scope_fingerprint(self.api_key),
            routing_headers=self.extra_headers,
            routing_fields=self.prompt_cache_request_field_values,
            reasoning_state_adapter=self.reasoning_trace_adapter,
        )
        if usage_contract is None:
            usage_contract = UsageContract(
                response_usage_confidence=(
                    UsageConfidence.AUTHORITATIVE
                    if usage_counts_authoritative
                    else UsageConfidence.REPORTED
                ),
                input_token_count_strategy="openai_compat_provider_payload",
            )
        self.usage_contract = usage_contract
        self.usage_counts_authoritative = usage_contract.response_usage_authoritative
        self.provider_concurrency_caps = dict(
            DEFAULT_PROVIDER_CONCURRENCY_CAPS
            if provider_concurrency_caps is None
            else provider_concurrency_caps
        )
        self.provider_retry_settings = provider_retry_settings or ProviderRetrySettings()
        self._provider_sleep_fn = provider_sleep_fn
        self._provider_random_fn = provider_random_fn
        self._provider_retry_deadline_allows: Callable[[float], bool] | None = None
        self._provider_retry_event_observer: Callable[[dict[str, object]], None] | None = None
        self.stream_no_progress_timeout_s = max(
            0.001,
            float(stream_no_progress_timeout_s),
        )
        self._stream_progress_clock = stream_progress_clock or monotonic
        self.inflight_deadline_grace_s = max(
            0.0,
            float(inflight_deadline_grace_s),
        )
        self.prompt_cache_policy_metadata = (
            copy.deepcopy(dict(prompt_cache_policy_metadata))
            if isinstance(prompt_cache_policy_metadata, Mapping)
            else None
        )
        self._disabled_prompt_cache_fields: set[str] = set()
        self._disabled_prompt_cache_fields_lock = threading.Lock()
        self._provider_retry_wall_clock_cap_seconds = _PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS
        self._temperature_compat_modes: dict[tuple[str, str], str] = {}
        self._temperature_compat_lock = threading.Lock()
        self._tool_choice_compat_disabled: set[tuple[str, str]] = set()
        self._tool_choice_compat_lock = threading.Lock()
        self._tool_calling_compat_disabled: set[tuple[str, str, str]] = set()
        self._tool_calling_compat_lock = threading.Lock()

    @property
    def reasoning_active(self) -> bool | None:
        """Whether incompatible reasoning is known to be active on this route.

        Agent recovery uses this to avoid manufacturing a forced tool choice
        that the provider's active reasoning mode cannot accept. Contracts that
        accept tool choice, plus unknown custom transports, remain ``None`` and
        keep the legacy capability fallback.
        """

        transport_provider_key = _transport_provider_key(
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
        )
        reasoning_provider_key = _reasoning_transport_provider_key(
            transport_provider_key=transport_provider_key,
            reasoning_trace_adapter=self.reasoning_trace_adapter,
        )
        contract = reasoning_contract_for(reasoning_provider_key, self.model)
        if contract.accepts_tool_choice_while_reasoning:
            return None
        return _reasoning_contract_active(
            provider_key=reasoning_provider_key,
            model=self.model,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self.reasoning_effort,
        )

    def _timeout_belongs_to_run_deadline(self, exc: BaseException) -> bool:
        """Whether a transport timeout is the run budget's narrowed allowance expiring.

        ``temporarily_clamp_client_timeout`` shrinks an attempt's timeout to the
        remaining run budget and installs the probe read here; a timeout after
        that allowance ran out is a budget stop, not a provider stall.
        """
        probe = getattr(self, "_run_deadline_limited_timeout_elapsed", None)
        if not callable(probe) or not isinstance(exc, httpx.TimeoutException):
            return False
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - classification must not change the failure
            return False

    def _temperature_compat_key(self, provider_key: str | None) -> tuple[str, str]:
        provider = _normalize_provider_key(provider_key) or _normalize_provider_key(self.base_url)
        model = str(self.model or "").strip().casefold()
        return provider, model

    def _temperature_compat_mode_for(self, key: tuple[str, str]) -> str | None:
        with self._temperature_compat_lock:
            return self._temperature_compat_modes.get(key)

    def _mark_temperature_compat_mode(self, key: tuple[str, str], mode: str) -> None:
        if mode not in _TEMPERATURE_COMPAT_MODES:
            raise ValueError(f"Unknown temperature compatibility mode: {mode}")
        with self._temperature_compat_lock:
            self._temperature_compat_modes[key] = mode

    def _disabled_prompt_cache_fields_snapshot(self) -> tuple[str, ...]:
        with self._disabled_prompt_cache_fields_lock:
            return tuple(
                field
                for field in _PROMPT_CACHE_FIELDS
                if field in self._disabled_prompt_cache_fields
            )

    def _disable_prompt_cache_fields(self, fields: tuple[str, ...]) -> tuple[str, ...]:
        requested = set(fields)
        clean_fields = tuple(field for field in _PROMPT_CACHE_FIELDS if field in requested)
        if not clean_fields:
            return ()
        with self._disabled_prompt_cache_fields_lock:
            for field in clean_fields:
                self._disabled_prompt_cache_fields.add(field)
            return tuple(
                field
                for field in _PROMPT_CACHE_FIELDS
                if field in self._disabled_prompt_cache_fields
            )

    def _active_prompt_cache_request_field_values(
        self,
        disabled_fields: tuple[str, ...],
    ) -> dict[str, str]:
        disabled = set(disabled_fields)
        safe_values = _safe_cache_request_field_values(self.prompt_cache_request_field_values)
        return {field: value for field, value in safe_values.items() if field not in disabled}

    def _tool_choice_compat_key(self, provider_key: str | None) -> tuple[str, str]:
        provider = _normalize_provider_key(provider_key) or _normalize_provider_key(self.base_url)
        model = str(self.model or "").strip().casefold()
        return provider, model

    def _tool_choice_compat_disabled_for(self, key: tuple[str, str]) -> bool:
        with self._tool_choice_compat_lock:
            return key in self._tool_choice_compat_disabled

    def _mark_tool_choice_compat_disabled(self, key: tuple[str, str]) -> None:
        with self._tool_choice_compat_lock:
            self._tool_choice_compat_disabled.add(key)

    def _tool_calling_compat_key(self, provider_key: str | None) -> tuple[str, str, str]:
        provider = _normalize_provider_key(provider_key) or _normalize_provider_key(self.base_url)
        model = str(self.model or "").strip().casefold()
        base_url = str(self.base_url or "").strip().casefold()
        return provider, model, base_url

    def _tool_calling_compat_disabled_for(self, key: tuple[str, str, str]) -> bool:
        with self._tool_calling_compat_lock:
            return key in self._tool_calling_compat_disabled

    def _mark_tool_calling_compat_disabled(self, key: tuple[str, str, str]) -> None:
        with self._tool_calling_compat_lock:
            self._tool_calling_compat_disabled.add(key)

    @property
    def supports_tool_calling(self) -> bool:
        provider_key = _transport_provider_key(
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
        )
        return not self._tool_calling_compat_disabled_for(
            self._tool_calling_compat_key(provider_key)
        )

    def _prompt_payload_for_estimation(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], str | None]:
        messages = gate_messages_for_provider_route(messages, self.route_identity)
        transport_provider_key = _transport_provider_key(
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
        )
        reasoning_provider_key = _reasoning_transport_provider_key(
            transport_provider_key=transport_provider_key,
            reasoning_trace_adapter=self.reasoning_trace_adapter,
        )
        disabled_prompt_cache_fields = self._disabled_prompt_cache_fields_snapshot()
        active_cache_field_values = self._active_prompt_cache_request_field_values(
            disabled_prompt_cache_fields
        )
        effective_tools = tools
        if self._tool_calling_compat_disabled_for(
            self._tool_calling_compat_key(transport_provider_key)
        ):
            effective_tools = None

        provider_messages = _messages_for_transport(
            messages,
            provider_key=transport_provider_key,
            reasoning_provider_key=reasoning_provider_key,
            model=self.model,
        )
        if CACHE_CONTROL_FIELD in active_cache_field_values:
            cache_policy = _compatible_cache_policy_metadata(
                self.prompt_cache_policy_metadata,
                prompt_cache_key=self.prompt_cache_key,
                prompt_cache_retention=self.prompt_cache_retention,
            )
            provider_messages = apply_openai_compatible_cache_control_breakpoint(
                provider_messages,
                cache_policy=cache_policy,
            ).messages

        prompt_payload: dict[str, Any] = {"messages": provider_messages}
        if effective_tools:
            prompt_payload["tools"] = effective_tools
        prompt_payload = _sanitize_transport_value(prompt_payload)
        return prompt_payload, transport_provider_key

    def request_capacity_ratio(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> float:
        """Measure hosted admission pressure separately from token accounting."""
        provider = _transport_provider_key(
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
        )
        if provider != "alysis":
            return 0.0
        prompt_payload, _ = self._prompt_payload_for_estimation(messages, tools)
        # Control fields are small; reserve headroom for their exact wire shape.
        return hosted_chat_capacity_ratio(
            {**prompt_payload, "model": self.model, "stream": True},
            headroom=4096,
        )

    def count_input_tokens(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> InputTokenCount:
        """Estimate provider-shaped prompt tokens with explicit local provenance."""
        prompt_payload, transport_provider_key = self._prompt_payload_for_estimation(
            messages, tools
        )
        return InputTokenCount(
            input_tokens=estimate_provider_payload_tokens(prompt_payload),
            source=UsageSource.LOCAL_ESTIMATE,
            confidence=UsageConfidence.ESTIMATED,
            raw_provider_usage={
                "estimator": "cl100k_base",
                "estimate_basis": "provider_prompt_payload",
                "provider_key": transport_provider_key,
                "protocol": "openai_compat",
                "model": self.model,
                "message_count": len(prompt_payload.get("messages") or []),
                "tool_count": len(prompt_payload.get("tools") or []),
            },
        )

    def _trace_capability(self, provider_key: str | None) -> ReasoningTraceCapability:
        capability = getattr(self, "reasoning_trace_capability", None)
        if capability is not None:
            return capability
        return resolve_reasoning_trace_capability(
            provider_key=provider_key or "",
            protocol=OPENAI_COMPAT_PROTOCOL,
            adapter_override=self.reasoning_trace_adapter,
        )

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse:
        wire_diagnostics = self._wire_request_diagnostics.begin_request()
        on_reasoning_delta = reasoning_trace_callback(
            on_reasoning_delta,
            capability=self._trace_capability(
                _transport_provider_key(
                    base_url=self.base_url, provider_key=self.provider_key, model=self.model
                )
            ),
            stream=stream,
        )
        messages = gate_messages_for_provider_route(messages, self.route_identity)
        url = f"{self.base_url}/chat/completions"
        headers = merge_canonical_headers(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "alysis-code/0.1.0",
            },
            self.extra_headers,
        )
        headers = _headers_with_default_accept_encoding(headers)
        resolved_temperature = self.temperature if temperature is None else float(temperature)
        transport_provider_key = _transport_provider_key(
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
        )
        reasoning_provider_key = _reasoning_transport_provider_key(
            transport_provider_key=transport_provider_key,
            reasoning_trace_adapter=self.reasoning_trace_adapter,
        )
        deepseek_thinking_enabled = (
            _deepseek_reasoning_payload_enabled(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self.reasoning_effort,
            )
            if _is_deepseek_provider(reasoning_provider_key)
            else None
        )
        documented_temperature_reason = documented_temperature_omit_reason(
            self.model,
            provider_key=transport_provider_key,
            thinking_enabled=deepseek_thinking_enabled,
            reasoning_effort=self.reasoning_effort,
        )
        temperature_key = self._temperature_compat_key(transport_provider_key)
        cached_temperature_compat_mode = self._temperature_compat_mode_for(temperature_key)
        disabled_prompt_cache_fields = self._disabled_prompt_cache_fields_snapshot()
        active_cache_field_values = self._active_prompt_cache_request_field_values(
            disabled_prompt_cache_fields
        )
        active_prompt_cache_key = (
            None
            if PROMPT_CACHE_KEY_FIELD in disabled_prompt_cache_fields
            else self.prompt_cache_key or active_cache_field_values.get(PROMPT_CACHE_KEY_FIELD)
        )
        active_prompt_cache_retention = (
            None
            if PROMPT_CACHE_RETENTION_FIELD in disabled_prompt_cache_fields
            else self.prompt_cache_retention
            or active_cache_field_values.get(PROMPT_CACHE_RETENTION_FIELD)
        )
        tool_choice_key = self._tool_choice_compat_key(transport_provider_key)
        cached_tool_choice_compat_disabled = self._tool_choice_compat_disabled_for(tool_choice_key)
        tool_calling_key = self._tool_calling_compat_key(transport_provider_key)
        cached_tool_calling_compat_disabled = self._tool_calling_compat_disabled_for(
            tool_calling_key
        )
        transport_metadata: dict[str, Any] = {}
        reasoning_contract = reasoning_contract_for(reasoning_provider_key, self.model)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages_for_transport(
                messages,
                provider_key=transport_provider_key,
                reasoning_provider_key=reasoning_provider_key,
                model=self.model,
            ),
        }
        if documented_temperature_reason is not None:
            transport_metadata["temperature_adjusted"] = True
            transport_metadata["temperature_adjustment"] = _TEMPERATURE_COMPAT_MODE_OMIT
            transport_metadata["temperature_adjustment_reason"] = "documented_model_policy"
            transport_metadata["temperature_omitted"] = True
            transport_metadata["temperature_omit_reason"] = documented_temperature_reason
        elif cached_temperature_compat_mode == _TEMPERATURE_COMPAT_MODE_DEFAULT:
            payload["temperature"] = _TEMPERATURE_DEFAULT_VALUE
            transport_metadata["temperature_adjusted"] = True
            transport_metadata["temperature_adjustment"] = _TEMPERATURE_COMPAT_MODE_DEFAULT
            transport_metadata["temperature_adjustment_reason"] = "cached_provider_rejection"
        elif cached_temperature_compat_mode == _TEMPERATURE_COMPAT_MODE_OMIT:
            transport_metadata["temperature_adjusted"] = True
            transport_metadata["temperature_adjustment"] = _TEMPERATURE_COMPAT_MODE_OMIT
            transport_metadata["temperature_adjustment_reason"] = "cached_provider_rejection"
            transport_metadata["temperature_omitted"] = True
            transport_metadata["temperature_omit_reason"] = "cached_provider_rejection"
        else:
            payload["temperature"] = resolved_temperature
        if active_prompt_cache_key:
            payload[PROMPT_CACHE_KEY_FIELD] = active_prompt_cache_key
        if active_prompt_cache_retention:
            payload[PROMPT_CACHE_RETENTION_FIELD] = active_prompt_cache_retention
        openrouter_session_id = active_cache_field_values.get(OPENROUTER_SESSION_ID_FIELD)
        if openrouter_session_id:
            payload[OPENROUTER_SESSION_ID_FIELD] = openrouter_session_id
        _set_header_if_absent(
            headers,
            OPENROUTER_SESSION_ID_HEADER_FIELD,
            active_cache_field_values.get(OPENROUTER_SESSION_ID_HEADER_FIELD, ""),
        )
        _set_header_if_absent(
            headers,
            XAI_CONVERSATION_ID_HEADER_FIELD,
            active_cache_field_values.get(XAI_CONVERSATION_ID_HEADER_FIELD, ""),
        )
        # Track whether this request runs in thinking/reasoning mode on a provider
        # that rejects a forced tool_choice while thinking (DeepSeek, OpenRouter/
        # MiMo, DashScope/Qwen, Zhipu GLM). OpenAI/Gemini-style reasoning accept a
        # forced choice, so those branches leave this False.
        thinking_active = False
        if _is_moonshot_provider(transport_provider_key):
            # Moonshot Platform and Kimi Code share response-state behavior but
            # expose different request contracts. Resolve the endpoint surface
            # first, then shape the request from its contract rather than from a
            # model-name heuristic.
            contract = reasoning_contract_for(transport_provider_key, self.model)
            if contract.wire == WIRE_REASONING_EFFORT:
                reasoning_effort = _documented_reasoning_effort(
                    provider_key=transport_provider_key,
                    model=self.model,
                    reasoning_effort=self.reasoning_effort,
                )
                if not reasoning_effort and contract.mode == ALWAYS_ON:
                    reasoning_effort = contract.default or None
                if reasoning_effort:
                    payload["reasoning_effort"] = reasoning_effort
            elif contract.wire == WIRE_THINKING_TYPE and contract.mode == ALWAYS_ON:
                thinking_active = True
            elif contract.wire == WIRE_THINKING_TYPE:
                thinking_enabled = self.enable_thinking
                if thinking_enabled is None:
                    thinking_enabled = _reasoning_effort_enables_thinking(self.reasoning_effort)
                # Optional Moonshot thinking defaults on when the toggle is
                # omitted; preserve that default for forced-tool handling.
                thinking_active = thinking_enabled is not False
                if thinking_enabled is not None:
                    payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
        elif _is_dashscope_provider(reasoning_provider_key):
            enable_thinking = self.enable_thinking
            if enable_thinking is None:
                enable_thinking = _reasoning_effort_enables_thinking(self.reasoning_effort)
            if enable_thinking is not None:
                payload["enable_thinking"] = enable_thinking
            reasoning_effort = _documented_reasoning_effort(
                provider_key=reasoning_provider_key,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
            )
            if enable_thinking is not False and reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
            thinking_active = enable_thinking is True
        elif _is_deepseek_provider(reasoning_provider_key):
            thinking_enabled = deepseek_thinking_enabled
            if thinking_enabled is not None:
                payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
            reasoning_effort = _documented_reasoning_effort(
                provider_key=reasoning_provider_key,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
            )
            if thinking_enabled is not False and reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
            thinking_active = (
                _reasoning_contract_active(
                    provider_key=reasoning_provider_key,
                    model=self.model,
                    enable_thinking=self.enable_thinking,
                    reasoning_effort=self.reasoning_effort,
                )
                is True
            )
        elif _is_together_deepseek_pro(transport_provider_key, self.model):
            # Together exposes DeepSeek's effort field but uses its own object
            # shape for disabling reasoning. The model otherwise reasons by
            # default, so omission preserves provider behavior.
            if self.enable_thinking is False:
                payload["reasoning"] = {"enabled": False}
            else:
                reasoning_effort = _documented_reasoning_effort(
                    provider_key=_TOGETHER_PROVIDER_KEY,
                    model=self.model,
                    reasoning_effort=self.reasoning_effort,
                )
                if reasoning_effort:
                    payload["reasoning_effort"] = reasoning_effort
        elif _is_openrouter_provider(reasoning_provider_key):
            always_thinking = reasoning_contract.mode == ALWAYS_ON
            if always_thinking and (
                self.enable_thinking is False
                or str(self.reasoning_effort or "").strip().casefold() == "none"
            ):
                raise LLMError(
                    f"OpenRouter model {self.model!r} does not support disabling thinking"
                )
            reasoning = _openrouter_reasoning_payload(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self.reasoning_effort,
            )
            if reasoning is not None:
                payload["reasoning"] = reasoning
            thinking_active = always_thinking or (
                reasoning is not None and reasoning.get("enabled") is not False
            )
        elif _is_gemini_provider(reasoning_provider_key):
            reasoning_effort = _gemini_reasoning_effort(
                model=self.model,
                reasoning_effort=self.reasoning_effort,
            )
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
        elif _is_nvidia_provider(reasoning_provider_key):
            # NVIDIA hosts models from many vendors behind one endpoint. Request
            # shaping is therefore model-contract-driven: the host alone never
            # implies Nemotron controls, and unknown models receive no guessed
            # reasoning parameter.
            contract = reasoning_contract_for(_NVIDIA_PROVIDER_KEY, self.model)
            thinking_enabled = self.enable_thinking
            if thinking_enabled is None:
                thinking_enabled = _reasoning_effort_enables_thinking(self.reasoning_effort)
            chat_template_kwargs: dict[str, bool] = {}
            if contract.wire == WIRE_REASONING_EFFORT:
                if thinking_enabled is False and contract.off == OFF_EXPLICIT:
                    payload["reasoning_effort"] = "none"
                elif thinking_enabled is not False:
                    reasoning_effort = _documented_reasoning_effort(
                        provider_key=_NVIDIA_PROVIDER_KEY,
                        model=self.model,
                        reasoning_effort=self.reasoning_effort,
                    )
                    if reasoning_effort:
                        payload["reasoning_effort"] = reasoning_effort
            elif (
                contract.wire == WIRE_CHAT_TEMPLATE_ENABLE_THINKING and thinking_enabled is not None
            ):
                chat_template_kwargs["enable_thinking"] = thinking_enabled
            if chat_template_kwargs:
                payload["chat_template_kwargs"] = chat_template_kwargs
        elif reasoning_contract.wire == WIRE_THINKING_TYPE and reasoning_contract.toggleable:
            thinking_type = self.reasoning_effort
            if thinking_type not in reasoning_contract.values:
                thinking_type = (
                    "disabled"
                    if self.enable_thinking is False
                    else reasoning_contract.default or "enabled"
                )
            if self.enable_thinking is not None or self.reasoning_effort:
                payload["thinking"] = {"type": thinking_type}
            thinking_active = self.enable_thinking is not False
        elif reasoning_contract.wire == WIRE_REASONING_ENABLED and reasoning_contract.toggleable:
            if self.enable_thinking is not None:
                payload["reasoning"] = {"enabled": self.enable_thinking}
            thinking_active = self.enable_thinking is not False
        else:
            # Any researched provider contract that uses the flat Chat
            # Completions effort field can opt in here without another
            # provider-specific request branch. Unknown routes retain the
            # legacy OpenAI/Azure/Mistral behavior and receive no newly guessed
            # controls.
            documented_effort = _documented_flat_reasoning_effort(
                provider_key=reasoning_provider_key,
                model=self.model,
                enable_thinking=self.enable_thinking,
                reasoning_effort=self.reasoning_effort,
            )
            if documented_effort:
                payload["reasoning_effort"] = documented_effort
            elif _uses_reasoning_effort(reasoning_provider_key) and self.reasoning_effort:
                payload["reasoning_effort"] = self.reasoning_effort
        # A reasoning model in thinking mode can 400 on any tool_choice parameter.
        # Omitting it preserves the default "auto" behavior while keeping tools
        # available to the model.
        if (
            thinking_active
            and tool_choice is not None
            and (
                not reasoning_contract.accepts_tool_choice_while_reasoning
                or _tool_choice_forces_a_call(tool_choice)
            )
        ):
            tool_choice = None
            transport_metadata["tool_choice_omitted"] = True
            transport_metadata["tool_choice_omit_reason"] = "thinking_mode"
        elif cached_tool_choice_compat_disabled and tool_choice is not None:
            tool_choice = None
            transport_metadata["tool_choice_omitted"] = True
            transport_metadata["tool_choice_omit_reason"] = "cached_provider_rejection"
        if cached_tool_calling_compat_disabled:
            required_tool_names = required_tool_names_for_client(self)
            if required_tool_names:
                raise RequiredToolCallingUnsupportedError(
                    "The selected model or provider does not support the required Computer Use "
                    f"tools ({', '.join(required_tool_names)}). Choose a tool-capable model or "
                    "provider."
                )
        if cached_tool_calling_compat_disabled and (tools or tool_choice is not None):
            tools = None
            tool_choice = None
            transport_metadata["tools_omitted"] = True
            transport_metadata["tools_omit_reason"] = "cached_provider_rejection"
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        elif tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format
        if max_tokens is not None:
            output_limit_field = (
                "max_completion_tokens"
                if _uses_max_completion_tokens(transport_provider_key, base_url=self.base_url)
                else "max_tokens"
            )
            payload[output_limit_field] = int(max_tokens)
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        provider_key = (
            transport_provider_key
            or self.provider_key
            or best_effort_provider_key(
                base_url=self.base_url,
                model=self.model,
            )
        )
        cache_policy = _compatible_cache_policy_metadata(
            self.prompt_cache_policy_metadata,
            prompt_cache_key=active_prompt_cache_key,
            prompt_cache_retention=active_prompt_cache_retention,
        )
        if CACHE_CONTROL_FIELD in active_cache_field_values:
            application = apply_openai_compatible_cache_control_breakpoint(
                payload.get("messages", []),
                cache_policy=cache_policy,
            )
            payload["messages"] = application.messages
            if cache_policy is not None:
                cache_policy = merge_cache_policy_metadata(
                    cache_policy,
                    application.policy_metadata(),
                )
        if disabled_prompt_cache_fields:
            cache_policy = _cache_policy_after_fields_disabled(
                cache_policy,
                fields=disabled_prompt_cache_fields,
                fallback="runtime_disabled_rejected_cache_fields",
            )
        # Sampling determinism controls, applied last so this is the single
        # place temperature/top_p/seed can enter the request. With none of the
        # three configured -- the default -- this writes nothing and the
        # payload keeps the exact keys, order and values the branches above
        # produced, so the request stays byte-identical to the pre-PR6
        # transport. The temperature override is declined whenever the
        # transport already rewrote or omitted the field for
        # provider-compatibility reasons, because that adjustment exists to
        # avoid a 400 this must not reintroduce.
        sampling_settings = active_sampling_settings()
        applied_sampling_fields = apply_sampling_to_payload(
            payload,
            sampling_settings,
            allow_temperature_override=not transport_metadata.get("temperature_adjusted"),
        )
        if applied_sampling_fields:
            transport_metadata["sampling_fields_applied"] = list(applied_sampling_fields)
        payload = _sanitize_transport_value(payload)
        if transport_provider_key == "alysis":
            validate_hosted_chat_capacity(payload)
        prompt_estimation_payload = {
            "messages": payload.get("messages", []),
        }
        for key in ("tools", "response_format"):
            if key in payload:
                prompt_estimation_payload[key] = payload[key]
        request_shape = build_request_shape_report(
            messages=messages,
            tools=tools,
            cache_policy=cache_policy,
            provider_payload=prompt_estimation_payload,
        )
        input_estimate_tokens = estimate_provider_payload_tokens(prompt_estimation_payload)
        layout_plan = LLMRequestPlan.from_chat_args(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            cache=RequestCachePlan(
                strategy=(
                    "openai_prompt_cache"
                    if active_prompt_cache_key or active_prompt_cache_retention
                    else "none"
                ),
                mode=(
                    "automatic"
                    if active_prompt_cache_key or active_prompt_cache_retention
                    else "manual"
                ),
                prompt_cache_key=active_prompt_cache_key,
                prompt_cache_retention=active_prompt_cache_retention,
            ),
        )
        request_plan_metadata = layout_plan.request_plan_metadata(
            input_mode="full",
            continuation_strategy="full_replay",
            provider_payload=prompt_estimation_payload,
            sent_provider_payload=prompt_estimation_payload,
            cache_policy_metadata=cache_policy,
        )
        request_plan_metadata.update(wire_diagnostics(payload, history_key="messages"))
        token_reconciliation = {
            "input_estimate_tokens": input_estimate_tokens,
            "sent_input_estimate_tokens": input_estimate_tokens,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
            "input_mode": "full",
        }
        telemetry = ProviderCallTelemetryRecorder(
            provider_key=provider_key,
            protocol="openai_compat",
            model=self.model,
            base_url=self.base_url,
            stream=stream,
            tools=tools,
            cache_policy=cache_policy,
            request_plan=request_plan_metadata,
            request_shape=request_shape,
            token_reconciliation=token_reconciliation,
            operation="chat_completions",
            sampling=sampling_settings,
        )
        telemetry_on_text_delta = telemetry.wrap_text_delta(on_text_delta)
        telemetry_on_reasoning_delta = telemetry.wrap_reasoning_delta(on_reasoning_delta)
        stream_restart_count = 0
        stream_restart_reason = ""
        any_text_delta_emitted = False
        # Provider-level attempts that actually began: _send_request runs once
        # per attempt. A retry scheduled by the retry loop but cancelled during
        # its backoff never gets here, so it is not counted as a retry.
        provider_attempts_started = 0

        def _record_retry(attempt: int, reason: str, wait_seconds: float) -> None:
            nonlocal stream_restart_count, stream_restart_reason, any_text_delta_emitted
            telemetry.on_retry(attempt, reason, wait_seconds)
            if stream and (
                str(reason).startswith("provider_stream_")
                or reason == RETRY_REASON_TRANSPORT_CONNECTION_DROP
            ):
                stream_restart_count += 1
                stream_restart_reason = str(reason)
                transport_metadata["stream_restart_count"] = stream_restart_count
                transport_metadata["stream_restart_reason"] = stream_restart_reason
            if stream and any_text_delta_emitted:
                # Tokens from the abandoned attempt already rendered; give the
                # surface a chance to reset its live block before the retry
                # restreams the reply (duck-typed hook — see the chat loop's
                # _on_text_delta). Observers must never change retry behavior.
                any_text_delta_emitted = False
                restart_hook = getattr(on_text_delta, "stream_restart", None)
                if callable(restart_hook):
                    try:
                        restart_hook()
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug("stream_restart_hook_failed", exc_info=True)

        @contextmanager
        def _recorded_request(**kwargs: Any) -> Iterator[tuple[httpx.Response, _HTTPAttempt]]:
            # Recompute from the final payload, including compatibility fallbacks;
            # the comparator remains the previous logical request across retries.
            request_plan_metadata.update(wire_diagnostics(payload, history_key="messages"))
            telemetry.set_request_plan(request_plan_metadata)
            attempt = _HTTPAttempt(stream=bool(kwargs.get("stream")))
            # 0 for the first provider-level attempt, n for its n-th retry;
            # compatibility re-sends inside one attempt share its index.
            attempt.values["retry_index"] = max(0, provider_attempts_started - 1)
            try:
                with cancellable_httpx_request(**kwargs) as resp:
                    attempt.values["status_code"] = resp.status_code
                    if resp.status_code >= 400:
                        # Error bodies are consumed by the existing error parser,
                        # outside the streaming byte observer. Their size is unknown.
                        attempt.values["body_bytes_received"] = None
                        attempt.values["byte_chunk_count"] = None
                    yield resp, attempt
                    attempt.values["terminal_reason"] = "done" if stream else "response"
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, CooperativeCancellationError)):
                    reason = "cancelled"
                elif isinstance(exc, DeadlineExhausted) or self._timeout_belongs_to_run_deadline(
                    exc
                ):
                    reason = "budget_abort"
                elif isinstance(exc, (LLMStreamNoProgressError, httpx.ReadTimeout)):
                    # A stall once the first event has arrived; before it the
                    # provider never answered. A non-streaming response is its
                    # own first event, so its timeouts are all first-event ones.
                    reason = (
                        "timeout_stall"
                        if "first_event_ms" in attempt.values
                        else "timeout_first_event"
                    )
                elif attempt.values.get("status_code", 0) >= 400:
                    reason = "http_error"
                elif isinstance(exc, httpx.RequestError):
                    reason = "transport_error"
                elif isinstance(exc, LLMError):
                    reason = "invalid_response"
                else:
                    reason = "client_error"
                attempt.values.setdefault("terminal_reason", reason)
                if isinstance(exc, Exception) and not isinstance(
                    exc,
                    (LLMError, httpx.RequestError, DeadlineExhausted, CooperativeCancellationError),
                ):
                    error = _StreamClientError("LLM response processing failed in the client")
                    mark_provider_call_non_retryable(error)
                    raise error from exc
                raise
            finally:
                attempt.values["latency_ms"] = attempt.elapsed_ms()
                telemetry.record_attempt(attempt.values)

        def _send_request() -> LLMResponse:
            nonlocal cache_policy, request_plan_metadata, request_shape, token_reconciliation
            nonlocal stream_restart_count, stream_restart_reason, provider_attempts_started
            telemetry.note_attempt_started(retry_index=provider_attempts_started)
            provider_attempts_started += 1
            temperature_retry_count = 0
            temperature_retry_modes: set[str] = set()
            cache_param_retry_used = False
            try:
                client_context = (
                    cancellation_aware_httpx_client(
                        timeout_s=_httpx_request_timeout(self.timeout_s),
                        transport=self._transport,
                        cancellation_token=cancellation_token,
                    )
                    if cancellation_token is not None and not stream
                    else provider_httpx_client(
                        timeout=_httpx_request_timeout(self.timeout_s),
                        transport=self._transport,
                    )
                )
                with client_context as client:
                    while True:
                        try:
                            if stream:
                                with _recorded_request(
                                    client=client,
                                    cancellation_token=cancellation_token,
                                    method="POST",
                                    url=url,
                                    stream=True,
                                    headers=headers,
                                    json=payload,
                                ) as (resp, attempt):
                                    # Before parsing: the routing headers are
                                    # the evidence for which backend answered,
                                    # and they must survive a stream that then
                                    # fails or is retried.
                                    telemetry.set_response_headers(resp.headers)
                                    attempt_deltas: list[str] = []
                                    attempt_reasoning_deltas: list[str] = []

                                    def _attempt_text_delta(
                                        delta: str,
                                        *,
                                        _attempt_deltas: list[str] = attempt_deltas,
                                    ) -> None:
                                        nonlocal any_text_delta_emitted
                                        _attempt_deltas.append(delta)
                                        if delta:
                                            any_text_delta_emitted = True
                                        if telemetry_on_text_delta is not None:
                                            try:
                                                telemetry_on_text_delta(delta)
                                            except Exception as exc:
                                                attempt.values["terminal_reason"] = "callback_error"
                                                error = _StreamClientError(
                                                    "LLM text callback failed"
                                                )
                                                mark_provider_call_non_retryable(error)
                                                raise error from exc

                                    def _attempt_reasoning_delta(
                                        delta: str,
                                        *,
                                        _attempt_reasoning_deltas: list[str] = (
                                            attempt_reasoning_deltas
                                        ),
                                    ) -> None:
                                        _attempt_reasoning_deltas.append(delta)
                                        if telemetry_on_reasoning_delta is not None:
                                            try:
                                                telemetry_on_reasoning_delta(delta)
                                            except Exception as exc:
                                                attempt.values["terminal_reason"] = "callback_error"
                                                error = _StreamClientError(
                                                    "LLM reasoning callback failed"
                                                )
                                                mark_provider_call_non_retryable(error)
                                                raise error from exc

                                    try:
                                        response = self._parse_stream_response(
                                            resp,
                                            on_text_delta=(
                                                _attempt_text_delta
                                                if on_text_delta is not None
                                                else None
                                            ),
                                            on_reasoning_delta=(
                                                _attempt_reasoning_delta
                                                if on_reasoning_delta is not None
                                                else None
                                            ),
                                            provider_key=reasoning_provider_key,
                                            cancellation_token=cancellation_token,
                                            manage_abort_callback=False,
                                            attempt=attempt,
                                        )
                                    except Exception as stream_error:
                                        if isinstance(stream_error, DeadlineExhausted):
                                            raise
                                        if attempt_deltas or attempt_reasoning_deltas:
                                            stream_restart_count += 1
                                            stream_restart_reason = (
                                                provider_unavailable_retry_reason(stream_error)
                                                or "provider_stream_interrupted"
                                            )
                                            transport_metadata["stream_restart_count"] = (
                                                stream_restart_count
                                            )
                                            transport_metadata["stream_restart_reason"] = (
                                                stream_restart_reason
                                            )
                                            # Once public output reached the UI, replaying the
                                            # request would duplicate text/trace content —
                                            # UNLESS the delta callback carries the
                                            # stream_restart reset channel, in which case the
                                            # surface erases the partial output right before
                                            # the retry restreams (the hook fires from
                                            # _record_retry, i.e. only when a retry actually
                                            # runs), and replaying is safe.
                                            surface_can_reset = callable(
                                                getattr(on_text_delta, "stream_restart", None)
                                            )
                                            if surface_can_reset:
                                                raise
                                            if isinstance(stream_error, LLMError):
                                                mark_provider_call_non_retryable(stream_error)
                                                raise
                                            interrupted = LLMError(
                                                "LLM stream interrupted after partial output: "
                                                f"{stream_error}"
                                            )
                                            mark_provider_call_non_retryable(interrupted)
                                            raise interrupted from stream_error
                                        raise
                                    if stream_restart_count:
                                        response = _response_with_stream_restart_metadata(
                                            response,
                                            count=stream_restart_count,
                                            reason=stream_restart_reason,
                                        )
                            else:
                                with _recorded_request(
                                    client=client,
                                    cancellation_token=cancellation_token,
                                    method="POST",
                                    url=url,
                                    stream=False,
                                    headers=headers,
                                    json=payload,
                                ) as (resp, attempt):
                                    # Captured before the status check so a 4xx/5xx
                                    # keeps its request id, which is usually the
                                    # only handle a provider accepts when asked
                                    # what happened to a specific call.
                                    telemetry.set_response_headers(resp.headers)
                                    if resp.status_code >= 400:
                                        raise self._error_from_response(resp)
                                    response = self._parse_non_stream_response(
                                        resp,
                                        provider_key=reasoning_provider_key,
                                        model=self.model,
                                        trace_adapter=self._trace_capability(
                                            reasoning_provider_key
                                        ).adapter,
                                    )
                                    attempt.values["usage"] = response.usage
                                    attempt.values["usage_observed"] = response.usage is not None
                                    if response.usage is not None:
                                        attempt.values["usage_source"] = "provider"
                        except LLMError as e:
                            if (
                                stream
                                and "stream_options" in payload
                                and _is_stream_options_unsupported_error(e)
                            ):
                                payload.pop("stream_options", None)
                                continue
                            rejected_cache_fields = _cache_param_rejected_fields(
                                e,
                                payload=payload,
                                headers=headers,
                            )
                            if rejected_cache_fields and not cache_param_retry_used:
                                cache_param_retry_used = True
                                disabled_fields = self._disable_prompt_cache_fields(
                                    rejected_cache_fields
                                )
                                _strip_cache_request_fields(
                                    payload=payload,
                                    headers=headers,
                                    fields=rejected_cache_fields,
                                )
                                cache_policy = _cache_policy_after_fields_disabled(
                                    cache_policy,
                                    fields=disabled_fields or rejected_cache_fields,
                                    fallback="stripped_rejected_cache_fields",
                                )
                                prompt_estimation_payload = {
                                    "messages": payload.get("messages", []),
                                }
                                for key in ("tools", "response_format"):
                                    if key in payload:
                                        prompt_estimation_payload[key] = payload[key]
                                request_shape = build_request_shape_report(
                                    messages=messages,
                                    tools=tools,
                                    cache_policy=cache_policy,
                                    provider_payload=prompt_estimation_payload,
                                    input_mode="cache_param_fallback",
                                )
                                request_plan_metadata = layout_plan.request_plan_metadata(
                                    input_mode="cache_param_fallback",
                                    continuation_strategy="full_replay",
                                    provider_payload=prompt_estimation_payload,
                                    sent_provider_payload=prompt_estimation_payload,
                                    cache_policy_metadata=cache_policy,
                                    extra={"fallback_used": True},
                                )
                                fallback_input_estimate = estimate_provider_payload_tokens(
                                    prompt_estimation_payload
                                )
                                token_reconciliation = {
                                    **token_reconciliation,
                                    "input_estimate_tokens": fallback_input_estimate,
                                    "sent_input_estimate_tokens": fallback_input_estimate,
                                    "input_mode": "cache_param_fallback",
                                }
                                telemetry.set_cache_policy(cache_policy)
                                telemetry.set_request_plan(request_plan_metadata)
                                telemetry.set_request_shape(request_shape)
                                telemetry.set_token_reconciliation(token_reconciliation)
                                transport_metadata["cache_param_fallback_used"] = True
                                transport_metadata["cache_param_retry_used"] = True
                                transport_metadata["cache_param_disabled_fields"] = list(
                                    disabled_fields or rejected_cache_fields
                                )
                                _LOGGER.info(
                                    "llm_cache_parameter_rejected_retrying_without_cache_fields",
                                    extra={
                                        "provider_key": provider_key,
                                        "model": self.model,
                                        "disabled_fields": list(
                                            disabled_fields or rejected_cache_fields
                                        ),
                                    },
                                )
                                continue
                            if "tool_choice" in payload and _tool_choice_unsupported_error(e):
                                rejected_tool_choice = payload.pop("tool_choice", None)
                                self._mark_tool_choice_compat_disabled(tool_choice_key)
                                transport_metadata["tool_choice_omitted"] = True
                                transport_metadata["tool_choice_omit_reason"] = (
                                    "provider_rejected_parameter"
                                )
                                transport_metadata["tool_choice_retry_used"] = True
                                _LOGGER.info(
                                    "llm_tool_choice_parameter_rejected_retrying_without_it",
                                    extra={
                                        "provider_key": provider_key,
                                        "model": self.model,
                                        "tool_choice": rejected_tool_choice,
                                    },
                                )
                                continue
                            if "tools" in payload and _tool_calling_unsupported_error(e):
                                self._mark_tool_calling_compat_disabled(tool_calling_key)
                                required_tool_names = required_tool_names_for_client(self)
                                if required_tool_names:
                                    raise RequiredToolCallingUnsupportedError(
                                        "The selected model or provider rejected the required "
                                        "Computer Use tools "
                                        f"({', '.join(required_tool_names)}). Choose a "
                                        "tool-capable model or provider."
                                    ) from e
                                payload.pop("tools", None)
                                payload.pop("tool_choice", None)
                                fallback_prompt_payload: dict[str, Any] = {
                                    "messages": payload.get("messages", []),
                                }
                                if "response_format" in payload:
                                    fallback_prompt_payload["response_format"] = payload[
                                        "response_format"
                                    ]
                                request_shape = build_request_shape_report(
                                    messages=messages,
                                    tools=None,
                                    cache_policy=cache_policy,
                                    provider_payload=fallback_prompt_payload,
                                    input_mode="tool_calling_fallback",
                                )
                                fallback_layout = LLMRequestPlan.from_chat_args(
                                    messages=messages,
                                    tools=None,
                                    tool_choice=None,
                                    response_format=response_format,
                                    stream=stream,
                                    temperature=temperature,
                                    max_tokens=max_tokens,
                                    cache=layout_plan.cache,
                                )
                                request_plan_metadata = fallback_layout.request_plan_metadata(
                                    input_mode="tool_calling_fallback",
                                    continuation_strategy="full_replay",
                                    provider_payload=fallback_prompt_payload,
                                    sent_provider_payload=fallback_prompt_payload,
                                    cache_policy_metadata=cache_policy,
                                    extra={"fallback_used": True},
                                )
                                fallback_input_estimate = estimate_provider_payload_tokens(
                                    fallback_prompt_payload
                                )
                                token_reconciliation = {
                                    **token_reconciliation,
                                    "input_estimate_tokens": fallback_input_estimate,
                                    "sent_input_estimate_tokens": fallback_input_estimate,
                                    "input_mode": "tool_calling_fallback",
                                }
                                telemetry.set_request_plan(request_plan_metadata)
                                telemetry.set_request_shape(request_shape)
                                telemetry.set_token_reconciliation(token_reconciliation)
                                transport_metadata["tools_omitted"] = True
                                transport_metadata["tools_omit_reason"] = (
                                    "provider_rejected_tool_calling"
                                )
                                transport_metadata["tools_retry_used"] = True
                                _LOGGER.info(
                                    "llm_tool_calling_rejected_retrying_without_tools",
                                    extra={
                                        "provider_key": provider_key,
                                        "model": self.model,
                                        "base_url_descriptor": endpoint_descriptor(self.base_url),
                                    },
                                )
                                continue
                            if "temperature" in payload and _temperature_unsupported_error(e):
                                rejected_temperature = payload.get("temperature")
                                compat_mode = _temperature_compat_mode_for_error(
                                    e,
                                    current_temperature=rejected_temperature,
                                )
                                if compat_mode is None or compat_mode in temperature_retry_modes:
                                    raise
                                temperature_retry_modes.add(compat_mode)
                                temperature_retry_count += 1
                                self._mark_temperature_compat_mode(temperature_key, compat_mode)
                                transport_metadata["temperature_adjusted"] = True
                                transport_metadata["temperature_adjustment"] = compat_mode
                                transport_metadata["temperature_adjustment_reason"] = (
                                    "provider_rejected_parameter"
                                )
                                transport_metadata["temperature_retry_used"] = True
                                transport_metadata["temperature_retry_count"] = (
                                    temperature_retry_count
                                )
                                if compat_mode == _TEMPERATURE_COMPAT_MODE_DEFAULT:
                                    payload["temperature"] = _TEMPERATURE_DEFAULT_VALUE
                                else:
                                    payload.pop("temperature", None)
                                    transport_metadata["temperature_omitted"] = True
                                    transport_metadata["temperature_omit_reason"] = (
                                        "provider_rejected_parameter"
                                    )
                                _LOGGER.info(
                                    "llm_temperature_parameter_rejected_retrying_with_compat_mode",
                                    extra={
                                        "provider_key": provider_key,
                                        "model": self.model,
                                        "temperature": rejected_temperature,
                                        "temperature_compat_mode": compat_mode,
                                    },
                                )
                                continue
                            raise
                        if not stream and telemetry_on_reasoning_delta is not None:
                            for reasoning_output in response.reasoning:
                                telemetry_on_reasoning_delta(reasoning_output.text)
                        return _merge_transport_metadata(
                            _merge_request_plan_metadata(
                                response,
                                request_plan_metadata=request_plan_metadata,
                            ),
                            transport_metadata=transport_metadata,
                        )
            except CooperativeCancellationError:
                raise
            except LLMError:
                raise
            except DeadlineExhausted:
                raise
            except httpx.DecodingError as e:
                raise_if_cancelled(cancellation_token)
                raise LLMError(
                    f"LLM response decompression failed: {sanitize_error_text_for_output(e)}"
                ) from e
            except Exception as e:  # noqa: BLE001 - network errors vary
                raise_if_cancelled(cancellation_token)
                if _is_connect_failure(e):
                    raise LLMError(
                        "LLM request failed for "
                        f"{endpoint_label(self.base_url)}: {sanitize_error_text_for_output(e)}"
                    ) from e
                if _is_read_timeout(e):
                    raise LLMError(
                        "LLM request failed for "
                        f"{endpoint_label(self.base_url)}: {sanitize_error_text_for_output(e)}"
                    ) from e
                raise LLMError(f"LLM request failed: {sanitize_error_text_for_output(e)}") from e

        return stamp_response_for_route(
            telemetry.run(
                lambda: run_provider_limited_call(
                    call=_send_request,
                    provider_key=provider_key,
                    provider_concurrency_caps=self.provider_concurrency_caps,
                    retry_settings=self.provider_retry_settings,
                    operation="chat_completions",
                    sleep_fn=self._provider_sleep_fn,
                    random_fn=self._provider_random_fn,
                    on_retry=_record_retry,
                    on_retry_event=self._provider_retry_event_observer,
                    retry_deadline_allows=getattr(self, "_provider_retry_deadline_allows", None),
                    retry_wall_clock_cap_seconds=getattr(
                        self,
                        "_provider_retry_wall_clock_cap_seconds",
                        _PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS,
                    ),
                    cancellation_token=cancellation_token,
                )
            ),
            self.route_identity,
        )

    @staticmethod
    def _parse_non_stream_response(
        resp: httpx.Response,
        *,
        provider_key: str | None,
        model: str | None = None,
        trace_adapter: str | None = None,
    ) -> LLMResponse:
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise LLMError("LLM returned non-JSON response") from e

        if not isinstance(data, dict):
            raise LLMError("Unexpected LLM response shape: expected a JSON object")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("Unexpected LLM response shape: missing choices[0]")
        choice0 = choices[0]
        if not isinstance(choice0, dict) or not isinstance(choice0.get("message"), dict):
            raise LLMError("Unexpected LLM response shape: missing choices[0].message")
        msg = choice0["message"]

        content = _normalize_assistant_content_to_text(msg.get("content"))
        tool_calls_raw = msg.get("tool_calls") or []
        tool_calls = _parse_tool_calls(tool_calls_raw)
        response_model = data.get("model") if isinstance(data.get("model"), str) else None
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            raw=data,
            response_model=response_model,
            usage=_parse_usage(data.get("usage"), provider_key=provider_key),
            provider_metadata=_provider_metadata_for_reasoning(
                provider_key=provider_key,
                message=msg,
                model=model,
            ),
            reasoning=_reasoning_outputs_from_message(
                msg, provider_key=provider_key, trace_adapter=trace_adapter
            ),
        )

    @staticmethod
    def _error_from_response(resp: httpx.Response) -> LLMError:
        try:
            body = resp.text
        except Exception:
            body = "<unable to read response body>"
        return _error_from_status_body(status_code=resp.status_code, body=body)

    def _parse_stream_response(
        self,
        resp: httpx.Response,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        provider_key: str | None,
        cancellation_token: Any | None = None,
        manage_abort_callback: bool = True,
        attempt: _HTTPAttempt | None = None,
    ) -> LLMResponse:
        if resp.status_code >= 400:
            body = self._safe_error_body(resp)
            raise _error_from_status_body(status_code=resp.status_code, body=body)

        # Register response.close only for the lifetime of this parse. Every
        # exit path (success, provider error, timeout, deadline or cancel) must
        # release the callback so the token cannot retain a stale response.
        set_abort = getattr(cancellation_token, "set_abort_callback", None)
        clear_abort = getattr(cancellation_token, "clear_abort_callback", None)
        if manage_abort_callback and callable(set_abort):
            set_abort(resp.close)
        try:
            return self._parse_stream_response_body(
                resp,
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
                provider_key=provider_key,
                cancellation_token=cancellation_token,
                attempt=attempt,
            )
        finally:
            if manage_abort_callback and callable(clear_abort):
                clear_abort()

    def _parse_stream_response_body(
        self,
        resp: httpx.Response,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        provider_key: str | None,
        cancellation_token: Any | None = None,
        attempt: _HTTPAttempt | None = None,
    ) -> LLMResponse:
        attempt = attempt or _HTTPAttempt(stream=True)
        content_parts: list[str] = []
        tool_chunks: dict[int, dict[str, Any]] = {}
        event_count = 0
        response_model: str | None = None
        # Streaming chunks carry system_fingerprint the same way they carry
        # model. The non-stream path keeps the whole body in ``raw``, so it
        # already has it; without this the streaming path -- which is the
        # default -- would silently record no fingerprint at all.
        system_fingerprint: str | None = None
        usage: LLMUsage | None = None
        accumulated_content = ""
        reasoning_parts: list[str] = []
        native_reasoning_content_parts: list[str] = []
        saw_reasoning_content = False
        reasoning_details: list[Any] = []
        mistral_content_chunks: list[dict[str, Any]] = []
        saw_done = False
        progress_clock = self._stream_progress_clock
        last_meaningful_progress = progress_clock()
        # Chunks are appended as received unless this client was opted in to
        # cumulative assembly (see resolve_stream_assembly).
        cumulative_chunks = (
            getattr(self, "stream_assembly", STREAM_ASSEMBLY_CONCAT) == STREAM_ASSEMBLY_CUMULATIVE
        )

        trace = CompatibleReasoningTrace(
            self._trace_capability(provider_key).adapter,
            provider=provider_key,
            cumulative=cumulative_chunks,
        )

        def _chunk_suffix(previous: str, incoming: str) -> str:
            if cumulative_chunks:
                return _stream_delta_suffix(previous=previous, incoming=incoming)
            return incoming

        def _check_stream_liveness(*, check_progress: bool) -> None:
            raise_if_cancelled(cancellation_token)
            stream_deadline_exhausted = getattr(
                self,
                "_stream_deadline_exhausted",
                None,
            )
            if callable(stream_deadline_exhausted) and stream_deadline_exhausted():
                raise DeadlineExhausted("run deadline and in-flight provider grace elapsed")
            if not check_progress:
                return
            progress_now = progress_clock()
            if progress_now - last_meaningful_progress >= self.stream_no_progress_timeout_s:
                raise LLMStreamNoProgressError(
                    "LLM stream produced no meaningful payload within "
                    f"{self.stream_no_progress_timeout_s:g}s."
                )

        def _observed_byte_chunks() -> Iterator[bytes]:
            for chunk in resp.iter_bytes():
                attempt.increment("byte_chunk_count")
                attempt.increment("body_bytes_received", len(chunk))
                if chunk:
                    attempt.first("first_byte_ms")
                _check_stream_liveness(check_progress=True)
                yield chunk

        text_chunks = decode_text_chunks(
            _observed_byte_chunks(),
            encoding=resp.encoding or "utf-8",
        )

        def _observed_lines() -> Iterator[str]:
            for text in iter_lines_from_text_chunks(text_chunks):
                _check_stream_liveness(check_progress=False)
                yield text

        try:
            for event in _stream_events(_observed_lines(), attempt):
                if event is None:
                    last_meaningful_progress = progress_clock()
                    saw_done = True
                    break

                if event.get("error") is not None:
                    attempt.values["terminal_reason"] = "provider_error"
                    raise _error_from_stream_error_event(event.get("error"))

                event_progress = False
                event_count += 1
                model = event.get("model")
                if isinstance(model, str) and model and model != response_model:
                    response_model = model
                    event_progress = True
                chunk_fingerprint = event.get("system_fingerprint")
                if (
                    isinstance(chunk_fingerprint, str)
                    and chunk_fingerprint
                    and chunk_fingerprint != system_fingerprint
                ):
                    system_fingerprint = chunk_fingerprint
                    event_progress = True
                parsed_usage = _parse_usage(event.get("usage"), provider_key=provider_key)
                if parsed_usage is not None:
                    attempt.values["usage_observed"] = True
                    attempt.values["usage_source"] = "provider"
                    attempt.values["usage"] = parsed_usage
                if parsed_usage is not None and parsed_usage != usage:
                    usage = parsed_usage
                    event_progress = True

                choices = event.get("choices") or []
                choice0 = choices[0] if isinstance(choices, list) and choices else None
                if isinstance(choice0, dict) and choice0.get("finish_reason") is not None:
                    attempt.values["finish_reason_observed"] = True
                delta = choice0.get("delta") if isinstance(choice0, dict) else None
                if isinstance(delta, dict):
                    reasoning_delta = delta.get(_DEEPSEEK_REASONING_CONTENT_KEY)
                    if isinstance(reasoning_delta, str):
                        saw_reasoning_content = True
                        native_reasoning_content_parts.append(reasoning_delta)
                    if not isinstance(reasoning_delta, str):
                        reasoning_delta = delta.get(_OPENROUTER_REASONING_KEY)
                    if isinstance(reasoning_delta, str) and reasoning_delta:
                        attempt.increment("reasoning_delta_count")
                        reasoning_parts.append(reasoning_delta)
                        event_progress = True
                    details_delta = delta.get(_OPENROUTER_REASONING_DETAILS_KEY)
                    if isinstance(details_delta, list) and details_delta:
                        attempt.increment("reasoning_delta_count")
                        reasoning_details.extend(details_delta)
                        event_progress = True
                    for reasoning_delta in trace.feed(delta):
                        event_progress = True
                        if on_reasoning_delta is not None:
                            on_reasoning_delta(reasoning_delta.text)

                    raw_content_delta = delta.get("content")
                    if _is_mistral_provider(provider_key) and _append_mistral_stream_content(
                        mistral_content_chunks,
                        raw_content_delta,
                    ):
                        event_progress = True
                    content_delta = _normalize_assistant_content_to_text(raw_content_delta)
                    if content_delta:
                        content_suffix = _chunk_suffix(accumulated_content, content_delta)
                        if content_suffix:
                            attempt.increment("text_delta_count")
                            content_parts.append(content_suffix)
                            accumulated_content += content_suffix
                            event_progress = True
                            if on_text_delta is not None:
                                on_text_delta(content_suffix)

                    tc_delta = delta.get("tool_calls") or []
                    if isinstance(tc_delta, list):
                        for raw_tc in tc_delta:
                            if not isinstance(raw_tc, dict):
                                continue
                            idx = raw_tc.get("index")
                            if not isinstance(idx, int):
                                continue
                            attempt.increment("tool_delta_count")
                            entry = tool_chunks.setdefault(
                                idx,
                                {
                                    "id": "",
                                    "name": "",
                                    "arguments": "",
                                    "provider_metadata": None,
                                },
                            )

                            tc_id = raw_tc.get("id")
                            if isinstance(tc_id, str) and tc_id and tc_id != entry["id"]:
                                entry["id"] = tc_id
                                event_progress = True

                            provider_metadata = _gemini_tool_call_provider_metadata(raw_tc)
                            if provider_metadata:
                                existing_metadata = entry.get("provider_metadata")
                                merged_metadata = _merge_provider_metadata(
                                    existing_metadata
                                    if isinstance(existing_metadata, dict)
                                    else None,
                                    provider_metadata,
                                )
                                if merged_metadata != existing_metadata:
                                    entry["provider_metadata"] = merged_metadata
                                    event_progress = True

                            fn = raw_tc.get("function")
                            if not isinstance(fn, dict):
                                continue
                            name = fn.get("name")
                            if isinstance(name, str) and name and name != entry["name"]:
                                entry["name"] = name
                                event_progress = True
                            args_piece = fn.get("arguments")
                            if isinstance(args_piece, str):
                                args_suffix = _chunk_suffix(entry["arguments"], args_piece)
                                if args_suffix:
                                    entry["arguments"] += args_suffix
                                    event_progress = True

                if event_progress:
                    last_meaningful_progress = progress_clock()
        except Exception:
            raise_if_cancelled(cancellation_token)
            raise

        # A transport abort can end the stream as clean EOF instead of raising.
        _check_stream_liveness(check_progress=False)
        if not saw_done:
            attempt.values["terminal_reason"] = "stream_truncated"
            raise LLMError("LLM stream truncated before [DONE]")
        streamed_tool_calls = _parse_stream_tool_calls(tool_chunks)
        streamed_content = "".join(content_parts)
        reasoning_message = {
            _DEEPSEEK_REASONING_CONTENT_KEY: "".join(reasoning_parts),
            _OPENROUTER_REASONING_KEY: "".join(reasoning_parts),
            _OPENROUTER_REASONING_DETAILS_KEY: reasoning_details,
            "content": mistral_content_chunks,
        }
        stream_raw: dict[str, Any] = {"stream": True, "events": event_count}
        if system_fingerprint:
            # Added only when the provider actually sent one, so a provider
            # that never emits it leaves ``raw`` exactly as before.
            stream_raw["system_fingerprint"] = system_fingerprint
        return LLMResponse(
            content=streamed_content,
            tool_calls=streamed_tool_calls,
            raw=stream_raw,
            response_model=response_model,
            usage=usage,
            provider_metadata=_provider_metadata_for_reasoning(
                provider_key=provider_key,
                message=reasoning_message,
                model=self.model,
                reasoning_content_message=(
                    {
                        _DEEPSEEK_REASONING_CONTENT_KEY: "".join(native_reasoning_content_parts),
                    }
                    if saw_reasoning_content
                    else {}
                ),
            ),
            reasoning=trace.outputs,
        )

    @staticmethod
    def _safe_error_body(resp: httpx.Response) -> str:
        try:
            resp.read()
        except Exception:
            pass
        try:
            body = resp.text
        except Exception:
            body = "<unable to read response body>"
        return body
