from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .llm.cache_capabilities import CacheCapabilitySpec
from .llm.metadata import canonicalize_extra_headers
from .llm.protocols import (
    OPENAI_COMPAT_PROTOCOL,
    SUPPORTED_LLM_PROTOCOLS,
    normalize_reasoning_trace_adapter,
    validate_reasoning_trace_adapter_for_protocol,
)
from .web_search_adapters import normalize_web_search_adapter

if TYPE_CHECKING:
    from .config import AppConfig

SUPPORTED_PROTOCOLS: frozenset[str] = SUPPORTED_LLM_PROTOCOLS
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_REASONING_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_UNSET = object()
SUBSCRIPTION_SELECTION_REQUIRED_KEY = "subscription_model_selection_required"


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    protocol: str = OPENAI_COMPAT_PROTOCOL
    base_url: str = ""
    api_key_env: str | None = None
    auth_provider: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    default_model: str = ""
    reasoning_effort: str | None = None
    reasoning_trace_adapter: str = "auto"
    web_search_adapter: str = "auto"
    web_search_model: str = ""
    notes: str = ""
    cache_capability: CacheCapabilitySpec | None = None

    def __post_init__(self) -> None:
        _validate_profile_name(self.name)
        _validate_protocol(self.protocol)
        _validate_web_search_adapter(self.web_search_adapter)
        normalized_reasoning_trace_adapter = validate_reasoning_trace_adapter_for_protocol(
            protocol=self.protocol,
            adapter=self.reasoning_trace_adapter,
        )
        object.__setattr__(
            self,
            "reasoning_trace_adapter",
            normalized_reasoning_trace_adapter,
        )
        if self.base_url:
            _validate_base_url(self.base_url)
        if (
            self.reasoning_effort is not None
            and _REASONING_EFFORT_RE.fullmatch(self.reasoning_effort) is None
        ):
            _raise_config_error("Profile reasoning_effort is not a valid provider effort id.")
        if self.cache_capability is not None and not isinstance(
            self.cache_capability, CacheCapabilitySpec
        ):
            _raise_config_error("Profile cache_capability must be a JSON object.")
        try:
            normalized_headers = canonicalize_extra_headers(self.extra_headers)
        except ValueError as exc:
            _raise_config_error(str(exc))
        object.__setattr__(self, "extra_headers", normalized_headers)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "protocol": self.protocol,
            "base_url": self.base_url,
            "extra_headers": dict(sorted(self.extra_headers.items())),
            "default_model": self.default_model,
            "web_search_adapter": normalize_web_search_adapter(self.web_search_adapter),
            "web_search_model": self.web_search_model,
            "notes": self.notes,
        }
        if self.api_key_env:
            data["api_key_env"] = self.api_key_env
        if self.auth_provider:
            data["auth_provider"] = self.auth_provider
        if self.reasoning_effort:
            data["reasoning_effort"] = self.reasoning_effort
        reasoning_trace_adapter = normalize_reasoning_trace_adapter(self.reasoning_trace_adapter)
        if reasoning_trace_adapter != "auto":
            data["reasoning_trace_adapter"] = reasoning_trace_adapter
        if self.cache_capability is not None and self.cache_capability.has_values():
            data["cache_capability"] = self.cache_capability.to_dict()
        return data

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ProfileSpec:
        if not isinstance(data, dict):
            _raise_config_error(f"Profile {name!r} must be a JSON object.")
        cache_capability = _coerce_cache_capability(
            data.get("cache_capability"),
            source=f"Profile {name!r}",
        )
        return cls(
            name=name,
            protocol=str(data.get("protocol") or OPENAI_COMPAT_PROTOCOL).strip(),
            base_url=str(data.get("base_url") or "").strip(),
            api_key_env=_optional_string(data.get("api_key_env")),
            auth_provider=_optional_string(data.get("auth_provider")),
            extra_headers=_coerce_headers(data.get("extra_headers")),
            default_model=str(data.get("default_model") or "").strip(),
            reasoning_effort=_optional_string(data.get("reasoning_effort")),
            reasoning_trace_adapter=normalize_reasoning_trace_adapter(
                _optional_string(data.get("reasoning_trace_adapter"))
            ),
            web_search_adapter=str(data.get("web_search_adapter") or "auto").strip(),
            web_search_model=str(data.get("web_search_model") or "").strip(),
            notes=str(data.get("notes") or ""),
            cache_capability=cache_capability,
        )


