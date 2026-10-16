from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_json", "atomic_write_text"]
_IGNORED_DIR_FSYNC_ERRNOS = frozenset(
    {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", getattr(errno, "ENOTSUP", errno.EINVAL)),
    }
)


def _atomic_temp_path(path: Path) -> tuple[int, Path]:
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    return fd, Path(temp_name)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _IGNORED_DIR_FSYNC_ERRNOS:
            return
        raise
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno in _IGNORED_DIR_FSYNC_ERRNOS:
            return
        raise
    finally:
        os.close(dir_fd)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    fd, temp_path = _atomic_temp_path(path)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            if existing_mode is not None:
                os.chmod(temp_path, existing_mode)
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_dir(path.parent)
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int = 2,
    sort_keys: bool = True,
    ensure_ascii: bool = False,
    encoding: str = "utf-8",
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            payload,
            indent=indent,
            sort_keys=sort_keys,
            ensure_ascii=ensure_ascii,
        )
        + "\n",
        encoding=encoding,
    )
