"""Real store pagination and output-delivery contracts; no answer shortcuts."""

import json
import uuid
from types import SimpleNamespace

import pytest
from test_agent_loop_failed_turn_history import _Client, _reply, _session

from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.config import AppConfig
from alysis_code.local_memory import MemoryStore, memory_turn
from alysis_code.memory_artifacts import artifact_source, register_memory_artifact
from alysis_code.memory_evidence import (
    INPUT_KEY,
    delivery,
    prepare_request,
    project_history,
    source,
)
from alysis_code.memory_pages import serialized
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.tools.artifacts import session_artifact_read


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.delenv("ALYSIS_MEMORY_DISABLED", raising=False)
    cfg = AppConfig()
    cfg.extra_fields = {"compaction": {"tool_output_offload_threshold_chars": 2200}}
    session = SimpleNamespace(runtime_kind=RuntimeKind.INTERACTIVE_CHAT, mode="auto", cfg=cfg)
    return MemoryStore(), session


def test_pages_reassemble_unicode_and_escaped_bodies_with_exact_coverage(memory):
    store, session = memory
    originals = {}
    for text in ['Greek: Καλημέρα 🌍 "quoted"\n' * 130, "日本語\t\\ notes " * 180, "Short note."]:
        saved = store.change({"action": "save", "content": text})
        originals[saved["id"]] = text.strip()
    tool = memory_turn(session).tools["memory_read"]
    arguments, collected, ranges = {}, {}, {}
    for _ in range(30):
        page = tool.run(arguments)
        assert "error" not in page
        assert len(serialized(page)) < 2200
        for record in page["entries"]:
            collected[record["id"]] = collected.get(record["id"], "") + record["content"]
            if "content_range" in record:
                area = record["content_range"]
                assert area["start"] == ranges.get(record["id"], 0)
                assert area["end"] - area["start"] == len(record["content"])
                ranges[record["id"]] = area["end"]
                receipt = source("local:test", [record])["records"][0]
                assert receipt["body_supplied"] is False
                assert receipt["content_range"] == area
        if not page["next_cursor"]:
            break
        arguments = {"cursor": page["next_cursor"]}
    else:
        pytest.fail("Pagination did not make progress")
    assert collected == originals


@pytest.mark.parametrize("change", ["delete", "edit", "insert", "query", "device", "scope"])
def test_stale_or_retargeted_continuation_is_rejected_without_skipping(memory, change):
    store, session = memory
    for index in range(12):
        store.change({"action": "save", "content": f"Project {index}: " + "notes " * 70})
    tool = memory_turn(session).tools["memory_read"]
    first = tool.run({"query": "Project"})
    args = {"query": "Project", "cursor": first["next_cursor"]}
    record = first["entries"][0]
    if change in {"delete", "edit"}:
        store.change(
            {
                "action": "delete" if change == "delete" else "save",
                "id": record["id"],
                "revision": record["revision"],
                "content": "Updated.",
            }
        )
    elif change == "insert":
        store.change({"action": "save", "content": "Project newly added."})
    elif change == "query":
        args["query"] = "notes"
    elif change == "device":
        with store.transaction() as db:
            db.execute("UPDATE settings SET value=? WHERE key='device'", (str(uuid.uuid4()),))
    else:
        session.memory_scope = {"project_id": str(uuid.uuid4())}
        tool = memory_turn(session).tools["memory_read"]
    result = tool.run(args)
    assert result["restart_required"] is True
    assert "entries" not in result
    assert tool.run({"offset": 1})["restart_required"] is True


def test_project_sections_are_bounded_and_external_edits_invalidate_cursor(memory):
    store, session = memory
    project_id = str(uuid.uuid4())
    session.memory_scope = {"project_id": project_id}
    before = store.snapshot(project_id)["project"]
    text = "Project history: 日本語 and Ελληνικά.\n" * 650
    store.change(
        {"action": "project_write", "revision": before["revision"], "content": text},
        project_id=project_id,
    )
    tool = memory_turn(session).tools["memory_read"]
    first = tool.run({})
    chunks, page = [first["project"]["content"]], first
    for _ in range(50):
        assert len(serialized(page)) < 2200
        if not page["next_cursor"]:
            break
        page = tool.run({"cursor": page["next_cursor"]})
        chunks.append(page["project"]["content"])
    assert "".join(chunks) == text
    from pathlib import Path

    Path(first["project"]["path"]).write_text("Changed outside the agent.")
    assert tool.run({"cursor": first["next_cursor"]})["restart_required"]


def test_requested_page_size_reduces_round_trips_without_dropping_records(memory):
    store, session = memory
    expected = {
        store.change({"action": "save", "content": f"Project {i}: " + "notes " * 100})["id"]
        for i in range(20)
    }
    tool = memory_turn(session).tools["memory_read"]
    small = tool.run({})
    assert small["next_cursor"]
    large = tool.run({"max_chars": 64000})
    assert large["next_cursor"] is None
    assert {r["id"] for r in large["entries"]} == expected
    assert len(serialized(large)) <= 64000
    assert "error" in tool.run({"max_chars": 64001})
    # Cursor positions cannot be altered while retaining a prior checksum.
    parts = small["next_cursor"].split(":")
    parts[1] = str(int(parts[1]) + 1)
    assert "error" in tool.run({"cursor": ":".join(parts)})


