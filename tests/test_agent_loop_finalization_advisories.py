from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.agent.turn.core import _spec_faithfulness_advisory_message
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.types import AssistantResponsePhase, LLMError
from alysis_code.session_store import read_session_events

_LIVE_BG_LINE_1 = (
    "- You have 1 background process(es) started with shell_background; they are terminated "
    "when this run ends. If the task requires a server/daemon to still be running after you "
    "finish, start it with shell_service_start (durable) instead, and re-verify."
)


class _RecordingClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.call_records: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = tools, tool_choice, stream, on_text_delta, temperature
        self.call_records.append({"messages": list(messages)})
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _FailingRecordingClient(_RecordingClient):
    def __init__(self, responses: list[LLMResponse], error_message: str) -> None:
        super().__init__(responses)
        self._error_message = error_message

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        if self.calls < len(self._responses):
            return super().chat(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                stream=stream,
                on_text_delta=on_text_delta,
                temperature=temperature,
            )
        self.call_records.append({"messages": list(messages)})
        self.calls += 1
        raise LLMError(self._error_message)


class _FakeTerminalManager:
    def __init__(self, statuses: tuple[str, ...]) -> None:
        self._statuses = statuses

    def list(self) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(status=status) for status in self._statuses)

    def shutdown_all(self) -> None:
        pass


class _FakeDurableServiceManager:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        self.status_calls: list[str] = []

    def status(self, service_id: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        _ = timeout_s
        assert service_id == self.payload["service_id"]
        self.status_calls.append(service_id)
        return dict(self.payload)

    def list_active(self) -> list[dict[str, Any]]:
        return [dict(self.payload)]


def _failed_edit_response() -> LLMResponse:
    """Record a real mutation attempt without producing file changes."""
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="attempt-initial-edit",
                name="fs_edit",
                arguments={
                    "path": "missing-edit-target.py",
                    "edits": [{"op": "replace_exact", "target": "before", "replacement": "after"}],
                },
            )
        ],
        raw={},
    )


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _controller_details(path: Path) -> list[str]:
    return [
        str(payload.get("detail") or "")
        for payload in _event_payloads(path, "controller_intervention")
    ]


def _create_one_shot_session(tmp_path: Path, *, session_id: str, one_shot_execution: bool = True):
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot_execution,
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    return sessions_dir, session


def test_spec_faithfulness_advisory_live_background_warning_is_one_shot_only() -> None:
    one_shot_message = _spec_faithfulness_advisory_message(
        one_shot_execution=True,
        live_background_processes=1,
    )
    interactive_message = _spec_faithfulness_advisory_message(
        one_shot_execution=False,
        live_background_processes=1,
    )
    no_live_process_message = _spec_faithfulness_advisory_message(
        one_shot_execution=True,
        live_background_processes=0,
    )

    assert one_shot_message.splitlines().count(_LIVE_BG_LINE_1) == 1
    assert _LIVE_BG_LINE_1 not in interactive_message.splitlines()
    assert _LIVE_BG_LINE_1 not in no_live_process_message.splitlines()


def test_clean_one_shot_final_does_not_get_generic_spec_advisory(tmp_path: Path) -> None:
    sessions_dir, session = _create_one_shot_session(
        tmp_path,
        session_id="clean-one-shot-no-generic-spec-advisory",
    )
    client = _RecordingClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "42\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="Completed work: wrote answer.txt.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Create answer.txt containing 42.")
    finally:
        session.close()

    log_path = sessions_dir / "clean-one-shot-no-generic-spec-advisory.jsonl"
    assert exit_code == 0
    assert client.calls == 2
    assert "spec_faithfulness_advisory" not in _controller_details(log_path)
    assert _event_payloads(log_path, "completion_gate_nudge") == []
    assert _event_payloads(log_path, "completion_gate_accepted_with_open_problems") == []
    final_events = _event_payloads(log_path, "final")
    assert final_events[-1]["content"] == "Completed work: wrote answer.txt."


