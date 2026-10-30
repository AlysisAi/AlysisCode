"""Strict Windows sandbox shared by standalone CLI and Desktop beta packages."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

from .branding import env_get
from .config import ConfigError
from .windows_mxc_contract import UPSTREAM_COMMIT as UPSTREAM_COMMIT
from .windows_mxc_contract import WINDOWS_RUNTIME_DIRECTORIES, helper_contract
from .windows_sandbox import (
    WindowsNativeRunner,
    _helper_json,
    _local_path,
    _parent_identity,
    build_request,
)

_foreground_reservations = ContextVar("mxc_foreground_reservations", default=None)


def _directory_discovery_roots() -> list[str]:
    """Local directory names/metadata, never recursive file-content reads."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetLogicalDrives.argtypes = []
    kernel.GetLogicalDrives.restype = wintypes.DWORD
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise ConfigError("Cannot determine local directory discovery roots.")
    roots = []
    for index in range(26):
        drive = f"{chr(ord('A') + index)}:\\"
        if mask & (1 << index) and kernel.GetDriveTypeW(drive) == 3:  # DRIVE_FIXED
            roots.append(str(_local_path(Path(drive))))
    return roots


def resolve_helper() -> Path:
    if platform.system() != "Windows":
        raise ConfigError("windows-mxc requires native Windows; in WSL use the Linux backend.")
    selected = env_get("ALYSIS_WINDOWS_MXC_HELPER")
    digest = env_get("ALYSIS_WINDOWS_MXC_SHA256")
    if selected is None and digest is None:
        from .windows_mxc_bundle import HELPER_SHA256, runtime_helper

        if platform.machine().lower() not in {"amd64", "x86_64"}:
            raise ConfigError("The bundled Windows sandbox currently supports Windows x64 only.")
        try:
            candidate = runtime_helper()
        except (OSError, ValueError) as exc:
            raise ConfigError(
                "The bundled Windows sandbox is missing or damaged. Reinstall Alysis Code; "
                "no command was launched."
            ) from exc
        digest = HELPER_SHA256
    elif not selected or not digest or not Path(selected).is_absolute():
        raise ConfigError(
            "A Windows sandbox override requires both an absolute ALYSIS_WINDOWS_MXC_HELPER "
            "and ALYSIS_WINDOWS_MXC_SHA256; no command was launched."
        )
    else:
        candidate = Path(selected)
    candidate = _local_path(candidate, directory=False)
    try:
        with candidate.open("rb") as binary:
            actual = hashlib.file_digest(binary, "sha256").hexdigest()
    except OSError as exc:
        raise ConfigError("Cannot read the pinned MXC helper") from exc
    if actual != digest.lower():
        raise ConfigError("MXC helper integrity check failed; no command was launched.")
    metadata = _helper_json(candidate, "mxc-protocol")
    expected = helper_contract()
    if any(type(metadata.get(k)) is not type(v) or metadata[k] != v for k, v in expected.items()):
        raise ConfigError("Incompatible MXC helper policy contract; no command was launched.")
    if _helper_json(candidate, "mxc-probe").get("available") is not True:
        raise ConfigError("This Windows host does not support strict MXC; no fallback is allowed.")
    return candidate


def _python_tool_root() -> Path | None:
    """Expose the engine's base Python as a read-only command fallback.

    Desktop deliberately keeps its own virtualenv off the workload PATH. The
    base interpreter can run project code without selecting engine packages.
    Derive it from the running interpreter, never PATH or request environment.
    Frozen builds do not contain a general-purpose Python executable.
    """
    if os.name != "nt" or getattr(sys, "frozen", False):
        return None
    # Managed Python installers may use a version alias. Like the fixed file
    # worker, use only its resolved installation; never grant the alias itself.
    root = Path(sys.base_prefix).resolve(strict=True)
    if not (root / "python.exe").is_file() or not (root / "Lib" / "os.py").is_file():
        return None
    root = _local_path(root)
    if root == Path(root.anchor) or Path.home().resolve().is_relative_to(root):
        raise ConfigError("A Python tool installation cannot grant the user profile or drive root.")
    return root


