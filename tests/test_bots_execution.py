"""Actual Alysis Code loop and Bots queue; scripted model/desktop, not live evidence."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_bots_browser_runtime import ClientFixture, advertised_names, response
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.desktop_linux import ComputerActionUncertain
from alysis_code.bots.models import TaskInput
from alysis_code.bots.object_storage import ObjectStorageError
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import LibraryCapacityExceeded, LimitReached
from alysis_code.cancellation import CooperativeCancellationError

study = execution_study


@pytest.mark.parametrize("at_review", [False, True])
def test_correction_racing_model_admission_refreshes_without_failing_or_charging(
    study, monkeypatch, at_review
):
    from alysis_code.bots.execution import TaskClient
    from alysis_code.bots.reply_review import ReplyReview

    store, task, manager, instances = study
    client = ClientFixture(
        [
            *([response("Obsolete draft")] if at_review else []),
            response("The current request is to use euros."),
            *(
                [response('{"decision":"ready","reason":"Uses current correction"}')]
                if at_review
                else []
            ),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    claimed = runtime.background.claim()
    if at_review:
        runtime.reply_review = ReplyReview(runtime)
    original, attempts = store.reserve_call, []

    def reserve(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == (2 if at_review else 1):
            correction = store.enqueue(
                "owner",
                task["chat_id"],
                TaskInput(message="Use euros", request_id="admission-race"),
            )
            store.claim("owner")
            runtime.background.correct(correction, task["id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "reserve_call", reserve)
    adapter = TaskClient(runtime, claimed, client, runtime.task_token(task["id"]))
    result = adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    assert result.content == "The current request is to use euros."
    assert "Use euros" in str(client.requests[-1]["messages"])
    assert store.usage("owner")["calls"] == len(client.requests) == (3 if at_review else 1)
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert not instances  # No stale command or hidden execution was replayed.


def test_execution_sends_a_call_again_after_a_brief_provider_outage(study, monkeypatch):
    import alysis_code.bots.runtime as runtime_module
    from alysis_code.bots.execution import TaskClient
    from alysis_code.llm.types import LLMError

    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    store, task, manager, _ = study
    outage = LLMError("LLM error 503: temporarily unavailable")
    outage.provider_status_code = 503
    client = ClientFixture([outage, TimeoutError("read timeout"), response("Still working")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    result = adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    assert result.content == "Still working"
    assert len(client.requests) == 3
    assert client.requests[0]["messages"] == client.requests[2]["messages"]
    events = store.task("owner", task["id"])["events"]
    retries = [e["data"] for e in events if e["kind"] == "provider_retry"]
    assert [r["exception_type"] for r in retries] == ["LLMError", "TimeoutError"]
    assert store.usage("owner")["calls"] == 3  # every attempt is accounted


@pytest.mark.parametrize("permanent", [False, True])
def test_execution_gives_up_on_rejections_and_repeated_outages(study, monkeypatch, permanent):
    import alysis_code.bots.runtime as runtime_module
    from alysis_code.bots.execution import TaskClient
    from alysis_code.llm.types import LLMError

    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    store, task, manager, _ = study

    def failure():
        error = LLMError(f"LLM error {400 if permanent else 503}: no")
        error.provider_status_code = 400 if permanent else 503
        return error

    client = ClientFixture([failure() for _ in range(5)])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    with pytest.raises(LLMError):
        adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    assert len(client.requests) == (1 if permanent else 4)


def test_execution_trims_old_reference_turns_as_new_receipts_accumulate(study):
    from alysis_code.bots.execution import TaskClient

    store, task, manager, _ = study
    client = ClientFixture([response("First"), response("Second")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, context_characters=16000, context_tokens=50000)
    context = [
        {"role": "user", "content": "old " * 3000, "_history_task_id": "old-turn"},
        {"role": "assistant", "content": "Old answer", "_history_task_id": "old-turn"},
        {"role": "user", "content": "Current source: 雲, -2.00"},
    ]
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]), context)
    messages = [{"role": "user", "content": task["message"]}]
    adapter.chat(messages=messages, tools=[])
    assert "old " * 3000 in str(client.requests[0]["messages"])
    messages.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "done",
                        "type": "function",
                        "function": {"name": "computer_run", "arguments": '{"command":"inspect"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "done", "content": "result " * 700},
        ]
    )
    adapter.chat(messages=messages, tools=[])
    wire = client.requests[1]["messages"]
    assert "old " * 3000 not in str(wire)
    assert "Old answer" not in str(wire)
    assert "Current source: 雲, -2.00" in str(wire)
    assert any(
        m["role"] == "system" and "Saved memory and working notes were refreshed" in m["content"]
        for m in wire
    )
    assert any(m.get("tool_call_id") == "done" for m in wire)
    assert all("_history_task_id" not in m for m in wire)
    assert context[0]["_history_task_id"] == "old-turn"  # Saved source unchanged.


def test_execution_replaces_checkpoint_reference_snapshot_with_current_note(study):
    from alysis_code.bots.execution import TaskClient
    from alysis_code.bots.models import NoteInput

    store, task, manager, _ = study
    client = ClientFixture([response("Current decision acknowledged")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    note = store.save_note(
        "owner", task["bot_id"], NoteInput(title="Project", content="Obsolete checkpoint decision")
    )
    context = [{"role": "user", "content": "Original work constraint"}]
    runtime._refresh_memory_context(task, context, None)
    checkpoint = json.loads(json.dumps(context))
    store.save_note(
        "owner",
        task["bot_id"],
        NoteInput(
            title="Project",
            content="Current corrected decision",
            expected_revision=note["revision"],
        ),
        note_id=note["id"],
    )
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]), checkpoint)
    adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    wire = client.requests[0]["messages"]
    assert "Current corrected decision" in str(wire)
    assert "Obsolete checkpoint decision" not in str(wire)
    assert "Original work constraint" in str(wire)
    assert all("_bot_reference_state" not in message for message in wire)
    assert "Obsolete checkpoint decision" in str(checkpoint)  # Durable input unchanged.


def test_approved_execution_automatically_leaves_chat_and_uses_code_engine(study):
    store, task, manager, instances = study
    client = ClientFixture(
        [
            response(name="computer_run", arguments={"command": "printf inspected"}),
            response("The command finished; no file was requested."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    assert client.requests == []  # No routing/model handoff call.
    assert store.task("owner", task["id"])["status"] == "waiting"
    claimed = runtime.background.claim()
    runtime.run(claimed, runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert result["status"] == "completed", result
    assert instances[0].calls[0][0] == "run"
    assert instances[0].closed
    names = advertised_names(client.requests[0])
    assert "computer_run" in names
    assert {"list_memories", "read_memory", "search_working_notes", "read_working_note"} <= names
    assert not {"propose_memory", "save_working_note", "consolidate_working_notes"} & names
    assert not names & {
        "shell_run",
        "read_file_text",
        "apply_patch",
        "coding_task",
        "continue_in_background",
    }
    assert any(e["kind"] == "execution_started" for e in result["events"])
    from alysis_code.agent.reproduction_first import REPRODUCTION_FIRST_CONDITIONAL_DIRECTIVE
    from alysis_code.config import AppConfig

    assert all(
        REPRODUCTION_FIRST_CONDITIONAL_DIRECTIVE not in (message.get("content") or "")
        for request in client.requests
        for message in request["messages"]
    )
    assert AppConfig().reproduction_first_enabled  # Main Code sessions keep their policy.


def test_reply_and_correction_while_code_engine_is_reasoning(study):
    store, task, manager, instances = study
    entered, resume = threading.Event(), threading.Event()
    requests = []

    class WorkClient:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                entered.set()
                assert resume.wait(15)
                return response(name="computer_run", arguments={"command": "stale command"})
            assert "Use euros" in str(kwargs["messages"])
            if len(requests) == 2:
                return response(name="computer_run", arguments={"command": "printf EUR"})
            return response("Used euros.")

    runtime = BotRuntime(store, "owner", lambda _: WorkClient(), desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    claimed = runtime.background.claim()
    with ThreadPoolExecutor() as pool:
        work = pool.submit(runtime.run, claimed, runtime.task_token(task["id"]), background=True)
        assert entered.wait(15)
        queued = store.enqueue(
            "owner", task["chat_id"], TaskInput(message="Use euros", request_id="correction")
        )
        foreground = store.claim("owner")
        assert foreground["id"] == queued["id"]
        runtime.background.correct(foreground, task["id"])
        runtime.client_factory = lambda _: ClientFixture([response("The correction is queued.")])
        runtime.run(foreground, runtime.task_token(foreground["id"]))
        assert store.task("owner", foreground["id"])["status"] == "completed"
        assert not work.done()
        resume.set()
        work.result(timeout=30)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert [args["command"] for op, args in instances[0].calls if op == "run"] == ["printf EUR"]


def test_last_available_call_reports_without_allowing_more_actions(study):
    store, task, manager, instances = study
    requests = []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                assert "computer_run" in advertised_names(kwargs)
                assert "2 model calls remaining" in str(kwargs["messages"])
                return response(name="computer_run", arguments={"command": "printf inspected"})
            assert kwargs["tools"] is None
            assert "1 model calls remaining" in str(kwargs["messages"])
            assert "reserved final call" in str(kwargs["messages"])
            return response("The source was inspected. No report was created.")

    runtime = BotRuntime(store, "owner", lambda _: Client(), desktop_computers=manager)
    runtime.limits = replace(runtime.limits, calls_per_task=2, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    claimed = runtime.background.claim()
    runtime.run(claimed, runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert result["status"] == "completed", result
    assert result["calls"] == 2
    assert "No report was created" in result["output"]
    assert len(instances[0].calls) == 1
    assert instances[0].closed


@pytest.mark.parametrize("repair", [0, 1, 2])
def test_code_report_review_sees_real_tool_evidence_and_never_replays_work(study, repair):
    from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview

    store, task, manager, instances = study
    requests = []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                return response(name="computer_run", arguments={"command": "printf inspected"})
            if len(requests) % 2 == 0:
                tool_messages = [m for m in kwargs["messages"] if m["role"] == "tool"]
                assert any("actual fixture result" in m["content"] for m in tool_messages)
                if not repair:
                    assert kwargs["tools"] is None  # report + review reserve
                return response(
                    "The requested window was displayed."
                    if len(requests) <= repair * 2
                    else "Command output was captured; no visible window was verified."
                )
            assert kwargs["tools"] is None
            assert kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS
            payload = json.loads(kwargs["messages"][1]["content"])
            assert any(
                m["role"] == "tool" and "actual fixture result" in m["content"]
                for m in payload["context_and_receipts"]
            )
            return response(
                json.dumps(
                    {
                        "decision": "repair" if len(requests) <= repair * 2 + 1 else "ready",
                        "reason": "Captured output alone does not prove a visible window.",
                    }
                )
            )

    runtime = BotRuntime(store, "owner", lambda _: Client(), desktop_computers=manager)
    runtime.reply_review = ReplyReview(runtime)
    runtime.limits = replace(
        runtime.limits, calls_per_task=8 if repair else 3, compaction_enabled=False
    )
    runtime.run(task, runtime.task_token(task["id"]))
    runtime.run(runtime.background.claim(), runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert result["status"] == "completed", result
    assert result["calls"] == 3 + repair * 2
    assert result["output"] == ("Command output was captured; no visible window was verified.")
    assert "The requested window was displayed." not in result["output"]
    finished = next(e for e in result["events"] if e["kind"] == "execution_finished")
    # The published report is not a replacement for the engine's audit outcome.
    assert finished["data"]["outcome"]["verified_success"] is False
    assert len(instances[0].calls) == 1 and instances[0].closed


@pytest.mark.parametrize("status_ready", [True, False])
def test_background_report_falls_back_to_a_plain_status_before_giving_up(study, status_ready):
    from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
    from alysis_code.bots.runtime import REVIEW_STATUS_ONLY

    store, task, manager, instances = study
    drafts, status_requests = [], []

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            messages = kwargs["messages"]
            if messages[0]["content"] == REVIEW_INSTRUCTIONS:
                ready = status_ready and len(status_requests) == 1
                return response(
                    json.dumps(
                        {
                            "decision": "ready" if ready else "repair",
                            "reason": "Only one of the requested items is in the saved file.",
                        }
                    )
                )
            if not drafts and not status_requests:
                drafts.append("tool")
                return response(name="computer_run", arguments={"command": "printf inspected"})
            if any(REVIEW_STATUS_ONLY in str(m.get("content")) for m in messages):
                status_requests.append(kwargs)
                assert kwargs["tools"] is None  # a status, not more work
                return response("Saved report.txt with one item; the other items were not found.")
            drafts.append("draft")
            return response("All requested items are in the report.")

    runtime = BotRuntime(store, "owner", lambda _: Client(), desktop_computers=manager)
    runtime.reply_review = ReplyReview(runtime)
    runtime.limits = replace(runtime.limits, calls_per_task=14, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    runtime.run(runtime.background.claim(), runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert drafts.count("draft") == 3 and len(status_requests) == 1
    if status_ready:
        assert result["status"] == "completed", result
        assert result["output"].startswith("Saved report.txt with one item")
    else:
        assert result["status"] == "failed"
        assert "didn't match the results" in result["error"]
    assert len(instances[0].calls) == 1  # the command never ran again


def test_stop_during_command_closes_executor_without_replaying(study, monkeypatch):
    from test_bots_desktop import Executor

    store, task, manager, instances = study
    entered = threading.Event()
    calls = []

    def running(self, owner, operation, args, *, cancel):
        calls.append(operation)
        entered.set()
        while not cancel():
            threading.Event().wait(0.01)
        raise CooperativeCancellationError("Stopped")

    monkeypatch.setattr(Executor, "call", running)
    client = ClientFixture([response(name="computer_run", arguments={"command": "long command"})])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.run(task, runtime.task_token(task["id"]))
    claimed = runtime.background.claim()
    with ThreadPoolExecutor() as pool:
        work = pool.submit(runtime.run, claimed, runtime.task_token(task["id"]), background=True)
        assert entered.wait(15)
        runtime.cancel(task["id"])
        work.result(timeout=10)
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert calls == ["run"] and len(client.requests) == 1
    assert instances[0].closed and not manager._computers


def test_rejected_execution_call_does_not_remain_running_or_log_arguments(study):
    store, task, manager, instances = study
    client = ClientFixture(
        [
            response(
                name="computer_run",
                arguments={"command": "private-synthetic-argument", "timeout": 300},
            ),
            response(name="computer_run", arguments={"command": "printf inspected"}),
            response("The corrected command finished."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    runtime.run(runtime.background.claim(), runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert result["status"] == "completed", result
    activity = [
        e
        for e in result["events"]
        if e["kind"] in {"tool_started", "tool_completed", "tool_failed"}
    ]
    assert [e["kind"] for e in activity] == [
        "tool_started",
        "tool_failed",
        "tool_started",
        "tool_completed",
    ]
    assert activity[1]["data"]["error_type"] == "ValidationError"
    assert "private-synthetic-argument" not in json.dumps(activity)
    assert [args["command"] for op, args in instances[0].calls if op == "run"] == [
        "printf inspected"
    ]


@pytest.mark.parametrize(
    "error",
    [LibraryCapacityExceeded, LimitReached, ObjectStorageError, ComputerActionUncertain],
)
def test_only_definite_library_refusal_allows_model_to_explain_blocker(study, monkeypatch, error):
    store, task, manager, _ = study
    attempted = []

    def reject(*args, **kwargs):
        attempted.append(True)
        raise error("Synthetic operation refusal")

    monkeypatch.setattr(manager, "call", reject)
    client = ClientFixture(
        [
            response(name="computer_run", arguments={"command": "synthetic operation"}),
            response("The export was refused; no Library file was published."),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.run(task, runtime.task_token(task["id"]))
    runtime.run(runtime.background.claim(), runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    if error is LibraryCapacityExceeded:
        assert result["status"] == "completed", result
        assert len(client.requests) == 2
        assert "Synthetic operation refusal" in str(client.requests[-1]["messages"])
    else:
        assert result["status"] in {"failed", "interrupted"}, result
        assert len(client.requests) == 1
    assert attempted == [True]  # Neither branch replays the refused/uncertain action.
    failures = [e for e in result["events"] if e["kind"] == "tool_failed"]
    assert len(failures) == 1 and failures[0]["data"]["error_type"] == error.__name__


def test_failed_activity_write_cannot_hide_uncertain_action(study, monkeypatch):
    from test_bots_desktop import Executor

    from alysis_code.bots.desktop_linux import ComputerActionUncertain

    store, task, manager, instances = study
    calls = []

    def uncertain(self, owner, operation, args, *, cancel):
        calls.append(operation)
        raise ComputerActionUncertain("Synthetic lost action receipt")

    event = store.event

    def activity(*args, **kwargs):
        if args[2] == "tool_failed":
            raise RuntimeError("Synthetic activity write outage")
        return event(*args, **kwargs)

    monkeypatch.setattr(Executor, "call", uncertain)
    monkeypatch.setattr(store, "event", activity)
    client = ClientFixture(
        [response(name="computer_run", arguments={"command": "synthetic action"})]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.run(task, runtime.task_token(task["id"]))
    runtime.run(runtime.background.claim(), runtime.task_token(task["id"]), background=True)
    result = store.task("owner", task["id"])
    assert result["status"] == "interrupted", result
    assert len(client.requests) == 1 and calls == ["run"]
    assert instances[0].closed


@pytest.mark.parametrize("claimed", [False, True])
def test_restart_preserves_queued_work_but_never_replays_started_execution(study, claimed):
    store, task, manager, instances = study
    runtime = BotRuntime(store, "owner", lambda _: ClientFixture([]), desktop_computers=manager)
    runtime.run(task, runtime.task_token(task["id"]))
    if claimed:
        runtime.background.claim()
    assert store.recover("owner") == int(claimed)
    if claimed:
        assert store.task("owner", task["id"])["status"] == "interrupted"
        assert runtime.background.claim() is None
    else:
        assert runtime.background.claim()["id"] == task["id"]
    assert not instances


def test_execution_context_is_bounded_and_excludes_pending_memory_bodies(study):
    from alysis_code.bots.execution import TaskClient
    from alysis_code.bots.models import MemoryInput, NoteInput

    store, task, manager, _ = study
    for i in range(150):
        store.save_memory(
            "owner",
            task["bot_id"],
            MemoryInput(title=f"Project {i}", content="Reference context " * 70),
        )
        store.save_note(
            "owner", task["bot_id"], NoteInput(title=f"Note {i}", content="Working context " * 70)
        )
    store.save_memory(
        "owner",
        task["bot_id"],
        MemoryInput(title="Pending", content="UNAPPROVED_PRIVATE_MARKER"),
        source_task_id=task["id"],
    )
    client = ClientFixture([response("Checked bounded references")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    adapter.chat(messages=[{"role": "user", "content": task["message"]}], tools=[])
    wire = json.dumps(client.requests[0]["messages"], ensure_ascii=False)
    assert len(wire) < 18000
    assert "UNAPPROVED_PRIVATE_MARKER" not in wire
    assert '"approved_count":150' in wire.replace('\\"', '"')
    assert len(store.list_memories("owner", task["bot_id"])) == 151


def test_note_change_during_code_reasoning_discards_unexecuted_action(study):
    from alysis_code.bots.execution import TaskClient
    from alysis_code.bots.models import NoteInput

    store, task, manager, _ = study
    note = store.save_note(
        "owner", task["bot_id"], NoteInput(title="Current decision", content="Use the old source")
    )

    class Client:
        model = "fixture"
        requests = []

        def chat(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                store.save_note(
                    "owner",
                    task["bot_id"],
                    NoteInput(
                        title=note["title"],
                        content="Use the corrected source",
                        expected_revision=note["revision"],
                    ),
                    note_id=note["id"],
                )
                return response(name="computer_run", arguments={"command": "stale action"})
            assert "Use the corrected source" in str(kwargs["messages"])
            return response(name="computer_run", arguments={"command": "current action"})

    client = Client()
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    # Use the actual advertised run schema, with the same approval as this task.
    schemas = manager.schemas(task, eligible=True, approved=True, image_input=False)
    answer = adapter.chat(messages=[{"role": "user", "content": task["message"]}], tools=schemas)
    assert answer.tool_calls[0].arguments["command"] == "current action"
    assert store.task("owner", task["id"])["calls"] == 2
    assert any(
        e["kind"] == "execution_response_discarded"
        for e in store.task("owner", task["id"])["events"]
    )


@pytest.mark.parametrize("computer_library", [False, True])
def test_computer_file_work_uses_verified_exports_instead_of_chat_writer(study, computer_library):
    store, task, manager, _ = study
    if computer_library:
        manager.store = store
    client = ClientFixture([response("No file was created.")])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    queued = runtime.background.claim()
    runtime.run(queued, runtime.task_token(queued["id"]), background=True)
    assert store.task("owner", task["id"])["status"] == "completed"
    names = advertised_names(client.requests[0])
    assert ("computer_save_file" in names) is computer_library
    assert ("save_file" in names) is not computer_library
    assert {"list_files", "read_file"} <= names


def test_research_guidance_reaches_chat_turns_and_computer_work():
    from alysis_code.bots.execution import PROMPT
    from alysis_code.bots.runtime import RESEARCH_GUIDANCE

    assert RESEARCH_GUIDANCE in PROMPT
    assert "keep searching until that many are supported" in RESEARCH_GUIDANCE
