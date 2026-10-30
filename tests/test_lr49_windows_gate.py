"""Host-independent Windows admission tests; no Windows shell/helper is launched.

Explicit dialect and MXC selection exercise production gate wiring on any host.
This is source/admission evidence, not native Windows/MXC qualification.
"""

from types import SimpleNamespace

import pytest
import test_lr49_github_credential_gate as owner
from test_approval_review import _Surface

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.config import ConfigError
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision

harness = owner.harness

POWERSHELL = [
    "git pu`\nsh origin main",
    "g`\r\nit pu`\r\nsh origin main",
    "git `\n\tpush origin main",
    "git p`\nu`\ns`\nh origin main",
    "Write-Output fixture; git push origin main",
    "Write-Output fixture && git push origin main",
    "Write-Output fixture || gh pr create --title fixture",
    "& git push origin main",
    '& "C:\\Program Files\\Git\\bin\\GIT.EXE" push origin main',
    '& "C:\\Program Files\\GitHub CLI\\GH.EXE" pr create --title fixture',
    "Invoke-Expression 'git push origin main'",
    "iEx 'gh pr create --title fixture'",
    "Start-Process git -ArgumentList push,origin,main",
    "saps git -ArgumentList 'push origin main'",
    "start git -ArgumentList push,origin,main",
    "cmd /d /c 'git push origin main'",
    "Set-Alias publish git; publish push origin main",
    "sal publish git; publish push origin main",
    "publish origin main",  # Unknown pre-existing alias/function.
    "Get-ChildItem",  # Even a familiar name can be shadowed by an alias/function.
    "& $env:LR49_PROGRAM $env:LR49_VERB origin main",
    "git $env:LR49_VERB origin main",
    "git --% %LR49_VERB% origin main",
    "git.exe -c credential.helper=manager ls-remote https://github.com/owner/a.git",
    "GIT.EXE push origin main",
    "GH.EXE pr create --title fixture",
    "# '\ngit push origin main\n# '",
]
CMD = [
    "git pu^\nsh origin main",
    "g^\r\nit pu^\r\nsh origin main",
    "git ^\r\n\tpush origin main",
    "git p^\nu^\ns^\nh origin main",
    "echo fixture & git push origin main",
    "echo fixture && git push origin main",
    "echo fixture || gh pr create --title fixture",
    '"C:\\Program Files\\Git\\bin\\git.exe" push origin main',
    '"C:\\Program Files\\GitHub CLI\\GH.EXE" pr create --title fixture',
    "C:\\Tools\\GIT.EXE push origin main",
    "C:\\Tools\\Gh.ExE pr create --title fixture",
    "GIT.EXE push origin main",
    "GH.EXE pr create --title fixture",
    "git %LR49_VERB% origin main",
    "%LR49_PROGRAM% push origin main",
    '"%LR49_PROGRAM%" %LR49_VERB% origin main',
    "git !LR49_VERB! origin main",
    'cmd /v:on /c "git !LR49_VERB! origin main"',
    'cmd /d /c "git push origin main"',
    "call git push origin main",
    'start "" git push origin main',
    "if exist fixture git push origin main",
    "for %i in (fixture) do git push origin main",
    "git publish origin main",  # Git alias/config is not a proven read.
    "gh publish",  # gh alias/config is not a proven read.
    "publish.cmd origin main",
    "git.exe -c credential.helper=manager ls-remote https://github.com/owner/a.git",
    "git ls-remote https://github.com/owner/a.git",
]
CASES = [("powershell", c) for c in POWERSHELL] + [("cmd", c) for c in CMD]


def windows_backend(monkeypatch, dialect):
    monkeypatch.setattr("alysis_code.command_review_rules.host_dialect", lambda: dialect)
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.resolve_shell_sandbox_settings",
        lambda _: ShellSandboxSettings(backend="windows-mxc"),
    )
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="windows-mxc"),
    )
    # Any accidental saved-account lookup fails before touching a credential.
    monkeypatch.setattr(
        "alysis_code.git_github_auth.load_credential",
        lambda: pytest.fail("credential lookup must never run in an admission test"),
    )


