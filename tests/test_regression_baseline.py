from __future__ import annotations

import pytest

from alysis_code.agent.completion_certificate import (
    CompletionCertificateInput,
    CompletionCertificateStatus,
    evaluate_completion_certificate,
)
from alysis_code.agent.regression_baseline import (
    BaselineRecord,
    _regression_baseline_enabled,
    aggregate_regression_results,
    baseline_command_key,
    classify_regression_diff,
    command_is_test_runner,
    node_id_file_path,
    parse_pytest_report,
    parse_test_report,
    parse_unittest_report,
)
from alysis_code.agent.verification import (
    HONEST_UNVERIFIED_FINALIZATION_MARKER,
    TurnExecutionState,
    _completion_gate_nudge_message,
    _completion_gate_problems,
    _completion_gate_repair_stage,
    _record_tool_effect,
    build_regressions_unresolved_marker,
    build_unattributed_failures_marker,
)
from alysis_code.config import AppConfig, ConfigError, set_config_value

# ---------------------------------------------------------------------------
# Test 1: parsers (fact extraction; conservative on ambiguity; deterministic)
# ---------------------------------------------------------------------------

_PYTEST_MIXED = """\
=========================== short test summary info ============================
FAILED tests/test_a.py::test_one - assert 1 == 2
FAILED tests/test_a.py::test_two
ERROR tests/test_b.py::test_setup - ValueError: boom
==== 2 failed, 116 passed, 3 skipped, 1 xfailed, 1 xpassed, 1 error in 1.23s ====
"""

_UNITTEST_MIXED = """\
..F..E
======================================================================
FAIL: test_bar (tests.test_foo.TestFoo)
----------------------------------------------------------------------
Traceback (most recent call last):
AssertionError
======================================================================
ERROR: test_qux (tests.test_foo.TestFoo)
----------------------------------------------------------------------
Traceback (most recent call last):
ValueError
----------------------------------------------------------------------
Ran 6 tests in 0.003s

FAILED (failures=1, errors=1, skipped=2)
"""


def test_pytest_parser_extracts_ids_and_counts() -> None:
    report = parse_test_report(_PYTEST_MIXED)
    assert report.runner == "pytest"
    assert report.failed_ids == ("tests/test_a.py::test_one", "tests/test_a.py::test_two")
    assert report.error_ids == ("tests/test_b.py::test_setup",)
    assert report.failed == 2
    assert report.errors == 1
    assert report.passed == 116
    assert report.skipped == 3
    assert report.counts_known is True
    assert report.ids_complete is True
    # xfail / xpass / skipped are not failures.
    assert "xfailed" not in report.failing_ids


def test_pytest_parser_all_passed() -> None:
    report = parse_test_report("==================== 5 passed in 0.10s ====================")
    assert report.runner == "pytest"
    assert report.failed == 0
    assert report.errors == 0
    assert report.failing_ids == ()
    assert report.counts_known is True
    assert report.ids_complete is True
    assert report.usable_as_baseline is True


def test_pytest_no_tests_ran_is_counts_known() -> None:
    report = parse_test_report("========== no tests ran in 0.01s ==========")
    assert report.runner == "pytest"
    assert report.counts_known is True
    assert report.failing_ids == ()


def test_unittest_parser_extracts_ids_and_counts() -> None:
    report = parse_test_report(_UNITTEST_MIXED)
    assert report.runner == "unittest"
    assert report.failed_ids == ("test_bar (tests.test_foo.TestFoo)",)
    assert report.error_ids == ("test_qux (tests.test_foo.TestFoo)",)
    assert report.failed == 1
    assert report.errors == 1
    assert report.skipped == 2
    assert report.counts_known is True
    assert report.ids_complete is True


def test_unittest_ok_run() -> None:
    report = parse_test_report("...\n----\nRan 3 tests in 0.001s\n\nOK\n")
    assert report.runner == "unittest"
    assert report.failed == 0
    assert report.errors == 0
    assert report.counts_known is True


@pytest.mark.parametrize(
    "output",
    [
        "Ran 3 tests in 0.001s\n",
        "OK\nRan 3 tests in 0.001s\n",
        "Ran 2 tests in 0.001s\nOK\nRan 3 tests in 0.001s\n",
    ],
)
def test_unittest_missing_terminal_verdict_stays_unknown(output: str) -> None:
    report = parse_test_report("FAIL: test_value (tests.Example)\n" + output)
    assert report.runner == "unittest"
    assert report.failed_ids == ("test_value (tests.Example)",)
    assert report.failed is None and report.errors is None
    assert report.counts_known is False
    assert report.usable_as_baseline is False


