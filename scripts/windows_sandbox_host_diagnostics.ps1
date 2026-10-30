# Read-only diagnostics for disposable CI launch failures. No token, job, ACL,
# desktop or policy is changed. Call inside the same test scope as doctor.
$ErrorActionPreference = 'Stop'
if (-not ('AlysisSandboxDiagnostics.HostContext' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace AlysisSandboxDiagnostics {
    public static class HostContext {
        [StructLayout(LayoutKind.Sequential)]
        struct BasicLimits {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct IoCounters {
            public ulong ReadOperations, WriteOperations, OtherOperations;
            public ulong ReadBytes, WriteBytes, OtherBytes;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct ExtendedLimits {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll")]
        static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll")]
        static extern uint GetCurrentThreadId();
        [DllImport("kernel32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool inJob);
        [DllImport("kernel32.dll", SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool QueryInformationJobObject(IntPtr job, int kind,
            out ExtendedLimits info, uint size, IntPtr returned);
        [DllImport("user32.dll", SetLastError=true)]
        static extern IntPtr GetProcessWindowStation();
        [DllImport("user32.dll", SetLastError=true)]
        static extern IntPtr GetThreadDesktop(uint thread);
        [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool GetUserObjectInformation(IntPtr handle, int kind,
            StringBuilder value, uint bytes, out uint required);

        public static string JobLimits() {
            bool inJob;
            if (!IsProcessInJob(GetCurrentProcess(), IntPtr.Zero, out inJob))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Read host job membership");
            if (!inJob) return "not_in_job";
            ExtendedLimits limits;
            if (!QueryInformationJobObject(IntPtr.Zero, 9, out limits,
                (uint)Marshal.SizeOf(typeof(ExtendedLimits)), IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Read host job limits");
            return String.Format("in_job flags=0x{0:x8} breakaway={1} silent_breakaway={2}",
                limits.Basic.Flags, (limits.Basic.Flags & 0x800) != 0,
                (limits.Basic.Flags & 0x1000) != 0);
        }
        static string ObjectName(IntPtr handle) {
            if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            var value = new StringBuilder(1024);
            uint required;
            if (!GetUserObjectInformation(handle, 2, value, 2048, out required))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Read host desktop name");
            return value.ToString();
        }
        public static string Desktop() {
            return ObjectName(GetProcessWindowStation()) + "\\" +
                ObjectName(GetThreadDesktop(GetCurrentThreadId()));
        }
    }
}
'@
}
. (Join-Path $PSScriptRoot 'windows_test_owner_scope.ps1')
$context = [ordered]@{
    tokenUser = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    tokenOwner = [AlysisSandboxTests.DefaultOwnerScope]::CurrentDefaultOwnerSid()
    sessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
}
try { $context.job = [AlysisSandboxDiagnostics.HostContext]::JobLimits() }
catch { $context.job = 'unavailable: ' + $_.Exception.Message }
try { $context.desktop = [AlysisSandboxDiagnostics.HostContext]::Desktop() }
catch { $context.desktop = 'unavailable: ' + $_.Exception.Message }
$descriptors = [ordered]@{}
foreach ($relative in @('', 'System32', 'System32\WindowsPowerShell',
    'System32\WindowsPowerShell\v1.0', 'System32\WindowsPowerShell\v1.0\powershell.exe')) {
    $path = if ($relative) { Join-Path $env:SystemRoot $relative } else { $env:SystemRoot }
    try { $descriptors[$path] = (Get-Acl -LiteralPath $path).Sddl }
    catch { $descriptors[$path] = 'unavailable: ' + $_.Exception.Message }
}
$context.shellDescriptors = $descriptors
$context | ConvertTo-Json -Depth 3