class WindowsMxcRunner(WindowsNativeRunner):
    def _execution_context(self, argv: list[str], request: bytes) -> str:
        """Bind reruns to the actual admitted policy, never child-reported data."""
        policy = json.loads(request)
        # Command selection is compared separately. Lifecycle ownership and
        # timeout do not change the workload's environment or access rights.
        for key in ("argv", "parent_pid", "parent_created", "timeout_ms"):
            policy.pop(key, None)
        with Path(argv[0]).open("rb") as stream:
            helper_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        encoded = json.dumps([helper_digest, policy], sort_keys=True, ensure_ascii=True).encode()
        return "windows-mxc-v1:" + hashlib.sha256(encoded).hexdigest()

    def run(self, *, root, cwd, cmd, timeout_s, cancel_check=None):
        if self.settings.github_auth:
            raise ConfigError(
                "GitHub authentication must use the contained native Git dispatcher, not a general shell command."
            )
        if self.settings.filesystem_access != "workspace-write":
            return super().run(
                root=root, cwd=cwd, cmd=cmd, timeout_s=timeout_s, cancel_check=cancel_check
            )
        from ._windows_git_reservations import GitLease, GitReservations

        roots = {
            root.resolve(),
            *(Path(p).resolve() for p in self.settings.additional_workspace_roots),
        }
        with ExitStack() as resources:
            for workspace in sorted(roots, key=lambda p: str(p).casefold()):
                lease = GitLease(workspace, timeout_s, cancel_check)
                resources.callback(lease.close)
            reservations = GitReservations()
            resources.callback(reservations.close)
            token = _foreground_reservations.set(reservations)
            resources.callback(_foreground_reservations.reset, token)
            return super().run(
                root=root, cwd=cwd, cmd=cmd, timeout_s=timeout_s, cancel_check=cancel_check
            )

    def _prepare(
        self, *, root: Path, cwd: Path, cmd: str, env_overrides: dict[str, str] | None = None
    ) -> tuple[list[str], bytes]:
        helper = resolve_helper()
        python_root = _python_tool_root()
        settings = self.settings
        if python_root is not None:
            # Reuse the full tool-root admission, alias/hardlink checks and
            # write denies. Do not grant any other host PATH directories.
            settings = replace(
                settings,
                windows_read_roots=tuple(
                    dict.fromkeys((*settings.windows_read_roots, str(python_root)))
                ),
            )
        request = build_request(
            settings,
            root=root,
            cwd=cwd,
            cmd=cmd,
            helper=helper,
            env_overrides=env_overrides,
            # MXC launches the workload from the trusted host. Its helper is not
            # a workload dependency; granting it makes Desktop's private profile
            # look like a required content root and suppresses drive discovery.
            include_helper_read=False,
        )
        # Exact credential-file grants must not expose the rest of ~/.ssh (or
        # another credential directory). PSEC's narrower file rule can override
        # its default private-directory boundary; explicit app-state denials are
        # applied afterward and continue to take precedence over every grant.
        from .macos_access import file_identity

        files = [
            _local_path(Path(file_identity(value).path), directory=False)
            for value in settings.additional_read_files
        ]
        boundaries = [
            value
            for value in request["deny_read"]
            if any(path.is_relative_to(Path(value)) for path in files)
        ]
        request["deny_read"] = [p for p in request["deny_read"] if p not in boundaries]
        request["discovery_deny"] = boundaries
        request["read"].extend(map(str, files))
        request["deny_write"].extend(map(str, files))
        system_root = _local_path(Path(os.environ.get("SystemRoot", r"C:\Windows")))
        # Do not expose Windows\Temp, setup logs and other non-runtime folders
        # through a recursive grant on the whole Windows installation.
        system_reads = [
            str(_local_path(system_root / name))
            for name in WINDOWS_RUNTIME_DIRECTORIES
            if name == "System32" or (system_root / name).exists()
        ]
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise ConfigError("MXC requires the trusted LOCALAPPDATA environment location.")
        # Supplying the environment location does not grant access to that tree.
        # Temporary writes stay in the already granted workspace. No host profile
        # or credential environment is inherited by the workload.
        env = dict(request["env"])
        env.update(LOCALAPPDATA=local_app_data, TEMP=str(cwd), TMP=str(cwd), TMPDIR=str(cwd))
        from .windows_toolchain import known_folder

        # OpenSSH initializes its ProgramData path before stderr, and silently
        # exits 255 when this variable is absent. A location is not a read grant:
        # keep ProgramData and its SSH configuration outside the filesystem policy.
        program_data = known_folder(0x23)  # CSIDL_COMMON_APPDATA
        if program_data is None:
            raise ConfigError("Cannot resolve the Windows ProgramData location.")
        env["PROGRAMDATA"] = str(program_data)
        # Git's bundled MSYS SSH needs a shared object namespace that PSEC denies.
        # Prefer the OS client, already covered by the System32 runtime read grant.
        openssh = system_root / "System32" / "OpenSSH"
        if (openssh / "ssh.exe").is_file():
            env["PATH"] = str(openssh) + os.pathsep + env.get("PATH", "")
        # npm enables Node's compile cache automatically under the temporary
        # directory. Here that directory is the project, so even npm --version
        # would leave unrelated cache files in Git status. Disable this optional
        # cache for the workload and its children without granting a host cache.
        env["NODE_DISABLE_COMPILE_CACHE"] = "1"
        # Windows rewrites TEMP/TMP at AppContainer birth, after the supplied
        # environment was constructed. Its package temp directory cannot support
        # Python's private (0700) temporary directories under PSEC. Restore the
        # already-admitted working directory inside the contained shell, before
        # project code starts. TMPDIR also survives child-process redirection.
        # This grants no new filesystem rights and changes no host environment.
        temp_literal = request["cwd"].replace("'", "''")
        request["argv"][-1] = (
            f"$env:TEMP='{temp_literal}'; $env:TMP=$env:TEMP; $env:TMPDIR=$env:TEMP; "
            + request["argv"][-1]
        )
        if python_root is not None:
            # Existing project/user tools retain precedence when admitted. This
            # never adds the engine virtualenv's Scripts directory or packages.
            entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
            if str(python_root).casefold() not in {entry.casefold() for entry in entries}:
                entries.append(str(python_root))
            env["PATH"] = os.pathsep.join(entries)
            env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        if getattr(sys, "frozen", False):
            # The one-file worker reuses this process's read-only extracted
            # runtime. Derive paths from the running interpreter, never inherited
            # _PYI_* values or a model-supplied environment. Normal bootloader
            # parent/runtime verification remains enabled.
            executable = _local_path(Path(sys.executable), directory=False)
            extracted = _local_path(Path(sys._MEIPASS))
            env.update(
                _PYI_ARCHIVE_FILE=str(executable),
                _PYI_APPLICATION_HOME_DIR=str(extracted),
                _PYI_PARENT_PROCESS_LEVEL="1",
                _PYI_SPLASH_IPC="0",
            )
            request["read"].append(str(executable))
        request.update(
            protocol=4,
            local_preview=settings.preview_access == "local",
            read=list(dict.fromkeys([*request["read"], *system_reads])),
            env=sorted(env.items()),
            timeout_ms=86_400_000,
            **_parent_identity(),
        )
        # Match Seatbelt's directory discovery without granting file contents.
        # Use native fixed drives only; mapped/network/removable drives are not
        # an implicit network or external-media capability.
        request["enumerate"] = (
            _directory_discovery_roots() if settings.windows_directory_discovery else []
        )
        request.pop("traverse")
        from .windows_private_state import protect_private_state

        protect_private_state(request)
        if request["write"]:
            reservations = _foreground_reservations.get()
            if reservations is not None:
                for workspace in request["write"]:
                    reservations.reserve(Path(workspace), request["deny_write"])
                return [str(helper), "mxc-run-request"], json.dumps(
                    request, ensure_ascii=False
                ).encode()
            from ._windows_sandbox_bookkeeping import prepare_instruction_files

            # Reuse exclusive-creation receipts so infrastructure is not mistaken
            # for a command edit. A read-only workspace needs no reservations.
            # Validate environment and parent identity before persistent effects.
            for workspace in request["write"]:
                if Path(workspace) == root.resolve():
                    prepare_instruction_files(Path(workspace))
                else:
                    # A secondary receipt cannot become a same-named primary baseline.
                    from .sandbox_bookkeeping import capture_instruction_creations

                    with capture_instruction_creations(Path(workspace)):
                        prepare_instruction_files(Path(workspace))
        return [str(helper), "mxc-run-request"], json.dumps(request, ensure_ascii=False).encode()
