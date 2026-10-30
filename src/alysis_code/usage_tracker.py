from __future__ import annotations

import copy
import json
import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from .llm.metadata import strip_provider_metadata_from_message
from .llm.types import BillingMode, CostSource, UsageConfidence, UsageContract, UsageSource
from .llm.types import parse_token_count as _safe_int
from .model_registry import ModelMeta, ModelRegistry
from .provider_telemetry import base_url_host
from .request_estimation import (
    RequestTokenBreakdown,
    estimate_request_token_breakdown,
    request_contains_media,
    request_message_signatures,
    tool_schema_signature,
)
from .session_store import read_session_events
from .token_budget import compute_input_budget, estimate_tokens


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class UsageRecord:
    timestamp: str
    role: str
    requested_model: str
    response_model: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    cost_usd: float | None
    usage_source: str
    billing_mode: str = BillingMode.METERED_API.value
    cost_source: str = CostSource.UNKNOWN.value
    provider_cost_usd: float | None = None
    usage_source_detail: str = UsageSource.LOCAL_ESTIMATE.value
    usage_confidence: str = UsageConfidence.ESTIMATED.value
    output_includes_reasoning: bool = True
    usage_schema_version: int = 7
    input_tokens_scope: str = "request"
    provider_key: str | None = None
    protocol: str | None = None
    base_url_host: str | None = None
    operation: str | None = None
    request_mode: str | None = None
    cache_strategy: str | None = None
    request_plan: dict[str, Any] | None = None
    request_has_media: bool = False
    cache_read_input_cost_per_token: float | None = None
    cache_creation_input_cost_per_token: float | None = None
    cache_creation_5m_input_cost_per_token: float | None = None
    cache_creation_1h_input_cost_per_token: float | None = None
    reasoning_output_cost_per_token: float | None = None
    cached_prompt_tokens: int | None = None
    uncached_prompt_tokens: int | None = None
    input_tokens_uncached: int | None = None
    input_tokens_uncached_derived: bool = False
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_creation_5m_input_tokens: int | None = None
    cache_creation_1h_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    raw_provider_usage: dict[str, Any] | None = None
    cache_cost_pricing_missing: bool = False
    request_token_estimate: RequestTokenBreakdown | None = None
    prompt_estimate_tokens: int | None = None
    prompt_estimate_error_tokens: int | None = None
    prompt_estimate_error_ratio: float | None = None
    raw_api_prompt_tokens: int | None = None
    raw_api_completion_tokens: int | None = None
    raw_api_total_tokens: int | None = None
    usage_correction_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_type": "llm_usage",
            "usage_schema_version": self.usage_schema_version,
            "input_tokens_scope": self.input_tokens_scope,
            "timestamp": self.timestamp,
            "role": self.role,
            "requested_model": self.requested_model,
            "response_model": self.response_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "input_cost_per_token": self.input_cost_per_token,
            "output_cost_per_token": self.output_cost_per_token,
            "cache_read_input_cost_per_token": self.cache_read_input_cost_per_token,
            "cache_creation_input_cost_per_token": self.cache_creation_input_cost_per_token,
            "cache_creation_5m_input_cost_per_token": (self.cache_creation_5m_input_cost_per_token),
            "cache_creation_1h_input_cost_per_token": (self.cache_creation_1h_input_cost_per_token),
            "reasoning_output_cost_per_token": self.reasoning_output_cost_per_token,
            "cost_usd": self.cost_usd,
            "billing_mode": self.billing_mode,
            "cost_source": self.cost_source,
            "provider_cost_usd": self.provider_cost_usd,
            "usage_source": self.usage_source,
            "usage_source_detail": self.usage_source_detail,
            "usage_confidence": self.usage_confidence,
            "output_includes_reasoning": self.output_includes_reasoning,
            "provider_key": self.provider_key,
            "protocol": self.protocol,
            "base_url_host": self.base_url_host,
            "operation": self.operation,
            "request_mode": self.request_mode,
            "cache_strategy": self.cache_strategy,
            "request_plan": copy.deepcopy(self.request_plan),
            "request_has_media": self.request_has_media,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "uncached_prompt_tokens": self.uncached_prompt_tokens,
            "input_tokens_uncached": self.input_tokens_uncached,
            "input_tokens_uncached_derived": self.input_tokens_uncached_derived,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "raw_provider_usage": copy.deepcopy(self.raw_provider_usage),
            "cache_cost_pricing_missing": self.cache_cost_pricing_missing,
            "request_token_estimate": (
                self.request_token_estimate.to_payload()
                if self.request_token_estimate is not None
                else None
            ),
            "prompt_estimate_tokens": self.prompt_estimate_tokens,
            "prompt_estimate_error_tokens": self.prompt_estimate_error_tokens,
            "prompt_estimate_error_ratio": self.prompt_estimate_error_ratio,
            "raw_api_prompt_tokens": self.raw_api_prompt_tokens,
            "raw_api_completion_tokens": self.raw_api_completion_tokens,
            "raw_api_total_tokens": self.raw_api_total_tokens,
            "usage_correction_reason": self.usage_correction_reason,
        }


@dataclass(frozen=True)
class ContextLeft:
    model_name: str
    max_input_tokens: int | None
    used_input_tokens: int
    remaining_tokens: int | None
    percent_left: float | None
    source: str
    context_window_tokens: int | None = None
    context_window_remaining_tokens: int | None = None
    context_window_percent_left: float | None = None
    effective_input_budget: int | None = None
    effective_remaining_tokens: int | None = None
    effective_percent_left: float | None = None
    startup_baseline_tokens: int = 0
    dynamic_context_budget_tokens: int | None = None
    dynamic_context_used_tokens: int = 0
    dynamic_context_remaining_tokens: int | None = None
    dynamic_context_percent_left: float | None = None
    token_count_source: str = UsageSource.LOCAL_ESTIMATE.value
    token_count_confidence: str = UsageConfidence.ESTIMATED.value
    local_request_estimate_tokens: int = 0
    anchor_token_count_source: str | None = None
    anchor_token_count_confidence: str | None = None
    provider_projection_applied: bool = False
    capacity_provider_key: str | None = None
    context_window_source: str | None = None
    max_output_tokens: int | None = None
    max_output_source: str | None = None
    safety_margin_tokens: int = 0


