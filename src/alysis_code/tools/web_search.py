from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from threading import RLock
from typing import Any

import httpx

from ..config import (
    AppConfig,
    resolve_web_search_adapter,
    resolve_web_search_mode,
)
from ..llm.openai_responses import OpenAIResponsesClient, ResponsesError
from ..llm.protocols import OPENAI_COMPAT_PROTOCOL
from ..llm.provider_limits import ProviderRetrySettings, resolve_provider_retry_settings
from ..profiles import get_active_profile
from ..provider_telemetry import record_web_search_call
from ..safety.safe_http import Resolver
from ..web_search_adapters import (
    web_search_adapter_is_external,
    web_search_adapter_is_native,
)
from .web_search_dashscope import (
    DashScopeChatSearchError,
    dashscope_chat_search,
)
from .web_search_ddgs import (
    DdgsSearchError,
    ddgs_search,
)
from .web_search_provider_adapters import (
    ProviderWebSearchError,
    anthropic_messages_search,
    cohere_web_search,
    gemini_grounding_search,
    groq_compound_search,
    minimax_coding_plan_search,
    mistral_conversations_search,
    moonshot_kimi_search,
    openrouter_web_search,
    perplexity_sonar_search,
    volcengine_web_search,
    zhipu_web_search,
)
from .web_search_runtime import (
    _ANTHROPIC_MESSAGES_PROVIDER as _ANTHROPIC_MESSAGES_PROVIDER,
)
from .web_search_runtime import (
    _COHERE_WEB_SEARCH_PROVIDER as _COHERE_WEB_SEARCH_PROVIDER,
)
from .web_search_runtime import (
    _DASHSCOPE_CHAT_PROVIDER as _DASHSCOPE_CHAT_PROVIDER,
)
from .web_search_runtime import (
    _DDGS_PROVIDER as _DDGS_PROVIDER,
)
from .web_search_runtime import (
    _GEMINI_GROUNDING_PROVIDER as _GEMINI_GROUNDING_PROVIDER,
)
from .web_search_runtime import (
    _GROQ_COMPOUND_PROVIDER as _GROQ_COMPOUND_PROVIDER,
)
from .web_search_runtime import (
    _MINIMAX_CODING_PLAN_PROVIDER as _MINIMAX_CODING_PLAN_PROVIDER,
)
from .web_search_runtime import (
    _MISTRAL_CONVERSATIONS_PROVIDER as _MISTRAL_CONVERSATIONS_PROVIDER,
)
from .web_search_runtime import (
    _MOONSHOT_KIMI_PROVIDER as _MOONSHOT_KIMI_PROVIDER,
)
from .web_search_runtime import (
    _OPENAI_RESPONSES_PROVIDER as _OPENAI_RESPONSES_PROVIDER,
)
from .web_search_runtime import (
    _OPENROUTER_WEB_MIN_TIMEOUT_S as _OPENROUTER_WEB_MIN_TIMEOUT_S,
)
from .web_search_runtime import (
    _OPENROUTER_WEB_PROVIDER as _OPENROUTER_WEB_PROVIDER,
)
from .web_search_runtime import (
    _PERPLEXITY_SONAR_PROVIDER as _PERPLEXITY_SONAR_PROVIDER,
)
from .web_search_runtime import (
    _TAVILY_PROVIDER as _TAVILY_PROVIDER,
)
from .web_search_runtime import (
    _VALID_WEB_SEARCH_PROVIDERS as _VALID_WEB_SEARCH_PROVIDERS,
)
from .web_search_runtime import (
    _VOLCENGINE_WEB_SEARCH_PROVIDER as _VOLCENGINE_WEB_SEARCH_PROVIDER,
)
from .web_search_runtime import (
    _WEB_SEARCH_ADAPTER_ENV as _WEB_SEARCH_ADAPTER_ENV,
)
from .web_search_runtime import (
    _WEB_SEARCH_MODE_AUTO as _WEB_SEARCH_MODE_AUTO,
)
from .web_search_runtime import (
    _WEB_SEARCH_MODE_EXTERNAL as _WEB_SEARCH_MODE_EXTERNAL,
)
from .web_search_runtime import (
    _WEB_SEARCH_MODE_NATIVE as _WEB_SEARCH_MODE_NATIVE,
)
from .web_search_runtime import (
    _WEB_SEARCH_MODE_OFF as _WEB_SEARCH_MODE_OFF,
)
from .web_search_runtime import (
    _WEB_SEARCH_PROVIDER_ENV as _WEB_SEARCH_PROVIDER_ENV,
)
from .web_search_runtime import (
    _XAI_RESPONSES_PROVIDER as _XAI_RESPONSES_PROVIDER,
)
from .web_search_runtime import (
    _ZHIPU_WEB_SEARCH_PROVIDER as _ZHIPU_WEB_SEARCH_PROVIDER,
)

