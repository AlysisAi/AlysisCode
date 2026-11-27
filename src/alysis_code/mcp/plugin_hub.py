"""Hub plugins: apps from the catalog that Alysis's server brokers (2026-11-23).

A hub plugin (``hub.<app>``) is one app of a large catalog reached through Alysis's own server.
The engine loads the signed-in account's connected apps from that server itself
(``hub_apps.load_hub_catalogs``) and runs their calls with the account's own key; the desktop app
never sends a hub catalog and never makes a hub call. Nothing is installed here, and no app's
sign-in is on this computer: it stays with the catalog's provider, behind Alysis's server.

The engine decides every hub tool's effect from the hints the tool declares, by one rule
(``hub_tool_effect``):

- a tool that declares that it only reads is a read when Alysis tested its app, and otherwise
  follows the chat's approval mode and the AI reviewer, as a verified plugin's change in the
  person's own account does;
- every other tool (one that creates, updates or deletes, or declares nothing) asks a person on
  every call, in every mode.

The provider assigns hints on a best-effort basis, so a hint can only lower friction for a read;
a missing or contradictory hint asks. The rule is a property of each tool, never of a task, a
site, a language or a model.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from ..extensions.manifest import MAX_VERIFIED_ARGUMENT_BYTES, custom_tool_alias

HubEffect = Literal["read", "own_change", "asks"]

#: The one server id part every hub catalog uses: ``hub.<app>/hub``.
HUB_SERVER = "hub"
#: An app catalog can be large (a CRM lists hundreds of actions); look-up keeps the model's
#: request small, and this bounds one app.
MAX_HUB_TOOLS = 300
#: At most this many connected apps in one chat.
MAX_HUB_APPS = 16
#: The hints a hub tool may declare. Anything else is refused.
HUB_HINTS = frozenset({"readOnly", "create", "update", "destructive"})
#: A call's arguments (an email body, a page) may take this much.
HUB_ARGUMENT_BYTES = MAX_VERIFIED_ARGUMENT_BYTES

# At most 24 characters after "hub.", so every model-facing name has room for its action
# (Alysis's server gives a longer catalog id a short stable one).
_HUB_PLUGIN_ID = re.compile(r"hub\.[a-z][a-z0-9_]{0,23}\Z")
_CATALOG_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_TOOL_NAME_LIMIT = 64
_CATALOG_FIELDS = frozenset(
    {"protocol_version", "plugin_id", "server_id", "catalog_id", "name", "tested", "tools"}
)


class HubCatalogError(ValueError):
    """A hub app or action the engine won't bind."""


def is_hub_plugin(plugin_id: Any) -> bool:
    return isinstance(plugin_id, str) and _HUB_PLUGIN_ID.fullmatch(plugin_id) is not None


def hub_server_id(plugin_id: str) -> str:
    return f"{plugin_id}/{HUB_SERVER}"


def hub_tool_effect(hints: Mapping[str, Any] | None, *, tested: bool) -> HubEffect:
    """The approval a hub tool's call needs, from the tool's own hints and whether Alysis
    tested its app."""
    hints = hints or {}
    reads_only = hints.get("readOnly") is True and not any(
        hints.get(name) is True for name in ("create", "update", "destructive")
    )
    if not reads_only:
        return "asks"
    return "read" if tested else "own_change"


def hub_tool_prefix(plugin_id: str) -> str:
    """``mcp__hub_<app>__``, distinct from verified (``mcp__<name>__``) and custom plugins."""
    return f"mcp__hub_{plugin_id.split('.', 1)[1]}__"


def hub_tool_alias(plugin_id: str, tool_name: str) -> str:
    """The model-facing part of a hub tool's name, within 64 characters with its prefix.

    Catalog actions usually repeat their app's id first (``<APP>_SEND_EMAIL`` in app ``<app>``);
    that repetition goes, since the prefix already names the app. An action that doesn't repeat
    it keeps a hash of its name, so it never takes the alias of one that does.
    """
    app = plugin_id.split(".", 1)[1]
    lowered = tool_name.lower()
    room = _TOOL_NAME_LIMIT - len(hub_tool_prefix(plugin_id))
    rest = lowered[len(app) + 1 :]
    if lowered.startswith(f"{app}_") and rest.strip("_"):
        return custom_tool_alias(rest, max_chars=room)
    suffix = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:8]
    return f"{custom_tool_alias(lowered, max_chars=room - len(suffix) - 1)}_{suffix}"


