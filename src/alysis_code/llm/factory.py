from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from ..config import resolve_web_search_adapter, resolve_web_search_mode
from ..model_reasoning import client_reasoning_settings
from ..model_registry import ModelRegistry, resolve_model_provider_key
from ..profile_presets import LOCAL_PROFILE_PRESET_KEYS, find_preset_for_profile
from ..profiles import (
    ProfileSpec,
    active_subscription_selection_ready,
    get_active_profile,
    resolve_effective_base_url,
)
from ..provider_auth import ProviderAuthError, create_provider_auth
from . import (
    anthropic_messages,
    gemini_generate_content,
    gemini_interactions,
    openai_compat,
    openai_responses,
)
from .base import ChatClient
from .cache_capabilities import resolve_effective_cache_capability
from .cache_policy import resolve_prompt_cache_policy
from .metadata import (
    build_provider_route_identity,
    canonicalize_extra_headers,
    credential_scope_fingerprint,
)
from .protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
    UnsupportedProtocolError,
    default_usage_contract_for_protocol,
    get_provider_protocol_capabilities,
    resolve_reasoning_trace_capability,
)
from .provider_limits import resolve_provider_retry_settings
from .types import BillingMode

if TYPE_CHECKING:
    from ..config import AppConfig


def make_llm_client(
    *,
    cfg: AppConfig,
    api_key: str,
    model: str,
    timeout_s: float | None = None,
    temperature: float = 0.2,
    prompt_cache_key: str | None = None,
    prompt_cache_retention: str | None = None,
    prompt_cache_namespace: str | None = None,
    enable_thinking: bool | None = None,
    reasoning_effort: str | None = None,
    transport: httpx.BaseTransport | None = None,
    profile: ProfileSpec | None = None,
    session_id: str | None = None,
) -> ChatClient:
    """Build an LLM client using the resolved provider profile protocol."""
    resolved_profile = profile or get_active_profile(cfg)
    protocol = str(resolved_profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    base_url = _resolve_base_url(cfg=cfg, profile=resolved_profile)
    request_headers = _session_routing_headers(
        base_url=base_url,
        extra_headers=resolved_profile.extra_headers,
        session_id=session_id,
    )
    provider_auth = None
    if resolved_profile.auth_provider:
        provider_auth = create_provider_auth(resolved_profile.auth_provider, transport=transport)
        if protocol != str(provider_auth.protocol or "").strip():
            raise UnsupportedProtocolError(
                f"Subscription adapter {resolved_profile.auth_provider!r} requires protocol "
                f"{provider_auth.protocol!r}, not {protocol!r}. Reconnect it through setup."
            )
        if base_url.rstrip("/") != str(provider_auth.base_url or "").rstrip("/"):
            raise UnsupportedProtocolError(
                f"Subscription adapter {resolved_profile.auth_provider!r} owns its endpoint. "
                "Reconnect it through setup instead of overriding base_url."
            )
        if protocol != OPENAI_RESPONSES_PROTOCOL:
            raise UnsupportedProtocolError(
                f"Alysis Code does not yet inject subscription authentication into "
                f"protocol {protocol!r}."
            )
        active_profile = get_active_profile(cfg)
        selection_ready = (
            active_subscription_selection_ready(cfg)
            if active_profile.name == resolved_profile.name
            else bool(
                resolved_profile.default_model and resolved_profile.reasoning_effort is not None
            )
        )
        if not selection_ready:
            raise ProviderAuthError(
                "Choose the subscription model and reasoning effort in "
                "/config → Default Model before using this connection."
            )
    resolved_reasoning_effort, resolved_enable_thinking = client_reasoning_settings(
        cfg,
        model=model,
        profile=resolved_profile,
        reasoning_effort=reasoning_effort,
        enable_thinking=enable_thinking,
        transport=transport,
    )
    preset = find_preset_for_profile(resolved_profile)
    provider_key = str(preset.provider_key or "").strip() if preset is not None else ""
    if not provider_key:
        provider_key = resolve_model_provider_key(
            cfg=cfg,
            model_name=model,
            base_url=base_url,
            profile_name=resolved_profile.name,
        )
    protocol = resolve_model_protocol(provider_key=provider_key, model=model, protocol=protocol)
    capabilities = get_provider_protocol_capabilities(
        provider_key=provider_key,
        protocol=protocol,
    )
    usage_contract = (
        capabilities.usage_contract
        if capabilities is not None
        else default_usage_contract_for_protocol(protocol)
    )
    # A loopback base URL only implies a free local runtime when no credential is
    # configured. Keyed gateways (LiteLLM, corporate proxies) front paid upstream
    # APIs over localhost too, and suppressing their cost would under-report spend.
    loopback_host = (urlparse(base_url).hostname or "").strip().lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    unkeyed_loopback = loopback_host and not str(api_key or "").strip()
    if resolved_profile.auth_provider:
        billing_mode = BillingMode.SUBSCRIPTION
    elif (
        capabilities is not None
        and capabilities.usage_contract.billing_mode == BillingMode.SUBSCRIPTION
    ):
        billing_mode = BillingMode.SUBSCRIPTION
    elif provider_key in LOCAL_PROFILE_PRESET_KEYS or unkeyed_loopback:
        billing_mode = BillingMode.LOCAL
    elif provider_key == "alysis":
        billing_mode = BillingMode.INCLUDED
    elif provider_key:
        billing_mode = BillingMode.METERED_API
    else:
        billing_mode = BillingMode.UNKNOWN
    usage_contract = replace(usage_contract, billing_mode=billing_mode)
    model_meta = ModelRegistry(cfg=cfg, api_key=api_key).get(
        model,
        include_provider_auth=False,
    )
    model_supports_reasoning = model_meta.supports_reasoning
    model_capability_source = model_meta.field_sources.get("supports_reasoning")
    if resolved_profile.auth_provider and resolved_profile.reasoning_effort is not None:
        model_supports_reasoning = True
        model_capability_source = "profile:reasoning_effort"
    reasoning_trace_capability = resolve_reasoning_trace_capability(
        provider_key=provider_key,
        protocol=protocol,
        adapter_override=resolved_profile.reasoning_trace_adapter,
        model_supports_reasoning=model_supports_reasoning,
        model_capability_source=model_capability_source,
    )
    cache_capability = resolve_effective_cache_capability(
        provider_key=provider_key,
        protocol=protocol,
        model=model,
        base_url=base_url,
        transport_capabilities=capabilities,
        auth_cache_capability=getattr(provider_auth, "cache_capability", None),
        preset_cache_capability=(preset.cache_capability if preset is not None else None),
        profile_cache_capability=resolved_profile.cache_capability,
    )
    cache_policy = resolve_prompt_cache_policy(
        cfg=cfg,
        capabilities=capabilities,
        provider_key=provider_key,
        protocol=protocol,
        model=model,
        prompt_cache_key=prompt_cache_key,
        prompt_cache_retention=prompt_cache_retention,
        prompt_cache_namespace=prompt_cache_namespace,
        cache_capability=cache_capability,
    )
    cache_policy_metadata = cache_policy.telemetry_metadata()
    credential_scope = credential_scope_fingerprint(api_key)
    if provider_auth is not None:
        auth_scope = getattr(provider_auth, "route_credential_scope", None)
        if callable(auth_scope):
            credential_scope = str(auth_scope() or "").strip() or credential_scope
    protocol_revision = ""
    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        protocol_revision = anthropic_messages.ANTHROPIC_MESSAGES_ROUTE_REVISION
    elif protocol == GEMINI_INTERACTIONS_PROTOCOL:
        protocol_revision = gemini_interactions.GEMINI_INTERACTIONS_ROUTE_REVISION
    route_identity = build_provider_route_identity(
        protocol=protocol,
        base_url=base_url,
        provider_key=provider_key,
        model=model,
        profile_name=resolved_profile.name,
        auth_provider=resolved_profile.auth_provider,
        credential_scope=credential_scope,
        routing_headers=request_headers,
        routing_fields=dict(cache_policy.request_field_values),
        reasoning_state_adapter=resolved_profile.reasoning_trace_adapter,
        protocol_revision=protocol_revision,
        session_scope=credential_scope_fingerprint(session_id),
    )
    if protocol == OPENAI_RESPONSES_PROTOCOL:
        return _attach_reasoning_trace_capability(
            openai_responses.OpenAIResponsesClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_s=60.0 if timeout_s is None else timeout_s,
                temperature=temperature,
                prompt_cache_key=cache_policy.prompt_cache_key,
                prompt_cache_retention=cache_policy.prompt_cache_retention,
                prompt_cache_policy_metadata=cache_policy_metadata,
                enable_thinking=resolved_enable_thinking,
                reasoning_effort=resolved_reasoning_effort,
                transport=transport,
                extra_headers=request_headers,
                provider_key=provider_key,
                web_search_mode=resolve_web_search_mode(cfg),
                web_search_adapter=resolve_web_search_adapter(cfg),
                provider_concurrency_caps=cfg.provider_concurrency_caps,
                provider_retry_settings=resolve_provider_retry_settings(cfg),
                provider_auth=provider_auth,
                session_id=session_id,
                usage_contract=usage_contract,
                route_identity=route_identity,
                stream_no_progress_timeout_s=cfg.llm_stream_no_progress_timeout_s,
                inflight_deadline_grace_s=(cfg.subagent_orchestration.inflight_deadline_grace_s),
            ),
            reasoning_trace_capability,
        )

    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        return _attach_reasoning_trace_capability(
            anthropic_messages.AnthropicMessagesClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
                default_max_tokens=model_meta.max_output_tokens,
                timeout_s=60.0 if timeout_s is None else timeout_s,
                temperature=temperature,
                prompt_cache_key=cache_policy.prompt_cache_key,
                prompt_cache_retention=cache_policy.prompt_cache_retention,
                prompt_cache_policy_metadata=cache_policy_metadata,
                enable_thinking=resolved_enable_thinking,
                reasoning_effort=resolved_reasoning_effort,
                transport=transport,
                extra_headers=request_headers,
                provider_key=provider_key,
                web_search_mode=resolve_web_search_mode(cfg),
                web_search_adapter=resolve_web_search_adapter(cfg),
                prompt_cache_control_enabled=cache_policy.anthropic_cache_control_enabled,
                prompt_cache_control_ttl=cache_policy.anthropic_cache_control_ttl,
                provider_concurrency_caps=cfg.provider_concurrency_caps,
                provider_retry_settings=resolve_provider_retry_settings(cfg),
                usage_contract=usage_contract,
                route_identity=route_identity,
            ),
            reasoning_trace_capability,
        )

    if protocol == GEMINI_GENERATE_CONTENT_PROTOCOL:
        return _attach_reasoning_trace_capability(
            gemini_generate_content.GeminiGenerateContentClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_s=60.0 if timeout_s is None else timeout_s,
                temperature=temperature,
                prompt_cache_key=cache_policy.prompt_cache_key,
                prompt_cache_retention=cache_policy.prompt_cache_retention,
                prompt_cache_policy_metadata=cache_policy_metadata,
                enable_thinking=resolved_enable_thinking,
                reasoning_effort=resolved_reasoning_effort,
                transport=transport,
                extra_headers=request_headers,
                provider_key=provider_key,
                web_search_mode=resolve_web_search_mode(cfg),
                web_search_adapter=resolve_web_search_adapter(cfg),
                explicit_cached_content_enabled=(
                    cache_policy.gemini_explicit_cached_content_enabled
                ),
                cached_content_ttl=cache_policy.gemini_cached_content_ttl,
                cached_content_min_tokens=cache_policy.min_cacheable_tokens,
                provider_concurrency_caps=cfg.provider_concurrency_caps,
                provider_retry_settings=resolve_provider_retry_settings(cfg),
                usage_contract=usage_contract,
                route_identity=route_identity,
            ),
            reasoning_trace_capability,
        )

    if protocol == GEMINI_INTERACTIONS_PROTOCOL:
        if not gemini_interactions.gemini_interactions_enabled(cfg):
            raise UnsupportedProtocolError(
                gemini_interactions.gemini_interactions_disabled_message()
            )
        return _attach_reasoning_trace_capability(
            gemini_interactions.GeminiInteractionsClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_s=60.0 if timeout_s is None else timeout_s,
                temperature=temperature,
                prompt_cache_key=cache_policy.prompt_cache_key,
                prompt_cache_retention=cache_policy.prompt_cache_retention,
                prompt_cache_policy_metadata=cache_policy_metadata,
                enable_thinking=resolved_enable_thinking,
                reasoning_effort=resolved_reasoning_effort,
                transport=transport,
                extra_headers=request_headers,
                provider_key=provider_key,
                provider_concurrency_caps=cfg.provider_concurrency_caps,
                provider_retry_settings=resolve_provider_retry_settings(cfg),
                usage_contract=usage_contract,
                route_identity=route_identity,
            ),
            reasoning_trace_capability,
        )

    if protocol != OPENAI_COMPAT_PROTOCOL:
        raise UnsupportedProtocolError(
            f"Profile {resolved_profile.name!r} uses recognized LLM protocol {protocol!r}, "
            "but Alysis Code does not implement a native chat client for that protocol yet. "
            "Use protocol='openai_compat', protocol='openai_responses', "
            "protocol='anthropic_messages', protocol='gemini_generate_content', or enable the "
            "experimental protocol='gemini_interactions' text-only prototype."
        )
    return _attach_reasoning_trace_capability(
        openai_compat.OpenAICompatClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_s=60.0 if timeout_s is None else timeout_s,
            temperature=temperature,
            prompt_cache_key=cache_policy.prompt_cache_key,
            prompt_cache_retention=cache_policy.prompt_cache_retention,
            prompt_cache_request_field_values=dict(cache_policy.request_field_values),
            prompt_cache_policy_metadata=cache_policy_metadata,
            enable_thinking=resolved_enable_thinking,
            reasoning_effort=resolved_reasoning_effort,
            transport=transport,
            extra_headers=request_headers,
            provider_key=provider_key,
            reasoning_trace_adapter=resolved_profile.reasoning_trace_adapter,
            usage_contract=usage_contract,
            provider_concurrency_caps=cfg.provider_concurrency_caps,
            provider_retry_settings=resolve_provider_retry_settings(cfg),
            stream_no_progress_timeout_s=cfg.llm_stream_no_progress_timeout_s,
            inflight_deadline_grace_s=(cfg.subagent_orchestration.inflight_deadline_grace_s),
            route_identity=route_identity,
            stream_assembly=cfg.llm_stream_assembly,
        ),
        reasoning_trace_capability,
    )


