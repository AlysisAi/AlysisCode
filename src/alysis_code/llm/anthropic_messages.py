from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..cancellation import CooperativeCancellationError
from ..error_text import sanitize_error_text_for_output
from ..provider_telemetry import ProviderCallTelemetryRecorder
from ..request_estimation import estimate_provider_payload_tokens
from ..web_search_adapters import ANTHROPIC_MESSAGES_ADAPTER, AUTO_WEB_SEARCH_ADAPTER
from .base import omitted_reasoning_display_required, serial_tool_calls_required
from .cache_capabilities import CACHE_CONTROL_FIELD
from .cache_control_blocks import (
    count_cache_control_blocks,
    count_explicit_cache_control_blocks,
    explicit_cache_control_payloads,
    strip_cache_control_blocks,
)
from .cache_policy import merge_cache_policy_metadata
from .cancellable_http import cancellation_aware_httpx_client, provider_httpx_client
from .http_cancellation import (
    cancellable_httpx_request,
    cancellable_httpx_send,
    raise_if_cancelled,
)
from .metadata import (
    ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
    PROVIDER_METADATA_KEY,
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
from .request_plan import LLMRequestPlan, RequestCachePlan, WireRequestDiagnostics
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

_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MESSAGES_ROUTE_REVISION = _DEFAULT_ANTHROPIC_VERSION
_DEFAULT_ACCEPT_ENCODING = "identity"
_ANTHROPIC_METADATA_KEY = ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY
_ALYSIS_WEB_SEARCH_FUNCTION_NAME = "web_search"
_ANTHROPIC_WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
_ANTHROPIC_WEB_SEARCH_TOOL_TYPES = frozenset(
    {
        "web_search_20250305",
        "web_search_20260209",
    }
)
_WEB_SEARCH_MODES_ALLOWING_ANTHROPIC_BUILTIN = frozenset({"auto", "native"})
_ANTHROPIC_CACHE_CONTROL_TTLS = frozenset({"5m", "1h"})
_ANTHROPIC_MAX_CACHE_CONTROL_BREAKPOINTS = 4
_ANTHROPIC_MIN_MANUAL_THINKING_BUDGET = 1024
_ANTHROPIC_MANUAL_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32768,
}
_ANTHROPIC_API_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_CLAUDE_MODEL_VERSION_RE = re.compile(
    r"(?:^|[/.:_-])claude[-_.](?P<family>opus|sonnet|haiku|fable|mythos)"
    r"[-_.](?P<major>\d+)(?:[-_.](?P<minor>\d+))?"
)


@dataclass(frozen=True)
class _ClaudeModelVersion:
    family: str
    major: int
    minor: int | None


@dataclass(frozen=True)
class _AnthropicThinkingPlan:
    config: dict[str, Any] | None
    output_effort: str | None
    active: bool


def _claude_model_version(model: str) -> _ClaudeModelVersion | None:
    normalized = str(model or "").strip().casefold()
    match = _CLAUDE_MODEL_VERSION_RE.search(normalized)
    if match is None:
        return None
    minor_text = match.group("minor")
    minor = int(minor_text) if minor_text is not None else None
    # Dated snapshots such as claude-opus-4-20250514 are Claude 4.0, not
    # version 4.20-million. Keep the same distinction as temperature_compat.
    if minor is not None and minor >= 100:
        minor = None
    return _ClaudeModelVersion(
        family=match.group("family"),
        major=int(match.group("major")),
        minor=minor,
    )


def _uses_adaptive_thinking(model: str) -> bool:
    normalized = str(model or "").strip().casefold()
    if "claude-mythos-preview" in normalized:
        return True
    version = _claude_model_version(model)
    if version is None:
        return False
    if version.family in {"fable", "mythos"}:
        return version.major >= 5
    if version.family == "haiku":
        return version.major == 5 and version.minor == 5
    if version.family not in {"opus", "sonnet"}:
        return False
    return version.major >= 5 or (
        version.major == 4 and version.minor is not None and version.minor >= 6
    )


def _supports_disabled_thinking(model: str) -> bool:
    normalized = str(model or "").strip().casefold()
    if "claude-mythos-preview" in normalized:
        return False
    version = _claude_model_version(model)
    if version is None:
        return True
    if version.family == "opus" and version.major == 5 and version.minor == 5:
        return False
    return not (version.family in {"fable", "mythos"} and version.major >= 5)


def _thinking_enabled_by_default(model: str) -> bool:
    """Models whose documented default already includes adaptive thinking.

    Opus joined the 5-generation families here: on Opus 5 an omitted ``thinking``
    field runs adaptive, where on Opus 4.8/4.7 the same request did not think at
    all. Reporting that honestly matters for output budgeting — ``max_tokens``
    caps thinking and answer text together.
    """

    normalized = str(model or "").strip().casefold()
    if "claude-mythos-preview" in normalized:
        return True
    version = _claude_model_version(model)
    if version is None or version.major < 5:
        return False
    return version.family in {
        "fable",
        "mythos",
        "opus",
        "sonnet",
    } or version == _ClaudeModelVersion("haiku", 5, 5)


def _supports_output_effort(model: str) -> bool:
    normalized = str(model or "").strip().casefold()
    if "claude-mythos-preview" in normalized:
        return True
    version = _claude_model_version(model)
    if version is None:
        return False
    if version == _ClaudeModelVersion("haiku", 5, 5):
        return True
    if version.family in {"fable", "mythos"}:
        return version.major >= 5
    if version.family == "opus":
        return version.major >= 5 or (
            version.major == 4 and version.minor is not None and version.minor >= 5
        )
    if version.family == "sonnet":
        return version.major >= 5 or (
            version.major == 4 and version.minor is not None and version.minor >= 6
        )
    return False


def _supports_xhigh_effort(model: str) -> bool:
    version = _claude_model_version(model)
    if version is None:
        return False
    if version == _ClaudeModelVersion("haiku", 5, 5):
        return True
    if version.family in {"fable", "mythos"}:
        return version.major >= 5
    if version.family == "opus":
        return version.major >= 5 or (
            version.major == 4 and version.minor is not None and version.minor >= 7
        )
    return version.family == "sonnet" and version.major >= 5


def _supports_max_effort(model: str) -> bool:
    normalized = str(model or "").strip().casefold()
    return "claude-mythos-preview" in normalized or _uses_adaptive_thinking(model)


def _manual_thinking_budget(*, effort: str | None, max_tokens: int) -> int:
    if max_tokens <= _ANTHROPIC_MIN_MANUAL_THINKING_BUDGET:
        raise LLMError("Anthropic manual thinking requires max_tokens greater than 1024")
    requested = _ANTHROPIC_MANUAL_THINKING_BUDGETS.get(effort or "high", 8192)
    # Preserve useful answer headroom where possible while satisfying the API's
    # strict budget_tokens < max_tokens constraint for smaller output limits.
    reserve = 1024 if max_tokens >= 2048 else 1
    return max(
        _ANTHROPIC_MIN_MANUAL_THINKING_BUDGET,
        min(requested, max_tokens - reserve),
    )


def _anthropic_thinking_plan(
    *,
    model: str,
    enable_thinking: bool | None,
    reasoning_effort: str | None,
    max_tokens: int,
    request_summary: bool,
    force_omitted_display: bool = False,
) -> _AnthropicThinkingPlan:
    effort = str(reasoning_effort or "").strip().casefold() or None
    if effort == "ultra":
        raise LLMError(
            "Anthropic Messages does not support reasoning_effort='ultra'; use xhigh or max"
        )
    if effort not in {None, "none", *_ANTHROPIC_MANUAL_THINKING_BUDGETS}:
        raise LLMError(f"Anthropic Messages reasoning_effort is not supported: {effort}")

    if enable_thinking is False or effort == "none":
        if _claude_model_version(model) == _ClaudeModelVersion("sonnet", 5, 5):
            # Sonnet 5.5 replaces disabled with between_tools. This mode still
            # produces signed progress blocks and never accepts forced tools.
            return _AnthropicThinkingPlan(
                config={"type": "between_tools"}, output_effort="high", active=True
            )
        if not _supports_disabled_thinking(model):
            raise LLMError(f"Anthropic model {model!r} does not support disabling thinking")
        # output_effort stays None on purpose: Opus 5 accepts disabled thinking
        # only at effort 'high' or below, and rejects it outright at xhigh/max.
        # Emitting no effort leaves the server default (high), which is the
        # documented way to keep a thinking-off route legal. Do not pass the
        # caller's effort through here.
        return _AnthropicThinkingPlan(
            config={"type": "disabled"},
            output_effort=None,
            active=False,
        )

    explicitly_active = enable_thinking is True or effort is not None
    if not explicitly_active:
        # Auto/default is intentionally provider-owned. Trace visibility must
        # never switch model reasoning on by itself. For models that already
        # think by default, requesting ``display=summarized`` changes visibility
        # only; the provider still owns effort and whether a simple turn thinks.
        default_active = _thinking_enabled_by_default(model)
        display_config = None
        if default_active and force_omitted_display:
            display_config = {"type": "adaptive", "display": "omitted"}
        elif default_active and request_summary:
            display_config = {"type": "adaptive", "display": "summarized"}
        return _AnthropicThinkingPlan(
            config=display_config,
            output_effort=None,
            active=default_active,
        )

    version = _claude_model_version(model)
    if version is None and "claude-mythos-preview" not in str(model).casefold():
        raise LLMError(
            f"Anthropic Messages cannot safely select a thinking mode for model {model!r}"
        )
    if version is not None and version.major < 4:
        raise LLMError(f"Anthropic model {model!r} does not support extended thinking")

    display = "summarized" if request_summary and not force_omitted_display else "omitted"
    if _uses_adaptive_thinking(model):
        config: dict[str, Any] = {"type": "adaptive", "display": display}
    else:
        config = {
            "type": "enabled",
            "budget_tokens": _manual_thinking_budget(
                effort=effort,
                max_tokens=max_tokens,
            ),
            "display": display,
        }

    output_effort: str | None = None
    if effort is not None and _supports_output_effort(model):
        if effort not in _ANTHROPIC_API_EFFORTS:
            raise LLMError(f"Anthropic Messages reasoning_effort is not supported: {effort}")
        if effort == "xhigh" and not _supports_xhigh_effort(model):
            raise LLMError(f"Anthropic model {model!r} does not support xhigh effort")
        if effort == "max" and not _supports_max_effort(model):
            raise LLMError(f"Anthropic model {model!r} does not support max effort")
        output_effort = effort
    return _AnthropicThinkingPlan(
        config=config,
        output_effort=output_effort,
        active=True,
    )


