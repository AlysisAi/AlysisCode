"""Opt-in integration checks; setup is always an explicit operator action.

Run on a disposable Windows test VM after building and provisioning the helper:
ALYSIS_RUN_INSTALLED_SANDBOX_TESTS=1 python -m pytest tests/test_windows_sandbox_installed.py
These checks do not constitute the complete release qualification matrix.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code.config import ConfigError
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.windows_sandbox import WindowsNativeRunner, resolve_helper

pytestmark = pytest.mark.skipif(
    platform.system() != "Windows" or os.environ.get("ALYSIS_RUN_INSTALLED_SANDBOX_TESTS") != "1",
    reason="requires explicitly provisioned Windows sandbox test VM",
)


def quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def assert_command_denied(result) -> None:
    # A setup failure (125), crash or missing executable is not proof that the
    # requested operation was contained. PowerShell must report access denied.
    assert result.returncode == 1, result.stderr
    # Rename-Item reports access denial as an IOException/WriteError, whereas
    # content operations use UnauthorizedAccessException/PermissionDenied.
    assert any(word in result.stderr for word in ("UnauthorizedAccess", "PermissionDenied")) or (
        "RenameItemIOError" in result.stderr
        and "Access to the path" in result.stderr
        and "is denied." in result.stderr
    ), result.stderr


@pytest.mark.parametrize("network", ["off", "on"])
@pytest.mark.parametrize("host_process_policy", [None, "AllSigned", "Restricted"])
def test_installed_script_files_preserve_host_policy_and_nested_inheritance(
    tmp_path, network, host_process_policy, monkeypatch, record_property
):
    from alysis_code.windows_sandbox import _powershell_execution_policy

    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if host_process_policy is not None:
        # Tighten only this test's engine process environment, then restore it.
        # MachinePolicy/UserPolicy may still take precedence on managed hosts.
        monkeypatch.setenv("PSExecutionPolicyPreference", host_process_policy)
    policy = _powershell_execution_policy(shell)
    record_property("effective_host_execution_policy", policy)
    body = (
        "Write-Output ('EFFECTIVE_POLICY=' + (Get-ExecutionPolicy).ToString()); "
        "Write-Output 'ACTUAL_SCRIPT_FILE_RAN'\n"
    )
    local = tmp_path / "local.ps1"
    remote = tmp_path / "remote.ps1"
    local.write_text(body, encoding="utf-8")
    remote.write_text(body, encoding="utf-8")
    Path(str(remote) + ":Zone.Identifier").write_text(
        "[ZoneTransfer]\nZoneId=3\n", encoding="ascii"
    )
    host_env = os.environ.copy()
    host_env["PSModulePath"] = str(shell.parent / "Modules")
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native", network=network))
    for script in (local, remote):
        host = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                policy,
                "-File",
                str(script),
            ],
            cwd=shell.parent,
            env=host_env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
        if policy == "RemoteSigned":
            assert (host.returncode == 0) == (script == local), host.stderr
            if script == remote:
                assert "is not digitally signed" in " ".join(host.stderr.split())
        for nested in (False, True):
            command = (
                f"& {quote(shell)} -NoProfile -NonInteractive -File {quote(script)}; exit $LASTEXITCODE"
                if nested
                else f"& {quote(script)}"
            )
            result = runner.run(root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=45)
            assert result.returncode == host.returncode, result.stderr
            if host.returncode == 0:
                assert result.stdout.splitlines() == [
                    f"EFFECTIVE_POLICY={policy}",
                    "ACTUAL_SCRIPT_FILE_RAN",
                ]
            else:
                assert "ACTUAL_SCRIPT_FILE_RAN" not in result.stdout
                assert "UnauthorizedAccess" in host.stderr and "UnauthorizedAccess" in result.stderr
                if policy == "RemoteSigned":
                    assert "is not digitally signed" in " ".join(result.stderr.split())


@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_command_has_appcontainer_token(tmp_path, network):
    command = r"""
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class TokenProbe {
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr p, uint a, out IntPtr t);
 [DllImport("advapi32.dll", SetLastError=true)] public static extern bool GetTokenInformation(IntPtr t, int c, out int v, int n, out int r);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}' -ErrorAction Stop;
