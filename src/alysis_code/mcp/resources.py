from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..runtime_kind import RuntimeKind, normalize_runtime_kind
from .errors import McpProtocolError
from .models import ResourcesMode
from .untrusted_content import _looks_like_binary_blob, build_untrusted_mcp_text_block

_RESOURCE_ENABLED_RUNTIME_KINDS = frozenset(
    {
        RuntimeKind.INTERACTIVE_CHAT,
        RuntimeKind.ONE_SHOT,
    }
)
_INLINE_RESOURCE_TEXT_CHAR_LIMIT = 12_000


class McpResourceNormalizationError(ValueError, McpProtocolError):
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
class McpListedResource:
    uri: str
    name: str
    description: str | None
    mime_type: str | None
    size: int | None
    raw_payload: dict[str, Any] = field(repr=False)

    def as_tool_payload(self, *, server_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "server_id": server_id,
            "uri": self.uri,
            "name": self.name,
        }
        if self.description:
            payload["description"] = self.description
        if self.mime_type:
            payload["mime_type"] = self.mime_type
        if self.size is not None:
            payload["size"] = self.size
        return payload


@dataclass(frozen=True)
class McpReadResourceContent:
    uri: str
    mime_type: str | None
    content_summary: str
    text: str | None = None
    raw_payload: dict[str, Any] = field(repr=False, default_factory=dict)

    def as_tool_payload(self, *, server_id: str, resource_uri: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "uri": self.uri,
            "content_summary": self.content_summary,
        }
        if self.mime_type:
            payload["mime_type"] = self.mime_type
        if self.text is not None:
            payload["text"] = build_untrusted_mcp_text_block(
                source_type="resource_read",
                server_id=server_id,
                source_name=resource_uri,
                text=self.text,
                mime_type=self.mime_type,
            )
        return payload


@dataclass(frozen=True)
class McpReadResourceResult:
    contents: tuple[McpReadResourceContent, ...]
    content_summary: str
    text: str
    mime_type: str | None
    raw_payload: dict[str, Any] = field(repr=False)


def resources_runtime_supported(runtime_kind: RuntimeKind | str) -> bool:
    return normalize_runtime_kind(runtime_kind) in _RESOURCE_ENABLED_RUNTIME_KINDS


def resources_mode_enabled(
    *,
    resources_mode: ResourcesMode,
    runtime_kind: RuntimeKind | str,
) -> bool:
    return resources_mode == "listed_read_only" and resources_runtime_supported(runtime_kind)


