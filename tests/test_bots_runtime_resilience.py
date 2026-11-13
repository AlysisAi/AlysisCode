from __future__ import annotations

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import alysis_code.bots.runtime as runtime_module
from alysis_code.bots.files import INLINE_FILE_BYTES
from alysis_code.bots.models import BotInput, FileInput, MemoryInput, TaskInput
from alysis_code.bots.runtime import (
    BINARY_FILE_MESSAGE,
    DATABASE_INTERRUPTED,
    EMPTY_REPLY_ERROR,
    EMPTY_REPLY_NUDGE,
    PROVIDER_INTERRUPTED,
    UNREADABLE_FILE_MESSAGE,
    BotRuntime,
    RuntimeLimits,
    transient_provider_error,
)
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.types import LLMError, LLMOutputLimitError, LLMResponse, LLMUsage, ToolCall


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)


class ScriptedClient:
    """Each step is a response to return or an exception to raise."""

    model = "test-model"
    supports_tool_calling = True

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def response(content="Done", calls=()):
    return LLMResponse(content, list(calls), {}, usage=LLMUsage(10, 5, 15))


def setup_chat(store, name="Atlas", owner="alice"):
    bot = store.save_bot(owner, BotInput(name=name, purpose="Help organize notes"))
    return bot, store.create_chat(owner, bot["id"], "New conversation")


def run_task(tmp_path, steps, *, token=None, limits=None):
    store = Store(tmp_path / "bots.sqlite3")
    bot, chat = setup_chat(store)
    store.enqueue("alice", chat["id"], TaskInput(message="Hi there", request_id="request_1"))
    task = store.claim("alice")
    client = ScriptedClient(steps)
    BotRuntime(store, "alice", lambda _: client, limits=limits).run(
        task, token or EventCancellationToken(threading.Event())
    )
    return store, bot, store.task("alice", task["id"]), client


def kinds(task):
    return [e["kind"] for e in task["events"]]


def http_error(status, retry_after=0):
    error = LLMError(f"LLM error {status}: upstream said no")
    error.provider_status_code = status
    if retry_after:
        error.provider_retry_after = retry_after
    return error


# ---------------------------------------------------------------- empty replies (EN-05)


@pytest.mark.parametrize(
    "content",
    ["", "  \n", "<follow_ups>\n- Next thing\n</follow_ups>", None],
    ids=["empty", "blank", "only_follow_ups", "none"],
)
def test_one_empty_reply_is_asked_again_and_the_answer_is_published(tmp_path, content):
    store, _, task, client = run_task(tmp_path, [response(content), response("Real answer")])
    assert task["status"] == "completed"
    assert task["output"] == "Real answer"
    assert len(client.requests) == 2
    assert {"role": "system", "content": EMPTY_REPLY_NUDGE} in client.requests[1]
    assert {"role": "system", "content": EMPTY_REPLY_NUDGE} not in client.requests[0]
    assert "empty_model_response" in kinds(task)
    assert "runtime_failure" not in kinds(task)
    assert store.usage("alice")["calls"] == 2


def test_two_empty_replies_fail_with_a_plain_reason(tmp_path):
    store, _, task, client = run_task(tmp_path, [response(""), response(None), response("Late")])
    assert task["status"] == "failed"
    assert task["error"] == EMPTY_REPLY_ERROR
    assert "check the model connection" not in task["error"].lower()
    assert "runtime_failure" not in kinds(task)
    assert len(client.requests) == 2
    assert store.usage("alice")["calls"] == 2


# ---------------------------------------------------------------- provider retry (EN-03)


@pytest.mark.parametrize(
    "error",
    [
        lambda: http_error(429),
        lambda: http_error(503),
        lambda: LLMError("HTTP 429 rate limited"),
        lambda: TimeoutError("read timeout"),
        lambda: ConnectionResetError("reset by peer"),
        lambda: ConnectionError("reset"),
        lambda: LLMError("LLM request failed: Server disconnected without sending a response."),
    ],
    ids=["429", "503", "429_text", "timeout", "reset", "connection", "disconnected"],
)
def test_one_transient_provider_error_is_retried_and_accounted(tmp_path, error):
    store, _, task, client = run_task(tmp_path, [error(), response("Hello!")])
    assert task["status"] == "completed"
    assert task["output"] == "Hello!"
    assert len(client.requests) == 2
    assert client.requests[0] == client.requests[1]
    retry = next(e["data"] for e in task["events"] if e["kind"] == "provider_retry")
    assert set(retry) == {"exception_type", "wait_seconds"}
    # Both attempts went through reserve_call; the failed one is never shown as free.
    usage = store.usage("alice")
    assert usage["calls"] == 2
    assert usage["unconfirmed_calls"] == 1
    assert [call["status"] for call in task["usage"]] == ["unknown", "completed"]