def test_requested_large_page_reaches_model_without_redundant_artifact_read(memory, tmp_path):
    from alysis_code.llm.types import LLMResponse, ToolCall

    store, _ = memory
    for i in range(20):
        store.change({"action": "save", "content": f"Project {i}: " + "notes " * 100})
    client = _Client(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="read", name="memory_read", arguments={"max_chars": 64000})
                ],
                raw={},
            ),
            _reply(),
        ]
    )
    agent = _session(tmp_path, client)
    agent.tool_output_offloader = ToolOutputOffloader(
        artifact_layout=agent.store.session_artifact_layout,
        workspace_root=tmp_path,
        threshold_chars=6000,
        preview_chars=2000,
    )
    try:
        assert agent.run_turn("Read my saved project notes.") == 0
        result = json.loads(
            [m for m in client.requests[-1] if m.get("role") == "tool"][-1]["content"]
        )
        assert len(result["entries"]) == 20 and result["next_cursor"] is None
        assert 6000 < len(serialized(result)) <= 64000
        assert not [
            e for e in agent.store.events_snapshot() if e["type"] == "tool_output_offloaded"
        ]
    finally:
        agent.close()


@pytest.fixture
def artifact(tmp_path):
    agent = _session(tmp_path, _Client([_reply()]))
    offloader = ToolOutputOffloader(
        artifact_layout=agent.store.session_artifact_layout,
        workspace_root=tmp_path,
        threshold_chars=100,
        preview_chars=80,
    )
    scope = "local:test:None:True"
    result = {
        "generation": 7,
        "entries": [
            {"id": "first", "revision": 2, "content": 'Καλημέρα "friend"\n' * 50},
            {"id": "last", "revision": 1, "content": "The tracker is Ember."},
        ],
        "project": None,
        "next_cursor": "next-page",
        "next_offset": 2,
        "total": 3,
    }
    output = offloader.maybe_offload(
        tool_name="memory_read",
        tool_call_id="read",
        step=1,
        result=result,
        content_json=serialized(result),
    )
    assert output.offloaded
    register_memory_artifact(agent, result, output, scope)
    try:
        yield agent, result, output, scope
    finally:
        agent.close()


def test_artifact_delivery_survives_compaction_and_registry_rebuild(artifact):
    agent, original, output, scope = artifact
    value = session_artifact_read(
        artifact_layout=agent.store.session_artifact_layout, locator=output.artifact_locator
    )
    current = source(scope, [], version=8, complete=True)
    messages = [
        {"role": "system", "content": "Current memory is empty.", INPUT_KEY: current},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "artifact", "function": {"name": "session_artifact_read"}}],
        },
        {"role": "tool", "tool_call_id": "artifact", "content": serialized(value)},
    ]
    wire, inputs = prepare_request(messages, artifact_resolver=lambda v: artifact_source(agent, v))
    delivered = inputs[-1]
    assert delivered["delivery_kind"] == "artifact"
    assert [r["id"] for r in delivered["records"]] == ["first", "last"]
    assert all(r["body_supplied"] for r in delivered["records"])
    assert "Ember" not in serialized(inputs)
    assert all(INPUT_KEY not in m for m in wire)
    receipt = delivery(inputs, "Previously supplied answer.", turn_id="earlier")
    projected = project_history(
        [{"role": "user", "content": "Summarized history."}],
        [receipt],
        scope=scope,
        current=current,
    )
    assert '"id": "last"' in str(projected)
    assert "absent_from_current_complete_snapshot" in str(projected)
    assert "Ember" not in str(projected)
    # Rebuild from durable events as after session restart, not process memory.
    del agent._memory_artifact_registry
    assert artifact_source(agent, value)["records"] == delivered["records"]
    other_scope = [{**messages[0], INPUT_KEY: source("local:other", [])}, *messages[1:]]
    assert (
        len(prepare_request(other_scope, artifact_resolver=lambda v: artifact_source(agent, v))[1])
        == 1
    )
    assert not prepare_request(messages[1:], artifact_resolver=lambda v: artifact_source(agent, v))[
        1
    ]


def test_partial_artifacts_never_claim_full_body_and_tampering_is_unknown(artifact):
    agent, original, output, scope = artifact
    manifest = next(
        e["payload"]
        for e in agent.store.events_snapshot()
        if e["type"] == "memory_artifact_created"
    )
    span = manifest["records"][0]
    value = session_artifact_read(
        artifact_layout=agent.store.session_artifact_layout,
        locator=output.artifact_locator,
        max_bytes=span["start"] + 30,
    )
    receipt = artifact_source(agent, value)
    assert len(receipt["records"]) == 1
    assert receipt["records"][0]["body_supplied"] is False
    assert receipt["records"][0]["body_fragment"]["end"] < span["end"] - span["start"]
    assert artifact_source(agent, {**value, "content": value["content"] + "changed"}) is None
    path = agent.store.session_artifact_layout.resolve_locator(output.artifact_locator)
    path.write_bytes(path.read_bytes().replace(b"Ember", b"Cedar"))
    assert artifact_source(agent, value) is None


