"""Exact, reviewed authentication inputs for a single Seatbelt command.

Only names, paths and identities cross the approval boundary. Credential bytes
are never copied into configuration, approval previews or scratch files.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError
from .host_file_policy import HostFilePolicy
from .macos_network import proxy_environment


@dataclass(frozen=True)
class FileIdentity:
    path: str
    device: int
    inode: int
    size: int
    modified_ns: int

    def check(self) -> None:
        if file_identity(self.path) != self:
            raise ConfigError("The approved file changed. Request access again.")


def file_identity(value: str) -> FileIdentity:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 4096:
        raise ConfigError("Read-file grants require an exact absolute file path.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError("Read-file grants require an exact absolute file path.")
    try:
        if path.resolve(strict=True) != path:
            raise ConfigError("Read-file grants require a canonical path without symlinks.")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ConfigError("Read-file grants require a regular, non-hardlinked file.")
        HostFilePolicy.capture().guard_read(path.parent, path.name)
    except OSError as exc:
        raise ConfigError("The requested read file is unavailable.") from exc
    return FileIdentity(str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def environment_names(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or len(values) > 16:
        raise ConfigError("Request at most sixteen environment variable names.")
    reserved = {
        "HOME",
        "PATH",
        "TMP",
        "TMPDIR",
        "TEMP",
        "ENV",
        "BASH_ENV",
        "ZDOTDIR",
        "PYTHONPATH",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "SSH_AUTH_SOCK",
        *(key.upper() for key in proxy_environment("")),
    }
    result = []
    for name in values:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            raise ConfigError("Environment grants contain names only, never values.")
        key = name.upper()
        if key in reserved or key.startswith(("ALYSIS_", "DYLD_", "LD_", "GIT_CONFIG")):
            raise ConfigError(
                "This variable controls sandbox/runtime policy and cannot be forwarded."
            )
        if name not in os.environ:
            raise ConfigError(
                f"The launcher has no {name} variable. Configure it outside the agent first."
            )
        result.append(name)
    return tuple(dict.fromkeys(result))


def environment_identity(names: tuple[str, ...]) -> bytes:
    """Ephemeral change detector; never persisted or included in approval text."""
    digest = hashlib.sha256()
    for name in environment_names(names):
        digest.update(name.encode() + b"\0" + os.environ[name].encode() + b"\0")
    return digest.digest()
