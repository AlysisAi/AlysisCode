"""Opt-in live WSL checks, using only temporary canaries and local listeners."""

from __future__ import annotations

import os
import platform
import shlex
import socket
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code.background_runner import build_background_shell_runner_from_settings
from alysis_code.config import AppConfig, ConfigError
from alysis_code.sandbox_runner import BwrapShellRunner, build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.verify_gate import run_task_verification
from alysis_code.windows_sandbox import resolve_helper

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux"
    or "microsoft" not in platform.release().lower()
    or os.environ.get("ALYSIS_RUN_INSTALLED_WSL_TESTS") != "1",
    reason="requires explicit live WSL sandbox testing",
)


@pytest.fixture(params=["linux", "drvfs"])
def workspace(request, tmp_path):
    parent = tmp_path
    if request.param == "drvfs":
        repository = Path(__file__).resolve().parents[1]
        if not str(repository).startswith("/mnt/"):
            pytest.skip("DrvFS check requires a repository under /mnt/<drive>")
        parent = repository / "native/windows-sandbox/target/wsl-canaries"
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="alysis-wsl-", dir=parent) as temporary:
        root = Path(temporary) / "workspace"
        root.mkdir()
        yield root


def runner(root):
    result = build_shell_runner_from_settings(ShellSandboxSettings(), root)
    assert isinstance(result, BwrapShellRunner), "WSL auto selection must use Linux isolation"
    return result


@pytest.mark.parametrize("access", ["workspace-write", "read-only"])
def test_wsl_multiple_workspaces_keep_real_paths_and_deny_unrelated(workspace, access):
    second = workspace.parent / "second folder"
    second.mkdir()
    outside = workspace.parent / "unrelated"
    outside.mkdir()
    (outside / "canary").write_text("unchanged")
    for root in (workspace, second):
        (root / "AGENTS.md").write_text("original instructions")
        (root / ".git").mkdir()
        (root / ".git/config").write_text("original metadata")
    settings = replace(
        ShellSandboxSettings(), filesystem_access=access, additional_workspace_roots=(str(second),)
    )
    script = (
        "import pathlib,subprocess,sys\n"
        "roots=" + repr([str(workspace), str(second)]) + "\n"
        "def denied(p):\n"
        " try: pathlib.Path(p).write_text('bad')\n"
        " except PermissionError: return\n"
        " except OSError as e:\n"
        "  if e.errno in (2,13,30): return\n"
        "  raise\n"
        " raise AssertionError('escaped '+p)\n"
        "for root in roots:\n"
        + (
            " pathlib.Path(root,'output').write_text('ok')\n"
            if access == "workspace-write"
            else " denied(str(pathlib.Path(root,'output')))\n"
        )
        + " for name in ['AGENTS.md','.git/config','.codex/unsafe']:\n"
        "  denied(str(pathlib.Path(root,name)))\n"
        "denied(" + repr(str(outside / "canary")) + ")\n"
        "if len(sys.argv)==1: subprocess.run([sys.executable,sys.argv[0],'child'],check=True)\n"
        "print('MULTI_WSL_OK')\n"
    )
    probe = workspace / "multi.py"
    probe.write_text(script)
    command = "python3 " + shlex.quote(str(probe))
    result = build_shell_runner_from_settings(settings, workspace).run(
        root=workspace, cwd=second, cmd=command, timeout_s=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "MULTI_WSL_OK" in result.stdout
    assert (outside / "canary").read_text() == "unchanged"
    for root in (workspace, second):
        assert (root / "AGENTS.md").read_text() == "original instructions"
        assert (root / ".git/config").read_text() == "original metadata"


def test_wsl_nested_workspace_metadata_cannot_escape_through_primary_alias(workspace):
    second = workspace / "engine"
    second.mkdir()
    (second / ".git").mkdir()
    (second / ".git/config").write_text("original")
    (second / "AGENTS.md").write_text("instructions")
    settings = ShellSandboxSettings(backend="bwrap", additional_workspace_roots=(str(second),))
    script = """import errno
from pathlib import Path
for root in (Path('/workspace/engine'), Path.cwd()):
    (root / 'ordinary').write_text('allowed')
    for relative in ('.git/config', 'AGENTS.md'):
        try:
            (root / relative).write_text('ESCAPED')
        except OSError as error:
            assert error.errno == errno.EROFS, error
        else:
            raise AssertionError('protected metadata escaped: ' + str(root / relative))
print('NESTED_PROTECTED')
"""
    result = build_shell_runner_from_settings(settings, workspace).run(
        root=workspace, cwd=second, cmd="/usr/bin/python3 -c " + shlex.quote(script), timeout_s=30
    )
    assert result.returncode == 0, result.stderr
    assert "NESTED_PROTECTED" in result.stdout
    assert (second / ".git/config").read_text() == "original"
    assert (second / "AGENTS.md").read_text() == "instructions"


@pytest.mark.parametrize("grant", ["control", "parent", "missing"])
def test_wsl_additional_folders_cannot_grant_desktop_private_state(workspace, monkeypatch, grant):
    import json

    desktop = workspace.parent / "desktop-state"
    control = desktop / "alysis-chats"
    control.mkdir(parents=True)
    state = control / "grant.json"
    state.write_text("private")
    monkeypatch.setenv("ALYSIS_DESKTOP_USER_DATA", str(desktop))
    protected = desktop / "future-state" if grant == "missing" else control
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(protected)]))
    selected = desktop if grant in {"parent", "missing"} else control
    settings = ShellSandboxSettings(backend="bwrap", additional_workspace_roots=(str(selected),))
    with pytest.raises(ConfigError, match="private|application"):
        build_shell_runner_from_settings(settings, workspace).run(
            root=workspace, cwd=workspace, cmd="touch MUST_NOT_RUN", timeout_s=30
        )
    assert state.read_text() == "private"
    assert not (workspace / "MUST_NOT_RUN").exists()


