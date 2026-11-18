"""Each owl uses only the connected apps the person leaves on for it, and asks for the others."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest

from alysis_code.bots.desktop_connectors import (
    AppNeedDecision,
    CatalogInput,
    DesktopConnectors,
    OwlApps,
    PluginChoice,
    ResultInput,
    app_ready_request,
)
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, Store
from alysis_code.cloud_connections import BoundConnections, ConnectionStore
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}}


def plugin(plugin_id: str, name: str, tools: list[tuple[str, str]]) -> dict:
    return {
        "plugin_id": plugin_id,
        "name": name,
        "tools": [
            {"name": tool, "description": tool, "input_schema": SCHEMA, "effect": effect}
            for tool, effect in tools
        ],
    }


# Two connected apps, and one in Alysis's catalog that isn't connected. The names are the
# person's own (a custom app keeps whatever name it was given), so one is in Greek.
CATALOG = {
    "revision": 1,
    "plugins": [
        plugin("alysis.todoist", "Todoist", [("find-tasks", "read"), ("add-tasks", "own_change")]),
        plugin("custom.notes", "Σημειώσεις", [("search", "read")]),
    ],
    "apps": [
        {"plugin_id": "alysis.todoist", "name": "Todoist"},
        {"plugin_id": "custom.notes", "name": "Σημειώσεις"},
        {"plugin_id": "alysis.linear", "name": "Linear"},
    ],
}
TODOIST_READ, TODOIST_ADD = "mcp__todoist__find_tasks", "mcp__todoist__add_tasks"
NOTES_SEARCH = "mcp__custom_notes__search"


class DesktopApp:
    """The desktop app: polls the hub and answers each call it claims."""

    def __init__(self, hub: DesktopConnectors, catalog: dict = CATALOG, on_call=None):
        self.hub, self.calls, self.stop, self.on_call = hub, [], threading.Event(), on_call
        hub.publish(CatalogInput(**catalog))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            for call in self.hub.next(wait=0.05)["calls"]:
                self.calls.append(call)
                if self.on_call:
                    self.on_call(call)
                self.hub.answer(call["id"], ResultInput(ok=True, text="fixture result"))

    def close(self):
        self.stop.set()
        self.thread.join(2)


class Client:
    model = "owl-apps-fixture"
    supports_tool_calling = True

    def __init__(self, responses, before=None):
        self.responses, self.requests, self.before = iter(responses), [], before

    def chat(self, **kwargs):
        self.requests.append(
            {"messages": copy.deepcopy(kwargs["messages"]), "tools": kwargs["tools"] or []}
        )
        if self.before:
            self.before(len(self.requests))
        return next(self.responses)


def say(content="Done", *, name=None, arguments=None):
    calls = [ToolCall("call-" + uuid4().hex[:8], name, arguments or {})] if name else []
    return LLMResponse(content, calls, {}, usage=LLMUsage(10, 5, 15))


def tool_results(request) -> list[str]:
    return [m["content"] for m in request["messages"] if m["role"] == "tool"]


def offered(request) -> set[str]:
    return {tool["function"]["name"] for tool in request["tools"]}


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    hub = DesktopConnectors()
    apps = OwlApps(store, "owner", hub)
    connections = ConnectionStore(tmp_path / "connections.sqlite3")
    owl = store.save_bot("owner", BotInput(name="Ήρα", purpose="Everyday work"))
    other = store.save_bot("owner", BotInput(name="Atlas", purpose="Other work"))
    chat = store.create_chat("owner", owl["id"], "Conversation")

    def runtime(client):
        made = BotRuntime(
            store,
            "owner",
            lambda _: client,
            connections=lambda bot_id: BoundConnections(
                connections, "owner", "bot:" + bot_id, None, apps.for_owl(bot_id)
            ),
        )
        made.limits = replace(made.limits, compaction_enabled=False)
        made.action_review_enabled = True
        return made

    return store, hub, apps, owl, other, chat, runtime


def switch(apps, bot_id, plugin_id, enabled):
    current = next(app for app in apps.listing(bot_id)["apps"] if app["plugin_id"] == plugin_id)
    return apps.choose(
        bot_id,
        PluginChoice(plugin_id=plugin_id, enabled=enabled, expected_revision=current["revision"]),
    )


def ask(store, chat, message="Βρες τις εργασίες μου 今日"):
    return store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=uuid4().hex))


# --- choices per owl ------------------------------------------------------------------------------


def test_every_connected_app_starts_on_and_each_owl_keeps_its_own_choice(env):
    store, hub, apps, owl, other, _, _ = env
    hub.publish(CatalogInput(**CATALOG))
    listing = apps.listing(owl["id"])
    assert listing["reachable"] is True
    assert [(a["name"], a["connected"], a["enabled"]) for a in listing["apps"]] == [
        ("Todoist", True, True),
        ("Σημειώσεις", True, True),
        ("Linear", False, True),
    ]

    switch(apps, owl["id"], "alysis.todoist", False)

    assert [p.name for p in apps.for_owl(owl["id"]).plugins()] == ["Σημειώσεις"]
    assert [p.name for p in apps.for_owl(other["id"]).plugins()] == ["Todoist", "Σημειώσεις"]
    todoist = apps.listing(owl["id"])["apps"][0]
    assert (todoist["enabled"], todoist["revision"]) == (False, 1)
    # A change made from a stale view is refused instead of overwriting the newer choice.
    with pytest.raises(Conflict):
        apps.choose(
            owl["id"],
            PluginChoice(plugin_id="alysis.todoist", enabled=True, expected_revision=0),
        )
    switch(apps, owl["id"], "alysis.todoist", True)
    assert [p.name for p in apps.for_owl(owl["id"]).plugins()] == ["Todoist", "Σημειώσεις"]


def test_an_owls_choices_leave_with_it(env):
    store, hub, apps, owl, _, _, _ = env
    hub.publish(CatalogInput(**CATALOG))
    switch(apps, owl["id"], "alysis.todoist", False)
    store.delete_bot("owner", owl["id"])
    with store.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM bot_plugin_grants WHERE bot_id=?", (owl["id"],)
        ).fetchone()


def test_an_older_desktop_app_without_an_app_list_keeps_every_plugin_on(env):
    """An app that sends only its connected plugins: owls use them all, as before."""
    store, hub, apps, owl, _, _, _ = env
    older = {key: value for key, value in CATALOG.items() if key != "apps"}
    hub.publish(CatalogInput(**older))
    assert [p.name for p in apps.for_owl(owl["id"]).plugins()] == ["Todoist", "Σημειώσεις"]
    assert [a["name"] for a in apps.listing(owl["id"])["apps"]] == ["Todoist", "Σημειώσεις"]
    assert apps.for_owl(owl["id"]).need_tools({"id": "t", "bot_id": owl["id"]}) == {}


def test_the_hub_alone_still_offers_every_plugin_to_every_owl(tmp_path):
    """Services that pass the hub itself (as before this change) keep account-wide plugins."""
    hub = DesktopConnectors()
    hub.publish(CatalogInput(**CATALOG))
    bound = BoundConnections(ConnectionStore(tmp_path / "c.sqlite3"), "o", "bot:x", None, hub)
    assert {TODOIST_READ, TODOIST_ADD, NOTES_SEARCH} <= set(bound.tools())


# --- tools offered in every kind of turn -----------------------------------------------------------


def test_a_chat_turn_is_offered_only_the_apps_on_for_its_owl(env):
    store, hub, apps, owl, _, chat, runtime = env
    app = DesktopApp(hub)
    try:
        switch(apps, owl["id"], "alysis.todoist", False)
        task = ask(store, chat)
        client = Client([say(name=NOTES_SEARCH, arguments={"query": "milk"}), say("Found")])
        runtime(client).run(store.claim("owner"), runtime(client).task_token(task["id"]))
    finally:
        app.close()
    tools = offered(client.requests[0])
    assert NOTES_SEARCH in tools
    assert not {TODOIST_READ, TODOIST_ADD} & tools
    assert [call["plugin_id"] for call in app.calls] == ["custom.notes"]
    listed = next(t for t in client.requests[0]["tools"] if t["function"]["name"] == "need_app")
    assert listed["function"]["parameters"]["properties"]["app"]["enum"] == [
        "alysis.todoist",
        "alysis.linear",
    ]
    assert "Todoist (alysis.todoist): off for you" in listed["function"]["description"]
    assert "Linear (alysis.linear): not connected" in listed["function"]["description"]


def test_a_routine_run_is_offered_only_the_apps_on_for_its_owl(env):
    store, hub, apps, owl, _, _, runtime = env
    store.save_schedule(
        "owner",
        owl["id"],
        ScheduleInput.model_validate(
            {
                "name": "Πρωινή σύνοψη",
                "prompt": "Σύνοψη των εργασιών μου",
                "timing": {"kind": "interval", "every_minutes": 60},
            }
        ),
        now=1000,
    )
    app = DesktopApp(hub)
    try:
        switch(apps, owl["id"], "custom.notes", False)
        assert store.enqueue_due_schedules("owner", max_calls=None, now=1000 + 3600) == 1
        run = store.claim("owner")
        assert store.schedule_context("owner", run["id"])
        client = Client([say("Summary")])
        runtime(client).run(run, runtime(client).task_token(run["id"]))
    finally:
        app.close()
    tools = offered(client.requests[0])
    assert {TODOIST_READ, TODOIST_ADD} <= tools
    assert NOTES_SEARCH not in tools


def test_an_exchange_with_another_owl_never_offers_the_switched_off_app(env):
    """The asking owl's own turn is filtered, the other owl gets no tools, nor does the report."""
    store, hub, apps, owl, other, chat, runtime = env
    from alysis_code.bots.cooperation import PeerRunner

    app = DesktopApp(hub)
    seen = []

    class Peer:
        model, supports_tool_calling = "peer-fixture", True

        def chat(self, **kwargs):
            seen.append({"tools": kwargs["tools"] or [], "messages": kwargs["messages"]})
            if kwargs["messages"][0]["content"].startswith("You are the receiving peer"):
                return LLMResponse("Το είδα.", [], {})
            if len(seen) == 1:
                return say(
                    name="request_peer",
                    arguments={"recipient_bot_id": other["id"], "brief": "Έλεγξε τη λίστα"},
                )
            return say("Atlas checked it.")

    try:
        switch(apps, owl["id"], "alysis.todoist", False)
        task = ask(store, chat, "Ask Atlas to check my list")
        made = runtime(Peer())
        made.run(store.claim("owner"), made.task_token(task["id"]))
        assert store.task("owner", task["id"])["status"] == "waiting"
        PeerRunner(made.peers, made.client_factory, made.limits, threading.Event()).run(
            made.peers.claim()
        )
        resumed = store.claim("owner")
        made.run(resumed, made.task_token(resumed["id"]))
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    asking, peer, report = seen
    assert NOTES_SEARCH in offered(asking) and TODOIST_READ not in offered(asking)
    assert peer["tools"] == [] and report["tools"] == []
    assert app.calls == []


