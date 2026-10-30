"""Shared provider capability metadata; no request implementation."""

from __future__ import annotations

_DASHSCOPE_RESPONSES_SEARCH_MODEL_PREFIXES = (
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.6-plus",
    "qwen3.6-flash",
    "qwen3.6-max",
)


_DASHSCOPE_CHAT_SEARCH_MODEL_PREFIXES = (
    "qwen3.5-plus",
    "qwen3.5-flash",
    "qwen3-max",
    "qwen-plus",
    "qwen-flash",
    "qwen-max",
)


def _normalized_dashscope_model(model: str | None) -> str:
    normalized = str(model or "").strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized


def dashscope_model_uses_responses_search(model: str | None) -> bool:
    normalized = _normalized_dashscope_model(model)
    return any(
        normalized.startswith(prefix) for prefix in _DASHSCOPE_RESPONSES_SEARCH_MODEL_PREFIXES
    )


def dashscope_model_supports_web_search(model: str | None) -> bool:
    normalized = _normalized_dashscope_model(model)
    return dashscope_model_uses_responses_search(normalized) or any(
        normalized.startswith(prefix) for prefix in _DASHSCOPE_CHAT_SEARCH_MODEL_PREFIXES
    )
