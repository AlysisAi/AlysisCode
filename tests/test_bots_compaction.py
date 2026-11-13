"""Deterministic provider fixtures qualify boundaries, not model summary quality."""

import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from test_bots_runtime import ScriptedClient, response

from alysis_code.bots.context_summary import BotContextSummary, current_memory_state
from alysis_code.bots.models import BotInput, HistoryQuery, MemoryInput, NoteInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import NotFound, Store, encode
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.types import ToolCall
from alysis_code.request_estimation import estimate_request_tokens


@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("alice", BotInput(name="Research", purpose="Keep project decisions"))
    chat = store.create_chat("alice", bot["id"], "Continuous chat")
    history = []
    for i in range(12):
        task = store.enqueue(
            "alice",
            chat["id"],
            TaskInput(
                message=f"Project {i}: " + "日本語 Ελληνικά café decision. " * 20,
                request_id=uuid4().hex,
            ),
        )
        store.finish(
            "alice", task["id"], "completed", content="Recorded, not independently verified."
        )
        history.append(task)
    task = store.enqueue(
        "alice",
        chat["id"],
        TaskInput(
            message="Current exact request: do not perform any writes.",
            request_id=uuid4().hex,
        ),
    )
    task = store.claim("alice")
    limits = RuntimeLimits(history_tokens=1500)
    runtime = BotRuntime(store, "alice", lambda _: None, limits=limits)
    source = store.conversation("alice", chat["id"])["messages"][0]
    payload = {
        "items": [
            {
                "kind": "project_context",
                "text": "Project decision; verification remains outstanding.",
                "source_message_ids": [source["id"]],
            }
        ]
    }
    return store, bot, chat, task, history, runtime, payload


def token():
    return EventCancellationToken(threading.Event())


def prepare(case, client):
    store, bot, chat, task, history, runtime, payload = case
    runtime.context_summary.prepare(task, client, runtime.limits, token())
    return runtime.context_summary.read(bot["id"])


def test_summary_persists_sources_and_exact_current_request(case):
    store, bot, chat, task, history, runtime, payload = case
    before = store.conversation("alice", chat["id"])["messages"]
    client = ScriptedClient([response(encode(payload))])
    state = prepare(case, client)
    assert state["revision"] == 1 and state["through_id"]
    assert store.task("alice", task["id"])["calls"] == 1
    assert store.usage("alice")["reported_input_tokens"] == 10
    assert store.conversation("alice", chat["id"])["messages"] == before
    reopened = Store(store.path)
    assert BotContextSummary(reopened, "alice").read(bot["id"])["content"] == encode(payload)
    context = runtime._context(task, mark_history=True)
    assert context[-1]["content"] == task["message"]
    assert any("_bot_summary" in m for m in context)
    wire = runtime._fit_context(task, json.loads(encode(context)))
    assert not any(any(k.startswith("_") for k in m) for m in wire)
    assert payload["items"][0]["text"] in encode(wire)
    cited = payload["items"][0]["source_message_ids"][0]
    assert (
        store.read_message("alice", bot["id"], cited, 0, 16000)["content"] == before[0]["content"]
    )
    assert store.search_history("alice", bot["id"], HistoryQuery(query="日本語"))["messages"]
    with pytest.raises(NotFound):
        BotContextSummary(reopened, "mallory").read(bot["id"])
    other = store.save_bot("alice", BotInput(name="Private", purpose="Separate"))
    assert BotContextSummary(reopened, "alice").read(other["id"])["through_id"] == ""
    # Re-entering the same task, including after process restart, cannot bill a second summary.
    prepare(case, ScriptedClient([]))
    assert store.usage("alice")["calls"] == 1


