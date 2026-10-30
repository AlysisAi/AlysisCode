from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import ntpath
import os
import platform
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from .branding import default_sandbox_docker_image
from .bwrap_etc import ensure_minimal_etc_dir
from .command_text import command_text
from .config import AppConfig, ConfigError
from .macos_sandbox import seatbelt_profile
from .process_reaping import (
    CommandCancelledError,
    CommandCleanupError,
    ProcessGroupRegistry,
    communicate_until_cancelled,
    run_in_tracked_process_group,
)
from .project_folders import allowed_roots, containing_root
from .sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings
from .service_persistence import apply_non_interactive_defaults

_SENSITIVE_ENV_KEYS = {"ALYSIS_API_KEY", "OPENAI_API_KEY"}
_LOGGER = logging.getLogger("alysis_code.sandbox_runner")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_BWRAP_UNSHARE_CGROUP_SUPPORTED: bool | None = None
_PROTECTED_REPO_META = (".git", ".alysis", ".alysis_images", "alysis-feedback")

# `protect_repo_meta` mounts `.git` read-only, which blocked `git commit` —
# the safest, most reversible git operation — with the same mechanism as
# history rewrites. A vetted allowlist of plain staging/commit commands runs
# with repo metadata writable for that single invocation; everything else
# (amend, rebase, force updates, config edits, reflog) keeps the read-only
# mount. The command must be a single simple `git` invocation: any shell
# metacharacter disqualifies it.
_SAFE_GIT_SHELL_METACHAR_RE = re.compile(r"[;&|<>`\n\r]|\$\(")
_SAFE_GIT_COMMIT_VALUE_FLAGS = {"-m", "--message", "--date"}
_SAFE_GIT_COMMIT_BARE_FLAGS = {
    "-a",
    "--all",
    "-am",
    "-q",
    "--quiet",
    "--no-verify",
    "--allow-empty",
    "--",
}
_SAFE_GIT_ADD_BARE_FLAGS = {"-A", "--all", "-u", "--update", "--"}
_SAFE_GIT_CONFIG_KEYS = {"user.name", "user.email"}


def is_safe_repo_meta_git_command(cmd: str) -> bool:
    """True for a single plain ``git add``/``git commit`` invocation.

    Deliberately conservative: unknown flags, chained commands, substitutions,
    and every other git subcommand return False and keep the read-only mount.
    """
    raw = str(cmd or "").strip()
    if not raw or _SAFE_GIT_SHELL_METACHAR_RE.search(raw):
        return False
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return False
    if not tokens or tokens[0] != "git":
        return False
    index = 1
    # Optional identity config: `-c user.name=...` / `-c user.email=...` only.
    while index + 1 < len(tokens) and tokens[index] == "-c":
        key_value = tokens[index + 1]
        key = key_value.split("=", 1)[0].strip().lower()
        if key not in _SAFE_GIT_CONFIG_KEYS:
            return False
        index += 2
    if index >= len(tokens):
        return False
    subcommand = tokens[index]
    rest = tokens[index + 1 :]
    if subcommand == "add":
        for token in rest:
            if token.startswith("-") and token not in _SAFE_GIT_ADD_BARE_FLAGS:
                return False
        return True
    if subcommand == "commit":
        position = 0
        while position < len(rest):
            token = rest[position]
            if token in _SAFE_GIT_COMMIT_VALUE_FLAGS:
                if position + 1 >= len(rest):
                    return False
                position += 2
                continue
            if any(token.startswith(f"{flag}=") for flag in _SAFE_GIT_COMMIT_VALUE_FLAGS):
                position += 1
                continue
            if token in _SAFE_GIT_COMMIT_BARE_FLAGS:
                position += 1
                continue
            if token.startswith("-"):
                return False
            position += 1
        return True
    return False


_BWRAP_SYSTEM_BIND_ROOTS = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"))


class ShellRunner(Protocol):
    # ``cancel_check`` is read while the command runs; once it returns true the
    # command and what it started are stopped and ``CommandCancelledError`` is
    # raised (a Stop, or the run's budget, ends a command that is still running).
    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def _sanitized_env(*, clear_env: bool) -> dict[str, str]:
    # Every shell backend's environment funnels through here, which makes it the
    # one place to declare that nobody is at the keyboard.
    if clear_env:
        env: dict[str, str] = {"HOME": "/tmp/home"}
        env["PATH"] = os.environ.get("PATH") or _DEFAULT_PATH
        lang = os.environ.get("LANG")
        if lang:
            env["LANG"] = lang
        return apply_non_interactive_defaults(env)
    blocked = {key.upper() for key in _SENSITIVE_ENV_KEYS}
    return apply_non_interactive_defaults(
        {k: v for k, v in os.environ.items() if k.upper() not in blocked}
    )


def _container_env(clear_env: bool, *, allowlist: tuple[str, ...] = ()) -> dict[str, str]:
    if clear_env:
        env: dict[str, str] = {"HOME": "/tmp/home"}
        lang = os.environ.get("LANG")
        if lang:
            env["LANG"] = lang
        return env
    env = _sanitized_env(clear_env=False)
    blocked = {"PATH", "HOME"}
    filtered = {k: v for k, v in env.items() if k.upper() not in blocked}
    if allowlist:
        allowed = {item.upper() for item in allowlist}
        allowed.add("LANG")
        filtered = {k: v for k, v in filtered.items() if k.upper() in allowed}
    filtered["HOME"] = "/tmp/home"
    return filtered


def _workspace_cwd(*, root: Path, cwd: Path) -> str:
    root_abs = root.resolve()
    cwd_abs = cwd.resolve()
    try:
        rel = cwd_abs.relative_to(root_abs)
    except ValueError as e:
        try:
            containing_root(root_abs, cwd_abs)
            return os.fspath(cwd_abs)
        except ValueError:
            raise RuntimeError(f"cwd is outside root: {cwd_abs}") from e
    if not rel.parts:
        return "/workspace"
    return os.fspath(PurePosixPath("/workspace", *rel.parts))


def _protected_repo_paths(root: Path) -> list[tuple[Path, str]]:
    root_abs = root.resolve()
    protected: list[tuple[Path, str]] = []
    for rel in _PROTECTED_REPO_META:
        candidate = root_abs / rel
        if not candidate.exists():
            continue
        # Never follow symlinked metadata roots to avoid binding host paths.
        if candidate.is_symlink():
            continue
        host_path = candidate.resolve()
        try:
            host_path.relative_to(root_abs)
        except ValueError:
            continue
        protected.append((host_path, rel))
    return protected


def _path_is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _path_is_inside_system_root(path: Path) -> bool:
    return _path_is_under_any(path, _BWRAP_SYSTEM_BIND_ROOTS)


def _path_is_lexically_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    candidate = path if path.is_absolute() else path.absolute()
    for root in roots:
        root_abs = root if root.is_absolute() else root.absolute()
        try:
            candidate.relative_to(root_abs)
            return True
        except ValueError:
            continue
    return False


