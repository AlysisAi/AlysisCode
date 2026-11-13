import json
import threading

import pytest
from test_bots_runtime import setup_task

from alysis_code.bots.models import FileInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, LimitReached, NotFound, Store
from alysis_code.bots.task_results import ReadToolResult, TaskResults
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, ToolCall


def test_long_task_pages_original_results_without_replaying_tools(tmp_path):
    store = Store(tmp_path / "context.db")
    bot, chat, task = setup_task(store, memory_enabled=False)
    text = "data " * 3800 + "Δέλτα / 東京 = 73.25"
    file = store.save_file("alice", bot["id"], FileInput(name="input.txt", content=text))
    requests = []

    class Client:
        model = "long-task-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            messages = kwargs["messages"]
            requests.append(messages)
            assert all(
                not any(k.startswith("_tool") or k.startswith("_stored") for k in m)
                for m in messages
            )
            if len(requests) == 1:
                return LLMResponse(
                    "",
                    [
                        ToolCall(str(i), "read_file", {"file_id": file["id"], "length": 20000})
                        for i in range(3)
                    ],
                    {},
                )
            if len(requests) == 2:
                sources = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                archived = next(s for s in sources if "stored_result_id" in s)
                # Original call/result identities survive compaction.
                assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
                    "0",
                    "1",
                    "2",
                ]
                assert len(json.dumps(messages, ensure_ascii=False)) < 40000
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "recover",
                            "read_tool_result",
                            {"result_id": archived["stored_result_id"], "find": "Δέλτα / 東京"},
                        )
                    ],
                    {},
                )
            recovered = json.loads(messages[-1]["content"])
            assert recovered["content"].startswith("Δέλτα / 東京 = 73.25")
            return LLMResponse("Recovered the exact value: 73.25", [], {})

    runtime = BotRuntime(
        store, "alice", lambda _: Client(), limits=RuntimeLimits(context_characters=40000)
    )
    runtime.run(store.claim("alice"), EventCancellationToken(threading.Event()))
    finished = store.task("alice", task["id"])
    assert finished["status"] == "completed", finished["error"]
    assert len(requests) == 3
    with store.connect() as db:
        archived = dict(db.execute("SELECT * FROM task_tool_results LIMIT 1").fetchone())
    assert json.loads(archived["content"])["content"] == text
    # A restart preserves sources; the API is task scoped even within one bot.
    results = TaskResults(Store(store.path), "alice")
    read = results.read(task["id"], ReadToolResult(result_id=archived["id"], find="Δέλτα"))
    assert "73.25" in read["content"]
    later = store.enqueue(
        "alice", chat["id"], TaskInput(message="Other task", request_id="different-task")
    )
    with pytest.raises(NotFound):
        results.read(later["id"], ReadToolResult(result_id=archived["id"]))
    with pytest.raises(NotFound):
        TaskResults(store, "other-owner").read(task["id"], ReadToolResult(result_id=archived["id"]))


def test_result_archive_bounds_and_cancellation(tmp_path, monkeypatch):
    store = Store(tmp_path / "bounded.db")
    _, _, task = setup_task(store)
    store.claim("alice")
    results = TaskResults(store, "alice")
    monkeypatch.setattr("alysis_code.bots.task_results.MAX_TASK_RESULT_BYTES", 8)
    result_id = results.archive(task["id"], "read_file", "東京")
    with pytest.raises(LimitReached):
        results.archive(task["id"], "read_file", "東京")
    assert results.read(task["id"], ReadToolResult(result_id=result_id))["content"] == "東京"
    store.cancel("alice", task["id"])
    with pytest.raises(Conflict):
        results.archive(task["id"], "read_file", "a")