def test_summary_receives_delivery_provenance_at_its_original_task(case):
    from alysis_code.memory_evidence import delivery, source

    store, bot, chat, task, history, runtime, payload = case
    first = history[0]
    saved = delivery(
        [source("bot:" + bot["id"], [{"id": "entry", "revision": 1, "content": "private-body"}])],
        "Recorded, not independently verified.",
        turn_id=first["id"],
    )
    store.event("alice", first["id"], "memory_delivery", saved)
    client = ScriptedClient([response(encode(payload))])
    state = prepare(case, client)
    assert state["revision"] == 1
    request = client.requests[0]
    body = json.loads(request[1]["content"])
    earlier = next(t for t in body["settled_tasks"] if t["task_id"] == first["id"])
    receipt = earlier["memory_delivery_receipts"][0]
    assert receipt["id"] == saved["id"]
    assert receipt["sources"][0]["records"][0]["body_supplied"]
    assert "private-body" not in encode(body)
    assert "current_comparison" not in encode(receipt)


def test_many_deliveries_do_not_prevent_summary_progress(case):
    from alysis_code.memory_evidence import delivery, source

    store, bot, chat, task, history, runtime, payload = case
    first = history[0]
    supplied = source(
        "bot:" + bot["id"],
        [{"id": str(i), "revision": 1, "content": "private-body"} for i in range(50)],
    )
    for _ in range(30):
        store.event(
            "alice",
            first["id"],
            "memory_delivery",
            delivery([supplied], "Recorded, not independently verified.", turn_id=first["id"]),
        )
    client = ScriptedClient([response(encode(payload))])
    state = prepare(case, client)
    assert state["revision"] == 1
    tasks = json.loads(client.requests[0][1]["content"])["settled_tasks"]
    assert tasks[0]["task_id"] == first["id"]
    assert tasks[0]["memory_delivery_coverage"]["recent_receipts_omitted"] > 0
    assert "private-body" not in encode(tasks)


def test_summary_can_cite_actual_current_note_revision_and_invalidates_on_edit(case):
    store, bot, chat, task, history, runtime, payload = case
    note = store.save_note(
        "alice", bot["id"], NoteInput(title="Pilot correction", content="Pilot postponed.")
    )
    item = payload["items"][0]
    item.update(
        text="Pilot postponed.",
        source_message_ids=[],
        source_note_references=[{"note_id": note["id"], "revision": note["revision"]}],
    )
    state = prepare(case, ScriptedClient([response(encode(payload))]))
    assert state["through_id"] and json.loads(state["content"]) == payload
    assert "source_note_references" in BotContextSummary.message(state)["content"]
    reopened = BotContextSummary(Store(store.path), "alice")
    assert reopened.read(bot["id"])["content"] == state["content"]
    store.save_note(
        "alice",
        bot["id"],
        NoteInput(title=note["title"], content="Pilot scheduled.", expected_revision=1),
        note_id=note["id"],
    )
    assert not reopened.read(bot["id"])["through_id"]


@pytest.mark.parametrize("bad", ["foreign_bot", "wrong_revision", "body_omitted", "inflight_edit"])
def test_summary_rejects_unseen_stale_and_foreign_note_references(case, bad):
    store, bot, chat, task, history, runtime, payload = case
    target = (
        store.save_bot("alice", BotInput(name="Other", purpose="Private"))
        if bad == "foreign_bot"
        else bot
    )
    note = store.save_note(
        "alice", target["id"], NoteInput(title="Current", content="Pilot postponed.")
    )
    if bad == "body_omitted":
        # Keep identity metadata but withhold its body from the model selection.
        original_state = runtime.context_summary._state

        def state_without_body(db, bot_id):
            state = original_state(db, bot_id)
            for selected in state["current_working_notes"]["items"]:
                selected.pop("content", None)
            return state

        runtime.context_summary._state = state_without_body
    payload["items"][0].update(
        text="Pilot postponed.",
        source_message_ids=[],
        source_note_references=[
            {
                "note_id": note["id"],
                "revision": 2 if bad == "wrong_revision" else 1,
            }
        ],
    )
    client = ScriptedClient([response(encode(payload))])
    if bad == "inflight_edit":
        chat_call = client.chat

        def edit_then_reply(**kwargs):
            store.save_note(
                "alice",
                bot["id"],
                NoteInput(title="Current", content="Pilot scheduled.", expected_revision=1),
                note_id=note["id"],
            )
            return chat_call(**kwargs)

        client.chat = edit_then_reply
    state = prepare(case, client)
    assert not state["through_id"] and state["revision"] == 0
    assert store.usage("alice")["calls"] == 1


