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
    totals = [
        re.search(pattern, result.stdout, re.MULTILINE)
        for pattern in (r"^1\.\.(\d+)$", r"^# tests (\d+)$", r"^# pass (\d+)$")
    ]
    assert all(totals), result.stdout
    counts = [int(total.group(1)) for total in totals if total is not None]
    assert counts[0] > 0 and len(set(counts)) == 1, result.stdout
