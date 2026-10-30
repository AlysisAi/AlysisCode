"""Reproduction-first protocol (verification step 5).

Mirrors the pure-logic + gate-integration style of ``test_regression_baseline.py``
and ``test_turn_contract_v2.py``. Most tests here are pure or gate-level (no LLM):
the tool-effect capture is driven through ``_record_tool_effect`` on a synthetic
buggy repo, so the protocol's facts are exercised without a provider. The scripted
sessions at the bottom cover the turn-level wiring (directive, summary, cleanup).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.completion_certificate import (
    CompletionCertificateInput,
    CompletionCertificateStatus,
    evaluate_completion_certificate,
)
from alysis_code.agent.reproduction_first import (
    MAX_REPRO_REVISION_ROUNDS,
    REPRO_NOT_REPRODUCING_ADVISORY,
    ReproAssessment,
    ReproPhase,
    ReproRun,
    ReproStatus,
    TaskShape,
    _reproduction_first_enabled,
    assess_reproduction,
    build_repro_artifacts_nudge_line,
    build_repro_nudge_line,
    build_repro_pre_edit_advisory,
    build_repro_status_summary,
    classify_repro_phase,
    is_delivered_test_path,
    match_repro_artifacts,
    repro_blocks_finalization,
    surviving_repro_artifacts,
    task_shape_from_turn_semantics,
)
from alysis_code.agent.turn_contract import TurnOutcome, TurnSemantics, TurnTaskShape
from alysis_code.agent.verification import (
    TurnExecutionState,
    _completion_gate_nudge_message,
    _completion_gate_problems,
    _completion_gate_repair_stage,
    _record_tool_effect,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult


@pytest.fixture
def approved_scripted_tools(monkeypatch):
    monkeypatch.setattr(
        NoopSurface,
        "request_approval",
        lambda self, request: ApprovalDecision(request.kind in {"shell_run", "verify_run"}),
    )


# ---------------------------------------------------------------------------
# Synthetic buggy repo + tool-effect driver
# ---------------------------------------------------------------------------

_BUG_REPORT = (
    "slugify() drops trailing dashes. Calling slugify('a--b--') returns 'a-b' but "
    "the expected output is 'a-b-'. See src/textutil.py."
)


@pytest.fixture()
def buggy_repo(tmp_path: Path) -> Path:
    """A tiny project with one defective function, no benchmark shape at all."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "textutil.py").write_text(
        "def slugify(value):\n    return value.replace('--', '-').rstrip('-')\n",
        encoding="utf-8",
    )
    return tmp_path


