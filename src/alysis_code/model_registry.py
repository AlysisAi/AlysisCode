from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .branding import env_get
from .chatgpt_codex_static_provider import (
    CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE,
    resolve_chatgpt_codex_static_model,
)
from .config import AppConfig
from .litellm_static_provider import (
    BUNDLED_MODEL_CATALOG_SOURCE,
    resolve_litellm_static_metadata,
)
from .model_metadata_utils import (
    model_name_variants,
    normalize_base_url,
    parse_bool,
    parse_non_negative_float,
    parse_positive_int,
)
from .provider_url import known_provider_key_from_base_url

_INT_FIELDS = ("context_window_tokens", "max_output_tokens")
_FLOAT_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_cost_per_token",
    "cache_creation_input_cost_per_token",
    "cache_creation_5m_input_cost_per_token",
    "cache_creation_1h_input_cost_per_token",
    "reasoning_output_cost_per_token",
)
_BOOL_FIELDS = ("supports_vision", "supports_reasoning")
_TRACKED_FIELDS = (
    "context_window_tokens",
    "max_output_tokens",
    "supports_vision",
    "supports_reasoning",
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_cost_per_token",
    "cache_creation_input_cost_per_token",
    "cache_creation_5m_input_cost_per_token",
    "cache_creation_1h_input_cost_per_token",
    "reasoning_output_cost_per_token",
)
DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS = 128_000
# Share the unknown-output allowance with request construction and context
# reservation. Resolved model capacities and explicit overrides take precedence.
DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS = 32_000

