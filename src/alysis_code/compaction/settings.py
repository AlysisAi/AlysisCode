from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from alysis_code.branding import env_get
from alysis_code.config import AppConfig
from alysis_code.model_metadata_utils import (
    parse_bool,
    parse_non_negative_float,
    parse_positive_int,
)


@dataclass(frozen=True)
class CompactionSettings:
    enabled: bool = True
    offload_tool_outputs: bool = True
    tool_output_offload_threshold_chars: int = 6000
    tool_output_preview_chars: int = 2000
    summarize_conversation: bool = True
    recent_user_turns_to_keep: int = 8
    trigger_ratio: float = 0.90
    target_ratio: float = 0.70
    max_chunk_messages: int = 60
    safety_margin_tokens: int = 512
    execution_min_removable_messages: int = 2
    execution_min_removable_tokens: int = 1024
    recent_tail_messages: int | None = None
    max_pins: int = 20
    max_pins_chars: int = 8000
    pin_snippet_chars: int = 600
    cache_aware_compaction: bool = True
    cache_aware_min_trigger_ratio: float = 0.72


def _resolve_bool(*, raw: Any, fallback: bool) -> bool:
    parsed = parse_bool(raw)
    return fallback if parsed is None else parsed


def _resolve_positive_int(*, raw: Any, fallback: int) -> int:
    parsed = parse_positive_int(raw)
    if parsed is None:
        return fallback
    return parsed


def _resolve_ratio(*, raw: Any, fallback: float) -> float:
    parsed = parse_non_negative_float(raw)
    if parsed is None:
        return fallback
    if parsed <= 0.05 or parsed >= 0.98:
        return fallback
    return parsed


def _resolve_optional_count(*, raw: Any, fallback: int | None) -> int | None:
    parsed = parse_non_negative_float(raw)
    if parsed is None or not math.isfinite(parsed) or parsed != int(parsed):
        return fallback
    return int(parsed)


