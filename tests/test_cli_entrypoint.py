from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def _entrypoint_env() -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([str(SRC_DIR), existing_pythonpath])
        if existing_pythonpath
        else str(SRC_DIR)
    )
    return env


def test_cold_one_shot_classic_workspace_picker_keeps_its_dependencies(tmp_path: Path) -> None:
    code = """
import io, os, sys
from pathlib import Path
from rich.console import Console
from alysis_code import cli, workspace_binding
from alysis_code.cli_impl.run import _resolve_startup_workspace_binding
root = Path(sys.argv[1])
os.environ['ALYSIS_CONFIG_DIR'] = str(root / 'config')
os.environ['ALYSIS_DATA_DIR'] = str(root / 'data')
os.environ['ALYSIS_TUI'] = '0'
assert 'alysis_code.cli_impl.commands.cli_surface' not in sys.modules
# Exercise the real classic chooser on a synthetic guarded workspace. Pipe
# input falls back to the ordinary text prompt rather than reading the desktop.
workspace_binding._home_directory = lambda: root
binding = _resolve_startup_workspace_binding(
    requested_path=root, interactive=True, console=Console(file=io.StringIO()))
assert binding.workspace_context.workspace_root == root
assert binding.broad_workspace_override_used is True
assert 'alysis_code.cli_impl.commands.cli_surface' in sys.modules
assert 'alysis_code.agent_loop' not in sys.modules
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=REPO_ROOT,
        input="1\n",
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_one_shot_admission_does_not_load_interactive_shell(tmp_path: Path) -> None:
    code = """
import os, sys
from pathlib import Path
from typer.testing import CliRunner
from alysis_code import cli
root = Path(sys.argv[1])
os.environ['ALYSIS_CONFIG_DIR'] = str(root / 'config')
os.environ['ALYSIS_DATA_DIR'] = str(root / 'data')
os.environ['ALYSIS_MODEL'] = 'local-qualification'
os.environ['ALYSIS_UPDATE_CHECK'] = '0'
calls = []
cli.run_agent = lambda **kwargs: (calls.append(kwargs), 0)[1]
runner = CliRunner()
result = runner.invoke(cli.app, ['run', '--path', str(root), '--deadline-seconds', '13',
                                 '--require-deadline', '--no-stream', '--model',
                                 'local-qualification', 'Inspect files'])
assert result.exit_code == 0, (result.output, result.exception)
assert len(calls) == 1
assert calls[0]['root'] == root
assert calls[0]['require_run_deadline'] is True
assert calls[0]['run_deadline_seconds'] == 13
assert calls[0]['one_shot_execution'] is True
assert calls[0]['cfg'].stream is False
for name in ('alysis_code.cli_impl.commands.cli_surface', 'alysis_code.cli_impl.commands.cli_common',
             'alysis_code.cli_impl.chat.loop', 'alysis_code.agent_loop'):
    assert name not in sys.modules, name
# Normal argument/filesystem admission still precedes execution.
result = runner.invoke(cli.app, ['run', '--session-dir', str(root / 'logs'), 'Inspect files'])
assert result.exit_code == 2, result.output
assert len(calls) == 1
# A second invocation must see its own embedding override, never the first call's.
cli.run_agent = lambda **kwargs: 17
result = runner.invoke(cli.app, ['run', '--path', str(root), '--model',
                                 'local-qualification', 'Inspect files'])
assert result.exit_code == 17, (result.output, result.exception)
assert len(calls) == 1
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_registration_defers_credential_crypto_until_store_access(tmp_path: Path) -> None:
    code = """
import base64, json, os, sys
from pathlib import Path
import alysis_code.cli
from alysis_code.mcp import token_store
from alysis_code.mcp.errors import McpTokenStoreCorruptError
assert 'cryptography.hazmat.bindings._rust' not in sys.modules
assert 'cryptography.hazmat.primitives.ciphers.aead' not in sys.modules
path = Path(sys.argv[1]) / 'tokens.json'
assert token_store.load_token_payload_result(path).payload == {}
assert 'cryptography.hazmat.primitives.ciphers.aead' not in sys.modules
os.environ[token_store.OAUTH_KEY_SOURCE_ENV] = token_store.KEY_SOURCE_FILESYSTEM
payload = {'test': {'access_token': 'disposable-secret'}}
token_store._write_encrypted_payload(path, payload)
assert 'cryptography.hazmat.primitives.ciphers.aead' in sys.modules
assert 'disposable-secret' not in path.read_text()
assert token_store.load_token_payload_result(path).payload == payload
envelope = json.loads(path.read_text())
ciphertext = bytearray(base64.b64decode(envelope['ciphertext']))
ciphertext[-1] ^= 1
envelope['ciphertext'] = base64.b64encode(ciphertext).decode()
path.write_text(json.dumps(envelope))
try:
    token_store.load_token_payload_result(path)
except McpTokenStoreCorruptError:
    pass
else:
    raise AssertionError('credential tampering must remain a hard failure')
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_registration_defers_diagnostic_backends() -> None:
    code = """
