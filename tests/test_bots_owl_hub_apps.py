"""Owls on this computer use the account's More apps (hub plugins, owl_hub_apps.py).

They're offered with each tool's own effect, switched per owl like any app, and called by the
service itself with the account's key: the desktop app takes no part.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_bots_owl_apps import Client, ask, offered, say, switch, tool_results

from alysis_code import hub_apps
from alysis_code.bots.action_review import ApprovalDecision
from alysis_code.bots.desktop_connectors import (
    CatalogInput,
    DesktopConnectors,
    OwlApps,
    OwlHubGrant,
    PluginCallUnavailable,
)
from alysis_code.bots.models import BotInput
from alysis_code.bots.owl_hub_apps import OwlHubApps
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cloud_connections import BoundConnections, ConnectionStore
from alysis_code.mcp.plugin_hub import bind_hub_catalog, hub_tool_name

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def catalog(app: str, name: str, tools: list[tuple[str, dict]], *, tested: bool) -> dict:
    return bind_hub_catalog(
        {
            "protocol_version": 1,
            "plugin_id": f"hub.{app}",
            "server_id": f"hub.{app}/hub",
            "catalog_id": app,
            "name": name,
            "tested": tested,
            "tools": [
                {"name": tool, "description": tool, "inputSchema": SCHEMA, "hints": hints}
                for tool, hints in tools
            ],
        }
    )


MAIL = catalog(
    "gmail",
    "Gmail",
    [("GMAIL_FETCH_EMAILS", {"readOnly": True}), ("GMAIL_SEND_EMAIL", {"create": True})],
    tested=True,
)
FETCH = hub_tool_name("hub.gmail", "GMAIL_FETCH_EMAILS")
SEND = hub_tool_name("hub.gmail", "GMAIL_SEND_EMAIL")


class Account:
    """The account's client: what the service sends to Alysis's apps function."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.error: hub_apps.HubAppsError | None = None
        self.sent = True
        self.connection_id = "ca_work"
        self.revision = 0

    def connections(self, *, manage=False):
        return {}

    def set_owl_grant(self, app, grant):
        assert app == "gmail"
        self.connection_id = grant["connection_id"]
        self.revision += 1

    def call(self, app, tool, arguments, request_id=None, connection_id=None, grant_revision=None):
        self.calls.append((app, tool, arguments))
        self.last_connection = connection_id
        assert (connection_id, grant_revision) == (self.connection_id, self.revision)
        if self.error:
            raise self.error
        return hub_apps.HubCallResult(
            is_error=False,
            text=json.dumps({"messages": ["Γεια σου"]}, ensure_ascii=False),
            sent=self.sent,
        )


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    account = Account()
    loads: list[int] = []

    def load(client):
        loads.append(1)
        return [
            {**MAIL, "connection_id": client.connection_id, "grant_revision": client.revision}
        ], {"loaded": 1}

    more = OwlHubApps(load=load, client=lambda **kwargs: account)
    apps = OwlApps(store, "owner", DesktopConnectors(), more)
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

    return store, account, apps, owl, other, chat, runtime, loads


def test_a_connected_more_app_is_on_for_every_owl_until_switched_off_for_one(env):
    store, _, apps, owl, other, _, _, loads = env
    listing = apps.listing(owl["id"])
    assert [(a["plugin_id"], a["name"], a["connected"], a["enabled"]) for a in listing["apps"]] == [
        ("hub.gmail", "Gmail", True, True)
    ]
    switch(apps, owl["id"], "hub.gmail", False)
    assert apps.for_owl(owl["id"]).plugins() == []
    assert [p.name for p in apps.for_owl(other["id"]).plugins()] == ["Gmail"]
    assert len(loads) == 3, "fresh discovery checks the broker's current grants for each owl"


def test_a_read_runs_in_the_service_with_no_desktop_app(env):
    store, account, _, _, _, chat, runtime, _ = env
    task = ask(store, chat)
    client = Client([say(name=FETCH, arguments={"q": "τιμολόγια"}), say("Found")])
    made = runtime(client)
    made.run(store.claim("owner"), made.task_token(task["id"]))
    assert {FETCH, SEND} <= offered(client.requests[0])
    described = next(t for t in client.requests[0]["tools"] if t["function"]["name"] == FETCH)
    assert "More apps" in described["function"]["description"]
    assert account.calls == [("gmail", "GMAIL_FETCH_EMAILS", {"q": "τιμολόγια"})]
    assert "Γεια σου" in json.loads(tool_results(client.requests[1])[-1])["result"]
    assert store.task("owner", task["id"])["status"] == "completed"


