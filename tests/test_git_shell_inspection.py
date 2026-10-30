import subprocess

import pytest

from alysis_code.git_shell_inspection import git_shell_inspect, inspection_argv
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.tools.shell import shell_run
from alysis_code.windows_mxc import WindowsMxcRunner


@pytest.mark.parametrize(
    "command",
    [
        "git status --porcelain=v1 -b",
        "git --literal-pathspecs --no-pager show --format=fuller HEAD -- 'report[1].txt'",
        "git show --format=fuller --no-ext-diff HEAD -- ':(literal)report[1].txt'",
        "git --no-pager log -2 --oneline",
        "git diff --cached --no-ext-diff --no-textconv",
    ],
)
def test_literal_inspections_are_recognized(command):
    assert inspection_argv(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status; Remove-Item foo",
        "git status | Out-File foo",
        "git status > foo",
        "git status;",
        "git show $(Get-Content secret)",
        "git show `whoami`",
        "git commit -am hello",
        "git checkout main",
        "C:/untrusted/git.exe status",
        "git -c core.pager=program status",
        "git status && git add .",
    ],
)
def test_writes_and_shell_syntax_are_not_reinterpreted(command):
    assert inspection_argv(command) is None


def test_show_and_status_match_fixture_without_writes(tmp_path):
    pytest.importorskip("dulwich")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.PIPE)

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    for name in ["report[1].txt", "report1.txt"]:
        (tmp_path / name).write_bytes(b"original\n")
    git("add", ".")
    git("commit", "-m", "Unicode δοκιμή")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    argv = inspection_argv(
        "git show --format=fuller --no-ext-diff HEAD -- ':(literal)report[1].txt'"
    )
    result = git_shell_inspect(root=tmp_path, argv=argv)
    assert result["exit_code"] == 0
    assert "Unicode δοκιμή" in result["stdout"]
    assert "report[1].txt" in result["stdout"]
    assert "report1.txt" not in result["stdout"]
    # Preserve Git's distinction between a blob spec and a pathspec after --.
    unmatched = "git show --format=fuller --no-ext-diff -- 'HEAD:report[1].txt'"
    empty = git_shell_inspect(root=tmp_path, argv=inspection_argv(unmatched))
    assert empty["exit_code"] == 0 and empty["stdout"] == ""
    assert git("show", "--format=fuller", "--no-ext-diff", "--", "HEAD:report[1].txt") == b""
    glob = git_shell_inspect(
        root=tmp_path, argv=inspection_argv("git show HEAD -- 'report[1].txt'")
    )
    # Git accepts the exact filename as well as wildcard matches unless literal
    # pathspecs are requested. Compare against the real CLI, not a glob assumption.
    names = (
        git("show", "--format=", "--name-only", "HEAD", "--", "report[1].txt").decode().splitlines()
    )
    assert names == ["report1.txt", "report[1].txt"]
    assert all(name in glob["stdout"] for name in names)
    assert (
        git_shell_inspect(root=tmp_path, argv=["status", "--porcelain=v1"])["stdout"].strip() == ""
    )
    assert git_shell_inspect(root=tmp_path, argv=["show", "--output=forbidden"])["exit_code"] != 0
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows runner routing")
@pytest.mark.parametrize("lazy", [False, True])
def test_shell_inspection_uses_readonly_worker_before_writable_runner(tmp_path, monkeypatch, lazy):
    from alysis_code.contained_read_tools import ContainedReadTools

    runner = WindowsMxcRunner(
        settings=ShellSandboxSettings(
            mode="strict", backend="windows-mxc", filesystem_access="workspace-write"
        )
    )
    observed = []

    def read(self, name, args, **kwargs):
        observed.append((self.settings, name, args))
        return {"exit_code": 0, "stdout": "## main\n", "stderr": "", "truncated": False}

    def refuse(self, **kwargs):
        raise AssertionError("Writable shell setup must not run")

    monkeypatch.setattr(ContainedReadTools, "run", read)
    monkeypatch.setattr(WindowsMxcRunner, "run", refuse)
    if lazy:
        from alysis_code.sandbox_runner import LazyShellRunner

        actual = runner
        runner = LazyShellRunner(lambda: actual, settings=actual.settings)
    result = shell_run(root=tmp_path, cmd="git status -b", runner=runner)
    assert result["exit_code"] == 0
    assert result["execution_backend"] == "contained-readonly-git"
    assert observed[0][0].filesystem_access == "read-only"
    assert observed[0][0].network == "off"
    assert observed[0][1] == "git_shell_inspect"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows runner routing")
def test_history_graph_selects_native_readonly_before_limited_adapter(tmp_path, monkeypatch):
    import subprocess

    from alysis_code import git_native

    runner = WindowsMxcRunner(settings=ShellSandboxSettings(backend="windows-mxc"))
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        result = subprocess.CompletedProcess("git", 0, "* abc main\n", "")
        result.inspection_backend = "contained-native-git-readonly"
        return result

    monkeypatch.setattr(git_native, "run", read)
    result = shell_run(root=tmp_path, cmd="git log --graph --oneline --decorate -8", runner=runner)
    assert result["exit_code"] == 0
    assert result["execution_backend"] == "contained-native-git-readonly"
    assert calls[0]["inspection"] is True
    assert calls[0]["argv"] == ["log", "--graph", "--oneline", "--decorate", "-8"]
    assert list(tmp_path.iterdir()) == []
