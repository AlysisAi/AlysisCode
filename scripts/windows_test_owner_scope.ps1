# Test-process-only scope: elevated GitHub runners may default new filesystem
# objects to Administrators. Native workspace admission deliberately requires
# TokenUser ownership. Set that default before any test subprocess creates its
# fixtures; this also covers nested mkdir/tempfile and sandbox-doctor workspaces.
# No existing object, ACL, account, privilege, group or elevation is changed.
if (-not ('AlysisSandboxTests.DefaultOwnerScope' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

namespace AlysisSandboxTests {
    public sealed class DefaultOwnerScope : IDisposable {
        const int TokenUser = 1;
        const int TokenOwner = 4;
        const uint TokenQuery = 0x0008;
        const uint TokenAdjustDefault = 0x0080;
        IntPtr token;
        IntPtr originalOwner;
        bool changed;

        [DllImport("kernel32.dll")]
        static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool CloseHandle(IntPtr handle);
        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer,
            int length, out int required);
        [DllImport("advapi32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool SetTokenInformation(IntPtr token, int kind, IntPtr buffer, int length);

        static IntPtr ReadInfo(IntPtr handle, int kind) {
            int required;
            GetTokenInformation(handle, kind, IntPtr.Zero, 0, out required);
            if (Marshal.GetLastWin32Error() != 122 || required <= 0)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Size token information");
            IntPtr buffer = Marshal.AllocHGlobal(required);
            if (!GetTokenInformation(handle, kind, buffer, required, out required)) {
                int error = Marshal.GetLastWin32Error();
                Marshal.FreeHGlobal(buffer);
                throw new Win32Exception(error, "Read token information");
            }
            return buffer;
        }

        static string SidAt(IntPtr buffer) {
            return new SecurityIdentifier(Marshal.ReadIntPtr(buffer)).Value;
        }

        public static string CurrentDefaultOwnerSid() {
            IntPtr handle;
            if (!OpenProcessToken(GetCurrentProcess(), TokenQuery, out handle))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Open test process token");
            IntPtr owner = IntPtr.Zero;
            try {
                owner = ReadInfo(handle, TokenOwner);
                return SidAt(owner);
            } finally {
                if (owner != IntPtr.Zero) Marshal.FreeHGlobal(owner);
                CloseHandle(handle);
            }
        }

        public static DefaultOwnerScope EnterCurrentUser() {
            var scope = new DefaultOwnerScope();
            IntPtr user = IntPtr.Zero;
            try {
                if (!OpenProcessToken(GetCurrentProcess(), TokenQuery | TokenAdjustDefault, out scope.token))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Open test process token");
                scope.originalOwner = ReadInfo(scope.token, TokenOwner);
                user = ReadInfo(scope.token, TokenUser);
                if (SidAt(scope.originalOwner) != SidAt(user)) {
                    // TOKEN_USER and TOKEN_OWNER both start with their SID pointer;
                    // TokenOwner consumes exactly that pointer, not user attributes.
                    if (!SetTokenInformation(scope.token, TokenOwner, user, IntPtr.Size))
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "Set test default owner");
                    scope.changed = true;
                }
                if (CurrentDefaultOwnerSid() != SidAt(user))
                    throw new InvalidOperationException("Test default owner does not match TokenUser");
                return scope;
            } catch {
                scope.Dispose();
                throw;
            } finally {
                if (user != IntPtr.Zero) Marshal.FreeHGlobal(user);
            }
        }

        public void Dispose() {
            if (token == IntPtr.Zero) return;
            try {
                if (changed && !SetTokenInformation(token, TokenOwner, originalOwner, IntPtr.Size))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Restore test default owner");
            } finally {
                if (originalOwner != IntPtr.Zero) Marshal.FreeHGlobal(originalOwner);
                CloseHandle(token);
                originalOwner = IntPtr.Zero;
                token = IntPtr.Zero;
            }
        }
    }
}
'@
}
