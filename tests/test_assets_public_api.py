"""Asset metadata imports must not eagerly load optional processing engines."""

import os
import subprocess
import sys
from pathlib import Path


def test_asset_index_import_defers_processing_and_public_exports_keep_identity():
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ, PYTHONPATH=str(source))
    code = """
import importlib, sys
from alysis_code.assets.index import AssetIndex
import alysis_code.assets as assets
assert 'alysis_code.assets.asset_read_core' not in sys.modules
assert 'alysis_code.assets.surface' not in sys.modules
assert 'alysis_code.assets.ocr' not in sys.modules
assert assets.AssetIndex is AssetIndex
assert set(assets.__all__) == set(assets._EXPORT_MODULES)
assert set(assets.__all__).issubset(dir(assets))
for name in assets.__all__:
    expected = getattr(importlib.import_module('.' + assets._EXPORT_MODULES[name], assets.__name__), name)
    assert getattr(assets, name) is expected, name
    assert getattr(assets, name) is expected, name
try:
    assets.unknown_public_export
except AttributeError:
    pass
else:
    raise AssertionError('unknown attributes must not resolve')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_browser_viewer_import_does_not_initialize_asset_ingestion():
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ, PYTHONPATH=str(source))
    code = """
import sys
from alysis_code.embedded_browser_visual import ComputerUseViewer, EmbeddedBrowserViewer
from alysis_code.assets.local_tools import LocalAssetViewer
assert issubclass(ComputerUseViewer, LocalAssetViewer)
assert issubclass(EmbeddedBrowserViewer, LocalAssetViewer)
assert 'alysis_code.assets.ingestion' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
