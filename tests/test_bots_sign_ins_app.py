"""The sign-ins routes: listed when the owl's computer keeps saved browser storage, signing
out one site or every site, and nothing changed while the owl's computer is open."""

from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from test_bots_cloud_desktop import Provider
from test_bots_sign_ins import GREEK, JAPANESE, profile

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Store


def service(tmp_path, *, key=b"k" * 32):
    store = Store(tmp_path / "bots.db", objects=LocalObjectStorage(tmp_path / "objects"))
    factory = CloudDesktopFactory(
        "owner",
        Provider(),
        ComputerSpec("fixture-image"),
        tmp_path / "leases",
        store=store,
        browser_enabled=True,
        persist_workspace=True,
        browser_state_key=key,
    )
    settings = Settings(
        store.path, "t" * 40, "fixture", "https://example.invalid", "fixture", owner="owner"
    )
    return store, factory, create_app(settings, task_store=store, cloud_computers=factory)


def saved_profile(store, factory):
    bot = store.save_bot("owner", BotInput(name="Νυχτερίδα"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message="Work", request_id=uuid4().hex))
    task = store.claim("owner")
    factory.browser_states.stage("owner", bot["id"], task["id"], profile())
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (task["id"],))
    return bot


def test_sign_ins_are_listed_and_signed_out_through_the_service(tmp_path):
    store, factory, app = service(tmp_path)
    bot = saved_profile(store, factory)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + "t" * 40
        assert "owl_sign_ins" in client.get("/v1/status").json()["capabilities"]
        listed = client.get(f"/v1/bots/{bot['id']}/sign-ins").json()
        assert listed["browser_open"] is False
        assert [site["site"] for site in listed["sites"]] == [
            "example.co.uk",
            GREEK,
            JAPANESE,
            "notes.example",
        ]
        assert listed["sites"][1]["name"] == "παράδειγμα.δοκιμή"
        out = client.post(f"/v1/bots/{bot['id']}/sign-ins/sign-out", json={"site": GREEK})
        assert out.status_code == 200 and out.json()["signed_out"] is True
        assert GREEK not in [site["site"] for site in out.json()["sites"]]
        everywhere = client.post(
            f"/v1/bots/{bot['id']}/sign-ins/sign-out", json={"everywhere": True}
        )
        assert everywhere.json() == {"signed_out": True, "sites": [], "browser_open": False}
        for body in ({}, {"site": GREEK, "everywhere": True}, {"site": ""}, {"other": 1}):
            assert (
                client.post(f"/v1/bots/{bot['id']}/sign-ins/sign-out", json=body).status_code == 422
            )


def test_an_open_computer_keeps_its_sign_ins_until_it_closes(tmp_path):
    store, factory, app = service(tmp_path)
    bot = saved_profile(store, factory)
    # A computer whose image declares no live sign-out (an older owl computer image).
    older = SimpleNamespace(_closed=False, _browser_started=True, browser_features=frozenset())
    factory._instances[bot["id"]] = (older, {"browser"})
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer " + "t" * 40
            out = client.post(
                f"/v1/bots/{bot['id']}/sign-ins/sign-out", json={"site": "example.co.uk"}
            ).json()
            assert out["signed_out"] is False and out["reason"] == "computer_in_use"
            assert "example.co.uk" in [site["site"] for site in out["sites"]]
    finally:
        factory._instances.pop(bot["id"])


def test_a_service_without_saved_browser_storage_offers_no_sign_ins(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Owl"))
    settings = Settings(
        store.path, "t" * 40, "fixture", "https://example.invalid", "fixture", owner="owner"
    )
    with TestClient(create_app(settings, task_store=store)) as client:
        client.headers["Authorization"] = "Bearer " + "t" * 40
        assert "owl_sign_ins" not in client.get("/v1/status").json()["capabilities"]
        assert client.get(f"/v1/bots/{bot['id']}/sign-ins").status_code == 404
