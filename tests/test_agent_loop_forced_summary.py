from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.types import AssistantResponsePhase
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface

SMOKE_MODEL = "gpt-4o-mini"


class _ForcedSummarySurface(NoopSurface):
    def __init__(self) -> None:
        super().__init__()
        self.final_messages: list[str] = []
        self.errors: list[str] = []

    def on_assistant_message_done(self, text: str) -> None:
        self.final_messages.append(text)

    def on_error(self, err: str) -> None:
        self.errors.append(err)


class _BudgetExhaustionClient:
    model = SMOKE_MODEL
    temperature = 0.2

    def __init__(self, *, finalization_mode: str = "ok") -> None:
        self.finalization_mode = finalization_mode
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = on_text_delta, temperature
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "stream": stream,
            }
        )
        tool_enabled_calls = sum(1 for item in self.calls if item["tools"] is not None)
        if tools is None:
            if self.finalization_mode == "error":
                raise RuntimeError("forced finalization failed")
            if self.finalization_mode == "blank":
                return LLMResponse(content="   ", tool_calls=[], raw={})
            return LLMResponse(
                content=(
                    "Completed work: inspected the repo and made partial progress.\n"
                    "Remaining work: the requested change is unfinished.\n"
                    "Known issues or risks: the turn hit the step budget before completion."
                ),
                tool_calls=[],
                raw={},
            )
        return LLMResponse(
            content="Still working.",
            tool_calls=[
                ToolCall(
                    id=f"call-{tool_enabled_calls}",
                    name="fs_list",
                    arguments={"root_path": "."},
                )
            ],
            raw={},
        )


class _ScriptedClient:
    model = SMOKE_MODEL
    temperature = 0.2

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        finalization_response: LLMResponse | None = None,
    ) -> None:
        self._responses = responses
        self._finalization_response = finalization_response
        self.calls: list[dict[str, Any]] = []
        self._tool_enabled_calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = on_text_delta, temperature
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "stream": stream,
            }
        )
        if tools is None and self._finalization_response is not None:
            return self._finalization_response
        response = self._responses[self._tool_enabled_calls]
        self._tool_enabled_calls += 1
        return response


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


def _assert_forced_summary_artifacts(
    *,
    log_path: Path,
    surface: _ForcedSummarySurface,
) -> str:
    assert _event_payloads(log_path, "forced_final_summary_requested")
    completed_events = _event_payloads(log_path, "forced_final_summary_completed")
    assert completed_events
    assistant_events = _event_payloads(log_path, "assistant_message")
    final_events = _event_payloads(log_path, "final")
    assert assistant_events
    assert final_events
    summary = surface.final_messages[-1]
    assert assistant_events[-1]["content"] == summary
    assert final_events[-1]["content"] == summary
    assert completed_events[-1]["controller_interventions_total"] >= 1
    assert (
        final_events[-1]["controller_interventions_total"]
        == completed_events[-1]["controller_interventions_total"]
    )
    assert "controller_interventions" in final_events[-1]
    return summary


def _assert_last_forced_summary_request(
    client: _ScriptedClient,
    *,
    latest_assistant_text: str,
    termination_cause: str,
) -> None:
    assert client.calls
    assert client.calls[-1]["tools"] is None
    request_messages = client.calls[-1]["messages"]
    assert request_messages[-2] == {
        "role": "assistant",
        "content": latest_assistant_text,
    }
    assert str(request_messages[-1].get("role")) == "system"
    assert f"Stop reason: {termination_cause}" in str(request_messages[-1].get("content"))


def _install_stub_subagent_run(session: Any, *, result_text: str) -> None:
    tool = session.tools["subagent_run"]

    def fake_run(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "subagent": str(args.get("name") or "explorer"),
            "subagent_session_id": "stub-subagent",
            "result": result_text,
            "usage": {},
            "sandbox": {"mode": "readonly", "tools": ["fs_read"]},
        }

    session.tools["subagent_run"] = replace(tool, run=fake_run)


