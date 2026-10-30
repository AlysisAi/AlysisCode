"""Provider response readings for display, independent of budgeting estimates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .llm.types import LLMUsage, UsageConfidence, UsageSource
from .session_store import read_session_events

_COUNTS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "uncached_tokens",
    "reasoning_tokens",
)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _validated_input_breakdown(value: Any, total: int | None) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or total is None:
        return None
    counts = {key: _count(value.get(key)) for key in ("instructions", "tools", "conversation")}
    if any(count is None for count in counts.values()) or sum(counts.values()) != total:
        return None
    return counts


def _provider_input_breakdown(usage: LLMUsage) -> dict[str, int] | None:
    """Read observed per-field attribution without estimating or retaining item IDs.

    Some Responses routes report this optional shape. Require a complete partition
    matching the provider input total; absent or inconsistent attribution stays
    unavailable. Nested item/content counts overlap and must not be added twice.
    """
    raw = usage.raw_provider_usage
    attribution = raw.get("attribution") if isinstance(raw, Mapping) else None
    if usage.input_tokens_scope != "request" or not isinstance(attribution, Mapping):
        return None
    fields, items = attribution.get("request_fields"), attribution.get("items")
    if not isinstance(fields, Mapping) or not isinstance(items, Mapping):
        return None
    item_counts = [
        _count(item.get("input_tokens")) if isinstance(item, Mapping) else None
        for item in items.values()
    ]
    if any(count is None for count in item_counts):
        return None
    counts = {"conversation": sum(item_counts)}
    for name in ("instructions", "tools"):
        field = fields.get(name)
        counts[name] = _count(field.get("input_tokens")) if isinstance(field, Mapping) else None
    return _validated_input_breakdown(counts, _count(usage.prompt_tokens))


def usage_route(client: Any) -> str:
    # A digest only; never persist credentials, URLs or account identifiers here.
    identity = getattr(client, "route_identity", None)
    values = [getattr(client, key, None) for key in ("model", "base_url", "provider_key")]
    values += [
        getattr(identity, key, None)
        for key in (
            "protocol",
            "profile_name",
            "auth_provider",
            "credential_scope",
            "routing_headers",
            "routing_fields",
        )
    ]
    return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()


def capture_provider_usage(usage: LLMUsage | None, *, client: Any) -> dict[str, Any] | None:
    if not isinstance(usage, LLMUsage) or usage.source != UsageSource.PROVIDER_RESPONSE:
        return None
    if usage.confidence == UsageConfidence.ESTIMATED:
        return None
    usage = usage.normalized()
    counts = dict(
        zip(
            _COUNTS,
            map(
                _count,
                (
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.total_tokens,
                    usage.cache_read_tokens,
                    usage.cache_creation_input_tokens,
                    usage.input_tokens_uncached,
                    usage.reasoning_tokens,
                ),
            ),
            strict=True,
        )
    )
    if all(value is None for value in counts.values()):
        return None
    breakdown = _provider_input_breakdown(usage)
    return {
        **counts,
        "uncached_derived": usage.input_tokens_uncached_derived,
        "input_scope": usage.input_tokens_scope,
        "model": str(getattr(client, "model", "")),
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "route_id": usage_route(client),
        **({"input_breakdown": breakdown} if breakdown is not None else {}),
    }


def validate_provider_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("model"), str):
        return None
    try:
        datetime.fromisoformat(value["measured_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(value.get("route_id"), str) or value.get("input_scope") not in {
        "request",
        "interaction",
    }:
        return None
    counts = {key: _count(value.get(key)) for key in _COUNTS}
    if all(count is None for count in counts.values()):
        return None
    breakdown = _validated_input_breakdown(value.get("input_breakdown"), counts["input_tokens"])
    return {
        **counts,
        **{key: value[key] for key in ("model", "measured_at", "route_id", "input_scope")},
        "uncached_derived": value.get("uncached_derived") is True,
        **(
            {"input_breakdown": breakdown}
            if breakdown is not None and value["input_scope"] == "request"
            else {}
        ),
    }


def session_provider_usage(session: Any) -> dict[str, Any] | None:
    reading = validate_provider_usage(getattr(session, "last_provider_usage", None))
    if reading is None or reading["route_id"] != usage_route(getattr(session, "client", None)):
        return None
    return {key: value for key, value in reading.items() if key != "route_id"}


def restore_provider_usage(session: Any, path: Path) -> None:
    """Restore the recorded reading, never recount a reconstructed conversation."""
    latest = None
    try:
        for event in read_session_events(path):
            if event.get("type") != "llm_usage":
                continue
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                reading = validate_provider_usage(payload.get("provider_context"))
                if reading is not None:
                    latest = reading
    except OSError:
        return
    session.last_provider_usage = latest