@dataclass(frozen=True)
class RequestContextMeasurement:
    """Provider-visible input measurement anchored to persistent session state.

    ``input_tokens`` measures the fully assembled provider request, including
    ephemeral controller messages. ``anchor_estimate_tokens`` is the local
    estimate of that same full request. ``persistent_anchor_estimate_tokens``
    is the local estimate of durable session state at that instant, allowing
    the HUD to add later persistent growth without losing recurring ephemeral
    request overhead.
    """

    input_tokens: int
    anchor_estimate_tokens: int
    source: str
    confidence: str
    persistent_anchor_estimate_tokens: int = 0
    requested_model: str = ""
    provider_key: str | None = None
    protocol: str | None = None
    base_url_host: str | None = None
    operation: str | None = None
    request_mode: str | None = None
    cache_strategy: str | None = None
    request_message_signatures: tuple[str, ...] = ()
    persistent_message_signatures: tuple[str, ...] = ()
    tool_schema_signature: str = ""
    request_has_media: bool = False
    persistent_has_media: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("anchor_estimate_tokens", self.anchor_estimate_tokens),
            ("persistent_anchor_estimate_tokens", self.persistent_anchor_estimate_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    def matches_route(
        self,
        *,
        requested_model: str,
        provider_key: str | None,
        protocol: str | None,
        base_url_host: str | None,
    ) -> bool:
        expected = (
            (self.requested_model, requested_model),
            (self.provider_key, provider_key),
            (self.protocol, protocol),
            (self.base_url_host, base_url_host),
        )
        return all(not left or str(left) == str(right or "") for left, right in expected)

    def projection_kind(
        self,
        *,
        messages: list[dict[str, Any]],
        tool_list: list[dict[str, Any]] | None,
    ) -> str | None:
        current = request_message_signatures(messages)
        if not self.request_message_signatures or self.anchor_estimate_tokens <= 0:
            return None
        if tool_schema_signature(tool_list) != self.tool_schema_signature:
            return None
        if current == self.request_message_signatures:
            return "exact_request"
        prefix = self.persistent_message_signatures
        if (
            not prefix
            or self.persistent_anchor_estimate_tokens <= 0
            or (self.request_has_media and not self.persistent_has_media)
            or len(current) < len(prefix)
            or current[: len(prefix)] != prefix
        ):
            return None
        if request_contains_media(messages[len(prefix) :]):
            return None
        return "append_only_projection"


@dataclass
class _ModelUsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    known_cost_usd: float = 0.0
    known_cost_calls: int = 0
    unknown_cost_count: int = 0
    provider_reported_cost_calls: int = 0
    catalog_estimated_cost_calls: int = 0
    subscription_calls: int = 0
    included_calls: int = 0
    local_calls: int = 0
    api_usage_calls: int = 0
    estimate_usage_calls: int = 0
    authoritative_usage_calls: int = 0
    reported_usage_calls: int = 0
    cached_prompt_tokens: int = 0
    uncached_prompt_tokens: int = 0
    input_tokens_uncached: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    input_tokens_uncached_reported_calls: int = 0
    input_tokens_uncached_derived_calls: int = 0
    cache_read_reported_calls: int = 0
    cache_creation_reported_calls: int = 0
    reasoning_tokens: int = 0
    cache_cost_pricing_missing_calls: int = 0
    estimated_bootstrap_prompt_tokens: int = 0
    estimated_tool_schema_tokens: int = 0
    estimated_live_conversation_history_tokens: int = 0
    estimated_inline_tool_transcript_tokens: int = 0
    estimated_memory_summary_tokens: int = 0
    estimated_pins_tokens: int = 0
    estimated_total_request_tokens: int = 0
    tool_schema_budget_reported_calls: int = 0
    tool_schema_budget_exceeded_calls: int = 0
    tool_schema_budget_overage_tokens: int = 0
    tool_schema_largest_tool_tokens: int = 0
    corrected_usage_calls: int = 0
    prompt_estimate_calibration_calls: int = 0
    prompt_estimate_error_ratio_sum: float = 0.0
    prompt_estimate_error_ratio_max: float = 0.0
    prompt_estimate_underestimate_calls: int = 0
    prompt_estimate_overestimate_calls: int = 0


def _calibration_group_key(record: UsageRecord) -> tuple[str, str, str, str, str, str, str]:
    return (
        record.requested_model.strip() or "unknown-model",
        record.provider_key or "",
        record.protocol or "",
        record.base_url_host or "",
        record.operation or "",
        record.request_mode or "",
        record.cache_strategy or "",
    )


def _record_matches_calibration_filters(
    record: UsageRecord,
    *,
    requested_model: str,
    provider_key: str,
    protocol: str,
    base_url_host_filter: str,
    operation: str,
    request_mode: str,
    cache_strategy: str,
) -> bool:
    return (
        (not requested_model or record.requested_model.strip() == requested_model)
        and (not provider_key or (record.provider_key or "") == provider_key)
        and (not protocol or (record.protocol or "") == protocol)
        and (not base_url_host_filter or (record.base_url_host or "") == base_url_host_filter)
        and (not operation or (record.operation or "") == operation)
        and (not request_mode or (record.request_mode or "") == request_mode)
        and (not cache_strategy or (record.cache_strategy or "") == cache_strategy)
    )


def _messages_without_provider_metadata(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        strip_provider_metadata_from_message(message)
        for message in messages
        if isinstance(message, dict)
    ]


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    if not messages:
        return 0
    serialized = json.dumps(
        _messages_without_provider_metadata(messages),
        ensure_ascii=False,
        sort_keys=True,
    )
    return estimate_tokens(serialized)


def _completion_payload_for_estimation(content: str, tool_calls: list[Any]) -> str:
    if content.strip():
        base = content
    else:
        base = ""
    if tool_calls:
        base += "\n" + json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
    return base


def estimate_completion_tokens(content: str, tool_calls: list[Any]) -> int:
    base = _completion_payload_for_estimation(content, tool_calls)
    if not base.strip():
        return 0
    return estimate_tokens(base)


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _safe_label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_request_plan_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    safe: dict[str, Any] = {}
    for key in (
        "input_mode",
        "continuation_strategy",
        "cache_strategy",
        "cache_mode",
        "cacheable_prefix_hash",
        "request_messages_signature",
        "tool_schema_hash",
    ):
        value = _safe_label(payload.get(key))
        if value is not None:
            safe[key] = value
    # Wire diagnostics are an allowlist of content-free values. Never pass through
    # an arbitrary provider metadata field (it may contain screen text or secrets).
    for key in ("wire_history_sha256", "wire_instructions_sha256", "wire_tools_sha256"):
        value = payload.get(key)
        if (
            isinstance(value, str)
            and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)
        ):
            safe[key] = value
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
        value = payload.get(key)
        if isinstance(value, str) and value in allowed:
            safe[key] = value
    for key in (
        "wire_previous_history_prefix_preserved",
        "wire_previous_instructions_unchanged",
        "wire_previous_tools_unchanged",
    ):
        if isinstance(payload.get(key), bool):
            safe[key] = payload[key]
    for key in ("previous_response_id_used", "fallback_used", "stream"):
        if key in payload:
            safe[key] = bool(payload.get(key))
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
        value = _safe_int(payload.get(key))
        if value is not None:
            safe[key] = value
    return safe or None


