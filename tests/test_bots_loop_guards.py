"""The owl's tool loop when a model repeats itself, calls tools it no longer has, or stalls."""

from __future__ import annotations

import json
import threading

import pytest
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.repeat_guard import REPEAT_STOPPED, UNWATCHED, WATCHED, RepeatGuard
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import LimitReached, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

study = execution_study


class ScriptedClient:
    """Each step is a response to return or an error to raise; the last one repeats if the
    loop asks for more."""

    model = "test-model"
    supports_tool_calling = True

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        return step


def call(name, arguments, index=0):
    return ToolCall(f"call_{index}", name, arguments)


def respond(content="", calls=()):
    return LLMResponse(content, list(calls), {}, usage=LLMUsage(10, 5, 15))


def chat_task(tmp_path, message="Hi there"):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Atlas", purpose="Help organize notes"))
    chat = store.create_chat("alice", bot["id"], "New conversation")
    store.enqueue("alice", chat["id"], TaskInput(message=message, request_id="request_1"))
    return store, bot, store.claim("alice")


def routine_task(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Atlas", purpose="Help organize notes"))
    store.create_chat("alice", bot["id"], "New conversation")
    store.save_schedule(
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
    assert store.enqueue_due_schedules("alice", max_calls=None, now=1400) == 1
    return store, bot, store.claim("alice")


def run(store, task, steps, **kwargs):
    client = ScriptedClient(steps)
    BotRuntime(store, "alice", lambda _: client, **kwargs).run(
        task, EventCancellationToken(threading.Event())
    )
    return store.task("alice", task["id"]), client


def tool_results(client, request=-1):
    return [json.loads(m["content"]) for m in client.requests[request] if m.get("role") == "tool"]


def started(task, name):
    return sum(
        1 for e in task["events"] if e["kind"] == "tool_started" and e["data"]["name"] == name
    )


# ---------------------------------------------------------------- the same failing call


@pytest.mark.parametrize("memory_id", ["μνήμη-που-λείπει", "不存在的记忆", "missing-memory"])
def test_a_call_that_keeps_failing_the_same_way_is_refused_then_ends_the_turn(tmp_path, memory_id):
    store, _, task = chat_task(tmp_path)
    stuck = respond(calls=[call("read_memory", {"memory_id": memory_id})])
    task, client = run(store, task, [stuck])
    # It ran until it had failed the same way three times; after that it was refused unrun.
    assert started(task, "read_memory") == WATCHED.failures
    results = tool_results(client)
    assert all("not found" in r["error"].lower() for r in results[: WATCHED.failures])
    refused = results[WATCHED.failures :]
    assert refused and all(r["executed"] is False for r in refused)
    assert all("failed the same way 3 times" in r["error"] for r in refused)
    # ~10 identical attempts end the turn with a plain failure, not the allowance.
    assert task["status"] == "failed"
    assert task["error"] == REPEAT_STOPPED
    assert task["failure"]["kind"] == "model"
    assert len(client.requests) == WATCHED.stop + 1


def test_a_routine_run_nobody_watches_gives_up_sooner(tmp_path):
    store, _, task = routine_task(tmp_path)
    stuck = respond(calls=[call("read_memory", {"memory_id": "記憶なし"})])
    task, client = run(store, task, [stuck], limits=RuntimeLimits(max_calls=50))
    assert started(task, "read_memory") == UNWATCHED.failures < WATCHED.failures
    assert task["status"] == "failed" and task["error"] == REPEAT_STOPPED
    assert len(client.requests) == UNWATCHED.stop + 1 < WATCHED.stop + 1


def test_a_refused_call_runs_again_after_another_step_works(tmp_path):
    store, _, task = chat_task(tmp_path)
    stuck = respond(calls=[call("read_memory", {"memory_id": "λείπει"})])
    other = respond(calls=[call("list_memories", {})])
    steps = [stuck] * 4 + [other, stuck, respond("Η μνήμη δεν βρέθηκε.")]
    task, client = run(store, task, steps)
    assert task["status"] == "completed"
    # Three failures, one refusal, then a step that worked lets it run once more.
    assert started(task, "read_memory") == 4
    assert tool_results(client)[3]["executed"] is False
    assert "executed" not in tool_results(client)[5]


def test_different_arguments_are_different_calls(tmp_path):
    store, _, task = chat_task(tmp_path)
    steps = [respond(calls=[call("read_memory", {"memory_id": f"记忆-{n}"})]) for n in range(6)]
    task, _ = run(store, task, [*steps, respond("没有找到")])
    assert task["status"] == "completed"
    assert started(task, "read_memory") == 6


# ---------------------------------------------------------------- the same successful result


def test_the_same_result_again_gets_a_note_and_back_to_back_repeats_stop(tmp_path):
    store, _, task = chat_task(tmp_path)
    again = respond(calls=[call("list_memories", {"query": "Αθήνα"})])
    task, client = run(store, task, [again])
    results = tool_results(client)
    assert "repeat_notice" not in results[WATCHED.note - 2]
    assert "3 times" in results[WATCHED.note - 1]["repeat_notice"]
    # Successful repeats are never refused, only noted...
    assert started(task, "list_memories") == WATCHED.stop
    # ...and ten in a row with nothing else in between end the turn.
    assert task["status"] == "failed" and task["error"] == REPEAT_STOPPED


def test_checking_between_other_steps_is_noted_but_not_stopped(tmp_path):
    store, _, task = chat_task(tmp_path)
    check = respond(calls=[call("list_memories", {})])
    other = respond(calls=[call("list_schedules", {})])
    steps = [check, other] * (WATCHED.stop + 2) + [respond("Nothing changed yet.")]
    task, client = run(store, task, steps)
    assert task["status"] == "completed"
    assert started(task, "list_memories") == WATCHED.stop + 2
    assert all(
        "repeat_notice" in r for r in tool_results(client)[2 * WATCHED.note :] if "memories" in r
    )


# ---------------------------------------------------------------- tools not offered now


@pytest.mark.parametrize("window", ["large", "small"])
def test_a_tool_not_offered_now_is_named_with_the_tools_that_are(tmp_path, monkeypatch, window):
    from alysis_code.bots.owl_toolset import SMALL_CONTEXT_WINDOW_TOKENS

    if window == "small":
        monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", str(SMALL_CONTEXT_WINDOW_TOKENS // 4))
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot(
        "alice", BotInput(name="Atlas", purpose="Help organize notes", memory_enabled=False)
    )
    chat = store.create_chat("alice", bot["id"], "New conversation")
    store.enqueue("alice", chat["id"], TaskInput(message="Θυμάσαι;", request_id="request_1"))
    task = store.claim("alice")
    # A tool the model saw in an earlier turn: memory has since been turned off.
    stale = respond(calls=[call("read_memory", {"memory_id": "μνήμη-1"})])
    task, client = run(store, task, [stale, respond("Η μνήμη είναι κλειστή.")])
    assert task["status"] == "completed"
    [result] = tool_results(client)
    assert result["executed"] is False
    assert "read_memory is not offered now" in result["error"]
    assert "Invalid tool arguments" not in result["error"] and "validation_errors" not in result
    offered = result["error"].split("Tools you can call now: ", 1)[1].rstrip(".").split(", ")
    assert "list_files" in offered and "read_memory" not in offered
    # A model with a small declared window is pointed at what it was actually offered.
    assert ("use_tool" in offered) is (window == "small")
    assert ("list_schedules" in offered) is (window == "large")
    failed = next(e["data"] for e in task["events"] if e["kind"] == "tool_failed")
    assert failed["error"] == result["error"]


@pytest.mark.parametrize("arguments", [{}, {"memory_id": 7}])
def test_real_validation_errors_keep_their_field_feedback(tmp_path, arguments):
    store, _, task = chat_task(tmp_path)
    bad = respond(calls=[call("read_memory", arguments)])
    task, client = run(store, task, [bad, respond("Done")])
    [result] = tool_results(client)
    assert result["error"] == "Invalid tool arguments."
    assert result["validation_errors"][0]["loc"] == ["memory_id"]


# ---------------------------------------------------------------- memory changing mid-call


class ChangesWhileThinking(ScriptedClient):
    """Runs ``change`` during the first ``times`` calls, as another writer would."""

    def __init__(self, steps, change, times=1):
        super().__init__(steps)
        self.change, self.times = change, times

    def chat(self, **kwargs):
        if len(self.requests) < self.times:
            self.change(len(self.requests))
        return super().chat(**kwargs)


def discarded(task):
    return sum(
        1
        for e in task["events"]
        if e["kind"] == "memory_refreshed" and e["data"].get("response_discarded")
    )


def memory_task(tmp_path):
    from alysis_code.bots.models import MemoryInput

    store, bot, task = chat_task(tmp_path, message="Τι προτιμώ;")
    memory = store.save_memory(
        "alice", bot["id"], MemoryInput(title="Γλώσσα", content="Απαντά στα ελληνικά")
    )
    return store, bot, task, memory


def test_index_metadata_written_mid_call_does_not_discard_the_response(tmp_path):
    from alysis_code.bots.memory_applicability import source_hash

    store, bot, task, memory = memory_task(tmp_path)

    def index(_):
        # What MemoryApplicability writes: the memory's scope, its content unchanged.
        with store.connect(write=True) as db:
            db.execute(
                "INSERT INTO memory_applicability VALUES(?,?,?)",
                (memory["id"], source_hash(memory), "general"),
            )

    client = ChangesWhileThinking([respond("Στα ελληνικά.")], index)
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    assert store.list_memories("alice", bot["id"])[0]["context_scope"] == "general"
    saved = store.task("alice", task["id"])
    assert saved["status"] == "completed" and saved["output"] == "Στα ελληνικά."
    assert len(client.requests) == 1 and discarded(saved) == 0


def test_a_note_from_the_owls_other_running_work_does_not_discard_the_response(tmp_path):
    from alysis_code.bots.background import BackgroundStore
    from alysis_code.bots.models import NoteInput

    store, bot, first = chat_task(tmp_path)
    background = BackgroundStore(store, "alice")
    background.submit(first, "Long work", [])
    assert background.claim()["id"] == first["id"]  # still running, off the reply loop
    store.enqueue(
        "alice", first["chat_id"], TaskInput(message="和计划一样吗?", request_id="request_2")
    )
    task = store.claim("alice")

    def note(_):
        store.save_note(
            "alice",
            bot["id"],
            NoteInput(title="进度", content="第二步完成"),
            source_task_id=first["id"],
        )

    client = ChangesWhileThinking([respond("是的。"), respond("Unused")], note)
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    saved = store.task("alice", task["id"])
    assert saved["status"] == "completed" and saved["output"] == "是的。"
    assert len(client.requests) == 1 and discarded(saved) == 0


def test_the_persons_memory_edit_mid_call_still_discards_but_at_most_twice(tmp_path):
    from alysis_code.bots.models import MemoryInput

    store, bot, task, memory = memory_task(tmp_path)

    def edit(n):
        store.save_memory(
            "alice",
            bot["id"],
            MemoryInput(title="Γλώσσα", content=f"Απαντά στα ελληνικά, εκδοχή {n}"),
            memory_id=memory["id"],
        )

    # The person edits once: that response is discarded and the next sees the new version.
    client = ChangesWhileThinking([respond("Παλιό"), respond("Νέο")], edit)
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    saved = store.task("alice", task["id"])
    assert saved["output"] == "Νέο" and discarded(saved) == 1
    assert "εκδοχή 0" in json.dumps(client.requests[1], ensure_ascii=False)

    # Memory that changes during every call cannot loop the turn: two discards, then kept.
    store, bot, task, memory = memory_task(tmp_path / "busy")
    steps = [respond(f"Απάντηση {n}") for n in range(5)]
    client = ChangesWhileThinking(steps, edit, times=5)
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    saved = store.task("alice", task["id"])
    assert saved["status"] == "completed" and saved["output"] == "Απάντηση 2"
    assert discarded(saved) == 2 and len(client.requests) == 3


def test_computer_work_discards_for_a_changing_note_at_most_twice(study):
    from alysis_code.bots.execution import TaskClient
    from alysis_code.bots.models import NoteInput

    store, task, manager, _ = study
    note = {"revision": 0, "id": None}

    def edit(n):
        saved = store.save_note(
            "owner",
            task["bot_id"],
            NoteInput(title="Σχέδιο", content=f"Έκδοση {n}", expected_revision=note["revision"]),
            note_id=note["id"],
        )
        note.update(revision=saved["revision"], id=saved["id"])

    client = ChangesWhileThinking([respond(f"Report {n}") for n in range(5)], edit, times=5)
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    answer = adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    assert answer.content == "Report 2" and len(client.requests) == 3
    kinds = [e["kind"] for e in store.task("owner", task["id"])["events"]]
    assert kinds.count("execution_response_discarded") == 2


# ---------------------------------------------------------------- a provider asking to wait


class WaitingToken(EventCancellationToken):
    """Records each pause instead of sleeping; ``stop_at`` presses Stop during that pause."""

    def __init__(self, stop_at=None):
        super().__init__(threading.Event())
        self.waits, self.stop_at = [], stop_at

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.stop_at == len(self.waits):
            self._event.set()
        return self._event.is_set()


def rate_limited(seconds):
    from alysis_code.llm.types import LLMError

    error = LLMError("LLM error 429: rate limited")
    error.provider_status_code = 429
    error.provider_retry_after = seconds
    return error


def waits(task):
    return [e["data"] for e in task["events"] if e["kind"] == "provider_retry"]


@pytest.mark.parametrize("kind", ["chat", "routine"])
def test_a_rate_limit_window_is_waited_out_instead_of_ending_the_turn(tmp_path, kind):
    store, bot, task = chat_task(tmp_path) if kind == "chat" else routine_task(tmp_path)
    token = WaitingToken()
    client = ScriptedClient([rate_limited(45), respond("Έτοιμο.")])
    BotRuntime(store, "alice", lambda _: client).run(task, token)
    saved = store.task("alice", task["id"])
    assert saved["status"] == "completed" and saved["output"] == "Έτοιμο."
    assert token.waits == [45]
    # The person sees the wait as a step, with how long and that Stop ends it.
    [event] = waits(saved)
    assert event["provider_asked"] is True and "45 seconds" in event["message"]
    if kind == "routine":
        assert store.list_schedules("alice", bot["id"])[0]["state"] == "active"


def test_long_waits_are_bounded_per_wait_and_per_turn(tmp_path):
    from alysis_code.bots.runtime import (
        PROVIDER_ASKED_TURN_SECONDS,
        PROVIDER_ASKED_WAIT_SECONDS,
        PROVIDER_INTERRUPTED,
    )

    # One wait longer than a turn honours ends the turn at once, as before.
    store, _, task = chat_task(tmp_path)
    token = WaitingToken()
    client = ScriptedClient([rate_limited(PROVIDER_ASKED_WAIT_SECONDS + 1), respond("Never")])
    BotRuntime(store, "alice", lambda _: client).run(task, token)
    saved = store.task("alice", task["id"])
    assert saved["status"] == "failed" and saved["error"] == PROVIDER_INTERRUPTED
    assert token.waits == [] and len(client.requests) == 1
    # Waits that each fit stop once the turn's total would pass its bound.
    each = PROVIDER_ASKED_TURN_SECONDS * 0.4
    store, _, task = chat_task(tmp_path / "total")
    token = WaitingToken()
    client = ScriptedClient([rate_limited(each)] * 3 + [respond("Never")])
    BotRuntime(store, "alice", lambda _: client).run(task, token)
    saved = store.task("alice", task["id"])
    assert saved["status"] == "failed" and saved["error"] == PROVIDER_INTERRUPTED
    assert token.waits == [each, each] and len(client.requests) == 3


def test_stop_during_a_long_wait_cancels_without_another_call(tmp_path):
    store, _, task = chat_task(tmp_path)
    token = WaitingToken(stop_at=1)
    client = ScriptedClient([rate_limited(90), respond("Never")])
    BotRuntime(store, "alice", lambda _: client).run(task, token)
    assert store.task("alice", task["id"])["status"] == "cancelled"
    assert len(client.requests) == 1


def test_computer_work_waits_out_a_rate_limit_window(study, monkeypatch):
    from alysis_code.bots.execution import TaskClient

    store, task, manager, _ = study
    client = ScriptedClient([rate_limited(60), respond("Still working")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    token = WaitingToken()
    adapter = TaskClient(runtime, task, client, token)
    answer = adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    assert answer.content == "Still working" and token.waits == [60]
    assert waits(store.task("owner", task["id"]))[0]["provider_asked"] is True


# ---------------------------------------------------------------- empty optional arguments


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_optional_argument_is_not_given(tmp_path, empty):
    store, bot, task = chat_task(tmp_path)
    save = call(
        "propose_memory",
        {"title": "Γλώσσα", "content": "Απαντά στα ελληνικά", "replaces_memory_id": empty},
    )
    task, client = run(store, task, [respond(calls=[save]), respond("Το θυμάμαι.")])
    [result] = tool_results(client)
    assert result["status"] == "active", result
    assert [m["content"] for m in store.list_memories("alice", bot["id"])] == [
        "Απαντά στα ελληνικά"
    ]


def test_empty_values_a_field_accepts_are_kept_and_required_ones_still_fail():
    from pydantic import ValidationError

    from alysis_code.bots.desktop import DEFINITIONS
    from alysis_code.bots.models import MemoryProposal, tool_input
    from alysis_code.bots.owl_tools import TOOL_DEFINITIONS

    element = DEFINITIONS["computer_element"][0]
    # An empty text is a value here (clear the field), so it is sent as written.
    cleared = tool_input(element, {"source_id": "s", "ref": 1, "action": "set_text", "text": ""})
    assert cleared.text == ""
    # A null for an optional number takes its default instead of failing the call.
    page = tool_input(TOOL_DEFINITIONS["read_file"][0], {"file_id": "文件", "offset": None})
    assert page.offset == 0
    # Required fields are never filled in, and other errors still come back.
    with pytest.raises(ValidationError):
        tool_input(MemoryProposal, {"title": "", "content": "内容"})
    with pytest.raises(ValidationError) as failure:
        tool_input(
            MemoryProposal, {"title": "标题", "content": "x" * 5000, "replaces_memory_id": ""}
        )
    assert [e["loc"] for e in failure.value.errors()] == [("content",)]


# ---------------------------------------------------------------- calls written as text


OFFERED = [
    {
        "type": "function",
        "function": {
            "name": "save_file",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string"}, "offset": {"type": "integer"}},
            },
        },
    },
]


@pytest.mark.parametrize(
    "content",
    [
        '<tool_call>\n{"name": "read_file", "arguments": {"file_id": "σημειώσεις", "offset": 5}}\n'
        "</tool_call>",
        '<tool_call>{"name": "read_file", "arguments": "{\\"file_id\\": \\"σημειώσεις\\", '
        '\\"offset\\": 5}"}',
        '<function=read_file>{"file_id": "σημειώσεις", "offset": 5}</function>',
        "<tool_call>\n<function=read_file>\n<parameter=file_id>\nσημειώσεις\n</parameter>\n"
        "<parameter=offset>\n5\n</parameter>\n</function>\n</tool_call>",
        '{"name": "read_file", "arguments": {"file_id": "σημειώσεις", "offset": 5}}',
        '```json\n{"name": "read_file", "parameters": {"file_id": "σημειώσεις", "offset": 5}}\n```',
    ],
    ids=["tool_call", "string_arguments", "function", "parameters", "json", "fenced_json"],
)
def test_a_call_written_as_text_is_read_by_its_structure(content):
    from alysis_code.bots.text_tool_calls import read

    written = read(content, OFFERED)
    [found] = written.calls
    assert (found.name, found.arguments) == ("read_file", {"file_id": "σημειώσεις", "offset": 5})
    # The same call after a short preface keeps the preface as the reply's own text.
    if content.startswith("<"):
        written = read("先看看文件。\n" + content, OFFERED)
        assert written.preface == "先看看文件。" and written.calls[0].name == "read_file"


def test_a_parameter_declared_as_text_stays_text():
    from alysis_code.bots.text_tool_calls import read

    written = read(
        "<function=save_file><parameter=name>123</parameter>"
        "<parameter=content>[1, 2]</parameter></function>",
        OFFERED,
    )
    assert written.calls[0].arguments == {"name": "123", "content": "[1, 2]"}


@pytest.mark.parametrize(
    "content",
    [
        '<tool_call>{"name": "read_file", "arguments": {"file_id": "x"}}</tool_call> 然后我会总结。',
        '<tool_call>{"name": "delete_everything", "arguments": {}}</tool_call>',
        '<tool_call>{"name": "read_file", "arguments": ["x"]}</tool_call>',
        "<tool_call>{not json}</tool_call>",
        '<function=read_file>{"file_id": "x"</function>',
    ],
    ids=["text_after", "not_offered", "not_an_object", "not_json", "broken_json"],
)
def test_markup_that_cannot_run_is_held_back(content):
    from alysis_code.bots.text_tool_calls import read

    written = read(content, OFFERED)
    assert written is not None and written.calls == []


@pytest.mark.parametrize(
    "content",
    [
        'Here is the format:\n```\n<tool_call>{"name": "read_file"}</tool_call>\n```\nDone.',
        "Use `<function=name>` markup for that.",
        '{"name": "Ελένη", "age": 7}',
        '{"name": "not_offered", "arguments": {}}',
        "Μια απλή απάντηση.",
    ],
    ids=["fenced_markup", "inline_markup", "other_json", "json_other_tool", "plain"],
)
def test_ordinary_replies_are_left_alone(content):
    from alysis_code.bots.text_tool_calls import read

    assert read(content, OFFERED) is None


@pytest.mark.parametrize("window", ["large", "small"])
def test_a_written_call_runs_through_the_same_path_as_a_real_one(tmp_path, monkeypatch, window):
    from alysis_code.bots.owl_toolset import SMALL_CONTEXT_WINDOW_TOKENS

    if window == "small":
        # list_schedules is then reached only through use_tool; the written call still runs.
        monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", str(SMALL_CONTEXT_WINDOW_TOKENS // 4))
    store, bot, task = chat_task(tmp_path, message="保存我的笔记")
    written = (
        "好的。\n<tool_call>\n"
        '{"name": "save_file", "arguments": {"name": "笔记.txt", "content": "第一行"}}\n'
        "</tool_call>\n<tool_call>\n"
        '{"name": "list_schedules", "arguments": {}}\n</tool_call>'
    )
    task, client = run(store, task, [respond(written), respond("已保存。")])
    assert task["status"] == "completed" and task["output"] == "已保存。"
    assert [f["name"] for f in store.list_files("alice", bot["id"])] == ["笔记.txt"]
    assert started(task, "save_file") == 1 and started(task, "list_schedules") == 1
    assistant = next(m for m in client.requests[1] if m.get("tool_calls"))
    assert assistant["content"] == "好的。"
    assert [c["function"]["name"] for c in assistant["tool_calls"]] == [
        "save_file",
        "list_schedules",
    ]
    assert "<tool_call>" not in json.dumps(client.requests[1], ensure_ascii=False)


def test_markup_that_cannot_run_is_never_published(tmp_path):
    from alysis_code.bots.text_tool_calls import WRITTEN_CALL_ERROR, WRITTEN_CALL_NUDGE

    stuck = '<tool_call>{"name": "send_email", "arguments": {"to": "Νίκος"}}</tool_call>'
    # Asked once, the model answers properly.
    store, _, task = chat_task(tmp_path)
    task, client = run(store, task, [respond(stuck), respond("Δεν μπορώ να στείλω email.")])
    assert task["output"] == "Δεν μπορώ να στείλω email."
    assert {"role": "system", "content": WRITTEN_CALL_NUDGE} in client.requests[1]
    # Twice, and the turn ends plainly with nothing published.
    store, _, task = chat_task(tmp_path / "again")
    task, client = run(store, task, [respond(stuck), respond(stuck), respond("Never")])
    assert task["status"] == "failed" and task["error"] == WRITTEN_CALL_ERROR
    assert task["failure"]["kind"] == "model" and len(client.requests) == 2


def test_computer_work_makes_a_written_call_as_the_call_itself(study):
    from alysis_code.bots.execution import TaskClient
    from alysis_code.bots.text_tool_calls import WRITTEN_CALL_NUDGE

    store, task, manager, _ = study
    written = '<function=computer_run>{"command": "ls /workspace"}</function>'
    client = ScriptedClient([respond("<tool_call>{oops}</tool_call>"), respond(written)])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    schemas = manager.schemas(task, eligible=True, approved=True, image_input=False)
    answer = adapter.chat(messages=[{"role": "user", "content": task["message"]}], tools=schemas)
    assert [(c.name, c.arguments) for c in answer.tool_calls] == [
        ("computer_run", {"command": "ls /workspace"})
    ]
    assert answer.content == ""
    assert {"role": "system", "content": WRITTEN_CALL_NUDGE} in client.requests[1]


# ---------------------------------------------------------------- refusals that say what to do


@pytest.mark.parametrize("letter", ["α", "字", "a"])
def test_a_value_too_long_says_nothing_was_done_and_how_to_resend(tmp_path, letter):
    from alysis_code.bots.models import NoteInput

    limit = NoteInput.model_fields["content"].metadata[1].max_length
    store, bot, task = chat_task(tmp_path)
    save = call("save_working_note", {"title": "Σχέδιο", "content": letter * (limit + 1)})
    task, client = run(store, task, [respond(calls=[save]), respond("Θα το χωρίσω.")])
    [result] = tool_results(client)
    assert result["executed"] is False
    assert result["error"].startswith("Nothing was done:")
    assert f"content: at most {limit:,} characters" in result["error"]
    assert "split" in result["error"]
    assert result["limits"] == [{"field": "content", "max_length": limit, "unit": "characters"}]
    assert result["validation_errors"][0]["type"] == "string_too_long"
    assert store.list_notes("alice", bot["id"]) == []
    # The activity line says the same plain thing, never the text that was sent.
    failed = next(e["data"] for e in task["events"] if e["kind"] == "tool_failed")
    assert failed["error"] == result["error"] and letter * 50 not in json.dumps(task)


def test_computer_work_returns_the_same_plain_refusal(study):
    from dataclasses import replace

    from test_bots_desktop_runtime import run_task

    store, task, manager, instances = study
    long = call("computer_run", {"command": "echo " + "ω" * 9000})
    client = ScriptedClient([respond(calls=[long]), respond("Split it.")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    run_task(runtime, task)
    refusal = next(json.loads(m["content"]) for m in client.requests[-1] if m.get("role") == "tool")
    assert refusal["executed"] is False and "command: at most 8,192 characters" in refusal["error"]
    assert not instances  # nothing reached a computer


@pytest.mark.parametrize(
    ("flags", "says"),
    [
        ({"timed_out": True}, "computer_launch"),
        ({"output_truncated": True}, "read that file in parts"),
        ({}, None),
    ],
    ids=["timed_out", "output_truncated", "finished"],
)
def test_a_command_stopped_part_way_says_it_may_have_partly_run(flags, says):
    from test_bots_desktop import Executor

    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers

    class Stopped(Executor):
        def call(self, owner, operation, args, *, cancel):
            result = super().call(owner, operation, args, cancel=cancel)
            return {**result, "timed_out": False, "output_truncated": False, **flags}

    manager = DesktopComputers("owner", ComputerViews(), factory=Stopped)
    try:
        result = manager.call(
            {"id": "task", "bot_id": "bot"},
            "computer_run",
            {"command": "python3 build.py", "timeout": 25},
            EventCancellationToken(threading.Event()),
            approved=True,
        )
    finally:
        manager.close()
    if says is None:
        assert "note" not in result
    else:
        assert "may have partly run" in result["note"] and says in result["note"]
        assert ("25-second" in result["note"]) is ("timed_out" in flags)


def test_a_file_too_large_for_the_library_says_nothing_was_saved(tmp_path):
    from alysis_code.bots.files import MAX_FILE_BYTES
    from alysis_code.bots.store import LibraryCapacityExceeded

    store, bot, _ = chat_task(tmp_path)
    with pytest.raises(LibraryCapacityExceeded, match="^Nothing was saved:.*several files"):
        store.save_stream("alice", bot["id"], "βίντεο.mp4", iter(()), MAX_FILE_BYTES + 1)
    assert store.list_files("alice", bot["id"]) == []


# ---------------------------------------------------------------- work on the owl's computer


def test_computer_work_refuses_and_then_stops_a_repeated_failing_call(study):
    from dataclasses import replace

    from test_bots_desktop_runtime import run_task

    store, task, manager, instances = study
    stuck = respond(calls=[call("computer_process", {"process_id": "διεργασία-进程"})])
    client = ScriptedClient([stuck])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    run_task(runtime, task)
    saved = store.task("owner", task["id"])
    # Work off the reply loop has nobody watching: two identical failures, then refusals.
    assert started(saved, "computer_process") == UNWATCHED.failures
    refused = [
        json.loads(m["content"])
        for m in client.requests[-1]
        if m.get("role") == "tool" and '"executed":false' in m["content"].replace(" ", "")
    ]
    assert refused and all("failed the same way 2 times" in r["error"] for r in refused)
    assert saved["status"] == "failed"
    assert saved["error"] == REPEAT_STOPPED
    assert saved["failure"]["kind"] == "model"
    assert not instances  # The refused calls never reached a computer.


# ---------------------------------------------------------------- the guard on its own


def test_an_image_is_a_fresh_look_and_never_a_repeat():
    guard = RepeatGuard(watched=False)
    for _ in range(UNWATCHED.stop * 2):
        assert guard.before("computer_observe", {}) is None
        assert guard.after("computer_observe", {}, {"frame": 1}, fresh_look=True) is None


def test_a_fresh_look_lets_a_refused_call_run_again():
    guard = RepeatGuard(watched=True)
    failure = {"error": "The element is not on the page."}
    for _ in range(WATCHED.failures):
        assert guard.before("browser_click", {"ref": "e7"}) is None
        guard.after("browser_click", {"ref": "e7"}, failure)
    assert guard.before("browser_click", {"ref": "e7"})["executed"] is False
    guard.after("browser_observe", {}, {"page": "now"}, fresh_look=True)
    assert guard.before("browser_click", {"ref": "e7"}) is None


def test_arguments_in_another_order_are_the_same_call():
    guard = RepeatGuard(watched=True)
    failure = {"error": "Δεν βρέθηκε."}
    for arguments in ({"a": 1, "b": "β"}, {"b": "β", "a": 1}, {"a": 1, "b": "β"}):
        assert guard.before("tool", arguments) is None
        guard.after("tool", arguments, failure)
    assert guard.before("tool", {"b": "β", "a": 1})["executed"] is False


def test_a_different_failure_starts_counting_again():
    guard = RepeatGuard(watched=True)
    for error in ("first", "second", "third", "fourth"):
        assert guard.before("tool", {}) is None
        guard.after("tool", {}, {"error": error})


def test_raised_errors_count_like_returned_ones():
    guard = RepeatGuard(watched=False)
    for _ in range(UNWATCHED.failures):
        assert guard.before("computer_process", {"process_id": "进程"}) is None
        guard.raised("computer_process", {"process_id": "进程"}, ValueError("gone"))
    assert guard.before("computer_process", {"process_id": "进程"})["executed"] is False
    with pytest.raises(LimitReached):
        for _ in range(UNWATCHED.stop):
            guard.before("computer_process", {"process_id": "进程"})
