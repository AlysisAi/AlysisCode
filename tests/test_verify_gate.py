from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import alysis_code.sandbox_runner as sandbox_runner_mod
import alysis_code.verify_gate as verify_gate_mod
from alysis_code.config import AppConfig
from alysis_code.failure_category import FailureCategory
from alysis_code.repo_scan import scan_workspace
from alysis_code.verification_command_analysis import VerificationCommandStatus
from alysis_code.verification_contract import (
    VerificationCommandExecutionMode,
    VerificationCommandValidationStatus,
    build_verification_command_specs,
)
from alysis_code.verify_gate import (
    ResolvedVerifyCommands,
    VerifyCommandResult,
    VerifyError,
    VerifyRunResult,
    _normalize_execution_semantics_parts,
    _verification_family_for_result,
    assess_verification_command_execution,
    compact_verification_payload,
    is_authoritative_verify_command_selection,
    is_toolchain_unavailable_verification_output,
    normalize_verify_mode,
    refine_generic_fallback_verify_command_selection,
    resolve_task_aware_verify_command_selection,
    resolve_verify_artifact_payload,
    resolve_verify_command_selection,
    resolve_verify_commands,
    resolve_verify_sandbox_mode,
    run_task_verification,
    validation_errors_for_selection,
    verification_selection_payload,
    verify_run_result_to_payload,
)
from alysis_code.workspace_context import resolve_workspace_context


def _cp(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args="cmd",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _write_repo_files(root: Path, files: dict[str, str]) -> None:
    for relpath, body in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _verify_cfg(*, mode: str | None = None) -> AppConfig:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {}
    if mode is not None:
        cfg.extra_fields["verify_sandbox"] = {"mode": mode}
    return cfg


@pytest.mark.parametrize("cleanup_pending", [False, True])
def test_timeout_report_preserves_pending_process_cleanup(tmp_path, monkeypatch, cleanup_pending):
    def timeout(*_args, **_kwargs):
        error = subprocess.TimeoutExpired("owned command", 0.4)
        error.cleanup_pending = cleanup_pending
        raise error

    monkeypatch.setattr(sandbox_runner_mod.HostShellRunner, "run", timeout)
    result = run_task_verification(
        root=tmp_path,
        commands=["python slow.py"],
        artifact_path=tmp_path / "verification.txt",
        cfg=_verify_cfg(mode="off"),
        timeout_s=0.4,
    )
    assert result.command_results[0].exit_code == 124
    assert ("process cleanup pending" in result.command_results[0].stderr) is cleanup_pending


def test_legacy_collections_import_collection_error_is_infra_unavailable() -> None:
    output = """ERROR collecting tests/test_app.py
tests/test_app.py:2: in <module>
    from collections import Mapping
E   ImportError: cannot import name 'Mapping' from 'collections'
"""

    assert is_toolchain_unavailable_verification_output(output) is True
    assert (
        is_toolchain_unavailable_verification_output(
            "ImportError: cannot import name 'Widget' from 'project.api'"
        )
        is False
    )


def test_typed_verification_contract_rejects_explicit_pipeline() -> None:
    specs = build_verification_command_specs(
        ("tool args | tail -n 1",),
        source="task_refinement.explicit_user_command",
        contract_type="task_acceptance",
    )

    assert specs[0].execution_mode == VerificationCommandExecutionMode.INVALID
    assert specs[0].validation_status == VerificationCommandValidationStatus.INVALID
    assert specs[0].rejection_reason == "unsafe_pipeline"
    assert specs[0].provenance == "EXPLICIT_USER_COMMAND"


def test_typed_verification_contract_rejects_inferred_pipeline() -> None:
    specs = build_verification_command_specs(
        ("tool args | tail -n 1",),
        source="task_refinement.no_authoritative_commands",
        contract_type="task_inferred",
    )

    assert specs[0].execution_mode == VerificationCommandExecutionMode.INVALID
    assert specs[0].validation_status == VerificationCommandValidationStatus.INVALID
    assert specs[0].rejection_reason == "unsafe_pipeline"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("true", "vacuous_verifier"),
        ("/bin/true", "vacuous_verifier"),
        (":", "vacuous_verifier"),
        ("exit 0", "vacuous_verifier"),
        ("echo ok", "non_assertive_observation"),
        ("printf ok", "non_assertive_observation"),
        ("python -c 'pass'", "vacuous_verifier"),
        ("python -c 'print(\"ok\")'", "vacuous_verifier"),
        ("pytest -q || true", "disallowed_shell_control_flow"),
        ("pytest -q || :", "disallowed_shell_control_flow"),
        ("pytest -q; true", "disallowed_shell_control_flow"),
        ("pytest -q | cat", "unsafe_pipeline"),
        ("pytest -q | tail -n 1", "unsafe_pipeline"),
        ("pytest -q | tee verify.log", "unsafe_pipeline"),
        ("bash -lc 'pytest -q || true'", "disallowed_shell_control_flow"),
        ("bash -lc 'pytest -q | cat'", "unsafe_pipeline"),
    ],
)
def test_typed_verification_contract_rejects_vacuous_authoritative_commands(
    command: str,
    reason: str,
) -> None:
    specs = build_verification_command_specs(
        (command,),
        source="cli.verify_cmd",
        contract_type="explicit_override",
    )

    assert specs[0].execution_mode == VerificationCommandExecutionMode.INVALID
    assert specs[0].validation_status == VerificationCommandValidationStatus.INVALID
    assert specs[0].rejection_reason == reason


@pytest.mark.parametrize("command", ["pytest -q", "diff expected actual", "grep -q needle file"])
def test_typed_verification_contract_keeps_assertive_commands(command: str) -> None:
    specs = build_verification_command_specs(
        (command,),
        source="cli.verify_cmd",
        contract_type="explicit_override",
    )

    assert specs[0].validation_status == VerificationCommandValidationStatus.VALID
    assert specs[0].rejection_reason == ""


def test_typed_verification_contract_accepts_safe_cd_and_assertive_command() -> None:
    specs = build_verification_command_specs(
        ("cd /tmp/project && python -m pytest tests/test_batching.py -v",),
        source="cli.verify_cmd",
        contract_type="explicit_override",
    )

    assert specs[0].execution_mode == VerificationCommandExecutionMode.TRUSTED_SHELL_EXPRESSION
    assert specs[0].validation_status == VerificationCommandValidationStatus.VALID


def test_typed_verification_contract_rejects_safe_cd_to_non_assertive_command() -> None:
    specs = build_verification_command_specs(
        ("cd /tmp/project && true",),
        source="cli.verify_cmd",
        contract_type="explicit_override",
    )

    assert specs[0].execution_mode == VerificationCommandExecutionMode.INVALID
    assert specs[0].validation_status == VerificationCommandValidationStatus.INVALID
    assert specs[0].rejection_reason == "vacuous_verifier"


def test_safe_cd_pytest_counts_as_real_execution() -> None:
    command = "cd /tmp/project && python -m pytest tests/test_batching.py -v"

    assert _normalize_execution_semantics_parts(command) == [
        "pytest",
        "tests/test_batching.py",
        "-v",
    ]
    assert _verification_family_for_result(command) == "pytest"

    assessment = assess_verification_command_execution(
        command=command,
        exit_code=0,
        output="1 passed\n",
    )

    assert assessment.real_execution is True
    assert assessment.non_execution_reason is None


def test_safe_cd_true_is_not_real_execution() -> None:
    assessment = assess_verification_command_execution(
        command="cd /tmp/project && true",
        exit_code=0,
        output="",
    )

    assert assessment.real_execution is False
    assert assessment.non_execution_reason == "vacuous_verifier"