@pytest.mark.parametrize("dialect,command", CASES)
def test_windows_classifier_requires_person_for_writes_or_unresolved_effects(dialect, command):
    assert github_write_reason(command, dialect=dialect)


@pytest.mark.parametrize("dialect,command", CASES)
@pytest.mark.parametrize("mode", ["auto", "review", "fullaccess"])
def test_windows_actual_gate_precedes_yes_full_and_network(
    harness, monkeypatch, dialect, command, mode
):
    build, dispatched, client, _ = harness
    windows_backend(monkeypatch, dialect)
    surface = _Surface(allow=True)
    tool = build(surface, mode, True)["shell_run"]
    # Retained networking is not a remote-write decision.
    if mode != "fullaccess":
        tool.run({"cmd": "echo fixture", "permissions": {"network": True, "scope": "turn"}})
    dispatched.clear()
    client.calls.clear()
    surface.requests.clear()
    surface.allow = False
    with pytest.raises(ApprovalDeclinedError):
        tool.run({"cmd": command})
    assert not dispatched and not client.calls
    assert len(surface.requests) == 1
    request = surface.requests[0]
    assert request.command == command
    assert request.metadata["always_ask_person"]
    assert request.metadata["mandatory_explicit_approval"]
    assert request.metadata["allow_for_session_disabled"]
    assert request.allow_for_session_scope is None


@pytest.mark.parametrize("helper", ["manager", "manager-core", "wincred"])
@pytest.mark.parametrize("mode", ["auto", "review"])
@pytest.mark.parametrize("decision", ["deny_auth", "deny_write", "allow_both"])
def test_windows_github_auth_and_configured_helper_have_two_operation_cards(
    harness, monkeypatch, helper, mode, decision
):
    build, dispatched, client, root = harness
    windows_backend(monkeypatch, "cmd")
    config = root / ".git" / "config"
    config.parent.mkdir(exist_ok=True)
    config.write_text("[credential]\n\thelper = " + helper + "\n")
    command = "git ls-remote https://github.com/owner/a.git"

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.kind == "sandbox_access":
                # Changing config during the credential card cannot create a read exemption.
                config.write_text(
                    "[credential]\n\thelper = "
                    + helper
                    + '\n[includeIf "gitdir:./"]\n\tpath = fixture-config\n'
                )
                return ApprovalDecision(decision != "deny_auth")
            return ApprovalDecision(decision == "allow_both")

    surface = Surface()
    tool = build(surface, mode, True)["shell_run"]
    for _ in range(2):
        surface.requests.clear()
        dispatched.clear()
        args = {"cmd": command, "permissions": {"github_auth": True, "network": True}}
        if decision == "allow_both":
            tool.run(args)
            assert dispatched == [command]
        else:
            with pytest.raises(ApprovalDeclinedError):
                tool.run(args)
            assert not dispatched
        assert not client.calls
        assert [r.kind for r in surface.requests] == (
            ["sandbox_access"] if decision == "deny_auth" else ["sandbox_access", "shell_run"]
        )
        for request in surface.requests:
            assert request.command == command
            assert request.metadata["always_ask_person"]
            assert request.metadata["mandatory_explicit_approval"]
            assert request.allow_for_session_scope is None


@pytest.mark.parametrize(
    "command",
    [
        "powershell -NoProfile -Command \"iex 'git push origin main'\"",
        'pwsh -Command "Start-Process git -ArgumentList push"',
        'CMD.EXE /D /C "git %LR49_VERB% origin main"',
    ],
)
def test_windows_wrappers_are_gated_from_posix_too(command):
    assert github_write_reason(command, dialect="posix")


@pytest.mark.parametrize("command", ['echo "git push"', "echo fixture"])
def test_literal_cmd_echo_is_not_classified_as_a_write(command):
    assert github_write_reason(command, dialect="cmd") is None


