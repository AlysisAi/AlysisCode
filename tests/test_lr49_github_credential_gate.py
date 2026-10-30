"""LR-49 inert controls: all command dispatch is captured, never executed."""

import io
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from test_approval_review import _reviewer, _Surface
from test_github_repository_policy import payload

from alysis_code.agent.errors import AgentRuntimeError, ApprovalDeclinedError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.mcp.github_repository_policy import RepositoryPolicy
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
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
        lambda **kw: dispatched.append(kw["cmd"]) or {"exit_code": 0, "stdout": "", "stderr": ""},
    )
    reviewer, client, store = _reviewer(root, [], user_message="Only owner/a is allowed")

    policy = RepositoryPolicy.parse(
        {
            **payload(),
            "repositories": [{"owner": "owner", "repo": "a"}],
            "workspace_repository": {"owner": "owner", "repo": "a"},
        }
    )

    def build(surface, mode="auto", yes=False, verification=False):
        return build_tools(
            root=root,
            console=Console(file=io.StringIO()),
            surface=surface,
            store=store,
            mode=mode,
            yes=yes,
            shell_runner=HostShellRunner(),
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            approval_reviewer=reviewer,
            verification_enabled=verification,
            github_repository_policy=policy,
            github_repository_policy_required=True,
        )

    build.store = store
    return build, dispatched, client, root


def test_exploit_shape_auto_ssh_outside_plugin_allow_list_requires_person(
    harness, monkeypatch, tmp_path
):
    build, dispatched, client, _ = harness
    surface = _Surface(allow=False)
    # A local, empty Unix socket is an inert SSH-agent identity; no agent is contacted.
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="lr49-") as folder,
        socket.socket(socket.AF_UNIX) as agent,
    ):
        path = str(Path(folder).resolve() / "agent.sock")
        agent.bind(path)
        monkeypatch.setenv("SSH_AUTH_SOCK", path)
        tool = build(surface)["shell_run"]
        with pytest.raises(ApprovalDeclinedError):
            tool.run(
                {
                    "cmd": "git push git@github.com:owner/b.git HEAD:main",
                    "permissions": {"network": True, "ssh_agent": True},
                }
            )
    assert [r.kind for r in surface.requests] == ["sandbox_access"]
    assert not dispatched and not client.calls


WRITES = [
    "git push git@github.com:owner/b.git HEAD:main",
    "git -C . push --force origin main",
    "git push --force-with-lease origin +HEAD:feature",
    "git send-pack git@github.com:owner/b.git main",
    "env LABEL=fixture git push origin main",
    "command git push origin main",
    "bash -lc 'git push origin main'",
    'cmd /c "git push origin main"',
    'powershell -Command "git push origin main"',
    "gh pr create --title fixture",
    "gh pr review 1 --approve",
    "gh issue comment 1 --body fixture",
    "gh release create v1",
    "gh workflow run test.yml",
    "gh api repos/owner/b/issues -X POST",
    "gh api repos/owner/b/issues --method=PATCH",
    "gh api repos/owner/b/issues -XDELETE",
    "gh api repos/owner/b/issues -f title=fixture",
    "gh api repos/owner/b/issues -F title=fixture",
    "gh api repos/owner/b/issues --input body.json",
    "gh api repos/owner/b/issues -ftitle=fixture",
    "gh api graphql -f 'query=mutation { addComment(input: {}) { clientMutationId } }'",
]
READS = [
    'echo "git push"',
    "gh api repos/owner/a",
    "gh api repos/owner/a -X GET",
    "gh api repos/owner/a --method=GET -f per_page=10",
    "gh api graphql -f 'query={ viewer { login } }'",
    "gh pr view 1",
]


@pytest.mark.parametrize("command", WRITES)
@pytest.mark.parametrize(
    "mode,yes", [("auto", False), ("auto", True), ("review", True), ("fullaccess", True)]
)
def test_remote_write_gate_precedes_shortcuts(harness, command, mode, yes):
    build, dispatched, client, _ = harness
    surface = _Surface(allow=False)
    with pytest.raises(ApprovalDeclinedError):
        build(surface, mode, yes)["shell_run"].run({"cmd": command})
    assert not dispatched and not client.calls
    assert len(surface.requests) == 1
    request = surface.requests[0]
    assert request.metadata["always_ask_person"] and request.metadata["mandatory_explicit_approval"]
    assert (
        request.metadata["allow_for_session_disabled"] and request.allow_for_session_scope is None
    )


