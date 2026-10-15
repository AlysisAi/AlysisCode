from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from ..config import ConfigError
from .types import BillingMode, ReasoningOutputKind, UsageConfidence, UsageContract

OPENAI_COMPAT_PROTOCOL = "openai_compat"
OPENAI_RESPONSES_PROTOCOL = "openai_responses"
ANTHROPIC_MESSAGES_PROTOCOL = "anthropic_messages"
GEMINI_GENERATE_CONTENT_PROTOCOL = "gemini_generate_content"
GEMINI_INTERACTIONS_PROTOCOL = "gemini_interactions"

SUPPORTED_LLM_PROTOCOLS: frozenset[str] = frozenset(
    {
        OPENAI_COMPAT_PROTOCOL,
        OPENAI_RESPONSES_PROTOCOL,
        ANTHROPIC_MESSAGES_PROTOCOL,
        GEMINI_GENERATE_CONTENT_PROTOCOL,
        GEMINI_INTERACTIONS_PROTOCOL,
    }
)

_PROVIDER_CAPABILITY_ALIASES: dict[str, str] = {
    "vertex_ai": "gemini",
    "vertex_ai-language-models": "gemini",
    "vertex-ai-language-models": "gemini",
    "google": "gemini",
    "google_ai": "gemini",
    "google-ai": "gemini",
}

_PROTOCOL_INPUT_TOKEN_COUNT_STRATEGIES: dict[str, str] = {
    OPENAI_COMPAT_PROTOCOL: "openai_compat_provider_payload",
    OPENAI_RESPONSES_PROTOCOL: "openai_responses",
    ANTHROPIC_MESSAGES_PROTOCOL: "anthropic_messages",
    GEMINI_GENERATE_CONTENT_PROTOCOL: "gemini_count_tokens",
    GEMINI_INTERACTIONS_PROTOCOL: "gemini_count_tokens_projection",
}


@dataclass(frozen=True)
class ReasoningTraceCapability:
    """How a provider route can supply user-visible reasoning trace content.

    This describes display capability, not whether the model reasons. Model
    effort remains an independent request setting.
    """

    adapter: str = "none"
    output_kind: ReasoningOutputKind | None = None
    supports_streaming: bool = False
    supports_buffered: bool = False
    requestable: bool = False
    continuation_state: str = "none"
    model_supports_reasoning: bool | None = None
    resolution_source: str = "provider_protocol"

    @property
    def has_safe_summary(self) -> bool:
        return self.output_kind == ReasoningOutputKind.SUMMARY

    @property
    def has_visible_reasoning(self) -> bool:
        return self.output_kind in {
            ReasoningOutputKind.SUMMARY,
            ReasoningOutputKind.PROVIDER_REASONING,
        }


def reasoning_trace_callback(
    callback: Callable[[str], None] | None,
    *,
    capability: ReasoningTraceCapability | None,
    stream: bool,
) -> Callable[[str], None] | None:
    """Honor the resolved route's display policy at the transport boundary.

    Standalone clients without factory metadata retain callback-based opt-in.
    Configured clients must also honor adapter overrides and model capability,
    including callers outside the main agent loop. This never changes effort
    or provider continuation state.
    """
    if callback is None or capability is None:
        return callback
    supported = capability.supports_streaming if stream else capability.supports_buffered
    return callback if capability.has_visible_reasoning and supported else None


def reasoning_summary_callback(
    callback: Callable[[str], None] | None,
    *,
    capability: ReasoningTraceCapability | None,
    stream: bool,
) -> Callable[[str], None] | None:
    """Honor the resolved route's display policy at the transport boundary.

    Standalone clients without factory metadata retain callback-based opt-in.
    Configured clients must also honor adapter overrides and model capability,
    including callers outside the main agent loop. This never changes effort
    or provider continuation state.
    """
    if callback is None or capability is None:
        return callback
    supported = capability.supports_streaming if stream else capability.supports_buffered
    return callback if capability.has_safe_summary and supported else None


