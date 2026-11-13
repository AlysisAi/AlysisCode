"""Real runtime threads and persistence; LLM responses are deterministic test fixtures."""

import threading
import time
from uuid import uuid4

import pytest

from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, Store
from alysis_code.llm.types import LLMResponse, ToolCall


def wait(read, accept):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = read()
        if accept(value):
            return value
        time.sleep(0.02)
    raise AssertionError("Runtime did not reach the required state")


def setup(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(
        "owner", BotInput(name="Mira", purpose="Analyze files", memory_enabled=False)
    )
    chat = store.create_chat("owner", bot["id"], "Chat")

    def send(message):
        return store.enqueue(
            "owner", chat["id"], TaskInput(message=message, request_id=uuid4().hex)
        )

    return store, bot, send


def test_long_work_keeps_context_replies_and_applies_exact_correction(tmp_path):
    store, bot, send = setup(tmp_path)
    original = send("Compare the supplied notes. Keep marker 雨-Δ-47. Save report.txt.")
    entered, release = threading.Event(), threading.Event()
    counts = {}
    contexts = []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def __init__(self, task_id):
            self.task_id = task_id

        def chat(self, **kw):
            n = counts[self.task_id] = counts.get(self.task_id, 0) + 1
            if self.task_id == original["id"]:
                contexts.append(kw["messages"].copy())
                if n == 1:
                    return LLMResponse(
                        "",
                        [
                            ToolCall(
                                "bg", "continue_in_background", {"summary": "Comparing the notes"}
                            )
                        ],
                        {},
                    )
                assert "雨-Δ-47" in str(kw["messages"])
                assert "continue_in_background" not in [t["function"]["name"] for t in kw["tools"]]
                if n == 2:
                    entered.set()
                    assert release.wait(6)
                    return LLMResponse(
                        "",
                        [
                            ToolCall(
                                "old",
                                "save_file",
                                {"name": "report.txt", "content": "Outdated answer"},
                            )
                        ],
                        {},
                    )
                if n == 3:
                    assert "Διόρθωση: 日本語 only; keep 雨-Δ-47." in str(kw["messages"])
                    return LLMResponse(
                        "",
                        [
                            ToolCall(
                                "file",
                                "save_file",
                                {"name": "report.txt", "content": "日本語 雨-Δ-47"},
                            )
                        ],
                        {},
                    )
                return LLMResponse("The revised report.txt is saved.", [], {})
            if n == 1:
                assert original["id"] in str(kw["messages"])
                return LLMResponse(
                    "", [ToolCall("fix", "update_background_task", {"task_id": original["id"]})], {}
                )
            return LLMResponse("The correction is queued; the report is still running.", [], {})

    runtime = BotRuntime(store, "owner", Client, limits=RuntimeLimits(max_calls=10))
    runtime.start()
    try:
        assert entered.wait(6)
        followup = send("Διόρθωση: 日本語 only; keep 雨-Δ-47.")
        runtime.wake()
        wait(lambda: store.task("owner", followup["id"]), lambda t: t["status"] == "completed")
        assert store.task("owner", original["id"])["status"] == "waiting"
        assert len(runtime.background.active(bot["id"])) == 1
        release.set()
        result = wait(
            lambda: store.task("owner", original["id"]), lambda t: t["status"] == "completed"
        )
        files = store.list_files("owner", bot["id"])
        assert len(files) == 1
        assert store.file("owner", bot["id"], files[0]["id"])["content"] == "日本語 雨-Δ-47"
        assert result["calls"] == 4  # Original context continued, no extra summarizer.
        calls = store.usage("owner")["calls"]
        time.sleep(0.25)
        assert store.usage("owner")["calls"] == calls
        assert runtime.background.active(bot["id"]) == []
    finally:
        release.set()
        runtime.stop()


def test_background_queue_survives_but_inflight_work_is_not_replayed(tmp_path):
    store, bot, send = setup(tmp_path)
    task = send("Prepare report")
    current = store.claim("owner")
    bg = BackgroundStore(store, "owner")
    messages = [{"role": "user", "content": task["message"]}]
    bg.submit(current, "Preparing report", messages)
    assert Store(store.path).recover("owner") == 0
    assert bg.claim()["id"] == task["id"]
    assert bg.checkpoint(task["id"]) == messages
    assert Store(store.path).recover("owner") == 1
    assert store.task("owner", task["id"])["status"] == "interrupted"
    assert bg.claim() is None
    assert store.claim("owner") is None


def test_cancel_and_pending_corrections_fence_writes_and_completion(tmp_path):
    store, bot, send = setup(tmp_path)
    task = send("Prepare report")
    bg = BackgroundStore(store, "owner")
    bg.submit(
        store.claim("owner"), "Preparing report", [{"role": "user", "content": "Prepare report"}]
    )
    bg.claim()
    correction = send("Wait: use the new instructions")
    source = store.claim("owner")
    bg.correct(source, task["id"])
    bg.correct(source, task["id"])
    with pytest.raises(Conflict):
        store.save_file(
            "owner",
            bot["id"],
            FileInput(name="bad.txt", content="stale"),
            source_task_id=task["id"],
        )
    updates = bg.updates(task["id"])
    assert [u["content"] for u in updates] == [correction["message"]]
    store.cancel("owner", task["id"])
    with pytest.raises(Conflict):
        store.save_file(
            "owner", bot["id"], FileInput(name="bad.txt", content="late"), source_task_id=task["id"]
        )
    store.finish("owner", task["id"], "completed", content="Too late")
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert store.list_files("owner", bot["id"]) == []


def test_background_call_budget_is_shared_with_the_initial_turn(tmp_path):
    store, bot, send = setup(tmp_path)
    original = send("Long analysis")
    calls = []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kw):
            calls.append(kw)
            return LLMResponse(
                "", [ToolCall("bg", "continue_in_background", {"summary": "Analyzing"})], {}
            )

    runtime = BotRuntime(
        store, "owner", lambda _: Client(), limits=RuntimeLimits(calls_per_task=1, max_calls=5)
    )
    runtime.start()
    try:
        wait(lambda: store.task("owner", original["id"]), lambda t: t["status"] == "failed")
        assert len(calls) == 1
    finally:
        runtime.stop()


