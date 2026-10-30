"""Blast-radius regression gate (verification step 6).

Mirrors the pure-logic + gate-integration style of ``test_regression_baseline.py``
and ``test_reproduction_first.py``. The scope is selected against a synthetic repo
on disk (so the import scan and the package-root walk are exercised for real), and
the observed runs are driven through ``_record_tool_effect`` — no provider, no LLM.

The six scenarios the protocol is specified against each have a named test:

* ``test_scope_picks_mirror_sibling_and_importing_tests`` — selection;
* ``test_pre_existing_failures_are_not_blamed_on_the_change`` — blame;
* ``test_regression_is_detected_then_cleared_by_a_repair`` — detect + repair;
* ``test_over_broad_breakage_switches_to_the_narrow_rewrite_directive`` — escalation;
* ``test_summary_lists_uncleared_regressions`` — honest summary;
* ``test_runtime_cap_shrinks_the_scope_without_disabling_the_gate`` — runtime cap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.blast_radius import (
    DEFAULT_OVER_BROAD_THRESHOLD,
    MIN_SCOPE_FILES,
    BlastRadiusPolicy,
    BlastRadiusScope,
    BlastRadiusStatus,
    ScopePhase,
    ScopeRun,
    ScopeTier,
    _blast_radius_gate_enabled,
    apply_scope_shrink_rounds,
    assess_blast_radius,
    blast_radius_blocks_finalization,
    build_blast_radius_nudge_line,
    build_blast_radius_scope_advisory,
    build_blast_radius_status_summary,
    build_repo_test_index,
    classify_scope_phase,
    command_path_selectors,
    extract_python_import_tokens,
    is_test_file,
    python_module_names,
    resolve_blast_radius_policy,
    select_blast_radius_scope,
    selection_covers,
    shrink_scope_for_runtime,
)
from alysis_code.agent.completion_certificate import (
    CompletionCertificateInput,
    CompletionCertificateStatus,
    evaluate_completion_certificate,
)
from alysis_code.agent.regression_baseline import parse_test_report
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
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.session_store import read_session_events
from alysis_code.surface import ApprovalDecision
from alysis_code.surface.noop_surface import NoopSurface

# ---------------------------------------------------------------------------
# Synthetic repo + drivers
# ---------------------------------------------------------------------------

#: ``src`` layout with a real package root, one shared module with two importers,
#: one unrelated corner of the repo, and a test that neither mirrors nor imports
#: the shared module — so over-selection is visible, not just under-selection.
_REPO_FILES = {
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": "def widen(value):\n    return value\n",
    "src/pkg/other.py": "from pkg.core import widen\n",
    "src/pkg/helpers/__init__.py": "",
    "src/pkg/helpers/fmt.py": "def fmt(value):\n    return str(value)\n",
    "tests/test_core.py": "from pkg.core import widen\n\ndef test_widen():\n    assert widen(1) == 1\n",
    "tests/test_other.py": "from pkg import other\n\ndef test_other():\n    assert other\n",
    "tests/test_consumer.py": "import pkg.core\n\ndef test_consumer():\n    assert pkg.core\n",
    "tests/test_unrelated.py": "def test_unrelated():\n    assert True\n",
    "tests/pkg/test_smoke.py": "def test_smoke():\n    assert True\n",
    "tests/helpers/test_fmt.py": "from pkg.helpers.fmt import fmt\n\ndef test_fmt():\n    assert fmt(1)\n",
    "docs/guide.md": "# guide\n",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for relative, content in _REPO_FILES.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def _pytest_scope_command(scope: BlastRadiusScope) -> str:
    """The synthetic fixture uses pytest; production suggestions are not its contract."""
    return "python -m pytest " + " ".join(scope.paths) + " -q"


def _pytest_output(*, failed: Sequence[str] = (), passed: int = 8) -> str:
    """Realistic pytest output: the short-summary section plus the counts line."""
    if not failed:
        return f"============================== {passed} passed in 1.20s ==============================="
    lines = ["=========================== short test summary info ============================"]
    lines.extend(f"FAILED {node} - AssertionError: boom" for node in failed)
    lines.append(
        f"========================= {len(failed)} failed, {passed} passed in 1.20s ========================="
    )
    return "\n".join(lines)


def _run_tests(
    state: TurnExecutionState,
    root: Path,
    command: str,
    *,
    failed: Sequence[str] = (),
    passed: int = 8,
    elapsed_ms: int | None = None,
) -> None:
    """Drive a ``shell_run`` test execution through the real tool-effect recorder."""
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok" if not failed else "failed",
        result={
            "cmd": command,
            "exit_code": 0 if not failed else 1,
            "stdout": _pytest_output(failed=failed, passed=passed),
            "stderr": "",
        },
        known_verification_commands=[],
        elapsed_ms=elapsed_ms,
    )


def _edit(state: TurnExecutionState, root: Path, path: str, *, created: bool = False) -> None:
    """Drive a successful ``fs_write`` through the real tool-effect recorder."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# changed\n", encoding="utf-8")
    _record_tool_effect(
        root=root,
        state=state,
        tool_name="fs_write",
        arguments={"path": path},
        status="ok",
        result={"path": path, "created": created},
        known_verification_commands=[],
    )


def _scoped_state(
    repo_root: Path,
    *,
    touched: Sequence[str] = ("src/pkg/core.py",),
    policy: BlastRadiusPolicy | None = None,
) -> TurnExecutionState:
    """A turn state whose scope is already selected for ``touched``."""
    state = TurnExecutionState(execution_requested=True)
    state.blast_radius_policy = policy or BlastRadiusPolicy()
    state.blast_radius_scope = select_blast_radius_scope(
        touched_paths=touched,
        index=build_repo_test_index(repo_root),
        policy=state.blast_radius_policy,
    )
    return state


def _scope_run(
    repo: Path,
    command: str,
    *,
    phase: ScopePhase,
    failed: Sequence[str] = (),
    passed: int = 8,
    duration_seconds: float | None = None,
) -> ScopeRun:
    return ScopeRun(
        command=command,
        selectors=command_path_selectors(command, workspace_root=repo),
        phase=phase,
        report=parse_test_report(_pytest_output(failed=failed, passed=passed)),
        duration_seconds=duration_seconds,
    )


