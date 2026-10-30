"""Host-validated, operation-scoped command destinations and daemon sockets."""

from __future__ import annotations

import ipaddress
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError
from .host_file_policy import HostFilePolicy


def network_domains(values: object) -> tuple[str, ...]:
    """DNS names or whole-label wildcards; never URL syntax or literal IP bypass."""
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 32:
        raise ConfigError("Request between one and 32 network domain rules.")
    result = []
    for value in values:
        if not isinstance(value, str) or len(value) > 253:
            raise ConfigError("Network domains must be exact DNS names.")
        try:
            prefix = "**." if value.startswith("**.") else ("*." if value.startswith("*.") else "")
            host = value[len(prefix) :].encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ConfigError("Invalid network domain.") from exc
        if not host or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in host.split(".")
        ):
            raise ConfigError(
                "Use DNS names or a leading *. / **. wildcard, not URLs or partial-label wildcards."
            )
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ConfigError("Network domains cannot grant literal IP destinations.")
        if host == "localhost" or host.endswith(".localhost"):
            raise ConfigError("Use local_preview for loopback access.")
        if prefix and "." not in host:
            raise ConfigError("Wildcard network rules require a multi-label domain.")
        result.append(prefix + host)
    return tuple(dict.fromkeys(result))


def domain_matches(host: str, rules: frozenset[str]) -> bool:
    for rule in rules:
        if rule.startswith("**."):
            suffix = rule[3:]
            if host == suffix or host.endswith("." + suffix):
                return True
        elif rule.startswith("*."):
            if host.endswith("." + rule[2:]):
                return True
        elif host == rule:
            return True
    return False


def private_network_endpoints(values: object) -> tuple[str, ...]:
    """Explicit numeric host:port exceptions, never an entire LAN or a DNS wildcard."""
    if not isinstance(values, (list, tuple)) or len(values) > 16:
        raise ConfigError("Request at most sixteen exact private IP:port endpoints.")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ConfigError("Private network endpoints must be IP:port strings.")
        try:
            host, port = value.rsplit(":", 1)
            address = ipaddress.ip_address(
                host[1:-1] if host.startswith("[") and host.endswith("]") else host
            )
            number = int(port)
            if str(number) != port or not 1 <= number <= 65535:
                raise ValueError
            if (
                not (address.is_private or address.is_loopback)
                or address.is_unspecified
                or address.is_multicast
                or address.is_link_local
                or (address.is_reserved and not address.is_loopback)
            ):
                raise ValueError
            canonical = f"[{address}]:{number}" if address.version == 6 else f"{address}:{number}"
            if value != canonical:
                raise ValueError
        except ValueError as exc:
            raise ConfigError(
                "Use an exact canonical private IP and port, such as 127.0.0.1:8080. Metadata/link-local, unspecified and multicast addresses cannot be granted."
            ) from exc
        result.append(canonical)
    return tuple(dict.fromkeys(result))


def endpoint_pairs(values: tuple[str, ...]) -> frozenset[tuple[str, int]]:
    return frozenset(
        (value.rsplit(":", 1)[0].strip("[]"), int(value.rsplit(":", 1)[1]))
        for value in private_network_endpoints(values)
    )


@dataclass(frozen=True)
class SocketIdentity:
    requested_path: str
    path: str
    device: int
    inode: int

    def check(self) -> None:
        if socket_identity(self.requested_path) != self:
            raise ConfigError("The approved daemon socket changed. Request access again.")


def socket_identity(value: str) -> SocketIdentity:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 4096:
        raise ConfigError("Daemon sockets require an exact absolute path.")
    requested = Path(value)
    if not requested.is_absolute() or ".." in requested.parts:
        raise ConfigError("Daemon sockets require an exact absolute path.")
    try:
        path = requested.resolve(strict=True)
        info = path.stat()
    except (OSError, RuntimeError) as exc:
        raise ConfigError("The requested daemon socket does not exist.") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise ConfigError("The requested daemon path is not a Unix socket.")
    policy = HostFilePolicy.capture()
    if any(path.is_relative_to(root) for root in (*policy.private_roots, *policy.readonly_roots)):
        raise ConfigError("Private application and runtime sockets cannot be granted.")
    return SocketIdentity(str(requested), str(path), info.st_dev, info.st_ino)


def proxy_environment(url: str) -> dict[str, str]:
    result = {
        key: url
        for key in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "npm_config_proxy",
            "npm_config_https_proxy",
        )
    }
    result.update(NO_PROXY="", no_proxy="", NODE_USE_ENV_PROXY="1")
    return result
