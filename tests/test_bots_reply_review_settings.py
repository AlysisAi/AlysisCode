"""Saved answer-review choice, using scripted models and disposable local stores."""

import json
import threading
from dataclasses import replace

from fastapi.testclient import TestClient
from test_bots_reply_review import Model, response, verdict

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, ReplyReviewSettings, TaskInput
from alysis_code.bots.reply_review import ReplyReview
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken

PATH = "/v1/settings/reply-review"


def test_authenticated_setting_persists_and_is_owner_scoped(tmp_path):
    settings = Settings(
        tmp_path / "bots.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    # No worker or provider is needed to read and save this setting.
    app = create_app(settings)
    with TestClient(app, headers={"Authorization": "Bearer wrong"}) as client:
        assert client.get(PATH).status_code == 401
        assert client.put(PATH, json={"enabled": False}).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.get(PATH).json() == {"enabled": True}
        for body in [
            {},
            {"enabled": "false"},
            {"enabled": 0},
            {"enabled": None},
            {"enabled": False, "owner": "other"},
        ]:
            assert client.put(PATH, json=body).status_code == 422
        assert client.get(PATH).json() == {"enabled": True}
        assert client.put(PATH, json={"enabled": False}).json() == {"enabled": False}
        assert app.state.runtime.action_review_enabled
        assert app.state.runtime.reply_review is not None
    for owner, expected in [("other", True), (settings.owner, False)]:
        with TestClient(
            create_app(replace(settings, owner=owner)),
            headers={"Authorization": "Bearer " + settings.token},
        ) as client:
            assert client.get(PATH).json() == {"enabled": expected}
            assert client.put(PATH, json={"enabled": True}).json() == {"enabled": True}


def test_default_off_and_reenabled_tasks_spend_only_expected_model_calls(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Help"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    for index, enabled in enumerate([True, False, True]):
        if index:
            store.reply_review_settings("owner", ReplyReviewSettings(enabled=enabled))
        store.enqueue(
            "owner", chat["id"], TaskInput(message="Say hello.", request_id=f"request-{index}")
        )
        task = store.claim("owner")
        assert task["config"]["reply_review_enabled"] is enabled
        model = Model([response("Hello!"), verdict("ready")] if enabled else [response("Hello!")])
        runtime = BotRuntime(store, "owner", lambda _, model=model: model)
        runtime.reply_review = ReplyReview(runtime)
        runtime.run(task, EventCancellationToken(threading.Event()))
        final = store.task("owner", task["id"])
        assert final["status"] == "completed", final["error"]
        assert final["output"] == "Hello!"
        assert final["calls"] == len(model.requests) == (2 if enabled else 1)
        assert sum(e["kind"] == "reply_review_finished" for e in final["events"]) == int(enabled)


def test_choice_is_captured_per_task_and_survives_reconnect_and_deduplication(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Help"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    data = TaskInput(message="Hello", request_id="first-task")
    first = store.enqueue("owner", chat["id"], data)
    store.reply_review_settings("owner", ReplyReviewSettings(enabled=False))
    second = store.enqueue(
        "owner", chat["id"], TaskInput(message="Hello", request_id="second-task")
    )
    store = Store(tmp_path / "bots.db")
    assert store.reply_review_settings("owner") == {"enabled": False}
    store.reply_review_settings("owner", ReplyReviewSettings(enabled=True))
    assert store.enqueue("owner", chat["id"], data)["id"] == first["id"]
    assert store.task("owner", first["id"])["config"]["reply_review_enabled"] is True
    assert store.task("owner", second["id"])["config"]["reply_review_enabled"] is False


def test_existing_database_and_legacy_task_keep_review_enabled(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Help"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    task = store.enqueue("owner", chat["id"], TaskInput(message="Hello", request_id="legacy-task"))
    config = {key: value for key, value in task["config"].items() if key != "reply_review_enabled"}
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET config=? WHERE id=?", (json.dumps(config), task["id"]))
        db.execute("DROP TABLE bot_reply_review_settings")
        db.execute("PRAGMA user_version=32")
    store = Store(tmp_path / "bots.db")
    assert store.reply_review_settings("owner") == {"enabled": True}
    store.reply_review_settings("owner", ReplyReviewSettings(enabled=False))
    model = Model([response("Hello!"), verdict("ready")])
    runtime = BotRuntime(store, "owner", lambda _: model)
    runtime.reply_review = ReplyReview(runtime)
    runtime.run(store.claim("owner"), EventCancellationToken(threading.Event()))
    final = store.task("owner", task["id"])
    assert final["status"] == "completed", final["error"]
    assert final["calls"] == 2


def test_stop_still_wins_when_answer_review_is_off(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Help"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.reply_review_settings("owner", ReplyReviewSettings(enabled=False))
    store.enqueue("owner", chat["id"], TaskInput(message="Hello", request_id="stop-test"))
    task = store.claim("owner")
    token = EventCancellationToken(threading.Event())

    def cancel(_):
        store.cancel("owner", task["id"])
        token._event.set()
        return response("Hello!")

    model = Model([cancel])
    runtime = BotRuntime(store, "owner", lambda _: model)
    runtime.reply_review = ReplyReview(runtime)
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert len(model.requests) == 1
    assert not [
        m for m in store.conversation("owner", chat["id"])["messages"] if m["role"] == "assistant"
    ]
