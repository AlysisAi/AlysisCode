from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import ToolDef, _resolve_session_pinned_prefix_len, create_session
from alysis_code.cli_impl.commands.chat_resume_helpers import _load_chat_resume_messages
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface


class _Client:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[Any], *, partial_text: str = "") -> None:
        self.responses = iter(responses)
        self.partial_text = partial_text
        self.requests: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], on_text_delta=None, **kwargs: Any):
        self.requests.append(copy.deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, BaseException):
            if self.partial_text and on_text_delta is not None:
                on_text_delta(self.partial_text)
            raise response
        return response


class _Surface(NoopSurface):
    def __init__(self) -> None:
        self.deltas: list[str] = []

    def on_assistant_token(self, text: str) -> None:
        self.deltas.append(text)

    def emit_message_delta(self, text: str, **kwargs: Any) -> None:
        self.deltas.append(text)


def _session(tmp_path: Path, client: _Client) -> Any:
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=True)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {"test-model": {"context_window_tokens": 32768, "max_output_tokens": 2048}},
        }
    }
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=4,
        no_log=False,
        client_override=client,
        compactor_client_override=client,
        hooks_enabled=False,
        surface=_Surface(),
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        enable_conversation_summarization=False,
        enable_tool_output_offload=False,
    )


def _reply(text: str = "Understood.") -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[], raw={})


def _user_contents(messages: list[dict[str, Any]]) -> list[Any]:
    return [message.get("content") for message in messages if message.get("role") == "user"]


@pytest.mark.parametrize("chat_only", [False, True])
@pytest.mark.parametrize("partial_text", ["", "Let me check that. "])
@pytest.mark.parametrize(
    "constraint,followup",
    [
        ("Leo can't do anything before 10:30 am.", "Maya needs to leave at 11:45. What works?"),
        ("Please keep the existing public API unchanged.", "What approach would you suggest?"),
    ],
)
def test_failed_answer_preserves_input_for_followup_and_resume(
    tmp_path: Path, chat_only: bool, partial_text: str, constraint: str, followup: str
) -> None:
    client = _Client([LLMError("connection timed out"), _reply()], partial_text=partial_text)
    session = _session(tmp_path, client)
    prefix_len = _resolve_session_pinned_prefix_len(session)
    try:
        with pytest.raises(LLMError, match="connection timed out"):
            session.run_turn(constraint, chat_only=chat_only)
        assert _resolve_session_pinned_prefix_len(session) == prefix_len
        assert session.messages[prefix_len:] == [{"role": "user", "content": constraint}]
        if partial_text:
            assert partial_text in session.surface.deltas
        assert _load_chat_resume_messages(session.store.path) == session.messages[prefix_len:]

        assert session.run_turn(followup, chat_only=chat_only) == 0
        assert len(client.requests) == 2
        assert _user_contents(client.requests[-1])[-2:] == [constraint, followup]
        assert _user_contents(client.requests[-1]).count(constraint) == 1
        assert _load_chat_resume_messages(session.store.path) == session.messages[prefix_len:]
        assert not any(
            message.get("role") == "assistant" and message.get("content") == partial_text
            for message in client.requests[-1]
        )
        events = list(read_session_events(session.store.path))
        assert any(event.get("type") == "terminal_error" for event in events)
    finally:
        session.close()


def test_repeated_failed_submissions_remain_distinct_after_resume(tmp_path: Path) -> None:
    prompt = "Keep the report anonymous, please."
    followup = "Could you try again now?"
    client = _Client([LLMError("offline"), LLMError("offline"), _reply()])
    session = _session(tmp_path, client)
    try:
        for _ in range(2):
            with pytest.raises(LLMError, match="offline"):
                session.run_turn(prompt)
        assert _user_contents(_load_chat_resume_messages(session.store.path)) == [prompt, prompt]
        assert session.run_turn(followup) == 0
        assert _user_contents(client.requests[-1])[-3:] == [prompt, prompt, followup]
        assert _user_contents(_load_chat_resume_messages(session.store.path)) == [
            prompt,
            prompt,
            followup,
        ]
    finally:
        session.close()


def test_error_after_completed_tool_preserves_results_without_reexecuting(tmp_path: Path) -> None:
    client = _Client(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="probe-1", name="noop_probe", arguments={})],
                raw={},
            ),
            LLMError("connection timed out"),
            _reply(),
        ]
    )
    session = _session(tmp_path, client)
    executions: list[dict[str, Any]] = []

    def run(args: dict[str, Any]) -> dict[str, Any]:
        executions.append(args)
        return {"ok": True, "value": "recorded result"}

    session.tools["noop_probe"] = ToolDef(
        name="noop_probe",
        description="Read a diagnostic value.",
        parameters={"type": "object", "properties": {}},
        run=run,
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]
    try:
        with pytest.raises(LLMError, match="connection timed out"):
            session.run_turn("Read the diagnostic value, please.")
        assert len(executions) == 1
        assert session.run_turn("What did you find?") == 0
        assert len(executions) == 1
        tools = [m for m in client.requests[-1] if m.get("role") == "tool"]
        assert len(tools) == 1 and tools[0]["tool_call_id"] == "probe-1"
        assert "recorded result" in tools[0]["content"]
        replay = _load_chat_resume_messages(session.store.path)
        assert [m for m in replay if m.get("role") == "tool"] == tools
    finally:
        session.close()


def test_cancelled_answer_keeps_accepted_input(tmp_path: Path) -> None:
    prompt = "Please use the figures I gave you, without estimating missing values."
    client = _Client([KeyboardInterrupt(), _reply()])
    session = _session(tmp_path, client)
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn(prompt)
        assert session.run_turn("Let's continue.") == 0
        assert _user_contents(client.requests[-1]).count(prompt) == 1
        assert _user_contents(_load_chat_resume_messages(session.store.path)).count(prompt) == 1
    finally:
        session.close()
