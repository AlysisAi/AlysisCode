"""R2 inert controls: command/service ports are captures, not network programs."""

import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_lr49_github_credential_gate as owner
from test_approval_review import _Surface

from alysis_code.agent.errors import AgentRuntimeError, ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.config import ConfigError
from alysis_code.shell_command_lexer import split_simple_commands
from alysis_code.surface import ApprovalDecision

# Re-export the real production-entry fixture for pytest discovery.
harness = owner.harness

CONTINUED = [
    "g\\\nit pu\\\nsh origin HEAD:main",
    "git\tpu\\\nsh\torigin HEAD:main",
    'git "pu\\\nsh" --force-with-lease origin main',
    "git\\\n push origin main",
    "git \\\n\tpush origin main",
    "git p\\\nu\\\ns\\\nh origin main",
    "sh -c 'git pu\\\nsh origin main'",
    'sh -c "git pu\\\nsh origin main"',
    "env LABEL=fixture sh -c 'gh pr cre\\\nate --title fixture'",
    "git -c core.sshCommand=opaque -C child pu\\\nsh origin main",
    "env GIT_DIR=child/.git GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.sshCommand GIT_CONFIG_VALUE_0=opaque git pu\\\nsh origin main",
    "gh api repos/owner/a/issues -XPO\\\nST",
]
AMBIGUOUS = [
    "git pu\\\r\nsh origin main",
    "git push $(opaque)",
    "sh -c 'git push $(opaque)'",
    'git push "unterminated',
]


@pytest.mark.parametrize("command", CONTINUED + AMBIGUOUS)
@pytest.mark.parametrize(
    "mode,permissions", [("fullaccess", None), ("auto", {"network": True, "git_metadata": True})]
)
def test_continued_or_ambiguous_write_cannot_use_mode_or_network_approval(
    harness, command, mode, permissions
):
    build, dispatched, client, _ = harness
    surface = _Surface(allow=False)
    args = {"cmd": command}
    if permissions is not None:
        args["permissions"] = permissions
    with pytest.raises(ApprovalDeclinedError):
        build(surface, mode, True)["shell_run"].run(args)
    assert not dispatched
    assert surface.requests[-1].command == command  # Original bytes bind the card.
    assert surface.requests[-1].metadata["always_ask_person"]


@pytest.mark.parametrize(
    "command",
    [
        'echo "git pu\\\nsh origin main"',
        "echo 'git pu\\\nsh origin main'",
        "printf '%s' 'gh pr cre\\\nate'",
    ],
)
def test_literal_data_and_active_escape_boundary(command):
    if command.startswith('echo "'):
        assert github_write_reason(command, dialect="posix")
    else:
        assert github_write_reason(command, dialect="posix") is None


@pytest.mark.parametrize("dialect", ["cmd", "powershell"])
def test_posix_continuation_is_not_applied_to_other_dialects(dialect):
    parts = split_simple_commands("g\\\nit push origin main", dialect=dialect)
    assert parts[0].words == ("g\\",)
    # No POSIX normalization, but unresolved Windows execution still needs a person.
    assert github_write_reason("g\\\nit push origin main", dialect=dialect)


@pytest.mark.parametrize(
    "command,words",
    [
        ("git pu\\\nsh", ("git", "push")),
        ('git "pu\\\nsh"', ("git", "push")),
        ("echo 'pu\\\nsh'", ("echo", "pu\\\nsh")),
        ("git pu\\\n\tsh", ("git", "pu", "sh")),
        ("git \\\n push", ("git", "push")),
    ],
)
def test_quote_aware_posix_tokens(command, words):
    assert split_simple_commands(command, dialect="posix")[0].words == words


