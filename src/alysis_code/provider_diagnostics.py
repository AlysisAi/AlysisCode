from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from .config import (
    ApiKeyResolution,
    AppConfig,
    resolve_api_key,
    resolve_profile_api_key,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
    resolve_web_search_adapter,
    resolve_web_search_mode,
    resolve_web_search_policy,
)
from .llm.cache_capabilities import (
    EffectiveCacheCapability,
    resolve_effective_cache_capability,
)
from .llm.cache_policy import (
    ResolvedPromptCachePolicy,
    prompt_cache_policy_diagnostic_rows,
    resolve_prompt_cache_policy,
)
from .llm.gemini_interactions import (
    gemini_interactions_disabled_message,
    gemini_interactions_enabled,
)
from .llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
    get_provider_protocol_capabilities,
)
from .model_registry import resolve_model_provider_key
from .profile_presets import (
    NATIVE_PROFILE_PROTOCOLS,
    canonical_model_alias_for_preset,
    find_preset_for_profile,
    known_model_family,
    model_known_incompatible_with_family,
    profile_provider_family,
)
from .profiles import ProfileSpec, get_active_profile, resolve_effective_base_url
from .provider_auth import ProviderAuthError, create_provider_auth
from .tools.web_search_runtime import resolve_web_search_runtime_status
from .web_search_adapters import (
    AUTO_WEB_SEARCH_ADAPTER,
    EXTERNAL_WEB_SEARCH_ADAPTERS,
    NATIVE_WEB_SEARCH_ADAPTERS,
    web_search_adapter_is_external,
    web_search_adapter_is_native,
)