@pytest.mark.parametrize("pipe_tail", ["cat", "tail -n 1", "tee verify.log"])
def test_masked_pipeline_exit_zero_is_not_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pipe_tail: str,
) -> None:
    command = f"pytest -q | {pipe_tail}"

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == command
        return _cp(returncode=0, stdout="1 failed\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_task_verification(
        root=tmp_path,
        commands=[command],
        artifact_path=tmp_path / "verify.txt",
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.command_results[0].status.value == "not_executed"
    assert result.command_results[0].non_execution_reason == "unsafe_pipeline"
    assert payload["all_passed"] is False
    assert payload["command_results"][0]["status"] == "not_executed"


def test_resolved_explicit_vacuous_verify_command_reports_validation_error(
    tmp_path: Path,
) -> None:
    resolved = resolve_verify_command_selection(
        cfg=AppConfig(model="test-model"),
        verify_cmd=["true"],
        root=tmp_path,
    )

    assert resolved.commands == ("true",)
    assert validation_errors_for_selection(resolved) == ["true: vacuous_verifier"]


def test_verify_gate_explicit_off_runs_commands_and_writes_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    shell_flags: list[bool] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        shell_flags.append(bool(_kwargs.get("shell")))
        calls.append(cmd)
        if cmd == "pytest -q":
            return _cp(returncode=0, stdout="ok\n")
        return _cp(returncode=1, stderr="lint failed\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T01.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q", "ruff check ."],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    assert result.all_passed is False
    assert result.failure_category_value == FailureCategory.VERIFICATION_FAILED.value
    assert result.failed_commands == ["ruff check ."]
    assert calls == ["pytest -q", "ruff check ."]
    assert shell_flags == [True, True]
    body = artifact.read_text(encoding="utf-8")
    assert "exit_code: 0" in body
    assert "exit_code: 1" in body
    assert "lint failed" in body


def test_verify_gate_failed_assertion_on_healthy_runner_is_verification_failed(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _cp(returncode=1, stdout="FAILED test_app.py::test_calc - AssertionError\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "assertion.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.failure_category_value == FailureCategory.VERIFICATION_FAILED.value
    assert payload["failure_category"] == FailureCategory.VERIFICATION_FAILED.value


def test_verify_gate_sandbox_start_failure_is_infra_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    def fail_build_runner(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise verify_gate_mod.ConfigError(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        )

    monkeypatch.setattr(verify_gate_mod, "build_shell_runner_from_settings", fail_build_runner)

    artifact = tmp_path / "verify" / "infra.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="strict"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.failure_category_value == FailureCategory.INFRA_UNAVAILABLE.value
    assert payload["failure_category"] == FailureCategory.INFRA_UNAVAILABLE.value
    assert result.failed_commands == ["pytest -q"]


def test_verify_run_payload_includes_primary_failure_for_failed_command(tmp_path: Path) -> None:
    artifact = tmp_path / "verify" / "primary_failure.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest -q", "ruff check ."],
        command_results=[
            VerifyCommandResult(
                command="pytest -q", exit_code=0, output="ok\n", real_execution=True
            ),
            VerifyCommandResult(
                command="ruff check .",
                exit_code=1,
                output="collected 2 files\nE   AssertionError: expected 2 == 3\n",
                effective_command="ruff check .",
            ),
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert payload["all_passed"] is False
    assert payload["primary_failure"] == {
        "command": "ruff check .",
        "effective_command": "ruff check .",
        "snippet": "E   AssertionError: expected 2 == 3",
        "output_truncated": False,
        "fallback_used": False,
    }


def test_verify_run_payload_omits_primary_failure_when_all_commands_pass(tmp_path: Path) -> None:
    artifact = tmp_path / "verify" / "primary_failure_pass.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("ok\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest -q"],
        command_results=[
            VerifyCommandResult(
                command="pytest -q", exit_code=0, output="ok\n", real_execution=True
            )
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert payload["all_passed"] is True
    assert "primary_failure" not in payload


def test_verify_run_payload_primary_failure_prefers_actionable_line_over_generic_noise(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "verify" / "primary_failure_actionable.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest tests/test_placeholder.py -q", "pytest tests/test_notes.py -q"],
        command_results=[
            VerifyCommandResult(
                command="pytest tests/test_placeholder.py -q",
                exit_code=1,
                output="============================= test session starts =============================\ncollected 0 items\n",
            ),
            VerifyCommandResult(
                command="pytest tests/test_notes.py -q",
                exit_code=1,
                output=(
                    "============================= test session starts =============================\n"
                    "FAILED tests/test_notes.py::test_filter - AssertionError: expected 1 == 2\n"
                ),
            ),
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)
    primary_failure = payload["primary_failure"]

    assert isinstance(primary_failure, dict)
    assert primary_failure["command"] == "pytest tests/test_notes.py -q"
    assert primary_failure["snippet"] == (
        "FAILED tests/test_notes.py::test_filter - AssertionError: expected 1 == 2"
    )


def test_verify_run_payload_primary_failure_prefers_module_not_found_over_pytest_node(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "verify" / "primary_failure_module_not_found.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest --doctest-glob=README.md -q README.md"],
        command_results=[
            VerifyCommandResult(
                command="pytest --doctest-glob=README.md -q README.md",
                exit_code=1,
                output=(
                    "F                                                                        [100%]\n"
                    "FAILED README.md::README.md\n"
                    "E   ModuleNotFoundError: No module named 'mathlet'\n"
                ),
            )
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)
    primary_failure = payload["primary_failure"]

    assert isinstance(primary_failure, dict)
    assert primary_failure["snippet"] == "E   ModuleNotFoundError: No module named 'mathlet'"


def test_compact_verification_payload_preserves_primary_failure_hint(tmp_path: Path) -> None:
    artifact = tmp_path / "verify" / "primary_failure_compact.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest tests/test_notes.py -q"],
        command_results=[
            VerifyCommandResult(
                command="pytest tests/test_notes.py -q",
                exit_code=1,
                output=(
                    "FAILED tests/test_notes.py::test_filter - AssertionError: "
                    "expected left side to equal right side after normalization\n"
                ),
            )
        ],
        artifact_path=artifact,
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    compact_payload = compact_verification_payload(payload, output_preview_chars=48)

    assert compact_payload is not None
    primary_failure = compact_payload["primary_failure"]
    assert isinstance(primary_failure, dict)
    assert primary_failure["command"] == "pytest tests/test_notes.py -q"
    assert primary_failure["effective_command"] == "pytest tests/test_notes.py -q"
    assert str(primary_failure["snippet"]).startswith("FAILED tests/test_notes.py::")
    assert str(primary_failure["snippet"]).endswith("...")
    assert primary_failure["output_truncated"] is False
    assert primary_failure["fallback_used"] is False


def test_verify_run_payload_includes_structured_pytest_failure_summary(tmp_path: Path) -> None:
    artifact = tmp_path / "verify" / "pytest_failure_summary.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    output = (
        "FAILED tests/test_calc.py::test_add - AssertionError: expected 2 == 3\n"
        "Traceback (most recent call last):\n"
        '  File "tests/test_calc.py", line 10, in test_add\n'
        "    assert add(1, 1) == 3\n"
        '  File "src/calc.py", line 4, in add\n'
        "    return a + b\n"
        "E   AssertionError: expected 2 == 3\n"
    )
    result = VerifyRunResult(
        commands=["python -m pytest tests/test_calc.py -q"],
        command_results=[
            VerifyCommandResult(
                command="python -m pytest tests/test_calc.py -q",
                exit_code=1,
                output=output,
            )
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    summary = payload["failure_summary"]
    assert isinstance(summary, dict)
    assert summary["framework"] == "pytest"
    assert summary["primary_error"] == "AssertionError: expected 2 == 3"
    assert summary["likely_next_files"] == ["tests/test_calc.py", "src/calc.py"]
    failing_tests = summary["failing_tests"]
    assert isinstance(failing_tests, list)
    assert failing_tests[0]["id"] == "tests/test_calc.py::test_add"
    stack_frames = summary["stack_frames"]
    assert isinstance(stack_frames, list)
    assert {"path": "src/calc.py", "line": 4, "symbol": "add"} in stack_frames
    command_summary = payload["command_results"][0]["failure_summary"]
    assert command_summary == summary


def test_verify_run_payload_failure_summary_rejects_escaping_paths_and_preserves_dot_paths(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "verify" / "escaping_failure_summary.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    output = (
        "FAILED ../outside.py::test_secret - AssertionError: leaked path\n"
        '  File "../outside.py", line 5, in test_secret\n'
        "    assert False\n"
        '  File "./.github/workflows/check.py", line 3, in check\n'
        "    assert False\n"
        "E   AssertionError: leaked path\n"
    )
    result = VerifyRunResult(
        commands=["python -m pytest -q"],
        command_results=[
            VerifyCommandResult(
                command="python -m pytest -q",
                exit_code=1,
                output=output,
            )
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    summary = payload["failure_summary"]
    assert isinstance(summary, dict)
    assert summary["failing_tests"] == []
    assert summary["likely_next_files"] == [".github/workflows/check.py"]
    assert {"path": ".github/workflows/check.py", "line": 3, "symbol": "check"} in summary[
        "stack_frames"
    ]
    assert "outside.py" not in str(summary)


def test_verify_run_payload_normalizes_absolute_pytest_nodeid(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_path = tests_dir / "test_calc.py"
    test_path.write_text("def test_add():\n    assert False\n", encoding="utf-8")
    artifact = tmp_path / "verify" / "absolute_nodeid_failure_summary.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    output = f"FAILED {test_path}::test_add - AssertionError: expected 2 == 3\n"
    result = VerifyRunResult(
        commands=["python -m pytest tests/test_calc.py -q"],
        command_results=[
            VerifyCommandResult(
                command="python -m pytest tests/test_calc.py -q",
                exit_code=1,
                output=output,
            )
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    summary = payload["failure_summary"]
    assert isinstance(summary, dict)
    assert summary["failing_tests"] == [
        {
            "id": "tests/test_calc.py::test_add",
            "path": "tests/test_calc.py",
            "message": "AssertionError: expected 2 == 3",
        }
    ]


def test_compact_verification_payload_preserves_failure_summary(tmp_path: Path) -> None:
    artifact = tmp_path / "verify" / "compact_failure_summary.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest tests/test_calc.py -q"],
        command_results=[
            VerifyCommandResult(
                command="pytest tests/test_calc.py -q",
                exit_code=1,
                output=(
                    "FAILED tests/test_calc.py::test_add - AssertionError: expected 2 == 3\n"
                    '  File "src/calc.py", line 4, in add\n'
                    "E   AssertionError: expected 2 == 3\n"
                ),
            )
        ],
        artifact_path=artifact,
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    compact_payload = compact_verification_payload(payload, output_preview_chars=48)

    assert compact_payload is not None
    summary = compact_payload["failure_summary"]
    assert isinstance(summary, dict)
    assert summary["framework"] == "pytest"
    assert summary["likely_next_files"] == ["tests/test_calc.py", "src/calc.py"]
    command_summary = compact_payload["command_results"][0]["failure_summary"]
    assert command_summary == summary


def test_verify_run_payload_summarizes_generic_python_traceback(tmp_path: Path) -> None:
    artifact = tmp_path / "verify" / "python_traceback_summary.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["python script.py"],
        command_results=[
            VerifyCommandResult(
                command="python script.py",
                exit_code=1,
                output=(
                    "Traceback (most recent call last):\n"
                    '  File "script.py", line 2, in <module>\n'
                    "    main()\n"
                    '  File "src/app.py", line 7, in main\n'
                    "    raise RuntimeError('boom')\n"
                    "RuntimeError: boom\n"
                ),
            )
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    summary = payload["failure_summary"]
    assert isinstance(summary, dict)
    assert summary["framework"] == "python"
    assert summary["primary_error"] == "RuntimeError: boom"
    assert {"path": "src/app.py", "line": 7, "symbol": "main"} in summary["stack_frames"]


def test_verify_run_payload_primary_failure_uses_actionable_command_output(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "verify" / "primary_failure_summary.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failed\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["go test ./..."],
        command_results=[
            VerifyCommandResult(
                command="go test ./...",
                exit_code=1,
                output="package example/pkg failed\n",
                real_execution=None,
            )
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)
    primary_failure = payload["primary_failure"]

    assert isinstance(primary_failure, dict)
    assert primary_failure["command"] == "go test ./..."
    assert primary_failure["effective_command"] == "go test ./..."
    assert primary_failure["snippet"] == "package example/pkg failed"
    assert primary_failure["output_truncated"] is False
    assert primary_failure["fallback_used"] is False


def test_verify_gate_handles_missing_shell_without_crashing(tmp_path: Path, monkeypatch) -> None:
    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("shell not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T02.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    assert result.all_passed is False
    assert result.failed_commands == ["pytest -q"]
    body = artifact.read_text(encoding="utf-8")
    assert "exit_code: 127" in body
    assert "shell not found" in body


def test_verify_gate_retries_pytest_with_interpreter_on_execution_layer_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    fallback_cmd = shlex.join([sys.executable, "-m", "pytest", "-q"])

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        text = str(cmd)
        calls.append(text)
        if text == "pytest -q":
            return _cp(returncode=126, stderr="/bin/sh: 1: pytest: Permission denied\n")
        if text == fallback_cmd:
            return _cp(returncode=0, stdout="ok via python -m pytest\n")
        raise AssertionError(f"unexpected command: {text!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T02_fallback.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert calls == ["pytest -q", fallback_cmd]
    assert result.all_passed is True
    assert result.command_results[0].effective_command == fallback_cmd
    assert result.command_results[0].fallback_used is True
    assert result.command_results[0].fallback_reason == "pytest_entrypoint_unavailable"
    assert result.command_results[0].real_execution is True
    assert payload["fallback_used"] is True
    assert payload["fallback_count"] == 1
    command_payload = payload["command_results"][0]
    assert command_payload["command"] == "pytest -q"
    assert command_payload["effective_command"] == fallback_cmd
    assert command_payload["fallback_used"] is True
    assert command_payload["fallback_reason"] == "pytest_entrypoint_unavailable"
    assert command_payload["real_execution"] is True
    body = artifact.read_text(encoding="utf-8")
    assert "requested_command: pytest -q" in body
    assert f"effective_command: {fallback_cmd}" in body
    assert "fallback_used: true" in body
    assert "initial output" in body


def test_verify_gate_retries_pytest_with_interpreter_on_entrypoint_import_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    fallback_cmd = shlex.join([sys.executable, "-m", "pytest", "-q"])

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        text = str(cmd)
        calls.append(text)
        if text == "pytest -q":
            return _cp(
                returncode=1,
                stderr=(
                    "Traceback (most recent call last):\n"
                    '  File "/home/user/.local/bin/pytest", line 5, in <module>\n'
                    "    from pytest import console_main\n"
                    "ModuleNotFoundError: No module named 'pytest'\n"
                ),
            )
        if text == fallback_cmd:
            return _cp(returncode=0, stdout="ok via python -m pytest\n")
        raise AssertionError(f"unexpected command: {text!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T02_fallback_import.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )

    assert calls == ["pytest -q", fallback_cmd]
    assert result.all_passed is True
    assert result.command_results[0].effective_command == fallback_cmd
    assert result.command_results[0].fallback_used is True
    assert result.command_results[0].fallback_reason == "pytest_entrypoint_unavailable"
    body = artifact.read_text(encoding="utf-8")
    assert "ModuleNotFoundError: No module named 'pytest'" in body
    assert "fallback_used: true" in body


def test_verify_gate_marks_go_no_tests_to_run_as_non_executing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "go test -run NonExistent ./..."
        return _cp(returncode=0, stdout="ok\texample/pkg\t0.002s [no tests to run]\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T05_go_no_tests.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["go test -run NonExistent ./..."],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.status == "not_run"
    assert result.failed_commands == []
    item = result.command_results[0]
    assert item.status == VerificationCommandStatus.SKIPPED
    assert item.ok is True
    assert item.real_execution is False
    assert item.non_execution_reason == "go_test_no_tests_to_run"
    assert payload["all_passed"] is False
    assert payload["status"] == "not_run"
    assert payload["failed_commands"] == []
    assert payload["command_results"][0]["status"] == "skipped"
    assert payload["command_results"][0]["ok"] is True
    assert payload["command_results"][0]["real_execution"] is False
    assert payload["command_results"][0]["non_execution_reason"] == "go_test_no_tests_to_run"
    body = artifact.read_text(encoding="utf-8")
    assert "real_execution: false" in body
    assert "non_execution_reason: go_test_no_tests_to_run" in body


def test_verify_gate_treats_pytest_exit_5_no_tests_as_skipped_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "pytest -q"
        (tmp_path / ".pytest_cache").mkdir()
        return _cp(
            returncode=5,
            stdout="custom plugin suppressed the collection summary\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T05_pytest_no_tests.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.status == "not_run"
    assert result.failed_commands == []
    assert result.summary == "verification skipped: nothing to verify (1/1)"
    item = result.command_results[0]
    assert item.status == VerificationCommandStatus.SKIPPED
    assert item.ok is True
    assert item.real_execution is False
    assert item.non_execution_reason == "pytest_no_tests_collected"
    assert payload["all_passed"] is False
    assert payload["status"] == "not_run"
    assert payload["failed_commands"] == []
    assert payload["summary"] == "verification skipped: nothing to verify (1/1)"
    assert payload["command_results"][0]["status"] == "skipped"
    assert payload["command_results"][0]["ok"] is True
    assert payload["command_results"][0]["real_execution"] is False
    assert payload["command_results"][0]["non_execution_reason"] == "pytest_no_tests_collected"
    assert not (tmp_path / ".pytest_cache").exists()
    body = artifact.read_text(encoding="utf-8")
    assert "status: skipped" in body
    assert "real_execution: false" in body
    assert "non_execution_reason: pytest_no_tests_collected" in body


def test_verify_gate_restores_preexisting_pytest_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_file = tmp_path / ".pytest_cache" / "v" / "cache" / "nodeids"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("preexisting\n", encoding="utf-8")
    added_file = tmp_path / ".pytest_cache" / "v" / "cache" / "lastfailed"

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "pytest -q"
        cache_file.write_text("mutated\n", encoding="utf-8")
        added_file.write_text("new cache entry\n", encoding="utf-8")
        return _cp(returncode=0, stdout="1 passed\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=tmp_path / "verify" / "T05_cache_restore.txt",
        cfg=_verify_cfg(mode="off"),
    )

    assert result.all_passed is True
    assert cache_file.read_text(encoding="utf-8") == "preexisting\n"
    assert not added_file.exists()


def test_verify_gate_treats_nothing_to_do_as_skipped_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "make test"
        return _cp(returncode=0, stdout="make: Nothing to be done for 'test'.\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T05_make_nothing_to_do.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["make test"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.status == "not_run"
    assert result.failed_commands == []
    assert result.summary == "verification skipped: nothing to verify (1/1)"
    item = result.command_results[0]
    assert item.status == VerificationCommandStatus.SKIPPED
    assert item.ok is True
    assert item.real_execution is False
    assert item.non_execution_reason == "verification_nothing_to_do"
    assert payload["command_results"][0]["status"] == "skipped"
    assert payload["command_results"][0]["non_execution_reason"] == ("verification_nothing_to_do")


def test_verify_gate_keeps_pytest_exit_1_as_failed_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "pytest -q"
        return _cp(returncode=1, stdout="FAILED tests/test_example.py::test_example\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T05_pytest_failed.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.failed_commands == ["pytest -q"]
    item = result.command_results[0]
    assert item.status == VerificationCommandStatus.FAILED
    assert item.ok is False
    assert item.real_execution is None
    assert item.non_execution_reason is None
    assert payload["all_passed"] is False
    assert payload["failed_commands"] == ["pytest -q"]
    assert payload["command_results"][0]["status"] == "failed"
    assert payload["command_results"][0]["ok"] is False


def test_verify_gate_marks_go_no_test_files_as_non_executing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "go test ./..."
        return _cp(returncode=0, stdout="?   \texample/pkg\t[no test files]\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T06_go_no_test_files.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["go test ./..."],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    assert result.status == "not_run"
    assert result.failed_commands == []
    item = result.command_results[0]
    assert item.status == VerificationCommandStatus.SKIPPED
    assert item.ok is True
    assert item.real_execution is False
    assert item.non_execution_reason == "go_test_no_test_files"
    assert payload["all_passed"] is False
    assert payload["status"] == "not_run"
    assert payload["failed_commands"] == []
    assert payload["command_results"][0]["status"] == "skipped"
    assert payload["command_results"][0]["ok"] is True
    assert payload["command_results"][0]["real_execution"] is False
    assert payload["command_results"][0]["non_execution_reason"] == "go_test_no_test_files"
    body = artifact.read_text(encoding="utf-8")
    assert "real_execution: false" in body
    assert "non_execution_reason: go_test_no_test_files" in body


def test_verify_gate_marks_normal_go_test_success_as_real_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "go test ./..."
        return _cp(returncode=0, stdout="ok\texample/pkg\t0.002s\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T07_go_ok.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["go test ./..."],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )

    assert result.all_passed is True
    assert result.command_results[0].real_execution is True
    assert result.command_results[0].non_execution_reason is None


def test_verify_gate_marks_mixed_go_package_output_as_real_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "go test ./..."
        return _cp(
            returncode=0,
            stdout=("?   \texample/pkg1\t[no test files]\nok  \texample/pkg2\t0.002s\n"),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T08_go_mixed_ok.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["go test ./..."],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )

    assert result.all_passed is True
    assert result.command_results[0].real_execution is True
    assert result.command_results[0].non_execution_reason is None


def test_verify_gate_marks_mixed_go_no_tests_to_run_and_real_execution_as_real(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == "go test ./..."
        return _cp(
            returncode=0,
            stdout=("ok  \texample/pkg1\t0.002s [no tests to run]\nok  \texample/pkg2\t0.003s\n"),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T09_go_mixed_real.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["go test ./..."],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )

    assert result.all_passed is True
    assert result.command_results[0].real_execution is True
    assert result.command_results[0].non_execution_reason is None


def test_verify_gate_rejects_compound_go_test_and_build_with_no_test_files_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command = "go test ./... && go build ./..."

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == command
        return _cp(returncode=0, stdout="?   \texample/pkg\t[no test files]\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T10_go_compound_no_test_files.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=[command],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    item = result.command_results[0]
    assert item.real_execution is False
    assert item.non_execution_reason == "disallowed_shell_control_flow"
    assert payload["all_passed"] is False
    assert payload["command_results"][0]["real_execution"] is False
    assert payload["command_results"][0]["status"] == "not_executed"
    body = artifact.read_text(encoding="utf-8")
    assert "status: not_executed" in body
    assert "non_execution_reason: disallowed_shell_control_flow" in body


def test_verify_gate_rejects_compound_go_test_and_build_with_no_tests_to_run_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command = "go test ./... && go build ./..."

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == command
        return _cp(returncode=0, stdout="ok\texample/pkg\t0.002s [no tests to run]\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T11_go_compound_no_tests_to_run.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=[command],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    item = result.command_results[0]
    assert item.real_execution is False
    assert item.non_execution_reason == "disallowed_shell_control_flow"
    assert payload["all_passed"] is False
    assert payload["command_results"][0]["real_execution"] is False
    assert payload["command_results"][0]["status"] == "not_executed"
    body = artifact.read_text(encoding="utf-8")
    assert "status: not_executed" in body
    assert "non_execution_reason: disallowed_shell_control_flow" in body


def test_verify_gate_rejects_wrapped_shell_compound_go_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command = 'bash -lc "go test ./... && go build ./..."'

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        assert cmd == command
        return _cp(returncode=0, stdout="?   \texample/pkg\t[no test files]\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T12_go_wrapped_compound.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=[command],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )
    payload = verify_run_result_to_payload(root=tmp_path, result=result)

    assert result.all_passed is False
    item = result.command_results[0]
    assert item.real_execution is False
    assert item.non_execution_reason == "disallowed_shell_control_flow"
    assert payload["all_passed"] is False
    assert payload["command_results"][0]["real_execution"] is False
    assert payload["command_results"][0]["status"] == "not_executed"
    body = artifact.read_text(encoding="utf-8")
    assert "status: not_executed" in body
    assert "non_execution_reason: disallowed_shell_control_flow" in body


def test_verify_mode_and_command_resolution() -> None:
    assert normalize_verify_mode("STRICT") == "strict"
    cfg = AppConfig(model="test-model")
    assert resolve_verify_commands(cfg=cfg, verify_cmd=None) == ["pytest -q"]
    assert resolve_verify_commands(cfg=cfg, verify_cmd=["python -m pytest -q"]) == [
        "python -m pytest -q"
    ]


def test_verify_command_resolution_prefers_repo_inference_over_generic_default_fallback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "packages" / "web").mkdir(parents=True)
    (repo / "packages" / "web" / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    (repo / "services" / "orders").mkdir(parents=True)
    (repo / "services" / "orders" / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>\n",
        encoding="utf-8",
    )

    cfg = AppConfig(model="test-model")
    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("npm --prefix packages/web test", "mvn -f services/orders/pom.xml test"),
        source="repo_scan.likely_test_commands",
    )
    assert resolve_verify_commands(cfg=cfg, verify_cmd=None, root=repo) == [
        "npm --prefix packages/web test",
        "mvn -f services/orders/pom.xml test",
    ]


def test_managed_host_unavailable_empty_config_uses_repo_inference(tmp_path: Path) -> None:
    _write_repo_files(tmp_path, {"tests/test_app.py": "def test_ok(): pass\n"})
    scan = scan_workspace(context=resolve_workspace_context(tmp_path))
    cfg = AppConfig(model="test-model", verify_commands=[])
    cfg.extra_fields = {"managed_host_verifier_unavailable": True}

    resolved = resolve_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=tmp_path,
        repo_scan=scan,
    )

    assert resolved.source == "repo_scan.likely_test_commands"
    assert resolved.commands


def test_verify_command_resolution_infers_plain_python_layout_without_manifest(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    cfg = AppConfig(model="test-model")
    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="repo_scan.likely_test_commands",
    )


def test_verify_command_resolution_keeps_ambiguous_repo_on_generic_fallback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "app.test.js").write_text("test('ok', () => {})\n", encoding="utf-8")

    cfg = AppConfig(model="test-model")
    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )


def test_verify_command_resolution_keeps_tests_helper_py_repo_on_generic_fallback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    cfg = AppConfig(model="test-model")
    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )


def test_verify_command_resolution_keeps_cli_and_nondefault_config_higher_priority(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "packages" / "web").mkdir(parents=True)
    (repo / "packages" / "web" / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )

    cfg = AppConfig(model="test-model", verify_commands=["make verify"])
    configured = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)
    cli = resolve_verify_command_selection(
        cfg=cfg,
        verify_cmd=["pnpm --dir packages/web test"],
        root=repo,
    )

    assert configured == ResolvedVerifyCommands(
        commands=("make verify",),
        source="config.verify_commands",
    )
    assert cli == ResolvedVerifyCommands(
        commands=("pnpm --dir packages/web test",),
        source="cli.verify_cmd",
    )


def test_generic_configured_verify_preset_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest -q", "ruff check ."),
        source="config.verify_commands_generic_preset",
    )


def test_pytest_plus_plain_ruff_check_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check"])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest -q", "ruff check"),
        source="config.verify_commands_generic_preset",
    )


def test_uv_run_pytest_and_plain_ruff_check_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "ruff check"])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("uv run pytest -q", "ruff check"),
        source="config.verify_commands_generic_preset",
    )


def test_uv_run_pytest_and_uv_run_ruff_check_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "uv run ruff check"])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("uv run pytest -q", "uv run ruff check"),
        source="config.verify_commands_generic_preset",
    )


def test_python_m_pytest_and_python_m_ruff_check_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=["python -m pytest -q", "python -m ruff check ."],
    )

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("python -m pytest -q", "python -m ruff check ."),
        source="config.verify_commands_generic_preset",
    )


def test_uv_run_python_m_pytest_and_ruff_check_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=["uv run python -m pytest -q", "uv run python -m ruff check ."],
    )

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("uv run python -m pytest -q", "uv run python -m ruff check ."),
        source="config.verify_commands_generic_preset",
    )


def test_py_m_pytest_and_py_m_ruff_check_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=["py -m pytest -q", "py -m ruff check ."],
    )

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("py -m pytest -q", "py -m ruff check ."),
        source="config.verify_commands_generic_preset",
    )


def test_windows_backslash_python_exe_generic_preset_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=[
            r"C:\Python311\python.exe -m pytest -q",
            r"C:\Python311\python.exe -m ruff check .",
        ],
    )

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=(
            r"C:\Python311\python.exe -m pytest -q",
            r"C:\Python311\python.exe -m ruff check .",
        ),
        source="config.verify_commands_generic_preset",
    )


def test_quoted_windows_python_exe_generic_preset_is_not_authoritative_for_managed_resolution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=[
            r'"C:\Program Files\Python311\python.exe" -m pytest -q',
            r'"C:\Program Files\Python311\python.exe" -m ruff check .',
        ],
    )

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=(
            r'"C:\Program Files\Python311\python.exe" -m pytest -q',
            r'"C:\Program Files\Python311\python.exe" -m ruff check .',
        ),
        source="config.verify_commands_generic_preset",
    )