@pytest.mark.parametrize("change", ["edit", "delete", "disable"])
def test_changed_memory_invalidates_persisted_and_inflight_summary(case, change):
    store, bot, chat, task, history, runtime, payload = case
    memory = store.save_memory("alice", bot["id"], MemoryInput(title="Preference", content="Old"))
    prepare(case, ScriptedClient([response(encode(payload))]))
    prior = encode(runtime._memory_records(task))
    messages = runtime._context(task, mark_history=True)
    assert any("_bot_summary" in m for m in messages)
    if change == "edit":
        store.save_memory(
            "alice",
            bot["id"],
            MemoryInput(title="Preference", content="New"),
            memory_id=memory["id"],
        )
    elif change == "delete":
        store.delete_item("alice", bot["id"], memory["id"], kind="memories")
    else:
        store.finish("alice", task["id"], "completed", content="Stopped before setup change")
        store.save_bot(
            "alice",
            BotInput(name="Research", purpose="Keep decisions", memory_enabled=False),
            bot["id"],
        )
    assert runtime.context_summary.read(bot["id"])["through_id"] == ""
    assert payload["items"][0]["text"] not in encode(runtime._context(task))
    if change != "disable":
        _, changed = runtime._refresh_memory_context(task, messages, prior)
        assert changed and not any("_bot_summary" in m for m in messages)


def test_summary_inflight_memory_change_is_charged_but_discarded(case):
    store, bot, chat, task, history, runtime, payload = case

    class Change(ScriptedClient):
        def chat(self, **kwargs):
            store.save_memory("alice", bot["id"], MemoryInput(title="New fact", content="Current"))
            return super().chat(**kwargs)

    state = prepare(case, Change([response(encode(payload))]))
    assert not state["through_id"]
    assert store.usage("alice")["calls"] == 1
    assert any(
        e["kind"] == "context_compaction_discarded"
        for e in store.task("alice", task["id"])["events"]
    )


@pytest.mark.parametrize("decision", ["approve", "edit", "delete", "reject"])
def test_recompaction_receives_current_memory_state_not_historical_review_claims(case, decision):
    store, bot, chat, task, history, runtime, payload = case
    memory = store.save_memory(
        "alice",
        bot["id"],
        MemoryInput(title="Format", content="Old preference 東京"),
        source_task_id=task["id"],
    )
    first = ScriptedClient([response(encode(payload))])
    prepare(case, first)
    assert json.loads(first.requests[0][1]["content"])["current_memory_state"] == {
        "approved_memories": [],
        "approved_count": 0,
        "index_complete": True,
        "omitted_count": 0,
        "pending_count": 1,
    }
    store.finish("alice", task["id"], "completed", content="Your preference is pending review.")
    if decision == "edit":
        store.save_memory(
            "alice",
            bot["id"],
            MemoryInput(title="Format", content="New preference Αθήνα"),
            memory_id=memory["id"],
        )
    elif decision == "delete":
        store.delete_item("alice", bot["id"], memory["id"], kind="memories")
    else:
        store.decide_memory(
            "alice", bot["id"], memory["id"], "active" if decision == "approve" else "rejected"
        )
    assert not runtime.context_summary.read(bot["id"])["through_id"]
    store.enqueue("alice", chat["id"], TaskInput(message="What is saved?", request_id=uuid4().hex))
    newer = store.claim("alice")
    client = ScriptedClient([response(encode(payload))])
    runtime.context_summary.prepare(newer, client, runtime.limits, token())
    request = json.loads(client.requests[0][1]["content"])
    snapshot = request["current_memory_state"]
    assert request["previous_summary"] == {"items": []}
    assert snapshot["pending_count"] == 0
    if decision in {"delete", "reject"}:
        assert snapshot["approved_memories"] == []
    else:
        assert snapshot["approved_memories"][0]["id"] == memory["id"]
        assert snapshot["approved_memories"][0]["content"] == (
            "New preference Αθήνα" if decision == "edit" else "Old preference 東京"
        )


