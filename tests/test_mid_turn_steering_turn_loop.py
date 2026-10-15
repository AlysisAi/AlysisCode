from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.prompt_context import (
    refresh_session_environment_context_message,
)
from alysis_code.agent.steering import SteerInbox, steer_inbox_for
from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.types import LLMError

STEER_TEXT = "actually use pytest, not unittest"
STEER_MARKER = "[Mid-turn message from the user]"


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _cfg() -> AppConfig:
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False, max_steps=4)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "test-model": {"context_window_tokens": 4096, "max_output_tokens": 512},
            },
            "default": {"context_window_tokens": 4096, "max_output_tokens": 512},
        }
    }
    return cfg


class _ScriptedClient:
    model = "test-model"
    temperature = 0.0

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        steer_on_call: int | None = None,
        steer_text: str = STEER_TEXT,
    ) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self._index = 0
        self._steer_on_call = steer_on_call
        self._steer_text = steer_text
        self.session: Any | None = None

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        on_reasoning_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse:
        _ = stream, on_text_delta, on_reasoning_delta, temperature, cancellation_token
        self.calls.append({"messages": list(messages), "tools": tools})
        response = self._responses[self._index]
        self._index += 1
        if self._steer_on_call is not None and len(self.calls) == self._steer_on_call:
            inbox = steer_inbox_for(self.session, create=True)
            assert inbox is not None
            inbox.send(self._steer_text)
        return response


def _make_session(tmp_path: Path, client: _ScriptedClient) -> Any:
    session = create_session(
        cfg=_cfg(),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        enable_tool_output_offload=False,
        enable_conversation_summarization=False,
    )
    session.client = client  # type: ignore[assignment]
    client.session = session
    session.tools["noop_probe"] = ToolDef(
        name="noop_probe",
        description="Test-only no-op.",
        parameters={"type": "object", "properties": {}},
        run=lambda _args: {"ok": True},
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]
    return session


def _two_step_script() -> list[LLMResponse]:
    return [
        LLMResponse(content="", tool_calls=[_tool_call("tc-1", "noop_probe", {})], raw={}),
        LLMResponse(content="Done.", tool_calls=[], raw={}),
    ]


def _contents(messages: list[dict[str, Any]]) -> list[str]:
    return [str(message.get("content") or "") for message in messages]


def test_steer_note_reaches_the_next_model_request(tmp_path: Path) -> None:
    client = _ScriptedClient(_two_step_script(), steer_on_call=1)
    session = _make_session(tmp_path, client)
    try:
        exit_code = session.run_turn("Do the thing.")
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 2
    assert not any(STEER_MARKER in text for text in _contents(client.calls[0]["messages"]))
    assert any(STEER_TEXT in text for text in _contents(client.calls[1]["messages"]))


def test_steer_note_is_committed_to_durable_user_history(tmp_path: Path) -> None:
    client = _ScriptedClient(_two_step_script(), steer_on_call=1)
    session = _make_session(tmp_path, client)
    try:
        session.run_turn("Do the thing.")
        steered = [
            message
            for message in session.messages
            if STEER_MARKER in str(message.get("content") or "")
        ]
    finally:
        session.close()

    assert len(steered) == 1
    assert steered[0]["role"] == "user"


def test_steer_note_lands_after_tool_results_without_splitting_them(tmp_path: Path) -> None:
    client = _ScriptedClient(_two_step_script(), steer_on_call=1)
    session = _make_session(tmp_path, client)
    try:
        session.run_turn("Do the thing.")
        roles = [str(message.get("role")) for message in session.messages]
        contents = _contents(session.messages)
    finally:
        session.close()

    steer_index = next(index for index, text in enumerate(contents) if STEER_MARKER in text)
    tool_indices = [index for index, role in enumerate(roles) if role == "tool"]
    assert tool_indices
    assert steer_index > max(tool_indices)
    for tool_index in tool_indices:
        assert roles[tool_index - 1] in {"assistant", "tool"}


def test_steering_delivery_is_recorded(tmp_path: Path) -> None:
    client = _ScriptedClient(_two_step_script(), steer_on_call=1)
    session = _make_session(tmp_path, client)
    try:
        session.run_turn("Do the thing.")
        events = [
            event
            for event in session.store._events  # noqa: SLF001 - telemetry assertion
            if str(event.get("type")) == "steer_message_delivered"
        ]
    finally:
        session.close()

    assert len(events) == 1
    payload = dict(events[0].get("payload") or {})
    assert payload["count"] == 1
    assert payload["step"] >= 2
    assert payload["chars"] >= len(STEER_TEXT)
    assert payload["messages"] == [STEER_TEXT]


def test_turn_without_steering_keeps_normal_behavior(tmp_path: Path) -> None:
    client = _ScriptedClient(_two_step_script())
    session = _make_session(tmp_path, client)
    try:
        exit_code = session.run_turn("Do the thing.")
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 2
    assert not any(
        STEER_MARKER in str(message.get("content") or "") for message in session.messages
    )
    assert not any(
        str(event.get("type")) == "steer_message_delivered"
        for event in session.store._events  # noqa: SLF001 - telemetry assertion
    )


