"""Plugin activation must not initialize the plugin installation machinery."""

import os
import subprocess
import sys
from pathlib import Path


def test_plugin_activation_defers_installation_and_preserves_public_api(tmp_path: Path):
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(
        os.environ,
        PYTHONPATH=str(source),
        ALYSIS_CONFIG_DIR=str(tmp_path / "config"),
        ALYSIS_DATA_DIR=str(tmp_path / "data"),
    )
    code = """
import importlib, json, sys
from pathlib import Path
from alysis_code.extensions.activation import resolve_active_plugins
from alysis_code.extensions.models import ExtensionState
from alysis_code.extensions.paths import project_extensions_path
from alysis_code.extensions.state import save_global_state
import alysis_code.extensions as extensions
root = Path(sys.argv[1])
save_global_state(ExtensionState(enabled=['example.enabled']))
decision = resolve_active_plugins(repo_root=root, workspace_trust_prompt=None)
assert decision.enabled_plugin_ids == frozenset({'example.enabled'})
assert not decision.workspace_trust_granted
# Untrusted project changes must still be ignored; lazy loading never grants trust.
overrides = project_extensions_path(root)
overrides.parent.mkdir(parents=True, exist_ok=True)
overrides.write_text(json.dumps({'enabled': ['example.untrusted']}))
decision = resolve_active_plugins(repo_root=root, workspace_trust_prompt=None)
assert decision.enabled_plugin_ids == frozenset({'example.enabled'})
assert decision.untrusted_project_plugin_ids == frozenset({'example.untrusted'})
assert 'alysis_code.extensions.install' not in sys.modules
assert 'alysis_code.skills.install' not in sys.modules
assert extensions.resolve_active_plugins is resolve_active_plugins
assert set(extensions.__all__) == set(extensions._EXPORT_MODULES)
assert set(extensions.__all__).issubset(dir(extensions))
for name in extensions.__all__:
    module = importlib.import_module('.' + extensions._EXPORT_MODULES[name], extensions.__name__)
    assert getattr(extensions, name) is getattr(module, name), name
assert 'alysis_code.extensions.install' in sys.modules
try:
    extensions.unknown_public_export
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
