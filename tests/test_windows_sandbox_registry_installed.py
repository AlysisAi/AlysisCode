"""Opt-in registry boundary canary; touches only one fresh per-user test key."""

from __future__ import annotations

import csv
import io
import os
import platform
import subprocess
import uuid
from pathlib import Path

import pytest

from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.windows_sandbox import WindowsNativeRunner

pytestmark = pytest.mark.skipif(
    platform.system() != "Windows" or os.environ.get("ALYSIS_RUN_INSTALLED_SANDBOX_TESTS") != "1",
    reason="requires explicitly provisioned Windows sandbox test VM",
)


def test_installed_cannot_open_host_private_registry_key(tmp_path):
    import winreg

    identity = subprocess.run(
        [str(Path(os.environ["SystemRoot"]) / "System32/whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    sid = next(csv.reader(io.StringIO(identity.stdout)))[1]
    assert sid.startswith("S-1-5-")
    relative = rf"Software\AlysisSandboxTest{uuid.uuid4().hex}"
    native_path = sid + "\\" + relative
    value = "non-secret-registry-canary"
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, relative, access=winreg.KEY_ALL_ACCESS
    ) as key:
        winreg.SetValueEx(key, "Canary", 0, winreg.REG_SZ, value)
    try:
        # Use HKEY_USERS and the exact host SID instead of virtualized HKCU.
        with winreg.OpenKey(winreg.HKEY_USERS, native_path, access=winreg.KEY_READ) as key:
            assert winreg.QueryValueEx(key, "Canary")[0] == value
        command = r"""
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class PrivateRegistryProbe {
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode)] public static extern int RegOpenKeyEx(IntPtr root, string path, uint options, uint access, out IntPtr key);
 [DllImport("advapi32.dll")] public static extern int RegCloseKey(IntPtr key);
}' -ErrorAction Stop;
foreach ($access in @(0x20019, 0x2, 0x4, 0x10000)) {
 $key=[IntPtr]::Zero;
 $errorCode=[PrivateRegistryProbe]::RegOpenKeyEx([IntPtr](-2147483645), 'NATIVE_PATH', 0, $access, [ref]$key);
 if ($key -ne [IntPtr]::Zero) { [PrivateRegistryProbe]::RegCloseKey($key) | Out-Null };
 if ($errorCode -ne 5) { throw "Expected registry access denied for $access; got $errorCode" }
};
'PRIVATE_REGISTRY_DENIED'
""".replace("NATIVE_PATH", native_path.replace("'", "''"))
        result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
            root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=60
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "PRIVATE_REGISTRY_DENIED"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative) as key:
            assert winreg.QueryValueEx(key, "Canary")[0] == value
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, relative)
