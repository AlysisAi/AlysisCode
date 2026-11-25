"""More apps for owls on this computer (hub plugins, 2026-11-24).

Owls in the local service use the apps connected under More apps as Work does: the service loads
their tools with the signed-in account's own key (``hub_apps.load_hub_catalogs``) and makes each
call itself, after the owl's approval step for the tool's effect (``bots/runtime.py``). The desktop
app takes no part, so these apps work whenever the service runs. Each owl uses only those the
person leaves on for it (``OwlApps``, keyed by ``hub.<app>``); an app connected later is on until
the person turns it off. Every call counts toward the plan's app calls on Alysis's server.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import hub_apps

# A newly connected app reaches owls within this time; tools are cached longer by hub_apps.
REFRESH_S = 60.0
# Refusals the server gives before anything reaches the app.
_NOT_SENT = frozenset(
    {
        "signed_out",
        "invalid_key",
        "rate_limited",
        "connections_limit",
        "allowance_used",
        "credits_used",
        "credits_not_connected",
        "not_connected",
        "unknown_tool",
        "invalid_request",
        "too_large",
        "not_found",
    }
)


@dataclass(frozen=True)
class HubTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    effect: str


@dataclass(frozen=True)
class HubPlugin:
    plugin_id: str
    name: str
    catalog_id: str
    tools: tuple[HubTool, ...]


def _plugin(catalog: dict[str, Any]) -> HubPlugin:
    return HubPlugin(
        plugin_id=catalog["plugin_id"],
        name=catalog["name"],
        catalog_id=catalog["catalog_id"],
        tools=tuple(
            HubTool(
                name=tool["name"],
                description=str(tool.get("description") or "")[:4000],
                input_schema=tool.get("inputSchema") or {"type": "object"},
                effect=tool["effect"],
            )
            for tool in catalog["tools"]
        ),
    )


class OwlHubApps:
    """The account's connected More apps, as owls see them, and their calls."""

    def __init__(
        self,
        load: Callable[
            [], tuple[list[dict[str, Any]], dict[str, Any]]
        ] = hub_apps.load_hub_catalogs,
        client: Callable[[], hub_apps.HubAppsClient] = hub_apps.HubAppsClient.from_saved_account,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load, self._client, self._clock = load, client, clock
        self._lock = threading.Lock()
        self._plugins: list[HubPlugin] = []
        self._at: float | None = None

    def plugins(self) -> list[HubPlugin]:
        """The connected apps with their tools; none while signed out or offline."""
        with self._lock:
            if self._at is not None and self._clock() - self._at < REFRESH_S:
                return list(self._plugins)
        catalogs, _status = self._load()
        plugins = [_plugin(catalog) for catalog in catalogs]
        with self._lock:
            self._plugins, self._at = plugins, self._clock()
        return list(plugins)

    def call(self, plugin_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One call, answered like the desktop app's (``ResultInput``)."""
        plugin = next((entry for entry in self.plugins() if entry.plugin_id == plugin_id), None)
        if plugin is None or not any(entry.name == tool for entry in plugin.tools):
            return {
                "ok": False,
                "error": "This app or action isn't connected to the person's Alysis account "
                "right now. Nothing was sent.",
                "dispatch_state": "not_dispatched",
            }
        try:
            result = self._client().call(plugin.catalog_id, tool, arguments)
        except hub_apps.HubAppsError as exc:
            sent = "not_dispatched" if exc.reason in _NOT_SENT else "unknown"
            note = (
                " Nothing was sent."
                if sent == "not_dispatched"
                else " It may have run; check before trying again."
            )
            return {"ok": False, "error": str(exc) + note, "dispatch_state": sent}
        if not result.sent:
            return {
                "ok": False,
                "error": f"{plugin.name} refused this request before it ran, so nothing was sent. "
                "Check the arguments against the tool's description.",
                "dispatch_state": "not_dispatched",
            }
        return {
            "ok": True,
            "text": result.text,
            "is_error": result.is_error,
            "dispatch_state": "dispatched",
        }
