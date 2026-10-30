"""Failure reporting preserves observed outcomes without granting coverage."""

from __future__ import annotations

import copy
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)
from test_inferred_verification_authority import _run, _ScriptedClient

import alysis_code.agent.turn.core as turn_core
from alysis_code.agent import session as session_mod
from alysis_code.agent.verification import (
    TurnExecutionState,
    _completion_gate_blocker_allows_final,
    _completion_gate_nudge_message,
    _record_tool_effect,
)
from alysis_code.config import AppConfig
from alysis_code.failure_category import FailureCategory
from alysis_code.llm.types import LLMResponse
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.verification_contract import build_verification_command_spec

_FAILED = "python3 -m unittest tests.test_required -v"
_PASSED = "python3 -m unittest tests.test_passing -v"
_PASS_OUTPUT = "Ran 1 test in 0.001s\n\nOK\n"
_FAIL_OUTPUT = (
    "AssertionError: required result differs\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n"
)


pytestmark = pytest.mark.usefixtures("approved_local_checks")


def _row(command: str, code: int, **extra: Any) -> dict[str, Any]:
    return {
        "command": command,
        "effective_command": command,
        "exit_code": code,
        "ok": code == 0,
        "status": "passed" if code == 0 else "failed",
        "real_execution": True,
        "output_preview": _PASS_OUTPUT if code == 0 else _FAIL_OUTPUT,
        **extra,
    }


def _record(
    root: Path,
    state: TurnExecutionState,
    rows: list[dict[str, Any]],
    *,
    specs: list[dict[str, Any]] | None = None,
    aggregate_category: str = "",
) -> dict[str, Any]:
    result = {
        "commands": [row["command"] for row in rows],
        "command_results": rows,
        "all_passed": all(row["exit_code"] == 0 for row in rows),
        "verification_command_specs": specs or [],
        "failure_category": aggregate_category,
    }
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="verify_run",
        arguments={"commands": result["commands"]},
        status="ok",
        result=result,
        known_verification_commands=[spec["original_text"] for spec in specs or []],
        scope_environment_known=True,
    )
    return result


def _spec(command: str = _FAILED, source: str = "repo_scan.likely_test_commands") -> dict:
    return build_verification_command_spec(command, source=source).as_payload()


def _line_for(diagnostic: str, command: str) -> str:
    return next(line for line in diagnostic.splitlines() if f"`{command}`" in line)


def test_mixed_outcomes_keep_exact_failure_and_current_pass_separate(tmp_path: Path) -> None:
    state = TurnExecutionState(execution_requested=True)
    result = _record(
        tmp_path,
        state,
        [_row(_FAILED, 1, failure_category="test_failure"), _row(_PASSED, 0)],
        specs=[_spec()],
    )
    failure_before = copy.deepcopy(state.observed_verification_failures)
    evidence_before = copy.deepcopy(state.executed_verification_evidence)
    diagnostic = state.verification_failure_diagnostic()

    failed_line = _line_for(diagnostic, _FAILED)
    assert "INFERRED_HEURISTIC" in failed_line and "ADVISORY" in failed_line
    assert "test_failure" in failed_line
    assert diagnostic.count(f"`{_FAILED}`") == 1
    assert "Unresolved required check" not in diagnostic
    passed_line = _line_for(diagnostic, _PASSED)
    assert "successful" in passed_line and _FAILED not in passed_line
    assert result["command_results"][1]["exit_code"] == 0
    assert state.observed_verification_failures == failure_before
    assert state.executed_verification_evidence == evidence_before
    assert state.last_verification_passed is False
    assert not state.covered_verification_commands
    # A recommendation does not disqualify the separate successful execution;
    # accepting it as evidence does not cover or clear the failed command.
    assert len(state.accepted_verification_evidence) == 1
    assert state.accepted_verification_evidence[0]["normalized_command"] == _PASSED
    assert state.accepted_verification_evidence[0]["covered_verification_commands"] == []

    _record(tmp_path, state, [_row(_PASSED, 0)], specs=[_spec()])
    assert state.observed_verification_failures == failure_before
    assert state.last_verification_passed is False
    assert _FAILED in state.verification_failure_diagnostic()


@pytest.mark.parametrize("mismatch", ["selector", "cwd", "duplicate", "invalid-enum"])
def test_metadata_is_not_borrowed_from_an_unmatched_or_ambiguous_spec(
    tmp_path: Path, mismatch: str
) -> None:
    spec = _spec()
    if mismatch == "selector":
        spec["original_text"] = _FAILED + " -k another_case"
    elif mismatch == "cwd":
        spec["working_directory"] = "elsewhere"
    elif mismatch == "invalid-enum":
        spec["requirement"] = "RECOMMENDED_BUT_NOT_AN_ENUM"
    specs = [spec, copy.deepcopy(spec)] if mismatch == "duplicate" else [spec]
    state = TurnExecutionState(execution_requested=True)
    _record(tmp_path, state, [_row(_FAILED, 1)], specs=specs)

    line = _line_for(state.verification_failure_diagnostic(), _FAILED)
    assert "selection provenance" not in line
    assert "selected requirement" not in line


