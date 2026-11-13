"""Provider-free evidence for peer authority, lifecycle and durable delivery."""

import json
import threading
import time
from uuid import uuid4

import pytest

from alysis_code.bots.cooperation import PeerRequest, PeerRunner, PeerStore
from alysis_code.bots.models import BotInput, FileInput, MemoryInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, NotFound, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMOutputLimitError, LLMResponse, LLMUsage, ToolCall


@pytest.fixture
def peers(tmp_path):
    store = Store(tmp_path / "peers.db")
    a = store.save_bot("owner", BotInput(name="Coordinator", purpose="Own the result"))
    b = store.save_bot("owner", BotInput(name="Νεφέλη", purpose="Review the constraints"))
    other = store.save_bot("another", BotInput(name="Other", purpose="Private"))
    chat = store.create_chat("owner", a["id"], "Chat")
    store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Ask the reviewer for help. Preserve 東京.", request_id=uuid4().hex),
    )
    task = store.claim("owner")
    return store, PeerStore(store, "owner"), a, b, other, task


def submit(peers, **kwargs):
    store, ps, a, b, other, task = peers
    request = PeerRequest(recipient_bot_id=b["id"], brief="Review 東京 capacity", **kwargs)
    continuation = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "peer-call",
                "type": "function",
                "function": {"name": "request_peer", "arguments": request.model_dump_json()},
            }
        ],
    }
    return ps.submit(task, request, continuation, 8)


def client_for(fn):
    class Client:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            return fn(kwargs)

    return lambda _: Client()


def run_peer(peers, fn=None, limits=None):
    store, ps, *_ = peers
    item = ps.claim()
    assert item is not None
    runner = PeerRunner(
        ps,
        client_for(fn or (lambda _: LLMResponse("Capacity 7; 東京 stays exact.", [], {}))),
        limits or RuntimeLimits(),
        threading.Event(),
    )
    runner.run(item)


def correction(peers, message):
    store, ps, a, b, other, task = peers
    store.enqueue("owner", task["chat_id"], TaskInput(message=message, request_id=uuid4().hex))
    return store.claim("owner")


def test_real_task_flow_returns_one_peer_result_in_original_chat(peers):
    store, ps, a, b, other, task = peers
    calls = []

    def respond(kw):
        calls.append(kw)
        if kw["messages"][0]["content"].startswith("You are the receiving peer"):
            assert kw["tools"] is None
            return LLMResponse("Peer constraint: capacity 7, 東京 unchanged.", [], {})
        if kw["tools"]:
            return LLMResponse(
                "",
                [
                    ToolCall(
                        "peer-call",
                        "request_peer",
                        {
                            "recipient_bot_id": b["id"],
                            "brief": "Review the capacity and preserve 東京.",
                        },
                    )
                ],
                {},
            )
        assert "Peer constraint: capacity 7" in str(kw["messages"])
        return LLMResponse("The reviewer says capacity 7; 東京 is unchanged.", [], {})

    runtime = BotRuntime(store, "owner", client_for(respond))
    runtime.run(task, EventCancellationToken(threading.Event()))
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert store.claim("owner") is None
    PeerRunner(ps, runtime.client_factory, runtime.limits, threading.Event()).run(ps.claim())
    resumed = store.claim("owner")
    assert resumed["id"] == task["id"]
    runtime.run(resumed, EventCancellationToken(threading.Event()))
    store.finish("owner", task["id"], "completed", content="duplicate")
    messages = store.conversation("owner", task["chat_id"])["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert len(calls) == store.usage("owner")["calls"] == 3
    assert ps.for_task(task["id"])["reported_at"] is not None
    assert store.claim("owner") is None
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE bot_id=?)",
                (b["id"],),
            ).fetchone()[0]
            == 0
        )


def test_same_owner_selection_and_self_denial(peers):
    store, ps, a, b, other, task = peers
    for recipient, error in ((other["id"], NotFound), ("missing", NotFound), (a["id"], Conflict)):
        with pytest.raises(error):
            ps.submit(task, PeerRequest(recipient_bot_id=recipient, brief="Review"), {}, 8)
    assert ps.for_task(task["id"]) is None


