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
    powershell_literal,
)

# The reservations of the command being prepared: a foreground command's for
# its whole run, a background command's while it is started.
_foreground_reservations = ContextVar("mxc_foreground_reservations", default=None)
# The scratch folder of the command being prepared (``windows_mxc_scratch``).
_command_scratch: ContextVar[Path | None] = ContextVar("mxc_command_scratch", default=None)
# Whether the request being prepared is a command of the person's session and
# not one of the engine's fixed workers (contained Git, the file workers).
# Only such a command is given the person's programs (``windows_path_tools``).
_person_command: ContextVar[bool] = ContextVar("mxc_person_command", default=False)
# The repositories the engine holds for the background command being started.
# Only those are given to the helper without the request to hold them. None
# for a foreground command, whose reservations hold every protected entry.
_held_repositories: ContextVar[frozenset[str] | None] = ContextVar(
    "mxc_held_repositories", default=None
)
# What the command being prepared may read of the person's home, and what in
# it is refused: the profile it was decided for, the read entries and the
# denied entries that windows_home_access added. They change with what the
# home holds, which says nothing about the command (_execution_context).
_home_entries: ContextVar[tuple[str, frozenset[str], frozenset[str]] | None] = ContextVar(
    "mxc_home_entries", default=None
)
# What every Windows process is told about the computer it runs on. Programs
# build paths from these (``$env:SystemDrive\Users`` resolved inside the
# project without them) and several fail outright when the user name is
# missing. None of them is a credential or a location in the person's profile.
_MACHINE_ENV = (
    "SYSTEMDRIVE",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "ALLUSERSPROFILE",
    "PUBLIC",
    "USERNAME",
    "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "OS",
)


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


def mxc_scope_bounds_command(scope: object, *, reviewed_access: bool = False) -> bool:
    """Whether the MXC sandbox, as ``scope`` configures it, is a command's boundary.

    ``scope`` is launcher settings or the MXC runner that executes the command.
    True for the strict sandbox of the probed helper revision when nothing in
    ``scope`` hands the command credentials or writable Git metadata: no exact
    read file, forwarded variable, SSH agent or GitHub authentication. Network
    and loopback must be off, unless ``reviewed_access`` says the access in
    ``scope`` came from a grant that went through the access review. The
    caller still has to decide a command that inherits such a grant from an
    earlier command. The legacy Windows helper, the native Git job, the host
    and unknown objects are never accepted.
    """
    from .sandbox_settings import ShellSandboxSettings
    from .windows_mxc_containment import PROBED_SECURITY_REVISION

    contract = helper_contract()
    if (
        contract.get("security_revision") != PROBED_SECURITY_REVISION
        or contract.get("containment") != "mxc-psec"
    ):
        return False
    if isinstance(scope, ShellSandboxSettings):
        from .sandbox_runner import select_shell_sandbox_backend

        settings = scope
        try:
            choice = select_shell_sandbox_backend(settings)
        except Exception:  # noqa: BLE001 - not being able to answer is a no
            return False
        if (
            settings.mode != "strict"
            or getattr(choice, "backend", None) != "windows-mxc"
            or getattr(choice, "requires_command_approval", False)
        ):
            return False
    elif type(scope) is WindowsMxcRunner:
        settings = scope.settings
        if not isinstance(settings, ShellSandboxSettings):
            return False
    else:
        return False
    if not reviewed_access and (settings.network != "off" or settings.preview_access != "auto"):
        return False
    return (
        settings.network_domains is None
        and not settings.private_network_endpoints
        and not settings.unix_sockets
        and not settings.additional_read_files
        and not settings.forward_env
        and settings.ssh_agent_socket is None
        and not settings.github_auth
        and not settings.git_metadata_access
        and settings.clear_env
        and settings.protect_repo_meta
    )


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


# How long a background command waits for a project whose lease another
# command holds, when it starts and when it stops. Past that it goes on
# without the reservation record, or leaves the taking back to a later command.
_BACKGROUND_LEASE_WAIT_S = 15.0
_RELEASE_LEASE_WAIT_S = 5.0


