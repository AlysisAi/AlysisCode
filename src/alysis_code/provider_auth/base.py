from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..llm.cache_capabilities import CacheCapabilitySpec


class ProviderAuthError(RuntimeError):
    """A provider account could not be connected or authorized safely."""


class ProviderLoginRequiredError(ProviderAuthError):
    """The selected provider account must be connected again."""


class ProviderCredentialScopeMismatchError(ProviderAuthError):
    """The live provider credential generation differs from the approved route."""


@dataclass(frozen=True, slots=True)
class ProviderAccountStatus:
    connected: bool
    verified: bool = True
    account_label: str | None = None
    detail: str | None = None
    credential_scope: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRequestAuthorization:
    """One exact provider credential snapshot prepared for a single HTTP request."""

    credential_scope: str
    headers: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class ProviderReasoningEffort:
    id: str
    label: str
    description: str = ""


_REASONING_EFFORT_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
}


def reasoning_effort_label(effort_id: str) -> str:
    """The name the engine shows for a provider reasoning-effort id.

    Hosts display this label as given. Known ids use their documented names
    (``xhigh`` is "Extra high"); any other id is turned into words
    (``very_high`` becomes "Very high"), so a new provider effort still reads well.
    """

    normalized = str(effort_id or "").strip()
    known = _REASONING_EFFORT_LABELS.get(normalized.casefold())
    if known is not None:
        return known
    words = normalized.replace("_", " ").replace("-", " ").split()
    return " ".join(words).capitalize() if words else normalized


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    label: str
    description: str = ""
    is_default: bool = False
    reasoning_efforts: tuple[ProviderReasoningEffort, ...] = ()
    default_reasoning_effort: str | None = None
    input_modalities: tuple[str, ...] = ("text",)
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderAuthSetupOption:
    id: str
    label: str
    description: str
    auth_hint: str = ""


@runtime_checkable
class ProviderAuthAdapter(Protocol):
    """Native model-transport auth owned by a provider-specific adapter."""

    provider_id: str
    display_name: str
    description: str
    profile_name: str
    auth_hint: str
    base_url: str
    protocol: str
    supports_previous_response_id: bool
    supports_temperature: bool
    requires_streaming: bool
    cache_capability: CacheCapabilitySpec | None

    def account_status(self) -> ProviderAccountStatus: ...

    def route_credential_scope(self) -> str:
        """Return a stable, non-secret identity for the live provider credential route."""

        ...

    def login(
        self,
        method: str = "browser",
        *,
        output_write: Callable[[str], None] | None = None,
    ) -> ProviderAccountStatus: ...

    def logout(self) -> ProviderAccountStatus: ...

    def list_models(self, *, refresh: bool = False) -> tuple[ProviderModel, ...]: ...

    def authorization_headers(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
        expected_credential_scope: str | None = None,
        cancellation_token: Any | None = None,
    ) -> Mapping[str, str]: ...

    def authorize_request(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
        expected_credential_scope: str | None = None,
        cancellation_token: Any | None = None,
    ) -> ProviderRequestAuthorization: ...

    def adapt_responses_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


__all__ = [
    "ProviderAccountStatus",
    "ProviderAuthAdapter",
    "ProviderAuthError",
    "ProviderCredentialScopeMismatchError",
    "ProviderAuthSetupOption",
    "ProviderLoginRequiredError",
    "ProviderModel",
    "ProviderRequestAuthorization",
    "ProviderReasoningEffort",
]