def test_a_change_waits_for_the_person_and_runs_once_they_allow_it(env):
    store, account, _, _, _, chat, runtime, _ = env
    task = ask(store, chat, "Στείλε το email")
    made = runtime(Client([say(name=SEND, arguments={"q": "Γεια"})]))
    made.run(store.claim("owner"), made.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert account.calls == [], "nothing is sent before the person decides"
    (review,) = made.action_reviews.list(task["bot_id"])
    assert review["action"]["observed_context"]["plugin"] == "Gmail"
    assert review["action"]["observed_context"]["connection_id"] == "ca_work"
    assert review["action"]["observed_context"]["always_ask"] is True
    made.action_reviews.decide(
        task["bot_id"],
        review["id"],
        ApprovalDecision(action="allow", expected_revision=review["revision"]),
    )
    resumed = runtime(Client([say(name=SEND, arguments={"q": "Γεια"}), say("Sent")]))
    resumed.run(resumed.background.claim(), resumed.task_token(task["id"]), background=True)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert account.calls == [("gmail", "GMAIL_SEND_EMAIL", {"q": "Γεια"})]
    assert account.last_connection == "ca_work"


def test_pending_owl_approval_cannot_move_to_another_account_with_the_same_label(env):
    store, account, apps, owl, _, chat, runtime, _ = env
    task = ask(store, chat)
    request = say(name=SEND, arguments={"q": "approved only for work"})
    made = runtime(Client([request]))
    made.run(store.claim("owner"), made.task_token(task["id"]))
    (review,) = made.action_reviews.list(owl["id"])
    made.action_reviews.decide(
        owl["id"],
        review["id"],
        ApprovalDecision(action="allow", expected_revision=review["revision"]),
    )
    apps.set_hub_grant(
        owl["id"],
        "gmail",
        OwlHubGrant(connection_id="ca_home", mode="actions", expected_revision=0),
    )
    resumed = runtime(Client([request]))
    resumed.run(resumed.background.claim(), resumed.task_token(task["id"]), background=True)
    assert account.calls == []
    assert store.task("owner", task["id"])["status"] == "waiting"
    reviews = made.action_reviews.list(owl["id"])
    assert any(
        r["action"]["observed_context"]["connection_id"] == "ca_home"
        and r["action"]["observed_context"]["grant_revision"] == 1
        for r in reviews
    )


def test_a_refusal_says_whether_anything_was_sent(env):
    _, account, apps, owl, _, _, _, _ = env
    more = apps.more_apps
    account.error = hub_apps.HubAppsError("allowance_used")

    def read():
        return more.call(
            owl["id"],
            "hub.gmail",
            "GMAIL_FETCH_EMAILS",
            {},
            connection_id="ca_work",
            grant_revision=0,
        )

    used = read()
    assert (used["ok"], used["dispatch_state"]) == (False, "not_dispatched")
    assert "Nothing was sent" in used["error"]
    account.error = hub_apps.HubAppsError("offline")
    assert read()["dispatch_state"] == "unknown"
    account.error, account.sent = None, False
    refused = read()
    assert (refused["ok"], refused["dispatch_state"]) == (False, "not_dispatched")
    calls = len(account.calls)
    gone = more.call(owl["id"], "hub.slack", "SLACK_SEND", {})
    assert (gone["ok"], gone["dispatch_state"]) == (False, "not_dispatched")
    assert len(account.calls) == calls, "an app that isn't connected is never called"


def test_a_switched_off_app_and_a_service_without_an_account_send_nothing(env, tmp_path):
    _, account, apps, owl, _, _, _, _ = env
    switch(apps, owl["id"], "hub.gmail", False)
    with pytest.raises(PluginCallUnavailable):
        apps.for_owl(owl["id"]).call(
            bot_id=owl["id"],
            task_id="t",
            plugin_id="hub.gmail",
            tool="GMAIL_FETCH_EMAILS",
            arguments={},
            approval="none",
        )
    hosted = OwlApps(Store(tmp_path / "hosted.sqlite3"), "owner", DesktopConnectors())
    with pytest.raises(PluginCallUnavailable):
        hosted.for_owl("b").call(
            bot_id="b",
            task_id="t",
            plugin_id="hub.gmail",
            tool="GMAIL_FETCH_EMAILS",
            arguments={},
            approval="none",
        )
    assert account.calls == []


def test_the_desktop_app_can_never_publish_a_more_app():
    with pytest.raises(ValidationError):
        CatalogInput(
            revision=1,
            plugins=[{"plugin_id": "hub.gmail", "name": "Gmail", "tools": []}],
        )
