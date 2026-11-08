# CI qualification only: use the interactive shell's process context, then own
# the worker in a kill-on-close job which permits the sandbox's explicit breakaway.
# https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class AlysisDesktopTestHost {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct StartupInfo {
        public int cb;
        public string reserved, desktop, title;
        public uint x, y, xSize, ySize, xChars, yChars, fill, flags;
        public ushort show, reservedBytes;
        public IntPtr reservedData, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct StartupInfoEx { public StartupInfo startup; public IntPtr attributes; }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr process, thread; public uint processId, threadId; }
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits {
        public long processTime, jobTime;
        public uint flags;
        public UIntPtr minWorkingSet, maxWorkingSet;
        public uint activeProcesses;
        public UIntPtr affinity;
        public uint priority, scheduling;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IoCounters { public ulong readOps, writeOps, otherOps, readBytes, writeBytes, otherBytes; }
    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimits {
        public BasicLimits basic;
        public IoCounters io;
        public UIntPtr processMemory, jobMemory, peakProcessMemory, peakJobMemory;
    }
    [DllImport("user32.dll")] static extern IntPtr GetShellWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool inJob);
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits limits, uint size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, int flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr key, IntPtr value, IntPtr size, IntPtr previous, IntPtr returnedSize);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string directory, ref StartupInfoEx startup, out ProcessInfo info);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);

    static void Check(bool ok, string operation) {
        if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error(), operation);
    }

    public static uint Run(string executable, string arguments, string directory, string environment) {
        IntPtr parent = IntPtr.Zero, job = IntPtr.Zero, list = IntPtr.Zero;
        IntPtr parentValue = IntPtr.Zero, jobValue = IntPtr.Zero, env = IntPtr.Zero;
        bool initialized = false;
        ProcessInfo child = new ProcessInfo();
        try {
            IntPtr window = GetShellWindow();
            if (window == IntPtr.Zero) throw new InvalidOperationException("No interactive desktop shell is available for qualification.");
            uint pid;
            GetWindowThreadProcessId(window, out pid);
            parent = OpenProcess(0x0080 | 0x1000, false, pid); // CREATE_PROCESS | QUERY_LIMITED_INFORMATION
            Check(parent != IntPtr.Zero, "Open desktop shell");
            bool inJob;
            Check(IsProcessInJob(parent, IntPtr.Zero, out inJob), "Inspect desktop shell job");
            if (inJob) throw new InvalidOperationException("Desktop shell is itself job-contained; this host cannot qualify the sandbox.");
            job = CreateJobObject(IntPtr.Zero, null);
            Check(job != IntPtr.Zero, "Create qualification lifetime job");
            var limits = new ExtendedLimits();
            limits.basic.flags = 0x2000 | 0x0800; // KILL_ON_JOB_CLOSE | BREAKAWAY_OK
            Check(SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf<ExtendedLimits>()), "Set qualification lifetime limits");
            IntPtr bytes = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref bytes);
            list = Marshal.AllocHGlobal(bytes);
            Check(InitializeProcThreadAttributeList(list, 2, 0, ref bytes), "Initialize launch attributes");
            initialized = true;
            parentValue = Marshal.AllocHGlobal(IntPtr.Size);
            jobValue = Marshal.AllocHGlobal(IntPtr.Size);
            Marshal.WriteIntPtr(parentValue, parent);
            Marshal.WriteIntPtr(jobValue, job);
            Check(UpdateProcThreadAttribute(list, 0, (IntPtr)0x00020000, parentValue, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero), "Set desktop parent");
            Check(UpdateProcThreadAttribute(list, 0, (IntPtr)0x0002000D, jobValue, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero), "Set qualification lifetime job");
            var startup = new StartupInfoEx();
            startup.startup.cb = Marshal.SizeOf<StartupInfoEx>();
            startup.startup.desktop = @"winsta0\default";
            startup.attributes = list;
            env = Marshal.StringToHGlobalUni(environment);
            Check(CreateProcessW(executable, new StringBuilder("\"" + executable + "\" " + arguments), IntPtr.Zero, IntPtr.Zero, false,
                0x00080000 | 0x00000400 | 0x08000000, env, directory, ref startup, out child), "Launch qualification from desktop");
            uint wait = WaitForSingleObject(child.process, 20 * 60 * 1000);
            if (wait == 258) throw new TimeoutException("Desktop qualification exceeded 20 minutes.");
            Check(wait == 0, "Wait for desktop qualification");
            uint exitCode;
            Check(GetExitCodeProcess(child.process, out exitCode), "Read qualification result");
            return exitCode;
        } finally {
            if (child.thread != IntPtr.Zero) CloseHandle(child.thread);
            if (child.process != IntPtr.Zero) CloseHandle(child.process);
            if (job != IntPtr.Zero) CloseHandle(job); // Also terminates the worker on errors/timeouts.
            if (initialized) DeleteProcThreadAttributeList(list);
            if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
            if (parentValue != IntPtr.Zero) Marshal.FreeHGlobal(parentValue);
            if (jobValue != IntPtr.Zero) Marshal.FreeHGlobal(jobValue);
            if (env != IntPtr.Zero) Marshal.FreeHGlobal(env);
            if (parent != IntPtr.Zero) CloseHandle(parent);
        }
    }
}
'@
