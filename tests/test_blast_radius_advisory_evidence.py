from __future__ import annotations

from pathlib import Path

import pytest
from test_blast_radius import _edit, _run_tests, _scoped_state
from test_subagent_mode_parity import ScriptedClient

from alysis_code.agent.blast_radius import (
    BlastRadiusStatus,
    build_blast_radius_scope_advisory,
    build_repo_test_index,
    select_blast_radius_scope,
)
from alysis_code.agent.tools_assembly import ToolDef
from alysis_code.agent.verification import TurnExecutionState
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events

pytestmark = pytest.mark.usefixtures("approved_local_checks")


def _scope_for_new_test(state: TurnExecutionState, root: Path) -> None:
    state.blast_radius_scope = select_blast_radius_scope(
        touched_paths=["tests/test_added.py"], index=build_repo_test_index(root)
    )


def _advisory(state: TurnExecutionState) -> str:
    return build_blast_radius_scope_advisory(
        state.blast_radius_scope,
        has_baseline=state.has_blast_radius_baseline(),
        baseline_covered_paths=state.blast_radius_baseline_covered_paths(),
        baseline_command=state.blast_radius_baseline_command(),
        agent_created_paths=state.agent_created_paths,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_existing.py").write_text("def test_existing():\n    assert True\n")
    return tmp_path


def test_whole_suite_baseline_cannot_claim_a_test_created_after_it(repo: Path):
    state = _scoped_state(repo, touched=["tests/test_existing.py"])
    _run_tests(state, repo, "pytest -q")
    _edit(state, repo, "tests/test_added.py", created=True)
    _scope_for_new_test(state, repo)
    assert state.has_blast_radius_baseline() is False
    assert state.blast_radius_baseline_covered_paths() == ("tests/test_existing.py",)
    assert state.blast_radius_baseline_command() == ""
    note = _advisory(state)
    assert "covers only: tests/test_existing.py" in note
    assert "Newly authored tests were not part of that baseline: tests/test_added.py" in note
    assert "for example" not in note
    assert state.blast_radius_runs[0].as_payload()["agent_created_paths"] == []


@pytest.mark.parametrize("baseline_fails", [False, True])
def test_authored_tests_actually_run_before_product_edit_can_have_a_baseline(
    repo: Path,
    baseline_fails: bool,
):
    state = TurnExecutionState(execution_requested=True)
    _edit(state, repo, "tests/test_added.py", created=True)
    _scope_for_new_test(state, repo)
    failure = "tests/test_added.py::test_added"
    _run_tests(state, repo, "pytest -q", failed=[failure] if baseline_fails else [])
    assert state.has_blast_radius_baseline() is True
    assert set(state.blast_radius_baseline_covered_paths()) == set(state.blast_radius_scope.paths)
    assert "Newly authored tests were not part" not in _advisory(state)
    assert "tests/test_added.py" in state.blast_radius_runs[0].agent_created_paths
    _edit(state, repo, "src/app.py")
    _run_tests(state, repo, "pytest -q", failed=[failure])
    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.agent_authored == (failure,)
    assert assessment.new_failures == ()
    assert state.last_verification_passed is False  # Advisory changes do not erase real failures.


def test_unreadable_or_post_edit_run_is_not_advisory_baseline(repo: Path):
    state = _scoped_state(repo, touched=["tests/test_existing.py"])
    _edit(state, repo, "src/app.py")
    _run_tests(state, repo, "pytest -q")
    assert state.has_blast_radius_baseline() is False
    assert state.blast_radius_baseline_command() == ""
    assert "No usable baseline covers:" in _advisory(state)
    assert "pytest" not in _advisory(state)  # No framework assumption from .py alone.
    assert (
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute").status
        == BlastRadiusStatus.UNATTRIBUTED
    )


@pytest.mark.parametrize("baseline_after_creation", [False, True])
@pytest.mark.parametrize("one_shot", [False, True])
def test_first_edit_advisory_uses_observed_unittest_and_honest_created_file_coverage(
    tmp_path: Path,
    baseline_after_creation: bool,
    one_shot: bool,
):
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_smoke.py").write_text("import app\n")
    command = "python3 -m unittest discover -s tests -v"
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            verify_commands=[command],
            extra_fields={"shell_sandbox": {"mode": "off"}},
        ),
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

    def run(_args):
        added = (tmp_path / "tests/test_app.py").is_file()
        output = "test_ok (test_smoke.Checks.test_ok) ... ok\n"
        if added:
            output += "test_added (test_app.Checks.test_added) ... ok\n"
        output += f"Ran {2 if added else 1} tests in 0.001s\n\nOK\n"
        return {
            "cmd": command,
            "effective_cmd": command,
            "exit_code": 0,
            "stdout": "",
            "stderr": output,
        }

    session.tools["shell_run"] = ToolDef(
        name="shell_run",
        description="Run tests.",
        parameters={"type": "object", "properties": {}},
        run=run,
    )
    session.tool_list = [t.as_openai_tool() for t in session.tools.values()]
    baseline = ToolCall(id="baseline", name="shell_run", arguments={"cmd": command})
    new_test = ToolCall(
        id="new-test",
        name="fs_write",
        arguments={
            "path": "tests/test_app.py",
            "content": "import unittest\nimport app\nclass Checks(unittest.TestCase):\n    def test_added(self):\n        self.assertEqual(app.VALUE, 2)\n",
        },
    )
    first = [new_test, baseline] if baseline_after_creation else [baseline, new_test]
    session.client = ScriptedClient(
        [
            *[LLMResponse(content="", tool_calls=[call], raw={}) for call in first],
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="fs_write",
                        arguments={"path": "app.py", "content": "VALUE = 2\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="verify", name="shell_run", arguments={"cmd": command})],
                raw={},
            ),
            LLMResponse(
                content="Updated app.VALUE and added its regression test. The recorded unittest run passed.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    try:
        session.run_turn(
            "Change app.VALUE to 2, add a useful regression test and run the configured checks."
        )
        events = list(read_session_events(session.store.path))
    finally:
        session.close()
    notices = [e["payload"] for e in events if e["type"] == "blast_radius_scope_advisory"]
    assert len(notices) == 1
    notice = notices[0]
    # Discovery records the files that existed at execution time. A command
    # whose recorded selection predates this new file is not advertised as
    # proven coverage of the expanded scope.
    assert notice["suggested_command"] == (command if baseline_after_creation else "")
    assert (f"for example `{command}`" in notice["message"]) is baseline_after_creation
    assert "pytest" not in notice["message"]
    assert notice["has_baseline"] is baseline_after_creation
    assert ("tests/test_app.py" in notice["baseline_covered_paths"]) is baseline_after_creation
    assert (
        "Newly authored tests were not part" in notice["message"]
    ) is not baseline_after_creation
    assert not [e for e in events if e["type"] == "completion_gate_nudge"]
