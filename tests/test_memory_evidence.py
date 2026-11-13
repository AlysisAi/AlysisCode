"""Receipt integrity and integration checks. Scripted replies are not quality evidence."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_agent_loop_failed_turn_history import _Client, _reply, _session

from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages
from alysis_code.llm.openai_compat import LLMError
from alysis_code.local_memory import MemoryStore, memory_turn
from alysis_code.memory_evidence import (
    EVENT,
    GUIDANCE,
    HISTORY_BUDGET,
    HISTORY_KEY,
    INPUT_KEY,
    delivery,
    prepare_request,
    project_history,
    receipt_page,
    session_receipts,
    source,
)


def receipts(session):
    return [e["payload"] for e in session.store.events_snapshot() if e["type"] == EVENT]


def test_actual_request_receipts_survive_deletion_summary_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    store = MemoryStore()
    entry = store.change({"action": "save", "content": "PRIVATE-original-memory-body"})
    client = _Client([_reply("First answer."), _reply("Second answer.")])
    agent = _session(tmp_path, client)
    try:
        # Merely preparing/estimating context must not claim a provider delivery.
        memory_turn(agent).context()
        assert receipts(agent) == []
        assert agent.run_turn("What is currently saved?") == 0
        first = receipts(agent)[0]
        assert first["sources"][0]["records"][0]["id"] == entry["id"]
        assert first["sources"][0]["records"][0]["revision"] == 1
        assert first["turn_id"]
        assert "PRIVATE" not in json.dumps(first)
        store.change(
            {"action": "save", "id": entry["id"], "revision": 1, "content": "PRIVATE-updated-body"}
        )
        assert agent.run_turn("And now?") == 0
        request = client.requests[-1]
        before_answer = next(
            i for i, m in enumerate(request) if m.get("content") == "First answer."
        )
        assert request[before_answer - 1]["content"].startswith(GUIDANCE)
        assert "current_comparison" not in request[before_answer - 1]["content"]
        assert all(not any(k.startswith("_memory") for k in m) for m in request)
        assert "current_version_differs_from_supplied_version" in json.dumps(
            request[before_answer + 1 :]
        )
        assert len(client.requests) == 2  # No extra model call to construct evidence.
        store.change({"action": "delete", "id": entry["id"], "revision": 2})
        compacted = [{"role": "user", "content": "Historical summary: two saved-state questions."}]
        agent.store.append(
            "conversation_summary_updated", {"active_conversation_messages": compacted}
        )
        restored = _load_chat_resume_messages(agent.store.path)
        assert len(restored[0][HISTORY_KEY]) == 2
        assert "PRIVATE" not in json.dumps(restored)
        log_path = agent.store.path
    finally:
        agent.close()

    resumed_client = _Client([_reply("Nothing currently saved."), _reply("Memory is off.")])
    resumed = _session(tmp_path, resumed_client)
    try:
        resumed.messages.extend(_load_chat_resume_messages(log_path))
        assert resumed.run_turn("What is saved now?") == 0
        text = json.dumps(resumed_client.requests[-1])
        assert "Historical memory inputs" in text
        assert "absent_from_current_complete_snapshot" in text
        assert "PRIVATE" not in text
        store.change({"action": "settings", "enabled": False})
        before = len(receipts(resumed))
        assert resumed.run_turn("Can you check memory with it turned off?") == 0
        assert len(receipts(resumed)) == before
        assert entry["id"] not in json.dumps(resumed_client.requests[-1])
        # Clearing conversation history also clears the historical receipt cache.
        resumed.store.append("conversation_cleared", {})
        assert session_receipts(resumed) == []
    finally:
        resumed.close()


def test_repeated_identical_responses_bind_to_their_actual_turns_and_scope():
    old = source("scope-a", [{"id": "record", "revision": 1, "content": "private"}])
    new = source("scope-a", [{"id": "record", "revision": 2, "content": "private-new"}])
    inputs = [
        delivery([old], "Same response", turn_id="one"),
        delivery([new], "Same response", turn_id="two"),
    ]
    messages = [
        {"role": "user", "content": "First question", "_memory_turn_id": "one"},
        {"role": "assistant", "content": "Same response"},
        {"role": "user", "content": "Second question", "_memory_turn_id": "two"},
        {"role": "assistant", "content": "Same response"},
    ]
    original = copy.deepcopy(messages)
    projected = project_history(messages, inputs, scope="scope-a", current=new)
    frames = [m["content"] for m in projected if m.get("_memory_evidence_context")]
    historical = [f for f in frames if f.startswith(GUIDANCE)]
    assert len(historical) == 2
    assert '"revision": 1' in historical[0]
    assert '"revision": 2' in historical[1]
    assert "current_version_differs" in frames[-1]
    assert "same_version" in frames[-1]
    assert all("current_comparison" not in f for f in historical)
    assert "private" not in "".join(frames)
    assert messages == original
    assert project_history(messages, inputs, scope="scope-b") == messages
    partial = source("scope-a", [], complete=False)
    assert "current_membership_unknown" in json.dumps(
        project_history(messages, inputs, scope="scope-a", current=partial)
    )
    assert "absent_from_current" not in json.dumps(
        project_history(messages, inputs, scope="scope-a", current=partial)
    )


def test_failed_provider_attempt_does_not_certify_a_historical_response(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    MemoryStore().change({"action": "save", "content": "Private saved fact"})
    client = _Client([LLMError("connection failed"), _reply("Successful response")])
    agent = _session(tmp_path, client)
    try:
        with pytest.raises(LLMError, match="connection failed"):
            agent.run_turn("What is saved?")
        assert receipts(agent) == []
        restored = _load_chat_resume_messages(agent.store.path)
        assert restored == [{"role": "user", "content": "What is saved?"}]
        assert agent.run_turn("Please try again.") == 0
        assert len(receipts(agent)) == 1
        assert not any(GUIDANCE in str(m.get("content")) for m in client.requests[-1])
    finally:
        agent.close()


def test_stream_compatibility_retry_keeps_the_actual_memory_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    entry = MemoryStore().change({"action": "save", "content": "Private saved fact"})
    client = _Client([LLMError("stream is not supported"), _reply("Successful response")])
    agent = _session(tmp_path, client)
    try:
        assert agent.run_turn("What is saved?") == 0
        assert len(client.requests) == 2
        actual = receipts(agent)
        assert len(actual) == 1
        assert actual[0]["sources"][0]["records"][0]["id"] == entry["id"]
        assert "Private saved fact" in json.dumps(client.requests[-1])
        assert all(not any(k.startswith("_memory") for k in m) for m in client.requests[-1])
    finally:
        agent.close()


def test_receipt_context_is_bounded_and_does_not_certify_unseen_bodies():
    metadata = source("scope", [{"id": str(i), "revision": 1} for i in range(100)])
    saved = [delivery([metadata], str(i)) for i in range(100)]
    messages = project_history([], saved, scope="scope", current=metadata)
    assert sum(len(m["content"]) for m in messages) <= HISTORY_BUDGET
    assert '"body_supplied": false' in json.dumps(messages).replace('\\"', '"')
    assert '"records_omitted": 88' in json.dumps(messages).replace('\\"', '"')
    wire, inputs = prepare_request(
        [{"role": "system", "content": "Current inputs", INPUT_KEY: metadata}, *messages]
    )
    assert inputs == [metadata]
    assert all(not any(k.startswith("_memory") for k in m) for m in wire)


def test_external_memory_owner_does_not_open_local_memory(monkeypatch):
    monkeypatch.setattr(
        "alysis_code.local_memory.MemoryStore",
        lambda: pytest.fail("Local memory must not be opened"),
    )
    turn = memory_turn(
        SimpleNamespace(runtime_kind="interactive_chat", memory_managed_externally=True)
    )
    assert turn.tools == {} and turn.context() == "" and turn.input == {}


def test_memory_tool_delivery_captures_only_new_protocol_results():
    snapshot = source("local:owner:project", [], complete=False)
    messages = [
        {"role": "system", "content": "Partial current snapshot", INPUT_KEY: snapshot},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "read", "function": {"name": "memory_read", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "read",
            "content": json.dumps(
                {
                    "entries": [
                        {
                            "id": "omitted",
                            "revision": 1,
                            "content": "private-body",
                            "updated_at": "then",
                        }
                    ],
                    "project": None,
                }
            ),
        },
    ]
    _, inputs = prepare_request(messages)
    assert inputs[-1]["delivery_kind"] == "tool_result"
    assert inputs[-1]["records"][0]["id"] == "omitted"
    assert "private-body" not in json.dumps(inputs)
    saved = delivery(inputs, "Read result")
    current = source(
        snapshot["scope"], [{"id": "omitted", "revision": 1, "content": "private-body"}]
    )
    projected = project_history([], [saved], scope=snapshot["scope"], current=current)
    assert "same_version" in json.dumps(projected)
    assert "current_version_differs" not in json.dumps(projected)
    _, later = prepare_request(
        [
            *messages,
            {"role": "assistant", "content": "Read result"},
            {"role": "user", "content": "Another request"},
        ]
    )
    assert later == [snapshot]


def test_receipt_stays_beside_its_response_when_provider_hoists_system_messages():
    from alysis_code.llm.openai_compat import _messages_for_transport

    saved = delivery(
        [source("scope", [{"id": "entry", "revision": 1, "content": "private"}])],
        "Historical reply",
        turn_id="turn",
    )
    messages = [
        {"role": "user", "content": "Earlier question", "_memory_turn_id": "turn"},
        {"role": "assistant", "content": "Historical reply"},
        {"role": "user", "content": "Current question"},
        {"role": "system", "content": "Current authoritative context"},
    ]
    wire, _ = prepare_request(project_history(messages, [saved], scope="scope"))
    adapted = _messages_for_transport(wire, provider_key="alysis", model="qwen3.8-2.4t")
    answer = next(i for i, m in enumerate(adapted) if m.get("content") == "Historical reply")
    assert adapted[answer - 1]["content"].startswith(GUIDANCE)
    assert adapted[0] == {"role": "system", "content": "Current authoritative context"}


def test_request_budget_counts_visible_evidence_without_host_bookkeeping():
    from alysis_code.memory_evidence import history_marker
    from alysis_code.request_estimation import estimate_request_tokens, request_message_signatures

    inputs = source("scope", [{"id": str(i), "revision": 1} for i in range(100)])
    saved = delivery([inputs], "Earlier answer", turn_id="one")
    messages = [
        history_marker([saved]),
        {"role": "user", "content": "Current question", "_memory_turn_id": "two"},
        {"role": "system", "content": "Current snapshot", INPUT_KEY: inputs},
        {"role": "user", "content": GUIDANCE, "_memory_evidence_context": True},
    ]
    wire, _ = prepare_request(messages)
    assert estimate_request_tokens(messages, []) == estimate_request_tokens(wire, [])
    assert request_message_signatures(messages) == request_message_signatures(wire)
    assert estimate_request_tokens(messages, []) > estimate_request_tokens(wire[:-1], [])


def test_later_edits_never_rewrite_historical_evidence_or_claim_unknown_is_absent():
    original = source("local:test", [{"id": "record", "revision": 1, "content": "private"}])
    receipt = delivery([original], "Original answer", turn_id="first")
    messages = [
        {"role": "user", "content": "Earlier question", "_memory_turn_id": "first"},
        {"role": "assistant", "content": "Original answer"},
        {"role": "user", "content": "Current question", "_memory_turn_id": "second"},
    ]
    history = []
    for current in (
        original,
        source("local:test", [{"id": "record", "revision": 2, "content": "different"}]),
        source("local:test", []),
        source("local:test", [], complete=False),
    ):
        projected = project_history(messages, [receipt], scope="local:test", current=current)
        history.append(next(m["content"] for m in projected if m["content"].startswith(GUIDANCE)))
        now = projected[-1]
        assert now["content"].startswith("Current memory observation")
        assert projected.index(now) > next(
            i for i, m in enumerate(projected) if m["content"] == "Current question"
        )
        assert "private" not in now["content"] and "different" not in now["content"]
    assert len(set(history)) == 1
    assert "current_membership_unknown" in now["content"]
    assert "absent_from_current_complete_snapshot" not in now["content"]
    assert receipt["sources"] == [original]


def test_receipt_lookup_recovers_omitted_records_and_older_receipts_with_stable_pagination():
    snapshot = source(
        "local:test", [{"id": str(i), "revision": 1, "content": "private"} for i in range(40)]
    )
    saved = [delivery([snapshot], "Answer", turn_id=str(i)) for i in range(30)]
    wire = project_history([], saved, scope="local:test", current=snapshot)
    assert "records_omitted" in json.dumps(wire)
    assert "private" not in json.dumps(wire)
    first = receipt_page(reversed(saved), scope="local:test", receipt_id=saved[0]["id"], limit=7)
    rows = first["records"]
    page = first
    appended = delivery([snapshot], "New answer", turn_id="new")
    while page["next_cursor"]:
        page = receipt_page(
            reversed([*saved, appended]),
            scope="local:test",
            receipt_id=saved[0]["id"],
            limit=7,
            cursor=page["next_cursor"],
        )
        rows.extend(page["records"])
    assert [r["record"]["id"] for r in rows] == [str(i) for i in range(40)]
    assert "private" not in json.dumps(rows)
    assert receipt_page(reversed(saved), scope="foreign")["records"] == []
    with pytest.raises(ValueError, match="changed filters"):
        receipt_page(reversed(saved), scope="foreign", cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="no longer available"):
        receipt_page([], scope="local:test", receipt_id=saved[0]["id"], cursor=first["next_cursor"])


def test_local_receipt_lookup_obeys_memory_disable_and_conversation_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    entry = MemoryStore().change({"action": "save", "content": "private-body"})
    agent = _session(tmp_path, _Client([_reply("Answer")]))
    try:
        assert agent.run_turn("What is saved?") == 0
        history = memory_turn(agent).tools["memory_history"]
        page = history.run({"record_id": entry["id"]})
        assert page["records"][0]["record"]["body_supplied"]
        assert "private-body" not in json.dumps(page)
        MemoryStore().change({"action": "settings", "enabled": False})
        assert "disabled" in history.run({})["error"]
        MemoryStore().change({"action": "settings", "enabled": True})
        agent.store.append("conversation_cleared", {})
        assert history.run({})["records"] == []
    finally:
        agent.close()


@pytest.mark.parametrize("count", [0, 1, 12, 100])
def test_total_visible_evidence_budget_includes_linked_receipts_and_current_observation(count):
    snapshot = source("scope", [{"id": str(i), "revision": 1} for i in range(count)])
    saved = [delivery([snapshot], str(i), turn_id=str(i)) for i in range(30)]
    messages = [
        m
        for i in range(30)
        for m in (
            {"role": "user", "content": "Question", "_memory_turn_id": str(i)},
            {"role": "assistant", "content": str(i)},
        )
    ]
    projected = project_history(messages, saved, scope="scope", current=snapshot)
    assert (
        sum(len(m["content"]) for m in projected if m.get("_memory_evidence_context"))
        <= HISTORY_BUDGET
    )
