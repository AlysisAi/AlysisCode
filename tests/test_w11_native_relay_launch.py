"""W11U1E-1 regression sources. Execution is reserved for authorized U3."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.mcp import native_relay_guard as guard
from alysis_code.mcp import transport_stdio as transport
from alysis_code.mcp.manager import McpManager
from alysis_code.mcp.models import ResolvedMcpConfig, ResolvedMcpServer
from alysis_code.mcp.person_approval import TOOL_SERVER_APPROVAL_ENV, server_identity


def _server(command: str, args=(), env=None) -> ResolvedMcpServer:
    return ResolvedMcpServer(
        id="native-relay",
        transport="stdio",
        enabled=True,
        enabled_in=None,
        trust="explicit",
        allowed_tools=(),
        denied_tools=(),
        startup_timeout_s=1,
        call_timeout_s=1,
        tool_prefix=None,
        prompts_mode="listed_get_only",
        command=command,
        args=tuple(args),
        env=env or {},
    )


def _file(path: Path, content: str = "fixture, never executed") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)
    return path


def _zero_launch(monkeypatch):
    launches = []

    def forbidden(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("Native relay reached Popen")

    monkeypatch.setattr(transport.subprocess, "Popen", forbidden)
    return launches


@pytest.mark.parametrize("fallback", [False, True])
def test_extensionless_windows_relay_is_denied_on_parent_and_child_path(
    tmp_path, monkeypatch, fallback
):
    _file(tmp_path / "bin" / "alysis-computer-use.exe")
    monkeypatch.setattr(transport, "_IS_WINDOWS", True)
    launches = _zero_launch(monkeypatch)
    child_env = {"PATH": str(tmp_path / "bin"), "PATHEXT": ".exe;.cmd"}
    host_env = {"PATH": str(tmp_path / "empty"), "PATHEXT": ".exe;.cmd"} if fallback else child_env
    channel = transport.McpStdioTransport(
        server=_server("alysis-computer-use", ["--allow-all"], child_env),
        workspace_root=tmp_path,
        host_env=host_env,
    )
    with pytest.raises(transport.McpStdioTransportError, match="native Computer Use MCP relay"):
        channel.start()
    assert launches == []
    assert channel._process is None


@pytest.mark.parametrize(
    "kind",
    ["absolute-mac", "absolute-windows", "symlink", "hardlink", "sh", "cmd", "batch", "cmd-batch"],
)
def test_resolved_relay_identity_and_shell_batch_forms_never_launch(tmp_path, monkeypatch, kind):
    windows = kind in {"absolute-windows", "cmd", "batch", "cmd-batch"}
    monkeypatch.setattr(transport, "_IS_WINDOWS", windows)
    helper = _file(tmp_path / ("alysis-computer-use.exe" if windows else "alysis-computer-mcp"))
    command, args = str(helper), ["--allow-all"]
    host_env = {"PATH": str(tmp_path), "PATHEXT": ".exe;.cmd"}
    if kind in {"symlink", "hardlink"}:
        alias = tmp_path / "ordinary-server"
        alias.symlink_to(helper) if kind == "symlink" else os.link(helper, alias)
        command = str(alias)
        host_env[TOOL_SERVER_APPROVAL_ENV] = json.dumps(
            {
                "version": 1,
                "exempt": [{"server_id": "alysis-computer", "command": str(helper)}],
            }
        )
    elif kind in {"sh", "cmd", "cmd-batch"}:
        command = str(_file(tmp_path / ("cmd.exe" if windows else "sh")))
        target = helper
        if kind == "cmd-batch":
            target = _file(tmp_path / "relay-wrapper.cmd", f'@echo off\n"{helper}" --allow-all\n')
        args = (
            ["/c", f'"{target}" --allow-all'] if windows else ["-c", f'exec "{helper}" --allow-all']
        )
    elif kind == "batch":
        command = str(_file(tmp_path / "relay-wrapper.cmd", f'@echo off\n"{helper}" --allow-all\n'))
    launches = _zero_launch(monkeypatch)
    channel = transport.McpStdioTransport(
        server=_server(command, args), workspace_root=tmp_path, host_env=host_env
    )
    with pytest.raises(transport.McpStdioTransportError, match="native Computer Use MCP relay"):
        channel.start()
    assert launches == []


@pytest.mark.parametrize("kind", ["direct", "sh-npx", "cmd-npx", "uvx", "npx-batch"])
def test_unrelated_ordinary_launchers_still_reach_popen(tmp_path, monkeypatch, kind):
    windows = kind in {"cmd-npx", "npx-batch"}
    monkeypatch.setattr(transport, "_IS_WINDOWS", windows)
    filename = {
        "direct": "server",
        "sh-npx": "sh",
        "cmd-npx": "cmd.exe",
        "uvx": "uvx",
        "npx-batch": "npx.cmd",
    }[kind]
    program = _file(tmp_path / filename, '@echo off\nnode "%~dp0server.js" %*\n')
    args = {
        "direct": [],
        "sh-npx": ["-c", "exec npx --yes ordinary-mcp"],
        "cmd-npx": ["/c", "npx --yes ordinary-mcp"],
        "uvx": ["ordinary-mcp"],
        "npx-batch": ["--yes", "ordinary-mcp"],
    }[kind]
    launches = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda *a, **k: launches.append((a, k)) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server(str(program), args),
        workspace_root=tmp_path,
        host_env={"PATH": str(tmp_path), "PATHEXT": ".exe;.cmd"},
    )
    assert channel._spawn([str(program), *args]) is sentinel
    assert len(launches) == 1
    assert str(program.resolve()) in str(launches[0][0][0])


@pytest.mark.parametrize("entry", ["catalog", "prompt", "enable", "restart"])
@pytest.mark.parametrize("approval", ["exempt", "marked-read"])
def test_manager_entry_paths_and_explicit_read_policy_cannot_bypass_launch_gate(
    tmp_path, monkeypatch, entry, approval
):
    monkeypatch.setattr(transport, "_IS_WINDOWS", False)
    helper = _file(tmp_path / "alysis-computer-mcp")
    shell = _file(tmp_path / "sh")
    server = _server(str(shell), ["-c", f'exec "{helper}"'])
    marks = tmp_path / "marks.json"
    marks.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {
                    server.id: {
                        "identity": server_identity(server),
                        "read_tools": ["computer_state"],
                    },
                },
            }
        )
    )
    monkeypatch.setenv(
        TOOL_SERVER_APPROVAL_ENV,
        json.dumps(
            {
                "version": 1,
                "read_marks": str(marks),
                "exempt": [{"server_id": server.id, "command": server.command}]
                if approval == "exempt"
                else [],
            }
        ),
    )
    config = ResolvedMcpConfig(
        tmp_path, tmp_path / "user.json", tmp_path / "project.json", True, False, (server,)
    )
    manager = McpManager(
        resolved_config=config, workspace_root=tmp_path, runtime_kind="interactive_chat"
    )
    launches = _zero_launch(monkeypatch)
    operations = {
        "catalog": lambda: manager._collect_server_catalog(server),
        "prompt": lambda: manager._ensure_prompt_server_snapshot(server),
        "enable": lambda: manager.enable_server(server_id=server.id),
        "restart": lambda: manager.restart_server(server_id=server.id),
    }
    try:
        with pytest.raises(Exception, match="native Computer Use MCP relay"):
            operations[entry]()
        assert launches == []
    finally:
        manager.close()


@pytest.mark.parametrize("bare", [False, True])
def test_posix_ordinary_symlink_keeps_selected_invocation_path(tmp_path, monkeypatch, bare):
    """Venv/argv[0] bootstrap control; no interpreter is executed here."""
    monkeypatch.setattr(transport, "_IS_WINDOWS", False)
    interpreter = _file(tmp_path / "base" / "python")
    alias = tmp_path / "venv" / "bin" / "python"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(interpreter)
    command = "python" if bare else str(alias)
    seen = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda *a, **k: seen.append((a, k)) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server(command, ["-m", "ordinary_mcp"]),
        workspace_root=tmp_path,
        host_env={"PATH": str(alias.parent)},
    )
    assert channel._spawn([command, "-m", "ordinary_mcp"]) is sentinel
    assert seen[0][0][0] == [str(alias), "-m", "ordinary_mcp"]
    assert str(interpreter) not in seen[0][0][0]


@pytest.mark.parametrize("other_extension", [".com", ".cmd"])
def test_windows_primary_exe_precedes_pathext_collision(tmp_path, monkeypatch, other_extension):
    monkeypatch.setattr(transport, "_IS_WINDOWS", True)
    exe = _file(tmp_path / "bin" / "ordinary-server.exe")
    _file(tmp_path / "bin" / f"ordinary-server{other_extension}")
    seen = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda *a, **k: seen.append(a[0]) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server("ordinary-server"),
        workspace_root=tmp_path,
        host_env={"PATH": str(exe.parent), "PATHEXT": f"{other_extension};.exe"},
    )
    assert channel._spawn(["ordinary-server"]) is sentinel
    assert seen == [[str(exe)]]


@pytest.mark.parametrize("first_extension", [".com", ".cmd"])
def test_windows_fallback_retains_child_pathext_order(tmp_path, monkeypatch, first_extension):
    monkeypatch.setattr(transport, "_IS_WINDOWS", True)
    first = _file(tmp_path / "child-bin" / f"ordinary-server{first_extension}", "@echo off\n")
    _file(tmp_path / "child-bin" / "ordinary-server.exe")
    child = {"PATH": str(first.parent), "PATHEXT": f"{first_extension};.exe"}
    seen = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda *a, **k: seen.append(a[0]) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server("ordinary-server", env=child),
        workspace_root=tmp_path,
        host_env={"PATH": str(tmp_path / "empty"), "PATHEXT": ".exe"},
    )
    with pytest.raises(FileNotFoundError):
        channel._spawn(["ordinary-server"])
    assert seen == []
    assert channel._spawn_resolved_windows_command(FileNotFoundError()) is sentinel
    expected = (
        transport.windows_batch_command_line(str(first), [])
        if first_extension == ".cmd"
        else [str(first)]
    )
    assert seen == [expected]


def _inspect(script, tmp_path, *, args=(), env=None):
    return guard.check_native_relay_launch(
        str(script),
        args,
        server_id="ordinary-server",
        cwd=tmp_path,
        env=env or {"PATH": str(tmp_path), "PATHEXT": ".cmd;.exe"},
        windows=True,
        host_env={},
    )


@pytest.mark.parametrize("limit", ["byte_budget", "token_budget", "script_budget", "depth_budget"])
def test_inspection_limits_remain_unproven_and_do_not_block_ordinary_servers(
    tmp_path, monkeypatch, limit
):
    monkeypatch.setattr(transport, "_IS_WINDOWS", True)
    root = tmp_path / "ordinary.cmd"
    if limit == "byte_budget":
        _file(root, " " * (guard.MAX_INSPECTION_BYTES + 1))
    elif limit == "token_budget":
        _file(root, "node " * (guard.MAX_INSPECTION_TOKENS + 1))
    elif limit == "script_budget":
        children = [
            _file(tmp_path / f"child-{i}.cmd", "") for i in range(guard.MAX_INSPECTION_SCRIPTS + 1)
        ]
        _file(root, "\n".join(f'call "{child}"' for child in children))
    else:
        chain = [
            root,
            *(tmp_path / f"child-{i}.cmd" for i in range(guard.MAX_INSPECTION_DEPTH + 1)),
        ]
        for index, script in enumerate(chain):
            _file(script, f'call "{chain[index + 1]}"\n' if index + 1 < len(chain) else "")
    result = _inspect(root, tmp_path)
    assert result.state == "unproven"
    assert result.obligation == "OBLIGATION-HELPER-ADMISSION"
    assert limit in result.limitations
    assert result.scripts <= guard.MAX_INSPECTION_SCRIPTS
    assert result.bytes_read <= guard.MAX_INSPECTION_BYTES
    assert result.tokens <= guard.MAX_INSPECTION_TOKENS
    assert result.depth <= guard.MAX_INSPECTION_DEPTH
    seen = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda *a, **k: seen.append(a[0]) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server(str(root)), workspace_root=tmp_path, host_env={}
    )
    assert channel._spawn([str(root)]) is sentinel
    assert len(seen) == 1
    assert channel._native_relay_inspection.state == "unproven"


def test_known_relay_in_inspected_prefix_still_denied_when_byte_budget_exhausted(tmp_path):
    root = _file(
        tmp_path / "wrapper.cmd", "alysis-computer-use.exe\n" + " " * guard.MAX_INSPECTION_BYTES
    )
    with pytest.raises(ValueError, match="native Computer Use MCP relay"):
        _inspect(root, tmp_path)


def test_known_executable_identity_is_denied_before_oversized_arguments(tmp_path):
    helper = _file(tmp_path / "alysis-computer-use.exe")
    with pytest.raises(ValueError, match="native Computer Use MCP relay"):
        _inspect(helper, tmp_path, args=[" " * (guard.MAX_INSPECTION_BYTES + 1)])


def test_cycle_is_finite_and_unreadable_script_is_unproven(tmp_path, monkeypatch):
    import builtins

    root = _file(tmp_path / "cycle.cmd", 'call "cycle.cmd"\n')
    result = _inspect(root, tmp_path)
    assert result.scripts == 1
    real_open = builtins.open

    def unreadable(path, *args, **kwargs):
        if str(path) == str(root):
            raise PermissionError("inert unreadable fixture")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", unreadable)
    result = _inspect(root, tmp_path)
    assert result.state == "unproven"
    assert "unreadable_script" in result.limitations


def test_environment_expansion_consumes_the_same_aggregate_byte_budget(tmp_path):
    root = _file(tmp_path / "expansion.cmd", "echo %BIG% %BIG%\n")
    result = _inspect(root, tmp_path, env={"BIG": "x" * (guard.MAX_INSPECTION_BYTES + 1)})
    assert "byte_budget" in result.limitations
    assert result.bytes_read == guard.MAX_INSPECTION_BYTES
    assert result.state == "unproven"


@pytest.mark.parametrize("data_kind", ["missing", "file", "directory", "alias"])
def test_ord08_manager_and_transport_preserve_data_arguments(tmp_path, monkeypatch, data_kind):
    from alysis_code.mcp import manager as manager_module

    monkeypatch.setattr(transport, "_IS_WINDOWS", False)
    command = _file(tmp_path / "ordinary-server")
    data = tmp_path / "data" / "alysis-computer-mcp"
    if data_kind == "file":
        _file(data)
    elif data_kind == "directory":
        data.mkdir(parents=True)
    elif data_kind == "alias":
        helper = _file(tmp_path / "alysis-computer-mcp")
        data.parent.mkdir()
        data.symlink_to(helper)
    server = _server(str(command), ["--label", str(data)])
    config = ResolvedMcpConfig(
        tmp_path, tmp_path / "user.json", tmp_path / "project.json", True, False, (server,)
    )
    manager = McpManager(
        resolved_config=config, workspace_root=tmp_path, runtime_kind="interactive_chat"
    )
    constructed = []
    sentinel = object()
    monkeypatch.setattr(
        manager_module,
        "_client_factory",
        lambda name: lambda **kw: constructed.append((name, kw)) or sentinel,
    )
    assert manager._build_live_client(server) is sentinel
    assert constructed[0][1]["server"].args == ("--label", str(data))
    launched = []
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda argv, **kw: launched.append(argv) or sentinel
    )
    channel = transport.McpStdioTransport(server=server, workspace_root=tmp_path, host_env={})
    assert channel._spawn([str(command), *server.args]) is sentinel
    assert launched == [[str(command), "--label", str(data)]]


@pytest.mark.parametrize(
    "program,args",
    [
        ("sh", ["-c", 'echo "alysis-computer-mcp"']),
        ("sh", ["-c", 'echo ";" alysis-computer-mcp']),
        ("sh", ["-c", "echo safe > alysis-computer-mcp"]),
        ("sh", ["-c", "DATA=/tmp/alysis-computer-mcp ordinary-server"]),
        ("sh", ["-c", "exec npx --label alysis-computer-mcp"]),
        ("sh", ["-s", "--", "alysis-computer-mcp"]),
        ("cmd.exe", ["/c", 'echo "alysis-computer-use.exe"']),
        ("cmd.exe", ["/c", "npx --label alysis-computer-use.exe"]),
        ("python", ["-m", "ordinary_mcp", "alysis-computer-mcp"]),
        ("python", ["server.py", "alysis-computer-mcp"]),
        ("node", ["server.js", "alysis-computer-mcp"]),
        ("uvx", ["ordinary-mcp", "--label", "alysis-computer-mcp"]),
    ],
)
def test_ord08_shell_and_interpreter_data_keep_original_argv(tmp_path, monkeypatch, program, args):
    windows = program == "cmd.exe"
    monkeypatch.setattr(transport, "_IS_WINDOWS", windows)
    command = _file(tmp_path / program)
    _file(tmp_path / "alysis-computer-mcp")
    _file(tmp_path / "alysis-computer-use.exe")
    sentinel = object()
    seen = []
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda argv, **kw: seen.append(argv) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server(str(command), args),
        workspace_root=tmp_path,
        host_env={"PATH": str(tmp_path)},
    )
    assert channel._spawn([str(command), *args]) is sentinel
    assert seen == [[str(command), *args]]
    assert channel._native_relay_inspection.state == "unproven"


@pytest.mark.parametrize(
    "program,args",
    [
        ("sh", ["-c", "echo safe; alysis-computer-mcp"]),
        ("sh", ["-c", "echo safe && alysis-computer-mcp"]),
        ("sh", ["-c", "exec alysis-computer-mcp"]),
        ("sh", ["alysis-computer-mcp"]),
        ("cmd.exe", ["/c", "echo safe & alysis-computer-use.exe"]),
        ("python", ["alysis-computer-mcp", "data"]),
        ("node", ["alysis-computer-mcp", "data"]),
        ("env", ["--", "alysis-computer-mcp"]),
    ],
)
def test_ord08_supported_execution_roles_still_deny(tmp_path, monkeypatch, program, args):
    monkeypatch.setattr(transport, "_IS_WINDOWS", program == "cmd.exe")
    command = _file(tmp_path / program)
    launches = _zero_launch(monkeypatch)
    channel = transport.McpStdioTransport(
        server=_server(str(command), args),
        workspace_root=tmp_path,
        host_env={"PATH": str(tmp_path)},
    )
    with pytest.raises(ValueError, match="native Computer Use MCP relay"):
        channel._spawn([str(command), *args])
    assert launches == []


def test_ord08_only_executed_script_is_read_and_alias_keeps_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "_IS_WINDOWS", False)
    target = _file(
        tmp_path / "scripts" / "ordinary.sh", "ordinary-server --input alysis-computer-mcp\n"
    )
    alias = tmp_path / "entry"
    alias.symlink_to(target)
    malicious_data = _file(tmp_path / "data.sh", "alysis-computer-mcp\n")
    seen = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda argv, **kw: seen.append(argv) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server(str(alias), [str(malicious_data)]),
        workspace_root=tmp_path,
        host_env={},
    )
    assert channel._spawn([str(alias), str(malicious_data)]) is sentinel
    assert seen == [[str(alias), str(malicious_data)]]
    assert channel._native_relay_inspection.scripts == 1
    # The same alias now selects an actually executing reserved command.
    target.write_text("alysis-computer-mcp\n", encoding="utf-8")
    with pytest.raises(ValueError, match="native Computer Use MCP relay"):
        channel._spawn([str(alias), str(malicious_data)])
    assert len(seen) == 1


def test_ord08_truncated_word_is_not_reinterpreted_as_reserved_command(tmp_path):
    suffix = "alysis-computer-use"
    root = _file(
        tmp_path / "long.cmd",
        " " * (guard.MAX_INSPECTION_BYTES - len(suffix)) + suffix + "-ordinary.exe",
    )
    result = _inspect(root, tmp_path)
    assert "byte_budget" in result.limitations
    assert result.state == "unproven"


@pytest.mark.parametrize(
    "program,args,denied",
    [
        ("cmd.exe", ["/c", "echo safe ; alysis-computer-use.exe"], False),
        ("cmd.exe", ["/c", "echo safe & alysis-computer-use.exe"], True),
        ("sh", ["-c", "echo safe ; alysis-computer-mcp"], True),
        ("powershell.exe", ["-Command", "'alysis-computer-use.exe'"], False),
        ("powershell.exe", ["-Command", "& 'alysis-computer-use.exe'"], True),
        ("pwsh", ["-Command", '"alysis-computer-mcp"'], False),
        ("pwsh", ["-Command", '& "alysis-computer-mcp"'], True),
        ("pwsh", ["-Command", "Write-Output 'alysis-computer-mcp'"], False),
        ("pwsh", ["-Command", "'ordinary text'; & 'alysis-computer-mcp'"], True),
        ("pwsh", ["-Command", "Write-Output x & 'alysis-computer-mcp'"], False),
    ],
)
def test_ord08_shell_dialects_distinguish_data_from_invocation(
    tmp_path, monkeypatch, program, args, denied
):
    windows = program.endswith(".exe")
    monkeypatch.setattr(transport, "_IS_WINDOWS", windows)
    executable = _file(tmp_path / program)
    seen = []
    sentinel = object()
    monkeypatch.setattr(
        transport.subprocess, "Popen", lambda argv, **kw: seen.append(argv) or sentinel
    )
    channel = transport.McpStdioTransport(
        server=_server(str(executable), args),
        workspace_root=tmp_path,
        host_env={"PATH": str(tmp_path)},
    )
    if denied:
        with pytest.raises(ValueError, match="native Computer Use MCP relay"):
            channel._spawn([str(executable), *args])
        assert seen == []
    else:
        assert channel._spawn([str(executable), *args]) is sentinel
        assert seen == [[str(executable), *args]]
        assert channel._native_relay_inspection.state == "unproven"


def test_ord08_powershell_file_keeps_its_own_literal_and_call_roles(tmp_path):
    executable = _file(tmp_path / "pwsh")
    script = _file(tmp_path / "ordinary.ps1", "'alysis-computer-mcp'\n")

    def inspect():
        return guard.check_native_relay_launch(
            str(executable),
            ["-File", str(script)],
            server_id="ordinary",
            cwd=tmp_path,
            env={"PATH": str(tmp_path)},
            windows=False,
            host_env={},
        )

    result = inspect()
    assert result.scripts == 1
    assert result.state == "unproven"
    script.write_text("& 'alysis-computer-mcp'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="native Computer Use MCP relay"):
        inspect()
