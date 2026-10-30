import os
from pathlib import Path

import pytest

from alysis_code.config import ConfigError
from alysis_code.git_native import native_argv, repository_metadata, run, safe_global_config
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.parametrize(
    "command",
    [
        "git status; git reset --hard",
        "git -c alias.x=evil x",
        "git --git-dir=elsewhere commit -m x",
        "git rebase main | powershell",
        'git commit -m "$secret"',
    ],
)
def test_shell_expressions_and_global_overrides_are_not_native_requests(command):
    assert native_argv(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "git switch main",
        "git rebase main",
        "git merge --no-ff feature",
        "git stash push -u",
        "git worktree add child main",
        "git submodule update --init",
        "git commit -S -m signed",
    ],
)
def test_literal_native_operations(command):
    assert native_argv(command)


def test_explicit_ssh_command_is_literal_and_other_transport_overrides_are_rejected():
    command = (
        'git -c \'core.sshCommand="C:/Windows/System32/OpenSSH/ssh.exe" -F "C:/keys/config"\' '
    )
    assert native_argv(command + "clone -- ssh://example/repo child")
    assert native_argv(command + "ls-remote ssh://example/repo", inspection=True)
    assert native_argv(command + "fetch origin; evil") is None
    for config in ("protocol.allow=always", "core.hooksPath=elsewhere", "core.sshCommand="):
        assert native_argv("git -c '" + config + "' fetch origin") is None


@pytest.mark.parametrize("reference", ["stash@{0}", "'stash@{0}'", '"stash@{0}"'])
def test_reflog_selectors_stay_literal_git_arguments(reference):
    assert native_argv("git stash pop " + reference) == ["stash", "pop", "stash@{0}"]
    assert native_argv("git stash pop " + reference + "; evil") is None
    assert native_argv("git stash pop " + reference + "$(evil)") is None


def test_quoted_metacharacters_remain_data_and_expansion_stays_rejected():
    assert native_argv('git commit -m "Fix #42; keep literal"') == [
        "commit",
        "-m",
        "Fix #42; keep literal",
    ]
    assert native_argv("git commit -m 'Cost $5; $(not-code)'") == [
        "commit",
        "-m",
        "Cost $5; $(not-code)",
    ]
    assert native_argv("git commit -m 'It''s literal'") == ["commit", "-m", "It's literal"]
    assert native_argv('git commit -m "Expand $secret"') is None
    assert native_argv("git commit -m 'safe'; evil") is None


def test_native_read_family_never_selects_mutations_or_shell_expressions():
    assert native_argv("git log --graph --oneline --decorate -8", inspection=True)
    assert native_argv("git show --format=raw HEAD", inspection=True)
    assert native_argv("git reset --hard", inspection=True) is None
    assert native_argv("git log; git reset --hard", inspection=True) is None
    assert native_argv("git branch --list --verbose --no-abbrev", inspection=True)
    assert native_argv("git branch -avv", inspection=True)
    assert native_argv("git branch -D topic", inspection=True) is None
    assert native_argv("git branch new-branch", inspection=True) is None
    assert native_argv("git diff --cached --stat", inspection=True)
    assert native_argv("git diff --stat", inspection=True) is None
    assert native_argv("git check-attr --all -- base.txt", inspection=True) is None
    assert native_argv("git check-ignore -v base.txt", inspection=True) is None
    assert native_argv("git describe --dirty", inspection=True) is None


def test_local_config_queries_are_strictly_readonly():
    assert native_argv("git config --local --get core.hooksPath", inspection=True)
    for command in [
        "git config --local core.hooksPath evil",
        "git config --local --unset user.name",
        "git config --file elsewhere --get user.name",
        "git config --local --get --help",
        "git config --local --get core.hooksPath; evil",
        "git config --local --get invalid",
    ]:
        assert native_argv(command, inspection=True) is None


def test_readonly_and_disabled_git_policy_refuse_before_any_preparation(tmp_path):
    for settings in (
        ShellSandboxSettings(filesystem_access="read-only"),
        ShellSandboxSettings(safe_git_writes=False),
    ):
        with pytest.raises(ConfigError, match="disabled"):
            run(
                settings=settings,
                root=tmp_path,
                cwd=tmp_path,
                argv=["reset", "--hard"],
                timeout_s=30,
                cancel_check=None,
            )
    assert list(tmp_path.iterdir()) == []


def test_linked_metadata_requires_explicit_folder_grant(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / ".git").write_text("gitdir: " + str(outside))
    with pytest.raises(ConfigError, match="outside the approved"):
        repository_metadata(work, [work])
    assert repository_metadata(work, [work, outside]) == [outside]


def test_global_preferences_do_not_forward_secrets_or_programs(tmp_path, monkeypatch):
    from alysis_code import windows_toolchain

    monkeypatch.setattr(windows_toolchain, "installed_tools", lambda: {})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".gitconfig").write_text(
        "[user]\nname = User\nemail = user@example.invalid\n[credential]\nhelper = !secret\n[http]\nextraHeader = AUTHORIZATION secret\n[core]\nhooksPath = outside\nautocrlf = true\n"
    )
    path = tmp_path / "safe"
    safe_global_config(path)
    value = path.read_text()
    assert "user@example.invalid" in value and "autocrlf = true" in value
    assert "secret" not in value and "hooksPath" not in value


@pytest.mark.skipif(os.name != "nt", reason="Windows handle identity")
def test_reservations_remove_only_exclusive_unchanged_creations(tmp_path):
    from alysis_code._windows_git_reservations import GitReservations

    existing = tmp_path / "AGENTS.md"
    existing.write_text("user instructions")
    missing = tmp_path / "CLAUDE.md"
    reservations = GitReservations()
    try:
        reservations.reserve(tmp_path, [str(existing), str(missing)])
        assert missing.exists()
    finally:
        reservations.close()
    assert existing.read_text() == "user instructions"
    assert not missing.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle identity")
def test_reservation_cleanup_preserves_a_modified_object(tmp_path):
    from alysis_code._windows_git_reservations import GitReservations

    path = tmp_path / "AGENTS.md"
    reservations = GitReservations()
    reservations.reserve(tmp_path, [str(path)])
    for pin in reservations.pins:
        pin.close()
    path.write_text("legitimate concurrent content")
    reservations.close()
    assert path.read_text() == "legitimate concurrent content"


def test_untrusted_tool_manifest_is_refused(tmp_path, monkeypatch):
    from alysis_code import windows_toolchain

    monkeypatch.setattr(windows_toolchain, "program_files", lambda: tmp_path)
    monkeypatch.setattr(windows_toolchain, "__file__", str(tmp_path / "windows_toolchain.py"))
    folder = tmp_path / "assets/windows-git"
    folder.mkdir(parents=True)
    (folder / "bundle-manifest.json").write_text('{"files": {}}')
    with pytest.raises(ValueError, match="manifest integrity"):
        windows_toolchain.installed_tools()
