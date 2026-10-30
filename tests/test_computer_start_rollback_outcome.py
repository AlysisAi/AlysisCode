"""R-2: trusted current-entry terminal state, not text or a refusal-shaped value."""

import copy
import json

import pytest
from test_computer_start_resolved_contract import entry
from test_computer_start_resolved_review import text_body
from test_computer_start_resolved_turn import CORPUS, body, session_for
from test_computer_use_skill import isolated_paths as isolated_paths

from alysis_code.agent.computer_start_resolved import FRESH, HOST_CODES, RETAIN, Refusal
from alysis_code.llm.types import LLMError
from alysis_code.run_outcome import task_outcome_record

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

TERMINAL_CODES = sorted(
    (
        HOST_CODES
        | FRESH
        | {
            "computer_start_response_invalid",
            "browser_start_timeout",
            "computer_selection_recovery_exhausted",
        }
    )
    - RETAIN
)


@pytest.mark.parametrize("coverage", ["incomplete", "unavailable", "empty", "exhausted"])
def test_actual_owned_terminal_is_blocked(tmp_path, monkeypatch, coverage):
    plan = copy.deepcopy(CORPUS["bases"]["plan"])
    if coverage == "exhausted":
        plan["app_steps"][0]["app"] = "Absent fixture"
    s, calls, requests = session_for(
        tmp_path, monkeypatch, "compat", False, [body("compat", False, json.dumps(plan))]
    )
    if coverage != "exhausted":
        s.resolved_entry.descriptor["discovery"].update(
            coverage="complete" if coverage == "empty" else coverage, targets=[]
        )
    counts = []
    original_record = s._record_task_outcome

    def record(**kw):
        if kw.get("terminal", True):
            evidence = s._computer_use_evidence_state
            counts.append((evidence["stateCalls"], evidence["actionCalls"]))
        return original_record(**kw)

    monkeypatch.setattr(s, "_record_task_outcome", record)
    try:
        assert s.run_turn("Observe the synthetic app without input.") == 0
        assert s.last_turn_outcome["outcome"] == "blocked"
        assert s.last_turn_outcome["verified_success"] is False
        assert not calls
        assert counts and all(pair == (0, 0) for pair in counts)
        assert len(requests) <= 3
    finally:
        s.close()


@pytest.mark.parametrize("code", TERMINAL_CODES)
@pytest.mark.parametrize("dispatch", ["not_dispatched", "unknown"])
def test_each_owned_terminal_code_keeps_reason_and_dispatch(tmp_path, monkeypatch, code, dispatch):
    s, calls, _ = session_for(
        tmp_path,
        monkeypatch,
        "compat",
        False,
        [body("compat", False, json.dumps(CORPUS["bases"]["plan"]))],
    )
    e = s.resolved_entry
    results = []

    def refuse(*_):
        value = e.refusal(code, dispatch)
        results.append(value)
        return value

    monkeypatch.setattr(e, "dispatch", refuse)
    try:
        s.run_turn("Observe the synthetic app.")
        assert s.last_turn_outcome["outcome"] == "blocked"
        assert s.last_turn_outcome["reason"] == code
        assert s.last_turn_outcome["verified_success"] is False
        assert results[0]["dispatch"] == dispatch
        assert not calls
    finally:
        s.close()


@pytest.mark.parametrize(
    "kind", ["plain", "bare", "foreign", "retired", "mutated", "owner_replaced"]
)
def test_lookalikes_and_stale_owners_do_not_authorize_blocked(tmp_path, monkeypatch, kind):
    outputs = [
        body("compat", False, json.dumps(CORPUS["bases"]["plan"])),
        text_body("compat", False),
    ]
    s, _, _ = session_for(tmp_path, monkeypatch, "compat", False, outputs)
    e = s.resolved_entry

    def fake(*_):
        value = e.refusal("computer_start_inventory_incomplete")
        if kind == "plain":
            return dict(value)
        if kind == "bare":
            return Refusal(value["code"])
        if kind == "foreign":
            other, _, _ = entry()
            return other.refusal(value["code"])
        if kind == "retired":
            e.retire("consumed")
        if kind == "mutated":
            value["dispatch"] = "unknown"
        if kind == "owner_replaced":
            s.resolved_entry, _, _ = entry()
        return value

    monkeypatch.setattr(e, "dispatch", fake)
    try:
        # Retired/changed ownership may stop the request, but is not a blocked verdict.
        try:
            s.run_turn("Observe the synthetic app.")
        except LLMError:
            pass
        assert (s.last_turn_outcome or {}).get("outcome") != "blocked"
    finally:
        s.close()


def test_ordinary_no_tools_and_reason_strings_are_not_authority(tmp_path, monkeypatch):
    s, calls, _ = session_for(tmp_path, monkeypatch, "compat", False, [text_body("compat", False)])
    try:
        s.run_turn("Explain the synthetic example.")
        assert s.last_turn_outcome["outcome"] == "completed_unverified"
        assert not calls
        for code in TERMINAL_CODES:
            assert (
                task_outcome_record(exit_code=0, reason=code)["outcome"] == "completed_unverified"
            )
    finally:
        s.close()


@pytest.mark.parametrize("control", ["cancel", "deadline", "provider"])
def test_terminal_refusal_does_not_override_stop_deadline_provider(tmp_path, monkeypatch, control):
    from alysis_code.cancellation import InteractiveCancellationToken
    from alysis_code.execution_deadline import ExecutionDeadline

    token = InteractiveCancellationToken()
    clock = [0.0]
    s, calls, _ = session_for(
        tmp_path,
        monkeypatch,
        "compat",
        False,
        [body("compat", False, json.dumps(CORPUS["bases"]["plan"]))],
    )
    e = s.resolved_entry
    if control == "deadline":
        s.execution_deadline = ExecutionDeadline.from_duration(60, clock=lambda: clock[0])

    def refuse(*_):
        value = e.refusal("computer_start_inventory_incomplete")
        if control == "cancel":
            token.cancel()
        elif control == "deadline":
            clock[0] = 61
        else:
            raise LLMError("synthetic provider failure")
        return value

    monkeypatch.setattr(e, "dispatch", refuse)
    try:
        if control == "cancel":
            with pytest.raises(KeyboardInterrupt):
                s.run_turn("Observe the synthetic app.", cancellation_token=token)
        elif control == "provider":
            with pytest.raises(LLMError):
                s.run_turn("Observe the synthetic app.")
        else:
            s.run_turn("Observe the synthetic app.")
        outcome = s.last_turn_outcome or {}
        assert outcome.get("outcome") != "blocked"
        assert not outcome.get("verified_success")
        if control == "deadline":
            assert outcome["outcome"] == "deadline_exceeded"
        assert not calls
    finally:
        s.close()


@pytest.mark.parametrize(
    "reason,outcome",
    [
        ("cancelled_by_user", "cancelled"),
        ("deadline_exhausted", "deadline_exceeded"),
        ("provider_failure", "provider_failure"),
    ],
)
def test_task_outcome_precedence_with_trusted_entry(reason, outcome):
    result = task_outcome_record(exit_code=0, reason=reason, trusted_entry_blocked=True)
    assert result["outcome"] == outcome and result["verified_success"] is False