@dataclass(frozen=True)
class ProviderProtocolCapabilities:
    """Provider capabilities separated by chat protocol and web-search adapter surface.

    The `supports_streaming`, `supports_tool_calling`, and `supports_structured_outputs`
    fields describe the chat transport named by `protocol`. The web-search fields describe
    the separate `web_search` runtime adapter that may exist even while chat uses
    `openai_compat`.
    """

    provider_key: str
    protocol: str
    supports_streaming: bool
    supports_tool_calling: bool
    supports_structured_outputs: bool
    supports_provider_hosted_web_search_adapter: bool
    default_web_search_adapter: str
    unsupported_parameters: tuple[str, ...] = ()
    quirks: tuple[str, ...] = ()
    supports_forced_tool_choice: bool = True
    cache_strategy: str = "none"
    supports_prompt_cache_key: bool = False
    supports_prompt_cache_retention: bool = False
    supports_cache_control: bool = False
    supports_explicit_cached_content: bool = False
    reports_cache_read_tokens: bool = False
    reports_cache_write_tokens: bool = False
    usage_contract: UsageContract = UsageContract()
    reasoning_trace: ReasoningTraceCapability = ReasoningTraceCapability()

    @property
    def usage_counts_authoritative(self) -> bool:
        """Backward-compatible view of the normalized response usage contract."""
        return self.usage_contract.response_usage_authoritative

    @property
    def supports_native_web_search(self) -> bool:
        """Backward-compatible alias for the provider-hosted web-search adapter flag."""
        return self.supports_provider_hosted_web_search_adapter


class UnsupportedProtocolError(ConfigError):
    """Raised when a recognized provider protocol lacks a chat client implementation."""


_REASONING_TRACE_CAPABILITIES_BY_ADAPTER: dict[str, ReasoningTraceCapability] = {
    "none": ReasoningTraceCapability(),
    "openai_compat_passive": ReasoningTraceCapability(
        adapter="openai_compat_passive",
        supports_streaming=True,
        supports_buffered=True,
    ),
    "openai_responses_summary": ReasoningTraceCapability(
        adapter="openai_responses_summary",
        output_kind=ReasoningOutputKind.SUMMARY,
        supports_streaming=True,
        supports_buffered=True,
        requestable=True,
        continuation_state="opaque",
    ),
    "anthropic_messages_summary": ReasoningTraceCapability(
        adapter="anthropic_messages_summary",
        output_kind=ReasoningOutputKind.SUMMARY,
        supports_streaming=True,
        supports_buffered=True,
        requestable=True,
        continuation_state="sensitive",
    ),
    "gemini_thought_summary": ReasoningTraceCapability(
        adapter="gemini_thought_summary",
        output_kind=ReasoningOutputKind.SUMMARY,
        supports_streaming=True,
        supports_buffered=True,
        requestable=True,
        continuation_state="opaque",
    ),
    "gemini_interactions_summary": ReasoningTraceCapability(
        adapter="gemini_interactions_summary",
        output_kind=ReasoningOutputKind.SUMMARY,
        supports_streaming=False,
        supports_buffered=True,
        requestable=True,
        continuation_state="opaque",
    ),
    "openrouter_reasoning": ReasoningTraceCapability(
        adapter="openrouter_reasoning",
        output_kind=ReasoningOutputKind.SUMMARY,
        supports_streaming=True,
        supports_buffered=True,
        continuation_state="sensitive",
    ),
    "deepseek_reasoning": ReasoningTraceCapability(
        adapter="deepseek_reasoning",
        output_kind=ReasoningOutputKind.PROVIDER_REASONING,
        supports_streaming=True,
        supports_buffered=True,
        continuation_state="sensitive",
    ),
    "dashscope_thinking": ReasoningTraceCapability(
        adapter="dashscope_thinking",
        output_kind=ReasoningOutputKind.PROVIDER_REASONING,
        supports_streaming=True,
        supports_buffered=True,
        continuation_state="sensitive",
    ),
    "mistral_thinking": ReasoningTraceCapability(
        adapter="mistral_thinking",
        output_kind=ReasoningOutputKind.PROVIDER_REASONING,
        supports_streaming=True,
        supports_buffered=True,
        continuation_state="sensitive",
    ),
    "moonshot_reasoning": ReasoningTraceCapability(
        adapter="moonshot_reasoning",
        output_kind=ReasoningOutputKind.PROVIDER_REASONING,
        supports_streaming=True,
        supports_buffered=True,
        continuation_state="sensitive",
    ),
    "nvidia_reasoning": ReasoningTraceCapability(
        adapter="nvidia_reasoning",
        output_kind=ReasoningOutputKind.PROVIDER_REASONING,
        supports_streaming=True,
        supports_buffered=True,
        continuation_state="sensitive",
    ),
    "glm_reasoning": ReasoningTraceCapability(
        adapter="glm_reasoning",
        output_kind=ReasoningOutputKind.PROVIDER_REASONING,
        supports_streaming=True,
        supports_buffered=True,
        continuation_state="sensitive",
    ),
}