def test_truncated_pytest_ids_incomplete_not_usable_as_baseline() -> None:
    # `pytest … | tail -1` keeps only the counts line: counts known, ids not.
    report = parse_test_report("==== 3 failed, 116 passed in 1.2s ====")
    assert report.counts_known is True
    assert report.failed == 3
    assert report.failed_ids == ()
    assert report.ids_complete is False
    assert report.usable_as_baseline is False


def test_garbled_output_is_counts_unknown_never_raises() -> None:
    for junk in ["", "   ", "totally unrelated text\nmore noise", "=== not a summary ==="]:
        report = parse_test_report(junk)
        assert report.counts_known is False
        assert report.runner == "unknown"
        assert report.failing_ids == ()


def test_non_test_output_returns_none_for_each_parser() -> None:
    assert parse_pytest_report("hello world") is None
    assert parse_unittest_report("hello world") is None


def test_parser_is_deterministic() -> None:
    assert parse_test_report(_PYTEST_MIXED) == parse_test_report(_PYTEST_MIXED)
    assert parse_test_report(_UNITTEST_MIXED) == parse_test_report(_UNITTEST_MIXED)


def test_pytest_param_id_with_spaced_dash_is_not_truncated() -> None:
    # Real pytest emits `test_param[a - 2]` whose param contains " - "; the
    # bracket-aware split must keep it whole, not truncate to `test_param[a`.
    text = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_p.py::test_x[a - 2] - AssertionError: assert 'a - 2'\n"
        "FAILED not-a-node-id line\n"
        "==================== 1 failed in 0.1s ====================\n"
    )
    report = parse_test_report(text)
    assert report.failed_ids == ("tests/test_p.py::test_x[a - 2]",)


def test_pytest_quiet_undecorated_counts_line() -> None:
    # pytest -q prints an undecorated final counts line (no '=' padding).
    text = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_q.py::test_a - AssertionError: boom\n"
        "2 failed, 3 passed, 1 skipped in 0.25s\n"
    )
    report = parse_test_report(text)
    assert report.runner == "pytest"
    assert report.failed == 2
    assert report.passed == 3
    assert report.skipped == 1
    assert report.failed_ids == ("tests/test_q.py::test_a",)


def test_pytest_stray_failed_line_outside_summary_is_ignored() -> None:
    # A `FAILED …` line in captured stdout must not become a phantom id; only the
    # short-summary section is scanned. Green run stays a usable baseline.
    text = (
        "captured stdout: FAILED tests/legacy.py::old - noise\n"
        "==================== 5 passed in 0.30s ====================\n"
    )
    report = parse_test_report(text)
    assert report.failed == 0
    assert report.failed_ids == ()
    assert report.ids_complete is True
    assert report.usable_as_baseline is True


# ---------------------------------------------------------------------------
# Test 2: baseline keying (comparability by identity, pipe-invariant)
# ---------------------------------------------------------------------------


def test_baseline_key_is_pipe_invariant() -> None:
    assert (
        baseline_command_key("pytest foo")
        == baseline_command_key("pytest foo | tail -40")
        == baseline_command_key("pytest foo | tail -5")
    )


def test_baseline_key_distinguishes_different_commands() -> None:
    assert baseline_command_key("pytest foo") != baseline_command_key("pytest bar")


def test_command_is_test_runner_recognizes_pytest_and_unittest_only() -> None:
    assert command_is_test_runner("pytest -q tests") is True
    assert command_is_test_runner("pytest tests | tail -5") is True
    assert command_is_test_runner("python -m pytest tests/x.py") is True
    assert command_is_test_runner("python -m unittest tests.foo") is True
    assert command_is_test_runner("python manage.py test app") is True
    assert command_is_test_runner("ruff check src") is False
    assert command_is_test_runner("./validate.sh") is False
    assert command_is_test_runner("echo hi") is False


def test_command_is_test_runner_strips_env_prefix_and_handles_paths() -> None:
    # Leading env-var assignment prefixes are stripped (common on SWE-bench).
    assert command_is_test_runner("PYTHONPATH=. pytest tests") is True
    assert command_is_test_runner("env FOO=bar PYTHONPATH=. pytest -q") is True
    # Windows venv path resolves to the pytest basename.
    assert command_is_test_runner(r"C:\venv\Scripts\pytest.exe tests") is True
    assert command_is_test_runner("C:/venv/Scripts/pytest.exe tests") is True
    # Glued -m form.
    assert command_is_test_runner("python -munittest discover") is True
    # An env prefix in front of a non-runner is still not a runner.
    assert command_is_test_runner("FOO=bar echo hi") is False


