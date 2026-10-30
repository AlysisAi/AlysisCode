"""The user's own online plugins (Plugins M2d): host-owned remote MCP, like Google.

The desktop app keeps the login and calls the server; credentials never enter the engine.
A plugin is a ``custom.*`` descriptor the person approved: one HTTPS endpoint and the tools the
person picked, each with the person's own "only reads" mark. A server's readOnlyHint never
counts: every call to an unmarked tool asks a person, in every mode.

Verified plugins (``alysis.notion``, ...) use the same host channel: a vendor's official server
in a content-pinned bundled descriptor, whose tool list and "only reads" marks Alysis reviewed.
Only the reviewed tools bind, and arguments the app fills in (host arguments) never come from
the AI.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..extensions.activation import ActivationDecision, resolve_active_plugins
from ..extensions.bundled import bundled_manifest, parse_bundled_manifest
from ..extensions.manifest import (
    DEFAULT_CONNECTOR_ARGUMENT_BYTES,
    MAX_CUSTOM_TOOLS,
    MAX_VERIFIED_ARGUMENT_BYTES,
    MAX_VERIFIED_TOOLS,
    PluginManifest,
    custom_tool_alias,
    load_manifest,
    resolve_manifest_path,
    verified_tool_alias,
)
from ..extensions.models import ProjectExtensionOverrides
from ..extensions.paths import installed_plugin_root
from ..extensions.registry import find_by_id, load_registry
from ..extensions.state import compute_effective_enabled, load_global_state
from ..host_actions import HostActionError, json_size_bytes
from .github_host import normalize_mcp_call_result, verified_bundled_plugin
from .untrusted_content import build_untrusted_mcp_text_block

CONNECTOR_MCP_ACTION = "mcp.connector.call"
MAX_CONNECTOR_PLUGINS = 16
#: The verified plugins this engine can bind (``custom_connector_mcp.verified_plugins``).
VERIFIED_CONNECTOR_PLUGIN_IDS = (
    "alysis.notion",
    "alysis.linear",
    "alysis.sentry",
    "alysis.atlassian",
    "alysis.supabase",
    "alysis.gitlab",
    "alysis.huggingface",
    "alysis.clickup",
    "alysis.todoist",
    "alysis.airtable",
)
_PLUGIN_ID = re.compile(r"custom\.[a-z][a-z0-9_]{0,19}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _fail(message: str) -> None:
    raise HostActionError("invalid_connector_mcp", message)


def _copy(raw: Any, limit: int) -> Any:
    try:
        data = json.dumps(raw, ensure_ascii=True, allow_nan=False)
        if len(data.encode()) > limit:
            _fail("Custom plugin payload exceeds its size limit.")
        return json.loads(data)
    except (ValueError, TypeError, RecursionError):
        _fail("Custom plugins require bounded JSON.")


def is_verified_connector(plugin_id: Any) -> bool:
    return isinstance(plugin_id, str) and plugin_id in VERIFIED_CONNECTOR_PLUGIN_IDS


def _known_plugin_id(plugin_id: Any) -> bool:
    return is_verified_connector(plugin_id) or (
        isinstance(plugin_id, str) and _PLUGIN_ID.fullmatch(plugin_id) is not None
    )


def connector_tool_prefix(plugin_id: str) -> str:
    """``mcp__custom_<name>__`` for the user's own plugins, ``mcp__<name>__`` for verified ones.

    A user-configured tool server whose names start the same way can't take these tools.
    """
    short = plugin_id.split(".", 1)[1]
    return f"mcp__{short}__" if is_verified_connector(plugin_id) else f"mcp__custom_{short}__"


def connector_tool_name(plugin_id: str, tool_name: str) -> str:
    """The model-facing name: ``mcp__custom_<name>__<tool>``, at most 64 characters."""
    if is_verified_connector(plugin_id):
        return connector_tool_prefix(plugin_id) + verified_tool_alias(plugin_id, tool_name)
    return f"mcp__custom_{plugin_id.split('.', 1)[1]}__{custom_tool_alias(tool_name)}"


def normalize_connector_catalogs(raw: Any) -> list[dict[str, Any]]:
    """The app's catalogs: identity, the descriptor hash it holds, and the discovered tools."""
    if raw is None:
        return []
    raw = _copy(raw, 1_600_000)
    if not isinstance(raw, list) or len(raw) > MAX_CONNECTOR_PLUGINS:
        _fail(f"connector_mcp must contain at most {MAX_CONNECTOR_PLUGINS} plugin catalogs.")
    seen: set[str] = set()
    for catalog in raw:
        if not isinstance(catalog, dict) or set(catalog) != {
            "protocol_version",
            "plugin_id",
            "server_id",
            "descriptor_sha256",
            "tools",
        }:
            _fail("Invalid custom plugin catalog fields.")
        plugin_id = catalog["plugin_id"]
        if not _known_plugin_id(plugin_id):
            _fail("Custom plugin ids are custom.<name>.")
        if plugin_id in seen:
            _fail("Duplicate custom plugin catalog.")
        seen.add(plugin_id)
        if (
            type(catalog["protocol_version"]) is not int
            or catalog["protocol_version"] != 1
            or not isinstance(catalog["server_id"], str)
            or not catalog["server_id"].startswith(f"{plugin_id}/")
            or not isinstance(catalog["descriptor_sha256"], str)
            or not _SHA256.fullmatch(catalog["descriptor_sha256"])
        ):
            _fail("Custom plugin catalog identity mismatch.")
        most = MAX_VERIFIED_TOOLS if is_verified_connector(plugin_id) else MAX_CUSTOM_TOOLS
        if not isinstance(catalog["tools"], list) or not 1 <= len(catalog["tools"]) <= most:
            _fail(f"Each custom plugin requires 1 to {most} tools.")
        names: set[str] = set()
        for tool in catalog["tools"]:
            # No readOnly here: the engine takes the person's marks from the descriptor.
            if not isinstance(tool, dict) or set(tool) != {"name", "description", "inputSchema"}:
                _fail("Invalid custom plugin tool fields.")
            name = tool["name"]
            if not isinstance(name, str) or not name or len(name) > 64 or name in names:
                _fail("Invalid or duplicate custom plugin tool name.")
            names.add(name)
            if not isinstance(tool["description"], str) or len(tool["description"]) > 8192:
                _fail("Invalid custom plugin tool description.")
            schema = tool["inputSchema"]
            if (
                not isinstance(schema, dict)
                or schema.get("type") != "object"
                or not isinstance(schema.get("properties", {}), dict)
            ):
                _fail("Custom plugin input schemas must describe objects.")
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if (
                len(properties) > 64
                or not isinstance(required, list)
                or any(not isinstance(k, str) or k not in properties for k in required)
            ):
                _fail("Invalid custom plugin schema properties.")
            _copy(schema, 24_000)
    return raw


