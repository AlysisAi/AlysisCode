from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .llm.cache_capabilities import (
    CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL,
    CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT,
    CACHE_STRATEGY_IMPLICIT_PROVIDER,
    CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
    CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
    CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
    CACHE_USAGE_SCHEMA_ANTHROPIC,
    CACHE_USAGE_SCHEMA_GEMINI,
    CACHE_USAGE_SCHEMA_OPENAI,
    CACHE_USAGE_SCHEMA_PROVIDER,
    OPENROUTER_SESSION_ID_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
    CacheCapabilitySpec,
)
from .llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
)
from .profiles import ProfileSpec
from .prompt_guidance_catalog import ReviewedModel
from .provider_url import is_alysis_gateway_url
from .web_search_adapters import (
    ANTHROPIC_MESSAGES_ADAPTER,
    AUTO_WEB_SEARCH_ADAPTER,
    COHERE_WEB_SEARCH_ADAPTER,
    DASHSCOPE_CHAT_ADAPTER,
    GEMINI_GROUNDING_ADAPTER,
    GROQ_COMPOUND_ADAPTER,
    MINIMAX_CODING_PLAN_ADAPTER,
    MISTRAL_CONVERSATIONS_ADAPTER,
    MOONSHOT_KIMI_ADAPTER,
    OPENAI_RESPONSES_ADAPTER,
    OPENROUTER_WEB_ADAPTER,
    PERPLEXITY_SONAR_ADAPTER,
    VOLCENGINE_WEB_SEARCH_ADAPTER,
    XAI_RESPONSES_ADAPTER,
    ZHIPU_WEB_SEARCH_ADAPTER,
)

NATIVE_PROFILE_PROTOCOLS: frozenset[str] = frozenset(
    {
        OPENAI_RESPONSES_PROTOCOL,
        ANTHROPIC_MESSAGES_PROTOCOL,
        GEMINI_GENERATE_CONTENT_PROTOCOL,
        GEMINI_INTERACTIONS_PROTOCOL,
    }
)
FIRST_PARTY_NATIVE_PRESET_KEYS: tuple[str, ...] = (
    "openai-responses",
    "anthropic",
    "gemini",
)
FIRST_CLASS_SETUP_PRESET_KEYS: tuple[str, ...] = (
    # "alysis" (hosted MiMo) deliberately absent: while no campaign is
    # running it stays off the primary picker entirely (advanced picker only).
    *FIRST_PARTY_NATIVE_PRESET_KEYS,
)
FIRST_PARTY_COMPATIBILITY_PRESET_KEYS: tuple[str, ...] = (
    "openai",
    "anthropic-compat",
    "gemini-compat",
)
LEGACY_NATIVE_ALIAS_PRESET_KEYS: tuple[str, ...] = ("anthropic-native", "gemini-native")
LOCAL_PROFILE_PRESET_KEYS: tuple[str, ...] = ("ollama", "lm-studio", "vllm")
_CUSTOM_PRESET_KEY = "custom"
# Account-gated hosted presets (`alysis login`, no API key). Kept off the
# primary provider picker while no hosted campaign is running.
_ACCOUNT_GATED_PRESET_KEYS: tuple[str, ...] = ("alysis",)
_CONVERSION_PRESET_BY_FAMILY: dict[str, dict[str, str]] = {
    "openai": {"native": "openai-responses", "compatibility": "openai"},
    "anthropic": {"native": "anthropic", "compatibility": "anthropic-compat"},
    "gemini": {"native": "gemini", "compatibility": "gemini-compat"},
}


@dataclass(frozen=True)
class ProfilePreset:
    key: str
    label: str
    protocol: str
    base_url: str
    api_key_env: str | None
    extra_headers: dict[str, str] = field(default_factory=dict)
    suggested_models: tuple[str, ...] = ()
    suggested_model_descriptions: dict[str, str] = field(default_factory=dict)
    model_aliases: dict[str, str] = field(default_factory=dict)
    validation_model: str = ""
    web_search_adapter: str = AUTO_WEB_SEARCH_ADAPTER
    web_search_model: str = ""
    setup_warning: str = ""
    notes: str = ""
    cache_capability: CacheCapabilitySpec | None = None
    # Keep new optional fields at the end so extensions using the legacy
    # positional constructor continue to bind the sixth argument to headers.
    provider_key: str = ""
    model_choices: tuple[ReviewedModel, ...] = ()
    # Hosted paid models need an authenticated catalog before picker display.
    paid_models: tuple[str, ...] = ()
    # Short account/billing guidance for connection UIs; notes may contain CLI details.
    connection_description: str = ""

    def __post_init__(self) -> None:
        if not self.model_choices:
            return
        model_ids = tuple(model.id for model in self.model_choices)
        if len(set(model_ids)) != len(model_ids):
            raise ValueError(f"Duplicate model choices in preset {self.key!r}")
        if self.suggested_models and self.suggested_models != model_ids:
            raise ValueError(f"Model choices disagree with suggested models in preset {self.key!r}")
        # Preserve the string-based picker/API contract while deriving the IDs
        # from the same records used for prompt selection.
        object.__setattr__(self, "suggested_models", model_ids)


_OPENAI_PROMPT_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    enabled=True,
    supports_prompt_cache_key=True,
    supports_prompt_cache_retention=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_OPENAI,
    min_cacheable_tokens=1024,
    source="preset",
)


def _cache_minimum(tokens: int) -> CacheCapabilitySpec:
    """A scoped override that only narrows the minimum cacheable prefix."""

    return CacheCapabilitySpec(min_cacheable_tokens=tokens, source="preset")


# Anthropic's minimum cacheable prefix is per-model and does not move
# monotonically with the version number: the 5-generation flagships halved the
# floor to 512 while Opus 4.6/4.5 and Haiku 4.5 still need 4096. A prefix under
# the floor is silently not cached, so the write premium buys nothing — the
# floor is what makes the request-shape report say so instead of guessing.
_ANTHROPIC_CACHE_CONTROL_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_ANTHROPIC_CACHE_CONTROL,
    enabled=True,
    supports_cache_control=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_ANTHROPIC,
    min_cacheable_tokens=1024,
    model_family_overrides=(
        ("claude-haiku-5-5", _cache_minimum(512)),
        ("claude-opus-5", _cache_minimum(512)),
        ("claude-fable-5", _cache_minimum(512)),
        ("claude-mythos-5", _cache_minimum(512)),
        ("claude-mythos-preview", _cache_minimum(2048)),
        ("claude-opus-4-7", _cache_minimum(2048)),
        ("claude-opus-4-6", _cache_minimum(4096)),
        ("claude-opus-4-5", _cache_minimum(4096)),
        ("claude-haiku-4-5", _cache_minimum(4096)),
        ("claude-haiku-5-5", _cache_minimum(512)),
    ),
    source="preset",
)
_GEMINI_EXPLICIT_CACHED_CONTENT_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_GEMINI_EXPLICIT_CACHED_CONTENT,
    enabled=True,
    supports_explicit_cached_content=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_GEMINI,
    min_cacheable_tokens=4096,
    source="preset",
)
_MISTRAL_PROMPT_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_MISTRAL_PROMPT_CACHE_KEY,
    enabled=True,
    supports_prompt_cache_key=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_OPENAI,
    min_cacheable_tokens=1024,
    emits_request_fields=True,
    notes=("Emits Mistral prompt_cache_key for stable server routing and prompt-cache hits.",),
    source="preset",
)
_OPENROUTER_STICKY_SESSION_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_OPENROUTER_STICKY_SESSION,
    enabled=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=True,
    request_fields=(OPENROUTER_SESSION_ID_FIELD,),
    notes=(
        "Emits OpenRouter session_id for sticky routing; upstream cache semantics remain "
        "route-dependent.",
    ),
    source="preset",
)
_XAI_CONVERSATION_HEADER_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_XAI_CONVERSATION_HEADER,
    enabled=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=True,
    request_fields=(XAI_CONVERSATION_ID_HEADER_FIELD,),
    notes=("Emits x-grok-conv-id for sticky cache routing on xAI Chat Completions.",),
    source="preset",
)
_QWEN_DIAGNOSTIC_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_QWEN_CACHE_CONTROL_BLOCKS,
    enabled=True,
    reports_cache_read_tokens=True,
    reports_cache_write_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    min_cacheable_tokens=1024,
    emits_request_fields=False,
    notes=(
        "Diagnostic-only in auto mode; Qwen cache_control content markers mutate "
        "message shape and require request-shape gating.",
    ),
    source="preset",
)
_MOONSHOT_AUTOMATIC_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_IMPLICIT_PROVIDER,
    enabled=True,
    supports_prompt_cache_key=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=True,
    notes=(
        "Moonshot caches matching prompt prefixes automatically; prompt_cache_key keeps a "
        "session on a stable cache-affinity route.",
    ),
    source="preset",
)
_ZAI_CODING_PLAN_CACHE_CAPABILITY = CacheCapabilitySpec(
    strategy=CACHE_STRATEGY_IMPLICIT_PROVIDER,
    enabled=True,
    reports_cache_read_tokens=True,
    usage_schema=CACHE_USAGE_SCHEMA_PROVIDER,
    emits_request_fields=False,
    notes=(
        "Z.AI Coding Plan caches matching prompt prefixes automatically and reports "
        "cached-input usage; Alysis Code emits no provider-specific cache fields.",
    ),
    source="preset",
)


def preset_protocol_kind(preset: ProfilePreset) -> str:
    return "native" if preset.protocol in NATIVE_PROFILE_PROTOCOLS else "compatibility"


def preset_protocol_summary(preset: ProfilePreset) -> str:
    if preset.protocol in NATIVE_PROFILE_PROTOCOLS:
        return (
            f"Native first-party protocol: {preset.protocol} (recommended for first-party API keys)"
        )
    return "Compatibility protocol: OpenAI-compatible chat transport"


def preset_selection_label(preset: ProfilePreset) -> str:
    """Return a setup/config label that keeps protocol details out of the primary choice."""
    if preset.key == "alysis":
        return "Alysis Code (free credits) - requires login"
    if preset.key == "openai-responses":
        return "OpenAI - Native Responses"
    if preset.key in {"anthropic", "anthropic-native"}:
        return "Anthropic Claude - Native Messages"
    if preset.key in {"gemini", "gemini-native"}:
        return "Google Gemini - Native GenerateContent"
    if preset.key == "openai":
        return "OpenAI - Compatibility/gateway Chat Completions"
    if preset.key == "anthropic-compat":
        return "Anthropic Claude compatibility - legacy OpenAI-compatible"
    if preset.key == "gemini-compat":
        return "Google Gemini compatibility - legacy OpenAI-compatible"
    if preset.key in LOCAL_PROFILE_PRESET_KEYS:
        return f"{preset.label} - Local endpoint"
    if preset.key == "custom":
        return "Custom OpenAI-compatible endpoint"
    return preset.label


def _advanced_only_preset_keys() -> frozenset[str]:
    """Preset keys deliberately kept off the primary provider picker.

    Everything else in :data:`PROFILE_PRESETS` is a real hosted provider — the
    native first-party APIs *and* the third-party API/gateway endpoints — and is
    surfaced directly so users are not limited to the big-three brands. Only the
    OpenAI-compatible duplicates of the native first-party providers, local
    endpoints (Ollama/LM Studio/vLLM), the manual custom-URL entry, the
    one-release legacy aliases, and the account-gated hosted MiMo preset
    (no hosted campaign is running, so it is not a provider choice) stay
    behind the advanced picker.
    """
    return frozenset(
        {
            _CUSTOM_PRESET_KEY,
            *FIRST_PARTY_COMPATIBILITY_PRESET_KEYS,
            *LOCAL_PROFILE_PRESET_KEYS,
            *LEGACY_NATIVE_ALIAS_PRESET_KEYS,
            *_ACCOUNT_GATED_PRESET_KEYS,
        }
    )


def provider_selection_presets() -> list[ProfilePreset]:
    """Presets shown directly on the primary provider picker.

    Native first-party providers lead — the best defaults for new users —
    followed by every other hosted provider in registration order.
    Compatibility duplicates, local endpoints, the custom-URL entry,
    one-release legacy aliases, and the account-gated hosted MiMo preset are
    the only presets held back for the advanced picker, so the user sees the
    full range of hosted providers up front instead of just
    OpenAI/Anthropic/Gemini.
    """
    by_key = PRESET_BY_KEY
    advanced = _advanced_only_preset_keys()
    leading = [by_key[key] for key in FIRST_CLASS_SETUP_PRESET_KEYS if key in by_key]
    leading_keys = {preset.key for preset in leading}
    rest = [
        preset
        for preset in PROFILE_PRESETS
        if preset.key not in advanced and preset.key not in leading_keys
    ]
    return [*leading, *rest]


def advanced_provider_selection_presets() -> list[ProfilePreset]:
    """Return the compatibility, local, custom, legacy alias, and account-gated presets.

    These are exactly the presets held off the primary provider picker: the
    OpenAI-compatible duplicates of the native first-party providers, local
    endpoints (Ollama/LM Studio/vLLM), the manual custom-URL entry, the
    one-release legacy aliases, and the account-gated hosted MiMo preset.
    """
    by_key = PRESET_BY_KEY
    first_party_compat = [
        by_key[key] for key in FIRST_PARTY_COMPATIBILITY_PRESET_KEYS if key in by_key
    ]
    local = [by_key[key] for key in LOCAL_PROFILE_PRESET_KEYS if key in by_key]
    custom = [by_key[_CUSTOM_PRESET_KEY]] if _CUSTOM_PRESET_KEY in by_key else []
    aliases = [by_key[key] for key in LEGACY_NATIVE_ALIAS_PRESET_KEYS if key in by_key]
    account_gated = [by_key[key] for key in _ACCOUNT_GATED_PRESET_KEYS if key in by_key]
    return [*first_party_compat, *local, *custom, *aliases, *account_gated]