# Preserve the existing Python API while sharing readiness with registration.
from .web_search_runtime import (
    WebSearchError as WebSearchError,
)
from .web_search_runtime import (
    WebSearchRuntimeConfig as WebSearchRuntimeConfig,
)
from .web_search_runtime import (
    WebSearchRuntimeStatus as WebSearchRuntimeStatus,
)
from .web_search_runtime import (
    _adapter_mode_incompatibility as _adapter_mode_incompatibility,
)
from .web_search_runtime import (
    _append_unique_note as _append_unique_note,
)
from .web_search_runtime import (
    _BackendReadiness as _BackendReadiness,
)
from .web_search_runtime import (
    _base_url_host as _base_url_host,
)
from .web_search_runtime import (
    _combine_notes as _combine_notes,
)
from .web_search_runtime import (
    _combined_api_key_available as _combined_api_key_available,
)
from .web_search_runtime import (
    _dashscope_model_supports_web_search as _dashscope_model_supports_web_search,
)
from .web_search_runtime import (
    _first_context_backend as _first_context_backend,
)
from .web_search_runtime import (
    _format_runtime_context as _format_runtime_context,
)
from .web_search_runtime import (
    _groq_search_model as _groq_search_model,
)
from .web_search_runtime import (
    _is_anthropic_base_url as _is_anthropic_base_url,
)
from .web_search_runtime import (
    _is_cohere_base_url as _is_cohere_base_url,
)
from .web_search_runtime import (
    _is_dashscope_base_url as _is_dashscope_base_url,
)
from .web_search_runtime import (
    _is_gemini_base_url as _is_gemini_base_url,
)
from .web_search_runtime import (
    _is_groq_base_url as _is_groq_base_url,
)
from .web_search_runtime import (
    _is_host_or_subdomain as _is_host_or_subdomain,
)
from .web_search_runtime import (
    _is_minimax_base_url as _is_minimax_base_url,
)
from .web_search_runtime import (
    _is_mistral_base_url as _is_mistral_base_url,
)
from .web_search_runtime import (
    _is_moonshot_base_url as _is_moonshot_base_url,
)
from .web_search_runtime import (
    _is_openrouter_base_url as _is_openrouter_base_url,
)
from .web_search_runtime import (
    _is_perplexity_base_url as _is_perplexity_base_url,
)
from .web_search_runtime import (
    _is_volcengine_base_url as _is_volcengine_base_url,
)
from .web_search_runtime import (
    _is_xai_base_url as _is_xai_base_url,
)
from .web_search_runtime import (
    _is_zhipu_base_url as _is_zhipu_base_url,
)
from .web_search_runtime import (
    _mode_unavailable_notes as _mode_unavailable_notes,
)
from .web_search_runtime import (
    _readiness_order as _readiness_order,
)
from .web_search_runtime import (
    _readiness_order_for_mode as _readiness_order_for_mode,
)
from .web_search_runtime import (
    _resolve_anthropic_messages_readiness as _resolve_anthropic_messages_readiness,
)
from .web_search_runtime import (
    _resolve_backend_readiness_map as _resolve_backend_readiness_map,
)
from .web_search_runtime import (
    _resolve_cohere_web_search_readiness as _resolve_cohere_web_search_readiness,
)
from .web_search_runtime import (
    _resolve_dashscope_chat_readiness as _resolve_dashscope_chat_readiness,
)
from .web_search_runtime import (
    _resolve_ddgs_readiness as _resolve_ddgs_readiness,
)
from .web_search_runtime import (
    _resolve_gemini_grounding_readiness as _resolve_gemini_grounding_readiness,
)
from .web_search_runtime import (
    _resolve_groq_compound_readiness as _resolve_groq_compound_readiness,
)
from .web_search_runtime import (
    _resolve_minimax_coding_plan_readiness as _resolve_minimax_coding_plan_readiness,
)
from .web_search_runtime import (
    _resolve_mistral_conversations_readiness as _resolve_mistral_conversations_readiness,
)
from .web_search_runtime import (
    _resolve_moonshot_kimi_readiness as _resolve_moonshot_kimi_readiness,
)
from .web_search_runtime import (
    _resolve_native_provider_readiness as _resolve_native_provider_readiness,
)
from .web_search_runtime import (
    _resolve_openai_readiness as _resolve_openai_readiness,
)
from .web_search_runtime import (
    _resolve_openrouter_web_readiness as _resolve_openrouter_web_readiness,
)
from .web_search_runtime import (
    _resolve_perplexity_sonar_readiness as _resolve_perplexity_sonar_readiness,
)
from .web_search_runtime import (
    _resolve_provider_override as _resolve_provider_override,
)
from .web_search_runtime import (
    _resolve_runtime_base_url as _resolve_runtime_base_url,
)
from .web_search_runtime import (
    _resolve_search_model_or_default as _resolve_search_model_or_default,
)
from .web_search_runtime import (
    _resolve_tavily_api_key as _resolve_tavily_api_key,
)
from .web_search_runtime import (
    _resolve_tavily_readiness as _resolve_tavily_readiness,
)
from .web_search_runtime import (
    _resolve_volcengine_web_search_readiness as _resolve_volcengine_web_search_readiness,
)
from .web_search_runtime import (
    _resolve_xai_responses_readiness as _resolve_xai_responses_readiness,
)
from .web_search_runtime import (
    _resolve_zhipu_web_search_readiness as _resolve_zhipu_web_search_readiness,
)
from .web_search_runtime import (
    _responses_auth_provider_for_base_url as _responses_auth_provider_for_base_url,
)
from .web_search_runtime import (
    _select_runtime_status as _select_runtime_status,
)
from .web_search_runtime import (
    _strict_runtime_error_message as _strict_runtime_error_message,
)
from .web_search_runtime import (
    resolve_web_search_runtime as resolve_web_search_runtime,
)
from .web_search_runtime import (
    resolve_web_search_runtime_status as resolve_web_search_runtime_status,
)
from .web_search_tavily import TavilySearchError, tavily_search