def test_request_idempotence_and_different_payload_rejected(peers):
    first = submit(peers)
    assert submit(peers)["id"] == first["id"]
    store, ps, a, b, other, task = peers
    with pytest.raises(Conflict):
        ps.submit(task, PeerRequest(recipient_bot_id=b["id"], brief="Changed"), {}, 8)
    assert ps.claim()["id"] == first["id"]
    assert ps.claim() is None


def test_selected_files_snapshot_and_other_bot_files_denied(peers):
    store, ps, a, b, other, task = peers
    private = store.save_file("owner", b["id"], FileInput(name="private.txt", content="PRIVATE"))
    with pytest.raises(NotFound):
        submit(peers, shared_file_ids=[private["id"]])
    shared = store.save_file(
        "owner", a["id"], FileInput(name="入力.txt", content=" Νεφέλη / 東京\n")
    )
    exchange = submit(peers, shared_file_ids=[shared["id"]])
    with store.connect(write=True) as db:
        db.execute("UPDATE files SET content='changed later' WHERE id=?", (shared["id"],))
    item, messages, _ = ps.context(exchange["id"])
    assert item["shared_files"][0]["content"] == " Νεφέλη / 東京\n"
    assert "PRIVATE" not in str(messages)
    assert "changed later" not in str(messages)
    assert len(item["shared_files"][0]["sha256"]) == 64


def test_recipient_identity_memory_and_private_history_boundaries(peers):
    store, ps, a, b, other, task = peers
    store.save_memory("owner", b["id"], MemoryInput(title="Constraint", content="Capacity 7"))
    # The source bot's unrelated state is never sent to its peer.
    store.save_memory("owner", a["id"], MemoryInput(title="Private", content="SOURCE-PRIVATE-73"))
    exchange = submit(peers)
    _, messages, _ = ps.context(exchange["id"])
    text = json.dumps(messages, ensure_ascii=False)
    assert "Νεφέλη" in text and "Capacity 7" in text
    assert "SOURCE-PRIVATE-73" not in text
    assert task["message"] not in text
    assert "reference data" in text


def test_correction_discards_inflight_reply_and_preserves_exact_user_text(peers):
    store, ps, a, b, other, task = peers
    exchange = submit(peers)
    calls = []

    def respond(kw):
        calls.append(kw)
        if len(calls) == 1:
            change = correction(peers, "Correction: only 5 people; 東京 stays. No price changes.")
            ps.correct(change, exchange["id"])
            ps.correct(change, exchange["id"])
            store.finish("owner", change["id"], "completed", content="Queued")
            return LLMResponse("Stale capacity 7", [], {})
        assert "only 5 people; 東京 stays. No price changes." in str(kw["messages"])
        return LLMResponse("Capacity 5; 東京", [], {})

    run_peer(peers, respond)
    item = ps.for_task(task["id"])
    assert item["revision"] == 1 and len(item["corrections"]) == 1
    assert item["result"] == "Capacity 5; 東京"
    assert item["calls"] == 2


def test_memory_change_discards_stale_peer_reply(peers):
    store, ps, a, b, other, task = peers
    memory = store.save_memory("owner", b["id"], MemoryInput(title="Capacity", content="7"))
    submit(peers)
    seen = []

    def respond(kw):
        seen.append(kw)
        if len(seen) == 1:
            store.save_memory(
                "owner", b["id"], MemoryInput(title="Capacity", content="5"), memory_id=memory["id"]
            )
            return LLMResponse("Stale seven", [], {})
        return LLMResponse("Current five", [], {})

    run_peer(peers, respond)
    assert ps.for_task(task["id"])["result"] == "Current five"
    assert len(seen) == 2


def test_cancel_during_peer_call_discards_late_reply(peers):
    store, ps, a, b, other, task = peers
    submit(peers)

    def respond(kw):
        store.cancel("owner", task["id"])
        assert kw["cancellation_token"].is_cancelled
        return LLMResponse("Too late", [], {})

    run_peer(peers, respond)
    assert ps.for_task(task["id"])["status"] == "cancelled"
    assert ps.for_task(task["id"])["result"] == ""
    assert store.claim("owner") is None