def test_policy_upgrade_retires_old_persisted_summary_and_background_checkpoint(case, monkeypatch):
    from alysis_code.bots import context_summary

    store, bot, chat, task, history, runtime, payload = case
    prepare(case, ScriptedClient([response(encode(payload))]))
    checkpoint = runtime._context(task, mark_history=True)
    assert any("_bot_summary" in m for m in checkpoint)
    monkeypatch.setattr(
        context_summary, "SUMMARY_POLICY_VERSION", context_summary.SUMMARY_POLICY_VERSION + 1
    )
    state = runtime.context_summary.read(bot["id"])
    assert not state["through_id"] and state["content"] == '{"items":[]}'
    runtime._refresh_memory_context(task, checkpoint, None)
    assert not any("_bot_summary" in m for m in checkpoint)


def test_memory_snapshot_bounds_identity_index_without_pending_or_retired_bodies(case):
    store, bot, chat, task, history, runtime, payload = case
    active = store.save_memory(
        "alice", bot["id"], MemoryInput(title="Active", content="Current body")
    )
    pending = store.save_memory(
        "alice",
        bot["id"],
        MemoryInput(title="Pending", content="Unapproved body"),
        source_task_id=task["id"],
    )
    state = current_memory_state(store.list_memories("alice", bot["id"]), content_budget=0)
    assert state["pending_count"] == 1
    assert state["approved_memories"] == []
    assert state["approved_count"] == 1
    assert not state["index_complete"] and state["omitted_count"] == 1
    assert "Current body" not in encode(state) and "Unapproved body" not in encode(state)
    store.decide_memory("alice", bot["id"], pending["id"], "rejected")
    store.delete_item("alice", bot["id"], active["id"], kind="memories")
    assert current_memory_state(store.list_memories("alice", bot["id"])) == {
        "approved_memories": [],
        "approved_count": 0,
        "index_complete": True,
        "omitted_count": 0,
        "pending_count": 0,
    }


@pytest.mark.parametrize(
    "bad",
    [
        "invalid",
        "foreign_source",
        "tool",
        "oversized",
        "total_tokens",
        "unclassified",
        "empty_project_citations",
    ],
)
def test_invalid_summaries_do_not_advance_or_execute_tools(case, bad):
    store, bot, chat, task, history, runtime, payload = case
    if bad == "foreign_source":
        payload["items"][0]["source_message_ids"] = [uuid4().hex]
    if bad == "empty_project_citations":
        payload["items"][0]["source_message_ids"] = []
    if bad == "oversized":
        payload["items"][0]["text"] = "α" * 1400
    if bad == "total_tokens":
        payload["items"][0]["text"] = "α" * 1000
        payload["items"] *= 6
    if bad == "unclassified":
        del payload["items"][0]["kind"]
    reply = response(
        "not JSON" if bad == "invalid" else encode(payload),
        [ToolCall("write", "save_file", {"name": "bad.txt", "content": "bad"})]
        if bad == "tool"
        else [],
    )
    state = prepare(case, ScriptedClient([reply]))
    assert not state["through_id"] and state["revision"] == 0
    assert store.list_files("alice", bot["id"]) == []
    assert store.usage("alice")["calls"] == 1
    assert any(
        e["kind"] == "context_compaction_failed" for e in store.task("alice", task["id"])["events"]
    )


