from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

import alysis_code.custom_tools.runtime as runtime_module
from alysis_code.custom_tools.discovery import CustomToolSpec, discover_custom_tools
from alysis_code.custom_tools.runtime import _worker_command, run_custom_tool
from alysis_code.custom_tools.trust import (
    ProjectToolTrustState,
    save_trust_state,
    trust_project_tool,
)
from alysis_code.tools.registry import iter_builtin_tool_metadata


def _built_in_tool_names() -> set[str]:
    return {spec.name.casefold() for spec in iter_builtin_tool_metadata()}


def _write_tool(
    workspace: Path,
    filename: str,
    *,
    name: str,
    body: str,
    extra_manifest_lines: list[str] | None = None,
) -> Path:
    extra_manifest_lines = list(extra_manifest_lines or [])
    path = workspace / ".alysis/tools" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "TOOL = {",
        f'    "name": "{name}",',
        '    "description": "Runtime test tool",',
        '    "input_schema": {"type": "object", "properties": {}, "required": []},',
    ]
    manifest_lines.extend(f"    {line}" for line in extra_manifest_lines)
    manifest_lines.append("}")
    path.write_text(
        "\n".join(
            [
                *manifest_lines,
                "",
                "def run(args):",
                *[f"    {line}" for line in body.splitlines()],
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_global_tool(
    config_dir: Path,
    filename: str,
    *,
    name: str,
    body: str,
    extra_manifest_lines: list[str] | None = None,
) -> Path:
    path = config_dir / "tools" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    extra_manifest_lines = list(extra_manifest_lines or [])
    manifest_lines = [
        "TOOL = {",
        f'    "name": "{name}",',
        '    "description": "Runtime test tool",',
        '    "input_schema": {"type": "object", "properties": {}, "required": []},',
    ]
    manifest_lines.extend(f"    {line}" for line in extra_manifest_lines)
    manifest_lines.append("}")
    path.write_text(
        "\n".join(
            [
                *manifest_lines,
                "",
                "def run(args):",
                *[f"    {line}" for line in body.splitlines()],
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _configure_trust_dir(workspace: Path, monkeypatch) -> Path:
    config_dir = workspace.parent / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(config_dir))
    return config_dir


def _discover_project_spec(workspace: Path, *, name: str | None = None) -> CustomToolSpec:
    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )
    assert not result.issues
    tools = {tool.name: tool for tool in result.project_tools}
    if name is not None:
        return tools[name]
    assert len(result.project_tools) == 1
    return result.project_tools[0]


def _discover_global_spec(
    *,
    workspace: Path,
    config_dir: Path,
    name: str | None = None,
) -> CustomToolSpec:
    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
        user_config_dir=config_dir,
    )
    assert not result.issues
    tools = {tool.name: tool for tool in result.global_tools}
    if name is not None:
        return tools[name]
    assert len(result.global_tools) == 1
    return result.global_tools[0]


def _trust_project_spec(spec: CustomToolSpec) -> CustomToolSpec:
    trust_project_tool(spec)
    return spec


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return _windows_pid_is_alive(pid)
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            fields = proc_stat.read_text(encoding="utf-8").split()
        except OSError:
            fields = []
        if len(fields) > 2 and fields[2] == "Z":
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_pid_is_alive(pid: int) -> bool:  # pragma: no cover - exercised on Windows
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def _wait_for_pid_exit(pid: int, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_alive(pid)


def _wait_for_file(path: Path, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


def _symlink_to_or_skip(
    link_path: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link_path.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError:
        pytest.skip("symlink creation is not supported in this test environment")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("symlink creation requires Windows Developer Mode or elevated privilege")
        raise


def test_runtime_worker_command_does_not_use_dash_m_runtime_module() -> None:
    command = _worker_command()

    assert ["-m", "alysis_code.custom_tools.runtime"] not in [
        command[index : index + 2] for index in range(len(command) - 1)
    ]


def test_runtime_worker_command_uses_isolated_startup() -> None:
    command = _worker_command()
    source_root = os.fspath(Path(runtime_module.__file__).resolve().parents[2])

    assert "-I" in command
    assert ["-m", "alysis_code.custom_tools.runtime"] not in [
        command[index : index + 2] for index in range(len(command) - 1)
    ]
    assert "-c" in command
    bootstrap = command[command.index("-c") + 1]
    assert "PYTHONPATH" not in bootstrap
    assert "sys.path.insert" in bootstrap
    assert "spec_from_file_location" in bootstrap
    assert repr(source_root) in bootstrap
    assert "runtime.py" in bootstrap


def test_runtime_worker_bootstrap_env_does_not_include_host_pythonpath(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/tmp/host-pythonpath")

    env = runtime_module._build_worker_bootstrap_env(
        {
            "PATH": "/usr/bin",
            "ALYSIS_WORKSPACE_ROOT": "/workspace",
            "ALYSIS_SESSION_ID": "session-1",
            "ALYSIS_TOOL_PATH": "/workspace/.alysis/tools/tool.py",
            "ALYSIS_TOOL_SCOPE": "project",
            "ALYSIS_TOOL_NAME": "tool",
        }
    )

    assert "PYTHONPATH" not in env


def test_runtime_default_execution_uses_worker_subprocess(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "success.py",
        name="success_tool",
        body=(
            "import os\n"
            "return {"
            "'cwd': os.getcwd(), "
            "'workspace': os.environ['ALYSIS_WORKSPACE_ROOT'], "
            "'pid': os.getpid()"
            "}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert result["result"]["cwd"] == os.fspath(workspace.resolve())
    assert result["result"]["workspace"] == os.fspath(workspace.resolve())
    assert result["result"]["pid"] != os.getpid()


def test_runtime_workspace_sitecustomize_does_not_run_before_tool_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    _configure_trust_dir(workspace, monkeypatch)
    sentinel = "WORKSPACE_SITECUSTOMIZE_SENTINEL"
    side_effect = workspace / "sitecustomize-ran.txt"
    (workspace / "sitecustomize.py").write_text(
        (
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"print({sentinel!r}, file=sys.stderr)\n"
            "Path(os.environ.get('ALYSIS_WORKSPACE_ROOT', '.'), "
            "'sitecustomize-ran.txt').write_text('ran', encoding='utf-8')\n"
        ),
        encoding="utf-8",
    )
    _write_tool(
        workspace,
        "quiet.py",
        name="quiet_tool",
        body="return {'ok': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )
    serialized = json.dumps(result, ensure_ascii=True, sort_keys=True)

    assert result["success"] is True
    assert not side_effect.exists()
    assert sentinel not in serialized
    assert "stdout_artifact_path" not in result
    assert "stderr_artifact_path" not in result


def test_runtime_host_pythonpath_sitecustomize_does_not_run_before_tool_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_pythonpath = tmp_path / "host-pythonpath"
    host_pythonpath.mkdir()
    _configure_trust_dir(workspace, monkeypatch)
    sentinel = "HOST_PYTHONPATH_SITECUSTOMIZE_SENTINEL"
    side_effect = workspace / "host-pythonpath-sitecustomize-ran.txt"
    (host_pythonpath / "sitecustomize.py").write_text(
        (
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"print({sentinel!r}, file=sys.stderr)\n"
            "Path(os.environ.get('ALYSIS_WORKSPACE_ROOT', '.'), "
            "'host-pythonpath-sitecustomize-ran.txt').write_text('ran', encoding='utf-8')\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", os.fspath(host_pythonpath))
    _write_tool(
        workspace,
        "quiet.py",
        name="quiet_tool",
        body="return {'ok': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )
    serialized = json.dumps(result, ensure_ascii=True, sort_keys=True)

    assert result["success"] is True
    assert not side_effect.exists()
    assert sentinel not in serialized
    assert "stdout_artifact_path" not in result
    assert "stderr_artifact_path" not in result


def test_runtime_success_without_stderr_does_not_create_stderr_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "quiet.py",
        name="quiet_tool",
        body="return {'ok': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )
    serialized = json.dumps(result, ensure_ascii=True, sort_keys=True)

    assert result["success"] is True
    assert result["stderr_preview"] == ""
    assert result["stderr_truncated"] is False
    assert "stderr_artifact_path" not in result
    assert "RuntimeWarning" not in serialized
    assert "runpy" not in serialized


def test_runtime_busy_loop_times_out_and_host_remains_usable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "loop.py",
        name="loop_tool",
        body="while True:\n    pass",
        extra_manifest_lines=['"timeout_s": 0.05,'],
    )
    _write_tool(
        workspace,
        "ok.py",
        name="ok_tool",
        body="return {'ok': True}",
    )
    loop_spec = _trust_project_spec(_discover_project_spec(workspace, name="loop_tool"))
    ok_spec = _trust_project_spec(_discover_project_spec(workspace, name="ok_tool"))

    timed_out = run_custom_tool(
        spec=loop_spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )
    after = run_custom_tool(
        spec=ok_spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert timed_out["success"] is False
    assert timed_out["timeout"] is True
    assert "timed out" in timed_out["error"]
    assert timed_out["error_info"]["type"] != "WorkerProcessTreeGuardError"
    assert after["success"] is True
    assert after["result"]["ok"] is True


def test_runtime_timeout_kills_descendant_process(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    pid_file = workspace / "child.pid"
    _write_tool(
        workspace,
        "timeout_child.py",
        name="timeout_child_tool",
        body=(
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n"
            "workspace = Path(os.environ['ALYSIS_WORKSPACE_ROOT'])\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            '"import time; time.sleep(3600)"])\n'
            "(workspace / 'child.pid').write_text(str(child.pid), encoding='utf-8')\n"
            "while True:\n"
            "    pass"
        ),
        extra_manifest_lines=[
            # Allow worker startup and descendant creation on Windows before timing out.
            '"timeout_s": 3.0,',
            '"capabilities": {"filesystem": {"write": "workspace"}, '
            '"process_spawn": "unrestricted"},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["timeout"] is True
    assert "timed out" in result["error"]
    assert pid_file.exists()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    assert _wait_for_pid_exit(child_pid)


def test_runtime_successful_tool_background_child_is_cleaned_up(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    pid_file = workspace / "background-child.pid"
    _write_tool(
        workspace,
        "background_child.py",
        name="background_child_tool",
        body=(
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n"
            "workspace = Path(os.environ['ALYSIS_WORKSPACE_ROOT'])\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            '"import time; time.sleep(3600)"])\n'
            "(workspace / 'background-child.pid').write_text(str(child.pid), encoding='utf-8')\n"
            "return {'ok': True, 'child_pid': child.pid}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "workspace"}, '
            '"process_spawn": "unrestricted"},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    file_pid = int(pid_file.read_text(encoding="utf-8"))
    assert result["result"]["child_pid"] == file_pid
    assert _wait_for_pid_exit(file_pid)


def test_runtime_process_tree_guard_failure_fails_before_user_code_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    side_effect = workspace / "side-effect.txt"
    _write_tool(
        workspace,
        "guard_failure.py",
        name="guard_failure_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], 'side-effect.txt').write_text('ran')\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    def fail_guard(proc: subprocess.Popen[bytes]):
        raise runtime_module.WorkerProcessTreeGuardError("simulated guard failure")

    monkeypatch.setattr(runtime_module, "_create_worker_process_tree_guard", fail_guard)

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["timeout"] is False
    assert result["error_info"]["type"] == "WorkerProcessTreeGuardError"
    assert not side_effect.exists()


def test_runtime_parent_interruption_during_guard_setup_terminates_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    worker_pid_file = workspace / "guard-setup-worker.pid"
    _write_tool(
        workspace,
        "guard_interruption.py",
        name="guard_interruption_tool",
        body="return {'ok': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))
    original_popen = runtime_module._popen_worker_process

    def record_worker_process(
        *,
        stdout_handle: object,
        stderr_handle: object,
        env: dict[str, str],
        cwd: Path,
    ) -> subprocess.Popen[bytes]:
        proc = original_popen(
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
            env=env,
            cwd=cwd,
        )
        worker_pid_file.write_text(str(proc.pid), encoding="utf-8")
        return proc

    def interrupt_guard_setup(proc: subprocess.Popen[bytes]):
        raise KeyboardInterrupt("simulated guard setup interruption")

    monkeypatch.setattr(runtime_module, "_popen_worker_process", record_worker_process)
    monkeypatch.setattr(runtime_module, "_create_worker_process_tree_guard", interrupt_guard_setup)

    with pytest.raises(KeyboardInterrupt):
        run_custom_tool(
            spec=spec,
            args={},
            workspace_root=workspace,
            session_id="session-1",
        )

    assert worker_pid_file.exists()
    worker_pid = int(worker_pid_file.read_text(encoding="utf-8"))
    assert _wait_for_pid_exit(worker_pid)


def test_runtime_parent_interruption_cleans_up_process_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    pid_file = workspace / "interrupted-child.pid"
    _write_tool(
        workspace,
        "interrupted.py",
        name="interrupted_tool",
        body=(
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "workspace = Path(os.environ['ALYSIS_WORKSPACE_ROOT'])\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            '"import time; time.sleep(3600)"])\n'
            "(workspace / 'interrupted-child.pid').write_text(str(child.pid), encoding='utf-8')\n"
            "time.sleep(3600)\n"
            "return {'ok': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "workspace"}, '
            '"process_spawn": "unrestricted"},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    def interrupt_after_child_starts(
        proc: subprocess.Popen[bytes],
        payload: bytes,
        *,
        timeout_s: float,
    ) -> None:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.flush()
        proc.stdin.close()
        assert _wait_for_file(pid_file)
        raise KeyboardInterrupt("simulated parent interruption")

    monkeypatch.setattr(runtime_module, "_communicate_worker_process", interrupt_after_child_starts)

    with pytest.raises(KeyboardInterrupt):
        run_custom_tool(
            spec=spec,
            args={},
            workspace_root=workspace,
            session_id="session-1",
        )

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    assert _wait_for_pid_exit(child_pid)


def test_runtime_blocks_host_package_import(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    side_effect = workspace / "host-import-ran.txt"
    _write_tool(
        workspace,
        "host_import.py",
        name="host_import_tool",
        body=(
            "import alysis_code.custom_tools.trust\n"
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], "
            "'host-import-ran.txt').write_text('ran', encoding='utf-8')\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["timeout"] is False
    assert result["error_info"]["type"] == "HostImportBlocked"
    assert "alysis_code" in result["error_info"]["message"]
    assert not side_effect.exists()


def test_runtime_blocks_host_package_import_via_importlib(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    side_effect = workspace / "host-importlib-ran.txt"
    _write_tool(
        workspace,
        "host_importlib.py",
        name="host_importlib_tool",
        body=(
            "import importlib\n"
            "importlib.import_module('alysis_code.custom_tools.trust')\n"
            "from pathlib import Path\n"
            "import os\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], "
            "'host-importlib-ran.txt').write_text('ran', encoding='utf-8')\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["timeout"] is False
    assert result["error_info"]["type"] == "HostImportBlocked"
    assert "alysis_code" in result["error_info"]["message"]
    assert not side_effect.exists()


def test_runtime_blocks_ctypes_import_as_policy_bypass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "ctypes_import.py",
        name="ctypes_import_tool",
        body="import ctypes\nreturn {'ran': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] in {"HostImportBlocked", "CapabilityViolation"}
    assert "ctypes" in result["error_info"]["message"]


def test_runtime_filesystem_write_none_blocks_workspace_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    blocked = workspace / "blocked.txt"
    _write_tool(
        workspace,
        "write_none.py",
        name="write_none_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], "
            "'blocked.txt').write_text('blocked', encoding='utf-8')\n"
            "return {'ran': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"read_only": True, "filesystem": {"write": "none"}},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "filesystem" in result["error_info"]["message"]
    assert "write" in result["error_info"]["message"]
    assert not blocked.exists()


def test_runtime_filesystem_write_none_blocks_truncate_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    victim = workspace / "victim.txt"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("do-not-truncate", encoding="utf-8")
    _write_tool(
        workspace,
        "truncate_none.py",
        name="truncate_none_tool",
        body=("import os\nos.truncate('victim.txt', 0)\nreturn {'ran': True}"),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "none"}},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert victim.read_text(encoding="utf-8") == "do-not-truncate"


def test_runtime_filesystem_write_unspecified_blocks_workspace_write_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    blocked = workspace / "blocked-default.txt"
    _write_tool(
        workspace,
        "write_unspecified.py",
        name="write_unspecified_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], "
            "'blocked-default.txt').write_text('blocked', encoding='utf-8')\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert not blocked.exists()


def test_runtime_filesystem_write_workspace_allows_workspace_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    allowed = workspace / "allowed.txt"
    _write_tool(
        workspace,
        "write_workspace.py",
        name="write_workspace_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "target = Path(os.environ['ALYSIS_WORKSPACE_ROOT'], 'allowed.txt')\n"
            "target.write_text('allowed', encoding='utf-8')\n"
            "return {'wrote': target.exists()}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "workspace"}},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert result["result"]["wrote"] is True
    assert allowed.read_text(encoding="utf-8") == "allowed"
    assert result["side_effects"] == {
        "workspace_writes": [{"path": "allowed.txt", "scope": "workspace"}]
    }


def test_runtime_filesystem_write_workspace_blocks_symlink_escape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    external.mkdir()
    workspace.mkdir()
    _configure_trust_dir(workspace, monkeypatch)
    _symlink_to_or_skip(workspace / "escape", external, target_is_directory=True)
    outside = external / "outside.txt"
    _write_tool(
        workspace,
        "write_escape.py",
        name="write_escape_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], 'escape', "
            "'outside.txt').write_text('escaped', encoding='utf-8')\n"
            "return {'ran': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "workspace"}},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert not outside.exists()


def test_runtime_filesystem_write_tool_dir_allows_only_original_tool_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    tool_dir_file = workspace / ".alysis" / "tools" / "tool-dir-allowed.txt"
    root_file = workspace / "tool-dir-blocked.txt"
    _write_tool(
        workspace,
        "write_tool_dir.py",
        name="write_tool_dir_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "tool_dir = Path(os.environ['ALYSIS_TOOL_PATH']).parent\n"
            "(tool_dir / 'tool-dir-allowed.txt').write_text('allowed', encoding='utf-8')\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], "
            "'tool-dir-blocked.txt').write_text('blocked', encoding='utf-8')\n"
            "return {'ran': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "tool_dir"}},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert tool_dir_file.exists()
    assert not root_file.exists()


def test_runtime_success_payload_reports_tool_dir_workspace_write_side_effect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    tool_dir_file = workspace / ".alysis" / "tools" / "tool-state.txt"
    _write_tool(
        workspace,
        "write_tool_dir.py",
        name="write_tool_dir_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "tool_dir = Path(os.environ['ALYSIS_TOOL_PATH']).parent\n"
            "(tool_dir / 'tool-state.txt').write_text('allowed', encoding='utf-8')\n"
            "return {'ran': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "tool_dir"}},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert tool_dir_file.read_text(encoding="utf-8") == "allowed"
    assert result["side_effects"] == {
        "workspace_writes": [{"path": ".alysis/tools/tool-state.txt", "scope": "tool_dir"}]
    }


def test_runtime_network_none_blocks_socket_connect(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "network_none.py",
        name="network_none_tool",
        body=(
            "import socket\n"
            "socket.create_connection(('127.0.0.1', 9), timeout=0.1)\n"
            "return {'ran': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"network_access": "none"},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "network" in result["error_info"]["message"]


def test_runtime_network_none_blocks_dns_lookup(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "network_dns_none.py",
        name="network_dns_none_tool",
        body=("import socket\nsocket.getaddrinfo('localhost', 9)\nreturn {'ran': True}"),
        extra_manifest_lines=[
            '"capabilities": {"network_access": "none"},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "network" in result["error_info"]["message"]


def test_runtime_network_unspecified_blocks_socket_connect_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "network_unspecified.py",
        name="network_unspecified_tool",
        body=(
            "import socket\n"
            "socket.create_connection(('127.0.0.1', 9), timeout=0.1)\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"


def test_runtime_network_local_allows_loopback_but_blocks_remote(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "network_local.py",
        name="network_local_tool",
        body=(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=0.05)\n"
            "except OSError:\n"
            "    pass\n"
            "socket.create_connection(('8.8.8.8', 53), timeout=0.05)\n"
            "return {'ran': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"network_access": "local"},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "8.8.8.8" in result["error_info"]["message"]


def test_runtime_network_restricted_allows_declared_host_and_blocks_other_host(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "network_restricted.py",
        name="network_restricted_tool",
        body=(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=0.05)\n"
            "except OSError:\n"
            "    pass\n"
            "socket.create_connection(('8.8.8.8', 53), timeout=0.05)\n"
            "return {'ran': True}"
        ),
        extra_manifest_lines=[
            '"capabilities": {',
            '    "network_access": "restricted",',
            '    "network_hosts": ["127.0.0.1"],',
            "},",
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "8.8.8.8" in result["error_info"]["message"]


def test_runtime_network_restricted_allows_declared_hostname_connect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        _write_tool(
            workspace,
            "network_restricted_hostname.py",
            name="network_restricted_hostname_tool",
            body=(
                "import socket\n"
                "with socket.create_connection(('localhost', args['port']), timeout=2):\n"
                "    pass\n"
                "return {'connected': True}"
            ),
            extra_manifest_lines=[
                '"capabilities": {',
                '    "network_access": "restricted",',
                '    "network_hosts": ["localhost"],',
                "},",
            ],
        )
        spec = _trust_project_spec(_discover_project_spec(workspace))

        result = run_custom_tool(
            spec=spec,
            args={"port": port},
            workspace_root=workspace,
            session_id="session-1",
        )

    assert result["success"] is True
    assert result["result"]["connected"] is True


def test_runtime_process_spawn_denied_by_default(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "spawn_denied.py",
        name="spawn_denied_tool",
        body=(
            "import subprocess\n"
            "import sys\n"
            "subprocess.Popen([sys.executable, '-c', \"print('child')\"])\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "process" in result["error_info"]["message"]


def test_runtime_process_spawn_denied_by_default_for_os_fork(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("os.fork is not available on this platform")
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "fork_denied.py",
        name="fork_denied_tool",
        body=(
            "import os\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os._exit(0)\n"
            "os.waitpid(pid, 0)\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "process" in result["error_info"]["message"]


def test_runtime_process_spawn_denied_by_default_for_os_spawnlp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not hasattr(os, "spawnlp"):
        pytest.skip("os.spawnlp is not available on this platform")
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "spawnlp_denied.py",
        name="spawnlp_denied_tool",
        body=(
            "import os\n"
            "import sys\n"
            "os.spawnlp(os.P_WAIT, sys.executable, sys.executable, '-c', 'pass')\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert "process" in result["error_info"]["message"]


def test_runtime_process_spawn_unrestricted_allows_spawn_under_existing_tree_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "spawn_allowed.py",
        name="spawn_allowed_tool",
        body=(
            "import subprocess\n"
            "import sys\n"
            "child = subprocess.Popen([sys.executable, '-c', \"print('child')\"], "
            "stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)\n"
            "stdout, stderr = child.communicate(timeout=5)\n"
            "return {'returncode': child.returncode, 'stdout': stdout.strip(), 'stderr': stderr}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"process_spawn": "unrestricted"},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert result["result"]["returncode"] == 0
    assert result["result"]["stdout"] == "child"


def test_runtime_caught_policy_violation_still_fails_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    blocked = workspace / "caught-policy.txt"
    _write_tool(
        workspace,
        "caught_policy.py",
        name="caught_policy_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "try:\n"
            "    Path(os.environ['ALYSIS_WORKSPACE_ROOT'], "
            "'caught-policy.txt').write_text('blocked', encoding='utf-8')\n"
            "except Exception:\n"
            "    pass\n"
            "return {'claimed': 'success'}"
        ),
        extra_manifest_lines=[
            '"capabilities": {"filesystem": {"write": "none"}},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["error_info"]["type"] == "CapabilityViolation"
    assert not blocked.exists()


def test_runtime_captures_stdout_and_stderr(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "streams.py",
        name="stream_tool",
        body="import sys\nprint('hello')\nprint('oops', file=sys.stderr)\nreturn {'ok': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert "hello" in result["stdout_preview"]
    assert "oops" in result["stderr_preview"]
    assert (workspace / result["stdout_artifact_path"]).exists()
    stderr_artifact = workspace / result["stderr_artifact_path"]
    assert stderr_artifact.exists()
    stderr_text = stderr_artifact.read_text(encoding="utf-8")
    assert "oops" in stderr_text
    assert "RuntimeWarning" not in result["stderr_preview"]
    assert "runpy" not in result["stderr_preview"]
    assert "found in sys.modules after import of package" not in result["stderr_preview"]
    assert "RuntimeWarning" not in stderr_text
    assert "runpy" not in stderr_text
    assert "found in sys.modules after import of package" not in stderr_text


def test_runtime_can_spool_streams_to_external_artifact_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "streams.py",
        name="stream_tool",
        body="import sys\nprint('hello')\nprint('oops', file=sys.stderr)\nreturn {'ok': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))
    artifact_dir = tmp_path / "external-session" / "tool_logs"

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
        artifact_dir=artifact_dir,
        artifact_reference_prefix="session_artifacts/tool_logs",
    )

    assert result["success"] is True
    assert result["stdout_artifact_path"].startswith("session_artifacts/tool_logs/")
    assert result["stderr_artifact_path"].startswith("session_artifacts/tool_logs/")
    assert not (workspace / ".alysis" / "runs" / "session-1" / "tool_logs").exists()
    stdout_artifacts = sorted(artifact_dir.glob("*.stdout.log"))
    stderr_artifacts = sorted(artifact_dir.glob("*.stderr.log"))
    assert len(stdout_artifacts) == 1
    assert len(stderr_artifacts) == 1
    assert stdout_artifacts[0].read_text(encoding="utf-8") == "hello\n"
    assert stderr_artifacts[0].read_text(encoding="utf-8") == "oops\n"


def test_runtime_tool_authored_stderr_is_still_spooled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "stderr.py",
        name="stderr_tool",
        body=("import sys\nprint('TOOL_STDERR_SENTINEL', file=sys.stderr)\nreturn {'ok': True}"),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert "TOOL_STDERR_SENTINEL" in result["stderr_preview"]
    assert "stderr_artifact_path" in result
    stderr_text = (workspace / result["stderr_artifact_path"]).read_text(encoding="utf-8")
    assert "TOOL_STDERR_SENTINEL" in stderr_text
    assert "RuntimeWarning" not in stderr_text
    assert "runpy" not in stderr_text


def test_docs_workspace_manifest_example_runs_under_subprocess_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "docs" / "examples" / "custom_tools" / "workspace_manifest.py"
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    tool_path = workspace / ".alysis" / "tools" / "workspace_manifest.py"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, tool_path)
    (workspace / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    spec = _trust_project_spec(_discover_project_spec(workspace, name="workspace_manifest"))

    result = run_custom_tool(
        spec=spec,
        args={"names": ["pyproject.toml", "missing-file.json"]},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert result["result"]["workspace_root"] == os.fspath(workspace.resolve())
    found_names = {item["name"] for item in result["result"]["found"]}
    assert "pyproject.toml" in found_names
    assert "missing-file.json" not in found_names
    assert result["stderr_preview"] == ""
    assert "stderr_artifact_path" not in result


def test_runtime_treats_system_exit_as_failure(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "exit.py",
        name="exit_tool",
        body="raise SystemExit(2)",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert "SystemExit" in result["error"]


def test_runtime_sanitizes_non_serializable_results(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "sanitize.py",
        name="sanitize_tool",
        body="return {'values': {1, 2, 3}}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert result["result"]["values"]["omitted"] is True


def test_runtime_fails_closed_on_stale_project_tool_hash_before_user_code_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    side_effect = workspace / "side-effect.txt"
    tool_path = _write_tool(
        workspace,
        "stale.py",
        name="stale_project_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], 'side-effect.txt').write_text('ran')\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))
    tool_path.write_text(tool_path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["timeout"] is False
    assert result["error_info"]["type"] == "StaleToolHash"
    assert "changed since discovery" in result["error"]
    assert spec.file_hash in result["error"]
    assert not side_effect.exists()


def test_runtime_fails_closed_on_stale_global_tool_hash_before_user_code_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    config_dir = _configure_trust_dir(workspace, monkeypatch)
    side_effect = workspace / "side-effect.txt"
    tool_path = _write_global_tool(
        config_dir,
        "stale_global.py",
        name="stale_global_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], 'side-effect.txt').write_text('ran')\n"
            "return {'ran': True}"
        ),
    )
    spec = _discover_global_spec(workspace=workspace, config_dir=config_dir)
    tool_path.write_text(tool_path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["timeout"] is False
    assert result["error_info"]["type"] == "StaleToolHash"
    assert "changed since discovery" in result["error"]
    assert spec.file_hash in result["error"]
    assert not side_effect.exists()


def test_runtime_fails_closed_when_project_tool_trust_removed_after_discovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    side_effect = workspace / "side-effect.txt"
    _write_tool(
        workspace,
        "revoked.py",
        name="revoked_tool",
        body=(
            "import os\n"
            "from pathlib import Path\n"
            "Path(os.environ['ALYSIS_WORKSPACE_ROOT'], 'side-effect.txt').write_text('ran')\n"
            "return {'ran': True}"
        ),
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))
    save_trust_state(ProjectToolTrustState())

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["timeout"] is False
    assert result["error_info"]["type"] == "TrustRevoked"
    assert "no longer trusted" in result["error"]
    assert not side_effect.exists()


def test_runtime_executes_from_sealed_copy_and_preserves_original_tool_path_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    tool_path = _write_tool(
        workspace,
        "sealed.py",
        name="sealed_tool",
        body="import os\nreturn {'file': __file__, 'env_tool_path': os.environ['ALYSIS_TOOL_PATH']}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    assert result["result"]["file"] != result["result"]["env_tool_path"]
    assert result["result"]["env_tool_path"] == os.fspath(tool_path.resolve())


def test_runtime_scrubs_worker_env_to_declared_allowlist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    monkeypatch.setenv("JIRA_TOKEN", "jira-token")
    monkeypatch.setenv("EXTRA_SECRET", "extra-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "should_not_leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should_not_leak")
    interesting_keys = [
        "JIRA_TOKEN",
        "EXTRA_SECRET",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "ALYSIS_WORKSPACE_ROOT",
        "ALYSIS_SESSION_ID",
        "ALYSIS_TOOL_PATH",
        "ALYSIS_TOOL_SCOPE",
        "ALYSIS_TOOL_NAME",
    ]
    _write_tool(
        workspace,
        "env.py",
        name="env_tool",
        body=(
            "import os\n"
            f"interesting = {interesting_keys!r}\n"
            "return {"
            "'keys': sorted(key for key in os.environ if key in interesting), "
            "'values': {key: os.environ[key] for key in interesting if key in os.environ}"
            "}"
        ),
        extra_manifest_lines=[
            '"required_env": ["JIRA_TOKEN"],',
            '"capabilities": {"secret_refs": ["EXTRA_SECRET"]},',
        ],
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert result["success"] is True
    values = result["result"]["values"]
    assert values["JIRA_TOKEN"] == "jira-token"
    assert values["EXTRA_SECRET"] == "extra-secret"
    for key in [
        "ALYSIS_WORKSPACE_ROOT",
        "ALYSIS_SESSION_ID",
        "ALYSIS_TOOL_PATH",
        "ALYSIS_TOOL_SCOPE",
        "ALYSIS_TOOL_NAME",
    ]:
        assert key in values
    assert "OPENAI_API_KEY" not in result["result"]["keys"]
    assert "AWS_SECRET_ACCESS_KEY" not in result["result"]["keys"]


def test_runtime_spools_large_stdout_to_artifact_and_returns_bounded_tail_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "big_stdout.py",
        name="big_stdout_tool",
        body="print('A' * 2_000_000 + 'THE_END')\nreturn {'ok': True}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    result = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session big output",
    )

    assert result["success"] is True
    assert result["stdout_truncated"] is True
    assert len(result["stdout_preview"]) <= 1_200
    assert "THE_END" in result["stdout_preview"]
    assert "stdout_artifact_path" in result
    artifact = workspace / result["stdout_artifact_path"]
    assert artifact.exists()
    content = artifact.read_text(encoding="utf-8")
    assert len(content) > len(result["stdout_preview"])
    assert content.endswith("THE_END\n")


def test_runtime_uses_fresh_module_load_per_call(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    _configure_trust_dir(workspace, monkeypatch)
    _write_tool(
        workspace,
        "fresh.py",
        name="fresh_tool",
        body="counter = globals().get('counter', 0) + 1\nglobals()['counter'] = counter\nreturn {'counter': counter}",
    )
    spec = _trust_project_spec(_discover_project_spec(workspace))

    first = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )
    second = run_custom_tool(
        spec=spec,
        args={},
        workspace_root=workspace,
        session_id="session-1",
    )

    assert first["result"]["counter"] == 1
    assert second["result"]["counter"] == 1