@pytest.mark.parametrize("command", READS)
def test_bounded_reads_and_echo_are_not_remote_writes(harness, command):
    from alysis_code.command_review_rules import github_write_reason

    build, dispatched, _, _ = harness
    assert github_write_reason(command) is None
    # Full access remains unchanged for legitimate reads; no person card.
    surface = _Surface(allow=False)
    build(surface, "fullaccess", True)["shell_run"].run({"cmd": command})
    assert dispatched == [command] and not surface.requests


@pytest.mark.parametrize("kind", ["file", "env", "github"])
@pytest.mark.parametrize("network", [False, True, "domain", "retained"])
@pytest.mark.parametrize("mode", ["auto", "review"])
def test_authentication_gate_covers_split_and_effective_access(
    harness, monkeypatch, tmp_path, kind, network, mode
):
    build, dispatched, client, _ = harness
    target = tmp_path / "opaque-input"
    target.write_text("inert fixture")
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")
    proposal = (
        {"read_files": [str(target)]}
        if kind == "file"
        else ({"environment": ["LR49_FIXTURE_INPUT"]} if kind == "env" else {"github_auth": True})
    )
    if network is True or (kind == "github" and network is not False):
        proposal["network"] = True
    if network == "domain":
        proposal["network_domains"] = ["github.com"]
    surface = _Surface(allow=True)
    tool = build(surface, mode, True)["shell_run"]
    if network == "retained":
        tool.run({"cmd": "echo network", "permissions": {"network": True, "scope": "turn"}})
        dispatched.clear()
        surface.requests.clear()
        client.calls.clear()
    surface.allow = False
    from alysis_code.config import ConfigError

    if kind == "github" and network is False:
        with pytest.raises(ConfigError, match="network"):
            tool.run(
                {"cmd": "git ls-remote https://github.com/owner/a.git", "permissions": proposal}
            )
        assert not surface.requests and not dispatched
        return
    with pytest.raises(ApprovalDeclinedError):
        tool.run({"cmd": "git ls-remote https://github.com/owner/a.git", "permissions": proposal})
    assert not dispatched and not client.calls
    (request,) = surface.requests
    assert request.metadata["mandatory_explicit_approval"] and request.metadata["always_ask_person"]
    assert "inert fixture" not in request.preview


@pytest.mark.parametrize("status", ["denied", "expired", "cancelled"])
def test_authentication_allow_cannot_answer_independent_write_card(harness, monkeypatch, status):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, client, _ = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return (
                ApprovalDecision(True)
                if request.kind == "sandbox_access"
                else ApprovalDecision(False, status=status)
            )

    surface = Surface()
    with pytest.raises((ApprovalDeclinedError, AgentRuntimeError)):
        build(surface)["shell_run"].run(
            {
                "cmd": WRITES[0],
                "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
            }
        )
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]
    assert not dispatched and not client.calls


@pytest.mark.parametrize(
    "command", ["python opaque.py", "bash opaque.sh", "bash -c '$(opaque)'", "gh alias-write"]
)
def test_opaque_credential_program_needs_independent_person(harness, monkeypatch, command):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, client, _ = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with pytest.raises(ApprovalDeclinedError):
        build(surface)["shell_run"].run(
            {"cmd": command, "permissions": {"environment": ["LR49_FIXTURE_INPUT"]}}
        )
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]
    assert not dispatched and not client.calls


def test_human_authentication_read_is_one_operation_only(harness, monkeypatch):
    build, dispatched, client, _ = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")
    surface = _Surface()
    tool = build(surface)["shell_run"]
    command = "git ls-remote https://github.com/owner/a.git"
    for _ in range(2):
        tool.run(
            {
                "cmd": command,
                "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
            }
        )
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"] * 2
    assert dispatched == [command, command] and not client.calls