# web_search runs as a one-shot tool call that the agent loop re-issues on its own
# when needed, so a slow/failed provider call should fail fast rather than burn the
# full provider retry budget. A ~20s read timeout retried 5x is ~148s of dead air
# before the agent's own retry even gets a chance. Cap web_search provider retries
# low; a transient blip still gets one quick retry.
_WEB_SEARCH_MAX_PROVIDER_RETRIES = 1


# After a native backend hard-fails in ``auto`` mode and an external backend
# serves the result, remember that per session: re-attempting the broken native
# backend on every call wastes a full failed round-trip before each fallback.
# The preference is dropped as soon as the remembered backend stops working or
# the native runtime it shadowed is reconfigured (fingerprint mismatch), so a
# recovered or fixed native backend is retried on the next call after that.
_AUTO_FALLBACK_PREFERENCES_LOCK = RLock()
_AUTO_FALLBACK_PREFERENCE_BY_SESSION: dict[str, tuple[str, str]] = {}


def _web_search_runtime_fingerprint(runtime: WebSearchRuntimeConfig) -> str:
    key_digest = sha256(str(runtime.api_key or "").encode("utf-8")).hexdigest()[:16]
    return "\n".join(
        [
            str(runtime.provider or ""),
            str(runtime.base_url or ""),
            str(runtime.model or ""),
            key_digest,
        ]
    )