# ---------------------------------------------------------------------------
# (a) Scope selection: neighbouring + importing tests
# ---------------------------------------------------------------------------


def test_scope_picks_mirror_sibling_and_importing_tests(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"],
        index=build_repo_test_index(repo),
    )
    tiers = {entry.path: entry.tier for entry in scope.entries}

    # The name mirror is nearest; the importer is found by the static scan even
    # though it is not named after the touched module.
    assert tiers["tests/test_core.py"] == ScopeTier.MIRROR
    assert tiers["tests/test_consumer.py"] == ScopeTier.IMPORTER
    # A test living in the touched package's mirrored test directory is in scope on
    # proximity alone, with no import of its own.
    assert tiers["tests/pkg/test_smoke.py"] == ScopeTier.PACKAGE
    # tests/test_other.py reaches pkg.core only through pkg.other. The scan is
    # direct-import only, so a transitive dependant is deliberately out of scope:
    # widening to the transitive closure would pull in most of a repo.
    assert "tests/test_other.py" not in tiers
    # A flat test that neither mirrors, imports, nor shares a package is out too.
    assert "tests/test_unrelated.py" not in tiers
    assert scope.language is not None and scope.diffable
    assert scope.suggested_command() == "", "Python files alone do not establish pytest"


def test_scope_orders_nearest_first(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"],
        index=build_repo_test_index(repo),
    )
    tiers = [entry.tier for entry in scope.entries]
    assert tiers == sorted(tiers), "scope must be ordered nearest-first for shrinking"
    assert scope.paths[0] == "tests/test_core.py"


def test_scope_is_empty_without_touched_paths(repo: Path) -> None:
    scope = select_blast_radius_scope(touched_paths=[], index=build_repo_test_index(repo))
    assert scope.empty
    assert scope.suggested_command() == ""


def test_a_non_source_change_has_no_scope(repo: Path) -> None:
    # A doc edit would otherwise drag in every test sharing its directory.
    (repo / "tests" / "README.md").write_text("# tests\n", encoding="utf-8")
    scope = select_blast_radius_scope(
        touched_paths=["docs/guide.md", "tests/README.md"],
        index=build_repo_test_index(repo),
    )
    assert scope.empty


def test_editing_a_test_file_puts_that_test_in_scope(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["tests/test_unrelated.py"], index=build_repo_test_index(repo)
    )
    assert scope.paths[0] == "tests/test_unrelated.py"


def test_scope_excludes_a_test_that_only_shares_a_name_prefix(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/helpers/fmt.py"],
        index=build_repo_test_index(repo),
    )
    assert "tests/helpers/test_fmt.py" in scope.paths
    assert "tests/test_unrelated.py" not in scope.paths


def test_index_skips_ignored_directories(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_real.py").write_text("import a\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "test_vendored.py").write_text("import a\n", encoding="utf-8")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "a.test.js").write_text("//\n", encoding="utf-8")

    index = build_repo_test_index(tmp_path)
    assert index.test_files == ("tests/test_real.py",)


def test_index_never_raises_on_a_missing_root(tmp_path: Path) -> None:
    index = build_repo_test_index(tmp_path / "does-not-exist")
    assert index.empty


# ---------------------------------------------------------------------------
# Pure helpers behind the selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_core.py", True),
        ("tests/core_test.py", True),
        ("src/pkg/core.py", False),
        ("src/app/thing.test.ts", True),
        ("src/app/thing.spec.js", True),
        ("src/app/__tests__/thing.ts", True),
        ("pkg/server_test.go", True),
        ("pkg/server.go", False),
        ("docs/guide.md", False),
    ],
)
def test_test_file_conventions(path: str, expected: bool) -> None:
    assert is_test_file(path) is expected


def test_python_module_names_use_the_package_root(repo: Path) -> None:
    index = build_repo_test_index(repo)
    names = python_module_names("src/pkg/core.py", index.package_dirs)
    # The ``src`` layout resolves to the importable name, not the on-disk path.
    assert names[0] == "pkg.core"
    assert "src.pkg.core" in names


def test_python_module_names_handle_a_namespace_layout() -> None:
    # No ``__init__.py`` anywhere: the source-root fallback still yields ``pkg.core``.
    assert "pkg.core" in python_module_names("src/pkg/core.py", ())


def test_extract_python_import_tokens_covers_both_forms() -> None:
    tokens = extract_python_import_tokens(
        "import pkg.core\n"
        "from pkg.helpers import fmt, other\n"
        "from . import sibling\n"
        "import os, sys\n"
    )
    assert "pkg.core" in tokens
    assert "pkg.helpers" in tokens
    assert "pkg.helpers.fmt" in tokens
    assert "os" in tokens and "sys" in tokens
    # A relative import carries no absolute name; guessing one would drag an
    # unrelated test into the scope.
    assert not any(token.startswith(".") for token in tokens)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m pytest tests/test_core.py -q", ("tests/test_core.py",)),
        ("pytest tests/test_a.py::test_x tests/test_b.py", None),
        (
            "python -m pytest -q",
            tuple(
                sorted(
                    path
                    for path in _REPO_FILES
                    if path.startswith("tests/") and path.endswith(".py")
                )
            ),
        ),
        ("pytest -k widen", None),
        ("pytest tests/helpers", ("tests/helpers/test_fmt.py",)),
        ("/opt/venv/bin/pytest tests/test_core.py -q", ("tests/test_core.py",)),
        (
            '"/opt/Project Tools/python" -m pytest "tests/unit space/test_core.py" -q',
            ("tests/unit space/test_core.py",),
        ),
        ("MODE=test /opt/venv/bin/python -m pytest tests/helpers", ("tests/helpers/test_fmt.py",)),
        ("env MODE=test /opt/venv/bin/pytest tests/helpers", ("tests/helpers/test_fmt.py",)),
    ],
)
def test_command_path_selectors(repo: Path, command: str, expected: tuple[str, ...] | None) -> None:
    if expected and "space" in command:
        for item in expected:
            path = repo / item
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_ok():\n    assert True\n")
    selected = command_path_selectors(command, workspace_root=repo)
    assert (tuple(sorted(selected)) if selected is not None else None) == (
        tuple(sorted(expected)) if expected is not None else None
    )


