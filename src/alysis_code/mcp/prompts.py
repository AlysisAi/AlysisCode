from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..runtime_kind import RuntimeKind, normalize_runtime_kind
from .errors import McpProtocolError
from .models import PromptsMode
from .untrusted_content import build_untrusted_mcp_text_block

_PROMPT_ENABLED_RUNTIME_KINDS = frozenset(
    {
        RuntimeKind.INTERACTIVE_CHAT,
        RuntimeKind.ONE_SHOT,
    }
)
_INLINE_PROMPT_TEXT_CHAR_LIMIT = 12_000


class McpPromptNormalizationError(ValueError, McpProtocolError):
    def __init__(
        self,
        message: str = "",
        *,
        server_id: str | None = None,
        method: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.server_id = server_id
        self.method = method
        self.cause = cause


@dataclass(frozen=True)
class McpPromptArgument:
    name: str
    description: str | None
    required: bool
    raw_payload: dict[str, Any] = field(repr=False)

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "required": self.required,
        }
        if self.description:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True)
class McpListedPrompt:
    name: str
    title: str | None
    description: str | None
    arguments: tuple[McpPromptArgument, ...]
    raw_payload: dict[str, Any] = field(repr=False)

    def as_payload(self, *, server_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "server_id": server_id,
            "name": self.name,
            "arguments": [argument.as_payload() for argument in self.arguments],
        }
        if self.title:
            payload["title"] = self.title
        if self.description:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True)
class McpPromptMessage:
    role: str
    content: tuple[dict[str, Any], ...]
    content_summary: str
    text: str | None = None
    raw_payload: dict[str, Any] = field(repr=False, default_factory=dict)

    def as_payload(self, *, server_id: str, prompt_name: str) -> dict[str, Any]:
        content_items: list[dict[str, Any]] = []
        for item in self.content:
            payload_item = dict(item)
            item_text = payload_item.get("text")
            mime_type = payload_item.get("mime_type")
            if isinstance(item_text, str):
                payload_item["text"] = build_untrusted_mcp_text_block(
                    source_type="prompt_get",
                    server_id=server_id,
                    source_name=prompt_name,
                    text=item_text,
                    mime_type=mime_type if isinstance(mime_type, str) else None,
                )
            content_items.append(payload_item)
        payload: dict[str, Any] = {
            "role": self.role,
            "content_summary": self.content_summary,
            "content": content_items,
        }
        if self.text is not None:
            payload["text"] = build_untrusted_mcp_text_block(
                source_type="prompt_get",
                server_id=server_id,
                source_name=prompt_name,
                text=self.text,
            )
        return payload


@dataclass(frozen=True)
class McpGetPromptResult:
    description: str | None
    messages: tuple[McpPromptMessage, ...]
    content_summary: str
    text: str
    raw_payload: dict[str, Any] = field(repr=False)


def prompts_runtime_supported(runtime_kind: RuntimeKind | str) -> bool:
    return normalize_runtime_kind(runtime_kind) in _PROMPT_ENABLED_RUNTIME_KINDS


def prompts_mode_enabled(
    *,
    prompts_mode: PromptsMode,
    runtime_kind: RuntimeKind | str,
) -> bool:
    return prompts_mode == "listed_get_only" and prompts_runtime_supported(runtime_kind)