$t=[IntPtr]::Zero; $v=0; $r=0;
if (-not [TokenProbe]::OpenProcessToken([IntPtr](-1), 8, [ref]$t)) { throw 'OpenProcessToken failed' };
try {
 if (-not [TokenProbe]::GetTokenInformation($t, 29, [ref]$v, 4, [ref]$r)) { throw 'Token query failed' };
 if ($v -ne 1) { throw 'Not an AppContainer' }; 'APPCONTAINER'
} finally { [TokenProbe]::CloseHandle($t) | Out-Null }
"""
    result = WindowsNativeRunner(
        ShellSandboxSettings(backend="windows-native", network=network)
    ).run(
        root=tmp_path,
        cwd=tmp_path,
        cmd=command,
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "APPCONTAINER"


@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_write_policy_and_protected_metadata(tmp_path, network):
    root = tmp_path / "workspace"
    root.mkdir()
    git = root / ".git"
    git.mkdir()
    config = git / "config"
    config.write_text("protected", encoding="utf-8")
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native", network=network))
    result = runner.run(
        root=root, cwd=root, cmd="Set-Content output.txt 'allowed' -ErrorAction Stop", timeout_s=60
    )
    assert result.returncode == 0, result.stderr
    assert (root / "output.txt").read_text().strip() == "allowed"
    result = runner.run(
        root=root, cwd=root, cmd="Set-Content .git/config 'changed' -ErrorAction Stop", timeout_s=60
    )
    assert_command_denied(result)
    assert config.read_text() == "protected"
    read_only = WindowsNativeRunner(replace(runner.settings, filesystem_access="read-only"))
    result = read_only.run(
        root=root, cwd=root, cmd="Get-Content output.txt -ErrorAction Stop", timeout_s=60
    )
    assert result.returncode == 0 and result.stdout.strip() == "allowed", result.stderr
    result = read_only.run(
        root=root,
        cwd=root,
        cmd="Write-Output 'READ_ONLY_WRITE_ATTEMPT'; Set-Content forbidden.txt 'changed' -ErrorAction Stop",
        timeout_s=60,
    )
    assert_command_denied(result)
    assert result.stdout.strip() == "READ_ONLY_WRITE_ATTEMPT"
    assert not (root / "forbidden.txt").exists()


@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_cannot_read_private_sibling(tmp_path, network):
    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("private-test-canary")
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native", network=network))
    result = runner.run(
        root=root, cwd=root, cmd=f"Get-Content {quote(secret)} -ErrorAction Stop", timeout_s=60
    )
    assert_command_denied(result)
    assert "private-test-canary" not in result.stdout


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "CONVENTIONS.md"])
@pytest.mark.parametrize("exists", [False, True])
def test_installed_cannot_create_or_replace_instruction_files(tmp_path, name, exists):
    protected = tmp_path / name
    if exists:
        protected.write_text("trusted instructions", encoding="utf-8")
    result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
        root=tmp_path,
        cwd=tmp_path,
        cmd=(
            "Set-Content ordinary.txt 'allowed' -ErrorAction Stop; "
            f"Set-Content {quote(protected)} 'untrusted instructions' -ErrorAction Stop"
        ),
        timeout_s=60,
    )
    assert (tmp_path / "ordinary.txt").read_text().strip() == "allowed", result.stderr
    assert_command_denied(result)
    # Missing protected names become empty placeholders before launch, just
    # like protected directories. The command must never supply their content.
    assert protected.read_text(encoding="utf-8") == ("trusted instructions" if exists else "")


def test_installed_metadata_with_explicit_public_write_is_protected(tmp_path):
    metadata = tmp_path / ".git"
    metadata.mkdir()
    config = metadata / "config"
    config.write_text("protected")
    # Explicit child allows can outrank inherited parent denies on Windows.
    subprocess.run(
        ["icacls.exe", str(config), "/grant", "*S-1-1-0:(F)"],
        capture_output=True,
        text=True,
        check=True,
    )
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    result = runner.run(
        root=tmp_path,
        cwd=tmp_path,
        cmd="Set-Content .git/config 'changed' -ErrorAction Stop",
        timeout_s=60,
    )
    assert config.read_text() == "protected", "Explicit child ACL bypassed metadata protection"
    assert result.returncode == 125, result.stderr
    assert "filesystem enforcement probe failed" in result.stderr
    assert "filesystem protection missing" in result.stderr


@pytest.mark.parametrize("existing", [False, True])
def test_installed_project_skill_roots_preserve_workflow_edits(tmp_path, existing):
    if existing:
        for directory in (tmp_path / ".alysis_skills", tmp_path / ".github/skills"):
            (directory / "example").mkdir(parents=True)
            (directory / "example/SKILL.md").write_text("trusted skill", encoding="utf-8")
        (tmp_path / ".github/workflows").mkdir()
    command = r"""
$ErrorActionPreference = 'Stop';
function Assert-AccessDenied([scriptblock]$Operation) {
 try { & $Operation; throw 'Operation unexpectedly succeeded' }
 catch {
  if ($_.Exception -is [System.UnauthorizedAccessException] -or
      $_.FullyQualifiedErrorId -match 'UnauthorizedAccess|PermissionDenied' -or
      ($_.FullyQualifiedErrorId -match 'RenameItemIOError' -and
       $_.Exception.Message -match 'Access to the path.*is denied')) { 'DENIED' }
  else { throw }
 }
};
New-Item -ItemType Directory -Path .github/workflows -Force | Out-Null;
Set-Content .github/workflows/test.yml 'allowed';
Assert-AccessDenied { New-Item -ItemType Directory -Path .alysis_skills/injected };
Assert-AccessDenied { New-Item -ItemType Directory -Path .github/skills/injected };
Assert-AccessDenied { Rename-Item .github moved-github };
"""
    if existing:
        command += r"""
if ((Get-Content .alysis_skills/example/SKILL.md -Raw) -ne 'trusted skill') { throw 'Skill read failed' };
if ((Get-Content .github/skills/example/SKILL.md -Raw) -ne 'trusted skill') { throw 'Skill read failed' };
Assert-AccessDenied { Set-Content .alysis_skills/example/SKILL.md 'changed' };
Assert-AccessDenied { Set-Content .github/skills/example/SKILL.md 'changed' };
"""
    result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
        root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("DENIED") == (5 if existing else 3), result.stdout
    assert (tmp_path / ".github/workflows/test.yml").read_text().strip() == "allowed"
    assert not (tmp_path / "moved-github").exists()
    assert not (tmp_path / ".github/skills/injected").exists()
    assert not (tmp_path / ".alysis_skills/injected").exists()
    if existing:
        for directory in (tmp_path / ".alysis_skills", tmp_path / ".github/skills"):
            assert (directory / "example/SKILL.md").read_text() == "trusted skill"


@pytest.mark.parametrize("existing", [False, True])
def test_installed_project_skill_parent_cannot_be_deleted(tmp_path, existing):
    if existing:
        (tmp_path / ".github/skills").mkdir(parents=True)
    result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
        root=tmp_path,
        cwd=tmp_path,
        cmd="Remove-Item .github -Recurse -Force -ErrorAction Stop",
        timeout_s=60,
    )
    assert_command_denied(result)
    assert (tmp_path / ".github/skills").is_dir()


def test_installed_junction_cannot_expose_private_sibling(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("private-test-canary")
    link = root / "linked"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run(
        [
            str(shell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(outside)} -ErrorAction Stop | Out-Null",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        with pytest.raises(ConfigError, match="reparse points"):
            WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
                root=root,
                cwd=root,
                cmd="Get-Content linked/canary.txt -ErrorAction Stop",
                timeout_s=60,
            )
    finally:
        # Remove just the junction, never recurse into its target.
        link.rmdir()
    assert canary.read_text() == "private-test-canary"


def test_installed_hardlink_cannot_change_private_sibling(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    canary = tmp_path / "private.txt"
    canary.write_text("private-test-canary")
    (root / "alias.txt").hardlink_to(canary)
    with pytest.raises(ConfigError, match="hardlinked files"):
        WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
            root=root,
            cwd=root,
            cmd="Set-Content alias.txt 'changed' -ErrorAction Stop",
            timeout_s=60,
        )
    assert canary.read_text() == "private-test-canary", (
        "Workspace grant exposed an outside hardlink"
    )


def test_installed_new_hardlink_cannot_make_metadata_writable(tmp_path):
    metadata = tmp_path / ".git"
    metadata.mkdir()
    protected = metadata / "config"
    protected.write_text("protected", encoding="utf-8")
    alias = tmp_path / "metadata-alias.txt"
    command = r"""