def resolve_model_protocol(*, provider_key: str | None, model: str, protocol: str) -> str:
    """Resolve model-specific transports for both initial connections and live switches."""
    # Hosted OpenAI models need Responses for reasoning with function tools.
    # Keep the managed endpoint and Alysis device credential on this route.
    if provider_key == "alysis" and model in {"gpt-6-luna", "gpt-6.1-sol"}:
        return OPENAI_RESPONSES_PROTOCOL
    if provider_key == "openai" and model == "gpt-6.1-sol":
        return OPENAI_RESPONSES_PROTOCOL
    if provider_key == "alysis" and model in {"claude-sonnet-5-5", "claude-haiku-5-5"}:
        return ANTHROPIC_MESSAGES_PROTOCOL
    return protocol


def _attach_reasoning_trace_capability(client: ChatClient, capability: object) -> ChatClient:
    """Expose the resolved trace dialect without widening every client constructor."""

    client.reasoning_trace_capability = capability  # type: ignore[attr-defined]
    return client


def _resolve_base_url(*, cfg: AppConfig, profile: ProfileSpec) -> str:
    return resolve_effective_base_url(cfg=cfg, profile=profile)


def _session_routing_headers(
    *, base_url: str, extra_headers: dict[str, str], session_id: str | None
) -> dict[str, str]:
    """Apply endpoint requirements without persisting per-conversation state.

    OpenCode Zen/Go requires session affinity across every supported protocol.
    Main and child sessions supply their own retained identity. A standalone
    auxiliary client gets one identity for its lifetime, including retries.
    Explicit user headers remain overrides. Profile names do not identify hosts.
    https://opencode.ai/docs/go/#where-can-i-use-it
    """
    headers = canonicalize_extra_headers(extra_headers)
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme == "https"
        and endpoint.hostname == "opencode.ai"
        and endpoint.path.rstrip("/") in {"/zen/v1", "/zen/go/v1"}
    ):
        identity = credential_scope_fingerprint(session_id) or uuid4().hex
        headers.setdefault("x-opencode-session", f"alysis-{identity}")
    return headers