def list_profiles(cfg: AppConfig) -> list[ProfileSpec]:
    migrate_legacy_to_profiles(cfg)
    profiles = _profile_dict(cfg)
    return [ProfileSpec.from_dict(name, profiles[name]) for name in sorted(profiles)]


def get_profile(cfg: AppConfig, name: str) -> ProfileSpec | None:
    migrate_legacy_to_profiles(cfg)
    profile_name = _normalize_profile_name(name)
    profiles = _profile_dict(cfg)
    data = profiles.get(profile_name)
    if not isinstance(data, dict):
        return None
    return ProfileSpec.from_dict(profile_name, data)


def get_active_profile(cfg: AppConfig) -> ProfileSpec:
    migrate_legacy_to_profiles(cfg)
    active = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    if not active:
        _raise_config_error("No active profile configured.")
    profile = get_profile(cfg, active)
    if profile is None:
        _raise_config_error(f"Active profile not found: {active}")
    return profile


def active_subscription_selection_ready(cfg: AppConfig) -> bool:
    """Whether the active auth profile has an explicit /config model+effort choice."""

    profile = get_active_profile(cfg)
    if not profile.auth_provider:
        return True
    marker = (cfg.extra_fields or {}).get(SUBSCRIPTION_SELECTION_REQUIRED_KEY)
    if marker is True or str(marker or "").strip() == profile.auth_provider:
        return False
    return bool(profile.default_model and profile.reasoning_effort is not None)


def subscription_selection_supported(profile: ProfileSpec, models: Any) -> bool:
    """Check an explicit profile selection against a provider model catalog."""

    if not profile.default_model or profile.reasoning_effort is None:
        return False
    selected = next(
        (item for item in models if str(getattr(item, "id", "")) == profile.default_model),
        None,
    )
    if selected is None:
        return False
    if profile.reasoning_effort == "auto":
        return True
    supported = {
        str(getattr(item, "id", ""))
        for item in tuple(getattr(selected, "reasoning_efforts", ()) or ())
    }
    return not supported or profile.reasoning_effort in supported


def migrate_subscription_endpoints(cfg: AppConfig) -> bool:
    """Point subscription profiles saved by an earlier release at their adapter's endpoint.

    A subscription adapter owns its protocol and endpoint: the client factory and the
    startup check reject a profile that disagrees. When an adapter moves endpoint, a
    profile saved before the move names an endpoint the adapter lists in
    ``legacy_base_urls``. Such profiles take the adapter's current protocol and endpoint
    here, in memory on load; the next config save writes them. Any other differing
    endpoint is an override and stays rejected.
    """

    profiles = _profile_dict(cfg)
    if not profiles:
        return False
    from .provider_auth import provider_auth_connection

    updated: dict[str, dict[str, Any]] = {}
    for name, data in profiles.items():
        if not isinstance(data, dict):
            continue
        provider_id = str(data.get("auth_provider") or "").strip()
        stored = str(data.get("base_url") or "").strip()
        if not provider_id or not stored:
            continue
        connection = provider_auth_connection(provider_id)
        if connection is None or _same_base_url(stored, connection.base_url):
            continue
        if not any(_same_base_url(stored, legacy) for legacy in connection.legacy_base_urls):
            continue
        updated[name] = {
            **data,
            "protocol": connection.protocol,
            "base_url": connection.base_url,
        }
    if not updated:
        return False
    _set_profile_dict(cfg, {**profiles, **updated})
    return True


def apply_hosted_prompt_cache_defaults(cfg: AppConfig, profile: ProfileSpec) -> bool:
    """Upgrade untouched hosted cache settings once, without saving during reads.

    Old releases serialized Manual even when it was never selected. For the
    hosted service, migrate that otherwise-default configuration to Auto. Keep
    Off, customized settings and choices made after this migration intact.
    Other providers retain their existing defaults.
    """
    if cfg.hosted_prompt_cache_defaults_applied:
        return False
    from .profile_presets import find_preset_for_profile

    preset = find_preset_for_profile(profile)
    if preset is None or preset.provider_key != "alysis":
        return False
    cfg.hosted_prompt_cache_defaults_applied = True
    if (
        str(cfg.prompt_cache_mode or "manual").strip().lower() == "manual"
        and not str(cfg.prompt_cache_key or "").strip()
        and not str(cfg.prompt_cache_retention or "").strip()
        and not cfg.anthropic_prompt_cache_enabled
        and str(cfg.anthropic_prompt_cache_ttl or "5m").strip().lower() == "5m"
        and cfg.cache.prompt_cache_key_enabled
        and not cfg.cache.keepalive_enabled
        and (profile.cache_capability is None or not profile.cache_capability.has_values())
    ):
        cfg.prompt_cache_mode = "auto"
    return True


