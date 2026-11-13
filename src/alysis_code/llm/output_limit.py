"""Recognize a reply that the model's output limit cut off, whatever protocol returned it.

The Responses client raises LLMOutputLimitError for such a reply. The Anthropic Messages and
chat-completions clients return it as an ordinary response, so every other caller keeps what it
got before, and raise LLMOutputLimitError only where that reply used to be an error. The agent
turn asks here, so it takes the same recovery on every protocol.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from typing import Any

from .metadata import ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY
from .types import LLMResponse


def anthropic_reply_hit_output_limit(raw: Any) -> bool:
    """Whether an Anthropic Messages reply stopped at its output limit."""
    return isinstance(raw, Mapping) and str(raw.get("stop_reason") or "").strip() == "max_tokens"


def chat_completion_hit_output_limit(raw: Any) -> bool:
    """Whether a chat-completions reply stopped at its output limit (finish_reason "length")."""
    if not isinstance(raw, Mapping):
        return False
    reason = raw.get("finish_reason")  # streamed replies record it at the top level
    if reason is None:
        choices = raw.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            reason = choices[0].get("finish_reason")
    return isinstance(reason, str) and reason.strip().lower() == "length"


def finished_text_blocks(content: Any) -> list[dict[str, Any]]:
    """The part of an interrupted Anthropic reply that may be replayed: its finished text.

    No tool call from it runs or is replayed (it was not finished being decided, even when its
    input happens to parse), and a thinking block cut off before its signature cannot be sent
    back; thinking is not required before an assistant message that has no tool use.
    """
    if not isinstance(content, list):
        return []
    return [
        copy.deepcopy(block)
        for block in content
        if isinstance(block, dict)
        and str(block.get("type") or "") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]


def _anthropic_reply_without_unfinished_parts(response: LLMResponse) -> LLMResponse:
    metadata = copy.deepcopy(response.provider_metadata) if response.provider_metadata else None
    anthropic_metadata = (
        metadata.get(ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY)
        if isinstance(metadata, dict)
        else None
    )
    if isinstance(anthropic_metadata, dict) and "content_blocks" in anthropic_metadata:
        anthropic_metadata["content_blocks"] = finished_text_blocks(
            anthropic_metadata["content_blocks"]
        )
    return dataclasses.replace(response, tool_calls=[], provider_metadata=metadata)


def response_cut_off_by_output_limit(response: Any) -> LLMResponse | None:
    """The reply reduced to what may be acted on and replayed, or None if it finished.

    None of its tool calls is kept, even one whose arguments parse: the model had not finished
    deciding on it. Anything other than an LLMResponse is left as it was.
    """
    if not isinstance(response, LLMResponse):
        return None
    raw = response.raw
    if anthropic_reply_hit_output_limit(raw):
        return _anthropic_reply_without_unfinished_parts(response)
    if chat_completion_hit_output_limit(raw):
        return dataclasses.replace(response, tool_calls=[])
    return None


def catalogued_output_ceiling(model_meta: Any) -> int | None:
    """The model's output ceiling when a catalog states it.

    None when the registry only guessed it (the unknown-model fallback), since naming a guessed
    limit could be refused by the provider.
    """
    ceiling = getattr(model_meta, "max_output_tokens", None)
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        return None
    field_sources = getattr(model_meta, "field_sources", None)
    source = (
        field_sources.get("max_output_tokens") if isinstance(field_sources, Mapping) else None
    ) or getattr(model_meta, "source", None)
    if not isinstance(source, str) or source in {"", "fallback"}:
        return None
    return ceiling