def _write_file(state: TurnExecutionState, root: Path, path: str, *, created: bool) -> None:
    """Drive a successful ``fs_write`` through the real tool-effect recorder."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# content\n", encoding="utf-8")
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="fs_write",
        arguments={"path": path},
        status="ok",
        result={"path": path, "created": created},
        known_verification_commands=[],
    )


def _run_command(
    state: TurnExecutionState,
    root: Path,
    command: str,
    *,
    exit_code: int,
) -> None:
    """Drive a ``shell_run`` result through the real tool-effect recorder."""
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok" if exit_code == 0 else "failed",
        result={"cmd": command, "exit_code": exit_code, "stdout": "", "stderr": ""},
        known_verification_commands=[],
    )


def _bugfix_state() -> TurnExecutionState:
    state = TurnExecutionState(execution_requested=True)
    state.repro_task_shape = TaskShape.BUG_FIX
    return state


# ---------------------------------------------------------------------------
# Test 1: task-shape classification (bug-fix vs feature/docs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("semantic_shape", "expected"),
    [
        (TurnTaskShape.BUG_FIX, TaskShape.BUG_FIX),
        (TurnTaskShape.IMPROVEMENT, TaskShape.OTHER),
        (TurnTaskShape.GENERAL, TaskShape.OTHER),
        (TurnTaskShape.UNKNOWN, TaskShape.OTHER),
    ],
)
def test_repro_task_shape_maps_validated_semantics(
    semantic_shape: TurnTaskShape,
    expected: TaskShape,
) -> None:
    semantics = TurnSemantics(outcome=TurnOutcome.CHANGE, task_shape=semantic_shape)
    assert task_shape_from_turn_semantics(semantics) is expected


def test_missing_semantic_contract_is_not_bug_fix_shaped() -> None:
    assert task_shape_from_turn_semantics(None) is TaskShape.OTHER


# ---------------------------------------------------------------------------
# Test 2: artifact matching and phase classification (facts, not heuristics)
# ---------------------------------------------------------------------------


def test_command_running_an_agent_created_file_matches_it() -> None:
    assert match_repro_artifacts("python repro_slug.py", {"repro_slug.py"}) == ("repro_slug.py",)
    assert match_repro_artifacts("sh repro_slug.py", {"repro_slug.py"}) == ("repro_slug.py",)
    assert match_repro_artifacts("sh -ec 'python repro_slug.py'", {"repro_slug.py"}) == (
        "repro_slug.py",
    )
    assert match_repro_artifacts(
        "bash -o pipefail checks/repro_slug.sh", {"checks/repro_slug.sh"}
    ) == ("checks/repro_slug.sh",)
    assert match_repro_artifacts(
        "pytest tests/repro_slug.py::test_case -q", {"tests/repro_slug.py"}
    ) == ("tests/repro_slug.py",)
    # Run from the directory holding the artifact: segment boundary still matches.
    assert match_repro_artifacts("python repro_slug.py", {"tmp/repro_slug.py"}) == (
        "tmp/repro_slug.py",
    )


def test_inline_code_path_literals_are_not_executed_repro_artifacts() -> None:
    command = (
        'python -c "import subprocess,sys; from pathlib import Path; '
        "subprocess.run([sys.executable,'checks.py'], check=True); "
        "text=Path('REVIEW_RESPONSES.md').read_text(); "
        "assert 'T1' in text and 'T2' in text\""
    )

    assert match_repro_artifacts(command, {"REVIEW_RESPONSES.md"}) == ()
    assert match_repro_artifacts("python -c 'repro_slug.py'", {"repro_slug.py"}) == ()
    assert match_repro_artifacts("sh -ec \"python -c 'repro_slug.py'\"", {"repro_slug.py"}) == ()
    assert (
        match_repro_artifacts(
            "env PYTHON=/usr/bin/python python -c 'repro_slug.py'", {"repro_slug.py"}
        )
        == ()
    )
    assert (
        match_repro_artifacts(
            "bash -o pipefail -c \"python -c 'repro_slug.py'\"", {"repro_slug.py"}
        )
        == ()
    )
    assert match_repro_artifacts("ruby -we 'repro_slug.py'", {"repro_slug.py"}) == ()


def test_unrelated_command_matches_no_artifact() -> None:
    assert match_repro_artifacts("pytest tests/test_auth.py", {"repro_slug.py"}) == ()
    assert match_repro_artifacts("python repro_slug.py", set()) == ()
    # A same-named file in a different tree is not the artifact.
    assert match_repro_artifacts("python other/repro.py", {"scratch/repro.py"}) == ()


def test_phase_is_pre_fix_until_a_pre_existing_path_is_modified() -> None:
    phase, product = classify_repro_phase(
        touched_repo_paths={"repro_slug.py"},
        created_paths={"repro_slug.py"},
    )
    assert phase == ReproPhase.PRE_FIX
    assert product == ()

    phase, product = classify_repro_phase(
        touched_repo_paths={"repro_slug.py", "src/textutil.py"},
        created_paths={"repro_slug.py"},
    )
    assert phase == ReproPhase.POST_FIX
    assert product == ("src/textutil.py",)


def test_new_files_the_agent_authored_are_not_product_edits() -> None:
    """Adding a failing test alongside the repro must not look like a fix."""
    phase, product = classify_repro_phase(
        touched_repo_paths={"repro_slug.py", "tests/test_slug.py"},
        created_paths={"repro_slug.py", "tests/test_slug.py"},
    )
    assert phase == ReproPhase.PRE_FIX
    assert product == ()


def test_writing_a_new_test_before_the_fix_keeps_the_run_pre_fix(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _write_file(state, buggy_repo, "tests/test_slug.py", created=True)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=1)

    assert state.repro_runs[0].phase == ReproPhase.PRE_FIX
    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == ReproStatus.FAILING_PRE_FIX


# ---------------------------------------------------------------------------
# Test (a): repro created and confirmed failing before the first product edit
# ---------------------------------------------------------------------------


def test_repro_confirmed_failing_before_first_product_edit(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=1)

    assert len(state.repro_runs) == 1
    run = state.repro_runs[0]
    assert run.phase == ReproPhase.PRE_FIX
    assert run.passed is False
    assert run.artifact_paths == ("repro_slug.py",)
    assert state.repro_artifact_paths == {"repro_slug.py"}

    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == ReproStatus.FAILING_PRE_FIX
    assert assessment.failing_pre_fix_command == "python repro_slug.py"


def test_full_protocol_reaches_passing_post_fix(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=1)
    _write_file(state, buggy_repo, "src/textutil.py", created=False)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=0)

    assert [run.phase for run in state.repro_runs] == [ReproPhase.PRE_FIX, ReproPhase.POST_FIX]
    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == ReproStatus.PASSING_POST_FIX
    assert assessment.satisfied is True
    assert repro_blocks_finalization(assessment, material_edit_count=2) is False


def test_repro_written_after_the_fix_never_validates_the_symptom(buggy_repo: Path) -> None:
    """The failure mode this step exists for: patch first, then a repro that passes."""
    state = _bugfix_state()
    _write_file(state, buggy_repo, "src/textutil.py", created=False)
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=0)

    assert state.repro_runs[0].phase == ReproPhase.POST_FIX
    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == ReproStatus.PASSING_UNVALIDATED
    assert repro_blocks_finalization(assessment, material_edit_count=2) is True


def test_still_failing_after_the_fix_is_a_contradiction() -> None:
    runs = [
        ReproRun("python repro.py", ("repro.py",), ReproPhase.PRE_FIX, passed=False, exit_code=1),
        ReproRun("python repro.py", ("repro.py",), ReproPhase.POST_FIX, passed=False, exit_code=1),
    ]
    assessment = assess_reproduction(runs=runs, applicable=True)
    assert assessment.status == ReproStatus.FAILING_POST_FIX
    assert assessment.contradicted is True


def test_run_with_no_observable_exit_code_is_not_recorded(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _record_tool_effect(
        root=buggy_repo,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": "python repro_slug.py"},
        status="ok",
        result={"cmd": "python repro_slug.py", "stdout": ""},
        known_verification_commands=[],
    )
    assert state.repro_runs == []


# ---------------------------------------------------------------------------
# Test (b): a not-failing repro triggers revision, not code edits
# ---------------------------------------------------------------------------


def test_passing_pre_fix_repro_reports_not_reproducing(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=0)

    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == ReproStatus.NOT_REPRODUCING
    assert assessment.needs_revision is True
    # The advisory tells the agent to revise the reproduction, never to edit code.
    advisory = build_repro_pre_edit_advisory(status=ReproStatus.NOT_REPRODUCING)
    assert advisory == REPRO_NOT_REPRODUCING_ADVISORY
    assert "revise the reproduction" in advisory
    assert "not a signal to start editing" in advisory


def test_repro_revision_rounds_are_bounded() -> None:
    runs = [
        ReproRun("python repro.py", ("repro.py",), ReproPhase.PRE_FIX, passed=True, exit_code=0)
    ]
    within = assess_reproduction(runs=runs, applicable=True, revision_rounds=1)
    assert within.needs_revision is True
    exhausted = assess_reproduction(
        runs=runs,
        applicable=True,
        revision_rounds=MAX_REPRO_REVISION_ROUNDS,
    )
    assert exhausted.status == ReproStatus.NOT_REPRODUCING
    assert exhausted.needs_revision is False


def test_not_reproducing_still_blocks_finalization() -> None:
    runs = [
        ReproRun("python repro.py", ("repro.py",), ReproPhase.PRE_FIX, passed=True, exit_code=0)
    ]
    assessment = assess_reproduction(runs=runs, applicable=True, revision_rounds=99)
    assert repro_blocks_finalization(assessment, material_edit_count=1) is True


# ---------------------------------------------------------------------------
# Test (c): no repro artifacts in the final diff
# ---------------------------------------------------------------------------


def test_surviving_artifacts_detects_scaffolding_left_in_the_tree(tmp_path: Path) -> None:
    (tmp_path / "repro_slug.py").write_text("x = 1\n", encoding="utf-8")
    assert surviving_repro_artifacts(tmp_path, ["repro_slug.py"]) == ("repro_slug.py",)
    (tmp_path / "repro_slug.py").unlink()
    assert surviving_repro_artifacts(tmp_path, ["repro_slug.py"]) == ()


def test_a_reproduction_written_as_a_real_test_is_not_scaffolding(tmp_path: Path) -> None:
    """A new test the task wanted delivered must never be demanded for deletion."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slug.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (tmp_path / "repro_slug.py").write_text("x = 1\n", encoding="utf-8")

    assert is_delivered_test_path("tests/test_slug.py") is True
    assert is_delivered_test_path("src/textutil.py") is False
    assert is_delivered_test_path("repro_slug.py") is False
    assert surviving_repro_artifacts(tmp_path, ["tests/test_slug.py", "repro_slug.py"]) == (
        "repro_slug.py",
    )


