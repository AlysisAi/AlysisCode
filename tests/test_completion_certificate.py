from __future__ import annotations

from pathlib import Path

from acceptance_declared import declared_contract

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    build_acceptance_contract,
    finalize_acceptance_contract,
    record_acceptance_tool_effect,
)
from alysis_code.agent.completion_certificate import (
    CompletionCertificateInput,
    CompletionCertificateStatus,
    evaluate_completion_certificate,
)
from alysis_code.agent_loop import (
    CompletionGateControllerState,
    CompletionGateDecisionKind,
    TurnExecutionState,
    _completion_gate_problems,
    build_completion_gate_snapshot,
    decide_completion_gate,
)


def _certificate(
    *,
    contract,
    material_edit_count: int = 1,
    verification_expected: bool = False,
    verification_attempt_count: int = 0,
    last_verification_passed: bool | None = None,
    failed_verification_commands: set[str] | None = None,
    expected_verification_commands: set[str] | None = None,
    missing_verification_commands: set[str] | None = None,
    accepted_verification_evidence: list[dict[str, object]] | None = None,
):
    return evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=contract,
            final_text="Done.",
            blocked=False,
            blocker_valid=False,
            material_edit_count=material_edit_count,
            require_material_result=True,
            verification_expected=verification_expected,
            verification_attempt_count=verification_attempt_count,
            last_verification_passed=last_verification_passed,
            failed_verification_commands=failed_verification_commands or set(),
            expected_verification_commands=expected_verification_commands or set(),
            missing_verification_commands=missing_verification_commands or set(),
            accepted_verification_evidence=accepted_verification_evidence or [],
        )
    )


def _first_criterion(contract, kind: AcceptanceCriterionKind):  # type: ignore[no-untyped-def]
    matches = [criterion for criterion in contract.criteria if criterion.kind == kind]
    assert matches
    return matches[0]


def test_advisory_unverified_criteria_do_not_block_completion(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Compare with reference model_ref.xml.",
    )

    certificate = _certificate(contract=contract)

    assert certificate.status == CompletionCertificateStatus.SUFFICIENT
    assert certificate.problems == ()


def test_hard_missing_output_blocks_completion(tmp_path: Path) -> None:
    contract = declared_contract(
        root=tmp_path, instruction="Create result.txt.", roles={"result.txt": "required_output"}
    )
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())

    certificate = _certificate(contract=contract)

    assert certificate.status == CompletionCertificateStatus.INSUFFICIENT
    assert "acceptance_criteria_unverified" in certificate.problems


def test_known_threshold_failure_blocks_completion_despite_green_tests(tmp_path: Path) -> None:
    contract = declared_contract(
        root=tmp_path,
        instruction="The result must report accuracy at least 90%.",
        roles={},
        thresholds=[{"metric": "accuracy", "operator": ">=", "value": 90, "unit": "%"}],
    )
    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_run",
        arguments={"cmd": "python eval.py"},
        status="ok",
        result={"effective_cmd": "python eval.py", "exit_code": 0, "stdout": "accuracy 72\n"},
        touched_paths=set(),
    )

    certificate = _certificate(
        contract=contract,
        verification_expected=True,
        verification_attempt_count=1,
        last_verification_passed=True,
        expected_verification_commands={"pytest -q"},
        accepted_verification_evidence=[{"evidence_category": "AUTHORITATIVE"}],
    )

    assert certificate.status == CompletionCertificateStatus.CONTRADICTED
    assert "acceptance_criteria_failed" in certificate.problems
    assert "verification_failed" not in certificate.problems


def test_explicit_preservation_violation_blocks_completion(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("keep\n", encoding="utf-8")
    contract = declared_contract(
        root=tmp_path,
        instruction="Do not modify README.md.",
        roles={"README.md": "preservation_target"},
    )
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths={"README.md"})

    certificate = _certificate(contract=contract)

    assert certificate.status == CompletionCertificateStatus.CONTRADICTED
    assert "acceptance_criteria_failed" in certificate.problems
    assert "unexpected_scope_changes" in certificate.problems