# Shared by every DashScope region. Alibaba retires the qwen3-coder-* line on
# 2026-10-10 platform-wide (notices 1949/1950/2000 intl, 118344/118345 cn) and
# names qwen3.7-plus as the coding replacement; qwen3.8-max-preview was retired
# 2026-08-05 and is server-routed to qwen3.8-max. Legacy-but-live ids
# (qwen3.7-max, qwen3.6-flash, qwen-flash) are deliberately not remapped.
_QWEN_RETIRED_MODEL_ALIASES: dict[str, str] = {
    "qwen3-coder-plus": "qwen3.7-plus",
    "qwen3-coder-next": "qwen3.7-plus",
    "qwen3.8-max-preview": "qwen3.8-max",
}

PROFILE_PRESETS: tuple[ProfilePreset, ...] = (
    ProfilePreset(
        key="openai",
        provider_key="openai",
        label="OpenAI",
        protocol="openai_compat",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        model_choices=(
            ReviewedModel("gpt-6-astra", "gpt-normal"),
            ReviewedModel("gpt-6.1-sol", "gpt-normal"),
            ReviewedModel("gpt-6.1-terra", "gpt-normal"),
            ReviewedModel("gpt-6-sol", "gpt-normal"),
            ReviewedModel("gpt-6-luna", "gpt-expanded"),
            ReviewedModel("gpt-5.6-terra", "gpt-normal"),
            ReviewedModel("gpt-5.6-sol", "gpt-normal"),
            ReviewedModel("gpt-5.6-luna", "gpt-expanded"),
            ReviewedModel("gpt-5.5", "gpt-expanded"),
            ReviewedModel("gpt-5.4-mini", "gpt-expanded"),
        ),
        suggested_model_descriptions={
            # gpt-6-astra (2026-09-03) is OpenAI's flagship and the default here;
            # Terra is the balanced pick at 1/5 of Astra's input price.
            # gpt-5.3-codex is Responses-only (Chat Completions: Not supported)
            # and lives on the openai-responses preset. gpt-5.6-luna replaces
            # gpt-5.4-nano as the economy tier: same $0.20 input, cheaper
            # output, 1.05M context.
            "gpt-6-astra": "default - GPT-6 flagship, reasoning always on, 1.05M",
            "gpt-6.1-sol": "coding - latest Sol, complex agentic work, 1.05M context",
            "gpt-6.1-terra": "balanced - 1.05M context, Off through Max reasoning, Responses tools",
            "gpt-6-sol": "coding - previous Sol, complex agentic work, 1.05M context",
            "gpt-6-luna": "fast - latest Luna, low-cost focused work, 1.05M context",
            "gpt-5.6-terra": "advanced - balanced 5.6 tier at 1/5 the price, 1.05M",
            "gpt-5.6-sol": "reasoning - top 5.6 tier, 1.05M context",
            "gpt-5.6-luna": "fast - low-cost 5.6 tier, full 1.05M context",
            "gpt-5.5": "fallback - previous flagship, 1.05M context",
            "gpt-5.4-mini": "economy - cheap tier for subagents, 400K",
        },
        model_aliases={
            "gpt-5.6": "gpt-5.6-sol",
            # No official bare alias exists for Astra yet; a typed "gpt-6"
            # would otherwise 404, so retain the explicit Astra shortcut.
            "gpt-6": "gpt-6-astra",
            # Official replacements from OpenAI's deprecations page (2026-07-23
            # codex/chat-latest shutdowns; gpt-5-nano shuts down 2026-12-11).
            "gpt-5-nano": "gpt-5.6-luna",
            "gpt-5-codex": "gpt-5.6-sol",
            "gpt-5.1-codex": "gpt-5.6-sol",
            "gpt-5.1-codex-max": "gpt-5.6-sol",
            "gpt-5.2-codex": "gpt-5.6-sol",
            "gpt-5.1-codex-mini": "gpt-5.6-terra",
            "gpt-5-chat-latest": "gpt-5.6-sol",
            "gpt-5.1-chat-latest": "gpt-5.6-sol",
            # gpt-5.3-codex cannot be called on /v1/chat/completions at all;
            # on this preset it remaps to Codex's own documented successor.
            "gpt-5.3-codex": "gpt-5.6-sol",
        },
        validation_model="gpt-5.6-luna",
        web_search_adapter=OPENAI_RESPONSES_ADAPTER,
        cache_capability=_OPENAI_PROMPT_CACHE_CAPABILITY,
        setup_warning=(
            "GPT-6 Sol/Luna and gpt-5.6/5.4 reject tool calls with reasoning_effort other than "
            "'none' on Chat Completions (and 5.6 defaults to 'medium') — for "
            "agentic runs use the OpenAI Responses preset, or pin effort to "
            "'none' here. gpt-5.3-codex is Responses-only and is not offered here. "
            "gpt-6-astra cannot disable reasoning and is rolling out to API "
            "accounts in stages from 2026-09-03; a 404 means your account is not "
            "enabled yet."
        ),
    ),
    ProfilePreset(
        key="openai-responses",
        provider_key="openai",
        label="OpenAI Responses",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        model_choices=(
            ReviewedModel("gpt-6-astra", "gpt-normal"),
            ReviewedModel("gpt-6.1-sol", "gpt-normal"),
            ReviewedModel("gpt-6.1-terra", "gpt-normal"),
            ReviewedModel("gpt-6-sol", "gpt-normal"),
            ReviewedModel("gpt-6-luna", "gpt-expanded"),
            ReviewedModel("gpt-5.6-terra", "gpt-normal"),
            ReviewedModel("gpt-5.6-sol", "gpt-normal"),
            ReviewedModel("gpt-5.6-luna", "gpt-expanded"),
            ReviewedModel("gpt-5.3-codex", "gpt-expanded"),
            ReviewedModel("gpt-5.5", "gpt-expanded"),
            ReviewedModel("gpt-5.4-mini", "gpt-expanded"),
        ),
        suggested_model_descriptions={
            "gpt-6-astra": "default - GPT-6 flagship, reasoning always on, 1.05M",
            "gpt-6.1-sol": "coding - latest Sol, complex agentic work, 1.05M context",
            "gpt-6.1-terra": "balanced - 1.05M context, Off through Max reasoning",
            "gpt-6-sol": "coding - previous Sol, complex agentic work, 1.05M context",
            "gpt-6-luna": "fast - latest Luna, low-cost focused work, 1.05M context",
            "gpt-5.6-terra": "advanced - balanced 5.6 tier at 1/5 the price, 1.05M",
            "gpt-5.6-sol": "reasoning - top 5.6 tier, 1.05M context",
            "gpt-5.6-luna": "fast - low-cost 5.6 tier, full 1.05M context",
            "gpt-5.3-codex": "coding - agentic codex model, 400K, Responses-only",
            "gpt-5.5": "fallback - previous flagship, 1.05M context",
            "gpt-5.4-mini": "economy - cheap tier for subagents, 400K",
        },
        model_aliases={
            "gpt-5.6": "gpt-5.6-sol",
            "gpt-6": "gpt-6-astra",
            # Official replacements from OpenAI's deprecations page (2026-07-23
            # codex/chat-latest shutdowns; gpt-5-nano shuts down 2026-12-11).
            "gpt-5-nano": "gpt-5.6-luna",
            "gpt-5-codex": "gpt-5.6-sol",
            "gpt-5.1-codex": "gpt-5.6-sol",
            "gpt-5.1-codex-max": "gpt-5.6-sol",
            "gpt-5.2-codex": "gpt-5.6-sol",
            "gpt-5.1-codex-mini": "gpt-5.6-terra",
            "gpt-5-chat-latest": "gpt-5.6-sol",
            "gpt-5.1-chat-latest": "gpt-5.6-sol",
        },
        validation_model="gpt-5.6-luna",
        web_search_adapter=OPENAI_RESPONSES_ADAPTER,
        cache_capability=_OPENAI_PROMPT_CACHE_CAPABILITY,
        notes=(
            "Native OpenAI Responses API chat with SSE streaming support. Use the OpenAI compat "
            "preset to keep Chat Completions-compatible behavior. gpt-6-astra is rolling out "
            "to API accounts in stages from 2026-09-03; a 404 means the account is not "
            "enabled yet."
        ),
    ),
    ProfilePreset(
        key="anthropic",
        provider_key="anthropic",
        label="Anthropic Claude",
        protocol="anthropic_messages",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        model_choices=(
            ReviewedModel("claude-sonnet-5", "claude-normal"),
            ReviewedModel("claude-haiku-5-5", "claude-normal"),
            ReviewedModel("claude-opus-5-5", "claude-normal"),
            ReviewedModel("claude-opus-5", "claude-normal"),
            ReviewedModel("claude-fable-5-1", "claude-normal"),
            ReviewedModel("claude-haiku-4-5", "claude-expanded"),
            ReviewedModel("claude-opus-4-8", "claude-expanded"),
            ReviewedModel("claude-opus-4-7", "claude-expanded"),
        ),
        suggested_model_descriptions={
            # Fable 5.1 (2026-09-01) supersedes Fable 5, which is now in
            # Anthropic's Legacy table. Fable 5 stays callable and is not
            # aliased: 5.1 rejects forced tool_choice, so a silent swap could
            # change request behaviour, not just billing.
            "claude-sonnet-5": "default - 1M context, best speed/intelligence mix",
            "claude-haiku-5-5": "fast - 1M context, adaptive thinking; higher rates above 100K input",
            "claude-opus-5-5": "advanced - latest Opus, thinking always on, 1M context",
            "claude-opus-5": "fallback - previous Opus, 1M context",
            "claude-fable-5-1": "reasoning - Mythos-class, adaptive thinking always on",
            "claude-haiku-4-5": "fast - 200K context, lowest cost tier",
            "claude-opus-4-8": "fallback - previous-generation opus, 1M context",
            "claude-opus-4-7": "legacy - prior opus generation, 1M context",
        },
        model_aliases={
            # claude-sonnet-4-6 moved to Anthropic's Legacy table; Sonnet 5 is
            # newer and cheaper. Retired haiku ids remap to the 4.5 bare alias.
            # claude-opus-4-1 retired 2026-08-05 (official replacement 4-8).
            "claude-sonnet-4": "claude-sonnet-5",
            "claude-sonnet-4-5": "claude-sonnet-5",
            "claude-sonnet-4-6": "claude-sonnet-5",
            "claude-4-sonnet": "claude-sonnet-5",
            "claude-3-5-haiku-latest": "claude-haiku-4-5",
            "claude-3-5-haiku-20241022": "claude-haiku-4-5",
            "claude-opus-5.5": "claude-opus-5-5",
            "claude-opus-4.8": "claude-opus-4-8",
            "claude-opus-4.7": "claude-opus-4-7",
            "claude-opus-4-1": "claude-opus-4-8",
            "claude-opus-4-6": "claude-opus-4-8",
            "claude-fable-5.1": "claude-fable-5-1",
        },
        validation_model="claude-haiku-4-5",
        web_search_adapter=ANTHROPIC_MESSAGES_ADAPTER,
        cache_capability=_ANTHROPIC_CACHE_CONTROL_CAPABILITY,
        notes=(
            "Native Anthropic Messages API chat with SSE streaming support. Compatibility mode "
            "remains available as anthropic-compat for legacy OpenAI-compatible fallback."
        ),
    ),
    ProfilePreset(
        key="anthropic-compat",
        provider_key="anthropic",
        label="Anthropic Claude compatibility",
        protocol="openai_compat",
        base_url="https://api.anthropic.com/v1/",
        api_key_env="ANTHROPIC_API_KEY",
        model_choices=(
            ReviewedModel("claude-sonnet-5", "claude-normal"),
            ReviewedModel("claude-haiku-5-5", "claude-normal"),
            ReviewedModel("claude-opus-5-5", "claude-normal"),
            ReviewedModel("claude-opus-5", "claude-normal"),
            ReviewedModel("claude-fable-5-1", "claude-normal"),
            ReviewedModel("claude-haiku-4-5", "claude-expanded"),
            ReviewedModel("claude-opus-4-8", "claude-expanded"),
            ReviewedModel("claude-opus-4-7", "claude-expanded"),
        ),
        suggested_model_descriptions={
            # Fable 5.1 (2026-09-01) supersedes Fable 5, which is now in
            # Anthropic's Legacy table. Fable 5 stays callable and is not
            # aliased: 5.1 rejects forced tool_choice, so a silent swap could
            # change request behaviour, not just billing.
            "claude-sonnet-5": "default - 1M context, best speed/intelligence mix",
            "claude-haiku-5-5": "fast - 1M context, adaptive thinking; higher rates above 100K input",
            "claude-opus-5-5": "advanced - latest Opus, thinking always on, 1M context",
            "claude-opus-5": "fallback - previous Opus, 1M context",
            "claude-fable-5-1": "reasoning - Mythos-class, adaptive thinking always on",
            "claude-haiku-4-5": "fast - 200K context, lowest cost tier",
            "claude-opus-4-8": "fallback - previous-generation opus, 1M context",
            "claude-opus-4-7": "legacy - prior opus generation, 1M context",
        },
        model_aliases={
            # claude-sonnet-4-6 moved to Anthropic's Legacy table; Sonnet 5 is
            # newer and cheaper. Retired haiku ids remap to the 4.5 bare alias.
            # claude-opus-4-1 retired 2026-08-05 (official replacement 4-8).
            "claude-sonnet-4": "claude-sonnet-5",
            "claude-sonnet-4-5": "claude-sonnet-5",
            "claude-sonnet-4-6": "claude-sonnet-5",
            "claude-4-sonnet": "claude-sonnet-5",
            "claude-3-5-haiku-latest": "claude-haiku-4-5",
            "claude-3-5-haiku-20241022": "claude-haiku-4-5",
            "claude-opus-5.5": "claude-opus-5-5",
            "claude-opus-4.8": "claude-opus-4-8",
            "claude-opus-4.7": "claude-opus-4-7",
            "claude-opus-4-1": "claude-opus-4-8",
            "claude-opus-4-6": "claude-opus-4-8",
            "claude-fable-5.1": "claude-fable-5-1",
        },
        validation_model="claude-haiku-4-5",
        web_search_adapter=ANTHROPIC_MESSAGES_ADAPTER,
        setup_warning=(
            "Anthropic labels the OpenAI SDK compatibility layer as a test path; "
            "use the anthropic preset for native Messages API behavior."
        ),
        notes=(
            "Chat uses Anthropic OpenAI-compat at /v1; web_search uses the native "
            "Anthropic Messages web_search adapter when the model/account supports it."
        ),
    ),
    ProfilePreset(
        key="anthropic-native",
        provider_key="anthropic",
        label="Anthropic Claude (native alias)",
        protocol="anthropic_messages",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        model_choices=(
            ReviewedModel("claude-sonnet-5", "claude-normal"),
            ReviewedModel("claude-haiku-5-5", "claude-normal"),
            ReviewedModel("claude-opus-5-5", "claude-normal"),
            ReviewedModel("claude-opus-5", "claude-normal"),
            ReviewedModel("claude-fable-5-1", "claude-normal"),
            ReviewedModel("claude-haiku-4-5", "claude-expanded"),
            ReviewedModel("claude-opus-4-8", "claude-expanded"),
            ReviewedModel("claude-opus-4-7", "claude-expanded"),
        ),
        suggested_model_descriptions={
            # Fable 5.1 (2026-09-01) supersedes Fable 5, which is now in
            # Anthropic's Legacy table. Fable 5 stays callable and is not
            # aliased: 5.1 rejects forced tool_choice, so a silent swap could
            # change request behaviour, not just billing.
            "claude-sonnet-5": "default - 1M context, best speed/intelligence mix",
            "claude-haiku-5-5": "fast - 1M context, adaptive thinking; higher rates above 100K input",
            "claude-opus-5-5": "advanced - latest Opus, thinking always on, 1M context",
            "claude-opus-5": "fallback - previous Opus, 1M context",
            "claude-fable-5-1": "reasoning - Mythos-class, adaptive thinking always on",
            "claude-haiku-4-5": "fast - 200K context, lowest cost tier",
            "claude-opus-4-8": "fallback - previous-generation opus, 1M context",
            "claude-opus-4-7": "legacy - prior opus generation, 1M context",
        },
        model_aliases={
            # claude-sonnet-4-6 moved to Anthropic's Legacy table; Sonnet 5 is
            # newer and cheaper. Retired haiku ids remap to the 4.5 bare alias.
            # claude-opus-4-1 retired 2026-08-05 (official replacement 4-8).
            "claude-sonnet-4": "claude-sonnet-5",
            "claude-sonnet-4-5": "claude-sonnet-5",
            "claude-sonnet-4-6": "claude-sonnet-5",
            "claude-4-sonnet": "claude-sonnet-5",
            "claude-3-5-haiku-latest": "claude-haiku-4-5",
            "claude-3-5-haiku-20241022": "claude-haiku-4-5",
            "claude-opus-5.5": "claude-opus-5-5",
            "claude-opus-4.8": "claude-opus-4-8",
            "claude-opus-4.7": "claude-opus-4-7",
            "claude-opus-4-1": "claude-opus-4-8",
            "claude-opus-4-6": "claude-opus-4-8",
            "claude-fable-5.1": "claude-fable-5-1",
        },
        validation_model="claude-haiku-4-5",
        web_search_adapter=ANTHROPIC_MESSAGES_ADAPTER,
        cache_capability=_ANTHROPIC_CACHE_CONTROL_CAPABILITY,
        notes=(
            "Legacy alias for the native anthropic preset. Prefer the anthropic preset for new "
            "first-party Claude profiles."
        ),
    ),
    ProfilePreset(
        key="gemini",
        provider_key="gemini",
        label="Google Gemini",
        protocol="gemini_generate_content",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
        model_choices=(
            ReviewedModel("gemini-3.8-flash", "gemini-normal"),
            ReviewedModel("gemini-3.7-flash", "gemini-normal"),
            ReviewedModel("gemini-3.5-flash-lite", "gemini-expanded"),
            ReviewedModel("gemini-3.1-pro-preview", "gemini-normal"),
        ),
        suggested_model_descriptions={
            # No GA Pro model exists; 3.1-pro-preview is still served with no
            # shutdown date. Intro pricing on 3.6/3.7/3.8 Flash ends 2026-12-31.
            "gemini-3.8-flash": "default - newest stable flash for SWE and agents, 1M",
            "gemini-3.7-flash": "fallback - previous stable flash model, 1M context",
            "gemini-3.5-flash-lite": "economy - lowest-cost stable tier, 1M context",
            "gemini-3.1-pro-preview": "advanced - pro reasoning preview, 1M context",
        },
        model_aliases={
            # Only shut-down or invalid legacy ids are rewritten. Active stable
            # ids and provider-managed *-latest aliases pass through unchanged.
            # gemini-3.1-flash-lite itself shuts down 2027-05-07, so the
            # 2.0-flash-lite remap lands on its documented successor.
            "gemini-2.0-flash": "gemini-3.6-flash",
            "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
            "gemini-3.1-preview": "gemini-3.1-pro-preview",
            "gemini-3-pro-preview": "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
        },
        validation_model="gemini-3.5-flash-lite",
        web_search_adapter=GEMINI_GROUNDING_ADAPTER,
        cache_capability=_GEMINI_EXPLICIT_CACHED_CONTENT_CAPABILITY,
        setup_warning=(
            "Gemini native GenerateContent uses the Google Gemini API v1beta surface and "
            "model availability can vary by account, region, and provider rollout."
        ),
        notes=(
            "Native Gemini GenerateContent API chat with streamGenerateContent SSE support. "
            "Compatibility mode remains available as gemini-compat for legacy OpenAI-compatible "
            "fallback."
        ),
    ),
    ProfilePreset(
        key="gemini-compat",
        provider_key="gemini",
        label="Google Gemini compatibility",
        protocol="openai_compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        model_choices=(
            ReviewedModel("gemini-3.8-flash", "gemini-normal"),
            ReviewedModel("gemini-3.7-flash", "gemini-normal"),
            ReviewedModel("gemini-3.5-flash-lite", "gemini-expanded"),
            ReviewedModel("gemini-3.1-pro-preview", "gemini-normal"),
        ),
        suggested_model_descriptions={
            # No GA Pro model exists; 3.1-pro-preview is still served with no
            # shutdown date. Intro pricing on 3.6/3.7/3.8 Flash ends 2026-12-31.
            "gemini-3.8-flash": "default - newest stable flash for SWE and agents, 1M",
            "gemini-3.7-flash": "fallback - previous stable flash model, 1M context",
            "gemini-3.5-flash-lite": "economy - lowest-cost stable tier, 1M context",
            "gemini-3.1-pro-preview": "advanced - pro reasoning preview, 1M context",
        },
        model_aliases={
            # Only shut-down or invalid legacy ids are rewritten. Active stable
            # ids and provider-managed *-latest aliases pass through unchanged.
            # gemini-3.1-flash-lite itself shuts down 2027-05-07, so the
            # 2.0-flash-lite remap lands on its documented successor.
            "gemini-2.0-flash": "gemini-3.6-flash",
            "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
            "gemini-3.1-preview": "gemini-3.1-pro-preview",
            "gemini-3-pro-preview": "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
        },
        validation_model="gemini-3.5-flash-lite",
        web_search_adapter=GEMINI_GROUNDING_ADAPTER,
        setup_warning=(
            "Gemini OpenAI compatibility is served from v1beta; use the gemini preset for "
            "native GenerateContent behavior."
        ),
    ),
    ProfilePreset(
        key="gemini-native",
        provider_key="gemini",
        label="Google Gemini (native alias)",
        protocol="gemini_generate_content",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
        model_choices=(
            ReviewedModel("gemini-3.8-flash", "gemini-normal"),
            ReviewedModel("gemini-3.7-flash", "gemini-normal"),
            ReviewedModel("gemini-3.5-flash-lite", "gemini-expanded"),
            ReviewedModel("gemini-3.1-pro-preview", "gemini-normal"),
        ),
        suggested_model_descriptions={
            # No GA Pro model exists; 3.1-pro-preview is still served with no
            # shutdown date. Intro pricing on 3.6/3.7/3.8 Flash ends 2026-12-31.
            "gemini-3.8-flash": "default - newest stable flash for SWE and agents, 1M",
            "gemini-3.7-flash": "fallback - previous stable flash model, 1M context",
            "gemini-3.5-flash-lite": "economy - lowest-cost stable tier, 1M context",
            "gemini-3.1-pro-preview": "advanced - pro reasoning preview, 1M context",
        },
        model_aliases={
            # Only shut-down or invalid legacy ids are rewritten. Active stable
            # ids and provider-managed *-latest aliases pass through unchanged.
            # gemini-3.1-flash-lite itself shuts down 2027-05-07, so the
            # 2.0-flash-lite remap lands on its documented successor.
            "gemini-2.0-flash": "gemini-3.6-flash",
            "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
            "gemini-3.1-preview": "gemini-3.1-pro-preview",
            "gemini-3-pro-preview": "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
        },
        validation_model="gemini-3.5-flash-lite",
        web_search_adapter=GEMINI_GROUNDING_ADAPTER,
        cache_capability=_GEMINI_EXPLICIT_CACHED_CONTENT_CAPABILITY,
        setup_warning=(
            "Gemini native GenerateContent uses the Google Gemini API v1beta surface and "
            "model availability can vary by account, region, and provider rollout."
        ),
        notes=(
            "Legacy alias for the native gemini preset. Prefer the gemini preset for new "
            "first-party Gemini profiles."
        ),
    ),
    ProfilePreset(
        key="deepseek",
        provider_key="deepseek",
        label="DeepSeek",
        protocol="openai_compat",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        model_choices=(
            ReviewedModel("deepseek-flash", "deepseek-expanded"),
            ReviewedModel("deepseek-v4-pro", "deepseek-normal"),
        ),
        suggested_model_descriptions={
            "deepseek-flash": "default - DeepSeek V4.1 Flash, native vision and reasoning, 1M context",
            "deepseek-v4-pro": "DeepSeek V4 Pro - reasoning, 1M context",
        },
        model_aliases={
            # deepseek-chat / deepseek-reasoner were discontinued 2026-07-24 and
            # no longer resolve; saved configs pinning them keep working via
            # these remaps.
            "deepseek-chat": "deepseek-flash",
            "deepseek-reasoner": "deepseek-flash",
            "deepseek-v4-flash": "deepseek-flash",
            "deepseek-v4-flash-vision-exp": "deepseek-flash",
            "deepseek-v4.1-flash-expires-on-0910": "deepseek-flash",
        },
        validation_model="deepseek-flash",
        setup_warning=(
            "DeepSeek V4.1 Flash uses the stable deepseek-flash ID. Retired Flash "
            "and beta selections migrate automatically. Pricing is peak/off-peak "
            "(off-peak is 50% of the listed rate)."
        ),
    ),
    ProfilePreset(
        key="nvidia",
        provider_key="nvidia",
        label="NVIDIA NIM (Hosted)",
        protocol="openai_compat",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        model_choices=(
            ReviewedModel("nvidia/nemotron-3-super-120b-a12b", "nemotron-normal"),
            ReviewedModel("nvidia/nemotron-3-ultra-550b-a55b", "nemotron-expanded"),
            ReviewedModel("nvidia/nemotron-3.5-lightning-30b-a3b", "nemotron-expanded"),
            ReviewedModel("moonshotai/kimi-k3", "kimi-normal"),
            ReviewedModel("deepseek-ai/deepseek-v4-pro-0813", "deepseek-normal"),
            ReviewedModel("deepseek-ai/deepseek-v4-flash-0731", "deepseek-normal"),
        ),
        suggested_model_descriptions={
            # The free endpoints for nemotron-3-nano and the undated DeepSeek
            # ids are marked Deprecated on build.nvidia.com; the dated DeepSeek
            # snapshots and Nemotron 3.5 Lightning are their live successors.
            "nvidia/nemotron-3-super-120b-a12b": (
                "default - balanced agentic reasoning, 1M context"
            ),
            "nvidia/nemotron-3-ultra-550b-a55b": (
                "advanced - frontier agentic reasoning, 1M context"
            ),
            "nvidia/nemotron-3.5-lightning-30b-a3b": (
                "fast - efficient reasoning and tool use, 1M hosted context"
            ),
            "moonshotai/kimi-k3": "coding - third-party Kimi K3 hosted by NVIDIA, 1M multimodal",
            "deepseek-ai/deepseek-v4-pro-0813": (
                "reasoning - third-party DeepSeek V4 Pro hosted by NVIDIA, 1M"
            ),
            "deepseek-ai/deepseek-v4-flash-0731": (
                "economy - third-party DeepSeek V4 Flash hosted by NVIDIA, 1M"
            ),
        },
        # No aliases on purpose: nemotron-3-nano and the undated DeepSeek ids
        # are "Free Endpoint: Deprecated" on build.nvidia.com but still appear
        # in the live /v1/models inventory, and live ids are never rewritten.
        # They simply leave the offline recommendation set.
        validation_model="nvidia/nemotron-3.5-lightning-30b-a3b",
        setup_warning=(
            "NVIDIA hosted Free Endpoints are rate-limited development endpoints for "
            "prototyping; availability is not a production SLA and may vary by account."
        ),
        notes=(
            "Hosted NVIDIA NIM OpenAI-compatible API. The live catalog includes models "
            "from NVIDIA and third parties; reasoning controls are model-specific."
        ),
    ),
    ProfilePreset(
        key="qwen-intl",
        provider_key="qwen",
        label="Alibaba Qwen / DashScope (Intl)",
        protocol="openai_compat",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        model_choices=(
            ReviewedModel("qwen3.7-plus", "qwen-normal"),
            ReviewedModel("qwen3.8-max", "qwen-normal"),
            ReviewedModel("qwen3.8-flash", "qwen-expanded"),
            ReviewedModel("qwen3.7-flash", "qwen-expanded"),
        ),
        suggested_model_descriptions={
            # Alibaba's documented coding recommendation is qwen3.7-plus; the
            # qwen3-coder-* line shuts down 2026-10-10 in every region.
            # qwen3.7-max, qwen3.6-flash and qwen-flash moved to the Legacy
            # table (still callable, not aliased).
            "qwen3.7-plus": "default - 1M context, balanced cost, coding pick",
            "qwen3.8-max": "advanced - newest multimodal flagship, 1M context",
            "qwen3.8-flash": "fast - low-cost multimodal tier, 1M context",
            "qwen3.7-flash": "economy - cheapest current-gen model, 1M context",
        },
        model_aliases=_QWEN_RETIRED_MODEL_ALIASES,
        validation_model="qwen3.7-flash",
        web_search_adapter=DASHSCOPE_CHAT_ADAPTER,
        web_search_model="qwen3.7-plus",
        cache_capability=_QWEN_DIAGNOSTIC_CACHE_CAPABILITY,
        setup_warning=(
            "DashScope API keys are region-specific; use a key from the Singapore region."
        ),
    ),
    ProfilePreset(
        key="qwen-us",
        provider_key="qwen",
        label="Alibaba Qwen / DashScope (US)",
        protocol="openai_compat",
        base_url="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        model_choices=(
            ReviewedModel("qwen3.7-plus", "qwen-normal"),
            ReviewedModel("qwen3.8-max", "qwen-normal"),
            ReviewedModel("qwen3.8-flash", "qwen-expanded"),
            ReviewedModel("qwen3.7-flash", "qwen-expanded"),
        ),
        suggested_model_descriptions={
            # Same roster as the Singapore surface; US (Virginia) "Global" SKUs
            # are priced ~17-30% below Singapore. Web search is not served for
            # qwen3.8-max in the US, so hosted search stays on qwen3.7-plus.
            "qwen3.7-plus": "default - 1M context, balanced cost, coding pick",
            "qwen3.8-max": "advanced - newest multimodal flagship, 1M context",
            "qwen3.8-flash": "fast - low-cost multimodal tier, 1M context",
            "qwen3.7-flash": "economy - cheapest current-gen model, 1M context",
        },
        model_aliases=_QWEN_RETIRED_MODEL_ALIASES,
        validation_model="qwen3.7-flash",
        web_search_adapter=DASHSCOPE_CHAT_ADAPTER,
        web_search_model="qwen3.7-plus",
        cache_capability=_QWEN_DIAGNOSTIC_CACHE_CAPABILITY,
        setup_warning=(
            "DashScope API keys are region-specific; use a key from the US region. "
            "The qwen3-coder-* line shuts down 2026-10-10 in every region — use "
            "qwen3.7-plus for code work."
        ),
    ),
    ProfilePreset(
        key="qwen-cn",
        provider_key="qwen",
        label="Alibaba Qwen / DashScope (China)",
        protocol="openai_compat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        model_choices=(
            ReviewedModel("qwen3.7-plus", "qwen-normal"),
            ReviewedModel("qwen3.8-max", "qwen-normal"),
            ReviewedModel("qwen3.8-flash", "qwen-expanded"),
            ReviewedModel("qwen3.7-flash", "qwen-expanded"),
        ),
        suggested_model_descriptions={
            # Alibaba's documented coding recommendation is qwen3.7-plus; the
            # qwen3-coder-* line shuts down 2026-10-10 in every region.
            # qwen3.7-max, qwen3.6-flash and qwen-flash moved to the Legacy
            # table (still callable, not aliased).
            "qwen3.7-plus": "default - 1M context, balanced cost, coding pick",
            "qwen3.8-max": "advanced - newest multimodal flagship, 1M context",
            "qwen3.8-flash": "fast - low-cost multimodal tier, 1M context",
            "qwen3.7-flash": "economy - cheapest current-gen model, 1M context",
        },
        model_aliases=_QWEN_RETIRED_MODEL_ALIASES,
        validation_model="qwen3.7-flash",
        web_search_adapter=DASHSCOPE_CHAT_ADAPTER,
        web_search_model="qwen3.7-plus",
        cache_capability=_QWEN_DIAGNOSTIC_CACHE_CAPABILITY,
        setup_warning="DashScope API keys are region-specific; use a key from the China region.",
    ),
    ProfilePreset(
        key="zhipu",
        provider_key="zhipu",
        label="Zhipu / GLM (China)",
        protocol="openai_compat",
        base_url="https://open.bigmodel.cn/api/paas/v4/",
        api_key_env="ZHIPUAI_API_KEY",
        connection_description="Uses a China BigModel account. Global Z.AI API keys and Coding Plan subscriptions use separate connections.",
        model_choices=(
            ReviewedModel("glm-5.3", "glm-normal"),
            ReviewedModel("glm-5.3-flash", "glm-normal"),
            ReviewedModel("glm-5.2", "glm-expanded"),
            ReviewedModel("glm-4.7-flashx", "glm-expanded"),
            ReviewedModel("glm-4.7-flash", "glm-expanded"),
        ),
        suggested_model_descriptions={
            # glm-5.3 / glm-5.3-flash cannot disable thinking (thinking.type=
            # disabled is rejected); reasoning_effort low|high|max is the knob.
            # glm-5.2 keeps the optional-thinking contract at the same price.
            # glm-5.1 / glm-5-turbo / glm-4.7 remain callable but are dominated.
            "glm-5.3": "default - 1M context, agentic coding flagship",
            "glm-5.3-flash": "fast - 1M multimodal, ~1/10 the price of glm-5.3",
            "glm-5.2": "fallback - previous flagship, thinking toggleable, 1M",
            "glm-4.7-flashx": "economy - 200K context, no free-tier rate caps",
            "glm-4.7-flash": "free - free tier, 200K context, rate limited",
        },
        # No aliases on purpose: glm-5.1, glm-5-turbo, glm-4.7 etc. remain
        # individually priced and callable — remapping would silently change
        # what users are billed.
        validation_model="glm-4.7-flash",
        web_search_adapter=ZHIPU_WEB_SEARCH_ADAPTER,
        # The Zhipu web_search tool endpoint is model-independent; this is the
        # label attached to hosted-search results.
        web_search_model="glm-5.3",
    ),
    ProfilePreset(
        key="zai",
        provider_key="zai",
        label="Z.AI GLM (pay-as-you-go)",
        protocol="openai_compat",
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        model_choices=(
            ReviewedModel("glm-5.3", "glm-normal"),
            ReviewedModel("glm-5.3-flash", "glm-normal"),
        ),
        suggested_model_descriptions={
            "glm-5.3": "default - agentic coding model",
            "glm-5.3-flash": "fast - multimodal coding model",
        },
        validation_model="glm-5.3-flash",
        connection_description="Uses your global Z.AI API balance. API usage is billed separately from a GLM Coding Plan subscription.",
        setup_warning="Use a Standard API Key from Z.AI for this general API connection. Coding Plan quota uses the separate Coding Plan connection.",
    ),
    ProfilePreset(
        key="zai-coding-plan",
        provider_key="zai_coding_plan",
        label="Z.AI GLM Coding Plan",
        protocol="openai_compat",
        base_url="https://api.z.ai/api/coding/paas/v4",
        api_key_env="ZAI_API_KEY",
        model_choices=(
            ReviewedModel("glm-5.3", "glm-normal"),
            ReviewedModel("glm-5.3-flash", "glm-normal"),
        ),
        suggested_model_descriptions={
            # docs.z.ai/devpack/overview lists exactly these two plan models.
            # Both reason unconditionally (effort low|high|max, default max).
            "glm-5.3": "default - latest agentic coding model, 1M context",
            "glm-5.3-flash": "fast - 1M multimodal, 3x the plan quota of glm-5.3",
        },
        model_aliases={
            # The plan proxy already routes these server-side; the local remap
            # keeps context/capability metadata honest for saved configs.
            "glm-5.2": "glm-5.3",
            "glm-5.1": "glm-5.3",
            "glm-5-turbo": "glm-5.3-flash",
            "glm-4.7": "glm-5.3-flash",
        },
        # GLM-5.3-Flash consumes a third of the plan credits of GLM-5.3 and is
        # available on every Coding Plan tier, so use it for the credential probe.
        validation_model="glm-5.3-flash",
        cache_capability=_ZAI_CODING_PLAN_CACHE_CAPABILITY,
        setup_warning=(
            "Requires a Z.AI GLM Coding Plan key for supported agent tools. "
            "General API balance and China BigModel keys use different endpoints and connections."
        ),
        connection_description=(
            "Uses your Z.AI GLM Coding Plan subscription quota. "
            "This is separate from the general Z.AI API, which is billed against API balance."
        ),
        notes=(
            "Subscription Coding Plan endpoint, not the general Z.AI or China Zhipu API. "
            "All plan tiers offer GLM-5.3 and GLM-5.3-Flash; GLM-5.2/5.1 requests are "
            "routed by the server to GLM-5.3 and GLM-5-Turbo/4.7 to GLM-5.3-Flash. "
            "Quota is credit-based (off-peak, including weekends, costs 50%)."
        ),
    ),
    ProfilePreset(
        key="moonshot",
        provider_key="moonshot",
        label="Kimi",
        protocol="openai_compat",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        model_choices=(
            ReviewedModel("kimi-k2.7-code", "kimi-normal"),
            ReviewedModel("kimi-k3", "kimi-normal"),
            ReviewedModel("kimi-k2.7-code-highspeed", "kimi-normal"),
            ReviewedModel("kimi-k2.6", "kimi-normal"),
        ),
        suggested_model_descriptions={
            # k2.7-code is the deliberate default: k3 is always-thinking at
            # pinned max effort and ~3x the input price — escalate to it when a
            # task needs the 1M window, don't route routine turns through it.
            "kimi-k2.7-code": "default - 256K context, long-horizon agentic coding",
            "kimi-k3": "advanced - 1M context, always-thinking at max effort",
            "kimi-k2.7-code-highspeed": "fast - ~180 tok/s coding variant, 256K context",
            "kimi-k2.6": "fallback - 256K context, thinking toggleable",
        },
        model_aliases={
            # Every id below now 404s on the platform (K2 family retired
            # 2026-05-25; kimi-k2.5 and moonshot-v1-* on 2026-08-31). Moonshot's
            # stated migration target is kimi-k3, but that is ~3x the price of
            # the retired tiers, so cheap retired ids land on kimi-k2.6 — the
            # only model here that still honours a thinking-off flag.
            "kimi-k2": "kimi-k2.6",
            "kimi-k2.5": "kimi-k2.6",
            "kimi-k2-thinking": "kimi-k2.6",
            "kimi-k2-thinking-turbo": "kimi-k2.6",
            "kimi-k2-0905-preview": "kimi-k2.6",
            "kimi-k2-0711-preview": "kimi-k2.6",
            "kimi-k2-turbo-preview": "kimi-k2.7-code-highspeed",
            "kimi-latest": "kimi-k2.6",
            "kimi-thinking-preview": "kimi-k2.6",
            "moonshot-v1-8k": "kimi-k2.6",
            "moonshot-v1-32k": "kimi-k2.6",
            "moonshot-v1-128k": "kimi-k2.6",
            "moonshot-v1-auto": "kimi-k2.6",
        },
        validation_model="kimi-k2.6",
        web_search_adapter=MOONSHOT_KIMI_ADAPTER,
        # Moonshot's pricing pages flag $web_search as "being updated; not
        # recommended in the near term". Hosted search stays on kimi-k2.6, the
        # cheapest id the docs still list as $web_search-capable.
        web_search_model="kimi-k2.6",
        cache_capability=_MOONSHOT_AUTOMATIC_CACHE_CAPABILITY,
        setup_warning=(
            "Moonshot API keys are region-scoped; use a key from the international "
            "platform (platform.kimi.ai) with this endpoint."
        ),
    ),
    ProfilePreset(
        key="kimi-code",
        provider_key="moonshot",
        label="Kimi Code",
        protocol="openai_compat",
        base_url="https://api.kimi.com/coding/v1",
        api_key_env="KIMI_API_KEY",
        model_choices=(
            ReviewedModel("k3", "kimi-normal"),
            ReviewedModel("k3-256k", "kimi-normal"),
            ReviewedModel("kimi-for-coding", "kimi-normal"),
            ReviewedModel("kimi-for-coding-highspeed", "kimi-normal"),
        ),
        suggested_model_descriptions={
            # Tier gating: kimi-for-coding = all members; k3 / k3-256k =
            # Moderato+ (k3 is 256K there, 1M only on Allegretto+); -highspeed =
            # Allegretto+ only. Default reasoning_effort on this endpoint is
            # "high"; disabling thinking silently routes to K2.6.
            "k3": "default - 256K context, 1M on Allegretto+",
            "k3-256k": "economy - K3 pinned to 256K, ~half the quota burn of k3",
            "kimi-for-coding": "coding - 256K context, all membership tiers",
            "kimi-for-coding-highspeed": "fast - 256K context, Allegretto tier or above",
        },
        model_aliases={
            # Cross-endpoint remaps: these are live, DIFFERENT ids on
            # platform.moonshot.ai — legal only inside this preset's alias table.
            "kimi-k3": "k3",
            "kimi-k2.7-code": "kimi-for-coding",
            "kimi-k2.7-code-highspeed": "kimi-for-coding-highspeed",
        },
        # Validation is a billed call against metered membership quota — no
        # /models endpoint exists on this surface.
        validation_model="kimi-for-coding",
        setup_warning=(
            "Requires a Kimi membership key from the kimi.com console; "
            "platform.kimi.ai pay-as-you-go keys are not valid here. "
            "Turning reasoning off routes requests to K2.6 (a different model)."
        ),
    ),
    ProfilePreset(
        key="moonshot-cn",
        provider_key="moonshot",
        label="Kimi (China)",
        protocol="openai_compat",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        model_choices=(
            ReviewedModel("kimi-k2.7-code", "kimi-normal"),
            ReviewedModel("kimi-k3", "kimi-normal"),
            ReviewedModel("kimi-k2.7-code-highspeed", "kimi-normal"),
            ReviewedModel("kimi-k2.6", "kimi-normal"),
        ),
        suggested_model_descriptions={
            # k2.7-code is the deliberate default: k3 is always-thinking at
            # pinned max effort and ~3x the input price — escalate to it when a
            # task needs the 1M window, don't route routine turns through it.
            "kimi-k2.7-code": "default - 256K context, long-horizon agentic coding",
            "kimi-k3": "advanced - 1M context, always-thinking at max effort",
            "kimi-k2.7-code-highspeed": "fast - ~180 tok/s coding variant, 256K context",
            "kimi-k2.6": "fallback - 256K context, thinking toggleable",
        },
        model_aliases={
            # Every id below now 404s on the platform (K2 family retired
            # 2026-05-25; kimi-k2.5 and moonshot-v1-* on 2026-08-31). Moonshot's
            # stated migration target is kimi-k3, but that is ~3x the price of
            # the retired tiers, so cheap retired ids land on kimi-k2.6 — the
            # only model here that still honours a thinking-off flag.
            "kimi-k2": "kimi-k2.6",
            "kimi-k2.5": "kimi-k2.6",
            "kimi-k2-thinking": "kimi-k2.6",
            "kimi-k2-thinking-turbo": "kimi-k2.6",
            "kimi-k2-0905-preview": "kimi-k2.6",
            "kimi-k2-0711-preview": "kimi-k2.6",
            "kimi-k2-turbo-preview": "kimi-k2.7-code-highspeed",
            "kimi-latest": "kimi-k2.6",
            "kimi-thinking-preview": "kimi-k2.6",
            "moonshot-v1-8k": "kimi-k2.6",
            "moonshot-v1-32k": "kimi-k2.6",
            "moonshot-v1-128k": "kimi-k2.6",
            "moonshot-v1-auto": "kimi-k2.6",
        },
        validation_model="kimi-k2.6",
        web_search_adapter=MOONSHOT_KIMI_ADAPTER,
        # Moonshot's pricing pages flag $web_search as "being updated; not
        # recommended in the near term". Hosted search stays on kimi-k2.6, the
        # cheapest id the docs still list as $web_search-capable.
        web_search_model="kimi-k2.6",
        cache_capability=_MOONSHOT_AUTOMATIC_CACHE_CAPABILITY,
        setup_warning=(
            "Moonshot API keys are region-scoped; use a key from the mainland-China "
            "platform (platform.moonshot.cn) with this endpoint."
        ),
    ),
    ProfilePreset(
        key="minimax",
        provider_key="minimax",
        label="MiniMax",
        protocol="openai_compat",
        base_url="https://api.minimax.io/v1",
        api_key_env="MINIMAX_API_KEY",
        model_choices=(
            ReviewedModel("MiniMax-M3", "minimax-expanded"),
            ReviewedModel("MiniMax-M3.1-Flash", "minimax-expanded"),
            ReviewedModel("MiniMax-M2.7", "minimax-normal"),
            ReviewedModel("MiniMax-M2.7-highspeed", "minimax-normal"),
            ReviewedModel("MiniMax-M2.5", "minimax-expanded"),
        ),
        suggested_model_descriptions={
            # M3 exposes thinking.type adaptive|disabled; M2.x cannot disable
            # thinking. M3 input above 512K bills at 2x the base rate.
            # MiniMax-M2 / M2.5 stay live as separately priced legacy ids, so
            # no alias remaps them.
            "MiniMax-M3": "default - 1M context, multimodal agentic coding",
            "MiniMax-M3.1-Flash": "fast - 1M context, five reasoning effort levels",
            "MiniMax-M2.7": "coding - 204K context, prior flagship",
            "MiniMax-M2.7-highspeed": "fast - same weights as M2.7, latency-tuned",
            "MiniMax-M2.5": "fallback - stable prior generation, 204K",
        },
        validation_model="MiniMax-M2.5",
        web_search_adapter=MINIMAX_CODING_PLAN_ADAPTER,
        setup_warning=(
            "MiniMax hosted web search requires a Token Plan key; pay-as-you-go model keys "
            "cannot call the Token Plan search endpoint."
        ),
        notes=(
            "Chat uses the OpenAI-compatible MiniMax API. Web search uses MiniMax's Token Plan "
            "search endpoint when the configured key has Token Plan access."
        ),
    ),
    ProfilePreset(
        key="xiaomi-mimo",
        provider_key="xiaomi",
        label="Xiaomi MiMo",
        protocol="openai_compat",
        base_url="https://api.xiaomimimo.com/v1",
        api_key_env="XIAOMI_API_KEY",
        model_choices=(
            ReviewedModel("mimo-v2.6-pro", "mimo-normal"),
            ReviewedModel("mimo-v2.6-flash", "mimo-expanded"),
            ReviewedModel("mimo-v2.6-pro-ultraspeed", "mimo-normal"),
            ReviewedModel("mimo-v2.5-pro", "mimo-normal"),
            ReviewedModel("mimo-v2.5", "mimo-expanded"),
        ),
        suggested_model_descriptions={
            # V2.6 API ids verified against Xiaomi's 2026-09-22 launch and
            # https://mimo.mi.com/docs/en-US/quick-start/summary/model.
            # V2.5 remains callable until 2026-10-21 10:00 (UTC+8).
            "mimo-v2.6-pro": "default - flagship coding & agents, multimodal (1M context)",
            "mimo-v2.6-flash": "efficient - multimodal coding & agents (1M context)",
            "mimo-v2.6-pro-ultraspeed": (
                "fast - Pro UltraSpeed, multimodal (1M context, 10x Pro token price)"
            ),
            "mimo-v2.5-pro": "legacy - previous flagship (retires 2026-10-21)",
            "mimo-v2.5": "legacy - previous omni model (retires 2026-10-21)",
        },
        validation_model="mimo-v2.6-pro",
        model_aliases={
            # Preserve existing V2.5 mappings while those models remain callable;
            # the retired V2 Flash id follows Xiaomi's server-side routing.
            "mimo": "mimo-v2.5-pro",
            "mimo-v2-flash": "mimo-v2.5",
        },
    ),
    ProfilePreset(
        key="bytedance",
        provider_key="bytedance",
        label="ByteDance Doubao",
        protocol="openai_compat",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_env="ARK_API_KEY",
        model_choices=(
            ReviewedModel("doubao-seed-evolving", "seed-expanded"),
            ReviewedModel("doubao-seed-2-1-pro-260628", "seed-normal"),
            ReviewedModel("doubao-seed-2-1-turbo-260628", "seed-expanded"),
            ReviewedModel("doubao-seed-2-0-mini-260428", "seed-expanded"),
        ),
        suggested_model_descriptions={
            # Ark's recommended table (2026-09-02). The seed-2-0-*-260215 ids
            # remain callable as previous-generation snapshots and are not
            # aliased. Prices on ark.cn-beijing are CNY.
            "doubao-seed-evolving": "default - rolling latest coding/agent model, 1M",
            "doubao-seed-2-1-pro-260628": "advanced - seed 2.1 flagship, 256K, flat pricing",
            "doubao-seed-2-1-turbo-260628": "fast - half the price of 2.1 pro, 256K",
            "doubao-seed-2-0-mini-260428": "economy - cheapest current snapshot, 224K input",
        },
        validation_model="doubao-seed-2-0-mini-260428",
        web_search_adapter=VOLCENGINE_WEB_SEARCH_ADAPTER,
        setup_warning=(
            "Ids follow Ark's model list (docs.volcengine.com/docs/82379/1330310); "
            "Ark may still require endpoint ids (ep-...) on some accounts. "
            "doubao-seed-evolving is a rolling id updated weekly — return "
            "encrypted_content in history and keep request config stable within "
            "a session."
        ),
    ),
    ProfilePreset(
        key="groq",
        provider_key="groq",
        label="Groq",
        protocol="openai_compat",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        model_choices=(
            ReviewedModel("openai/gpt-oss-120b", "gpt-oss-expanded"),
            ReviewedModel("qwen/qwen3.8-27b", "qwen-expanded"),
            ReviewedModel("openai/gpt-oss-20b", "gpt-oss-expanded"),
            ReviewedModel("groq/compound", "general-expanded"),
        ),
        suggested_model_descriptions={
            # groq/compound runs server-side built-in tools and does NOT accept
            # client tool_call — never route normal agent tool loops to it.
            # qwen/qwen3.6-27b stays callable (preview) but 3.8-27b supersedes it.
            "openai/gpt-oss-120b": "default - 131K context, adjustable reasoning",
            "qwen/qwen3.8-27b": "coding - thinking modes and vision, preview tier",
            "openai/gpt-oss-20b": "fast - cheapest non-deprecated production id",
            "groq/compound": "agentic - server-side web search and code exec",
        },
        model_aliases={
            # Both llama ids left the free/developer tiers 2026-08-16 (Groq
            # deprecations table; enterprise committed-spend keys still see
            # them); the other retired ids remap per the same table.
            "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
            "llama-3.1-8b-instant": "openai/gpt-oss-20b",
            "qwen/qwen3-32b": "openai/gpt-oss-120b",
            "meta-llama/llama-4-scout-17b-16e-instruct": "openai/gpt-oss-120b",
            "meta-llama/llama-4-maverick-17b-128e-instruct": "openai/gpt-oss-120b",
            "moonshotai/kimi-k2-instruct": "openai/gpt-oss-120b",
            "moonshotai/kimi-k2-instruct-0905": "openai/gpt-oss-120b",
        },
        validation_model="openai/gpt-oss-20b",
        web_search_adapter=GROQ_COMPOUND_ADAPTER,
        web_search_model="groq/compound-mini",
        setup_warning=(
            "Groq is mostly OpenAI-compatible; avoid preview-only models as production "
            "defaults (qwen/qwen3.8-27b is preview and may be pulled without notice)."
        ),
    ),
    ProfilePreset(
        key="cerebras",
        provider_key="cerebras",
        label="Cerebras",
        protocol="openai_compat",
        base_url="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
        model_choices=(
            ReviewedModel("gpt-oss-120b", "gpt-oss-expanded"),
            ReviewedModel("gemma-4-31b", "gemma-expanded"),
        ),
        suggested_model_descriptions={
            # Cerebras' public catalog is exactly these two models; GLM 5.x,
            # Kimi, Qwen-Coder, MiniMax and DeepSeek exist only as dedicated
            # (enterprise) endpoints with no public id. Context is the free-tier
            # floor (65K); paid keys get 131K / 40K output. gpt-oss-120b cannot
            # disable reasoning (effort low|medium|high).
            "gpt-oss-120b": "default - reasoning + tools, ~3000 tok/s",
            "gemma-4-31b": "fallback - only image-input model, optional reasoning",
        },
        model_aliases={
            # The llama family left Cerebras public endpoints 2026-02-16 (and
            # "llama3.3-70b" was never a valid spelling of the id). zai-glm-4.7
            # was deprecated 2026-08-17 with no named successor.
            "llama3.3-70b": "gpt-oss-120b",
            "llama-3.3-70b": "gpt-oss-120b",
            "llama3.1-70b": "gpt-oss-120b",
            "llama3.1-8b": "gpt-oss-120b",
            "qwen-3-32b": "gpt-oss-120b",
            "qwen-3-coder-480b": "gpt-oss-120b",
            "zai-glm-4.6": "gpt-oss-120b",
            "zai-glm-4.7": "gpt-oss-120b",
            "deepseek-r1-distill-llama-70b": "gpt-oss-120b",
        },
        validation_model="gpt-oss-120b",
        setup_warning=(
            "Cerebras public endpoints serve only gpt-oss-120b and gemma-4-31b; "
            "zai-glm-4.7 was deprecated 2026-08-17 and now remaps to gpt-oss-120b."
        ),
    ),
    ProfilePreset(
        key="mistral",
        provider_key="mistral",
        label="Mistral AI",
        protocol="openai_compat",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        model_choices=(
            ReviewedModel("mistral-medium-3-5", "mistral-normal"),
            ReviewedModel("mistral-large-4", "mistral-normal"),
            ReviewedModel("mistral-large-2512", "mistral-normal"),
            ReviewedModel("mistral-small-2603", "mistral-expanded"),
            ReviewedModel("zai-glm-5-3", "glm-normal"),
            ReviewedModel("zai-glm-5-2", "glm-expanded"),
            ReviewedModel("codestral-2508", "mistral-expanded"),
            ReviewedModel("ministral-8b-2512", "mistral-expanded"),
        ),
        suggested_model_descriptions={
            # codestral is FIM/completion-oriented — routers should prefer the
            # default for multi-file agentic patch turns. The hosted GLM
            # routes are Public Preview with 1M context and a 1-month
            # deprecation notice.
            "mistral-medium-3-5": "default - agentic and coding flagship, 256K",
            "mistral-large-4": "advanced - Mistral Large 4, 1M context, Low/Medium/High reasoning",
            "mistral-large-2512": "advanced - mistral large 3, 675B MoE, 256K",
            "mistral-small-2603": "fast - mistral small 4, low latency",
            "zai-glm-5-3": "coding - hosted GLM-5.3, 1M context, preview tier",
            "zai-glm-5-2": "coding - hosted GLM-5.2, 1M context, preview tier",
            "codestral-2508": "fim - code completion and FIM, 128K context",
            "ministral-8b-2512": "economy - small tool-capable model",
        },
        model_aliases={
            # Mistral documents mistral-medium-3-5 as the primary API id. Keep
            # the former Alysis Code default as a compatibility alias, while the
            # provider-managed -latest alias passes through unchanged.
            # devstral-2512 / magistral-* / mistral-medium-2508 were deprecated
            # 2026-05-22 (still callable under the 6-month GA notice policy).
            "mistral-large-4-0": "mistral-large-4",
            "mistral-medium-2604": "mistral-medium-3-5",
            "mistral-medium-3": "mistral-medium-3-5",
            "mistral-medium-2508": "mistral-medium-3-5",
            "mistral-medium-2505": "mistral-medium-3-5",
            "mistral-small-latest": "mistral-small-2603",
            "mistral-small-2506": "mistral-small-2603",
            "mistral-large-latest": "mistral-large-2512",
            "mistral-large-2411": "mistral-medium-3-5",
            "mistral-large-2407": "mistral-large-2512",
            "codestral-latest": "codestral-2508",
            "devstral-2512": "mistral-medium-3-5",
            "devstral-latest": "mistral-medium-3-5",
            "devstral-medium-latest": "mistral-medium-3-5",
            "devstral-medium-2507": "mistral-medium-3-5",
            "devstral-small-2507": "mistral-small-2603",
            "labs-devstral-small-2512": "mistral-medium-3-5",
            "magistral-medium-latest": "mistral-medium-3-5",
            "magistral-small-latest": "mistral-small-2603",
            "ministral-8b-latest": "ministral-8b-2512",
            "open-mistral-nemo-2407": "ministral-8b-2512",
        },
        validation_model="ministral-3b-2512",
        web_search_adapter=MISTRAL_CONVERSATIONS_ADAPTER,
        web_search_model="mistral-medium-latest",
        cache_capability=_MISTRAL_PROMPT_CACHE_CAPABILITY,
    ),
    ProfilePreset(
        key="xai",
        provider_key="xai",
        label="xAI Grok",
        protocol="openai_compat",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        model_choices=(
            ReviewedModel("grok-4.7", "grok-normal"),
            ReviewedModel("grok-4.6", "grok-normal"),
            ReviewedModel("grok-4.5", "grok-normal"),
            ReviewedModel("grok-build-0.1", "grok-expanded"),
            ReviewedModel("grok-4.3", "grok-normal"),
            ReviewedModel("grok-4.20-0309-reasoning", "grok-normal"),
            ReviewedModel("grok-4.20-0309-non-reasoning", "grok-expanded"),
        ),
        suggested_model_descriptions={
            # Regions: grok-4.3 is us-east-1 only; 4.6/4.5/build-0.1/4.20 are
            # us-east-1 + us-west-2. Batch API only on 4.3 and the 4.20 family.
            # 4.7 has a shared 500K window with no separate text-output limit.
            "grok-4.7": "default - Grok 4.7, coding and agents, vision, 500K context",
            "grok-4.6": "fallback - previous flagship for coding and agents, 500K",
            "grok-4.5": "fallback - previous flagship for coding and agents",
            "grok-build-0.1": "coding - agentic engineering model, 256K",
            "grok-4.3": "advanced - 1M context window",
            "grok-4.20-0309-reasoning": "reasoning - dedicated snapshot, 1M context",
            "grok-4.20-0309-non-reasoning": "fast - no-reasoning snapshot, 1M context",
        },
        model_aliases={
            # Retired 2026-05-15; xAI keeps the slugs resolving as redirects
            # (no shutdown date is published). The *-non-reasoning slugs
            # deliberately map to the non-reasoning snapshot (xAI's own redirect
            # lands them on grok-4.3 with effort=none, which the alias table
            # cannot express).
            "grok-code-fast-1": "grok-build-0.1",
            "grok-4": "grok-4.3",
            "grok-4-0709": "grok-4.3",
            "grok-4-fast": "grok-4.3",
            "grok-4.3-latest": "grok-4.3",
            "grok-4-fast-reasoning": "grok-4.3",
            "grok-4-1-fast-reasoning": "grok-4.3",
            "grok-4-fast-non-reasoning": "grok-4.20-0309-non-reasoning",
            "grok-4-1-fast-non-reasoning": "grok-4.20-0309-non-reasoning",
            "grok-3": "grok-4.3",
        },
        validation_model="grok-4.7",
        web_search_adapter=XAI_RESPONSES_ADAPTER,
        cache_capability=_XAI_CONVERSATION_HEADER_CACHE_CAPABILITY,
        setup_warning=(
            "Retired slugs (grok-4, grok-4-fast, grok-3, grok-code-fast-1) still "
            "resolve as redirects billed at grok-4.3 / grok-build-0.1 rates; migrate "
            "pinned configs explicitly. Ids use dots, not dashes (grok-4.7). "
            "grok-4.3 is served from us-east-1 only. "
            "Grok 4.7 Fast is not available on the public xAI API. "
            "Grok 4.7 catalog prices are base rates; xAI doubles input, cached-input "
            "and output rates above 200K prompt tokens."
        ),
    ),
    ProfilePreset(
        key="cohere",
        provider_key="cohere",
        label="Cohere (compat)",
        protocol="openai_compat",
        base_url="https://api.cohere.ai/compatibility/v1",
        api_key_env="COHERE_API_KEY",
        model_choices=(
            ReviewedModel("command-a-plus-05-2026", "command-normal"),
            ReviewedModel("command-a-reasoning-08-2025", "command-normal"),
            ReviewedModel("command-a-03-2025", "command-normal"),
            ReviewedModel("command-r7b-12-2024", "command-expanded"),
        ),
        suggested_model_descriptions={
            # Reasoning toggle (thinking=disabled) is a native Chat V2 param and
            # may not pass through /compatibility/v1 — treat as thinking-on.
            "command-a-plus-05-2026": "default - newest command a+, 128K context",
            "command-a-reasoning-08-2025": "reasoning - 256K context, thinking is a toggle",
            "command-a-03-2025": "advanced - 256K context, prior flagship",
            "command-r7b-12-2024": "economy - cheapest live chat model, 128K",
        },
        model_aliases={
            "command": "command-a-03-2025",
            "command-light": "command-r-08-2024",
            "command-r": "command-r-08-2024",
            "command-r-plus": "command-r-plus-08-2024",
        },
        validation_model="command-r7b-12-2024",
        web_search_adapter=COHERE_WEB_SEARCH_ADAPTER,
        setup_warning=(
            "Cohere shut down the v1 hosted web-search connector on 2025-09-15; "
            "hosted web search on this preset needs migration to an external "
            "search adapter."
        ),
        notes=(
            "Chat uses Cohere's OpenAI compatibility API (api.cohere.ai/compatibility/v1 — "
            "documented and correct; do not migrate to v2/chat). The v1 hosted web-search "
            "connector this preset's adapter targeted was shut down 2025-09-15."
        ),
    ),
    ProfilePreset(
        key="openrouter",
        provider_key="openrouter",
        label="OpenRouter (gateway)",
        protocol="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        model_choices=(
            ReviewedModel("anthropic/claude-sonnet-5", "claude-normal"),
            ReviewedModel("anthropic/claude-opus-5", "claude-normal"),
            ReviewedModel("anthropic/claude-opus-5.5", "claude-normal"),
            ReviewedModel("anthropic/claude-fable-5.1", "claude-normal"),
            ReviewedModel("openai/gpt-6-astra", "gpt-normal"),
            ReviewedModel("openai/gpt-6-sol", "gpt-normal"),
            ReviewedModel("openai/gpt-6-luna", "gpt-expanded"),
            ReviewedModel("openai/gpt-5.6-terra", "gpt-normal"),
            ReviewedModel("openai/gpt-5.6-luna", "gpt-expanded"),
            ReviewedModel("google/gemini-3.8-flash", "gemini-normal"),
            ReviewedModel("x-ai/grok-4.7", "grok-normal"),
            ReviewedModel("x-ai/grok-4.6", "grok-normal"),
            ReviewedModel("z-ai/glm-5.3", "glm-normal"),
            ReviewedModel("z-ai/glm-5.3-flashx", "glm-normal"),
            ReviewedModel("z-ai/glm-5.3-flash", "glm-normal"),
            ReviewedModel("moonshotai/kimi-k3", "kimi-normal"),
            ReviewedModel("deepseek/deepseek-v4-pro-0813", "deepseek-normal"),
            ReviewedModel("deepseek/deepseek-v4.1-flash", "deepseek-expanded"),
            ReviewedModel("deepseek/deepseek-v4-flash-0731", "deepseek-normal"),
            ReviewedModel("deepseek/deepseek-v4-flash-vision-exp", "deepseek-expanded"),
            ReviewedModel("qwen/qwen3.8-max", "qwen-normal"),
            ReviewedModel("qwen/qwen3.8-max-0902", "qwen-normal"),
            ReviewedModel("xiaomi/mimo-v2.6-pro", "mimo-normal"),
            ReviewedModel("xiaomi/mimo-v2.6-flash", "mimo-expanded"),
            ReviewedModel("xiaomi/mimo-v2.6-pro-ultraspeed", "mimo-normal"),
            ReviewedModel("inception/mercury-2.5", "general-expanded"),
        ),
        suggested_model_descriptions={
            # Vendor prefixes are exact: z-ai/ (not zai/), x-ai/, moonshotai/;
            # OpenRouter spells Claude versions with dots (claude-fable-5.1).
            # Avoid '-latest' floating aliases (~z-ai/glm-latest etc.) and
            # rate-limited :free variants for agent loops.
            "anthropic/claude-sonnet-5": "default - coding and agents, 1M context",
            "anthropic/claude-opus-5": "advanced - long-horizon autonomous work, 1M",
            "anthropic/claude-opus-5.5": "advanced - latest Opus, thinking always on, 1M",
            "anthropic/claude-fable-5.1": "reasoning - Mythos-class, always-on thinking",
            "openai/gpt-6-astra": "advanced - GPT-6 flagship, reasoning always on, 1.05M",
            "openai/gpt-6-sol": "coding - latest Sol, complex agentic work, 1.05M",
            "openai/gpt-6-luna": "fast - latest Luna, low-cost focused work, 1.05M",
            "openai/gpt-5.6-terra": "coding - balanced gpt-5.6 tier, 1.05M context",
            "openai/gpt-5.6-luna": "fast - cost-efficient gpt-5.6 tier",
            "google/gemini-3.8-flash": "fallback - newest stable Gemini flash, 1M",
            "x-ai/grok-4.7": "agentic - latest Grok for coding and agents, 500K",
            "x-ai/grok-4.6": "agentic - previous Grok coding model, 500K",
            "z-ai/glm-5.3": "open - GLM-5.3 agentic coding flagship, 1.3M routing",
            "z-ai/glm-5.3-flashx": "fast - GLM-5.3 multimodal reasoning, 1M context",
            "z-ai/glm-5.3-flash": "economy - cheapest 1M-class multimodal tool caller",
            "moonshotai/kimi-k3": "open - Kimi K3 long-horizon coding, 1M multimodal",
            "deepseek/deepseek-v4-pro-0813": "reasoning - DeepSeek V4 Pro snapshot, 1M",
            "deepseek/deepseek-v4.1-flash": "fast - DeepSeek V4.1 Flash with vision, 1M",
            "deepseek/deepseek-v4-flash-0731": "legacy - DeepSeek V4 Flash snapshot, 1M",
            "deepseek/deepseek-v4-flash-vision-exp": (
                "vision preview - image understanding and tools, 1M context"
            ),
            "qwen/qwen3.8-max": "multimodal - flagship Qwen agent model, 1M context",
            "qwen/qwen3.8-max-0902": "coding - September Qwen3.8 Max snapshot, 1M",
            "xiaomi/mimo-v2.6-pro": "coding - MiMo V2.6 Pro with vision, 1M context",
            "xiaomi/mimo-v2.6-flash": "economy - MiMo V2.6 Flash with vision, 1M",
            "xiaomi/mimo-v2.6-pro-ultraspeed": "fast - accelerated MiMo V2.6 Pro, 1M",
            "inception/mercury-2.5": "fast - diffusion reasoning and tool use, 260K",
        },
        validation_model="deepseek/deepseek-v4-flash-0731",
        web_search_adapter=OPENROUTER_WEB_ADAPTER,
        cache_capability=_OPENROUTER_STICKY_SESSION_CACHE_CAPABILITY,
        setup_warning=(
            "OpenRouter routes through upstream providers; availability, pricing, privacy, "
            "and parameter support can vary by route."
        ),
        notes="Single API to many providers' models.",
    ),
    ProfilePreset(
        key="perplexity",
        provider_key="perplexity",
        label="Perplexity Agent API",
        # Sonar Chat Completions (api.perplexity.ai, models sonar / sonar-pro)
        # shuts down 2026-09-27. The Agent API is an OpenAI Responses-format
        # surface (POST /v1/agent; /v1/responses is an accepted alias), so the
        # native Responses client drives it directly.
        protocol="openai_responses",
        base_url="https://api.perplexity.ai/v1",
        api_key_env="PERPLEXITY_API_KEY",
        model_choices=(
            ReviewedModel("perplexity/sonar", "sonar-normal"),
            ReviewedModel("perplexity/glm-5.3", "glm-normal"),
            ReviewedModel("perplexity/kimi-k3", "kimi-normal"),
            ReviewedModel("perplexity/kimi-k2.7-code", "kimi-normal"),
            ReviewedModel("perplexity/deepseek-v4-flash-0731", "deepseek-normal"),
        ),
        suggested_model_descriptions={
            # Perplexity-hosted ids only. Third-party routes (openai/*,
            # anthropic/*, google/*, xai/*) also resolve on this surface but
            # are billed at their vendors' list prices; users can type them in.
            "perplexity/sonar": "default - Perplexity search model, grounded via web_search tool",
            "perplexity/glm-5.3": "coding - hosted GLM-5.3 agentic coding, tools + MCP",
            "perplexity/kimi-k3": "advanced - hosted Kimi K3 long-horizon coding",
            "perplexity/kimi-k2.7-code": "agentic - hosted Kimi K2.7 Code specialist",
            "perplexity/deepseek-v4-flash-0731": "fast - cheapest hosted route ($0.13/$0.26)",
        },
        model_aliases={
            # Retired Sonar Chat Completions ids. Perplexity maps them to the
            # fast/low/medium/high presets; the closest explicit model is the
            # hosted Sonar route.
            "sonar": "perplexity/sonar",
            "sonar-pro": "perplexity/sonar",
            "sonar-reasoning": "perplexity/sonar",
            "sonar-reasoning-pro": "perplexity/sonar",
            "sonar-deep-research": "perplexity/sonar",
        },
        validation_model="perplexity/deepseek-v4-flash-0731",
        web_search_adapter=PERPLEXITY_SONAR_ADAPTER,
        web_search_model="perplexity/sonar",
        setup_warning=(
            "Agent API (Responses format) at https://api.perplexity.ai/v1. Profiles "
            "saved against the old Sonar base URL (https://api.perplexity.ai, models "
            "sonar / sonar-pro) stop working on 2026-09-27 — re-create them from this "
            "preset. temperature/top_p are ignored for GPT-5-family routes."
        ),
        notes=(
            "Perplexity Agent API: one Responses-format endpoint for Perplexity-hosted "
            "Sonar/GLM/Kimi/DeepSeek/Nemotron routes plus third-party vendors, with "
            "built-in web_search, fetch-URL, sandbox, and MCP tools."
        ),
    ),
    ProfilePreset(
        key="together",
        provider_key="together",
        label="Together AI",
        protocol="openai_compat",
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
        model_choices=(
            ReviewedModel("zai-org/GLM-5.3", "glm-normal"),
            ReviewedModel("moonshotai/Kimi-K3", "kimi-normal"),
            ReviewedModel("deepseek-ai/DeepSeek-V4-Pro-0813", "deepseek-normal"),
            ReviewedModel("deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek-normal"),
            ReviewedModel("zai-org/GLM-5.3-Flash", "glm-normal"),
            ReviewedModel("MiniMaxAI/MiniMax-M3", "minimax-expanded"),
            ReviewedModel("MiniMaxAI/MiniMax-M3.1-Flash", "minimax-expanded"),
            ReviewedModel("reflection/Beam-1.0-501B-A23B", "general-expanded"),
            ReviewedModel("nvidia/NVIDIA-Nemotron-3.5-Super-VL", "nemotron-expanded"),
            ReviewedModel("openai/gpt-oss-120b", "gpt-oss-expanded"),
            ReviewedModel("Qwen/Qwen3.5-9B", "qwen-expanded"),
        ),
        suggested_model_descriptions={
            # Ids are case-sensitive and vendor-prefixed. Kimi-K3 reasons
            # unconditionally — never emit a reasoning-off param for it.
            # Kimi-K2.7-Code left serverless 2026-08-27; gpt-oss-20b is
            # scheduled for removal 2026-09-14 (Together names Qwen3.5-9B as
            # its replacement).
            "zai-org/GLM-5.3": "default - agentic coding flagship, 1M context",
            "moonshotai/Kimi-K3": "coding - long-horizon coding, 1M multimodal",
            "deepseek-ai/DeepSeek-V4-Pro-0813": "reasoning - current flagship, 1M context",
            "deepseek-ai/DeepSeek-V4-Flash-0731": "fast - current 1M-context release",
            "zai-org/GLM-5.3-Flash": "economy - cheapest 1M-context tool caller",
            "MiniMaxAI/MiniMax-M3": "multimodal - 512K context, vision + agentic coding",
            "MiniMaxAI/MiniMax-M3.1-Flash": "fast - 1M context, five reasoning effort levels",
            "reflection/Beam-1.0-501B-A23B": "coding - 1M text context, five reasoning effort levels",
            "nvidia/NVIDIA-Nemotron-3.5-Super-VL": "multimodal - 262K context, low/medium/high reasoning",
            "openai/gpt-oss-120b": "open - larger tool-capable GPT-OSS model, 128K context",
            "Qwen/Qwen3.5-9B": "fallback - small vision + tool model, 262K context",
        },
        model_aliases={
            # Fallback policy, NOT vendor renames: Together retires serverless
            # models with a blank successor column (dates from its deprecations
            # page). Several are cross-vendor substitutions — surface the swap
            # to the user at resolution time.
            "zai-org/GLM-5.1": "zai-org/GLM-5.2",
            "moonshotai/Kimi-K2.7-Code": "moonshotai/Kimi-K3",
            "moonshotai/Kimi-K2.6": "moonshotai/Kimi-K3",
            "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8": "moonshotai/Kimi-K3",
            "Qwen/Qwen3-Coder-Next-FP8": "moonshotai/Kimi-K3",
            "deepseek-ai/DeepSeek-V4-Pro": "deepseek-ai/DeepSeek-V4-Pro-0813",
            "openai/gpt-oss-20b": "Qwen/Qwen3.5-9B",
        },
        validation_model="Qwen/Qwen3.5-9B",
        setup_warning=(
            "Together retires serverless models on a published schedule with no "
            "successor mapping — expect id churn; verify access with Together's "
            "Models API. openai/gpt-oss-20b is removed 2026-09-14."
        ),
    ),
    ProfilePreset(
        key="fireworks",
        provider_key="fireworks",
        label="Fireworks AI",
        protocol="openai_compat",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
        model_choices=(
            ReviewedModel("accounts/fireworks/models/glm-5p3", "glm-normal"),
            ReviewedModel("accounts/fireworks/models/kimi-k3", "kimi-normal"),
            ReviewedModel("accounts/fireworks/models/deepseek-v4-pro-0813", "deepseek-normal"),
            ReviewedModel("accounts/fireworks/models/deepseek-v4-flash-0731", "deepseek-normal"),
            ReviewedModel("accounts/fireworks/models/glm-5p3-flash", "glm-normal"),
            ReviewedModel("accounts/fireworks/models/minimax-m3", "minimax-expanded"),
            ReviewedModel("accounts/fireworks/models/qwen3p7-plus", "qwen-normal"),
        ),
        suggested_model_descriptions={
            # 'p' is the decimal convention (5p3 = 5.3). Catalog membership does
            # NOT imply serverless availability on Fireworks — every id here is
            # confirmed serverless-capable. glm-5p2 and kimi-k2p7-code remain
            # serverless and callable; they are superseded, not retired.
            "accounts/fireworks/models/glm-5p3": "default - agentic coding flagship, 1M context",
            "accounts/fireworks/models/kimi-k3": "coding - Fireworks' top code/agent pick, 1M",
            "accounts/fireworks/models/deepseek-v4-pro-0813": (
                "reasoning - current 1M-context production release"
            ),
            "accounts/fireworks/models/deepseek-v4-flash-0731": (
                "fast - current lowest-cost 1M-context release"
            ),
            "accounts/fireworks/models/glm-5p3-flash": (
                "economy - 1M context, image input, ~1/10 of glm-5p3"
            ),
            "accounts/fireworks/models/minimax-m3": "multimodal - 512K context, effort control",
            "accounts/fireworks/models/qwen3p7-plus": "fallback - 262K context, standard tier only",
        },
        model_aliases={
            # qwen2p5-coder is not serverless-capable at all (on-demand GPU
            # only); glm-5p1 lost serverless; the undated DeepSeek ids,
            # gpt-oss-20b and minimax-m2p7 were retired in August 2026 with
            # Fireworks' documented replacements below.
            "accounts/fireworks/models/qwen2p5-coder-32b-instruct": (
                "accounts/fireworks/models/kimi-k2p7-code"
            ),
            "accounts/fireworks/models/glm-5p1": "accounts/fireworks/models/glm-5p2",
            "accounts/fireworks/models/deepseek-v4-pro": (
                "accounts/fireworks/models/deepseek-v4-pro-0813"
            ),
            "accounts/fireworks/models/deepseek-v4-flash": (
                "accounts/fireworks/models/deepseek-v4-flash-0731"
            ),
            "accounts/fireworks/models/gpt-oss-20b": "accounts/fireworks/models/gpt-oss-120b",
            "accounts/fireworks/models/minimax-m2p7": "accounts/fireworks/models/minimax-m3",
        },
        validation_model="accounts/fireworks/models/deepseek-v4-flash-0731",
    ),
    # Account-gated hosted preset — registered after the hosted third-party
    # vendors so listings that read PROFILE_PRESETS order do not headline it.
    ProfilePreset(
        key="alysis",
        provider_key="alysis",
        label="Alysis Code",
        protocol="openai_compat",
        # Resolve the hosted endpoint from portable cloud configuration when
        # materializing a profile; never bake a deployment into the preset.
        base_url="",
        api_key_env=None,
        # Free models are available offline; paid models require the authenticated
        # gateway catalog before the managed account picker offers them.
        # Open models run on Together AI and Mistral Large 4 on Mistral AI
        # (Oct 23, 2026); DeepSeek is paid-only, under a new id.
        model_choices=(
            ReviewedModel("glm-5.3-flash", "glm-normal"),
            ReviewedModel("gpt-6-luna", "gpt-expanded"),
            ReviewedModel("claude-haiku-5-5", "claude-normal"),
            ReviewedModel("deepseek-v4.1-flash", "deepseek-expanded"),
            ReviewedModel("deepseek-v4-pro", "deepseek-normal"),
            ReviewedModel("mistral-large-4", "mistral-normal"),
            ReviewedModel("minimax-m3.1-flash", "minimax-expanded"),
            ReviewedModel("reflection-beam", "general-expanded"),
            ReviewedModel("nemotron-3.5-super-vl", "general-expanded"),
            ReviewedModel("glm-5.3", "glm-normal"),
            ReviewedModel("kimi-k3", "kimi-normal"),
            ReviewedModel("gpt-6.1-terra", "gpt-normal"),
            ReviewedModel("qwen3.8-2.4t", "qwen-normal"),
        ),
        suggested_model_descriptions={
            "claude-haiku-5-5": "Claude Haiku 5.5 - fast coding, 1M context, Free credits; 5x rates above 100K input",
            "gpt-6-luna": "GPT-6 Luna - efficient coding and reasoning, included with free credits",
            "deepseek-v4.1-flash": "DeepSeek V4.1 Flash - reasoning and coding, 1M context, Beta and Alpha",
            "deepseek-v4-pro": "DeepSeek V4 Pro - deep reasoning and coding, 1M context, Alpha",
            "mistral-large-4": "Mistral Large 4 (Preview) - coding, 1M context, Alpha",
            "minimax-m3.1-flash": "MiniMax M3.1 Flash - 1M context, Beta and Alpha, text and code",
            "reflection-beam": "Reflection Beam - 1M context, Beta and Alpha, text and code",
            "nemotron-3.5-super-vl": "NVIDIA Nemotron 3.5 Super VL - 262K context, Beta and Alpha, text and code",
            "glm-5.3": "GLM 5.3 - 1M context, Low/High/Max reasoning, Alpha, text and code",
            "kimi-k3": "Kimi K3 - Alpha, text and code",
            "gpt-6.1-terra": "GPT-6.1 Terra - 1.05M context, Off through Max reasoning; account access applies",
            "qwen3.8-2.4t": "Qwen 3.8 2.4T - availability is controlled by the account catalog",
            "glm-5.3-flash": "GLM 5.3 Flash - coding and reasoning, 1M context, included with free credits",
        },
        validation_model="glm-5.3-flash",
        paid_models=(
            "minimax-m3.1-flash",
            "reflection-beam",
            "nemotron-3.5-super-vl",
            "glm-5.3",
            "kimi-k3",
            "gpt-6.1-terra",
            "qwen3.8-2.4t",
            "deepseek-v4.1-flash",
            "deepseek-v4-pro",
            "mistral-large-4",
        ),
        cache_capability=CacheCapabilitySpec(
            model_family_overrides=(("claude-haiku-5-5", _cache_minimum(512)),), source="preset"
        ),
        # Older releases defaulted to DeepSeek on Free credits. DeepSeek is now
        # paid-only (as deepseek-v4.1-flash), so saved legacy ids move to GLM 5.3
        # Flash, the other free chat model, instead of failing; paid users pick
        # DeepSeek again from the account catalog.
        model_aliases={
            "deepseek-flash": "glm-5.3-flash",
            "deepseek-v4-flash": "glm-5.3-flash",
            "deepseek-v4-flash-vision-exp": "glm-5.3-flash",
            "deepseek-v4.1-flash-expires-on-0910": "glm-5.3-flash",
        },
        setup_warning="Run `alysis login` to connect your Alysis account and use free credits.",
        notes=(
            "GLM 5.3 Flash, GPT-6 Luna, and Claude Haiku 5.5 with free credits; paid plans add "
            "DeepSeek, MiniMax, Mistral and other models from your account catalog. Authenticate with `alysis login`."
        ),
    ),
    ProfilePreset(
        key="ollama",
        provider_key="ollama",
        label="Ollama (local)",
        protocol="openai_compat",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        model_choices=(ReviewedModel("llama3.3", "llama-expanded"),),
        notes="Local Ollama server. No API key required.",
    ),
    ProfilePreset(
        key="lm-studio",
        provider_key="lm-studio",
        label="LM Studio (local)",
        protocol="openai_compat",
        base_url="http://localhost:1234/v1",
        api_key_env=None,
        model_choices=(ReviewedModel("local-model", "general-expanded"),),
        notes="Local LM Studio server. No API key required.",
    ),
    ProfilePreset(
        key="vllm",
        provider_key="vllm",
        label="vLLM (self-hosted)",
        protocol="openai_compat",
        base_url="http://localhost:8000/v1",
        api_key_env=None,
        model_choices=(ReviewedModel("local-model", "general-expanded"),),
    ),
    ProfilePreset(
        key="custom",
        label="Custom (specify URL manually)",
        protocol="openai_compat",
        base_url="",
        api_key_env=None,
        model_choices=(),
        notes="Use for unlisted endpoints. Type the URL during setup.",
    ),
)

