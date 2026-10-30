from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import alysis_code.agent.turn.core as turn_core
import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.empty_response_stall import (
    DEFAULT_HANDLING_BUDGET_SECONDS,
    DEFAULT_MAX_RECOVERY_CYCLES,
    DEFAULT_STALL_SECONDS,
    DEFAULT_STALL_THRESHOLD,
    EmptyResponseStallPolicy,
    EmptyResponseStallTracker,
    compact_recent_tool_output,
    resolve_empty_response_stall_policy,
    response_is_contentless,
)
from alysis_code.agent.turn.core import _salvage_summary
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult

_VERIFY_OK_COMMAND = "pytest tests/test_cli.py -q"


class _FakeClock:
    """Monotonic clock the tests advance explicitly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StallingClient:
    """Answers a scripted prefix, then returns contentless responses forever.

    Models the reported failure: an endpoint that keeps accepting requests but
    stops producing anything the runtime can act on.
    """

    model = "test-model"
    temperature = 0.2

    def __init__(self, prefix: list[LLMResponse]) -> None:
        self._prefix = prefix
        self.calls = 0
        self.call_records: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta: Any | None = None,
        on_reasoning_delta: Any | None = None,
        temperature: float | None = None,
        cancellation_token: Any | None = None,
    ) -> LLMResponse:
        _ = (
            on_text_delta,
            on_reasoning_delta,
            temperature,
            tools,
            tool_choice,
            stream,
            cancellation_token,
        )
        self.call_records.append({"messages": [dict(item) for item in messages]})
        index = self.calls
        self.calls += 1
        if index < len(self._prefix):
            return self._prefix[index]
        return LLMResponse(content="", tool_calls=[], raw={})


class _ScriptedClient:
    """Returns a fixed script; raises if the turn asks for more than scripted."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        stream: bool = False,
        on_text_delta: Any | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, tool_choice, stream, on_text_delta, temperature
        if self.calls >= len(self._responses):
            raise AssertionError("scripted response exhausted")
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _FakeTerminalManager:
    def list(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(status="running"),)

    def shutdown_all(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_real_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record recovery backoff instead of sleeping through it."""
    slept: list[float] = []
    monkeypatch.setattr(turn_core, "sleep", lambda seconds: slept.append(float(seconds)))
    return slept


@pytest.fixture(autouse=True)
def _fake_verify_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the configured verification command as passing without running it."""

    def fake_run_task_verification(
        *,
        root: Path,
        commands: list[str],
        artifact_path: Path,
        cfg: AppConfig,
    ) -> VerifyRunResult:
        _ = root, cfg
        command_results = [
            VerifyCommandResult(command=command, exit_code=0, output="ok\n", real_execution=True)
            for command in commands
        ]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("ok\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=command_results,
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(agent_loop_mod, "run_task_verification", fake_run_task_verification)
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind == "verify_run"),
    )


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir(exist_ok=True)
    for args in (
        ["init"],
        ["config", "user.name", "Test User"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(
            ["git", "-C", os.fspath(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", os.fspath(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _events_of(sessions_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return list(read_session_events(sessions_dir / f"{session_id}.jsonl"))


def _payloads(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in events if event.get("type") == event_type]


def _run_stalling_turn(
    tmp_path: Path,
    *,
    session_id: str,
    prefix: list[LLMResponse],
    cfg: AppConfig | None = None,
    max_steps: int = 12,
    terminal_manager: Any | None = None,
) -> tuple[int, list[dict[str, Any]], _StallingClient]:
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=cfg or AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=max_steps,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    if terminal_manager is not None:
        session.terminal_manager = terminal_manager
    client = _StallingClient(prefix)
    session.client = client  # type: ignore[assignment]
    try:
        exit_code = session.run_turn("Count the lines in data.txt and save it to answer.txt.")
    finally:
        session.close()
    return exit_code, _events_of(sessions_dir, session_id), client


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------


def test_only_responses_without_text_and_without_tool_calls_are_contentless() -> None:
    tool_call = ToolCall(id="tc1", name="fs_read", arguments={"path": "a.txt"})

    assert response_is_contentless(LLMResponse(content="", tool_calls=[], raw={})) is True
    assert response_is_contentless(LLMResponse(content="   \n", tool_calls=[], raw={})) is True
    assert response_is_contentless(None) is True
    # A tool-call-only response is the ordinary shape of a working step.
    assert response_is_contentless(LLMResponse(content="", tool_calls=[tool_call], raw={})) is False
    assert response_is_contentless(LLMResponse(content="done", tool_calls=[], raw={})) is False


# ---------------------------------------------------------------------------
# Stall tracking
# ---------------------------------------------------------------------------


def test_stall_is_declared_at_the_consecutive_threshold() -> None:
    clock = _FakeClock()
    tracker = EmptyResponseStallTracker(
        policy=EmptyResponseStallPolicy(consecutive_threshold=3),
        clock=clock,
    )

    first = tracker.observe(contentless=True)
    second = tracker.observe(contentless=True)
    third = tracker.observe(contentless=True)

    assert (first.stalled, second.stalled) == (False, False)
    assert third.stalled is True
    assert third.trigger == "consecutive_contentless_responses"
    assert third.consecutive_contentless == 3


def test_stall_is_declared_on_streak_duration_before_the_count_threshold() -> None:
    clock = _FakeClock()
    tracker = EmptyResponseStallTracker(
        policy=EmptyResponseStallPolicy(consecutive_threshold=10, stall_seconds=300.0),
        clock=clock,
    )

    assert tracker.observe(contentless=True).stalled is False
    clock.advance(301.0)
    signal = tracker.observe(contentless=True)

    assert signal.stalled is True
    assert signal.trigger == "contentless_streak_duration"
    assert signal.consecutive_contentless == 2


def test_a_usable_response_clears_the_streak_and_stops_billing() -> None:
    clock = _FakeClock()
    tracker = EmptyResponseStallTracker(
        policy=EmptyResponseStallPolicy(consecutive_threshold=3),
        clock=clock,
    )

    tracker.observe(contentless=True)
    tracker.observe(contentless=True)
    clock.advance(30.0)
    tracker.observe(contentless=False)
    clock.advance(600.0)

    assert tracker.consecutive_contentless == 0
    assert tracker.observe(contentless=True).stalled is False
    # Only the contentless streak was charged, not the 600s of ordinary work.
    assert tracker.handling_seconds == pytest.approx(30.0)


def test_recovery_cycles_are_capped_per_session() -> None:
    tracker = EmptyResponseStallTracker(
        policy=EmptyResponseStallPolicy(max_recovery_cycles=2),
        clock=_FakeClock(),
    )

    first = tracker.plan_recovery()
    tracker.note_recovery_started(backoff_seconds=first.backoff_seconds)
    second = tracker.plan_recovery()
    tracker.note_recovery_started(backoff_seconds=second.backoff_seconds)
    third = tracker.plan_recovery()

    assert [first.allowed, second.allowed, third.allowed] == [True, True, False]
    assert third.reason == "recovery_cycles_exhausted"
    assert second.backoff_seconds > first.backoff_seconds


def test_recovery_is_refused_once_the_handling_budget_is_spent() -> None:
    clock = _FakeClock()
    tracker = EmptyResponseStallTracker(
        policy=EmptyResponseStallPolicy(
            consecutive_threshold=2,
            max_recovery_cycles=5,
            handling_budget_seconds=600.0,
        ),
        clock=clock,
    )

    tracker.observe(contentless=True)
    clock.advance(700.0)
    tracker.observe(contentless=True)
    plan = tracker.plan_recovery()

    assert plan.allowed is False
    assert plan.reason == "handling_budget_exhausted"
    assert tracker.remaining_budget_seconds() == 0.0


def test_disabled_policy_never_declares_a_stall_or_allows_recovery() -> None:
    tracker = EmptyResponseStallTracker(
        policy=EmptyResponseStallPolicy(enabled=False, consecutive_threshold=1),
        clock=_FakeClock(),
    )

    signal = tracker.observe(contentless=True)

    assert signal.stalled is False
    assert tracker.plan_recovery().reason == "stall_guard_disabled"


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


def test_policy_defaults_come_from_config() -> None:
    policy = resolve_empty_response_stall_policy(AppConfig(model="test-model"))

    assert policy.enabled is True
    assert policy.consecutive_threshold == DEFAULT_STALL_THRESHOLD
    assert policy.stall_seconds == DEFAULT_STALL_SECONDS
    assert policy.max_recovery_cycles == DEFAULT_MAX_RECOVERY_CYCLES
    assert policy.handling_budget_seconds == DEFAULT_HANDLING_BUDGET_SECONDS


def test_policy_reads_configured_thresholds() -> None:
    policy = resolve_empty_response_stall_policy(
        AppConfig(
            model="test-model",
            empty_response_stall_threshold=5,
            empty_response_stall_seconds=45.0,
            empty_response_max_recovery_cycles=1,
            empty_response_handling_budget_seconds=90.0,
        )
    )

    assert policy.consecutive_threshold == 5
    assert policy.stall_seconds == 45.0
    assert policy.max_recovery_cycles == 1
    assert policy.handling_budget_seconds == 90.0


def test_env_kill_switch_disables_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_EMPTY_RESPONSE_STALL", "off")
    assert resolve_empty_response_stall_policy(AppConfig(model="test-model")).enabled is False

    monkeypatch.setenv("ALYSIS_EMPTY_RESPONSE_STALL", "on")
    disabled_cfg = AppConfig(model="test-model", empty_response_stall_guard_enabled=False)
    assert resolve_empty_response_stall_policy(disabled_cfg).enabled is True


def test_unusable_threshold_values_fall_back_to_defaults() -> None:
    class _BadCfg:
        empty_response_stall_threshold = 0
        empty_response_stall_seconds = "not-a-number"
        empty_response_max_recovery_cycles = -1
        empty_response_handling_budget_seconds = float("inf")

    policy = resolve_empty_response_stall_policy(_BadCfg())

    assert policy.consecutive_threshold == DEFAULT_STALL_THRESHOLD
    assert policy.stall_seconds == DEFAULT_STALL_SECONDS
    assert policy.max_recovery_cycles == DEFAULT_MAX_RECOVERY_CYCLES
    assert policy.handling_budget_seconds == DEFAULT_HANDLING_BUDGET_SECONDS


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------


def test_compaction_elides_the_recent_tool_block_and_keeps_call_pairing() -> None:
    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "x" * 5000},
        {"role": "tool", "tool_call_id": "tc2", "content": "y" * 4000},
        {"role": "system", "content": "controller nudge appended after the tools"},
    ]

    compacted, report = compact_recent_tool_output(messages, keep_chars=100)

    assert report.applied is True
    assert report.block_size == 2
    assert report.compacted_messages == 2
    assert report.removed_characters == (5000 - 100) + (4000 - 100)
    assert [item["role"] for item in compacted] == [item["role"] for item in messages]
    assert [item.get("tool_call_id") for item in compacted[2:4]] == ["tc1", "tc2"]
    assert compacted[2]["content"].startswith("x" * 100)
    assert "elided by the runtime" in compacted[2]["content"]
    # The original list is left untouched.
    assert messages[2]["content"] == "x" * 5000


def test_compaction_keeps_short_tool_results_verbatim() -> None:
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
    ]

    compacted, report = compact_recent_tool_output(messages, keep_chars=100)

    assert report.applied is False
    assert report.block_size == 1
    assert compacted[1]["content"] == "ok"


def test_compaction_only_touches_the_most_recent_block() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "old", "content": "a" * 900},
        {"role": "assistant", "content": "thinking"},
        {"role": "tool", "tool_call_id": "new", "content": "b" * 900},
    ]

    compacted, report = compact_recent_tool_output(messages, keep_chars=10)

    assert report.block_size == 1
    assert compacted[0]["content"] == "a" * 900
    assert compacted[2]["content"].startswith("b" * 10)