def test_command_is_test_runner_reads_quoted_paths_with_spaces() -> None:
    # Install folders often contain spaces ("Program Files", "Alysis Code", user names).
    assert command_is_test_runner(r'"C:\Program Files\Python312\python.exe" -m pytest -q') is True
    assert command_is_test_runner(r'"C:\Users\Jo Smith\venv\Scripts\pytest.exe" tests') is True
    assert command_is_test_runner(r'PYTHONPATH=. "C:\a b\python.exe" -m unittest discover') is True
    assert command_is_test_runner("'/opt/my tools/bin/pytest' -q") is True
    assert command_is_test_runner(r'"C:\Program Files\Git\bin\git.exe" status') is False
    assert command_is_test_runner('pytest "unbalanced') is False


# ---------------------------------------------------------------------------
# Test 3: diff classification (pre_existing / regression / unattributed /
# agent_authored, plus the mixed case and the sympy-12489 shape)
# ---------------------------------------------------------------------------


def _report(text: str):
    return parse_test_report(text)


_SUMMARY_HEADER = "=========================== short test summary info ============================"


def _pyout(counts: str, *, failed: tuple[str, ...] = (), errors: tuple[str, ...] = ()) -> str:
    """Build realistic pytest output: a short-summary section then a counts line."""
    lines: list[str] = []
    if failed or errors:
        lines.append(_SUMMARY_HEADER)
        lines.extend(f"FAILED {node}" for node in failed)
        lines.extend(f"ERROR {node}" for node in errors)
    lines.append(f"==================== {counts} ====================")
    return "\n".join(lines)


def _rep(counts: str, *, failed: tuple[str, ...] = (), errors: tuple[str, ...] = ()):
    return parse_test_report(_pyout(counts, failed=failed, errors=errors))


def _baseline(command: str, counts: str, *, failed: tuple[str, ...] = ()) -> BaselineRecord:
    return BaselineRecord(
        command=command,
        command_key=baseline_command_key(command),
        report=_rep(counts, failed=failed),
        edit_generation=0,
    )


def test_diff_pre_existing_only_sympy_shape_does_not_block() -> None:
    baseline = _baseline(
        "pytest m", "3 failed, 116 passed in 1s", failed=("m.py::a", "m.py::b", "m.py::c")
    )
    post = _rep("3 failed, 116 passed in 1s", failed=("m.py::a", "m.py::b", "m.py::c"))
    diff = classify_regression_diff(post_report=post, baseline=baseline, agent_created_paths=[])
    assert set(diff.pre_existing) == {"m.py::a", "m.py::b", "m.py::c"}
    assert diff.regressions == ()
    assert diff.blocks is False
    assert diff.all_failures_benign is True


def test_diff_regression() -> None:
    baseline = _baseline("pytest m", "2 failed, 117 passed in 1s", failed=("m.py::a", "m.py::b"))
    post = _rep("3 failed, 116 passed in 1s", failed=("m.py::a", "m.py::b", "m.py::c"))
    diff = classify_regression_diff(post_report=post, baseline=baseline, agent_created_paths=[])
    assert diff.regressions == ("m.py::c",)
    assert set(diff.pre_existing) == {"m.py::a", "m.py::b"}
    assert diff.blocks is True
    assert diff.baseline_command == "pytest m"


def test_diff_unattributed_when_no_baseline() -> None:
    post = _rep("1 failed, 1 passed in 1s", failed=("m.py::c",))
    diff = classify_regression_diff(post_report=post, baseline=None, agent_created_paths=[])
    assert diff.unattributed == ("m.py::c",)
    assert diff.regressions == ()
    assert diff.has_comparable_baseline is False


def test_diff_unattributed_when_baseline_truncated() -> None:
    # A truncated baseline (counts known, ids not) is not comparable.
    baseline = _baseline("pytest m", "2 failed, 117 passed in 1s")  # counts but no ids
    post = _rep("1 failed in 1s", failed=("m.py::c",))
    diff = classify_regression_diff(post_report=post, baseline=baseline, agent_created_paths=[])
    assert diff.unattributed == ("m.py::c",)
    assert diff.regressions == ()
    assert diff.has_comparable_baseline is False


