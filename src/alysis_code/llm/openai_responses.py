from __future__ import annotations

import copy
import hashlib
import hmac
import inspect
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any

import httpx

from ..cancellation import CooperativeCancellationError
from ..computer_use_call_diagnostics import (
    _count_provider_images,
    _provider_attempt_ordinal,
    _provider_serialization_ordinal,
    note_computer_use_provider_evidence,
)
from ..computer_use_diagnostics import DIAGNOSTIC_CODES, set_safe_provider_error_fields
from ..error_text import sanitize_error_text_for_output
from ..execution_deadline import DeadlineExhausted
from ..mcp.github_policy_json import arguments as github_policy_arguments
from ..provider_auth import (
    ProviderAuthAdapter,
    ProviderAuthError,
    ProviderCredentialScopeMismatchError,
)
from ..provider_telemetry import ProviderCallTelemetryRecorder
from ..request_estimation import estimate_provider_payload_tokens
from ..web_search_adapters import AUTO_WEB_SEARCH_ADAPTER, OPENAI_RESPONSES_ADAPTER
from . import resolved_entry
from .base import (
    input_token_count_request,
    omitted_reasoning_display_required,
    scoped_input_token_counter,
    serial_tool_calls_required,
    stateless_responses_required,
)
from .cache_policy import merge_cache_policy_metadata
from .cancellable_http import cancellation_aware_httpx_client, provider_httpx_client
from .focused_input_diagnostic import record_stream_diagnostic
from .http_cancellation import (
    cancellable_httpx_request,
    cancellable_httpx_send,
    raise_if_cancelled,
)
from .metadata import (
    OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
    PROVIDER_METADATA_KEY,
    ProviderRouteIdentity,
    build_provider_route_identity,
    canonicalize_extra_headers,
    credential_scope_fingerprint,
    gate_messages_for_provider_route,
    merge_canonical_headers,
    stamp_response_for_route,
)
from .protocols import (
    OPENAI_RESPONSES_PROTOCOL,
    get_provider_protocol_capabilities,
    reasoning_trace_callback,
)
from .provider_limits import (
    DEFAULT_PROVIDER_CONCURRENCY_CAPS,
    DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS,
    ProviderRetrySettings,
    best_effort_provider_key,
    mark_provider_call_non_retryable,
    run_provider_limited_call,
)
from .rate_limit_metadata import HARD_QUOTA_CODES, retry_metadata
from .request_plan import LLMRequestPlan, RequestCachePlan, WireRequestDiagnostics
from .request_shape import build_request_shape_report
from .streaming import (
    SSEFrame,
    iter_lines_from_text_chunks,
    iter_sse_frames,
    parse_sse_json_frame,
)
from .temperature_compat import documented_temperature_omit_reason
from .types import (
    AssistantResponsePhase,
    InputTokenCount,
    LLMError,
    LLMOutputLimitError,
    LLMResponse,
    LLMStreamNoProgressError,
    LLMUsage,
    ReasoningOutput,
    ReasoningOutputKind,
    ToolCall,
    UsageConfidence,
    UsageContract,
)
from .usage_normalization import parse_compatible_usage


class ResponsesError(RuntimeError):
    def __init__(self, message: object = "") -> None:
        super().__init__(sanitize_error_text_for_output(message))


def _callable_accepts_keyword(callable_obj: Callable[..., Any], keyword: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == keyword
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        )
        for parameter in signature.parameters.values()
    )


_DEFAULT_ACCEPT_ENCODING = "identity"
_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS = DEFAULT_PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS
_OPENAI_RESPONSES_METADATA_KEY = OPENAI_RESPONSES_PROVIDER_METADATA_KEY
_WEB_SEARCH_MODES_ALLOWING_OPENAI_BUILTIN = frozenset({"auto", "native"})
_RESPONSES_TOOL_CHOICE_STRINGS = frozenset({"auto", "none", "required"})
_RESPONSES_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
_RESPONSES_JSON_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ALYSIS_WEB_SEARCH_FUNCTION_NAME = "web_search"
_RESPONSES_CALL_ID_MAX_LENGTH = 64
_RESPONSES_CALL_ID_RE = re.compile(rf"^[A-Za-z0-9_-]{{1,{_RESPONSES_CALL_ID_MAX_LENGTH}}}$")
# Canonical prefix for persisted and provider-visible normalized identifiers.
_RESPONSES_CALL_ID_HASH_PREFIX = "alysis_"
_RESPONSES_CALL_ID_HASH_LENGTH = _RESPONSES_CALL_ID_MAX_LENGTH - len(_RESPONSES_CALL_ID_HASH_PREFIX)
_RESPONSES_CALL_ID_NORMALIZATION_SCHEME = "sha256-ascii-v1"
_RESPONSES_HOSTED_WEB_SEARCH_TYPES = frozenset({"web_search", "web_search_preview"})
_MIN_RESPONSES_OUTPUT_TOKENS = 16
_PERPLEXITY_API_HOST = "api.perplexity.ai"
_PERPLEXITY_INTEGRATION = "alysis-code"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResponsesContinuation:
    previous_response_id: str
    suffix_messages: list[dict[str, Any]]
    anchor_index: int


def _stable_request_signature(value: Any) -> str:
    # Preserve existing valid-Unicode fingerprints and encode any isolated
    # UTF-16 surrogates losslessly. This digest never normalizes opaque values.
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def _json_bytes(value: Any) -> bytes:
    """Encode JSON without HTTPX's strict UTF-8 conversion of lone surrogates."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(
        "ascii"
    )


def _non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _clamp_responses_max_output_tokens(value: int) -> int:
    requested = int(value)
    if requested >= _MIN_RESPONSES_OUTPUT_TOKENS:
        return requested
    _LOGGER.warning(
        "OpenAI Responses max_output_tokens adjusted from %d to documented minimum %d",
        requested,
        _MIN_RESPONSES_OUTPUT_TOKENS,
    )
    return _MIN_RESPONSES_OUTPUT_TOKENS


# Some models (notably the GPT-5 reasoning family) reject a non-default
# ``temperature`` on the Responses API with a 400/422 ("temperature is not
# supported … only the default (1) value is supported"). Unlike the chat-compat
# client, this one had no fallback, so such a model failed on every call — and on
# setup-wizard key/model validation. We now detect that error once per
# (base_url, model), drop ``temperature`` from the payload, retry the offending
# call immediately, and omit it from every later call to the same model.
_TEMPERATURE_UNSUPPORTED_STATUS_MARKERS = ("error 400", "error 422")
_TEMPERATURE_UNSUPPORTED_TOKENS = (
    "unsupported",
    "not support",
    "not allowed",
    "only the default",
    "out of range",
    "deprecated",
)
_RESPONSES_OMIT_TEMPERATURE_MODELS: set[str] = set()

# Endpoints (base_url + model) whose gateway rejected the optional ``include``
# entries (e.g. ``web_search_call.action.sources``). Source metadata is an
# enhancement, not a requirement, so once a gateway rejects it we stop sending
# it for the rest of the process instead of failing every request.
_RESPONSES_OMIT_INCLUDE_ENDPOINTS: set[str] = set()

_INCLUDE_UNSUPPORTED_STATUS_MARKERS = ("error 400", "error 422", "web_search unsupported")
_INCLUDE_VALUE_CONTEXT_TOKENS = (
    "web_search_call.action.sources",
    "include value",
    "include values",
    "'include'",
    '"include"',
    "include parameter",
    "include field",
    "param 'include'",
    'param "include"',
    "parameter: include",
    "parameter 'include'",
)
_INCLUDE_UNSUPPORTED_TOKENS = (
    "invalid",
    "unsupported",
    "not support",
    "not allowed",
    "not permitted",
    "unknown",
    "unrecognized",
    "unexpected",
    "cannot be produced",
    "cannot",
    "forbidden",
    "must be omitted",
    "extra",
)

_REASONING_SUMMARY_UNSUPPORTED_STATUS_MARKERS = ("error 400", "error 422")
_REASONING_SUMMARY_UNSUPPORTED_TOKENS = (
    "invalid",
    "unsupported",
    "not support",
    "not allowed",
    "not permitted",
    "unknown",
    "unknown parameter",
    "unknown field",
    "unrecognized",
    "unexpected",
    "invalid parameter",
    "extra",
    "extra inputs",
    "forbidden",
    "cannot be set",
    "must be omitted",
)


def _responses_endpoint_model_key(base_url: str, model: str) -> str:
    return f"{str(base_url).strip().rstrip('/')}\n{str(model).strip()}"


def _responses_temperature_omit_key(base_url: str, model: str) -> str:
    return _responses_endpoint_model_key(base_url, model)


def _responses_include_unsupported(err: Exception) -> bool:
    """Return whether a 400/422 clearly rejects the optional ``include`` entries.

    Requires an explicit ``include``-value context token so unrelated 400s that
    merely contain the word "include" (e.g. "input must include a message") do
    not permanently drop source metadata for the endpoint.
    """

    text = str(err).casefold()
    if not any(marker in text for marker in _INCLUDE_UNSUPPORTED_STATUS_MARKERS):
        return False
    if not any(token in text for token in _INCLUDE_VALUE_CONTEXT_TOKENS):
        return False
    return any(token in text for token in _INCLUDE_UNSUPPORTED_TOKENS)


def _responses_temperature_unsupported(err: Exception) -> bool:
    text = str(err).casefold()
    if "temperature" not in text:
        return False
    if not any(marker in text for marker in _TEMPERATURE_UNSUPPORTED_STATUS_MARKERS):
        return False
    return any(token in text for token in _TEMPERATURE_UNSUPPORTED_TOKENS)


def _responses_reasoning_summary_unsupported(err: Exception) -> bool:
    """Return whether a 400/422 clearly rejects ``reasoning.summary``.

    Keep this deliberately narrower than the generic unsupported-parameter
    fallback: an unrelated response summary error must not disable reasoning
    summaries for the rest of the client session.
    """

    text = str(err).casefold()
    if not any(marker in text for marker in _REASONING_SUMMARY_UNSUPPORTED_STATUS_MARKERS):
        return False
    if "summary" not in text:
        return False
    if not any(
        context in text
        for context in (
            "reasoning",
            "parameter",
            "field",
            "request argument",
            "extra inputs",
        )
    ):
        return False
    return any(token in text for token in _REASONING_SUMMARY_UNSUPPORTED_TOKENS)


def _without_responses_reasoning_summary(payload: dict[str, Any]) -> bool:
    """Remove only ``reasoning.summary`` from an already-adapted payload."""

    raw_reasoning = payload.get("reasoning")
    if not isinstance(raw_reasoning, dict) or "summary" not in raw_reasoning:
        return False
    reasoning = copy.deepcopy(raw_reasoning)
    reasoning.pop("summary", None)
    if reasoning:
        payload["reasoning"] = reasoning
    else:
        payload.pop("reasoning", None)
    return True


def _headers_with_default_accept_encoding(headers: dict[str, str]) -> dict[str, str]:
    request_headers = dict(headers)
    if not any(key.lower() == "accept-encoding" for key in request_headers):
        request_headers["accept-encoding"] = _DEFAULT_ACCEPT_ENCODING
    return request_headers


@dataclass(frozen=True)
class WebSearchCitation:
    title: str
    url: str
    start_index: int | None = None
    end_index: int | None = None


@dataclass(frozen=True)
class WebSearchSource:
    url: str
    title: str = ""


@dataclass(frozen=True)
class WebSearchResponse:
    answer: str
    citations: list[WebSearchCitation]
    sources: list[WebSearchSource]
    queries: list[str]
    raw: dict[str, Any]
    response_id: str | None = None
    model: str | None = None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_error_message(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        message = str(error_obj.get("message") or "").strip()
        if message:
            return message
    return None


def _parse_arguments(args_s: Any) -> dict[str, Any]:
    if isinstance(args_s, dict):
        return dict(args_s)
    if not isinstance(args_s, str):
        args_s = json.dumps(args_s if args_s is not None else {})
    try:
        args = json.loads(args_s)
    except json.JSONDecodeError:
        return {"_raw_arguments": args_s}
    if not isinstance(args, dict):
        return {"_raw_arguments": args_s}
    return args


def _json_arguments(args: Any) -> str:
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return args
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if args is None:
        return "{}"
    return json.dumps(args, ensure_ascii=False, separators=(",", ":"))


def _responses_call_id(value: Any, *, fallback: str) -> str:
    """Return a stable Responses-compatible function call identifier.

    OpenAI Responses currently caps ``call_id`` at 64 characters. Some
    provider surfaces can nevertheless emit a longer identifier and then
    reject that same identifier when the conversation is replayed. Preserve
    already-compatible IDs byte-for-byte. Map every other value into a
    provider-safe namespace using 216 bits of SHA-256, which avoids the prefix
    truncation collisions created by simply slicing a long provider ID.

    The function is deterministic so independently serialized
    ``function_call`` and ``function_call_output`` items remain paired across
    retries, transcript reloads, and process restarts.
    """

    raw = "" if value is None else str(value)
    if not raw.strip():
        raw = str(fallback or "call")
    if _RESPONSES_CALL_ID_RE.fullmatch(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()
    return _RESPONSES_CALL_ID_HASH_PREFIX + digest[:_RESPONSES_CALL_ID_HASH_LENGTH]


def _responses_function_call_id(item: Mapping[str, Any], *, index: int) -> str:
    raw_call_id = item.get("call_id")
    if raw_call_id is None or not str(raw_call_id).strip():
        raw_call_id = item.get("id")
    return _responses_call_id(raw_call_id, fallback=f"call_{index}")


def _responses_function_call_ids_by_output_index(
    output_items: list[Any],
) -> dict[int, str]:
    """Canonicalize one assistant batch and reject ambiguous pairings."""

    call_ids: dict[int, str] = {}
    seen: set[str] = set()
    for index, item in enumerate(output_items):
        if not isinstance(item, dict) or str(item.get("type") or "") != "function_call":
            continue
        call_id = _responses_function_call_id(item, index=index)
        if call_id in seen:
            # This covers exact provider duplicates, repeated safe IDs, digest
            # collisions, and a deliberate alias where a safe raw ID equals
            # the canonical form of another raw ID. Any of those makes a
            # function_call_output pairing ambiguous, so fail before dispatch.
            raise LLMError(
                "OpenAI Responses returned duplicate or ambiguous function call IDs "
                "in one assistant batch"
            )
        seen.add(call_id)
        call_ids[index] = call_id
    return call_ids


def _normalized_responses_output_items(
    output_items: list[Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Copy output items and canonicalize every replayable function call ID."""

    call_ids = _responses_function_call_ids_by_output_index(output_items)
    normalized: list[dict[str, Any]] = []
    rewritten = False
    for index, raw_item in enumerate(output_items):
        if not isinstance(raw_item, dict):
            continue
        item = copy.deepcopy(raw_item)
        if index in call_ids:
            call_id = call_ids[index]
            original_call_id = item.get("call_id")
            if not isinstance(original_call_id, str) or original_call_id != call_id:
                rewritten = True
            item["call_id"] = call_id
        normalized.append(item)
    return normalized, rewritten


