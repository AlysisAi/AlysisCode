"""Reviewed model metadata and parent/subagent prompt profiles.

This catalog selects prompt text, never model capabilities or execution policy.
Preset model choices carry their reviewed prompt assignments; account discovery,
pricing rows, routes, and retirement migrations do not establish a model's family.
Unknown or opaque identities use the configured general-expanded fallback. Keep routing
exact: no prefix, substring, namespace stripping, or provider-name inference.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, cast, get_args

PromptFamily = Literal[
    "general",
    "gpt",
    "claude",
    "gemini",
    "qwen",
    "glm",
    "deepseek",
    "kimi",
    "minimax",
    "mimo",
    "seed",
    "nemotron",
    "gpt-oss",
    "gemma",
    "mistral",
    "grok",
    "command",
    "sonar",
    "llama",
]

PROMPT_FAMILIES: tuple[PromptFamily, ...] = get_args(PromptFamily)

ParentPromptGuidanceProfile = Literal[
    "compact",
    "balanced",
    "expanded",
    "gpt-astra",
    "gpt-sol",
    "gpt-terra",
    "gpt-luna",
    "gpt-5.5",
    "claude-fable",
    "claude-opus",
    "claude-sonnet",
    "gemini",
    "qwen",
    "glm",
    "general-normal",
    "general-expanded",
    "gpt-normal",
    "gpt-expanded",
    "claude-normal",
    "claude-expanded",
    "gemini-normal",
    "gemini-expanded",
    "qwen-normal",
    "qwen-expanded",
    "glm-normal",
    "glm-expanded",
    "deepseek-normal",
    "deepseek-expanded",
    "kimi-normal",
    "kimi-expanded",
    "minimax-normal",
    "minimax-expanded",
    "mimo-normal",
    "mimo-expanded",
    "seed-normal",
    "seed-expanded",
    "nemotron-normal",
    "nemotron-expanded",
    "gpt-oss-normal",
    "gpt-oss-expanded",
    "gemma-normal",
    "gemma-expanded",
    "mistral-normal",
    "mistral-expanded",
    "grok-normal",
    "grok-expanded",
    "command-normal",
    "command-expanded",
    "sonar-normal",
    "sonar-expanded",
    "llama-normal",
    "llama-expanded",
]

SubagentPromptGuidanceProfile = Literal[
    "general-subagent",
    "gpt-subagent",
    "claude-subagent",
    "gemini-subagent",
    "qwen-subagent",
    "glm-subagent",
    "deepseek-subagent",
    "kimi-subagent",
    "minimax-subagent",
    "mimo-subagent",
    "seed-subagent",
    "nemotron-subagent",
    "gpt-oss-subagent",
    "gemma-subagent",
    "mistral-subagent",
    "grok-subagent",
    "command-subagent",
    "sonar-subagent",
    "llama-subagent",
]

PromptGuidanceProfile = ParentPromptGuidanceProfile | SubagentPromptGuidanceProfile
GUIDANCE_PROFILES: tuple[ParentPromptGuidanceProfile, ...] = get_args(ParentPromptGuidanceProfile)
ALL_GUIDANCE_PROFILES: tuple[PromptGuidanceProfile, ...] = (
    *GUIDANCE_PROFILES,
    *get_args(SubagentPromptGuidanceProfile),
)
PromptGuidanceVariant = Literal["normal", "expanded", "subagent", "compact", "balanced"]

# Legacy saved names remain valid. Explicit compact/balanced rendering remains
# available; named legacy profiles select their family's normal/expanded text.
_PROFILE_ASSIGNMENTS: dict[str, tuple[PromptFamily, PromptGuidanceVariant]] = {
    "compact": ("general", "compact"),
    "balanced": ("general", "balanced"),
    "expanded": ("general", "expanded"),
    "gpt-astra": ("gpt", "normal"),
    "gpt-sol": ("gpt", "normal"),
    "gpt-terra": ("gpt", "normal"),
    "gpt-luna": ("gpt", "expanded"),
    "gpt-5.5": ("gpt", "expanded"),
    "claude-fable": ("claude", "expanded"),
    "claude-opus": ("claude", "expanded"),
    "claude-sonnet": ("claude", "expanded"),
    "gemini": ("gemini", "expanded"),
    "qwen": ("qwen", "expanded"),
    "glm": ("glm", "expanded"),
}
for _family in PROMPT_FAMILIES:
    for _variant in ("normal", "expanded", "subagent"):
        _PROFILE_ASSIGNMENTS[f"{_family}-{_variant}"] = (
            _family,
            cast(PromptGuidanceVariant, _variant),
        )


def profile_family(profile: str) -> PromptFamily:
    """Resolve only a registered profile; unknown names use the general family."""
    return _PROFILE_ASSIGNMENTS.get(profile, ("general", "expanded"))[0]


def profile_variant(profile: str) -> PromptGuidanceVariant:
    """Preserve legacy density names and distinguish the one child variant."""
    return _PROFILE_ASSIGNMENTS.get(profile, ("general", "expanded"))[1]


def subagent_profile(profile: str) -> SubagentPromptGuidanceProfile:
    """Every parent variant in a family shares one canonical worker profile."""
    return cast(SubagentPromptGuidanceProfile, f"{profile_family(profile)}-subagent")


@dataclass(frozen=True)
class ReviewedModel:
    """One selectable model identity and its explicit prompt assignment."""

    id: str
    prompt_profile: ParentPromptGuidanceProfile

    def __post_init__(self) -> None:
        if not self.id or self.id != self.id.strip():
            raise ValueError("Reviewed models require a nonempty, trimmed model ID")
        if self.prompt_profile not in GUIDANCE_PROFILES:
            raise ValueError(f"Invalid parent prompt profile: {self.prompt_profile!r}")


# Reviewed identities outside the preset choices remain available for saved
# sessions and manually entered models. Selectable preset IDs live exclusively
# with their model-choice metadata, never in this compatibility catalog.
_ADDITIONAL_REVIEWED_MODEL_PROFILES: dict[str, ParentPromptGuidanceProfile] = {
    # gpt
    "gpt-5.6": "gpt-normal",
    "gpt-5.4": "gpt-expanded",
    "gpt-5.3-codex-spark": "gpt-expanded",
    # Reviewed bare OpenAI text/tool metadata IDs; older variants default expanded.
    "chatgpt-4o-latest": "gpt-expanded",
    "codex-mini-latest": "gpt-expanded",
    "gpt-3.5-turbo": "gpt-expanded",
    "gpt-3.5-turbo-0125": "gpt-expanded",
    "gpt-3.5-turbo-1106": "gpt-expanded",
    "gpt-4": "gpt-expanded",
    "gpt-4-0125-preview": "gpt-expanded",
    "gpt-4-0613": "gpt-expanded",
    "gpt-4-1106-preview": "gpt-expanded",
    "gpt-4-turbo": "gpt-expanded",
    "gpt-4-turbo-2024-04-09": "gpt-expanded",
    "gpt-4-turbo-preview": "gpt-expanded",
    "gpt-4.1": "gpt-expanded",
    "gpt-4.1-2025-04-14": "gpt-expanded",
    "gpt-4.1-mini": "gpt-expanded",
    "gpt-4.1-mini-2025-04-14": "gpt-expanded",
    "gpt-4.1-nano": "gpt-expanded",
    "gpt-4.1-nano-2025-04-14": "gpt-expanded",
    "gpt-4o": "gpt-expanded",
    "gpt-4o-2024-05-13": "gpt-expanded",
    "gpt-4o-2024-08-06": "gpt-expanded",
    "gpt-4o-2024-11-20": "gpt-expanded",
    "gpt-4o-mini": "gpt-expanded",
    "gpt-4o-mini-2024-07-18": "gpt-expanded",
    "gpt-5": "gpt-expanded",
    "gpt-5.1": "gpt-expanded",
    "gpt-5.1-2025-11-13": "gpt-expanded",
    "gpt-5.2": "gpt-expanded",
    "gpt-5.2-2025-12-11": "gpt-expanded",
    "gpt-5.2-chat-latest": "gpt-expanded",
    "gpt-5.3-chat-latest": "gpt-expanded",
    "gpt-5.2-pro": "gpt-expanded",
    "gpt-5.2-pro-2025-12-11": "gpt-expanded",
    "gpt-5.5-2026-04-23": "gpt-expanded",
    "gpt-5.5-pro": "gpt-expanded",
    "gpt-5.5-pro-2026-04-23": "gpt-expanded",
    "gpt-5.4-2026-03-05": "gpt-expanded",
    "gpt-5.4-pro": "gpt-expanded",
    "gpt-5.4-pro-2026-03-05": "gpt-expanded",
    "gpt-5.4-mini-2026-03-17": "gpt-expanded",
    "gpt-5.4-nano": "gpt-expanded",
    "gpt-5.4-nano-2026-03-17": "gpt-expanded",
    "gpt-5-pro": "gpt-expanded",
    "gpt-5-pro-2025-10-06": "gpt-expanded",
    "gpt-5-2025-08-07": "gpt-expanded",
    "gpt-5-codex": "gpt-expanded",
    "gpt-5.1-codex": "gpt-expanded",
    "gpt-5.1-codex-max": "gpt-expanded",
    "gpt-5.1-codex-mini": "gpt-expanded",
    "gpt-5.2-codex": "gpt-expanded",
    "gpt-5-mini": "gpt-expanded",
    "gpt-5-mini-2025-08-07": "gpt-expanded",
    "gpt-5-nano": "gpt-expanded",
    "gpt-5-nano-2025-08-07": "gpt-expanded",
    "o1": "gpt-expanded",
    "o1-2024-12-17": "gpt-expanded",
    "o1-pro": "gpt-expanded",
    "o1-pro-2025-03-19": "gpt-expanded",
    "o3": "gpt-expanded",
    "o3-2025-04-16": "gpt-expanded",
    "o3-mini": "gpt-expanded",
    "o3-mini-2025-01-31": "gpt-expanded",
    "o3-pro": "gpt-expanded",
    "o3-pro-2025-06-10": "gpt-expanded",
    "o4-mini": "gpt-expanded",
    "o4-mini-2025-04-16": "gpt-expanded",
    # claude
    "claude-fable-5": "claude-normal",
    # gemini
    "gemini-3.6-flash": "gemini-normal",
    "gemini-3.5-flash": "gemini-normal",
    "gemini-3.1-flash-lite": "gemini-expanded",
    "gemini-3-flash-preview": "gemini-normal",
    # qwen
    "qwen3.8-max-0902": "qwen-normal",
    # Ids that left a preset's suggested list (retired or superseded) keep
    # the prompt family they were reviewed with, so saved configs that still
    # name them get the same guidance.
    "MiniMax-M2.7": "minimax-normal",
    "MiniMax-M2.7-highspeed": "minimax-normal",
    "MiniMax-M2.5": "minimax-expanded",
    "MiniMaxAI/MiniMax-M3": "minimax-expanded",
    "mimo-v2.5-pro": "mimo-normal",
    "mimo-v2.5": "mimo-expanded",
    "gemma-4-31b": "gemma-expanded",
    "mistral-medium-3-5": "mistral-normal",
    "mistral-large-2512": "mistral-normal",
    "zai-glm-5-2": "glm-expanded",
    "perplexity/kimi-k2.7-code": "kimi-normal",
    "perplexity/deepseek-v4-flash-0731": "deepseek-normal",
    "deepseek/deepseek-v4-flash-0731": "deepseek-normal",
    "deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek-normal",
    # deepseek
    "deepseek-v4-flash": "deepseek-normal",
    "deepseek-v4-flash-vision-exp": "deepseek-expanded",
    "deepseek-v4.1-flash-expires-on-0910": "deepseek-expanded",
}


def build_model_prompt_catalog(
    model_choices: Iterable[ReviewedModel],
) -> dict[str, ParentPromptGuidanceProfile]:
    """Combine explicit assignments, rejecting ambiguous shared model IDs."""
    catalog = dict(_ADDITIONAL_REVIEWED_MODEL_PROFILES)
    for model in model_choices:
        previous = catalog.get(model.id)
        if previous is not None and previous != model.prompt_profile:
            raise ValueError(f"Conflicting prompt assignments for model {model.id!r}")
        catalog[model.id] = model.prompt_profile
    return catalog


def get_model_prompt_catalog() -> dict[str, ParentPromptGuidanceProfile]:
    """Build an independent default mapping from the selectable model records.

    Import presets after config initialization: protocol validation imports
    ConfigError, while PromptGuidanceConfig calls this through its default factory.
    Migration aliases are deliberately excluded; a replacement may change family.
    """
    from .profile_presets import PROFILE_PRESETS

    choices: list[ReviewedModel] = []
    for preset in PROFILE_PRESETS:
        if tuple(model.id for model in preset.model_choices) != preset.suggested_models:
            raise ValueError(f"Preset {preset.key!r} has models without reviewed prompt metadata")
        choices.extend(preset.model_choices)
    return build_model_prompt_catalog(choices)
