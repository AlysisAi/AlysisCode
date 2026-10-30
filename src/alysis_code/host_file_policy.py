"""Immutable protected-path policy for model-facing file tools executed by the host.

Capture launcher-owned roots when tools are constructed. Direct file actions hold
native ancestor handles and admit no-follow read handles against these roots; recursive
readers and POSIX final mutations use the contained worker. Existing stale-file checks
and approval decisions remain separate requirements.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config
from .branding import env_get
from .config import ConfigError
from .host_file_handles import admit_host_path
from .host_runtime_policy import desktop_runtime_roots
from .local_memory import memory_home


class HostFilePolicyError(RuntimeError):
    pass


def _path_names(path: Path) -> tuple[Path, ...]:
    if not path.is_absolute() or "\0" in str(path):
        raise ConfigError("Protected host file paths must be absolute paths.")
    try:
        return tuple(dict.fromkeys((Path(os.path.abspath(path)), path.resolve())))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError("A protected host file path could not be resolved.") from exc


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


@dataclass(frozen=True, slots=True)
class HostFilePolicy:
    private_roots: tuple[Path, ...]
    readonly_roots: tuple[Path, ...]

    @classmethod
    def capture(cls, *, readonly_workspace: Path | None = None) -> HostFilePolicy:
        private = [config.config_path().parent, config._data_dir(), memory_home()]
        raw = env_get("ALYSIS_DESKTOP_CONTROL_PATHS")
        if raw is not None:
            try:
                if len(raw) > 131_072:
                    raise ValueError("oversized policy")
                paths = json.loads(raw)
                if (
                    not isinstance(paths, list)
                    or len(paths) > 256
                    or any(
                        not isinstance(path, str)
                        or not path
                        or "\0" in path
                        or not Path(path).is_absolute()
                        for path in paths
                    )
                ):
                    raise ValueError("invalid control paths")
                private.extend(Path(path) for path in paths)
            except (TypeError, ValueError) as exc:
                raise ConfigError("Invalid protected host file policy.") from exc
        readonly = [
            Path(prefix)
            for prefix in (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)
        ]
        readonly.extend(desktop_runtime_roots())
        readonly.append(Path(__file__).parent)
        if os.name == "nt":
            from .windows_mxc_bundle import runtime_cache_root

            readonly.append(runtime_cache_root())
        if getattr(sys, "frozen", False):
            readonly.append(Path(sys.executable))
        if readonly_workspace is not None:
            readonly.append(readonly_workspace)
        return cls(
            tuple(dict.fromkeys(name for path in private for name in _path_names(path))),
            tuple(dict.fromkeys(name for path in readonly for name in _path_names(path))),
        )

    def _targets(self, root: Path, path: str) -> tuple[Path, ...]:
        try:
            target = root / path
            if os.name == "nt" and any(":" in part for part in target.parts[1:]):
                raise HostFilePolicyError("Blocked access to a Windows alternate data stream.")
            return _path_names(target)
        except ConfigError as exc:
            raise HostFilePolicyError("Blocked access to an unresolved host file path.") from exc

    def guard_write(self, root: Path, path: str) -> None:
        admit_host_path(root / path)
        if any(
            _overlaps(target, protected)
            for target in self._targets(root, path)
            for protected in (*self.private_roots, *self.readonly_roots)
        ):
            raise HostFilePolicyError(f"Blocked write to protected host state or runtime: {path}")
        self.guard_read(root, path)

    def guard_read(self, root: Path, path: str, *, recursive: bool = False) -> None:
        admit_host_path(root / path)
        targets = self._targets(root, path)
        if any(
            target.is_relative_to(protected) or (recursive and protected.is_relative_to(target))
            for target in targets
            for protected in self.private_roots
        ):
            raise HostFilePolicyError(
                f"Blocked read of private host state: {path}. Choose a narrower ordinary project path."
            )
        # A hardlink can give private state an unrelated name without a symlink to
        # resolve. Reject multiply-linked files before previews/copies return bytes.
        for target in targets:
            try:
                metadata = target.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                raise HostFilePolicyError(f"Blocked read through a hard-linked file: {path}")
        if recursive:
            # Recursive tools do not accept an immutable exclusion callback. Check
            # aliases before dispatch rather than filtering already-read secrets.
            def unreadable(error: OSError) -> None:
                raise HostFilePolicyError(
                    "The recursive read scope could not be inspected."
                ) from error

            for current, directories, files in os.walk(
                root / path, followlinks=False, onerror=unreadable
            ):
                for name in directories:
                    directory = Path(current) / name
                    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                        raise HostFilePolicyError(
                            "Blocked recursive read through a linked directory."
                        )
                for name in files:
                    self.guard_read(root, str(Path(current) / name))

    def guard_opened_read(self, path: Path) -> None:
        """Check the actual no-follow handle name without recursively opening it."""
        if any(
            name.is_relative_to(protected)
            for name in self._targets(path.parent, path.name)
            for protected in self.private_roots
        ):
            raise HostFilePolicyError(
                "Blocked read of private host state through an opened handle."
            )