PRESET_BY_KEY: dict[str, ProfilePreset] = {preset.key: preset for preset in PROFILE_PRESETS}


def get_preset(key: str) -> ProfilePreset | None:
    return PRESET_BY_KEY.get(str(key or "").strip().lower())


def _preset_base_url(preset: ProfilePreset) -> str:
    if preset.key == "alysis":
        from .alysis_cloud import gateway_base_url

        return gateway_base_url()
    return preset.base_url


def model_display_name(model: str) -> str:
    """Display a release name while retaining the routing ID in configuration."""
    return {
        "deepseek-flash": "DeepSeek V4.1 Flash",
        "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
        "deepseek-v4-pro": "DeepSeek V4 Pro",
        "minimax-m3": "MiniMax M3",
        "mistral-large-4": "Mistral Large 4 (Preview)",
        "minimax-m3.1-flash": "MiniMax M3.1 Flash",
        "minimaxai/minimax-m3.1-flash": "MiniMax M3.1 Flash",
        "qwen3.8-flash": "Qwen3.8 Flash",
        "gpt-oss-120b": "GPT-OSS 120B",
        "reflection-beam": "Reflection Beam",
        "reflection/beam-1.0-501b-a23b": "Reflection Beam",
        "nemotron-3.5-super-vl": "NVIDIA Nemotron 3.5 Super VL",
        "nvidia/nvidia-nemotron-3.5-super-vl": "NVIDIA Nemotron 3.5 Super VL",
        "mistral-small-4": "Mistral Small 4",
        "glm-5.3": "GLM 5.3",
        "kimi-k3": "Kimi K3",
        "muse-spark-open": "Muse Spark Open",
        "glm-5.3-flash": "GLM 5.3 Flash",
        "gpt-6-luna": "GPT-6 Luna",
        "gpt-6.1-sol": "GPT-6.1 Sol",
        "gpt-6.1-terra": "GPT-6.1 Terra",
        "qwen3.8-2.4t": "Qwen 3.8 2.4T",
        "claude-sonnet-5-5": "Claude Sonnet 5.5",
        "claude-haiku-5-5": "Claude Haiku 5.5",
        "grok-4.7": "Grok 4.7",
    }.get(model.casefold(), model)