@pytest.mark.parametrize("standing", ["allow", "deny"])
def test_authentication_ignores_allow_rules_and_operation_reuse_but_keeps_deny(tmp_path, standing):
    from alysis_code.agent.action_approval import decide_action_approval
    from alysis_code.agent.scoped_access import SandboxAccessApproval
    from alysis_code.surface import StandingApproval

    request = SandboxAccessApproval("sandbox_access", "auth", "{}", authentication=True)
    surface = _Surface(allow=False, standing=StandingApproval(standing, "old rule"))
    decision = decide_action_approval(
        request,
        mode="auto",
        root=tmp_path,
        surface=surface,
        can_ask_person=True,
        operation_approved=True,
        requires_authorization=False,
    )
    assert not decision.allow and len(surface.requests) == (1 if standing == "allow" else 0)


def test_authentication_without_person_cannot_dispatch(tmp_path):
    from alysis_code.agent.action_approval import decide_action_approval
    from alysis_code.agent.scoped_access import SandboxAccessApproval

    surface = _Surface()
    with pytest.raises(AgentRuntimeError, match="Nobody"):
        decide_action_approval(
            SandboxAccessApproval("sandbox_access", "auth", "{}", authentication=True),
            mode="auto",
            root=tmp_path,
            surface=surface,
            can_ask_person=False,
            operation_approved=True,
        )
    assert not surface.requests


@pytest.mark.parametrize("change", ["file", "env", "generation", "revoke"])
def test_change_during_independent_write_approval_prevents_dispatch(
    harness, monkeypatch, tmp_path, change
):
    from alysis_code.config import ConfigError
    from alysis_code.surface import ApprovalDecision

    build, dispatched, client, _ = harness
    target = tmp_path / "opaque-input"
    target.write_text("inert fixture")
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")
    proposal = (
        {"network": True, "read_files": [str(target)]}
        if change == "file"
        else {"network": True, "environment": ["LR49_FIXTURE_INPUT"]}
    )
    tools = None

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.kind == "shell_run":
                if change == "file":
                    target.unlink()
                    target.write_text("different inert fixture")
                elif change == "env":
                    monkeypatch.setenv("LR49_FIXTURE_INPUT", "different inert fixture")
                elif change == "generation":
                    build.store.append("user_message", {"content": "Stop"})
                else:
                    tools["sandbox_permissions"].run({"action": "revoke"})
            return ApprovalDecision(True)

    surface = Surface()
    tools = build(surface)
    with pytest.raises(ConfigError, match="changed"):
        tools["shell_run"].run({"cmd": WRITES[0], "permissions": proposal})
    assert len(surface.requests) == 2 and not dispatched and not client.calls


def test_ordinary_network_access_still_uses_reviewer(harness):
    build, dispatched, client, _ = harness
    surface = _Surface(allow=False)
    build(surface)["shell_run"].run(
        {"cmd": "echo public-download", "permissions": {"network": True}}
    )
    assert (
        dispatched == ["echo public-download"] and not surface.requests and len(client.calls) == 1
    )


@pytest.mark.parametrize("change", ["command", "cwd"])
def test_changed_arguments_cannot_retarget_authentication_grant(harness, monkeypatch, change):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, _, root = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")
    (root / "other").mkdir()
    original = "git ls-remote https://github.com/owner/a.git"
    args = {
        "cmd": original,
        "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
    }

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            args["cmd" if change == "command" else "cwd"] = (
                WRITES[0] if change == "command" else "other"
            )
            return ApprovalDecision(True)

    tool = build(Surface())["shell_run"]
    if change == "cwd":
        with pytest.raises(AgentRuntimeError, match="does not match"):
            tool.run(args)
        assert not dispatched
    else:
        tool.run(args)
        assert dispatched == [original]  # immutable captured command, never the changed push


