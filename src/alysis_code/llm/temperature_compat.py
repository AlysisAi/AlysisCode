from __future__ import annotations

import re

ANTHROPIC_DEPRECATED_SAMPLING_PARAMETERS = "anthropic_sampling_parameters_deprecated"
GEMINI_3_DEFAULT_TEMPERATURE = "gemini_3_default_temperature"
DEEPSEEK_THINKING_TEMPERATURE_UNSUPPORTED = "deepseek_thinking_temperature_unsupported"
QWEN_QVQ_DEFAULT_TEMPERATURE = "qwen_qvq_default_temperature"
KIMI_FIXED_TEMPERATURE = "kimi_fixed_temperature"
GPT6_REASONING_TEMPERATURE_UNSUPPORTED = "gpt6_reasoning_temperature_unsupported"

_CLAUDE_OPUS_VERSION_RE = re.compile(r"claude[-_.]opus[-_.](?P<major>\d+)(?:[-_.](?P<minor>\d+))?")
_CLAUDE_SONNET_VERSION_RE = re.compile(
    r"claude[-_.]sonnet[-_.](?P<major>\d+)(?:[-_.](?P<minor>\d+))?"
)
_GEMINI_3_RE = re.compile(r"(?:^|[/.:_-])gemini[-_.]?3(?:[-_.]|$)")
_DEEPSEEK_V4_RE = re.compile(r"(?:^|[/.:_-])deepseek[-_.]v?4(?:[-_.]|$)")
_QWEN_QVQ_RE = re.compile(r"(?:^|[/.:_-])qvq(?:[-_.]|$)")
_CURRENT_KIMI_RE = re.compile(r"(?:^|[/.:_-])kimi[-_.]?k(?:3|2[-_.]?(?:6|7))(?:[-_.]|$)")
_GPT6_RE = re.compile(r"(?:^|/)gpt-6(?:\.1)?-(?:astra|sol|luna)(?:$|-)")


def documented_temperature_omit_reason(
    model: str,
    *,
    provider_key: str | None = None,
    thinking_enabled: bool | None = None,
    reasoning_effort: str | None = None,
) -> str | None:
    """Return the documented reason an optional temperature must be omitted.

    The checks deliberately use model-family patterns rather than a closed list of
    snapshots so provider aliases, gateway-qualified names, and later compatible
    releases receive the same safe request shape.
    """

    normalized_model = str(model or "").strip().casefold()
    if not normalized_model:
        return None

    # GPT-6 defaults to reasoning. Temperature is valid only at explicit none.
    # https://developers.openai.com/api/docs/guides/latest-model
    if _GPT6_RE.search(normalized_model) and str(reasoning_effort or "").casefold() != "none":
        return GPT6_REASONING_TEMPERATURE_UNSUPPORTED

    opus = _CLAUDE_OPUS_VERSION_RE.search(normalized_model)
    if opus is not None:
        major = int(opus.group("major"))
        minor_text = opus.group("minor")
        minor = int(minor_text) if minor_text is not None else None
        # Pre-4.6 snapshot IDs use a date after the major version (for example
        # claude-opus-4-20250514). Do not mistake that date for a minor release.
        if major > 4 or (major == 4 and minor is not None and 7 <= minor < 100):
            return ANTHROPIC_DEPRECATED_SAMPLING_PARAMETERS

    sonnet = _CLAUDE_SONNET_VERSION_RE.search(normalized_model)
    if sonnet is not None and int(sonnet.group("major")) >= 5:
        return ANTHROPIC_DEPRECATED_SAMPLING_PARAMETERS

    if re.search(r"(?:^|[/.:_-])claude[-_.]haiku[-_.]5[-_.]5(?:$|[-_.])", normalized_model):
        return ANTHROPIC_DEPRECATED_SAMPLING_PARAMETERS

    if _GEMINI_3_RE.search(normalized_model):
        return GEMINI_3_DEFAULT_TEMPERATURE

    if _QWEN_QVQ_RE.search(normalized_model):
        return QWEN_QVQ_DEFAULT_TEMPERATURE

    normalized_provider = str(provider_key or "").strip().casefold()
    if normalized_provider == "moonshot" and _CURRENT_KIMI_RE.search(normalized_model):
        return KIMI_FIXED_TEMPERATURE
    if (
        normalized_provider == "deepseek"
        and thinking_enabled is not False
        and _DEEPSEEK_V4_RE.search(normalized_model)
    ):
        return DEEPSEEK_THINKING_TEMPERATURE_UNSUPPORTED

    return None


__all__ = [
    "ANTHROPIC_DEPRECATED_SAMPLING_PARAMETERS",
    "DEEPSEEK_THINKING_TEMPERATURE_UNSUPPORTED",
    "GEMINI_3_DEFAULT_TEMPERATURE",
    "GPT6_REASONING_TEMPERATURE_UNSUPPORTED",
    "KIMI_FIXED_TEMPERATURE",
    "QWEN_QVQ_DEFAULT_TEMPERATURE",
    "documented_temperature_omit_reason",
]
