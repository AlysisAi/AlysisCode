"""`git commit` blocked by protect_repo_meta's blanket read-only `.git`.

The protection was right, the blanket was not: it stopped the safest,
most reversible git operation with the same mechanism as history rewrites,
and users discovered it only after the work was done. The fix: a vetted
allowlist of plain `git add` / `git commit` invocations runs with repo
metadata writable for that single command; everything else keeps the
read-only mount. Bonus fixes: the bwrap backend now actually honors
`protect_repo_meta` (it was welded to the hardened profile), and the
model is told the git policy up front in the environment context.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from alysis_code.agent.prompt_context import _sandbox_git_policy_line
from alysis_code.config import AppConfig
from alysis_code.sandbox_runner import (
    BwrapShellRunner,
    _build_bwrap_argv,
    _build_docker_argv,
    is_safe_repo_meta_git_command,
)
from alysis_code.sandbox_settings import resolve_shell_sandbox_settings

SAFE_COMMANDS = [
    "git add -A",
    "git add logmerge.py tests/test_logmerge.py",
    "git add -u -- src",
    'git commit -m "fix: stable tie ordering"',
    "git commit -am wip",
    "git commit --all --message=done --no-verify",
    'git -c user.name=Apollo -c user.email=a@b.c commit -m "msg"',
    'git commit -m "multi word message" --allow-empty',
]

UNSAFE_COMMANDS = [
    "",
    "ls -la",
    "git push",
    "git push --force origin main",
    "git rebase -i HEAD~3",
    "git commit --amend -m oops",
    "git commit -m ok --amend",
    "git config user.name evil",
    "git -c core.hooksPath=/tmp/evil commit -m x",
    "git -c user.name=A push",
    'git commit -m "x" && rm -rf /',
    "git commit -m x; git push",
    "git commit -m `whoami`",
    'git commit -m "$(cat /etc/passwd)"',
    "git add -A | tee log",
    "git commit -F msg.txt",
    'git commit -m x --author="A <a@b.c>"',  # angle brackets trip the metachar guard
]


def test_safe_git_commands_are_recognized() -> None:
    for command in SAFE_COMMANDS:
        assert is_safe_repo_meta_git_command(command), command


def test_unsafe_git_commands_are_rejected() -> None:
    for command in UNSAFE_COMMANDS:
        assert not is_safe_repo_meta_git_command(command), command


def test_bare_git_commit_without_flags_is_allowed() -> None:
    # No editor exists in the sandbox, so a bare `git commit` fails on its own
    # terms; the classifier does not need to special-case it.
    assert is_safe_repo_meta_git_command("git commit")


def _bwrap_ro_binds(args: list[str]) -> list[str]:
    return [
        args[i + 2]
        for i in range(len(args) - 2)
        if args[i] == "--ro-bind" and args[i + 2].startswith("/workspace/")
    ]


def _bwrap_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> list[str]:
    # Mount-policy tests must not discover SDKs from the CI runner's host PATH.
    monkeypatch.setenv("PATH", os.defpath)
    args, _env = _build_bwrap_argv(
        root=tmp_path,
        cwd=tmp_path,
        cmd=kwargs.pop("cmd", "true"),
        network="off",
        clear_env=True,
        profile="hardened",
        unshare_cgroup=False,
        **kwargs,
    )
    return args


def test_bwrap_protects_git_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    assert "/workspace/.git" in _bwrap_ro_binds(_bwrap_args(tmp_path, monkeypatch))


def test_bwrap_safe_git_write_skips_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    args = _bwrap_args(tmp_path, monkeypatch, allow_repo_meta_writes=True)
    assert "/workspace/.git" not in _bwrap_ro_binds(args)


def test_bwrap_honors_protect_repo_meta_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Previously welded to the hardened profile; the config knob was dead.
    (tmp_path / ".git").mkdir()
    args = _bwrap_args(tmp_path, monkeypatch, protect_repo_meta=False)
    assert "/workspace/.git" not in _bwrap_ro_binds(args)


def test_docker_safe_git_write_skips_protection(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    def build(allow: bool) -> list[str]:
        args, _env = _build_docker_argv(
            root=tmp_path,
            cwd=tmp_path,
            cmd="git commit -m x",
            container_name="t",
            network="off",
            docker_image="img",
            clear_env=True,
            pids_limit=None,
            memory_limit=None,
            cpus=None,
            read_only_rootfs=False,
            protect_repo_meta=True,
            env_allowlist=(),
            allow_repo_meta_writes=allow,
        )
        return args

    assert any(":ro" in item and "/.git" in item for item in build(False))
    assert not any(":ro" in item and "/.git" in item for item in build(True))


def test_settings_parse_safe_git_writes(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_SAFE_GIT_WRITES", raising=False)
    cfg = AppConfig(model="test-model")
    assert resolve_shell_sandbox_settings(cfg).safe_git_writes is True
    cfg.extra_fields["shell_sandbox"] = {"safe_git_writes": False}
    assert resolve_shell_sandbox_settings(cfg).safe_git_writes is False
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_SAFE_GIT_WRITES", "true")
    assert resolve_shell_sandbox_settings(cfg).safe_git_writes is True


def test_git_policy_line_states_the_allowance(monkeypatch) -> None:
    from types import SimpleNamespace

    # This assertion describes the other backends' restricted Git allowance.
    # The native Mac policy permits real Git, including literal command lists.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "docker")
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="docker"),
    )
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_SAFE_GIT_WRITES", raising=False)
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META", raising=False)
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_MODE", raising=False)
    cfg = AppConfig(model="test-model")
    line = _sandbox_git_policy_line(cfg)
    assert line is not None and line.startswith("git_policy:")
    assert "git add" in line and "git commit" in line
    assert "protect_repo_meta" in line

    cfg.extra_fields["shell_sandbox"] = {"safe_git_writes": False}
    blocked_line = _sandbox_git_policy_line(cfg)
    assert blocked_line is not None and "all .git writes incl." in blocked_line

    cfg.extra_fields["shell_sandbox"] = {"protect_repo_meta": False}
    assert _sandbox_git_policy_line(cfg) is None

    cfg.extra_fields["shell_sandbox"] = {"mode": "off"}
    assert _sandbox_git_policy_line(cfg) is None


def test_git_policy_line_describes_native_mac_scope(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="seatbelt"),
    )
    cfg = AppConfig(model="test-model")
    cfg.extra_fields["shell_sandbox"] = {"safe_git_writes": True, "protect_repo_meta": True}
    line = _sandbox_git_policy_line(cfg)
    assert line and "real Git under Seatbelt" in line and "&&, || or ;" in line
    assert "github_auth" in line and "credentials" in line
    assert "network_domains" in line and "unix_sockets" in line


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_real_bwrap_commit_succeeds_and_amend_stays_blocked(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "a.txt").write_text("two\n")

    runner = BwrapShellRunner(network="off", clear_env=True, profile="hardened")
    add = runner.run(root=tmp_path, cwd=tmp_path, cmd="git add a.txt", timeout_s=30)
    assert add.returncode == 0, add.stderr
    commit = runner.run(
        root=tmp_path,
        cwd=tmp_path,
        cmd='git -c user.name=t -c user.email=t@t commit -m "sandboxed commit"',
        timeout_s=30,
    )
    assert commit.returncode == 0, commit.stderr
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    assert "sandboxed commit" in log.stdout

    amend = runner.run(
        root=tmp_path,
        cwd=tmp_path,
        cmd='git -c user.name=t -c user.email=t@t commit --amend -m "rewrite"',
        timeout_s=30,
    )
    assert amend.returncode != 0
    assert "Read-only file system" in (amend.stderr or "") + (amend.stdout or "")