$ErrorActionPreference='Stop';
Set-Content ordinary.txt 'ordinary';
New-Item -ItemType HardLink -Path ordinary-alias.txt -Target ordinary.txt | Out-Null;
Set-Content ordinary-alias.txt 'allowed';
try {
 New-Item -ItemType HardLink -Path metadata-alias.txt -Target .git/config | Out-Null;
 Set-Content metadata-alias.txt 'changed';
 throw 'Protected metadata was writable through a new hardlink'
} catch {
 if ($_.Exception -isnot [UnauthorizedAccessException] -and
     $_.CategoryInfo.Category -ne 'PermissionDenied' -and
     -not ($_.Exception -is [ComponentModel.Win32Exception] -and
           $_.Exception.NativeErrorCode -eq 5)) { throw };
 'BLOCKED'
}
"""
    try:
        result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
            root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=60
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "BLOCKED"
        assert protected.read_text() == "protected"
        assert (tmp_path / "ordinary.txt").read_text().strip() == "allowed"
    finally:
        # Remove only the alias name; never recurse or follow its target.
        alias.unlink(missing_ok=True)


@pytest.mark.parametrize("location", ["workspace", "profile"])
def test_installed_junction_attempt_cannot_expose_private_target(tmp_path, location):
    root = tmp_path / "workspace"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    canary = private / "canary.txt"
    canary.write_text("private-test-canary")
    junction = root / "junction"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # A successful host control establishes that this volume and command
    # support junctions. Remove only its directory entry before the preflight.
    control_link = root / "control-junction"
    try:
        control = subprocess.run(
            [
                str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(control_link),
                str(private),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert control.returncode == 0, control.stderr
        assert control_link.is_junction()
        assert (control_link / "canary.txt").read_text() == "private-test-canary"
    finally:
        if control_link.is_junction():
            control_link.rmdir()

    def sddl():
        control = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Acl -LiteralPath {quote(private)}).Sddl; "
                f"(Get-Acl -LiteralPath {quote(canary)}).Sddl",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            # Do not import PowerShell 7-only modules into Windows PowerShell.
            env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"},
        )
        assert control.returncode == 0, control.stderr
        return control.stdout

    before = sddl()
    destination = (
        quote(junction) if location == "workspace" else "(Join-Path $env:USERPROFILE 'junction')"
    )
    command = (
        "$ErrorActionPreference='Stop'; "
        "Set-Content started.txt 'started'; "
        f"$link={destination}; "
        "$ErrorActionPreference='Continue'; "
        f"$message=(& $env:ComSpec /d /c mklink /J $link {quote(private)} 2>&1 | Out-String); "
        "$status=$LASTEXITCODE; $ErrorActionPreference='Stop'; "
        "if ($status -ne 0) { "
        "if ($message -notmatch 'Access is denied') { throw $message }; "
        "'CREATION_BLOCKED'; exit 0 }; "
        "try { Get-Content -LiteralPath (Join-Path $link 'canary.txt') -ErrorAction Stop; "
        "throw 'Private target was readable through a new junction' } "
        "catch { if ($_.Exception -isnot [UnauthorizedAccessException] -and "
        "$_.CategoryInfo.Category -ne 'PermissionDenied') { throw }; 'READ_BLOCKED' }"
    )
    try:
        result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
            root=root, cwd=root, cmd=command, timeout_s=60
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() in {"CREATION_BLOCKED", "READ_BLOCKED"}
        assert (root / "started.txt").read_text().strip() == "started"
        assert canary.read_text() == "private-test-canary", "Profile cleanup followed a junction"
        assert sddl() == before, "ACL cleanup changed the private junction target"
    finally:
        if junction.is_junction():
            junction.rmdir()


def test_installed_new_private_hardlink_cannot_expose_or_repermission_target(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    canary = private / "canary.txt"
    canary.write_text("private-hardlink-canary", encoding="utf-8")
    alias = root / "private-alias.txt"
    # Distinct inheritance makes unintended cleanup propagation observable:
    # this read grant belongs to the workspace, never to the private sibling.
    subprocess.run(
        ["icacls.exe", str(root), "/grant", "*S-1-1-0:(OI)(CI)(RX)"],
        capture_output=True,
        text=True,
        check=True,
    )
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"

    def private_sddl():
        result = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Acl -LiteralPath {quote(private)}).Sddl; "
                f"(Get-Acl -LiteralPath {quote(canary)}).Sddl",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"},
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    before = private_sddl()
    command = r"""
$ErrorActionPreference = 'Stop';
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class PrivateHardlinkProbe {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool CreateHardLinkW(string n, string e, IntPtr sa);
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr CreateFileW(string p, uint a, uint s, IntPtr sa, uint c, uint f, IntPtr t);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}' -ErrorAction Stop;
"""
    command += f"$alias={quote(alias)}; $private={quote(canary)}; "
    command += r"""
