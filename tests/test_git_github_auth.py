import json
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from alysis_code.config import ConfigError
from alysis_code.git_github_auth import GithubCredential, load_credential, validate_command
from alysis_code.git_native import clone_destination, run
from alysis_code.sandbox_runner import select_shell_sandbox_backend
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.parametrize(
    "command",
    [
        "git fetch origin",
        "git -C child push origin HEAD",
        "git clone -- https://github.com/owner/repo.git child",
        "git ls-remote origin",
    ],
)
def test_exact_remote_commands(command):
    validate_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "git fetch origin; evil",
        "git -c alias.x=evil x",
        "git log",
        "git commit -m x",
        "python fetch.py",
        "git fetch $(evil)",
    ],
)
def test_auth_cannot_be_attached_to_other_commands(command):
    with pytest.raises(ConfigError, match="literal"):
        validate_command(command)


@pytest.mark.parametrize(
    "changes,system",
    [
        ({"mode": "off"}, "Windows"),
        ({"network": "off"}, "Windows"),
        ({"backend": "docker"}, "Windows"),
        ({}, "Darwin"),
        ({"clear_env": False}, "Windows"),
        ({"protect_repo_meta": False}, "Windows"),
    ],
)
def test_auth_never_falls_back_to_host(changes, system):
    settings = replace(
        ShellSandboxSettings(
            backend="windows-mxc", network="on", github_auth=True, unavailable_fallback="ask"
        ),
        **changes,
    )
    with pytest.raises(ConfigError, match="GitHub authentication requires"):
        select_shell_sandbox_backend(settings, system=system)


def test_credential_loader_uses_fixed_host_reader_without_workspace_or_token_overrides(
    tmp_path, monkeypatch
):
    from alysis_code import windows_sandbox, windows_toolchain

    executable = tmp_path / "GitHub CLI/gh.exe"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(windows_toolchain, "program_files", lambda: tmp_path)
    monkeypatch.setattr(windows_toolchain, "known_folder", lambda _: tmp_path / "standard-roaming")
    monkeypatch.setattr(windows_sandbox, "_local_path", lambda p, **_: p)
    monkeypatch.setenv("GH_TOKEN", "must-not-use-this")
    monkeypatch.setenv("GH_CONFIG_DIR", "workspace-controlled")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-use-this-either")
    monkeypatch.setenv("APPDATA", "isolated-app-profile")
    calls = []

    def invoke(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout="synthetic_account_token_123456\n", returncode=0)

    monkeypatch.setattr(subprocess, "run", invoke)
    credential = load_credential()
    assert calls[0][0] == [str(executable), "auth", "token", "--hostname", "github.com"]
    assert calls[0][1]["cwd"] == executable.parent
    assert calls[0][1]["env"]["APPDATA"] == str(tmp_path / "standard-roaming")
    assert not ({"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"} & calls[0][1]["env"].keys())
    assert credential.token not in repr(credential)
    env = {}
    credential.configure(env)
    config = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]
    assert ("url.https://github.com/.insteadOf", "git@github.com:") in config
    assert ("http.followRedirects", "false") in config
    assert all("github.com" in k for k, v in config if "Authorization" in v)
    assert credential.token not in credential.redact(credential.token)
    assert credential.basic not in credential.redact(credential.basic)


def test_clone_requires_fresh_child_of_approved_folder(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    assert clone_destination(["clone", "--", "repo", "child"], root, [root]) == root / "child"
    for target in (".", "../outside", str(tmp_path / "outside")):
        with pytest.raises(ConfigError):
            clone_destination(["clone", "repo", target], root, [root])
    with pytest.raises(ConfigError):
        clone_destination(["clone", "--template=evil", "repo", "child"], root, [root])


@pytest.mark.parametrize(
    "online,authenticated,interrupted",
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
@pytest.mark.skipif(os.name != "nt", reason="Native Windows Git reservations")
def test_native_transport_honors_network_and_auth_is_only_in_process_env(
    tmp_path, monkeypatch, online, authenticated, interrupted
):
    from alysis_code import git_github_auth, git_native, windows_mxc, windows_toolchain

    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    openssh = tmp_path / "Windows/System32/OpenSSH/ssh.exe"
    openssh.parent.mkdir(parents=True)
    openssh.touch()
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(
        windows_toolchain, "installed_tools", lambda: {"git": (tmp_path / "git.exe", tmp_path)}
    )
    monkeypatch.setattr(
        git_native, "safe_global_config", lambda p: p.write_text("[user]\nname=test\n")
    )
    credential = GithubCredential("synthetic_account_token_123456")
    loads, requests = [], []
    monkeypatch.setattr(
        git_github_auth, "load_credential", lambda: loads.append(True) or credential
    )

    def prepare(self, **kwargs):
        return ["helper"], json.dumps(
            {
                "cwd": str(root),
                "read": [],
                "deny_read": [],
                "deny_write": [],
                "env": [],
                "network": "off",
            }
        ).encode()

    def execute(self, **kwargs):
        _, payload = self._prepare(root=root, cwd=root, cmd="")
        requests.append(json.loads(payload))
        assert self.settings.github_auth is False
        if interrupted:
            raise subprocess.TimeoutExpired(
                "git", 30, output=credential.token, stderr=credential.basic
            )
        return subprocess.CompletedProcess("", 0, credential.token if authenticated else "ok", "")

    monkeypatch.setattr(windows_mxc.WindowsMxcRunner, "_prepare", prepare)
    monkeypatch.setattr(windows_mxc.WindowsMxcRunner, "run", execute)
    try:
        result = run(
            settings=ShellSandboxSettings(
                backend="windows-mxc", network="on" if online else "off", github_auth=authenticated
            ),
            root=root,
            cwd=root,
            argv=["fetch", "origin"],
            timeout_s=30,
            cancel_check=None,
        )
        assert not interrupted
    except subprocess.TimeoutExpired as exc:
        assert interrupted
        result = exc
        assert credential.basic not in result.stderr
    assert requests[0]["network"] == ("on" if online else "off")
    assert ("protocol.https.allow=always" in requests[0]["argv"]) == online
    assert ("protocol.ssh.allow=always" in requests[0]["argv"]) == online
    assert (
        dict(requests[0]["env"])["GIT_SSH"].replace("\\", "/").endswith("/System32/OpenSSH/ssh.exe")
    )
    assert bool(loads) == authenticated
    assert ("GIT_CONFIG_COUNT" in dict(requests[0]["env"])) == authenticated
    assert credential.token not in result.stdout
    assert credential.basic not in repr(requests[0]["argv"])
    assert not list(root.rglob("*token*"))
