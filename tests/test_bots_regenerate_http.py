"""Another answer over HTTP: the capability, the route, its request id and its refusals."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
from alysis_code.llm.types import LLMResponse, LLMUsage


class AnswerModel:
    __test__ = False
    model = "provider-fixture"
    supports_tool_calling = True

    def chat(self, **kwargs):
        # The service checks each reply before it is published, as it does for every owl.
        if kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS:
            return LLMResponse(
                '{"decision":"ready","reason":"Scripted fixture reply."}',
                [],
                {},
                usage=LLMUsage(3, 2, 5),
            )
        return LLMResponse("Νέα απάντηση 新しい答え", [], {}, usage=LLMUsage(10, 5, 15))


def prepared(tmp_path):
    settings = Settings(
        tmp_path / "regenerate.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: AnswerModel())
    store = app.state.store
    bot = store.save_bot(settings.owner, BotInput(name="Έλενα"))
    chat = store.create_chat(settings.owner, bot["id"], "Συζήτηση")
    tasks = []
    for number in range(2):
        task = store.enqueue(
            settings.owner,
            chat["id"],
            TaskInput(message=f"Ερώτηση {number} 東京", request_id="req_" + uuid4().hex),
        )
        store.claim(settings.owner)
        store.finish(settings.owner, task["id"], "completed", content=f"Απάντηση {number}")
        tasks.append(task)
    return settings, app, chat, tasks


def test_status_names_regenerate_reply_and_conversations_carry_read_at(tmp_path):
    settings, app, chat, _ = prepared(tmp_path)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert "regenerate_reply" in client.get("/v1/status").json()["capabilities"]
        assert client.get(f"/v1/chats/{chat['id']}").json()["read_at"] is None
        page = client.post(f"/v1/chats/{chat['id']}/page", json={"limit": 2}).json()
        assert "read_at" in page


def test_regenerate_over_http(tmp_path):
    settings, app, chat, (older, latest) = prepared(tmp_path)
    path = f"/v1/tasks/{latest['id']}/regenerate"
    with TestClient(app) as client:
        assert client.post(path, json={"request_id": "again_" + "a" * 8}).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        for bad in ({}, {"request_id": "short"}, {"request_id": "again_ok_1", "extra": 1}):
            assert client.post(path, json=bad).status_code == 422
        refused = client.post(
            f"/v1/tasks/{older['id']}/regenerate", json={"request_id": "again_older_1"}
        )
        assert refused.status_code == 409
        assert (
            client.post(
                f"/v1/tasks/{'f' * 32}/regenerate", json={"request_id": "again_none_1"}
            ).status_code
            == 404
        )
        accepted = client.post(path, json={"request_id": "again_latest_1"})
        assert accepted.status_code == 202
        task = accepted.json()
        assert task["config"]["regenerate"]["replaces"] == latest["id"]
        assert client.post(path, json={"request_id": "again_latest_1"}).json()["id"] == task["id"]
        until = time.monotonic() + 3
        while time.monotonic() < until:
            result = client.get(f"/v1/tasks/{task['id']}").json()
            if result["status"] == "completed":
                break
            time.sleep(0.02)
        assert result["status"] == "completed"
        messages = client.get(f"/v1/chats/{chat['id']}").json()["messages"]
        assert [m["content"] for m in messages[-2:]] == ["Απάντηση 1", "Νέα απάντηση 新しい答え"]
