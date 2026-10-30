"""Real skill install/materialization with only the git subprocess port replaced."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from alysis_code.skills import install as install_mod
from alysis_code.skills.state import SkillLifecycleError, load_global_skill_state


@pytest.mark.parametrize(
    "failure", [None, "clone_error", "clone_timeout", "revision_error", "revision_timeout"]
)
def test_skill_git_install_isolated_and_bounded(tmp_path, monkeypatch, failure):
    hostile = {
        "HOME": str(tmp_path / "private-home"),
        "GIT_DIR": "/fixture/foreign.git",
        "GIT_WORK_TREE": "/fixture/work",
        "GH_TOKEN": "fixture-secret",
        "GIT_ASKPASS": "never-run",
        "SSH_ASKPASS": "never-run",
        "SSH_AUTH_SOCK": "/fixture/agent",
        "GIT_SSH_COMMAND": "never-run",
        "GIT_CONFIG_PARAMETERS": "never-run",
        "GIT_SSL_NO_VERIFY": "true",
        "NETRC": "/fixture/netrc",
        "LD_PRELOAD": "/fixture/loader",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.file:///fixture/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://",
        "PYTHONPATH": "/fixture/python",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SSL_CERT_DIR", "/fixture/ca-directory")
    monkeypatch.setattr(install_mod.shutil, "which", lambda _: "/fixture/git")
    calls, homes = [], []
    commit = "a" * 40

    def fake_run(args, **kwargs):
        calls.append(args)
        env = kwargs.get("env")
        assert env is not None, "skill git inherited the process environment"
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs["timeout"] == 120 and kwargs["shell"] is False
        assert kwargs["check"] is True and kwargs["capture_output"] is True
        assert env["GIT_ALLOW_PROTOCOL"] == "https"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"] == os.devnull
        home = Path(env["HOME"])
        assert home.is_dir() and not list(home.iterdir())
        assert env["USERPROFILE"] == env["XDG_CONFIG_HOME"] == env["GIT_TEMPLATE_DIR"] == str(home)
        homes.append(home)
        config = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert config == {
            "credential.helper": "",
            "core.hooksPath": str(home),
            "http.sslVerify": "true",
        }
        for key, value in hostile.items():
            assert env.get(key) != value, key
        assert env["SSL_CERT_DIR"] == "/fixture/ca-directory"
        phase = "clone" if args[1] == "clone" else "revision"
        if failure == phase + "_timeout":
            raise subprocess.TimeoutExpired(args, 120)
        if failure == phase + "_error":
            raise subprocess.CalledProcessError(1, args, stderr="fixture failure")
        if phase == "clone":
            target = Path(args[-1])
            target.mkdir()
            (target / "SKILL.md").write_text(
                "---\nname: isolated-skill\ndescription: Inert fixture\n---\n\nUse it.\n"
            )
        return subprocess.CompletedProcess(args, 0, stdout=commit + "\n", stderr="")

    monkeypatch.setattr(install_mod.subprocess, "run", fake_run)
    config = tmp_path / "config"

    def invoke():
        return install_mod.install_skill_bundle(
            source="https://example.invalid/skill.git",
            workspace_root=tmp_path,
            user_config_dir=config,
        )

    if failure and (failure.startswith("clone") or failure.endswith("timeout")):
        with pytest.raises(SkillLifecycleError, match="clone|timed out"):
            invoke()
        assert not load_global_skill_state(user_config_dir=config).managed_installs
    else:
        result = invoke()
        assert result.source_kind == "git"
        assert result.source_commit == (None if failure == "revision_error" else commit)
        assert (result.bundle_path / "SKILL.md").is_file()
    assert len(calls) == (1 if failure and failure.startswith("clone") else 2)
    assert len(set(homes)) == len(calls) and all(not home.exists() for home in homes)


@pytest.mark.parametrize(
    "source",
    [
        "file:///fixture/repo",
        "ssh://example.invalid/repo",
        "git@example.invalid:repo",
        "http://example.invalid/repo",
        "--upload-pack=fixture",
    ],
)
def test_skill_remote_transport_refused_before_subprocess(tmp_path, monkeypatch, source):
    monkeypatch.setattr(install_mod.shutil, "which", lambda _: "/fixture/git")

    def unexpected(*args, **kwargs):
        pytest.fail("unsupported source reached git")

    monkeypatch.setattr(install_mod.subprocess, "run", unexpected)
    with pytest.raises(SkillLifecycleError, match="HTTPS"):
        install_mod._clone_git_source(source, tmp_path / "clone")