def _responses_metadata_requires_full_replay(metadata: Mapping[str, Any]) -> bool:
    """Return whether a server-side continuation contains an unusable call ID.

    A ``previous_response_id`` points at the provider's original response, so
    its function-call IDs cannot be renamed client-side. When canonicalization
    was necessary, replay the normalized function call and output together
    instead of appending an output to an incompatible server-side anchor.
    """

    if metadata.get("call_ids_rewritten") is True:
        return True
    output_items = metadata.get("output_items")
    if not isinstance(output_items, list):
        return False
    _normalized, rewritten = _normalized_responses_output_items(output_items)
    return rewritten


def _content_to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("content")
        return text if isinstance(text, str) else ""
    return str(raw)


def _responses_message_content(raw: Any, *, role: str) -> str | list[dict[str, Any]]:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return _content_to_text(raw)

    parts: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            text = item
            if text:
                parts.append(
                    {"type": "output_text" if role == "assistant" else "input_text", "text": text}
                )
            continue
        if not isinstance(item, dict):
            continue
        part_type = str(item.get("type") or "").strip()
        text = item.get("text") or item.get("content")
        if part_type in {"text", "input_text", "output_text"} and isinstance(text, str):
            parts.append(
                {
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": text,
                }
            )
            continue
        if role != "user":
            continue
        if part_type == "image_url":
            image_url = item.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "").strip()
            elif isinstance(image_url, str):
                url = image_url.strip()
            if url:
                parts.append({"type": "input_image", "image_url": url})
            continue
        if part_type == "input_image":
            copied = {key: copy.deepcopy(value) for key, value in item.items()}
            if copied.get("image_url") or copied.get("file_id"):
                parts.append(copied)
    return parts if parts else ""


def _chat_tool_call_parts(raw_tool_call: Any) -> tuple[str, str, str] | None:
    if not isinstance(raw_tool_call, dict):
        return None
    raw_call_id = raw_tool_call.get("id") or raw_tool_call.get("call_id")
    function = raw_tool_call.get("function")
    if isinstance(function, dict):
        name = str(function.get("name") or "").strip()
        arguments = _json_arguments(function.get("arguments"))
    else:
        name = str(raw_tool_call.get("name") or "").strip()
        arguments = _json_arguments(raw_tool_call.get("arguments"))
    if not name:
        return None
    call_id = _responses_call_id(raw_call_id, fallback=f"call_{name}")
    return call_id, name, arguments


def _metadata_output_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get(PROVIDER_METADATA_KEY)
    if not isinstance(metadata, dict):
        return []
    responses_metadata = metadata.get(_OPENAI_RESPONSES_METADATA_KEY)
    if not isinstance(responses_metadata, dict):
        return []
    output_items = responses_metadata.get("output_items")
    if not isinstance(output_items, list):
        return []
    normalized, _rewritten = _normalized_responses_output_items(output_items)
    # Responses output items may carry response-only lifecycle annotations.
    # The live /responses input schema rejects ``status`` when the same items
    # are replayed manually (for example, a stateless encrypted-reasoning item
    # followed by a function call). Preserve the opaque reasoning/call linkage
    # byte-for-byte while removing only that output-only field at the request
    # boundary.
    for item in normalized:
        item.pop("status", None)
    return normalized


def _metadata_responses_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    metadata = message.get(PROVIDER_METADATA_KEY)
    if not isinstance(metadata, dict):
        return None
    responses_metadata = metadata.get(_OPENAI_RESPONSES_METADATA_KEY)
    if not isinstance(responses_metadata, dict):
        return None
    return responses_metadata


def _response_with_request_plan_metadata(
    response: LLMResponse,
    request_plan_metadata: dict[str, Any],
) -> LLMResponse:
    provider_metadata = copy.deepcopy(response.provider_metadata) or {}
    responses_metadata = provider_metadata.setdefault(_OPENAI_RESPONSES_METADATA_KEY, {})
    if isinstance(responses_metadata, dict):
        responses_metadata["request_plan"] = copy.deepcopy(request_plan_metadata)
    return LLMResponse(
        content=response.content,
        tool_calls=response.tool_calls,
        raw=response.raw,
        response_model=response.response_model,
        usage=response.usage,
        provider_metadata=provider_metadata,
        reasoning=response.reasoning,
        assistant_phase=response.assistant_phase,
    )


def _responses_continuation_from_messages(
    messages: list[dict[str, Any]],
) -> _ResponsesContinuation | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip() != "assistant":
            continue
        responses_metadata = _metadata_responses_payload(message)
        if not responses_metadata:
            continue
        if _responses_metadata_requires_full_replay(responses_metadata):
            continue
        previous_response_id = str(responses_metadata.get("response_id") or "").strip()
        request_plan_metadata = responses_metadata.get("request_plan")
        if not previous_response_id or not isinstance(request_plan_metadata, dict):
            continue
        request_message_count = _non_negative_int(
            request_plan_metadata.get("request_message_count")
        )
        request_messages_signature = str(
            request_plan_metadata.get("request_messages_signature") or ""
        ).strip()
        if request_message_count != index or not request_messages_signature:
            continue
        prefix_messages = messages[:index]
        if _stable_request_signature(prefix_messages) != request_messages_signature:
            continue
        suffix_messages = messages[index + 1 :]
        if not suffix_messages:
            continue
        return _ResponsesContinuation(
            previous_response_id=previous_response_id,
            suffix_messages=copy.deepcopy(suffix_messages),
            anchor_index=index,
        )
    return None


def _responses_previous_response_rejected(err: Exception) -> bool:
    text = str(err).casefold()
    if "previous_response_id" in text:
        return True
    return "previous response" in text and any(
        marker in text
        for marker in ("not found", "invalid", "expired", "unknown", "does not exist")
    )


def _responses_input_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role in {"system", "developer", "user"}:
            input_items.append(
                {
                    "role": role,
                    "content": _responses_message_content(message.get("content"), role=role),
                }
            )
            continue
        if role == "assistant":
            metadata_items = _metadata_output_items(message)
            if metadata_items:
                input_items.extend(metadata_items)
                continue

            content = _responses_message_content(message.get("content"), role=role)
            if content:
                input_items.append({"role": "assistant", "content": content})
            raw_tool_calls = message.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                seen_call_ids: set[str] = set()
                for raw_tool_call in raw_tool_calls:
                    parts = _chat_tool_call_parts(raw_tool_call)
                    if parts is None:
                        continue
                    call_id, name, arguments = parts
                    if call_id in seen_call_ids:
                        raise LLMError(
                            "OpenAI Responses cannot replay an assistant batch with duplicate "
                            "or ambiguous function call IDs"
                        )
                    seen_call_ids.add(call_id)
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        }
                    )
            continue
        if role == "tool":
            raw_call_id = message.get("tool_call_id") or message.get("call_id")
            if raw_call_id is None or not str(raw_call_id).strip():
                raise LLMError("OpenAI Responses tool message is missing tool_call_id")
            call_id = _responses_call_id(raw_call_id, fallback="call_tool")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _content_to_text(message.get("content")),
                }
            )
            continue
        raise LLMError(f"OpenAI Responses cannot send message role {role!r}")
    return input_items


def _assistant_output_parts(data: dict[str, Any]) -> list[dict[str, Any]]:
    output = data.get("output")
    if not isinstance(output, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "message":
            continue
        if str(item.get("role") or "") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                parts.append(part)
    return parts


def _extract_answer_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    text_parts: list[str] = []
    for part in _assistant_output_parts(data):
        part_type = str(part.get("type") or "")
        if part_type not in {"output_text", "text"}:
            continue
        text = part.get("text")
        if isinstance(text, str):
            text_parts.append(text)
    return "".join(text_parts)


def _assistant_response_phase(data: dict[str, Any]) -> AssistantResponsePhase | None:
    """Normalize a provider-declared assistant phase without inferring from prose."""

    output = data.get("output")
    if not isinstance(output, list):
        return None
    for item in reversed(output):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "message":
            continue
        if str(item.get("role") or "") != "assistant":
            continue
        try:
            return AssistantResponsePhase(str(item.get("phase") or "").strip())
        except ValueError:
            return None
    return None


def _extract_citations(data: dict[str, Any]) -> list[WebSearchCitation]:
    citations: list[WebSearchCitation] = []
    for part in _assistant_output_parts(data):
        annotations = part.get("annotations")
        if not isinstance(annotations, list):
            continue
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            if str(annotation.get("type") or "") != "url_citation":
                continue
            url = str(annotation.get("url") or "").strip()
            if not url:
                continue
            citations.append(
                WebSearchCitation(
                    title=str(annotation.get("title") or "").strip(),
                    url=url,
                    start_index=_coerce_int(annotation.get("start_index")),
                    end_index=_coerce_int(annotation.get("end_index")),
                )
            )
    raw_citations = data.get("citations")
    if isinstance(raw_citations, list):
        for raw_citation in raw_citations:
            citation = _coerce_citation(raw_citation)
            if citation is not None:
                citations.append(citation)
    return _dedupe_citations(citations)


def _coerce_citation(raw_citation: Any) -> WebSearchCitation | None:
    if isinstance(raw_citation, str):
        url = raw_citation.strip()
        if not url:
            return None
        return WebSearchCitation(title="", url=url)
    if not isinstance(raw_citation, dict):
        return None

    citation_payload = raw_citation
    for nested_key in ("url_citation", "web_citation", "x_citation"):
        nested = raw_citation.get(nested_key)
        if isinstance(nested, dict):
            citation_payload = nested
            break

    url = str(
        citation_payload.get("url")
        or citation_payload.get("uri")
        or citation_payload.get("link")
        or ""
    ).strip()
    if not url:
        return None
    start_index = citation_payload.get("start_index")
    if start_index is None:
        start_index = citation_payload.get("startIndex")
    end_index = citation_payload.get("end_index")
    if end_index is None:
        end_index = citation_payload.get("endIndex")
    return WebSearchCitation(
        title=str(citation_payload.get("title") or citation_payload.get("name") or "").strip(),
        url=url,
        start_index=_coerce_int(start_index),
        end_index=_coerce_int(end_index),
    )


def _dedupe_citations(citations: list[WebSearchCitation]) -> list[WebSearchCitation]:
    deduped: list[WebSearchCitation] = []
    seen: set[str] = set()
    for citation in citations:
        url = str(citation.url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(citation)
    return deduped


def _extract_sources_and_queries(data: dict[str, Any]) -> tuple[list[WebSearchSource], list[str]]:
    output = data.get("output")
    if not isinstance(output, list):
        return [], []

    sources: list[WebSearchSource] = []
    queries: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "web_search_call":
            continue
        action = item.get("action")
        if not isinstance(action, dict):
            continue

        raw_sources = action.get("sources")
        if isinstance(raw_sources, list):
            for raw_source in raw_sources:
                if not isinstance(raw_source, dict):
                    continue
                url = str(raw_source.get("url") or "").strip()
                if not url:
                    continue
                sources.append(
                    WebSearchSource(
                        url=url,
                        title=str(raw_source.get("title") or "").strip(),
                    )
                )

        raw_queries = action.get("queries")
        if isinstance(raw_queries, list):
            for raw_query in raw_queries:
                query = str(raw_query or "").strip()
                if query:
                    queries.append(query)
        raw_query = action.get("query")
        if isinstance(raw_query, str) and raw_query.strip():
            queries.append(raw_query.strip())

    return sources, queries


def _has_web_search_call_output(data: dict[str, Any]) -> bool:
    output = data.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("type") or "") == "web_search_call"
        for item in output
    )


