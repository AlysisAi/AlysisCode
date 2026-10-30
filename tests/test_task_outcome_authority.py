from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.completion_gate import CompletionGateControllerState
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.run_outcome import task_outcome_notice, task_outcome_record


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("deadline_exhausted", "deadline_exceeded"),
        ("provider_failure_salvaged", "provider_failure"),
        ("cancelled_by_user", "cancelled"),
        ("max_steps_exhausted", "incomplete"),
        ("blocked", "blocked"),
        ("completed", "completed_unverified"),
    ],
)
def test_exit_zero_does_not_prove_verified_success(reason: str, expected: str) -> None:
    result = task_outcome_record(exit_code=0, reason=reason)
    assert result["outcome"] == expected
    assert not result["verified_success"]


def _sufficient_check_state():
    return {
        "completion_certificate": {"status": "SUFFICIENT", "problems": []},
        "acceptance_contract": {
            "criteria": [
                {
                    "id": "result",
                    "enforcement": "HARD",
                    "required": True,
                    "required_for_finalization": True,
                    "status": "PASSED",
                }
            ]
        },
        "accepted_verification_evidence": [
            {
                "real_execution": True,
                "observed_exit_code": 0,
                "observed_output": True,
                "generation": 0,
                "evidence_category": "REPO_NATIVE",
            }
        ],
    }


@pytest.mark.parametrize("applicability", [False, True, None, "false", 0])
@pytest.mark.parametrize("observed_failure", [False, True])
def test_outcome_uses_host_check_applicability_without_hiding_observed_failures(
    applicability, observed_failure
):
    state = {
        **_sufficient_check_state(),
        "verification_expected": applicability,
        "missing_verification_commands": ["npm test"],
        "verification_coverage_stale": True,
        "unresolved_observed_verification_failures": int(observed_failure),
    }
    record = task_outcome_record(exit_code=0, reason="completed", state=state)
    expected = [] if applicability is False else ["verification_incomplete", "verification_stale"]
    if observed_failure:
        expected.append("verification_failed")
    assert set(record["problems"]) == set(expected)
    assert record["verified_success"] is (applicability is False and not observed_failure)


def test_outstanding_observed_check_failure_survives_required_checks_and_other_passing_evidence():
    state = {
        **_sufficient_check_state(),
        "failed_verification_commands": [],
        "unresolved_observed_verification_failures": 1,
    }
    record = task_outcome_record(exit_code=0, reason="completed", state=state)
    assert record["outcome"] == "completed_unverified"
    assert record["verified_success"] is False
    assert record["problems"] == ["verification_failed"]
    assert "checks failed" in task_outcome_notice(record)
    # Only the execution ledger's successful resolution removes this failure.
    state["unresolved_observed_verification_failures"] = 0
    cleared = task_outcome_record(exit_code=0, reason="completed", state=state)
    assert cleared["verified_success"] is True
    assert task_outcome_notice(cleared) == ""


@pytest.mark.parametrize("count", [None, True, "1", -1])
def test_malformed_failure_count_cannot_prove_success_or_invent_a_failed_check(count):
    record = task_outcome_record(
        exit_code=0,
        reason="completed",
        state={**_sufficient_check_state(), "unresolved_observed_verification_failures": count},
    )
    assert record["verified_success"] is False
    assert record["problems"] == ["verification_evidence_unavailable"]
    assert "unconfirmed" in task_outcome_notice(record)


@pytest.mark.parametrize(
    "state",
    [
        {},
        {
            "completion_certificate": {"status": "SUFFICIENT", "problems": []},
            "verification_attempt_count": 3,
        },
        {"material_edit_count": 1, "touched_repo_paths": ["answer.txt"]},
        {"artifact_checks": [{"items": [{"status": "passed", "basis": "observed"}]}]},
    ],
)
def test_uncertified_completion_is_not_evidence_that_no_check_ran(state):
    import copy

    record = task_outcome_record(exit_code=0, reason="completed", state=state)
    original = copy.deepcopy(record)
    assert record["outcome"] == "completed_unverified"
    assert task_outcome_notice(record) == ""
    assert record == original
    assert record["verified_success"] is False