SUPPORTED_REASONING_TRACE_ADAPTERS: frozenset[str] = frozenset(
    {"auto", *_REASONING_TRACE_CAPABILITIES_BY_ADAPTER}
)
_REASONING_TRACE_ADAPTERS_BY_PROTOCOL: dict[str, frozenset[str]] = {
    OPENAI_COMPAT_PROTOCOL: frozenset(
        {
            "auto",
            "none",
            "openai_compat_passive",
            "openrouter_reasoning",
            "deepseek_reasoning",
            "dashscope_thinking",
            "mistral_thinking",
            "moonshot_reasoning",
            "nvidia_reasoning",
            "glm_reasoning",
        }
    ),
    OPENAI_RESPONSES_PROTOCOL: frozenset({"auto", "none", "openai_responses_summary"}),
    ANTHROPIC_MESSAGES_PROTOCOL: frozenset({"auto", "none", "anthropic_messages_summary"}),
    GEMINI_GENERATE_CONTENT_PROTOCOL: frozenset({"auto", "none", "gemini_thought_summary"}),
    GEMINI_INTERACTIONS_PROTOCOL: frozenset({"auto", "none", "gemini_interactions_summary"}),
}

_OPENAI_COMPAT_REASONING_ADAPTER_BY_PROVIDER: dict[str, str] = {
    "alysis": "openrouter_reasoning",
    "openrouter": "openrouter_reasoning",
    "deepseek": "deepseek_reasoning",
    "qwen": "dashscope_thinking",
    "dashscope": "dashscope_thinking",
    "mistral": "mistral_thinking",
    "moonshot": "moonshot_reasoning",
    "nvidia": "nvidia_reasoning",
    "kimi_code": "moonshot_reasoning",
    "kimi-code": "moonshot_reasoning",
    "zhipu": "glm_reasoning",
    "zai": "glm_reasoning",
    "zai_coding_plan": "glm_reasoning",
}


def normalize_reasoning_trace_adapter(value: str | None) -> str:
    normalized = str(value or "auto").strip().lower().replace("-", "_") or "auto"
    if normalized not in SUPPORTED_REASONING_TRACE_ADAPTERS:
        allowed = ", ".join(sorted(SUPPORTED_REASONING_TRACE_ADAPTERS))
        raise ConfigError(f"reasoning_trace_adapter must be one of: {allowed}")
    return normalized


def validate_reasoning_trace_adapter_for_protocol(
    *,
    protocol: str,
    adapter: str | None,
) -> str:
    normalized_protocol = str(protocol or "").strip().lower()
    normalized_adapter = normalize_reasoning_trace_adapter(adapter)
    allowed = _REASONING_TRACE_ADAPTERS_BY_PROTOCOL.get(normalized_protocol)
    if allowed is not None and normalized_adapter not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigError(
            f"reasoning_trace_adapter {normalized_adapter!r} is not valid for protocol "
            f"{normalized_protocol!r}; choose one of: {choices}"
        )
    return normalized_adapter


