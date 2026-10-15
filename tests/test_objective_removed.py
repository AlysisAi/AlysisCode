"""Conversation turns need no objective command, pinned state or task relation."""

from __future__ import annotations

import copy
import io

import pytest
from rich.console import Console

from alysis_code import cli as cli_mod
from alysis_code.agent.task_evidence import restore_conversation_evidence
from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.chat.loop import _handle_chat_command_impl
from alysis_code.cli_impl.chat.state import _ForgeChatState
from alysis_code.cli_impl.chat_slash_completer import SPECS
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse


class _Client:
    model = "test-model"
    temperature = 0.2

    def __init__(self):
        self.requests = []

    def chat(self, *, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return LLMResponse(content="Understood.", tool_calls=[], raw={})


def _session(root, *, name="session", mode="readonly"):
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            skills_enabled=False,
            subagents_enabled=False,
            web_search_mode="off",
        ),
        root=root,
        mode=mode,
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="test-key",
        session_id_override=name,
    )
    session.client = _Client()
    return session


def _command(session, text):
    output = io.StringIO()
    result = _handle_chat_command_impl(
        cli_mod,
        input_text=text,
        root=session.root,
        session=session,
        pending_images=[],
        console=Console(file=output, force_terminal=False),
        forge_state=_ForgeChatState(),
    )
    return result, output.getvalue()


@pytest.mark.parametrize("mode", ["readonly", "auto"])
def test_followups_have_only_normal_conversation_context(tmp_path, mode):
    session = _session(tmp_path, mode=mode)
    try:
        first = "Explain how a login form works."
        second = "Now explain billing instead."
        assert session.run_turn(first) == 0
        assert session.run_turn(second) == 0
        assert not hasattr(session, "task_state")
        for request in session.client.requests:
            text = str(request)
            assert "<task_brief>" not in text
            assert "<task_requirements>" not in text
        latest = session.client.requests[-1]
        assert sum(m.get("content") == first for m in latest) == 1
        assert sum(m.get("content") == second for m in latest) == 1
        types = {event["type"] for event in session.store.events_snapshot()}
        assert not types & {"session_task_state", "task_identity_resolved"}
    finally:
        session.close()


def test_removed_command_is_neither_advertised_nor_executed(tmp_path):
    session = _session(tmp_path)
    try:
        assert not any(spec.name == "objective" for spec in SPECS)
        _, help_text = _command(session, "/help")
        assert "/objective" not in help_text
        before = copy.deepcopy(session.messages)
        result, output = _command(session, "/objective new Do something")
        assert result == "handled"
        assert "Unknown" in output
        assert session.messages == before
        assert session.client.requests == []
    finally:
        session.close()


def test_old_objective_records_do_not_pin_or_block_resumed_requests(tmp_path):
    original = _session(tmp_path, name="original")
    resumed = _session(tmp_path, name="resumed")
    try:
        assert original.run_turn("Explain the old feature.") == 0
        baseline = original._task_evidence_state.snapshot
        original.store.append("session_task_state", {"transition": "accepted", "state": None})
        restore_conversation_evidence(
            resumed, original.store.events_snapshot(), source_session_id="original"
        )
        assert resumed._task_evidence_state.snapshot == baseline
        assert resumed.run_turn("Explain the new feature.") == 0
        assert not hasattr(resumed, "task_state")
        assert "<task_brief>" not in str(resumed.client.requests)
    finally:
        original.close()
        resumed.close()


def test_long_prior_request_does_not_impose_an_objective_delivery_gate(tmp_path):
    session = _session(tmp_path)
    try:
        assert session.run_turn("Explain this text: " + "example " * 4000) == 0
        session.messages = session.messages[: session.pinned_prefix_len]
        assert session.run_turn("Now explain something else.") == 0
        assert "task_requirements_unavailable" not in str(session.last_turn_outcome)
    finally:
        session.close()


def test_clear_resets_evidence_and_history_without_objective_state(tmp_path):
    session = _session(tmp_path)
    try:
        assert session.run_turn("Explain the first topic.") == 0
        original_id = session._task_evidence_state.task_id
        result, _ = _command(session, "/clear")
        assert result == "handled"
        assert session._task_evidence_state is None
        assert session.run_turn("Explain a different topic.") == 0
        assert session._task_evidence_state.task_id != original_id
        assert "Explain the first topic." not in str(session.client.requests[-1])
    finally:
        session.close()