import sys
from alysis_code import cli
from alysis_code.cli_impl.commands import root
for module in ('alysis_code.auth_diagnostics', 'alysis_code.provider_diagnostics',
               'alysis_code.sandbox_doctor', 'alysis_code.cli_impl.commands.auth'):
    assert module not in sys.modules, module
from alysis_code.cli_impl.commands import auth
assert root._JSON_OPTION_HELP == auth.JSON_OPTION_HELP
auth.credential_backend_fields = lambda: {'credential_backend': 'disposable-test'}
args = {'connection': 'test', 'authenticated': False, 'detail': 'not connected'}
assert root._auth_status_payload(**args) == auth._status_payload(**args)
from alysis_code import auth_diagnostics, provider_diagnostics, sandbox_doctor
sentinel = object()
calls = []
def record(*args, **kwargs):
    calls.append((args, kwargs))
    return sentinel
for module, name in ((auth_diagnostics, 'auth_doctor_payload'),
                     (provider_diagnostics, 'build_provider_diagnostics'),
                     (provider_diagnostics, 'validate_active_provider_live'),
                     (provider_diagnostics, 'validate_web_search_live'),
                     (provider_diagnostics, 'probe_reasoning_suppression_live'),
                     (sandbox_doctor, 'diagnose_sandbox')):
    setattr(module, name, record)
    assert getattr(root, name)(sentinel, timeout_s=1.25) is sentinel
    assert calls[-1] == ((sentinel,), {'timeout_s': 1.25})
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_command_registration_does_not_load_interactive_compatibility_surface() -> None:
    code = """
import sys
from typer.testing import CliRunner
from alysis_code import cli
from alysis_code.cli_impl.commands import root
assert cli.app is root.app
for args in (["--help"], ["--version"], ["ide-bridge", "--help"], ["forge", "--help"]):
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "alysis_code.cli_impl.commands.cli_surface" not in sys.modules
    assert "alysis_code.cli_impl.chat.loop" not in sys.modules

# Legacy Python callers still get the real exported objects. Loading the facade
# cannot replace an override already installed by an embedding caller.
sentinel = object()
cli.load_config = sentinel
assert callable(cli._home_panel)
from alysis_code.cli_impl.commands import cli_surface
assert cli._home_panel is cli_surface._home_panel
assert cli.load_config is sentinel
assert "_home_panel" in dir(cli)
namespace = {}
exec("from alysis_code.cli import *", namespace)
assert namespace["_home_panel"] is cli_surface._home_panel
assert namespace["load_config"] is sentinel
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_selected_cli_group_preserves_routing_without_loading_other_commands() -> None:
    code = """
import sys
from typer.testing import CliRunner
from typer.main import get_group
from alysis_code.cli_impl.commands import root, ide_bridge
for name in ("extensions", "mcp", "forge", "sessions", "skills"):
    assert "alysis_code.cli_impl.commands." + name not in sys.modules, name
assert "alysis_code.cli_impl.assets_cli" not in sys.modules
runner = CliRunner()
result = runner.invoke(root.app, ["ide-bridge", "--help"])
assert result.exit_code == 0, result.output
assert "health" in result.output
assert "alysis_code.cli_impl.commands.extensions" not in sys.modules

# Use the real root command routing. Its admission callback must still run
# before the selected command, including when the callback refuses the call.
calls = []
def refuse(flag):
    calls.append("root")
    raise RuntimeError("root refused")
