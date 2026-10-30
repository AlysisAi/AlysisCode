from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import alysis_code.skills.eval_runner as eval_runner_module
import alysis_code.skills.evals as evals_module
from alysis_code.config import AppConfig
from alysis_code.skills.eval_models import (
    SkillsEvalAuthPreflightResult,
    SkillsEvalCase,
    SkillsEvalExecutionRequest,
    SkillsEvalExecutionResult,
    SkillsEvalMode,
    SkillsEvalRecord,
    SkillsEvalVerificationResult,
)
from alysis_code.skills.eval_runner import (
    OneShotSkillsEvalExecutor,
    classify_skills_eval_failure,
    run_skills_eval_auth_preflight,
)
from alysis_code.skills.evals import (
    aggregate_skills_eval_records,
    evaluate_skills_launch_readiness,
    extract_skills_eval_metrics,
    load_skills_eval_cases,
    prepared_skills_eval_workspace,
    render_skills_eval_summary_markdown,
    resolve_skills_eval_modes,
    run_shell_verification_command,
    run_skills_eval_suite,
    summarize_skills_launch_candidate_metrics,
)
from alysis_code.skills.models import SkillBundle
from alysis_code.skills.prompting import EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
from alysis_code.verification_command_analysis import analyze_verification_command

_BUNDLED_PACK_NAMES = {
    "alysis-docs",
    "image-pdf-files",
    "office-files",
    "local-files",
    "address-pr-comments",
    "code-review",
    "commit",
    "debug",
    "fix-ci",
    "release-notes",
    "security-review",
    "skill-creator",
    "documents",
    "spreadsheets",
    "presentations",
    "pdf",
    "research",
}


class _FakeExecutor:
    def __init__(self) -> None:
        self.requests: list[SkillsEvalExecutionRequest] = []

    def execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        self.requests.append(request)
        session_log_path = request.sessions_dir / f"{request.session_id}.jsonl"
        session_log_path.parent.mkdir(parents=True, exist_ok=True)
        session_log_path.write_text("", encoding="utf-8")

        conventions_present = any(
            (request.workspace / filename).exists()
            for filename in ("AGENTS.md", "CLAUDE.md", "CONVENTIONS.md")
        )
        skills_root_exists = (request.workspace / ".alysis_skills").exists()
        matched_skill_names: tuple[str, ...] = ()
        skill_read_names: tuple[str, ...] = ()
        explicit_used = False
        if request.case.invocation_mode == "explicit_skill":
            explicit_used = True
        elif request.mode.skills_enabled and request.case.expected_skills:
            skill_read_names = (request.case.expected_skills[0],)

        automatic_selection = bool(
            request.mode.skills_enabled
            and request.mode.skills_auto_invoke
            and request.case.invocation_mode == "normal"
        )
        selection_status = None
        selection_names: tuple[str, ...] = ()
        if automatic_selection:
            selection_status = "selected" if request.case.expected_skills else "no_match"
            selection_names = request.case.expected_skills

        tool_call_count = len(skill_read_names)
        return SkillsEvalExecutionResult(
            agent_exit_code=0,
            skills_advertised_present=request.mode.skills_enabled and skills_root_exists,
            repo_conventions_present=conventions_present,
            matched_skill_context_attached=bool(matched_skill_names),
            matched_skill_names=matched_skill_names,
            explicit_skill_context_used=explicit_used,
            skill_read_called=bool(skill_read_names),
            skill_read_names=skill_read_names,
            skill_read_call_count=len(skill_read_names),
            successful_skill_read_names=skill_read_names,
            successful_skill_read_count=len(skill_read_names),
            skill_selection_status=selection_status,
            skill_selection_selected_names=selection_names,
            skill_selection_call_count=int(automatic_selection),
            tool_call_count=tool_call_count,
            session_log_path=session_log_path,
            session_artifact_root=request.sessions_dir / request.session_id,
        )


def _fake_verification_runner(
    *,
    workspace: Path,
    command: str,
    timeout_seconds: float | None,
) -> SkillsEvalVerificationResult:
    _ = workspace, command, timeout_seconds
    return SkillsEvalVerificationResult(exit_code=0, output_preview="ok")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _prepare_commit_verifier_repo(
    repo: Path,
    *,
    include_debug_log: bool = False,
    add_third_commit: bool = False,
    create_target_commit: bool = True,
    settings_content: str = "retry_limit = 3\n",
) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Skills Eval")
    _git(repo, "config", "user.email", "skills-eval@example.invalid")
    (repo / "README.md").write_text("# Commit verifier\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "chore: initialize fixture")

    (repo / "settings.toml").write_text(settings_content, encoding="utf-8")
    (repo / "debug.log").write_text("tmp trace line\n", encoding="utf-8")
    if not create_target_commit:
        return
    staged_paths = ["settings.toml"]
    if include_debug_log:
        staged_paths.append("debug.log")
    _git(repo, "add", *staged_paths)
    _git(repo, "commit", "-qm", "feat: configure retry limit")

    if add_third_commit:
        (repo / "settings.toml").write_text("retry_limit = 4\n", encoding="utf-8")
        _git(repo, "add", "settings.toml")
        _git(repo, "commit", "-qm", "fix: adjust retry limit")


class _ExecutorFakeStore:
    def __init__(self, path: Path) -> None:
        self.enabled = True
        self.path = path
        self.session_artifact_root = path.parent / "artifacts"


class _ExecutorFakeSession:
    def __init__(self, tmp_path: Path, skill: SkillBundle) -> None:
        self.messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "<skill_context>\n- pytest\n</skill_context>\n"},
            {"role": "user", "content": "<repo_conventions>\nrepo rules\n</repo_conventions>\n"},
        ]
        self.skill_registry = {"pytest": skill}
        self.store = _ExecutorFakeStore(tmp_path / "sessions" / "skills-eval.jsonl")
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "tool_call",
                            "payload": {"name": "skill_read", "arguments": {"name": "pytest"}},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "tool_call",
                            "payload": {"name": "fs_read", "arguments": {"path": "README.md"}},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.run_turn_calls: list[dict[str, Any]] = []
        self.closed = False

    def run_turn(self, instruction: str, **kwargs: Any) -> int:
        self.run_turn_calls.append({"instruction": instruction, **kwargs})
        return 0

    def close(self) -> None:
        self.closed = True


def test_load_skills_eval_cases_parses_sample_fixture_manifest() -> None:
    manifest = Path("tests/fixtures/skills_eval/sample_cases.json").resolve()

    cases = load_skills_eval_cases(manifest)

    assert len(cases) == 5
    assert cases[0].id == "conventions_parser_tests"
    assert cases[0].workspace.is_dir()
    explicit = next(case for case in cases if case.invocation_mode == "explicit_skill")
    assert explicit.explicit_skill_name == "pytest"
    assert explicit.expected_skills == ("pytest",)
    assert any(case.id == "empty_workspace_first_skill_authoring" for case in cases)


def test_load_bundled_pack_eval_cases_covers_positive_explicit_and_negative_cases() -> None:
    manifest = Path("tests/fixtures/skills_eval/bundled_pack_cases.json").resolve()

    cases = load_skills_eval_cases(manifest)

    assert len(cases) == 52
    for name in sorted(_BUNDLED_PACK_NAMES):
        matching = [case for case in cases if case.expected_skills == (name,)]
        expected_normal_count = 2 if name == "commit" else 1
        assert (
            sum(case.invocation_mode == "normal" for case in matching) == expected_normal_count
        ), name
        assert sum(case.invocation_mode == "explicit_skill" for case in matching) == 1, name
        explicit = next(case for case in matching if case.invocation_mode == "explicit_skill")
        assert explicit.explicit_skill_name == name
        normal = next(case for case in matching if case.invocation_mode == "normal")
        assert f"${name}" not in normal.task
        assert normal.verification_command

    negatives = [case for case in cases if "negative-control" in case.tags]
    assert len(negatives) == len(_BUNDLED_PACK_NAMES)
    assert all(not case.expected_skills for case in negatives)
    assert all(case.verification_command for case in negatives)
    assert all(case.session_mode == "readonly" for case in negatives)
    adjacent_skills = {
        tag.removesuffix("-adjacent")
        for case in negatives
        for tag in case.tags
        if tag.endswith("-adjacent")
    }
    assert adjacent_skills == _BUNDLED_PACK_NAMES
    assert all(case.workspace.is_dir() for case in cases)
    assert eval_runner_module._launch_suite_coverage_errors(cases) == ()

    truncated_pack = tuple(
        case
        for case in cases
        if case.expected_skills == ("debug",) or "debug-adjacent" in case.tags
    )
    truncated_errors = eval_runner_module._launch_suite_coverage_errors(truncated_pack)
    assert any(error.startswith("missing bundled skill coverage") for error in truncated_errors)

    unstaged_commit = next(case for case in cases if case.id == "bundled_commit_unstaged_normal")
    assert unstaged_commit.workspace.name == "bundled_commit_unstaged"
    assert unstaged_commit.invocation_mode == "normal"
    assert unstaged_commit.expected_skills == ("commit",)
    assert unstaged_commit.tags == ("bundled-pack", "positive", "unstaged")
    assert unstaged_commit.verification_command

    specific_ci = next(case for case in cases if case.id == "bundled_fix_ci_normal")
    assert specific_ci.expected_skills == ("fix-ci",)
    assert "failing" in specific_ci.task.casefold()
    assert "ci-output.txt" in specific_ci.task

    release_concept = next(
        case for case in cases if case.id == "bundled_negative_release_notes_concept"
    )
    assert release_concept.expected_skills == ()
    assert "explain" in release_concept.task.casefold()
    assert "do not" in release_concept.task.casefold()
    assert "release-notes-adjacent" in release_concept.tags

    verifier_analyses = {
        case.id: analyze_verification_command(
            case.verification_command,
            trusted=True,
            workspace_root=case.workspace,
        )
        for case in cases
    }
    invalid_verifiers = {
        case_id: analysis
        for case_id, analysis in verifier_analyses.items()
        if not analysis.is_valid_verifier
    }
    assert not invalid_verifiers


def test_load_skills_eval_cases_rejects_unknown_session_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "id": "invalid-mode",
                    "workspace": "workspace",
                    "task": "Read the fixture.",
                    "session_mode": "unrestricted",
                }
            ]
        ),
        encoding="utf-8",
    )

    try:
        load_skills_eval_cases(manifest)
    except ValueError as exc:
        assert "unsupported session_mode: unrestricted" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("invalid session_mode was accepted")


def test_load_skills_eval_cases_rejects_more_permissive_session_modes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = tmp_path / "cases.json"

    for session_mode in ("review", "auto", "fullaccess"):
        manifest.write_text(
            json.dumps(
                [
                    {
                        "id": "invalid-mode",
                        "workspace": "workspace",
                        "task": "Read the fixture.",
                        "session_mode": session_mode,
                    }
                ]
            ),
            encoding="utf-8",
        )

        try:
            load_skills_eval_cases(manifest)
        except ValueError as exc:
            assert f"unsupported session_mode: {session_mode}" in str(exc)
        else:  # pragma: no cover - assertion helper
            raise AssertionError(f"session_mode {session_mode!r} was accepted")


def test_load_skills_eval_cases_rejects_parent_workspace_escape(tmp_path: Path) -> None:
    manifest_root = tmp_path / "manifests"
    manifest_root.mkdir()
    (tmp_path / "outside").mkdir()
    manifest = manifest_root / "cases.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "id": "escaped-workspace",
                    "workspace": "../outside",
                    "task": "Read the fixture.",
                }
            ]
        ),
        encoding="utf-8",
    )

    try:
        load_skills_eval_cases(manifest)
    except ValueError as exc:
        assert "workspace resolves outside the manifest directory" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("parent workspace escape was accepted")


