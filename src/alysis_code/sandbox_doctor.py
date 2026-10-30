from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .branding import default_sandbox_docker_image, env_get
from .command_text import command_text
from .config import AppConfig
from .sandbox_runner import SEATBELT_EXECUTABLE, build_shell_runner_from_settings
from .sandbox_settings import ShellSandboxSettings, resolve_shell_sandbox_settings

SandboxCheckStatus = Literal["ok", "missing", "failed", "skipped", "disabled"]

_DOCKER_INFO_TIMEOUT_S = 10
_DOCKER_IMAGE_INSPECT_TIMEOUT_S = 20
_DOCKER_PULL_TIMEOUT_S = 900
_SANDBOX_SMOKE_TIMEOUT_S = 15
_READY_SMOKE_OUTPUT = "alysis-sandbox-ready"


@dataclass(frozen=True)
class SandboxCheck:
    name: str
    status: SandboxCheckStatus
    detail: str


@dataclass(frozen=True)
class SandboxDiagnostic:
    ready: bool
    status: str
    configured_mode: str
    configured_backend: str
    selected_backend: str | None
    docker_image: str
    server_image: str
    checks: tuple[SandboxCheck, ...]
    next_steps: tuple[str, ...]
    can_pull: bool = False


@dataclass(frozen=True)
class SandboxImagePullResult:
    image: str
    ok: bool
    output: str


@dataclass(frozen=True)
class SandboxPullResult:
    ok: bool
    results: tuple[SandboxImagePullResult, ...]
    error: str | None = None


@dataclass(frozen=True)
class BubblewrapInstallPlan:
    manager: str
    command: tuple[str, ...]
    display: str
    # Package-index refresh that runs before ``command``. A fresh WSL or cloud
    # image can ship empty or stale apt lists, and then a bare ``apt-get install``
    # fails with "Unable to locate package" or a 404 on a superseded version.
    refresh_command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BubblewrapInstallResult:
    ok: bool
    command: str
    detail: str


# (manager, install command, index refresh to run first or None)
_BWRAP_INSTALL_MANAGERS: tuple[tuple[str, tuple[str, ...], tuple[str, ...] | None], ...] = (
    ("apt-get", ("apt-get", "install", "-y", "bubblewrap"), ("apt-get", "update")),
    ("dnf", ("dnf", "install", "-y", "bubblewrap"), None),
    ("pacman", ("pacman", "-S", "--noconfirm", "bubblewrap"), None),
    ("zypper", ("zypper", "--non-interactive", "install", "bubblewrap"), None),
    ("apk", ("apk", "add", "bubblewrap"), None),
)
_BWRAP_INSTALL_TIMEOUT_S = 300
# ``apt-get update`` exits 100 when any configured repository fails to refresh.
# The distro archive is usually still fine (a stale third-party PPA is the common
# cause), so the install still runs. Any other failure, typically ``sudo``
# rejecting the password, stops before a second password prompt.
_APT_PARTIAL_REFRESH_EXIT = 100


def default_sandbox_images(*, include_server: bool = True) -> tuple[str, ...]:
    images = [default_sandbox_docker_image("dev")]
    if include_server:
        images.append(resolve_server_sandbox_image())
    return tuple(dict.fromkeys(images))


def configured_sandbox_images(cfg: AppConfig, *, include_server: bool = True) -> tuple[str, ...]:
    settings = resolve_shell_sandbox_settings(cfg)
    images = [settings.docker_image]
    if include_server:
        images.append(resolve_server_sandbox_image())
    return tuple(dict.fromkeys(images))


def resolve_server_sandbox_image() -> str:
    return env_get(
        "ALYSIS_SERVER_DOCKER_IMAGE",
        default=env_get(
            "ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE",
            default=default_sandbox_docker_image("server"),
        ),
    ) or default_sandbox_docker_image("server")


