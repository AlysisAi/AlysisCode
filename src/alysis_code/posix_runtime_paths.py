"""Discover installed POSIX executables without naming languages or managers."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path


def path_runtime_paths(
    path_value: str | None,
    *,
    workspace_roots: tuple[Path, ...] = (),
    system_paths: tuple[Path, ...] = (),
    check_preparation: Callable[[], None] = lambda: None,
) -> tuple[Path, ...]:
    """Grant executable files and conventional code directories, not PATH trees.

    Caller-supplied workspace paths cannot introduce host read authority.
    Nonstandard support data still requires an explicit runtime/read scope.
    Return aliases as well as canonical targets so both names remain usable.
    """
    candidates: list[Path] = []
    system_paths = tuple(
        dict.fromkeys(alias for path in system_paths for alias in (path.absolute(), path.resolve()))
    )
    workspace_roots = tuple(path.resolve() for path in workspace_roots)
    for entry in (path_value if path_value is not None else os.defpath).split(os.pathsep):
        check_preparation()
        directory = Path(entry)
        # Relative/empty entries are command-relative workspace lookups, not
        # trusted host installations. Never resolve them against engine cwd.
        if not entry or not directory.is_absolute():
            continue
        directory = Path(os.path.abspath(directory))
        try:
            canonical = directory.resolve(strict=True)
        except (OSError, RuntimeError):
            # A stale or cyclic host PATH entry must not disable unrelated
            # tools. It confers no runtime read authority. Python <3.13 uses
            # RuntimeError for symlink loops rather than OSError.
            continue
        if any(directory.is_relative_to(p) or canonical.is_relative_to(p) for p in workspace_roots):
            continue
        try:
            executables = tuple(directory.iterdir())
        except OSError:
            continue
        directory_available = any(directory.is_relative_to(p) for p in system_paths) and any(
            canonical.is_relative_to(p) for p in system_paths
        )
        for executable in executables:
            check_preparation()
            # Only links can escape a directory already present at both names.
            # A PATH directory that is itself an external link still needs
            # every executable inspected.
            if directory_available and not executable.is_symlink():
                continue
            try:
                if not stat.S_ISREG(executable.stat().st_mode) or not os.access(
                    executable, os.X_OK
                ):
                    continue
                target = executable.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if any(target.is_relative_to(p) for p in workspace_roots):
                continue
            # A mounted PATH directory can contain executable links to an
            # installation outside that mount. Inspect their targets while
            # avoiding redundant aliases inside an already mounted tree.
            if not any(executable.is_relative_to(p) for p in system_paths):
                candidates.append(executable)
            # Preserve a host alias, but do not rediscover libraries for a
            # target already available through a system/runtime mount.
            if any(target.is_relative_to(p) for p in system_paths):
                continue
            candidates.append(target)
            if target.parent.name in {"bin", "sbin"}:
                for name in ("lib", "lib64", "libexec", "share", "include"):
                    code_path = target.parent.parent / name
                    try:
                        code_target = code_path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    # Bubblewrap needs the target mounted as well as the link;
                    # the caller validates both against its filesystem policy.
                    candidates.extend((code_path, code_target))
    return tuple(dict.fromkeys(path for path in candidates if path.exists()))