def _require_object(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpPromptNormalizationError(f"{context} must be an object.")
    return value


def _require_string(value: Any, *, context: str) -> str:
    if not isinstance(value, str):
        raise McpPromptNormalizationError(f"{context} must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise McpPromptNormalizationError(f"{context} cannot be empty.")
    return cleaned


def _optional_string(value: Any, *, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise McpPromptNormalizationError(f"{context} must be a string when present.")
    cleaned = value.strip()
    return cleaned or None


def _normalize_prompt_arguments(value: Any, *, context: str) -> tuple[McpPromptArgument, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise McpPromptNormalizationError(f"{context} must be an array when present.")
    normalized: list[McpPromptArgument] = []
    seen_names: set[str] = set()
    for index, raw_item in enumerate(value):
        payload = _require_object(raw_item, context=f"{context}[{index}]")
        name = _require_string(payload.get("name"), context=f"{context}[{index}].name")
        name_key = name.casefold()
        if name_key in seen_names:
            raise McpPromptNormalizationError(
                f"{context} contains repeated argument name {name!r}."
            )
        seen_names.add(name_key)
        required = payload.get("required", False)
        if not isinstance(required, bool):
            raise McpPromptNormalizationError(
                f"{context}[{index}].required must be a boolean when present."
            )
        normalized.append(
            McpPromptArgument(
                name=name,
                description=_optional_string(
                    payload.get("description"),
                    context=f"{context}[{index}].description",
                ),
                required=required,
                raw_payload=dict(payload),
            )
        )
    return tuple(normalized)


def _normalize_prompt_content_items(
    value: Any, *, context: str
) -> tuple[tuple[dict[str, Any], ...], str]:
    raw_items = value if isinstance(value, list) else [value]
    normalized: list[dict[str, Any]] = []
    summaries: list[str] = []
    for index, raw_item in enumerate(raw_items):
        payload = _require_object(raw_item, context=f"{context}[{index}]")
        item_type = _require_string(payload.get("type"), context=f"{context}[{index}].type")
        normalized_item: dict[str, Any] = {"type": item_type}
        summary = f"{item_type} item omitted"
        mime_type = _optional_string(
            payload.get("mimeType"),
            context=f"{context}[{index}].mimeType",
        )
        if mime_type:
            normalized_item["mime_type"] = mime_type
            summary = f"{mime_type} {summary}"
        if item_type == "text":
            text = _require_string(payload.get("text"), context=f"{context}[{index}].text")
            inline_text = text
            truncated = False
            if len(inline_text) > _INLINE_PROMPT_TEXT_CHAR_LIMIT:
                inline_text = inline_text[:_INLINE_PROMPT_TEXT_CHAR_LIMIT]
                truncated = True
            summary = f"text({len(text)} chars)"
            if mime_type:
                summary = f"{mime_type} {summary}"
            if truncated:
                summary += f", truncated to {_INLINE_PROMPT_TEXT_CHAR_LIMIT} chars"
            normalized_item["text"] = inline_text
        else:
            uri = _optional_string(payload.get("uri"), context=f"{context}[{index}].uri")
            if uri:
                normalized_item["uri"] = uri
            resource = payload.get("resource")
            if isinstance(resource, dict):
                resource_uri = _optional_string(
                    resource.get("uri"),
                    context=f"{context}[{index}].resource.uri",
                )
                if resource_uri:
                    normalized_item["resource_uri"] = resource_uri
        normalized_item["summary"] = summary
        normalized.append(normalized_item)
        summaries.append(summary)
    return tuple(normalized), ", ".join(summaries) or "no content"


def normalize_list_prompts_result(
    result: dict[str, Any],
) -> tuple[tuple[McpListedPrompt, ...], str | None]:
    prompts = result.get("prompts")
    if not isinstance(prompts, list):
        raise McpPromptNormalizationError("prompts/list result.prompts must be an array.")
    normalized: list[McpListedPrompt] = []
    for index, raw_item in enumerate(prompts):
        payload = _require_object(raw_item, context=f"prompts/list prompt[{index}]")
        normalized.append(
            McpListedPrompt(
                name=_require_string(
                    payload.get("name"), context=f"prompts/list prompt[{index}].name"
                ),
                title=_optional_string(
                    payload.get("title"),
                    context=f"prompts/list prompt[{index}].title",
                ),
                description=_optional_string(
                    payload.get("description"),
                    context=f"prompts/list prompt[{index}].description",
                ),
                arguments=_normalize_prompt_arguments(
                    payload.get("arguments"),
                    context=f"prompts/list prompt[{index}].arguments",
                ),
                raw_payload=dict(payload),
            )
        )
    next_cursor = result.get("nextCursor")
    if next_cursor is None:
        return tuple(normalized), None
    return tuple(normalized), _require_string(next_cursor, context="prompts/list nextCursor")


def normalize_get_prompt_result(
    result: dict[str, Any],
    *,
    expected_name: str,
) -> McpGetPromptResult:
    if "name" in result:
        result_name = _require_string(result.get("name"), context="prompts/get result.name")
        if result_name != expected_name:
            raise McpPromptNormalizationError(
                "prompts/get result.name must match the requested prompt name."
            )
    messages = result.get("messages")
    if not isinstance(messages, list):
        raise McpPromptNormalizationError("prompts/get result.messages must be an array.")
    normalized_messages: list[McpPromptMessage] = []
    text_parts: list[str] = []
    summaries: list[str] = []
    for index, raw_item in enumerate(messages):
        payload = _require_object(raw_item, context=f"prompts/get messages[{index}]")
        role = _require_string(payload.get("role"), context=f"prompts/get messages[{index}].role")
        content_items, content_summary = _normalize_prompt_content_items(
            payload.get("content"),
            context=f"prompts/get messages[{index}].content",
        )
        text_parts.extend(
            item["text"]
            for item in content_items
            if isinstance(item.get("text"), str) and str(item.get("text")).strip()
        )
        summaries.append(f"{role}: {content_summary}")
        normalized_messages.append(
            McpPromptMessage(
                role=role,
                content=content_items,
                content_summary=content_summary,
                text="\n\n".join(
                    item["text"]
                    for item in content_items
                    if isinstance(item.get("text"), str) and str(item.get("text")).strip()
                )
                or None,
                raw_payload=dict(payload),
            )
        )
    return McpGetPromptResult(
        description=_optional_string(
            result.get("description"),
            context="prompts/get result.description",
        ),
        messages=tuple(normalized_messages),
        content_summary="; ".join(summaries) or "no messages",
        text="\n\n".join(text_parts),
        raw_payload=dict(result),
    )
