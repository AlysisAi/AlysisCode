"""Real repository regressions for the agent's Git command boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from alysis_code.tools import git


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    root = tmp_path / "repository with spaces"
    root.mkdir()
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "Git Fixture")
    run_git(root, "config", "user.email", "fixture@example.invalid")
    return root


def run_git(root: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
    ).stdout.strip()


def commit(root):
    (root / "file.txt").write_text("one\n", encoding="utf-8")
    run_git(root, "add", "file.txt")
    run_git(root, "commit", "-m", "Initial change")
    return run_git(root, "rev-parse", "HEAD")


def test_new_repository_has_empty_history_and_can_review_first_staged_file(repo):
    result = git.git_history(root=repo, mode="log")
    assert result["commits"] == []
    assert result["truncated"] is False
    (repo / "file.txt").write_text("first\n", encoding="utf-8")
    run_git(repo, "add", "file.txt")
    assert "+first" in git.git_diff(root=repo, staged=True)["diff"]


def test_missing_revision_is_still_an_error_in_new_repository(repo):
    with pytest.raises(git.GitError, match="invalid revision"):
        git.git_history(root=repo, mode="log", ref="missing-branch")


@pytest.mark.parametrize("mode", ["log", "show"])
def test_revision_named_like_a_file_is_unambiguous(repo, mode):
    head = commit(repo)
    (repo / "HEAD").write_text("a file, not a revision", encoding="utf-8")
    result = git.git_history(root=repo, mode=mode, ref="HEAD")
    assert (result["commits"][0] if mode == "log" else result["commit"])["commit"] == head


@pytest.mark.parametrize("mode", ["log", "show"])
def test_revision_cannot_be_interpreted_as_a_git_option(repo, mode):
    commit(repo)
    with pytest.raises(git.GitError, match="invalid revision"):
        git.git_history(root=repo, mode=mode, ref="--all")


def test_process_uses_explicit_cwd_closed_stdin_and_noninteractive_environment(repo, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git.subprocess, "run", run)
    supplied = {**os.environ, "GIT_PAGER": "broken-pager", "GIT_INDEX_FILE": "private-index"}
    git._run_git(repo, ["status"], env=supplied)
    argv, options = calls[0]
    assert "--no-pager" in argv
    assert options["cwd"] == str(repo)
    assert options["stdin"] == subprocess.DEVNULL
    assert 0 < options["timeout"] <= 30
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["env"]["GIT_INDEX_FILE"] == "private-index"
    assert supplied["GIT_PAGER"] == "broken-pager"


def test_timeout_is_a_git_error_with_partial_diagnostics(repo, monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, kwargs.get("timeout", 30), output=b"partial output", stderr=b"partial diagnostic"
        )

    monkeypatch.setattr(git.subprocess, "run", timeout)
    with pytest.raises(git.GitError, match="timed out") as caught:
        git._run_git(repo, ["status"])
    assert "partial diagnostic" in str(caught.value)


def test_history_ignores_configured_external_textconv(repo):
    commit(repo)
    run_git(repo, "config", "diff.fixture.textconv", "missing-fixture-converter")
    (repo / ".gitattributes").write_text("file.txt diff=fixture\n", encoding="utf-8")
    assert "+one" in git.git_history(root=repo, mode="show", ref="HEAD")["patch_excerpt"]


def test_history_preserves_unicode_despite_local_output_encoding(repo):
    commit(repo)
    subject = "Διόρθωση αρχείου"
    run_git(repo, "commit", "--allow-empty", "-m", subject)
    run_git(repo, "config", "i18n.logOutputEncoding", "ISO8859-7")
    assert git.git_history(root=repo, mode="log", limit=1)["commits"][0]["subject"] == subject
    assert git.git_history(root=repo, mode="show", ref="HEAD")["commit"]["subject"] == subject


def test_history_path_is_literal_not_a_wildcard(repo):
    for name in ("[ab].txt", "a.txt"):
        (repo / name).write_text(name + "\n", encoding="utf-8")
        run_git(repo, "add", "--", name)
    run_git(repo, "commit", "-m", "Files with pattern characters")
    result = git.git_history(root=repo, mode="show", ref="HEAD", path="[ab].txt")
    assert "+[ab].txt" in result["patch_excerpt"]
    assert "+a.txt" not in result["patch_excerpt"]


def test_non_repository_remains_an_error(tmp_path):
    with pytest.raises(git.GitError, match="not a git repository"):
        git.git_history(root=tmp_path, mode="log")
