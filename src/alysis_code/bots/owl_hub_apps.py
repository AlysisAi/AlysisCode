"""More apps for local and managed Cloud Owls.

The local service uses the signed-in account. The managed service exchanges its
owner registration for an expiring, single-Owl broker delegation. Both call
the broker directly after the Owl's normal approval checks.
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
        "grant_changed",
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
    connection_id: str | None = None
    grant_revision: int | None = None


def _plugin(catalog: dict[str, Any]) -> HubPlugin:
    return HubPlugin(
        plugin_id=catalog["plugin_id"],
        name=catalog["name"],
        catalog_id=catalog["catalog_id"],
        connection_id=catalog.get("connection_id"),
        grant_revision=catalog.get("grant_revision"),
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
            ..., tuple[list[dict[str, Any]], dict[str, Any]]
        ] = hub_apps.load_hub_catalogs,
        client: Callable[..., hub_apps.HubAppsClient] | None = None,
        clock: Callable[[], float] = time.monotonic,
        manage_client: Callable[..., hub_apps.HubAppsClient] | None = None,
    ) -> None:
        self._load, self._client, self._clock = (
            load,
            client or hub_apps.HubAppsClient.from_saved_account,
            clock,
        )
        self._lock = threading.Lock()
        self._manage = manage_client or self._client
        self._plugins: dict[str, tuple[float, list[HubPlugin]]] = {}

    def plugins(self, owl_id: str, *, refresh: bool = False) -> list[HubPlugin]:
        """The connected apps with their tools; none while signed out or offline."""
        with self._lock:
            hit = self._plugins.get(owl_id)
            if hit and not refresh and self._clock() - hit[0] < REFRESH_S:
                return list(hit[1])
        try:
            catalogs, _status = self._load(self._client(owl_id=owl_id, timeout_s=8))
        except hub_apps.HubAppsError:
            return []
        plugins = [_plugin(catalog) for catalog in catalogs]
        with self._lock:
            if len(self._plugins) >= 128:
                self._plugins.clear()
            self._plugins[owl_id] = self._clock(), plugins
        return list(plugins)

    def settings(self, owl_id: str) -> list[dict] | None:
        try:
            listing = self._manage(owl_id=owl_id, timeout_s=8).connections(manage=True)
        except hub_apps.HubAppsError:
            return None
        return listing.get("hub_accounts")

    def set_grant(self, owl_id: str, app: str, grant: dict) -> None:
        self._manage(owl_id=owl_id).set_owl_grant(app, grant)
        with self._lock:
            self._plugins.pop(owl_id, None)

    def event_client(self, owl_id: str):
        return self._client(owl_id=owl_id, timeout_s=8)

    def call(
        self,
        owl_id: str,
        plugin_id: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        connection_id: str | None = None,
        grant_revision: int | None = None,
    ) -> dict[str, Any]:
        """One call, answered like the desktop app's (``ResultInput``)."""
        plugin = next(
            (entry for entry in self.plugins(owl_id) if entry.plugin_id == plugin_id), None
        )
        if (
            plugin is None
            or plugin.connection_id != connection_id
            or plugin.grant_revision != grant_revision
            or not any(entry.name == tool for entry in plugin.tools)
        ):
            return {
                "ok": False,
                "error": "This app or action isn't connected to the person's Alysis account "
                "right now. Nothing was sent.",
                "dispatch_state": "not_dispatched",
            }
        try:
            result = self._client(owl_id=owl_id).call(
                plugin.catalog_id,
                tool,
                arguments,
                connection_id=connection_id,
                grant_revision=grant_revision,
            )
        except hub_apps.HubAppsError as exc:
            if exc.reason in {"grant_changed", "not_connected"}:
                with self._lock:
                    self._plugins.pop(owl_id, None)
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
