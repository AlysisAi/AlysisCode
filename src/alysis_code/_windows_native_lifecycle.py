"""Private cancellation/cleanup events for the trusted native broker.

Duplicate handles into the original Popen process before sending its request.
Neither handle is inheritable or named, so a workload cannot acknowledge cleanup.
The broker acknowledges only after its native process guards have unwound.
"""

from __future__ import annotations

import ctypes
import json
from ctypes import wintypes


class WindowsNativeLifecycle:
    def __init__(self, process, request: bytes) -> None:
        self._handles = []
        self._kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel.DuplicateHandle.restype = wintypes.BOOL
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        try:
            remote = []
            # Give the helper only wait rights on cancellation and signal rights
            # on completion. Keep both engine handles until helper exit.
            for access in (0x100000, 0x2):  # SYNCHRONIZE, EVENT_MODIFY_STATE
                handle = kernel.CreateEventW(None, True, False, None)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handles.append(handle)
                duplicate = wintypes.HANDLE()
                if not kernel.DuplicateHandle(
                    kernel.GetCurrentProcess(),
                    handle,
                    int(process._handle),
                    ctypes.byref(duplicate),
                    access,
                    False,
                    0,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                remote.append(duplicate.value)
            payload = json.loads(request)
            payload["lifecycle"] = {"cancel": remote[0], "complete": remote[1]}
            self.request = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except BaseException:
            # The caller reaps the request-gated helper, closing remote handles.
            self.close()
            raise

    def cancel(self) -> None:
        if not self._kernel.SetEvent(self._handles[0]):
            raise ctypes.WinError(ctypes.get_last_error())

    def verify(self) -> None:
        if self._kernel.WaitForSingleObject(self._handles[1], 0) != 0:
            raise RuntimeError("Native sandbox cleanup was not acknowledged by the broker.")

    def close(self) -> None:
        while self._handles:
            if not self._kernel.CloseHandle(self._handles.pop()):
                raise ctypes.WinError(ctypes.get_last_error())
