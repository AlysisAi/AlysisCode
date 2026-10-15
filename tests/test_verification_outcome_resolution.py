from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.agent.verification import (
    TurnExecutionState,
    _completion_gate_problems,
    _record_tool_effect,
    scope_environment_is_host,
)
from alysis_code.config import AppConfig
from alysis_code.sandbox_runner import DisabledShellRunner, HostShellRunner
from alysis_code.tools.shell import shell_run
from alysis_code.verify_gate import run_task_verification, verify_run_result_to_payload

_PYTHON = (
    subprocess.list2cmdline([Path(sys.executable).as_posix()])
    if os.name == "nt"
    else shlex.quote(sys.executable)
)
_CONTRACT = f"{_PYTHON} -m unittest discover"
_CHECK = f"{_PYTHON} check_api.py"


def _fixture(root: Path, *, correct: bool = False) -> None:
    root.mkdir(exist_ok=True)
    (root / "api.py").write_text(
        "def combine(a, b):\n    return sum((a, b))\n"
        if correct
        else "def combine(a, b):\n    return a - b\n"
    )
    (root / "check_api.py").write_text(
        "import sys\nfrom api import combine\n"
        "if sys.argv[1:] != ['skip']:\n    assert combine(2, 3) == 5, 'sum failed'\n"
    )
    (root / "other_check.py").write_text(
        "from api import combine\nassert combine(4, 7) == 11, 'other sum failed'\n"
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests/__init__.py").write_text("")
    (root / "tests/test_existing.py").write_text(
        "import unittest\nclass Existing(unittest.TestCase):\n"
        "    def test_existing(self):\n        self.assertEqual(1 + 1, 2)\n"
    )


def _state() -> TurnExecutionState:
    return TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={_CONTRACT},
        touched_repo_paths={"api.py", "check_api.py", "other_check.py"},
    )


def _observe(root, state, tool, command=_CHECK, *, environment_known=True, known_commands=None):
    before = {path.name: path.read_bytes() for path in root.glob("*.py")}
    if tool == "shell_run":
        arguments = {"cmd": command, "cwd": "."}
        result = shell_run(root=root, cmd=command, runner=HostShellRunner(), timeout_s=20)
    else:
        arguments = {"commands": [command]}
        executed = run_task_verification(
            root=root,
            commands=[command],
            artifact_path=root / "verification-output.txt",
            cfg=AppConfig(model="unused", extra_fields={"verify_sandbox": {"mode": "off"}}),
            timeout_s=20,
        )
        result = verify_run_result_to_payload(root=root, result=executed)
    changed = [
        path.name for path in root.glob("*.py") if before.get(path.name) != path.read_bytes()
    ]
    if changed:
        result["touched_repo_paths"] = changed
    _record_tool_effect(
        root=root,
        state=state,
        tool_name=tool,
        arguments=arguments,
        status="ok",
        result=result,
        known_verification_commands=[_CONTRACT] if known_commands is None else known_commands,
        scope_environment_known=environment_known,
    )
    return result


def _problems(state):
    return _completion_gate_problems(
        state=state,
        final_text="Implemented and checked the API.",
        blocked=False,
        verification_expected=True,
        require_material_edit_evidence=False,
        evidence_v2=True,
        turn_intent="execute",
    )


