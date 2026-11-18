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
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, StrictBool

from ..cloud_connections import ConnectionUnavailable
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


PLUGIN_ID = r"^(alysis|custom)\.[a-z][a-z0-9_-]{0,63}$"


class CatalogPlugin(Input):
    plugin_id: str = Field(pattern=PLUGIN_ID)
    name: str = Field(min_length=1, max_length=120)
    tools: list[CatalogTool] = Field(max_length=96)


class CatalogApp(Input):
    plugin_id: str = Field(pattern=PLUGIN_ID)
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


class AppNeedDecision(Input):
    """The person's answer on an app card: turn it on for the owl, connect it, or not now."""

    action: Literal["turn_on", "connect", "dismiss"]
    expected_revision: int = Field(ge=1)


class NeedInput(Input):
    app: str = Field(pattern=PLUGIN_ID)
    reason: str = Field(default="", max_length=300)


class ResultInput(Input):
    ok: bool
    text: str = Field(default="", max_length=60000)
    is_error: bool = False
    dispatch_state: Literal["not_dispatched", "dispatched", "unknown"] = "dispatched"
    error: str = Field(default="", max_length=1000)


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
    result: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bot_id": self.bot_id,
            "task_id": self.task_id,
            "plugin_id": self.plugin_id,
            "name": self.tool,
            "arguments": self.arguments,
            "approval": self.approval,
        }


class DesktopConnectors:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Condition()
        self._plugins: list[CatalogPlugin] = []
        self._revision = -1
        self._seen: float | None = None
        self._calls: dict[str, _Call] = {}
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
        with self._lock:
            self._seen = self._clock()
            call = self._calls.get(call_id)
            if call is None or not call.claimed or call.result is not None:
                raise NotFound("This plugin call is no longer waiting.")
            call.result = data.model_dump()
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

    def __init__(self, store, owner: str, hub: DesktopConnectors):
        self.store, self.owner, self.hub = store, owner, hub

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
        """Every app the desktop app named, with whether it's connected and on for this owl."""
        reachable, apps = self.hub.catalog()
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
        return {"reachable": reachable, "apps": rows}

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

    # ---- an app a turn needs ------------------------------------------------------------------
    def need(self, task: dict, arguments: dict) -> dict:
        """need_app: show the person a card to turn on or connect the app the request needs."""
        args = NeedInput.model_validate(arguments)
        reachable, apps = self.hub.catalog()
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
                db.execute(
                    "INSERT INTO bot_app_needs(id,bot_id,source_task_id,plugin_id,name,reason,"
                    "state,status,revision,resume_task_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending',1,NULL,?,?)",
                    (
                        uid(),
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
        reachable, apps = self.hub.catalog()
        connected = {app["plugin_id"] for app in apps if app["connected"]} if reachable else set()
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
                ("dismissed", "dismiss"),
                ("connecting", "connect"),
            ):
                return self.public(row)
            if row["status"] not in OPEN_NEEDS or row["revision"] != data.expected_revision:
                raise Conflict("This app request changed. Review its current state.")
            if data.action == "dismiss":
                self._status(db, need_id, "dismissed")
            elif row["plugin_id"] in connected:
                self._resume(db, row, "turned_on" if row["state"] == "off" else "connected")
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
                if row["plugin_id"] not in connected or self._queue_full(db):
                    continue
                self._resume(db, row, "connected")
                resumed = True
        return resumed

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

    def _resume(self, db, need, how: str) -> None:
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

    def plugins(self) -> list[CatalogPlugin]:
        off = self.apps.switched_off(self.bot_id)
        return [plugin for plugin in self.apps.hub.plugins() if plugin.plugin_id not in off]

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
    ) -> dict[str, Any]:
        if bot_id != self.bot_id:
            raise PluginCallUnavailable(
                "This plugin call belongs to another owl. Nothing was sent.", dispatched=False
            )
        self.require_on(plugin_id, self._name(plugin_id))
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
        return next((app["name"] for app in apps if app["plugin_id"] == plugin_id), plugin_id)

    def need_tools(self, task: dict) -> dict[str, Any]:
        """need_app, while the app answers and some app it named is off here or not connected."""
        from ..agent.tools_assembly import ToolDef

        reachable, apps = self.apps.hub.catalog()
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
