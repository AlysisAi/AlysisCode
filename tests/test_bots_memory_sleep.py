"""Controller/transport qualification. Actual RAM preservation needs live Azure."""

import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_cloud_desktop import Provider
from test_bots_compute import AzureWire
from test_bots_compute_capacity import isolated  # noqa: F401

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import CommandReceipt, ComputerSpec, ComputeUncertain
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.hosted_maintenance import MaintenanceLease
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.workspace_objects import WorkspaceObjects


class MemoryProvider(Provider):
    supports_memory_sleep = True

    def __init__(self):
        super().__init__()
        self.source = b""
        self.sources = {}
        self.starts = 0
        self.resumes = 0
        self.deleted = []

    def upload(self, ref, path, chunks, *, size, cancel):
        assert Path(path).name in {"cloud_desktop_worker.py", "screen_channel.py"}
        self.source = b"".join(chunks)
        self.sources[Path(path).name] = self.source
        self.last_source = Path(path).name
        assert len(self.source) == size and not cancel()

    def start(self, ref):
        self.resumes += 1
        self.allocations[ref.resource_id] = (ref, "running")
        if self.mode == "lost-resume":
            raise ComputeUncertain("Lost resume receipt")
        return self.status(ref)

    def delete(self, ref):
        self.deleted.append(ref.resource_id)
        return super().delete(ref)

    def run(self, ref, argv, *, timeout):
        self.actions.append(argv)
        if len(argv) == 4:
            digest = (
                "wrong"
                if self.mode == self.last_source
                else hashlib.sha256(self.source).hexdigest()
            )
            return CommandReceipt(0, digest, "")
        if argv[-2] == "start_memory":
            self.starts += 1
            return CommandReceipt(
                0,
                json.dumps({"ready": True, "session": argv[-1], "uid": 1000, "memory_sleep": True}),
                "",
            )
        request = json.loads(base64.b64decode(argv[-1]))
        result = {"ready": True, "uid": 1000, "memory_sleep": self.mode != "lost-memory"}
        if request["operation"] == "resume_memory":
            result["surface"] = "browser" if self.mode == "browser-screen" else "desktop"
        return CommandReceipt(
            0,
            json.dumps({"id": request["id"], "session": request["session"], "result": result}),
            "",
        )


def factory(tmp_path, provider):
    return CloudDesktopFactory(
        "owner", provider, ComputerSpec("image", suspend_mode="Memory"), tmp_path / "leases"
    )


@pytest.mark.parametrize("browser", [False, True])
def test_idle_memory_retains_owned_resource_and_reattaches_after_controller_restart(
    tmp_path, browser
):
    provider = MemoryProvider()
    first_factory = factory(tmp_path, provider)
    first = first_factory("bot", tmp_path, cancel=lambda: False)
    assert set(provider.sources) == {"cloud_desktop_worker.py", "screen_channel.py"}
    original = first.ref
    first.suspend(uuid4().hex)
    assert first._released
    assert provider.status(original).state == "stopped"
    assert not provider.deleted
    record = json.loads(first._record.read_text())
    assert record["state"] == "sleeping" and record["resume_kind"] == "memory"
    if browser:
        provider.mode = "browser-screen"
    second_factory = factory(tmp_path, provider)
    second = second_factory("bot", tmp_path, cancel=lambda: False)
    assert second.ref == original and provider.starts == 1 and provider.resumes == 1
    assert second._computer._resume_surface == ("browser" if browser else "desktop")
    requests = [
        json.loads(base64.b64decode(args[-1]))
        for args in provider.actions
        if len(args) > 4 and args[-2] != "start_memory"
    ]
    assert [request["operation"] for request in requests] == ["resume_memory"]
    assert not provider.deleted
    second.close()
    assert provider.deleted == [original.resource_id]


@pytest.mark.parametrize("mode", ["lost-resume", "lost-memory"])
def test_uncertain_resume_never_erases_or_replaces_the_sleeping_computer(tmp_path, mode):
    provider = MemoryProvider()
    computers = factory(tmp_path, provider)
    first = computers("bot", tmp_path, cancel=lambda: False)
    first.suspend(uuid4().hex)
    provider.mode = mode
    with pytest.raises((ComputeUncertain, DesktopError)):
        computers("bot", tmp_path, cancel=lambda: False)
    assert not provider.deleted and len(provider.allocations) == 1
    provider.mode = None
    second = computers("bot", tmp_path, cancel=lambda: False)
    assert second.ref == first.ref and provider.starts == 1
    second.close()


