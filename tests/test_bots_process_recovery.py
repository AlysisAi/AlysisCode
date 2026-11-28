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
FIRST_STEPS = {
    # Keeps the owl's own record: a later run sees it.
    'save': ToolCall('save', 'save_file', {'name': 'saved-before-crash.md', 'content': 'Durable result'}),
    # Shows the person a card: something outside the owl's own records.
    'card': ToolCall('card', 'propose_action', {'question': 'Να το στείλω;', 'instructions': '发送报告。', 'offline_computer': False}),
}
class BlockingFixture:
    model = 'process-crash-fixture'
    supports_tool_calling = True
    calls = 0
    def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse('', [FIRST_STEPS[sys.argv[2]]], {})
        while True:
            time.sleep(.1)
client = BlockingFixture()
runtime = BotRuntime(Store(Path(sys.argv[1])), 'owner', lambda _: client)
runtime.start()
while True:
    time.sleep(.1)
"""


def until(predicate, timeout=60, *, process=None, stderr_path=None):
    """Wait for ``predicate``; the deadline only bounds a failure.

    A passing check returns as soon as its state appears. The bound is generous
    because the worker child imports the engine before it starts, which can take
    many seconds on a busy machine. If ``process`` exits first, the wait ends at
    once with its exit code and the end of its error output.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process is not None and process.poll() is not None:
            detail = ""
            if stderr_path is not None and stderr_path.exists():
                detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise AssertionError(
                f"The worker exited early with code {process.returncode}"
                + (f":\n{detail}" if detail else "")
            )
        time.sleep(0.02)
    raise AssertionError("The worker did not reach the expected state")


def test_killed_peer_is_not_replayed_and_reports_once_after_restart(tmp_path, monkeypatch):
    # Another source tree with an engine package of its own sits first on the import path,
    # as an earlier test in the same worker can leave one: the fixture's child process
    # still runs the engine under test.
    other_tree = tmp_path / "other-source-tree"
    (other_tree / "alysis_code").mkdir(parents=True)
    (other_tree / "alysis_code" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(other_tree))
    fixture = Path(__file__).resolve().parents[1] / "scripts/qa/bots_peer_recovery.py"
    result = runpy.run_path(str(fixture))["verify"](tmp_path)
    assert result["fixture_provider_calls"] == 2
    assert result["unknown_fixture_calls"] == 1


@pytest.mark.parametrize("step", ["save", "card"])
@pytest.mark.parametrize("scheduled", [False, True])
def test_killed_worker_retains_files_releases_lease_and_replays_only_what_changed_nothing(
    tmp_path, scheduled, step
):
    """A killed turn starts again once, told so, when its only step kept the owl's own record;
    one that showed the person a card is never replayed."""
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
    child_stderr = tmp_path / "worker-child-stderr.txt"
    with child_stderr.open("wb") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(store.path), step], stderr=stderr
        )
    try:
        if scheduled:
            until(
                lambda: bool(store.schedule_runs("owner", bot["id"], schedule["id"])["runs"]),
                process=child,
                stderr_path=child_stderr,
            )
            task = store.task(
                "owner",
                store.schedule_runs("owner", bot["id"], schedule["id"])["runs"][0]["task_id"],
            )
        until(
            lambda: store.task("owner", task["id"])["calls"] == 2,
            process=child,
            stderr_path=child_stderr,
        )
        assert len(store.list_files("owner", bot["id"])) == int(step == "save")
        # This process was created solely for fault injection. No user process
        # or existing service is terminated by this test.
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    calls = []
    told = []

    class FinalFixture:
        model = "restart-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            calls.append(
                next(m for m in reversed(kwargs["messages"]) if m["role"] == "user")["content"]
            )
            told.append(str(kwargs["messages"]))
            return LLMResponse("New task finished", [], {})

    runtime = BotRuntime(Store(store.path), "owner", lambda _: FinalFixture())
    runtime.start()
    try:
        if step == "save":
            # The cut-off turn queues again and runs first, as the oldest.
            until(lambda: store.task("owner", task["id"])["status"] == "completed")
        next_chat = store.create_chat("owner", bot["id"], "Second")
        next_task = store.enqueue(
            "owner",
            next_chat["id"],
            TaskInput(message="Continue with new work", request_id="second_request"),
        )
        until(lambda: store.task("owner", next_task["id"])["status"] == "completed")
        cut = store.task("owner", task["id"])
        # The call it was waiting on when killed may have been charged.
        assert cut["usage"][1]["status"] == "unknown"
        if step == "save":
            assert cut["status"] == "completed" and cut["restarts"] == 1
            assert calls == ["Save a result", "Continue with new work"]
            # It is told it restarted and what it saved before, and saves nothing twice.
            assert "interrupted by a restart" in told[0] and "saved-before-crash.md" in told[0]
            assert "interrupted by a restart" not in told[1]
        else:
            assert cut["status"] == "interrupted" and cut["restarts"] == 0
            assert cut["failure"] == {
                "kind": "service",
                "not_rerun": "acted",
                "step": "propose_action",
            }
            assert calls == ["Continue with new work"]
        if scheduled:
            # Its only run stays the one run, retried by nobody: a routine run that acted is
            # never replayed, and one that changed nothing finished in its second start.
            routine = store.list_schedules("owner", bot["id"])[0]
            assert routine["state"] == "ended" and routine["retry"] is None
            assert len(store.schedule_runs("owner", bot["id"], schedule["id"])["runs"]) == 1
        files = store.list_files("owner", bot["id"])
        assert len(files) == int(step == "save")
        if files:
            assert store.file("owner", bot["id"], files[0]["id"])["content"] == "Durable result"
    finally:
        runtime.stop()