def test_load_skills_eval_cases_rejects_symlink_workspace_escape(
    tmp_path: Path, require_symlinks
) -> None:
    manifest_root = tmp_path / "manifests"
    manifest_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (manifest_root / "workspace-link").symlink_to(outside, target_is_directory=True)
    manifest = manifest_root / "cases.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "id": "escaped-workspace",
                    "workspace": "workspace-link",
                    "task": "Read the fixture.",
                }
            ]
        ),
        encoding="utf-8",
    )

    try:
        load_skills_eval_cases(manifest)
    except ValueError as exc:
        assert "workspace resolves outside the manifest directory" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("symlink workspace escape was accepted")


def test_skills_eval_case_preserves_existing_positional_field_order(tmp_path: Path) -> None:
    case = SkillsEvalCase(
        "case",
        tmp_path,
        "task",
        "normal",
        None,
        ("debug",),
        "python repro.py",
        ("tag",),
        "notes",
    )

    assert case.tags == ("tag",)
    assert case.notes == "notes"
    assert case.session_mode is None


def test_shell_verification_uses_invoking_interpreter_when_path_lacks_python(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", "")

    result = run_shell_verification_command(
        workspace=tmp_path,
        command='python -c "import sys; print(sys.prefix)"',
    )

    assert result.exit_code == 0
    assert Path(result.output_preview).resolve() == Path(sys.prefix).resolve()


def test_shell_verification_keeps_python_assertions_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")

    result = run_shell_verification_command(
        workspace=tmp_path,
        command='python -c "assert False"',
    )

    assert result.exit_code != 0
    assert "AssertionError" in result.output_preview


def test_shell_verification_timeout_terminates_the_command_process_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    interpreter_dir = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", interpreter_dir + os.pathsep + os.environ.get("PATH", ""))
    command = (
        'python -c "import subprocess,sys,time; '
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)']); "
        'time.sleep(5)"'
    )

    started_at = time.monotonic()
    result = run_shell_verification_command(
        workspace=tmp_path,
        command=command,
        timeout_seconds=0.05,
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 2.0
    assert result.exit_code == 124
    assert "timed out" in result.output_preview.casefold()


def test_shell_verification_timeout_terminates_windows_process_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    popen_kwargs: dict[str, Any] = {}
    taskkill_calls: list[tuple[list[str], dict[str, Any]]] = []

    class _TimedOutProcess:
        pid = 4321
        returncode = -9
        communicate_calls = 0
        killed = False

        def communicate(self, *, timeout: float | None) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("verifier", timeout)
            return "", ""

        def kill(self) -> None:
            self.killed = True

    proc = _TimedOutProcess()

    def _fake_popen(*_args: Any, **kwargs: Any) -> _TimedOutProcess:
        popen_kwargs.update(kwargs)
        return proc

    def _fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        taskkill_calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        evals_module,
        "_VERIFICATION_PLATFORM_NAME",
        "nt",
        raising=False,
    )
    monkeypatch.setattr(evals_module.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(evals_module.subprocess, "run", _fake_run)

    result = run_shell_verification_command(
        workspace=tmp_path,
        command="verifier",
        timeout_seconds=0.05,
    )

    assert popen_kwargs["creationflags"] == 0x00000200
    assert "start_new_session" not in popen_kwargs
    assert taskkill_calls == [
        (
            ["taskkill", "/PID", "4321", "/T", "/F"],
            {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "check": False,
                "timeout": 1.0,
            },
        )
    ]
    assert proc.killed is False
    assert result.exit_code == 124


def test_shell_verification_cancellation_terminates_and_reaps_process_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    communicate_calls = 0
    killed_groups: list[tuple[int, signal.Signals]] = []

    class _InterruptedProcess:
        pid = 5432
        returncode = -9

        def communicate(self, *, timeout: float | None) -> tuple[str, str]:
            nonlocal communicate_calls
            communicate_calls += 1
            if communicate_calls == 1:
                raise KeyboardInterrupt
            assert timeout == 1.0
            return "", ""

        def kill(self) -> None:
            raise AssertionError("process-group termination should be sufficient")

    monkeypatch.setattr(
        evals_module,
        "_VERIFICATION_PLATFORM_NAME",
        "posix",
        raising=False,
    )
    monkeypatch.setattr(
        evals_module.subprocess, "Popen", lambda *_args, **_kwargs: _InterruptedProcess()
    )
    monkeypatch.setattr(
        evals_module.os,
        "killpg",
        lambda pid, sig: killed_groups.append((pid, sig)),
        raising=False,
    )
    monkeypatch.setattr(evals_module.signal, "SIGKILL", 9, raising=False)

    with pytest.raises(KeyboardInterrupt):
        run_shell_verification_command(
            workspace=tmp_path,
            command="verifier",
            timeout_seconds=5.0,
        )

    assert killed_groups == [(5432, signal.SIGKILL)]
    assert communicate_calls == 2


def test_bundled_code_review_verifiers_require_the_seeded_zero_count_finding(
    tmp_path: Path,
) -> None:
    manifest = Path("tests/fixtures/skills_eval/bundled_pack_cases.json").resolve()
    cases = [
        case
        for case in load_skills_eval_cases(manifest)
        if case.expected_skills == ("code-review",)
    ]
    reports = (
        "P1 src/calculator.py:2 - A zero count slips through the guard, so the division "
        "crashes by dividing by zero. Change the guard to `count <= 0`.\n",
        "calculator.py:2: Use `count <= 0`; `count == 0` currently reaches the division "
        "and raises `ZeroDivisionError`.\n",
    )
    invalid_reports = (
        "calculator.py: no findings; everything is correct\n",
        "calculator.py:2 - count zero division is not a bug; no finding is needed.\n",
        "calculator.py:99 - Change the guard to `count <= 0`.\n",
    )

    assert len(cases) == len(reports) == 2
    for case, report in zip(cases, reports, strict=True):
        assert case.verification_command
        for index, invalid_report in enumerate(invalid_reports):
            invalid_workspace = tmp_path / f"{case.id}-invalid-{index}"
            invalid_workspace.mkdir()
            (invalid_workspace / "REVIEW.md").write_text(
                invalid_report,
                encoding="utf-8",
            )
            invalid = run_shell_verification_command(
                workspace=invalid_workspace,
                command=case.verification_command,
            )
            assert invalid.exit_code != 0, (case.id, invalid_report)

        valid_workspace = tmp_path / f"{case.id}-valid"
        valid_workspace.mkdir()
        (valid_workspace / "REVIEW.md").write_text(report, encoding="utf-8")

        valid = run_shell_verification_command(
            workspace=valid_workspace,
            command=case.verification_command,
        )

        assert valid.exit_code == 0, (case.id, valid.output_preview)


def test_bundled_commit_unstaged_verification_command_checks_scope_and_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = Path("tests/fixtures/skills_eval/bundled_pack_cases.json").resolve()
    commit_case = next(
        case
        for case in load_skills_eval_cases(manifest)
        if case.id == "bundled_commit_unstaged_normal"
    )
    assert commit_case.verification_command
    interpreter_dir = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", interpreter_dir + os.pathsep + os.environ.get("PATH", ""))

    passing_repo = tmp_path / "passing"
    _prepare_commit_verifier_repo(passing_repo)
    passing = run_shell_verification_command(
        workspace=passing_repo,
        command=commit_case.verification_command,
    )

    debug_repo = tmp_path / "debug-included"
    _prepare_commit_verifier_repo(debug_repo, include_debug_log=True)
    debug_included = run_shell_verification_command(
        workspace=debug_repo,
        command=commit_case.verification_command,
    )

    third_commit_repo = tmp_path / "third-commit"
    _prepare_commit_verifier_repo(third_commit_repo, add_third_commit=True)
    third_commit = run_shell_verification_command(
        workspace=third_commit_repo,
        command=commit_case.verification_command,
    )

    assert passing.exit_code == 0, passing.output_preview
    assert debug_included.exit_code == 1, debug_included.output_preview
    assert third_commit.exit_code == 1, third_commit.output_preview


def test_original_bundled_commit_verifiers_require_the_requested_scoped_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = Path("tests/fixtures/skills_eval/bundled_pack_cases.json").resolve()
    commit_cases = [
        case
        for case in load_skills_eval_cases(manifest)
        if case.id in {"bundled_commit_normal", "bundled_commit_explicit"}
    ]
    assert len(commit_cases) == 2
    interpreter_dir = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", interpreter_dir + os.pathsep + os.environ.get("PATH", ""))

    for case in commit_cases:
        assert case.verification_command
        initial_only = tmp_path / f"{case.id}-initial-only"
        _prepare_commit_verifier_repo(initial_only, create_target_commit=False)
        passing = tmp_path / f"{case.id}-passing"
        _prepare_commit_verifier_repo(passing)
        unrelated = tmp_path / f"{case.id}-unrelated"
        _prepare_commit_verifier_repo(unrelated, include_debug_log=True)
        wrong_content = tmp_path / f"{case.id}-wrong-content"
        _prepare_commit_verifier_repo(wrong_content, settings_content="retry_limit = 99\n")

        assert (
            run_shell_verification_command(
                workspace=initial_only,
                command=case.verification_command,
            ).exit_code
            == 1
        ), case.id
        assert (
            run_shell_verification_command(
                workspace=passing,
                command=case.verification_command,
            ).exit_code
            == 0
        ), case.id
        assert (
            run_shell_verification_command(
                workspace=unrelated,
                command=case.verification_command,
            ).exit_code
            == 1
        ), case.id
        assert (
            run_shell_verification_command(
                workspace=wrong_content,
                command=case.verification_command,
            ).exit_code
            == 1
        ), case.id


def test_prepared_bundled_pack_workspace_copies_deterministic_fixture(tmp_path: Path) -> None:
    manifest = Path("tests/fixtures/skills_eval/bundled_pack_cases.json").resolve()
    commit_case = next(
        case for case in load_skills_eval_cases(manifest) if case.id == "bundled_commit_normal"
    )
    mode = next(mode for mode in resolve_skills_eval_modes() if mode.name == "combined_auto")

    with prepared_skills_eval_workspace(
        source_workspace=commit_case.workspace,
        mode=mode,
        temp_base_dir=tmp_path,
    ) as workspace:
        assert (workspace / "prepare_repo.sh").is_file()
        assert "git init" in (workspace / "prepare_repo.sh").read_text(encoding="utf-8")
        assert not (workspace / ".git").exists()


def test_resolve_skills_eval_modes_returns_expected_matrix() -> None:
    modes = resolve_skills_eval_modes()

    assert [mode.name for mode in modes] == [
        "baseline",
        "conventions_only",
        "skills_manual_only",
        "skills_auto_only",
        "combined_manual",
        "combined_auto",
    ]
    assert modes[0].skills_enabled is False and modes[0].conventions_enabled is False
    assert modes[-1].skills_enabled is True and modes[-1].skills_auto_invoke is True


def test_prepared_eval_workspace_uses_explicit_external_temp_base(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "README.md").write_text("hi\n", encoding="utf-8")
    temp_base = tmp_path / "external-temp"
    mode = SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )

    with prepared_skills_eval_workspace(
        source_workspace=source,
        mode=mode,
        temp_base_dir=temp_base,
    ) as workspace:
        assert workspace.exists()
        assert workspace.resolve().is_relative_to(temp_base.resolve())
        assert not workspace.resolve().is_relative_to(source.resolve())
        assert workspace.name == "workspace"
        assert workspace.parent.name.startswith("se-")
        preserved_workspace = workspace

    assert temp_base.exists()
    assert not preserved_workspace.exists()


