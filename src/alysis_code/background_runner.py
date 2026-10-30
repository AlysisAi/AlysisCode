from __future__ import annotations

import os
import subprocess
import threading
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .branding import default_sandbox_docker_image
from .config import AppConfig, ConfigError
from .macos_process_tree import MacProcessTree
from .macos_sandbox_owner import MacSandboxOwner
from .sandbox_runner import (
    _SENSITIVE_ENV_KEYS,
    _build_bwrap_argv,
    _build_docker_argv,
    _docker_cleanup_container,
    _docker_policy_env,
    _emit_warning,
    _supports_bwrap_unshare_cgroup,
    seatbelt_execution,
    select_shell_sandbox_backend,
)
from .sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings
from .service_persistence import apply_non_interactive_defaults

BackgroundTerminationMode = Literal["process_group", "direct"]


@dataclass(frozen=True)
class BackgroundProcessSpawn:
    popen: subprocess.Popen[bytes]
    cleanup: Callable[[], None]
    started_argv: tuple[str, ...]
    termination_mode: BackgroundTerminationMode = "process_group"
    orphan_cleanup_kind: str | None = None
    orphan_cleanup_id: str | None = None
    process_tree: MacProcessTree | MacSandboxOwner | None = None


class BackgroundShellRunner(Protocol):
    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn: ...


def _background_env(env_overrides: dict[str, str] | None) -> dict[str, str]:
    blocked = {key.upper() for key in _SENSITIVE_ENV_KEYS}
    env = {key: value for key, value in os.environ.items() if key.upper() not in blocked}
    return _apply_env_overrides(env, env_overrides)


def _apply_env_overrides(
    env: dict[str, str],
    env_overrides: dict[str, str] | None,
) -> dict[str, str]:
    blocked = {key.upper() for key in _SENSITIVE_ENV_KEYS}
    # Defaults first so an explicit override still wins. Shared by the
    # background runners and the durable-service manager, so this covers every
    # non-foreground spawn in one place.
    merged = apply_non_interactive_defaults(env)
    for key, value in (env_overrides or {}).items():
        if key.upper() in blocked:
            continue
        merged[key] = value
    return merged


def _noop_cleanup() -> None:
    return None


@dataclass(frozen=True)
class HostBackgroundRunner:
    additional_workspace_roots: tuple[str, ...] = ()

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        kwargs: dict[str, object] = {}
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        popen = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_background_env(env_overrides),
            bufsize=0,
            **kwargs,
        )
        return BackgroundProcessSpawn(
            popen=popen,
            cleanup=_noop_cleanup,
            started_argv=(cmd,),
        )


@dataclass(frozen=True)
class SeatbeltBackgroundRunner:
    """Background processes in the same macOS Seatbelt profile as SeatbeltShellRunner."""

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

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        scope = ExitStack()
        try:
            wrapped, env, owner = scope.enter_context(
                seatbelt_execution(
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
                )
            )
            merged = _apply_env_overrides(env, env_overrides)
            for key in (
                "HOME",
                "PATH",
                "TMPDIR",
                "TMP",
                "TEMP",
                *self.forward_env,
                *(("SSH_AUTH_SOCK",) if self.ssh_agent_socket else ()),
            ):
                merged[key] = env[key]
            if self.network_domains is not None or self.private_network_endpoints:
                from .macos_network import proxy_environment

                for key in proxy_environment(env["http_proxy"]):
                    merged[key] = env[key]
            from .terminal_ownership import seatbelt_scratch_cleanup_id

            scratch_id = seatbelt_scratch_cleanup_id(Path(env["TMPDIR"]))
            popen = owner.spawn(
                wrapped,
                shell=True,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged,
                bufsize=0,
                start_new_session=True,
            )
        except BaseException:
            scope.close()
            raise
        tree = owner

        def cleanup() -> None:
            try:
                if tree is not None and not tree.terminate():
                    raise RuntimeError("Sandbox child cleanup could not be confirmed.")
            finally:
                scope.close()

        return BackgroundProcessSpawn(
            popen=popen,
            cleanup=cleanup,
            started_argv=(cmd,),
            process_tree=tree,
            orphan_cleanup_kind="seatbelt-scratch",
            orphan_cleanup_id=scratch_id,
        )


@dataclass(frozen=True)
class DisabledBackgroundRunner:
    reason: str = "Background shell execution is disabled."

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        raise RuntimeError(self.reason)


class LazyBackgroundShellRunner:
    def __init__(self, loader: Callable[[], BackgroundShellRunner]) -> None:
        self._loader = loader
        self._runner: BackgroundShellRunner | None = None
        self._load_error: Exception | None = None
        self._lock = threading.Lock()

    def _resolve_runner(self) -> BackgroundShellRunner:
        if self._runner is not None:
            return self._runner
        if self._load_error is not None:
            raise self._load_error
        with self._lock:
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

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        return self._resolve_runner().start(
            root=root,
            cwd=cwd,
            cmd=cmd,
            env_overrides=env_overrides,
        )