def test_a_throttled_call_is_retried_a_few_times_then_fails_the_turn(tmp_path):
    # Several owls share one model account: a few temporary errors in a row are survived...
    store, _, task, client = run_task(
        tmp_path, [http_error(429), http_error(503), TimeoutError("again"), response("Made it")]
    )
    assert task["status"] == "completed" and task["output"] == "Made it"
    waits = [e["data"]["wait_seconds"] for e in task["events"] if e["kind"] == "provider_retry"]
    assert len(waits) == 3 and waits == sorted(waits)  # the pause grows each time
    # ...but a provider that keeps failing ends the turn instead of retrying forever.
    store, _, task, client = run_task(
        tmp_path / "again",
        [http_error(503), http_error(503), http_error(503), http_error(503), response("Never")],
    )
    assert task["status"] == "failed"
    assert len(client.requests) == 4
    assert kinds(task).count("provider_retry") == 3
    assert store.usage("alice")["calls"] == 4
    # The person is told it was the model service, not their connection, and to try later.
    assert task["error"] == PROVIDER_INTERRUPTED


@pytest.mark.parametrize(
    ("error", "busy"),
    [
        (lambda: http_error(429, retry_after=120), True),
        (lambda: http_error(400), False),
        (lambda: http_error(401), False),
        (lambda: TimeoutError("a tool timed out"), False),
    ],
    ids=["429_long_wait", "400", "401", "not_a_model_error"],
)
def test_only_a_busy_model_service_is_reported_as_temporary(tmp_path, error, busy):
    _, _, task, _ = run_task(tmp_path, [error()] * 4 + [response("Never")])
    assert task["status"] == "failed"
    assert (task["error"] == PROVIDER_INTERRUPTED) is busy


def test_each_model_call_gets_its_own_retry_and_tools_never_run_twice(tmp_path):
    save = ToolCall("save", "save_file", {"name": "notes.md", "content": "Notes"})
    store, bot, task, client = run_task(
        tmp_path,
        [
            http_error(502),
            response("Saving your notes now.", [save]),
            http_error(503),
            response("Saved notes.md."),
        ],
    )
    assert task["status"] == "completed"
    assert len(client.requests) == 4
    assert [f["name"] for f in store.list_files("alice", bot["id"])] == ["notes.md"]
    assert kinds(task).count("tool_started") == 1
    assert kinds(task).count("provider_retry") == 2


@pytest.mark.parametrize(
    "error",
    [
        lambda: RuntimeError("Authorization: Bearer PRIVATE_TEST_SECRET"),
        lambda: http_error(401),
        lambda: http_error(400),
        lambda: http_error(429, retry_after=120),
        lambda: LLMOutputLimitError(response("Partial reply")),
    ],
    ids=["unknown", "401", "400", "429_long_wait", "output_limit"],
)
def test_rejections_and_partial_output_are_not_retried(tmp_path, error):
    store, _, task, client = run_task(tmp_path, [error(), response("Should not run")])
    assert task["status"] == "failed"
    assert len(client.requests) == 1
    assert "provider_retry" not in kinds(task)
    assert "PRIVATE_TEST_SECRET" not in json.dumps(task)


def test_exhausted_quota_is_not_retried(tmp_path):
    error = http_error(429)
    error.provider_error_code = "insufficient_quota"
    assert not transient_provider_error(error)
    _, _, task, client = run_task(tmp_path, [error, response("Should not run")])
    assert task["status"] == "failed"
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    "status,detail,headers",
    [
        (429, {"code": "insufficient_quota"}, {}),
        (429, {"type": "billing_hard_limit_reached"}, {}),
        (429, {"code": "rate_limit_exceeded"}, {"Retry-After": "120"}),
        (503, {"code": "server_error"}, {"Retry-After": "120"}),
    ],
)
def test_adapter_metadata_reaches_bots_retry_owner(tmp_path, status, detail, headers):
    error = OpenAICompatClient._error_from_response(
        httpx.Response(status, json={"error": detail}, headers=headers)
    )
    _, _, task, client = run_task(tmp_path, [error, response("Should not run")])
    assert task["status"] == "failed"
    assert len(client.requests) == 1
    assert "provider_retry" not in kinds(task)


