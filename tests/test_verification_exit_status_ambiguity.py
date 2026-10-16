"""126/127 and launcher-like metadata are not proof that a verifier never ran."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_shell_verification_recovery import _shell, _state
from test_verification_batch_recovery import _check, _record

from alysis_code.agent.verification import _completion_gate_problems
from alysis_code.verify_gate import assess_verification_command_execution

_FAILURE = (
    "================ short test summary info ================\n"
    "FAILED tests/test_app.py::test_behavior - AssertionError: behavior failed\n"
    "================ 1 failed in 0.01s ================\n"
)


def _guarded_problems(state) -> list[str]:
    return _completion_gate_problems(
        state=state,
        final_text="Implemented and checked the result.",
        blocked=False,
        verification_expected=True,
        require_material_edit_evidence=False,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
        blast_radius_enabled=True,
    )


def _observe(root: Path, state, tool: str, command: str, code: int, output: str) -> dict[str, Any]:
    if tool == "shell_run":
        return _shell(root, state, command, required=[command], exit_code=code, output=output)
    assessment = assess_verification_command_execution(
        command=command, exit_code=code, output=output
    )
    item = _check(command, passed=code == 0, launched=assessment.real_execution is True)
    item.update(
        exit_code=code,
        output_preview=output,
        real_execution=assessment.real_execution,
        non_execution_reason=assessment.non_execution_reason,
    )
    _, result = _record(root, [item], required=[command], state=state)
    return result


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
@pytest.mark.parametrize("exit_code", [126, 127])
@pytest.mark.parametrize("output", ["", "Verifier executed and a dependency failed.\n", _FAILURE])
def test_ambiguous_exit_cannot_hide_failure_even_in_agent_authored_test(
    tmp_path: Path,
    tool: str,
    exit_code: int,
    output: str,
):
    state = _state()
    command = "pytest -q"
    _observe(tmp_path, state, tool, command, 0, "1 passed\n")
    accepted = list(state.accepted_verification_evidence)
    state.agent_created_paths.add("tests/test_app.py")
    result = _observe(tmp_path, state, tool, command, exit_code, output)
    assert state.last_verification_passed is False
    assert state.failed_verification_commands() == {command}
    assert "verification_failed" in _guarded_problems(state)
    assert state.accepted_verification_evidence == accepted
    assert state.rejected_verification_evidence[-1]["observed_exit_code"] == exit_code
    assert "verification_recovered_launch_failures" not in result
    if output == _FAILURE:
        assert state.latest_regression_diff["agent_authored"]


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_a_new_real_success_can_resolve_a_previous_ambiguous_failure(tmp_path: Path, tool: str):
    state = _state()
    command = "pytest -q"
    _observe(tmp_path, state, tool, command, 0, "1 passed\n")
    _observe(tmp_path, state, tool, command, 127, "")
    assert "verification_failed" in _guarded_problems(state)
    _observe(tmp_path, state, tool, command, 0, "1 passed\n")
    assert state.last_verification_passed is True
    assert state.failed_verification_commands() == set()
    assert _guarded_problems(state) == []
    assert state.rejected_verification_evidence[-1]["observed_exit_code"] == 127


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_an_actual_test_runner_can_report_real_test_failure_and_exit127(
    tmp_path: Path,
    tool: str,
):
    """Run a genuine pytest hook that changes its real failing exit status."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text(
        'import os\ndef test_behavior():\n    assert os.environ.get("ALYSIS_TEST_FAIL") != "1"\n'
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    if exitstatus != 0:\n        session.exitstatus = 127\n"
    )
    argv = [Path(sys.executable).as_posix(), "-m", "pytest", "-q"]
    command = shlex.join(argv)
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    env.pop("ALYSIS_TEST_FAIL", None)
    passed = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert passed.returncode == 0
    state = _state()
    _observe(tmp_path, state, tool, command, passed.returncode, passed.stdout + passed.stderr)
    assert state.last_verification_passed is True
    state.agent_created_paths.add("tests/test_app.py")
    failed = subprocess.run(
        argv,
        cwd=tmp_path,
        env={**env, "ALYSIS_TEST_FAIL": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = failed.stdout + failed.stderr
    assert failed.returncode == 127
    assert "FAILED tests/test_app.py::test_behavior" in output
    assert "1 failed" in output
    _observe(tmp_path, state, tool, command, failed.returncode, output)
    assert state.last_verification_passed is False
    assert "verification_failed" in _guarded_problems(state)
    assert state.latest_regression_diff["agent_authored"], state.latest_regression_diff
