from __future__ import annotations

from dataclasses import replace

import pytest

from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.config import AppConfig
from alysis_code.sandbox_runner import (
    BwrapShellRunner,
    DisabledShellRunner,
    DockerShellRunner,
    HostShellRunner,
    LazyShellRunner,
    SeatbeltShellRunner,
    execution_scope_fingerprint,
    with_closed_stdin,
)
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.windows_mxc import WindowsMxcRunner
from alysis_code.windows_sandbox import WindowsNativeRunner

RUNNERS = [
    HostShellRunner(),
    BwrapShellRunner(),
    DockerShellRunner(),
    SeatbeltShellRunner(),
    WindowsNativeRunner(ShellSandboxSettings()),
    WindowsMxcRunner(ShellSandboxSettings()),
]


@pytest.mark.parametrize("runner", RUNNERS)
def test_known_scope_survives_verifier_stdin_change_and_hides_environment(runner, monkeypatch):
    monkeypatch.setenv("SCOPE_TEST_SECRET", "not-for-tool-output")
    scope = execution_scope_fingerprint(runner)
    assert scope is not None and len(scope) == 64
    assert "not-for-tool-output" not in scope
    assert execution_scope_fingerprint(with_closed_stdin(runner)) == scope
    monkeypatch.setenv("SCOPE_TEST_SECRET", "different-value")
    assert execution_scope_fingerprint(runner) != scope


def test_different_backend_and_policy_are_not_comparable():
    scopes = [execution_scope_fingerprint(runner) for runner in RUNNERS]
    assert len(set(scopes)) == len(scopes)
    native = RUNNERS[-1]
    different = replace(native, settings=replace(native.settings, network="on"))
    assert execution_scope_fingerprint(different) != scopes[-1]
    assert execution_scope_fingerprint(DisabledShellRunner()) is None
    assert execution_scope_fingerprint(object()) is None


def test_scope_observation_does_not_initialize_a_lazy_runner():
    lazy = LazyShellRunner(lambda: pytest.fail("Scope observation must not initialize a runner"))
    assert execution_scope_fingerprint(lazy) is None
    lazy._runner = RUNNERS[-1]
    assert execution_scope_fingerprint(lazy) == execution_scope_fingerprint(RUNNERS[-1])


@pytest.mark.parametrize(
    "difference",
    [None, "command", "cwd", "environment", "policy", "backend", "unknown", "not_executed"],
)
@pytest.mark.parametrize(
    "first_tool,last_tool", [("shell_run", "verify_run"), ("verify_run", "shell_run")]
)
def test_sandbox_failure_resolves_only_after_its_own_real_rerun(
    tmp_path, monkeypatch, difference, first_tool, last_tool
):
    (tmp_path / "check.py").write_text("assert True\n")
    command = "python check.py"
    state = TurnExecutionState(execution_requested=True)
    runner = RUNNERS[-1]
    scope = execution_scope_fingerprint(runner)

    def observe(tool, passed, *, cmd=command, cwd=".", execution_scope=scope, real=True):
        item = {
            "cmd": cmd,
            "effective_cmd": cmd,
            "command": cmd,
            "effective_command": cmd,
            "cwd": cwd,
            "exit_code": 0 if passed else 1,
            "stdout": "check passed" if passed else "AssertionError: check failed",
            "output_preview": "check passed" if passed else "AssertionError: check failed",
            "real_execution": real,
            "ok": passed,
            "verification_execution_scope": execution_scope,
        }
        if not real:
            item["stdout"] = ""
            item["output_preview"] = ""
            item["exit_code"] = None
        result = (
            item
            if tool == "shell_run"
            else {"commands": [cmd], "command_results": [item], "all_passed": passed}
        )
        _record_tool_effect(
            root=tmp_path,
            state=state,
            tool_name=tool,
            arguments={"cmd": cmd, "cwd": cwd},
            status="ok",
            result=result,
            known_verification_commands=["python -m unittest discover"],
            scope_environment_known=False,
        )

    observe(first_tool, False)
    assert state.last_verification_passed is False
    assert len(state.observed_verification_failures) == 1
    kwargs = {}
    if difference == "command":
        kwargs["cmd"] = command + " --other"
    if difference == "cwd":
        (tmp_path / "elsewhere").mkdir()
        kwargs["cwd"] = "elsewhere"
    if difference == "environment":
        monkeypatch.setenv("SCOPE_TEST_VARIANT", "different")
        kwargs["execution_scope"] = execution_scope_fingerprint(runner)
    if difference == "policy":
        kwargs["execution_scope"] = execution_scope_fingerprint(
            replace(runner, settings=replace(runner.settings, network="on"))
        )
    if difference == "backend":
        kwargs["execution_scope"] = execution_scope_fingerprint(HostShellRunner())
    if difference == "unknown":
        kwargs["execution_scope"] = None
    if difference == "not_executed":
        kwargs["real"] = False
    observe(last_tool, True, **kwargs)
    if difference is None:
        assert not state.observed_verification_failures
        assert state.last_verification_passed is True
    else:
        assert len(state.observed_verification_failures) == 1
        assert state.last_verification_passed is False


def test_real_host_tools_preserve_the_same_scope(tmp_path, monkeypatch):
    from alysis_code.tools.shell import shell_run
    from alysis_code.verify_gate import run_task_verification, verify_run_result_to_payload

    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "off")
    command = "echo scope-check"
    shell = shell_run(root=tmp_path, cmd=command, runner=HostShellRunner())
    verified = run_task_verification(
        root=tmp_path,
        commands=[command],
        artifact_path=tmp_path / "check-output.txt",
        cfg=AppConfig(model="unused"),
    )
    receipt = verify_run_result_to_payload(root=tmp_path, result=verified)["command_results"][0]
    assert receipt["verification_execution_scope"] == shell["verification_execution_scope"]