@dataclass(frozen=True)
class BwrapBackgroundRunner:
    network: str = "off"
    clear_env: bool = True
    profile: str = "hardened"
    filesystem_access: str = "workspace-write"
    additional_workspace_roots: tuple[str, ...] = ()

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        root_abs = root.resolve()
        argv, run_env = _build_bwrap_argv(
            root=root_abs,
            cwd=cwd,
            cmd=cmd,
            network=self.network,
            clear_env=self.clear_env,
            profile=self.profile,
            unshare_cgroup=_supports_bwrap_unshare_cgroup(),
            filesystem_access=self.filesystem_access,
            additional_workspace_roots=self.additional_workspace_roots,
        )
        popen = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root_abs),
            env=_apply_env_overrides(run_env, env_overrides),
            bufsize=0,
            start_new_session=True,
        )
        return BackgroundProcessSpawn(
            popen=popen,
            cleanup=_noop_cleanup,
            started_argv=tuple(argv),
        )


@dataclass(frozen=True)
class DockerBackgroundRunner:
    network: str = "off"
    docker_image: str = default_sandbox_docker_image("dev")
    clear_env: bool = True
    pids_limit: int | None = None
    memory_limit: str | None = None
    cpus: str | None = None
    read_only_rootfs: bool = False
    protect_repo_meta: bool = True
    env_allowlist: tuple[str, ...] = ()
    warning_callback: Callable[[str], None] | None = None
    filesystem_access: str = "workspace-write"

    def start(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        env_overrides: dict[str, str] | None = None,
    ) -> BackgroundProcessSpawn:
        root_abs = root.resolve()
        container_name = f"alysis-bgsbx-{uuid.uuid4().hex[:12]}"
        argv, run_env = _build_docker_argv(
            root=root_abs,
            cwd=cwd,
            cmd=cmd,
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
        run_env = _docker_policy_env(
            _apply_env_overrides(run_env, env_overrides), self.filesystem_access
        )

        def cleanup() -> None:
            _docker_cleanup_container(
                container_name,
                cwd=str(root_abs),
                env=run_env,
                warning_callback=self.warning_callback,
                reason="background process cleanup",
                quiet=True,
            )

        popen = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root_abs),
            env=run_env,
            bufsize=0,
        )
        return BackgroundProcessSpawn(
            popen=popen,
            cleanup=cleanup,
            started_argv=tuple(argv),
            termination_mode="direct",
            orphan_cleanup_kind="docker-container",
            orphan_cleanup_id=container_name,
        )


def build_background_shell_runner_from_settings(
    settings: ShellSandboxSettings,
    root: Path,
    warning_callback: Callable[[str], None] | None = None,
) -> BackgroundShellRunner:
    _ = root.resolve()

    choice = select_shell_sandbox_backend(settings)
    if choice.backend == "host":
        # With requires_command_approval the shell guards ask before each start.
        return HostBackgroundRunner(additional_workspace_roots=settings.additional_workspace_roots)
    if choice.backend == "windows-mxc":
        from .windows_mxc import WindowsMxcRunner, resolve_helper

        resolve_helper()
        return WindowsMxcRunner(settings)
    if choice.backend == "windows-native":
        from .windows_sandbox import WindowsNativeRunner, resolve_helper

        resolve_helper()
        return WindowsNativeRunner(settings)
    if choice.backend == "bwrap":
        return BwrapBackgroundRunner(
            additional_workspace_roots=settings.additional_workspace_roots,
            filesystem_access=settings.filesystem_access,
            network=settings.network,
            clear_env=settings.clear_env,
            profile=settings.bwrap_profile,
        )
    if choice.backend == "docker":
        return _docker_background_runner_from_settings(
            settings,
            warning_callback=warning_callback,
        )
    if choice.backend == "seatbelt":
        return SeatbeltBackgroundRunner(
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
        )
    if settings.mode == "warn":
        disabled_reason = (
            "Background shell sandbox unavailable "
            f"({choice.unavailable_reason}); host fallback is disabled."
        )
        _emit_warning(disabled_reason, warning_callback=warning_callback)
        return DisabledBackgroundRunner(reason=disabled_reason)
    raise ConfigError(
        "Shell sandbox strict mode is enabled, but no usable backend is available "
        f"for background processes: {choice.unavailable_reason}. Install bubblewrap (Linux) "
        "or Docker, or set ALYSIS_SHELL_SANDBOX_MODE=off for explicit unsafe host execution, "
        "or ALYSIS_SHELL_SANDBOX_FALLBACK=ask to approve each command."
    )


def _docker_background_runner_from_settings(
    settings: ShellSandboxSettings,
    *,
    warning_callback: Callable[[str], None] | None,
) -> DockerBackgroundRunner:
    return DockerBackgroundRunner(
        filesystem_access=settings.filesystem_access,
        network=settings.network,
        docker_image=settings.docker_image,
        clear_env=settings.clear_env,
        pids_limit=settings.docker_pids_limit,
        memory_limit=settings.docker_memory,
        cpus=settings.docker_cpus,
        read_only_rootfs=settings.docker_read_only,
        protect_repo_meta=settings.protect_repo_meta,
        env_allowlist=settings.docker_env_allowlist,
        warning_callback=warning_callback,
    )


def build_background_shell_runner(
    cfg: AppConfig,
    root: Path,
    warning_callback: Callable[[str], None] | None = None,
) -> BackgroundShellRunner:
    settings = resolve_shell_sandbox_settings(cfg)
    return build_background_shell_runner_from_settings(
        settings,
        root,
        warning_callback=warning_callback,
    )
