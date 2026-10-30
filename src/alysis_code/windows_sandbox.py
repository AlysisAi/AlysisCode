"""Experimental native Windows shell backend shared by CLI and desktop.

The helper is pinned by digest, owns the execution lease and native jobs, and
fails closed. It is deliberately opt-in until installed-host qualification.
Host file tools, hooks, MCP servers and computer use are separate authorities.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .branding import env_get
from .config import ConfigError
from .process_reaping import CommandCancelledError, CommandCleanupError, communicate_until_cancelled
from .sandbox_bookkeeping import PROTECTED_INSTRUCTION_FILES
from .sandbox_settings import ShellSandboxSettings
from .skills.discovery import project_skill_root_relative_paths

_ASSETS = Path(__file__).parent / "assets" / "windows-sandbox"
PROTOCOL_VERSION = 3
SECURITY_REVISION = 4
CONTAINMENT = "appcontainer-write-restricted"
_PROTECTED_DIRS = (".git", ".alysis", ".sylliptor", ".codex", ".claude", ".agents")
_PROTECTED_FILES = PROTECTED_INSTRUCTION_FILES
# Fixed trusted compatibility flags, never inherited from the host or caller.
# Node's default realpath walks C:\\ and private ancestors while resolving
# scripts/dependencies. These flags retain the granted workspace paths. Windows
# ACLs still enforce access, and pre-existing workspace aliases remain rejected.
_NODE_OPTIONS = "--preserve-symlinks --preserve-symlinks-main"
_EXECUTION_POLICIES = frozenset(
    {"Restricted", "AllSigned", "RemoteSigned", "Unrestricted", "Bypass"}
)
_EXECUTION_POLICY_QUERY = (
    "$ErrorActionPreference='Stop'; [Console]::Out.Write(("
    r"Microsoft.PowerShell.Security\Get-ExecutionPolicy).ToString())"
)
_EXECUTION_POLICY_QUERY_TIMEOUT_S = 10
_SAFE_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "LANG",
        "CI",
        "NO_COLOR",
        "TERM",
        "PORT",
        "HOST",
        "GIT_TERMINAL_PROMPT",
    }
)


def _powershell_execution_policy(shell: Path) -> str:
    """Read the trusted host policy; never evaluate workspace commands here.

    AppContainer cannot read the host's CurrentUser policy key. Snapshot the
    effective host policy for this request and give it to the isolated process.
    PowerShell still evaluates MachinePolicy/UserPolicy before Process scope.
    No registry settings or ACLs are changed, and a failed query has no fallback.
    """
    system_root = shell.parents[3]
    environment = {
        "SYSTEMROOT": str(system_root),
        "WINDIR": str(system_root),
        "PATH": os.pathsep.join((str(shell.parent), str(system_root / "System32"))),
        "PSModulePath": str(shell.parent / "Modules"),
    }
    # This is the trusted engine's process preference, not request env_overrides.
    # Dropping a caller's AllSigned/Restricted setting could weaken its policy.
    process_policy = os.environ.get("PSExecutionPolicyPreference")
    if process_policy is not None:
        if process_policy not in _EXECUTION_POLICIES | {"Undefined"}:
            raise ConfigError(
                "Invalid host Windows PowerShell process policy; no sandbox command was launched."
            )
        environment["PSExecutionPolicyPreference"] = process_policy
    try:
        result = subprocess.run(
            [
                str(shell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _EXECUTION_POLICY_QUERY,
            ],
            cwd=shell.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_EXECUTION_POLICY_QUERY_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        reason = f"query timed out after {_EXECUTION_POLICY_QUERY_TIMEOUT_S} seconds"
    except OSError as exc:
        # Numeric codes identify startup failures without exposing paths or the
        # OS exception message. Captured output is never part of diagnostics.
        winerror = getattr(exc, "winerror", None)
        if type(winerror) is int:
            reason = f"query process could not start (winerror={winerror})"
        elif type(exc.errno) is int:
            reason = f"query process could not start (errno={exc.errno})"
        else:
            reason = "query process could not start (OS error code unavailable)"
    except ValueError:
        reason = "query process could not start (invalid arguments)"
    except subprocess.SubprocessError:
        reason = "query process failed"
    else:
        if result.returncode:
            reason = f"query exited with returncode={result.returncode}"
        elif result.stderr.strip():
            reason = "query produced stderr"
        else:
            try:
                policy = result.stdout.decode("ascii").strip()
            except UnicodeError:
                reason = "query returned an invalid response"
            else:
                if policy in _EXECUTION_POLICIES:
                    return policy
                reason = "query returned an invalid response"
    raise ConfigError(
        f"Cannot read the host Windows PowerShell execution policy ({reason}); "
        "no sandbox command was launched."
    ) from None


def engine_capabilities() -> dict[str, Any]:
    """Static compatibility contract; never evidence of installation or readiness."""
    return {
        "contract": "alysis.windows-sandbox.capabilities",
        "schema_version": 1,
        "protocol": PROTOCOL_VERSION,
        "security_revision": SECURITY_REVISION,
        "platforms": ["win32-x64"],
        "containment": CONTAINMENT,
        "network_modes": ["off", "on"],
        "filesystem_modes": ["read-only", "workspace-write"],
        "qualification": "experimental-unqualified",
        "features": {
            "foreground": True,
            "session_background": True,
            "durable_services": False,
            "interactive_pty": False,
            "destination_allowlist": False,
        },
    }


def _helper_json(helper: Path, operation: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(helper), operation],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        # This is a strict UTF-8 machine protocol, rather than terminal output
        # eligible for the engine's legacy-code-page decoding fallback.
        stdout = (
            result.stdout.decode("utf-8") if isinstance(result.stdout, bytes) else result.stdout
        )
        stderr = (
            result.stderr.decode("utf-8") if isinstance(result.stderr, bytes) else result.stderr
        )
        if result.returncode:
            raise ConfigError(f"Native sandbox {operation} failed: {stderr.strip()}")
        value = json.loads(stdout)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ConfigError(f"Invalid or unavailable native sandbox {operation} response") from exc


def _verify_helper_protocol(helper: Path) -> dict[str, Any]:
    metadata = _helper_json(helper, "protocol")
    if (
        type(metadata.get("protocol")) is not int
        or metadata["protocol"] != PROTOCOL_VERSION
        or type(metadata.get("security_revision")) is not int
        or metadata["security_revision"] != SECURITY_REVISION
        or metadata.get("implementation") != "alysis-windows-sandbox"
        or metadata.get("containment") != CONTAINMENT
        or metadata.get("network_modes") != ["off", "on"]
    ):
        raise ConfigError(
            "Native sandbox helper is incompatible with this engine. Update the engine and helper together; no command was launched."
        )
    return metadata


def resolve_helper() -> Path:
    if platform.system() != "Windows":
        raise ConfigError(
            "windows-native requires native Windows. In WSL2 use the Linux bwrap backend."
        )
    override = env_get("ALYSIS_WINDOWS_SANDBOX_HELPER")
    if override:
        candidate = Path(override)
        digest = env_get("ALYSIS_WINDOWS_SANDBOX_SHA256")
        if not candidate.is_absolute() or not digest:
            raise ConfigError(
                "A development helper requires an absolute ALYSIS_WINDOWS_SANDBOX_HELPER and ALYSIS_WINDOWS_SANDBOX_SHA256."
            )
    else:
        candidate = _ASSETS / "alysis-sandbox.exe"
        try:
            manifest = json.loads((_ASSETS / "manifest.json").read_text(encoding="utf-8"))
            if type(manifest["protocol"]) is not int or manifest["protocol"] != PROTOCOL_VERSION:
                raise ValueError("unsupported protocol")
            digest = manifest["sha256"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(
                "Native Windows sandbox helper is not packaged. Build scripts/build_windows_sandbox.py or install a Windows sandbox-enabled release."
            ) from exc
    try:
        with candidate.open("rb") as binary:
            actual = hashlib.file_digest(binary, "sha256").hexdigest()
    except OSError as exc:
        raise ConfigError(f"Cannot read Windows sandbox helper: {candidate}") from exc
    if not isinstance(digest, str) or actual != digest.lower():
        raise ConfigError(
            "Windows sandbox helper integrity check failed. Reinstall the helper; no command was launched."
        )
    # A digest establishes the selected bytes, not their protocol. Development
    # pins can point at older helpers; fail before setup or execution mutates ACLs.
    _verify_helper_protocol(candidate)
    return candidate.resolve()


def _local_path(path: Path, *, directory: bool = True) -> Path:
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ConfigError(
            "Native sandbox paths must be absolute paths on a local NTFS drive; UNC paths are unsupported."
        )
    # Inspect before resolve so a junction cannot silently broaden the requested root.
    for item in (path, *path.parents):
        try:
            attrs = item.lstat().st_file_attributes
        except AttributeError:  # Pure policy tests on non-Windows hosts.
            attrs = 0
        except OSError as exc:
            raise ConfigError(f"Sandbox path is unavailable: {item}") from exc
        if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ConfigError(
                f"Sandbox roots and working directories cannot traverse reparse points: {item}"
            )
    if directory and not path.is_dir():
        raise ConfigError(f"Sandbox directory does not exist: {path}")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        fs = ctypes.create_unicode_buffer(32)
        if (
            kernel.GetDriveTypeW(path.anchor) != 3
            or not kernel.GetVolumeInformationW(path.anchor, None, 0, None, None, None, fs, len(fs))
            or fs.value != "NTFS"
        ):
            raise ConfigError("The native sandbox preview supports fixed local NTFS volumes only.")
    return path.resolve()


def build_request(
    settings: ShellSandboxSettings,
    *,
    root: Path,
    cwd: Path,
    cmd: str,
    helper: Path,
    env_overrides: dict[str, str] | None = None,
    include_helper_read: bool = True,
) -> dict[str, Any]:
    if settings.network not in {"off", "on"}:
        raise ConfigError("Native Windows network policy must be explicitly off or on.")
    if settings.filesystem_access not in {"workspace-write", "read-only"}:
        raise ConfigError("Unsupported native sandbox filesystem policy")
    if not settings.clear_env or not settings.protect_repo_meta:
        raise ConfigError("Native Windows sandbox requires clear_env and protect_repo_meta.")
    root = _local_path(root)
    cwd = _local_path(cwd)
    from .project_folders import allowed_roots

    roots = list(
        dict.fromkeys(
            [
                root,
                *(_local_path(Path(p)) for p in settings.additional_workspace_roots),
                *(_local_path(p) for p in allowed_roots(root)[1:]),
            ]
        )
    )
    if any(path == Path(path.anchor) or path == Path.home().resolve() for path in roots):
        raise ConfigError("A drive root or the entire user profile cannot be a sandbox workspace.")
    cwd_root = next((path for path in roots if cwd.is_relative_to(path)), None)
    if cwd_root is None:
        raise ConfigError("Sandbox cwd must be inside an approved workspace.")
    # A venv continues importing the standard library and native modules from its
    # base installation. Protect both installations, including the inverse overlap
    # where the selected project is a descendant of a trusted Python directory.
    runtime_roots: list[Path] = []
    for prefix in dict.fromkeys(
        (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)
    ):
        runtime = Path(prefix).resolve()
        if not any(runtime.is_relative_to(path) or path.is_relative_to(runtime) for path in roots):
            continue
        runtime = _local_path(Path(prefix))
        if settings.filesystem_access == "workspace-write" and cwd.is_relative_to(runtime):
            raise ConfigError(
                "A trusted host Python runtime cannot be a writable sandbox working directory. "
                "Choose a project folder outside the engine's Python installations."
            )
        runtime_roots.append(runtime)
    from .host_runtime_policy import desktop_runtime_roots

    if os.name == "nt":
        from .windows_mxc_bundle import runtime_cache_root

        try:
            cache = runtime_cache_root()
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if any(cache.is_relative_to(path) or path.is_relative_to(cache) for path in roots):
            if not cache.exists():
                raise ConfigError(
                    "A writable sandbox cannot contain an uninitialized helper cache. Choose a narrower project."
                )
            runtime_roots.append(_local_path(cache))

    for runtime in desktop_runtime_roots():
        if cwd.is_relative_to(runtime):
            raise ConfigError("A trusted Desktop runtime cannot be a sandbox working directory.")
        if any(runtime.is_relative_to(path) or path.is_relative_to(runtime) for path in roots):
            # A missing future runtime cannot be reserved through a broad writable
            # grant: refuse before the child can plant an installer destination.
            runtime_roots.append(_local_path(runtime))
    from .windows_toolchain import installed_tools, search_directories

    tools = installed_tools()
    tool_roots = list(
        dict.fromkeys(
            [
                *[_local_path(Path(p)) for p in settings.windows_read_roots],
                *[directory for _, directory in tools.values()],
            ]
        )
    )
    if any(
        workspace != path and workspace.is_relative_to(path)
        for path in tool_roots
        for workspace in roots
    ):
        raise ConfigError(
            "A tool read root cannot contain the workspace; choose a narrower tool directory."
        )
    helper_root = _local_path(helper.parent)
    reads = list(
        dict.fromkeys([*roots, *([helper_root] if include_helper_read else []), *tool_roots])
    )
    # Every positive recursive grant can expose another name of a hardlinked
    # file. Read-only tool/helper roots need the same checks as the workspace.
    for path in reads:
        _validate_workspace_tree(path)
    # PowerShell resolves ancestor metadata even with SeChangeNotifyPrivilege.
    # Grant only object traversal/attributes on private profile ancestors, never
    # directory listing or inherited read access to sibling projects/secrets.
    profile = Path.home().resolve()
    traverse = list(
        dict.fromkeys(
            str(parent)
            for path in reads
            for parent in path.parents
            if parent.is_relative_to(profile) and parent not in reads
        )
    )
    # Never propagate API tokens, profile paths, Python startup hooks or proxy credentials.
    env = {k.upper(): v for k, v in os.environ.items() if k.upper() in _SAFE_ENV}
    env.update({"CI": "1", "GIT_TERMINAL_PROMPT": "0"})
    for key, value in (env_overrides or {}).items():
        if key.upper() not in _SAFE_ENV:
            raise ConfigError(
                f"Environment variable {key!r} is not allowed in the native sandbox preview."
            )
        env[key.upper()] = value
    env["NODE_OPTIONS"] = _NODE_OPTIONS
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    shell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # Executable discovery and filesystem access must agree. Never inherit an
    # arbitrary host/project executable merely because it appears on PATH.
    configured_paths = [Path(p) for p in env.get("PATH", "").split(os.pathsep) if p]
    permitted_paths = [
        p
        for p in configured_paths
        if p.is_absolute() and any(p.is_relative_to(r) for r in tool_roots)
    ]
    env["PATH"] = os.pathsep.join(
        map(
            str,
            dict.fromkeys(
                [
                    *search_directories(tools),
                    shell.parent,
                    system_root / "System32",
                    *permitted_paths,
                ]
            ),
        )
    )
    # PowerShell source is a single argv element. The host never evaluates it.
    # PowerShell's default C: drive tries to enumerate ancestors that an
    # AppContainer cannot read. A process-local drive starts at the granted
    # workspace, preserving relative commands without granting ancestor listing.
    ps_root = str(cwd_root).replace("'", "''")
    relative_cwd = str(cwd.relative_to(cwd_root)).replace("/", "\\")
    ps_cwd = ("Alysis:\\" + ("" if relative_cwd == "." else relative_cwd)).replace("'", "''")
    source = (
        f"New-PSDrive -Name Alysis -PSProvider FileSystem -Root '{ps_root}' "
        "-Scope Global -ErrorAction Stop | Out-Null; "
        f"Set-Location -LiteralPath '{ps_cwd}' -ErrorAction Stop; " + cmd
    )
    argv = [
        str(shell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        _powershell_execution_policy(shell),
        "-Command",
        source,
    ]
    protected_dirs = [workspace / name for workspace in roots for name in _PROTECTED_DIRS]
    for workspace in roots:
        for parts in project_skill_root_relative_paths():
            path = workspace.joinpath(*parts)
            if not any(path.is_relative_to(existing) for existing in protected_dirs):
                protected_dirs.append(path)
    deny_write = [str(path) + (os.sep if not path.exists() else "") for path in protected_dirs]
    # Reserve absent instruction files too: otherwise a command could plant
    # trusted-looking instructions for the host agent's next turn. The helper
    # materializes empty, protected file placeholders before granting writes.
    protected_files = [workspace / name for workspace in roots for name in _PROTECTED_FILES]
    trusted_paths = [
        *protected_dirs,
        *protected_files,
        helper_root,
        Path(__file__).parent.resolve(),
        *runtime_roots,
    ]
    if getattr(sys, "frozen", False):
        trusted_paths.append(_local_path(Path(sys.executable), directory=False))
    if settings.filesystem_access == "workspace-write" and any(
        workspace.is_relative_to(path) for workspace in roots for path in trusted_paths
    ):
        raise ConfigError(
            "A writable workspace cannot be inside protected metadata or a trusted host runtime."
        )
    deny_write += [str(path) for path in protected_files]
    for path in [*protected_dirs, *protected_files]:
        if path.exists():
            _local_path(path, directory=False)
    deny_write += [str(path) for path in tool_roots if path not in roots]
    # Protect the trusted engine and helper even when developing their own repository.
    deny_write += [str(helper_root), str(Path(__file__).parent)]
    # The sandbox must not replace dependencies later imported by its host.
    deny_write += [str(runtime) for runtime in runtime_roots]
    if getattr(sys, "frozen", False):
        deny_write.append(str(Path(sys.executable)))
    if settings.filesystem_access == "read-only":
        deny_write.extend(map(str, roots))
    deny_read = [
        str(Path.home() / name)
        for name in (".ssh", ".aws", ".azure", ".gnupg", ".alysis", ".sylliptor")
        if (Path.home() / name).exists()
    ]
    return {
        "protocol": PROTOCOL_VERSION,
        "cwd": str(cwd),
        "argv": argv,
        "read": list(dict.fromkeys(map(str, reads))),
        "traverse": traverse,
        "write": list(map(str, roots)) if settings.filesystem_access == "workspace-write" else [],
        "deny_read": deny_read,
        "deny_write": list(dict.fromkeys(deny_write)),
        "env": sorted(env.items()),
        "network": settings.network,
    }


def _validate_workspace_tree(root: Path) -> None:
    """Reject existing aliases before any inheritable read/write grant is changed.

    Windows hardlinks share a security descriptor and file content. Granting
    one name can expose another name outside the granted tree. Reparse points
    are also unsupported in this preview. This snapshot does not close
    concurrent host-side replacement races during native ACL propagation.
    """
    pending = [root]
    try:
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    # DirEntry.stat on Windows reports st_nlink as zero. A
                    # fresh path stat opens the object and reads its link count.
                    info = os.stat(entry.path, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & (
                        stat.FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        raise ConfigError(
                            f"Native sandbox preview cannot grant a tree containing reparse points: {entry.path}"
                        )
                    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        raise ConfigError(
                            f"Native sandbox preview cannot grant a tree containing hardlinked files: {entry.path}"
                        )
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
    except OSError as exc:
        raise ConfigError(f"Cannot inspect sandbox grant tree safely: {exc}") from exc


def _parent_identity() -> dict[str, int]:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        raise ConfigError("Cannot identify the engine process for sandbox lifecycle ownership")
    return {
        "parent_pid": os.getpid(),
        "parent_created": times[0].dwHighDateTime << 32 | times[0].dwLowDateTime,
    }


def helper_status() -> dict[str, Any]:
    helper = resolve_helper()
    return _helper_json(helper, "status")


@dataclass(frozen=True)
class WindowsNativeRunner:
    settings: ShellSandboxSettings
    close_stdin: bool = True

    def _prepare(
        self, *, root: Path, cwd: Path, cmd: str, env_overrides: dict[str, str] | None = None
    ) -> tuple[list[str], bytes]:
        helper = resolve_helper()
        request = build_request(
            self.settings, root=root, cwd=cwd, cmd=cmd, helper=helper, env_overrides=env_overrides
        )
        from .windows_private_state import protect_private_state

        previous_denials = set(request["deny_read"])
        protect_private_state(request)
        if set(request["deny_read"]) - previous_denials:
            # The legacy backend has no MXC lifetime pins for control-state
            # ancestors. Refuse intersecting scope rather than rely on ACLs
            # attached only to a replaceable child path.
            raise ConfigError("Legacy sandbox folders cannot overlap private application state.")
        from .host_runtime_policy import desktop_runtime_roots

        if any(
            runtime.is_relative_to(Path(grant)) or Path(grant).is_relative_to(runtime)
            for runtime in desktop_runtime_roots()
            for grant in request["write"]
        ):
            raise ConfigError(
                "Legacy sandbox writable folders cannot overlap trusted Desktop runtimes."
            )
        from ._windows_sandbox_bookkeeping import prepare_instruction_files

        # This runs only for an approved actual native execution, after policy
        # validation. Empty reservations persist; the helper still protects them.
        for workspace in request["write"]:
            if Path(workspace) == root.resolve():
                prepare_instruction_files(Path(workspace))
            else:
                from .sandbox_bookkeeping import capture_instruction_creations

                with capture_instruction_creations(Path(workspace)):
                    prepare_instruction_files(Path(workspace))
        request.update(_parent_identity())
        return [str(helper), "run-request"], json.dumps(request, ensure_ascii=False).encode("utf-8")

    def _execution_context(self, argv: list[str], request: bytes) -> str | None:
        return None

    def run(
        self,
        *,
        root: Path,
        cwd: Path,
        cmd: str,
        timeout_s: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if cancel_check is not None and cancel_check():
            raise CommandCancelledError(cmd)
        argv, request = self._prepare(root=root, cwd=cwd, cmd=cmd)
        execution_context = self._execution_context(argv, request)
        if cancel_check is not None and cancel_check():
            raise CommandCancelledError(cmd)
        # The trusted helper waits for its JSON request before launching work.
        # Attach an engine-owned job before sending that request, so cancellation
        # can acknowledge all descendants have drained, not just helper exit.
        with subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd
        ) as child:
            job = None
            try:
                if os.name == "nt":
                    from ._windows_sandbox_job import WindowsSandboxJob

                    job = WindowsSandboxJob(child)
                try:
                    if cancel_check is None:
                        stdout, stderr = child.communicate(request, timeout=timeout_s)
                    else:
                        stdout, stderr = communicate_until_cancelled(
                            child, input=request, timeout=timeout_s, cancel_check=cancel_check
                        )
                except BaseException as exc:
                    drain_error = None
                    try:
                        if job is not None:
                            job.terminate_and_drain()
                        else:
                            child.kill()
                    except BaseException as error:
                        drain_error = error
                        # Closing the owned kill-on-close job still requests
                        # termination. Failure to prove drain must not be
                        # returned as a successful cancellation acknowledgment.
                        if job is not None:
                            job.close()
                        if child.poll() is None:
                            child.kill()
                    stdout, stderr = child.communicate(timeout=10)
                    if isinstance(exc, (CommandCancelledError, subprocess.TimeoutExpired)):
                        exc.stdout = stdout.decode("utf-8", errors="replace")
                        exc.stderr = stderr.decode("utf-8", errors="replace")
                        if isinstance(exc, CommandCancelledError):
                            exc.cmd = cmd
                    if drain_error is not None:
                        raise CommandCleanupError(
                            cmd,
                            stdout=stdout.decode("utf-8", errors="replace"),
                            stderr=stderr.decode("utf-8", errors="replace"),
                        ) from drain_error
                    raise
                if job is not None:
                    job.wait_empty()
            finally:
                if job is not None:
                    job.close()
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)
        completed = subprocess.CompletedProcess(
            cmd,
            child.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
        if execution_context is not None:
            completed.execution_context = execution_context
        return completed

    def start(
        self, *, root: Path, cwd: Path, cmd: str, env_overrides: dict[str, str] | None = None
    ):
        from .background_runner import BackgroundProcessSpawn

        argv, request = self._prepare(root=root, cwd=cwd, cmd=cmd, env_overrides=env_overrides)
        child = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd
        )
        try:
            assert child.stdin is not None
            child.stdin.write(request)
            child.stdin.close()
        except BaseException:
            child.kill()
            child.wait()
            raise

        def cleanup() -> None:
            if child.poll() is None:
                child.kill()
            child.wait()

        return BackgroundProcessSpawn(
            popen=child, cleanup=cleanup, started_argv=tuple(argv), termination_mode="direct"
        )
