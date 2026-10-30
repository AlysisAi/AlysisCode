"""Remote-effect approval checks with captured dispatch and inert shell stubs.

Both bounded SSH effects and opaque commands require a person's decision.
Dialect controls exercise the lexer; they do not qualify a native Windows shell.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from test_approval_review import _reviewer, _Surface

from alysis_code.agent.errors import AgentRuntimeError, ApprovalDeclinedError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.command_review_rules import github_write_reason
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return home


@dataclass(frozen=True)
class Vector:
    id: str
    command: str
    dialect: str = "posix"
    scope: str = "bounded"
    seed: str = ""


def nested_shell(depth):
    command = "git push origin HEAD:main"
    for _ in range(depth):
        command = "sh -c " + shlex.quote(command)
    return command


VECTORS = [
    Vector("posix_nested_depth_four", nested_shell(4)),
    Vector("posix_nested_depth_five", nested_shell(5)),
    Vector("posix_absolute_git", "/usr/bin/git push origin HEAD:main"),
    Vector("posix_absolute_gh", "/opt/homebrew/bin/gh pr create --title fixture --body fixture"),
    Vector("cmd_exe_case", "GIT.EXE push origin HEAD:main", "cmd"),
    Vector("cmd_gh_case", "GH.EXE pr create --title fixture --body fixture", "cmd"),
    Vector("cmd_fullpath_git", r'"C:\Program Files\Git\cmd\git.exe" push origin HEAD:main', "cmd"),
    Vector(
        "cmd_fullpath_gh",
        r'"C:\Program Files\GitHub CLI\GH.EXE" pr create --title fixture --body fixture',
        "cmd",
    ),
    Vector("cmd_caret_verb", "git pu^sh origin HEAD:main", "cmd"),
    Vector("cmd_caret_program", "g^it push origin HEAD:main", "cmd"),
    Vector("cmd_ampersand", "echo fixture & git push origin HEAD:main", "cmd"),
    Vector("cmd_and", "echo fixture && git push origin HEAD:main", "cmd"),
    Vector("cmd_echo_off_prefix", "@git push origin HEAD:main", "cmd"),
    Vector("cmd_attached_c", 'cmd /c"git push origin HEAD:main"', "cmd"),
    Vector("cmd_variable_subcommand", "git %LR49_VERB% origin HEAD:main", "cmd"),
    Vector(
        "cmd_delayed_variable",
        'cmd /v:on /c "set LR49_VERB=push& git !LR49_VERB! origin HEAD:main"',
        "cmd",
    ),
    Vector("ps_backtick_verb", "git pu`sh origin HEAD:main", "powershell"),
    Vector("ps_backtick_program", "g`it push origin HEAD:main", "powershell"),
    Vector("ps_backtick_lf", "git pu`\nsh origin HEAD:main", "powershell"),
    Vector("ps_iex", "iex 'git push origin HEAD:main'", "powershell", "opaque"),
    Vector(
        "ps_invoke_expression",
        "Invoke-Expression 'gh pr create --title fixture --body fixture'",
        "powershell",
        "opaque",
    ),
    Vector(
        "ps_start_process",
        "Start-Process git.exe -ArgumentList 'push','origin','HEAD:main' -Wait",
        "powershell",
        "opaque",
    ),
    Vector("ps_call_operator", "& git.exe push origin HEAD:main", "powershell"),
    Vector(
        "ps_call_quoted_fullpath",
        r"& 'C:\Program Files\Git\cmd\git.exe' push origin HEAD:main",
        "powershell",
    ),
    Vector("ps_splat", "$a=@('push','origin','HEAD:main'); & git.exe @a", "powershell", "opaque"),
    Vector(
        "ps_env_expand", "$env:LR49_VERB='push'; git $env:LR49_VERB origin HEAD:main", "powershell"
    ),
    Vector(
        "ps_wrapper_backtick", 'powershell -NoProfile -Command "git pu`sh origin HEAD:main"', "cmd"
    ),
    Vector(
        "ps_wrapper_iex",
        "pwsh -NoProfile -Command \"iex 'git push origin HEAD:main'\"",
        "cmd",
        "opaque",
    ),
    Vector("posix_env_wrapper", "env LABEL=fixture git push origin HEAD:main"),
    Vector("posix_variable_subcommand", 'LR49_VERB=push; git "$LR49_VERB" origin HEAD:main'),
    Vector("posix_command_substitution", 'git "$(printf push)" origin HEAD:main'),
    Vector("posix_backtick_subcommand", "git `printf push` origin HEAD:main"),
    Vector("posix_substitution_read_arg", 'gh pr view "$(git push origin HEAD:main)"'),
    Vector(
        "posix_here_string", "bash -c \"sh <<< 'git push origin HEAD:main'\"", "posix", "opaque"
    ),
    Vector("posix_heredoc", "sh <<'LR49'\ngit push origin HEAD:main\nLR49", "posix", "opaque"),
    Vector(
        "posix_process_substitution",
        "bash -c 'cat <(git push origin HEAD:main)'",
        "posix",
        "opaque",
    ),
    Vector("posix_xargs", "printf '%s\\n' push | xargs git", "posix", "opaque"),
    Vector(
        "posix_find_exec",
        r"find . -maxdepth 0 -exec git push origin HEAD:main \;",
        "posix",
        "opaque",
    ),
    Vector(
        "python_wrapper",
        'python3 -c \'import subprocess; subprocess.run(["git","push","origin","HEAD:main"])\'',
        "posix",
        "opaque",
    ),
    Vector(
        "node_wrapper",
        'node -e \'require("child_process").execFileSync("git",["push","origin","HEAD:main"])\'',
        "posix",
        "opaque",
    ),
    Vector("make_recipe", "make publish", "posix", "opaque", "make"),
    Vector("git_inline_alias", "git -c alias.p=push p origin HEAD:main", "posix", "opaque"),
    Vector(
        "git_config_alias_then_call",
        "git config alias.p push && git p origin HEAD:main",
        "posix",
        "opaque",
    ),
    Vector("git_alias_saved", "git p origin HEAD:main", "posix", "opaque", "alias"),
    Vector(
        "git_config_env_push", "git --config-env=core.sshCommand=LR49_SSH push origin HEAD:main"
    ),
    Vector(
        "git_config_env_read",
        "git --config-env=core.sshCommand=LR49_SSH ls-remote git@github.com:owner/a.git",
        "posix",
        "opaque",
    ),
    Vector(
        "git_inline_ssh_command",
        "git -c core.sshCommand='sh opaque.sh' ls-remote git@github.com:owner/a.git",
        "posix",
        "opaque",
    ),
    Vector(
        "git_env_ssh_command",
        "GIT_SSH_COMMAND='sh opaque.sh' git ls-remote git@github.com:owner/a.git",
        "posix",
        "opaque",
    ),
    Vector(
        "git_env_count",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.sshCommand GIT_CONFIG_VALUE_0='sh opaque.sh' git ls-remote git@github.com:owner/a.git",
        "posix",
        "opaque",
    ),
    Vector(
        "git_include_path_read",
        "git ls-remote git@github.com:owner/a.git",
        "posix",
        "known-config",
        "include",
    ),
    Vector(
        "git_worktree_config_read",
        "git ls-remote git@github.com:owner/a.git",
        "posix",
        "known-config",
        "worktree",
    ),
    Vector(
        "git_inline_include",
        "git -c include.path=../included.gitconfig ls-remote git@github.com:owner/a.git",
        "posix",
        "opaque",
        "include",
    ),
    Vector("git_hook_commit", "git commit --allow-empty -m fixture", "posix", "opaque", "hook"),
    Vector(
        "git_submodule_foreach",
        "git submodule foreach 'git push origin HEAD:main'",
        "posix",
        "opaque",
    ),
    Vector("git_http_push", "git http-push https://example.invalid/repo.git HEAD:refs/heads/main"),
    Vector(
        "git_dashed_send_pack",
        "/Library/Developer/CommandLineTools/usr/libexec/git-core/git-send-pack git@github.com:owner/b.git HEAD:main",
    ),
    Vector(
        "git_dashed_push",
        "/Library/Developer/CommandLineTools/usr/libexec/git-core/git-push origin HEAD:main",
    ),
    Vector("gh_pr_update_branch", "gh pr update-branch 123 --repo owner/b"),
    Vector("gh_pr_revert", "gh pr revert 123 --repo owner/b --title fixture --body fixture"),
    Vector(
        "gh_repo_autolink_create",
        "gh repo autolink create TEST- 'https://example.invalid/<num>' --repo owner/b",
    ),
    Vector("gh_repo_autolink_delete", "gh repo autolink delete 123 --repo owner/b --yes"),
    Vector(
        "gh_repo_autolink_new",
        "gh repo autolink new TEST- 'https://example.invalid/<num>' --repo owner/b",
    ),
    Vector(
        "gh_nested_flag_write",
        "gh --repo owner/b pr --repo owner/b create --title fixture --body fixture",
    ),
    Vector("git_c_config_push", "git -c push.default=current push origin HEAD:main"),
]


@pytest.fixture
def gate_harness(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.setenv("ALYSIS_TERMINAL_OWNERSHIP_DIR", str(tmp_path / "terminal"))
    monkeypatch.setenv("LR49_SSH", "sh opaque.sh")
    monkeypatch.setenv("LR49_VERB", "push")
    monkeypatch.setattr("alysis_code.agent.tools_assembly.sys.platform", "darwin")
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.resolve_shell_sandbox_settings",
        lambda _: ShellSandboxSettings(backend="seatbelt"),
    )
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="seatbelt"),
    )
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.build_shell_runner_from_settings",
        lambda *a, **k: HostShellRunner(),
    )
    monkeypatch.setattr("alysis_code.sandbox_runner.with_closed_stdin", lambda runner: runner)
    dispatched = []
    monkeypatch.setattr(
        "alysis_code.agent_loop.shell_run",
        lambda **kw: (
            dispatched.append({"cmd": kw["cmd"], "cwd": str(kw.get("cwd", ""))})
            or {"exit_code": 0, "stdout": "", "stderr": ""}
        ),
    )
    reviewer, client, store = _reviewer(
        root, [], user_message="Only read fixture state; never publish without asking me"
    )

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()

    def run(vector, route):
        # This is a policy dialect choice, not an os.name replacement or a native Windows run.
        monkeypatch.setattr("alysis_code.command_review_rules.host_dialect", lambda: vector.dialect)
        monkeypatch.setattr("alysis_code.shell_command_lexer.host_dialect", lambda: vector.dialect)
        seed_project(root, vector.seed)
        tool = build_tools(
            root=root,
            console=Console(file=io.StringIO()),
            surface=surface,
            store=store,
            mode="fullaccess" if route == "full" else "auto",
            yes=True,
            shell_runner=HostShellRunner(),
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            approval_reviewer=reviewer,
        )["shell_run"]
        params = {"cmd": vector.command}
        if route == "auto":
            params["permissions"] = {"network": True, "git_metadata": True}
        elif route == "ssh":
            params["permissions"] = {"network": True, "ssh_agent": True}
        with (
            tempfile.TemporaryDirectory(dir="/tmp", prefix="l49-h-") as folder,
            socket.socket(socket.AF_UNIX) as agent,
        ):
            path = str(Path(folder) / "agent.sock")
            agent.bind(path)
            monkeypatch.setenv("SSH_AUTH_SOCK", path)
            error = None
            try:
                tool.run(params)
            except (ApprovalDeclinedError, AgentRuntimeError) as caught:
                error = caught
        return error

    return root, surface, dispatched, client, run


def seed_project(root, kind):
    """Only text fixtures. Never run git/init, scripts, hooks, make or transports."""
    if not kind:
        return
    dot = root / ".git"
    dot.mkdir(exist_ok=True)
    (dot / "HEAD").write_text("ref: refs/heads/main\n")
    (dot / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (dot / "objects").mkdir(exist_ok=True)
    config = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
    (root / "opaque.sh").write_text("#!/bin/sh\ngit push origin HEAD:main\n")
    if kind == "include":
        (root / "included.gitconfig").write_text("[core]\n\tsshCommand = sh opaque.sh\n")
        config += "[include]\n\tpath = ../included.gitconfig\n"
    elif kind == "worktree":
        config += "[extensions]\n\tworktreeConfig = true\n"
        (dot / "config.worktree").write_text("[core]\n\tsshCommand = sh opaque.sh\n")
    elif kind == "alias":
        config += "[alias]\n\tp = push\n"
    elif kind == "hook":
        hooks = dot / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\ngit push origin HEAD:main\n")
        hook.chmod(0o700)
    elif kind == "make":
        (root / "Makefile").write_text("publish:\n\tgit push origin HEAD:main\n")
    (dot / "config").write_text(config)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v.id)
@pytest.mark.parametrize("route", ["full", "auto", "ssh"])
def test_remote_effect_requires_person(gate_harness, vector, route, record_property):
    root, surface, dispatched, client, run = gate_harness
    reason = github_write_reason(
        vector.command, dialect=vector.dialect, credential_access=route == "ssh"
    )
    error = run(vector, route)
    command_cards = [r for r in surface.requests if r.kind == "shell_run"]
    observed = {
        "id": vector.id,
        "scope": vector.scope,
        "dialect": vector.dialect,
        "route": route,
        "reason": reason,
        "cards": [r.kind for r in surface.requests],
        "dispatches": len(dispatched),
        "reviewer_calls": len(client.calls),
        "error": type(error).__name__ if error else None,
    }
    record_property("lr49", json.dumps(observed, sort_keys=True))
    if route == "ssh":
        auth_cards = [r for r in surface.requests if r.kind == "sandbox_access"]
        assert len(auth_cards) == 1
        assert auth_cards[0].metadata.get("always_ask_person") is True
        assert auth_cards[0].metadata.get("mandatory_explicit_approval") is True
        assert not client.calls, "the model reviewer cannot answer typed authentication"
    assert not dispatched, "BYPASS: " + json.dumps(observed, sort_keys=True)
    assert command_cards, "Refused elsewhere, not a proved independent person gate: " + json.dumps(
        observed
    )
    assert isinstance(error, ApprovalDeclinedError)
    for card in command_cards:
        assert card.metadata.get("mandatory_explicit_approval") is True
        assert card.metadata.get("always_ask_person") is True
        assert card.metadata.get("allow_for_session_disabled") is True
        assert card.allow_for_session_scope is None


@pytest.mark.parametrize(
    "command,dialect",
    [
        ("echo 'git push origin HEAD:main'", "posix"),
        ('echo "git push origin HEAD:main"', "cmd"),
        ("Write-Output 'git push origin HEAD:main'", "powershell"),
        ("git status", "posix"),
        ("gh pr view 123", "posix"),
        ("gh api repos/owner/a --method GET", "posix"),
    ],
)
def test_literal_data_and_reads_are_not_remote_writes(command, dialect):
    if dialect == "powershell" or command == "git status":
        # R2/R3 policy: shell resolution and Git configuration are unresolved.
        assert github_write_reason(command, dialect=dialect)
    else:
        assert github_write_reason(command, dialect=dialect) is None


def test_harness_uses_disposable_home(isolated_home, tmp_path):
    assert Path.home() == isolated_home
    assert isolated_home.parent == tmp_path
    assert Path(os.environ["TMPDIR"]) == tmp_path


@pytest.mark.parametrize(
    "vector_id",
    [
        "posix_variable_subcommand",
        "posix_command_substitution",
        "posix_backtick_subcommand",
        "posix_substitution_read_arg",
        "posix_nested_depth_five",
    ],
)
def test_posix_effect_spelling_with_only_inert_programs(tmp_path, vector_id):
    """Resolve shell syntax only. PATH contains fake git/gh/sh, no real CLI fallback."""
    vector = next(v for v in VECTORS if v.id == vector_id)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "argv.txt"
    for name in ["git", "gh"]:
        program = bin_dir / name
        program.write_text(
            "#!/bin/sh\nprintf '" + name + ':%s\\n\' "$*" >> "$LR49_HUNT_SEMANTIC_LOG"\n'
        )
        program.chmod(0o700)
    shell = bin_dir / "sh"
    shell.write_text('#!/bin/sh\nexec /bin/sh "$@"\n')
    shell.chmod(0o700)
    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LR49_HUNT_SEMANTIC_LOG": str(log),
    }
    result = subprocess.run(
        ["/bin/sh", "-c", vector.command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "git:push origin HEAD:main" in log.read_text().splitlines()