def test_generic_configured_verify_preset_still_allows_repo_inference(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("npm test",),
        source="repo_scan.likely_test_commands",
    )


def test_pytest_plus_plain_ruff_check_still_allows_repo_inference(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check"])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("npm test",),
        source="repo_scan.likely_test_commands",
    )


def test_repo_inference_beats_uv_run_pytest_generic_preset_in_js_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "ruff check"])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("npm test",),
        source="repo_scan.likely_test_commands",
    )


def test_repo_inference_beats_uv_run_pytest_and_uv_run_ruff_check_generic_preset_in_js_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "uv run ruff check"])

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("npm test",),
        source="repo_scan.likely_test_commands",
    )


def test_repo_inference_beats_module_invoked_generic_preset_in_js_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    cfg = AppConfig(
        model="test-model",
        verify_commands=["uv run python -m pytest -q", "uv run python -m ruff check ."],
    )

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("npm test",),
        source="repo_scan.likely_test_commands",
    )


def test_repo_inference_beats_windows_launcher_generic_preset_in_js_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    cfg = AppConfig(
        model="test-model",
        verify_commands=[
            r"C:\Python311\python.exe -m pytest -q",
            r"C:\Python311\python.exe -m ruff check .",
        ],
    )

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=("npm test",),
        source="repo_scan.likely_test_commands",
    )


