"""A selected project is a default directory, not the identity of every check."""

from types import SimpleNamespace

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionEnforcement,
    AcceptanceCriterionKind,
    build_acceptance_contract,
)
from alysis_code.agent.task_evidence import (
    TaskEvidenceState,
    persist_task_evidence,
    restore_task_evidence,
    seed_execution_state,
)
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.run_outcome import task_outcome_record


def _check(root, state, cwd, *, code=0):
    result = {
        "cmd": "python -m unittest",
        "cwd": str(cwd),
        "exit_code": code,
        "stdout": "Ran 1 test in 0.01s\n\n" + ("OK\n" if code == 0 else "FAILED (failures=1)\n"),
        "stderr": "",
        "touched_repo_paths": [],
    }
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": result["cmd"], "cwd": str(cwd)},
        status="ok",
        result=result,
        known_verification_commands=["python -m unittest"],
    )
    return result


@pytest.fixture
def scopes(tmp_path):
    root = tmp_path / "selected"
    other = tmp_path / "Δοκιμή  資料"
    root.mkdir()
    other.mkdir()
    state = TurnExecutionState(execution_requested=True)
    state.acceptance_contract = build_acceptance_contract(root=root, instruction="")
    return root, other, state


def test_external_pass_is_observed_without_becoming_a_project_check(scopes):
    root, other, state = scopes
    result = _check(root, state, other)
    assert result["evidence_verdict"] == "supplemental"
    assert state.last_verification_passed is True
    assert not state.observed_verification_failures
    assert not state.covered_verification_commands
    assert not state.accepted_verification_evidence
    assert not state.test_baselines  # External IDs cannot seed the selected project's baseline.
    observation = state.executed_verification_evidence[-1]
    assert observation["working_directory"] == str(other.resolve())
    assert observation["observed_exit_code"] == 0
    outcome = task_outcome_record(
        exit_code=0,
        reason="completed",
        state={
            "executed_verification_evidence": state.executed_verification_evidence,
        },
    )
    assert outcome["observed_passing_checks"] == 1
    assert not outcome["verified_success"]


@pytest.mark.parametrize("failing_scope", ["selected", "other"])
def test_same_command_elsewhere_cannot_clear_a_failure(scopes, failing_scope):
    root, other, state = scopes
    failed, passed = (root, other) if failing_scope == "selected" else (other, root)
    _check(root, state, failed, code=1)
    _check(root, state, passed)
    assert state.last_verification_passed is False
    assert len(state.observed_verification_failures) == 1
    _check(root, state, failed)
    assert not state.observed_verification_failures
    assert state.last_verification_passed is True


def test_external_failure_does_not_poison_required_project_command(scopes):
    root, other, state = scopes
    state.expected_verification_commands = {"python -m unittest"}
    state.acceptance_contract = build_acceptance_contract(
        root=root, instruction="Run `python -m unittest`."
    )
    _check(root, state, root)
    criterion = next(c for c in state.acceptance_contract.criteria if c.commands)
    assert criterion.status.value == "PASSED"
    _check(root, state, other, code=1)
    assert criterion.status.value == "PASSED"
    assert state.covered_verification_commands == {"python -m unittest"}
    assert not state.failed_verification_commands()
    assert state.last_verification_passed is False  # The external failure still matters.


def test_external_edits_have_exact_paths_and_expire_old_checks(scopes):
    root, other, state = scopes
    target = other / "任意  όνομα.custom-extension"
    _check(root, state, other)
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="fs_write",
        arguments={"path": str(target), "content": "new content"},
        status="ok",
        result={"path": str(target), "scoped_workspace": str(other), "created": True},
        known_verification_commands=[],
    )
    assert state.touched_external_paths == {str(target)}
    assert not state.touched_repo_paths
    assert state.verification_relevant_edit_generation == 1
    assert state.acceptance_contract.evidence[-1].paths == (str(target),)
    outcome = task_outcome_record(
        exit_code=0,
        reason="completed",
        state={
            "touched_external_paths": sorted(state.touched_external_paths),
            "verification_relevant_edit_generation": state.verification_relevant_edit_generation,
            "executed_verification_evidence": state.executed_verification_evidence,
        },
    )
    assert outcome["material_paths"] == [str(target)]
    assert outcome["observed_passing_checks"] == 0


@pytest.mark.parametrize("quoted_json", [False, True])
def test_observed_external_output_satisfies_exact_required_path(scopes, quoted_json):
    import json

    from alysis_code.agent.acceptance_contract import _missing_required_output_paths

    root, other, state = scopes
    target = other / "new  αρχείο.txt"
    literal = json.dumps(str(target), ensure_ascii=False) if quoted_json else f'"{target}"'
    state.acceptance_contract = build_acceptance_contract(
        root=root, instruction=f"Create {literal}."
    )
    criterion = next(c for c in state.acceptance_contract.criteria if c.required)
    target.write_text("verified fixture", encoding="utf-8")
    # Host existence alone must not satisfy an unobserved external output.
    assert _missing_required_output_paths(criterion=criterion, root=root)
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="fs_write",
        arguments={"path": str(target), "content": "verified fixture"},
        status="ok",
        result={"path": str(target), "scoped_workspace": str(other), "created": True},
        known_verification_commands=[],
    )
    observations = state.acceptance_contract.external_path_observations
    assert criterion.status.value == "PASSED"
    assert not _missing_required_output_paths(
        criterion=criterion, root=root, external_path_observations=observations
    )
    # A once-observed output that disappears cannot keep its passed verdict.
    target.unlink()
    assert _missing_required_output_paths(
        criterion=criterion, root=root, external_path_observations=observations
    )


