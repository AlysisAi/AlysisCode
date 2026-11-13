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
                    if not directory:
                        # A command watched for the files it changes, in another
                        # thread, is not charged with this file appearing.
                        from .sandbox_bookkeeping import note_reservation_file

                        note_reservation_file(root.joinpath(*key), api.stat(handle), removed=False)
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

    def detach(self) -> list[tuple[Path, tuple[int, bytes], bool]]:
        """Stop holding the entries without removing them, and say which ones were made here.

        A background command outlives the call that starts it. Its entries
        stay, held by the helper for as long as the command runs, and are
        removed later by whoever stops the last such command in the folder
        (``windows_mxc_reservations``). Each is returned with the identity it
        was created with and whether it is a directory, parents first.
        """
        made = [(path, identity, directory) for path, _, identity, directory in self.created]
        for pin in reversed(self.pins):
            pin.close()
        self.pins.clear()
        self.created.clear()
        return made

    def close(self):
        # Closing only happens after the native job has drained (including on
        # cancellation). The helper independently retains its own policy pins.
        for pin in reversed(self.pins):
            pin.close()
        self.pins.clear()
        remove_reservations(
            [(path, identity, directory) for path, _, identity, directory in self.created],
            api=self.api,
        )
        self.created.clear()


class HeldDirectories:
    """Directories kept in existence: nobody can delete or rename them, anybody can write in them.

    The sandbox's rule for a protected directory names its path. If the
    directory went away while a command runs, the command could make its own
    under that name. A handle that does not share deleting keeps it there. It
    shares writing, unlike the helper's own handle on a protected directory:
    that one also stops every rename into the directory, which is how Git
    replaces its index, for every program on the computer.
    """

    def __init__(self, paths: list[Path]) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.CreateFileW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            w.DWORD,
            w.LPVOID,
            w.DWORD,
            w.DWORD,
            w.HANDLE,
        ]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handles: list[int] = []
        # The directories that are held. One that could not be opened is not
        # among them; the caller then leaves it to the helper to hold.
        self.paths: list[Path] = []
        for path in paths:
            # List access makes the handle take part in sharing; without it a
            # handle restricts nobody. Opened as the folder itself, never
            # through a link.
            handle = self.kernel.CreateFileW(str(path), 0x81, 0x3, None, 3, 0x02200000, None)
            if handle != w.HANDLE(-1).value:
                self.handles.append(handle)
                self.paths.append(path)

    def close(self) -> None:
        for handle in self.handles:
            self.kernel.CloseHandle(handle)
        self.handles.clear()
        self.paths.clear()


