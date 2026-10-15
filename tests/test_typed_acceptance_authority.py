import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionEnforcement,
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    build_acceptance_contract,
    invalidate_acceptance_evidence,
    record_acceptance_tool_effect,
)


def _commands(contract):
    return [item for item in contract.criteria if item.commands]


def _record(contract, root, command="pytest -q", **overrides):
    result = {"effective_cmd": command, "exit_code": 0, "stdout": "2 passed in 0.10s"}
    result.update(overrides.pop("result", {}))
    record_acceptance_tool_effect(
        contract=contract,
        root=root,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok",
        result=result,
        touched_paths=set(),
        **overrides,
    )


@pytest.mark.parametrize(
    "instruction,symbol",
    [
        ("Run no more than `max_active` jobs concurrently.", "max_active"),
        ("Run no more than `parallel_limit` jobs concurrently.", "parallel_limit"),
        ("Implement the `Scheduler.cancel()` interface.", "Scheduler.cancel()"),
    ],
)
def test_symbols_are_typed_non_executable_context(tmp_path, instruction, symbol):
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert not _commands(contract)
    criterion = next(
        item
        for item in contract.criteria
        if item.kind == AcceptanceCriterionKind.PUBLIC_SYMBOL_INTERFACE
    )
    assert symbol in criterion.description
    assert criterion.enforcement == AcceptanceCriterionEnforcement.ADVISORY
    assert not criterion.required_for_finalization


@pytest.mark.parametrize("path", ["/work/scheduler.py", "src/worker.py", "C:/work/jobs.py"])
def test_file_locations_do_not_become_commands(tmp_path, path):
    contract = build_acceptance_contract(
        root=tmp_path, instruction=f"Implement the scheduler in `{path}` and test cancellation."
    )
    assert not _commands(contract)
    assert contract.path_refs


@pytest.mark.parametrize("command", ["pytest", "python -m pytest -q", "./check.sh"])
def test_direct_commands_remain_explicit(tmp_path, command):
    contract = build_acceptance_contract(root=tmp_path, instruction=f"Run `{command}`.")
    assert [item.commands for item in _commands(contract)] == [(command,)]
    assert _commands(contract)[0].required_for_finalization


@pytest.mark.parametrize(
    "instruction,command",
    [
        ("`python -m pytest -q` must pass.", "python -m pytest -q"),
        ("`pytest` must succeed.", "pytest"),
        ("Acceptance check: `pytest -q`", "pytest -q"),
        ("Verify using `pytest`.", "pytest"),
    ],
)
def test_explicit_checks_support_prefix_and_postfix_requirements(tmp_path, instruction, command):
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert [item.commands for item in _commands(contract)] == [(command,)]
    assert _commands(contract)[0].required_for_finalization


def test_current_instruction_is_acceptance_authority(tmp_path):
    contract = build_acceptance_contract(
        root=tmp_path, instruction="Run `pytest -q`. Create result.txt."
    )
    assert [item.commands for item in _commands(contract)] == [("pytest -q",)]
    assert contract.allowed_output_paths == {"result.txt"}


def test_inferred_command_suggestion_is_not_a_required_command(tmp_path):
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Consider running `python -m pytest -q` if useful.",
    )
    assert not _commands(contract)
    assert not contract.expectations


@pytest.mark.parametrize(
    "result",
    [
        {"stdout": "no tests ran in 0.01s", "real_execution": True},
        {"executed_test_count": 0, "real_execution": True},
        {"discovered_test_count": 0, "real_execution": True},
        {"real_execution": False},
    ],
)
def test_zero_or_nonexecuted_tests_cannot_pass(tmp_path, result):
    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    _record(contract, tmp_path, result=result, evidence_allowed=True)
    assert _commands(contract)[0].status != AcceptanceCriterionStatus.PASSED
    assert contract.evidence[-1].evidence_allowed is False


def test_explicit_command_respects_rejected_evidence(tmp_path):
    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    _record(contract, tmp_path, evidence_allowed=False)
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.BLOCKED


def test_evidence_records_host_identity_and_observed_metadata(tmp_path):
    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    contract.task_id = "task-one"
    _record(
        contract,
        tmp_path,
        evidence_allowed=True,
        result={"stdout": "collected 2 items\n2 passed in 0.10s", "result_id": "run-007"},
    )
    evidence = contract.evidence[-1].as_payload()
    assert evidence["task_id"] == "task-one"
    assert evidence["generation"] == 0
    assert evidence["tool_name"] == "shell_run"
    assert evidence["cwd"] == str(tmp_path)
    assert evidence["exit_code"] == 0
    assert evidence["discovered_test_count"] == evidence["executed_test_count"] == 2
    assert evidence["result_id"] == "run-007"
    assert evidence["criterion_ids"] == [_commands(contract)[0].criterion_id]


