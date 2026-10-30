"""A native directory denial must not become a misleading Git-filter failure."""

import subprocess

import pytest

from alysis_code.tools import git


@pytest.mark.parametrize(
    "message",
    [
        "fatal: cannot change to 'C:\\synthetic': Permission denied\n",
        "fatal: Unable to read current working directory: Permission denied\n",
    ],
)
@pytest.mark.parametrize("binary_stdin", [False, True])
def test_directory_denial_is_actionable_without_retry(tmp_path, monkeypatch, message, binary_stdin):
    calls = []

    def denied(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 128, b"" if binary_stdin else "", message.encode() if binary_stdin else message
        )

    monkeypatch.setattr(git.subprocess, "run", denied)
    with pytest.raises(git.GitError, match="git_directory_access_denied") as caught:
        git._run_git(tmp_path, ["status"], input_s="synthetic patch" if binary_stdin else None)
    assert "shell_run using the configured approval policy" in str(caught.value)
    assert message.strip() in str(caught.value)
    assert len(calls) == 1
    assert calls[0][0] == ["git", "--no-pager", "-C", str(tmp_path), "status"]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert calls[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_filter_inspection_preserves_the_directory_denial(tmp_path, monkeypatch):
    calls = []

    def denied(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 128, "", "fatal: cannot change to 'C:\\synthetic': Permission denied\n"
        )

    monkeypatch.setattr(git.subprocess, "run", denied)
    with pytest.raises(git.GitError, match="git_directory_access_denied"):
        git.git_status(root=tmp_path)
    assert len(calls) == 1
    assert "config" in calls[0]


@pytest.mark.parametrize(
    "code,message",
    [
        (128, "fatal: cannot change to 'missing': No such file or directory\n"),
        (128, "fatal: not a git repository\n"),
        (128, "error: open('file.txt'): Permission denied\n"),
        (128, "fatal: bad revision 'Permission denied'\n"),
        (0, "fatal: Unable to read current working directory: Permission denied\n"),
    ],
)
def test_other_results_are_not_reclassified(tmp_path, monkeypatch, code, message):
    result = subprocess.CompletedProcess([], code, "preserved output", message)
    monkeypatch.setattr(git.subprocess, "run", lambda *_a, **_kw: result)
    assert git._run_git(tmp_path, ["status"]) is result