def test_surviving_artifacts_ignores_paths_outside_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    try:
        assert surviving_repro_artifacts(tmp_path / "repo", ["../elsewhere.py"]) == ()
    finally:
        outside.unlink()


def test_leftover_scaffolding_blocks_the_gate(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=1)
    _write_file(state, buggy_repo, "src/textutil.py", created=False)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=0)
    state.repro_surviving_artifacts = surviving_repro_artifacts(
        buggy_repo,
        state.repro_artifact_paths,
    )

    problems = _completion_gate_problems(
        state=state,
        final_text="Fixed the trailing dash.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        reproduction_first_enabled=True,
    )
    assert "repro_artifacts_present" in problems
    assert "repro_unconfirmed" not in problems
    assert _completion_gate_repair_stage(problems) == "repro_unconfirmed"

    (buggy_repo / "repro_slug.py").unlink()
    state.repro_surviving_artifacts = surviving_repro_artifacts(
        buggy_repo,
        state.repro_artifact_paths,
    )
    cleaned = _completion_gate_problems(
        state=state,
        final_text="Fixed the trailing dash.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        reproduction_first_enabled=True,
    )
    assert "repro_artifacts_present" not in cleaned
    assert "repro_unconfirmed" not in cleaned


# ---------------------------------------------------------------------------
# Test (d): the summary states the repro status
# ---------------------------------------------------------------------------