_FALLBACKS: dict[str, Any] = {
    "context_window_tokens": DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS,
    "max_output_tokens": DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
    "supports_vision": False,
    "supports_reasoning": None,
    "input_cost_per_token": None,
    "output_cost_per_token": None,
    "cache_read_input_cost_per_token": None,
    "cache_creation_input_cost_per_token": None,
    "cache_creation_5m_input_cost_per_token": None,
    "cache_creation_1h_input_cost_per_token": None,
    "reasoning_output_cost_per_token": None,
}
_ENV_FIELD_MAP: dict[str, str] = {
    "context_window_tokens": "ALYSIS_CONTEXT_WINDOW",
    "max_output_tokens": "ALYSIS_MAX_OUTPUT_TOKENS",
    "supports_vision": "ALYSIS_SUPPORTS_VISION",
    "supports_reasoning": "ALYSIS_SUPPORTS_REASONING",
    "input_cost_per_token": "ALYSIS_INPUT_COST_PER_TOKEN",
    "output_cost_per_token": "ALYSIS_OUTPUT_COST_PER_TOKEN",
    "cache_read_input_cost_per_token": "ALYSIS_CACHE_READ_INPUT_COST_PER_TOKEN",
    "cache_creation_input_cost_per_token": "ALYSIS_CACHE_CREATION_INPUT_COST_PER_TOKEN",
    "cache_creation_5m_input_cost_per_token": ("ALYSIS_CACHE_CREATION_5M_INPUT_COST_PER_TOKEN"),
    "cache_creation_1h_input_cost_per_token": ("ALYSIS_CACHE_CREATION_1H_INPUT_COST_PER_TOKEN"),
    "reasoning_output_cost_per_token": "ALYSIS_REASONING_OUTPUT_COST_PER_TOKEN",
}
_DEPRECATED_MODEL_CAPABILITIES_WARNING = (
    "Config key `model_capabilities` is deprecated and ignored; use `model_metadata_overrides`."
)
_FALLBACK_WARNING = (
    "Using fallback context/max_output; set model_metadata_overrides for best performance."
)
OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE = "official_provider_model_catalog"
CANONICAL_MODEL_CATALOG_SOURCE = "canonical_model_catalog"
_CANONICAL_MODEL_SOURCES: dict[str, tuple[str, ...]] = {
    "qwen3.8-max": ("https://help.aliyun.com/en/model-studio/qwen3-8-max",),
    "qwen3.7-plus": ("https://www.alibabacloud.com/help/en/model-studio/qwen3-7-plus",),
    "qwen3.8-flash": ("https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash",),
    "qwen3.7-flash": ("https://www.alibabacloud.com/help/en/model-studio/qwen3-7-flash",),
    "deepseek-v4-pro": ("https://api-docs.deepseek.com/quick_start/pricing/",),
    "deepseek-v4-flash": ("https://api-docs.deepseek.com/quick_start/pricing/",),
    "deepseek-v4-flash-vision-exp": ("https://api-docs.deepseek.com/quick_start/pricing/",),
    "deepseek-flash": (
        "https://api-docs.deepseek.com/updates/",
        "https://api-docs.deepseek.com/quick_start/pricing/",
    ),
    "glm-5.3": (
        "https://docs.z.ai/guides/llm/glm-5.3",
        "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
    ),
    "glm-5.3-flash": (
        "https://docs.z.ai/guides/vlm/glm-5.3-flash",
        "https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash",
    ),
    "kimi-k3": (
        "https://platform.kimi.ai/docs/models.md",
        "https://platform.kimi.ai/docs/pricing/chat-k3",
    ),
}
_CANONICAL_MODEL_METADATA: dict[str, dict[str, Any]] = {
    "qwen3.8-max": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    # The DashScope default model was absent from the pinned snapshot and fell
    # back to 128K/8K on every run.
    "qwen3.7-plus": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "qwen3.8-flash": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "qwen3.7-flash": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "deepseek-v4-pro": {
        "context_window_tokens": 1_000_000,
        # DeepSeek publishes "384K" (393,216) as the V4 output ceiling.
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "supports_reasoning": True,
    },
    "deepseek-v4-flash": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "supports_reasoning": True,
    },
    "deepseek-v4-flash-vision-exp": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    # DeepSeek-V4.1-Flash GA (2026-09-10), verified against official API docs.
    "deepseek-flash": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    # GLM-5.3 (2026-08-19) and GLM-5.3-Flash (2026-08-26): 1M context, 128K
    # output, thinking cannot be disabled. Flash is natively multimodal.
    "glm-5.3": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": False,
        "supports_reasoning": True,
    },
    "glm-5.3-flash": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    # Kimi K3: 1M shared window; 131,072 is the API's normal completion
    # allowance and therefore the useful local reserve on every host.
    "kimi-k3": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
    },
}
# Provider model ids are routing details, not separate capability records. This
# declarative alias table lets each route inherit the canonical model's limits
# while retaining provider-specific pricing, availability, and stricter caps.
_PROVIDER_CANONICAL_MODEL_IDS: dict[str, dict[str, str]] = {
    "qwen": {
        "qwen3.8-max": "qwen3.8-max",
        "qwen3.7-plus": "qwen3.7-plus",
        "qwen3.8-flash": "qwen3.8-flash",
        "qwen3.7-flash": "qwen3.7-flash",
    },
    "deepseek": {
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-flash": "deepseek-flash",
        "deepseek-v4-flash": "deepseek-flash",
        "deepseek-v4-flash-vision-exp": "deepseek-flash",
        "deepseek-v4.1-flash-expires-on-0910": "deepseek-flash",
    },
    "zhipu": {
        "glm-5.3": "glm-5.3",
        "glm-5.3-flash": "glm-5.3-flash",
    },
    "zai_coding_plan": {
        "glm-5.3": "glm-5.3",
        "glm-5.3-flash": "glm-5.3-flash",
    },
    "moonshot": {
        "kimi-k3": "kimi-k3",
    },
    "openrouter": {
        "qwen/qwen3.8-max": "qwen3.8-max",
        "qwen/qwen3.8-flash": "qwen3.8-flash",
        "deepseek/deepseek-v4-pro-0813": "deepseek-v4-pro",
        "deepseek/deepseek-v4-flash-0731": "deepseek-v4-flash",
        "deepseek/deepseek-v4-flash-vision-exp": "deepseek-v4-flash-vision-exp",
        "z-ai/glm-5.3": "glm-5.3",
        "z-ai/glm-5.3-flash": "glm-5.3-flash",
        "moonshotai/kimi-k3": "kimi-k3",
    },
    "together": {
        "deepseek-ai/DeepSeek-V4-Pro-0813": "deepseek-v4-pro",
        "deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek-v4-flash",
        "zai-org/GLM-5.3": "glm-5.3",
        "zai-org/GLM-5.3-Flash": "glm-5.3-flash",
        "moonshotai/Kimi-K3": "kimi-k3",
    },
    "fireworks": {
        "accounts/fireworks/models/deepseek-v4-pro-0813": "deepseek-v4-pro",
        "accounts/fireworks/models/deepseek-v4-flash-0731": "deepseek-v4-flash",
        "accounts/fireworks/models/deepseek-v4-flash-vision-exp": "deepseek-v4-flash-vision-exp",
        "accounts/fireworks/models/glm-5p3": "glm-5.3",
        "accounts/fireworks/models/glm-5p3-flash": "glm-5.3-flash",
        "accounts/fireworks/models/kimi-k3": "kimi-k3",
    },
    "nvidia": {
        "deepseek-ai/deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-ai/deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek-ai/deepseek-v4-pro-0813": "deepseek-v4-pro",
        "deepseek-ai/deepseek-v4-flash-0731": "deepseek-v4-flash",
        "moonshotai/kimi-k3": "kimi-k3",
    },
    "perplexity": {
        "perplexity/glm-5.3": "glm-5.3",
        "perplexity/kimi-k3": "kimi-k3",
        "perplexity/deepseek-v4-flash-0731": "deepseek-v4-flash",
    },
}
_OFFICIAL_PROVIDER_MODEL_SOURCES: dict[str, dict[str, tuple[str, ...]]] = {
    "openai": {
        model: (
            f"https://developers.openai.com/api/docs/models/{model}",
            "https://developers.openai.com/api/docs/pricing",
        )
        for model in (
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.3-codex",
        )
    },
    "zai_coding_plan": {
        "glm-5.3": ("https://docs.z.ai/guides/llm/glm-5.3", "https://docs.z.ai/devpack/overview"),
        "glm-5.3-flash": (
            "https://docs.z.ai/guides/vlm/glm-5.3-flash",
            "https://docs.z.ai/devpack/overview",
        ),
    },
    "zhipu": {
        model: (model_url, "https://open.bigmodel.cn/pricing")
        for model, model_url in {
            "glm-5.3": "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
            "glm-5.3-flash": "https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash",
            "glm-5.2": "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2",
            "glm-4.7-flashx": "https://docs.bigmodel.cn/cn/guide/models/text/glm-4.7",
            "glm-4.7-flash": "https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash",
        }.items()
    },
    "xai": {
        model: (
            f"https://docs.x.ai/developers/models/{model}",
            "https://docs.x.ai/developers/pricing",
        )
        for model in (
            "grok-4.7",
            "grok-4.6",
            "grok-4.5",
            "grok-build-0.1",
            "grok-4.3",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
        )
    },
    "gemini": {
        model: (model_url, "https://ai.google.dev/gemini-api/docs/pricing")
        for model, model_url in {
            "gemini-3.8-flash": ("https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash"),
            "gemini-3.7-flash": ("https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash"),
            "gemini-3.6-flash": ("https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash"),
            "gemini-3.5-flash-lite": (
                "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite"
            ),
        }.items()
    },
    "qwen": {
        "qwen3.8-max": (
            "https://help.aliyun.com/en/model-studio/qwen3-8-max",
            "https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions",
        ),
        "qwen3.8-flash": ("https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash",),
        "qwen3.7-flash": ("https://www.alibabacloud.com/help/en/model-studio/qwen3-7-flash",),
    },
    "deepseek": {
        model: (
            "https://api-docs.deepseek.com/quick_start/pricing/",
            "https://api-docs.deepseek.com/guides/thinking_mode/",
        )
        for model in (
            "deepseek-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "deepseek-v4.1-flash-expires-on-0910",
        )
    },
    "openrouter": {
        model: ("https://openrouter.ai/api/v1/models",)
        for model in (
            "anthropic/claude-sonnet-5",
            "openai/gpt-6-astra",
            "openai/gpt-6-sol",
            "openai/gpt-6-luna",
            "anthropic/claude-opus-5.5",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-luna",
            "anthropic/claude-opus-5",
            "anthropic/claude-fable-5.1",
            "google/gemini-3.8-flash",
            "x-ai/grok-4.7",
            "x-ai/grok-4.6",
            "z-ai/glm-5.3-flashx",
            "deepseek/deepseek-v4.1-flash",
            "qwen/qwen3.8-max-0902",
            "xiaomi/mimo-v2.6-pro",
            "xiaomi/mimo-v2.6-flash",
            "xiaomi/mimo-v2.6-pro-ultraspeed",
            "inception/mercury-2.5",
            "z-ai/glm-5.3",
            "z-ai/glm-5.3-flash",
            "moonshotai/kimi-k3",
            "qwen/qwen3.8-max",
            "qwen/qwen3.8-flash",
            "deepseek/deepseek-v4-pro-0813",
            "deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-v4-flash-vision-exp",
        )
    },
    "together": {
        model: ("https://docs.together.ai/docs/serverless-models",)
        for model in (
            "zai-org/GLM-5.3",
            "zai-org/GLM-5.3-Flash",
            "moonshotai/Kimi-K3",
            "deepseek-ai/DeepSeek-V4-Pro-0813",
            "deepseek-ai/DeepSeek-V4-Flash-0731",
            "Qwen/Qwen3.5-9B",
            "MiniMaxAI/MiniMax-M3",
        )
    },
    "fireworks": {
        model: (f"https://fireworks.ai/models/fireworks/{slug}",)
        for model, slug in {
            "accounts/fireworks/models/glm-5p3": "glm-5p3",
            "accounts/fireworks/models/glm-5p3-flash": "glm-5p3-flash",
            "accounts/fireworks/models/kimi-k3": "kimi-k3",
            "accounts/fireworks/models/deepseek-v4-pro-0813": "deepseek-v4-pro-0813",
            "accounts/fireworks/models/deepseek-v4-flash-0731": "deepseek-v4-flash-0731",
            "accounts/fireworks/models/deepseek-v4-flash-vision-exp": (
                "deepseek-v4-flash-vision-exp"
            ),
        }.items()
    },
    "nvidia": {
        "nvidia/nemotron-3-super-120b-a12b": (
            "https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/build",
        ),
        "nvidia/nemotron-3-ultra-550b-a55b": (
            "https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b",
        ),
        "nvidia/nemotron-3.5-lightning-30b-a3b": (
            "https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b",
        ),
        "nvidia/nemotron-3-nano-30b-a3b": (
            "https://build.nvidia.com/nvidia/nemotron-3-nano-30b-a3b",
        ),
        "moonshotai/kimi-k3": ("https://build.nvidia.com/moonshotai/kimi-k3",),
        "deepseek-ai/deepseek-v4-pro-0813": (
            "https://build.nvidia.com/deepseek-ai/deepseek-v4-pro-0813",
        ),
        "deepseek-ai/deepseek-v4-flash-0731": (
            "https://build.nvidia.com/deepseek-ai/deepseek-v4-flash-0731",
        ),
        "deepseek-ai/deepseek-v4-pro": ("https://build.nvidia.com/deepseek-ai/deepseek-v4-pro",),
        "deepseek-ai/deepseek-v4-flash": (
            "https://build.nvidia.com/deepseek-ai/deepseek-v4-flash",
        ),
    },
    "moonshot": {
        model: ("https://platform.kimi.ai/docs/pricing/chat-k3",)
        if model == "kimi-k3"
        else ("https://platform.kimi.ai/docs/pricing/chat-k27-code",)
        if model.startswith("kimi-k2.7")
        else ("https://platform.kimi.ai/docs/pricing/chat-k26",)
        for model in (
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        )
    },
    "minimax": {
        model: ("https://platform.minimax.io/docs/guides/pricing-paygo",)
        for model in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed")
    },
    "bytedance": {
        model: (
            "https://docs.volcengine.com/docs/82379/1330310",
            "https://docs.volcengine.com/docs/82379/1544106",
        )
        for model in (
            "doubao-seed-evolving",
            "doubao-seed-2-1-pro-260628",
            "doubao-seed-2-1-turbo-260628",
            "doubao-seed-2-0-mini-260428",
        )
    },
    "groq": {
        "qwen/qwen3.8-27b": ("https://console.groq.com/docs/model/qwen/qwen3.8-27b",),
        "groq/compound": ("https://console.groq.com/docs/models",),
        "groq/compound-mini": ("https://console.groq.com/docs/models",),
    },
    "cerebras": {
        "gemma-4-31b": ("https://inference-docs.cerebras.ai/models/gemma-4-31b",),
    },
    "mistral": {
        "zai-glm-5-3": (
            "https://docs.mistral.ai/models/zai-glm-5-3",
            "https://docs.mistral.ai/inference/pricing",
        ),
        "mistral-small-2603": (
            "https://docs.mistral.ai/models/mistral-small-4-0-26-03",
            "https://docs.mistral.ai/inference/pricing",
        ),
        "zai-glm-5-2": (
            "https://docs.mistral.ai/models/zai-glm-5-2",
            "https://docs.mistral.ai/inference/pricing",
        ),
    },
    "cohere": {
        "command-a-plus-05-2026": ("https://docs.cohere.com/docs/command-a-plus",),
        "command-a-reasoning-08-2025": ("https://docs.cohere.com/docs/command-a-reasoning",),
    },
    "perplexity": {
        model: ("https://docs.perplexity.ai/docs/agent-api/models",)
        for model in (
            "perplexity/sonar",
            "perplexity/glm-5.3",
            "perplexity/kimi-k3",
            "perplexity/kimi-k2.7-code",
            "perplexity/deepseek-v4-flash-0731",
        )
    },
    "anthropic": {
        "claude-opus-5-5": (
            "https://platform.claude.com/docs/en/models/opus-5-5/overview",
            "https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5",
        ),
        "claude-opus-5": (
            "https://platform.claude.com/docs/en/models/opus-5/overview",
            "https://platform.claude.com/docs/en/about-claude/pricing",
        ),
        "claude-fable-5-1": (
            "https://platform.claude.com/docs/en/models/fable-5-1/overview",
            "https://platform.claude.com/docs/en/about-claude/pricing",
        ),
    },
}
_OFFICIAL_PROVIDER_MODEL_METADATA: dict[str, dict[str, dict[str, Any]]] = {
    "openai": {
        # GPT-6.1 Sol: Standard rates at <=272K input. Longer prompts cost
        # 2x input/cache and 1.5x output; this flat schema records base rates.
        "gpt-6.1-sol": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.00001,
            "cache_read_input_cost_per_token": 0.0000001,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.00001,
        },
        # The provenance-pinned LiteLLM snapshot (2026-07-11) predates OpenAI's
        # 5.6 price cuts (Luna/Terra 2026-07-30, Sol 2026-08-21, promo through
        # at least 2026-11-21) and its cache-write charge (1.25x input on the
        # 5.6 family only). Windows: 5.6 = 1.05M total / 922K max input; 5.4
        # mini/nano and 5.3-codex = 400K total / 272K max input. Rates above
        # 272K input on 5.6 are 2x in / 1.5x out, which this flat schema
        # cannot express — base rates are recorded.
        # GPT-6 Astra (2026-09-03): same 1.05M / 922K-input / 128K shape,
        # $10 / $50, cached input $1, cache writes $12.50 (1.25x input).
        "gpt-6-astra": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00005,
            "cache_read_input_cost_per_token": 0.000001,
            "cache_creation_input_cost_per_token": 0.0000125,
            "reasoning_output_cost_per_token": 0.00005,
        },
        # September 22, 2026 releases. Standard rates at <=272K input;
        # longer prompts cost 2x input/cache and 1.5x output.
        "gpt-6-sol": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.00001,
            "cache_read_input_cost_per_token": 0.0000002,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.00001,
        },
        "gpt-6-luna": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000001,
            "output_cost_per_token": 0.0000005,
            "cache_read_input_cost_per_token": 0.00000001,
            "cache_creation_input_cost_per_token": 0.000000125,
            "reasoning_output_cost_per_token": 0.0000005,
        },
        "gpt-5.6-sol": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000004,
            "output_cost_per_token": 0.00002,
            "cache_read_input_cost_per_token": 0.0000004,
            "cache_creation_input_cost_per_token": 0.000005,
            "reasoning_output_cost_per_token": 0.00002,
        },
        "gpt-5.6-terra": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000012,
            "cache_read_input_cost_per_token": 0.0000002,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.000012,
        },
        "gpt-5.6-luna": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000002,
            "output_cost_per_token": 0.0000012,
            "cache_read_input_cost_per_token": 0.00000002,
            "cache_creation_input_cost_per_token": 0.00000025,
            "reasoning_output_cost_per_token": 0.0000012,
        },
        "gpt-5.5": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.00003,
            "cache_read_input_cost_per_token": 0.0000005,
            "reasoning_output_cost_per_token": 0.00003,
        },
        "gpt-5.4-mini": {
            "context_window_tokens": 400_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000075,
            "output_cost_per_token": 0.0000045,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.0000045,
        },
        "gpt-5.4-nano": {
            "context_window_tokens": 400_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000002,
            "output_cost_per_token": 0.00000125,
            "cache_read_input_cost_per_token": 0.00000002,
            "reasoning_output_cost_per_token": 0.00000125,
        },
        "gpt-5.3-codex": {
            "context_window_tokens": 400_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000175,
            "output_cost_per_token": 0.000014,
            "cache_read_input_cost_per_token": 0.000000175,
            "reasoning_output_cost_per_token": 0.000014,
        },
    },
    "zai_coding_plan": {
        # Plan credits are not token prices, so this route intentionally
        # carries no monetary cost fields. (GLM-5.3 is also on the Z.AI
        # pay-per-token price list since 2026-08-18 — $1.40/$4.40/$0.26 — but
        # that is a different endpoint.)
        "glm-5.3": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "glm-5.3-flash": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
        },
    },
    "zhipu": {
        # open.bigmodel.cn bills in CNY; this schema is USD-per-token, so only
        # capacity is recorded for the China surface.
        "glm-5.3": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "glm-5.3-flash": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
        },
        "glm-5.2": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "glm-4.7-flashx": {
            "context_window_tokens": 200_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "glm-4.7-flash": {
            "context_window_tokens": 200_000,
            "max_output_tokens": 131_072,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "cache_read_input_cost_per_token": 0.0,
        },
    },
    "xai": {
        # Grok 4.7/4.6 postdate the provenance-pinned LiteLLM snapshot. xAI
        # publishes a 500K shared context with no separate text-output limit,
        # so use the full window here and let the registry's generic
        # shared-window policy reserve a practical output allowance. These are
        # the base rates for prompts up to 200K tokens; xAI doubles all three
        # rates above that threshold, which this flat-price schema cannot
        # represent yet.
        # 4.7 verified against xAI's model page and release notes, 2026-09-21.
        "grok-4.7": {
            "context_window_tokens": 500_000,
            "max_output_tokens": 500_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.0000005,
            "reasoning_output_cost_per_token": 0.000006,
        },
        "grok-4.6": {
            "context_window_tokens": 500_000,
            "max_output_tokens": 500_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.0000005,
            "reasoning_output_cost_per_token": 0.000006,
        },
        # Same shape as 4.6; cache reads are cheaper ($0.30 vs $0.50).
        "grok-4.5": {
            "context_window_tokens": 500_000,
            "max_output_tokens": 500_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.0000003,
            "reasoning_output_cost_per_token": 0.000006,
        },
        # docs.x.ai/developers/models (2026-08-21): build-0.1 is a 256K shared
        # window; 4.3 and the 4.20 snapshots are 1M shared windows (the pinned
        # snapshot overstated 4.3 at 2M and lacked the others entirely). Base
        # rates below 200K prompt tokens; xAI doubles them above that.
        "grok-build-0.1": {
            "context_window_tokens": 256_000,
            "max_output_tokens": 256_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000002,
            "cache_read_input_cost_per_token": 0.0000002,
            "reasoning_output_cost_per_token": 0.000002,
        },
        "grok-4.3": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 1_000_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000125,
            "output_cost_per_token": 0.0000025,
            "cache_read_input_cost_per_token": 0.0000002,
            "reasoning_output_cost_per_token": 0.0000025,
        },
        "grok-4.20-0309-reasoning": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 1_000_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000125,
            "output_cost_per_token": 0.0000025,
            "cache_read_input_cost_per_token": 0.0000002,
            "reasoning_output_cost_per_token": 0.0000025,
        },
        "grok-4.20-0309-non-reasoning": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 1_000_000,
            "supports_vision": True,
            "supports_reasoning": False,
            "input_cost_per_token": 0.00000125,
            "output_cost_per_token": 0.0000025,
            "cache_read_input_cost_per_token": 0.0000002,
        },
    },
    "gemini": {
        # These stable models postdate the provenance-pinned LiteLLM snapshot.
        # Capacity and current standard-tier pricing are from Google's model
        # and pricing pages. The 3.8/3.7/3.6 introductory rates expire
        # 2026-12-31 and become $1.50 / $7.50 / $0.15 from 2027-01-01.
        "gemini-3.8-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000075,
            "output_cost_per_token": 0.00000375,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.00000375,
        },
        "gemini-3.7-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000075,
            "output_cost_per_token": 0.00000375,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.00000375,
        },
        "gemini-3.6-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000075,
            "output_cost_per_token": 0.00000375,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.00000375,
        },
        "gemini-3.5-flash-lite": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000003,
            "output_cost_per_token": 0.0000025,
            "cache_read_input_cost_per_token": 0.00000003,
            "reasoning_output_cost_per_token": 0.0000025,
        },
    },
    "qwen": {
        # The current Qwen ids are served from the China, Singapore, Frankfurt,
        # US, Hong Kong and Tokyo surfaces under the same bare model id. Prices
        # vary by region/currency, which this provider-level flat schema cannot
        # encode, so costs remain unknown instead of applying one region
        # globally. Capacity comes from the canonical layer.
        "qwen3.8-max": {},
        "qwen3.8-flash": {},
        "qwen3.7-flash": {},
    },
    "deepseek": {
        # DeepSeek charges half price outside two daily peak windows (peak =
        # 01:00-04:00 and 06:00-10:00 UTC Mon-Fri since 2026-08-16). The
        # registry cannot express time-dependent rates, so use peak pricing as
        # a conservative upper-bound estimate rather than under-reporting cost.
        "deepseek-v4-pro": {
            "input_cost_per_token": 0.00000132,
            "output_cost_per_token": 0.00000396,
            "cache_read_input_cost_per_token": 0.000000044,
            "reasoning_output_cost_per_token": 0.00000396,
        },
        # Stable Flash and migrated legacy routes share V4.1 peak prices.
        **{
            model: {
                "input_cost_per_token": 0.0000003,
                "output_cost_per_token": 0.0000012,
                "cache_read_input_cost_per_token": 0.000000006,
                "reasoning_output_cost_per_token": 0.0000012,
            }
            for model in (
                "deepseek-flash",
                "deepseek-v4-flash",
                "deepseek-v4-flash-vision-exp",
                "deepseek-v4.1-flash-expires-on-0910",
            )
        },
    },
    "openrouter": {
        # Prices are OpenRouter's listed gateway rates (openrouter.ai/api/v1/
        # models, 2026-09-02); windows are the gateway routing contract, which
        # may exceed an individual upstream route's cap. The preset's default
        # and the two OpenAI routes were absent from the pinned snapshot and
        # fell back to 128K/8K.
        "anthropic/claude-sonnet-5": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.00001,
            "cache_read_input_cost_per_token": 0.0000002,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.00001,
        },
        "openai/gpt-6-astra": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00005,
            "cache_read_input_cost_per_token": 0.000001,
            "cache_creation_input_cost_per_token": 0.0000125,
            "reasoning_output_cost_per_token": 0.00005,
        },
        # Verified against OpenRouter's public catalog on 2026-09-22.
        "openai/gpt-6-sol": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.00001,
            "cache_read_input_cost_per_token": 0.0000002,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.00001,
        },
        "openai/gpt-6-luna": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000001,
            "output_cost_per_token": 0.0000005,
            "cache_read_input_cost_per_token": 0.00000001,
            "cache_creation_input_cost_per_token": 0.000000125,
            "reasoning_output_cost_per_token": 0.0000005,
        },
        "anthropic/claude-opus-5.5": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000004,
            "output_cost_per_token": 0.00002,
            "cache_read_input_cost_per_token": 0.0000002,
            "cache_creation_input_cost_per_token": 0.000005,
            "cache_creation_5m_input_cost_per_token": 0.000005,
            "cache_creation_1h_input_cost_per_token": 0.000008,
            "reasoning_output_cost_per_token": 0.00002,
        },
        "openai/gpt-5.6-terra": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000012,
            "cache_read_input_cost_per_token": 0.0000002,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.000012,
        },
        "openai/gpt-5.6-luna": {
            "context_window_tokens": 1_050_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000002,
            "output_cost_per_token": 0.0000012,
            "cache_read_input_cost_per_token": 0.00000002,
            "cache_creation_input_cost_per_token": 0.00000025,
            "reasoning_output_cost_per_token": 0.0000012,
        },
        "anthropic/claude-opus-5": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
            "cache_read_input_cost_per_token": 0.0000005,
            "cache_creation_input_cost_per_token": 0.00000625,
            "reasoning_output_cost_per_token": 0.000025,
        },
        "anthropic/claude-fable-5.1": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00005,
            "cache_read_input_cost_per_token": 0.00000025,
            "cache_creation_input_cost_per_token": 0.0000125,
            "reasoning_output_cost_per_token": 0.00005,
        },
        "google/gemini-3.8-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 65_536,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000075,
            "output_cost_per_token": 0.00000375,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.00000375,
        },
        # September additions verified against OpenRouter's /api/v1/models
        # on 2026-09-22. Limits and prices belong to these gateway routes.
        # Grok uses base rates through 200K input; longer prompts cost 2x.
        "x-ai/grok-4.7": {
            "context_window_tokens": 500_000,
            "max_output_tokens": 450_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000016,
            "output_cost_per_token": 0.0000048,
            "cache_read_input_cost_per_token": 0.0000004,
            "reasoning_output_cost_per_token": 0.0000048,
        },
        "z-ai/glm-5.3-flashx": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000037,
            "output_cost_per_token": 0.00000125,
            "cache_read_input_cost_per_token": 0.000000075,
            "reasoning_output_cost_per_token": 0.00000125,
        },
        "deepseek/deepseek-v4.1-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 384_000,
            "supports_vision": True,
            "supports_reasoning": True,
            # Conservative peak rates; off-peak requests cost half as much.
            "input_cost_per_token": 0.0000003,
            "output_cost_per_token": 0.0000012,
            "cache_read_input_cost_per_token": 0.000000006,
            "reasoning_output_cost_per_token": 0.0000012,
        },
        "qwen/qwen3.8-max-0902": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.00000025,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.000006,
        },
        "xiaomi/mimo-v2.6-pro": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000000435,
            "output_cost_per_token": 0.00000087,
            "cache_read_input_cost_per_token": 0.0000000036,
            "reasoning_output_cost_per_token": 0.00000087,
        },
        "xiaomi/mimo-v2.6-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000014,
            "output_cost_per_token": 0.00000028,
            "cache_read_input_cost_per_token": 0.0000000028,
            "reasoning_output_cost_per_token": 0.00000028,
        },
        "xiaomi/mimo-v2.6-pro-ultraspeed": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000435,
            "output_cost_per_token": 0.0000087,
            "cache_read_input_cost_per_token": 0.000000036,
            "reasoning_output_cost_per_token": 0.0000087,
        },
        "inception/mercury-2.5": {
            "context_window_tokens": 260_000,
            "max_output_tokens": 65_536,
            "supports_vision": False,
            "supports_reasoning": True,
            # OpenRouter's current promotional rates, reviewed 2026-09-22.
            "input_cost_per_token": 0.00000004,
            "output_cost_per_token": 0.00000015,
            "cache_read_input_cost_per_token": 0.000000004,
            "reasoning_output_cost_per_token": 0.00000015,
        },
        "x-ai/grok-4.6": {
            "context_window_tokens": 500_000,
            "max_output_tokens": 450_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.0000005,
            "reasoning_output_cost_per_token": 0.000006,
        },
        "z-ai/glm-5.3": {
            "context_window_tokens": 1_310_720,
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
            "cache_read_input_cost_per_token": 0.00000026,
            "reasoning_output_cost_per_token": 0.0000044,
        },
        "z-ai/glm-5.3-flash": {
            "context_window_tokens": 1_310_720,
            "input_cost_per_token": 0.000000075,
            "output_cost_per_token": 0.00000025,
            "cache_read_input_cost_per_token": 0.000000015,
            "reasoning_output_cost_per_token": 0.00000025,
        },
        "moonshotai/kimi-k3": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_cost_per_token": 0.0000003,
            "reasoning_output_cost_per_token": 0.000015,
        },
        "qwen/qwen3.8-max": {
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000006,
            "cache_read_input_cost_per_token": 0.00000025,
            "cache_creation_input_cost_per_token": 0.0000025,
            "reasoning_output_cost_per_token": 0.000006,
        },
        "qwen/qwen3.8-flash": {
            "input_cost_per_token": 0.00000015,
            "output_cost_per_token": 0.00000047,
            "cache_read_input_cost_per_token": 0.000000016,
            "cache_creation_input_cost_per_token": 0.0000002,
            "reasoning_output_cost_per_token": 0.00000047,
        },
        "deepseek/deepseek-v4-pro-0813": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000066,
            "output_cost_per_token": 0.00000198,
            "cache_read_input_cost_per_token": 0.000000022,
            "reasoning_output_cost_per_token": 0.00000198,
        },
        "deepseek/deepseek-v4-flash-0731": {
            # OpenRouter's public model catalog advertises a 1.31M routing
            # window for this snapshot even though its current top provider is
            # capped at 1,048,576 tokens. Preserve the gateway-level contract;
            # route-specific limits remain OpenRouter's responsibility.
            "context_window_tokens": 1_310_720,
            "input_cost_per_token": 0.000000065,
            "output_cost_per_token": 0.00000018,
            "cache_read_input_cost_per_token": 0.000000016,
            "reasoning_output_cost_per_token": 0.00000018,
        },
        "deepseek/deepseek-v4-flash-vision-exp": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000022,
            "output_cost_per_token": 0.00000066,
            "cache_read_input_cost_per_token": 0.000000007,
            "reasoning_output_cost_per_token": 0.00000066,
        },
    },
    "together": {
        # docs.together.ai/docs/serverless-models, 2026-09-02.
        "zai-org/GLM-5.3": {
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
            "cache_read_input_cost_per_token": 0.00000026,
            "reasoning_output_cost_per_token": 0.0000044,
        },
        "zai-org/GLM-5.3-Flash": {
            # Together lists the text route only; image input is not advertised.
            "supports_vision": False,
            "input_cost_per_token": 0.00000015,
            "output_cost_per_token": 0.0000005,
            "cache_read_input_cost_per_token": 0.00000003,
            "reasoning_output_cost_per_token": 0.0000005,
        },
        "moonshotai/Kimi-K3": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_cost_per_token": 0.0000003,
            "reasoning_output_cost_per_token": 0.000015,
        },
        "deepseek-ai/DeepSeek-V4-Pro-0813": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000132,
            "output_cost_per_token": 0.00000396,
            "cache_read_input_cost_per_token": 0.00000013,
            "reasoning_output_cost_per_token": 0.00000396,
        },
        "deepseek-ai/DeepSeek-V4-Flash-0731": {
            "input_cost_per_token": 0.00000014,
            "output_cost_per_token": 0.00000028,
            "cache_read_input_cost_per_token": 0.00000003,
            "reasoning_output_cost_per_token": 0.00000028,
        },
        "Qwen/Qwen3.5-9B": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000017,
            "output_cost_per_token": 0.00000025,
            "reasoning_output_cost_per_token": 0.00000025,
        },
        "MiniMaxAI/MiniMax-M3": {
            "context_window_tokens": 524_288,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000003,
            "output_cost_per_token": 0.0000012,
            "cache_read_input_cost_per_token": 0.00000006,
            "reasoning_output_cost_per_token": 0.0000012,
        },
    },
    "fireworks": {
        # docs.fireworks.ai/serverless/pricing, 2026-09-02. Fireworks lists
        # 1,040,000 for its 1M-class routes; canonical capacity is inherited
        # and only the stricter gateway figures are overridden here.
        "accounts/fireworks/models/glm-5p3": {
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
            "cache_read_input_cost_per_token": 0.00000026,
            "reasoning_output_cost_per_token": 0.0000044,
        },
        "accounts/fireworks/models/glm-5p3-flash": {
            "input_cost_per_token": 0.00000015,
            "output_cost_per_token": 0.0000005,
            "cache_read_input_cost_per_token": 0.00000003,
            "reasoning_output_cost_per_token": 0.0000005,
        },
        "accounts/fireworks/models/kimi-k3": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_cost_per_token": 0.0000003,
            "reasoning_output_cost_per_token": 0.000015,
        },
        "accounts/fireworks/models/deepseek-v4-pro-0813": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000132,
            "output_cost_per_token": 0.00000396,
            "cache_read_input_cost_per_token": 0.000000044,
            "reasoning_output_cost_per_token": 0.00000396,
        },
        "accounts/fireworks/models/deepseek-v4-flash-0731": {
            "context_window_tokens": 1_048_576,
            "input_cost_per_token": 0.00000022,
            "output_cost_per_token": 0.00000066,
            "cache_read_input_cost_per_token": 0.000000007,
            "reasoning_output_cost_per_token": 0.00000066,
        },
        "accounts/fireworks/models/deepseek-v4-flash-vision-exp": {
            "input_cost_per_token": 0.00000022,
            "output_cost_per_token": 0.00000066,
            "cache_read_input_cost_per_token": 0.000000007,
            "reasoning_output_cost_per_token": 0.00000066,
        },
    },
    "nvidia": {
        # NVIDIA's hosted NIM catalog publishes the Nemotron 3 Super, Ultra and
        # 3.5 Lightning models with 1M context; Nano's hosted endpoint
        # advertises 262K. The hosted Nemotron endpoints accept up to 32,768
        # output tokens; 16,384 is their default, not their ceiling. Hosted
        # third-party routes (DeepSeek V4, Kimi K3) use the samples' 16,384
        # ceiling. Free Endpoint access is rate-limited prototyping, so no
        # durable token price is encoded here. The undated DeepSeek ids and
        # Nano are Deprecated free endpoints kept for saved configs.
        "nvidia/nemotron-3-super-120b-a12b": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 32_768,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "nvidia/nemotron-3-ultra-550b-a55b": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 32_768,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "nvidia/nemotron-3.5-lightning-30b-a3b": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "nvidia/nemotron-3-nano-30b-a3b": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": False,
            "supports_reasoning": True,
        },
        "moonshotai/kimi-k3": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
        },
        "deepseek-ai/deepseek-v4-pro-0813": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
        },
        "deepseek-ai/deepseek-v4-flash-0731": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
        },
        "deepseek-ai/deepseek-v4-pro": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
        },
        "deepseek-ai/deepseek-v4-flash": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 16_384,
        },
    },
    "minimax": {
        # platform.minimax.io/docs/guides/pricing-paygo (2026-09-02). M3 is
        # listed at "permanent 50% off" ($0.30/$1.20); input above 512K bills
        # 2x, which this flat schema cannot express — base rates recorded.
        "MiniMax-M3": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 524_288,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000003,
            "output_cost_per_token": 0.0000012,
            "cache_read_input_cost_per_token": 0.00000006,
            "reasoning_output_cost_per_token": 0.0000012,
        },
        "MiniMax-M2.7": {
            "context_window_tokens": 204_800,
            "max_output_tokens": 204_800,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000003,
            "output_cost_per_token": 0.0000012,
            "cache_read_input_cost_per_token": 0.00000006,
            "cache_creation_input_cost_per_token": 0.000000375,
            "reasoning_output_cost_per_token": 0.0000012,
        },
        "MiniMax-M2.7-highspeed": {
            "context_window_tokens": 204_800,
            "max_output_tokens": 204_800,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000006,
            "output_cost_per_token": 0.0000024,
            "cache_read_input_cost_per_token": 0.00000006,
            "cache_creation_input_cost_per_token": 0.000000375,
            "reasoning_output_cost_per_token": 0.0000024,
        },
    },
    "bytedance": {
        # Ark (ark.cn-beijing) bills in CNY, so only capacity is recorded. The
        # 2.0 family has a 256K window but a 224K max input; 2.1 and the
        # rolling seed-evolving id allow 256K output.
        "doubao-seed-evolving": {
            "context_window_tokens": 1_048_576,
            "max_output_tokens": 262_144,
            "supports_vision": True,
            "supports_reasoning": True,
        },
        "doubao-seed-2-1-pro-260628": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 262_144,
            "supports_vision": True,
            "supports_reasoning": True,
        },
        "doubao-seed-2-1-turbo-260628": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 262_144,
            "supports_vision": True,
            "supports_reasoning": True,
        },
        "doubao-seed-2-0-mini-260428": {
            "context_window_tokens": 229_376,
            "max_output_tokens": 131_072,
            "supports_vision": True,
            "supports_reasoning": True,
        },
    },
    "groq": {
        # console.groq.com/docs/model/qwen/qwen3.8-27b prints "131,042" for the
        # window (almost certainly 131,072); the lower literal is kept.
        "qwen/qwen3.8-27b": {
            "context_window_tokens": 131_042,
            "max_output_tokens": 16_384,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000008,
            "output_cost_per_token": 0.000004,
            "reasoning_output_cost_per_token": 0.000004,
        },
        # Compound systems run server-side tools; Groq lists no token price
        # ("-") and an 8,192-token completion cap. Absent from the snapshot.
        "groq/compound": {
            "context_window_tokens": 131_072,
            "max_output_tokens": 8_192,
            "supports_vision": False,
            "supports_reasoning": False,
        },
        "groq/compound-mini": {
            "context_window_tokens": 131_072,
            "max_output_tokens": 8_192,
            "supports_vision": False,
            "supports_reasoning": False,
        },
    },
    "cerebras": {
        # inference-docs.cerebras.ai/models/gemma-4-31b: 65K context / 32K
        # output on the free tier (131K / 40K paid); the page's own price
        # figures contradict each other, so cost stays unknown.
        "gemma-4-31b": {
            "context_window_tokens": 65_536,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
        },
    },
    "mistral": {
        # docs.mistral.ai/models/mistral-small-4-0-26-03: 256K context; the
        # output ceiling is unpublished, so a conservative reserve is used.
        "mistral-small-2603": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000015,
            "output_cost_per_token": 0.0000006,
            "reasoning_output_cost_per_token": 0.0000006,
        },
        # GLM-5.3 joined Mistral's Public Preview on 2026-09-15. Its page
        # documents tools and text input, but no reasoning control.
        "zai-glm-5-3": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "supports_vision": False,
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
            "cache_read_input_cost_per_token": 0.00000014,
        },
        # Third-party GLM-5.2 route hosted by Mistral (Public Preview,
        # 2026-08-06). Reasoning support is not documented on the page.
        "zai-glm-5-2": {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 128_000,
            "supports_vision": False,
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
            "cache_read_input_cost_per_token": 0.00000014,
        },
    },
    "cohere": {
        # docs.cohere.com/docs/models (2026-09-02). Per-token prices for the
        # live Command models render client-side and could not be verified,
        # so only capacity is recorded. The preset default was falling back
        # to 128K/8K.
        "command-a-plus-05-2026": {
            "context_window_tokens": 128_000,
            "max_output_tokens": 64_000,
            "supports_vision": True,
            "supports_reasoning": True,
        },
        "command-a-reasoning-08-2025": {
            "context_window_tokens": 256_000,
            "max_output_tokens": 32_000,
            "supports_vision": False,
            "supports_reasoning": True,
        },
    },
    "perplexity": {
        # Agent API (docs.perplexity.ai/docs/agent-api/models, 2026-09-02).
        # Perplexity publishes prices but no windows for its hosted routes;
        # GLM/Kimi/DeepSeek capacity comes from the canonical layer. Sonar's
        # window is undocumented, so a conservative 128K is recorded.
        "perplexity/sonar": {
            "context_window_tokens": 128_000,
            "max_output_tokens": 8_192,
            "supports_vision": False,
            "supports_reasoning": False,
            "input_cost_per_token": 0.00000025,
            "output_cost_per_token": 0.0000025,
        },
        "perplexity/glm-5.3": {
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
            "reasoning_output_cost_per_token": 0.0000044,
        },
        "perplexity/kimi-k3": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "reasoning_output_cost_per_token": 0.000015,
        },
        "perplexity/kimi-k2.7-code": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000095,
            "output_cost_per_token": 0.000004,
            "reasoning_output_cost_per_token": 0.000004,
        },
        "perplexity/deepseek-v4-flash-0731": {
            "input_cost_per_token": 0.00000013,
            "output_cost_per_token": 0.00000026,
            "reasoning_output_cost_per_token": 0.00000026,
        },
    },
    "moonshot": {
        # platform.kimi.ai pricing pages (USD, global platform). Capacity for
        # kimi-k3 is inherited from the canonical layer.
        "kimi-k3": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_cost_per_token": 0.0000003,
        },
        "kimi-k2.7-code": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000095,
            "output_cost_per_token": 0.000004,
            "cache_read_input_cost_per_token": 0.00000019,
        },
        "kimi-k2.7-code-highspeed": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.0000019,
            "output_cost_per_token": 0.000008,
            "cache_read_input_cost_per_token": 0.00000038,
        },
        "kimi-k2.6": {
            "context_window_tokens": 262_144,
            "max_output_tokens": 32_768,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00000095,
            "output_cost_per_token": 0.000004,
            "cache_read_input_cost_per_token": 0.00000016,
        },
    },
    "anthropic": {
        # Opus 5 and Fable 5.1 ship ahead of the vendored litellm mirror, and
        # that mirror is refresh-policy pinned to an upstream commit — so their
        # capacity lives here rather than as a hand-edit that would falsify the
        # snapshot's provenance. Drop these once a catalog refresh carries them.
        # Anthropic documents "1M context + 128K output"; the window below is
        # the input+output sum so the local reserve does not eat the 1M input.
        # Opus 5.5 (2026-09-22): $4/$20 per MTok, cache reads $0.20.
        "claude-opus-5-5": {
            "context_window_tokens": 1_128_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000004,
            "output_cost_per_token": 0.00002,
            "cache_read_input_cost_per_token": 0.0000002,
            "cache_creation_input_cost_per_token": 0.000005,
            "cache_creation_5m_input_cost_per_token": 0.000005,
            "cache_creation_1h_input_cost_per_token": 0.000008,
        },
        "claude-opus-5": {
            "context_window_tokens": 1_128_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.000005,
            "output_cost_per_token": 0.000025,
            "cache_read_input_cost_per_token": 0.0000005,
            "cache_creation_input_cost_per_token": 0.00000625,
            "cache_creation_5m_input_cost_per_token": 0.00000625,
            "cache_creation_1h_input_cost_per_token": 0.00001,
        },
        # Fable 5.1 (2026-09-01): Fable 5 in/out pricing with cache reads at
        # 0.025x ($0.25/MTok) instead of 0.1x.
        "claude-fable-5-1": {
            "context_window_tokens": 1_128_000,
            "max_output_tokens": 128_000,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00005,
            "cache_read_input_cost_per_token": 0.00000025,
            "cache_creation_input_cost_per_token": 0.0000125,
            "cache_creation_5m_input_cost_per_token": 0.0000125,
            "cache_creation_1h_input_cost_per_token": 0.00002,
        },
    },
}
# Hosted Flash has the same capabilities and token prices as the direct API.
# Keep legacy IDs resolvable for sessions restored before config migration.
_HOSTED_FLASH_IDS = (
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4.1-flash-expires-on-0910",
)
_PROVIDER_CANONICAL_MODEL_IDS["alysis"] = {model: "deepseek-flash" for model in _HOSTED_FLASH_IDS}
_OFFICIAL_PROVIDER_MODEL_SOURCES["alysis"] = {
    model: _OFFICIAL_PROVIDER_MODEL_SOURCES["deepseek"]["deepseek-flash"]
    for model in _HOSTED_FLASH_IDS
}
_OFFICIAL_PROVIDER_MODEL_METADATA["alysis"] = {
    model: dict(_OFFICIAL_PROVIDER_MODEL_METADATA["deepseek"]["deepseek-flash"])
    for model in _HOSTED_FLASH_IDS
}
_PROVIDER_CANONICAL_MODEL_IDS["alysis"]["glm-5.3-flash"] = "glm-5.3-flash"
_OFFICIAL_PROVIDER_MODEL_SOURCES["alysis"]["glm-5.3-flash"] = (
    "https://docs.z.ai/guides/vlm/glm-5.3-flash",
    "https://docs.z.ai/guides/overview/pricing",
)
_OFFICIAL_PROVIDER_MODEL_METADATA["alysis"]["glm-5.3-flash"] = {
    "context_window_tokens": 1_000_000,
    "max_output_tokens": 131_072,
    # Hosted vision needs a documented conservative image-token bound before
    # it can use credit reservations; text and function tools are enabled now.
    "supports_vision": False,
    "supports_reasoning": True,
    "input_cost_per_token": 0.00000015,
    "cache_read_input_cost_per_token": 0.00000003,
    "output_cost_per_token": 0.0000005,
}

