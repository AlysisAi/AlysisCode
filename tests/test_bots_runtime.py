from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from bots_task_state import task_state

from alysis_code.bots.models import BotInput, FileInput, MemoryInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, NotFound, Store, encode
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bots.sqlite3")


def setup_task(store, owner="alice", message="Organize my notes", **config):
    bot = store.save_bot(owner, BotInput(name="Atlas", purpose="Help organize notes", **config))
    chat = store.create_chat(owner, bot["id"], "New conversation")
    task = store.enqueue(
        owner, chat["id"], TaskInput(message=message, request_id="request_" + chat["id"])
    )
    return bot, chat, task


class ScriptedClient:
    """Provider fixture only; never used by the shipped service or demo."""

    model = "test-model"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        return next(self.responses)


def response(content="Done", calls=()):
    return LLMResponse(content, list(calls), {}, usage=LLMUsage(10, 5, 15))


def run_claimed(store, client, limits=None):
    task = store.claim("alice")
    runtime = BotRuntime(store, "alice", lambda _: client, limits=limits)
    runtime.run(task, EventCancellationToken(threading.Event()))
    return store.task("alice", task["id"])


def test_data_and_task_history_survive_new_store(store):
    bot, chat, task = setup_task(store)
    store.save_memory("alice", bot["id"], MemoryInput(title="Language", content="Ελληνικά 日本語"))
    store.save_file("alice", bot["id"], FileInput(name="Σημειώσεις.md", content="こんにちは"))
    run_claimed(store, ScriptedClient([response("Έτοιμο")]))
    reopened = Store(store.path)
    assert reopened.conversation("alice", chat["id"])["messages"][-1]["content"] == "Έτοιμο"
    assert reopened.list_memories("alice", bot["id"])[0]["content"] == "Ελληνικά 日本語"
    assert reopened.list_files("alice", bot["id"])[0]["name"] == "Σημειώσεις.md"
    assert reopened.task("alice", task["id"])["status"] == "completed"


@pytest.mark.parametrize("content", ["", " \t\r\n", "\t  Δάφνη 日本語\u2028\u0085\r\n\n"])
def test_saved_file_content_is_verbatim_for_user_and_model(store, content):
    bot, _, _ = setup_task(store)
    uploaded = store.save_file(
        "alice", bot["id"], FileInput(name="  uploaded.txt  ", content=content)
    )
    assert uploaded["name"] == "uploaded.txt"
    client = ScriptedClient(
        [
            response(
                calls=[ToolCall("save", "save_file", {"name": "generated.txt", "content": content})]
            ),
            response("Saved"),
        ]
    )
    assert run_claimed(store, client)["status"] == "completed"
    reopened = Store(store.path)
    files = reopened.list_files("alice", bot["id"])
    assert len(files) == 2
    assert all(
        reopened.file("alice", bot["id"], file["id"])["content"] == content for file in files
    )


def test_file_limit_applies_to_actual_content_including_whitespace():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FileInput(name="large.txt", content=" " * 100001)


def test_duplicate_submit_is_atomic_under_concurrency(store):
    bot = store.save_bot("alice", BotInput(name="One", purpose="Test"))
    chat = store.create_chat("alice", bot["id"], "New conversation")
    data = TaskInput(message="Only once", request_id="same_request")
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = list(pool.map(lambda _: store.enqueue("alice", chat["id"], data), range(12)))
    assert len({t["id"] for t in tasks}) == 1
    assert len(store.conversation("alice", chat["id"])["messages"]) == 1
    with pytest.raises(Conflict):
        store.enqueue("alice", chat["id"], TaskInput(message="Changed", request_id=data.request_id))


def test_only_one_claim_wins(store):
    setup_task(store)
    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks = list(pool.map(lambda _: store.claim("alice"), range(6)))
    assert len([t for t in tasks if t]) == 1


def test_all_cross_owner_references_are_denied(store):
    bot, chat, task = setup_task(store)
    memory = store.save_memory(
        "alice", bot["id"], MemoryInput(title="Private", content="Private fact")
    )
    file = store.save_file(
        "alice", bot["id"], FileInput(name="private.txt", content="Private text")
    )
    assert store.list_bots("mallory") == []
    attempts = [
        lambda: store.bot("mallory", bot["id"]),
        lambda: store.conversation("mallory", chat["id"]),
        lambda: store.task("mallory", task["id"]),
        lambda: store.cancel("mallory", task["id"]),
        lambda: store.list_memories("mallory", bot["id"]),
        lambda: store.list_files("mallory", bot["id"]),
        lambda: store.file("mallory", bot["id"], file["id"]),
        lambda: store.decide_memory("mallory", bot["id"], memory["id"], "active"),
        lambda: store.delete_item("mallory", bot["id"], file["id"], kind="files"),
        lambda: store.save_bot("mallory", BotInput(name="Take over", purpose="No"), bot["id"]),
    ]
    for attempt in attempts:
        with pytest.raises(NotFound):
            attempt()


