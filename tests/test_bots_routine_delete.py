from __future__ import annotations

import pytest

from alysis_code.bots.models import BotInput
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.bots.triggers import TriggerInput, TriggerStore


def schedule():
    return ScheduleInput.model_validate(
        {
            "name": "Review",
            "prompt": "Review notes",
            "timing": {"kind": "interval", "every_minutes": 5},
        }
    )


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    return store, store.save_bot("alice", BotInput(name="Atlas", purpose="Notes"))["id"]


def test_a_routine_and_its_history_can_be_deleted(setup):
    store, bot = setup
    item = store.save_schedule("alice", bot, schedule(), now=1000)
    assert store.enqueue_due_schedules("alice", max_calls=10, now=1301) == 1
    store.finish("alice", store.claim("alice")["id"], "completed", content="Reviewed")
    store.delete_schedule("alice", bot, item["id"])
    assert store.list_schedules("alice", bot) == []
    with pytest.raises(NotFound):
        store.schedule_runs("alice", bot, item["id"])
    assert store.enqueue_due_schedules("alice", max_calls=10, now=99999) == 0


def test_a_running_routine_is_not_deleted_and_only_its_owner_can_delete(setup):
    store, bot = setup
    item = store.save_schedule("alice", bot, schedule(), now=1000)
    store.enqueue_due_schedules("alice", max_calls=10, now=1301)
    store.claim("alice")
    with pytest.raises(Conflict, match="current run"):
        store.delete_schedule("alice", bot, item["id"])
    with pytest.raises(NotFound):
        store.delete_schedule("bob", bot, item["id"])
    assert len(store.list_schedules("alice", bot)) == 1


def test_a_file_routine_can_be_deleted(setup):
    store, bot = setup
    triggers = TriggerStore(store, "alice")
    rule = triggers.create(bot, TriggerInput(name="On file", instructions="Review new files"))
    triggers.delete(bot, rule["id"])
    assert triggers.list(bot) == []
    with pytest.raises(NotFound):
        triggers.delete(bot, rule["id"])


def test_routine_delete_routes(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "routines.db", "t" * 40, "fixture", "https://example.invalid", "fixture"
    )
    app = create_app(settings, client_factory=lambda _: None)
    bot = app.state.store.save_bot(settings.owner, BotInput(name="Mira", purpose="Routes"))["id"]
    item = app.state.store.save_schedule(settings.owner, bot, schedule())
    rule = app.state.runtime.triggers.create(
        bot, TriggerInput(name="On file", instructions="Review new files")
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        assert client.delete(f"/v1/bots/{bot}/schedules/{item['id']}").status_code == 204
        assert client.delete(f"/v1/bots/{bot}/triggers/{rule['id']}").status_code == 204
        assert client.delete(f"/v1/bots/{bot}/schedules/{item['id']}").status_code == 404
        assert client.get(f"/v1/bots/{bot}/schedules").json() == []
        assert client.get(f"/v1/bots/{bot}/triggers").json() == []
