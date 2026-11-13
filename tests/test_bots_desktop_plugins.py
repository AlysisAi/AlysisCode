"""Owls use the desktop app's plugins: reads run, own changes follow the mode, others ask."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from test_bots_browser_runtime import ClientFixture, response

from alysis_code.bots import desktop_connectors
from alysis_code.bots.action_review import ApprovalDecision
from alysis_code.bots.desktop_connectors import (
    CatalogInput,
    DesktopConnectors,
    PluginCallUnavailable,
    ResultInput,
)
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.cloud_connections import BoundConnections, ConnectionStore, ConnectionUnavailable

SCHEMA = {"type": "object", "properties": {"content": {"type": "string"}}}
CATALOG = {
    "revision": 1,
    "plugins": [
        {
            "plugin_id": "alysis.todoist",
            "name": "Todoist",
            "tools": [
                {
                    "name": "find-tasks",
                    "description": "Find",
                    "input_schema": SCHEMA,
                    "effect": "read",
                },
                {
                    "name": "add-tasks",
                    "description": "Add",
                    "input_schema": SCHEMA,
                    "effect": "own_change",
                },
                {
                    "name": "add-comments",
                    "description": "Comment",
                    "input_schema": SCHEMA,
                    "effect": "asks",
                },
            ],
        }
    ],
}
READ, OWN, ASKS = (
    "mcp__todoist__find_tasks",
    "mcp__todoist__add_tasks",
    "mcp__todoist__add_comments",
)


class App:
    """The desktop app: polls the hub and answers every call it claims."""

    def __init__(self, hub: DesktopConnectors, *, answer: bool = True):
        self.hub, self.answer, self.calls, self.stop = hub, answer, [], threading.Event()
        hub.publish(CatalogInput(**CATALOG))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            for call in self.hub.next(wait=0.05)["calls"]:
                self.calls.append(call)
                if self.answer:
                    self.hub.answer(call["id"], ResultInput(ok=True, text="fixture result"))

    def close(self):
        self.stop.set()
        self.thread.join(2)


@pytest.fixture
def hub():
    return DesktopConnectors()


def test_a_call_reaches_the_app_and_returns_its_answer(hub):
    app = App(hub)
    try:
        result = hub.call(
            bot_id="b",
            task_id="t",
            plugin_id="alysis.todoist",
            tool="find-tasks",
            arguments={"content": "milk"},
            approval="none",
        )
    finally:
        app.close()
    assert result["ok"] is True and result["text"] == "fixture result"
    (call,) = app.calls
    assert call["name"] == "find-tasks" and call["arguments"] == {"content": "milk"}
    assert call["approval"] == "none" and call["plugin_id"] == "alysis.todoist"


def test_without_the_app_nothing_is_sent_and_no_plugin_is_offered(hub):
    assert hub.plugins() == [] and not hub.available()
    with pytest.raises(PluginCallUnavailable) as refused:
        hub.call(
            bot_id="b",
            task_id="t",
            plugin_id="alysis.todoist",
            tool="find-tasks",
            arguments={},
            approval="none",
        )
    assert refused.value.dispatched is False and "Nothing was sent" in str(refused.value)


def test_plugins_disappear_when_the_app_goes_quiet():
    now = [0.0]
    hub = DesktopConnectors(clock=lambda: now[0])
    hub.publish(CatalogInput(**CATALOG))
    assert [p.name for p in hub.plugins()] == ["Todoist"]
    now[0] = desktop_connectors.DESKTOP_IDLE_S + 1
    assert hub.plugins() == [] and not hub.available()


@pytest.mark.parametrize("claimed", [False, True])
def test_an_unanswered_call_times_out_and_says_whether_it_may_have_run(hub, monkeypatch, claimed):
    monkeypatch.setattr(desktop_connectors, "CALL_TIMEOUT_S", 0.3)
    hub.publish(CatalogInput(**CATALOG))
    app = App(hub, answer=False) if claimed else None
    try:
        with pytest.raises(PluginCallUnavailable) as late:
            hub.call(
                bot_id="b",
                task_id="t",
                plugin_id="alysis.todoist",
                tool="add-tasks",
                arguments={},
                approval="automatic",
            )
    finally:
        if app:
            app.close()
    assert late.value.dispatched is claimed


def test_an_answer_for_an_unknown_call_is_refused(hub):
    with pytest.raises(NotFound):
        hub.answer("a" * 32, ResultInput(ok=True))


def test_owls_get_one_tool_per_plugin_tool_with_its_effect(tmp_path, hub):
    app = App(hub)
    try:
        bound = BoundConnections(
            ConnectionStore(tmp_path / "c.sqlite3"), "owner", "bot:x", None, hub
        )
        tools = bound.tools()
        listed = tools["list_connected_apps"].run({})
    finally:
        app.close()
    assert {READ, OWN, ASKS} <= set(tools)
    assert tools[READ].metadata["effect"] == "read" and tools[READ].metadata["read_only"] is True
    assert tools[OWN].metadata["effect"] == "own_change"
    assert tools[ASKS].metadata["effect"] == "asks"
    assert "approves every call" in tools[ASKS].description
    assert listed["plugins"][0]["plugin"] == "Todoist"
    assert "Plugins in the sidebar" in listed["plugins_note"]
    with pytest.raises(ConnectionUnavailable):
        tools[OWN].run({"content": "x"})  # only the owl's approval step may call a change


def test_main_agents_never_see_desktop_plugins(tmp_path, hub):
    App(hub).close()
    bound = BoundConnections(ConnectionStore(tmp_path / "c.sqlite3"), "owner", "main", None)
    assert set(bound.tools()) == {"list_connected_apps"}


# --- An owl's turn ------------------------------------------------------------------------------


def _task(store: Store):
    bot = store.save_bot("owner", BotInput(name="Helper", purpose="Personal tasks"))
    chat = store.create_chat("owner", bot["id"], "Conversation")
    return store.enqueue(
        "owner", chat["id"], TaskInput(message="Handle my Todoist", request_id="todoist-request")
    )


def _runtime(store: Store, tmp_path, hub, answers):
    client = ClientFixture(answers)
    connections = ConnectionStore(tmp_path / "connections.sqlite3")
    runtime = BotRuntime(
        store,
        "owner",
        lambda _: client,
        connections=lambda bot_id: BoundConnections(
            connections, "owner", "bot:" + bot_id, None, hub
        ),
    )
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.action_review_enabled = True
    return runtime, client


def _review(decision: str):
    import json

    return response(
        json.dumps(
            {"decision": decision, "risk": "low", "user_authorization": "high", "reason": "Fixture"}
        )
    )


def test_an_owl_reads_through_a_plugin_without_asking(tmp_path, hub):
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    app = App(hub)
    try:
        runtime, client = _runtime(
            store,
            tmp_path,
            hub,
            [response(name=READ, arguments={"content": "milk"}), response("Found it")],
        )
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert [c["approval"] for c in app.calls] == ["none"]
    assert len(client.requests) == 2  # no reviewer call for a read
    tools = {schema["function"]["name"] for schema in client.requests[0]["tools"]}
    assert {READ, OWN, ASKS} <= tools
    system = client.requests[0]["messages"][0]["content"]
    assert "Connected plugins" in system


def test_an_own_change_follows_the_automatic_reviewer(tmp_path, hub):
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    app = App(hub)
    try:
        runtime, client = _runtime(
            store,
            tmp_path,
            hub,
            [
                response(name=OWN, arguments={"content": "Buy milk"}),
                _review("allow"),
                response("Added"),
            ],
        )
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert [c["approval"] for c in app.calls] == ["automatic"]


def test_a_change_that_reaches_people_asks_the_person_every_time(tmp_path, hub):
    store = Store(tmp_path / "bots.sqlite3")
    task = _task(store)
    app = App(hub)
    try:
        runtime, client = _runtime(
            store, tmp_path, hub, [response(name=ASKS, arguments={"content": "done"})]
        )
        runtime.run(store.claim("owner"), runtime.task_token(task["id"]))
        assert store.task("owner", task["id"])["status"] == "waiting"
        assert len(client.requests) == 1, "the automatic reviewer never answers it"
        assert app.calls == []
        (review,) = runtime.action_reviews.list(task["bot_id"])
        assert review["action"]["observed_context"]["always_ask"] is True
        assert review["action"]["observed_context"]["plugin"] == "Todoist"
        with pytest.raises(Conflict):
            runtime.action_reviews.decide(
                task["bot_id"],
                review["id"],
                ApprovalDecision(action="always_allow", expected_revision=review["revision"]),
            )
        runtime.action_reviews.decide(
            task["bot_id"],
            review["id"],
            ApprovalDecision(action="allow", expected_revision=review["revision"]),
        )
        resumed, _ = _runtime(
            store,
            tmp_path,
            hub,
            [response(name=ASKS, arguments={"content": "done"}), response("Commented")],
        )
        claimed = resumed.background.claim()
        resumed.run(claimed, resumed.task_token(task["id"]), background=True)
    finally:
        app.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert [c["approval"] for c in app.calls] == ["person"]
