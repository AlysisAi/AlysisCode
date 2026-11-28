"""Plugins for owls, reached through the desktop app that keeps their sign-ins (2026-11-10).

The desktop app holds every plugin sign-in on the person's computer, as in Work. It tells this
service which plugin tools it can call (with Alysis's approval effect for each) and answers this
service's calls one at a time; the service never receives a token. An owl's call waits until the
app answers, the time runs out or the task stops. Without the app, plugins are unavailable and the
owl says so.

Each owl uses the connected plugins the person leaves on for it (``OwlApps``, 2026-11-18). A new
owl, and every owl from before these choices, has every connected plugin on, as when plugins were
account-wide. A plugin switched off for an owl isn't offered to it in any turn (chat, routine or
exchange) and a call to it is refused before anything is sent, from the step after the switch.
When a request needs an app that is off for the owl or not connected, the owl asks for it with a
card (``need_app``), decided from the app's id, and turning it on or connecting it carries the
request on with a turn that has no new message (``app_ready``).

Owls in the local service also use the apps connected under More apps (``hub.<app>``,
``owl_hub_apps.py``): the service calls them itself with the account's key, never through the
desktop app, and each owl's switches cover them the same way.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, StrictBool, field_validator, model_validator

from ..cloud_connections import ConnectionUnavailable
from ..hub_apps import HubAppsError
from ..mcp.plugin_hub import is_hub_connection_id
from .connector_results import ConnectorEvidence
from .models import Input
from .store import Conflict, LimitReached, NotFound, encode, uid

Effect = Literal["read", "own_change", "asks"]
CALL_TIMEOUT_S = 90.0
POLL_WAIT_S = 8.0
# The app polls continuously while it runs; a longer silence means it is closed or stopped.
DESKTOP_IDLE_S = 30.0
MAX_BATCH = 8


class CatalogTool(Input):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    effect: Effect


# The desktop app's own plugins. More apps never come from the desktop app (owl_hub_apps.py).
DESKTOP_PLUGIN_ID = r"^(alysis|custom)\.[a-z][a-z0-9_-]{0,63}$"
# Any app an owl's switch can name: the desktop app's plugins and the More apps.
PLUGIN_ID = r"^(?:(?:alysis|custom)\.[a-z][a-z0-9_-]{0,63}|hub\.[a-z][a-z0-9_]{0,23})$"


class CatalogPlugin(Input):
    plugin_id: str = Field(pattern=DESKTOP_PLUGIN_ID)
    name: str = Field(min_length=1, max_length=120)
    tools: list[CatalogTool] = Field(max_length=96)


class CatalogApp(Input):
    plugin_id: str = Field(pattern=DESKTOP_PLUGIN_ID)
    name: str = Field(min_length=1, max_length=120)


class CatalogInput(Input):
    revision: int = Field(ge=0)
    plugins: list[CatalogPlugin] = Field(max_length=64)
    # Every app owls could use once it's connected in Alysis, connected or not, so an owl can ask
    # for one that isn't. Sent only to a service listing the ``owl_apps`` capability.
    apps: list[CatalogApp] | None = Field(default=None, max_length=128)


class PluginChoice(Input):
    """Whether one owl may use one connected app."""

    plugin_id: str = Field(pattern=PLUGIN_ID)
    enabled: StrictBool
    expected_revision: int = Field(ge=0)


class OwlHubGrant(Input):
    connection_id: str | None
    mode: Literal["inherit", "read", "actions"]
    expected_revision: int = Field(ge=0, strict=True)

    @field_validator("connection_id")
    @classmethod
    def valid_connection_id(cls, value: str | None) -> str | None:
        if value is not None and not is_hub_connection_id(value):
            raise ValueError("Invalid More Apps connection ID.")
        return value


class AppNeedDecision(Input):
    """The person's answer on an app card: turn it on for the owl, connect it, or not now."""

    action: Literal["turn_on", "connect", "dismiss", "use_account"]
    expected_revision: int = Field(ge=1)
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)


class NeedInput(Input):
    app: str = Field(pattern=PLUGIN_ID)
    reason: str = Field(default="", max_length=300)


