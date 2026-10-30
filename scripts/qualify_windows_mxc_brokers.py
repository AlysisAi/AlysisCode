"""Exact synthetic Shell.Application / WScript.Shell probes; no real user data."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from alysis_code import windows_sandbox
from alysis_code.sandbox_runner import build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Native Windows is required")
    if not args.output.is_absolute() or args.output.exists():
        parser.error("--output must be a new absolute directory; previous evidence is retained")
    if not args.helper.is_absolute() or not args.helper.is_file():
        parser.error("--helper must be an existing absolute helper path")
    selected_node = shutil.which("node")
    if not selected_node:
        parser.error("Node.js must be available for the bounded synthetic child")
    root = args.output
    helper = args.helper
    work, outside = root / "workspace", root / "outside"
    work.mkdir(parents=True)
    outside.mkdir()
    node = Path(selected_node).resolve(strict=True)
    ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    os.environ["ALYSIS_WINDOWS_MXC_HELPER"] = str(helper)
    os.environ["ALYSIS_WINDOWS_MXC_SHA256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
    os.environ["ALYSIS_CONFIG_DIR"] = str(root / "config")
    os.environ["ALYSIS_DATA_DIR"] = str(root / "data")
    for name in ["config", "data"]:
        (root / name).mkdir()
    shutil.copyfile(__file__, root / "harness.py")
    canary = outside / "read-canary.txt"
    canary_value = uuid.uuid4().hex
    canary.write_text(canary_value)
    child = work / "broker-child.cjs"
    child.write_text(
        """const fs=require('node:fs');
    const [complete,outside,nonce,canary]=process.argv.slice(2);let r={nonce,pid:process.pid};
    try {fs.readFileSync(canary);r.outside_read='read'} catch(e){r.outside_read=e.code}
    try {fs.writeFileSync(outside,nonce,{flag:'wx'});r.outside='written'}
    catch(e){r.outside=e.code}
    fs.writeFileSync(complete,JSON.stringify(r),{flag:'wx'});
    """,
        encoding="utf-8",
    )
    fixture = work / "broker.ps1"
    fixture.write_text(
        r"""
    param([string]$Config)
    $ErrorActionPreference='Stop'
    $configData = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
    $stage = 'activation'
    $broker = $null
    try {
      if ($configData.kind -eq 'shell') { $broker = New-Object -ComObject Shell.Application }
      elseif ($configData.kind -eq 'wscript') { $broker = New-Object -ComObject WScript.Shell }
      else { throw 'Unknown synthetic broker' }
      $stage = 'launch'
      if ($configData.kind -eq 'shell') {
        $broker.ShellExecute([string]$configData.node,[string]$configData.arguments,[string]$configData.cwd,'open',0)
        # ShellExecute is asynchronous. Keep the contained parent alive long enough
        # to observe its child; returning now would immediately drain the native job.
        $deadline = [DateTime]::UtcNow.AddSeconds(8)
        while (-not (Test-Path -LiteralPath $configData.complete) -and [DateTime]::UtcNow -lt $deadline) {
          Start-Sleep -Milliseconds 30
        }
        @{ok=$true;stage=$stage;completion_seen=(Test-Path -LiteralPath $configData.complete)} | ConvertTo-Json -Compress
      } else {
        $command = '"'+[string]$configData.node+'" '+[string]$configData.arguments
        $exitCode = $broker.Run($command,0,$true)
        @{ok=$true;stage=$stage;exit_code=$exitCode} | ConvertTo-Json -Compress
      }
    } catch {
      $exception = $_.Exception
      while ($null -ne $exception.InnerException) {$exception=$exception.InnerException}
      @{ok=$false;stage=$stage;hresult=$exception.HResult;error_type=$exception.GetType().FullName;
        access_denied=($exception.HResult -eq -2147024891 -or $exception -is [UnauthorizedAccessException])
      } | ConvertTo-Json -Compress
    } finally {
      if ($null -ne $broker) {[Runtime.InteropServices.Marshal]::FinalReleaseComObject($broker) | Out-Null}
    }
    """,
        encoding="utf-8",
    )
    fixture_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [child, fixture]}
    record = {
        "root": str(root),
        "status": "running",
        "cases": [],
        "helper_sha256": os.environ["ALYSIS_WINDOWS_MXC_SHA256"],
        "fixture_sha256": fixture_hashes,
        "engine_module": windows_sandbox.__file__,
        "engine_module_sha256": hashlib.sha256(
            Path(windows_sandbox.__file__).read_bytes()
        ).hexdigest(),
        "scope": "Two exact local automation brokers with bounded synthetic child and outside-read/write proof; no task registration, real user data or credential reads.",
    }

    def save():
        (root / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    def probe(kind, label, network=None):
        nonce = uuid.uuid4().hex
        complete = work / (label + "-complete.json")
        marker = outside / (label + "-outside.txt")
        config = work / (label + "-config.json")
        config.write_text(
            json.dumps(
                {
                    "kind": kind,
                    "node": str(node),
                    "cwd": str(work),
                    "complete": str(complete),
                    "arguments": subprocess.list2cmdline(
                        [str(child), str(complete), str(marker), nonce, str(canary)]
                    ),
                }
            ),
            encoding="utf-8",
        )
        argv = [
            str(ps),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(fixture),
            "-Config",
            str(config),
        ]
        if network is None:
            run = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        else:
            runner = build_shell_runner_from_settings(
                ShellSandboxSettings(
                    backend="windows-mxc", network=network, windows_read_roots=(str(node.parent),)
                ),
                work,
            )
            command = (
                "& '"
                + str(fixture).replace("'", "''")
                + "' -Config '"
                + str(config).replace("'", "''")
                + "'; exit $LASTEXITCODE"
            )
            run = runner.run(root=work, cwd=work, cmd=command, timeout_s=20)
        event = {
            "label": label,
            "kind": kind,
            "network": network,
            "exit_code": run.returncode,
            "stdout": run.stdout,
            "stderr": run.stderr,
        }
        record["cases"].append(event)
        save()
        assert run.returncode == 0, event
        result = json.loads(run.stdout)
        event["launch"] = result
        if result.get("ok"):
            deadline = time.monotonic() + 8
            while not complete.exists() and time.monotonic() < deadline:
                time.sleep(0.04)
        event["outside_created"] = marker.exists()
        if complete.exists():
            event["child"] = json.loads(complete.read_text())
        if network is None:
            assert (
                result.get("ok")
                and marker.read_text() == nonce
                and event["child"]["nonce"] == nonce
                and event["child"]["outside_read"] == "read"
            ), event
            event["qualified"] = "host-positive-control"
        else:
            assert not marker.exists(), "Broker escaped: " + json.dumps(event)
            if result.get("access_denied"):
                event["qualified"] = "explicit-broker-access-denied"
            elif (
                result.get("ok")
                and event.get("child", {}).get("nonce") == nonce
                and event["child"]["outside"] in ["EACCES", "EPERM"]
                and event["child"]["outside_read"] in ["EACCES", "EPERM"]
            ):
                event["qualified"] = "child-executed-with-outside-read-and-write-denied"
            else:
                raise AssertionError("Inconclusive broker outcome: " + json.dumps(event))
        save()

    try:
        for kind in ["shell", "wscript"]:
            probe(kind, kind + "-host-before")
            for network in ["off", "on"]:
                probe(kind, kind + "-contained-" + network, network)
            probe(kind, kind + "-host-after")
        assert canary.read_text() == canary_value
        assert fixture_hashes == {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [child, fixture]
        }
        record["fixtures_unchanged"] = True
        record["status"] = "passed"
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)
        raise
    finally:
        save()
        print(
            json.dumps(
                {"root": str(root), "status": record["status"], "cases": len(record["cases"])}
            )
        )


if __name__ == "__main__":
    main()