class HeldNames:
    """Names that do not exist, kept from being created for as long as this object lives.

    This sandbox refuses a path only when it exists. A name that must not
    appear while a command runs is therefore taken by the engine first: the
    entry is created and marked for deletion while a handle to it stays open.
    Windows then refuses to open it and to create anything under its name or
    inside it, for every program, and removes it when the handle closes, also
    when the engine dies. A program that looks the name up is told that it
    cannot be opened, which Git takes as "not there": an empty entry would
    not do, because Git stops at an empty "commondir" and treats an empty
    "rebase-merge" folder as a rebase in progress.
    """

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.CreateFileW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            w.DWORD,
            w.LPVOID,
            w.DWORD,
            w.DWORD,
            w.HANDLE,
        ]
        self.kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, w.LPVOID]
        self.kernel.RemoveDirectoryW.argtypes = [w.LPCWSTR]
        self.kernel.SetFileInformationByHandle.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            w.LPVOID,
            w.DWORD,
        ]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handles: list[int] = []
        self.paths: list[Path] = []

    def hold(self, path: Path, *, directory: bool) -> None:
        """Take ``path``. ``FileExistsError`` when something is there already."""
        kernel = self.kernel
        if directory:
            if not kernel.CreateDirectoryW(str(path), None):
                raise ctypes.WinError(ctypes.get_last_error())
            # DELETE access; opened as the folder itself, never through a link.
            # Deleting is not shared, so nothing can be renamed onto the name.
            handle = kernel.CreateFileW(str(path), 0x00010000, 0x3, None, 3, 0x02200000, None)
        else:
            handle = kernel.CreateFileW(str(path), 0x00010000, 0x3, None, 1, 0x80, None)
        if handle == w.HANDLE(-1).value:
            error = ctypes.WinError(ctypes.get_last_error())
            if directory:
                kernel.RemoveDirectoryW(str(path))
            raise error
        marked = w.BOOL(True)
        # FileDispositionInfo: the deletion that waits for the last handle.
        if not kernel.SetFileInformationByHandle(
            handle, 4, ctypes.byref(marked), ctypes.sizeof(marked)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(handle)
            try:
                path.rmdir() if directory else path.unlink()
            except OSError:
                pass
            raise error
        self.handles.append(handle)
        self.paths.append(path)

    def close(self) -> None:
        for handle in self.handles:
            self.kernel.CloseHandle(handle)
        self.handles.clear()
        self.paths.clear()


def remove_reservations(
    entries: list[tuple[Path, tuple[int, bytes], bool]],
    *,
    api: _Api | None = None,
    announce: bool = False,
) -> tuple[list[Path], list[tuple[Path, tuple[int, bytes], bool]]]:
    """Delete the entries that are still the empty objects this engine made.

    ``entries`` are (path, identity at creation, is a directory), parents
    first. Returns the paths that were removed, and the entries that could not
    be judged or removed yet because something has them open; those are worth
    another try later. Everything else is left alone for good: an entry that
    is gone, that is not the object that was created (someone replaced it),
    that is a link, that was made read-only, or that is no longer empty (a
    file with content or a second name, a directory with something in it that
    is not ours).

    ``announce`` tells change tracking about each file before it goes. A
    foreground command needs none: its entries come and go within the command.
    """
    api = api or _Api()
    api.kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    api.kernel.SetFileInformationByHandle.restype = w.BOOL
    in_use = (5, 32, 33)  # access denied (also: deletion pending), sharing, lock
    removed: list[Path] = []
    busy: list[tuple[Path, tuple[int, bytes], bool]] = []
    for entry in reversed(entries):
        path, identity, directory = entry
        handle = api.kernel.CreateFileW(str(path), 0x10081, 1, None, 3, 0x02200000, None)
        if handle == w.HANDLE(-1).value:
            # Never force deletion of a changed/in-use object. In use is the
            # one case that is tried again; a missing entry needs nothing.
            if ctypes.get_last_error() in in_use:
                busy.append(entry)
            continue
        deleted = False
        try:
            info = api.info(handle)
            if info.identity() != identity or info.attributes & 0x400:
                continue
            if not directory and (info.size_high or info.size_low or info.links != 1):
                continue
            if announce and not directory:
                from .sandbox_bookkeeping import note_reservation_file

                note_reservation_file(path, api.stat(handle), removed=True)
            # Neither call removes a nonempty directory. No recursive
            # deletion, and no pathname-based unlink after the identity check.
            # The first form frees the name at once, also while another
            # program has the entry open: otherwise the name stays taken
            # until that program lets go, and the next command, which makes
            # the entry anew, fails on it. Older systems only know the second.
            unlink_now = w.DWORD(0x1 | 0x2)  # delete, with POSIX semantics
            deleted = bool(
                api.kernel.SetFileInformationByHandle(handle, 21, ctypes.byref(unlink_now), 4)
            )
            if not deleted:
                delete = ctypes.c_ubyte(1)
                deleted = bool(
                    api.kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), 1)
                )
            if not deleted:
                error = ctypes.get_last_error()
                if (not directory and error in (32, 33)) or (
                    directory and any(other[0].is_relative_to(path) for other in busy)
                ):
                    # A file something has locked, or a directory that still
                    # holds one of our own entries that is in use, goes later.
                    # A directory that holds anything else is no longer an
                    # empty reservation, and neither is a read-only file.
                    busy.append(entry)
        finally:
            api.close(handle)
        if deleted:
            # With the second form Windows removes the object only when its
            # last handle closes. Until then it is still there, and so is the
            # directory that holds it.
            probe = api.kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02200000, None)
            if probe != w.HANDLE(-1).value:
                api.close(probe)
                busy.append(entry)
            elif ctypes.get_last_error() in in_use:
                busy.append(entry)
            else:
                removed.append(path)
    return removed, busy