class ResultInput(Input):
    ok: bool
    text: str = Field(default="", max_length=60000, repr=False)
    is_error: bool = False
    dispatch_state: Literal["not_dispatched", "dispatched", "unknown"] = "dispatched"
    error: str = Field(default="", max_length=1000)
    evidence: ConnectorEvidence | None = None

    @model_validator(mode="after")
    def one_dispatched_result(self):
        if self.evidence is not None and (
            not self.ok or self.dispatch_state != "dispatched" or self.text
        ):
            raise ValueError("Rich evidence belongs to one dispatched plugin result.")
        return self


class PluginCallUnavailable(ConnectionUnavailable):
    """Safe to show; ``dispatched`` says whether the app may have sent the call."""

    def __init__(self, message: str, *, dispatched: bool):
        super().__init__(message)
        self.dispatched = dispatched


class AppSwitchedOff(PluginCallUnavailable):
    """The person switched this app off for the owl after the step was offered.

    ``refusal`` is closed, structured data the app words for people in their language.
    """

    def __init__(self, plugin_id: str, name: str):
        super().__init__(
            f"{name} was switched off for you, so this step was not run and nothing was sent. "
            f"Don't use {name} for this request; tell the person plainly which part wasn't done.",
            dispatched=False,
        )
        self.refusal = {"code": "app_off", "plugin_id": plugin_id, "name": name}


@dataclass
class _Call:
    id: str
    bot_id: str
    task_id: str
    plugin_id: str
    tool: str
    arguments: dict[str, Any]
    approval: str
    claimed: bool = False
    result: dict[str, Any] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bot_id": self.bot_id,
            "task_id": self.task_id,
            "plugin_id": self.plugin_id,
            "name": self.tool,
            "arguments": self.arguments,
            "approval": self.approval,
            "result_version": 1,
        }


class DesktopConnectors:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Condition()
        self._plugins: list[CatalogPlugin] = []
        self._revision = -1
        self._seen: float | None = None
        self._calls: dict[str, _Call] = {}
        # A lost HTTP acknowledgement can be retried without retaining any result bytes.
        self._accepted: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._apps: list[CatalogApp] = []

    # ---- the desktop app --------------------------------------------------------------------
    def publish(self, data: CatalogInput) -> dict[str, Any]:
        with self._lock:
            self._plugins, self._revision = list(data.plugins), data.revision
            self._apps = list(data.apps or [])
            self._seen = self._clock()
            self._lock.notify_all()
        return {"revision": data.revision, "plugins": len(data.plugins)}

    def next(self, wait: float = POLL_WAIT_S) -> dict[str, Any]:
        deadline = self._clock() + wait
        with self._lock:
            while True:
                self._seen = self._clock()
                ready = [call for call in self._calls.values() if not call.claimed][:MAX_BATCH]
                if ready:
                    for call in ready:
                        call.claimed = True
                    return {"revision": self._revision, "calls": [c.public() for c in ready]}
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return {"revision": self._revision, "calls": []}
                self._lock.wait(remaining)

    def answer(self, call_id: str, data: ResultInput) -> dict[str, Any]:
        payload = data.model_dump(exclude_none=True)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).digest()
        with self._lock:
            now = self._clock()
            self._seen = now
            while self._accepted and next(iter(self._accepted.values()))[0] <= now:
                self._accepted.popitem(last=False)
            if accepted := self._accepted.get(call_id):
                if digest != accepted[1]:
                    raise Conflict("This plugin call already accepted a different result.")
                return {"accepted": True}
            call = self._calls.get(call_id)
            if call is None or not call.claimed or call.result is not None:
                raise NotFound("This plugin call is no longer waiting.")
            call.result = payload
            self._accepted[call_id] = (now + 2 * CALL_TIMEOUT_S, digest)
            if len(self._accepted) > 256:
                self._accepted.popitem(last=False)
            self._lock.notify_all()
        return {"accepted": True}

    # ---- owls ---------------------------------------------------------------------------------
    def _available(self) -> bool:
        return self._seen is not None and self._clock() - self._seen <= DESKTOP_IDLE_S

    def available(self) -> bool:
        with self._lock:
            return self._available()

    def plugins(self) -> list[CatalogPlugin]:
        """The connected plugins, only while the app answers."""
        with self._lock:
            return list(self._plugins) if self._available() else []

    def catalog(self) -> tuple[bool, list[dict[str, Any]]]:
        """Whether the app answers now, and every app its last catalog named.

        ``connected`` means the app published the plugin's tools. An app that sent no list of
        apps (an older one) names only its connected plugins.
        """
        with self._lock:
            names = {plugin.plugin_id: plugin.name for plugin in self._plugins}
            tools = set(names)
            for app in self._apps:
                names.setdefault(app.plugin_id, app.name)
            return self._available(), [
                {"plugin_id": plugin_id, "name": name, "connected": plugin_id in tools}
                for plugin_id, name in names.items()
            ]

    def call(
        self,
        *,
        bot_id: str,
        task_id: str,
        plugin_id: str,
        tool: str,
        arguments: dict[str, Any],
        approval: str,
        token: Any = None,
    ) -> dict[str, Any]:
        call = _Call(uuid4().hex, bot_id, task_id, plugin_id, tool, arguments, approval)
        with self._lock:
            if not self._available():
                raise PluginCallUnavailable(
                    "Plugins are reached through Alysis on this computer, and it isn't "
                    "connected right now. Nothing was sent. Open Alysis, then try again.",
                    dispatched=False,
                )
            self._calls[call.id] = call
            self._lock.notify_all()
            deadline = self._clock() + CALL_TIMEOUT_S
            try:
                while call.result is None:
                    if token is not None:
                        token.throw_if_cancelled()
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise PluginCallUnavailable(
                            "Alysis on this computer didn't answer in time. "
                            + (
                                "The call may have run; check before trying again."
                                if call.claimed
                                else "Nothing was sent."
                            ),
                            dispatched=call.claimed,
                        )
                    self._lock.wait(min(remaining, 1.0))
            finally:
                self._calls.pop(call.id, None)
        return call.result