def test_switching_an_app_off_mid_task_takes_effect_from_the_next_step(env):
    store, hub, apps, owl, _, chat, runtime = env
    app = DesktopApp(hub, on_call=lambda call: switch(apps, owl["id"], "alysis.todoist", False))
    try:
        task = ask(store, chat)
        client = Client(
            [say(name=TODOIST_READ, arguments={"query": "σήμερα"}), say("Βρήκα τρεις εργασίες.")]
        )
        made = runtime(client)
        made.run(store.claim("owner"), made.task_token(task["id"]))
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert TODOIST_READ in offered(client.requests[0])
    assert not {TODOIST_READ, TODOIST_ADD} & offered(client.requests[1])
    assert NOTES_SEARCH in offered(client.requests[1])


def test_a_step_offered_before_the_switch_is_refused_with_a_plain_note(env):
    store, hub, apps, owl, _, chat, runtime = env
    app = DesktopApp(hub)

    def before(step):
        if step == 1:  # The person switches it off while the model is choosing this step.
            switch(apps, owl["id"], "alysis.todoist", False)

    try:
        task = ask(store, chat)
        client = Client(
            [say(name=TODOIST_ADD, arguments={"query": "γάλα"}), say("Δεν το πρόσθεσα.")],
            before=before,
        )
        made = runtime(client)
        made.run(store.claim("owner"), made.task_token(task["id"]))
    finally:
        app.close()
    assert app.calls == [], "nothing reaches the app"
    assert len(client.requests) == 2, "no reviewer was asked about a refused change"
    failed = [e for e in store.task("owner", task["id"])["events"] if e["kind"] == "tool_failed"]
    assert failed[0]["data"]["refusal"] == {
        "code": "app_off",
        "plugin_id": "alysis.todoist",
        "name": "Todoist",
    }
    note = json.loads(tool_results(client.requests[1])[-1])
    assert "Todoist was switched off for you" in note["error"]