@pytest.mark.parametrize("phase", ["queued", "running", "completed"])
def test_restart_never_replays_uncertain_peer_work(peers, phase):
    store, ps, a, b, other, task = peers
    submit(peers)
    if phase == "running":
        item = ps.claim()
        ps.reserve_call(item["id"], 0, "fixture", 100)
    elif phase == "completed":
        run_peer(peers)
    restarted = Store(store.path)
    restarted.recover("owner")
    recovered = PeerStore(restarted, "owner")
    item = recovered.for_task(task["id"])
    assert item["status"] == ("interrupted" if phase == "running" else phase)
    assert bool(recovered.claim()) == (phase == "queued")
    if phase == "running":
        assert restarted.usage("owner")["unconfirmed_calls"] == 1
        assert restarted.claim("owner")["id"] == task["id"]


def test_recipient_unavailable_before_dispatch_is_reported(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    with store.connect(write=True) as db:
        db.execute("DELETE FROM bots WHERE id=?", (b["id"],))
    run_peer(peers, lambda _: pytest.fail("No provider call for absent recipient"))
    assert ps.for_task(task["id"])["status"] == "failed"
    assert store.usage("owner")["calls"] == 0


def test_peer_tools_and_recursive_dispatch_are_never_executed(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    run_peer(
        peers,
        lambda _: LLMResponse(
            "",
            [ToolCall("recursive", "request_peer", {"recipient_bot_id": a["id"], "brief": "Loop"})],
            {},
        ),
    )
    assert ps.for_task(task["id"])["status"] == "failed"
    assert len(ps.list(a["id"])) == 1


def test_service_budget_charges_peer_calls(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    run_peer(
        peers, lambda _: pytest.fail("No model call past service cap"), RuntimeLimits(max_calls=0)
    )
    assert ps.for_task(task["id"])["status"] == "failed"
    assert store.usage("owner")["calls"] == 0


def test_peer_budget_bounds_repeated_context_changes(peers):
    store, ps, a, b, other, task = peers
    memory = store.save_memory("owner", b["id"], MemoryInput(title="Changing", content="0"))
    submit(peers)
    calls = []

    def respond(kw):
        calls.append(kw)
        store.save_memory(
            "owner",
            b["id"],
            MemoryInput(title="Changing", content=str(len(calls))),
            memory_id=memory["id"],
        )
        return LLMResponse("Stale", [], {})

    run_peer(peers, respond)
    item = ps.for_task(task["id"])
    assert item["status"] == "failed" and item["calls"] == 4
    assert len(calls) == store.usage("owner")["calls"] == 4


def test_provider_failure_is_unknown_and_not_retried(peers):
    store, ps, a, b, other, task = peers
    submit(peers)

    def fail(_):
        raise RuntimeError("SYNTHETIC-SECRET must not persist")

    run_peer(peers, fail)
    item = ps.for_task(task["id"])
    assert item["status"] == "interrupted"
    assert "SYNTHETIC-SECRET" not in item["error"]
    assert store.usage("owner")["unconfirmed_calls"] == 1


def test_incomplete_peer_response_is_accounted_and_never_published(peers):
    store, ps, a, b, other, task = peers
    submit(peers)

    def fail(_):
        raise LLMOutputLimitError(LLMResponse("Partial reply", [], {}, usage=LLMUsage(20, 10, 30)))

    run_peer(peers, fail)
    item = ps.for_task(task["id"])
    assert item["status"] == "failed" and item["result"] == ""
    receipt = store.task("owner", task["id"])["usage"][0]
    assert receipt["status"] == "incomplete"
    assert receipt["usage"]["total_tokens"] == 30


def test_foreground_message_runs_while_peer_is_blocked(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    entered, release = threading.Event(), threading.Event()

    def slow(_):
        entered.set()
        assert release.wait(5)
        return LLMResponse("Peer returned", [], {})

    worker = threading.Thread(target=lambda: run_peer(peers, slow))
    worker.start()
    try:
        assert entered.wait(3)
        followup = correction(peers, "Are you still available?")
        runtime = BotRuntime(
            store,
            "owner",
            client_for(lambda _: LLMResponse("Yes, the peer is still working.", [], {})),
        )
        runtime.run(followup, EventCancellationToken(threading.Event()))
        assert store.task("owner", followup["id"])["status"] == "completed"
        assert ps.for_task(task["id"])["status"] == "running"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_recipient_cannot_correct_source_or_read_another_owners_exchange(peers):
    store, ps, a, b, other, task = peers
    exchange = submit(peers)
    chat = store.create_chat("owner", b["id"], "Recipient chat")
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Change the origin", request_id=uuid4().hex)
    )
    incoming = store.claim("owner")
    with pytest.raises(NotFound):
        ps.correct(incoming, exchange["id"])
    with pytest.raises(NotFound):
        PeerStore(store, "another").read(other["id"], exchange["id"])


def test_queue_prioritizes_user_correction_before_peer_report(peers):
    store, ps, a, b, other, task = peers
    exchange = submit(peers)
    run_peer(peers)
    followup = correction(peers, "Correction: capacity must be 4")
    assert followup["id"] != task["id"]
    updated = ps.correct(followup, exchange["id"])
    assert updated["status"] == "queued" and updated["revision"] == 1
    assert store.claim("owner") is None


def test_correction_between_context_and_call_uses_latest_without_charging_retry(peers, monkeypatch):
    store, ps, a, b, other, task = peers
    exchange = submit(peers)
    reserve = ps.reserve_call
    changed = False

    def racing_reserve(*args):
        nonlocal changed
        if not changed:
            changed = True
            change = correction(peers, "Now use capacity 4; preserve 東京")
            ps.correct(change, exchange["id"])
            store.finish("owner", change["id"], "completed", content="Queued")
        return reserve(*args)

    monkeypatch.setattr(ps, "reserve_call", racing_reserve)

    def respond(kw):
        assert "Now use capacity 4; preserve 東京" in str(kw["messages"])
        return LLMResponse("Current capacity 4; 東京", [], {})

    run_peer(peers, respond)
    item = ps.for_task(task["id"])
    assert item["status"] == "completed" and item["calls"] == 1
    assert store.usage("owner")["calls"] == 1


def test_memory_changed_at_commit_cannot_publish_stale_reply(peers, monkeypatch):
    store, ps, a, b, other, task = peers
    memory = store.save_memory("owner", b["id"], MemoryInput(title="Capacity", content="7"))
    submit(peers)
    finish = ps.finish
    calls = []

    def racing_finish(*args, **kwargs):
        if len(calls) == 1:
            store.save_memory(
                "owner", b["id"], MemoryInput(title="Capacity", content="5"), memory_id=memory["id"]
            )
        return finish(*args, **kwargs)

    monkeypatch.setattr(ps, "finish", racing_finish)

    def respond(kw):
        calls.append(kw)
        return LLMResponse("Stale seven" if len(calls) == 1 else "Current five", [], {})

    run_peer(peers, respond)
    assert len(calls) == 2
    assert ps.for_task(task["id"])["result"] == "Current five"


def test_durable_cancel_wakes_provider_wait(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    entered = threading.Event()
    woke = threading.Event()

    def respond(kw):
        entered.set()
        if kw["cancellation_token"].wait_cancelled(3):
            woke.set()
        kw["cancellation_token"].throw_if_cancelled()
        return LLMResponse("Should not arrive", [], {})

    worker = threading.Thread(target=lambda: run_peer(peers, respond))
    worker.start()
    try:
        assert entered.wait(2)
        store.cancel("owner", task["id"])
        assert woke.wait(1)
    finally:
        worker.join(4)
    assert not worker.is_alive()
    assert ps.for_task(task["id"])["status"] == "cancelled"
    assert store.usage("owner")["unconfirmed_calls"] == 1


def test_runtime_shutdown_stops_peer_and_restart_does_not_replay(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    entered = threading.Event()

    def respond(kw):
        entered.set()
        assert kw["cancellation_token"].wait_cancelled(3)
        kw["cancellation_token"].throw_if_cancelled()

    runtime = BotRuntime(store, "owner", client_for(respond))
    runtime.start()
    try:
        assert entered.wait(2)
        assert runtime.is_running
    finally:
        runtime.stop()
    assert not runtime.is_running
    assert runtime._lease is None
    assert ps.for_task(task["id"])["status"] == "interrupted"
    assert store.usage("owner")["calls"] == 1
    assert ps.claim() is None


def test_runtime_delivers_completed_receipt_once_after_restart(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    run_peer(peers)
    report_calls = []

    def respond(kw):
        report_calls.append(kw)
        return LLMResponse("Saved reviewer result: capacity 7, 東京.", [], {})

    runtime = BotRuntime(Store(store.path), "owner", client_for(respond))
    runtime.start()
    try:
        until = time.monotonic() + 3
        while store.task("owner", task["id"])["status"] != "completed" and time.monotonic() < until:
            time.sleep(0.02)
        assert store.task("owner", task["id"])["status"] == "completed"
    finally:
        runtime.stop()
    assert len(report_calls) == 1 and report_calls[0]["tools"] is None
    assert len(store.conversation("owner", task["chat_id"])["messages"]) == 2
    runtime.start()
    runtime.stop()
    assert len(report_calls) == 1


def test_peer_api_requires_user_auth_and_omits_shared_file_contents(peers):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app

    store, ps, a, b, other, task = peers
    file = store.save_file("owner", a["id"], FileInput(name="scope.txt", content="Shared input"))
    exchange = submit(peers, shared_file_ids=[file["id"]])
    settings = Settings(
        store.path, "t" * 40, "fixture", "https://example.invalid", "fixture", owner="owner"
    )
    # No lifespan: this test verifies HTTP access to the prepared queued receipt.
    client = TestClient(create_app(settings))
    path = f"/v1/bots/{b['id']}/exchanges"
    assert client.get(path).status_code == 401
    assert client.get(f"/v1/tasks/{task['id']}").status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.token
    records = client.get(path).json()
    assert records[0]["id"] == exchange["id"]
    assert records[0]["shared_files"][0]["name"] == "scope.txt"
    assert "content" not in records[0]["shared_files"][0]
    assert "continuation" not in records[0]
    assert client.get(f"/v1/tasks/{task['id']}").json()["peer_exchange"]["id"] == exchange["id"]
    assert client.get(f"/v1/bots/{other['id']}/exchanges").status_code == 404


def test_v9_migration_keeps_existing_tasks_and_adds_empty_peer_store(peers):
    store, ps, a, b, other, task = peers
    with store.connect(write=True) as db:
        db.execute("DROP TABLE peer_calls")
        db.execute("DROP TABLE peer_exchanges")
        db.execute("PRAGMA user_version=9")
    migrated = Store(store.path)
    assert migrated.task("owner", task["id"])["message"] == task["message"]
    assert PeerStore(migrated, "owner").list(a["id"]) == []
    assert PeerStore(migrated, "owner").for_task(task["id"]) is None


def test_failed_parent_report_keeps_peer_result_without_claiming_delivery(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    run_peer(peers)
    resumed = store.claim("owner")
    runtime = BotRuntime(store, "owner", client_for(lambda _: LLMResponse("", [], {})))
    runtime.run(resumed, EventCancellationToken(threading.Event()))
    item = ps.for_task(task["id"])
    assert item["status"] == "completed" and item["result"]
    assert item["source_status"] == "failed"
    assert item["reported_at"] is None
    assert len(store.conversation("owner", task["chat_id"])["messages"]) == 1
    assert store.claim("owner") is None


def test_correction_control_turn_cannot_duplicate_the_original_report(peers):
    store, ps, a, b, other, task = peers
    exchange = submit(peers)
    run_peer(peers)
    followup = correction(peers, "Use exactly 3 attendees; keep the fixed date unchanged.")
    calls = []

    def respond(kw):
        calls.append(kw)
        if len(calls) == 1:
            return LLMResponse(
                "",
                [ToolCall("update", "update_peer_exchange", {"exchange_id": exchange["id"]})],
                {},
            )
        assert kw["tools"] is None
        return LLMResponse("Final current review: three attendees, fixed date unchanged.", [], {})

    runtime = BotRuntime(store, "owner", client_for(respond))
    token = EventCancellationToken(threading.Event())
    runtime.run(followup, token)
    assert len(calls) == 1
    assert store.task("owner", followup["id"])["status"] == "completed"
    assert [m["role"] for m in store.conversation("owner", task["chat_id"])["messages"]] == [
        "user",
        "user",
    ]
    run_peer(peers)
    runtime.run(store.claim("owner"), token)
    messages = store.conversation("owner", task["chat_id"])["messages"]
    assert [m["role"] for m in messages] == ["user", "user", "assistant"]
    assert messages[-1]["task_id"] == task["id"]


@pytest.mark.parametrize("arrival", ["before_call", "during_call"])
def test_user_correction_preempts_unpublished_peer_report(peers, arrival):
    store, ps, a, b, other, task = peers
    exchange = submit(peers)
    run_peer(peers)
    reporting = store.claim("owner")
    reports = []

    def enqueue_correction():
        return store.enqueue(
            "owner",
            task["chat_id"],
            TaskInput(message="Use three people; preserve 東京.", request_id=uuid4().hex),
        )

    def stale_report(kw):
        reports.append(kw)
        enqueue_correction()
        return LLMResponse("Old report: seven people.", [], {})

    if arrival == "before_call":
        enqueue_correction()
    runtime = BotRuntime(store, "owner", client_for(stale_report))
    token = EventCancellationToken(threading.Event())
    runtime.run(reporting, token)
    assert len(reports) == (1 if arrival == "during_call" else 0)
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert ps.for_task(task["id"])["reported_at"] is None
    assert [m["role"] for m in store.conversation("owner", task["chat_id"])["messages"]] == [
        "user",
        "user",
    ]

    followup = store.claim("owner")
    assert followup["id"] != task["id"]
    ps.correct(followup, exchange["id"])
    store.finish("owner", followup["id"], "completed", publish_reply=False)
    run_peer(peers, lambda _: LLMResponse("Current review: three people; 東京.", [], {}))
    current = BotRuntime(
        store,
        "owner",
        client_for(lambda _: LLMResponse("Current report: three people; 東京.", [], {})),
    )
    current.run(store.claim("owner"), token)
    messages = store.conversation("owner", task["chat_id"])["messages"]
    assert [m["role"] for m in messages] == ["user", "user", "assistant"]
    assert messages[-1]["content"] == "Current report: three people; 東京."
    assert ps.for_task(task["id"])["revision"] == 1


def test_user_stop_during_report_preserves_receipt_without_publishing_report(peers):
    store, ps, a, b, other, task = peers
    exchange = submit(peers)
    run_peer(peers)

    def reporting(_):
        store.enqueue(
            "owner",
            task["chat_id"],
            TaskInput(message="Stop this peer request now.", request_id=uuid4().hex),
        )
        return LLMResponse("Old report must not be delivered.", [], {})

    token = EventCancellationToken(threading.Event())
    BotRuntime(store, "owner", client_for(reporting)).run(store.claim("owner"), token)
    stop_calls = []

    def stopping(kw):
        stop_calls.append(kw)
        if len(stop_calls) == 1:
            return LLMResponse(
                "", [ToolCall("stop", "stop_peer_exchange", {"exchange_id": exchange["id"]})], {}
            )
        return LLMResponse(
            "Stopped the pending report; the saved reply remains in history.", [], {}
        )

    BotRuntime(store, "owner", client_for(stopping)).run(store.claim("owner"), token)
    item = ps.for_task(task["id"])
    assert item["source_status"] == item["status"] == "cancelled"
    assert item["result"] == "Capacity 7; 東京 stays exact."
    assert item["reported_at"] is None
    messages = store.conversation("owner", task["chat_id"])["messages"]
    assert [m["role"] for m in messages] == ["user", "user", "assistant"]
    assert messages[-1]["task_id"] != task["id"]
    assert store.claim("owner") is None