def diagnose_sandbox(
    cfg: AppConfig,
    *,
    include_smoke: bool = False,
    include_server_image: bool = True,
) -> SandboxDiagnostic:
    settings = resolve_shell_sandbox_settings(cfg)
    server_image = resolve_server_sandbox_image()
    checks: list[SandboxCheck] = []

    if settings.mode == "off":
        checks.append(
            SandboxCheck(
                "sandbox mode",
                "disabled",
                "shell_sandbox.mode=off; commands will run on the host shell.",
            )
        )
        return SandboxDiagnostic(
            ready=True,
            status="disabled",
            configured_mode=settings.mode,
            configured_backend=settings.backend,
            selected_backend=None,
            docker_image=settings.docker_image,
            server_image=server_image,
            checks=tuple(checks),
            next_steps=(
                "Sandboxing is disabled by configuration. Re-enable strict mode for safer command execution.",
            ),
        )

    selected_backend = _select_configured_backend(settings)
    if selected_backend == "seatbelt":
        available = platform.system() == "Darwin" and os.path.exists(SEATBELT_EXECUTABLE)
        checks = [
            SandboxCheck("selected backend", "ok" if available else "missing", "seatbelt"),
            SandboxCheck("Seatbelt", "ok" if available else "missing", SEATBELT_EXECUTABLE),
        ]
        if available and include_smoke:
            _append_smoke_check(checks=checks, settings=settings)
        return _diagnostic(
            settings=settings,
            server_image=server_image,
            selected_backend="seatbelt",
            checks=checks,
            next_steps=() if available else ("The native macOS sandbox is unavailable.",),
        )
    if selected_backend == "windows-mxc":
        return _diagnose_windows_mxc(settings, include_smoke=include_smoke)
    if selected_backend == "windows-native":
        return _diagnose_windows_native(settings, include_smoke=include_smoke)
    server_worker_backend = _resolve_server_worker_backend()
    needs_server_docker_image = include_server_image and (
        selected_backend == "docker" or server_worker_backend == "docker"
    )
    is_linux = _is_linux()
    bwrap_path = shutil.which("bwrap") if is_linux else None
    docker_path = shutil.which("docker")

    if bwrap_path:
        checks.append(SandboxCheck("bubblewrap", "ok", bwrap_path))
    elif is_linux:
        checks.append(
            SandboxCheck("bubblewrap", "missing", "bubblewrap (`bwrap`) is not installed.")
        )
    else:
        checks.append(
            SandboxCheck(
                "bubblewrap",
                "skipped",
                "bubblewrap backend is only supported on Linux.",
            )
        )

    if docker_path:
        checks.append(SandboxCheck("Docker CLI", "ok", docker_path))
    else:
        checks.append(
            SandboxCheck("Docker CLI", "missing", "Docker is not installed or not on PATH.")
        )

    if selected_backend is None:
        checks.append(
            SandboxCheck(
                "selected backend",
                "failed",
                "No usable sandbox backend was found for shell_sandbox.backend=auto.",
            )
        )
        return _diagnostic(
            settings=settings,
            server_image=server_image,
            selected_backend=None,
            checks=checks,
            next_steps=_install_backend_steps(),
        )

    checks.append(
        SandboxCheck(
            "selected backend",
            "ok",
            selected_backend,
        )
    )

    if selected_backend == "bwrap":
        if not bwrap_path:
            next_steps = (
                _bwrap_linux_only_steps()
                if _check_status(checks, "bubblewrap") == "skipped"
                else _bwrap_missing_steps(settings=settings)
            )
            return _diagnostic(
                settings=settings,
                server_image=server_image,
                selected_backend=selected_backend,
                checks=checks,
                next_steps=next_steps,
            )
        if include_smoke:
            _append_smoke_check(checks=checks, settings=settings)
        if needs_server_docker_image:
            docker_info = _run_docker_info()
            checks.append(docker_info)
            if docker_info.status == "ok":
                checks.append(_docker_image_check(server_image, label="server sandbox image"))
        return _diagnostic(
            settings=settings,
            server_image=server_image,
            selected_backend=selected_backend,
            checks=checks,
            next_steps=_next_steps_for_checks(checks, selected_backend=selected_backend),
        )

    docker_info = _run_docker_info()
    checks.append(docker_info)
    if docker_info.status != "ok":
        return _diagnostic(
            settings=settings,
            server_image=server_image,
            selected_backend=selected_backend,
            checks=checks,
            next_steps=_docker_daemon_steps(docker_path=docker_path),
        )

    checks.append(_docker_image_check(settings.docker_image, label="sandbox image"))
    if needs_server_docker_image:
        checks.append(_docker_image_check(server_image, label="server sandbox image"))
    if include_smoke and _check_status(checks, "sandbox image") == "ok":
        _append_smoke_check(checks=checks, settings=settings)

    return _diagnostic(
        settings=settings,
        server_image=server_image,
        selected_backend=selected_backend,
        checks=checks,
        next_steps=_next_steps_for_checks(checks, selected_backend=selected_backend),
    )


