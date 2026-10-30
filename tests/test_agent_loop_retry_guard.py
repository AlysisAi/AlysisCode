from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.agent_loop import (
    ToolDef,
    _resolve_session_pinned_prefix_len,
    create_session,
)
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMError, LLMResponse, ToolCall
from alysis_code.session_metrics import score_session_events
from alysis_code.session_store import read_session_events
from alysis_code.tools.availability import (
    _reset_tool_availability_for_tests,
    mark_available,
    mark_unavailable,
    register_tool_availability,
)


class _FakeClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.model = "test-model"
        self.temperature = 0.2
        self._responses = responses
        self.calls = 0
        self.temperatures: list[float | None] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta
        self.temperatures.append(temperature)
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _FailingClient:
    model = "test-model"
    temperature = 0.2

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        raise LLMError("LLM request failed: connection timed out")


def _task_brief_message_count(session: Any) -> int:
    return sum(
        1
        for message in session.messages
        if str(message.get("role") or "") == "user"
        and str(message.get("content") or "").startswith("<task_brief>")
    )


def _messages_without_task_brief(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message
        for message in messages
        if not (
            str(message.get("role") or "") == "user"
            and str(message.get("content") or "").startswith("<task_brief>")
        )
    ]


def test_run_turn_blocks_repeated_identical_failed_tool_calls(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="retry-guard",
    )

    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "missing.txt"})],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc2", name="fs_read", arguments={"path": "missing.txt"})],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc3", name="fs_read", arguments={"path": "missing.txt"})],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc4", name="fs_read", arguments={"path": "missing.txt"})],
            raw={},
        ),
        LLMResponse(content="done", tool_calls=[], raw={}),
    ]
    fake_client = _FakeClient(responses)
    session.client = fake_client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Read missing.txt.")
    finally:
        session.close()

    assert exit_code == 0
    assert fake_client.calls == 5
    assert all(temperature is None for temperature in fake_client.temperatures)

    events = list(read_session_events(sessions_dir / "retry-guard.jsonl"))
    tool_results = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("type") == "tool_result"
    ]
    assert len(tool_results) == 4
    assert "missing.txt" in str(tool_results[0].get("error", ""))
    assert "missing.txt" in str(tool_results[1].get("error", ""))
    assert "Blocked repeated tool call" in str(tool_results[2].get("error", ""))
    assert "Blocked repeated tool call" in str(tool_results[3].get("error", ""))

    warnings = [
        event
        for event in events
        if event.get("type") == "warning"
        and event.get("payload", {}).get("warning") == "repeated_tool_failure_guard"
    ]
    assert warnings


def test_repeated_scope_refusal_preserves_structured_recovery_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the scoped-access route without changing the host platform.
    monkeypatch.setattr("alysis_code.agent.tools_assembly.sys", SimpleNamespace(platform="darwin"))
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="retry-path-escape",
    )

    escaped_path = str(Path.home() / "scope-refusal-output.txt")
    repeated_call = {"path": escaped_path, "content": "#!/bin/sh\n"}
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"tc{index}", name="fs_write", arguments=repeated_call)],
            raw={},
        )
        for index in range(1, 5)
    ]
    responses.append(
        LLMResponse(
            content="blocked: filesystem writes are workspace-scoped. category: policy",
            tool_calls=[],
            raw={},
        )
    )
    fake_client = _FakeClient(responses)
    session.client = fake_client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn(f"Write the requested output at {escaped_path}.")
    finally:
        session.close()

    assert exit_code == 0
    events = list(read_session_events(sessions_dir / "retry-path-escape.jsonl"))
    tool_results = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("type") == "tool_result"
    ]
    assert len(tool_results) == 4
    # External paths now enter scoped-access admission. This file would require
    # a whole-home grant and is refused before any file tool dispatch.
    assert tool_results[0]["error_code"] == "scoped_file_access_refused"
    assert tool_results[0]["dispatch_state"] == "not_dispatched"
    assert tool_results[0]["retryable"] is False

    repeated_result = tool_results[2]
    assert repeated_result["error_code"] == "repeated_tool_failure_guard"
    assert repeated_result["previous_error_code"] == "scoped_file_access_refused"
    assert "Do not retry the same blocked tool call" in repeated_result["guidance"]
    assert any(
        action["action"] == "ask_or_explain_boundary"
        for action in repeated_result["suggested_next_actions"]
    )

    warnings = [
        event
        for event in events
        if event.get("type") == "warning"
        and event.get("payload", {}).get("warning") == "repeated_tool_failure_guard"
    ]
    assert warnings
    assert warnings[0]["payload"]["previous_error_code"] == "scoped_file_access_refused"


