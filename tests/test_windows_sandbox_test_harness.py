"""The CI ownership scope changes only fixture birth ownership, then restores it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows token ownership")
ROOT = Path(__file__).resolve().parents[1]


def _powershell(script: str, tmp_path: Path) -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None, "Windows qualification requires PowerShell"
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
        env={
            **os.environ,
            "ALYSIS_TEST_OWNER_SCRIPT": str(ROOT / "scripts" / "windows_test_owner_scope.ps1"),
            "ALYSIS_TEST_RUN_SCRIPT": str(ROOT / "scripts" / "test_windows_sandbox.ps1"),
            "ALYSIS_TEST_OWNER_ROOT": str(tmp_path),
            "ALYSIS_TEST_NATIVE_RUN_SCRIPT": str(
                ROOT / "scripts" / "test_windows_sandbox_native.ps1"
            ),
            "ALYSIS_TEST_OWNER_PYTHON": sys.executable,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_test_owner_scope_controls_child_birth_and_restores_token(tmp_path: Path) -> None:
    _powershell(
        r"""
$ErrorActionPreference = 'Stop'
. $env:ALYSIS_TEST_OWNER_SCRIPT
$beforeOwner = [AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid()
$beforeIdentity = (& whoami.exe /all /fo csv | Out-String)
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$scope = [AlysisSandboxTests.DefaultOwnerScope]::EnterCurrentUser()
try {
    if ([AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid() -ne $user) {
        throw 'Default owner differs from TokenUser'
    }
    if ((& whoami.exe /all /fo csv | Out-String) -ne $beforeIdentity) {
        throw 'Scope changed identity, groups, privileges or elevation'
    }
    & $env:ALYSIS_TEST_OWNER_PYTHON -c 'import os,pathlib; p=pathlib.Path(os.environ["ALYSIS_TEST_OWNER_ROOT"])/"child"/"nested"; p.mkdir(parents=True); (p/"file").write_text("fixture")'
    if ($LASTEXITCODE -ne 0) { throw 'Child fixture creation failed' }
    foreach ($relative in @('child', 'child/nested', 'child/nested/file')) {
        $acl = Get-Acl -LiteralPath (Join-Path $env:ALYSIS_TEST_OWNER_ROOT $relative)
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $user) {
            throw "Child fixture has the wrong birth owner: $relative"
        }
    }
}
finally { $scope.Dispose() }
if ([AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid() -ne $beforeOwner) {
    throw 'Default owner was not restored'
}
if ((& whoami.exe /all /fo csv | Out-String) -ne $beforeIdentity) {
    throw 'Token context changed after restoration'
}
""",
        tmp_path,
    )


def test_test_runner_restores_owner_environment_and_location_after_failure(tmp_path: Path) -> None:
    _powershell(
        r"""
$ErrorActionPreference = 'Stop'
. $env:ALYSIS_TEST_OWNER_SCRIPT
$beforeOwner = [AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid()
$beforeIdentity = (& whoami.exe /all /fo csv | Out-String)
$beforeLocation = (Get-Location).Path
$env:PYTHONPATH = 'sandbox-harness-restoration-canary'
$global:alysisTestInvocationCount = 0
function global:Invoke-AlysisExpectedTestFailure {
    $global:alysisTestInvocationCount++
    # Pass the harness self-test preflight; fail only inside the active scope.
    $global:LASTEXITCODE = if ($global:alysisTestInvocationCount -eq 1) { 0 } else { 19 }
}
$caught = $false
try { & $env:ALYSIS_TEST_RUN_SCRIPT -Python 'Invoke-AlysisExpectedTestFailure' }
catch {
    if ($_.Exception.Message -notlike '*Sandbox regression tests failed*') { throw }
    $caught = $true
}
if (-not $caught) { throw 'Harness did not surface failed tests' }
if ($global:alysisTestInvocationCount -ne 2) { throw 'Failure did not occur inside owner scope' }
if ([AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid() -ne $beforeOwner) {
    throw 'Failed harness left the default owner changed'
}
if ((& whoami.exe /all /fo csv | Out-String) -ne $beforeIdentity) {
    throw 'Failed harness changed token identity, groups, privileges or elevation'
}
if ((Get-Location).Path -ne $beforeLocation) { throw 'Harness did not restore location' }
if ($env:PYTHONPATH -ne 'sandbox-harness-restoration-canary') {
    throw 'Harness did not restore environment'
}
""",
        tmp_path,
    )


def test_native_test_runner_restores_owner_and_location_after_failure(tmp_path: Path) -> None:
    _powershell(
        r"""
$ErrorActionPreference = 'Stop'
. $env:ALYSIS_TEST_OWNER_SCRIPT
$beforeOwner = [AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid()
$beforeIdentity = (& whoami.exe /all /fo csv | Out-String)
$beforeLocation = (Get-Location).Path
$global:alysisNativeInvocationCount = 0
function global:Invoke-AlysisExpectedNativeFailure {
    $global:alysisNativeInvocationCount++
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ([AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid() -ne $user) {
        throw 'Native test command started without the fixture owner scope'
    }
    $global:LASTEXITCODE = if ($global:alysisNativeInvocationCount -eq 1) { 0 } else { 19 }
}
$caught = $false
try { & $env:ALYSIS_TEST_NATIVE_RUN_SCRIPT -Cargo 'Invoke-AlysisExpectedNativeFailure' }
catch {
    if ($_.Exception.Message -notlike '*Native primitive tests failed*') { throw }
    $caught = $true
}
if (-not $caught -or $global:alysisNativeInvocationCount -ne 2) {
    throw 'Native harness did not stop after the failed second test command'
}
if ([AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid() -ne $beforeOwner) {
    throw 'Failed native harness left the default owner changed'
}
if ((& whoami.exe /all /fo csv | Out-String) -ne $beforeIdentity) {
    throw 'Failed native harness changed token identity, groups, privileges or elevation'
}
if ((Get-Location).Path -ne $beforeLocation) { throw 'Native harness did not restore location' }
""",
        tmp_path,
    )