def test_unknown_attempt_is_charged_and_not_replayed_on_next_task(case):
    store, bot, chat, task, history, runtime, payload = case

    class Failure(ScriptedClient):
        def chat(self, **kwargs):
            raise RuntimeError("SECRET provider detail")

    prepare(case, Failure([]))
    assert store.usage("alice")["unconfirmed_calls"] == 1
    assert "SECRET" not in encode(store.task("alice", task["id"])["events"])
    store.finish("alice", task["id"], "completed", content="Answered with history retrieval.")
    store.enqueue("alice", chat["id"], TaskInput(message="Next", request_id=uuid4().hex))
    newer = store.claim("alice")
    runtime.context_summary.prepare(newer, ScriptedClient([]), runtime.limits, token())
    assert store.usage("alice")["calls"] == 1
    # A later explicit user task can recover after the persisted cooldown.
    with store.connect(write=True) as db:
        db.execute("UPDATE bot_context_summaries SET updated_at=updated_at-301")
    runtime.context_summary.prepare(
        newer, ScriptedClient([response(encode(payload))]), runtime.limits, token()
    )
    assert store.usage("alice")["calls"] == 2
    assert runtime.context_summary.read(bot["id"])["revision"] == 1


@pytest.mark.parametrize("citations", [[], ["memory-record-not-a-message"]])
def test_personal_memory_items_cannot_be_persisted_as_project_summary(case, citations):
    store, bot, chat, task, history, runtime, payload = case
    response_payload = {
        "items": [
            *payload["items"],
            {
                "kind": "personal_memory",
                "text": "Αναλυτικά / 詳細な説明 — pending approval",
                "source_message_ids": citations,
            },
        ]
    }
    state = prepare(case, ScriptedClient([response(encode(response_payload))]))
    assert state["revision"] == 1 and state["through_id"]
    assert state["content"] == encode(payload)
    assert "pending approval" not in encode(runtime._context(task))


def test_memory_only_summary_advances_cursor_without_retaining_personal_content(case):
    store, bot, chat, task, history, runtime, payload = case
    payload["items"][0]["kind"] = "personal_memory"
    state = prepare(case, ScriptedClient([response(encode(payload))]))
    assert state["revision"] == 1 and state["through_id"]
    assert state["content"] == '{"items":[]}'


@pytest.mark.parametrize("wrapper", ["```json\n{}\n```", "```\n{}\n```", "  ```JSON\n{}\n```  "])
def test_complete_json_code_fence_is_formatting_not_a_failed_summary(case, wrapper):
    store, bot, chat, task, history, runtime, payload = case
    state = prepare(case, ScriptedClient([response(wrapper.format(encode(payload)))]))
    assert state["revision"] == 1
    assert state["content"] == encode(payload)


@pytest.mark.parametrize(
    "wrapper",
    ["prose\n```json\n{}\n```", "```json\n{}\n```\nprose", "```json\n{}\n```\n```json\n{{}}\n```"],
)
def test_fence_support_does_not_silently_ignore_extra_prose_or_multiple_outputs(case, wrapper):
    store, bot, chat, task, history, runtime, payload = case
    state = prepare(case, ScriptedClient([response(wrapper.format(encode(payload)))]))
    assert state["revision"] == 0 and not state["through_id"]


def test_background_checkpoint_retains_valid_summary_but_rejects_stale_generation(case):
    store, bot, chat, task, history, runtime, payload = case
    prepare(case, ScriptedClient([response(encode(payload))]))
    checkpoint = json.loads(encode(runtime._context(task, mark_history=True)))
    memory_state, changed = runtime._refresh_memory_context(task, checkpoint, None)
    assert changed and any("_bot_summary" in m for m in checkpoint)
    assert payload["items"][0]["text"] in encode(runtime._fit_context(task, checkpoint))
    store.save_memory("alice", bot["id"], MemoryInput(title="Current", content="Corrected"))
    _, changed = runtime._refresh_memory_context(task, checkpoint, memory_state)
    assert changed and not any("_bot_summary" in m for m in checkpoint)


