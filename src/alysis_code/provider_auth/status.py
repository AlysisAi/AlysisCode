"""Fresh provider-owned account identity for the CLI and desktop management."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import ProviderAuthAdapter, ProviderAuthError


def connected_credential_scope(status: Any) -> str | None:
    """Return scope from the same validated snapshot as the account status."""

    if not bool(status.connected) or not bool(status.verified):
        return None
    scope = str(getattr(status, "credential_scope", None) or "").strip()
    return scope or None


def native_provider_status_fields(
    provider_id: str, create_adapter: Callable[[str], ProviderAuthAdapter]
) -> dict[str, Any]:
    """Read a fresh provider-owned status for CLI and IDE callers; never cache identity."""
    try:
        adapter = create_adapter(provider_id)
        status = adapter.account_status()
    except ProviderAuthError as exc:
        return dict(connection=provider_id, authenticated=False, verified=False, error=str(exc))
    return dict(
        connection=provider_id,
        authenticated=status.connected,
        account_label=status.account_label,
        credential_scope=connected_credential_scope(status),
        detail=status.detail,
        transport=f"native Alysis Code client ({adapter.protocol})",
        display_name=adapter.display_name,
        verified=bool(status.verified),
    )
