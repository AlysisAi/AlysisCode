"""Reserve fixed instruction names using retained, non-reparse Windows handles."""

from __future__ import annotations

import msvcrt
import os
from ctypes import wintypes as w
from pathlib import Path

from ._windows_managed_session import (
    _OPEN_REPARSE_POINT,
    _READ_ATTRIBUTES,
    _SHARE_READ,
    _SHARE_WRITE,
    _SYNCHRONIZE,
    _Api,
    _PinnedDirectory,
)
from .config import ConfigError
from .sandbox_bookkeeping import PROTECTED_INSTRUCTION_FILES, record_instruction_creation


def prepare_instruction_files(root: Path) -> None:
    # Import lazily: the policy module is also importable on POSIX.
    from .windows_sandbox import _local_path

    requested = Path(root)
    if any(part == ".." or part.endswith((" ", ".")) for part in requested.parts):
        raise ConfigError("Sandbox workspace must use an unambiguous local path")
    _local_path(requested)  # Fixed local NTFS, existing directory, no aliases.
    if requested == Path(requested.anchor) or requested == Path.home().resolve():
        raise ConfigError("A drive root or user profile cannot be a sandbox workspace")
    api = _Api()
    directory = _PinnedDirectory(api, requested)
    try:
        anchor = api.kernel.CreateFileW(
            requested.anchor,
            _READ_ATTRIBUTES | _SYNCHRONIZE | 1,  # FILE_LIST_DIRECTORY pins against rename.
            _SHARE_READ | _SHARE_WRITE,
            None,
            3,
            0x02000000 | _OPEN_REPARSE_POINT,
            None,
        )
        if anchor == w.HANDLE(-1).value:
            import ctypes

            raise ctypes.WinError(ctypes.get_last_error())
        directory.handles.append(anchor)
        directory.validate()
        for component in requested.parts[1:]:
            handle = api.open_relative(
                directory.handles[-1], component, directory=True, create=False, read_data=True
            )
            directory.handles.append(handle)
            directory.validate()
        for name in PROTECTED_INSTRUCTION_FILES:
            leaf = None
            try:
                try:
                    leaf = api.open_relative(
                        directory.handles[-1],
                        name,
                        directory=False,
                        create=True,
                        create_new=True,
                        metadata_only=True,
                        read_data=True,
                    )
                except FileExistsError:
                    # Existing files are never modified or adopted as bookkeeping.
                    leaf = api.open_relative(
                        directory.handles[-1],
                        name,
                        directory=False,
                        create=False,
                        metadata_only=True,
                    )
                    api.assert_regular(leaf)
                    continue
                api.assert_regular(leaf)
                # The same creation handle denies write/delete sharing until the
                # receipt is captured. No path re-stat can adopt a user's later edit.
                fd = msvcrt.open_osfhandle(leaf, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
                leaf = None
                try:
                    record_instruction_creation(requested / name, os.fstat(fd))
                finally:
                    os.close(fd)
            finally:
                api.close(leaf)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConfigError(f"Cannot reserve native sandbox instruction files safely: {exc}") from exc
    finally:
        directory.close()
