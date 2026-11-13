from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent.session as session_mod
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.process_reaping import (
    ProcessGroupRegistry,
    ProcessReapOutcome,
    process_group_exists,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import read_session_events

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")


def _make_session(tmp_path: Path, *, runtime_kind: RuntimeKind, session_id: str):
    sessions_dir = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=runtime_kind is RuntimeKind.ONE_SHOT,
        runtime_kind=runtime_kind,
        verification_enabled=False,
        session_log_dir_override=sessions_dir,
        session_id_override=session_id,
    )
    return session, sessions_dir


def _events(sessions_dir: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    return [
        event.get("payload") or {}
        for event in read_session_events(sessions_dir / f"{session_id}.jsonl")
        if str(event.get("type") or "") == event_type
    ]


class _FakeReaper:
    """Stands in for the signalling layer so the wiring is testable on any host."""

    def __init__(self, *, outcomes: tuple[ProcessReapOutcome, ...] = ()) -> None:
        self.outcomes = outcomes
        self.reap_calls = 0
        self.survivor_calls = 0

    def reap(self, registry, **kwargs):
        self.reap_calls += 1
        return self.outcomes

    def survivors(self, registry):
        self.survivor_calls += 1
        return tuple(outcome.payload() for outcome in self.outcomes)


def _install_fake_reaper(monkeypatch, fake: _FakeReaper) -> None:
    monkeypatch.setattr(session_mod, "reap_tracked_groups", fake.reap)
    monkeypatch.setattr(session_mod, "survivor_payloads", fake.survivors)


def _outcome(**overrides: Any) -> ProcessReapOutcome:
    base: dict[str, Any] = {
        "pgid": 4242,
        "command": "python -m pytest sklearn/tests/test_forest.py",
        "origin": "shell_run:host",
        "runtime_s": 91.4,
        "signal_used": "SIGTERM",
    }
    base.update(overrides)
    return ProcessReapOutcome(**base)


# ---------------------------------------------------------------------------
# Test 1: autonomous turn finalization reaps; interactive reports
# ---------------------------------------------------------------------------


def test_one_shot_turn_finalization_reaps_and_records(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-one-shot"
    )
    try:
        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    finally:
        session.store.close()

    assert fake.reap_calls == 1
    assert fake.survivor_calls == 0
    payloads = _events(sessions_dir, "reap-one-shot", "process_reaped")
    assert len(payloads) == 1
    assert payloads[0]["pgid"] == 4242
    assert payloads[0]["signal_used"] == "SIGTERM"
    assert payloads[0]["runtime_s"] == 91.4
    assert payloads[0]["event"] == "turn_finalization"
    assert payloads[0]["runtime_kind"] == "one_shot"
    assert _events(sessions_dir, "reap-one-shot", "process_survivors") == []


def test_interactive_turn_finalization_reports_without_reaping(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(command="npm run dev"),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.INTERACTIVE_CHAT, session_id="reap-interactive"
    )
    try:
        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    finally:
        session.store.close()

    assert fake.reap_calls == 0, "an interactive turn must never auto-kill"
    assert fake.survivor_calls == 1
    payloads = _events(sessions_dir, "reap-interactive", "process_survivors")
    assert len(payloads) == 1
    assert payloads[0]["count"] == 1
    assert payloads[0]["groups"][0]["command"] == "npm run dev"
    assert _events(sessions_dir, "reap-interactive", "process_reaped") == []


def test_interactive_session_close_reaps(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(command="npm run dev", signal_used="SIGKILL"),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.INTERACTIVE_CHAT, session_id="reap-close"
    )
    session.close()

    assert fake.reap_calls == 1
    payloads = _events(sessions_dir, "reap-close", "process_reaped")
    assert len(payloads) == 1
    assert payloads[0]["event"] == "session_close"
    assert payloads[0]["signal_used"] == "SIGKILL"


def test_kill_switch_restores_legacy_behavior(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_PROCESS_REAPING", "off")
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-off"
    )
    session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    session.close()

    assert fake.reap_calls == 0
    assert fake.survivor_calls == 0
    assert _events(sessions_dir, "reap-off", "process_reaped") == []
    assert _events(sessions_dir, "reap-off", "process_survivors") == []


def test_reaping_failure_never_breaks_the_turn(tmp_path: Path, monkeypatch) -> None:
    def _explode(registry, **kwargs):
        raise RuntimeError("signal layer is broken")

    monkeypatch.setattr(session_mod, "reap_tracked_groups", _explode)
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-error"
    )
    try:
        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
    finally:
        session.store.close()

    warnings = [
        payload
        for payload in _events(sessions_dir, "reap-error", "warning")
        if payload.get("warning") == "process_reaping_failed"
    ]
    assert len(warnings) == 1
    assert "signal layer is broken" in warnings[0]["error"]