def test_matching_tool_bridge_text_and_final_emit_once(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="assistant-dedupe-same-final",
        surface=surface,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Done.",
                tool_calls=[ToolCall(id="tc1", name="fs_list", arguments={"root_path": "."})],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Inspect the repo and finish the task.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert surface.final_messages == ["Done."]
    assistant_events = _event_payloads(log_path, "assistant_message")
    final_events = _event_payloads(log_path, "final")
    assert [event["content"] for event in assistant_events] == ["Done."]
    assert [event["content"] for event in final_events] == ["Done."]
    assert assistant_events[0]["tool_calls"] == ["fs_list"]


def test_distinct_tool_bridge_text_and_final_both_emit(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="assistant-dedupe-distinct-final",
        surface=surface,
    )
    client = _ScriptedClient(
        [
            LLMResponse(
                content="Inspecting files.",
                tool_calls=[ToolCall(id="tc1", name="fs_list", arguments={"root_path": "."})],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Inspect the repo and finish the task.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert surface.final_messages == ["Inspecting files.", "Done."]
    assistant_events = _event_payloads(log_path, "assistant_message")
    final_events = _event_payloads(log_path, "final")
    assert [event["content"] for event in assistant_events] == [
        "Inspecting files.",
        "Done.",
    ]
    assert [event["content"] for event in final_events] == ["Done."]


def test_generic_max_steps_exhaustion_emits_forced_final_summary(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-generic-max-steps",
        surface=surface,
    )
    client = _BudgetExhaustionClient()
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Keep working on the repo task until it is done.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert len([call for call in client.calls if call["tools"] is not None]) == 3
    assert len(client.calls) == 4
    assert client.calls[-1]["tools"] is None
    assert client.calls[-1]["stream"] is False
    final_request_messages = client.calls[-1]["messages"]
    assert str(final_request_messages[-1].get("role")) == "system"
    assert "Stop reason: the overall step budget is exhausted" in str(
        final_request_messages[-1].get("content")
    )
    assert "No more tool calls are allowed." in str(final_request_messages[-1].get("content"))
    assert surface.final_messages
    assert "Completed work:" in surface.final_messages[-1]
    assert "Remaining work:" in surface.final_messages[-1]
    assert "Known issues or risks:" in surface.final_messages[-1]

    assistant_events = _event_payloads(log_path, "assistant_message")
    final_events = _event_payloads(log_path, "final")
    assert assistant_events
    assert final_events
    assert assistant_events[-1]["content"] == surface.final_messages[-1]
    assert final_events[-1]["content"] == surface.final_messages[-1]
    assert _event_payloads(log_path, "forced_final_summary_requested")
    assert _event_payloads(log_path, "forced_final_summary_completed")


@pytest.mark.parametrize("finalization_mode", ["error", "blank"])
def test_forced_final_summary_uses_fallback_when_needed(
    tmp_path: Path,
    finalization_mode: str,
) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override=f"forced-summary-fallback-{finalization_mode}",
        surface=surface,
    )
    client = _BudgetExhaustionClient(finalization_mode=finalization_mode)
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Keep working on the repo task until it is done.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert len([call for call in client.calls if call["tools"] is not None]) == 2
    assert len(client.calls) == 3
    assert client.calls[-1]["tools"] is None
    assert surface.final_messages
    assert "The turn stopped before it could finish" in surface.final_messages[-1]
    assert "Listed directories: ." in surface.final_messages[-1]
    assert (
        "A reliable final completion summary could not be produced"
        not in surface.final_messages[-1]
    )
    assert "Remaining work:" in surface.final_messages[-1]
    assert "Known issues or risks:" in surface.final_messages[-1]

    fallback_events = _event_payloads(log_path, "forced_final_summary_fallback")
    assert fallback_events
    assert fallback_events[-1]["fallback_reason"] in {"finalization_error", "blank_response"}
    assert _event_payloads(log_path, "forced_final_summary_completed") == []
    assistant_events = _event_payloads(log_path, "assistant_message")
    final_events = _event_payloads(log_path, "final")
    assert assistant_events[-1]["content"] == surface.final_messages[-1]
    assert final_events[-1]["content"] == surface.final_messages[-1]
    assert fallback_events[-1]["controller_interventions_total"] >= 1
    assert (
        final_events[-1]["controller_interventions_total"]
        == fallback_events[-1]["controller_interventions_total"]
    )
    assert "controller_interventions" in final_events[-1]


def test_forced_final_summary_uses_local_fallback_when_deadline_is_too_close(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=10.0,
        deadline_monotonic=11.0,
        configured_duration_seconds=1.0,
        clock=lambda: 10.75,
    )
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-deadline-fallback",
        surface=surface,
        execution_deadline=deadline,
    )
    client = _BudgetExhaustionClient()
    session.client = client  # type: ignore[assignment]

    try:
        summary = session._emit_forced_final_summary_before_termination(
            reason="deadline_exhausted",
            termination_cause="the run deadline is exhausted",
            termination_kind="deadline_exhausted",
            max_steps=2,
        )
        log_path = session.store.path
    finally:
        session.close()

    assert client.calls == []
    assert "run deadline was exhausted" in summary
    assert "step budget before completion" not in summary
    fallback_events = _event_payloads(log_path, "forced_final_summary_fallback")
    assert fallback_events[-1]["reason"] == "deadline_exhausted"
    assert fallback_events[-1]["termination_kind"] == "deadline_exhausted"
    assert fallback_events[-1]["fallback_reason"] == "local_summary_due_to_deadline"


def test_max_steps_fallback_summary_uses_truthful_termination_wording_with_stagnation(
    tmp_path: Path,
) -> None:
    (tmp_path / "repeat.txt").write_text("x\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-exploration-max-steps-fallback",
        surface=surface,
    )
    client = _ScriptedClient(
        [
            *[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id=f"tc{idx}", name="fs_read", arguments={"path": "repeat.txt"})
                    ],
                    raw={},
                )
                for idx in range(1, 11)
            ],
        ],
        finalization_response=LLMResponse(content="   ", tool_calls=[], raw={}),
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement search command and update tests.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    fallback_events = _event_payloads(log_path, "forced_final_summary_fallback")
    assert fallback_events
    assert fallback_events[-1]["reason"] == "max_steps_exceeded"
    assert fallback_events[-1]["termination_cause"] == "the overall step budget is exhausted"
    assert surface.final_messages
    assert "overall step budget is exhausted" in surface.final_messages[-1]
    assert "before the turn terminated" in surface.final_messages[-1]
    assistant_events = _event_payloads(log_path, "assistant_message")
    final_events = _event_payloads(log_path, "final")
    assert assistant_events[-1]["content"] == surface.final_messages[-1]
    assert final_events[-1]["content"] == surface.final_messages[-1]
    assert fallback_events[-1]["controller_interventions_total"] >= 1
    assert (
        final_events[-1]["controller_interventions_total"]
        == fallback_events[-1]["controller_interventions_total"]
    )
    assert "controller_interventions" in final_events[-1]