def test_cross_thread_environment_refresh_is_refused_during_active_turn(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingClient:
        model = "test-model"
        temperature = 0.0

        def chat(self, **_kwargs: Any) -> LLMResponse:
            started.set()
            assert release.wait(timeout=3)
            return LLMResponse(content="Done.", tool_calls=[], raw={})

    client = BlockingClient()
    session = _make_session(tmp_path, client)  # type: ignore[arg-type]
    errors: list[BaseException] = []

    def run_turn() -> None:
        try:
            session.run_turn("Do the thing.")
        except BaseException as exc:  # noqa: BLE001 - surface worker failures
            errors.append(exc)

    worker = threading.Thread(target=run_turn)
    worker.start()
    try:
        assert started.wait(timeout=2)
        before = list(session.messages)
        assert session._turn_owner_thread_id == worker.ident  # noqa: SLF001

        session.mode = "review"
        assert refresh_session_environment_context_message(session) is False
        assert session.messages == before
        warnings = [
            event
            for event in session.store._events  # noqa: SLF001 - telemetry assertion
            if str(event.get("type")) == "warning"
            and str((event.get("payload") or {}).get("warning"))
            == "environment_context_refresh_cross_thread_refused"
        ]
        assert len(warnings) == 1
    finally:
        release.set()
        worker.join(timeout=3)

    try:
        assert not worker.is_alive()
        assert errors == []
        assert session._turn_owner_thread_id is None  # noqa: SLF001
        assert refresh_session_environment_context_message(session) is True
        assert session.messages != before
    finally:
        session.close()


def test_same_thread_environment_refresh_succeeds_during_active_turn(
    tmp_path: Path,
) -> None:
    refreshed: list[bool] = []

    class RefreshingClient:
        model = "test-model"
        temperature = 0.0
        session: Any | None = None

        def chat(self, **_kwargs: Any) -> LLMResponse:
            assert self.session is not None
            refreshed.append(refresh_session_environment_context_message(self.session))
            return LLMResponse(content="Done.", tool_calls=[], raw={})

    client = RefreshingClient()
    session = _make_session(tmp_path, client)  # type: ignore[arg-type]
    client.session = session
    try:
        session.run_turn("Do the thing.")
        assert refreshed == [True]
        assert session._turn_owner_thread_id is None  # noqa: SLF001
    finally:
        session.close()


def test_broken_inbox_cannot_fail_the_turn(tmp_path: Path) -> None:
    class ExplodingInbox(SteerInbox):
        def drain(self) -> list[str]:
            raise RuntimeError("inbox exploded")

    client = _ScriptedClient(_two_step_script())
    session = _make_session(tmp_path, client)
    try:
        session.steer_inbox = ExplodingInbox()
        exit_code = session.run_turn("Do the thing.")
        warnings = [
            event
            for event in session.store._events  # noqa: SLF001 - telemetry assertion
            if str(event.get("type")) == "warning"
            and str((event.get("payload") or {}).get("warning")) == "steer_message_delivery_failed"
        ]
    finally:
        session.close()

    assert exit_code == 0
    assert warnings


def test_rollback_after_llm_error_returns_the_note_to_the_inbox(tmp_path: Path) -> None:
    class FailingClient(_ScriptedClient):
        def chat(self, **kwargs: Any) -> LLMResponse:  # type: ignore[override]
            self.calls.append({"messages": list(kwargs.get("messages") or [])})
            raise LLMError("provider exploded")

    client = FailingClient([])
    session = _make_session(tmp_path, client)
    restored_notifications: list[list[str]] = []
    session.surface.on_steer_messages_restored = lambda messages: restored_notifications.append(
        list(messages)
    )
    inbox = steer_inbox_for(session, create=True)
    assert inbox is not None
    inbox.send(STEER_TEXT)
    try:
        with pytest.raises(LLMError, match="provider exploded"):
            session.run_turn("Do the thing.")
        recovered = inbox.drain()
    finally:
        session.close()

    assert recovered == [STEER_TEXT]
    assert restored_notifications == [[STEER_TEXT]]
    assert not any(
        STEER_MARKER in str(message.get("content") or "") for message in session.messages
    )


def test_rollback_restores_drained_note_before_newer_arrival(tmp_path: Path) -> None:
    newer_text = "newer message sent during the failed request"

    class FailingClient(_ScriptedClient):
        def chat(self, **kwargs: Any) -> LLMResponse:  # type: ignore[override]
            self.calls.append({"messages": list(kwargs.get("messages") or [])})
            inbox = steer_inbox_for(self.session, create=True)
            assert inbox is not None
            inbox.send(newer_text)
            raise LLMError("provider exploded")

    client = FailingClient([])
    session = _make_session(tmp_path, client)
    inbox = steer_inbox_for(session, create=True)
    assert inbox is not None
    inbox.send(STEER_TEXT)
    try:
        with pytest.raises(LLMError, match="provider exploded"):
            session.run_turn("Do the thing.")
        recovered = inbox.drain()
    finally:
        session.close()

    assert recovered == [STEER_TEXT, newer_text]


def test_multiple_mid_turn_notes_arrive_in_order(tmp_path: Path) -> None:
    class MultiSteerClient(_ScriptedClient):
        def chat(self, **kwargs: Any) -> LLMResponse:  # type: ignore[override]
            response = super().chat(**kwargs)
            if len(self.calls) == 1:
                inbox = steer_inbox_for(self.session, create=True)
                assert inbox is not None
                inbox.send("first note")
                inbox.send("second note")
            return response

    client = MultiSteerClient(_two_step_script())
    session = _make_session(tmp_path, client)
    try:
        session.run_turn("Do the thing.")
        steered = [
            str(message.get("content") or "")
            for message in session.messages
            if STEER_MARKER in str(message.get("content") or "")
        ]
    finally:
        session.close()

    assert len(steered) == 2
    assert "first note" in steered[0]
    assert "second note" in steered[1]