def test_diff_agent_authored_wins_over_regression() -> None:
    baseline = _baseline("pytest m", "0 failed, 3 passed in 1s")
    post = _rep("1 failed, 2 passed in 1s", failed=("tests/test_new.py::test_repro",))
    diff = classify_regression_diff(
        post_report=post,
        baseline=baseline,
        agent_created_paths=["tests/test_new.py"],
    )
    assert diff.agent_authored == ("tests/test_new.py::test_repro",)
    assert diff.regressions == ()


def test_diff_agent_created_basename_does_not_collide_with_existing_file() -> None:
    # A created bare `test_models.py` must NOT mark a genuine regression in a
    # different pre-existing `pkg/tests/test_models.py` as agent_authored.
    baseline = _baseline("pytest m", "0 failed, 3 passed in 1s")
    post = _rep("1 failed, 2 passed in 1s", failed=("pkg/tests/test_models.py::test_x",))
    diff = classify_regression_diff(
        post_report=post,
        baseline=baseline,
        agent_created_paths=["test_models.py"],
    )
    assert diff.agent_authored == ()
    assert diff.regressions == ("pkg/tests/test_models.py::test_x",)


def test_diff_mixed_case() -> None:
    baseline = _baseline("pytest m", "1 failed, 5 passed in 1s", failed=("pkg/test_a.py::pre",))
    post = _rep(
        "3 failed, 3 passed in 1s",
        failed=(
            "pkg/test_a.py::pre",
            "pkg/test_a.py::newbreak",
            "pkg/test_repro.py::authored",
        ),
    )
    diff = classify_regression_diff(
        post_report=post,
        baseline=baseline,
        agent_created_paths=["pkg/test_repro.py"],
    )
    assert diff.pre_existing == ("pkg/test_a.py::pre",)
    assert diff.regressions == ("pkg/test_a.py::newbreak",)
    assert diff.agent_authored == ("pkg/test_repro.py::authored",)
    assert diff.unattributed == ()


def test_aggregate_precedence_across_runs() -> None:
    # Same id pre-existing in one comparable run and "regression" in a run with
    # no baseline: precedence keeps it pre-existing (never a false regression).
    comparable = classify_regression_diff(
        post_report=_rep("1 failed in 1s", failed=("m.py::x",)),
        baseline=_baseline("pytest m", "1 failed in 1s", failed=("m.py::x",)),
        agent_created_paths=[],
    )
    uncomparable = classify_regression_diff(
        post_report=_rep("1 failed in 1s", failed=("m.py::x",)),
        baseline=None,
        agent_created_paths=[],
    )
    aggregate = aggregate_regression_results([comparable, uncomparable])
    assert aggregate.pre_existing == ("m.py::x",)
    assert aggregate.regressions == ()
    assert aggregate.unattributed == ()


def test_node_id_file_path_extraction() -> None:
    assert node_id_file_path("tests/test_a.py::TestX::test_b") == "tests/test_a.py"
    assert node_id_file_path("tests/test_a.py") == "tests/test_a.py"
    assert node_id_file_path("test_b (pkg.mod.Class)") is None


# ---------------------------------------------------------------------------
# Test 4: gate policy
# ---------------------------------------------------------------------------


def _certificate(**overrides):
    base = dict(
        contract=None,
        final_text="done",
        blocked=False,
        blocker_valid=False,
        material_edit_count=1,
        require_material_result=True,
        verification_expected=True,
        verification_attempt_count=1,
        last_verification_passed=True,
        regression_baseline_enabled=True,
    )
    base.update(overrides)
    return evaluate_completion_certificate(CompletionCertificateInput(**base))


def test_certificate_regressions_block_and_contradict() -> None:
    cert = _certificate(regressions=("m.py::c",))
    assert "regressions_detected" in cert.problems
    assert cert.status == CompletionCertificateStatus.CONTRADICTED
    assert cert.regressions == ("m.py::c",)


def test_certificate_unattributed_blocks_insufficient() -> None:
    cert = _certificate(unattributed_failures=("m.py::c",))
    assert "unattributed_failures" in cert.problems
    assert cert.status == CompletionCertificateStatus.INSUFFICIENT


