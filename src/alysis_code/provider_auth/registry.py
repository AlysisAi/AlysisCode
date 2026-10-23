from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .base import ProviderAuthAdapter, ProviderAuthError, ProviderAuthSetupOption

ProviderAuthFactory = Callable[..., ProviderAuthAdapter]

_FACTORIES: dict[str, ProviderAuthFactory] = {}
_INSTANCES: dict[str, ProviderAuthAdapter] = {}


def register_provider_auth(provider_id: str, factory: ProviderAuthFactory) -> None:
    normalized = str(provider_id or "").strip()
    if not normalized:
        raise ProviderAuthError("Provider auth id cannot be empty.")
    _FACTORIES[normalized] = factory


def create_provider_auth(
    provider_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ProviderAuthAdapter:
    _ensure_builtins()
    normalized = str(provider_id or "").strip()
    factory = _FACTORIES.get(normalized)
    if factory is None:
        available = ", ".join(sorted(_FACTORIES)) or "none"
        raise ProviderAuthError(
            f"Unknown provider authentication adapter {normalized or '(empty)'!r}. "
            f"Available adapters: {available}."
        )
    if transport is not None:
        return factory(transport=transport)
    instance = _INSTANCES.get(normalized)
    if instance is None:
        instance = factory(transport=None)
        _INSTANCES[normalized] = instance
    return instance


@dataclass(frozen=True, slots=True)
class ProviderAuthConnection:
    """The transport a subscription adapter owns.

    ``legacy_base_urls`` are endpoints the adapter used in earlier releases; a profile
    saved then still names one of them and is moved to ``base_url`` on load.
    """

    protocol: str
    base_url: str
    legacy_base_urls: tuple[str, ...] = ()


def provider_auth_connection(provider_id: str) -> ProviderAuthConnection | None:
    """Return the adapter's protocol and endpoint, read without touching credentials.

    Unknown adapters return None so callers keep their own error for them.
    """

    _ensure_builtins()
    normalized = str(provider_id or "").strip()
    factory = _FACTORIES.get(normalized)
    if factory is None:
        return None
    source: object = factory
    protocol = getattr(source, "protocol", None)
    base_url = getattr(source, "base_url", None)
    if not isinstance(protocol, str) or not isinstance(base_url, str):
        source = create_provider_auth(normalized)
        protocol = getattr(source, "protocol", None)
        base_url = getattr(source, "base_url", None)
    if not isinstance(protocol, str) or not isinstance(base_url, str):
        return None
    legacy = getattr(source, "legacy_base_urls", ())
    return ProviderAuthConnection(
        protocol=protocol,
        base_url=base_url,
        legacy_base_urls=tuple(str(item) for item in legacy or () if str(item).strip()),
    )


def provider_auth_ids() -> tuple[str, ...]:
    _ensure_builtins()
    return tuple(sorted(_FACTORIES))


def provider_auth_setup_options() -> tuple[ProviderAuthSetupOption, ...]:
    return tuple(
        ProviderAuthSetupOption(
            id=provider_id,
            label=adapter.display_name,
            description=adapter.description,
            auth_hint=str(getattr(adapter, "auth_hint", "") or ""),
        )
        for provider_id in provider_auth_ids()
        for adapter in (create_provider_auth(provider_id),)
    )


def _ensure_builtins() -> None:
    if "openai-codex" in _FACTORIES:
        return
    from .openai_codex import OpenAICodexSubscriptionAuth

    register_provider_auth("openai-codex", OpenAICodexSubscriptionAuth)


__all__ = [
    "ProviderAuthConnection",
    "create_provider_auth",
    "provider_auth_connection",
    "provider_auth_ids",
    "provider_auth_setup_options",
    "register_provider_auth",
]