def verified_custom_plugin(
    root: Path, plugin_id: str
) -> tuple[dict[str, Any], PluginManifest] | None:
    """Installed by the person (user scope), on, intact, and still the approved descriptor."""
    try:
        decision = resolve_active_plugins(repo_root=root, workspace_trust_prompt=None)
    except (RuntimeError, ValidationError):
        # A malformed repository file is untrusted input: use the user's own state.
        decision = ActivationDecision(
            enabled_plugin_ids=frozenset(
                compute_effective_enabled(load_global_state(), ProjectExtensionOverrides())
            ),
            workspace_trust_was_prompted=False,
            workspace_trust_granted=False,
            untrusted_project_plugin_ids=frozenset(),
        )
    if plugin_id not in decision.enabled_plugin_ids:
        return None
    record = load_global_state().installed.get(plugin_id)
    if (
        record is None
        or record.trust != "manifest-sha256"
        or record.source_kind != "custom"
        or not record.manifest_sha256
        or record.package_sha256 != record.manifest_sha256
        or record.commit != f"sha256:{record.manifest_sha256}"
    ):
        return None
    plugin_root = installed_plugin_root(plugin_id, project=False, repo_root=root)
    try:
        content = resolve_manifest_path(plugin_root).read_bytes()
        if hashlib.sha256(content).hexdigest() != record.manifest_sha256:
            return None
        manifest = load_manifest(plugin_root)
    except (OSError, ValueError):
        return None
    if manifest.plugin.id != plugin_id or len(manifest.components.custom_connector) != 1:
        return None
    connector = manifest.components.custom_connector[0]
    return {
        "plugin_id": plugin_id,
        "server_id": f"{plugin_id}/{connector.id}",
        "manifest_sha256": record.manifest_sha256,
        "package_sha256": record.manifest_sha256,
        "source_kind": "custom",
        "scope": "user",
    }, manifest


def verified_connector_plugin(
    root: Path, plugin_id: str
) -> tuple[dict[str, Any], PluginManifest] | None:
    """A verified plugin this engine binds: bundled, installed for the user, on, and pinned.

    The installed descriptor is this engine's pinned copy, or an older pinned copy that grants
    exactly the same access (the same reviewed tools and marks), as for Google.
    """
    if not is_verified_connector(plugin_id):
        return None
    try:
        pinned = _pinned_verified_manifest(plugin_id)
        if pinned is None:
            return None
        server = pinned.components.verified_connector[0].id

        def serves(manifest: PluginManifest) -> bool:
            connectors = manifest.components.verified_connector
            return (
                manifest.plugin.id == plugin_id
                and len(connectors) == 1
                and connectors[0].id == server
            )

        return verified_bundled_plugin(root, plugin_id, server, serves, user_only=True)
    except (OSError, RuntimeError):
        return None


def _pinned_verified_manifest(plugin_id: str) -> PluginManifest | None:
    entry = find_by_id(load_registry(), plugin_id)
    if entry is None or entry.source_kind != "bundled":
        return None
    manifest = parse_bundled_manifest(bundled_manifest(entry))
    if manifest is None or len(manifest.components.verified_connector) != 1:
        return None
    return manifest


def connector_plugin(root: Path, plugin_id: str) -> tuple[dict[str, Any], PluginManifest] | None:
    """The verified descriptor behind a connector catalog: the person's own, or Alysis's."""
    if is_verified_connector(plugin_id):
        return verified_connector_plugin(root, plugin_id)
    return verified_custom_plugin(root, plugin_id)


