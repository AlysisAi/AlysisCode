"""Windows verifier paths retain their spelling and their actual exit status."""

from __future__ import annotations

import json

import pytest

from alysis_code.agent.verification_commands import _matching_effective_verification_commands
from alysis_code.config import split_verify_command_parts
from alysis_code.verification_command_analysis import analyze_verification_command
from alysis_code.verification_contract import build_verification_command_specs
from alysis_code.verify_gate import (
    _expand_verification_command_globs,
    assess_verification_command_execution,
)


@pytest.mark.parametrize(
    ("command", "argv", "family"),
    [
        (
            r".\.runtime\node.exe --test invoice.test.js",
            (r".\.runtime\node.exe", "--test", "invoice.test.js"),
            "node:test",
        ),
        (
            r'& "C:\Program Files\nodejs\node.exe" --test "tests\invoice test.js"',
            (r"C:\Program Files\nodejs\node.exe", "--test", r"tests\invoice test.js"),
            "node:test",
        ),
        (
            r"& '.\local runtime\node.exe' --test invoice.test.js",
            (r".\local runtime\node.exe", "--test", "invoice.test.js"),
            "node:test",
        ),
        (r"C:\tools\node.exe --test", (r"C:\tools\node.exe", "--test"), "node:test"),
        (r".\tools\npm.cmd test", (r".\tools\npm.cmd", "test"), "npm:test"),
    ],
)
def test_windows_verifier_is_a_single_assertive_command(command, argv, family):
    assert tuple(split_verify_command_parts(command)) == argv
    analysis = analyze_verification_command(command)
    assert analysis.is_valid_verifier
    assert analysis.parts == argv
    assert analysis.command_family == family
    assert analysis.shell_control_flow == "none"
    (spec,) = build_verification_command_specs([command], source="task_prompt")
    assert spec.validation_status == "VALID"
    assert spec.execution_mode == "ARGV"
    assert spec.argv == argv
    result = assess_verification_command_execution(
        command=command, exit_code=0, output="ℹ tests 7\nℹ pass 7\nℹ fail 0\n"
    )
    assert result.real_execution is True
    failure = assess_verification_command_execution(
        command=command, exit_code=1, output="ℹ tests 7\nℹ pass 6\nℹ fail 1\n"
    )
    assert failure.real_execution is not True


@pytest.mark.parametrize(
    "suffix",
    [
        "; exit 0",
        " || echo passed",
        " && echo passed",
        " & echo passed",
        " | Out-Null",
        "> result.txt",
        " $(exit 0)",
        " `; exit 0",
        "\nexit 0",
        ' --test-name-pattern "$FILTER"',
    ],
)
def test_windows_call_operator_does_not_admit_other_shell_expressions(suffix):
    command = r"& '.\.runtime\node.exe' --test" + suffix
    assert not analyze_verification_command(command).is_valid_verifier
    (spec,) = build_verification_command_specs([command], source="task_prompt")
    assert spec.validation_status == "INVALID"


@pytest.mark.parametrize(
    "command",
    [
        r"& '.\.runtime\node.exe' --help",
        r"& '.\.runtime\node.exe' --test --version",
        r"& $runner --test",
        r"& { node --test; exit 0 }",
    ],
)
def test_non_test_or_dynamic_commands_remain_invalid(command):
    assert not analyze_verification_command(command).is_valid_verifier


@pytest.mark.parametrize("output", ["# tests 0\n# pass 0\n", "ℹ tests 0\nℹ pass 0\n"])
def test_zero_node_tests_are_not_successful_verification(output):
    result = assess_verification_command_execution(
        command=r".\.runtime\node.exe --test", exit_code=0, output=output
    )
    assert result.real_execution is False
    assert result.non_execution_reason == "node_test_zero_tests"


def test_windows_quote_styles_cover_only_the_same_command():
    required = r".\.runtime\node.exe --test invoice.test.js"
    observed = r"& '.\.runtime\node.exe' --test 'invoice.test.js'"
    assert _matching_effective_verification_commands(
        observed_command=observed, effective_verification_commands=[required]
    ) == {required}
    for other in [
        r".\other\node.exe --test invoice.test.js",
        r".\.runtime\node.exe --test another.test.js",
    ]:
        assert not _matching_effective_verification_commands(
            observed_command=other, effective_verification_commands=[required]
        )


