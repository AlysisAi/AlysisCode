"""A hosted Owl checks Gmail through the production HTTP client; only the broker wire is synthetic."""

import io
import json
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_bots_owl_apps import Client, offered, say, tool_results

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.app_events import poll_one
from alysis_code.bots.desktop_connectors import AppNeedDecision, DesktopConnectors, OwlApps
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_hub_apps import OwlHubApps
from alysis_code.bots.store import Conflict, Store
from alysis_code.hub_apps import HubAppsClient, HubCallResult


def test_hosted_gmail_review_pins_account_and_recovers_pagination(tmp_path):
    settings = Settings(
        tmp_path / "cloud.db",
        "t" * 40,
        "example",
        "https://model.invalid",
        "example",
        owner="account:fixture",
        service_mode="cloud",
    )
    account = {
        "id": "ca_work",
        "app": "gmail",
        "selected": True,
        "status": "active",
        "label": "Sales",
        "grant_revision": 3,
    }
    calls, pages = [], []

    def opener(request, timeout):
        assert request.get_header("Authorization") == "Bearer synthetic-delegation"
        url = urlsplit(request.full_url)
        assert url.hostname == "broker.invalid" and f"/owls/{owl}/" in url.path
        if url.path.endswith("/connections"):
            body = {"connections": [account], "account_selection": 1}
            if url.query == "manage=1":
                body["hub_accounts"] = [
                    {
                        "plugin_id": "hub.gmail",
                        "catalog_id": "gmail",
                        "name": "Gmail",
                        "accounts": [account],
                        "grant": {"mode": "read", "connection_id": account["id"], "revision": 3},
                    }
                ]
        elif url.path.endswith("/tools"):
            assert url.path.endswith("/" + account["id"] + "/tools")
            body = {
                "id": "hub.gmail",
                "name": "Gmail",
                "catalog_id": "gmail",
                "connection_id": account["id"],
                "grant_revision": 3,
                "tested": True,
                "tools": [
                    {"name": tool, "hints": {"readOnly": True}}
                    for tool in ("GMAIL_FETCH_EMAILS", "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID")
                ],
            }
        else:
            assert url.path.endswith("/ca_work/call")
            data = json.loads(request.data)
            assert data["grant_revision"] == 3 and data["app"] == "gmail"
            if data["tool"] == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
                assert data["arguments"] == {"message_id": "111"}
                response = {"messageText": "Delivery arrives Friday."}
            else:
                assert data["tool"] == "GMAIL_FETCH_EMAILS"
                calls.append(data["arguments"])
                response = pages.pop(0)
            body = {"is_error": False, "text": json.dumps(response), "sent": True}
        return io.BytesIO(json.dumps({"schema_version": 1, **body}).encode())

    def client(**options):
        return HubAppsClient(
            "synthetic-delegation", url="https://broker.invalid/apps", opener=opener, **options
        )

    model = Client(
        [
            say(name="read_event_email", arguments={"message_id": "111"}),
            say("The delivery arrives Friday."),
        ]
    )

    def service():
        return create_app(
            settings,
            more_apps=OwlHubApps(client=client, manage_client=client),
            client_factory=lambda _: model,
        )

    app = service()
    http = TestClient(app, headers={"Authorization": "Bearer " + settings.token})
    owl = http.post("/v1/bots", json={"name": "Sales", "purpose": "Review email"}).json()["id"]
    base = f"/v1/bots/{owl}"
    assert http.get(base + "/plugins").json()["hub_accounts"][0]["accounts"][0]["label"] == "Sales"
    assert "gmail_event_activations" in http.get("/v1/status").json()["capabilities"]
    rule = http.post(
        base + "/triggers",
        json={
            "kind": "gmail_received",
            "sender": "client@example.org",
            "name": "Client mail",
            "instructions": "Review the message.",
        },
    )
    assert rule.status_code == 201, rule.text
    assert (
        rule.json()["state"] == "draft" and rule.json()["app_event"]["connection_id"] == "ca_work"
    )
    path = base + "/triggers/" + rule.json()["id"]
    assert (
        http.patch(
            path, json={"state": "active", "expected_revision": 1, "connection_id": "ca_other"}
        ).status_code
        == 409
    )
    assert (
        http.patch(
            path, json={"state": "active", "expected_revision": 1, "connection_id": "ca_work"}
        ).status_code
        == 200
    )
    now = time.time() + 910
    message = {
        "messageId": "111",
        "messageTimestamp": str(int((now - 60) * 1000)),
        "subject": "Proposal",
    }
    pages.append({"messages": [message], "nextPageToken": "page-two"})
    assert poll_one(app.state.runtime.triggers, now)
    assert not poll_one(app.state.runtime.triggers, now)
    resumed = service()
    pages.append({"messages": [message, dict(message, messageId="222")], "nextPageToken": ""})
    assert poll_one(resumed.state.runtime.triggers, now + 2)
    assert calls[0]["query"] == calls[1]["query"] and "from:client@example.org" in calls[0]["query"]
    assert calls[1]["page_token"] == "page-two"
    assert resumed.state.runtime.triggers.dispatch(max_calls=20, now=now + 10) == 1
    deliveries = http.get(base + "/triggers").json()[0]["recent_deliveries"]
    assert {d["event"]["message_id"] for d in deliveries} == {"111", "222"}
    assert len(deliveries) == 2 and len({d["task_id"] for d in deliveries}) == 1
    runtime = resumed.state.runtime
    claimed = runtime.store.claim(settings.owner)
    assert claimed["id"] == deliveries[0]["task_id"]
    runtime.run(claimed, runtime.task_token(claimed["id"]))
    assert "read_event_email" in offered(model.requests[0])
    assert "list_connected_apps" not in offered(model.requests[0])
    assert "Delivery arrives Friday." in tool_results(model.requests[1])[-1]
    # A different selected mailbox never receives the original read, even on the next service.
    account["id"] = "ca_other"
    assert poll_one(resumed.state.runtime.triggers, now + 1000)
    paused = http.get(base + "/triggers").json()[0]
    assert (
        paused["state"] == "paused"
        and paused["app_event"]["notice"] == "reconnect"
        and len(calls) == 2
    )
    assert (
        http.patch(
            path,
            json={
                "state": "active",
                "expected_revision": paused["revision"],
                "connection_id": "ca_work",
            },
        ).status_code
        == 409
    )
    response = http.patch(
        path,
        json={
            "state": "active",
            "expected_revision": paused["revision"],
            "connection_id": "ca_other",
        },
    )
    assert (
        response.status_code == 200 and response.json()["app_event"]["connection_id"] == "ca_other"
    )