@pytest.mark.parametrize(
    "problem",
    [
        "verification_failed",
        "acceptance_criteria_failed",
        "regressions_detected",
        "unattributed_failures",
        "repro_failing_after_fix",
        "blast_radius_regressions",
    ],
)
def test_passing_checks_never_hide_other_failed_validation(problem):
    assert "checks failed" in task_outcome_notice(
        {
            "outcome": "completed_unverified",
            "problems": [problem],
            "observed_passing_checks": 4,
        }
    )


@pytest.mark.parametrize(
    "problem",
    [
        "verification_not_attempted",
        "verification_incomplete",
        "verification_stale",
        "acceptance_criteria_unverified",
        "repro_unconfirmed",
        "blast_radius_unverified",
    ],
)
def test_known_unmet_requirements_still_have_a_visible_notice(problem):
    notice = task_outcome_notice({"outcome": "completed_unverified", "problems": [problem]})
    assert notice and "unconfirmed" in notice
    assert "checks failed" not in notice


@pytest.mark.parametrize(
    "reason,word",
    [
        ("cancelled_by_user", "cancelled"),
        ("deadline_exhausted", "limit"),
        ("provider_failure", "service failed"),
        ("blocked", "blocked"),
        ("max_steps_exhausted", "incomplete"),
    ],
)
def test_terminal_notices_describe_the_actual_outcome(reason, word):
    notice = task_outcome_notice(task_outcome_record(exit_code=0, reason=reason))
    assert word in notice
    assert "no test or check" not in notice


def test_final_emission_preserves_specific_model_caveats_and_uncertified_metadata(tmp_path):
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "logs",
    )
    text = "The commit was created. Not verified: signature trust is unknown."
    record = task_outcome_record(exit_code=0, reason="completed")
    try:
        emitted = session._emit_final_assistant_text(
            final_text=text,
            allow_language_rewrite=False,
            final_event_payload={"task_outcome": record, "verification_notice": True},
        )
        assert emitted == text
        final = [e["payload"] for e in session.store.events_snapshot() if e["type"] == "final"][-1]
        assert final["verification_notice"] is False
        assert final["task_outcome"] == record
        assert session.messages[-1]["content"] == text
    finally:
        session.close()


@pytest.mark.parametrize("contradiction", [None, "failed", "missing", "stale"])
def test_passing_baseline_notice_distinguishes_unbound_task_from_check_failure(contradiction):
    state = {
        "completion_certificate": {"status": "SUFFICIENT"},
        "accepted_verification_evidence": [{"evidence_category": "AUTHORITATIVE"}],
        "last_verification_passed": True,
    }
    if contradiction == "failed":
        state["failed_verification_commands"] = ["node --test"]
    elif contradiction == "missing":
        state["missing_verification_commands"] = ["node --test"]
    elif contradiction == "stale":
        state["verification_coverage_stale"] = True
    result = task_outcome_record(exit_code=0, reason="completed", state=state)
    assert result["outcome"] == "completed_unverified"
    assert result["verified_success"] is False
    assert "no_material_edits" in result["problems"]
    if contradiction is None:
        assert task_outcome_notice(result) == ""
    elif contradiction == "failed":
        assert "verification_failed" in result["problems"]
        assert task_outcome_notice(result) == (
            "\n\nNot verified: required or observed checks failed."
        )
    else:
        assert (
            "verification_stale" if contradiction == "stale" else "verification_incomplete"
        ) in result["problems"]
        assert task_outcome_notice(result) == (
            "\n\nNot verified: some task requirements or checks remain unconfirmed."
        )


@pytest.mark.parametrize("contradiction", ["failed", "missing", "stale", "hard"])
def test_a_sufficient_certificate_cannot_override_current_unresolved_evidence(contradiction):
    state: dict[str, Any] = {
        "completion_certificate": {"status": "SUFFICIENT"},
        "accepted_verification_evidence": [{"evidence_category": "AUTHORITATIVE"}],
    }
    if contradiction == "failed":
        state["failed_verification_commands"] = ["pytest"]
    elif contradiction == "missing":
        state["missing_verification_commands"] = ["pytest"]
    elif contradiction == "stale":
        state["verification_coverage_stale"] = True
    else:
        state["acceptance_contract"] = {
            "criteria": [
                {
                    "criterion_id": "required",
                    "enforcement": "HARD",
                    "required": True,
                    "required_for_finalization": True,
                    "status": "UNVERIFIED",
                }
            ]
        }
    result = task_outcome_record(exit_code=0, reason="completed", state=state)
    assert result["outcome"] == "completed_unverified"
    assert result["problems"]