def test_current_generation_authoritative_verification_plus_hard_coverage_is_sufficient(
    tmp_path: Path,
) -> None:
    (tmp_path / "result.txt").write_text("ok\n", encoding="utf-8")
    contract = declared_contract(
        root=tmp_path, instruction="Create result.txt.", roles={"result.txt": "required_output"}
    )
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths={"result.txt"})

    certificate = _certificate(
        contract=contract,
        verification_expected=True,
        verification_attempt_count=1,
        last_verification_passed=True,
        expected_verification_commands={"pytest -q"},
        accepted_verification_evidence=[{"evidence_category": "AUTHORITATIVE"}],
    )

    assert certificate.status == CompletionCertificateStatus.SUFFICIENT
    assert certificate.problems == ()
    assert certificate.covered_hard_criterion_ids == ("ac001",)
    assert certificate.evidence_hierarchy == ("HOST_AUTHORITATIVE",)


def test_exact_task_native_direct_evidence_covers_mapped_command_criterion(
    tmp_path: Path,
) -> None:
    (tmp_path / "expected.txt").write_text("ok\n", encoding="utf-8")
    (tmp_path / "actual.txt").write_text("ok\n", encoding="utf-8")
    contract = declared_contract(
        root=tmp_path,
        instruction="Create actual.txt and run `diff expected.txt actual.txt`.",
        roles={},
        commands=["diff expected.txt actual.txt"],
    )
    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="shell_run",
        arguments={"cmd": "diff expected.txt actual.txt"},
        status="ok",
        result={
            "effective_cmd": "diff expected.txt actual.txt",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        },
        touched_paths=set(),
    )
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths={"actual.txt"})

    command = _first_criterion(contract, AcceptanceCriterionKind.EXPLICIT_COMMAND_IO)
    certificate = _certificate(contract=contract)

    assert command.status == AcceptanceCriterionStatus.PASSED
    assert certificate.status == CompletionCertificateStatus.SUFFICIENT


def test_self_authored_pytest_alone_cannot_cover_unrelated_hard_output(
    tmp_path: Path,
) -> None:
    contract = declared_contract(
        root=tmp_path, instruction="Create result.txt.", roles={"result.txt": "required_output"}
    )
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())

    certificate = _certificate(
        contract=contract,
        verification_expected=True,
        verification_attempt_count=1,
        last_verification_passed=True,
        expected_verification_commands={"pytest -q"},
        accepted_verification_evidence=[{"origin": "SELF_AUTHORED"}],
    )

    assert certificate.status == CompletionCertificateStatus.INSUFFICIENT
    assert "acceptance_criteria_unverified" in certificate.problems


def test_green_certificate_allows_completion_gate_without_extra_nudge(tmp_path: Path) -> None:
    (tmp_path / "result.txt").write_text("ok\n", encoding="utf-8")
    contract = declared_contract(
        root=tmp_path, instruction="Create result.txt.", roles={"result.txt": "required_output"}
    )
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths={"result.txt"})
    state = TurnExecutionState(
        execution_requested=True,
        material_edit_count=1,
        acceptance_contract=contract,
    )

    problems = _completion_gate_problems(
        state=state,
        final_text="Created result.txt.",
        blocked=False,
        verification_expected=False,
        require_material_edit_evidence=True,
    )
    snapshot = build_completion_gate_snapshot(
        stage="complete",
        problems=problems,
        material_edit_count=state.material_edit_count,
        material_edit_tools={"fs_write"},
        touched_repo_paths={"result.txt"},
        verification_relevant_edit_generation=1,
        last_successful_verification_generation=None,
        expected_verification_commands=set(),
        covered_verification_commands=set(),
        missing_verification_commands=set(),
        failed_verification_command_snippets={},
        verification_coverage_stale=False,
        last_verification_passed=None,
        verification_expected=False,
        final_text="Created result.txt.",
        repo_tool_activity_observed=True,
        acceptance_status_counts=contract.status_counts(),
        acceptance_problems=contract.problem_names(),
        acceptance_failure_summaries=contract.failure_summaries(),
    )

    decision = decide_completion_gate(CompletionGateControllerState(), snapshot)

    assert problems == []
    assert state.latest_completion_certificate["status"] == "SUFFICIENT"
    assert decision.kind == CompletionGateDecisionKind.ALLOW_FINAL
