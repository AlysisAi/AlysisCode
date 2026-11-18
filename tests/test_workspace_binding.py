from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from alysis_code import workspace_binding as workspace_binding_mod
from alysis_code.workspace_binding import (
    WorkspaceBindingError,
    WorkspaceRiskLevel,
    discover_workspace_candidates,
    resolve_workspace_binding,
)
from alysis_code.workspace_binding_ui import resolve_startup_workspace_binding


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def test_resolve_workspace_binding_errors_when_missing_without_create(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "repo"

    with pytest.raises(WorkspaceBindingError, match="create_if_missing=True"):
        resolve_workspace_binding(missing)


def test_resolve_workspace_binding_creates_missing_path_when_requested(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "repo"

    binding = resolve_workspace_binding(missing, create_if_missing=True)

    assert binding.created_path is True
    assert missing.exists()
    assert binding.requested_path == missing.resolve()
    assert binding.resolved_candidate_path == missing.resolve()
    assert binding.workspace_context.workspace_root == missing.resolve()
    assert binding.risk_level == WorkspaceRiskLevel.HEALTHY


def test_resolve_workspace_binding_marks_git_repo_root_healthy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)

    binding = resolve_workspace_binding(repo)

    assert binding.workspace_context.workspace_root == repo.resolve()
    assert binding.risk_level == WorkspaceRiskLevel.HEALTHY


def test_resolve_workspace_binding_marks_git_subdir_healthy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    subdir = repo / "pkg" / "api"
    subdir.mkdir(parents=True)

    binding = resolve_workspace_binding(subdir)

    assert binding.resolved_candidate_path == subdir.resolve()
    assert binding.workspace_context.workspace_root == repo.resolve()
    assert binding.workspace_context.focus_relpath == "pkg/api"
    assert binding.risk_level == WorkspaceRiskLevel.HEALTHY


def test_resolve_workspace_binding_marks_home_directory_guarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    binding = resolve_workspace_binding(home, allow_broad_workspace=True)

    assert binding.risk_level == WorkspaceRiskLevel.GUARDED
    assert "home directory" in binding.risk_reasons[0]


def test_resolve_workspace_binding_marks_filesystem_root_blocked() -> None:
    binding = resolve_workspace_binding(Path("/"))

    assert binding.risk_level == WorkspaceRiskLevel.BLOCKED
    assert "filesystem root" in binding.risk_reasons[0]


def test_resolve_workspace_binding_marks_empty_non_home_dir_healthy(tmp_path: Path) -> None:
    workspace = tmp_path / "empty"
    workspace.mkdir()

    binding = resolve_workspace_binding(workspace)

    assert binding.risk_level == WorkspaceRiskLevel.HEALTHY
    assert binding.workspace_context.workspace_root == workspace.resolve()


def test_resolve_workspace_binding_marks_broad_plain_dir_guarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "wide"
    workspace.mkdir()
    monkeypatch.setattr(
        workspace_binding_mod,
        "_home_directory",
        lambda: (tmp_path / "somewhere_else").resolve(),
    )
    for name in [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "iota",
        "kappa",
        "lambda",
        "mu",
    ]:
        (workspace / name).mkdir()

    binding = resolve_workspace_binding(workspace)

    assert binding.risk_level == WorkspaceRiskLevel.GUARDED
    assert "many top-level entries" in binding.risk_reasons[0]


def test_discover_workspace_candidates_is_shallow_and_deterministic(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "notes").mkdir()
    (home / "misc").mkdir()
    (home / "alysis-feedback").mkdir()
    (home / "alysis-feedback" / "bundle.zip").write_bytes(b"zip")

    repo = home / "repo-alpha"
    _init_git_repo_with_commit(repo)

    container = home / "code"
    container.mkdir()
    project = container / "beta"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='beta'\n", encoding="utf-8")
    (project / "tests").mkdir()

    candidates = discover_workspace_candidates(home)

    assert [candidate.path for candidate in candidates] == [repo.resolve(), project.resolve()]
    assert candidates[0].score > candidates[1].score
    assert candidates[0].source == "child"
    assert candidates[1].source == "nested:code"


def test_resolve_startup_workspace_binding_noninteractive_guarded_requires_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    with pytest.raises(WorkspaceBindingError, match="allow-broad-workspace"):
        resolve_startup_workspace_binding(
            requested_path=home,
            interactive=False,
            source="cwd",
        )


def test_resolve_startup_workspace_binding_allows_guarded_override_noninteractive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    binding = resolve_startup_workspace_binding(
        requested_path=home,
        interactive=False,
        allow_broad_workspace=True,
        source="cwd",
    )

    assert binding.risk_level == WorkspaceRiskLevel.GUARDED
    assert binding.binding_source == "cwd"


def test_resolve_startup_workspace_binding_interactive_create_folder_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    prompts = iter(["greenfield"])

    binding = resolve_startup_workspace_binding(
        requested_path=home,
        interactive=True,
        source="cwd",
        select_action_interactive=lambda **_kwargs: ("create_folder", True),
        prompt_text=lambda *_args, **_kwargs: next(prompts),
    )

    assert binding.created_path is True
    assert binding.binding_source == "startup_create_path"
    assert binding.requested_path == (home / "greenfield").resolve()
    assert binding.requested_path.exists()


def test_resolve_startup_workspace_binding_create_folder_cancel_returns_to_action_menu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = home / "project"
    _init_git_repo_with_commit(project)
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    actions = iter(["create_folder", "choose_project"])

    binding = resolve_startup_workspace_binding(
        requested_path=home,
        interactive=True,
        source="cwd",
        select_action_interactive=lambda **_kwargs: (next(actions), True),
        select_candidate_interactive=lambda **_kwargs: (project.resolve(), True),
        prompt_text=lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert binding.created_path is False
    assert binding.binding_source == "startup_candidate"
    assert binding.requested_path == project.resolve()


def test_resolve_startup_workspace_binding_enter_path_cancel_returns_to_action_menu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = home / "project"
    _init_git_repo_with_commit(project)
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    actions = iter(["enter_path", "choose_project"])

    binding = resolve_startup_workspace_binding(
        requested_path=home,
        interactive=True,
        source="cwd",
        select_action_interactive=lambda **_kwargs: (next(actions), True),
        select_candidate_interactive=lambda **_kwargs: (project.resolve(), True),
        prompt_text=lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert binding.created_path is False
    assert binding.binding_source == "startup_candidate"
    assert binding.requested_path == project.resolve()