def test_external_edit_ledger_survives_resume_without_granting_authority(scopes):
    root, other, state = scopes
    state.touched_external_paths.add(str(other / "changed.data"))
    state.verification_relevant_edit_generation = 2
    ledger = TaskEvidenceState("task", str(root.resolve()), state.acceptance_contract.snapshot)
    events = []
    session = SimpleNamespace(
        root=root,
        _task_evidence_state=ledger,
        store=SimpleNamespace(append=lambda *args: events.append(args)),
    )
    persist_task_evidence(session, state)
    restore_task_evidence(
        session, ledger.to_payload(), event_session_id="chat", expected_session_id="chat"
    )
    restored = TurnExecutionState(execution_requested=True)
    seed_execution_state(restored, session._task_evidence_state)
    assert restored.touched_external_paths == state.touched_external_paths
    assert restored.verification_relevant_edit_generation == 2
    assert not restored.accepted_verification_evidence


@pytest.mark.parametrize(
    "name",
    ["Δοκιμή  website/τίτλος.widget", "資料  website/名前.widget", "résumé  website/title.widget"],
)
def test_ambiguous_external_prose_cannot_invent_required_fragments(scopes, name):
    root, other, _ = scopes
    contract = build_acceptance_contract(root=root, instruction=f"Create {other / name} please.")
    path_criteria = [c for c in contract.criteria if c.path_refs]
    assert path_criteria
    assert all(c.enforcement == AcceptanceCriterionEnforcement.ADVISORY for c in path_criteria)
    assert not contract.allowed_output_paths


@pytest.mark.parametrize(
    "name",
    ["Δοκιμή  website/τίτλος.widget", "資料  website/名前.widget", "résumé  website/title.widget"],
)
def test_literal_external_paths_preserve_exact_unicode_and_spacing(scopes, name):
    root, other, _ = scopes
    target = other / name
    contract = build_acceptance_contract(root=root, instruction=f'Create "{target}".')
    required = [
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    ]
    assert len(required) == 1
    assert required[0].enforcement == AcceptanceCriterionEnforcement.HARD
    assert required[0].path_refs[0].absolute_path == target.as_posix()


def test_unquoted_unicode_relative_path_has_no_extension_whitelist(scopes):
    root, _, _ = scopes
    contract = build_acceptance_contract(
        root=root, instruction="Create 資料/τίτλος.unlisted_extension."
    )
    assert contract.allowed_output_paths == {"資料/τίτλος.unlisted_extension"}


def test_relative_output_in_external_task_does_not_require_a_project_copy(scopes):
    root, other, _ = scopes
    contract = build_acceptance_contract(
        root=root, instruction=f'Inspect "{other}". Create shell-note.txt in that directory.'
    )
    output = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert output.enforcement == AcceptanceCriterionEnforcement.ADVISORY
    assert not contract.allowed_output_paths
    assert not contract.expectations


def test_absolute_project_requirement_remains_exact_in_a_mixed_task(scopes):
    root, other, _ = scopes
    contract = build_acceptance_contract(
        root=root, instruction=f'Inspect "{other}". Create "{root / "summary.txt"}".'
    )
    output = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert output.enforcement == AcceptanceCriterionEnforcement.HARD
    assert contract.allowed_output_paths == {"summary.txt"}


def test_external_prose_does_not_turn_a_command_or_format_into_a_project_check(scopes):
    root, other, _ = scopes
    contract = build_acceptance_contract(
        root=root,
        instruction=f'Inspect "{other}". Create report.json as valid JSON. Run `python -m unittest` there.',
        authoritative_verification_commands=["python -m unittest"],
    )
    for criterion in contract.criteria:
        if criterion.kind in {
            AcceptanceCriterionKind.EXPLICIT_COMMAND_IO,
            AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA,
        }:
            assert criterion.enforcement == AcceptanceCriterionEnforcement.ADVISORY
        if criterion.kind == AcceptanceCriterionKind.EXPLICIT_HOST_USER_VERIFICATION_COMMAND:
            assert criterion.enforcement == AcceptanceCriterionEnforcement.HARD


def test_absolute_interpreter_is_not_an_external_task_folder(scopes):
    root, _, _ = scopes
    contract = build_acceptance_contract(
        root=root,
        instruction='Run `"/opt/toolchain/bin/python" check.py`. Preserve check.py unchanged.',
    )
    assert not any(ref.absolute_path.startswith("/opt/") for ref in contract.path_refs)
    commands = [item for item in contract.criteria if item.commands]
    assert len(commands) == 1
    assert commands[0].enforcement == AcceptanceCriterionEnforcement.HARD