def test_run_turn_returns_tool_result_when_tool_arguments_are_invalid_json(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=5,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="invalid-json-retry",
    )

    responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="fs_read",
                    arguments={"_raw_arguments": '{"path":"missing.txt"'},
                )
            ],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc2", name="fs_read", arguments={"path": "missing.txt"})],
            raw={},
        ),
        LLMResponse(content="done", tool_calls=[], raw={}),
    ]
    fake_client = _FakeClient(responses)
    session.client = fake_client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Read missing.txt.")
    finally:
        session.close()

    assert exit_code == 0
    assert fake_client.calls == 3
    assert all(temperature is None for temperature in fake_client.temperatures)

    events = list(read_session_events(sessions_dir / "invalid-json-retry.jsonl"))
    recoveries = [
        event.get("payload", {})
        for event in events
        if event.get("type") == "invalid_tool_json_recovered"
    ]
    assert recoveries == [{"tool": "fs_read", "tool_call_id": "tc1", "step": 1}]

    retries = [
        event
        for event in events
        if event.get("type") == "warning"
        and event.get("payload", {}).get("warning") == "adaptive_temperature_retry"
    ]
    assert retries == []

    tool_results = [
        event.get("payload", {}) for event in events if event.get("type") == "tool_result"
    ]
    assert len(tool_results) == 2
    invalid_json_result = tool_results[0].get("result", {})
    assert invalid_json_result == {
        "error": "tool call arguments were not valid JSON",
        "error_code": "invalid_tool_arguments_json",
        "guidance": "Re-issue the call with a valid JSON object for arguments.",
    }
    assert "missing.txt" in str(tool_results[1].get("result", {}).get("error", ""))


def test_run_turn_preserves_unicode_in_tool_result_message(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    (tmp_path / "greek.txt").write_text("καλημέρα\n", encoding="utf-8")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="unicode-tool-result",
    )

    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "greek.txt"})],
            raw={},
        ),
        LLMResponse(content="done", tool_calls=[], raw={}),
    ]
    fake_client = _FakeClient(responses)
    session.client = fake_client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Read greek.txt.")
    finally:
        session.close()

    assert exit_code == 0
    events = list(read_session_events(sessions_dir / "unicode-tool-result.jsonl"))
    tool_result_events = [
        event.get("payload", {}) for event in events if event.get("type") == "tool_result"
    ]
    assert tool_result_events
    content = str(tool_result_events[0].get("content") or "")
    assert "καλημέρα" in content
    assert "\\u03" not in content


def test_run_turn_rolls_back_user_message_after_llm_error(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="override-key",
    )
    session.client = _FailingClient()  # type: ignore[assignment]
    baseline_messages = copy.deepcopy(session.messages)
    baseline_pinned_prefix_len = _resolve_session_pinned_prefix_len(session)

    try:
        with pytest.raises(LLMError):
            session.run_turn("Refactor src/app.py and explain the change.")
        # The turn's transient messages (its user message) are rolled back and
        # the pinned prefix is unchanged. The one pinned message that may differ
        # is the task brief, which is re-rendered from the task the turn
        # accepted before dispatch (acceptance is not withdrawn by the failure).
        assert _messages_without_task_brief(session.messages) == _messages_without_task_brief(
            baseline_messages
        )
        assert _resolve_session_pinned_prefix_len(session) == baseline_pinned_prefix_len
    finally:
        session.close()


