"""Language-independent discovery and private HOME projection."""

from __future__ import annotations

import json
import os
import shlex
import sys
import uuid
from pathlib import Path

import pytest

from alysis_code.macos_sandbox import project_read_paths, runtime_read_paths
from alysis_code.posix_runtime_paths import path_runtime_paths
from alysis_code.sandbox_runner import SeatbeltShellRunner


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nprintf runtime-ok\n")
    path.chmod(0o755)
    return path


def test_unknown_executables_aliases_and_code_dirs_without_adjacent_data(tmp_path):
    prefix = tmp_path / "installation"
    tool = executable(prefix / "bin" / ("工具-" + uuid.uuid4().hex))
    library = prefix / "lib"
    library.mkdir()
    alias = tmp_path / "launchers" / "alias"
    alias.parent.mkdir()
    alias.symlink_to(tool)
    secret = tool.parent / "private-config"
    secret.write_text("synthetic")
    (alias.parent / "subdirectory").mkdir()
    paths = runtime_read_paths(str(alias.parent) + os.pathsep + str(tool.parent))
    assert {tool, alias, library} <= set(paths)
    assert all(not secret.is_relative_to(p) for p in paths)
    assert alias.parent / "subdirectory" not in paths


def test_system_path_alias_discovers_external_installation(tmp_path, require_symlinks):
    system = tmp_path / "system"
    alias = system / "bin" / uuid.uuid4().hex
    alias.parent.mkdir(parents=True)
    tool = executable(tmp_path / "installation" / "bin" / alias.name)
    library = tool.parent.parent / "lib"
    library.mkdir()
    alias.symlink_to(tool)
    ordinary = executable(alias.parent / "already-mounted")
    paths = path_runtime_paths(str(alias.parent), system_paths=(system,))
    assert {tool, library} <= set(paths)
    assert alias not in paths and ordinary not in paths


def test_discovered_code_directory_includes_canonical_target(tmp_path, require_symlinks):
    tool = executable(tmp_path / "installation" / "bin" / uuid.uuid4().hex)
    target = tool.parent.parent / "lib-platform"
    target.mkdir()
    library = tool.parent.parent / "lib"
    library.symlink_to(target.name, target_is_directory=True)
    paths = path_runtime_paths(str(tool.parent))
    assert {tool, library, target} <= set(paths)


def test_system_path_directory_alias_discovers_regular_external_executables(
    tmp_path, require_symlinks
):
    system = tmp_path / "system"
    system.mkdir()
    tool = executable(tmp_path / "installation" / "bin" / uuid.uuid4().hex)
    (system / "bin").symlink_to(tool.parent, target_is_directory=True)
    paths = path_runtime_paths(str(system / "bin"), system_paths=(system,))
    assert tool in paths


