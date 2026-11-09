// Compiled separately with the installed .NET Framework compiler. Fixed,
// bounded observations only; this is never a replacement shell or release test.
using System;
using System.Threading;
using System.Runtime.InteropServices;

static class ThreadProbe
{
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool QueueUserWorkItem(IntPtr callback, IntPtr argument, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr GetModuleHandleW(string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, ExactSpelling = true, SetLastError = true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")]
    static extern uint GetCurrentProcessId();
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("ole32.dll")]
    static extern int CoInitializeEx(IntPtr reserved, uint model);
    [DllImport("ole32.dll")]
    static extern void CoUninitialize();
    [DllImport("kernelbase.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    static extern bool DeriveCapabilitySidsFromName(string name, out IntPtr groups,
        out uint groupCount, out IntPtr capabilities, out uint capabilityCount);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CheckTokenCapability(IntPtr token, IntPtr capability, out bool present);
    [DllImport("kernel32.dll")]
    static extern IntPtr LocalFree(IntPtr memory);
    static void FreeSids(IntPtr array, uint count)
    {
        if (array == IntPtr.Zero) return;
        for (int i = 0; i < count; i++) LocalFree(Marshal.ReadIntPtr(array, i * IntPtr.Size));
        LocalFree(array);
    }
    static bool Capability(string name)
    {
        IntPtr groups = IntPtr.Zero, capabilities = IntPtr.Zero;
        uint groupCount = 0, capabilityCount = 0;
        try {
            if (!DeriveCapabilitySidsFromName(name, out groups, out groupCount,
                out capabilities, out capabilityCount))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            if (capabilityCount != 1) throw new InvalidOperationException();
            bool present;
            if (!CheckTokenCapability(IntPtr.Zero, Marshal.ReadIntPtr(capabilities), out present))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return present;
        } finally {
            FreeSids(groups, groupCount);
            FreeSids(capabilities, capabilityCount);
        }
    }
    static void Report(string name, Func<bool> probe)
    {
        try { Console.WriteLine("ALYSIS_CLR_" + name + "=" + probe()); }
        catch (Exception error) {
            Console.WriteLine("ALYSIS_CLR_" + name + "=" + error.GetType().Name + ":" + error.HResult);
        }
    }
    static void Main()
    {
        Console.WriteLine("ALYSIS_CLR_ENTRY=True");
        // Ask Windows for effective capability access, not mere SID presence.
        Report("CAP_REGISTRY_READ", delegate { return Capability("registryRead"); });
        Report("CAP_INSTRUMENTATION", delegate { return Capability("lpacInstrumentation"); });
        Report("CAP_COM", delegate { return Capability("lpacCom"); });
        Report("EVENT", delegate {
            using (var signal = new ManualResetEvent(false)) {
                signal.Set(); return signal.WaitOne(1000);
            }
        });
        Report("SEMAPHORE", delegate {
            using (var signal = new Semaphore(0, 1)) {
                signal.Release(); return signal.WaitOne(1000);
            }
        });
        Report("THREAD", delegate {
            var thread = new Thread(delegate() {});
            thread.IsBackground = true;
            thread.Start(); return thread.Join(2000);
        });
        Report("COM_MTA", delegate {
            int hr = int.MinValue;
            var thread = new Thread(delegate() {
                hr = CoInitializeEx(IntPtr.Zero, 0);
                if (hr >= 0) CoUninitialize();
            });
            thread.IsBackground = true;
            thread.Start();
            bool finished = thread.Join(2000);
            Console.WriteLine("ALYSIS_CLR_COM_HRESULT=" + hr);
            return finished && hr >= 0;
        });
        int worker, completion;
        ThreadPool.GetMinThreads(out worker, out completion);
        Console.WriteLine("ALYSIS_CLR_MIN_WORKER=" + worker);
        ThreadPool.GetMaxThreads(out worker, out completion);
        Console.WriteLine("ALYSIS_CLR_MAX_WORKER=" + worker);
        // Keep this event alive until process exit even if the worker is late.
        var done = new ManualResetEvent(false);
        Report("POOL_QUEUE", delegate {
            return ThreadPool.QueueUserWorkItem(delegate { done.Set(); });
        });
        Console.WriteLine("ALYSIS_CLR_POOL_COMPLETED=" + done.WaitOne(3000));
        Report("SELF_WORKER_ACCESS", delegate {
            // PROCESS_CREATE_THREAD | PROCESS_DUP_HANDLE, on this process only.
            IntPtr handle = OpenProcess(0x42, false, GetCurrentProcessId());
            if (handle == IntPtr.Zero) {
                Console.WriteLine("ALYSIS_CLR_SELF_WORKER_ERROR=" + Marshal.GetLastWin32Error());
                return false;
            }
            return CloseHandle(handle);
        });
        var nativeDone = new ManualResetEvent(false);
        // A reverse P/Invoke delegate would require another CLR thread attach.
        // SetEvent has the worker callback ABI: one pointer argument and a
        // 32-bit return value. This callback stays entirely in Windows code.
        IntPtr callback = GetProcAddress(GetModuleHandleW("kernel32.dll"), "SetEvent");
        Report("NATIVE_POOL_QUEUE", delegate {
            bool queued = callback != IntPtr.Zero && QueueUserWorkItem(
                callback, nativeDone.SafeWaitHandle.DangerousGetHandle(), 0);
            if (!queued)
                Console.WriteLine("ALYSIS_CLR_NATIVE_POOL_ERROR=" + Marshal.GetLastWin32Error());
            return queued;
        });
        Console.WriteLine("ALYSIS_CLR_NATIVE_POOL_COMPLETED=" + nativeDone.WaitOne(3000));
        GC.KeepAlive(nativeDone);
        GC.KeepAlive(done);
    }
}