def test_executable_path_does_not_broaden_actual_test_operands(repo: Path) -> None:
    selectors = command_path_selectors(
        "/opt/venv/bin/python -m pytest tests/helpers -q", workspace_root=repo
    )
    assert selection_covers(selectors, "tests/helpers/test_fmt.py") is True
    assert selection_covers(selectors, "tests/integration/test_core.py") is False


def test_unparsed_command_is_inconclusive() -> None:
    # Unterminated shell quoting cannot earn whole-suite coverage merely because
    # structured parsing produced no argument vector.
    assert command_path_selectors('pytest "tests/unit/test_core.py') is None


@pytest.mark.parametrize("executable", ["python", "/opt/venv/bin/python"])
def test_changed_working_directory_does_not_claim_other_test_scopes(
    repo: Path, executable: str
) -> None:
    selectors = command_path_selectors(
        f"cd tests/helpers && {executable} -m pytest -q", workspace_root=repo
    )
    assert selectors == ("tests/helpers/test_fmt.py",)
    assert selection_covers(selectors, "tests/helpers/test_fmt.py") is True
    assert selection_covers(selectors, "tests/integration/test_core.py") is False


def test_selection_covers_treats_no_selectors_as_whole_suite() -> None:
    assert selection_covers((), "tests/test_core.py") is True
    assert selection_covers(("tests/test_core.py",), "tests/test_core.py") is True
    assert selection_covers(("tests",), "tests/test_core.py") is True
    assert selection_covers(("tests/test_core.py",), "tests/test_other.py") is False


def test_scope_phase_ignores_files_the_agent_created() -> None:
    # Writing a new test is not a change to existing behaviour, so the tree is
    # still clean — the same discriminator step 5 uses.
    assert (
        classify_scope_phase(
            touched_repo_paths={"tests/test_new.py"},
            created_paths={"tests/test_new.py"},
        )
        == ScopePhase.BASELINE
    )
    assert (
        classify_scope_phase(
            touched_repo_paths={"tests/test_new.py", "src/pkg/core.py"},
            created_paths={"tests/test_new.py"},
        )
        == ScopePhase.GATE
    )


# ---------------------------------------------------------------------------
# (b) Pre-existing failures are excluded from blame
# ---------------------------------------------------------------------------


def test_pre_existing_failures_are_not_blamed_on_the_change(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    already_broken = "tests/test_other.py::test_other"

    _run_tests(state, repo, command, failed=[already_broken])
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command, failed=[already_broken])

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == BlastRadiusStatus.CLEAN
    assert assessment.pre_existing == (already_broken,)
    assert assessment.new_failures == ()
    assert not blast_radius_blocks_finalization(assessment, material_edit_count=1)


def test_a_clean_whole_suite_run_baselines_any_scope(repo: Path) -> None:
    state = _scoped_state(repo)
    already_broken = "tests/test_other.py::test_other"

    # The agent ran the whole suite before editing and the scope afterwards. The
    # commands differ, but the whole-suite run covered every scope file.
    _run_tests(state, repo, "python -m pytest -q", failed=[already_broken])
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(
        state,
        repo,
        _pytest_scope_command(state.blast_radius_scope),
        failed=[already_broken, "tests/test_core.py::test_widen"],
    )

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.baseline_whole_suite is False
    assert state.blast_radius_runs[0].covers(state.blast_radius_scope.paths)
    assert assessment.pre_existing == (already_broken,)
    assert assessment.new_failures == ("tests/test_core.py::test_widen",)


def test_a_run_after_the_first_edit_is_never_credited_as_a_baseline(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)

    # Fix first, then run: the run cannot be a baseline, or it would mask exactly
    # the breakage this step exists to catch.
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command, failed=["tests/test_core.py::test_widen"])
    _run_tests(state, repo, command, failed=["tests/test_core.py::test_widen"])

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.has_baseline is False
    assert assessment.status == BlastRadiusStatus.UNATTRIBUTED
    assert assessment.new_failures == ()
    assert assessment.unattributed == ("tests/test_core.py::test_widen",)


def test_failures_outside_the_baseline_coverage_are_unattributed(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    # The clean tree was only partly measured: a failure in a file it covered can be
    # called new, a failure in a file it never ran cannot.
    baseline = _scope_run(repo, "pytest tests/test_core.py", phase=ScopePhase.BASELINE)
    gate = _scope_run(
        repo,
        "pytest -q",
        phase=ScopePhase.GATE,
        failed=["tests/test_core.py::test_widen", "tests/test_other.py::test_other"],
    )
    assessment = assess_blast_radius(scope=scope, runs=[baseline, gate], applicable=True)
    assert assessment.new_failures == ("tests/test_core.py::test_widen",)
    assert assessment.unattributed == ("tests/test_other.py::test_other",)
    assert assessment.status == BlastRadiusStatus.REGRESSED


def test_several_clean_runs_compose_into_one_baseline(repo: Path) -> None:
    """Every clean-tree run observed the same unpatched tree, so they compose."""
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    runs = [
        _scope_run(
            repo,
            "pytest tests/test_core.py",
            phase=ScopePhase.BASELINE,
            failed=["tests/test_core.py::test_widen"],
        ),
        _scope_run(repo, "pytest tests/test_consumer.py", phase=ScopePhase.BASELINE),
        _scope_run(
            repo,
            "pytest -q",
            phase=ScopePhase.GATE,
            failed=["tests/test_core.py::test_widen", "tests/test_consumer.py::test_consumer"],
        ),
    ]
    assessment = assess_blast_radius(scope=scope, runs=runs, applicable=True)
    assert assessment.pre_existing == ("tests/test_core.py::test_widen",)
    assert assessment.new_failures == ("tests/test_consumer.py::test_consumer",)
    assert "pytest tests/test_core.py" in assessment.baseline_command


def test_a_failing_test_the_agent_just_wrote_is_not_a_regression(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)

    _run_tests(state, repo, command)
    _edit(state, repo, "tests/test_added.py", created=True)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, "pytest -q", failed=["tests/test_added.py::test_new"])

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.agent_authored == ("tests/test_added.py::test_new",)
    assert assessment.new_failures == ()


