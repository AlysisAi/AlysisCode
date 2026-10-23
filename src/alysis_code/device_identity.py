"""A persistent, random identity for this local CLI installation.

This is not a credential or a hardware fingerprint. Keep it across logout and
hostname changes; a separate data directory represents a separate installation.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from .config import _data_dir


def _read_identity(path: Path) -> str:
    value = path.read_text(encoding="ascii").strip()
    identity = UUID(value)
    if identity.version != 4 or str(identity) != value:
        raise ValueError("Invalid CLI installation identity")
    return value


def get_installation_id() -> str:
    path = _data_dir() / "cli-installation-id"
    try:
        return _read_identity(path)
    except FileNotFoundError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish a fully written file without replacing an existing identity.
    # Concurrent first logins therefore use the same ID, and a crash cannot
    # leave a partially written identity for the next login to read.
    fd, temporary = tempfile.mkstemp(prefix=".cli-installation-id-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(str(uuid4()) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        Path(temporary).unlink(missing_ok=True)
    return _read_identity(path)