original_guard = root._enforce_clean_build
root._enforce_clean_build = refuse
ide_bridge.run_stdio_bridge = lambda: (calls.append("ide"), 17)[1]
result = runner.invoke(root.app, ["ide-bridge", "--stdio"])
assert result.exit_code != 0
assert calls == ["root"]
root._enforce_clean_build = original_guard
result = runner.invoke(root.app, ["ide-bridge", "--stdio"])
assert result.exit_code == 17, result.output
assert calls == ["root", "ide"]

# All existing groups remain discoverable. Single-command groups must remain
# groups, and nested MCP/Forge commands retain their original paths.
expected = {"auth", "config", "profile", "update", "sessions", "forge", "server",
            "ext", "report", "mcp", "skill", "agents", "conventions", "tool",
            "hooks", "sandbox", "ide-bridge"}
assert expected <= set(get_group(root.app).list_commands(None))
for path in ([name] for name in sorted(expected)):
    result = runner.invoke(root.app, [*path, "--help"])
    assert result.exit_code == 0, (path, result.output, result.exception)
for path in (["forge", "assets"], ["mcp", "auth"], ["mcp", "prompts"]):
    result = runner.invoke(root.app, [*path, "--help"])
    assert result.exit_code == 0, (path, result.output, result.exception)
from alysis_code.cli_impl.commands.forge import forge_app
assert root.forge_app is forge_app
assert len([item for item in forge_app.registered_groups if item.name == "assets"]) == 1
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cold_host_executor_materializes_complete_canonical_dependencies() -> None:
    code = """
import sys
from alysis_code import cli
from alysis_code.cli_impl.forge import build_forge_host_dependency_factory, build_host_task_executor
from types import ModuleType
assert "alysis_code.cli_impl.commands.cli_surface" not in sys.modules
factory = build_forge_host_dependency_factory(cli, runtime_binding_for=lambda request: None)
assert callable(factory)
assert "alysis_code.cli_impl.commands.cli_surface" not in sys.modules
try:
    build_forge_host_dependency_factory(ModuleType("alysis_code.cli"), runtime_binding_for=lambda request: None)
except RuntimeError:
    pass
else:
    raise AssertionError("a lookalike module cannot supply execution authority")
executor = build_host_task_executor(cli)
assert callable(executor)
assert build_host_task_executor(cli) is executor
from alysis_code.cli_impl.chat import _sync_cli_globals
_sync_cli_globals(cli)
from alysis_code.cli_impl.chat import loop
assert loop.run_agent is cli.run_agent
from alysis_code import agent_loop
assert callable(agent_loop.run_agent)
assert "PIL.Image" not in sys.modules
from alysis_code.tools.image_generation import ImageGenerationError, _normalize_and_validate_image
try:
    _normalize_and_validate_image(raw_bytes=b"not an image", output_suffix=".png", max_bytes=1024, max_pixels=100)
except ImageGenerationError as exc:
    assert "invalid or unsupported" in str(exc)
else:
    raise AssertionError("first image use must still perform real decoding")
assert "PIL.Image" in sys.modules
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ide_registration_defers_session_and_swarm_execution() -> None:
    code = """
import io, sys
from alysis_code.ide import stdio_bridge
bridge = stdio_bridge.StdioBridge(stdin=io.StringIO(), stdout=io.StringIO())
assert "alysis_code.agent.session" not in sys.modules
assert "alysis_code.swarm_orchestrator" not in sys.modules
assert "alysis_code.agent.tools_assembly" not in sys.modules

# First use delegates to the real module, including the injected dependency
# boundary used by embedders. Arguments, return values and errors are preserved.
from alysis_code.agent import session
from alysis_code import swarm_orchestrator
from alysis_code.agent import browser_start, computer_use_tools, tool_definition, tools_assembly
from typing import get_type_hints
assert tool_definition.ToolDef is tools_assembly.ToolDef
assert browser_start.ToolDef is tools_assembly.ToolDef
assert computer_use_tools.ToolDef is tools_assembly.ToolDef
assert "visual_delivery" in get_type_hints(tool_definition.ToolDef)
sentinel = object()
calls = []
session.create_session = lambda **kwargs: (calls.append(kwargs), sentinel)[1]
assert bridge._create_session(root=sentinel) is sentinel
assert calls == [{"root": sentinel}]
swarm_orchestrator.run_swarm = lambda **kwargs: (calls.append(kwargs), 17)[1]
assert stdio_bridge.run_swarm(root=sentinel) == 17
assert calls[-1] == {"root": sentinel}
def fail(**kwargs):
    raise RuntimeError("execution refused")