def resolve_reasoning_trace_capability(
    *,
    provider_key: str,
    protocol: str,
    adapter_override: str | None = None,
    model_supports_reasoning: bool | None = None,
    model_capability_source: str | None = None,
) -> ReasoningTraceCapability:
    """Resolve a safe display adapter for a concrete provider route.

    Unknown OpenAI-compatible endpoints are passive: conventional fields may
    be retained for continuity, but Alysis Code injects no guessed request fields
    and displays no raw chain-of-thought.
    """

    override = validate_reasoning_trace_adapter_for_protocol(
        protocol=protocol,
        adapter=adapter_override,
    )
    if override != "auto":
        return replace(
            _REASONING_TRACE_CAPABILITIES_BY_ADAPTER[override],
            model_supports_reasoning=model_supports_reasoning,
            resolution_source="profile_override",
        )

    capabilities = get_provider_protocol_capabilities(
        provider_key=provider_key,
        protocol=protocol,
    )
    if capabilities is not None and capabilities.reasoning_trace.adapter != "none":
        resolved = capabilities.reasoning_trace
    else:
        normalized_protocol = str(protocol or "").strip().lower()
        if normalized_protocol != OPENAI_COMPAT_PROTOCOL:
            resolved = _REASONING_TRACE_CAPABILITIES_BY_ADAPTER["none"]
        else:
            normalized_provider = str(provider_key or "").strip().lower()
            normalized_provider = _PROVIDER_CAPABILITY_ALIASES.get(
                normalized_provider,
                normalized_provider,
            )
            adapter = _OPENAI_COMPAT_REASONING_ADAPTER_BY_PROVIDER.get(
                normalized_provider,
                "openai_compat_passive",
            )
            resolved = _REASONING_TRACE_CAPABILITIES_BY_ADAPTER[adapter]

    normalized_protocol = str(protocol or "").strip().lower()
    source = str(model_capability_source or "").strip() or "unknown_model_metadata"
    if model_supports_reasoning is False:
        fallback_adapter = (
            "openai_compat_passive" if normalized_protocol == OPENAI_COMPAT_PROTOCOL else "none"
        )
        return replace(
            _REASONING_TRACE_CAPABILITIES_BY_ADAPTER[fallback_adapter],
            model_supports_reasoning=False,
            resolution_source=f"model_metadata:{source}",
        )
    return replace(
        resolved,
        model_supports_reasoning=model_supports_reasoning,
        resolution_source=(
            f"model_metadata:{source}" if model_supports_reasoning is True else "provider_protocol"
        ),
    )


def default_usage_contract_for_protocol(protocol: str) -> UsageContract:
    """Return conservative guarantees for an otherwise unknown provider route."""

    normalized_protocol = str(protocol or "").strip().lower()
    return UsageContract(
        response_usage_confidence=UsageConfidence.REPORTED,
        input_token_count_strategy=_PROTOCOL_INPUT_TOKEN_COUNT_STRATEGIES.get(
            normalized_protocol,
            "none",
        ),
    )