def test_relevant_edit_expires_pass_and_requires_current_evidence(tmp_path):
    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    _record(contract, tmp_path)
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.PASSED
    invalidate_acceptance_evidence(contract, generation=1)
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.UNVERIFIED
    _record(contract, tmp_path, generation=0)
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.UNVERIFIED
    assert contract.evidence[-1].evidence_allowed is False
    _record(contract, tmp_path, generation=1)
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.PASSED


def test_foreign_task_cannot_advance_generation_or_satisfy_requirement(tmp_path):
    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    contract.task_id = "task-one"
    _record(contract, tmp_path)
    _record(contract, tmp_path, task_id="task-two", generation=8)
    assert contract.generation == 0
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.PASSED
    assert not contract.evidence[-1].criterion_ids


def test_check_from_different_cwd_does_not_satisfy_requirement(tmp_path):
    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    _record(contract, tmp_path, result={"cwd": str(tmp_path.parent)})
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.BLOCKED


def test_multi_command_results_keep_independent_authority_and_outcomes(tmp_path):
    commands = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    contract = build_acceptance_contract(
        root=tmp_path, instruction="", authoritative_verification_commands=commands
    )
    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="verify_run",
        arguments={"commands": commands},
        status="failed",
        result={
            "commands": commands,
            "all_passed": False,
            "command_results": [
                {
                    "command": commands[0],
                    "exit_code": 0,
                    "ok": True,
                    "output": "2 passed in 0.1s",
                    "verification_evidence_allowed": True,
                },
                {
                    "command": commands[1],
                    "exit_code": 1,
                    "ok": False,
                    "output": "1 failed in 0.1s",
                    "verification_evidence_allowed": False,
                },
            ],
        },
        touched_paths=set(),
        known_verification_commands=commands,
        verification_authoritative=True,
        evidence_allowed=False,
    )
    criteria = _commands(contract)
    assert criteria[0].status == AcceptanceCriterionStatus.PASSED
    assert criteria[1].status != AcceptanceCriterionStatus.PASSED
    assert [item.command for item in contract.evidence] == commands
    assert [item.exit_code for item in contract.evidence] == [0, 1]


def test_unittest_success_counts_real_tests_and_no_edit_stays_valid(tmp_path):
    command = "python -m unittest"
    contract = build_acceptance_contract(root=tmp_path, instruction=f"Run `{command}`.")
    _record(contract, tmp_path, command, result={"stdout": "Ran 3 tests in 0.01s\n\nOK"})
    assert contract.evidence[-1].executed_test_count == 3
    invalidate_acceptance_evidence(contract, generation=0)
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.PASSED


@pytest.mark.parametrize(
    "instruction",
    [
        "Document `pytest -q` in README.",
        "The old check was `pytest -q`.",
        "For example run `pytest -q`.",
        "The `pytest` package is used here.",
    ],
)
def test_command_mentions_never_acquire_hard_authority(tmp_path, instruction):
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert not _commands(contract)


def test_inferred_artifact_and_threshold_remain_nonblocking(tmp_path):
    contract = build_acceptance_contract(
        root=tmp_path, instruction="Consider creating `extra.txt` with memory under 20mb."
    )
    assert not contract.required_criteria()


def test_delegated_requirements_keep_their_source(tmp_path):
    contract = build_acceptance_contract(
        root=tmp_path, instruction="Run `pytest -q`.", delegated=True
    )
    assert _commands(contract)[0].source.value == "delegated_task"
    assert _commands(contract)[0].required_for_finalization


def test_unknown_task_state_cannot_be_promoted_into_authority(tmp_path):
    with pytest.raises(TypeError, match="task_state"):
        build_acceptance_contract(root=tmp_path, instruction="", task_state={"objective": "run"})


def test_mutating_batch_does_not_allow_unordered_sibling_evidence(tmp_path):
    commands = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    contract = build_acceptance_contract(
        root=tmp_path, instruction="", authoritative_verification_commands=commands
    )
    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="verify_run",
        arguments={},
        status="ok",
        result={
            "commands": commands,
            "all_passed": True,
            "command_results": [
                {
                    "command": command,
                    "exit_code": 0,
                    "ok": True,
                    "output": "1 passed in 0.1s",
                    "verification_evidence_allowed": True,
                }
                for command in commands
            ],
        },
        touched_paths={"product.py"},
        known_verification_commands=commands,
        verification_authoritative=True,
        evidence_allowed=True,
    )
    assert all(item.status != AcceptanceCriterionStatus.PASSED for item in _commands(contract))
    assert all(item.evidence_allowed is False for item in contract.evidence)


