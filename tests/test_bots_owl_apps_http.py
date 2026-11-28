"""The per-owl apps routes, end to end through the service (needs FastAPI)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from alysis_code.bots.app import Settings, create_app  # noqa: E402

CATALOG = {
    "revision": 1,
    "plugins": [
        {
            "plugin_id": "custom.notes",
            "name": "Σημειώσεις",
            "tools": [{"name": "search", "description": "", "input_schema": {}, "effect": "read"}],
        }
    ],
    "apps": [
        {"plugin_id": "custom.notes", "name": "Σημειώσεις"},
        {"plugin_id": "alysis.linear", "name": "Linear"},
    ],
}


def service(tmp_path, mode="development"):
    settings = Settings(
        tmp_path / "apps.db",
        "t" * 40,
        "fixture-key",
        "https://example.invalid/v1",
        "fixture",
        service_mode=mode,
    )
    client = TestClient(create_app(settings))
    client.headers["Authorization"] = "Bearer " + settings.token
    return client


def test_local_hub_grant_settings_use_the_current_owl_and_preserve_disconnected_accounts(
    tmp_path, monkeypatch
):
    from alysis_code import hub_apps

    calls = []
    state = {"connection_id": None, "mode": "inherit", "revision": 0}

    class Broker:
        def __init__(self, owl_id):
            self.owl_id = owl_id

        def connections(self, *, manage):
            assert manage
            return {
                "hub_accounts": [
                    {
                        "plugin_id": "hub.notes",
                        "catalog_id": "notes",
                        "name": "Notes",
                        "accounts": [],
                        "grant": state,
                    }
                ]
            }

        def set_owl_grant(self, app, grant):
            calls.append((self.owl_id, app, grant))
            if state["revision"] != grant["expected_revision"]:
                raise hub_apps.HubAppsError("conflict")
            state.update(
                mode=grant["mode"],
                connection_id=grant["connection_id"],
                revision=state["revision"] + 1,
            )

    monkeypatch.setattr(
        hub_apps.HubAppsClient,
        "from_saved_account",
        classmethod(lambda cls, owl_id, **kw: Broker(owl_id)),
    )
    with service(tmp_path, "local") as client:
        owl = client.post("/v1/bots", json={"name": "Juno", "purpose": ""}).json()["id"]
        prefix = f"/v1/bots/{owl}"
        body = {"mode": "read", "connection_id": "ca_work", "expected_revision": 0}
        saved = client.patch(prefix + "/hub-grants/notes", json=body)
        assert saved.status_code == 200 and calls == [(owl, "notes", body)]
        assert saved.json()["hub_accounts"][0]["grant"]["revision"] == 1
        assert saved.json()["apps"][0]["connected"] is False
        assert client.patch(prefix + "/hub-grants/notes", json=body).status_code == 409
        assert client.patch("/v1/bots/missing/hub-grants/notes", json=body).status_code == 404
        assert len(calls) == 2


def test_status_lists_the_capability_and_each_owl_keeps_its_own_switches(tmp_path):
    with service(tmp_path) as client:
        assert "owl_apps" in client.get("/v1/status").json()["capabilities"]
        owl = client.post("/v1/bots", json={"name": "Ήρα", "purpose": ""}).json()
        other = client.post("/v1/bots", json={"name": "東京", "purpose": ""}).json()
        assert client.get(f"/v1/bots/{owl['id']}/plugins").json() == {
            "reachable": False,
            "apps": [],
        }
        assert client.put("/v1/desktop-plugins/catalog", json=CATALOG).status_code == 200
        listing = client.get(f"/v1/bots/{owl['id']}/plugins").json()
        assert listing["reachable"] is True
        assert [(a["name"], a["connected"], a["enabled"]) for a in listing["apps"]] == [
            ("Σημειώσεις", True, True),
            ("Linear", False, True),
        ]
        choice = {"plugin_id": "custom.notes", "enabled": False, "expected_revision": 0}
        changed = client.put(f"/v1/bots/{owl['id']}/plugins", json=choice).json()
        assert changed["apps"][0]["enabled"] is False
        assert client.put(f"/v1/bots/{owl['id']}/plugins", json=choice).status_code == 409
        assert client.get(f"/v1/bots/{other['id']}/plugins").json()["apps"][0]["enabled"]
        loose = {"plugin_id": "custom.notes", "enabled": "no", "expected_revision": 1}
        assert client.put(f"/v1/bots/{owl['id']}/plugins", json=loose).status_code == 422
        bad = {**choice, "plugin_id": "Notes app", "expected_revision": 1}
        assert client.put(f"/v1/bots/{owl['id']}/plugins", json=bad).status_code == 422


def test_an_older_desktop_catalog_is_still_accepted_and_unknown_cards_are_not_found(tmp_path):
    with service(tmp_path) as client:
        owl = client.post("/v1/bots", json={"name": "Ήρα", "purpose": ""}).json()
        older = {key: value for key, value in CATALOG.items() if key != "apps"}
        assert client.put("/v1/desktop-plugins/catalog", json=older).status_code == 200
        names = [a["name"] for a in client.get(f"/v1/bots/{owl['id']}/plugins").json()["apps"]]
        assert names == ["Σημειώσεις"]
        missing = client.patch(
            f"/v1/bots/{owl['id']}/app-needs/{'a' * 32}",
            json={"action": "turn_on", "expected_revision": 1},
        )
        assert missing.status_code == 404
        wrong = client.patch(
            f"/v1/bots/{owl['id']}/app-needs/{'a' * 32}",
            json={"action": "enable", "expected_revision": 1},
        )
        assert wrong.status_code == 422