def resolve_compaction_settings(cfg: AppConfig) -> CompactionSettings:
    """
    Resolve compaction settings from environment and cfg.extra_fields.

    Precedence:
    1) defaults
    2) env vars
    3) cfg.extra_fields["compaction"] (highest priority)
    """

    settings = CompactionSettings()

    enabled = _resolve_bool(
        raw=env_get("ALYSIS_ENABLE_COMPACTION"),
        fallback=settings.enabled,
    )
    offload_tool_outputs = _resolve_bool(
        raw=env_get("ALYSIS_OFFLOAD_TOOL_OUTPUTS"),
        fallback=settings.offload_tool_outputs,
    )
    threshold_chars = _resolve_positive_int(
        raw=env_get("ALYSIS_TOOL_OUTPUT_OFFLOAD_THRESHOLD_CHARS"),
        fallback=settings.tool_output_offload_threshold_chars,
    )
    preview_chars = _resolve_positive_int(
        raw=env_get("ALYSIS_TOOL_OUTPUT_PREVIEW_CHARS"),
        fallback=settings.tool_output_preview_chars,
    )
    summarize_conversation = _resolve_bool(
        raw=env_get("ALYSIS_SUMMARIZE_CONVERSATION"),
        fallback=settings.summarize_conversation,
    )
    recent_user_turns_to_keep = _resolve_positive_int(
        raw=env_get("ALYSIS_COMPACTION_RECENT_TURNS"),
        fallback=settings.recent_user_turns_to_keep,
    )
    trigger_ratio = _resolve_ratio(
        raw=env_get("ALYSIS_COMPACTION_TRIGGER_RATIO"),
        fallback=settings.trigger_ratio,
    )
    target_ratio = _resolve_ratio(
        raw=env_get("ALYSIS_COMPACTION_TARGET_RATIO"),
        fallback=settings.target_ratio,
    )
    max_chunk_messages = _resolve_positive_int(
        raw=env_get("ALYSIS_COMPACTION_MAX_CHUNK_MESSAGES"),
        fallback=settings.max_chunk_messages,
    )
    safety_margin_tokens = _resolve_positive_int(
        raw=env_get("ALYSIS_COMPACTION_SAFETY_MARGIN_TOKENS"),
        fallback=settings.safety_margin_tokens,
    )
    execution_min_removable_messages = _resolve_positive_int(
        raw=env_get("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_MESSAGES"),
        fallback=settings.execution_min_removable_messages,
    )
    execution_min_removable_tokens = _resolve_positive_int(
        raw=env_get("ALYSIS_EXECUTION_COMPACTION_MIN_REMOVABLE_TOKENS"),
        fallback=settings.execution_min_removable_tokens,
    )
    recent_tail_messages = _resolve_optional_count(
        raw=env_get("ALYSIS_COMPACTION_RECENT_TAIL_MESSAGES"),
        fallback=settings.recent_tail_messages,
    )
    max_pins = _resolve_positive_int(
        raw=env_get("ALYSIS_MAX_PINS"),
        fallback=settings.max_pins,
    )
    max_pins_chars = _resolve_positive_int(
        raw=env_get("ALYSIS_MAX_PINS_CHARS"),
        fallback=settings.max_pins_chars,
    )
    pin_snippet_chars = _resolve_positive_int(
        raw=env_get("ALYSIS_PIN_SNIPPET_CHARS"),
        fallback=settings.pin_snippet_chars,
    )
    cache_aware_compaction = _resolve_bool(
        raw=env_get("ALYSIS_CACHE_AWARE_COMPACTION"),
        fallback=settings.cache_aware_compaction,
    )
    cache_aware_min_trigger_ratio = _resolve_ratio(
        raw=env_get("ALYSIS_CACHE_AWARE_MIN_TRIGGER_RATIO"),
        fallback=settings.cache_aware_min_trigger_ratio,
    )

    raw_compaction = (
        cfg.extra_fields.get("compaction") if isinstance(cfg.extra_fields, dict) else None
    )
    if isinstance(raw_compaction, dict):
        enabled = _resolve_bool(raw=raw_compaction.get("enabled"), fallback=enabled)
        offload_tool_outputs = _resolve_bool(
            raw=raw_compaction.get("offload_tool_outputs"),
            fallback=offload_tool_outputs,
        )
        threshold_chars = _resolve_positive_int(
            raw=raw_compaction.get("tool_output_offload_threshold_chars"),
            fallback=threshold_chars,
        )
        preview_chars = _resolve_positive_int(
            raw=raw_compaction.get("tool_output_preview_chars"),
            fallback=preview_chars,
        )
        summarize_conversation = _resolve_bool(
            raw=raw_compaction.get("summarize_conversation"),
            fallback=summarize_conversation,
        )
        recent_user_turns_to_keep = _resolve_positive_int(
            raw=raw_compaction.get("recent_user_turns_to_keep"),
            fallback=recent_user_turns_to_keep,
        )
        trigger_ratio = _resolve_ratio(
            raw=raw_compaction.get("trigger_ratio"),
            fallback=trigger_ratio,
        )
        target_ratio = _resolve_ratio(
            raw=raw_compaction.get("target_ratio"),
            fallback=target_ratio,
        )
        max_chunk_messages = _resolve_positive_int(
            raw=raw_compaction.get("max_chunk_messages"),
            fallback=max_chunk_messages,
        )
        safety_margin_tokens = _resolve_positive_int(
            raw=raw_compaction.get("safety_margin_tokens"),
            fallback=safety_margin_tokens,
        )
        execution_min_removable_messages = _resolve_positive_int(
            raw=raw_compaction.get("execution_min_removable_messages"),
            fallback=execution_min_removable_messages,
        )
        execution_min_removable_tokens = _resolve_positive_int(
            raw=raw_compaction.get("execution_min_removable_tokens"),
            fallback=execution_min_removable_tokens,
        )
        recent_tail_messages = _resolve_optional_count(
            raw=raw_compaction.get("recent_tail_messages"),
            fallback=recent_tail_messages,
        )
        max_pins = _resolve_positive_int(
            raw=raw_compaction.get("max_pins"),
            fallback=max_pins,
        )
        max_pins_chars = _resolve_positive_int(
            raw=raw_compaction.get("max_pins_chars"),
            fallback=max_pins_chars,
        )
        pin_snippet_chars = _resolve_positive_int(
            raw=raw_compaction.get("pin_snippet_chars"),
            fallback=pin_snippet_chars,
        )
        cache_aware_compaction = _resolve_bool(
            raw=raw_compaction.get("cache_aware_compaction"),
            fallback=cache_aware_compaction,
        )
        cache_aware_min_trigger_ratio = _resolve_ratio(
            raw=raw_compaction.get("cache_aware_min_trigger_ratio"),
            fallback=cache_aware_min_trigger_ratio,
        )

    if target_ratio >= trigger_ratio:
        trigger_ratio = settings.trigger_ratio
        target_ratio = settings.target_ratio

    return CompactionSettings(
        enabled=enabled,
        offload_tool_outputs=offload_tool_outputs,
        tool_output_offload_threshold_chars=threshold_chars,
        tool_output_preview_chars=preview_chars,
        summarize_conversation=summarize_conversation,
        recent_user_turns_to_keep=recent_user_turns_to_keep,
        trigger_ratio=trigger_ratio,
        target_ratio=target_ratio,
        max_chunk_messages=max_chunk_messages,
        safety_margin_tokens=safety_margin_tokens,
        execution_min_removable_messages=execution_min_removable_messages,
        execution_min_removable_tokens=execution_min_removable_tokens,
        recent_tail_messages=recent_tail_messages,
        max_pins=max_pins,
        max_pins_chars=max_pins_chars,
        pin_snippet_chars=pin_snippet_chars,
        cache_aware_compaction=cache_aware_compaction,
        cache_aware_min_trigger_ratio=cache_aware_min_trigger_ratio,
    )
