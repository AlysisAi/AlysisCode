"""Native Windows handles for the managed-session boundary (loaded only on Windows).

NtCreateFile.RootDirectory provides the Windows equivalent of openat: a renamed
parent cannot redirect the leaf open. FILE_OPEN_REPARSE_POINT opens the link
itself so admission can reject every reparse type, including junctions. Handles
allow rename/delete sharing; identity checks detect substitutions before sealing,
and the retained file handle continues to reference the original object afterward.

API contracts: learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntcreatefile
and learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo.
"""

from __future__ import annotations

import ctypes as C
import msvcrt
import os
from contextlib import contextmanager
from ctypes import wintypes as W
from pathlib import Path

from .managed_session import ManagedSessionError

_KERNEL = C.WinDLL("kernel32", use_last_error=True)
_NT = C.WinDLL("ntdll", use_last_error=True)
_ADVAPI = C.WinDLL("advapi32", use_last_error=True)
_POINTER = C.c_void_p
_INVALID_HANDLE = C.c_void_p(-1).value
_FILE_READ_DATA = 0x0001
_FILE_APPEND_DATA = 0x0004
_FILE_READ_ATTRIBUTES = 0x0080
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_SYNCHRONIZE = 0x00100000
_SHARE_ALL = 0x00000007
_REPARSE_POINT = 0x00000400
_DIRECTORY = 0x00000010
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1


class _UnicodeString(C.Structure):
    _fields_ = [("Length", W.USHORT), ("MaximumLength", W.USHORT), ("Buffer", W.LPWSTR)]


class _ObjectAttributes(C.Structure):
    _fields_ = [
        ("Length", W.ULONG),
        ("RootDirectory", W.HANDLE),
        ("ObjectName", C.POINTER(_UnicodeString)),
        ("Attributes", W.ULONG),
        ("SecurityDescriptor", _POINTER),
        ("SecurityQualityOfService", _POINTER),
    ]


class _IoStatusBlock(C.Structure):
    # The first field is a union of NTSTATUS and PVOID, hence pointer-sized.
    _fields_ = [("Status", _POINTER), ("Information", C.c_size_t)]


class _FileInfo(C.Structure):
    _fields_ = [
        ("attributes", W.DWORD),
        ("creation", W.FILETIME),
        ("access", W.FILETIME),
        ("write", W.FILETIME),
        ("volume", W.DWORD),
        ("size_high", W.DWORD),
        ("size_low", W.DWORD),
        ("links", W.DWORD),
        ("index_high", W.DWORD),
        ("index_low", W.DWORD),
    ]


class _FileIdInfo(C.Structure):
    _fields_ = [("volume", C.c_ulonglong), ("identifier", C.c_ubyte * 16)]