_PROVIDER_CANONICAL_MODEL_IDS["alysis"]["gpt-6-luna"] = "gpt-6-luna"
_OFFICIAL_PROVIDER_MODEL_SOURCES["alysis"]["gpt-6-luna"] = (
    "https://developers.openai.com/api/docs/models/gpt-6-luna",
    "https://developers.openai.com/api/docs/pricing",
)
_OFFICIAL_PROVIDER_MODEL_METADATA["alysis"]["gpt-6-luna"] = {
    "context_window_tokens": 1_050_000,
    "max_output_tokens": 128_000,
    "supports_vision": False,
    "supports_reasoning": True,
    "input_cost_per_token": 0.0000001,
    "cache_read_input_cost_per_token": 0.00000001,
    "cache_creation_input_cost_per_token": 0.000000125,
    "output_cost_per_token": 0.0000005,
}

# Paid hosted Sol shares API pricing, with the hosted modality restriction.
_PROVIDER_CANONICAL_MODEL_IDS["alysis"]["gpt-6.1-sol"] = "gpt-6.1-sol"
_OFFICIAL_PROVIDER_MODEL_SOURCES["alysis"]["gpt-6.1-sol"] = _OFFICIAL_PROVIDER_MODEL_SOURCES[
    "openai"
]["gpt-6.1-sol"]
_OFFICIAL_PROVIDER_MODEL_METADATA["alysis"]["gpt-6.1-sol"] = {
    **_OFFICIAL_PROVIDER_MODEL_METADATA["openai"]["gpt-6.1-sol"],
    # Hosted Responses meters text and function tools, like the Luna route.
    "supports_vision": False,
}