@pytest.fixture(autouse=True)
def _stable_native_environment(monkeypatch):
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_same_executed_supplemental_check_resolves_failure_without_covering_contract(
    tmp_path, tool
):
    _fixture(tmp_path)
    original_checker = (tmp_path / "check_api.py").read_bytes()
    state = _state()
    _observe(tmp_path, state, tool)
    assert state.last_verification_passed is False
    assert "sum failed" in state.last_verification_failure_snippet
    _fixture(tmp_path, correct=True)
    state.note_verification_relevant_edit()
    result = _observe(tmp_path, state, tool)
    assert (tmp_path / "check_api.py").read_bytes() == original_checker
    assert result["verification_evidence_supplemental_only"] is True
    assert result["verification_evidence_allowed"] is False
    assert state.last_verification_passed is True
    assert state.last_verification_failure_snippet == ""
    assert not state.observed_verification_failures
    assert not state.covered_verification_commands
    assert state.missing_verification_commands() == {_CONTRACT}
    assert _problems(state) == ["verification_incomplete"]


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_resolving_one_failure_preserves_the_other_and_its_diagnostic(tmp_path, tool):
    _fixture(tmp_path)
    state = _state()
    _observe(tmp_path, state, tool)
    other = _CHECK.replace("check_api.py", "other_check.py")
    _observe(tmp_path, state, tool, other)
    assert len(state.observed_verification_failures) == 2
    _fixture(tmp_path, correct=True)
    state.note_verification_relevant_edit()
    _observe(tmp_path, state, tool)
    assert state.last_verification_passed is False
    assert len(state.observed_verification_failures) == 1
    assert "other sum failed" in state.last_verification_failure_snippet
    assert "verification_failed" in _problems(state)
    _observe(tmp_path, state, tool, other)
    assert not state.observed_verification_failures
    assert _problems(state) == ["verification_incomplete"]


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
@pytest.mark.parametrize(
    "difference",
    ["arguments", "cwd", "cd_symlink", "environment", "unknown_failure", "unknown_pass"],
)
def test_passing_different_or_unknown_context_does_not_resolve_failure(
    tmp_path, monkeypatch, request, tool, difference
):
    _fixture(tmp_path)
    state = _state()
    command = _CHECK
    if difference == "cd_symlink":
        request.getfixturevalue("require_symlinks")
        _fixture(tmp_path / "first")
        _fixture(tmp_path / "second", correct=True)
        (tmp_path / "working").symlink_to(tmp_path / "first", target_is_directory=True)
        command = "cd working && " + _CHECK
    _observe(tmp_path, state, tool, command, environment_known=difference != "unknown_failure")
    before = dict(state.observed_verification_failures)
    root = tmp_path
    if difference == "cwd":
        root = tmp_path / "other"
        _fixture(root, correct=True)
    elif difference == "arguments":
        command += " skip"
    elif difference == "cd_symlink":
        (tmp_path / "working").unlink()
        (tmp_path / "working").symlink_to(tmp_path / "second", target_is_directory=True)
    else:
        _fixture(root, correct=True)
        if difference == "environment":
            monkeypatch.setenv("OUTCOME_CHECK_CONTEXT", "different")
    _observe(root, state, tool, command, environment_known=difference != "unknown_pass")
    assert state.last_verification_passed is False
    assert state.observed_verification_failures == before
    assert "verification_failed" in _problems(state)


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_passing_required_suite_does_not_hide_unresolved_supplemental_failure(tmp_path, tool):
    _fixture(tmp_path)
    state = _state()
    _observe(tmp_path, state, tool)
    _observe(tmp_path, state, tool, _CONTRACT)
    assert state.covered_verification_commands == {_CONTRACT}
    assert state.last_verification_passed is False
    assert "sum failed" in state.last_verification_failure_snippet
    assert "verification_failed" in _problems(state)


@pytest.mark.parametrize("mode, expected", [("strict", False), ("off", True), ("invalid", False)])
@pytest.mark.parametrize("runner", [HostShellRunner(), DisabledShellRunner()])
def test_verifier_environment_uses_its_own_runner_selection(monkeypatch, mode, expected, runner):
    monkeypatch.delenv("ALYSIS_VERIFY_SANDBOX_MODE", raising=False)
    cfg = AppConfig(model="unused", extra_fields={"verify_sandbox": {"mode": mode}})
    assert scope_environment_is_host(runner, verification_config=cfg) is expected
    assert scope_environment_is_host(runner) is isinstance(runner, HostShellRunner)


@pytest.mark.parametrize("kind", ["skipped", "not_run", "missing_output"])
def test_unobserved_result_neither_resolves_failure_nor_adds_permanent_failure(tmp_path, kind):
    _fixture(tmp_path)
    state = _state()
    _observe(tmp_path, state, "verify_run")
    failed = dict(state.observed_verification_failures)
    item = {
        "command": _CHECK,
        "effective_command": _CHECK,
        "exit_code": 0 if kind == "missing_output" else None,
        "real_execution": kind == "missing_output",
        "ok": kind == "missing_output",
        "status": "passed" if kind == "missing_output" else kind,
    }
    result = {
        "commands": [_CHECK],
        "command_results": [item],
        "all_passed": kind == "missing_output",
    }
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="verify_run",
        arguments={"commands": [_CHECK]},
        status="ok",
        result=result,
        known_verification_commands=[_CONTRACT],
    )
    assert state.observed_verification_failures == failed
    _fixture(tmp_path, correct=True)
    _observe(tmp_path, state, "verify_run")
    assert state.last_verification_passed is True
    assert not state.observed_verification_failures