@pytest.mark.parametrize(
    "command",
    [
        "git -c core.sshCommand='sh opaque.sh' fetch origin",
        "git fetch --upload-pack='sh opaque.sh' origin",
        "git ls-remote --upload-pack='sh opaque.sh' git@github.com:owner/a.git",
        "git ls-remote ext::opaque",
        "GIT_SSH_COMMAND='sh opaque.sh' git ls-remote git@github.com:owner/a.git",
        "./git ls-remote git@github.com:owner/a.git",
        "./gh pr view 1",
        "./bash -c 'gh pr view 1'",
        "gh opaque-alias view",
        'gh api graphql -f "query=query Read { viewer { login } } $EXTRA_OPERATION"',
    ],
)
def test_unresolved_credential_reads_need_a_second_card(harness, monkeypatch, command):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, _, _ = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with pytest.raises(ApprovalDeclinedError):
        build(surface)["shell_run"].run(
            {
                "cmd": command,
                "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
            }
        )
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]
    assert not dispatched


def test_cwd_swap_during_write_card_cannot_retarget_repository(harness, monkeypatch):
    from alysis_code.config import ConfigError
    from alysis_code.surface import ApprovalDecision

    build, dispatched, _, root = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")
    approved = root / "approved"
    approved.mkdir()
    other = root / "other"
    other.mkdir()

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.kind == "shell_run":
                approved.rename(root / "original")
                approved.symlink_to(other, target_is_directory=True)
            return ApprovalDecision(True)

    with pytest.raises(ConfigError, match="links|changed"):
        build(Surface())["shell_run"].run(
            {
                "cmd": "git push origin feature",
                "cwd": "approved",
                "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
            }
        )
    assert not dispatched


@pytest.mark.parametrize("mode", ["auto", "review"])
@pytest.mark.parametrize("network", [False, True, "domain", "retained"])
def test_ssh_agent_alone_or_with_effective_network_needs_person(
    harness, monkeypatch, mode, network
):
    build, dispatched, client, _ = harness
    surface = _Surface()
    tool = build(surface, mode)["shell_run"]
    if network == "retained":
        tool.run({"cmd": "echo network", "permissions": {"network": True, "scope": "turn"}})
        dispatched.clear()
        client.calls.clear()
        surface.requests.clear()
    surface.allow = False
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="lr49-") as folder,
        socket.socket(socket.AF_UNIX) as agent,
    ):
        path = str(Path(folder).resolve() / "agent.sock")
        agent.bind(path)
        monkeypatch.setenv("SSH_AUTH_SOCK", path)
        proposal = {"ssh_agent": True}
        if network is True:
            proposal["network"] = True
        elif network == "domain":
            proposal["network_domains"] = ["github.com"]
        with pytest.raises(ApprovalDeclinedError):
            tool.run({"cmd": "git ls-remote git@github.com:owner/a.git", "permissions": proposal})
    assert len(surface.requests) == 1 and not dispatched and not client.calls


@pytest.mark.parametrize("input_name", ["GIT_SSH_COMMAND", "GH_PAGER"])
def test_execution_control_environment_is_not_proved_read_only(harness, monkeypatch, input_name):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, _, _ = harness
    monkeypatch.setenv(input_name, "opaque-fixture")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with pytest.raises(ApprovalDeclinedError):
        build(surface)["shell_run"].run(
            {
                "cmd": "git ls-remote git@github.com:owner/a.git",
                "permissions": {"network": True, "environment": [input_name]},
            }
        )
    assert len(surface.requests) == 2 and not dispatched


def test_separate_human_cards_allow_exact_write_once(harness, monkeypatch):
    build, dispatched, client, _ = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")
    surface = _Surface()
    tool = build(surface)["shell_run"]
    for _ in range(2):
        tool.run(
            {
                "cmd": WRITES[0],
                "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
            }
        )
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"] * 2
    assert dispatched == [WRITES[0]] * 2 and not client.calls


@pytest.mark.parametrize("status", ["expired", "cancelled"])
def test_expired_or_cancelled_authentication_never_dispatches(harness, monkeypatch, status):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, client, _ = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(False, status=status)

    surface = Surface()
    with pytest.raises(AgentRuntimeError, match=status):
        build(surface)["shell_run"].run(
            {
                "cmd": WRITES[0],
                "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
            }
        )
    assert len(surface.requests) == 1 and not dispatched and not client.calls


