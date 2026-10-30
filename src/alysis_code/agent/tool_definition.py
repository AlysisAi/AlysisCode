"""Tool declaration and model schema projection, independent of execution assembly."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..assets.local_tools import LocalAssetViewer
from ..context.tool_schema_budgeter import (
    CUSTOM_MCP_SCHEMA_FAMILIES,
    DEFAULT_CUSTOM_MCP_DESCRIPTION_MAX_CHARS,
    JSON_SCHEMA_NAMED_SCHEMA_MAP_KEYS,
    JSON_SCHEMA_OPAQUE_DATA_KEYS,
    compact_custom_mcp_tool_parameters,
)
from ..mcp.visual_delivery import McpVisualDelivery


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any]], dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)
    # Host-only delivery interface, preserved when dispatch guards replace run.
    # It is deliberately excluded from model schemas and persistence metadata.
    visual_delivery: LocalAssetViewer | McpVisualDelivery | None = field(
        default=None, repr=False, compare=False
    )

    def as_openai_tool(self) -> dict[str, Any]:
        family = _model_schema_family(self.metadata)
        description_max_chars = self.metadata.get("model_description_max_chars")
        if family in CUSTOM_MCP_SCHEMA_FAMILIES:
            description_max_chars = _schema_description_max_chars(description_max_chars)
        description = str(self.metadata.get("model_description") or self.description)
        description = _model_facing_tool_description(
            description,
            max_chars=description_max_chars,
        )
        parameters = self.parameters
        if bool(self.metadata.get("compact_parameters_for_model")):
            if family in CUSTOM_MCP_SCHEMA_FAMILIES:
                parameters = compact_custom_mcp_tool_parameters(self.parameters)
            else:
                parameters = _drop_model_facing_schema_prose(
                    self.parameters, preserve_descriptions=family == "builtin"
                )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": parameters,
            },
        }


_MODEL_FACING_SCHEMA_PROSE_KEYS = frozenset(
    {
        "$comment",
        "description",
        "example",
        "examples",
        "markdownDescription",
        "title",
    }
)


def _model_schema_family(metadata: dict[str, Any]) -> str:
    tool_type = str(metadata.get("tool_type") or "").strip().lower()
    if tool_type == "custom_tool":
        return "custom"
    if tool_type in {"mcp", "mcp_tool"}:
        return "mcp"
    return tool_type


def _schema_description_max_chars(value: Any) -> int:
    try:
        configured = int(value)
    except (TypeError, ValueError):
        configured = 0
    if configured <= 0:
        return DEFAULT_CUSTOM_MCP_DESCRIPTION_MAX_CHARS
    return configured


def _model_facing_tool_description(description: str, *, max_chars: Any) -> str:
    text = " ".join(str(description or "").split())
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _drop_model_facing_schema_prose(
    value: Any,
    *,
    preserve_descriptions: bool = False,
    _inside_named_schema_map: bool = False,
) -> Any:
    """Remove schema annotations without deleting named fields or literal data.

    Built-in parameter descriptions are the concise calling contract. Keep them
    verbatim, including whitespace in quoted examples, rather than abbreviating
    their meaning. Custom/MCP schemas use their existing separate budget policy.
    """
    if isinstance(value, dict):
        reduced: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if _inside_named_schema_map:
                reduced[key] = _drop_model_facing_schema_prose(
                    item, preserve_descriptions=preserve_descriptions
                )
                continue
            if normalized_key in _MODEL_FACING_SCHEMA_PROSE_KEYS and not (
                preserve_descriptions and normalized_key == "description"
            ):
                continue
            if (
                normalized_key in JSON_SCHEMA_OPAQUE_DATA_KEYS
                or normalized_key == "dependentRequired"
            ):
                reduced[key] = copy.deepcopy(item)
                continue
            reduced[key] = _drop_model_facing_schema_prose(
                item,
                preserve_descriptions=preserve_descriptions,
                _inside_named_schema_map=(
                    normalized_key in JSON_SCHEMA_NAMED_SCHEMA_MAP_KEYS
                    or normalized_key == "dependencies"
                ),
            )
        return reduced
    if isinstance(value, list):
        return [
            _drop_model_facing_schema_prose(item, preserve_descriptions=preserve_descriptions)
            for item in value
        ]
    return copy.deepcopy(value)


def _drop_schema_descriptions(value: Any) -> Any:
    return _drop_model_facing_schema_prose(value)
