from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code import windows_sandbox as native
from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_runner import LazyShellRunner, select_shell_sandbox_backend
from alysis_code.sandbox_settings import (
    ADDITIONAL_WORKSPACE_ROOTS_ENV,
    ShellSandboxSettings,
    resolve_shell_sandbox_settings,
)
from alysis_code.sandbox_workspace import runner_workspace_roots


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        "null",
        '"folder"',
        "[42]",
        '["relative"]',
        '["a\\u0000b"]',
        "[" + ",".join('"/root"' for _ in range(33)) + "]",
    ],
)
def test_invalid_launcher_scope_refused(monkeypatch, value):
    monkeypatch.setenv(ADDITIONAL_WORKSPACE_ROOTS_ENV, value)
    with pytest.raises(ConfigError, match="ADDITIONAL_WORKSPACE_ROOTS"):
        resolve_shell_sandbox_settings(AppConfig(model="test"))


def test_project_config_cannot_add_roots_and_settings_are_frozen(tmp_path, monkeypatch):
    cfg = AppConfig(model="test")
    cfg.extra_fields["shell_sandbox"] = {
        "additional_workspace_roots": [str(tmp_path / "unapproved")]
    }
    monkeypatch.delenv(ADDITIONAL_WORKSPACE_ROOTS_ENV, raising=False)
    assert resolve_shell_sandbox_settings(cfg).additional_workspace_roots == ()
    monkeypatch.setenv(ADDITIONAL_WORKSPACE_ROOTS_ENV, json.dumps([str(tmp_path)]))
    settings = resolve_shell_sandbox_settings(cfg)
    runner = LazyShellRunner(
        lambda: pytest.fail("metadata must not initialize a helper"), settings=settings
    )
    monkeypatch.setenv(ADDITIONAL_WORKSPACE_ROOTS_ENV, "[]")
    assert runner_workspace_roots(runner) == (str(tmp_path),)
    assert resolve_shell_sandbox_settings(cfg).additional_workspace_roots == ()


@pytest.fixture
def folders(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_powershell_execution_policy", lambda _: "RemoteSigned")
    paths = [tmp_path / name for name in ("app", "engine", "unrelated", "helper")]
    for path in paths:
        path.mkdir()
    return paths


def policy(folders, *, cwd=None, settings=None):
    primary, secondary, _, helper = folders
    settings = settings or ShellSandboxSettings(additional_workspace_roots=(str(secondary),))
    return native.build_request(
        settings,
        root=primary,
        cwd=cwd or primary,
        cmd="Write-Output OK",
        helper=helper / "helper.exe",
    )


@pytest.mark.parametrize("access", ["read-only", "workspace-write"])
@pytest.mark.parametrize("network", ["off", "on"])
def test_all_roots_keep_protections_and_cwd_can_use_secondary(folders, access, network):
    a, b, outside, _ = folders
    settings = ShellSandboxSettings(
        filesystem_access=access,
        network=network,
        additional_workspace_roots=(str(b), str(a), str(b)),
    )
    request = policy(folders, cwd=b, settings=settings)
    assert request["write"] == ([str(a), str(b)] if access == "workspace-write" else [])
    assert str(outside) not in request["read"]
    assert request["cwd"] == str(b)
    for root in (a, b):
        for name in ("AGENTS.md", ".git", ".codex", ".github/skills"):
            assert root / name in map(Path, request["deny_write"])
    assert str(b).replace("'", "''") in request["argv"][-1]
    with pytest.raises(ConfigError, match="approved workspace"):
        policy(folders, cwd=outside, settings=settings)


def test_secondary_alias_and_metadata_root_refused(folders):
    a, b, outside, _ = folders
    target = outside / "secret"
    target.write_text("unchanged")
    link = b / "alias"
    link.hardlink_to(target)
    with pytest.raises(ConfigError, match="hardlinked"):
        policy(folders)
    link.unlink()
    metadata = a / ".git"
    metadata.mkdir()
    with pytest.raises(ConfigError, match="protected metadata"):
        policy(folders, settings=ShellSandboxSettings(additional_workspace_roots=(str(metadata),)))
    assert list(metadata.iterdir()) == []


@pytest.mark.parametrize("raw", ["", "{}", "[1]", '["relative"]', '["\\u0000"]'])
def test_invalid_desktop_runtime_root_policy_refuses(folders, monkeypatch, raw):
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", raw)
    with pytest.raises(ConfigError, match="runtime policy"):
        policy(folders)


def test_existing_and_future_runtime_generations_are_protected(folders, monkeypatch):
    primary, secondary, outside, _ = folders
    runtime = primary / "installed-runtimes"
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(runtime)]))
    with pytest.raises(ConfigError):
        policy(folders)
    assert not runtime.exists()
    runtime.mkdir()
    (runtime / "old-generation").mkdir()
    request = policy(folders)
    assert str(runtime) in request["deny_write"]
    assert str(runtime) not in request["deny_read"]
    with pytest.raises(ConfigError, match="Desktop runtime"):
        policy(folders, cwd=runtime)
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(secondary)]))
    with pytest.raises(ConfigError, match="trusted host runtime"):
        policy(folders)
    monkeypatch.setenv(
        "ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(outside / "future-runtime")])
    )
    assert policy(folders)["write"] == [str(primary), str(secondary)]