@pytest.mark.parametrize("confirm_replacement", [False, True])
def test_pending_cloud_app_request_resumes_once_for_the_confirmed_account(
    tmp_path, confirm_replacement
):
    class Accounts:
        selected = "ca_work"
        status = "needs_sign_in"

        def settings(self, bot_id):
            return [
                {
                    "plugin_id": "hub.gmail",
                    "name": "Gmail",
                    "grant": {"connection_id": self.selected},
                    "accounts": [{"id": self.selected, "status": self.status}],
                }
            ]

    store = Store(tmp_path / "needs.db")
    account = Accounts()
    apps = OwlApps(store, "owner", DesktopConnectors(), account)
    owl = store.save_bot("owner", BotInput(name="Sales", purpose="Handle replies"))
    chat = store.create_chat("owner", owl["id"], "Sales mail")
    task = store.enqueue(
        "owner", chat["id"], TaskInput(message="Read the proposal", request_id="incoming")
    )
    assert (
        apps.need(task, {"app": "hub.gmail", "reason": "To read the proposal"})["status"]
        == "card_shown"
    )
    card = store.conversation("owner", chat["id"])["tasks"][0]["app_needs"][0]
    assert (
        apps.decide(owl["id"], card["id"], AppNeedDecision(action="connect", expected_revision=1))[
            "status"
        ]
        == "connecting"
    )
    store.claim("owner")
    store.finish("owner", task["id"], "completed", content="Connect Gmail to continue.")
    account.selected = "ca_other"
    account.status = "active"
    apps.resume_hub_connections()
    assert store.claim("owner") is None

    if confirm_replacement:
        with pytest.raises(Conflict):
            apps.decide(
                owl["id"],
                card["id"],
                AppNeedDecision(
                    action="use_account", connection_id="ca_wrong", expected_revision=2
                ),
            )
        apps.decide(
            owl["id"],
            card["id"],
            AppNeedDecision(action="use_account", connection_id="ca_other", expected_revision=2),
        )
    else:
        account.selected = "ca_work"
    apps.resume_hub_connections()
    apps.resume_hub_connections()
    resumed = store.claim("owner")
    assert resumed["request_id"] == "app_ready_" + card["id"]
    assert resumed["config"]["app_ready"]["connection_id"] == account.selected
    assert store.claim("owner") is None


