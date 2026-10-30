"""Handle-bound Windows I/O for saved sandbox permission authority."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from .. import _windows_session_files as native
from ..config import ConfigError


@contextmanager
def _directory(path: Path, base: Path, *, create: bool = False):
    handles = []
    try:
        # Retain every ancestor without delete sharing, including the config
        # directory. No pathname replacement can redirect publication or reads.
        for parent in reversed(path.parents):
            private = parent == base or parent.is_relative_to(base)
            try:
                fd = native.open_directory(parent, read_security=private, retain_name=True)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    native.create_private_directory(parent)
                except FileExistsError:
                    pass
                fd = native.open_directory(parent, read_security=private, retain_name=True)
            handles.append(fd)
            if private:
                native.assert_current_user_private(fd)
        yield handles[-1]
    except OSError as exc:
        raise ConfigError("Sandbox permission storage could not be opened privately.") from exc
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _validated_leaf(directory: int, name: str) -> int | None:
    fd, _ = native.open_session_leaf(directory, name, append=False)
    if fd is None:
        return None
    try:
        native.assert_same_regular_leaf(directory, name, fd)
        native.assert_current_user_private(fd)
        if os.fstat(fd).st_size > 65536:
            raise ConfigError("Sandbox permission storage is too large.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read(path: Path, base: Path) -> bytes | None:
    with _directory(path, base) as directory:
        if directory is None:
            return None
        fd = _validated_leaf(directory, path.name)
        if fd is None:
            return None
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(65537)
            if len(data) > 65536:
                raise ConfigError("Sandbox permission storage is too large.")
            native.assert_same_regular_leaf(directory, path.name, stream.fileno())
            native.assert_current_user_private(stream.fileno())
            return data


def write(path: Path, base: Path, data: bytes) -> None:
    if len(data) > 65536:
        raise ConfigError("Sandbox permission storage is too large.")
    with _directory(path, base, create=True) as directory:
        previous = _validated_leaf(directory, path.name)
        if previous is not None:
            os.close(previous)
        temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
        fd = native.create_private_file(directory, temporary.name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                native.assert_same_regular_leaf(directory, temporary.name, stream.fileno())
                native.assert_current_user_private(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