# ---------------------------------------------------------------------------
# (c) An introduced regression is detected, then cleared by a repair
# ---------------------------------------------------------------------------


def test_regression_is_detected_then_cleared_by_a_repair(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    broken = "tests/test_consumer.py::test_consumer"

    _run_tests(state, repo, command)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command, failed=[broken])

    detected = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert detected.status == BlastRadiusStatus.REGRESSED
    assert detected.new_failures == (broken,)
    assert blast_radius_blocks_finalization(detected, material_edit_count=1)
    problems = _completion_gate_problems(
        state=state,
        final_text="Done.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        blast_radius_enabled=True,
    )
    assert "blast_radius_regressions" in problems
    assert _completion_gate_repair_stage(problems) == "blast_radius_regressions"

    # The repair narrows the change and re-runs the same scope: nothing new fails.
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command)

    repaired = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert repaired.status == BlastRadiusStatus.CLEAN
    assert repaired.new_failures == ()
    assert not blast_radius_blocks_finalization(repaired, material_edit_count=2)
    assert "blast_radius_regressions" not in _completion_gate_problems(
        state=state,
        final_text="Done.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        blast_radius_enabled=True,
    )


def test_a_scope_never_re_run_after_the_fix_blocks_as_unverified(repo: Path) -> None:
    state = _scoped_state(repo)
    _run_tests(state, repo, _pytest_scope_command(state.blast_radius_scope))
    _edit(state, repo, "src/pkg/core.py")

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == BlastRadiusStatus.GATE_MISSING
    problems = _completion_gate_problems(
        state=state,
        final_text="Done.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        blast_radius_enabled=True,
    )
    assert problems == ["blast_radius_unverified"]
    nudge = _completion_gate_nudge_message(problems, blast_radius_assessment=assessment)
    assert "Current test coverage has not been established for the selected scope" in nudge
    assert "tests/test_core.py" in nudge


def test_a_partial_re_run_does_not_satisfy_the_gate(repo: Path) -> None:
    state = _scoped_state(repo)
    _run_tests(state, repo, "pytest -q")
    _edit(state, repo, "src/pkg/core.py")
    # Only one of the scope's files was re-run.
    _run_tests(state, repo, "python -m pytest tests/test_core.py -q")

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == BlastRadiusStatus.GATE_MISSING


def test_the_gate_is_inert_without_material_edits(repo: Path) -> None:
    state = _scoped_state(repo)
    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert not blast_radius_blocks_finalization(assessment, material_edit_count=0)


def test_regressions_already_reported_by_step_three_are_not_double_reported(repo: Path) -> None:
    """One fact, one blocker: the step-3 stage owns same-command regressions."""
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    broken = "tests/test_consumer.py::test_consumer"

    _run_tests(state, repo, command)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command, failed=[broken])

    problems = _completion_gate_problems(
        state=state,
        final_text="Done.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        regression_baseline_enabled=True,
        blast_radius_enabled=True,
    )
    assert "regressions_detected" in problems
    assert "blast_radius_regressions" not in problems
    certificate = state.latest_completion_certificate
    assert certificate["blast_radius_new_failures"] == []
    assert certificate["regressions"] == [broken]


# ---------------------------------------------------------------------------
# (d) Over-threshold breakage switches to the narrow-rewrite directive
# ---------------------------------------------------------------------------


def _many_failures(count: int) -> list[str]:
    return [f"tests/test_core.py::test_case_{index}" for index in range(count)]


def test_over_broad_breakage_switches_to_the_narrow_rewrite_directive(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    broken = _many_failures(DEFAULT_OVER_BROAD_THRESHOLD + 1)
    assessment = assess_blast_radius(
        scope=scope,
        runs=[
            _scope_run(repo, "pytest -q", phase=ScopePhase.BASELINE),
            _scope_run(repo, "pytest -q", phase=ScopePhase.GATE, failed=broken),
        ],
        applicable=True,
    )
    assert assessment.status == BlastRadiusStatus.REGRESSED
    assert assessment.over_broad is True

    nudge = build_blast_radius_nudge_line(assessment)
    assert "the change itself is too broad" in nudge
    assert "write a narrower patch" in nudge
    # The listing is bounded — 200 node ids must not be pasted into the context.
    assert "more)" in nudge
    assert nudge.count("::") <= 12

    # The summary carries the same verdict, also bounded.
    summary = build_blast_radius_status_summary(assessment)
    assert "REGRESSIONS INTRODUCED" in summary
    assert "over-broad and should be rewritten narrowly" in summary
    assert summary.count("::") <= 12


def test_breakage_below_the_threshold_asks_for_a_targeted_repair(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    assessment = assess_blast_radius(
        scope=scope,
        runs=[
            _scope_run(repo, "pytest -q", phase=ScopePhase.BASELINE),
            _scope_run(repo, "pytest -q", phase=ScopePhase.GATE, failed=_many_failures(2)),
        ],
        applicable=True,
    )
    assert assessment.over_broad is False
    nudge = build_blast_radius_nudge_line(assessment)
    assert "write a narrower patch" not in nudge
    assert "keeping your fix intact" in nudge
    assert "Do not edit, skip, or delete those tests" in nudge


def test_the_over_broad_threshold_is_configurable(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    runs = [
        _scope_run(repo, "pytest -q", phase=ScopePhase.BASELINE),
        _scope_run(repo, "pytest -q", phase=ScopePhase.GATE, failed=_many_failures(3)),
    ]
    strict = assess_blast_radius(
        scope=scope, runs=runs, applicable=True, policy=BlastRadiusPolicy(over_broad_threshold=3)
    )
    lenient = assess_blast_radius(
        scope=scope, runs=runs, applicable=True, policy=BlastRadiusPolicy(over_broad_threshold=99)
    )
    assert strict.over_broad is True
    assert lenient.over_broad is False


# ---------------------------------------------------------------------------
# (e) The summary lists uncleared regressions
# ---------------------------------------------------------------------------


def test_summary_lists_uncleared_regressions(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    broken = ["tests/test_consumer.py::test_consumer", "tests/pkg/test_smoke.py::test_smoke"]

    _run_tests(state, repo, command)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command, failed=broken)

    summary = build_blast_radius_status_summary(
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    )
    assert "REGRESSIONS INTRODUCED" in summary
    for node_id in broken:
        assert node_id in summary
    assert "KNOWN BREAKAGE" in summary


def test_summary_reports_a_clean_blast_radius_too(repo: Path) -> None:
    """Success is reported with what else was checked, never silently."""
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)

    _run_tests(state, repo, command)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command)

    summary = build_blast_radius_status_summary(
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    )
    assert "Blast radius:" in summary
    assert "nothing that passed before it fails now" in summary


def test_summary_names_an_unattributed_result_as_unattributed(repo: Path) -> None:
    state = _scoped_state(repo)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(
        state,
        repo,
        _pytest_scope_command(state.blast_radius_scope),
        failed=["tests/test_core.py::test_widen"],
    )

    summary = build_blast_radius_status_summary(
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    )
    assert "UNATTRIBUTED" in summary
    assert "tests/test_core.py::test_widen" in summary
    assert "failures are mine: tests/test_core.py::test_widen" in summary


def test_summary_handles_unattributed_run_with_no_failures(repo: Path) -> None:
    state = _scoped_state(repo)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, _pytest_scope_command(state.blast_radius_scope))

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    summary = build_blast_radius_status_summary(assessment)

    assert assessment.status == BlastRadiusStatus.UNATTRIBUTED
    assert not assessment.satisfied
    assert "Checks passed:" in summary
    assert "no failures were reported" in summary
    assert "before/after comparison is unavailable" in summary
    assert "UNATTRIBUTED" not in summary
    assert "⚠" not in summary
    assert "failures are mine: ." not in summary


