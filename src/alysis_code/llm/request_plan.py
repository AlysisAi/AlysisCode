from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..request_estimation import (
    estimate_message_tokens,
    estimate_provider_payload_tokens,
    estimate_request_token_breakdown,
    estimate_tool_schema_tokens,
    sanitize_messages_for_estimation,
)
from .cache_control_blocks import cacheable_prefix_message_count


def _stable_payload_hash(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8", errors="surrogatepass")).hexdigest()


def _wire_fingerprint(value: Any) -> tuple[str, int]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8", errors="surrogatepass"
    )
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _wire_request_shape(payload: Mapping[str, Any], history: Any) -> dict[str, Any]:
    """Describe the adapted request without retaining content, names or URLs."""
    images = 0
    media = 0
    text = 0
    # Walk only provider content containers, never JSON embedded inside tool text.
    pending = list(history) if isinstance(history, list) else []
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        kind = item.get("type") if isinstance(item.get("type"), str) else None
        if kind in {"input_image", "image_url", "image"}:
            images += 1
            media += 1
        elif kind in {"input_audio", "audio", "input_file", "file", "document", "video"}:
            media += 1
        elif kind in {"text", "input_text", "output_text"}:
            text += 1
        for key in ("content", "output"):
            content = item.get(key)
            if isinstance(content, list):
                pending.extend(content)
            elif isinstance(content, str) and kind not in {"text", "input_text", "output_text"}:
                text += 1
    tool_count = 0
    pending = list(payload.get("tools") or [])
    while pending:
        tool = pending.pop()
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
            pending.extend(tool["tools"])
        else:
            tool_count += 1
    choice = payload.get("tool_choice")
    if isinstance(choice, dict):
        choice = choice.get("type")
    if choice is None:
        choice = "omitted"
    if not isinstance(choice, str) or choice not in {
        "auto",
        "required",
        "none",
        "function",
        "custom",
        "tool",
        "any",
        "omitted",
    }:
        choice = "other"
    reasoning = payload.get("reasoning")
    effort = (
        reasoning.get("effort") if isinstance(reasoning, dict) else payload.get("reasoning_effort")
    )
    if effort is None:
        effort = "omitted"
    if not isinstance(effort, str) or effort not in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
        "omitted",
    }:
        effort = "other"
    return {
        "wire_image_parts": images,
        "wire_media_parts": media,
        "wire_text_parts": text,
        "wire_tool_count": tool_count,
        "wire_tool_choice": choice,
        "wire_reasoning_effort": effort,
    }


class WireRequestDiagnostics:
    """Content-free comparison of consecutive prepared requests on one client.

    Fingerprint the final transport payload, after provider adaptation. Lengths
    refer to canonical UTF-8 JSON, not HTTP bytes or tokens. This observes local
    prefix stability, not provider cache eligibility, cache hits, or delivery.
    Only hashes and lengths are retained, including for opaque provider items.
    A retry compares against the preceding logical request, not its own first
    attempt. A new client has no comparison, including after process restart.
    """

    def __init__(self) -> None:
        self._previous: (
            tuple[tuple[tuple[str, int], ...], tuple[str, int], tuple[str, int]] | None
        ) = None

    def begin_request(self) -> Callable[..., dict[str, Any]]:
        previous = self._previous

        def describe(
            payload: Mapping[str, Any],
            *,
            history_key: str,
            instructions_key: str | None = None,
        ) -> dict[str, Any]:
            history = payload.get(history_key, [])
            items = tuple(_wire_fingerprint(item) for item in history)
            instructions = _wire_fingerprint(
                payload.get(instructions_key) if instructions_key is not None else None
            )
            tools = _wire_fingerprint(payload.get("tools"))
            history_hash, history_bytes = _wire_fingerprint(history)
            result: dict[str, Any] = {
                "wire_schema_version": 1,
                "wire_history_sha256": history_hash,
                "wire_history_bytes": history_bytes,
                "wire_history_items": len(items),
                "wire_instructions_sha256": instructions[0],
                "wire_instructions_bytes": instructions[1],
                "wire_tools_sha256": tools[0],
                "wire_tools_bytes": tools[1],
                **_wire_request_shape(payload, history),
            }
            if previous is not None:
                old_items, old_instructions, old_tools = previous
                shared = 0
                for old_item, item in zip(old_items, items, strict=False):
                    if old_item != item:
                        break
                    shared += 1
                result.update(
                    wire_previous_history_items=len(old_items),
                    wire_shared_prefix_items=shared,
                    wire_shared_prefix_bytes=sum(size for _, size in items[:shared]),
                    wire_previous_history_prefix_preserved=(shared == len(old_items)),
                    wire_previous_instructions_unchanged=(instructions == old_instructions),
                    wire_previous_tools_unchanged=(tools == old_tools),
                )
            self._previous = items, instructions, tools
            return result

        return describe


