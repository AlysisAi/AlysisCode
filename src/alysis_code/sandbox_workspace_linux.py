"""Additional Bubblewrap mounts retain real paths and per-workspace protections."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from .branding import env_get
from .config import ConfigError


def validate_workspace_tree(root: Path, *, check_preparation: Callable[[], None]) -> None:
    """Symlinks resolve inside the mount namespace; external hardlinks do not.

    Do not follow symlinks while inspecting. Their destinations must already be
    mounted, so npm's ordinary executable links need no additional authority.
    Reject hardlinks whose inode could also have an unapproved host name.
    """
    pending = [root]
    try:
        while pending:
            check_preparation()
            directory = pending.pop()
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ConfigError("Workspace changed during sandbox alias inspection.")
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_preparation()
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        raise ConfigError(
                            "Linux sandbox workspace contains hardlinked files; use copied files."
                        )
    except OSError as exc:
        raise ConfigError("Cannot inspect Linux sandbox workspace aliases.") from exc


def add_workspace_bindings(
    args: list[str],
    *,
    root: Path,
    additional: tuple[str, ...],
    read_only: bool,
    profile: str,
    protect_repo_meta: bool,
    clear_env: bool,
    allow_repo_meta_writes: bool = False,
    runtime_roots: tuple[Path, ...] = (),
    check_preparation: Callable[[], None] = lambda: None,
) -> None:
    if profile != "hardened" or not protect_repo_meta or not clear_env:
        raise ConfigError(
            "Additional Linux/WSL workspace folders require the hardened sandbox policy."
        )
    from .host_file_policy import HostFilePolicy
    from .host_runtime_policy import desktop_runtime_roots
    from .sandbox_runner import _python_runtime_ro_bind_roots
    from .skills.discovery import project_skill_root_relative_paths
    from .windows_sandbox import _PROTECTED_DIRS, _PROTECTED_FILES

    roots = list(dict.fromkeys([root, *(Path(p) for p in additional)]))
    private = list(HostFilePolicy.capture().private_roots)
    desktop_root = env_get("ALYSIS_DESKTOP_USER_DATA")
    if desktop_root:
        if not Path(desktop_root).is_absolute() or "\0" in desktop_root:
            raise ConfigError("Desktop application data must be an absolute local path.")
        if any(Path(desktop_root).resolve().is_relative_to(path) for path in roots):
            raise ConfigError(
                "A sandbox grant cannot contain the Desktop application's data folder."
            )
    protected_system = [
        Path(p)
        for p in (
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/etc",
            "/proc",
            "/sys",
            "/dev",
            "/workspace",
        )
    ]
    protected_system.extend(_python_runtime_ro_bind_roots())
    protected_system.extend(desktop_runtime_roots())
    protected_system.extend(runtime_roots)
    protected_system.append(Path(__file__).parent.resolve())
    for path in roots:
        if not path.is_absolute() or not path.is_dir() or path.resolve() != path:
            raise ConfigError(
                "Additional sandbox folders must be existing canonical absolute directories."
            )
        if path in (Path("/"), Path.home().resolve()) or any(
            part.is_symlink() for part in (path, *path.parents)
        ):
            raise ConfigError(
                "Sandbox folders cannot be filesystem roots, home directories or symbolic links."
            )
        if any(
            path.is_relative_to(p) or p.is_relative_to(path) for p in [*private, *protected_system]
        ):
            raise ConfigError(
                "Additional workspace scope overlaps private state or a trusted runtime."
            )
        validate_workspace_tree(path, check_preparation=check_preparation)
    protections = [
        (
            path,
            [path / p for p in _PROTECTED_DIRS if p != ".git" or not allow_repo_meta_writes]
            + [path.joinpath(*p) for p in project_skill_root_relative_paths()],
            [path / p for p in _PROTECTED_FILES],
        )
        for path in roots
    ]
    for path, directories, files in protections:
        # Unlike an ordinary symlink resolved by the sandbox, a bind source is
        # resolved by the host. Never import an ungranted metadata alias here.
        for source in (*directories, *files):
            if any(
                candidate.is_symlink()
                for candidate in (source, *source.parents)
                if candidate.is_relative_to(path)
            ):
                raise ConfigError("Protected sandbox metadata cannot contain symlink aliases.")
    if any(
        candidate.is_relative_to(p)
        for candidate in roots
        for _, dirs, files in protections
        for p in [*dirs, *files]
    ):
        raise ConfigError("A workspace cannot be inside another workspace's protected metadata.")
    # Every granted folder keeps its absolute path, including the primary folder.
    # /workspace remains a compatibility alias and receives the same exclusions.
    for path in sorted(roots, key=lambda p: len(p.parts)):
        args.extend(["--ro-bind" if read_only else "--bind", str(path), str(path)])
    if read_only:
        return
    # Bubblewrap creates absent mount points in its mount namespace. A fresh
    # read-only tmpfs reserves missing directories; /dev/null reserves files.
    # Existing protected sources are never replaced or made writable.
    for path, directories, files in protections:
        destinations = [path]
        if path.is_relative_to(root):
            destinations.append(Path("/workspace") / path.relative_to(root))
        directories = [
            source
            for source in dict.fromkeys(directories)
            if not any(source != parent and source.is_relative_to(parent) for parent in directories)
        ]
        for destination in destinations:
            for source in dict.fromkeys(directories):
                target = destination / source.relative_to(path)
                if source.exists():
                    args.extend(["--ro-bind", str(source), str(target)])
                else:
                    args.extend(["--tmpfs", str(target), "--remount-ro", str(target)])
            for source in files:
                args.extend(
                    [
                        "--ro-bind",
                        str(source) if source.exists() else "/dev/null",
                        str(destination / source.relative_to(path)),
                    ]
                )
