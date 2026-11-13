"""Viewer-created sessions use real runtime ownership without model/task fabrication."""

import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_bots_cloud_desktop import CheckpointProvider
from test_bots_desktop import Executor

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputeCreationRefused, ComputerSpec
from alysis_code.bots.computer import ComputerControlPause, ComputerViews
from alysis_code.bots.computer_control import ComputerControls, ControlRequest
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.hosted_maintenance import MaintenanceLease
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Conflict, Store
from alysis_code.bots.workspace_objects import WorkspaceObjects


def session(tmp_path, *, cloud=False, objects_enabled=True):
    objects = LocalObjectStorage(tmp_path / "objects") if objects_enabled else None
    store = Store(tmp_path / "db", objects=objects)
    bot = store.save_bot("owner", BotInput(name="Viewer", purpose="Work"))["id"]
    views = ComputerViews()
    provider = CheckpointProvider() if cloud else None
    factory = (
        CloudDesktopFactory(
            "owner",
            provider,
            ComputerSpec("image"),
            tmp_path / "leases",
            persist_workspace=True,
            store=store,
        )
        if cloud
        else Executor
    )
    manager = DesktopComputers("owner", views, factory=factory, store=store)
    controls = ComputerControls(store, "owner", views)
    controls.adopt_computer = manager.adopt
    return store, bot, manager, controls, provider


def test_stop_reaps_viewer_that_was_adopted_by_a_task(tmp_path):
    store, bot, manager, controls, provider = session(tmp_path, cloud=True)
    try:
        controls.open(bot, manager, lambda: False)  # viewer callback only watches service exit
        chat = store.create_chat("owner", bot, "Adopt viewer")
        store.enqueue("owner", chat["id"], TaskInput(message="Help", request_id=uuid4().hex))
        task = store.claim("owner")
        manager.adopt(task)
        store.cancel("owner", task["id"])
        manager.reap()
        assert manager.views.current("owner", bot)[0] is None
        assert all(state == "missing" for _, state in provider.allocations.values())
    finally:
        manager.close()


@pytest.mark.parametrize("objects_enabled", [False, True])
def test_viewer_idle_checkpoints_without_task_and_reopens_saved_workspace(
    tmp_path, objects_enabled
):
    store, bot, manager, controls, provider = session(
        tmp_path, cloud=True, objects_enabled=objects_enabled
    )
    try:
        controls.open(bot, manager, lambda: False)
        first = manager.views.current("owner", bot)[0].generation
        controls.open(bot, manager, lambda: False)
        assert manager.views.current("owner", bot)[0].generation == first
        assert len(provider.allocations) == 1
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM chats").fetchone()[0] == 0
        now = controls._row(bot)["updated_at"]
        controls.wall_clock = lambda: now + 299
        controls.expire_idle(manager.suspend)
        assert manager.views.current("owner", bot)[0] is not None
        controls.wall_clock = lambda: now + 301
        controls.expire_idle(manager.suspend)
        assert controls._row(bot)["state"] == "idle"
        assert manager.views.current("owner", bot)[0] is None
        assert all(state == "missing" for _, state in provider.allocations.values())
        if objects_enabled:
            assert WorkspaceObjects(store).current("owner", bot) is not None
        controls.open(bot, manager, lambda: False)
        assert manager.views.current("owner", bot)[0].generation != first
        assert provider.restored == [provider.checkpoint]
    finally:
        manager.close()