@pytest.mark.parametrize("column", ["agent_authored", "pre_existing", "unattributed"])
def test_summary_does_not_hide_other_failure_columns(repo: Path, column: str) -> None:
    state = _scoped_state(repo)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, _pytest_scope_command(state.blast_radius_scope))
    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    failure = "tests/test_core.py::test_widen"
    summary = build_blast_radius_status_summary(replace(assessment, **{column: (failure,)}))
    assert failure in summary
    assert "Checks passed:" not in summary
    assert "no failures were reported" not in summary


def test_summary_is_silent_when_the_protocol_does_not_apply(repo: Path) -> None:
    state = _scoped_state(repo)
    assessment = state.compute_blast_radius_assessment(enabled=False, turn_intent="execute")
    assert build_blast_radius_status_summary(assessment) == ""


def test_new_failures_rank_the_certificate_contradicted() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="Done.",
            blocked=False,
            blocker_valid=False,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=1,
            last_verification_passed=True,
            blast_radius_enabled=True,
            blast_radius_new_failures=("tests/test_core.py::test_widen",),
            blast_radius_status=BlastRadiusStatus.REGRESSED.value,
        )
    )
    assert certificate.status == CompletionCertificateStatus.CONTRADICTED
    assert "blast_radius_regressions" in certificate.problems
    assert certificate.blast_radius_new_failures == ("tests/test_core.py::test_widen",)


def test_the_unverified_deficit_is_repairable_not_contradicted() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="Done.",
            blocked=False,
            blocker_valid=False,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=1,
            last_verification_passed=True,
            blast_radius_enabled=True,
            blast_radius_unverified=True,
            blast_radius_status=BlastRadiusStatus.GATE_MISSING.value,
        )
    )
    assert certificate.status == CompletionCertificateStatus.INSUFFICIENT
    assert "blast_radius_unverified" in certificate.problems


def test_a_blocked_finalization_is_exempt() -> None:
    certificate = evaluate_completion_certificate(
        CompletionCertificateInput(
            contract=None,
            final_text="Blocked on a missing credential.",
            blocked=True,
            blocker_valid=True,
            material_edit_count=1,
            require_material_result=True,
            verification_expected=False,
            verification_attempt_count=0,
            last_verification_passed=None,
            blast_radius_enabled=True,
            blast_radius_new_failures=("tests/test_core.py::test_widen",),
        )
    )
    assert "blast_radius_regressions" not in certificate.problems


# ---------------------------------------------------------------------------
# (f) The runtime cap shrinks the scope; it never disables the gate
# ---------------------------------------------------------------------------