def _bind(root: Path, catalog: dict[str, Any]) -> dict[str, Any] | None:
    verified = connector_plugin(root, catalog["plugin_id"])
    if verified is None:
        return None
    plugin, manifest = verified
    if (
        catalog["descriptor_sha256"] != plugin["manifest_sha256"]
        or catalog["server_id"] != plugin["server_id"]
    ):
        return None
    if manifest.components.verified_connector:
        connector = manifest.components.verified_connector[0]
        marks = {tool.name: tool.reads_only for tool in connector.tools}
        host_arguments = [argument.name for argument in connector.host_arguments]
        extra = {
            "host_arguments": host_arguments,
            "max_argument_bytes": connector.max_argument_bytes,
        }
    else:
        marks = {
            tool.name: tool.reads_only for tool in manifest.components.custom_connector[0].tools
        }
        host_arguments, extra = [], {}
    tools = []
    for tool in catalog["tools"]:
        if tool["name"] not in marks:
            _fail("A custom plugin catalog names a tool the person didn't approve.")
        schema = tool["inputSchema"]
        # The app fills these in after approval: the AI must never see or set them.
        if set(host_arguments) & (
            set(schema.get("properties", {})) | set(schema.get("required", []))
        ):
            _fail("A verified plugin catalog still exposes an argument the app fills in.")
        tools.append({**tool, "readOnly": marks[tool["name"]]})
    return {**catalog, "tools": tools, "plugin": plugin, **extra}


def bind_connector_catalogs(root: Path, catalogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bound = []
    for catalog in catalogs:
        result = _bind(root, catalog)
        if result is None:
            raise HostActionError(
                "custom_plugin_inactive",
                "This custom plugin was turned off, removed or changed.",
            )
        bound.append(result)
    return bound


def bind_active_connector_catalogs(
    root: Path, catalogs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Bind the catalogs whose plugin is still on and unchanged; report the others."""
    bound, dropped = [], []
    for catalog in catalogs:
        result = _bind(root, catalog)
        if result is None:
            dropped.append(
                {
                    "plugin_id": catalog["plugin_id"],
                    "code": "custom_plugin_inactive",
                    "message": "This custom plugin was turned off, removed or changed.",
                }
            )
        else:
            bound.append(result)
    return bound, dropped


def assert_connector_current(root: Path, catalog: dict[str, Any]) -> None:
    verified = connector_plugin(root, catalog["plugin_id"])
    if verified is None or verified[0] != catalog["plugin"]:
        raise HostActionError(
            "custom_plugin_revoked",
            "Custom plugin changed or was turned off; reconnect the session.",
        )


def connector_session_payload(catalogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "protocol_version": 1,
            **c["plugin"],
            "tool_names": [t["name"] for t in c["tools"]],
        }
        for c in catalogs
    ]


def normalize_connector_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    if (
        set(raw) != {"plugin_id", "name", "arguments"}
        or not _known_plugin_id(raw.get("plugin_id"))
        or not isinstance(raw.get("name"), str)
        or not 1 <= len(raw["name"]) <= 64
        or not isinstance(raw.get("arguments"), dict)
    ):
        _fail("Custom plugin calls require a plugin, an approved tool name and arguments object.")
    if not is_verified_connector(raw["plugin_id"]):
        return _copy(raw, 8 * 1024)
    # Measured in compact UTF-8, as the app measures; the plugin's own limit (at most this)
    # is checked against its bound catalog.
    try:
        if json_size_bytes(raw) > MAX_VERIFIED_ARGUMENT_BYTES:
            _fail("Custom plugin payload exceeds its size limit.")
        return json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, RecursionError):
        _fail("Custom plugins require bounded JSON.")


def connector_argument_limit(catalog: dict[str, Any]) -> int:
    """A call's size limit: the verified descriptor's own, else the host action default."""
    return int(catalog.get("max_argument_bytes", DEFAULT_CONNECTOR_ARGUMENT_BYTES))


def check_connector_call(catalog: dict[str, Any], arguments: dict[str, Any]) -> None:
    """A call within its plugin's size limit that sets no argument the app fills in."""
    if set(catalog.get("host_arguments", ())) & set(arguments["arguments"]):
        raise HostActionError(
            "invalid_host_action_arguments",
            "This plugin call sets an argument only the app may fill in.",
        )
    if json_size_bytes(arguments) > connector_argument_limit(catalog):
        raise HostActionError(
            "host_action_arguments_too_large",
            "IDE host action arguments exceed the allowed size.",
        )


def wrap_connector_result(
    catalog: dict[str, Any], name: str, raw: dict[str, Any]
) -> dict[str, Any]:
    result = normalize_mcp_call_result(raw)
    return {
        "server_id": catalog["server_id"],
        "tool_name": name,
        "is_error": result.get("isError", False),
        "text": build_untrusted_mcp_text_block(
            source_type="tool_result",
            server_id=catalog["server_id"],
            source_name=name,
            text=json.dumps(result, ensure_ascii=False),
        ),
    }
