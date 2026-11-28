"""A task opening a computer hears where it is, from the computer's own start (cloud_desktop.py).

The first browser or computer step of a task waits while its computer boots on this device or
starts or wakes from sleep in the Cloud (about 30 s). The task gets ``computer_phase`` events as
that happens (``queued``, ``starting``, ``ready``), so the app's live line can say "Starting its
computer…" from them, never from a timer.
"""

from __future__ import annotations

import json
import threading

import pytest

from alysis_code.bots.cloud_desktop import PHASE_EVENT, CloudDesktopFactory
from alysis_code.bots.compute import (
    CommandReceipt,
    ComputeError,
    ComputerRef,
    ComputerSpec,
    ComputerStatus,
)
from alysis_code.bots.computer_context import opening_for

OWNER = "owner"


class Provider:
    """A provider whose create waits until released, like a computer booting."""

    scope = "synthetic-scope"
    find_is_complete = True

    def __init__(self):
        self.allocations = {}
        self.creating = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail = None

    def find(self, identity):
        return next(
            (
                ref
                for ref, state in self.allocations.values()
                if ref.identity == identity and state != "missing"
            ),
            None,
        )

    def create(self, identity, spec):
        self.creating.set()
        assert self.release.wait(5)
        ref = ComputerRef("synthetic", self.scope, identity.allocation_id, identity)
        if self.fail is not None:
            # Like a computer on this device that did not boot: its disk is there to clean up.
            self.allocations[ref.resource_id] = (ref, "stopped")
            raise self.fail
        self.allocations[ref.resource_id] = (ref, "running")
        return ref

    def status(self, ref):
        state = self.allocations[ref.resource_id][1]
        return ComputerStatus(state, state)

    def stop(self, ref):
        self.allocations[ref.resource_id] = (ref, "stopped")
        return self.status(ref)

    def delete(self, ref):
        self.allocations[ref.resource_id] = (ref, "missing")
        return self.status(ref)

    def run(self, ref, argv, *, timeout):
        return CommandReceipt(0, json.dumps({"ready": True, "session": argv[-1], "uid": 1000}), "")


class Events:
    """The task store's event write, recorded."""

    def __init__(self):
        self.events = []
        self.path = None

    def event(self, owner, task_id, kind, data):
        self.events.append((owner, task_id, kind, data))

    def phases(self, task_id):
        return [d["phase"] for _, t, k, d in self.events if t == task_id and k == PHASE_EVENT]


@pytest.mark.parametrize("place", ["this device", "cloud"])
def test_a_task_opening_its_computer_is_told_it_starts_then_that_it_is_ready(tmp_path, place):
    provider = Provider()
    factory = CloudDesktopFactory(
        OWNER,
        provider,
        ComputerSpec("image"),
        tmp_path / "ledger",
        max_computers=1 if place == "this device" else None,
        wait_for_capacity=place == "this device",
    )
    factory.store = events = Events()
    provider.release.clear()
    opened = []

    def open_for_task():
        with opening_for("task-1"):
            opened.append(factory("bot", tmp_path, cancel=lambda: False))

    thread = threading.Thread(target=open_for_task)
    thread.start()
    try:
        assert provider.creating.wait(5)
        # While the computer boots or wakes, the task already knows: before the step ends.
        assert events.phases("task-1") == ["starting"]
    finally:
        provider.release.set()
        thread.join(5)
    assert events.phases("task-1") == ["starting", "ready"]
    # Opening it again while it runs tells nothing new: the step does not wait for it.
    computer = opened[0]
    computer.close()
    with opening_for("task-1"):
        again = factory("bot", tmp_path, cancel=lambda: False)
    assert events.phases("task-1") == ["starting", "ready", "starting", "ready"]
    again.close()
    # Opened to be watched, by no task: no task is told.
    viewer = factory("bot", tmp_path, cancel=lambda: False)
    viewer.close()
    assert {task for _, task, _, _ in events.events} == {"task-1"}


def test_a_task_waiting_for_a_free_computer_is_told_once_then_that_it_starts(tmp_path):
    provider = Provider()
    factory = CloudDesktopFactory(
        OWNER,
        provider,
        ComputerSpec("image"),
        tmp_path / "ledger",
        max_computers=1,
        wait_for_capacity=True,
    )
    factory.store = events = Events()
    first = factory("other-bot", tmp_path, cancel=lambda: False)
    opened = []

    def open_for_task():
        with opening_for("task-2"):
            opened.append(factory("bot", tmp_path, cancel=lambda: False))

    thread = threading.Thread(target=open_for_task)
    thread.start()
    try:
        for _ in range(100):
            if events.phases("task-2"):
                break
            thread.join(0.05)
        # Several turns of the wait, one event.
        thread.join(0.6)
        assert events.phases("task-2") == ["queued"]
    finally:
        first.close()
        thread.join(5)
    assert events.phases("task-2") == ["queued", "starting", "ready"]
    opened[0].close()


def test_a_start_that_fails_tells_no_ready_and_the_step_says_why(tmp_path):
    provider = Provider()
    provider.fail = ComputeError("The local computer did not become ready.")
    factory = CloudDesktopFactory(OWNER, provider, ComputerSpec("image"), tmp_path / "ledger")
    factory.store = events = Events()
    with opening_for("task-3"), pytest.raises(ComputeError):
        factory("bot", tmp_path, cancel=lambda: False)
    # The app's line ends with the step's own end; no "ready" for a computer that never came.
    assert events.phases("task-3") == ["starting"]
    assert factory._told == {}


def test_a_cloud_computer_waking_from_sleep_tells_its_task_the_same_way(tmp_path):
    from uuid import uuid4

    from test_bots_memory_sleep import MemoryProvider
    from test_bots_memory_sleep import factory as sleeping_factory

    provider = MemoryProvider()
    first = sleeping_factory(tmp_path, provider)("bot", tmp_path, cancel=lambda: False)
    first.suspend(uuid4().hex)
    awake = sleeping_factory(tmp_path, provider)
    awake.store = events = Events()
    with opening_for("task-wake"):
        woken = awake("bot", tmp_path, cancel=lambda: False)
    # It woke the sleeping computer (no new one), and the task heard it start and come back.
    assert provider.resumes == 1 and provider.starts == 1
    assert events.phases("task-wake") == ["starting", "ready"]
    woken.close()
