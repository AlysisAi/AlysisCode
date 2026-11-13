from __future__ import annotations

import time
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.llm.types import LLMResponse, LLMUsage


def test_browser_configuration_is_explicit_linux_only_and_lazy(tmp_path, monkeypatch):
    settings = Settings(
        tmp_path / "browser.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    assert create_app(settings).state.runtime.browser_computers is None
    with pytest.raises(ValueError, match="boolean"):
        replace(settings, browser_enabled="false")
    monkeypatch.setattr("alysis_code.bots.app.platform.system", lambda: "Darwin")
    with pytest.raises(ValueError, match="Linux"):
        create_app(replace(settings, browser_enabled=True))
    monkeypatch.setattr("alysis_code.bots.app.platform.system", lambda: "Linux")
    computers = create_app(replace(settings, browser_enabled=True)).state.runtime.browser_computers
    assert computers.views.frame(settings.owner, "bot") == {"state": "not_running"}
    assert {s["function"]["name"] for s in computers.schemas({"bot_id": "bot"}, True)} == {
        "browser_navigate",
        "browser_pages",
        "browser_close_page",
        "browser_switch_page",
        "browser_observe",
        "browser_source",
    }
    computers.close()


class TestModel:
    __test__ = False
    model = "provider-fixture"
    supports_tool_calling = True

    def chat(self, **kwargs):
        from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS

        if kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS:
            return LLMResponse(
                '{"decision":"ready","reason":"Scripted API fixture reply."}',
                [],
                {},
                usage=LLMUsage(3, 2, 5),
            )
        return LLMResponse("Fixture response", [], {}, usage=LLMUsage(3, 2, 5))


def test_approval_rules_api_persistence_validation_and_scope(tmp_path):
    from alysis_code.bots.models import BotInput

    settings = Settings(
        tmp_path / "rules.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: TestModel())
    bot = app.state.store.save_bot(
        settings.owner, BotInput(name="Review", purpose="Synthetic test")
    )
    other = app.state.store.save_bot(settings.owner, BotInput(name="Other", purpose="Separate bot"))
    base = f"/v1/bots/{bot['id']}/action-review-rules"
    with TestClient(app) as client:
        assert client.get(base).status_code == 401
        assert client.post(base, json={"effect": "allow", "description": "Test"}).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.get(base).json() == []
        for bad in [
            {"effect": "deny", "description": "Test"},
            {"effect": "allow", "description": " "},
        ]:
            assert client.post(base, json=bad).status_code == 422
        rule = client.post(
            base, json={"effect": "ask", "description": "Before deleting files"}
        ).json()
        path = base + "/" + rule["id"]
        assert client.get(f"/v1/bots/{other['id']}/action-review-rules").json() == []
        assert (
            client.patch(
                f"/v1/bots/{other['id']}/action-review-rules/{rule['id']}",
                json={"effect": "allow", "description": "Test", "expected_revision": 1},
            ).status_code
            == 404
        )
        assert (
            client.patch(
                path,
                json={
                    "effect": "allow",
                    "description": "Write local reports",
                    "expected_revision": 1,
                },
            ).status_code
            == 200
        )
        assert client.request("DELETE", path, json={"expected_revision": 1}).status_code == 409
        assert client.request("DELETE", path, json={"expected_revision": True}).status_code == 422
    with TestClient(create_app(settings, client_factory=lambda _: TestModel())) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        (saved,) = client.get(base).json()
        assert saved["revision"] == 2 and saved["description"] == "Write local reports"
        assert client.request("DELETE", path, json={"expected_revision": 2}).status_code == 200
        assert client.get(base).json() == []


def test_action_review_api_requires_auth_and_preserves_pending_without_worker(tmp_path):
    from alysis_code.bots.action_review import ApprovalRequired
    from alysis_code.bots.models import BotInput, TaskInput

    settings = Settings(
        tmp_path / "reviews.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: TestModel())
    runtime, store = app.state.runtime, app.state.store
    assert runtime.action_review_enabled
    bot = store.save_bot(settings.owner, BotInput(name="Review", purpose="Synthetic test"))
    chat = store.create_chat(settings.owner, bot["id"], "Test")
    store.enqueue(
        settings.owner, chat["id"], TaskInput(message="Prepare work", request_id="review-api")
    )
    task = store.claim(settings.owner)
    with pytest.raises(ApprovalRequired):
        runtime.action_reviews.request(
            task, {"tool": "computer_run", "arguments": {"command": "true"}}, "Confirm"
        )
    runtime.action_reviews.pause(task, [])
    # No lifespan: deliberately exercise a stopped worker without model calls.
    client = TestClient(app)
    base = f"/v1/bots/{bot['id']}"
    assert client.get(base + "/action-reviews").status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.token
    assert client.get(base + "/action-review-settings").json() == {"mode": "auto", "rules": []}
    assert (
        client.patch(base + "/action-review-settings", json={"mode": "review"}).status_code == 200
    )
    assert (
        client.patch(base + "/action-review-settings", json={"mode": "bypass"}).status_code == 422
    )
    (review,) = client.get(base + "/action-reviews").json()
    path = base + "/action-reviews/" + review["id"]
    assert client.patch(path, json={"action": "allow", "expected_revision": 1}).status_code == 503
    assert (
        client.patch(path, json={"action": "always_allow", "expected_revision": 1}).status_code
        == 503
    )
    assert client.get(base + "/action-reviews").json()[0]["status"] == "pending"
    assert client.patch(path, json={"action": "deny", "expected_revision": 1}).status_code == 200
    assert store.task(settings.owner, task["id"])["status"] == "cancelled"
    assert client.get(base + "/action-review-usage").json()["actions"]["calls"] == 0
    client.close()


@pytest.mark.parametrize("database_error", [True, False])
def test_database_error_is_private_and_does_not_mask_programming_errors(
    tmp_path, monkeypatch, database_error
):
    from alysis_code.bots.postgres import SharedDatabaseError

    settings = Settings(
        tmp_path / "errors.db", "t" * 40, "fixture-key", "https://example.invalid", "fixture"
    )
    app = create_app(settings)
    called = []

    def fail(_owner):
        called.append(True)
        error = SharedDatabaseError if database_error else RuntimeError
        raise error("private configuration must not leave the service")

    monkeypatch.setattr(app.state.store, "list_bots", fail)
    # Explicit fault injection at the store boundary; no model is called.
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/v1/bots").status_code == 401
        assert not called
        response = client.get("/v1/bots", headers={"Authorization": "Bearer " + settings.token})
    assert called == [True]  # The request was never automatically retried.
    assert "private configuration" not in response.text
    if database_error:
        assert response.status_code == 503
        assert "did not confirm success" in response.json()["detail"]
        assert response.headers["cache-control"] == "no-store"
    else:
        assert response.status_code == 500


def test_authenticated_api_roundtrip_and_restart(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture-key", "https://example.invalid/v1", "fixture"
    )
    with TestClient(create_app(settings, client_factory=lambda _: TestModel())) as client:
        assert client.get("/v1/bots").status_code == 401
        assert client.post("/v1/bots", json={"name": "Test", "purpose": "Test"}).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.get("/v1/status").json()["usage"]["calls"] == 0
        assert "fixture-key" not in repr(settings)
        assert client.post("/v1/bots", json={"name": " ", "purpose": "Test"}).status_code == 422
        bot = client.post("/v1/bots", json={"name": "Έλενα", "purpose": "Organize notes"}).json()
        chat = client.post(f"/v1/bots/{bot['id']}/chats", json={}).json()
        path = f"/v1/chats/{chat['id']}/tasks"
        data = {"message": "A small test", "request_id": "same_request"}
        task = client.post(path, json=data).json()
        assert client.post(path, json=data).json()["id"] == task["id"]
        until = time.monotonic() + 3
        while time.monotonic() < until:
            result = client.get(f"/v1/tasks/{task['id']}").json()
            if result["status"] == "completed":
                break
            time.sleep(0.02)
        assert result["status"] == "completed"
        assert result["calls"] == 2
        conversation = client.get(f"/v1/chats/{chat['id']}").json()
        assert conversation["unread"]
        assert (
            client.post(
                f"/v1/chats/{chat['id']}/read", json={"activity_seq": conversation["activity_seq"]}
            ).status_code
            == 200
        )
        assert not client.get(f"/v1/chats/{chat['id']}").json()["unread"]
        assert (
            client.post(
                f"/v1/chats/{chat['id']}/read",
                json={"activity_seq": conversation["activity_seq"] + 100},
            ).status_code
            == 404
        )
        assert (
            client.post(f"/v1/chats/{chat['id']}/read", json={"activity_seq": 0}).status_code == 422
        )
        assert client.get("/v1/status").headers["cache-control"] == "no-store"
        file = client.post(
            f"/v1/bots/{bot['id']}/files", json={"name": "notes.txt", "content": "Text"}
        ).json()
        assert client.get(f"/v1/bots/{bot['id']}/files/{file['id']}").json()["content"] == "Text"
        assert client.post("/v1/bots", content="x" * 512001).status_code == 413
    with TestClient(
        create_app(settings, client_factory=lambda _: TestModel()),
        headers={"Authorization": "Bearer " + settings.token},
    ) as client:
        assert client.get("/v1/bots").json()[0]["name"] == "Έλενα"
        assert (
            client.get(f"/v1/chats/{chat['id']}").json()["messages"][-1]["content"]
            == "Fixture response"
        )
        assert client.get("/v1/status").json()["usage"]["calls"] == 2


def test_note_api_version_checks_and_history(tmp_path):
    settings = Settings(
        tmp_path / "notes.db", "t" * 40, "fixture-key", "https://example.invalid/v1", "fixture"
    )
    with TestClient(create_app(settings, client_factory=lambda _: TestModel())) as client:
        assert client.get("/v1/bots/unknown/notes").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post(
            "/v1/bots", json={"name": "Memory", "purpose": "Remember projects"}
        ).json()
        base = f"/v1/bots/{bot['id']}"
        note = client.post(base + "/notes", json={"title": "Pilot", "content": "Initial"}).json()
        path = base + "/notes/" + note["id"]
        data = {"title": "Pilot", "content": "Corrected", "expected_revision": 1}
        assert client.put(path, json=data).json()["revision"] == 2
        assert client.put(path, json=data).status_code == 409
        assert client.get(path).json()["revisions"][1]["content"] == "Initial"
        assert (
            client.post(path + "/history", json={"before_revision": 2}).json()["revisions"][0][
                "revision"
            ]
            == 1
        )
        assert (
            client.post(base + "/history/search", json={"query": "anything"}).json()["messages"]
            == []
        )
        assert client.post(base + "/history/search", json={"limit": 1000}).status_code == 422
        assert client.get(base + "/notes").json()[0]["content"] == "Corrected"


def test_schedule_api_review_conflicts_validation_and_real_completion(tmp_path):
    from datetime import UTC, datetime

    settings = Settings(
        tmp_path / "schedules.db", "t" * 40, "fixture-key", "https://example.invalid/v1", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: TestModel())
    with TestClient(app) as client:
        assert client.get("/v1/bots/unknown/schedules").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Scheduled", "purpose": "Review notes"}).json()
        base = f"/v1/bots/{bot['id']}/schedules"
        payload = {
            "name": "Review",
            "prompt": "Review the current notes",
            "enabled": False,
            "timing": {"kind": "interval", "every_minutes": 5},
        }
        created = client.post(base, json=payload)
        assert created.status_code == 201
        schedule = created.json()
        assert schedule["state"] == "paused" and schedule["next_run_at"] is None
        path = base + "/" + schedule["id"]
        assert (
            client.patch(path, json={"state": "active", "expected_revision": 1}).json()["revision"]
            == 2
        )
        assert (
            client.patch(path, json={"state": "paused", "expected_revision": 1}).status_code == 409
        )
        assert client.put(path, json={**payload, "expected_revision": 1}).status_code == 409
        assert (
            client.put(
                path,
                json={
                    **payload,
                    "timing": {"kind": "daily", "time": "09:00", "timezone": "invalid"},
                    "expected_revision": 2,
                },
            ).status_code
            == 422
        )
        due = datetime.fromtimestamp(time.time() + 0.2, UTC).isoformat()
        assert (
            client.put(
                path,
                json={
                    **payload,
                    "enabled": True,
                    "timing": {"kind": "once", "at": due},
                    "expected_revision": 2,
                },
            ).status_code
            == 200
        )
        until = time.monotonic() + 5
        while time.monotonic() < until:
            runs = client.post(path + "/runs", json={}).json()["runs"]
            if runs and runs[0]["current_status"] == "completed":
                break
            time.sleep(0.02)
        assert runs[0]["current_status"] == "completed"
        assert client.get(f"/v1/tasks/{runs[0]['task_id']}").json()["output"] == "Fixture response"
        assert client.get(base).json()[0]["last_run"]["status"] == "completed"
        assert client.post(path + "/runs", json={"before": runs[0]["id"]}).json()["runs"] == []
        assert client.post(path + "/runs", json={"before": 0}).status_code == 422
        assert client.get("/v1/status").json()["usage"]["calls"] == 1
        assert "schedules" in client.get("/v1/status").json()["capabilities"]


def test_zero_allowance_blocks_provider_before_queued_task_execution(tmp_path):
    from alysis_code.bots.models import BotInput, TaskInput
    from alysis_code.bots.store import Store

    settings = Settings(
        tmp_path / "zero.db",
        "t" * 40,
        "fixture-key",
        "https://example.invalid/v1",
        "fixture",
        max_calls=0,
    )
    store = Store(settings.database)
    bot = store.save_bot(settings.owner, BotInput(name="Zero", purpose="No spend"))
    chat = store.create_chat(settings.owner, bot["id"], "Queued before startup")
    task = store.enqueue(
        settings.owner,
        chat["id"],
        TaskInput(message="Do not call provider", request_id="zero_budget_task"),
    )
    called = []

    class CountingFixture(TestModel):
        def chat(self, **kwargs):
            called.append(True)
            return super().chat(**kwargs)

    with TestClient(
        create_app(settings, client_factory=lambda _: CountingFixture()),
        headers={"Authorization": "Bearer " + settings.token},
    ) as client:
        until = time.monotonic() + 3
        while time.monotonic() < until:
            result = client.get(f"/v1/tasks/{task['id']}").json()
            if result["status"] == "failed":
                break
            time.sleep(0.02)
        assert result["status"] == "failed"
        assert called == []
        assert client.get("/v1/status").json()["usage"]["calls"] == 0