def test_offload_preserves_continuation_and_receipts_require_registered_origin(artifact):
    agent, original, output, scope = artifact
    stub = json.loads(output.content_for_message)
    assert stub["next_cursor"] == "next-page" and stub["generation"] == 7
    unregistered = SimpleNamespace(store=agent.store)
    agent.store.append("conversation_cleared", {})
    value = session_artifact_read(
        artifact_layout=agent.store.session_artifact_layout, locator=output.artifact_locator
    )
    assert artifact_source(unregistered, value) is None


def test_partial_artifact_of_a_record_section_preserves_its_original_location(artifact):
    agent, original, output, scope = artifact
    record = original["entries"][0]
    record["content_range"] = {"start": 100, "end": 100 + len(record["content"]), "total": 3000}
    offloader = ToolOutputOffloader(
        artifact_layout=agent.store.session_artifact_layout,
        workspace_root=agent.root,
        threshold_chars=100,
        preview_chars=80,
    )
    output = offloader.maybe_offload(
        tool_name="memory_read",
        tool_call_id="section",
        step=2,
        result=original,
        content_json=serialized(original),
    )
    register_memory_artifact(agent, original, output, scope)
    manifest = [
        e["payload"]
        for e in agent.store.events_snapshot()
        if e["type"] == "memory_artifact_created"
    ][-1]
    value = session_artifact_read(
        artifact_layout=agent.store.session_artifact_layout,
        locator=output.artifact_locator,
        max_bytes=manifest["records"][0]["start"] + 30,
    )
    received = artifact_source(agent, value)["records"][0]
    assert received["body_supplied"] is False
    assert "content_range" not in received
    assert received["body_fragment"]["source_content_range"] == record["content_range"]


def test_actual_turn_records_artifact_delivery_and_restores_receipt_after_summary(
    memory, tmp_path, monkeypatch
):
    from alysis_code import local_memory
    from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages
    from alysis_code.llm.types import LLMResponse, ToolCall
    from alysis_code.memory_evidence import HISTORY_KEY

    original_memory_turn = local_memory.memory_turn

    def legacy_surface(session):
        memory = original_memory_turn(session)
        # Exercise a legacy/unbounded tool surface through the real dispatcher.
        memory.tools["memory_read"].metadata.pop("offload_exempt", None)
        return memory

    monkeypatch.setattr(local_memory, "memory_turn", legacy_surface)

    store, _ = memory
    target = store.change({"action": "save", "content": "The delivery tracker is Ember."})
    for index in range(30):
        store.change({"action": "save", "content": f"Archive {index}: " + "Planning notes. " * 70})

    class Client(_Client):
        def __init__(self):
            super().__init__([])

        def chat(self, **kwargs):
            self.requests.append(kwargs["messages"])
            if len(self.requests) == 1:
                call = ToolCall(
                    id="memory", name="memory_read", arguments={"query": "delivery tracker"}
                )
            elif len(self.requests) == 2:
                value = json.loads(
                    [m for m in kwargs["messages"] if m.get("role") == "tool"][-1]["content"]
                )
                call = ToolCall(
                    id="artifact",
                    name="session_artifact_read",
                    arguments={"locator": value["artifact_locator"]},
                )
            else:
                return _reply("Read the recorded tracker name.")
            return LLMResponse(content="", tool_calls=[call], raw={})

    client = Client()
    agent = _session(tmp_path, client)
    agent.tool_output_offloader = ToolOutputOffloader(
        artifact_layout=agent.store.session_artifact_layout,
        workspace_root=tmp_path,
        threshold_chars=100,
        preview_chars=80,
    )
    try:
        assert agent.run_turn("Remind me of the saved delivery tracker name.") == 0
        assert len(client.requests) == 3
        receipts = [
            e["payload"] for e in agent.store.events_snapshot() if e["type"] == "memory_delivery"
        ]
        found = [
            r
            for receipt in receipts
            for s in receipt["sources"]
            if s.get("delivery_kind") == "artifact"
            for r in s["records"]
        ]
        assert any(r["id"] == target["id"] and r["body_supplied"] for r in found)
        store.change({"action": "delete", "id": target["id"], "revision": 1})
        agent.store.append(
            "conversation_summary_updated",
            {
                "active_conversation_messages": [
                    {"role": "user", "content": "Earlier conversation summarized."}
                ]
            },
        )
        restored = _load_chat_resume_messages(agent.store.path)
        persisted = [
            r
            for m in restored
            for receipt in m.get(HISTORY_KEY, [])
            for s in receipt["sources"]
            for r in s["records"]
        ]
        assert any(r["id"] == target["id"] and r["body_supplied"] for r in persisted)
        assert "Ember" not in serialized(restored)
    finally:
        agent.close()
