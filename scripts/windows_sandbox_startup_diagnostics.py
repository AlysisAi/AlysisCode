"""Two fixed, contained startup observations after an installed smoke failure.

Uses the real request policy and lifecycle. Only the diagnostic PowerShell
source differs. No permissions, deadlines or production commands are changed.
Failed workspaces are retained; pending recovery prevents further launches.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alysis_code import windows_sandbox as native  # noqa: E402
from alysis_code.config import ConfigError  # noqa: E402
from alysis_code.sandbox_settings import ShellSandboxSettings  # noqa: E402

ENTRY = "ALYSIS_DIAGNOSTIC_POWERSHELL_ENTRY"
DONE = "ALYSIS_DIAGNOSTIC_BOOTSTRAP_DONE"


def main() -> None:
    report: dict[str, object] = {"schema": 1, "diagnostic_only": True, "cases": []}
    if os.name != "nt" or os.environ.get("ALYSIS_RUN_INSTALLED_SANDBOX_TESTS") != "1":
        report["category"] = "requires_installed_windows_test_environment"
        print(json.dumps(report))
        return
    journal = Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox/execution-pending.json"
    original_builder = native.build_request
    cases = []
    for kind in ("powershell_entry", "production_bootstrap"):
        if journal.exists():
            report["category"] = "recovery_pending_no_further_launch"
            break
        root = Path(tempfile.mkdtemp(prefix="alysis-startup-observation-"))

        def request(*args, _kind=kind, **kwargs):
            payload = original_builder(*args, **kwargs)
            if payload["argv"][-2] != "-Command":
                raise ValueError("unexpected diagnostic shell argument layout")
            source = f"[Console]::Out.WriteLine('{ENTRY}'); "
            if _kind == "production_bootstrap":
                source += payload["argv"][-1]
            payload["argv"][-1] = source
            return payload

        record: dict[str, object] = {"query": kind, "timeout_seconds": 15}
        output: str | bytes = ""
        try:
            with patch.object(native, "build_request", request):
                result = native.WindowsNativeRunner(
                    ShellSandboxSettings(backend="windows-native", network="off")
                ).run(root=root, cwd=root, cmd=f"[Console]::Out.WriteLine('{DONE}')", timeout_s=15)
            output = result.stdout
            record.update(category="completed", returncode=result.returncode)
        except subprocess.TimeoutExpired as error:
            output = error.output or ""
            record["category"] = "timeout"
        except (ConfigError, OSError, subprocess.SubprocessError):
            record["category"] = "launch_or_cleanup_failed"
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        record["powershell_entry_reached"] = ENTRY in output.splitlines()
        record["bootstrap_completed"] = DONE in output.splitlines()
        record["recovery_pending"] = journal.exists()
        record["workspace_retained"] = True
        cases.append(record)
    report["cases"] = cases
    encoded = json.dumps(report, indent=2) + "\n"
    destination = (
        Path(__file__).resolve().parents[1]
        / "native/windows-sandbox/target/startup-diagnostics.json"
    )
    destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