CONFIGS = {
    "sshCommand": "[core]\n sshCommand = sh opaque.sh\n",
    "insteadOf": '[url "ext::sh opaque.sh"]\n insteadOf = git@github.com:\n[protocol "ext"]\n allow = always\n',
    "helper": "[credential]\n helper = !sh opaque.sh\n",
    "hooks": "[core]\n hooksPath = ./opaque-hooks\n",
    "include": "[include]\n path = ../opaque.cfg\n",
    "includeIf": '[includeIf "gitdir:./"]\n path = ../opaque.cfg\n',
    "worktree": "[extensions]\n worktreeConfig = true\n",
}


@pytest.mark.parametrize("configuration", CONFIGS)
@pytest.mark.parametrize("during_auth", [False, True])
@pytest.mark.parametrize("explicit_metadata", [False, True])
def test_repository_configuration_never_proves_credentialed_git_read(
    harness, monkeypatch, configuration, during_auth, explicit_metadata
):
    build, dispatched, client, root = harness
    (root / ".git").mkdir()
    config = root / ".git/config"
    config.write_text("[core]\n" if during_auth else CONFIGS[configuration])
    (root / ".git/config.worktree").write_text("[core]\n sshCommand = sh opaque.sh\n")
    (root / "opaque.cfg").write_text("[core]\n sshCommand = sh opaque.sh\n")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.kind == "sandbox_access" and during_auth:
                config.write_text(CONFIGS[configuration])
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="lr49-r2-") as folder,
        socket.socket(socket.AF_UNIX) as sock,
    ):
        path = str(Path(folder).resolve() / "agent.sock")
        sock.bind(path)
        monkeypatch.setenv("SSH_AUTH_SOCK", path)
        with pytest.raises(ApprovalDeclinedError):
            build(surface)["shell_run"].run(
                {
                    "cmd": "git ls-remote git@github.com:owner/a.git",
                    "permissions": {
                        "network": True,
                        "ssh_agent": True,
                        "git_metadata": explicit_metadata,
                    },
                }
            )
    assert not dispatched and not client.calls
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]


@pytest.mark.parametrize(
    "command",
    [
        "git ls-remote git@github.com:owner/a.git",
        "git -C child ls-remote git@github.com:owner/a.git",
        "git -c core.sshCommand=opaque ls-remote git@github.com:owner/a.git",
        "GIT_DIR=child/.git git ls-remote git@github.com:owner/a.git",
        "GIT_CONFIG_SYSTEM=opaque.cfg git ls-remote git@github.com:owner/a.git",
        "env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.sshCommand GIT_CONFIG_VALUE_0=opaque git ls-remote git@github.com:owner/a.git",
        "sh -c 'git ls-remote git@github.com:owner/a.git'",
    ],
)
def test_git_configuration_variants_need_separate_decision(command):
    assert github_write_reason(command, credential_access=True, dialect="posix")