# ---------------------------------------------------------------------------
# Test 2: the turn boundary fires on every exit path
# ---------------------------------------------------------------------------


def test_turn_finalization_fires_even_when_the_turn_raises(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-raise"
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(session_mod, "_run_turn", _boom)
    try:
        with pytest.raises(RuntimeError, match="provider exploded"):
            session.run_turn("do the thing")
    finally:
        session.store.close()

    assert fake.reap_calls == 1


def test_turn_finalization_fires_on_cancellation(tmp_path: Path, monkeypatch) -> None:
    # KeyboardInterrupt is a BaseException, so an `except Exception` boundary
    # would miss it; the reap hook must be in a `finally`.
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-cancel"
    )

    def _cancel(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(session_mod, "_run_turn", _cancel)
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn("do the thing")
    finally:
        session.store.close()

    assert fake.reap_calls == 1


def test_interrupted_turn_cancels_children_records_once_and_session_is_reusable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = "interactive-turn-cancel"
    session, sessions_dir = _make_session(
        tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        session_id=session_id,
    )
    original_scheduler = session.child_scheduler
    calls = 0

    class _PendingChildScheduler:
        def __init__(self) -> None:
            self.pending = ["active-child"]
            self.cancel_calls: list[dict[str, Any]] = []

        def pending_run_ids(self) -> list[str]:
            return list(self.pending)

        def cancel(self, **kwargs: Any) -> dict[str, Any]:
            self.cancel_calls.append(dict(kwargs))
            self.pending.clear()
            return {"cancelled_run_ids": ["active-child"], "children": []}

    scheduler = _PendingChildScheduler()
    session.child_scheduler = scheduler  # type: ignore[assignment]

    def _interrupt_then_cancel_then_succeed(*_args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        if calls == 2:
            scheduler.pending = ["tui-active-child"]
            kwargs["cancellation_token"].cancel()
        return 0

    monkeypatch.setattr(session_mod, "_run_turn", _interrupt_then_cancel_then_succeed)
    classic_token = InteractiveCancellationToken()
    tui_token = InteractiveCancellationToken()
    next_turn_token = InteractiveCancellationToken()
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn("classic interrupt", cancellation_token=classic_token)
        assert classic_token.is_cancelled is True

        assert session.run_turn("TUI interrupt", cancellation_token=tui_token) == 0
        assert tui_token.is_cancelled is True

        assert session.run_turn("next turn", cancellation_token=next_turn_token) == 0
        assert next_turn_token.is_cancelled is False
    finally:
        session.child_scheduler = original_scheduler
        session.close()

    interrupted = _events(sessions_dir, session_id, "turn_interrupted")
    requested = _events(sessions_dir, session_id, "cancellation_requested")
    assert len(requested) == 2
    assert all(event["reason"] == "cancelled_by_user" for event in requested)
    assert all(event["trigger"] == "interactive_token" for event in requested)
    assert len(interrupted) == 2
    assert [event["reason"] for event in interrupted] == [
        "cancelled_by_user",
        "cancelled_by_user",
    ]
    assert [event["trigger"] for event in interrupted] == [
        "keyboard_interrupt",
        "cancellation_token",
    ]
    assert [event["cancellation_already_requested"] for event in interrupted] == [
        False,
        True,
    ]
    assert all(event["cancellation_requested"] is True for event in interrupted)
    lifecycle_events = [
        event["type"]
        for event in read_session_events(sessions_dir / f"{session_id}.jsonl")
        if event["type"] in {"cancellation_requested", "turn_interrupted"}
    ]
    assert lifecycle_events == [
        "cancellation_requested",
        "turn_interrupted",
        "cancellation_requested",
        "turn_interrupted",
    ]
    # Interactive tokens publish directly to the children accepted by their
    # own turn. Session-wide cancellation would be able to catch children that
    # belong to a later turn, so it is intentionally reserved for legacy tokens.
    assert scheduler.cancel_calls == []


def test_cancellation_request_persists_before_polling_turn_records_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "interactive-cancellation-order-race"
    session, sessions_dir = _make_session(
        tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        session_id=session_id,
    )
    token = InteractiveCancellationToken()
    turn_started = threading.Event()
    turn_observed_cancellation = threading.Event()
    request_append_started = threading.Event()
    allow_request_append = threading.Event()
    interrupted_append_started = threading.Event()
    failures: list[BaseException] = []
    original_append = session.store.append

    def _delayed_append(event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "cancellation_requested":
            request_append_started.set()
            assert allow_request_append.wait(timeout=3)
        elif event_type == "turn_interrupted":
            interrupted_append_started.set()
        original_append(event_type, payload)

    def _poll_until_cancelled(*_args: Any, **kwargs: Any) -> int:
        turn_started.set()
        cancellation_token = kwargs["cancellation_token"]
        assert cancellation_token.wait(timeout=3)
        turn_observed_cancellation.set()
        return 0

    def _run() -> None:
        try:
            session.run_turn("poll for cancellation", cancellation_token=token)
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            failures.append(exc)

    monkeypatch.setattr(session.store, "append", _delayed_append)
    monkeypatch.setattr(session_mod, "_run_turn", _poll_until_cancelled)
    worker = threading.Thread(target=_run)
    canceller = threading.Thread(target=token.cancel)
    try:
        worker.start()
        assert turn_started.wait(timeout=3)
        canceller.start()
        assert request_append_started.wait(timeout=3)
        assert turn_observed_cancellation.wait(timeout=3)

        # The token event is already visible to the worker, but its terminal
        # event must wait for the request event's append to finish.
        assert not interrupted_append_started.wait(timeout=0.1)

        allow_request_append.set()
        canceller.join(timeout=3)
        worker.join(timeout=3)
        assert not canceller.is_alive()
        assert not worker.is_alive()
        assert failures == []
    finally:
        allow_request_append.set()
        canceller.join(timeout=3)
        worker.join(timeout=3)
        session.close()

    lifecycle_events = [
        event["type"]
        for event in read_session_events(sessions_dir / f"{session_id}.jsonl")
        if event["type"] in {"cancellation_requested", "turn_interrupted"}
    ]
    assert lifecycle_events == ["cancellation_requested", "turn_interrupted"]


class _ToolCallThenDoneClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, tool_call: ToolCall) -> None:
        self._tool_call = tool_call
        self.calls = 0

    def chat(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content="", tool_calls=[self._tool_call], raw={})
        return LLMResponse(content="Done.", tool_calls=[], raw={})


def test_tool_result_after_cancellation_is_logged_after_the_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The token is visible before its request callback appends. A tool that
    # returns because of the cancellation (a subagent wait, for one) must still
    # be logged after the request, whichever thread gets scheduled first.
    session_id = "interactive-cancelled-tool-result-order"
    session, sessions_dir = _make_session(
        tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        session_id=session_id,
    )
    token = InteractiveCancellationToken()
    tool_started = threading.Event()
    request_append_started = threading.Event()
    allow_request_append = threading.Event()
    tool_result_append_started = threading.Event()
    outcomes: list[BaseException | int] = []
    original_append = session.store.append

    def _delayed_append(event_type: str, payload: dict[str, Any], **kwargs: Any) -> None:
        if event_type == "cancellation_requested":
            request_append_started.set()
            assert allow_request_append.wait(timeout=3)
        elif event_type == "tool_result" and payload.get("tool_call_id") == "wait-for-cancel":
            tool_result_append_started.set()
        original_append(event_type, payload, **kwargs)

    def _wait_for_cancellation(_args: dict[str, Any]) -> dict[str, Any]:
        tool_started.set()
        assert token.wait(timeout=3)
        return {"entries": []}

    def _run() -> None:
        try:
            outcomes.append(session.run_turn("List the files.", cancellation_token=token))
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            outcomes.append(exc)

    session.client = _ToolCallThenDoneClient(  # type: ignore[assignment]
        ToolCall(id="wait-for-cancel", name="fs_list", arguments={"path": "."})
    )
    session.tools["fs_list"] = replace(session.tools["fs_list"], run=_wait_for_cancellation)
    monkeypatch.setattr(session.store, "append", _delayed_append)
    worker = threading.Thread(target=_run)
    canceller = threading.Thread(target=token.cancel)
    try:
        worker.start()
        assert tool_started.wait(timeout=5)
        canceller.start()
        assert request_append_started.wait(timeout=3)
        # The tool has already returned, but its result waits for the request.
        assert not tool_result_append_started.wait(timeout=0.3)
        allow_request_append.set()
        canceller.join(timeout=3)
        worker.join(timeout=5)
        assert not canceller.is_alive()
        assert not worker.is_alive()
        assert all(isinstance(outcome, int | KeyboardInterrupt) for outcome in outcomes)
    finally:
        allow_request_append.set()
        canceller.join(timeout=3)
        worker.join(timeout=5)
        session.close()

    ordered = [
        event["type"]
        for event in read_session_events(sessions_dir / f"{session_id}.jsonl")
        if event["type"] == "cancellation_requested"
        or (
            event["type"] == "tool_result"
            and (event.get("payload") or {}).get("tool_call_id") == "wait-for-cancel"
        )
    ]
    assert ordered == ["cancellation_requested", "tool_result"]


def test_late_cancelled_turn_cleanup_does_not_cancel_a_new_turn_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session, _sessions_dir = _make_session(
        tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        session_id="overlapping-interactive-turns",
    )
    original_scheduler = session.child_scheduler
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    failures: list[BaseException] = []

    class _PendingChildScheduler:
        def __init__(self) -> None:
            self.pending: list[str] = []
            self.cancel_calls: list[dict[str, Any]] = []

        def pending_run_ids(self) -> list[str]:
            return list(self.pending)

        def cancel(self, **kwargs: Any) -> dict[str, Any]:
            self.cancel_calls.append(dict(kwargs))
            self.pending.clear()
            return {"cancelled_run_ids": [], "children": []}

    scheduler = _PendingChildScheduler()
    session.child_scheduler = scheduler  # type: ignore[assignment]

    def _overlapping_turns(
        _session: Any,
        instruction: str,
        **kwargs: Any,
    ) -> int:
        if instruction == "cancelled turn":
            first_started.set()
            assert kwargs["cancellation_token"].is_cancelled is False
            assert release_first.wait(timeout=5)
            return 0
        scheduler.pending = ["new-turn-child"]
        second_started.set()
        assert release_second.wait(timeout=5)
        return 0

    def _call_turn(instruction: str, token: InteractiveCancellationToken) -> None:
        try:
            session.run_turn(instruction, cancellation_token=token)
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            failures.append(exc)

    monkeypatch.setattr(session_mod, "_run_turn", _overlapping_turns)
    cancelled_token = InteractiveCancellationToken()
    next_token = InteractiveCancellationToken()
    first_thread = threading.Thread(
        target=_call_turn,
        args=("cancelled turn", cancelled_token),
    )
    second_thread = threading.Thread(
        target=_call_turn,
        args=("next turn", next_token),
    )
    try:
        first_thread.start()
        assert first_started.wait(timeout=5)
        cancelled_token.cancel()

        second_thread.start()
        assert second_started.wait(timeout=5)
        release_first.set()
        first_thread.join(timeout=5)

        assert first_thread.is_alive() is False
        assert scheduler.cancel_calls == []
        assert scheduler.pending == ["new-turn-child"]
        assert next_token.is_cancelled is False

        release_second.set()
        second_thread.join(timeout=5)
        assert second_thread.is_alive() is False
        assert failures == []
    finally:
        release_first.set()
        release_second.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)
        session.child_scheduler = original_scheduler
        session.close()


def test_turn_finalization_fires_on_success(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeReaper(outcomes=(_outcome(),))
    _install_fake_reaper(monkeypatch, fake)
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-success"
    )
    monkeypatch.setattr(session_mod, "_run_turn", lambda *_a, **_k: 0)
    try:
        assert session.run_turn("do the thing") == 0
    finally:
        session.store.close()

    assert fake.reap_calls == 1


# ---------------------------------------------------------------------------
# Test 3: end to end with a real process group (POSIX)
# ---------------------------------------------------------------------------


@posix_only
def test_real_orphan_is_reaped_at_one_shot_turn_finalization(tmp_path: Path) -> None:
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-real"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        registry = session.process_group_registry
        assert isinstance(registry, ProcessGroupRegistry)
        pgid = os.getpgid(child.pid)
        assert registry.register(pgid=pgid, command="pytest -q", origin="shell_run:host")

        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)

        child.wait(timeout=15)
        assert not process_group_exists(pgid)
        payloads = _events(sessions_dir, "reap-real", "process_reaped")
        assert len(payloads) == 1
        assert payloads[0]["pgid"] == pgid
        assert payloads[0]["command"] == "pytest -q"
        assert payloads[0]["signal_used"] in {"SIGTERM", "SIGKILL"}
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        session.store.close()