def test_prior_edit_generation_does_not_prove_the_current_request_was_implemented():
    result = task_outcome_record(
        exit_code=0,
        reason="completed",
        state={
            "completion_certificate": {"status": "SUFFICIENT"},
            "accepted_verification_evidence": [{"evidence_category": "AUTHORITATIVE"}],
            "verification_relevant_edit_generation": 7,
            "material_edit_count": 0,
        },
    )
    assert not result["verified_success"]
    assert "no_material_edits" in result["problems"]


def test_inferred_concern_does_not_veto_supported_hard_coverage():
    state = {
        "completion_certificate": {"status": "SUFFICIENT"},
        "accepted_verification_evidence": [{"evidence_category": "AUTHORITATIVE"}],
        "acceptance_contract": {
            "criteria": [
                {
                    "id": "required-check",
                    "enforcement": "HARD",
                    "required": True,
                    "required_for_finalization": True,
                    "status": "PASSED",
                },
                {
                    "enforcement": "ADVISORY",
                    "required": False,
                    "required_for_finalization": False,
                    "status": "UNVERIFIED",
                },
            ]
        },
    }
    assert task_outcome_record(exit_code=0, reason="completed", state=state)["verified_success"]


class _CheckClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, command: str):
        self.command = command
        self.calls = 0

    def chat(self, **_: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="check", name="shell_run", arguments={"cmd": self.command}),
                ],
                raw={},
            )
        return LLMResponse(content="All checks passed. The work is done.", tool_calls=[], raw={})


@pytest.mark.parametrize("check_passes", [True, False])
def test_real_no_edit_check_sets_task_outcome_and_exact_host_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_passes: bool
) -> None:
    from alysis_code.surface import ApprovalDecision
    from alysis_code.surface.noop_surface import NoopSurface

    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind == "shell_run"),
    )
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    host_output = tmp_path / "host" / "result.json"
    monkeypatch.setenv("ALYSIS_TASK_OUTCOME_PATH", str(host_output))
    (tmp_path / "check.py").write_text(
        "raise SystemExit(0)\n" if check_passes else "raise SystemExit(3)\n", encoding="utf-8"
    )
    command = '"' + sys.executable.replace("\\", "/") + '" check.py'
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", verify_commands=[command]),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=True,
        session_log_dir_override=tmp_path / "logs",
        session_id_override="outcome-root",
    )
    session.client = _CheckClient(command)
    try:
        code = session.run_turn(f"Run `{command}` and report the result. Do not modify files.")
        outcome = session.last_turn_outcome
        assert code == 0
        assert outcome["verified_success"] is check_passes
        assert outcome["outcome"] == (
            "verified_success" if check_passes else "completed_unverified"
        )
        assert outcome["generation"] == 0
        assert outcome["material_paths"] == []
        assert outcome["task_id"] == session._task_evidence_state.task_id
        assert json.loads(host_output.read_text(encoding="utf-8")) == outcome
        assert json.loads(session.store.path.with_suffix(".outcome.json").read_text()) == outcome
        finals = [e["payload"] for e in session.store.events_snapshot() if e["type"] == "final"]
        assert finals[-1]["task_outcome"] == outcome
        if not check_passes:
            assert "Not verified:" in finals[-1]["content"]
        # An inherited host path can never be overwritten by a child outcome.
        session.subagent_depth = 1
        session._record_task_outcome(exit_code=1, reason="blocked")
        assert json.loads(host_output.read_text(encoding="utf-8")) == outcome
    finally:
        session.close()


def _snapshot(generation: int, problem: str = "verification_failed", failure: str = "same"):
    return {
        "verification_relevant_edit_generation": generation,
        "problems": [problem],
        "failed_verification_signatures": [failure],
        "missing_verification_commands": ["pytest -q"],
    }