PROVIDER_PROTOCOL_CAPABILITIES: tuple[ProviderProtocolCapabilities, ...] = (
    ProviderProtocolCapabilities(
        provider_key="openai",
        protocol=OPENAI_COMPAT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="openai_compat_passive",
            output_kind=None,
            supports_streaming=True,
            supports_buffered=True,
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="openai_compat_provider_payload",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="openai_responses",
        cache_strategy="openai_prompt_cache",
        supports_prompt_cache_key=True,
        supports_prompt_cache_retention=True,
        reports_cache_read_tokens=True,
        reports_cache_write_tokens=True,
        quirks=("Alysis Code's current chat runtime uses Chat Completions-compatible requests.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="alysis",
        protocol=OPENAI_COMPAT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="openrouter_reasoning",
            output_kind=ReasoningOutputKind.PROVIDER_REASONING,
            supports_streaming=True,
            supports_buffered=True,
        ),
        # Declaring cache support must retain the existing local payload
        # estimator and conservative response-usage confidence for this route.
        usage_contract=default_usage_contract_for_protocol(OPENAI_COMPAT_PROTOCOL),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="none",
        # The gateway does not forward OpenAI affinity/retention fields.
        cache_strategy="implicit_provider",
        reports_cache_read_tokens=True,
        quirks=("Hosted DeepSeek and GLM use provider-managed prompt caching.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="openai_responses_summary",
            output_kind=ReasoningOutputKind.SUMMARY,
            supports_streaming=True,
            supports_buffered=True,
            requestable=True,
            continuation_state="opaque",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="openai_responses",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=True,
        default_web_search_adapter="openai_responses",
        cache_strategy="openai_prompt_cache",
        supports_prompt_cache_key=True,
        supports_prompt_cache_retention=True,
        reports_cache_read_tokens=True,
        reports_cache_write_tokens=True,
        quirks=("Native OpenAI Responses chat supports buffered and SSE streaming responses.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="alysis",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="openai_responses_summary",
            output_kind=ReasoningOutputKind.SUMMARY,
            supports_streaming=True,
            supports_buffered=True,
            requestable=True,
            continuation_state="opaque",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="none",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="none",
        cache_strategy="implicit_provider",
        reports_cache_read_tokens=True,
        reports_cache_write_tokens=True,
        quirks=("Hosted Luna supports stateless text and function tools with shared credits.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="alysis",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="anthropic_messages_summary",
            output_kind=ReasoningOutputKind.SUMMARY,
            supports_streaming=True,
            supports_buffered=True,
            requestable=True,
            continuation_state="sensitive",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="none",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=False,
        supports_forced_tool_choice=False,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="none",
        cache_strategy="anthropic_cache_control",
        supports_cache_control=True,
        reports_cache_read_tokens=True,
        reports_cache_write_tokens=True,
        quirks=("Hosted Sonnet supports text, function tools, and five-minute prompt caching.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="anthropic",
        protocol=OPENAI_COMPAT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="openai_compat_passive",
            supports_streaming=True,
            supports_buffered=True,
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="openai_compat_provider_payload",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=False,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="anthropic_messages",
        unsupported_parameters=("reasoning_effort",),
        quirks=(
            "Current Claude chat path uses Anthropic's OpenAI-compatible endpoint.",
            "Advanced Claude features require the native Messages API.",
        ),
    ),
    ProviderProtocolCapabilities(
        provider_key="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="anthropic_messages_summary",
            output_kind=ReasoningOutputKind.SUMMARY,
            supports_streaming=True,
            supports_buffered=True,
            requestable=True,
            continuation_state="sensitive",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="anthropic_messages",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=False,
        supports_provider_hosted_web_search_adapter=True,
        default_web_search_adapter="anthropic_messages",
        cache_strategy="anthropic_cache_control",
        supports_cache_control=True,
        reports_cache_read_tokens=True,
        reports_cache_write_tokens=True,
        quirks=("Native Anthropic Messages chat supports buffered and SSE streaming responses.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="gemini",
        protocol=OPENAI_COMPAT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="openai_compat_passive",
            supports_streaming=True,
            supports_buffered=True,
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="openai_compat_provider_payload",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="gemini_grounding",
        unsupported_parameters=("reasoning_effort:xhigh",),
        quirks=("Current Gemini chat path uses Google's OpenAI-compatible v1beta endpoint.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="moonshot",
        protocol=OPENAI_COMPAT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="moonshot_reasoning",
            output_kind=ReasoningOutputKind.PROVIDER_REASONING,
            supports_streaming=True,
            supports_buffered=True,
            continuation_state="sensitive",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="openai_compat_provider_payload",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=True,
        default_web_search_adapter="moonshot_kimi",
        cache_strategy="implicit_provider",
        supports_prompt_cache_key=True,
        reports_cache_read_tokens=True,
        quirks=("Kimi reasoning_content must be retained when assistant messages are replayed.",),
    ),
    ProviderProtocolCapabilities(
        provider_key="nvidia",
        protocol=OPENAI_COMPAT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="nvidia_reasoning",
            output_kind=ReasoningOutputKind.PROVIDER_REASONING,
            supports_streaming=True,
            supports_buffered=True,
            continuation_state="sensitive",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.REPORTED,
            input_token_count_strategy="openai_compat_provider_payload",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=False,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="auto",
        quirks=(
            "Hosted NVIDIA NIM uses the OpenAI-compatible chat protocol.",
            "Nemotron reasoning_content is retained only for same-route tool continuation.",
            "Hosted Free Endpoints are rate-limited prototyping endpoints.",
        ),
    ),
    ProviderProtocolCapabilities(
        provider_key="zai_coding_plan",
        protocol=OPENAI_COMPAT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="glm_reasoning",
            output_kind=ReasoningOutputKind.PROVIDER_REASONING,
            supports_streaming=True,
            supports_buffered=True,
            continuation_state="sensitive",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.REPORTED,
            input_token_count_strategy="openai_compat_provider_payload",
            billing_mode=BillingMode.SUBSCRIPTION,
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="auto",
        cache_strategy="implicit_provider",
        reports_cache_read_tokens=True,
        quirks=(
            "GLM Coding Plan uses a subscription key and a dedicated OpenAI-compatible endpoint.",
            "GLM-5.3 has a reasoning floor: low is the minimum and off is not available.",
            "reasoning_content is displayed as provider reasoning, not a summary.",
        ),
    ),
    ProviderProtocolCapabilities(
        provider_key="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="gemini_thought_summary",
            output_kind=ReasoningOutputKind.SUMMARY,
            supports_streaming=True,
            supports_buffered=True,
            requestable=True,
            continuation_state="opaque",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="gemini_count_tokens",
        ),
        supports_streaming=True,
        supports_tool_calling=True,
        supports_structured_outputs=True,
        supports_provider_hosted_web_search_adapter=True,
        default_web_search_adapter="gemini_grounding",
        cache_strategy="gemini_explicit_cached_content",
        supports_explicit_cached_content=True,
        reports_cache_read_tokens=True,
        quirks=(
            "Native Gemini GenerateContent chat supports buffered and streamGenerateContent "
            "SSE responses.",
        ),
    ),
    ProviderProtocolCapabilities(
        provider_key="gemini",
        protocol=GEMINI_INTERACTIONS_PROTOCOL,
        reasoning_trace=ReasoningTraceCapability(
            adapter="gemini_interactions_summary",
            output_kind=ReasoningOutputKind.SUMMARY,
            supports_streaming=False,
            supports_buffered=True,
            requestable=True,
            continuation_state="opaque",
        ),
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="gemini_count_tokens_projection",
        ),
        supports_streaming=False,
        supports_tool_calling=False,
        supports_structured_outputs=False,
        supports_provider_hosted_web_search_adapter=False,
        default_web_search_adapter="gemini_grounding",
        unsupported_parameters=("stream", "tools", "tool_choice", "response_format", "web_search"),
        supports_forced_tool_choice=False,
        cache_strategy="gemini_implicit",
        quirks=(
            "Experimental Gemini Interactions prototype; gated by "
            "ALYSIS_EXPERIMENTAL_GEMINI_INTERACTIONS=1 or "
            "experimental_gemini_interactions_enabled=true.",
            "Only text-only buffered requests are implemented; Gemini GenerateContent remains "
            "the stable native Gemini protocol.",
        ),
    ),
)


def get_provider_protocol_capabilities(
    *,
    provider_key: str,
    protocol: str,
) -> ProviderProtocolCapabilities | None:
    normalized_provider = str(provider_key or "").strip().lower()
    normalized_provider = _PROVIDER_CAPABILITY_ALIASES.get(
        normalized_provider,
        normalized_provider,
    )
    normalized_protocol = str(protocol or "").strip().lower()
    for capabilities in PROVIDER_PROTOCOL_CAPABILITIES:
        if (
            capabilities.provider_key == normalized_provider
            and capabilities.protocol == normalized_protocol
        ):
            return capabilities
    return None
