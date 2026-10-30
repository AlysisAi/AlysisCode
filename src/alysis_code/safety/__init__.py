from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .mcp_sanitize import MAX_MCP_TOOL_DESCRIPTION_CHARS, sanitize_mcp_tool_description

if TYPE_CHECKING:
    from .safe_http import SafeHttpError, safe_http_request


def __getattr__(name: str) -> Any:
    # Text sanitization has no network dependency. Resolve the real HTTP
    # boundary only when a caller requests it; its checks remain unchanged.
    if name in {"SafeHttpError", "safe_http_request"}:
        from . import safe_http

        value = getattr(safe_http, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "MAX_MCP_TOOL_DESCRIPTION_CHARS",
    "SafeHttpError",
    "safe_http_request",
    "sanitize_mcp_tool_description",
]