def test_cancellation_is_never_retried(tmp_path):
    assert not transient_provider_error(CooperativeCancellationError("cancelled_by_user"))
    cancelled = threading.Event()

    class CancelledDuringCall(ScriptedClient):
        def chat(self, **kwargs):
            cancelled.set()
            return super().chat(**kwargs)

    store = Store(tmp_path / "bots.sqlite3")
    _, chat = setup_chat(store)
    store.enqueue("alice", chat["id"], TaskInput(message="Hi", request_id="request_1"))
    task = store.claim("alice")
    client = CancelledDuringCall([TimeoutError("closed by stop"), response("Never")])
    BotRuntime(store, "alice", lambda _: client).run(task, EventCancellationToken(cancelled))
    assert len(client.requests) == 1
    assert "provider_retry" not in kinds(store.task("alice", task["id"]))


def test_stop_during_the_retry_pause_cancels_without_a_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 10.0)
    cancel = threading.Event()

    class FailsThenCancels(ScriptedClient):
        def chat(self, **kwargs):
            threading.Timer(0.1, cancel.set).start()
            return super().chat(**kwargs)

    store = Store(tmp_path / "bots.sqlite3")
    _, chat = setup_chat(store)
    store.enqueue("alice", chat["id"], TaskInput(message="Hi", request_id="request_1"))
    task = store.claim("alice")
    client = FailsThenCancels([http_error(503), response("Never")])
    started = time.monotonic()
    BotRuntime(store, "alice", lambda _: client).run(task, EventCancellationToken(cancel))
    assert time.monotonic() - started < 5
    assert len(client.requests) == 1
    assert store.task("alice", task["id"])["status"] == "cancelled"