@pytest.mark.parametrize(
    "commands",
    [
        ["make verify"],
        ["pnpm --dir packages/web test"],
        ["PYTHONPATH=src pytest -q"],
        ["pytest tests/api/test_users.py -q"],
        ["cargo test"],
        ["go test ./..."],
    ],
)
def test_explicit_repo_specific_config_verify_commands_remain_authoritative(
    tmp_path: Path,
    commands: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    cfg = AppConfig(model="test-model", verify_commands=commands)

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=tuple(commands),
        source="config.verify_commands",
    )


@pytest.mark.parametrize(
    "commands",
    [
        ["ruff check app.py"],
        ["ruff check src tests"],
        ["ruff check --config pyproject.toml ."],
        ["ruff check --select F401 ."],
        ["ruff check src/app.py"],
    ],
)
def test_targeted_or_flagged_ruff_config_verify_commands_remain_authoritative(
    tmp_path: Path,
    commands: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=commands)

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=tuple(commands),
        source="config.verify_commands",
    )


@pytest.mark.parametrize(
    "commands",
    [
        ["uv run pytest tests/api/test_users.py -q"],
        ["poetry run pytest -m smoke -q"],
        ["pipenv run pytest tests/custom.py -q"],
        ["uv run PYTHONPATH=src pytest -q"],
    ],
)
def test_targeted_or_env_runner_prefixed_pytest_commands_remain_authoritative(
    tmp_path: Path,
    commands: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=commands)

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=tuple(commands),
        source="config.verify_commands",
    )