def test_summary_reports_a_satisfied_protocol_plainly() -> None:
    assessment = ReproAssessment(
        status=ReproStatus.PASSING_POST_FIX,
        applicable=True,
        failing_pre_fix_command="python repro_slug.py",
        latest_post_fix_command="python repro_slug.py",
    )
    summary = build_repro_status_summary(assessment)
    assert "failed before the fix, passes after it" in summary
    assert "python repro_slug.py" in summary


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ReproStatus.NOT_ATTEMPTED, "none was run"),
        (ReproStatus.NOT_REPRODUCING, "could not reproduce the reported symptom"),
        (ReproStatus.FAILING_PRE_FIX, "never re-run afterwards"),
        (ReproStatus.FAILING_POST_FIX, "STILL FAILS after it"),
        (ReproStatus.PASSING_UNVALIDATED, "never observed failing before the fix"),
    ],
)
def test_summary_states_every_unsatisfied_status_explicitly(
    status: ReproStatus,
    expected: str,
) -> None:
    summary = build_repro_status_summary(ReproAssessment(status=status, applicable=True))
    assert expected in summary
    assert summary.startswith("\n\n---\n⚠️")


def test_summary_is_empty_for_a_non_applicable_turn() -> None:
    assert build_repro_status_summary(ReproAssessment(applicable=False)) == ""


def test_summary_surfaces_the_guardrail_signals() -> None:
    assessment = ReproAssessment(
        status=ReproStatus.PASSING_POST_FIX,
        applicable=True,
        edited_after_fix=("repro_slug.py",),
        surviving_artifacts=("repro_slug.py",),
    )
    summary = build_repro_status_summary(assessment)
    assert "edited after the fix (repro_slug.py)" in summary
    assert "scaffolding left in the tree: repro_slug.py" in summary


def test_editing_the_repro_after_the_fix_is_recorded(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "repro_slug.py", created=True)
    _run_command(state, buggy_repo, "python repro_slug.py", exit_code=1)
    _write_file(state, buggy_repo, "src/textutil.py", created=False)
    assert state.repro_artifacts_edited_after_fix == set()
    _write_file(state, buggy_repo, "repro_slug.py", created=False)
    assert state.repro_artifacts_edited_after_fix == {"repro_slug.py"}

    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.edited_after_fix == ("repro_slug.py",)


# ---------------------------------------------------------------------------
# Test (e): non-bugfix tasks skip the repro phase cleanly
# ---------------------------------------------------------------------------