@pytest.mark.parametrize("one_shot_execution", [False, True])
def test_child_assisted_exploration_after_edit_attempt_emits_generic_forced_summary(
    tmp_path: Path,
    one_shot_execution: bool,
) -> None:
    (tmp_path / "src" / "mini_notes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "mini_notes" / "cli.py").write_text("print('x')\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=13,
        chat_turn_fixed_override=13,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot_execution,
        enable_chat_turn_step_budget=True,
        subagents_enabled=True,
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-post-explore-max-steps",
        surface=surface,
    )
    _install_stub_subagent_run(
        session,
        result_text="Edit targets: src/mini_notes/cli.py, src/mini_notes/logic.py, tests/test_cli.py",
    )
    client = _ScriptedClient(
        [
            _failed_edit_response(),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="subagent_run",
                        arguments={"name": "explorer", "task": "Map repo"},
                    )
                ],
                raw={},
            ),
            *[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"tc{idx}",
                            name="fs_read",
                            arguments={"path": "src/mini_notes/cli.py"},
                        )
                    ],
                    raw={},
                )
                for idx in range(2, 13)
            ],
        ],
        finalization_response=LLMResponse(
            content=(
                "Completed work: inspected `src/mini_notes/cli.py` and mapped likely targets.\n"
                "Remaining work: implementation has not started.\n"
                "Known issues or risks: the step budget ended before edits began."
            ),
            tool_calls=[],
            raw={},
        ),
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement search command and update tests.")
        log_path = session.store.path
    finally:
        session.close()

    # Chat hands control back at its turn budget; one-shot exits the process.
    assert exit_code == (1 if one_shot_execution else 0)
    assert bool(surface.errors) is one_shot_execution
    assert len([call for call in client.calls if call["tools"] is not None]) == session.max_steps
    assert client.calls[-1]["tools"] is None
    assert _event_payloads(log_path, "one_shot_post_explore_stagnation_detected") == []
    assert _event_payloads(log_path, "implementation_bootstrap_nudge") == []
    assert _event_payloads(log_path, "one_shot_exploration_stagnation_detected")
    assert _event_payloads(log_path, "one_shot_post_explore_incomplete_after_retries") == []
    requested = _event_payloads(log_path, "forced_final_summary_requested")
    assert requested[-1]["reason"] == (
        "max_steps_exceeded" if one_shot_execution else "max_steps_exhausted"
    )
    assert requested[-1]["termination_cause"] == "the overall step budget is exhausted"
    budget_events = _event_payloads(
        log_path, "error" if one_shot_execution else "interactive_step_budget_handoff"
    )
    assert "exploration" in budget_events[-1]["stagnation_state"]
    summary = _assert_forced_summary_artifacts(log_path=log_path, surface=surface)
    assert "Known issues or risks: the step budget ended" in summary