def _headers_with_default_accept_encoding(headers: dict[str, str]) -> dict[str, str]:
    request_headers = dict(headers)
    if not any(key.lower() == "accept-encoding" for key in request_headers):
        request_headers["accept-encoding"] = _DEFAULT_ACCEPT_ENCODING
    return request_headers


def _non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_prompt_cache_control_ttl(value: str | None) -> str:
    normalized = str(value or "").strip().lower() or "5m"
    return normalized if normalized in _ANTHROPIC_CACHE_CONTROL_TTLS else "5m"


def _response_with_cache_policy_metadata(
    response: LLMResponse,
    cache_policy: dict[str, Any] | None,
    request_plan_metadata: dict[str, Any] | None = None,
) -> LLMResponse:
    if not cache_policy and not request_plan_metadata:
        return response
    provider_metadata = copy.deepcopy(response.provider_metadata) or {}
    anthropic_metadata = provider_metadata.setdefault(_ANTHROPIC_METADATA_KEY, {})
    if isinstance(anthropic_metadata, dict):
        if cache_policy:
            anthropic_metadata["cache_policy"] = copy.deepcopy(cache_policy)
        if request_plan_metadata:
            anthropic_metadata["request_plan"] = copy.deepcopy(request_plan_metadata)
    return LLMResponse(
        content=response.content,
        tool_calls=response.tool_calls,
        raw=response.raw,
        response_model=response.response_model,
        usage=response.usage,
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


def _anthropic_blocks_from_content(raw: Any, *, role: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [{"type": "text", "text": raw}] if raw else []
    if not isinstance(raw, list):
        text = _content_to_text(raw)
        return [{"type": "text", "text": text}] if text else []

    blocks: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "").strip()
        text = item.get("text") or item.get("content")
        if block_type in {"text", "input_text", "output_text"} and isinstance(text, str):
            block = {"type": "text", "text": text}
            cache_control = item.get(CACHE_CONTROL_FIELD)
            if isinstance(cache_control, dict):
                block[CACHE_CONTROL_FIELD] = copy.deepcopy(cache_control)
            blocks.append(block)
            continue
        if role == "user" and block_type == "image_url":
            image_url = item.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "").strip()
            elif isinstance(image_url, str):
                url = image_url.strip()
            if url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            continue
        if block_type in {
            "text",
            "image",
            "tool_result",
            "tool_use",
            "server_tool_use",
            "web_search_tool_result",
        }:
            blocks.append(copy.deepcopy(item))
    return blocks


def _function_name_from_tool(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    if str(tool.get("type") or "") == "function":
        return str(tool.get("name") or "").strip()
    return ""


def _is_alysis_web_search_function(tool: dict[str, Any]) -> bool:
    return _function_name_from_tool(tool) == _ALYSIS_WEB_SEARCH_FUNCTION_NAME


def _is_anthropic_hosted_web_search_tool(tool: dict[str, Any]) -> bool:
    return str(tool.get("type") or "").strip() in _ANTHROPIC_WEB_SEARCH_TOOL_TYPES


def _anthropic_builtin_web_search_allowed(*, mode: str, adapter: str) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    normalized_adapter = str(adapter or "").strip().lower() or AUTO_WEB_SEARCH_ADAPTER
    if normalized_mode not in _WEB_SEARCH_MODES_ALLOWING_ANTHROPIC_BUILTIN:
        return False
    return normalized_adapter in {AUTO_WEB_SEARCH_ADAPTER, ANTHROPIC_MESSAGES_ADAPTER}


@dataclass(frozen=True)
class _AnthropicToolMapping:
    tools: list[dict[str, Any]]
    added_builtin_web_search: bool
    removed_alysis_web_search: bool


def _anthropic_tool_from_chat_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    tool_type = str(tool.get("type") or "").strip()
    if tool_type == "function":
        function = tool.get("function")
        source = function if isinstance(function, dict) else tool
        name = str(source.get("name") or "").strip()
        if not name:
            return None
        mapped: dict[str, Any] = {
            "name": name,
            "input_schema": copy.deepcopy(source.get("parameters") or {"type": "object"}),
        }
        description = str(source.get("description") or "").strip()
        if description:
            mapped["description"] = description
        return mapped
    if _is_anthropic_hosted_web_search_tool(tool):
        return copy.deepcopy(tool)
    raise LLMError(f"Anthropic Messages does not support tool type {tool_type!r}")


def _anthropic_tools(
    tools: list[dict[str, Any]] | None,
    *,
    mode: str,
    adapter: str,
    provider_key: str | None = None,
) -> _AnthropicToolMapping:
    normalized_mode = str(mode or "off").strip().lower()
    normalized_adapter = (
        str(adapter or AUTO_WEB_SEARCH_ADAPTER).strip().lower() or AUTO_WEB_SEARCH_ADAPTER
    )
    raw_tools = [tool for tool in tools or [] if isinstance(tool, dict)]
    if provider_key == "alysis":
        if normalized_mode == "native" or any(
            _is_anthropic_hosted_web_search_tool(tool) for tool in raw_tools
        ):
            raise LLMError(
                "Hosted Sonnet does not support built-in web search; use external search"
            )
        if normalized_mode == "auto":
            normalized_mode = "external"
    alysis_web_search_present = any(_is_alysis_web_search_function(tool) for tool in raw_tools)
    use_builtin_web_search = alysis_web_search_present and _anthropic_builtin_web_search_allowed(
        mode=normalized_mode,
        adapter=normalized_adapter,
    )
    if normalized_mode == "native" and alysis_web_search_present and not use_builtin_web_search:
        raise LLMError(
            "web_search_mode=native with protocol=anthropic_messages requires "
            "web_search_adapter='auto' or 'anthropic_messages' for Anthropic hosted web_search; "
            f"got {normalized_adapter!r}"
        )

    mapped_tools: list[dict[str, Any]] = []
    removed_alysis_web_search = False
    for tool in raw_tools:
        if _is_alysis_web_search_function(tool):
            if normalized_mode in {"off", "native"} or use_builtin_web_search:
                removed_alysis_web_search = True
                continue
        if _is_anthropic_hosted_web_search_tool(tool) and normalized_mode in {"off", "external"}:
            continue
        mapped = _anthropic_tool_from_chat_tool(tool)
        if mapped is not None:
            mapped_tools.append(mapped)

    if use_builtin_web_search and not any(
        _is_anthropic_hosted_web_search_tool(tool) for tool in mapped_tools
    ):
        mapped_tools.append(
            {
                "type": _ANTHROPIC_WEB_SEARCH_TOOL_TYPE,
                "name": _ALYSIS_WEB_SEARCH_FUNCTION_NAME,
                "max_uses": 5,
            }
        )
    return _AnthropicToolMapping(
        tools=mapped_tools,
        added_builtin_web_search=use_builtin_web_search,
        removed_alysis_web_search=removed_alysis_web_search,
    )


def _anthropic_tool_choice(
    tool_choice: Any,
    *,
    removed_alysis_web_search: bool,
    added_builtin_web_search: bool,
) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip()
        if normalized == "auto":
            return {"type": "auto"}
        if normalized == "none":
            return {"type": "none"}
        if normalized == "required":
            return {"type": "any"}
        raise LLMError(f"Anthropic Messages does not support tool_choice={tool_choice!r}")
    if not isinstance(tool_choice, dict):
        raise LLMError("Anthropic Messages tool_choice must be a string or object")

    choice_type = str(tool_choice.get("type") or "").strip()
    if choice_type == "function":
        if "name" in tool_choice:
            name = str(tool_choice.get("name") or "").strip()
        else:
            function = tool_choice.get("function")
            name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        if not name:
            raise LLMError("Anthropic Messages forced function tool_choice is missing name")
        if (
            name == _ALYSIS_WEB_SEARCH_FUNCTION_NAME
            and removed_alysis_web_search
            and not added_builtin_web_search
        ):
            raise LLMError(
                "Anthropic Messages removed the Alysis Code web_search function for the selected "
                "web_search_mode; do not force tool_choice to function web_search"
            )
        return {"type": "tool", "name": name}
    if choice_type in {"auto", "any", "none"}:
        return {"type": choice_type}
    if choice_type == "tool":
        name = str(tool_choice.get("name") or "").strip()
        if not name:
            raise LLMError("Anthropic Messages forced tool_choice is missing name")
        return {"type": "tool", "name": name}
    raise LLMError(f"Anthropic Messages does not support tool_choice type {choice_type!r}")


def _metadata_content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get(PROVIDER_METADATA_KEY)
    if not isinstance(metadata, dict):
        return []
    anthropic_metadata = metadata.get(_ANTHROPIC_METADATA_KEY)
    if not isinstance(anthropic_metadata, dict):
        return []
    blocks = anthropic_metadata.get("content_blocks")
    if not isinstance(blocks, list):
        return []
    copied: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict):
            copied.append(copy.deepcopy(block))
    return copied