class _SecurityAttributes(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", _POINTER), ("inherit", W.BOOL)]


def _bind(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


_CloseHandle = _bind(_KERNEL, "CloseHandle", W.BOOL, W.HANDLE)
_CreateFile = _bind(
    _KERNEL,
    "CreateFileW",
    W.HANDLE,
    W.LPCWSTR,
    W.DWORD,
    W.DWORD,
    _POINTER,
    W.DWORD,
    W.DWORD,
    W.HANDLE,
)
_CreateDirectory = _bind(_KERNEL, "CreateDirectoryW", W.BOOL, W.LPCWSTR, _POINTER)
_GetFileInfo = _bind(_KERNEL, "GetFileInformationByHandle", W.BOOL, W.HANDLE, C.POINTER(_FileInfo))
_GetFileInfoEx = _bind(
    _KERNEL,
    "GetFileInformationByHandleEx",
    W.BOOL,
    W.HANDLE,
    C.c_int,
    _POINTER,
    W.DWORD,
)
_GetFileType = _bind(_KERNEL, "GetFileType", W.DWORD, W.HANDLE)
_NtCreateFile = _bind(
    _NT,
    "NtCreateFile",
    W.LONG,
    C.POINTER(W.HANDLE),
    W.ULONG,
    C.POINTER(_ObjectAttributes),
    C.POINTER(_IoStatusBlock),
    _POINTER,
    W.ULONG,
    W.ULONG,
    W.ULONG,
    W.ULONG,
    _POINTER,
    W.ULONG,
)
_NtStatusToError = _bind(_NT, "RtlNtStatusToDosError", W.ULONG, W.LONG)
_LocalFree = _bind(_KERNEL, "LocalFree", _POINTER, _POINTER)
_GetCurrentProcess = _bind(_KERNEL, "GetCurrentProcess", W.HANDLE)
_OpenProcessToken = _bind(
    _ADVAPI, "OpenProcessToken", W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)
)
_GetTokenInformation = _bind(
    _ADVAPI,
    "GetTokenInformation",
    W.BOOL,
    W.HANDLE,
    C.c_int,
    _POINTER,
    W.DWORD,
    C.POINTER(W.DWORD),
)
_SidToString = _bind(_ADVAPI, "ConvertSidToStringSidW", W.BOOL, _POINTER, C.POINTER(W.LPWSTR))
_StringToDescriptor = _bind(
    _ADVAPI,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    W.BOOL,
    W.LPCWSTR,
    W.DWORD,
    C.POINTER(_POINTER),
    C.POINTER(W.DWORD),
)
_GetSecurityInfo = _bind(
    _ADVAPI,
    "GetSecurityInfo",
    W.DWORD,
    W.HANDLE,
    C.c_int,
    W.DWORD,
    C.POINTER(_POINTER),
    _POINTER,
    C.POINTER(_POINTER),
    _POINTER,
    C.POINTER(_POINTER),
)
_GetDacl = _bind(
    _ADVAPI,
    "GetSecurityDescriptorDacl",
    W.BOOL,
    _POINTER,
    C.POINTER(W.BOOL),
    C.POINTER(_POINTER),
    C.POINTER(W.BOOL),
)
_SetSecurityInfo = _bind(
    _ADVAPI,
    "SetSecurityInfo",
    W.DWORD,
    W.HANDLE,
    C.c_int,
    W.DWORD,
    _POINTER,
    _POINTER,
    _POINTER,
    _POINTER,
)
_GetSecurityControl = _bind(
    _ADVAPI,
    "GetSecurityDescriptorControl",
    W.BOOL,
    _POINTER,
    C.POINTER(W.WORD),
    C.POINTER(W.DWORD),
)


def close_handle(handle: int | None) -> None:
    if handle not in (None, 0, -1, _INVALID_HANDLE):
        _CloseHandle(handle)


def _check(success) -> None:
    if not success:
        raise C.WinError(C.get_last_error())


def _sid_text(sid) -> str:
    value = W.LPWSTR()
    _check(_SidToString(sid, C.byref(value)))
    try:
        return value.value
    finally:
        _LocalFree(value)


def _current_user_sid() -> str:
    token = W.HANDLE()
    _check(_OpenProcessToken(_GetCurrentProcess(), 0x0008, C.byref(token)))  # TOKEN_QUERY
    try:
        needed = W.DWORD()
        _GetTokenInformation(token, 1, None, 0, C.byref(needed))  # TokenUser
        if not needed.value:
            raise C.WinError(C.get_last_error())
        buffer = C.create_string_buffer(needed.value)
        _check(_GetTokenInformation(token, 1, buffer, needed, C.byref(needed)))
        return _sid_text(C.cast(buffer, C.POINTER(_POINTER))[0])
    finally:
        close_handle(token.value)


@contextmanager
def _private_descriptor(sid: str, *, set_owner: bool = False):
    descriptor = _POINTER()
    # A protected DACL contains only the actual user's SID, with no inherited ACEs.
    sddl = (f"O:{sid}" if set_owner else "") + f"D:P(A;;FA;;;{sid})"
    _check(_StringToDescriptor(sddl, 1, C.byref(descriptor), None))
    try:
        yield descriptor
    finally:
        _LocalFree(descriptor)


def create_private_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _private_descriptor(_current_user_sid(), set_owner=True) as descriptor:
        attributes = _SecurityAttributes(C.sizeof(_SecurityAttributes), descriptor, False)
        _check(_CreateDirectory(str(path), C.byref(attributes)))


def _information(handle: int) -> tuple[_FileInfo, tuple[int, bytes]]:
    info, identity = _FileInfo(), _FileIdInfo()
    _check(_GetFileInfo(handle, C.byref(info)))
    # Use the full 128-bit identity, including on ReFS; never truncate file IDs.
    _check(_GetFileInfoEx(handle, 18, C.byref(identity), C.sizeof(identity)))  # FileIdInfo
    return info, (identity.volume, bytes(identity.identifier))


def open_directory(path: Path) -> int:
    handle = _CreateFile(
        str(path),
        _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _SHARE_ALL,
        None,
        3,
        0x02200000,
        None,  # OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT
    )
    if handle == _INVALID_HANDLE:
        raise C.WinError(C.get_last_error())
    try:
        info, _ = _information(handle)
        if info.attributes & _REPARSE_POINT or not info.attributes & _DIRECTORY:
            raise ManagedSessionError(
                "Managed session directory must not be a symlink or reparse point"
            )
        # A CRT descriptor retains the native directory handle and lets Python
        # compare its fstat identity directly with the initial lstat result,
        # using the same platform/version-specific file-ID representation.
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        handle = None
        return fd
    except Exception:
        close_handle(handle)
        raise


def assert_same_directory(directory_fd: int, path: Path) -> None:
    current = None
    try:
        current = open_directory(path)
        if (
            _information(msvcrt.get_osfhandle(current))[1]
            != _information(msvcrt.get_osfhandle(directory_fd))[1]
        ):
            raise ManagedSessionError("Managed session directory changed after admission")
    except (OSError, ManagedSessionError) as exc:
        raise ManagedSessionError("Managed session directory changed after admission") from exc
    finally:
        if current is not None:
            os.close(current)


def _open_relative(
    directory: int, filename: str, *, access: int, create: bool = False, descriptor=None
) -> tuple[int, bool]:
    # Only a validated session basename is allowed: no device, ADS or traversal syntax.
    if not filename or any(char in filename for char in "\\/:") or filename in {".", ".."}:
        raise ManagedSessionError("Managed session log name must be a single filename")
    buffer = C.create_unicode_buffer(filename)
    size = len(filename.encode("utf-16-le"))
    name = _UnicodeString(size, size + 2, C.cast(buffer, W.LPWSTR))
    attributes = _ObjectAttributes(
        C.sizeof(_ObjectAttributes),
        msvcrt.get_osfhandle(directory),
        C.pointer(name),
        0x40,
        descriptor,
        None,
    )
    handle, status_block = W.HANDLE(), _IoStatusBlock()
    status = _NtCreateFile(
        C.byref(handle),
        access | _SYNCHRONIZE,
        C.byref(attributes),
        C.byref(status_block),
        None,
        0x80,
        _SHARE_ALL,
        3 if create else 1,  # FILE_OPEN_IF / FILE_OPEN
        0x00200000 | 0x40 | 0x20,
        None,
        0,  # REPARSE_POINT | NON_DIRECTORY | SYNCHRONOUS
    )
    if status < 0:
        raise C.WinError(_NtStatusToError(status))
    return handle.value, status_block.Information == 2  # FILE_CREATED


def open_session_leaf(directory: int, filename: str, *, append: bool) -> tuple[int | None, bool]:
    handle = None
    try:
        access = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _READ_CONTROL | _WRITE_DAC
        if append:
            # Omitting FILE_WRITE_DATA forces writes to EOF in the kernel, even
            # when another writer changes the length between seek and write.
            access |= _FILE_APPEND_DATA
            with _private_descriptor(_current_user_sid(), set_owner=True) as descriptor:
                handle, created = _open_relative(
                    directory,
                    filename,
                    access=access,
                    create=True,
                    descriptor=descriptor,
                )
        else:
            try:
                handle, created = _open_relative(directory, filename, access=access)
            except FileNotFoundError:
                return None, False
        flags = os.O_BINARY | os.O_NOINHERIT | (os.O_RDWR | os.O_APPEND if append else os.O_RDONLY)
        fd = msvcrt.open_osfhandle(handle, flags)
        handle = None  # CRT owns the handle from here, including failure cleanup by the caller.
        return fd, created
    except OSError as exc:
        raise ManagedSessionError("Managed session log could not be opened safely") from exc
    finally:
        close_handle(handle)


def _regular_identity(handle: int) -> tuple[int, bytes]:
    info, identity = _information(handle)
    if _GetFileType(handle) != 1 or info.attributes & (_DIRECTORY | _REPARSE_POINT):
        raise ManagedSessionError(
            "Managed session log could not be opened safely: expected a regular file without reparse points"
        )
    if info.links != 1:
        raise ManagedSessionError("Managed session log must not be hard-linked")
    return identity


def assert_same_regular_leaf(directory: int, filename: str, file_fd: int) -> None:
    current = None
    try:
        opened_identity = _regular_identity(msvcrt.get_osfhandle(file_fd))
        try:
            current, _ = _open_relative(directory, filename, access=_FILE_READ_ATTRIBUTES)
            if _regular_identity(current) != opened_identity:
                raise ManagedSessionError("Managed session log changed while it was being opened")
        except (OSError, ManagedSessionError) as exc:
            raise ManagedSessionError(
                "Managed session log changed while it was being opened"
            ) from exc
    except OSError as exc:
        raise ManagedSessionError("Managed session log could not be inspected safely") from exc
    finally:
        close_handle(current)


def _dacl(descriptor):
    present, defaulted, acl = W.BOOL(), W.BOOL(), _POINTER()
    _check(_GetDacl(descriptor, C.byref(present), C.byref(acl), C.byref(defaulted)))
    if not present or not acl.value:
        raise ManagedSessionError("Managed session log must have an owner-only ACL")
    return acl


def _acl_bytes(acl) -> bytes:
    # ACL header: revision, reserved, size (USHORT), ACE count, reserved.
    size = C.cast(acl, C.POINTER(W.USHORT))[1]
    return C.string_at(acl, size)


def enforce_owner_private_file(file_fd: int) -> None:
    handle = msvcrt.get_osfhandle(file_fd)
    original, verified, owner = _POINTER(), _POINTER(), _POINTER()
    try:
        result = _GetSecurityInfo(
            handle, _SE_FILE_OBJECT, 1, C.byref(owner), None, None, None, C.byref(original)
        )  # OWNER_SECURITY_INFORMATION
        if result:
            raise C.WinError(result)
        with _private_descriptor(_sid_text(owner)) as descriptor:
            acl = _dacl(descriptor)
            result = _SetSecurityInfo(
                handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            )
            if result:
                raise C.WinError(result)
            result = _GetSecurityInfo(
                handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION,
                None,
                None,
                None,
                None,
                C.byref(verified),
            )
            if result:
                raise C.WinError(result)
            control, revision = W.WORD(), W.DWORD()
            _check(_GetSecurityControl(verified, C.byref(control), C.byref(revision)))
            if not control.value & 0x1000 or _acl_bytes(_dacl(verified)) != _acl_bytes(acl):
                raise ManagedSessionError(
                    "Managed session log could not be restricted to its owner"
                )
    except OSError as exc:
        raise ManagedSessionError(
            "Managed session log could not be restricted to its owner"
        ) from exc
    finally:
        if verified:
            _LocalFree(verified)
        if original:
            _LocalFree(original)