# ---- each owl's apps ------------------------------------------------------------------------------

NEED_TOOL = "need_app"
# Statuses of an app card: shown, the person chose Connect and it isn't connected yet, carried on
# (turned on or connected), or "Not now".
OPEN_NEEDS = ("pending", "connecting")
MAX_QUEUED = 20

# A turn the app starts after the person turned on or connected an app the owl asked for; like a
# first hello it has no message from the person, so this text stands in for one.
APP_READY_REQUEST = (
    "This message comes from the Alysis app, not from the person. The person {how} {name} for "
    "you, so you can use it now. Carry on with their request that needed it and finish the work "
    "now with {name}'s tools. Don't ask them to send the request again. Reply in the language the "
    "person has been using with you."
)
_HOW = {"turned_on": "turned on", "connected": "connected"}


def app_ready_request(config: dict) -> str:
    """The app's request to carry on once the app in ``config["app_ready"]`` can be used."""
    ready = config.get("app_ready") or {}
    return APP_READY_REQUEST.format(
        how=_HOW.get(ready.get("how"), "turned on"),
        name=json.dumps(ready.get("name") or "", ensure_ascii=False),
    )


class OwlApps:
    """Which of the desktop app's plugins each owl may use, and the apps its turns asked for.

    A choice is stored only once the person makes one; no row means on.
    """

    def __init__(self, store, owner: str, hub: DesktopConnectors, more_apps: Any = None):
        self.store, self.owner, self.hub = store, owner, hub
        # The account's More apps (owl_hub_apps.OwlHubApps); None where the service has no account.
        self.more_apps = more_apps

    def more(self, bot_id: str, *, refresh: bool = False) -> list[Any]:
        return self.more_apps.plugins(bot_id, refresh=refresh) if self.more_apps is not None else []

    def for_owl(self, bot_id: str) -> OwlPlugins:
        return OwlPlugins(self, bot_id)

    def switched_off(self, bot_id: str) -> set[str]:
        with self.store.connect() as db:
            return {
                row["plugin_id"]
                for row in db.execute(
                    "SELECT plugin_id FROM bot_plugin_grants WHERE bot_id=? AND enabled=0",
                    (bot_id,),
                )
            }

    def listing(self, bot_id: str) -> dict:
        """Every app the desktop app named and every connected More app, with whether it's
        connected and on for this owl."""
        reachable, apps = self.hub.catalog()
        self.store.bot(self.owner, bot_id)
        accounts = self.more_apps.settings(bot_id) if self.more_apps is not None else None
        named = {app["plugin_id"] for app in apps}
        if accounts is not None:
            for group in accounts:
                if group["plugin_id"] not in named:
                    apps.append(
                        {
                            "plugin_id": group["plugin_id"],
                            "name": group["name"],
                            "connected": any(
                                row["status"] == "active" for row in group["accounts"]
                            ),
                        }
                    )
                    named.add(group["plugin_id"])
        apps += [
            {"plugin_id": plugin.plugin_id, "name": plugin.name, "connected": True}
            for plugin in (self.more(bot_id) if accounts is None else [])
            if plugin.plugin_id not in named
        ]
        with self.store.connect() as db:
            self.store._bot(db, self.owner, bot_id)
            choices = {
                row["plugin_id"]: row
                for row in db.execute(
                    "SELECT plugin_id,enabled,revision FROM bot_plugin_grants WHERE bot_id=?",
                    (bot_id,),
                )
            }
        rows = [
            {
                **app,
                "enabled": bool(choices[app["plugin_id"]]["enabled"])
                if app["plugin_id"] in choices
                else True,
                "revision": choices[app["plugin_id"]]["revision"]
                if app["plugin_id"] in choices
                else 0,
            }
            for app in apps
        ]
        rows.sort(key=lambda row: (not row["connected"], row["name"].casefold(), row["plugin_id"]))
        return {
            "reachable": reachable,
            "apps": rows,
            **({"hub_accounts": accounts} if accounts is not None else {}),
        }

    def set_hub_grant(self, bot_id: str, app: str, data: OwlHubGrant) -> dict:
        self.store.bot(self.owner, bot_id)
        if self.more_apps is None:
            raise ConnectionUnavailable("More apps aren't available to owls in this service.")
        try:
            self.more_apps.set_grant(bot_id, app, data.model_dump())
        except HubAppsError as exc:
            if exc.reason in {"conflict", "not_connected", "invalid_request"}:
                raise Conflict(str(exc)) from exc
            raise ConnectionUnavailable(str(exc)) from exc
        with self.store.connect(write=True) as db:
            self._pause_app_watches(db, bot_id, app)
        return self.listing(bot_id)

    def choose(self, bot_id: str, data: PluginChoice) -> dict:
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            self._set(db, bot_id, data.plugin_id, data.enabled, expected=data.expected_revision)
        return self.listing(bot_id)

    @staticmethod
    def _set(db, bot_id: str, plugin_id: str, enabled: bool, *, expected: int | None = None):
        row = db.execute(
            "SELECT revision FROM bot_plugin_grants WHERE bot_id=? AND plugin_id=?",
            (bot_id, plugin_id),
        ).fetchone()
        revision = row["revision"] if row else 0
        if expected is not None and expected != revision:
            raise Conflict("This app's setting changed. Review its current state.")
        db.execute(
            "INSERT INTO bot_plugin_grants(bot_id,plugin_id,enabled,revision,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(bot_id,plugin_id) DO UPDATE SET "
            "enabled=excluded.enabled,revision=excluded.revision,updated_at=excluded.updated_at",
            (bot_id, plugin_id, int(enabled), revision + 1, time.time()),
        )
        if not enabled and plugin_id.startswith("hub."):
            db.execute(
                "UPDATE bot_app_needs SET status='dismissed',revision=revision+1,updated_at=? "
                "WHERE bot_id=? AND plugin_id=? AND status='connecting'",
                (time.time(), bot_id, plugin_id),
            )
            OwlApps._pause_app_watches(db, bot_id, plugin_id.removeprefix("hub."))

    @staticmethod
    def _pause_app_watches(db, bot_id, catalog_id):
        from .app_events import SOURCES
        from .triggers import revoke

        for watch in db.execute(
            "SELECT id,kind FROM bot_triggers WHERE bot_id=? AND kind!='file_added' AND state='active'",
            (bot_id,),
        ).fetchall():
            if SOURCES[watch["kind"]].catalog != catalog_id:
                continue
            revoke(db, watch["id"], "Connected app access changed.")
            db.execute(
                "UPDATE bot_triggers SET state='paused',revision=revision+1,updated_at=? WHERE id=?",
                (time.time(), watch["id"]),
            )
            db.execute(
                "UPDATE app_event_watches SET notice='reconnect' WHERE trigger_id=?", (watch["id"],)
            )

    # ---- an app a turn needs ------------------------------------------------------------------
    def need_catalog(self, bot_id):
        reachable, desktop = self.hub.catalog()
        if self.more_apps is None:
            return reachable, desktop
        groups = self.more_apps.settings(bot_id)
        if groups is None:
            return reachable, desktop
        rows = (
            [app for app in desktop if not app["plugin_id"].startswith("hub.")] if reachable else []
        )
        for group in groups:
            grant = group["grant"]
            expected = grant.get("connection_id")
            active = next(
                (
                    row
                    for row in group["accounts"]
                    if row.get("status") == "active"
                    and (row["id"] == expected if expected else row.get("selected"))
                ),
                None,
            )
            rows.append(
                {
                    "plugin_id": group["plugin_id"],
                    "name": group["name"],
                    "connected": bool(active),
                    "connection_id": expected or (active and active["id"]),
                }
            )
        return True, rows

    def need(self, task: dict, arguments: dict) -> dict:
        """need_app: show the person a card to turn on or connect the app the request needs."""
        args = NeedInput.model_validate(arguments)
        reachable, apps = self.need_catalog(task["bot_id"])
        app = next((item for item in apps if item["plugin_id"] == args.app), None)
        if app is None:
            raise NotFound("No app with this id is in Alysis. Use one need_app lists.")
        name = app["name"]
        if not reachable:
            return {
                "app": name,
                "status": "unavailable",
                "next_step": "Alysis on the person's computer isn't connected, so apps can't be "
                "turned on or connected now. Say so plainly.",
            }
        if app["connected"] and args.app not in self.switched_off(task["bot_id"]):
            return {
                "app": name,
                "status": "on",
                "next_step": f"{name} is on for you. Use its tools for this request.",
            }
        state = "off" if app["connected"] else "not_connected"
        now = time.time()
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            asked = db.execute(
                "SELECT status FROM bot_app_needs WHERE source_task_id=? AND plugin_id=?",
                (source["id"], args.app),
            ).fetchone()
            if asked is not None and asked["status"] == "dismissed":
                return {
                    "app": name,
                    "status": "declined",
                    "next_step": f"The person chose not to use {name} for this request. Do what "
                    "you can without it and say plainly what that leaves undone.",
                }
            if asked is None:
                need_id = uid()
                db.execute(
                    "INSERT INTO bot_app_needs(id,bot_id,source_task_id,plugin_id,name,reason,"
                    "state,status,revision,resume_task_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending',1,NULL,?,?)",
                    (
                        need_id,
                        source["bot_id"],
                        source["id"],
                        args.app,
                        name,
                        " ".join(args.reason.split()),
                        state,
                        now,
                        now,
                    ),
                )
                if app.get("connection_id"):
                    db.execute(
                        "INSERT INTO hub_need_accounts VALUES(?,?)", (need_id, app["connection_id"])
                    )
        return {
            "app": name,
            "status": "card_shown",
            "state": state,
            "next_step": "A card in this chat asks the person to "
            + ("turn " + name + " on for you" if state == "off" else "connect " + name)
            + ". Tell them in one short line what you need it for, then end your reply. The "
            "request carries on by itself once they do; don't ask them to send it again.",
        }

    @staticmethod
    def public(row) -> dict:
        return {
            key: row[key]
            for key in (
                "id",
                "bot_id",
                "source_task_id",
                "plugin_id",
                "name",
                "reason",
                "state",
                "status",
                "revision",
                "resume_task_id",
                "created_at",
                "updated_at",
            )
        }

    def decide(self, bot_id: str, need_id: str, data: AppNeedDecision) -> dict:
        reachable, apps = self.need_catalog(bot_id)
        connected = {app["plugin_id"]: app for app in apps if app["connected"]} if reachable else {}
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            row = db.execute(
                "SELECT * FROM bot_app_needs WHERE id=? AND bot_id=?", (need_id, bot_id)
            ).fetchone()
            if row is None:
                raise NotFound("App request not found.")
            # The same press delivered again changes nothing.
            if (row["status"], data.action) in (
                ("resumed", "turn_on"),
                ("resumed", "connect"),
                ("resumed", "use_account"),
                ("dismissed", "dismiss"),
                ("connecting", "connect"),
            ):
                return self.public(row)
            if row["status"] not in OPEN_NEEDS or row["revision"] != data.expected_revision:
                raise Conflict("This app request changed. Review its current state.")
            if data.action == "dismiss":
                self._status(db, need_id, "dismissed")
            elif data.action == "use_account":
                current = connected.get(row["plugin_id"])
                if (
                    row["status"] != "connecting"
                    or not row["plugin_id"].startswith("hub.")
                    or not current
                    or not data.connection_id
                    or current.get("connection_id") != data.connection_id
                ):
                    raise Conflict(
                        "The selected account changed. Review it again before continuing."
                    )
                db.execute(
                    "INSERT INTO hub_need_accounts VALUES(?,?) ON CONFLICT(need_id) DO UPDATE SET connection_id=excluded.connection_id",
                    (need_id, data.connection_id),
                )
                self._resume(db, row, "connected", connection_id=data.connection_id)
            elif row["plugin_id"] in connected and self._matches_account(
                db, row, connected[row["plugin_id"]]
            ):
                self._resume(
                    db,
                    row,
                    "turned_on" if row["state"] == "off" else "connected",
                    connection_id=connected[row["plugin_id"]].get("connection_id"),
                )
            elif data.action == "turn_on" and row["state"] == "not_connected":
                raise Conflict(
                    f"{row['name']} isn't connected in Alysis on this computer. Connect it first."
                )
            elif data.action == "turn_on":
                # Disconnected since the card was shown: the card now offers to connect it.
                db.execute(
                    "UPDATE bot_app_needs SET state='not_connected',revision=revision+1,"
                    "updated_at=? WHERE id=?",
                    (time.time(), need_id),
                )
            else:
                # Carried on once the desktop app's catalog shows it connected (resume_connected).
                self._status(db, need_id, "connecting")
            return self.public(
                db.execute("SELECT * FROM bot_app_needs WHERE id=?", (need_id,)).fetchone()
            )

    def resume_connected(self) -> bool:
        """Carry on the requests whose app the person chose to connect and now has.

        A full task queue leaves them waiting; the next catalog the app sends tries again.
        """
        reachable, apps = self.hub.catalog()
        if not reachable:
            return False
        connected = {app["plugin_id"] for app in apps if app["connected"]}
        resumed = False
        with self.store.connect(write=True) as db:
            for row in db.execute(
                "SELECT n.* FROM bot_app_needs n JOIN bots b ON b.id=n.bot_id "
                "WHERE b.owner=? AND n.status='connecting' ORDER BY n.created_at,n.id",
                (self.owner,),
            ).fetchall():
                if (
                    row["plugin_id"].startswith("hub.")
                    or row["plugin_id"] not in connected
                    or self._queue_full(db)
                ):
                    continue
                self._resume(db, row, "connected")
                resumed = True
        return resumed

    @staticmethod
    def _matches_account(db, need, app):
        bound = db.execute(
            "SELECT connection_id FROM hub_need_accounts WHERE need_id=?", (need["id"],)
        ).fetchone()
        return not bound or bound[0] == app.get("connection_id")

    def resume_hub_connections(self, stopping=lambda: False):
        """Only probe the broker for people who pressed Connect; coalesce work for each Owl."""
        with self.store.connect() as db:
            bots = db.execute(
                "SELECT DISTINCT n.bot_id FROM bot_app_needs n JOIN bots b ON b.id=n.bot_id "
                "WHERE b.owner=? AND b.archived_at IS NULL AND n.plugin_id LIKE 'hub.%' AND n.status='connecting'",
                (self.owner,),
            ).fetchall()
        for bot in bots:
            if stopping():
                return
            bot_id = bot[0]
            _, apps = self.need_catalog(bot_id)
            connected = {
                app["plugin_id"]: app
                for app in apps
                if app["plugin_id"].startswith("hub.") and app["connected"]
            }
            with self.store.connect(write=True) as db:
                for need in db.execute(
                    "SELECT * FROM bot_app_needs WHERE bot_id=? AND status='connecting'", (bot_id,)
                ).fetchall():
                    app = connected.get(need["plugin_id"])
                    if app and self._matches_account(db, need, app) and not self._queue_full(db):
                        self._resume(db, need, "connected", connection_id=app.get("connection_id"))

    def _status(self, db, need_id: str, status: str, resume_task_id: str | None = None):
        db.execute(
            "UPDATE bot_app_needs SET status=?,revision=revision+1,resume_task_id=?,updated_at=? "
            "WHERE id=?",
            (status, resume_task_id, time.time(), need_id),
        )

    def _queue_full(self, db) -> bool:
        return (
            db.execute(
                "SELECT count(*) FROM tasks WHERE owner=? AND "
                "status IN ('queued','running','waiting','cancelling')",
                (self.owner,),
            ).fetchone()[0]
            >= MAX_QUEUED
        )

    def _resume(self, db, need, how: str, *, connection_id=None) -> None:
        """Turn the app on for the owl and carry its request on, as one change."""
        self._set(db, need["bot_id"], need["plugin_id"], True)
        source = self.store._task(db, self.owner, need["source_task_id"])
        request_id = "app_ready_" + need["id"]
        previous = db.execute(
            "SELECT id FROM tasks WHERE owner=? AND request_id=?", (self.owner, request_id)
        ).fetchone()
        task_id = previous["id"] if previous else None
        # Only the newest request in its chat goes on: a newer one already moved the conversation
        # on, and work that stopped or waits for something else stays as it is.
        if (
            task_id is None
            and source["status"] in ("queued", "running", "completed")
            and not db.execute(
                "SELECT 1 FROM tasks WHERE chat_id=? AND id<>? AND created_at>=? LIMIT 1",
                (source["chat_id"], source["id"], source["created_at"]),
            ).fetchone()
        ):
            if self._queue_full(db):
                raise LimitReached("The task queue is full. Wait for tasks to finish.")
            bot = self.store._bot(db, self.owner, source["bot_id"])
            task_id, now = uid(), time.time()
            # The person carried its request on: an archived owl comes back (Store._bring_back).
            if bot["archived_at"] is not None:
                self.store._bring_back(db, bot["id"], now)
                bot = self.store._bot(db, self.owner, bot["id"])
            db.execute(
                "INSERT INTO tasks(id,chat_id,bot_id,owner,request_id,message,status,config,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    source["chat_id"],
                    bot["id"],
                    self.owner,
                    request_id,
                    "",
                    "queued",
                    encode(
                        {
                            **bot,
                            "reply_review_enabled": self.store._reply_review_enabled(
                                db, self.owner
                            ),
                            "app_ready": {
                                "plugin_id": need["plugin_id"],
                                "name": need["name"],
                                "how": how,
                                **({"connection_id": connection_id} if connection_id else {}),
                            },
                        }
                    ),
                    now,
                    now,
                ),
            )
            self.store._event(db, task_id, "queued", {})
        self._status(db, need["id"], "resumed", task_id)


