"""Configuration and catalog handling must not initialize unused connections."""

import os
import subprocess
import sys
from pathlib import Path


def test_cold_mcp_catalog_defers_clients_and_keeps_public_contracts(tmp_path: Path):
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        ALYSIS_CONFIG_DIR=str(tmp_path / "config"),
        ALYSIS_DATA_DIR=str(tmp_path / "data"),
    )
    code = """
import dataclasses, json, pickle, sys, typing
from pathlib import Path
import alysis_code.mcp as mcp
from alysis_code.mcp import manager
from alysis_code.mcp.catalog import McpListedTool
from alysis_code.mcp.config import user_mcp_config_path
root = Path(sys.argv[1])
unused = (
    'alysis_code.mcp.client', 'alysis_code.mcp.transport_http',
    'alysis_code.mcp.transport_stdio', 'alysis_code.mcp.oauth',
)
empty = mcp.create_mcp_manager(workspace_root=root, runtime_kind='one_shot')
assert empty.tool_bindings == ()
assert empty.catalog_snapshot_metadata()['exposed_tool_count'] == 0
empty.close()
config = user_mcp_config_path()
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(json.dumps({'servers': {
    'disabled': {'transport': 'stdio', 'command': 'unused', 'enabled': False}
}}))
disabled = mcp.create_mcp_manager(workspace_root=root, runtime_kind='one_shot')
assert len(disabled.resolved_servers) == 1
assert disabled.active_servers == ()
assert disabled.tool_bindings == ()
disabled.close()
# A rejected trust decision must still precede connection construction.
server = dataclasses.replace(disabled.resolved_servers[0], enabled=True, trust='none')
resolved = dataclasses.replace(disabled.resolved_config, servers=(server,))
untrusted = manager.McpManager(
    resolved_config=resolved, workspace_root=root, runtime_kind='one_shot'
)
assert untrusted.tool_bindings == ()
assert 'disabled' in untrusted.server_startup_errors()
untrusted.close()
assert all(name not in sys.modules for name in unused), [
    name for name in unused if name in sys.modules
]
tool = McpListedTool('example', 'description', {'type': 'object'}, {'private': True})
assert [field.name for field in dataclasses.fields(tool)] == [
    'name', 'description', 'input_schema', 'raw_payload'
]
assert typing.get_type_hints(McpListedTool)['name'] is str
assert 'raw_payload' not in repr(tool)
try:
    tool.name = 'changed'
except dataclasses.FrozenInstanceError:
    pass
else:
    raise AssertionError('catalog entries must remain frozen')
assert pickle.loads(pickle.dumps(tool)) == tool
assert 'alysis_code.mcp.client' not in sys.modules
exports = {}
exec('from alysis_code.mcp.manager import *', exports)
assert 'McpHttpClient' in exports and 'McpStdioClient' in exports
# Old pickles name the former defining module; that lookup must still work.
assert pickle.loads(b'calysis_code.mcp.client\\nMcpListedTool\\n.') is McpListedTool
from alysis_code.mcp import client
assert client.McpListedTool is McpListedTool
assert manager.McpListedTool is McpListedTool
for name in ('McpHttpClient', 'McpStdioClient'):
    assert getattr(mcp, name) is getattr(client, name)
    assert getattr(manager, name) is getattr(client, name)
    assert exports[name] is getattr(client, name)
assert mcp.MCP_PROTOCOL_VERSION == client.MCP_PROTOCOL_VERSION
assert set(mcp.__all__).issubset(dir(mcp))
assert {'McpHttpClient', 'McpStdioClient'}.issubset(dir(manager))
assert typing.get_type_hints(client.McpHttpClient)['transport'] is client.McpHttpTransport
from alysis_code.mcp.visual_delivery import McpVisualDelivery
assert typing.get_type_hints(
    manager.McpToolBinding, localns={'McpVisualDelivery': McpVisualDelivery}
)
for module in (mcp, manager):
    try:
        module.unknown_public_export
    except AttributeError:
        pass
    else:
        raise AssertionError('unknown attributes must not resolve')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