def test_required_command_reconciliation_remains_available_with_default_strict_scope(
    tmp_path, monkeypatch
):
    _fixture(tmp_path)
    test_file = tmp_path / "tests/test_existing.py"
    passing = test_file.read_text()
    test_file.write_text(passing.replace("1 + 1, 2", "1 + 1, 3"))
    state = _state()
    monkeypatch.delenv("ALYSIS_VERIFY_SANDBOX_MODE", raising=False)
    default_config = AppConfig(model="unused")
    environment_known = scope_environment_is_host(
        HostShellRunner(), verification_config=default_config
    )
    assert environment_known is False
    # Obtain real process outcomes through the host helper, then explicitly mark
    # scope unknown as the default strict verifier does. No sandbox is faked.
    _observe(tmp_path, state, "verify_run", _CONTRACT, environment_known=environment_known)
    assert state.last_verification_passed is False
    assert state.failed_verification_commands() == {_CONTRACT}
    assert not state.observed_verification_failures
    test_file.write_text(passing)
    _observe(tmp_path, state, "verify_run", _CONTRACT, environment_known=environment_known)
    assert state.last_verification_passed is True
    assert state.failed_verification_commands() == set()
    assert _problems(state) == []


def test_test_failure_attribution_cannot_hide_an_outstanding_non_test_failure(tmp_path):
    _fixture(tmp_path)
    test_file = tmp_path / "tests/test_existing.py"
    test_file.write_text(test_file.read_text().replace("1 + 1, 2", "1 + 1, 3"))
    state = _state()
    narrow = _CONTRACT + " -s tests -v"
    _observe(tmp_path, state, "shell_run", narrow)
    _observe(tmp_path, state, "shell_run")
    state.note_verification_relevant_edit()
    _observe(tmp_path, state, "shell_run", narrow)
    assert state.last_verification_attempt_was_test_run is True
    assert len(state.observed_verification_failures) == 2
    assert state.compute_regression_diff(enabled=True).pre_existing
    problems = _completion_gate_problems(
        state=state,
        final_text="The suite failure predates this edit.",
        blocked=False,
        verification_expected=True,
        require_material_edit_evidence=False,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
    )
    assert "verification_failed" in problems


@pytest.mark.parametrize("older_failure", [False, True])
def test_attributed_test_failure_cannot_hide_an_older_unattributed_observation(
    tmp_path, older_failure
):
    _fixture(tmp_path)
    for name in ["test_existing.py", "test_other.py"]:
        (tmp_path / "tests" / name).write_text(
            "import unittest\nclass Existing(unittest.TestCase):\n"
            "    def test_existing(self):\n        self.assertEqual(1 + 1, 3)\n"
        )
    state = _state()
    prefix = f"{_PYTHON} -m unittest "
    if older_failure:
        _observe(tmp_path, state, "shell_run", prefix + "tests.test_existing -v")
    current = prefix + "tests.test_other -v"
    _observe(tmp_path, state, "shell_run", current)
    state.note_verification_relevant_edit()
    _observe(tmp_path, state, "shell_run", current)
    assert state.compute_regression_diff(enabled=True).pre_existing
    problems = _completion_gate_problems(
        state=state,
        final_text="The test failure predates this edit.",
        blocked=False,
        verification_expected=True,
        require_material_edit_evidence=False,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
    )
    assert ("verification_failed" in problems) is older_failure


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_zero_exit_that_changes_verified_source_does_not_retire_failure(tmp_path, tool):
    _fixture(tmp_path)
    state = _state()
    _observe(tmp_path, state, tool)
    failed = dict(state.observed_verification_failures)
    (tmp_path / "check_api.py").write_text(
        "from pathlib import Path\n"
        "Path('api.py').write_text('def combine(a, b): return a + b\\n')\n"
    )
    result = _observe(tmp_path, state, tool)
    assert result["touched_repo_paths"] == ["api.py"]
    assert state.last_verification_passed is False
    assert state.observed_verification_failures == failed