@pytest.mark.parametrize("one_shot_execution", [False, True])
def test_exploration_stagnation_budget_end_emits_generic_forced_summary(
    tmp_path: Path,
    one_shot_execution: bool,
) -> None:
    (tmp_path / "repeat.txt").write_text("x\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=11,
        chat_turn_fixed_override=11,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot_execution,
        enable_chat_turn_step_budget=True,
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-exploration-max-steps",
        surface=surface,
    )
    client = _ScriptedClient(
        [
            _failed_edit_response(),
            *[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id=f"tc{idx}", name="fs_read", arguments={"path": "repeat.txt"})
                    ],
                    raw={},
                )
                for idx in range(1, 11)
            ],
        ],
        finalization_response=LLMResponse(
            content=(
                "Completed work: repeated exploration confirmed the same file state.\n"
                "Remaining work: implementation and verification are still pending.\n"
                "Known issues or risks: the step budget ended before progress."
            ),
            tool_calls=[],
            raw={},
        ),
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement search command and update tests.")
        log_path = session.store.path
    finally:
        session.close()

    # Chat hands control back at its turn budget; one-shot exits the process.
    assert exit_code == (1 if one_shot_execution else 0)
    assert bool(surface.errors) is one_shot_execution
    assert len([call for call in client.calls if call["tools"] is not None]) == session.max_steps
    assert client.calls[-1]["tools"] is None
    assert _event_payloads(log_path, "one_shot_exploration_stagnation_detected")
    assert _event_payloads(log_path, "one_shot_exploration_incomplete_after_retries") == []
    requested = _event_payloads(log_path, "forced_final_summary_requested")
    assert requested[-1]["reason"] == (
        "max_steps_exceeded" if one_shot_execution else "max_steps_exhausted"
    )
    assert requested[-1]["termination_cause"] == "the overall step budget is exhausted"
    budget_events = _event_payloads(
        log_path, "error" if one_shot_execution else "interactive_step_budget_handoff"
    )
    assert "exploration" in budget_events[-1]["stagnation_state"]
    summary = _assert_forced_summary_artifacts(log_path=log_path, surface=surface)
    assert "Known issues or risks: the step budget ended" in summary


def test_edit_stagnation_budget_end_emits_generic_forced_summary(tmp_path: Path) -> None:
    (tmp_path / "target.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-edit-max-steps",
        surface=surface,
    )
    client = _ScriptedClient(
        [
            *[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"tc{idx}",
                            name="fs_edit",
                            arguments={
                                "path": "target.txt",
                                "edits": [
                                    {
                                        "op": "replace_wrong",
                                        "target": "alpha",
                                        "replacement": "ALPHA",
                                    }
                                ],
                            },
                        )
                    ],
                    raw={},
                )
                for idx in range(1, 13)
            ],
        ],
        finalization_response=LLMResponse(
            content=(
                "Completed work: attempted localized edits on `target.txt`.\n"
                "Remaining work: the requested change is still unfinished.\n"
                "Known issues or risks: the step budget ended before a successful write."
            ),
            tool_calls=[],
            raw={},
        ),
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement search command and update tests.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 1
    assert surface.errors
    assert len([call for call in client.calls if call["tools"] is not None]) == session.max_steps
    assert client.calls[-1]["tools"] is None
    assert _event_payloads(log_path, "one_shot_edit_stagnation_detected")
    assert _event_payloads(log_path, "one_shot_edit_incomplete_after_retries") == []
    requested = _event_payloads(log_path, "forced_final_summary_requested")
    assert requested[-1]["reason"] == "max_steps_exceeded"
    assert requested[-1]["termination_cause"] == "the overall step budget is exhausted"
    errors = _event_payloads(log_path, "error")
    assert "failed_edit" in errors[-1]["stagnation_state"]
    summary = _assert_forced_summary_artifacts(log_path=log_path, surface=surface)
    assert "Known issues or risks: the step budget ended" in summary


