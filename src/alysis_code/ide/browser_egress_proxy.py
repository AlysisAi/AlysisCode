"""Bounded loopback egress proxy for owned Chromium sessions.

The proxy is deliberately small and restrictive.  Chromium sends HTTP proxy
requests to a loopback listener; this module resolves each destination, rejects
the entire answer set when any address is non-public, and connects to a captured
numeric address.  The hostname is never passed to the dialer, closing the DNS
validation-to-connect race that URL-only request interception cannot close.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import re
import secrets
import select
import socket
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias
from urllib.parse import urlsplit

__all__ = [
    "BrowserEgressProxy",
    "BrowserEgressProxyCleanupError",
    "BrowserEgressProxyError",
    "BrowserEgressProxyLimitError",
    "BrowserEgressProxyLimits",
    "BrowserEgressProxyProtocolError",
    "BrowserEgressProxySecurityError",
    "start_browser_egress_proxy",
]

AddressResolver: TypeAlias = Callable[[str, int], Iterable[str]]


class ConnectedSocket(Protocol):
    def recv(self, size: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def settimeout(self, value: float | None) -> None: ...

    def shutdown(self, how: int) -> None: ...

    def close(self) -> None: ...

    def fileno(self) -> int: ...


NumericDialer: TypeAlias = Callable[[str, int, float], ConnectedSocket]
OwnedPreviewOriginAllowed: TypeAlias = Callable[[str, str, int], bool]

_TOKEN_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_METHODS = {b"GET", b"HEAD", b"POST", b"PUT", b"PATCH", b"DELETE", b"OPTIONS"}
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_RELAY_CHUNK_BYTES = 64 * 1024
_POLL_SECONDS = 0.1


class BrowserEgressProxyError(RuntimeError):
    """Base error with a message safe for user-visible diagnostics."""


class BrowserEgressProxyCleanupError(BrowserEgressProxyError):
    pass


class BrowserEgressProxySecurityError(BrowserEgressProxyError):
    pass


class BrowserEgressProxyLimitError(BrowserEgressProxyError):
    pass


class BrowserEgressProxyProtocolError(BrowserEgressProxyError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserEgressProxyLimits:
    max_header_bytes: int = 64 * 1024
    max_header_line_bytes: int = 8 * 1024
    max_header_count: int = 100
    max_connections: int = 32
    max_resolutions: int = 4
    max_resolved_addresses: int = 16
    dns_timeout_seconds: float = 2.0
    connect_timeout_seconds: float = 5.0
    header_timeout_seconds: float = 5.0
    idle_timeout_seconds: float = 30.0
    connection_lifetime_seconds: float = 300.0
    max_bytes_per_connection: int = 128 * 1024 * 1024
    max_bytes_total: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        integer_limits = (
            (self.max_header_bytes, 1024, 1024 * 1024, "header byte"),
            (self.max_header_line_bytes, 256, self.max_header_bytes, "header line"),
            (self.max_header_count, 1, 1000, "header count"),
            (self.max_connections, 1, 256, "connection"),
            (self.max_resolutions, 1, 32, "resolution"),
            (self.max_resolved_addresses, 1, 128, "resolved address"),
            (self.max_bytes_per_connection, 1024, 2**40, "connection byte"),
            (self.max_bytes_total, self.max_bytes_per_connection, 2**44, "total byte"),
        )
        for value, minimum, maximum, label in integer_limits:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise BrowserEgressProxyLimitError(
                    f"Browser egress proxy {label} limit is outside its safe range."
                )
        time_limits = (
            (self.dns_timeout_seconds, 0.05, 30.0, "DNS"),
            (self.connect_timeout_seconds, 0.05, 60.0, "connect"),
            (self.header_timeout_seconds, 0.05, 60.0, "header"),
            (self.idle_timeout_seconds, 0.05, 600.0, "idle"),
            (self.connection_lifetime_seconds, 0.05, 3600.0, "lifetime"),
        )
        for value, minimum, maximum, label in time_limits:
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not minimum <= float(value) <= maximum
            ):
                raise BrowserEgressProxyLimitError(
                    f"Browser egress proxy {label} timeout is outside its safe range."
                )


@dataclass(frozen=True, slots=True)
class _ParsedRequest:
    method: bytes
    scheme: str
    host: str
    port: int
    upstream_header: bytes | None
    buffered_body: bytes
    connect_tunnel: bool
    proxy_authorization: bytes


class _RequestFailure(Exception):
    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status


class BrowserEgressProxy:
    """A single-session, loopback-only validating HTTP/CONNECT proxy."""

    def __init__(
        self,
        *,
        limits: BrowserEgressProxyLimits | None = None,
        resolver: AddressResolver | None = None,
        dialer: NumericDialer | None = None,
        allow_local_destinations: bool = False,
        local_destinations_loopback_only: bool = False,
        owned_preview_origin_allowed: OwnedPreviewOriginAllowed | None = None,
        allowed_hosts: frozenset[str] | None = None,
        allowed_private_endpoints: frozenset[tuple[str, int]] = frozenset(),
        require_authentication: bool = False,
    ) -> None:
        if not isinstance(require_authentication, bool):
            raise BrowserEgressProxySecurityError("Invalid proxy authentication policy.")
        # A loopback port is reachable by other commands with local-preview
        # permission. Bind access to this invocation with an in-memory secret.
        self._credential = "alysis:" + secrets.token_urlsafe(32) if require_authentication else None
        self._authorization = (
            b"Basic " + base64.b64encode(self._credential.encode("ascii"))
            if self._credential is not None
            else None
        )
        if not isinstance(allow_local_destinations, bool):
            raise BrowserEgressProxySecurityError(
                "Browser egress proxy local-destination policy is invalid."
            )
        if not isinstance(local_destinations_loopback_only, bool):
            raise BrowserEgressProxySecurityError(
                "Browser egress proxy loopback policy is invalid."
            )
        if local_destinations_loopback_only and not allow_local_destinations:
            raise BrowserEgressProxySecurityError(
                "Browser egress proxy loopback policy requires explicit local access."
            )
        if owned_preview_origin_allowed is not None and not callable(owned_preview_origin_allowed):
            raise BrowserEgressProxySecurityError(
                "Browser egress proxy preview-origin policy is invalid."
            )
        self._limits = limits or BrowserEgressProxyLimits()
        self._resolver = resolver or _default_resolver
        self._dialer = dialer
        self._allow_local_destinations = allow_local_destinations
        self._local_destinations_loopback_only = local_destinations_loopback_only
        self._owned_preview_origin_allowed = owned_preview_origin_allowed
        from ..config import ConfigError
        from ..macos_network import network_domains, private_network_endpoints

        try:
            if allowed_hosts is not None and (
                not isinstance(allowed_hosts, frozenset)
                or (
                    allowed_hosts
                    and frozenset(network_domains(tuple(allowed_hosts))) != allowed_hosts
                )
            ):
                raise ConfigError("Invalid proxy destination allowlist.")
            if not isinstance(allowed_private_endpoints, frozenset):
                raise ConfigError("Invalid private endpoint allowlist.")
            private_network_endpoints(
                tuple(
                    f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                    for host, port in allowed_private_endpoints
                )
            )
        except (ConfigError, ValueError, TypeError) as exc:
            raise BrowserEgressProxySecurityError("Invalid proxy destination allowlist.") from exc
        self._allowed_private_endpoints = allowed_private_endpoints
        self._allowed_hosts = allowed_hosts
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._resolution_slots = threading.BoundedSemaphore(self._limits.max_resolutions)
        self._listener: socket.socket | None = None
        self._listener_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._helpers: set[threading.Thread] = set()
        self._active_sockets: set[ConnectedSocket] = set()
        self._started = False
        self._closed = False
        self._port = 0
        self._total_bytes = 0
        self._terminal_error: BrowserEgressProxyError | None = None
        self._denied_endpoints: set[tuple[str, int]] = set()

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        with self._lock:
            if not self._started or self._port <= 0:
                raise BrowserEgressProxyError("Browser egress proxy has not started.")
            return self._port

    @property
    def proxy_url(self) -> str:
        userinfo = self._credential + "@" if self._credential is not None else ""
        return f"http://{userinfo}{self.host}:{self.port}"

    @property
    def terminal_error(self) -> BrowserEgressProxyError | None:
        with self._lock:
            return self._terminal_error

    @property
    def healthy(self) -> bool:
        with self._lock:
            thread = self._listener_thread
            return bool(
                self._started
                and not self._closed
                and self._terminal_error is None
                and thread is not None
                and thread.is_alive()
            )

    def start(self) -> BrowserEgressProxy:
        with self._lock:
            if self._closed:
                raise BrowserEgressProxyError("Browser egress proxy is already closed.")
            if self._started:
                return self
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind((self.host, 0))
                listener.listen(self._limits.max_connections)
                listener.settimeout(_POLL_SECONDS)
                port = listener.getsockname()[1]
                if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                    raise OSError("invalid listener port")
            except BaseException as exc:
                listener.close()
                raise BrowserEgressProxyError(
                    "Browser egress proxy could not bind its loopback listener."
                ) from exc
            self._listener = listener
            self._port = port
            self._denied_endpoints.add((self.host, port))
            self._started = True
            thread = threading.Thread(
                target=self._accept_loop,
                name="alysis-browser-proxy-listener",
                daemon=True,
            )
            self._listener_thread = thread
            try:
                thread.start()
            except BaseException as exc:
                self._started = False
                self._listener = None
                listener.close()
                raise BrowserEgressProxyError(
                    "Browser egress proxy listener could not start."
                ) from exc
        return self

    def deny_endpoint(self, host: str, port: int) -> None:
        """Permanently deny an owned control endpoint, including in local mode."""

        try:
            normalized_host, normalized_port = _normalize_host_port(host, port)
        except _RequestFailure as exc:
            raise BrowserEgressProxySecurityError(
                "Browser egress proxy denied endpoint is invalid."
            ) from exc
        with self._lock:
            self._denied_endpoints.add((normalized_host, normalized_port))

    def close(self, *, timeout: float = 2.0) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not 0.05 <= float(timeout) <= 30.0
        ):
            raise BrowserEgressProxyLimitError("Browser egress proxy close timeout is invalid.")
        deadline = time.monotonic() + float(timeout)
        with self._close_lock:
            with self._lock:
                self._closed = True
                self._stop.set()
                listener = self._listener
                self._listener = None
                sockets = tuple(self._active_sockets)
                listener_thread = self._listener_thread
            _close_socket(listener)
            for item in sockets:
                _close_socket(item)
            current = threading.current_thread()
            if listener_thread is not None and listener_thread is not current:
                listener_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            while True:
                with self._lock:
                    workers = tuple(item for item in self._workers if item is not current)
                    helpers = tuple(item for item in self._helpers if item is not current)
                background_threads = workers + helpers
                if not background_threads or time.monotonic() >= deadline:
                    break
                for thread in background_threads:
                    thread.join(timeout=min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            with self._lock:
                listener_alive = bool(
                    listener_thread is not None
                    and listener_thread is not current
                    and listener_thread.is_alive()
                )
                workers_alive = any(
                    item is not current and item.is_alive() for item in self._workers
                )
                helpers_alive = any(
                    item is not current and item.is_alive() for item in self._helpers
                )
                sockets_remaining = bool(self._active_sockets)
                if listener_alive or workers_alive or helpers_alive or sockets_remaining:
                    error = BrowserEgressProxyCleanupError(
                        "Browser egress proxy cleanup did not finish before its deadline."
                    )
                    if self._terminal_error is None:
                        self._terminal_error = error
                    raise error

    def __enter__(self) -> BrowserEgressProxy:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                return
            try:
                client, peer = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                self._fail_proxy("Browser egress proxy listener failed.")
                return
            peer_host = peer[0] if isinstance(peer, tuple) and peer else None
            if peer_host != self.host:
                _close_socket(client)
                continue
            client.settimeout(_POLL_SECONDS)
            with self._lock:
                if self._stop.is_set():
                    _close_socket(client)
                    return
                if len(self._workers) >= self._limits.max_connections:
                    overloaded = True
                else:
                    overloaded = False
                    worker = threading.Thread(
                        target=self._serve_client,
                        args=(client,),
                        name="alysis-browser-proxy-client",
                        daemon=True,
                    )
                    self._workers.add(worker)
                    self._active_sockets.add(client)
            if overloaded:
                _send_error_and_drain(client, 503, max_drain_bytes=self._limits.max_header_bytes)
                continue
            try:
                worker.start()
            except BaseException:
                with self._lock:
                    self._workers.discard(worker)
                    self._active_sockets.discard(client)
                _close_socket(client)

    def _serve_client(self, client: socket.socket) -> None:
        upstream: ConnectedSocket | None = None
        try:
            raw_header, buffered = self._read_header(client)
            parsed = _parse_request(raw_header, buffered, limits=self._limits)
            if self._authorization is not None and not hmac.compare_digest(
                parsed.proxy_authorization, self._authorization
            ):
                raise _RequestFailure(407)
            from ..macos_network import domain_matches

            if (
                self._allowed_hosts is not None
                and not domain_matches(parsed.host, self._allowed_hosts)
                and (parsed.host, parsed.port) not in self._allowed_private_endpoints
            ):
                raise _RequestFailure(403)
            addresses = self._resolve_public(
                parsed.host,
                parsed.port,
                scheme=parsed.scheme,
            )
            upstream = self._connect(addresses, parsed.port)
            with self._lock:
                if self._stop.is_set():
                    raise _RequestFailure(503)
                self._active_sockets.add(upstream)
            upstream.settimeout(_POLL_SECONDS)
            transferred = 0
            if parsed.connect_tunnel:
                response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
                transferred = self._send_counted(client, response, transferred)
            elif parsed.upstream_header is not None:
                transferred = self._send_counted(upstream, parsed.upstream_header, transferred)
            if parsed.buffered_body:
                transferred = self._send_counted(upstream, parsed.buffered_body, transferred)
            self._relay(client, upstream, transferred=transferred)
        except _RequestFailure as exc:
            if upstream is None:
                _send_error_and_drain(
                    client, exc.status, max_drain_bytes=self._limits.max_header_bytes
                )
        except BaseException:
            if upstream is None:
                _send_error_and_drain(client, 502, max_drain_bytes=self._limits.max_header_bytes)
        finally:
            _close_socket(upstream)
            _close_socket(client)
            current = threading.current_thread()
            with self._lock:
                self._active_sockets.discard(client)
                if upstream is not None:
                    self._active_sockets.discard(upstream)
                self._workers.discard(current)

    def _start_helper(self, target: Callable[[], None], *, name: str) -> threading.Thread:
        def run() -> None:
            try:
                target()
            finally:
                with self._lock:
                    self._helpers.discard(threading.current_thread())

        helper = threading.Thread(target=run, name=name, daemon=True)
        with self._lock:
            if self._stop.is_set():
                raise _RequestFailure(503)
            self._helpers.add(helper)
        try:
            helper.start()
        except BaseException:
            with self._lock:
                self._helpers.discard(helper)
            raise
        return helper

    def _read_header(self, client: socket.socket) -> tuple[bytes, bytes]:
        deadline = time.monotonic() + self._limits.header_timeout_seconds
        payload = bytearray()
        while True:
            if self._stop.is_set():
                raise _RequestFailure(503)
            marker = payload.find(b"\r\n\r\n")
            if marker >= 0:
                end = marker + 4
                if end > self._limits.max_header_bytes:
                    raise _RequestFailure(431)
                return bytes(payload[:end]), bytes(payload[end:])
            if len(payload) >= self._limits.max_header_bytes:
                raise _RequestFailure(431)
            if time.monotonic() >= deadline:
                raise _RequestFailure(408)
            try:
                chunk = client.recv(min(4096, self._limits.max_header_bytes - len(payload)))
            except TimeoutError:
                continue
            except OSError as exc:
                raise _RequestFailure(400) from exc
            if not chunk:
                raise _RequestFailure(400)
            payload.extend(chunk)

    def _resolve_public(self, host: str, port: int, *, scheme: str) -> tuple[str, ...]:
        with self._lock:
            if (host, port) in self._denied_endpoints:
                raise _RequestFailure(403)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            values = (str(literal),)
        else:
            if not self._resolution_slots.acquire(blocking=False):
                raise _RequestFailure(503)
            completed = threading.Event()
            outcome: dict[str, Any] = {}

            def resolve() -> None:
                try:
                    values: list[str] = []
                    iterator = iter(self._resolver(host, port))
                    for _ in range(self._limits.max_resolved_addresses + 1):
                        try:
                            values.append(next(iterator))
                        except StopIteration:
                            break
                    outcome["values"] = tuple(values)
                except BaseException as exc:  # noqa: BLE001 - normalized below
                    outcome["error"] = exc
                finally:
                    self._resolution_slots.release()
                    completed.set()

            try:
                self._start_helper(resolve, name="alysis-browser-proxy-dns")
            except BaseException:
                self._resolution_slots.release()
                raise
            deadline = time.monotonic() + self._limits.dns_timeout_seconds
            while not completed.wait(_POLL_SECONDS):
                if self._stop.is_set():
                    raise _RequestFailure(503)
                if time.monotonic() >= deadline:
                    raise _RequestFailure(504)
            if "error" in outcome:
                raise _RequestFailure(502)
            raw_values = outcome.get("values", ())
            if not isinstance(raw_values, tuple):
                raise _RequestFailure(502)
            if len(raw_values) > self._limits.max_resolved_addresses:
                raise _RequestFailure(403)
            parsed_values: list[str] = []
            try:
                for value in raw_values:
                    address = ipaddress.ip_address(str(value))
                    normalized = str(address)
                    if normalized not in parsed_values:
                        parsed_values.append(normalized)
            except ValueError as exc:
                raise _RequestFailure(403) from exc
            values = tuple(parsed_values)
        with self._lock:
            denied = any((value, port) in self._denied_endpoints for value in values)
        owned_preview_allowed = False
        if self._owned_preview_origin_allowed is not None:
            try:
                owned_preview_allowed = bool(self._owned_preview_origin_allowed(scheme, host, port))
            except Exception:
                owned_preview_allowed = False
        owned_preview_addresses_safe = bool(values) and all(
            _is_allowed_local_address(value) for value in values
        )
        if (
            not values
            or denied
            or (
                not self._allow_local_destinations
                and not (owned_preview_allowed and owned_preview_addresses_safe)
                and not (
                    values
                    and all((value, port) in self._allowed_private_endpoints for value in values)
                )
                and any(not _is_public_address(value) for value in values)
            )
            or (
                self._allow_local_destinations
                and self._local_destinations_loopback_only
                and any(not _is_public_or_loopback_address(value) for value in values)
            )
            or (
                self._allow_local_destinations
                and not self._local_destinations_loopback_only
                and any(not _is_allowed_local_address(value) for value in values)
            )
        ):
            raise _RequestFailure(403)
        return values

    def _connect(self, addresses: tuple[str, ...], port: int) -> ConnectedSocket:
        deadline = time.monotonic() + self._limits.connect_timeout_seconds
        for address in addresses:
            if self._stop.is_set():
                raise _RequestFailure(503)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                if self._dialer is not None:
                    connected = self._dial_injected_bounded(address, port, remaining)
                else:
                    connected = self._dial_numeric(address, port, remaining)
            except (OSError, TimeoutError):
                continue
            if connected is None:
                continue
            return connected
        raise _RequestFailure(502)

    def _dial_injected_bounded(self, address: str, port: int, timeout: float) -> ConnectedSocket:
        """Bound an injected dialer and close any socket returned after abandonment."""

        if self._dialer is None:
            raise OSError("dialer unavailable")
        completed = threading.Event()
        abandoned = threading.Event()
        outcome: dict[str, Any] = {}
        outcome_lock = threading.Lock()

        def dial() -> None:
            connected: ConnectedSocket | None = None
            try:
                connected = self._dialer(address, port, timeout)
                if connected is None:
                    raise OSError("dialer returned no socket")
                with outcome_lock:
                    should_close = abandoned.is_set() or self._stop.is_set()
                    if not should_close:
                        outcome["connected"] = connected
                if should_close:
                    _close_socket(connected)
            except BaseException as exc:  # noqa: BLE001 - normalized by caller
                with outcome_lock:
                    outcome["error"] = exc
            finally:
                completed.set()

        self._start_helper(dial, name="alysis-browser-proxy-dial")
        deadline = time.monotonic() + timeout
        while not completed.wait(_POLL_SECONDS):
            if self._stop.is_set() or time.monotonic() >= deadline:
                with outcome_lock:
                    abandoned.set()
                    late_socket = outcome.pop("connected", None)
                if late_socket is not None:
                    _close_socket(late_socket)
                raise TimeoutError
        with outcome_lock:
            connected = outcome.get("connected")
            error = outcome.get("error")
        if connected is not None:
            return connected
        if isinstance(error, BaseException):
            raise OSError("numeric dial failed") from error
        raise OSError("numeric dial failed")

    def _dial_numeric(self, address: str, port: int, timeout: float) -> socket.socket:
        parsed = ipaddress.ip_address(address)
        family = socket.AF_INET6 if isinstance(parsed, ipaddress.IPv6Address) else socket.AF_INET
        connected = socket.socket(family, socket.SOCK_STREAM)
        with self._lock:
            if self._stop.is_set():
                connected.close()
                raise OSError("proxy stopping")
            self._active_sockets.add(connected)
        try:
            connected.settimeout(timeout)
            if family == socket.AF_INET6:
                connected.connect((str(parsed), port, 0, 0))
            else:
                connected.connect((str(parsed), port))
            return connected
        except BaseException:
            with self._lock:
                self._active_sockets.discard(connected)
            connected.close()
            raise

    def _send_counted(self, destination: ConnectedSocket, payload: bytes, transferred: int) -> int:
        updated = self._consume_bytes(transferred, len(payload))
        try:
            destination.sendall(payload)
        except (OSError, TimeoutError) as exc:
            raise _RequestFailure(502) from exc
        return updated

    def _consume_bytes(self, transferred: int, amount: int) -> int:
        updated = transferred + amount
        if updated > self._limits.max_bytes_per_connection:
            raise _RequestFailure(509)
        with self._lock:
            if self._total_bytes + amount > self._limits.max_bytes_total:
                raise _RequestFailure(509)
            self._total_bytes += amount
        return updated

    def _relay(
        self, client: ConnectedSocket, upstream: ConnectedSocket, *, transferred: int
    ) -> None:
        started = time.monotonic()
        last_activity = started
        readable: list[ConnectedSocket] = [client, upstream]
        while readable and not self._stop.is_set():
            now = time.monotonic()
            if now - started >= self._limits.connection_lifetime_seconds:
                raise _RequestFailure(504)
            if now - last_activity >= self._limits.idle_timeout_seconds:
                raise _RequestFailure(504)
            wait = min(
                _POLL_SECONDS,
                self._limits.connection_lifetime_seconds - (now - started),
                self._limits.idle_timeout_seconds - (now - last_activity),
            )
            try:
                ready, _, _ = select.select(readable, [], [], max(0.0, wait))
            except (OSError, ValueError) as exc:
                if self._stop.is_set():
                    return
                raise _RequestFailure(502) from exc
            for source in ready:
                destination = upstream if source is client else client
                try:
                    payload = source.recv(_RELAY_CHUNK_BYTES)
                except TimeoutError:
                    continue
                except OSError as exc:
                    raise _RequestFailure(502) from exc
                if not payload:
                    readable.remove(source)
                    with suppress(OSError):
                        destination.shutdown(socket.SHUT_WR)
                    continue
                transferred = self._send_counted(destination, payload, transferred)
                last_activity = time.monotonic()

    def _fail_proxy(self, message: str) -> None:
        error = BrowserEgressProxyError(message)
        with self._lock:
            if self._terminal_error is None:
                self._terminal_error = error
            self._stop.set()
            listener = self._listener
            self._listener = None
            sockets = tuple(self._active_sockets)
        _close_socket(listener)
        for item in sockets:
            _close_socket(item)


def start_browser_egress_proxy(
    *,
    limits: BrowserEgressProxyLimits | None = None,
    resolver: AddressResolver | None = None,
    dialer: NumericDialer | None = None,
    allow_local_destinations: bool = False,
    local_destinations_loopback_only: bool = False,
) -> BrowserEgressProxy:
    return BrowserEgressProxy(
        limits=limits,
        resolver=resolver,
        dialer=dialer,
        allow_local_destinations=allow_local_destinations,
        local_destinations_loopback_only=local_destinations_loopback_only,
    ).start()


def _parse_request(
    raw_header: bytes,
    buffered: bytes,
    *,
    limits: BrowserEgressProxyLimits,
) -> _ParsedRequest:
    if not raw_header.endswith(b"\r\n\r\n") or b"\x00" in raw_header:
        raise _RequestFailure(400)
    lines = raw_header[:-4].split(b"\r\n")
    if not lines or len(lines[0]) > limits.max_header_line_bytes:
        raise _RequestFailure(414)
    request_parts = lines[0].split(b" ")
    if len(request_parts) != 3 or any(not item for item in request_parts):
        raise _RequestFailure(400)
    method, target, version = request_parts
    if not _TOKEN_RE.fullmatch(method) or version not in {b"HTTP/1.0", b"HTTP/1.1"}:
        raise _RequestFailure(400)
    if len(target) > limits.max_header_line_bytes or any(
        value < 0x21 or value == 0x7F for value in target
    ):
        raise _RequestFailure(400)
    if len(lines) - 1 > limits.max_header_count:
        raise _RequestFailure(431)
    headers: list[tuple[bytes, bytes]] = []
    grouped: dict[bytes, list[bytes]] = {}
    for line in lines[1:]:
        if not line or len(line) > limits.max_header_line_bytes or line[:1] in b" \t":
            raise _RequestFailure(400)
        if b":" not in line:
            raise _RequestFailure(400)
        name, value = line.split(b":", 1)
        if not _TOKEN_RE.fullmatch(name) or value[:1] in b" \t" and not value.strip(b" \t"):
            raise _RequestFailure(400)
        stripped = value.strip(b" \t")
        if any(item < 0x20 and item != 0x09 or item == 0x7F for item in stripped):
            raise _RequestFailure(400)
        lowered = name.lower()
        headers.append((lowered, stripped))
        grouped.setdefault(lowered, []).append(stripped)
    for singleton in (
        b"host",
        b"content-length",
        b"transfer-encoding",
        b"upgrade",
        b"proxy-authorization",
    ):
        if len(grouped.get(singleton, ())) > 1:
            raise _RequestFailure(400)
    if len(grouped.get(b"host", ())) != 1:
        raise _RequestFailure(400)
    if b"content-length" in grouped and b"transfer-encoding" in grouped:
        raise _RequestFailure(400)
    # HTTP/1.0 clients (including curl after a 1.0 redirect) use the same
    # validated authority and framing path. Chunked bodies require HTTP/1.1.
    if version == b"HTTP/1.0" and b"transfer-encoding" in grouped:
        raise _RequestFailure(400)
    if b"content-length" in grouped:
        raw_length = grouped[b"content-length"][0]
        if not raw_length.isdigit():
            raise _RequestFailure(400)
    if b"transfer-encoding" in grouped and grouped[b"transfer-encoding"][0].lower() != b"chunked":
        raise _RequestFailure(400)
    connection_tokens = _connection_tokens(grouped.get(b"connection", ()))
    authorization = grouped.get(b"proxy-authorization", [b""])[0]
    if any(
        item in {b"host", b"content-length", b"transfer-encoding"} for item in connection_tokens
    ):
        raise _RequestFailure(400)

    if method == b"CONNECT":
        if b"content-length" in grouped or b"transfer-encoding" in grouped or b"upgrade" in grouped:
            raise _RequestFailure(400)
        host, port = _parse_authority(target, default_port=None)
        # HTTPS clients may omit the default TLS port from Host even though
        # CONNECT's request target must carry it. Keep both authorities equal.
        host_value, host_port = _parse_authority(grouped[b"host"][0], default_port=443)
        if host != host_value or port != host_port:
            raise _RequestFailure(400)
        return _ParsedRequest(method, "https", host, port, None, buffered, True, authorization)
    if method not in _METHODS:
        raise _RequestFailure(405)
    try:
        target_text = target.decode("ascii")
        parsed = urlsplit(target_text)
        parsed_port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise _RequestFailure(400) from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "ws"} or not parsed.netloc or parsed.fragment:
        raise _RequestFailure(400)
    if parsed.username is not None or parsed.password is not None or "\\" in parsed.netloc:
        raise _RequestFailure(400)
    default_port = 80
    host, port = _normalize_host_port(parsed.hostname, parsed_port or default_port)
    host_value, host_port = _parse_authority(grouped[b"host"][0], default_port=default_port)
    if host != host_value or port != host_port:
        raise _RequestFailure(400)
    upgrade = grouped.get(b"upgrade", [b""])[0].lower()
    websocket = scheme == "ws" or upgrade == b"websocket"
    if websocket != (upgrade == b"websocket" and b"upgrade" in connection_tokens):
        raise _RequestFailure(400)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        origin_target = path.encode("ascii")
    except UnicodeError as exc:
        raise _RequestFailure(400) from exc
    blocked_headers = {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
    } | connection_tokens
    output = [method + b" " + origin_target + b" HTTP/1.1"]
    for name, value in headers:
        if name in blocked_headers or name == b"upgrade":
            continue
        output.append(name + b": " + value)
    if websocket:
        output.extend((b"connection: Upgrade", b"upgrade: websocket"))
    else:
        output.append(b"connection: close")
    return _ParsedRequest(
        method,
        scheme,
        host,
        port,
        b"\r\n".join(output) + b"\r\n\r\n",
        buffered,
        False,
        authorization,
    )


def _connection_tokens(values: list[bytes] | tuple[bytes, ...]) -> set[bytes]:
    output: set[bytes] = set()
    for value in values:
        for item in value.split(b","):
            token = item.strip().lower()
            if not token or not _TOKEN_RE.fullmatch(token):
                raise _RequestFailure(400)
            output.add(token)
    return output


def _parse_authority(value: bytes, *, default_port: int | None) -> tuple[str, int]:
    try:
        text = value.decode("ascii")
    except UnicodeError as exc:
        raise _RequestFailure(400) from exc
    if (
        not text
        or any(ord(item) < 0x21 or ord(item) == 0x7F for item in text)
        or any(item in text for item in "/?#@\\,")
    ):
        raise _RequestFailure(400)
    if text.startswith("["):
        end = text.find("]")
        if end <= 1 or text.find("]", end + 1) >= 0:
            raise _RequestFailure(400)
        host_text = text[1:end]
        suffix = text[end + 1 :]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                raise _RequestFailure(400)
            port = int(suffix[1:])
        elif default_port is not None:
            port = default_port
        else:
            raise _RequestFailure(400)
        try:
            address = ipaddress.IPv6Address(host_text)
        except ValueError as exc:
            raise _RequestFailure(400) from exc
        return _normalize_host_port(str(address), port)
    if text.count(":") > 1:
        raise _RequestFailure(400)
    if ":" in text:
        host_text, raw_port = text.rsplit(":", 1)
        if not raw_port.isdigit():
            raise _RequestFailure(400)
        port = int(raw_port)
    elif default_port is not None:
        host_text = text
        port = default_port
    else:
        raise _RequestFailure(400)
    return _normalize_host_port(host_text, port)


def _normalize_host_port(host: str | None, port: int) -> tuple[str, int]:
    if not host or isinstance(port, bool) or not 1 <= port <= 65535:
        raise _RequestFailure(400)
    normalized = host.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            normalized = normalized.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise _RequestFailure(400) from exc
        if (
            not normalized
            or len(normalized) > 253
            or any(not _DNS_LABEL_RE.fullmatch(label) for label in normalized.split("."))
        ):
            raise _RequestFailure(400) from None
    else:
        normalized = str(address)
    return normalized, port


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if _is_transition_address(address):
        return False
    return bool(address.is_global) and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _is_allowed_local_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if _is_transition_address(address):
        return False
    return not any(
        (
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _is_public_or_loopback_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if _is_transition_address(address):
        return False
    return bool(address.is_loopback or _is_public_address(value))


def _is_transition_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
        or any(address in network for network in _NAT64_NETWORKS)
    )


def _default_resolver(host: str, port: int) -> Iterable[str]:
    return {
        item[4][0]
        for item in socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    }


def _send_error(client: ConnectedSocket, status: int) -> None:
    messages = {
        400: b"Bad Request",
        403: b"Forbidden",
        405: b"Method Not Allowed",
        407: b"Proxy Authentication Required",
        408: b"Request Timeout",
        414: b"URI Too Long",
        431: b"Request Header Fields Too Large",
        502: b"Bad Gateway",
        503: b"Service Unavailable",
        504: b"Gateway Timeout",
        509: b"Bandwidth Limit Exceeded",
    }
    reason = messages.get(status, b"Bad Gateway")
    body = b"Managed browser request blocked."
    response = (
        b"HTTP/1.1 "
        + str(status).encode("ascii")
        + b" "
        + reason
        + b"\r\nContent-Type: text/plain\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + (b'\r\nProxy-Authenticate: Basic realm="command"' if status == 407 else b"")
        + b"\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
        + body
    )
    with suppress(OSError, TimeoutError):
        client.sendall(response)


def _send_error_and_drain(client: socket.socket, status: int, *, max_drain_bytes: int) -> None:
    """Deliver a bounded error without a Windows unread-data connection reset."""

    _send_error(client, status)
    with suppress(OSError):
        client.shutdown(socket.SHUT_WR)
    try:
        client.settimeout(0.05)
    except OSError:
        return
    remaining = max_drain_bytes
    with suppress(OSError, TimeoutError):
        while remaining > 0:
            payload = client.recv(min(4096, remaining))
            if not payload:
                break
            remaining -= len(payload)
            if b"\r\n\r\n" in payload:
                break
    with suppress(OSError):
        client.close()


def _close_socket(value: ConnectedSocket | socket.socket | None) -> None:
    if value is None:
        return
    with suppress(OSError):
        value.shutdown(socket.SHUT_RDWR)
    with suppress(OSError):
        value.close()