def test_non_bugfix_turn_never_enters_the_protocol(buggy_repo: Path) -> None:
    state = TurnExecutionState(execution_requested=True)
    state.repro_task_shape = task_shape_from_turn_semantics(
        TurnSemantics(
            outcome=TurnOutcome.CHANGE,
            task_shape=TurnTaskShape.IMPROVEMENT,
        )
    )
    assert state.repro_task_shape == TaskShape.OTHER

    _write_file(state, buggy_repo, "scratch_demo.py", created=True)
    _run_command(state, buggy_repo, "python scratch_demo.py", exit_code=0)

    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.applicable is False
    assert state.latest_repro_assessment == {}
    assert build_repro_status_summary(assessment) == ""

    problems = _completion_gate_problems(
        state=state,
        final_text="Added the flag.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        reproduction_first_enabled=True,
    )
    assert "repro_unconfirmed" not in problems
    assert "repro_artifacts_present" not in problems


def test_read_only_turn_skips_the_protocol() -> None:
    state = _bugfix_state()
    assert state.compute_repro_assessment(enabled=True, turn_intent="").applicable is False


def test_turn_with_no_material_edits_does_not_block_on_the_repro() -> None:
    state = _bugfix_state()
    assessment = state.compute_repro_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == ReproStatus.NOT_ATTEMPTED
    assert repro_blocks_finalization(assessment, material_edit_count=0) is False


# ---------------------------------------------------------------------------
# Completion-gate integration
# ---------------------------------------------------------------------------


def test_unvalidated_symptom_blocks_the_completion_gate(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "src/textutil.py", created=False)

    problems = _completion_gate_problems(
        state=state,
        final_text="Fixed the trailing dash.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        reproduction_first_enabled=True,
    )
    assert "repro_unconfirmed" in problems
    assert _completion_gate_repair_stage(problems) == "repro_unconfirmed"


def test_kill_switch_reverts_the_gate_policy(buggy_repo: Path) -> None:
    state = _bugfix_state()
    _write_file(state, buggy_repo, "src/textutil.py", created=False)

    problems = _completion_gate_problems(
        state=state,
        final_text="Fixed the trailing dash.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        reproduction_first_enabled=False,
    )
    assert "repro_unconfirmed" not in problems
    assert state.latest_repro_assessment == {}


def test_blocked_finalization_is_exempt() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="blocked: no network access",
            blocked=True,
            blocker_valid=True,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=0,
            last_verification_passed=None,
            reproduction_first_enabled=True,
            repro_unconfirmed=True,
            repro_status=ReproStatus.NOT_ATTEMPTED.value,
        )
    )
    assert "repro_unconfirmed" not in certificate.problems


def test_certificate_ranks_a_failing_post_fix_repro_as_contradicted() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="Fixed.",
            blocked=False,
            blocker_valid=False,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=0,
            last_verification_passed=None,
            reproduction_first_enabled=True,
            repro_unconfirmed=True,
            repro_failing_after_fix=True,
            repro_status=ReproStatus.FAILING_POST_FIX.value,
        )
    )
    assert "repro_unconfirmed" in certificate.problems
    assert certificate.status == CompletionCertificateStatus.CONTRADICTED
    assert certificate.repro_status == ReproStatus.FAILING_POST_FIX.value


def test_unconfirmed_repro_alone_is_insufficient_not_contradicted() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="Fixed.",
            blocked=False,
            blocker_valid=False,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=0,
            last_verification_passed=None,
            reproduction_first_enabled=True,
            repro_unconfirmed=True,
            repro_status=ReproStatus.NOT_ATTEMPTED.value,
        )
    )
    assert certificate.status == CompletionCertificateStatus.INSUFFICIENT


def test_gate_nudge_names_the_concrete_repro_deficit() -> None:
    assessment = ReproAssessment(status=ReproStatus.FAILING_POST_FIX, applicable=True)
    nudge = _completion_gate_nudge_message(
        ["repro_unconfirmed"],
        repro_assessment=assessment,
    )
    assert "still fails after the fix" in nudge
    assert "Do not weaken or delete the reproduction" in nudge
    assert "Run the relevant tests now" in nudge


def test_gate_nudge_names_leftover_scaffolding() -> None:
    assessment = ReproAssessment(
        status=ReproStatus.PASSING_POST_FIX,
        applicable=True,
        surviving_artifacts=("repro_slug.py",),
    )
    nudge = _completion_gate_nudge_message(
        ["repro_artifacts_present"],
        repro_assessment=assessment,
    )
    assert "repro_slug.py" in nudge
    assert "Delete it" in nudge


def test_nudge_line_builders_are_empty_when_nothing_applies() -> None:
    assert build_repro_nudge_line(ReproAssessment(status=ReproStatus.PASSING_POST_FIX)) == ""
    assert build_repro_artifacts_nudge_line(()) == ""


