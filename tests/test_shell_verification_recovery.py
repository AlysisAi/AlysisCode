from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_subagent_mode_parity import ScriptedClient
from test_verification_batch_recovery import _problems

from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


def _shell(
    root: Path,
    state: TurnExecutionState,
    command: str,
    *,
    required: list[str],
    exit_code: int = 0,
    output: str | None = "Ran 1 test in 0.001s\n\nOK\n",
    touched: list[str] | None = None,
) -> dict[str, Any]:
    state.expected_verification_commands = set(required)
    result: dict[str, Any] = {"cmd": command, "effective_cmd": command, "exit_code": exit_code}
    if output is not None:
        result.update(stdout="", stderr=output)
    if touched:
        result["touched_repo_paths"] = touched
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok",
        result=result,
        known_verification_commands=required,
        verification_authoritative=True,
    )
    return result


def _state() -> TurnExecutionState:
    state = TurnExecutionState(execution_requested=True, material_edit_count=1)
    state.note_verification_relevant_edit()
    return state


@pytest.mark.parametrize("command", ["python -m unittest discover", "pytest -q"])
def test_shell_unavailable_diagnostic_preserves_evidence_but_not_passed_verdict(
    tmp_path: Path, command: str
):
    state = _state()
    _shell(tmp_path, state, command, required=[command])
    accepted = deepcopy(state.accepted_verification_evidence)
    result = _shell(
        tmp_path,
        state,
        command,
        required=[command],
        exit_code=127,
        output="/bin/sh: command not found\n",
    )
    assert state.last_verification_passed is False
    assert state.accepted_verification_evidence == accepted
    assert state.rejected_verification_evidence[-1]["observed_exit_code"] == 127
    assert state.failed_verification_commands() == {command}
    assert "verification_failed" in _problems(state)
    assert result["exit_code"] == 127
    assert result["stderr"] == "/bin/sh: command not found\n"
    assert result["verification_evidence_allowed"] is False
    assert "verification_recovered_launch_failures" not in result


@pytest.mark.parametrize(
    "invalidator",
    [
        "executed_failure",
        "stale_generation",
        "mutation_in_call",
        "different_obligation",
        "missing_prior_output",
        "missing_current_output",
        "unresolved_real_failure",
        "no_tests",
    ],
)
def test_shell_recovery_requires_current_observed_uncontradicted_same_obligation(
    tmp_path: Path,
    invalidator: str,
):
    required = ["pytest tests/a.py -q"]
    state = _state()
    _shell(
        tmp_path,
        state,
        required[0],
        required=required,
        output=None if invalidator == "missing_prior_output" else "1 passed\n",
    )
    if invalidator == "stale_generation":
        state.note_verification_relevant_edit()
    if invalidator == "unresolved_real_failure":
        _shell(tmp_path, state, required[0], required=required, exit_code=1, output="1 failed\n")
    if invalidator == "different_obligation":
        required = ["pytest tests/b.py -q"]
    exit_code = 1 if invalidator == "executed_failure" else 5 if invalidator == "no_tests" else 127
    output = (
        "1 failed\n"
        if invalidator == "executed_failure"
        else "no tests ran in 0.01s\n"
        if invalidator == "no_tests"
        else None
        if invalidator == "missing_current_output"
        else "/bin/sh: pytest: command not found\n"
    )
    result = _shell(
        tmp_path,
        state,
        required[0],
        required=required,
        exit_code=exit_code,
        output=output,
        touched=["src/app.py"] if invalidator == "mutation_in_call" else None,
    )
    assert state.last_verification_passed is False
    assert required[0] in state.failed_verification_commands()
    assert "verification_failed" in _problems(state)
    assert "verification_recovered_launch_failures" not in result


