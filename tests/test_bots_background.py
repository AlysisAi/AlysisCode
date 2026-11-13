"""Fixture evidence for durable conversation/job separation; no live models."""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from alysis_code.bots.coding import CodingResult, CodingStore, WorkspaceRegistration
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, ToolCall


@pytest.fixture
def background(tmp_path):
    store = Store(tmp_path / "bots.db")
    coding = CodingStore(store, "owner")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Work and answer"))
    workspace, worker = uuid4().hex, uuid4().hex
    coding.register(WorkspaceRegistration(id=workspace, worker_id=worker, name="Isolated"))
    coding.bind(bot["id"], workspace)
    chat = store.create_chat("owner", bot["id"], "Chat")
    original = store.enqueue(
        "owner", chat["id"], TaskInput(message="Build the report", request_id=uuid4().hex)
    )

    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kw):
            if any(t["function"]["name"] == "coding_task" for t in kw.get("tools") or []):
                return LLMResponse("", [ToolCall("start", "coding_task", {"brief": "Build"})], {})
            return LLMResponse("The recorded checks and changes are ready.", [], {})

    runtime = BotRuntime(store, "owner", lambda _: Client(), coding_enabled=True)
    runtime.run(store.claim("owner"), EventCancellationToken(threading.Event()))
    job = coding.claim(workspace, worker)
    assert store.task("owner", original["id"])["status"] == "waiting"
    return store, coding, runtime, original, job, worker


def send(store, original, message):
    return store.enqueue(
        "owner", original["chat_id"], TaskInput(message=message, request_id=uuid4().hex)
    )


def finish(coding, job, worker, revision=0):
    return coding.finish(
        job["id"],
        CodingResult(
            worker_id=worker,
            status="completed",
            instruction_revision=revision,
            outcome={"outcome": "verified_success", "verified_success": True},
        ),
    )


def test_reply_finishes_while_work_remains_running(background):
    store, coding, runtime, original, job, worker = background
    message = send(store, original, "What is running?")
    calls = []

    class Reply:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kw):
            calls.append(kw)
            assert original["id"] in str(kw["messages"])
            return LLMResponse("The report is still running.", [], {})

    runtime.client_factory = lambda _: Reply()
    runtime.run(store.claim("owner"), EventCancellationToken(threading.Event()))
    assert store.task("owner", message["id"])["status"] == "completed"
    assert coding.get(job["id"])["status"] == "running"
    assert len(calls) == 1
    assert not runtime.task_token(original["id"]).is_cancelled
    assert store.claim("owner") is None  # Waiting does not poll the model.


def test_inbox_survives_restart_and_is_prioritized_over_receipt(background):
    store, coding, runtime, original, job, worker = background
    queued = send(store, original, "Only use the revised format")
    finish(coding, job, worker)
    restarted = Store(store.path)
    assert restarted.recover("owner") == 0
    assert restarted.claim("owner")["id"] == queued["id"]
    restarted.finish("owner", queued["id"], "completed", content="Noted")
    report = restarted.claim("owner")
    assert report["id"] == original["id"]
    runtime.run(report, EventCancellationToken(threading.Event()))
    assert restarted.claim("owner") is None
    restarted.finish("owner", original["id"], "completed", content="duplicate")
    messages = restarted.conversation("owner", original["chat_id"])["messages"]
    assert (
        len([m for m in messages if m["task_id"] == original["id"] and m["role"] == "assistant"])
        == 1
    )
    assert coding.claim(job["workspace_id"], worker) is None


def test_corrections_are_exact_ordered_deduplicated_and_stale_success_is_rejected(background):
    store, coding, runtime, original, job, worker = background
    correction = send(store, original, "Διόρθωση: μόνο τοπικά. 日本語も残してください。")
    source = store.claim("owner")
    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(
            pool.map(lambda _: coding.update_instructions(source, original["id"]), range(8))
        )
    assert {v["instruction_revision"] for v in versions} == {1}
    assert coding.instructions(job["id"])[0]["content"] == correction["message"]
    late = finish(coding, job, worker, revision=0)
    assert late["status"] == "failed"
    assert late["result"]["outcome"]["reason"] == "newer_instructions_pending"
    assert not late["result"]["outcome"]["verified_success"]


def test_current_revision_keeps_actual_verdict_and_stop_does_not_cancel_followup(background):
    store, coding, runtime, original, job, worker = background
    queued = send(store, original, "Explain your approach")
    runtime.cancel(original["id"])
    assert runtime.task_token(original["id"]).is_cancelled
    assert coding.get(job["id"])["status"] == "cancelled"
    assert store.claim("owner")["id"] == queued["id"]
    assert finish(coding, job, worker)["status"] == "cancelled"


def test_second_writer_is_rejected_and_foreground_claim_is_serial(background):
    store, coding, runtime, original, job, worker = background
    send(store, original, "Another work request")
    send(store, original, "Another question")
    foreground = store.claim("owner")
    assert store.claim("owner") is None
    with pytest.raises(Conflict, match="already running"):
        coding.submit(foreground, "Duplicate write", [])
    assert coding.claim(job["workspace_id"], worker) is None


def test_stale_worker_yields_one_incomplete_receipt(background):
    store, coding, runtime, original, job, worker = background
    with store.connect(write=True) as db:
        db.execute("UPDATE coding_workspaces SET heartbeat=0")
    assert coding.for_task(original["id"])["status"] == "interrupted"
    report = store.claim("owner")
    runtime.run(report, EventCancellationToken(threading.Event()))
    assert store.task("owner", original["id"])["output"].startswith("Work status: incomplete.")
    assert store.claim("owner") is None


def test_completion_may_suggest_but_cannot_execute_more_work(background):
    store, coding, runtime, original, job, worker = background
    finish(coding, job, worker)
    calls = []

    class Reply:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                assert [t["function"]["name"] for t in kw["tools"]] == [
                    "propose_action",
                    "withdraw_action",
                ]
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "suggest",
                            "propose_action",
                            {
                                "question": "Shall I draft a checklist?",
                                "instructions": "Draft a short checklist for the reviewed report.",
                                "offline_computer": False,
                                "browser_origin": None,
                            },
                        )
                    ],
                    {},
                )
            assert kw["tools"] is None
            return LLMResponse("Result ready. A checklist is available for approval.", [], {})

    runtime.client_factory = lambda _: Reply()
    runtime.run(store.claim("owner"), EventCancellationToken(threading.Event()))
    proposal = runtime.proposals.list(original["bot_id"])[0]
    assert proposal["status"] == "pending" and proposal["ready"]
    assert proposal["task_id"] is None
    assert store.claim("owner") is None
    assert len(calls) == 2


def test_last_remaining_report_call_does_not_offer_optional_work(background):
    from alysis_code.bots.runtime import RuntimeLimits

    store, coding, runtime, original, job, worker = background
    finish(coding, job, worker)
    runtime.limits = RuntimeLimits(max_calls=store.usage("owner")["calls"] + 1)

    class Reply:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kw):
            assert kw["tools"] is None
            return LLMResponse("The actual result is ready.", [], {})

    runtime.client_factory = lambda _: Reply()
    runtime.run(store.claim("owner"), EventCancellationToken(threading.Event()))
    assert store.task("owner", original["id"])["status"] == "completed"
    assert runtime.proposals.list(original["bot_id"]) == []