def test_runtime_cap_shrinks_the_scope_without_disabling_the_gate(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    policy = BlastRadiusPolicy(scope_seconds_cap=10.0)
    assert len(scope.entries) > 1

    shrunk = shrink_scope_for_runtime(scope, observed_seconds=42.0, policy=policy)
    assert shrunk is not None
    assert not shrunk.empty, "shrinking must never become skipping"
    assert len(shrunk.entries) < len(scope.entries)
    assert shrunk.dropped_for_runtime
    assert shrunk.shrink_rounds == 1
    # Nearest tests are the ones kept.
    assert shrunk.paths[0] == scope.paths[0]
    assert max(entry.tier for entry in shrunk.entries) < max(entry.tier for entry in scope.entries)


def test_a_run_inside_the_cap_does_not_shrink_the_scope(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    assert (
        shrink_scope_for_runtime(
            scope, observed_seconds=1.0, policy=BlastRadiusPolicy(scope_seconds_cap=10.0)
        )
        is None
    )


def test_shrinking_bottoms_out_instead_of_emptying(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    policy = BlastRadiusPolicy(scope_seconds_cap=0.001)
    for _ in range(10):
        smaller = shrink_scope_for_runtime(scope, observed_seconds=999.0, policy=policy)
        if smaller is None:
            break
        scope = smaller
    assert len(scope.entries) >= MIN_SCOPE_FILES
    assert not scope.empty


def test_a_shrunk_scope_still_gates(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    shrunk = shrink_scope_for_runtime(
        scope, observed_seconds=999.0, policy=BlastRadiusPolicy(scope_seconds_cap=1.0)
    )
    assert shrunk is not None
    command = _pytest_scope_command(shrunk)
    broken = f"{shrunk.paths[0]}::test_widen"
    assessment = assess_blast_radius(
        scope=shrunk,
        runs=[
            _scope_run(repo, command, phase=ScopePhase.BASELINE),
            _scope_run(repo, command, phase=ScopePhase.GATE, failed=[broken]),
        ],
        applicable=True,
    )
    assert assessment.status == BlastRadiusStatus.REGRESSED
    assert assessment.new_failures == (broken,)


def test_the_summary_admits_a_shrunk_scope(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    shrunk = shrink_scope_for_runtime(
        scope, observed_seconds=999.0, policy=BlastRadiusPolicy(scope_seconds_cap=1.0)
    )
    assert shrunk is not None
    command = _pytest_scope_command(shrunk)
    assessment = assess_blast_radius(
        scope=shrunk,
        runs=[
            _scope_run(repo, command, phase=ScopePhase.BASELINE),
            _scope_run(repo, command, phase=ScopePhase.GATE),
        ],
        applicable=True,
    )
    summary = build_blast_radius_status_summary(assessment)
    assert "shrunk to stay inside the runtime cap" in summary


def test_a_shrink_survives_the_scope_being_reselected(repo: Path) -> None:
    """A later edit must not hand back a scope the runtime cap already rejected."""
    index = build_repo_test_index(repo)
    policy = BlastRadiusPolicy(scope_seconds_cap=1.0)
    scope = select_blast_radius_scope(touched_paths=["src/pkg/core.py"], index=index, policy=policy)
    shrunk = shrink_scope_for_runtime(scope, observed_seconds=999.0, policy=policy)
    assert shrunk is not None

    reselected = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py", "src/pkg/other.py"], index=index, policy=policy
    )
    assert len(reselected.entries) > len(shrunk.entries)
    carried = apply_scope_shrink_rounds(reselected, shrunk.shrink_rounds)
    assert carried.shrink_rounds == shrunk.shrink_rounds
    assert len(carried.entries) < len(reselected.entries)
    assert max(entry.tier for entry in carried.entries) < max(
        entry.tier for entry in reselected.entries
    )


def test_applying_zero_shrink_rounds_is_a_no_op(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    assert apply_scope_shrink_rounds(scope, 0) == scope
    assert apply_scope_shrink_rounds(scope, -3) == scope


# ---------------------------------------------------------------------------
# Honest degradation: a scope that ran but could not be read
# ---------------------------------------------------------------------------


def test_an_unreadable_scope_run_is_not_reported_as_never_run(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    _edit(state, repo, "src/pkg/core.py")
    # A runner whose output carries no parseable per-test results.
    _record_tool_effect(
        root=repo,
        state=state,
        tool_name="shell_run",
        arguments={"cmd": command},
        status="ok",
        result={"cmd": command, "exit_code": 0, "stdout": "1 passed\n", "stderr": ""},
        known_verification_commands=[],
    )

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == BlastRadiusStatus.UNREADABLE
    # Nudging here would loop forever: re-running the same runner cannot help.
    assert not blast_radius_blocks_finalization(assessment, material_edit_count=1)
    assert (
        _completion_gate_problems(
            state=state,
            final_text="Done.",
            blocked=False,
            verification_expected=False,
            turn_intent="execute",
            blast_radius_enabled=True,
        )
        == []
    )
    summary = build_blast_radius_status_summary(assessment)
    assert "could not read per-test results" in summary


def test_structured_verify_pass_overrides_truncated_runner_output(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    _run_tests(state, repo, command)
    _edit(state, repo, "src/pkg/core.py")
    _record_tool_effect(
        root=repo,
        state=state,
        tool_name="verify_run",
        arguments={"commands": [command]},
        status="ok",
        result={
            "commands": [command],
            "all_passed": True,
            "command_results": [
                {
                    "command": command,
                    "effective_command": command,
                    "exit_code": 0,
                    "status": "passed",
                    "ok": True,
                    "real_execution": True,
                    "output_preview": "... output truncated before summary ...",
                    "output_truncated": True,
                }
            ],
        },
        known_verification_commands=[command],
        verification_authoritative=True,
    )

    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    summary = build_blast_radius_status_summary(assessment)

    assert assessment.status == BlastRadiusStatus.CLEAN
    assert "nothing that passed before it fails now" in summary
    assert "could not read per-test results" not in summary


def test_the_file_cap_records_what_it_dropped(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"],
        index=build_repo_test_index(repo),
        policy=BlastRadiusPolicy(max_scope_files=1),
    )
    assert len(scope.entries) == 1
    assert scope.dropped_for_cap, "a silent cap reads as full coverage"
    assert "Scope was capped" in build_blast_radius_status_summary(
        assess_blast_radius(
            scope=scope,
            runs=[
                _scope_run(repo, "pytest -q", phase=ScopePhase.BASELINE),
                _scope_run(repo, "pytest -q", phase=ScopePhase.GATE),
            ],
            applicable=True,
        )
    )


def test_a_run_duration_is_captured_from_the_tool_call(repo: Path) -> None:
    state = _scoped_state(repo)
    _run_tests(state, repo, "pytest -q", elapsed_ms=4_500)
    assert state.blast_radius_runs[-1].duration_seconds == pytest.approx(4.5)


# ---------------------------------------------------------------------------
# Kill-switch, policy resolution and non-applicability
# ---------------------------------------------------------------------------


def test_kill_switch_env_wins_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AppConfig(blast_radius_gate_enabled=True)
    monkeypatch.setenv("ALYSIS_BLAST_RADIUS", "off")
    assert _blast_radius_gate_enabled(cfg) is False
    monkeypatch.setenv("ALYSIS_BLAST_RADIUS", "on")
    assert _blast_radius_gate_enabled(AppConfig(blast_radius_gate_enabled=False)) is True
    monkeypatch.delenv("ALYSIS_BLAST_RADIUS")
    assert _blast_radius_gate_enabled(cfg) is True
    assert _blast_radius_gate_enabled(AppConfig(blast_radius_gate_enabled=False)) is False


def test_disabled_gate_still_captures_runs_but_never_blocks(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    _run_tests(state, repo, command)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command, failed=["tests/test_core.py::test_widen"])

    assert state.blast_radius_runs, "capture is telemetry and is never kill-switched"
    problems = _completion_gate_problems(
        state=state,
        final_text="Done.",
        blocked=False,
        verification_expected=False,
        turn_intent="execute",
        blast_radius_enabled=False,
    )
    assert not any(problem.startswith("blast_radius") for problem in problems)
    assert state.latest_blast_radius_assessment == {}


def test_policy_falls_back_on_nonsense_config_values() -> None:
    class _Broken:
        blast_radius_max_scope_files = 0
        blast_radius_scope_seconds_cap = float("nan")
        blast_radius_over_broad_threshold = "twenty"

    policy = resolve_blast_radius_policy(_Broken())
    assert policy == BlastRadiusPolicy()


def test_policy_reads_the_real_config() -> None:
    policy = resolve_blast_radius_policy(
        AppConfig(
            blast_radius_max_scope_files=7,
            blast_radius_scope_seconds_cap=12.5,
            blast_radius_over_broad_threshold=3,
        )
    )
    assert policy == BlastRadiusPolicy(
        max_scope_files=7, scope_seconds_cap=12.5, over_broad_threshold=3
    )


def test_a_non_execute_turn_is_not_applicable(repo: Path) -> None:
    state = _scoped_state(repo)
    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="chat")
    assert assessment.status == BlastRadiusStatus.NOT_APPLICABLE
    assert assessment.applicable is False


def test_a_non_python_scope_is_not_gated(tmp_path: Path) -> None:
    """No per-test ids to read means no honest diff, so the gate stays out of it."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "widget.ts").write_text("export const widget = 1;\n", encoding="utf-8")
    (tmp_path / "src" / "widget.test.ts").write_text(
        "import { widget } from './widget';\n", encoding="utf-8"
    )
    scope = select_blast_radius_scope(
        touched_paths=["src/widget.ts"], index=build_repo_test_index(tmp_path)
    )
    assert "src/widget.test.ts" in scope.paths
    assert scope.diffable is False
    assessment = assess_blast_radius(scope=scope, runs=[], applicable=True)
    assert assessment.status == BlastRadiusStatus.NOT_APPLICABLE


def test_the_scope_advisory_states_whether_a_baseline_exists(repo: Path) -> None:
    scope = select_blast_radius_scope(
        touched_paths=["src/pkg/core.py"], index=build_repo_test_index(repo)
    )
    with_baseline = build_blast_radius_scope_advisory(scope, has_baseline=True)
    without_baseline = build_blast_radius_scope_advisory(scope, has_baseline=False)
    assert "A recorded run before edits to existing files covers this scope" in with_baseline
    assert "cannot yet be told apart" in without_baseline
    for text in (with_baseline, without_baseline):
        assert "tests/test_core.py" in text
        assert "Advisory only" in text
    assert build_blast_radius_scope_advisory(scope, has_baseline=True) != ""


def test_has_blast_radius_baseline_tracks_coverage(repo: Path) -> None:
    state = _scoped_state(repo)
    assert state.has_blast_radius_baseline() is False
    _run_tests(state, repo, "python -m pytest tests/test_core.py -q")
    assert state.has_blast_radius_baseline() is False, "a partial run is not a scope baseline"
    _run_tests(state, repo, "pytest -q")
    assert state.has_blast_radius_baseline() is True


def test_state_payload_carries_the_scope_and_assessment(repo: Path) -> None:
    state = _scoped_state(repo)
    command = _pytest_scope_command(state.blast_radius_scope)
    _run_tests(state, repo, command)
    _edit(state, repo, "src/pkg/core.py")
    _run_tests(state, repo, command, failed=["tests/test_core.py::test_widen"])
    state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")

    payload = state.as_payload()
    assert payload["blast_radius_scope"]["paths"]
    assert payload["blast_radius_runs"]
    assert payload["blast_radius_assessment"]["new_failures"] == ["tests/test_core.py::test_widen"]
    assert payload["completion_gate_blast_radius_repair_attempts"] == 0


# ---------------------------------------------------------------------------
# Scripted sessions: turn-level wiring (directive, scope advisory, summary)
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


def _scripted_session(root: Path, session_id: str) -> Any:
    class ScriptedSurface(NoopSurface):
        def request_approval(self, request):
            return ApprovalDecision(request.kind == "shell_run")

    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", verify_commands=["pytest -q"]),
        root=root,
        mode="auto",
        yes=True,
        max_steps=10,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=True,
        session_log_dir_override=root / "sessions",
        session_id_override=session_id,
        surface=ScriptedSurface(),
    )
    # The scripted shell below models host execution in this fixture workspace.
    session.shell_runner = HostShellRunner()
    return session


def _fake_shell_run_breaking_a_neighbour(root: Path):
    """A ``shell_run`` stand-in whose pytest output tracks the real source state.

    The neighbouring test "passes" while ``core.py`` is intact and "fails" once the
    change lands, so the scripted run exercises the real clean-tree/patched ordering
    instead of a hard-coded pair of outputs.
    """

    def fake_shell_run(*, root: Path, cmd: str, cwd: str | None = None, runner=None):
        _ = cwd, runner
        source = (root / "src" / "pkg" / "core.py").read_text(encoding="utf-8")
        broken = "widen" not in source
        failed = ["tests/test_consumer.py::test_consumer"] if broken else []
        return {
            "cmd": cmd,
            "effective_cmd": cmd,
            "exit_code": 1 if broken else 0,
            "stdout": _pytest_output(failed=failed),
            "stderr": "",
        }

    _ = root
    return fake_shell_run


def _final_text(root: Path, session_id: str) -> str:
    events = list(read_session_events(root / "sessions" / f"{session_id}.jsonl"))
    finals = [event for event in events if event.get("type") == "final"]
    assert finals, "the turn produced no final event"
    return str((finals[-1].get("payload") or {}).get("content") or "")


def _event_types(root: Path, session_id: str) -> set[str]:
    events = list(read_session_events(root / "sessions" / f"{session_id}.jsonl"))
    return {str(event.get("type") or "") for event in events}


_SCOPE_COMMAND = (
    "python -m pytest tests/test_core.py tests/test_consumer.py tests/pkg/test_smoke.py -q"
)
_BREAKING_EDIT = "def broadened(value):\n    return value\n"


def test_scripted_run_reports_breakage_it_could_not_clear(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: baseline clean, change breaks a neighbour, summary says so."""
    monkeypatch.setattr(agent_loop_mod, "shell_run", _fake_shell_run_breaking_a_neighbour(repo))
    session = _scripted_session(repo, "blast-radius-broken")
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="shell_run", arguments={"cmd": _SCOPE_COMMAND})
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="fs_write",
                        arguments={"path": "src/pkg/core.py", "content": _BREAKING_EDIT},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc3", name="shell_run", arguments={"cmd": _SCOPE_COMMAND})
                ],
                raw={},
            ),
            LLMResponse(content="Renamed the helper as asked.", tool_calls=[], raw={}),
        ]
    )

    try:
        exit_code = session.run_turn("Rename widen in src/pkg/core.py.")
    finally:
        session.close()

    assert exit_code == 0
    # The directive went out at turn start, while the tree was still clean.
    assert any("Blast-radius protocol" in prompt for prompt in session.client.seen_system_prompts)
    # The concrete scope was advertised once the first change landed.
    assert "blast_radius_scope_selected" in _event_types(repo, "blast-radius-broken")
    assert "blast_radius_scope_advisory" in _event_types(repo, "blast-radius-broken")
    # And the breakage is stated in the visible summary, not swallowed.
    final_text = _final_text(repo, "blast-radius-broken")
    assert "REGRESSIONS INTRODUCED" in final_text
    assert "tests/test_consumer.py::test_consumer" in final_text
    assert "KNOWN BREAKAGE" in final_text


def test_scripted_clean_run_reports_a_clean_blast_radius(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same wiring on a change that breaks nothing still reports what it checked."""
    monkeypatch.setattr(agent_loop_mod, "shell_run", _fake_shell_run_breaking_a_neighbour(repo))
    session = _scripted_session(repo, "blast-radius-clean")
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc1", name="shell_run", arguments={"cmd": _SCOPE_COMMAND})
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc2",
                        name="fs_write",
                        arguments={
                            "path": "src/pkg/core.py",
                            "content": "def widen(value):\n    return value or value\n",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc3", name="shell_run", arguments={"cmd": _SCOPE_COMMAND})
                ],
                raw={},
            ),
            LLMResponse(content="Adjusted widen.", tool_calls=[], raw={}),
        ]
    )

    try:
        exit_code = session.run_turn("Adjust widen in src/pkg/core.py.")
    finally:
        session.close()

    assert exit_code == 0
    final_text = _final_text(repo, "blast-radius-clean")
    assert "nothing that passed before it fails now" in final_text
    assert "REGRESSIONS INTRODUCED" not in final_text


def test_scripted_run_with_the_gate_disabled_is_untouched(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALYSIS_BLAST_RADIUS", "off")
    monkeypatch.setattr(agent_loop_mod, "shell_run", _fake_shell_run_breaking_a_neighbour(repo))
    session = _scripted_session(repo, "blast-radius-off")
    session.client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="fs_write",
                        arguments={"path": "src/pkg/core.py", "content": _BREAKING_EDIT},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="tc2", name="shell_run", arguments={"cmd": _SCOPE_COMMAND})
                ],
                raw={},
            ),
            LLMResponse(content="Renamed the helper as asked.", tool_calls=[], raw={}),
        ]
    )

    try:
        session.run_turn("Rename widen in src/pkg/core.py.")
    finally:
        session.close()

    assert not any(
        "Blast-radius protocol" in prompt for prompt in session.client.seen_system_prompts
    )
    assert "REGRESSIONS INTRODUCED" not in _final_text(repo, "blast-radius-off")


def test_config_key_round_trips() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "blast_radius_gate_enabled", "false")
    assert cfg.blast_radius_gate_enabled is False
    cfg = set_config_value(cfg, "blast_radius_gate_enabled", "true")
    assert cfg.blast_radius_gate_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "blast_radius_gate_enabled", "maybe")


def test_knob_config_keys_round_trip_and_reject_nonsense() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "blast_radius_max_scope_files", "12")
    cfg = set_config_value(cfg, "blast_radius_scope_seconds_cap", "45.5")
    cfg = set_config_value(cfg, "blast_radius_over_broad_threshold", "5")
    assert resolve_blast_radius_policy(cfg) == BlastRadiusPolicy(
        max_scope_files=12, scope_seconds_cap=45.5, over_broad_threshold=5
    )
    for key, value in (
        ("blast_radius_max_scope_files", "0"),
        ("blast_radius_over_broad_threshold", "x"),
        # inf/nan parse as floats but would silently disable the cap; direct
        # attribute assignment does not re-run the field's allow_inf_nan=False.
        ("blast_radius_scope_seconds_cap", "inf"),
        ("blast_radius_scope_seconds_cap", "nan"),
    ):
        with pytest.raises(ConfigError):
            set_config_value(cfg, key, value)


def test_both_blast_radius_stages_share_one_repair_budget(repo: Path) -> None:
    state = _scoped_state(repo)
    state.increment_repair_attempts_for_stage("blast_radius_unverified")
    state.increment_repair_attempts_for_stage("blast_radius_regressions")
    # Two faces of one protocol: alternating between them must not buy extra rounds.
    assert state.repair_attempts_for_stage("blast_radius_unverified") == 2
    assert state.repair_attempts_for_stage("blast_radius_regressions") == 2
    assert state.completion_gate_repair_attempts == 2
    assert state.completion_gate_regression_repair_attempts == 0


def test_proven_breakage_outranks_a_generic_verification_failure() -> None:
    assert (
        _completion_gate_repair_stage(["verification_failed", "blast_radius_regressions"])
        == "blast_radius_regressions"
    )


def test_an_unmeasured_blast_radius_yields_to_the_verification_stages() -> None:
    # "You ran no tests at all" is the more fundamental complaint and owns the loop;
    # the blast-radius coverage stage takes over once that is satisfied.
    assert (
        _completion_gate_repair_stage(["verification_not_attempted", "blast_radius_unverified"])
        == "verification_not_attempted"
    )
    assert _completion_gate_repair_stage(["blast_radius_unverified"]) == "blast_radius_unverified"
