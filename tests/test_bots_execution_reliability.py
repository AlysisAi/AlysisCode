from uuid import uuid4

import pytest
from test_bots_browser_runtime import ClientFixture, advertised_names, response
from test_bots_desktop_runtime import run_task
from test_bots_desktop_runtime import study as execution_study
from test_bots_object_storage import setup, task

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.backup import _references
from alysis_code.bots.browser_state import BrowserStates
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import ObjectStorageError
from alysis_code.bots.retention import ObjectRetention, RetentionPolicy
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, NotFound, Store

desktop_study = execution_study


def test_execution_lane_refreshes_choices_and_never_offers_self_updates(desktop_study):
    store, task, desktop, _ = desktop_study
    client = ClientFixture([response("Done")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=desktop)
    run_task(runtime, task)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert "computer_run" in advertised_names(client.requests[0])
    assert "update_background_task" not in advertised_names(client.requests[0])
    assert "stop_background_task" not in advertised_names(client.requests[0])


def test_model_choices_refresh_when_background_work_stops(tmp_path):
    _, store, bot = setup(tmp_path)
    original_id = task(store, bot)
    original = store.task("owner", original_id)
    BackgroundStore(store, "owner").submit(original, "Work pending", [])
    store.enqueue(
        "owner",
        original["chat_id"],
        TaskInput(message="Correct my request", request_id=uuid4().hex),
    )
    current = store.claim("owner")

    class Client(ClientFixture):
        def chat(self, **kwargs):
            result = super().chat(**kwargs)
            if len(self.requests) == 1:
                store.cancel("owner", original_id)
            return result

    client = Client([response(name="list_local_computers"), response("Ready")])
    runtime = BotRuntime(store, "owner", lambda _: client)
    runtime.run(current, runtime.task_token(current["id"]))
    assert store.task("owner", current["id"])["status"] == "completed"
    assert "update_background_task" in advertised_names(client.requests[0])
    updater = next(
        s["function"]
        for s in client.requests[0]["tools"]
        if s["function"]["name"] == "update_background_task"
    )
    assert updater["parameters"]["properties"]["task_id"]["enum"] == [original_id]
    assert "update_background_task" not in advertised_names(client.requests[1])
    assert "stop_background_task" not in advertised_names(client.requests[1])
    for request in client.requests:
        proposal = next(
            s["function"] for s in request["tools"] if s["function"]["name"] == "propose_action"
        )
        assert proposal["parameters"]["properties"]["offline_computer"]["const"] is False
        assert proposal["parameters"]["properties"]["browser_origin"]["type"] == "null"
        facts = [
            m["content"]
            for m in request["messages"]
            if m["role"] == "system"
            and str(m["content"]).startswith("Current execution availability")
        ]
        assert len(facts) == 1
    assert '"active_background_tasks":[]' in facts[0]


def test_browser_state_is_encrypted_owned_and_survives_service_restart(tmp_path):
    objects, store, bot = setup(tmp_path)
    key = bytes(range(32))
    states = BrowserStates(store, key)
    task_id = task(store, bot)
    data = b"synthetic cookie and IndexedDB snapshot" * 4000
    ref = states.stage("owner", bot, task_id, data)
    assert data not in b"".join(objects.read(ref))
    with pytest.raises(Conflict, match="finalizing"):
        states.current("owner", bot)
    store.finish("owner", task_id, "completed", content="Saved")
    reopened = Store(store.path, objects=objects)
    assert BrowserStates(reopened, key).current("owner", bot) == data
    assert not store.list_files("owner", bot)
    other = store.save_bot("owner", BotInput(name="Other", purpose="Isolation"))["id"]
    assert states.current("owner", other) is None
    with pytest.raises(NotFound):
        states.current("another-account", bot)
    with pytest.raises(ObjectStorageError, match="decrypt"):
        BrowserStates(store, bytes(reversed(key))).current("owner", bot)
    with store.connect() as db:
        assert ref in _references(db)
    ObjectRetention(store, "owner", RetentionPolicy(300, 300, 2, 300)).sweep(now=ref.size + 3e9)
    assert states.current("owner", bot) == data


def test_failed_browser_checkpoint_cannot_replace_previous_profile(tmp_path):
    _, store, bot = setup(tmp_path)
    states = BrowserStates(store, bytes(range(32)))
    first = task(store, bot)
    states.stage("owner", bot, first, b"first")
    store.finish("owner", first, "completed", content="Saved")
    second = task(store, bot)
    states.stage("owner", bot, second, b"must not replace")
    store.cancel("owner", second)
    store.finish("owner", second, "cancelled")
    assert states.current("owner", bot) == b"first"
    with pytest.raises(Conflict):
        states.stage("owner", bot, second, b"late write")


def test_missing_browser_key_cannot_silently_open_an_empty_replacement(tmp_path):
    from test_bots_cloud_browser import BrowserProvider

    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec
    from alysis_code.bots.desktop_linux import DesktopError

    _, store, bot = setup(tmp_path)
    task_id = task(store, bot)
    BrowserStates(store, bytes(range(32))).stage("owner", bot, task_id, b"existing profile")
    store.finish("owner", task_id, "completed", content="Saved")
    provider = BrowserProvider()
    factory = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("image"),
        tmp_path / "leases",
        browser_enabled=True,
        store=store,
    )
    with pytest.raises(DesktopError, match="encryption key is required"):
        factory.browser(bot, lambda: False)
    assert all(state == "missing" for _, state in provider.allocations.values())
    assert not any(
        isinstance(action, dict) and action["operation"] == "browser_start"
        for action in provider.actions
    )