@pytest.mark.parametrize(
    "commands",
    [
        ["uv run ruff check src/app.py"],
        ["poetry run ruff check src tests"],
        ["pipenv run ruff check --config pyproject.toml ."],
        ["uv run ruff check --select F401 ."],
    ],
)
def test_targeted_runner_prefixed_ruff_commands_remain_authoritative(
    tmp_path: Path,
    commands: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=commands)

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=tuple(commands),
        source="config.verify_commands",
    )


@pytest.mark.parametrize(
    "commands",
    [
        ["python -m pytest tests/api/test_users.py -q"],
        [r"C:\Python311\python.exe -m pytest tests/api/test_users.py -q"],
        [r'"C:\Program Files\Python311\python.exe" -m pytest -m smoke -q'],
        ["py -m pytest tests/custom.py -q"],
        ["python -m pytest -m smoke -q"],
        ["python -m ruff check src/app.py"],
        [r"C:\Python311\python.exe -m ruff check src/app.py"],
        [r"C:\Python311\python.exe -m ruff check src tests"],
        [r"C:\Python311\python.exe -m ruff check --config pyproject.toml ."],
        ["python -m ruff check src tests"],
        ["python -m ruff check --config pyproject.toml ."],
        ["python -m ruff check --select F401 ."],
        ["uv run python -m ruff check src/app.py"],
        [r'uv run "C:\Program Files\Python311\python.exe" -m pytest tests/custom.py -q'],
        ["uv run py -m pytest tests/api/test_users.py -q"],
    ],
)
def test_targeted_module_invoked_commands_remain_authoritative(
    tmp_path: Path,
    commands: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=commands)

    resolved = resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)

    assert resolved == ResolvedVerifyCommands(
        commands=tuple(commands),
        source="config.verify_commands",
    )


def test_verify_command_resolution_keeps_empty_config_as_error_even_when_repo_is_inferable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "packages" / "web").mkdir(parents=True)
    (repo / "packages" / "web" / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )

    cfg = AppConfig(model="test-model")
    cfg.verify_commands = []

    with pytest.raises(VerifyError, match="Configured verify_commands is empty."):
        resolve_verify_command_selection(cfg=cfg, verify_cmd=None, root=repo)


def test_verify_command_resolution_can_treat_empty_config_as_unavailable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model")
    cfg.verify_commands = []

    resolved = resolve_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        allow_empty_config=True,
    )

    assert resolved.commands == ()
    assert resolved.source == "repo_scan.no_authoritative_commands"
    assert resolved.contract_type == "unavailable"


def test_execution_semantics_normalization_aligns_module_invoked_pytest_and_ruff_forms() -> None:
    assert _normalize_execution_semantics_parts("python -m ruff check .") == ["ruff", "check", "."]
    assert _normalize_execution_semantics_parts("python3 -m ruff check") == ["ruff", "check"]
    assert _normalize_execution_semantics_parts("py -m pytest -q") == ["pytest", "-q"]
    assert _normalize_execution_semantics_parts("py -m ruff check .") == ["ruff", "check", "."]
    assert _normalize_execution_semantics_parts("uv run py -m pytest -q") == ["pytest", "-q"]
    assert _normalize_execution_semantics_parts("uv run py -m ruff check .") == [
        "ruff",
        "check",
        ".",
    ]
    assert _normalize_execution_semantics_parts(r"C:\Python311\python.exe -m pytest -q") == [
        "pytest",
        "-q",
    ]
    assert _normalize_execution_semantics_parts(r"C:\Python311\python.exe -m ruff check .") == [
        "ruff",
        "check",
        ".",
    ]
    assert _normalize_execution_semantics_parts(
        r'"C:\Program Files\Python311\python.exe" -m pytest -q'
    ) == ["pytest", "-q"]
    assert _normalize_execution_semantics_parts(
        r'uv run "C:\Program Files\Python311\python.exe" -m ruff check .'
    ) == ["ruff", "check", "."]
    assert _normalize_execution_semantics_parts(r"C:/Python311/python.exe -m pytest -q") == [
        "pytest",
        "-q",
    ]
    assert _normalize_execution_semantics_parts(r"C:/Python311/python.exe -m ruff check .") == [
        "ruff",
        "check",
        ".",
    ]

    assert _verification_family_for_result("python -m ruff check .") == "ruff:check"
    assert _verification_family_for_result("python3 -m ruff check .") == "ruff:check"
    assert _verification_family_for_result("py -m pytest -q") == "pytest"
    assert _verification_family_for_result("py -m ruff check .") == "ruff:check"
    assert _verification_family_for_result("uv run py -m pytest -q") == "pytest"
    assert _verification_family_for_result("uv run py -m ruff check .") == "ruff:check"
    assert _verification_family_for_result(r"C:\Python311\python.exe -m pytest -q") == "pytest"
    assert (
        _verification_family_for_result(r"C:\Python311\python.exe -m ruff check .") == "ruff:check"
    )
    assert (
        _verification_family_for_result(r'"C:\Program Files\Python311\python.exe" -m pytest -q')
        == "pytest"
    )
    assert (
        _verification_family_for_result(
            r'uv run "C:\Program Files\Python311\python.exe" -m ruff check .'
        )
        == "ruff:check"
    )


def test_verify_command_refinement_replaces_generic_fallback_with_node_test_for_js_tasks() -> None:
    selection = ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )

    refined = refine_generic_fallback_verify_command_selection(
        selection=selection,
        task={
            "estimated_files": ["test/app.test.js", "src/app.js"],
            "write_scope": ["test/app.test.js", "src/app.js"],
            "acceptance_criteria": ["Keep the Node test green."],
        },
    )

    assert refined == ResolvedVerifyCommands(
        commands=("node --test",),
        source="task_refinement.node_test",
    )


