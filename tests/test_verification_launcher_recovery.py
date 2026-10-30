"""Real missing executables are distinct from executed checks returning status 127."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.config import AppConfig
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.verify_gate import (
    _run_verify_command_once,
    compact_verification_payload,
    run_task_verification,
    verify_run_result_to_payload,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Host POSIX launcher observation")


def _run(root: Path, state: TurnExecutionState, command: str, known: list[str]):
    cfg = AppConfig(model="unused")
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    result = run_task_verification(
        root=root, commands=[command], artifact_path=root / "evidence.log", cfg=cfg
    )
    payload = verify_run_result_to_payload(root=root, result=result)
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="verify_run",
        arguments={"commands": [command]},
        status="ok",
        result=payload,
        known_verification_commands=known,
        scope_environment_known=True,
    )
    return payload


@pytest.mark.parametrize("required", [False, True])
def test_real_missing_launcher_recovers_only_when_it_was_optional(tmp_path, required):
    (tmp_path / "test_ok.py").write_text(
        "import unittest\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n"
    )
    unavailable = shlex.quote(str(tmp_path / "missing" / "python")) + " -m unittest test_ok -v"
    available = shlex.quote(sys.executable) + " -m unittest test_ok -v"
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={unavailable} if required else set(),
    )
    first = _run(tmp_path, state, unavailable, [unavailable])
    item = first["command_results"][0]
    assert item["host_launcher_unavailable"] is True
    assert item["real_execution"] is False
    assert (
        compact_verification_payload(first)["command_results"][0]["host_launcher_unavailable"]
        is True
    )
    assert state.last_verification_passed is False
    _run(tmp_path, state, available, [unavailable])
    assert state.last_verification_passed is (not required)
    assert state.covered_verification_commands == set()  # No invented command equivalence.
    assert bool(state.failed_verification_commands()) is required
    assert any(e["observed_exit_code"] == 127 for e in state.rejected_verification_evidence)


def test_real_program_returning_127_is_never_a_missing_launcher(tmp_path):
    executable = tmp_path / "python"
    executable.write_text("#!/bin/sh\necho 'python: command not found' >&2\nexit 127\n")
    executable.chmod(0o700)
    result = _run_verify_command_once(
        command=shlex.quote(str(executable)) + " -m unittest",
        root=tmp_path,
        runner=HostShellRunner(),
        runner_build_error=None,
        timeout_s=5,
    )
    assert result.exit_code == 127
    assert not result.host_launcher_unavailable


def test_assignments_and_wrappers_cannot_use_host_path_as_proof(tmp_path):
    for command in [
        "PATH=/not-a-real-path python -m unittest",
        "sh -c 'python -m unittest'",
        "command python -m unittest",
    ]:
        result = _run_verify_command_once(
            command=command,
            root=tmp_path,
            runner=HostShellRunner(),
            runner_build_error=None,
            timeout_s=5,
        )
        assert not result.host_launcher_unavailable


@pytest.mark.parametrize("required", [False, True])
def test_shell_missing_launcher_recovers_through_real_verify_run(tmp_path, required):
    from alysis_code.sandbox_runner import LazyShellRunner
    from alysis_code.tools.shell import shell_run

    (tmp_path / "test_ok.py").write_text(
        "import unittest\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n"
    )
    unavailable = shlex.quote(str(tmp_path / "missing" / "python")) + " -m unittest test_ok -v"
    available = shlex.quote(sys.executable) + " -m unittest test_ok -v"
    state = TurnExecutionState(
        execution_requested=True,
        expected_verification_commands={unavailable} if required else set(),
    )
    result = shell_run(root=tmp_path, cmd=unavailable, runner=LazyShellRunner(HostShellRunner))
    assert result["host_launcher_unavailable"] is True
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": unavailable},
        status="ok",
        result=result,
        known_verification_commands=[unavailable],
        scope_environment_known=True,
    )
    assert state.last_verification_passed is False
    _run(tmp_path, state, available, [unavailable])
    assert state.last_verification_passed is (not required)
    assert bool(state.failed_verification_commands()) is required