def _request_plan_from_response(response: Any | None) -> dict[str, Any] | None:
    metadata = getattr(response, "provider_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    direct = _safe_request_plan_payload(metadata.get("request_plan"))
    if direct is not None:
        return direct
    for value in metadata.values():
        if not isinstance(value, Mapping):
            continue
        nested = _safe_request_plan_payload(value.get("request_plan"))
        if nested is not None:
            return nested
    return None


def _cache_strategy_from_response(response: Any | None) -> str | None:
    metadata = getattr(response, "provider_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    for value in metadata.values():
        if not isinstance(value, Mapping):
            continue
        cache_policy = value.get("cache_policy")
        if isinstance(cache_policy, Mapping):
            strategy = _safe_label(cache_policy.get("strategy"))
            if strategy is not None:
                return strategy
    return None


def _protocol_from_client(client: Any | None) -> str | None:
    for attr in ("protocol", "provider_protocol", "protocol_name"):
        value = _safe_label(getattr(client, attr, None))
        if value is not None:
            return value
    module = str(getattr(getattr(client, "__class__", None), "__module__", "") or "")
    leaf = module.rsplit(".", 1)[-1]
    if leaf in {
        "openai_responses",
        "openai_compat",
        "anthropic_messages",
        "gemini_generate_content",
        "gemini_interactions",
    }:
        return leaf
    class_name = str(getattr(getattr(client, "__class__", None), "__name__", "") or "")
    if class_name:
        return class_name
    return None


def usage_context_from_client_response(
    *,
    client: Any | None,
    response: Any | None,
    operation: str,
    request_plan: Mapping[str, Any] | None = None,
    cache_strategy: str | None = None,
    request_mode: str | None = None,
) -> dict[str, Any]:
    safe_plan = _safe_request_plan_payload(request_plan) or _request_plan_from_response(response)
    inferred_cache_strategy = (
        _safe_label(cache_strategy)
        or (
            str(safe_plan.get("cache_strategy"))
            if safe_plan and safe_plan.get("cache_strategy")
            else None
        )
        or _cache_strategy_from_response(response)
    )
    inferred_request_mode = (
        _safe_label(request_mode)
        or (str(safe_plan.get("input_mode")) if safe_plan and safe_plan.get("input_mode") else None)
        or "full"
    )
    usage_contract = getattr(client, "usage_contract", None)
    if not isinstance(usage_contract, UsageContract):
        usage_contract = UsageContract(
            response_usage_confidence=(
                UsageConfidence.AUTHORITATIVE
                if bool(getattr(client, "usage_counts_authoritative", False))
                else UsageConfidence.REPORTED
            )
        )
    contract_billing_mode = getattr(usage_contract, "billing_mode", BillingMode.METERED_API)
    billing_mode_value = str(
        getattr(contract_billing_mode, "value", contract_billing_mode)
        or BillingMode.METERED_API.value
    )
    return {
        "provider_key": _safe_label(getattr(client, "provider_key", None)),
        "protocol": _protocol_from_client(client),
        "base_url_host": base_url_host(getattr(client, "base_url", None)),
        "operation": _safe_label(operation),
        "request_mode": inferred_request_mode,
        "cache_strategy": inferred_cache_strategy,
        "request_plan": safe_plan,
        "api_usage_counts_authoritative": usage_contract.response_usage_authoritative,
        "api_prompt_tokens_authoritative": usage_contract.response_usage_authoritative,
        "api_usage_confidence": usage_contract.response_usage_confidence.value,
        "api_usage_source_detail": UsageSource.PROVIDER_RESPONSE.value,
        "api_output_includes_reasoning": (usage_contract.normalized_output_includes_reasoning),
        "billing_mode": billing_mode_value,
    }


def _prompt_estimate_calibration(
    *,
    api_prompt_tokens: int | None,
    estimated_prompt_tokens: int,
    input_tokens_scope: str,
    usage_source: str,
    api_prompt_tokens_authoritative: bool = False,
) -> tuple[int | None, float | None]:
    if (
        api_prompt_tokens is None
        or estimated_prompt_tokens <= 0
        or (usage_source != "api" and not api_prompt_tokens_authoritative)
        or input_tokens_scope != "request"
    ):
        return None, None
    error_tokens = api_prompt_tokens - estimated_prompt_tokens
    return error_tokens, api_prompt_tokens / max(1, estimated_prompt_tokens)


def _robust_calibration_ratios(values: Iterable[float]) -> list[float]:
    """Return a coherent route-local sample without imposing a universal cap."""

    ratios = [float(value) for value in values if math.isfinite(value) and value > 0]
    if len(ratios) < 3:
        return []
    center = float(median(ratios))
    deviations = [abs(value - center) for value in ratios]
    mad = float(median(deviations))
    # An evenly split or otherwise high-spread sample has no defensible center.
    # Reject it instead of letting the uncapped upper mode become the p90.
    if center <= 0 or (mad > 0 and (mad / center) > 0.25):
        return []
    tolerance = max(0.05, abs(center) * 0.10) if mad == 0 else max(0.05, mad * 5.2)
    coherent = [value for value in ratios if abs(value - center) <= tolerance]
    # A valid cluster must contain at least three calls and a strict majority;
    # equal-sized modes remain unstable until one shape dominates.
    return coherent if len(coherent) >= 3 and len(coherent) * 2 > len(ratios) else []


def _resolve_model_rate(
    requested_meta: ModelMeta,
    response_meta: ModelMeta | None,
    field: str,
) -> float | None:
    value = getattr(requested_meta, field, None)
    if value is None and response_meta is not None:
        value = getattr(response_meta, field, None)
    return parse_rate(value)


def parse_rate(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


@dataclass(frozen=True)
class _CostComputation:
    cost_usd: float | None
    cache_cost_pricing_missing: bool
    cost_source: CostSource = CostSource.UNKNOWN


def _usage_cost_usd(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    input_tokens_uncached: int | None,
    cache_read_input_tokens: int | None,
    cache_creation_input_tokens: int | None,
    cache_creation_5m_input_tokens: int | None,
    cache_creation_1h_input_tokens: int | None,
    reasoning_tokens: int | None,
    input_rate: float | None,
    output_rate: float | None,
    cache_read_input_rate: float | None,
    cache_creation_input_rate: float | None,
    cache_creation_5m_input_rate: float | None,
    cache_creation_1h_input_rate: float | None,
    reasoning_output_rate: float | None,
) -> _CostComputation:
    if input_rate is None or output_rate is None:
        return _CostComputation(cost_usd=None, cache_cost_pricing_missing=False)

    read_tokens = max(0, cache_read_input_tokens or 0)
    creation_5m_tokens = max(0, cache_creation_5m_input_tokens or 0)
    creation_1h_tokens = max(0, cache_creation_1h_input_tokens or 0)
    creation_total_tokens = max(0, cache_creation_input_tokens or 0)
    creation_ttl_tokens = creation_5m_tokens + creation_1h_tokens
    creation_generic_tokens = max(0, creation_total_tokens - creation_ttl_tokens)
    if creation_total_tokens == 0:
        creation_generic_tokens = 0

    cache_pricing_missing = False
    cost = 0.0
    uncached_tokens = input_tokens_uncached
    if uncached_tokens is None:
        uncached_tokens = max(0, prompt_tokens - read_tokens - creation_total_tokens)
    cost += max(0, uncached_tokens) * input_rate

    if read_tokens > 0:
        if cache_read_input_rate is None:
            cache_pricing_missing = True
        else:
            cost += read_tokens * cache_read_input_rate

    if creation_5m_tokens > 0:
        rate = (
            cache_creation_5m_input_rate
            if cache_creation_5m_input_rate is not None
            else cache_creation_input_rate
        )
        if rate is None:
            cache_pricing_missing = True
        else:
            cost += creation_5m_tokens * rate

    if creation_1h_tokens > 0:
        rate = (
            cache_creation_1h_input_rate
            if cache_creation_1h_input_rate is not None
            else cache_creation_input_rate
        )
        if rate is None:
            cache_pricing_missing = True
        else:
            cost += creation_1h_tokens * rate

    if creation_generic_tokens > 0:
        if cache_creation_input_rate is None:
            cache_pricing_missing = True
        else:
            cost += creation_generic_tokens * cache_creation_input_rate

    billed_completion_tokens = max(0, completion_tokens)
    reasoning = max(0, reasoning_tokens or 0)
    if reasoning > 0 and reasoning_output_rate is not None:
        visible_completion_tokens = max(0, billed_completion_tokens - reasoning)
        cost += visible_completion_tokens * output_rate
        cost += reasoning * reasoning_output_rate
    else:
        cost += billed_completion_tokens * output_rate

    if cache_pricing_missing:
        return _CostComputation(cost_usd=None, cache_cost_pricing_missing=True)
    return _CostComputation(
        cost_usd=cost,
        cache_cost_pricing_missing=False,
        cost_source=CostSource.CATALOG_ESTIMATE,
    )


def build_usage_record(
    *,
    role: str,
    requested_model: str,
    response_model: str | None,
    messages: list[dict[str, Any]],
    response_content: str,
    response_tool_calls: list[Any],
    api_prompt_tokens: int | None,
    api_completion_tokens: int | None,
    api_total_tokens: int | None,
    registry: ModelRegistry,
    api_usage: Any | None = None,
    api_cached_prompt_tokens: int | None = None,
    api_input_tokens_uncached: int | None = None,
    api_input_tokens_uncached_derived: bool | None = None,
    api_cache_read_input_tokens: int | None = None,
    api_cache_creation_input_tokens: int | None = None,
    api_cache_creation_5m_input_tokens: int | None = None,
    api_cache_creation_1h_input_tokens: int | None = None,
    api_reasoning_tokens: int | None = None,
    api_provider_cost_usd: float | None = None,
    api_raw_provider_usage: dict[str, Any] | None = None,
    tool_list: list[dict[str, Any]] | None = None,
    pinned_prefix_len: int = 0,
    provider_key: str | None = None,
    protocol: str | None = None,
    base_url_host: str | None = None,
    operation: str | None = None,
    request_mode: str | None = None,
    cache_strategy: str | None = None,
    request_plan: Mapping[str, Any] | None = None,
    api_usage_counts_authoritative: bool = False,
    api_prompt_tokens_authoritative: bool | None = None,
    api_usage_confidence: str = UsageConfidence.REPORTED.value,
    api_usage_source_detail: str = UsageSource.PROVIDER_RESPONSE.value,
    api_output_includes_reasoning: bool = True,
    billing_mode: str = BillingMode.METERED_API.value,
) -> UsageRecord:
    prompt_tokens_authoritative = (
        bool(api_usage_counts_authoritative)
        if api_prompt_tokens_authoritative is None
        else bool(api_prompt_tokens_authoritative)
    )
    request_token_estimate = estimate_request_token_breakdown(
        messages=messages,
        tool_list=tool_list,
        pinned_prefix_len=pinned_prefix_len,
    )
    estimated_prompt_tokens = request_token_estimate.total_tokens or estimate_prompt_tokens(
        messages
    )
    safe_request_plan = _safe_request_plan_payload(request_plan)
    provider_prompt_estimate_tokens = (
        _safe_int(safe_request_plan.get("serialized_request_estimate_tokens"))
        if safe_request_plan is not None
        else None
    )
    fallback_prompt_estimate_tokens = (
        provider_prompt_estimate_tokens
        if provider_prompt_estimate_tokens is not None
        else estimated_prompt_tokens
    )
    estimated_completion_tokens = estimate_completion_tokens(response_content, response_tool_calls)
    input_tokens_scope = getattr(api_usage, "input_tokens_scope", "request")
    if api_usage is not None:
        reported_output_includes_reasoning = bool(
            getattr(api_usage, "output_includes_reasoning", True)
        )
        reported_total_includes_reasoning = bool(
            getattr(api_usage, "total_includes_reasoning", True)
        )
        normalizer = getattr(api_usage, "normalized", None)
        if callable(normalizer):
            api_usage = normalizer()
            api_output_includes_reasoning = bool(
                getattr(api_usage, "output_includes_reasoning", True)
            )
            if not reported_output_includes_reasoning:
                api_completion_tokens = getattr(api_usage, "completion_tokens", None)
            if not reported_total_includes_reasoning:
                api_total_tokens = getattr(api_usage, "total_tokens", None)
        if api_cached_prompt_tokens is None:
            api_cached_prompt_tokens = getattr(api_usage, "cached_prompt_tokens", None)
        if api_input_tokens_uncached is None:
            api_input_tokens_uncached = getattr(api_usage, "input_tokens_uncached", None)
            if api_input_tokens_uncached_derived is None:
                api_input_tokens_uncached_derived = bool(
                    getattr(api_usage, "input_tokens_uncached_derived", False)
                )
        if api_cache_read_input_tokens is None:
            api_cache_read_input_tokens = getattr(api_usage, "cache_read_input_tokens", None)
        if api_cache_creation_input_tokens is None:
            api_cache_creation_input_tokens = getattr(
                api_usage,
                "cache_creation_input_tokens",
                None,
            )
        if api_cache_creation_5m_input_tokens is None:
            api_cache_creation_5m_input_tokens = getattr(
                api_usage,
                "cache_creation_5m_input_tokens",
                None,
            )
        if api_cache_creation_1h_input_tokens is None:
            api_cache_creation_1h_input_tokens = getattr(
                api_usage,
                "cache_creation_1h_input_tokens",
                None,
            )
        if api_reasoning_tokens is None:
            api_reasoning_tokens = getattr(api_usage, "reasoning_tokens", None)
        if api_provider_cost_usd is None:
            api_provider_cost_usd = getattr(api_usage, "provider_cost_usd", None)
        if api_raw_provider_usage is None:
            api_raw_provider_usage = getattr(api_usage, "raw_provider_usage", None)

    raw_api_prompt_tokens = _safe_int(api_prompt_tokens)
    raw_api_completion_tokens = _safe_int(api_completion_tokens)
    raw_api_total_tokens = _safe_int(api_total_tokens)
    prompt_tokens = raw_api_prompt_tokens
    completion_tokens = raw_api_completion_tokens
    total_tokens = raw_api_total_tokens
    prompt_measurement_source = str(api_usage_source_detail or "").strip()
    prompt_measurement_is_local = prompt_measurement_source == UsageSource.LOCAL_ESTIMATE.value
    any_provider_usage_reported = any(
        value is not None for value in (raw_api_completion_tokens, raw_api_total_tokens)
    ) or (raw_api_prompt_tokens is not None and not prompt_measurement_is_local)
    usage_source = "estimate" if prompt_measurement_is_local else "api"
    # A local estimate cannot invalidate a provider measurement. Preserve each
    # reported component independently, even for partial or unusual payloads.
    if prompt_tokens is None:
        prompt_tokens = fallback_prompt_estimate_tokens
        usage_source = "estimate"
    if completion_tokens is None:
        completion_tokens = estimated_completion_tokens
        usage_source = "estimate"
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    if usage_source == "estimate":
        usage_source_detail = (
            UsageSource.MIXED.value
            if any_provider_usage_reported
            else UsageSource.LOCAL_ESTIMATE.value
        )
        usage_confidence = UsageConfidence.ESTIMATED.value
    else:
        usage_source_detail = (
            str(api_usage_source_detail or "").strip() or UsageSource.PROVIDER_RESPONSE.value
        )
        requested_confidence = str(api_usage_confidence or "").strip().lower()
        if api_usage_counts_authoritative and prompt_tokens_authoritative:
            usage_confidence = UsageConfidence.AUTHORITATIVE.value
        elif requested_confidence in {item.value for item in UsageConfidence}:
            usage_confidence = requested_confidence
        else:
            usage_confidence = UsageConfidence.REPORTED.value
    cache_read_input_tokens = _safe_int(api_cache_read_input_tokens)
    if cache_read_input_tokens is None:
        cache_read_input_tokens = _safe_int(api_cached_prompt_tokens)
    cache_creation_5m_input_tokens = _safe_int(api_cache_creation_5m_input_tokens)
    cache_creation_1h_input_tokens = _safe_int(api_cache_creation_1h_input_tokens)
    cache_creation_input_tokens = _safe_int(api_cache_creation_input_tokens)
    if cache_creation_input_tokens is None:
        creation_parts = [
            value
            for value in (cache_creation_5m_input_tokens, cache_creation_1h_input_tokens)
            if value is not None
        ]
        if creation_parts:
            cache_creation_input_tokens = sum(creation_parts)
    input_tokens_uncached = _safe_int(api_input_tokens_uncached)
    input_tokens_uncached_derived = bool(api_input_tokens_uncached_derived)
    reasoning_tokens = _safe_int(api_reasoning_tokens)
    raw_provider_usage = (
        copy.deepcopy(api_raw_provider_usage) if isinstance(api_raw_provider_usage, dict) else None
    )

    cached_prompt_tokens = cache_read_input_tokens
    uncached_prompt_tokens: int | None = None
    if raw_api_prompt_tokens is not None and not prompt_measurement_is_local:
        if cached_prompt_tokens is not None and prompt_tokens >= cached_prompt_tokens:
            uncached_prompt_tokens = prompt_tokens - cached_prompt_tokens
        elif input_tokens_uncached is not None:
            uncached_prompt_tokens = input_tokens_uncached
    if (
        input_tokens_uncached is None
        and raw_api_prompt_tokens is not None
        and not prompt_measurement_is_local
    ):
        cached_read = cache_read_input_tokens or 0
        cache_creation = cache_creation_input_tokens or 0
        if (
            cached_read > 0 or cache_creation > 0
        ) and prompt_tokens >= cached_read + cache_creation:
            input_tokens_uncached = prompt_tokens - cached_read - cache_creation
            input_tokens_uncached_derived = True
    prompt_estimate_error_tokens, prompt_estimate_error_ratio = _prompt_estimate_calibration(
        api_prompt_tokens=(None if prompt_measurement_is_local else raw_api_prompt_tokens),
        estimated_prompt_tokens=fallback_prompt_estimate_tokens,
        input_tokens_scope=input_tokens_scope,
        usage_source=usage_source,
        api_prompt_tokens_authoritative=prompt_tokens_authoritative,
    )
    requested_meta = registry.get(requested_model)
    response_meta: ModelMeta | None = None
    if response_model:
        response_meta = registry.get(response_model)

    input_rate = requested_meta.input_cost_per_token
    output_rate = requested_meta.output_cost_per_token
    if input_rate is None and response_meta is not None:
        input_rate = response_meta.input_cost_per_token
    if output_rate is None and response_meta is not None:
        output_rate = response_meta.output_cost_per_token
    cache_read_input_rate = _resolve_model_rate(
        requested_meta,
        response_meta,
        "cache_read_input_cost_per_token",
    )
    cache_creation_input_rate = _resolve_model_rate(
        requested_meta,
        response_meta,
        "cache_creation_input_cost_per_token",
    )
    cache_creation_5m_input_rate = _resolve_model_rate(
        requested_meta,
        response_meta,
        "cache_creation_5m_input_cost_per_token",
    )
    cache_creation_1h_input_rate = _resolve_model_rate(
        requested_meta,
        response_meta,
        "cache_creation_1h_input_cost_per_token",
    )
    reasoning_output_rate = _resolve_model_rate(
        requested_meta,
        response_meta,
        "reasoning_output_cost_per_token",
    )
    safe_billing_mode = str(billing_mode or "").strip().lower()
    if safe_billing_mode not in {item.value for item in BillingMode}:
        safe_billing_mode = BillingMode.UNKNOWN.value
    provider_cost_usd = _safe_float(api_provider_cost_usd)
    if safe_billing_mode == BillingMode.SUBSCRIPTION.value:
        cost = _CostComputation(None, False, CostSource.SUBSCRIPTION)
    elif safe_billing_mode == BillingMode.INCLUDED.value:
        cost = _CostComputation(None, False, CostSource.INCLUDED)
    elif safe_billing_mode == BillingMode.LOCAL.value:
        cost = _CostComputation(None, False, CostSource.LOCAL)
    elif provider_cost_usd is not None and provider_cost_usd >= 0:
        cost = _CostComputation(
            cost_usd=provider_cost_usd,
            cache_cost_pricing_missing=False,
            cost_source=CostSource.PROVIDER_REPORTED,
        )
    else:
        cost = _usage_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            input_tokens_uncached=input_tokens_uncached,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_creation_5m_input_tokens=cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=cache_creation_1h_input_tokens,
            reasoning_tokens=reasoning_tokens,
            input_rate=input_rate,
            output_rate=output_rate,
            cache_read_input_rate=cache_read_input_rate,
            cache_creation_input_rate=cache_creation_input_rate,
            cache_creation_5m_input_rate=cache_creation_5m_input_rate,
            cache_creation_1h_input_rate=cache_creation_1h_input_rate,
            reasoning_output_rate=reasoning_output_rate,
        )
    safe_request_mode = _safe_label(request_mode) or (
        str(safe_request_plan.get("input_mode"))
        if safe_request_plan and safe_request_plan.get("input_mode")
        else None
    )
    safe_cache_strategy = _safe_label(cache_strategy) or (
        str(safe_request_plan.get("cache_strategy"))
        if safe_request_plan and safe_request_plan.get("cache_strategy")
        else None
    )

    return UsageRecord(
        usage_schema_version=7,
        input_tokens_scope=input_tokens_scope,
        timestamp=now_iso(),
        role=role,
        requested_model=requested_model,
        response_model=response_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        input_cost_per_token=input_rate,
        output_cost_per_token=output_rate,
        cache_read_input_cost_per_token=cache_read_input_rate,
        cache_creation_input_cost_per_token=cache_creation_input_rate,
        cache_creation_5m_input_cost_per_token=cache_creation_5m_input_rate,
        cache_creation_1h_input_cost_per_token=cache_creation_1h_input_rate,
        reasoning_output_cost_per_token=reasoning_output_rate,
        cost_usd=cost.cost_usd,
        billing_mode=safe_billing_mode,
        cost_source=cost.cost_source.value,
        provider_cost_usd=provider_cost_usd,
        usage_source=usage_source,
        usage_source_detail=usage_source_detail,
        usage_confidence=usage_confidence,
        output_includes_reasoning=bool(api_output_includes_reasoning),
        provider_key=_safe_label(provider_key),
        protocol=_safe_label(protocol),
        base_url_host=_safe_label(base_url_host),
        operation=_safe_label(operation),
        request_mode=safe_request_mode,
        cache_strategy=safe_cache_strategy,
        request_plan=safe_request_plan,
        # Ephemeral pixels have already been scrubbed when accounting runs. The
        # adapter measured the actual outgoing content before that cleanup.
        request_has_media=(
            safe_request_plan["wire_media_parts"] > 0
            if safe_request_plan is not None and "wire_media_parts" in safe_request_plan
            else request_contains_media(messages)
        ),
        cached_prompt_tokens=cached_prompt_tokens,
        uncached_prompt_tokens=uncached_prompt_tokens,
        input_tokens_uncached=input_tokens_uncached,
        input_tokens_uncached_derived=input_tokens_uncached_derived,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_creation_5m_input_tokens=cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=cache_creation_1h_input_tokens,
        reasoning_tokens=reasoning_tokens,
        raw_provider_usage=raw_provider_usage,
        cache_cost_pricing_missing=cost.cache_cost_pricing_missing,
        request_token_estimate=request_token_estimate,
        prompt_estimate_tokens=fallback_prompt_estimate_tokens,
        prompt_estimate_error_tokens=prompt_estimate_error_tokens,
        prompt_estimate_error_ratio=prompt_estimate_error_ratio,
        raw_api_prompt_tokens=raw_api_prompt_tokens,
        raw_api_completion_tokens=raw_api_completion_tokens,
        raw_api_total_tokens=raw_api_total_tokens,
        usage_correction_reason=None,
    )


class UsageSummary:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_model: dict[str, _ModelUsageTotals] = {}
        self._records: list[UsageRecord] = []
        self.calls = 0

    def add_record(self, record: UsageRecord) -> None:
        with self._lock:
            key = record.requested_model.strip() or "unknown-model"
            totals = self._by_model.setdefault(key, _ModelUsageTotals())
            self._records.append(record)
            totals.prompt_tokens += record.prompt_tokens
            totals.completion_tokens += record.completion_tokens
            totals.total_tokens += record.total_tokens
            if record.cost_usd is None:
                if record.cost_source == CostSource.SUBSCRIPTION.value:
                    totals.subscription_calls += 1
                elif record.cost_source == CostSource.INCLUDED.value:
                    totals.included_calls += 1
                elif record.cost_source == CostSource.LOCAL.value:
                    totals.local_calls += 1
                else:
                    totals.unknown_cost_count += 1
            else:
                totals.known_cost_usd += float(record.cost_usd)
                totals.known_cost_calls += 1
                if record.cost_source == CostSource.PROVIDER_REPORTED.value:
                    totals.provider_reported_cost_calls += 1
                elif record.cost_source == CostSource.CATALOG_ESTIMATE.value:
                    totals.catalog_estimated_cost_calls += 1
            if record.usage_source == "api":
                totals.api_usage_calls += 1
            else:
                totals.estimate_usage_calls += 1
            if record.usage_confidence == UsageConfidence.AUTHORITATIVE.value:
                totals.authoritative_usage_calls += 1
            elif record.usage_confidence == UsageConfidence.REPORTED.value:
                totals.reported_usage_calls += 1
            if record.usage_correction_reason:
                totals.corrected_usage_calls += 1
            if record.prompt_estimate_error_ratio is not None:
                totals.prompt_estimate_calibration_calls += 1
                totals.prompt_estimate_error_ratio_sum += float(record.prompt_estimate_error_ratio)
                totals.prompt_estimate_error_ratio_max = max(
                    totals.prompt_estimate_error_ratio_max,
                    float(record.prompt_estimate_error_ratio),
                )
                error_tokens = record.prompt_estimate_error_tokens or 0
                if error_tokens > 0:
                    totals.prompt_estimate_underestimate_calls += 1
                elif error_tokens < 0:
                    totals.prompt_estimate_overestimate_calls += 1
            totals.cached_prompt_tokens += max(0, record.cached_prompt_tokens or 0)
            totals.uncached_prompt_tokens += max(0, record.uncached_prompt_tokens or 0)
            totals.input_tokens_uncached += max(0, record.input_tokens_uncached or 0)
            totals.cache_read_input_tokens += max(0, record.cache_read_input_tokens or 0)
            totals.cache_creation_input_tokens += max(0, record.cache_creation_input_tokens or 0)
            if record.input_tokens_uncached is not None:
                if record.input_tokens_uncached_derived:
                    totals.input_tokens_uncached_derived_calls += 1
                else:
                    totals.input_tokens_uncached_reported_calls += 1
            if record.cache_read_input_tokens is not None:
                totals.cache_read_reported_calls += 1
            if record.cache_creation_input_tokens is not None:
                totals.cache_creation_reported_calls += 1
            totals.cache_creation_5m_input_tokens += max(
                0,
                record.cache_creation_5m_input_tokens or 0,
            )
            totals.cache_creation_1h_input_tokens += max(
                0,
                record.cache_creation_1h_input_tokens or 0,
            )
            totals.reasoning_tokens += max(0, record.reasoning_tokens or 0)
            if record.cache_cost_pricing_missing:
                totals.cache_cost_pricing_missing_calls += 1
            breakdown = record.request_token_estimate
            if breakdown is not None:
                totals.estimated_bootstrap_prompt_tokens += breakdown.bootstrap_prompt_tokens
                totals.estimated_tool_schema_tokens += breakdown.tool_schema_tokens
                totals.estimated_live_conversation_history_tokens += (
                    breakdown.live_conversation_history_tokens
                )
                totals.estimated_inline_tool_transcript_tokens += (
                    breakdown.inline_tool_transcript_tokens
                )
                totals.estimated_memory_summary_tokens += breakdown.memory_summary_tokens
                totals.estimated_pins_tokens += breakdown.pins_tokens
                totals.estimated_total_request_tokens += breakdown.total_tokens
                tool_budget = breakdown.tool_schema_budget
                if tool_budget is not None:
                    totals.tool_schema_budget_reported_calls += 1
                    totals.tool_schema_budget_overage_tokens += tool_budget.over_budget_tokens
                    if tool_budget.over_budget:
                        totals.tool_schema_budget_exceeded_calls += 1
                    if tool_budget.largest_tools:
                        totals.tool_schema_largest_tool_tokens = max(
                            totals.tool_schema_largest_tool_tokens,
                            tool_budget.largest_tools[0].token_estimate,
                        )
            self.calls += 1

    def records(self) -> list[UsageRecord]:
        with self._lock:
            return list(self._records)

    def cache_efficiency_summary(self) -> dict[str, Any]:
        with self._lock:
            reported = [
                (position, record)
                for position, record in enumerate(self._records, start=1)
                if record.cached_prompt_tokens is not None
                or record.uncached_prompt_tokens is not None
            ]
            cached = sum(max(0, record.cached_prompt_tokens or 0) for _, record in reported)
            uncached = sum(max(0, record.uncached_prompt_tokens or 0) for _, record in reported)
            denominator = cached + uncached
            largest = max(
                reported,
                key=lambda item: max(0, item[1].uncached_prompt_tokens or 0),
                default=None,
            )
            largest_payload: dict[str, Any] | None = None
            if largest is not None:
                position, record = largest
                largest_payload = {
                    "call_position": position,
                    "uncached_prompt_tokens": max(
                        0,
                        record.uncached_prompt_tokens or 0,
                    ),
                    "role": record.role,
                    "requested_model": record.requested_model,
                    "timestamp": record.timestamp,
                }
            return {
                "reported_calls": len(reported),
                "cached_prompt_tokens": cached,
                "uncached_prompt_tokens": uncached,
                "hit_ratio": cached / denominator if denominator > 0 else None,
                "largest_uncached_call": largest_payload,
            }

    def merge_records(self, records: Iterable[UsageRecord]) -> int:
        merged = 0
        for record in records:
            self.add_record(record)
            merged += 1
        return merged

    def add_event_payload(self, payload: dict[str, Any]) -> None:
        if str(payload.get("event_type") or "") != "llm_usage":
            return
        record = UsageRecord(
            usage_schema_version=max(1, _safe_int(payload.get("usage_schema_version")) or 1),
            input_tokens_scope=str(
                payload.get("input_tokens_scope")
                or (
                    "interaction"
                    if payload.get("protocol") == "gemini_interactions"
                    or (
                        isinstance(payload.get("raw_provider_usage"), dict)
                        and (
                            _safe_int(payload["raw_provider_usage"].get("toolUsePromptTokenCount"))
                            or 0
                        )
                        > 0
                    )
                    else "request"
                )
            ),
            timestamp=str(payload.get("timestamp") or ""),
            role=str(payload.get("role") or "main"),
            requested_model=str(payload.get("requested_model") or "unknown-model"),
            response_model=(
                str(payload.get("response_model"))
                if payload.get("response_model") is not None
                else None
            ),
            prompt_tokens=max(0, _safe_int(payload.get("prompt_tokens")) or 0),
            completion_tokens=max(0, _safe_int(payload.get("completion_tokens")) or 0),
            total_tokens=max(0, _safe_int(payload.get("total_tokens")) or 0),
            input_cost_per_token=(
                _safe_float(payload.get("input_cost_per_token"))
                if payload.get("input_cost_per_token") is not None
                else None
            ),
            output_cost_per_token=(
                _safe_float(payload.get("output_cost_per_token"))
                if payload.get("output_cost_per_token") is not None
                else None
            ),
            cache_read_input_cost_per_token=(
                _safe_float(payload.get("cache_read_input_cost_per_token"))
                if payload.get("cache_read_input_cost_per_token") is not None
                else None
            ),
            cache_creation_input_cost_per_token=(
                _safe_float(payload.get("cache_creation_input_cost_per_token"))
                if payload.get("cache_creation_input_cost_per_token") is not None
                else None
            ),
            cache_creation_5m_input_cost_per_token=(
                _safe_float(payload.get("cache_creation_5m_input_cost_per_token"))
                if payload.get("cache_creation_5m_input_cost_per_token") is not None
                else None
            ),
            cache_creation_1h_input_cost_per_token=(
                _safe_float(payload.get("cache_creation_1h_input_cost_per_token"))
                if payload.get("cache_creation_1h_input_cost_per_token") is not None
                else None
            ),
            reasoning_output_cost_per_token=(
                _safe_float(payload.get("reasoning_output_cost_per_token"))
                if payload.get("reasoning_output_cost_per_token") is not None
                else None
            ),
            cost_usd=(
                _safe_float(payload.get("cost_usd"))
                if payload.get("cost_usd") is not None
                else None
            ),
            billing_mode=str(payload.get("billing_mode") or BillingMode.METERED_API.value),
            cost_source=str(payload.get("cost_source") or CostSource.UNKNOWN.value),
            provider_cost_usd=(
                _safe_float(payload.get("provider_cost_usd"))
                if payload.get("provider_cost_usd") is not None
                else None
            ),
            usage_source=str(payload.get("usage_source") or "estimate"),
            usage_source_detail=str(
                payload.get("usage_source_detail")
                or (
                    UsageSource.PROVIDER_RESPONSE.value
                    if str(payload.get("usage_source") or "") == "api"
                    else UsageSource.LOCAL_ESTIMATE.value
                )
            ),
            usage_confidence=str(
                payload.get("usage_confidence")
                or (
                    UsageConfidence.REPORTED.value
                    if str(payload.get("usage_source") or "") == "api"
                    else UsageConfidence.ESTIMATED.value
                )
            ),
            output_includes_reasoning=_safe_bool(payload.get("output_includes_reasoning", True)),
            provider_key=_safe_label(payload.get("provider_key")),
            protocol=_safe_label(payload.get("protocol")),
            base_url_host=_safe_label(payload.get("base_url_host")),
            operation=_safe_label(payload.get("operation")),
            request_mode=_safe_label(payload.get("request_mode")),
            cache_strategy=_safe_label(payload.get("cache_strategy")),
            request_plan=_safe_request_plan_payload(payload.get("request_plan")),
            request_has_media=_safe_bool(payload.get("request_has_media")),
            cached_prompt_tokens=_safe_int(payload.get("cached_prompt_tokens")),
            uncached_prompt_tokens=_safe_int(payload.get("uncached_prompt_tokens")),
            input_tokens_uncached=_safe_int(payload.get("input_tokens_uncached")),
            input_tokens_uncached_derived=bool(payload.get("input_tokens_uncached_derived")),
            cache_read_input_tokens=(
                _safe_int(payload.get("cache_read_input_tokens"))
                if payload.get("cache_read_input_tokens") is not None
                else _safe_int(payload.get("cached_prompt_tokens"))
            ),
            cache_creation_input_tokens=_safe_int(payload.get("cache_creation_input_tokens")),
            cache_creation_5m_input_tokens=_safe_int(payload.get("cache_creation_5m_input_tokens")),
            cache_creation_1h_input_tokens=_safe_int(payload.get("cache_creation_1h_input_tokens")),
            reasoning_tokens=_safe_int(payload.get("reasoning_tokens")),
            raw_provider_usage=(
                copy.deepcopy(payload.get("raw_provider_usage"))
                if isinstance(payload.get("raw_provider_usage"), dict)
                else None
            ),
            cache_cost_pricing_missing=_safe_bool(payload.get("cache_cost_pricing_missing")),
            request_token_estimate=RequestTokenBreakdown.from_payload(
                payload.get("request_token_estimate")
            ),
            prompt_estimate_tokens=_safe_int(payload.get("prompt_estimate_tokens")),
            prompt_estimate_error_tokens=_safe_int(payload.get("prompt_estimate_error_tokens")),
            prompt_estimate_error_ratio=(
                _safe_float(payload.get("prompt_estimate_error_ratio"))
                if payload.get("prompt_estimate_error_ratio") is not None
                else None
            ),
            raw_api_prompt_tokens=_safe_int(payload.get("raw_api_prompt_tokens")),
            raw_api_completion_tokens=_safe_int(payload.get("raw_api_completion_tokens")),
            raw_api_total_tokens=_safe_int(payload.get("raw_api_total_tokens")),
            usage_correction_reason=(
                str(payload.get("usage_correction_reason"))
                if payload.get("usage_correction_reason") is not None
                else None
            ),
        )
        self.add_record(record)

    def by_model_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[dict[str, Any]] = []
            for model_name in sorted(self._by_model):
                totals = self._by_model[model_name]
                rows.append(
                    {
                        "model": model_name,
                        "prompt_tokens": totals.prompt_tokens,
                        "completion_tokens": totals.completion_tokens,
                        "total_tokens": totals.total_tokens,
                        "cost_usd": (
                            totals.known_cost_usd if totals.known_cost_calls > 0 else None
                        ),
                        "known_cost_calls": totals.known_cost_calls,
                        "unknown_cost_count": totals.unknown_cost_count,
                        "provider_reported_cost_calls": totals.provider_reported_cost_calls,
                        "catalog_estimated_cost_calls": totals.catalog_estimated_cost_calls,
                        "subscription_calls": totals.subscription_calls,
                        "included_calls": totals.included_calls,
                        "local_calls": totals.local_calls,
                        "api_usage_calls": totals.api_usage_calls,
                        "estimate_usage_calls": totals.estimate_usage_calls,
                        "authoritative_usage_calls": totals.authoritative_usage_calls,
                        "reported_usage_calls": totals.reported_usage_calls,
                        "corrected_usage_calls": totals.corrected_usage_calls,
                        "prompt_estimate_calibration_calls": (
                            totals.prompt_estimate_calibration_calls
                        ),
                        "prompt_estimate_error_ratio_avg": (
                            totals.prompt_estimate_error_ratio_sum
                            / totals.prompt_estimate_calibration_calls
                            if totals.prompt_estimate_calibration_calls > 0
                            else None
                        ),
                        "prompt_estimate_error_ratio_max": (
                            totals.prompt_estimate_error_ratio_max
                            if totals.prompt_estimate_calibration_calls > 0
                            else None
                        ),
                        "prompt_estimate_underestimate_calls": (
                            totals.prompt_estimate_underestimate_calls
                        ),
                        "prompt_estimate_overestimate_calls": (
                            totals.prompt_estimate_overestimate_calls
                        ),
                        "cached_prompt_tokens": totals.cached_prompt_tokens,
                        "uncached_prompt_tokens": totals.uncached_prompt_tokens,
                        "input_tokens_uncached": totals.input_tokens_uncached,
                        "cache_read_input_tokens": totals.cache_read_input_tokens,
                        "cache_creation_input_tokens": totals.cache_creation_input_tokens,
                        "cache_creation_5m_input_tokens": (totals.cache_creation_5m_input_tokens),
                        "cache_creation_1h_input_tokens": (totals.cache_creation_1h_input_tokens),
                        "input_tokens_uncached_reported_calls": (
                            totals.input_tokens_uncached_reported_calls
                        ),
                        "input_tokens_uncached_derived_calls": (
                            totals.input_tokens_uncached_derived_calls
                        ),
                        "cache_read_reported_calls": totals.cache_read_reported_calls,
                        "cache_creation_reported_calls": totals.cache_creation_reported_calls,
                        "reasoning_tokens": totals.reasoning_tokens,
                        "cache_cost_pricing_missing_calls": (
                            totals.cache_cost_pricing_missing_calls
                        ),
                        "request_token_estimate": {
                            "bootstrap_prompt_tokens": totals.estimated_bootstrap_prompt_tokens,
                            "tool_schema_tokens": totals.estimated_tool_schema_tokens,
                            "live_conversation_history_tokens": (
                                totals.estimated_live_conversation_history_tokens
                            ),
                            "inline_tool_transcript_tokens": (
                                totals.estimated_inline_tool_transcript_tokens
                            ),
                            "memory_summary_tokens": totals.estimated_memory_summary_tokens,
                            "pins_tokens": totals.estimated_pins_tokens,
                            "total_tokens": totals.estimated_total_request_tokens,
                            "tool_schema_budget_reported_calls": (
                                totals.tool_schema_budget_reported_calls
                            ),
                            "tool_schema_budget_exceeded_calls": (
                                totals.tool_schema_budget_exceeded_calls
                            ),
                            "tool_schema_budget_overage_tokens": (
                                totals.tool_schema_budget_overage_tokens
                            ),
                            "tool_schema_largest_tool_tokens": (
                                totals.tool_schema_largest_tool_tokens
                            ),
                        },
                    }
                )
            return rows

    def totals(self) -> dict[str, Any]:
        with self._lock:
            prompt = sum(v.prompt_tokens for v in self._by_model.values())
            completion = sum(v.completion_tokens for v in self._by_model.values())
            total = sum(v.total_tokens for v in self._by_model.values())
            known_cost = sum(v.known_cost_usd for v in self._by_model.values())
            known_cost_calls = sum(v.known_cost_calls for v in self._by_model.values())
            unknown_cost_calls = sum(v.unknown_cost_count for v in self._by_model.values())
            provider_reported_cost_calls = sum(
                v.provider_reported_cost_calls for v in self._by_model.values()
            )
            catalog_estimated_cost_calls = sum(
                v.catalog_estimated_cost_calls for v in self._by_model.values()
            )
            subscription_calls = sum(v.subscription_calls for v in self._by_model.values())
            included_calls = sum(v.included_calls for v in self._by_model.values())
            local_calls = sum(v.local_calls for v in self._by_model.values())
            api_usage_calls = sum(v.api_usage_calls for v in self._by_model.values())
            estimate_usage_calls = sum(v.estimate_usage_calls for v in self._by_model.values())
            authoritative_usage_calls = sum(
                v.authoritative_usage_calls for v in self._by_model.values()
            )
            reported_usage_calls = sum(v.reported_usage_calls for v in self._by_model.values())
            corrected_usage_calls = sum(v.corrected_usage_calls for v in self._by_model.values())
            cached_prompt_tokens = sum(v.cached_prompt_tokens for v in self._by_model.values())
            uncached_prompt_tokens = sum(v.uncached_prompt_tokens for v in self._by_model.values())
            input_tokens_uncached = sum(v.input_tokens_uncached for v in self._by_model.values())
            cache_read_input_tokens = sum(
                v.cache_read_input_tokens for v in self._by_model.values()
            )
            cache_creation_input_tokens = sum(
                v.cache_creation_input_tokens for v in self._by_model.values()
            )
            cache_creation_5m_input_tokens = sum(
                v.cache_creation_5m_input_tokens for v in self._by_model.values()
            )
            cache_creation_1h_input_tokens = sum(
                v.cache_creation_1h_input_tokens for v in self._by_model.values()
            )
            input_tokens_uncached_reported_calls = sum(
                v.input_tokens_uncached_reported_calls for v in self._by_model.values()
            )
            input_tokens_uncached_derived_calls = sum(
                v.input_tokens_uncached_derived_calls for v in self._by_model.values()
            )
            cache_read_reported_calls = sum(
                v.cache_read_reported_calls for v in self._by_model.values()
            )
            cache_creation_reported_calls = sum(
                v.cache_creation_reported_calls for v in self._by_model.values()
            )
            reasoning_tokens = sum(v.reasoning_tokens for v in self._by_model.values())
            cache_cost_pricing_missing_calls = sum(
                v.cache_cost_pricing_missing_calls for v in self._by_model.values()
            )
            prompt_estimate_calibration_calls = sum(
                v.prompt_estimate_calibration_calls for v in self._by_model.values()
            )
            prompt_estimate_error_ratio_sum = sum(
                v.prompt_estimate_error_ratio_sum for v in self._by_model.values()
            )
            prompt_estimate_error_ratio_max = max(
                (v.prompt_estimate_error_ratio_max for v in self._by_model.values()),
                default=0.0,
            )
            prompt_estimate_underestimate_calls = sum(
                v.prompt_estimate_underestimate_calls for v in self._by_model.values()
            )
            prompt_estimate_overestimate_calls = sum(
                v.prompt_estimate_overestimate_calls for v in self._by_model.values()
            )
            estimated_bootstrap_prompt_tokens = sum(
                v.estimated_bootstrap_prompt_tokens for v in self._by_model.values()
            )
            estimated_tool_schema_tokens = sum(
                v.estimated_tool_schema_tokens for v in self._by_model.values()
            )
            estimated_live_conversation_history_tokens = sum(
                v.estimated_live_conversation_history_tokens for v in self._by_model.values()
            )
            estimated_inline_tool_transcript_tokens = sum(
                v.estimated_inline_tool_transcript_tokens for v in self._by_model.values()
            )
            estimated_memory_summary_tokens = sum(
                v.estimated_memory_summary_tokens for v in self._by_model.values()
            )
            estimated_pins_tokens = sum(v.estimated_pins_tokens for v in self._by_model.values())
            estimated_total_request_tokens = sum(
                v.estimated_total_request_tokens for v in self._by_model.values()
            )
            tool_schema_budget_reported_calls = sum(
                v.tool_schema_budget_reported_calls for v in self._by_model.values()
            )
            tool_schema_budget_exceeded_calls = sum(
                v.tool_schema_budget_exceeded_calls for v in self._by_model.values()
            )
            tool_schema_budget_overage_tokens = sum(
                v.tool_schema_budget_overage_tokens for v in self._by_model.values()
            )
            tool_schema_largest_tool_tokens = max(
                (v.tool_schema_largest_tool_tokens for v in self._by_model.values()),
                default=0,
            )
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "cost_usd": known_cost if known_cost_calls > 0 else None,
                "known_cost_usd": known_cost,
                "known_cost_calls": known_cost_calls,
                "unknown_cost_calls": unknown_cost_calls,
                "provider_reported_cost_calls": provider_reported_cost_calls,
                "catalog_estimated_cost_calls": catalog_estimated_cost_calls,
                "subscription_calls": subscription_calls,
                "included_calls": included_calls,
                "local_calls": local_calls,
                "api_usage_calls": api_usage_calls,
                "estimate_usage_calls": estimate_usage_calls,
                "authoritative_usage_calls": authoritative_usage_calls,
                "reported_usage_calls": reported_usage_calls,
                "corrected_usage_calls": corrected_usage_calls,
                "prompt_estimate_calibration_calls": prompt_estimate_calibration_calls,
                "prompt_estimate_error_ratio_avg": (
                    prompt_estimate_error_ratio_sum / prompt_estimate_calibration_calls
                    if prompt_estimate_calibration_calls > 0
                    else None
                ),
                "prompt_estimate_error_ratio_max": (
                    prompt_estimate_error_ratio_max
                    if prompt_estimate_calibration_calls > 0
                    else None
                ),
                "prompt_estimate_underestimate_calls": prompt_estimate_underestimate_calls,
                "prompt_estimate_overestimate_calls": prompt_estimate_overestimate_calls,
                "cached_prompt_tokens": cached_prompt_tokens,
                "uncached_prompt_tokens": uncached_prompt_tokens,
                "input_tokens_uncached": input_tokens_uncached,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cache_creation_5m_input_tokens": cache_creation_5m_input_tokens,
                "cache_creation_1h_input_tokens": cache_creation_1h_input_tokens,
                "input_tokens_uncached_reported_calls": input_tokens_uncached_reported_calls,
                "input_tokens_uncached_derived_calls": input_tokens_uncached_derived_calls,
                "cache_read_reported_calls": cache_read_reported_calls,
                "cache_creation_reported_calls": cache_creation_reported_calls,
                "reasoning_tokens": reasoning_tokens,
                "cache_cost_pricing_missing_calls": cache_cost_pricing_missing_calls,
                "cache_efficiency": self.cache_efficiency_summary(),
                "request_token_estimate": {
                    "bootstrap_prompt_tokens": estimated_bootstrap_prompt_tokens,
                    "tool_schema_tokens": estimated_tool_schema_tokens,
                    "live_conversation_history_tokens": (
                        estimated_live_conversation_history_tokens
                    ),
                    "inline_tool_transcript_tokens": estimated_inline_tool_transcript_tokens,
                    "memory_summary_tokens": estimated_memory_summary_tokens,
                    "pins_tokens": estimated_pins_tokens,
                    "total_tokens": estimated_total_request_tokens,
                    "tool_schema_budget_reported_calls": tool_schema_budget_reported_calls,
                    "tool_schema_budget_exceeded_calls": tool_schema_budget_exceeded_calls,
                    "tool_schema_budget_overage_tokens": tool_schema_budget_overage_tokens,
                    "tool_schema_largest_tool_tokens": tool_schema_largest_tool_tokens,
                },
                "calls": self.calls,
            }

    def recent_calibration_snapshot(
        self,
        *,
        requested_model: str | None = None,
        provider_key: str | None = None,
        protocol: str | None = None,
        base_url_host: str | None = None,
        operation: str | None = None,
        request_mode: str | None = None,
        cache_strategy: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        model_key = str(requested_model or "").strip()
        provider_key_filter = str(provider_key or "").strip()
        protocol_filter = str(protocol or "").strip()
        base_url_host_filter = str(base_url_host or "").strip()
        operation_filter = str(operation or "").strip()
        request_mode_filter = str(request_mode or "").strip()
        cache_strategy_filter = str(cache_strategy or "").strip()
        max_items = max(1, int(limit or 20))
        with self._lock:
            records = [
                record
                for record in self._records
                if _record_matches_calibration_filters(
                    record,
                    requested_model=model_key,
                    provider_key=provider_key_filter,
                    protocol=protocol_filter,
                    base_url_host_filter=base_url_host_filter,
                    operation=operation_filter,
                    request_mode=request_mode_filter,
                    cache_strategy=cache_strategy_filter,
                )
            ][-max_items:]
        snapshot = self._calibration_snapshot_from_records(records)
        snapshot["group_filter"] = {
            "requested_model": model_key,
            "provider_key": provider_key_filter,
            "protocol": protocol_filter,
            "base_url_host": base_url_host_filter,
            "operation": operation_filter,
            "request_mode": request_mode_filter,
            "cache_strategy": cache_strategy_filter,
        }
        return snapshot

    def calibration_group_rows(self, *, limit: int = 200) -> list[dict[str, Any]]:
        max_items = max(1, int(limit or 200))
        with self._lock:
            records = self._records[-max_items:]
        groups: dict[tuple[str, str, str, str, str, str, str], list[UsageRecord]] = {}
        for record in records:
            groups.setdefault(_calibration_group_key(record), []).append(record)
        rows: list[dict[str, Any]] = []
        for key, group_records in sorted(groups.items()):
            snapshot = self._calibration_snapshot_from_records(group_records)
            rows.append(
                {
                    "requested_model": key[0],
                    "provider_key": key[1],
                    "protocol": key[2],
                    "base_url_host": key[3],
                    "operation": key[4],
                    "request_mode": key[5],
                    "cache_strategy": key[6],
                    **snapshot,
                }
            )
        return rows

    def _calibration_snapshot_from_records(
        self,
        records: list[UsageRecord],
    ) -> dict[str, Any]:
        media_known_records = [record for record in records if record.usage_schema_version >= 5]
        request_records = [
            record for record in media_known_records if record.input_tokens_scope == "request"
        ]
        calibration_records = [record for record in request_records if not record.request_has_media]
        raw_ratios = [
            float(record.prompt_estimate_error_ratio)
            for record in calibration_records
            if record.prompt_estimate_error_ratio is not None
            and math.isfinite(float(record.prompt_estimate_error_ratio))
            and float(record.prompt_estimate_error_ratio) > 0
        ]
        ratios = _robust_calibration_ratios(raw_ratios)
        api_prompt_tokens = sum(max(0, record.prompt_tokens) for record in records)
        cache_read_tokens = sum(max(0, record.cache_read_input_tokens or 0) for record in records)
        cache_creation_tokens = sum(
            max(0, record.cache_creation_input_tokens or 0) for record in records
        )
        underestimate_calls = sum(
            1 for record in records if (record.prompt_estimate_error_tokens or 0) > 0
        )
        overestimate_calls = sum(
            1 for record in records if (record.prompt_estimate_error_tokens or 0) < 0
        )
        ratios_sorted = sorted(ratios)
        p90 = None
        if ratios_sorted:
            p90_index = min(
                len(ratios_sorted) - 1,
                max(0, math.ceil(len(ratios_sorted) * 0.9) - 1),
            )
            p90 = ratios_sorted[p90_index]
        return {
            "records": len(records),
            "calibration_calls": len(raw_ratios),
            "calibration_usable_calls": len(ratios),
            "calibration_outlier_calls": max(0, len(raw_ratios) - len(ratios)),
            "calibration_excluded_unknown_media_calls": (len(records) - len(media_known_records)),
            "calibration_excluded_media_calls": (len(request_records) - len(calibration_records)),
            "calibration_excluded_aggregate_calls": len(media_known_records) - len(request_records),
            "calibration_stable": len(ratios) >= 3,
            "prompt_estimate_error_ratio_avg": (sum(ratios) / len(ratios) if ratios else None),
            "prompt_estimate_error_ratio_p90": p90,
            "prompt_estimate_error_ratio_max": max(ratios) if ratios else None,
            "prompt_estimate_error_ratio_raw_max": (max(raw_ratios) if raw_ratios else None),
            "prompt_estimate_underestimate_calls": underestimate_calls,
            "prompt_estimate_overestimate_calls": overestimate_calls,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": cache_creation_tokens,
            "cache_hit_ratio": (
                cache_read_tokens / api_prompt_tokens if api_prompt_tokens > 0 else None
            ),
        }


def aggregate_usage_from_session_logs(paths: list[Path]) -> UsageSummary:
    summary = UsageSummary()
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        for event in read_session_events(path):
            if str(event.get("type") or "") != "llm_usage":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict):
                summary.add_event_payload(payload)
    return summary


def compute_context_left(
    *,
    messages: list[dict[str, Any]],
    model_name: str,
    registry: ModelRegistry,
    tool_list: list[dict[str, Any]] | None = None,
    pinned_prefix_len: int = 0,
    safety_margin_tokens: int = 512,
    startup_baseline_tokens: int = 0,
    prompt_estimate_multiplier: float | None = None,
    request_measurement: RequestContextMeasurement | None = None,
) -> ContextLeft:
    meta = registry.get(model_name)
    context_window = meta.context_window_tokens
    effective_input_budget = compute_input_budget(meta, safety_margin=safety_margin_tokens)
    baseline = max(0, int(startup_baseline_tokens or 0))
    request_estimate = estimate_request_token_breakdown(
        messages=messages,
        tool_list=tool_list,
        pinned_prefix_len=pinned_prefix_len,
    )
    local_used = request_estimate.total_tokens
    used = local_used
    token_count_source = UsageSource.LOCAL_ESTIMATE.value
    token_count_confidence = UsageConfidence.ESTIMATED.value
    multiplier = 1.0
    projection_kind = (
        request_measurement.projection_kind(messages=messages, tool_list=tool_list)
        if request_measurement is not None
        else None
    )
    anchor_token_count_source: str | None = None
    anchor_token_count_confidence: str | None = None
    provider_projection_applied = False
    if request_measurement is not None and projection_kind is not None:
        anchor_token_count_source = request_measurement.source
        anchor_token_count_confidence = request_measurement.confidence
        # Provider usage measures the whole request, not its startup portion.
        # Estimate that portion with the same measured/local ratio used to
        # project subsequent growth. Integer ceiling keeps an identical startup
        # request exact instead of introducing floating-point rounding drift.
        baseline = (
            baseline * request_measurement.input_tokens
            + request_measurement.anchor_estimate_tokens
            - 1
        ) // request_measurement.anchor_estimate_tokens
        if projection_kind == "exact_request":
            used = request_measurement.input_tokens
            token_count_source = request_measurement.source
            token_count_confidence = request_measurement.confidence
        else:
            scale = request_measurement.input_tokens / max(
                1,
                request_measurement.anchor_estimate_tokens,
            )
            persistent_delta = max(
                0,
                local_used - request_measurement.persistent_anchor_estimate_tokens,
            )
            used = max(
                0,
                request_measurement.input_tokens + math.ceil(persistent_delta * scale),
            )
            if request_measurement.source == UsageSource.LOCAL_ESTIMATE.value:
                token_count_source = UsageSource.LOCAL_ESTIMATE.value
                token_count_confidence = UsageConfidence.ESTIMATED.value
            else:
                token_count_source = UsageSource.MIXED.value
                token_count_confidence = UsageConfidence.ESTIMATED.value
                provider_projection_applied = True
    elif isinstance(prompt_estimate_multiplier, int | float):
        # Calibration is allowed to make the universal fallback more
        # conservative, never more optimistic. The caller supplies a
        # provider/protocol/operation-scoped robust percentile, so do not cap a
        # measured tokenizer gap at an arbitrary universal ratio.
        multiplier = max(1.0, float(prompt_estimate_multiplier))
        used = math.ceil(used * multiplier)
        baseline = math.ceil(baseline * multiplier)
    context_remaining = max(0, context_window - used) if context_window > 0 else None
    context_percent = (
        (context_remaining / context_window) * 100.0
        if (context_remaining is not None and context_window > 0)
        else None
    )
    effective_remaining = (
        max(0, effective_input_budget - used) if effective_input_budget > 0 else None
    )
    effective_percent = (
        (effective_remaining / effective_input_budget) * 100.0
        if (effective_remaining is not None and effective_input_budget > 0)
        else None
    )
    # Normalize remaining capacity against the space available after startup.
    # Use the same request count as the capacity budget: provider framing and
    # ephemeral wrappers occupy context too, even when history has not grown.
    dynamic_budget = max(0, effective_input_budget - baseline)
    dynamic_used = max(0, used - baseline)
    dynamic_remaining = max(0, dynamic_budget - dynamic_used)
    dynamic_percent = (dynamic_remaining / dynamic_budget) * 100.0 if dynamic_budget > 0 else 0.0
    return ContextLeft(
        model_name=model_name,
        max_input_tokens=context_window,
        used_input_tokens=used,
        remaining_tokens=context_remaining,
        percent_left=context_percent,
        source=meta.source,
        context_window_tokens=context_window,
        context_window_remaining_tokens=context_remaining,
        context_window_percent_left=context_percent,
        effective_input_budget=effective_input_budget,
        effective_remaining_tokens=effective_remaining,
        effective_percent_left=effective_percent,
        startup_baseline_tokens=baseline,
        dynamic_context_budget_tokens=dynamic_budget,
        dynamic_context_used_tokens=dynamic_used,
        dynamic_context_remaining_tokens=dynamic_remaining,
        dynamic_context_percent_left=dynamic_percent,
        token_count_source=token_count_source,
        token_count_confidence=token_count_confidence,
        local_request_estimate_tokens=local_used,
        anchor_token_count_source=anchor_token_count_source,
        anchor_token_count_confidence=anchor_token_count_confidence,
        provider_projection_applied=provider_projection_applied,
        capacity_provider_key=meta.provider_key,
        context_window_source=meta.field_sources.get("context_window_tokens", meta.source),
        max_output_tokens=meta.max_output_tokens,
        max_output_source=meta.field_sources.get("max_output_tokens", meta.source),
        safety_margin_tokens=max(0, int(safety_margin_tokens)),
    )


def format_usd(cost: float | None, style: str = "table") -> str:
    if cost is None:
        return "n/a"
    decimals = 3 if style == "hud" else 4
    return f"${cost:.{decimals}f}"


def format_cost(cost: float | None) -> str:
    # Backward-compatible alias for existing call sites.
    return format_usd(cost, style="table")


def format_context_percent(ctx: ContextLeft) -> str:
    if ctx.percent_left is None:
        return "n/a"
    return f"{ctx.percent_left:.1f}%"