def test_certificate_pre_existing_only_does_not_block() -> None:
    cert = _certificate(
        pre_existing_failures=("m.py::a", "m.py::b"),
        last_verification_passed=False,
        regression_attribution_supersedes_last_failure=True,
    )
    assert "verification_failed" not in cert.problems
    assert "regressions_detected" not in cert.problems
    assert "unattributed_failures" not in cert.problems
    assert cert.pre_existing_failures == ("m.py::a", "m.py::b")
    assert cert.status == CompletionCertificateStatus.SUFFICIENT


def test_certificate_unattributed_with_failed_last_run_not_masked() -> None:
    # An unattributed failure IS the reason the last run failed; the specific
    # unattributed_failures problem must supersede the generic verification_failed,
    # never accepting silently.
    cert = _certificate(
        unattributed_failures=("m.py::c",),
        last_verification_passed=False,
        regression_attribution_supersedes_last_failure=True,
    )
    assert "verification_failed" not in cert.problems
    assert "unattributed_failures" in cert.problems


def test_certificate_regression_with_failed_last_run_blocks_as_regression() -> None:
    cert = _certificate(
        regressions=("m.py::c",),
        last_verification_passed=False,
        regression_attribution_supersedes_last_failure=True,
    )
    assert "regressions_detected" in cert.problems
    assert cert.status == CompletionCertificateStatus.CONTRADICTED


def test_certificate_contract_failure_never_cleared_by_attribution() -> None:
    # A named contract command failing always blocks, even if attribution would
    # otherwise supersede the last failure (step 2 stays intact).
    cert = _certificate(
        failed_verification_commands={"pytest -q"},
        last_verification_passed=False,
        pre_existing_failures=("m.py::a",),
        regression_attribution_supersedes_last_failure=True,
    )
    assert "verification_failed" in cert.problems


def test_certificate_kill_switch_reverts_to_legacy() -> None:
    # With regression attribution disabled, the last-run failure blocks as before
    # and no regression/unattributed problems are produced.
    cert = _certificate(
        regression_baseline_enabled=False,
        regressions=("m.py::c",),
        last_verification_passed=False,
        regression_attribution_supersedes_last_failure=True,
    )
    assert "regressions_detected" not in cert.problems
    assert "verification_failed" in cert.problems
    assert cert.regressions == ()


def test_certificate_regressions_block_without_verification_contract() -> None:
    # verification_expected False (no resolvable verify command), but the agent's
    # own before/after runs prove a regression -> must still block (never a silent
    # success just because no verify command resolved).
    cert = _certificate(verification_expected=False, regressions=("m.py::c",))
    assert "regressions_detected" in cert.problems
    assert cert.status == CompletionCertificateStatus.CONTRADICTED


def test_certificate_regressions_exempt_on_blocked_finalization() -> None:
    cert = _certificate(blocked=True, blocker_valid=True, regressions=("m.py::c",))
    assert "regressions_detected" not in cert.problems


def test_certificate_supersede_still_flags_missing_verification() -> None:
    # Supersede clears the generic verification_failed but must NOT skip the
    # missing-command coverage check (step-2 coverage enforcement intact).
    cert = _certificate(
        last_verification_passed=False,
        pre_existing_failures=("m.py::a",),
        regression_attribution_supersedes_last_failure=True,
        missing_verification_commands={"pytest tests/test_b.py"},
        expected_verification_commands={"pytest tests/test_b.py"},
    )
    assert "verification_failed" not in cert.problems
    assert "verification_incomplete" in cert.problems


def test_repair_stage_precedence() -> None:
    assert _completion_gate_repair_stage(["regressions_detected", "verification_failed"]) == (
        "regressions_detected"
    )
    assert _completion_gate_repair_stage(["unattributed_failures"]) == "unattributed_failures"


def _state_with_baseline() -> TurnExecutionState:
    state = TurnExecutionState(execution_requested=True)
    state.note_test_execution(
        command="pytest m",
        report=_rep("0 failed, 3 passed in 1s"),
    )
    return state


def test_gate_regression_blocks_then_clears_after_fixing_rerun() -> None:
    state = _state_with_baseline()
    state.verification_attempt_count = 1
    # Edit (bump generation), then a post-edit run surfaces a new failure.
    state.note_material_edit()
    state.note_verification_relevant_edit()
    state.touched_repo_paths.add("m.py")
    state.last_verification_passed = True  # a later narrow run passed
    state.note_test_execution(
        command="pytest m",
        report=_rep("1 failed, 2 passed in 1s", failed=("m.py::c",)),
    )
    problems = _completion_gate_problems(
        state=state,
        final_text="I checked and there are no regressions.",  # prose cannot clear
        blocked=False,
        verification_expected=True,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
    )
    assert "regressions_detected" in problems

    # The fix is a new edit (new generation); a passing rerun at that generation
    # clears the regression — the stale failing run no longer counts.
    state.note_material_edit()
    state.note_verification_relevant_edit()
    state.note_test_execution(
        command="pytest m",
        report=_rep("0 failed, 3 passed in 1s"),
    )
    cleared = _completion_gate_problems(
        state=state,
        final_text="done",
        blocked=False,
        verification_expected=True,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
    )
    assert "regressions_detected" not in cleared