@pytest.mark.parametrize("readiness", [False, True])
@pytest.mark.parametrize("swap", ["symlink", "inode"])
def test_service_and_readiness_cwd_swap_denies_before_manager(
    harness, monkeypatch, readiness, swap
):
    build, _, _, root = harness
    child = root / "child"
    child.mkdir()
    replacement = root / "replacement"
    replacement.mkdir()
    calls = []

    class Manager:
        def start(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(payload={"status": "started"})

    original = owner.build_tools
    monkeypatch.setattr(
        owner, "build_tools", lambda **kw: original(**kw, durable_service_manager=Manager())
    )

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.metadata.get("always_ask_person"):
                assert request.metadata["cwd"] == str(child)
                child.rename(root / "retired")
                if swap == "symlink":
                    child.symlink_to(replacement, target_is_directory=True)
                else:
                    child.mkdir()
            return ApprovalDecision(True)

    args = {"cmd": "echo inert" if readiness else "git push origin main", "cwd": "child"}
    if readiness:
        args["readiness"] = {"type": "command", "command": "git push origin main"}
    with pytest.raises((ConfigError, AgentRuntimeError)):
        build(Surface(), "fullaccess", True)["shell_service_start"].run(args)
    assert not calls


@pytest.mark.parametrize("late", [False, True])
def test_no_auth_cwd_is_checked_after_card_and_at_shell_dispatch(harness, monkeypatch, late):
    build, dispatched, _, root = harness
    child = root / "child"
    child.mkdir()

    def swap():
        child.rename(root / "retired")
        child.mkdir()

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if not late and request.metadata.get("always_ask_person"):
                swap()
            return ApprovalDecision(True)

    if late:
        original = build.store.append

        def append(kind, *args, **kwargs):
            if kind == "cmd":
                swap()
            return original(kind, *args, **kwargs)

        monkeypatch.setattr(build.store, "append", append)
    with pytest.raises((ConfigError, AgentRuntimeError)):
        build(Surface(), "fullaccess", True)["shell_run"].run(
            {"cmd": "git push origin main", "cwd": "child"}
        )
    assert not dispatched


@pytest.mark.parametrize(
    "command",
    ["echo '$(git push origin main)'", "printf '%s' 'git pu\\\nsh'", "echo '`gh pr create`'"],
)
def test_posix_literal_single_quoted_code_is_data(command):
    assert github_write_reason(command, dialect="posix") is None


@pytest.mark.parametrize(
    "command", ['env -S "git push origin main"', 'env --split-string="gh pr create"']
)
def test_ambiguous_env_split_payload_needs_person_without_credentials(command):
    assert github_write_reason(command, dialect="posix")


def test_nested_shell_parse_limit_requires_person_without_credentials():
    import shlex

    command = "git push origin main"
    for _ in range(8):
        command = "sh -c " + shlex.quote(command)
    assert github_write_reason(command, dialect="posix")


@pytest.mark.parametrize("phase", ["approval", "dispatch"])
def test_background_cwd_binding_is_checked_without_authentication(harness, monkeypatch, phase):
    build, _, _, root = harness
    child = root / "child"
    child.mkdir()
    calls = []

    def swap():
        child.rename(root / "retired")
        child.mkdir()

    class Manager:
        def start(self, **kw):
            calls.append(kw)
            raise AssertionError("must not dispatch")

    original = owner.build_tools
    monkeypatch.setattr(
        owner, "build_tools", lambda **kw: original(**kw, terminal_manager=Manager())
    )
    if phase == "dispatch":
        original_append = build.store.append

        def append(kind, *args, **kw):
            if kind == "bg_start":
                swap()
            return original_append(kind, *args, **kw)

        monkeypatch.setattr(build.store, "append", append)

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if phase == "approval" and request.metadata.get("always_ask_person"):
                swap()
            return ApprovalDecision(True)

    with pytest.raises((AgentRuntimeError, ConfigError)):
        build(Surface(), "fullaccess", True)["shell_background"].run(
            {"cmd": "git push origin main", "cwd": "child"}
        )
    assert not calls


@pytest.mark.parametrize("phase", ["approval", "dispatch"])
def test_consumer_verification_original_cwd_is_bound(harness, monkeypatch, phase):
    build, _, _, root = harness
    (root / "artifact.txt").write_text("inert")
    calls = []

    def swap():
        root.rename(root.parent / "retired")
        root.mkdir()

    monkeypatch.setattr(
        "alysis_code.agent.tools_assembly.run_consumer_profile", lambda **kw: calls.append(kw)
    )
    if phase == "dispatch":

        def mutation_join(*, operation, **kwargs):
            swap()
            return operation(), []

        monkeypatch.setattr(
            "alysis_code.agent.tools_assembly._run_with_command_mutation_detection", mutation_join
        )

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if phase == "approval" and request.metadata.get("always_ask_person"):
                swap()
            return ApprovalDecision(True)

    with pytest.raises((AgentRuntimeError, ConfigError)):
        build(Surface(), "fullaccess", True, verification=True)["verify_run"].run(
            {
                "consumer_profile": {
                    "profile": "cli",
                    "artifacts": ["artifact.txt"],
                    "checks": [{"command": "git push origin main"}],
                }
            }
        )
    assert not calls