def sync_active_profile_to_config(cfg: AppConfig) -> bool:
    """Mirror the active profile connection defaults onto legacy top-level fields."""
    active = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    if not active:
        return False
    profile = get_profile(cfg, active)
    if profile is None:
        return False

    changed = apply_hosted_prompt_cache_defaults(cfg, profile)
    if profile.base_url and not _same_base_url(getattr(cfg, "base_url", ""), profile.base_url):
        cfg.base_url = profile.base_url
        changed = True
    desired_model = (
        profile.default_model if profile.auth_provider else profile.default_model or None
    )
    if desired_model is not None and str(getattr(cfg, "model", "") or "") != desired_model:
        cfg.model = desired_model
        changed = True
    if profile.reasoning_effort is not None or profile.auth_provider:
        effective_effort = (
            None if profile.reasoning_effort in {None, "auto"} else profile.reasoning_effort
        )
        effective_thinking = None if effective_effort is None else effective_effort != "none"
        if (
            getattr(cfg, "llm_reasoning_effort", None) == effective_effort
            and getattr(cfg, "llm_enable_thinking", None) == effective_thinking
        ):
            return changed
        cfg.llm_reasoning_effort = effective_effort
        cfg.llm_enable_thinking = effective_thinking
        changed = True
    return changed


def add_profile(
    cfg: AppConfig,
    profile: ProfileSpec,
    *,
    allow_auth_profile_update: bool = False,
) -> None:
    migrate_legacy_to_profiles(cfg)
    profiles = dict(_profile_dict(cfg))
    existing_data = profiles.get(profile.name)
    if isinstance(existing_data, dict):
        existing = ProfileSpec.from_dict(profile.name, existing_data)
        replacing_auth_profile = bool(existing.auth_provider)
        unchanged = existing.to_dict() == profile.to_dict()
        if replacing_auth_profile and not unchanged and not allow_auth_profile_update:
            _raise_config_error(
                f"Profile {profile.name!r} is managed by the AI subscription "
                f"{existing.auth_provider!r} and cannot be replaced by a generic profile edit."
            )
    profiles[profile.name] = profile.to_dict()
    _set_profile_dict(cfg, profiles)


def remove_profile(cfg: AppConfig, name: str) -> None:
    migrate_legacy_to_profiles(cfg)
    profile_name = _normalize_profile_name(name)
    profiles = dict(_profile_dict(cfg))
    if profile_name not in profiles:
        _raise_config_error(f"Profile not found: {profile_name}")
    profiles.pop(profile_name, None)
    _set_profile_dict(cfg, profiles)
    active = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    if active != profile_name:
        return
    if profiles:
        set_active_profile(cfg, sorted(profiles)[0])
    else:
        cfg.extra_fields.pop("active_profile", None)


def set_active_profile(cfg: AppConfig, name: str) -> None:
    profile_name = _normalize_profile_name(name)
    profile = get_profile(cfg, profile_name)
    if profile is None:
        _raise_config_error(f"Profile not found: {profile_name}")
    previous_profile = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    if previous_profile and previous_profile != profile.name:
        # Legacy-config hygiene: "router" is a deprecated role_models key from
        # the removed semantic router. It stored a provider-specific model id,
        # so never carry it into a different API profile; drop it on switch.
        for section in ("role_models", "forge_role_models"):
            raw_models = cfg.extra_fields.get(section)
            if not isinstance(raw_models, dict) or "router" not in raw_models:
                continue
            next_models = dict(raw_models)
            next_models.pop("router", None)
            if next_models:
                cfg.extra_fields[section] = next_models
            else:
                cfg.extra_fields.pop(section, None)
    cfg.extra_fields["active_profile"] = profile.name
    apply_hosted_prompt_cache_defaults(cfg, profile)
    if profile.base_url:
        cfg.base_url = profile.base_url
    if profile.default_model or profile.auth_provider:
        cfg.model = profile.default_model
    if profile.reasoning_effort is not None or profile.auth_provider:
        effective_effort = (
            None if profile.reasoning_effort in {None, "auto"} else profile.reasoning_effort
        )
        cfg.llm_reasoning_effort = effective_effort
        cfg.llm_enable_thinking = None if effective_effort is None else effective_effort != "none"


