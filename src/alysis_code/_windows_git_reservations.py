"""Command-scoped protected names for native Git, removed by verified handles.

Existing objects are never owned or removed. Creation uses handle-relative
CREATE_NEW; cleanup checks the original file identity and deletes the opened
object, never a path that another process could replace between check/delete.
"""

from __future__ import annotations

import ctypes
import hashlib
import time
from ctypes import wintypes as w
from pathlib import Path

from ._windows_managed_session import _Api, _PinnedDirectory


class GitLease:
    """Serialize reservation ownership across engine processes/worktrees."""

    def __init__(self, root: Path, timeout: float, cancel_check):
        api = self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
        api.CreateMutexW.restype = w.HANDLE
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        api.ReleaseMutex.argtypes = [w.HANDLE]
        api.CloseHandle.argtypes = [w.HANDLE]
        name = "Local\\Alysis.Git." + hashlib.sha256(str(root).casefold().encode()).hexdigest()
        self.handle = api.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel_check and cancel_check():
                    from .process_reaping import CommandCancelledError

                    raise CommandCancelledError("Git operation cancelled before launch")
                status = api.WaitForSingleObject(self.handle, 100)
                if status in (0, 0x80):
                    break
                if status != 258:
                    raise ctypes.WinError(ctypes.get_last_error())
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another contained Git operation is still running")
        except BaseException:
            api.CloseHandle(self.handle)
            raise

    def close(self):
        self.api.ReleaseMutex(self.handle)
        self.api.CloseHandle(self.handle)


class GitReservations:
    def __init__(self):
        self.api = _Api()
        self.pins = []
        self.created = []

    def reserve(self, root: Path, paths: list[str]):
        api = self.api
        pin = _PinnedDirectory(api, root)
        self.pins.append(pin)
        anchor = api.kernel.CreateFileW(root.anchor, 0x100081, 3, None, 3, 0x02200000, None)
        if anchor == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        pin.handles.append(anchor)
        for part in root.parts[1:]:
            pin.handles.append(
                api.open_relative(
                    pin.handles[-1], part, directory=True, create=False, read_data=True
                )
            )
            pin.validate()
        root_handle = pin.handles[-1]
        opened = {}
        for value in paths:
            path = Path(value)
            if not path.is_relative_to(root) or path == root:
                continue
            parent = root_handle
            parts = path.relative_to(root).parts
            for index, part in enumerate(parts):
                key = parts[: index + 1]
                directory = index < len(parts) - 1 or value.endswith(("/", "\\")) or path.is_dir()
                if key in opened:
                    parent = opened[key]
                    continue
                try:
                    handle = api.open_relative(
                        parent,
                        part,
                        directory=directory,
                        create=True,
                        create_new=True,
                        metadata_only=True,
                        read_data=True,
                    )
                    info = api.info(handle)
                    self.created.append((root.joinpath(*key), handle, info.identity(), directory))
                except FileExistsError:
                    handle = api.open_relative(
                        parent,
                        part,
                        directory=directory,
                        create=False,
                        metadata_only=True,
                        read_data=True,
                    )
                info = api.info(handle)
                if info.attributes & 0x400:
                    api.close(handle)
                    raise ValueError("Protected Git path cannot be a reparse point")
                opened[key] = handle
                pin.handles.append(handle)
                parent = handle

    def close(self):
        api = self.api
        # Closing only happens after the native job has drained (including on
        # cancellation). The helper independently retains its own policy pins.
        for pin in reversed(self.pins):
            pin.close()
        self.pins.clear()
        api.kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        api.kernel.SetFileInformationByHandle.restype = w.BOOL
        for path, _, identity, directory in reversed(self.created):
            handle = api.kernel.CreateFileW(str(path), 0x10081, 1, None, 3, 0x02200000, None)
            if handle == w.HANDLE(-1).value:
                continue  # Never force deletion of a changed/in-use object.
            try:
                info = api.info(handle)
                if info.identity() != identity or info.attributes & 0x400:
                    continue
                if not directory and (info.size_high or info.size_low or info.links != 1):
                    continue
                delete = ctypes.c_ubyte(1)
                # FileDispositionInfo refuses nonempty directories. No recursive
                # deletion, and no pathname-based unlink after the identity check.
                api.kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), 1)
            finally:
                api.close(handle)
        self.created.clear()