def test_windows_full_access_does_not_silently_accept_scoped_credentials(harness, monkeypatch):
    build, dispatched, client, _ = harness
    windows_backend(monkeypatch, "cmd")
    surface = _Surface(allow=True)
    with pytest.raises(ConfigError, match="unavailable"):
        build(surface, "fullaccess", True)["shell_run"].run(
            {
                "cmd": "git ls-remote https://github.com/owner/a.git",
                "permissions": {"github_auth": True, "network": True},
            }
        )
    assert not dispatched and not client.calls and not surface.requests


@pytest.mark.parametrize("runner_kind", ["mxc", "native", "lazy", "resolved_lazy"])
def test_production_runner_selects_powershell_without_injecting_dialect(
    harness, monkeypatch, runner_kind
):
    from alysis_code.agent.tools_assembly import build_tools as real_build_tools
    from alysis_code.sandbox_runner import LazyShellRunner
    from alysis_code.windows_mxc import WindowsMxcRunner
    from alysis_code.windows_sandbox import WindowsNativeRunner

    build, dispatched, client, _ = harness
    settings = ShellSandboxSettings(backend="windows-mxc")
    runner = (
        WindowsNativeRunner(settings) if runner_kind == "native" else WindowsMxcRunner(settings)
    )
    if "lazy" in runner_kind:
        lazy = LazyShellRunner(
            lambda: pytest.fail("must not initialize the Windows helper"), settings=settings
        )
        if runner_kind == "resolved_lazy":
            lazy._runner = runner
        runner = lazy

    def build_with_actual_runner(**kwargs):
        kwargs["shell_runner"] = runner
        return real_build_tools(**kwargs)

    # Keep the deliberately mismatched Seatbelt config from the common fixture:
    # the actual captured Windows runner must win over config and host OS.
    monkeypatch.setattr(owner, "build_tools", build_with_actual_runner)
    surface = _Surface(allow=False)
    with pytest.raises(ApprovalDeclinedError):
        build(surface, "fullaccess", True)["shell_run"].run({"cmd": "Get-ChildItem"})
    assert not dispatched and not client.calls
    assert surface.requests[-1].metadata["always_ask_person"]
    assert "PowerShell" in surface.requests[-1].reason


@pytest.mark.parametrize("backend", ["windows-mxc", "windows-native"])
def test_host_shell_dialect_is_independent_of_scoped_file_backend(harness, monkeypatch, backend):
    build, dispatched, client, _ = harness
    monkeypatch.setattr("alysis_code.shell_command_lexer.host_dialect", lambda: "cmd")
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend=backend),
    )
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.resolve_shell_sandbox_settings",
        lambda _: ShellSandboxSettings(backend=backend),
    )
    surface = _Surface(allow=False)
    # The explicit host runner uses cmd. File-tool settings do not select
    # a PowerShell interpreter for that command.
    with pytest.raises(ApprovalDeclinedError):
        build(surface, "fullaccess", True)["shell_run"].run({"cmd": "Get-ChildItem"})
    assert not dispatched and not client.calls
    assert surface.requests[-1].metadata["always_ask_person"]
    assert "PowerShell" not in surface.requests[-1].reason


@pytest.mark.parametrize("command", CMD)
def test_host_cmd_gate_with_no_powershell_backend_hint(harness, monkeypatch, command):
    build, dispatched, client, _ = harness
    monkeypatch.setattr("alysis_code.shell_command_lexer.host_dialect", lambda: "cmd")
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.resolve_shell_sandbox_settings",
        lambda _: ShellSandboxSettings(mode="off"),
    )
    surface = _Surface(allow=False)
    with pytest.raises(ApprovalDeclinedError):
        build(surface, "fullaccess", True)["shell_run"].run({"cmd": command})
    assert not dispatched and not client.calls
    assert surface.requests[-1].metadata["always_ask_person"]
    assert "PowerShell" not in surface.requests[-1].reason
