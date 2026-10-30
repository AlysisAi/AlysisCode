"""Host-owned session grants and reusable workspace permission profiles.

This is authority, not transcript replay. It lives under protected user config;
workspace files, imported chats and model-provided JSON cannot mint grants.
Only non-credential access descriptions are stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict
from pathlib import Path

from ..atomic_io import atomic_write_json
from ..config import ConfigError, config_path
from ..managed_session import normalize_managed_session_id
from ..sandbox_settings import ShellSandboxSettings


class SandboxAccessStore:
    def __init__(self, *, root: Path, session_id: str, settings: ShellSandboxSettings):
        self.root = root.resolve()
        info = self.root.stat()
        self.binding = {
            "root": str(self.root),
            "device": info.st_dev,
            "inode": info.st_ino,
            "policy": hashlib.sha256(
                json.dumps(asdict(settings), sort_keys=True).encode()
            ).hexdigest(),
        }
        self.base = config_path().parent.resolve() / "sandbox-access"
        self.directory = self.base / hashlib.sha256(str(self.root).encode()).hexdigest()
        self.session_key = hashlib.sha256(session_id.encode()).hexdigest()

    @staticmethod
    def profile_name(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise ConfigError(
                "Use a permission profile name of 1–64 letters, digits, underscores or hyphens."
            )
        normalize_managed_session_id(value)  # Reject Windows device names on every host.
        return value

    def _path(self, profile: str | None) -> Path:
        path = (
            self.directory
            / ("profiles" if profile else "sessions")
            / ((self.profile_name(profile) if profile else self.session_key) + ".json")
        )
        for parent in (path, *path.parents):
            if parent == self.base.parent:
                break
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ConfigError(
                    "Sandbox permission storage cannot contain symlinks or junctions."
                )
        return path

    def load(self, profile: str | None = None) -> dict | None:
        path = self._path(profile)
        if os.name == "nt":
            from . import _windows_access_store

            raw = _windows_access_store.read(path, self.base)
            if raw is None:
                return None
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ConfigError(
                    "Sandbox permission storage is invalid; no grant was applied."
                ) from exc
            return self._grant(data)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 65536
                or info.st_mode & 0o022
            ):
                raise ConfigError("Sandbox permission storage is not a private regular file.")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ConfigError("Sandbox permission storage belongs to another user.")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                data = json.load(stream)
        except (ValueError, UnicodeError) as exc:
            raise ConfigError(
                "Sandbox permission storage is invalid; no grant was applied."
            ) from exc
        finally:
            os.close(fd)
        return self._grant(data)

    def _grant(self, data: object) -> dict | None:
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ConfigError("Unsupported sandbox permission storage.")
        if data.get("binding") != self.binding:
            return None
        grant = data.get("grant")
        if grant is not None and not isinstance(grant, dict):
            raise ConfigError("Invalid saved sandbox grant.")
        return grant

    def save(self, grant: dict | None, profile: str | None = None) -> None:
        path = self._path(profile)
        payload = {"version": 1, "binding": self.binding, "grant": grant}
        if os.name == "nt":
            from . import _windows_access_store

            _windows_access_store.write(path, self.base, json.dumps(payload).encode("utf-8"))
            return
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._path(profile)
        # Replace with a tombstone on revocation; no unsafe recursive deletion.
        atomic_write_json(path, payload)
        os.chmod(path, 0o600)

    def profiles(self) -> dict[str, dict]:
        self._path(None)
        directory = self.directory / "profiles"
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise ConfigError("Sandbox permission storage cannot contain symlinks.")
        result = {}
        for path in directory.glob("*.json"):
            value = self.load(path.stem)
            if value is not None:
                result[path.stem] = value
        return result
