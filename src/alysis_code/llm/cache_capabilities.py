from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
    ProviderProtocolCapabilities,
)

if TYPE_CHECKING:
    from ..profile_presets import ProfilePreset
    from ..profiles import ProfileSpec


CACHE_STRATEGY_INHERIT = "inherit"
CACHE_STRATEGY_NONE = "none"
CACHE_STRATEGY_IMPLICIT_PROVIDER = "implicit_provider"
CACHE_STRATEGY_OPENAI_PROMPT_CACHE = "openai_prompt_cache"
CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY = "mistral_prompt_cache_key"
CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL = "anthropic_cache_control"
CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT = "gemini_explicit_cached_content"
CACHE_STRATEGY_GEMINI_IMPLICIT = "gemini_implicit"
CACHE_STRATEGY_OPENROUTER_STICKY_SESSION = "openrouter_sticky_session"
CACHE_STRATEGY_XAI_CONVERSATION_HEADER = "xai_conversation_header"
CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS = "qwen_cache_control_blocks"

KNOWN_CACHE_STRATEGIES: frozenset[str] = frozenset(
    {
        CACHE_STRATEGY_NONE,
        CACHE_STRATEGY_IMPLICIT_PROVIDER,
        CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
        CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
        CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL,
        CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT,
        CACHE_STRATEGY_GEMINI_IMPLICIT,
        CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
        CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
        CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    }
)

PROFILE_CACHE_STRATEGIES: frozenset[str] = frozenset(
    {CACHE_STRATEGY_INHERIT, *KNOWN_CACHE_STRATEGIES}
)

CACHE_USAGE_SCHEMA_NONE = "none"
CACHE_USAGE_SCHEMA_OPENAI = "openai"
CACHE_USAGE_SCHEMA_ANTHROPIC = "anthropic"
CACHE_USAGE_SCHEMA_GEMINI = "gemini"
CACHE_USAGE_SCHEMA_PROVIDER = "provider"

KNOWN_CACHE_USAGE_SCHEMAS: frozenset[str] = frozenset(
    {
        CACHE_USAGE_SCHEMA_NONE,
        CACHE_USAGE_SCHEMA_OPENAI,
        CACHE_USAGE_SCHEMA_ANTHROPIC,
        CACHE_USAGE_SCHEMA_GEMINI,
        CACHE_USAGE_SCHEMA_PROVIDER,
    }
)

PROMPT_CACHE_KEY_FIELD = "prompt_cache_key"
PROMPT_CACHE_RETENTION_FIELD = "prompt_cache_retention"
CACHE_CONTROL_FIELD = "cache_control"
CACHED_CONTENT_FIELD = "cached_content"
OPENROUTER_SESSION_ID_FIELD = "session_id"
OPENROUTER_SESSION_ID_HEADER_FIELD = "x-session-id"
XAI_CONVERSATION_ID_HEADER_FIELD = "x-grok-conv-id"

KNOWN_CACHE_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        PROMPT_CACHE_KEY_FIELD,
        PROMPT_CACHE_RETENTION_FIELD,
        CACHE_CONTROL_FIELD,
        CACHED_CONTENT_FIELD,
        OPENROUTER_SESSION_ID_FIELD,
        OPENROUTER_SESSION_ID_HEADER_FIELD,
        XAI_CONVERSATION_ID_HEADER_FIELD,
    }
)


