"""A command with no route to a remote follows the mode instead of a mandatory person card.

The mandatory person decision for unresolved command effects exists so that a
remote write is never implicit. A command in the default macOS Seatbelt sandbox
has no network, no host sockets and no credentials, so it cannot perform one.
It cannot stage one for later either: a literal Git command that changes the
repository writes objects, refs and the index, never the configuration, hooks
or other entries that decide what a later Git runs, and every other command
has all of ``.git`` write-protected. Every widening keeps the person decision,
and so does a remote write the command states.

Dispatch is a capture: no command in this file runs.
"""

from __future__ import annotations

import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from rich.console import Console
from test_approval_review import _reviewer, _Surface

from alysis_code.agent.errors import AgentRuntimeError, ApprovalDeclinedError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.command_review_rules import github_write_reason
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_runner import (
    HostShellRunner,
    LazyShellRunner,
    SeatbeltShellRunner,
    seatbelt_scope_has_no_remote_route,
)
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision

CLOSED = ShellSandboxSettings(backend="seatbelt")
_KEEP = object()

OPAQUE = ["pytest -q", "python3 -m pytest tests", "npm test", "make test", "ls -la", "wc -l a.py"]
# Literal Git commands that only inspect: metadata stays write-protected.
GIT_READS = [
    "git status",
    "git status --short && git diff --stat",
    "git diff",
    "git log --oneline -5",
    "git show --stat HEAD",
    "git branch --show-current",
    "git rev-parse --abbrev-ref HEAD",
    "git -C sub status",
    "git config --get user.name",
]
# Literal Git commands that change the repository. The runner gives them the
# metadata without what a later Git would run, so the attempts among them to
# write configuration or a hook are refused by the sandbox, not by a person.
GIT_CHANGES = [
    "git commit -m x",
    "git status && git commit -m x",
    "git add -A && git commit -m x",
    "git checkout -b feature",
    "git switch -c feature",
    "git branch -D old",
    "git stash",
    "git reset --soft HEAD~1",
    "git stash apply stash@{0}",
    "git init",
    "git log --output=.git/hooks/pre-push --format=x",
    "git config core.fsmonitor ./x.sh",
    "git -c alias.x=!sh x",
]
# A wrapped or mixed command is not a literal Git command: all of .git stays protected.
MIXED_GIT = ["sh -c 'git config core.hooksPath .hooks'", "echo ok && git status"]
GIT_COMMANDS = GIT_CHANGES + MIXED_GIT
STATED_REMOTE_WRITES = [
    "git push origin main",
    "git -C child push origin HEAD:main",
    "git add -A && git commit -m x && git push",
    "git -c user.name=x push origin main",
    "gh pr create --title fixture",
    "gh api repos/owner/a/issues -X POST",
]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.setenv("ALYSIS_TERMINAL_OWNERSHIP_DIR", str(tmp_path / "terminal"))
    monkeypatch.setattr("alysis_code.agent.tools_assembly.sys.platform", "darwin")
    monkeypatch.setattr("alysis_code.command_review_rules.host_dialect", lambda: "posix")
    monkeypatch.setattr("alysis_code.shell_command_lexer.host_dialect", lambda: "posix")
    state = {"settings": CLOSED, "backend": "seatbelt"}
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.resolve_shell_sandbox_settings",
        lambda _: state["settings"],
    )
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend=state["backend"], requires_command_approval=False),
    )
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.build_shell_runner_from_settings",
        lambda settings, *a, **k: _runner_for(settings),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        "alysis_code.agent_loop.shell_run",
        lambda **kw: dispatched.append(kw["cmd"]) or {"exit_code": 0, "stdout": "", "stderr": ""},
    )
    reviewer, client, store = _reviewer(root, [], user_message="Run the checks")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()

    def run(
        command,
        *,
        mode="auto",
        permissions=None,
        runner=None,
        settings=None,
        backend=_KEEP,
        yes=True,
        non_interactive=False,
    ):
        if settings is not None:
            state["settings"] = settings
        if backend is not _KEEP:
            state["backend"] = backend
        tool = build_tools(
            root=root,
            console=Console(file=io.StringIO()),
            # An unattended run has no host to ask on its behalf.
            surface=None if non_interactive else surface,
            store=store,
            mode=mode,
            yes=yes,
            shell_runner=runner if runner is not None else _runner_for(state["settings"]),
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            approval_reviewer=reviewer,
            non_interactive=non_interactive,
        )["shell_run"]
        params = {"cmd": command}
        if permissions is not None:
            params["permissions"] = permissions
        try:
            tool.run(params)
        except (ApprovalDeclinedError, AgentRuntimeError) as caught:
            return caught
        return None

    return SimpleNamespace(run=run, surface=surface, dispatched=dispatched, client=client)


