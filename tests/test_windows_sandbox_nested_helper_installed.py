"""Opt-in negative control for the production wrapper's legacy exec route.

The nested helper receives only an inert command. No credential values are read
directly, and no setup, ACL, elevation or account-management command is invoked.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
from pathlib import Path

import pytest

from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.windows_sandbox import WindowsNativeRunner, resolve_helper

pytestmark = pytest.mark.skipif(
    platform.system() != "Windows" or os.environ.get("ALYSIS_RUN_INSTALLED_SANDBOX_TESTS") != "1",
    reason="requires explicitly provisioned Windows sandbox test VM",
)


def _acl_counts(state: Path) -> tuple[int, int]:
    with sqlite3.connect(state.as_uri() + "?mode=ro", uri=True) as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("working_aces", "ace_holders")
        )


@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_nested_legacy_exec_refuses_credentials_before_launch(tmp_path, network):
    store = Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox"
    journal = store / "execution-pending.json"
    assert not journal.exists(), "Complete native recovery before running this control."
    before = _acl_counts(store / "state.db")
    assert before == (0, 0), "Installed control requires an idle native sandbox."

    workspace, tools = tmp_path / "workspace", tmp_path / "tools"
    workspace.mkdir()
    tools.mkdir()
    helper = resolve_helper()
    nested = tools / "alysis-sandbox.exe"
    shutil.copyfile(helper, nested)
    assert (
        hashlib.sha256(nested.read_bytes()).digest() == hashlib.sha256(helper.read_bytes()).digest()
    )
    quoted_helper = "'" + str(nested).replace("'", "''") + "'"
    canary = "NESTED_LEGACY_EXEC_MUST_NOT_RUN"
    command = r"""
$ErrorActionPreference='Stop';
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class NestedTokenProbe {
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool GetTokenInformation(IntPtr t, int c, out int v, int n, out int r);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}' -ErrorAction Stop;
$t=[IntPtr]::Zero; $v=0; $r=0;
if (-not [NestedTokenProbe]::OpenProcessToken([IntPtr](-1), 8, [ref]$t)) { throw 'Token open failed' };
try {
 if (-not [NestedTokenProbe]::GetTokenInformation($t, 29, [ref]$v, 4, [ref]$r)) { throw 'Token query failed' };
 if ($v -ne 1) { throw 'Control did not run inside AppContainer' };
} finally { [NestedTokenProbe]::CloseHandle($t) | Out-Null };
"""
    command += f"$nestedHelper={quoted_helper};\n"
    command += r"""
$protocolOutput = (& $nestedHelper protocol | Out-String);
if ($LASTEXITCODE -ne 0) { throw 'Nested helper execution control failed' };
$protocol = $protocolOutput | ConvertFrom-Json;
if ($protocol.protocol -ne 3 -or $protocol.security_revision -ne 4) { throw 'Wrong nested helper' };
$shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe';
$ErrorActionPreference='Continue';
"""
    command += (
        "$nestedOutput = @(& $nestedHelper exec --quiet -- $shell -NoLogo -NoProfile "
        f"-NonInteractive -Command \"Write-Output '{canary}'\" 2>&1 | "
        "ForEach-Object { $_.ToString() });\n"
    )
    command += r"""
$nestedExit = $LASTEXITCODE;
$ErrorActionPreference='Stop';
@{appcontainer=$v; protocol=$protocol.protocol; exit_code=$nestedExit;
  output=($nestedOutput -join "`n")} | ConvertTo-Json -Compress;
"""
    result = WindowsNativeRunner(
        ShellSandboxSettings(
            backend="windows-native", network=network, windows_read_roots=(str(tools),)
        )
    ).run(root=workspace, cwd=workspace, cmd=command, timeout_s=45)
    assert not journal.exists(), "Preserve the fixture and complete normal native recovery."
    assert _acl_counts(store / "state.db") == before, "Nested helper changed ACL lifecycle state."
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["appcontainer"] == 1
    assert evidence["protocol"] == 3
    # Exit 15 is the raw exec credential-admission refusal. A missing binary,
    # child launch error or setup failure in the outer helper is not evidence.
    assert evidence["exit_code"] == 15, evidence
    assert "sandbox user not provisioned" in evidence["output"], evidence
    assert "cannot open owner-only credential store" in evidence["output"], evidence
    assert canary not in evidence["output"]
    assert canary not in result.stderr


@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_raw_runner_cannot_gain_authority(tmp_path, network):
    store = Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox"
    journal = store / "execution-pending.json"
    assert not journal.exists(), "Complete native recovery before running this control."
    before = _acl_counts(store / "state.db")
    assert before == (0, 0), "Installed control requires an idle native sandbox."
    workspace, tools = tmp_path / "workspace", tmp_path / "tools"
    workspace.mkdir()
    tools.mkdir()
    private = tmp_path / "private-canary.txt"
    private.write_text("unchanged synthetic data", encoding="utf-8")
    # The normal host can open this object for both operations. The descendant
    # probes handles only; even an unexpected success never changes its bytes.
    with private.open("r+b"):
        pass
    nested = tools / "alysis-sandbox.exe"
    helper = resolve_helper()
    shutil.copyfile(helper, nested)
    assert (
        hashlib.sha256(nested.read_bytes()).digest() == hashlib.sha256(helper.read_bytes()).digest()
    )

    token_probe = r"""
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class RawRunnerTokenProbe {
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool GetTokenInformation(IntPtr t, int c, out int v, int n, out int r);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}' -ErrorAction Stop;
$t=[IntPtr]::Zero; $v=0; $r=0;
if (-not [RawRunnerTokenProbe]::OpenProcessToken([IntPtr](-1), 8, [ref]$t)) { throw 'Token open failed' };
try {
 if (-not [RawRunnerTokenProbe]::GetTokenInformation($t, 29, [ref]$v, 4, [ref]$r)) { throw 'Token query failed' };
 if ($v -ne 1) { throw 'Process is outside AppContainer' };
} finally { [RawRunnerTokenProbe]::CloseHandle($t) | Out-Null };
"""
    target = tools / "authority-probe.ps1"
    quoted_private = "'" + str(private).replace("'", "''") + "'"
    target.write_text(
        "$ErrorActionPreference='Stop';\n"
        + token_probe
        + f"$private={quoted_private};\n"
        + r"""