def test_task_aware_verify_resolution_refines_generic_configured_preset_to_node_test(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["test/app.test.js", "src/app.js"],
            "write_scope": ["test/app.test.js", "src/app.js"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=("node --test",),
        source="task_refinement.node_test",
    )


def test_verify_command_refinement_suppresses_generic_fallback_for_js_bootstrap_without_tests() -> (
    None
):
    selection = ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )

    refined = refine_generic_fallback_verify_command_selection(
        selection=selection,
        task={
            "estimated_files": ["package.json", "src/index.js"],
            "write_scope": ["package.json", "src/index.js"],
            "acceptance_criteria": ["Bootstrap the package layout."],
        },
    )

    assert refined == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_task_aware_verify_resolution_returns_no_authoritative_for_js_bootstrap_under_generic_preset(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["package.json", "src/index.js"],
            "write_scope": ["package.json", "src/index.js"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_task_aware_verify_resolution_returns_no_authoritative_for_docs_only_task_under_generic_preset(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README.md", "docs/usage.md"],
            "write_scope": ["README.md", "docs/usage.md"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )
    assert resolved.contract_type == "unavailable"
    assert resolved.reason == "docs-only task does not expose a confident verification command"


def test_task_aware_verify_resolution_suppresses_generic_pytest_for_static_artifact(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.html").write_text("<h1>Demo</h1>\n", encoding="utf-8")
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["output.txt"],
            "write_scope": ["output.txt"],
            "acceptance_criteria": ["Create the requested static output artifact."],
        },
    )

    assert resolved.commands == ()
    assert resolved.contract_type == "unavailable"


def test_task_aware_verify_resolution_uses_doctest_for_docs_task_that_requests_it(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("Example\n=======\n", encoding="utf-8")
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README"],
            "write_scope": ["README"],
            "acceptance_criteria": ["python -m doctest README.md passes"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(
            shlex.join([sys.executable, "-m", "doctest", "README.md"]),
            shlex.join(
                [sys.executable, "-m", "pytest", "--doctest-glob=README.md", "-q", "README.md"]
            ),
        ),
        source="task_refinement.doctest",
    )
    assert resolved.contract_type == "task_inferred"


def test_task_aware_verify_resolution_uses_explicit_pytest_command_from_task_text(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model")

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["calc.py", "test_calc.py", "README.md"],
            "write_scope": ["calc.py", "test_calc.py", "README.md"],
            "acceptance_criteria": ["Running `python -m pytest test_calc.py` passes."],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(shlex.join([sys.executable, "-m", "pytest", "test_calc.py"]),),
        source="task_refinement.explicit_pytest",
    )
    assert resolved.contract_type == "task_inferred"


def test_task_aware_verify_resolution_prefers_explicit_pytest_doctest_command(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README.md"],
            "write_scope": ["README.md"],
            "acceptance_criteria": ["`pytest --doctest-glob=README.md -q` passes"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest --doctest-glob=README.md -q",),
        source="task_refinement.explicit_pytest",
    )


def test_docs_only_task_under_pytest_plus_plain_ruff_check_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README.md", "docs/usage.md"],
            "write_scope": ["README.md", "docs/usage.md"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_js_bootstrap_task_under_pytest_plus_plain_ruff_check_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["package.json", "src/index.js"],
            "write_scope": ["package.json", "src/index.js"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_docs_only_task_under_uv_run_pytest_and_ruff_check_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "ruff check"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README.md", "docs/usage.md"],
            "write_scope": ["README.md", "docs/usage.md"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_js_bootstrap_task_under_uv_run_pytest_and_ruff_check_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "ruff check"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["package.json", "src/index.js"],
            "write_scope": ["package.json", "src/index.js"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_docs_only_task_under_uv_run_pytest_and_uv_run_ruff_check_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "uv run ruff check"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README.md", "docs/usage.md"],
            "write_scope": ["README.md", "docs/usage.md"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_js_bootstrap_task_under_uv_run_pytest_and_uv_run_ruff_check_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["uv run pytest -q", "uv run ruff check"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["package.json", "src/index.js"],
            "write_scope": ["package.json", "src/index.js"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_docs_only_task_under_uv_run_python_m_pytest_and_ruff_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=["uv run python -m pytest -q", "uv run python -m ruff check ."],
    )

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README.md", "docs/usage.md"],
            "write_scope": ["README.md", "docs/usage.md"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_js_bootstrap_task_under_uv_run_python_m_pytest_and_ruff_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=["uv run python -m pytest -q", "uv run python -m ruff check ."],
    )

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["package.json", "src/index.js"],
            "write_scope": ["package.json", "src/index.js"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_docs_only_task_under_windows_launcher_generic_preset_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=[
            r"C:\Python311\python.exe -m pytest -q",
            r"C:\Python311\python.exe -m ruff check .",
        ],
    )

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["README.md", "docs/usage.md"],
            "write_scope": ["README.md", "docs/usage.md"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_js_bootstrap_task_under_windows_launcher_generic_preset_resolves_to_no_authoritative_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(
        model="test-model",
        verify_commands=[
            r"C:\Python311\python.exe -m pytest -q",
            r"C:\Python311\python.exe -m ruff check .",
        ],
    )

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["package.json", "src/index.js"],
            "write_scope": ["package.json", "src/index.js"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_task_aware_verify_resolution_does_not_treat_text_fixtures_as_docs_only(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["tests/fixtures/cases.txt", "src/prompts/system.md"],
            "write_scope": ["tests/fixtures/cases.txt", "src/prompts/system.md"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest -q", "ruff check ."),
        source="config.verify_commands_generic_preset",
    )


def test_task_aware_verify_resolution_returns_no_authoritative_for_ci_only_task_under_generic_preset(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": [".github/workflows/ci.yml"],
            "write_scope": [".github/workflows/ci.yml"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )
    assert (
        resolved.reason
        == "CI-only task does not expose a confident repo-native verification command"
    )
    assert resolved.contract_type == "unavailable"


def test_task_aware_verify_resolution_returns_no_authoritative_for_terraform_task_under_generic_preset(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["infra/main.tf", "infra/terraform.tfvars"],
            "write_scope": ["infra/main.tf", "infra/terraform.tfvars"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )
    assert (
        resolved.reason
        == "Terraform/Compose task does not expose a confident repo-native verification command"
    )
    assert resolved.contract_type == "unavailable"


def test_task_aware_verify_resolution_returns_no_authoritative_for_compose_task_under_generic_preset(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["docker-compose.yml"],
            "write_scope": ["docker-compose.yml"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )
    assert (
        resolved.reason
        == "Terraform/Compose task does not expose a confident repo-native verification command"
    )
    assert resolved.contract_type == "unavailable"


def test_task_aware_verify_resolution_returns_no_authoritative_for_python_repo_without_tests(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def run() -> None:\n    return None\n", encoding="utf-8")
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["src/app.py"],
            "write_scope": ["src/app.py"],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )
    assert (
        resolved.reason
        == "Python task has no discoverable test surface, so generic pytest is not trusted"
    )
    assert resolved.contract_type == "unavailable"


@pytest.mark.parametrize(
    ("files", "instruction", "expected_reason"),
    [
        (
            {
                "README.md": "# Demo\n",
            },
            "Fix the bug.",
            "repo scan found a docs-only workspace with no authoritative verification surface",
        ),
        (
            {
                "index.html": "<!doctype html><title>Demo</title>\n",
                "styles.css": "body { font-family: sans-serif; }\n",
                "assets/logo.svg": "<svg></svg>\n",
            },
            "Fix the bug.",
            "repo scan found a static web workspace with no authoritative verification surface",
        ),
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
                "src/index.ts": "export const value = 1;\n",
            },
            "Update the auth flow.",
            "repo scan found a JS/Node workspace without a real repo-native test command",
        ),
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
                "src/index.ts": "export const value = 1;\n",
            },
            "Fix the bug.",
            "repo scan found a JS/Node workspace without a real repo-native test command",
        ),
        (
            {
                ".github/workflows/ci.yml": (
                    "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                )
            },
            "Fix the bug.",
            "repo scan found a CI-only workspace with no authoritative verification surface",
        ),
        (
            {
                "versions.tf": 'terraform {\n  required_version = ">= 1.6.0"\n}\n',
            },
            "Adjust the policy.",
            "repo scan found a Terraform-only workspace with no authoritative verification surface",
        ),
        (
            {
                "compose.yaml": "services:\n  web:\n    image: nginx:latest\n",
            },
            "Adjust startup order.",
            "repo scan found a Compose-only workspace with no authoritative verification surface",
        ),
        (
            {
                "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
                "src/demo/uploads.py": "def handle_upload() -> None:\n    return None\n",
            },
            "Refactor the parser.",
            "repo scan found a Python workspace without a discoverable test surface",
        ),
        (
            {
                "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
                "src/demo/uploads.py": "def handle_upload() -> None:\n    return None\n",
            },
            "Fix the bug.",
            "repo scan found a Python workspace without a discoverable test surface",
        ),
    ],
)
def test_task_aware_verify_resolution_suppresses_generic_fallback_for_pathless_prompts(
    tmp_path: Path,
    files: dict[str, str],
    instruction: str,
    expected_reason: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(repo, files)
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": [instruction]},
    )

    if "package.json" in files:
        assert resolved == ResolvedVerifyCommands(
            commands=("npm run build",),
            source="repo_scan.likely_test_commands",
        )
        assert resolved.contract_type == "selected"
        assert not is_authoritative_verify_command_selection(resolved)
        assert verify_gate_mod.required_verify_commands(resolved) == ()
        assert "pytest" not in " ".join(resolved.commands)
        return

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="repo_scan.no_authoritative_commands",
    )
    assert resolved.reason == expected_reason
    assert resolved.contract_type == "unavailable"


@pytest.mark.parametrize(
    ("files", "instruction", "task_paths", "expected_reason"),
    [
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
                ".env.example": "API_URL=\n",
            },
            "Update .env.example handling.",
            [".env.example"],
            "repo scan found a JS/Node workspace without a real repo-native test command",
        ),
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
                ".npmrc": "save-exact=true\n",
            },
            "Adjust .npmrc defaults.",
            [".npmrc"],
            "repo scan found a JS/Node workspace without a real repo-native test command",
        ),
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
                "vercel.json": '{\n  "framework": "vite"\n}\n',
            },
            "Update vercel.json config.",
            ["vercel.json"],
            "repo scan found a JS/Node workspace without a real repo-native test command",
        ),
        (
            {
                "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
                ".env.example": "UPLOAD_DIR=\n",
                "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
            },
            "Update .env.example defaults.",
            [".env.example"],
            "repo scan found a Python workspace without a discoverable test surface",
        ),
    ],
)
def test_task_aware_verify_resolution_keeps_repo_grounded_invalidation_for_neutral_config_paths(
    tmp_path: Path,
    files: dict[str, str],
    instruction: str,
    task_paths: list[str],
    expected_reason: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(repo, files)
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": list(task_paths),
            "write_scope": list(task_paths),
            "acceptance_criteria": [instruction],
        },
    )

    if "package.json" in files:
        assert resolved == ResolvedVerifyCommands(
            commands=("npm run build",),
            source="repo_scan.likely_test_commands",
        )
        assert resolved.contract_type == "selected"
        assert not is_authoritative_verify_command_selection(resolved)
        assert verify_gate_mod.required_verify_commands(resolved) == ()
        assert "pytest" not in " ".join(resolved.commands)
        return

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="repo_scan.no_authoritative_commands",
    )
    assert resolved.reason == expected_reason
    assert resolved.contract_type == "unavailable"


@pytest.mark.parametrize(
    ("instruction", "expected_reason"),
    [
        (
            "Fix the bug.",
            "repo scan found a mixed workspace without an authoritative verification surface",
        ),
        (
            "Update the auth flow.",
            "repo scan found a mixed workspace without an authoritative verification surface",
        ),
        (
            "Refactor the parser.",
            "repo scan found a mixed workspace without an authoritative verification surface",
        ),
    ],
)
def test_task_aware_verify_resolution_suppresses_generic_fallback_for_mixed_workspace_vague_prompts(
    tmp_path: Path,
    instruction: str,
    expected_reason: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
            "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
            "src/index.ts": "export const value = 1;\n",
            "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
        },
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": [instruction]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="repo_scan.no_authoritative_commands",
    )
    assert resolved.reason == expected_reason
    assert resolved.contract_type == "unavailable"


def test_task_aware_verify_resolution_keeps_repo_grounded_invalidation_for_mixed_workspace_neutral_config_path(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
            "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
            ".env.example": "API_URL=\nUPLOAD_DIR=\n",
            "src/index.ts": "export const value = 1;\n",
            "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
        },
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": [".env.example"],
            "write_scope": [".env.example"],
            "acceptance_criteria": ["Update .env.example defaults."],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="repo_scan.no_authoritative_commands",
    )
    assert (
        resolved.reason
        == "repo scan found a mixed workspace without an authoritative verification surface"
    )
    assert resolved.contract_type == "unavailable"


@pytest.mark.parametrize(
    ("files", "expected_commands"),
    [
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"test":"vitest run"}}\n',
                "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
                "src/index.ts": "export const value = 1;\n",
                "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
            },
            ("npm test",),
        ),
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
                "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
                "src/index.ts": "export const value = 1;\n",
                "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
                "tests/test_app.py": "def test_placeholder() -> None:\n    assert True\n",
            },
            ("pytest -q",),
        ),
    ],
)
def test_task_aware_verify_resolution_keeps_recommendations_for_mixed_workspaces(
    tmp_path: Path,
    files: dict[str, str],
    expected_commands: tuple[str, ...],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(repo, files)
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": ["Fix the bug."]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=expected_commands,
        source="repo_scan.likely_test_commands",
    )
    assert resolved.contract_type == "selected"
    assert not is_authoritative_verify_command_selection(resolved)
    assert verify_gate_mod.required_verify_commands(resolved) == ()
    assert (
        resolved.reason == "repo scan suggested verification commands from the existing workspace"
    )