def test_aggregate_pass_without_observed_exit_cannot_verify_explicit_check(tmp_path):
    contract = build_acceptance_contract(root=tmp_path, instruction="Run `pytest -q`.")
    _record(contract, tmp_path, result={"exit_code": None, "all_passed": True})
    assert _commands(contract)[0].status != AcceptanceCriterionStatus.PASSED


@pytest.mark.parametrize(
    "instruction",
    [
        "Do not run `pytest -q`.",
        "Don't run `pytest -q`.",
        "Don’t run `pytest -q`.",
        "Never execute `./check.sh`.",
        "Review the code without running `pytest -q`.",
        "Do not run the command: `pytest -q`.",
        "Do not verify using `pytest -q`.",
    ],
)
def test_negated_command_introductions_do_not_create_execution_obligations(tmp_path, instruction):
    contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
    assert not _commands(contract)


def test_command_prohibition_does_not_leak_to_later_positive_command(tmp_path):
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction="Never execute `./check.sh`, but run `pytest -q`.",
    )
    assert [item.commands for item in _commands(contract)] == [("pytest -q",)]


def test_negation_inside_quoted_command_does_not_change_user_authority(tmp_path):
    command = "python -c \"assert 'do not run' != ''\""
    contract = build_acceptance_contract(root=tmp_path, instruction=f"Run `{command}`.")
    assert [item.commands for item in _commands(contract)] == [(command,)]


def _record_host_expansion(
    tmp_path,
    *,
    allowed=True,
    generation=0,
    touched=(),
    known=True,
    cwd=None,
    authoritative=True,
    covered_command=None,
):
    required = "pytest tests/test_*.py -q"
    executed = "pytest tests/test_one.py tests/test_two.py -q"
    (tmp_path / "tests").mkdir()
    for name in ("one", "two"):
        (tmp_path / "tests" / f"test_{name}.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
    contract = build_acceptance_contract(
        root=tmp_path,
        instruction=f"Run `{required}`.",
        authoritative_verification_commands=[required] if authoritative else [],
    )
    record_acceptance_tool_effect(
        contract=contract,
        root=tmp_path,
        tool_name="verify_run",
        arguments={},
        status="ok",
        result={
            "commands": [required],
            "all_passed": True,
            "command_results": [
                {
                    "command": required,
                    "effective_command": executed,
                    "exit_code": 0,
                    "ok": True,
                    "output": "2 passed in 0.1s",
                    "verification_evidence_allowed": allowed,
                    "verification_evidence_covered_commands": [covered_command or required],
                    "cwd": str(cwd or tmp_path),
                }
            ],
        },
        touched_paths=set(touched),
        known_verification_commands=[required] if known else [],
        verification_authoritative=authoritative,
        evidence_allowed=allowed,
        generation=generation,
    )
    return contract, executed


def test_host_expanded_command_preserves_required_and_actual_command_identities(tmp_path):
    contract, executed = _record_host_expansion(tmp_path)
    assert all(item.status == AcceptanceCriterionStatus.PASSED for item in _commands(contract))
    evidence = contract.evidence[-1]
    assert evidence.command == executed
    assert evidence.origin.value == "HOST_AUTHORITATIVE"
    assert set(evidence.criterion_ids) == {item.criterion_id for item in _commands(contract)}


@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed": False},
        {"generation": -1},
        {"touched": ("product.py",)},
        {"covered_command": "pytest tests/unrequested.py -q"},
    ],
)
def test_host_coverage_cannot_override_rejection_staleness_mutation_or_unknown_contract(
    tmp_path, overrides
):
    contract, _ = _record_host_expansion(tmp_path, **overrides)
    assert all(item.status != AcceptanceCriterionStatus.PASSED for item in _commands(contract))


def test_host_expansion_coverage_requires_the_expected_workspace(tmp_path):
    contract, _ = _record_host_expansion(tmp_path, cwd=tmp_path.parent)
    assert all(item.status != AcceptanceCriterionStatus.PASSED for item in _commands(contract))


def test_explicit_user_glob_coverage_keeps_user_provenance(tmp_path):
    contract, executed = _record_host_expansion(tmp_path, known=False, authoritative=False)
    assert all(item.status == AcceptanceCriterionStatus.PASSED for item in _commands(contract))
    assert contract.evidence[-1].command == executed
    assert contract.evidence[-1].origin.value == "USER_EXPLICIT"


def test_shell_result_cannot_supply_additional_host_coverage(tmp_path):
    required = "pytest tests/test_required.py -q"
    contract = build_acceptance_contract(root=tmp_path, instruction=f"Run `{required}`.")
    _record(
        contract,
        tmp_path,
        command="pytest tests/test_other.py -q",
        known_verification_commands=[required],
        evidence_allowed=True,
        result={"verification_evidence_covered_commands": [required]},
    )
    assert _commands(contract)[0].status == AcceptanceCriterionStatus.UNVERIFIED
