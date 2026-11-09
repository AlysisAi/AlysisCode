// Offline diagnostic only, run after a failed HTTPS qualification. It never
// requests credentials, changes permissions, or replaces the failed result.
using System;
using System.Runtime.InteropServices;

static class WindowsSchannelProbe
{
    [DllImport("secur32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    static extern int QuerySecurityPackageInfoW(string package, out IntPtr info);
    [DllImport("secur32.dll")]
    static extern int FreeContextBuffer(IntPtr buffer);
    [DllImport("secur32.dll")]
    static extern int LsaConnectUntrusted(out IntPtr handle);
    [DllImport("secur32.dll")]
    static extern int LsaDeregisterLogonProcess(IntPtr handle);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    static extern IntPtr GetModuleHandleW(string name);

    static int Main()
    {
        IntPtr buffer;
        int query = QuerySecurityPackageInfoW("Microsoft Unified Security Protocol Provider", out buffer);
        // Close every returned buffer immediately; never output its contents.
        int? freed = query == 0 && buffer != IntPtr.Zero
            ? (int?)FreeContextBuffer(buffer) : null;
        bool loaded = GetModuleHandleW("schannel.dll") != IntPtr.Zero;
        IntPtr handle;
        int lsa = LsaConnectUntrusted(out handle);
        int? closed = lsa == 0 && handle != IntPtr.Zero
            ? (int?)LsaDeregisterLogonProcess(handle) : null;
        Console.WriteLine("ALYSIS_SSPI_PACKAGE_STATUS=" + query);
        Console.WriteLine("ALYSIS_SSPI_PACKAGE_FREE=" + (freed.HasValue ? freed.ToString() : "NONE"));
        Console.WriteLine("ALYSIS_SSPI_MODULE_LOADED=" + loaded);
        Console.WriteLine("ALYSIS_SSPI_LSA_STATUS=" + lsa);
        Console.WriteLine("ALYSIS_SSPI_LSA_CLOSE=" + (closed.HasValue ? closed.ToString() : "NONE"));
        return query == 0 && freed == 0 && lsa == 0 && closed == 0 ? 0 : 1;
    }
}
