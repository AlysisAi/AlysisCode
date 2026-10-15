"""Acceptance authority through real tool results and host-owned session turns.

The verifier and shell execute small local fixtures. Their production payloads
then enter the same TurnExecutionState recorder used by the agent turn loop.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    EvidenceOrigin,
    build_acceptance_contract,
    finalize_acceptance_contract,
)
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.tools.fs import fs_read, fs_write
from alysis_code.tools.shell import shell_run
from alysis_code.verify_gate import run_task_verification, verify_run_result_to_payload


@pytest.fixture(autouse=True)
def _local_verifier_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("answer = 2\n", encoding="utf-8")
    (tmp_path / "tests" / "test_ok.py").write_text(
        "from app import answer\n\ndef test_answer():\n    assert answer == 2\n",
        encoding="utf-8",
    )
    return tmp_path


def _state(root: Path, commands: list[str], *, objective: str | None = None) -> TurnExecutionState:
    if objective is None:
        objective = "Update the implementation. " + " ".join(f"Run `{cmd}`." for cmd in commands)
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands=set(commands),
        acceptance_contract=build_acceptance_contract(
            root=root,
            instruction=objective,
            authoritative_verification_commands=commands,
            effective_verification_commands=commands,
        ),
    )
    state.acceptance_contract.task_id = "acceptance-integration:1"
    return state


def _command_criterion(state: TurnExecutionState, command: str) -> Any:
    assert state.acceptance_contract is not None
    matches = [
        item
        for item in state.acceptance_contract.criteria
        if item.kind == AcceptanceCriterionKind.EXPLICIT_COMMAND_IO and item.commands == (command,)
    ]
    assert len(matches) == 1
    return matches[0]


def _record(
    root: Path,
    state: TurnExecutionState,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    _record_tool_effect(
        root=root,
        state=state,
        tool_name=tool_name,
        arguments=arguments,
        status="ok",
        result=result,
        known_verification_commands=sorted(state.expected_verification_commands),
        verification_authoritative=True,
    )


def _verify(root: Path, state: TurnExecutionState, commands: list[str]) -> dict[str, Any]:
    run_result = run_task_verification(
        root=root,
        commands=commands,
        artifact_path=root / ".verification" / "result.txt",
        timeout_s=30,
    )
    payload = verify_run_result_to_payload(root=root, result=run_result)
    _record(root, state, tool_name="verify_run", arguments={}, result=payload)
    return payload


def test_relevant_edit_invalidates_pass_until_real_recheck(python_repo: Path) -> None:
    command = "pytest -q tests/test_ok.py"
    state = _state(python_repo, [command])
    assert _verify(python_repo, state, [command])["status"] == "passed"
    criterion = _command_criterion(state, command)
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert state.covered_verification_commands == {command}
    original_evidence_ids = tuple(criterion.evidence_ids)

    arguments = {"path": "app.py", "content": "# implementation revised\nanswer = 2\n"}
    _record(
        python_repo,
        state,
        tool_name="fs_write",
        arguments=arguments,
        result=fs_write(root=python_repo, **arguments),
    )

    assert state.verification_relevant_edit_generation == 1
    assert criterion.status != AcceptanceCriterionStatus.PASSED
    assert command not in state.covered_verification_commands
    finalize_acceptance_contract(
        contract=state.acceptance_contract, root=python_repo, touched_paths=state.touched_repo_paths
    )
    assert criterion.status != AcceptanceCriterionStatus.PASSED

    assert _verify(python_repo, state, [command])["status"] == "passed"
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert state.covered_verification_commands == {command}
    assert tuple(criterion.evidence_ids) != original_evidence_ids
    assert state.acceptance_contract is not None
    evidence = [item for item in state.acceptance_contract.evidence if item.command == command]
    assert [item.generation for item in evidence] == [0, 1]
    assert all(item.task_id == "acceptance-integration:1" for item in evidence)


def test_read_only_activity_keeps_verification_valid(python_repo: Path) -> None:
    command = "pytest -q tests/test_ok.py"
    state = _state(python_repo, [command])
    assert _verify(python_repo, state, [command])["status"] == "passed"
    criterion = _command_criterion(state, command)
    evidence_ids = tuple(criterion.evidence_ids)

    _record(
        python_repo,
        state,
        tool_name="fs_read",
        arguments={"path": "app.py"},
        result=fs_read(root=python_repo, path="app.py"),
    )
    finalize_acceptance_contract(
        contract=state.acceptance_contract, root=python_repo, touched_paths=state.touched_repo_paths
    )

    assert state.verification_relevant_edit_generation == 0
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert tuple(criterion.evidence_ids) == evidence_ids
    assert state.covered_verification_commands == {command}


def test_required_text_artifact_edit_invalidates_its_explicit_check(tmp_path: Path) -> None:
    (tmp_path / "actual.txt").write_text("correct", encoding="utf-8")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nassert Path('actual.txt').read_text() == 'correct'\n",
        encoding="utf-8",
    )
    command = "python check.py"
    state = _state(tmp_path, [command], objective="Create actual.txt. Run `python check.py`.")
    assert _verify(tmp_path, state, [command])["status"] == "passed"
    criterion = _command_criterion(state, command)
    assert criterion.status == AcceptanceCriterionStatus.PASSED

    arguments = {"path": "actual.txt", "content": "wrong"}
    _record(
        tmp_path,
        state,
        tool_name="fs_write",
        arguments=arguments,
        result=fs_write(root=tmp_path, **arguments),
    )
    # Confirm the changed artifact really invalidates the check's result;
    # this independent run deliberately does not update the execution state.
    assert shell_run(root=tmp_path, cmd=command, runner=HostShellRunner())["exit_code"] == 1
    assert criterion.status != AcceptanceCriterionStatus.PASSED
    assert command not in state.covered_verification_commands
    assert state.verification_relevant_edit_generation == 1

    arguments = {"path": "actual.txt", "content": "correct"}
    _record(
        tmp_path,
        state,
        tool_name="fs_write",
        arguments=arguments,
        result=fs_write(root=tmp_path, **arguments),
    )
    assert _verify(tmp_path, state, [command])["status"] == "passed"
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert state.covered_verification_commands == {command}


@pytest.mark.parametrize("tool_name", ["shell_run", "verify_run"])
def test_check_that_mutates_declared_text_artifact_cannot_claim_coverage(
    tmp_path: Path, tool_name: str
) -> None:
    (tmp_path / "actual.txt").write_text("correct", encoding="utf-8")
    (tmp_path / "optional_check.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / "required_check.py").write_text(
        "from pathlib import Path\n"
        "target = Path('actual.txt')\n"
        "assert target.read_text() == 'correct'\n"
        "target.write_text('wrong')\n",
        encoding="utf-8",
    )
    command = "python required_check.py"
    state = _state(
        tmp_path, [command], objective="Create actual.txt. Run `python required_check.py`."
    )
    if tool_name == "shell_run":
        arguments = {"cmd": command}
        result = shell_run(root=tmp_path, cmd=command, runner=HostShellRunner())
        assert result["exit_code"] == 0
    else:
        arguments = {}
        # Place the mandatory mutating check after an unrelated successful
        # child so per-command authority cannot hide behind the batch result.
        run_result = run_task_verification(
            root=tmp_path,
            commands=["python optional_check.py", command],
            artifact_path=tmp_path / ".verification" / "result.txt",
            timeout_s=30,
        )
        result = verify_run_result_to_payload(root=tmp_path, result=run_result)
        assert result["status"] == "passed"
    assert (tmp_path / "actual.txt").read_text(encoding="utf-8") == "wrong"
    result["touched_repo_paths"] = ["actual.txt"]

    _record(tmp_path, state, tool_name=tool_name, arguments=arguments, result=result)

    assert state.verification_relevant_edit_generation == 1
    assert _command_criterion(state, command).status != AcceptanceCriterionStatus.PASSED
    assert command not in state.covered_verification_commands
    assert state.last_verification_passed is False
    if tool_name == "verify_run":
        assert result["command_results"][1]["verification_evidence_allowed"] is False
    else:
        assert result["verification_evidence_allowed"] is False


def test_batch_outcomes_and_metadata_remain_per_command(python_repo: Path) -> None:
    (python_repo / "tests" / "test_bad.py").write_text(
        "def test_regression():\n    assert False, 'deliberate failing check'\n", encoding="utf-8"
    )
    passing, failing = "pytest -q tests/test_ok.py", "pytest -q tests/test_bad.py"
    state = _state(python_repo, [passing, failing])

    result = _verify(python_repo, state, [passing, failing])

    assert result["status"] == "failed"
    assert _command_criterion(state, passing).status == AcceptanceCriterionStatus.PASSED
    assert _command_criterion(state, failing).status == AcceptanceCriterionStatus.FAILED
    assert state.covered_verification_commands == {passing}
    assert failing in state.failed_verification_commands()
    assert state.acceptance_contract is not None
    evidence = [item.as_payload() for item in state.acceptance_contract.evidence if item.command]
    assert [item["command"] for item in evidence] == [passing, failing]
    assert [item["exit_code"] for item in evidence] == [0, 1]
    assert all(item["task_id"] == "acceptance-integration:1" for item in evidence)
    assert all(item["generation"] == 0 for item in evidence)
    assert all(item["tool_name"] == "verify_run" for item in evidence)
    assert all(Path(item["cwd"]).resolve() == python_repo.resolve() for item in evidence)
    assert evidence[0]["executed_test_count"] == 1
    # The production payload bounds stdout to 400 characters. The failure's
    # final pytest summary is beyond that preview, so its count is unknown.
    assert result["command_results"][1]["output_truncated"] is True
    assert evidence[1]["executed_test_count"] is None
    assert all(item["result_id"] for item in evidence)
    assert len({item["result_id"] for item in evidence}) == 2


def test_host_glob_expansion_satisfies_requested_check_and_retains_executed_command(
    python_repo: Path,
) -> None:
    command = "pytest tests/test_*.py -q"
    state = _state(python_repo, [command])

    result = _verify(python_repo, state, [command])

    assert result["status"] == "passed"
    observed = result["command_results"][0]
    assert observed["command"] == command
    assert observed["effective_command"] == "pytest tests/test_ok.py -q"
    assert state.covered_verification_commands == {command}
    assert _command_criterion(state, command).status == AcceptanceCriterionStatus.PASSED
    assert state.acceptance_contract is not None
    execution_evidence = [item for item in state.acceptance_contract.evidence if item.command]
    assert len(execution_evidence) == 1
    assert execution_evidence[0].command == observed["effective_command"]
    assert execution_evidence[0].evidence_allowed is True


@pytest.mark.parametrize("tool_name", ["shell_run", "verify_run"])
def test_explicit_artifact_checker_is_independent_of_unrun_repo_checks(
    python_repo: Path, tool_name: str
) -> None:
    (python_repo / "actual.txt").write_text("correct", encoding="utf-8")
    (python_repo / "check.py").write_text(
        "from pathlib import Path\nassert Path('actual.txt').read_text() == 'correct'\n",
        encoding="utf-8",
    )
    command, repo_command = "python check.py", "pytest -q"
    objective = "Create actual.txt. Run `python check.py`."
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={repo_command},
        acceptance_contract=build_acceptance_contract(
            root=python_repo,
            instruction=objective,
            effective_verification_commands=[repo_command],
        ),
    )
    if tool_name == "shell_run":
        arguments = {"cmd": command}
        result = shell_run(root=python_repo, cmd=command, runner=HostShellRunner())
        assert result["exit_code"] == 0
    else:
        arguments = {}
        run_result = run_task_verification(
            root=python_repo,
            commands=[command],
            artifact_path=python_repo / ".verification" / "result.txt",
            timeout_s=30,
        )
        result = verify_run_result_to_payload(root=python_repo, result=run_result)
        assert result["status"] == "passed"

    _record_tool_effect(
        root=python_repo,
        state=state,
        tool_name=tool_name,
        arguments=arguments,
        status="ok",
        result=result,
        known_verification_commands=[repo_command],
        verification_authoritative=False,
    )

    assert _command_criterion(state, command).status == AcceptanceCriterionStatus.PASSED
    assert repo_command not in state.covered_verification_commands
    assert state.expected_verification_commands == {repo_command}
    assert state.acceptance_contract is not None
    repo_criteria = [
        item for item in state.acceptance_contract.criteria if repo_command in item.commands
    ]
    assert repo_criteria
    assert all(item.status != AcceptanceCriterionStatus.PASSED for item in repo_criteria)
    checker_evidence = next(
        item for item in state.acceptance_contract.evidence if item.command == command
    )
    assert checker_evidence.origin == EvidenceOrigin.USER_EXPLICIT
    assert checker_evidence.category == "TASK_ACCEPTANCE"


def test_zero_tests_cannot_borrow_success_from_another_batch_command(python_repo: Path) -> None:
    (python_repo / "empty_tests").mkdir()
    passing, empty = "pytest -q tests/test_ok.py", "pytest -q empty_tests"
    state = _state(python_repo, [passing, empty])

    result = _verify(python_repo, state, [passing, empty])

    assert result["command_results"][0]["exit_code"] == 0
    assert result["command_results"][1]["exit_code"] == 5
    assert _command_criterion(state, passing).status == AcceptanceCriterionStatus.PASSED
    assert _command_criterion(state, empty).status != AcceptanceCriterionStatus.PASSED
    assert empty not in state.covered_verification_commands
    assert state.acceptance_contract is not None
    empty_evidence = next(
        item for item in state.acceptance_contract.evidence if item.command == empty
    )
    assert empty_evidence.executed_test_count == 0
    assert empty_evidence.evidence_allowed is False


def test_successful_unittest_run_records_executed_tests(python_repo: Path) -> None:
    checks = python_repo / "checks"
    checks.mkdir()
    (checks / "test_unittest.py").write_text(
        "import unittest\n\n"
        "class AnswerTests(unittest.TestCase):\n"
        "    def test_answer(self):\n"
        "        self.assertEqual(1 + 1, 2)\n",
        encoding="utf-8",
    )
    command = "python -m unittest discover -s checks -p test_unittest.py"
    state = _state(python_repo, [command])

    result = _verify(python_repo, state, [command])

    assert result["status"] == "passed"
    assert "Ran 1 test" in result["command_results"][0]["output_preview"]
    assert _command_criterion(state, command).status == AcceptanceCriterionStatus.PASSED
    assert state.acceptance_contract is not None
    evidence = next(item for item in state.acceptance_contract.evidence if item.command == command)
    assert evidence.executed_test_count == 1
    assert evidence.evidence_allowed is True


def test_mutating_command_is_rejected_until_clean_rerun(python_repo: Path) -> None:
    command = "python check.py"
    (python_repo / "check.py").write_text(
        "from pathlib import Path\n"
        "target = Path('app.py')\n"
        "if 'revised' not in target.read_text():\n"
        "    target.write_text('# revised\\nanswer = 2\\n')\n"
        "assert 'answer = 2' in target.read_text()\n",
        encoding="utf-8",
    )
    state = _state(python_repo, [command])
    result = shell_run(root=python_repo, cmd=command, runner=HostShellRunner())
    assert result["exit_code"] == 0
    assert "revised" in (python_repo / "app.py").read_text(encoding="utf-8")
    # The turn dispatcher adds the material paths from its before/after snapshot.
    result["touched_repo_paths"] = ["app.py"]
    _record(python_repo, state, tool_name="shell_run", arguments={"cmd": command}, result=result)

    assert result["verification_evidence_allowed"] is False
    assert _command_criterion(state, command).status != AcceptanceCriterionStatus.PASSED
    assert command not in state.covered_verification_commands

    clean_result = shell_run(root=python_repo, cmd=command, runner=HostShellRunner())
    _record(
        python_repo, state, tool_name="shell_run", arguments={"cmd": command}, result=clean_result
    )
    assert _command_criterion(state, command).status == AcceptanceCriterionStatus.PASSED
    assert command in state.covered_verification_commands


def test_same_command_in_different_directory_does_not_cover_required_check(
    python_repo: Path,
) -> None:
    other = python_repo / "other"
    (other / "tests").mkdir(parents=True)
    (other / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (other / "tests" / "test_ok.py").write_text(
        "def test_unrelated():\n    assert True\n", encoding="utf-8"
    )
    command = "pytest -q tests/test_ok.py"
    state = _state(python_repo, [command])
    arguments = {"cmd": command, "cwd": "other"}
    result = shell_run(root=python_repo, runner=HostShellRunner(), **arguments)
    assert result["exit_code"] == 0

    _record(python_repo, state, tool_name="shell_run", arguments=arguments, result=result)

    assert _command_criterion(state, command).status != AcceptanceCriterionStatus.PASSED
    assert command not in state.covered_verification_commands
    assert result["verification_evidence_allowed"] is False
    assert _verify(python_repo, state, [command])["status"] == "passed"
    assert _command_criterion(state, command).status == AcceptanceCriterionStatus.PASSED


@pytest.mark.parametrize(
    "command",
    [
        "python --version",  # non-assertive: it reports, it cannot check anything
        "python -c \"print('hello')\"",  # unknown capability: prints, asserts nothing
    ],
)
def test_requested_informational_command_satisfies_its_own_criterion(
    tmp_path: Path, command: str
) -> None:
    objective = f"Run `{command}` and tell me exactly what it prints."
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={command},
        acceptance_contract=build_acceptance_contract(
            root=tmp_path,
            instruction=objective,
            effective_verification_commands=[command],
        ),
    )
    result = shell_run(root=tmp_path, cmd=command, runner=HostShellRunner())
    assert result["exit_code"] == 0

    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok",
        result=result,
        known_verification_commands=[command],
        verification_authoritative=False,
    )

    assert result["verification_evidence_allowed"] is False
    assert _command_criterion(state, command).status == AcceptanceCriterionStatus.PASSED
    assert state.acceptance_contract is not None
    assert state.acceptance_contract.problem_names() == []
    evidence = next(item for item in state.acceptance_contract.evidence if item.command == command)
    assert evidence.evidence_allowed is False


def test_requested_informational_command_elsewhere_does_not_satisfy_it(tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    command = "python --version"
    objective = f"Run `{command}` and tell me exactly what it prints."
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={command},
        acceptance_contract=build_acceptance_contract(
            root=tmp_path, instruction=objective, effective_verification_commands=[command]
        ),
    )
    arguments = {"cmd": command, "cwd": "other"}
    result = shell_run(root=tmp_path, runner=HostShellRunner(), **arguments)
    assert result["exit_code"] == 0

    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="shell_run",
        arguments=arguments,
        status="ok",
        result=result,
        known_verification_commands=[command],
        verification_authoritative=False,
    )

    assert _command_criterion(state, command).status != AcceptanceCriterionStatus.PASSED


class _RecordingClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], **_: Any) -> LLMResponse:
        self.requests.append(copy.deepcopy(messages))
        return LLMResponse(content="The requested file is present.", tool_calls=[], raw={})


def _latest_contract(session: Any) -> dict[str, Any]:
    contracts = [
        event["payload"]
        for event in session.store.events_snapshot()
        if event.get("type") == "acceptance_contract"
    ]
    assert contracts, "the actual turn must emit its acceptance contract"
    return copy.deepcopy(contracts[-1])


def _required_outputs(payload: dict[str, Any]) -> set[str]:
    return {
        path
        for item in payload["criteria"]
        if item["kind"] == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH.value
        and item["required_for_finalization"]
        for path in item["paths"]
    }


def test_current_request_replaces_previous_turn_acceptance(tmp_path):
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", skills_enabled=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        one_shot_execution=True,
    )
    session.client = _RecordingClient()
    try:
        for instruction, paths in (
            ("Create alpha.txt.", {"alpha.txt"}),
            ("Also create amendment.txt.", {"amendment.txt"}),
            ("Create beta.txt.", {"beta.txt"}),
        ):
            assert session.run_turn(instruction) == 0
            contract = _latest_contract(session)
            assert set(contract["allowed_output_paths"]) == paths
    finally:
        session.close()


def test_new_task_selects_its_explicit_check_instead_of_previous_task_check(tmp_path: Path) -> None:
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("assert True\n", encoding="utf-8")
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", skills_enabled=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=tmp_path / ".sessions",
    )
    session.client = _RecordingClient()
    try:
        assert session.run_turn("Run `python one.py`.") == 0
        assert session.effective_verification_commands == ["python one.py"]

        assert session.run_turn("Run `python two.py`.") == 0

        assert session.effective_verification_commands == ["python two.py"]
        contract = _latest_contract(session)
        assert {command for item in contract["criteria"] for command in item["commands"]} == {
            "python two.py"
        }
        assert session.run_turn("continue") == 0
        assert session.effective_verification_commands == []
    finally:
        session.close()