def model_options_for_preset(
    preset: ProfilePreset, *, available_models: set[str] | None = None
) -> tuple[tuple[str, str, str], ...]:
    """Return picker rows for the models this preset intentionally supports."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for model in preset.suggested_models:
        model_id = str(model or "").strip()
        if not model_id or model_id in seen:
            continue
        if (
            preset.key == "alysis"
            and available_models is not None
            and model_id not in available_models
        ):
            continue
        if model_id in preset.paid_models and model_id not in (available_models or set()):
            continue
        seen.add(model_id)
        description = str(preset.suggested_model_descriptions.get(model_id) or "").strip()
        label = model_display_name(model_id)
        rows.append((model_id, label, description or "suggested by provider preset"))
    return tuple(rows)


def canonical_model_alias_for_preset(preset: ProfilePreset, model: str) -> str:
    """Map explicit stale provider aliases to the preset's current model ID."""
    raw = str(model or "").strip()
    if not raw:
        return raw
    for alias, canonical in preset.model_aliases.items():
        if str(alias or "").strip().casefold() == raw.casefold():
            normalized = str(canonical or "").strip()
            return normalized or raw
    return raw


def find_preset_for_profile(profile: ProfileSpec) -> ProfilePreset | None:
    """Best-effort mapping from a persisted profile back to a known provider preset."""
    name_match = get_preset(profile.name)
    if name_match is not None and _profile_matches_preset(profile, name_match):
        return name_match

    for preset in PROFILE_PRESETS:
        if preset.key == "custom":
            continue
        if _profile_matches_preset(profile, preset):
            return preset

    if profile.protocol != OPENAI_COMPAT_PROTOCOL:
        return None
    return find_preset_for_base_url(profile.base_url)


