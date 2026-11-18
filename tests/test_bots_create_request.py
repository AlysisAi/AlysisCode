"""Creating an owl never makes two: a create sent again with its request id returns the same owl.

The desktop app gives each owl it is making one request id and sends it with every try, so an
answer lost on the way back (a dropped connection, a closed laptop) can be retried safely.
"""

from __future__ import annotations

import pytest

from alysis_code.bots.models import BotCreateInput, BotInput, TaskInput
from alysis_code.bots.store import Conflict, Store

SETUP = {
    "name": "Άτλας 東京",
    "purpose": "Βοηθά με τις εργασίες μου",
    "instructions": "Γράφε απλά.",
}


def test_a_lost_answer_then_a_retry_makes_one_owl(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    first = store.save_bot("alice", BotCreateInput(**SETUP, request_id="owl-create-1"))
    # The first answer never reached the app; it sends the same request again.
    again = store.save_bot("alice", BotCreateInput(**SETUP, request_id="owl-create-1"))
    assert again == first
    assert [bot["id"] for bot in store.list_bots("alice")] == [first["id"]]


def test_the_same_request_id_with_other_details_is_refused(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    made = store.save_bot("alice", BotCreateInput(**SETUP, request_id="owl-create-1"))
    for changed in (
        {**SETUP, "name": "Άλλο όνομα"},
        {**SETUP, "instructions": "日本語で答えて。"},
        {**SETUP, "files_enabled": False},
    ):
        with pytest.raises(Conflict):
            store.save_bot("alice", BotCreateInput(**changed, request_id="owl-create-1"))
    assert [bot["id"] for bot in store.list_bots("alice")] == [made["id"]]


def test_request_ids_belong_to_one_person_and_are_optional(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    alice = store.save_bot("alice", BotCreateInput(**SETUP, request_id="owl-create-1"))
    bob = store.save_bot("bob", BotCreateInput(**SETUP, request_id="owl-create-1"))
    assert alice["id"] != bob["id"]
    # Older apps send no id: each create makes an owl, as before.
    store.save_bot("carol", BotInput(**SETUP))
    store.save_bot("carol", BotCreateInput(**SETUP))
    assert len(store.list_bots("carol")) == 2


def test_a_replay_still_answers_at_the_owl_limit(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    made = store.save_bot("alice", BotCreateInput(**SETUP, request_id="owl-create-1"))
    for index in range(29):
        store.save_bot("alice", BotInput(name=f"Owl {index}"))
    assert store.save_bot("alice", BotCreateInput(**SETUP, request_id="owl-create-1")) == made


def test_deleting_the_owl_erases_its_request(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    made = store.save_bot("alice", BotCreateInput(**SETUP, request_id="owl-create-1"))
    chat = store.create_chat("alice", made["id"], "Συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message="Γεια", request_id="message-1"))
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='failed',failure='{\"kind\":\"model\"}'")
    store.delete_bot("alice", made["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM bot_create_requests").fetchone()[0] == 0
        assert db.execute("SELECT failure FROM tasks").fetchone()[0] is None
    assert store.list_bots("alice") == []


def test_request_ids_are_bounded():
    with pytest.raises(ValueError):
        BotCreateInput(**SETUP, request_id="short")
    with pytest.raises(ValueError):
        BotCreateInput(**SETUP, request_id="has spaces in it")


def test_the_service_returns_the_same_owl_for_a_retried_create(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture-key", "https://example.invalid/v1", "fixture"
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "bot_create_request_id" in client.get("/v1/status").json()["capabilities"]
        assert "task_failure_kinds" in client.get("/v1/status").json()["capabilities"]
        body = {**SETUP, "request_id": "owl-create-1"}
        first = client.post("/v1/bots", json=body)
        again = client.post("/v1/bots", json=body)
        assert first.status_code == again.status_code == 201
        assert again.json()["id"] == first.json()["id"]
        assert client.post("/v1/bots", json={**body, "name": "Άλλο"}).status_code == 409
        assert len(client.get("/v1/bots").json()) == 1
        # Without an id, as older apps send it, a create still makes an owl.
        assert client.post("/v1/bots", json=SETUP).status_code == 201
        assert len(client.get("/v1/bots").json()) == 2
        # Editing an owl takes no request id.
        path = f"/v1/bots/{first.json()['id']}"
        assert client.put(path, json=body).status_code == 422