def _tool_use_blocks_from_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return blocks
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue
        call_id = str(raw_tool_call.get("id") or raw_tool_call.get("call_id") or "").strip()
        function = raw_tool_call.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            arguments = _json_arguments(function.get("arguments"))
        else:
            name = str(raw_tool_call.get("name") or "").strip()
            arguments = _json_arguments(raw_tool_call.get("arguments"))
        if not name:
            continue
        if not call_id:
            call_id = f"toolu_{name}"
        blocks.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": arguments,
            }
        )
    return blocks


def _is_tool_result_user_message(message: dict[str, Any]) -> bool:
    if str(message.get("role") or "") != "user":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)
    )


def _anthropic_messages_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[str | list[dict[str, Any]] | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    system_blocks: list[dict[str, Any]] = []
    anthropic_messages: list[dict[str, Any]] = []
    trailing_system_instruction = False

    def _flush_system_parts() -> None:
        if not system_parts:
            return
        text = "\n\n".join(system_parts).strip()
        system_parts.clear()
        if text:
            system_blocks.append({"type": "text", "text": text})

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role not in {"system", "developer"}:
            trailing_system_instruction = False
        if role in {"system", "developer"}:
            content = message.get("content")
            if _content_to_text(content).strip():
                trailing_system_instruction = True
            if isinstance(content, list) and count_cache_control_blocks(content) > 0:
                _flush_system_parts()
                system_blocks.extend(_anthropic_blocks_from_content(content, role="system"))
                continue
            text = _content_to_text(content).strip()
            if text:
                if system_blocks:
                    system_blocks.append({"type": "text", "text": text})
                else:
                    system_parts.append(text)
            continue
        if role == "user":
            blocks = _anthropic_blocks_from_content(message.get("content"), role="user")
            if blocks:
                anthropic_messages.append({"role": "user", "content": blocks})
            continue
        if role == "assistant":
            metadata_blocks = _metadata_content_blocks(message)
            if metadata_blocks:
                anthropic_messages.append({"role": "assistant", "content": metadata_blocks})
                continue
            blocks = _anthropic_blocks_from_content(message.get("content"), role="assistant")
            blocks.extend(_tool_use_blocks_from_tool_calls(message))
            if blocks:
                anthropic_messages.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            tool_use_id = str(message.get("tool_call_id") or message.get("call_id") or "").strip()
            if not tool_use_id:
                raise LLMError("Anthropic Messages tool result is missing tool_call_id")
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": _content_to_text(message.get("content")),
            }
            if anthropic_messages and _is_tool_result_user_message(anthropic_messages[-1]):
                content = anthropic_messages[-1].setdefault("content", [])
                if isinstance(content, list):
                    content.append(tool_result_block)
                else:
                    anthropic_messages.append({"role": "user", "content": [tool_result_block]})
            else:
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": [tool_result_block],
                    }
                )
            continue
        raise LLMError(f"Anthropic Messages cannot send message role {role!r}")
    if (
        trailing_system_instruction
        and anthropic_messages
        and anthropic_messages[-1]["role"] == "assistant"
        and not any(block.get("type") == "tool_use" for block in anthropic_messages[-1]["content"])
    ):
        # Hoisting a controller nudge into `system` leaves the previous answer
        # at the end of `messages`, which current Claude models reject as an
        # assistant prefill. Start a fresh response without downgrading or
        # duplicating the actual controller instruction. This wire-only cue
        # never becomes a user turn in the persisted conversation.
        anthropic_messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": "Continue following the instructions above."}],
            }
        )
    if system_blocks:
        _flush_system_parts()
        system: str | list[dict[str, Any]] | None = system_blocks
    else:
        system = "\n\n".join(system_parts).strip() or None
    return system, anthropic_messages


def _anthropic_system_with_cache_control(
    system: str | list[dict[str, Any]] | None,
    *,
    cache_control: Mapping[str, Any] | None,
) -> tuple[str | list[dict[str, Any]] | None, bool]:
    if not system or not isinstance(cache_control, Mapping):
        return system, False
    if count_cache_control_blocks(system) > 0:
        return system, True
    cache_control_payload = copy.deepcopy(dict(cache_control))
    if isinstance(system, str):
        text = system.strip()
        if not text:
            return system, False
        return (
            [
                {
                    "type": "text",
                    "text": system,
                    CACHE_CONTROL_FIELD: cache_control_payload,
                }
            ],
            True,
        )
    copied: list[dict[str, Any]] = [copy.deepcopy(block) for block in system]
    for index in range(len(copied) - 1, -1, -1):
        block = copied[index]
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "text").strip().lower()
        text = block.get("text")
        if block_type != "text" or not isinstance(text, str) or not text.strip():
            continue
        block[CACHE_CONTROL_FIELD] = cache_control_payload
        return copied, True
    return system, False


def _cache_control_ttl(cache_control: Mapping[str, Any] | None) -> str:
    if not isinstance(cache_control, Mapping):
        return "5m"
    return _normalize_prompt_cache_control_ttl(str(cache_control.get("ttl") or "5m"))


def _append_policy_list_value(
    cache_policy: dict[str, Any] | None,
    key: str,
    value: str,
) -> None:
    if cache_policy is None:
        return
    normalized = str(value or "").strip()
    if not normalized:
        return
    existing = cache_policy.get(key)
    values = [str(item).strip() for item in existing] if isinstance(existing, list) else []
    if normalized not in values:
        values.append(normalized)
    cache_policy[key] = values


def _apply_anthropic_cache_control_plan(
    *,
    payload: dict[str, Any],
    cache_control: Mapping[str, Any] | None,
    cache_policy: dict[str, Any] | None,
) -> None:
    if not isinstance(cache_control, Mapping):
        return

    explicit_payloads = explicit_cache_control_payloads(payload)
    explicit_block_count = count_explicit_cache_control_blocks(payload)
    requested_ttl = _cache_control_ttl(cache_control)
    ttl_conflict = (
        bool(explicit_payloads) and _cache_control_ttl(explicit_payloads[-1]) != requested_ttl
    )
    top_level_allowed = (
        not ttl_conflict and explicit_block_count < _ANTHROPIC_MAX_CACHE_CONTROL_BREAKPOINTS
    )
    explicit_slot_limit = _ANTHROPIC_MAX_CACHE_CONTROL_BREAKPOINTS - (1 if top_level_allowed else 0)

    system = payload.get("system")
    if system and not ttl_conflict:
        system_has_cache_control = count_cache_control_blocks(system) > 0
        if not system_has_cache_control and explicit_block_count < explicit_slot_limit:
            updated_system, added_system_cache_control = _anthropic_system_with_cache_control(
                system,
                cache_control=cache_control,
            )
            if added_system_cache_control:
                payload["system"] = updated_system
                explicit_block_count = count_explicit_cache_control_blocks(payload)
        elif system_has_cache_control:
            explicit_block_count = count_explicit_cache_control_blocks(payload)

    if top_level_allowed:
        payload[CACHE_CONTROL_FIELD] = copy.deepcopy(dict(cache_control))
    else:
        _append_policy_list_value(
            cache_policy,
            "warnings",
            (
                "anthropic_top_level_cache_control_skipped_ttl_conflict"
                if ttl_conflict
                else "anthropic_top_level_cache_control_skipped_breakpoint_limit"
            ),
        )

    explicit_block_count = count_explicit_cache_control_blocks(payload)
    if cache_policy is not None:
        cache_policy["used"] = bool(top_level_allowed or explicit_block_count > 0)
        cache_policy["top_level_cache_control_used"] = bool(top_level_allowed)
        cache_policy["explicit_block_used"] = explicit_block_count > 0
        cache_policy["explicit_block_count"] = explicit_block_count
        if not top_level_allowed and explicit_block_count <= 0:
            cache_policy["fallback"] = "cache_control_not_applied"
            _append_policy_list_value(cache_policy, "disabled_fields", CACHE_CONTROL_FIELD)


def _payload_has_cache_control(payload: Mapping[str, Any]) -> bool:
    return count_cache_control_blocks(payload) > 0


def _cache_control_rejection_reason(response: httpx.Response) -> str | None:
    if response.status_code != 400:
        return None
    try:
        body = response.text
    except Exception:
        body = ""
    lowered = body.lower()
    if any(
        marker in lowered
        for marker in (
            "cache_control",
            "cache control",
            "prompt cache",
            "cache breakpoint",
            "ephemeral",
            "ttl",
        )
    ):
        return "anthropic_cache_control_rejected"
    return None


