"""Windows handle-relative admission for managed session logs.

NtCreateFile documents RootDirectory-relative opens and FILE_OPEN_REPARSE_POINT:
https://learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntcreatefile
Every relative name here is exactly one component. Each directory handle remains
open without FILE_SHARE_DELETE, including the drive root and all ancestors. Replay
and append use the same admitted leaf handle, transferred to the Python CRT.
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import stat
from ctypes import wintypes as w
from pathlib import Path

from . import _windows_session_files as _main_windows
from .managed_session import (
    ManagedSessionError,
    ManagedSessionPersistence,
    _close_quietly,
    _identity,
    _read_events_from_fd,
)

_READ_ATTRIBUTES = 0x80
_SYNCHRONIZE = 0x100000
_READ_CONTROL = 0x20000
_WRITE_DAC = 0x40000
_DIRECTORY = 0x10
_REPARSE_POINT = 0x400
_SHARE_READ = 1
_SHARE_WRITE = 2
_OPEN_REPARSE_POINT = 0x200000


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", w.ULONG),
        ("RootDirectory", w.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", w.ULONG),
        ("SecurityDescriptor", w.LPVOID),
        ("SecurityQualityOfService", w.LPVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_size_t), ("Information", ctypes.c_size_t)]


class _FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", w.DWORD),
        ("created", w.FILETIME),
        ("accessed", w.FILETIME),
        ("written", w.FILETIME),
        ("volume", w.DWORD),
        ("size_high", w.DWORD),
        ("size_low", w.DWORD),
        ("links", w.DWORD),
        ("index_high", w.DWORD),
        ("index_low", w.DWORD),
    ]

    def identity(self) -> tuple[int, bytes]:
        return self._full_identity


class _Api:
    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        bindings = [
            (
                self.kernel,
                "CreateFileW",
                [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE],
                w.HANDLE,
            ),
            (self.kernel, "CloseHandle", [w.HANDLE], w.BOOL),
            (
                self.kernel,
                "GetFileInformationByHandle",
                [w.HANDLE, ctypes.POINTER(_FileInfo)],
                w.BOOL,
            ),
            (self.kernel, "GetFileType", [w.HANDLE], w.DWORD),
            (self.kernel, "GetCurrentProcess", [], w.HANDLE),
            (
                self.kernel,
                "DuplicateHandle",
                [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD],
                w.BOOL,
            ),
            (self.kernel, "GetDriveTypeW", [w.LPCWSTR], w.UINT),
            (self.kernel, "LocalFree", [w.LPVOID], w.LPVOID),
            (
                self.advapi,
                "OpenProcessToken",
                [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)],
                w.BOOL,
            ),
            (
                self.advapi,
                "GetTokenInformation",
                [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)],
                w.BOOL,
            ),
            (self.advapi, "ConvertSidToStringSidW", [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL),
            (
                self.advapi,
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)],
                w.BOOL,
            ),
            (
                self.advapi,
                "GetSecurityDescriptorDacl",
                [
                    w.LPVOID,
                    ctypes.POINTER(w.BOOL),
                    ctypes.POINTER(w.LPVOID),
                    ctypes.POINTER(w.BOOL),
                ],
                w.BOOL,
            ),
            (
                self.advapi,
                "GetSecurityInfo",
                [
                    w.HANDLE,
                    w.DWORD,
                    w.DWORD,
                    ctypes.POINTER(w.LPVOID),
                    w.LPVOID,
                    w.LPVOID,
                    w.LPVOID,
                    ctypes.POINTER(w.LPVOID),
                ],
                w.DWORD,
            ),
            (
                self.advapi,
                "SetSecurityInfo",
                [w.HANDLE, w.DWORD, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID],
                w.DWORD,
            ),
            (
                self.ntdll,
                "NtCreateFile",
                [
                    ctypes.POINTER(w.HANDLE),
                    w.DWORD,
                    ctypes.POINTER(_ObjectAttributes),
                    ctypes.POINTER(_IoStatusBlock),
                    w.LPVOID,
                    w.DWORD,
                    w.DWORD,
                    w.DWORD,
                    w.DWORD,
                    w.LPVOID,
                    w.DWORD,
                ],
                w.LONG,
            ),
            (self.ntdll, "RtlNtStatusToDosError", [w.LONG], w.ULONG),
        ]
        for library, name, args, result in bindings:
            function = getattr(library, name)
            function.argtypes = args
            function.restype = result

    def close(self, handle: int | None) -> None:
        if handle is not None:
            self.kernel.CloseHandle(handle)

    def info(self, handle: int) -> _FileInfo:
        value = _FileInfo()
        if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        if self.kernel.GetFileType(handle) != 1:  # FILE_TYPE_DISK
            raise ManagedSessionError("Managed session log must be a regular disk file")
        # Retain main's full 128-bit volume/file identity, including on ReFS.
        # BY_HANDLE_FILE_INFORMATION's 64-bit index is not the identity gate.
        value._full_identity = _main_windows._information(handle)[1]
        return value

    def stat(self, handle: int) -> os.stat_result:
        # Match lstat using this Python version's native identity representation
        # while retaining ownership of the original ancestor pin.
        duplicate = w.HANDLE()
        process = self.kernel.GetCurrentProcess()
        if not self.kernel.DuplicateHandle(
            process, handle, process, ctypes.byref(duplicate), 0, False, 2
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        except Exception:
            self.close(duplicate.value)
            raise
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def sid_string(self, sid: int) -> str:
        value = w.LPWSTR()
        if not self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return value.value or ""
        finally:
            self.kernel.LocalFree(value)

    def current_user(self) -> str:
        token = w.HANDLE()
        if not self.advapi.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 8, ctypes.byref(token)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = w.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return self.sid_string(ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0])
        finally:
            self.close(token.value)

    def private_descriptor(self, owner: str) -> w.LPVOID:
        descriptor = w.LPVOID()
        # The descriptor is used at creation as well as on an admitted existing file.
        # Keep main's owner-only policy. Every nested directory/file is born
        # with an explicit descriptor, so inherited grants are unnecessary.
        sddl = f"O:{owner}D:P(A;;FA;;;{owner})"
        if not self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return descriptor

    def admit_private_file(self, handle: int, *, owner: str, descriptor: w.LPVOID) -> None:
        self.assert_regular(handle)
        actual_owner, actual_sd = w.LPVOID(), w.LPVOID()
        error = self.advapi.GetSecurityInfo(
            handle, 1, 1, ctypes.byref(actual_owner), None, None, None, ctypes.byref(actual_sd)
        )
        if error:
            raise ctypes.WinError(error)
        try:
            if self.sid_string(actual_owner.value) != owner:
                raise ManagedSessionError("Managed session log must be owned by the current user")
        finally:
            self.kernel.LocalFree(actual_sd)
        present, defaulted, dacl = w.BOOL(), w.BOOL(), w.LPVOID()
        if (
            not self.advapi.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            )
            or not present.value
            or not dacl.value
        ):
            raise ManagedSessionError("Managed session log private permissions are unavailable")
        # This is a verified regular file, never a directory: no descendant propagation.
        error = self.advapi.SetSecurityInfo(handle, 1, 0x80000004, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
        verified = w.LPVOID()
        try:
            error = self.advapi.GetSecurityInfo(
                handle, 1, 4, None, None, None, None, ctypes.byref(verified)
            )
            if error:
                raise ctypes.WinError(error)
            control, revision = w.WORD(), w.DWORD()
            _main_windows._check(
                _main_windows._GetSecurityControl(
                    verified, ctypes.byref(control), ctypes.byref(revision)
                )
            )
            if not control.value & 0x1000 or _main_windows._acl_bytes(
                _main_windows._dacl(verified)
            ) != _main_windows._acl_bytes(dacl):
                raise ManagedSessionError(
                    "Managed session log could not be restricted to its owner"
                )
        finally:
            self.kernel.LocalFree(verified)

    def assert_regular(self, handle: int) -> _FileInfo:
        info = self.info(handle)
        if info.attributes & (_DIRECTORY | _REPARSE_POINT):
            raise ManagedSessionError(
                "Managed session log could not be opened safely: not a regular file"
            )
        if info.links != 1:
            raise ManagedSessionError("Managed session log must not be hard-linked")
        return info

    def open_relative(
        self,
        parent: int,
        name: str,
        *,
        directory: bool,
        create: bool,
        append: bool = False,
        descriptor: w.LPVOID | None = None,
        metadata_only: bool = False,
        create_new: bool = False,
        read_data: bool = False,
    ) -> int:
        if not name or name in {".", ".."} or any(c in name for c in "\\/:\x00"):
            raise ManagedSessionError("Managed session path must use ordinary file names")
        buffer = ctypes.create_unicode_buffer(name)
        size = len(name.encode("utf-16-le"))
        unicode_name = _UnicodeString(size, size + 2, ctypes.cast(buffer, w.LPWSTR))
        attrs = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent,
            ctypes.pointer(unicode_name),
            0x40,
            descriptor,
            None,  # OBJ_CASE_INSENSITIVE, non-inheritable handle
        )
        access = _READ_ATTRIBUTES | _SYNCHRONIZE
        if read_data:
            access |= 1  # FILE_READ_DATA / FILE_LIST_DIRECTORY engages sharing checks.
        if not directory and not metadata_only:
            access |= 1 | _READ_CONTROL | _WRITE_DAC  # FILE_READ_DATA
            if append:
                access |= 4  # FILE_APPEND_DATA, not overwrite access
        result, io = w.HANDLE(), _IoStatusBlock()
        status = self.ntdll.NtCreateFile(
            ctypes.byref(result),
            access,
            ctypes.byref(attrs),
            ctypes.byref(io),
            None,
            0,
            _SHARE_READ | (_SHARE_WRITE if directory or (metadata_only and not create_new) else 0),
            2 if create_new else (3 if create else 1),  # CREATE / OPEN_IF / OPEN; no truncation
            _OPEN_REPARSE_POINT | 0x20 | (1 if directory else 0x40),
            None,
            0,  # SYNCHRONOUS_IO_NONALERT, DIRECTORY_FILE / NON_DIRECTORY_FILE
        )
        if status < 0:
            raise ctypes.WinError(self.ntdll.RtlNtStatusToDosError(status))
        return result.value


class _PinnedDirectory:
    def __init__(self, api: _Api, path: Path) -> None:
        self.api, self.path = api, path
        self.handles: list[int] = []

    def validate(self) -> None:
        for handle in self.handles:
            attributes = self.api.info(handle).attributes
            if attributes & _REPARSE_POINT:
                raise ManagedSessionError(
                    "Managed session directory must not be a symlink or reparse point"
                )
            if not attributes & _DIRECTORY:
                raise ManagedSessionError("Managed session destination must be a directory")

    def validate_leaf(self, filename: str, file_fd: int) -> None:
        expected = self.api.assert_regular(msvcrt.get_osfhandle(file_fd))
        handle = self.api.open_relative(
            self.handles[-1], filename, directory=False, create=False, metadata_only=True
        )
        try:
            actual = self.api.assert_regular(handle)
            if expected.identity() != actual.identity():
                raise ManagedSessionError("Managed session log changed after admission")
        finally:
            self.api.close(handle)

    def close(self) -> None:
        for handle in reversed(self.handles):
            self.api.close(handle)
        self.handles.clear()


def prepare_windows_managed_session(
    *, sessions_dir: Path, session_id: str, append: bool
) -> ManagedSessionPersistence:
    requested = Path(sessions_dir).expanduser()
    if any(part == ".." or part.endswith((" ", ".")) for part in requested.parts):
        raise ManagedSessionError("Managed session directory must use an unambiguous local path")
    requested = requested.absolute()
    if requested.drive.startswith("\\") or len(requested.drive) != 2:
        raise ManagedSessionError("Managed session directory requires a local drive path")
    api = _Api()
    if api.kernel.GetDriveTypeW(requested.anchor) != 3:  # DRIVE_FIXED
        raise ManagedSessionError("Managed session directory requires a local fixed drive")
    owner = api.current_user()
    try:
        before = requested.lstat()
    except FileNotFoundError:
        before = None
    if before is not None:
        if before.st_file_attributes & _REPARSE_POINT:
            raise ManagedSessionError(
                "Managed session directory must not be a symlink or reparse point"
            )
        if not stat.S_ISDIR(before.st_mode):
            raise ManagedSessionError("Managed session destination must be a directory")
    descriptor = api.private_descriptor(owner)
    directory = _PinnedDirectory(api, requested)
    file_fd, leaf = None, None
    try:
        anchor = api.kernel.CreateFileW(
            requested.anchor,
            _READ_ATTRIBUTES | _SYNCHRONIZE | 1,  # FILE_LIST_DIRECTORY enforces no-delete sharing.
            _SHARE_READ | _SHARE_WRITE,
            None,
            3,
            0x02000000 | _OPEN_REPARSE_POINT,
            None,
        )
        if anchor == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        directory.handles.append(anchor)
        directory.validate()
        for component in requested.parts[1:]:
            try:
                handle = api.open_relative(
                    directory.handles[-1], component, directory=True, create=False, read_data=True
                )
            except FileNotFoundError:
                if not append:
                    raise
                handle = api.open_relative(
                    directory.handles[-1],
                    component,
                    directory=True,
                    create=True,
                    descriptor=descriptor,
                    read_data=True,
                )
            directory.handles.append(handle)
            directory.validate()
        if before is not None:
            opened = api.stat(directory.handles[-1])
            after = requested.lstat()
            if _identity(opened) != _identity(before) or _identity(after) != _identity(before):
                raise ManagedSessionError(
                    "Managed session directory changed while it was being opened"
                )
        try:
            sidecar = api.open_relative(
                directory.handles[-1],
                f"{session_id}.jsonl.uncommitted.json",
                directory=False,
                create=False,
                metadata_only=True,
            )
        except FileNotFoundError:
            pass
        else:
            api.close(sidecar)
            raise ManagedSessionError(
                "Managed session has a legacy uncommitted sidecar; reconcile it before opening"
            )
        try:
            leaf = api.open_relative(
                directory.handles[-1],
                f"{session_id}.jsonl",
                directory=False,
                create=append,
                append=append,
                descriptor=descriptor,
            )
        except FileNotFoundError:
            if append:
                raise
        if leaf is not None:
            directory.validate()
            api.admit_private_file(leaf, owner=owner, descriptor=descriptor)
            flags = (os.O_RDWR | os.O_APPEND) if append else os.O_RDONLY
            file_fd = msvcrt.open_osfhandle(leaf, flags | os.O_BINARY | os.O_NOINHERIT)
            leaf = None  # Ownership transferred to the CRT descriptor.
            directory.validate_leaf(f"{session_id}.jsonl", file_fd)
        events = _read_events_from_fd(file_fd) if file_fd is not None else ()
        return ManagedSessionPersistence(
            directory_fd=-1,
            directory_capability=directory,
            sessions_dir=requested,
            session_id=session_id,
            file_fd=file_fd,
            append_enabled=append,
            events=events,
        )
    except Exception as exc:
        api.close(leaf)
        _close_quietly(file_fd)
        directory.close()
        if isinstance(exc, ManagedSessionError):
            raise
        if isinstance(exc, OSError):
            if exc.winerror == 267:
                raise ManagedSessionError(
                    "Managed session destination must be a directory"
                ) from exc
            raise ManagedSessionError("Managed session log could not be opened safely") from exc
        raise
    finally:
        api.kernel.LocalFree(descriptor)