def test_compaction_is_a_no_op_without_tool_messages() -> None:
    messages = [{"role": "user", "content": "hello"}]

    compacted, report = compact_recent_tool_output(messages)

    assert report.applied is False
    assert report.block_size == 0
    assert compacted == messages


# ---------------------------------------------------------------------------
# Turn behaviour
# ---------------------------------------------------------------------------


def test_completed_empty_openai_stream_reaches_agent_recovery(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "openai-completed-empty-recovery"
    session = create_session(
        cfg=AppConfig(model="gpt-5.5", routing_mode="code_only", stream=True),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    calls = 0
    final_text = "The repository inspection is complete."

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/responses/input_tokens"):
            return httpx.Response(
                200,
                json={"object": "response.input_tokens", "input_tokens": 1},
            )
        calls += 1
        if calls == 1:
            response = {
                "id": "resp_empty",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [],
            }
        else:
            response = {
                "id": "resp_final",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": final_text}],
                    }
                ],
            }
        event = {"type": "response.completed", "response": response}
        payload = (
            f"event: response.completed\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        ).encode()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=payload,
        )

    session.client = OpenAIResponsesClient(  # type: ignore[assignment]
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.5",
        transport=httpx.MockTransport(handler),
    )

    try:
        exit_code = session.run_turn("Inspect the repository and report what you find.")
    finally:
        session.close()

    events = _events_of(sessions_dir, session_id)
    assert exit_code == 0
    assert calls >= 2
    assert _payloads(events, "empty_model_response_recovery")
    assert not _payloads(events, "error")
    final_events = _payloads(events, "final")
    assert final_events[-1]["content"].startswith(final_text)