def test_failed_memory_stop_remains_owned_and_cleanup_retries_sleep_not_deletion(tmp_path):
    provider = MemoryProvider()
    computers = factory(tmp_path, provider)
    first = computers("bot", tmp_path, cancel=lambda: False)
    provider.allocations[first.ref.resource_id] = (first.ref, "transitioning")
    with pytest.raises(DesktopError, match="unconfirmed"):
        first.suspend(uuid4().hex)
    assert json.loads(first._record.read_text())["state"] == "sleep_requested"
    assert not provider.deleted
    provider.allocations[first.ref.resource_id] = (first.ref, "stopped")
    first.close()
    assert json.loads(first._record.read_text())["state"] == "sleeping"
    assert not provider.deleted


@pytest.mark.parametrize("source", ["cloud_desktop_worker.py", "screen_channel.py"])
def test_unverified_bridge_source_cannot_start_memory_computer(tmp_path, source):
    provider = MemoryProvider()
    provider.mode = source
    with pytest.raises(DesktopError, match="installation"):
        factory(tmp_path, provider)("bot", tmp_path, cancel=lambda: False)
    assert provider.starts == 0
    assert all(state == "missing" for _, state in provider.allocations.values())


@pytest.mark.parametrize("bounded", [False, True])
def test_memory_policy_is_explicit_and_only_bounded_tests_auto_delete(bounded):
    import time

    wire = AzureWire()
    if bounded:
        wire.provider.expires_at = time.time() + 1800
    wire.provider.create(
        wire.identity, ComputerSpec(str(uuid4()), suspend_mode="Memory", idle_seconds=180)
    )
    policy = json.loads(wire.requests[-1].content)["lifecycle"]
    assert policy["autoSuspendPolicy"] == {"enabled": True, "interval": 180, "mode": "Memory"}
    assert policy["autoDeletePolicy"]["enabled"] is bounded
    wire.provider.close()


def test_unsupported_provider_cannot_silently_claim_memory_preservation(tmp_path):
    with pytest.raises(ValueError, match="does not support"):
        factory(tmp_path, Provider())


def test_older_workers_refuse_the_memory_allocation_database(tmp_path, monkeypatch):
    from alysis_code.bots import store as store_module

    path = tmp_path / "db"
    store_module.Store(path)
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 38)
    with pytest.raises(RuntimeError, match="compatible runtime"):
        store_module.Store(path)


def test_recovery_api_requires_authentication_owner_and_explicit_confirmation(tmp_path):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    settings = Settings(
        tmp_path / "db", "t" * 40, "fixture", "https://example.invalid", "fixture", max_calls=0
    )
    app = create_app(settings, client_factory=lambda _: pytest.fail("No model call"))
    with TestClient(app) as client:
        assert (
            client.post(
                f"/v1/bots/{uuid4().hex}/computer/recover", json={"confirm": True}
            ).status_code
            == 401
        )
        client.headers["Authorization"] = "Bearer " + settings.token
        bot = client.post("/v1/bots", json={"name": "Test", "purpose": "Recovery"}).json()["id"]
        path = f"/v1/bots/{bot}/computer/recover"
        for body in ({}, {"confirm": False}):
            assert client.post(path, json=body).status_code == 422
        assert client.post(path, json={"confirm": True}).status_code == 409
        assert (
            client.post(
                f"/v1/bots/{uuid4().hex}/computer/recover", json={"confirm": True}
            ).status_code
            == 404
        )
        assert client.get(path).status_code == 405


def test_three_minute_idle_preserves_resource_and_active_operation_defers_sleep(tmp_path):
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.computer_control import ComputerControls
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.store import Store

    store = Store(tmp_path / "db")
    bot = store.save_bot("owner", BotInput(name="Viewer", purpose="Idle"))["id"]
    provider = MemoryProvider()
    views = ComputerViews()
    computers = factory(tmp_path, provider)
    manager = DesktopComputers("owner", views, factory=computers, store=store)
    controls = ComputerControls(store, "owner", views, idle_seconds=180, memory_sleep=True)
    controls.open(bot, manager, lambda: False)
    original = manager._computers[bot].executor.ref
    started = controls._row(bot)["updated_at"]
    controls.wall_clock = lambda: started + 179
    controls.expire_idle(manager.suspend)
    assert provider.status(original).state == "running"
    controls.wall_clock = lambda: started + 181
    with views.agent_operation("owner", bot):
        controls.expire_idle(manager.suspend)
        assert provider.status(original).state == "running"
    controls.expire_idle(manager.suspend)
    assert controls._row(bot)["state"] == "idle"
    assert provider.status(original).state == "stopped" and not provider.deleted