def _free_repositories(request: dict, workspaces: list[str]) -> None:
    """Leave the repositories of a person's command writable for everyone but the command.

    The helper holds every protected folder open without sharing writing.
    For a repository that stops each rename into it, which is how Git
    replaces its index: while a command ran in a project, "git add" and
    "git commit" failed there with "unable to write new index file", for the
    engine's own Git job and for the person's Git alike, for as long as a
    development server ran. A repository is therefore given to the helper as
    a read-only rule without the request to hold it. The rule alone refuses
    everything the containment probes try against a repository. What the
    handle added, that the folder cannot go away under the rule, the engine
    provides itself with a handle that shares writing: the reservations of a
    foreground command hold it, and ``start`` holds it for a background one.
    """
    if not _person_command.get():
        return  # the engine's fixed workers decide about repositories themselves
    from .windows_sandbox import repository_directories

    repositories = {str(path) for path in repository_directories([Path(p) for p in workspaces])}
    held = _held_repositories.get()
    if held is not None:
        # A repository that appeared since, or could not be opened, stays the
        # helper's to hold: nothing else would keep it in place.
        repositories &= held
    freed = [path for path in request["deny_write"] if path in repositories]
    if freed:
        request["deny_write"] = [path for path in request["deny_write"] if path not in freed]
        request["read"] = list(dict.fromkeys([*request["read"], *freed]))


def _take_back_reservations(workspaces) -> None:
    """Remove recorded reservations no running command holds. The caller holds the leases."""
    from .windows_mxc_reservations import remove_unheld_reservations

    for workspace in workspaces:
        try:
            remove_unheld_reservations(Path(workspace))
        except Exception:  # noqa: BLE001 - taking back never fails the command that tried
            pass


def _release_background_records(records) -> None:
    """A background command has stopped: let its records go and take back what no one holds."""
    from ._windows_git_reservations import GitLease

    for record in records:
        try:
            lease = GitLease(record.workspace, _RELEASE_LEASE_WAIT_S, None)
        except (TimeoutError, OSError):
            # The project is busy. The next command there takes them back.
            record.let_go()
            continue
        try:
            record.let_go()
            _take_back_reservations([record.workspace])
        finally:
            lease.close()


