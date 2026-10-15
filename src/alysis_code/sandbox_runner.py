from __future__ import annotations

import ipaddress
import logging
import ntpath
import os
import platform
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from .branding import default_sandbox_docker_image
from .bwrap_etc import ensure_minimal_etc_dir
from .command_text import command_text
from .config import AppConfig, ConfigError
from .process_reaping import (
    CommandCancelledError,
    ProcessGroupRegistry,
    communicate_until_cancelled,
    run_in_tracked_process_group,
)
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
_BWRAP_COMMON_TOOLCHAIN_COMMANDS = (
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "py.test",
    "ruff",
    "mypy",
    "pyright",
    "tox",
    "nox",
    "uv",
    "poetry",
    "pipx",
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "bun",
    "cargo",
    "rustc",
    "rustup",
    "go",
    "java",
    "javac",
    "jar",
    "mvn",
    "gradle",
)


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


def _path_env_entries(path_value: str | None) -> tuple[Path, ...]:
    entries: list[Path] = []
    for raw in (path_value or "").split(os.pathsep):
        if not raw.strip():
            continue
        entries.append(Path(raw.strip()))
    return tuple(entries)


def _toolchain_root_for_executable(executable: Path, *, command_name: str) -> Path:
    resolved = executable.resolve()
    parts = tuple(part.casefold() for part in resolved.parts)
    if ".cargo" in parts:
        index = parts.index(".cargo")
        return Path(*resolved.parts[: index + 1])
    if ".rustup" in parts:
        index = parts.index(".rustup")
        return Path(*resolved.parts[: index + 1])
    if command_name in {"node", "npm", "npx", "pnpm", "yarn", "bun"}:
        for marker in (".nvm", ".fnm", ".volta"):
            if marker in parts:
                index = parts.index(marker)
                # Bind the version manager root. npm/yarn shims commonly jump between
                # bin and lib/node_modules under the same versioned install.
                if marker == ".nvm" and index + 3 < len(resolved.parts):
                    return Path(*resolved.parts[: index + 4])
                return Path(*resolved.parts[: index + 1])
    if resolved.parent.name == "bin":
        return resolved.parent.parent
    return resolved.parent


def _toolchain_extra_env(bind_roots: tuple[Path, ...]) -> dict[str, str]:
    env: dict[str, str] = {}
    cargo_home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")
    rustup_home = Path(os.environ.get("RUSTUP_HOME") or Path.home() / ".rustup")
    python_userbase = Path(site.getuserbase())
    if any(root == cargo_home.resolve() for root in bind_roots if root.exists()):
        env["CARGO_HOME"] = os.fspath(cargo_home.resolve())
    if rustup_home.exists():
        env["RUSTUP_HOME"] = os.fspath(rustup_home.resolve())
    if python_userbase.exists() and any(
        root == python_userbase.resolve() for root in bind_roots if root.exists()
    ):
        env["PYTHONUSERBASE"] = os.fspath(python_userbase.resolve())
    return env


def _common_toolchain_ro_bind_roots(path_value: str | None) -> tuple[Path, ...]:
    search_path = os.pathsep.join(os.fspath(path) for path in _path_env_entries(path_value))
    candidates: list[Path] = []
    for command_name in _BWRAP_COMMON_TOOLCHAIN_COMMANDS:
        executable = shutil.which(command_name, path=search_path or None)
        if not executable:
            continue
        executable_path = Path(executable)
        if _path_is_inside_system_root(executable_path):
            continue
        candidates.append(
            _toolchain_root_for_executable(executable_path, command_name=command_name)
        )
        if command_name in {"cargo", "rustc", "rustup"}:
            cargo_home = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")
            rustup_home = Path(os.environ.get("RUSTUP_HOME") or Path.home() / ".rustup")
            candidates.extend([cargo_home, rustup_home])
    return _dedupe_existing_paths(candidates)


def _add_common_toolchain_bindings(
    args: list[str],
    *,
    path_value: str | None,
    created_dirs: set[str],
) -> dict[str, str]:
    bound_dests: set[str] = set()
    bind_roots = _common_toolchain_ro_bind_roots(path_value)
    for root in bind_roots:
        _add_bwrap_ro_bind(
            args,
            root,
            root,
            created_dirs=created_dirs,
            bound_dests=bound_dests,
        )
    return _toolchain_extra_env(bind_roots)


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


@dataclass(frozen=True)
class HostShellRunner:
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
        return run_in_tracked_process_group(
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
    def __init__(self, loader: Callable[[], ShellRunner]) -> None:
        self._loader = loader
        self._runner: ShellRunner | None = None
        self._load_error: Exception | None = None

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
) -> tuple[list[str], dict[str, str]]:
    """Build bwrap argv + parent-process env. Pure function; no subprocess calls."""

    root_abs = root.resolve()
    workdir = _workspace_cwd(root=root_abs, cwd=cwd)
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

    args.extend(["--bind", os.fspath(root_abs), "/workspace"])
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
    toolchain_env = _add_common_toolchain_bindings(
        args,
        path_value=path_value,
        created_dirs=created_dirs,
    )

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
    for key, value in toolchain_env.items():
        args.extend(["--setenv", key, value])
    if network == "off":
        args.append("--unshare-net")
    args.extend(["sh", "-lc", cmd])
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

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
        )
        return run_in_tracked_process_group(
            args,
            cwd=os.fspath(root.resolve()),
            env=run_env,
            timeout=timeout_s,
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
) -> tuple[list[str], dict[str, str]]:
    """Build docker run argv + parent-process env. Pure function."""

    root_abs = root.resolve()
    workdir = _workspace_cwd(root=root_abs, cwd=cwd)
    run_env = _sanitized_env(clear_env=clear_env)
    container_env = _container_env(clear_env, allowlist=env_allowlist)

    args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "-v",
        f"{os.fspath(root_abs)}:/workspace:rw",
        "-w",
        workdir,
    ]
    if (protect_repo_meta and not allow_repo_meta_writes) or read_only_rootfs:
        for host_path, rel_posix_path in _protected_repo_paths(root_abs):
            args.extend(
                [
                    "-v",
                    f"{os.fspath(host_path)}:/workspace/{rel_posix_path}:ro",
                ]
            )
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


