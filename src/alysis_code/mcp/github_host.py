"""Credential-free desktop routing for the installed official GitHub MCP plugin.

Electron owns discovery, HTTP, account credentials, repository binding and approval.
Only bounded schemas and tool results cross this session's existing host broker.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..extensions.activation import resolve_active_plugins
from ..extensions.bundled import bundled_manifest
from ..extensions.manifest import load_manifest, resolve_manifest_path
from ..extensions.paths import installed_plugin_root
from ..extensions.registry import find_by_id, load_registry
from ..extensions.state import load_global_state, load_project_state
from ..host_actions import HostActionError
from .untrusted_content import build_untrusted_mcp_text_block, reduce_model_facing_tool_schema

GITHUB_PLUGIN_ID = "alysis.github"
GITHUB_MCP_SERVER_ID = "alysis.github/github"
GITHUB_MCP_ACTION = "mcp.github.call"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
GITHUB_MCP_MAX_TOOLS = 32
GITHUB_MCP_MAX_CATALOG_BYTES = 120_000
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def _fail(message: str) -> None:
    raise HostActionError("invalid_github_mcp", message)


def _json_copy(value: Any, *, limit: int) -> Any:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=True)
        if len(encoded.encode("utf-8")) > limit:
            _fail("GitHub MCP payload exceeds its size limit.")
        return json.loads(encoded)
    except (ValueError, TypeError, RecursionError) as exc:
        _fail(f"GitHub MCP payload must be bounded JSON ({type(exc).__name__}).")


def normalize_host_mcp_catalog(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    raw = _json_copy(raw, limit=GITHUB_MCP_MAX_CATALOG_BYTES)
    if (
        isinstance(raw, dict)
        and type(raw.get("protocol_version")) is int
        and raw["protocol_version"] == 2
    ):
        if (
            set(raw) != {"protocol_version", "plugin_id", "server_id", "discovery"}
            or raw["discovery"] != {"version": 1}
            or type(raw["discovery"].get("version")) is not int
        ):
            _fail("GitHub MCP v2 requires only a supported discovery bootstrap.")
        if raw["plugin_id"] != GITHUB_PLUGIN_ID or raw["server_id"] != GITHUB_MCP_SERVER_ID:
            _fail("host_mcp must identify the installed GitHub MCP plugin.")
        return raw
    if not isinstance(raw, dict) or set(raw) != {
        "protocol_version",
        "plugin_id",
        "server_id",
        "tools",
    }:
        _fail("host_mcp must contain protocol_version, plugin_id, server_id and tools.")
    if type(raw["protocol_version"]) is not int or raw["protocol_version"] != 1:
        _fail("host_mcp.protocol_version must be 1.")
    if raw["plugin_id"] != GITHUB_PLUGIN_ID or raw["server_id"] != GITHUB_MCP_SERVER_ID:
        _fail("host_mcp must identify the installed GitHub MCP plugin.")
    tools = raw["tools"]
    if not isinstance(tools, list) or not 1 <= len(tools) <= GITHUB_MCP_MAX_TOOLS:
        _fail("host_mcp.tools must contain 1 to 32 discovered tools.")
    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {
            "name",
            "description",
            "inputSchema",
            "readOnly",
        }:
            _fail("Each host MCP tool requires name, description, inputSchema and readOnly.")
        name = tool["name"]
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in seen:
            _fail("Host MCP tool names must be unique bounded identifiers.")
        seen.add(name)
        if type(tool["readOnly"]) is not bool:
            _fail("Host MCP readOnly must be a boolean selected by the host policy.")
        if not isinstance(tool["description"], str) or len(tool["description"]) > 8192:
            _fail("Host MCP descriptions must be bounded strings.")
        schema = tool["inputSchema"]
        if not isinstance(schema, dict) or schema.get("type") != "object":
            _fail("Host MCP inputSchema must describe an object.")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 64:
            _fail("Host MCP properties must be a bounded object.")
        if {"owner", "repo", "repository"} & set(properties):
            _fail("The host must bind repository target fields before exposing MCP schemas.")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(key, str) or key not in properties for key in required
        ):
            _fail("Host MCP required fields must be declared properties.")
        _json_copy(schema, limit=24_000)
    return raw


def active_host_plugin(
    root: Path, plugin_id: str, server_name: str, url: str
) -> dict[str, Any] | None:
    """Verify installed content and runtime trust; an enabled id alone grants nothing."""
    decision = resolve_active_plugins(repo_root=root, workspace_trust_prompt=None)
    if plugin_id not in decision.enabled_plugin_ids:
        return None
    entry = find_by_id(load_registry(), plugin_id)
    if entry is None:
        return None
    expected = bundled_manifest(entry)
    digest = hashlib.sha256(expected).hexdigest()
    for project, state in ((False, load_global_state()), (True, load_project_state(root))):
        if project and not decision.workspace_trust_granted:
            continue
        record = state.installed.get(plugin_id)
        if record is None or record.trust != "manifest-sha256" or record.source_kind != "bundled":
            continue
        if (
            record.manifest_sha256 != digest
            or record.package_sha256 != digest
            or record.commit != entry.commit
        ):
            continue
        plugin_root = installed_plugin_root(plugin_id, project=project, repo_root=root)
        try:
            manifest_bytes = resolve_manifest_path(plugin_root).read_bytes()
            if manifest_bytes != expected:
                continue
            manifest = load_manifest(plugin_root)
        except (OSError, ValueError):
            continue
        if not any(
            server.id == server_name
            and server.host_managed
            and server.enabled
            and str(server.url) == url
            for server in manifest.components.mcp_server
        ):
            continue
        return {
            "plugin_id": plugin_id,
            "server_id": f"{plugin_id}/{server_name}",
            "manifest_sha256": digest,
            "package_sha256": digest,
            "source_kind": "bundled",
            "scope": "project" if project else "user",
        }
    return None


def active_github_plugin(root: Path) -> dict[str, Any] | None:
    return active_host_plugin(root, GITHUB_PLUGIN_ID, "github", GITHUB_MCP_URL)


def bind_host_mcp_catalog(root: Path, catalog: dict[str, Any] | None) -> dict[str, Any] | None:
    if catalog is None:
        return None
    snapshot = active_github_plugin(root)
    if snapshot is None:
        raise HostActionError(
            "github_plugin_inactive",
            "Install and enable the trusted GitHub plugin before connecting MCP tools.",
        )
    return {**catalog, "plugin": snapshot}


def assert_host_mcp_current(root: Path, bound: dict[str, Any]) -> None:
    if active_github_plugin(root) != bound["plugin"]:
        raise HostActionError(
            "github_plugin_revoked",
            "The GitHub plugin changed or was disabled; reconnect the session.",
        )


def host_mcp_session_payload(bound: dict[str, Any] | None) -> dict[str, Any] | None:
    if bound is None:
        return None
    if bound["protocol_version"] == 2:
        return {
            "protocol_version": 2,
            **bound["plugin"],
            "discovery": {"version": 1},
            "tool_names": [],
        }
    return {
        "protocol_version": 1,
        **bound["plugin"],
        "tool_names": [tool["name"] for tool in bound["tools"]],
    }


def normalize_mcp_call_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    if any(key in raw for key in ("catalog_id", "target_id", "schema_hash")):
        from .github_discovery import CALL, discovery_arguments

        return discovery_arguments(CALL, raw)
    if (
        set(raw) != {"name", "arguments"}
        or not isinstance(raw.get("name"), str)
        or not _TOOL_NAME.fullmatch(raw["name"])
    ):
        _fail("MCP calls require a discovered tool name and arguments object.")
    if not isinstance(raw["arguments"], dict):
        _fail("MCP tool arguments must be an object.")
    if {"owner", "repo", "repository"} & set(raw["arguments"]):
        _fail("MCP tool calls cannot override the host-bound repository.")
    return _json_copy(raw, limit=8 * 1024)


def normalize_mcp_call_result(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) - {"content", "isError", "structuredContent"}:
        _fail("MCP result contains unsupported fields.")
    content = raw.get("content")
    if not isinstance(content, list) or len(content) > 100:
        _fail("MCP result content must be a bounded array.")
    # GitHub's repository tools return text; do not enable media/resource side channels.
    for item in content:
        if (
            not isinstance(item, dict)
            or set(item) - {"type", "text", "annotations"}
            or item.get("type") != "text"
            or not isinstance(item.get("text"), str)
        ):
            _fail("GitHub MCP results must contain text content only.")
    if "isError" in raw and type(raw["isError"]) is not bool:
        _fail("MCP isError must be a boolean.")
    if "structuredContent" in raw and not isinstance(raw["structuredContent"], dict):
        _fail("MCP structuredContent must be an object.")
    return _json_copy(raw, limit=64 * 1024)


def tool_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    return reduce_model_facing_tool_schema(copy.deepcopy(tool["inputSchema"]))


def wrap_mcp_result(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    result = normalize_mcp_call_result(raw)
    # Treat structured content as untrusted text too, instead of promoting its instructions.
    body = json.dumps(result, ensure_ascii=False)
    return {
        "server_id": GITHUB_MCP_SERVER_ID,
        "tool_name": name,
        "is_error": result.get("isError", False),
        "text": build_untrusted_mcp_text_block(
            source_type="tool_result", server_id=GITHUB_MCP_SERVER_ID, source_name=name, text=body
        ),
    }
