from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools.git import (
    _GIT_HISTORY_SHOW_BODY_MAX_CHARS,
    _GIT_HISTORY_SHOW_PATCH_MAX_CHARS,
    GitError,
    git_history,
    git_status,
)


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="git-history-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(tmp_path: Path):
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, subject: str, body: str | None = None) -> str:
    args = ["commit", "-m", subject]
    if body is not None:
        args.extend(["-m", body])
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_history_repo(repo: Path) -> tuple[str, str, str]:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    app = repo / "app.py"
    app.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    commit1 = _commit(repo, "init app")

    app.write_text("alpha\nbeta-updated\ngamma\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    commit2 = _commit(repo, "fix parser", "Detailed parser history for review.")

    notes = repo / "notes.txt"
    notes.write_text("side note\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    commit3 = _commit(repo, "touch notes")

    return commit1, commit2, commit3


def test_build_tools_registers_git_history_for_git_backed_workspace(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    tools = _build_tools(tmp_path)

    assert "git_history" in tools
    schema = tools["git_history"].as_openai_tool()["function"]["parameters"]
    assert schema["required"] == ["mode"]
    assert schema["properties"]["mode"]["enum"] == ["log", "show", "blame"]


def test_build_tools_omits_git_history_outside_git_repo(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    assert "git_history" not in tools


def test_git_history_log_mode_returns_structured_commits(tmp_path: Path) -> None:
    _commit1, commit2, _commit3 = _init_history_repo(tmp_path)

    result = git_history(
        root=tmp_path,
        mode="log",
        path="app.py",
        limit=2,
        ref="HEAD",
        grep="fix",
        author="Test User",
    )

    assert result["mode"] == "log"
    assert result["path"] == "app.py"
    assert result["limit"] == 2
    assert result["ref"] == "HEAD"
    assert result["grep"] == "fix"
    assert result["author"] == "Test User"
    assert result["truncated"] is False
    commits = result["commits"]
    assert len(commits) == 1
    assert commits[0]["commit"] == commit2
    assert commits[0]["subject"] == "fix parser"
    assert commits[0]["author_name"] == "Test User"
    assert commits[0]["body_excerpt"] == "Detailed parser history for review."
    assert commits[0]["body_truncated"] is False


def test_git_history_worktree_head_keeps_subject_only_commit(tmp_path: Path) -> None:
    main_repo = tmp_path / "main"
    worktree = tmp_path / "feature-worktree"
    main_repo.mkdir()
    _init_history_repo(main_repo)
    main_head = _git(main_repo, "rev-parse", "HEAD").stdout.strip()
    _git(main_repo, "worktree", "add", "-b", "feature", str(worktree))

    feature_file = worktree / "feature.txt"
    feature_file.write_text("worktree-only\n", encoding="utf-8")
    _git(worktree, "add", "feature.txt")
    worktree_head = _commit(worktree, "subject only worktree commit")

    status = git_status(root=worktree)["status"]
    history = git_history(root=worktree, mode="log", ref="HEAD", limit=1)

    assert status.splitlines()[0].startswith("## feature")
    assert worktree_head != main_head
    assert len(history["commits"]) == 1
    commit = history["commits"][0]
    assert commit["commit"] == worktree_head
    assert worktree_head.startswith(commit["short_commit"])
    assert commit["author_name"] == "Test User"
    assert commit["author_email"] == "test@example.com"
    assert commit["subject"] == "subject only worktree commit"
    assert commit["body_excerpt"] == ""
    assert commit["body_truncated"] is False


def test_git_history_show_mode_returns_commit_and_patch_excerpt(tmp_path: Path) -> None:
    _commit1, commit2, _commit3 = _init_history_repo(tmp_path)

    result = git_history(root=tmp_path, mode="show", commit=commit2, path="app.py")

    assert result["mode"] == "show"
    assert result["path"] == "app.py"
    commit_meta = result["commit"]
    assert commit_meta["commit"] == commit2
    assert commit_meta["subject"] == "fix parser"
    assert commit_meta["body_excerpt"] == "Detailed parser history for review."
    assert commit_meta["body_truncated"] is False
    assert "beta-updated" in result["patch_excerpt"]
    assert result["patch_truncated"] is False


def test_git_history_show_accepts_ref_alias_through_tool(tmp_path: Path) -> None:
    _commit1, _commit2, commit3 = _init_history_repo(tmp_path)
    tools = _build_tools(tmp_path)

    result = tools["git_history"].run({"mode": "show", "ref": "HEAD"})

    assert result["mode"] == "show"
    assert result["commit"]["commit"] == commit3
    assert result["commit"]["subject"] == "touch notes"


def test_git_history_blame_mode_returns_line_mapping(tmp_path: Path) -> None:
    commit1, commit2, _commit3 = _init_history_repo(tmp_path)

    result = git_history(
        root=tmp_path,
        mode="blame",
        path="app.py",
        start_line=1,
        end_line=2,
    )

    assert result["mode"] == "blame"
    assert result["path"] == "app.py"
    assert result["start_line"] == 1
    assert result["end_line"] == 2
    lines = result["lines"]
    assert len(lines) == 2
    assert lines[0]["line_number"] == 1
    assert lines[0]["commit"] == commit1
    assert lines[0]["content"] == "alpha"
    assert lines[1]["line_number"] == 2
    assert lines[1]["commit"] == commit2
    assert lines[1]["content"] == "beta-updated"


def test_git_history_reports_not_git_repo(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="not a git repository"):
        git_history(root=tmp_path, mode="log")


def test_git_history_rejects_invalid_commit_and_range(tmp_path: Path) -> None:
    _init_history_repo(tmp_path)

    with pytest.raises(GitError, match="invalid revision|invalid revision or path"):
        git_history(root=tmp_path, mode="show", commit="deadbeef")

    with pytest.raises(GitError, match="Invalid line range"):
        git_history(
            root=tmp_path,
            mode="blame",
            path="app.py",
            start_line=4,
            end_line=2,
        )


def test_git_history_reports_git_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("missing git")

    monkeypatch.setattr("alysis_code.tools.git.subprocess.run", _boom)

    with pytest.raises(GitError, match="git not available"):
        git_history(root=tmp_path, mode="log")


def test_git_history_show_truncates_large_body_and_patch(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    target = tmp_path / "big.txt"
    target.write_text("".join(f"old line {i}\n" for i in range(600)), encoding="utf-8")
    _git(tmp_path, "add", "big.txt")
    _commit(tmp_path, "seed big file")

    target.write_text("".join(f"new line {i}\n" for i in range(600)), encoding="utf-8")
    _git(tmp_path, "add", "big.txt")
    commit = _commit(tmp_path, "rewrite big file", "body " * 400)

    result = git_history(root=tmp_path, mode="show", commit=commit, path="big.txt")

    commit_meta = result["commit"]
    assert commit_meta["body_truncated"] is True
    assert len(commit_meta["body_excerpt"]) <= _GIT_HISTORY_SHOW_BODY_MAX_CHARS + 14
    assert commit_meta["body_excerpt"].endswith("...(truncated)")
    assert result["patch_truncated"] is True
    assert len(result["patch_excerpt"]) <= _GIT_HISTORY_SHOW_PATCH_MAX_CHARS + 14
    assert result["patch_excerpt"].endswith("...(truncated)")


@pytest.fixture(autouse=True)
def history_semantics_without_os_backend(monkeypatch):
    # These tests isolate Git history behavior; installed tests cover containment.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
