"""Apps of the catalog that Alysis's server brokers (hub plugins, 2026-11-23): the engine's client.

The engine holds the signed-in account's device key and calls Alysis's ``apps`` function with it,
as it does for usage-limit resets. The desktop app never holds that key, and no app's sign-in is on
this computer: it stays with the catalog's provider, behind Alysis's server.

- The desktop app lists, connects and removes apps through ``alysis apps --json``.
- A chat asks for the connected apps with ``session.create`` ``hub_apps``; the engine loads their
  tools here (``load_hub_catalogs``) and runs each call here too, after the approval its effect
  needs (``mcp/plugin_hub.py``).

Every failure carries a reason code the app words for the person, in their language; the English
messages here are for the command line.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from . import alysis_cloud as cloud
from .mcp.plugin_hub import (
    MAX_HUB_APPS,
    HubCatalogError,
    bind_hub_catalog,
    check_hub_catalog,
    check_hub_tool,
    is_hub_plugin,
)

_RESPONSE_MAX = 4 * 1024 * 1024
_TOOLS_TTL_S = 600.0
_LOAD_TIMEOUT_S = 8.0

MESSAGES = {
    "signed_out": "No Alysis Code account is connected. Sign in to use more apps.",
    "invalid_key": "Your Alysis Code sign-in has expired. Sign in again to use more apps.",
    "rate_limited": "Too many app requests in a short time. Wait a minute and try again.",
    "offline": "Couldn't reach your Alysis Code account. Check your connection and try again.",
    "connections_limit": "Your plan's limit on connected apps is reached. Remove one, or upgrade.",
    "allowance_used": "This month's app calls on your plan are used. Upgrade for more.",
    "credits_not_connected": "This month's included app calls are used.",
    "credits_used": "Your plan's credits are used up. App calls continue when they renew.",
    "not_connected": "This app isn't connected to your account.",
    "unknown_tool": "This app doesn't offer that action.",
    "invalid_request": "This app request isn't valid.",
    "too_large": "This app request is too large.",
    "not_found": "Not found.",
    "provider_unavailable": "The app service didn't answer. Nothing was changed on our side.",
    "unavailable": "More apps aren't available right now. Try again later.",
}
_STATUS_REASONS = {401: "invalid_key", 413: "too_large", 429: "rate_limited"}


class HubAppsError(Exception):
    """A refusal or failure, with the reason code the desktop app translates."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason if reason in MESSAGES else "unavailable"
        super().__init__(message or MESSAGES[self.reason])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class HubCallResult:
    is_error: bool
    text: str
    sent: bool


