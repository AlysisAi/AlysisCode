"""Source preservation checks; live model quality is evaluated separately."""

import json
from types import SimpleNamespace

import pytest
from test_agent_loop_failed_turn_history import _Client, _reply, _session

from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.local_memory import MemoryStore, memory_turn
from alysis_code.memory_evidence import (
    GUIDANCE,
    INPUT_KEY,
    delivery,
    prepare_request,
    project_history,
    source,
)
from alysis_code.memory_sources import (
    ANNOTATION_KEY,
    EVENT_PREFIX,
    annotate,
    capture,
    read_local,
    read_receipt,
)
from alysis_code.session_store import SessionStore


def test_historical_reads_keep_original_receipt_links_across_later_tool_steps(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    memory = MemoryStore()
    saved = memory.change({"action": "save", "content": "Original source wording."})
    client = _Client([_reply("First answer")])
    agent = _session(tmp_path, client)
    try:
        agent.run_turn("Read what is saved.")
        original = next(
            e["payload"] for e in agent.store.events_snapshot() if e["type"] == "memory_delivery"
        )
        memory.change({"action": "delete", "id": saved["id"], "revision": 1})
        client.responses = iter(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall("old", "memory_source_read", {"receipt_id": original["id"]})
                    ],
                    raw={},
                ),
                LLMResponse(content="", tool_calls=[ToolCall("now", "memory_read", {})], raw={}),
                _reply("Verified the original text."),
            ]
        )
        assert agent.run_turn("Check the old text and what is saved now.") == 0
        recent = [
            e["payload"] for e in agent.store.events_snapshot() if e["type"] == "memory_delivery"
        ][-1]
        assert all(not s["records"] for s in recent["sources"])
        assert recent["historical_reads"][0]["receipt_id"] == original["id"]
        assert "Original source wording." not in json.dumps(recent)
        reader = memory_turn(agent).tools["memory_source_read"]
        read = reader.run({"receipt_id": recent["id"]})
        link = read["historical_reads"][0]
        assert link["content_range"]["start"] == 0
        assert (
            "Original source wording." in reader.run({"receipt_id": link["receipt_id"]})["content"]
        )
        projected = project_history(agent.messages, [recent], scope=recent["sources"][0]["scope"])
        at = next(
            i for i, m in enumerate(projected) if m.get("content") == "Verified the original text."
        )
        assert original["id"] in projected[at - 1]["content"]
    finally:
        agent.close()


def test_original_automatic_source_remains_readable_after_edit_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    memory = MemoryStore()
    saved = memory.change({"action": "save", "content": "The tracker is called Maple."})
    agent = _session(tmp_path, _Client([_reply("Maple"), _reply("Harbor")]))
    try:
        assert agent.run_turn("What is the saved name?") == 0
        memory.change(
            {
                "action": "save",
                "id": saved["id"],
                "revision": 1,
                "content": "The tracker is called Harbor.",
            }
        )
        assert agent.run_turn("What is saved now?") == 0
        memory.change({"action": "delete", "id": saved["id"], "revision": 2})
        receipts = [
            e["payload"] for e in agent.store.events_snapshot() if e["type"] == "memory_delivery"
        ]
        reader = memory_turn(agent).tools["memory_source_read"]
        first = reader.run({"receipt_id": receipts[0]["id"]})
        second = reader.run({"receipt_id": receipts[1]["id"]})
        assert "The tracker is called Maple." in first["content"]
        assert "The tracker is called Harbor." not in first["content"]
        assert "The tracker is called Harbor." in second["content"]
        assert memory.snapshot()["total"] == 0
        assert "Maple" not in json.dumps(receipts)
        assert first["mutation_provenance"]["actor"] == "unknown"
        agent.store.append("conversation_cleared", {})
        assert "error" in reader.run({"receipt_id": receipts[0]["id"]})
    finally:
        agent.close()


def test_followup_preserves_original_observations_beside_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    memory = MemoryStore()
    saved = memory.change({"action": "save", "content": "Original observed wording."})
    client = _Client([_reply("First answer"), _reply("Second answer"), _reply("Current answer")])
    agent = _session(tmp_path, client)
    try:
        agent.run_turn("Check the saved entry.")
        memory.change(
            {
                "action": "save",
                "id": saved["id"],
                "revision": 1,
                "content": "Updated observed wording.",
            }
        )
        agent.run_turn("Check it again.")
        memory.change({"action": "delete", "id": saved["id"], "revision": 2})
        agent.run_turn("What is saved now?")
        messages = client.requests[-1]
        for answer, original in [
            ("First answer", "Original observed wording."),
            ("Second answer", "Updated observed wording."),
        ]:
            at = next(i for i, m in enumerate(messages) if m.get("content") == answer)
            assert original in messages[at - 1]["content"]
            assert "absent_from_current" not in messages[at - 1]["content"]
        assert memory.snapshot()["total"] == 0
    finally:
        agent.close()


