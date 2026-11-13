"""Real Bots store/request integration, separate from live quality evaluation."""

import json
from uuid import uuid4

import pytest
import test_bots_memory_changes as memory_cases
from bots_task_state import task_state

from alysis_code.bots.execution import TaskClient
from alysis_code.bots.models import BotInput, MemoryInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, NotFound, encode
from alysis_code.llm.types import LLMResponse
from alysis_code.memory_evidence import GUIDANCE
from alysis_code.memory_sources import ANNOTATION_KEY

case = memory_cases.case


class Client:
    model = "fixture"

    def __init__(self):
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return LLMResponse(content="A response for this turn.", tool_calls=[], raw={})


def test_private_source_annotation_does_not_consume_context_capacity(case):
    store, owner, bot, task = case
    store.save_memory(
        owner, bot["id"], MemoryInput(title="Project", content="Saved project details. " * 60)
    )
    runtime = BotRuntime(store, owner, lambda _: Client())
    context = runtime._context(task)
    without_observation = [
        {k: v for k, v in message.items() if k != ANNOTATION_KEY} for message in context
    ]
    runtime.limits = RuntimeLimits(context_characters=len(encode(without_observation)))
    assert runtime._context(task) == context


def test_foreground_handoff_receipts_are_scoped_and_survive_deleted_bodies(case):
    store, owner, bot, task = case
    saved = store.save_memory(
        owner, bot["id"], MemoryInput(title="Private title", content="private-original-body")
    )
    client = Client()
    runtime = BotRuntime(store, owner, lambda _: client)
    context = runtime._context(task, mark_history=True)
    wire, inputs = runtime._fit_context(task, context, with_memory_inputs=True)
    assert store.memory_deliveries(owner, bot["id"]) == []
    reply = client.chat(messages=wire)
    runtime._record_memory_delivery(task, inputs, reply)
    store.finish(owner, task["id"], "completed", content=reply.content)
    received = store.memory_deliveries(owner, bot["id"])
    assert received[0]["turn_id"] == task["id"]
    record = received[0]["sources"][0]["records"][0]
    assert record["id"] == saved["id"] and record["body_supplied"]
    assert "private-original-body" not in json.dumps(received)
    assert "Private title" not in json.dumps(received)
    store.delete_item(owner, bot["id"], saved["id"], kind="memories")
    store.enqueue(owner, task["chat_id"], TaskInput(message="And now?", request_id=uuid4().hex))
    following = store.claim(owner)
    refreshed = BotRuntime(store, owner, lambda _: client)
    wire = refreshed._fit_context(following, refreshed._context(following, mark_history=True))
    before = next(i for i, m in enumerate(wire) if m.get("content") == reply.content)
    assert wire[before - 1]["content"].startswith(GUIDANCE)
    assert "absent_from_current_complete_snapshot" not in wire[before - 1]["content"]
    assert "absent_from_current_complete_snapshot" in json.dumps(wire[before + 1 :])
    assert "private-original-body" in wire[before - 1]["content"]
    # Not in what the request says of the memory as it is now.
    assert "private-original-body" not in wire[0]["content"] + task_state(wire)
    page = store.memory_delivery_page(owner, bot["id"], record_id=saved["id"])
    assert page["records"][0]["receipt_id"] == received[0]["id"]
    assert page["records"][0]["record"] == record
    assert "private-original-body" not in json.dumps(page)
    original = store.memory_source_read(owner, bot["id"], receipt_id=received[0]["id"])
    assert "private-original-body" in original["content"]
    assert original["mutation_provenance"]["actor"] == "unknown"
    with pytest.raises(NotFound):
        store.memory_source_read("another-owner", bot["id"], receipt_id=received[0]["id"])
    with pytest.raises(NotFound):
        store.memory_delivery_page("another-owner", bot["id"])
    assert all(not any(k.startswith("_memory") for k in m) for m in wire)
    with pytest.raises(NotFound):
        store.memory_deliveries("another-owner", bot["id"])
    other = store.save_bot(owner, BotInput(name="Other", purpose="Isolated"))
    assert store.memory_deliveries(owner, other["id"]) == []
    assert store.memory_delivery_page(owner, other["id"])["records"] == []
    with pytest.raises(NotFound):
        store.memory_source_read(owner, other["id"], receipt_id=received[0]["id"])
    # A later answer can use historical text while its current snapshot is empty.
    # Preserve that separate read link instead of attaching only the empty state.
    audit = [
        *refreshed._context(following),
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "old",
                    "type": "function",
                    "function": {
                        "name": "memory_source_read",
                        "arguments": json.dumps({"receipt_id": received[0]["id"]}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "old", "content": json.dumps(original)},
    ]
    _, read_inputs = refreshed._fit_context(following, audit, with_memory_inputs=True)
    refreshed._record_memory_delivery(following, read_inputs, reply)
    latest = store.memory_deliveries(owner, bot["id"])[-1]
    assert all(not s["records"] for s in latest["sources"])
    reread = store.memory_source_read(owner, bot["id"], receipt_id=latest["id"])
    assert reread["historical_reads"][0]["receipt_id"] == received[0]["id"]
    following["config"]["memory_enabled"] = False
    off = refreshed._fit_context(following, refreshed._context(following))
    assert saved["id"] not in json.dumps(off)
    assert "private-original-body" not in json.dumps(off)
    store.finish(owner, following["id"], "completed", content="Memory checks complete.")
    store.save_bot(
        owner, BotInput(name=bot["name"], purpose=bot["purpose"], memory_enabled=False), bot["id"]
    )
    with pytest.raises(Conflict, match="disabled"):
        store.memory_source_read(owner, bot["id"], receipt_id=received[0]["id"])


def test_execution_adapter_records_actual_refreshed_inputs(case):
    store, owner, bot, task = case
    saved = store.save_memory(owner, bot["id"], MemoryInput(title="Project", content="first-body"))
    client = Client()
    runtime = BotRuntime(store, owner, lambda _: client)
    runtime.background.submit(task, "Continue", runtime._context(task, mark_history=True))
    claimed = runtime.background.claim()
    # UI edit after the handoff must be reflected in the actual worker request.
    updated = store.save_memory(
        owner, bot["id"], MemoryInput(title="Project", content="second-body"), memory_id=saved["id"]
    )
    adapter = TaskClient(runtime, claimed, client, runtime.task_token(task["id"]))
    adapter.chat(messages=[{"role": "user", "content": "Read the project context."}])
    assert len(client.requests) == 1
    assert "second-body" in json.dumps(client.requests[0]["messages"])
    assert all(not any(k.startswith("_memory") for k in m) for m in client.requests[0]["messages"])
    received = store.memory_deliveries(owner, bot["id"])
    assert len(received) == 1
    assert any(
        r.get("updated_at") == updated["updated_at"]
        for s in received[0]["sources"]
        for r in s["records"]
    )
    original = store.memory_source_read(owner, bot["id"], receipt_id=received[0]["id"])
    assert "second-body" in original["content"] and "first-body" not in original["content"]


def test_display_formatting_keeps_receipt_attached_to_its_own_answer(case):
    store, owner, bot, task = case
    saved = store.save_memory(owner, bot["id"], MemoryInput(title="Project", content="private"))
    runtime = BotRuntime(store, owner, lambda _: Client())
    _, inputs = runtime._fit_context(task, runtime._context(task), with_memory_inputs=True)
    response = LLMResponse(
        content="The saved name is Cedar.\n\n<follow_ups>\n- When did it change?\n</follow_ups>",
        tool_calls=[],
        raw={},
    )
    runtime._record_memory_delivery(task, inputs, response)
    store.finish(owner, task["id"], "completed", content="The saved name is Cedar.")
    store.delete_item(owner, bot["id"], saved["id"], kind="memories")
    store.enqueue(
        owner, task["chat_id"], TaskInput(message="Was that saved then?", request_id=uuid4().hex)
    )
    following = store.claim(owner)
    wire = runtime._fit_context(following, runtime._context(following, mark_history=True))
    index = next(i for i, m in enumerate(wire) if m.get("content") == "The saved name is Cedar.")
    receipt = json.loads(wire[index - 1]["content"].removeprefix(GUIDANCE))
    assert receipt["turn_id"] == task["id"]
    assert receipt["sources"][0]["records"][0]["body_supplied"]
    assert "current_comparison" not in json.dumps(receipt)


def test_coding_broker_refreshes_and_records_service_owned_inputs(case, tmp_path):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.coding import WorkspaceRegistration

    store, owner, bot, task = case
    client = Client()
    worker_token = "w" * 40
    app = create_app(
        Settings(
            database=tmp_path / "controller.db",
            token="a" * 40,
            api_key="fixture",
            base_url="https://fixture.invalid",
            model=client.model,
            owner=owner,
            coding_token=worker_token,
        ),
        task_store=store,
        client_factory=lambda _: client,
    )
    runtime = app.state.runtime
    saved = store.save_memory(owner, bot["id"], MemoryInput(title="Project", content="first-body"))
    workspace, worker = uuid4().hex, uuid4().hex
    runtime.coding.register(WorkspaceRegistration(id=workspace, worker_id=worker, name="Project"))
    store.finish(owner, task["id"], "completed", publish_reply=False)
    runtime.coding.bind(bot["id"], workspace)
    store.enqueue(
        owner, task["chat_id"], TaskInput(message="Check current context", request_id=uuid4().hex)
    )
    task = store.claim(owner)
    job = runtime.coding.submit(task, "Check current context", runtime._context(task))
    assert runtime.coding.claim(workspace, worker)["id"] == job["id"]
    updated = store.save_memory(
        owner, bot["id"], MemoryInput(title="Project", content="second-body"), memory_id=saved["id"]
    )
    payload = {
        "worker_id": worker,
        "request_id": uuid4().hex,
        "phase": "execute",
        "messages": [
            {
                "role": "user",
                "content": "What is saved now?",
                "_memory_input": {"scope": "forged"},
                "_memory_source_observation": {
                    "message_digest": "forged",
                    "records": [{"content": "forged-body"}],
                },
            }
        ],
    }
    transport = TestClient(app)
    headers = {"Authorization": "Bearer " + worker_token}
    result = transport.post(
        f"/internal/coding/jobs/{job['id']}/model", headers=headers, json=payload
    )
    assert result.status_code == 200, result.text
    assert "second-body" in json.dumps(client.requests[0]["messages"])
    assert "forged" not in json.dumps(client.requests[0]["messages"])
    recorded = store.memory_deliveries(owner, bot["id"])
    assert recorded[0]["sources"][0]["records"][0]["updated_at"] == updated["updated_at"]
    original = store.memory_source_read(owner, bot["id"], receipt_id=recorded[0]["id"])
    assert "second-body" in original["content"] and "forged-body" not in original["content"]
    store.delete_item(owner, bot["id"], saved["id"], kind="memories")
    cached = transport.post(
        f"/internal/coding/jobs/{job['id']}/model", headers=headers, json=payload
    )
    assert cached.status_code == 200 and cached.json() == result.json()
    assert len(client.requests) == len(store.memory_deliveries(owner, bot["id"])) == 1
    transport.close()


def test_repeated_deliveries_deduplicate_sources_within_task(case):
    from alysis_code.memory_sources import EVENT_PREFIX

    store, owner, bot, task = case
    store.save_memory(owner, bot["id"], MemoryInput(title="Project", content="Original source"))
    client = Client()
    runtime = BotRuntime(store, owner, lambda _: client)
    wire, inputs = runtime._fit_context(task, runtime._context(task), with_memory_inputs=True)
    for _ in range(3):
        runtime._record_memory_delivery(task, inputs, client.chat(messages=wire))
    with store.connect() as db:
        kinds = [r[0] for r in db.execute("SELECT kind FROM events WHERE task_id=?", (task["id"],))]
    assert sum(k.startswith(EVENT_PREFIX) for k in kinds) == 1
    assert len(store.memory_deliveries(owner, bot["id"])) == 3