def _runner_for(settings: ShellSandboxSettings) -> SeatbeltShellRunner:
    return SeatbeltShellRunner(
        network=settings.network,
        network_domains=settings.network_domains,
        private_network_endpoints=settings.private_network_endpoints,
        unix_sockets=settings.unix_sockets,
        additional_read_files=settings.additional_read_files,
        forward_env=settings.forward_env,
        ssh_agent_socket=settings.ssh_agent_socket,
        git_metadata_access=settings.git_metadata_access,
        github_auth=settings.github_auth,
        preview_access=settings.preview_access,
        clear_env=settings.clear_env,
        protect_repo_meta=settings.protect_repo_meta,
        additional_workspace_roots=settings.additional_workspace_roots,
        additional_read_roots=settings.additional_read_roots,
    )


def _person_cards(surface):
    return [r for r in surface.requests if r.kind == "shell_run"]


@pytest.mark.parametrize("command", OPAQUE + GIT_READS + GIT_COMMANDS)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_contained_command_runs_without_a_mandatory_person_card(harness, command, mode):
    assert harness.run(command, mode=mode) is None
    assert harness.dispatched == [command]
    assert not _person_cards(harness.surface)
    assert not harness.client.calls


@pytest.mark.parametrize("command", STATED_REMOTE_WRITES)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_stated_remote_write_still_needs_a_person_when_contained(harness, command, mode):
    assert isinstance(harness.run(command, mode=mode), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert card.command == command and card.metadata["always_ask_person"]


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard HEAD~1",
        "git branch -D old",
        "git clean -fd",
        "git gc --prune=now",
        "rm -rf build dist",
        "cd web && rm -rf node_modules",
    ],
)
def test_git_command_that_throws_work_away_goes_to_the_reviewer(harness, command):
    """Without --yes, "Approve for me" asks its reviewer, never a person, and runs on its yes."""
    assert harness.run(command, yes=False) is None
    assert harness.dispatched == [command]
    assert len(harness.client.calls) == 1
    assert not _person_cards(harness.surface)


@pytest.mark.parametrize("command", ["rm -rf /", "rm -rf ~", "rm -rf $HOME/", "rm -rf /usr"])
@pytest.mark.parametrize("yes", [False, True])
def test_removing_a_root_or_home_folder_is_refused_and_nobody_is_asked(harness, command, yes):
    refusal = harness.run(command, yes=yes)
    # Refused by the shell policy, or by the earlier list that holds in every mode.
    assert isinstance(refusal, AgentRuntimeError) and str(refusal).startswith("Blocked")
    assert not harness.dispatched and not harness.client.calls
    assert not _person_cards(harness.surface)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build dist",
        'rm -rf "$BUILD_DIR"',
        "cd .. && rm -rf project",
        "cd build; rm -rf *",
        "FORCE=1 rm -rf *",
        'for d in a b; do rm -rf "$d"; done',
        "ls -A | xargs rm -rf",
        "rm -{r,f} build",
    ],
)
def test_removing_a_folder_is_decided_even_under_yes(harness, command):
    """--yes answers the ordinary confirmations, never this one."""
    assert harness.run(command) is None
    assert harness.dispatched == [command]
    assert len(harness.client.calls) == 1  # the reviewer decided
    assert not _person_cards(harness.surface)


@pytest.mark.parametrize("yes", [False, True])
def test_removing_a_folder_is_refused_where_nobody_can_decide(harness, yes):
    refusal = harness.run("rm -rf build dist", yes=yes, non_interactive=True)
    assert isinstance(refusal, AgentRuntimeError)
    assert not harness.dispatched and not harness.client.calls
    if yes:
        assert "nobody can be asked" in str(refusal) and "--yes" not in str(refusal)


def test_removing_a_folder_cannot_be_allowed_for_the_session(harness):
    """What the folder holds changes; the person is told why and asked each time."""
    assert isinstance(harness.run("rm -rf build", mode="review"), ApprovalDeclinedError)
    (card,) = _person_cards(harness.surface)
    assert card.allow_for_session_scope is None
    assert "removes a folder" in card.reason
    # The reviewer may still answer it in "Approve for me": nothing forces a person.
    assert not card.metadata.get("always_ask_person")


