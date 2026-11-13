"""Host-side Git never runs what a workspace folder supplies.

The engine inspects the workspace with Git on the host, outside any sandbox.
The agent can write ordinary files there, so a folder must not be able to turn
that inspection into running a program.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from alysis_code.agent.turn.snapshot import _list_git_workspace_snapshot_paths
from alysis_code.git_safe import build_git_process_env

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git")


def _marker_program(tmp_path):
    marker = tmp_path / "ran-on-host"
    program = tmp_path / "program.py"
    program.write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n", encoding="utf-8"
    )
    return marker, f'"{sys.executable}" "{program}"'


def _bare_layout(folder, command):
    """A plain folder made to look like a repository, with no .git anywhere."""
    (folder / "objects").mkdir(parents=True)
    (folder / "refs" / "heads").mkdir(parents=True)
    (folder / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (folder / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
        f"\tworktree = {folder}\n\tfsmonitor = {command}\n",
        encoding="utf-8",
    )
    (folder / "a.txt").write_text("data\n", encoding="utf-8")


def _clean_environment(monkeypatch, tmp_path):
    for name in ("GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_unprotected_git_would_run_the_folder_program(tmp_path, monkeypatch):
    """The control: without the safety configuration the program does run."""
    _clean_environment(monkeypatch, tmp_path)
    marker, command = _marker_program(tmp_path)
    folder = tmp_path / "project"
    _bare_layout(folder, command)
    subprocess.run(
        ["git", "-C", str(folder), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=False,
    )
    if not marker.exists():
        pytest.skip("this Git does not treat the folder as a repository")


def test_workspace_snapshot_does_not_run_a_program_from_a_plain_folder(tmp_path, monkeypatch):
    _clean_environment(monkeypatch, tmp_path)
    marker, command = _marker_program(tmp_path)
    folder = tmp_path / "project"
    _bare_layout(folder, command)
    assert _list_git_workspace_snapshot_paths(folder) is None  # not a repository
    assert not marker.exists()


def test_repository_monitor_program_is_not_started(tmp_path, monkeypatch):
    _clean_environment(monkeypatch, tmp_path)
    marker, command = _marker_program(tmp_path)
    folder = tmp_path / "repo"
    folder.mkdir()
    subprocess.run(["git", "init", "-q", str(folder)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(folder), "config", "core.fsmonitor", command],
        check=True,
        capture_output=True,
    )
    (folder / "a.txt").write_text("data\n", encoding="utf-8")
    assert _list_git_workspace_snapshot_paths(folder) == {"a.txt"}
    assert not marker.exists()


@pytest.mark.parametrize("arguments", [["status", "--porcelain"], ["diff"], ["rev-parse", "HEAD"]])
def test_hardened_environment_covers_other_inspection_commands(tmp_path, monkeypatch, arguments):
    _clean_environment(monkeypatch, tmp_path)
    marker, command = _marker_program(tmp_path)
    folder = tmp_path / "project"
    _bare_layout(folder, command)
    subprocess.run(
        ["git", "-C", str(folder), *arguments],
        capture_output=True,
        check=False,
        env=build_git_process_env(),
    )
    assert not marker.exists()


def test_safety_configuration_is_appended_once_and_wins():
    env = build_git_process_env(
        {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_CONFIG_KEY_1": "user.name",
            "GIT_CONFIG_VALUE_1": "fixture",
        }
    )
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("user.name", "fixture")
    pairs = {(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in (2, 3)}
    assert pairs == {("safe.bareRepository", "explicit"), ("core.fsmonitor", "false")}
    assert build_git_process_env(env) == env  # idempotent
    assert build_git_process_env({"GIT_CONFIG_COUNT": "not-a-number"})["GIT_CONFIG_COUNT"] == "2"


def test_explicitly_named_git_directory_still_works(tmp_path, monkeypatch):
    """The engine's own scratch and checkpoint repositories name their directory."""
    _clean_environment(monkeypatch, tmp_path)
    store = tmp_path / "objects.git"
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("data\n", encoding="utf-8")
    env = build_git_process_env()
    subprocess.run(["git", "init", "--bare", "-q", str(store)], check=True, env=env)
    listed = subprocess.run(
        ["git", f"--git-dir={store}", f"--work-tree={work}", "ls-files", "--others"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert listed.stdout.split() == ["a.txt"]


# --- a repository kept outside any .git folder ------------------------------------


def _separate_repository(tmp_path, monkeypatch):
    """A workspace whose .git is a file pointing to a folder with another name."""
    _clean_environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(workspace / "meta"), str(workspace)],
        check=True,
        capture_output=True,
    )
    assert (workspace / ".git").is_file() and (workspace / "meta" / "config").is_file()
    return workspace


def test_git_directory_named_by_a_git_file_is_found(tmp_path, monkeypatch):
    from alysis_code.git_safe import separate_git_directories

    workspace = _separate_repository(tmp_path, monkeypatch)
    assert separate_git_directories(workspace) == ((workspace / "meta").resolve(),)


def test_ordinary_layouts_need_no_extra_protection(tmp_path, monkeypatch):
    from alysis_code.git_safe import separate_git_directories

    _clean_environment(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert separate_git_directories(plain) == ()

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    assert separate_git_directories(repo) == ()

    # A linked worktree keeps its Git directory under the main repository's .git.
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=f", "-c", "user.email=f@example.invalid"]
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", "first"], check=True, capture_output=True)
    linked = tmp_path / "linked"
    subprocess.run([*git, "worktree", "add", "-q", str(linked)], check=True, capture_output=True)
    assert (linked / ".git").is_file()
    assert separate_git_directories(linked) == ()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not a pointer\n",
        "gitdir:\n",
        "gitdir: missing-folder\n",
        "gitdir: ../outside\x00\n",
        # The workspace itself or a folder containing it is never protected.
        "gitdir: .\n",
        "gitdir: ..\n",
        "gitdir: /\n",
    ],
)
def test_unusable_git_file_is_ignored(tmp_path, content):
    from alysis_code.git_safe import separate_git_directories

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_text(content, encoding="utf-8")
    assert separate_git_directories(workspace) == ()


def test_file_tools_cannot_write_a_git_directory_with_another_name(tmp_path, monkeypatch):
    import io

    from rich.console import Console
    from test_approval_review import _store, _Surface

    from alysis_code.agent.errors import AgentRuntimeError
    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.runtime_kind import RuntimeKind

    workspace = _separate_repository(tmp_path, monkeypatch)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    tools = build_tools(
        root=workspace,
        console=Console(file=io.StringIO()),
        surface=_Surface(allow=False),
        store=_store(workspace),
        mode="auto",
        yes=True,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    before = (workspace / "meta" / "config").read_text(encoding="utf-8")
    for path in ("meta/config", "meta/hooks/pre-push", "meta/hooks/post-index-change"):
        with pytest.raises(AgentRuntimeError, match="protected path"):
            tools["fs_write"].run({"path": path, "content": "#!/bin/sh\n"})
    with pytest.raises(AgentRuntimeError, match="protected path"):
        tools["fs_mkdir"].run({"path": "meta/hooks/extra"})
    assert (workspace / "meta" / "config").read_text(encoding="utf-8") == before
    assert not (workspace / "meta" / "hooks" / "pre-push").exists()
    # Ordinary files in the workspace are unaffected.
    tools["fs_write"].run({"path": "notes.txt", "content": "ok\n"})
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "ok\n"
