"""Handle-based admission for host filesystem tools.

Windows directory handles deny delete sharing for the duration of a tool action.
Every file read is admitted on its opened no-follow handle before returning bytes.
These helpers are inactive for explicitly unsandboxed Full access and non-tool I/O.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from .host_file_policy import HostFilePolicy


_ACTIVE: ContextVar[FileAction | None] = ContextVar("host_file_action", default=None)
_WINDOWS_PATH_ROOT: ContextVar[tuple[tuple[Path, str], ...] | None] = ContextVar(
    "windows_workspace_path_root", default=None
)


def _error(message: str) -> RuntimeError:
    from .host_file_policy import HostFilePolicyError

    return HostFilePolicyError(message)


if os.name == "nt":
    import ctypes
    import msvcrt
    import re
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME),
            ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    class _BasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation", ctypes.c_int64),
            ("access", ctypes.c_int64),
            ("write", ctypes.c_int64),
            ("change", ctypes.c_int64),
            ("attributes", wintypes.DWORD),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel.GetFileType.restype = wintypes.DWORD
    _kernel.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    def _windows_nt_dos_path(name: str) -> Path:
        """Translate only descendants of the retained, launcher-owned workspace."""
        match = re.fullmatch(r"(\\Device\\HarddiskVolume[0-9]+)(\\.*)?", name, re.IGNORECASE)
        if match is None:
            raise _error("Unsupported opened host file volume.")
        suffix = match[2] or "\\"
        if any(c in suffix for c in ("/", ":", "\0")) or any(
            part in {".", ".."} for part in suffix.split("\\")
        ):
            raise _error("Invalid opened host file path.")
        if anchors := _WINDOWS_PATH_ROOT.get():
            for root, native_root in anchors:
                if name.casefold() == native_root.casefold():
                    return root
                if name.casefold().startswith(native_root.casefold() + "\\"):
                    return root / name[len(native_root) + 1 :]
            raise _error("Opened host file is outside the retained workspace.")
        raise _error("Could not identify the opened host file object without a retained workspace.")

    def _windows_final_path(handle: int, info: _FileInfo) -> Path:
        buffer = ctypes.create_unicode_buffer(32768)
        length = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if 0 < length < len(buffer):
            name = buffer.value
            if name.startswith("\\\\?\\UNC\\"):
                name = "\\\\" + name[8:]
            elif name.startswith("\\\\?\\"):
                name = name[4:]
            return Path(name)
        error = ctypes.get_last_error()
        if length or error not in {3, 5}:
            raise _error(f"Could not identify the opened host file object (Windows error {error}).")
        # Keep FILE_NAME_NORMALIZED; FILE_NAME_OPENED could hide a linked target.
        length = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 2)
        if not 0 < length < len(buffer):
            raise _error(
                "Could not identify the opened host file object "
                f"(DOS error {error}; NT error {ctypes.get_last_error()})."
            )
        path = _windows_nt_dos_path(buffer.value)
        # Bind the translated name back to this exact object before returning
        # it to policy checks. Do not trust a drive mapping or a pathname alone.
        check = _kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02200000, None)
        if check == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            actual = _FileInfo()
            if not _kernel.GetFileInformationByHandle(check, ctypes.byref(actual)):
                raise ctypes.WinError(ctypes.get_last_error())
            if (
                _kernel.GetFileType(check) != 1
                or actual.attributes & 0x400
                or (actual.volume, actual.index_high, actual.index_low)
                != (info.volume, info.index_high, info.index_low)
                or actual.links != info.links
            ):
                raise _error("Opened host file changed during volume resolution.")
        finally:
            _kernel.CloseHandle(check)
        return path

    def _windows_handle_info(handle: int, *, directory: bool) -> _FileInfo:
        info = _FileInfo()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if _kernel.GetFileType(handle) != 1 or info.attributes & 0x400:
            raise _error("Blocked a linked or unexpected host file object.")
        if directory and not info.attributes & 0x10:
            raise NotADirectoryError("Expected an opened directory.")
        if not directory and info.attributes & 0x10:
            raise IsADirectoryError("Expected an opened regular file.")
        if not directory and info.links != 1:
            raise _error("Blocked access through a hard-linked file.")
        return info

    def _windows_opened_path(handle: int, *, directory: bool) -> Path:
        return _windows_final_path(handle, _windows_handle_info(handle, directory=directory))

    def _windows_open(
        path: Path, *, directory: bool, deny_writes: bool = False
    ) -> tuple[int, Path]:
        handle = _kernel.CreateFileW(
            str(path),
            # FILE_LIST_DIRECTORY participates in sharing checks. An attribute-
            # only handle does not prevent rename even without SHARE_DELETE.
            0x1 if directory else 0x80000000,
            0x1 if deny_writes else 0x1 | 0x2,
            None,
            3,
            0x00200000 | (0x02000000 if directory else 0),
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return handle, _windows_opened_path(handle, directory=directory)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise


@contextmanager
def windows_workspace_paths(root: Path, *, additional: tuple[Path, ...] = ()) -> Iterator[None]:
    with ExitStack() as stack:
        for path in dict.fromkeys((root, *additional)):
            stack.enter_context(_windows_workspace_path(path))
        yield


@contextmanager
def _windows_workspace_path(root: Path) -> Iterator[None]:
    """Resolve contained mutations using the launcher's retained cwd object.

    PSEC may deny both Mount Manager and QueryDosDevice. The fixed worker can
    derive a narrow mapping from its already authorized workspace handle instead.
    This context grants no access. The kernel still authorizes every open; every
    translated name is reopened and matched to the original volume/file identity.
    """
    if os.name != "nt":
        yield
        return
    # Do not accept UNC/device names or a caller-provided relative root. The
    # dispatcher supplies Path.cwd(), never a model argument or environment hint.
    if not root.is_absolute() or len(root.drive) != 2 or root.drive[1] != ":":
        raise _error("Workspace path resolution requires an absolute local drive path.")
    # Retain the directory against rename. Publication separately denies write
    # sharing on its immediate parent; doing that for the whole action would
    # also prevent ordinary child rename/hardlink operations.
    handle = _kernel.CreateFileW(str(root), 1, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _windows_handle_info(handle, directory=True)
        buffer = ctypes.create_unicode_buffer(32768)
        length = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 2)
        if not 0 < length < len(buffer):
            raise _error(
                "Could not identify the retained workspace "
                f"(Windows error {ctypes.get_last_error()})."
            )
        token = _WINDOWS_PATH_ROOT.set(
            (*(_WINDOWS_PATH_ROOT.get() or ()), (root, buffer.value.rstrip("\\")))
        )
        try:
            yield
        finally:
            _WINDOWS_PATH_ROOT.reset(token)
    finally:
        _kernel.CloseHandle(handle)


class FileAction:
    def __init__(self, policy: HostFilePolicy, *, readonly: bool) -> None:
        self.policy = policy
        self.readonly = readonly
        self._pins: dict[Path, int] = {}

    def close(self) -> None:
        if os.name == "nt":
            for handle in reversed(tuple(self._pins.values())):
                _kernel.CloseHandle(handle)
        self._pins.clear()

    def pin(self, path: Path, *, leaf: bool = False) -> None:
        if os.name != "nt":
            return
        absolute = Path(os.path.abspath(path))
        directories = list(reversed(absolute.parents))
        if leaf:
            directories.append(absolute)
        for current in directories:
            if current in self._pins:
                continue
            try:
                handle, opened = _windows_open(current, directory=True)
            except (FileNotFoundError, NotADirectoryError):
                break
            try:
                # CI uses ordinary NTFS 8.3 names (for example RUNNER~1).
                # The opened directory is already non-reparse and denies rename;
                # admit alternate spelling only when both names identify it.
                if not os.path.samestat(current.lstat(), opened.lstat()):
                    raise _error("Blocked an aliased host file ancestor.")
            except BaseException:
                _kernel.CloseHandle(handle)
                raise
            self._pins[current] = handle

    def admit(self, path: Path) -> None:
        self.pin(path)
        # Read-only actions retain the leaf too, covering readers such as image
        # decoders/read ledgers which open the admitted filename themselves.
        if self.readonly and os.name == "nt" and path not in self._pins:
            try:
                handle, opened = _windows_open(path, directory=False)
            except (FileNotFoundError, NotADirectoryError):
                return
            try:
                self.policy.guard_opened_read(opened)
            except BaseException:
                _kernel.CloseHandle(handle)
                raise
            self._pins[path] = handle


@contextmanager
def host_file_action(policy: HostFilePolicy, *, readonly: bool = False) -> Iterator[None]:
    action = FileAction(policy, readonly=readonly)
    token = _ACTIVE.set(action)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
        action.close()


def admit_host_path(path: Path) -> None:
    if action := _ACTIVE.get():
        action.admit(path)


def guard_host_resolved_read(path: Path) -> None:
    if action := _ACTIVE.get():
        action.policy.guard_read(path.parent, path.name)


def guard_host_mutation(path: Path) -> None:
    if action := _ACTIVE.get():
        action.policy.guard_write(path.parent, path.name)


@contextmanager
def open_host_file_read(path: Path) -> Iterator[BinaryIO]:
    action = _ACTIVE.get()
    if action is None:
        with path.open("rb") as stream:
            yield stream
        return
    action.pin(path)
    action.policy.guard_opened_read(path)
    if os.name == "nt":
        handle, opened = _windows_open(path, directory=False)
        try:
            action.policy.guard_opened_read(opened)
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
    else:
        # Walk each component relative to the preceding directory descriptor;
        # a pathname swap cannot redirect this open through a new symlink.
        absolute = Path(os.path.abspath(path))
        parent = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in absolute.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(fd)
            raise _error("Blocked a non-regular or hard-linked host file.")
        if Path("/proc/self/fd").is_dir():
            try:
                action.policy.guard_opened_read(Path(os.readlink(f"/proc/self/fd/{fd}")))
            except BaseException:
                os.close(fd)
                raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


def mkdir_host_path(path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
    action = _ACTIVE.get()
    if action is None or os.name != "nt":
        path.mkdir(parents=parents, exist_ok=exist_ok)
        return
    action.pin(path)
    if not parents:
        path.mkdir(exist_ok=exist_ok)
        action.pin(path, leaf=True)
        return
    # Admit every new ancestor before creating or opening anything below it.
    # Insertion of a junction between mkdir and admission is rejected unopened.
    for current in (*reversed(path.parents), path):
        if current in action._pins:
            if current == path and not exist_ok:
                raise FileExistsError(str(path))
            continue
        current.mkdir(exist_ok=(exist_ok if current == path else True))
        action.pin(current, leaf=True)


def chmod_host_fd(fd: int, mode: int) -> None:
    """Apply mode to the staged object, never to a replaceable temporary name."""
    if os.name != "nt":
        os.fchmod(fd, mode)
        return
    # Python only added Windows fd chmod in 3.13; support our older runtimes
    # using the equivalent native readonly attribute update on the open handle.
    handle = msvcrt.get_osfhandle(fd)
    current = _FileInfo()
    if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(current)):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = current.attributes
    attributes = attributes & ~1 if mode & stat.S_IWRITE else attributes | 1
    info = _BasicInfo(0, 0, 0, 0, attributes or 0x80)
    if not _kernel.SetFileInformationByHandle(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())


def rename_host_file_no_replace(source: Path, destination: Path) -> None:
    """Rename a regular Windows sibling through its admitted file handle.

    Different parents require the caller's verified copy/staging path (EXDEV).
    The destination is only a leaf name relative to the source's actual parent;
    no destination-path open or replacement is allowed. Failure retains source.
    """
    if os.name != "nt":
        raise NotImplementedError("Handle publication requires Windows.")
    source, destination = (Path(os.path.abspath(path)) for path in (source, destination))
    for path in (source, destination):
        if not path.name or path.name in {".", ".."} or ":" in path.name:
            raise _error("Publication requires ordinary file names.")
    if source.parent != destination.parent:
        raise OSError(errno.EXDEV, "Handle publication requires sibling staging.")

    action = _ACTIVE.get()
    if action is not None and action.readonly:
        raise _error("Blocked publication during a read-only host file action.")
    parent_handle = None
    source_handle = wintypes.HANDLE()
    try:
        if action is not None:
            # Host actions already pin ancestors. Contained workers have the
            # native helper's lifetime pins and cannot list private ancestors.
            action.pin(source)
            action.policy.guard_write(source.parent, source.name)
            action.policy.guard_write(destination.parent, destination.name)
        # Unlike an attribute-only handle, LIST_DIRECTORY with READ-only sharing
        # blocks both parent replacement and FSCTL_SET_REPARSE_POINT writers.
        parent_handle, opened_parent = _windows_open(
            destination.parent, directory=True, deny_writes=True
        )
        if not os.path.samestat(destination.parent.lstat(), opened_parent.lstat()):
            raise _error("Blocked an aliased publication directory.")

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p),
            ]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

        class RenameInformation(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.ULONG),
                ("FileName", wintypes.WCHAR * 1),
            ]

        native = ctypes.WinDLL("ntdll")
        native.NtOpenFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.ULONG,
            ctypes.POINTER(ObjectAttributes),
            ctypes.POINTER(IoStatusBlock),
            wintypes.ULONG,
            wintypes.ULONG,
        ]
        native.NtOpenFile.restype = ctypes.c_long
        native.NtSetInformationFile.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(IoStatusBlock),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        native.NtSetInformationFile.restype = ctypes.c_long
        native.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        native.RtlNtStatusToDosError.restype = wintypes.ULONG
        text = ctypes.create_unicode_buffer(source.name)
        name = UnicodeString(
            len(source.name.encode("utf-16-le")),
            ctypes.sizeof(text),
            ctypes.cast(text, wintypes.LPWSTR),
        )
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            parent_handle,
            ctypes.pointer(name),
            0x40,
            None,
            None,
        )
        io = IoStatusBlock()
        # DELETE | READ_ATTRIBUTES | SYNCHRONIZE, SHARE_READ; do not follow a
        # source reparse point. Relative open binds it to the retained parent.
        result = native.NtOpenFile(
            ctypes.byref(source_handle),
            0x110080,
            ctypes.byref(attributes),
            ctypes.byref(io),
            1,
            0x00200000 | 0x40 | 0x20,
        )
        if result < 0:
            raise ctypes.WinError(native.RtlNtStatusToDosError(result))
        opened_source = _windows_opened_path(source_handle.value, directory=False)
        if opened_source.parent != opened_parent:
            raise _error("Opened publication source has an unexpected parent.")
        if action is not None:
            action.policy.guard_write(opened_source.parent, opened_source.name)
            action.policy.guard_write(opened_parent, destination.name)
        encoded = destination.name.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInformation) + len(encoded))
        rename = RenameInformation.from_buffer(buffer)
        rename.ReplaceIfExists = False
        # NULL plus a simple name uses the already-open source's parent. A
        # non-NULL RootDirectory reopens it for write and conflicts with MXC pins.
        rename.RootDirectory = None
        rename.FileNameLength = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + RenameInformation.FileName.offset, encoded, len(encoded)
        )
        result = native.NtSetInformationFile(
            source_handle, ctypes.byref(io), buffer, len(buffer), 10
        )
        if result < 0:
            raise ctypes.WinError(native.RtlNtStatusToDosError(result))
    finally:
        if source_handle.value:
            _kernel.CloseHandle(source_handle)
        if parent_handle is not None:
            _kernel.CloseHandle(parent_handle)


@contextmanager
def windows_packet_file(root: Path, name: str) -> Iterator[BinaryIO]:
    """Create relative to an admitted directory object, not its mutable pathname.

    The retained file handle denies writers and deletes its own object on close.
    The worker opens it with delete sharing. No pathname cleanup is performed.
    """
    action = _ACTIVE.get()
    if os.name != "nt" or action is None or Path(name).name != name:
        raise _error("Packet creation requires an admitted Windows directory.")
    root = Path(os.path.abspath(root))
    action.pin(root, leaf=True)
    action.policy.guard_write(root, name)

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    native = ctypes.WinDLL("ntdll")
    native.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    native.NtCreateFile.restype = ctypes.c_long
    native.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    native.RtlNtStatusToDosError.restype = wintypes.ULONG
    text = ctypes.create_unicode_buffer(name)
    string = UnicodeString(
        len(name.encode("utf-16-le")), ctypes.sizeof(text), ctypes.cast(text, wintypes.LPWSTR)
    )
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        action._pins[root],
        ctypes.pointer(string),
        0x40,
        None,
        None,
    )
    handle, status = wintypes.HANDLE(), IoStatusBlock()
    result = native.NtCreateFile(
        ctypes.byref(handle),
        0x40010000 | 0x100000,
        ctypes.byref(attributes),
        ctypes.byref(status),
        None,
        0x80,
        1,
        2,
        0x00200000 | 0x1000 | 0x40 | 0x20,
        None,
        0,
    )
    if result < 0:
        raise ctypes.WinError(native.RtlNtStatusToDosError(result))
    try:
        fd = msvcrt.open_osfhandle(handle.value, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "wb") as stream:
        yield stream


@contextmanager
def open_windows_packet_read(path: Path) -> Iterator[BinaryIO]:
    """Share deletion with the launcher's retained, delete-on-close packet."""
    handle = _kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _FileInfo()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if _kernel.GetFileType(handle) != 1 or info.attributes & (0x400 | 0x10) or info.links != 1:
            raise _error("Invalid contained request file.")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream
