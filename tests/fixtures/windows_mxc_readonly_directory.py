"""Bounded replay of the native read-only directory diagnostics (2026-10-11).

Run inside the sandbox being qualified, with its initial cwd set to the same
existing synthetic directory supplied as the sole argument:

    python -I -B native-readonly-directory-probe.py C:\\synthetic\\workspace

Add --require-compatible to make a denied valid directory operation fail the
probe (exit 1). Without it, exit 0 still means observations were collected.
The historical comparison is not the compatibility verdict: matching the old
failure matrix must never be mistaken for a successful fix.

The caller owns sandbox launch and policy. This script launches no processes,
changes no grants or ACLs, reads no file contents, and creates/deletes nothing.
It opens only the supplied directory, closes every successful open, and tests
alternate spellings of that same directory. Run separately with a child
directory as cwd/argument if a second directory is wanted.

The CreateFileW and NtOpenFile matrices reproduce helper call 4 exactly for one
directory. SetCurrentDirectoryW/_wchdir cases reproduce call 3, except that the
separate .git target is deliberately omitted. Those calls used helper SHA256
6ba9d41e3c86bdc473d7993d73ef34cfaf3395ee4643fdaaf43376b16533a30a on Windows build
26200, with a read-only workspace and no explicit Git read root. The historical
matrix below is evidence of that host, not an assertion of desired behavior.

Observed matrix:
* SetCurrentDirectoryW: exact inherited absolute backslash cwd succeeds;
  forward slashes, trailing backslash, /., \\., '.', and \\?\\ prefix fail (5).
* _wchdir: same success/failure split; ucrtbase errno=13 on failure, while the
  msvcrt ctypes errno slot stayed 0. Neither errno slot is a policy assertion.
* CreateFileW: access 0/0x20/0x100020/0x1200a9 succeeds; MAXIMUM_ALLOWED
  (0x02000000) fails (5), for share 3/7 and flags 0x02000000/0x42000000.
* NtOpenFile, access 0x100020/0x1200a9: options 0x4020 succeeds;
  options 1/0x21/0x4021 return 0xc0000022, for share 3/7.
* NtOpenFile, access 0x20/0x02000000: options 1 returns 0xc0000022;
  options 0x21/0x4021/0x4020 return 0xc000000d (no explicit SYNCHRONIZE).

Output is JSON Lines. Each operation records the prior observation separately
from its current result. A different matrix is not treated as probe failure.
Additional path-resolution observations mirror Git for Windows' getcwd APIs,
including its 260-character buffer. They are diagnostics, outside the original
73-case comparison, and only query the supplied synthetic directory.
"""

from __future__ import annotations

import ctypes
import json
import ntpath
import os
import sys
from ctypes import wintypes as w


def directory_compatibility(records: list[dict]) -> dict:
    """Check valid operations independently of the historical failure matrix.

    Invalid NT access/option combinations, MAXIMUM_ALLOWED, and volume-root
    lookups are diagnostics only. No new parent grants are required to pass.
    This checks directory APIs, not complete Git or containment qualification.
    """
    expected = {}
    for spelling in (
        "absolute_backslash",
        "absolute_forward_slash",
        "trailing_backslash",
        "forward_dot",
        "backslash_dot",
        "relative_dot",
        "extended_prefix",
    ):
        expected[("SetCurrentDirectoryW", spelling)] = ("success", True)
        for dll in ("ucrtbase", "msvcrt"):
            expected[("_wchdir", spelling, dll)] = ("result", 0)
    for access in ("0x0", "0x20", "0x100020", "0x1200a9"):
        for share in (3, 7):
            for flags in ("0x2000000", "0x42000000"):
                expected[("CreateFileW", access, share, flags)] = ("success", True)
    for access in ("0x100020", "0x1200a9"):
        for share in (3, 7):
            for options in ("0x1", "0x21", "0x4021", "0x4020"):
                expected[("NtOpenFile", access, share, options)] = ("status", "0x00000000")
    for capacity in (260, 32768):
        expected[("GetFinalPathNameByHandleW", capacity, 0)] = ("valid_path", True)

    fields = {
        "SetCurrentDirectoryW": ("spelling",),
        "_wchdir": ("spelling", "dll"),
        "CreateFileW": ("access", "share", "flags"),
        "NtOpenFile": ("access", "share", "options"),
        "GetFinalPathNameByHandleW": ("capacity", "flags"),
    }
    observed = {}
    failures = []
    for row in records:
        operation = row.get("operation")
        if operation not in fields:
            continue
        key = (operation, *(row.get(field) for field in fields[operation]))
        if key not in expected:
            continue
        if key in observed:
            failures.append({"case": list(key), "reason": "duplicate observation"})
            continue
        field, wanted = expected[key]
        if field == "valid_path":
            length = row.get("result")
            actual = (
                type(length) is int
                and 0 < length < row["capacity"]
                and isinstance(row.get("path"), str)
                and bool(row["path"])
            )
        else:
            actual = row.get(field)
        # bool is an int subclass; malformed observations cannot count as success.
        observed[key] = type(actual) is type(wanted) and actual == wanted
        if not observed[key]:
            failures.append({"case": list(key), "reason": "operation failed", "observed": row})
    for key in sorted(expected.keys() - observed.keys()):
        failures.append({"case": list(key), "reason": "missing observation"})
    return {
        "passed": not failures,
        "required_checks": len(expected),
        "observed_checks": len(observed),
        "failures": failures,
    }


_records: list[dict] = []


def emit(**record: object) -> None:
    _records.append(record)
    print(json.dumps(record, ensure_ascii=True), flush=True)


def main() -> int:
    _records.clear()
    require_compatible = len(sys.argv) == 3 and sys.argv[2] == "--require-compatible"
    if os.name != "nt" or (len(sys.argv) != 2 and not require_compatible):
        emit(
            error="Requires Windows, one absolute synthetic directory, and optional --require-compatible."
        )
        return 2

    target = sys.argv[1]
    initial_cwd = os.getcwd()
    drive, tail = ntpath.splitdrive(target)
    # Lexical checks only: do not resolve links, enumerate, stat, or open parents.
    if (
        len(drive) != 2
        or drive[1] != ":"
        or not tail.startswith("\\")
        or "/" in target
        or target != ntpath.normpath(target)
        or target.endswith("\\")
        or "\0" in target
        or ntpath.normcase(target) != ntpath.normcase(initial_cwd)
    ):
        emit(
            error="Argument must be a normalized absolute DOS directory matching initial cwd.",
            initial_cwd=initial_cwd,
            target=target,
        )
        return 2

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k.SetCurrentDirectoryW.argtypes = [w.LPCWSTR]
    k.SetCurrentDirectoryW.restype = w.BOOL
    k.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        ctypes.c_void_p,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    k.CreateFileW.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    k.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    k.GetFinalPathNameByHandleW.restype = w.DWORD
    k.GetLongPathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    k.GetLongPathNameW.restype = w.DWORD

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", w.ULONG),
            ("RootDirectory", w.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", w.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    nt.NtOpenFile.argtypes = [
        ctypes.POINTER(w.HANDLE),
        w.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        w.DWORD,
        w.DWORD,
    ]
    nt.NtOpenFile.restype = w.LONG

    emit(
        event="start",
        target=target,
        initial_cwd=initial_cwd,
        windows_build=sys.getwindowsversion().build,
        pointer_bits=ctypes.sizeof(ctypes.c_void_p) * 8,
        expected_matrix_source="2026-10-11 helper calls 3 and 4; Windows build 26200",
    )
    checks = 0
    differences = 0

    def record_matches(actual: object, expected: object) -> bool:
        nonlocal checks, differences
        checks += 1
        matches = actual == expected
        differences += not matches
        return matches

    def close(handle: object) -> None:
        if not k.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    spellings = [
        ("absolute_backslash", target),
        ("absolute_forward_slash", target.replace("\\", "/")),
        ("trailing_backslash", target + "\\"),
        ("forward_dot", target + "/."),
        ("backslash_dot", target + "\\."),
        ("relative_dot", "."),
        ("extended_prefix", "\\\\?\\" + target),
    ]
    for label, spelling in spellings:
        expected_ok = label == "absolute_backslash"
        ctypes.set_last_error(0)
        result = bool(k.SetCurrentDirectoryW(spelling))
        error = ctypes.get_last_error()
        emit(
            operation="SetCurrentDirectoryW",
            spelling=label,
            success=result,
            winerror=error,
            prior_expected_success=expected_ok,
            prior_expected_winerror=0 if expected_ok else 5,
            matches_prior=record_matches((result, error), (expected_ok, 0 if expected_ok else 5)),
        )
        for dll in ("ucrtbase", "msvcrt"):
            crt = ctypes.CDLL(dll, use_errno=True)
            crt._wchdir.argtypes = [w.LPCWSTR]
            crt._wchdir.restype = ctypes.c_int
            ctypes.set_errno(0)
            result = crt._wchdir(spelling)
            error = ctypes.get_errno()
            expected_result = 0 if expected_ok else -1
            expected_errno = 13 if not expected_ok and dll == "ucrtbase" else 0
            emit(
                operation="_wchdir",
                dll=dll,
                spelling=label,
                result=result,
                ctypes_errno=error,
                prior_expected_result=expected_result,
                prior_expected_ctypes_errno=expected_errno,
                matches_prior=record_matches((result, error), (expected_result, expected_errno)),
            )
        # All spellings denote target. Restore its canonical spelling before
        # the next case, and stop rather than probing an unexpected cwd.
        restored = bool(k.SetCurrentDirectoryW(target))
        if not restored or ntpath.normcase(os.getcwd()) != ntpath.normcase(target):
            emit(error="Could not restore supplied directory after chdir case.", spelling=label)
            return 2

    invalid_handle = ctypes.c_void_p(-1).value
    # Git for Windows calls GetFinalPathNameByHandleW with DOS normalization,
    # then GetLongPathNameW if that fails. Preserve both failures explicitly.
    # A larger buffer and the NT-volume form distinguish length/root lookup.
    handle = k.CreateFileW(target, 0, 7, None, 3, 0x02000000, None)
    if handle == invalid_handle:
        emit(operation="git_getcwd_open", success=False, winerror=ctypes.get_last_error())
    else:
        try:
            for capacity in (260, 32768):
                for flags in (0, 2):
                    buffer = ctypes.create_unicode_buffer(capacity)
                    ctypes.set_last_error(0)
                    length = k.GetFinalPathNameByHandleW(handle, buffer, capacity, flags)
                    emit(
                        operation="GetFinalPathNameByHandleW",
                        capacity=capacity,
                        flags=flags,
                        result=length,
                        winerror=ctypes.get_last_error(),
                        path=buffer.value if 0 < length < capacity else None,
                    )
        finally:
            close(handle)
    for capacity in (260, 32768):
        buffer = ctypes.create_unicode_buffer(capacity)
        ctypes.set_last_error(0)
        length = k.GetLongPathNameW(target, buffer, capacity)
        emit(
            operation="GetLongPathNameW",
            capacity=capacity,
            result=length,
            winerror=ctypes.get_last_error(),
            path=buffer.value if 0 < length < capacity else None,
        )
    for access in (0, 0x20, 0x100020, 0x1200A9, 0x02000000):
        for share in (3, 7):
            for flags in (0x02000000, 0x42000000):
                ctypes.set_last_error(0)
                handle = k.CreateFileW(target, access, share, None, 3, flags, None)
                error = ctypes.get_last_error()
                success = handle != invalid_handle
                if success:
                    close(handle)
                expected_ok = access != 0x02000000
                emit(
                    operation="CreateFileW",
                    access=hex(access),
                    share=share,
                    flags=hex(flags),
                    success=success,
                    winerror=error,
                    prior_expected_success=expected_ok,
                    prior_expected_winerror=0 if expected_ok else 5,
                    matches_prior=record_matches(
                        (success, error), (expected_ok, 0 if expected_ok else 5)
                    ),
                )

    native_path = "\\??\\" + target
    native_buffer = ctypes.create_unicode_buffer(native_path)
    path_bytes = len(native_path.encode("utf-16-le"))
    if path_bytes > 65532:
        emit(error="Directory exceeds UNICODE_STRING length limit.")
        return 2
    name = UnicodeString(path_bytes, path_bytes + 2, ctypes.cast(native_buffer, w.LPWSTR))
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes), None, ctypes.pointer(name), 0x40, None, None
    )
    for access in (0x20, 0x100020, 0x1200A9, 0x02000000):
        for share in (3, 7):
            for options in (1, 0x21, 0x4021, 0x4020):
                handle = w.HANDLE()
                io = IoStatusBlock()
                status = nt.NtOpenFile(
                    ctypes.byref(handle),
                    access,
                    ctypes.byref(attributes),
                    ctypes.byref(io),
                    share,
                    options,
                )
                unsigned_status = status & 0xFFFFFFFF
                if status >= 0:
                    close(handle)
                if access in (0x20, 0x02000000):
                    expected_status = 0xC0000022 if options == 1 else 0xC000000D
                else:
                    expected_status = 0 if options == 0x4020 else 0xC0000022
                emit(
                    operation="NtOpenFile",
                    access=hex(access),
                    share=share,
                    options=hex(options),
                    status=f"0x{unsigned_status:08x}",
                    prior_expected_status=f"0x{expected_status:08x}",
                    matches_prior=record_matches(unsigned_status, expected_status),
                )

    compatibility = directory_compatibility(_records)
    emit(
        event="complete",
        checks=checks,
        differences_from_prior=differences,
        cwd=os.getcwd(),
        directory_compatibility=compatibility,
        require_compatible=require_compatible,
    )
    return 1 if require_compatible and not compatibility["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