def test_gate_pre_existing_only_does_not_block_end_to_end() -> None:
    # sympy-12489 mechanized: the module was already broken; post-edit it shows
    # the same failures + everything else passing -> no block.
    state = TurnExecutionState(execution_requested=True)
    state.note_test_execution(
        command="pytest m",
        report=_rep("2 failed, 5 passed in 1s", failed=("m.py::a", "m.py::b")),
    )
    state.verification_attempt_count = 1
    state.note_material_edit()
    state.note_verification_relevant_edit()
    state.touched_repo_paths.add("m.py")
    state.note_qualifying_execution_evidence()
    # Post-edit run: same 2 failures still failing, everything else passing. The
    # last verification attempt was a test run (set by _record_tool_effect in a
    # real run; set explicitly here since we bypass it).
    state.last_verification_passed = False
    state.last_verification_attempt_was_test_run = True
    state.note_test_execution(
        command="pytest m",
        report=_rep("2 failed, 5 passed in 1s", failed=("m.py::a", "m.py::b")),
    )
    problems = _completion_gate_problems(
        state=state,
        final_text="The two failures are pre-existing; my change passes.",
        blocked=False,
        verification_expected=True,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
    )
    assert "verification_failed" not in problems
    assert "regressions_detected" not in problems
    cert = state.latest_completion_certificate
    assert set(cert["pre_existing_failures"]) == {"m.py::a", "m.py::b"}


def test_gate_no_edit_turn_is_exempt() -> None:
    # A pre-edit failing run (no verification-relevant edit) is a baseline, not a
    # post-edit run: no regression diff runs.
    state = TurnExecutionState(execution_requested=True)
    state.note_test_execution(
        command="pytest m",
        report=_rep("1 failed, 2 passed in 1s", failed=("m.py::a",)),
    )
    diff = state.compute_regression_diff(enabled=True)
    assert diff.regressions == ()
    assert diff.unattributed == ()
    assert diff.pre_existing == ()


def test_gate_unattributed_one_round_policy_surfaces_problem() -> None:
    # Realistic path: the unattributed run is itself the failing last attempt.
    state = TurnExecutionState(execution_requested=True)
    state.verification_attempt_count = 1
    state.note_material_edit()
    state.note_verification_relevant_edit()
    state.touched_repo_paths.add("m.py")
    state.last_verification_passed = False
    state.last_verification_attempt_was_test_run = True
    # No baseline for this command -> failures are unattributed.
    state.note_test_execution(
        command="pytest m",
        report=_rep("1 failed, 2 passed in 1s", failed=("m.py::c",)),
    )
    problems = _completion_gate_problems(
        state=state,
        final_text="done",
        blocked=False,
        verification_expected=True,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=True,
    )
    assert "unattributed_failures" in problems
    # The generic verification_failed must not mask the specific attribution.
    assert "verification_failed" not in problems


def test_gate_kill_switch_env_and_config(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_REGRESSION_BASELINE", raising=False)
    assert (
        _regression_baseline_enabled(AppConfig(model="x", regression_baseline_enabled=True)) is True
    )
    assert (
        _regression_baseline_enabled(AppConfig(model="x", regression_baseline_enabled=False))
        is False
    )
    monkeypatch.setenv("ALYSIS_REGRESSION_BASELINE", "off")
    assert (
        _regression_baseline_enabled(AppConfig(model="x", regression_baseline_enabled=True))
        is False
    )
    monkeypatch.setenv("ALYSIS_REGRESSION_BASELINE", "on")
    assert (
        _regression_baseline_enabled(AppConfig(model="x", regression_baseline_enabled=False))
        is True
    )


def test_gate_kill_switch_config_key_roundtrip() -> None:
    cfg = AppConfig(model="x")
    assert cfg.regression_baseline_enabled is True
    set_config_value(cfg, "regression_baseline_enabled", "off")
    assert cfg.regression_baseline_enabled is False
    set_config_value(cfg, "regression_baseline_enabled", "true")
    assert cfg.regression_baseline_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "regression_baseline_enabled", "maybe")