if (-not [PrivateHardlinkProbe]::CreateHardLinkW($alias, $private, [IntPtr]::Zero)) {
 $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error();
 if ($errorCode -ne 5) { throw "Unexpected link error: $errorCode" };
 'CREATION_BLOCKED'; exit 0
};
'CREATED';
function Assert-Blocked([uint32]$access, [string]$label) {
 $handle = [PrivateHardlinkProbe]::CreateFileW($alias, $access, 7, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero);
 $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error();
 if ($handle -ne [IntPtr](-1)) {
  [PrivateHardlinkProbe]::CloseHandle($handle) | Out-Null;
  throw "$label unexpectedly allowed"
 };
 if ($errorCode -ne 5) { throw "Unexpected $label error: $errorCode" };
 $label + '_BLOCKED'
};
Assert-Blocked 1 'READ';
Assert-Blocked 2 'WRITE';
"""
    try:
        result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
            root=root, cwd=root, cmd=command, timeout_s=60
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() in (
            ["CREATION_BLOCKED"],
            ["CREATED", "READ_BLOCKED", "WRITE_BLOCKED"],
        ), result.stdout
        assert canary.read_text(encoding="utf-8") == "private-hardlink-canary"
        assert private_sddl() == before, "ACL cleanup changed the private hardlink target"
    finally:
        if alias.exists():
            alias.unlink()


@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_command_cannot_open_host_process_memory(tmp_path, network):
    command = (
        "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
        "public class HostProcessProbe { "
        '[DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint rights, bool inherit, uint pid); '
        '[DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle); }\'; '
        f"$h=[HostProcessProbe]::OpenProcess(0x10, $false, {os.getpid()}); "
        "$e=[Runtime.InteropServices.Marshal]::GetLastWin32Error(); "
        "if ($h -ne [IntPtr]::Zero) { [HostProcessProbe]::CloseHandle($h) | Out-Null; exit 9 }; "
        "if ($e -ne 5) { throw \"Unexpected OpenProcess error: $e\" }; Write-Output 'BLOCKED'"
    )
    result = WindowsNativeRunner(
        ShellSandboxSettings(backend="windows-native", network=network)
    ).run(root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BLOCKED"


@pytest.mark.parametrize(
    "family,address", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
)
@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_command_cannot_send_udp_to_host(tmp_path, family, address, network):
    with socket.socket(family, socket.SOCK_DGRAM) as listener:
        listener.bind((address, 0))
        listener.settimeout(0.2)
        port = listener.getsockname()[1]
        with socket.socket(family, socket.SOCK_DGRAM) as control:
            control.sendto(b"host-control", (address, port))
        assert listener.recvfrom(32)[0] == b"host-control"
        address_family = "InterNetworkV6" if family == socket.AF_INET6 else "InterNetwork"
        command = (
            f"$s=[Net.Sockets.UdpClient]::new([Net.Sockets.AddressFamily]::{address_family}); "
            f"$ep=[Net.IPEndPoint]::new([Net.IPAddress]::Parse('{address}'), {port}); "
            "try { $s.Send([byte[]](65,66,67), 3, $ep) | Out-Null; exit 9 } "
            "catch { $e=$_.Exception; while ($e.InnerException) { $e=$e.InnerException }; "
            "if ($e.NativeErrorCode -eq 10013) { Write-Output 'BLOCKED'; exit 0 }; throw }"
        )
        result = WindowsNativeRunner(
            ShellSandboxSettings(backend="windows-native", network=network)
        ).run(root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=60)
        # UDP can acknowledge queuing a datagram even when WFP drops it. Check
        # actual delivery to a listener verified by host controls on both sides.
        assert result.returncode in (0, 9), result.stderr
        with pytest.raises(socket.timeout):
            listener.recvfrom(32)
        with socket.socket(family, socket.SOCK_DGRAM) as control:
            control.sendto(b"host-control-after", (address, port))
        assert listener.recvfrom(32)[0] == b"host-control-after"


def test_installed_command_cannot_use_dns_service_for_egress(tmp_path):
    """A real DNS response distinguishes service egress from timeout/cache errors."""
    host_name = "host-" + uuid.uuid4().hex + ".invalid"
    sandbox_name = "sandbox-" + uuid.uuid4().hex + ".invalid"
    received: list[bytes] = []
    stopped = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 53))
        listener.settimeout(0.1)

        def serve():
            while not stopped.is_set():
                try:
                    packet, sender = listener.recvfrom(4096)
                except TimeoutError:
                    continue
                if len(packet) < 12:
                    continue
                received.append(packet)
                # Echo the question with NXDOMAIN. No forwarding or global DNS
                # settings changes, and no request ever leaves localhost.
                listener.sendto(
                    packet[:2] + b"\x81\x83" + packet[4:6] + b"\0" * 6 + packet[12:], sender
                )

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        try:

            def command(name):
                return (
                    f"Resolve-DnsName '{name}' -Server 127.0.0.1 -Type A "
                    "-DnsOnly -NoHostsFile -QuickTimeout -ErrorAction Stop"
                )

            shell = (
                Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            subprocess.run(
                [str(shell), "-NoProfile", "-NonInteractive", "-Command", command(host_name)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert any(host_name.split(".")[0].encode() in p for p in received), (
                "DNS host control did not reach listener"
            )
            result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
                root=tmp_path,
                cwd=tmp_path,
                cmd="Set-Content dns-started.txt 'started' -ErrorAction Stop; "
                + command(sandbox_name),
                timeout_s=30,
            )
            assert result.returncode != 125, result.stderr
            assert (tmp_path / "dns-started.txt").read_text().strip() == "started", result.stderr
            assert not any(sandbox_name.split(".")[0].encode() in p for p in received), (
                "Windows DNS service transmitted the sandbox canary despite network=off"
            )
        finally:
            stopped.set()
            server.join(timeout=2)


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item .git/config -ErrorAction Stop",
        "Rename-Item .git moved-metadata -ErrorAction Stop",
    ],
)
def test_installed_cannot_delete_or_rename_metadata(tmp_path, command):
    metadata = tmp_path / ".git"
    metadata.mkdir()
    config = metadata / "config"
    config.write_text("protected", encoding="utf-8")
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    result = runner.run(root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=60)
    assert_command_denied(result)
    assert config.read_text() == "protected"
    assert not (tmp_path / "moved-metadata").exists()


def test_installed_cannot_write_metadata_alternate_stream(tmp_path):
    metadata = tmp_path / ".git"
    metadata.mkdir()
    config = metadata / "config"
    config.write_text("protected")
    protected_stream = Path(str(config) + ":bypass")
    protected_stream.write_text("protected-stream")
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary")
    ordinary_stream = Path(str(ordinary) + ":bypass")
    ordinary_stream.write_text("ordinary-stream")
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    result = runner.run(
        root=tmp_path,
        cwd=tmp_path,
        cmd=(
            "Set-Content ordinary.txt -Stream bypass 'allowed' -ErrorAction Stop; "
            "Set-Content .git/config -Stream bypass 'changed' -ErrorAction Stop"
        ),
        timeout_s=60,
    )
    # The provider obscures a denied ADS as FileNotFoundException. Both streams
    # exist, and the identical operation on the ordinary file must succeed, so
    # unsupported streams or a wrong working directory cannot satisfy the test.
    assert ordinary_stream.read_text().strip() == "allowed", result.stderr
    assert result.returncode == 1, result.stderr
    assert "Could not open the alternate data stream 'bypass'" in result.stderr
    assert "System.IO.FileNotFoundException" in result.stderr
    assert protected_stream.read_text() == "protected-stream"
    assert config.read_text() == "protected"


def test_installed_child_does_not_receive_host_secrets(tmp_path, monkeypatch):
    for key in ("ALYSIS_SANDBOX_TEST_SECRET", "OPENAI_API_KEY", "PYTHONSTARTUP", "NODE_OPTIONS"):
        monkeypatch.setenv(key, "alysis-private-test-canary")
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    result = runner.run(
        root=tmp_path,
        cwd=tmp_path,
        cmd="Get-ChildItem Env: | Where-Object { $_.Value -eq 'alysis-private-test-canary' } | Select-Object -ExpandProperty Name",
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip(), "Host secret/startup environment reached the command"


@pytest.mark.parametrize(
    "family,address", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
)
@pytest.mark.parametrize("network", ["off", "on"])
def test_installed_command_cannot_connect_to_host(tmp_path, family, address, network):
    with socket.socket(family) as listener:
        listener.bind((address, 0))
        listener.listen()
        port = listener.getsockname()[1]
        address_family = "InterNetworkV6" if family == socket.AF_INET6 else "InterNetwork"
        listener.settimeout(0.2)
        with socket.socket(family) as control:
            control.connect((address, port))
            accepted, _ = listener.accept()
            accepted.close()
        cmd = (
            f"$s = [System.Net.Sockets.TcpClient]::new([System.Net.Sockets.AddressFamily]::{address_family}); "
            f"try {{ $a=$s.BeginConnect([System.Net.IPAddress]::Parse('{address}'), {port}, $null, $null); "
            "if (-not $a.AsyncWaitHandle.WaitOne(2000)) { 'BLOCKED'; exit 0 }; "
            "$s.EndConnect($a); exit 9 } "
            "catch { $e=$_.Exception; while ($e.InnerException) { $e=$e.InnerException }; "
            "if ($e.NativeErrorCode -eq 10013) { Write-Output 'BLOCKED'; exit 0 }; throw } "
            "finally { $s.Dispose() }"
        )
        result = WindowsNativeRunner(
            ShellSandboxSettings(backend="windows-native", network=network)
        ).run(root=tmp_path, cwd=tmp_path, cmd=cmd, timeout_s=60)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "BLOCKED"
        # AppContainer loopback isolation can drop packets rather than return
        # WSAEACCES. A timeout counts only with a live listener, no accepted
        # sandbox connection, and successful host controls before and after.
        with pytest.raises(socket.timeout):
            listener.accept()
        with socket.socket(family) as control:
            control.connect((address, port))
            accepted, _ = listener.accept()
            accepted.close()


_TREE_COMMAND = (
    "$p=Start-Process -FilePath $PSHOME\\powershell.exe "
    "-ArgumentList '-NoLogo -NoProfile -NonInteractive -Command Start-Sleep -Seconds 120' "
    "-PassThru -WindowStyle Hidden; "
    "Set-Content profile-path.txt $env:USERPROFILE; "
    "Set-Content child.pid $p.Id; Set-Content parent.pid $PID; Start-Sleep -Seconds 120"
)


def wait_for_tree(root, process):
    deadline = time.monotonic() + 30
    while not (root / "parent.pid").exists():
        if process.poll() is not None:
            pytest.fail(f"broker exited before launching test tree: {process.stderr.read()!r}")
        if time.monotonic() > deadline:
            pytest.fail("sandbox command did not create its test process tree")
        time.sleep(0.1)


def assert_tree_stopped(root, timeout_ms=5000):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for name in ("parent.pid", "child.pid"):
        pid = int((root / name).read_text().strip())
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            assert ctypes.get_last_error() == 87, "Cannot establish child termination"
        else:
            try:
                assert kernel.WaitForSingleObject(handle, timeout_ms) == 0, (
                    f"sandbox descendant {pid} survived"
                )
            finally:
                kernel.CloseHandle(handle)


def recover(root=None):
    result = subprocess.run(
        [str(resolve_helper()), "recover"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    if root is not None and (root / "profile-path.txt").exists():
        assert not Path((root / "profile-path.txt").read_text().strip()).exists(), (
            "Recovered profile survived"
        )


def test_installed_leader_exit_waits_for_descendants_before_cleanup(tmp_path):
    command = _TREE_COMMAND.removesuffix("Start-Sleep -Seconds 120") + "exit 0"
    result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
        root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=60
    )
    assert result.returncode == 0, result.stderr
    # No grace period: completion means descendants have already stopped.
    assert_tree_stopped(tmp_path, timeout_ms=0)
    assert not Path((tmp_path / "profile-path.txt").read_text().strip()).exists()
    assert not (Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox/execution-pending.json").exists()


def test_installed_background_stop_busy_lease_and_recovery(tmp_path):
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    started = runner.start(root=tmp_path, cwd=tmp_path, cmd=_TREE_COMMAND)
    try:
        try:
            wait_for_tree(tmp_path, started.popen)
            busy = runner.run(
                root=tmp_path, cwd=tmp_path, cmd="Write-Output unexpected", timeout_s=10
            )
            assert busy.returncode == 125 and "busy" in busy.stderr
            assert "unexpected" not in busy.stdout
            for operation in (["install"], ["uninstall"], ["wfp", "uninstall"]):
                maintenance = subprocess.run(
                    [str(resolve_helper()), *operation],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert maintenance.returncode != 0 and "busy" in maintenance.stderr
        finally:
            started.cleanup()
        assert_tree_stopped(tmp_path)
        pending = runner.run(
            root=tmp_path, cwd=tmp_path, cmd="Write-Output unexpected", timeout_s=10
        )
        assert pending.returncode == 125 and "unfinished" in pending.stderr
        maintenance = subprocess.run(
            [str(resolve_helper()), "uninstall"], capture_output=True, text=True, timeout=10
        )
        assert maintenance.returncode != 0 and "recover-native" in maintenance.stderr
    finally:
        recover(tmp_path)
    result = runner.run(root=tmp_path, cwd=tmp_path, cmd="Write-Output recovered", timeout_s=60)
    assert result.returncode == 0 and result.stdout.strip() == "recovered", result.stderr


def test_installed_immediate_recovery_waits_for_descendants(tmp_path):
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    started = runner.start(root=tmp_path, cwd=tmp_path, cmd=_TREE_COMMAND)
    try:
        wait_for_tree(tmp_path, started.popen)
        journal = Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox/execution-pending.json"
        marker = json.loads(journal.read_text())
        assert marker["version"] == 2 and marker["created"] > 0
        started.cleanup()
        # Do not wait on either descendant before invoking recovery: recovery
        # itself must prove the job is empty before removing write protection.
        recover(tmp_path)
        assert_tree_stopped(tmp_path, timeout_ms=0)
        assert not journal.exists()
    finally:
        started.cleanup()
        recover(tmp_path)


def test_installed_recovery_keeps_denies_while_job_is_alive(tmp_path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    started = runner.start(root=tmp_path, cwd=tmp_path, cmd=_TREE_COMMAND)
    handle = None
    try:
        wait_for_tree(tmp_path, started.popen)
        journal = Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox/execution-pending.json"
        marker = json.loads(journal.read_text())
        suffix = marker["container"]["name"].removeprefix("Alysis.Execution.")
        handle = kernel.OpenJobObjectW(4, False, "Global\\AlysisSandboxJob." + suffix)
        assert handle, f"Cannot hold test job: {ctypes.get_last_error()}"
        # This trusted host-only query handle intentionally delays last-handle
        # closure, so broker exit does not yet terminate the descendants.
        started.cleanup()
        result = subprocess.run(
            [str(resolve_helper()), "recover"], capture_output=True, text=True, timeout=40
        )
        assert result.returncode == 125 and "processes; recovery remains pending" in result.stderr
        assert json.loads(journal.read_text()) == marker, "Recovery discarded the live job journal"
        assert Path((tmp_path / "profile-path.txt").read_text().strip()).exists()
    finally:
        if handle:
            kernel.CloseHandle(handle)
        started.cleanup()
        recover(tmp_path)
    assert_tree_stopped(tmp_path, timeout_ms=0)


def test_installed_child_cannot_open_containment_job(tmp_path):
    command = (
        _TREE_COMMAND.removesuffix("Start-Sleep -Seconds 120")
        + r"""
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public class JobProbe {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 public static extern IntPtr OpenJobObject(uint rights, bool inherit, string name);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}' -ErrorAction Stop;
$deadline=[DateTime]::UtcNow.AddSeconds(30);
while (-not (Test-Path -LiteralPath job-name.txt)) {
 if ([DateTime]::UtcNow -gt $deadline) { throw 'Missing host job name' }; Start-Sleep -Milliseconds 50
};
$name=(Get-Content -LiteralPath job-name.txt -Raw).Trim();
foreach ($rights in @(1,2,4,8,0x1f003f)) {
 $h=[JobProbe]::OpenJobObject($rights, $false, $name);
 $e=[Runtime.InteropServices.Marshal]::GetLastWin32Error();
 if ($h -ne [IntPtr]::Zero) { [JobProbe]::CloseHandle($h) | Out-Null; throw "Opened host job: $rights" };
 if ($e -ne 5 -and $e -ne 2) { throw "Unexpected job open error: $e" }
}; 'JOB_BLOCKED'
"""
    )
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    started = runner.start(root=tmp_path, cwd=tmp_path, cmd=command)
    try:
        wait_for_tree(tmp_path, started.popen)
        journal = Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox/execution-pending.json"
        marker = json.loads(journal.read_text())
        suffix = marker["container"]["name"].removeprefix("Alysis.Execution.")
        (tmp_path / "job-name.txt").write_text("Global\\AlysisSandboxJob." + suffix)
        # start() deliberately closes stdin after the execution request.
        started.popen.stdin = None
        stdout, stderr = started.popen.communicate(timeout=60)
        assert started.popen.returncode == 0, stderr
        assert stdout.decode().strip() == "JOB_BLOCKED"
    finally:
        started.cleanup()
        recover(tmp_path)


def test_installed_timeout_kills_descendants(tmp_path):
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            runner.run(root=tmp_path, cwd=tmp_path, cmd=_TREE_COMMAND, timeout_s=10)
        assert_tree_stopped(tmp_path)
    finally:
        recover(tmp_path)


def test_installed_engine_exit_kills_descendants(tmp_path):
    # An independent engine process exits without calling background cleanup.
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys,time; from pathlib import Path; "
        f"sys.path.insert(0, {source!r}); "
        "from alysis_code.windows_sandbox import WindowsNativeRunner; "
        "from alysis_code.sandbox_settings import ShellSandboxSettings; "
        f"root=Path({str(tmp_path)!r}); "
        "runner=WindowsNativeRunner(ShellSandboxSettings(backend='windows-native')); "
        f"started=runner.start(root=root,cwd=root,cmd={_TREE_COMMAND!r}); "
        "deadline=time.monotonic()+30\n"
        "while not (root/'parent.pid').exists():\n"
        " assert started.popen.poll() is None and time.monotonic()<deadline\n"
        " time.sleep(0.1)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=40
        )
        assert result.returncode == 0, result.stderr
        assert_tree_stopped(tmp_path)
    finally:
        recover(tmp_path)


def test_installed_profile_is_fresh_and_removed_after_each_command(tmp_path):
    runner = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native"))
    profiles = []
    for _index in range(2):
        result = runner.run(
            root=tmp_path,
            cwd=tmp_path,
            cmd="Set-Content -LiteralPath (Join-Path $env:TEMP canary.txt) 'private' -ErrorAction Stop; $env:USERPROFILE",
            timeout_s=60,
        )
        assert result.returncode == 0, result.stderr
        profile = Path(result.stdout.strip())
        assert "alysis.execution." in str(profile).lower(), result.stdout
        assert not profile.exists(), "Execution profile survived normal cleanup"
        profiles.append(profile)
    assert profiles[0] != profiles[1], "Execution identity/profile was reused"


def test_installed_quoted_subdirectory_and_native_working_directory(tmp_path):
    root = tmp_path / "project's workspace"
    cwd = root / "child's folder"
    cwd.mkdir(parents=True)
    result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native")).run(
        root=root,
        cwd=cwd,
        cmd="Set-Content local.txt 'correct' -ErrorAction Stop; & $env:ComSpec /d /c cd; exit $LASTEXITCODE",
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr
    assert (cwd / "local.txt").read_text().strip() == "correct"
    assert Path(result.stdout.strip()) == cwd


def test_installed_native_node_and_git_compatibility(tmp_path):
    import shutil

    node = shutil.which("node")
    git = shutil.which("git")
    assert node and git, "Qualification requires installed Node.js and Git"
    # The system-wide Node installation on this host has no AppContainer read
    # ACE. Use a disposable, user-owned portable tool directory with an explicit
    # read root; never broaden Program Files permissions for the test.
    tool_root = tmp_path / "readonly-tools"
    tool_root.mkdir()
    tool = tool_root / "node.exe"
    shutil.copyfile(node, tool)
    dependency = tmp_path / "node_modules" / "local-canary"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_text("module.exports='ok'", encoding="utf-8")
    (tmp_path / "main.js").write_text(
        "require('fs').writeFileSync('node.txt',require('local-canary'))", encoding="utf-8"
    )
    result = WindowsNativeRunner(
        ShellSandboxSettings(
            backend="windows-native",
            windows_read_roots=(str(tool_root),),
        )
    ).run(
        root=tmp_path,
        cwd=tmp_path,
        cmd=(
            f"$ErrorActionPreference='Stop'; & {quote(tool)} main.js; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
            f"& {quote(Path(git))} --version; exit $LASTEXITCODE"
        ),
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "node.txt").read_text() == "ok"
    assert result.stdout.startswith("git version ")


@pytest.mark.parametrize("network", ["off", "on"])
@pytest.mark.parametrize("operation", ["spawn", "spawnSync", "fork", "test"])
def test_installed_node_subprocess_compatibility(tmp_path, network, operation, record_property):
    import hashlib
    import shutil

    # Node/libuv uses the ordinary namespace for child stdio and IPC, retrying
    # ERROR_ACCESS_DENIED indefinitely as though it were a name collision.
    # The finite API control identifies that exact compatibility limitation.
    # Actual Node execution below is bounded and recovers its intact fixture.
    # A fixed libuv uses LOCAL; opening the ordinary host namespace is never
    # the desired fix. Never rewrite the user's command to disable isolation.
    command = r"""
