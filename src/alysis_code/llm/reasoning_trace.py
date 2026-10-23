"""Provider-specific display decoding, separate from request/replay metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import ReasoningOutput, ReasoningOutputKind


@dataclass(frozen=True)
class CompatibleTraceAdapter:
    fields: tuple[str, ...] = ()
    summaries: bool = False
    reasoning_details: bool = False
    thinking_chunks: bool = False


# These are response formats, not instructions to enable thinking or change effort.
# Unknown compatible endpoints stay passive; a profile can explicitly select a
# documented adapter without guessing from a model name or from answer content.
COMPATIBLE_TRACE_ADAPTERS = {
    "none": CompatibleTraceAdapter(),
    "openai_compat_passive": CompatibleTraceAdapter(summaries=True),
    "openrouter_reasoning": CompatibleTraceAdapter(
        fields=("reasoning", "reasoning_content", "reasoning_text"),
        summaries=True,
        reasoning_details=True,
    ),
    "deepseek_reasoning": CompatibleTraceAdapter(fields=("reasoning_content",)),
    "dashscope_thinking": CompatibleTraceAdapter(fields=("reasoning_content",)),
    "moonshot_reasoning": CompatibleTraceAdapter(fields=("reasoning_content",)),
    "nvidia_reasoning": CompatibleTraceAdapter(fields=("reasoning_content",)),
    "glm_reasoning": CompatibleTraceAdapter(fields=("reasoning_content",)),
    "mistral_thinking": CompatibleTraceAdapter(thinking_chunks=True),
}


def _thinking_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    pieces = []
    for chunk in content:
        if not isinstance(chunk, dict) or chunk.get("type") != "thinking":
            continue
        thinking = chunk.get("thinking")
        if isinstance(thinking, str):
            pieces.append(thinking)
        elif isinstance(thinking, list):
            pieces.extend(
                part["text"]
                for part in thinking
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
    return "".join(pieces)


class CompatibleReasoningTrace:
    """Normalize one response's reasoning deltas without mutating wire data.

    Some gateways duplicate reasoning in both a string field and structured
    details. Prefer a structured summary, then structured reasoning within a
    packet, then keep the first channel used to avoid displaying both copies. Opaque
    details never count as a text channel. Repeated words remain valid deltas;
    cumulative snapshots are deduplicated only when that mode is configured.
    """

    def __init__(self, adapter: str, *, provider: str | None, cumulative: bool = False) -> None:
        self.adapter = COMPATIBLE_TRACE_ADAPTERS.get(adapter, CompatibleTraceAdapter())
        self.provider = provider
        self.cumulative = cumulative
        self._channel: str | None = None
        self._parts: dict[tuple[ReasoningOutputKind, str], str] = {}

    def feed(self, message: dict[str, Any]) -> tuple[ReasoningOutput, ...]:
        parts: list[tuple[ReasoningOutputKind, str, str]] = []
        channel = "details"
        details = message.get("reasoning_details")
        if isinstance(details, list):
            for position, detail in enumerate(details):
                if not isinstance(detail, dict):
                    continue
                kind = detail.get("type")
                if kind == "reasoning.summary" and self.adapter.summaries:
                    text = detail.get("summary")
                    output_kind = ReasoningOutputKind.SUMMARY
                elif kind == "reasoning.text" and self.adapter.reasoning_details:
                    text = detail.get("text")
                    output_kind = ReasoningOutputKind.PROVIDER_REASONING
                else:
                    continue
                if not isinstance(text, str) or not text:
                    continue
                if detail.get("id"):
                    key = f"id:{detail['id']}"
                elif detail.get("index") is not None:
                    key = f"index:{detail['index']}"
                else:
                    key = f"position:{position}"
                parts.append((output_kind, key, text))

        if parts:
            # Gateways may include both a summary and the underlying reasoning.
            # Choose one representation rather than concatenating the two.
            preferred = (
                ReasoningOutputKind.SUMMARY
                if any(kind == ReasoningOutputKind.SUMMARY for kind, _key, _text in parts)
                else ReasoningOutputKind.PROVIDER_REASONING
            )
            parts = [part for part in parts if part[0] == preferred]
            channel = f"details:{preferred}"

        if not parts and self.adapter.thinking_chunks:
            text = _thinking_text(message.get("content"))
            if text:
                channel = "thinking"
                parts.append((ReasoningOutputKind.PROVIDER_REASONING, "thinking", text))

        if not parts:
            for field in self.adapter.fields:
                text = message.get(field)
                if isinstance(text, str) and text:
                    channel = "field"
                    parts.append((ReasoningOutputKind.PROVIDER_REASONING, "field", text))
                    break

        if not parts or (self._channel is not None and channel != self._channel):
            return ()
        self._channel = channel
        deltas = []
        for kind, key, incoming in parts:
            identity = (kind, key)
            previous = self._parts.get(identity, "")
            text = incoming
            if self.cumulative:
                if incoming.startswith(previous):
                    text = incoming[len(previous) :]
                elif previous.startswith(incoming):
                    text = ""
            if not text:
                continue
            self._parts[identity] = previous + text
            deltas.append(ReasoningOutput(text=text, kind=kind, provider=self.provider))
        return tuple(deltas)

    @property
    def outputs(self) -> tuple[ReasoningOutput, ...]:
        return tuple(
            ReasoningOutput(text=text, kind=kind, provider=self.provider)
            for (kind, _key), text in self._parts.items()
            if text.strip()
        )
