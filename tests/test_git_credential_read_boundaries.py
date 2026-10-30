"""Credential-backed Git reads require approval when repository configuration can execute code."""

import json
import socket
import tempfile
from pathlib import Path

import pytest
import test_lr49_github_credential_gate as fixture_owner
from test_approval_review import _Surface

from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.macos_git import literal_git_process
from alysis_code.surface import ApprovalDecision

# Share the build_tools fixture used by the credential-gate tests.
harness = fixture_owner.harness


def test_literal_git_transport_is_not_rewritten_to_disable_local_ssh_command():
    command = "git ls-remote git@github.com:owner/a.git"
    assert literal_git_process(command) == [
        "/usr/bin/git",
        "ls-remote",
        "git@github.com:owner/a.git",
    ]


@pytest.mark.parametrize("changed_during_auth", [False, True])
@pytest.mark.parametrize("explicit_metadata", [False, True])
def test_executable_repo_config_is_not_a_literal_read_proof(
    harness, monkeypatch, changed_during_auth, explicit_metadata
):
    build, dispatched, client, root = harness
    (root / ".git").mkdir()
    cfg = root / ".git/config"
    # The opaque local script is NEVER executed. Its effect port is only a specimen.
    script = root / "opaque.sh"
    script.write_text("#!/bin/sh\n# opaque remote effects; not executed\nexit 1\n")
    configured = "[core]\n    sshCommand = sh " + str(script) + "\n"
    cfg.write_text("[core]\n" if changed_during_auth else configured)

    class Surface(_Surface):
        def request_approval(self, request):
            self.requests.append(request)
            if request.kind == "sandbox_access" and changed_during_auth:
                cfg.write_text(configured)
            return ApprovalDecision(request.kind == "sandbox_access")

    surface = Surface()
    error = None
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="git-read-") as folder,
        socket.socket(socket.AF_UNIX) as agent,
    ):
        path = str(Path(folder).resolve() / "agent.sock")
        agent.bind(path)
        monkeypatch.setenv("SSH_AUTH_SOCK", path)
        try:
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
        except ApprovalDeclinedError as e:
            error = e
    print(
        json.dumps(
            {
                "changed_during_auth": changed_during_auth,
                "explicit_metadata": explicit_metadata,
                "cards": [(x.kind, x.metadata) for x in surface.requests],
                "dispatched": dispatched,
                "config": cfg.read_text(),
                "error": type(error).__name__ if error else None,
            }
        )
    )
    assert not dispatched, (
        "executable local Git config was treated as proven read after auth approval only"
    )


@pytest.mark.parametrize(
    "command", ["git pu\\\nsh origin HEAD:main", "git p\\\nush --force-with-lease origin HEAD:main"]
)
def test_auto_network_metadata_grant_is_not_an_approval_of_a_continued_push(harness, command):
    build, dispatched, client, root = harness
    surface = _Surface(allow=False)
    error = None
    try:
        build(surface, "auto", True)["shell_run"].run(
            {"cmd": command, "permissions": {"network": True, "git_metadata": True}}
        )
    except ApprovalDeclinedError as e:
        error = e
    print(
        json.dumps(
            {
                "case": "effective-network",
                "error": type(error).__name__ if error else None,
                "cards": [x.kind for x in surface.requests],
                "dispatched": dispatched,
                "reviewer_calls": len(client.calls),
            }
        )
    )
    assert not dispatched, (
        "public network/metadata approval was reused for a hidden push without a person"
    )