foreach ($access in @([System.IO.FileAccess]::Read, [System.IO.FileAccess]::Write)) {
 try {
  $stream=[System.IO.File]::Open($private, [System.IO.FileMode]::Open, $access);
  $stream.Dispose(); throw 'Private file authority gained';
 } catch [System.UnauthorizedAccessException] { }
};
@{marker='RAW_RUNNER_REMAINS_CONTAINED'; appcontainer=$v;
 user_sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value} | ConvertTo-Json -Compress;
""",
        encoding="utf-8",
    )
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    payload = json.dumps(
        {
            "Exec": {
                "owner_sid": None,
                "argv": [
                    str(shell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(target),
                ],
                "env_overlay": [],
            }
        }
    ).encode("utf-8")
    spec = tools / "runner-spec.bin"
    spec.write_bytes(len(payload).to_bytes(4, "little") + payload)
    stdout, stderr = workspace / "runner.stdout", workspace / "runner.stderr"
    # File redirection avoids qualifying a second anonymous-pipe implementation.
    # cmd /d prevents AutoRun; all names are owned fixtures and quoted literally.
    redirect = f'"{nested}" runner < "{spec}" > "{stdout}" 2> "{stderr}"'
    command = "$ErrorActionPreference='Stop';\n" + token_probe
    command += f"$helper='{str(nested).replace(chr(39), chr(39) * 2)}';\n"
    command += "$protocol=(& $helper protocol | Out-String | ConvertFrom-Json);\n"
    command += (
        "if ($LASTEXITCODE -ne 0 -or $protocol.protocol -ne 3) { throw 'Helper control failed' };\n"
    )
    command += f"$redirect='{redirect.replace(chr(39), chr(39) * 2)}';\n"
    command += r"""
$psi=New-Object System.Diagnostics.ProcessStartInfo;
$psi.FileName=Join-Path $env:SystemRoot 'System32\cmd.exe';
$psi.Arguments='/d /s /c "'+$redirect+'"';
$psi.UseShellExecute=$false; $psi.CreateNoWindow=$true;
$process=New-Object System.Diagnostics.Process; $process.StartInfo=$psi;
try {
 if (-not $process.Start()) { throw 'Raw runner control did not start' };
 if (-not $process.WaitForExit(5000)) { $process.Kill(); throw 'Raw runner exceeded five seconds' };
 @{appcontainer=$v; exit_code=$process.ExitCode;
   user_sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value} | ConvertTo-Json -Compress;
} finally { $process.Dispose() };
"""
    result = WindowsNativeRunner(
        ShellSandboxSettings(
            backend="windows-native", network=network, windows_read_roots=(str(tools),)
        )
    ).run(root=workspace, cwd=workspace, cmd=command, timeout_s=30)
    assert not journal.exists(), "Preserve the fixture and complete normal native recovery."
    assert _acl_counts(store / "state.db") == before
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["appcontainer"] == 1
    out, error = stdout.read_text(errors="replace"), stderr.read_text(errors="replace")
    if evidence["exit_code"] == 0:
        child = json.loads(out)
        assert child == {
            "marker": "RAW_RUNNER_REMAINS_CONTAINED",
            "appcontainer": 1,
            "user_sid": evidence["user_sid"],
        }
    else:
        # Accept a real authority/containment refusal, not JSON/schema, file
        # resolution, unsupported entry point or fixture construction errors.
        assert evidence["exit_code"] == 1, (evidence, error)
        # Windows rejects this lowbox/LUA restricted-token combination with
        # ERROR_INVALID_PARAMETER on the qualified baseline. This is a token
        # construction refusal after valid RunnerCmd dispatch, not bad JSON.
        token_refused = (
            "make_sandbox_token: CreateRestrictedToken(disable=[Admins, logon-SIDs])" in error
            and "0x80070057" in error
        )
        assert token_refused or "Access is denied" in error or "0x80070005" in error, error
        assert any(
            stage in error
            for stage in (
                "protect runner process",
                "make_sandbox_token",
                "create containment job",
                "CreateProcessAsUserW",
                "OpenProcessToken",
            )
        ), error
        assert "RAW_RUNNER_REMAINS_CONTAINED" not in out
        print(
            json.dumps(
                {
                    "network": network,
                    "raw_runner_exit": evidence["exit_code"],
                    "refusal": error.strip(),
                    "journal_absent": True,
                    "acl_counts": before,
                }
            )
        )
    assert private.read_text(encoding="utf-8") == "unchanged synthetic data"
