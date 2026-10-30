"""Engine-owned lifetime observation for a request-gated trusted sandbox helper.

Attach before writing its JSON request. No workload can start until that request
is delivered. The unnamed, non-inherited job is separate from the helper's own
enforcement job. Cancellation waits for captured process handles as well as nested
job accounting; normal completion only observes the active process count.
https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes


class _Limits(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _Limits),
        ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong),
        ("kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong),
        ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


class WindowsSandboxJob:
    def __init__(self, process) -> None:
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel = self._kernel
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
            if not kernel.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            # Popen retains the original process handle, never a reopened numeric PID.
            if not kernel.AssignProcessToJobObject(
                self._handle, wintypes.HANDLE(int(process._handle))
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def wait_empty(self, timeout_s: float = 10) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            info = _Accounting()
            if not self._kernel.QueryInformationJobObject(
                self._handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.active == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Sandbox process family did not drain; cancellation is not acknowledged."
                )
            time.sleep(0.005)

    def _close_process_admission(self) -> None:
        # Existing members remain associated. A nonempty family cannot create
        # another member when the limit is one; when empty it has no spawner.
        # The job is unnamed/non-inherited and cannot be reopened by a workload.
        limits = _ExtendedLimits()
        if not self._kernel.QueryInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        limits.basic.flags |= 0x8  # ACTIVE_PROCESS; preserve the held job's other limits
        limits.basic.active_limit = 1
        if not self._kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        actual = _ExtendedLimits()
        if not self._kernel.QueryInformationJobObject(
            self._handle, 9, ctypes.byref(actual), ctypes.sizeof(actual), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if actual.basic.flags != limits.basic.flags or actual.basic.active_limit != 1:
            raise RuntimeError("Sandbox job process admission was not closed.")

    def _original_members(self) -> list:
        # Process IDs are locators only. Retain each original handle and verify
        # its association with this exact job before using it as completion proof.
        capacity = 32
        while True:

            class ProcessIds(ctypes.Structure):
                _fields_ = [
                    ("assigned", wintypes.DWORD),
                    ("count", wintypes.DWORD),
                    ("ids", ctypes.c_size_t * capacity),
                ]

            info = ProcessIds()
            complete = self._kernel.QueryInformationJobObject(
                self._handle, 3, ctypes.byref(info), ctypes.sizeof(info), None
            )
            if complete and info.count == info.assigned and info.count <= capacity:
                break
            if not complete:
                error = ctypes.get_last_error()
                if error != 234:  # ERROR_MORE_DATA
                    raise ctypes.WinError(error)
            if capacity >= 65536:
                raise RuntimeError(
                    "Sandbox job membership snapshot is incomplete; cancellation is not acknowledged."
                )
            capacity = min(65536, max(capacity * 2, info.assigned, info.count))
        handles = []
        try:
            for pid in info.ids[: info.count]:
                handle = self._kernel.OpenProcess(0x1000 | 0x100000, False, pid)
                if not handle:
                    error = ctypes.get_last_error()
                    if error == 87:  # original process already ceased to exist
                        continue
                    raise ctypes.WinError(error)
                handles.append(handle)
                member = wintypes.BOOL()
                if not self._kernel.IsProcessInJob(handle, self._handle, ctypes.byref(member)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not member.value and self._kernel.WaitForSingleObject(handle, 0) != 0:
                    raise RuntimeError("Sandbox job member identity changed during cancellation.")
            return handles
        except BaseException:
            for handle in handles:
                self._kernel.CloseHandle(handle)
            raise

    def terminate_and_drain(self) -> None:
        deadline = time.monotonic() + 10
        self._close_process_admission()
        handles = self._original_members()
        try:
            if not self._kernel.TerminateJobObject(self._handle, 125):
                raise ctypes.WinError(ctypes.get_last_error())
            self.wait_empty(max(0, deadline - time.monotonic()))
            # ActiveProcesses==0 can precede process-handle signaling. Complete
            # termination of the captured family before acknowledging cancellation.
            for handle in handles:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if self._kernel.WaitForSingleObject(handle, remaining) != 0:
                    raise RuntimeError("Sandbox process termination was not acknowledged.")
        finally:
            for handle in handles:
                self._kernel.CloseHandle(handle)

    def close(self) -> None:
        if self._handle:
            handle, self._handle = self._handle, None
            if not self._kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