def _temperature_rejection_reason(response: httpx.Response) -> str | None:
    if response.status_code not in {400, 422}:
        return None
    try:
        body = response.text
    except Exception:
        body = ""
    if "temperature" in body.casefold():
        return "provider_rejected_temperature"
    return None


def _thinking_display_rejection_reason(response: httpx.Response) -> str | None:
    """Return a fallback reason only for explicit summary-display incompatibility."""

    if response.status_code not in {400, 422}:
        return None
    try:
        body = response.text
    except Exception:
        body = ""
    lowered = body.casefold()
    if not any(marker in lowered for marker in ("display", "summarized")):
        return None
    if not any(
        marker in lowered
        for marker in (
            "unsupported",
            "not supported",
            "does not support",
            "unknown",
            "unrecognized",
            "invalid",
            "not allowed",
            "not permitted",
            "unexpected",
            "extra input",
            "extra field",
        )
    ):
        return None
    return "provider_rejected_thinking_display"


def _payload_requests_summarized_thinking(payload: Mapping[str, Any]) -> bool:
    thinking = payload.get("thinking")
    return isinstance(thinking, Mapping) and thinking.get("display") == "summarized"


def _without_thinking_display(payload: Mapping[str, Any]) -> dict[str, Any]:
    downgraded = copy.deepcopy(dict(payload))
    thinking = downgraded.get("thinking")
    if isinstance(thinking, dict):
        thinking.pop("display", None)
    return downgraded


def _downgrade_anthropic_cache_control_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    downgraded = copy.deepcopy(dict(payload))
    strip_cache_control_blocks(downgraded)
    return downgraded


def _mark_anthropic_cache_control_downgrade(
    cache_policy: dict[str, Any] | None,
    *,
    reason: str,
) -> None:
    if cache_policy is None:
        return
    cache_policy["status"] = "fallback"
    cache_policy["used"] = False
    cache_policy["fallback"] = reason
    cache_policy["top_level_cache_control_used"] = False
    cache_policy["explicit_block_used"] = False
    cache_policy["explicit_block_count"] = 0
    _append_policy_list_value(cache_policy, "disabled_fields", CACHE_CONTROL_FIELD)
    _append_policy_list_value(cache_policy, "warnings", reason)


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

    input_tokens = _as_non_negative_int(raw.get("input_tokens"))
    output_tokens = _as_non_negative_int(raw.get("output_tokens"))
    output_details = raw.get("output_tokens_details")
    reasoning_tokens = (
        _as_non_negative_int(output_details.get("thinking_tokens"))
        if isinstance(output_details, dict)
        else None
    )
    cache_read_input_tokens = _as_non_negative_int(raw.get("cache_read_input_tokens"))
    cache_creation = raw.get("cache_creation")
    cache_creation_5m_input_tokens: int | None = None
    cache_creation_1h_input_tokens: int | None = None
    if isinstance(cache_creation, dict):
        cache_creation_5m_input_tokens = _as_non_negative_int(
            cache_creation.get("ephemeral_5m_input_tokens")
        )
        cache_creation_1h_input_tokens = _as_non_negative_int(
            cache_creation.get("ephemeral_1h_input_tokens")
        )
    cache_creation_input_tokens = _as_non_negative_int(raw.get("cache_creation_input_tokens"))
    if cache_creation_input_tokens is None:
        creation_parts = [
            value
            for value in (cache_creation_5m_input_tokens, cache_creation_1h_input_tokens)
            if value is not None
        ]
        if creation_parts:
            cache_creation_input_tokens = sum(creation_parts)

    has_cache_accounting = any(
        value is not None for value in (cache_read_input_tokens, cache_creation_input_tokens)
    )
    prompt_tokens = input_tokens
    if has_cache_accounting:
        prompt_tokens = sum(
            value or 0
            for value in (input_tokens, cache_read_input_tokens, cache_creation_input_tokens)
        )
    total_tokens = None
    if prompt_tokens is not None and output_tokens is not None:
        total_tokens = prompt_tokens + output_tokens
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_prompt_tokens=cache_read_input_tokens,
        input_tokens_uncached=input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_creation_5m_input_tokens=cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=cache_creation_1h_input_tokens,
        # Anthropic output_tokens already includes thinking; this is a subset.
        reasoning_tokens=reasoning_tokens,
        raw_provider_usage=copy.deepcopy(raw),
    )