# Anthropic's launch specifications and standard five-minute cache pricing.
_PROVIDER_CANONICAL_MODEL_IDS["alysis"]["claude-sonnet-5-5"] = "claude-sonnet-5-5"
_OFFICIAL_PROVIDER_MODEL_SOURCES["alysis"]["claude-sonnet-5-5"] = (
    "https://platform.claude.com/docs/en/models/sonnet-5-5/overview",
)
_OFFICIAL_PROVIDER_MODEL_METADATA["alysis"]["claude-sonnet-5-5"] = {
    "context_window_tokens": 1_000_000,
    "max_output_tokens": 128_000,
    # The hosted route initially meters text/tools; image accounting is separate.
    "supports_vision": False,
    "supports_reasoning": True,
    "input_cost_per_token": 0.000002,
    "cache_read_input_cost_per_token": 0.0000002,
    "cache_creation_input_cost_per_token": 0.0000025,
    "output_cost_per_token": 0.00001,
}

# Verified launch specifications, 2026-10-07. Flat metadata shows the <=100K
# prompt tier; hosted settlement applies 5x to ALL tokens above that threshold.
for _haiku_provider in ("anthropic", "alysis"):
    _PROVIDER_CANONICAL_MODEL_IDS.setdefault(_haiku_provider, {})["claude-haiku-5-5"] = (
        "claude-haiku-5-5"
    )
    _OFFICIAL_PROVIDER_MODEL_SOURCES.setdefault(_haiku_provider, {})["claude-haiku-5-5"] = (
        "https://platform.claude.com/docs/en/models/haiku-5-5/overview",
    )
    _OFFICIAL_PROVIDER_MODEL_METADATA.setdefault(_haiku_provider, {})["claude-haiku-5-5"] = {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "supports_vision": _haiku_provider == "anthropic",
        "supports_reasoning": True,
        "input_cost_per_token": 0.0000001,
        "cache_read_input_cost_per_token": 0.00000001,
        "cache_creation_input_cost_per_token": 0.000000125,
        "output_cost_per_token": 0.0000005,
    }