def test_stall_is_detected_at_the_threshold_during_a_turn(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    _, events, _ = _run_stalling_turn(
        tmp_path,
        session_id="stall-detected-at-threshold",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
                raw={},
            )
        ],
    )

    detections = _payloads(events, "empty_response_stall_detected")
    assert detections, "expected the stall guard to fire"
    first = detections[0]
    assert first["trigger"] == "consecutive_contentless_responses"
    assert first["consecutive_contentless"] == DEFAULT_STALL_THRESHOLD
    assert first["policy"]["consecutive_threshold"] == DEFAULT_STALL_THRESHOLD


def test_a_slow_endpoint_stalls_on_elapsed_time_before_the_count_threshold(
    tmp_path: Path,
) -> None:
    """The failure that cost hours: few empty responses, each taking over ten minutes.

    The count threshold alone never bounded wall-clock time. With a slow endpoint
    the streak-duration rule fires first, so the turn stops on the second empty
    response instead of waiting for a third that is minutes away.
    """
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override="stall-slow-endpoint",
    )
    clock = _FakeClock()
    session.empty_response_stall_tracker = EmptyResponseStallTracker(
        policy=EmptyResponseStallPolicy(consecutive_threshold=10, stall_seconds=300.0),
        clock=clock,
    )

    class _SlowStallingClient(_StallingClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            # One real provider attempt consumes the entire handling budget.
            # This must not rely on compatibility retries replaying the call.
            clock.advance(601.0)
            return super().chat(**kwargs)

    client = _SlowStallingClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
                raw={},
            )
        ]
    )
    session.client = client  # type: ignore[assignment]

    try:
        session.run_turn("Count the lines in data.txt and save it to answer.txt.")
    finally:
        session.close()

    events = _events_of(sessions_dir, "stall-slow-endpoint")
    detections = _payloads(events, "empty_response_stall_detected")

    assert detections
    assert detections[0]["trigger"] == "contentless_streak_duration"
    # Two contentless responses, not the count threshold of ten.
    assert detections[0]["consecutive_contentless"] == 2
    assert _payloads(events, "empty_response_stall_salvage")
    # The 10-minute handling budget also stops the session re-asking: the second
    # empty response alone spent it, so no recovery cycle was granted.
    assert not _payloads(events, "empty_response_stall_recovery")
    assert client.calls <= 4