def test_running_history_is_a_cursor_barrier_and_recent_turns_stay_unsummarized(case):
    store, bot, chat, task, history, runtime, payload = case
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (history[0]["id"],))
    assert not prepare(case, ScriptedClient([]))["through_id"]
    assert store.usage("alice")["calls"] == 0
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE tasks SET status='interrupted',error='Unknown write outcome' WHERE id=?",
            (history[0]["id"],),
        )
    client = ScriptedClient([response(encode(payload))])
    prepare(case, client)
    sources = json.loads(client.requests[0][1]["content"])["settled_tasks"]
    assert sources[0]["status"] == "interrupted"
    assert sources[0]["error"] == "Unknown write outcome"
    assert not {t["id"] for t in history[-4:]} & {r["task_id"] for r in sources}


def test_compaction_shares_task_call_allowance_and_tools_remain_exact(case):
    store, bot, chat, task, history, runtime, payload = case
    runtime.limits = replace(runtime.limits, calls_per_task=2)
    client = ScriptedClient([response(encode(payload)), response("Here is the current answer.")])
    runtime.client_factory = lambda _: client
    runtime.run(task, token())
    final = store.task("alice", task["id"])
    assert final["status"] == "completed" and final["calls"] == 2
    context = runtime._context(task, mark_history=True)
    context.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "r",
                        "type": "function",
                        "function": {"name": "read_message", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "r", "content": "Exact receipt " + "Ω" * 1000},
        ]
    )
    schemas = [
        {"type": "function", "function": {"name": "read_message", "description": "D" * 5000}}
    ]
    runtime.limits = replace(
        runtime.limits, context_tokens=estimate_request_tokens(context, schemas) - 100
    )
    wire = runtime._fit_context(task, context, schemas)
    exchange = [m for m in wire if m["role"] != "system"]
    assert exchange[-2]["tool_calls"][0]["id"] == exchange[-1]["tool_call_id"] == "r"
    assert exchange[-1]["content"] == "Exact receipt " + "Ω" * 1000
    assert estimate_request_tokens(wire, schemas) <= runtime.limits.context_tokens


def test_cancelled_compaction_does_not_commit(case):
    store, bot, chat, task, history, runtime, payload = case
    cancel = threading.Event()

    class Cancel(ScriptedClient):
        def chat(self, **kwargs):
            cancel.set()
            return super().chat(**kwargs)

    with pytest.raises(CooperativeCancellationError):
        runtime.context_summary.prepare(
            task,
            Cancel([response(encode(payload))]),
            runtime.limits,
            EventCancellationToken(cancel),
        )
    assert runtime.context_summary.read(bot["id"])["through_id"] == ""
    assert store.usage("alice")["calls"] == 1


def test_compaction_leaves_one_service_call_and_memory_off_spends_none(case):
    store, bot, chat, task, history, runtime, payload = case
    runtime.context_summary.prepare(
        task, ScriptedClient([]), replace(runtime.limits, max_calls=1), token()
    )
    assert store.usage("alice")["calls"] == 0
    task["config"]["memory_enabled"] = False
    prepare(case, ScriptedClient([]))
    assert store.usage("alice")["calls"] == 0


def test_incremental_failure_keeps_previous_summary_and_cursor(case):
    store, bot, chat, task, history, runtime, payload = case
    first = prepare(case, ScriptedClient([response(encode(payload))]))
    store.finish("alice", task["id"], "completed", content="First result")
    store.enqueue("alice", chat["id"], TaskInput(message="Continue", request_id=uuid4().hex))
    newer = store.claim("alice")
    client = ScriptedClient([response("Malformed summary")])
    runtime.context_summary.prepare(newer, client, runtime.limits, token())
    second = runtime.context_summary.read(bot["id"])
    assert second["revision"] == first["revision"]
    assert second["through_id"] == first["through_id"]
    assert second["content"] == first["content"]
    request = json.loads(client.requests[0][1]["content"])
    assert request["previous_summary"] == payload
    assert first["through_id"] not in {t["task_id"] for t in request["settled_tasks"]}
    assert payload["items"][0]["text"] in encode(runtime._context(newer))


