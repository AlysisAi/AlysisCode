"""Offline event/timer simulations for the fixed installed HTTPS fixture."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_fixed_native_https_probe_failure_and_redaction_controls():
    node = shutil.which("node")
    if not node:
        pytest.skip("optional offline HTTPS fixture harness requires Node.js 20+")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True, timeout=5
    )
    release = re.fullmatch(
        r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version.stdout.strip()
    )
    assert release is not None, "Expected a Node.js release version in vMAJOR.MINOR.PATCH form"
    if int(release.group(1)) < 20:
        pytest.skip(
            "optional offline HTTPS fixture harness unavailable: requires Node.js 20+; "
            "no HTTPS simulation coverage on this runtime"
        )
    fixture = Path(__file__).parent / "fixtures" / "windows_native_https_probe.test.cjs"
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", str(fixture)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout
    assert "# skipped 0" in result.stdout
    assert "# tests 20" in result.stdout
