from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent.turn.core as turn_core
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


def _response(content: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=list(calls),
        raw={"choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 1}},
    )


class _Client:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(kwargs)
        assert len(self.requests) <= len(self.responses), "unbounded empty-response recovery"
        return self.responses[len(self.requests) - 1]


def _session(tmp_path: Path, *, depth: int = 0, max_steps: int = 12, **kwargs: Any) -> Any:
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=max_steps,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=False,
        subagent_depth=depth,
        verification_enabled=False,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
        **kwargs,
    )


def _payloads(session: Any, event_type: str) -> list[dict[str, Any]]:
    return [
        event["payload"]
        for event in read_session_events(session.store.path)
        if event["type"] == event_type
    ]


@pytest.mark.parametrize("depth", [0, 1], ids=["interactive", "child"])
def test_empty_stop_after_read_recovers_to_tools_and_visible_final(
    tmp_path: Path, depth: int
) -> None:
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    session = _session(tmp_path, depth=depth)
    final_text = "Saved the line count to answer.txt."
    client = _Client(
        [
            _response("", ToolCall(id="read", name="fs_read", arguments={"path": "data.txt"})),
            _response(),
            _response(
                "",
                ToolCall(
                    id="write", name="fs_write", arguments={"path": "answer.txt", "content": "2\n"}
                ),
            ),
            _response(final_text),
        ]
    )
    session.client = client
    try:
        assert (
            session.run_turn("Count the lines in data.txt and save the count to answer.txt.") == 0
        )
    finally:
        session.close()

    assert len(client.requests) == 4
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "2\n"
    assert [item["content"] for item in _payloads(session, "final")] == [final_text]
    final = _payloads(session, "final")[-1]
    assert final["verification_notice"] is False
    assert final["task_outcome"]["verified_success"] is False
    recovery = _payloads(session, "empty_model_response_model_control_anomaly")
    assert len(recovery) == 1
    assert recovery[0]["forced_tool_choice"] is None
    assert "edit a relevant path" not in recovery[0]["message"]


@pytest.mark.parametrize("depth", [0, 1], ids=["interactive", "child"])
def test_empty_first_response_recovers_without_inventing_a_write_request(
    tmp_path: Path, depth: int
) -> None:
    session = _session(tmp_path, depth=depth)
    client = _Client([_response(" \n"), _response("Hello.")])
    session.client = client
    try:
        assert session.run_turn("Hello") == 0
    finally:
        session.close()

    assert len(client.requests) == 2
    assert [item["content"] for item in _payloads(session, "final")] == ["Hello."]
    recovery = _payloads(session, "empty_model_response_model_control_anomaly")[-1]
    assert "edit a relevant path" not in recovery["message"]
    assert recovery["forced_tool_choice"] is None


@pytest.mark.parametrize("guard_enabled", [True, False])
@pytest.mark.parametrize("depth", [0, 1], ids=["interactive", "child"])
def test_persistent_empty_responses_end_with_bounded_incomplete_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_enabled: bool, depth: int
) -> None:
    monkeypatch.setenv("ALYSIS_EMPTY_RESPONSE_STALL", "on" if guard_enabled else "off")
    monkeypatch.setattr(turn_core, "sleep", lambda seconds: None)
    session = _session(tmp_path, depth=depth)
    client = _Client([_response() for _ in range(8)])
    session.client = client
    try:
        session.run_turn("Explain the available options.")
    finally:
        session.close()

    assert 2 < len(client.requests) < 8
    final = _payloads(session, "final")[-1]
    assert final["content"].strip()
    assert "Remaining work:" in final["content"]
    assert final["internal_fallback"] is True
    assert final["degraded"] is True
    assert final["stop_reason"] == "empty_response_anomaly_retry_exhausted"
    assert _payloads(session, "empty_model_response_anomaly_incomplete_after_retries")
    assert not _payloads(session, "turn_intent_finalized")


def test_empty_response_does_not_start_recovery_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [10.0]
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=now[0],
        deadline_monotonic=110.0,
        configured_duration_seconds=100.0,
        clock=lambda: now[0],
    )
    session = _session(tmp_path, execution_deadline=deadline)
    client = _Client([_response()])
    original_chat = client.chat

    def exhausted_chat(**kwargs: Any) -> LLMResponse:
        response = original_chat(**kwargs)
        now[0] = 111.0
        return response

    monkeypatch.setattr(client, "chat", exhausted_chat)
    session.client = client
    try:
        session.run_turn("Explain the available options.")
    finally:
        session.close()

    assert len(client.requests) == 1
    assert not _payloads(session, "empty_response_stall_recovery")
    final = _payloads(session, "final")[-1]
    assert final["content"].strip()
    assert final["internal_fallback"] is True
    assert final["stop_reason"] == "run_budget_exhausted"
    assert _payloads(session, "deadline_exhausted")


def test_empty_response_at_step_limit_reports_incomplete_without_another_call(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, max_steps=1)
    client = _Client([_response()])
    session.client = client
    try:
        session.run_turn("Explain the available options.")
    finally:
        session.close()

    assert len(client.requests) == 1
    assert not _payloads(session, "empty_response_stall_recovery")
    final = _payloads(session, "final")[-1]
    assert final["content"].strip()
    assert final["internal_fallback"] is True
    assert final["degraded"] is True
    stalled = _payloads(session, "empty_response_stall_detected")[-1]
    assert stalled["plan"]["allowed"] is False
    assert stalled["plan"]["reason"] == "step_budget_exhausted"
