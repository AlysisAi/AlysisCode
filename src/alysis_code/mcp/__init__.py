from importlib import import_module
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


# A bridge frame uses this package's policy parser without starting any MCP client.
_EXPORT_MODULES = {
    "MCP_PROTOCOL_VERSION": "client",
    "McpHttpClient": "client",
    "McpManager": "manager",
    "McpStdioClient": "client",
    "McpToolBinding": "manager",
    "ResolvedMcpConfig": "models",
    "ResolvedMcpServer": "models",
    "create_mcp_manager": "manager",
    "load_resolved_mcp_config": "config",
    "project_mcp_config_path": "config",
    "redact_sensitive_mapping": "config",
    "user_mcp_config_path": "config",
}


def __getattr__(name: str) -> Any:
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
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