def _dedupe_existing_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    roots: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.exists() or _path_is_inside_system_root(resolved):
            continue
        if _path_is_under_any(resolved, tuple(roots)):
            continue
        key = os.fspath(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return tuple(roots)


def _executable_symlink_mounts(executable: Path) -> tuple[tuple[Path, Path], ...]:
    mounts: list[tuple[Path, Path]] = []
    seen: set[str] = set()

    def add_mount(host_path: Path, dest_path: Path) -> None:
        try:
            host_resolved = host_path.resolve()
        except OSError:
            return
        if not host_resolved.exists() or _path_is_inside_system_root(host_resolved):
            return
        key = f"{os.fspath(host_resolved)}\0{dest_path.as_posix()}"
        if key in seen:
            return
        seen.add(key)
        mounts.append((host_resolved, dest_path))

    current = executable
    for _ in range(16):
        try:
            if not current.is_symlink():
                break
            target_raw = os.readlink(current)
        except OSError:
            break
        target = Path(target_raw)
        if not target.is_absolute():
            target = current.parent / target
        if not _path_is_lexically_under_any(target, (Path(sys.prefix), Path(sys.exec_prefix))):
            add_mount(target, target)
        current = target
    return tuple(mounts)


def _executable_symlink_parent_roots(executable: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[str] = set()

    def add_root(path: Path) -> None:
        key = os.fspath(path)
        if key in seen:
            return
        seen.add(key)
        roots.append(path)

    def add_symlink_ancestor_parent_roots(path: Path) -> None:
        current = Path(path.anchor or "/")
        for part in path.parts[1:-1]:
            current = current / part
            try:
                is_link = current.is_symlink()
            except OSError:
                continue
            if is_link:
                add_root(current.parent)

    current = executable
    for _ in range(16):
        try:
            if not current.is_symlink():
                break
            target_raw = os.readlink(current)
        except OSError:
            break
        target = Path(target_raw)
        if not target.is_absolute():
            target = current.parent / target
        add_root(target.parent)
        add_symlink_ancestor_parent_roots(target)
        current = target
    return tuple(roots)


def _python_runtime_ro_bind_roots() -> tuple[Path, ...]:
    executable = Path(sys.executable)
    candidates = _dedupe_existing_paths(
        [
            Path(sys.prefix),
            Path(sys.exec_prefix),
            Path(sys.base_prefix),
            Path(sys.base_exec_prefix),
            executable.resolve().parent,
            # Editable installs keep engine code outside the interpreter prefix.
            # Bind only the trusted package, not its checkout or neighboring files.
            Path(__file__).resolve().parent,
            *_executable_symlink_parent_roots(executable),
        ]
    )
    return candidates


def _add_bwrap_dir(
    args: list[str],
    dest: PurePosixPath,
    *,
    created_dirs: set[str],
) -> None:
    dest_s = dest.as_posix()
    if dest_s in {"", ".", "/"} or dest.parent == dest or dest_s in created_dirs:
        return
    _add_bwrap_dir(args, dest.parent, created_dirs=created_dirs)
    args.extend(["--dir", dest_s])
    created_dirs.add(dest_s)


def _add_bwrap_ro_bind(
    args: list[str],
    host_path: Path,
    dest_path: Path,
    *,
    created_dirs: set[str],
    bound_dests: set[str],
    resolve_dest: bool = True,
) -> None:
    host_resolved = host_path.resolve()
    if not host_resolved.exists():
        return
    dest = PurePosixPath((dest_path.resolve() if resolve_dest else dest_path.absolute()).as_posix())
    dest_s = dest.as_posix()
    if dest_s in bound_dests:
        return
    _add_bwrap_dir(args, dest.parent, created_dirs=created_dirs)
    args.extend(["--ro-bind", os.fspath(host_resolved), dest_s])
    bound_dests.add(dest_s)


def _add_python_runtime_bindings(args: list[str], *, created_dirs: set[str]) -> None:
    bound_dests: set[str] = set()
    for root in _python_runtime_ro_bind_roots():
        _add_bwrap_ro_bind(
            args,
            root,
            root,
            created_dirs=created_dirs,
            bound_dests=bound_dests,
        )


def _add_host_runtime_bindings(
    args: list[str],
    *,
    path_value: str | None,
    workspace_roots: tuple[Path, ...],
    created_dirs: set[str],
    check_preparation: Callable[[], None],
) -> tuple[Path, ...]:
    """Use the same executable discovery as Seatbelt, plus explicit runtime data."""
    from .host_file_policy import HostFilePolicy
    from .host_runtime_policy import desktop_runtime_roots
    from .posix_runtime_paths import path_runtime_paths

    policy = HostFilePolicy.capture()
    home = Path.home().resolve()
    bound_dests: set[str] = set()
    mounted_directories: set[Path] = set()
    available_roots = (*_BWRAP_SYSTEM_BIND_ROOTS, *_python_runtime_ro_bind_roots())
    discovered = path_runtime_paths(
        path_value,
        workspace_roots=workspace_roots,
        system_paths=available_roots,
        check_preparation=check_preparation,
    )
    paths = tuple(dict.fromkeys((*desktop_runtime_roots(), *discovered)))
    inspected_roots: set[Path] = set()
    for path in paths:
        check_preparation()
        root = path.resolve()
        if home.is_relative_to(root):
            raise ConfigError("Runtime access requires a narrow installation directory.")
        if any(
            root.is_relative_to(private) or private.is_relative_to(root)
            for private in policy.private_roots
        ):
            raise ConfigError("A sandbox runtime scope cannot overlap private host state.")
        if any(root.is_relative_to(p) or p.is_relative_to(root) for p in workspace_roots):
            raise ConfigError("A sandbox workspace cannot overlap a trusted runtime.")
        # A hardlink can expose an unapproved file under a runtime's otherwise
        # ordinary name. Do not follow symlinks: their targets still need their
        # own namespace mount. Trusted runtime trees must use copied files.
        pending = []
        inspected = 0
        already_available = any(root.is_relative_to(p) for p in available_roots)
        try:
            if not already_available and root.exists() and root not in inspected_roots:
                info = root.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise ConfigError("Runtime executable is hardlinked to another file.")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(root)
                inspected_roots.add(root)
            while pending:
                check_preparation()
                directory = pending.pop()
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    raise ConfigError("Runtime directory changed during sandbox preparation.")
                with os.scandir(directory) as entries:
                    for entry in entries:
                        check_preparation()
                        inspected += 1
                        if inspected > 200_000:
                            raise ConfigError(
                                "Runtime scope is too large; choose a narrower installation."
                            )
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                            raise ConfigError(
                                "Runtime scope contains hardlinked files; install with copy semantics."
                            )
        except OSError as exc:
            raise ConfigError("Cannot inspect the sandbox runtime scope.") from exc
        if any(path.is_relative_to(directory) for directory in mounted_directories):
            continue
        if path != root:
            # Preserve executable identity and self-relative library lookup.
            # Mounting the target file at its alias would turn the symlink into
            # a different executable path (for example Node's process.execPath).
            _add_bwrap_dir(args, PurePosixPath(path.parent), created_dirs=created_dirs)
            args.extend(["--symlink", str(root), str(path)])
            bound_dests.add(str(path))
            continue
        if already_available:
            continue
        _add_bwrap_ro_bind(
            args,
            root,
            path,
            created_dirs=created_dirs,
            bound_dests=bound_dests,
            resolve_dest=False,
        )
        if root.is_dir():
            mounted_directories.add(path)
    return tuple(dict.fromkeys(path.resolve() for path in paths))


def _hardened_java_config_ro_bind_paths(*, etc_root: Path = Path("/etc")) -> tuple[Path, ...]:
    paths: list[Path] = []
    seen: set[str] = set()
    try:
        candidates = [*etc_root.glob("java-*"), etc_root / ".java"]
    except OSError:
        candidates = [etc_root / ".java"]
    for candidate in sorted(candidates, key=os.fspath):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_dir():
            continue
        resolved_key = os.fspath(resolved)
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        paths.append(candidate)
    return tuple(paths)


def _add_hardened_java_config_bindings(args: list[str]) -> None:
    for path in _hardened_java_config_ro_bind_paths():
        # bwrap consumes Linux namespace paths even when this pure argv builder
        # is exercised by the Windows test harness.
        path_s = path.as_posix()
        args.extend(["--ro-bind", path_s, path_s])


def _bwrap_path_value(path_value: str | None) -> str:
    entries: list[str] = []
    for raw in (path_value or _DEFAULT_PATH).split(os.pathsep):
        cleaned = raw.strip()
        if cleaned and cleaned not in entries:
            entries.append(cleaned)
    return os.pathsep.join(entries)


def _host_verification_launcher_unavailable(command: str, cwd: Path) -> bool:
    """Observe a missing direct verifier executable before the host shell runs."""
    from .verification_command_analysis import analyze_verification_command

    if os.name != "posix" or os.environ.get("BASH_ENV") or os.environ.get("ENV"):
        return False
    analysis = analyze_verification_command(command, trusted=True, workspace_root=cwd)
    # Wrappers, assignments and shell functions need their own environment;
    # a host PATH lookup cannot establish whether those launched a checker.
    if not (
        analysis.is_valid_verifier
        and analysis.parts
        and analysis.shell_control_flow == "none"
        and analysis.unwrapped_command == analysis.normalized_command
        and not any(key.startswith("BASH_FUNC_") for key in os.environ)
    ):
        return False
    head = shlex.split(command)[0]
    if head == "command" or any(character in head for character in "$~*?[]=\\"):
        return False
    search_path = os.pathsep.join(
        str(cwd / part) if not Path(part).is_absolute() else part for part in os.get_exec_path()
    )
    executable = str(cwd / head) if "/" in head and not Path(head).is_absolute() else head
    return shutil.which(executable, path=search_path) is None


@dataclass(frozen=True)
class HostShellRunner:
    additional_workspace_roots: tuple[str, ...] = ()
    process_group_registry: ProcessGroupRegistry | None = None
    close_stdin: bool = False
    # True when this runner stands in for a sandbox that is not available here: the shell
    # guards then ask the user to approve each command (see select_shell_sandbox_backend).
    requires_command_approval: bool = False

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        launcher_unavailable = _host_verification_launcher_unavailable(cmd, cwd)
        result = run_in_tracked_process_group(
            cmd,
            shell=True,
            cwd=str(cwd),
            timeout=timeout_s,
            registry=self.process_group_registry,
            origin="shell_run:host",
            command_label=cmd,
            stdin=subprocess.DEVNULL if self.close_stdin else None,
            cancel_check=cancel_check,
        )

        if launcher_unavailable and result.returncode == 127:
            result.host_launcher_unavailable = True
        return result


@dataclass(frozen=True)
class DisabledShellRunner:
    reason: str = "Shell execution is disabled."

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        raise RuntimeError(self.reason)


class LazyShellRunner:
    def __init__(
        self,
        loader: Callable[[], ShellRunner],
        *,
        settings: ShellSandboxSettings | None = None,
    ) -> None:
        self._loader = loader
        self._runner: ShellRunner | None = None
        self._load_error: Exception | None = None
        self._settings = settings

    @property
    def execution_settings(self) -> ShellSandboxSettings | None:
        """Settings captured by the loader, unaffected by later config edits."""
        return self._settings

    @property
    def resolved_runner(self) -> ShellRunner | None:
        """Inspect an already-loaded runner without initializing a helper."""
        return self._runner

    def _resolve_runner(self) -> ShellRunner:
        if self._runner is not None:
            return self._runner
        if self._load_error is not None:
            raise self._load_error
        try:
            runner = self._loader()
        except Exception as exc:
            self._load_error = exc
            raise
        self._runner = runner
        return runner

    @property
    def requires_command_approval(self) -> bool:
        try:
            runner = self._resolve_runner()
        except Exception:  # noqa: BLE001 - the same error is raised when a command runs
            return False
        return bool(getattr(runner, "requires_command_approval", False))

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runner = self._resolve_runner()
        if cancel_check is None:
            return runner.run(root=root, cwd=cwd, cmd=cmd, timeout_s=timeout_s)
        return runner.run(
            root=root, cwd=cwd, cmd=cmd, timeout_s=timeout_s, cancel_check=cancel_check
        )


def _validate_bwrap_primary_host_scope(root: Path, *, filesystem_access: str) -> None:
    """The primary /workspace alias must preserve the host file-tool boundary."""
    from .branding import env_get
    from .host_file_policy import HostFilePolicy

    policy = HostFilePolicy.capture()
    if any(
        root.is_relative_to(private) or private.is_relative_to(root)
        for private in policy.private_roots
    ):
        raise ConfigError("The primary sandbox workspace overlaps private host state.")
    desktop_root = env_get("ALYSIS_DESKTOP_USER_DATA")
    if desktop_root:
        desktop = Path(desktop_root)
        if not desktop.is_absolute() or "\0" in desktop_root:
            raise ConfigError("Desktop application data must be an absolute local path.")
        if desktop.resolve().is_relative_to(root):
            raise ConfigError(
                "A sandbox grant cannot contain the Desktop application's data folder."
            )
    if filesystem_access == "workspace-write" and any(
        root.is_relative_to(runtime) or runtime.is_relative_to(root)
        for runtime in (*policy.readonly_roots, Path(__file__).parent.resolve())
    ):
        # A read-only mount at the runtime's original name leaves the /workspace
        # alias writable. Refuse overlapping writes rather than rely on pathname
        # checks before a concurrently mutable worker operation.
        raise ConfigError("The primary sandbox workspace overlaps a trusted runtime.")


def _build_bwrap_argv(
    *,
    root: Path,
    cwd: Path,
    cmd: str,
    network: str,
    clear_env: bool,
    profile: str,
    unshare_cgroup: bool,
    protect_repo_meta: bool = True,
    allow_repo_meta_writes: bool = False,
    filesystem_access: str = "workspace-write",
    additional_workspace_roots: tuple[str, ...] = (),
    check_preparation: Callable[[], None] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build bwrap argv + parent-process env without starting subprocesses."""

    if check_preparation is None:
        deadline = time.monotonic() + 30

        def check_preparation() -> None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    cmd, 30, stderr="Sandbox preparation timed out before launch."
                )

    check_preparation()

    root_abs = root.resolve()
    additional_workspace_roots = tuple(
        dict.fromkeys((*additional_workspace_roots, *map(os.fspath, allowed_roots(root)[1:])))
    )
    _validate_filesystem_access(filesystem_access)
    # This applies even without additional roots or a Desktop launcher. Otherwise
    # a contained file worker can follow a post-admission alias into private state
    # already present beneath the primary mount.
    _validate_bwrap_primary_host_scope(root_abs, filesystem_access=filesystem_access)
    workdir = (
        _workspace_cwd(root=root_abs, cwd=cwd)
        if cwd.resolve().is_relative_to(root_abs)
        else os.fspath(cwd.resolve())
    )
    from .sandbox_workspace import contains_workspace_path

    if not contains_workspace_path(root_abs, cwd, additional_workspace_roots):
        raise ConfigError("cwd is outside root and approved additional sandbox workspaces")
    run_env = _sanitized_env(clear_env=clear_env)
    path_value = _bwrap_path_value(run_env.get("PATH"))
    args: list[str] = ["bwrap", "--die-with-parent"]
    created_dirs: set[str] = set()
    if clear_env:
        args.append("--clearenv")
    if profile == "hardened":
        args.extend(["--unshare-pid", "--unshare-ipc", "--unshare-uts"])
        if unshare_cgroup:
            args.append("--unshare-cgroup")
    else:
        args.append("--unshare-pid")

    workspace_bind = "--ro-bind" if filesystem_access == "read-only" else "--bind"
    args.extend([workspace_bind, os.fspath(root_abs), "/workspace"])
    # Repo-meta protection now honors the protect_repo_meta setting (it was
    # previously welded to the hardened profile, making the config knob dead on
    # the bwrap backend), and a vetted safe git write skips the read-only
    # mounts for its single invocation.
    if profile == "hardened" and protect_repo_meta and not allow_repo_meta_writes:
        for host_path, rel_posix_path in _protected_repo_paths(root_abs):
            args.extend(
                [
                    "--ro-bind",
                    os.fspath(host_path),
                    f"/workspace/{rel_posix_path}",
                ]
            )
    if profile == "hardened":
        for sys_dir in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(sys_dir).exists():
                args.extend(["--ro-bind", sys_dir, sys_dir])
        etc_dir = ensure_minimal_etc_dir(network=network)
        args.extend(
            [
                "--dir",
                "/etc",
                "--ro-bind",
                os.fspath(etc_dir / "passwd"),
                "/etc/passwd",
                "--ro-bind",
                os.fspath(etc_dir / "group"),
                "/etc/group",
                "--ro-bind",
                os.fspath(etc_dir / "nsswitch.conf"),
                "/etc/nsswitch.conf",
                "--ro-bind",
                os.fspath(etc_dir / "hosts"),
                "/etc/hosts",
                "--ro-bind",
                os.fspath(etc_dir / "resolv.conf"),
                "/etc/resolv.conf",
            ]
        )
        if Path("/etc/alternatives").exists():
            args.extend(["--ro-bind", "/etc/alternatives", "/etc/alternatives"])
        _add_hardened_java_config_bindings(args)
        _add_hardened_tls_bindings(args)
    else:
        for sys_dir in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
            if Path(sys_dir).exists():
                args.extend(["--ro-bind", sys_dir, sys_dir])

    args.extend(
        [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/home",
        ]
    )
    created_dirs.update({"/proc", "/dev", "/tmp", "/tmp/home"})
    _add_python_runtime_bindings(args, created_dirs=created_dirs)
    runtime_roots = _add_host_runtime_bindings(
        args,
        path_value=path_value,
        workspace_roots=(root_abs, *(Path(p) for p in additional_workspace_roots)),
        created_dirs=created_dirs,
        check_preparation=check_preparation,
    )
    from .host_runtime_policy import desktop_runtime_roots

    if additional_workspace_roots or desktop_runtime_roots():
        from .sandbox_workspace_linux import add_workspace_bindings

        add_workspace_bindings(
            args,
            root=root_abs,
            additional=additional_workspace_roots,
            read_only=filesystem_access == "read-only",
            profile=profile,
            protect_repo_meta=protect_repo_meta,
            clear_env=clear_env,
            allow_repo_meta_writes=allow_repo_meta_writes,
            runtime_roots=runtime_roots,
            check_preparation=check_preparation,
        )
    else:
        from .sandbox_workspace_linux import validate_workspace_tree

        validate_workspace_tree(root_abs, check_preparation=check_preparation)

    args.extend(
        [
            "--chdir",
            workdir,
            "--setenv",
            "HOME",
            "/tmp/home",
        ]
    )
    if path_value:
        args.extend(["--setenv", "PATH", path_value])
    lang_value = run_env.get("LANG")
    if lang_value:
        args.extend(["--setenv", "LANG", lang_value])
    if network == "off":
        args.append("--unshare-net")
    args.extend(["sh", "-lc", cmd])
    check_preparation()
    return args, run_env


@dataclass(frozen=True)
class BwrapShellRunner:
    network: str = "off"
    clear_env: bool = True
    profile: str = "hardened"
    protect_repo_meta: bool = True
    safe_git_writes: bool = True
    process_group_registry: ProcessGroupRegistry | None = None
    close_stdin: bool = False
    filesystem_access: str = "workspace-write"
    additional_workspace_roots: tuple[str, ...] = ()

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        deadline = time.monotonic() + timeout_s

        def check_preparation() -> None:
            if cancel_check is not None and cancel_check():
                raise CommandCancelledError(cmd)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    cmd, timeout_s, stderr="Sandbox preparation timed out before launch."
                )

        allow_repo_meta_writes = bool(
            self.protect_repo_meta and self.safe_git_writes and is_safe_repo_meta_git_command(cmd)
        )
        args, run_env = _build_bwrap_argv(
            root=root,
            cwd=cwd,
            cmd=cmd,
            network=self.network,
            clear_env=self.clear_env,
            profile=self.profile,
            unshare_cgroup=_supports_bwrap_unshare_cgroup(),
            protect_repo_meta=self.protect_repo_meta,
            allow_repo_meta_writes=allow_repo_meta_writes,
            filesystem_access=self.filesystem_access,
            additional_workspace_roots=self.additional_workspace_roots,
            check_preparation=check_preparation,
        )
        return run_in_tracked_process_group(
            args,
            cwd=os.fspath(root.resolve()),
            env=run_env,
            timeout=max(0, deadline - time.monotonic()),
            registry=self.process_group_registry,
            origin="shell_run:bwrap",
            command_label=cmd,
            stdin=subprocess.DEVNULL if self.close_stdin else None,
            cancel_check=cancel_check,
        )


def _supports_bwrap_unshare_cgroup() -> bool:
    global _BWRAP_UNSHARE_CGROUP_SUPPORTED
    if _BWRAP_UNSHARE_CGROUP_SUPPORTED is not None:
        return _BWRAP_UNSHARE_CGROUP_SUPPORTED
    try:
        cp = subprocess.run(
            ["bwrap", "--help"],
            check=False,
            capture_output=True,
            **command_text(),
        )
    except OSError:
        _BWRAP_UNSHARE_CGROUP_SUPPORTED = False
        return False
    help_text = (cp.stdout or "") + (cp.stderr or "")
    _BWRAP_UNSHARE_CGROUP_SUPPORTED = cp.returncode == 0 and "--unshare-cgroup" in help_text
    return _BWRAP_UNSHARE_CGROUP_SUPPORTED


def _add_hardened_tls_bindings(args: list[str]) -> None:
    args.extend(["--dir", "/etc/ssl", "--dir", "/etc/ssl/certs"])
    if Path("/etc/ssl/certs").exists():
        args.extend(["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"])

    args.extend(["--dir", "/etc/pki", "--dir", "/etc/pki/tls", "--dir", "/etc/pki/tls/certs"])
    if Path("/etc/pki/tls/certs").exists():
        args.extend(["--ro-bind", "/etc/pki/tls/certs", "/etc/pki/tls/certs"])

    args.extend(["--dir", "/etc/ca-certificates"])
    if Path("/etc/ca-certificates").exists():
        args.extend(["--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates"])


def _docker_policy_env(env: dict[str, str], filesystem_access: str) -> dict[str, str]:
    if filesystem_access != "read-only":
        return env
    # ReadOnlyForceRecursive first appeared in API 1.44. Negotiating an older
    # API can silently ignore the field, so require this known protocol version
    # for every read-only launch. An incompatible daemon must reject it.
    # Remove case aliases too: Windows environment keys are case-insensitive.
    return {
        **{key: value for key, value in env.items() if key.upper() != "DOCKER_API_VERSION"},
        "DOCKER_API_VERSION": "1.44",
    }


def _build_docker_argv(
    *,
    root: Path,
    cwd: Path,
    cmd: str,
    container_name: str,
    network: str,
    docker_image: str,
    clear_env: bool,
    pids_limit: int | None,
    memory_limit: str | None,
    cpus: str | None,
    read_only_rootfs: bool,
    protect_repo_meta: bool,
    env_allowlist: tuple[str, ...],
    published_ports: tuple[tuple[str, int, int], ...] = (),
    allow_repo_meta_writes: bool = False,
    filesystem_access: str = "workspace-write",
) -> tuple[list[str], dict[str, str]]:
    """Build docker run argv + parent-process env. Pure function."""

    root_abs = root.resolve()
    workdir = _workspace_cwd(root=root_abs, cwd=cwd)
    run_env = _docker_policy_env(_sanitized_env(clear_env=clear_env), filesystem_access)
    container_env = _container_env(clear_env, allowlist=env_allowlist)

    _validate_filesystem_access(filesystem_access)
    args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
    ]
    mounts = [
        (root_abs, "/workspace"),
        *[(path, os.fspath(path)) for path in allowed_roots(root)[1:]],
    ]
    for source_path, destination in mounts:
        if not destination.startswith("/"):
            raise ConfigError(
                "Additional Docker folders require POSIX paths; use the native Windows sandbox."
            )
        if filesystem_access == "read-only":
            # Require recursive read-only support for every attached root.
            source = os.fspath(source_path)
            if any(char in source + destination for char in ("\r", "\n", "\0")):
                raise ConfigError(
                    "Read-only Docker workspace path contains unsupported control characters."
                )
            source_field = '"source=' + source.replace('"', '""') + '"'
            target_field = '"target=' + destination.replace('"', '""') + '"'
            if not any(char in destination for char in (",", '"')):
                target_field = "target=" + destination
            args.extend(
                [
                    "--mount",
                    f"type=bind,{source_field},{target_field},readonly,"
                    "bind-recursive=readonly,bind-propagation=rprivate",
                ]
            )
        else:
            args.extend(["-v", f"{os.fspath(source_path)}:{destination}:rw"])
        if filesystem_access != "read-only" and (
            (protect_repo_meta and not allow_repo_meta_writes) or read_only_rootfs
        ):
            for host_path, rel_posix_path in _protected_repo_paths(source_path):
                args.extend(["-v", f"{os.fspath(host_path)}:{destination}/{rel_posix_path}:ro"])
    args.extend(["-w", workdir])
    if pids_limit is not None:
        args.extend(["--pids-limit", str(pids_limit)])
    if memory_limit is not None:
        args.extend(["--memory", memory_limit])
    if cpus is not None:
        args.extend(["--cpus", cpus])
    if read_only_rootfs:
        args.extend(["--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev"])
    for host, host_port, container_port in published_ports:
        try:
            publish_address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Docker published-port host must be an IP address") from exc
        if not publish_address.is_loopback:
            raise ValueError("Docker published ports must bind to a loopback address")
        if not (0 < host_port <= 65535 and 0 < container_port <= 65535):
            raise ValueError("Docker published ports must be between 1 and 65535")
        formatted_host = f"[{host}]" if ":" in host else host
        args.extend(["--publish", f"{formatted_host}:{host_port}:{container_port}/tcp"])
    if network == "off":
        args.extend(["--network", "none"])
    if os.name == "posix" and hasattr(os, "getuid") and hasattr(os, "getgid"):
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    args.extend(["--cap-drop=ALL", "--security-opt", "no-new-privileges"])
    for key, value in container_env.items():
        args.extend(["-e", f"{key}={value}"])
    args.extend([docker_image, "sh", "-lc", f"mkdir -p /tmp/home && {cmd}"])
    return args, run_env


def _docker_cleanup_container(
    container_name: str,
    *,
    cwd: str,
    env: dict[str, str],
    warning_callback: Callable[[str], None] | None,
    reason: str,
    quiet: bool = False,
) -> None:
    """Best-effort forced cleanup of a docker container by name."""

    if not quiet:
        _emit_warning(
            (f"Docker shell sandbox {container_name} cleanup after {reason}; killing container."),
            warning_callback=warning_callback,
        )
    for cleanup_args in (
        ["docker", "kill", "--signal=KILL", container_name],
        ["docker", "rm", "-f", container_name],
    ):
        try:
            subprocess.run(
                cleanup_args,
                check=False,
                capture_output=True,
                **command_text(),
                timeout=5,
                cwd=cwd,
                env=env,
            )
        except Exception:  # noqa: BLE001
            # Docker cleanup is best-effort; callers must not fail while unwinding.
            _LOGGER.exception("Docker cleanup command failed for container %s", container_name)


@dataclass(frozen=True)
class DockerShellRunner:
    network: str = "off"
    docker_image: str = default_sandbox_docker_image("dev")
    clear_env: bool = True
    pids_limit: int | None = None
    memory_limit: str | None = None
    cpus: str | None = None
    read_only_rootfs: bool = False
    protect_repo_meta: bool = True
    safe_git_writes: bool = True
    env_allowlist: tuple[str, ...] = ()
    warning_callback: Callable[[str], None] | None = None
    close_stdin: bool = False
    filesystem_access: str = "workspace-write"

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        root_abs = root.resolve()
        container_name = f"alysis-sbx-{uuid.uuid4().hex[:12]}"
        allow_repo_meta_writes = bool(
            self.protect_repo_meta
            and self.safe_git_writes
            and not self.read_only_rootfs
            and is_safe_repo_meta_git_command(cmd)
        )
        args, run_env = _build_docker_argv(
            root=root_abs,
            cwd=cwd,
            cmd=cmd,
            allow_repo_meta_writes=allow_repo_meta_writes,
            container_name=container_name,
            network=self.network,
            docker_image=self.docker_image,
            clear_env=self.clear_env,
            pids_limit=self.pids_limit,
            memory_limit=self.memory_limit,
            cpus=self.cpus,
            read_only_rootfs=self.read_only_rootfs,
            protect_repo_meta=self.protect_repo_meta,
            env_allowlist=self.env_allowlist,
            filesystem_access=self.filesystem_access,
        )
        proc: subprocess.Popen[str] | None = None
        container_killed = False

        def cleanup_container(reason: str) -> None:
            nonlocal container_killed
            if container_killed:
                return
            container_killed = True
            _docker_cleanup_container(
                container_name,
                cwd=os.fspath(root_abs),
                env=run_env,
                warning_callback=self.warning_callback,
                reason=reason,
            )

        def kill_docker_cli() -> None:
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **command_text(),
                cwd=os.fspath(root_abs),
                env=run_env,
                **({"stdin": subprocess.DEVNULL} if self.close_stdin else {}),
            )
            if cancel_check is None:
                stdout, stderr = proc.communicate(timeout=timeout_s)
            else:
                stdout, stderr = communicate_until_cancelled(
                    proc, timeout=timeout_s, cancel_check=cancel_check
                )
            return subprocess.CompletedProcess(
                args=args,
                returncode=proc.returncode if proc.returncode is not None else 1,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            cleanup_container("timeout")
            kill_docker_cli()
            raise
        except CommandCancelledError:
            cleanup_container("cancel")
            kill_docker_cli()
            raise
        except BaseException:
            if proc is not None:
                cleanup_container("exception")
                kill_docker_cli()
            raise
        finally:
            if proc is not None and proc.poll() is None:
                cleanup_container("unfinished process")
                kill_docker_cli()


SEATBELT_EXECUTABLE = "/usr/bin/sandbox-exec"


def _validate_filesystem_access(value: str) -> None:
    if value not in {"read-only", "workspace-write"}:
        raise ConfigError(f"Unsupported sandbox filesystem policy: {value!r}")


@dataclass(frozen=True)
class SandboxBackendChoice:
    """Where shell commands run under some sandbox settings, decided in one place.

    ``backend`` is "host", "bwrap", "docker", "seatbelt", "windows-native", "windows-mxc", or None
    when the settings ask for a sandbox that this machine cannot provide. The native
    backend validates its helper in the runner builders and always fails closed.
    """

    backend: str | None
    unavailable_reason: str = ""
    requires_command_approval: bool = False


def select_shell_sandbox_backend(
    settings: ShellSandboxSettings,
    *,
    system: str | None = None,
    which: Callable[[str], str | None] | None = None,
    path_exists: Callable[[str], bool] | None = None,
) -> SandboxBackendChoice:
    """Pick the backend for ``settings``; the shell and background builders and the shell
    guards all use this, so they cannot disagree.

    ``auto`` prefers native Seatbelt on macOS and bubblewrap, then Docker, on Linux. When no
    backend is usable and ``unavailable_fallback`` is "ask", commands run on the host and
    each one needs the user's approval; with "refuse" nothing runs.
    Explicit Windows backends never fall back; their builders validate platform and helper.
    """
    _validate_filesystem_access(settings.filesystem_access)
    if settings.github_auth:
        current = (system if system is not None else platform.system()).lower()
        if (
            settings.mode != "strict"
            or not (
                (current == "windows" and settings.backend in {"auto", "windows-mxc"})
                or (current == "darwin" and settings.backend in {"auto", "seatbelt"})
            )
            or settings.network != "on"
            or not settings.clear_env
            or not settings.protect_repo_meta
            or not settings.safe_git_writes
        ):
            raise ConfigError(
                "GitHub authentication requires strict Windows MXC or macOS Seatbelt with networking and environment/metadata isolation; no host fallback is allowed."
            )
    if settings.additional_read_files:
        current = (system if system is not None else platform.system()).lower()
        supported = (current == "windows" and settings.backend in {"auto", "windows-mxc"}) or (
            current == "darwin" and settings.backend in {"auto", "seatbelt"}
        )
        if (
            settings.mode != "strict"
            or not supported
            or not settings.clear_env
            or not settings.protect_repo_meta
        ):
            raise ConfigError(
                "Exact read-file permissions require strict Windows MXC or macOS Seatbelt; "
                "no host fallback is allowed."
            )
    if (
        settings.network_domains is not None
        or settings.private_network_endpoints
        or settings.unix_sockets
        or settings.forward_env
        or settings.ssh_agent_socket
        or settings.git_metadata_access
    ):
        current = (system if system is not None else platform.system()).lower()
        if (
            settings.mode != "strict"
            or settings.backend not in {"auto", "seatbelt"}
            or current != "darwin"
        ):
            raise ConfigError(
                "Scoped network, authentication and Git permissions require strict macOS Seatbelt; no host fallback is allowed."
            )
    if settings.mode == "off":
        return SandboxBackendChoice("host")
    from .host_runtime_policy import desktop_runtime_roots

    extended_roots = bool(
        settings.additional_workspace_roots
        or settings.additional_read_roots
        or settings.network_domains is not None
        or settings.private_network_endpoints
        or settings.unix_sockets
        or settings.additional_read_files
        or settings.forward_env
        or settings.ssh_agent_socket
        or settings.git_metadata_access
        or desktop_runtime_roots()
    )
    if extended_roots and (not settings.clear_env or not settings.protect_repo_meta):
        raise ConfigError(
            "Additional workspace folders require environment isolation and metadata protection."
        )
    if settings.backend in {"windows-native", "windows-mxc"}:
        # Keep explicit native containment even when general host fallback is enabled.
        # Helper validation belongs in the builders, not approval-policy inspection.
        return SandboxBackendChoice(settings.backend)
    # Looked up at call time, so a patched shutil.which or os.path.exists applies.
    which = which or shutil.which
    path_exists = path_exists or os.path.exists
    current = (system if system is not None else platform.system()).lower()
    if settings.backend == "auto" and current == "windows":
        return SandboxBackendChoice("windows-mxc")
    available = {
        "bwrap": current == "linux" and which("bwrap") is not None,
        "docker": which("docker") is not None,
        "seatbelt": current == "darwin" and path_exists(SEATBELT_EXECUTABLE),
    }
    if settings.backend == "auto":
        backend = next(
            (
                b
                for b in (("seatbelt", "docker") if current == "darwin" else ("bwrap", "docker"))
                if available[b]
            ),
            None,
        )
        reason = "auto backend could not find a supported sandbox" if backend is None else ""
    else:
        backend = settings.backend if available.get(settings.backend) else None
        reason = {
            "bwrap": "bwrap backend selected, but bubblewrap is not available",
            "docker": "docker backend selected, but docker is not available",
            "seatbelt": "seatbelt backend selected, but sandbox-exec is not available (macOS only)",
        }.get(settings.backend, f"unsupported shell sandbox backend: {settings.backend}")
    if backend is not None:
        if (
            settings.network_domains is not None
            or settings.private_network_endpoints
            or settings.unix_sockets
            or settings.additional_read_files
            or settings.forward_env
            or settings.ssh_agent_socket
            or settings.git_metadata_access
        ) and backend != "seatbelt":
            raise ConfigError("Network capability grants require macOS Seatbelt.")
        if settings.additional_read_roots and backend != "seatbelt":
            raise ConfigError("Operation-local read grants require macOS Seatbelt.")
        if extended_roots and backend not in {"bwrap", "seatbelt"}:
            raise ConfigError(
                "Additional sandbox workspace folders require Windows MXC/native, Linux/WSL bubblewrap or macOS Seatbelt; no host fallback is allowed."
            )
        return SandboxBackendChoice(backend)
    if extended_roots:
        raise ConfigError(
            "No sandbox supporting additional workspace folders is available; no host fallback is allowed."
        )
    if settings.unavailable_fallback == "ask" and settings.filesystem_access != "read-only":
        return SandboxBackendChoice("host", reason, requires_command_approval=True)
    return SandboxBackendChoice(None, reason)


def describe_shell_backend(
    choice: SandboxBackendChoice,
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Name the shell that runs a command under ``choice``, for the model's prompt.

    It follows the runners built from the same choice: HostShellRunner passes the command
    with ``shell=True`` (%COMSPEC% on Windows, /bin/sh elsewhere), BwrapShellRunner and
    DockerShellRunner run ``sh -lc``, and SeatbeltShellRunner runs ``/bin/sh -c`` inside
    sandbox-exec.
    """
    if choice.backend == "host":
        current = (system if system is not None else platform.system()).lower()
        if current == "windows":
            env = os.environ if environ is None else environ
            shell = ntpath.basename(str(env.get("COMSPEC") or "")).lower() or "cmd.exe"
        else:
            shell = "sh"
        if choice.requires_command_approval:
            return f"{shell} (host, each command needs approval)"
        return f"{shell} (host)"
    if choice.backend == "bwrap":
        return "sh (bwrap sandbox)"
    if choice.backend == "docker":
        return "sh (Docker sandbox)"
    if choice.backend == "seatbelt":
        return "sh (Seatbelt sandbox)"
    if choice.backend in {"windows-mxc", "windows-native"}:
        return f"Windows PowerShell (powershell.exe, {choice.backend} sandbox)"
    return "none (no usable sandbox, so shell commands are disabled)"


def seatbelt_command(
    cmd: str,
    *,
    root: Path,
    network: str,
    protect_repo_meta: bool,
    safe_git_writes: bool,
    filesystem_access: str = "workspace-write",
    additional_workspace_roots: tuple[str, ...] = (),
    temp_dir: Path | None = None,
    additional_read_roots: tuple[str, ...] = (),
    preview_access: str = "auto",
    network_proxy_port: int | None = None,
    unix_sockets: tuple[str, ...] = (),
    additional_read_files: tuple[str, ...] = (),
    git_metadata_access: bool = False,
    runtime_paths: tuple[Path, ...] | None = None,
    check_preparation: Callable[[], None] = lambda: None,
) -> str:
    """``cmd`` wrapped in sandbox-exec, as one string for ``/bin/sh``."""
    from .macos_git import literal_git_process
    from .macos_sandbox import policy_paths, runtime_read_paths
    from .macos_sandbox_owner import ownership_names

    _validate_filesystem_access(filesystem_access)
    check_preparation()
    root_abs = root.resolve()
    all_roots = tuple(
        dict.fromkeys((*allowed_roots(root_abs), *(Path(p) for p in additional_workspace_roots)))
    )
    protected: list[Path] = []
    git_argv = literal_git_process(cmd) if safe_git_writes else None
    metadata_writes = git_argv is not None or git_metadata_access
    if protect_repo_meta:
        for folder in all_roots:
            protected.extend(
                folder / name
                for name in _PROTECTED_REPO_META
                if name != ".git" or not metadata_writes
            )
            protected.extend(
                path
                for path, rel in _protected_repo_paths(folder)
                if rel != ".git" or not metadata_writes
            )
    if runtime_paths is None:
        runtime_paths = runtime_read_paths(
            os.environ.get("PATH"), workspace_roots=all_roots, check_preparation=check_preparation
        )
    roots, policy, anchors = policy_paths(
        root_abs,
        additional_workspace_roots,
        readonly_roots=runtime_paths,
        protected=tuple(protected),
        additional_read_roots=additional_read_roots,
        filesystem_access=filesystem_access,
        check_preparation=check_preparation,
    )
    parameters = ["-D", f"WORKSPACE={root_abs}"]
    groups = {
        "FOLDER": roots[1:],
        "READ": (*policy.readonly_roots, *(Path(p) for p in additional_read_roots)),
        "PROTECTED": tuple(dict.fromkeys(p.resolve() for p in protected)),
        "PRIVATE": policy.private_roots,
        "READONLY": policy.readonly_roots,
        "ANCHOR": anchors,
        "SOCKET": tuple(Path(p) for p in unix_sockets),
        "FILE": tuple(Path(p) for p in additional_read_files),
    }
    for prefix, paths in groups.items():
        for index, path in enumerate(paths):
            parameters += ["-D", f"{prefix}_{index}={path}"]
    if temp_dir is not None:
        parameters += ["-D", f"TMPDIR={temp_dir.resolve()}"]
    owner_names = ownership_names(temp_dir) if temp_dir is not None else None
    if owner_names is not None:
        parameters += ["-D", f"OWNER_ALLOW={owner_names[0]}", "-D", f"OWNER_DENY={owner_names[1]}"]
    profile = seatbelt_profile(
        network=network,
        preview_access=preview_access,
        filesystem_access=filesystem_access,
        protected_path_count=len(groups["PROTECTED"]),
        extra_folder_count=len(roots) - 1,
        read_root_count=len(groups["READ"]),
        private_path_count=len(groups["PRIVATE"]),
        readonly_path_count=len(groups["READONLY"]),
        anchor_count=len(anchors),
        scratch=temp_dir is not None,
        protect_repo_meta=protect_repo_meta,
        git_metadata_writes=metadata_writes,
        read_file_count=len(additional_read_files),
        network_proxy_port=network_proxy_port,
        unix_socket_count=len(unix_sockets),
        ownership=owner_names is not None,
    )
    process = git_argv if git_argv is not None else ["/bin/sh", "-c", cmd]
    return shlex.join([SEATBELT_EXECUTABLE, *parameters, "-p", profile, *process])


@contextmanager
def seatbelt_execution(
    *,
    root: Path,
    cwd: Path,
    cmd: str,
    filesystem_access: str,
    network: str,
    clear_env: bool,
    protect_repo_meta: bool,
    safe_git_writes: bool,
    additional_workspace_roots: tuple[str, ...],
    additional_read_roots: tuple[str, ...] = (),
    preview_access: str = "auto",
    network_domains: tuple[str, ...] | None = None,
    private_network_endpoints: tuple[str, ...] = (),
    unix_sockets: tuple[str, ...] = (),
    additional_read_files: tuple[str, ...] = (),
    forward_env: tuple[str, ...] = (),
    ssh_agent_socket: str | None = None,
    git_metadata_access: bool = False,
    check_preparation: Callable[[], None] | None = None,
):
    from .macos_git import stage_global_preferences
    from .macos_sandbox import project_read_paths, runtime_read_paths
    from .sandbox_workspace import contains_workspace_path

    # Background starts also have a finite preparation budget. Foreground and
    # file-worker callers supply their command deadline and cancellation check.
    if check_preparation is None:
        preparation_deadline = time.monotonic() + 30

        def check_preparation() -> None:
            if time.monotonic() >= preparation_deadline:
                raise subprocess.TimeoutExpired(
                    cmd, 30, stderr="Sandbox preparation timed out before launch."
                )

    check_preparation()

    if not contains_workspace_path(
        root, cwd, (*additional_workspace_roots, *additional_read_roots)
    ):
        raise ConfigError("Seatbelt cwd must be inside an approved workspace.")
    with ExitStack() as lifetime:
        directory = lifetime.enter_context(
            # Darwin Unix socket paths are limited to 104 bytes. The per-user
            # /var/folders prefix leaves too little room for worker sockets.
            tempfile.TemporaryDirectory(
                prefix=f"alysis-seatbelt-{secrets.token_hex(16)}-", dir="/private/tmp"
            )
        )
        scratch = Path(directory).resolve()
        from .macos_sandbox_owner import MacSandboxOwner

        owner = MacSandboxOwner(scratch)
        (scratch / "home").mkdir()
        env = _sanitized_env(clear_env=clear_env)
        runtime_paths = runtime_read_paths(
            env.get("PATH"),
            workspace_roots=(*allowed_roots(root), *(Path(p) for p in additional_workspace_roots)),
            check_preparation=check_preparation,
        )
        env.update(
            HOME=str(scratch / "home"), TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch)
        )
        from .macos_access import environment_names, file_identity

        for path in additional_read_files:
            file_identity(path)
        env.update({name: os.environ[name] for name in environment_names(forward_env)})
        if ssh_agent_socket is not None:
            if ssh_agent_socket not in unix_sockets:
                raise ConfigError("SSH agent forwarding requires its exact approved socket.")
            env["SSH_AUTH_SOCK"] = ssh_agent_socket
        proxy_port = None
        if network_domains is not None or private_network_endpoints:
            from .ide.browser_egress_proxy import BrowserEgressProxy
            from .macos_network import endpoint_pairs, proxy_environment
            from .macos_network import network_domains as validate_domains

            if network != "on":
                raise ConfigError("Domain-scoped command access requires network permission.")
            proxy = lifetime.enter_context(
                BrowserEgressProxy(
                    require_authentication=True,
                    allowed_hosts=frozenset(validate_domains(network_domains))
                    if network_domains
                    else frozenset(),
                    allowed_private_endpoints=endpoint_pairs(private_network_endpoints),
                )
            )
            proxy_port = proxy.port
            env.update(proxy_environment(proxy.proxy_url))
        if unix_sockets:
            from .macos_network import socket_identity

            for path in unix_sockets:
                identity = socket_identity(path)
                if identity.path != path:
                    raise ConfigError("Daemon socket paths must be canonical.")
        # Git may be launched by any contained program, including the fixed
        # read worker. Its private configuration must not depend on recognizing
        # the outer command. Metadata write authority remains separate below.
        stage_global_preferences(scratch / "home")
        env.update(
            GIT_CONFIG_GLOBAL=str(scratch / "home" / ".gitconfig"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            # Git children must not probe an unapproved parent repository when
            # the selected project has no .git of its own.
            GIT_CEILING_DIRECTORIES=os.pathsep.join(
                str(Path(path).resolve().parent)
                for path in (*allowed_roots(root), *additional_workspace_roots)
            ),
        )
        wrapped = seatbelt_command(
            cmd,
            root=root,
            network=network,
            protect_repo_meta=protect_repo_meta,
            safe_git_writes=safe_git_writes,
            filesystem_access=filesystem_access,
            additional_workspace_roots=additional_workspace_roots,
            additional_read_roots=additional_read_roots,
            preview_access=preview_access,
            temp_dir=scratch,
            network_proxy_port=proxy_port,
            unix_sockets=unix_sockets,
            additional_read_files=additional_read_files,
            git_metadata_access=git_metadata_access,
            runtime_paths=runtime_paths,
            check_preparation=check_preparation,
        )
        project_read_paths(
            scratch / "home",
            (*runtime_paths, *(Path(p) for p in (*additional_read_roots, *additional_read_files))),
            user_home=Path.home().resolve(),
        )
        from .terminal_ownership import TerminalOwnershipLedger, seatbelt_scratch_cleanup_id

        ledger = TerminalOwnershipLedger()
        ownership_handle = ledger.track(
            child_pid=None,
            termination_mode="resource",
            posix_pgid=None,
            cleanup_kind="seatbelt-scratch",
            cleanup_id=seatbelt_scratch_cleanup_id(scratch),
        )
        if ownership_handle is None:
            raise ConfigError("Could not persist Seatbelt command ownership before launch.")
        try:
            check_preparation()
            yield wrapped, env, owner
        finally:
            if not owner.terminate():
                raise CommandCleanupError(cmd)
            owner.release_records()
            ledger.release(ownership_handle)


@dataclass(frozen=True)
class SeatbeltShellRunner:
    github_auth: bool = False
    filesystem_access: str = "workspace-write"
    additional_workspace_roots: tuple[str, ...] = ()
    additional_read_roots: tuple[str, ...] = ()
    network_domains: tuple[str, ...] | None = None
    private_network_endpoints: tuple[str, ...] = ()
    unix_sockets: tuple[str, ...] = ()
    additional_read_files: tuple[str, ...] = ()
    forward_env: tuple[str, ...] = ()
    ssh_agent_socket: str | None = None
    git_metadata_access: bool = False
    preview_access: str = "auto"
    clear_env: bool = True
    network: str = "off"
    protect_repo_meta: bool = True
    safe_git_writes: bool = True
    process_group_registry: ProcessGroupRegistry | None = None
    close_stdin: bool = False

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        credential = None
        if self.github_auth:
            from .macos_github_auth import validate_command

            if not (
                self.clear_env
                and self.protect_repo_meta
                and self.safe_git_writes
                and self.network == "on"
            ):
                raise ConfigError(
                    "GitHub authentication requires contained Git with reviewed networking."
                )
            validate_command(cmd)
        deadline = time.monotonic() + timeout_s

        def check_preparation() -> None:
            if cancel_check is not None and cancel_check():
                raise CommandCancelledError(cmd)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    cmd, timeout_s, stderr="Sandbox preparation timed out before launch."
                )

        with seatbelt_execution(
            root=root,
            cwd=cwd,
            cmd=cmd,
            filesystem_access=self.filesystem_access,
            additional_workspace_roots=self.additional_workspace_roots,
            additional_read_roots=self.additional_read_roots,
            preview_access=self.preview_access,
            clear_env=self.clear_env,
            network=self.network,
            protect_repo_meta=self.protect_repo_meta,
            safe_git_writes=self.safe_git_writes,
            network_domains=self.network_domains,
            private_network_endpoints=self.private_network_endpoints,
            unix_sockets=self.unix_sockets,
            additional_read_files=self.additional_read_files,
            forward_env=self.forward_env,
            ssh_agent_socket=self.ssh_agent_socket,
            git_metadata_access=self.git_metadata_access,
            check_preparation=check_preparation,
        ) as (wrapped, env, owner):
            if self.github_auth:
                from .macos_github_auth import load_credential

                credential = load_credential(
                    workspace_roots=(
                        *allowed_roots(root),
                        *(Path(p) for p in self.additional_workspace_roots),
                    )
                )
                credential.configure(env)
                check_preparation()
            try:
                completed = run_in_tracked_process_group(
                    wrapped,
                    shell=True,
                    cwd=str(cwd),
                    env=env,
                    timeout=max(0, deadline - time.monotonic()),
                    registry=self.process_group_registry,
                    origin="shell_run:seatbelt",
                    track_descendants=True,
                    process_owner=owner,
                    popen_factory=owner.spawn,
                    command_label=cmd,
                    stdin=subprocess.DEVNULL if self.close_stdin else None,
                    cancel_check=cancel_check,
                )
            except (subprocess.TimeoutExpired, CommandCancelledError) as exc:
                if credential is not None:
                    for name in ("output", "stderr"):
                        value = getattr(exc, name, None)
                        if isinstance(value, bytes):
                            value = value.decode("utf-8", errors="replace")
                        if isinstance(value, str):
                            setattr(exc, name, credential.redact(value))
                raise
            if credential is not None:
                completed.stdout = credential.redact(completed.stdout)
                completed.stderr = credential.redact(completed.stderr)
            if completed.returncode and self.protect_repo_meta and not self.git_metadata_access:
                from .macos_git import literal_git_process

                if not self.safe_git_writes or literal_git_process(cmd) is None:
                    # Report the policy actually applied, without guessing the cause
                    # from localized stderr or automatically rerunning side effects.
                    completed.sandbox_policy = {
                        "backend": "seatbelt",
                        "filesystem_access": self.filesystem_access,
                        "git_metadata": "protected",
                        "guidance": (
                            "Policy context, not a diagnosis of this exit code: folder-write "
                            "permissions do not grant Git metadata writes to mixed shell commands. "
                            "Use cwd with a literal Git command, or request operation-only "
                            "permissions.git_metadata=true for the entire exact script when "
                            "metadata writes are needed. Inspect partial effects before retrying."
                        ),
                    }
            return completed


def _emit_warning(message: str, *, warning_callback: Callable[[str], None] | None) -> None:
    if warning_callback is not None:
        warning_callback(message)
        return
    print(f"[alysis] {message}", file=sys.stderr)


def with_closed_stdin(runner: ShellRunner) -> ShellRunner:
    """Return an equivalent runner that never inherits the caller's stdin.

    Automated callers such as verification must not be able to block on an
    interactive prompt: nobody is watching the terminal, so a command that asks
    a question would otherwise wait until its timeout. Runners that do not
    execute anything (disabled/lazy) are returned unchanged.
    """

    if is_dataclass(runner) and any(field.name == "close_stdin" for field in fields(runner)):
        return replace(runner, close_stdin=True)  # type: ignore[type-var]
    return runner


def execution_scope_fingerprint(runner: object) -> str | None:
    """Identify a known execution scope without exposing environment values.

    Resolve only an already-used lazy runner. Custom runners stay incomparable.
    Process tracking, callbacks and stdin handling do not change the filesystem,
    environment or isolation policy used to compare verification executions.
    """
    from .windows_mxc import WindowsMxcRunner
    from .windows_sandbox import WindowsNativeRunner

    if type(runner) is LazyShellRunner:
        runner = runner._runner
    if type(runner) not in {
        HostShellRunner,
        BwrapShellRunner,
        DockerShellRunner,
        SeatbeltShellRunner,
        WindowsNativeRunner,
        WindowsMxcRunner,
    }:
        return None
    ignored = {"process_group_registry", "warning_callback", "close_stdin"}
    settings = {
        item.name: getattr(runner, item.name) for item in fields(runner) if item.name not in ignored
    }
    settings = {
        key: asdict(value) if is_dataclass(value) else value for key, value in settings.items()
    }
    context = (
        type(runner).__module__,
        type(runner).__qualname__,
        settings,
        sorted(os.environ.items()),
    )
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def build_shell_runner_from_settings(
    settings: ShellSandboxSettings,
    root: Path,
    warning_callback: Callable[[str], None] | None = None,
    *,
    process_group_registry: ProcessGroupRegistry | None = None,
) -> ShellRunner:
    _ = root.resolve()

    # The Docker backend is deliberately not given the registry: `killpg` on the
    # `docker run` CLI does not stop the container, so teardown must go through
    # `_docker_cleanup_container` (which this runner already does on timeout and
    # on exception). This mirrors DockerBackgroundRunner's "direct" termination
    # mode.
    choice = select_shell_sandbox_backend(settings)
    if choice.backend == "host":
        return HostShellRunner(
            additional_workspace_roots=settings.additional_workspace_roots,
            process_group_registry=process_group_registry,
            requires_command_approval=choice.requires_command_approval,
        )
    if choice.backend == "windows-mxc":
        from .windows_mxc import WindowsMxcRunner, resolve_helper

        resolve_helper()
        return WindowsMxcRunner(settings)
    if choice.backend == "windows-native":
        from .windows_sandbox import WindowsNativeRunner, resolve_helper

        resolve_helper()
        return WindowsNativeRunner(settings)
    if choice.backend == "bwrap":
        return BwrapShellRunner(
            additional_workspace_roots=settings.additional_workspace_roots,
            filesystem_access=settings.filesystem_access,
            network=settings.network,
            clear_env=settings.clear_env,
            profile=settings.bwrap_profile,
            protect_repo_meta=settings.protect_repo_meta,
            safe_git_writes=settings.safe_git_writes,
            process_group_registry=process_group_registry,
        )
    if choice.backend == "docker":
        return DockerShellRunner(
            filesystem_access=settings.filesystem_access,
            network=settings.network,
            docker_image=settings.docker_image,
            clear_env=settings.clear_env,
            pids_limit=settings.docker_pids_limit,
            memory_limit=settings.docker_memory,
            cpus=settings.docker_cpus,
            read_only_rootfs=settings.docker_read_only,
            protect_repo_meta=settings.protect_repo_meta,
            safe_git_writes=settings.safe_git_writes,
            env_allowlist=settings.docker_env_allowlist,
            warning_callback=warning_callback,
        )
    if choice.backend == "seatbelt":
        return SeatbeltShellRunner(
            github_auth=settings.github_auth,
            network_domains=settings.network_domains,
            private_network_endpoints=settings.private_network_endpoints,
            unix_sockets=settings.unix_sockets,
            additional_read_files=settings.additional_read_files,
            forward_env=settings.forward_env,
            ssh_agent_socket=settings.ssh_agent_socket,
            git_metadata_access=settings.git_metadata_access,
            additional_read_roots=settings.additional_read_roots,
            preview_access=settings.preview_access,
            filesystem_access=settings.filesystem_access,
            additional_workspace_roots=settings.additional_workspace_roots,
            clear_env=settings.clear_env,
            network=settings.network,
            protect_repo_meta=settings.protect_repo_meta,
            safe_git_writes=settings.safe_git_writes,
            process_group_registry=process_group_registry,
        )
    if settings.mode == "warn":
        disabled_reason = (
            f"Shell sandbox unavailable ({choice.unavailable_reason}); host fallback is disabled."
        )
        _emit_warning(disabled_reason, warning_callback=warning_callback)
        return DisabledShellRunner(reason=disabled_reason)
    raise ConfigError(
        "Shell sandbox strict mode is enabled, but no usable backend is available: "
        f"{choice.unavailable_reason}. Install bubblewrap (Linux) or Docker, or set "
        "ALYSIS_SHELL_SANDBOX_MODE=off for explicit unsafe host execution, or "
        "ALYSIS_SHELL_SANDBOX_FALLBACK=ask to approve each command. "
        "Run `alysis doctor sandbox` for a guided diagnosis or "
        "`alysis setup sandbox` after installing/starting Docker."
    )


def build_shell_runner(
    cfg: AppConfig,
    root: Path,
    warning_callback: Callable[[str], None] | None = None,
) -> ShellRunner:
    settings = resolve_shell_sandbox_settings(cfg)
    return build_shell_runner_from_settings(settings, root, warning_callback=warning_callback)