@pytest.mark.parametrize(
    ("instruction", "task_paths", "expected_reason"),
    [
        (
            "Update src/index.ts auth handling.",
            ["src/index.ts"],
            "frontend/JS task should not inherit a generic Python verification fallback",
        ),
        (
            "Refactor src/demo/app.py.",
            ["src/demo/app.py"],
            "Python task has no discoverable test surface, so generic pytest is not trusted",
        ),
    ],
)
def test_task_aware_verify_resolution_keeps_task_specific_no_authoritative_behavior_in_mixed_workspaces(
    tmp_path: Path,
    instruction: str,
    task_paths: list[str],
    expected_reason: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
            "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
            "src/index.ts": "export const value = 1;\n",
            "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
        },
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": list(task_paths),
            "write_scope": list(task_paths),
            "acceptance_criteria": [instruction],
        },
    )

    if task_paths == ["src/index.ts"]:
        assert resolved == ResolvedVerifyCommands(
            commands=("npm run build",),
            source="repo_scan.likely_test_commands",
        )
        assert resolved.contract_type == "selected"
        assert not is_authoritative_verify_command_selection(resolved)
        assert verify_gate_mod.required_verify_commands(resolved) == ()
        assert "pytest" not in " ".join(resolved.commands)
        return

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )
    assert resolved.reason == expected_reason
    assert resolved.contract_type == "unavailable"


@pytest.mark.parametrize(
    ("instruction", "task_paths"),
    [
        ("Update package.json.", ["package.json"]),
        ("Update tsconfig.json.", ["tsconfig.json"]),
    ],
)
def test_task_aware_verify_resolution_keeps_js_bootstrap_paths_non_authoritative(
    tmp_path: Path,
    instruction: str,
    task_paths: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
            "tsconfig.json": '{\n  "compilerOptions": {\n    "strict": true\n  }\n}\n',
            "src/index.ts": "export const value = 1;\n",
        },
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": list(task_paths),
            "write_scope": list(task_paths),
            "acceptance_criteria": [instruction],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=("npm run build",),
        source="repo_scan.likely_test_commands",
    )
    assert resolved.contract_type == "selected"
    assert not is_authoritative_verify_command_selection(resolved)
    assert verify_gate_mod.required_verify_commands(resolved) == ()
    assert "pytest" not in " ".join(resolved.commands)


def test_task_aware_verify_resolution_suppresses_generic_pytest_for_static_html_task(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "index.html": "<!doctype html><title>Demo</title>\n",
            "styles.css": "body { font-family: sans-serif; }\n",
        },
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={
            "estimated_files": ["index.html", "styles.css"],
            "write_scope": ["index.html", "styles.css"],
            "acceptance_criteria": ["Build a static HTML timer page."],
        },
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )
    assert resolved.reason == "static web task does not expose a confident verification command"
    assert resolved.contract_type == "unavailable"


@pytest.mark.parametrize(
    ("files", "instruction", "expected_commands"),
    [
        (
            {
                "package.json": '{"name":"demo-web","scripts":{"test":"vitest run"}}\n',
                "src/index.ts": "export const value = 1;\n",
            },
            "Fix the bug.",
            ("npm test",),
        ),
        (
            {
                "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\n",
                "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
                "tests/test_app.py": "def test_placeholder() -> None:\n    assert True\n",
            },
            "Fix the bug.",
            ("pytest -q",),
        ),
    ],
)
def test_task_aware_verify_resolution_keeps_recommendations_for_pathless_prompts(
    tmp_path: Path,
    files: dict[str, str],
    instruction: str,
    expected_commands: tuple[str, ...],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(repo, files)
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": [instruction]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=expected_commands,
        source="repo_scan.likely_test_commands",
    )
    assert resolved.contract_type == "selected"
    assert not is_authoritative_verify_command_selection(resolved)
    assert verify_gate_mod.required_verify_commands(resolved) == ()
    assert (
        resolved.reason == "repo scan suggested verification commands from the existing workspace"
    )


def test_task_aware_verify_resolution_uses_node_build_and_lint_scripts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "package.json": (
                '{"name":"demo-web","scripts":{"build":"next build","lint":"next lint"}}\n'
            ),
            "src/index.ts": "export const value = 1;\n",
        },
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": ["Fix the bug."]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=("npm run build", "npm run lint"),
        source="repo_scan.likely_test_commands",
    )
    assert resolved.contract_type == "selected"
    assert not is_authoritative_verify_command_selection(resolved)
    assert verify_gate_mod.required_verify_commands(resolved) == ()
    assert "pytest" not in " ".join(resolved.commands)


def test_task_aware_verify_resolution_returns_unavailable_for_node_repo_without_scripts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "package.json": '{"name":"demo-web","scripts":{"test":"echo no test specified"}}\n',
            "src/index.ts": "export const value = 1;\n",
        },
    )
    cfg = AppConfig(model="test-model", verify_commands=["pytest -q"])

    resolved = resolve_task_aware_verify_command_selection(
        cfg=cfg,
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": ["Fix the bug."]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="repo_scan.no_authoritative_commands",
    )
    assert resolved.contract_type == "unavailable"


def test_verify_command_refinement_does_not_override_explicit_cli_or_config_commands() -> None:
    task = {
        "estimated_files": ["test/app.test.js", "src/app.js"],
        "write_scope": ["test/app.test.js", "src/app.js"],
        "acceptance_criteria": ["Use node --test for verification."],
    }

    configured = refine_generic_fallback_verify_command_selection(
        selection=ResolvedVerifyCommands(
            commands=("make verify",),
            source="config.verify_commands",
        ),
        task=task,
    )
    cli = refine_generic_fallback_verify_command_selection(
        selection=ResolvedVerifyCommands(
            commands=("pnpm --dir packages/web test",),
            source="cli.verify_cmd",
        ),
        task=task,
    )

    assert configured == ResolvedVerifyCommands(
        commands=("make verify",),
        source="config.verify_commands",
    )
    assert cli == ResolvedVerifyCommands(
        commands=("pnpm --dir packages/web test",),
        source="cli.verify_cmd",
    )


def test_task_aware_plain_non_python_directory_gets_no_generic_pytest(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "output.txt").write_text("existing\n", encoding="utf-8")

    resolved = resolve_task_aware_verify_command_selection(
        cfg=AppConfig(model="test-model"),
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": ["Create result.txt."]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="repo_scan.no_authoritative_commands",
    )
    assert resolved.contract_type == "unavailable"
    assert "generic pytest fallback" in resolved.reason


def test_task_aware_existing_python_pytest_surface_keeps_pytest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "src/demo.py": "def value() -> int:\n    return 1\n",
            "tests/test_demo.py": "def test_value() -> None:\n    assert True\n",
        },
    )

    resolved = resolve_task_aware_verify_command_selection(
        cfg=AppConfig(model="test-model"),
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": ["Fix the Python behavior."]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="repo_scan.likely_test_commands",
    )