def find_preset_for_base_url(base_url: str) -> ProfilePreset | None:
    normalized = _normalized_base_url(base_url)
    if not normalized:
        return None
    if is_alysis_gateway_url(base_url):
        return get_preset("alysis")
    matches: list[ProfilePreset] = []
    for preset in PROFILE_PRESETS:
        if preset.key == "custom":
            continue
        if _normalized_base_url(_preset_base_url(preset)) == normalized:
            matches.append(preset)
    if not matches:
        return None
    compatibility = next(
        (preset for preset in matches if preset.protocol == OPENAI_COMPAT_PROTOCOL),
        None,
    )
    return compatibility or matches[0]


def _profile_matches_preset(profile: ProfileSpec, preset: ProfilePreset) -> bool:
    if str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip() != preset.protocol:
        return False
    if preset.key == "alysis":
        return is_alysis_gateway_url(profile.base_url)
    profile_url = _normalized_base_url(profile.base_url)
    preset_url = _normalized_base_url(_preset_base_url(preset))
    if not profile_url or not preset_url or profile_url != preset_url:
        return False
    return True


_FIRST_PARTY_FAMILY_PROVIDER_KEYS: frozenset[str] = frozenset({"openai", "anthropic", "gemini"})


def profile_provider_family(profile: ProfileSpec) -> str | None:
    """Resolve a profile to a first-party family for protocol conversion and diagnostics."""
    # A third-party preset can speak a first-party wire protocol (Perplexity's
    # Agent API is OpenAI Responses-format). Protocol alone must not classify
    # such a profile as OpenAI/Anthropic/Gemini for conversion or diagnostics.
    matched = find_preset_for_profile(profile)
    if (
        matched is not None
        and matched.provider_key
        and matched.provider_key not in _FIRST_PARTY_FAMILY_PROVIDER_KEYS
    ):
        return None

    protocol = str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
    if protocol == OPENAI_RESPONSES_PROTOCOL:
        return "openai"
    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        return "anthropic"
    if protocol == GEMINI_GENERATE_CONTENT_PROTOCOL:
        return "gemini"
    if protocol == GEMINI_INTERACTIONS_PROTOCOL:
        return "gemini"

    preset = get_preset(profile.name)
    if preset is not None:
        if preset.key in {"openai", "openai-responses"}:
            return "openai"
        if preset.key in {"anthropic", "anthropic-compat", "anthropic-native"}:
            return "anthropic"
        if preset.key in {"gemini", "gemini-compat", "gemini-native"}:
            return "gemini"

    normalized_name = str(profile.name or "").strip().lower()
    if "openai" in normalized_name:
        return "openai"
    if "anthropic" in normalized_name or "claude" in normalized_name:
        return "anthropic"
    if "gemini" in normalized_name or "google" in normalized_name:
        return "gemini"

    parsed = _split_base_url(profile.base_url)
    if parsed[0] == "api.openai.com":
        return "openai"
    if parsed[0] == "api.anthropic.com":
        return "anthropic"
    if parsed[0] == "generativelanguage.googleapis.com":
        return "gemini"
    return None