def test_gate_disabled_state_reverts_to_legacy() -> None:
    state = _state_with_baseline()
    state.verification_attempt_count = 1
    state.note_material_edit()
    state.note_verification_relevant_edit()
    state.touched_repo_paths.add("m.py")
    state.last_verification_passed = True
    state.note_test_execution(
        command="pytest m",
        report=_rep("1 failed, 2 passed in 1s", failed=("m.py::c",)),
    )
    problems = _completion_gate_problems(
        state=state,
        final_text="done",
        blocked=False,
        verification_expected=True,
        evidence_v2=True,
        turn_intent="execute",
        regression_baseline_enabled=False,
    )
    assert "regressions_detected" not in problems


# ---------------------------------------------------------------------------
# Test 4 (cont.): honest-finalization markers + nudge lines
# ---------------------------------------------------------------------------


def test_regressions_unresolved_marker_is_visible_and_distinct() -> None:
    marker = build_regressions_unresolved_marker(["m.py::c"], baseline_command="pytest m")
    assert "REGRESSIONS UNRESOLVED" in marker
    assert "m.py::c" in marker
    assert "pytest m" in marker
    assert marker != HONEST_UNVERIFIED_FINALIZATION_MARKER
    assert "UNATTRIBUTED" not in marker


def test_unattributed_marker_is_visible_and_distinct() -> None:
    marker = build_unattributed_failures_marker(["m.py::c"])
    assert "UNATTRIBUTED FAILURES" in marker
    assert "m.py::c" in marker
    assert marker != HONEST_UNVERIFIED_FINALIZATION_MARKER
    assert "REGRESSIONS UNRESOLVED" not in marker


def test_nudge_names_regressions_and_is_action_only() -> None:
    message = _completion_gate_nudge_message(
        ["regressions_detected"],
        regression_ids=["m.py::c"],
        regression_baseline_command="pytest m",
    )
    assert "m.py::c" in message
    assert "pytest m" in message
    assert "A written explanation cannot clear this" in message
    assert "this checklist is advisory" not in message


def test_nudge_unattributed_asks_to_establish_attribution() -> None:
    message = _completion_gate_nudge_message(
        ["unattributed_failures"],
        unattributed_ids=["m.py::c"],
    )
    assert "m.py::c" in message
    assert "baseline" in message.lower()
    assert "this checklist is advisory" not in message


# ---------------------------------------------------------------------------
# Test 5: pre-edit nudge (baseline existence, not successful contract evidence)
# ---------------------------------------------------------------------------


def test_has_usable_pre_edit_baseline_does_not_claim_contract_coverage() -> None:
    state = TurnExecutionState(execution_requested=True)
    assert state.has_usable_pre_edit_baseline() is False
    state.note_test_execution(
        command="pytest -q",
        report=_report("==== 3 passed in 1s ===="),
    )
    state.expected_verification_commands = {"pytest other"}
    state.note_verification_relevant_edit()
    assert state.has_usable_pre_edit_baseline() is True
    assert state.missing_verification_commands() == {"pytest other"}
    assert not state.accepted_verification_evidence


@pytest.mark.parametrize("tool_name", ["verify_run", "shell_run"])
@pytest.mark.parametrize("passed", [True, False])
def test_pre_edit_baseline_survives_failed_verification(tmp_path, tool_name, passed) -> None:
    """Replay the host report and unknown-execution shape of a failed live run."""
    command = "python3 -m unittest discover -s tests -v"
    known = "python -m unittest discover"
    output = "Ran 1 test in 0.01s\n\nOK\n" if passed else _UNITTEST_MIXED
    exit_code = 0 if passed else 1
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text("import unittest\n")
    state = TurnExecutionState(execution_requested=True, expected_verification_commands={known})
    if tool_name == "verify_run":
        arguments = {"commands": [command]}
        result = {
            "all_passed": passed,
            "executed_count": 1,
            "command_results": [
                {
                    "command": command,
                    "effective_command": command,
                    "exit_code": exit_code,
                    "ok": passed,
                    "real_execution": True if passed else None,
                    "output_preview": output[:100],
                    "output_chars": len(output),
                    "host_test_report": parse_test_report(output).as_payload(),
                }
            ],
        }
    else:
        arguments = {"cmd": command}
        result = {
            "cmd": command,
            "exit_code": exit_code,
            "stdout": output,
            "stderr": "",
            "real_execution": True if passed else None,
        }
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name=tool_name,
        arguments=arguments,
        status="success" if passed else "failed",
        result=result,
        known_verification_commands=[known],
        verification_authoritative=True,
    )
    assert baseline_command_key(known) not in state.test_baselines
    assert state.test_baselines[baseline_command_key(command)].usable
    # The advisory runs after the first edit; the baseline must still refer to gen 0.
    state.note_verification_relevant_edit()
    assert state.has_usable_pre_edit_baseline() is True
    if not passed:
        assert state.test_baselines[baseline_command_key(command)].failing_ids
        assert state.last_verification_passed is False
        assert not state.accepted_verification_evidence
        assert not state.executed_verification_evidence
        evidence = state.supplemental_verification_evidence[-1]
        assert evidence["real_execution"] is None
        assert evidence["allowed_to_satisfy_contract"] is False
        assert evidence["observed_exit_code"] == 1


