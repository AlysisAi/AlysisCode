"""Git install subprocess policy, inspected through inert subprocess ports only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from alysis_code.extensions import install as install_mod


def test_git_cannot_inherit_credentials_config_or_interactive_stdin(tmp_path, monkeypatch):
    hostile = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!never-run-fixture",
        "GIT_CONFIG_PARAMETERS": "fixture",
        "GIT_CONFIG_GLOBAL": "/fixture/private-gitconfig",
        "GIT_CONFIG_SYSTEM": "/fixture/system-gitconfig",
        "GIT_TEMPLATE_DIR": "/fixture/templates",
        "GIT_DIR": "/fixture/foreign.git",
        "GIT_WORK_TREE": "/fixture/foreign",
        "GIT_SSH_COMMAND": "never-run-fixture",
        "GIT_ASKPASS": "never-run-fixture",
        "SSH_ASKPASS": "never-run-fixture",
        "SSH_AUTH_SOCK": "/fixture/agent",
        "GIT_TRACE": "/fixture/trace",
        "GIT_TRACE_CURL": "/fixture/trace-curl",
        "GIT_SSL_NO_VERIFY": "true",
        "GH_TOKEN": "synthetic-token",
        "GITHUB_TOKEN": "synthetic-token",
        "LD_PRELOAD": "/fixture/loader",
        "DYLD_INSERT_LIBRARIES": "/fixture/loader",
        "PYTHONPATH": "/fixture/python",
        "CUSTOM_PRIVATE_SECRET": "synthetic-secret",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    tls = {
        "SSL_CERT_FILE": "/fixture/ca.pem",
        "GIT_SSL_CAINFO": "/fixture/git-ca.pem",
        "HTTPS_PROXY": "http://fixture.invalid:3128",
        "NO_PROXY": "fixture.invalid",
    }
    for key, value in tls.items():
        monkeypatch.setenv(key, value)
    captured = []

    def fake_run(args, **kwargs):
        env = kwargs.get("env")
        assert env is not None, "git inherited ambient environment"
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs["shell"] is False and kwargs["check"] is True
        assert kwargs["timeout"] == 120 and kwargs["capture_output"] is True
        assert kwargs["cwd"] == str(tmp_path)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert env["GIT_ALLOW_PROTOCOL"] == "https"
        home = Path(env["HOME"])
        assert home.is_dir() and not list(home.iterdir())
        assert env["USERPROFILE"] == str(home)
        assert env["XDG_CONFIG_HOME"] == str(home)
        assert env["GIT_TEMPLATE_DIR"] == str(home)
        config = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert config == {
            "credential.helper": "",
            "core.hooksPath": str(home),
            "http.sslVerify": "true",
        }
        for key in hostile:
            if key != "GIT_CONFIG_KEY_0":  # same key, but controlled empty helper above
                assert env.get(key) != hostile[key], key
        for key, value in tls.items():
            assert env[key] == value
        captured.append(home)
        return subprocess.CompletedProcess(args, 0, stdout="fixture-output", stderr="")

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
    result = install_mod._run_git(["git", "rev-parse", "HEAD"], cwd=tmp_path)
    assert result.stdout == "fixture-output"
    assert len(captured) == 1 and not captured[0].exists()
    assert os.environ["GH_TOKEN"] == "synthetic-token"


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_git_isolation_cleaned_on_subprocess_failure(tmp_path, monkeypatch, failure):
    homes = []

    def fake_run(args, **kwargs):
        env = kwargs.get("env")
        assert env is not None
        homes.append(Path(env["HOME"]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 120)
        raise subprocess.CalledProcessError(1, args, stderr="fixture refused")

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
    with pytest.raises(install_mod.PluginInstallError, match="timed out|command failed"):
        install_mod._run_git(["git", "init", str(tmp_path)], cwd=None)
    assert len(homes) == 1 and not homes[0].exists()


def test_pinned_clone_sequence_keeps_hash_verification_and_fresh_homes(tmp_path, monkeypatch):
    calls = []
    homes = []
    commit = "a" * 40

    def fake_run(args, **kwargs):
        env = kwargs.get("env")
        assert env is not None and kwargs.get("stdin") == subprocess.DEVNULL
        calls.append(args)
        homes.append(Path(env["HOME"]))
        return subprocess.CompletedProcess(
            args, 0, stdout=commit + "\n" if args[1] == "rev-parse" else "", stderr=""
        )

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
    install_mod._clone_pinned_git_repo(
        git_url="https://example.invalid/plugin.git", commit=commit, destination=tmp_path
    )
    assert [args[1] for args in calls] == ["init", "remote", "fetch", "checkout", "rev-parse"]
    assert calls[2] == ["git", "fetch", "--depth", "1", "origin", commit]
    assert len(set(homes)) == 5 and all(not home.exists() for home in homes)


def test_pinned_clone_still_refuses_hash_mismatch(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="b" * 40 + "\n", stderr="")

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
    with pytest.raises(install_mod.PluginInstallError, match="checkout mismatch"):
        install_mod._clone_pinned_git_repo(
            git_url="https://example.invalid/plugin.git", commit="a" * 40, destination=tmp_path
        )
