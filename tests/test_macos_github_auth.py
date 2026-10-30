import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_macos_access

from alysis_code import macos_github_auth, sandbox_runner
from alysis_code.agent.errors import ApprovalDeclinedError
from alysis_code.config import ConfigError
from alysis_code.git_github_auth import GithubCredential
from alysis_code.macos_github_auth import validate_command
from alysis_code.sandbox_runner import SeatbeltShellRunner, select_shell_sandbox_backend
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS credential reader and Seatbelt"
)

mac_auth = test_macos_access.auth


@pytest.fixture
def auth(mac_auth):
    return mac_auth


@pytest.mark.parametrize(
    "command",
    [
        "git fetch origin",
        "git -C child push origin HEAD",
        "/usr/bin/git clone -- https://github.com/o/r.git child",
        "git ls-remote origin",
    ],
)
def test_literal_remote_git(command):
    validate_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "git fetch; echo x",
        "git fetch && git push",
        "git fetch $(echo x)",
        "git -c core.sshCommand=evil fetch",
        "git commit -m x",
        "python fetch.py",
    ],
)
def test_no_shell_or_executable_configuration(command):
    with pytest.raises(ConfigError, match="literal"):
        validate_command(command)


def test_mac_loader_ignores_workspace_and_environment_overrides(tmp_path, monkeypatch):
    executable = tmp_path / "gh"
    monkeypatch.setattr(macos_github_auth, "credential_reader", lambda: executable)
    monkeypatch.setenv("GH_TOKEN", "untrusted")
    monkeypatch.setenv("GH_CONFIG_DIR", "untrusted")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "untrusted")
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (
            calls.append((a, k))
            or SimpleNamespace(returncode=0, stdout="synthetic_account_token_123456")
        ),
    )
    credential = macos_github_auth.load_credential()
    argv, options = calls[0]
    assert argv[0] == [str(executable), "auth", "token", "--hostname", "github.com"]
    assert not {"GH_TOKEN", "GH_CONFIG_DIR", "DYLD_INSERT_LIBRARIES"} & options["env"].keys()
    assert options["cwd"] == executable.parent
    assert credential.token not in repr(credential)


def test_credential_reader_cannot_be_workspace_controlled(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_github_auth, "credential_reader", lambda: tmp_path / "gh")
    with pytest.raises(ConfigError, match="writable workspace"):
        macos_github_auth.load_credential(workspace_roots=(tmp_path,))


def test_git_does_not_discover_unapproved_parent_repository(auth):
    manager, _, _ = auth
    # A valid outer repository would not be within this project's authority.
    # Git should stop discovery before opening even its .git control file.
    (manager.root.parent / ".git").write_text("gitdir: /unapproved-parent-metadata\n")
    result = SeatbeltShellRunner().run(
        root=manager.root, cwd=manager.root, cmd="git rev-parse --show-toplevel", timeout_s=20
    )
    assert result.returncode != 0
    assert "Operation not permitted" not in result.stderr
    assert "unapproved-parent-metadata" not in result.stderr


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "warn"},
        {"network": "off"},
        {"backend": "host"},
        {"clear_env": False},
        {"protect_repo_meta": False},
        {"safe_git_writes": False},
    ],
)
def test_no_host_fallback(changes):
    settings = replace(
        ShellSandboxSettings(backend="seatbelt", github_auth=True, network="on"), **changes
    )
    with pytest.raises(ConfigError, match="GitHub authentication requires"):
        select_shell_sandbox_backend(settings, system="Darwin")


def test_review_has_full_authority_and_no_retention(auth):
    manager, requests, _ = auth
    grant = manager.prepare(
        {"github_auth": True, "network": True}, command="git fetch origin", tool="shell_run"
    )
    assert grant.settings.github_auth
    assert requests[-1].authentication
    assert "full authority" in requests[-1].preview
    assert requests[-1].metadata["allow_for_session_disabled"]
    assert not manager.prepare(
        None, command="git fetch origin", tool="shell_run"
    ).settings.github_auth
    for scope in ("turn", "session"):
        with pytest.raises(ConfigError):
            manager.prepare(
                {"github_auth": True, "network": True, "scope": scope},
                command="git fetch origin",
                tool="shell_run",
            )
    manager.approve = lambda _: ApprovalDecision(False)
    with pytest.raises(ApprovalDeclinedError):
        manager.prepare(
            {"github_auth": True, "network": True}, command="git fetch origin", tool="shell_run"
        )


@pytest.mark.parametrize("interrupted", [False, True])
def test_result_and_timeout_output_are_redacted(auth, monkeypatch, interrupted):
    manager, _, _ = auth
    credential = GithubCredential("synthetic_account_token_123456")
    monkeypatch.setattr(macos_github_auth, "load_credential", lambda **_: credential)

    def execute(command, **kwargs):
        assert credential.token not in command and credential.basic not in command
        assert credential.basic in str(kwargs["env"])
        if interrupted:
            raise subprocess.TimeoutExpired(
                "git fetch origin", 30, output=credential.token.encode(), stderr=credential.basic
            )
        return subprocess.CompletedProcess(
            "git fetch origin", 0, credential.token, credential.basic
        )

    monkeypatch.setattr(sandbox_runner, "run_in_tracked_process_group", execute)
    runner = SeatbeltShellRunner(github_auth=True, network="on")
    if interrupted:
        with pytest.raises(subprocess.TimeoutExpired) as failure:
            runner.run(root=manager.root, cwd=manager.root, cmd="git fetch origin", timeout_s=30)
        result = failure.value
    else:
        result = runner.run(
            root=manager.root, cwd=manager.root, cmd="git fetch origin", timeout_s=30
        )
    assert credential.token not in result.stdout
    assert credential.basic not in result.stderr
