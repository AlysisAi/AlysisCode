from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.agent.blast_radius import (
    BlastRadiusScope,
    BlastRadiusStatus,
    ScopeEntry,
    ScopeLanguage,
    ScopeTier,
    command_path_selectors,
    selection_covers,
)
from alysis_code.agent.verification import (
    TurnExecutionState,
    _record_tool_effect,
    scope_environment_is_host,
)
from alysis_code.sandbox_runner import HostShellRunner, LazyShellRunner


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    (tmp_path / "unit").mkdir()
    (tmp_path / "integration").mkdir()
    (tmp_path / "unit/test_alpha.py").write_text(
        "import pytest\n@pytest.mark.safe\ndef test_safe():\n    assert True\n"
        "def test_other():\n    assert True\n"
    )
    (tmp_path / "integration/test_beta.py").write_text("def test_beta():\n    assert True\n")
    return tmp_path


def _command(options: str) -> str:
    executable = (
        # Forward slashes work in CMD and preserve the path in the shared
        # POSIX-shaped verification-command observer.
        subprocess.list2cmdline([Path(sys.executable).as_posix()])
        if os.name == "nt"
        else shlex.quote(sys.executable)
    )
    return f"{executable} -m pytest {options}"


def _selected(repo: Path, options: str, **kwargs):
    return command_path_selectors(_command(options), workspace_root=repo, **kwargs)


def _run(repo: Path, command: str, *, cwd: Path | None = None):
    return subprocess.run(
        command,
        shell=True,
        cwd=cwd or repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )


@pytest.mark.parametrize(
    "options",
    [
        "unit/test_alpha.py::test_safe -q",
        "unit/test_alpha.py -k safe -q",
        "unit/test_alpha.py -m safe -q",
        "unit/test_alpha.py --deselect=unit/test_alpha.py::test_other -q",
    ],
)
def test_successful_partial_file_run_cannot_earn_full_file_coverage(repo: Path, options: str):
    path = repo / "unit/test_alpha.py"
    path.write_text(
        path.read_text().replace(
            "def test_other():\n    assert True", "def test_other():\n    assert False"
        )
    )
    result = _run(repo, _command(options))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert _selected(repo, options) is None


def test_bare_directory_runs_only_its_files(repo: Path):
    (repo / "integration/test_beta.py").write_text("def test_beta():\n    assert False\n")
    result = _run(repo, _command("unit -q"))
    assert result.returncode == 0, result.stdout + result.stderr
    selected = _selected(repo, "unit -q")
    assert selected == ("unit/test_alpha.py",)
    assert not selection_covers(selected, "integration/test_beta.py")