def test_removing_a_folder_is_decided_again_the_next_time(harness):
    assert harness.run("rm -rf build") is None
    assert harness.run("rm -rf build") is None
    assert harness.dispatched == ["rm -rf build"] * 2
    assert len(harness.client.calls) == 2 and not _person_cards(harness.surface)


def test_a_check_may_not_remove_a_folder(verify_harness):
    refusal = verify_harness.run("rm -rf build")
    assert isinstance(refusal, AgentRuntimeError) and "Blocked command" in str(refusal)
    assert not verify_harness.ran


@pytest.mark.parametrize("command", ["git commit -m x", "git add -A && git commit -m x"])
def test_ordinary_git_change_asks_nobody_in_approve_for_me(harness, command):
    assert harness.run(command, yes=False) is None
    assert harness.dispatched == [command]
    assert not harness.client.calls and not _person_cards(harness.surface)


def test_contained_command_in_review_mode_gets_the_ordinary_card(harness):
    assert isinstance(harness.run("pytest -q", mode="review"), ApprovalDeclinedError)
    (card,) = _person_cards(harness.surface)
    assert "review mode" in card.reason
    assert not card.metadata.get("always_ask_person")
    assert card.allow_for_session_scope is not None


@pytest.mark.parametrize(
    "permissions",
    [
        {"network": True},
        {"network": True, "git_metadata": True},
        {"git_metadata": True},
        {"local_preview": True},
    ],
)
def test_any_granted_widening_keeps_the_person_decision(harness, permissions):
    # Full access cannot request scoped access, so this is "Approve for me" only.
    assert isinstance(harness.run("pytest -q", permissions=permissions), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert card.command == "pytest -q" and card.metadata["always_ask_person"]


@pytest.mark.parametrize(
    "settings",
    [
        replace(CLOSED, network="on"),
        replace(CLOSED, mode="warn"),
        replace(CLOSED, mode="off"),
        replace(CLOSED, preview_access="local"),
        replace(CLOSED, protect_repo_meta=False),
        replace(CLOSED, clear_env=False),
    ],
)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_launcher_settings_that_open_a_route_keep_the_person_decision(harness, settings, mode):
    assert isinstance(harness.run("pytest -q", mode=mode, settings=settings), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert card.metadata["always_ask_person"]


@pytest.mark.parametrize(
    "backend", ["host", "bwrap", "docker", "windows-mxc", "windows-native", None]
)
def test_only_seatbelt_is_claimed_as_contained(harness, backend):
    assert isinstance(harness.run("pytest -q", backend=backend), ApprovalDeclinedError)
    assert not harness.dispatched


@pytest.mark.parametrize(
    "runner",
    [
        HostShellRunner(),
        SeatbeltShellRunner(network="on"),
        SeatbeltShellRunner(preview_access="local"),
        SeatbeltShellRunner(git_metadata_access=True),
        SeatbeltShellRunner(unix_sockets=("/tmp/agent.sock",)),
        LazyShellRunner(lambda: HostShellRunner()),
        LazyShellRunner(lambda: HostShellRunner(), settings=replace(CLOSED, network="on")),
    ],
)
def test_the_real_runner_decides_not_the_configuration(harness, runner):
    # Closed settings never certify a runner that is open or runs on the host.
    assert isinstance(harness.run("pytest -q", runner=runner), ApprovalDeclinedError)
    assert not harness.dispatched


def test_unresolved_lazy_runner_is_judged_by_its_captured_settings(harness):
    runner = LazyShellRunner(lambda: SeatbeltShellRunner(), settings=CLOSED)
    # Full access asks nothing else of the runner, so it shows the guard alone.
    assert harness.run("pytest -q", mode="fullaccess", runner=runner) is None
    assert harness.dispatched == ["pytest -q"]
    assert runner.resolved_runner is None  # the guard never initialises a helper


@pytest.mark.parametrize("command", OPAQUE + ["echo $HOME", "sh -c 'echo x'"])
def test_reason_without_a_route_drops_only_uncertainty(command):
    assert github_write_reason(command, dialect="posix")
    assert github_write_reason(command, dialect="posix", remote_unreachable=True) is None


@pytest.mark.parametrize("command", STATED_REMOTE_WRITES)
def test_reason_without_a_route_keeps_stated_remote_writes(command):
    assert github_write_reason(command, dialect="posix", remote_unreachable=True) == (
        github_write_reason(command, dialect="posix")
    )


@pytest.mark.parametrize("command", GIT_READS + GIT_COMMANDS)
def test_git_keeps_its_reason_whenever_a_route_exists(command):
    # With a network, a credential or an unlocked configuration, what Git's
    # configuration makes it run is not contained, so every Git command asks.
    assert github_write_reason(command, dialect="posix")
    assert github_write_reason(command, dialect="posix", remote_unreachable=True) is None


@pytest.mark.parametrize("dialect", ["powershell", "cmd"])
def test_windows_dialects_are_never_treated_as_unreachable(dialect):
    # Windows containment is not claimed here, so its reasons are never dropped.
    assert github_write_reason("pytest -q", dialect=dialect, remote_unreachable=True)


def test_credentials_always_keep_the_reason():
    assert github_write_reason(
        "pytest -q", dialect="posix", credential_access=True, remote_unreachable=True
    )


@pytest.mark.parametrize(
    "change",
    [
        {"network": "on"},
        {"network_domains": ("example.test",)},
        {"network_domains": ()},
        {"private_network_endpoints": ("10.0.0.1:22",)},
        {"unix_sockets": ("/tmp/agent.sock",)},
        {"additional_read_files": ("/tmp/key",)},
        {"forward_env": ("TOKEN",)},
        {"ssh_agent_socket": "/tmp/agent.sock"},
        {"git_metadata_access": True},
        {"github_auth": True},
        {"preview_access": "local"},
        {"preview_access": "lan"},
        {"clear_env": False},
        {"protect_repo_meta": False},
    ],
)
def test_every_widening_field_opens_the_scope(change):
    assert seatbelt_scope_has_no_remote_route(CLOSED)
    assert seatbelt_scope_has_no_remote_route(SeatbeltShellRunner())
    assert not seatbelt_scope_has_no_remote_route(replace(CLOSED, **change))
    assert not seatbelt_scope_has_no_remote_route(replace(SeatbeltShellRunner(), **change))


def test_scope_check_fails_closed_on_unknown_objects():
    assert not seatbelt_scope_has_no_remote_route(object())
    assert not seatbelt_scope_has_no_remote_route(None)
    assert not seatbelt_scope_has_no_remote_route(HostShellRunner())


@pytest.fixture
def verify_harness(tmp_path, monkeypatch):
    """verify_run through the real guard, with the verification runner captured."""
    import subprocess

    from alysis_code.config import AppConfig

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.setenv("ALYSIS_TERMINAL_OWNERSHIP_DIR", str(tmp_path / "terminal"))
    monkeypatch.setattr("alysis_code.command_review_rules.host_dialect", lambda: "posix")
    monkeypatch.setattr("alysis_code.shell_command_lexer.host_dialect", lambda: "posix")
    state = {"settings": CLOSED}
    choice = SimpleNamespace(backend="seatbelt", requires_command_approval=False)
    for target in ("alysis_code.agent.tools_assembly", "alysis_code.verify_gate"):
        monkeypatch.setattr(f"{target}.resolve_shell_sandbox_settings", lambda _: state["settings"])
    for target in ("alysis_code.agent.tools_assembly", "alysis_code.sandbox_runner"):
        monkeypatch.setattr(f"{target}.select_shell_sandbox_backend", lambda _: choice)
    ran: list[str] = []

    class Runner:
        def run(self, *, root, cwd, cmd, timeout_s, cancel_check=None):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(
        "alysis_code.verify_gate.build_shell_runner_from_settings", lambda *a, **k: Runner()
    )
    reviewer, _client, store = _reviewer(root, [], user_message="Run the checks")
    surface = _Surface(allow=False)

    def run(command, *, mode="auto", settings=None):
        if settings is not None:
            state["settings"] = settings
        tool = build_tools(
            root=root,
            console=Console(file=io.StringIO()),
            surface=surface,
            store=store,
            mode=mode,
            yes=True,
            cfg=AppConfig(model="test-model"),
            shell_runner=_runner_for(state["settings"]),
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            approval_reviewer=reviewer,
        )["verify_run"]
        try:
            tool.run({"commands": [command]})
        except (ApprovalDeclinedError, AgentRuntimeError) as caught:
            return caught
        return None

    return SimpleNamespace(run=run, surface=surface, ran=ran)


@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_contained_verification_command_needs_no_person_card(verify_harness, mode):
    assert verify_harness.run("pytest -q", mode=mode) is None
    assert verify_harness.ran == ["pytest -q"]
    assert not [r for r in verify_harness.surface.requests if r.kind == "verify_run"]


@pytest.mark.parametrize(
    "settings",
    [replace(CLOSED, network="on"), replace(CLOSED, preview_access="local")],
)
def test_verification_command_with_a_route_keeps_the_person_decision(verify_harness, settings):
    assert isinstance(verify_harness.run("pytest -q", settings=settings), ApprovalDeclinedError)
    assert not verify_harness.ran
    (card,) = [r for r in verify_harness.surface.requests if r.kind == "verify_run"]
    assert card.metadata["always_ask_person"]


def test_stated_remote_write_in_verification_still_needs_a_person(verify_harness):
    assert isinstance(verify_harness.run("git push origin main"), ApprovalDeclinedError)
    assert not verify_harness.ran


_WIDENINGS = [
    {"network": "on"},
    {"network_domains": ("example.test",)},
    {"private_network_endpoints": ("10.0.0.1:22",)},
    {"unix_sockets": ("/tmp/agent.sock",)},
    {"additional_read_files": ("/tmp/key",)},
    {"forward_env": ("TOKEN",)},
    {"ssh_agent_socket": "/tmp/agent.sock"},
    {"github_auth": True, "network": "on"},
    {"preview_access": "local"},
    {"clear_env": False},
]


@pytest.mark.parametrize("change", [{}, *_WIDENINGS])
def test_runner_locks_git_exactly_when_the_guard_may_skip_the_person(monkeypatch, tmp_path, change):
    """The guard skips the person when the scope has no route to a remote. The
    runner asks the same question of itself before it decides whether a Git
    command may change what a later Git runs, so the two cannot disagree."""
    from contextlib import contextmanager

    seen: list[bool] = []

    @contextmanager
    def capture(**kwargs):
        seen.append(kwargs["git_programs_protected"])
        raise _Stop
        yield

    class _Stop(Exception):
        pass

    monkeypatch.setattr("alysis_code.sandbox_runner.seatbelt_execution", capture)
    monkeypatch.setattr("alysis_code.macos_github_auth.validate_command", lambda _: None)
    runner = replace(SeatbeltShellRunner(), **change)
    with pytest.raises(_Stop):
        runner.run(root=tmp_path, cwd=tmp_path, cmd="git commit -m x", timeout_s=5)
    assert seen == [seatbelt_scope_has_no_remote_route(runner)] == [not change]


def test_verification_git_write_follows_the_mode(verify_harness):
    assert verify_harness.run("git commit -m x") is None
    assert verify_harness.ran == ["git commit -m x"]
    assert not [r for r in verify_harness.surface.requests if r.kind == "verify_run"]


def test_verification_git_read_needs_no_person_card(verify_harness):
    assert verify_harness.run("git status --short") is None
    assert verify_harness.ran == ["git status --short"]


@pytest.mark.parametrize("command", GIT_READS)
def test_git_reads_keep_the_metadata_protected_and_writes_get_it(command):
    from alysis_code.macos_git import git_metadata_writes, literal_git_process

    assert literal_git_process(command) is not None
    assert not git_metadata_writes(command)


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        # Git's own revision syntax, which no shell rewrites.
        ("git reset --soft HEAD~1", ["reset", "--soft", "HEAD~1"]),
        ("git diff main~2 HEAD^", ["diff", "main~2", "HEAD^"]),
        ("git stash apply stash@{0}", ["stash", "apply", "stash@{0}"]),
        ("git log @{u}..HEAD", ["log", "@{u}..HEAD"]),
        ("git show HEAD@{1.day.ago}", ["show", "HEAD@{1.day.ago}"]),
        ("git checkout @{-1}", ["checkout", "@{-1}"]),
        ("git commit -m 'about ~ and {a,b}'", ["commit", "-m", "about ~ and {a,b}"]),
    ],
)
def test_revision_syntax_is_a_literal_git_command(command, argv):
    from alysis_code.macos_git import GIT_EXECUTABLE, literal_git_argv

    assert literal_git_argv(command) == [GIT_EXECUTABLE, *argv]


