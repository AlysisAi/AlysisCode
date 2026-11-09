# Read-only access checks for the fixed NUL device, under both the runner's
# token and an admin-filtered token. Opening WRITE_DAC does not modify a DACL.
# No caller-selected paths, device writes, permission changes or elevation.
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;

public static class AlysisNullDeviceDiagnostics {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool CreateRestrictedToken(SafeAccessTokenHandle existing, uint flags,
        uint disableCount, IntPtr disable, uint privilegeCount, IntPtr privileges,
        uint restrictedCount, IntPtr restricted, out SafeAccessTokenHandle token);

    public sealed class Observation {
        public bool AdministratorEnabled;
        public int Read;
        public int Write;
        public int ReadWrite;
        public int ReadControl;
        public int WriteDac;
    }

    static int Open(uint access) {
        using (var handle = CreateFileW(@"\\.\NUL", access, 3,
            IntPtr.Zero, 3, 0, IntPtr.Zero)) {
            return handle.IsInvalid ? Marshal.GetLastWin32Error() : 0;
        }
    }

    static Observation Observe() {
        using (var identity = WindowsIdentity.GetCurrent()) {
            return new Observation {
                AdministratorEnabled = new WindowsPrincipal(identity).IsInRole(
                    WindowsBuiltInRole.Administrator),
                Read = Open(0x80000000), Write = Open(0x40000000),
                ReadWrite = Open(0xc0000000), ReadControl = Open(0x20000),
                WriteDac = Open(0x40000)
            };
        }
    }

    public static Observation[] Run() {
        var host = Observe();
        Observation filtered = null;
        using (var identity = WindowsIdentity.GetCurrent()) {
            SafeAccessTokenHandle token;
            // LUA_TOKEN | DISABLE_MAX_PRIVILEGE: never elevate the observation.
            if (!CreateRestrictedToken(identity.AccessToken, 5, 0, IntPtr.Zero,
                0, IntPtr.Zero, 0, IntPtr.Zero, out token)) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            using (token) {
                WindowsIdentity.RunImpersonated(token, () => {
                    filtered = Observe();
                    if (filtered.AdministratorEnabled)
                        throw new InvalidOperationException("Admin-filtered control still has administrator membership");
                });
            }
        }
        return new[] { host, filtered };
    }
}
'@
$observations = [AlysisNullDeviceDiagnostics]::Run()
@{
    schema = 1
    diagnostic_only = $true
    device = 'NUL'
    host = $observations[0]
    admin_filtered = $observations[1]
} | ConvertTo-Json -Depth 4
