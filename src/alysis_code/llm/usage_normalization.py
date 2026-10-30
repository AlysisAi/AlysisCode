"""Normalize usage payloads shared by OpenAI-compatible provider routes.

Compatibility describes the transport envelope, not the provider's usage
schema.  This module deliberately decodes documented *shapes* instead of
maintaining a provider-name switch: gateways and first-party endpoints often
surface the same cache fields, while a single gateway can return several
upstream schemas.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from .types import LLMUsage
from .types import parse_token_count as _non_negative_int


def _non_negative_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed is not None and math.isfinite(parsed) and parsed >= 0 else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _non_negative_int(value)
        if parsed is not None:
            return parsed
    return None


def _provider_cost_usd(
    raw: Mapping[str, Any], *, provider_key: str | None
) -> tuple[float | None, str | None]:
    """Return only provider amounts whose unit is explicitly USD-compatible.

    Perplexity-style payloads identify ``total_cost`` and may name the
    currency. OpenRouter documents its numeric ``usage.cost`` as the charged
    account-credit amount; OpenRouter credits are USD-denominated. A generic
    numeric ``cost`` is otherwise ambiguous and is intentionally ignored.
    """

    cost = raw.get("cost")
    if isinstance(cost, Mapping):
        currency = str(cost.get("currency") or "USD").strip().upper()
        if currency != "USD":
            return None, currency or None
        total = _non_negative_float(cost.get("total_cost"))
        if total is None:
            total = _non_negative_float(cost.get("total"))
        return total, "USD" if total is not None else None

    normalized_provider = str(provider_key or "").strip().lower()
    if normalized_provider == "openrouter":
        amount = _non_negative_float(cost)
        if amount is not None:
            return amount, "USD"

    currency = str(raw.get("currency") or "").strip().upper()
    if currency in {"", "USD"}:
        amount = _non_negative_float(raw.get("total_cost"))
        if amount is not None:
            return amount, "USD"
    return None, currency or None


def parse_compatible_usage(
    raw: Any,
    *,
    provider_key: str | None = None,
    responses_shape: bool = False,
) -> LLMUsage | None:
    """Decode common provider usage variants without inventing absent values."""

    if not isinstance(raw, Mapping):
        return None

    prompt_details = _mapping(raw.get("prompt_tokens_details"))
    input_details = _mapping(raw.get("input_tokens_details"))
    completion_details = _mapping(raw.get("completion_tokens_details"))
    output_details = _mapping(raw.get("output_tokens_details"))
    token_group = _mapping(raw.get("tokens"))
    billed_units = _mapping(raw.get("billed_units"))

    if responses_shape:
        prompt = _first_int(
            raw.get("input_tokens"), raw.get("prompt_tokens"), token_group.get("input_tokens")
        )
        completion = _first_int(
            raw.get("output_tokens"),
            raw.get("completion_tokens"),
            token_group.get("output_tokens"),
        )
    else:
        prompt = _first_int(
            raw.get("prompt_tokens"), raw.get("input_tokens"), token_group.get("input_tokens")
        )
        completion = _first_int(
            raw.get("completion_tokens"),
            raw.get("output_tokens"),
            token_group.get("output_tokens"),
        )

    cache_read = _first_int(
        prompt_details.get("cached_tokens"),
        input_details.get("cached_tokens"),
        prompt_details.get("cache_read_input_tokens"),
        input_details.get("cache_read_input_tokens"),
        raw.get("cache_read_input_tokens"),
        raw.get("cached_prompt_tokens"),
        raw.get("cached_tokens"),
        raw.get("prompt_cache_hit_tokens"),
        raw.get("cached_prompt_text_tokens"),
    )
    cache_write = _first_int(
        prompt_details.get("cache_write_tokens"),
        input_details.get("cache_write_tokens"),
        prompt_details.get("cache_creation_input_tokens"),
        input_details.get("cache_creation_input_tokens"),
        raw.get("cache_creation_input_tokens"),
        raw.get("cache_write_tokens"),
    )
    cache_creation = _mapping(raw.get("cache_creation"))
    cache_creation_5m = _first_int(
        cache_creation.get("ephemeral_5m_input_tokens"),
        prompt_details.get("cache_creation_5m_input_tokens"),
        input_details.get("cache_creation_5m_input_tokens"),
    )
    cache_creation_1h = _first_int(
        cache_creation.get("ephemeral_1h_input_tokens"),
        prompt_details.get("cache_creation_1h_input_tokens"),
        input_details.get("cache_creation_1h_input_tokens"),
    )
    if cache_write is None:
        write_parts = [
            value for value in (cache_creation_5m, cache_creation_1h) if value is not None
        ]
        if write_parts:
            cache_write = sum(write_parts)

    cache_miss = _first_int(
        raw.get("prompt_cache_miss_tokens"),
        raw.get("input_tokens_uncached"),
        raw.get("uncached_prompt_tokens"),
        prompt_details.get("uncached_tokens"),
        input_details.get("uncached_tokens"),
    )
    cache_miss_derived = False
    if prompt is None and cache_read is not None and cache_miss is not None:
        prompt = cache_read + cache_miss + (cache_write or 0)
    if (
        cache_miss is None
        and prompt is not None
        and (cache_read is not None or cache_write is not None)
        and prompt >= (cache_read or 0) + (cache_write or 0)
    ):
        # Implied by the counts the provider did send — usable, but not itself
        # a reported figure.
        cache_miss = prompt - (cache_read or 0) - (cache_write or 0)
        cache_miss_derived = True

    reasoning = _first_int(
        output_details.get("reasoning_tokens"),
        completion_details.get("reasoning_tokens"),
        raw.get("reasoning_tokens"),
    )
    total = _first_int(raw.get("total_tokens"))
    provider_cost_usd, provider_cost_currency = _provider_cost_usd(raw, provider_key=provider_key)

    # Cohere's native schema distinguishes context tokens from billed units.
    # Preserve the provider's context counts above and retain billed units only
    # in raw_provider_usage; substituting them would corrupt context accounting.
    has_billed_units = bool(billed_units)
    if (
        prompt is None
        and completion is None
        and total is None
        and cache_read is None
        and cache_write is None
        and reasoning is None
        and provider_cost_usd is None
        and not has_billed_units
    ):
        return None

    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_prompt_tokens=cache_read,
        input_tokens_uncached=cache_miss,
        input_tokens_uncached_derived=cache_miss_derived,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        cache_creation_5m_input_tokens=cache_creation_5m,
        cache_creation_1h_input_tokens=cache_creation_1h,
        reasoning_tokens=reasoning,
        provider_cost_usd=provider_cost_usd,
        provider_cost_currency=provider_cost_currency,
        raw_provider_usage=copy.deepcopy(dict(raw)),
    )


__all__ = ["parse_compatible_usage"]