def test_ready_durable_outcome_finishes_without_redundant_spec_advisory(
    tmp_path: Path,
) -> None:
    sessions_dir, session = _create_one_shot_session(
        tmp_path,
        session_id="ready-durable-outcome-no-spec-advisory",
    )
    payload = {
        "service_id": "svc_ready",
        "ownership": "DURABLE_SERVICE",
        "status": "running",
        "alive": True,
        "identity_valid": True,
        "readiness": {
            "type": "tcp",
            "status": "ready",
            "strength": "owned_endpoint",
            "endpoint_owned": True,
            "host": "127.0.0.1",
            "port": 8080,
            "listener_pids": [123],
        },
    }
    session.durable_service_manager = _FakeDurableServiceManager(payload)  # type: ignore[assignment]
    session.tools["shell_service_start"] = ToolDef(
        name="shell_service_start",
        description="Start a durable local service.",
        parameters={"type": "object", "properties": {}},
        run=lambda _args: dict(payload),
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]
    final_text = "Hosted locally at http://localhost:8080."
    client = _RecordingClient(
        [
            LLMResponse(
                content="Starting the durable service.",
                tool_calls=[
                    ToolCall(
                        id="tc-service",
                        name="shell_service_start",
                        arguments={"cmd": "python -m http.server 8080"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content=final_text, tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Keep the service running on port 8080.")
    finally:
        session.close()

    log_path = sessions_dir / "ready-durable-outcome-no-spec-advisory.jsonl"
    assert exit_code == 0
    assert client.calls == 2
    assert "spec_faithfulness_advisory" not in _controller_details(log_path)
    assert _event_payloads(log_path, "completion_gate_nudge") == []
    assert session.durable_service_manager.status_calls
    assert set(session.durable_service_manager.status_calls) == {"svc_ready"}
    final_events = _event_payloads(log_path, "final")
    assert final_events[-1]["content"] == final_text


def test_one_shot_finalization_advisory_mentions_live_background_process(
    tmp_path: Path,
) -> None:
    sessions_dir, session = _create_one_shot_session(
        tmp_path,
        session_id="live-background-spec-advisory",
    )
    session.terminal_manager = _FakeTerminalManager(  # type: ignore[assignment]
        ("running", "exited")
    )
    client = _RecordingClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "42\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="Completed work: wrote answer.txt.",
                tool_calls=[],
                raw={},
            ),
            LLMResponse(
                content="Completed work: answer.txt matches the requested output.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Create answer.txt containing 42.")
    finally:
        session.close()

    log_path = sessions_dir / "live-background-spec-advisory.jsonl"
    nudge_events = _event_payloads(log_path, "completion_gate_nudge")
    final_call_messages = client.call_records[-1]["messages"]

    assert exit_code == 0
    assert [event["stage"] for event in nudge_events] == ["spec_faithfulness_advisory"]
    assert nudge_events[0]["live_background_processes"] == 1
    assert nudge_events[0]["message"].splitlines().count(_LIVE_BG_LINE_1) == 1
    assert any(
        message.get("role") == "system" and _LIVE_BG_LINE_1 in str(message.get("content") or "")
        for message in final_call_messages
    )
    progress_messages = [
        str(event.get("message") or "") for event in _event_payloads(log_path, "progress")
    ]
    assert "Requirements satisfied; running an optional final review." in progress_messages
    assert not any("missing execution evidence" in message for message in progress_messages)


@pytest.mark.parametrize(
    "error_message",
    [
        'LLM error 502: {"error":{"message":"Upstream model error"}}',
        "provider rejected the request: invalid parameter shape",
    ],
    ids=["transient-provider-failure", "non-transient-model-failure"],
)
def test_optional_review_failure_preserves_gate_clear_answer(
    tmp_path: Path,
    error_message: str,
) -> None:
    session_id = "optional-review-fallback"
    sessions_dir, session = _create_one_shot_session(
        tmp_path,
        session_id=session_id,
    )
    session.terminal_manager = _FakeTerminalManager(("running",))  # type: ignore[assignment]
    accepted_text = "Completed work: wrote answer.txt."
    client = _FailingRecordingClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "42\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content=accepted_text, tool_calls=[], raw={}),
        ],
        error_message,
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Create answer.txt containing 42.")
    finally:
        session.close()

    log_path = sessions_dir / f"{session_id}.jsonl"
    fallbacks = _event_payloads(log_path, "optional_finalization_failure_fallback")
    final_events = _event_payloads(log_path, "final")

    assert exit_code == 0
    assert client.calls == 3
    assert len(fallbacks) == 1
    assert fallbacks[0]["preserved_gate_clear_answer"] is True
    assert _event_payloads(log_path, "error") == []
    assert _event_payloads(log_path, "terminal_error") == []
    assert final_events[-1]["content"] == accepted_text
    assert final_events[-1]["degraded_reason"] == "optional_finalization_model_failure"
    assert final_events[-1]["preserved_gate_clear_answer"] is True


def test_interactive_file_work_finishes_without_optional_model_review(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "interactive-no-optional-review"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    accepted_text = "Completed work: wrote answer.txt."
    client = _RecordingClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "42\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content=accepted_text, tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Create answer.txt containing 42.")
    finally:
        session.close()

    log_path = sessions_dir / f"{session_id}.jsonl"
    assert exit_code == 0
    assert client.calls == 2
    assert _event_payloads(log_path, "completion_gate_nudge") == []
    assert _event_payloads(log_path, "optional_finalization_failure_fallback") == []
    assert _event_payloads(log_path, "final")[-1]["content"].startswith(
        accepted_text + "\n\nNot verified:"
    )
    assert session.last_turn_outcome["verified_success"] is False


@pytest.mark.parametrize("one_shot_execution", [False, True])
@pytest.mark.parametrize("attempted_mutation", [False, True])
def test_assistant_prose_does_not_change_completion_gate_decisions(
    tmp_path: Path, one_shot_execution: bool, attempted_mutation: bool
) -> None:
    texts = {
        "which-file": "Which file?",
        "file-name": "I need the file name before continuing.",
        "arabic": "أي ملف يجب أن أستخدم؟",
        "greek": "Ποιο αρχείο να χρησιμοποιήσω;",
        "japanese": "どのファイルを使いますか？",
        "armenian": "Ո՞ր ֆայլն օգտագործեմ՞",
        "below-300": "Should this remain under three hundred characters?",
        "above-300": "Which file? " + ("This sentence is declarative. " * 20),
        "rhetorical": (
            "What about edge cases? The implementation description remains a declarative "
            "answer and does not request a controller exception."
        ),
        "markdown-code": "The example is `value = predicate ? left : right` and is complete.",
        "fenced-code": "Result:\n```text\nready?\n```\nThe report is complete.",
        "ascii-semicolon": "The command completed; the result is recorded.",
        "question-then-explanation": "Which file? " + ("The explanation continues. " * 30),
    }
    signatures: dict[str, tuple[Any, ...]] = {}

    for case_id, final_text in texts.items():
        case_root = tmp_path / case_id
        case_root.mkdir()
        sessions_dir, session = _create_one_shot_session(
            case_root, session_id=case_id, one_shot_execution=one_shot_execution
        )
        client = _RecordingClient(
            [
                *([_failed_edit_response()] if attempted_mutation else []),
                LLMResponse(content=final_text, tool_calls=[], raw={}),
                LLMResponse(content="Finished.", tool_calls=[], raw={}),
                LLMResponse(content="Finished.", tool_calls=[], raw={}),
            ]
        )
        session.client = client  # type: ignore[assignment]

        try:
            exit_code = session.run_turn("Create the requested output file.")
        finally:
            session.close()

        log_path = sessions_dir / f"{case_id}.jsonl"
        decision_events = []
        for event in read_session_events(log_path):
            event_type = str(event.get("type") or "")
            if event_type not in {
                "one_shot_completion_gate_failed",
                "completion_gate_nudge",
                "completion_gate_accepted_with_open_problems",
                "completion_gate_blocker_accepted",
            }:
                continue
            payload = dict(event.get("payload") or {})
            assert "clarification_response" not in payload
            assert "clarification_allows_completion" not in payload
            decision_events.append(
                (
                    event_type,
                    payload.get("stage"),
                    tuple(payload.get("problems") or payload.get("remaining_problems") or ()),
                    payload.get("blocked_response"),
                    payload.get("blocked_response_allows_completion"),
                    payload.get("verification_expected"),
                )
            )

        assert exit_code == 0
        assert client.calls == (3 if attempted_mutation else 1)
        assert _controller_details(log_path).count("completion_gate_checklist") == (
            1 if attempted_mutation else 0
        )
        assert _event_payloads(log_path, "completion_gate_blocker_accepted") == []
        signatures[case_id] = tuple(decision_events)

    baseline = signatures["which-file"]
    assert bool(baseline) is attempted_mutation
    assert all(signature == baseline for signature in signatures.values())


def test_continuation_nudge_does_not_force_generic_final_spec_advisory(
    tmp_path: Path,
) -> None:
    sessions_dir, session = _create_one_shot_session(
        tmp_path,
        session_id="continuation-without-generic-spec-advisory",
    )
    client = _RecordingClient(
        [
            LLMResponse(
                content="I will inspect the repo, make the edit, and then verify it.",
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "ready\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="Completed work: wrote answer.txt.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Create answer.txt.")
    finally:
        session.close()

    log_path = sessions_dir / "continuation-without-generic-spec-advisory.jsonl"
    details = _controller_details(log_path)
    assert exit_code == 0
    assert client.calls == 3
    assert details.count("non_final_progress_continuation_nudge") == 1
    assert "spec_faithfulness_advisory" not in details


def test_read_only_chat_final_does_not_get_spec_advisory(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="auto"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override="read-only-chat-no-spec-advisory",
    )
    client = _RecordingClient(
        [
            LLMResponse(
                content="The repo contains README.md at the workspace root.",
                tool_calls=[],
                raw={},
            )
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("can you inspect the repo we are working?")
    finally:
        session.close()

    log_path = sessions_dir / "read-only-chat-no-spec-advisory.jsonl"
    assert exit_code == 0
    assert "spec_faithfulness_advisory" not in _controller_details(log_path)
    assert _event_payloads(log_path, "completion_gate_nudge") == []