def pull_sandbox_images(
    images: tuple[str, ...] | list[str] | None = None,
    *,
    timeout_s: int = _DOCKER_PULL_TIMEOUT_S,
) -> SandboxPullResult:
    selected_images = tuple(dict.fromkeys(images or default_sandbox_images()))
    if not selected_images:
        return SandboxPullResult(ok=True, results=())

    docker_path = shutil.which("docker")
    if not docker_path:
        return SandboxPullResult(
            ok=False,
            results=(),
            error=(
                "Docker is not installed. Install Docker Desktop, open it, then run "
                "`alysis sandbox pull` again."
            ),
        )

    docker_info = _run_docker_info()
    if docker_info.status != "ok":
        return SandboxPullResult(
            ok=False,
            results=(),
            error=_docker_not_running_message(),
        )

    results: list[SandboxImagePullResult] = []
    for image in selected_images:
        try:
            proc = subprocess.run(
                ["docker", "pull", image],
                check=False,
                capture_output=True,
                **command_text(),
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            results.append(
                SandboxImagePullResult(
                    image=image,
                    ok=False,
                    output=f"docker pull timed out after {timeout_s}s",
                )
            )
            continue
        except OSError as exc:
            results.append(
                SandboxImagePullResult(
                    image=image,
                    ok=False,
                    output=_compact_detail(str(exc)),
                )
            )
            continue
        output = _compact_detail(_combined_output(proc.stdout, proc.stderr), max_chars=4000)
        results.append(
            SandboxImagePullResult(
                image=image,
                ok=proc.returncode == 0,
                output=output,
            )
        )
    return SandboxPullResult(ok=all(item.ok for item in results), results=tuple(results))


def _needs_sudo() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        return True
    try:
        return geteuid() != 0
    except OSError:
        return True


def detect_bubblewrap_install_plan() -> BubblewrapInstallPlan | None:
    """Return how to install Bubblewrap on this host, or ``None`` if not applicable.

    Returns ``None`` when not on Linux, when ``bwrap`` is already installed, when no
    supported package manager is found, or when elevation is needed but ``sudo`` is
    unavailable.
    """
    if not _is_linux() or shutil.which("bwrap"):
        return None
    needs_sudo = _needs_sudo()
    sudo_path = shutil.which("sudo")
    if needs_sudo and not sudo_path:
        return None
    prefix: tuple[str, ...] = ("sudo",) if needs_sudo else ()
    for manager, base_cmd, base_refresh in _BWRAP_INSTALL_MANAGERS:
        if not shutil.which(manager):
            continue
        command = (*prefix, *base_cmd)
        refresh = (*prefix, *base_refresh) if base_refresh else None
        steps = (refresh, command) if refresh else (command,)
        return BubblewrapInstallPlan(
            manager=manager,
            command=command,
            display=" && ".join(" ".join(step) for step in steps),
            refresh_command=refresh,
        )
    return None


def install_bubblewrap(
    *,
    plan: BubblewrapInstallPlan | None = None,
    timeout_s: int = _BWRAP_INSTALL_TIMEOUT_S,
) -> BubblewrapInstallResult:
    """Install Bubblewrap via the host package manager.

    The package manager runs with inherited stdio so an interactive ``sudo`` password
    prompt and progress output reach the user directly.
    """
    plan = plan or detect_bubblewrap_install_plan()
    if plan is None:
        return BubblewrapInstallResult(
            ok=False,
            command="",
            detail=(
                "No supported Linux package manager was found to install Bubblewrap "
                "automatically. Install `bubblewrap` manually, or use Docker."
            ),
        )
    refresh_note = ""
    if plan.refresh_command:
        try:
            refresh = subprocess.run(list(plan.refresh_command), check=False, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return BubblewrapInstallResult(
                ok=False,
                command=plan.display,
                detail=f"package index refresh timed out after {timeout_s}s",
            )
        except OSError as exc:
            return BubblewrapInstallResult(ok=False, command=plan.display, detail=str(exc))
        if refresh.returncode == _APT_PARTIAL_REFRESH_EXIT:
            refresh_note = (
                f" (the package index refresh also reported errors, exit code {refresh.returncode})"
            )
        elif refresh.returncode != 0:
            return BubblewrapInstallResult(
                ok=False,
                command=plan.display,
                detail=f"package index refresh exited with code {refresh.returncode}",
            )
    try:
        proc = subprocess.run(list(plan.command), check=False, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return BubblewrapInstallResult(
            ok=False,
            command=plan.display,
            detail=f"install timed out after {timeout_s}s",
        )
    except OSError as exc:
        return BubblewrapInstallResult(ok=False, command=plan.display, detail=str(exc))
    if proc.returncode != 0:
        return BubblewrapInstallResult(
            ok=False,
            command=plan.display,
            detail=f"package manager exited with code {proc.returncode}{refresh_note}",
        )
    if not shutil.which("bwrap"):
        return BubblewrapInstallResult(
            ok=False,
            command=plan.display,
            detail="bubblewrap (`bwrap`) is still not on PATH after the install completed",
        )
    return BubblewrapInstallResult(ok=True, command=plan.display, detail="bubblewrap installed")


def format_sandbox_problem_message(result: SandboxDiagnostic) -> str:
    if result.ready:
        if result.status == "disabled":
            return "Alysis Code sandboxing is disabled by configuration."
        if result.selected_backend == "windows-native":
            return "Alysis Code Windows sandbox preview passed its live checks (experimental)."
        if result.selected_backend == "windows-mxc":
            return "Alysis Code Windows beta sandbox passed the requested local checks."
        return "Alysis Code sandbox is ready."

    lines = [
        "Alysis Code needs a safe runner before it can execute tests and shell commands.",
        "",
        "A safe runner isolates command execution from the rest of your computer.",
        "",
        "Status: not ready",
    ]
    if result.selected_backend:
        lines.append(f"Selected backend: {result.selected_backend}")
    lines.append("")
    lines.append("Next step:")
    for step in result.next_steps:
        lines.append(f"- {step}")
    return "\n".join(lines)


def _diagnostic(
    *,
    settings: ShellSandboxSettings,
    server_image: str,
    selected_backend: str | None,
    checks: list[SandboxCheck],
    next_steps: tuple[str, ...],
) -> SandboxDiagnostic:
    ready = _checks_ready(checks, selected_backend=selected_backend)
    can_pull = any(check.name.endswith("image") and check.status == "missing" for check in checks)
    return SandboxDiagnostic(
        ready=ready,
        status="ready" if ready else "not_ready",
        configured_mode=settings.mode,
        configured_backend=settings.backend,
        selected_backend=selected_backend,
        docker_image=settings.docker_image,
        server_image=server_image,
        checks=tuple(checks),
        next_steps=next_steps,
        can_pull=can_pull,
    )


def _select_configured_backend(settings: ShellSandboxSettings) -> str | None:
    if settings.backend == "seatbelt" or (
        settings.backend == "auto"
        and platform.system() == "Darwin"
        and os.path.exists(SEATBELT_EXECUTABLE)
    ):
        return "seatbelt"
    if settings.backend == "auto" and platform.system() == "Windows":
        return "windows-mxc"
    if settings.backend in {"windows-native", "windows-mxc"}:
        return settings.backend
    if settings.backend == "bwrap":
        return "bwrap"
    if settings.backend == "docker":
        return "docker"

    if _is_linux() and shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("docker"):
        return "docker"
    return None


def _diagnose_windows_mxc(
    settings: ShellSandboxSettings, *, include_smoke: bool = False
) -> SandboxDiagnostic:
    from dataclasses import replace

    from .config import ConfigError
    from .windows_mxc import WindowsMxcRunner, resolve_helper

    checks = [
        SandboxCheck(
            "qualification",
            "skipped",
            "Windows beta sandbox. These local checks do not certify every host or workload.",
        )
    ]
    try:
        resolve_helper()
        checks.append(
            SandboxCheck(
                "native capability",
                "ok",
                "Pinned strict MXC helper supports this host. This does not qualify workspace policy or production use.",
            )
        )
        if include_smoke:
            # Independent of project content; probe execution and outside access.
            with tempfile.TemporaryDirectory(prefix="alysis-mxc-doctor-") as temporary:
                base = Path(temporary)
                root = base / "workspace"
                root.mkdir()
                outside = base / "outside.txt"
                outside.write_text("unchanged", encoding="utf-8")
                escaped = str(outside).replace("'", "''")
                command = (
                    "$ErrorActionPreference='Stop'; "
                    "$readBlocked=$false; $writeBlocked=$false; "
                    f"try {{ [IO.File]::ReadAllText('{escaped}') | Out-Null }} "
                    "catch { $readBlocked=$true }; "
                    f"try {{ [IO.File]::WriteAllText('{escaped}','escaped') }} "
                    "catch { $writeBlocked=$true }; "
                    "if (!$readBlocked -or !$writeBlocked) { exit 41 }; "
                    f"Write-Output '{_READY_SMOKE_OUTPUT}'"
                )
                probe_settings = replace(settings, additional_workspace_roots=())
                completed = WindowsMxcRunner(probe_settings).run(
                    root=root, cwd=root, cmd=command, timeout_s=_SANDBOX_SMOKE_TIMEOUT_S
                )
                if (
                    completed.returncode != 0
                    or _READY_SMOKE_OUTPUT not in completed.stdout
                    or outside.read_text(encoding="utf-8") != "unchanged"
                ):
                    raise ConfigError("Windows sandbox execution/containment smoke failed.")
                from .contained_read_tools import ContainedReadTools

                (root / "sandbox-doctor.txt").write_text("fixture", encoding="utf-8")
                worker = ContainedReadTools(probe_settings, root)
                result = worker.run("fs_list", {})
                if "sandbox-doctor.txt" not in str(result):
                    raise ConfigError("Contained CLI file worker smoke failed.")
                if probe_settings.filesystem_access == "workspace-write":
                    worker.run_write("fs_mkdir", {"path": "worker-created"})
                    if not (root / "worker-created").is_dir():
                        raise ConfigError("Contained CLI mutation worker smoke failed.")
            checks.append(
                SandboxCheck(
                    "native execution and containment",
                    "ok",
                    "Command ran; outside reads and writes were denied.",
                )
            )
    except (ConfigError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        checks.append(SandboxCheck("native capability", "failed", str(exc)))
    ready = not any(check.status == "failed" for check in checks)
    return SandboxDiagnostic(
        ready=ready,
        status="ready" if ready else "not_ready",
        configured_mode=settings.mode,
        configured_backend=settings.backend,
        selected_backend="windows-mxc",
        docker_image=settings.docker_image,
        server_image=resolve_server_sandbox_image(),
        checks=tuple(checks),
        next_steps=()
        if ready
        else (
            "Reinstall the CLI if its helper is missing or damaged. Use a supported Windows x64 host and a local NTFS project. No command runs outside containment as a fallback.",
        ),
    )


def _diagnose_windows_native(
    settings: ShellSandboxSettings, *, include_smoke: bool
) -> SandboxDiagnostic:
    from .config import ConfigError
    from .windows_sandbox import WindowsNativeRunner, helper_status

    checks = [
        SandboxCheck(
            "qualification",
            "skipped",
            "Experimental, opt-in shell backend. Hooks, MCP, host file tools and computer use retain separate permissions. Durable services and destination-scoped network approvals are not supported.",
        ),
        SandboxCheck(
            "Node subprocess compatibility",
            "skipped",
            "Node runtimes without the libuv AppContainer pipe fix can hang when creating subprocesses, including the default test runner. A fixed runtime needs separate qualification; launch smoke does not test it. Commands are never rewritten or run outside the sandbox to bypass this limitation.",
        ),
    ]
    ready = False
    smoke_root: Path | None = None
    try:
        status = helper_status()
        user = status.get("user", {})
        provisioned = user.get("user", {})
        if (
            not provisioned.get("exists")
            or not user.get("cred_present")
            or user.get("marker_version") != 3
        ):
            raise ConfigError("Native sandbox account is missing or setup requires repair.")
        if user.get("owner_user_sid") != user.get("real_user_sid"):
            raise ConfigError("This experimental installation belongs to a different Windows user.")
        ambient = status.get("ambient", {}).get("paths", [])
        if not ambient or any(item.get("present") is False for item in ambient):
            raise ConfigError("Native sandbox ambient filesystem protection needs repair.")
        checks.append(
            SandboxCheck(
                "native setup",
                "ok",
                "Account and ambient protection records are present; commands verify filesystem and network enforcement before launch.",
            )
        )
        if settings.network not in {"off", "on"}:
            raise ConfigError("The native sandbox requires an explicit off/on network policy.")
        checks.append(
            SandboxCheck(
                "network policy",
                "ok",
                "Outbound internet capability requested; no destination allowlist. Localhost/private-network server capabilities are not granted."
                if settings.network == "on"
                else "No network capabilities requested; network access is blocked.",
            )
        )
        if settings.network == "on":
            checks.append(
                SandboxCheck(
                    "network client compatibility",
                    "skipped",
                    "Public HTTPS qualification remains unresolved. PowerShell and Windows curl using Schannel fail with SEC_E_NO_CREDENTIALS under the restricted token; Node HTTPS also needs reliable host and sandbox controls. Doctor smoke makes no internet request.",
                )
            )
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        if not local_app_data.is_absolute():
            raise ConfigError("Native diagnostics require an absolute LOCALAPPDATA directory.")
        pending = local_app_data / "alysis-sandbox" / "execution-pending.json"
        if _native_recovery_pending(pending):
            raise ConfigError(
                "An execution is active or cleanup was interrupted; stop active commands and inspect the native recovery journal."
            )
        if include_smoke:
            # Never use TemporaryDirectory here: its exception cleanup can
            # delete an object still recorded in the native recovery journal.
            smoke_root = Path(tempfile.mkdtemp(prefix="alysis-native-smoke-"))
            original = smoke_root.lstat()
            canonical = smoke_root.resolve(strict=True)
            try:
                result = WindowsNativeRunner(settings).run(
                    root=smoke_root,
                    cwd=smoke_root,
                    cmd=f"Write-Output '{_READY_SMOKE_OUTPUT}'",
                    timeout_s=45,
                )
                if result.returncode or result.stdout.strip() != _READY_SMOKE_OUTPUT:
                    raise ConfigError(f"Native launch smoke failed: {result.stderr.strip()}")
                if _native_recovery_pending(pending):
                    raise ConfigError("Native smoke left an unfinished recovery journal.")
                observed = smoke_root.lstat()
                if (
                    not stat.S_ISDIR(observed.st_mode)
                    or (observed.st_dev, observed.st_ino) != (original.st_dev, original.st_ino)
                    or smoke_root.resolve(strict=True) != canonical
                ):
                    raise ConfigError("Native smoke workspace identity changed; refusing cleanup.")
                shutil.rmtree(smoke_root)
                smoke_root = None
            except BaseException as exc:
                # Also retain/report on Ctrl+C, outside the ordinary diagnostic
                # error envelope. Recovery must precede operator removal.
                exc.add_note(f"Native diagnostic workspace retained for recovery: {smoke_root}")
                raise
            ready = True
            checks.append(
                SandboxCheck(
                    "native launch and policy",
                    "ok",
                    f"PowerShell launched with verified network={settings.network} AppContainer capabilities and independent write restrictions. Setup probes passed. This smoke does not contact an internet endpoint; full release qualification remains separate.",
                )
            )
        else:
            checks.append(
                SandboxCheck(
                    "native launch and policy",
                    "skipped",
                    "Run doctor with --smoke to verify actual enforcement. Setup presence alone does not establish readiness.",
                )
            )
    except (ConfigError, OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        checks.append(SandboxCheck("native sandbox", "failed", str(exc)))
    finally:
        if smoke_root is not None:
            checks.append(
                SandboxCheck(
                    "retained diagnostic workspace",
                    "skipped",
                    f"Preserved {smoke_root}. Keep it intact; run alysis sandbox recover-native before removing it. Do not delete the recovery journal.",
                )
            )
    return SandboxDiagnostic(
        ready=ready,
        status="experimental_ready" if ready else "not_ready",
        configured_mode=settings.mode,
        configured_backend=settings.backend,
        selected_backend="windows-native",
        docker_image=settings.docker_image,
        server_image=resolve_server_sandbox_image(),
        checks=tuple(checks),
        next_steps=()
        if ready
        else (
            "Build or install the native helper, then run alysis sandbox setup --native-windows (UAC consent).",
            "Set ALYSIS_SHELL_SANDBOX_BACKEND=windows-native and ALYSIS_SHELL_SANDBOX_NETWORK=off, then run alysis sandbox doctor --smoke.",
            "For WSL2, install bubblewrap inside the Linux distribution instead.",
        ),
    )


def _native_recovery_pending(journal: Path) -> bool:
    # Only definitive absence permits cleanup. Path.exists may hide an access
    # error on some Python versions; a symlink is also an unfinished record.
    try:
        journal.lstat()
    except FileNotFoundError:
        return False
    return True


def _is_linux() -> bool:
    return platform.system().lower() == "linux"


def _resolve_server_worker_backend() -> str:
    raw = str(env_get("ALYSIS_SERVER_WORKER_BACKEND") or "").strip().lower()
    if raw in {"bwrap", "docker"}:
        return raw
    if _is_linux():
        return "bwrap"
    return "docker"


def _run_docker_info() -> SandboxCheck:
    if not shutil.which("docker"):
        return SandboxCheck("Docker daemon", "skipped", "Docker CLI is not installed.")
    try:
        proc = subprocess.run(
            ["docker", "info"],
            check=False,
            capture_output=True,
            **command_text(),
            timeout=_DOCKER_INFO_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxCheck("Docker daemon", "failed", str(exc))
    if proc.returncode == 0:
        return SandboxCheck("Docker daemon", "ok", "running")
    return SandboxCheck(
        "Docker daemon",
        "failed",
        _docker_info_failure_detail(stdout=proc.stdout, stderr=proc.stderr),
    )


def _docker_image_check(image: str, *, label: str) -> SandboxCheck:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            capture_output=True,
            **command_text(),
            timeout=_DOCKER_IMAGE_INSPECT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxCheck(label, "failed", str(exc))
    if proc.returncode == 0:
        return SandboxCheck(label, "ok", image)
    return SandboxCheck(label, "missing", f"{image} is not downloaded locally.")


def _append_smoke_check(*, checks: list[SandboxCheck], settings: ShellSandboxSettings) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="alysis-sandbox-smoke-") as raw_tmp:
            root = Path(raw_tmp)
            runner = build_shell_runner_from_settings(settings, root, warning_callback=None)
            result = runner.run(
                root=root,
                cwd=root,
                cmd=f"printf {_READY_SMOKE_OUTPUT}",
                timeout_s=_SANDBOX_SMOKE_TIMEOUT_S,
            )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        checks.append(SandboxCheck("sandbox smoke test", "failed", str(exc)))
        return
    output = _combined_output(result.stdout, result.stderr).strip()
    if result.returncode == 0 and output == _READY_SMOKE_OUTPUT:
        checks.append(SandboxCheck("sandbox smoke test", "ok", "command executed in sandbox"))
        return
    detail = output or f"exit code {result.returncode}"
    checks.append(SandboxCheck("sandbox smoke test", "failed", detail))


def _checks_ready(checks: list[SandboxCheck], *, selected_backend: str | None) -> bool:
    if selected_backend is None:
        return False
    required_names = {"selected backend"}
    if selected_backend == "bwrap":
        required_names.add("bubblewrap")
    elif selected_backend == "docker":
        required_names.update({"Docker CLI", "Docker daemon", "sandbox image"})
    if _check_status(checks, "Docker daemon") is not None:
        required_names.update({"Docker CLI", "Docker daemon"})
    if _check_status(checks, "server sandbox image") is not None:
        required_names.add("server sandbox image")
    if any(check.status == "failed" for check in checks):
        return False
    for check in checks:
        if check.name in required_names and check.status != "ok":
            return False
    smoke_status = _check_status(checks, "sandbox smoke test")
    return smoke_status in {None, "ok"}


def _check_status(checks: list[SandboxCheck], name: str) -> SandboxCheckStatus | None:
    for check in checks:
        if check.name == name:
            return check.status
    return None


def _next_steps_for_checks(
    checks: list[SandboxCheck],
    *,
    selected_backend: str,
) -> tuple[str, ...]:
    if _check_status(checks, "Docker daemon") not in {None, "ok"}:
        return _docker_daemon_steps(docker_path=shutil.which("docker"))

    if selected_backend == "bwrap":
        if _check_status(checks, "bubblewrap") == "skipped":
            return _bwrap_linux_only_steps()
        if _check_status(checks, "bubblewrap") != "ok":
            return _bwrap_missing_steps(settings=None)
        if _check_status(checks, "sandbox smoke test") == "failed":
            return (
                "Bubblewrap is installed but the sandbox smoke test failed. Check kernel user namespace support or set ALYSIS_SHELL_SANDBOX_BACKEND=docker.",
                "Run `alysis doctor sandbox` again after fixing the host sandbox runtime.",
            )
        if _check_status(checks, "server sandbox image") == "missing":
            return (
                "Run `alysis sandbox pull --server` to download Alysis Code's server worker image.",
            )
        return ("Sandbox is ready.",)

    if _check_status(checks, "Docker CLI") != "ok":
        return ("Install Docker Desktop, open it, then run `alysis doctor sandbox`.",)
    if _check_status(checks, "Docker daemon") != "ok":
        return _docker_daemon_steps(docker_path=shutil.which("docker"))
    if _check_status(checks, "sandbox image") == "missing":
        return ("Run `alysis sandbox pull` to download Alysis Code's safe runner image.",)
    if _check_status(checks, "sandbox smoke test") == "failed":
        return (
            "Docker is running, but the sandbox smoke test failed. Run `alysis doctor sandbox` and inspect the smoke test detail.",
        )
    if _check_status(checks, "server sandbox image") == "missing":
        return (
            "Run `alysis sandbox pull --server` to download Alysis Code's server worker image.",
        )
    return ("Sandbox is ready.",)


def _install_backend_steps() -> tuple[str, ...]:
    if platform.system().lower() == "linux":
        return (
            "Install Bubblewrap with your Linux package manager, then run `alysis doctor sandbox`.",
            "Or install Docker, start Docker, then run `alysis sandbox pull`.",
        )
    return ("Install Docker Desktop, open it, then run `alysis sandbox pull`.",)


def _bwrap_missing_steps(settings: ShellSandboxSettings | None) -> tuple[str, ...]:
    backend_note = ""
    if settings is not None and settings.backend == "bwrap":
        backend_note = " because shell_sandbox.backend=bwrap is configured"
    return (
        f"Install Bubblewrap (`bwrap`){backend_note}, then run `alysis doctor sandbox`.",
        "Or set ALYSIS_SHELL_SANDBOX_BACKEND=docker and run `alysis sandbox pull`.",
    )


def _bwrap_linux_only_steps() -> tuple[str, ...]:
    return (
        "Bubblewrap is Linux-only. Install Docker Desktop, set ALYSIS_SHELL_SANDBOX_BACKEND=docker, then run `alysis sandbox pull`.",
    )


def _docker_daemon_steps(*, docker_path: str | None) -> tuple[str, ...]:
    if docker_path:
        return (
            "Docker is installed, but it is not running. Open Docker Desktop or start the Docker service, then run `alysis doctor sandbox`.",
        )
    return ("Install Docker Desktop, open it, then run `alysis sandbox pull`.",)


def _docker_not_running_message() -> str:
    return (
        "Docker is installed, but it is not running. Open Docker Desktop or start the Docker "
        "service, then run `alysis sandbox pull` again."
    )


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(part for part in ((stdout or "").strip(), (stderr or "").strip()) if part)


def _docker_info_failure_detail(*, stdout: str | None, stderr: str | None) -> str:
    combined = _combined_output(stdout, stderr)
    important_lines: list[str] = []
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if any(
            marker in lowered
            for marker in (
                "cannot connect",
                "failed to connect",
                "docker daemon",
                "permission denied",
            )
        ):
            important_lines.append(line)
    if important_lines:
        return _compact_detail(" ".join(important_lines))
    return _compact_detail(combined)


def _compact_detail(value: str, *, max_chars: int = 700) -> str:
    text = value.strip()
    if not text:
        return "(no output)"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def sandbox_env_summary() -> dict[str, str | None]:
    keys = (
        "ALYSIS_SHELL_SANDBOX_MODE",
        "ALYSIS_SHELL_SANDBOX_BACKEND",
        "ALYSIS_SHELL_SANDBOX_NETWORK",
        "ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE",
        "ALYSIS_VERIFY_SANDBOX_MODE",
    )
    return {key: os.environ.get(key) for key in keys}