@dataclass(frozen=True)
class CacheCapabilitySpec:
    """Declarative cache capability metadata for profiles and presets.

    ``None`` fields mean "inherit from the lower-precedence capability source".
    Persisted user profiles should store this object under ``cache_capability``.
    """

    strategy: str = CACHE_STRATEGY_INHERIT
    enabled: bool | None = None
    supports_prompt_cache_key: bool | None = None
    supports_prompt_cache_retention: bool | None = None
    supports_cache_control: bool | None = None
    supports_explicit_cached_content: bool | None = None
    reports_cache_read_tokens: bool | None = None
    reports_cache_write_tokens: bool | None = None
    emits_request_fields: bool | None = None
    request_fields: tuple[str, ...] = field(default_factory=tuple)
    usage_schema: str | None = None
    min_cacheable_tokens: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    source: str = "profile"
    model_overrides: tuple[tuple[str, CacheCapabilitySpec], ...] = ()
    model_family_overrides: tuple[tuple[str, CacheCapabilitySpec], ...] = ()
    endpoint_overrides: tuple[tuple[str, CacheCapabilitySpec], ...] = ()

    def __post_init__(self) -> None:
        strategy = normalize_cache_strategy(
            self.strategy,
            allow_inherit=True,
            field_name=f"{self.source} cache strategy",
        )
        usage_schema = (
            normalize_cache_usage_schema(
                self.usage_schema,
                field_name=f"{self.source} cache usage_schema",
            )
            if self.usage_schema is not None
            else None
        )
        min_tokens = self.min_cacheable_tokens
        if min_tokens is not None:
            try:
                min_tokens = int(min_tokens)
            except (TypeError, ValueError):
                raise ValueError(
                    f"{self.source} cache min_cacheable_tokens must be an integer."
                ) from None
            if min_tokens < 0:
                raise ValueError(f"{self.source} cache min_cacheable_tokens must be non-negative.")
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "usage_schema", usage_schema)
        object.__setattr__(self, "min_cacheable_tokens", min_tokens)
        object.__setattr__(
            self,
            "request_fields",
            _request_field_tuple(
                self.request_fields,
                field_name=f"{self.source} cache request_fields",
            ),
        )
        object.__setattr__(
            self,
            "notes",
            tuple(note for note in (str(item).strip() for item in self.notes) if note),
        )
        object.__setattr__(self, "source", str(self.source or "profile").strip() or "profile")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        source: str = "profile",
    ) -> CacheCapabilitySpec | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError(f"{source} cache_capability must be an object.")
        strategy = value.get("strategy", value.get("cache_strategy", CACHE_STRATEGY_INHERIT))
        return cls(
            strategy=str(strategy or CACHE_STRATEGY_INHERIT).strip(),
            enabled=_optional_bool(value.get("enabled"), field_name=f"{source} cache enabled"),
            supports_prompt_cache_key=_optional_bool(
                value.get("supports_prompt_cache_key"),
                field_name=f"{source} cache supports_prompt_cache_key",
            ),
            supports_prompt_cache_retention=_optional_bool(
                value.get("supports_prompt_cache_retention"),
                field_name=f"{source} cache supports_prompt_cache_retention",
            ),
            supports_cache_control=_optional_bool(
                value.get("supports_cache_control"),
                field_name=f"{source} cache supports_cache_control",
            ),
            supports_explicit_cached_content=_optional_bool(
                value.get("supports_explicit_cached_content"),
                field_name=f"{source} cache supports_explicit_cached_content",
            ),
            reports_cache_read_tokens=_optional_bool(
                value.get("reports_cache_read_tokens"),
                field_name=f"{source} cache reports_cache_read_tokens",
            ),
            reports_cache_write_tokens=_optional_bool(
                value.get("reports_cache_write_tokens"),
                field_name=f"{source} cache reports_cache_write_tokens",
            ),
            emits_request_fields=_optional_bool(
                value.get(
                    "emits_request_fields",
                    value.get("emit_request_fields", value.get("request_fields_enabled")),
                ),
                field_name=f"{source} cache emits_request_fields",
            ),
            request_fields=_request_field_tuple(
                value.get("request_fields"),
                field_name=f"{source} cache request_fields",
            ),
            usage_schema=(
                str(value.get("usage_schema")).strip()
                if value.get("usage_schema") is not None
                else None
            ),
            min_cacheable_tokens=_optional_int(
                value.get("min_cacheable_tokens"),
                field_name=f"{source} cache min_cacheable_tokens",
            ),
            notes=_string_tuple(value.get("notes"), field_name=f"{source} cache notes"),
            source=source,
            model_overrides=_scoped_specs(
                value.get("models"),
                source=f"{source} cache models",
            ),
            model_family_overrides=_scoped_specs(
                value.get("model_families"),
                source=f"{source} cache model_families",
            ),
            endpoint_overrides=_scoped_specs(
                value.get("endpoints"),
                source=f"{source} cache endpoints",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.strategy != CACHE_STRATEGY_INHERIT:
            data["strategy"] = self.strategy
        for key in (
            "enabled",
            "supports_prompt_cache_key",
            "supports_prompt_cache_retention",
            "supports_cache_control",
            "supports_explicit_cached_content",
            "reports_cache_read_tokens",
            "reports_cache_write_tokens",
            "emits_request_fields",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = bool(value)
        if self.request_fields:
            data["request_fields"] = list(self.request_fields)
        if self.usage_schema is not None:
            data["usage_schema"] = self.usage_schema
        if self.min_cacheable_tokens is not None:
            data["min_cacheable_tokens"] = self.min_cacheable_tokens
        if self.notes:
            data["notes"] = list(self.notes)
        if self.model_overrides:
            data["models"] = {
                key: spec.to_dict() for key, spec in self.model_overrides if spec.has_values()
            }
        if self.model_family_overrides:
            data["model_families"] = {
                key: spec.to_dict()
                for key, spec in self.model_family_overrides
                if spec.has_values()
            }
        if self.endpoint_overrides:
            data["endpoints"] = {
                key: spec.to_dict() for key, spec in self.endpoint_overrides if spec.has_values()
            }
        return data

    def has_values(self) -> bool:
        return bool(self.to_dict())


@dataclass(frozen=True)
class EffectiveCacheCapability:
    provider_key: str
    protocol: str
    model: str
    strategy: str = CACHE_STRATEGY_NONE
    enabled: bool = False
    source: str = "default"
    supports_prompt_cache_key: bool = False
    supports_prompt_cache_retention: bool = False
    supports_cache_control: bool = False
    supports_explicit_cached_content: bool = False
    reports_cache_read_tokens: bool = False
    reports_cache_write_tokens: bool = False
    emits_request_fields: bool = False
    usage_schema: str = CACHE_USAGE_SCHEMA_NONE
    min_cacheable_tokens: int | None = None
    emitted_fields: tuple[str, ...] = ()
    trusted_usage_fields: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.enabled and self.emitted_fields:
            return "enabled"
        if self.enabled:
            return "available"
        if self.strategy == CACHE_STRATEGY_NONE:
            return "disabled"
        return "unsupported"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "strategy": self.strategy,
            "source": self.source,
            "provider_key": self.provider_key,
            "protocol": self.protocol,
            "model": self.model,
            "emitted_fields": list(self.emitted_fields),
            "trusted_usage_fields": list(self.trusted_usage_fields),
            "usage_schema": self.usage_schema,
            "emits_request_fields": self.emits_request_fields,
        }
        if self.min_cacheable_tokens is not None:
            payload["min_cacheable_tokens"] = self.min_cacheable_tokens
        if self.notes:
            payload["notes"] = list(self.notes)
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload

    def summary(self) -> str:
        fields = ", ".join(self.emitted_fields) if self.emitted_fields else "none"
        usage = ", ".join(self.trusted_usage_fields) if self.trusted_usage_fields else "none"
        return (
            f"{self.status}; strategy={self.strategy}; source={self.source}; "
            f"emits={fields}; usage={usage}"
        )


@dataclass(frozen=True)
class _CacheCapabilityState:
    strategy: str
    enabled: bool
    supports_prompt_cache_key: bool
    supports_prompt_cache_retention: bool
    supports_cache_control: bool
    supports_explicit_cached_content: bool
    reports_cache_read_tokens: bool
    reports_cache_write_tokens: bool
    emits_request_fields: bool
    request_fields: tuple[str, ...]
    usage_schema: str
    min_cacheable_tokens: int | None
    source: str
    notes: tuple[str, ...]


def resolve_effective_cache_capability(
    *,
    provider_key: str,
    protocol: str,
    model: str,
    base_url: str | None = None,
    transport_capabilities: ProviderProtocolCapabilities | None,
    auth_cache_capability: CacheCapabilitySpec | Mapping[str, Any] | None = None,
    preset_cache_capability: CacheCapabilitySpec | Mapping[str, Any] | None = None,
    profile_cache_capability: CacheCapabilitySpec | Mapping[str, Any] | None = None,
    runtime_disabled_fields: tuple[str, ...] = (),
) -> EffectiveCacheCapability:
    """Resolve cache behavior from transport, auth adapter, preset, and profile metadata.

    The resolver deliberately separates "declared support" from "transport projection".
    User/profile overrides may opt into provider features, but emitted request fields are
    still clamped to fields the active client implementation knows how to project.
    """

    normalized_provider = _safe_text(provider_key).lower()
    normalized_protocol = _safe_text(protocol).lower()
    normalized_model = _safe_text(model)

    state = _state_from_transport_capabilities(transport_capabilities)
    auth_spec = _coerce_spec(auth_cache_capability, source="auth_adapter")
    preset_spec = _coerce_spec(preset_cache_capability, source="preset")
    profile_spec = _coerce_spec(profile_cache_capability, source="profile")
    # A provider endpoint can serve both API-key and subscription traffic.
    # Its generic preset is the default; the selected auth adapter defines the
    # actual account route. Explicit profile preferences remain the final layer.
    if preset_spec is not None:
        state = _apply_spec(state, preset_spec, origin="preset")
        state = _apply_scoped_specs(
            state,
            preset_spec,
            model=normalized_model,
            base_url=base_url,
            origin="preset",
        )
    if auth_spec is not None:
        state = _apply_spec(state, auth_spec, origin="auth_adapter")
        state = _apply_scoped_specs(
            state,
            auth_spec,
            model=normalized_model,
            base_url=base_url,
            origin="auth_adapter",
        )
    if profile_spec is not None:
        state = _apply_spec(state, profile_spec, origin="profile")
        state = _apply_scoped_specs(
            state,
            profile_spec,
            model=normalized_model,
            base_url=base_url,
            origin="profile",
        )

    warnings: list[str] = []
    allowed_fields, projection_warnings = _projected_fields(
        state=state,
        protocol=normalized_protocol,
    )
    warnings.extend(projection_warnings)
    disabled_fields = _request_field_tuple(
        runtime_disabled_fields,
        field_name="runtime disabled cache fields",
    )
    if disabled_fields:
        disabled = set(disabled_fields)
        allowed_fields = tuple(field for field in allowed_fields if field not in disabled)
        warnings.append(
            "Session-local cache capability downgrade disabled rejected field(s): "
            + ", ".join(disabled_fields)
        )

    strategy = normalize_cache_strategy(state.strategy, allow_inherit=False)
    enabled = bool(state.enabled) and strategy != CACHE_STRATEGY_NONE
    if (
        enabled
        and state.emits_request_fields
        and not _strategy_has_effective_surface(strategy, allowed_fields)
    ):
        enabled = False
        warnings.append(
            f"Cache strategy {strategy!r} has no safe emitted field for protocol "
            f"{normalized_protocol!r}."
        )
    if not enabled and strategy == CACHE_STRATEGY_NONE:
        allowed_fields = ()

    trusted_usage_fields = _trusted_usage_fields(
        read=state.reports_cache_read_tokens,
        write=state.reports_cache_write_tokens,
    )
    usage_schema = state.usage_schema
    if not trusted_usage_fields and usage_schema != CACHE_USAGE_SCHEMA_NONE:
        usage_schema = CACHE_USAGE_SCHEMA_NONE

    return EffectiveCacheCapability(
        provider_key=normalized_provider,
        protocol=normalized_protocol,
        model=normalized_model,
        strategy=strategy if enabled or strategy != CACHE_STRATEGY_INHERIT else CACHE_STRATEGY_NONE,
        enabled=enabled,
        source=state.source,
        supports_prompt_cache_key=PROMPT_CACHE_KEY_FIELD in allowed_fields,
        supports_prompt_cache_retention=PROMPT_CACHE_RETENTION_FIELD in allowed_fields,
        supports_cache_control=CACHE_CONTROL_FIELD in allowed_fields,
        supports_explicit_cached_content=CACHED_CONTENT_FIELD in allowed_fields,
        reports_cache_read_tokens=state.reports_cache_read_tokens,
        reports_cache_write_tokens=state.reports_cache_write_tokens,
        emits_request_fields=state.emits_request_fields,
        usage_schema=usage_schema,
        min_cacheable_tokens=state.min_cacheable_tokens,
        emitted_fields=allowed_fields if enabled else (),
        trusted_usage_fields=trusted_usage_fields,
        notes=state.notes,
        warnings=tuple(_dedupe(warnings)),
    )


def profile_cache_capability_from_preset(
    preset: ProfilePreset | None,
) -> CacheCapabilitySpec | None:
    if preset is None:
        return None
    return getattr(preset, "cache_capability", None)


def profile_cache_capability(profile: ProfileSpec | None) -> CacheCapabilitySpec | None:
    if profile is None:
        return None
    return getattr(profile, "cache_capability", None)


def cache_capability_diagnostic_rows(
    capability: EffectiveCacheCapability,
) -> tuple[tuple[str, str], ...]:
    emitted_fields = ", ".join(capability.emitted_fields) if capability.emitted_fields else "none"
    usage_fields = (
        ", ".join(capability.trusted_usage_fields) if capability.trusted_usage_fields else "none"
    )
    warnings = "; ".join(capability.warnings) if capability.warnings else "none"
    return (
        ("cache_status", capability.status),
        ("cache_strategy", capability.strategy),
        ("cache_capability_source", capability.source),
        ("cache_emitted_fields", emitted_fields),
        ("cache_usage_fields", usage_fields),
        ("cache_usage_schema", capability.usage_schema),
        ("cache_warnings", warnings),
    )


def normalize_cache_strategy(
    value: str | None,
    *,
    allow_inherit: bool = False,
    field_name: str = "cache strategy",
) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        normalized = CACHE_STRATEGY_INHERIT if allow_inherit else CACHE_STRATEGY_NONE
    allowed = PROFILE_CACHE_STRATEGIES if allow_inherit else KNOWN_CACHE_STRATEGIES
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {allowed_text}")
    if normalized == CACHE_STRATEGY_INHERIT and not allow_inherit:
        return CACHE_STRATEGY_NONE
    return normalized


def normalize_cache_usage_schema(
    value: str | None,
    *,
    field_name: str = "cache usage_schema",
) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        normalized = CACHE_USAGE_SCHEMA_NONE
    if normalized not in KNOWN_CACHE_USAGE_SCHEMAS:
        allowed_text = ", ".join(sorted(KNOWN_CACHE_USAGE_SCHEMAS))
        raise ValueError(f"{field_name} must be one of: {allowed_text}")
    return normalized


def _coerce_spec(
    value: CacheCapabilitySpec | Mapping[str, Any] | None,
    *,
    source: str,
) -> CacheCapabilitySpec | None:
    if value is None:
        return None
    if isinstance(value, CacheCapabilitySpec):
        if value.source == source:
            return value
        return CacheCapabilitySpec(
            strategy=value.strategy,
            enabled=value.enabled,
            supports_prompt_cache_key=value.supports_prompt_cache_key,
            supports_prompt_cache_retention=value.supports_prompt_cache_retention,
            supports_cache_control=value.supports_cache_control,
            supports_explicit_cached_content=value.supports_explicit_cached_content,
            reports_cache_read_tokens=value.reports_cache_read_tokens,
            reports_cache_write_tokens=value.reports_cache_write_tokens,
            emits_request_fields=value.emits_request_fields,
            request_fields=value.request_fields,
            usage_schema=value.usage_schema,
            min_cacheable_tokens=value.min_cacheable_tokens,
            notes=value.notes,
            source=source,
            model_overrides=value.model_overrides,
            model_family_overrides=value.model_family_overrides,
            endpoint_overrides=value.endpoint_overrides,
        )
    return CacheCapabilitySpec.from_mapping(value, source=source)


def _scoped_specs(value: Any, *, source: str) -> tuple[tuple[str, CacheCapabilitySpec], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError(f"{source} must be an object.")
    specs: list[tuple[str, CacheCapabilitySpec]] = []
    for key, raw_spec in value.items():
        label = str(key or "").strip()
        if not label:
            continue
        spec = CacheCapabilitySpec.from_mapping(raw_spec, source=f"{source}[{label!r}]")
        if spec is not None:
            specs.append((label, spec))
    return tuple(specs)


def _apply_scoped_specs(
    state: _CacheCapabilityState,
    spec: CacheCapabilitySpec,
    *,
    model: str,
    base_url: str | None,
    origin: str,
) -> _CacheCapabilityState:
    resolved = state
    for key, scoped_spec in spec.endpoint_overrides:
        if _endpoint_rule_matches(key, base_url):
            resolved = _apply_spec(resolved, scoped_spec, origin=origin)
    for key, scoped_spec in spec.model_family_overrides:
        if _model_family_rule_matches(key, model):
            resolved = _apply_spec(resolved, scoped_spec, origin=origin)
    for key, scoped_spec in spec.model_overrides:
        if _model_rule_matches(key, model):
            resolved = _apply_spec(resolved, scoped_spec, origin=origin)
    return resolved


def _endpoint_rule_matches(rule: str, base_url: str | None) -> bool:
    normalized_rule = _normalize_endpoint_rule(rule)
    if not normalized_rule:
        return False
    normalized_url = _normalize_endpoint_rule(base_url)
    if normalized_url == normalized_rule:
        return True
    return _endpoint_host(base_url) == normalized_rule


def _normalize_endpoint_rule(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/").lower()
    if not text:
        return ""
    host = _endpoint_host(text)
    if "://" not in text and "/" not in text and host:
        return host
    return text


def _endpoint_host(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return ""
    return (parsed.hostname or "").rstrip(".").lower()


def _model_rule_matches(rule: str, model: str) -> bool:
    normalized_rule = str(rule or "").strip().casefold()
    normalized_model = str(model or "").strip().casefold()
    if not normalized_rule or not normalized_model:
        return False
    return normalized_model == normalized_rule or fnmatch.fnmatchcase(
        normalized_model,
        normalized_rule,
    )


def _model_family_rule_matches(rule: str, model: str) -> bool:
    normalized_rule = str(rule or "").strip().casefold().rstrip("*")
    normalized_model = str(model or "").strip().casefold()
    if not normalized_rule or not normalized_model:
        return False
    return normalized_model.startswith(normalized_rule)


def _state_from_transport_capabilities(
    capabilities: ProviderProtocolCapabilities | None,
) -> _CacheCapabilityState:
    if capabilities is None:
        return _CacheCapabilityState(
            strategy=CACHE_STRATEGY_NONE,
            enabled=False,
            supports_prompt_cache_key=False,
            supports_prompt_cache_retention=False,
            supports_cache_control=False,
            supports_explicit_cached_content=False,
            reports_cache_read_tokens=False,
            reports_cache_write_tokens=False,
            emits_request_fields=False,
            request_fields=(),
            usage_schema=CACHE_USAGE_SCHEMA_NONE,
            min_cacheable_tokens=None,
            source="default",
            notes=("No bundled cache capability metadata for this provider/protocol.",),
        )

    strategy = normalize_cache_strategy(capabilities.cache_strategy, allow_inherit=False)
    enabled = strategy != CACHE_STRATEGY_NONE
    return _CacheCapabilityState(
        strategy=strategy,
        enabled=enabled,
        supports_prompt_cache_key=bool(capabilities.supports_prompt_cache_key),
        supports_prompt_cache_retention=bool(capabilities.supports_prompt_cache_retention),
        supports_cache_control=bool(capabilities.supports_cache_control),
        supports_explicit_cached_content=bool(capabilities.supports_explicit_cached_content),
        reports_cache_read_tokens=bool(capabilities.reports_cache_read_tokens),
        reports_cache_write_tokens=bool(capabilities.reports_cache_write_tokens),
        emits_request_fields=True,
        request_fields=(),
        usage_schema=_usage_schema_for_strategy(strategy),
        min_cacheable_tokens=_min_tokens_for_strategy(strategy),
        source="protocol",
        notes=(),
    )


def _apply_spec(
    state: _CacheCapabilityState,
    spec: CacheCapabilitySpec,
    *,
    origin: str,
) -> _CacheCapabilityState:
    if spec.enabled is False:
        return _CacheCapabilityState(
            strategy=CACHE_STRATEGY_NONE,
            enabled=False,
            supports_prompt_cache_key=False,
            supports_prompt_cache_retention=False,
            supports_cache_control=False,
            supports_explicit_cached_content=False,
            reports_cache_read_tokens=False,
            reports_cache_write_tokens=False,
            emits_request_fields=False,
            request_fields=(),
            usage_schema=CACHE_USAGE_SCHEMA_NONE,
            min_cacheable_tokens=spec.min_cacheable_tokens,
            source=spec.source,
            notes=_dedupe((*state.notes, *spec.notes)),
        )

    strategy = state.strategy
    if spec.strategy != CACHE_STRATEGY_INHERIT:
        strategy = spec.strategy
    enabled = state.enabled if spec.enabled is None else bool(spec.enabled)
    if strategy == CACHE_STRATEGY_NONE:
        enabled = False
    emits_request_fields = state.emits_request_fields
    if spec.emits_request_fields is not None:
        emits_request_fields = bool(spec.emits_request_fields)
    elif origin == "profile" and spec.enabled is True:
        # A profile override is an explicit advanced user opt-in, including scoped
        # models/model_families/endpoints overrides, which inherit the profile
        # origin. Preset metadata can stay diagnostic-only by setting
        # emits_request_fields=False.
        emits_request_fields = True

    return _CacheCapabilityState(
        strategy=strategy,
        enabled=enabled,
        supports_prompt_cache_key=_merge_bool(
            state.supports_prompt_cache_key,
            spec.supports_prompt_cache_key,
        ),
        supports_prompt_cache_retention=_merge_bool(
            state.supports_prompt_cache_retention,
            spec.supports_prompt_cache_retention,
        ),
        supports_cache_control=_merge_bool(
            state.supports_cache_control,
            spec.supports_cache_control,
        ),
        supports_explicit_cached_content=_merge_bool(
            state.supports_explicit_cached_content,
            spec.supports_explicit_cached_content,
        ),
        reports_cache_read_tokens=_merge_bool(
            state.reports_cache_read_tokens,
            spec.reports_cache_read_tokens,
        ),
        reports_cache_write_tokens=_merge_bool(
            state.reports_cache_write_tokens,
            spec.reports_cache_write_tokens,
        ),
        emits_request_fields=emits_request_fields,
        request_fields=_dedupe((*state.request_fields, *spec.request_fields)),
        usage_schema=spec.usage_schema
        or state.usage_schema
        or _usage_schema_for_strategy(strategy),
        min_cacheable_tokens=(
            spec.min_cacheable_tokens
            if spec.min_cacheable_tokens is not None
            else state.min_cacheable_tokens
        ),
        source=spec.source if spec.has_values() else state.source,
        notes=_dedupe((*state.notes, *spec.notes)),
    )


def _projected_fields(
    *,
    state: _CacheCapabilityState,
    protocol: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    physical_fields = _transport_projection_fields(protocol)
    if not state.emits_request_fields:
        return (), ()
    wanted: list[str] = []
    if state.supports_prompt_cache_key:
        wanted.append(PROMPT_CACHE_KEY_FIELD)
    if state.supports_prompt_cache_retention:
        wanted.append(PROMPT_CACHE_RETENTION_FIELD)
    if state.supports_cache_control:
        wanted.append(CACHE_CONTROL_FIELD)
    if state.supports_explicit_cached_content:
        wanted.append(CACHED_CONTENT_FIELD)
    wanted.extend(state.request_fields)

    strategy_fields = _strategy_projection_fields(state.strategy)
    wanted = [field for field in wanted if field in strategy_fields]
    fields = tuple(field for field in wanted if field in physical_fields)
    dropped = [field for field in wanted if field not in physical_fields]
    warnings = tuple(
        f"Dropped cache field {field!r}; protocol {protocol!r} cannot project it."
        for field in dropped
    )
    return fields, warnings


def _transport_projection_fields(protocol: str) -> tuple[str, ...]:
    normalized = str(protocol or "").strip().lower()
    if normalized in {OPENAI_COMPAT_PROTOCOL, OPENAI_RESPONSES_PROTOCOL}:
        fields = [PROMPT_CACHE_KEY_FIELD, PROMPT_CACHE_RETENTION_FIELD]
        if normalized == OPENAI_COMPAT_PROTOCOL:
            fields.extend(
                (
                    CACHE_CONTROL_FIELD,
                    OPENROUTER_SESSION_ID_FIELD,
                    OPENROUTER_SESSION_ID_HEADER_FIELD,
                    XAI_CONVERSATION_ID_HEADER_FIELD,
                )
            )
        return tuple(fields)
    if normalized == ANTHROPIC_MESSAGES_PROTOCOL:
        return (CACHE_CONTROL_FIELD,)
    if normalized == GEMINI_GENERATE_CONTENT_PROTOCOL:
        return (CACHED_CONTENT_FIELD,)
    if normalized == GEMINI_INTERACTIONS_PROTOCOL:
        return ()
    return ()


def _strategy_projection_fields(strategy: str) -> tuple[str, ...]:
    normalized = str(strategy or "").strip().lower()
    if normalized == CACHE_STRATEGY_IMPLICIT_PROVIDER:
        return (PROMPT_CACHE_KEY_FIELD,)
    if normalized in {
        CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    }:
        return (PROMPT_CACHE_KEY_FIELD, PROMPT_CACHE_RETENTION_FIELD)
    if normalized == CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY:
        return (PROMPT_CACHE_KEY_FIELD,)
    if normalized == CACHE_STRATEGY_OPENROUTER_STICKY_SESSION:
        return (OPENROUTER_SESSION_ID_FIELD, OPENROUTER_SESSION_ID_HEADER_FIELD)
    if normalized == CACHE_STRATEGY_XAI_CONVERSATION_HEADER:
        return (XAI_CONVERSATION_ID_HEADER_FIELD, PROMPT_CACHE_KEY_FIELD)
    if normalized == CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL:
        return (CACHE_CONTROL_FIELD,)
    if normalized == CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT:
        return (CACHED_CONTENT_FIELD,)
    if normalized == CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS:
        return (CACHE_CONTROL_FIELD,)
    return ()


def _strategy_has_effective_surface(strategy: str, projected_fields: tuple[str, ...]) -> bool:
    if strategy in {
        CACHE_STRATEGY_IMPLICIT_PROVIDER,
        CACHE_STRATEGY_GEMINI_IMPLICIT,
        CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
        CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
        CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    }:
        return True
    if strategy == CACHE_STRATEGY_NONE:
        return False
    return bool(projected_fields)


def _usage_schema_for_strategy(strategy: str) -> str:
    if strategy in {
        CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
        CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
    }:
        return CACHE_USAGE_SCHEMA_OPENAI
    if strategy == CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL:
        return CACHE_USAGE_SCHEMA_ANTHROPIC
    if strategy in {
        CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT,
        CACHE_STRATEGY_GEMINI_IMPLICIT,
    }:
        return CACHE_USAGE_SCHEMA_GEMINI
    if strategy in {
        CACHE_STRATEGY_IMPLICIT_PROVIDER,
        CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
        CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
        CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    }:
        return CACHE_USAGE_SCHEMA_PROVIDER
    return CACHE_USAGE_SCHEMA_NONE


def _min_tokens_for_strategy(strategy: str) -> int | None:
    if strategy in {
        CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
        CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
        CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    }:
        return 1024
    if strategy == CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT:
        return 4096
    return None


def _trusted_usage_fields(*, read: bool, write: bool) -> tuple[str, ...]:
    fields: list[str] = []
    if read:
        fields.append("cache_read_input_tokens")
    if write:
        fields.extend(
            [
                "cache_creation_input_tokens",
                "cache_creation_5m_input_tokens",
                "cache_creation_1h_input_tokens",
            ]
        )
    return tuple(fields)


def _merge_bool(current: bool, override: bool | None) -> bool:
    return current if override is None else bool(override)


def _optional_bool(value: Any, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be true or false.")


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer.") from None
    if number < 0:
        raise ValueError(f"{field_name} must be non-negative.")
    return number


def _string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a string or list of strings.")
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return tuple(items)


def _request_field_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    fields = _string_tuple(value, field_name=field_name)
    unknown = [field for field in fields if field not in KNOWN_CACHE_REQUEST_FIELDS]
    if unknown:
        allowed_text = ", ".join(sorted(KNOWN_CACHE_REQUEST_FIELDS))
        unknown_text = ", ".join(unknown)
        raise ValueError(
            f"{field_name} contains unknown field(s): {unknown_text}. "
            f"Allowed fields: {allowed_text}."
        )
    return _dedupe(fields)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)
