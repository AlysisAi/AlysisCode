"""Fault injection against a real worker process; no paid model calls."""

from __future__ import annotations

import runpy
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse

CHILD = """
import sys, time
from pathlib import Path
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse, ToolCall
class BlockingFixture:
    model = 'process-crash-fixture'
    supports_tool_calling = True
    calls = 0
    def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse('', [ToolCall('save', 'save_file', {'name': 'saved-before-crash.md', 'content': 'Durable result'})], {})
        while True:
            time.sleep(.1)
client = BlockingFixture()
runtime = BotRuntime(Store(Path(sys.argv[1])), 'owner', lambda _: client)
runtime.start()
while True:
    time.sleep(.1)
"""


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("The worker did not reach the expected state")


def test_killed_peer_is_not_replayed_and_reports_once_after_restart(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "scripts/qa/bots_peer_recovery.py"
    result = runpy.run_path(str(fixture))["verify"](tmp_path)
    assert result["fixture_provider_calls"] == 2
    assert result["unknown_fixture_calls"] == 1


@pytest.mark.parametrize("scheduled", [False, True])
def test_killed_worker_retains_files_releases_lease_and_never_replays(tmp_path, scheduled):
    store = Store(tmp_path / "state.db")
    bot = store.save_bot("owner", BotInput(name="Test", purpose="Process recovery"))
    if scheduled:
        schedule = store.save_schedule(
            "owner",
            bot["id"],
            ScheduleInput(
                name="Crash test",
                prompt="Save a result",
                timing={"kind": "once", "at": datetime.fromtimestamp(time.time() + 0.2, UTC)},
            ),
        )
    else:
        chat = store.create_chat("owner", bot["id"], "First")
        task = store.enqueue(
            "owner", chat["id"], TaskInput(message="Save a result", request_id="first_request")
        )
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(store.path)])
    try:
        if scheduled:
            until(lambda: bool(store.schedule_runs("owner", bot["id"], schedule["id"])["runs"]))
            task = store.task(
                "owner",
                store.schedule_runs("owner", bot["id"], schedule["id"])["runs"][0]["task_id"],
            )
        until(lambda: store.task("owner", task["id"])["calls"] == 2)
        assert len(store.list_files("owner", bot["id"])) == 1
        # This process was created solely for fault injection. No user process
        # or existing service is terminated by this test.
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    calls = []

    class FinalFixture:
        model = "restart-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            calls.append(
                next(m for m in reversed(kwargs["messages"]) if m["role"] == "user")["content"]
            )
            return LLMResponse("New task finished", [], {})

    runtime = BotRuntime(Store(store.path), "owner", lambda _: FinalFixture())
    runtime.start()
    try:
        next_chat = store.create_chat("owner", bot["id"], "Second")
        next_task = store.enqueue(
            "owner",
            next_chat["id"],
            TaskInput(message="Continue with new work", request_id="second_request"),
        )
        until(lambda: store.task("owner", next_task["id"])["status"] == "completed")
        interrupted = store.task("owner", task["id"])
        assert interrupted["status"] == "interrupted"
        assert interrupted["usage"][-1]["status"] == "unknown"
        assert calls == ["Continue with new work"]
        if scheduled:
            assert store.list_schedules("owner", bot["id"])[0]["state"] == "paused"
            assert len(store.schedule_runs("owner", bot["id"], schedule["id"])["runs"]) == 1
        files = store.list_files("owner", bot["id"])
        assert len(files) == 1
        assert store.file("owner", bot["id"], files[0]["id"])["content"] == "Durable result"
    finally:
        runtime.stop()
