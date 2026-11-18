"""The app finds things over HTTP: conversation pages, what needs the person, and search."""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, TaskInput


class IdleModel:
    __test__ = False
    model = "provider-fixture"
    supports_tool_calling = True

    def chat(self, **kwargs):
        raise AssertionError("These tests make no model calls.")


def prepared(tmp_path, turns=5, *, failed_last=False):
    settings = Settings(
        tmp_path / "find.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: IdleModel())
    store = app.state.store
    bot = store.save_bot(settings.owner, BotInput(name="Έλενα"))
    chat = store.create_chat(settings.owner, bot["id"], "Συζήτηση")
    for number in range(turns):
        task = store.enqueue(
            settings.owner,
            chat["id"],
            TaskInput(message=f"Καλημέρα {number} 東京", request_id="req_" + uuid4().hex),
        )
        store.claim(settings.owner)
        last = failed_last and number == turns - 1
        store.finish(
            settings.owner,
            task["id"],
            "failed" if last else "completed",
            content="" if last else f"Απάντηση {number}",
            error="Example failure" if last else None,
        )
    return settings, app, bot, chat


def test_status_names_the_new_capabilities(tmp_path):
    settings, app, _, _ = prepared(tmp_path, turns=0)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        capabilities = client.get("/v1/status").json()["capabilities"]
    assert {"conversation_pages", "needs_you", "history_retrieval"} <= set(capabilities)


def test_conversation_pages_and_polls_over_http(tmp_path):
    settings, app, _, chat = prepared(tmp_path)
    path = f"/v1/chats/{chat['id']}/page"
    with TestClient(app) as client:
        assert client.post(path, json={}).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        whole = client.get(f"/v1/chats/{chat['id']}").json()
        latest = client.post(path, json={"limit": 3}).json()
        assert latest["messages"] == whole["messages"][-3:]
        assert latest["earlier"] == latest["messages"][0]["id"]
        earlier = client.post(path, json={"limit": 3, "before": latest["earlier"]}).json()
        assert earlier["messages"] == whole["messages"][-6:-3]
        quiet = client.post(
            path,
            json={"limit": 3, "after": latest["messages"][-1]["id"], "version": latest["version"]},
        ).json()
        assert quiet["unchanged"] is True and quiet["messages"] == []
        for bad in (
            {"limit": 0},
            {"after": latest["messages"][-1]["id"]},
            {"version": latest["version"], "before": latest["earlier"]},
            {"unknown": True},
        ):
            assert client.post(path, json=bad).status_code == 422
        assert client.post(path, json={"before": "m" * 32}).status_code == 404
        assert client.post(f"/v1/chats/{'c' * 32}/page", json={}).status_code == 404


def test_needs_you_over_http(tmp_path):
    settings, app, bot, _ = prepared(tmp_path, turns=2, failed_last=True)
    with TestClient(app) as client:
        assert client.get("/v1/needs-you").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        result = client.get("/v1/needs-you").json()
    assert result["count"] == 1
    [item] = result["items"]
    assert (item["kind"], item["bot_id"], item["bot_name"]) == ("failed", bot["id"], "Έλενα")
    assert item["text"] == "Καλημέρα 1 東京"


def test_history_search_over_http_shows_excerpts_for_any_language(tmp_path):
    settings, app, bot, _ = prepared(tmp_path, turns=3)
    path = f"/v1/bots/{bot['id']}/history/search"
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        exact = client.post(path, json={"query": "ΚΑΛΗΜΈΡΑ 2"}).json()
        loose = client.post(path, json={"query": "καλημερα 1"}).json()
        japanese = client.post(path, json={"query": "東京", "limit": 2}).json()
    assert exact["match"] == "exact"
    assert exact["messages"][0]["excerpt"] == {
        "before": "",
        "match": "Καλημέρα 2",
        "after": " 東京",
    }
    assert loose["match"] == "loose"
    assert loose["messages"][0]["excerpt"]["match"] == "Καλημέρα 1"
    assert [m["excerpt"]["match"] for m in japanese["messages"]] == ["東京", "東京"]
    assert japanese["next_before"] == japanese["messages"][-1]["position"]