def test_bot_cannot_read_another_bots_file(store):
    first, _, _ = setup_task(store)
    other = store.save_bot("alice", BotInput(name="Other", purpose="Separate"))
    file = store.save_file(
        "alice", other["id"], FileInput(name="private.txt", content="not-visible")
    )
    client = ScriptedClient(
        [
            response(calls=[ToolCall("read1", "read_file", {"file_id": file["id"]})]),
            response("File was unavailable"),
        ]
    )
    result = run_claimed(store, client)
    assert result["status"] == "completed"
    assert "not-visible" not in json.dumps(client.requests)
    assert (
        "File not found"
        in next(m for m in reversed(client.requests[-1]) if m["role"] == "tool")["content"]
    )
    assert store.list_files("alice", first["id"]) == []


def test_proposed_memory_requires_review_then_appears_in_new_conversation(store):
    bot, _, _ = setup_task(store, message="I prefer concise replies in Greek")
    client = ScriptedClient(
        [
            response(
                calls=[
                    ToolCall(
                        "memory1",
                        "propose_memory",
                        {"title": "Reply style", "content": "Concise replies in Greek"},
                    )
                ]
            ),
            response("Review my memory suggestion."),
        ]
    )
    result = run_claimed(store, client)
    assert result["status"] == "completed"
    memory = store.list_memories("alice", bot["id"])[0]
    assert memory["status"] == "pending"
    chat2 = store.create_chat("alice", bot["id"], "New conversation")
    task2 = store.enqueue(
        "alice",
        chat2["id"],
        TaskInput(message="What do you remember?", request_id="second_request"),
    )
    runtime = BotRuntime(store, "alice", lambda _: client)
    assert "Concise replies in Greek" not in task_state(runtime._context(task2))
    store.decide_memory("alice", bot["id"], memory["id"], "active")
    assert "Concise replies in Greek" in task_state(runtime._context(task2))
    store.delete_item("alice", bot["id"], memory["id"], kind="memories")
    assert "Concise replies in Greek" not in task_state(runtime._context(task2))


def test_disabled_capability_is_enforced_even_if_model_calls_it(store):
    bot, _, _ = setup_task(store, files_enabled=False, memory_enabled=False)
    store.save_memory("alice", bot["id"], MemoryInput(title="Hidden", content="UNUSED_MEMORY"))
    client = ScriptedClient(
        [
            response(calls=[ToolCall("save1", "save_file", {"name": "no.md", "content": "No"})]),
            response("File access is off"),
        ]
    )
    result = run_claimed(store, client)
    assert result["status"] == "completed"
    assert store.list_files("alice", bot["id"]) == []
    assert "UNUSED_MEMORY" not in json.dumps(client.requests)
    assert any(e["kind"] == "tool_failed" for e in result["events"])


def test_conversation_keeps_older_turns_until_context_capacity_is_needed(store):
    _, chat, task = setup_task(store, message="Earlier decision: no email integration")
    for index in range(9):
        store.claim("alice")
        store.finish("alice", task["id"], "completed", content=f"Reply {index}")
        task = store.enqueue(
            "alice",
            chat["id"],
            TaskInput(message=f"Follow-up {index}", request_id=f"history_turn_{index}"),
        )
    runtime = BotRuntime(store, "alice", lambda _: ScriptedClient([]))
    full = runtime._context(task)
    # The instructions, nine settled turns, the task's state and its request.
    assert len(full) == 21
    assert full[1]["content"] == "Earlier decision: no email integration"
    assert full[-2]["content"] == task_state(full)
    assert not any(e["kind"] == "context_window" for e in store.task("alice", task["id"])["events"])
    # Force a real context boundary, retaining the newest complete exchanges.
    runtime.limits = RuntimeLimits(context_characters=len(encode([full[0], *full[5:]])))
    bounded = runtime._context(task)
    assert bounded == [full[0], *full[5:]]
    assert [m["role"] for m in bounded[1:]] == ["user", "assistant"] * 7 + ["system", "user"]
    assert any(e["kind"] == "context_window" for e in store.task("alice", task["id"])["events"])
    assert len(store.conversation("alice", chat["id"])["messages"]) == 19