def test_workspace_path_entries_cannot_promote_external_symlinks(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    tool = executable(tmp_path / "host-install" / "bin" / uuid.uuid4().hex)
    (workspace / "alias").symlink_to(tool)
    outside_alias = tmp_path / "workspace-alias"
    outside_alias.symlink_to(workspace, target_is_directory=True)
    monkeypatch.chdir(workspace)
    paths = runtime_read_paths(
        os.pathsep.join(("", ".", "../host-install/bin", str(workspace), str(outside_alias))),
        workspace_roots=(workspace,),
    )
    assert all(not tool.is_relative_to(p.resolve()) for p in paths)


def test_unusable_path_entries_do_not_hide_other_installed_tools(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    missing = tmp_path / "missing"
    tool = executable(tmp_path / "installation" / "bin" / uuid.uuid4().hex)
    paths = runtime_read_paths(os.pathsep.join(map(str, (loop, missing, tool, tool.parent))))
    assert tool in paths
    assert loop not in paths
    assert missing not in paths


@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
def test_unusable_path_entry_keeps_execution_contained(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    private = tmp_path / "private"
    private.write_text("synthetic")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setenv("PATH", str(loop) + os.pathsep + os.defpath)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    command = shlex.join(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            f"from pathlib import Path; Path({str(private)!r}).read_text()",
        ]
    )
    result = SeatbeltShellRunner().run(root=project, cwd=project, cmd=command, timeout_s=10)
    assert result.returncode != 0
    assert "PermissionError" in result.stderr
    assert private.read_text() == "synthetic"


def test_projection_does_not_write_through_links_or_replace_staged_files(tmp_path):
    user_home, scratch = tmp_path / "user", tmp_path / "scratch"
    scratch.mkdir()
    runtime = user_home / "custom" / "sdk"
    runtime.mkdir(parents=True)
    (runtime / "data").write_text("runtime")
    (user_home / "config").write_text("host")
    (scratch / "config").write_text("staged")
    project_read_paths(
        scratch,
        (runtime / "absent" / "child", runtime, user_home / "config", user_home / ".." / "escape"),
        user_home=user_home,
    )
    assert (scratch / "custom" / "sdk" / "data").read_text() == "runtime"
    assert (scratch / "config").read_text() == "staged"
    assert not (runtime / "absent").exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
def test_home_relative_runtime_and_exact_file_grants_are_scoped(tmp_path, monkeypatch):
    user_home, project = tmp_path / "user", tmp_path / "project"
    project.mkdir()
    runtime = user_home / ".unknown-tool" / "sdk"
    runtime.mkdir(parents=True)
    (runtime / "data").write_text("runtime")
    config = runtime.parent / "approved-config"
    config.write_text("synthetic-approved")
    private = runtime.parent / "unapproved-config"
    private.write_text("synthetic-private")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", "[]")
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    source = f"""import json,os
from pathlib import Path
home=Path.home()/'.unknown-tool'
out={{'runtime':os.access(home/'sdk/data',os.R_OK),
      'approved':os.access(home/'approved-config',os.R_OK),
      'private':os.access({str(private)!r},os.R_OK)}}
for key,path in [('sdk_write',home/'sdk/changed'),('cache_write',home/'cache/data')]:
 try: path.parent.mkdir(parents=True,exist_ok=True); path.write_text('new'); out[key]=True
 except PermissionError: out[key]=False
print(json.dumps(out))
"""
    cmd = shlex.join([sys.executable, "-I", "-B", "-c", source])
    for granted in (True, False):
        runner = SeatbeltShellRunner(
            additional_read_roots=(str(runtime),) if granted else (),
            additional_read_files=(str(config),) if granted else (),
        )
        result = runner.run(root=project, cwd=project, cmd=cmd, timeout_s=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "runtime": granted,
            "approved": granted,
            "private": False,
            "sdk_write": not granted,
            "cache_write": True,
        }
    assert not (runtime / "changed").exists()
    assert not (runtime.parent / "cache").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
def test_workspace_executable_alias_cannot_read_host_target(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    tool = executable(tmp_path / "host" / "bin" / uuid.uuid4().hex)
    (project / "host-tool").symlink_to(tool)
    monkeypatch.setenv("PATH", str(project) + os.pathsep + os.defpath)
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    result = SeatbeltShellRunner().run(root=project, cwd=project, cmd="host-tool", timeout_s=10)
    assert result.returncode != 0
    assert "runtime-ok" not in result.stdout


def test_homebrew_openssl_configuration_does_not_grant_adjacent_settings(monkeypatch):
    original_exists = Path.exists
    configurations = {
        Path(prefix) / "etc/openssl@3" / name
        for prefix in ("/usr/local", "/opt/homebrew")
        for name in ("openssl.cnf", "cert.pem")
    }
    monkeypatch.setattr(
        Path, "exists", lambda path: path in configurations or original_exists(path)
    )
    paths = runtime_read_paths("")
    assert configurations <= set(paths)
    for config in configurations:
        assert not any(config.parent.is_relative_to(path) for path in paths)