$ErrorActionPreference='Stop';
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public static class FinitePipeProbe {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 public static extern IntPtr CreateNamedPipeW(string name, uint mode, uint pipeMode, uint max,
   uint output, uint input, uint timeout, IntPtr security);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr handle);
 public static int Probe(string name) {
   IntPtr handle=CreateNamedPipeW(name, 3, 0, 1, 1024, 1024, 0, IntPtr.Zero);
   if (handle == new IntPtr(-1)) return Marshal.GetLastWin32Error();
   if (!CloseHandle(handle)) throw new System.ComponentModel.Win32Exception();
   return 0;
 }
}' -ErrorAction Stop;
$id=[Guid]::NewGuid().ToString('N');
@{ordinary=[FinitePipeProbe]::Probe('\\?\pipe\uv\alysis_probe_'+$id);
  local=[FinitePipeProbe]::Probe('\\.\pipe\LOCAL\alysis_probe_'+$id)} | ConvertTo-Json -Compress
"""
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    control = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert control.returncode == 0, control.stderr
    assert json.loads(control.stdout) == {"ordinary": 0, "local": 0}, control.stdout
    result = WindowsNativeRunner(
        ShellSandboxSettings(
            backend="windows-native", network=network, filesystem_access="read-only"
        )
    ).run(root=tmp_path, cwd=tmp_path, cmd=command, timeout_s=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {"ordinary": 5, "local": 0}, observed

    node = shutil.which("node")
    assert node, "Node subprocess qualification requires Node.js"
    tool_root = tmp_path / "readonly-tools"
    tool_root.mkdir()
    tool = tool_root / "node.exe"
    shutil.copyfile(node, tool)
    digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    assert digest == hashlib.sha256(Path(node).read_bytes()).hexdigest()
    record_property("node_sha256", digest)
    script = tmp_path / "probe.cjs"
    marker = tmp_path / "node-test-executed.json"
    preamble = (
        "const assert=require('node:assert/strict');\n"
        "const {spawn,spawnSync,fork}=require('node:child_process');\n"
        "function finish(){\n"
        "require('node:fs').writeFileSync('node-test-executed.json',"
        "JSON.stringify({uv:process.versions.uv,node:process.versions.node}));\n"
        "console.log('NODE_PROBE_OK');}\n"
    )
    body = {
        "test": (
            "require('node:test').test('sandbox test-runner canary',()=>{\n"
            "assert.equal(2+2,4); finish(); });\n"
        ),
        "spawn": (
            "const child=spawn(process.execPath,['-e',\"process.stdout.write('CHILD_OK')\"],"
            "{stdio:['ignore','pipe','pipe']});\n"
            "let stdout='',stderr=''; child.stdout.on('data',b=>stdout+=b);\n"
            "child.stderr.on('data',b=>stderr+=b);\n"
            "child.on('error',error=>{throw error});\n"
            "child.on('close',code=>{assert.equal(code,0); assert.equal(stdout,'CHILD_OK');"
            "assert.equal(stderr,''); finish();});\n"
        ),
        "spawnSync": (
            "const child=spawnSync(process.execPath,['-e',\"process.stdout.write('CHILD_OK')\"],"
            "{encoding:'utf8',timeout:5000});\n"
            "assert.ifError(child.error); assert.equal(child.status,0);"
            "assert.equal(child.stdout,'CHILD_OK'); assert.equal(child.stderr,''); finish();\n"
        ),
        "fork": (
            "const child=fork('child.cjs',[],{stdio:['ignore','ignore','ignore','ipc']});\n"
            "let received=false; child.on('message',message=>{"
            "assert.equal(message,'CHILD_OK'); received=true;});\n"
            "child.on('error',error=>{throw error});\n"
            "child.on('close',code=>{assert.equal(code,0);assert.equal(received,true);finish();});\n"
        ),
    }[operation]
    script.write_text(preamble + body, encoding="utf-8")
    (tmp_path / "child.cjs").write_text(
        "process.send('CHILD_OK',error=>{if(error)throw error;process.disconnect();});\n",
        encoding="utf-8",
    )
    arguments = (
        ["--test", "--test-reporter=tap", script.name] if operation == "test" else [script.name]
    )
    host = subprocess.run(
        [str(tool), *arguments],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        env={k: v for k, v in os.environ.items() if not k.upper().startswith("NODE_")},
    )
    assert host.returncode == 0 and "NODE_PROBE_OK" in host.stdout, host.stderr or host.stdout
    version = json.loads(marker.read_text())
    record_property("node_version", version["node"])
    record_property("libuv_version", version["uv"])
    uv = tuple(int(part) for part in version["uv"].split("."))
    marker.unlink()
    try:
        result = WindowsNativeRunner(
            ShellSandboxSettings(
                backend="windows-native", network=network, windows_read_roots=(str(tool_root),)
            )
        ).run(
            root=tmp_path,
            cwd=tmp_path,
            cmd=(
                "$ErrorActionPreference='Stop'; Set-Content node-invoked.txt 'started'; "
                f"& {quote(tool)} {' '.join(arguments)}; exit $LASTEXITCODE"
            ),
            timeout_s=15,
        )
    except subprocess.TimeoutExpired:
        # Preserve the exact workspace until normal identity-bound recovery
        # succeeds. A recovery failure must remain a failure, never an xfail.
        recover(tmp_path)
        assert not (
            Path(os.environ["LOCALAPPDATA"]) / "alysis-sandbox/execution-pending.json"
        ).exists()
        invoked = tmp_path / "node-invoked.txt"
        if (
            uv < (1, 53, 0)
            and invoked.exists()
            and invoked.read_text().strip() == "started"
            and not marker.exists()
        ):
            pytest.xfail(
                "Known release gate: pre-1.53 libuv loops on AppContainer ordinary-pipe denial; "
                f"Node {operation} timed out before successful completion; recovery succeeded"
            )
        pytest.fail(
            f"Node {operation} timed out outside the confirmed pre-1.53 pipe incompatibility"
        )
    assert result.returncode == 0 and "NODE_PROBE_OK" in result.stdout, (
        result.stderr or result.stdout
    )
    assert json.loads(marker.read_text()) == version


@pytest.mark.parametrize("protocol", [1, 2])
def test_installed_rejects_older_protocol(tmp_path, protocol):
    payload = {
        "protocol": protocol,
        "cwd": str(tmp_path),
        "argv": ["must-not-run.exe"],
        "read": [],
        "write": [],
        "deny_read": [],
        "deny_write": [],
        "env": [],
        "network": "off",
        "parent_pid": 0,
        "parent_created": 0,
    }
    result = subprocess.run(
        [str(resolve_helper()), "run-request"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 125
    assert "unsupported protocol" in result.stderr


@pytest.mark.skipif(
    os.environ.get("ALYSIS_RUN_SANDBOX_NETWORK_TESTS") != "1",
    reason="requires explicit public HTTPS qualification; sends no workspace data",
)
def test_installed_https_access_is_explicit_and_does_not_persist(tmp_path, record_property):
    import shutil

    node = shutil.which("node")
    assert node, "Public HTTPS qualification requires Node.js"
    tool_root = tmp_path / "readonly-tools"
    tool_root.mkdir()
    tool = tool_root / "node.exe"
    shutil.copyfile(node, tool)
    script = tmp_path / "https-probe.js"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "windows_native_https_probe.cjs", script)

    def record_timing(label, result):
        tag = "ALYSIS_HTTPS_STAGE "
        lines = [line for line in result.stderr.splitlines() if line.startswith(tag)]
        assert len(lines) == 1 and len(lines[0]) < 512, result.stderr
        evidence = json.loads(lines[0][len(tag) :])
        record_property(f"https_{label}_timing", json.dumps(evidence, sort_keys=True))

    control = subprocess.run(
        [str(tool), str(script)],
        capture_output=True,
        text=True,
        timeout=35,
        env={
            k: v
            for k, v in os.environ.items()
            if k.upper()
            not in {"NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED"}
        },
    )
    record_timing("host", control)
    assert control.returncode == 0 and control.stdout.strip() == "PUBLIC_HTTPS_OK", control.stderr
    for index, network in enumerate(("on", "off", "on")):
        result = WindowsNativeRunner(
            ShellSandboxSettings(
                backend="windows-native",
                network=network,
                windows_read_roots=(str(tool_root),),
            )
        ).run(
            root=tmp_path,
            cwd=tmp_path,
            cmd=f"& {quote(tool)} https-probe.js; exit $LASTEXITCODE",
            timeout_s=60,
        )
        record_timing(f"sandbox_{index}_{network}", result)
        assert result.returncode == (0 if network == "on" else 31), result.stderr
        assert result.stdout.strip() == (
            "PUBLIC_HTTPS_OK" if network == "on" else "NETWORK_BLOCKED"
        )


@pytest.mark.skipif(
    os.environ.get("ALYSIS_RUN_SANDBOX_NETWORK_TESTS") != "1",
    reason="requires explicit public HTTPS compatibility qualification",
)
def test_installed_schannel_https_compatibility(tmp_path):
    # Fetch fixed public package metadata, without credentials, proxies,
    # redirects, package installation or executing anything downloaded.
    command = r"""