def test_repair_budget_is_shared_across_alternating_stages_and_resets_after_edit():
    state = CompletionGateControllerState()
    for problem in ("verification_failed", "acceptance_criteria_unverified", "repro_unconfirmed"):
        assert state.claim_repair(_snapshot(0, problem)) is None
    assert (
        state.claim_repair(_snapshot(0, "blast_radius_unverified"))
        == "shared_generation_repair_exhausted"
    )
    assert state.claim_repair(_snapshot(1, "blast_radius_unverified")) is None


def test_same_evidence_cannot_get_new_repair_budget_from_stage_label():
    state = CompletionGateControllerState()
    assert state.claim_repair({**_snapshot(0), "stage": "one"}) is None
    assert state.claim_repair({**_snapshot(0), "stage": "two"}) is None
    assert (
        state.claim_repair({**_snapshot(0), "stage": "three"})
        == "unchanged_evidence_repair_exhausted"
    )
    assert state.claim_repair(_snapshot(0, failure="a new observed failure")) is None


def test_server_command_completion_is_not_task_verification():
    from alysis_code.server.worker_runner import job_task_outcome

    command_event = {"event": "run_completed", "data": {"ok": True, "exit_code": 0}}
    result = job_task_outcome(status="succeeded", exit_code=0, event=command_event)
    assert result["outcome"] == "completed_unverified"
    assert result["verified_success"] is False
    positive = task_outcome_record(
        exit_code=0,
        reason="completed",
        state={
            "material_edit_count": 1,
            "completion_certificate": {"status": "SUFFICIENT"},
            "accepted_verification_evidence": [{"evidence_category": "HOST_AUTHORITATIVE"}],
        },
    )
    command_event["data"]["task_outcome"] = positive
    assert job_task_outcome(status="succeeded", exit_code=0, event=command_event)[
        "verified_success"
    ]
    assert not job_task_outcome(status="cancelled", exit_code=0, event=command_event)[
        "verified_success"
    ]
    assert not job_task_outcome(status="running", exit_code=None, event=command_event)[
        "verified_success"
    ]


@pytest.mark.parametrize(
    "reason", ["provider_failure", "deadline_exhausted", "cancelled", "blocked"]
)
def test_failed_server_job_preserves_recorded_task_failure_and_identity(reason):
    from alysis_code.server.worker_runner import job_task_outcome

    record = task_outcome_record(
        exit_code=1,
        reason=reason,
        task_id="current-task",
        state={"verification_relevant_edit_generation": 7},
    )
    event = {"event": "run_completed", "data": {"ok": False, "task_outcome": record}}
    result = job_task_outcome(status="failed", exit_code=1, event=event)
    assert result == record


def test_server_cancellation_and_failed_worker_override_success_without_losing_identity():
    from alysis_code.server.worker_runner import job_task_outcome

    record = task_outcome_record(
        exit_code=0,
        reason="completed",
        task_id="current-task",
        state={
            "completion_certificate": {"status": "SUFFICIENT"},
            "accepted_verification_evidence": [{}],
            "verification_relevant_edit_generation": 3,
            "material_edit_count": 1,
        },
    )
    event = {"event": "run_completed", "data": {"ok": True, "task_outcome": record}}
    for status, outcome in [("cancelled", "cancelled"), ("failed", "incomplete")]:
        result = job_task_outcome(status=status, exit_code=1, event=event)
        assert result["outcome"] == outcome and not result["verified_success"]
        assert result["task_id"] == "current-task" and result["generation"] == 3
    assert record["verified_success"] is True


def test_passing_read_only_checks_are_reported_without_claiming_whole_task_success():
    from alysis_code.run_outcome import task_outcome_notice

    state = {
        "accepted_verification_evidence": [
            {
                "real_execution": True,
                "observed_exit_code": 0,
                "observed_output": True,
            }
        ]
    }
    record = task_outcome_record(exit_code=0, reason="completed", state=state)
    assert not record["verified_success"]
    assert record["observed_passing_checks"] == 1
    assert task_outcome_notice(record) == ""
    state["failed_verification_commands"] = ["required-check"]
    record = task_outcome_record(exit_code=0, reason="completed", state=state)
    assert "Observed checks passed" not in task_outcome_notice(record)
    assert "checks failed" in task_outcome_notice(record)
