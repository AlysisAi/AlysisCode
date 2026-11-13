"""Fixture sessions test receipt/repair semantics; real Linux coverage is separate."""

import shlex
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from alysis_code.bots import coding_worker


def test_intermediate_text_summary_cannot_close_execution_before_independent_checks():
    requests = []

    class Broker:
        client = SimpleNamespace(base_url="http://fixture.invalid")

        def post(self, path, **kwargs):
            requests.append(kwargs)
            return {"content": "Fixture summary", "tool_calls": [], "instruction_revision": 2}

    client = coding_worker.BrokerClient(Broker(), "fixture-job", "fixture", execution_limit=3)
    client.chat(messages=[], tools=[{"type": "function"}])
    client.chat(messages=[], tools=None)  # Intermediate summary before independent checks.
    assert not client.reporting_started and client.execution_calls == 2
    assert client.instruction_revision == 2
    client.chat(messages=[], tools=[{"type": "function"}])  # Bounded repair is still execution.
    client.chat(messages=[], tools=[{"type": "function"}])  # Reserved report cannot act.
    assert [r["phase"] for r in requests] == ["execute", "execute", "execute", "report"]
    assert client.execution_calls == 3 and client.reporting_started
    assert requests[-1]["tools"] is None
    assert "Execution is now closed" in requests[-1]["messages"][-1]["content"]


def test_reserved_report_cannot_return_executable_tool_calls():
    class Broker:
        client = SimpleNamespace(base_url="http://fixture.invalid")

        def post(self, path, **kwargs):
            assert kwargs["phase"] == "report" and kwargs["tools"] is None
            return {
                "content": "",
                "tool_calls": [
                    {"id": "unexpected", "name": "shell_run", "arguments": {"cmd": "forbidden"}}
                ],
            }

    client = coding_worker.BrokerClient(Broker(), "fixture", "fixture", execution_limit=1)
    client.execution_calls = 1
    with pytest.raises(coding_worker.LLMError, match="no further actions"):
        client.chat(messages=[], tools=[{"type": "function"}])


@pytest.mark.parametrize(
    "outcomes,calls,reporting,expected_turns,passed,interrupt_repair",
    [
        ([1, 0], 1, False, 2, True, False),
        ([1, 1], 1, False, 2, False, False),
        ([1], 7, False, 1, False, False),
        ([1], 1, True, 1, False, False),
        ([0], 1, False, 1, True, False),
        ([1], 1, False, 2, False, True),
    ],
)
def test_independent_failure_repairs_once_without_resetting_budget(
    tmp_path, monkeypatch, outcomes, calls, reporting, expected_turns, passed, interrupt_repair
):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    observed = []
    job = {
        "id": uuid4().hex,
        "workspace_id": uuid4().hex,
        "session_id": uuid4().hex,
        "context": {"original_request": "Correct this project", "execution_calls": 8},
    }

    class Session:
        effective_verification_commands = ["project-check"]
        verification_selection_source = "fixture-existing-project"
        verification_authoritative = True
        last_turn_outcome = {"outcome": "completed_unverified", "verified_success": False}
        messages = [{"role": "assistant", "content": "Fixture report"}]
        client = SimpleNamespace(
            execution_calls=0, reporting_started=reporting, instruction_revision=0
        )
        max_steps = 8

        def run_turn(self, request, **kw):
            observed.append((request, self.max_steps, kw["ephemeral_system_messages"]))
            self.client.execution_calls += calls
            if interrupt_repair and len(observed) == 2:
                raise TimeoutError("Fixture repair interrupted")
            # Model-created test selections must not replace the frozen checks.
            self.effective_verification_commands = ["weakened-check"]
            return 0

        def close(self):
            pass

    executions = []
    responses = iter(outcomes)

    class Runner:
        def run(self, **kw):
            assert kw["root"] == workspace and kw["cwd"] == workspace
            executions.append(kw["cmd"])
            return SimpleNamespace(
                returncode=next(responses), stdout="real receipt fixture", stderr=""
            )

    monkeypatch.setattr(coding_worker, "make_session", lambda *args: Session())
    result = coding_worker.execute(
        job,
        workspace=workspace,
        state=state,
        broker=None,
        settings={"verify": []},
        stop=threading.Event(),
        runner=Runner(),
    )
    assert len(observed) == expected_turns
    assert executions == ["project-check"] * len(outcomes)
    assert result["verification"]["passed"] is passed
    assert result["verification"]["complete"]
    assert result["verification"]["repair_attempts"] == expected_turns - 1
    assert result["status"] == ("completed" if passed else "failed")
    assert not result["outcome"]["verified_success"]  # Passing alone cannot upgrade the verdict.
    if interrupt_repair:
        assert result["verification"]["checks"][0]["exit_code"] == 1
        assert "no retry was made after this interruption" in result["summary"]
    if expected_turns == 2:
        assert observed[1][0] == job["context"]["original_request"]
        assert observed[1][1] == 8 - calls + coding_worker.REPORT_CALLS
        assert "project-check" in observed[1][2][-1]