def test_run_turn_llm_error_keeps_accepted_task_and_brief_in_agreement(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", routing_mode="code_only")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="override-key",
    )
    session.client = _FailingClient()  # type: ignore[assignment]
    baseline_messages = copy.deepcopy(session.messages)
    baseline_pinned_prefix_len = _resolve_session_pinned_prefix_len(session)

    try:
        with pytest.raises(LLMError):
            session.run_turn("Fix src/parser.py without changing the CSV shape.")
        assert _messages_without_task_brief(session.messages) == _messages_without_task_brief(
            baseline_messages
        )
        assert _task_brief_message_count(session) == 0
        assert _resolve_session_pinned_prefix_len(session) == baseline_pinned_prefix_len
    finally:
        session.close()


def test_run_turn_returns_optional_unavailable_tool_result_without_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reset_tool_availability_for_tests()
    caplog.set_level("INFO", logger="alysis_code.tools.availability")
    reason = "module not importable: fake_optional_dependency"
    register_tool_availability("fake_optional_tool", optional=True)
    mark_unavailable("fake_optional_tool", reason)

    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="optional-unavailable",
    )
    session.client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fake_optional_tool",
                        arguments={"value": "ignored"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Call the fake optional tool.")
    finally:
        session.close()
        _reset_tool_availability_for_tests()

    assert exit_code == 0
    events = list(read_session_events(sessions_dir / "optional-unavailable.jsonl"))
    tool_results = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("type") == "tool_result"
        and event.get("payload", {}).get("name") == "fake_optional_tool"
    ]
    assert tool_results == [
        {"status": "tool_unavailable", "tool": "fake_optional_tool", "reason": reason}
    ]
    assert "error" not in tool_results[0]
    assert score_session_events(events)["tool_errors"] == 0
    assert [
        event
        for event in events
        if event.get("type") == "warning"
        and event.get("payload", {}).get("warning") == "repeated_tool_failure_guard"
    ] == []
    assert (
        len(
            [
                record
                for record in caplog.records
                if "optional_tool_unavailable" in record.getMessage()
                and "fake_optional_tool" in record.getMessage()
            ]
        )
        == 1
    )


def test_run_turn_optional_tool_becomes_unavailable_mid_session(tmp_path: Path) -> None:
    _reset_tool_availability_for_tests()
    reason = "module not importable: fake_optional_dependency"
    register_tool_availability("fake_optional_tool", optional=True)
    mark_available("fake_optional_tool")
    call_count = 0

    def _run_fake_optional_tool(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        _ = args
        call_count += 1
        mark_unavailable("fake_optional_tool", reason)
        return {"ok": True}

    cfg = AppConfig(model="test-model", routing_mode="code_only")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="optional-mid-session",
    )
    fake_tool = ToolDef(
        name="fake_optional_tool",
        description="Fake optional tool for availability policy tests.",
        parameters={"type": "object", "properties": {}, "required": []},
        run=_run_fake_optional_tool,
    )
    session.tools[fake_tool.name] = fake_tool
    session.tool_list.append(fake_tool.as_openai_tool())
    session.client = _FakeClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fake_optional_tool", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc2", name="fake_optional_tool", arguments={})],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Call the fake optional tool twice.")
    finally:
        session.close()
        _reset_tool_availability_for_tests()

    assert exit_code == 0
    assert call_count == 1
    events = list(read_session_events(sessions_dir / "optional-mid-session.jsonl"))
    tool_results = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("type") == "tool_result"
        and event.get("payload", {}).get("name") == "fake_optional_tool"
    ]
    assert tool_results == [
        {"ok": True},
        {"status": "tool_unavailable", "tool": "fake_optional_tool", "reason": reason},
    ]
    assert "error" not in tool_results[1]
    assert score_session_events(events)["tool_errors"] == 0