def shared_factory(tmp_path, provider, store, owner):
    bot = store.save_bot(owner, BotInput(name="Memory test", purpose="Sleep safely"))["id"]
    computers = CloudDesktopFactory(
        owner,
        provider,
        ComputerSpec("image", suspend_mode="Memory"),
        tmp_path / owner,
        store=store,
        max_computers=1,
    )
    return bot, computers


def test_shared_resume_reserves_capacity_and_keeps_sleeping_ownership(isolated, tmp_path):  # noqa: F811
    provider = MemoryProvider()
    first_store, second_store = isolated("first"), isolated("second")
    first_bot, first_factory = shared_factory(tmp_path, provider, first_store, "first")
    second_bot, second_factory = shared_factory(tmp_path, provider, second_store, "second")
    original = first_factory(first_bot, tmp_path, cancel=lambda: False)
    original.suspend(uuid4().hex)
    ledger = AllocationLedger(first_store, "first")
    assert ledger.capacity(provider.scope) == 0 and not ledger.pending(include_sleeping=False)
    first_factory.reconcile()
    assert not provider.deleted and ledger.get(first_bot)["state"] == "sleeping"
    other = second_factory(second_bot, tmp_path, cancel=lambda: False)
    with pytest.raises(DesktopError, match="busy"):
        first_factory(first_bot, tmp_path, cancel=lambda: False)
    assert ledger.get(first_bot)["state"] == "sleeping"
    other.close()
    restored = first_factory(first_bot, tmp_path, cancel=lambda: False)
    assert restored.ref == original.ref and ledger.capacity(provider.scope) == 1
    restored.close()


def test_maintenance_admission_cannot_resume_paid_compute(isolated, tmp_path):  # noqa: F811
    provider = MemoryProvider()
    store = isolated("owner")
    bot, computers = shared_factory(tmp_path, provider, store, "owner")
    computer = computers(bot, tmp_path, cancel=lambda: False)
    computer.suspend(uuid4().hex)
    lease = MaintenanceLease(store)
    token = lease.acquire()
    assert token is not None
    try:
        with pytest.raises(DesktopError, match="maintenance"):
            computers(bot, tmp_path, cancel=lambda: False)
        assert provider.resumes == 0 and not provider.deleted
    finally:
        lease.release(token)
    computers(bot, tmp_path, cancel=lambda: False).close()


def test_maintenance_blocks_recovery_before_provider_mutation(tmp_path):
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.computer_control import ComputerControls
    from alysis_code.bots.store import Conflict, Store

    store = Store(tmp_path / "db")
    bot = store.save_bot("owner", BotInput(name="Viewer", purpose="Recovery"))["id"]
    controls = ComputerControls(store, "owner", ComputerViews(), memory_sleep=True)

    class Factory:
        def recover(self, _bot):
            pytest.fail("Maintenance must block recovery before any provider call")

    lease = MaintenanceLease(store)
    token = lease.acquire()
    assert token is not None
    try:
        with pytest.raises(Conflict, match="maintenance"):
            controls.recover_saved(bot, Factory())
    finally:
        lease.release(token)


def test_recover_requires_backup_and_replaces_only_the_detached_owned_computer(isolated, tmp_path):  # noqa: F811
    provider = MemoryProvider()
    store = isolated("owner")
    bot, computers = shared_factory(tmp_path, provider, store, "owner")
    computer = computers(bot, tmp_path, cancel=lambda: False)
    with pytest.raises(DesktopError, match="active session"):
        computers.recover(bot)
    computer.suspend(uuid4().hex)
    with pytest.raises(DesktopError, match="no saved"):
        computers.recover(bot)
    assert not provider.deleted
    chat = store.create_chat("owner", bot, "Backup")
    store.enqueue("owner", chat["id"], TaskInput(message="Save", request_id=uuid4().hex))
    task = store.claim("owner")
    backups = WorkspaceObjects(store)
    backups.stage(
        "owner", bot, task["id"], [b"synthetic archive bytes"], len(b"synthetic archive bytes")
    )
    store.finish("owner", task["id"], "completed")
    before = backups.current("owner", bot)
    assert before is not None and computers.recovery_available(bot)
    computers.recover(bot)
    assert provider.deleted == [computer.ref.resource_id]
    assert AllocationLedger(store, "owner").get(bot)["state"] == "deleted"
    assert backups.current("owner", bot) == before