_BUILT_IN_MODEL_METADATA: dict[str, dict[str, Any]] = {
    "deepseek-chat": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
    },
    "deepseek-reasoner": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "supports_vision": False,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
    },
    # Xiaomi MiMo V2.6 launch (2026-09-22), overseas USD pay-as-you-go rates.
    # https://mimo.mi.com/docs/en-US/quick-start/summary/model
    # https://mimo.mi.com/docs/en-US/api/chat/openai-api
    # https://mimo.mi.com/docs/en-US/price/pay-as-you-go
    "mimo-v2.6-pro": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
        "cache_read_input_cost_per_token": 0.0000000036,
    },
    "mimo-v2.6-flash": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.00000014,
        "output_cost_per_token": 0.00000028,
        "cache_read_input_cost_per_token": 0.0000000028,
    },
    "mimo-v2.6-pro-ultraspeed": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.00000435,
        "output_cost_per_token": 0.0000087,
        "cache_read_input_cost_per_token": 0.000000036,
    },
    # Xiaomi MiMo pay-as-you-go ids (mimo.mi.com/docs/en-US/price/pay-as-you-go,
    # 2026-09-02). mimo-v2-flash shut down 2026-06-30 and is aliased to
    # mimo-v2.5 at the preset layer. Cache reads are $0.0036 / $0.0028 per MTok.
    "mimo-v2.5-pro": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": False,
        "supports_reasoning": True,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
        "cache_read_input_cost_per_token": 0.0000000036,
    },
    "mimo-v2.5": {
        "context_window_tokens": 1_000_000,
        "max_output_tokens": 131_072,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.00000014,
        "output_cost_per_token": 0.00000028,
        "cache_read_input_cost_per_token": 0.0000000028,
    },
    # Legacy friendly id kept for sessions that logged in before model choice.
    # Hosted Xiaomi MiMo trial: the CLI sends the friendly id "mimo" (the proxy
    # pins it to the real upstream id server-side). Without an entry here the id
    # resolves nowhere and falls back to generic unknown-model metadata, emitting
    # a metadata note on every run and silently shrinking the usable context.
    # Values mirror the bundled `openrouter/xiaomi/mimo-v2-flash` catalog entry
    # (262144 input / 16384 output); costs use the mimo-v2.5-pro list price.
    "mimo": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 16_384,
        "supports_vision": False,
        "input_cost_per_token": 0.000000435,
        "output_cost_per_token": 0.00000087,
    },
    # Kimi Code membership ids served by api.kimi.com/coding/v1 (subscription
    # billing, so no per-token costs). Context windows from
    # kimi.com/code/docs, September 2026. k3 is 1M only on Allegretto+ (256K on
    # Moderato); k3-256k is the same model pinned to 256K at ~half the quota.
    "k3": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 1_048_576,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "k3-256k": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "kimi-for-coding": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    "kimi-for-coding-highspeed": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
    },
    # Moonshot Kimi models newer than the bundled LiteLLM snapshot (which tops
    # out at moonshot/kimi-k2.6). Values from platform.kimi.ai/docs/pricing,
    # July 2026; remove once a snapshot refresh covers these ids.
    "kimi-k3": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 1_048_576,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_read_input_cost_per_token": 0.0000003,
    },
    "kimi-k2.7-code": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.00000095,
        "output_cost_per_token": 0.000004,
        "cache_read_input_cost_per_token": 0.00000019,
    },
    "kimi-k2.7-code-highspeed": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 262_144,
        "supports_vision": True,
        "supports_reasoning": True,
        "input_cost_per_token": 0.0000019,
        "output_cost_per_token": 0.000008,
        "cache_read_input_cost_per_token": 0.00000038,
    },
}