def target_preset_for_profile_conversion(
    profile: ProfileSpec,
    *,
    target: str,
) -> ProfilePreset | None:
    normalized_target = normalize_conversion_target(target)
    family = profile_provider_family(profile)
    if family is None:
        return None
    preset_key = _CONVERSION_PRESET_BY_FAMILY.get(family, {}).get(normalized_target)
    if preset_key is None:
        return None
    return get_preset(preset_key)


def normalize_conversion_target(value: str) -> str:
    target = str(value or "").strip().lower().replace("_", "-")
    if target in {"native", "first-party", "firstparty"}:
        return "native"
    if target in {"compat", "compatibility", "openai-compatible", "gateway"}:
        return "compatibility"
    raise ValueError("conversion target must be 'native' or 'compatibility'")


def convert_profile_to_preset(profile: ProfileSpec, preset: ProfilePreset) -> ProfileSpec:
    current_model = str(profile.default_model or "").strip()
    default_model = canonical_model_alias_for_preset(preset, current_model)
    target_family = _preset_provider_family(preset)
    if not default_model or _model_known_incompatible_with_family(default_model, target_family):
        default_model = preset.suggested_models[0] if preset.suggested_models else ""
    notes = _converted_profile_notes(profile, preset)

    return ProfileSpec(
        name=profile.name,
        protocol=preset.protocol,
        base_url=_preset_base_url(preset),
        api_key_env=profile.api_key_env or preset.api_key_env,
        extra_headers=dict(profile.extra_headers),
        default_model=default_model,
        reasoning_effort=profile.reasoning_effort,
        web_search_adapter=preset.web_search_adapter,
        web_search_model=preset.web_search_model,
        notes=notes,
    )