@pytest.mark.parametrize("viewer_adopted", [False, True])
def test_idle_releases_both_view_generations_of_one_memory_computer(tmp_path, viewer_adopted):
    from alysis_code.bots.browser import BrowserComputers, _Computer
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.runtime import BotRuntime
    from alysis_code.bots.store import Conflict, Store

    store = Store(tmp_path / "db")
    bot = store.save_bot("owner", BotInput(name="Viewer first", purpose="Sleep"))["id"]
    other = store.save_bot("owner", BotInput(name="Unrelated viewer", purpose="Stay open"))["id"]
    provider = MemoryProvider()
    computers = factory(tmp_path, provider)
    views = ComputerViews()
    desktop = DesktopComputers("owner", views, factory=computers, store=store)
    browser = BrowserComputers("owner", views, lambda *_: None)
    runtime = BotRuntime(
        store, "owner", lambda _: None, desktop_computers=desktop, browser_computers=browser
    )
    controls = runtime.computer_controls
    try:
        controls.open(bot, desktop, lambda: False)
        controls.open(other, desktop, lambda: False)
        original = desktop._computers[bot].executor.ref
        unrelated = desktop._computers[other].executor.ref
        first_generation = desktop._computers[bot].generation
        chat = store.create_chat("owner", bot, "Chat")
        store.enqueue(
            "owner", chat["id"], TaskInput(message="Open browser", request_id="browser-idle")
        )
        task = store.claim("owner")
        if viewer_adopted:
            desktop.adopt(task)
        surface = computers._acquire(bot, "browser", lambda: False)
        generation = views.bind(
            "owner", bot, surface="browser", capture=lambda: b"", alive=surface.alive
        )
        browser._computers[bot] = _Computer(surface, generation, task["id"], browser.clock())
        views.claim("owner", bot, task["id"], "browser")
        views.activate("owner", bot, generation)
        assert generation != first_generation
        with store.connect(write=True) as db:
            db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
            db.execute(
                "UPDATE bot_computer_control SET task_id=?,session_id=?,state='paused' WHERE bot_id=?",
                (task["id"], generation, bot),
            )
        started = controls._row(bot)["updated_at"]
        with store.connect(write=True) as db:
            db.execute(
                "UPDATE bot_computer_control SET updated_at=? WHERE bot_id=?",
                (started + 100, other),
            )
        controls.wall_clock = lambda: started + 181
        # Stale admission must not revoke either view or stop unrelated work.
        with pytest.raises(Conflict):
            runtime._suspend_computer(task["id"], first_generation)
        with views.agent_operation("owner", bot):
            controls.expire_idle(runtime._suspend_computer)
        assert provider.status(original).state == "running"
        controls.expire_idle(runtime._suspend_computer)
        assert controls._row(bot)["state"] == "idle"
        assert bot not in desktop._computers and bot not in browser._computers
        assert views.current("owner", bot)[0] is None
        assert provider.status(original).state == "stopped"
        assert provider.status(unrelated).state == "running"
        assert other in desktop._computers and not provider.deleted
        assert store.task("owner", task["id"])["status"] == "waiting"
        controls.open(bot, desktop, lambda: False)
        assert desktop._computers[bot].executor.ref == original
        assert provider.status(original).state == "running" and provider.resumes == 1
    finally:
        browser.close()
        desktop.close()


def test_idle_without_a_task_is_scoped_to_one_bot_across_both_surfaces(tmp_path):
    from alysis_code.bots.browser import BrowserComputers, _Computer
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.runtime import BotRuntime
    from alysis_code.bots.store import Store

    store = Store(tmp_path / "db")
    first, other = [
        store.save_bot("owner", BotInput(name=name, purpose="Viewer"))["id"]
        for name in ("First", "Other")
    ]
    provider = MemoryProvider()
    computers, views = factory(tmp_path, provider), ComputerViews()
    desktop = DesktopComputers("owner", views, factory=computers, store=store)
    browser = BrowserComputers("owner", views, lambda *_: None)
    runtime = BotRuntime(
        store, "owner", lambda _: None, desktop_computers=desktop, browser_computers=browser
    )
    try:
        for bot in (first, other):
            runtime.computer_controls.open(bot, desktop, lambda: False)
            surface = computers._acquire(bot, "browser", lambda: False)
            generation = views.bind(
                "owner", bot, surface="browser", capture=lambda: b"", alive=surface.alive
            )
            browser._computers[bot] = _Computer(surface, generation, None, browser.clock())
            views.activate("owner", bot, generation)
        original = desktop._computers[first].executor.ref
        unrelated = desktop._computers[other].executor.ref
        generation = browser._computers[first].generation
        with store.connect(write=True) as db:
            db.execute(
                "UPDATE bot_computer_control SET session_id=?,state='suspending' WHERE bot_id=?",
                (generation, first),
            )
        runtime._suspend_computer(None, generation)
        assert first not in desktop._computers and first not in browser._computers
        assert provider.status(original).state == "stopped"
        assert other in desktop._computers and other in browser._computers
        assert provider.status(unrelated).state == "running" and not provider.deleted
    finally:
        browser.close()
        desktop.close()