class HubAppsClient:
    """One signed-in account's view of the brokered catalog."""

    def __init__(
        self,
        key: str,
        *,
        url: str | None = None,
        timeout_s: float = 20.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._key = key
        self._url = (url or cloud.apps_url()).rstrip("/")
        self._timeout = timeout_s
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    @classmethod
    def from_saved_account(cls, **kwargs: Any) -> HubAppsClient:
        from .config import load_persisted_profile_keys

        key = load_persisted_profile_keys().get(cloud.PROFILE_KEY)
        if not key:
            raise HubAppsError("signed_out")
        return cls(key, **kwargs)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        url = self._url + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        request = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with self._open(request, timeout=timeout_s or self._timeout) as response:
                raw = response.read(_RESPONSE_MAX + 1)
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read(4096)).get("error")
            except (ValueError, UnicodeDecodeError, AttributeError, OSError):
                code = None
            reason = code if isinstance(code, str) and code in MESSAGES else None
            raise HubAppsError(reason or _STATUS_REASONS.get(exc.code, "unavailable")) from None
        except cloud.AlysisCloudConfigError:
            raise HubAppsError("unavailable") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HubAppsError("offline") from None
        try:
            payload = json.loads(raw) if len(raw) <= _RESPONSE_MAX else None
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise HubAppsError("unavailable")
        return payload

    def catalog(self, query: str = "") -> dict[str, Any]:
        """Every app this account may connect, with this month's allowance."""
        return self._request("GET", "/catalog", query={"query": query[:100]} if query else None)

    def connections(self) -> dict[str, Any]:
        return self._request("GET", "/connections")

    def connect(self, app: str) -> dict[str, Any]:
        """A page where the person signs in to the app; nothing is connected until they do."""
        return self._request("POST", "/connect", body={"app": app})

    def disconnect(self, connection_id: str) -> bool:
        return (
            self._request("DELETE", f"/connections/{urllib.parse.quote(connection_id, safe='')}")[
                "removed"
            ]
            is True
        )

    def tools(self, app: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        return self._request("GET", "/tools", query={"app": app}, timeout_s=timeout_s)

    def call(
        self, app: str, tool: str, arguments: dict[str, Any], *, request_id: str | None = None
    ) -> HubCallResult:
        payload = self._request(
            "POST",
            "/call",
            body={
                "app": app,
                "tool": tool,
                "arguments": arguments,
                "request_id": request_id or str(uuid.uuid4()),
            },
            timeout_s=90.0,
        )
        text = payload.get("text")
        if not isinstance(text, str):
            raise HubAppsError("unavailable")
        return HubCallResult(
            is_error=payload.get("is_error") is True, text=text, sent=payload.get("sent") is True
        )


_tools_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _catalog_from(app: str, listing: dict[str, Any]) -> dict[str, Any]:
    plugin_id = listing.get("id")
    if not is_hub_plugin(plugin_id) or listing.get("catalog_id") != app:
        raise HubAppsError("unavailable")
    catalog = {
        "protocol_version": 1,
        "plugin_id": plugin_id,
        "server_id": f"{plugin_id}/hub",
        "catalog_id": app,
        "name": str(listing.get("name") or app)[:120],
        "tested": listing.get("tested") is True,
        "tools": [],
    }
    seen: set[str] = set()
    for item in listing.get("tools") or []:
        if not isinstance(item, dict):
            continue
        tool = {
            "name": item.get("name"),
            "description": item.get("description") or "",
            "inputSchema": item.get("input_schema") or {"type": "object"},
            "hints": item.get("hints") or {},
        }
        # One malformed action leaves out that action, not the whole app.
        try:
            check_hub_tool(plugin_id, tool, seen)
        except HubCatalogError:
            continue
        catalog["tools"].append(tool)
    try:
        check_hub_catalog(catalog)
    except HubCatalogError:
        raise HubAppsError("unavailable") from None
    return catalog


def load_hub_catalogs(
    client: HubAppsClient | None = None, *, now: Callable[[], float] = time.monotonic
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The signed-in account's connected apps, bound with each tool's effect, and a status the
    session reports (``hub_apps``). Failing to load never fails the session: it has no apps."""
    try:
        client = client or HubAppsClient.from_saved_account(timeout_s=_LOAD_TIMEOUT_S)
        listed = client.connections()
        apps = sorted(
            {
                c["app"]
                for c in listed.get("connections") or []
                if isinstance(c, dict)
                and c.get("status") == "active"
                and isinstance(c.get("app"), str)
            }
        )[:MAX_HUB_APPS]
        if not apps:
            return [], {"loaded": 0, "allowance": listed.get("allowance")}
        key = client._key  # noqa: SLF001 - the cache belongs to one account.

        def tools(app: str) -> dict[str, Any]:
            with _cache_lock:
                hit = _tools_cache.get((key, app))
            if hit and now() - hit[0] < _TOOLS_TTL_S:
                return hit[1]
            catalog = _catalog_from(app, client.tools(app, timeout_s=_LOAD_TIMEOUT_S))
            with _cache_lock:
                _tools_cache[(key, app)] = (now(), catalog)
            return catalog

        catalogs, failed = [], []
        with ThreadPoolExecutor(max_workers=min(4, len(apps))) as pool:
            for app, future in [(app, pool.submit(tools, app)) for app in apps]:
                try:
                    catalogs.append(bind_hub_catalog(future.result()))
                except (HubAppsError, HubCatalogError):
                    failed.append(app)
        return catalogs, {
            "loaded": len(catalogs),
            **({"failed": failed} if failed else {}),
            "allowance": listed.get("allowance"),
        }
    except HubAppsError as exc:
        return [], {"loaded": 0, "error": exc.reason}