@posix_only
def test_real_interactive_survivor_is_reported_then_reaped_at_close(tmp_path: Path) -> None:
    session, sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.INTERACTIVE_CHAT, session_id="reap-real-interactive"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(child.pid)
        assert session.process_group_registry is not None
        session.process_group_registry.register(
            pgid=pgid, command="npm run dev", origin="shell_run:host"
        )

        session._reap_tracked_process_groups(event=session_mod.ReapEvent.TURN_FINALIZATION)
        time.sleep(0.2)
        assert child.poll() is None, "an interactive turn killed the user's dev server"

        session.close()
        child.wait(timeout=15)
        assert not process_group_exists(pgid)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    survivors = _events(sessions_dir, "reap-real-interactive", "process_survivors")
    assert len(survivors) == 1
    assert survivors[0]["groups"][0]["command"] == "npm run dev"
    reaped = _events(sessions_dir, "reap-real-interactive", "process_reaped")
    assert len(reaped) == 1
    assert reaped[0]["event"] == "session_close"


@posix_only
def test_session_shell_runner_places_commands_in_their_own_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This test exercises host process-group isolation, not sandbox backend
    # availability. Opt out only here so it also runs on macOS without Docker.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    session, _sessions_dir = _make_session(
        tmp_path, runtime_kind=RuntimeKind.ONE_SHOT, session_id="reap-runner"
    )
    try:
        runner = session.shell_runner
        assert runner is not None
        completed = runner.run(root=tmp_path, cwd=tmp_path, cmd="ps -o pgid= -p $$", timeout_s=30)
        assert completed.returncode == 0
        child_pgid = int(completed.stdout.strip())
        assert child_pgid != os.getpgrp(), "the command ran in the agent's own process group"
    finally:
        session.close()