# --- asking for an app ----------------------------------------------------------------------------


def needs(store, chat, task_id):
    tasks = store.conversation("owner", chat["id"])["tasks"]
    return next(task for task in tasks if task["id"] == task_id)["app_needs"]


def test_an_owl_asks_for_an_app_that_is_off_with_one_card_and_carries_on_when_turned_on(env):
    store, hub, apps, owl, _, chat, runtime = env
    app = DesktopApp(hub)
    try:
        switch(apps, owl["id"], "alysis.todoist", False)
        task = ask(store, chat, "Πρόσθεσε «γάλα» στο Todoist")
        reason = "Για να προσθέσω την εργασία στη λίστα σου."
        client = Client(
            [
                say(name="need_app", arguments={"app": "alysis.todoist", "reason": reason}),
                say("Χρειάζομαι το Todoist για αυτό."),
            ]
        )
        made = runtime(client)
        made.run(store.claim("owner"), made.task_token(task["id"]))
        (card,) = needs(store, chat, task["id"])
        assert (card["plugin_id"], card["name"], card["state"], card["status"]) == (
            "alysis.todoist",
            "Todoist",
            "off",
            "pending",
        )
        assert card["reason"] == reason
        receipt = json.loads(tool_results(client.requests[1])[-1])
        assert receipt["status"] == "card_shown"
        (item,) = [i for i in store.needs_you("owner")["items"] if i["kind"] == "app"]
        assert (item["id"], item["text"], item["state"]) == (card["id"], "Todoist", "off")

        decided = apps.decide(
            owl["id"], card["id"], AppNeedDecision(action="turn_on", expected_revision=1)
        )
        assert decided["status"] == "resumed" and decided["resume_task_id"]
        # Pressing again (a resent request) changes nothing.
        again = apps.decide(
            owl["id"], card["id"], AppNeedDecision(action="turn_on", expected_revision=1)
        )
        assert again["resume_task_id"] == decided["resume_task_id"]
        carried = store.claim("owner")
        assert carried["id"] == decided["resume_task_id"]
        assert carried["message"] == "" and carried["request_id"] == "app_ready_" + card["id"]
        assert carried["config"]["app_ready"] == {
            "plugin_id": "alysis.todoist",
            "name": "Todoist",
            "how": "turned_on",
        }
        follow = Client([say(name=TODOIST_READ, arguments={"query": "γάλα"}), say("Το βρήκα.")])
        resumed = runtime(follow)
        resumed.run(carried, resumed.task_token(carried["id"]))
    finally:
        app.close()
    assert TODOIST_ADD in offered(follow.requests[0])
    assert [call["name"] for call in app.calls] == ["find-tasks"]
    request = [m for m in follow.requests[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert request == app_ready_request(carried["config"])
    assert '"Todoist"' in request and "turned on" in request
    assert [i for i in store.needs_you("owner")["items"] if i["kind"] == "app"] == []
    assert [m["role"] for m in store.conversation("owner", chat["id"])["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]


def test_connecting_an_app_carries_the_request_on_once_the_app_says_it_is_connected(env):
    store, hub, apps, owl, _, chat, runtime = env
    app = DesktopApp(hub)
    try:
        task = ask(store, chat, "Άνοιξε το θέμα στο Linear")
        client = Client(
            [
                say(name="need_app", arguments={"app": "alysis.linear", "reason": "Το θέμα."}),
                say("Χρειάζομαι το Linear."),
            ]
        )
        made = runtime(client)
        made.run(store.claim("owner"), made.task_token(task["id"]))
        (card,) = needs(store, chat, task["id"])
        assert card["state"] == "not_connected"
        with pytest.raises(Conflict):
            apps.decide(
                owl["id"], card["id"], AppNeedDecision(action="turn_on", expected_revision=1)
            )
        waiting = apps.decide(
            owl["id"], card["id"], AppNeedDecision(action="connect", expected_revision=1)
        )
        assert waiting["status"] == "connecting"
        assert apps.resume_connected() is False and store.claim("owner") is None
        # The person finishes connecting it: the app's next catalog names its tools.
        connected = copy.deepcopy(CATALOG)
        connected["plugins"].append(plugin("alysis.linear", "Linear", [("get_issue", "read")]))
        hub.publish(CatalogInput(**connected))
        assert apps.resume_connected() is True
    finally:
        app.close()
    (card,) = needs(store, chat, task["id"])
    assert card["status"] == "resumed"
    carried = store.claim("owner")
    assert carried["config"]["app_ready"]["how"] == "connected"
    assert "connected" in app_ready_request(carried["config"])


def test_a_card_is_decided_from_the_apps_id_never_from_the_message(env):
    """The person names the app in the message; only the owl's need_app call makes a card."""
    store, hub, apps, owl, _, chat, runtime = env
    app = DesktopApp(hub)
    try:
        switch(apps, owl["id"], "alysis.todoist", False)
        task = ask(store, chat, "Χρησιμοποίησε το Todoist και το Linear")
        client = Client([say("Δεν μπορώ τώρα.")])
        made = runtime(client)
        made.run(store.claim("owner"), made.task_token(task["id"]))
    finally:
        app.close()
    assert needs(store, chat, task["id"]) == []
    with pytest.raises(ValueError):
        apps.need({"id": task["id"], "bot_id": owl["id"]}, {"app": "Todoist", "reason": ""})


def test_not_now_closes_the_card_and_the_owl_is_told_if_it_asks_again(env):
    store, hub, apps, owl, _, chat, runtime = env
    hub.publish(CatalogInput(**CATALOG))
    switch(apps, owl["id"], "alysis.todoist", False)
    task = ask(store, chat)
    first = apps.need(task, {"app": "alysis.todoist", "reason": "Λίστα"})
    assert first["status"] == "card_shown"
    (card,) = needs(store, chat, task["id"])
    apps.decide(owl["id"], card["id"], AppNeedDecision(action="dismiss", expected_revision=1))
    assert apps.need(task, {"app": "alysis.todoist", "reason": "Λίστα"})["status"] == "declined"
    with pytest.raises(Conflict):
        apps.decide(owl["id"], card["id"], AppNeedDecision(action="turn_on", expected_revision=1))
    assert [i for i in store.needs_you("owner")["items"] if i["kind"] == "app"] == []


def test_an_app_already_on_or_out_of_reach_shows_no_card(env, monkeypatch):
    store, hub, apps, owl, _, chat, _ = env
    hub.publish(CatalogInput(**CATALOG))
    task = ask(store, chat)
    assert apps.need(task, {"app": "alysis.todoist", "reason": ""})["status"] == "on"
    from alysis_code.bots import desktop_connectors

    monkeypatch.setattr(desktop_connectors, "DESKTOP_IDLE_S", -1)
    assert apps.need(task, {"app": "alysis.linear", "reason": ""})["status"] == "unavailable"
    assert apps.for_owl(owl["id"]).need_tools(task) == {}
    assert needs(store, chat, task["id"]) == []


def test_a_newer_request_in_the_chat_is_not_overtaken_by_the_carry_on(env):
    store, hub, apps, owl, _, chat, _ = env
    hub.publish(CatalogInput(**CATALOG))
    switch(apps, owl["id"], "alysis.todoist", False)
    task = ask(store, chat)
    apps.need(task, {"app": "alysis.todoist", "reason": "Λίστα"})
    store.finish("owner", store.claim("owner")["id"], "completed", content="Χρειάζομαι το Todoist.")
    ask(store, chat, "Άσ' το, κάτι άλλο")
    (card,) = needs(store, chat, task["id"])
    decided = apps.decide(
        owl["id"], card["id"], AppNeedDecision(action="turn_on", expected_revision=1)
    )
    assert decided["status"] == "resumed" and decided["resume_task_id"] is None
    assert apps.listing(owl["id"])["apps"][0]["enabled"] is True


def test_a_database_from_before_the_choices_opens_with_every_app_on(tmp_path):
    store = Store(tmp_path / "bots.db")
    owl = store.save_bot("owner", BotInput(name="Ήρα", purpose=""))
    with store.connect(write=True) as db:
        db.execute("DROP TABLE bot_app_needs")
        db.execute("DROP TABLE bot_plugin_grants")
        db.execute("PRAGMA user_version=41")
    store = Store(tmp_path / "bots.db")
    hub = DesktopConnectors()
    hub.publish(CatalogInput(**CATALOG))
    apps = OwlApps(store, "owner", hub)
    assert [p.name for p in apps.for_owl(owl["id"]).plugins()] == ["Todoist", "Σημειώσεις"]
    switch(apps, owl["id"], "custom.notes", False)
    assert [p.name for p in apps.for_owl(owl["id"]).plugins()] == ["Todoist"]


def test_turning_on_an_app_that_was_disconnected_meanwhile_offers_to_connect_it(env):
    store, hub, apps, owl, _, chat, _ = env
    hub.publish(CatalogInput(**CATALOG))
    switch(apps, owl["id"], "custom.notes", False)
    task = ask(store, chat)
    apps.need(task, {"app": "custom.notes", "reason": "Οι σημειώσεις σου"})
    (card,) = needs(store, chat, task["id"])
    gone = copy.deepcopy(CATALOG)
    gone["plugins"] = [p for p in gone["plugins"] if p["plugin_id"] != "custom.notes"]
    hub.publish(CatalogInput(**gone))
    changed = apps.decide(
        owl["id"], card["id"], AppNeedDecision(action="turn_on", expected_revision=1)
    )
    assert (changed["state"], changed["status"], changed["revision"]) == (
        "not_connected",
        "pending",
        2,
    )
    assert [t["id"] for t in store.conversation("owner", chat["id"])["tasks"]] == [task["id"]]
    assert apps.for_owl(owl["id"]).need_tools(task)  # still off for the owl: nothing turned on
    assert "custom.notes" in apps.switched_off(owl["id"])
