from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_subagent_mode_parity import ScriptedClient

from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.agent.verification import (
    TurnExecutionState,
    _completion_gate_problems,
    _record_tool_effect,
    _verification_evidence_observation,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events


def _check(command: str, *, passed: bool = True, launched: bool = True) -> dict[str, Any]:
    return {
        "command": command,
        "effective_command": command,
        "ok": passed,
        "exit_code": 0 if passed else 1 if launched else 127,
        "real_execution": launched,
        "status": "passed" if passed else "failed",
        "non_execution_reason": "" if launched else "execution_layer_failure",
        "output_preview": "1 passed\n"
        if passed
        else "assertion failed\n"
        if launched
        else "unavailable\n",
    }


def _assert_raw_results_preserved(
    actual: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    # Host-derived authority annotations may be added, but every observation
    # and its order must remain intact, including duplicate command occurrences.
    assert len(actual) == len(expected)
    for observed, original in zip(actual, expected, strict=True):
        assert {key: observed[key] for key in original} == original
        assert isinstance(observed["verification_evidence_allowed"], bool)


def _record(
    root: Path,
    checks: list[dict[str, Any]],
    *,
    required: list[str],
    touched: list[str] | None = None,
    state: TurnExecutionState | None = None,
) -> tuple[TurnExecutionState, dict[str, Any]]:
    if state is None:
        state = TurnExecutionState(execution_requested=True, material_edit_count=1)
        state.note_verification_relevant_edit()
    state.expected_verification_commands = set(required)
    result = {
        "commands": [item["command"] for item in checks],
        "command_results": deepcopy(checks),
        "all_passed": all(item["ok"] for item in checks),
        "status": "passed" if all(item["ok"] for item in checks) else "failed",
        "failure_category": "infra_unavailable",
        **({"touched_repo_paths": touched} if touched else {}),
    }
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="verify_run",
        arguments={"commands": result["commands"]},
        status="ok",
        result=result,
        known_verification_commands=required,
        verification_authoritative=True,
    )
    return state, result


def _problems(state: TurnExecutionState) -> list[str]:
    return _completion_gate_problems(
        state=state,
        final_text="Implemented the change and checked the result.",
        blocked=False,
        verification_expected=True,
        require_material_edit_evidence=False,
    )


@pytest.mark.parametrize(
    ("required", "replacement"),
    [
        ("pytest -q", "pytest -q"),
        ("python -m unittest discover", "python3 -m unittest discover"),
        ("pytest -q", "python3 -m pytest -q"),
    ],
)
@pytest.mark.parametrize("failure_first", [False, True])
def test_accepted_equivalent_does_not_erase_unproven_launch_failure_in_batch(
    tmp_path: Path, required: str, replacement: str, failure_first: bool
) -> None:
    checks = [_check(required, passed=False, launched=False), _check(replacement)]
    if not failure_first:
        checks.reverse()
    state, result = _record(tmp_path, checks, required=[required])

    assert state.last_verification_passed is False
    assert state.failed_verification_commands() == {required}
    assert state.covered_verification_commands == {required}
    assert state.last_verification_failure_snippet
    assert "verification_failed" in _problems(state)
    assert [item["normalized_command"] for item in state.accepted_verification_evidence] == [
        replacement
    ]
    assert state.accepted_verification_evidence[0]["observed_exit_code"] == 0
    assert state.accepted_verification_evidence[0]["observed_output"] is True
    assert state.rejected_verification_evidence[0]["observed_exit_code"] == 127
    _assert_raw_results_preserved(result["command_results"], checks)
    assert result["all_passed"] is False  # The unavailable invocation remains visible.


@pytest.mark.parametrize("failure_first", [False, True])
@pytest.mark.parametrize("launched", [False, True])
def test_identical_commands_keep_each_occurrences_evidence_and_failure(
    tmp_path: Path, failure_first: bool, launched: bool
) -> None:
    command = "pytest -q"
    failure = _check(command, passed=False, launched=launched)
    if launched:
        failure["output_preview"] = "================= 1 failed in 0.01s =================\n"
    success = _check(command)
    checks = [failure, success] if failure_first else [success, failure]

    state, result = _record(tmp_path, checks, required=[command])

    assert len(state.accepted_verification_evidence) == 1
    assert state.accepted_verification_evidence[0]["observed_exit_code"] == 0
    assert state.accepted_verification_evidence[0]["observed_output"] is True
    assert len(state.rejected_verification_evidence) == 1
    assert state.rejected_verification_evidence[0]["observed_exit_code"] == failure["exit_code"]
    assert state.last_verification_passed is False
    assert state.failed_verification_commands() == {command}
    assert "verification_failed" in _problems(state)
    _assert_raw_results_preserved(result["command_results"], checks)
    assert result["all_passed"] is False
    if launched:
        assert [run.report.failed for run in state.post_edit_test_runs] == (
            [1, 0] if failure_first else [0, 1]
        )


@pytest.mark.parametrize("missing_first", [False, True])
def test_identical_commands_do_not_borrow_another_occurrences_output_capture(
    tmp_path: Path, missing_first: bool
) -> None:
    command = "pytest -q"
    missing = _check(command)
    missing.pop("output_preview")
    observed = _check(command)
    checks = [missing, observed] if missing_first else [observed, missing]

    state, _ = _record(tmp_path, checks, required=[command])

    assert [record["observed_output"] for record in state.executed_verification_evidence] == (
        [False, True] if missing_first else [True, False]
    )


def test_identical_command_retry_without_output_does_not_recover_failed_launch(
    tmp_path: Path,
) -> None:
    command = "pytest -q"
    missing = _check(command)
    missing.pop("output_preview")
    checks = [_check(command, passed=False, launched=False), missing]

    state, result = _record(tmp_path, checks, required=[command])

    assert state.last_verification_passed is False
    assert state.accepted_verification_evidence == []
    assert state.failed_verification_commands() == {command}
    assert "verification_failed" in _problems(state)
    assert "verification_recovered_launch_failures" not in result


def test_batch_observation_requires_a_specific_result_occurrence() -> None:
    assert _verification_evidence_observation(
        tool_name="verify_run",
        result={"all_passed": True, "command_results": [_check("pytest -q"), _check("pytest -q")]},
    ) == (None, False)


@pytest.mark.parametrize(
    ("required", "accepted_command"),
    [
        ("pytest -q", "pytest -q"),
        ("pytest -q", "python3 -m pytest -q"),
        ("python -m unittest discover -s tests -v", "python3 -m unittest discover -s tests -v"),
    ],
)
def test_later_unavailable_call_preserves_evidence_but_invalidates_passed_verdict(
    tmp_path: Path, required: str, accepted_command: str
) -> None:
    state, _ = _record(tmp_path, [_check(accepted_command)], required=[required])
    accepted = deepcopy(state.accepted_verification_evidence)
    unavailable = _check(required, passed=False, launched=False)

    state, result = _record(tmp_path, [unavailable], required=[required], state=state)

    assert state.last_verification_passed is False
    assert state.accepted_verification_evidence == accepted
    assert state.rejected_verification_evidence[-1]["observed_exit_code"] == 127
    assert state.failed_verification_commands() == {required}
    assert state.covered_verification_commands == {required}
    assert "verification_failed" in _problems(state)
    _assert_raw_results_preserved(result["command_results"], [unavailable])
    assert result["all_passed"] is False
    assert result["failure_category"] == "infra_unavailable"
    assert "verification_recovered_launch_failures" not in result


@pytest.mark.parametrize(
    "invalidator",
    [
        "executed_failure",
        "stale_generation",
        "mutation_in_call",
        "different_obligation",
        "missing_output",
        "unresolved_real_failure",
    ],
)
def test_unavailable_call_cannot_reuse_invalid_or_contradicted_coverage(
    tmp_path: Path, invalidator: str
) -> None:
    command = "pytest tests/a.py -q"
    passed = _check(command)
    if invalidator == "missing_output":
        passed.pop("output_preview")
    state, _ = _record(tmp_path, [passed], required=[command])
    if invalidator == "stale_generation":
        state.note_verification_relevant_edit()
    elif invalidator == "unresolved_real_failure":
        state, _ = _record(
            tmp_path, [_check(command, passed=False)], required=[command], state=state
        )
    if invalidator == "different_obligation":
        command = "pytest tests/b.py -q"
    failed = _check(command, passed=False, launched=invalidator == "executed_failure")

    state, result = _record(
        tmp_path,
        [failed],
        required=[command],
        state=state,
        touched=["src/app.py"] if invalidator == "mutation_in_call" else None,
    )

    assert state.last_verification_passed is False
    assert command in state.failed_verification_commands()
    assert "verification_failed" in _problems(state)
    assert "verification_recovered_launch_failures" not in result


def test_current_coverage_does_not_satisfy_a_new_required_obligation(tmp_path: Path) -> None:
    required = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    state, _ = _record(tmp_path, [_check(required[0])], required=required[:1])
    state, _ = _record(
        tmp_path,
        [_check(required[0], passed=False, launched=False)],
        required=required,
        state=state,
    )
    assert state.covered_verification_commands == {required[0]}
    assert "verification_failed" in _problems(state)
    assert required[1] not in state.covered_verification_commands


def test_unavailable_call_preserves_an_unrelated_real_failure(tmp_path: Path) -> None:
    required = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    state, _ = _record(tmp_path, [_check(required[0])], required=required)
    state, _ = _record(
        tmp_path, [_check(required[1], passed=False)], required=required, state=state
    )
    failure = state.failed_verification_command_snippets[required[1]]

    state, _ = _record(
        tmp_path,
        [_check(required[0], passed=False, launched=False)],
        required=required,
        state=state,
    )

    assert state.last_verification_passed is False
    assert state.failed_verification_commands() == set(required)
    assert state.failed_verification_command_snippets[required[1]] == failure
    assert "verification_failed" in _problems(state)


def test_mixed_batch_keeps_valid_evidence_without_erasing_unrelated_failure(tmp_path: Path) -> None:
    required = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    state, _ = _record(
        tmp_path,
        [_check(required[0]), _check(required[1], passed=False)],
        required=required,
    )

    assert state.last_verification_passed is False
    assert state.failed_verification_commands() == {required[1]}
    assert [item["normalized_command"] for item in state.accepted_verification_evidence] == [
        required[0]
    ]
    assert "verification_failed" in _problems(state)


@pytest.mark.parametrize(
    "failure",
    [
        "executed_failure",
        "different_obligation",
        "missing_observation",
        "mutated_source",
        "supplemental_replacement",
        "unobserved_replacement",
    ],
)
def test_replacement_does_not_hide_contradictory_or_unaccepted_evidence(
    tmp_path: Path, failure: str
) -> None:
    required = "pytest tests/a.py -q"
    checks = [
        _check(required, passed=False, launched=False),
        _check("python3 -m pytest tests/a.py -q"),
    ]
    obligations = [required]
    touched = None
    if failure == "executed_failure":
        checks[0] = _check(required, passed=False)
    elif failure == "different_obligation":
        checks[1] = _check("pytest tests/b.py -q")
        obligations.append(checks[1]["command"])
    elif failure == "missing_observation":
        checks[1].pop("output_preview")
    elif failure == "mutated_source":
        touched = ["src/app.py"]
    elif failure == "supplemental_replacement":
        (tmp_path / "checks.py").write_text("assert True\n")
        checks[1] = _check("python checks.py")
    elif failure == "unobserved_replacement":
        checks[1]["real_execution"] = False

    state, _ = _record(tmp_path, checks, required=obligations, touched=touched)

    assert state.last_verification_passed is False
    assert required in state.failed_verification_commands()
    assert "verification_failed" in _problems(state)


def test_a_subsequent_observed_pass_clears_failure_but_becomes_stale_after_edit(
    tmp_path: Path,
) -> None:
    required = "pytest -q"
    state, _ = _record(
        tmp_path,
        [_check(required, passed=False, launched=False), _check("python3 -m pytest -q")],
        required=[required],
    )
    assert "verification_failed" in _problems(state)
    # A later success must repeat the failed execution, not merely a command
    # whose accepted selection can cover the same verification obligation.
    state, _ = _record(tmp_path, [_check(required)], required=[required], state=state)
    assert _problems(state) == []
    state.note_verification_relevant_edit()
    assert "verification_incomplete" in _problems(state)


@pytest.mark.parametrize("other_check_ran", [False, True])
def test_recovery_cannot_complete_a_different_required_obligation(
    tmp_path: Path, other_check_ran: bool
) -> None:
    required = ["pytest tests/a.py -q", "pytest tests/b.py -q"]
    checks = [
        _check(required[0], passed=False, launched=False),
        _check("python3 -m pytest tests/a.py -q"),
    ]
    if other_check_ran:
        checks.append(_check(required[1], passed=False))
    state, _ = _record(tmp_path, checks, required=required)

    assert state.covered_verification_commands == {required[0]}
    assert state.failed_verification_commands() == (
        set(required) if other_check_ran else {required[0]}
    )
    assert "verification_failed" in _problems(state)


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("failed_check_executed", [False, True])
@pytest.mark.parametrize("repeat_same_command", [False, True])
@pytest.mark.parametrize("separate_calls", [False, True])
def test_finalization_does_not_hide_failed_or_ambiguous_execution(
    tmp_path: Path,
    one_shot: bool,
    failed_check_executed: bool,
    repeat_same_command: bool,
    separate_calls: bool,
) -> None:
    required = "python -m unittest discover"
    checks = [
        _check(required, passed=False, launched=failed_check_executed),
        _check(required if repeat_same_command else "python3 -m unittest discover"),
    ]
    payload = {
        "commands": [item["command"] for item in checks],
        "command_results": checks,
        "all_passed": False,
        "status": "failed",
        "failure_category": "verification_failed" if failed_check_executed else "infra_unavailable",
    }
    payloads = (
        [
            {
                "commands": [item["command"]],
                "command_results": [item],
                "all_passed": item["ok"],
                "status": item["status"],
                "failure_category": payload["failure_category"] if not item["ok"] else "",
            }
            for item in reversed(checks)
        ]
        if separate_calls
        else [payload]
    )
    queued_payloads = deepcopy(payloads)
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
    session.tools["verify_run"] = ToolDef(
        name="verify_run",
        description="Run the configured checks.",
        parameters={"type": "object", "properties": {}},
        run=lambda _args: queued_payloads.pop(0),
    )
    session.tool_list = [tool.as_openai_tool() for tool in session.tools.values()]
    report = "Created result.py exporting VALUE. A verification run passed. " + (
        "The original check failed and remains unresolved."
        if failed_check_executed
        else "The original launcher returned an unavailable diagnostic; a separate check passed."
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
                        arguments={"path": "result.py", "content": 'VALUE = "hello"\n'},
                    )
                ],
                raw={},
            ),
            *[
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id=f"verify-{index}", name="verify_run", arguments={})],
                    raw={},
                )
                for index in range(len(payloads))
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

    nudges = [event for event in events if event["type"] == "completion_gate_nudge"]
    assert nudges
    assert len(client.requests) == 4 + int(separate_calls)
    finals = [event["payload"]["content"] for event in events if event["type"] == "final"]
    assert len(finals) == 1
    reported_content = finals[0].split("\n\n---", 1)[0]
    # Preserve the model's report and append the host's failure notice. Neither
    # the text nor the structured result may conceal unresolved failed checks.
    final_payload = [event["payload"] for event in events if event["type"] == "final"][0]
    assert final_payload["verification_notice"] is True
    assert final_payload["task_outcome"]["verified_success"] is False
    assert "verification_failed" in final_payload["task_outcome"]["problems"]
    assert reported_content.startswith(report + "\n\nNot verified:")
    assert "required or observed checks failed" in reported_content
    verification = [
        event["payload"]["result"]
        for event in events
        if event["type"] == "tool_result" and event["payload"]["name"] == "verify_run"
    ][-1]
    assert verification["all_passed"] is False
    _assert_raw_results_preserved(
        verification["command_results"], [checks[0]] if separate_calls else checks
    )
