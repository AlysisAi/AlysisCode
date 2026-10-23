"""Read reuse must not survive removal of its delivered model context."""

from __future__ import annotations

import copy
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)
from test_compactor_prefix_reuse import SUMMARY, _compactor
from test_read_ledger_delivery_boundary import _Client, _read, _response, _session
from test_subagents import _build_main_tools

from alysis_code import agent_loop
from alysis_code import cli as cli_mod
from alysis_code.agent import empty_response_stall
from alysis_code.agent import session as session_mod
from alysis_code.agent.turn import core as turn_core
from alysis_code.cli_impl.chat.loop import _handle_chat_command_impl
from alysis_code.cli_impl.chat.state import _ForgeChatState
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMError
from alysis_code.llm.types import LLMResponse
from alysis_code.subagents import SubagentDefinition


def _final() -> LLMResponse:
    return LLMResponse(content="Source inspected without changes.", tool_calls=[], raw={})


def _tool_results(messages):
    results = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            results[message["tool_call_id"]] = json.loads(message["content"])
        except json.JSONDecodeError:
            # Empty-response recovery intentionally replaces JSON with an elided preview.
            continue
    return results


@pytest.mark.parametrize("outcome", ["applied", "failed", "no-op"])
def test_actual_manual_compaction_retires_only_successful_history_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    client = _Client(
        [
            _response(_read("old-read", {"path": "archived.txt"})),
            _final(),
            _response(_read("recent-read", {"path": "retained.txt"})),
            _final(),
        ]
    )
    session = _session(tmp_path, monkeypatch, client)
    original = "Precise archived source evidence.\n" * 100
    (session.root / "archived.txt").write_bytes(original.encode("utf-8"))
    (session.root / "retained.txt").write_bytes(b"Recent source evidence.\n")
    summary_client = _Client(
        [
            LLMResponse(
                content=("invalid summary" if outcome == "failed" else json.dumps(SUMMARY)),
                tool_calls=[],
                raw={},
            )
        ]
        * 2
    )
    compactor = _compactor(
        tmp_path / "compactor",
        main_client=client,
        compactor_client=summary_client,
    )
    compactor._settings = replace(compactor._settings, max_chunk_messages=4)
    try:
        assert session.run_turn("Inspect archived.txt without editing or execution.") == 0
        assert session.run_turn("Inspect retained.txt without editing or execution.") == 0
        assert _tool_results(session.messages)["old-read"]["content"] == original
        assert session.read_ledger.snapshot()
        session.messages.append({"role": "user", "content": "Explain the prior inspection."})
        compactor.state.pinned_prefix_len = session.pinned_prefix_len
        if outcome == "no-op":
            compactor.state.pinned_prefix_len = len(session.messages)
        session.conversation_compactor = compactor
        assert (
            _handle_chat_command_impl(
                cli_mod,
                input_text="/compact preserve the inspection findings",
                root=session.root,
                session=session,
                pending_images=[],
                console=Console(file=io.StringIO(), force_terminal=False),
                forge_state=_ForgeChatState(),
            )
            == "handled"
        )
        applied = outcome == "applied"
        assert compactor.state.history_chunk_index == int(applied)
        assert ("old-read" not in _tool_results(session.messages)) is applied
        assert "recent-read" in _tool_results(session.messages)
        reread = session.tools["fs_read"].run({"path": "archived.txt"})
        assert bool(reread.get("read_ledger_skipped")) is not applied
        if applied:
            assert reread["content"] == original
            # Conservative reset also retires still-retained coverage.
            recent = session.tools["fs_read"].run({"path": "retained.txt"})
            assert not recent.get("read_ledger_skipped")
    finally:
        session.close()
        compactor._store.close()