def _converted_profile_notes(profile: ProfileSpec, preset: ProfilePreset) -> str:
    notes = str(profile.notes or "").strip()
    if not notes:
        return preset.notes
    source_preset = find_preset_for_profile(profile)
    if source_preset is not None and notes == str(source_preset.notes or "").strip():
        return preset.notes

    lowered = notes.lower()
    target_is_native = preset.protocol in NATIVE_PROFILE_PROTOCOLS
    if target_is_native and (
        "openai-compat" in lowered
        or "openai compatible" in lowered
        or "openai-compatible" in lowered
        or "compatibility mode" in lowered
    ):
        return preset.notes
    if not target_is_native and "native" in lowered:
        return preset.notes
    return notes


def _split_base_url(value: str | None) -> tuple[str, str]:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return "", ""
    path = parsed.path.rstrip("/").lower()
    return (parsed.hostname or "").rstrip(".").lower(), path


def _preset_provider_family(preset: ProfilePreset) -> str | None:
    for family, targets in _CONVERSION_PRESET_BY_FAMILY.items():
        if preset.key in targets.values():
            return family
    return None


def _model_known_incompatible_with_family(model: str, family: str | None) -> bool:
    if family is None:
        return False
    normalized = model.strip().lower()
    model_family = _known_model_family(normalized)
    if model_family is None:
        return False
    if model_family != family:
        return True
    return _has_known_provider_namespace(normalized)