def test_second_background_request_returns_one_honest_tool_error(tmp_path):
    store, bot, send = setup(tmp_path)
    original = send("First report")
    bg = BackgroundStore(store, "owner")
    bg.submit(
        store.claim("owner"), "First report", [{"role": "user", "content": original["message"]}]
    )
    second = send("Second report")
    calls = []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                return LLMResponse(
                    "", [ToolCall("bg", "continue_in_background", {"summary": "Second report"})], {}
                )
            replies = [m for m in kw["messages"] if m.get("tool_call_id") == "bg"]
            assert len(replies) == 1 and "error" in replies[0]["content"]
            return LLMResponse(
                "The first report is still running. I have not started another.", [], {}
            )

    runtime = BotRuntime(store, "owner", lambda _: Client())
    task = store.claim("owner")
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", second["id"])["status"] == "completed"
    assert len(bg.active(bot["id"])) == 1


def test_completion_cannot_overtake_a_new_correction_or_cross_bot_scope(tmp_path):
    from alysis_code.bots.store import NotFound

    store, bot, send = setup(tmp_path)
    original = send("First report")
    bg = BackgroundStore(store, "owner")
    bg.submit(
        store.claim("owner"), "First report", [{"role": "user", "content": original["message"]}]
    )
    bg.claim()
    send("Use revised requirements")
    bg.correct(store.claim("owner"), original["id"])
    other = store.save_bot("owner", BotInput(name="Other", purpose="Other context"))
    with pytest.raises(NotFound):
        bg.read(other["id"], original["id"])
    store.finish("owner", original["id"], "completed", content="Stale completion")
    task = store.task("owner", original["id"])
    assert task["status"] == "failed" and task["output"] == ""
    assert "correction" in task["error"]