@pytest.mark.parametrize(
    "command",
    [
        # What a shell does rewrite stays an ordinary shell command.
        "git add ~/notes.txt",
        "git add ~",
        "git -c core.editor=~/bin/ed commit",
        "git log --format=a:~",
        "git add {a,b}.py",
        "git add file{1..3}.py",
        "git add src/{a,b}/x.py",
        "git stash apply stash@{0",
        "git stash apply stash@{$n}",
        "git log { HEAD }",
        "{ git status; }",
        "git add *.py",
    ],
)
def test_shell_expansion_is_never_a_literal_git_command(command):
    from alysis_code.macos_git import literal_git_process

    assert literal_git_process(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "git init",
        "git init -q",
        "git init -b main",
        "git init --initial-branch=main app",
        "git -C app init -q",
    ],
)
def test_plain_init_creates_a_repository(command):
    from alysis_code.macos_git import git_creates_repository

    assert git_creates_repository(command)


@pytest.mark.parametrize(
    "command",
    [
        # Anything that lets the command choose configuration or hooks is not plain.
        "git init --template=evil",
        "git init --template evil",
        "git -c init.templateDir=evil init",
        "git init --separate-git-dir=meta",
        "git init --bare",
        "git init a b",
        "git init && git config core.fsmonitor ./x.sh",
        "git -c alias.init=!sh init",
        "git --exec-path=. init",
        "git clone ./a b",
        "sh -c 'git init'",
    ],
)
def test_init_with_anything_chosen_by_the_command_is_not_plain(command):
    from alysis_code.macos_git import git_creates_repository

    assert not git_creates_repository(command)