def _merge_citation_sources(
    sources: list[WebSearchSource],
    citations: list[WebSearchCitation],
) -> list[WebSearchSource]:
    merged = list(sources)
    seen = {str(source.url or "").strip() for source in merged if str(source.url or "").strip()}
    for citation in citations:
        url = str(citation.url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(WebSearchSource(url=url, title=str(citation.title or "").strip()))
    return merged


def _function_name_from_tool(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    if str(tool.get("type") or "") == "function":
        return str(tool.get("name") or "").strip()
    return ""


def _responses_tool_from_chat_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    tool_type = str(tool.get("type") or "").strip()
    if tool_type == "function":
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            if not name:
                return None
            mapped: dict[str, Any] = {
                "type": "function",
                "name": name,
                "parameters": copy.deepcopy(function.get("parameters") or {"type": "object"}),
            }
            description = str(function.get("description") or "").strip()
            if description:
                mapped["description"] = description
            strict = function.get("strict", tool.get("strict"))
            # Responses auto-normalizes an unspecified schema into strict mode,
            # including making optional selectors required. Preserve the tool's
            # original optionality unless its author explicitly chose strict mode.
            mapped["strict"] = False if strict is None else bool(strict)
            return mapped

        name = str(tool.get("name") or "").strip()
        if not name:
            return None
        mapped = copy.deepcopy(tool)
        mapped["type"] = "function"
        if mapped.get("strict") is None:
            mapped["strict"] = False
        return mapped
    if tool_type in {"web_search", "web_search_preview"}:
        return copy.deepcopy(tool)
    raise LLMError(f"OpenAI Responses does not support tool type {tool_type!r}")


def _tools_contain_function(tools: list[dict[str, Any]] | None, name: str) -> bool:
    if not tools:
        return False
    return any(_function_name_from_tool(tool) == name for tool in tools if isinstance(tool, dict))


def _is_alysis_web_search_function(tool: dict[str, Any]) -> bool:
    return _function_name_from_tool(tool) == _ALYSIS_WEB_SEARCH_FUNCTION_NAME


def _is_responses_hosted_web_search_tool(tool: dict[str, Any]) -> bool:
    return str(tool.get("type") or "").strip() in _RESPONSES_HOSTED_WEB_SEARCH_TYPES


def _openai_builtin_web_search_allowed(*, mode: str, adapter: str) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    normalized_adapter = str(adapter or "").strip().lower() or AUTO_WEB_SEARCH_ADAPTER
    if normalized_mode not in _WEB_SEARCH_MODES_ALLOWING_OPENAI_BUILTIN:
        return False
    return normalized_adapter in {AUTO_WEB_SEARCH_ADAPTER, OPENAI_RESPONSES_ADAPTER}


@dataclass(frozen=True)
class _ResponsesToolMapping:
    tools: list[dict[str, Any]]
    added_builtin_web_search: bool
    removed_alysis_web_search: bool


def _responses_tools(
    tools: list[dict[str, Any]] | None,
    *,
    mode: str,
    adapter: str,
    provider_key: str | None = None,
) -> _ResponsesToolMapping:
    normalized_mode = str(mode or "off").strip().lower()
    normalized_adapter = (
        str(adapter or AUTO_WEB_SEARCH_ADAPTER).strip().lower() or AUTO_WEB_SEARCH_ADAPTER
    )
    raw_tools = [tool for tool in tools or [] if isinstance(tool, dict)]
    alysis_web_search_present = any(_is_alysis_web_search_function(tool) for tool in raw_tools)
    capabilities = get_provider_protocol_capabilities(
        provider_key=provider_key or "", protocol=OPENAI_RESPONSES_PROTOCOL
    )
    # A Responses-compatible gateway may only accept function tools. Auto search
    # must retain the local function instead of upgrading it to a paid built-in.
    builtin_web_search_allowed = (
        capabilities is None or capabilities.supports_provider_hosted_web_search_adapter
    )
    hosted_web_search_present = any(
        _is_responses_hosted_web_search_tool(tool) for tool in raw_tools
    )
    if not builtin_web_search_allowed and (
        (normalized_mode == "native" and alysis_web_search_present)
        or (normalized_mode in {"auto", "native"} and hosted_web_search_present)
    ):
        raise LLMError(
            f"Provider {provider_key!r} does not support built-in web search. "
            "Use web_search_mode='auto' or 'external' with the Alysis web_search function."
        )
    use_openai_builtin_web_search = (
        builtin_web_search_allowed
        and alysis_web_search_present
        and _openai_builtin_web_search_allowed(
            mode=normalized_mode,
            adapter=normalized_adapter,
        )
    )
    if (
        normalized_mode == "native"
        and alysis_web_search_present
        and not use_openai_builtin_web_search
    ):
        raise LLMError(
            "web_search_mode=native with protocol=openai_responses requires "
            "web_search_adapter='auto' or 'openai_responses' for OpenAI hosted web_search; "
            f"got {normalized_adapter!r}"
        )

    mapped_tools: list[dict[str, Any]] = []
    removed_alysis_web_search = False
    for tool in raw_tools:
        if _is_alysis_web_search_function(tool):
            if normalized_mode in {"off", "native"} or use_openai_builtin_web_search:
                removed_alysis_web_search = True
                continue
        if _is_responses_hosted_web_search_tool(tool) and normalized_mode in {"off", "external"}:
            continue
        mapped = _responses_tool_from_chat_tool(tool)
        if mapped is not None:
            mapped_tools.append(mapped)
    if use_openai_builtin_web_search and not any(
        _is_responses_hosted_web_search_tool(tool) for tool in mapped_tools
    ):
        mapped_tools.append({"type": "web_search", "external_web_access": True})
    return _ResponsesToolMapping(
        tools=mapped_tools,
        added_builtin_web_search=use_openai_builtin_web_search,
        removed_alysis_web_search=removed_alysis_web_search,
    )


def _tool_choice_for_mapped_tools(
    tool_choice: Any,
    *,
    removed_alysis_web_search: bool,
) -> Any:
    if not removed_alysis_web_search:
        return _responses_tool_choice(tool_choice)
    if isinstance(tool_choice, dict) and str(tool_choice.get("type") or "").strip() == "function":
        if "name" in tool_choice:
            name = str(tool_choice.get("name") or "").strip()
        else:
            function = tool_choice.get("function")
            name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        if name == _ALYSIS_WEB_SEARCH_FUNCTION_NAME:
            raise LLMError(
                "OpenAI Responses removed the Alysis Code web_search function for the selected "
                "web_search_mode; do not force tool_choice to function web_search"
            )
    return _responses_tool_choice(tool_choice)


def _responses_tool_choice(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip()
        if normalized in _RESPONSES_TOOL_CHOICE_STRINGS:
            return normalized
        raise LLMError(f"OpenAI Responses does not support tool_choice={tool_choice!r}")
    if not isinstance(tool_choice, dict):
        raise LLMError("OpenAI Responses tool_choice must be a string or object")

    choice_type = str(tool_choice.get("type") or "").strip()
    if choice_type == "function":
        if "name" in tool_choice:
            name = str(tool_choice.get("name") or "").strip()
        else:
            function = tool_choice.get("function")
            name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        if not name:
            raise LLMError("OpenAI Responses forced function tool_choice is missing name")
        return {"type": "function", "name": name}
    if choice_type == "allowed_tools":
        return copy.deepcopy(tool_choice)
    if choice_type in {"web_search", "web_search_preview"}:
        return copy.deepcopy(tool_choice)
    raise LLMError(f"OpenAI Responses does not support tool_choice type {choice_type!r}")


def _responses_text_config(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    if not response_format:
        return None
    if "format" in response_format and isinstance(response_format.get("format"), dict):
        return copy.deepcopy(response_format)

    response_type = str(response_format.get("type") or "").strip()
    if response_type == "json_schema":
        raw_json_schema = response_format.get("json_schema")
        json_schema = raw_json_schema if isinstance(raw_json_schema, dict) else response_format
        name = str(json_schema.get("name") or "").strip()
        schema = json_schema.get("schema")
        if not name or not _RESPONSES_JSON_SCHEMA_NAME_RE.fullmatch(name):
            raise LLMError(
                "OpenAI Responses json_schema response_format requires a valid name "
                "(letters, digits, underscores, or dashes; max 64 chars)"
            )
        if not isinstance(schema, dict):
            raise LLMError("OpenAI Responses json_schema response_format requires schema object")
        fmt: dict[str, Any] = {
            "type": "json_schema",
            "name": name,
            "schema": copy.deepcopy(schema),
        }
        description = str(json_schema.get("description") or "").strip()
        if description:
            fmt["description"] = description
        if "strict" in json_schema:
            fmt["strict"] = bool(json_schema.get("strict"))
        return {"format": fmt}
    if response_type in {"json_object", "text"}:
        return {"format": {"type": response_type}}
    raise LLMError(f"OpenAI Responses does not support response_format type {response_type!r}")


def _responses_reasoning(
    *,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
    request_summary: bool = False,
    provider_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    reasoning: dict[str, Any] = {}
    effort = str(reasoning_effort or "").strip().lower()
    if model == "gpt-6.1-sol" and (
        (effort and effort not in {"low", "medium", "high", "xhigh", "max"})
        or (not effort and enable_thinking is False)
    ):
        raise LLMError("GPT-6.1 Sol requires reasoning effort low, medium, high, xhigh, or max.")
    if provider_key == "alysis" and model == "gpt-6-luna":
        # Older hosted pickers exposed the generic Responses efforts. Preserve
        # saved configs and environment overrides at Luna's supported bounds.
        effort = {"minimal": "low", "ultra": "max"}.get(effort, effort)
    if effort:
        if effort not in _RESPONSES_REASONING_EFFORTS:
            raise LLMError(f"OpenAI Responses reasoning_effort is not supported: {effort}")
        reasoning["effort"] = effort
    elif enable_thinking is False:
        reasoning["effort"] = "none"
    if request_summary:
        # Summary visibility is independent from reasoning effort. In
        # particular, an automatic/default effort must remain omitted while
        # still asking the provider for the best supported summary.
        reasoning["summary"] = "auto"
    return reasoning or None


def _parse_usage(raw: Any) -> LLMUsage | None:
    usage = parse_compatible_usage(raw, responses_shape=True)
    if usage is None:
        return None
    if (
        usage.prompt_tokens is None
        and usage.completion_tokens is None
        and usage.total_tokens is None
        and usage.cached_prompt_tokens is None
        and usage.cache_creation_input_tokens is None
        and usage.reasoning_tokens is None
        and usage.provider_cost_usd is None
    ):
        return None
    return usage


def _citation_to_dict(citation: WebSearchCitation) -> dict[str, Any]:
    return {
        "title": citation.title,
        "url": citation.url,
        "start_index": citation.start_index,
        "end_index": citation.end_index,
    }


def _source_to_dict(source: WebSearchSource) -> dict[str, Any]:
    return {"title": source.title, "url": source.url}


def _response_hit_output_limit(data: dict[str, Any]) -> bool:
    details = data.get("incomplete_details")
    return (
        data.get("status") == "incomplete"
        and isinstance(details, dict)
        and details.get("reason") == "max_output_tokens"
    )


def _responses_provider_metadata(data: dict[str, Any]) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {}
    response_id = str(data.get("id") or "").strip()
    if response_id:
        metadata["response_id"] = response_id
    output = data.get("output")
    if isinstance(output, list):
        if _response_hit_output_limit(data):
            # No client-side calls from an interrupted generation are executed.
            # Do not replay orphan calls (even syntactically valid ones), or use
            # a stored response ID that would restore those calls server-side.
            safe_output = [
                item
                for item in output
                if isinstance(item, dict) and item.get("type") != "function_call"
            ]
            if len(safe_output) != len(output):
                metadata.pop("response_id", None)
            output = safe_output
        normalized_output, call_ids_rewritten = _normalized_responses_output_items(output)
        metadata["output_items"] = normalized_output
        if call_ids_rewritten:
            metadata["call_ids_rewritten"] = True
            metadata["call_id_normalization"] = _RESPONSES_CALL_ID_NORMALIZATION_SCHEME
        web_search_calls = [
            copy.deepcopy(item)
            for item in normalized_output
            if isinstance(item, dict) and str(item.get("type") or "") == "web_search_call"
        ]
        if web_search_calls:
            metadata["web_search_calls"] = web_search_calls
    citations = [_citation_to_dict(citation) for citation in _extract_citations(data)]
    if citations:
        metadata["citations"] = citations
    sources, queries = _extract_sources_and_queries(data)
    sources = _merge_citation_sources(sources, _extract_citations(data))
    if sources:
        metadata["sources"] = [_source_to_dict(source) for source in sources]
    if queries:
        metadata["queries"] = list(queries)
    stream_metadata = data.get("stream_metadata")
    if isinstance(stream_metadata, dict):
        metadata["stream_metadata"] = copy.deepcopy(stream_metadata)
    return {_OPENAI_RESPONSES_METADATA_KEY: metadata} if metadata else None


def _responses_reasoning_outputs(data: dict[str, Any]) -> tuple[ReasoningOutput, ...]:
    """Extract provider-generated summaries, never opaque/raw reasoning state."""

    output = data.get("output")
    if not isinstance(output, list):
        return ()
    seen: set[str] = set()
    summaries: list[ReasoningOutput] = []
    for item in output:
        if not isinstance(item, dict) or str(item.get("type") or "") != "reasoning":
            continue
        parts = item.get("summary")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if str(part.get("type") or "") not in {"summary_text", "text"}:
                continue
            summary = part.get("text")
            if not isinstance(summary, str) or not summary.strip() or summary in seen:
                continue
            seen.add(summary)
            summaries.append(
                ReasoningOutput(
                    text=summary,
                    kind=ReasoningOutputKind.SUMMARY,
                    provider="openai",
                )
            )
    return tuple(summaries)


def _has_responses_reasoning_output(data: dict[str, Any]) -> bool:
    """Return whether the provider produced a reasoning item of any visibility class."""

    output = data.get("output")
    return isinstance(output, list) and any(
        isinstance(item, dict) and str(item.get("type") or "") == "reasoning" for item in output
    )


def _extract_refusal(data: dict[str, Any]) -> str:
    output = data.get("output")
    if not isinstance(output, list):
        return ""
    refusals: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            refusal = part.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                refusals.append(refusal.strip())
            if str(part.get("type") or "") == "refusal":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    refusals.append(text.strip())
    return "\n".join(refusals)


def _parse_response_tool_calls(data: dict[str, Any]) -> list[ToolCall]:
    output = data.get("output")
    if not isinstance(output, list):
        return []
    call_ids = _responses_function_call_ids_by_output_index(output)
    tool_calls: list[ToolCall] = []
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "function_call":
            continue
        call_id = call_ids[index]
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        provider_metadata: dict[str, Any] = {
            _OPENAI_RESPONSES_METADATA_KEY: {
                "item_id": item.get("id"),
                "output_index": index,
                "status": item.get("status"),
            }
        }
        checked = resolved_entry.arguments(item.get("arguments"), name=name, call_id=call_id)
        if resolved_entry.active() and name == resolved_entry.NAME and item.get("name") != name:
            checked = ({}, None)
        restricted_args = github_policy_arguments(item.get("arguments"), name)
        tool_calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=(
                    checked[0]
                    if checked is not None
                    else restricted_args
                    if restricted_args is not None
                    else _parse_arguments(item.get("arguments") or "{}")
                ),
                _resolved_entry_proof=checked[1] if checked is not None else None,
                provider_metadata=provider_metadata,
            )
        )
    return tool_calls


def _response_from_json(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, content=_json_bytes(data), headers={"content-type": "application/json"}
    )


def _event_output_index(data: dict[str, Any], *, event_type: str) -> int:
    index = data.get("output_index")
    if isinstance(index, int) and index >= 0:
        return index
    raise LLMError(f"OpenAI Responses stream {event_type} event is missing output_index")


def _event_content_index(data: dict[str, Any]) -> int:
    index = data.get("content_index")
    if isinstance(index, int) and index >= 0:
        return index
    return 0


def _stream_error_message(data: dict[str, Any]) -> str:
    message = _extract_error_message(data)
    if message:
        return message
    response = data.get("response")
    if isinstance(response, dict):
        message = _extract_error_message(response)
        if message:
            return message
        status = str(response.get("status") or "").strip()
        incomplete = response.get("incomplete_details")
        if isinstance(incomplete, dict):
            reason = str(incomplete.get("reason") or "").strip()
            if reason:
                return f"status={status or 'incomplete'} reason={reason}"
        if status:
            return f"status={status}"
    return repr(data)


def _responses_stream_error(data: dict[str, Any], event_type: str) -> LLMError:
    """Retain closed provider metadata from HTTP-200 SSE failures.

    Protected desktop turns discard exception text. Structured codes must survive
    that boundary, and transient errors use the existing bounded request retry
    policy. No tool action is dispatched or replayed by this helper.
    """
    response = data.get("response")
    container = response if isinstance(response, dict) else data
    detail = container.get("error")
    detail = detail if isinstance(detail, dict) else container
    codes = [
        value
        for value in (detail.get("code"), detail.get("type"))
        if type(value) is str and value in DIAGNOSTIC_CODES
    ]
    code = next((value for value in codes if value in HARD_QUOTA_CODES), next(iter(codes), None))
    status = next(
        (
            value
            for value in (detail.get("status_code"), detail.get("status"), detail.get("code"))
            if type(value) is int and 400 <= value <= 599
        ),
        None,
    )
    if status is None:
        status = {
            "rate_limit_exceeded": 429,
            "insufficient_quota": 429,
            "usage_limit_reached": 429,
            "billing_hard_limit_reached": 429,
            "server_error": 500,
            "internal_error": 500,
            "overloaded_error": 503,
        }.get(code)
    error = set_safe_provider_error_fields(
        LLMError(
            sanitize_error_text_for_output(
                f"OpenAI Responses stream {event_type}: {_stream_error_message(data)}"
            )
        ),
        status=status,
        code=code,
        parameter=detail.get("param"),
    )
    error.provider_error_origin = "responses_stream"
    if code in HARD_QUOTA_CODES:
        mark_provider_call_non_retryable(error)
    return error


class _OpenAIResponsesStreamAccumulator:
    def __init__(
        self,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None,
    ) -> None:
        self.on_text_delta = on_text_delta
        self.on_reasoning_delta = on_reasoning_delta
        self.response: dict[str, Any] = {"object": "response", "output": []}
        self.output_items: dict[int, dict[str, Any]] = {}
        self._resolved_selectors: dict[int, dict[str, str]] = {}
        self.text_parts: dict[tuple[int, int], dict[str, Any]] = {}
        self.reasoning_summary_parts: dict[tuple[int, int], dict[str, Any]] = {}
        self.reasoning_delta_keys: set[tuple[int, int] | None] = set()
        self.argument_chunks: dict[int, list[str]] = {}
        self.unknown_events: list[dict[str, Any]] = []
        self.event_count = 0
        self.text_delta_seen = False
        self.seen_final = False
        self.final_response: dict[str, Any] | None = None
        self.progress_revision = 0

    def _mark_progress(self) -> None:
        self.progress_revision += 1

    def handle(self, frame: SSEFrame, data: dict[str, Any]) -> None:
        event_type = str(data.get("type") or frame.event or "").strip()
        if not event_type:
            self._append_unknown(frame=frame, data=data)
            return
        self.event_count += 1

        if event_type == "error":
            raise _responses_stream_error(data, event_type)
        if event_type == "response.incomplete":
            response = data.get("response")
            if isinstance(response, dict) and _response_hit_output_limit(response):
                self._handle_final_response(data)
                return
            raise LLMError(f"OpenAI Responses stream {event_type}: {_stream_error_message(data)}")
        if event_type == "response.failed":
            raise _responses_stream_error(data, event_type)
        if event_type in {"response.created", "response.in_progress", "response.queued"}:
            self._merge_response(data.get("response"))
            return
        if event_type in {"response.completed", "response.done"}:
            self._handle_final_response(data)
            return
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            self._handle_output_item(data)
            return
        if event_type in {"response.content_part.added", "response.content_part.done"}:
            self._handle_content_part(data)
            return
        if event_type == "response.output_text.delta":
            self._handle_output_text_delta(data)
            return
        if event_type == "response.output_text.done":
            self._handle_output_text_done(data)
            return
        if event_type == "response.output_text.annotation.added":
            self._handle_output_text_annotation(data)
            return
        if event_type in {
            "response.reasoning_summary_text.delta",
            # Compatibility event emitted by older/private Responses surfaces.
            "response.reasoning_summary.delta",
        }:
            self._handle_reasoning_summary_delta(data)
            return
        if event_type in {
            "response.reasoning_summary_text.done",
            "response.reasoning_summary.done",
        }:
            self._handle_reasoning_summary_done(data)
            return
        if event_type in {
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
        }:
            self._handle_reasoning_summary_part(data, done=event_type.endswith(".done"))
            return
        if event_type == "response.function_call_arguments.delta":
            self._handle_function_arguments_delta(data)
            return
        if event_type == "response.function_call_arguments.done":
            self._handle_function_arguments_done(data)
            return
        if event_type.startswith("response.web_search_call."):
            self._handle_web_search_call_state(event_type, data)
            return

        self._append_unknown(frame=frame, data=data)

    def finish(self) -> dict[str, Any]:
        if self.event_count <= 0:
            raise LLMError("OpenAI Responses stream returned no events")
        data = copy.deepcopy(self.final_response or self.response)
        output = data.get("output")
        if not isinstance(output, list) or not output:
            output = self._ordered_output_items()
            data["output"] = output
        elif self.output_items:
            data["output"] = self._merge_ordered_items(output)
        reasoning_only_partial = bool(output) and all(
            isinstance(item, dict) and str(item.get("type") or "") == "reasoning" for item in output
        )
        if not self.seen_final and not reasoning_only_partial:
            raise LLMError("OpenAI Responses stream ended before response.completed")
        if "output_text" not in data:
            text = _extract_answer_text(data)
            if text:
                data["output_text"] = text
        stream_metadata: dict[str, Any] = {"events": self.event_count}
        if not self.seen_final:
            stream_metadata["ended_before_response_completed"] = True
        if self.unknown_events:
            stream_metadata["unknown_events"] = copy.deepcopy(self.unknown_events)
        data["stream_metadata"] = stream_metadata
        return data

    def _merge_response(self, raw_response: Any) -> None:
        if not isinstance(raw_response, dict):
            return
        response = copy.deepcopy(raw_response)
        output = response.pop("output", None)
        if any(
            key not in self.response or self.response[key] != value
            for key, value in response.items()
        ):
            self.response.update(response)
            self._mark_progress()
        if isinstance(output, list):
            for index, item in enumerate(output):
                if isinstance(item, dict):
                    self._set_output_item(index, item)

    def _handle_final_response(self, data: dict[str, Any]) -> None:
        self._merge_response(data.get("response"))
        for index, item in self.output_items.items():
            self._emit_completed_reasoning_summary(index, item)
        response = data.get("response")
        final_response = (
            copy.deepcopy(response) if isinstance(response, dict) else copy.deepcopy(self.response)
        )
        if not self.seen_final or self.final_response != final_response:
            self._mark_progress()
        self.final_response = final_response
        self.seen_final = True

    def _handle_output_item(self, data: dict[str, Any]) -> None:
        index = _event_output_index(data, event_type=str(data.get("type") or "output_item"))
        item = data.get("item")
        if not isinstance(item, dict):
            return
        self._set_output_item(index, item)
        if str(data.get("type") or "").endswith(".done"):
            self._emit_completed_reasoning_summary(index, item)

    def _emit_completed_reasoning_summary(self, index: int, item: dict[str, Any]) -> None:
        # Compatible endpoints may deliver a whole summary on the completed
        # item/response instead of separate summary deltas. Reuse the same
        # per-part deduplication and answer-ordering guard as summary.done.
        if item.get("type") != "reasoning" or not isinstance(item.get("summary"), list):
            return
        for summary_index, part in enumerate(item["summary"]):
            if isinstance(part, dict) and part.get("type") == "summary_text":
                self._handle_reasoning_summary_done(
                    {
                        "output_index": index,
                        "summary_index": summary_index,
                        "item_id": item.get("id"),
                        "text": part.get("text"),
                    }
                )

    def _check_resolved_selectors(self, index: int, item: dict[str, Any]) -> None:
        if not resolved_entry.active():
            return
        # Remember first non-empty selectors even across empty/missing updates.
        selectors = self._resolved_selectors.setdefault(index, {})
        for key in ("id", "call_id", "name"):
            value = item.get(key)
            if isinstance(value, str) and value:
                previous = selectors.setdefault(key, value)
                if previous != value:
                    resolved_entry.invalidate_parent()

    def _set_output_item(self, index: int, item: dict[str, Any]) -> None:
        self._check_resolved_selectors(index, item)
        copied = copy.deepcopy(item)
        existing = self.output_items.get(index)
        if existing is not None:
            merged = copy.deepcopy(existing)
            merged.update(copied)
            copied = merged
        if existing != copied:
            self.output_items[index] = copied
            self._mark_progress()

    def _ensure_message_item(self, output_index: int, item_id: str | None = None) -> dict[str, Any]:
        item = self.output_items.get(output_index)
        if not isinstance(item, dict):
            item = {
                "type": "message",
                "role": "assistant",
                "content": [],
            }
            if item_id:
                item["id"] = item_id
            self.output_items[output_index] = item
            self._mark_progress()
        else:
            changed = False
            if "type" not in item:
                changed = True
            item.setdefault("type", "message")
            if "role" not in item:
                changed = True
            item.setdefault("role", "assistant")
            if item_id and not item.get("id"):
                item["id"] = item_id
                changed = True
            content = item.get("content")
            if not isinstance(content, list):
                item["content"] = []
                changed = True
            if changed:
                self._mark_progress()
        return item

    def _ensure_text_part(
        self, output_index: int, content_index: int, item_id: str
    ) -> dict[str, Any]:
        key = (output_index, content_index)
        part = self.text_parts.get(key)
        if part is None:
            part = {"type": "output_text", "text": ""}
            self.text_parts[key] = part
            item = self._ensure_message_item(output_index, item_id)
            content = item.setdefault("content", [])
            if isinstance(content, list):
                while len(content) <= content_index:
                    content.append({"type": "output_text", "text": ""})
                content[content_index] = part
            self._mark_progress()
        return part

    def _handle_content_part(self, data: dict[str, Any]) -> None:
        output_index = _event_output_index(data, event_type=str(data.get("type") or "content_part"))
        content_index = _event_content_index(data)
        item_id = str(data.get("item_id") or "").strip()
        raw_part = data.get("part")
        if not isinstance(raw_part, dict):
            return
        item = self._ensure_message_item(output_index, item_id or None)
        content = item.setdefault("content", [])
        if not isinstance(content, list):
            content = []
            item["content"] = content
            self._mark_progress()
        while len(content) <= content_index:
            content.append({"type": "output_text", "text": ""})
            self._mark_progress()
        part = copy.deepcopy(raw_part)
        if str(part.get("type") or "") in {"text", "output_text"}:
            part["type"] = "output_text"
            part.setdefault("text", "")
            self.text_parts[(output_index, content_index)] = part
        changed = content[content_index] != part
        content[content_index] = part
        if changed:
            self._mark_progress()

    def _handle_output_text_delta(self, data: dict[str, Any]) -> None:
        output_index = _event_output_index(data, event_type="response.output_text.delta")
        content_index = _event_content_index(data)
        item_id = str(data.get("item_id") or "").strip()
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        part = self._ensure_text_part(output_index, content_index, item_id)
        existing = part.get("text")
        part["text"] = (existing if isinstance(existing, str) else "") + delta
        self.text_delta_seen = True
        self._mark_progress()
        if self.on_text_delta is not None:
            self.on_text_delta(delta)

    def _handle_output_text_done(self, data: dict[str, Any]) -> None:
        output_index = _event_output_index(data, event_type="response.output_text.done")
        content_index = _event_content_index(data)
        item_id = str(data.get("item_id") or "").strip()
        text = data.get("text")
        if not isinstance(text, str):
            return
        part = self._ensure_text_part(output_index, content_index, item_id)
        if part.get("text") != text or part.get("type") != "output_text":
            part["text"] = text
            part["type"] = "output_text"
            self._mark_progress()

    def _handle_output_text_annotation(self, data: dict[str, Any]) -> None:
        output_index = _event_output_index(
            data,
            event_type="response.output_text.annotation.added",
        )
        content_index = _event_content_index(data)
        item_id = str(data.get("item_id") or "").strip()
        annotation = data.get("annotation")
        if not isinstance(annotation, dict):
            return
        part = self._ensure_text_part(output_index, content_index, item_id)
        annotations = part.setdefault("annotations", [])
        if isinstance(annotations, list):
            annotations.append(copy.deepcopy(annotation))
            self._mark_progress()

    @staticmethod
    def _reasoning_indices(data: dict[str, Any]) -> tuple[int, int] | None:
        output_index = data.get("output_index")
        summary_index = data.get("summary_index")
        if not isinstance(output_index, int) or output_index < 0:
            return None
        if not isinstance(summary_index, int) or summary_index < 0:
            summary_index = 0
        return output_index, summary_index

    def _ensure_reasoning_summary_part(
        self,
        *,
        output_index: int,
        summary_index: int,
        item_id: str,
    ) -> dict[str, Any] | None:
        key = (output_index, summary_index)
        existing_part = self.reasoning_summary_parts.get(key)
        if existing_part is not None:
            return existing_part

        item = self.output_items.get(output_index)
        if item is None:
            item = {"type": "reasoning", "summary": []}
            if item_id:
                item["id"] = item_id
            self.output_items[output_index] = item
        elif str(item.get("type") or "") not in {"", "reasoning"}:
            # Malformed/colliding provider events must not overwrite another
            # output item merely to reconstruct optional reasoning metadata.
            return None
        else:
            item.setdefault("type", "reasoning")
            if item_id and not item.get("id"):
                item["id"] = item_id

        summary = item.get("summary")
        if not isinstance(summary, list):
            summary = []
            item["summary"] = summary
        while len(summary) <= summary_index:
            summary.append({"type": "summary_text", "text": ""})
        part = summary[summary_index]
        if not isinstance(part, dict):
            part = {"type": "summary_text", "text": ""}
            summary[summary_index] = part
        else:
            part.setdefault("type", "summary_text")
            part.setdefault("text", "")
        self.reasoning_summary_parts[key] = part
        self._mark_progress()
        return part

    def _reasoning_part_for_event(self, data: dict[str, Any]) -> dict[str, Any] | None:
        indices = self._reasoning_indices(data)
        if indices is None:
            return None
        output_index, summary_index = indices
        return self._ensure_reasoning_summary_part(
            output_index=output_index,
            summary_index=summary_index,
            item_id=str(data.get("item_id") or "").strip(),
        )

    def _handle_reasoning_summary_delta(self, data: dict[str, Any]) -> None:
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        key = self._reasoning_indices(data)
        part = self._reasoning_part_for_event(data)
        if part is not None:
            existing = part.get("text")
            part["text"] = (existing if isinstance(existing, str) else "") + delta
        self.reasoning_delta_keys.add(key)
        self._mark_progress()
        if self.on_reasoning_delta is not None:
            self.on_reasoning_delta(delta)

    def _handle_reasoning_summary_done(self, data: dict[str, Any]) -> None:
        text = data.get("text")
        if not isinstance(text, str):
            return
        key = self._reasoning_indices(data)
        part = self._reasoning_part_for_event(data)
        if part is not None:
            if part.get("type") != "summary_text" or part.get("text") != text:
                part["type"] = "summary_text"
                part["text"] = text
                self._mark_progress()
        # Some compatible providers send only the completed summary event. It is
        # still genuine provider output, so surface it once, but never inject it
        # after visible answer text has already started or duplicate prior deltas.
        if (
            text
            and self.on_reasoning_delta is not None
            and key not in self.reasoning_delta_keys
            and None not in self.reasoning_delta_keys
            and not self.text_delta_seen
        ):
            self.reasoning_delta_keys.add(key)
            self._mark_progress()
            self.on_reasoning_delta(text)

    def _handle_reasoning_summary_part(self, data: dict[str, Any], *, done: bool) -> None:
        raw_part = data.get("part")
        if not isinstance(raw_part, dict):
            return
        part = self._reasoning_part_for_event(data)
        if part is None:
            return
        updated = copy.deepcopy(part)
        updated.update(copy.deepcopy(raw_part))
        updated["type"] = "summary_text"
        if part != updated:
            part.clear()
            part.update(updated)
            self._mark_progress()
        text = raw_part.get("text")
        if done and isinstance(text, str):
            completed = dict(data)
            completed["text"] = text
            self._handle_reasoning_summary_done(completed)

    def _handle_function_arguments_delta(self, data: dict[str, Any]) -> None:
        output_index = _event_output_index(
            data,
            event_type="response.function_call_arguments.delta",
        )
        self._check_resolved_selectors(
            output_index,
            {
                **data,
                "id": data.get("item_id"),
            },
        )
        delta = data.get("delta")
        if isinstance(delta, str):
            item = self.output_items.get(output_index)
            name = (
                item.get("name", resolved_entry.NAME)
                if isinstance(item, dict)
                else resolved_entry.NAME
            )
            if resolved_entry.active():
                resolved_entry.bound_stream(
                    "".join(self.argument_chunks.get(output_index, ())) + delta, name
                )
            self.argument_chunks.setdefault(output_index, []).append(delta)
        item = self.output_items.get(output_index)
        if isinstance(item, dict) and str(item.get("type") or "") == "function_call":
            existing = item.get("arguments")
            item["arguments"] = (existing if isinstance(existing, str) else "") + (
                delta if isinstance(delta, str) else ""
            )
        if isinstance(delta, str) and delta:
            self._mark_progress()

    def _handle_function_arguments_done(self, data: dict[str, Any]) -> None:
        output_index = _event_output_index(
            data,
            event_type="response.function_call_arguments.done",
        )
        self._check_resolved_selectors(
            output_index,
            {
                **data,
                "id": data.get("item_id"),
            },
        )
        item = self.output_items.get(output_index)
        before = copy.deepcopy(item) if isinstance(item, dict) else None
        if not isinstance(item, dict):
            item = {"type": "function_call"}
            self.output_items[output_index] = item
        item["type"] = "function_call"
        for key in ("item_id", "call_id", "name", "status"):
            value = data.get(key)
            if value is not None:
                item["id" if key == "item_id" else key] = copy.deepcopy(value)
        arguments = data.get("arguments")
        if isinstance(arguments, str):
            item["arguments"] = arguments
        elif output_index in self.argument_chunks:
            item["arguments"] = "".join(self.argument_chunks[output_index])
        if before != item:
            self._mark_progress()

    def _handle_web_search_call_state(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            output_index = _event_output_index(data, event_type=event_type)
        except LLMError:
            self._append_unknown(frame=SSEFrame(event=event_type, data=json.dumps(data)), data=data)
            return
        item = self.output_items.get(output_index)
        before = copy.deepcopy(item) if isinstance(item, dict) else None
        if not isinstance(item, dict):
            item = {"type": "web_search_call"}
            self.output_items[output_index] = item
        item["type"] = "web_search_call"
        item_id = data.get("item_id")
        if isinstance(item_id, str) and item_id.strip():
            item["id"] = item_id
        status = event_type.rsplit(".", 1)[-1]
        if status:
            item["status"] = status
        for key in ("action", "results"):
            value = data.get(key)
            if value is not None:
                item[key] = copy.deepcopy(value)
        if before != item:
            self._mark_progress()

    def _ordered_output_items(self) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(item)
            for _index, item in sorted(self.output_items.items(), key=lambda pair: pair[0])
            if isinstance(item, dict)
        ]

    def _merge_ordered_items(self, output: list[Any]) -> list[dict[str, Any]]:
        merged: dict[int, dict[str, Any]] = {}
        for index, item in enumerate(output):
            if isinstance(item, dict):
                merged[index] = copy.deepcopy(item)
        for index, item in self.output_items.items():
            if not isinstance(item, dict):
                continue
            if index in merged:
                copied = copy.deepcopy(item)
                copied.update(copy.deepcopy(merged[index]))
                merged[index] = copied
            else:
                merged[index] = copy.deepcopy(item)
        return [item for _index, item in sorted(merged.items(), key=lambda pair: pair[0])]

    def _append_unknown(self, *, frame: SSEFrame, data: dict[str, Any]) -> None:
        self.unknown_events.append(
            {
                "event": frame.event,
                "data": copy.deepcopy(data),
            }
        )


class OpenAIResponsesClient:
    # Requests may continue server-side via previous_response_id. Replaying the
    # visible messages is a different cache stream and cannot refresh that prefix.
    cache_keepalive_transport = "response_continuation"
    usage_contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        input_token_count_strategy="openai_responses",
    )
    supports_tool_calling = True
    supports_forced_tool_choice = True
    # The subscription adapter lifts only the initial instruction prefix;
    # later system/developer messages retain their position in input.
    preserves_late_system_message_position = True
    # Neither serialization nor retry removes an explicit non-executing choice.
    preserves_tool_choice_none = True
    usage_counts_authoritative = usage_contract.response_usage_authoritative

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 20.0,
        temperature: float = 1.0,
        prompt_cache_key: str | None = None,
        prompt_cache_retention: str | None = None,
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
        transport: httpx.BaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_key: str | None = None,
        web_search_mode: str = "off",
        web_search_adapter: str = AUTO_WEB_SEARCH_ADAPTER,
        provider_concurrency_caps: dict[str, int] | None = None,
        provider_retry_settings: ProviderRetrySettings | None = None,
        provider_sleep_fn: Callable[[float], None] | None = None,
        provider_random_fn: Callable[[], float] | None = None,
        prompt_cache_policy_metadata: Mapping[str, Any] | None = None,
        provider_auth: ProviderAuthAdapter | None = None,
        session_id: str | None = None,
        usage_contract: UsageContract | None = None,
        route_identity: ProviderRouteIdentity | None = None,
        stream_no_progress_timeout_s: float = 240.0,
        stream_progress_clock: Callable[[], float] | None = None,
        inflight_deadline_grace_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.prompt_cache_key = str(prompt_cache_key or "").strip() or None
        self.prompt_cache_retention = str(prompt_cache_retention or "").strip() or None
        self.enable_thinking = enable_thinking
        self.reasoning_effort = str(reasoning_effort or "").strip().lower() or None
        self._transport = transport
        self.extra_headers = canonicalize_extra_headers(extra_headers)
        self.provider_key = str(provider_key or "").strip() or None
        if route_identity is None:
            credential_scope = credential_scope_fingerprint(self.api_key)
            if provider_auth is not None:
                auth_scope = getattr(provider_auth, "route_credential_scope", None)
                if callable(auth_scope):
                    credential_scope = str(auth_scope() or "").strip() or credential_scope
            route_identity = build_provider_route_identity(
                protocol="openai_responses",
                base_url=self.base_url,
                provider_key=self.provider_key,
                model=self.model,
                credential_scope=credential_scope,
                routing_headers=self.extra_headers,
                session_scope=credential_scope_fingerprint(session_id),
            )
        self.route_identity = route_identity
        self.web_search_mode = str(web_search_mode or "off").strip().lower()
        self.web_search_adapter = (
            str(web_search_adapter or AUTO_WEB_SEARCH_ADAPTER).strip().lower()
            or AUTO_WEB_SEARCH_ADAPTER
        )
        self.provider_concurrency_caps = dict(
            DEFAULT_PROVIDER_CONCURRENCY_CAPS
            if provider_concurrency_caps is None
            else provider_concurrency_caps
        )
        self.provider_retry_settings = provider_retry_settings or ProviderRetrySettings()
        self._provider_sleep_fn = provider_sleep_fn
        self._provider_random_fn = provider_random_fn
        self.prompt_cache_policy_metadata = (
            copy.deepcopy(dict(prompt_cache_policy_metadata))
            if isinstance(prompt_cache_policy_metadata, Mapping)
            else None
        )
        self.provider_auth = provider_auth
        self.session_id = str(session_id or "").strip() or None
        self.usage_contract = usage_contract or type(self).usage_contract
        # Auth adapters can target a narrower endpoint surface than the public
        # API. Counting requires an explicit opt-in on those routes; response
        # usage and billing guarantees remain independent of that capability.
        if (
            provider_auth is not None
            and getattr(provider_auth, "supports_input_token_count", False) is not True
        ):
            self.usage_contract = replace(self.usage_contract, input_token_count_strategy="none")
        self.usage_counts_authoritative = self.usage_contract.response_usage_authoritative
        self._input_token_count_available: bool | None = None
        self._reasoning_summary_support_by_model: dict[str, bool] = {}
        self.stream_no_progress_timeout_s = max(
            0.001,
            float(stream_no_progress_timeout_s),
        )
        self._stream_progress_clock = stream_progress_clock or monotonic
        self.inflight_deadline_grace_s = max(
            0.0,
            float(inflight_deadline_grace_s),
        )
        self._provider_retry_wall_clock_cap_seconds = _PROVIDER_RETRY_WALL_CLOCK_CAP_SECONDS
        self._wire_request_diagnostics = WireRequestDiagnostics()

    def _reasoning_summary_support_key(self) -> str:
        return _responses_temperature_omit_key(self.base_url, self.model)

    def _should_request_reasoning_summary(self) -> bool:
        if omitted_reasoning_display_required(self):
            return False
        capability = getattr(self, "reasoning_trace_capability", None)
        if self.enable_thinking is False:
            return False
        if not bool(getattr(capability, "requestable", False)):
            return False
        if not bool(getattr(capability, "has_safe_summary", False)):
            return False
        return (
            self._reasoning_summary_support_by_model.get(self._reasoning_summary_support_key())
            is not False
        )

    def _headers(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        cancellation_token: Any | None = None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "alysis-code/0.1.0",
        }
        if httpx.URL(url).host == _PERPLEXITY_API_HOST:
            headers["X-Pplx-Integration"] = _PERPLEXITY_INTEGRATION
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers = merge_canonical_headers(headers, self.extra_headers)
        if self.provider_auth is not None:
            for key in tuple(headers):
                if key.casefold() in {
                    "authorization",
                    "chatgpt-account-id",
                    "originator",
                    "session-id",
                    "x-session-affinity",
                    "x-session-id",
                }:
                    headers.pop(key, None)
            expected_scope = str(self.route_identity.credential_scope or "").strip()
            authorize_request = getattr(self.provider_auth, "authorize_request", None)
            if callable(authorize_request):
                if not expected_scope:
                    raise ProviderAuthError(
                        "The selected provider account is missing an approved route identity."
                    )
                authorization_kwargs: dict[str, Any] = {
                    "force_refresh": force_refresh,
                    "session_id": self.session_id,
                    "expected_credential_scope": expected_scope or None,
                }
                if cancellation_token is not None and _callable_accepts_keyword(
                    authorize_request,
                    "cancellation_token",
                ):
                    authorization_kwargs["cancellation_token"] = cancellation_token
                authorization = authorize_request(url, **authorization_kwargs)
                live_scope = str(authorization.credential_scope or "").strip()
                if expected_scope and not hmac.compare_digest(live_scope, expected_scope):
                    raise ProviderCredentialScopeMismatchError(
                        "The selected provider account changed before the request was sent."
                    )
                provider_headers = authorization.headers
            else:
                if expected_scope:
                    raise ProviderAuthError(
                        "The selected provider authentication adapter cannot bind requests "
                        "to the approved account."
                    )
                authorization_headers = self.provider_auth.authorization_headers
                authorization_kwargs = {
                    "force_refresh": force_refresh,
                    "session_id": self.session_id,
                }
                if cancellation_token is not None and _callable_accepts_keyword(
                    authorization_headers,
                    "cancellation_token",
                ):
                    authorization_kwargs["cancellation_token"] = cancellation_token
                provider_headers = authorization_headers(url, **authorization_kwargs)
            headers = merge_canonical_headers(headers, provider_headers)
        return _headers_with_default_accept_encoding(headers)

    @staticmethod
    def _error_from_response(response: httpx.Response) -> ResponsesError:
        try:
            data = response.json()
        except Exception:
            body = response.text
            if len(body) > 1000:
                body = body[:1000] + "...(truncated)"
            return ResponsesError(
                sanitize_error_text_for_output(f"Responses error {response.status_code}: {body}")
            )
        if isinstance(data, dict):
            error_message = _extract_error_message(data)
            if error_message:
                lower = error_message.lower()
                if "unsupported" in lower or "not support" in lower:
                    return ResponsesError(
                        sanitize_error_text_for_output(
                            f"Responses web_search unsupported: {error_message}"
                        )
                    )
                return ResponsesError(
                    sanitize_error_text_for_output(
                        f"Responses error {response.status_code}: {error_message}"
                    )
                )
        return ResponsesError(
            sanitize_error_text_for_output(f"Responses error {response.status_code}: {data!r}")
        )

    @staticmethod
    def _llm_error_from_response(response: httpx.Response) -> LLMError:
        try:
            data = response.json()
        except Exception:
            body = response.text
            if len(body) > 1000:
                body = body[:1000] + "...(truncated)"
            retry_after, reset_at = retry_metadata(response, {})
            return set_safe_provider_error_fields(
                LLMError(
                    sanitize_error_text_for_output(f"LLM error {response.status_code}: {body}")
                ),
                status=response.status_code,
                retry_after=retry_after,
                reset_at=reset_at,
            )
        error_message = _extract_error_message(data)
        raw_error = data.get("error") if isinstance(data, dict) else None
        if isinstance(raw_error, dict) and isinstance(raw_error.get("code"), str):
            error_text = json.dumps(data)
        else:
            error_text = error_message or repr(data)
        error = LLMError(
            sanitize_error_text_for_output(f"LLM error {response.status_code}: {error_text}")
        )
        detail = data.get("error") if isinstance(data, dict) else None
        detail = detail if isinstance(detail, dict) else {}
        # Subscription limits identify the reason in error.type, whereas the
        # public API normally uses error.code. Both pass through the closed set.
        codes = [
            value
            for value in (detail.get("code"), detail.get("type"))
            if type(value) is str and value in DIAGNOSTIC_CODES
        ]
        code = next(
            (value for value in codes if value in HARD_QUOTA_CODES), next(iter(codes), None)
        )
        retry_after, reset_at = retry_metadata(response, detail)
        set_safe_provider_error_fields(
            error,
            status=response.status_code,
            code=code,
            parameter=detail.get("param"),
            retry_after=retry_after,
            reset_at=reset_at,
        )
        if error.provider_error_code in HARD_QUOTA_CODES:
            mark_provider_call_non_retryable(error)
        # Keep the hosted gateway display metadata alongside the closed fields
        # used by protected desktop sessions.
        error.provider_status_code = response.status_code
        error.provider_error_body = sanitize_error_text_for_output(json.dumps(data))
        return error

    @scoped_input_token_counter
    def count_input_tokens(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> InputTokenCount | None:
        if (
            not self.usage_contract.supports_input_token_count
            or self._input_token_count_available is False
        ):
            return None
        messages = gate_messages_for_provider_route(messages, self.route_identity)
        tool_mapping = _responses_tools(
            tools,
            mode=self.web_search_mode,
            adapter=self.web_search_adapter,
            provider_key=self.provider_key,
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "input": _responses_input_from_messages(messages),
        }
        if tool_mapping.tools:
            payload["tools"] = tool_mapping.tools
            payload["tool_choice"] = (
                "auto"
                if tool_choice is None
                else _tool_choice_for_mapped_tools(
                    tool_choice,
                    removed_alysis_web_search=tool_mapping.removed_alysis_web_search,
                )
            )
        if self.provider_auth is not None:
            adapted = self.provider_auth.adapt_responses_payload(payload)
            payload = {
                key: adapted[key]
                for key in ("model", "input", "instructions", "tools", "tool_choice")
                if key in adapted
            }
        url = f"{self.base_url}/responses/input_tokens"

        def _send_request() -> InputTokenCount | None:
            auth_refresh_used = False
            while True:
                try:
                    with provider_httpx_client(
                        timeout=self.timeout_s, transport=self._transport
                    ) as client:
                        response = client.post(
                            url,
                            headers=self._headers(url, force_refresh=auth_refresh_used),
                            content=_json_bytes(payload),
                        )
                except httpx.HTTPError as exc:
                    raise LLMError(
                        "OpenAI input token count request failed: "
                        f"{sanitize_error_text_for_output(exc)}"
                    ) from exc
                if (
                    response.status_code == 401
                    and self.provider_auth is not None
                    and not auth_refresh_used
                ):
                    auth_refresh_used = True
                    continue
                if response.status_code in {404, 405, 501}:
                    self._input_token_count_available = False
                    return None
                if response.status_code >= 400:
                    raise self._llm_error_from_response(response)
                try:
                    data = response.json()
                except Exception as exc:  # noqa: BLE001
                    raise LLMError("OpenAI input token count returned non-JSON response") from exc
                count = _non_negative_int(
                    data.get("input_tokens") if isinstance(data, dict) else None
                )
                if count is None:
                    raise LLMError("OpenAI input token count response omitted input_tokens")
                self._input_token_count_available = True
                return InputTokenCount(
                    input_tokens=count,
                    raw_provider_usage=copy.deepcopy(data),
                )

        with input_token_count_request():
            return run_provider_limited_call(
                call=_send_request,
                provider_key=self.provider_key,
                provider_concurrency_caps=self.provider_concurrency_caps,
                retry_settings=self.provider_retry_settings,
                operation="responses_count_input_tokens",
                sleep_fn=self._provider_sleep_fn,
                random_fn=self._provider_random_fn,
                retry_deadline_allows=getattr(self, "_provider_retry_deadline_allows", None),
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
        request_plan: LLMRequestPlan | None = None,
    ) -> LLMResponse:
        default_cache = RequestCachePlan(
            strategy=(
                "openai_prompt_cache"
                if self.prompt_cache_key or self.prompt_cache_retention
                else "none"
            ),
            mode=(
                "automatic" if self.prompt_cache_key or self.prompt_cache_retention else "manual"
            ),
            prompt_cache_key=self.prompt_cache_key,
            prompt_cache_retention=self.prompt_cache_retention,
        )
        plan = request_plan or LLMRequestPlan.from_chat_args(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            cache=default_cache,
        )
        if (
            request_plan is not None
            and plan.cache.mode != "off"
            and plan.cache.strategy == "none"
            and not plan.cache.prompt_cache_key
            and not plan.cache.prompt_cache_retention
            and (self.prompt_cache_key or self.prompt_cache_retention)
        ):
            plan = plan.with_cache(default_cache)
        messages = gate_messages_for_provider_route(plan.message_list(), self.route_identity)
        tools = plan.tool_list()
        tool_choice = plan.tool_choice
        response_format = plan.response_format
        public_stream = plan.stream
        on_reasoning_delta = reasoning_trace_callback(
            on_reasoning_delta,
            capability=getattr(self, "reasoning_trace_capability", None),
            stream=public_stream,
        )
        stream = public_stream or bool(
            self.provider_auth is not None
            and getattr(self.provider_auth, "requires_streaming", False)
        )
        temperature = plan.temperature
        max_tokens = plan.max_tokens
        if max_tokens is not None:
            max_tokens = _clamp_responses_max_output_tokens(max_tokens)
        tool_mapping = _responses_tools(
            tools,
            mode=self.web_search_mode,
            adapter=self.web_search_adapter,
            provider_key=self.provider_key,
        )
        mapped_tools = tool_mapping.tools
        temp_omit_key = _responses_temperature_omit_key(self.base_url, self.model)
        include_omit_key = _responses_endpoint_model_key(self.base_url, self.model)
        reasoning_summary_support_key = self._reasoning_summary_support_key()
        reasoning = _responses_reasoning(
            enable_thinking=self.enable_thinking,
            reasoning_effort=self.reasoning_effort,
            request_summary=(
                on_reasoning_delta is not None and self._should_request_reasoning_summary()
            ),
            provider_key=self.provider_key,
            model=self.model,
        )
        documented_temperature_reason = documented_temperature_omit_reason(
            self.model,
            provider_key=self.provider_key,
            reasoning_effort=(reasoning or {}).get("effort"),
        )
        text_config = _responses_text_config(response_format)
        full_input = _responses_input_from_messages(messages)
        continuation = _responses_continuation_from_messages(messages)
        previous_response_id: str | None = None
        sent_input = full_input
        input_mode = "full"
        continuation_anchor_index: int | None = None
        stateless_responses = stateless_responses_required(self)
        supports_previous_response_id = not stateless_responses and (
            self.provider_auth is None
            or bool(getattr(self.provider_auth, "supports_previous_response_id", True))
        )
        if self.provider_key == "alysis":
            # Hosted credits meter the complete request. Shared upstream storage
            # must never be addressable by another Alysis account.
            supports_previous_response_id = False
        if continuation is not None and supports_previous_response_id:
            # With previous_response_id the API appends the sent input items to the
            # stored thread; system/developer messages from turn 1 are already
            # retained server-side, so resending them duplicates the instructions
            # on every chained turn. Send only the new suffix.
            continuation_input = _responses_input_from_messages(continuation.suffix_messages)
            if continuation_input:
                previous_response_id = continuation.previous_response_id
                sent_input = continuation_input
                input_mode = "previous_response_id"
                continuation_anchor_index = continuation.anchor_index

        active_cache_policy = plan.cache.openai_prompt_cache_policy_metadata()
        declared_cache_strategy = (
            str(self.prompt_cache_policy_metadata.get("strategy") or "").strip()
            if isinstance(self.prompt_cache_policy_metadata, dict)
            else ""
        )
        if active_cache_policy is not None and declared_cache_strategy not in {"", "none"}:
            # A key field activates routing on the declared cache (for example
            # the subscription's implicit cache); it is not a different strategy.
            active_cache_policy["strategy"] = declared_cache_strategy
        cache_policy = merge_cache_policy_metadata(
            self.prompt_cache_policy_metadata,
            active_cache_policy,
        )

        def _build_payload(
            input_items: list[dict[str, Any]],
            *,
            prior_response_id: str | None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "model": self.model,
                "input": input_items,
            }
            if stateless_responses:
                payload["store"] = False
            if prior_response_id:
                payload["previous_response_id"] = prior_response_id
            if (
                documented_temperature_reason is None
                and temp_omit_key not in _RESPONSES_OMIT_TEMPERATURE_MODELS
            ):
                payload["temperature"] = (
                    self.temperature if temperature is None else float(temperature)
                )
            if plan.cache.prompt_cache_key:
                payload["prompt_cache_key"] = plan.cache.prompt_cache_key
            if plan.cache.prompt_cache_retention:
                payload["prompt_cache_retention"] = plan.cache.prompt_cache_retention
            if reasoning is not None:
                payload["reasoning"] = copy.deepcopy(reasoning)
            if mapped_tools:
                payload["tools"] = mapped_tools
                if serial_tool_calls_required(self):
                    payload["parallel_tool_calls"] = False
                payload["tool_choice"] = (
                    "auto"
                    if tool_choice is None
                    else _tool_choice_for_mapped_tools(
                        tool_choice,
                        removed_alysis_web_search=tool_mapping.removed_alysis_web_search,
                    )
                )
                if (
                    tool_mapping.added_builtin_web_search
                    and include_omit_key not in _RESPONSES_OMIT_INCLUDE_ENDPOINTS
                ):
                    payload["include"] = ["web_search_call.action.sources"]
            elif tool_choice is not None:
                payload["tool_choice"] = _tool_choice_for_mapped_tools(
                    tool_choice,
                    removed_alysis_web_search=tool_mapping.removed_alysis_web_search,
                )
            if text_config is not None:
                payload["text"] = text_config
            if max_tokens is not None:
                payload["max_output_tokens"] = int(max_tokens)
            if stream:
                payload["stream"] = True
            evidence_ordinal = _provider_serialization_ordinal()
            if evidence_ordinal is not None:
                evidence: dict[str, object] = {
                    "phase": "serialized",
                    "attemptOrdinal": evidence_ordinal,
                }
                image_count = _count_provider_images(payload)
                if image_count is not None:
                    evidence["serializedImageCount"] = image_count
                note_computer_use_provider_evidence(evidence)
            if self.provider_auth is not None:
                payload = self.provider_auth.adapt_responses_payload(payload)
            if self.provider_key == "alysis":
                payload["store"] = False
                payload["include"] = ["reasoning.encrypted_content"]
                payload.pop("temperature", None)
            if stateless_responses:
                # Authentication adapters may rewrite the complete provider
                # dialect. Reassert the sensitive-session boundary after that
                # rewrite so no route can re-enable storage or continuation.
                # Stateless reasoning items can only be replayed when the API
                # returns their encrypted continuation payload, so this include
                # value is part of the required Computer Use protocol rather
                # than an optional response enhancement.
                payload["store"] = False
                payload.pop("previous_response_id", None)
                raw_include = payload.get("include")
                include = list(raw_include) if isinstance(raw_include, list) else []
                if "reasoning.encrypted_content" not in include:
                    include.append("reasoning.encrypted_content")
                payload["include"] = include
            if self._reasoning_summary_support_by_model.get(reasoning_summary_support_key) is False:
                _without_responses_reasoning_summary(payload)
            return payload

        def _prompt_estimation_payload(payload: dict[str, Any]) -> dict[str, Any]:
            estimation_payload: dict[str, Any] = {
                "input": payload.get("input", []),
            }
            for key in ("tools", "text", "include", "parallel_tool_calls"):
                if key in payload:
                    estimation_payload[key] = payload[key]
            return estimation_payload

        payload = _build_payload(sent_input, prior_response_id=previous_response_id)
        full_estimate_payload = _build_payload(full_input, prior_response_id=None)
        full_input_estimate_tokens = estimate_provider_payload_tokens(
            _prompt_estimation_payload(full_estimate_payload)
        )

        wire_diagnostics = self._wire_request_diagnostics.begin_request()

        def _request_plan_metadata(current_payload: dict[str, Any]) -> dict[str, Any]:
            extra: dict[str, Any] = {
                "full_input_item_count": len(full_input),
                "sent_input_item_count": len(sent_input),
                "previous_response_id_used": previous_response_id is not None,
                # Continuation never resends stable instructions; kept at 0 so the
                # telemetry schema stays stable for downstream consumers.
                "resent_stable_instruction_count": 0,
            }
            if continuation_anchor_index is not None:
                extra["continuation_anchor_index"] = continuation_anchor_index
            metadata = plan.request_plan_metadata(
                input_mode=input_mode,
                continuation_strategy=(
                    "previous_response_id" if previous_response_id else "full_replay"
                ),
                provider_payload=_prompt_estimation_payload(full_estimate_payload),
                sent_provider_payload=_prompt_estimation_payload(current_payload),
                cache_policy_metadata=cache_policy,
                extra=extra,
            )
            metadata.update(
                wire_diagnostics(
                    current_payload, history_key="input", instructions_key="instructions"
                )
            )
            metadata["request_messages_signature"] = _stable_request_signature(messages)
            return metadata

        request_plan_metadata = _request_plan_metadata(payload)

        def _token_reconciliation_metadata(
            current_payload: dict[str, Any],
        ) -> dict[str, Any]:
            sent_input_estimate_tokens = estimate_provider_payload_tokens(
                _prompt_estimation_payload(current_payload)
            )
            return {
                "input_estimate_tokens": full_input_estimate_tokens,
                "sent_input_estimate_tokens": sent_input_estimate_tokens,
                "estimator": "cl100k_base",
                "estimate_basis": "provider_prompt_payload",
                "input_mode": str(request_plan_metadata.get("input_mode") or input_mode),
            }

        def _request_shape_metadata(current_payload: dict[str, Any]) -> dict[str, Any]:
            return build_request_shape_report(
                messages=messages,
                tools=tools,
                cache_policy=cache_policy,
                provider_payload=_prompt_estimation_payload(current_payload),
                input_mode=str(request_plan_metadata.get("input_mode") or input_mode),
            )

        provider_key = self.provider_key or best_effort_provider_key(
            base_url=self.base_url,
            model=self.model,
        )
        telemetry = ProviderCallTelemetryRecorder(
            provider_key=provider_key,
            protocol="openai_responses",
            model=self.model,
            base_url=self.base_url,
            stream=stream,
            tools=tools,
            web_search_mode=self.web_search_mode,
            web_search_adapter=self.web_search_adapter,
            native_web_search=tool_mapping.added_builtin_web_search,
            cache_policy=cache_policy,
            request_plan=request_plan_metadata,
            request_shape=_request_shape_metadata(payload),
            token_reconciliation=_token_reconciliation_metadata(payload),
            operation="responses_chat",
        )
        telemetry_on_text_delta = telemetry.wrap_text_delta(on_text_delta)
        telemetry_on_reasoning_delta = telemetry.wrap_reasoning_delta(on_reasoning_delta)
        public_output_emitted = False

        def _tracked_text_delta(delta: str) -> None:
            nonlocal public_output_emitted
            if delta:
                public_output_emitted = True
            if telemetry_on_text_delta is not None:
                telemetry_on_text_delta(delta)

        def _tracked_reasoning_delta(delta: str) -> None:
            nonlocal public_output_emitted
            if delta:
                public_output_emitted = True
            if telemetry_on_reasoning_delta is not None:
                telemetry_on_reasoning_delta(delta)

        def _finalize_response(response: LLMResponse) -> LLMResponse:
            raw_reasoning = payload.get("reasoning")
            if isinstance(raw_reasoning, dict) and raw_reasoning.get("summary") == "auto":
                self._reasoning_summary_support_by_model[reasoning_summary_support_key] = True
            if not public_stream:
                if on_reasoning_delta is not None and telemetry_on_reasoning_delta is not None:
                    for summary in response.reasoning:
                        if summary.kind == ReasoningOutputKind.SUMMARY:
                            telemetry_on_reasoning_delta(summary.text)
                if (
                    on_text_delta is not None
                    and telemetry_on_text_delta is not None
                    and response.content
                ):
                    telemetry_on_text_delta(response.content)
            telemetry.set_request_plan(request_plan_metadata)
            telemetry.set_request_shape(_request_shape_metadata(payload))
            telemetry.set_token_reconciliation(_token_reconciliation_metadata(payload))
            return _response_with_request_plan_metadata(
                response,
                request_plan_metadata,
            )

        def _transport_payload_bytes() -> bytes:
            # This is the body actually passed to HTTPX, after authentication
            # adaptation and every retry edit. Preparation is not transport entry.
            body = _json_bytes(payload)
            ordinal = _provider_attempt_ordinal(prepare=True)
            if ordinal is not None:
                evidence: dict[str, object] = {
                    "phase": "payload_prepared_at_transport",
                    "attemptOrdinal": ordinal,
                }
                count = _count_provider_images(payload)
                if count is not None:
                    evidence["adaptedImageCount"] = count
                if self.provider_auth is None:
                    evidence["adapter"] = "none"
                else:
                    from ..provider_auth.openai_codex import OpenAICodexSubscriptionAuth

                    evidence["adapter"] = (
                        "openai_codex_subscription"
                        if isinstance(self.provider_auth, OpenAICodexSubscriptionAuth)
                        else "other"
                    )
                note_computer_use_provider_evidence(evidence)
            return body

        def _send_request() -> LLMResponse:
            nonlocal input_mode, payload, previous_response_id, reasoning
            nonlocal request_plan_metadata, sent_input
            url = f"{self.base_url}/responses"
            previous_response_fallback_used = False
            reasoning_summary_fallback_used = False
            auth_refresh_used = False

            def _refresh_request_metadata() -> None:
                nonlocal request_plan_metadata
                request_plan_metadata = _request_plan_metadata(payload)
                telemetry.set_request_plan(request_plan_metadata)
                telemetry.set_request_shape(_request_shape_metadata(payload))
                telemetry.set_token_reconciliation(_token_reconciliation_metadata(payload))

            def _retry_without_include(err: Exception) -> bool:
                nonlocal payload
                if "include" not in payload:
                    return False
                if stateless_responses:
                    # ``reasoning.encrypted_content`` is required to continue a
                    # manually replayed, store=false reasoning session. Failing
                    # closed is safer than retrying a request whose next turn
                    # cannot satisfy the provider's pairing contract.
                    return False
                if not _responses_include_unsupported(err):
                    return False
                # Bounded: the retried payload has no ``include`` entries, so
                # this branch cannot fire twice for the same request.
                _RESPONSES_OMIT_INCLUDE_ENDPOINTS.add(include_omit_key)
                payload = dict(payload)
                payload.pop("include", None)
                _refresh_request_metadata()
                return True

            def _retry_without_reasoning_summary(err: Exception) -> bool:
                nonlocal payload, reasoning, reasoning_summary_fallback_used
                if reasoning_summary_fallback_used:
                    return False
                if not _responses_reasoning_summary_unsupported(err):
                    return False
                if (
                    not isinstance(payload.get("reasoning"), dict)
                    or "summary" not in payload["reasoning"]
                ):
                    return False
                reasoning_summary_fallback_used = True
                if (
                    self._reasoning_summary_support_by_model.get(reasoning_summary_support_key)
                    is not True
                ):
                    self._reasoning_summary_support_by_model[reasoning_summary_support_key] = False
                if isinstance(reasoning, dict):
                    reasoning = copy.deepcopy(reasoning)
                    reasoning.pop("summary", None)
                    reasoning = reasoning or None
                payload = _build_payload(sent_input, prior_response_id=previous_response_id)
                _without_responses_reasoning_summary(payload)
                _refresh_request_metadata()
                return True

            while True:
                try:
                    client_context = (
                        cancellation_aware_httpx_client(
                            timeout_s=self.timeout_s,
                            transport=self._transport,
                            cancellation_token=cancellation_token,
                        )
                        if cancellation_token is not None and not public_stream
                        else provider_httpx_client(
                            timeout=self.timeout_s, transport=self._transport
                        )
                    )
                    with client_context as client:
                        if stream:
                            with cancellable_httpx_request(
                                client=client,
                                cancellation_token=cancellation_token,
                                method="POST",
                                url=url,
                                stream=True,
                                headers=self._headers(
                                    url,
                                    force_refresh=auth_refresh_used,
                                    cancellation_token=cancellation_token,
                                ),
                                content=_transport_payload_bytes(),
                            ) as response:
                                if response.status_code >= 400:
                                    response.read()
                                    if (
                                        response.status_code == 401
                                        and self.provider_auth is not None
                                        and not auth_refresh_used
                                    ):
                                        auth_refresh_used = True
                                        continue
                                    err = self._llm_error_from_response(response)
                                    if (
                                        previous_response_id
                                        and not previous_response_fallback_used
                                        and _responses_previous_response_rejected(err)
                                    ):
                                        previous_response_fallback_used = True
                                        previous_response_id = None
                                        sent_input = full_input
                                        input_mode = (
                                            "full_retry_after_previous_response_id_rejected"
                                        )
                                        payload = _build_payload(
                                            sent_input,
                                            prior_response_id=None,
                                        )
                                        _refresh_request_metadata()
                                        continue
                                    if _retry_without_reasoning_summary(err):
                                        continue
                                    if _retry_without_include(err):
                                        continue
                                    if (
                                        "temperature" in payload
                                        and _responses_temperature_unsupported(err)
                                    ):
                                        payload.pop("temperature", None)
                                        _RESPONSES_OMIT_TEMPERATURE_MODELS.add(temp_omit_key)
                                        continue
                                    raise err
                                return _finalize_response(
                                    self._parse_stream_response(
                                        response,
                                        on_text_delta=(
                                            _tracked_text_delta
                                            if public_stream and on_text_delta is not None
                                            else None
                                        ),
                                        on_reasoning_delta=(
                                            _tracked_reasoning_delta
                                            if public_stream and on_reasoning_delta is not None
                                            else None
                                        ),
                                        cancellation_token=cancellation_token,
                                    )
                                )
                        response = cancellable_httpx_send(
                            client=client,
                            cancellation_token=cancellation_token,
                            method="POST",
                            url=url,
                            headers=self._headers(
                                url,
                                force_refresh=auth_refresh_used,
                                cancellation_token=cancellation_token,
                            ),
                            content=_transport_payload_bytes(),
                        )
                except CooperativeCancellationError:
                    raise
                except httpx.DecodingError as e:
                    raise_if_cancelled(cancellation_token)
                    err = LLMError(
                        "OpenAI Responses decompression failed: "
                        f"{sanitize_error_text_for_output(e)}"
                    )
                    if stream and public_output_emitted:
                        mark_provider_call_non_retryable(err)
                    raise err from e
                except DeadlineExhausted:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise_if_cancelled(cancellation_token)
                    if isinstance(e, LLMError):
                        if stream and public_output_emitted:
                            mark_provider_call_non_retryable(e)
                        raise
                    err = LLMError(
                        f"OpenAI Responses request failed: {sanitize_error_text_for_output(e)}"
                    )
                    if stream and public_output_emitted:
                        mark_provider_call_non_retryable(err)
                    raise err from e
                if response.status_code >= 400:
                    if (
                        response.status_code == 401
                        and self.provider_auth is not None
                        and not auth_refresh_used
                    ):
                        auth_refresh_used = True
                        continue
                    err = self._llm_error_from_response(response)
                    if (
                        previous_response_id
                        and not previous_response_fallback_used
                        and _responses_previous_response_rejected(err)
                    ):
                        previous_response_fallback_used = True
                        previous_response_id = None
                        sent_input = full_input
                        input_mode = "full_retry_after_previous_response_id_rejected"
                        payload = _build_payload(sent_input, prior_response_id=None)
                        _refresh_request_metadata()
                        continue
                    if _retry_without_reasoning_summary(err):
                        continue
                    if _retry_without_include(err):
                        continue
                    # The model rejected ``temperature``: drop it (and remember
                    # that for this model) and retry once. Bounded — the second
                    # attempt has no ``temperature`` so it can't loop here.
                    if "temperature" in payload and _responses_temperature_unsupported(err):
                        payload.pop("temperature", None)
                        _RESPONSES_OMIT_TEMPERATURE_MODELS.add(temp_omit_key)
                        continue
                    raise err
                return _finalize_response(self._parse_chat_response(response))

        result = stamp_response_for_route(
            telemetry.run(
                lambda: run_provider_limited_call(
                    call=_send_request,
                    provider_key=provider_key,
                    provider_concurrency_caps=self.provider_concurrency_caps,
                    retry_settings=self.provider_retry_settings,
                    operation="responses_chat",
                    sleep_fn=self._provider_sleep_fn,
                    random_fn=self._provider_random_fn,
                    on_retry=telemetry.on_retry,
                    on_retry_event=getattr(
                        self,
                        "_provider_retry_event_observer",
                        None,
                    ),
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
        if _response_hit_output_limit(result.raw):
            # Raise only after normalization, accounting and route stamping, and
            # outside transport retries. Recovery belongs to the agent turn.
            raise LLMOutputLimitError(result)
        return result

    def _parse_stream_response(
        self,
        response: httpx.Response,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse:
        record_stream_diagnostic(response)
        accumulator = _OpenAIResponsesStreamAccumulator(
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )
        progress_clock = self._stream_progress_clock
        last_meaningful_progress = progress_clock()

        def _observed_byte_chunks() -> Iterator[bytes]:
            for chunk in response.iter_bytes():
                raise_if_cancelled(cancellation_token)
                stream_deadline_exhausted = getattr(
                    self,
                    "_stream_deadline_exhausted",
                    None,
                )
                if callable(stream_deadline_exhausted) and stream_deadline_exhausted():
                    raise DeadlineExhausted("run deadline and in-flight provider grace elapsed")
                progress_now = progress_clock()
                if progress_now - last_meaningful_progress >= self.stream_no_progress_timeout_s:
                    raise LLMStreamNoProgressError(
                        "LLM stream produced no meaningful payload within "
                        f"{self.stream_no_progress_timeout_s:g}s."
                    )
                yield chunk

        text_chunks = resolved_entry.decode_chunks(
            _observed_byte_chunks(),
            encoding=response.encoding or "utf-8",
        )
        for frame in iter_sse_frames(iter_lines_from_text_chunks(text_chunks)):
            raw_event = (
                resolved_entry.provider_loads(frame.data)
                if resolved_entry.active()
                else parse_sse_json_frame(frame, stream_name="OpenAI Responses stream")
            )
            if not isinstance(raw_event, dict):
                raise LLMError("OpenAI Responses stream emitted non-object JSON event")
            progress_revision = accumulator.progress_revision
            if str(raw_event.get("type") or frame.event or "") in {
                "error",
                "response.failed",
                "response.incomplete",
            }:
                record_stream_diagnostic(response, raw_event, event_count=accumulator.event_count)
            accumulator.handle(frame, raw_event)
            if resolved_entry.active():
                for item in accumulator.output_items.values():
                    resolved_entry.bound_stream(item.get("arguments"), item.get("name", ""))
            if accumulator.progress_revision != progress_revision:
                last_meaningful_progress = progress_clock()
        data = accumulator.finish()
        if resolved_entry.active():
            for index, item in enumerate(data.get("output", [])):
                if isinstance(item, dict) and item.get("name") == resolved_entry.NAME:
                    resolved_entry.bound_stream(item.get("arguments"))
                    if index in accumulator.argument_chunks and item.get("arguments") != "".join(
                        accumulator.argument_chunks[index]
                    ):
                        resolved_entry.invalidate_parent()
        return self._parse_chat_response(_response_from_json(data))

    @staticmethod
    def _parse_chat_response(response: httpx.Response) -> LLMResponse:
        try:
            data = (
                resolved_entry.provider_loads(response.content)
                if resolved_entry.active()
                else response.json()
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError("OpenAI Responses returned non-JSON response") from e
        if not isinstance(data, dict):
            raise LLMError("Unexpected OpenAI Responses payload: expected JSON object")

        output_limit_reached = _response_hit_output_limit(data)
        if data.get("status") in {"failed", "incomplete"} and not output_limit_reached:
            raise LLMError(f"OpenAI Responses: {_stream_error_message({'response': data})}")
        content = _extract_answer_text(data)
        tool_calls = [] if output_limit_reached else _parse_response_tool_calls(data)
        reasoning = _responses_reasoning_outputs(data)
        if not content and not tool_calls:
            refusal = _extract_refusal(data)
            if refusal:
                raise LLMError(f"OpenAI Responses refusal: {refusal}")
            if not _has_responses_reasoning_output(data):
                status = str(data.get("status") or "").strip()
                # A completed response with no output is a valid provider result,
                # even though it gives the agent nothing to act on. Preserve that
                # structure as an empty LLMResponse so the shared turn-level
                # recovery and stall policy can decide what happens next. A
                # token-limited response also needs normalization before chat()
                # raises its recoverable error with usage and continuation state.
                if status.casefold() != "completed" and not output_limit_reached:
                    suffix = f" (status={status})" if status else ""
                    raise LLMError(
                        f"OpenAI Responses returned no assistant text or tool calls{suffix}"
                    )

        response_model = data.get("model") if isinstance(data.get("model"), str) else None
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            raw=data,
            response_model=response_model,
            usage=_parse_usage(data.get("usage")),
            provider_metadata=_responses_provider_metadata(data),
            reasoning=reasoning,
            assistant_phase=_assistant_response_phase(data),
        )

    def web_search(
        self,
        *,
        query: str,
        allowed_domains: list[str] | None = None,
        external_web_access: bool | None = None,
        include_source_details: bool = True,
        tool_choice: str | dict[str, Any] | None = "required",
    ) -> WebSearchResponse:
        url = f"{self.base_url}/responses"

        tool_spec: dict[str, Any] = {"type": "web_search"}
        if allowed_domains:
            tool_spec["filters"] = {"allowed_domains": list(allowed_domains)}
        if external_web_access is not None:
            tool_spec["external_web_access"] = bool(external_web_access)

        include_omit_key = _responses_endpoint_model_key(self.base_url, self.model)
        include_omitted = include_omit_key in _RESPONSES_OMIT_INCLUDE_ENDPOINTS

        payload: dict[str, Any] = {
            "model": self.model,
            "input": query,
            "tools": [tool_spec],
        }
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if include_source_details and not include_omitted:
            payload["include"] = ["web_search_call.action.sources"]
        if self.provider_auth is not None:
            payload = self.provider_auth.adapt_responses_payload(payload)

        provider_key = self.provider_key or best_effort_provider_key(
            base_url=self.base_url,
            model=self.model,
        )

        def _perform_request(request_payload: dict[str, Any]) -> httpx.Response:
            def _send_request() -> httpx.Response:
                auth_refresh_used = False
                try:
                    while True:
                        with provider_httpx_client(
                            timeout=self.timeout_s, transport=self._transport
                        ) as client:
                            response = client.post(
                                url,
                                headers=self._headers(url, force_refresh=auth_refresh_used),
                                content=_json_bytes(request_payload),
                            )
                        if (
                            response.status_code == 401
                            and self.provider_auth is not None
                            and not auth_refresh_used
                        ):
                            auth_refresh_used = True
                            continue
                        break
                except httpx.DecodingError as e:
                    raise ResponsesError(
                        "Responses response decompression failed: "
                        f"{sanitize_error_text_for_output(e)}"
                    ) from e
                except Exception as e:  # noqa: BLE001
                    raise ResponsesError(
                        f"Responses request failed: {sanitize_error_text_for_output(e)}"
                    ) from e
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                return response

            return run_provider_limited_call(
                call=_send_request,
                provider_key=provider_key,
                provider_concurrency_caps=self.provider_concurrency_caps,
                retry_settings=self.provider_retry_settings,
                operation="responses_web_search",
                sleep_fn=self._provider_sleep_fn,
                random_fn=self._provider_random_fn,
                retry_deadline_allows=getattr(self, "_provider_retry_deadline_allows", None),
            )

        try:
            response = _perform_request(payload)
        except ResponsesError as err:
            if "include" not in payload or not _responses_include_unsupported(err):
                raise
            # The gateway rejected the optional ``include`` entries. Source
            # metadata is an enhancement, not a requirement: retry once without
            # it and remember the capability so later calls skip the doomed
            # variant. Bounded — the retried payload has no ``include``.
            _RESPONSES_OMIT_INCLUDE_ENDPOINTS.add(include_omit_key)
            include_omitted = True
            payload = {key: value for key, value in payload.items() if key != "include"}
            response = _perform_request(payload)

        try:
            data = response.json()
        except Exception as e:  # noqa: BLE001
            raise ResponsesError("Responses API returned non-JSON response") from e

        if not isinstance(data, dict):
            raise ResponsesError("Unexpected Responses API payload: expected JSON object")

        if response.status_code >= 400:
            error_message = _extract_error_message(data)
            if error_message:
                lower = error_message.lower()
                if "unsupported" in lower or "not support" in lower:
                    raise ResponsesError(f"Responses web_search unsupported: {error_message}")
                raise ResponsesError(f"Responses error {response.status_code}: {error_message}")
            raise ResponsesError(f"Responses error {response.status_code}: {data!r}")

        answer = _extract_answer_text(data)
        citations = _extract_citations(data)
        sources, queries = _extract_sources_and_queries(data)
        sources = _merge_citation_sources(sources, citations)
        if not sources:
            # Absent source metadata is tolerated only when it was not
            # requested (the gateway rejected the optional include) AND the
            # response still shows a real search happened. A response with no
            # web_search_call at all never searched — treat it as a failure so
            # callers (e.g. auto-mode fallback) can engage a working backend
            # instead of accepting an unsourced answer.
            if not (include_omitted and _has_web_search_call_output(data)):
                raise ResponsesError("Responses web_search did not return sources")

        response_id = str(data.get("id") or "").strip() or None
        response_model = str(data.get("model") or "").strip() or None
        return WebSearchResponse(
            answer=answer,
            citations=citations,
            sources=sources,
            queries=queries,
            raw=data,
            response_id=response_id,
            model=response_model,
        )


resolved_entry._register_client(
    OpenAIResponsesClient, ("chat", "_parse_chat_response", "_parse_stream_response")
)