def update_active_profile_defaults(
    cfg: AppConfig,
    *,
    base_url: str | None = None,
    default_model: str | None = None,
    reasoning_effort: str | None | object = _UNSET,
    allow_subscription_selection: bool = False,
) -> bool:
    if not isinstance((cfg.extra_fields or {}).get("profiles"), dict):
        return False
    active = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    if not active:
        return False
    current = get_profile(cfg, active)
    if current is None:
        _raise_config_error(f"Active profile not found: {active}")
    if current.auth_provider:
        if base_url is not None and not _same_base_url(base_url, current.base_url):
            _raise_config_error(
                "Subscription endpoints are owned by their provider adapter; reconnect "
                "through setup instead of overriding base_url."
            )
        selection_changes_requested = default_model is not None or reasoning_effort is not _UNSET
        if selection_changes_requested and not allow_subscription_selection:
            _raise_config_error(
                "Subscription model and reasoning effort are managed in /config → Default Model."
            )

    next_base_url = current.base_url
    if base_url is not None:
        next_base_url = validate_base_url(base_url, key="base_url", allow_empty=True)

    next_default_model = current.default_model
    if default_model is not None:
        next_default_model = str(default_model or "").strip()
    next_reasoning_effort = current.reasoning_effort
    if reasoning_effort is not _UNSET:
        next_reasoning_effort = str(reasoning_effort or "auto").strip().lower() or "auto"

    updated = ProfileSpec(
        name=current.name,
        protocol=current.protocol,
        base_url=next_base_url,
        api_key_env=current.api_key_env,
        auth_provider=current.auth_provider,
        extra_headers=dict(current.extra_headers),
        default_model=next_default_model,
        reasoning_effort=next_reasoning_effort,
        reasoning_trace_adapter=current.reasoning_trace_adapter,
        web_search_adapter=current.web_search_adapter,
        web_search_model=current.web_search_model,
        notes=current.notes,
        cache_capability=current.cache_capability,
    )
    if updated.to_dict() == current.to_dict():
        return False
    add_profile(
        cfg,
        updated,
        allow_auth_profile_update=bool(current.auth_provider),
    )
    if base_url is not None:
        cfg.base_url = updated.base_url
    if default_model is not None:
        cfg.model = updated.default_model
    if reasoning_effort is not _UNSET:
        effective_effort = None if updated.reasoning_effort == "auto" else updated.reasoning_effort
        cfg.llm_reasoning_effort = effective_effort
        cfg.llm_enable_thinking = None if effective_effort is None else effective_effort != "none"
    return True


def apply_runtime_base_url_override(cfg: AppConfig, base_url: str) -> None:
    """Apply an explicit launch endpoint to the actual transient provider profile.

    Updating only the legacy top-level field leaves the transport pointing at the
    saved profile endpoint. Keep both representations aligned without persisting
    the user's launch override or inferring a different protocol/provider.
    """
    normalized = validate_base_url(base_url, key="base_url")
    get_active_profile(cfg)
    update_active_profile_defaults(cfg, base_url=normalized)
    cfg.base_url = normalized