def test_unknown_targeted_failure_recovers_only_after_genuine_required_execution(
    tmp_path, monkeypatch
):
    _fixture(tmp_path)
    (tmp_path / "tests/test_api.py").write_text(
        "import unittest\nfrom api import combine\nclass Api(unittest.TestCase):\n"
        "    def test_sum(self):\n        self.assertEqual(combine(2, 3), 5)\n"
    )
    monkeypatch.delenv("ALYSIS_VERIFY_SANDBOX_MODE", raising=False)
    unknown_scope = scope_environment_is_host(
        HostShellRunner(), verification_config=AppConfig(model="unused")
    )
    assert unknown_scope is False
    targeted = f"{_PYTHON} -m unittest tests.test_api -v"
    state = _state()
    first = _observe(tmp_path, state, "verify_run", targeted, environment_known=unknown_scope)
    assert first["verification_evidence_supplemental_only"] is True
    assert state.last_verification_passed is False
    _fixture(tmp_path, correct=True)
    state.note_verification_relevant_edit()
    second = _observe(tmp_path, state, "verify_run", targeted, environment_known=unknown_scope)
    assert second["all_passed"] is True
    assert state.last_verification_passed is False
    assert state.observed_verification_failures
    assert state.missing_verification_commands() == {_CONTRACT}
    third = _observe(tmp_path, state, "verify_run", _CONTRACT, environment_known=unknown_scope)
    assert third["verification_evidence_allowed"] is True
    assert not state.observed_verification_failures
    assert state.last_verification_passed is True
    assert _problems(state) == []


@pytest.mark.parametrize(
    "incomplete", ["failed", "skipped", "empty", "partial", "missing_output", "failed_boundary"]
)
def test_incomplete_required_result_does_not_release_unknown_failure(tmp_path, incomplete):
    _fixture(tmp_path)
    state = _state()
    _observe(tmp_path, state, "verify_run", environment_known=False)
    previous = dict(state.observed_verification_failures)
    row = {
        "command": _CONTRACT,
        "effective_command": _CONTRACT,
        "exit_code": 0,
        "ok": True,
        "real_execution": True,
        "output_preview": "Ran 1 test in 0.001s\n\nOK\n",
    }
    result = {"commands": [_CONTRACT], "command_results": [row], "all_passed": True}
    if incomplete == "failed":
        row.update(
            exit_code=1, ok=False, output_preview="Ran 1 test in 0.001s\nFAILED (failures=1)\n"
        )
        result["all_passed"] = False
    elif incomplete == "skipped":
        row.update(
            exit_code=None,
            real_execution=False,
            status="skipped",
            non_execution_reason="no_tests_collected",
        )
    elif incomplete == "empty":
        result["command_results"] = []
    elif incomplete == "partial":
        result["commands"] = [_CONTRACT, _CHECK]
    elif incomplete == "missing_output":
        row.pop("output_preview")
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="verify_run",
        arguments={"commands": result["commands"]},
        status="failed" if incomplete == "failed_boundary" else "ok",
        result=result,
        known_verification_commands=[_CONTRACT],
        scope_environment_known=False,
    )
    assert state.observed_verification_failures == previous
    assert state.last_verification_passed is False


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_no_contract_unknown_context_keeps_legacy_accepted_pass_recovery(tmp_path, tool):
    _fixture(tmp_path)
    state = _state()
    state.expected_verification_commands.clear()
    _observe(tmp_path, state, tool, environment_known=False, known_commands=[])
    assert state.last_verification_passed is False
    assert state.observed_verification_failures
    _fixture(tmp_path, correct=True)
    result = _observe(tmp_path, state, tool, environment_known=False, known_commands=[])
    assert result["verification_evidence_allowed"] is True
    assert not state.observed_verification_failures
    assert state.last_verification_passed is True
    assert not state.covered_verification_commands
    assert _problems(state) == []
