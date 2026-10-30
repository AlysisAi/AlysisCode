"""Phased degradation of the wall-clock run budget.

Every time-based rule here is driven by an injected clock, so the thresholds are
exercised without sleeping and without depending on machine speed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig, ConfigError, resolve_run_deadline
from alysis_code.execution_deadline import (
    DEFAULT_RUN_DEADLINE_SECONDS,
    DeadlineDegradationPolicy,
    DeadlineOperation,
    DeadlinePhase,
    ExecutionDeadline,
    resolve_deadline_degradation_policy,
)
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _budget(
    seconds: float | None,
    clock: _FakeClock,
    **policy_kwargs: Any,
) -> ExecutionDeadline:
    return ExecutionDeadline.from_duration(
        seconds,
        clock=clock,
        degradation_policy=DeadlineDegradationPolicy(**policy_kwargs),
    )


def _event_payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.get("payload") or {})
        for event in read_session_events(path)
        if event.get("type") == event_type
    ]


def _allows(
    deadline: ExecutionDeadline,
    operation: DeadlineOperation,
    *,
    allow_during_finalization: bool = True,
) -> bool:
    return deadline.start_decision(
        operation,
        minimum_remaining_seconds=0.05,
        allow_during_finalization=allow_during_finalization,
    ).allowed


# ---------------------------------------------------------------------------
# (a) phase transitions fire at the thresholds
# ---------------------------------------------------------------------------


def test_stages_advance_at_seventy_five_and_ninety_percent_of_budget() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)

    assert deadline.degradation_stage() is DeadlinePhase.NORMAL

    clock.advance(749.0)
    assert deadline.degradation_stage() is DeadlinePhase.NORMAL

    clock.advance(1.0)
    assert deadline.elapsed_fraction() == pytest.approx(0.75)
    assert deadline.degradation_stage() is DeadlinePhase.CONVERGENCE

    clock.advance(149.0)
    assert deadline.degradation_stage() is DeadlinePhase.CONVERGENCE

    clock.advance(1.0)
    assert deadline.elapsed_fraction() == pytest.approx(0.90)
    assert deadline.degradation_stage() is DeadlinePhase.WRAP_UP


def test_convergence_closes_new_work_but_keeps_editing_available() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)
    clock.advance(760.0)

    assert deadline.degradation_stage() is DeadlinePhase.CONVERGENCE
    assert _allows(deadline, DeadlineOperation.SUBAGENT) is False
    assert _allows(deadline, DeadlineOperation.SHELL_BACKGROUND) is False
    # Convergence is "finish what you started", so the tools that finish work
    # stay open.
    assert _allows(deadline, DeadlineOperation.MUTATION_TOOL) is True
    assert _allows(deadline, DeadlineOperation.EXPLORATION_TOOL) is True
    assert _allows(deadline, DeadlineOperation.VERIFICATION) is True


def test_wrap_up_closes_editing_but_keeps_verification_and_summary_available() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)
    clock.advance(910.0)

    assert deadline.degradation_stage() is DeadlinePhase.WRAP_UP
    assert _allows(deadline, DeadlineOperation.MUTATION_TOOL) is False
    assert _allows(deadline, DeadlineOperation.EXPLORATION_TOOL) is False
    assert _allows(deadline, DeadlineOperation.SUBAGENT) is False
    # Wrap-up still has to verify and report, so these must survive it.
    assert _allows(deadline, DeadlineOperation.VERIFICATION) is True
    assert _allows(deadline, DeadlineOperation.SHELL_TOOL) is True
    assert _allows(deadline, DeadlineOperation.MAIN_LLM) is True
    # Blocking dispatch itself would terminate the turn rather than degrade it.
    assert _allows(deadline, DeadlineOperation.TOOL_DISPATCH) is True


def test_blocked_operation_reports_the_budget_reason() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)
    clock.advance(910.0)

    decision = deadline.start_decision(
        DeadlineOperation.MUTATION_TOOL,
        minimum_remaining_seconds=0.05,
        allow_during_finalization=True,
    )

    assert decision.allowed is False
    assert decision.reason == "budget_degradation_disallows_operation"
    assert decision.phase is DeadlinePhase.WRAP_UP


def test_each_stage_is_reported_once() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)

    assert deadline.maybe_enter_degradation_stage() is None

    clock.advance(760.0)
    assert deadline.maybe_enter_degradation_stage() is DeadlinePhase.CONVERGENCE
    assert deadline.maybe_enter_degradation_stage() is None

    clock.advance(150.0)
    assert deadline.maybe_enter_degradation_stage() is DeadlinePhase.WRAP_UP
    assert deadline.maybe_enter_degradation_stage() is None
    assert deadline.degradation_stages_entered == ("convergence", "wrap_up")


def test_a_skipped_stage_still_applies_its_restrictions() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)

    # One long operation can span both thresholds; the run must land in wrap-up
    # with the convergence restrictions still in force.
    clock.advance(950.0)

    assert deadline.maybe_enter_degradation_stage() is DeadlinePhase.WRAP_UP
    assert _allows(deadline, DeadlineOperation.SUBAGENT) is False
    assert _allows(deadline, DeadlineOperation.MUTATION_TOOL) is False


def test_wrap_up_restrictions_survive_the_finalization_window() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)
    # A slow model call pushes the finalization reserve out to its 25% cap, so
    # the reserve window and wrap-up overlap.
    deadline.observe_duration(DeadlineOperation.MAIN_LLM, 200.0)
    clock.advance(910.0)

    assert deadline.phase() is DeadlinePhase.FINALIZATION_WINDOW
    # The finalization window normally carves out editing so required outputs
    # can be written. That carve-out must not reopen what wrap-up closed.
    assert _allows(deadline, DeadlineOperation.MUTATION_TOOL) is False


def test_finalization_window_before_wrap_up_still_allows_editing() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)
    deadline.observe_duration(DeadlineOperation.MAIN_LLM, 200.0)
    clock.advance(885.0)

    # A slow model pushes the reserve to its 120s cap, so the window opens at
    # 88.5% elapsed - before wrap-up. The pre-existing carve-out applies there,
    # which is what keeps the ladder monotonic rather than merely stricter.
    assert deadline.phase() is DeadlinePhase.FINALIZATION_WINDOW
    assert deadline.degradation_stage() is DeadlinePhase.CONVERGENCE
    assert _allows(deadline, DeadlineOperation.MUTATION_TOOL) is True


def test_stages_are_measured_against_an_inherited_budget_span() -> None:
    clock = _FakeClock(50.0)
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=50.0,
        deadline_monotonic=150.0,
        clock=clock,
        degradation_policy=DeadlineDegradationPolicy(),
    )

    clock.advance(80.0)
    assert deadline.elapsed_fraction() == pytest.approx(0.80)
    assert deadline.degradation_stage() is DeadlinePhase.CONVERGENCE


# ---------------------------------------------------------------------------
# (c) unlimited mode never degrades
# ---------------------------------------------------------------------------


def test_unlimited_budget_never_degrades() -> None:
    clock = _FakeClock()
    deadline = _budget(None, clock)

    clock.advance(10_000_000.0)

    assert deadline.enabled is False
    assert deadline.elapsed_fraction() is None
    assert deadline.degradation_stage() is DeadlinePhase.NORMAL
    assert deadline.phase() is DeadlinePhase.NORMAL
    assert deadline.maybe_enter_degradation_stage() is None
    assert deadline.degradation_blocked_operations() == frozenset()
    for operation in DeadlineOperation:
        assert _allows(deadline, operation) is True


def test_disabled_degradation_keeps_the_budget_but_drops_the_stages() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock, enabled=False)
    clock.advance(950.0)

    assert deadline.degradation_stage() is DeadlinePhase.NORMAL
    assert _allows(deadline, DeadlineOperation.MUTATION_TOOL) is True
    assert _allows(deadline, DeadlineOperation.SUBAGENT) is True
    # The hard budget still ends the run.
    clock.advance(50.0)
    assert deadline.is_exhausted() is True


def test_kill_switch_disables_degradation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_RUN_BUDGET_DEGRADATION", "off")

    policy = resolve_deadline_degradation_policy(AppConfig(model="test-model"))

    assert policy.enabled is False


def test_inverted_thresholds_collapse_instead_of_inverting() -> None:
    policy = DeadlineDegradationPolicy(
        convergence_fraction=0.95,
        wrap_up_fraction=0.80,
    ).normalized()

    assert policy.convergence_fraction == 0.80
    assert policy.wrap_up_fraction == 0.80


@pytest.mark.parametrize("bad", [0.0, -1.0, 1.5, float("nan"), "abc", None])
def test_invalid_thresholds_fall_back_to_defaults(bad: Any) -> None:
    policy = DeadlineDegradationPolicy(
        convergence_fraction=bad,
        wrap_up_fraction=bad,
    ).normalized()

    assert policy.convergence_fraction == 0.75
    assert policy.wrap_up_fraction == 0.90


# ---------------------------------------------------------------------------
# Budget resolution: default, precedence, explicit unlimited
# ---------------------------------------------------------------------------


def test_budget_defaults_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)

    resolved = resolve_run_deadline(
        AppConfig(model="test-model"),
        default_seconds=DEFAULT_RUN_DEADLINE_SECONDS,
    )

    assert resolved.seconds == 3600.0
    assert resolved.source == "runtime_default"
    assert resolved.unlimited is False


@pytest.mark.parametrize("token", ["unlimited", "never", "off", "none"])
def test_environment_can_select_unlimited_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    monkeypatch.setenv("ALYSIS_RUN_DEADLINE_SECONDS", token)

    resolved = resolve_run_deadline(
        AppConfig(model="test-model"),
        default_seconds=DEFAULT_RUN_DEADLINE_SECONDS,
    )

    # Selecting unlimited has to stop the search, or the default would win and
    # there would be no way to turn it off.
    assert resolved.unlimited is True
    assert resolved.source == "environment"


def test_no_deadline_flag_selects_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)

    resolved = resolve_run_deadline(
        AppConfig(model="test-model"),
        cli_no_deadline=True,
        default_seconds=DEFAULT_RUN_DEADLINE_SECONDS,
    )

    assert resolved.unlimited is True
    assert resolved.source == "explicit_cli"


def test_no_deadline_flag_outranks_a_configured_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_RUN_DEADLINE_SECONDS", "300")

    resolved = resolve_run_deadline(
        AppConfig(model="test-model", run_deadline_seconds=120.0),
        cli_no_deadline=True,
        default_seconds=DEFAULT_RUN_DEADLINE_SECONDS,
    )

    assert resolved.unlimited is True


def test_config_can_select_unlimited_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)

    resolved = resolve_run_deadline(
        AppConfig(model="test-model", run_deadline_unlimited=True),
        default_seconds=DEFAULT_RUN_DEADLINE_SECONDS,
    )

    assert resolved.unlimited is True
    assert resolved.source == "config"


def test_setting_run_deadline_to_a_word_records_the_unlimited_choice() -> None:
    from alysis_code.config import set_config_value

    cfg = set_config_value(AppConfig(model="test-model"), "run_deadline_seconds", "unlimited")

    assert cfg.run_deadline_seconds is None
    assert cfg.run_deadline_unlimited is True

    cfg = set_config_value(cfg, "run_deadline_seconds", "900")

    assert cfg.run_deadline_seconds == 900.0
    assert cfg.run_deadline_unlimited is False


@pytest.mark.parametrize("bad", ["0", "-1", "nan"])
def test_zero_and_negative_budgets_remain_invalid(
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    # A zero-second budget is exhausted before it starts, so it stays an error
    # rather than becoming a spelling of "unlimited".
    monkeypatch.setenv("ALYSIS_RUN_DEADLINE_SECONDS", bad)

    with pytest.raises(ConfigError):
        resolve_run_deadline(
            AppConfig(model="test-model"),
            default_seconds=DEFAULT_RUN_DEADLINE_SECONDS,
        )


def test_explicit_budget_still_wins_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)

    resolved = resolve_run_deadline(
        AppConfig(model="test-model"),
        cli_deadline_seconds=120.0,
        default_seconds=DEFAULT_RUN_DEADLINE_SECONDS,
    )

    assert resolved.seconds == 120.0
    assert resolved.source == "explicit_cli"


def test_negative_cli_budget_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)

    with pytest.raises(ConfigError):
        resolve_run_deadline(AppConfig(model="test-model"), cli_deadline_seconds=-5.0)


def test_run_cli_forwards_the_budget_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect
    import os

    from typer.testing import CliRunner

    from alysis_code import cli as cli_mod
    from alysis_code.cli import app as alysis_app
    from alysis_code.cli_impl.chat.loop import run as run_command

    captured: dict[str, Any] = {}
    run_signature = inspect.signature(run_command)

    def fake_run_impl(_cli_mod: Any, *args: Any, **kwargs: Any) -> int:
        forwarded = run_signature.bind(*args, **kwargs).arguments
        for name in ("deadline_seconds", "no_deadline", "require_deadline"):
            captured[name] = forwarded[name]
        return 0

    monkeypatch.setattr(cli_mod, "run_impl", fake_run_impl, raising=False)
    from alysis_code.cli_impl import chat as chat_facade

    monkeypatch.setattr(chat_facade, "run_impl", fake_run_impl)

    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "hello",
            "--path",
            os.fspath(tmp_path),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-deadline",
        ],
        env={
            "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
            "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        },
    )

    assert result.exit_code == 0
    assert captured["no_deadline"] is True
    assert captured["deadline_seconds"] is None
    assert captured["require_deadline"] is False


def test_interactive_runs_are_not_given_a_default_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)

    class _FakeSession:
        def run_turn(self, *_args: Any, **_kwargs: Any) -> int:
            return 0

        def close(self) -> None:
            return None

    def fake_create_session(**kwargs: Any) -> _FakeSession:
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(agent_loop, "create_session", fake_create_session)

    code = agent_loop.run_agent(
        cfg=AppConfig(model="test-model", stream=False),
        root=tmp_path,
        instruction="do the task",
        mode="auto",
        runtime_kind="chat",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="override-key",
        one_shot_execution=False,
        enable_compaction=False,
        verification_enabled=False,
    )

    # A person sitting at an interactive session is their own timeout.
    assert code == 0
    assert captured["execution_deadline"] is None


# ---------------------------------------------------------------------------
# (b) work persisted and clean exit at expiry, and (d) max-steps unchanged
# ---------------------------------------------------------------------------


class _EditThenStallClient:
    """Writes a file on the first step, then never answers again."""

    model = "test-model"
    temperature = 0.2

    def __init__(self, clock: _FakeClock, *, budget_seconds: float) -> None:
        self.clock = clock
        self.budget_seconds = budget_seconds
        self.calls = 0

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta: Any = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = messages, tools, stream, on_text_delta, temperature
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="fs_write",
                        arguments={"path": "result.txt", "content": "salvaged work\n"},
                    )
                ],
                raw={},
            )
        # Burn the whole budget before answering again.
        self.clock.advance(self.budget_seconds)
        return LLMResponse(content="Still working.", tool_calls=[], raw={})


def _session_with_budget(
    tmp_path: Path,
    clock: _FakeClock,
    *,
    budget_seconds: float,
    max_steps: int = 12,
) -> Any:
    return create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            stream=False,
            extra_fields={"shell_sandbox": {"mode": "off"}},
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=max_steps,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=ExecutionDeadline.from_duration(
            budget_seconds,
            clock=clock,
            source="explicit_cli",
            degradation_policy=DeadlineDegradationPolicy(),
        ),
        one_shot_execution=True,
        enable_compaction=False,
        verification_enabled=False,
    )


def test_expiry_keeps_persisted_work_and_exits_zero(tmp_path: Path) -> None:
    clock = _FakeClock()
    session = _session_with_budget(tmp_path, clock, budget_seconds=600.0)
    session.client = _EditThenStallClient(clock, budget_seconds=600.0)

    try:
        exit_code = session.run_turn("Write result.txt.")
        log_path = session.store.path
    finally:
        session.close()

    # The edit landed before the budget ran out, so the run exits clean.
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "salvaged work\n"
    assert exit_code == 0

    salvage = _event_payloads(log_path, "run_budget_salvage")
    assert salvage, "expiry must record what it salvaged"
    assert salvage[-1]["material_work_persisted"] is True
    assert salvage[-1]["exit_code"] == 0
    assert "result.txt" in salvage[-1]["salvaged_paths"]
    # The shared salvage record is what marks the session degraded.
    assert _event_payloads(log_path, "session_degraded")
    assert _event_payloads(log_path, "deadline_exhausted")


class _NeverEditsClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, clock: _FakeClock, *, budget_seconds: float) -> None:
        self.clock = clock
        self.budget_seconds = budget_seconds
        self.calls = 0

    def chat(self, **kwargs: Any) -> LLMResponse:
        _ = kwargs
        self.calls += 1
        self.clock.advance(self.budget_seconds)
        return LLMResponse(content="Thinking about it.", tool_calls=[], raw={})


def test_expiry_without_persisted_work_still_exits_zero(tmp_path: Path) -> None:
    clock = _FakeClock()
    session = _session_with_budget(tmp_path, clock, budget_seconds=600.0)
    session.client = _NeverEditsClient(clock, budget_seconds=600.0)

    try:
        exit_code = session.run_turn("Write result.txt.")
        log_path = session.store.path
    finally:
        session.close()

    # Running out of budget is an outcome, not a crash, even when the run has
    # nothing to show for itself: it stopped itself on purpose and said so.
    # Exiting non-zero here is what made harnesses record the stop as
    # NonZeroAgentExitCodeError.
    assert exit_code == 0
    salvage = _event_payloads(log_path, "run_budget_salvage")
    # The salvage record's own exit_code still reports whether material work
    # persisted -- it is shared with the empty-response-stall path, where it is
    # still the process exit code. It no longer decides the budget stop's.
    assert salvage[-1]["material_work_persisted"] is False
    assert salvage[-1]["exit_code"] == 1
    # The stop is machine-identifiable without parsing prose.
    assert _event_payloads(log_path, "deadline_exhausted")[-1]["stop_reason"] == (
        "run_budget_exhausted"
    )
    assert _event_payloads(log_path, "final")[-1]["stop_reason"] == "run_budget_exhausted"


@pytest.mark.parametrize("chat_only", [False, True], ids=["normal", "chat-only"])
def test_expiry_before_the_turn_starts_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_only: bool
) -> None:
    clock = _FakeClock()
    deadline = ExecutionDeadline.from_duration(10.0, clock=clock, source="explicit_cli")
    clock.advance(20.0)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="auto", stream=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        one_shot_execution=True,
        enable_compaction=False,
        verification_enabled=False,
    )

    def unexpected_model_call(**_kwargs: Any) -> LLMResponse:
        pytest.fail("An expired turn must not dispatch a model request")

    monkeypatch.setattr(session.client, "chat", unexpected_model_call)
    try:
        exit_code = session.run_turn("Do something.", chat_only=chat_only)
        log_path = session.store.path
    finally:
        session.close()

    # Both entry paths stop before model/tool work. The normal path has already
    # built salvage state; explicit chat stops before that state exists.
    assert exit_code == 0
    assert _event_payloads(log_path, "deadline_exhausted")
    assert not _event_payloads(log_path, "run_budget_salvage")
    outcome = _event_payloads(log_path, "turn_outcome")[-1]
    assert outcome["outcome"] == "deadline_exceeded"
    assert outcome["verified_success"] is False


def test_phase_transitions_are_recorded_in_the_session_log(tmp_path: Path) -> None:
    clock = _FakeClock()
    budget = 600.0
    session = _session_with_budget(tmp_path, clock, budget_seconds=budget)

    class _WrapUpClient:
        model = "test-model"
        temperature = 0.2

        def __init__(self) -> None:
            self.calls = 0
            self.system_messages: list[list[str]] = []

        def chat(self, **kwargs: Any) -> LLMResponse:
            self.system_messages.append(
                [
                    str(message.get("content") or "")
                    for message in kwargs.get("messages") or []
                    if message.get("role") == "system"
                ]
            )
            self.calls += 1
            if self.calls == 1:
                clock.advance(budget * 0.80)
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"call-{self.calls}",
                            name="fs_write",
                            arguments={"path": "a.txt", "content": "one\n"},
                        )
                    ],
                    raw={},
                )
            if self.calls == 2:
                clock.advance(budget * 0.15)
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"call-{self.calls}",
                            name="fs_write",
                            arguments={"path": "b.txt", "content": "two\n"},
                        )
                    ],
                    raw={},
                )
            return LLMResponse(content="Done.", tool_calls=[], raw={})

    client = _WrapUpClient()
    session.client = client

    try:
        session.run_turn("Write two files.")
        log_path = session.store.path
    finally:
        session.close()

    # The model is told what closed rather than only discovering it by having a
    # tool refused.
    sent = ["\n".join(messages) for messages in client.system_messages]
    assert any("Run budget checkpoint" in text for text in sent)
    assert any("Run budget wrap-up" in text for text in sent)

    transitions = _event_payloads(log_path, "deadline_phase_transition")
    stages = [payload["deadline_phase"] for payload in transitions]

    assert "convergence" in stages
    assert "wrap_up" in stages
    convergence = transitions[stages.index("convergence")]
    assert convergence["elapsed_fraction"] >= 0.75
    assert "subagent" in convergence["blocked_operations"]
    wrap_up = transitions[stages.index("wrap_up")]
    assert wrap_up["elapsed_fraction"] >= 0.90
    assert "mutation_tool" in wrap_up["blocked_operations"]

    # The second edit was requested after wrap-up, so it must have been refused
    # rather than written.
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    blocked = _event_payloads(log_path, "deadline_operation_blocked")
    assert any(payload.get("operation") == "mutation_tool" for payload in blocked)


def test_max_steps_still_ends_a_run_inside_its_budget(tmp_path: Path) -> None:
    clock = _FakeClock()

    class _LoopingClient:
        model = "test-model"
        temperature = 0.2

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, **kwargs: Any) -> LLMResponse:
            _ = kwargs
            self.calls += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call-{self.calls}",
                        name="fs_write",
                        arguments={
                            "path": f"step_{self.calls}.txt",
                            "content": "x\n",
                        },
                    )
                ],
                raw={},
            )

    # A budget that never advances: the step limit is the only thing that can
    # stop this run, exactly as before the budget existed.
    session = _session_with_budget(tmp_path, clock, budget_seconds=10_000.0, max_steps=3)
    client = _LoopingClient()
    session.client = client

    try:
        session.run_turn("Loop forever.")
        log_path = session.store.path
    finally:
        session.close()

    assert client.calls <= 4
    assert clock.now == 0.0
    assert not _event_payloads(log_path, "deadline_exhausted")
    assert not _event_payloads(log_path, "deadline_phase_transition")
    assert not _event_payloads(log_path, "run_budget_salvage")


def test_shell_run_is_not_classified_as_a_file_mutation() -> None:
    from alysis_code.agent.turn.core import _deadline_operation_for_tool_name

    # Wrap-up closes file mutation; a foreground shell command is how most runs
    # execute their tests, so it must not be swept up by that.
    assert _deadline_operation_for_tool_name("shell_run") is DeadlineOperation.SHELL_TOOL
    assert _deadline_operation_for_tool_name("fs_write") is DeadlineOperation.MUTATION_TOOL
    assert _deadline_operation_for_tool_name("verify_run") is DeadlineOperation.VERIFICATION
    assert _deadline_operation_for_tool_name("shell_background") is (
        DeadlineOperation.SHELL_BACKGROUND
    )


def test_budget_state_is_visible_in_deadline_telemetry() -> None:
    clock = _FakeClock()
    deadline = _budget(1000.0, clock)
    clock.advance(910.0)
    deadline.maybe_enter_degradation_stage()

    snapshot = deadline.telemetry_snapshot()

    assert snapshot["elapsed_fraction"] == pytest.approx(0.91)
    assert snapshot["degradation_stage"] == "wrap_up"
    assert snapshot["degradation_stages_entered"] == ["wrap_up"]
    assert "mutation_tool" in snapshot["degradation_blocked_operations"]
    assert snapshot["degradation_policy"]["convergence_fraction"] == 0.75
    assert snapshot["degradation_policy"]["wrap_up_fraction"] == 0.90


def test_subagent_budget_inherits_the_parent_degradation_policy() -> None:
    from alysis_code.execution_deadline import derive_subagent_deadline

    clock = _FakeClock()
    parent = _budget(1000.0, clock, convergence_fraction=0.5, wrap_up_fraction=0.6)

    child = derive_subagent_deadline(parent, 30.0)

    assert child.degradation_policy is parent.degradation_policy