def test_one_recovery_is_attempted_then_the_turn_salvages(
    tmp_path: Path,
    _no_real_backoff_sleep: list[float],
) -> None:
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    cfg = AppConfig(
        model="test-model",
        routing_mode="code_only",
        empty_response_max_recovery_cycles=1,
    )

    exit_code, events, client = _run_stalling_turn(
        tmp_path,
        session_id="stall-single-recovery-then-salvage",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
                raw={},
            )
        ],
        cfg=cfg,
    )

    recoveries = _payloads(events, "empty_response_stall_recovery")
    salvages = _payloads(events, "empty_response_stall_salvage")
    assert len(recoveries) == 1
    assert recoveries[0]["cycle"] == 1
    # This fixture's tool output is already small, so there is no bulk to drop;
    # the recovery still re-issues, and says so honestly.
    assert recoveries[0]["compaction"]["applied"] is False
    assert len(salvages) == 1
    assert salvages[0]["stop_reason"] == "empty_response_anomaly_retry_exhausted"
    assert salvages[0]["material_work_persisted"] is False
    assert exit_code == 0
    final = _payloads(events, "final")[-1]
    assert final["stop_reason"] == "empty_response_anomaly_retry_exhausted"
    assert final["degraded"] is True
    assert final["internal_fallback"] is True
    assert final["internal_fallback_kind"] == "empty_response_stall_salvage"
    assert not (tmp_path / "answer.txt").exists()
    # The recovery backed off before re-issuing, and the turn stopped instead of
    # re-asking indefinitely.
    assert _no_real_backoff_sleep == [pytest.approx(2.0)]
    assert client.calls < 10