@pytest.mark.parametrize(
    "output",
    ["", "unparseable", "2 failed in 0.1s", _UNITTEST_MIXED[:100]],
)
def test_pre_edit_baseline_requires_complete_report(output) -> None:
    state = TurnExecutionState(execution_requested=True)
    state.note_test_execution(
        command="pytest tests/test_existing.py -q",
        report=parse_test_report(output),
    )
    state.note_verification_relevant_edit()
    assert state.has_usable_pre_edit_baseline() is False


def test_post_edit_report_does_not_retroactively_establish_baseline() -> None:
    state = TurnExecutionState(execution_requested=True)
    state.note_verification_relevant_edit()
    state.note_test_execution(command="pytest tests", report=parse_test_report(_PYTEST_MIXED))
    assert state.post_edit_test_runs
    assert state.has_usable_pre_edit_baseline() is False


def test_pre_edit_baseline_query_rejects_later_generation_record() -> None:
    state = TurnExecutionState(execution_requested=True)
    command = "pytest tests"
    state.test_baselines[baseline_command_key(command)] = BaselineRecord(
        command=command,
        command_key=baseline_command_key(command),
        report=parse_test_report(_PYTEST_MIXED),
        edit_generation=1,
    )
    assert state.has_usable_pre_edit_baseline() is False


def test_baseline_existence_does_not_merge_regression_commands() -> None:
    state = TurnExecutionState(execution_requested=True)
    baseline_command = "pytest tests/test_existing.py -q"
    state.note_test_execution(command=baseline_command, report=_rep("1 passed in 0.1s"))
    state.note_verification_relevant_edit()
    assert state.has_usable_pre_edit_baseline() is True
    # Existence alone does not make a distinct test selection comparable to the
    # actual baseline for regression attribution.
    state.note_test_execution(
        command="pytest tests/test_other.py -q",
        report=_rep("1 failed in 0.1s", failed=("tests/test_other.py::test_other",)),
    )
    diff = state.compute_regression_diff(enabled=True)
    assert diff.unattributed == ("tests/test_other.py::test_other",)
    assert diff.regressions == ()
    assert set(state.test_baselines) == {baseline_command_key(baseline_command)}


# ---------------------------------------------------------------------------
# Test 6: prompt byte-immutability with the feature on/off
# ---------------------------------------------------------------------------


def test_prompt_bytes_identical_regardless_of_kill_switch(monkeypatch) -> None:
    from alysis_code.agent.prompt_context import _compose_session_system_prompt
    from alysis_code.agent_loop import SYSTEM_PROMPT

    def compose() -> str:
        return _compose_session_system_prompt(
            base_prompt=SYSTEM_PROMPT,
            trusted_prompt_append="",
            include_write_guidance=True,
            include_skill_discovery_guidance=True,
            include_skill_lifecycle_guidance=True,
            include_subagent_guidance=True,
            include_one_shot_guidance=True,
        )

    monkeypatch.setenv("ALYSIS_REGRESSION_BASELINE", "on")
    prompt_on = compose()
    monkeypatch.setenv("ALYSIS_REGRESSION_BASELINE", "off")
    prompt_off = compose()

    assert prompt_on.encode("utf-8") == prompt_off.encode("utf-8")
    assert "REGRESSION_BASELINE" not in prompt_on
    assert "regression_baseline" not in prompt_on
    assert "ALYSIS_REGRESSION_BASELINE" not in prompt_on
