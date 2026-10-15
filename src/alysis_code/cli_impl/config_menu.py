from __future__ import annotations

import copy
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

import typer
from click import Abort
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from ..branding import env_get
from ..config import (
    _ROLE_TEMPERATURE_FIELDS,
    DEFAULT_SUBAGENT_TIMEOUT_S,
    AgentRuntimeSettings,
    AppConfig,
    ConfigError,
    _coerce_optional_positive_float,
    _normalize_web_search_mode,
    clear_persisted_api_key,
    clear_persisted_profile_key,
    config_path,
    load_config,
    resolve_api_key,
    resolve_llm_reasoning_effort,
    save_config,
    save_persisted_api_key,
    save_persisted_profile_key,
    set_config_value,
)
from ..llm.cache_capabilities import (
    EffectiveCacheCapability,
    resolve_effective_cache_capability,
)
from ..llm.cache_policy import ResolvedPromptCachePolicy, resolve_prompt_cache_policy
from ..llm.metadata import credential_scope_fingerprint
from ..llm.protocols import (
    OPENAI_COMPAT_PROTOCOL,
    get_provider_protocol_capabilities,
    validate_reasoning_trace_adapter_for_protocol,
)
from ..model_registry import resolve_model_provider_key
from ..profile_presets import (
    PROFILE_PRESETS,
    ProfilePreset,
    advanced_provider_selection_presets,
    find_preset_for_base_url,
    find_preset_for_profile,
    make_profile_from_preset,
    model_display_name,
    model_options_for_preset,
    preset_protocol_summary,
    preset_selection_label,
    provider_selection_presets,
)
from ..profiles import (
    SUBSCRIPTION_SELECTION_REQUIRED_KEY,
    ProfileSpec,
    apply_hosted_prompt_cache_defaults,
    get_active_profile,
    list_profiles,
    set_active_profile,
    subscription_selection_supported,
    validate_base_url,
)
from ..provider_diagnostics import provider_diagnostic_warning_lines
from ..provider_model_catalog import (
    ProviderModelCatalogError,
    ProviderModelOption,
    discover_provider_models,
)
from ..provider_url import display_endpoint
from ..reasoning_contracts import (
    UNKNOWN_CONTRACT,
    reasoning_contract_for,
    reasoning_labels_allowed_by_contract,
)
from ..sandbox_settings import (
    apply_sandbox_mode_to_config,
    normalize_sandbox_mode,
    sandbox_mode_from_config,
)
from ..surface.console import make_console
from ..surface.styles import STYLE_CONTENT, STYLE_DIM, STYLE_EMPHASIS
from ..web_search_adapters import WEB_SEARCH_ADAPTER_CHOICES, normalize_web_search_adapter
from ..web_search_policy import normalize_web_search_policy

_LOGGER = logging.getLogger(__name__)

ROLE_ORDER: tuple[str, ...] = (
    "coding",
    "planner",
    "review",
    "compactor",
    "conflict_review",
    "conflict_resolve",
    "approval_review",
)
ROLE_MODEL_ORDER: tuple[str, ...] = ROLE_ORDER
# Forge runs never ask for approvals, so the approval reviewer has no Forge override.
FORGE_ROLE_ORDER: tuple[str, ...] = tuple(r for r in ROLE_MODEL_ORDER if r != "approval_review")
SECTION_VALUES: tuple[tuple[str, str], ...] = (
    ("profile", "Provider Profile"),
    ("api_key", "API Key"),
    ("default", "Default Model"),
    ("web_search", "Web Search"),
    ("cache", "Context & Cache"),
    ("request_timeout", "Request timeout (advanced)"),
    ("subagents", "Subagent model overrides"),
    ("forge", "Forge model overrides"),
    ("sandbox", "Sandbox"),
    ("personas", "Personas"),
    ("execution", "Model Access"),
)
SANDBOX_MODES: tuple[str, ...] = ("strict", "warn", "off")
THINKING_LABELS: tuple[str, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "auto",
)
STEP_BUDGET_POLICIES: tuple[str, ...] = ("autonomous", "limited", "adaptive", "fixed")
PROMPT_CACHE_MODES: tuple[str, ...] = ("auto", "manual", "off")
ANTHROPIC_PROMPT_CACHE_TTLS: tuple[str, ...] = ("5m", "1h")
_CACHE_AWARE_COMPACTION_DEFAULT = True
_CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT = 0.72
_CACHE_AWARE_MIN_TRIGGER_RATIO_MIN = 0.05
_CACHE_AWARE_MIN_TRIGGER_RATIO_MAX = 0.98
_THINKING_LABEL_EXTRA_FIELD = "llm_thinking_label"
_MISSING_REQUIRED = "[yellow]missing · required[/yellow]"
_CLEAR_KEY_PENDING = "[yellow]will be cleared on save[/yellow]"
_MISSING_RUNTIME = "[yellow]AI subscription · connection required[/yellow]"
_WARNING_SUMMARIES = {_MISSING_REQUIRED, _CLEAR_KEY_PENDING, _MISSING_RUNTIME}
_ROLE_DESCRIPTIONS: dict[str, str] = {
    "coding": "generating and editing code",
    "planner": "high-level task planning",
    "review": "reviewing diffs and changes",
    "compactor": "summarizing long conversations",
    "conflict_review": "reviewing merge conflicts",
    "conflict_resolve": "resolving merge conflicts",
    "approval_review": "approving actions in Approve for me (empty = the chat's model)",
}
_FIELD_LABELS: dict[str, str] = {
    "max_steps": "Max steps per response",
    "task_max_steps": "Max steps per task",
    "subagent_max_steps": "Max steps per subagent run",
    "subagent_timeout_s": "Subagent timeout (seconds or unlimited)",
}
_API_KEY_ENV_PROMPT = "API key env var name (NOT the key itself, e.g. 'ANTHROPIC_API_KEY')"
_API_KEY_ENV_FIELD_NAME = "API key env var name"
_SECRET_FORCE_TOKEN = "force"
_CUSTOM_MODEL_VALUE = "__custom_model__"
_ADVANCED_PROVIDER_PRESETS_VALUE = "__advanced_provider_presets__"
_EXECUTION_BACKENDS: tuple[str, ...] = ("native", "delegated")


def _normalize_execution_backend(value: Any) -> str:
    normalized = str(value or "native").strip().lower()
    if normalized not in _EXECUTION_BACKENDS:
        raise ValueError("Model access must be one of: API key, AI subscription.")
    return normalized


def _runtime_setup_options() -> tuple[Any, ...]:
    from ..provider_auth import provider_auth_setup_options

    return tuple(provider_auth_setup_options())


