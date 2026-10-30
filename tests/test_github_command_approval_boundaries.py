"""GitHub command approvals refuse ambiguous syntax before dispatch."""

import socket
import tempfile
from pathlib import Path

import pytest
import test_lr49_github_credential_gate as fixture_owner
from test_approval_review import _Surface

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.command_review_rules import github_write_reason
from alysis_code.surface import ApprovalDecision

# Reuse real tool assembly with fake credentials and dispatch.
harness = fixture_owner.harness


@pytest.mark.parametrize(
    "command",
    [
        "git pu\\\nsh origin HEAD:main",
        "g\\\nit push origin HEAD:main",
        "gh pr cre\\\nate --title fixture",
        "gh api repos/owner/b/issues -XPO\\\nST",
    ],
)
def test_posix_continuation_must_not_hide_write(harness, command):
    build, dispatched, client, _ = harness
    surface = _Surface(allow=False)
    try:
        build(surface, "fullaccess", True)["shell_run"].run({"cmd": command})
    except ApprovalDeclinedError:
        pass
    assert not dispatched, {"dispatched": dispatched, "cards": [r.kind for r in surface.requests]}
    assert len(surface.requests) == 1
    assert surface.requests[0].metadata.get("always_ask_person") is True


@pytest.mark.parametrize(
    "config",
    [
        "[core]\n sshCommand = sh opaque-fixture.sh\n",
        '[url "ext::sh opaque-fixture.sh"]\n insteadOf = git@github.com:\n[protocol "ext"]\n allow = always\n',
    ],
)
def test_repo_executable_config_is_not_a_credential_read_proof(harness, monkeypatch, config):
    build, dispatched, client, root = harness
    (root / ".git").mkdir()
    (root / ".git/config").write_text(config)
    (root / "opaque-fixture.sh").write_text("# inert fixture, never executed\n")

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="pr97-") as d,
        socket.socket(socket.AF_UNIX) as sock,
    ):
        p = str(Path(d).resolve() / "agent.sock")
        sock.bind(p)
        monkeypatch.setenv("SSH_AUTH_SOCK", p)
        try:
            build(surface)["shell_run"].run(
                {
                    "cmd": "git ls-remote git@github.com:owner/a.git",
                    "permissions": {"network": True, "ssh_agent": True},
                }
            )
        except ApprovalDeclinedError:
            pass
    assert not dispatched, {"dispatched": dispatched, "cards": [r.kind for r in surface.requests]}
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]


@pytest.mark.parametrize(
    "command",
    [
        'git -c alias.pub="!git push origin main" pub',
        'git -c core.sshCommand="sh opaque.sh" ls-remote git@github.com:owner/a.git',
        'GIT_SSH_COMMAND="sh opaque.sh" git ls-remote git@github.com:owner/a.git',
        'env -S "git push origin main"',
        'sh -c "git push origin main"',
        "sh <<EOF\ngit push origin main\nEOF",
        "./symlink-to-git push origin main",
        "cd other && git push origin main",
        "bash opaque.sh",
        "git -c remote.origin.url=git@github.com:owner/b.git push origin main",
        "gh api graphql --input payload.json",
        "gh api repos/owner/b/issues -f title=fixture",
    ],
)
def test_explicit_or_opaque_ssh_forms_refuse(harness, monkeypatch, command):
    build, dispatched, client, _ = harness

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="pr97-") as d,
        socket.socket(socket.AF_UNIX) as sock,
    ):
        p = str(Path(d).resolve() / "agent.sock")
        sock.bind(p)
        monkeypatch.setenv("SSH_AUTH_SOCK", p)
        with pytest.raises(ApprovalDeclinedError):
            build(surface)["shell_run"].run(
                {"cmd": command, "permissions": {"network": True, "ssh_agent": True}}
            )
    assert not dispatched and not client.calls
    assert [r.kind for r in surface.requests] == ["sandbox_access", "shell_run"]


@pytest.mark.parametrize(
    "command",
    [
        "git fetch origin",
        "gh api repos/owner/a -X GET",
        "gh api graphql -f 'query=query { viewer { login } }'",
        'echo "git push origin main"',
    ],
)
def test_noncredential_literal_reads_and_git_config_boundary(command):
    if command.startswith("git "):
        assert github_write_reason(command)
    else:
        assert github_write_reason(command) is None


@pytest.mark.parametrize(
    "command,expected",
    [
        ("git pu\\\nsh origin HEAD:main", "git:<push><origin><HEAD:main>"),
        ("g\\\nit push origin HEAD:main", "git:<push><origin><HEAD:main>"),
        ("gh pr cre\\\nate --title fixture", "gh:<pr><create><--title><fixture>"),
    ],
)
def test_real_posix_shell_normalizes_continuations_with_inert_functions(command, expected):
    import os
    import subprocess

    # Functions shadow both programs. Only the shell's builtin printf executes.
    prefix = "git() { printf 'git:'; printf '<%s>' \"$@\"; }; gh() { printf 'gh:'; printf '<%s>' \"$@\"; }; "
    result = subprocess.run(
        ["/bin/sh", "-c", prefix + command],
        capture_output=True,
        text=True,
        timeout=5,
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
    )
    assert result.returncode == 0
    assert result.stdout == expected


def test_pinned_git_reads_local_executable_config_without_network(tmp_path):
    import os
    import subprocess

    from alysis_code.macos_git import literal_git_process

    root = tmp_path / "repo"
    root.mkdir()
    for p in [".git/objects", ".git/refs/heads"]:
        (root / p).mkdir(parents=True, exist_ok=True)
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    marker = tmp_path / "marker"
    script = root / "fixture-transport.sh"
    script.write_text('printf configured-transport >> "' + str(marker) + '"\nexit 17\n')
    (root / ".git/config").write_text(
        "[core]\n repositoryformatversion = 0\n bare = false\n sshCommand = /bin/sh "
        + str(script)
        + "\n"
    )
    empty = tmp_path / "empty-global"
    empty.write_text("")
    command = "git ls-remote git@github.com:owner/a.git"
    argv = literal_git_process(command)
    assert argv[0] == "/usr/bin/git"
    # No ssh, GitHub connection or credential is used: Git invokes only our inert script.
    result = subprocess.run(
        argv,
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": os.environ["HOME"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(empty),
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert marker.exists(), result.stderr
    assert marker.read_text().startswith("configured-transport")
    assert result.returncode != 0


@pytest.mark.parametrize(
    "command",
    [
        "git pu\\\nsh origin HEAD:main",
        "g\\\nit push origin HEAD:main",
        "gh pr cre\\\nate --title fixture",
    ],
)
def test_continuation_write_with_auto_yes_and_network_approval(harness, command):
    build, dispatched, client, root = harness
    surface = _Surface(allow=False)
    try:
        build(surface, "auto", True)["shell_run"].run(
            {"cmd": command, "permissions": {"network": True}}
        )
    except ApprovalDeclinedError:
        pass
    assert not dispatched, {
        "cards": [r.kind for r in surface.requests],
        "reviewer_calls": len(client.calls),
        "dispatched": dispatched,
    }
    assert any(
        r.kind == "shell_run" and r.metadata.get("always_ask_person") for r in surface.requests
    )