def _optional_payload_token_estimate(payload: Any) -> int | None:
    if payload is None:
        return None
    return estimate_provider_payload_tokens(payload)


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _safe_label(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


@dataclass(frozen=True)
class RequestLayout:
    schema_version: int = 1
    input_mode: str = "full"
    continuation_strategy: str = "full_replay"
    cache_strategy: str = "none"
    cache_mode: str = "manual"
    message_count: int = 0
    request_message_count: int = 0
    tool_count: int = 0
    stable_prefix_message_count: int = 0
    dynamic_suffix_message_count: int = 0
    provider_metadata_message_count: int = 0
    stable_prefix_estimated_tokens: int = 0
    dynamic_suffix_estimated_tokens: int = 0
    tool_schema_tokens: int = 0
    total_estimated_tokens: int = 0
    serialized_request_estimate_tokens: int | None = None
    sent_serialized_request_estimate_tokens: int | None = None
    cacheable_prefix_hash: str = ""
    request_messages_signature: str = ""
    tool_schema_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "input_mode": self.input_mode,
            "continuation_strategy": self.continuation_strategy,
            "cache_strategy": self.cache_strategy,
            "cache_mode": self.cache_mode,
            "message_count": self.message_count,
            "request_message_count": self.request_message_count,
            "tool_count": self.tool_count,
            "stable_prefix_message_count": self.stable_prefix_message_count,
            "dynamic_suffix_message_count": self.dynamic_suffix_message_count,
            "provider_metadata_message_count": self.provider_metadata_message_count,
            "stable_prefix_estimated_tokens": self.stable_prefix_estimated_tokens,
            "dynamic_suffix_estimated_tokens": self.dynamic_suffix_estimated_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "total_estimated_tokens": self.total_estimated_tokens,
            "cacheable_prefix_hash": self.cacheable_prefix_hash,
            "request_messages_signature": self.request_messages_signature,
            "tool_schema_hash": self.tool_schema_hash,
        }
        if self.serialized_request_estimate_tokens is not None:
            payload["serialized_request_estimate_tokens"] = self.serialized_request_estimate_tokens
        if self.sent_serialized_request_estimate_tokens is not None:
            payload["sent_serialized_request_estimate_tokens"] = (
                self.sent_serialized_request_estimate_tokens
            )
        for key, value in self.metadata.items():
            if key in payload:
                continue
            if isinstance(value, bool):
                payload[key] = value
                continue
            parsed_int = _safe_int(value)
            if parsed_int is not None:
                payload[key] = parsed_int
                continue
            if value is not None:
                payload[key] = _safe_label(value)
        return payload

    @classmethod
    def from_payload(cls, payload: Any) -> RequestLayout | None:
        if not isinstance(payload, Mapping):
            return None

        metadata: dict[str, Any] = {}
        known = set(cls.__dataclass_fields__)
        for key, value in payload.items():
            if key not in known:
                metadata[str(key)] = copy.deepcopy(value)

        return cls(
            schema_version=_safe_int(payload.get("schema_version")) or 1,
            input_mode=_safe_label(payload.get("input_mode"), "full"),
            continuation_strategy=_safe_label(
                payload.get("continuation_strategy"),
                "full_replay",
            ),
            cache_strategy=_safe_label(payload.get("cache_strategy"), "none"),
            cache_mode=_safe_label(payload.get("cache_mode"), "manual"),
            message_count=_safe_int(payload.get("message_count")) or 0,
            request_message_count=_safe_int(payload.get("request_message_count")) or 0,
            tool_count=_safe_int(payload.get("tool_count")) or 0,
            stable_prefix_message_count=(
                _safe_int(payload.get("stable_prefix_message_count")) or 0
            ),
            dynamic_suffix_message_count=(
                _safe_int(payload.get("dynamic_suffix_message_count")) or 0
            ),
            provider_metadata_message_count=(
                _safe_int(payload.get("provider_metadata_message_count")) or 0
            ),
            stable_prefix_estimated_tokens=(
                _safe_int(payload.get("stable_prefix_estimated_tokens")) or 0
            ),
            dynamic_suffix_estimated_tokens=(
                _safe_int(payload.get("dynamic_suffix_estimated_tokens")) or 0
            ),
            tool_schema_tokens=_safe_int(payload.get("tool_schema_tokens")) or 0,
            total_estimated_tokens=_safe_int(payload.get("total_estimated_tokens")) or 0,
            serialized_request_estimate_tokens=_safe_int(
                payload.get("serialized_request_estimate_tokens")
            ),
            sent_serialized_request_estimate_tokens=_safe_int(
                payload.get("sent_serialized_request_estimate_tokens")
            ),
            cacheable_prefix_hash=_safe_label(payload.get("cacheable_prefix_hash")),
            request_messages_signature=_safe_label(payload.get("request_messages_signature")),
            tool_schema_hash=_safe_label(payload.get("tool_schema_hash")),
            metadata=metadata,
        )


