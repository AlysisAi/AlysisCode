"""Independent fixtures for committed 17ecf75d; no model or Linux claims."""

import json

import pytest
from test_bots_browser_runtime import ClientFixture, response
from test_bots_desktop_runtime import study as study

from alysis_code.bots.cooperation import PeerRequest
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, NotFound
from alysis_code.llm.types import LLMResponse, ToolCall


def conversation_task(store, task):
    # Cooperation is controlled by the conversational bot. An accepted computer
    # task now enters the exclusive execution engine before any model call.
    store.finish("owner", task["id"], "completed", content="Earlier task finished.")
    store.enqueue(
        "owner",
        task["chat_id"],
        TaskInput(message="Ask the reviewer to review the work.", request_id="peer-review"),
    )
    return store.claim("owner")


@pytest.mark.parametrize("change", ["cancel", "permission", "wrong_task"])
def test_export_rechecks_persisted_authority_before_publication(study, change):
    store, task, manager, instances = study
    manager.store = store
    runtime = BotRuntime(store, "owner", lambda _: None, desktop_computers=manager)
    token = runtime.task_token(task["id"])
    manager.call(task, "computer_run", {"command": "fixture"}, token, approved=True)
    saved = store.save_file

    def revoke_before_save(owner, bot_id, data, **kwargs):
        if change == "cancel":
            store.cancel(owner, task["id"])
        elif change == "permission":
            with store.connect(write=True) as db:
                db.execute("UPDATE bots SET files_enabled=0 WHERE id=?", (bot_id,))
        else:
            other = store.save_bot(owner, BotInput(name="Other", purpose="No authority"))
            bot_id = other["id"]
        return saved(owner, bot_id, data, **kwargs)

    (instances[0].workspace / "result.txt").write_text("東京")
    store.save_file = revoke_before_save
    with pytest.raises(Conflict, match="cannot write"):
        manager.call(task, "computer_save_file", {"name": "result.txt"}, token, approved=True)
    assert not store.list_files("owner", task["bot_id"])


def test_other_owner_file_never_reaches_workspace(study):
    store, task, manager, instances = study
    manager.store = store
    foreign = store.save_bot("another", BotInput(name="Foreign", purpose="Private"))
    file = store.save_file(
        "another", foreign["id"], FileInput(name="secret.txt", content="Private")
    )
    token = BotRuntime(store, "owner", lambda _: None).task_token(task["id"])
    with pytest.raises(NotFound):
        manager.call(task, "computer_import_file", {"file_id": file["id"]}, token, approved=True)
    assert not instances


@pytest.mark.parametrize("control", ["request_peer", "update_peer_exchange"])
def test_mixed_peer_batch_rejects_all_then_repairs_once(study, control):
    store, task, manager, instances = study
    task = conversation_task(store, task)
    reviewer = store.save_bot("owner", BotInput(name="Reviewer", purpose="Review selected work"))
    runtime = BotRuntime(store, "owner", lambda _: None, desktop_computers=manager)
    if control == "request_peer":
        arguments = {"recipient_bot_id": reviewer["id"], "brief": "Review only"}
    else:
        source = task
        exchange = runtime.peers.submit(
            source, PeerRequest(recipient_bot_id=reviewer["id"], brief="Review only"), {}, 8
        )
        store.enqueue(
            "owner",
            task["chat_id"],
            TaskInput(message="Use four people and preserve 東京.", request_id="correction"),
        )
        task = store.claim("owner")
        arguments = {"exchange_id": exchange["id"]}
    mixed = LLMResponse(
        "",
        [
            ToolCall("control", control, arguments),
            ToolCall("write", "save_file", {"name": "must-not-exist.txt", "content": "wrong"}),
        ],
        {},
    )
    client = ClientFixture([mixed, response(name=control, arguments=arguments)])
    runtime.client_factory = lambda _: client
    runtime.run(task, runtime.task_token(task["id"]))
    feedback = [m for m in client.requests[1]["messages"] if m["role"] == "tool"][-2:]
    assert [m["tool_call_id"] for m in feedback] == ["control", "write"]
    assert all(json.loads(m["content"])["executed"] is False for m in feedback)
    assert not store.list_files("owner", task["bot_id"]) and not instances
    assert store.usage("owner")["calls"] == 2
    if control == "request_peer":
        assert runtime.peers.for_task(task["id"])["status"] == "queued"
    else:
        item = runtime.peers.for_task(source["id"])
        assert item["revision"] == 1
        assert item["corrections"] == [{"source_task_id": task["id"], "content": task["message"]}]
        assert store.task("owner", task["id"])["status"] == "completed"
        assert store.task("owner", task["id"])["output"] == ""


def test_repeated_mixed_peer_batches_spend_finite_budget_without_exchange(study):
    store, task, manager, instances = study
    task = conversation_task(store, task)
    reviewer = store.save_bot("owner", BotInput(name="Reviewer", purpose="Review"))
    mixed = LLMResponse(
        "",
        [
            ToolCall(
                "peer", "request_peer", {"recipient_bot_id": reviewer["id"], "brief": "Review"}
            ),
            ToolCall("write", "save_file", {"name": "must-not-exist.txt", "content": "wrong"}),
        ],
        {},
    )
    client = ClientFixture([mixed, mixed])
    runtime = BotRuntime(
        store,
        "owner",
        lambda _: client,
        desktop_computers=manager,
        limits=RuntimeLimits(calls_per_task=2),
    )
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.usage("owner")["calls"] == 2
    assert store.task("owner", task["id"])["status"] == "failed"
    assert runtime.peers.for_task(task["id"]) is None
    assert not store.list_files("owner", task["bot_id"]) and not instances