@pytest.mark.parametrize("exists", [True, False])
def test_wsl_desktop_runtime_parent_is_protected_without_extra_folders(
    workspace, monkeypatch, exists
):
    import json

    runtime = workspace / "runtime-generations"
    if exists:
        runtime.mkdir()
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", json.dumps([str(runtime)]))
    with pytest.raises(ConfigError, match="trusted runtime"):
        build_shell_runner_from_settings(ShellSandboxSettings(backend="bwrap"), workspace).run(
            root=workspace, cwd=workspace, cmd="touch MUST_NOT_RUN", timeout_s=30
        )
    assert not (workspace / "MUST_NOT_RUN").exists()
    assert runtime.exists() == exists


@pytest.mark.parametrize("source", ["configuration", "data", "desktop-control"])
def test_wsl_primary_file_worker_refuses_private_state_without_extended_roots(
    workspace, monkeypatch, source
):
    import json

    from alysis_code import config
    from alysis_code.contained_read_tools import ContainedReadTools

    project = workspace / "ordinary-project"
    project.mkdir()
    private = workspace / "private"
    private.mkdir()
    canary = private / "canary.txt"
    canary.write_text("SYNTHETIC_PRIVATE_CANARY")
    for prefix in ("ALYSIS", "SYLLIPTOR"):
        for name in ("DESKTOP_CONTROL_PATHS", "DESKTOP_USER_DATA", "DESKTOP_RUNTIME_ROOTS"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    if source == "configuration":
        monkeypatch.setattr(config, "config_path", lambda: private / "config.json")
    elif source == "data":
        monkeypatch.setattr(config, "_data_dir", lambda: private)
    else:
        monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", json.dumps([str(private)]))
    worker = ContainedReadTools(
        ShellSandboxSettings(backend="bwrap", additional_workspace_roots=()), workspace
    )
    # Reject the whole effective mount before a narrow reader can be raced from
    # ordinary-project to private inside /workspace. No worker may see the canary.
    with pytest.raises(ConfigError, match="private host state"):
        worker.run("search_rg", {"root_path": project.name, "pattern": "SYNTHETIC_PRIVATE"})
    with pytest.raises(ConfigError, match="private host state"):
        worker.run_write("fs_mkdir", {"path": "ordinary-project/MUST_NOT_RUN"})
    assert not (project / "MUST_NOT_RUN").exists()
    assert canary.read_text() == "SYNTHETIC_PRIVATE_CANARY"
    assert not list(workspace.glob(".alysis-file-request-*"))


def test_wsl_workspace_write_and_metadata_protection(workspace):
    metadata = workspace / ".git"
    metadata.mkdir()
    config = metadata / "config"
    config.write_text("protected")
    result = runner(workspace).run(
        root=workspace, cwd=workspace, cmd="printf allowed > output.txt", timeout_s=30
    )
    assert result.returncode == 0, result.stderr
    assert (workspace / "output.txt").read_text() == "allowed"
    result = runner(workspace).run(
        root=workspace, cwd=workspace, cmd="printf changed > .git/config", timeout_s=30
    )
    assert result.returncode != 0
    assert "Read-only file system" in result.stderr
    assert config.read_text() == "protected"


def test_wsl_private_sibling_and_windows_mount_are_hidden(workspace, monkeypatch):
    secret = workspace.parent / "private.txt"
    secret.write_text("private-test-canary")
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-canary")
    result = runner(workspace).run(
        root=workspace,
        cwd=workspace,
        cmd=(
            f"test ! -e {shlex.quote(str(secret))} && "
            "test ! -e /mnt/c/Windows/System32/cmd.exe && "
            'test -z "$OPENAI_API_KEY" && printf ISOLATED'
        ),
        timeout_s=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ISOLATED"
    assert secret.read_text() == "private-test-canary"


def test_wsl_network_namespace_is_separate(workspace):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)):
            connection, _ = listener.accept()
            connection.close()
        script = (
            "import os,socket; "
            f"assert os.readlink('/proc/self/ns/net') != {os.readlink('/proc/self/ns/net')!r}; "
            "s=socket.socket(); s.settimeout(1); "
            f"assert s.connect_ex(('127.0.0.1', {port})) != 0; print('ISOLATED')"
        )
        result = runner(workspace).run(
            root=workspace,
            cwd=workspace,
            cmd="/usr/bin/python3 -c " + shlex.quote(script),
            timeout_s=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ISOLATED"
        with pytest.raises(socket.timeout):
            listener.accept()


def test_wsl_cannot_select_windows_helper():
    with pytest.raises(ConfigError, match="WSL2"):
        resolve_helper()


def test_wsl_readonly_cwd_aliases_do_not_bypass_workspace_mount(workspace):
    (workspace / "input.txt").write_text("protected")
    settings = ShellSandboxSettings(backend="bwrap", filesystem_access="read-only")
    readonly = build_shell_runner_from_settings(settings, workspace)
    alias = workspace / "cwd-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    result = readonly.run(
        root=workspace,
        cwd=alias,
        cmd="printf forbidden > input.txt",
        timeout_s=30,
    )
    assert result.returncode != 0
    assert "Read-only file system" in result.stderr
    outside = workspace / "outside-cwd"
    outside.symlink_to(workspace.parent, target_is_directory=True)
    with pytest.raises(RuntimeError, match="outside root"):
        readonly.run(root=workspace, cwd=outside, cmd="touch forbidden", timeout_s=30)
    assert (workspace / "input.txt").read_text() == "protected"
    assert not (workspace.parent / "forbidden").exists()


