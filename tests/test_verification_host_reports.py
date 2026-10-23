from __future__ import annotations

import copy
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code import agent_loop
from alysis_code.agent.blast_radius import (
    BlastRadiusStatus,
    build_repo_test_index,
    select_blast_radius_scope,
)
from alysis_code.agent.regression_baseline import TestReport as ParsedTestReport
from alysis_code.agent.regression_baseline import baseline_command_key, parse_test_report
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.agent.verification_result import verification_result_for_model
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult


def _tools(root: Path, commands: list[str]):
    cfg = AppConfig(model="test-model", skills_enabled=False, verify_commands=commands)
    cfg.extra_fields["verify_sandbox"] = {"mode": "off"}
    store = SessionStore(
        enabled=False,
        sessions_dir=root.parent / "sessions",
        session_id="host-reports",
        cwd=str(root),
        repo_root=str(root),
    )
    return agent_loop.build_tools(
        root=root,
        console=Console(file=io.StringIO()),
        store=store,
        cfg=cfg,
        mode="fullaccess",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        authoritative_verification_commands=commands,
        effective_verification_commands=commands,
    )


def _record(root: Path, state: TurnExecutionState, result: dict, commands: list[str]):
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="verify_run",
        arguments={"commands": commands},
        status="ok",
        result=result,
        known_verification_commands=commands,
        verification_authoritative=True,
    )


def test_real_long_failing_baseline_survives_preview_and_later_pass(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "__init__.py").write_text("")
    (root / "sample.py").write_text("VALUE = 0\n")
    (root / "tests" / "test_sample.py").write_text(
        "import unittest\nfrom sample import VALUE\n\n"
        "class Sample(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        print('diagnostic ' * 150 + 'HOST_PARSE_ONLY_SENTINEL')\n"
        "        self.assertEqual(VALUE, 123)\n"
        "    def test_existing(self):\n"
        "        self.assertEqual(2 + 2, 4)\n"
    )
    executable = (
        subprocess.list2cmdline([Path(sys.executable).as_posix()])
        if os.name == "nt"
        else shlex.quote(sys.executable)
    )
    command = f"{executable} -m unittest discover -s tests -v"
    tools = _tools(root, [command])
    state = TurnExecutionState(execution_requested=True)
    state.blast_radius_scope = select_blast_radius_scope(
        touched_paths=["sample.py"], index=build_repo_test_index(root)
    )

    before = tools["verify_run"].run({"commands": [command]})
    row = before["command_results"][0]
    assert row["exit_code"] == 1
    assert row["output_truncated"] is True
    assert row["output_chars"] > 1000
    assert not parse_test_report(row["output_preview"]).counts_known
    report = ParsedTestReport.from_payload(row["host_test_report"])
    assert report is not None and report.usable_as_baseline
    assert report.failed == 1 and "test_value" in report.failed_ids[0]
    assert len(json.dumps(row["host_test_report"])) < 400
    visible = verification_result_for_model(before)
    assert "host_test_report" not in visible["command_results"][0]
    assert visible["command_results"][0]["output_preview"] == row["output_preview"]
    assert "HOST_PARSE_ONLY_SENTINEL" not in json.dumps(visible)

    _record(root, state, before, [command])
    assert state.test_baselines[baseline_command_key(command)].report == report
    assert state.has_blast_radius_baseline()
    (root / "sample.py").write_text("VALUE = 123\n")
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="fs_write",
        arguments={"path": "sample.py"},
        status="ok",
        result={"path": "sample.py", "created": False},
        known_verification_commands=[command],
    )
    after = tools["verify_run"].run({"commands": [command]})
    assert after["all_passed"] is True
    _record(root, state, after, [command])
    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == BlastRadiusStatus.CLEAN
    assert assessment.has_baseline
    assert state.post_edit_test_runs[-1].report.failed == 0
    assert state.pending_regression_capture_events[0]["report"] == report.as_payload()


