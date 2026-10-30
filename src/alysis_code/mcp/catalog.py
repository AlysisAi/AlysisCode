"""MCP catalog values that do not require a live transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class McpListedTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    raw_payload: dict[str, Any] = field(repr=False)