class WindowsMxcRunner(WindowsNativeRunner):
    def _uses_native_lifecycle(self) -> bool:
        # MXC uses the engine's non-breakaway observer job and its own protocol.
        return False

    def _execution_context(self, argv: list[str], request: bytes) -> str:
        """Bind reruns to the actual admitted policy, never child-reported data."""
        policy = json.loads(request)
        # Command selection is compared separately. Lifecycle ownership and
        # timeout do not change the workload's environment or access rights.
        for key in ("argv", "parent_pid", "parent_created", "timeout_ms"):
            policy.pop(key, None)
        home = _home_entries.get()
        if home is not None:
            # A new file in the person's home between a failing check and its
            # rerun does not make the rerun another command. That the home is
            # readable, and which home, is what counts.
            where, read, denied = home
            policy["read"] = [value for value in policy["read"] if value not in read]
            policy["deny_read"] = [value for value in policy["deny_read"] if value not in denied]
            policy["home"] = where
        with Path(argv[0]).open("rb") as stream:
            helper_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        encoded = json.dumps([helper_digest, policy], sort_keys=True, ensure_ascii=True)
        scratch = _command_scratch.get()
        if scratch is not None:
            # Every command gets a scratch folder of its own with a new name.
            # Two runs with the same rights must still compare as the same.
            # HOMEPATH carries the folder without its drive, so match that.
            folder = str(scratch)[len(scratch.drive) :]
            encoded = encoded.replace(json.dumps(folder, ensure_ascii=True)[1:-1], "scratch")
        return "windows-mxc-v1:" + hashlib.sha256(encoded.encode()).hexdigest()

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
        from .windows_mxc_scratch import create_scratch, remove_scratch

        roots = self._projects(root)
        with ExitStack() as resources:
            for workspace in roots:
                lease = GitLease(workspace, timeout_s, cancel_check)
                resources.callback(lease.close)
            # Names a background command reserved and could not take back
            # itself: its engine process ended, or a command was running here
            # when it stopped. That command may be this one, so the same is
            # done once more when this command has ended, still under the
            # leases (registered first, it runs after the reservations close).
            _take_back_reservations(roots)
            resources.callback(_take_back_reservations, roots)
            reservations = GitReservations()
            resources.callback(reservations.close)
            token = _foreground_reservations.set(reservations)
            resources.callback(_foreground_reservations.reset, token)
            # The base class returns once every process of the command has
            # ended, so nothing still holds a file in the scratch folder.
            scratch = create_scratch()
            resources.callback(remove_scratch, scratch)
            scratch_token = _command_scratch.set(scratch)
            resources.callback(_command_scratch.reset, scratch_token)
            person_token = _person_command.set(True)
            resources.callback(_person_command.reset, person_token)
            return super().run(
                root=root, cwd=cwd, cmd=cmd, timeout_s=timeout_s, cancel_check=cancel_check
            )

    def _projects(self, root: Path) -> list[Path]:
        """Every folder this command reserves names in, in the order their leases are taken.

        The same folders the request makes writable: the project, the folders
        approved for it, and the folders attached to the session.
        """
        from .project_folders import allowed_roots

        return sorted(
            {
                root.resolve(),
                *(Path(p).resolve() for p in self.settings.additional_workspace_roots),
                *(p.resolve() for p in allowed_roots(root)[1:]),
            },
            key=lambda p: str(p).casefold(),
        )

    def start(
        self, *, root: Path, cwd: Path, cmd: str, env_overrides: dict[str, str] | None = None
    ):
        if self.settings.filesystem_access != "workspace-write":
            return super().start(root=root, cwd=cwd, cmd=cmd, env_overrides=env_overrides)
        from ._windows_git_reservations import GitLease, GitReservations, HeldDirectories
        from .windows_mxc_reservations import BackgroundRecord, by_project
        from .windows_mxc_scratch import create_scratch, remove_scratch
        from .windows_sandbox import repository_directories

        roots = self._projects(root)
        # Kept in existence by the engine for as long as the command runs,
        # because the helper is not asked to hold them (_free_repositories).
        repositories = HeldDirectories(repository_directories(roots))
        records: list[BackgroundRecord] = []
        # The names this command reserves are recorded so that they can be
        # taken back when the last background command in the project stops
        # (windows_mxc_reservations). Making and recording them needs the
        # project's lease, as every command's reservations do.
        leases = ExitStack()
        reservations = None
        try:
            scratch = create_scratch()
            try:
                for workspace in roots:
                    lease = GitLease(workspace, _BACKGROUND_LEASE_WAIT_S, None)
                    leases.callback(lease.close)
                reservations = GitReservations()
            except (TimeoutError, OSError):
                # A command is running in the project and keeps its lease.
                # Start without a record, as before there was one: the names
                # then stay.
                leases.close()
                reservations = None
        except BaseException:
            leases.close()
            repositories.close()
            raise
        token = _command_scratch.set(scratch)
        person_token = _person_command.set(True)
        reservation_token = _foreground_reservations.set(reservations)
        held_token = _held_repositories.set(frozenset(map(str, repositories.paths)))
        try:
            if reservations is not None:
                _take_back_reservations(roots)
            try:
                spawn = super().start(root=root, cwd=cwd, cmd=cmd, env_overrides=env_overrides)
            except BaseException:
                if reservations is not None:
                    reservations.close()
                repositories.close()
                remove_scratch(scratch)
                raise
            if reservations is not None:
                made = reservations.detach()
                try:
                    # One record per project, also where nothing had to be
                    # made: it says a command relies on the names there.
                    for workspace, entries in by_project(roots, made).items():
                        records.append(BackgroundRecord(workspace, entries))
                except (OSError, ConfigError):
                    # Without a record nothing is taken back. The command runs.
                    for record in records:
                        record.discard()
                    records.clear()
        finally:
            _held_repositories.reset(held_token)
            _foreground_reservations.reset(reservation_token)
            _person_command.reset(person_token)
            _command_scratch.reset(token)
            leases.close()

        def cleanup() -> None:
            # The background command keeps its scratch folder for as long as
            # it runs, like the scratch folder of a macOS background command.
            # Stopping it ends the helper; its processes follow a moment later
            # and hold their open files until then.
            try:
                spawn.cleanup()
            finally:
                repositories.close()
                remove_scratch(scratch, wait_s=3.0)
                _release_background_records(records)

        return replace(spawn, cleanup=cleanup)

    def _prepare(
        self, *, root: Path, cwd: Path, cmd: str, env_overrides: dict[str, str] | None = None
    ) -> tuple[list[str], bytes]:
        helper = resolve_helper()
        python_root = _python_tool_root()
        settings = self.settings
        if python_root is not None:
            # Reuse the full tool-root admission, alias/hardlink checks and
            # write denies. The folders on the person's PATH are added below.
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
            # This sandbox decides by path and changes no permissions, so a
            # folder is not searched for hard links and links before it is
            # granted. A project with a uv environment or pnpm's node_modules
            # holds thousands of hard-linked files, the person's Documents
            # holds three links Windows put there, and every such folder was
            # refused before a command could start. What lies behind a link
            # is decided where it lies (docs/windows-cli-sandbox.md).
            validate_trees=False,
            # With directory discovery PowerShell can stand in the folder
            # itself. The fixed workers see nothing above their folders and
            # keep the drive.
            real_location=settings.windows_directory_discovery,
        )
        location = request.pop("location", None)
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
        # No host profile or credential environment is inherited by the workload.
        env = dict(request["env"])
        for name in _MACHINE_ENV:
            value = os.environ.get(name)
            if value and "\0" not in value:
                env.setdefault(name, value)
        scratch = _command_scratch.get()
        # The person's profile and roaming application data, when this command
        # is told where they really are and may read them (windows_home_access).
        home = None
        _home_entries.set(None)
        if scratch is None:
            # A command that cannot write (and the fixed workers that run with
            # a read-only base) keeps the working directory as its temp folder.
            env.update(LOCALAPPDATA=local_app_data, TEMP=str(cwd), TMP=str(cwd), TMPDIR=str(cwd))
        else:
            from .windows_mxc_scratch import scratch_environment

            env.update(scratch_environment(scratch))
            if _person_command.get():
                from .windows_home_access import home_environment, person_home

                home = person_home()
                if home is not None:
                    # The local application data and the temp folder stay the
                    # command's own: that is where programs write.
                    env.update(home_environment(*home))
        from .windows_toolchain import known_folder

        # OpenSSH initializes its ProgramData path before stderr, and silently
        # exits 255 when this variable is absent. A location is not a read grant.
        # A person's command reads ProgramData under windows_home_access, without
        # the SSH server's folder; the fixed workers read none of it.
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
        # Windows rewrites TEMP/TMP and LOCALAPPDATA at AppContainer birth, after
        # the supplied environment was constructed. Its package temp directory
        # cannot support Python's private (0700) temporary directories under
        # PSEC, and its package folder is a new one for every command. Restore
        # the admitted locations inside the contained shell, before project
        # code starts. TMPDIR also survives child-process redirection. This
        # grants no new filesystem rights and changes no host environment.
        temp_literal = powershell_literal(env["TEMP"])
        restore = f"$env:TEMP='{temp_literal}'; $env:TMP=$env:TEMP; $env:TMPDIR=$env:TEMP; "
        if scratch is not None:
            restore += f"$env:LOCALAPPDATA='{powershell_literal(env['LOCALAPPDATA'])}'; "
            # The Windows folder API does not read LOCALAPPDATA. For an app
            # container it answers with a "Packages\<container>\AC" folder
            # below the profile's local data, and with USERPROFILE moved to
            # the scratch home that folder does not exist. A program that asks
            # the API then gets an empty answer and writes relative to the
            # working directory: PowerShell left its module cache in the
            # project as "Microsoft\Windows\PowerShell". The container's name
            # is only known inside it, so the API itself is asked to make the
            # folders, which also replaces the empty answer this process has
            # already been given. Making them with CreateDirectory does not:
            # that was tried. No cmdlet is used: one would make PowerShell
            # look for that cache first.
            restore += (
                "& { foreach ($n in 'LocalApplicationData','InternetCache') "
                "{ try { [void][Environment]::GetFolderPath($n,'Create') } catch { } } }; "
            )
        request["argv"][-1] = restore + request["argv"][-1]
        entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
        folders: list[Path] = []
        if _person_command.get():
            # The person's own programs, after the engine's Node.js, Git and
            # system folders and before its fallback Python.
            from .windows_path_tools import host_path_folders, private_and_runtime_folders

            grants, folders = host_path_folders(
                os.environ.get("PATH"),
                readable=[Path(path) for path in (*request["read"], *system_reads)],
                apart=[
                    *(Path(path) for path in (*request["read"], *request["write"])),
                    *([scratch.parent] if scratch is not None else []),
                    system_root,
                    *private_and_runtime_folders(),
                ],
                program_files=[
                    folder for folder in (known_folder(0x26), known_folder(0x2A)) if folder
                ],
            )
            request["read"].extend(map(str, grants))
            entries.extend(map(str, folders))
        if python_root is not None:
            # Existing project/user tools retain precedence when admitted. This
            # never adds the engine virtualenv's Scripts directory or packages.
            entries.append(str(python_root))
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            if home is None or home[1] is None:
                # Without the person's roaming application data there are no
                # packages of theirs to find: the folder Python would look in
                # is the scratch home.
                env["PYTHONNOUSERSITE"] = "1"
        if entries:
            # The first of two spellings of one folder decides its place.
            ordered: dict[str, str] = {}
            for entry in entries:
                ordered.setdefault(entry.casefold().rstrip("\\/"), entry)
            env["PATH"] = os.pathsep.join(ordered.values())
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

        # The scratch folder is writable but is not a project: nothing in it
        # is reserved or protected, and the host never reads it as trusted.
        workspaces = list(request["write"])
        if scratch is not None and workspaces:
            request["write"].append(str(scratch))
        protect_private_state(request)
        if location is not None and any(
            Path(request["cwd"]).is_relative_to(Path(boundary))
            for boundary in request.get("discovery_deny", [])
        ):
            # A folder the application manages inside its own data: the data
            # folder is hidden from the command, and PowerShell cannot stand
            # in a folder whose parents it cannot see. A program started there
            # found itself in the root of the drive. The drive that stands
            # for the folder still works.
            in_the_folder, on_the_drive = location
            request["argv"][-1] = request["argv"][-1].replace(in_the_folder, on_the_drive, 1)
        if home is not None:
            # After everything that names a folder to deny: no grant made here
            # contains one of those, or a project, or the scratch folder.
            from .windows_home_access import readable_places
            from .windows_path_tools import private_and_runtime_folders

            writable = [Path(value) for value in request["write"]]
            access = readable_places(
                *home,
                # Windows' own folders are on every PATH and say nothing about
                # which tools the person has. A PATH folder the command can
                # write says nothing either: the command could put a file
                # there named like the folder it wants to read.
                programs=[
                    folder
                    for folder in folders
                    if not folder.is_relative_to(system_root)
                    and not any(folder.is_relative_to(root) for root in writable)
                ],
                program_roots=[
                    folder for folder in (known_folder(0x26), known_folder(0x2A)) if folder
                ],
                program_data=program_data,
                apart=[
                    *(
                        Path(value.rstrip("\\/"))
                        for key in ("write", "deny_read", "discovery_deny")
                        for value in request.get(key, [])
                    ),
                    *private_and_runtime_folders(),
                ],
            )
            # What was approved for this command by name stays readable: a
            # credential file the person let it read, a folder they attached.
            approved = [*files, *(Path(value) for value in settings.windows_read_roots)]
            refused = [
                path
                for path in access.deny
                if not any(
                    path.is_relative_to(other) or other.is_relative_to(path) for other in approved
                )
            ]
            added = [value for value in map(str, access.read) if value not in request["read"]]
            denied = [value for value in map(str, refused) if value not in request["deny_read"]]
            request["read"].extend(added)
            request["deny_read"].extend(denied)
            _home_entries.set(
                (
                    "|".join(str(folder) for folder in home if folder is not None),
                    frozenset(added),
                    frozenset(denied),
                )
            )
        if workspaces:
            reservations = _foreground_reservations.get()
            if reservations is not None:
                for workspace in workspaces:
                    reservations.reserve(Path(workspace), request["deny_write"])
                _free_repositories(request, workspaces)
                return [str(helper), "mxc-run-request"], json.dumps(
                    request, ensure_ascii=False
                ).encode()
            from ._windows_sandbox_bookkeeping import prepare_instruction_files

            # Reuse exclusive-creation receipts so infrastructure is not mistaken
            # for a command edit. A read-only workspace needs no reservations.
            # Validate environment and parent identity before persistent effects.
            plain = {str(Path(value).resolve()) for value in settings.windows_plain_write_roots}
            for workspace in workspaces:
                if workspace in plain:
                    continue  # not a project: nothing is reserved there
                if Path(workspace) == root.resolve():
                    prepare_instruction_files(Path(workspace))
                else:
                    # A secondary receipt cannot become a same-named primary baseline.
                    from .sandbox_bookkeeping import capture_instruction_creations

                    with capture_instruction_creations(Path(workspace)):
                        prepare_instruction_files(Path(workspace))
            _free_repositories(request, workspaces)
        return [str(helper), "mxc-run-request"], json.dumps(request, ensure_ascii=False).encode()
