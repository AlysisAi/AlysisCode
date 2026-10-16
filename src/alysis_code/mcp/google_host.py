"""Host-owned Google Workspace tools; credentials never enter the Python engine."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..host_actions import HostActionError
from .github_host import active_host_plugin, normalize_mcp_call_result
from .untrusted_content import build_untrusted_mcp_text_block

GOOGLE_MCP_ACTION = "mcp.google.call"
SERVICES = {
    "drive": "drivemcp.googleapis.com",
    "docs": "docsmcp.googleapis.com",
    "sheets": "sheetsmcp.googleapis.com",
    "slides": "slidesmcp.googleapis.com",
    "calendar": "calendarmcp.googleapis.com",
    "gmail": "gmailmcp.googleapis.com",
    "chat": "chatmcp.googleapis.com",
    "contacts": "people.googleapis.com",
}
_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def _fail(message: str) -> None:
    raise HostActionError("invalid_google_mcp", message)


def _copy(raw: Any, limit: int) -> Any:
    try:
        data = json.dumps(raw, ensure_ascii=True, allow_nan=False)
        if len(data.encode()) > limit:
            _fail("Google MCP payload exceeds its size limit.")
        return json.loads(data)
    except (ValueError, TypeError, RecursionError):
        _fail("Google MCP requires bounded JSON.")


def normalize_google_catalogs(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    raw = _copy(raw, 800_000)
    if not isinstance(raw, list) or len(raw) > 8:
        _fail("google_mcp must contain at most eight service catalogs.")
    seen: set[str] = set()
    for catalog in raw:
        if not isinstance(catalog, dict) or set(catalog) != {
            "protocol_version",
            "service",
            "plugin_id",
            "server_id",
            "tools",
        }:
            _fail("Invalid Google catalog fields.")
        service = catalog["service"]
        if not isinstance(service, str) or service not in SERVICES or service in seen:
            _fail("Unknown or duplicate Google service.")
        seen.add(service)
        if (
            type(catalog["protocol_version"]) is not int
            or catalog["protocol_version"] != 1
            or catalog["plugin_id"] != f"alysis.google_{service}"
            or catalog["server_id"] != f"alysis.google_{service}/{service}"
        ):
            _fail("Google catalog identity mismatch.")
        if not isinstance(catalog["tools"], list) or not 1 <= len(catalog["tools"]) <= 32:
            _fail("Each Google service requires 1 to 32 tools.")
        names: set[str] = set()
        for tool in catalog["tools"]:
            if not isinstance(tool, dict) or set(tool) != {
                "name",
                "description",
                "inputSchema",
                "readOnly",
            }:
                _fail("Invalid Google tool fields.")
            name = tool["name"]
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
                _fail("Invalid or duplicate Google tool name.")
            names.add(name)
            if (
                type(tool["readOnly"]) is not bool
                or not isinstance(tool["description"], str)
                or len(tool["description"]) > 8192
            ):
                _fail("Invalid Google tool policy or description.")
            schema = tool["inputSchema"]
            if (
                not isinstance(schema, dict)
                or schema.get("type") != "object"
                or not isinstance(schema.get("properties", {}), dict)
            ):
                _fail("Google input schemas must describe objects.")
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            if (
                len(properties) > 64
                or not isinstance(required, list)
                or any(not isinstance(k, str) or k not in properties for k in required)
            ):
                _fail("Invalid Google schema properties.")
            _copy(schema, 24_000)
    return raw


def active_google_plugin(root: Path, service: str) -> dict[str, Any] | None:
    if service not in SERVICES:
        return None
    return active_host_plugin(
        root, f"alysis.google_{service}", service, f"https://{SERVICES[service]}/mcp/v1"
    )


def bind_google_catalogs(root: Path, catalogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bound = []
    for catalog in catalogs:
        plugin = active_google_plugin(root, catalog["service"])
        if plugin is None:
            _fail("Install and enable the trusted Google plugin before connecting tools.")
        bound.append({**catalog, "plugin": plugin})
    return bound


def assert_google_current(root: Path, catalog: dict[str, Any]) -> None:
    if active_google_plugin(root, catalog["service"]) != catalog["plugin"]:
        raise HostActionError(
            "google_plugin_revoked", "Google plugin changed or was disabled; reconnect the session."
        )


def google_session_payload(catalogs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "protocol_version": 1,
            "service": c["service"],
            **c["plugin"],
            "tool_names": [t["name"] for t in c["tools"]],
        }
        for c in catalogs
    ]


def normalize_google_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    if (
        set(raw) != {"service", "name", "arguments"}
        or not isinstance(raw.get("service"), str)
        or raw["service"] not in SERVICES
        or not isinstance(raw.get("name"), str)
        or not _NAME.fullmatch(raw["name"])
        or not isinstance(raw.get("arguments"), dict)
    ):
        _fail("Google calls require a known service, discovered tool name and arguments object.")
    return _copy(raw, 8 * 1024)


def wrap_google_result(catalog: dict[str, Any], name: str, raw: dict[str, Any]) -> dict[str, Any]:
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