def test_windows_glob_expansion_keeps_executable_path_and_call_operator(tmp_path):
    (tmp_path / "one test.js").write_text("")
    command = r"& '.\local runtime\node.exe' --test *.js"
    expanded = _expand_verification_command_globs(command, root=tmp_path)
    assert expanded.startswith("& ")
    assert analyze_verification_command(expanded).parts == (
        r".\local runtime\node.exe",
        "--test",
        "one test.js",
    )


def test_native_execution_identity_binds_environment_policy_and_helper(tmp_path):
    from alysis_code.sandbox_settings import ShellSandboxSettings
    from alysis_code.windows_mxc import WindowsMxcRunner

    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"first helper")
    runner = WindowsMxcRunner(ShellSandboxSettings(backend="windows-mxc"))
    policy = {
        "protocol": 4,
        "argv": ["shell", "npm.cmd test"],
        "cwd": str(tmp_path),
        "env": [["SYNTHETIC_SECRET", "never-publish-this-value"]],
        "read": [str(tmp_path)],
        "write": [str(tmp_path)],
        "deny_read": [],
        "deny_write": [],
        "network": "off",
        "local_preview": False,
        "enumerate": ["C:\\"],
        "discovery_deny": [],
        "parent_pid": 1,
        "parent_created": 1,
        "timeout_ms": 100,
    }

    def identity(request):
        return runner._execution_context(
            [str(helper), "mxc-run-request"], json.dumps(request).encode()
        )

    original = identity(policy)
    assert original.startswith("windows-mxc-v1:")
    assert "never-publish-this-value" not in original
    assert (
        identity(
            {
                **policy,
                "argv": ["shell", "different command"],
                "parent_pid": 2,
                "parent_created": 2,
                "timeout_ms": 200,
            }
        )
        == original
    )
    for key, value in {
        "env": [["SYNTHETIC_SECRET", "changed"]],
        "cwd": str(tmp_path / "other"),
        "write": [],
        "read": [],
        "deny_read": ["private"],
        "deny_write": ["protected"],
        "network": "on",
        "local_preview": True,
        "enumerate": [],
        "discovery_deny": ["private"],
    }.items():
        assert identity({**policy, key: value}) != original, key
    helper.write_bytes(b"second helper")
    assert identity(policy) != original


def test_native_verification_rerun_key_requires_same_command_cwd_and_policy(tmp_path):
    from alysis_code.agent.verification import _verification_outcome_key

    arguments = dict(
        root=tmp_path,
        command="npm.cmd test",
        working_directory=".",
        environment_known=False,
        execution_context="windows-mxc-v1:" + "a" * 64,
    )
    original = _verification_outcome_key(**arguments)
    assert original
    for change in [
        {"command": "npm.cmd run build"},
        {"working_directory": "other"},
        {"execution_context": "windows-mxc-v1:" + "b" * 64},
    ]:
        assert _verification_outcome_key(**{**arguments, **change}) != original
    for invalid in [None, "", "a" * 64, "windows-mxc-v1:untrusted", 42]:
        assert _verification_outcome_key(**{**arguments, "execution_context": invalid}) is None


def test_combined_verification_context_keeps_native_and_runner_scope_independent(tmp_path):
    from alysis_code.agent.verification import _verification_outcome_key

    arguments = dict(
        root=tmp_path,
        command="npm.cmd test",
        working_directory=".",
        environment_known=False,
        execution_context="windows-mxc-v1:" + "a" * 64,
        execution_scope="b" * 64,
    )
    original = _verification_outcome_key(**arguments)
    assert original
    for change in [
        {"execution_context": "windows-mxc-v1:" + "c" * 64},
        {"execution_scope": "d" * 64},
        {"execution_context": None},
        {"execution_scope": None},
    ]:
        assert _verification_outcome_key(**{**arguments, **change}) != original
