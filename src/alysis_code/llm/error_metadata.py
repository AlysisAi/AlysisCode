"""Shared, bounded provider error metadata for HTTP and streaming adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from ..computer_use_diagnostics import DIAGNOSTIC_CODES, set_safe_provider_error_fields
from .rate_limit_metadata import HARD_QUOTA_CODES, retry_timing

_Error = TypeVar("_Error", bound=BaseException)


def provider_error_code(detail: Mapping[str, Any]) -> str | None:
    # A generic or unknown code must not hide a permanent quota in error.type.
    codes = [
        value
        for value in (detail.get("code"), detail.get("type"))
        if type(value) is str and value in DIAGNOSTIC_CODES
    ]
    return next((code for code in codes if code in HARD_QUOTA_CODES), next(iter(codes), None))


def apply_provider_error_metadata(
    error: _Error,
    *,
    status: int | None,
    detail: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> _Error:
    retry_after, reset_at = retry_timing(status, detail, headers=headers)
    return set_safe_provider_error_fields(
        error,
        status=status,
        code=provider_error_code(detail),
        parameter=detail.get("param"),
        retry_after=retry_after,
        reset_at=reset_at,
    )
