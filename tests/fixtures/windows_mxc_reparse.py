"""Bounded metadata-only reparse probe; only explicit synthetic fixture paths."""

import ctypes
import errno
import json
import os
import struct
import sys
from ctypes import wintypes


def set_junction(link, target):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(link), 0x100, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        return {"stage": "open_metadata", "error": ctypes.get_last_error()}
    try:
        substitute = ("\\??\\" + str(target)).encode("utf-16-le")
        printable = str(target).encode("utf-16-le")
        payload = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printable))
        payload += substitute + b"\0\0" + printable + b"\0\0"
        raw = struct.pack("<IHH", 0xA0000003, len(payload), 0) + payload
        buffer = ctypes.create_string_buffer(raw)
        returned = wintypes.DWORD()
        ok = kernel.DeviceIoControl(
            handle, 0x900A4, buffer, len(raw), None, 0, ctypes.byref(returned), None
        )
        return {"stage": "set_reparse", "error": 0 if ok else ctypes.get_last_error()}
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    if sys.argv[3] == "deny-ancestor":
        # This directory is writable, but contains a separately protected leaf.
        # Exercise the new write-sharing ancestor after PSEC activation, not
        # only a strict leaf or a mutation before workload launch.
        from pathlib import Path

        ancestor, target = Path(sys.argv[1]), Path(sys.argv[2])
        ordinary = ancestor / "ordinary"
        ordinary.write_text("allowed")
        ordinary.rename(ancestor / "renamed")
        assert (ancestor / "renamed").read_text() == "allowed"
        protected = ancestor / "protected"
        assert protected.read_text() == "protected-original"
        attempts = []
        for action, operation in [
            ("write_protected", lambda: protected.write_text("bad")),
            ("delete_protected", lambda: protected.unlink()),
            ("rename_protected", lambda: protected.rename(ancestor / "stolen")),
        ]:
            try:
                operation()
                attempts.append({"action": action, "denied": False})
            except OSError as error:
                attempts.append(
                    {
                        "action": action,
                        "denied": (
                            error.winerror in (5, 32)
                            or (
                                error.winerror is None
                                and error.errno in (errno.EPERM, errno.EACCES)
                            )
                        ),
                        "error": error.winerror,
                        "errno": error.errno,
                    }
                )
        value = set_junction(ancestor, target)
        value.update(is_junction=os.path.isjunction(ancestor), attempts=attempts)
        value["ok"] = (
            value["error"] in (5, 32, 145)
            and not value["is_junction"]
            and all(item["denied"] for item in attempts)
            and protected.read_text() == "protected-original"
        )
        print(json.dumps(value), flush=True)
        sys.exit(0 if value["ok"] else 1)
    value = set_junction(sys.argv[1], sys.argv[2])
    value["is_junction"] = os.path.isjunction(sys.argv[1])
    expected = sys.argv[3]
    value["ok"] = (
        (value["error"] == 0 and value["is_junction"])
        if expected == "allow"
        else (value["error"] in (5, 32) and not value["is_junction"])
    )
    print(json.dumps(value), flush=True)
    sys.exit(0 if value["ok"] else 1)
