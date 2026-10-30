"""Seatbelt filesystem policy shared by shell and contained file workers.

Paths are passed as sandbox-exec parameters. Never interpolate filesystem names
into SBPL. Each invocation owns its scratch directory and protects its authority
anchors against rename, including absent private and runtime destinations.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .command_text import command_text
from .config import ConfigError
from .host_file_policy import HostFilePolicy
from .project_folders import allowed_roots


def _xcode_code_paths(
    developer: Path, *, applications_root: Path = Path("/Applications")
) -> tuple[Path, ...]:
    """Selected system Xcode code, SDKs and public bundle metadata."""
    try:
        developer = developer.resolve(strict=True)
        bundle = developer.parent.parent
        if (
            developer.name != "Developer"
            or developer.parent.name != "Contents"
            or bundle.suffix != ".app"
            or bundle.parent != applications_root.resolve()
        ):
            return ()
        result = []
        for relative in (
            "usr",
            "Library/Frameworks",
            "Library/PrivateFrameworks",
            "Library/Xcode/PlugIns",
            "Library/Xcode/PrivatePlugIns",
            "Toolchains/XcodeDefault.xctoolchain/usr",
            "Platforms/MacOSX.platform/Developer/SDKs",
            "Platforms/MacOSX.platform/Developer/usr",
            "Platforms/MacOSX.platform/Developer/Library/Frameworks",
            "Platforms/MacOSX.platform/Developer/Library/PrivateFrameworks",
            "Platforms/MacOSX.platform/Developer/Library/Xcode/PlugIns",
            "Platforms/MacOSX.platform/Developer/Library/Xcode/PrivatePlugIns",
        ):
            candidate = developer / relative
            if candidate.is_dir() and candidate.resolve().is_relative_to(developer):
                result.append(candidate.resolve())
        for name in ("Frameworks", "SharedFrameworks", "PlugIns"):
            candidate = developer.parent / name
            if candidate.is_dir() and candidate.resolve().is_relative_to(developer.parent):
                result.append(candidate.resolve())
        # xcrun discovers the selected platform and toolchain from public
        # bundle metadata. Do not grant their adjacent configuration trees.
        for directory in (
            developer.parent,
            developer / "Platforms/MacOSX.platform",
            developer / "Toolchains/XcodeDefault.xctoolchain",
        ):
            for name in ("Info.plist", "version.plist", "ToolchainInfo.plist"):
                candidate = directory / name
                if candidate.is_file() and candidate.resolve().is_relative_to(bundle):
                    result.append(candidate.resolve())
        return tuple(result)
    except (OSError, RuntimeError):
        return ()


def _selected_xcode_code_paths() -> tuple[Path, ...]:
    if sys.platform != "darwin":
        return ()
    try:
        selected = subprocess.run(
            ["/usr/bin/xcode-select", "--print-path"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **command_text(),
            timeout=1,
            check=False,
            env={"PATH": os.defpath},
        )
        return _xcode_code_paths(Path(selected.stdout.strip())) if selected.returncode == 0 else ()
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return ()


def runtime_read_paths(
    path_value: str | None,
    *,
    workspace_roots: tuple[Path, ...] = (),
    check_preparation: Callable[[], None] = lambda: None,
) -> tuple[Path, ...]:
    """Code/SDK directories, not whole installation prefixes or system data.

    Discover executable files on the host PATH, regardless of their names.
    Neither a PATH entry nor an installation prefix is a recursive read grant.
    Nonstandard runtime data locations need an explicit read grant. Workspace
    PATH entries cannot promote their symlink targets to host read authority.
    Keep executable aliases for projection into the command's private HOME;
    policy_paths canonicalizes every actual kernel grant.
    """
    candidates = [
        Path(p)
        for p in (
            "/System/Library",
            "/System/Cryptexes/OS",
            "/bin",
            "/sbin",
            "/Library/Apple/System/Library",
            "/Library/Developer",
            "/Library/Frameworks",
            "/Library/Java/JavaVirtualMachines",
            "/private/var/db/dyld",
            "/private/var/db/timezone",
            "/private/etc/ssl/cert.pem",
            "/private/etc/ssl/openssl.cnf",
            "/private/etc/localtime",
            "/private/etc/hosts",
            "/private/etc/resolv.conf",
            "/private/etc/protocols",
            "/private/etc/services",
            "/private/etc/passwd",
            # Public OS MIME database, used by package/file-type consumers.
            # Do not expose the adjacent web-server configuration directory.
            "/private/etc/apache2/mime.types",
        )
    ]
    for prefix in ("/usr", "/usr/local", "/opt/homebrew"):
        candidates.extend(
            Path(prefix) / name for name in ("bin", "sbin", "lib", "libexec", "share", "include")
        )
    # Homebrew's versioned packages and links contain executable/runtime code.
    # Its etc/var directories can contain database state or credentials.
    for prefix in ("/usr/local", "/opt/homebrew"):
        candidates.extend(Path(prefix) / name for name in ("Cellar", "opt"))
        # Homebrew Node/OpenSSL loads this public runtime configuration.
        # Keep adjacent service settings and credentials outside the grant.
        candidates.extend(
            Path(prefix) / "etc/openssl@3" / name for name in ("openssl.cnf", "cert.pem")
        )
    check_preparation()
    candidates.extend(_selected_xcode_code_paths())
    check_preparation()
    from .posix_runtime_paths import path_runtime_paths

    candidates.extend(
        path_runtime_paths(
            path_value,
            workspace_roots=workspace_roots,
            system_paths=tuple(path for path in candidates if path.exists()),
            check_preparation=check_preparation,
        )
    )
    return tuple(dict.fromkeys(path for path in candidates if path.exists()))


def project_read_paths(home: Path, paths: tuple[Path, ...], *, user_home: Path) -> None:
    """Expose already-approved home-relative paths without copying host data.

    The sandbox still enforces the target's read/write permissions. Unprojected
    siblings stay private; new cache directories are created in scratch HOME.
    Never follow a previously projected parent while constructing another link,
    or replace files staged by the trusted launcher (such as Git preferences).
    """
    for path in sorted(set(paths), key=lambda p: (len(p.parts), str(p))):
        if ".." in path.parts or path == user_home or not path.is_relative_to(user_home):
            continue
        destination = home / path.relative_to(user_home)
        if any(
            parent.is_symlink() or parent.is_file()
            for parent in destination.parents
            if parent != home and parent.is_relative_to(home)
        ):
            continue
        if destination.exists() or destination.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(path.resolve(), target_is_directory=path.is_dir())


def seatbelt_profile(
    *,
    network: str,
    preview_access: str = "auto",
    protected_path_count: int,
    extra_folder_count: int = 0,
    filesystem_access: str = "workspace-write",
    read_root_count: int = 0,
    private_path_count: int = 0,
    readonly_path_count: int = 0,
    anchor_count: int = 0,
    scratch: bool = False,
    protect_repo_meta: bool = True,
    git_metadata_writes: bool = False,
    network_proxy_port: int | None = None,
    unix_socket_count: int = 0,
    read_file_count: int = 0,
    ownership: bool = False,
) -> str:
    lines = [
        "(version 1)",
        "(deny default)",
        # Start from closed authority. An allow-default profile exposes host
        # process arguments/environment, Mach services and new OS operations
        # even when file and IP access have been constrained.
        "(allow process-fork)",
        "(deny process-exec)",
        "(allow syscall*)",
        "(allow mach-bootstrap)",
        # Python multiprocessing workers reopen semaphores created by their
        # parent. Keep host and other command sandboxes' semaphores excluded.
        "(allow ipc-posix-sem* (semaphore-owner same-sandbox))",
        # POSIX UID/GID lookup used by OpenSSH. This is the libinfo service,
        # not OpenDirectory's authentication/session-management endpoints.
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
        "(allow system-socket)",
        "(allow system-fcntl)",
        '(allow system-mac-syscall (require-all (mac-policy-name "Sandbox") (mac-syscall-number 2)))',
        "(allow file-read-metadata file-test-existence)",
        # Some process-inspection operations have their own compatibility
        # default on macOS. Explicitly close the family before scoped grants.
        "(deny process-info*)",
        "(allow process-info* (target self) (target children) (target same-sandbox))",
        "(deny sysctl-read)",
        # Hardware/OS characteristics are runtime inputs, unlike kern.procargs
        # and kern.proc, which disclose other processes' private state.
        '(allow sysctl-read (sysctl-name-prefix "hw.") '
        '(sysctl-name "kern.ostype") (sysctl-name "kern.osrelease") '
        '(sysctl-name "kern.osversion") (sysctl-name "kern.version") '
        '(sysctl-name "kern.argmax") (sysctl-name "kern.boottime") '
        '(sysctl-name "kern.hostname") '
        '(sysctl-name "kern.sysv.semmns") '
        '(sysctl-name "kern.maxfilesperproc"))',
        # File/network confinement alone must not let a command kill its host
        # or another chat, or acquire a host task port for memory access.
        "(deny signal)",
        "(allow signal (target self) (target children) (target same-sandbox))",
        "(deny mach-priv-task-port)",
        "(deny process-info-setcontrol)",
        "(allow process-info-setcontrol (target self) (target children) (target same-sandbox))",
        "(deny file-write*)",
        "(deny file-read-data)",
        "(deny file-link)",
        # Directory enumeration discovers paths without opening file contents.
        # This applies to any program/language, not a list of search commands.
        # Private host roots are denied again below, including their metadata.
        "(allow file-read-data (vnode-type DIRECTORY))",
        '(allow file-read-data (literal "/"))',
    ]
    if ownership:
        lines += [
            '(allow mach-lookup (global-name (param "OWNER_ALLOW")))',
            '(deny mach-lookup (global-name (param "OWNER_DENY")))',
        ]
    for name in [
        "WORKSPACE",
        *[f"FOLDER_{i}" for i in range(extra_folder_count)],
        *[f"READ_{i}" for i in range(read_root_count)],
    ]:
        lines.append(
            f'(allow file-read-data file-map-executable process-exec (subpath (param "{name}")))'
        )
    for index in range(read_file_count):
        lines.append(f'(allow file-read-data (literal (param "FILE_{index}")))')
    if filesystem_access == "workspace-write":
        for name in ["WORKSPACE", *[f"FOLDER_{i}" for i in range(extra_folder_count)]]:
            lines.append(f'(allow file-write* file-link (subpath (param "{name}")))')
    if scratch:
        lines.append(
            '(allow file-read-data file-map-executable process-exec file-write* (subpath (param "TMPDIR")))'
        )
    # Descriptor I/O is needed for stdout and ordinary pipes; this grants no
    # writable host directory. Inherited handles are closed by the launcher.
    lines.append(
        '(allow file-write* (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/stdout") (literal "/dev/stderr") '
        '(literal "/dev/tty") (regex #"^/dev/fd/[0-9]+$"))'
    )
    lines.append(
        '(allow file-read-data (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/random") (literal "/dev/urandom") '
        '(literal "/dev/stdin") (literal "/dev/tty") (regex #"^/dev/fd/[0-9]+$"))'
    )
    for prefix, count in (("PROTECTED", protected_path_count), ("READONLY", readonly_path_count)):
        for index in range(count):
            lines.append(f'(deny file-write* file-link (subpath (param "{prefix}_{index}")))')
            lines.append(f'(deny file-write* file-link (literal (param "{prefix}_{index}")))')
    if protect_repo_meta:
        # Match nested repositories as well as the workspace root. Renaming a
        # parent preserves the component name and cannot remove this exclusion.
        names = r"\.alysis|\.alysis_images|alysis-feedback"
        if not git_metadata_writes:
            names += r"|\.git"
        lines.append(f'(deny file-write* file-link (regex #"/({names})(/|$)"))')
    for index in range(anchor_count):
        lines.append(f'(allow file-read-data (literal (param "ANCHOR_{index}")))')
    for index in range(private_path_count):
        lines.append(
            f'(deny file-read* file-read-data file-read-metadata file-write* file-link process-exec (subpath (param "PRIVATE_{index}")))'
        )
        lines.append(
            f'(deny file-read* file-read-data file-read-metadata file-write* file-link (literal (param "PRIVATE_{index}")))'
        )
    for index in range(anchor_count):
        lines.append(f'(deny file-write-unlink (literal (param "ANCHOR_{index}")))')
    # File restrictions do not prevent connecting to host Unix sockets. Network
    # grants allow IP traffic only; they must never expose arbitrary local daemons.
    lines += ["(deny network-outbound)", "(deny network-inbound)", "(deny network-bind)"]
    if network == "on" and network_proxy_port is None:
        lines.append('(allow network-outbound (remote ip "*:*"))')
        # macOS DNS uses this system endpoint; it is not a general Unix grant.
        lines.append('(allow network-outbound (literal "/private/var/run/mDNSResponder"))')
        if preview_access == "local":
            # Native TLS validation is a daemon capability: trustd can fetch
            # certificate/revocation URLs outside the caller's network policy.
            # Require both broad and loopback authority. Never enable it for
            # offline, domain-filtered or exact-private-endpoint operations.
            lines.append(
                '(allow mach-lookup (global-name "com.apple.trustd") '
                '(global-name "com.apple.trustd.agent"))'
            )
    lines.append('(deny network-outbound (remote ip "localhost:*"))')
    if network_proxy_port is not None:
        if type(network_proxy_port) is not int or not 1 <= network_proxy_port <= 65535:
            raise ConfigError("Invalid command proxy port.")
        lines.append(f'(allow network-outbound (remote ip "localhost:{network_proxy_port}"))')
    if preview_access == "local":
        lines += [
            '(allow network-outbound (remote ip "localhost:*"))',
            '(allow network-inbound network-bind (local ip "localhost:*"))',
        ]
    # Project IPC and a command's private scratch sockets remain usable. External
    # read/write folder grants confer filesystem access, not access to daemons.
    for name in ["WORKSPACE", *(["TMPDIR"] if scratch else [])]:
        lines.append(
            f'(allow network-outbound network-inbound network-bind (subpath (param "{name}")))'
        )
    for index in range(unix_socket_count):
        lines.append(f'(allow network-outbound (literal (param "SOCKET_{index}")))')
    for prefix, count in (
        ("PRIVATE", private_path_count),
        ("PROTECTED", protected_path_count),
        ("READONLY", readonly_path_count),
    ):
        for index in range(count):
            lines.append(
                f'(deny network-outbound network-inbound network-bind (subpath (param "{prefix}_{index}")))'
            )
    return "\n".join(lines)


def policy_paths(
    root: Path,
    additional: tuple[str, ...],
    *,
    readonly_roots: tuple[Path, ...],
    protected: tuple[Path, ...],
    additional_read_roots: tuple[str, ...] = (),
    filesystem_access: str = "workspace-write",
    check_preparation: Callable[[], None] = lambda: None,
) -> tuple[tuple[Path, ...], HostFilePolicy, tuple[Path, ...]]:
    roots = tuple(dict.fromkeys((*allowed_roots(root), *(Path(p) for p in additional))))
    policy = HostFilePolicy.capture()
    readonly = tuple(dict.fromkeys(p.resolve() for p in (*policy.readonly_roots, *readonly_roots)))
    policy = HostFilePolicy(policy.private_roots, readonly)
    if any(Path.home().resolve().is_relative_to(path) for path in readonly):
        raise ConfigError("Seatbelt runtime read grants cannot include the home directory.")
    read_roots = tuple(Path(p) for p in additional_read_roots)
    links: dict[tuple[int, int], list[tuple[Path, os.stat_result, bool]]] = {}
    for path in (*roots, *read_roots):
        check_preparation()
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ConfigError("Seatbelt workspace roots must be existing canonical directories.")
        if path in (Path(path.anchor), Path.home().resolve()):
            raise ConfigError("Seatbelt cannot grant an entire filesystem or home directory.")
        if any(path.is_relative_to(p) for p in (*policy.private_roots, *protected)):
            raise ConfigError("Seatbelt workspace overlaps protected host state or metadata.")

    # Inspect each content-accessible tree once. Path construction and repeated
    # Python ancestor comparisons for every ordinary file made large projects
    # take minutes to prepare. Use native directory entries and component-safe
    # string prefixes; retain identities only for actual hardlinks.
    excluded = frozenset(str(p) for p in (*policy.private_roots, *policy.readonly_roots))
    excluded_prefixes = tuple(p.rstrip(os.sep) + os.sep for p in excluded)
    writable_paths = frozenset(str(p) for p in roots)
    writable_prefixes = tuple(p + os.sep for p in writable_paths)
    protected_paths = frozenset(str(p) for p in protected)
    protected_prefixes = tuple(p + os.sep for p in protected_paths)
    scan_roots: list[str] = []
    for path in sorted(set(map(str, (*roots, *read_roots))), key=len):
        if not any(path == parent or path.startswith(parent + os.sep) for parent in scan_roots):
            scan_roots.append(path)
    pending = list(scan_roots)
    while pending:
        check_preparation()
        directory = pending.pop()
        if directory in excluded or directory.startswith(excluded_prefixes):
            continue
        try:
            # Never follow a directory that became a link during preparation.
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise ConfigError("Seatbelt workspace changed during alias inspection.")
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_preparation()
                    target = entry.path
                    if target in excluded or target.startswith(excluded_prefixes):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(target)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        writable = (
                            filesystem_access == "workspace-write"
                            and (target in writable_paths or target.startswith(writable_prefixes))
                            and not (
                                target in protected_paths or target.startswith(protected_prefixes)
                            )
                        )
                        links.setdefault((info.st_dev, info.st_ino), []).append(
                            (Path(target), info, writable)
                        )
        except OSError as exc:
            raise ConfigError("Cannot inspect Seatbelt workspace aliases.") from exc
    for entries in links.values():
        check_preparation()
        first = entries[0][1]
        if len(entries) != first.st_nlink or len({entry[2] for entry in entries}) != 1:
            raise ConfigError(
                "Seatbelt found hardlinked files with aliases outside the same approved access "
                "scope. Grant the package store's exact folder as well, or configure the package "
                "manager to copy files/use a project-local store: " + str(entries[0][0])
            )
        for path, info, _ in entries:
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_nlink) != (
                info.st_dev,
                info.st_ino,
                info.st_nlink,
            ):
                raise ConfigError("Seatbelt hardlink aliases changed during inspection.")
    anchors = set(roots)
    for path in (*roots, *read_roots, *policy.private_roots, *policy.readonly_roots, *protected):
        # Renaming a parent would move a protected child out of its exclusion.
        anchors.update(path.parents)
    return roots, policy, tuple(sorted(anchors, key=str))