def known_model_family(model: str) -> str | None:
    """Best-effort model-family classifier for static diagnostics.

    This is intentionally conservative. Unknown custom gateway models return None so doctor
    diagnostics do not over-warn on valid provider-specific names Alysis Code cannot know offline.
    """
    return _known_model_family(str(model or "").strip().lower())


def model_known_incompatible_with_family(model: str, family: str | None) -> bool:
    """Public wrapper used by diagnostics and tests."""
    return _model_known_incompatible_with_family(model, family)


def _known_model_family(model: str) -> str | None:
    known_prefixes: dict[str, tuple[str, ...]] = {
        "openai": ("gpt-", "chatgpt-", "o1", "o3", "o4", "o5"),
        "anthropic": ("claude-",),
        "gemini": ("gemini-",),
    }
    known_namespaces: dict[str, tuple[str, ...]] = {
        "openai": ("openai",),
        "anthropic": ("anthropic", "anthropic-ai"),
        "gemini": ("google", "gemini"),
    }
    parts = [part for part in model.split("/") if part]
    for known_family, namespaces in known_namespaces.items():
        if parts and parts[0] in namespaces:
            return known_family
    model_id = parts[-1] if parts else model
    for known_family, prefixes in known_prefixes.items():
        if model_id.startswith(prefixes):
            return known_family
    return None


def _has_known_provider_namespace(model: str) -> bool:
    if "/" not in model:
        return False
    namespace = model.split("/", 1)[0]
    return namespace in {"openai", "anthropic", "anthropic-ai", "google", "gemini"}


def _normalized_base_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def make_profile_from_preset(
    preset: ProfilePreset,
    *,
    name: str | None = None,
) -> ProfileSpec:
    profile_name = str(name or preset.key).strip().lower()
    return ProfileSpec(
        name=profile_name,
        protocol=preset.protocol,
        base_url=_preset_base_url(preset),
        api_key_env=preset.api_key_env,
        extra_headers=dict(preset.extra_headers),
        default_model=preset.suggested_models[0] if preset.suggested_models else "",
        web_search_adapter=preset.web_search_adapter,
        web_search_model=preset.web_search_model,
        notes=preset.notes,
    )