def test_recovery_cycles_stay_within_the_configured_cap(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    _, events, _ = _run_stalling_turn(
        tmp_path,
        session_id="stall-recovery-cap",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
                raw={},
            )
        ],
    )

    recoveries = _payloads(events, "empty_response_stall_recovery")
    assert 1 <= len(recoveries) <= DEFAULT_MAX_RECOVERY_CYCLES
    assert [item["cycle"] for item in recoveries] == list(range(1, len(recoveries) + 1))
    assert _payloads(events, "empty_response_stall_salvage")


def test_recovery_reissues_against_a_compacted_context(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("alpha\n" * 4000, encoding="utf-8")

    _, events, client = _run_stalling_turn(
        tmp_path,
        session_id="stall-recovery-compacts-context",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
                raw={},
            )
        ],
    )

    recoveries = _payloads(events, "empty_response_stall_recovery")
    assert recoveries and recoveries[0]["compaction"]["removed_characters"] > 0

    def _tool_payload_size(record: dict[str, Any]) -> int:
        return sum(
            len(str(message.get("content") or ""))
            for message in record["messages"]
            if message.get("role") == "tool"
        )

    before = _tool_payload_size(client.call_records[1])
    after = _tool_payload_size(client.call_records[-1])
    assert after < before
    # Every tool result still answers its call, so the request stays valid.
    assert all(
        message.get("tool_call_id")
        for message in client.call_records[-1]["messages"]
        if message.get("role") == "tool"
    )