session.create_session = fail
try:
    bridge._create_session(root=sentinel)
except RuntimeError as exc:
    assert str(exc) == "execution refused"
else:
    raise AssertionError("execution errors must propagate")

# Stop arriving during a cold import must prevent entry into the worker,
# whose setup can mutate a workspace before its own cancellation checkpoint.
import builtins, threading
from unittest.mock import patch
cancelled = threading.Event()
original_import = builtins.__import__
imports = []
def stop_during_import(name, *args, **kwargs):
    result = original_import(name, *args, **kwargs)
    if name == "swarm_orchestrator":
        imports.append(name)
        cancelled.set()
    return result
before = list(calls)
with patch("builtins.__import__", stop_during_import):
    try:
        stdio_bridge.run_swarm(root=sentinel, cancellation_event=cancelled)
    except stdio_bridge.BridgeCancellationError:
        pass
    else:
        raise AssertionError("Stop during import must prevent worker entry")
assert imports == ["swarm_orchestrator"]
assert calls == before

# An already-cancelled request should not even load execution dependencies.
with patch("builtins.__import__", side_effect=AssertionError("unexpected import")):
    try:
        stdio_bridge.run_swarm(root=sentinel, cancellation_event=cancelled)
    except stdio_bridge.BridgeCancellationError:
        pass
    else:
        raise AssertionError("Stop before import must prevent worker entry")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_and_plain_trace_imports_defer_optional_execution_and_rendering() -> None:
    code = """
import io, sys
import alysis_code.cli
import alysis_code.cli_impl.chat.commands
assert 'rich.markdown' not in sys.modules
for module in ('alysis_code.agent_loop', 'alysis_code.agent.session',
               'alysis_code.agent.tools_assembly',
               'alysis_code.ide.health',
               'alysis_code.ide.stdio_bridge', 'alysis_code.swarm_orchestrator',
               'alysis_code.llm.anthropic_messages',
               'alysis_code.llm.gemini_generate_content'):
    assert module not in sys.modules, module
from rich.console import Console
from alysis_code.surface.rich_surface import RichSurface
output = io.StringIO()
surface = RichSurface(console=Console(file=output, force_terminal=False, width=80))
surface._render_markdown('## Deferred heading\\n\\n**Rendered content**')
assert 'rich.markdown' in sys.modules
assert 'Deferred heading' in output.getvalue()
assert 'Rendered content' in output.getvalue()

# Importing the command preserves its callable boundary; actual dispatch loads
# the bridge and forwards its exit status without requiring a running IDE.
from alysis_code.cli_impl.commands import ide_bridge
from alysis_code.ide import health
health.health_payload = lambda: {'status': 'observed'}
assert ide_bridge.health_payload() == {'status': 'observed'}
from alysis_code.ide import stdio_bridge
stdio_bridge.run_stdio_bridge = lambda: 17
assert ide_bridge.run_stdio_bridge() == 17
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=_entrypoint_env(),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "module_name",
    [
        "alysis_code",
        "alysis_code.cli",
    ],
)
def test_module_entrypoint_help_outputs_cli_usage(module_name: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=_entrypoint_env(),
    )

    assert proc.returncode == 0
    help_text = proc.stdout + proc.stderr
    assert "Usage:" in help_text
    assert "run" in help_text
    assert "chat" in help_text


@pytest.mark.parametrize(
    "module_name",
    [
        "alysis_code",
        "alysis_code.cli",
    ],
)
def test_module_entrypoint_version_outputs_package_version(module_name: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", module_name, "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=_entrypoint_env(),
    )

    assert proc.returncode == 0
    # One line, version first, then the build stamp. The bare version stays the
    # leading token because downstream readers (the VS Code extension, the
    # managed-CLI smoke test, the release distribution validator) parse it by
    # prefix, non-emptiness and substring respectively.
    printed = proc.stdout.strip()
    assert printed.splitlines() == [printed]
    assert printed.split()[0] == __version__
    for marker in ("commit:", "built:", "dirty:"):
        assert marker in printed
    assert proc.stderr.strip() == ""