def test_cancellation_during_checks_does_not_run_the_next_command(tmp_path):
    cancelled = threading.Event()
    executed = []

    class Runner:
        def run(self, **kw):
            executed.append(kw["cmd"])
            cancelled.set()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = coding_worker.verify_workspace(
        ["first", "second"], workspace=tmp_path, runner=Runner(), cancelled=cancelled.is_set
    )
    assert executed == ["first"]
    assert not result["passed"] and not result["complete"]


def test_deadline_between_checks_preserves_partial_receipt(tmp_path, monkeypatch):
    clock = iter([0, 0, 61])
    monkeypatch.setattr(coding_worker.time, "monotonic", lambda: next(clock))
    executed = []

    class Runner:
        def run(self, **kw):
            executed.append(kw["cmd"])
            return SimpleNamespace(returncode=0, stdout="first check passed", stderr="")

    result = coding_worker.verify_workspace(
        ["first", "second"], workspace=tmp_path, runner=Runner(), cancelled=lambda: False
    )
    assert executed == ["first"]
    assert result["checks"][0]["exit_code"] == 0
    assert not result["passed"] and not result["complete"]


@pytest.mark.parametrize(
    "test_body,reason,certificate,verified",
    [
        ("self.assertTrue(True)", "completed", "SUFFICIENT", True),
        ("self.assertTrue(False)", "completed", "SUFFICIENT", False),
        (None, "completed", "SUFFICIENT", False),
        ("self.assertTrue(True)", "max_steps_exceeded", "SUFFICIENT", False),
        ("self.assertTrue(True)", "cancelled", "SUFFICIENT", False),
        ("self.assertTrue(True)", "completed", "INSUFFICIENT", False),
    ],
)
def test_final_real_checks_refresh_stale_evidence_without_overriding_completion(
    tmp_path, test_body, reason, certificate, verified
):
    from alysis_code.agent.acceptance_contract import build_acceptance_contract
    from alysis_code.agent.verification import TurnExecutionState
    from alysis_code.run_outcome import task_outcome_record
    from alysis_code.sandbox_runner import HostShellRunner

    command = shlex.quote(sys.executable) + " -m unittest discover -v"
    if test_body:
        (tmp_path / "test_final.py").write_text(
            "import unittest\nclass FinalCheck(unittest.TestCase):\n"
            f"    def test_result(self): {test_body}\n"
        )
    state = TurnExecutionState(
        execution_requested=True,
        material_edit_count=1,
        verification_relevant_edit_generation=2,
        last_successful_verification_generation=1,
        expected_verification_commands={command},
        covered_verification_command_generations={command: 1},
        acceptance_contract=build_acceptance_contract(
            root=tmp_path,
            instruction="Complete this project and its checks.",
            authoritative_verification_commands=[command],
            effective_verification_commands=[command],
        ),
        latest_completion_certificate={"status": certificate},
    )
    session = SimpleNamespace(
        _turn_execution_state=state,
        last_turn_outcome={"exit_code": 0 if reason == "completed" else 1, "reason": reason},
        store=SimpleNamespace(append=lambda *args: None),
    )

    def record(**kwargs):
        session.last_turn_outcome = task_outcome_record(state=state.as_payload(), **kwargs)

    session._record_task_outcome = record
    assert state.verification_coverage_is_stale()
    check = coding_worker.verify_workspace(
        [command], workspace=tmp_path, runner=HostShellRunner(), cancelled=lambda: False
    )
    coding_worker.record_independent_verification(
        session, workspace=tmp_path, commands=[command], check=check, authoritative=True
    )
    assert session.last_turn_outcome["verified_success"] is verified
    assert session.last_turn_outcome["reason"] == reason
    assert state.latest_completion_certificate["status"] == certificate