def _require_object(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpResourceNormalizationError(f"{context} must be an object.")
    return value


def _require_string(value: Any, *, context: str) -> str:
    if not isinstance(value, str):
        raise McpResourceNormalizationError(f"{context} must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise McpResourceNormalizationError(f"{context} cannot be empty.")
    return cleaned


def _optional_string(value: Any, *, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise McpResourceNormalizationError(f"{context} must be a string when present.")
    cleaned = value.strip()
    return cleaned or None


def _optional_size(value: Any, *, context: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise McpResourceNormalizationError(
            f"{context} must be a non-negative integer when present."
        )
    if value < 0:
        raise McpResourceNormalizationError(f"{context} must be >= 0 when present.")
    return int(value)


def _mime_is_textual(mime_type: str | None) -> bool:
    if mime_type is None:
        return True
    lowered = mime_type.strip().lower()
    if not lowered:
        return True
    if lowered.startswith("text/"):
        return True
    return (
        lowered == "application/json"
        or lowered.endswith("+json")
        or lowered == "application/xml"
        or lowered.endswith("+xml")
        or lowered in {"application/yaml", "application/x-yaml", "text/yaml"}
        or lowered.endswith("+yaml")
        or lowered in {"application/javascript", "application/x-javascript"}
    )


def _inline_text_payload(
    *,
    text: str,
    mime_type: str | None,
) -> tuple[str | None, str]:
    if not _mime_is_textual(mime_type):
        mime_label = mime_type or "non-text"
        return None, f"{mime_label} text omitted ({len(text)} chars)"
    if _looks_like_binary_blob(text):
        return None, f"binary-like text omitted ({len(text)} chars)"
    inline_text = text
    truncated = False
    if len(inline_text) > _INLINE_RESOURCE_TEXT_CHAR_LIMIT:
        inline_text = inline_text[:_INLINE_RESOURCE_TEXT_CHAR_LIMIT]
        truncated = True
    summary_kind = "json text" if mime_type and "json" in mime_type.lower() else "text"
    summary = f"{summary_kind}({len(text)} chars)"
    if mime_type:
        summary = f"{mime_type} {summary}"
    if truncated:
        summary += f", truncated to {_INLINE_RESOURCE_TEXT_CHAR_LIMIT} chars"
    return inline_text, summary


def normalize_list_resources_result(
    result: dict[str, Any],
) -> tuple[tuple[McpListedResource, ...], str | None]:
    resources = result.get("resources")
    if not isinstance(resources, list):
        raise McpResourceNormalizationError("resources/list result.resources must be an array.")
    normalized: list[McpListedResource] = []
    for index, raw_item in enumerate(resources):
        payload = _require_object(raw_item, context=f"resources/list resource[{index}]")
        normalized.append(
            McpListedResource(
                uri=_require_string(
                    payload.get("uri"), context=f"resources/list resource[{index}].uri"
                ),
                name=_require_string(
                    payload.get("name"), context=f"resources/list resource[{index}].name"
                ),
                description=_optional_string(
                    payload.get("description"),
                    context=f"resources/list resource[{index}].description",
                ),
                mime_type=_optional_string(
                    payload.get("mimeType"),
                    context=f"resources/list resource[{index}].mimeType",
                ),
                size=_optional_size(
                    payload.get("size"),
                    context=f"resources/list resource[{index}].size",
                ),
                raw_payload=dict(payload),
            )
        )
    next_cursor = result.get("nextCursor")
    if next_cursor is None:
        return tuple(normalized), None
    return tuple(normalized), _require_string(next_cursor, context="resources/list nextCursor")


def normalize_read_resource_result(
    result: dict[str, Any],
    *,
    expected_uri: str,
) -> McpReadResourceResult:
    contents = result.get("contents")
    if not isinstance(contents, list):
        raise McpResourceNormalizationError("resources/read result.contents must be an array.")
    normalized: list[McpReadResourceContent] = []
    text_parts: list[str] = []
    for index, raw_item in enumerate(contents):
        payload = _require_object(raw_item, context=f"resources/read contents[{index}]")
        item_uri = _optional_string(
            payload.get("uri"), context=f"resources/read contents[{index}].uri"
        )
        resolved_uri = item_uri or expected_uri
        if resolved_uri != expected_uri:
            raise McpResourceNormalizationError(
                f"resources/read contents[{index}].uri must match the requested resource URI."
            )
        mime_type = _optional_string(
            payload.get("mimeType"),
            context=f"resources/read contents[{index}].mimeType",
        )
        has_text = "text" in payload
        has_blob = "blob" in payload
        if has_text and has_blob:
            raise McpResourceNormalizationError(
                f"resources/read contents[{index}] cannot include both text and blob."
            )
        if not has_text and not has_blob:
            raise McpResourceNormalizationError(
                f"resources/read contents[{index}] must include text or blob."
            )
        inline_text: str | None = None
        content_summary: str
        if has_text:
            raw_text = payload.get("text")
            if not isinstance(raw_text, str):
                raise McpResourceNormalizationError(
                    f"resources/read contents[{index}].text must be a string."
                )
            inline_text, content_summary = _inline_text_payload(text=raw_text, mime_type=mime_type)
            if inline_text is not None:
                text_parts.append(inline_text)
        else:
            raw_blob = payload.get("blob")
            if not isinstance(raw_blob, str):
                raise McpResourceNormalizationError(
                    f"resources/read contents[{index}].blob must be a string."
                )
            blob_summary = f"blob omitted ({len(raw_blob)} chars)"
            if mime_type:
                blob_summary = f"{mime_type} {blob_summary}"
            content_summary = blob_summary
        normalized.append(
            McpReadResourceContent(
                uri=resolved_uri,
                mime_type=mime_type,
                content_summary=content_summary,
                text=inline_text,
                raw_payload=dict(payload),
            )
        )
    mime_type = next((item.mime_type for item in normalized if item.mime_type), None)
    return McpReadResourceResult(
        contents=tuple(normalized),
        content_summary=", ".join(item.content_summary for item in normalized) or "no content",
        text="\n\n".join(text_parts),
        mime_type=mime_type,
        raw_payload=dict(result),
    )