@dataclass(frozen=True)
class ModelMeta:
    model_name: str
    context_window_tokens: int
    max_output_tokens: int
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    cache_read_input_cost_per_token: float | None = None
    cache_creation_input_cost_per_token: float | None = None
    cache_creation_5m_input_cost_per_token: float | None = None
    cache_creation_1h_input_cost_per_token: float | None = None
    reasoning_output_cost_per_token: float | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "fallback"
    supports_vision: bool = False
    supports_reasoning: bool | None = None
    field_sources: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    provider_key: str | None = None


@dataclass
class _LayerData:
    name: str
    values: dict[str, Any] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    model_name: str | None = None


def _parse_field(field: str, raw_value: Any) -> Any | None:
    if field in _INT_FIELDS:
        return parse_positive_int(raw_value)
    if field in _FLOAT_FIELDS:
        return parse_non_negative_float(raw_value)
    if field in _BOOL_FIELDS:
        return parse_bool(raw_value)
    return None


def _dedupe_warnings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        if not clean:
            continue
        if clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _build_model_alias_index(mapping: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key in mapping:
        if not isinstance(key, str):
            continue
        for variant in model_name_variants(key):
            index.setdefault(variant.casefold(), key)
    return index


def _lookup_model_override(
    *,
    mapping: Any,
    requested_model: str,
    label: str,
    warnings: list[str],
) -> tuple[str, dict[str, Any]] | None:
    if mapping is None:
        return None
    if not isinstance(mapping, dict):
        warnings.append(f"Ignoring invalid {label}: expected object.")
        return None

    alias_index = _build_model_alias_index(mapping)
    for variant in model_name_variants(requested_model):
        key = alias_index.get(variant.casefold())
        if key is None:
            continue
        raw = mapping.get(key)
        if isinstance(raw, dict):
            return key, raw
        warnings.append(f"Ignoring invalid {label}[{key!r}]: expected object.")
        return None
    return None


def _lookup_endpoint_override(
    *,
    endpoints: Any,
    base_url: str,
    warnings: list[str],
) -> tuple[str, dict[str, Any]] | None:
    if endpoints is None:
        return None
    if not isinstance(endpoints, dict):
        warnings.append("Ignoring invalid model_metadata_overrides.endpoints: expected object.")
        return None

    if base_url in endpoints:
        exact = endpoints.get(base_url)
        if isinstance(exact, dict):
            return base_url, exact
        warnings.append(
            f"Ignoring invalid model_metadata_overrides.endpoints[{base_url!r}]: expected object."
        )
        return None

    normalized_base_url = normalize_base_url(base_url)
    if not normalized_base_url:
        return None
    for endpoint_key, endpoint_value in endpoints.items():
        if not isinstance(endpoint_key, str):
            continue
        if normalize_base_url(endpoint_key) != normalized_base_url:
            continue
        if isinstance(endpoint_value, dict):
            return endpoint_key, endpoint_value
        warnings.append(
            "Ignoring invalid model_metadata_overrides.endpoints"
            f"[{endpoint_key!r}]: expected object."
        )
        return None
    return None


class ModelRegistry:
    def __init__(self, *, cfg: AppConfig, api_key: str | None = None) -> None:
        _ = api_key
        self._cfg = cfg
        from .task_budget import provider_auth_network_allowed

        self._provider_auth_metadata_allowed = provider_auth_network_allowed()
        self.last_error: str | None = None
        self.last_warnings: list[str] = []
        self.last_source: str | None = None

    def _provider_route_identity(self) -> tuple[str | None, str]:
        """Resolve explicit preset route identity without trusting model aliases."""

        try:
            from .profile_presets import find_preset_for_profile
            from .profiles import get_active_profile

            profile = get_active_profile(self._cfg)
            preset = find_preset_for_profile(profile)
        except Exception:  # noqa: BLE001 - metadata fallback must remain offline-safe
            return None, str(self._cfg.base_url or "").strip()
        provider_key = str(preset.provider_key or "").strip() if preset is not None else ""
        base_url = str(profile.base_url or self._cfg.base_url or "").strip()
        return provider_key or None, base_url

    def _provider_hint(self) -> str | None:
        provider_key, _base_url = self._provider_route_identity()
        return provider_key

    def _resolve_env_layer(self) -> _LayerData:
        layer = _LayerData(name="env")
        for field_name, env_name in _ENV_FIELD_MAP.items():
            raw_value = env_get(env_name)
            if raw_value is None:
                continue
            parsed = _parse_field(field_name, raw_value)
            if parsed is None:
                layer.warnings.append(f"Ignoring invalid {env_name}: {raw_value!r}")
                continue
            layer.values[field_name] = parsed
            layer.field_sources[field_name] = f"env:{env_name}"
        return layer

    def _apply_user_scope(
        self,
        *,
        layer: _LayerData,
        source: str,
        payload: dict[str, Any],
    ) -> None:
        for field_name in _TRACKED_FIELDS:
            if field_name in layer.values:
                continue
            if field_name not in payload:
                continue
            raw_value = payload.get(field_name)
            if raw_value is None:
                continue
            parsed = _parse_field(field_name, raw_value)
            if parsed is None:
                layer.warnings.append(f"Ignoring invalid {field_name} in {source}: {raw_value!r}")
                continue
            layer.values[field_name] = parsed
            layer.field_sources[field_name] = source

    def _resolve_user_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name="user")
        raw_overrides = self._cfg.extra_fields.get("model_metadata_overrides")
        if raw_overrides is None:
            return layer
        if not isinstance(raw_overrides, dict):
            layer.warnings.append("Ignoring invalid model_metadata_overrides: expected object.")
            return layer

        scoped_payloads: list[tuple[str, dict[str, Any]]] = []
        endpoint_match = _lookup_endpoint_override(
            endpoints=raw_overrides.get("endpoints"),
            base_url=self._cfg.base_url,
            warnings=layer.warnings,
        )
        if endpoint_match is not None:
            endpoint_key, endpoint_payload = endpoint_match
            endpoint_model_match = _lookup_model_override(
                mapping=endpoint_payload.get("models"),
                requested_model=requested_model,
                label=f"model_metadata_overrides.endpoints[{endpoint_key!r}].models",
                warnings=layer.warnings,
            )
            if endpoint_model_match is not None:
                model_key, model_payload = endpoint_model_match
                scoped_payloads.append(
                    (
                        f"user:endpoints[{endpoint_key!r}].models[{model_key!r}]",
                        model_payload,
                    )
                )

            endpoint_default = endpoint_payload.get("default")
            if endpoint_default is not None:
                if isinstance(endpoint_default, dict):
                    scoped_payloads.append(
                        (f"user:endpoints[{endpoint_key!r}].default", endpoint_default)
                    )
                else:
                    layer.warnings.append(
                        "Ignoring invalid model_metadata_overrides.endpoints"
                        f"[{endpoint_key!r}].default: expected object."
                    )

        model_match = _lookup_model_override(
            mapping=raw_overrides.get("models"),
            requested_model=requested_model,
            label="model_metadata_overrides.models",
            warnings=layer.warnings,
        )
        if model_match is not None:
            model_key, model_payload = model_match
            scoped_payloads.append((f"user:models[{model_key!r}]", model_payload))

        default_payload = raw_overrides.get("default")
        if default_payload is not None:
            if isinstance(default_payload, dict):
                scoped_payloads.append(("user:default", default_payload))
            else:
                layer.warnings.append(
                    "Ignoring invalid model_metadata_overrides.default: expected object."
                )

        for source, payload in scoped_payloads:
            self._apply_user_scope(layer=layer, source=source, payload=payload)

        return layer

    def _resolve_bundled_model_catalog_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name=BUNDLED_MODEL_CATALOG_SOURCE)
        provider_hint, route_base_url = self._provider_route_identity()
        try:
            static_meta = resolve_litellm_static_metadata(
                requested_model,
                base_url=route_base_url,
                provider_hint=provider_hint,
            )
        except TypeError as exc:
            # Preserve compatibility with lightweight test/plugin resolvers that
            # implement the older ``(model, *, base_url)`` extension surface.
            if "provider_hint" not in str(exc):
                raise
            static_meta = resolve_litellm_static_metadata(
                requested_model,
                base_url=route_base_url,
            )
        layer.error = static_meta.error
        layer.raw_metadata = static_meta.raw_metadata
        layer.model_name = static_meta.model_key

        values = {
            "context_window_tokens": static_meta.context_window_tokens,
            "max_output_tokens": static_meta.max_output_tokens,
            "supports_vision": static_meta.supports_vision,
            "supports_reasoning": parse_bool(static_meta.raw_metadata.get("supports_reasoning")),
            "input_cost_per_token": static_meta.input_cost_per_token,
            "output_cost_per_token": static_meta.output_cost_per_token,
            "cache_read_input_cost_per_token": static_meta.cache_read_input_cost_per_token,
            "cache_creation_input_cost_per_token": (
                static_meta.cache_creation_input_cost_per_token
            ),
            "cache_creation_5m_input_cost_per_token": (
                static_meta.cache_creation_5m_input_cost_per_token
            ),
            "cache_creation_1h_input_cost_per_token": (
                static_meta.cache_creation_1h_input_cost_per_token
            ),
            "reasoning_output_cost_per_token": static_meta.reasoning_output_cost_per_token,
        }
        for field_name, value in values.items():
            if value is None:
                continue
            layer.values[field_name] = value
            layer.field_sources[field_name] = BUNDLED_MODEL_CATALOG_SOURCE
        return layer

    def _resolve_provider_auth_layer(self, requested_model: str) -> _LayerData:
        """Project the active account's live model catalog into context metadata."""

        layer = _LayerData(name="provider_auth")
        try:
            from .profiles import get_active_profile

            profile = get_active_profile(self._cfg)
        except Exception:  # noqa: BLE001 - malformed profiles fall through to static metadata
            return layer
        provider_id = str(profile.auth_provider or "").strip()
        if not provider_id:
            return layer
        try:
            from .provider_auth import create_provider_auth

            models = create_provider_auth(provider_id).list_models(refresh=False)
        except Exception as exc:  # noqa: BLE001 - offline startup must remain possible
            layer.warnings.append(f"Subscription model metadata unavailable: {exc}")
            models = ()

        requested_variants = {
            value.casefold() for value in model_name_variants(requested_model) if value
        }
        selected = next(
            (
                model
                for model in models
                if requested_variants.intersection(
                    value.casefold() for value in model_name_variants(model.id) if value
                )
            ),
            None,
        )
        if selected is None and provider_id != "openai-codex":
            return layer

        source = f"provider_auth:{provider_id}"
        static_subscription_model = (
            resolve_chatgpt_codex_static_model(requested_model)
            if provider_id == "openai-codex"
            else None
        )
        if selected is not None:
            layer.model_name = selected.id
        elif static_subscription_model is not None:
            layer.model_name = static_subscription_model.id
            source = f"provider_auth:{provider_id}:{CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE}"
            layer.warnings.append(
                "Using the bundled ChatGPT subscription capacity snapshot because live "
                "metadata for the selected model is unavailable."
            )
        else:
            layer.model_name = requested_model
            source = f"provider_auth:{provider_id}:conservative-default"
            layer.warnings.append(
                "Using conservative ChatGPT subscription capacity because the selected "
                "model is absent from both live and bundled subscription metadata."
            )

        context_window_tokens = (
            selected.context_window_tokens if selected is not None else None
        ) or (
            static_subscription_model.context_window_tokens
            if static_subscription_model is not None
            else None
        )
        max_output_tokens = (selected.max_output_tokens if selected is not None else None) or (
            static_subscription_model.max_output_tokens
            if static_subscription_model is not None
            else None
        )
        input_modalities = (
            selected.input_modalities
            if selected is not None
            else (
                static_subscription_model.input_modalities
                if static_subscription_model is not None
                else ("text",)
            )
        )
        layer.raw_metadata = {
            "provider_auth": provider_id,
            "subscription_backed": True,
            "input_modalities": list(input_modalities),
        }
        selected_reasoning_efforts = (
            selected.reasoning_efforts
            if selected is not None
            else (
                static_subscription_model.reasoning_efforts
                if static_subscription_model is not None
                else ()
            )
        )
        selected_default_effort = (
            selected.default_reasoning_effort
            if selected is not None
            else (
                static_subscription_model.default_reasoning_effort
                if static_subscription_model is not None
                else None
            )
        )
        if selected is not None or static_subscription_model is not None:
            supports_reasoning = bool(selected_reasoning_efforts or selected_default_effort)
            layer.values["supports_reasoning"] = supports_reasoning
            layer.field_sources["supports_reasoning"] = source
            layer.raw_metadata["supports_reasoning"] = supports_reasoning
        if context_window_tokens is not None:
            layer.values["context_window_tokens"] = context_window_tokens
            layer.field_sources["context_window_tokens"] = source
            layer.raw_metadata["context_window_tokens"] = context_window_tokens
            # The Codex catalog currently omits a response-output ceiling. Keep
            # Alysis Code's existing local reserve without adding a wire-level cap.
            layer.values["max_output_tokens"] = (
                max_output_tokens or DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
            )
            layer.field_sources["max_output_tokens"] = (
                source if max_output_tokens is not None else f"{source}:local-default"
            )
            layer.raw_metadata["max_output_tokens"] = layer.values["max_output_tokens"]
        if max_output_tokens is not None and "max_output_tokens" not in layer.values:
            layer.values["max_output_tokens"] = max_output_tokens
            layer.field_sources["max_output_tokens"] = source
            layer.raw_metadata["max_output_tokens"] = max_output_tokens
        if provider_id == "openai-codex" and "context_window_tokens" not in layer.values:
            layer.values["context_window_tokens"] = DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW_TOKENS
            layer.field_sources["context_window_tokens"] = source
            layer.values["max_output_tokens"] = DEFAULT_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
            layer.field_sources["max_output_tokens"] = f"{source}:local-default"
            layer.raw_metadata["context_window_tokens"] = layer.values["context_window_tokens"]
            layer.raw_metadata["max_output_tokens"] = layer.values["max_output_tokens"]
        layer.values["supports_vision"] = "image" in input_modalities
        layer.field_sources["supports_vision"] = source
        for field_name in _FLOAT_FIELDS:
            layer.values[field_name] = 0.0
            layer.field_sources[field_name] = f"{source}:included"
        return layer

    def _resolve_official_provider_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name=OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE)
        provider_key, route_base_url = self._provider_route_identity()
        normalized_provider = str(provider_key or "").strip().lower()
        provider_models = _OFFICIAL_PROVIDER_MODEL_METADATA.get(normalized_provider)
        if provider_models is None:
            return layer
        model_match = _lookup_model_override(
            mapping=provider_models,
            requested_model=requested_model,
            label=f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}.{normalized_provider}",
            warnings=layer.warnings,
        )
        if model_match is None:
            return layer

        model_key, payload = model_match
        payload = dict(payload)
        if normalized_provider == "moonshot":
            try:
                route_host = (urlsplit(route_base_url).hostname or "").rstrip(".").lower()
            except ValueError:
                route_host = ""
            if route_host != "api.moonshot.ai":
                # The verified prices are Moonshot's global USD rates. Capacity
                # and capabilities also apply to China, but those prices do not.
                for field_name in _FLOAT_FIELDS:
                    payload.pop(field_name, None)
        source = f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:{normalized_provider}"
        layer.model_name = model_key
        layer.raw_metadata = {
            **payload,
            "provider": normalized_provider,
            "catalog_source": OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE,
            "catalog_sources": list(
                _OFFICIAL_PROVIDER_MODEL_SOURCES.get(normalized_provider, {}).get(model_key, ())
            ),
        }
        self._apply_user_scope(layer=layer, source=source, payload=payload)
        return layer

    def _resolve_canonical_model_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name=CANONICAL_MODEL_CATALOG_SOURCE)
        provider_key, _route_base_url = self._provider_route_identity()
        normalized_provider = str(provider_key or "").strip().lower()

        canonical_id: str | None = None
        provider_aliases = _PROVIDER_CANONICAL_MODEL_IDS.get(normalized_provider, {})
        provider_alias_index = _build_model_alias_index(provider_aliases)
        for variant in model_name_variants(requested_model):
            route_id = provider_alias_index.get(variant.casefold())
            if route_id is not None:
                canonical_id = provider_aliases.get(route_id)
                break

        if canonical_id is None:
            canonical_alias_index = _build_model_alias_index(_CANONICAL_MODEL_METADATA)
            for variant in model_name_variants(requested_model):
                canonical_id = canonical_alias_index.get(variant.casefold())
                if canonical_id is not None:
                    break
        if canonical_id is None:
            return layer

        payload = _CANONICAL_MODEL_METADATA.get(canonical_id)
        if payload is None:
            return layer
        source = f"{CANONICAL_MODEL_CATALOG_SOURCE}:{canonical_id}"
        layer.raw_metadata = {
            **payload,
            "canonical_model": canonical_id,
            "canonical_catalog_sources": list(_CANONICAL_MODEL_SOURCES.get(canonical_id, ())),
        }
        self._apply_user_scope(layer=layer, source=source, payload=payload)
        return layer

    def _resolve_builtin_layer(self, requested_model: str) -> _LayerData:
        layer = _LayerData(name="built_in")
        model_match = _lookup_model_override(
            mapping=_BUILT_IN_MODEL_METADATA,
            requested_model=requested_model,
            label="built_in_model_catalog",
            warnings=layer.warnings,
        )
        if model_match is None:
            return layer

        model_key, payload = model_match
        layer.model_name = model_key
        layer.raw_metadata = dict(payload)
        self._apply_user_scope(layer=layer, source="built_in", payload=payload)
        return layer

    def _resolve_learned_layer(self, requested_model: str) -> _LayerData:
        _ = requested_model
        # Placeholder layer for deterministic precedence; learned cache is not implemented yet.
        return _LayerData(name="learned")

    def _resolve_field_value(self, field_name: str, layers: list[_LayerData]) -> tuple[Any, str]:
        for layer in layers:
            if field_name not in layer.values:
                continue
            value = layer.values[field_name]
            if value is None:
                continue
            return value, layer.field_sources.get(field_name, layer.name)
        return _FALLBACKS[field_name], "fallback"

    def get(self, model_name: str, *, include_provider_auth: bool = True) -> ModelMeta:
        from .task_budget import provider_auth_network_allowed

        include_provider_auth = (
            include_provider_auth
            and self._provider_auth_metadata_allowed
            and provider_auth_network_allowed()
        )
        requested = model_name.strip() or self._cfg.model.strip() or "unknown-model"
        warnings: list[str] = []
        if "model_capabilities" in self._cfg.extra_fields:
            warnings.append(_DEPRECATED_MODEL_CAPABILITIES_WARNING)

        env_layer = self._resolve_env_layer()
        user_layer = self._resolve_user_layer(requested)
        provider_auth_layer = (
            self._resolve_provider_auth_layer(requested)
            if include_provider_auth
            else _LayerData(name="provider_auth")
        )
        official_provider_layer = self._resolve_official_provider_layer(requested)
        bundled_catalog_layer = self._resolve_bundled_model_catalog_layer(requested)
        canonical_model_layer = self._resolve_canonical_model_layer(requested)
        route_provider, route_base_url = self._provider_route_identity()
        active_provider = (
            str(route_provider or known_provider_key_from_base_url(route_base_url) or "")
            .strip()
            .lower()
        )
        bundled_provider = (
            str(bundled_catalog_layer.raw_metadata.get("litellm_provider") or "").strip().lower()
        )
        catalog_route_provider = (
            str(bundled_catalog_layer.raw_metadata.get("catalog_provider_hint") or "")
            .strip()
            .lower()
            or bundled_provider
        )
        if catalog_route_provider and catalog_route_provider != active_provider:
            # Catalog prices belong to the route selected by the catalog lookup.
            # A model-name match may still supply portable capability data, but
            # it cannot establish prices for an unrelated compatible endpoint.
            for field_name in _FLOAT_FIELDS:
                bundled_catalog_layer.values.pop(field_name, None)
                bundled_catalog_layer.field_sources.pop(field_name, None)
            bundled_catalog_layer.raw_metadata = {}
        built_in_layer = self._resolve_builtin_layer(requested)
        learned_layer = self._resolve_learned_layer(requested)
        layers = [
            env_layer,
            user_layer,
            provider_auth_layer,
            official_provider_layer,
            canonical_model_layer,
            bundled_catalog_layer,
            built_in_layer,
            learned_layer,
        ]

        for layer in layers:
            warnings.extend(layer.warnings)

        field_sources: dict[str, str] = {}
        resolved_fields: dict[str, Any] = {}
        for field_name in _TRACKED_FIELDS:
            value, source = self._resolve_field_value(field_name, layers)
            resolved_fields[field_name] = value
            field_sources[field_name] = source

        context_window_tokens = (
            parse_positive_int(resolved_fields["context_window_tokens"])
            or _FALLBACKS["context_window_tokens"]
        )
        max_output_tokens = (
            parse_positive_int(resolved_fields["max_output_tokens"])
            or _FALLBACKS["max_output_tokens"]
        )
        supports_vision = (
            parse_bool(resolved_fields["supports_vision"])
            if resolved_fields["supports_vision"] is not None
            else _FALLBACKS["supports_vision"]
        )
        if supports_vision is None:
            supports_vision = _FALLBACKS["supports_vision"]
        supports_reasoning = (
            parse_bool(resolved_fields["supports_reasoning"])
            if resolved_fields["supports_reasoning"] is not None
            else None
        )
        input_cost_per_token = (
            parse_non_negative_float(resolved_fields["input_cost_per_token"])
            if resolved_fields["input_cost_per_token"] is not None
            else None
        )
        output_cost_per_token = (
            parse_non_negative_float(resolved_fields["output_cost_per_token"])
            if resolved_fields["output_cost_per_token"] is not None
            else None
        )
        cache_read_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_read_input_cost_per_token"])
            if resolved_fields["cache_read_input_cost_per_token"] is not None
            else None
        )
        cache_creation_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_creation_input_cost_per_token"])
            if resolved_fields["cache_creation_input_cost_per_token"] is not None
            else None
        )
        cache_creation_5m_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_creation_5m_input_cost_per_token"])
            if resolved_fields["cache_creation_5m_input_cost_per_token"] is not None
            else None
        )
        cache_creation_1h_input_cost_per_token = (
            parse_non_negative_float(resolved_fields["cache_creation_1h_input_cost_per_token"])
            if resolved_fields["cache_creation_1h_input_cost_per_token"] is not None
            else None
        )
        reasoning_output_cost_per_token = (
            parse_non_negative_float(resolved_fields["reasoning_output_cost_per_token"])
            if resolved_fields["reasoning_output_cost_per_token"] is not None
            else None
        )

        if max_output_tokens >= context_window_tokens:
            # Shared-window metadata (e.g. the Kimi Code ids publish max_tokens
            # up to the full context). Clamping to window-1 — the old behaviour —
            # left a 1-token input budget, which surfaced as "context: 0% left"
            # on a fresh 1M-context session. Reserve a conservative response
            # allowance instead so the input budget keeps most of the window.
            clamped = max(1, min(max_output_tokens, max(4096, context_window_tokens // 8)))
            warnings.append(
                "max_output_tokens >= context_window_tokens (shared window); "
                f"reserving {clamped} tokens for output."
            )
            max_output_tokens = clamped

        key_fallback = (
            field_sources.get("context_window_tokens") == "fallback"
            or field_sources.get("max_output_tokens") == "fallback"
        )
        if key_fallback:
            warnings.append(_FALLBACK_WARNING)

        all_sources = {field_sources.get(field_name, "fallback") for field_name in _TRACKED_FIELDS}
        overall_source = next(iter(all_sources)) if len(all_sources) == 1 else "mixed"

        resolved_model_name = next(
            (
                layer.model_name
                for layer in layers
                if layer.values and isinstance(layer.model_name, str) and layer.model_name.strip()
            ),
            requested,
        )

        final_warnings = tuple(_dedupe_warnings(warnings))
        self.last_error = bundled_catalog_layer.error if key_fallback else None
        self.last_warnings = list(final_warnings)
        self.last_source = overall_source

        raw_metadata = dict(bundled_catalog_layer.raw_metadata)
        raw_metadata.update(canonical_model_layer.raw_metadata)
        raw_metadata.update(official_provider_layer.raw_metadata)
        raw_metadata.update(provider_auth_layer.raw_metadata)
        return ModelMeta(
            model_name=resolved_model_name,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            input_cost_per_token=input_cost_per_token,
            output_cost_per_token=output_cost_per_token,
            cache_read_input_cost_per_token=cache_read_input_cost_per_token,
            cache_creation_input_cost_per_token=cache_creation_input_cost_per_token,
            cache_creation_5m_input_cost_per_token=cache_creation_5m_input_cost_per_token,
            cache_creation_1h_input_cost_per_token=cache_creation_1h_input_cost_per_token,
            reasoning_output_cost_per_token=reasoning_output_cost_per_token,
            raw_metadata=raw_metadata,
            source=overall_source,
            supports_vision=bool(supports_vision),
            supports_reasoning=supports_reasoning,
            field_sources=field_sources,
            warnings=final_warnings,
            provider_key=self._provider_hint(),
        )


def resolve_model_provider_key(
    *,
    cfg: AppConfig,
    model_name: str,
    base_url: str | None = None,
    profile_name: str | None = None,
) -> str | None:
    requested_model = str(model_name or "").strip()
    resolved_base_url = base_url or getattr(cfg, "base_url", None)
    try:
        from .profile_presets import find_preset_for_base_url

        preset = find_preset_for_base_url(str(resolved_base_url or ""))
    except Exception:  # noqa: BLE001 - provider inference remains best effort
        preset = None
    preset_provider = str(preset.provider_key or "").strip() if preset is not None else ""
    if preset_provider:
        return preset_provider

    url_provider = _provider_key_from_base_url(resolved_base_url)
    if url_provider == "openrouter":
        return url_provider

    if requested_model:
        meta = ModelRegistry(cfg=cfg).get(requested_model)
        provider = str(meta.raw_metadata.get("litellm_provider") or "").strip()
        if provider:
            return provider
        resolved_model = str(meta.model_name or "").strip()
        if "/" in resolved_model:
            return resolved_model.split("/", 1)[0].strip() or None

    if url_provider:
        return url_provider

    profile_provider = str(profile_name or "").strip()
    if profile_provider and profile_provider.lower() != "default":
        return profile_provider

    if "/" in requested_model:
        return requested_model.split("/", 1)[0].strip() or None
    return requested_model or None


def _provider_key_from_base_url(base_url: str | None) -> str | None:
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        hostname = (urlsplit(raw).hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if not hostname:
        return None
    known_provider = known_provider_key_from_base_url(raw)
    if known_provider:
        return known_provider
    parts = [
        part
        for part in hostname.split(".")
        if part and part not in {"api", "ai", "www", "com", "v1", "v1beta"}
    ]
    return parts[-1] if parts else hostname