@pytest.mark.parametrize("one_shot_execution", [False, True])
def test_repeated_non_final_progress_accepts_after_single_nudge(
    tmp_path: Path,
    one_shot_execution: bool,
) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot_execution,
        enable_chat_turn_step_budget=True,
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-repeated-non-final-progress",
        surface=surface,
    )
    repeated_progress_text = "I will implement search next."
    client = _ScriptedClient(
        [
            _failed_edit_response(),
            LLMResponse(
                content=repeated_progress_text,
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
            LLMResponse(
                content=repeated_progress_text,
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
            LLMResponse(
                content=repeated_progress_text,
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
        ],
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement search command and update tests.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 3
    assert not surface.errors
    # The failed edit supplies execution evidence; unresolved work stays visible
    # after bounded correction, while preserving the model's own summary.
    assert surface.final_messages[-1].startswith(repeated_progress_text)
    assert "No files were changed" in surface.final_messages[-1]
    assert len(_event_payloads(log_path, "continuation_nudge")) == 1
    assert _event_payloads(log_path, "completion_gate_accepted_with_open_problems")
    assert _event_payloads(log_path, "one_shot_incomplete_after_retries") == []
    assert _event_payloads(log_path, "forced_final_summary_requested") == []
    assert _event_payloads(log_path, "forced_final_summary_completed") == []


@pytest.mark.parametrize("one_shot_execution", [False, True])
def test_non_final_progress_continuation_cap_accepts_second_final(
    tmp_path: Path,
    one_shot_execution: bool,
) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot_execution,
        enable_chat_turn_step_budget=True,
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-non-final-progress-cap",
        surface=surface,
    )
    final_progress_text = "I will update the parser next."
    client = _ScriptedClient(
        [
            _failed_edit_response(),
            LLMResponse(
                content="I will inspect the parser next.",
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
            LLMResponse(
                content=final_progress_text,
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
            LLMResponse(
                content=final_progress_text,
                tool_calls=[],
                raw={},
                assistant_phase=AssistantResponsePhase.COMMENTARY,
            ),
        ],
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement search command and update tests.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 3
    assert not surface.errors
    # The failed edit supplies execution evidence; unresolved work stays visible
    # after bounded correction, while preserving the model's own summary.
    assert surface.final_messages[-1].startswith(final_progress_text)
    assert "No files were changed" in surface.final_messages[-1]
    assert len(_event_payloads(log_path, "continuation_nudge")) == 1
    assert _event_payloads(log_path, "completion_gate_accepted_with_open_problems")
    assert _event_payloads(log_path, "one_shot_incomplete_after_retries") == []
    assert _event_payloads(log_path, "forced_final_summary_requested") == []
    assert _event_payloads(log_path, "forced_final_summary_completed") == []


@pytest.mark.parametrize("one_shot_execution", [False, True])
def test_completion_gate_open_problems_accepts_second_final(
    tmp_path: Path,
    one_shot_execution: bool,
) -> None:
    sessions_dir = tmp_path / "sessions"
    surface = _ForcedSummarySurface()
    session = create_session(
        cfg=AppConfig(model=SMOKE_MODEL, routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot_execution,
        enable_chat_turn_step_budget=True,
        session_log_dir_override=sessions_dir,
        session_id_override="forced-summary-completion-gate-terminal",
        surface=surface,
    )
    latest_final_text = "Implemented the requested code change."
    client = _ScriptedClient(
        [
            _failed_edit_response(),
            LLMResponse(content=latest_final_text, tool_calls=[], raw={}),
            LLMResponse(content=latest_final_text, tool_calls=[], raw={}),
        ],
    )
    session.client = client  # type: ignore[assignment]

    try:
        exit_code = session.run_turn("Implement the requested code change.")
        log_path = session.store.path
    finally:
        session.close()

    assert exit_code == 0
    assert len(client.calls) == 3
    assert not surface.errors
    # The failed edit supplies execution evidence; unresolved work stays visible
    # after bounded correction, while preserving the model's own summary.
    assert surface.final_messages[-1].startswith(latest_final_text)
    assert "No files were changed" in surface.final_messages[-1]
    assert len(_event_payloads(log_path, "completion_gate_nudge")) == 1
    accepted_events = _event_payloads(log_path, "completion_gate_accepted_with_open_problems")
    assert accepted_events
    assert accepted_events[-1]["stage"] == "no_material_edits"
    assert _event_payloads(log_path, "one_shot_completion_gate_incomplete_after_retries") == []
    assert _event_payloads(log_path, "forced_final_summary_requested") == []
    assert _event_payloads(log_path, "forced_final_summary_completed") == []