def _provider_metadata_message_count(messages: list[dict[str, Any]]) -> int:
    return sum(1 for message in messages if "_alysis_provider_metadata" in message)


def _policy_label(policy: Mapping[str, Any] | None, key: str) -> str | None:
    if not isinstance(policy, Mapping):
        return None
    value = policy.get(key)
    return str(value).strip() if value is not None else None


def _merge_metadata(*items: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for key, value in item.items():
            merged[str(key)] = copy.deepcopy(value)
    return merged


@dataclass(frozen=True)
class RequestCachePlan:
    strategy: str = "none"
    mode: str = "manual"
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None
    anthropic_cache_control_enabled: bool = False
    anthropic_cache_control_ttl: str = "5m"

    def __post_init__(self) -> None:
        mode = str(self.mode or "manual").strip().lower()
        if mode not in {"manual", "automatic", "auto", "off"}:
            mode = "manual"
        if mode == "auto":
            mode = "automatic"
        ttl = str(self.anthropic_cache_control_ttl or "5m").strip().lower()
        if ttl not in {"5m", "1h"}:
            ttl = "5m"
        object.__setattr__(self, "strategy", str(self.strategy or "none").strip() or "none")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(
            self,
            "prompt_cache_key",
            None if mode == "off" else str(self.prompt_cache_key or "").strip() or None,
        )
        object.__setattr__(
            self,
            "prompt_cache_retention",
            None if mode == "off" else str(self.prompt_cache_retention or "").strip() or None,
        )
        object.__setattr__(
            self,
            "anthropic_cache_control_enabled",
            False if mode == "off" else bool(self.anthropic_cache_control_enabled),
        )
        object.__setattr__(self, "anthropic_cache_control_ttl", ttl)

    def anthropic_cache_control_payload(self) -> dict[str, str] | None:
        if not self.anthropic_cache_control_enabled:
            return None
        payload = {"type": "ephemeral"}
        if self.anthropic_cache_control_ttl == "1h":
            payload["ttl"] = "1h"
        return payload

    def anthropic_cache_policy_metadata(self) -> dict[str, Any] | None:
        if not self.anthropic_cache_control_enabled:
            return None
        return {
            "strategy": "anthropic_cache_control",
            "enabled": True,
            "ttl": self.anthropic_cache_control_ttl,
            "mode": self.mode if self.mode != "off" else "manual",
        }

    def openai_prompt_cache_policy_metadata(self) -> dict[str, Any] | None:
        if (
            self.mode == "off"
            or self.strategy != "openai_prompt_cache"
            or (not self.prompt_cache_key and not self.prompt_cache_retention)
        ):
            return None
        metadata: dict[str, Any] = {
            "strategy": "openai_prompt_cache",
            "enabled": True,
            "mode": self.mode if self.mode != "off" else "manual",
        }
        if self.prompt_cache_retention:
            metadata["retention"] = self.prompt_cache_retention
        return metadata


@dataclass(frozen=True)
class LLMRequestPlan:
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    tool_choice: Any | None = None
    response_format: dict[str, Any] | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    cache: RequestCachePlan = field(default_factory=RequestCachePlan)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_chat_args(
        cls,
        *,
        messages: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
        tools: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cache: RequestCachePlan | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMRequestPlan:
        return cls(
            messages=tuple(
                copy.deepcopy(message) for message in (messages or []) if isinstance(message, dict)
            ),
            tools=tuple(copy.deepcopy(tool) for tool in (tools or []) if isinstance(tool, dict)),
            tool_choice=copy.deepcopy(tool_choice),
            response_format=(
                copy.deepcopy(response_format) if isinstance(response_format, dict) else None
            ),
            stream=bool(stream),
            temperature=temperature,
            max_tokens=max_tokens,
            cache=cache or RequestCachePlan(),
            metadata=copy.deepcopy(metadata or {}),
        )

    def message_list(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(message) for message in self.messages]

    def tool_list(self) -> list[dict[str, Any]] | None:
        tools = [copy.deepcopy(tool) for tool in self.tools]
        return tools or None

    def with_cache(self, cache: RequestCachePlan) -> LLMRequestPlan:
        return replace(self, cache=cache)

    def layout(
        self,
        *,
        input_mode: str = "full",
        continuation_strategy: str | None = None,
        provider_payload: Mapping[str, Any] | None = None,
        sent_provider_payload: Mapping[str, Any] | None = None,
        cache_policy_metadata: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> RequestLayout:
        messages = self.message_list()
        tools = self.tool_list() or []
        sanitized_messages = sanitize_messages_for_estimation(messages)
        stable_prefix_count = min(
            cacheable_prefix_message_count(sanitized_messages),
            len(sanitized_messages),
        )
        stable_prefix = sanitized_messages[:stable_prefix_count]
        dynamic_suffix = sanitized_messages[stable_prefix_count:]
        tool_schema_tokens = estimate_tool_schema_tokens(tools)
        stable_prefix_tokens = estimate_message_tokens(stable_prefix)
        dynamic_suffix_tokens = estimate_message_tokens(dynamic_suffix)
        breakdown = estimate_request_token_breakdown(
            messages=messages,
            tool_list=tools,
            pinned_prefix_len=stable_prefix_count,
        )
        policy_cache_strategy = _policy_label(cache_policy_metadata, "strategy")
        policy_cache_mode = _policy_label(cache_policy_metadata, "mode")
        metadata = _merge_metadata(self.metadata, extra)
        return RequestLayout(
            input_mode=_safe_label(input_mode, "full"),
            continuation_strategy=_safe_label(
                continuation_strategy,
                "previous_response_id" if input_mode == "previous_response_id" else "full_replay",
            ),
            cache_strategy=_safe_label(policy_cache_strategy, self.cache.strategy),
            cache_mode=_safe_label(policy_cache_mode, self.cache.mode),
            message_count=len(sanitized_messages),
            request_message_count=len(sanitized_messages),
            tool_count=len(tools),
            stable_prefix_message_count=stable_prefix_count,
            dynamic_suffix_message_count=len(dynamic_suffix),
            provider_metadata_message_count=_provider_metadata_message_count(messages),
            stable_prefix_estimated_tokens=stable_prefix_tokens,
            dynamic_suffix_estimated_tokens=dynamic_suffix_tokens,
            tool_schema_tokens=tool_schema_tokens,
            total_estimated_tokens=breakdown.total_tokens,
            serialized_request_estimate_tokens=_optional_payload_token_estimate(provider_payload),
            sent_serialized_request_estimate_tokens=_optional_payload_token_estimate(
                sent_provider_payload
            ),
            cacheable_prefix_hash=_stable_payload_hash(stable_prefix) if stable_prefix else "",
            request_messages_signature=_stable_payload_hash(sanitized_messages),
            tool_schema_hash=_stable_payload_hash(tools) if tools else "",
            metadata=metadata,
        )

    def request_plan_metadata(
        self,
        *,
        input_mode: str = "full",
        continuation_strategy: str | None = None,
        provider_payload: Mapping[str, Any] | None = None,
        sent_provider_payload: Mapping[str, Any] | None = None,
        cache_policy_metadata: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.layout(
            input_mode=input_mode,
            continuation_strategy=continuation_strategy,
            provider_payload=provider_payload,
            sent_provider_payload=sent_provider_payload,
            cache_policy_metadata=cache_policy_metadata,
            extra=extra,
        ).to_payload()
