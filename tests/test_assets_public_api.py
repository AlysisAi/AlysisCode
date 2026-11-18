"""Local file viewers import without loading optional processing engines."""

import os
import subprocess
import sys
from pathlib import Path


def test_browser_viewers_share_the_local_asset_viewer():
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ, PYTHONPATH=str(source))
    code = """
import sys
from alysis_code.embedded_browser_visual import ComputerUseViewer, EmbeddedBrowserViewer
from alysis_code.assets.local_tools import LocalAssetViewer
assert issubclass(ComputerUseViewer, LocalAssetViewer)
assert issubclass(EmbeddedBrowserViewer, LocalAssetViewer)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