def hub_tool_name(plugin_id: str, tool_name: str) -> str:
    return hub_tool_prefix(plugin_id) + hub_tool_alias(plugin_id, tool_name)


def _bounded(value: Any, limit: int) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        raise HubCatalogError("A hub action's schema isn't plain JSON.") from None
    if size > limit:
        raise HubCatalogError("A hub action's schema is too large.")


def check_hub_tool(plugin_id: str, tool: Any, seen: set[str]) -> None:
    """One action of a hub app: its name, description, object input schema and hints. ``seen``
    holds the app's actions so far, so no two share a name or a model-facing name."""
    if not isinstance(tool, dict) or set(tool) != {"name", "description", "inputSchema", "hints"}:
        raise HubCatalogError("Invalid hub action fields.")
    name = tool["name"]
    if (
        not isinstance(name, str)
        or not name
        or len(name) > _TOOL_NAME_LIMIT
        or f"name:{name}" in seen
    ):
        raise HubCatalogError("Invalid or duplicate hub action name.")
    if not isinstance(tool["description"], str) or len(tool["description"]) > 8192:
        raise HubCatalogError("Invalid hub action description.")
    schema = tool["inputSchema"]
    properties = schema.get("properties", {}) if isinstance(schema, dict) else None
    required = schema.get("required", []) if isinstance(schema, dict) else None
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or not isinstance(properties, dict)
        or len(properties) > 64
        or not isinstance(required, list)
        or any(not isinstance(k, str) or k not in properties for k in required)
    ):
        raise HubCatalogError("A hub action's inputs must describe an object.")
    _bounded(schema, 24_000)
    hints = tool["hints"]
    if (
        not isinstance(hints, dict)
        or not set(hints) <= HUB_HINTS
        or any(type(value) is not bool for value in hints.values())
    ):
        raise HubCatalogError(
            "Hub action hints are true or false: readOnly, create, update, destructive."
        )
    alias = hub_tool_name(plugin_id, name)
    if f"alias:{alias}" in seen:
        raise HubCatalogError("Two hub actions would share one name.")
    seen.update({f"name:{name}", f"alias:{alias}"})


def check_hub_catalog(catalog: Any) -> None:
    """One connected app as the engine loaded it from Alysis's server."""
    if not isinstance(catalog, dict) or set(catalog) != _CATALOG_FIELDS:
        raise HubCatalogError("Invalid hub app fields.")
    plugin_id = catalog["plugin_id"]
    if (
        not is_hub_plugin(plugin_id)
        or type(catalog["protocol_version"]) is not int
        or catalog["protocol_version"] != 1
        or catalog["server_id"] != hub_server_id(plugin_id)
        or not isinstance(catalog["catalog_id"], str)
        or not _CATALOG_ID.fullmatch(catalog["catalog_id"])
        or not isinstance(catalog["name"], str)
        or not catalog["name"].strip()
        or len(catalog["name"]) > 120
        or type(catalog["tested"]) is not bool
    ):
        raise HubCatalogError("Hub app identity mismatch.")
    tools = catalog["tools"]
    if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_HUB_TOOLS:
        raise HubCatalogError(f"Each hub app has 1 to {MAX_HUB_TOOLS} actions.")
    seen: set[str] = set()
    for tool in tools:
        check_hub_tool(plugin_id, tool, seen)


def bind_hub_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """The engine's own decision for each action: ``readOnly`` only for a read of an app Alysis
    tested, and the ``effect`` its call needs (``hub_tool_effect``)."""
    check_hub_catalog(catalog)
    bound = copy.deepcopy(catalog)
    for tool in bound["tools"]:
        tool["effect"] = hub_tool_effect(tool["hints"], tested=bound["tested"])
        tool["readOnly"] = tool["effect"] == "read"
    bound["plugin"] = {
        "plugin_id": catalog["plugin_id"],
        "server_id": catalog["server_id"],
        "source_kind": "hub",
        "scope": "user",
    }
    bound["max_argument_bytes"] = HUB_ARGUMENT_BYTES
    return bound
