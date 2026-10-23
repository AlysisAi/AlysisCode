"""Windows installer failure paths with real uv and isolated tool directories."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell installer")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install.ps1"


@pytest.fixture(params=["powershell", "pwsh"])
def installer(tmp_path, request):
    shell = shutil.which(request.param)
    uv = shutil.which("uv")
    if not shell or not uv:
        pytest.skip(f"{request.param} and uv are required")
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith(("ALYSIS_", "UV_"))
    }
    environment.update(
        UV_TOOL_DIR=str(tmp_path / "tools"),
        UV_TOOL_BIN_DIR=str(tmp_path / "bin with spaces"),
        UV_NO_CONFIG="1",
        UV_OFFLINE="1",
        ALYSIS_NO_MODIFY_PATH="1",
    )
    environment["PATH"] = str(Path(uv).parent) + os.pathsep + environment["PATH"]

    def run():
        return subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return environment, run


@pytest.mark.parametrize("entry_point", ["alysis.exe", "alysis-code.exe", "alysis-ide-bridge.exe"])
def test_conflicting_executable_is_preserved(installer, entry_point):
    environment, run = installer
    directory = Path(environment["UV_TOOL_BIN_DIR"])
    directory.mkdir()
    executable = directory / entry_point
    executable.write_bytes(b"existing installation")
    result = run()
    assert result.returncode != 0
    assert "already exists" in result.stdout + result.stderr
    assert executable.read_bytes() == b"existing installation"
    assert "Installing Alysis Code" not in result.stdout


def test_uv_failure_stops_installation(installer):
    environment, run = installer
    Path(environment["UV_TOOL_DIR"]).write_text("not a directory")
    result = run()
    assert result.returncode != 0
    assert "uv failed" in result.stdout + result.stderr
    assert "is installed." not in result.stdout


def test_invalid_version_is_rejected(installer):
    environment, run = installer
    environment["ALYSIS_VERSION"] = "0.15.2; Write-Output unexpected"
    result = run()
    assert result.returncode != 0
    assert "ALYSIS_VERSION must be" in result.stdout + result.stderr
    assert "Installing Alysis Code" not in result.stdout
