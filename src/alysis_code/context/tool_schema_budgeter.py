from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..token_budget import estimate_tokens

DEFAULT_TOOL_SCHEMA_BUDGET_TOKENS = 8_000
DEFAULT_LARGEST_TOOL_COUNT = 5
DEFAULT_CUSTOM_MCP_DESCRIPTION_MAX_CHARS = 800

CUSTOM_MCP_SCHEMA_FAMILIES = frozenset({"custom", "custom_tool", "mcp", "mcp_tool"})
MODEL_FACING_SCHEMA_ANNOTATION_KEYS = frozenset(
    {
        "$comment",
        "default",
        "description",
        "example",
        "examples",
        "markdownDescription",
        "title",
    }
)
JSON_SCHEMA_NAMED_SCHEMA_MAP_KEYS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
        "properties",
    }
)
JSON_SCHEMA_OPAQUE_DATA_KEYS = frozenset(
    {
        "const",
        "default",
        "enum",
    }
)


@dataclass(frozen=True)
class ToolSchemaBudgetEntry:
    name: str
    token_estimate: int
    family: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "token_estimate": self.token_estimate,
            "family": self.family,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ToolSchemaBudgetEntry | None:
        if not isinstance(payload, dict):
            return None
        name = str(payload.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            token_estimate=_as_non_negative_int(payload.get("token_estimate")),
            family=_tool_family(name, payload.get("family")),
        )


@dataclass(frozen=True)
class ToolSchemaBudgetReport:
    tool_count: int
    total_tokens: int
    budget_tokens: int
    over_budget_tokens: int
    signature: str
    largest_tools: tuple[ToolSchemaBudgetEntry, ...] = ()

    @property
    def over_budget(self) -> bool:
        return self.over_budget_tokens > 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool_count": self.tool_count,
            "total_tokens": self.total_tokens,
            "budget_tokens": self.budget_tokens,
            "over_budget_tokens": self.over_budget_tokens,
            "over_budget": self.over_budget,
            "signature": self.signature,
            "largest_tools": [entry.to_payload() for entry in self.largest_tools],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ToolSchemaBudgetReport | None:
        if not isinstance(payload, dict):
            return None
        signature = str(payload.get("signature") or "").strip()
        entries: list[ToolSchemaBudgetEntry] = []
        raw_entries = payload.get("largest_tools")
        if isinstance(raw_entries, list):
            for raw_entry in raw_entries:
                entry = ToolSchemaBudgetEntry.from_payload(raw_entry)
                if entry is not None:
                    entries.append(entry)
        return cls(
            tool_count=_as_non_negative_int(payload.get("tool_count")),
            total_tokens=_as_non_negative_int(payload.get("total_tokens")),
            budget_tokens=_as_non_negative_int(payload.get("budget_tokens")),
            over_budget_tokens=_as_non_negative_int(payload.get("over_budget_tokens")),
            signature=signature,
            largest_tools=tuple(entries),
        )


def tool_schema_token_total(tool_list: list[dict[str, Any]] | None) -> int:
    """Count the complete request array without constructing a diagnostic ranking."""
    tools = [tool for tool in (tool_list or []) if isinstance(tool, dict)]
    return estimate_tokens(_stable_json(tools)) if tools else 0


def analyze_tool_schema_budget(
    tool_list: list[dict[str, Any]] | None,
    *,
    budget_tokens: int = DEFAULT_TOOL_SCHEMA_BUDGET_TOKENS,
    largest_count: int = DEFAULT_LARGEST_TOOL_COUNT,
) -> ToolSchemaBudgetReport:
    tools = [tool for tool in (tool_list or []) if isinstance(tool, dict)]
    entries: list[ToolSchemaBudgetEntry] = []
    for index, tool in enumerate(tools, start=1):
        name = _tool_name(tool, fallback=f"tool_{index}")
        entries.append(
            ToolSchemaBudgetEntry(
                name=name,
                token_estimate=_estimate_single_tool_schema_tokens(tool),
                family=_tool_family(name, None),
            )
        )
    total = tool_schema_token_total(tools)
    budget = max(0, int(budget_tokens or 0))
    largest_n = max(0, int(largest_count or 0))
    largest_tools = tuple(
        sorted(entries, key=lambda entry: (-entry.token_estimate, entry.name.casefold()))[
            :largest_n
        ]
    )
    return ToolSchemaBudgetReport(
        tool_count=len(tools),
        total_tokens=total,
        budget_tokens=budget,
        over_budget_tokens=max(0, total - budget) if budget > 0 else 0,
        signature=tool_schema_signature(tools),
        largest_tools=largest_tools,
    )


def tool_schema_signature(tool_list: list[dict[str, Any]] | None) -> str:
    payload = _stable_json([tool for tool in (tool_list or []) if isinstance(tool, dict)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def compact_custom_mcp_tool_parameters(
    value: Any,
    *,
    _inside_named_schema_map: bool = False,
) -> Any:
    """Drop annotation-only JSON Schema prose from custom/MCP model-facing schemas."""
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if not _inside_named_schema_map:
                if normalized_key in MODEL_FACING_SCHEMA_ANNOTATION_KEYS:
                    continue
                if normalized_key in JSON_SCHEMA_OPAQUE_DATA_KEYS:
                    # enum/const/default hold constraint data, not subschemas.
                    compacted[key] = copy.deepcopy(item)
                    continue
            # Only a schema keyword opens a name map. Every entry of a name map is a
            # schema again, even one named "properties" or "$defs", so its annotations
            # are dropped and its enum/const literals are copied unchanged.
            compacted[key] = compact_custom_mcp_tool_parameters(
                item,
                _inside_named_schema_map=not _inside_named_schema_map
                and normalized_key in JSON_SCHEMA_NAMED_SCHEMA_MAP_KEYS,
            )
        return compacted
    if isinstance(value, list):
        return [compact_custom_mcp_tool_parameters(item) for item in value]
    return value


def _estimate_single_tool_schema_tokens(tool: dict[str, Any]) -> int:
    return estimate_tokens(_stable_json(tool))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tool_name(tool: dict[str, Any], *, fallback: str) -> str:
    function = tool.get("function")
    if isinstance(function, dict):
        name = str(function.get("name") or "").strip()
        if name:
            return name
    name = str(tool.get("name") or "").strip()
    return name or fallback


def _tool_family(name: str, raw_family: Any) -> str:
    family = str(raw_family or "").strip().lower()
    if family:
        return family
    normalized = str(name or "").strip().lower()
    if normalized.startswith("mcp__") or normalized.startswith("mcp_"):
        return "mcp"
    if normalized.startswith("custom__") or normalized.startswith("custom_"):
        return "custom"
    return "builtin"


def _as_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)
