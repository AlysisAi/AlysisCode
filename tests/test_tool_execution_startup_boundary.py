"""Registration must preserve policy without loading unused tool implementations."""

import os
import subprocess
import sys
from pathlib import Path


def test_cold_tool_registration_and_real_dispatch_preserve_public_callables(tmp_path: Path):
    code = """
import importlib, inspect, sys
from pathlib import Path
from alysis_code import agent_loop
from alysis_code.agent import tools_assembly
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
root = Path(sys.argv[1])
(root / 'example.py').write_text('def synthetic_example():\\n    return 1\\n')
store = SessionStore(enabled=False, sessions_dir=root / 'sessions', session_id='cold-tools',
                     cwd=str(root), repo_root=str(root))
tools = agent_loop.build_tools(root=root, console=None, store=store, mode='readonly', yes=False,
                              cfg=AppConfig(model='synthetic', web_search_mode='off',
                                            extra_fields={'shell_sandbox': {'mode': 'off'}}),
                              non_interactive=True)
assert 'symbol_search' in tools and 'repo_map' in tools and 'search_rg' in tools
assert 'shell_run' not in tools and 'git_apply_patch' not in tools
assert 'alysis_code.ide.managed_browser' not in sys.modules
assert 'alysis_code.ide.browser_egress_proxy' not in sys.modules
# Cold annotation introspection resolves the actual type through its namespace.
import typing
hints = typing.get_type_hints(tools_assembly.build_tools)
from alysis_code.ide.managed_browser import ManagedBrowserService
assert hints['managed_browser_service'] == ManagedBrowserService | None
assert set(tools_assembly._EXECUTION_IMPORTS) <= set(dir(tools_assembly))
for module in set(tools_assembly._EXECUTION_IMPORTS.values()):
    assert module not in sys.modules, module
# A temporary assembly override must not become a permanent facade export.
assert 'symbol_search' not in agent_loop.__dict__
assert 'symbol_search' not in tools_assembly.__dict__
temporary = lambda **kwargs: {'temporary': True}
tools_assembly.symbol_search = temporary
canonical = agent_loop.symbol_search
assert canonical is not temporary
del tools_assembly.symbol_search
result = tools['symbol_search'].run({'query': 'synthetic_example', 'exact': True})
assert any(match['name'] == 'synthetic_example' for match in result['matches']), result
assert tools_assembly.symbol_search is canonical
agent_loop.symbol_search = temporary
assert tools['symbol_search'].run({'query': 'synthetic_example'}) == {'temporary': True}
agent_loop.symbol_search = canonical
assert 'alysis_code.tools.symbols' in sys.modules
assert 'alysis_code.tools.web' not in sys.modules
# Compatibility exports return real callables, with their actual signatures.
namespace = {}
exec('from alysis_code.agent.tools_assembly import *', namespace)
for name, module in tools_assembly._EXECUTION_IMPORTS.items():
    expected = getattr(importlib.import_module(module), name)
    assert namespace[name] is expected, name
    assert getattr(tools_assembly, name) is expected, name
    assert inspect.signature(namespace[name]) == inspect.signature(expected)
for name in tools_assembly._MANAGED_BROWSER_EXPORTS:
    expected = getattr(importlib.import_module('alysis_code.ide.managed_browser'), name)
    assert namespace[name] is expected, name
    assert getattr(tools_assembly, name) is expected, name
public = {}
exec('from alysis_code.agent_loop import *', public)
for name in ('search_rg', 'shell_run', 'symbol_search', 'web_fetch'):
    assert public[name] is getattr(tools_assembly, name), name
from alysis_code.tools import errors
import pickle
for module, name in (('git', 'GitError'), ('history', 'HistorySearchError'),
                     ('search', 'SearchError'), ('shell', 'ShellError'),
                     ('symbols', 'SymbolSearchError')):
    old_module = importlib.import_module('alysis_code.tools.' + module)
    error_type = getattr(errors, name)
    assert getattr(old_module, name) is error_type
    assert pickle.loads(('calysis_code.tools.' + module + '\\n' + name + '\\n.').encode()) is error_type
    original = error_type('synthetic failure')
    restored = pickle.loads(pickle.dumps(original))
    assert type(restored) is error_type and restored.args == original.args
payload = {'exit_code': 1}
assert errors.ShellError('failed', result_payload=payload).result_payload is payload
"""
    # Private engine state is a sibling of the ordinary source tree. A recursive
    # source scan must still refuse a workspace that contains private state.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", code, str(workspace)],
        env=dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            ALYSIS_CONFIG_DIR=str(tmp_path / "config"),
            ALYSIS_DATA_DIR=str(tmp_path / "data"),
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dispatch_honors_existing_python_override(monkeypatch):
    from alysis_code.agent import tools_assembly

    def sentinel(**kwargs):
        return kwargs

    monkeypatch.setattr(tools_assembly, "symbol_search", sentinel)
    assert tools_assembly._execution_dependency("symbol_search") is sentinel
