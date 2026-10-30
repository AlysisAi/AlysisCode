from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import MCP_PROTOCOL_VERSION, McpHttpClient, McpStdioClient

from .config import (
    load_resolved_mcp_config,
    project_mcp_config_path,
    redact_sensitive_mapping,
    user_mcp_config_path,
)
from .manager import McpManager, McpToolBinding, create_mcp_manager
from .models import ResolvedMcpConfig, ResolvedMcpServer


def __getattr__(name: str) -> Any:
    if name not in {"MCP_PROTOCOL_VERSION", "McpHttpClient", "McpStdioClient"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import client

    value = getattr(client, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "McpHttpClient",
    "McpManager",
    "McpStdioClient",
    "McpToolBinding",
    "ResolvedMcpConfig",
    "ResolvedMcpServer",
    "create_mcp_manager",
    "load_resolved_mcp_config",
    "project_mcp_config_path",
    "redact_sensitive_mapping",
    "user_mcp_config_path",
]