@pytest.mark.parametrize("tool_turn", ["first", "second", "both"])
def test_repeated_inline_reads_do_not_crowd_out_distinct_source_versions(
    tmp_path, monkeypatch, tool_turn
):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    memory = MemoryStore()
    saved = memory.change({"action": "save", "content": "First observed version."})
    client = _Client(
        [
            *(
                LLMResponse(content="", tool_calls=[ToolCall(str(i), "memory_read", {})], raw={})
                for i in range(4 if tool_turn in {"first", "both"} else 0)
            ),
            _reply("First answer"),
            *(
                LLMResponse(content="", tool_calls=[ToolCall(str(i), "memory_read", {})], raw={})
                for i in range(4 if tool_turn in {"second", "both"} else 0)
            ),
            _reply("Updated answer"),
            *(_reply("Current memory is empty") for _ in range(5)),
        ]
    )
    agent = _session(tmp_path, client)
    agent.max_steps = 8
    try:
        agent.run_turn("Read the entry and check it.")
        memory.change(
            {
                "action": "save",
                "id": saved["id"],
                "revision": 1,
                "content": "Second observed version.",
            }
        )
        agent.run_turn("What does it say now?")
        memory.change({"action": "delete", "id": saved["id"], "revision": 2})
        for i in range(5):
            agent.run_turn(f"Check the current state again, please ({i + 1}).")
        latest = json.dumps(client.requests[-1])
        assert "First observed version." in latest
        assert "Second observed version." in latest
        # Summarized replies still have original observations via the ledger.
        from alysis_code.local_memory import read_source_context
        from alysis_code.memory_evidence import session_receipts

        current = memory_turn(agent)
        current.context()
        projected = project_history(
            [{"role": "user", "content": "Older turns were summarized."}],
            session_receipts(agent),
            scope=current.input["scope"],
            current=current.input,
            source_reader=lambda **args: read_source_context(agent, **args),
        )
        assert "First observed version." in json.dumps(projected)
        assert "Second observed version." in json.dumps(projected)
    finally:
        agent.close()