def test_extra_folder_cannot_make_future_desktop_repair_generations_writable(folders, monkeypatch):
    from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError

    _primary, extra, _outside, _helper = folders
    runtime = extra / "cli-runtime"
    active = runtime / "active"
    active.mkdir(parents=True)
    (active / "canary.txt").write_text("unchanged")
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(runtime)]))
    request = policy(folders)
    assert str(extra) in request["write"]
    assert str(runtime) in request["deny_write"]
    host = HostFilePolicy.capture()
    for target in (active / "canary.txt", runtime / "future" / "canary.txt", runtime):
        with pytest.raises(HostFilePolicyError, match="protected host"):
            host.guard_write(extra, str(target))
    host.guard_read(extra, str(active / "canary.txt"))
    host.guard_write(extra, "ordinary.txt")
    with pytest.raises(ConfigError, match="trusted host runtime"):
        policy(folders, settings=ShellSandboxSettings(additional_workspace_roots=(str(runtime),)))
    assert (active / "canary.txt").read_text() == "unchanged"
    assert not (runtime / "future").exists()


@pytest.mark.parametrize("backend", ["docker"])
def test_unsupported_backend_does_not_drop_roots_or_fall_back(tmp_path, backend):
    settings = ShellSandboxSettings(
        backend=backend, unavailable_fallback="ask", additional_workspace_roots=(str(tmp_path),)
    )
    with pytest.raises(ConfigError, match="Additional|additional"):
        select_shell_sandbox_backend(
            settings, system="Darwin", which=lambda _: "/bin/docker", path_exists=lambda _: True
        )


def test_cli_add_dir_is_repeatable_and_invocation_scoped(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from alysis_code import capabilities
    from alysis_code.cli_impl.commands.root import app

    monkeypatch.delenv(ADDITIONAL_WORKSPACE_ROOTS_ENV, raising=False)
    observed = []

    def payload():
        observed.append(
            resolve_shell_sandbox_settings(AppConfig(model="test")).additional_workspace_roots
        )
        return {"ok": True}

    monkeypatch.setattr(capabilities, "build_capabilities_payload", payload)
    result = CliRunner().invoke(
        app, ["--add-dir", str(tmp_path), "--add-dir", str(tmp_path), "capabilities"]
    )
    assert result.exit_code == 0, result.output
    assert observed == [(str(tmp_path),)]
    assert ADDITIONAL_WORKSPACE_ROOTS_ENV not in os.environ
    assert CliRunner().invoke(app, ["capabilities"]).exit_code == 0
    assert observed[-1] == ()


def test_tool_dispatch_accepts_only_captured_secondary_cwd(folders, monkeypatch):
    import subprocess

    from alysis_code.agent.errors import AgentRuntimeError
    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.session_store import SessionStore
    from alysis_code.surface import ApprovalDecision
    from alysis_code.surface.noop_surface import NoopSurface
    from alysis_code.windows_mxc import WindowsMxcRunner

    primary, secondary, outside, _ = folders
    settings = ShellSandboxSettings(
        backend="windows-mxc", additional_workspace_roots=(str(secondary),)
    )
    calls = []

    def run(self, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(kwargs["cmd"], 0, "captured", "")

    monkeypatch.setattr(WindowsMxcRunner, "run", run)
    captured = LazyShellRunner(lambda: WindowsMxcRunner(settings), settings=settings)
    surface = NoopSurface()
    monkeypatch.setattr(
        surface, "request_approval", lambda request: ApprovalDecision(request.kind == "shell_run")
    )
    tools = build_tools(
        root=primary,
        console=None,
        surface=surface,
        store=SessionStore(
            enabled=False,
            cwd=str(primary),
            repo_root=str(primary),
            sessions_dir=primary.parent / "sessions",
            session_id="multi-root-test",
        ),
        mode="fullaccess",
        yes=True,
        shell_runner=captured,
        verification_enabled=False,
    )
    monkeypatch.setenv(ADDITIONAL_WORKSPACE_ROOTS_ENV, json.dumps([str(outside)]))
    result = tools["shell_run"].run({"cmd": "echo captured", "cwd": str(secondary)})
    assert result["exit_code"] == 0
    assert calls[0]["cwd"] == secondary
    with pytest.raises(AgentRuntimeError, match="escapes"):
        tools["shell_run"].run({"cmd": "echo forbidden", "cwd": str(outside)})
    assert len(calls) == 1