$ErrorActionPreference='Stop';
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;
$request=[Net.HttpWebRequest]::Create('https://registry.npmjs.org/is-number/7.0.0');
$request.Proxy=$null; $request.AllowAutoRedirect=$false;
# Host positive controls can take about 12 seconds on slower connections.
$request.Timeout=25000; $request.ReadWriteTimeout=25000;
try {
 $response=$request.GetResponse();
 try {
  $reader=[IO.StreamReader]::new($response.GetResponseStream());
  try { $data=$reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() };
  if ($response.StatusCode -ne 200 -or $data.name -ne 'is-number' -or $data.version -ne '7.0.0') {
   throw 'Unexpected public registry response'
  };
  'PUBLIC_HTTPS_OK'
 } finally { $response.Dispose() }
} catch {
 if ($_.Exception.GetBaseException().NativeErrorCode -eq -2146893042) {
  'UNSUPPORTED_SCHANNEL:0x8009030e'; exit 35
 };
 $errorObject=$_.Exception;
 while ($errorObject -and $errorObject -isnot [Net.WebException]) { $errorObject=$errorObject.InnerException };
 if (-not $errorObject -or $errorObject.Status -notin @('ConnectFailure','NameResolutionFailure','Timeout')) { throw };
 'NETWORK_BLOCKED'; exit 31
}
"""
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    control = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=40,
        env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"},
    )
    assert control.returncode == 0 and control.stdout.strip() == "PUBLIC_HTTPS_OK", control.stderr
    result = WindowsNativeRunner(ShellSandboxSettings(backend="windows-native", network="on")).run(
        root=tmp_path,
        cwd=tmp_path,
        cmd=command,
        timeout_s=60,
    )
    if result.returncode == 35 and result.stdout.strip() == "UNSUPPORTED_SCHANNEL:0x8009030e":
        pytest.xfail(
            "Known release gate: restricted-token Schannel SEC_E_NO_CREDENTIALS; filesystem protection must not be removed to bypass it"
        )
    assert result.returncode == 0 and result.stdout.strip() == "PUBLIC_HTTPS_OK", result.stderr