@pytest.mark.parametrize("human_control", [False, True])
def test_real_task_adopts_viewing_computer_and_preserves_human_pause(
    tmp_path, monkeypatch, human_control
):
    monkeypatch.setattr(Executor, "human_input", lambda *_: None, raising=False)
    store, bot, manager, controls, _ = session(tmp_path)
    try:
        controls.open(bot, manager, lambda: False)
        generation = manager.views.current("owner", bot)[0].generation
        if human_control:
            controls.request(
                bot, ControlRequest(action="take", client_id=uuid4().hex, session_id=generation)
            )
        chat = store.create_chat("owner", bot, "Real task")
        store.enqueue("owner", chat["id"], TaskInput(message="Help", request_id=uuid4().hex))
        task = store.claim("owner")
        if human_control:
            with pytest.raises(ComputerControlPause):
                controls.check(task)
            assert controls._row(bot)["task_id"] == task["id"]
        else:
            controls.check(task)
            assert controls._row(bot) is None
        assert manager._computers[bot].task_id == task["id"]
        controls.open(bot, manager, lambda: False)
        assert manager.views.current("owner", bot)[0].generation == generation
        manager.release(task["id"], cancelled=False)
        assert manager.views.current("owner", bot)[0] is None
    finally:
        manager.close()


def test_opening_is_transactional_against_maintenance_and_has_no_model_calls(tmp_path):
    store, bot, manager, controls, _ = session(tmp_path)
    lease = MaintenanceLease(store)
    token = lease.acquire()
    with pytest.raises(Conflict, match="maintenance"):
        controls.open(bot, manager, lambda: False)
    assert manager._computers == {}
    lease.release(token)
    entered = threading.Event()
    release = threading.Event()
    original = manager.factory

    def factory(*a, **kw):
        entered.set()
        assert release.wait(3)
        return original(*a, **kw)

    manager.factory = factory
    opening = threading.Thread(target=controls.open, args=(bot, manager, lambda: False))
    opening.start()
    assert entered.wait(2)
    try:
        assert lease.acquire() is None
        assert controls.status(bot) == {"state": "agent", "available": False}
    finally:
        release.set()
        opening.join(3)
        manager.close()