class _ChatClientFactory(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


class _WebSearchProbe(Protocol):
    def __call__(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProviderDiagnostics:
    profile_name: str
    provider_key: str
    protocol: str
    protocol_kind: str
    base_url_host: str
    model: str
    api_key_source: str
    api_key_present: bool
    web_search_policy: str
    web_search_mode: str
    web_search_configured_adapter: str
    web_search_runtime_adapter: str
    web_search_registration_ready: bool
    web_search_backend_kind: str
    streaming_supported: bool
    stream_enabled: bool
    unsupported_parameters: tuple[str, ...]
    cache_capability: EffectiveCacheCapability
    cache_policy: ResolvedPromptCachePolicy
    quirks: tuple[str, ...]
    notes: tuple[str, ...]
    issues: tuple[str, ...]

    def rows(self) -> tuple[tuple[str, str], ...]:
        return (
            ("profile", self.profile_name),
            ("provider_key", self.provider_key),
            ("protocol", self.protocol),
            ("protocol_kind", self.protocol_kind),
            ("base_url_host", self.base_url_host or "(missing)"),
            ("model", self.model or "(missing)"),
            ("api_key_source", self.api_key_source),
            ("api_key_present", "yes" if self.api_key_present else "no"),
            ("web_search_policy", self.web_search_policy),
            ("web_search_mode", self.web_search_mode),
            ("web_search_configured_adapter", self.web_search_configured_adapter),
            ("web_search_runtime_adapter", self.web_search_runtime_adapter or "(none)"),
            (
                "web_search_ready",
                "yes" if self.web_search_registration_ready else "no",
            ),
            ("web_search_backend_kind", self.web_search_backend_kind),
            ("stream_enabled", "yes" if self.stream_enabled else "no"),
            ("streaming_supported", "yes" if self.streaming_supported else "no"),
            (
                "unsupported_parameters",
                ", ".join(self.unsupported_parameters) if self.unsupported_parameters else "none",
            ),
            *prompt_cache_policy_diagnostic_rows(self.cache_policy),
            ("known_quirks", "; ".join(self.quirks) if self.quirks else "none"),
            ("notes", "; ".join(self.notes) if self.notes else "none"),
            ("issues", "; ".join(self.issues) if self.issues else "none"),
        )


@dataclass(frozen=True)
class ProviderLiveValidation:
    profile_name: str
    provider_key: str
    protocol: str
    model: str
    status: str
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def rows(self) -> tuple[tuple[str, str], ...]:
        return (
            ("profile", self.profile_name),
            ("provider_key", self.provider_key),
            ("protocol", self.protocol),
            ("model", self.model or "(missing)"),
            ("status", self.status),
            ("message", self.message),
        )


def build_provider_diagnostics(cfg: AppConfig) -> ProviderDiagnostics:
    profile = get_active_profile(cfg)
    base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
    model = str(profile.default_model or getattr(cfg, "model", "") or "").strip()
    provider_key = (
        resolve_model_provider_key(
            cfg=cfg,
            model_name=model,
            base_url=base_url,
            profile_name=profile.name,
        )
        or profile.name
    )
    api_key = _resolve_active_profile_api_key(cfg, profile.name)
    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    from .llm.factory import resolve_model_protocol

    protocol = resolve_model_protocol(provider_key=provider_key, model=model, protocol=protocol)
    capabilities = get_provider_protocol_capabilities(
        provider_key=provider_key,
        protocol=protocol,
    )
    preset = find_preset_for_profile(profile)
    provider_auth = None
    auth_issues: list[str] = []
    try:
        if profile.auth_provider:
            provider_auth = create_provider_auth(profile.auth_provider)
    except ProviderAuthError:
        # Diagnostics must remain available for broken profiles. Adapter
        # exceptions can contain credentials; do not copy their text here.
        auth_issues.append(
            "The active profile's authentication adapter could not be initialized. "
            "Check its auth_provider setting and installed authentication support "
            "before using the profile."
        )
    cache_capability = resolve_effective_cache_capability(
        provider_key=provider_key,
        protocol=protocol,
        model=model,
        base_url=base_url,
        transport_capabilities=capabilities,
        auth_cache_capability=getattr(provider_auth, "cache_capability", None),
        preset_cache_capability=(preset.cache_capability if preset is not None else None),
        profile_cache_capability=profile.cache_capability,
    )
    cache_policy = resolve_prompt_cache_policy(
        cfg=cfg,
        capabilities=capabilities,
        provider_key=provider_key,
        protocol=protocol,
        model=model,
        prompt_cache_key=resolve_prompt_cache_key(cfg),
        prompt_cache_retention=resolve_prompt_cache_retention(cfg),
        prompt_cache_namespace=None,
        cache_capability=cache_capability,
    )
    # Mirrors _disable_unsupported_native_streaming: unknown provider capabilities
    # assume streaming works; only a known supports_streaming=False entry disables it.
    streaming_supported = capabilities.supports_streaming if capabilities is not None else True
    protocol_kind = "native" if protocol in NATIVE_PROFILE_PROTOCOLS else "compatibility"

    policy = resolve_web_search_policy(cfg)
    mode = resolve_web_search_mode(cfg)
    configured_adapter = resolve_web_search_adapter(cfg)
    search_status = resolve_web_search_runtime_status(cfg=cfg, api_key=api_key.key)
    runtime_adapter = search_status.provider or ""
    backend_kind = _web_search_backend_kind(runtime_adapter, configured_adapter)
    effective_search_ready = search_status.registration_ready and policy != "off"

    notes = [_redact_url_text(note) for note in search_status.notes]
    if policy == "off":
        notes.append("web_search_policy=off prevents web_search tool registration.")
    if capabilities is None:
        notes.append(
            "No bundled capability record for this provider/protocol; treating it as a custom profile."
        )
    notes.extend(capabilities.quirks if capabilities is not None else ())

    issues = _provider_diagnostic_issues(
        cfg=cfg,
        profile=profile,
        provider_key=provider_key,
        protocol=protocol,
        base_url=base_url,
        api_key=api_key,
        model=model,
        streaming_supported=streaming_supported,
        web_search_mode=mode,
        configured_adapter=configured_adapter,
        runtime_adapter=runtime_adapter,
        search_ready=search_status.registration_ready,
        search_notes=search_status.notes,
    )
    issues.extend(auth_issues)

    unsupported = capabilities.unsupported_parameters if capabilities is not None else ()
    quirks = (
        capabilities.quirks
        if capabilities is not None
        else ("Custom provider profile; validate gateway-specific behavior with a smoke run.",)
    )
    return ProviderDiagnostics(
        profile_name=profile.name,
        provider_key=str(provider_key or ""),
        protocol=protocol,
        protocol_kind=protocol_kind,
        base_url_host=_base_url_host(base_url),
        model=model,
        api_key_source=_redacted_api_key_source(api_key),
        api_key_present=bool(api_key.key),
        web_search_policy=policy,
        web_search_mode=mode,
        web_search_configured_adapter=configured_adapter,
        web_search_runtime_adapter=runtime_adapter,
        web_search_registration_ready=effective_search_ready,
        web_search_backend_kind=backend_kind,
        streaming_supported=streaming_supported,
        stream_enabled=bool(getattr(cfg, "stream", False)),
        unsupported_parameters=unsupported,
        cache_capability=cache_capability,
        cache_policy=cache_policy,
        quirks=quirks,
        notes=tuple(_dedupe(notes)),
        issues=tuple(_dedupe([_redact_url_text(issue) for issue in issues])),
    )


def provider_diagnostic_warning_lines(cfg: AppConfig) -> tuple[str, ...]:
    """Return redacted active-provider setup issues suitable for setup/config UX."""
    return build_provider_diagnostics(cfg).issues


def validate_active_provider_live(
    cfg: AppConfig,
    *,
    timeout_s: float = 15.0,
    client_factory: _ChatClientFactory | None = None,
) -> ProviderLiveValidation:
    """Run a minimal active-profile text request after explicit user opt-in.

    The caller is responsible for confirmation. This function keeps output redacted and classifies
    common provider failures without making any live requests from unit tests unless a real factory
    is supplied intentionally.
    """
    profile = get_active_profile(cfg)
    base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
    model = str(profile.default_model or getattr(cfg, "model", "") or "").strip()
    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    provider_key = (
        resolve_model_provider_key(
            cfg=cfg,
            model_name=model,
            base_url=base_url,
            profile_name=profile.name,
        )
        or profile_provider_family(profile)
        or profile.name
    )
    if not model:
        return ProviderLiveValidation(
            profile_name=profile.name,
            provider_key=str(provider_key or ""),
            protocol=protocol,
            model=model,
            status="failed",
            message="Active profile has no model configured. Set a model before live validation.",
        )
    api_key = _resolve_active_profile_api_key(cfg, profile.name)
    if not api_key.key and not profile.auth_provider:
        return ProviderLiveValidation(
            profile_name=profile.name,
            provider_key=str(provider_key or ""),
            protocol=protocol,
            model=model,
            status="failed",
            message=(
                "Active profile has no API key. Export the profile API key environment variable "
                "or store a profile key before live validation."
            ),
        )
    try:
        if client_factory is None:
            from .llm.factory import make_llm_client

            client_factory = make_llm_client
        client = client_factory(
            cfg=cfg,
            api_key=api_key.key or "",
            model=model,
            timeout_s=timeout_s,
            temperature=0.0,
            profile=profile,
        )
        if getattr(client, "supports_tool_calling", True) is False:
            return ProviderLiveValidation(
                profile_name=profile.name,
                provider_key=str(provider_key or ""),
                protocol=protocol,
                model=model,
                status="failed",
                message=(
                    "Active profile chat client does not support tool calling; choose a "
                    "tool-capable profile before using coding-agent modes."
                ),
            )
        response = client.chat(
            messages=[{"role": "user", "content": "Reply with only: ok"}],
            tools=_live_validation_tool_schema(),
            max_tokens=20,
        )
    except Exception as exc:  # pragma: no cover - exercised with fake clients in tests.
        return ProviderLiveValidation(
            profile_name=profile.name,
            provider_key=str(provider_key or ""),
            protocol=protocol,
            model=model,
            status="failed",
            message=_classify_live_validation_error(str(exc), model=model),
        )
    if getattr(
        client, "supports_tool_calling", True
    ) is False or _response_reports_tool_calling_disabled(response):
        return ProviderLiveValidation(
            profile_name=profile.name,
            provider_key=str(provider_key or ""),
            protocol=protocol,
            model=model,
            status="failed",
            message=(
                "Provider accepted chat but rejected tool calling; choose a tool-capable "
                "model/profile before using coding-agent modes."
            ),
        )
    if str(getattr(response, "content", "") or "").strip() or getattr(response, "tool_calls", None):
        return ProviderLiveValidation(
            profile_name=profile.name,
            provider_key=str(provider_key or ""),
            protocol=protocol,
            model=model,
            status="passed",
            message="Minimal tool-capability request completed successfully.",
        )
    return ProviderLiveValidation(
        profile_name=profile.name,
        provider_key=str(provider_key or ""),
        protocol=protocol,
        model=model,
        status="failed",
        message="Provider returned an empty response to the live validation prompt.",
    )


@dataclass(frozen=True)
class WebSearchLiveValidation:
    """Result of exercising the enabled web_search tool end-to-end once.

    Static readiness checks only prove the configuration parses; they cannot
    catch a gateway that rejects real search requests. This probe issues one
    minimal call through the resolved adapter and reports what actually served
    it, so "ready" always means "a real call worked".
    """

    mode: str
    configured_adapter: str
    resolved_adapter: str
    backend_used: str
    fallback_used: bool
    status: str
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def rows(self) -> tuple[tuple[str, str], ...]:
        return (
            ("web_search_mode", self.mode),
            ("configured_adapter", self.configured_adapter),
            ("resolved_adapter", self.resolved_adapter or "(none)"),
            ("backend_used", self.backend_used or "(none)"),
            ("fallback_used", "yes" if self.fallback_used else "no"),
            ("status", self.status),
            ("message", self.message),
        )


_WEB_SEARCH_PROBE_QUERY = "current UTC date"


def _cfg_with_web_search_probe_overrides(
    cfg: AppConfig,
    *,
    timeout_s: float,
    adapter: str | None = None,
) -> AppConfig:
    update: dict[str, Any] = {"web_search_timeout_s": float(timeout_s)}
    if adapter:
        update["web_search_adapter"] = adapter
    return cfg.model_copy(update=update)


def validate_web_search_live(
    cfg: AppConfig,
    *,
    timeout_s: float = 15.0,
    search_fn: _WebSearchProbe | None = None,
) -> WebSearchLiveValidation:
    """Issue one real, minimal web_search call through the resolved adapter.

    The caller is responsible for confirmation (the probe makes live requests
    and may incur provider cost). Reports the adapter that was chosen, whether
    an external fallback served the call, and the failing adapter's error
    verbatim (URL-redacted) when something breaks.
    """

    mode = resolve_web_search_mode(cfg)
    configured_adapter = resolve_web_search_adapter(cfg)
    policy = resolve_web_search_policy(cfg)
    profile = get_active_profile(cfg)
    api_key = _resolve_active_profile_api_key(cfg, profile.name)
    search_status = resolve_web_search_runtime_status(cfg=cfg, api_key=api_key.key)
    resolved_adapter = search_status.provider or ""
    if policy == "off" or mode == "off" or not search_status.registration_ready:
        return WebSearchLiveValidation(
            mode=mode,
            configured_adapter=configured_adapter,
            resolved_adapter=resolved_adapter,
            backend_used="",
            fallback_used=False,
            status="skipped",
            message=(
                "web_search is not enabled or not configured: "
                f"{_redact_url_text(search_status.summary)}"
            ),
        )
    if search_fn is None:
        from .tools.web_search import web_search

        search_fn = web_search
    # Mirror web_search()'s own override resolution (env adapter/provider
    # overrides included), not just the config field: fallback only happens
    # when no explicit adapter override pins the backend.
    try:
        from .tools.web_search import _resolve_provider_override

        provider_override, _override_error = _resolve_provider_override(cfg=cfg, strict=False)
    except Exception:  # noqa: BLE001 - treat unknown override state as pinned.
        provider_override = resolved_adapter

    def _probe(probe_cfg: AppConfig) -> dict[str, Any]:
        return search_fn(
            query=_WEB_SEARCH_PROBE_QUERY,
            cfg=probe_cfg,
            api_key=api_key.key,
            max_sources=1,
        )

    # Pin the resolved adapter first so a hard failure surfaces its own error
    # instead of being silently masked by an in-call fallback. Env adapter
    # overrides can defeat the pin, so the actually-used backend is compared
    # against the resolved adapter instead of being assumed.
    try:
        result = _probe(
            _cfg_with_web_search_probe_overrides(
                cfg,
                timeout_s=timeout_s,
                adapter=resolved_adapter,
            )
        )
    except Exception as exc:  # noqa: BLE001 - report any probe failure verbatim.
        resolved_error = _redact_url_text(str(exc))
    else:
        backend_used = str(result.get("backend_adapter") or resolved_adapter)
        fallback_used = bool(backend_used) and backend_used != resolved_adapter
        return WebSearchLiveValidation(
            mode=mode,
            configured_adapter=configured_adapter,
            resolved_adapter=resolved_adapter,
            backend_used=backend_used,
            fallback_used=fallback_used,
            status="passed",
            message=(
                f"web_search served {int(result.get('source_count') or 0)} source(s) "
                f"via {backend_used}."
                + (
                    f" The resolved adapter {resolved_adapter} did not serve the probe; "
                    "an in-call fallback did."
                    if fallback_used
                    else ""
                )
            ),
        )

    can_fall_back = mode == "auto" and provider_override is None
    if can_fall_back:
        try:
            fallback_result = _probe(_cfg_with_web_search_probe_overrides(cfg, timeout_s=timeout_s))
        except Exception as exc:  # noqa: BLE001 - combined failure is the report.
            return WebSearchLiveValidation(
                mode=mode,
                configured_adapter=configured_adapter,
                resolved_adapter=resolved_adapter,
                backend_used="",
                fallback_used=False,
                status="failed",
                message=(
                    f"{resolved_adapter} failed: {resolved_error}; "
                    f"fallback backends also failed: {_redact_url_text(str(exc))}"
                ),
            )
        backend_used = str(fallback_result.get("backend_adapter") or "")
        fallback_used = bool(backend_used) and backend_used != resolved_adapter
        return WebSearchLiveValidation(
            mode=mode,
            configured_adapter=configured_adapter,
            resolved_adapter=resolved_adapter,
            backend_used=backend_used,
            fallback_used=fallback_used,
            status="passed",
            message=(
                f"{resolved_adapter} failed ({resolved_error}); "
                f"fallback {backend_used} served the probe."
                if fallback_used
                else (
                    f"web_search served the probe via {backend_used} after an "
                    f"initial {resolved_adapter} failure ({resolved_error})."
                )
            ),
        )
    return WebSearchLiveValidation(
        mode=mode,
        configured_adapter=configured_adapter,
        resolved_adapter=resolved_adapter,
        backend_used="",
        fallback_used=False,
        status="failed",
        message=f"{resolved_adapter} failed: {resolved_error}",
    )


@dataclass(frozen=True)
class ReasoningSuppressionReport:
    """Whether a reasoning-off request actually suppressed provider reasoning.

    Latency-sensitive internal calls are issued with enable_thinking=False,
    but some gateways ignore the flag and burn reasoning tokens anyway. This
    makes the per-provider behavior visible instead of assumed.
    """

    profile_name: str
    provider_key: str
    protocol: str
    model: str
    outcome: str
    reasoning_tokens: int | None
    message: str

    def rows(self) -> tuple[tuple[str, str], ...]:
        return (
            ("profile", self.profile_name),
            ("provider_key", self.provider_key),
            ("protocol", self.protocol),
            ("model", self.model or "(missing)"),
            ("reasoning_off_outcome", self.outcome),
            (
                "reasoning_tokens",
                "(not reported)" if self.reasoning_tokens is None else str(self.reasoning_tokens),
            ),
            ("message", self.message),
        )


def probe_reasoning_suppression_live(
    cfg: AppConfig,
    *,
    timeout_s: float = 15.0,
    client_factory: _ChatClientFactory | None = None,
) -> ReasoningSuppressionReport:
    """Send one reasoning-off request and report whether reasoning was suppressed.

    Mirrors how internal latency-sensitive clients are built (enable_thinking
    False, empty reasoning effort) and inspects usage reasoning-token details
    on the response. The caller is responsible for confirmation.
    """

    profile = get_active_profile(cfg)
    base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    model = str(profile.default_model or getattr(cfg, "model", "") or "").strip()
    provider_key = (
        resolve_model_provider_key(
            cfg=cfg,
            model_name=model,
            base_url=base_url,
            profile_name=profile.name,
        )
        or profile_provider_family(profile)
        or profile.name
    )

    def _report(outcome: str, message: str, reasoning_tokens: int | None = None):
        return ReasoningSuppressionReport(
            profile_name=profile.name,
            provider_key=str(provider_key or ""),
            protocol=protocol,
            model=model,
            outcome=outcome,
            reasoning_tokens=reasoning_tokens,
            message=message,
        )

    if not model:
        return _report("skipped", "Active profile has no model configured.")
    api_key = _resolve_active_profile_api_key(cfg, profile.name)
    if not api_key.key and not profile.auth_provider:
        return _report("skipped", "Active profile has no API key.")
    try:
        if client_factory is None:
            from .llm.factory import make_llm_client

            client_factory = make_llm_client
        client = client_factory(
            cfg=cfg,
            api_key=api_key.key or "",
            model=model,
            timeout_s=timeout_s,
            temperature=0.0,
            profile=profile,
            enable_thinking=False,
            reasoning_effort="",
        )
        response = client.chat(
            messages=[{"role": "user", "content": "Reply with only: ok"}],
            max_tokens=20,
        )
    except Exception as exc:  # noqa: BLE001 - probe failures become the report.
        return _report("failed", _classify_live_validation_error(str(exc), model=model))

    usage = getattr(response, "usage", None)
    reasoning_tokens = getattr(usage, "reasoning_tokens", None)
    visible_reasoning = bool(getattr(response, "reasoning", ()) or ())
    if isinstance(reasoning_tokens, int) and reasoning_tokens > 0:
        return _report(
            "ignored",
            (
                f"Provider returned {reasoning_tokens} reasoning token(s) despite "
                "enable_thinking=false; latency-sensitive internal calls (e.g. routing) "
                "will pay reasoning latency on this provider."
            ),
            reasoning_tokens,
        )
    if visible_reasoning:
        return _report(
            "ignored",
            (
                "Provider returned a reasoning trace despite enable_thinking=false; "
                "latency-sensitive internal calls (e.g. routing) will pay reasoning "
                "latency on this provider."
            ),
            reasoning_tokens if isinstance(reasoning_tokens, int) else None,
        )
    if isinstance(reasoning_tokens, int):
        return _report(
            "suppressed",
            "Provider honored enable_thinking=false (0 reasoning tokens).",
            reasoning_tokens,
        )
    return _report(
        "not_reported",
        (
            "Provider does not report reasoning token details; suppression cannot be "
            "verified from usage data."
        ),
    )


def _live_validation_tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "diagnostic_echo",
                "description": "Return a short diagnostic acknowledgement.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
    ]


def _response_reports_tool_calling_disabled(response: Any) -> bool:
    metadata = getattr(response, "provider_metadata", None)
    if not isinstance(metadata, dict):
        return False
    transport = metadata.get("transport")
    if not isinstance(transport, dict):
        return False
    if transport.get("tools_retry_used") is True:
        return True
    return str(transport.get("tools_omit_reason") or "") in {
        "provider_rejected_tool_calling",
        "cached_provider_rejection",
    }


def _provider_diagnostic_issues(
    *,
    cfg: AppConfig,
    profile: ProfileSpec,
    provider_key: str,
    protocol: str,
    base_url: str,
    api_key: ApiKeyResolution,
    model: str,
    streaming_supported: bool,
    web_search_mode: str,
    configured_adapter: str,
    runtime_adapter: str,
    search_ready: bool,
    search_notes: tuple[str, ...],
) -> list[str]:
    issues: list[str] = []
    missing_key_issue = _missing_api_key_issue(
        profile=profile,
        provider_key=provider_key,
        base_url=base_url,
        api_key=api_key,
    )
    if missing_key_issue:
        issues.append(missing_key_issue)
    issues.extend(
        _provider_model_issues(
            profile=profile,
            protocol=protocol,
            model=model,
            stream_enabled=bool(getattr(cfg, "stream", False)),
            web_search_mode=web_search_mode,
        )
    )
    if bool(getattr(cfg, "stream", False)) and not streaming_supported:
        issues.append(
            f"stream=true is configured but protocol={protocol} does not support streaming yet; "
            "Alysis Code will disable streaming for native chat runs until that protocol's "
            "streaming path is implemented. Suggested fix: `alysis config set stream false`."
        )
    if protocol == GEMINI_INTERACTIONS_PROTOCOL and not gemini_interactions_enabled(cfg):
        issues.append(gemini_interactions_disabled_message())
    issues.extend(
        _provider_protocol_mismatch_issues(
            profile=profile,
            protocol=protocol,
            base_url=base_url,
        )
    )

    if web_search_mode == "native" and configured_adapter in EXTERNAL_WEB_SEARCH_ADAPTERS:
        issues.append(
            f"web_search_mode=native is incompatible with web_search_adapter={configured_adapter}; "
            "native mode never uses Tavily or other external search providers. Suggested fix: "
            "`alysis config set web_search_mode external` or choose a provider-hosted "
            "web_search_adapter."
        )
    if web_search_mode == "external" and configured_adapter in NATIVE_WEB_SEARCH_ADAPTERS:
        issues.append(
            f"web_search_mode=external is incompatible with web_search_adapter={configured_adapter}; "
            "external mode does not use provider-hosted search adapters. Suggested fix: "
            "`alysis config set web_search_mode native` or choose an external adapter such as Tavily."
        )
    if web_search_mode in {"native", "external"} and not search_ready:
        detail = search_notes[0] if search_notes else "no ready web_search adapter"
        issues.append(f"web_search_mode={web_search_mode} is not ready: {detail}")

    if (
        web_search_mode == "external"
        and not search_ready
        and (
            configured_adapter == AUTO_WEB_SEARCH_ADAPTER
            or web_search_adapter_is_external(configured_adapter)
        )
    ):
        issues.append(
            "external web_search needs ALYSIS_WEB_SEARCH_API_KEY or TAVILY_API_KEY for "
            "the Tavily adapter, or the 'ddgs' package for the keyless backend. Suggested "
            "fix: export one search key, `pip install ddgs`, or run "
            "`alysis config set web_search_mode auto`."
        )

    is_custom_openai_compat = protocol == OPENAI_COMPAT_PROTOCOL and provider_key not in {
        "openai",
        "anthropic",
        "gemini",
        "qwen",
        "openrouter",
    }
    if web_search_mode == "native" and is_custom_openai_compat and not search_ready:
        issues.append(
            "custom OpenAI-compatible profiles do not automatically provide native hosted "
            "search; use a native provider preset, configure a matching native web_search_adapter, "
            "or switch web_search_mode to auto/external."
        )

    if (
        runtime_adapter
        and web_search_mode == "native"
        and not web_search_adapter_is_native(runtime_adapter)
    ):
        issues.append(f"native web_search selected non-native adapter {runtime_adapter}.")
    if (
        runtime_adapter
        and web_search_mode == "external"
        and not web_search_adapter_is_external(runtime_adapter)
    ):
        issues.append(f"external web_search selected non-external adapter {runtime_adapter}.")

    return issues


def _provider_model_issues(
    *,
    profile: ProfileSpec,
    protocol: str,
    model: str,
    stream_enabled: bool,
    web_search_mode: str,
) -> list[str]:
    issues: list[str] = []
    normalized_model = str(model or "").strip()
    if not normalized_model:
        issues.append(
            "Model is empty for the active profile. Suggested fix: `alysis config set model "
            "<provider-model-id>` or choose a model in `/config`."
        )
        return issues

    family = profile_provider_family(profile)
    model_family = known_model_family(normalized_model)
    if family is not None and model_known_incompatible_with_family(normalized_model, family):
        issues.append(
            f"Model {normalized_model!r} looks like a {model_family} model, but the active "
            f"profile/protocol is for {family}. Suggested fix: choose a {family} model or "
            "convert the profile to the matching provider."
        )

    preset = find_preset_for_profile(profile)
    if preset is not None:
        canonical = canonical_model_alias_for_preset(preset, normalized_model)
        if canonical and canonical != normalized_model:
            issues.append(
                f"Model {normalized_model!r} is a known renamed/deprecated alias for preset "
                f"{preset.key!r}; suggested model: {canonical!r}."
            )

    if _looks_like_unstable_model_alias(normalized_model):
        issues.append(
            f"Model {normalized_model!r} looks like a preview/latest alias. Availability can vary "
            "by account, region, and provider rollout; use a dated/stable model or run "
            "`alysis doctor providers --live --yes` to validate this profile."
        )

    if web_search_mode == "native" and _likely_non_chat_or_search_model(normalized_model):
        issues.append(
            f"Native web_search is enabled, but model {normalized_model!r} looks like a "
            "non-chat, audio, image, embedding, or realtime model that is unlikely to support "
            "provider-hosted search. Choose a chat model or set `web_search_mode` to off/auto."
        )

    if (
        stream_enabled
        and protocol != OPENAI_COMPAT_PROTOCOL
        and _likely_non_chat_stream_model(normalized_model)
    ):
        issues.append(
            f"stream=true is enabled, but model {normalized_model!r} looks like a specialized "
            "model that may not support this native streaming protocol. Validate with "
            "`alysis doctor providers --live --yes` or choose a standard chat model."
        )

    return issues


def _resolve_active_profile_api_key(cfg: AppConfig, profile_name: str) -> ApiKeyResolution:
    profile_key = resolve_profile_api_key(cfg, profile_name)
    if profile_key.key:
        return profile_key
    return resolve_api_key(cfg, profile_name=profile_name)


def _provider_protocol_mismatch_issues(
    *,
    profile: ProfileSpec,
    protocol: str,
    base_url: str,
) -> list[str]:
    issues: list[str] = []
    host = _base_url_host(base_url)
    path = _base_url_path(base_url)
    profile_name = profile.name
    plain_anthropic_legacy = profile_name == "anthropic" and protocol == OPENAI_COMPAT_PROTOCOL
    plain_gemini_legacy = profile_name == "gemini" and protocol == OPENAI_COMPAT_PROTOCOL
    explicit_first_party_compat = profile_name in {"anthropic-compat", "gemini-compat"}

    if profile_name.endswith("-native") and protocol == OPENAI_COMPAT_PROTOCOL:
        issues.append(
            f"Profile {profile_name!r} is named like a native profile but uses "
            "protocol=openai_compat. Suggested fix: "
            f"`alysis profile convert {profile_name} --to native`."
        )

    if plain_anthropic_legacy:
        issues.append(
            "Profile 'anthropic' uses legacy compatibility semantics. Plain 'anthropic' now "
            "means native Anthropic Messages for new profiles. Convert explicitly with "
            "`alysis profile convert anthropic --to native`, or keep an explicit legacy "
            "fallback profile such as `anthropic-compat`."
        )

    if plain_gemini_legacy:
        issues.append(
            "Profile 'gemini' uses legacy compatibility semantics. Plain 'gemini' now means "
            "native Gemini GenerateContent for new profiles. Convert explicitly with "
            "`alysis profile convert gemini --to native`, or keep an explicit legacy fallback "
            "profile such as `gemini-compat`."
        )

    if (
        protocol == OPENAI_RESPONSES_PROTOCOL
        and host != "api.openai.com"
        and not profile.auth_provider
    ):
        issues.append(
            "protocol=openai_responses is intended for the OpenAI Responses API at "
            "api.openai.com. Suggested fix: "
            f"`alysis profile convert {profile_name} --to compatibility` or update the base URL."
        )

    if protocol == ANTHROPIC_MESSAGES_PROTOCOL and host != "api.anthropic.com":
        issues.append(
            "protocol=anthropic_messages is intended for the Anthropic first-party Messages API. "
            f"Suggested fix: `alysis profile convert {profile_name} --to compatibility` "
            "or update the base URL."
        )

    if (
        protocol == OPENAI_COMPAT_PROTOCOL
        and host == "api.anthropic.com"
        and not explicit_first_party_compat
    ):
        issues.append(
            "This looks like the Anthropic first-party API using compatibility mode. "
            "Recommended fix for new first-party Claude profiles: "
            f"`alysis profile convert {profile_name} --to native`."
        )

    if protocol == GEMINI_GENERATE_CONTENT_PROTOCOL and "/openai" in path:
        issues.append(
            "This looks like the Gemini OpenAI-compatible endpoint, but the profile uses "
            "native Gemini GenerateContent. Suggested fix: "
            f"`alysis profile convert {profile_name} --to compatibility`."
        )

    if protocol == GEMINI_INTERACTIONS_PROTOCOL and (
        host != "generativelanguage.googleapis.com" or "/openai" in path
    ):
        issues.append(
            "protocol=gemini_interactions is an experimental Gemini first-party protocol for "
            "generativelanguage.googleapis.com/v1beta/interactions, not OpenAI-compatible "
            "gateways. Suggested fix: use the stable `gemini` preset or "
            f"`alysis profile convert {profile_name} --to compatibility`."
        )

    if (
        protocol == OPENAI_COMPAT_PROTOCOL
        and host == "generativelanguage.googleapis.com"
        and "/openai" not in path
        and not explicit_first_party_compat
    ):
        issues.append(
            "This looks like the Gemini native API using compatibility mode. Recommended fix "
            f"for new first-party Gemini profiles: `alysis profile convert {profile_name} --to native`."
        )

    return issues


def _looks_like_unstable_model_alias(model: str) -> bool:
    normalized = model.strip().lower()
    return (
        normalized.endswith("-latest")
        or "-latest-" in normalized
        or normalized.endswith("-preview")
        or "-preview-" in normalized
    )


def _likely_non_chat_or_search_model(model: str) -> bool:
    normalized = model.strip().lower()
    markers = (
        "audio",
        "embedding",
        "embed",
        "image",
        "realtime",
        "speech",
        "tts",
        "transcribe",
        "whisper",
    )
    return any(marker in normalized for marker in markers)


def _likely_non_chat_stream_model(model: str) -> bool:
    normalized = model.strip().lower()
    markers = (
        "audio",
        "embedding",
        "embed",
        "image",
        "live",
        "realtime",
        "speech",
        "tts",
        "transcribe",
        "whisper",
    )
    return any(marker in normalized for marker in markers)


def _classify_live_validation_error(error_text: str, *, model: str) -> str:
    text = _redact_url_text(error_text)
    lowered = text.lower()
    if "401" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
        return "Provider rejected the API key during live validation. Check the active profile key."
    if "403" in lowered or "permission" in lowered or "forbidden" in lowered:
        return (
            "Provider denied live validation for this key/account. Check account permissions, "
            "region, billing, and model access."
        )
    if (
        "404" in lowered
        or "not found" in lowered
        or "model_not_found" in lowered
        or "does not exist" in lowered
        or "not supported" in lowered
    ):
        return (
            f"Provider could not use model {model!r}. Model availability can vary by account, "
            "region, and rollout; choose another model or set the relevant smoke/model override."
        )
    if "429" in lowered or "rate limit" in lowered or "quota" in lowered:
        return "Provider rate limit or quota blocked live validation. Retry later or use another model."
    if "timeout" in lowered or "timed out" in lowered:
        return "Live validation timed out. Retry later, increase timeout, or choose a faster model."
    return f"Live validation failed: {text}"


def _missing_api_key_issue(
    *,
    profile: ProfileSpec,
    provider_key: str,
    base_url: str,
    api_key: ApiKeyResolution,
) -> str | None:
    if profile.auth_provider:
        return None
    if api_key.key:
        return None
    if not _profile_requires_api_key(profile=profile, provider_key=provider_key, base_url=base_url):
        return None

    env_name = str(profile.api_key_env or "").strip()
    if env_name:
        return (
            f"API key is missing for active profile {profile.name!r}. Suggested fix: "
            f"export {env_name} or run `alysis config set-api-key`."
        )
    return (
        f"API key is missing for active profile {profile.name!r}. Suggested fix: "
        "run `alysis config set-api-key` or set a profile-specific api_key_env."
    )


def _profile_requires_api_key(
    *,
    profile: ProfileSpec,
    provider_key: str,
    base_url: str,
) -> bool:
    if str(profile.api_key_env or "").strip():
        return True
    host = _base_url_host(base_url)
    if not host:
        return False
    if _is_local_host(host):
        return False
    if provider_key in {
        "openai",
        "anthropic",
        "gemini",
        "qwen",
        "openrouter",
        "deepseek",
        "mistral",
        "xai",
    }:
        return True
    return "." in host


def _is_local_host(host: str) -> bool:
    normalized = host.strip("[]").lower()
    return normalized in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or normalized.endswith(
        ".localhost"
    )


def _web_search_backend_kind(runtime_adapter: str, configured_adapter: str) -> str:
    adapter = runtime_adapter or (
        "" if configured_adapter == AUTO_WEB_SEARCH_ADAPTER else configured_adapter
    )
    if not adapter:
        return "unavailable"
    if web_search_adapter_is_native(adapter):
        return "native/provider-hosted"
    if web_search_adapter_is_external(adapter):
        return "external"
    return "unknown"


def _base_url_host(base_url: str) -> str:
    try:
        return (urlsplit(base_url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def _base_url_path(base_url: str) -> str:
    try:
        return (urlsplit(base_url).path or "").rstrip("/").lower()
    except ValueError:
        return ""


_URL_RE = re.compile(r"https?://[^\s),;]+")


def _redact_url_text(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        return _base_url_host(match.group(0)) or "(url)"

    return _URL_RE.sub(_replace, str(value or ""))


def _redacted_api_key_source(api_key: ApiKeyResolution) -> str:
    source = str(api_key.source or "").strip()
    if not api_key.key:
        return "missing"
    if source.startswith("env:"):
        return f"env:{source.removeprefix('env:')} (redacted)"
    if source.startswith("stored:profile="):
        return f"stored profile:{source.removeprefix('stored:profile=')} (redacted)"
    if source in {"stored", "stored:legacy"}:
        return "stored legacy key (redacted)"
    return "configured (redacted)"


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
