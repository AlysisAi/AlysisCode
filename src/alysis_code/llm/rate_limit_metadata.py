"""Bounded provider quota metadata; never retains response bodies or messages."""

from __future__ import annotations

import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

HARD_QUOTA_CODES = frozenset(
    {"usage_limit_reached", "insufficient_quota", "billing_hard_limit_reached"}
)
MAX_RETRY_SECONDS = 31 * 24 * 60 * 60
MAX_RESET_AT = 4_102_444_800  # 2100-01-01


def bounded_seconds(value: Any) -> int:
    if type(value) not in (int, float) or value < 0:
        return 0
    if type(value) is float and not math.isfinite(value):
        return 0
    # Saturate long waits instead of treating them as permission to retry now.
    return math.ceil(min(value, MAX_RETRY_SECONDS))


def bounded_reset_at(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= MAX_RESET_AT else 0


def retry_metadata(response: Any, detail: dict, *, now: float | None = None) -> tuple[int, int]:
    """Read standard Retry-After and subscription error reset fields for HTTP 429."""
    if response.status_code != 429:
        return 0, 0
    instant = time.time() if now is None else now
    reset_at = bounded_reset_at(detail.get("resets_at"))
    waits = [
        bounded_seconds(detail.get("resets_in_seconds")),
        bounded_seconds(detail.get("retry_after_seconds")),
    ]
    if reset_at:
        waits.append(bounded_seconds(max(0, reset_at - instant)))
    header = response.headers.get("retry-after", "")
    if len(header) <= 80:
        try:
            waits.append(bounded_seconds(float(header)))
        except (ValueError, OverflowError):
            try:
                date = parsedate_to_datetime(header)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                waits.append(bounded_seconds(max(0, date.timestamp() - instant)))
            except (ValueError, TypeError, OverflowError):
                pass
    wait = max(waits)
    return wait, max(reset_at, bounded_reset_at(math.ceil(instant + wait)) if wait else 0)