def _record_session_fallback_preference(
    session_id: str | None,
    provider: str,
    *,
    failed_runtime_fingerprint: str,
) -> None:
    if not session_id:
        return
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        _AUTO_FALLBACK_PREFERENCE_BY_SESSION[session_id] = (
            provider,
            failed_runtime_fingerprint,
        )


def _session_fallback_preference(
    session_id: str | None,
    *,
    current_runtime_fingerprint: str,
) -> str | None:
    """Return the remembered fallback backend, or None when it no longer applies.

    A preference recorded against a differently-configured native runtime is
    discarded: the user changing the web-search configuration mid-session must
    give the newly configured backend a fresh chance.
    """

    if not session_id:
        return None
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        stored = _AUTO_FALLBACK_PREFERENCE_BY_SESSION.get(session_id)
        if stored is None:
            return None
        provider, failed_fingerprint = stored
        if failed_fingerprint != current_runtime_fingerprint:
            _AUTO_FALLBACK_PREFERENCE_BY_SESSION.pop(session_id, None)
            return None
        return provider


def _clear_session_fallback_preference(session_id: str | None) -> None:
    if not session_id:
        return
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        _AUTO_FALLBACK_PREFERENCE_BY_SESSION.pop(session_id, None)


def _reset_web_search_session_state_for_tests() -> None:
    with _AUTO_FALLBACK_PREFERENCES_LOCK:
        _AUTO_FALLBACK_PREFERENCE_BY_SESSION.clear()


def _web_search_provider_retry_settings(cfg: AppConfig | None) -> ProviderRetrySettings:
    base = resolve_provider_retry_settings(cfg)
    capped = min(base.max_retries, _WEB_SEARCH_MAX_PROVIDER_RETRIES)
    if capped == base.max_retries:
        return base
    return ProviderRetrySettings(
        max_retries=capped,
        base_delay_seconds=base.base_delay_seconds,
        max_delay_seconds=base.max_delay_seconds,
    )


def _validate_query(raw_query: Any) -> str:
    query = str(raw_query or "").strip()
    if not query:
        raise WebSearchError("query must be a non-empty string.", recoverable=True)
    return query


def _validate_allowed_domains(raw_allowed_domains: Any) -> list[str] | None:
    if raw_allowed_domains is None:
        return None
    if not isinstance(raw_allowed_domains, list):
        raise WebSearchError(
            "allowed_domains must be an array of non-empty domain strings.",
            recoverable=True,
        )
    cleaned: list[str] = []
    for item in raw_allowed_domains:
        domain = str(item or "").strip().lower()
        if not domain:
            raise WebSearchError(
                "allowed_domains must contain only non-empty domain strings.",
                recoverable=True,
            )
        cleaned.append(domain)
    return cleaned


def _validate_max_sources(raw_max_sources: Any) -> int:
    try:
        max_sources = int(raw_max_sources if raw_max_sources is not None else 8)
    except (TypeError, ValueError) as e:
        raise WebSearchError(
            "max_sources must be an integer between 1 and 20.",
            recoverable=True,
        ) from e
    if max_sources < 1 or max_sources > 20:
        raise WebSearchError("max_sources must be an integer between 1 and 20.", recoverable=True)
    return max_sources


def _validate_external_web_access(raw_external_web_access: Any) -> bool:
    if raw_external_web_access is None:
        return True
    if not isinstance(raw_external_web_access, bool):
        raise WebSearchError("external_web_access must be a boolean.", recoverable=True)
    return raw_external_web_access