@pytest.mark.parametrize(
    "command",
    [
        "git log --output=x",
        "git diff -O order",
        "git -c alias.s=!sh s",
        "git config a.b c",
        # Git accepts unique abbreviations of long options and combined short ones.
        "git log --outp=x",
        "git grep --open-files-in-pa=cat needle",
        "git grep -lOcat needle",
        "git branch --set-upstream-t=origin/main",
        "git tag --del v1",
        "git config --unset-al core.x",
    ],
)
def test_read_looking_git_that_can_write_or_run_programs_is_not_a_read(command):
    from alysis_code.macos_git import git_metadata_writes

    assert git_metadata_writes(command)


@pytest.mark.parametrize("command", GIT_READS)
@pytest.mark.parametrize(
    "settings",
    [replace(CLOSED, network="on"), replace(CLOSED, preview_access="local")],
)
def test_git_read_with_a_route_keeps_the_person_decision(harness, command, settings):
    assert isinstance(harness.run(command, settings=settings), ApprovalDeclinedError)
    assert not harness.dispatched


def test_git_read_on_a_host_runner_keeps_the_person_decision(harness):
    assert isinstance(harness.run("git status", runner=HostShellRunner()), ApprovalDeclinedError)
    assert not harness.dispatched


@pytest.mark.parametrize(
    "command",
    [
        # Verification expands globs after the guard has judged the command, and
        # a file name can be an option: neither is the command that will run.
        "git add *.py",
        "git diff --exit-code '*'",
        "git status [a-z]*",
        "g?t status",
    ],
)
def test_verification_git_command_that_may_be_rewritten_needs_a_person(verify_harness, command):
    assert isinstance(verify_harness.run(command), ApprovalDeclinedError)
    assert not verify_harness.ran


def test_verification_glob_without_git_is_still_contained(verify_harness):
    assert verify_harness.run("pytest -q tests/test_*.py") is None
    assert len(verify_harness.ran) == 1


def test_glob_expansion_question_matches_the_expander(tmp_path):
    from alysis_code.macos_git import git_metadata_writes
    from alysis_code.verify_gate import (
        _expand_verification_command_globs,
        glob_expansion_may_yield_git,
    )

    (tmp_path / "--output=o.txt").write_text("x")
    (tmp_path / "a.py").write_text("x")
    for command in ("git diff --exit-code '*'", "git add *.py"):
        rewritten = _expand_verification_command_globs(command, root=tmp_path)
        # The rewrite is what makes these dangerous, and the guard's question says so.
        assert rewritten != command and git_metadata_writes(rewritten)
        assert glob_expansion_may_yield_git(command)
    assert not glob_expansion_may_yield_git("git status --short")
