// Diagnostic only: snapshot the fixed PowerShell child of a known live broker.
// Emit method names, never memory dumps, locals, command lines or environment.
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Diagnostics.Runtime;

if (!OperatingSystem.IsWindows() || !Environment.Is64BitProcess || args.Length != 1 ||
    Environment.GetEnvironmentVariable("ALYSIS_RUN_INSTALLED_SANDBOX_TESTS") != "1")
    return 2;

try
{
    using var broker = Process.GetProcessById(int.Parse(args[0]));
    if (!string.Equals(broker.MainModule?.FileName,
        Environment.GetEnvironmentVariable("ALYSIS_WINDOWS_SANDBOX_HELPER"),
        StringComparison.OrdinalIgnoreCase))
        return 3;
    var expectedShell = Path.Combine(Environment.SystemDirectory,
        "WindowsPowerShell", "v1.0", "powershell.exe");
    var records = new List<object>();
    foreach (var process in Process.GetProcessesByName("powershell"))
    {
        using (process)
        {
            var info = new IntPtr[6]; // PROCESS_BASIC_INFORMATION, native x64.
            if (Native.NtQueryInformationProcess(process.Handle, 0, info, 48, out _) != 0 ||
                info[5].ToInt64() != broker.Id || process.StartTime < broker.StartTime ||
                !string.Equals(process.MainModule?.FileName, expectedShell,
                    StringComparison.OrdinalIgnoreCase))
                continue;
            // Snapshot inspection does not leave the command suspended. The
            // broker still owns cancellation and all containment/cleanup.
            using var target = DataTarget.CreateSnapshotAndAttach(process.Id);
            foreach (var version in target.ClrVersions)
            {
                // Use only the installed system Framework DAC, no downloads
                // or executable code from the diagnostic workspace.
                var dac = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "Microsoft.NET", "Framework64", "v4.0.30319", "mscordacwks.dll");
                using var runtime = version.CreateRuntime(dac);
                records.Add(new {
                    runtime = version.Version.ToString(),
                    native_thread_count = process.Threads.Count,
                    pool_legacy_data = runtime.ThreadPool?.HasLegacyData,
                    pool_uses_windows = runtime.ThreadPool?.UsingWindowsThreadPool,
                    pool_native_workers = runtime.ThreadPool?.WindowsThreadPoolThreadCount,
                    pool_min_threads = runtime.ThreadPool?.MinThreads,
                    pool_max_threads = runtime.ThreadPool?.MaxThreads,
                    pool_active_workers = runtime.ThreadPool?.ActiveWorkerThreads,
                    pool_idle_workers = runtime.ThreadPool?.IdleWorkerThreads,
                    threads = runtime.Threads.Where(t => t.IsAlive).Take(32).Select(t => new {
                        id = t.OSThreadId,
                        frames = t.EnumerateStackTrace().Take(48)
                            .Select(f => f.Method?.Signature ?? f.FrameName ?? f.Kind.ToString()).ToArray()
                    }).ToArray()
                });
            }
        }
    }
    Console.WriteLine(JsonSerializer.Serialize(new { category = "observed", records }));
    return 0;
}
catch (Exception error)
{
    Console.WriteLine(JsonSerializer.Serialize(new { category = "unavailable",
        exception_type = error.GetType().FullName }));
    return 1;
}

static class Native
{
    [DllImport("ntdll.dll")]
    internal static extern int NtQueryInformationProcess(IntPtr process, int kind,
        [Out] IntPtr[] info, int length, out int returned);
}