def test_shell_recovery_preserves_other_failed_and_new_required_obligations(tmp_path: Path):
    required = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    state = _state()
    _shell(tmp_path, state, required[0], required=required)
    _shell(tmp_path, state, required[1], required=required, exit_code=1, output="1 failed\n")
    failure = state.failed_verification_command_snippets[required[1]]
    _shell(
        tmp_path,
        state,
        required[0],
        required=required + ["pytest tests/c.py -q"],
        exit_code=127,
        output="/bin/sh: pytest: command not found\n",
    )
    assert state.last_verification_passed is False
    assert state.failed_verification_commands() == set(required)
    assert state.failed_verification_command_snippets[required[1]] == failure
    assert state.covered_verification_commands == {required[0]}
    assert "verification_failed" in _problems(state)
    assert "pytest tests/c.py -q" not in state.covered_verification_commands


def test_shell_recovery_does_not_cover_a_new_required_command(tmp_path: Path):
    required = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    state = _state()
    _shell(tmp_path, state, required[0], required=required[:1])
    _shell(
        tmp_path,
        state,
        required[0],
        required=required,
        exit_code=127,
        output="/bin/sh: pytest: command not found\n",
    )
    assert state.covered_verification_commands == {required[0]}
    assert "verification_failed" in _problems(state)


def test_pipeline_unavailable_filter_cannot_hide_a_real_failed_first_stage(tmp_path: Path):
    state = _state()
    required = ["pytest -q"]
    _shell(tmp_path, state, required[0], required=required, output="1 passed\n")
    command = "pytest -q | missing-filter"
    result = {
        "cmd": command,
        "exit_code": 127,
        "pipeline_stage_status": [1, 127],
        "stdout": "1 failed\n",
        "stderr": "/bin/sh: missing-filter: command not found\n",
    }
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok",
        result=result,
        known_verification_commands=required,
        verification_authoritative=True,
    )
    assert state.last_verification_passed is False
    assert "verification_failed" in _problems(state)
    assert "verification_recovered_launch_failures" not in result


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("executed_failure", [False, True])
def test_shell_completion_replays_accepted_python3_then_unavailable_python(
    tmp_path: Path,
    one_shot: bool,
    executed_failure: bool,
):
    required = "python -m unittest discover"
    accepted = "python3 -m unittest discover -s tests -v"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", verify_commands=[required]),
        root=tmp_path,
        mode="fullaccess",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "logs",
        one_shot_execution=one_shot,
        enable_chat_turn_step_budget=True,
    )
    outputs = [
        {
            "cmd": accepted,
            "effective_cmd": accepted,
            "exit_code": 0,
            "stdout": "",
            "stderr": "test_ok (test_app.Checks.test_ok) ... ok\nRan 1 test in 0.001s\n\nOK\n",
        },
        {
            "cmd": required,
            "effective_cmd": required,
            "exit_code": 1 if executed_failure else 127,
            "stdout": "",
            "stderr": "Ran 1 test in 0.001s\n\nFAILED (failures=1)\n"
            if executed_failure
            else "/bin/sh: python: command not found\n",
        },
    ]
    session.tools["shell_run"] = ToolDef(
        name="shell_run",
        description="Run a command.",
        parameters={"type": "object", "properties": {}},
        run=lambda _args: deepcopy(outputs.pop(0)),
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]
    report = "Created result.py exporting VALUE. The requested python3 check passed. " + (
        "A subsequent check executed and failed; that failure remains unresolved."
        if executed_failure
        else "The python alias returned an unavailable diagnostic; the earlier python3 check passed."
    )
    final = LLMResponse(content=report, tool_calls=[], raw={})
    client = ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="fs_write",
                        arguments={"path": "result.py", "content": "VALUE = 1\n"},
                    )
                ],
                raw={},
            ),
            *[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id=f"check-{i}", name="shell_run", arguments={"cmd": cmd})
                    ],
                    raw={},
                )
                for i, cmd in enumerate([accepted, required])
            ],
            final,
            final,
        ]
    )
    session.client = client
    try:
        session.run_turn("Create result.py exporting VALUE and run the configured checks.")
        events = list(read_session_events(session.store.path))
    finally:
        session.close()
    assert [e for e in events if e["type"] == "completion_gate_nudge"]
    assert len(client.requests) == 5
    finals = [e["payload"]["content"] for e in events if e["type"] == "final"]
    assert len(finals) == 1
    assert finals[0].split("\n\n---", 1)[0] == (
        report + "\n\nNot verified: required or observed checks failed."
    )
