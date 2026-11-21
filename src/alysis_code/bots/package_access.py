"""Package installs from an owl computer's shell, through a guard that reaches package indexes only.

The shell runs in a namespace with no network (desktop_linux.py). Inside an owl computer, its
bridge (cloud_desktop_worker.py) starts this guard next to the browser's: an HTTPS proxy on a
private Unix socket that admits only CONNECT tunnels to port 443 of the hosts declared in
package_indexes.json, only when the TLS server name is that same host, resolves each host itself
and dials only public addresses. The socket and the declared index list are bound read-only into
the shell's namespace; there the desktop worker relays a loopback port to the socket, points the
installers at it and declares which package indexes this computer can use. Nothing else the shell
runs reaches a network, and installed packages stay inside the owl's computer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..ide.browser_egress_proxy import (
    AddressResolver,
    BrowserEgressProxy,
    BrowserEgressProxyError,
    BrowserEgressProxyLimits,
    NumericDialer,
)

# The reviewed list of package indexes (configuration, not code): see its own description.
CONFIG = Path(__file__).with_name("package_indexes.json")
# Names inside the guard's directory, which the shell's namespace sees at PACKAGES_IN_COMPUTER.
SOCKET = "proxy.sock"
INDEXES = "indexes.json"
PACKAGES_IN_COMPUTER = "/run/alysis-packages"
HTTPS_PORT = 443
MAX_INDEXES = 8
MAX_HOSTS = 32
# Package downloads are larger and longer-lived than page loads: a wheel can be hundreds of MiB,
# and one installer connection fetches many files. Bounded per computer session.
LIMITS = BrowserEgressProxyLimits(
    max_connections=32,
    idle_timeout_seconds=60.0,
    connection_lifetime_seconds=1800.0,
    max_bytes_per_connection=1024 * 1024 * 1024,
    max_bytes_total=4 * 1024 * 1024 * 1024,
)


class PackageIndexError(ValueError):
    """The package index list is unusable; no package index is offered."""


@dataclass(frozen=True)
class PackageIndex:
    ecosystem: str
    index_url: str
    hosts: tuple[str, ...]

    def declaration(self) -> dict:
        return {"index_url": self.index_url, "hosts": list(self.hosts)}


def package_indexes(path: Path = CONFIG) -> tuple[PackageIndex, ...]:
    """The declared package indexes, validated. Hosts are exact DNS names (no IP literals,
    wildcards or ports); each index URL is HTTPS on the default port, on one of its own hosts."""
    from ..config import ConfigError
    from ..macos_network import network_domains

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageIndexError("The package index list cannot be read.") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or not isinstance(value.get("indexes"), dict)
        or len(value["indexes"]) > MAX_INDEXES
    ):
        raise PackageIndexError("The package index list has an unknown shape.")
    result = []
    for ecosystem, index in value["indexes"].items():
        if (
            not isinstance(ecosystem, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", ecosystem)
            or not isinstance(index, dict)
            or set(index) != {"index_url", "hosts"}
        ):
            raise PackageIndexError("Each package index needs a name, an index URL and hosts.")
        hosts = index["hosts"]
        if (
            not isinstance(hosts, list)
            or not 1 <= len(hosts) <= MAX_HOSTS
            or any(not isinstance(host, str) or host.startswith("*") for host in hosts)
        ):
            raise PackageIndexError("Package index hosts must be exact host names.")
        try:
            normalized = network_domains(hosts)
        except ConfigError as exc:
            raise PackageIndexError("Package index hosts must be exact host names.") from exc
        if normalized != tuple(hosts):
            raise PackageIndexError("List package index hosts once each, in lowercase.")
        url = index["index_url"]
        try:
            parts = urlsplit(url) if isinstance(url, str) else None
            port = parts.port if parts is not None else None
        except ValueError:
            parts = None
        if (
            parts is None
            or parts.scheme != "https"
            or parts.hostname not in normalized
            or port not in (None, HTTPS_PORT)
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise PackageIndexError("A package index URL must be HTTPS on one of its hosts.")
        result.append(PackageIndex(ecosystem, url, normalized))
    if len({host for index in result for host in index.hosts}) > MAX_HOSTS:
        raise PackageIndexError("The package index list names too many hosts.")
    return tuple(result)


class PackageGuard:
    """One computer session's package-index guard, listening at ``directory / SOCKET``."""

    def __init__(
        self,
        directory: Path,
        indexes: tuple[PackageIndex, ...],
        *,
        resolver: AddressResolver | None = None,
        dialer: NumericDialer | None = None,
    ):
        if not indexes:
            raise PackageIndexError("No package index is declared.")
        self.directory = Path(directory)
        self.indexes = tuple(indexes)
        self._proxy = BrowserEgressProxy(
            limits=LIMITS,
            resolver=resolver,
            dialer=dialer,
            allowed_hosts=frozenset(host for index in self.indexes for host in index.hosts),
            tunnel_ports=frozenset({HTTPS_PORT}),
            require_tunnel_sni=True,
            unix_socket_path=str(self.directory / SOCKET),
        )

    def start(self) -> PackageGuard:
        # A new private directory: nothing another process prepared is trusted or replaced.
        self.directory.mkdir(mode=0o700)
        declared = {index.ecosystem: index.declaration() for index in self.indexes}
        (self.directory / INDEXES).write_text(
            json.dumps({"schema": 1, "indexes": declared}), encoding="utf-8"
        )
        self._proxy.start()
        return self

    @property
    def healthy(self) -> bool:
        return self._proxy.healthy

    def close(self) -> None:
        self._proxy.close()


def start_package_guard(directory: Path, *, config: Path = CONFIG) -> PackageGuard | None:
    """This computer session's guard, or None when no package index is declared or the guard
    cannot start. Without it the shell stays offline and the computer declares no index."""
    try:
        indexes = package_indexes(config)
        return PackageGuard(directory, indexes).start() if indexes else None
    except (PackageIndexError, BrowserEgressProxyError, OSError):
        return None