READONLY_TEST = """import errno
import os
import tempfile
import unittest
from pathlib import Path

class ReadOnlyWorkspace(unittest.TestCase):
    def test_filesystem_ceiling(self):
        root = Path.cwd()
        self.assertEqual((root / "input.txt").read_text(), "readable")
        for operation in (
            lambda: (root / "new.txt").write_text("forbidden"),
            lambda: (root / "input.txt").write_text("forbidden"),
            lambda: (root / "input.txt").unlink(),
            lambda: (root / "input.txt").rename(root / "renamed.txt"),
            lambda: (root / ".git/config").write_text("forbidden"),
        ):
            with self.assertRaises(OSError) as caught:
                operation()
            self.assertEqual(caught.exception.errno, errno.EROFS)
        # A runtime discovered beneath the workspace can expose a second host-path
        # mount; that alias must be read-only too.
        alias = Path(os.environ["CARGO_HOME"]) / "alias.txt"
        self.assertEqual(alias.read_text(), "alias-readable")
        with self.assertRaises(OSError) as caught:
            alias.write_text("forbidden")
        self.assertEqual(caught.exception.errno, errno.EROFS)
        with tempfile.TemporaryFile() as temporary:
            temporary.write(b"ephemeral scratch remains available")
        print("WSL_READONLY_CONFIRMED", flush=True)
"""