@pytest.mark.parametrize(
    "filename, content",
    [
        ("pytest.ini", "[pytest]\ntestpaths = unit\naddopts = --capture=sys\n"),
        (
            "pyproject.toml",
            '[tool.pytest.ini_options]\ntestpaths = ["unit"]\naddopts = "--capture=sys"\n',
        ),
        ("setup.cfg", "[tool:pytest]\ntestpaths = unit\naddopts = --capture=sys\n"),
        ("tox.ini", "[pytest]\ntestpaths = unit\naddopts = --capture=sys\n"),
    ],
)
def test_configured_default_paths_do_not_claim_unselected_integration(
    repo: Path, filename: str, content: str
):
    (repo / filename).write_text(content)
    (repo / "integration/test_beta.py").write_text("def test_beta():\n    assert False\n")
    result = _run(repo, _command("-q"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _selected(repo, "-q") == ("unit/test_alpha.py",)
    # Explicit operands replace configured default testpaths.
    assert _selected(repo, "integration/test_beta.py -q") == ("integration/test_beta.py",)


@pytest.mark.parametrize(
    "source",
    [
        "config",
        "inherited_environment",
        pytest.param(
            "inline_environment",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX inline environment syntax"),
        ),
    ],
)
def test_hidden_default_filter_cannot_bypass_selection_guard(
    repo: Path, monkeypatch: pytest.MonkeyPatch, source: str
):
    path = repo / "unit/test_alpha.py"
    path.write_text(
        path.read_text().replace(
            "def test_other():\n    assert True", "def test_other():\n    assert False"
        )
    )
    command = _command("unit/test_alpha.py -q")
    if source == "config":
        (repo / "pytest.ini").write_text("[pytest]\naddopts = -k safe\n")
    elif source == "inherited_environment":
        monkeypatch.setenv("PYTEST_ADDOPTS", "-k safe")
    else:
        command = 'PYTEST_ADDOPTS="-k safe" ' + command
    result = _run(repo, command)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert command_path_selectors(command, workspace_root=repo) is None


def test_configuration_search_starts_from_explicit_argument_directory(repo: Path):
    (repo / "unit/pytest.ini").write_text("[pytest]\naddopts = -k safe\n")
    path = repo / "unit/test_alpha.py"
    path.write_text(
        path.read_text().replace(
            "def test_other():\n    assert True", "def test_other():\n    assert False"
        )
    )
    result = _run(repo, _command("unit -q"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert _selected(repo, "unit -q") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX command/env shell builtins")
def test_command_env_assignments_and_cleared_inherited_options(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    command = 'command env PYTEST_ADDOPTS="-k safe" ' + _command("unit -q")
    result = _run(repo, command)
    assert result.returncode == 0
    assert "1 passed" in result.stdout
    assert command_path_selectors(command, workspace_root=repo) is None
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k safe")
    cleared = "env PYTEST_ADDOPTS= " + _command("unit -q")
    assert command_path_selectors(cleared, workspace_root=repo) == ("unit/test_alpha.py",)


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m pytest unit -q",
        "bash -lc 'python -m pytest unit -q'",
    ],
)
def test_environment_wrappers_without_observed_options_are_inconclusive(repo: Path, command: str):
    assert command_path_selectors(command, workspace_root=repo) is None


@pytest.mark.parametrize(
    "filename,content",
    [
        ("pytest.toml", '[pytest]\naddopts = ["-k", "safe"]\n'),
        ("pyproject.toml", '[tool.pytest]\naddopts = ["-k", "safe"]\n'),
        ("pytest.ini", "[pytest]\npython_functions = test_safe\n"),
        ("pyproject.toml", "tool = 1\n"),
    ],
)
def test_unsupported_or_invalid_config_does_not_imply_whole_scope(
    repo: Path, filename: str, content: str
):
    (repo / filename).write_text(content)
    assert _selected(repo, "unit/test_alpha.py -q") is None


def test_configured_directory_exclusion_is_respected(repo: Path):
    (repo / "pytest.ini").write_text("[pytest]\nnorecursedirs = integration\n")
    (repo / "integration/test_beta.py").write_text("def test_beta():\n    assert False\n")
    result = _run(repo, _command("-q"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _selected(repo, "-q") == ("unit/test_alpha.py",)


def test_effective_cwd_and_cd_prefix_are_composed(repo: Path):
    result = _run(repo, _command("-q"), cwd=repo / "unit")
    assert result.returncode == 0
    assert _selected(repo, "-q", working_directory="unit") == ("unit/test_alpha.py",)
    command = "cd unit && " + _command("-q")
    assert command_path_selectors(command, workspace_root=repo) == ("unit/test_alpha.py",)
    assert _selected(repo, "-q", working_directory="..") is None


def test_default_discovery_records_only_existing_matching_files(repo: Path):
    selected = _selected(repo, "-q")
    assert set(selected or ()) == {"unit/test_alpha.py", "integration/test_beta.py"}
    assert not selection_covers(selected, "unit/test_added_later.py")
    (repo / "pytest.ini").write_text("[pytest]\npython_files = test_alpha.py\n")
    assert _selected(repo, "-q") == ("unit/test_alpha.py",)


def test_selection_does_not_import_conftest_or_tests(repo: Path):
    (repo / "conftest.py").write_text("raise RuntimeError('must not import')\n")
    (repo / "unit/test_alpha.py").write_text("raise RuntimeError('must not import')\n")
    assert _selected(repo, "unit/test_alpha.py -q") == ("unit/test_alpha.py",)


@pytest.mark.parametrize(
    "options",
    [
        "--lf -q",
        "--ff -q",
        "--pyargs package",
        "-o addopts='-k safe' unit/test_alpha.py",
        "--ignore=integration -q",
        "--unknown-plugin-option -q",
        "missing",
        "unit/*.py",
    ],
)
def test_unsupported_or_unresolved_selection_stays_inconclusive(repo: Path, options: str):
    assert _selected(repo, options) is None


def test_unknown_environment_or_runner_cannot_claim_defaults(repo: Path):
    assert _selected(repo, "unit/test_alpha.py", environment_known=False) is None
    assert command_path_selectors("npm test", workspace_root=repo) is None
    assert command_path_selectors("go test ./...", workspace_root=repo) is None
    assert command_path_selectors("pytest -q") is None
    assert not scope_environment_is_host(object())
    assert scope_environment_is_host(HostShellRunner())
    lazy = LazyShellRunner(lambda: (_ for _ in ()).throw(AssertionError("must not instantiate")))
    assert not scope_environment_is_host(lazy)
    lazy._runner = HostShellRunner()
    assert scope_environment_is_host(lazy)


def _state() -> TurnExecutionState:
    state = TurnExecutionState(execution_requested=True)
    state.blast_radius_scope = BlastRadiusScope(
        entries=tuple(
            ScopeEntry(path, ScopeTier.IMPORTER)
            for path in (
                "unit/test_alpha.py",
                "integration/test_beta.py",
            )
        ),
        language=ScopeLanguage.PYTHON,
    )
    return state


def _record(state: TurnExecutionState, repo: Path, options: str, tool: str, *, cwd: str = "."):
    command = _command(options)
    result = _run(repo, command, cwd=repo / cwd)
    item = {
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if tool == "verify_run":
        payload = {
            "commands": [command],
            "all_passed": result.returncode == 0,
            "command_results": [item],
        }
        arguments = {"commands": [command]}
    else:
        payload = {**item, "cmd": command, "cwd": str(repo / cwd)}
        arguments = {"cmd": command, "cwd": cwd}
    _record_tool_effect(
        root=repo,
        state=state,
        tool_name=tool,
        arguments=arguments,
        status="ok" if result.returncode == 0 else "failed",
        result=payload,
        known_verification_commands=[],
        verification_authoritative=True,
        scope_environment_known=True,
    )
    return result


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_partial_passes_cannot_clear_file_scope_gate_or_baseline(repo: Path, tool: str):
    state = _state()
    assert _record(state, repo, "unit/test_alpha.py::test_safe -q", tool).returncode == 0
    assert not state.has_blast_radius_baseline()
    assert state.blast_radius_runs[0].selectors is None
    state.touched_repo_paths.add("existing.py")
    assert _record(state, repo, "unit/test_alpha.py -k safe -q", tool).returncode == 0
    assert (
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute").status
        == BlastRadiusStatus.GATE_MISSING
    )


@pytest.mark.parametrize("tool", ["shell_run", "verify_run"])
def test_full_observed_scope_still_baselines_and_verifies(repo: Path, tool: str):
    state = _state()
    assert _record(state, repo, "-q", tool).returncode == 0
    assert state.has_blast_radius_baseline()
    state.touched_repo_paths.add("existing.py")
    assert _record(state, repo, "unit integration -q", tool).returncode == 0
    assert (
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute").status
        == BlastRadiusStatus.CLEAN
    )


def test_real_failing_scope_remains_failed_and_regression_is_not_suppressed(repo: Path):
    state = _state()
    _record(state, repo, "-q", "verify_run")
    state.touched_repo_paths.add("integration/test_beta.py")
    (repo / "integration/test_beta.py").write_text("def test_beta():\n    assert False\n")
    assert _record(state, repo, "-q", "verify_run").returncode == 1
    assert state.last_verification_passed is False
    assessment = state.compute_blast_radius_assessment(enabled=True, turn_intent="execute")
    assert assessment.status == BlastRadiusStatus.REGRESSED
    assert assessment.new_failures == ("integration/test_beta.py::test_beta",)


def test_native_shell_cwd_is_used_by_actual_effect_recorder(repo: Path):
    state = _state()
    _record(state, repo, "-q", "shell_run", cwd="unit")
    assert state.blast_radius_baseline_covered_paths() == ("unit/test_alpha.py",)