def test_salvage_exits_zero_and_reports_the_work_left_in_the_working_tree(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    (repo / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    exit_code, events, _ = _run_stalling_turn(
        repo,
        session_id="stall-salvage-keeps-work",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "2\n"},
                    )
                ],
                raw={},
            )
        ],
    )

    salvages = _payloads(events, "empty_response_stall_salvage")
    degraded = _payloads(events, "session_degraded")
    assert exit_code == 0
    assert salvages and salvages[0]["material_work_persisted"] is True
    assert "answer.txt" in salvages[0]["salvaged_paths"]
    assert salvages[0]["salvage_evidence_sources"] == ["git_diff", "touched_paths"]
    assert degraded and degraded[0]["reason"] == "empty_response_stall"
    # The edit is still on disk; salvage keeps work rather than discarding it.
    assert (repo / "answer.txt").read_text(encoding="utf-8") == "2\n"
    final_payloads = _payloads(events, "final")
    assert final_payloads and final_payloads[-1].get("degraded") is True
    assert "working tree" in final_payloads[-1]["content"]


def test_stall_after_optional_check_preserves_the_gate_clear_answer(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    (repo / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    final_text = "Created answer.txt containing the requested line count."

    exit_code, events, _ = _run_stalling_turn(
        repo,
        session_id="stall-preserves-gate-clear-answer",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "2\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content=final_text, tool_calls=[], raw={}),
        ],
        terminal_manager=_FakeTerminalManager(),
    )

    assert exit_code == 0
    final_payloads = _payloads(events, "final")
    assert final_payloads[-1]["content"] == final_text
    assert final_payloads[-1]["preserved_gate_clear_answer"] is True
    assert final_payloads[-1]["degraded_reason"] == "optional_finalization_check_stalled"
    interventions = _payloads(events, "controller_intervention")
    assert any(
        item.get("detail") == "gate_clear_answer_preserved_after_empty_response_stall"
        for item in interventions
    )


def test_salvage_summary_reports_runtime_outcomes_separately_from_files() -> None:
    summary = _salvage_summary(
        headline="The endpoint stopped.",
        salvaged_paths=[],
        durable_service_ids=["svc_ready"],
        material_edit_count=1,
        verification_attempt_count=0,
        missing_action="report the final result",
        stop_reason="empty_response_stall",
    )

    assert "No file changes were found" in summary
    assert "Durable services left running: svc_ready" in summary
    assert "Changes left in the working tree" not in summary


@pytest.mark.parametrize(
    "artifact_path", [None, "result.outcome.json", "nested/result.outcome.json"]
)
def test_salvage_distinguishes_session_outcomes_from_user_artifacts(
    tmp_path: Path, artifact_path: str | None
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    (repo / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", os.fspath(repo), "add", "data.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "data"],
        check=True,
        capture_output=True,
        text=True,
    )

    exit_code, events, _ = _run_stalling_turn(
        repo,
        session_id="stall-salvage-nothing-produced",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
                raw={},
            ),
            *(
                [
                    LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                id="write",
                                name="fs_write",
                                arguments={
                                    "path": artifact_path,
                                    "content": '{"observed_lines": 2}\n',
                                },
                            )
                        ],
                        raw={},
                    )
                ]
                if artifact_path
                else []
            ),
        ],
    )

    salvages = _payloads(events, "empty_response_stall_salvage")
    # A bounded self-stop exits cleanly; it must still expose incomplete work.
    assert exit_code == 0
    assert salvages and salvages[0]["material_work_persisted"] is bool(artifact_path)
    assert salvages[0]["salvaged_paths"] == ([artifact_path] if artifact_path else [])
    assert salvages[0]["salvage_evidence_sources"] == (
        ["git_diff", "touched_paths"] if artifact_path else ["git_diff"]
    )
    assert salvages[0]["stop_reason"] == "empty_response_anomaly_retry_exhausted"
    final = _payloads(events, "final")[-1]
    assert final["stop_reason"] == "empty_response_anomaly_retry_exhausted"
    assert final["degraded"] is True
    assert final["internal_fallback"] is True
    assert final["internal_fallback_kind"] == "empty_response_stall_salvage"
    assert not (repo / "answer.txt").exists()