@pytest.mark.parametrize("execution", ["foreground", "background", "verification"])
def test_wsl_readonly_workspace_across_execution_paths(workspace, monkeypatch, execution):
    (workspace / "input.txt").write_text("readable")
    (workspace / ".git").mkdir()
    (workspace / ".git/config").write_text("protected")
    cargo_home = workspace / "toolchain"
    (cargo_home / "bin").mkdir(parents=True)
    (cargo_home / "alias.txt").write_text("alias-readable")
    cargo = cargo_home / "bin/cargo"
    cargo.write_text("#!/bin/sh\nexit 0\n")
    cargo.chmod(0o755)
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setenv("PATH", str(cargo_home / "bin") + ":" + os.environ["PATH"])
    (workspace / "test_readonly.py").write_text(READONLY_TEST)
    settings = ShellSandboxSettings(backend="bwrap", filesystem_access="read-only")
    cmd = "/usr/bin/python3 -B -m unittest -v test_readonly.py"
    if execution == "foreground":
        result = build_shell_runner_from_settings(settings, workspace).run(
            root=workspace,
            cwd=workspace,
            cmd=cmd,
            timeout_s=30,
        )
        assert result.returncode == 0, result.stderr
        assert "WSL_READONLY_CONFIRMED" in result.stdout
    elif execution == "background":
        spawn = build_background_shell_runner_from_settings(settings, workspace).start(
            root=workspace,
            cwd=workspace,
            cmd=cmd,
            env_overrides={"ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS": "workspace-write"},
        )
        try:
            stdout, stderr = spawn.popen.communicate(timeout=30)
            assert spawn.popen.returncode == 0, stderr
            assert b"WSL_READONLY_CONFIRMED" in stdout
        finally:
            if spawn.popen.poll() is None:
                spawn.popen.kill()
                spawn.popen.communicate(timeout=5)
            spawn.cleanup()
    else:
        monkeypatch.setenv("ALYSIS_VERIFY_SANDBOX_MODE", "strict")
        monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "bwrap")
        monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS", "read-only")
        result = run_task_verification(
            root=workspace,
            commands=[cmd],
            artifact_path=workspace.parent / "verification.md",
            cfg=AppConfig(model="fixture"),
            timeout_s=30,
        )
        assert result.all_passed, result.summary
        assert "WSL_READONLY_CONFIRMED" in (workspace.parent / "verification.md").read_text()
        assert all(not item.fallback_used for item in result.command_results)
    assert (workspace / "input.txt").read_text() == "readable"
    assert (workspace / ".git/config").read_text() == "protected"
    assert (cargo_home / "alias.txt").read_text() == "alias-readable"
    assert not (workspace / "new.txt").exists()
    assert not (workspace / "renamed.txt").exists()