def test_category_fallback_is_bound_to_one_execution_not_broadcast_across_batch(
    tmp_path: Path,
) -> None:
    single = TurnExecutionState(execution_requested=True)
    _record(tmp_path, single, [_row(_FAILED, 127)], aggregate_category="launch_error")
    assert "launch_error" in _line_for(single.verification_failure_diagnostic(), _FAILED)

    mixed = TurnExecutionState(execution_requested=True)
    _record(
        tmp_path,
        mixed,
        [_row(_FAILED, 1), _row(_PASSED, 1, failure_category="test_failure")],
        aggregate_category="launch_error",
    )
    diagnostic = mixed.verification_failure_diagnostic()
    assert "launch_error" not in diagnostic
    assert "test_failure" in _line_for(diagnostic, _PASSED)


def test_relevant_edit_removes_stale_pass_from_diagnostic_not_from_history(tmp_path: Path) -> None:
    state = TurnExecutionState(execution_requested=True)
    _record(tmp_path, state, [_row(_FAILED, 1), _row(_PASSED, 0)])
    assert _PASSED in state.verification_failure_diagnostic()
    executed = copy.deepcopy(state.executed_verification_evidence)
    state.note_verification_relevant_edit()

    diagnostic = state.verification_failure_diagnostic()
    assert _FAILED in diagnostic and _PASSED not in diagnostic
    assert "before the latest relevant edit" in diagnostic
    assert state.executed_verification_evidence == executed
    assert state.last_verification_passed is False


def test_required_failure_and_missing_coverage_remain_despite_different_pass(
    tmp_path: Path,
) -> None:
    state = TurnExecutionState(execution_requested=True, expected_verification_commands={_FAILED})
    specs = [_spec(source="cli.verify_cmd")]
    _record(tmp_path, state, [_row(_FAILED, 1)], specs=specs)
    _record(tmp_path, state, [_row(_PASSED, 0)], specs=specs)

    diagnostic = state.verification_failure_diagnostic()
    assert "required" in _line_for(diagnostic, _FAILED)
    assert "successful" in _line_for(diagnostic, _PASSED)
    assert state.failed_verification_commands() == {_FAILED}
    assert state.missing_verification_commands() == {_FAILED}
    assert not state.covered_verification_commands
    assert not state.accepted_verification_evidence
    assert state.last_verification_passed is False