# ---------------------------------------------------------------------------
# Kill-switch resolution
# ---------------------------------------------------------------------------


def test_kill_switch_env_wins_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AppConfig()
    assert _reproduction_first_enabled(cfg) is True

    cfg.reproduction_first_enabled = False
    assert _reproduction_first_enabled(cfg) is False

    monkeypatch.setenv("ALYSIS_REPRODUCTION_FIRST", "on")
    assert _reproduction_first_enabled(cfg) is True
    monkeypatch.setenv("ALYSIS_REPRODUCTION_FIRST", "off")
    cfg.reproduction_first_enabled = True
    assert _reproduction_first_enabled(cfg) is False


def test_config_key_round_trips() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "reproduction_first_enabled", "false")
    assert cfg.reproduction_first_enabled is False
    cfg = set_config_value(cfg, "reproduction_first_enabled", "true")
    assert cfg.reproduction_first_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "reproduction_first_enabled", "maybe")


# ---------------------------------------------------------------------------
# Scripted sessions: turn-level wiring (directive, summary, cleanup)
# ---------------------------------------------------------------------------


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.seen_system_prompts: list[str] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = tools, stream, on_text_delta, temperature
        self.seen_system_prompts.extend(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        )
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


def _events(sessions_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return list(read_session_events(sessions_dir / f"{session_id}.jsonl"))


def _event_types(sessions_dir: Path, session_id: str) -> set[str]:
    return {str(event.get("type") or "") for event in _events(sessions_dir, session_id)}


def _final_text(sessions_dir: Path, session_id: str) -> str:
    finals = [event for event in _events(sessions_dir, session_id) if event.get("type") == "final"]
    assert finals, "the turn produced no final event"
    return str((finals[-1].get("payload") or {}).get("content") or "")


def _fake_shell_run_tracking_the_fix(root: Path):
    """A ``shell_run`` stand-in whose exit code follows the actual source state.

    The reproduction "fails" while the defect is present and "passes" once it is
    gone, so the scripted run exercises the real pre/post ordering rather than a
    hard-coded pair of exit codes.
    """

    def fake_shell_run(*, root: Path, cmd: str, cwd: str | None = None, runner=None):
        _ = cwd, runner
        source = (root / "src" / "textutil.py").read_text(encoding="utf-8")
        exit_code = 1 if "rstrip" in source else 0
        return {
            "cmd": cmd,
            "effective_cmd": cmd,
            "exit_code": exit_code,
            "stdout": "",
            "stderr": "AssertionError\n" if exit_code else "",
        }

    _ = root
    return fake_shell_run


def _session(tmp_path: Path, session_id: str, *, task_shape: str = "bug_fix") -> Any:
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", verify_commands=["pytest -q"]),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=tmp_path / "sessions",
        session_id_override=session_id,
    )
    _ = task_shape
    return session


_REPRO_SOURCE = "from src.textutil import slugify\nassert slugify('a--b--') == 'a-b-'\n"


def test_scripted_run_reports_a_satisfied_reproduction(
    buggy_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved_scripted_tools,
) -> None:
    """Repro first, fix second, repro passes: engagement is recorded and exit is clean.

    Router-free path: there is no predicted task shape and no host-appended
    status line; the observed repro runs are the protocol's record and the
    engagement-based gate stays satisfied.
    """
    monkeypatch.setattr(agent_loop_mod, "shell_run", _fake_shell_run_tracking_the_fix(buggy_repo))
    session = _session(buggy_repo, "repro-satisfied")
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "repro_slug.py", "content": _REPRO_SOURCE},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc2", name="shell_run", arguments={"cmd": "python repro_slug.py"})
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc3",
                        name="fs_write",
                        arguments={
                            "path": "src/textutil.py",
                            "content": "def slugify(value):\n    return value.replace('--', '-')\n",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc4", name="shell_run", arguments={"cmd": "python repro_slug.py"})
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc5", name="fs_delete", arguments={"path": "repro_slug.py"})
                ],
                raw={},
            ),
            LLMResponse(content="Fixed slugify in src/textutil.py.", tool_calls=[], raw={}),
        ]
    )
    try:
        exit_code = session.run_turn(_BUG_REPORT)
    finally:
        session.close()

    emitted = _final_text(buggy_repo / "sessions", "repro-satisfied")
    assert exit_code == 0
    # (d) the final text is the model's own summary; no host-appended status line.
    assert emitted.startswith("Fixed slugify in src/textutil.py.")
    assert "failed before the fix, passes after it" not in emitted
    # (a) the conditional directive reached the model before any edit.
    assert any(
        "Reproduction-first protocol" in prompt for prompt in session.client.seen_system_prompts
    )
    # (c) no scaffolding survives in the delivered tree.
    assert not (buggy_repo / "repro_slug.py").exists()

    types = _event_types(buggy_repo / "sessions", "repro-satisfied")
    # No predicted task shape exists on the router-free path; the observed
    # repro runs are the protocol record and the gate never blocks.
    assert "reproduction_first_task_shape" not in types
    assert "repro_run_observed" in types
    assert "one_shot_completion_gate_repro_unconfirmed" not in types