def _runtime_setup_rows() -> list[tuple[str, str, str]]:
    """Return registry-backed delegated runtime choices for both config UIs."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for option in _runtime_setup_options():
        runtime_id = str(getattr(option, "id", "") or "").strip()
        if not runtime_id or runtime_id in seen:
            continue
        seen.add(runtime_id)
        label = str(getattr(option, "label", "") or runtime_id).strip() or runtime_id
        description = str(getattr(option, "description", "") or "").strip()
        rows.append((runtime_id, label, description))
    return rows


def _is_direct_subscription_id(runtime_id: str) -> bool:
    from ..provider_auth import provider_auth_ids

    return str(runtime_id or "").strip() in provider_auth_ids()


@dataclass(frozen=True)
class ConfigMenuResult:
    saved: bool
    changes: dict[str, Any]
    api_key_changed: bool
    error: str | None = None


@dataclass
class ConfigMenuState:
    fields: dict[str, str]
    thinking_label: str
    role_models: dict[str, str]
    forge_role_models: dict[str, str]
    role_temperatures: dict[str, str]
    profiles: dict[str, dict[str, Any]]
    active_profile: str
    execution_backend: str = "native"
    execution_runtime: str = ""
    agent_runtimes: dict[str, Any] = field(default_factory=dict)
    api_key_source: str = "missing"
    masked_api_key: str = "(not set)"
    new_api_key: str = ""
    new_api_key_profile: str | None = None
    clear_stored_key_confirmed: bool = False
    clear_stored_key_profile: str | None = None
    config_warning: str | None = None
    subscription_selection_required: bool = False
    default_workspace_path: str = ""
    thinking_label_explicitly_set: bool = field(default=False, repr=False)
    hosted_prompt_cache_defaults_applied: bool = False
    prompt_cache_mode_explicitly_set: bool = field(default=False, repr=False)
    cache_prompt_key_enabled: bool = True
    cache_keepalive_enabled: bool = False
    _subscription_models_cache: tuple[Any, ...] = field(default=(), repr=False)
    _subscription_models_loaded: bool = field(default=False, repr=False)
    _provider_models_cache_identity: tuple[str, ...] | None = field(default=None, repr=False)
    _provider_models_cache: tuple[ProviderModelOption, ...] = field(default=(), repr=False)
    _provider_model_catalog_warning: str = field(default="", repr=False)
    _account_changed: bool = field(default=False, repr=False)
    _original: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_cfg(cls, cfg: AppConfig) -> ConfigMenuState:
        apply_hosted_prompt_cache_defaults(cfg, get_active_profile(cfg))
        warnings: list[str] = []
        try:
            resolved_key = resolve_api_key(cfg)
            api_key_source = resolved_key.source
            masked_api_key = mask_api_key(resolved_key.key)
        except ConfigError as exc:
            warnings.append(str(exc))
            api_key_source = "error"
            masked_api_key = "(not set)"
        try:
            thinking_label = thinking_label_from_cfg(cfg)
        except ConfigError as exc:
            warnings.append(str(exc))
            thinking_label = "auto"
        profiles = {profile.name: profile.to_dict() for profile in list_profiles(cfg)}
        active_profile = str(cfg.extra_fields.get("active_profile") or "")
        execution = getattr(cfg, "execution", None)
        execution_backend = _normalize_execution_backend(getattr(execution, "backend", "native"))
        execution_runtime = str(getattr(execution, "runtime", None) or "").strip()
        try:
            active_spec = ProfileSpec.from_dict(active_profile, profiles[active_profile])
        except (KeyError, ConfigError):
            active_spec = None
        if active_spec is not None and active_spec.auth_provider:
            execution_backend = "delegated"
            execution_runtime = active_spec.auth_provider
            configured_model = active_spec.default_model
            thinking_label = (
                "auto"
                if active_spec.reasoning_effort is None
                else (
                    "off"
                    if active_spec.reasoning_effort == "none"
                    else active_spec.reasoning_effort
                )
            )
            selection_marker = cfg.extra_fields.get(SUBSCRIPTION_SELECTION_REQUIRED_KEY)
            subscription_selection_required = bool(
                selection_marker is True
                or str(selection_marker or "").strip() == active_spec.auth_provider
                or not active_spec.default_model
                or active_spec.reasoning_effort is None
            )
        else:
            configured_model = str(getattr(cfg, "model", "") or "")
            subscription_selection_required = False
        agent_runtimes = copy.deepcopy(dict(getattr(cfg, "agent_runtimes", {}) or {}))
        default_workspace_path = str(cfg.extra_fields.get("default_workspace_path") or "")
        role_models = _normalized_role_model_values(
            cfg.extra_fields.get("role_models") if isinstance(cfg.extra_fields, dict) else None
        )
        raw_compaction = _raw_compaction_extra_fields(cfg)
        forge_role_models = _normalized_role_model_values(
            cfg.extra_fields.get("forge_role_models")
            if isinstance(cfg.extra_fields, dict)
            else None
        )
        role_temperatures: dict[str, str] = {}
        default_temperature = _finite_float(getattr(cfg, "temperature", 0.2), fallback=0.2)
        for role in ROLE_ORDER:
            field_name = _ROLE_TEMPERATURE_FIELDS.get(role)
            if field_name is None:
                continue
            current = _finite_float(getattr(cfg, field_name, default_temperature), fallback=None)
            if current is None or current == default_temperature:
                role_temperatures[role] = ""
            else:
                role_temperatures[role] = _format_number(current)

        state = cls(
            fields={
                "model": configured_model,
                "base_url": str(getattr(cfg, "base_url", "") or ""),
                "llm_timeout_s": _format_number(getattr(cfg, "llm_timeout_s", 60.0)),
                "step_budget_policy": _normalize_step_budget_policy(
                    getattr(cfg, "step_budget_policy", "autonomous") or "autonomous"
                ),
                "max_steps": _format_integer(getattr(cfg, "max_steps", 25)),
                "task_max_steps": _format_integer(getattr(cfg, "task_max_steps", 100)),
                "subagent_max_steps": _format_integer(getattr(cfg, "subagent_max_steps", 16)),
                "subagent_timeout_s": _format_optional_limit(
                    getattr(cfg, "subagent_timeout_s", DEFAULT_SUBAGENT_TIMEOUT_S)
                ),
                "stream": "true" if bool(getattr(cfg, "stream", True)) else "false",
                "prompt_cache_mode": _normalize_prompt_cache_mode(
                    getattr(cfg, "prompt_cache_mode", "manual") or "manual"
                ),
                "prompt_cache_key": str(getattr(cfg, "prompt_cache_key", "") or ""),
                "prompt_cache_retention": str(getattr(cfg, "prompt_cache_retention", "") or ""),
                "anthropic_prompt_cache_enabled": (
                    "true"
                    if bool(getattr(cfg, "anthropic_prompt_cache_enabled", False))
                    else "false"
                ),
                "anthropic_prompt_cache_ttl": _normalize_anthropic_prompt_cache_ttl(
                    getattr(cfg, "anthropic_prompt_cache_ttl", "5m") or "5m"
                ),
                "cache_aware_compaction": _cache_aware_compaction_text(
                    raw_compaction.get("cache_aware_compaction")
                ),
                "cache_aware_min_trigger_ratio": _cache_aware_min_trigger_ratio_text(
                    raw_compaction.get("cache_aware_min_trigger_ratio")
                ),
                "web_search_mode": str(getattr(cfg, "web_search_mode", "auto") or "auto"),
                "web_search_policy": str(getattr(cfg, "web_search_policy", "auto") or "auto"),
                "web_search_adapter": str(getattr(cfg, "web_search_adapter", "auto") or "auto"),
                "web_search_base_url": str(getattr(cfg, "web_search_base_url", "") or ""),
                "web_search_model": str(getattr(cfg, "web_search_model", "") or ""),
                "sandbox_mode": sandbox_mode_from_config(cfg),
                "default_persona": str(getattr(cfg, "default_persona", "code") or "code"),
            },
            thinking_label=thinking_label,
            role_models=role_models,
            forge_role_models=forge_role_models,
            role_temperatures=role_temperatures,
            profiles=profiles,
            active_profile=active_profile,
            execution_backend=execution_backend,
            execution_runtime=execution_runtime,
            agent_runtimes=agent_runtimes,
            api_key_source=api_key_source,
            masked_api_key=masked_api_key,
            config_warning="; ".join(warnings) or None,
            subscription_selection_required=subscription_selection_required,
            default_workspace_path=default_workspace_path,
        )
        state._normalize_provider_thinking_label()
        state.hosted_prompt_cache_defaults_applied = cfg.hosted_prompt_cache_defaults_applied
        state.cache_prompt_key_enabled = cfg.cache.prompt_cache_key_enabled
        state.cache_keepalive_enabled = cfg.cache.keepalive_enabled
        state._original = state.snapshot()
        return state

    @property
    def dirty(self) -> bool:
        return self.snapshot() != self._original

    def snapshot(self) -> dict[str, Any]:
        return {
            "fields": {key: str(value) for key, value in sorted(self.fields.items())},
            "thinking_label": self.thinking_label,
            "role_models": {role: str(self.role_models.get(role, "")) for role in ROLE_MODEL_ORDER},
            "forge_role_models": {
                role: str(self.forge_role_models.get(role, "")) for role in FORGE_ROLE_ORDER
            },
            "role_temperatures": {
                role: str(self.role_temperatures.get(role, ""))
                for role in ROLE_ORDER
                if role in _ROLE_TEMPERATURE_FIELDS
            },
            "profiles": {name: dict(data) for name, data in sorted(self.profiles.items())},
            "active_profile": self.active_profile,
            "execution_backend": self.execution_backend,
            "execution_runtime": self.execution_runtime,
            "agent_runtimes": copy.deepcopy(self.agent_runtimes),
            "default_workspace_path": self.default_workspace_path,
            "new_api_key": self.new_api_key,
            "new_api_key_profile": self.new_api_key_profile,
            "clear_stored_key_confirmed": self.clear_stored_key_confirmed,
            "clear_stored_key_profile": self.clear_stored_key_profile,
            "thinking_label_explicitly_set": self.thinking_label_explicitly_set,
            "hosted_prompt_cache_defaults_applied": self.hosted_prompt_cache_defaults_applied,
            "prompt_cache_mode_explicitly_set": self.prompt_cache_mode_explicitly_set,
            "subscription_selection_required": self.subscription_selection_required,
        }

    def reset(self) -> None:
        original_fields = self._original.get("fields")
        if isinstance(original_fields, dict):
            self.fields = {str(key): str(value) for key, value in original_fields.items()}
        self.thinking_label = str(self._original.get("thinking_label") or "auto")
        self.subscription_selection_required = bool(
            self._original.get("subscription_selection_required", False)
        )
        self.role_models = _role_text_map_from_snapshot(self._original.get("role_models"))
        self.forge_role_models = _role_text_map_from_snapshot(
            self._original.get("forge_role_models")
        )
        self.role_temperatures = _role_text_map_from_snapshot(
            self._original.get("role_temperatures")
        )
        profiles = self._original.get("profiles")
        self.profiles = {
            str(name): dict(data)
            for name, data in (profiles.items() if isinstance(profiles, dict) else ())
            if isinstance(data, dict)
        }
        self.active_profile = str(self._original.get("active_profile") or "")
        self.execution_backend = _normalize_execution_backend(
            self._original.get("execution_backend")
        )
        self.execution_runtime = str(self._original.get("execution_runtime") or "")
        raw_agent_runtimes = self._original.get("agent_runtimes")
        self.agent_runtimes = copy.deepcopy(
            dict(raw_agent_runtimes) if isinstance(raw_agent_runtimes, dict) else {}
        )
        self.default_workspace_path = str(self._original.get("default_workspace_path") or "")
        self.new_api_key = str(self._original.get("new_api_key") or "")
        raw_new_key_profile = self._original.get("new_api_key_profile")
        self.new_api_key_profile = str(raw_new_key_profile) if raw_new_key_profile else None
        self.clear_stored_key_confirmed = bool(
            self._original.get("clear_stored_key_confirmed", False)
        )
        raw_clear_profile = self._original.get("clear_stored_key_profile")
        self.clear_stored_key_profile = str(raw_clear_profile) if raw_clear_profile else None
        self.thinking_label_explicitly_set = bool(
            self._original.get("thinking_label_explicitly_set", False)
        )
        self.hosted_prompt_cache_defaults_applied = bool(
            self._original.get("hosted_prompt_cache_defaults_applied", False)
        )
        self.prompt_cache_mode_explicitly_set = bool(
            self._original.get("prompt_cache_mode_explicitly_set", False)
        )
        self.refresh_api_key_status()

    def set_field(self, name: str, value: str) -> None:
        key = str(name).strip()
        if key == "new_api_key":
            self.new_api_key = str(value)
            self.new_api_key_profile = self.active_profile or None
            self.clear_stored_key_confirmed = False
            self.clear_stored_key_profile = None
            return
        if key not in {
            "model",
            "base_url",
            "llm_timeout_s",
            "step_budget_policy",
            "max_steps",
            "task_max_steps",
            "subagent_max_steps",
            "subagent_timeout_s",
            "prompt_cache_mode",
            "prompt_cache_key",
            "prompt_cache_retention",
            "anthropic_prompt_cache_enabled",
            "anthropic_prompt_cache_ttl",
            "cache_aware_compaction",
            "cache_aware_min_trigger_ratio",
            "web_search_mode",
            "web_search_policy",
            "web_search_adapter",
            "web_search_base_url",
            "web_search_model",
        }:
            raise KeyError(f"Unknown config menu field: {name}")
        self.fields[key] = str(value)
        if key in {
            "prompt_cache_mode",
            "prompt_cache_key",
            "prompt_cache_retention",
            "anthropic_prompt_cache_enabled",
            "anthropic_prompt_cache_ttl",
        }:
            self.hosted_prompt_cache_defaults_applied = True
        if key == "prompt_cache_mode":
            self.prompt_cache_mode_explicitly_set = True
        if key == "model":
            profile = _active_subscription_profile(self)
            if profile is not None and str(value).strip() != profile.default_model:
                self.subscription_selection_required = True
            self._normalize_provider_thinking_label()
        if key == "base_url":
            self.refresh_api_key_status()

    def set_thinking_label(self, label: str) -> None:
        normalized = _normalize_thinking_label(label)
        self.thinking_label = normalized
        self.thinking_label_explicitly_set = True
        if _active_subscription_profile(self) is not None:
            self.subscription_selection_required = True

    def _normalize_provider_thinking_label(self) -> None:
        """Drop a stale effort inherited from another provider/model surface."""

        preset = _active_preset(self)
        if preset is None or preset.key not in {"alysis", "nvidia", "zai-coding-plan"}:
            return
        allowed = _thinking_labels_for_state(
            self,
            model=str(self.fields.get("model") or ""),
        )
        if self.thinking_label in allowed:
            return
        self.thinking_label = "auto"
        self.thinking_label_explicitly_set = True

    def set_step_budget_policy(self, value: str) -> None:
        self.fields["step_budget_policy"] = _normalize_step_budget_policy(value)

    def set_max_steps(self, value: str) -> None:
        self.fields["max_steps"] = str(value)

    def set_task_max_steps(self, value: str) -> None:
        self.fields["task_max_steps"] = str(value)

    def set_subagent_max_steps(self, value: str) -> None:
        self.fields["subagent_max_steps"] = str(value)

    def set_subagent_timeout_s(self, value: str) -> None:
        self.fields["subagent_timeout_s"] = str(value)

    def set_role_model(self, role: str, model: str) -> None:
        role_key = _normalize_role(role)
        self.role_models[role_key] = str(model)

    def set_forge_role_model(self, role: str, model: str) -> None:
        role_key = _normalize_role(role)
        self.forge_role_models[role_key] = str(model)

    def set_role_temperature(self, role: str, value: str) -> None:
        role_key = _normalize_role(role)
        if role_key not in _ROLE_TEMPERATURE_FIELDS:
            raise KeyError(f"Role has no temperature setting: {role}")
        self.role_temperatures[role_key] = str(value)

    def mark_clear_stored_key_confirmed(self) -> None:
        self.clear_stored_key_confirmed = True
        self.clear_stored_key_profile = self.active_profile or None
        self.new_api_key = ""
        self.new_api_key_profile = None

    def set_default_workspace_path(self, path: str) -> None:
        self.default_workspace_path = str(path or "").strip()

    def set_execution_backend(self, backend: str, *, runtime: str | None = None) -> None:
        normalized = _normalize_execution_backend(backend)
        self.execution_backend = normalized
        if normalized == "native":
            self.execution_runtime = ""
            if self.active_profile in self.profiles:
                active = ProfileSpec.from_dict(
                    self.active_profile,
                    self.profiles[self.active_profile],
                )
                if active.auth_provider:
                    replacement = next(
                        (
                            name
                            for name, data in sorted(self.profiles.items())
                            if not ProfileSpec.from_dict(name, data).auth_provider
                        ),
                        None,
                    )
                    if replacement:
                        self.set_active_profile_name(replacement)
            return
        if runtime is not None:
            self.execution_runtime = str(runtime or "").strip()
        runtime_id = self.execution_runtime
        if _is_direct_subscription_id(runtime_id):
            from ..provider_auth import create_provider_auth

            adapter = create_provider_auth(runtime_id)
            existing_data = self.profiles.get(adapter.profile_name)
            existing = (
                ProfileSpec.from_dict(adapter.profile_name, existing_data)
                if isinstance(existing_data, dict)
                else None
            )
            if existing is not None and existing.auth_provider == runtime_id:
                profile = ProfileSpec(
                    name=existing.name,
                    protocol=adapter.protocol,
                    base_url=adapter.base_url,
                    api_key_env=existing.api_key_env,
                    auth_provider=runtime_id,
                    extra_headers=dict(existing.extra_headers),
                    default_model=existing.default_model,
                    reasoning_effort=existing.reasoning_effort,
                    web_search_adapter=existing.web_search_adapter,
                    web_search_model=existing.web_search_model,
                    notes=existing.notes,
                    cache_capability=existing.cache_capability,
                    reasoning_trace_adapter=existing.reasoning_trace_adapter,
                )
            else:
                profile = ProfileSpec(
                    name=adapter.profile_name,
                    protocol=adapter.protocol,
                    base_url=adapter.base_url,
                    auth_provider=runtime_id,
                    notes=f"{adapter.display_name}. Uses Alysis Code's native agent loop.",
                )
            self.add_profile_spec(
                profile,
                make_active=True,
                allow_subscription_update=True,
            )
            self.subscription_selection_required = bool(
                self.subscription_selection_required
                or not profile.default_model
                or profile.reasoning_effort is None
            )
            return
        if not runtime_id or runtime_id in self.agent_runtimes:
            return
        option = next(
            (
                item
                for item in _runtime_setup_options()
                if str(getattr(item, "id", "") or "").strip() == runtime_id
            ),
            None,
        )
        if option is None:
            return
        self.agent_runtimes[runtime_id] = AgentRuntimeSettings(
            adapter=str(getattr(option, "adapter", "") or "").strip(),
            executable=str(getattr(option, "default_executable", "") or "").strip(),
        )

    def set_active_profile_name(self, name: str) -> None:
        profile_name = str(name or "").strip().lower()
        if profile_name not in self.profiles:
            raise KeyError(f"Unknown profile: {name}")
        was_pending_for_profile = bool(
            self.subscription_selection_required and self.active_profile == profile_name
        )
        original_pending_for_profile = bool(
            self._original.get("subscription_selection_required")
            and str(self._original.get("active_profile") or "") == profile_name
        )
        self.active_profile = profile_name
        self._subscription_models_cache = ()
        self._subscription_models_loaded = False
        self._provider_models_cache_identity = None
        self._provider_models_cache = ()
        self._provider_model_catalog_warning = ""
        profile = ProfileSpec.from_dict(profile_name, self.profiles[profile_name])
        cache_cfg = self._resolution_cfg()
        if apply_hosted_prompt_cache_defaults(cache_cfg, profile):
            self.fields["prompt_cache_mode"] = cache_cfg.prompt_cache_mode
            self.hosted_prompt_cache_defaults_applied = True
        if profile.base_url:
            self.fields["base_url"] = profile.base_url
        if profile.auth_provider:
            self.fields["model"] = profile.default_model
            self.thinking_label = (
                "auto"
                if profile.reasoning_effort is None
                else ("off" if profile.reasoning_effort == "none" else profile.reasoning_effort)
            )
            self.subscription_selection_required = bool(
                was_pending_for_profile
                or original_pending_for_profile
                or not profile.default_model
                or profile.reasoning_effort is None
            )
        else:
            if profile.default_model:
                self.fields["model"] = profile.default_model
            if profile.reasoning_effort is not None:
                self.thinking_label = (
                    "off" if profile.reasoning_effort == "none" else profile.reasoning_effort
                )
            self.subscription_selection_required = False
        self._normalize_provider_thinking_label()
        self.refresh_api_key_status()

    def staged_api_key_target_profile(self) -> str | None:
        if not self.new_api_key.strip():
            return None
        return self.new_api_key_profile or self.active_profile or None

    def staged_api_key_for_active_profile(self) -> str:
        target = self.staged_api_key_target_profile()
        if target != (self.active_profile or None):
            return ""
        return self.new_api_key.strip()

    def add_profile_spec(
        self,
        profile: ProfileSpec,
        *,
        make_active: bool = True,
        allow_subscription_update: bool = False,
    ) -> None:
        existing_data = self.profiles.get(profile.name)
        existing = (
            ProfileSpec.from_dict(profile.name, existing_data)
            if isinstance(existing_data, dict)
            else None
        )
        if existing is not None and existing.auth_provider:
            trusted_refresh = bool(
                allow_subscription_update and profile.auth_provider == existing.auth_provider
            )
            if not trusted_refresh:
                raise ConfigError(
                    f"Profile {existing.name!r} is managed by the "
                    f"{existing.auth_provider!r} subscription connection and cannot be "
                    "overwritten through generic profile settings. Choose a different "
                    "profile name or manage the connection through auth."
                )
        self.profiles[profile.name] = profile.to_dict()
        if make_active:
            self.set_active_profile_name(profile.name)

    def update_active_profile_spec(self, **fields: Any) -> None:
        if self.active_profile not in self.profiles:
            raise KeyError("No active profile configured.")
        profile = ProfileSpec.from_dict(self.active_profile, self.profiles[self.active_profile])
        if profile.auth_provider:
            protected = {
                "protocol",
                "base_url",
                "api_key_env",
                "auth_provider",
                "extra_headers",
                "default_model",
                "reasoning_effort",
                "reasoning_trace_adapter",
            }
            if any(key in protected and fields[key] != getattr(profile, key) for key in fields):
                raise ConfigError(
                    "Subscription connection fields are provider-managed; use Default Model "
                    "for model and reasoning choices."
                )
        self.profiles[self.active_profile] = replace(profile, **fields).to_dict()
        self.set_active_profile_name(self.active_profile)

    def remove_profile_name(self, name: str) -> bool:
        profile_name = str(name or "").strip().lower()
        if profile_name not in self.profiles:
            raise KeyError(f"Unknown profile: {name}")
        staged_key_discarded = bool(
            self.new_api_key.strip() and self.staged_api_key_target_profile() == profile_name
        )
        if staged_key_discarded:
            self.new_api_key = ""
            self.new_api_key_profile = None
        self.profiles.pop(profile_name, None)
        if self.active_profile == profile_name:
            next_profile = sorted(self.profiles)[0] if self.profiles else ""
            if next_profile:
                self.set_active_profile_name(next_profile)
            else:
                self.active_profile = ""
                self.refresh_api_key_status()
        return staged_key_discarded

    def _resolution_cfg(self) -> AppConfig:
        cfg = AppConfig(
            model=str(self.fields.get("model", "") or ""),
            base_url=str(self.fields.get("base_url", "") or ""),
            stream=str(self.fields.get("stream", "true")).strip().lower()
            in {"1", "true", "yes", "on"},
            prompt_cache_mode=_normalize_prompt_cache_mode(
                self.fields.get("prompt_cache_mode", "manual")
            ),
            hosted_prompt_cache_defaults_applied=self.hosted_prompt_cache_defaults_applied,
            cache={
                "prompt_cache_key_enabled": self.cache_prompt_key_enabled,
                "keepalive_enabled": self.cache_keepalive_enabled,
            },
            prompt_cache_key=str(self.fields.get("prompt_cache_key", "") or ""),
            prompt_cache_retention=str(self.fields.get("prompt_cache_retention", "") or ""),
            anthropic_prompt_cache_enabled=_normalize_bool_text(
                self.fields.get("anthropic_prompt_cache_enabled", "false"),
                label="Anthropic prompt cache",
            ),
            anthropic_prompt_cache_ttl=_normalize_anthropic_prompt_cache_ttl(
                self.fields.get("anthropic_prompt_cache_ttl", "5m")
            ),
            web_search_mode=str(self.fields.get("web_search_mode", "auto") or "auto"),
            web_search_policy=str(self.fields.get("web_search_policy", "auto") or "auto"),
            web_search_adapter=str(self.fields.get("web_search_adapter", "auto") or "auto"),
            web_search_base_url=str(self.fields.get("web_search_base_url", "") or "") or None,
            web_search_model=str(self.fields.get("web_search_model", "") or "") or None,
        )
        cfg.extra_fields = {"profiles": dict(self.profiles)}
        if self.active_profile:
            cfg.extra_fields["active_profile"] = self.active_profile
        execution = getattr(cfg, "execution", None)
        if execution is not None:
            direct = self.execution_backend == "delegated" and _is_direct_subscription_id(
                self.execution_runtime
            )
            execution.backend = "native" if direct else self.execution_backend
            execution.runtime = None if direct else (self.execution_runtime or None)
        if hasattr(cfg, "agent_runtimes"):
            cfg.agent_runtimes = copy.deepcopy(self.agent_runtimes)
        return cfg

    def refresh_api_key_status(self) -> None:
        try:
            resolved_key = resolve_api_key(self._resolution_cfg())
        except ConfigError:
            self.api_key_source = "error"
            self.masked_api_key = "(not set)"
            return
        self.api_key_source = resolved_key.source
        self.masked_api_key = mask_api_key(resolved_key.key)

    def commit_to(self, cfg: AppConfig) -> ConfigMenuResult:
        validation_error = self.validate()
        if validation_error is not None:
            return ConfigMenuResult(
                saved=False,
                changes={},
                api_key_changed=False,
                error=validation_error,
            )

        changes: dict[str, Any] = {}
        cfg.hosted_prompt_cache_defaults_applied = self.hosted_prompt_cache_defaults_applied

        execution = getattr(cfg, "execution", None)
        if execution is None:
            return ConfigMenuResult(
                saved=False,
                changes={},
                api_key_changed=False,
                error="Model access configuration is unavailable.",
            )
        current_backend = _normalize_execution_backend(getattr(execution, "backend", "native"))
        current_runtime = str(getattr(execution, "runtime", None) or "").strip()
        direct = self.execution_backend == "delegated" and _is_direct_subscription_id(
            self.execution_runtime
        )
        desired_backend = "native" if direct else self.execution_backend
        desired_runtime = None if direct else (self.execution_runtime or None)
        if current_backend != desired_backend:
            execution.backend = desired_backend
            changes["execution.backend"] = desired_backend
        if current_runtime != str(desired_runtime or ""):
            execution.runtime = desired_runtime
            changes["execution.runtime"] = desired_runtime
        current_agent_runtimes = dict(getattr(cfg, "agent_runtimes", {}) or {})
        if current_agent_runtimes != self.agent_runtimes:
            cfg.agent_runtimes = copy.deepcopy(self.agent_runtimes)
            changes["agent_runtimes"] = sorted(self.agent_runtimes)

        original_profiles = self._original.get("profiles")
        if self.profiles != (original_profiles if isinstance(original_profiles, dict) else {}):
            cfg.extra_fields["profiles"] = dict(sorted(self.profiles.items()))
            changes["profiles"] = sorted(self.profiles)

        original_active_profile = str(self._original.get("active_profile") or "")
        if self.active_profile != original_active_profile:
            if self.active_profile:
                cfg.extra_fields["active_profile"] = self.active_profile
                set_active_profile(cfg, self.active_profile)
            else:
                cfg.extra_fields.pop("active_profile", None)
            changes["active_profile"] = self.active_profile

        original_workspace = str(self._original.get("default_workspace_path") or "")
        if self.default_workspace_path != original_workspace:
            if self.default_workspace_path:
                cfg.extra_fields["default_workspace_path"] = self.default_workspace_path
            else:
                cfg.extra_fields.pop("default_workspace_path", None)
            changes["default_workspace_path"] = self.default_workspace_path

        desired_base_url = str(self.fields.get("base_url", ""))
        if str(getattr(cfg, "base_url", "") or "") != desired_base_url:
            set_config_value(cfg, "base_url", desired_base_url)
            changes["base_url"] = desired_base_url

        desired_model = str(self.fields.get("model", ""))
        original_fields = self._original.get("fields")
        original_model = (
            str(original_fields.get("model", "") or "")
            if isinstance(original_fields, dict)
            else str(getattr(cfg, "model", "") or "")
        )
        # Only write the model when the user changed it in this menu.  Subscription
        # profiles can intentionally differ from the top-level compatibility field;
        # comparing against ``cfg.model`` made an unrelated save rewrite the active
        # subscription model.
        if desired_model != original_model:
            set_config_value(
                cfg,
                "model",
                desired_model,
                allow_subscription_selection=True,
            )
            changes["model"] = desired_model

        desired_timeout = float(str(self.fields.get("llm_timeout_s", "")).strip())
        current_timeout = _finite_float(getattr(cfg, "llm_timeout_s", None), fallback=None)
        if current_timeout != desired_timeout:
            set_config_value(cfg, "llm_timeout_s", _format_number(desired_timeout))
            changes["llm_timeout_s"] = desired_timeout

        desired_step_budget_policy = _normalize_step_budget_policy(
            self.fields.get("step_budget_policy", "autonomous")
        )
        if str(getattr(cfg, "step_budget_policy", "") or "") != desired_step_budget_policy:
            set_config_value(cfg, "step_budget_policy", desired_step_budget_policy)
            changes["step_budget_policy"] = desired_step_budget_policy

        for key in ("max_steps", "task_max_steps", "subagent_max_steps"):
            desired_int = int(str(self.fields.get(key, "")).strip())
            current_int = _finite_int(getattr(cfg, key, None), fallback=None)
            if current_int != desired_int:
                set_config_value(cfg, key, str(desired_int))
                changes[key] = desired_int

        desired_subagent_timeout = _coerce_optional_positive_float(
            self.fields.get("subagent_timeout_s", ""),
            key="subagent_timeout_s",
        )
        current_subagent_timeout = _finite_float(
            getattr(cfg, "subagent_timeout_s", None),
            fallback=None,
        )
        if current_subagent_timeout != desired_subagent_timeout:
            set_config_value(
                cfg,
                "subagent_timeout_s",
                (
                    "unlimited"
                    if desired_subagent_timeout is None
                    else _format_number(desired_subagent_timeout)
                ),
            )
            changes["subagent_timeout_s"] = desired_subagent_timeout

        desired_cache_mode = _normalize_prompt_cache_mode(
            self.fields.get("prompt_cache_mode", "manual")
        )
        if (
            str(getattr(cfg, "prompt_cache_mode", "") or "manual") != desired_cache_mode
            or self.prompt_cache_mode_explicitly_set
        ):
            set_config_value(cfg, "prompt_cache_mode", desired_cache_mode)
            changes["prompt_cache_mode"] = desired_cache_mode

        for key in ("prompt_cache_key", "prompt_cache_retention"):
            desired_text = str(self.fields.get(key, "") or "").strip()
            current_text = str(getattr(cfg, key, "") or "").strip()
            if current_text != desired_text:
                set_config_value(cfg, key, desired_text)
                changes[key] = desired_text or None

        desired_anthropic_enabled = _normalize_bool_text(
            self.fields.get("anthropic_prompt_cache_enabled", "false"),
            label="Anthropic prompt cache",
        )
        if bool(getattr(cfg, "anthropic_prompt_cache_enabled", False)) != desired_anthropic_enabled:
            set_config_value(
                cfg,
                "anthropic_prompt_cache_enabled",
                "true" if desired_anthropic_enabled else "false",
            )
            changes["anthropic_prompt_cache_enabled"] = desired_anthropic_enabled

        desired_anthropic_ttl = _normalize_anthropic_prompt_cache_ttl(
            self.fields.get("anthropic_prompt_cache_ttl", "5m")
        )
        if str(getattr(cfg, "anthropic_prompt_cache_ttl", "") or "5m") != desired_anthropic_ttl:
            set_config_value(cfg, "anthropic_prompt_cache_ttl", desired_anthropic_ttl)
            changes["anthropic_prompt_cache_ttl"] = desired_anthropic_ttl

        desired_web_search_policy = normalize_web_search_policy(
            self.fields.get("web_search_policy", "auto")
        )
        if str(getattr(cfg, "web_search_policy", "auto") or "auto") != desired_web_search_policy:
            set_config_value(cfg, "web_search_policy", desired_web_search_policy)
            changes["web_search_policy"] = desired_web_search_policy

        desired_web_search_mode = _normalize_web_search_mode(
            self.fields.get("web_search_mode", "auto")
        )
        if str(getattr(cfg, "web_search_mode", "auto") or "auto") != desired_web_search_mode:
            set_config_value(cfg, "web_search_mode", desired_web_search_mode)
            changes["web_search_mode"] = desired_web_search_mode

        desired_web_search_adapter = normalize_web_search_adapter(
            self.fields.get("web_search_adapter", "auto")
        )
        if str(getattr(cfg, "web_search_adapter", "auto") or "auto") != desired_web_search_adapter:
            set_config_value(cfg, "web_search_adapter", desired_web_search_adapter)
            changes["web_search_adapter"] = desired_web_search_adapter

        for key in ("web_search_base_url", "web_search_model"):
            desired_text = str(self.fields.get(key, "") or "").strip()
            current_text = str(getattr(cfg, key, "") or "").strip()
            if current_text != desired_text:
                set_config_value(cfg, key, desired_text)
                changes[key] = desired_text or None

        desired_cache_aware = _normalize_bool_text(
            self.fields.get("cache_aware_compaction", "true"),
            label="Cache-aware compaction",
        )
        desired_cache_aware_ratio = _parse_cache_aware_min_trigger_ratio(
            self.fields.get("cache_aware_min_trigger_ratio", _CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT)
        )
        raw_compaction = _raw_compaction_extra_fields(cfg)
        compaction = dict(raw_compaction)
        current_cache_aware = _cache_aware_compaction_bool(compaction.get("cache_aware_compaction"))
        current_cache_aware_ratio = _cache_aware_min_trigger_ratio_value(
            compaction.get("cache_aware_min_trigger_ratio")
        )
        compaction_changed = False
        if current_cache_aware != desired_cache_aware:
            if desired_cache_aware == _CACHE_AWARE_COMPACTION_DEFAULT:
                compaction.pop("cache_aware_compaction", None)
            else:
                compaction["cache_aware_compaction"] = desired_cache_aware
            changes["compaction.cache_aware_compaction"] = desired_cache_aware
            compaction_changed = True
        if current_cache_aware_ratio != desired_cache_aware_ratio:
            if desired_cache_aware_ratio == _CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT:
                compaction.pop("cache_aware_min_trigger_ratio", None)
            else:
                compaction["cache_aware_min_trigger_ratio"] = desired_cache_aware_ratio
            changes["compaction.cache_aware_min_trigger_ratio"] = desired_cache_aware_ratio
            compaction_changed = True
        if compaction_changed:
            if compaction:
                cfg.extra_fields["compaction"] = dict(sorted(compaction.items()))
            else:
                cfg.extra_fields.pop("compaction", None)

        desired_thinking_value = thinking_label_to_config_value(self.thinking_label)
        desired_reasoning_effort = thinking_label_to_reasoning_effort(self.thinking_label)
        original_thinking_label = str(self._original.get("thinking_label") or "auto")
        should_write_thinking = (
            self.thinking_label_explicitly_set or self.thinking_label != original_thinking_label
        )
        if should_write_thinking:
            if getattr(cfg, "llm_enable_thinking", None) != desired_thinking_value:
                set_config_value(
                    cfg,
                    "llm_enable_thinking",
                    _thinking_config_text(desired_thinking_value),
                    allow_subscription_selection=True,
                )
                changes["llm_enable_thinking"] = desired_thinking_value
            profile_effort = get_active_profile(cfg).reasoning_effort
            desired_profile_effort = desired_reasoning_effort or "auto"
            if (
                getattr(cfg, "llm_reasoning_effort", None) != desired_reasoning_effort
                or profile_effort != desired_profile_effort
            ):
                set_config_value(
                    cfg,
                    "llm_reasoning_effort",
                    desired_reasoning_effort or "auto",
                    allow_subscription_selection=True,
                )
                changes["llm_reasoning_effort"] = desired_reasoning_effort
            _set_thinking_label_hint(cfg, self.thinking_label)

        raw_role_models = cfg.extra_fields.get("role_models")
        role_models = _role_model_storage_values(raw_role_models, self.role_models)
        current_role_models = _role_model_storage_values(
            raw_role_models,
            _normalized_role_model_values(raw_role_models),
        )
        if current_role_models != role_models:
            if role_models:
                cfg.extra_fields["role_models"] = dict(sorted(role_models.items()))
            else:
                cfg.extra_fields.pop("role_models", None)
            changes["role_models"] = dict(role_models)

        raw_forge_role_models = cfg.extra_fields.get("forge_role_models")
        forge_role_models = _role_model_storage_values(
            raw_forge_role_models,
            self.forge_role_models,
        )
        current_forge_role_models = _role_model_storage_values(
            raw_forge_role_models,
            _normalized_role_model_values(raw_forge_role_models),
        )
        if current_forge_role_models != forge_role_models:
            if forge_role_models:
                cfg.extra_fields["forge_role_models"] = dict(sorted(forge_role_models.items()))
            else:
                cfg.extra_fields.pop("forge_role_models", None)
            changes["forge_role_models"] = dict(forge_role_models)

        default_temperature = _finite_float(getattr(cfg, "temperature", 0.2), fallback=0.2)
        for role in ROLE_ORDER:
            field_name = _ROLE_TEMPERATURE_FIELDS.get(role)
            if field_name is None:
                continue
            raw_value = str(self.role_temperatures.get(role, "")).strip()
            current_value = _finite_float(getattr(cfg, field_name, None), fallback=None)
            if raw_value:
                desired_temperature = float(raw_value)
            else:
                desired_temperature = default_temperature
            if current_value != desired_temperature:
                set_config_value(cfg, field_name, _format_number(desired_temperature))
                changes[field_name] = desired_temperature

        desired_sandbox_mode = normalize_sandbox_mode(self.fields.get("sandbox_mode", "strict"))
        if sandbox_mode_from_config(cfg) != desired_sandbox_mode:
            apply_sandbox_mode_to_config(cfg, desired_sandbox_mode)
            changes["sandbox_mode"] = desired_sandbox_mode

        desired_persona = str(self.fields.get("default_persona") or "code").strip().lower()
        if desired_persona not in {"code", "architect", "ask", "debug"}:
            desired_persona = "code"
        if str(getattr(cfg, "default_persona", "code") or "code") != desired_persona:
            set_config_value(cfg, "default_persona", desired_persona)
            changes["default_persona"] = desired_persona

        active = get_active_profile(cfg)
        selection_confirmed = bool(
            active.default_model
            and active.reasoning_effort is not None
            and self.thinking_label_explicitly_set
            and subscription_selection_supported(
                active,
                _subscription_models_for_state(self),
            )
        )
        if active.auth_provider and self.subscription_selection_required and selection_confirmed:
            cfg.extra_fields.pop(SUBSCRIPTION_SELECTION_REQUIRED_KEY, None)
            cfg.extra_fields.pop("subscription_reconnect_required", None)
            cfg.extra_fields["onboarded"] = True
            self.subscription_selection_required = False
            changes["subscription_model_selection"] = "configured"
        elif active.auth_provider and self.subscription_selection_required:
            cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = active.auth_provider

        return ConfigMenuResult(
            saved=True,
            changes=changes,
            api_key_changed=bool(
                self.new_api_key.strip() or self.clear_stored_key_confirmed or self._account_changed
            ),
        )

    def validate(self) -> str | None:
        try:
            backend = _normalize_execution_backend(self.execution_backend)
        except ValueError as exc:
            return str(exc)
        if not isinstance(self.agent_runtimes, dict):
            return "Agent runtime settings must be a mapping."
        if backend == "delegated":
            runtime = str(self.execution_runtime or "").strip()
            if not runtime:
                return "Choose a supported AI subscription connection."
            known_runtime_ids = {value for value, _label, _description in _runtime_setup_rows()}
            if runtime not in known_runtime_ids:
                return f"Unknown AI subscription connection: {runtime}"
            if not _is_direct_subscription_id(runtime) and runtime not in self.agent_runtimes:
                return f"AI subscription connection settings are missing: {runtime}"
            if _is_direct_subscription_id(runtime):
                try:
                    active = ProfileSpec.from_dict(
                        self.active_profile,
                        self.profiles[self.active_profile],
                    )
                except (KeyError, ConfigError):
                    return "AI subscription profile is missing."
                if active.auth_provider != runtime:
                    return "AI subscription profile does not match the selected connection."
                if self.subscription_selection_required:
                    try:
                        from ..provider_auth import create_provider_auth

                        connected = create_provider_auth(runtime).account_status().connected
                    except Exception:
                        connected = False
                    if connected:
                        model = str(self.fields.get("model") or "").strip()
                        if not model:
                            return "Choose a subscription model in Default Model."
                        models = _subscription_models_for_state(self)
                        if not models:
                            return (
                                self.config_warning or "Subscription model catalog is unavailable."
                            )
                        selected = next((item for item in models if item.id == model), None)
                        if selected is None:
                            return "Choose a model available to the connected subscription account."
                        if not self.thinking_label_explicitly_set:
                            return "Choose a reasoning effort for the subscription model."
                        effort = thinking_label_to_reasoning_effort(self.thinking_label)
                        supported = {item.id for item in selected.reasoning_efforts}
                        if effort is not None and supported and effort not in supported:
                            return "Choose a reasoning effort supported by the subscription model."
        timeout_text = str(self.fields.get("llm_timeout_s", "")).strip()
        try:
            timeout = float(timeout_text)
        except ValueError:
            return "Request timeout (seconds) must be a positive number."
        if timeout <= 0 or not math.isfinite(timeout):
            return "Request timeout (seconds) must be a positive number."
        base_url_text = str(self.fields.get("base_url", "")).strip()
        try:
            validate_base_url(base_url_text, key="Base URL", allow_empty=True)
        except ConfigError as exc:
            return str(exc)
        try:
            _normalize_thinking_label(self.thinking_label)
        except ValueError as exc:
            return str(exc)
        try:
            _normalize_step_budget_policy(self.fields.get("step_budget_policy", "autonomous"))
        except ValueError as exc:
            return str(exc)
        for key in ("max_steps", "task_max_steps", "subagent_max_steps"):
            text = str(self.fields.get(key, "")).strip()
            label = _FIELD_LABELS.get(key, key)
            try:
                value = int(text)
            except ValueError:
                return f"{label} must be a positive integer."
            if value <= 0:
                return f"{label} must be a positive integer."
        try:
            _coerce_optional_positive_float(
                self.fields.get("subagent_timeout_s", ""),
                key="subagent_timeout_s",
            )
        except ConfigError:
            return "Subagent timeout must be a positive number or unlimited."
        try:
            _normalize_prompt_cache_mode(self.fields.get("prompt_cache_mode", "manual"))
        except ValueError as exc:
            return str(exc)
        try:
            normalize_web_search_policy(self.fields.get("web_search_policy", "auto"))
            _normalize_web_search_mode(self.fields.get("web_search_mode", "auto"))
            normalize_web_search_adapter(self.fields.get("web_search_adapter", "auto"))
            validate_base_url(
                str(self.fields.get("web_search_base_url", "") or "").strip(),
                key="Web search base URL",
                allow_empty=True,
            )
        except (ConfigError, ValueError) as exc:
            return str(exc)
        try:
            _normalize_bool_text(
                self.fields.get("anthropic_prompt_cache_enabled", "false"),
                label="Anthropic prompt cache",
            )
        except ValueError as exc:
            return str(exc)
        try:
            _normalize_anthropic_prompt_cache_ttl(
                self.fields.get("anthropic_prompt_cache_ttl", "5m")
            )
        except ValueError as exc:
            return str(exc)
        try:
            _normalize_bool_text(
                self.fields.get("cache_aware_compaction", "true"),
                label="Cache-aware compaction",
            )
        except ValueError as exc:
            return str(exc)
        try:
            _parse_cache_aware_min_trigger_ratio(
                self.fields.get(
                    "cache_aware_min_trigger_ratio",
                    _CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT,
                )
            )
        except ValueError as exc:
            return str(exc)
        for role, raw_value in self.role_temperatures.items():
            text = str(raw_value).strip()
            if not text:
                continue
            label = f"{_role_label(role)} temperature"
            try:
                temperature = float(text)
            except ValueError:
                return f"{label} must be a non-negative number."
            if temperature < 0 or not math.isfinite(temperature):
                return f"{label} must be a non-negative number."
        return None


def run_config_menu(
    *,
    cfg: AppConfig | None = None,
    auto_focus: str | None = None,
) -> ConfigMenuResult:
    """Open the inline configuration menu and persist changes on save."""
    effective_cfg = cfg or load_config()
    state = ConfigMenuState.from_cfg(effective_cfg)
    console = _resolve_console()

    if auto_focus == "api_key":
        _run_api_key_section(state, console)
    elif auto_focus == "model":
        _run_default_section(state, console)
    elif auto_focus == "execution":
        _run_execution_section(state, console)
    elif auto_focus == "web_search":
        _run_web_search_section(state, console)

    while True:
        action = _prompt_main_action(console, state)
        if action == "execution":
            _run_execution_section(state, console)
        elif action == "profile":
            _run_provider_section(state, console)
        elif action == "api_key":
            _run_api_key_section(state, console)
        elif action == "default":
            _run_default_section(state, console)
        elif action == "web_search":
            _run_web_search_section(state, console)
        elif action == "cache":
            _run_cache_section(state, console)
        elif action == "request_timeout":
            _run_request_timeout_section(state, console)
        elif action == "subagents":
            _run_subagent_section(state, console)
        elif action == "forge":
            _run_forge_section(state, console)
        elif action == "personas":
            _run_personas_section(state, console)
        elif action == "sandbox":
            _run_sandbox_section(state, console)
        elif action == "save":
            result = _save_and_exit(state, effective_cfg, console)
            if result.saved or result.error is None:
                return result
        elif action == "cancel" and _confirm_cancel_when_dirty(state, console):
            return ConfigMenuResult(
                saved=state._account_changed, changes={}, api_key_changed=state._account_changed
            )


def _resolve_console() -> Console:
    return make_console()


def _execution_summary_text(state: ConfigMenuState) -> str:
    if _active_alysis_profile(state) is not None:
        return "Alysis Code subscription"
    if state.execution_backend == "native":
        return "API key · Alysis Code agent"
    runtime = str(state.execution_runtime or "").strip()
    if not runtime:
        return _MISSING_RUNTIME
    labels = {value: label for value, label, _description in _runtime_setup_rows()}
    return f"AI subscription · {labels.get(runtime, runtime)}"


def _active_alysis_profile(state: ConfigMenuState) -> ProfileSpec | None:
    """Recognize hosted account access by its effective endpoint, not its model or key."""
    from .. import alysis_cloud

    if state.execution_backend != "native" or state.active_profile not in state.profiles:
        return None
    profile = ProfileSpec.from_dict(state.active_profile, state.profiles[state.active_profile])
    if profile.auth_provider or profile.protocol != OPENAI_COMPAT_PROTOCOL:
        return None
    effective_url = str(state.fields.get("base_url") or profile.base_url).strip().rstrip("/")
    preset = next(p for p in PROFILE_PRESETS if p.key == alysis_cloud.PROFILE_KEY)
    hosted_urls = {preset.base_url.strip().rstrip("/")}
    try:
        hosted_urls.add(alysis_cloud.gateway_base_url().strip().rstrip("/"))
    except alysis_cloud.AlysisCloudConfigError:
        pass
    return profile if effective_url in hosted_urls else None


def _inactive_native_summary(summary: str) -> str:
    if summary == _MISSING_REQUIRED:
        base = "not configured"
    elif summary == _CLEAR_KEY_PENDING:
        base = "will be cleared on save"
    else:
        base = re.sub(r"\[/?[a-z][a-z0-9 _#-]*\]", "", str(summary)).strip()
    return f"{base} · inactive while using an AI subscription"


def _profile_summary_text(state: ConfigMenuState) -> str:
    if state.active_profile:
        return f"{state.active_profile} (active)"
    if state.profiles:
        return f"{len(state.profiles)} profiles, none active"
    return _MISSING_REQUIRED


def _api_key_summary_text(state: ConfigMenuState) -> str:
    if state.clear_stored_key_confirmed and (
        not state.clear_stored_key_profile
        or state.clear_stored_key_profile == (state.active_profile or None)
    ):
        return _CLEAR_KEY_PENDING
    pending_key = state.staged_api_key_for_active_profile()
    if pending_key:
        return f"set ({mask_api_key(pending_key)})"
    if state.masked_api_key != "(not set)":
        return f"set ({state.masked_api_key})"
    return _MISSING_REQUIRED


def _default_model_summary_text(state: ConfigMenuState) -> str:
    model = state.fields.get("model", "").strip()
    if not model:
        return _MISSING_REQUIRED
    return f"{model_display_name(model)} · thinking {state.thinking_label}"


def _cache_summary_text(state: ConfigMenuState) -> str:
    mode = _normalize_prompt_cache_mode(state.fields.get("prompt_cache_mode", "manual"))
    compaction = _cache_aware_summary_text(state)
    policy, policy_error = _resolved_cache_policy_with_error_for_state(state)
    policy_summary = _cache_policy_summary_text(policy, compact=True, error=policy_error)
    if mode == "off":
        return f"cache off · {policy_summary} · {compaction}"
    if mode == "auto":
        ttl = (
            policy.anthropic_cache_control_ttl
            if policy is not None
            else _normalize_anthropic_prompt_cache_ttl(
                state.fields.get("anthropic_prompt_cache_ttl", "5m")
            )
        )
        return f"auto · {policy_summary} · Anthropic TTL {ttl} · {compaction}"

    key = str(state.fields.get("prompt_cache_key", "") or "").strip()
    retention = str(state.fields.get("prompt_cache_retention", "") or "").strip()
    key_summary = key or "no manual key"
    retention_summary = retention or "default retention"
    anthropic = (
        "on"
        if _normalize_bool_text(
            state.fields.get("anthropic_prompt_cache_enabled", "false"),
            label="Anthropic prompt cache",
        )
        else "off"
    )
    return (
        f"manual · {policy_summary} · {key_summary} · {retention_summary} · "
        f"Anthropic {anthropic} · {compaction}"
    )


def _effective_cache_capability_for_state(
    state: ConfigMenuState,
) -> EffectiveCacheCapability | None:
    if not state.active_profile or state.active_profile not in state.profiles:
        return None
    try:
        profile = ProfileSpec.from_dict(state.active_profile, state.profiles[state.active_profile])
        preview_profile = ProfileSpec(
            name=profile.name,
            protocol=profile.protocol or OPENAI_COMPAT_PROTOCOL,
            base_url=str(state.fields.get("base_url", profile.base_url) or profile.base_url),
            api_key_env=profile.api_key_env,
            auth_provider=profile.auth_provider,
            extra_headers=dict(profile.extra_headers),
            default_model=str(
                state.fields.get("model", profile.default_model) or profile.default_model
            ),
            reasoning_effort=profile.reasoning_effort,
            web_search_adapter=profile.web_search_adapter,
            web_search_model=profile.web_search_model,
            notes=profile.notes,
            cache_capability=profile.cache_capability,
            reasoning_trace_adapter=profile.reasoning_trace_adapter,
        )
        cfg = state._resolution_cfg()
        model = str(preview_profile.default_model or getattr(cfg, "model", "") or "").strip()
        base_url = str(preview_profile.base_url or getattr(cfg, "base_url", "") or "").strip()
        provider_key = (
            resolve_model_provider_key(
                cfg=cfg,
                model_name=model,
                base_url=base_url,
                profile_name=preview_profile.name,
            )
            or preview_profile.name
        )
        from ..llm.factory import resolve_model_protocol

        protocol = resolve_model_protocol(
            provider_key=provider_key,
            model=model,
            protocol=str(preview_profile.protocol or OPENAI_COMPAT_PROTOCOL).strip(),
        )
        capabilities = get_provider_protocol_capabilities(
            provider_key=provider_key,
            protocol=protocol,
        )
        preset = find_preset_for_profile(preview_profile)
        from ..provider_auth import create_provider_auth

        provider_auth = (
            create_provider_auth(preview_profile.auth_provider)
            if preview_profile.auth_provider
            else None
        )
        return resolve_effective_cache_capability(
            provider_key=provider_key,
            protocol=protocol,
            model=model,
            base_url=base_url,
            transport_capabilities=capabilities,
            auth_cache_capability=getattr(provider_auth, "cache_capability", None),
            preset_cache_capability=(preset.cache_capability if preset is not None else None),
            profile_cache_capability=preview_profile.cache_capability,
        )
    except Exception as exc:
        _LOGGER.warning(
            "Effective cache capability resolution failed for profile %s: %s",
            state.active_profile,
            exc,
        )
        return None


def _resolved_cache_policy_for_state(
    state: ConfigMenuState,
) -> ResolvedPromptCachePolicy | None:
    policy, _error = _resolved_cache_policy_with_error_for_state(state)
    return policy


def _resolved_cache_policy_with_error_for_state(
    state: ConfigMenuState,
) -> tuple[ResolvedPromptCachePolicy | None, str | None]:
    if not state.active_profile or state.active_profile not in state.profiles:
        return None, None
    try:
        profile = ProfileSpec.from_dict(state.active_profile, state.profiles[state.active_profile])
        cfg = state._resolution_cfg()
        model = str(state.fields.get("model", profile.default_model) or profile.default_model)
        base_url = str(state.fields.get("base_url", profile.base_url) or profile.base_url)
        provider_key = (
            resolve_model_provider_key(
                cfg=cfg,
                model_name=model,
                base_url=base_url,
                profile_name=profile.name,
            )
            or profile.name
        )
        from ..llm.factory import resolve_model_protocol

        protocol = resolve_model_protocol(
            provider_key=provider_key,
            model=model,
            protocol=str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip(),
        )
        capabilities = get_provider_protocol_capabilities(
            provider_key=provider_key,
            protocol=protocol,
        )
        cache_capability = _effective_cache_capability_for_state(state)
        policy = resolve_prompt_cache_policy(
            cfg=cfg,
            capabilities=capabilities,
            provider_key=provider_key,
            protocol=protocol,
            model=model,
            prompt_cache_key=str(state.fields.get("prompt_cache_key", "") or ""),
            prompt_cache_retention=str(state.fields.get("prompt_cache_retention", "") or ""),
            prompt_cache_namespace=None,
            cache_capability=cache_capability,
        )
        return policy, None
    except Exception as exc:
        _LOGGER.warning(
            "Prompt cache policy resolution failed for profile %s: %s",
            state.active_profile,
            exc,
        )
        return None, str(exc)


def _cache_policy_summary_text(
    policy: ResolvedPromptCachePolicy | None,
    *,
    compact: bool,
    error: str | None = None,
) -> str:
    if policy is None:
        if error:
            return f"policy unavailable: {error}"
        return "policy unknown"
    fields = ", ".join(policy.emitted_fields) if policy.emitted_fields else "no fields"
    if compact:
        return f"{policy.status} {policy.strategy}"
    allowed = ", ".join(policy.allowed_fields) if policy.allowed_fields else "none"
    usage = ", ".join(policy.trusted_usage_fields) if policy.trusted_usage_fields else "none"
    return (
        f"{policy.status}; strategy={policy.strategy}; source={policy.capability_source}; "
        f"allowed={allowed}; emits={fields}; usage={usage}"
    )


def _cache_aware_summary_text(state: ConfigMenuState) -> str:
    aware = (
        "on"
        if _normalize_bool_text(
            state.fields.get("cache_aware_compaction", "true"),
            label="Cache-aware compaction",
        )
        else "off"
    )
    ratio = _normalize_cache_aware_min_trigger_ratio(
        state.fields.get("cache_aware_min_trigger_ratio", _CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT)
    )
    return f"compaction {aware} · min {ratio}"


def _override_summary_text(values: dict[str, str]) -> str:
    count = sum(1 for value in values.values() if str(value).strip())
    if count <= 0:
        return "none"
    return _pluralize(count, "override")


def _web_search_summary_text(state: ConfigMenuState) -> str:
    policy = normalize_web_search_policy(state.fields.get("web_search_policy", "auto"))
    mode = _normalize_web_search_mode(state.fields.get("web_search_mode", "auto"))
    adapter = normalize_web_search_adapter(state.fields.get("web_search_adapter", "auto"))
    suffix = f" / adapter {adapter}" if adapter != "auto" else ""
    access = "model decides" if policy == "auto" else "off"
    return f"access {access} / backend {mode}{suffix}"


def _web_search_policy_rows() -> list[tuple[str, str, str]]:
    return [
        (
            "auto",
            "Model decides (recommended)",
            "Expose web search to the active model and let it decide when external evidence is needed.",
        ),
        ("off", "Off", "Do not expose web search to the active model."),
    ]


def _web_search_mode_rows() -> list[tuple[str, str, str]]:
    return [
        (
            "auto",
            "Auto (recommended)",
            "Use provider-native search when ready, otherwise use configured external search.",
        ),
        (
            "external",
            "External",
            "Use model-independent search only; Tavily requires an external search API key.",
        ),
        (
            "native",
            "Provider native",
            "Use only the active model provider's supported search adapter.",
        ),
        ("off", "Off", "Disable all web search backends."),
    ]


def _top_level_menu_rows(state: ConfigMenuState) -> list[tuple[str, str, str]]:
    subagent_values = {
        **{role: state.role_models.get(role, "") for role in ROLE_ORDER},
        **{f"{role}_temperature": value for role, value in state.role_temperatures.items()},
    }
    delegated = state.execution_backend == "delegated" and not _is_direct_subscription_id(
        state.execution_runtime
    )
    direct_subscription = state.execution_backend == "delegated" and _is_direct_subscription_id(
        state.execution_runtime
    )
    profile_summary = _profile_summary_text(state)
    api_key_summary = _api_key_summary_text(state)
    model_summary = _default_model_summary_text(state)
    if delegated:
        profile_summary = _inactive_native_summary(profile_summary)
        api_key_summary = _inactive_native_summary(api_key_summary)
        model_summary = _inactive_native_summary(model_summary)
    elif direct_subscription:
        api_key_summary = "not used · managed by AI subscription"
    native_suffix = " (inactive)" if delegated else ""
    api_key_suffix = " (not used)" if direct_subscription else native_suffix
    hosted_account = _active_alysis_profile(state) is not None
    if hosted_account:
        api_key_summary = "Manage your Alysis Code subscription connection"
    return [
        ("profile", f"Provider Profile{native_suffix}", profile_summary),
        ("api_key", "Account" if hosted_account else f"API Key{api_key_suffix}", api_key_summary),
        ("default", f"Default Model{native_suffix}", model_summary),
        ("web_search", "Web Search", _web_search_summary_text(state)),
        ("cache", "Context & Cache", _cache_summary_text(state)),
        (
            "request_timeout",
            "Request timeout (advanced)",
            f"{state.fields['llm_timeout_s']} seconds",
        ),
        ("subagents", "Subagent model overrides", _override_summary_text(subagent_values)),
        (
            "forge",
            "Forge model overrides",
            _override_summary_text(state.forge_role_models),
        ),
        ("sandbox", "Sandbox", _sandbox_summary_text(state)),
        ("personas", "Personas", _persona_summary_text(state)),
        ("execution", "Model Access", _execution_summary_text(state)),
    ]


def _run_execution_section(state: ConfigMenuState, console: Console) -> None:
    from ..alysis_cloud import PROFILE_KEY

    while True:
        hosted_account = _active_alysis_profile(state) is not None
        selected_backend = _run_config_picker(
            console=console,
            title="Model Access",
            subtitle="How would you like to connect Alysis Code to AI models?",
            rows=[
                (
                    "native",
                    "Use an API key",
                    "Connect directly to a supported model provider.",
                ),
                (
                    "delegated",
                    "Use an AI subscription",
                    "Sign in through a supported provider connection; API-key settings stay saved.",
                ),
            ],
            current_value="delegated" if hosted_account else state.execution_backend,
        )
        if selected_backend is None:
            _print_section_cancelled(console, "Model Access")
            return
        if selected_backend == "native":
            state.set_execution_backend("native")
            if hosted_account:
                _run_provider_section(state, console)
                return
            console.print("[green]Model access:[/green] API key. Save to apply.")
            return

        rows = [
            (
                PROFILE_KEY,
                "Alysis Code account",
                "Connect your Alysis Code subscription in the browser.",
            ),
            *_runtime_setup_rows(),
        ]
        current_runtime = PROFILE_KEY if hosted_account else state.execution_runtime
        if current_runtime not in {value for value, _label, _description in rows}:
            current_runtime = rows[0][0]
        selected_runtime = _run_config_picker(
            console=console,
            title="AI Subscription",
            subtitle=(
                "Connect during setup (or with `alysis auth login`), then choose the "
                "subscription model and reasoning effort in Default Model."
            ),
            rows=rows,
            current_value=current_runtime,
        )
        if selected_runtime is None:
            continue
        if selected_runtime == PROFILE_KEY:
            _run_alysis_account_section(state, console)
            return
        state.set_execution_backend("delegated", runtime=selected_runtime)
        runtime_label = next(
            (label for value, label, _description in rows if value == selected_runtime),
            selected_runtime,
        )
        console.print(
            f"[green]Model access:[/green] AI subscription via {escape(runtime_label)}. "
            "Save to apply."
        )
        _run_subscription_account_section(selected_runtime, console)
        return


def _run_alysis_account_section(state: ConfigMenuState, console: Console) -> None:
    from .. import account_login
    from ..alysis_cloud import PROFILE_KEY
    from ..profiles import get_profile

    while True:
        cfg = load_config()
        status = account_login.login_status(cfg)
        rows = [
            (
                "connect",
                "Reconnect / switch account" if status.logged_in else "Connect account",
                "Open Alysis Code sign-in in your browser.",
            )
        ]
        if status.logged_in:
            rows.append(
                ("disconnect", "Disconnect account", "Remove your saved account connection.")
            )
        rows.append(("back", "Back", "Return to configuration."))
        action = _run_config_picker(
            console=console,
            title="Alysis Code Account",
            subtitle="Account: connected" if status.logged_in else "Account: not connected",
            rows=rows,
            current_value="back",
        )
        if action in {None, "back"}:
            return
        try:
            if action == "disconnect":
                if not _prompt_yes_no("Disconnect your Alysis Code account? [y/N]"):
                    continue
                account_login.logout(cfg)
                state._account_changed = True
                state.refresh_api_key_status()
                console.print("[yellow]Alysis Code account disconnected.[/yellow]")
                continue
            account_login.login(
                cfg,
                output_write=lambda message: console.print(message, highlight=False),
            )
            profile = get_profile(cfg, PROFILE_KEY)
            if profile is not None:
                state.profiles[PROFILE_KEY] = profile.to_dict()
                state.set_active_profile_name(PROFILE_KEY)
                state.set_execution_backend("native")
            state._account_changed = True
            console.print("[green]Alysis Code account connected.[/green]")
        except (account_login.AlysisLoginError, ConfigError) as exc:
            console.print(f"[red]Alysis Code account action failed:[/red] {escape(str(exc))}")


def _run_subscription_account_section(provider_id: str, console: Console) -> None:
    from ..provider_auth import ProviderAuthError, create_provider_auth

    try:
        adapter = create_provider_auth(provider_id)
    except (ProviderAuthError, ValueError) as exc:
        console.print(f"[red]Subscription connection unavailable:[/red] {escape(str(exc))}")
        return
    while True:
        try:
            status = adapter.account_status()
        except ProviderAuthError as exc:
            console.print(f"[red]Authentication status failed:[/red] {escape(str(exc))}")
            return
        account = status.account_label or ("connected" if status.connected else "not connected")
        rows = [
            (
                "connect",
                "Reconnect / switch account" if status.connected else "Connect account",
                "Open the provider sign-in flow in your browser.",
            )
        ]
        if status.connected:
            rows.append(
                (
                    "disconnect",
                    "Disconnect account",
                    "Remove locally stored subscription credentials.",
                )
            )
        rows.append(("back", "Back", "Return to configuration."))
        action = _run_config_picker(
            console=console,
            title="AI Subscription Account",
            subtitle=f"Account: {account}",
            rows=rows,
            current_value="back",
        )
        if action in {None, "back"}:
            return
        try:
            if action == "disconnect":
                if not _prompt_yes_no("Disconnect this AI subscription account? [y/N]"):
                    continue
                result = adapter.logout()
                console.print(
                    f"[yellow]{escape(result.detail or 'Disconnected locally.')}[/yellow]"
                )
                continue
            if status.connected:
                adapter.logout()
            result = adapter.login(
                method="browser",
                output_write=lambda message: console.print(message, highlight=False),
            )
            if not result.connected:
                raise ProviderAuthError(result.detail or "Provider sign-in did not complete.")
            console.print(
                f"[green]Connected:[/green] {escape(result.account_label or provider_id)}"
            )
        except ProviderAuthError as exc:
            console.print(f"[red]Subscription account action failed:[/red] {escape(str(exc))}")


def _sandbox_summary_text(state: ConfigMenuState) -> str:
    mode = normalize_sandbox_mode(state.fields.get("sandbox_mode", "strict"))
    base = {
        "strict": "strict · sandboxed (recommended)",
        "warn": "warn · sandbox, fail-closed if missing",
        "off": "off · host execution (less safe)",
    }[mode]
    if _sandbox_mode_env_override() is not None:
        return f"{base} · overridden by env"
    return base


def _sandbox_mode_env_override() -> str | None:
    for name in ("ALYSIS_SHELL_SANDBOX_MODE", "ALYSIS_VERIFY_SANDBOX_MODE"):
        value = str(env_get(name) or "").strip()
        if value:
            return f"{name}={value}"
    return None


def _print_section_cancelled(console: Console, section_name: str) -> None:
    console.print(f'[dim]Section "{section_name}" cancelled.[/dim]')


def _run_web_search_section(state: ConfigMenuState, console: Console) -> None:
    fields_snapshot = dict(state.fields)
    try:
        policy = _run_config_picker(
            console=console,
            title="Web Search",
            subtitle="When Alysis Code should search before asking the selected model to answer.",
            rows=_web_search_policy_rows(),
            current_value=normalize_web_search_policy(
                state.fields.get("web_search_policy", "auto")
            ),
        )
        if policy is None:
            raise Abort()
        state.set_field("web_search_policy", normalize_web_search_policy(policy))

        mode = _run_config_picker(
            console=console,
            title="Web Search Backend",
            subtitle=(
                "How Alysis Code executes searches. Auto can use model-independent Tavily "
                "when an external search key is configured."
            ),
            rows=_web_search_mode_rows(),
            current_value=_normalize_web_search_mode(state.fields.get("web_search_mode", "auto")),
        )
        if mode is None:
            raise Abort()
        state.set_field("web_search_mode", _normalize_web_search_mode(mode))
    except (Abort, EOFError, KeyboardInterrupt):
        state.fields = fields_snapshot
        _print_section_cancelled(console, "Web Search")
        return
    console.print("[green]Web search settings updated.[/green] Save to apply.")


def _persona_summary_text(state: ConfigMenuState) -> str:
    persona = str(state.fields.get("default_persona") or "code").strip().lower()
    labels = {
        "code": "code · implementation (default)",
        "architect": "architect · plans, writes markdown only",
        "ask": "ask · read-only questions",
        "debug": "debug · reproduce-before-fix",
    }
    return labels.get(persona, persona)


def _run_personas_section(state: ConfigMenuState, console: Console) -> None:
    current = str(state.fields.get("default_persona") or "code").strip().lower()
    rows = [
        ("code", "Code (default)", "Implementation work; keeps your execution mode."),
        ("architect", "Architect", "Plans and designs; writes markdown documents only."),
        ("ask", "Ask", "Read-only questions with inspection tools."),
        ("debug", "Debug", "Reproduce-before-fix; keeps your execution mode."),
    ]
    value = _run_config_picker(
        console=console,
        title="Personas",
        subtitle=(
            "The persona chat starts in. Switch live with /persona or Tab; "
            "per-persona model roles: config set persona_models.<persona> <role>."
        ),
        rows=rows,
        current_value=current,
    )
    if value is None:
        _print_section_cancelled(console, "Personas")
        return
    state.fields["default_persona"] = str(value).strip().lower()


def _run_sandbox_section(state: ConfigMenuState, console: Console) -> None:
    override = _sandbox_mode_env_override()
    if override is not None:
        console.print(
            f"[yellow]Note: {override} is set in your environment and overrides "
            "this config value at runtime.[/yellow]"
        )
    current = normalize_sandbox_mode(state.fields.get("sandbox_mode", "strict"))
    rows = [
        (
            "strict",
            "Strict (recommended)",
            "Run shell & verification commands inside a sandbox (bubblewrap/Docker).",
        ),
        (
            "warn",
            "Warn",
            "Try the sandbox; fail closed if no backend (no host fallback).",
        ),
        (
            "off",
            "Off (less safe)",
            "Run commands directly on the host shell. No isolation.",
        ),
    ]
    value = _run_config_picker(
        console=console,
        title="Sandbox",
        subtitle="How Alysis Code isolates shell and verification commands.",
        rows=rows,
        current_value=current,
    )
    if value is None:
        _print_section_cancelled(console, "Sandbox")
        return
    normalized = normalize_sandbox_mode(value)
    state.fields["sandbox_mode"] = normalized
    if normalized == "off":
        console.print(
            "[yellow]Sandbox set to off - commands will run on the host shell. "
            "Save to apply.[/yellow]"
        )
    else:
        console.print(f"[green]Sandbox mode: {normalized}.[/green] Save to apply.")


def _summary_is_markup(summary: str) -> bool:
    return summary in _WARNING_SUMMARIES


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _role_label(role: str) -> str:
    return str(role).replace("_", " ").capitalize()


def _role_description(role: str) -> str:
    return _ROLE_DESCRIPTIONS.get(str(role), "role-specific work")


def _print_role_explainer(console: Console, *, roles: tuple[str, ...] = ROLE_ORDER) -> None:
    from rich.table import Table

    console.print("[dim]Roles:[/dim]")
    table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)
    table.add_column("role", no_wrap=True, style="dim")
    table.add_column("description", no_wrap=False, style="dim")
    for role in roles:
        table.add_row(role, _role_description(role))
    console.print(table)


def _config_picker_hint(*, row_count: int, allow_save: bool = False) -> str:
    jump_limit = min(max(row_count, 1), 9)
    base = f"↑/↓ navigate  Enter select  1-{jump_limit} jump"
    if allow_save:
        return f"{base}  s save  q exit"
    return f"{base}  Esc back"


def _build_config_picker_panel(
    *,
    title: str,
    subtitle: str,
    rows: list[tuple[str, str, str]],
    selected_value: str | None,
    interactive: bool,
    footer_hint: str | None = None,
) -> Panel:
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    selected = str(selected_value or "").strip().casefold()
    table = Table(show_header=False, box=None, expand=True, padding=(0, 1), collapse_padding=True)
    table.add_column("option", no_wrap=True, ratio=3)
    table.add_column("description", no_wrap=False, ratio=5, overflow="fold")

    for index, (value, label, description) in enumerate(rows, start=1):
        row_selected = str(value).strip().casefold() == selected
        marker = "> " if row_selected else "  "
        option_style = "bold cyan" if row_selected else STYLE_CONTENT
        desc_style = STYLE_CONTENT if row_selected else STYLE_DIM
        table.add_row(
            Text(f"{marker}{index}) {label}", style=option_style),
            Text(str(description or ""), style=desc_style),
        )

    renderables: list[Any] = []
    if str(subtitle or "").strip():
        renderables.append(Text(str(subtitle), style="dim"))
        renderables.append(Text(""))
    renderables.append(table)
    renderables.append(Text(""))
    renderables.append(
        Text(
            footer_hint or _config_picker_hint(row_count=len(rows)),
            style="dim",
        )
    )
    return Panel(
        Group(*renderables),
        title=title,
        border_style="cyan" if interactive else "dim",
    )


def _build_config_top_level_panel(
    *,
    state: ConfigMenuState,
    selected_value: str | None,
    interactive: bool,
    unknown_key_message: str | None = None,
) -> Panel:
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    rows = _top_level_menu_rows(state)
    selected = str(selected_value or "").strip().casefold()
    pending_changes = _pending_change_count(state)

    table = Table(show_header=False, box=None, expand=True, padding=(0, 1), collapse_padding=True)
    table.add_column("option", no_wrap=True, ratio=3)
    table.add_column("summary", no_wrap=False, ratio=5, overflow="fold")
    for index, (value, label, summary) in enumerate(rows, start=1):
        row_selected = str(value).strip().casefold() == selected
        marker = "> " if row_selected else "  "
        option_style = "bold cyan" if row_selected else STYLE_CONTENT
        summary_style = STYLE_CONTENT if row_selected else STYLE_DIM
        summary_text = (
            Text.from_markup(summary)
            if _summary_is_markup(summary)
            else Text(summary, style=summary_style)
        )
        table.add_row(
            Text(f"{marker}{index}) {label}", style=option_style),
            summary_text,
        )

    renderables: list[Any] = []
    if pending_changes > 0:
        renderables.append(Text(f"Pending changes: {pending_changes}", style="yellow"))
    else:
        renderables.append(Text("No pending changes.", style="dim"))
    if state.config_warning:
        renderables.append(Text(str(state.config_warning), style="yellow"))
    renderables.append(Text(""))
    renderables.append(table)
    renderables.append(Text(""))
    renderables.append(Text(_config_picker_hint(row_count=len(rows), allow_save=True), style="dim"))
    if unknown_key_message:
        renderables.append(Text(f"Unknown key: {unknown_key_message}", style="red"))
    return Panel(
        Group(*renderables),
        title="Alysis Code Configuration",
        border_style="cyan" if interactive else "dim",
    )


def _prompt_inline_choice_fallback(
    *,
    console: Console,
    title: str,
    text: str,
    choices: list[tuple[str, str]],
    default: str | None = None,
    prompt_label: str | None = None,
    fallback_reason: str | None = None,
) -> str | None:
    if not choices:
        return None
    error_message: str | None = None
    while True:
        console.print()
        console.print(f"[bold]{title}[/bold]")
        if fallback_reason:
            console.print(f"[dim]{fallback_reason}[/dim]")
            fallback_reason = None
        if error_message:
            console.print(f"[red]{error_message}[/red]")
        if text:
            console.print(f"[dim]{text}[/dim]")
        console.print()
        for index, (value, label) in enumerate(choices, start=1):
            suffix = " [dim](current)[/dim]" if default and value == default else ""
            console.print(f"  {index}) {label}{suffix}")
        footer = prompt_label or f"[1-{len(choices)}] choose  [Enter/q] back"
        console.print()
        console.print(f"[dim]{footer}[/dim]")
        try:
            raw = str(typer.prompt("Choice", default="", show_default=False)).strip()
        except (Abort, EOFError, KeyboardInterrupt):
            console.print("")
            return None
        if not raw:
            return None
        normalized = raw.casefold()
        if normalized in {"q", "quit", "cancel", "c"}:
            return None
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(choices):
                return choices[index][0]
        error_message = "Unknown choice."


def _prompt_inline_choice(
    *,
    console: Console,
    title: str,
    text: str,
    choices: list[tuple[str, str]],
    default: str | None = None,
    prompt_label: str | None = None,
) -> str | None:
    return _prompt_inline_choice_fallback(
        console=console,
        title=title,
        text=text,
        choices=choices,
        default=default,
        prompt_label=prompt_label,
    )


def _prompt_main_action_fallback(console: Console, state: ConfigMenuState) -> str:
    error_message: str | None = None
    while True:
        rows = _top_level_menu_rows(state)
        pending_changes = _pending_change_count(state)
        console.print()
        if error_message:
            console.print(f"[red]{error_message}[/red]")
        console.rule("[bold]Alysis Code Configuration[/bold]")
        if pending_changes > 0:
            console.print(f"[yellow]Pending changes: {pending_changes}[/yellow]")
        else:
            console.print("[dim]No pending changes.[/dim]")
        if state.config_warning:
            console.print(f"[yellow]{escape(str(state.config_warning))}[/yellow]")
        console.print()
        for index, (_value, label, summary) in enumerate(rows, start=1):
            summary_text = (
                summary if _summary_is_markup(summary) else f"[dim]{escape(summary)}[/dim]"
            )
            console.print(f"  {index}) [bold]{label}[/bold]  {summary_text}")
        console.print()
        jump_hint = f"[1-{len(rows)}]"
        if state.dirty:
            console.print(f"[dim]{jump_hint} edit  [s] save  [q] cancel  [Enter] save & exit[/dim]")
        else:
            console.print(f"[dim]{jump_hint} edit  [s] save  [q] cancel  [Enter] cancel[/dim]")
        try:
            raw = str(typer.prompt("Choice", default="", show_default=False)).strip()
        except (Abort, EOFError, KeyboardInterrupt):
            console.print("")
            return "cancel"
        if not raw:
            return "save" if state.dirty else "cancel"
        normalized = raw.casefold()
        if normalized in {"s", "save"}:
            return "save"
        if normalized in {"q", "quit", "cancel", "c"}:
            return "cancel"
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(rows):
                return rows[index][0]
        error_message = "Unknown choice."


def _try_run_config_live_menu(
    *,
    console: Console,
    rows: list[tuple[str, str, str]],
    current_value: str,
    panel_builder: Callable[[str | None, bool], Panel],
    unknown_key_panel_builder: Callable[[str | None, bool, str | None], Panel] | None = None,
    confirm_on_digit: bool = False,
    command_hotkeys: dict[str, str] | None = None,
    accept_right: bool = False,
    too_small_result: str | None = None,
) -> tuple[str | None, bool, str | None]:
    try:
        from ..cli import (
            _clear_terminal_screen,
            _is_non_interactive_terminal,
            _read_input_keys_with_timeout,
            _terminal_too_small,
            _terminal_too_small_panel,
            _watch_terminal_resize,
        )
    except Exception as exc:
        return None, False, str(exc)

    if _is_non_interactive_terminal():
        return None, False, "non-interactive terminal"

    try:
        from prompt_toolkit.input import create_input
        from prompt_toolkit.keys import Keys
        from rich.live import Live
    except Exception as exc:
        return None, False, str(exc)

    if not rows:
        return None, True, None

    if _terminal_too_small():
        console.print(_terminal_too_small_panel())
        return too_small_result, True, None

    selected_index = next(
        (idx for idx, (value, _label, _desc) in enumerate(rows) if value == current_value),
        0,
    )
    if selected_index < 0 or selected_index >= len(rows):
        selected_index = 0

    input_reader: Any | None = None
    try:
        input_reader = create_input()
    except Exception as exc:
        return None, False, str(exc)

    def _selected_value() -> str:
        return rows[selected_index][0]

    last_unknown_key: str | None = None

    def _panel() -> Panel:
        if unknown_key_panel_builder is not None:
            return unknown_key_panel_builder(_selected_value(), True, last_unknown_key)
        return panel_builder(_selected_value(), True)

    def _hotkey_for(key: Any, raw_data: Any) -> str | None:
        if isinstance(raw_data, str) and len(raw_data) == 1 and raw_data.isprintable():
            return raw_data.lower()
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            return key.lower()
        return None

    def _is_escape(key: Any) -> bool:
        return bool(key == Keys.Escape or key == Keys.ControlC or key in {"escape", "c-c"})

    try:
        with _watch_terminal_resize() as consume_resize:
            with Live(
                _panel(),
                console=console,
                auto_refresh=False,
                transient=True,
                screen=False,
            ) as live:
                with input_reader.raw_mode():
                    while True:
                        if _terminal_too_small():
                            console.print(_terminal_too_small_panel())
                            return too_small_result, True, None

                        resized = consume_resize()
                        if resized:
                            _clear_terminal_screen(console=console)
                            live.update(_panel(), refresh=True)

                        key_presses = _read_input_keys_with_timeout(
                            input_reader=input_reader,
                            timeout_s=0.12,
                        )
                        if not key_presses:
                            continue

                        panel_updated = False
                        for key_press in key_presses:
                            key = key_press.key
                            raw_data = key_press.data

                            if key == Keys.Up or key == "up":
                                last_unknown_key = None
                                selected_index = (selected_index - 1) % len(rows)
                                panel_updated = True
                                continue
                            if key == Keys.Down or key == "down":
                                last_unknown_key = None
                                selected_index = (selected_index + 1) % len(rows)
                                panel_updated = True
                                continue

                            digit_key: str | None = None
                            if isinstance(key, str) and key.isdigit():
                                digit_key = key
                            elif isinstance(raw_data, str) and raw_data.isdigit():
                                digit_key = raw_data
                            if digit_key is not None:
                                last_unknown_key = None
                                idx = int(digit_key) - 1
                                if 0 <= idx < len(rows):
                                    selected_index = idx
                                    if confirm_on_digit:
                                        return _selected_value(), True, None
                                    panel_updated = True
                                continue

                            if accept_right and (key == Keys.Right or key == "right"):
                                return _selected_value(), True, None

                            if (
                                key == Keys.Enter
                                or key == Keys.ControlM
                                or raw_data in {chr(13), chr(10)}
                            ):
                                return _selected_value(), True, None

                            if _is_escape(key):
                                if command_hotkeys and "escape" in command_hotkeys:
                                    return command_hotkeys["escape"], True, None
                                return None, True, None

                            hotkey = _hotkey_for(key, raw_data)
                            if hotkey and command_hotkeys and hotkey in command_hotkeys:
                                return command_hotkeys[hotkey], True, None
                            if (
                                hotkey
                                and not hotkey.isdigit()
                                and unknown_key_panel_builder is not None
                            ):
                                last_unknown_key = hotkey
                                panel_updated = True

                        if panel_updated:
                            live.update(_panel(), refresh=True)
    except Exception as exc:
        return None, False, str(exc)
    finally:
        if input_reader is not None:
            close = getattr(input_reader, "close", None)
            if callable(close):
                close()

    return None, True, None


def _run_config_picker(
    *,
    console: Console,
    title: str,
    subtitle: str,
    rows: list[tuple[str, str, str]],
    current_value: str,
    footer_hint: str | None = None,
) -> str | None:
    selected_value, interactive_available, reason = _try_run_config_live_menu(
        console=console,
        rows=rows,
        current_value=current_value,
        panel_builder=lambda selected, interactive: _build_config_picker_panel(
            title=title,
            subtitle=subtitle,
            rows=rows,
            selected_value=selected,
            interactive=interactive,
            footer_hint=footer_hint,
        ),
        confirm_on_digit=True,
        too_small_result=None,
    )
    if interactive_available:
        return selected_value

    detail = reason or "unknown reason"
    console.print(f"[dim]Interactive picker unavailable: {detail}. Using numeric input.[/dim]")
    return _prompt_inline_choice(
        console=console,
        title=title,
        text=subtitle,
        choices=[(value, label) for value, label, _description in rows],
        default=current_value,
        prompt_label=footer_hint,
    )


def _run_config_top_level(*, state: ConfigMenuState, console: Console) -> str:
    rows = _top_level_menu_rows(state)
    current_value = rows[0][0] if rows else "cancel"
    selected_value, interactive_available, reason = _try_run_config_live_menu(
        console=console,
        rows=rows,
        current_value=current_value,
        panel_builder=lambda selected, interactive: _build_config_top_level_panel(
            state=state,
            selected_value=selected,
            interactive=interactive,
        ),
        unknown_key_panel_builder=lambda selected, interactive, unknown_key: (
            _build_config_top_level_panel(
                state=state,
                selected_value=selected,
                interactive=interactive,
                unknown_key_message=unknown_key,
            )
        ),
        confirm_on_digit=True,
        command_hotkeys={
            "s": "save",
            "q": "cancel",
            "escape": "cancel",
        },
        accept_right=True,
        too_small_result="cancel",
    )
    if interactive_available and selected_value is not None:
        return selected_value
    if interactive_available:
        return "cancel"

    detail = reason or "unknown reason"
    console.print(f"[dim]Interactive picker unavailable: {detail}. Using numeric input.[/dim]")
    return _prompt_main_action_fallback(console, state)


def _prompt_main_action(console: Console, state: ConfigMenuState) -> str:
    return _run_config_top_level(state=state, console=console)


def _run_provider_section(state: ConfigMenuState, console: Console) -> None:
    console.print()
    console.rule("[bold]Provider Profile[/bold]")
    if state.execution_backend == "delegated" and not _is_direct_subscription_id(
        state.execution_runtime
    ):
        console.print(
            "[yellow]API-key provider settings are preserved but inactive while an AI subscription is selected.[/yellow]"
        )
    console.print(
        "[dim]A profile bundles provider URL, headers, and the API key env variable. "
        "The active profile is what Alysis Code uses for all model calls.[/dim]"
    )
    for profile_name in sorted(state.profiles):
        marker = "active" if profile_name == state.active_profile else ""
        profile = ProfileSpec.from_dict(profile_name, state.profiles[profile_name])
        console.print(f"{profile.name}: {display_endpoint(profile.base_url)} {marker}".rstrip())
    try:
        action = _run_config_picker(
            console=console,
            title="Provider Profile",
            subtitle=f"Active profile: {state.active_profile or '(none)'}",
            rows=[
                ("switch", "Switch active profile", "Choose another configured profile."),
                ("add_preset", "Add from preset", "Pick a known provider (OpenAI, Anthropic, ...)"),
                ("add_custom", "Add custom", "Use any OpenAI-compatible base URL"),
                (
                    "edit",
                    "Edit current",
                    "Change URL, model, trace adapter, headers, and notes",
                ),
                ("remove", "Remove", "Delete a profile (applied on save)"),
                ("back", "Back", "Return to the menu"),
            ],
            current_value="switch" if state.profiles else "add_preset",
        )
        if action in {None, "back"}:
            return
        if action == "switch":
            _run_profile_switch(state, console)
        elif action == "add_preset":
            _run_profile_add_preset(state, console)
        elif action == "add_custom":
            _run_profile_add_custom(state, console)
        elif action == "edit":
            _run_profile_edit_current(state, console)
        elif action == "remove":
            _run_profile_remove(state, console)
    except (Abort, EOFError, KeyboardInterrupt):
        console.print("")
        _print_section_cancelled(console, "Provider Profile")
        return
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return


def _run_profile_switch(state: ConfigMenuState, console: Console) -> None:
    if not state.profiles:
        return
    rows = [(name, name, "") for name in sorted(state.profiles)]
    selected = _run_config_picker(
        console=console,
        title="Provider Profile",
        subtitle=f"Active: {state.active_profile or 'none'}",
        rows=rows,
        current_value=state.active_profile or rows[0][0],
    )
    if selected:
        state.set_active_profile_name(selected)
        staged_profile = state.staged_api_key_target_profile()
        if staged_profile and staged_profile != selected:
            console.print(
                f"[yellow]The unsaved API key remains bound to profile "
                f"{escape(staged_profile)}.[/yellow]"
            )
        if (
            state.clear_stored_key_confirmed
            and state.clear_stored_key_profile
            and state.clear_stored_key_profile != selected
        ):
            console.print(
                f"[yellow]Stored-key removal remains bound to profile "
                f"{escape(state.clear_stored_key_profile)}.[/yellow]"
            )
        _print_state_provider_diagnostic_warnings(state, console)


def _run_profile_add_preset(state: ConfigMenuState, console: Console) -> None:
    rows = _provider_picker_rows(_ordered_profile_presets_for_setup())
    selected = _run_config_picker(
        console=console,
        title="Provider Profile",
        subtitle="Pick a provider preset",
        rows=rows,
        current_value=rows[0][0] if rows else "openai-responses",
    )
    if selected == _ADVANCED_PROVIDER_PRESETS_VALUE:
        advanced_rows = [
            (preset.key, preset_selection_label(preset), _preset_description(preset))
            for preset in _advanced_profile_presets_for_setup()
        ]
        selected = _run_config_picker(
            console=console,
            title="Advanced Provider Profile",
            subtitle="Pick a local, custom, or legacy OpenAI-compatible provider preset",
            rows=advanced_rows,
            current_value=advanced_rows[0][0] if advanced_rows else "openai",
        )
    if not selected:
        return
    preset = next(preset for preset in PROFILE_PRESETS if preset.key == selected)
    profile_name = _prompt_text("Profile name", preset.key)
    profile = make_profile_from_preset(preset, name=profile_name)
    if not profile.base_url:
        base_url = _prompt_text("Base URL", "")
        profile = ProfileSpec(
            name=profile.name,
            protocol=profile.protocol,
            base_url=base_url,
            api_key_env=profile.api_key_env,
            auth_provider=profile.auth_provider,
            extra_headers=profile.extra_headers,
            default_model=profile.default_model,
            reasoning_effort=profile.reasoning_effort,
            web_search_adapter=profile.web_search_adapter,
            web_search_model=profile.web_search_model,
            notes=profile.notes,
            cache_capability=profile.cache_capability,
            reasoning_trace_adapter=profile.reasoning_trace_adapter,
        )
    state.add_profile_spec(profile)
    console.print(f"[green]Profile {profile.name} added.[/green]")
    _print_preset_warning(console, preset)
    _print_state_provider_diagnostic_warnings(state, console)


def _preset_description(preset: ProfilePreset) -> str:
    prefix = preset_protocol_summary(preset)
    if preset.notes:
        return f"{prefix}. {preset.notes}"
    parsed = urlparse(preset.base_url)
    if parsed.netloc:
        return f"{prefix}. Host: {parsed.netloc}"
    return f"{prefix}. Use any OpenAI-compatible base URL"


def _ordered_profile_presets_for_setup() -> list[ProfilePreset]:
    return provider_selection_presets()


def _advanced_profile_presets_for_setup() -> list[ProfilePreset]:
    return advanced_provider_selection_presets()


def _provider_picker_rows(presets: list[ProfilePreset]) -> list[tuple[str, str, str]]:
    rows = [
        (preset.key, preset_selection_label(preset), _preset_description(preset))
        for preset in presets
    ]
    rows.append(
        (
            _ADVANCED_PROVIDER_PRESETS_VALUE,
            "Advanced / local / compatibility providers",
            "Show local endpoints (Ollama, LM Studio, vLLM), custom URLs, and legacy "
            "OpenAI-compatible first-party presets.",
        )
    )
    return rows


def _print_preset_warning(console: Console, preset: ProfilePreset) -> None:
    warning = str(preset.setup_warning or "").strip()
    if warning:
        console.print(f"[yellow]{warning}[/yellow]")


def _run_profile_add_custom(state: ConfigMenuState, console: Console) -> None:
    name = _prompt_text("Profile name", "custom")
    base_url = _prompt_text("Base URL", "")
    headers = _parse_header_text(_prompt_text("Extra headers (k=v, comma-separated)", ""))
    profile = ProfileSpec(
        name=name.lower(),
        protocol="openai_compat",
        base_url=base_url,
        extra_headers=headers,
        web_search_adapter="auto",
        web_search_model="",
        notes="Custom OpenAI-compatible endpoint.",
    )
    state.add_profile_spec(profile)
    console.print(f"[green]Profile {profile.name} added.[/green]")
    _print_state_provider_diagnostic_warnings(state, console)


def _prompt_web_search_adapter(console: Console, current: str) -> str:
    for _attempt in range(3):
        value = _prompt_non_secret_text(
            console,
            "Web search adapter",
            current,
            "Web search adapter",
        )
        try:
            return normalize_web_search_adapter(value or "auto")
        except (ConfigError, ValueError) as e:
            allowed = ", ".join(WEB_SEARCH_ADAPTER_CHOICES)
            console.print(f"[red]{e}[/red]")
            console.print(f"[dim]Allowed adapters: {allowed}[/dim]")
    return normalize_web_search_adapter(current or "auto")


def _prompt_reasoning_trace_adapter(
    console: Console,
    *,
    protocol: str,
    current: str,
) -> str:
    for _attempt in range(3):
        value = _prompt_non_secret_text(
            console,
            "Reasoning trace adapter",
            current,
            "Reasoning trace adapter",
        )
        try:
            return validate_reasoning_trace_adapter_for_protocol(
                protocol=protocol,
                adapter=value or "auto",
            )
        except (ConfigError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
    return validate_reasoning_trace_adapter_for_protocol(
        protocol=protocol,
        adapter=current or "auto",
    )


def _run_profile_edit_current(state: ConfigMenuState, console: Console) -> None:
    if not state.active_profile or state.active_profile not in state.profiles:
        console.print(
            "[yellow]No active profile selected. Switch to one first or add a new profile.[/yellow]"
        )
        return
    profile = ProfileSpec.from_dict(state.active_profile, state.profiles[state.active_profile])
    if profile.auth_provider or _active_alysis_profile(state) is not None:
        console.print(
            "[yellow]This subscription connection is provider-managed. Use Default Model "
            "to choose its model and reasoning effort.[/yellow]"
        )
        return
    console.print(f"[dim]Protocol:[/dim] {profile.protocol} [dim](advanced/read-only)[/dim]")
    console.print(_build_profile_edit_current_panel(profile.name))
    console.print(
        "[dim]Note: the API key itself is stored separately (encrypted in keyring). "
        "This field is only the name of the env variable Alysis Code reads as a fallback.[/dim]"
    )
    base_url = _prompt_non_secret_text(console, "Base URL", profile.base_url, "Base URL")
    api_key_env = _prompt_non_secret_text(
        console, _API_KEY_ENV_PROMPT, profile.api_key_env or "", _API_KEY_ENV_FIELD_NAME
    )
    default_model = _prompt_non_secret_text(
        console, "Default model", profile.default_model, "Default model"
    )
    reasoning_trace_adapter = _prompt_reasoning_trace_adapter(
        console,
        protocol=profile.protocol,
        current=profile.reasoning_trace_adapter,
    )
    web_search_adapter = _prompt_web_search_adapter(console, profile.web_search_adapter)
    web_search_model = _prompt_non_secret_text(
        console,
        "Web search model",
        profile.web_search_model,
        "Web search model",
    )
    headers_default = ", ".join(f"{key}={value}" for key, value in profile.extra_headers.items())
    headers = _parse_header_text(
        _prompt_non_secret_text(console, "Extra headers", headers_default, "Extra headers")
    )
    notes = _prompt_non_secret_text(console, "Notes", profile.notes, "Notes")
    state.update_active_profile_spec(
        base_url=base_url,
        api_key_env=api_key_env or None,
        default_model=default_model,
        reasoning_trace_adapter=reasoning_trace_adapter,
        web_search_adapter=web_search_adapter or "auto",
        web_search_model=web_search_model,
        extra_headers=headers,
        notes=notes,
    )
    _print_state_provider_diagnostic_warnings(state, console)


def _print_state_provider_diagnostic_warnings(
    state: ConfigMenuState,
    console: Console,
) -> None:
    try:
        issues = provider_diagnostic_warning_lines(state._resolution_cfg())
    except ConfigError:
        return
    for issue in issues:
        console.print(f"[yellow]Provider diagnostic: {issue}[/yellow]")


def _build_profile_edit_current_panel(profile_name: str) -> Panel:
    from rich.console import Group
    from rich.text import Text

    return Panel(
        Group(
            Text("Editable here:", style=STYLE_EMPHASIS),
            Text("  Base URL", style="dim"),
            Text("  API key env var NAME", style="dim"),
            Text("  Default model", style="dim"),
            Text("  Reasoning trace adapter", style="dim"),
            Text("  Web search adapter", style="dim"),
            Text("  Web search model", style="dim"),
            Text("  Extra headers", style="dim"),
            Text("  Notes", style="dim"),
            Text(""),
            Text("Advanced/read-only here:", style=STYLE_EMPHASIS),
            Text(
                "  Protocol (use `alysis profile convert --to native|compatibility`)",
                style="dim",
            ),
            Text(""),
            Text("Stored separately:", style=STYLE_EMPHASIS),
            Text("  The actual API key", style="dim"),
            Text("   (use the API Key section to update)", style="dim"),
        ),
        title=f"Edit Profile: {profile_name}",
        border_style="cyan",
    )


def _looks_like_secret(value: str) -> bool:
    """Conservative pattern check for accidentally-pasted secrets."""
    text = str(value or "").strip()
    if len(text) < 16:
        return False
    if text.startswith(("sk-", "sk_", "anthropic-", "Bearer ", "ghp_", "gho_", "github_pat_")):
        return True
    if len(text) >= 40 and all(c.isalnum() or c in "_-" for c in text):
        return True
    return False


def _run_profile_remove(state: ConfigMenuState, console: Console) -> None:
    if not state.profiles:
        return
    rows = [(name, name, "") for name in sorted(state.profiles)]
    selected = _run_config_picker(
        console=console,
        title="Provider Profile",
        subtitle="Pick a profile to remove (applied on save)",
        rows=rows,
        current_value=state.active_profile or rows[0][0],
    )
    if selected and _prompt_yes_no(f"Remove profile {selected}? [y/N]"):
        staged_key_discarded = state.remove_profile_name(selected)
        console.print(f"[yellow]Profile {selected} will be removed on save.[/yellow]")
        if staged_key_discarded:
            console.print("[yellow]Its unsaved API key was discarded.[/yellow]")


def _run_api_key_section(state: ConfigMenuState, console: Console) -> None:
    if _active_alysis_profile(state) is not None:
        _run_alysis_account_section(state, console)
        return
    console.print()
    console.rule("[bold]API Key[/bold]")
    if state.execution_backend == "delegated":
        console.print(
            "[yellow]This API key is preserved separately while an AI subscription is selected.[/yellow]"
        )
    console.print("[dim]Used by the active profile for all model calls.[/dim]")
    console.print(f"Stored: {state.masked_api_key} · source: {state.api_key_source}")
    try:
        value = str(
            typer.prompt(
                'New API key (Enter to keep current, "clear" to remove)',
                default="",
                hide_input=True,
                show_default=False,
            )
        )
    except (Abort, EOFError, KeyboardInterrupt):
        console.print("")
        _print_section_cancelled(console, "API Key")
        return
    if value.strip().casefold() == "clear":
        try:
            if _prompt_yes_no("Clear the stored API key? [y/N]"):
                state.mark_clear_stored_key_confirmed()
                console.print("[yellow]Stored API key will be cleared on save.[/yellow]")
        except (Abort, EOFError, KeyboardInterrupt):
            console.print("")
            _print_section_cancelled(console, "API Key")
            return
    elif value.strip():
        state.set_field("new_api_key", value.strip())


def _run_default_section(state: ConfigMenuState, console: Console) -> None:
    console.print()
    console.rule("[bold]Default Model[/bold]")
    direct_subscription = _active_subscription_profile(state) is not None
    hosted_account = _active_alysis_profile(state) is not None
    if state.execution_backend == "delegated" and not direct_subscription:
        console.print(
            "[yellow]This API-key model is preserved but inactive while an AI subscription is selected.[/yellow]"
        )
    console.print(
        "[dim]Used when you chat with the agent. Subagents and Forge roles fall back "
        "to this model unless overridden in the sections below.[/dim]"
    )
    try:
        model = _prompt_default_model(console, state)
        base_url = (
            state.fields["base_url"]
            if direct_subscription or hosted_account
            else _prompt_text("Base URL", state.fields["base_url"])
        )
        state.set_field("base_url", base_url)
        thinking_label = _prompt_thinking_label(
            console,
            state.thinking_label,
            labels=_thinking_labels_for_state(state, model=model),
        )
    except (Abort, EOFError, KeyboardInterrupt):
        console.print("")
        _print_section_cancelled(console, "Default Model")
        return

    state.set_field("model", model)
    state.set_thinking_label(thinking_label)
    if direct_subscription:
        console.print("[dim]Model and reasoning choices came from your subscription account.[/dim]")
    else:
        console.print(
            "[dim]Reasoning effort. Some providers ignore this until they add native "
            "reasoning support.[/dim]"
        )


def _run_request_timeout_section(state: ConfigMenuState, console: Console) -> None:
    try:
        timeout = _prompt_positive_float_text(
            console, "Request timeout (seconds)", state.fields["llm_timeout_s"]
        )
    except (Abort, EOFError, KeyboardInterrupt):
        _print_section_cancelled(console, "Request timeout")
        return
    state.set_field("llm_timeout_s", timeout)


def _run_cache_section(state: ConfigMenuState, console: Console) -> None:
    console.print()
    console.rule("[bold]Context & Cache[/bold]")
    console.print(
        "[dim]Controls provider prompt caching for repeated system/context payloads. "
        "Auto mode derives safe provider-aware cache settings when the active API supports them.[/dim]"
    )
    policy, policy_error = _resolved_cache_policy_with_error_for_state(state)
    console.print(
        "[dim]Effective cache policy: "
        + escape(
            _cache_policy_summary_text(
                policy,
                compact=False,
                error=policy_error,
            )
        )
        + "[/dim]"
    )
    fields_snapshot = dict(state.fields)
    try:
        mode = _prompt_prompt_cache_mode(console, state.fields["prompt_cache_mode"])
        state.set_field("prompt_cache_mode", mode)
        state.set_field(
            "prompt_cache_key",
            _prompt_optional_config_text(
                console,
                "Manual cache key",
                state.fields["prompt_cache_key"],
                "manual cache key",
            ),
        )
        state.set_field(
            "prompt_cache_retention",
            _prompt_optional_config_text(
                console,
                "Manual cache retention",
                state.fields["prompt_cache_retention"],
                "manual cache retention",
            ),
        )
        state.set_field(
            "anthropic_prompt_cache_enabled",
            _prompt_anthropic_prompt_cache_enabled(
                console,
                state.fields["anthropic_prompt_cache_enabled"],
            ),
        )
        state.set_field(
            "anthropic_prompt_cache_ttl",
            _prompt_anthropic_prompt_cache_ttl(
                console,
                state.fields["anthropic_prompt_cache_ttl"],
            ),
        )
        state.set_field(
            "cache_aware_compaction",
            _prompt_cache_aware_compaction_enabled(
                console,
                state.fields["cache_aware_compaction"],
            ),
        )
        state.set_field(
            "cache_aware_min_trigger_ratio",
            _prompt_cache_aware_min_trigger_ratio(
                console,
                state.fields["cache_aware_min_trigger_ratio"],
            ),
        )
    except (Abort, EOFError, KeyboardInterrupt):
        state.fields = dict(fields_snapshot)
        console.print("")
        _print_section_cancelled(console, "Context & Cache")
        return
    console.print("[green]Context/cache settings updated.[/green] Save to apply.")


def _prompt_prompt_cache_mode(console: Console, current: str) -> str:
    rows = [
        (
            "auto",
            "Auto",
            "Derive provider-aware cache settings when the active API supports caching.",
        ),
        (
            "manual",
            "Manual",
            "Use the manual cache key/retention below for compatible providers.",
        ),
        ("off", "Off", "Disable prompt caching for all providers."),
    ]
    value = _run_config_picker(
        console=console,
        title="Context & Cache",
        subtitle="Prompt cache mode.",
        rows=rows,
        current_value=_normalize_prompt_cache_mode(current),
    )
    if value is None:
        raise Abort()
    return _normalize_prompt_cache_mode(value)


def _prompt_anthropic_prompt_cache_enabled(console: Console, current: str) -> str:
    rows = [
        ("true", "On", "Enable Anthropic cache_control in manual mode."),
        ("false", "Off", "Only auto mode enables Anthropic cache_control."),
    ]
    value = _run_config_picker(
        console=console,
        title="Context & Cache",
        subtitle="Anthropic cache_control override.",
        rows=rows,
        current_value="true"
        if _normalize_bool_text(current, label="Anthropic prompt cache")
        else "false",
    )
    if value is None:
        raise Abort()
    return "true" if _normalize_bool_text(value, label="Anthropic prompt cache") else "false"


def _prompt_anthropic_prompt_cache_ttl(console: Console, current: str) -> str:
    rows = [
        ("5m", "5 minutes", "Lower cache lifetime; safest default."),
        ("1h", "1 hour", "Longer lifetime for stable long-running coding sessions."),
    ]
    value = _run_config_picker(
        console=console,
        title="Context & Cache",
        subtitle="Anthropic cache_control TTL.",
        rows=rows,
        current_value=_normalize_anthropic_prompt_cache_ttl(current),
    )
    if value is None:
        raise Abort()
    return _normalize_anthropic_prompt_cache_ttl(value)


def _prompt_cache_aware_compaction_enabled(console: Console, current: str) -> str:
    rows = [
        ("true", "On", "Compact earlier when the next request is likely to miss provider cache."),
        ("false", "Off", "Use only the normal compaction trigger ratio."),
    ]
    value = _run_config_picker(
        console=console,
        title="Context & Cache",
        subtitle="Cache-aware compaction trigger.",
        rows=rows,
        current_value="true"
        if _normalize_bool_text(current, label="Cache-aware compaction")
        else "false",
    )
    if value is None:
        raise Abort()
    return "true" if _normalize_bool_text(value, label="Cache-aware compaction") else "false"


def _prompt_cache_aware_min_trigger_ratio(console: Console, current: str) -> str:
    for _attempt in range(3):
        value = _prompt_text("Cache-aware min trigger ratio", current)
        try:
            return _normalize_cache_aware_min_trigger_ratio(value)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
    return _normalize_cache_aware_min_trigger_ratio(current)


def _prompt_default_model(console: Console, state: ConfigMenuState) -> str:
    rows = _default_model_rows(state)
    if state._provider_model_catalog_warning:
        console.print(f"[yellow]{state._provider_model_catalog_warning}[/yellow]")
    current_model = str(state.fields.get("model") or "").strip()
    row_values = {value for value, _label, _description in rows}
    current_value = current_model if current_model in row_values else rows[0][0]
    selected = _run_config_picker(
        console=console,
        title="Default Model",
        subtitle=_default_model_picker_subtitle(state),
        rows=rows,
        current_value=current_value,
    )
    if selected is None:
        raise Abort()
    if selected == _CUSTOM_MODEL_VALUE:
        return _prompt_text("Model", current_model)
    return selected


def _default_model_picker_subtitle(state: ConfigMenuState) -> str:
    if _active_subscription_profile(state) is not None:
        return "Pick a model for your connected subscription. The provider checks access when used."
    preset = _active_preset(state)
    if preset is None:
        return "Pick a provider-advertised model, or type a custom model ID."
    return f"Pick a supported model for {preset.label}."


def _provider_models_for_state(
    state: ConfigMenuState,
    preset: ProfilePreset | None,
) -> tuple[ProviderModelOption, ...]:
    if preset is not None and preset.key not in {"alysis", "nvidia", "custom"}:
        state._provider_model_catalog_warning = ""
        return ()
    try:
        profile_data = state.profiles[state.active_profile]
        profile = ProfileSpec.from_dict(state.active_profile, profile_data)
    except (KeyError, ConfigError):
        state._provider_model_catalog_warning = ""
        return ()
    if profile.auth_provider:
        state._provider_model_catalog_warning = ""
        return ()
    api_key = state.staged_api_key_for_active_profile()
    if not api_key:
        try:
            api_key = resolve_api_key(state._resolution_cfg()).key
        except ConfigError:
            api_key = ""
    identity = (
        profile.name,
        profile.protocol,
        str(profile.base_url or "").strip().rstrip("/"),
        credential_scope_fingerprint(api_key),
        credential_scope_fingerprint(tuple(sorted(profile.extra_headers.items()))),
    )
    if state._provider_models_cache_identity == identity:
        return state._provider_models_cache
    if not api_key and preset is not None and preset.key == "alysis":
        state._provider_model_catalog_warning = ""
        return ()
    if not api_key and preset is not None and preset.key == "nvidia":
        state._provider_models_cache_identity = identity
        state._provider_models_cache = ()
        state._provider_model_catalog_warning = (
            "Add the NVIDIA API key to load its live hosted-model catalog."
        )
        return ()
    try:
        options = discover_provider_models(profile=profile, api_key=api_key)
    except ProviderModelCatalogError:
        options = ()
        provider_label = (
            "NVIDIA's" if preset is not None and preset.key == "nvidia" else "The provider's"
        )
        state._provider_model_catalog_warning = (
            f"{provider_label} live model catalog is unavailable; recommended and custom models "
            "remain available."
        )
        if preset is not None and preset.key == "alysis":
            state._provider_model_catalog_warning = (
                "The hosted catalog is unavailable; only free models can be selected."
            )
    else:
        state._provider_model_catalog_warning = ""
    state._provider_models_cache_identity = identity
    state._provider_models_cache = tuple(sorted(options, key=lambda item: item.id.casefold()))
    return state._provider_models_cache


def _provider_model_option_rows(
    state: ConfigMenuState, preset: ProfilePreset | None
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    is_nvidia = preset is not None and preset.key == "nvidia"
    catalog_label = "live NVIDIA catalog" if is_nvidia else "live provider catalog"
    for option in _provider_models_for_state(state, preset):
        description = str(option.description or "").strip()
        if not description:
            owner = option.id.partition("/")[0]
            description = (
                f"{catalog_label} · hosted model from {owner}"
                if is_nvidia and owner and owner != option.id
                else catalog_label
            )
        label = option.label or option.id
        if option.chat_compatibility == "unknown":
            label = f"{label} (chat compatibility unverified)"
            description = (
                f"{description} · provider catalog does not declare endpoint compatibility; "
                "verify this model before relying on it"
            )
        rows.append((option.id, label, description))
    return rows


def _preset_model_option_rows(
    preset: ProfilePreset,
    *,
    state: ConfigMenuState | None = None,
) -> list[tuple[str, str, str]]:
    """Model picker rows for a preset, widened with provider-advertised live models.

    NVIDIA's API catalog contains both NVIDIA and third-party models, so its live
    ``/v1/models`` inventory is merged with a small curated recommendation set.
    Alysis adds reviewed paid models only when its authenticated catalog permits
    them. Discovery for NVIDIA is best-effort and
    offline-safe: on failure the static rows still render.
    """
    if preset.key == "alysis":
        available = (
            {model.id for model in _provider_models_for_state(state, preset)}
            if state is not None
            else set()
        )
        return list(model_options_for_preset(preset, available_models=available))

    rows = list(model_options_for_preset(preset))

    if preset.key == "nvidia" and state is not None:
        known = {value for value, _label, _description in rows}
        for row in _provider_model_option_rows(state, preset):
            if row[0] in known:
                continue
            known.add(row[0])
            rows.append(row)
        return rows

    return rows


def _default_model_rows(state: ConfigMenuState) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    current_model = str(state.fields.get("model") or "").strip()

    if _active_subscription_profile(state) is not None:
        state._provider_model_catalog_warning = ""
        subscription_models = _subscription_models_for_state(state)
        for model in subscription_models:
            if model.id in seen:
                continue
            seen.add(model.id)
            efforts = ", ".join(effort.id for effort in model.reasoning_efforts)
            description = model.description
            if efforts:
                description = f"{description} · reasoning: {efforts}" if description else efforts
            rows.append((model.id, model.label, description))
        if not rows and current_model:
            rows.append(
                (
                    current_model,
                    current_model,
                    "saved model · live subscription catalog unavailable",
                )
            )
        return rows

    preset = _active_preset(state)
    if preset is not None and preset.key not in {"nvidia", "custom"}:
        state._provider_model_catalog_warning = ""
    if preset is not None and preset.key == "alysis":
        # Saved or manually entered IDs cannot expand hosted account access.
        return _preset_model_option_rows(preset, state=state)

    if current_model:
        rows.append((current_model, current_model, "current configured model"))
        seen.add(current_model)

    custom_added = False
    if preset is not None:
        preset_rows = _preset_model_option_rows(preset, state=state)
        curated_count = len(model_options_for_preset(preset))
        for index, (value, label, description) in enumerate(preset_rows):
            if preset.key == "nvidia" and index == curated_count and not custom_added:
                rows.append(
                    (
                        _CUSTOM_MODEL_VALUE,
                        "Type a custom model name",
                        "Use any model supported by the active provider",
                    )
                )
                custom_added = True
            if value in seen:
                continue
            seen.add(value)
            rows.append((value, label, description))

    if not custom_added:
        rows.append(
            (
                _CUSTOM_MODEL_VALUE,
                "Type a custom model name",
                "Use any model supported by the active provider",
            )
        )
    if preset is None or preset.key == "custom":
        for row in _provider_model_option_rows(state, preset):
            if row[0] not in seen:
                seen.add(row[0])
                rows.append(row)
    return rows


def _active_preset(state: ConfigMenuState) -> ProfilePreset | None:
    if _active_alysis_profile(state) is not None:
        # Managed gateway overrides still use the Alysis model offering.
        return next(preset for preset in PROFILE_PRESETS if preset.key == "alysis")
    if state.active_profile and state.active_profile in state.profiles:
        profile = ProfileSpec.from_dict(state.active_profile, state.profiles[state.active_profile])
        preset = find_preset_for_profile(profile)
        if preset is not None:
            state_base_url = str(state.fields.get("base_url", "") or "").strip().rstrip("/")
            profile_base_url = str(profile.base_url or "").strip().rstrip("/")
            if not state_base_url or state_base_url == profile_base_url:
                return preset
            base_url_preset = find_preset_for_base_url(state_base_url)
            return base_url_preset or preset

    return find_preset_for_base_url(state.fields.get("base_url", ""))


def _run_subagent_section(state: ConfigMenuState, console: Console) -> None:
    console.print()
    console.rule("[bold]Subagent model overrides[/bold]")
    console.print(
        "[dim]Override the model used by the agent's internal roles. Leave empty to inherit "
        "the default model.[/dim]"
    )
    temperature_controls_available = _temperature_controls_available(state)
    if not temperature_controls_available:
        console.print(
            "[dim]Temperature is managed by the active AI subscription. Choose the model "
            "and reasoning effort in Default Model instead.[/dim]"
        )
    _print_role_explainer(console, roles=ROLE_ORDER)
    role_model_snapshot = dict(state.role_models)
    role_temp_snapshot = dict(state.role_temperatures)
    try:
        for role in ROLE_ORDER:
            role_label = _role_label(role)
            model = _prompt_override_text(
                f"  {role_label} model (Enter to skip)",
                state.role_models.get(role, ""),
            )
            state.set_role_model(role, model)
            if temperature_controls_available and role in _ROLE_TEMPERATURE_FIELDS:
                temperature = _prompt_override_text(
                    f"  {role_label} temperature (Enter to skip)",
                    state.role_temperatures.get(role, ""),
                )
                state.set_role_temperature(role, temperature)
    except (Abort, EOFError, KeyboardInterrupt):
        state.role_models = dict(role_model_snapshot)
        state.role_temperatures = dict(role_temp_snapshot)
        console.print("")
        _print_section_cancelled(console, "Subagent model overrides")
        return

    overrides = _non_empty_role_values(state.role_models, roles=ROLE_ORDER)
    if overrides:
        console.print(f"[dim]{_pluralize(len(overrides), 'override')} set.[/dim]")
    else:
        console.print("[dim]No subagent model overrides set.[/dim]")


def _run_forge_section(state: ConfigMenuState, console: Console) -> None:
    console.print()
    console.rule("[bold]Forge model overrides[/bold]")
    console.print(
        "[dim]Override the model used by Forge swarm roles. Leave empty to inherit the "
        "subagent override, then the default model.[/dim]"
    )
    _print_role_explainer(console, roles=FORGE_ROLE_ORDER)
    role_model_snapshot = dict(state.forge_role_models)
    try:
        for role in FORGE_ROLE_ORDER:
            model = _prompt_override_text(
                f"  {_role_label(role)} model (Enter to skip)",
                state.forge_role_models.get(role, ""),
            )
            state.set_forge_role_model(role, model)
    except (Abort, EOFError, KeyboardInterrupt):
        state.forge_role_models = dict(role_model_snapshot)
        console.print("")
        _print_section_cancelled(console, "Forge model overrides")
        return

    overrides = _non_empty_role_values(state.forge_role_models, roles=FORGE_ROLE_ORDER)
    if overrides:
        console.print(f"[dim]{_pluralize(len(overrides), 'override')} set.[/dim]")
    else:
        console.print("[dim]No forge model overrides set.[/dim]")


def _save_and_exit(
    state: ConfigMenuState,
    cfg: AppConfig,
    console: Console,
) -> ConfigMenuResult:
    result = state.commit_to(cfg)
    if not result.saved:
        console.print(f"[red]{result.error or 'Config validation failed.'}[/red]")
        return result
    try:
        save_config(cfg)
        if state.new_api_key.strip():
            key_profile = state.staged_api_key_target_profile()
            if key_profile:
                save_persisted_profile_key(key_profile, state.new_api_key.strip())
            else:
                save_persisted_api_key(state.new_api_key.strip())
        if state.clear_stored_key_confirmed:
            clear_profile = state.clear_stored_key_profile or state.active_profile
            if clear_profile:
                clear_persisted_profile_key(clear_profile)
            else:
                clear_persisted_api_key()
    except (ConfigError, OSError) as exc:
        message = str(exc)
        console.print(f"[red]Failed to save config:[/red] {message}")
        if isinstance(exc, OSError):
            try:
                path = config_path()
            except Exception as path_exc:  # noqa: BLE001
                console.print(
                    f"[dim]Could not resolve config path for permission hint: {path_exc}[/dim]"
                )
            else:
                console.print(f"[dim]Check write permission on {path}.[/dim]")
        return ConfigMenuResult(saved=False, changes={}, api_key_changed=False, error=message)

    change_count = len(result.changes) + (1 if result.api_key_changed else 0)
    console.print(f"[green]Saved {_pluralize(change_count, 'change')}.[/green]")
    return result


def _confirm_cancel_when_dirty(state: ConfigMenuState, console: Console) -> bool:
    if not state.dirty:
        return True
    try:
        if _prompt_yes_no("Discard pending changes? [y/N]"):
            return True
    except (Abort, EOFError, KeyboardInterrupt):
        console.print("")
    console.print("[dim]Returning to configuration menu.[/dim]")
    return False


def _prompt_text(prompt: str, default: str) -> str:
    value = str(typer.prompt(prompt, default=str(default), show_default=True))
    if value == "":
        return str(default)
    return value.strip()


def _prompt_optional_config_text(
    console: Console,
    prompt: str,
    current: str,
    field_name: str,
) -> str:
    display_value = str(current or "").strip() or "(not set)"
    console.print(
        f"[dim]Current {field_name}: {escape(display_value)}. "
        "Enter keeps it; type 'clear' to unset.[/dim]"
    )
    value = str(typer.prompt(prompt, default="", show_default=False)).strip()
    if not value:
        return str(current or "").strip()
    if value.casefold() == "clear":
        return ""
    return value


def _prompt_non_secret_text(
    console: Console,
    prompt: str,
    default: str,
    field_name: str,
) -> str:
    while True:
        value = _prompt_text(prompt, default)
        guarded = _resolve_non_secret_value(console, value, field_name)
        if guarded is not None:
            return guarded


def _resolve_non_secret_value(console: Console, value: str, field_name: str) -> str | None:
    candidate = str(value)
    while _looks_like_secret(candidate):
        console.print(f"[red]That looks like an API key, not a {field_name}.[/red]")
        console.print(
            "[yellow]Tip: paste API keys in the API Key section, not here.\n"
            "Keys here are written to plaintext profile config.[/yellow]"
        )
        replacement = _prompt_override_text(
            f"Re-enter {field_name} (or 'force' to keep this value)",
            "",
        )
        if replacement == _SECRET_FORCE_TOKEN:
            console.print("[red]Confirmed: storing potentially-sensitive value in plaintext.[/red]")
            return candidate
        if replacement:
            candidate = replacement
            continue
        return None
    return candidate


def _prompt_override_text(prompt: str, default: str) -> str:
    value = str(typer.prompt(prompt, default=str(default), show_default=True))
    return value.strip()


def _prompt_thinking_label(
    console: Console,
    current: str,
    *,
    labels: tuple[str, ...] | None = None,
) -> str:
    resolved_labels = labels or THINKING_LABELS
    current_value = current if current in resolved_labels else "auto"
    rows = [
        (
            label,
            label,
            {
                "off": "no extra reasoning tokens",
                "minimal": "minimal reasoning budget",
                "low": "small reasoning budget",
                "medium": "medium reasoning budget",
                "high": "large reasoning budget",
                "xhigh": "maximum reasoning budget when supported",
                "max": "provider maximum reasoning budget when supported",
                "ultra": "provider ultra reasoning budget when supported",
                "auto": "let the provider decide",
            }.get(label, "provider-specific reasoning budget"),
        )
        for label in resolved_labels
    ]
    value = _run_config_picker(
        console=console,
        title="Default Model",
        subtitle=(
            "Reasoning effort. Some providers ignore this until they add native reasoning support."
        ),
        rows=rows,
        current_value=current_value,
    )
    if value is None:
        return current
    return _normalize_thinking_label(value)


def _active_subscription_profile(state: ConfigMenuState) -> ProfileSpec | None:
    if not state.active_profile or state.active_profile not in state.profiles:
        return None
    profile = ProfileSpec.from_dict(state.active_profile, state.profiles[state.active_profile])
    return profile if profile.auth_provider else None


def _temperature_controls_available(state: ConfigMenuState) -> bool:
    """Return whether the active transport accepts caller-controlled sampling."""

    profile = _active_subscription_profile(state)
    if profile is None:
        return True
    try:
        from ..provider_auth import create_provider_auth

        adapter = create_provider_auth(profile.auth_provider or "")
    except Exception:  # noqa: BLE001 - unknown adapters must not expose a false control
        return False
    return bool(getattr(adapter, "supports_temperature", False))


def _subscription_models_for_state(state: ConfigMenuState) -> tuple[Any, ...]:
    profile = _active_subscription_profile(state)
    if profile is None or not profile.auth_provider:
        return ()
    if state._subscription_models_loaded:
        return state._subscription_models_cache
    try:
        from ..provider_auth import create_provider_auth

        models = tuple(create_provider_auth(profile.auth_provider).list_models(refresh=True))
        state._subscription_models_cache = models
        state._subscription_models_loaded = True
        state.config_warning = None
        return models
    except Exception as exc:  # noqa: BLE001 - config remains usable while offline
        state.config_warning = f"Subscription model catalog unavailable: {exc}"
        state._subscription_models_cache = ()
        state._subscription_models_loaded = True
        return ()


def _thinking_labels_for_state(
    state: ConfigMenuState,
    *,
    model: str | None = None,
) -> tuple[str, ...]:
    profile = _active_subscription_profile(state)
    if profile is None:
        preset = _active_preset(state)
        contract = reasoning_contract_for(
            getattr(preset, "provider_key", None),
            str(model or state.fields.get("model") or ""),
            preset_key=getattr(preset, "key", None),
        )
        preset_key = getattr(preset, "key", None)
        if preset_key == "nvidia" and contract is UNKNOWN_CONTRACT:
            return ("auto",)
        current = _normalize_thinking_label(state.thinking_label)
        if preset_key in {"alysis", "nvidia", "zai-coding-plan"}:
            # A global effort may have been saved for the previously active
            # provider. Do not preserve an invalid value merely because it is
            # current; these controls are exact per hosted model and surface.
            current = "auto"
        return tuple(
            reasoning_labels_allowed_by_contract(
                contract,
                THINKING_LABELS,
                current=current,
            )
        )
    model_id = str(model or state.fields.get("model") or "").strip()
    selected = next(
        (item for item in _subscription_models_for_state(state) if item.id == model_id),
        None,
    )
    if selected is None:
        current = _normalize_thinking_label(state.thinking_label)
        if not state._subscription_models_cache and current != "auto":
            return ("auto", current)
        return ("auto",)
    labels: list[str] = ["auto"]
    for effort in selected.reasoning_efforts:
        label = "off" if effort.id == "none" else effort.id
        if label in THINKING_LABELS and label not in labels:
            labels.append(label)
    return tuple(labels)


def _prompt_positive_float_text(console: Console, prompt: str, current: str) -> str:
    for _attempt in range(3):
        value = _prompt_text(prompt, current)
        number = _finite_float(value, fallback=None)
        if number is not None and number > 0:
            return _format_number(number)
        console.print(f"[red]{prompt} must be a positive number.[/red]")
    return current


def _prompt_positive_int_text(console: Console, prompt: str, current: str) -> str:
    for _attempt in range(3):
        value = _prompt_text(prompt, current)
        number = _finite_int(value, fallback=None)
        if number is not None and number > 0:
            return str(number)
        console.print(f"[red]{prompt} must be a positive integer.[/red]")
    return current


def _prompt_yes_no(prompt: str) -> bool:
    clean_prompt = str(prompt).removesuffix(" [y/N]")
    return bool(typer.confirm(clean_prompt, default=False))


def _parse_header_text(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in str(raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        if "=" not in text:
            raise ConfigError("Profile headers must use k=v syntax.")
        key, value = text.split("=", 1)
        if key.strip() and value.strip():
            headers[key.strip()] = value.strip()
    return headers


def _pending_change_count(state: ConfigMenuState) -> int:
    original = state._original
    current = state.snapshot()
    count = 0
    for key in (
        "fields",
        "role_models",
        "forge_role_models",
        "role_temperatures",
        "profiles",
        "agent_runtimes",
    ):
        original_values = original.get(key) if isinstance(original.get(key), dict) else {}
        current_values = current.get(key) if isinstance(current.get(key), dict) else {}
        for field_name in set(original_values) | set(current_values):
            if str(original_values.get(field_name, "")) != str(current_values.get(field_name, "")):
                count += 1
    for key in (
        "active_profile",
        "execution_backend",
        "execution_runtime",
        "default_workspace_path",
        "thinking_label",
        "thinking_label_explicitly_set",
        "new_api_key",
        "new_api_key_profile",
        "clear_stored_key_confirmed",
    ):
        if current.get(key) != original.get(key):
            count += 1
    return count


def mask_api_key(api_key: str | None) -> str:
    clean = str(api_key or "").strip()
    if not clean:
        return "(not set)"
    suffix = clean[-4:] if len(clean) >= 4 else clean
    if clean.startswith("sk-"):
        return f"sk-…{suffix}"
    return f"…{suffix}"


def thinking_label_to_config_value(label: str) -> bool | None:
    normalized = _normalize_thinking_label(label)
    if normalized == "off":
        return False
    if normalized == "auto":
        return None
    return True


def thinking_label_to_reasoning_effort(label: str) -> str | None:
    normalized = _normalize_thinking_label(label)
    if normalized == "off":
        return "none"
    if normalized == "auto":
        return None
    return normalized


def thinking_label_from_cfg(cfg: AppConfig) -> str:
    value = getattr(cfg, "llm_enable_thinking", None)
    reasoning_effort = resolve_llm_reasoning_effort(cfg)
    if reasoning_effort == "none":
        return "off"
    if reasoning_effort and reasoning_effort != "none":
        return reasoning_effort
    if value is False:
        return "off"
    if value is None:
        return "auto"
    hint = ""
    if isinstance(cfg.extra_fields, dict):
        hint = str(cfg.extra_fields.get(_THINKING_LABEL_EXTRA_FIELD) or "").strip().lower()
    if hint in THINKING_LABELS and hint not in {"auto", "off"}:
        return hint
    return "medium"


def _set_thinking_label_hint(cfg: AppConfig, label: str) -> None:
    normalized = _normalize_thinking_label(label)
    if normalized not in {"off", "auto"}:
        cfg.extra_fields[_THINKING_LABEL_EXTRA_FIELD] = normalized
        return
    cfg.extra_fields.pop(_THINKING_LABEL_EXTRA_FIELD, None)


def _normalize_role(role: str) -> str:
    role_key = str(role or "").strip().lower()
    if role_key not in ROLE_MODEL_ORDER:
        raise KeyError(f"Unknown role: {role}")
    return role_key


def _normalize_thinking_label(label: str) -> str:
    normalized = str(label or "").strip().lower()
    if normalized not in THINKING_LABELS:
        allowed = ", ".join(THINKING_LABELS)
        raise ValueError(f"thinking must be one of: {allowed}")
    return normalized


def _normalize_step_budget_policy(value: str) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {"adaptive": "autonomous", "fixed": "limited"}
    if normalized not in STEP_BUDGET_POLICIES:
        allowed = ", ".join(STEP_BUDGET_POLICIES)
        raise ValueError(f"Step budget policy must be one of: {allowed}")
    return aliases.get(normalized, normalized)


def _normalize_prompt_cache_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower() or "manual"
    if normalized not in PROMPT_CACHE_MODES:
        allowed = ", ".join(PROMPT_CACHE_MODES)
        raise ValueError(f"Prompt cache mode must be one of: {allowed}")
    return normalized


def _normalize_anthropic_prompt_cache_ttl(value: Any) -> str:
    normalized = str(value or "").strip().lower() or "5m"
    if normalized not in ANTHROPIC_PROMPT_CACHE_TTLS:
        allowed = ", ".join(ANTHROPIC_PROMPT_CACHE_TTLS)
        raise ValueError(f"Anthropic prompt cache TTL must be one of: {allowed}")
    return normalized


def _raw_compaction_extra_fields(cfg: AppConfig) -> dict[str, Any]:
    raw = cfg.extra_fields.get("compaction") if isinstance(cfg.extra_fields, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def _cache_aware_compaction_bool(value: Any) -> bool:
    if value is None:
        return _CACHE_AWARE_COMPACTION_DEFAULT
    try:
        return _normalize_bool_text(value, label="Cache-aware compaction")
    except ValueError:
        return _CACHE_AWARE_COMPACTION_DEFAULT


def _cache_aware_compaction_text(value: Any) -> str:
    return "true" if _cache_aware_compaction_bool(value) else "false"


def _parse_cache_aware_min_trigger_ratio(value: Any) -> float:
    raw = _CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT if value is None else value
    try:
        ratio = float(str(raw).strip() or _CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT)
    except (TypeError, ValueError) as exc:
        raise ValueError(_cache_aware_min_trigger_ratio_error()) from exc
    if (
        not math.isfinite(ratio)
        or ratio <= _CACHE_AWARE_MIN_TRIGGER_RATIO_MIN
        or ratio >= _CACHE_AWARE_MIN_TRIGGER_RATIO_MAX
    ):
        raise ValueError(_cache_aware_min_trigger_ratio_error())
    return ratio


def _cache_aware_min_trigger_ratio_value(value: Any) -> float:
    try:
        return _parse_cache_aware_min_trigger_ratio(value)
    except ValueError:
        return _CACHE_AWARE_MIN_TRIGGER_RATIO_DEFAULT


def _cache_aware_min_trigger_ratio_text(value: Any) -> str:
    return _format_number(_cache_aware_min_trigger_ratio_value(value))


def _normalize_cache_aware_min_trigger_ratio(value: Any) -> str:
    return _format_number(_parse_cache_aware_min_trigger_ratio(value))


def _cache_aware_min_trigger_ratio_error() -> str:
    return (
        "Cache-aware compaction min trigger ratio must be greater than "
        f"{_CACHE_AWARE_MIN_TRIGGER_RATIO_MIN} and less than "
        f"{_CACHE_AWARE_MIN_TRIGGER_RATIO_MAX}."
    )


def _normalize_bool_text(value: Any, *, label: str) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{label} must be true or false.")


def _thinking_config_text(value: bool | None) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "auto"


def _normalized_role_model_values(raw: Any) -> dict[str, str]:
    source = raw if isinstance(raw, dict) else {}
    values = {role: "" for role in ROLE_MODEL_ORDER}
    for key, value in source.items():
        role = str(key).strip().lower()
        if role in values:
            values[role] = str(value).strip()
    return values


def _non_empty_role_values(
    values: dict[str, str],
    *,
    roles: tuple[str, ...] = ROLE_MODEL_ORDER,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for role in roles:
        value = str(values.get(role, "")).strip()
        if value:
            out[role] = value
    return out


def _role_model_storage_values(
    raw: Any,
    values: dict[str, str],
    *,
    roles: tuple[str, ...] = ROLE_MODEL_ORDER,
) -> dict[str, str]:
    out: dict[str, str] = {}
    source = raw if isinstance(raw, dict) else {}
    modeled_roles = set(roles)
    for key, value in source.items():
        role = str(key).strip().lower()
        if not role or role in modeled_roles:
            continue
        model = str(value).strip()
        if model:
            out[role] = model
    out.update(_non_empty_role_values(values, roles=roles))
    return out


def _role_text_map_from_snapshot(raw: Any) -> dict[str, str]:
    source = raw if isinstance(raw, dict) else {}
    return {role: str(source.get(role, "")) for role in ROLE_MODEL_ORDER}


def _finite_float(raw: Any, *, fallback: float | None) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(value):
        return fallback
    return value


def _finite_int(raw: Any, *, fallback: int | None) -> int | None:
    try:
        text = str(raw).strip()
        value = int(text)
    except (TypeError, ValueError):
        return fallback
    return value


def _format_number(raw: Any) -> str:
    value = _finite_float(raw, fallback=None)
    if value is None:
        return str(raw or "")
    if value.is_integer():
        return str(int(value))
    text = f"{value:.12g}"
    return re.sub(r"\.0+$", "", text)


def _format_optional_limit(raw: Any) -> str:
    if raw is None:
        return "unlimited"
    return _format_number(raw)


def _format_integer(raw: Any) -> str:
    value = _finite_int(raw, fallback=None)
    if value is None:
        return str(raw or "")
    return str(value)
