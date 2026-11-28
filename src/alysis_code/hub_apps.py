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
import re
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
    is_hub_connection_id,
    is_hub_plugin,
)

_RESPONSE_MAX = 4 * 1024 * 1024
_TOOLS_TTL_S = 600.0
_LOAD_TIMEOUT_S = 8.0

MESSAGES = {
    "conflict": "This owl's app grant changed. Review its current setting.",
    "grant_changed": "This owl's app grant changed. Review the account and action again.",
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


# The languages of the Alysis apps and website; the page after signing in to an app uses one.
RETURN_LANGUAGES = frozenset({"en", "de", "el", "es", "fr", "zh-CN"})


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
        owl_id: str | None = None,
    ) -> None:
        self._key = key
        self._url = (url or cloud.apps_url()).rstrip("/")
        self.owl_id = owl_id
        if owl_id is not None:
            if not re.fullmatch(r"[a-f0-9]{32}", owl_id):
                raise HubAppsError("invalid_request")
            self._url += f"/owls/{owl_id}"
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

    def connections(self, *, manage: bool = False) -> dict[str, Any]:
        return self._request("GET", "/connections", query={"manage": "1"} if manage else None)

    def set_owl_grant(self, catalog_id: str, grant: dict[str, Any]) -> None:
        if not self.owl_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", catalog_id):
            raise HubAppsError("invalid_request")
        self._request("PATCH", f"/grants/{catalog_id}", body=grant)

    def select(self, connection_id: str) -> dict[str, Any]:
        if not is_hub_connection_id(connection_id):
            raise HubAppsError("invalid_request")
        result = self._request("POST", f"/connections/{connection_id}/select", body={})
        if result.get("selected") != connection_id:
            raise HubAppsError("unavailable")
        return result

    def connect(self, app: str, *, language: str | None = None) -> dict[str, Any]:
        """A page where the person signs in to the app; nothing is connected until they do.

        ``language`` is the Alysis app's own (one of ``RETURN_LANGUAGES``): the page the person
        returns to after signing in is shown in it. Any other value is left out.
        """
        body: dict[str, Any] = {"app": app}
        if language in RETURN_LANGUAGES:
            body["language"] = language
        return self._request("POST", "/connect", body=body)

    def disconnect(self, connection_id: str) -> bool:
        return (
            self._request("DELETE", f"/connections/{urllib.parse.quote(connection_id, safe='')}")[
                "removed"
            ]
            is True
        )

    def tools(
        self, app: str, *, connection_id: str | None = None, timeout_s: float | None = None
    ) -> dict[str, Any]:
        path = (
            f"/connections/{urllib.parse.quote(connection_id, safe='')}/tools"
            if connection_id
            else "/tools"
        )
        result = self._request("GET", path, query={"app": app}, timeout_s=timeout_s)
        if connection_id and result.get("connection_id") != connection_id:
            raise HubAppsError("not_connected")
        return result

    def call(
        self,
        app: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        request_id: str | None = None,
        connection_id: str | None = None,
        grant_revision: int | None = None,
        timeout_s: float = 90.0,
    ) -> HubCallResult:
        payload = self._request(
            "POST",
            f"/connections/{urllib.parse.quote(connection_id, safe='')}/call"
            if connection_id
            else "/call",
            body={
                "app": app,
                "tool": tool,
                "arguments": arguments,
                "request_id": request_id or str(uuid.uuid4()),
                **({"grant_revision": grant_revision} if grant_revision is not None else {}),
            },
            timeout_s=timeout_s,
        )
        text = payload.get("text")
        if not isinstance(text, str):
            raise HubAppsError("unavailable")
        return HubCallResult(
            is_error=payload.get("is_error") is True, text=text, sent=payload.get("sent") is True
        )


_tools_cache: dict[tuple[str, str, str, str | None, int | None], tuple[float, dict[str, Any]]] = {}
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
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in listed.get("connections") or []:
            if isinstance(row, dict) and isinstance(row.get("app"), str):
                grouped.setdefault(row["app"], []).append(row)
        # Only the broker can say which provider identity is selected. Old brokers
        # cannot pin calls. A public app added without a sign-in has no identity.
        chosen = {}
        for app, rows in grouped.items():
            active = [
                row
                for row in rows
                if row.get("status") == "active"
                and isinstance(row.get("id"), str)
                and is_hub_connection_id(row["id"])
                and (row.get("selected") is True or row["id"] == f"added-{app}")
            ]
            if len(active) == 1:
                chosen[app] = active[0]
        apps = sorted(chosen)[:MAX_HUB_APPS]
        if not apps:
            return [], {"loaded": 0, "allowance": listed.get("allowance")}
        key = client._key  # noqa: SLF001 - the cache belongs to one account.

        def tools(app: str) -> dict[str, Any]:
            row = chosen[app]
            id_ = row["id"]
            revision = row.get("grant_revision")
            scope = client.owl_id
            if scope and (type(revision) is not int or revision < 0):
                raise HubAppsError("grant_changed")
            cache_key = (key, app, id_, scope, revision)
            with _cache_lock:
                hit = _tools_cache.get(cache_key)
            if hit and now() - hit[0] < _TOOLS_TTL_S:
                catalog = hit[1]
            else:
                bound_id = id_ if listed.get("account_selection") == 1 else None
                if not bound_id and id_ != f"added-{app}":
                    raise HubAppsError("not_connected")
                listing = client.tools(app, connection_id=bound_id, timeout_s=_LOAD_TIMEOUT_S)
                if scope and listing.get("grant_revision") != revision:
                    raise HubAppsError("grant_changed")
                catalog = _catalog_from(app, listing)
                if bound_id:
                    catalog["connection_id"] = bound_id
                if scope:
                    catalog["grant_revision"] = revision
                with _cache_lock:
                    if len(_tools_cache) >= 512:
                        _tools_cache.clear()
                    _tools_cache[cache_key] = (now(), catalog)
            if id_ == f"added-{app}":
                return catalog
            label = str(row.get("label") or id_).strip()[:80]
            return {**catalog, "name": f"{catalog['name'][:36]} · {label}"}

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