def test_prepared_eval_workspace_masks_conventions_when_mode_disables_them(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace"
    (source / "nested").mkdir(parents=True)
    (source / "src").mkdir()
    (source / ".git").mkdir()
    (source / ".alysis_skills" / "pytest").mkdir(parents=True)
    (source / ".agents" / "skills" / "lint").mkdir(parents=True)
    (source / ".claude" / "skills" / "docs").mkdir(parents=True)
    (source / ".github" / "skills" / "review").mkdir(parents=True)
    (source / "AGENTS.md").write_text("repo conventions\n", encoding="utf-8")
    (source / "nested" / "CLAUDE.md").write_text("nested conventions\n", encoding="utf-8")
    (source / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / ".alysis_skills" / "pytest" / "SKILL.md").write_text(
        "pytest skill\n",
        encoding="utf-8",
    )
    (source / ".agents" / "skills" / "lint" / "SKILL.md").write_text(
        "lint skill\n",
        encoding="utf-8",
    )
    (source / ".agents" / "notes.md").write_text("keep me\n", encoding="utf-8")
    (source / ".claude" / "skills" / "docs" / "SKILL.md").write_text(
        "docs skill\n",
        encoding="utf-8",
    )
    (source / ".claude" / "workspace-notes.md").write_text("keep me\n", encoding="utf-8")
    (source / ".github" / "skills" / "review" / "SKILL.md").write_text(
        "review skill\n",
        encoding="utf-8",
    )

    baseline = SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )
    conventions_only = SkillsEvalMode(
        name="conventions_only",
        conventions_enabled=True,
        skills_enabled=False,
        skills_auto_invoke=False,
    )
    combined_manual = SkillsEvalMode(
        name="combined_manual",
        conventions_enabled=True,
        skills_enabled=True,
        skills_auto_invoke=False,
    )

    with prepared_skills_eval_workspace(source_workspace=source, mode=baseline) as workspace:
        masked_workspace = workspace
        assert not (workspace / "AGENTS.md").exists()
        assert not (workspace / "nested" / "CLAUDE.md").exists()
        assert not (workspace / ".git").exists()
        assert not (workspace / ".alysis_skills").exists()
        assert not (workspace / ".agents" / "skills").exists()
        assert not (workspace / ".claude" / "skills").exists()
        assert not (workspace / ".github" / "skills").exists()
        assert (workspace / ".agents" / "notes.md").read_text(encoding="utf-8") == "keep me\n"
        assert (workspace / ".claude" / "workspace-notes.md").read_text(
            encoding="utf-8"
        ) == "keep me\n"
        assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"

    assert not masked_workspace.exists()

    with prepared_skills_eval_workspace(
        source_workspace=source,
        mode=conventions_only,
    ) as workspace:
        assert (workspace / "AGENTS.md").exists()
        assert (workspace / "nested" / "CLAUDE.md").exists()
        assert not (workspace / ".git").exists()
        assert not (workspace / ".alysis_skills").exists()
        assert not (workspace / ".agents" / "skills").exists()
        assert not (workspace / ".claude" / "skills").exists()
        assert not (workspace / ".github" / "skills").exists()
        assert (workspace / ".agents" / "notes.md").exists()
        assert (workspace / ".claude" / "workspace-notes.md").exists()

    with prepared_skills_eval_workspace(
        source_workspace=source,
        mode=combined_manual,
    ) as workspace:
        assert (workspace / "AGENTS.md").exists()
        assert (workspace / ".alysis_skills" / "pytest" / "SKILL.md").exists()
        assert (workspace / ".agents" / "skills" / "lint" / "SKILL.md").exists()
        assert (workspace / ".claude" / "skills" / "docs" / "SKILL.md").exists()
        assert (workspace / ".github" / "skills" / "review" / "SKILL.md").exists()
        assert not (workspace / ".git").exists()