def test_one_provider_hiccup_does_not_pause_a_routine(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, _ = setup_chat(store)
    schedule = store.save_schedule(
        "alice",
        bot["id"],
        ScheduleInput.model_validate(
            {
                "name": "Digest",
                "prompt": "Summarize my notes",
                "timing": {"kind": "interval", "every_minutes": 5},
            }
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("alice", max_calls=5, now=1400) == 1
    client = ScriptedClient([http_error(503), response("Digest ready.")])
    runtime = BotRuntime(store, "alice", lambda _: client, limits=RuntimeLimits(max_calls=5))
    runtime.run(store.claim("alice"), EventCancellationToken(threading.Event()))
    assert store.list_schedules("alice", bot["id"])[0]["state"] == "active"
    run = store.schedule_runs("alice", bot["id"], schedule["id"])["runs"][0]
    assert run["current_status"] == "completed"


# ---------------------------------------------------------------- foreground workers (EN-04)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class BlockingByBot:
    """Blocks the named bot's calls until released; answers every other bot at once."""

    model = "test-model"
    supports_tool_calling = True

    def __init__(self, slow_name):
        self.slow_name = slow_name
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, **kwargs):
        if f'"name":"{self.slow_name}"' in kwargs["messages"][0]["content"]:
            self.entered.set()
            token = kwargs["cancellation_token"]
            while not self.release.is_set():
                token.throw_if_cancelled()
                time.sleep(0.02)
            return response("Slow answer")
        return response("Fast answer")


def two_bots(store):
    _, slow_chat = setup_chat(store, "Slow")
    _, fast_chat = setup_chat(store, "Fast")
    return slow_chat, fast_chat


def enqueue(store, chat, request_id, message="Hi"):
    return store.enqueue("alice", chat["id"], TaskInput(message=message, request_id=request_id))


def test_another_bot_answers_while_one_bot_is_busy(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    slow_chat, fast_chat = two_bots(store)
    client = BlockingByBot("Slow")
    runtime = BotRuntime(store, "alice", lambda _: client)
    runtime.start()
    try:
        slow = enqueue(store, slow_chat, "request_slow", "Long job")
        runtime.wake()
        assert client.entered.wait(5)
        fast = enqueue(store, fast_chat, "request_fast")
        runtime.wake()
        assert wait_for(lambda: store.task("alice", fast["id"])["status"] == "completed")
        assert store.task("alice", fast["id"])["output"] == "Fast answer"
        assert store.task("alice", slow["id"])["status"] == "running"
        client.release.set()
        assert wait_for(lambda: store.task("alice", slow["id"])["status"] == "completed")
    finally:
        client.release.set()
        runtime.stop()


def test_one_bot_still_answers_one_message_at_a_time(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    slow_chat, _ = two_bots(store)
    client = BlockingByBot("Slow")
    runtime = BotRuntime(store, "alice", lambda _: client)
    runtime.start()
    try:
        first = enqueue(store, slow_chat, "request_first", "Long job")
        runtime.wake()
        assert client.entered.wait(5)
        second = enqueue(store, slow_chat, "request_second", "And then this")
        runtime.wake()
        time.sleep(0.5)
        assert store.task("alice", second["id"])["status"] == "queued"
        client.release.set()
        assert wait_for(lambda: store.task("alice", second["id"])["status"] == "completed")
        assert store.task("alice", first["id"])["status"] == "completed"
    finally:
        client.release.set()
        runtime.stop()


def test_cancel_stops_only_its_own_task_and_stop_interrupts_every_task(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    first_chat, second_chat = two_bots(store)
    _, third_chat = setup_chat(store, "Third")
    entered = {"Slow": threading.Event(), "Fast": threading.Event(), "Third": threading.Event()}

    class BlockEveryBot:
        model = "test-model"
        supports_tool_calling = True

        def chat(self, **kwargs):
            system = kwargs["messages"][0]["content"]
            name = next(n for n in entered if f'"name":"{n}"' in system)
            entered[name].set()
            kwargs["cancellation_token"].wait(10)
            kwargs["cancellation_token"].throw_if_cancelled()
            raise AssertionError("Cancellation was not delivered")

    runtime = BotRuntime(store, "alice", lambda _: BlockEveryBot())
    runtime.start()
    try:
        tasks = [
            enqueue(store, chat, f"request_{n}")
            for n, chat in enumerate((first_chat, second_chat, third_chat))
        ]
        runtime.wake()
        assert all(event.wait(5) for event in entered.values())
        runtime.cancel(tasks[0]["id"])
        assert wait_for(lambda: store.task("alice", tasks[0]["id"])["status"] == "cancelled")
        assert [store.task("alice", t["id"])["status"] for t in tasks[1:]] == ["running"] * 2
    finally:
        runtime.stop()
    assert [store.task("alice", t["id"])["status"] for t in tasks[1:]] == ["interrupted"] * 2
    assert runtime._thread is None and runtime._workers == []


def test_a_single_worker_keeps_the_previous_one_at_a_time_behaviour(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    slow_chat, fast_chat = two_bots(store)
    client = BlockingByBot("Slow")
    runtime = BotRuntime(store, "alice", lambda _: client, foreground_workers=1)
    runtime.start()
    try:
        assert runtime.is_running
        assert sum(worker.name.startswith("alysis-bots-worker") for worker in runtime._workers) == 1
        enqueue(store, slow_chat, "request_slow", "Long job")
        runtime.wake()
        assert client.entered.wait(5)
        fast = enqueue(store, fast_chat, "request_fast")
        runtime.wake()
        time.sleep(0.5)
        assert store.task("alice", fast["id"])["status"] == "queued"
        client.release.set()
        assert wait_for(lambda: store.task("alice", fast["id"])["status"] == "completed")
    finally:
        client.release.set()
        runtime.stop()


# ---------------------------------------------------------------- database hiccups (EN-10)


def test_one_locked_database_tick_pauses_the_scheduler_instead_of_every_lane(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    _, chat = setup_chat(store)
    original, ticks = store.enqueue_due_schedules, []

    def locked_once(*args, **kwargs):
        ticks.append(1)
        if len(ticks) == 2:
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    store.enqueue_due_schedules = locked_once
    client = ScriptedClient([response("Hello!")])
    runtime = BotRuntime(store, "alice", lambda _: client)
    runtime.start()
    try:
        assert wait_for(lambda: len(ticks) >= 4, timeout=8)
        assert runtime.is_running
        task = enqueue(store, chat, "request_after_lock")
        runtime.wake()
        assert wait_for(lambda: store.task("alice", task["id"])["status"] == "completed")
    finally:
        runtime.stop()


def test_repeated_database_failures_still_stop_every_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "DATABASE_RETRIES", 1)
    store = Store(tmp_path / "bots.sqlite3")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    store.enqueue_due_schedules = broken
    runtime = BotRuntime(store, "alice", lambda _: None)
    runtime.start()
    try:
        assert wait_for(lambda: not runtime.is_running, timeout=8)
    finally:
        runtime.stop()


def test_any_other_lane_error_still_stops_every_lane_at_once(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")

    def broken(*args, **kwargs):
        raise ValueError("unexpected")

    store.claim = broken
    runtime = BotRuntime(store, "alice", lambda _: None)
    runtime.start()
    try:
        assert wait_for(lambda: not runtime.is_running, timeout=2)
    finally:
        runtime.stop()


def test_a_task_that_lost_the_database_is_interrupted_and_the_lane_resumes(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    _, chat = setup_chat(store)
    original, refused = store.finish, []

    def locked_twice(owner, task_id, status, **kwargs):
        if len(refused) < 2:
            refused.append(status)
            raise sqlite3.OperationalError("database is locked")
        return original(owner, task_id, status, **kwargs)

    store.finish = locked_twice
    client = ScriptedClient([response("First"), response("Second")])
    runtime = BotRuntime(store, "alice", lambda _: client, foreground_workers=1)
    runtime.start()
    try:
        first = enqueue(store, chat, "request_first")
        runtime.wake()
        assert wait_for(lambda: store.task("alice", first["id"])["status"] == "interrupted", 10)
        assert store.task("alice", first["id"])["error"] == DATABASE_INTERRUPTED
        assert refused == ["completed", "failed"]
        assert runtime.is_running
        second = enqueue(store, chat, "request_second")
        runtime.wake()
        assert wait_for(lambda: store.task("alice", second["id"])["status"] == "completed", 10)
    finally:
        runtime.stop()
    assert store.task("alice", first["id"])["output"] == ""


def test_a_locked_database_is_not_read_as_cancellation(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    _, chat = setup_chat(store)
    task = enqueue(store, chat, "request_token")
    runtime = BotRuntime(store, "alice", lambda _: None)
    token = runtime.task_token(task["id"])

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    store.task = locked
    assert token.is_cancelled is False
    assert not runtime._stop.is_set()


# ---------------------------------------------------------------- memory index (EN-11)


def memory_heavy_task(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, chat = setup_chat(store)
    for index in range(40):
        store.save_memory(
            "alice",
            bot["id"],
            MemoryInput(title=f"Fact {index}", content=f"Detail about topic {index}. " * 12),
        )
    enqueue(store, chat, "request_memory_index")
    return store, store.claim("alice")


class IndexFails(ScriptedClient):
    def __init__(self, steps, error):
        super().__init__(steps)
        self.error = error

    def chat(self, **kwargs):
        if kwargs["messages"][0]["content"].startswith("Classify saved bot memory records"):
            self.requests.append(None)
            raise self.error()
        return super().chat(**kwargs)


def test_a_memory_index_provider_error_does_not_fail_the_reply(tmp_path):
    store, task = memory_heavy_task(tmp_path)
    client = IndexFails([response("Here is your answer.")], lambda: http_error(502))
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    result = store.task("alice", task["id"])
    assert result["status"] == "completed"
    assert result["output"] == "Here is your answer."
    incomplete = [e["data"] for e in result["events"] if e["kind"] == "memory_index_incomplete"]
    assert incomplete == [{"reason": "provider_error"}]
    assert [call["status"] for call in result["usage"]] == ["unknown", "completed"]


def test_cancellation_during_the_memory_index_still_cancels(tmp_path):
    store, task = memory_heavy_task(tmp_path)
    cancelled = threading.Event()

    def cancel_then_fail():
        cancelled.set()
        return TimeoutError("closed by stop")

    client = IndexFails([response("Never")], cancel_then_fail)
    BotRuntime(store, "alice", lambda _: client).run(task, EventCancellationToken(cancelled))
    assert store.task("alice", task["id"])["status"] == "cancelled"
    assert client.requests == [None]


# ---------------------------------------------------------------- tool batch size (EN-12)


def thirteen_reads(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot, chat = setup_chat(store)
    files = [
        store.save_file("alice", bot["id"], FileInput(name=f"f{i}.txt", content=str(i)))
        for i in range(13)
    ]
    enqueue(store, chat, "request_thirteen", "Summarise all my files")
    return store, store.claim("alice"), files


def reads(files, prefix):
    return [
        ToolCall(f"{prefix}{i}", "read_file", {"file_id": f["id"]}) for i, f in enumerate(files)
    ]


def test_an_oversized_tool_batch_is_returned_unrun_for_the_model_to_split(tmp_path):
    store, task, files = thirteen_reads(tmp_path)
    client = ScriptedClient(
        [
            response("Reading all of them.", reads(files, "a")),
            response("", reads(files[:12], "b")),
            response("", reads(files[12:], "c")),
            response("Summary of 13 files."),
        ]
    )
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    result = store.task("alice", task["id"])
    assert result["status"] == "completed"
    rejected = [m for m in client.requests[1] if m["role"] == "tool"]
    assert len(rejected) == 13
    assert all(json.loads(m["content"])["executed"] is False for m in rejected)
    assert kinds(result).count("tool_batch_rejected") == 1
    assert kinds(result).count("tool_started") == 13


def test_repeated_oversized_batches_end_the_task(tmp_path):
    store, task, files = thirteen_reads(tmp_path)
    client = ScriptedClient([response("", reads(files, f"{n}_")) for n in range(4)])
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    result = store.task("alice", task["id"])
    assert result["status"] == "failed"
    assert result["error"] == "The model requested too many actions in one response."
    assert len(client.requests) == 3
    assert "tool_started" not in kinds(result)


# ---------------------------------------------------------------- uploaded text (EN-13)


def uploaded(tmp_path, name, data):
    store = Store(tmp_path / "bots.sqlite3")
    bot, chat = setup_chat(store)
    file = store.save_bytes("alice", bot["id"], name, data)
    enqueue(store, chat, "request_read_upload", "Read my upload")
    return store, store.claim("alice"), file


def test_uploaded_text_stored_as_bytes_is_read_in_pages(tmp_path):
    data = ("name,value\n" + "row,1\n" * 30000).encode()
    store, task, file = uploaded(tmp_path, "big.csv", data)
    assert file["binary"] is True
    runtime = BotRuntime(store, "alice", lambda _: None)
    first = runtime._tool(task, "read_file", {"file_id": file["id"]}, ["read_file"])
    assert first == {
        "name": "big.csv",
        "content": data.decode()[:12000],
        "next_offset": 12000,
        "total_characters": len(data),
    }
    last = runtime._tool(
        task, "read_file", {"file_id": file["id"], "offset": 170000, "length": 20000}, ["read_file"]
    )
    assert last["content"] == data.decode()[170000:]
    assert last["next_offset"] is None


@pytest.mark.parametrize(
    "name,data",
    [
        ("photo.png", b"\x89PNG\r\n\x1a\n" + bytes(64)),
        ("report.pdf", b"%PDF-1.7 plain looking text"),
        ("notes.txt", b"caf\xe9 is not UTF-8"),
        ("dump.txt", b"text\x00with a NUL"),
        ("huge.txt", b"a" * (INLINE_FILE_BYTES + 1)),
    ],
    ids=["image", "pdf", "latin1", "nul", "over_2mib"],
)
def test_other_uploads_are_explained_without_a_missing_computer(tmp_path, name, data):
    store, task, file = uploaded(tmp_path, name, data)
    runtime = BotRuntime(store, "alice", lambda _: None)
    result = runtime._tool(task, "read_file", {"file_id": file["id"]}, ["read_file"])
    assert result["message"] == UNREADABLE_FILE_MESSAGE
    assert result["content"] is None
    runtime.desktop_computers = SimpleNamespace()
    result = runtime._tool(task, "read_file", {"file_id": file["id"]}, ["read_file"])
    assert result["message"] == BINARY_FILE_MESSAGE
