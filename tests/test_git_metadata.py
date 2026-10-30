import subprocess

import pytest

from alysis_code.git_metadata import execute, mutation_argv, operation
from alysis_code.git_shell_inspection import git_shell_inspect, inspection_argv
from alysis_code.tools.git import GitError


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        return (
            subprocess.check_output(
                ["git", "--no-optional-locks", "-C", str(tmp_path), *args], stderr=subprocess.PIPE
            )
            .decode("utf-8")
            .strip()
        )

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "core.autocrlf", "false")
    for path in ("staged.txt", "notes.txt"):
        (tmp_path / path).write_bytes(b"original\n")
    git("add", ".")
    git("commit", "-m", "initial")
    (tmp_path / "staged.txt").write_bytes(b"staged\n")
    git("add", "staged.txt")
    (tmp_path / "notes.txt").write_bytes(b"unstaged\n")
    (tmp_path / "untracked.txt").write_bytes(b"untracked\n")
    return tmp_path, git


def worktree(root):
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts
    }


def test_create_branch_and_commit_preserve_dirty_worktree(repository, monkeypatch):
    root, git = repository
    before = worktree(root)
    index = (root / ".git/index").read_bytes()
    config = (root / ".git/config").read_bytes()
    # A native Git hook must never execute in the fixed operation worker.
    (root / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 67\n")

    class NoProcess(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            raise AssertionError("Fixed worker must not execute programs")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", NoProcess)
        execute(root, operation(mutation_argv("git switch -c test/alysis-git-20261024")))
        assert (root / ".git/index").read_bytes() == index
        result = execute(root, operation(mutation_argv('git commit -m "Test Alysis Git commit"')))
    assert git("branch", "--show-current") == "test/alysis-git-20261024"
    assert git("show", "--format=", "--name-only", "HEAD") == "staged.txt"
    assert git("log", "-1", "--format=%s") == "Test Alysis Git commit"
    assert git("rev-parse", "HEAD") == result["commit"]
    assert "Test Alysis Git commit" in git("reflog", "-1")
    assert worktree(root) == before
    assert (root / ".git/config").read_bytes() == config
    assert (root / ".git/index").read_bytes() == index
    assert "notes.txt" in git("status", "--short")
    with pytest.raises(GitError, match="already exists"):
        execute(root, operation(["switch", "-c", "test/alysis-git-20261024"]))


def test_explicit_add_stages_only_named_file(repository):
    root, git = repository
    execute(root, operation(["add", "--", "notes.txt"]))
    assert git("diff", "--cached", "--name-only").splitlines() == ["notes.txt", "staged.txt"]
    assert git("show", ":notes.txt") == "unstaged"
    assert "?? untracked.txt" in git("status", "--short")


def test_commit_only_preserves_other_staged_files(repository):
    root, git = repository
    git("add", "notes.txt")
    before = (root / ".git/index").read_bytes()
    execute(root, operation(["commit", "--only", "staged.txt", "-m", "selected change"]))
    assert git("show", "--format=", "--name-only", "HEAD") == "staged.txt"
    assert git("diff", "--cached", "--name-only") == "notes.txt"
    assert (root / ".git/index").read_bytes() == before
    result = git_shell_inspect(root=root, argv=["show", "--stat", "--oneline", "--summary", "HEAD"])
    assert result["exit_code"] == 0, result
    assert "selected change" in result["stdout"] and "staged.txt" in result["stdout"]
    assert "1 insertion" in result["stdout"] and "1 deletion" in result["stdout"]
    names = git_shell_inspect(root=root, argv=["show", "--format=", "--name-only", "HEAD"])
    assert names["stdout"].strip() == git("show", "--format=", "--name-only", "HEAD")


def test_commit_only_refuses_partially_staged_content(repository):
    root, git = repository
    old = git("rev-parse", "HEAD")
    (root / "staged.txt").write_bytes(b"unstaged new content\n")
    with pytest.raises(GitError, match="unstaged content"):
        execute(root, operation(["commit", "--only", "staged.txt", "-m", "must not commit"]))
    assert git("rev-parse", "HEAD") == old


@pytest.mark.parametrize(
    "command",
    [
        "git status; git switch -c bad",
        "git switch -C main",
        "git commit --amend -m bad",
        "git -c core.hooksPath=evil commit -m bad",
        "git switch -c ../bad",
        "git add -A",
        "git add .",
    ],
)
def test_unsupported_operations_do_not_write(repository, command):
    root, _ = repository
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    argv = mutation_argv(command)
    if argv:
        with pytest.raises((GitError, ValueError)):
            execute(root, operation(argv))
    assert before == {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_branch_inspection_is_readonly(repository):
    root, git = repository
    for command in (
        "git branch --list test/alysis-git-20261024",
        "git branch --show-current",
        "git branch",
    ):
        result = git_shell_inspect(root=root, argv=inspection_argv(command))
        assert result["exit_code"] == 0, result
    assert inspection_argv("git branch new") is None
    assert git_shell_inspect(root=root, argv=["branch", "--show-current"])["stdout"].strip() == git(
        "branch", "--show-current"
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--cached", "--name-only"],
        ["--cached", "--name-status"],
        ["--name-only"],
        ["--name-status"],
    ],
)
def test_diff_file_lists_match_git(repository, options):
    root, git = repository
    result = git_shell_inspect(root=root, argv=["diff", *options])
    assert result["exit_code"] == 0, result
    assert result["stdout"].strip() == git("diff", *options)


def test_branch_name_is_not_a_required_output_file(tmp_path):
    from alysis_code.agent.acceptance_contract import build_acceptance_contract

    for instruction in (
        "Create and switch to a new branch named test/alysis-git-20261024 from the current branch.",
        "Create branch `feature/test.txt`.",
        "Create a tag named release/v1.",
    ):
        contract = build_acceptance_contract(root=tmp_path, instruction=instruction)
        assert not contract.allowed_output_paths
        assert not contract.path_refs
    contract = build_acceptance_contract(
        root=tmp_path, instruction="On branch test/one create src/output.py."
    )
    assert "src/output.py" in contract.allowed_output_paths


def test_readonly_policy_refuses_before_launch(repository):
    from alysis_code.config import ConfigError
    from alysis_code.git_metadata import run
    from alysis_code.sandbox_settings import ShellSandboxSettings

    root, _ = repository
    with pytest.raises(ConfigError, match="disabled"):
        run(
            settings=ShellSandboxSettings(filesystem_access="read-only"),
            root=root,
            cwd=root,
            argv=["switch", "-c", "test/no"],
            timeout_s=30,
            cancel_check=None,
        )


@pytest.mark.parametrize("lock", ["HEAD.lock", "index.lock"])
def test_concurrent_git_writer_is_not_overwritten(repository, lock):
    root, git = repository
    old = git("rev-parse", "HEAD")
    (root / ".git" / lock).write_bytes(b"another writer")
    with pytest.raises(Exception, match="lock"):
        execute(root, operation(["commit", "-m", "must not commit"]))
    assert git("rev-parse", "HEAD") == old
    assert (root / ".git" / lock).read_bytes() == b"another writer"