def test_prepared_eval_workspace_ignores_local_worktrees_and_reports(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    (source / ".claude" / "worktrees" / "agent-clone" / "src").mkdir(parents=True)
    (source / ".claude" / "workspace-notes.md").write_text("keep me\n", encoding="utf-8")
    (source / ".claude" / "worktrees" / "agent-clone" / "src" / "dup.py").write_text(
        "print('dup')\n",
        encoding="utf-8",
    )
    (source / "reports" / "old-run").mkdir(parents=True)
    (source / "reports" / "old-run" / "summary.json").write_text("{}", encoding="utf-8")
    (source / ".mypy_cache").mkdir()
    (source / ".mypy_cache" / "cache.json").write_text("{}", encoding="utf-8")
    (source / "reports_eval").mkdir()
    (source / "reports_eval" / "notes.md").write_text("keep reports_eval\n", encoding="utf-8")
    (source / "README.md").write_text("hi\n", encoding="utf-8")

    mode = SkillsEvalMode(
        name="baseline",
        conventions_enabled=False,
        skills_enabled=False,
        skills_auto_invoke=False,
    )

    with prepared_skills_eval_workspace(source_workspace=source, mode=mode) as workspace:
        assert (workspace / "README.md").read_text(encoding="utf-8") == "hi\n"
        assert (workspace / ".claude" / "workspace-notes.md").read_text(
            encoding="utf-8"
        ) == "keep me\n"
        assert not (workspace / ".claude" / "worktrees").exists()
        assert not (workspace / "reports").exists()
        assert not (workspace / ".mypy_cache").exists()
        assert (workspace / "reports_eval" / "notes.md").read_text(
            encoding="utf-8"
        ) == "keep reports_eval\n"


def test_extract_skills_eval_metrics_reads_host_events() -> None:
    events = [
        {
            "type": "skill_matches",
            "payload": {
                "matches": [
                    {"name": "pytest"},
                    {"name": "pytest"},
                    {"name": "lint"},
                ]
            },
        },
        {
            "type": "tool_call",
            "payload": {"name": "skill_read", "arguments": {"name": "pytest"}},
        },
        {
            "type": "tool_call",
            "payload": {"name": "fs_read", "arguments": {"path": "README.md"}},
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["matched_skill_context_attached"] is True
    assert metrics["matched_skill_names"] == ("pytest", "lint")
    assert metrics["skill_read_called"] is True
    assert metrics["skill_read_names"] == ("pytest",)
    assert metrics["skill_read_call_count"] == 1
    assert metrics["manual_skill_bundle_accessed"] is False
    assert metrics["manual_skill_bundle_names"] == ()
    assert metrics["manual_skill_bundle_access_count"] == 0
    assert metrics["tool_call_count"] == 2


def test_extract_skills_eval_metrics_separates_blocked_attempts_from_loaded_skills() -> None:
    events = [
        {
            "type": "skill_selection",
            "payload": {
                "status": "selected",
                "selected_names": ["debug"],
                "failure_kind": "",
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "skill_read",
                "arguments": {"name": "fix-ci"},
                "tool_call_id": "blocked-correct",
            },
        },
        {
            "type": "skill_selection_mismatch_blocked",
            "payload": {
                "tool_call_id": "blocked-correct",
                "requested_tool": "skill_read",
                "requested_name": "fix-ci",
                "reason": "selected_skill_mismatch",
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "skill_read",
                "tool_call_id": "blocked-correct",
                "result": {"error": "blocked"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "skill_read",
                "arguments": {"name": "debug"},
                "tool_call_id": "loaded-wrong",
            },
        },
        {
            "type": "skill_selection_read_satisfied",
            "payload": {"tool_call_id": "loaded-wrong", "name": "debug"},
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "skill_read",
                "tool_call_id": "loaded-wrong",
                "result": {"name": "debug", "path": "SKILL.md"},
            },
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["skill_read_names"] == ("fix-ci", "debug")
    assert metrics["successful_skill_read_names"] == ("debug",)
    assert metrics["successful_skill_read_count"] == 1
    assert metrics["skill_selection_status"] == "selected"
    assert metrics["skill_selection_selected_names"] == ("debug",)
    assert metrics["skill_selection_call_count"] == 1
    assert metrics["skill_selection_blocked_count"] == 1
    assert metrics["skill_selection_blocked_reasons"] == ("selected_skill_mismatch",)


def test_automatic_selection_scoring_requires_exact_selection_and_successful_load() -> None:
    base = dict(
        case_id="fix-ci",
        mode="combined_auto",
        workspace=Path("/tmp/fix-ci"),
        task="repair CI",
        invocation_mode="normal",
        explicit_skill_name=None,
        expected_skills=("fix-ci",),
        tags=("bundled-pack",),
        notes="",
        status="passed",
        passed=True,
        skip_reason=None,
        agent_exit_code=0,
        verification_command=None,
        verification_exit_code=None,
        verification_output_preview=None,
        skills_advertised_present=True,
        repo_conventions_present=True,
        matched_skill_context_attached=False,
        matched_skill_names=(),
        explicit_skill_context_used=False,
        skill_read_called=True,
        skill_read_names=("fix-ci", "debug"),
        skill_read_call_count=2,
        manual_skill_bundle_accessed=False,
        manual_skill_bundle_names=(),
        manual_skill_bundle_access_count=0,
        tool_call_count=2,
        session_log_path=None,
        session_artifact_root=None,
        automatic_selection_required=True,
        skill_selection_status="selected",
        skill_selection_call_count=1,
    )

    wrong = SkillsEvalRecord(
        **base,
        skill_selection_selected_names=("debug",),
        successful_skill_read_names=("debug",),
        successful_skill_read_count=1,
        skill_selection_blocked_count=1,
    )
    extra = SkillsEvalRecord(
        **base,
        skill_selection_selected_names=("fix-ci", "debug"),
        successful_skill_read_names=("fix-ci", "debug"),
        successful_skill_read_count=2,
    )
    unread = SkillsEvalRecord(
        **base,
        skill_selection_selected_names=("fix-ci",),
        successful_skill_read_names=(),
        successful_skill_read_count=0,
    )
    extra_loaded = SkillsEvalRecord(
        **base,
        skill_selection_selected_names=("fix-ci",),
        successful_skill_read_names=("fix-ci", "debug"),
        successful_skill_read_count=2,
    )
    exact = SkillsEvalRecord(
        **base,
        skill_selection_selected_names=("fix-ci",),
        successful_skill_read_names=("fix-ci",),
        successful_skill_read_count=1,
    )

    assert wrong.automatic_selection_exact_match() is False
    assert wrong.relevant_skill_used() is False
    assert extra.automatic_selection_exact_match() is False
    assert extra.relevant_skill_used() is False
    assert unread.automatic_selection_exact_match() is True
    assert unread.relevant_skill_used() is False
    assert extra_loaded.automatic_selection_exact_match() is True
    assert extra_loaded.relevant_skill_used() is False
    assert exact.automatic_selection_exact_match() is True
    assert exact.relevant_skill_used() is True

    # Current on-demand runs are scored by successful loading, without inventing
    # a missing preliminary selection. Failed and extra loads still do not count.
    on_demand = replace(
        exact,
        skill_selection_call_count=0,
        skill_selection_status=None,
        skill_selection_selected_names=(),
    )
    assert on_demand.automatic_selection_exact_match() is None
    assert on_demand.selector_available() is None
    assert on_demand.relevant_skill_used() is True
    assert not replace(on_demand, successful_skill_read_names=()).relevant_skill_used()
    assert not replace(
        on_demand, successful_skill_read_names=("fix-ci", "debug")
    ).relevant_skill_used()
    summary = evals_module._skill_selection_summary([on_demand, exact, wrong])
    assert summary["automatic_selection_runs"] == 2
    assert summary["automatic_selection_exact_match_count"] == 1
    assert summary["selector_unavailable_run_count"] == 0
    unavailable = replace(exact, skill_selection_status="unavailable")
    mixed = evals_module._skill_selection_summary([on_demand, unavailable])
    assert mixed["automatic_selection_runs"] == 1
    assert mixed["selector_unavailable_run_count"] == 1


def test_automatic_negative_scoring_requires_none_and_ignores_blocked_read_attempt() -> None:
    common = dict(
        case_id="negative",
        mode="combined_auto",
        workspace=Path("/tmp/negative"),
        task="explain a concept",
        invocation_mode="normal",
        explicit_skill_name=None,
        expected_skills=(),
        tags=("bundled-pack", "negative-control"),
        notes="",
        status="passed",
        passed=True,
        skip_reason=None,
        agent_exit_code=0,
        verification_command=None,
        verification_exit_code=0,
        verification_output_preview=None,
        skills_advertised_present=True,
        repo_conventions_present=True,
        matched_skill_context_attached=False,
        matched_skill_names=(),
        explicit_skill_context_used=False,
        skill_read_called=True,
        skill_read_names=("debug",),
        skill_read_call_count=1,
        successful_skill_read_names=(),
        successful_skill_read_count=0,
        manual_skill_bundle_accessed=False,
        manual_skill_bundle_names=(),
        manual_skill_bundle_access_count=0,
        tool_call_count=1,
        session_log_path=None,
        session_artifact_root=None,
        automatic_selection_required=True,
        skill_selection_call_count=1,
        skill_selection_blocked_count=1,
    )
    no_match = SkillsEvalRecord(
        **common,
        skill_selection_status="no_match",
        skill_selection_selected_names=(),
    )
    wrong = SkillsEvalRecord(
        **common,
        skill_selection_status="selected",
        skill_selection_selected_names=("debug",),
    )

    assert no_match.automatic_selection_exact_match() is True
    assert no_match.any_skill_activity() is False
    assert wrong.automatic_selection_exact_match() is False
    assert wrong.any_skill_activity() is True

    on_demand = replace(no_match, skill_selection_call_count=0, skill_selection_status=None)
    assert on_demand.automatic_selection_exact_match() is None
    assert not on_demand.any_skill_activity()
    assert replace(on_demand, successful_skill_read_names=("debug",)).any_skill_activity()


def test_extract_skills_eval_metrics_tracks_skill_lifecycle_cli_usage() -> None:
    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "shell_run",
                "arguments": {"cmd": "alysis skill init pytest-debug"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "verify_run",
                "arguments": {
                    "commands": [
                        "alysis skill validate ./.alysis_skills/pytest-debug",
                        "pytest -q",
                    ]
                },
            },
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["skill_lifecycle_cli_used"] is True
    assert metrics["skill_lifecycle_cli_commands"] == ("init", "validate")
    assert metrics["skill_lifecycle_cli_call_count"] == 2
    assert metrics["tool_call_count"] == 2


def test_extract_skills_eval_metrics_tracks_direct_manual_skill_access(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".alysis_skills" / "pytest").mkdir(parents=True)
    (workspace / ".agents" / "skills" / "lint").mkdir(parents=True)
    (workspace / ".claude" / "skills" / "docs").mkdir(parents=True)
    (workspace / ".github" / "skills" / "review").mkdir(parents=True)

    events = [
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_read",
                "arguments": {"path": ".alysis_skills/pytest/SKILL.md"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_read_lines",
                "arguments": {
                    "path": str(workspace / ".agents" / "skills" / "lint" / "SKILL.md"),
                    "start_line": 1,
                    "end_line": 20,
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_list",
                "arguments": {"root_path": ".claude/skills/docs"},
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "search_rg",
                "arguments": {"pattern": "pytest", "root_path": ".github/skills/review"},
            },
        },
        {
            "type": "tool_call",
            "payload": {"name": "fs_read", "arguments": {"path": "README.md"}},
        },
        {
            "type": "tool_call",
            "payload": {"name": "search_rg", "arguments": {"pattern": "pytest", "root_path": "."}},
        },
    ]

    metrics = extract_skills_eval_metrics(events, workspace_root=workspace)

    assert metrics["manual_skill_bundle_accessed"] is True
    assert metrics["manual_skill_bundle_names"] == ("pytest", "lint", "docs", "review")
    assert metrics["manual_skill_bundle_access_count"] == 4
    assert metrics["tool_call_count"] == 6


def test_extract_skills_eval_metrics_tracks_launch_runtime_events() -> None:
    events = [
        {
            "type": "route_decision",
            "payload": {
                "route": "repo",
                "execution_posture_source": "fallback",
                "router_execution_posture_source": "router",
            },
        },
        {
            "type": "interactive_completion_gate_failed",
            "payload": {
                "stage": "verification_incomplete",
                "state": {"verification_attempt_count": 1},
            },
        },
        {
            "type": "interactive_completion_gate_incomplete_after_retries",
            "payload": {
                "stage": "verification_not_attempted",
                "state": {"verification_attempt_count": 2},
            },
        },
        {
            "type": "forced_final_summary_requested",
            "payload": {"reason": "step_budget"},
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["completion_gate_failure_count"] == 1
    assert metrics["completion_gate_incomplete_after_retries_count"] == 1
    assert metrics["forced_final_summary_count"] == 1
    assert metrics["verification_credit_miss_count"] == 2
    assert "execution_posture_fallback_count" not in metrics


def test_extract_skills_eval_metrics_counts_one_shot_launch_runtime_events() -> None:
    events = [
        {
            "type": "one_shot_completion_gate_failed",
            "payload": {
                "stage": "verification_not_attempted",
                "state": {"verification_attempt_count": 1},
            },
        },
        {
            "type": "one_shot_completion_gate_incomplete_after_retries",
            "payload": {
                "stage": "verification_incomplete",
                "state": {"verification_attempt_count": 3},
            },
        },
    ]

    metrics = extract_skills_eval_metrics(events)

    assert metrics["completion_gate_failure_count"] == 1
    assert metrics["completion_gate_incomplete_after_retries_count"] == 1
    assert metrics["verification_credit_miss_count"] == 2


def test_extract_skills_eval_metrics_counts_execution_evidence_finalization_block() -> None:
    metrics = extract_skills_eval_metrics(
        [
            {
                "type": "execution_evidence_finalization_blocked",
                "payload": {"stage": "execution_evidence"},
            }
        ]
    )

    assert metrics["completion_gate_failure_count"] == 1


def test_bundled_pack_synthetic_events_score_hit_miss_and_false_fire() -> None:
    def _record(
        *,
        case_id: str,
        expected: tuple[str, ...],
        events: list[dict[str, object]],
    ) -> SkillsEvalRecord:
        metrics = extract_skills_eval_metrics(events)
        return SkillsEvalRecord(
            case_id=case_id,
            mode="combined_auto",
            workspace=Path("/tmp/bundled-pack"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=expected,
            tags=(),
            notes="",
            status="passed",
            passed=True,
            skip_reason=None,
            agent_exit_code=0,
            verification_command=None,
            verification_exit_code=None,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=False,
            matched_skill_context_attached=bool(metrics["matched_skill_context_attached"]),
            matched_skill_names=tuple(metrics["matched_skill_names"]),
            explicit_skill_context_used=False,
            skill_read_called=bool(metrics["skill_read_called"]),
            skill_read_names=tuple(metrics["skill_read_names"]),
            skill_read_call_count=int(metrics["skill_read_call_count"]),
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=int(metrics["tool_call_count"]),
            session_log_path=None,
            session_artifact_root=None,
        )

    hit = _record(
        case_id="hit",
        expected=("code-review",),
        events=[
            {
                "type": "skill_matches",
                "payload": {"matches": [{"name": "code-review"}]},
            },
            {
                "type": "tool_call",
                "payload": {
                    "name": "skill_read",
                    "arguments": {"name": "code-review"},
                },
            },
        ],
    )
    miss = _record(
        case_id="miss",
        expected=("fix-ci",),
        events=[
            {
                "type": "tool_call",
                "payload": {"name": "fs_read", "arguments": {"path": "ci-output.txt"}},
            }
        ],
    )
    false_fire = _record(
        case_id="false-fire",
        expected=(),
        events=[
            {
                "type": "skill_matches",
                "payload": {"matches": [{"name": "debug"}]},
            }
        ],
    )

    assert hit.relevant_skill_used() is True
    assert miss.relevant_skill_used() is False
    assert miss.any_skill_activity() is False
    assert false_fire.relevant_skill_used() is False
    assert false_fire.any_skill_activity() is True
    combined = aggregate_skills_eval_records([hit, miss, false_fire])["modes"]["combined_auto"]
    assert combined["false_negative_rate"] == 0.5
    assert combined["false_positive_rate"] == 1.0


def test_aggregate_skills_eval_records_computes_mode_rates() -> None:
    records = [
        SkillsEvalRecord(
            case_id="a",
            mode="combined_auto",
            workspace=Path("/tmp/a"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("pytest",),
            tags=("bundled-pack",),
            notes="",
            status="passed",
            passed=True,
            skip_reason=None,
            agent_exit_code=0,
            verification_command=None,
            verification_exit_code=None,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=True,
            manual_skill_bundle_names=("pytest",),
            manual_skill_bundle_access_count=1,
            tool_call_count=0,
            completion_gate_failure_count=1,
            completion_gate_incomplete_after_retries_count=0,
            forced_final_summary_count=0,
            verification_credit_miss_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="b",
            mode="combined_auto",
            workspace=Path("/tmp/b"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=(),
            tags=(),
            notes="",
            status="failed",
            passed=False,
            skip_reason=None,
            agent_exit_code=1,
            verification_command=None,
            verification_exit_code=None,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=True,
            manual_skill_bundle_names=("lint",),
            manual_skill_bundle_access_count=1,
            tool_call_count=0,
            completion_gate_failure_count=0,
            completion_gate_incomplete_after_retries_count=1,
            forced_final_summary_count=1,
            verification_credit_miss_count=0,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="c",
            mode="baseline",
            workspace=Path("/tmp/c"),
            task="task",
            invocation_mode="explicit_skill",
            explicit_skill_name="pytest",
            expected_skills=("pytest",),
            tags=(),
            notes="",
            status="skipped",
            passed=None,
            skip_reason="explicit skill invocation requires a skills-enabled mode",
            agent_exit_code=None,
            verification_command=None,
            verification_exit_code=None,
            verification_output_preview=None,
            skills_advertised_present=False,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=0,
            session_log_path=None,
            session_artifact_root=None,
        ),
    ]

    summary = aggregate_skills_eval_records(records)
    combined_auto = summary["modes"]["combined_auto"]
    baseline = summary["modes"]["baseline"]

    assert combined_auto["executed_runs"] == 2
    assert combined_auto["pass_rate"] == 0.5
    assert combined_auto["skill_trigger_rate"] == 1.0
    assert combined_auto["skill_lifecycle_cli_rate"] == 0.0
    assert combined_auto["manual_skill_access_rate"] == 1.0
    assert combined_auto["negative_control_runs"] == 1
    assert combined_auto["false_positive_count"] == 1
    assert combined_auto["false_positive_rate"] == 1.0
    assert combined_auto["completion_gate_failure_rate"] == 0.5
    assert combined_auto["completion_gate_incomplete_after_retries_rate"] == 0.5
    assert combined_auto["forced_final_summary_rate"] == 0.5
    assert combined_auto["verification_credit_miss_rate"] == 0.5
    assert combined_auto["bundled_normal_skill_activation"] == {
        "pytest": {"runs": 1, "successful_runs": 1}
    }
    assert combined_auto["bundled_expected_skills"] == ["pytest"]
    assert baseline["skipped_runs"] == 1
    assert summary["relevant_skill_usage_rate"] == 1.0
    assert summary["explicit_invocation_success_rate"] is None
    assert summary["completion_gate_failure_rate"] == 0.5
    assert summary["forced_final_summary_rate"] == 0.5
    assert summary["per_skill"]["pytest"]["runs"] == 1
    assert summary["per_skill"]["pytest"]["triggered"] == 1


def test_launch_candidate_metrics_ignore_control_mode_denominator_pollution() -> None:
    records = [
        SkillsEvalRecord(
            case_id="baseline_expected",
            mode="baseline",
            workspace=Path("/tmp/baseline"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("pytest-debug",),
            tags=(),
            notes="",
            status="failed",
            passed=False,
            skip_reason=None,
            agent_exit_code=1,
            verification_command=None,
            verification_exit_code=1,
            verification_output_preview=None,
            skills_advertised_present=False,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="conventions_expected",
            mode="conventions_only",
            workspace=Path("/tmp/conventions"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("architecture-review",),
            tags=(),
            notes="",
            status="failed",
            passed=False,
            skip_reason=None,
            agent_exit_code=1,
            verification_command=None,
            verification_exit_code=1,
            verification_output_preview=None,
            skills_advertised_present=False,
            repo_conventions_present=True,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="manual_explicit",
            mode="skills_manual_only",
            workspace=Path("/tmp/manual"),
            task="task",
            invocation_mode="explicit_skill",
            explicit_skill_name="pytest-debug",
            expected_skills=("pytest-debug",),
            tags=(),
            notes="",
            status="passed",
            passed=True,
            skip_reason=None,
            agent_exit_code=0,
            verification_command=None,
            verification_exit_code=0,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=False,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=True,
            skill_read_called=False,
            skill_read_names=(),
            skill_read_call_count=0,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=1,
            session_log_path=None,
            session_artifact_root=None,
        ),
        SkillsEvalRecord(
            case_id="combined_manual_expected",
            mode="combined_manual",
            workspace=Path("/tmp/combined"),
            task="task",
            invocation_mode="normal",
            explicit_skill_name=None,
            expected_skills=("verification-playbook",),
            tags=(),
            notes="",
            status="passed",
            passed=True,
            skip_reason=None,
            agent_exit_code=0,
            verification_command=None,
            verification_exit_code=0,
            verification_output_preview=None,
            skills_advertised_present=True,
            repo_conventions_present=True,
            matched_skill_context_attached=False,
            matched_skill_names=(),
            explicit_skill_context_used=False,
            skill_read_called=True,
            skill_read_names=("verification-playbook",),
            skill_read_call_count=1,
            manual_skill_bundle_accessed=False,
            manual_skill_bundle_names=(),
            manual_skill_bundle_access_count=0,
            tool_call_count=2,
            session_log_path=None,
            session_artifact_root=None,
        ),
    ]

    summary = aggregate_skills_eval_records(records)
    summary["false_positive_rate"] = 0.0
    launch = summarize_skills_launch_candidate_metrics(
        summary,
        config_snapshot={"skills_auto_invoke": False},
    )
    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": False},
    )

    assert summary["relevant_skill_usage_rate_all_modes"] == 0.5
    assert summary["relevant_skill_usage_rate_skill_enabled"] == 1.0
    assert summary["pass_rate_all_modes"] == 0.5
    assert summary["pass_rate_skill_enabled"] == 1.0
    assert launch["launch_mode_names"] == ["skills_manual_only", "combined_manual"]
    assert launch["relevant_skill_usage_rate_launch_modes"] == 1.0
    assert launch["pass_rate_launch_modes"] == 1.0
    assert gates["production_ready"] is True


def test_summarize_skills_launch_candidate_metrics_uses_auto_modes_for_default_true() -> None:
    summary = {
        "modes": {
            "skills_manual_only": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 0,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 0,
                "explicit_skill_runs": 0,
                "explicit_skill_success_count": 0,
            },
            "combined_manual": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 0,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 0,
                "explicit_skill_runs": 0,
                "explicit_skill_success_count": 0,
            },
            "skills_auto_only": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 2,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 2,
                "negative_control_runs": 1,
                "false_positive_count": 0,
                "explicit_skill_runs": 1,
                "explicit_skill_success_count": 1,
            },
            "combined_auto": {
                "skills_enabled": True,
                "executed_runs": 2,
                "passed_runs": 2,
                "expected_skill_runs": 2,
                "relevant_skill_usage_count": 2,
                "negative_control_runs": 1,
                "false_positive_count": 0,
                "explicit_skill_runs": 1,
                "explicit_skill_success_count": 1,
            },
        }
    }

    launch = summarize_skills_launch_candidate_metrics(
        summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert launch["launch_mode_basis"] == "skills_auto_invoke=true"
    assert launch["launch_mode_names"] == ["skills_auto_only", "combined_auto"]
    assert launch["pass_rate_launch_modes"] == 1.0
    assert launch["relevant_skill_usage_rate_launch_modes"] == 1.0
    assert launch["false_positive_rate_launch_modes"] == 0.0
    assert launch["explicit_invocation_success_rate_launch_modes"] == 1.0


def test_evaluate_skills_launch_readiness_fails_when_runtime_gates_are_exceeded() -> None:
    summary = {
        "pass_rate": 0.10,
        "completion_gate_failure_rate": 0.25,
        "completion_gate_incomplete_after_retries_rate": 0.10,
        "forced_final_summary_rate": 0.20,
        "verification_credit_miss_rate": 0.30,
        "false_positive_rate": 0.50,
        "relevant_skill_usage_rate": 0.70,
        "explicit_invocation_success_rate": 0.50,
    }

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": False},
    )

    assert gates["production_ready"] is False
    assert sorted(gates["failing_gates"]) == [
        "completion_gate_failure_rate",
        "completion_gate_incomplete_after_retries_rate",
        "explicit_skill_success_rate",
        "false_positive_rate",
        "forced_final_summary_rate",
        "pass_rate",
        "relevant_skill_usage_rate",
        "verification_credit_miss_rate",
    ]


def test_evaluate_skills_launch_readiness_allows_default_auto_invoke_enabled() -> None:
    summary = _passing_launch_summary(relevant_skill_usage_count=1)

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert gates["production_ready"] is True
    assert gates["failing_gates"] == []
    assert "skills_auto_invoke_default_disabled" not in gates["gates"]


def test_evaluate_skills_launch_readiness_has_no_selector_gate_for_on_demand_runs() -> None:
    summary = _passing_launch_summary(relevant_skill_usage_count=1)
    summary["modes"]["combined_auto"].update(
        {
            "automatic_selection_runs": 0,
            "automatic_selection_exact_match_count": 0,
            "selector_available_run_count": 0,
            "selector_unavailable_run_count": 0,
        }
    )

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert gates["production_ready"] is True
    assert gates["failing_gates"] == []
    for name in ("automatic_selection_exact_match_rate", "selector_unavailable_runs"):
        assert gates["gates"][name]["status"] == "not_applicable"
        assert gates["gates"][name]["actual"] is None

    # Removing selector-only gates must not remove behavioral false-positive gates.
    summary["modes"]["combined_auto"]["false_positive_count"] = 1
    bad = evaluate_skills_launch_readiness(
        summary=summary, config_snapshot={"skills_auto_invoke": True}
    )
    assert bad["production_ready"] is False
    assert bad["failing_gates"] == ["false_positive_rate"]
    summary["modes"]["combined_auto"]["false_positive_count"] = 0

    manual_summary = {
        **summary,
        "modes": {"combined_manual": dict(summary["modes"]["combined_auto"])},
    }
    manual_gates = evaluate_skills_launch_readiness(
        summary=manual_summary,
        config_snapshot={"skills_auto_invoke": False},
    )

    assert manual_gates["production_ready"] is True
    assert manual_gates["gates"]["automatic_selection_exact_match_rate"]["status"] == "pass"


def test_evaluate_skills_launch_readiness_passes_only_when_thresholds_are_met() -> None:
    summary = {
        "pass_rate": 1.0,
        "completion_gate_failure_rate": 0.0,
        "completion_gate_incomplete_after_retries_rate": 0.0,
        "forced_final_summary_rate": 0.0,
        "verification_credit_miss_rate": 0.0,
        "false_positive_rate": 0.0,
        "relevant_skill_usage_rate": 1.0,
        "explicit_invocation_success_rate": 1.0,
    }

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": False},
    )

    assert gates["production_ready"] is True
    assert gates["failing_gates"] == []


def test_evaluate_skills_launch_readiness_rejects_false_positive_activation() -> None:
    summary = _passing_launch_summary(relevant_skill_usage_count=1)
    summary["modes"]["combined_auto"]["false_positive_count"] = 1

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert gates["production_ready"] is False
    assert gates["failing_gates"] == ["false_positive_rate"]


def test_evaluate_skills_launch_readiness_rejects_inexact_or_unavailable_selector() -> None:
    summary = _passing_launch_summary(relevant_skill_usage_count=1)
    auto = summary["modes"]["combined_auto"]
    auto.update(
        {
            "automatic_selection_runs": 2,
            "automatic_selection_exact_match_count": 1,
            "selector_available_run_count": 1,
            "selector_unavailable_run_count": 1,
        }
    )

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert gates["production_ready"] is False
    assert "automatic_selection_exact_match_rate" in gates["failing_gates"]
    assert "selector_unavailable_runs" in gates["failing_gates"]


def test_evaluate_skills_launch_readiness_requires_normal_activation_per_bundled_skill() -> None:
    summary = _passing_launch_summary(relevant_skill_usage_count=1)
    summary["modes"]["combined_auto"]["bundled_expected_skills"] = ["debug", "commit"]
    summary["modes"]["combined_auto"]["bundled_normal_skill_activation"] = {
        "debug": {"runs": 1, "successful_runs": 1},
    }

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert gates["production_ready"] is False
    assert "bundled_normal_skill_activation" in gates["failing_gates"]
    assert gates["gates"]["bundled_normal_skill_activation"]["missing_skills"] == ["commit"]


def test_evaluate_skills_launch_readiness_scopes_runtime_rates_to_launch_mode() -> None:
    summary = _passing_launch_summary(relevant_skill_usage_count=1)
    summary["modes"]["baseline"] = {
        "skills_enabled": False,
        "executed_runs": 10,
        "passed_runs": 10,
        "failed_runs": 0,
        "completion_gate_failure_run_count": 10,
        "completion_gate_incomplete_after_retries_run_count": 10,
        "forced_final_summary_run_count": 10,
        "verification_credit_miss_run_count": 10,
    }
    summary.update(
        {
            "completion_gate_failure_rate": 0.9091,
            "completion_gate_incomplete_after_retries_rate": 0.9091,
            "forced_final_summary_rate": 0.9091,
            "verification_credit_miss_rate": 0.9091,
        }
    )

    gates = evaluate_skills_launch_readiness(
        summary=summary,
        config_snapshot={"skills_auto_invoke": True},
    )

    assert gates["production_ready"] is True
    for gate_name in (
        "completion_gate_failure_rate",
        "completion_gate_incomplete_after_retries_rate",
        "forced_final_summary_rate",
        "verification_credit_miss_rate",
    ):
        assert gates["gates"][gate_name]["actual"] == 0.0


def test_render_skills_eval_summary_markdown_includes_launch_metrics_and_release_gates() -> None:
    summary = {
        "generated_at": "2026-04-24T00:00:00+00:00",
        "modes": {
            "combined_auto": {
                "executed_runs": 2,
                "skipped_runs": 0,
                "pass_rate": 1.0,
                "skill_trigger_rate": 1.0,
                "skill_read_rate": 0.5,
                "skill_lifecycle_cli_rate": 0.0,
                "manual_skill_access_rate": 0.5,
                "false_negative_rate": 0.0,
                "false_positive_rate": 0.0,
                "explicit_invocation_success_rate": None,
            }
        },
        "completion_gate_failure_count": 1,
        "completion_gate_failure_run_count": 1,
        "completion_gate_failure_rate": 0.5,
        "completion_gate_incomplete_after_retries_count": 0,
        "completion_gate_incomplete_after_retries_run_count": 0,
        "completion_gate_incomplete_after_retries_rate": 0.0,
        "forced_final_summary_count": 1,
        "forced_final_summary_run_count": 1,
        "forced_final_summary_rate": 0.5,
        "verification_credit_miss_count": 1,
        "verification_credit_miss_run_count": 1,
        "verification_credit_miss_rate": 0.5,
        "pass_rate": 0.5,
        "relevant_skill_usage_rate": 1.0,
        "explicit_invocation_success_rate": 1.0,
        "release_gates": {
            "gates": {
                "completion_gate_failure_rate": {
                    "status": "fail",
                    "actual": 0.5,
                    "max_allowed": 0.05,
                },
            }
        },
    }

    rendered = render_skills_eval_summary_markdown(summary=summary)

    assert "Launch Runtime Metrics" in rendered
    assert "launch-candidate pass rate" in rendered
    assert "relevant skill usage rate (launch-candidate modes)" in rendered
    assert "completion-gate failure rate" in rendered
    assert "Release Gates" in rendered
    assert "`completion_gate_failure_rate`: fail" in rendered


def test_skills_eval_record_manual_access_counts_as_skill_activity() -> None:
    record = SkillsEvalRecord(
        case_id="manual",
        mode="combined_manual",
        workspace=Path("/tmp/manual"),
        task="task",
        invocation_mode="normal",
        explicit_skill_name=None,
        expected_skills=("pytest",),
        tags=(),
        notes="",
        status="passed",
        passed=True,
        skip_reason=None,
        agent_exit_code=0,
        verification_command=None,
        verification_exit_code=None,
        verification_output_preview=None,
        skills_advertised_present=True,
        repo_conventions_present=False,
        matched_skill_context_attached=False,
        matched_skill_names=(),
        explicit_skill_context_used=False,
        skill_read_called=False,
        skill_read_names=(),
        skill_read_call_count=0,
        manual_skill_bundle_accessed=True,
        manual_skill_bundle_names=("pytest",),
        manual_skill_bundle_access_count=1,
        tool_call_count=1,
        session_log_path=None,
        session_artifact_root=None,
    )

    assert record.observed_skill_names() == ("pytest",)
    assert record.relevant_skill_used() is True
    assert record.any_skill_activity() is True


def test_run_skills_eval_suite_writes_results_and_skips_explicit_cases_without_skills(
    tmp_path: Path,
) -> None:
    manifest = Path("tests/fixtures/skills_eval/sample_cases.json").resolve()
    cases = load_skills_eval_cases(manifest)
    selected_cases = tuple(
        case
        for case in cases
        if case.id in {"skills_parser_pytest_normal", "skills_parser_pytest_explicit"}
    )
    selected_modes = resolve_skills_eval_modes(["baseline", "skills_manual_only", "combined_auto"])
    executor = _FakeExecutor()

    artifacts = run_skills_eval_suite(
        cases=selected_cases,
        modes=selected_modes,
        output_dir=tmp_path / "out",
        executor=executor,
        verification_runner=_fake_verification_runner,
        manifest_path=manifest,
        max_steps=7,
    )

    assert artifacts.results_path.exists()
    assert artifacts.summary_json_path.exists()
    assert artifacts.summary_md_path.exists()
    results = [
        json.loads(line)
        for line in artifacts.results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(results) == 6
    skipped = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_explicit" and item["mode"] == "baseline"
    ]
    assert skipped and skipped[0]["status"] == "skipped"
    assert skipped[0]["skip_reason"] == "explicit skill invocation requires a skills-enabled mode"

    manual_normal = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_normal" and item["mode"] == "skills_manual_only"
    ][0]
    auto_normal = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_normal" and item["mode"] == "combined_auto"
    ][0]
    explicit_auto = [
        item
        for item in results
        if item["case_id"] == "skills_parser_pytest_explicit" and item["mode"] == "combined_auto"
    ][0]

    assert manual_normal["skill_read_called"] is True
    assert "manual_skill_bundle_accessed" in manual_normal
    assert "manual_skill_bundle_names" in manual_normal
    assert "manual_skill_bundle_access_count" in manual_normal
    assert auto_normal["matched_skill_context_attached"] is False
    assert auto_normal["skill_read_called"] is True
    assert explicit_auto["explicit_skill_context_used"] is True
    summary_markdown = artifacts.summary_md_path.read_text(encoding="utf-8")
    assert "skills_manual_only" in summary_markdown
    assert "lifecycle CLI rate" in summary_markdown
    assert "manual access rate" in summary_markdown

    assert executor.requests
    baseline_requests = [
        request for request in executor.requests if request.mode.name == "baseline"
    ]
    manual_requests = [
        request for request in executor.requests if request.mode.name == "skills_manual_only"
    ]
    assert baseline_requests
    assert not (baseline_requests[0].workspace / "AGENTS.md").exists()
    assert manual_requests and not (manual_requests[0].workspace / "AGENTS.md").exists()


def test_skills_eval_suite_passes_only_the_remaining_run_deadline_to_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clock_values = iter((100.0, 102.5))
    observed_timeouts: list[float | None] = []

    def _verification_runner(
        *,
        workspace: Path,
        command: str,
        timeout_seconds: float | None,
    ) -> SkillsEvalVerificationResult:
        _ = workspace, command
        observed_timeouts.append(timeout_seconds)
        return SkillsEvalVerificationResult(exit_code=0, output_preview="ok")

    monkeypatch.setattr(evals_module, "monotonic", lambda: next(clock_values))
    artifacts = run_skills_eval_suite(
        cases=(
            SkillsEvalCase(
                id="deadline",
                workspace=workspace,
                task="Inspect the fixture.",
                verification_command="python -c 'assert True'",
            ),
        ),
        modes=(
            SkillsEvalMode(
                name="combined_auto",
                conventions_enabled=True,
                skills_enabled=True,
                skills_auto_invoke=True,
            ),
        ),
        output_dir=tmp_path / "out",
        executor=_FakeExecutor(),
        verification_runner=_verification_runner,
        deadline_seconds=5.0,
    )

    assert artifacts.records[0].status == "passed"
    assert observed_timeouts == [2.5]


def test_skills_eval_suite_fails_without_starting_verification_after_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    clock_values = iter((100.0, 105.0))

    def _unexpected_verification_runner(**_kwargs: Any) -> SkillsEvalVerificationResult:
        raise AssertionError("verification must not start after the per-run deadline")

    monkeypatch.setattr(evals_module, "monotonic", lambda: next(clock_values))
    artifacts = run_skills_eval_suite(
        cases=(
            SkillsEvalCase(
                id="deadline",
                workspace=workspace,
                task="Inspect the fixture.",
                verification_command="python -c 'assert True'",
            ),
        ),
        modes=(
            SkillsEvalMode(
                name="combined_auto",
                conventions_enabled=True,
                skills_enabled=True,
                skills_auto_invoke=True,
            ),
        ),
        output_dir=tmp_path / "out",
        executor=_FakeExecutor(),
        verification_runner=_unexpected_verification_runner,
        deadline_seconds=5.0,
    )

    record = artifacts.records[0]
    assert record.status == "failed"
    assert record.verification_exit_code == 124
    assert "deadline" in str(record.verification_output_preview).casefold()


def test_one_shot_skills_eval_executor_uses_explicit_skill_context(
    monkeypatch, tmp_path: Path
) -> None:
    skill = SkillBundle(
        name="pytest",
        description="Debug pytest failures.",
        instructions='Read "$ARGUMENTS" starting with $1 and $2.',
        bundle_name="pytest",
        bundle_path=tmp_path / ".alysis_skills" / "pytest",
        entry_path=tmp_path / ".alysis_skills" / "pytest" / "SKILL.md",
        source_scope="project",
        source_kind="native",
        source_family=".alysis_skills",
        source_path=tmp_path / ".alysis_skills" / "pytest",
        trust_level="untrusted",
    )
    fake_session = _ExecutorFakeSession(tmp_path, skill)

    def _fake_create_session(**kwargs: Any) -> _ExecutorFakeSession:
        _ = kwargs
        return fake_session

    monkeypatch.setattr(
        "alysis_code.skills.eval_runner.create_session",
        _fake_create_session,
    )
    executor = OneShotSkillsEvalExecutor(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        api_key_override="key",
    )
    request = SkillsEvalExecutionRequest(
        case=SkillsEvalCase(
            id="explicit",
            workspace=tmp_path,
            task="Investigate the pytest failure.",
            invocation_mode="explicit_skill",
            explicit_skill_name="pytest",
        ),
        mode=SkillsEvalMode(
            name="combined_manual",
            conventions_enabled=True,
            skills_enabled=True,
            skills_auto_invoke=False,
        ),
        workspace=tmp_path,
        output_dir=tmp_path / "out",
        sessions_dir=tmp_path / "sessions",
        session_id="skills_eval_explicit",
        max_steps=5,
    )

    result = executor.execute(request)

    assert result.agent_exit_code == 0
    assert result.skills_advertised_present is True
    assert result.repo_conventions_present is True
    assert result.explicit_skill_context_used is True
    assert result.skill_read_called is True
    assert result.skill_read_names == ("pytest",)
    assert result.skill_lifecycle_cli_used is False
    assert result.skill_lifecycle_cli_commands == ()
    assert result.skill_lifecycle_cli_call_count == 0
    assert fake_session.run_turn_calls
    explicit_message = fake_session.run_turn_calls[0]["ephemeral_user_messages"][0]
    assert "<explicit_skill_context>" in explicit_message
    assert (
        "turn_requirement: Apply this selected skill before taking other actions on the next user task."
        in explicit_message
    )
    assert (
        "task_binding: Treat this wrapper and the next user message as one bound instruction set."
        in explicit_message
    )
    assert len(explicit_message) <= EXPLICIT_SKILL_CONTEXT_TOTAL_MAX_CHARS
    assert '- $ARGUMENTS = "Investigate the pytest failure."' in explicit_message
    assert '- $1 = "Investigate"' in explicit_message
    assert '- $2 = "the"' in explicit_message
    assert (
        'Read "Investigate the pytest failure." starting with Investigate and the.'
        in explicit_message
    )
    assert fake_session.closed is True


def test_run_skills_eval_auth_preflight_classifies_auth_failures(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _fake_execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        _ = self, request
        return SkillsEvalExecutionResult(
            agent_exit_code=1,
            error="401 invalid_api_key",
        )

    monkeypatch.setattr(
        OneShotSkillsEvalExecutor,
        "execute",
        _fake_execute,
    )

    result = run_skills_eval_auth_preflight(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        workspace=workspace,
        output_dir=tmp_path / "out",
        api_key_override="test-key",
    )

    assert result.ok is False
    assert result.classification == "auth"
    assert "invalid_api_key" in result.message


def test_run_skills_eval_auth_preflight_succeeds_on_clean_execution(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_log_path = tmp_path / "out" / "sessions" / "preflight.jsonl"

    def _fake_execute(self, request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
        _ = request
        assert self.session_mode == "readonly"
        assert self.one_shot_execution is False
        assert self.verification_enabled is False
        assert self.deadline_seconds == 12.5
        assert self.deadline_source == "test"
        session_log_path.parent.mkdir(parents=True, exist_ok=True)
        session_log_path.write_text("", encoding="utf-8")
        return SkillsEvalExecutionResult(
            agent_exit_code=0,
            session_log_path=session_log_path,
            session_artifact_root=session_log_path.parent / "artifacts",
        )

    monkeypatch.setattr(
        OneShotSkillsEvalExecutor,
        "execute",
        _fake_execute,
    )

    result = run_skills_eval_auth_preflight(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        workspace=workspace,
        output_dir=tmp_path / "out",
        api_key_override="test-key",
        deadline_seconds=12.5,
        deadline_source="test",
    )

    assert result.ok is True
    assert result.classification == "ok"
    assert result.message == "Auth preflight passed."
    assert result.session_log_path == session_log_path


def test_one_shot_eval_executor_passes_case_verification_as_authoritative(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, Any] = {}
    session_log_path = tmp_path / "session.jsonl"

    class _FakeStore:
        enabled = True
        path = session_log_path
        session_artifact_root = tmp_path / "artifacts"

    class _FakeSession:
        messages: list[dict[str, object]] = []
        skill_registry: dict[str, object] = {}
        store = _FakeStore()

        def run_turn(self, *_args, **_kwargs) -> int:
            session_log_path.write_text("", encoding="utf-8")
            return 0

        def close(self) -> None:
            return None

    def _fake_create_session(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(eval_runner_module, "create_session", _fake_create_session)
    monkeypatch.setattr(eval_runner_module, "read_session_events", lambda _path: [])
    executor = OneShotSkillsEvalExecutor(cfg=AppConfig(model="test-model"), api_key_override="key")
    request = SkillsEvalExecutionRequest(
        case=SkillsEvalCase(
            id="case",
            workspace=tmp_path,
            task="Write a file and verify it.",
            verification_command="test -f README.md",
        ),
        mode=SkillsEvalMode(
            name="skills_manual_only",
            conventions_enabled=False,
            skills_enabled=True,
            skills_auto_invoke=False,
        ),
        workspace=tmp_path,
        output_dir=tmp_path,
        sessions_dir=tmp_path,
        session_id="session",
        max_steps=3,
    )

    result = executor.execute(request)

    assert result.agent_exit_code == 0
    assert captured["mode"] == "auto"
    assert captured["one_shot_execution"] is True
    assert captured["verification_enabled"] is True
    assert captured["authoritative_verification_commands"] == ["test -f README.md"]

    captured.clear()
    readonly_request = replace(
        request,
        case=replace(
            request.case,
            task="Explain the documented concept without changing or running code.",
            session_mode="readonly",
        ),
    )
    readonly_result = executor.execute(readonly_request)

    assert readonly_result.agent_exit_code == 0
    assert captured["mode"] == "readonly"
    assert captured["one_shot_execution"] is False
    assert captured["verification_enabled"] is False
    assert captured["authoritative_verification_commands"] is None


def test_one_shot_eval_executor_creates_a_fresh_deadline_per_run(
    tmp_path: Path, monkeypatch
) -> None:
    deadlines: list[Any] = []
    session_log_path = tmp_path / "session.jsonl"

    class _FakeStore:
        enabled = True
        path = session_log_path
        session_artifact_root = tmp_path / "artifacts"

    class _FakeSession:
        messages: list[dict[str, object]] = []
        skill_registry: dict[str, object] = {}
        store = _FakeStore()

        def run_turn(self, *_args, **_kwargs) -> int:
            session_log_path.write_text("", encoding="utf-8")
            return 0

        def close(self) -> None:
            return None

    def _fake_create_session(**kwargs: Any) -> _FakeSession:
        deadlines.append(kwargs["execution_deadline"])
        return _FakeSession()

    monkeypatch.setattr(eval_runner_module, "create_session", _fake_create_session)
    monkeypatch.setattr(eval_runner_module, "read_session_events", lambda _path: [])
    executor = OneShotSkillsEvalExecutor(
        cfg=AppConfig(model="test-model"),
        api_key_override="key",
        deadline_seconds=12.5,
        deadline_source="test",
    )
    request = SkillsEvalExecutionRequest(
        case=SkillsEvalCase(id="case", workspace=tmp_path, task="Read the fixture."),
        mode=SkillsEvalMode(
            name="combined_auto",
            conventions_enabled=True,
            skills_enabled=True,
            skills_auto_invoke=True,
        ),
        workspace=tmp_path,
        output_dir=tmp_path,
        sessions_dir=tmp_path,
        session_id="session",
        max_steps=3,
    )

    first = executor.execute(request)
    second = executor.execute(request)

    assert first.agent_exit_code == 0
    assert second.agent_exit_code == 0
    assert len(deadlines) == 2
    assert deadlines[0] is not deadlines[1]
    for deadline in deadlines:
        assert deadline.configured_duration_seconds == 12.5
        assert deadline.source == "test"
        assert deadline.enabled is True


def test_one_shot_eval_executor_reports_clean_runtime_deadline_stops_as_eval_errors(
    tmp_path: Path, monkeypatch
) -> None:
    session_log_path = tmp_path / "session.jsonl"

    class _FakeStore:
        enabled = True
        path = session_log_path
        session_artifact_root = tmp_path / "artifacts"

    class _FakeSession:
        messages: list[dict[str, object]] = []
        skill_registry: dict[str, object] = {}
        store = _FakeStore()

        def run_turn(self, *_args, **_kwargs) -> int:
            session_log_path.write_text("", encoding="utf-8")
            return 0

        def close(self) -> None:
            return None

    monkeypatch.setattr(eval_runner_module, "create_session", lambda **_kwargs: _FakeSession())
    monkeypatch.setattr(
        eval_runner_module,
        "read_session_events",
        lambda _path: [{"type": "deadline_exhausted", "payload": {}}],
    )
    executor = OneShotSkillsEvalExecutor(
        cfg=AppConfig(model="test-model"),
        api_key_override="key",
        deadline_seconds=12.5,
    )
    request = SkillsEvalExecutionRequest(
        case=SkillsEvalCase(id="case", workspace=tmp_path, task="Read the fixture."),
        mode=SkillsEvalMode(
            name="combined_auto",
            conventions_enabled=True,
            skills_enabled=True,
            skills_auto_invoke=True,
        ),
        workspace=tmp_path,
        output_dir=tmp_path,
        sessions_dir=tmp_path,
        session_id="session",
        max_steps=3,
    )

    result = executor.execute(request)

    assert result.agent_exit_code == 0
    assert result.error == "run deadline exhausted"


def test_skills_eval_suite_fails_a_clean_exit_with_an_execution_error(tmp_path: Path) -> None:
    class _DeadlineExecutor:
        def execute(self, _request: SkillsEvalExecutionRequest) -> SkillsEvalExecutionResult:
            return SkillsEvalExecutionResult(
                agent_exit_code=0,
                error="run deadline exhausted",
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = run_skills_eval_suite(
        cases=(SkillsEvalCase(id="deadline", workspace=workspace, task="Inspect the fixture."),),
        modes=(
            SkillsEvalMode(
                name="combined_auto",
                conventions_enabled=True,
                skills_enabled=True,
                skills_auto_invoke=True,
            ),
        ),
        output_dir=tmp_path / "out",
        executor=_DeadlineExecutor(),
    )

    assert len(artifacts.records) == 1
    assert artifacts.records[0].status == "failed"
    assert artifacts.records[0].passed is False
    assert artifacts.records[0].error == "run deadline exhausted"


def test_one_shot_eval_executor_rejects_more_permissive_case_session_mode(
    tmp_path: Path, monkeypatch
) -> None:
    create_calls = 0

    def _fake_create_session(**_kwargs):
        nonlocal create_calls
        create_calls += 1
        raise AssertionError("create_session must not receive an unsafe case override")

    monkeypatch.setattr(eval_runner_module, "create_session", _fake_create_session)
    executor = OneShotSkillsEvalExecutor(
        cfg=AppConfig(model="test-model"),
        api_key_override="key",
        session_mode="auto",
    )
    request = SkillsEvalExecutionRequest(
        case=SkillsEvalCase(
            id="case",
            workspace=tmp_path,
            task="Read the fixture.",
            session_mode="fullaccess",  # type: ignore[arg-type]
        ),
        mode=SkillsEvalMode(
            name="skills_manual_only",
            conventions_enabled=False,
            skills_enabled=True,
            skills_auto_invoke=False,
        ),
        workspace=tmp_path,
        output_dir=tmp_path,
        sessions_dir=tmp_path,
        session_id="session",
        max_steps=3,
    )

    result = executor.execute(request)

    assert result.agent_exit_code == 1
    assert result.error == "eval cases may only override session mode to readonly"
    assert create_calls == 0


def test_classify_skills_eval_failure_treats_missing_model_as_provider_misconfiguration() -> None:
    classification = classify_skills_eval_failure(
        "Model is not set. Run: alysis config set model <MODEL>",
        agent_exit_code=1,
    )

    assert classification == "provider"


def _run_stubbed_eval_cli(
    monkeypatch,
    tmp_path: Path,
    *,
    summary: dict[str, object],
    record_status: str = "passed",
    launch_gate: bool = False,
    preflight_ok: bool = True,
    configured_skills_auto_invoke: bool = True,
    extra_args: tuple[str, ...] = (),
) -> tuple[int, dict[str, object]]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    launch_cases = _complete_launch_cases(tmp_path)
    artifacts = SimpleNamespace(
        output_dir=tmp_path / "out",
        results_path=tmp_path / "out" / "results.jsonl",
        summary_json_path=tmp_path / "out" / "summary.json",
        summary_md_path=tmp_path / "out" / "summary.md",
        records=(SimpleNamespace(status=record_status),),
        summary=summary,
    )
    written: dict[str, object] = {}

    def _fake_preflight(**kwargs: Any) -> SkillsEvalAuthPreflightResult:
        written["preflight_call_count"] = int(written.get("preflight_call_count", 0)) + 1
        written["preflight_deadline_seconds"] = kwargs.get("deadline_seconds")
        written["preflight_deadline_source"] = kwargs.get("deadline_source")
        return SkillsEvalAuthPreflightResult(
            ok=preflight_ok,
            classification="ok" if preflight_ok else "auth",
            message="passed" if preflight_ok else "redacted failure",
            agent_exit_code=0 if preflight_ok else 1,
        )

    def _fake_run_skills_eval_suite(**kwargs: Any) -> SimpleNamespace:
        written["suite_call_count"] = int(written.get("suite_call_count", 0)) + 1
        executor = kwargs["executor"]
        written["suite_deadline_seconds"] = executor.deadline_seconds
        written["suite_deadline_source"] = executor.deadline_source
        written["suite_verification_deadline_seconds"] = kwargs.get("deadline_seconds")
        return artifacts

    def _fake_write_skills_eval_artifacts(**kwargs: Any) -> SimpleNamespace:
        written.update(kwargs)
        return SimpleNamespace(**{**artifacts.__dict__, "summary": dict(kwargs["summary"])})

    monkeypatch.setattr(
        eval_runner_module,
        "load_skills_eval_cases",
        lambda _path: launch_cases,
    )
    resolved_modes = (
        (
            SkillsEvalMode(
                name="combined_auto",
                conventions_enabled=True,
                skills_enabled=True,
                skills_auto_invoke=True,
            ),
        )
        if launch_gate
        else ()
    )
    monkeypatch.setattr(
        eval_runner_module,
        "resolve_skills_eval_modes",
        lambda _modes: resolved_modes,
    )
    monkeypatch.setattr(
        eval_runner_module,
        "load_config",
        lambda: AppConfig(
            model="test-model",
            web_search_mode="off",
            skills_auto_invoke=configured_skills_auto_invoke,
        ),
    )
    monkeypatch.setattr(
        eval_runner_module,
        "run_skills_eval_auth_preflight",
        _fake_preflight,
    )
    monkeypatch.setattr(
        eval_runner_module,
        "run_skills_eval_suite",
        _fake_run_skills_eval_suite,
    )
    monkeypatch.setattr(
        eval_runner_module,
        "write_skills_eval_artifacts",
        _fake_write_skills_eval_artifacts,
    )
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)
    argv = ["--manifest", str(manifest), "--output-dir", str(tmp_path / "out")]
    if launch_gate:
        argv.extend(("--launch-gate", "--mode", "combined_auto"))
    argv.extend(extra_args)
    return eval_runner_module.main(argv), written


def _passing_launch_summary(*, relevant_skill_usage_count: int) -> dict[str, object]:
    return {
        "modes": {
            "combined_auto": {
                "skills_enabled": True,
                "executed_runs": 1,
                "passed_runs": 1,
                "failed_runs": 0,
                "expected_skill_runs": 1,
                "relevant_skill_usage_count": relevant_skill_usage_count,
                "negative_control_runs": 1,
                "false_positive_count": 0,
                "explicit_skill_runs": 1,
                "explicit_skill_success_count": 1,
                "completion_gate_failure_run_count": 0,
                "completion_gate_incomplete_after_retries_run_count": 0,
                "forced_final_summary_run_count": 0,
                "verification_credit_miss_run_count": 0,
                "automatic_selection_runs": 1,
                "automatic_selection_exact_match_count": 1,
                "selector_available_run_count": 1,
                "selector_unavailable_run_count": 0,
                "skill_selection_blocked_count": 0,
                "skill_selection_blocked_run_count": 0,
                "skill_selection_unhonored_count": 0,
                "skill_selection_unhonored_run_count": 0,
                "bundled_normal_skill_activation": {
                    "debug": {
                        "runs": 1,
                        "successful_runs": int(relevant_skill_usage_count > 0),
                    }
                },
            }
        },
        "completion_gate_failure_rate": 0.0,
        "completion_gate_incomplete_after_retries_rate": 0.0,
        "forced_final_summary_rate": 0.0,
        "verification_credit_miss_rate": 0.0,
        "false_positive_rate": 0.0,
    }


def _complete_launch_cases(tmp_path: Path) -> tuple[SkillsEvalCase, ...]:
    verification_command = "python -c 'assert True'"
    return (
        SkillsEvalCase(
            id="debug-normal",
            workspace=tmp_path,
            task="Fix the failure.",
            expected_skills=("debug",),
            verification_command=verification_command,
        ),
        SkillsEvalCase(
            id="debug-explicit",
            workspace=tmp_path,
            task="Fix the failure.",
            invocation_mode="explicit_skill",
            explicit_skill_name="debug",
            expected_skills=("debug",),
            verification_command=verification_command,
        ),
        SkillsEvalCase(
            id="debug-negative",
            workspace=tmp_path,
            task="Explain ValueError.",
            verification_command=verification_command,
            tags=("negative-control", "debug-adjacent"),
            session_mode="readonly",
        ),
    )


def _eval_cli_usage_exit(argv: list[str]) -> int:
    try:
        eval_runner_module.main(argv)
    except SystemExit as exc:
        return int(exc.code)
    raise AssertionError("skills eval CLI accepted invalid launch arguments")


def test_eval_runner_rejects_unknown_or_empty_case_selection(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(
        eval_runner_module,
        "load_skills_eval_cases",
        lambda _path: _complete_launch_cases(tmp_path),
    )

    unknown_exit = _eval_cli_usage_exit(["--manifest", str(manifest), "--case", "not-a-case"])
    empty_exit = _eval_cli_usage_exit(["--manifest", str(manifest), "--case", ""])

    assert unknown_exit == 2
    assert empty_exit == 2


def test_eval_runner_rejects_empty_manifest(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(eval_runner_module, "load_skills_eval_cases", lambda _path: ())

    exit_code = _eval_cli_usage_exit(["--manifest", str(manifest)])

    assert exit_code == 2


def test_eval_runner_launch_gate_rejects_case_filtering(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(
        eval_runner_module,
        "load_skills_eval_cases",
        lambda _path: _complete_launch_cases(tmp_path),
    )

    exit_code = _eval_cli_usage_exit(
        [
            "--manifest",
            str(manifest),
            "--launch-gate",
            "--mode",
            "combined_auto",
            "--case",
            "debug-normal",
        ]
    )

    assert exit_code == 2


def test_eval_runner_launch_gate_rejects_incomplete_skill_coverage(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    incomplete_cases = (_complete_launch_cases(tmp_path)[0],)
    monkeypatch.setattr(
        eval_runner_module,
        "load_skills_eval_cases",
        lambda _path: incomplete_cases,
    )

    exit_code = _eval_cli_usage_exit(
        ["--manifest", str(manifest), "--launch-gate", "--mode", "combined_auto"]
    )

    assert exit_code == 2


def test_eval_runner_main_returns_nonzero_when_a_case_fails(monkeypatch, tmp_path: Path) -> None:
    exit_code, _written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary={"modes": {}},
        record_status="failed",
    )

    assert exit_code == 1


def test_eval_runner_main_returns_nonzero_when_all_selected_runs_are_skipped(
    monkeypatch, tmp_path: Path
) -> None:
    exit_code, _written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary={"modes": {}},
        record_status="skipped",
    )

    assert exit_code == 1


def test_eval_runner_launch_gate_requires_exactly_combined_auto_mode_before_execution(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(
        eval_runner_module,
        "load_skills_eval_cases",
        lambda _path: _complete_launch_cases(tmp_path),
    )
    model_setup_calls = 0

    def _unexpected_load_config() -> AppConfig:
        nonlocal model_setup_calls
        model_setup_calls += 1
        raise AssertionError("launch validation must finish before model setup")

    monkeypatch.setattr(eval_runner_module, "load_config", _unexpected_load_config)

    invalid_mode_args = (
        (),
        ("--mode", "combined_manual"),
        ("--mode", "combined_auto", "--mode", "combined_auto"),
        ("--mode", "combined_auto", "--mode", "skills_auto_only"),
    )
    for mode_args in invalid_mode_args:
        exit_code = _eval_cli_usage_exit(["--manifest", str(manifest), "--launch-gate", *mode_args])
        assert exit_code == 2

    assert model_setup_calls == 0


def test_eval_runner_launch_gate_rejects_an_explicit_unlimited_deadline(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    monkeypatch.delenv("ALYSIS_RUN_DEADLINE_SECONDS", raising=False)
    monkeypatch.setattr(
        eval_runner_module,
        "load_skills_eval_cases",
        lambda _path: _complete_launch_cases(tmp_path),
    )
    monkeypatch.setattr(eval_runner_module, "resolve_skills_eval_modes", lambda _modes: ())
    monkeypatch.setattr(
        eval_runner_module,
        "load_config",
        lambda: AppConfig(
            model="test-model",
            skills_auto_invoke=True,
            run_deadline_unlimited=True,
        ),
    )

    def _unexpected_preflight(**_kwargs: Any) -> SkillsEvalAuthPreflightResult:
        raise AssertionError("preflight must not run without a finite launch deadline")

    monkeypatch.setattr(
        eval_runner_module,
        "run_skills_eval_auth_preflight",
        _unexpected_preflight,
    )

    exit_code = _eval_cli_usage_exit(
        ["--manifest", str(manifest), "--launch-gate", "--mode", "combined_auto"]
    )

    assert exit_code == 2


def test_eval_runner_launch_gate_persists_readiness_and_enforces_it(
    monkeypatch, tmp_path: Path
) -> None:
    exit_code, written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary=_passing_launch_summary(relevant_skill_usage_count=0),
        launch_gate=True,
    )

    assert exit_code == 1
    release_gates = written["summary"]["release_gates"]
    assert release_gates["production_ready"] is False
    assert "relevant_skill_usage_rate" in release_gates["failing_gates"]


def test_eval_runner_launch_gate_uses_effective_auto_mode_for_selector_gates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    summary = _passing_launch_summary(relevant_skill_usage_count=1)
    summary["modes"]["combined_auto"].update(  # type: ignore[index]
        {
            "automatic_selection_exact_match_count": 0,
            "selector_available_run_count": 0,
            "selector_unavailable_run_count": 1,
        }
    )

    exit_code, written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary=summary,
        launch_gate=True,
        configured_skills_auto_invoke=False,
    )

    assert exit_code == 1
    release_gates = written["summary"]["release_gates"]
    assert release_gates["default_skills_auto_invoke"] is True
    assert "automatic_selection_exact_match_rate" in release_gates["failing_gates"]
    assert "selector_unavailable_runs" in release_gates["failing_gates"]


def test_eval_runner_launch_gate_marks_failed_selected_record_not_ready(
    monkeypatch, tmp_path: Path
) -> None:
    exit_code, written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary=_passing_launch_summary(relevant_skill_usage_count=1),
        record_status="failed",
        launch_gate=True,
    )

    assert exit_code == 1
    release_gates = written["summary"]["release_gates"]
    assert release_gates["production_ready"] is False
    assert "failed_selected_records" in release_gates["failing_gates"]


def test_eval_runner_launch_gate_fails_fast_when_auth_preflight_fails(
    monkeypatch, tmp_path: Path
) -> None:
    exit_code, written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary=_passing_launch_summary(relevant_skill_usage_count=1),
        launch_gate=True,
        preflight_ok=False,
    )

    assert exit_code == 1
    assert written["preflight_call_count"] == 1
    assert written.get("suite_call_count", 0) == 0


def test_eval_runner_launch_gate_returns_zero_when_cases_and_readiness_pass(
    monkeypatch, tmp_path: Path
) -> None:
    exit_code, _written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary=_passing_launch_summary(relevant_skill_usage_count=1),
        launch_gate=True,
    )

    assert exit_code == 0
    assert _written["preflight_call_count"] == 1
    assert _written["suite_call_count"] == 1
    assert _written["preflight_deadline_seconds"] == 300.0
    assert _written["preflight_deadline_source"] == "runtime_default"
    assert _written["suite_deadline_seconds"] == 300.0
    assert _written["suite_deadline_source"] == "runtime_default"
    assert _written["suite_verification_deadline_seconds"] == 300.0


def test_eval_runner_diagnostic_deadline_is_opt_in(monkeypatch, tmp_path: Path) -> None:
    exit_code, written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary={"modes": {}},
    )

    assert exit_code == 0
    assert written.get("preflight_call_count", 0) == 0
    assert written["suite_deadline_seconds"] is None
    assert written["suite_verification_deadline_seconds"] is None

    exit_code, written = _run_stubbed_eval_cli(
        monkeypatch,
        tmp_path,
        summary={"modes": {}},
        extra_args=("--deadline-seconds", "12.5"),
    )

    assert exit_code == 0
    assert written["suite_deadline_seconds"] == 12.5
    assert written["suite_deadline_source"] == "explicit_cli"
    assert written["suite_verification_deadline_seconds"] == 12.5


def test_eval_runner_rejects_invalid_deadline_values(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")

    for value in ("0", "-1", "nan", "inf", "not-a-number"):
        exit_code = _eval_cli_usage_exit(["--manifest", str(manifest), "--deadline-seconds", value])
        assert exit_code == 2, value
