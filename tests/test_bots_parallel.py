"""Actual scheduling/authority paths; model and VM providers are explicit fixtures."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_bots_cloud_desktop import Provider
from test_bots_desktop import token

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.computer_control import ComputerControls, ControlRequest
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import CooperativeCancellationError
from alysis_code.llm.types import LLMResponse


def queued(store, *, bot=None):
    bot = bot or store.save_bot(
        "owner", BotInput(name=uuid4().hex, purpose="Concurrency fixture", memory_enabled=False)
    )
    chat = store.create_chat("owner", bot["id"], "Independent work")
    return bot, store.enqueue(
        "owner", chat["id"], TaskInput(message="Isolated work", request_id=uuid4().hex)
    )


def until(check):
    deadline = time.monotonic() + 4
    while not check():
        assert time.monotonic() < deadline, "Independent work did not make progress"
        threading.Event().wait(0.01)


@pytest.mark.parametrize("background", [False, True])
def test_real_workers_overlap_and_stop_one_without_stopping_another(tmp_path, background):
    store = Store(tmp_path / "db")
    tasks = [queued(store)[1] for _ in range(3)]
    entered = {task["id"]: threading.Event() for task in tasks}
    release = threading.Event()

    class Client:
        model = "parallel-fixture"
        supports_tool_calling = True

        def __init__(self, task_id):
            self.task_id = task_id

        def chat(self, **kwargs):
            entered[self.task_id].set()
            while not release.wait(0.01):
                kwargs["cancellation_token"].throw_if_cancelled()
            return LLMResponse("Fixture completed", [], {})

    runtime = BotRuntime(store, "owner", Client, limits=RuntimeLimits(foreground_workers=2))
    if background:
        for _ in tasks:
            source = store.claim("owner")
            runtime.background.submit(
                source, "Work", [{"role": "user", "content": source["message"]}]
            )
    runtime.start()
    try:
        assert entered[tasks[0]["id"]].wait(3)
        assert entered[tasks[1]["id"]].wait(3), "Second Bot was serialized behind the first"
        assert not entered[tasks[2]["id"]].is_set(), "Worker bound was exceeded"
        runtime.cancel(tasks[0]["id"])
        until(lambda: store.task("owner", tasks[0]["id"])["status"] == "cancelled")
        assert entered[tasks[2]["id"]].wait(3), "Cancellation did not release this worker slot"
        assert store.task("owner", tasks[1]["id"])["status"] == (
            "waiting" if background else "running"
        )
        assert not release.is_set()
        release.set()
        until(lambda: all(store.task("owner", t["id"])["status"] == "completed" for t in tasks[1:]))
    finally:
        release.set()
        workers = list(runtime._workers)
        runtime.stop()
    assert all(not worker.is_alive() for worker in workers)
    assert store.usage("owner")["calls"] == 3
    assert store.usage("owner")["unconfirmed_calls"] == 1
    # Reacquisition proves no old worker retained authority after Stop.
    replacement = BotRuntime(Store(store.path), "owner", Client)
    replacement.start()
    replacement.stop()


def test_parallel_foreground_keeps_one_writer_for_the_same_bot(tmp_path):
    store = Store(tmp_path / "db")
    bot, first = queued(store)
    _, same = queued(store, bot=bot)
    _, other = queued(store)
    entered = {t["id"]: threading.Event() for t in (first, same, other)}
    release = threading.Event()

    class Client:
        model = "same-bot-fixture"
        supports_tool_calling = True

        def __init__(self, task_id):
            self.task_id = task_id

        def chat(self, **kwargs):
            entered[self.task_id].set()
            if self.task_id == first["id"]:
                while not release.wait(0.01):
                    kwargs["cancellation_token"].throw_if_cancelled()
            return LLMResponse("Fixture completed", [], {})

    runtime = BotRuntime(store, "owner", Client)
    runtime.start()
    try:
        assert entered[first["id"]].wait(3) and entered[other["id"]].wait(3)
        assert not entered[same["id"]].is_set()
        release.set()
        assert entered[same["id"]].wait(3)
        until(lambda: store.task("owner", same["id"])["status"] == "completed")
    finally:
        release.set()
        runtime.stop()


def test_app_manager_reaches_fifo_queue_and_release_does_not_stop_sibling(tmp_path):
    store = Store(tmp_path / "db")
    tasks = [queued(store)[1] for _ in range(3)]
    for _ in tasks:
        assert store.claim("owner") is not None
    provider = Provider()
    factory = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("fixture-image"),
        tmp_path / "leases",
        store=store,
        wait_for_capacity=True,
    )
    app = create_app(
        Settings(
            store.path, "t" * 40, "fixture", "https://example.invalid", "fixture", owner="owner"
        ),
        task_store=store,
        cloud_computers=factory,
    )
    manager = app.state.runtime.desktop_computers
    assert manager.max_computers is None  # Actual app wiring delegates admission.

    def call(task):
        return manager.call(task, "computer_run", {"command": "true"}, token(), approved=True)

    try:
        call(tasks[0])
        call(tasks[1])
        with ThreadPoolExecutor(max_workers=1) as pool:
            third = pool.submit(call, tasks[2])
            until(lambda: factory.capacity()["queued"] == 1)
            assert not third.done() and len(provider.allocations) == 2
            manager.release(tasks[0]["id"], cancelled=True)
            assert third.result(timeout=3)["exit_code"] == 0
        assert manager._computers[tasks[1]["bot_id"]].executor.alive()
        assert factory.capacity()["active"] == 2
        assert sum(state == "running" for _, state in provider.allocations.values()) == 2
    finally:
        manager.close()
    assert all(state == "missing" for _, state in provider.allocations.values())


def test_slow_factory_startup_reserves_capacity_without_blocking_other_bot(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(Provider):
        def create(self, identity, spec):
            if identity.bot_id == "slow":
                entered.set()
                assert release.wait(4)
            return super().create(identity, spec)

    provider = Slow()
    factory = CloudDesktopFactory(
        "owner",
        provider,
        ComputerSpec("fixture-image"),
        tmp_path / "leases",
        wait_for_capacity=True,
    )
    surfaces = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(factory, "slow", tmp_path, cancel=lambda: False)
            try:
                assert entered.wait(2)
                second = pool.submit(factory, "fast", tmp_path, cancel=lambda: False)
                surfaces.append(second.result(timeout=2))
                assert factory.capacity()["active"] == 2
                cancelled = threading.Event()
                cancelled.set()
                with pytest.raises(CooperativeCancellationError):
                    factory("third", tmp_path, cancel=cancelled.is_set)
            finally:
                release.set()
                surfaces.append(first.result(timeout=3))
    finally:
        release.set()
        for surface in surfaces:
            surface.close()


def test_control_status_and_stop_do_not_wait_on_another_bot_provider(tmp_path):
    store = Store(tmp_path / "db")
    bots = [queued(store)[0] for _ in range(2)]
    views = ComputerViews()
    entered, release = threading.Event(), threading.Event()

    def alive():
        entered.set()
        assert release.wait(4)
        return True

    sessions = [
        views.bind(
            "owner",
            bot["id"],
            capture=lambda: b"",
            alive=alive if index == 0 else lambda: True,
            human_input=lambda event: {},
        )
        for index, bot in enumerate(bots)
    ]
    controls = ComputerControls(store, "owner", views)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(controls.status, bots[0]["id"])
            try:
                assert entered.wait(2)
                assert pool.submit(controls.status, bots[1]["id"]).result(timeout=1)["available"]
                pool.submit(controls.stop).result(timeout=1)
            finally:
                release.set()
            first.result(timeout=2)
        with pytest.raises(Conflict, match="stopping"):
            controls.request(
                bots[1]["id"],
                ControlRequest(action="take", client_id=uuid4().hex, session_id=sessions[1]),
            )
    finally:
        release.set()
        controls.stop()


def test_slow_computer_cleanup_does_not_block_sibling_operations(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(Provider):
        def stop(self, ref):
            if ref.identity.bot_id == "slow":
                entered.set()
                assert release.wait(4)
            return super().stop(ref)

    provider = Slow()
    factory = CloudDesktopFactory("owner", provider, ComputerSpec("fixture-image"), tmp_path)
    first = factory("slow", tmp_path, cancel=lambda: False)
    second = factory("fast", tmp_path, cancel=lambda: False)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup = pool.submit(first.close)
            try:
                assert entered.wait(2)
                assert factory.capacity()["active"] == 2
                assert pool.submit(second.alive).result(timeout=1)
                result = pool.submit(second.call, "fast", "run", {"command": "true"})
                assert result.result(timeout=1)["exit_code"] == 0
            finally:
                release.set()
            cleanup.result(timeout=2)
        assert factory.capacity()["active"] == 1 and second.alive()
    finally:
        release.set()
        first.close()
        second.close()


@pytest.mark.parametrize("count", [False, 0, 17])
def test_worker_concurrency_is_bounded(count):
    with pytest.raises(ValueError):
        RuntimeLimits(background_workers=count)