def _extract_error_message(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        message = str(error_obj.get("message") or "").strip()
        if message:
            error_type = str(error_obj.get("type") or "").strip()
            return f"{error_type}: {message}" if error_type else message
    return None


def _text_from_content_blocks(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _thinking_summaries_from_content_blocks(content: list[Any]) -> list[str]:
    summaries: list[str] = []
    for block in content:
        if not isinstance(block, dict) or str(block.get("type") or "") != "thinking":
            continue
        thinking = block.get("thinking")
        if isinstance(thinking, str) and thinking:
            # This field is only requested with display="summarized". Never
            # surface signatures, redacted_thinking blocks, or opaque state.
            summaries.append(thinking)
    return summaries


def _parse_tool_calls(content: list[Any]) -> list[ToolCall]:
    tool_calls: list[ToolCall] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") != "tool_use":
            continue
        tool_use_id = str(block.get("id") or f"toolu_{index}").strip()
        name = str(block.get("name") or "").strip()
        if not name:
            continue
        raw_input = block.get("input")
        arguments = (
            dict(raw_input) if isinstance(raw_input, dict) else {"_raw_arguments": raw_input}
        )
        tool_calls.append(
            ToolCall(
                id=tool_use_id,
                name=name,
                arguments=arguments,
                provider_metadata={
                    _ANTHROPIC_METADATA_KEY: {
                        "content_index": index,
                    }
                },
            )
        )
    return tool_calls


def _collect_web_search_metadata(content: list[Any]) -> dict[str, Any]:
    server_tool_uses: list[dict[str, Any]] = []
    web_search_results: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    queries: list[str] = []

    def _append_source(raw: dict[str, Any]) -> None:
        url = str(raw.get("url") or "").strip()
        if not url:
            return
        source: dict[str, Any] = {
            "url": url,
            "title": str(raw.get("title") or "").strip(),
        }
        for key in ("page_age", "encrypted_content", "encrypted_index", "cited_text"):
            value = raw.get(key)
            if value is not None:
                source[key] = value
        sources.append(source)

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "server_tool_use" and str(block.get("name") or "") == "web_search":
            copied = copy.deepcopy(block)
            server_tool_uses.append(copied)
            raw_input = block.get("input")
            if isinstance(raw_input, dict):
                query = str(raw_input.get("query") or "").strip()
                if query:
                    queries.append(query)
            continue
        if block_type == "web_search_tool_result":
            web_search_results.append(copy.deepcopy(block))
            result_content = block.get("content")
            if isinstance(result_content, list):
                for result in result_content:
                    if (
                        isinstance(result, dict)
                        and str(result.get("type") or "") == "web_search_result"
                    ):
                        web_search_results.append(copy.deepcopy(result))
                        _append_source(result)
            continue
        if block_type != "text":
            continue
        raw_citations = block.get("citations")
        if not isinstance(raw_citations, list):
            continue
        for citation in raw_citations:
            if not isinstance(citation, dict):
                continue
            if str(citation.get("type") or "") != "web_search_result_location":
                continue
            url = str(citation.get("url") or "").strip()
            if not url:
                continue
            citation_payload = {
                "url": url,
                "title": str(citation.get("title") or "").strip(),
                "encrypted_index": citation.get("encrypted_index"),
                "cited_text": citation.get("cited_text"),
            }
            citations.append(citation_payload)
            _append_source(citation)

    if queries:
        queries = list(dict.fromkeys(queries))
    deduped_sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped_sources.append(source)
    metadata: dict[str, Any] = {}
    if server_tool_uses:
        metadata["server_tool_uses"] = server_tool_uses
    if web_search_results:
        metadata["web_search_results"] = web_search_results
    if citations:
        metadata["citations"] = citations
    if deduped_sources:
        metadata["sources"] = deduped_sources
    if queries:
        metadata["queries"] = queries
    return metadata


def _anthropic_provider_metadata(data: dict[str, Any]) -> dict[str, Any] | None:
    content = data.get("content")
    metadata: dict[str, Any] = {}
    message_id = str(data.get("id") or "").strip()
    if message_id:
        metadata["message_id"] = message_id
    stop_reason = str(data.get("stop_reason") or "").strip()
    if stop_reason:
        metadata["stop_reason"] = stop_reason
    stop_sequence = data.get("stop_sequence")
    if stop_sequence is not None:
        metadata["stop_sequence"] = stop_sequence
    if isinstance(content, list):
        metadata["content_blocks"] = copy.deepcopy(content)
        metadata.update(_collect_web_search_metadata(content))
    usage = data.get("usage")
    if isinstance(usage, dict):
        metadata["usage"] = copy.deepcopy(usage)
    stream_metadata = data.get("stream_metadata")
    if isinstance(stream_metadata, dict):
        metadata["stream_metadata"] = copy.deepcopy(stream_metadata)
    return {_ANTHROPIC_METADATA_KEY: metadata} if metadata else None


def _response_from_json(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=data)


def _event_index(data: dict[str, Any], *, event_type: str) -> int:
    index = data.get("index")
    if isinstance(index, int) and index >= 0:
        return index
    raise LLMError(f"Anthropic Messages stream {event_type} event is missing a valid index")


class _AnthropicStreamAccumulator:
    def __init__(
        self,
        *,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None,
        reasoning_is_summary: bool,
    ) -> None:
        self.on_text_delta = on_text_delta
        self.on_reasoning_delta = on_reasoning_delta
        self.reasoning_is_summary = reasoning_is_summary
        self.message: dict[str, Any] = {
            "type": "message",
            "role": "assistant",
            "content": [],
        }
        self.content_blocks: dict[int, dict[str, Any]] = {}
        self.input_json_chunks: dict[int, list[str]] = {}
        self.usage: dict[str, Any] = {}
        self.unknown_events: list[dict[str, Any]] = []
        self.event_count = 0
        self.seen_message_start = False
        self.seen_message_stop = False

    def handle(self, frame: SSEFrame, data: dict[str, Any]) -> None:
        event_type = str(data.get("type") or frame.event or "").strip()
        if not event_type:
            self._append_unknown(frame=frame, data=data)
            return
        self.event_count += 1

        if event_type == "message_start":
            self._handle_message_start(data)
            return
        if event_type == "content_block_start":
            self._handle_content_block_start(data)
            return
        if event_type == "content_block_delta":
            self._handle_content_block_delta(data)
            return
        if event_type == "content_block_stop":
            self._handle_content_block_stop(data)
            return
        if event_type == "message_delta":
            self._handle_message_delta(data)
            return
        if event_type == "message_stop":
            self.seen_message_stop = True
            return
        if event_type == "ping":
            return
        if event_type == "error":
            message = _extract_error_message(data)
            raise LLMError(f"Anthropic Messages stream error: {message or data!r}")

        self._append_unknown(frame=frame, data=data)

    def _handle_message_start(self, data: dict[str, Any]) -> None:
        raw_message = data.get("message")
        if not isinstance(raw_message, dict):
            raise LLMError("Anthropic Messages stream message_start missing message object")
        message = copy.deepcopy(raw_message)
        content = message.get("content")
        if not isinstance(content, list):
            message["content"] = []
        else:
            message["content"] = []
        self.message.update(message)
        usage = message.get("usage")
        if isinstance(usage, dict):
            self.usage.update(copy.deepcopy(usage))
        self.seen_message_start = True

    def _handle_content_block_start(self, data: dict[str, Any]) -> None:
        index = _event_index(data, event_type="content_block_start")
        raw_block = data.get("content_block")
        block = copy.deepcopy(raw_block) if isinstance(raw_block, dict) else {}
        if not str(block.get("type") or "").strip():
            block["type"] = "unknown"
        block.setdefault("_stream_index", index)
        self.content_blocks[index] = block
        if (
            block.get("type") == "thinking"
            and self.reasoning_is_summary
            and self.on_reasoning_delta is not None
        ):
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking:
                self.on_reasoning_delta(thinking)
        if str(block.get("type") or "") in {"tool_use", "server_tool_use"}:
            self.input_json_chunks.setdefault(index, [])

    def _handle_content_block_delta(self, data: dict[str, Any]) -> None:
        index = _event_index(data, event_type="content_block_delta")
        block = self.content_blocks.setdefault(index, {"type": "unknown", "_stream_index": index})
        raw_delta = data.get("delta")
        if not isinstance(raw_delta, dict):
            self._append_block_delta(block, raw_delta)
            return
        delta_type = str(raw_delta.get("type") or "").strip()

        if delta_type == "text_delta":
            text = raw_delta.get("text")
            if isinstance(text, str) and text:
                existing = block.get("text")
                block["text"] = (existing if isinstance(existing, str) else "") + text
                if self.on_text_delta is not None:
                    self.on_text_delta(text)
            return

        if delta_type == "input_json_delta":
            partial_json = raw_delta.get("partial_json")
            if isinstance(partial_json, str):
                self.input_json_chunks.setdefault(index, []).append(partial_json)
            return

        if delta_type == "thinking_delta":
            thinking = raw_delta.get("thinking")
            if isinstance(thinking, str) and thinking:
                existing = block.get("thinking")
                block["thinking"] = (existing if isinstance(existing, str) else "") + thinking
                if self.reasoning_is_summary and self.on_reasoning_delta is not None:
                    self.on_reasoning_delta(thinking)
            return

        if delta_type == "signature_delta":
            signature = raw_delta.get("signature")
            if isinstance(signature, str) and signature:
                existing = block.get("signature")
                block["signature"] = (existing if isinstance(existing, str) else "") + signature
            return

        if delta_type in {"citations_delta", "citation_delta"}:
            self._append_citation_delta(block, raw_delta)
            return

        self._append_block_delta(block, raw_delta)

    def _handle_content_block_stop(self, data: dict[str, Any]) -> None:
        index = _event_index(data, event_type="content_block_stop")
        block = self.content_blocks.get(index)
        if block is None:
            raise LLMError(
                "Anthropic Messages stream content_block_stop before content_block_start"
            )
        chunks = self.input_json_chunks.get(index)
        if chunks is None:
            return
        joined = "".join(chunks).strip()
        if not joined:
            if "input" not in block:
                block["input"] = {}
            return
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError as exc:
            raise LLMError(
                "Anthropic Messages stream emitted malformed tool input JSON "
                f"for content block {index}: {exc.msg}"
            ) from exc
        block["input"] = parsed if isinstance(parsed, dict) else {"_raw_arguments": parsed}

    def _handle_message_delta(self, data: dict[str, Any]) -> None:
        delta = data.get("delta")
        if isinstance(delta, dict):
            for key in ("stop_reason", "stop_sequence"):
                if key in delta:
                    self.message[key] = copy.deepcopy(delta[key])
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage.update(copy.deepcopy(usage))

    def finish(self) -> dict[str, Any]:
        if not self.seen_message_start:
            raise LLMError("Anthropic Messages stream returned no message_start event")
        if not self.seen_message_stop:
            raise LLMError("Anthropic Messages stream ended before message_stop")
        for index in tuple(self.input_json_chunks):
            self._handle_content_block_stop({"index": index})
        content = [
            self._public_content_block(block)
            for _index, block in sorted(self.content_blocks.items(), key=lambda item: item[0])
        ]
        self.message["content"] = content
        if self.usage:
            self.message["usage"] = copy.deepcopy(self.usage)
        stream_metadata: dict[str, Any] = {"events": self.event_count}
        if self.unknown_events:
            stream_metadata["unknown_events"] = copy.deepcopy(self.unknown_events)
        self.message["stream_metadata"] = stream_metadata
        return copy.deepcopy(self.message)

    @staticmethod
    def _append_citation_delta(block: dict[str, Any], delta: dict[str, Any]) -> None:
        citations = block.setdefault("citations", [])
        if not isinstance(citations, list):
            citations = []
            block["citations"] = citations
        citation = delta.get("citation")
        if isinstance(citation, dict):
            citations.append(copy.deepcopy(citation))
            return
        raw_citations = delta.get("citations")
        if isinstance(raw_citations, list):
            citations.extend(
                copy.deepcopy(item) for item in raw_citations if isinstance(item, dict)
            )

    @staticmethod
    def _append_block_delta(block: dict[str, Any], delta: Any) -> None:
        deltas = block.setdefault("_stream_deltas", [])
        if isinstance(deltas, list):
            deltas.append(copy.deepcopy(delta))

    def _append_unknown(self, *, frame: SSEFrame, data: dict[str, Any]) -> None:
        self.unknown_events.append(
            {
                "event": frame.event,
                "data": copy.deepcopy(data),
            }
        )

    @staticmethod
    def _public_content_block(block: dict[str, Any]) -> dict[str, Any]:
        copied = copy.deepcopy(block)
        copied.pop("_stream_index", None)
        return copied


class AnthropicMessagesClient:
    usage_contract = UsageContract(
        response_usage_confidence=UsageConfidence.AUTHORITATIVE,
        input_token_count_strategy="anthropic_messages",
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
        transport: httpx.BaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_key: str | None = None,
        web_search_mode: str = "off",
        web_search_adapter: str = AUTO_WEB_SEARCH_ADAPTER,
        prompt_cache_control_enabled: bool = False,
        prompt_cache_control_ttl: str = "5m",
        prompt_cache_policy_metadata: Mapping[str, Any] | None = None,
        provider_concurrency_caps: dict[str, int] | None = None,
        provider_retry_settings: ProviderRetrySettings | None = None,
        provider_sleep_fn: Callable[[float], None] | None = None,
        provider_random_fn: Callable[[], float] | None = None,
        default_max_tokens: int = 4096,
        usage_contract: UsageContract | None = None,
        route_identity: ProviderRouteIdentity | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.supports_forced_tool_choice = _claude_model_version(model) != _ClaudeModelVersion(
            "sonnet", 5, 5
        )
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.prompt_cache_key = str(prompt_cache_key or "").strip() or None
        self.prompt_cache_retention = str(prompt_cache_retention or "").strip() or None
        self.enable_thinking = enable_thinking
        self.reasoning_effort = str(reasoning_effort or "").strip().lower() or None
        self._transport = transport
        self.extra_headers = canonicalize_extra_headers(extra_headers)
        self.provider_key = str(provider_key or "").strip() or None
        self.route_identity = route_identity or build_provider_route_identity(
            protocol="anthropic_messages",
            base_url=self.base_url,
            provider_key=self.provider_key,
            model=self.model,
            credential_scope=credential_scope_fingerprint(self.api_key),
            routing_headers=self.extra_headers,
            protocol_revision=ANTHROPIC_MESSAGES_ROUTE_REVISION,
        )
        self.web_search_mode = str(web_search_mode or "off").strip().lower()
        self.web_search_adapter = (
            str(web_search_adapter or AUTO_WEB_SEARCH_ADAPTER).strip().lower()
            or AUTO_WEB_SEARCH_ADAPTER
        )
        self.prompt_cache_control_enabled = bool(prompt_cache_control_enabled)
        self.prompt_cache_control_ttl = _normalize_prompt_cache_control_ttl(
            prompt_cache_control_ttl
        )
        self.prompt_cache_policy_metadata = (
            copy.deepcopy(dict(prompt_cache_policy_metadata))
            if isinstance(prompt_cache_policy_metadata, Mapping)
            else None
        )
        self.provider_concurrency_caps = dict(
            DEFAULT_PROVIDER_CONCURRENCY_CAPS
            if provider_concurrency_caps is None
            else provider_concurrency_caps
        )
        self.provider_retry_settings = provider_retry_settings or ProviderRetrySettings()
        self._provider_sleep_fn = provider_sleep_fn
        self._provider_random_fn = provider_random_fn
        self.default_max_tokens = int(default_max_tokens)
        self.usage_contract = usage_contract or type(self).usage_contract
        self.usage_counts_authoritative = self.usage_contract.response_usage_authoritative
        self._input_token_count_available: bool | None = None
        self._temperature_omit_after_rejection = False
        self._thinking_display_supported: bool | None = None
        self._wire_request_diagnostics = WireRequestDiagnostics()

    def _headers(self) -> dict[str, str]:
        headers = merge_canonical_headers(
            {
                **(
                    {"Authorization": "Bearer " + self.api_key}
                    if self.provider_key == "alysis"
                    else {"x-api-key": self.api_key}
                ),
                "anthropic-version": _DEFAULT_ANTHROPIC_VERSION,
                "Content-Type": "application/json",
                "User-Agent": "alysis-code/0.1.0",
            },
            self.extra_headers,
        )
        return _headers_with_default_accept_encoding(headers)

    @staticmethod
    def _llm_error_from_response(response: httpx.Response) -> LLMError:
        # Keep the structured envelope for shared error presentation. Extracting
        # only the message loses hosted credit/RPM codes and mislabels all 429s
        # as provider throttling. Preserve the existing readable exception text.
        safe_body = sanitize_error_text_for_output(response.text)
        try:
            data = response.json()
        except Exception:
            body = safe_body
            if len(body) > 1000:
                body = body[:1000] + "...(truncated)"
        else:
            body = _extract_error_message(data) or repr(data)
            raw_error = data.get("error") if isinstance(data, dict) else None
            if isinstance(raw_error, dict) and isinstance(raw_error.get("code"), str):
                # Surfaces and persisted errors can receive only str(error).
                # Retain coded envelopes there as well as on the exception.
                body = safe_body
        error = LLMError(
            sanitize_error_text_for_output(f"LLM error {response.status_code}: {body}")
        )
        error.provider_status_code = response.status_code
        error.provider_error_body = safe_body
        return error

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
        system, anthropic_messages = _anthropic_messages_from_messages(messages)
        tool_mapping = _anthropic_tools(
            tools,
            mode=self.web_search_mode,
            adapter=self.web_search_adapter,
            provider_key=self.provider_key,
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
        }
        force_omitted_display = omitted_reasoning_display_required(self)
        thinking_plan = _anthropic_thinking_plan(
            model=self.model,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.default_max_tokens,
            request_summary=False,
            force_omitted_display=force_omitted_display,
        )
        if thinking_plan.config is not None:
            payload["thinking"] = copy.deepcopy(thinking_plan.config)
            if self._thinking_display_supported is False and not force_omitted_display:
                payload = _without_thinking_display(payload)
        if system:
            payload["system"] = system
        if tool_mapping.tools:
            payload["tools"] = tool_mapping.tools
            mapped_tool_choice = _anthropic_tool_choice(
                tool_choice,
                removed_alysis_web_search=tool_mapping.removed_alysis_web_search,
                added_builtin_web_search=tool_mapping.added_builtin_web_search,
            )
            if mapped_tool_choice is not None:
                if not (
                    thinking_plan.active
                    and _claude_model_version(self.model) != _ClaudeModelVersion("haiku", 5, 5)
                    and mapped_tool_choice.get("type") in {"any", "tool"}
                ):
                    payload["tool_choice"] = mapped_tool_choice
        count_cache_plan = RequestCachePlan(
            strategy=("anthropic_cache_control" if self.prompt_cache_control_enabled else "none"),
            mode="automatic" if self.prompt_cache_control_enabled else "manual",
            anthropic_cache_control_enabled=self.prompt_cache_control_enabled,
            anthropic_cache_control_ttl=self.prompt_cache_control_ttl,
        )
        _apply_anthropic_cache_control_plan(
            payload=payload,
            cache_control=count_cache_plan.anthropic_cache_control_payload(),
            cache_policy=merge_cache_policy_metadata(
                self.prompt_cache_policy_metadata,
                count_cache_plan.anthropic_cache_policy_metadata(),
            ),
        )
        url = f"{self.base_url}/messages/count_tokens"

        def _send_request() -> InputTokenCount | None:
            try:
                with provider_httpx_client(
                    timeout=self.timeout_s, transport=self._transport
                ) as client:
                    response = client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                raise LLMError(
                    "Anthropic input token count request failed: "
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
                raise LLMError("Anthropic input token count returned non-JSON response") from exc
            count = _non_negative_int(data.get("input_tokens") if isinstance(data, dict) else None)
            if count is None:
                raise LLMError("Anthropic input token count response omitted input_tokens")
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
            operation="anthropic_messages_count_input_tokens",
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
            strategy="anthropic_cache_control" if self.prompt_cache_control_enabled else "none",
            mode="automatic" if self.prompt_cache_control_enabled else "manual",
            anthropic_cache_control_enabled=self.prompt_cache_control_enabled,
            anthropic_cache_control_ttl=self.prompt_cache_control_ttl,
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
            and not plan.cache.anthropic_cache_control_enabled
            and self.prompt_cache_control_enabled
        ):
            plan = plan.with_cache(default_cache)
        messages = gate_messages_for_provider_route(plan.message_list(), self.route_identity)
        tools = plan.tool_list()
        tool_choice = plan.tool_choice
        response_format = plan.response_format
        stream = plan.stream
        on_reasoning_delta = reasoning_trace_callback(
            on_reasoning_delta,
            capability=getattr(self, "reasoning_trace_capability", None),
            stream=stream,
        )
        temperature = plan.temperature
        max_tokens = plan.max_tokens
        if response_format is not None:
            raise LLMError("Anthropic Messages does not support response_format")

        system, anthropic_messages = _anthropic_messages_from_messages(messages)
        tool_mapping = _anthropic_tools(
            tools,
            mode=self.web_search_mode,
            adapter=self.web_search_adapter,
            provider_key=self.provider_key,
        )
        effective_max_tokens = (
            int(max_tokens) if max_tokens is not None else self.default_max_tokens
        )
        force_omitted_display = omitted_reasoning_display_required(self)
        thinking_plan = _anthropic_thinking_plan(
            model=self.model,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self.reasoning_effort,
            max_tokens=effective_max_tokens,
            request_summary=(
                on_reasoning_delta is not None and self._thinking_display_supported is not False
            ),
            force_omitted_display=force_omitted_display,
        )
        temperature_omit_reason = documented_temperature_omit_reason(self.model)
        if thinking_plan.active:
            temperature_omit_reason = "anthropic_extended_thinking_temperature_unsupported"
        if self._temperature_omit_after_rejection and temperature_omit_reason is None:
            temperature_omit_reason = "provider_rejected_parameter"
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": effective_max_tokens,
            "messages": anthropic_messages,
        }
        if thinking_plan.config is not None:
            payload["thinking"] = copy.deepcopy(thinking_plan.config)
            if self._thinking_display_supported is False and not force_omitted_display:
                payload = _without_thinking_display(payload)
        if thinking_plan.output_effort is not None:
            payload["output_config"] = {"effort": thinking_plan.output_effort}
        if temperature_omit_reason is None:
            payload["temperature"] = self.temperature if temperature is None else float(temperature)
        cache_control = plan.cache.anthropic_cache_control_payload()
        cache_policy = merge_cache_policy_metadata(
            self.prompt_cache_policy_metadata,
            plan.cache.anthropic_cache_policy_metadata(),
        )
        if stream:
            payload["stream"] = True
        if system:
            payload["system"] = system

        def _validate_omitted_reasoning_response(response: LLMResponse) -> LLMResponse:
            if not force_omitted_display:
                return response
            metadata = response.provider_metadata
            anthropic_metadata = (
                metadata.get(_ANTHROPIC_METADATA_KEY) if isinstance(metadata, dict) else None
            )
            blocks = (
                anthropic_metadata.get("content_blocks")
                if isinstance(anthropic_metadata, dict)
                else None
            )
            if isinstance(blocks, list) and any(
                isinstance(block, dict)
                and block.get("type") == "thinking"
                and bool(block.get("thinking"))
                for block in blocks
            ):
                raise LLMError(
                    "Anthropic Messages violated the required omitted reasoning display contract"
                )
            return response

        forced_tool_choice_omitted = False
        if tool_mapping.tools:
            payload["tools"] = tool_mapping.tools
            mapped_tool_choice = _anthropic_tool_choice(
                tool_choice,
                removed_alysis_web_search=tool_mapping.removed_alysis_web_search,
                added_builtin_web_search=tool_mapping.added_builtin_web_search,
            )
            serial_calls = serial_tool_calls_required(self)
            if mapped_tool_choice is not None or serial_calls:
                effective_tool_choice = copy.deepcopy(mapped_tool_choice or {"type": "auto"})
                if (
                    thinking_plan.active
                    and _claude_model_version(self.model) != _ClaudeModelVersion("haiku", 5, 5)
                    and effective_tool_choice.get("type") in {"any", "tool"}
                ):
                    # Anthropic rejects forced tool use while extended thinking
                    # is active. Leaving the choice unset preserves automatic
                    # tool selection without failing the entire turn.
                    forced_tool_choice_omitted = True
                    if serial_calls:
                        payload["tool_choice"] = {
                            "type": "auto",
                            "disable_parallel_tool_use": True,
                        }
                else:
                    if serial_calls:
                        effective_tool_choice["disable_parallel_tool_use"] = True
                    payload["tool_choice"] = effective_tool_choice
        elif tool_choice is not None:
            raise LLMError("Anthropic Messages tool_choice requires at least one available tool")

        _apply_anthropic_cache_control_plan(
            payload=payload,
            cache_control=cache_control,
            cache_policy=cache_policy,
        )

        def _prompt_estimation_payload(current_payload: Mapping[str, Any]) -> dict[str, Any]:
            estimation_payload = {
                "messages": current_payload.get("messages", []),
            }
            for key in ("system", "tools", "cache_control"):
                if key in current_payload:
                    estimation_payload[key] = current_payload[key]
            return estimation_payload

        def _request_shape_metadata(
            current_payload: Mapping[str, Any],
            *,
            input_mode: str = "full",
        ) -> dict[str, Any]:
            return build_request_shape_report(
                messages=messages,
                tools=tools,
                cache_policy=cache_policy,
                provider_payload=_prompt_estimation_payload(current_payload),
                input_mode=input_mode,
            )

        def _token_reconciliation_metadata(
            current_payload: Mapping[str, Any],
            *,
            input_mode: str = "full",
        ) -> dict[str, Any]:
            input_estimate_tokens = estimate_provider_payload_tokens(
                _prompt_estimation_payload(current_payload)
            )
            return {
                "input_estimate_tokens": input_estimate_tokens,
                "sent_input_estimate_tokens": input_estimate_tokens,
                "estimator": "cl100k_base",
                "estimate_basis": "provider_prompt_payload",
                "input_mode": input_mode,
            }

        request_shape = _request_shape_metadata(payload)
        token_reconciliation = _token_reconciliation_metadata(payload)
        request_plan_extra: dict[str, Any] = {}
        if temperature_omit_reason is not None:
            request_plan_extra.update(
                {
                    "temperature_omitted": True,
                    "temperature_omit_reason": temperature_omit_reason,
                }
            )
        payload_thinking = payload.get("thinking")
        if isinstance(payload_thinking, Mapping):
            request_plan_extra.update(
                {
                    "thinking_mode": payload_thinking.get("type"),
                    "thinking_display": payload_thinking.get("display"),
                    "reasoning_summary_requested": _payload_requests_summarized_thinking(payload),
                }
            )
        if forced_tool_choice_omitted:
            request_plan_extra.update(
                {
                    "tool_choice_omitted": True,
                    "tool_choice_omit_reason": "anthropic_extended_thinking_forced_tool_unsupported",
                }
            )
        wire_diagnostics = self._wire_request_diagnostics.begin_request()
        request_plan_metadata = plan.request_plan_metadata(
            input_mode="full",
            continuation_strategy="full_replay",
            provider_payload=_prompt_estimation_payload(payload),
            sent_provider_payload=_prompt_estimation_payload(payload),
            cache_policy_metadata=cache_policy,
            extra=request_plan_extra or None,
        )

        request_plan_metadata.update(
            wire_diagnostics(payload, history_key="messages", instructions_key="system")
        )

        provider_key = self.provider_key or best_effort_provider_key(
            base_url=self.base_url,
            model=self.model,
        )
        telemetry = ProviderCallTelemetryRecorder(
            provider_key=provider_key,
            protocol="anthropic_messages",
            model=self.model,
            base_url=self.base_url,
            stream=stream,
            tools=tools,
            web_search_mode=self.web_search_mode,
            web_search_adapter=self.web_search_adapter,
            native_web_search=tool_mapping.added_builtin_web_search,
            cache_policy=cache_policy,
            request_plan=request_plan_metadata,
            request_shape=request_shape,
            token_reconciliation=token_reconciliation,
            operation="anthropic_messages_chat",
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

        def _activate_reasoning_summary_fallback(
            current_payload: Mapping[str, Any],
            *,
            reason: str,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            downgraded_payload = _without_thinking_display(current_payload)
            fallback_plan = plan.request_plan_metadata(
                input_mode="reasoning_summary_fallback",
                continuation_strategy="full_replay",
                provider_payload=_prompt_estimation_payload(downgraded_payload),
                sent_provider_payload=_prompt_estimation_payload(downgraded_payload),
                cache_policy_metadata=cache_policy,
                extra={
                    "fallback_used": True,
                    "thinking_display_omitted": True,
                    "reasoning_summary_requested": False,
                    "reasoning_summary_fallback_reason": reason,
                },
            )
            fallback_plan.update(
                wire_diagnostics(
                    downgraded_payload, history_key="messages", instructions_key="system"
                )
            )
            telemetry.set_request_plan(fallback_plan)
            telemetry.set_request_shape(
                _request_shape_metadata(
                    downgraded_payload,
                    input_mode="reasoning_summary_fallback",
                )
            )
            telemetry.set_token_reconciliation(
                _token_reconciliation_metadata(
                    downgraded_payload,
                    input_mode="reasoning_summary_fallback",
                )
            )
            return downgraded_payload, fallback_plan

        def _send_request() -> LLMResponse:
            url = f"{self.base_url}/messages"
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
                    if self._thinking_display_supported is False and (
                        _payload_requests_summarized_thinking(request_payload)
                    ):
                        request_payload, active_request_plan_metadata = (
                            _activate_reasoning_summary_fallback(
                                request_payload,
                                reason="cached_provider_rejection",
                            )
                        )
                    cache_control_retry_used = False
                    temperature_retry_used = False
                    thinking_display_retry_used = False
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
                                    thinking_display_rejection = (
                                        _thinking_display_rejection_reason(response)
                                        if not thinking_display_retry_used
                                        and _payload_requests_summarized_thinking(request_payload)
                                        else None
                                    )
                                    if thinking_display_rejection is not None:
                                        thinking_display_retry_used = True
                                        self._thinking_display_supported = False
                                        request_payload, active_request_plan_metadata = (
                                            _activate_reasoning_summary_fallback(
                                                request_payload,
                                                reason=thinking_display_rejection,
                                            )
                                        )
                                        continue
                                    temperature_rejection = (
                                        _temperature_rejection_reason(response)
                                        if not temperature_retry_used
                                        and "temperature" in request_payload
                                        else None
                                    )
                                    if temperature_rejection is not None:
                                        temperature_retry_used = True
                                        self._temperature_omit_after_rejection = True
                                        request_payload = copy.deepcopy(dict(request_payload))
                                        request_payload.pop("temperature", None)
                                        active_request_plan_metadata = plan.request_plan_metadata(
                                            input_mode="temperature_fallback",
                                            continuation_strategy="full_replay",
                                            provider_payload=_prompt_estimation_payload(
                                                request_payload
                                            ),
                                            sent_provider_payload=_prompt_estimation_payload(
                                                request_payload
                                            ),
                                            cache_policy_metadata=cache_policy,
                                            extra={
                                                "fallback_used": True,
                                                "temperature_omitted": True,
                                                "temperature_omit_reason": (temperature_rejection),
                                            },
                                        )
                                        active_request_plan_metadata.update(
                                            wire_diagnostics(
                                                request_payload,
                                                history_key="messages",
                                                instructions_key="system",
                                            )
                                        )
                                        telemetry.set_request_plan(active_request_plan_metadata)
                                        telemetry.set_request_shape(
                                            _request_shape_metadata(
                                                request_payload,
                                                input_mode="temperature_fallback",
                                            )
                                        )
                                        telemetry.set_token_reconciliation(
                                            _token_reconciliation_metadata(
                                                request_payload,
                                                input_mode="temperature_fallback",
                                            )
                                        )
                                        continue
                                    downgrade_reason = (
                                        _cache_control_rejection_reason(response)
                                        if not cache_control_retry_used
                                        and _payload_has_cache_control(request_payload)
                                        else None
                                    )
                                    if downgrade_reason is not None:
                                        cache_control_retry_used = True
                                        request_payload = (
                                            _downgrade_anthropic_cache_control_payload(
                                                request_payload
                                            )
                                        )
                                        _mark_anthropic_cache_control_downgrade(
                                            cache_policy,
                                            reason=downgrade_reason,
                                        )
                                        telemetry.set_cache_policy(cache_policy)
                                        active_request_plan_metadata = plan.request_plan_metadata(
                                            input_mode="cache_control_fallback",
                                            continuation_strategy="full_replay",
                                            provider_payload=_prompt_estimation_payload(
                                                request_payload
                                            ),
                                            sent_provider_payload=_prompt_estimation_payload(
                                                request_payload
                                            ),
                                            cache_policy_metadata=cache_policy,
                                            extra={"fallback_used": True},
                                        )
                                        active_request_plan_metadata.update(
                                            wire_diagnostics(
                                                request_payload,
                                                history_key="messages",
                                                instructions_key="system",
                                            )
                                        )
                                        telemetry.set_request_plan(active_request_plan_metadata)
                                        telemetry.set_request_shape(
                                            _request_shape_metadata(
                                                request_payload,
                                                input_mode="cache_control_fallback",
                                            )
                                        )
                                        telemetry.set_token_reconciliation(
                                            _token_reconciliation_metadata(
                                                request_payload,
                                                input_mode="cache_control_fallback",
                                            )
                                        )
                                        continue
                                    raise self._llm_error_from_response(response)
                                return _response_with_cache_policy_metadata(
                                    _validate_omitted_reasoning_response(
                                        self._parse_stream_response(
                                            response,
                                            on_text_delta=(
                                                _tracked_text_delta
                                                if telemetry_on_text_delta is not None
                                                else None
                                            ),
                                            on_reasoning_delta=(
                                                _tracked_reasoning_delta
                                                if telemetry_on_reasoning_delta is not None
                                                else None
                                            ),
                                            reasoning_is_summary=(
                                                _payload_requests_summarized_thinking(
                                                    request_payload
                                                )
                                            ),
                                        )
                                    ),
                                    cache_policy,
                                    active_request_plan_metadata,
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
                        thinking_display_rejection = (
                            _thinking_display_rejection_reason(response)
                            if not thinking_display_retry_used
                            and _payload_requests_summarized_thinking(request_payload)
                            else None
                        )
                        if thinking_display_rejection is not None:
                            thinking_display_retry_used = True
                            self._thinking_display_supported = False
                            request_payload, active_request_plan_metadata = (
                                _activate_reasoning_summary_fallback(
                                    request_payload,
                                    reason=thinking_display_rejection,
                                )
                            )
                            continue
                        temperature_rejection = (
                            _temperature_rejection_reason(response)
                            if not temperature_retry_used and "temperature" in request_payload
                            else None
                        )
                        if temperature_rejection is not None:
                            temperature_retry_used = True
                            self._temperature_omit_after_rejection = True
                            request_payload = copy.deepcopy(dict(request_payload))
                            request_payload.pop("temperature", None)
                            active_request_plan_metadata = plan.request_plan_metadata(
                                input_mode="temperature_fallback",
                                continuation_strategy="full_replay",
                                provider_payload=_prompt_estimation_payload(request_payload),
                                sent_provider_payload=_prompt_estimation_payload(request_payload),
                                cache_policy_metadata=cache_policy,
                                extra={
                                    "fallback_used": True,
                                    "temperature_omitted": True,
                                    "temperature_omit_reason": temperature_rejection,
                                },
                            )
                            active_request_plan_metadata.update(
                                wire_diagnostics(
                                    request_payload,
                                    history_key="messages",
                                    instructions_key="system",
                                )
                            )
                            telemetry.set_request_plan(active_request_plan_metadata)
                            telemetry.set_request_shape(
                                _request_shape_metadata(
                                    request_payload,
                                    input_mode="temperature_fallback",
                                )
                            )
                            telemetry.set_token_reconciliation(
                                _token_reconciliation_metadata(
                                    request_payload,
                                    input_mode="temperature_fallback",
                                )
                            )
                            continue
                        downgrade_reason = (
                            _cache_control_rejection_reason(response)
                            if not cache_control_retry_used
                            and _payload_has_cache_control(request_payload)
                            else None
                        )
                        if downgrade_reason is not None:
                            cache_control_retry_used = True
                            request_payload = _downgrade_anthropic_cache_control_payload(
                                request_payload
                            )
                            _mark_anthropic_cache_control_downgrade(
                                cache_policy,
                                reason=downgrade_reason,
                            )
                            telemetry.set_cache_policy(cache_policy)
                            active_request_plan_metadata = plan.request_plan_metadata(
                                input_mode="cache_control_fallback",
                                continuation_strategy="full_replay",
                                provider_payload=_prompt_estimation_payload(request_payload),
                                sent_provider_payload=_prompt_estimation_payload(request_payload),
                                cache_policy_metadata=cache_policy,
                                extra={"fallback_used": True},
                            )
                            active_request_plan_metadata.update(
                                wire_diagnostics(
                                    request_payload,
                                    history_key="messages",
                                    instructions_key="system",
                                )
                            )
                            telemetry.set_request_plan(active_request_plan_metadata)
                            telemetry.set_request_shape(
                                _request_shape_metadata(
                                    request_payload,
                                    input_mode="cache_control_fallback",
                                )
                            )
                            telemetry.set_token_reconciliation(
                                _token_reconciliation_metadata(
                                    request_payload,
                                    input_mode="cache_control_fallback",
                                )
                            )
                            continue
                        break
            except CooperativeCancellationError:
                raise
            except httpx.DecodingError as e:
                raise_if_cancelled(cancellation_token)
                err = LLMError(
                    f"Anthropic Messages decompression failed: {sanitize_error_text_for_output(e)}"
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
                    f"Anthropic Messages request failed: {sanitize_error_text_for_output(e)}"
                )
                if stream and public_output_emitted:
                    mark_provider_call_non_retryable(err)
                raise err from e
            if response.status_code >= 400:
                raise self._llm_error_from_response(response)
            return _response_with_cache_policy_metadata(
                _validate_omitted_reasoning_response(
                    self._parse_chat_response(
                        response,
                        on_reasoning_delta=telemetry_on_reasoning_delta,
                        reasoning_is_summary=(
                            _payload_requests_summarized_thinking(request_payload)
                        ),
                    )
                ),
                cache_policy,
                active_request_plan_metadata,
            )

        return stamp_response_for_route(
            telemetry.run(
                lambda: run_provider_limited_call(
                    call=_send_request,
                    provider_key=provider_key,
                    provider_concurrency_caps=self.provider_concurrency_caps,
                    retry_settings=self.provider_retry_settings,
                    operation="anthropic_messages_chat",
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
        reasoning_is_summary: bool,
    ) -> LLMResponse:
        accumulator = _AnthropicStreamAccumulator(
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
            reasoning_is_summary=reasoning_is_summary,
        )
        for frame in iter_sse_frames(response.iter_lines()):
            raw_event = parse_sse_json_frame(frame, stream_name="Anthropic Messages stream")
            if not isinstance(raw_event, dict):
                raise LLMError("Anthropic Messages stream emitted non-object JSON event")
            accumulator.handle(frame, raw_event)
        data = accumulator.finish()
        # Streamed thinking was already emitted chunk-by-chunk. Parsing the
        # accumulated message must not emit the same summary a second time.
        return AnthropicMessagesClient._parse_chat_response(
            response=_response_from_json(data),
            reasoning_is_summary=reasoning_is_summary,
        )

    @staticmethod
    def _parse_chat_response(
        response: httpx.Response,
        *,
        reasoning_is_summary: bool,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        try:
            data = response.json()
        except Exception as e:  # noqa: BLE001
            raise LLMError("Anthropic Messages returned non-JSON response") from e
        if not isinstance(data, dict):
            raise LLMError("Unexpected Anthropic Messages payload: expected JSON object")
        content = data.get("content")
        if not isinstance(content, list):
            raise LLMError("Unexpected Anthropic Messages payload: missing content list")

        stop_reason = str(data.get("stop_reason") or "").strip()
        if stop_reason == "refusal":
            raise LLMError("Anthropic Messages refusal")

        for block in content:
            if isinstance(block, dict) and str(block.get("type") or "") == "refusal":
                text = str(block.get("text") or block.get("refusal") or "").strip()
                suffix = f": {text}" if text else ""
                raise LLMError(f"Anthropic Messages refusal{suffix}")

        text = _text_from_content_blocks(content)
        tool_calls = _parse_tool_calls(content)
        if not text and not tool_calls:
            suffix = f" (stop_reason={stop_reason})" if stop_reason else ""
            raise LLMError(f"Anthropic Messages returned no assistant text or tool calls{suffix}")

        summaries = _thinking_summaries_from_content_blocks(content) if reasoning_is_summary else []
        if on_reasoning_delta is not None:
            for summary in summaries:
                on_reasoning_delta(summary)

        response_model = data.get("model") if isinstance(data.get("model"), str) else None
        reasoning = tuple(
            ReasoningOutput(
                text=summary,
                kind=ReasoningOutputKind.SUMMARY,
                provider="anthropic",
            )
            for summary in summaries
        )
        return LLMResponse(
            content=text,
            tool_calls=tool_calls,
            raw=data,
            response_model=response_model,
            usage=_parse_usage(data.get("usage")),
            provider_metadata=_anthropic_provider_metadata(data),
            reasoning=reasoning,
        )