def test_runtime_creates_actual_file_and_preserves_original(store):
    bot, _, _ = setup_task(store)
    client = ScriptedClient(
        [
            response(
                calls=[
                    ToolCall(
                        "save1",
                        "save_file",
                        {"name": "brief.md", "content": "# Original\nΚαλημέρα"},
                    )
                ]
            ),
            response(
                calls=[ToolCall("save2", "save_file", {"name": "brief.md", "content": "Overwrite"})]
            ),
            response("Original saved, duplicate rejected"),
        ]
    )
    result = run_claimed(store, client)
    files = store.list_files("alice", bot["id"])
    assert len(files) == 1
    assert store.file("alice", bot["id"], files[0]["id"])["content"] == "# Original\nΚαλημέρα"
    assert result["calls"] == 3
    assert store.usage("alice")["reported_cost_usd"] is None


def test_budget_survives_restart_and_prevents_next_provider_call(store):
    _, chat, _ = setup_task(store)
    client = ScriptedClient(
        [response(calls=[ToolCall("list1", "list_files", {})]), response("Should not run")]
    )
    result = run_claimed(store, client, RuntimeLimits(max_calls=1))
    assert result["status"] == "failed"
    assert len(client.requests) == 1
    assert Store(store.path).usage("alice")["calls"] == 1
    assert len(store.conversation("alice", chat["id"])["messages"]) == 1


def test_restart_marks_running_interrupted_and_does_not_replay(store):
    _, _, first = setup_task(store)
    store.claim("alice")
    call_id = store.reserve_call("alice", first["id"], "model", 100)
    assert call_id
    _, _, queued = setup_task(store)
    assert Store(store.path).recover("alice") == 1
    assert store.task("alice", first["id"])["status"] == "interrupted"
    assert store.task("alice", first["id"])["usage"][0]["status"] == "unknown"
    assert store.claim("alice")["id"] == queued["id"]


def test_cancel_wins_race_with_completion(store):
    _, chat, task = setup_task(store)
    store.claim("alice")
    store.cancel("alice", task["id"])
    store.finish("alice", task["id"], "completed", content="Too late")
    assert store.task("alice", task["id"])["status"] == "cancelled"
    assert len(store.conversation("alice", chat["id"])["messages"]) == 1


def test_worker_stops_inflight_provider_and_keeps_unknown_cost(store):
    _, _, task = setup_task(store)
    entered = threading.Event()

    class BlockingClient:
        model = "blocking-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            entered.set()
            token = kwargs["cancellation_token"]
            token.wait(5)
            token.throw_if_cancelled()
            raise AssertionError("Cancellation was not delivered")

    runtime = BotRuntime(store, "alice", lambda _: BlockingClient())
    runtime.start()
    try:
        assert entered.wait(2)
        runtime.cancel(task["id"])
        until = time.monotonic() + 3
        while (
            store.task("alice", task["id"])["status"] == "cancelling" and time.monotonic() < until
        ):
            time.sleep(0.02)
        assert store.task("alice", task["id"])["status"] == "cancelled"
        assert store.usage("alice")["unconfirmed_calls"] == 1
        assert store.usage("alice")["reported_cost_usd"] is None
    finally:
        runtime.stop()


def test_second_worker_is_refused_and_shutdown_does_not_lose_queue(store):
    first = BotRuntime(store, "alice", lambda _: ScriptedClient([]))
    second = BotRuntime(Store(store.path), "alice", lambda _: ScriptedClient([]))
    first.start()
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            second.start()
    finally:
        first.stop()
    second.start()
    second.stop()


def test_provider_error_cannot_leak_credentials_or_become_success(store):
    _, _, task = setup_task(store)

    class BrokenClient:
        model = "broken"
        supports_tool_calling = True

        def chat(self, **kwargs):
            raise RuntimeError("Authorization: Bearer PRIVATE_TEST_SECRET")

    result = run_claimed(store, BrokenClient())
    assert result["status"] == "failed"
    assert "PRIVATE_TEST_SECRET" not in json.dumps(result)
    assert "PRIVATE_TEST_SECRET" not in store.path.read_bytes().decode("utf-8", errors="ignore")
    diagnostic = next(e["data"] for e in result["events"] if e["kind"] == "runtime_failure")
    assert diagnostic["exception_type"] == "RuntimeError"
    assert diagnostic["location"]["module"].startswith("alysis_code.")
    assert set(diagnostic["location"]) == {"module", "function", "line"}


@pytest.mark.parametrize(
    "name", ["../outside.txt", "dir/file", "dir\\file", "bad\0name", ".", ".."]
)
def test_file_labels_never_accept_paths(name):
    with pytest.raises(ValueError):
        FileInput(name=name, content="text")