def update_profile(
    cfg: AppConfig,
    name: str,
    *,
    allow_auth_profile_update: bool = False,
    allow_subscription_selection: bool = False,
    **fields: Any,
) -> None:
    profile_name = _normalize_profile_name(name)
    current = get_profile(cfg, profile_name)
    if current is None:
        _raise_config_error(f"Profile not found: {profile_name}")
    if current.auth_provider:
        connection_fields = {
            "protocol",
            "base_url",
            "api_key_env",
            "auth_provider",
            "extra_headers",
            "reasoning_trace_adapter",
        }
        if connection_fields.intersection(fields) and not allow_auth_profile_update:
            _raise_config_error(
                "Subscription connection fields are owned by their provider adapter."
            )
        if {"default_model", "reasoning_effort"}.intersection(fields) and not (
            allow_subscription_selection or allow_auth_profile_update
        ):
            _raise_config_error(
                "Subscription model and reasoning effort are managed in /config → Default Model."
            )
    values = {
        "name": current.name,
        "protocol": current.protocol,
        "base_url": current.base_url,
        "api_key_env": current.api_key_env,
        "auth_provider": current.auth_provider,
        "extra_headers": dict(current.extra_headers),
        "default_model": current.default_model,
        "reasoning_effort": current.reasoning_effort,
        "reasoning_trace_adapter": current.reasoning_trace_adapter,
        "web_search_adapter": current.web_search_adapter,
        "web_search_model": current.web_search_model,
        "notes": current.notes,
        "cache_capability": current.cache_capability,
    }
    values.update(fields)
    updated = ProfileSpec(**values)
    add_profile(
        cfg,
        updated,
        allow_auth_profile_update=bool(current.auth_provider),
    )
    if str((cfg.extra_fields or {}).get("active_profile") or "") == profile_name:
        set_active_profile(cfg, profile_name)


def rename_profile(cfg: AppConfig, old: str, new: str) -> None:
    old_name = _normalize_profile_name(old)
    new_name = _normalize_profile_name(new)
    if old_name == new_name:
        return
    current = get_profile(cfg, old_name)
    if current is None:
        _raise_config_error(f"Profile not found: {old_name}")
    if get_profile(cfg, new_name) is not None:
        _raise_config_error(f"Profile already exists: {new_name}")
    profiles = dict(_profile_dict(cfg))
    profiles.pop(old_name, None)
    profiles[new_name] = ProfileSpec(
        name=new_name,
        protocol=current.protocol,
        base_url=current.base_url,
        api_key_env=current.api_key_env,
        auth_provider=current.auth_provider,
        extra_headers=dict(current.extra_headers),
        default_model=current.default_model,
        reasoning_effort=current.reasoning_effort,
        reasoning_trace_adapter=current.reasoning_trace_adapter,
        web_search_adapter=current.web_search_adapter,
        web_search_model=current.web_search_model,
        notes=current.notes,
        cache_capability=current.cache_capability,
    ).to_dict()
    _set_profile_dict(cfg, profiles)
    if str((cfg.extra_fields or {}).get("active_profile") or "") == old_name:
        set_active_profile(cfg, new_name)


def migrate_legacy_to_profiles(cfg: AppConfig) -> bool:
    extra_fields = dict(getattr(cfg, "extra_fields", {}) or {})
    if isinstance(extra_fields.get("profiles"), dict):
        cfg.extra_fields = extra_fields
        return False

    base_url = str(getattr(cfg, "base_url", "") or "").strip()
    if not base_url or _same_base_url(base_url, DEFAULT_OPENAI_BASE_URL):
        base_url = DEFAULT_OPENAI_BASE_URL
        api_key_env = None if _legacy_api_key_present() else "OPENAI_API_KEY"
    else:
        api_key_env = None
    profile = ProfileSpec(
        name="default",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url=base_url,
        api_key_env=api_key_env,
        default_model=str(getattr(cfg, "model", "") or "").strip(),
        reasoning_effort=(
            str(getattr(cfg, "llm_reasoning_effort", "") or "").strip().lower() or None
        ),
        notes="Migrated from legacy base_url/model config.",
    )
    extra_fields["profiles"] = {"default": profile.to_dict()}
    extra_fields["active_profile"] = "default"
    cfg.extra_fields = extra_fields
    return True


def _profile_dict(cfg: AppConfig) -> dict[str, dict[str, Any]]:
    raw = (cfg.extra_fields or {}).get("profiles")
    if not isinstance(raw, dict):
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    for name, data in raw.items():
        profile_name = _normalize_profile_name(str(name))
        if isinstance(data, dict):
            profiles[profile_name] = dict(data)
    return profiles


def _set_profile_dict(cfg: AppConfig, profiles: dict[str, dict[str, Any]]) -> None:
    extra_fields = dict(cfg.extra_fields or {})
    extra_fields["profiles"] = dict(sorted(profiles.items()))
    cfg.extra_fields = extra_fields


def _normalize_profile_name(name: str) -> str:
    normalized = str(name or "").strip().lower()
    _validate_profile_name(normalized)
    return normalized


