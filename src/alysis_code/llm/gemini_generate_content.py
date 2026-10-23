from __future__ import annotations

import copy
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

import httpx

from ..cancellation import CooperativeCancellationError
from ..error_text import sanitize_error_text_for_output
from ..provider_telemetry import ProviderCallTelemetryRecorder
from ..reasoning_contracts import WIRE_THINKING_LEVEL, reasoning_contract_for
from ..request_estimation import estimate_provider_payload_tokens
from ..token_budget import estimate_tokens
from ..web_search_adapters import AUTO_WEB_SEARCH_ADAPTER, GEMINI_GROUNDING_ADAPTER
from .cache_policy import merge_cache_policy_metadata
from .cancellable_http import cancellation_aware_httpx_client, provider_httpx_client
from .http_cancellation import (
    cancellable_httpx_request,
    cancellable_httpx_send,
    raise_if_cancelled,
)
from .metadata import (
    GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
    PROVIDER_METADATA_KEY,
    TOOL_CALL_PROVIDER_METADATA_KEY,
    ProviderRouteIdentity,
    build_provider_route_identity,
    canonicalize_extra_headers,
    credential_scope_fingerprint,
    gate_messages_for_provider_route,
    merge_canonical_headers,
    stamp_response_for_route,
)
from .protocols import reasoning_trace_callback
from .provider_limits import (
    DEFAULT_PROVIDER_CONCURRENCY_CAPS,
    ProviderRetrySettings,
    best_effort_provider_key,
    mark_provider_call_non_retryable,
    run_provider_limited_call,
)
from .request_plan import LLMRequestPlan, RequestCachePlan
from .request_shape import build_request_shape_report
from .streaming import SSEFrame, iter_sse_frames, parse_sse_json_frame
from .temperature_compat import documented_temperature_omit_reason
from .types import (
    InputTokenCount,
    LLMError,
    LLMResponse,
    LLMUsage,
    ReasoningOutput,
    ReasoningOutputKind,
    ToolCall,
    UsageConfidence,
    UsageContract,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_ACCEPT_ENCODING = "identity"
_GEMINI_METADATA_KEY = GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY
_ALYSIS_WEB_SEARCH_FUNCTION_NAME = "web_search"
_WEB_SEARCH_MODES_ALLOWING_GEMINI_GROUNDING = frozenset({"auto", "native"})
_INCLUDE_SERVER_SIDE_TOOL_INVOCATIONS = "includeServerSideToolInvocations"
_TOOL_CALL_PROVIDER_METADATA_KEY = TOOL_CALL_PROVIDER_METADATA_KEY
_DUMMY_IMPORTED_FUNCTION_CALL_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
_GEMINI_EXPLICIT_CACHE_MIN_TOKENS = 1024
_GEMINI_EXPLICIT_CACHE_MAX_ENTRIES = 8
_GEMINI_EXPLICIT_CACHE_REFRESH_FRACTION = 0.10
_GEMINI_EXPLICIT_CACHE_REFRESH_MAX_SECONDS = 60.0
_GEMINI_EXPLICIT_CACHE_REFRESH_MIN_SECONDS = 1.0
_GEMINI_EXPLICIT_CACHE_TRANSIENT_CREATE_FAILURE_LIMIT = 3
_GEMINI_EXPLICIT_CACHE_EVICT_ALL_TIMEOUT_S = 5.0


def _non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_cached_content_min_tokens(value: int | None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = _GEMINI_EXPLICIT_CACHE_MIN_TOKENS
    return max(0, number)


def _normalize_cached_content_max_entries(value: int | None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = _GEMINI_EXPLICIT_CACHE_MAX_ENTRIES
    return max(1, number)


def _parse_cached_content_ttl_seconds(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    multiplier = 1.0
    if text[-1:] in {"s", "m", "h"}:
        unit = text[-1]
        text = text[:-1].strip()
        multiplier = {"s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    try:
        seconds = float(text) * multiplier
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


def _cached_content_refresh_margin_seconds(ttl_seconds: float | None) -> float | None:
    if ttl_seconds is None:
        return None
    margin = min(
        _GEMINI_EXPLICIT_CACHE_REFRESH_MAX_SECONDS,
        max(
            _GEMINI_EXPLICIT_CACHE_REFRESH_MIN_SECONDS,
            ttl_seconds * _GEMINI_EXPLICIT_CACHE_REFRESH_FRACTION,
        ),
    )
    if margin >= ttl_seconds:
        margin = ttl_seconds / 2.0
    return max(0.0, margin)


def _cached_content_resource_url(base_url: str, name: str) -> str:
    resource = str(name or "").strip()
    if resource.startswith(("http://", "https://")):
        return resource
    if "/" not in resource:
        resource = f"cachedContents/{quote(resource, safe='')}"
    return f"{base_url.rstrip('/')}/{resource.lstrip('/')}"


def _rounded_non_negative_seconds(value: float | None) -> int | None:
    if value is None:
        return None
    return max(0, int(round(value)))


def _cached_content_create_usage_tokens(raw: Any) -> int | None:
    if not isinstance(raw, dict):
        return None
    try:
        tokens = int(raw.get("totalTokenCount"))
    except (TypeError, ValueError):
        return None
    return tokens if tokens >= 0 else None


def _trimmed_create_error_detail(detail: str, *, limit: int = 300) -> str:
    text = " ".join(str(detail or "").split())
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def _cached_content_create_failure_is_transient(status_code: int | None) -> bool:
    # Discriminate by HTTP semantics at runtime (never per-provider tables):
    # transport failures, timeouts, throttling, and server errors may succeed
    # on a later attempt, while other 4xx rejections are deterministic for
    # this client configuration.
    if status_code is None:
        return True
    return status_code in {408, 429} or status_code >= 500


@dataclass(frozen=True)
class _GeminiCachedContentPlan:
    signature: str
    create_payload: dict[str, Any]
    suffix_contents: list[dict[str, Any]]
    estimated_tokens: int


@dataclass(frozen=True)
class _GeminiCachedContentEntry:
    name: str
    signature: str
    created_at: float
    last_used_at: float
    ttl_seconds: float | None = None
    refresh_after: float | None = None
    expires_at: float | None = None
    estimated_tokens: int = 0
    # Creation spend already billed by the provider but not yet attached to a
    # successful response's usage; cleared once reported so retries that reuse
    # the entry report it exactly once.
    pending_creation_tokens: int | None = None


def _headers_with_default_accept_encoding(headers: dict[str, str]) -> dict[str, str]:
    request_headers = dict(headers)
    if not any(key.lower() == "accept-encoding" for key in request_headers):
        request_headers["accept-encoding"] = _DEFAULT_ACCEPT_ENCODING
    return request_headers


def _gemini_native_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/openai"):
        return normalized.removesuffix("/openai")
    return normalized


def _gemini_model_resource_name(model: str) -> str:
    normalized = str(model or "").strip()
    if normalized.startswith("models/"):
        return normalized
    return f"models/{normalized}"


def _gemini_cached_content_rejection_reason(response: httpx.Response) -> str | None:
    if response.status_code not in {400, 403, 404, 410}:
        return None
    try:
        data = response.json()
    except Exception:
        data = response.text
    rendered = json.dumps(data, ensure_ascii=False, sort_keys=True).casefold()
    if not any(
        marker in rendered for marker in ("cachedcontent", "cached content", "cached_content")
    ):
        return None
    if any(
        marker in rendered
        for marker in (
            "not found",
            "not_found",
            "not exist",
            "does not exist",
            "expired",
            "deleted",
            "invalid",
            "permission",
            "denied",
            "gone",
        )
    ):
        return "stale_cached_content"
    return None


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _response_with_cache_metadata(
    response: LLMResponse,
    cache_metadata: dict[str, Any] | None,
    request_plan_metadata: dict[str, Any] | None = None,
    cache_creation_input_tokens: int | None = None,
) -> LLMResponse:
    if not cache_metadata and not request_plan_metadata and cache_creation_input_tokens is None:
        return response
    provider_metadata = copy.deepcopy(response.provider_metadata) or {}
    gemini_metadata = provider_metadata.setdefault(_GEMINI_METADATA_KEY, {})
    if isinstance(gemini_metadata, dict):
        if cache_metadata:
            gemini_metadata["cache_policy"] = copy.deepcopy(cache_metadata)
        if request_plan_metadata:
            gemini_metadata["request_plan"] = copy.deepcopy(request_plan_metadata)
    usage = response.usage
    if cache_creation_input_tokens is not None:
        if usage is None:
            usage = LLMUsage(
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                cache_creation_input_tokens=cache_creation_input_tokens,
            )
        else:
            usage = replace(
                usage,
                cache_creation_input_tokens=(
                    (usage.cache_creation_input_tokens or 0) + cache_creation_input_tokens
                ),
            )
    return LLMResponse(
        content=response.content,
        tool_calls=response.tool_calls,
        raw=response.raw,
        response_model=response.response_model,
        usage=usage,
        provider_metadata=provider_metadata,
        reasoning=response.reasoning,
    )


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
        return text if isinstance(text, str) else json.dumps(raw, ensure_ascii=False)
    return str(raw)


def _json_arguments(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return dict(args)
    if args is None:
        return {}
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {"_raw_arguments": args}
        if isinstance(parsed, dict):
            return parsed
        return {"_raw_arguments": args}
    return {"_raw_arguments": json.dumps(args, ensure_ascii=False)}


def _json_response_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"result": value}
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}
    return {"result": value}


def _gemini_parts_from_content(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [{"text": raw}] if raw else []
    if not isinstance(raw, list):
        text = _content_to_text(raw)
        return [{"text": text}] if text else []

    parts: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            if item:
                parts.append({"text": item})
            continue
        if not isinstance(item, dict):
            continue
        part_type = str(item.get("type") or "").strip()
        text = item.get("text") or item.get("content")
        if part_type in {"text", "input_text", "output_text"} and isinstance(text, str):
            parts.append({"text": text})
            continue
        if part_type == "image_url":
            image_url = item.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "").strip()
            elif isinstance(image_url, str):
                url = image_url.strip()
            if url:
                parts.append({"fileData": {"fileUri": url}})
            continue
        if any(
            key in item for key in ("functionCall", "functionResponse", "inlineData", "fileData")
        ):
            parts.append(copy.deepcopy(item))
    return parts


def _metadata_content(message: dict[str, Any]) -> dict[str, Any] | None:
    metadata = message.get(PROVIDER_METADATA_KEY)
    if not isinstance(metadata, dict):
        return None
    gemini_metadata = metadata.get(_GEMINI_METADATA_KEY)
    if not isinstance(gemini_metadata, dict):
        return None
    content = gemini_metadata.get("content")
    if isinstance(content, dict):
        return _normalize_gemini_content_wire_keys(content)
    return None


def _normalize_gemini_content_wire_keys(content: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(content)
    parts = copied.get("parts")
    if not isinstance(parts, list):
        return copied
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "thought_signature" in part:
            if "thoughtSignature" not in part:
                part["thoughtSignature"] = copy.deepcopy(part["thought_signature"])
            part.pop("thought_signature", None)
    return copied


def _function_name_from_tool(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    if str(tool.get("type") or "") == "function":
        return str(tool.get("name") or "").strip()
    return ""


def _is_alysis_web_search_function(tool: dict[str, Any]) -> bool:
    return _function_name_from_tool(tool) == _ALYSIS_WEB_SEARCH_FUNCTION_NAME


def _is_gemini_grounding_tool(tool: dict[str, Any]) -> bool:
    return isinstance(tool.get("google_search"), dict) or isinstance(tool.get("googleSearch"), dict)


def _gemini_grounding_allowed(*, mode: str, adapter: str) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    normalized_adapter = str(adapter or "").strip().lower() or AUTO_WEB_SEARCH_ADAPTER
    if normalized_mode not in _WEB_SEARCH_MODES_ALLOWING_GEMINI_GROUNDING:
        return False
    return normalized_adapter in {AUTO_WEB_SEARCH_ADAPTER, GEMINI_GROUNDING_ADAPTER}


@dataclass(frozen=True)
class _GeminiToolMapping:
    tools: list[dict[str, Any]]
    added_google_search: bool
    removed_alysis_web_search: bool
    include_server_side_tool_invocations: bool


# Gemini's ``Schema`` message is an OpenAPI 3.0 subset, not JSON Schema. Any
# other keyword is a hard 400 ("Unknown name ... Cannot find field"), and the
# tool registry, MCP servers and custom tools all emit plain JSON Schema
# (``additionalProperties``, ``uniqueItems``, ``$ref`` ...). Everything not in
# this set is dropped or rewritten before the declaration leaves the client.
_GEMINI_SCHEMA_FIELDS: frozenset[str] = frozenset(
    {
        "type",
        "format",
        "title",
        "description",
        "nullable",
        "enum",
        "maxItems",
        "minItems",
        "properties",
        "required",
        "minProperties",
        "maxProperties",
        "minLength",
        "maxLength",
        "pattern",
        "example",
        "anyOf",
        "propertyOrdering",
        "default",
        "items",
        "minimum",
        "maximum",
    }
)
# ``format`` is validated per type; unknown values are rejected outright.
_GEMINI_SCHEMA_FORMATS: dict[str, frozenset[str]] = {
    "string": frozenset({"date-time", "enum"}),
    "number": frozenset({"float", "double"}),
    "integer": frozenset({"int32", "int64"}),
}
_GEMINI_SCHEMA_TYPES: frozenset[str] = frozenset(
    {"string", "number", "integer", "boolean", "array", "object", "null"}
)
_GEMINI_SCHEMA_MAX_DEPTH = 32


def _resolve_local_schema_ref(ref: str, root: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve ``#/$defs/x`` / ``#/definitions/x`` style pointers inside ``root``."""
    if not ref.startswith("#/"):
        return None
    node: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _gemini_schema(
    schema: Any,
    *,
    root: dict[str, Any] | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    """Project an arbitrary JSON Schema onto Gemini's ``Schema`` subset.

    Unknown keywords are dropped; ``const`` becomes a one-value ``enum``;
    ``oneOf`` becomes ``anyOf``; a single-branch ``allOf`` is merged; union
    ``type`` lists become ``nullable`` + ``anyOf``; local ``$ref`` pointers are
    inlined; ``required`` is trimmed to declared properties; ``format`` values
    Gemini does not validate are removed.
    """
    if not isinstance(schema, dict):
        return {}
    if depth > _GEMINI_SCHEMA_MAX_DEPTH:
        return {}
    root_schema = root if root is not None else schema

    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = _resolve_local_schema_ref(ref, root_schema)
        merged: dict[str, Any] = dict(target or {})
        merged.update({key: value for key, value in schema.items() if key != "$ref"})
        if target is None and len(merged) == 0:
            return {}
        return _gemini_schema(merged, root=root_schema, depth=depth + 1)

    working: dict[str, Any] = dict(schema)
    all_of = working.pop("allOf", None)
    if isinstance(all_of, list):
        branches = [branch for branch in all_of if isinstance(branch, dict)]
        if len(branches) == 1:
            merged_all = dict(branches[0])
            merged_all.update(working)
            working = merged_all
    one_of = working.pop("oneOf", None)
    if isinstance(one_of, list) and "anyOf" not in working:
        working["anyOf"] = one_of
    if "const" in working and "enum" not in working:
        working["enum"] = [working["const"]]
    working.pop("const", None)

    out: dict[str, Any] = {}
    raw_type = working.get("type")
    nullable = bool(working.get("nullable"))
    resolved_type: str | None = None
    if isinstance(raw_type, list):
        candidates = [str(item).strip().lower() for item in raw_type if isinstance(item, str)]
        if "null" in candidates:
            nullable = True
        non_null = [item for item in candidates if item != "null" and item in _GEMINI_SCHEMA_TYPES]
        if len(non_null) == 1:
            resolved_type = non_null[0]
        elif len(non_null) > 1 and "anyOf" not in working:
            working["anyOf"] = [{"type": item} for item in non_null]
    elif isinstance(raw_type, str):
        candidate = raw_type.strip().lower()
        if candidate == "null":
            nullable = True
        elif candidate in _GEMINI_SCHEMA_TYPES:
            resolved_type = candidate
    if resolved_type is not None:
        out["type"] = resolved_type
    if nullable:
        out["nullable"] = True

    for key in ("title", "description", "pattern"):
        value = working.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    for key in (
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minLength",
        "maxLength",
    ):
        value = working.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    for key in ("minimum", "maximum"):
        value = working.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    if "default" in working and working["default"] is not None:
        out["default"] = copy.deepcopy(working["default"])
    if "example" in working and working["example"] is not None:
        out["example"] = copy.deepcopy(working["example"])

    fmt = working.get("format")
    if isinstance(fmt, str) and resolved_type in _GEMINI_SCHEMA_FORMATS:
        if fmt in _GEMINI_SCHEMA_FORMATS[resolved_type]:
            out["format"] = fmt

    enum_values = working.get("enum")
    if isinstance(enum_values, list) and enum_values:
        if resolved_type in {None, "string"}:
            out["enum"] = [str(value) for value in enum_values]
            out["type"] = "string"
        else:
            # Gemini only validates enums on strings; keep the value list
            # visible to the model through the description instead.
            rendered = ", ".join(str(value) for value in enum_values)
            hint = f"Allowed values: {rendered}"
            out["description"] = (
                f"{out['description']} ({hint})" if out.get("description") else hint
            )

    properties = working.get("properties")
    if isinstance(properties, dict):
        clean_properties: dict[str, Any] = {}
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_name, str) or not prop_name:
                continue
            clean_properties[prop_name] = _gemini_schema(
                prop_schema if isinstance(prop_schema, dict) else {},
                root=root_schema,
                depth=depth + 1,
            )
        if clean_properties:
            out["properties"] = clean_properties
            required = working.get("required")
            if isinstance(required, list):
                kept = [
                    name for name in required if isinstance(name, str) and name in clean_properties
                ]
                if kept:
                    out["required"] = kept
            ordering = working.get("propertyOrdering")
            if isinstance(ordering, list):
                kept_order = [
                    name for name in ordering if isinstance(name, str) and name in clean_properties
                ]
                if kept_order:
                    out["propertyOrdering"] = kept_order

    items = working.get("items")
    if isinstance(items, list):
        items = items[0] if items and isinstance(items[0], dict) else None
    if isinstance(items, dict):
        out["items"] = _gemini_schema(items, root=root_schema, depth=depth + 1)
    elif out.get("type") == "array":
        # Gemini requires ``items`` on arrays; an untyped item schema is the
        # closest expression of JSON Schema's "anything".
        out["items"] = {}

    any_of = working.get("anyOf")
    if isinstance(any_of, list):
        branches = [
            _gemini_schema(branch, root=root_schema, depth=depth + 1)
            for branch in any_of
            if isinstance(branch, dict)
        ]
        # A ``{"type": "null"}`` branch is Gemini's ``nullable`` flag.
        if any(branch == {"nullable": True} for branch in branches):
            out["nullable"] = True
        branches = [branch for branch in branches if branch != {"nullable": True}]
        if len(branches) == 1:
            single = branches[0]
            for key, value in single.items():
                out.setdefault(key, value)
        elif branches:
            out["anyOf"] = branches

    return {key: value for key, value in out.items() if key in _GEMINI_SCHEMA_FIELDS}


def _gemini_function_declaration_from_chat_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    tool_type = str(tool.get("type") or "").strip()
    if tool_type != "function":
        if _is_gemini_grounding_tool(tool):
            return None
        raise LLMError(f"Gemini GenerateContent does not support tool type {tool_type!r}")
    function = tool.get("function")
    source = function if isinstance(function, dict) else tool
    name = str(source.get("name") or "").strip()
    if not name:
        return None
    raw_parameters = source.get("parameters")
    parameters = _gemini_schema(raw_parameters) if isinstance(raw_parameters, dict) else {}
    if not parameters.get("type"):
        parameters["type"] = "object"
    declaration: dict[str, Any] = {
        "name": name,
        "parameters": parameters,
    }
    description = str(source.get("description") or "").strip()
    if description:
        declaration["description"] = description
    return declaration


def _gemini_tools(
    tools: list[dict[str, Any]] | None,
    *,
    mode: str,
    adapter: str,
) -> _GeminiToolMapping:
    normalized_mode = str(mode or "off").strip().lower()
    normalized_adapter = (
        str(adapter or AUTO_WEB_SEARCH_ADAPTER).strip().lower() or AUTO_WEB_SEARCH_ADAPTER
    )
    raw_tools = [tool for tool in tools or [] if isinstance(tool, dict)]
    alysis_web_search_present = any(_is_alysis_web_search_function(tool) for tool in raw_tools)
    use_google_search = alysis_web_search_present and _gemini_grounding_allowed(
        mode=normalized_mode, adapter=normalized_adapter
    )
    if normalized_mode == "native" and alysis_web_search_present and not use_google_search:
        raise LLMError(
            "web_search_mode=native with protocol=gemini_generate_content requires "
            "web_search_adapter='auto' or 'gemini_grounding' for Gemini Google Search grounding; "
            f"got {normalized_adapter!r}"
        )

    function_declarations: list[dict[str, Any]] = []
    mapped_tools: list[dict[str, Any]] = []
    removed_alysis_web_search = False
    for tool in raw_tools:
        if _is_alysis_web_search_function(tool):
            if normalized_mode in {"off", "native"} or use_google_search:
                removed_alysis_web_search = True
                continue
        if _is_gemini_grounding_tool(tool):
            if normalized_mode in {"off", "external"}:
                continue
            mapped_tools.append(copy.deepcopy(tool))
            continue
        declaration = _gemini_function_declaration_from_chat_tool(tool)
        if declaration is not None:
            function_declarations.append(declaration)
    if function_declarations:
        mapped_tools.insert(0, {"functionDeclarations": function_declarations})
    if use_google_search and not any(_is_gemini_grounding_tool(tool) for tool in mapped_tools):
        mapped_tools.append({"google_search": {}})
    include_server_side_tool_invocations = bool(function_declarations) and any(
        _is_gemini_grounding_tool(tool) for tool in mapped_tools
    )
    return _GeminiToolMapping(
        tools=mapped_tools,
        added_google_search=use_google_search,
        removed_alysis_web_search=removed_alysis_web_search,
        include_server_side_tool_invocations=include_server_side_tool_invocations,
    )


def _gemini_tool_choice(
    tool_choice: Any,
    *,
    removed_alysis_web_search: bool,
    added_google_search: bool,
) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip()
        if normalized == "auto":
            return {"functionCallingConfig": {"mode": "AUTO"}}
        if normalized == "none":
            return {"functionCallingConfig": {"mode": "NONE"}}
        if normalized == "required":
            return {"functionCallingConfig": {"mode": "ANY"}}
        raise LLMError(f"Gemini GenerateContent does not support tool_choice={tool_choice!r}")
    if not isinstance(tool_choice, dict):
        raise LLMError("Gemini GenerateContent tool_choice must be a string or object")

    choice_type = str(tool_choice.get("type") or "").strip()
    if choice_type == "function":
        if "name" in tool_choice:
            name = str(tool_choice.get("name") or "").strip()
        else:
            function = tool_choice.get("function")
            name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        if not name:
            raise LLMError("Gemini GenerateContent forced function tool_choice is missing name")
        if (
            name == _ALYSIS_WEB_SEARCH_FUNCTION_NAME
            and removed_alysis_web_search
            and not added_google_search
        ):
            raise LLMError(
                "Gemini GenerateContent removed the Alysis Code web_search function for the "
                "selected web_search_mode; do not force tool_choice to function web_search"
            )
        if name == _ALYSIS_WEB_SEARCH_FUNCTION_NAME and added_google_search:
            raise LLMError(
                "Gemini GenerateContent cannot force Google Search grounding via tool_choice"
            )
        return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    if choice_type in {"auto", "any", "none"}:
        mode = "ANY" if choice_type == "any" else choice_type.upper()
        return {"functionCallingConfig": {"mode": mode}}
    raise LLMError(f"Gemini GenerateContent does not support tool_choice type {choice_type!r}")


def _system_instruction_from_parts(parts: list[str]) -> dict[str, Any] | None:
    text = "\n\n".join(part for part in parts if part).strip()
    if not text:
        return None
    return {"parts": [{"text": text}]}


def _tool_call_provider_metadata_indexes(
    message: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    metadata = message.get(PROVIDER_METADATA_KEY)
    if not isinstance(metadata, dict):
        return {}, {}
    entries = metadata.get(_TOOL_CALL_PROVIDER_METADATA_KEY)
    if not isinstance(entries, list):
        return {}, {}
    by_id: dict[str, dict[str, Any]] = {}
    by_index: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_metadata = entry.get("metadata")
        if not isinstance(entry_metadata, dict):
            continue
        gemini_metadata = entry_metadata.get(_GEMINI_METADATA_KEY)
        if not isinstance(gemini_metadata, dict):
            continue
        copied = copy.deepcopy(gemini_metadata)
        tool_call_id = entry.get("id")
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            by_id[tool_call_id.strip()] = copied
        index = entry.get("index")
        if isinstance(index, int):
            by_index[index] = copied
    return by_id, by_index


def _apply_gemini_thought_signature(
    part: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> None:
    if isinstance(metadata, dict):
        if "thoughtSignature" in metadata:
            part["thoughtSignature"] = copy.deepcopy(metadata["thoughtSignature"])
            return
        if "thought_signature" in metadata:
            part["thoughtSignature"] = copy.deepcopy(metadata["thought_signature"])
            return
    part["thoughtSignature"] = _DUMMY_IMPORTED_FUNCTION_CALL_THOUGHT_SIGNATURE


def _tool_call_parts_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []
    parts: list[dict[str, Any]] = []
    metadata_by_id, metadata_by_index = _tool_call_provider_metadata_indexes(message)
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue
        tool_call_index = len(parts)
        call_id = str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or "").strip()
        function = raw_tool_call.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            args = _json_arguments(function.get("arguments"))
        else:
            name = str(raw_tool_call.get("name") or "").strip()
            args = _json_arguments(raw_tool_call.get("arguments"))
        if not name:
            continue
        function_call: dict[str, Any] = {"name": name, "args": args}
        if call_id:
            function_call["id"] = call_id
        part = {"functionCall": function_call}
        metadata = metadata_by_id.get(call_id) if call_id else None
        if metadata is None:
            metadata = metadata_by_index.get(tool_call_index)
        _apply_gemini_thought_signature(part, metadata)
        parts.append(part)
    return parts


def _collect_function_call_names(content: dict[str, Any], call_names: dict[str, str]) -> None:
    parts = content.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        function_call = part.get("functionCall")
        if not isinstance(function_call, dict):
            continue
        call_id = str(function_call.get("id") or "").strip()
        name = str(function_call.get("name") or "").strip()
        if call_id and name:
            call_names[call_id] = name


def _collect_idless_function_call_names(
    content: dict[str, Any],
    message: dict[str, Any],
    call_names: dict[str, str],
    idless_call_ids: set[str],
) -> None:
    """Pair native ID-less calls with their local assistant ToolCall IDs."""

    parts = content.get("parts")
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(parts, list) or not isinstance(raw_tool_calls, list):
        return
    native_calls = [
        part.get("functionCall")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)
    ]
    if len(native_calls) != len(raw_tool_calls):
        return
    for native_call, raw_tool_call in zip(native_calls, raw_tool_calls, strict=True):
        if not isinstance(native_call, dict) or native_call.get("id") not in {None, ""}:
            continue
        if not isinstance(raw_tool_call, dict):
            continue
        local_call_id = str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or "").strip()
        function = raw_tool_call.get("function")
        local_name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        native_name = str(native_call.get("name") or "").strip()
        name = native_name or local_name
        if local_call_id and name:
            call_names[local_call_id] = name
            idless_call_ids.add(local_call_id)


def _is_function_response_user_content(content: dict[str, Any]) -> bool:
    if str(content.get("role") or "") != "user":
        return False
    parts = content.get("parts")
    return (
        isinstance(parts, list)
        and bool(parts)
        and all(
            isinstance(part, dict) and isinstance(part.get("functionResponse"), dict)
            for part in parts
        )
    )


def _gemini_contents_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    idless_call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role in {"system", "developer"}:
            text = _content_to_text(message.get("content")).strip()
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            parts = _gemini_parts_from_content(message.get("content"))
            if parts:
                contents.append({"role": "user", "parts": parts})
            continue
        if role == "assistant":
            metadata_content = _metadata_content(message)
            if metadata_content is not None:
                contents.append(metadata_content)
                _collect_function_call_names(metadata_content, call_names)
                _collect_idless_function_call_names(
                    metadata_content,
                    message,
                    call_names,
                    idless_call_ids,
                )
                continue
            parts = _gemini_parts_from_content(message.get("content"))
            parts.extend(_tool_call_parts_from_message(message))
            if parts:
                content = {"role": "model", "parts": parts}
                contents.append(content)
                _collect_function_call_names(content, call_names)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or message.get("call_id") or "").strip()
            if not call_id:
                raise LLMError("Gemini GenerateContent function response is missing tool_call_id")
            name = str(message.get("name") or call_names.get(call_id) or "").strip()
            if not name:
                raise LLMError(
                    "Gemini GenerateContent function response is missing function name for "
                    f"tool_call_id={call_id!r}"
                )
            response_payload = _json_response_payload(message.get("content"))
            function_response_part = {
                "functionResponse": {
                    "name": name,
                    "response": response_payload,
                }
            }
            if call_id not in idless_call_ids:
                function_response_part["functionResponse"]["id"] = call_id
            if contents and _is_function_response_user_content(contents[-1]):
                parts = contents[-1].setdefault("parts", [])
                if isinstance(parts, list):
                    parts.append(function_response_part)
                else:
                    contents.append({"role": "user", "parts": [function_response_part]})
            else:
                contents.append({"role": "user", "parts": [function_response_part]})
            continue
        raise LLMError(f"Gemini GenerateContent cannot send message role {role!r}")
    return _system_instruction_from_parts(system_parts), contents


def _explicit_cached_content_plan(
    *,
    model: str,
    system_instruction: dict[str, Any] | None,
    contents: list[dict[str, Any]],
    ttl: str | None,
    min_tokens: int,
) -> _GeminiCachedContentPlan | None:
    if len(contents) < 2:
        return None
    cache_contents = copy.deepcopy(contents[:-1])
    suffix_contents = copy.deepcopy(contents[-1:])
    create_payload: dict[str, Any] = {
        "model": _gemini_model_resource_name(model),
        "contents": cache_contents,
    }
    if system_instruction is not None:
        create_payload["systemInstruction"] = copy.deepcopy(system_instruction)
    if ttl:
        create_payload["ttl"] = ttl
    estimated_tokens = estimate_tokens(
        json.dumps(create_payload, ensure_ascii=False, sort_keys=True)
    )
    if estimated_tokens < max(0, int(min_tokens)):
        return None
    return _GeminiCachedContentPlan(
        signature=_stable_digest(create_payload),
        create_payload=create_payload,
        suffix_contents=suffix_contents,
        estimated_tokens=estimated_tokens,
    )


def _parse_usage(raw: Any) -> LLMUsage | None:
    if not isinstance(raw, dict):
        return None

    def _as_non_negative_int(value: Any) -> int | None:
        try:
            parsed = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        if parsed is None or parsed >= 0:
            return parsed
        return None

    prompt_tokens = _as_non_negative_int(raw.get("promptTokenCount"))
    completion_tokens = _as_non_negative_int(raw.get("candidatesTokenCount"))
    tool_use_prompt_tokens = _as_non_negative_int(raw.get("toolUsePromptTokenCount"))
    thoughts_tokens = _as_non_negative_int(raw.get("thoughtsTokenCount"))
    total_tokens = _as_non_negative_int(raw.get("totalTokenCount"))
    cached_tokens = _as_non_negative_int(raw.get("cachedContentTokenCount"))
    # Gemini reports tool-use prompts separately from the ordinary prompt and
    # candidates separately from thinking. Fold each provider-owned component
    # into the corresponding billing side so prompt+completion reconciles with
    # totalTokenCount while the raw breakdown remains available for diagnostics.
    if tool_use_prompt_tokens:
        prompt_tokens = (prompt_tokens or 0) + tool_use_prompt_tokens
    if thoughts_tokens:
        completion_tokens = (completion_tokens or 0) + thoughts_tokens
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    input_tokens_uncached = None
    if prompt_tokens is not None and cached_tokens is not None:
        input_tokens_uncached = max(0, prompt_tokens - cached_tokens)
    usage = LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_prompt_tokens=cached_tokens,
        input_tokens_uncached=input_tokens_uncached,
        cache_read_input_tokens=cached_tokens,
        reasoning_tokens=thoughts_tokens,
        raw_provider_usage=copy.deepcopy(raw),
    )
    if (
        usage.prompt_tokens is None
        and usage.completion_tokens is None
        and usage.total_tokens is None
        and usage.cached_prompt_tokens is None
        and usage.cache_read_input_tokens is None
    ):
        return None
    return usage


def _extract_error_message(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        message = str(error_obj.get("message") or "").strip()
        if message:
            status = str(error_obj.get("status") or "").strip()
            return f"{status}: {message}" if status else message
    return None


def _gemini_thought_summary_request_rejected(response: httpx.Response) -> bool:
    if response.status_code not in {400, 422}:
        return False
    detail = response.text.casefold()
    names_summary_field = any(
        marker in detail for marker in ("includethoughts", "include_thoughts", "thought summaries")
    )
    rejects_field = any(
        marker in detail
        for marker in (
            "unsupported",
            "not supported",
            "unknown field",
            "unknown name",
            "unrecognized",
            "invalid field",
        )
    )
    return names_summary_field and rejects_field


def _remove_gemini_thought_summary_request(payload: dict[str, Any]) -> bool:
    generation_config = payload.get("generationConfig")
    if not isinstance(generation_config, dict):
        return False
    thinking_config = generation_config.get("thinkingConfig")
    if not isinstance(thinking_config, dict) or "includeThoughts" not in thinking_config:
        return False
    thinking_config.pop("includeThoughts", None)
    if not thinking_config:
        generation_config.pop("thinkingConfig", None)
    return True


def _gemini_payload_requests_thought_summaries(payload: dict[str, Any]) -> bool:
    generation_config = payload.get("generationConfig")
    if not isinstance(generation_config, dict):
        return False
    thinking_config = generation_config.get("thinkingConfig")
    return isinstance(thinking_config, dict) and thinking_config.get("includeThoughts") is True


def _candidate(data: dict[str, Any]) -> dict[str, Any] | None:
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return None
    return next((item for item in candidates if isinstance(item, dict)), None)


def _candidate_content(candidate: dict[str, Any]) -> dict[str, Any] | None:
    content = candidate.get("content")
    return content if isinstance(content, dict) else None


def _extract_text(parts: list[Any]) -> str:
    text_parts: list[str] = []
    for part in parts:
        if (
            isinstance(part, dict)
            and part.get("thought") is not True
            and isinstance(part.get("text"), str)
        ):
            text_parts.append(part["text"])
    return "".join(text_parts)


def _emit_reasoning_parts(
    parts: list[Any],
    callback: Callable[[str], None] | None,
) -> None:
    if callback is None:
        return
    for part in parts:
        if not isinstance(part, dict) or part.get("thought") is not True:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            callback(text)


def _reasoning_outputs_from_parts(parts: list[Any]) -> tuple[ReasoningOutput, ...]:
    """Normalize Gemini thought summaries while excluding opaque signatures."""

    outputs: list[ReasoningOutput] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("thought") is not True:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            outputs.append(
                ReasoningOutput(
                    text=text,
                    kind=ReasoningOutputKind.SUMMARY,
                    provider="gemini",
                )
            )
    return tuple(outputs)


def _gemini_function_call_id(
    function_call: dict[str, Any],
    index: int,
    *,
    fallback_scope: str = "",
) -> str:
    native_id = str(function_call.get("id") or "").strip()
    if native_id:
        return native_id
    scope = str(fallback_scope or "").strip() or secrets.token_hex(16)
    digest = hashlib.sha256(f"{scope}:{index}".encode()).hexdigest()[:32]
    return f"alysis_gemini_idless_{digest}"


def _parse_tool_calls(parts: list[Any], *, fallback_scope: str = "") -> list[ToolCall]:
    tool_calls: list[ToolCall] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        function_call = part.get("functionCall")
        if not isinstance(function_call, dict):
            continue
        name = str(function_call.get("name") or "").strip()
        if not name:
            continue
        call_id = _gemini_function_call_id(
            function_call,
            index,
            fallback_scope=fallback_scope,
        )
        raw_args = function_call.get("args")
        args = dict(raw_args) if isinstance(raw_args, dict) else _json_arguments(raw_args)
        metadata: dict[str, Any] = {"part_index": index}
        for key in ("thoughtSignature", "thought_signature"):
            if key in part:
                metadata[key] = part.get(key)
        tool_calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=args,
                provider_metadata={_GEMINI_METADATA_KEY: metadata},
            )
        )
    return tool_calls


def _source_from_grounding_chunk(chunk: Any) -> dict[str, Any] | None:
    if not isinstance(chunk, dict):
        return None
    payload = chunk.get("web") if isinstance(chunk.get("web"), dict) else chunk
    url = str(payload.get("uri") or payload.get("url") or "").strip()
    if not url:
        return None
    return {"url": url, "title": str(payload.get("title") or "").strip()}


def _grounding_metadata_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    grounding = candidate.get("groundingMetadata")
    if not isinstance(grounding, dict):
        return {}
    raw_chunks = grounding.get("groundingChunks")
    chunks = raw_chunks if isinstance(raw_chunks, list) else []
    sources: list[dict[str, Any]] = []
    for chunk in chunks:
        source = _source_from_grounding_chunk(chunk)
        if source is not None and source not in sources:
            sources.append(source)
    citations: list[dict[str, Any]] = []
    supports = grounding.get("groundingSupports")
    if isinstance(supports, list):
        for support in supports:
            if not isinstance(support, dict):
                continue
            segment = support.get("segment") if isinstance(support.get("segment"), dict) else {}
            for raw_index in support.get("groundingChunkIndices", []):
                try:
                    source = _source_from_grounding_chunk(chunks[int(raw_index)])
                except (TypeError, ValueError, IndexError):
                    continue
                if source is None:
                    continue
                citations.append(
                    {
                        "title": source["title"],
                        "url": source["url"],
                        "start_index": segment.get("startIndex"),
                        "end_index": segment.get("endIndex"),
                        "text": segment.get("text"),
                    }
                )
    raw_queries = grounding.get("webSearchQueries")
    queries = (
        [str(query).strip() for query in raw_queries if str(query).strip()]
        if isinstance(raw_queries, list)
        else []
    )
    payload: dict[str, Any] = {
        "groundingMetadata": copy.deepcopy(grounding),
    }
    if sources:
        payload["sources"] = sources
    if citations:
        payload["citations"] = citations
    if queries:
        payload["queries"] = queries
    return payload


def _gemini_provider_metadata(
    data: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {}
    response_id = str(data.get("responseId") or "").strip()
    if response_id:
        metadata["response_id"] = response_id
    model_version = str(data.get("modelVersion") or "").strip()
    if model_version:
        metadata["model_version"] = model_version
    finish_reason = str(candidate.get("finishReason") or "").strip()
    if finish_reason:
        metadata["finish_reason"] = finish_reason
    for key in ("finishMessage", "safetyRatings", "citationMetadata"):
        value = candidate.get(key)
        if value is not None:
            metadata[_camel_to_snake(key)] = copy.deepcopy(value)
    content = _candidate_content(candidate)
    if isinstance(content, dict):
        # Thought signatures are bound to the provider's exact Part. In
        # particular, FunctionCall.id is optional; never insert a local fallback
        # ID into the native replay envelope.
        metadata["content"] = copy.deepcopy(content)
    metadata.update(_grounding_metadata_payload(candidate))
    usage = data.get("usageMetadata")
    if isinstance(usage, dict):
        metadata["usage"] = copy.deepcopy(usage)
    stream_metadata = data.get("streamMetadata")
    if isinstance(stream_metadata, dict):
        metadata["stream_metadata"] = copy.deepcopy(stream_metadata)
    return {_GEMINI_METADATA_KEY: metadata} if metadata else None


def _camel_to_snake(value: str) -> str:
    result = []
    for index, char in enumerate(value):
        if char.isupper() and index > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _response_from_json(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=data)


def _append_unique(target: list[Any], values: list[Any]) -> None:
    for value in values:
        copied = copy.deepcopy(value)
        if copied not in target:
            target.append(copied)


def _merge_stream_grounding_metadata(
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(current)
    for key, value in incoming.items():
        if isinstance(value, list):
            existing = merged.setdefault(key, [])
            if isinstance(existing, list):
                _append_unique(existing, value)
            else:
                merged[key] = copy.deepcopy(value)
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_stream_grounding_metadata(merged[key], value)
            continue
        merged[key] = copy.deepcopy(value)
    return merged


class _GeminiStreamAccumulator:
    def __init__(
        self,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None,
    ) -> None:
        self.on_text_delta = on_text_delta
        self.on_reasoning_delta = on_reasoning_delta
        self.response_id: str | None = None
        self.model_version: str | None = None
        self.usage_metadata: dict[str, Any] = {}
        self.parts: list[dict[str, Any]] = []
        self.role = "model"
        self.finish_reason: str | None = None
        self.finish_message: str | None = None
        self.safety_ratings: list[Any] | None = None
        self.citation_metadata: dict[str, Any] | None = None
        self.grounding_metadata: dict[str, Any] = {}
        self.stream_metadata: dict[str, Any] = {"chunks": 0}
        self.seen_candidate = False

    def handle(self, frame: SSEFrame, data: dict[str, Any]) -> None:
        _ = frame
        error_message = _extract_error_message(data)
        if error_message:
            raise LLMError(f"Gemini GenerateContent stream error: {error_message}")
        self.stream_metadata["chunks"] = int(self.stream_metadata["chunks"]) + 1
        if not data:
            self._record_unknown_chunk(data)
            return

        response_id = data.get("responseId")
        if isinstance(response_id, str) and response_id.strip():
            self.response_id = response_id
        model_version = data.get("modelVersion")
        if isinstance(model_version, str) and model_version.strip():
            self.model_version = model_version
        usage = data.get("usageMetadata")
        if isinstance(usage, dict):
            self.usage_metadata.update(copy.deepcopy(usage))

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            self._record_unknown_chunk(data)
            return
        candidate = next((item for item in candidates if isinstance(item, dict)), None)
        if candidate is None:
            self._record_unknown_chunk(data)
            return
        self.seen_candidate = True
        self._handle_candidate(candidate)

    def _handle_candidate(self, candidate: dict[str, Any]) -> None:
        finish_reason = candidate.get("finishReason")
        if isinstance(finish_reason, str) and finish_reason.strip():
            self.finish_reason = finish_reason
        finish_message = candidate.get("finishMessage")
        if isinstance(finish_message, str) and finish_message.strip():
            self.finish_message = finish_message
        safety_ratings = candidate.get("safetyRatings")
        if isinstance(safety_ratings, list):
            self.safety_ratings = copy.deepcopy(safety_ratings)
        citation_metadata = candidate.get("citationMetadata")
        if isinstance(citation_metadata, dict):
            self.citation_metadata = copy.deepcopy(citation_metadata)
        grounding_metadata = candidate.get("groundingMetadata")
        if isinstance(grounding_metadata, dict):
            self.grounding_metadata = _merge_stream_grounding_metadata(
                self.grounding_metadata,
                grounding_metadata,
            )

        content = candidate.get("content")
        if not isinstance(content, dict):
            return
        role = content.get("role")
        if isinstance(role, str) and role.strip():
            self.role = role
        parts = content.get("parts")
        if not isinstance(parts, list):
            return
        for part in parts:
            if not isinstance(part, dict):
                continue
            copied = copy.deepcopy(part)
            self.parts.append(copied)
            text = copied.get("text")
            if not isinstance(text, str) or not text:
                continue
            if copied.get("thought") is True:
                if self.on_reasoning_delta is not None:
                    self.on_reasoning_delta(text)
            elif self.on_text_delta is not None:
                self.on_text_delta(text)

    def _record_unknown_chunk(self, data: dict[str, Any]) -> None:
        unknown = self.stream_metadata.setdefault("unknown_chunks", [])
        if isinstance(unknown, list):
            unknown.append(copy.deepcopy(data))

    def finish(self) -> dict[str, Any]:
        if int(self.stream_metadata["chunks"]) <= 0:
            raise LLMError("Gemini GenerateContent stream returned no chunks")
        if not self.seen_candidate:
            raise LLMError("Gemini GenerateContent stream returned no candidate chunks")

        candidate: dict[str, Any] = {
            "content": {
                "role": self.role,
                "parts": copy.deepcopy(self.parts),
            }
        }
        if self.finish_reason:
            candidate["finishReason"] = self.finish_reason
        if self.finish_message:
            candidate["finishMessage"] = self.finish_message
        if self.safety_ratings is not None:
            candidate["safetyRatings"] = copy.deepcopy(self.safety_ratings)
        if self.citation_metadata is not None:
            candidate["citationMetadata"] = copy.deepcopy(self.citation_metadata)
        if self.grounding_metadata:
            candidate["groundingMetadata"] = copy.deepcopy(self.grounding_metadata)

        data: dict[str, Any] = {
            "candidates": [candidate],
            "streamMetadata": copy.deepcopy(self.stream_metadata),
        }
        if self.response_id:
            data["responseId"] = self.response_id
        if self.model_version:
            data["modelVersion"] = self.model_version
        if self.usage_metadata:
            data["usageMetadata"] = copy.deepcopy(self.usage_metadata)
        return data


class GeminiGenerateContentClient:
    usage_contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        input_token_count_strategy="gemini_count_tokens",
    )
    usage_counts_authoritative = usage_contract.response_usage_authoritative
    supports_tool_calling = True
    supports_forced_tool_choice = True

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
        thinking_level: str | None = None,
        thinking_budget: int | None = None,
        transport: httpx.BaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_key: str | None = None,
        web_search_mode: str = "off",
        web_search_adapter: str = AUTO_WEB_SEARCH_ADAPTER,
        explicit_cached_content_enabled: bool = False,
        cached_content_ttl: str | None = None,
        cached_content_min_tokens: int | None = _GEMINI_EXPLICIT_CACHE_MIN_TOKENS,
        cached_content_max_entries: int | None = _GEMINI_EXPLICIT_CACHE_MAX_ENTRIES,
        cached_content_time_fn: Callable[[], float] | None = None,
        prompt_cache_policy_metadata: Mapping[str, Any] | None = None,
        provider_concurrency_caps: dict[str, int] | None = None,
        provider_retry_settings: ProviderRetrySettings | None = None,
        provider_sleep_fn: Callable[[float], None] | None = None,
        provider_random_fn: Callable[[], float] | None = None,
        usage_contract: UsageContract | None = None,
        route_identity: ProviderRouteIdentity | None = None,
    ) -> None:
        self.base_url = _gemini_native_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.prompt_cache_key = str(prompt_cache_key or "").strip() or None
        self.prompt_cache_retention = str(prompt_cache_retention or "").strip() or None
        self.enable_thinking = enable_thinking
        self.reasoning_effort = str(reasoning_effort or "").strip().lower() or None
        self.thinking_level = str(thinking_level or "").strip().lower() or None
        self.thinking_budget = thinking_budget
        self._transport = transport
        self.extra_headers = canonicalize_extra_headers(extra_headers)
        self.provider_key = str(provider_key or "").strip() or None
        self.route_identity = route_identity or build_provider_route_identity(
            protocol="gemini_generate_content",
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
            credential_scope=credential_scope_fingerprint(self.api_key),
            routing_headers=self.extra_headers,
        )
        self.web_search_mode = str(web_search_mode or "off").strip().lower()
        self.web_search_adapter = (
            str(web_search_adapter or AUTO_WEB_SEARCH_ADAPTER).strip().lower()
            or AUTO_WEB_SEARCH_ADAPTER
        )
        self.explicit_cached_content_enabled = bool(explicit_cached_content_enabled)
        self.cached_content_ttl = str(cached_content_ttl or "3600s").strip() or "3600s"
        self.cached_content_min_tokens = _normalize_cached_content_min_tokens(
            cached_content_min_tokens
        )
        self.cached_content_max_entries = _normalize_cached_content_max_entries(
            cached_content_max_entries
        )
        self.cached_content_ttl_seconds = _parse_cached_content_ttl_seconds(self.cached_content_ttl)
        self.cached_content_refresh_margin_seconds = _cached_content_refresh_margin_seconds(
            self.cached_content_ttl_seconds
        )
        self._cached_content_time_fn = cached_content_time_fn or time.monotonic
        self.prompt_cache_policy_metadata = (
            copy.deepcopy(dict(prompt_cache_policy_metadata))
            if isinstance(prompt_cache_policy_metadata, Mapping)
            else None
        )
        self._cached_content_by_signature: dict[str, _GeminiCachedContentEntry] = {}
        self._cached_content_create_disabled_reason: str | None = None
        self._cached_content_create_transient_failures = 0
        self._thought_summaries_supported: bool | None = None
        self.provider_concurrency_caps = dict(
            DEFAULT_PROVIDER_CONCURRENCY_CAPS
            if provider_concurrency_caps is None
            else provider_concurrency_caps
        )
        self.provider_retry_settings = provider_retry_settings or ProviderRetrySettings()
        self._provider_sleep_fn = provider_sleep_fn
        self._provider_random_fn = provider_random_fn
        self.usage_contract = usage_contract or type(self).usage_contract
        self.usage_counts_authoritative = self.usage_contract.response_usage_authoritative
        self._input_token_count_available: bool | None = None

    def _headers(self) -> dict[str, str]:
        headers = merge_canonical_headers(
            {
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "alysis-code/0.1.0",
            },
            self.extra_headers,
        )
        return _headers_with_default_accept_encoding(headers)

    @staticmethod
    def _llm_error_from_response(response: httpx.Response) -> LLMError:
        try:
            data = response.json()
        except Exception:
            body = response.text
            if len(body) > 1000:
                body = body[:1000] + "...(truncated)"
            return LLMError(
                sanitize_error_text_for_output(f"LLM error {response.status_code}: {body}")
            )
        error_message = _extract_error_message(data)
        if error_message:
            return LLMError(
                sanitize_error_text_for_output(f"LLM error {response.status_code}: {error_message}")
            )
        return LLMError(
            sanitize_error_text_for_output(f"LLM error {response.status_code}: {data!r}")
        )

    def count_input_tokens(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
    ) -> InputTokenCount | None:
        if self._input_token_count_available is False:
            return None
        messages = gate_messages_for_provider_route(messages, self.route_identity)
        system_instruction, contents = _gemini_contents_from_messages(messages)
        tool_mapping = _gemini_tools(
            tools,
            mode=self.web_search_mode,
            adapter=self.web_search_adapter,
        )
        generate_request: dict[str, Any] = {
            "model": f"models/{self.model}",
            "contents": contents,
        }
        if system_instruction is not None:
            generate_request["systemInstruction"] = system_instruction
        if tool_mapping.tools:
            generate_request["tools"] = tool_mapping.tools
            mapped_tool_choice = _gemini_tool_choice(
                tool_choice,
                removed_alysis_web_search=tool_mapping.removed_alysis_web_search,
                added_google_search=tool_mapping.added_google_search,
            )
            tool_config = dict(mapped_tool_choice or {})
            if tool_mapping.include_server_side_tool_invocations:
                tool_config[_INCLUDE_SERVER_SIDE_TOOL_INVOCATIONS] = True
            if tool_config:
                generate_request["toolConfig"] = tool_config
        payload = {"generateContentRequest": generate_request}
        encoded_model = quote(self.model, safe="")
        url = f"{self.base_url}/models/{encoded_model}:countTokens"

        def _send_request() -> InputTokenCount | None:
            try:
                with provider_httpx_client(
                    timeout=self.timeout_s, transport=self._transport
                ) as client:
                    response = client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                raise LLMError(
                    "Gemini input token count request failed: "
                    f"{sanitize_error_text_for_output(exc)}"
                ) from exc
            if response.status_code in {404, 405, 501}:
                self._input_token_count_available = False
                return None
            if response.status_code >= 400:
                raise self._llm_error_from_response(response)
            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                raise LLMError("Gemini input token count returned non-JSON response") from exc
            count = _non_negative_int(data.get("totalTokens") if isinstance(data, dict) else None)
            if count is None:
                raise LLMError("Gemini input token count response omitted totalTokens")
            self._input_token_count_available = True
            return InputTokenCount(
                input_tokens=count,
                raw_provider_usage=copy.deepcopy(data),
            )

        return run_provider_limited_call(
            call=_send_request,
            provider_key=self.provider_key,
            provider_concurrency_caps=self.provider_concurrency_caps,
            retry_settings=self.provider_retry_settings,
            operation="gemini_generate_content_count_input_tokens",
            sleep_fn=self._provider_sleep_fn,
            random_fn=self._provider_random_fn,
            retry_deadline_allows=getattr(self, "_provider_retry_deadline_allows", None),
        )

    def _resolve_cached_content(
        self,
        *,
        client: httpx.Client,
        plan: _GeminiCachedContentPlan,
        cancellation_token: Any | None = None,
    ) -> tuple[str | None, str, dict[str, Any]]:
        lifecycle = self._cached_content_lifecycle_metadata()
        now = self._cached_content_now()
        entry = self._cached_content_by_signature.get(plan.signature)
        if entry is not None:
            refresh_reason = self._cached_content_refresh_reason(entry, now)
            if refresh_reason:
                lifecycle["refresh_reason"] = refresh_reason
                self._evict_cached_content_entry(
                    client=client,
                    signature=plan.signature,
                    reason=refresh_reason,
                    lifecycle=lifecycle,
                    cancellation_token=cancellation_token,
                )
            else:
                entry = replace(entry, last_used_at=now)
                self._cached_content_by_signature[plan.signature] = entry
                lifecycle["reused_entry_count"] = 1
                if entry.pending_creation_tokens is not None:
                    # Creation spend from an attempt that failed before any
                    # successful use; keep surfacing it until a success
                    # consumes the pending marker.
                    lifecycle["cache_creation_input_tokens"] = entry.pending_creation_tokens
                lifecycle.update(self._cached_content_entry_timing_metadata(entry, now))
                lifecycle["entry_count"] = len(self._cached_content_by_signature)
                return entry.name, "reused", lifecycle
        if self._cached_content_create_disabled_reason:
            lifecycle["create_disabled_reason"] = self._cached_content_create_disabled_reason
            return None, "create_disabled", lifecycle
        try:
            response = cancellable_httpx_send(
                client=client,
                cancellation_token=cancellation_token,
                method="POST",
                url=f"{self.base_url}/cachedContents",
                headers=self._headers(),
                json=plan.create_payload,
            )
        except Exception as e:  # noqa: BLE001
            raise_if_cancelled(cancellation_token)
            return (
                None,
                self._record_cached_content_create_failure(
                    status="create_failed",
                    detail=repr(e),
                    lifecycle=lifecycle,
                    transient=True,
                ),
                lifecycle,
            )
        if response.status_code >= 400:
            return (
                None,
                self._record_cached_content_create_failure(
                    status="create_rejected",
                    detail=f"status {response.status_code}: {response.text}",
                    lifecycle=lifecycle,
                    transient=_cached_content_create_failure_is_transient(response.status_code),
                ),
                lifecycle,
            )
        try:
            data = response.json()
        except Exception:
            return (
                None,
                self._record_cached_content_create_failure(
                    status="create_non_json",
                    detail="create response was not JSON",
                    lifecycle=lifecycle,
                ),
                lifecycle,
            )
        if not isinstance(data, dict):
            return (
                None,
                self._record_cached_content_create_failure(
                    status="create_unexpected_payload",
                    detail="create response was not a JSON object",
                    lifecycle=lifecycle,
                ),
                lifecycle,
            )
        # Cache-write ingestion is billed once the create succeeds, so capture it
        # before validating the rest of the payload.
        create_usage_tokens = _cached_content_create_usage_tokens(data.get("usageMetadata"))
        if create_usage_tokens is not None:
            lifecycle["cache_creation_input_tokens"] = create_usage_tokens
        name = str(data.get("name") or "").strip()
        if not name:
            return (
                None,
                self._record_cached_content_create_failure(
                    status="create_missing_name",
                    detail="create response had no cachedContents name",
                    lifecycle=lifecycle,
                ),
                lifecycle,
            )
        self._cached_content_create_transient_failures = 0
        while len(self._cached_content_by_signature) >= self.cached_content_max_entries:
            oldest_key = min(
                self._cached_content_by_signature,
                key=lambda key: self._cached_content_by_signature[key].last_used_at,
            )
            self._evict_cached_content_entry(
                client=client,
                signature=oldest_key,
                reason="max_entries_exceeded",
                lifecycle=lifecycle,
                cancellation_token=cancellation_token,
            )
        ttl_seconds = self.cached_content_ttl_seconds
        refresh_margin_seconds = self.cached_content_refresh_margin_seconds
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        refresh_after = (
            max(now, expires_at - refresh_margin_seconds)
            if expires_at is not None and refresh_margin_seconds is not None
            else None
        )
        entry = _GeminiCachedContentEntry(
            name=name,
            signature=plan.signature,
            created_at=now,
            last_used_at=now,
            ttl_seconds=ttl_seconds,
            refresh_after=refresh_after,
            expires_at=expires_at,
            estimated_tokens=plan.estimated_tokens,
            pending_creation_tokens=create_usage_tokens,
        )
        self._cached_content_by_signature[plan.signature] = entry
        lifecycle["created_entry_count"] = 1
        lifecycle.update(self._cached_content_entry_timing_metadata(entry, now))
        lifecycle["entry_count"] = len(self._cached_content_by_signature)
        return name, "created", lifecycle

    def _record_cached_content_create_failure(
        self,
        *,
        status: str,
        detail: str,
        lifecycle: dict[str, Any],
        transient: bool = False,
    ) -> str:
        error_detail = _trimmed_create_error_detail(f"{status}: {detail}")
        lifecycle["create_error"] = error_detail
        if transient:
            # Transient failures may clear up on their own, so keep retrying
            # the create until several consecutive attempts have missed.
            self._cached_content_create_transient_failures += 1
            lifecycle["create_transient_failure_count"] = (
                self._cached_content_create_transient_failures
            )
            if (
                self._cached_content_create_transient_failures
                < _GEMINI_EXPLICIT_CACHE_TRANSIENT_CREATE_FAILURE_LIMIT
            ):
                return status
            error_detail = _trimmed_create_error_detail(
                f"{error_detail} "
                f"({self._cached_content_create_transient_failures} consecutive "
                f"transient failures)"
            )
        # Negative memoization: a deterministic rejection (or an exhausted
        # transient budget) keeps failing on the same client config, so stop
        # paying a blocking round-trip per call.
        self._cached_content_create_disabled_reason = error_detail
        return status

    def apply_cache_settings(
        self,
        *,
        enabled: bool | None = None,
        ttl: str | None = None,
        min_tokens: int | None = None,
    ) -> None:
        previous = (
            self.explicit_cached_content_enabled,
            self.cached_content_ttl,
            self.cached_content_min_tokens,
        )
        if enabled is not None:
            self.explicit_cached_content_enabled = bool(enabled)
        if ttl is not None:
            self.cached_content_ttl = str(ttl or "3600s").strip() or "3600s"
        if min_tokens is not None:
            self.cached_content_min_tokens = _normalize_cached_content_min_tokens(min_tokens)
        self.cached_content_ttl_seconds = _parse_cached_content_ttl_seconds(self.cached_content_ttl)
        self.cached_content_refresh_margin_seconds = _cached_content_refresh_margin_seconds(
            self.cached_content_ttl_seconds
        )
        if previous == (
            self.explicit_cached_content_enabled,
            self.cached_content_ttl,
            self.cached_content_min_tokens,
        ):
            return
        self._evict_all_cached_content_entries(reason="cache_settings_changed")
        self._cached_content_create_disabled_reason = None
        self._cached_content_create_transient_failures = 0

    def _evict_all_cached_content_entries(self, *, reason: str) -> None:
        if not self._cached_content_by_signature:
            return
        signatures = list(self._cached_content_by_signature)
        try:
            # Config-apply path: clamp per-delete latency so a hanging endpoint
            # cannot stall a config save for max_entries * timeout_s.
            timeout_s = min(self.timeout_s, _GEMINI_EXPLICIT_CACHE_EVICT_ALL_TIMEOUT_S)
            with provider_httpx_client(timeout=timeout_s, transport=self._transport) as client:
                for index, signature in enumerate(signatures):
                    lifecycle: dict[str, Any] = {}
                    self._evict_cached_content_entry(
                        client=client,
                        signature=signature,
                        reason=reason,
                        lifecycle=lifecycle,
                    )
                    delete_status = str(lifecycle.get("delete_status") or "unknown")
                    if delete_status in {"deleted", "already_absent"}:
                        _LOGGER.debug(
                            "gemini_cached_content_evicted",
                            extra={
                                "model": self.model,
                                "eviction_reason": reason,
                                "delete_status": delete_status,
                            },
                        )
                        continue
                    # First failed delete: assume the endpoint is unhealthy and
                    # let the remaining server-side entries expire via their TTL
                    # instead of queueing more blocking round-trips.
                    _LOGGER.warning(
                        "gemini_cached_content_evict_delete_failed",
                        extra={
                            "model": self.model,
                            "eviction_reason": reason,
                            "delete_status": delete_status,
                            "remaining_entry_count": len(signatures) - index - 1,
                        },
                    )
                    break
        except Exception:
            _LOGGER.warning(
                "gemini_cached_content_evict_transport_failed",
                exc_info=True,
                extra={
                    "model": self.model,
                    "eviction_reason": reason,
                    "remaining_entry_count": len(self._cached_content_by_signature),
                },
            )
        finally:
            # Entries not deleted expire server-side via their TTL; local
            # tracking must never keep the stale references.
            self._cached_content_by_signature.clear()

    def _clear_cached_content_entry(
        self,
        plan: _GeminiCachedContentPlan,
        *,
        client: httpx.Client | None = None,
        reason: str = "cleared",
        lifecycle: dict[str, Any] | None = None,
        cancellation_token: Any | None = None,
    ) -> None:
        entry = self._cached_content_by_signature.pop(plan.signature, None)
        if entry is None:
            return
        target_lifecycle = lifecycle if lifecycle is not None else {}
        target_lifecycle["evicted_entry_count"] = (
            int(target_lifecycle.get("evicted_entry_count") or 0) + 1
        )
        self._append_cached_content_eviction_reason(target_lifecycle, reason)
        if client is not None:
            self._delete_cached_content_entry(
                client=client,
                entry=entry,
                lifecycle=target_lifecycle,
                cancellation_token=cancellation_token,
            )
        if lifecycle is not None:
            lifecycle["entry_count"] = len(self._cached_content_by_signature)

    def _cached_content_now(self) -> float:
        try:
            value = float(self._cached_content_time_fn())
        except Exception:
            return time.monotonic()
        return value

    def _cached_content_lifecycle_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entry_count": len(self._cached_content_by_signature),
            "max_entries": self.cached_content_max_entries,
        }
        ttl_seconds = _rounded_non_negative_seconds(self.cached_content_ttl_seconds)
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        refresh_margin = _rounded_non_negative_seconds(self.cached_content_refresh_margin_seconds)
        if refresh_margin is not None:
            payload["refresh_margin_seconds"] = refresh_margin
        return payload

    @staticmethod
    def _append_cached_content_eviction_reason(
        lifecycle: dict[str, Any],
        reason: str,
    ) -> None:
        normalized = str(reason or "evicted").strip() or "evicted"
        reasons = lifecycle.setdefault("eviction_reasons", [])
        if isinstance(reasons, list) and normalized not in reasons:
            reasons.append(normalized)

    def _cached_content_entry_timing_metadata(
        self,
        entry: _GeminiCachedContentEntry,
        now: float,
    ) -> dict[str, int]:
        metadata: dict[str, int] = {
            "cache_age_seconds": _rounded_non_negative_seconds(now - entry.created_at) or 0,
            "cached_content_estimated_tokens": max(0, int(entry.estimated_tokens)),
        }
        if entry.expires_at is not None:
            expires_in = _rounded_non_negative_seconds(entry.expires_at - now)
            metadata["expires_in_seconds"] = expires_in or 0
        if entry.refresh_after is not None:
            refresh_in = _rounded_non_negative_seconds(entry.refresh_after - now)
            metadata["refresh_in_seconds"] = refresh_in or 0
        return metadata

    @staticmethod
    def _cached_content_refresh_reason(
        entry: _GeminiCachedContentEntry,
        now: float,
    ) -> str:
        if entry.expires_at is not None and now >= entry.expires_at:
            return "expired"
        if entry.refresh_after is not None and now >= entry.refresh_after:
            return "ttl_refresh_due"
        return ""

    def _evict_cached_content_entry(
        self,
        *,
        client: httpx.Client,
        signature: str,
        reason: str,
        lifecycle: dict[str, Any],
        cancellation_token: Any | None = None,
    ) -> None:
        entry = self._cached_content_by_signature.pop(signature, None)
        if entry is None:
            return
        lifecycle["evicted_entry_count"] = int(lifecycle.get("evicted_entry_count") or 0) + 1
        self._append_cached_content_eviction_reason(lifecycle, reason)
        self._delete_cached_content_entry(
            client=client,
            entry=entry,
            lifecycle=lifecycle,
            cancellation_token=cancellation_token,
        )
        lifecycle["entry_count"] = len(self._cached_content_by_signature)

    def _delete_cached_content_entry(
        self,
        *,
        client: httpx.Client,
        entry: _GeminiCachedContentEntry,
        lifecycle: dict[str, Any],
        cancellation_token: Any | None = None,
    ) -> None:
        lifecycle["delete_attempt_count"] = int(lifecycle.get("delete_attempt_count") or 0) + 1
        try:
            response = cancellable_httpx_send(
                client=client,
                cancellation_token=cancellation_token,
                method="DELETE",
                url=_cached_content_resource_url(self.base_url, entry.name),
                headers=self._headers(),
            )
        except Exception:
            raise_if_cancelled(cancellation_token)
            lifecycle["delete_failure_count"] = int(lifecycle.get("delete_failure_count") or 0) + 1
            lifecycle["delete_status"] = "delete_failed"
            return
        if response.status_code < 400:
            lifecycle["delete_success_count"] = int(lifecycle.get("delete_success_count") or 0) + 1
            lifecycle["delete_status"] = "deleted"
            return
        if response.status_code in {404, 410}:
            lifecycle["delete_success_count"] = int(lifecycle.get("delete_success_count") or 0) + 1
            lifecycle["delete_status"] = "already_absent"
            return
        lifecycle["delete_failure_count"] = int(lifecycle.get("delete_failure_count") or 0) + 1
        lifecycle["delete_status"] = "delete_rejected"

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
        on_reasoning_delta = reasoning_trace_callback(
            on_reasoning_delta,
            capability=getattr(self, "reasoning_trace_capability", None),
            stream=stream,
        )
        if self.prompt_cache_key or self.prompt_cache_retention:
            raise LLMError("Gemini GenerateContent does not support prompt_cache_key settings")

        messages = gate_messages_for_provider_route(messages, self.route_identity)
        system_instruction, contents = _gemini_contents_from_messages(messages)
        tool_mapping = _gemini_tools(
            tools,
            mode=self.web_search_mode,
            adapter=self.web_search_adapter,
        )
        temperature_omit_reason = documented_temperature_omit_reason(self.model)
        generation_config: dict[str, Any] = {}
        if temperature_omit_reason is None:
            generation_config["temperature"] = (
                self.temperature if temperature is None else float(temperature)
            )
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = int(max_tokens)
        text_config = _gemini_response_format(response_format)
        generation_config.update(text_config)
        thinking_config = _gemini_thinking_config(
            model=self.model,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self.reasoning_effort,
            thinking_level=self.thinking_level,
            thinking_budget=self.thinking_budget,
        )
        if on_reasoning_delta is not None and self._thought_summaries_supported is not False:
            thinking_config["includeThoughts"] = True
        if thinking_config:
            generation_config["thinkingConfig"] = thinking_config

        explicit_cached_content_requested = self.explicit_cached_content_enabled
        explicit_cached_content_disabled_reason = (
            "tools_or_tool_config_present"
            if explicit_cached_content_requested and tool_mapping.tools
            else ""
        )
        explicit_cached_content_active = (
            explicit_cached_content_requested and not explicit_cached_content_disabled_reason
        )
        cache_plan = (
            _explicit_cached_content_plan(
                model=self.model,
                system_instruction=system_instruction,
                contents=contents,
                ttl=self.cached_content_ttl,
                min_tokens=self.cached_content_min_tokens,
            )
            if explicit_cached_content_active
            else None
        )
        cache_metadata: dict[str, Any] | None = None
        if explicit_cached_content_requested:
            cache_metadata = {
                "strategy": "gemini_explicit_cached_content",
                "enabled": True,
                "ttl": self.cached_content_ttl,
                "min_tokens": self.cached_content_min_tokens,
                "eligible": cache_plan is not None,
            }
            if explicit_cached_content_disabled_reason:
                cache_metadata["disabled_fields"] = ["cached_content"]
                cache_metadata["fallback"] = "full_payload"
                cache_metadata["warnings"] = [
                    f"gemini_explicit_cached_content_skipped_for_"
                    f"{explicit_cached_content_disabled_reason}"
                ]
            if cache_plan is not None:
                cache_metadata["cacheable_prefix_estimated_tokens"] = cache_plan.estimated_tokens
                cache_metadata["cacheable_prefix_hash"] = cache_plan.signature[:16]
        cache_metadata = merge_cache_policy_metadata(
            self.prompt_cache_policy_metadata,
            cache_metadata,
        )
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
                    "gemini_explicit_cached_content"
                    if explicit_cached_content_requested
                    else "none"
                ),
                mode="automatic" if explicit_cached_content_requested else "manual",
            ),
        )

        def _build_payload(
            *,
            request_contents: list[dict[str, Any]],
            cached_content_name: str | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "contents": request_contents,
                "generationConfig": generation_config,
            }
            if cached_content_name:
                payload["cachedContent"] = cached_content_name
            elif system_instruction is not None:
                payload["systemInstruction"] = system_instruction
            if tool_mapping.tools:
                payload["tools"] = tool_mapping.tools
                mapped_tool_choice = _gemini_tool_choice(
                    tool_choice,
                    removed_alysis_web_search=tool_mapping.removed_alysis_web_search,
                    added_google_search=tool_mapping.added_google_search,
                )
                tool_config = dict(mapped_tool_choice or {})
                if tool_mapping.include_server_side_tool_invocations:
                    tool_config[_INCLUDE_SERVER_SIDE_TOOL_INVOCATIONS] = True
                if tool_config:
                    payload["toolConfig"] = tool_config
            elif tool_choice is not None:
                raise LLMError(
                    "Gemini GenerateContent tool_choice requires at least one available tool"
                )
            return payload

        def _prompt_estimation_payload(payload: dict[str, Any]) -> dict[str, Any]:
            estimation_payload: dict[str, Any] = {
                "contents": payload.get("contents", []),
            }
            for key in ("systemInstruction", "tools", "toolConfig", "cachedContent"):
                if key in payload:
                    estimation_payload[key] = payload[key]
            return estimation_payload

        payload = _build_payload(request_contents=contents)
        full_input_estimate_tokens = estimate_provider_payload_tokens(
            _prompt_estimation_payload(payload)
        )

        def _token_reconciliation_metadata(
            current_payload: dict[str, Any],
            *,
            input_mode: str,
        ) -> dict[str, Any]:
            sent_input_estimate_tokens = estimate_provider_payload_tokens(
                _prompt_estimation_payload(current_payload)
            )
            return {
                "input_estimate_tokens": full_input_estimate_tokens,
                "sent_input_estimate_tokens": sent_input_estimate_tokens,
                "estimator": "cl100k_base",
                "estimate_basis": "provider_prompt_payload",
                "input_mode": input_mode,
            }

        def _request_shape_metadata(
            current_payload: dict[str, Any],
            *,
            input_mode: str,
        ) -> dict[str, Any]:
            return build_request_shape_report(
                messages=messages,
                tools=tools,
                cache_policy=cache_metadata,
                provider_payload=_prompt_estimation_payload(current_payload),
                input_mode=input_mode,
            )

        def _request_plan_metadata(
            current_payload: dict[str, Any],
            *,
            input_mode: str,
            fallback_used: bool = False,
        ) -> dict[str, Any]:
            extra: dict[str, Any] = {"fallback_used": fallback_used}
            if temperature_omit_reason is not None:
                extra.update(
                    {
                        "temperature_omitted": True,
                        "temperature_omit_reason": temperature_omit_reason,
                    }
                )
            return layout_plan.request_plan_metadata(
                input_mode=input_mode,
                continuation_strategy="full_replay",
                provider_payload=_prompt_estimation_payload(payload),
                sent_provider_payload=_prompt_estimation_payload(current_payload),
                cache_policy_metadata=cache_metadata,
                extra=extra,
            )

        request_plan_metadata = _request_plan_metadata(payload, input_mode="full")

        provider_key = self.provider_key or best_effort_provider_key(
            base_url=self.base_url,
            model=self.model,
        )
        telemetry = ProviderCallTelemetryRecorder(
            provider_key=provider_key,
            protocol="gemini_generate_content",
            model=self.model,
            base_url=self.base_url,
            stream=stream,
            tools=tools,
            web_search_mode=self.web_search_mode,
            web_search_adapter=self.web_search_adapter,
            native_web_search=tool_mapping.added_google_search,
            cache_policy=cache_metadata,
            request_plan=request_plan_metadata,
            request_shape=_request_shape_metadata(
                payload,
                input_mode="full",
            ),
            token_reconciliation=_token_reconciliation_metadata(
                payload,
                input_mode="full",
            ),
            operation="gemini_generate_content_chat",
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

        def _send_request() -> LLMResponse:
            encoded_model = quote(self.model, safe="")
            operation = "streamGenerateContent?alt=sse" if stream else "generateContent"
            url = f"{self.base_url}/models/{encoded_model}:{operation}"
            try:
                client_context = (
                    cancellation_aware_httpx_client(
                        timeout_s=self.timeout_s,
                        transport=self._transport,
                        cancellation_token=cancellation_token,
                    )
                    if cancellation_token is not None and not stream
                    else provider_httpx_client(timeout=self.timeout_s, transport=self._transport)
                )
                with client_context as client:
                    request_payload = payload
                    active_request_plan_metadata = request_plan_metadata
                    cached_content_name: str | None = None
                    cache_lifecycle_metadata: dict[str, Any] = {}
                    cache_creation_input_tokens: int | None = None
                    stale_cached_content_retry_used = False
                    thought_summary_fallback_used = False

                    def _consume_pending_cache_creation_tokens() -> None:
                        # Called only once a response parsed successfully: the
                        # creation spend is attached to this response's usage,
                        # so retries reusing the entry must not report it again.
                        if cache_plan is None or cache_creation_input_tokens is None:
                            return
                        entry = self._cached_content_by_signature.get(cache_plan.signature)
                        if entry is not None and entry.pending_creation_tokens is not None:
                            self._cached_content_by_signature[cache_plan.signature] = replace(
                                entry,
                                pending_creation_tokens=None,
                            )

                    if cache_plan is not None:
                        (
                            cached_content_name,
                            cache_status,
                            cache_lifecycle_metadata,
                        ) = self._resolve_cached_content(
                            client=client,
                            plan=cache_plan,
                            cancellation_token=cancellation_token,
                        )
                        raw_creation_tokens = cache_lifecycle_metadata.get(
                            "cache_creation_input_tokens"
                        )
                        if isinstance(raw_creation_tokens, int):
                            cache_creation_input_tokens = raw_creation_tokens
                        if cache_metadata is not None:
                            cache_metadata.update(cache_lifecycle_metadata)
                            cache_metadata["status"] = cache_status
                            cache_metadata["used"] = cached_content_name is not None
                            if cached_content_name is None:
                                cache_metadata["fallback"] = "full_payload"
                        if cached_content_name is not None:
                            request_payload = _build_payload(
                                request_contents=cache_plan.suffix_contents,
                                cached_content_name=cached_content_name,
                            )
                            active_request_plan_metadata = _request_plan_metadata(
                                request_payload,
                                input_mode="cached_content",
                            )
                            telemetry.set_request_plan(active_request_plan_metadata)
                            telemetry.set_request_shape(
                                _request_shape_metadata(
                                    request_payload,
                                    input_mode="cached_content",
                                )
                            )
                            telemetry.set_token_reconciliation(
                                _token_reconciliation_metadata(
                                    request_payload,
                                    input_mode="cached_content",
                                )
                            )
                    elif explicit_cached_content_requested and cache_metadata is not None:
                        cache_metadata["status"] = (
                            "disabled_for_request"
                            if explicit_cached_content_disabled_reason
                            else "not_eligible"
                        )
                        cache_metadata["used"] = False
                        if explicit_cached_content_disabled_reason:
                            cache_metadata["fallback"] = "full_payload"
                        telemetry.set_request_shape(
                            _request_shape_metadata(
                                request_payload,
                                input_mode=(
                                    "cache_disabled_for_request"
                                    if explicit_cached_content_disabled_reason
                                    else "not_eligible"
                                ),
                            )
                        )
                        active_request_plan_metadata = _request_plan_metadata(
                            request_payload,
                            input_mode=(
                                "cache_disabled_for_request"
                                if explicit_cached_content_disabled_reason
                                else "not_eligible"
                            ),
                            fallback_used=True,
                        )
                        telemetry.set_request_plan(active_request_plan_metadata)
                        telemetry.set_token_reconciliation(
                            _token_reconciliation_metadata(
                                request_payload,
                                input_mode=(
                                    "cache_disabled_for_request"
                                    if explicit_cached_content_disabled_reason
                                    else "not_eligible"
                                ),
                            )
                        )
                    if cache_plan is not None and cached_content_name is None:
                        active_request_plan_metadata = _request_plan_metadata(
                            request_payload,
                            input_mode="full",
                            fallback_used=True,
                        )
                        telemetry.set_request_plan(active_request_plan_metadata)
                        telemetry.set_request_shape(
                            _request_shape_metadata(
                                request_payload,
                                input_mode="full",
                            )
                        )
                    telemetry.set_cache_policy(cache_metadata)
                    while True:
                        if stream:
                            with cancellable_httpx_request(
                                client=client,
                                cancellation_token=cancellation_token,
                                method="POST",
                                url=url,
                                stream=True,
                                headers=self._headers(),
                                json=request_payload,
                            ) as response:
                                if response.status_code >= 400:
                                    response.read()
                                    if (
                                        not thought_summary_fallback_used
                                        and _gemini_thought_summary_request_rejected(response)
                                        and _remove_gemini_thought_summary_request(request_payload)
                                    ):
                                        thought_summary_fallback_used = True
                                        self._thought_summaries_supported = False
                                        _remove_gemini_thought_summary_request(payload)
                                        active_request_plan_metadata = _request_plan_metadata(
                                            request_payload,
                                            input_mode="retry_without_thought_summaries",
                                            fallback_used=True,
                                        )
                                        telemetry.set_request_plan(active_request_plan_metadata)
                                        telemetry.set_request_shape(
                                            _request_shape_metadata(
                                                request_payload,
                                                input_mode="retry_without_thought_summaries",
                                            )
                                        )
                                        telemetry.set_token_reconciliation(
                                            _token_reconciliation_metadata(
                                                request_payload,
                                                input_mode="retry_without_thought_summaries",
                                            )
                                        )
                                        continue
                                    stale_reason = (
                                        _gemini_cached_content_rejection_reason(response)
                                        if cached_content_name is not None
                                        and not stale_cached_content_retry_used
                                        else None
                                    )
                                    if stale_reason is not None and cache_plan is not None:
                                        stale_cached_content_retry_used = True
                                        self._clear_cached_content_entry(
                                            cache_plan,
                                            client=client,
                                            reason=stale_reason,
                                            lifecycle=cache_lifecycle_metadata,
                                            cancellation_token=cancellation_token,
                                        )
                                        if cache_metadata is not None:
                                            cache_metadata.update(cache_lifecycle_metadata)
                                            cache_metadata["status"] = "stale_retry"
                                            cache_metadata["used"] = False
                                            cache_metadata["fallback"] = "full_payload"
                                        telemetry.set_cache_policy(cache_metadata)
                                        cached_content_name = None
                                        request_payload = copy.deepcopy(payload)
                                        if self._thought_summaries_supported is False:
                                            _remove_gemini_thought_summary_request(request_payload)
                                        active_request_plan_metadata = _request_plan_metadata(
                                            request_payload,
                                            input_mode=("full_retry_after_cached_content_rejected"),
                                            fallback_used=True,
                                        )
                                        telemetry.set_request_plan(active_request_plan_metadata)
                                        telemetry.set_request_shape(
                                            _request_shape_metadata(
                                                request_payload,
                                                input_mode=(
                                                    "full_retry_after_cached_content_rejected"
                                                ),
                                            )
                                        )
                                        telemetry.set_token_reconciliation(
                                            _token_reconciliation_metadata(
                                                request_payload,
                                                input_mode="full_retry_after_cached_content_rejected",
                                            )
                                        )
                                        continue
                                    raise self._llm_error_from_response(response)
                                parsed_response = self._parse_stream_response(
                                    response,
                                    on_text_delta=(
                                        _tracked_text_delta
                                        if telemetry_on_text_delta is not None
                                        else None
                                    ),
                                    on_reasoning_delta=(
                                        _tracked_reasoning_delta
                                        if telemetry_on_reasoning_delta is not None
                                        and _gemini_payload_requests_thought_summaries(
                                            request_payload
                                        )
                                        else None
                                    ),
                                    reasoning_is_summary=_gemini_payload_requests_thought_summaries(
                                        request_payload
                                    ),
                                )
                                _consume_pending_cache_creation_tokens()
                                return _response_with_cache_metadata(
                                    parsed_response,
                                    cache_metadata,
                                    active_request_plan_metadata,
                                    cache_creation_input_tokens=cache_creation_input_tokens,
                                )
                        response = cancellable_httpx_send(
                            client=client,
                            cancellation_token=cancellation_token,
                            method="POST",
                            url=url,
                            headers=self._headers(),
                            json=request_payload,
                        )
                        if response.status_code < 400:
                            break
                        if (
                            not thought_summary_fallback_used
                            and _gemini_thought_summary_request_rejected(response)
                            and _remove_gemini_thought_summary_request(request_payload)
                        ):
                            thought_summary_fallback_used = True
                            self._thought_summaries_supported = False
                            _remove_gemini_thought_summary_request(payload)
                            active_request_plan_metadata = _request_plan_metadata(
                                request_payload,
                                input_mode="retry_without_thought_summaries",
                                fallback_used=True,
                            )
                            telemetry.set_request_plan(active_request_plan_metadata)
                            telemetry.set_request_shape(
                                _request_shape_metadata(
                                    request_payload,
                                    input_mode="retry_without_thought_summaries",
                                )
                            )
                            telemetry.set_token_reconciliation(
                                _token_reconciliation_metadata(
                                    request_payload,
                                    input_mode="retry_without_thought_summaries",
                                )
                            )
                            continue
                        stale_reason = (
                            _gemini_cached_content_rejection_reason(response)
                            if cached_content_name is not None
                            and not stale_cached_content_retry_used
                            else None
                        )
                        if stale_reason is not None and cache_plan is not None:
                            stale_cached_content_retry_used = True
                            self._clear_cached_content_entry(
                                cache_plan,
                                client=client,
                                reason=stale_reason,
                                lifecycle=cache_lifecycle_metadata,
                                cancellation_token=cancellation_token,
                            )
                            if cache_metadata is not None:
                                cache_metadata.update(cache_lifecycle_metadata)
                                cache_metadata["status"] = "stale_retry"
                                cache_metadata["used"] = False
                                cache_metadata["fallback"] = "full_payload"
                            telemetry.set_cache_policy(cache_metadata)
                            cached_content_name = None
                            request_payload = copy.deepcopy(payload)
                            if self._thought_summaries_supported is False:
                                _remove_gemini_thought_summary_request(request_payload)
                            active_request_plan_metadata = _request_plan_metadata(
                                request_payload,
                                input_mode="full_retry_after_cached_content_rejected",
                                fallback_used=True,
                            )
                            telemetry.set_request_plan(active_request_plan_metadata)
                            telemetry.set_request_shape(
                                _request_shape_metadata(
                                    request_payload,
                                    input_mode="full_retry_after_cached_content_rejected",
                                )
                            )
                            telemetry.set_token_reconciliation(
                                _token_reconciliation_metadata(
                                    request_payload,
                                    input_mode="full_retry_after_cached_content_rejected",
                                )
                            )
                            continue
                        break
            except CooperativeCancellationError:
                raise
            except httpx.DecodingError as e:
                raise_if_cancelled(cancellation_token)
                err = LLMError(
                    "Gemini GenerateContent decompression failed: "
                    f"{sanitize_error_text_for_output(e)}"
                )
                if stream and public_output_emitted:
                    mark_provider_call_non_retryable(err)
                raise err from e
            except Exception as e:  # noqa: BLE001
                raise_if_cancelled(cancellation_token)
                if isinstance(e, LLMError):
                    if stream and public_output_emitted:
                        mark_provider_call_non_retryable(e)
                    raise
                err = LLMError(
                    f"Gemini GenerateContent request failed: {sanitize_error_text_for_output(e)}"
                )
                if stream and public_output_emitted:
                    mark_provider_call_non_retryable(err)
                raise err from e
            if response.status_code >= 400:
                raise self._llm_error_from_response(response)
            parsed_response = self._parse_chat_response(
                response,
                on_reasoning_delta=(
                    telemetry_on_reasoning_delta
                    if _gemini_payload_requests_thought_summaries(request_payload)
                    else None
                ),
                reasoning_is_summary=_gemini_payload_requests_thought_summaries(request_payload),
            )
            _consume_pending_cache_creation_tokens()
            return _response_with_cache_metadata(
                parsed_response,
                cache_metadata,
                active_request_plan_metadata,
                cache_creation_input_tokens=cache_creation_input_tokens,
            )

        return stamp_response_for_route(
            telemetry.run(
                lambda: run_provider_limited_call(
                    call=_send_request,
                    provider_key=provider_key,
                    provider_concurrency_caps=self.provider_concurrency_caps,
                    retry_settings=self.provider_retry_settings,
                    operation="gemini_generate_content_chat",
                    sleep_fn=self._provider_sleep_fn,
                    random_fn=self._provider_random_fn,
                    on_retry=telemetry.on_retry,
                    on_retry_event=getattr(
                        self,
                        "_provider_retry_event_observer",
                        None,
                    ),
                    retry_deadline_allows=getattr(self, "_provider_retry_deadline_allows", None),
                    cancellation_token=cancellation_token,
                )
            ),
            self.route_identity,
        )

    @staticmethod
    def _parse_stream_response(
        response: httpx.Response,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None,
        reasoning_is_summary: bool = False,
    ) -> LLMResponse:
        accumulator = _GeminiStreamAccumulator(
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta if reasoning_is_summary else None,
        )
        for frame in iter_sse_frames(response.iter_lines()):
            raw_event = parse_sse_json_frame(frame, stream_name="Gemini GenerateContent stream")
            if not isinstance(raw_event, dict):
                raise LLMError("Gemini GenerateContent stream emitted non-object JSON event")
            accumulator.handle(frame, raw_event)
        data = accumulator.finish()
        return GeminiGenerateContentClient._parse_chat_response(
            _response_from_json(data),
            reasoning_is_summary=reasoning_is_summary,
        )

    @staticmethod
    def _parse_chat_response(
        response: httpx.Response,
        *,
        on_reasoning_delta: Callable[[str], None] | None = None,
        reasoning_is_summary: bool = False,
    ) -> LLMResponse:
        try:
            data = response.json()
        except Exception as e:  # noqa: BLE001
            raise LLMError("Gemini GenerateContent returned non-JSON response") from e
        if not isinstance(data, dict):
            raise LLMError("Unexpected Gemini GenerateContent payload: expected JSON object")
        candidate = _candidate(data)
        if candidate is None:
            raise LLMError("Unexpected Gemini GenerateContent payload: missing candidates")
        content = _candidate_content(candidate)
        if content is None:
            finish_reason = str(candidate.get("finishReason") or "").strip()
            suffix = f" (finish_reason={finish_reason})" if finish_reason else ""
            raise LLMError(f"Gemini GenerateContent returned no candidate content{suffix}")
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise LLMError("Unexpected Gemini GenerateContent payload: missing content parts")

        text = _extract_text(parts)
        response_scope = str(data.get("responseId") or "").strip() or secrets.token_hex(16)
        tool_calls = _parse_tool_calls(parts, fallback_scope=response_scope)
        if not text and not tool_calls:
            finish_reason = str(candidate.get("finishReason") or "").strip()
            suffix = f" (finish_reason={finish_reason})" if finish_reason else ""
            raise LLMError(
                f"Gemini GenerateContent returned no assistant text or tool calls{suffix}"
            )
        parsed_response = LLMResponse(
            content=text,
            tool_calls=tool_calls,
            raw=data,
            response_model=data.get("modelVersion")
            if isinstance(data.get("modelVersion"), str)
            else None,
            usage=_parse_usage(data.get("usageMetadata")),
            provider_metadata=_gemini_provider_metadata(data, candidate),
            reasoning=(_reasoning_outputs_from_parts(parts) if reasoning_is_summary else ()),
        )
        if reasoning_is_summary:
            _emit_reasoning_parts(parts, on_reasoning_delta)
        return parsed_response


def _gemini_response_format(response_format: dict[str, Any] | None) -> dict[str, Any]:
    if not response_format:
        return {}
    response_type = str(response_format.get("type") or "").strip()
    if response_type == "json_object":
        return {"responseMimeType": "application/json"}
    if response_type == "json_schema":
        raw_json_schema = response_format.get("json_schema")
        json_schema = raw_json_schema if isinstance(raw_json_schema, dict) else response_format
        schema = json_schema.get("schema")
        if not isinstance(schema, dict):
            raise LLMError(
                "Gemini GenerateContent json_schema response_format requires schema object"
            )
        return {
            "responseMimeType": "application/json",
            # responseSchema is the same OpenAPI-subset ``Schema`` message as
            # function parameters, so it needs the same projection.
            "responseSchema": _gemini_schema(schema),
        }
    if response_type == "text":
        return {}
    raise LLMError(
        f"Gemini GenerateContent does not support response_format type {response_type!r}"
    )


def _thinking_budget(reasoning_effort: str) -> int:
    effort = str(reasoning_effort or "").strip().lower()
    if effort in {"none", "minimal"}:
        return 0
    if effort == "low":
        return 1024
    if effort == "medium":
        return 4096
    if effort == "high":
        return 8192
    if effort == "xhigh":
        return 16384
    raise LLMError(f"Gemini GenerateContent reasoning_effort is not supported: {effort}")


def _is_gemini_3_model(model: str | None) -> bool:
    return "gemini-3" in str(model or "").strip().lower()


def _thinking_level_from_reasoning_effort(
    *,
    model: str,
    reasoning_effort: str,
) -> str | None:
    effort = str(reasoning_effort or "").strip().lower()
    contract = reasoning_contract_for("gemini", model)
    if contract.wire != WIRE_THINKING_LEVEL:
        return None
    if contract.allows_value(effort):
        return effort
    return contract.default or None


def _gemini_thinking_config(
    *,
    model: str,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
    thinking_level: str | None,
    thinking_budget: int | None,
) -> dict[str, Any]:
    if thinking_level is not None and thinking_budget is not None:
        raise LLMError("Gemini GenerateContent cannot set both thinking_level and thinking_budget")
    contract = reasoning_contract_for("gemini", model)
    if thinking_level is not None and contract.wire != WIRE_THINKING_LEVEL:
        raise LLMError("Gemini GenerateContent thinking_level requires a Gemini 3 model")
    if thinking_level is not None:
        normalized_level = str(thinking_level or "").strip().lower()
        if not contract.allows_value(normalized_level):
            allowed = ", ".join(contract.values)
            raise LLMError(f"Gemini GenerateContent thinking_level must be one of: {allowed}")
        return {"thinkingLevel": normalized_level}
    if thinking_budget is not None:
        return {"thinkingBudget": int(thinking_budget)}
    if reasoning_effort:
        if _is_gemini_3_model(model):
            level = _thinking_level_from_reasoning_effort(
                model=model,
                reasoning_effort=reasoning_effort,
            )
            return {"thinkingLevel": level} if level else {}
        return {"thinkingBudget": _thinking_budget(reasoning_effort)}
    if enable_thinking is False:
        if _is_gemini_3_model(model):
            return {"thinkingLevel": contract.default} if contract.default else {}
        return {"thinkingBudget": 0}
    return {}
