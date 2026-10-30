"""Bounded launch-policy regressions; no native helper or real private state."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code import windows_private_state, windows_sandbox
from alysis_code.config import ConfigError
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.parametrize("runtime_name", ["venv", "exec", "base", "base-exec"])
@pytest.mark.parametrize(
    "workspace_relation", ["contains-runtime", "inside-runtime", "cwd-inside-runtime"]
)
def test_host_python_runtime_cannot_become_a_writable_workspace(
    tmp_path, monkeypatch, runtime_name, workspace_relation
):
    """The host may import code from either its venv or its base after a tool exits."""
    container = tmp_path / "selected-tree"
    runtime = container / runtime_name
    package = runtime / "Lib" / "site-packages" / "synthetic_package"
    package.mkdir(parents=True)
    host_import = package / "late_import.py"
    host_import.write_text("VALUE = 'synthetic-original'\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    other_runtime = outside / "other-runtime"
    other_runtime.mkdir()
    prefixes = dict.fromkeys(
        ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix"), str(other_runtime)
    )
    prefixes[
        {
            "venv": "prefix",
            "exec": "exec_prefix",
            "base": "base_prefix",
            "base-exec": "base_exec_prefix",
        }[runtime_name]
    ] = str(runtime)
    monkeypatch.setattr(
        windows_sandbox,
        "sys",
        SimpleNamespace(
            **prefixes,
            executable=str(other_runtime / "Scripts" / "python.exe"),
        ),
    )
    # No host interpreter, PowerShell query, helper, home or config is inspected.
    monkeypatch.setattr(windows_sandbox, "_powershell_execution_policy", lambda _: "RemoteSigned")
    home = outside / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config = outside / "config"
    data = outside / "data"
    config.mkdir()
    data.mkdir()
    monkeypatch.setattr(windows_private_state.config, "config_path", lambda: config / "config.json")
    monkeypatch.setattr(windows_private_state.config, "_data_dir", lambda: data)
    for prefix in ("ALYSIS", "SYLLIPTOR"):
        for name in ("DESKTOP_CONTROL_PATHS", "DESKTOP_USER_DATA"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    helper_dir = outside / "helper"
    helper_dir.mkdir()
    helper = helper_dir / "synthetic-helper.exe"
    helper.write_bytes(b"never executed")
    root = package if workspace_relation == "inside-runtime" else container
    cwd = package if workspace_relation == "cwd-inside-runtime" else root

    def build_request():
        return windows_sandbox.build_request(
            ShellSandboxSettings(backend="windows-mxc", filesystem_access="workspace-write"),
            root=root,
            cwd=cwd,
            cmd="Write-Output 'synthetic command; never executed'",
            helper=helper,
        )

    if workspace_relation != "contains-runtime":
        with pytest.raises(ConfigError, match="runtime"):
            build_request()
        assert not (root / "AGENTS.md").exists()
        assert host_import.read_text(encoding="utf-8") == "VALUE = 'synthetic-original'\n"
        return
    request = build_request()
    windows_private_state.protect_private_state(request)
    assert any(host_import.is_relative_to(Path(grant)) for grant in request["write"])
    assert any(
        host_import.is_relative_to(Path(denied))
        for denied in (*request["deny_write"], *request["deny_read"])
    ), f"Trusted host import remains writable: {runtime_name}, {workspace_relation}"
    assert host_import.read_text(encoding="utf-8") == "VALUE = 'synthetic-original'\n"