def test_primary_calendar_updates_and_cancellations_are_scoped_to_the_approved_account(tmp_path):
    from datetime import UTC, datetime

    settings = Settings(
        tmp_path / "calendar.db",
        "t" * 40,
        "example",
        "https://model.invalid",
        "fixture",
        service_mode="cloud",
    )

    class Calendar:
        tested = False
        pages = []
        requests = []

        def connections(self, manage=False):
            return {
                "connections": [
                    {
                        "id": "ca_calendar",
                        "app": "googlecalendar",
                        "status": "active",
                        "selected": True,
                        "label": "Team calendar",
                        "grant_revision": 2,
                    }
                ]
            }

        def tools(self, app, *, connection_id, timeout_s):
            assert (app, connection_id) == ("googlecalendar", "ca_calendar")
            return {
                "grant_revision": 2,
                "tested": self.tested,
                "tools": [{"name": "GOOGLECALENDAR_EVENTS_LIST", "hints": {"readOnly": True}}],
            }

        def call(self, app, tool, args, *, connection_id, grant_revision, timeout_s):
            assert (app, tool, connection_id, grant_revision) == (
                "googlecalendar",
                "GOOGLECALENDAR_EVENTS_LIST",
                "ca_calendar",
                2,
            )
            self.requests.append(args)
            return HubCallResult(is_error=False, sent=True, text=json.dumps(self.pages.pop(0)))

    calendar = Calendar()
    app = create_app(settings, more_apps=OwlHubApps(client=lambda **_: calendar))
    http = TestClient(app, headers={"Authorization": "Bearer " + settings.token})
    owl = http.post("/v1/bots", json={"name": "Calendar", "purpose": "Summarize changes"}).json()[
        "id"
    ]
    route = f"/v1/bots/{owl}/triggers"
    data = {
        "kind": "calendar_changed",
        "name": "Meeting changes",
        "instructions": "Summarize changes to meeting times.",
    }
    assert http.post(route, json=data).status_code == 409
    calendar.tested = True
    rule = http.post(route, json=data).json()
    assert rule["app_event"]["catalog_id"] == "googlecalendar"
    assert (
        http.patch(
            route + "/" + rule["id"],
            json={"state": "active", "expected_revision": 1, "connection_id": "ca_calendar"},
        ).status_code
        == 200
    )
    now = time.time() + 910
    updated = datetime.fromtimestamp(now - 50, UTC).isoformat()
    event = {
        "id": "weekly_31",
        "etag": "v2",
        "updated": updated,
        "summary": "Delivery meeting",
        "status": "confirmed",
        "start": {"dateTime": "2026-12-01T18:00:00Z"},
        "end": {"dateTime": "2026-12-01T19:00:00Z"},
        "location": "Athens",
    }
    calendar.pages.extend(
        [
            {"items": [event], "nextPageToken": "two"},
            {"items": [event, {"id": "cancelled_3", "status": "cancelled"}]},
            {
                "items": [
                    {
                        **event,
                        "updated": datetime.fromtimestamp(now + 300, UTC).isoformat(),
                        "etag": "v3",
                        "location": "Corfu",
                    }
                ]
            },
        ]
    )
    assert poll_one(app.state.runtime.triggers, now)
    assert poll_one(app.state.runtime.triggers, now + 2)
    assert poll_one(app.state.runtime.triggers, now + 910)
    deliveries = http.get(route).json()[0]["recent_deliveries"]
    assert len(deliveries) == 3
    assert {d["event"].get("location") for d in deliveries} == {"Athens", "Corfu", None}
    assert any(d["event"]["status"] == "cancelled" for d in deliveries)
    assert all(
        args["calendarId"] == "primary" and args["showDeleted"] for args in calendar.requests
    )
    assert calendar.requests[0]["updatedMin"] == calendar.requests[1]["updatedMin"]
    assert calendar.requests[1]["pageToken"] == "two" and "pageToken" not in calendar.requests[2]