def test_existing_metadata_directory_is_a_valid_authenticated_cwd(harness, monkeypatch):
    build, dispatched, _, root = harness
    monkeypatch.setenv("LR49_FIXTURE_INPUT", "inert fixture")
    (root / ".github").mkdir()
    surface = _Surface()
    command = "git ls-remote https://github.com/owner/a.git"
    build(surface)["shell_run"].run(
        {
            "cmd": command,
            "cwd": ".github",
            "permissions": {"network": True, "environment": ["LR49_FIXTURE_INPUT"]},
        }
    )
    assert dispatched == [command] and len(surface.requests) == 2


@pytest.mark.parametrize("mode,yes", [("auto", True), ("review", False), ("fullaccess", True)])
def test_consumer_verification_cannot_bypass_remote_write_gate(harness, monkeypatch, mode, yes):
    build, dispatched, client, root = harness
    (root / "artifact.txt").write_text("inert artifact")
    # Capture the real verify_run's consumer dispatch entry. Nothing executes.
    consumer_calls = []
    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.run_consumer_profile",
        lambda **kw: consumer_calls.append(kw) or {},
    )
    surface = _Surface(allow=False)
    with pytest.raises(ApprovalDeclinedError):
        build(surface, mode, yes, verification=True)["verify_run"].run(
            {
                "consumer_profile": {
                    "profile": "cli",
                    "artifacts": ["artifact.txt"],
                    "checks": [{"command": "gh api repos/owner/b/issues -f title=fixture"}],
                }
            }
        )
    assert not consumer_calls and not dispatched and not client.calls
    (request,) = surface.requests
    assert request.kind == "verify_run" and request.metadata["always_ask_person"]


@pytest.mark.parametrize(
    "command",
    [
        "git diff --ext-diff",
        "git diff --textconv",
        "gh pr view 1 --web",
        "bash --rcfile=opaque.sh -ic 'git ls-remote git@github.com:owner/a.git'",
    ],
)
def test_ssh_only_cannot_hide_executable_read_options(harness, monkeypatch, command):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, _, _ = harness

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="lr49-") as folder,
        socket.socket(socket.AF_UNIX) as agent,
    ):
        path = str(Path(folder).resolve() / "agent.sock")
        agent.bind(path)
        monkeypatch.setenv("SSH_AUTH_SOCK", path)
        with pytest.raises(ApprovalDeclinedError):
            build(surface)["shell_run"].run(
                {"cmd": command, "permissions": {"network": True, "ssh_agent": True}}
            )
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]
    assert not dispatched


def test_literal_ssh_agent_read_remains_usable_after_both_person_decisions(harness, monkeypatch):
    build, dispatched, client, _ = harness
    surface = _Surface()
    command = "git ls-remote git@github.com:owner/a.git"
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="lr49-") as folder,
        socket.socket(socket.AF_UNIX) as agent,
    ):
        path = str(Path(folder).resolve() / "agent.sock")
        agent.bind(path)
        monkeypatch.setenv("SSH_AUTH_SOCK", path)
        build(surface)["shell_run"].run(
            {"cmd": command, "permissions": {"network": True, "ssh_agent": True}}
        )
    assert dispatched == [command] and not client.calls
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]


def test_arbitrary_environment_is_opaque_without_inspecting_values(harness, monkeypatch):
    from alysis_code.surface import ApprovalDecision

    build, dispatched, _, _ = harness
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "opaque-fixture")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with pytest.raises(ApprovalDeclinedError):
        build(surface)["shell_run"].run(
            {
                "cmd": "git diff",
                "permissions": {"network": True, "environment": ["GIT_EXTERNAL_DIFF"]},
            }
        )
    assert len(surface.requests) == 2 and not dispatched
    assert all("opaque-fixture" not in r.preview for r in surface.requests)


def test_git_status_configuration_requires_person_without_new_credentials(harness):
    build, dispatched, client, _ = harness
    surface = _Surface(allow=False)
    with pytest.raises(ApprovalDeclinedError):
        build(surface, "fullaccess", True)["shell_run"].run({"cmd": "git status"})
    assert not dispatched and not client.calls
    assert surface.requests[0].metadata["always_ask_person"]
