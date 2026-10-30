"""Exercise note tools and compaction, not a scripted model's task quality."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)
from test_compactor_prefix_reuse import _compactor
from test_read_ledger_delivery_boundary import _Client, _response, _session

from alysis_code.agent.prompt_context import _is_host_managed_user_context_message
from alysis_code.agent.task_notes import task_notes_index
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.runtime_kind import RuntimeKind


def _call(name: str, call_id: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


class RecordingClient(_Client):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        return super().chat(**kwargs)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (RuntimeKind.INTERACTIVE_CHAT, True),
        (RuntimeKind.ONE_SHOT, True),
        (RuntimeKind.SUBAGENT, False),
        (RuntimeKind.FORGE_EXEC, False),
    ],
)
def test_notes_entry_is_pinned_without_creating_files_or_changing_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: RuntimeKind, expected: bool
) -> None:
    session = _session(tmp_path, monkeypatch, _Client([]), runtime_kind=kind)
    try:
        anchors = [
            message
            for message in session.messages
            if str(message.get("content", "")).startswith("<task_notes_context>")
        ]
        assert len(anchors) == int(expected)
        if expected:
            assert session.messages.index(anchors[0]) < session.pinned_prefix_len
            assert _is_host_managed_user_context_message(anchors[0]["content"])
            assert task_notes_index(session.store.session_id) in anchors[0]["content"]
        assert not (session.root / "Task notes").exists()
        assert {"fs_read", "fs_write", "shell_run"} <= session.tools.keys()
    finally:
        session.close()


def test_notes_paths_are_stable_and_isolate_opaque_conversation_ids() -> None:
    ids = ["chat_one", "work_one", "../chat_one", "CHAT_ONE", "έργο/日本語"]
    paths = [task_notes_index(value) for value in ids]
    assert len(set(paths)) == len(ids)
    for session_id, path in zip(ids, paths, strict=True):
        assert path == task_notes_index(session_id)
        assert Path(path).parts[0] == "Task notes"
        assert len(Path(path).parts) == 3
        assert not Path(path).is_absolute()


def test_one_turn_reopens_and_updates_notes_after_actual_execution_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "one-continuous-task"
    index = task_notes_index(session_id)
    topic = str(Path(index).with_name("Έρευνα-日本語.md"))
    index_text = f"# Current work\nRead [{Path(topic).name}]({Path(topic).name}).\nRemaining: verify the result.\n"
    original = "# Findings\nKeep source units unchanged.\nRejected approach: rounding before aggregation.\nStatus: verification pending.\n"
    revised = original.replace(
        "Status: verification pending.",
        "Status: source inspection complete; calculation remains unverified.",
    )
    client = RecordingClient(
        [
            _response(
                _call("fs_write", "index", path=index, content=index_text),
                _call("fs_write", "topic", path=topic, content=original),
            ),
            _response(_call("fs_read", "old-notes", path=topic)),
            *[_response(_call("fs_read", f"source-{i}", path=f"source-{i}.txt")) for i in range(4)],
            _response(_call("fs_read", "index-again", path=index, path_base="workspace_root")),
            _response(_call("fs_read", "notes-again", path=topic, path_base="workspace_root")),
            _response(_call("fs_write", "update", path=topic, content=revised)),
            LLMResponse(
                content="Recorded findings and remaining verification in the task notes.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session = _session(
        tmp_path,
        monkeypatch,
        client,
        session_id_override=session_id,
        compaction_profile="execution",
    )
    session.max_steps = 12
    for i in range(4):
        (session.root / f"source-{i}.txt").write_text(
            "Source evidence with original units.\n" * 250
        )
    # Deliberately omit note paths and details from the summary. The host entry
    # point must survive independently, and details must be recovered from disk.
    summary = {
        "goal": "Continue the same task",
        "constraints": [],
        "decisions": [],
        "work_done": [],
        "open_threads": [],
        "next_steps": [],
    }
    summarizer = _Client([LLMResponse(content=json.dumps(summary), tool_calls=[], raw={})] * 3)
    compactor = _compactor(
        tmp_path / "compactor", main_client=client, compactor_client=summarizer, profile="execution"
    )
    compactor.state.pinned_prefix_len = session.pinned_prefix_len
    session.conversation_compactor = compactor
    compacted = False

    def compact_at_milestone(**kwargs):
        nonlocal compacted
        # Four subsequent tool exchanges keep the normal protected execution
        # tail intact while allowing the original note reads to be removed.
        if compacted or len(client.requests) < 6:
            return kwargs["messages"], False
        messages, changed = compactor.compact_now(**kwargs)
        assert changed, "The test must cross a real compaction boundary in this turn"
        compacted = True
        return messages, changed

    monkeypatch.setattr(compactor, "maybe_compact", compact_at_milestone)
    try:
        assert (
            session.run_turn(
                "Inspect the source files, maintain working notes and record what remains unverified."
            )
            == 0
        )
        assert compacted
        after = client.requests[6]
        assert any(
            index in str(message.get("content", ""))
            for message in after[: session.pinned_prefix_len]
        )
        assert not any(message.get("tool_call_id") == "old-notes" for message in after)
        results = {
            m["tool_call_id"]: json.loads(m["content"])
            for m in session.messages
            if m.get("role") == "tool"
        }
        assert results["index-again"]["content"] == index_text
        assert results["notes-again"]["content"] == original
        assert not results["notes-again"].get("read_ledger_skipped")
        assert (session.root / topic).read_text() == revised
        assert (
            sum(event["type"] == "user_message" for event in session.store.events_snapshot()) == 1
        )
    finally:
        session.close()
        compactor._store.close()