def test_salvage_falls_back_to_touched_paths_outside_a_git_repository(
    tmp_path: Path,
) -> None:
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    exit_code, events, _ = _run_stalling_turn(
        tmp_path,
        session_id="stall-salvage-without-git",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "2\n"},
                    )
                ],
                raw={},
            )
        ],
    )

    salvages = _payloads(events, "empty_response_stall_salvage")
    assert exit_code == 0
    assert salvages[0]["salvage_evidence_sources"] == ["touched_paths"]
    assert "answer.txt" in salvages[0]["salvaged_paths"]


def test_kill_switch_disables_stall_recovery_but_preserves_clean_stop_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_EMPTY_RESPONSE_STALL", "off")
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    (repo / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    exit_code, events, _ = _run_stalling_turn(
        repo,
        session_id="stall-guard-disabled",
        prefix=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "2\n"},
                    )
                ],
                raw={},
            )
        ],
    )

    # The switch disables shared stall recovery, not the clean-stop exit policy.
    assert exit_code == 0
    assert (repo / "answer.txt").read_text(encoding="utf-8") == "2\n"
    event_types = {event.get("type") for event in events}
    assert "empty_response_stall_detected" not in event_types
    assert "empty_response_stall_recovery" not in event_types
    assert "empty_response_stall_salvage" not in event_types
    assert "session_degraded" not in event_types
    assert _payloads(events, "empty_model_response_anomaly_incomplete_after_retries")
    final = _payloads(events, "final")[-1]
    assert final["stop_reason"] == "empty_response_anomaly_retry_exhausted"
    assert final["internal_fallback"] is True
    assert final["internal_fallback_kind"] == "empty_response_anomaly"


def test_the_recovery_budget_is_shared_across_turns_in_one_session(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override="stall-budget-per-session",
    )
    read_call = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
        raw={},
    )

    try:
        session.client = _StallingClient([read_call])  # type: ignore[assignment]
        session.run_turn("Count the lines in data.txt.")
        first_turn_events = len(_events_of(sessions_dir, "stall-budget-per-session"))
        session.client = _StallingClient([read_call])  # type: ignore[assignment]
        session.run_turn("Try again please.")
    finally:
        session.close()

    events = _events_of(sessions_dir, "stall-budget-per-session")
    second_turn = events[first_turn_events:]

    assert _payloads(events, "empty_response_stall_recovery"), "first turn should have recovered"
    # The budget is spent, so the second turn salvages without re-spending it.
    assert not _payloads(second_turn, "empty_response_stall_recovery")
    second_salvages = _payloads(second_turn, "empty_response_stall_salvage")
    assert len(second_salvages) == 1
    assert second_salvages[0]["recovery_cycles_used"] == DEFAULT_MAX_RECOVERY_CYCLES


def test_normal_responses_are_unaffected(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            verify_commands=[_VERIFY_OK_COMMAND],
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=7,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=sessions_dir,
        session_id_override="stall-guard-normal-turn",
    )
    session.client = _ScriptedClient(  # type: ignore[assignment]
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fs_read", arguments={"path": "data.txt"})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="fs_write",
                        arguments={"path": "answer.txt", "content": "2\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc3",
                        name="verify_run",
                        arguments={"commands": [_VERIFY_OK_COMMAND]},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Wrote answer.txt and verified it.", tool_calls=[], raw={}),
        ]
    )

    try:
        exit_code = session.run_turn("Count the lines in data.txt and save it to answer.txt.")
    finally:
        session.close()

    events = _events_of(sessions_dir, "stall-guard-normal-turn")
    event_types = {event.get("type") for event in events}

    assert exit_code == 0
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "2\n"
    # Tool-call-only responses carry no text but are never treated as a stall.
    assert "empty_response_stall_detected" not in event_types
    assert "empty_response_stall_recovery" not in event_types
    assert "session_degraded" not in event_types