def _enforce_external_web_access_contract(*, provider: str, external_web_access: bool) -> None:
    if external_web_access or provider == _OPENAI_RESPONSES_PROVIDER:
        return
    raise WebSearchError(
        "external_web_access=false is supported only by the openai_responses web_search backend; "
        f"{provider} always uses external web access.",
        recoverable=True,
    )


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def _with_web_search_observability(
    *,
    result: dict[str, Any],
    runtime: WebSearchRuntimeConfig,
    cfg: AppConfig | None,
    fallback_occurred: bool = False,
) -> dict[str, Any]:
    enriched = dict(result)
    # Host wall-clock provenance: when this search actually ran. Lets the model
    # anchor "today"/"current" reasoning to real time instead of its training
    # prior (which otherwise poisons follow-up queries with a stale year).
    enriched["retrieved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    citations = enriched.get("citations")
    sources = enriched.get("sources")
    queries = enriched.get("queries")
    citation_count = len(citations) if isinstance(citations, list) else 0
    source_count = len(sources) if isinstance(sources, list) else 0
    query_count = len(queries) if isinstance(queries, list) else 0
    provider_hosted = web_search_adapter_is_native(runtime.provider)
    chat_protocol = _observability_chat_protocol(cfg)
    enriched["protocol"] = chat_protocol or runtime.provider
    enriched["chat_protocol"] = chat_protocol
    enriched["search_protocol"] = runtime.provider
    enriched["backend_adapter"] = runtime.provider
    enriched["provider_hosted_search"] = provider_hosted
    enriched["external_search_provider"] = (
        runtime.provider if web_search_adapter_is_external(runtime.provider) else None
    )
    enriched["citation_count"] = citation_count
    enriched["source_count"] = source_count
    record_web_search_call(
        protocol=chat_protocol or runtime.provider,
        provider_key=runtime.provider,
        model=runtime.model,
        web_search_mode=resolve_web_search_mode(cfg),
        web_search_adapter=resolve_web_search_adapter(cfg),
        provider_hosted_search=provider_hosted,
        external_provider_name=enriched["external_search_provider"],
        source_count=source_count,
        citation_count=citation_count,
        query_count=query_count,
        fallback_occurred=fallback_occurred,
    )
    return enriched


def _observability_chat_protocol(cfg: AppConfig | None) -> str:
    if cfg is None:
        return ""
    try:
        profile = get_active_profile(cfg)
    except Exception:  # noqa: BLE001
        return OPENAI_COMPAT_PROTOCOL
    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    return protocol or OPENAI_COMPAT_PROTOCOL


def _openai_responses_search(
    *,
    query: str,
    runtime: WebSearchRuntimeConfig,
    cfg: AppConfig | None,
    allowed_domains: list[str] | None,
    max_sources: int,
    external_web_access: bool,
    transport: httpx.BaseTransport | None,
    client_factory: Callable[..., OpenAIResponsesClient],
    session_id: str | None,
) -> dict[str, Any]:
    provider_auth = None
    if runtime.auth_provider:
        from ..provider_auth import create_provider_auth

        provider_auth = create_provider_auth(runtime.auth_provider, transport=transport)
    client = client_factory(
        base_url=str(runtime.base_url or ""),
        api_key=runtime.api_key,
        model=str(runtime.model or ""),
        timeout_s=runtime.timeout_s,
        transport=transport,
        provider_concurrency_caps=getattr(cfg, "provider_concurrency_caps", None),
        provider_retry_settings=_web_search_provider_retry_settings(cfg),
        provider_auth=provider_auth,
        session_id=session_id,
    )

    try:
        is_xai_runtime = runtime.provider == _XAI_RESPONSES_PROVIDER
        response = client.web_search(
            query=query,
            allowed_domains=allowed_domains,
            external_web_access=None if is_xai_runtime else external_web_access,
            include_source_details=not is_xai_runtime,
        )
    except ResponsesError as e:
        raise WebSearchError(str(e)) from e

    raw_sources = [
        {
            "url": str(source.url or "").strip(),
            "title": str(source.title or "").strip(),
        }
        for source in response.sources
        if str(source.url or "").strip()
    ]
    deduped_sources = _dedupe_sources(raw_sources)
    sources_truncated = len(deduped_sources) > max_sources
    final_sources = deduped_sources[:max_sources]

    citations = [
        {
            "title": str(citation.title or "").strip(),
            "url": str(citation.url or "").strip(),
            "start_index": citation.start_index,
            "end_index": citation.end_index,
        }
        for citation in response.citations
        if str(citation.url or "").strip()
    ]

    queries: list[str] = []
    for raw_query in response.queries:
        query_text = str(raw_query or "").strip()
        if query_text:
            queries.append(query_text)

    return {
        "query": query,
        "answer": str(response.answer or "").strip(),
        "citations": citations,
        "sources": final_sources,
        "queries": queries,
        "model": response.model or runtime.model,
        "backend": runtime.provider,
        "allowed_domains": allowed_domains or [],
        "external_web_access": external_web_access,
        "response_id": response.response_id,
        "sources_truncated": sources_truncated,
    }


def web_search(
    *,
    query: str,
    cfg: AppConfig | None,
    api_key: str | None = None,
    allowed_domains: list[str] | None = None,
    max_sources: int = 8,
    external_web_access: bool = True,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    client_factory: Callable[..., OpenAIResponsesClient] = OpenAIResponsesClient,
    session_id: str | None = None,
) -> dict[str, Any]:
    # Defense-in-depth runtime guard mirroring web_fetch: the master web-tools
    # switch blocks execution even if registration gating was bypassed.
    from ..config import resolve_web_tools_enabled

    if not resolve_web_tools_enabled(cfg):
        raise WebSearchError(
            "web_search is disabled by the web-tools policy "
            "(ALYSIS_WEB_TOOLS=off / web_tools_enabled=false). "
            "No network request was performed.",
            recoverable=False,
        )
    validated_query = _validate_query(query)
    validated_allowed_domains = _validate_allowed_domains(allowed_domains)
    validated_max_sources = _validate_max_sources(max_sources)
    validated_external_web_access = _validate_external_web_access(external_web_access)

    provider_override, _override_error = _resolve_provider_override(cfg=cfg, strict=True)
    runtime = resolve_web_search_runtime(cfg=cfg, api_key=api_key, strict=True)
    if runtime is None:
        raise WebSearchError("web_search is not enabled or configured.")
    _enforce_external_web_access_contract(
        provider=runtime.provider,
        external_web_access=validated_external_web_access,
    )

    def _run_tavily(tavily_runtime: WebSearchRuntimeConfig) -> dict[str, Any]:
        return tavily_search(
            query=validated_query,
            api_key=tavily_runtime.api_key,
            max_results=validated_max_sources,
            include_domains=validated_allowed_domains,
            timeout_s=tavily_runtime.timeout_s,
            transport=transport,
            resolver=resolver,
        )

    def _run_ddgs(ddgs_runtime: WebSearchRuntimeConfig) -> dict[str, Any]:
        return ddgs_search(
            query=validated_query,
            max_results=validated_max_sources,
            include_domains=validated_allowed_domains,
            timeout_s=ddgs_runtime.timeout_s,
        )

    def _fallback_to_external_or_raise(provider: str, error: Exception) -> dict[str, Any]:
        if provider_override is not None:
            raise WebSearchError(str(error)) from error
        mode = resolve_web_search_mode(cfg)
        if mode == _WEB_SEARCH_MODE_NATIVE:
            raise WebSearchError(
                f"native web search via {provider} failed and web_search_mode="
                "native disables external fallback backends (set web_search_mode "
                f"to 'auto' or 'external' to allow them): {error}"
            ) from error
        if mode != _WEB_SEARCH_MODE_AUTO or not validated_external_web_access:
            raise WebSearchError(str(error)) from error
        failures = [f"{provider}: {error}"]
        last_error: Exception = error
        if provider != _TAVILY_PROVIDER:
            tavily_runtime = _resolve_tavily_readiness(cfg=cfg).runtime
            if tavily_runtime is not None:
                try:
                    result = _with_web_search_observability(
                        result=_run_tavily(tavily_runtime),
                        runtime=tavily_runtime,
                        cfg=cfg,
                        fallback_occurred=True,
                    )
                except TavilySearchError as tavily_error:
                    failures.append(f"tavily: {tavily_error}")
                    last_error = tavily_error
                else:
                    _record_session_fallback_preference(
                        session_id,
                        _TAVILY_PROVIDER,
                        failed_runtime_fingerprint=_web_search_runtime_fingerprint(runtime),
                    )
                    return result
        if provider != _DDGS_PROVIDER:
            ddgs_runtime = _resolve_ddgs_readiness(cfg=cfg).runtime
            if ddgs_runtime is not None:
                try:
                    result = _with_web_search_observability(
                        result=_run_ddgs(ddgs_runtime),
                        runtime=ddgs_runtime,
                        cfg=cfg,
                        fallback_occurred=True,
                    )
                except DdgsSearchError as ddgs_error:
                    failures.append(f"ddgs: {ddgs_error}")
                    last_error = ddgs_error
                else:
                    _record_session_fallback_preference(
                        session_id,
                        _DDGS_PROVIDER,
                        failed_runtime_fingerprint=_web_search_runtime_fingerprint(runtime),
                    )
                    return result
        if len(failures) == 1:
            raise WebSearchError(str(error)) from error
        raise WebSearchError(
            "web_search failed across auto backends: " + "; ".join(failures)
        ) from last_error

    preferred_fallback = _session_fallback_preference(
        session_id,
        current_runtime_fingerprint=_web_search_runtime_fingerprint(runtime),
    )
    if (
        preferred_fallback in (_TAVILY_PROVIDER, _DDGS_PROVIDER)
        and preferred_fallback != runtime.provider
        and provider_override is None
        and resolve_web_search_mode(cfg) == _WEB_SEARCH_MODE_AUTO
        and validated_external_web_access
    ):
        preferred_runtime = (
            _resolve_tavily_readiness(cfg=cfg).runtime
            if preferred_fallback == _TAVILY_PROVIDER
            else _resolve_ddgs_readiness(cfg=cfg).runtime
        )
        if preferred_runtime is None:
            _clear_session_fallback_preference(session_id)
        else:
            try:
                return _with_web_search_observability(
                    result=(
                        _run_tavily(preferred_runtime)
                        if preferred_fallback == _TAVILY_PROVIDER
                        else _run_ddgs(preferred_runtime)
                    ),
                    runtime=preferred_runtime,
                    cfg=cfg,
                    fallback_occurred=True,
                )
            except (TavilySearchError, DdgsSearchError):
                # The remembered fallback stopped working; forget it and give
                # the configured backend another chance on this call.
                _clear_session_fallback_preference(session_id)

    if runtime.provider == _TAVILY_PROVIDER:
        try:
            return _with_web_search_observability(
                result=_run_tavily(runtime),
                runtime=runtime,
                cfg=cfg,
            )
        except TavilySearchError as e:
            return _fallback_to_external_or_raise(_TAVILY_PROVIDER, e)

    if runtime.provider == _DDGS_PROVIDER:
        try:
            return _with_web_search_observability(
                result=_run_ddgs(runtime),
                runtime=runtime,
                cfg=cfg,
            )
        except DdgsSearchError as e:
            raise WebSearchError(str(e)) from e

    if runtime.provider == _DASHSCOPE_CHAT_PROVIDER:
        try:
            return _with_web_search_observability(
                result=dashscope_chat_search(
                    query=validated_query,
                    base_url=str(runtime.base_url or ""),
                    api_key=runtime.api_key,
                    model=str(runtime.model or ""),
                    max_results=validated_max_sources,
                    include_domains=validated_allowed_domains,
                    timeout_s=runtime.timeout_s,
                    transport=transport,
                    resolver=resolver,
                    provider_concurrency_caps=getattr(cfg, "provider_concurrency_caps", None),
                    provider_retry_settings=_web_search_provider_retry_settings(cfg),
                ),
                runtime=runtime,
                cfg=cfg,
            )
        except DashScopeChatSearchError as e:
            return _fallback_to_external_or_raise(_DASHSCOPE_CHAT_PROVIDER, e)

    provider_retry_settings = _web_search_provider_retry_settings(cfg)
    native_kwargs = {
        "query": validated_query,
        "base_url": str(runtime.base_url or ""),
        "api_key": runtime.api_key,
        "model": str(runtime.model or ""),
        "max_results": validated_max_sources,
        "allowed_domains": validated_allowed_domains,
        "timeout_s": runtime.timeout_s,
        "transport": transport,
        "resolver": resolver,
        "provider_concurrency_caps": getattr(cfg, "provider_concurrency_caps", None),
        "provider_retry_settings": provider_retry_settings,
    }

    if runtime.provider == _ANTHROPIC_MESSAGES_PROVIDER:
        try:
            return _with_web_search_observability(
                result=anthropic_messages_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_ANTHROPIC_MESSAGES_PROVIDER, e)

    if runtime.provider == _GEMINI_GROUNDING_PROVIDER:
        try:
            return _with_web_search_observability(
                result=gemini_grounding_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_GEMINI_GROUNDING_PROVIDER, e)

    if runtime.provider == _OPENROUTER_WEB_PROVIDER:
        try:
            return _with_web_search_observability(
                result=openrouter_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_OPENROUTER_WEB_PROVIDER, e)

    if runtime.provider == _MOONSHOT_KIMI_PROVIDER:
        try:
            return _with_web_search_observability(
                result=moonshot_kimi_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_MOONSHOT_KIMI_PROVIDER, e)

    if runtime.provider == _ZHIPU_WEB_SEARCH_PROVIDER:
        try:
            return _with_web_search_observability(
                result=zhipu_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_ZHIPU_WEB_SEARCH_PROVIDER, e)

    if runtime.provider == _VOLCENGINE_WEB_SEARCH_PROVIDER:
        try:
            return _with_web_search_observability(
                result=volcengine_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_VOLCENGINE_WEB_SEARCH_PROVIDER, e)

    if runtime.provider == _PERPLEXITY_SONAR_PROVIDER:
        try:
            return _with_web_search_observability(
                result=perplexity_sonar_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_PERPLEXITY_SONAR_PROVIDER, e)

    if runtime.provider == _GROQ_COMPOUND_PROVIDER:
        try:
            return _with_web_search_observability(
                result=groq_compound_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_GROQ_COMPOUND_PROVIDER, e)

    if runtime.provider == _MISTRAL_CONVERSATIONS_PROVIDER:
        try:
            return _with_web_search_observability(
                result=mistral_conversations_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_MISTRAL_CONVERSATIONS_PROVIDER, e)

    if runtime.provider == _MINIMAX_CODING_PLAN_PROVIDER:
        try:
            return _with_web_search_observability(
                result=minimax_coding_plan_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_MINIMAX_CODING_PLAN_PROVIDER, e)

    if runtime.provider == _COHERE_WEB_SEARCH_PROVIDER:
        try:
            return _with_web_search_observability(
                result=cohere_web_search(**native_kwargs),
                runtime=runtime,
                cfg=cfg,
            )
        except ProviderWebSearchError as e:
            return _fallback_to_external_or_raise(_COHERE_WEB_SEARCH_PROVIDER, e)

    try:
        return _with_web_search_observability(
            result=_openai_responses_search(
                query=validated_query,
                runtime=runtime,
                cfg=cfg,
                allowed_domains=validated_allowed_domains,
                max_sources=validated_max_sources,
                external_web_access=validated_external_web_access,
                transport=transport,
                client_factory=client_factory,
                session_id=session_id,
            ),
            runtime=runtime,
            cfg=cfg,
        )
    except WebSearchError as openai_error:
        return _fallback_to_external_or_raise(runtime.provider, openai_error)