class _RecordingClient(_Client):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        result = super().chat(**kwargs)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.parametrize("boundary", ["automatic", "overflow"])
@pytest.mark.parametrize("outcome", ["applied", "failed", "no-op"])
def test_real_turn_compaction_boundary_preserves_or_retires_read_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, outcome: str
) -> None:
    client = _RecordingClient([_response(_read("old", {"path": "source.txt"})), _final()])
    session = _session(tmp_path, monkeypatch, client)
    original = "Exact prior file data.\n" * 160
    (session.root / "source.txt").write_bytes(original.encode("utf-8"))
    summary_client = _Client(
        [
            LLMResponse(
                content="invalid" if outcome == "failed" else json.dumps(SUMMARY),
                tool_calls=[],
                raw={},
            )
        ]
        * 2
    )
    compactor = _compactor(
        tmp_path / "compactor", main_client=client, compactor_client=summary_client
    )
    try:
        assert session.run_turn("Inspect source.txt without edits or execution.") == 0
        compactor.state.pinned_prefix_len = session.pinned_prefix_len
        first = True

        def compact_once(**kwargs):
            nonlocal first
            if not first or outcome == "no-op":
                return kwargs["messages"], False
            first = False
            return compactor.compact_now(**kwargs)

        monkeypatch.setattr(
            compactor,
            "maybe_compact",
            compact_once if boundary == "automatic" else lambda **kw: (kw["messages"], False),
        )
        if boundary == "overflow":
            monkeypatch.setattr(compactor, "compact_for_overflow", compact_once)
            client.responses.append(LLMError("LLM error 400: maximum context length exceeded"))
        client.responses.extend([_response(_read("again", {"path": "source.txt"})), _final()])
        session.conversation_compactor = compactor
        if boundary == "overflow" and outcome != "applied":
            # Existing overflow failure must not retire evidence or pretend to recover.
            with pytest.raises(LLMError, match="maximum context length"):
                session.run_turn("Recheck the exact source text without edits or execution.")
            read = session.tools["fs_read"].run({"path": "source.txt"})
        else:
            assert (
                session.run_turn("Recheck the exact source text without edits or execution.") == 0
            )
            read = _tool_results(session.messages)["again"]
        assert bool(read.get("read_ledger_skipped")) is (outcome != "applied")
        if outcome == "applied":
            assert read["content"] == original
            assert "old" not in _tool_results(session.messages)
            # Newly delivered content is again reusable without force.
            assert session.tools["fs_read"].run({"path": "source.txt"})["read_ledger_skipped"]
    finally:
        session.close()
        compactor._store.close()


@pytest.mark.parametrize("applied", [False, True])
def test_empty_response_recovery_resets_only_when_tool_text_is_elided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, applied: bool
) -> None:
    original = "Evidence after the retained beginning.\n" * (90 if applied else 1)
    client = _RecordingClient(
        [
            _response(_read("first", {"path": "source.txt"})),
            LLMResponse(content="", tool_calls=[], raw={}),
            _response(_read("again", {"path": "source.txt"})),
            _final(),
        ]
    )
    session = _session(tmp_path, monkeypatch, client)
    (session.root / "source.txt").write_bytes(original.encode("utf-8"))
    session.empty_response_stall_tracker = empty_response_stall.EmptyResponseStallTracker(
        policy=empty_response_stall.EmptyResponseStallPolicy(consecutive_threshold=1)
    )
    monkeypatch.setattr(turn_core, "sleep", lambda _: None)
    try:
        assert session.run_turn("Inspect source.txt without edits or execution.") == 0
        recoveries = [
            e["payload"]
            for e in session.store.events_snapshot()
            if e["type"] == "empty_response_stall_recovery"
        ]
        assert len(recoveries) == 1
        assert recoveries[0]["compaction"]["applied"] is applied
        result = _tool_results(session.messages)["again"]
        assert bool(result.get("read_ledger_skipped")) is not applied
        if applied:
            assert result["content"] == original
    finally:
        session.close()


@pytest.mark.parametrize("changed", [False, True])
def test_ordinary_child_resume_retains_actual_read_text_and_checks_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    children = []
    clients = []
    original = "Original child source evidence.\n"
    (tmp_path / "source.txt").write_bytes(original.encode("utf-8"))

    def client_factory(**kwargs):
        client = _RecordingClient(
            [_response(_read(f"read-{len(clients)}", {"path": "source.txt"})), _final()]
        )
        clients.append(client)
        return client

    def create_child(**kwargs):
        kwargs.update(enable_compaction=False, verification_enabled=False)
        child = session_mod.create_session(**kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(session_mod, "_make_session_llm_client", client_factory)
    monkeypatch.setattr(agent_loop, "create_session", create_child)
    tools = _build_main_tools(
        tmp_path=tmp_path,
        cfg=AppConfig(model=_Client.model, stream=False, skills_enabled=False),
        skills_enabled=False,
        subagents_enabled=True,
        non_interactive=True,
        subagent_registry={
            "reader": SubagentDefinition(
                name="reader",
                description="Inspect source.",
                system_prompt="Inspect source.",
                mode="readonly",
                allow_tools=("fs_read",),
                allow_workspace_writes=False,
            )
        },
    )
    scheduler = tools["subagent_run"].run.__self__.child_scheduler
    try:
        first = tools["subagent_run"].run({"name": "reader", "task": "Inspect source."})
        assert first["status"] == "success"
        if changed:
            (tmp_path / "source.txt").write_bytes(b"Changed source evidence.\n")
        resumed = tools["subagent_resume"].run(
            {"run_id": first["run_id"], "task": "Inspect current source."}
        )
        record = scheduler._children[resumed["run_id"]]
        record.completion.result(timeout=10)
        record.worker_bookkeeping_completion.result(timeout=10)
        assert len(clients) == 2
        # The actual first request in the resumed child contains original delivered text.
        assert _tool_results(clients[1].requests[0])["read-0"]["content"] == original
        again = _tool_results(children[1].messages)["read-1"]
        assert bool(again.get("read_ledger_skipped")) is not changed
        if changed:
            assert again["content"] == "Changed source evidence.\n"
    finally:
        scheduler.shutdown(cancel_pending=True)
        for child in children:
            child.close()