def _validate_profile_name(name: str) -> None:
    normalized = str(name or "").strip()
    if not normalized:
        _raise_config_error("Profile name must be non-empty.")
    if normalized != normalized.lower() or not _PROFILE_NAME_RE.fullmatch(normalized):
        _raise_config_error(
            "Profile name must use lowercase letters, digits, hyphens, or underscores."
        )


def _validate_protocol(protocol: str) -> None:
    if str(protocol or "").strip() not in SUPPORTED_PROTOCOLS:
        allowed = ", ".join(sorted(SUPPORTED_PROTOCOLS))
        _raise_config_error(f"Profile protocol must be one of: {allowed}")


def _validate_web_search_adapter(adapter: str) -> None:
    try:
        normalize_web_search_adapter(adapter)
    except ValueError as exc:
        _raise_config_error(str(exc))


def _validate_base_url(base_url: str) -> None:
    validate_base_url(base_url, key="Profile base_url")


def validate_base_url(
    base_url: str,
    *,
    key: str = "base_url",
    allow_empty: bool = False,
) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        if allow_empty:
            return ""
        _raise_config_error(f"{key} must be non-empty.")
    if any(ch.isspace() for ch in normalized):
        _raise_config_error(f"{key} must not contain whitespace.")
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        _raise_config_error(f"{key} must be a valid http:// or https:// URL.")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        _raise_config_error(f"{key} must be a valid http:// or https:// URL.")
    if parsed.query or parsed.fragment:
        _raise_config_error(f"{key} must not include query strings or fragments.")
    return normalized


def resolve_effective_base_url(*, cfg: AppConfig, profile: ProfileSpec | None = None) -> str:
    resolved_profile = profile or get_active_profile(cfg)
    profile_base_url = validate_base_url(
        resolved_profile.base_url,
        key=f"Profile {resolved_profile.name} base_url",
        allow_empty=True,
    ).rstrip("/")
    if profile_base_url:
        return profile_base_url
    cfg_base_url = validate_base_url(
        str(getattr(cfg, "base_url", "") or ""),
        key="base_url",
        allow_empty=True,
    ).rstrip("/")
    default_base_url = DEFAULT_OPENAI_BASE_URL.rstrip("/")
    return cfg_base_url or default_base_url


def connection_fingerprint(cfg: AppConfig) -> tuple[str, ...]:
    """Return connection fields that require rebuilding an LLM client.

    API-key profiles can safely apply model/reasoning changes to their existing
    client in place. A trace-adapter change always requires rebuilding the
    concrete client capability, while a subscription model and reasoning effort
    are a provider-managed pair that also require a rebuild.
    """

    profile = get_active_profile(cfg)
    execution = getattr(cfg, "execution", None)
    subscription_model = profile.default_model if profile.auth_provider else ""
    subscription_reasoning = profile.reasoning_effort if profile.auth_provider else ""
    return (
        str(getattr(execution, "backend", "native") or "native").strip(),
        str(getattr(execution, "runtime", None) or "").strip(),
        profile.name,
        str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip(),
        resolve_effective_base_url(cfg=cfg, profile=profile),
        str(profile.auth_provider or "").strip(),
        str(subscription_model or "").strip(),
        str(subscription_reasoning or "").strip(),
        str(profile.reasoning_trace_adapter or "auto").strip(),
    )


def _coerce_cache_capability(raw: Any, *, source: str) -> CacheCapabilitySpec | None:
    try:
        return CacheCapabilitySpec.from_mapping(raw, source=source)
    except ValueError as exc:
        _raise_config_error(str(exc))


def _coerce_headers(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _raise_config_error("Profile extra_headers must be a JSON object.")
    try:
        return canonicalize_extra_headers(raw)
    except ValueError as exc:
        _raise_config_error(str(exc))


def _optional_string(raw: Any) -> str | None:
    value = str(raw or "").strip()
    return value or None


def _same_base_url(left: str, right: str) -> bool:
    return str(left or "").strip().rstrip("/") == str(right or "").strip().rstrip("/")


def _legacy_api_key_present() -> bool:
    try:
        from .config import load_persisted_api_key

        return bool(load_persisted_api_key())
    except Exception:
        return False


def _raise_config_error(message: str) -> None:
    from .config import ConfigError

    raise ConfigError(message)