@dataclass(frozen=True)
class SandboxBackendChoice:
    """Where shell commands run under some sandbox settings, decided in one place.

    ``backend`` is "host", "bwrap", "docker" or "seatbelt", or None when the settings ask
    for a sandbox that this machine cannot provide (``unavailable_reason`` says why).
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

    ``auto`` prefers bubblewrap on Linux, then Docker, then Seatbelt on macOS. When no
    backend is usable and ``unavailable_fallback`` is "ask", commands run on the host and
    each one needs the user's approval; with "refuse" nothing runs.
    """
    if settings.mode == "off":
        return SandboxBackendChoice("host")
    # Looked up at call time, so a patched shutil.which or os.path.exists applies.
    which = which or shutil.which
    path_exists = path_exists or os.path.exists
    current = (system if system is not None else platform.system()).lower()
    available = {
        "bwrap": current == "linux" and which("bwrap") is not None,
        "docker": which("docker") is not None,
        "seatbelt": current == "darwin" and path_exists(SEATBELT_EXECUTABLE),
    }
    if settings.backend == "auto":
        backend = next((b for b in ("bwrap", "docker", "seatbelt") if available[b]), None)
        reason = "auto backend could not find bwrap or docker" if backend is None else ""
    else:
        backend = settings.backend if available.get(settings.backend) else None
        reason = {
            "bwrap": "bwrap backend selected, but bubblewrap is not available",
            "docker": "docker backend selected, but docker is not available",
            "seatbelt": "seatbelt backend selected, but sandbox-exec is not available (macOS only)",
        }.get(settings.backend, f"unsupported shell sandbox backend: {settings.backend}")
    if backend is not None:
        return SandboxBackendChoice(backend)
    if settings.unavailable_fallback == "ask":
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
    return "none (no usable sandbox, so shell commands are disabled)"


def seatbelt_profile(*, network: str, protected_path_count: int) -> str:
    """A macOS Seatbelt (sandbox-exec) profile: everything allowed except writes outside the
    workspace and the temporary folders, writes to protected repository metadata, and, with
    the network off, connections to anything but this machine.

    Paths come in as ``-D`` parameters so no path is ever quoted into the profile text. In
    Seatbelt the last matching rule wins, so the narrower rules follow the broad ones.
    """
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
        '    (subpath (param "WORKSPACE"))',
        '    (subpath (param "TMPDIR"))',
        '    (subpath "/private/tmp")',
        '    (subpath "/private/var/folders")',
        '    (literal "/dev/null")',
        '    (literal "/dev/zero")',
        '    (literal "/dev/stdout")',
        '    (literal "/dev/stderr")',
        '    (literal "/dev/tty")',
        '    (literal "/dev/dtracehelper")',
        '    (regex #"^/dev/fd/[0-9]+$"))',
    ]
    lines += [
        f'(deny file-write* (subpath (param "PROTECTED_{index}")))'
        for index in range(protected_path_count)
    ]
    if network != "on":
        lines += [
            '(deny network-outbound (remote ip "*:*"))',
            '(allow network-outbound (remote ip "localhost:*"))',
        ]
    return "\n".join(lines)


def seatbelt_command(
    cmd: str,
    *,
    root: Path,
    network: str,
    protect_repo_meta: bool,
    safe_git_writes: bool,
) -> str:
    """``cmd`` wrapped in sandbox-exec, as one string for ``/bin/sh``."""
    root_abs = os.path.realpath(root)
    protected: list[str] = []
    if protect_repo_meta and not (safe_git_writes and is_safe_repo_meta_git_command(cmd)):
        protected = [os.path.realpath(path) for path, _rel in _protected_repo_paths(Path(root_abs))]
    parameters = [
        "-D",
        f"WORKSPACE={root_abs}",
        "-D",
        f"TMPDIR={os.path.realpath(tempfile.gettempdir())}",
    ]
    for index, path in enumerate(protected):
        parameters += ["-D", f"PROTECTED_{index}={path}"]
    profile = seatbelt_profile(network=network, protected_path_count=len(protected))
    return shlex.join([SEATBELT_EXECUTABLE, *parameters, "-p", profile, "/bin/sh", "-c", cmd])


@dataclass(frozen=True)
class SeatbeltShellRunner:
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
        wrapped = seatbelt_command(
            cmd,
            root=root,
            network=self.network,
            protect_repo_meta=self.protect_repo_meta,
            safe_git_writes=self.safe_git_writes,
        )
        return run_in_tracked_process_group(
            wrapped,
            shell=True,
            cwd=str(cwd),
            timeout=timeout_s,
            registry=self.process_group_registry,
            origin="shell_run:seatbelt",
            command_label=cmd,
            stdin=subprocess.DEVNULL if self.close_stdin else None,
            cancel_check=cancel_check,
        )


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
            process_group_registry=process_group_registry,
            requires_command_approval=choice.requires_command_approval,
        )
    if choice.backend == "bwrap":
        return BwrapShellRunner(
            network=settings.network,
            clear_env=settings.clear_env,
            profile=settings.bwrap_profile,
            protect_repo_meta=settings.protect_repo_meta,
            safe_git_writes=settings.safe_git_writes,
            process_group_registry=process_group_registry,
        )
    if choice.backend == "docker":
        return DockerShellRunner(
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