def test_agent_created_test_file_does_not_retroactively_establish_contract(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "demo.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    pre_turn_scan = scan_workspace(context=resolve_workspace_context(repo))
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text(
        "def test_generated() -> None:\n    assert True\n",
        encoding="utf-8",
    )

    resolved = resolve_task_aware_verify_command_selection(
        cfg=AppConfig(model="test-model"),
        verify_cmd=None,
        root=repo,
        repo_scan=pre_turn_scan,
        task={"acceptance_criteria": ["Fix the Python behavior."]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=(),
        source="repo_scan.no_authoritative_commands",
    )
    assert resolved.contract_type == "unavailable"


def test_non_python_repo_with_make_check_selects_project_native_command(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_files(
        repo,
        {
            "Makefile": "check:\n\tcc -Wall main.c -o main && ./main\n",
            "main.c": "int main(void) { return 0; }\n",
        },
    )

    resolved = resolve_task_aware_verify_command_selection(
        cfg=AppConfig(model="test-model"),
        verify_cmd=None,
        root=repo,
        task={"acceptance_criteria": ["Fix the C checker."]},
    )

    assert resolved == ResolvedVerifyCommands(
        commands=("make check",),
        source="repo_scan.likely_test_commands",
    )
    assert resolved.contract_type == "selected"
    assert not is_authoritative_verify_command_selection(resolved)
    assert verify_gate_mod.required_verify_commands(resolved) == ()


def test_verify_command_refinement_prefers_node_test_targets_over_pytest_text_mentions() -> None:
    selection = ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )

    refined = refine_generic_fallback_verify_command_selection(
        selection=selection,
        task={
            "estimated_files": ["test/app.test.js", "src/app.js"],
            "write_scope": ["test/app.test.js", "src/app.js"],
            "acceptance_criteria": ["Use node --test instead of pytest -q for verification."],
        },
    )

    assert refined == ResolvedVerifyCommands(
        commands=("node --test",),
        source="task_refinement.node_test",
    )


def test_verify_command_refinement_uses_acceptance_text_for_js_source_only_tasks() -> None:
    selection = ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )

    refined = refine_generic_fallback_verify_command_selection(
        selection=selection,
        task={
            "estimated_files": ["src/index.js"],
            "write_scope": ["src/index.js"],
            "acceptance_criteria": ["Use node --test for test verification."],
        },
    )

    assert refined == ResolvedVerifyCommands(
        commands=("node --test",),
        source="task_refinement.node_test",
    )


def test_verify_command_refinement_uses_plan_requirements_for_js_source_only_tasks() -> None:
    selection = ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )

    refined = refine_generic_fallback_verify_command_selection(
        selection=selection,
        task={
            "estimated_files": ["src/index.js"],
            "write_scope": ["src/index.js"],
        },
        plan_requirements=["Use node --test for test verification."],
    )

    assert refined == ResolvedVerifyCommands(
        commands=("node --test",),
        source="task_refinement.node_test",
    )


def test_verify_command_refinement_does_not_override_python_task_from_node_test_text() -> None:
    selection = ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )

    refined = refine_generic_fallback_verify_command_selection(
        selection=selection,
        task={
            "estimated_files": ["src/app.py", "tests/test_app.py"],
            "write_scope": ["src/app.py", "tests/test_app.py"],
            "acceptance_criteria": ["Use node --test for test verification."],
        },
        plan_requirements=["Use node --test for test verification."],
    )

    assert refined == selection


def test_verify_command_refinement_ignores_negated_node_test_instruction() -> None:
    selection = ResolvedVerifyCommands(
        commands=("pytest -q",),
        source="config.verify_commands_fallback",
    )

    refined = refine_generic_fallback_verify_command_selection(
        selection=selection,
        task={
            "estimated_files": ["src/index.js"],
            "write_scope": ["src/index.js"],
            "acceptance_criteria": ["Don't use node --test for verification here."],
        },
        plan_requirements=["Do not use node --test for verification here."],
    )

    assert refined == ResolvedVerifyCommands(
        commands=(),
        source="task_refinement.no_authoritative_commands",
    )


def test_verification_selection_payload_marks_repo_native_selection_as_authoritative() -> None:
    selection = ResolvedVerifyCommands(
        commands=("pytest tests/api/test_users.py -q",),
        source="config.verify_commands",
        reason="repo-specific verify_commands configuration is authoritative",
        contract_type="repo_native",
    )

    payload = verification_selection_payload(
        selection,
        authoritative=is_authoritative_verify_command_selection(selection),
    )

    assert payload == {
        "verification_selection_source": "config.verify_commands",
        "verification_selection_reason": (
            "repo-specific verify_commands configuration is authoritative"
        ),
        "verification_contract_type": "repo_native",
        "verification_authoritative": True,
        "verification_best_effort": False,
    }


def test_verification_selection_payload_marks_task_inferred_selection_as_non_authoritative() -> (
    None
):
    selection = ResolvedVerifyCommands(
        commands=("node --test",),
        source="task_refinement.node_test",
        reason="task-aware refinement preferred node --test over a generic Python fallback",
        contract_type="task_inferred",
    )

    payload = verification_selection_payload(
        selection,
        authoritative=is_authoritative_verify_command_selection(selection),
    )

    assert payload == {
        "verification_selection_source": "task_refinement.node_test",
        "verification_selection_reason": (
            "task-aware refinement preferred node --test over a generic Python fallback"
        ),
        "verification_contract_type": "task_inferred",
        "verification_authoritative": False,
        "verification_best_effort": False,
    }


def test_verify_sandbox_mode_env_overrides_config(monkeypatch) -> None:
    cfg = AppConfig(model="test-model")
    cfg.extra_fields = {"verify_sandbox": {"mode": "warn"}}
    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "strict")
    assert resolve_verify_sandbox_mode(cfg) == "strict"


def test_resolve_verify_sandbox_mode_defaults_to_strict() -> None:
    assert resolve_verify_sandbox_mode(AppConfig(model="test-model")) == "strict"


def test_verify_sandbox_mode_off_allows_explicit_host_execution(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(cmd))
        return _cp(returncode=0, stdout="ok via host\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "explicit_off.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="off"),
    )

    assert result.all_passed is True
    assert calls == ["pytest -q"]
    assert "ok via host" in artifact.read_text(encoding="utf-8")


def test_verify_gate_can_use_docker_runner_when_enabled(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []

    monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "warn")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_NETWORK", "off")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE", "test/alysis-sandbox:dev")
    monkeypatch.setattr(
        sandbox_runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Linux")

    class FakePopen:
        def __init__(self, cmd, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            if not (isinstance(cmd, list) and cmd and cmd[0] == "docker"):
                raise AssertionError(f"unexpected command invocation: {cmd!r}")
            self.returncode: int | None = None

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            self.returncode = 0
            return "ok\n", ""

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: int | None = None) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    artifact = tmp_path / "verify" / "T03.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=AppConfig(model="test-model"),
    )
    assert result.all_passed is True
    assert any(isinstance(cmd, list) and cmd and cmd[0] == "docker" for cmd in calls)
    body = artifact.read_text(encoding="utf-8")
    assert "exit_code: 0" in body
    assert "ok" in body


def test_verify_gate_default_strict_without_backend_records_failure(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(cmd))
        raise AssertionError("host subprocess should not run when verify sandbox is unavailable")

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "docker")
    # Windows platform discovery may invoke `ver`; this test observes execution,
    # not OS discovery, so make that independent of the machine running it.
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T04.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(),
    )
    assert result.all_passed is False
    assert calls == []
    body = artifact.read_text(encoding="utf-8")
    assert "exit_code: 127" in body
    assert "verify sandbox unavailable" in body


def test_verify_gate_warn_without_backend_fails_closed_without_host_subprocess(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(cmd))
        raise AssertionError(
            "host subprocess should not run in warn mode when backend is unavailable"
        )

    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "docker")
    monkeypatch.setattr(sandbox_runner_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sandbox_runner_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = tmp_path / "verify" / "T04_warn.txt"
    result = run_task_verification(
        root=tmp_path,
        commands=["pytest -q"],
        artifact_path=artifact,
        cfg=_verify_cfg(mode="warn"),
    )
    assert result.all_passed is False
    assert calls == []
    body = artifact.read_text(encoding="utf-8")
    assert "exit_code: 127" in body
    assert "host fallback is disabled" in body


def test_verify_run_payload_uses_repo_relative_artifact_path_under_root(tmp_path: Path) -> None:
    artifact = tmp_path / "sessions" / "sid" / "verify" / "step001_verify_run.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("ok\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest -q"],
        command_results=[VerifyCommandResult(command="pytest -q", exit_code=0, output="ok\n")],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=tmp_path, result=result)
    artifact_ref = resolve_verify_artifact_payload(root=tmp_path, artifact_path=artifact)

    assert payload["artifact_path"] == "sessions/sid/verify/step001_verify_run.txt"
    assert payload["artifact_saved"] is True
    assert payload["artifact_readable_via_fs"] is True
    assert payload["artifact_location"] == "workspace_root"
    assert artifact_ref.artifact_path == payload["artifact_path"]


def test_verify_run_payload_hides_external_artifact_path_from_model(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = tmp_path / "runtime" / "sessions" / "sid" / "verify" / "step001_verify_run.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("failure\n", encoding="utf-8")
    result = VerifyRunResult(
        commands=["pytest -q"],
        command_results=[
            VerifyCommandResult(command="pytest -q", exit_code=1, output="assertion failed\n")
        ],
        artifact_path=artifact,
    )

    payload = verify_run_result_to_payload(root=repo, result=result)

    assert payload["artifact_path"] is None
    assert payload["artifact_saved"] is True
    assert payload["artifact_readable_via_fs"] is False
    assert payload["artifact_location"] == "external_session_store"
    assert payload["fallback_used"] is False
    assert payload["fallback_count"] == 0
    command_payload = payload["command_results"][0]
    assert command_payload["command"] == "pytest -q"
    assert command_payload["effective_command"] == "pytest -q"
    assert command_payload["exit_code"] == 1
    assert command_payload["status"] == "failed"
    assert command_payload["ok"] is False
    assert command_payload["real_execution"] is None
    assert command_payload["output_preview"] == "assertion failed\n"
    assert command_payload["output_chars"] == 17
    assert command_payload["output_truncated"] is False
    assert command_payload["fallback_used"] is False
    assert command_payload["failure_summary"]["primary_error"] == "assertion failed"


def test_verification_rejection_guidance_maps_reasons_to_fixes() -> None:
    from alysis_code.verification_command_analysis import verification_rejection_guidance

    pipe = verification_rejection_guidance("unsafe_pipeline")
    assert "pipeline" in pipe.lower()
    assert "not a failure of your code" in pipe

    control = verification_rejection_guidance("disallowed_shell_control_flow")
    assert "single command" in control.lower()

    observe = verification_rejection_guidance("non_assertive_observation")
    assert "prove" in observe.lower() or "fails on a wrong result" in observe

    # Unknown reasons yield an empty string so callers can append unconditionally.
    assert verification_rejection_guidance("some_future_reason") == ""
    assert verification_rejection_guidance(None) == ""