def test_deduplication_restart_scope_and_disabled_access(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    memory = MemoryStore()
    memory.change({"action": "save", "content": "Keep explanations short."})
    agent = _session(tmp_path, _Client([_reply("First"), _reply("Second")]))
    try:
        assert agent.run_turn("What is saved?") == agent.run_turn("And now?") == 0
        events = agent.store.events_snapshot()
        assert len([e for e in events if e["type"].startswith(EVENT_PREFIX)]) == 1
        receipt = next(e["payload"] for e in events if e["type"] == "memory_delivery")
        identity = agent.store.session_id
    finally:
        agent.close()
    # A new SessionStore reads the persisted log, not the old in-process cache.
    resumed = SimpleNamespace(
        store=SessionStore(
            enabled=True,
            sessions_dir=tmp_path / "sessions",
            session_id=identity,
            cwd=str(tmp_path),
            repo_root=None,
        ),
        runtime_kind="interactive_chat",
        mode="readonly",
    )
    reader = memory_turn(resumed).tools["memory_source_read"]
    assert "Keep explanations short." in reader.run({"receipt_id": receipt["id"]})["content"]
    with pytest.raises(ValueError, match="scope"):
        read_local(resumed, scope="another-owner", receipt_id=receipt["id"])
    memory.change({"action": "settings", "enabled": False})
    assert "disabled" in reader.run({"receipt_id": receipt["id"]})["error"]


def test_bound_annotation_only_archives_surviving_preview_and_preserves_fragments():
    records = [
        {
            "id": "one",
            "revision": 2,
            "content": "δείγμα",
            "content_range": {"start": 10, "end": 16, "total": 30},
        }
    ]
    metadata = source("local:scope", records, complete=False)
    content = json.dumps(records, ensure_ascii=False)
    message = {
        "role": "system",
        "content": content,
        INPUT_KEY: metadata,
        ANNOTATION_KEY: annotate(content, records),
    }
    wire, inputs = prepare_request([message])
    assert wire == [{"role": "system", "content": content}]
    assert "δείγμα" not in json.dumps(inputs, ensure_ascii=False)
    receipt = delivery(inputs, "Observed", turn_id="turn")
    result = read_receipt(receipt, scope="local:scope", load=inputs.observations.get)
    observation = json.loads(result["content"])[0]["original_observation"]
    assert observation["records"][0]["content"] == "δείγμα"
    assert observation["records"][0]["body_supplied"] is False
    assert observation["records"][0]["content_range"] == records[0]["content_range"]
    assert receipt["observed_at"] <= receipt["received_at"]
    # Context fitting cannot retain a source annotation for a replaced body.
    _, changed = prepare_request([{**message, "content": "trimmed away"}])
    assert changed == [] and not changed.observations
    _, removed = prepare_request([])
    assert removed == [] and not removed.observations


def test_source_paging_legacy_receipts_and_corrupt_sources():
    record = {"id": "one", "content": 'Greek Ελληνικά, emoji 🪴, quotes "\\". ' * 800}
    content = json.dumps(record)
    _, inputs = prepare_request(
        [
            {
                "role": "system",
                "content": content,
                INPUT_KEY: source("scope", [record]),
                ANNOTATION_KEY: annotate(content, [record]),
            }
        ]
    )
    receipt = delivery(inputs, "response")
    parts, offset = [], 0
    while True:
        page = read_receipt(
            receipt, scope="scope", load=inputs.observations.get, offset=offset, max_chars=197
        )
        parts.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert (
        json.loads("".join(parts))[0]["original_observation"]["records"][0]["content"]
        == record["content"]
    )
    legacy = delivery([source("scope", [record])], "older answer")
    assert (
        json.loads(read_receipt(legacy, scope="scope", load=lambda _: None)["content"])[0]["status"]
        == "unavailable"
    )

    corrupted = dict(next(iter(inputs.observations.values())), records=[])
    assert (
        json.loads(read_receipt(receipt, scope="scope", load=lambda _: corrupted)["content"])[0][
            "status"
        ]
        == "unavailable"
    )


def test_inline_original_source_is_bounded_and_continues_without_losing_text():
    from alysis_code.memory_sources import with_original_source

    record = {"id": "entry", "content": 'Ελληνικά 🪴 "quoted" \\ text. ' * 800}
    content = json.dumps(record)
    _, inputs = prepare_request(
        [
            {
                "role": "system",
                "content": content,
                INPUT_KEY: source("scope", [record]),
                ANNOTATION_KEY: annotate(content, [record]),
            }
        ]
    )
    receipt = delivery(inputs, "answer", turn_id="first")

    def reader(**args):
        return read_receipt(
            receipt,
            scope="scope",
            load=inputs.observations.get,
            **{k: v for k, v in args.items() if k != "receipt_id"},
        )

    enriched = with_original_source(receipt, reader, max_chars=1000)
    assert len(json.dumps(enriched)) - len(json.dumps(receipt)) <= 1000
    page = enriched["original_source"]
    assert page["next_offset"] == page["content_range"]["end"] < page["content_range"]["total"]
    parts = [page["content"]]
    offset = page["next_offset"]
    while offset is not None:
        page = reader(offset=offset)
        parts.append(page["content"])
        offset = page["next_offset"]
    assert json.loads("".join(parts))[0]["original_observation"]["records"][0] == {
        "id": "entry",
        "body_supplied": True,
        "content": record["content"],
    }
    assert with_original_source(receipt, lambda **args: {}, max_chars=1000) == receipt


def test_manual_compactor_receives_sources_and_respects_memory_disable(tmp_path, monkeypatch):
    from test_conversation_compaction import _make_cfg

    from alysis_code.agent.session import create_session

    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    memory = MemoryStore()
    memory.change({"action": "save", "content": "Original compactor observation."})
    client = _Client([_reply("The first answer")])
    agent = create_session(
        cfg=_make_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        client_override=client,
        compactor_client_override=client,
        hooks_enabled=False,
        session_log_dir_override=tmp_path / "sessions",
    )
    try:
        agent.run_turn("Check the saved entry.")
        compactor = agent.conversation_compactor
        assert compactor is not None
        prepared = compactor._prepare_chunk_messages_for_compactor(agent.messages, 20000)
        assert "Original compactor observation." in json.dumps(prepared)
        memory.change({"action": "settings", "enabled": False})
        prepared = compactor._prepare_chunk_messages_for_compactor(agent.messages, 20000)
        assert "Original compactor observation." not in json.dumps(prepared)
    finally:
        agent.close()


def test_failed_attempt_does_not_archive_source_bodies(tmp_path, monkeypatch):
    from alysis_code.llm.openai_compat import LLMError

    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    MemoryStore().change({"action": "save", "content": "Source that failed to arrive."})
    agent = _session(tmp_path, _Client([LLMError("connect failed")]))
    try:
        with pytest.raises(LLMError):
            agent.run_turn("What is saved?")
        assert not any(e["type"].startswith(EVENT_PREFIX) for e in agent.store.events_snapshot())
    finally:
        agent.close()


def test_duplicate_step_receipts_do_not_crowd_out_sources_for_retained_answers():
    receipts, messages = [], []
    for turn, value, steps in [("one", "first", 1), ("two", "second", 7), ("three", "empty", 2)]:
        rows = [] if value == "empty" else [{"id": "record", "revision": turn, "content": value}]
        content = json.dumps(rows)
        _, inputs = prepare_request(
            [
                {
                    "role": "system",
                    "content": content,
                    INPUT_KEY: source("scope", rows),
                    ANNOTATION_KEY: annotate(content, rows),
                }
            ]
        )
        messages.extend(
            [
                {"role": "user", "content": "Question", "_memory_turn_id": turn},
                {"role": "assistant", "content": value},
            ]
        )
        for _ in range(steps - 1):
            receipts.append(delivery(inputs, "", turn_id=turn))
        receipts.append(delivery(inputs, value, turn_id=turn))
    messages.append({"role": "user", "content": "Were the earlier values really saved?"})
    projected = project_history(messages, receipts, scope="scope")
    for value in ("first", "second", "empty"):
        at = next(
            i
            for i, m in enumerate(projected)
            if m.get("role") == "assistant" and m.get("content") == value
        )
        assert projected[at - 1]["content"].startswith(GUIDANCE)
        assert "source_ref" in projected[at - 1]["content"]


@pytest.mark.parametrize("additional_tool_read", [False, True])
def test_repeated_empty_turns_do_not_hide_distinct_original_sources_after_compaction(
    additional_tool_read,
):
    receipts = []
    scope = "bot:" + "b" * 32
    for turn, rows in enumerate(
        [
            [{"id": "r" * 32, "revision": 1, "content": "First source"}],
            [{"id": "r" * 32, "revision": 2, "content": "Second source"}],
            *([[]] * 10),
        ]
    ):
        content = json.dumps(rows)
        _, inputs = prepare_request(
            [
                {
                    "role": "system",
                    "content": content,
                    INPUT_KEY: source(scope, rows),
                    ANNOTATION_KEY: annotate(content, rows),
                }
            ]
        )
        if turn == 1 and additional_tool_read:
            inputs.append(capture(inputs, source(scope, rows, complete=False), rows))
        receipts.append(delivery(inputs, str(turn), turn_id=f"{turn:032x}"))
    # Older answers and their tools have been compacted. Recent empty snapshots
    # must leave room for the original nonempty observation locators.
    messages = []
    for turn in range(8, 12):
        messages.extend(
            [
                {"role": "user", "content": "Question", "_memory_turn_id": f"{turn:032x}"},
                {"role": "assistant", "content": str(turn)},
            ]
        )
    projected = project_history(messages, receipts, scope=scope)
    encoded = json.dumps(projected)
    assert receipts[0]["id"] in encoded
    assert receipts[1]["id"] in encoded
    assert sum(r["id"] in encoded for r in receipts[2:]) == 1


def test_linked_observation_survives_outside_recent_receipt_window(tmp_path, monkeypatch):
    from alysis_code.local_memory import read_source_context
    from alysis_code.memory_evidence import session_receipts

    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    memory = MemoryStore()
    saved = memory.change({"action": "save", "content": "Original outside the recent window."})
    agent = _session(tmp_path, _Client([_reply("First answer"), _reply("Historical answer")]))
    try:
        agent.run_turn("Read the saved entry.")
        original = session_receipts(agent)[0]
        memory.change({"action": "delete", "id": saved["id"], "revision": 1})
        agent.run_turn("Was the earlier answer supported?")
        latest = session_receipts(agent)[-1]
        assert latest["historical_reads"][0]["receipt_id"] == original["id"]
        current = memory_turn(agent)
        current.context()
        projected = project_history(
            [
                {"role": "system", "content": "empty", INPUT_KEY: current.input},
                {"role": "user", "content": "Older history was compacted."},
            ],
            [latest],
            scope=current.input["scope"],
            current=current.input,
            source_reader=lambda **args: read_source_context(agent, **args),
        )
        assert "Original outside the recent window." in json.dumps(projected)
        wire, inputs = prepare_request(projected)
        assert all(not row["records"] for row in inputs)
        assert inputs.historical_reads[0]["receipt_id"] == original["id"]
        assert not any("_memory_source_reads" in m for m in wire)
        # Any changed/truncated message loses its read receipt rather than
        # certifying text that was no longer sent to the provider.
        changed = [
            {**m, "content": "truncated"} if "_memory_source_reads" in m else m for m in projected
        ]
        assert not prepare_request(changed)[1].historical_reads
        memory.change({"action": "settings", "enabled": False})
        from alysis_code.memory_sources import with_original_source

        assert (
            with_original_source(
                latest,
                lambda **args: read_source_context(agent, **args),
                max_chars=4000,
            )
            == latest
        )
    finally:
        agent.close()
