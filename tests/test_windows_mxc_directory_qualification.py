"""Real host API observations exercise the verdict; they do not qualify MXC."""

import copy
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows directory APIs")
FIXTURE = Path(__file__).parent / "fixtures/windows_mxc_readonly_directory.py"


@pytest.fixture(scope="module")
def observations(tmp_path_factory):
    root = tmp_path_factory.mktemp("directory-verdict")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(FIXTURE), str(root), "--require-compatible"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[-1]["directory_compatibility"]["passed"] is True
    assert rows[-1]["directory_compatibility"]["required_checks"] == 55
    assert rows[-1]["directory_compatibility"]["observed_checks"] == 55
    return rows


def test_complete_native_directory_observations_pass(observations):
    verdict = runpy.run_path(str(FIXTURE))["directory_compatibility"]
    assert verdict(observations)["passed"] is True


@pytest.mark.parametrize("fault", ["missing", "duplicate", "denied", "wrong-type", "empty"])
def test_incomplete_or_failed_native_observations_cannot_qualify(observations, fault):
    verdict = runpy.run_path(str(FIXTURE))["directory_compatibility"]
    rows = copy.deepcopy(observations)
    index = next(i for i, row in enumerate(rows) if row.get("operation") == "SetCurrentDirectoryW")
    if fault == "missing":
        del rows[index]
    elif fault == "duplicate":
        rows.append(copy.deepcopy(rows[index]))
    elif fault == "denied":
        rows[index].update(success=False, winerror=5)
        # A perfect match with the HISTORICAL failure is still incompatible.
        rows[index]["matches_prior"] = True
    elif fault == "wrong-type":
        rows[index]["success"] = 1
    else:
        rows.clear()
    result = verdict(rows)
    assert result["passed"] is False
    assert result["failures"]