@pytest.mark.parametrize(
    ("output", "exit_code", "real_execution"),
    [
        ("incomplete runner output\n", 0, True),
        ("Ran 1 test in 0.1s\n", 0, True),
        ("==== 2 failed, 3 passed in 0.5s ====\n", 1, None),
        ("Ran 1 test in 0.1s\n\nOK\n", 0, False),
    ],
)
def test_full_unknown_incomplete_and_nonexecuted_reports_stay_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output, exit_code, real_execution
) -> None:
    command = "python3 -m unittest discover"
    result = VerifyRunResult(
        commands=[command],
        command_results=[
            VerifyCommandResult(
                command=command,
                output=output,
                exit_code=exit_code,
                real_execution=real_execution,
            )
        ],
        artifact_path=tmp_path / "report.txt",
    )
    monkeypatch.setattr(agent_loop, "run_task_verification", lambda **_: result)
    payload = _tools(tmp_path, [command])["verify_run"].run({"commands": [command]})
    state = TurnExecutionState(execution_requested=True)
    _record(tmp_path, state, payload, [command])
    assert not state.test_baselines
    assert not state.blast_radius_runs[-1].report.usable_as_baseline
    assert state.pending_regression_capture_events[-1]["kind"] == "baseline_unusable"


def test_wrapper_keeps_fallback_and_repeated_command_reports_paired_by_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = "pytest -q"
    failing = (
        "diagnostic\n" * 80
        + "=== short test summary info ===\n"
        + "FAILED tests/test_one.py::test_first - failed\n"
        + "==== 1 failed in 0.5s ====\n"
    )
    result = VerifyRunResult(
        commands=[command, command],
        command_results=[
            VerifyCommandResult(command=command, exit_code=1, output=failing),
            VerifyCommandResult(
                command=command,
                effective_command="python3 -m pytest -q",
                exit_code=0,
                output="==== 1 passed in 0.5s ====\n",
                real_execution=True,
                fallback_used=True,
            ),
        ],
        artifact_path=tmp_path / "report.txt",
    )
    monkeypatch.setattr(agent_loop, "run_task_verification", lambda **_: result)
    payload = _tools(tmp_path, [command])["verify_run"].run({"commands": [command]})
    rows = payload["command_results"]
    assert rows[0]["host_test_report"]["failed_ids"] == ["tests/test_one.py::test_first"]
    assert rows[1]["host_test_report"]["failed"] == 0
    assert rows[1]["effective_command"] == "python3 -m pytest -q"
    assert rows[1]["fallback_used"] is True
    assert "host_test_report" not in verification_result_for_model(payload)["command_results"][1]


@pytest.mark.parametrize("bad", [None, {}, {"runner": "unittest", "counts_known": True}])
def test_invalid_host_report_does_not_fall_back_to_claimed_success(tmp_path: Path, bad) -> None:
    command = "python3 -m unittest discover"
    state = TurnExecutionState(execution_requested=True)
    _record(
        tmp_path,
        state,
        {
            "all_passed": True,
            "command_results": [
                {
                    "command": command,
                    "exit_code": 0,
                    "ok": True,
                    "real_execution": True,
                    "output_preview": "Ran 1 test in 0.1s\n\nOK\n",
                    "host_test_report": bad,
                }
            ],
        },
        [command],
    )
    assert not state.test_baselines


def test_report_hydration_recomputes_ids_and_rejects_invalid_counts() -> None:
    incomplete = parse_test_report("==== 2 failed in 0.5s ====\n").as_payload()
    incomplete["ids_complete"] = True
    restored = ParsedTestReport.from_payload(incomplete)
    assert restored is not None and not restored.ids_complete
    assert not restored.usable_as_baseline
    for value in (True, -1, "2"):
        invalid = copy.deepcopy(incomplete)
        invalid["failed"] = value
        assert ParsedTestReport.from_payload(invalid) is None
