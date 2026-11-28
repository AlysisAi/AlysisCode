"""Bounded provider quota metadata; never retains response bodies or messages."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections.abc import Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

HARD_QUOTA_CODES = frozenset(
    {"usage_limit_reached", "insufficient_quota", "billing_hard_limit_reached"}
)
MAX_RETRY_SECONDS = 31 * 24 * 60 * 60
MAX_RESET_AT = 4_102_444_800  # 2100-01-01
RETRY_TIMING_STATUS_CODES = frozenset({408, 429, *range(500, 600)})

BILLING_ERROR_CODE = "billing_hard_limit_reached"
_BILLING_CODES = frozenset(
    {
        BILLING_ERROR_CODE,
        "insufficient_quota",
        "insufficient_balance",
        "insufficient_funds",
        "insufficient_credits",
        "credits_exhausted",
        "credit_balance_exhausted",
        "payment_required",
        "billing_not_active",
        "billing_disabled",
        "subscription_required",
        "subscription_expired",
    }
)
_BILLING_MESSAGE = re.compile(
    r"\b(?:insufficient (?:account |credit )?(?:balance|credits?|funds)"
    r"|(?:credit |account )?balance is (?:too low|insufficient)"
    r"|out of credits|not enough credits|no (?:remaining credits|credits (?:left|remaining))|payment required"
    r"|no (?:active (?:subscription|plan)|resource package)"
    r"|(?:subscription|plan) (?:has expired|is (?:expired|inactive))"
    r"|(?:subscription|plan) does not (?:yet )?include access to"
    r"|(?:billing|spending) hard limit (?:has been )?reached)\b",
    re.IGNORECASE,
)


def requires_provider_billing(status: int | None, detail: Mapping[str, Any]) -> bool:
    """Match an explicit funding or subscription rejection in the provider's error fields."""
    if status == 402:
        return True
    if status not in {None, 400, 401, 403, 429}:
        return False
    for field in ("code", "type", "status", "reason"):
        code = detail.get(field)
        if isinstance(code, str) and code.casefold() in _BILLING_CODES:
            return True
    message = detail.get("message")
    if not isinstance(message, str):
        return False
    normalized = " ".join(unicodedata.normalize("NFKC", message).split())
    return _BILLING_MESSAGE.search(normalized) is not None


def provider_error_detail(data: Any) -> Mapping[str, Any]:
    """Read only the error envelope (or a plain-text rejection), never other payload fields."""
    if isinstance(data, str):
        return {"message": data}
    if not isinstance(data, Mapping):
        return {}
    nested = data.get("error")
    if isinstance(nested, Mapping):
        return nested
    if isinstance(nested, str) and not data.get("message"):
        return {**data, "message": nested}
    return data


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
    return retry_timing(response.status_code, detail, headers=response.headers, now=now)


def retry_timing(
    status_code: int | None,
    detail: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    now: float | None = None,
) -> tuple[int, int]:
    """Retain server minimum waits; subscription reset fields apply only to 429.

    HTTP errors and errors inside an HTTP-200 stream use the same bounded
    metadata. Keeping timing does not grant retry authority or extend a budget.
    """
    if status_code not in RETRY_TIMING_STATUS_CODES:
        return 0, 0
    instant = time.time() if now is None else now
    reset_at = bounded_reset_at(detail.get("resets_at")) if status_code == 429 else 0
    waits = [bounded_seconds(detail.get("retry_after_seconds"))]
    if status_code == 429:
        waits.append(bounded_seconds(detail.get("resets_in_seconds")))
    if reset_at:
        waits.append(bounded_seconds(max(0, reset_at - instant)))
    header = (headers or {}).get("retry-after", "")
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
    reset_at = (
        max(reset_at, bounded_reset_at(math.ceil(instant + wait)) if wait else 0)
        if status_code == 429
        else 0
    )
    return wait, reset_at
