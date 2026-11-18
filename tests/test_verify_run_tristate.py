"""A skip is not a pass.

``VerifyRunResult.all_passed`` was ``all(item.ok ...)`` — vacuously True for an
empty command list, and True for a run where every command was benignly
skipped. Consumers branching on the boolean inherited a false green: the repair
loop skipped repair as "already passing", acceptance criteria were satisfied by
runs that ran nothing, and session telemetry recorded
``{"commands": [], "all_passed": true}`` (observed live twice in
canary session 20260831T161825Z_614a3ecd).

The fix is a tri-state run ``status`` — ``passed`` / ``failed`` / ``not_run`` —
with ``all_passed`` true only for ``passed``, and consumers taught that
``not_run`` is an absence of observation, neither green nor red.
"""

from __future__ import annotations

from pathlib import Path

from alysis_code.agent.acceptance_contract import _command_passed
from alysis_code.session_metrics import score_session_events
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult

ARTIFACT = Path("/tmp/verify-artifact-unused.json")


def _passed(command: str = "pytest -q") -> VerifyCommandResult:
    return VerifyCommandResult(command=command, exit_code=0, output="1 passed", real_execution=True)


def _failed(command: str = "pytest -q") -> VerifyCommandResult:
    return VerifyCommandResult(command=command, exit_code=1, output="1 failed", real_execution=True)


def _skipped(command: str = "python -m unittest discover") -> VerifyCommandResult:
    return VerifyCommandResult(
        command=command,
        exit_code=0,
        output="Ran 0 tests",
        real_execution=False,
        non_execution_reason="no_applicable_verification",
    )


def _inconclusive(command: str = "true") -> VerifyCommandResult:
    return VerifyCommandResult(command=command, exit_code=0, output="", real_execution=None)


def _run(items: list[VerifyCommandResult]) -> VerifyRunResult:
    return VerifyRunResult(
        commands=[item.command for item in items],
        command_results=items,
        artifact_path=ARTIFACT,
    )


# --- VerifyRunResult semantics -------------------------------------------------


def test_empty_run_is_not_a_pass() -> None:
    result = _run([])
    assert result.status == "not_run"
    assert result.all_passed is False
    assert result.executed_count == 0
    assert result.failure_category_value is None  # not a failure either
    assert result.summary == "verification skipped: no commands"


def test_all_skipped_run_is_not_a_pass() -> None:
    result = _run([_skipped()])
    assert result.status == "not_run"
    assert result.all_passed is False
    assert result.skipped_count == 1
    assert result.failure_category_value is None
    assert "verification skipped: nothing to verify" in result.summary


def test_real_pass_with_a_skip_still_passes() -> None:
    result = _run([_passed(), _skipped("go test ./...")])
    assert result.status == "passed"
    assert result.all_passed is True
    assert result.executed_count == 1
    assert "skipped: go test ./..." in result.summary


def test_real_pass_alone_passes() -> None:
    result = _run([_passed()])
    assert result.status == "passed"
    assert result.all_passed is True
    assert result.summary == "verification passed (1/1)"


def test_failure_still_fails() -> None:
    result = _run([_passed(), _failed("npm test")])
    assert result.status == "failed"
    assert result.all_passed is False
    assert result.failure_category_value is not None
    assert "npm test" in result.failed_commands


def test_inconclusive_execution_is_not_a_pass_and_not_a_skip() -> None:
    result = _run([_inconclusive()])
    assert result.status == "failed"
    assert result.all_passed is False


# --- consumers -----------------------------------------------------------------


def test_acceptance_command_passed_treats_not_run_as_unknown() -> None:
    payload = {"all_passed": False, "status": "not_run", "commands": []}
    assert _command_passed(status="completed", result=payload) is None
    real_pass = {"all_passed": True, "status": "passed", "commands": ["pytest -q"]}
    assert _command_passed(status="completed", result=real_pass) is True
    real_fail = {"all_passed": False, "status": "failed", "commands": ["pytest -q"]}
    assert _command_passed(status="completed", result=real_fail) is False


def test_session_metrics_do_not_count_not_run_as_failure() -> None:
    events = [
        {
            "type": "verify_run",
            "payload": {
                "commands": [],
                "all_passed": False,
                "status": "not_run",
                "summary": "verification skipped: no commands",
                "verification_authoritative": True,
            },
        }
    ]
    metrics = score_session_events(events)
    assert metrics["authoritative_verification_failures"] == 0
    assert metrics["non_authoritative_verification_failures"] == 0

    failing = [
        {
            "type": "verify_run",
            "payload": {
                "commands": ["pytest -q"],
                "all_passed": False,
                "status": "failed",
                "summary": "verification failed (0/1)",
                "verification_authoritative": True,
            },
        }
    ]
    metrics = score_session_events(failing)
    assert metrics["authoritative_verification_failures"] == 1