def test_nudge_fallback_does_not_invent_latest_failed_outcome() -> None:
    message = _completion_gate_nudge_message(
        ["verification_failed"], verification_failure_snippet="An earlier check failed."
    )
    assert "An earlier check failed" in message
    assert "last verification failed" not in message.lower()
    assert "latest verification attempt" not in message.lower()


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
def test_actual_session_delivers_failure_identity_and_separate_real_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, one_shot: bool
) -> None:
    for prefix in ("ALYSIS", "ALYSIS"):
        for suffix in ("VERIFY_SANDBOX_MODE", "SHELL_SANDBOX_MODE"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests/__init__.py").write_text("")
    (root / "tests/test_required.py").write_text(
        "import unittest\nclass Required(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(2 + 2, 5)\n"
    )
    (root / "tests/test_passing.py").write_text(
        "import unittest\nclass Passing(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(2 + 2, 4)\n"
    )
    executable = (
        subprocess.list2cmdline([Path(sys.executable).as_posix()])
        if os.name == "nt"
        else shlex.quote(sys.executable)
    )
    failed = f"{executable} -m unittest tests.test_required -v"
    passed = f"{executable} -m unittest tests.test_passing -v"
    final = LLMResponse(content="The checks are complete.", tool_calls=[], raw={})
    responses = [
        _run("verify_run", "required-fails", failed),
        _run("verify_run", "different-passes", passed),
        final,
        final,
        final,
        final,
    ]
    clients: list[_ScriptedClient] = []
    snapshots: list[dict[str, Any]] = []
    original_record = turn_core._record_tool_effect

    def observe(**kwargs: Any) -> None:
        original_record(**kwargs)
        if kwargs["tool_name"] == "verify_run":
            state = kwargs["state"]
            snapshots.append(
                {
                    "result": copy.deepcopy(kwargs["result"]),
                    "diagnostic": state.verification_failure_diagnostic(),
                    "missing": state.missing_verification_commands(),
                    "failed": state.failed_verification_commands(),
                    "accepted": copy.deepcopy(state.accepted_verification_evidence),
                }
            )

    def create_client(**kwargs: Any) -> _ScriptedClient:
        client = _ScriptedClient(responses, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(turn_core, "_record_tool_effect", observe)
    monkeypatch.setattr(session_mod, "_make_session_llm_client", create_client)
    cfg = AppConfig(
        model="offline-failure-diagnostic-model",
        routing_mode="code_only",
        skills_enabled=False,
        extra_fields={"verify_sandbox": {"mode": "off"}},
    )
    session = session_mod.create_session(
        cfg=cfg,
        root=root,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="unused-offline-key",
        verify_cmd=[failed],
        one_shot_execution=one_shot,
        enable_chat_turn_step_budget=True,
        session_log_dir_override=tmp_path / "sessions",
        surface=NoopSurface(),
        enable_compaction=False,
        subagents_enabled=False,
    )
    try:
        session.run_turn("Run the repository checks and report their outcomes; do not edit files.")
    finally:
        session.close()

    assert len(clients) == 1
    assert [s["result"]["command_results"][0]["exit_code"] for s in snapshots] == [1, 0]
    latest = snapshots[-1]
    assert latest["failed"] == latest["missing"] == {failed}
    assert not latest["accepted"]
    assert "required" in _line_for(latest["diagnostic"], failed)
    assert "successful" in _line_for(latest["diagnostic"], passed)
    delivered = [
        message["content"]
        for request in clients[0].requests[3:]
        for message in request["messages"]
        if isinstance(message.get("content"), str)
        and message["content"].startswith("Finalization check")
    ]
    assert delivered, "the real completion path must provide an actionable diagnostic"
    assert all(latest["diagnostic"] in message for message in delivered)


@pytest.mark.parametrize("separate_required_failure", [False, True])
def test_inherited_display_diagnosis_does_not_change_blocker_acceptance(
    tmp_path: Path, separate_required_failure: bool
) -> None:
    """Only the aggregate label varies; execution/failure policy must not."""
    states = []
    for aggregate in ("", FailureCategory.INFRA_UNAVAILABLE.value):
        state = TurnExecutionState(
            execution_requested=True,
            touched_repo_paths={"logic.py"},
            expected_verification_commands={_PASSED} if separate_required_failure else set(),
        )
        specs = [_spec()]
        if separate_required_failure:
            specs.append(_spec(_PASSED, source="cli.verify_cmd"))
            _record(tmp_path, state, [_row(_PASSED, 1)], specs=specs)
        _record(
            tmp_path,
            state,
            [_row(_FAILED, 127, real_execution=None)],
            specs=specs,
            aggregate_category=aggregate,
        )
        states.append(state)

    original, restored_display = states
    assert original.last_verification_failure_category == FailureCategory.VERIFICATION_FAILED.value
    assert (
        restored_display.last_verification_failure_category
        == original.last_verification_failure_category
    )
    assert restored_display.last_verification_passed is original.last_verification_passed is False
    assert not _completion_gate_blocker_allows_final(state=original, blocked_response=True)
    assert not _completion_gate_blocker_allows_final(state=restored_display, blocked_response=True)
    assert "infra_unavailable" in _line_for(
        restored_display.verification_failure_diagnostic(), _FAILED
    )
    assert (
        restored_display.failed_verification_commands() == original.failed_verification_commands()
    )
    assert (
        restored_display.missing_verification_commands() == original.missing_verification_commands()
    )
    if separate_required_failure:
        assert restored_display.failed_verification_commands() == {_PASSED}
        assert "required" in _line_for(restored_display.verification_failure_diagnostic(), _PASSED)


def test_explicit_per_command_category_keeps_existing_blocker_policy(tmp_path: Path) -> None:
    state = TurnExecutionState(execution_requested=True, touched_repo_paths={"logic.py"})
    _record(
        tmp_path,
        state,
        [_row(_FAILED, 127, failure_category=FailureCategory.INFRA_UNAVAILABLE.value)],
        specs=[_spec()],
        aggregate_category="test_failure",
    )
    assert state.last_verification_failure_category == FailureCategory.INFRA_UNAVAILABLE.value
    assert _completion_gate_blocker_allows_final(state=state, blocked_response=True)
    assert not _completion_gate_blocker_allows_final(state=state, blocked_response=False)
    assert "infra_unavailable" in _line_for(state.verification_failure_diagnostic(), _FAILED)
    assert "test_failure" not in state.verification_failure_diagnostic()