class OwlPlugins:
    """The desktop app's plugins as one owl sees them: only those on for it.

    It stands in for the hub in ``BoundConnections`` (same ``available``/``plugins``/``call``).
    """

    def __init__(self, apps: OwlApps, bot_id: str):
        self.apps, self.bot_id = apps, bot_id

    def available(self) -> bool:
        return self.apps.hub.available()

    def plugins(self) -> list[Any]:
        """The desktop app's plugins while it answers, and the More apps, on for this owl."""
        off = self.apps.switched_off(self.bot_id)
        return [
            plugin
            for plugin in [*self.apps.hub.plugins(), *self.apps.more(self.bot_id, refresh=True)]
            if plugin.plugin_id not in off
        ]

    def require_on(self, plugin_id: str, name: str) -> None:
        if plugin_id in self.apps.switched_off(self.bot_id):
            raise AppSwitchedOff(plugin_id, name)

    def still_on(self, tools: dict[str, Any]) -> dict[str, Any]:
        """``tools`` without the plugin tools of apps switched off for this owl since."""
        off = self.apps.switched_off(self.bot_id)
        return {
            name: tool
            for name, tool in tools.items()
            if not tool.metadata.get("desktop_connector")
            or tool.metadata.get("plugin_id") not in off
        }

    def call(
        self,
        *,
        bot_id: str,
        task_id: str,
        plugin_id: str,
        tool: str,
        arguments: dict[str, Any],
        approval: str,
        token: Any = None,
        connection_id: str | None = None,
        grant_revision: int | None = None,
    ) -> dict[str, Any]:
        if bot_id != self.bot_id:
            raise PluginCallUnavailable(
                "This plugin call belongs to another owl. Nothing was sent.", dispatched=False
            )
        self.require_on(plugin_id, self._name(plugin_id))
        if plugin_id.startswith("hub."):
            if self.apps.more_apps is None:
                raise PluginCallUnavailable(
                    "More apps aren't available to owls in this service. Nothing was sent.",
                    dispatched=False,
                )
            return self.apps.more_apps.call(
                bot_id,
                plugin_id,
                tool,
                arguments,
                connection_id=connection_id,
                grant_revision=grant_revision,
            )
        return self.apps.hub.call(
            bot_id=bot_id,
            task_id=task_id,
            plugin_id=plugin_id,
            tool=tool,
            arguments=arguments,
            approval=approval,
            token=token,
        )

    def _name(self, plugin_id: str) -> str:
        _, apps = self.apps.hub.catalog()
        named = next((app["name"] for app in apps if app["plugin_id"] == plugin_id), None)
        if named is None:
            named = next(
                (p.name for p in self.apps.more(self.bot_id) if p.plugin_id == plugin_id), plugin_id
            )
        return named

    def need_tools(self, task: dict) -> dict[str, Any]:
        """need_app, while the app answers and some app it named is off here or not connected."""
        from ..agent.tool_definition import ToolDef

        reachable, apps = self.apps.need_catalog(self.bot_id)
        if not reachable:
            return {}
        off = self.apps.switched_off(self.bot_id)
        wanted = [app for app in apps if not app["connected"] or app["plugin_id"] in off]
        if not wanted:
            return {}
        listed = "; ".join(
            f"{app['name']} ({app['plugin_id']}): "
            + ("off for you" if app["connected"] else "not connected in Alysis")
            for app in wanted
        )
        return {
            NEED_TOOL: ToolDef(
                name=NEED_TOOL,
                description=(
                    "Show the person a card in this chat to turn on or connect an app the current "
                    "request needs, instead of using that service's website, another way round or "
                    "saying you can't. Apps you can't use now: "
                    + listed
                    + ". Give the app's id and, in the person's language, one short sentence on "
                    "why the request needs it. Then tell the person in one short line and end "
                    "your reply: the request carries on by itself once they turn it on or connect "
                    "it. Use it only for an app the current request needs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "app": {"type": "string", "enum": [app["plugin_id"] for app in wanted]},
                        "reason": {"type": "string", "maxLength": 300},
                    },
                    "required": ["app", "reason"],
                    "additionalProperties": False,
                },
                run=lambda arguments: self.apps.need(task, arguments),
                metadata={"tool_type": "connector", "app_need": True, "read_only": True},
            )
        }