def test_supplemental_check_after_silent_authoritative_verify_finalizes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved_scripted_tools,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "title.py").write_text(
        "def normalize_title(value):\n    return value.strip().lower()\n",
        encoding="utf-8",
    )
    (tmp_path / "checks.py").write_text("# host-owned checks\n", encoding="utf-8")
    command = (
        'python -c "import subprocess,sys; from pathlib import Path; '
        "subprocess.run([sys.executable,'checks.py'], check=True); "
        "text=Path('REVIEW_RESPONSES.md').read_text(); "
        "assert 'T1' in text and 'T2' in text\""
    )

    def fake_shell_run(*, root: Path, cmd: str, cwd: str | None = None, runner=None):
        _ = cwd, runner
        source = (root / "src" / "title.py").read_text(encoding="utf-8")
        exit_code = 1 if ".lower()" in source else 0
        return {
            "cmd": cmd,
            "effective_cmd": cmd,
            "exit_code": exit_code,
            "stdout": "",
            "stderr": "AssertionError\n" if exit_code else "",
        }

    def silent_verify(
        *,
        root: Path,
        commands: list[str],
        artifact_path: Path,
        cfg: AppConfig,
    ) -> VerifyRunResult:
        _ = root, cfg
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("", encoding="utf-8")
        return VerifyRunResult(
            commands=list(commands),
            command_results=[
                VerifyCommandResult(
                    command=item,
                    exit_code=0,
                    output="",
                    real_execution=True,
                )
                for item in commands
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(agent_loop_mod, "shell_run", fake_shell_run)
    monkeypatch.setattr(agent_loop_mod, "run_task_verification", silent_verify)
    session_id = "required-output-silent-authoritative"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        authoritative_verification_commands=[command],
        session_log_dir_override=tmp_path / "sessions",
        session_id_override=session_id,
    )
    final_text = "Preserved title case and resolved T1 and T2. Verification passed."
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "repro_title_case.py", "content": "assert False\n"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="shell_run",
                        arguments={"cmd": "python repro_title_case.py"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc3",
                        name="fs_write",
                        arguments={
                            "path": "src/title.py",
                            "content": "def normalize_title(value):\n    return value.strip()\n",
                        },
                    ),
                    ToolCall(
                        id="tc4",
                        name="fs_write",
                        arguments={
                            "path": "REVIEW_RESPONSES.md",
                            "content": "# Review Responses\n\nT1 resolved.\nT2 resolved.\n",
                        },
                    ),
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc5",
                        name="shell_run",
                        arguments={"cmd": "python repro_title_case.py"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc6",
                        name="fs_delete",
                        arguments={"path": "repro_title_case.py"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc7",
                        name="verify_run",
                        arguments={"commands": [command]},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc8",
                        name="shell_run",
                        arguments={"cmd": "python checks.py"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content=final_text, tool_calls=[], raw={}),
        ]
    )

    try:
        exit_code = session.run_turn(
            "Fix normalize_title so it preserves case, run python checks.py, and write "
            "REVIEW_RESPONSES.md mapping T1 and T2 to their resolutions."
        )
    finally:
        session.close()

    events = _events(tmp_path / "sessions", session_id)
    finalizations = [event for event in events if event.get("type") == "turn_intent_finalized"]
    verify_results = [
        event
        for event in events
        if event.get("type") == "tool_result"
        and (event.get("payload") or {}).get("name") == "verify_run"
    ]

    assert exit_code == 0
    assert session.client.calls == 8
    assert len(verify_results) == 1
    assert len(finalizations) == 1
    finalization = dict(finalizations[0].get("payload") or {})
    state = dict(finalization.get("state") or {})
    certificate = dict(state.get("completion_certificate") or {})
    assert certificate.get("status") == "SUFFICIENT"
    assert certificate.get("problems") == []
    assert certificate.get("repro_artifacts_present") == []
    assert set(certificate.get("covered_hard_criterion_ids") or []) == set(
        certificate.get("hard_criterion_ids") or []
    )
    assert finalization.get("acceptance_problems") == []
    criteria = list((finalization.get("acceptance_contract") or {}).get("criteria") or [])
    assert {
        criterion["kind"]: criterion["status"]
        for criterion in criteria
        if criterion["kind"]
        in {
            "required_artifact_path",
            "explicit_host_user_verification_command",
        }
    } == {
        "required_artifact_path": "PASSED",
        "explicit_host_user_verification_command": "PASSED",
    }
    assert state.get("repro_artifact_paths") == ["repro_title_case.py"]
    assert [run.get("artifact_paths") for run in state.get("repro_runs") or []] == [
        ["repro_title_case.py"],
        ["repro_title_case.py"],
    ]
    assert state.get("repro_surviving_artifacts") == []
    assert state.get("last_verification_passed") is True
    supplemental = list(state.get("supplemental_verification_evidence") or [])
    assert supplemental[-1]["normalized_command"] == "python checks.py"
    assert supplemental[-1]["observed_exit_code"] == 0
    assert not any(event.get("type") == "one_shot_completion_gate_failed" for event in events)
    assert not any(event.get("type") == "completion_gate_nudge" for event in events)
    assert not any(
        event.get("type") == "completion_gate_accepted_with_open_problems" for event in events
    )
    delivered = _final_text(tmp_path / "sessions", session_id)
    assert delivered.startswith(final_text + "\n\nPreserved verified checkpoint:")
    assert session.last_turn_outcome["verified_success"] is True
    checkpoint = session.last_turn_outcome["best_verified_checkpoint"]
    assert checkpoint["archive_path"] in delivered
    assert Path(checkpoint["archive_path"]).is_file()


def test_scripted_run_without_reproduction_engagement_binds_no_repro_gate(
    buggy_repo: Path,
) -> None:
    """Patch with no reproduction at all: the engagement-based protocol stays out.

    Router-free path: no task-shape prediction exists, so a bug-shaped request
    that never runs a reproduction produces no repro advisory, no repro gate
    stage, and no host-appended honesty line; the remaining honesty pressure
    comes from the verification/completion gate.
    """
    session = _session(buggy_repo, "repro-unvalidated")
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={
                            "path": "src/textutil.py",
                            "content": "def slugify(value):\n    return value.replace('--', '-')\n",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Fixed slugify in src/textutil.py.", tool_calls=[], raw={}),
        ]
    )
    try:
        session.run_turn(_BUG_REPORT)
    finally:
        session.close()

    emitted = _final_text(buggy_repo / "sessions", "repro-unvalidated")
    assert "Reproduction: none was run" not in emitted

    types = _event_types(buggy_repo / "sessions", "repro-unvalidated")
    assert "repro_run_observed" not in types
    assert "repro_pre_edit_advisory" not in types
    assert "one_shot_completion_gate_repro_unconfirmed" not in types


def test_scripted_non_bugfix_run_is_untouched_by_the_protocol(tmp_path: Path) -> None:
    """(e) A feature task gets only the conditional directive; nothing engages.

    Router-free path: every execute-capable turn carries the conditional
    reproduction-first directive (which itself says to skip symptom-free
    tasks); no advisory, no repro record, and no summary line appear.
    """
    (tmp_path / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    session = _session(tmp_path, "repro-feature-task", task_shape="improvement")
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={
                            "path": "cli.py",
                            "content": "def main(verbose=False):\n    return 0\n",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Added the --verbose flag to cli.py.", tool_calls=[], raw={}),
        ]
    )
    try:
        exit_code = session.run_turn("Add a --verbose flag to cli.py.")
    finally:
        session.close()

    emitted = _final_text(tmp_path / "sessions", "repro-feature-task")
    assert exit_code == 0
    assert "Reproduction" not in emitted
    assert any(
        "skip it for tasks that report no symptom" in prompt
        for prompt in session.client.seen_system_prompts
    )
    types = _event_types(tmp_path / "sessions", "repro-feature-task")
    assert "reproduction_first_task_shape" not in types
    assert "repro_run_observed" not in types
    assert "repro_pre_edit_advisory" not in types