def test_multiple_valid_citations_advance_incremental_summary_without_truncation(case):
    store, bot, chat, task, history, runtime, payload = case
    first = prepare(case, ScriptedClient([response(encode(payload))]))
    store.finish("alice", task["id"], "completed", content="First result")
    store.enqueue("alice", chat["id"], TaskInput(message="Continue", request_id=uuid4().hex))
    newer = store.claim("alice")
    runtime.limits = replace(runtime.limits, history_tokens=3000)
    records, through = runtime.context_summary._candidate(newer, first, 3000)
    ids = [m["id"] for r in records for m in r["messages"]]
    assert len(ids) >= 5
    citations = [*payload["items"][0]["source_message_ids"], *ids[:5]]
    payload["items"][0]["source_message_ids"] = citations
    runtime.context_summary.prepare(
        newer, ScriptedClient([response(encode(payload))]), runtime.limits, token()
    )
    second = runtime.context_summary.read(bot["id"])
    assert second["revision"] == first["revision"] + 1
    assert second["through_id"] == through["id"]
    assert json.loads(second["content"])["items"][0]["source_message_ids"] == citations


def test_memory_edit_during_answer_removes_summary_and_discards_stale_action(case):
    store, bot, chat, task, history, runtime, payload = case
    memory = store.save_memory("alice", bot["id"], MemoryInput(title="Marker", content="Old"))

    class Change(ScriptedClient):
        def chat(self, **kwargs):
            if len(self.requests) == 1:
                store.save_memory(
                    "alice",
                    bot["id"],
                    MemoryInput(title="Marker", content="New"),
                    memory_id=memory["id"],
                )
            return super().chat(**kwargs)

    client = Change(
        [
            response(encode(payload)),
            response(
                "Stale", [ToolCall("stale", "save_file", {"name": "stale.txt", "content": "Old"})]
            ),
            response("New"),
        ]
    )
    runtime.client_factory = lambda _: client
    runtime.run(task, token())
    assert store.task("alice", task["id"])["status"] == "completed"
    assert store.task("alice", task["id"])["calls"] == 3
    assert store.list_files("alice", bot["id"]) == []
    assert payload["items"][0]["text"] in encode(client.requests[1])
    assert payload["items"][0]["text"] not in encode(client.requests[2])


def test_short_conversation_has_no_summary_call(tmp_path):
    store = Store(tmp_path / "small.db")
    bot = store.save_bot("alice", BotInput(name="Small", purpose="Help"))
    chat = store.create_chat("alice", bot["id"], "Chat")
    store.enqueue("alice", chat["id"], TaskInput(message="Hello", request_id=uuid4().hex))
    task = store.claim("alice")
    client = ScriptedClient([response("Hello")])
    runtime = BotRuntime(store, "alice", lambda _: client)
    runtime.run(task, token())
    assert store.usage("alice")["calls"] == 1
    assert runtime.context_summary.read(bot["id"])["through_id"] == ""


def test_summary_input_contains_original_observation_for_its_historical_task(case):
    from alysis_code.memory_evidence import INPUT_KEY, delivery, prepare_request, source
    from alysis_code.memory_sources import ANNOTATION_KEY, annotate

    store, bot, chat, task, history, runtime, payload = case
    records = [{"id": "record", "content": "Historical project source text."}]
    content = encode(records)
    _, inputs = prepare_request(
        [
            {
                "role": "system",
                "content": content,
                INPUT_KEY: source("bot:" + bot["id"], records),
                ANNOTATION_KEY: annotate(content, records),
            }
        ]
    )
    receipt = delivery(inputs, "Recorded, not independently verified.", turn_id=history[0]["id"])
    store.record_memory_delivery("alice", history[0]["id"], inputs, receipt)
    candidate = runtime.context_summary._candidate(
        task,
        runtime.context_summary.read(bot["id"]),
        runtime.limits.history_tokens,
    )
    assert candidate is not None
    archived_tasks, _ = candidate
    evidence = archived_tasks[0]["memory_delivery_receipts"]
    assert evidence[0]["turn_id"] == history[0]["id"]
    assert "Historical project source text." in encode(evidence)