def test_api_open_is_async_owner_scoped_and_duplicate_requests_reuse_one_start(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    made = []

    def factory(*a, **kw):
        entered.set()
        assert release.wait(3)
        executor = Executor(*a, **kw)
        made.append(executor)
        return executor

    settings = Settings(
        tmp_path / "db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    views = ComputerViews()
    manager = DesktopComputers(settings.owner, views, factory=factory)
    app = create_app(
        settings, desktop_computers=manager, client_factory=lambda _: pytest.fail("No model call")
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Test", "purpose": "Viewer"}).json()["id"]
        path = f"/v1/bots/{bot}/computer"
        assert client.post(path + "/open").status_code == 202
        assert entered.wait(2)
        try:
            assert client.post(path + "/open").status_code == 202
            assert client.get(path).json()["lifecycle"] == "starting"
            assert client.post("/v1/bots/" + "f" * 32 + "/computer/open").status_code == 404
            assert (
                client.post(path + "/open", headers={"Authorization": "Bearer invalid"}).status_code
                == 401
            )
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while (frame := client.get(path).json())["state"] != "live" or frame.get(
            "lifecycle"
        ) != "ready":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert len(made) == 1
        assert client.post(path + "/open").status_code == 202
        assert client.get(path).json()["lifecycle"] == "ready"


def test_viewer_prepares_a_lazy_display_without_model_or_task(tmp_path):
    class LazyDisplay(Executor):
        display_ready = False

        def capture(self):
            if not self.display_ready:
                raise RuntimeError("Display is not started")
            return super().capture()

        def call(self, owner, operation, args, **kw):
            if operation == "observe":
                self.display_ready = True
            return super().call(owner, operation, args, **kw)

    store, bot, manager, controls, _ = session(tmp_path)
    manager.factory = LazyDisplay
    try:
        controls.open(bot, manager, lambda: False)
        assert manager.views.frame("owner", bot)["state"] == "live"
        assert controls._row(bot)["task_id"] is None
        before = list(manager._computers[bot].executor.calls)
        controls.open(bot, manager, lambda: False)
        assert manager._computers[bot].executor.calls == before
    finally:
        manager.close()


@pytest.mark.parametrize(
    ("failure", "shown"),
    [
        (
            ComputeCreationRefused(
                "Not enough free disk space for another private computer. It needs about 7 GB "
                "free and this device has 4.8 GB. Free up space or finish the other task, then "
                "try again.",
                code="insufficient_local_storage",
            ),
            "Not enough free disk space for another private computer. It needs about 7 GB free "
            "and this device has 4.8 GB. Free up space or finish the other task, then try again.",
        ),
        # Anything untyped stays generic: it may carry paths or provider details.
        (
            RuntimeError("C:\\secret\\path failed"),
            "The computer could not start. Please try again. Your saved files are retained.",
        ),
    ],
)
def test_api_open_shows_why_a_computer_was_refused(tmp_path, failure, shown):
    def factory(*a, **kw):
        raise failure

    settings = Settings(
        tmp_path / "db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    manager = DesktopComputers(settings.owner, ComputerViews(), factory=factory)
    app = create_app(
        settings, desktop_computers=manager, client_factory=lambda _: pytest.fail("No model call")
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Test", "purpose": "Viewer"}).json()["id"]
        path = f"/v1/bots/{bot}/computer"
        assert client.post(path + "/open").status_code == 202
        deadline = time.monotonic() + 3
        while (frame := client.get(path).json()).get("lifecycle") != "failed":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert frame["error"] == shown


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_plain_followup_releases_its_adopted_viewer_and_preserves_other_bots(tmp_path, outcome):
    from alysis_code.bots.runtime import BotRuntime
    from alysis_code.cancellation import EventCancellationToken
    from alysis_code.llm.types import LLMResponse, LLMUsage

    store, bot, manager, _, _ = session(tmp_path)
    stopped = threading.Event()

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **_):
            if outcome == "failed":
                raise ValueError("Synthetic provider failure")
            if outcome == "cancelled":
                stopped.set()
            return LLMResponse("The total is 10.", [], {}, usage=LLMUsage(10, 2, 12))

    runtime = BotRuntime(store, "owner", lambda _: Client(), desktop_computers=manager)
    other = store.save_bot("owner", BotInput(name="Other", purpose="Independent work"))["id"]
    try:
        for identity in (bot, other):
            runtime.computer_controls.open(identity, manager, lambda: False)
        own_executor = manager._computers[bot].executor
        other_executor = manager._computers[other].executor
        chat = store.create_chat("owner", bot, "Follow up")
        store.enqueue(
            "owner", chat["id"], TaskInput(message="What is the total?", request_id=uuid4().hex)
        )
        task = store.claim("owner")
        runtime.run(task, EventCancellationToken(stopped))
        assert store.task("owner", task["id"])["status"] == outcome
        assert runtime.computer_controls._row(bot) is None
        assert bot not in manager._computers and own_executor.closed
        assert manager._computers[other].executor is other_executor and not other_executor.closed
        assert runtime.computer_controls._row(other)["state"] == "viewing"
    finally:
        manager.close()


def test_owl_list_says_when_an_owl_waits_for_a_computer(tmp_path):
    settings = Settings(
        tmp_path / "db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    views = ComputerViews()
    app = create_app(
        settings, computer_views=views, client_factory=lambda _: pytest.fail("No model")
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        waiting = client.post("/v1/bots", json={"name": "Waits", "purpose": "Queue"}).json()["id"]
        idle = client.post("/v1/bots", json={"name": "Idle", "purpose": "Nothing"}).json()["id"]
        views.set_lifecycle(settings.owner, waiting, "queued")
        listed = {bot["id"]: bot["computer"] for bot in client.get("/v1/bots").json()}
        assert listed == {waiting: "queued", idle: None}


def test_an_owl_needs_only_a_name(tmp_path):
    settings = Settings(
        tmp_path / "db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    app = create_app(settings, client_factory=lambda _: pytest.fail("No model"))
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + settings.token
        made = client.post("/v1/bots", json={"name": "Atlas"})
        assert made.status_code == 201
        assert made.json()["purpose"] == "" and made.json()["instructions"] == ""
        assert client.post("/v1/bots", json={"name": " "}).status_code == 422
