from __future__ import annotations

import base64
import socket
import threading
import time
from collections.abc import Iterable
from urllib.parse import urlsplit

import pytest

from alysis_code.ide.browser_egress_proxy import (
    BrowserEgressProxy,
    BrowserEgressProxyCleanupError,
    BrowserEgressProxyLimits,
    BrowserEgressProxySecurityError,
)

_PUBLIC_V4 = "93.184.216.34"
_PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


class RecordingDialer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self.peers: list[socket.socket] = []
        self.called = threading.Event()

    def __call__(self, address: str, port: int, timeout: float) -> socket.socket:
        local, peer = socket.socketpair()
        local.settimeout(timeout)
        peer.settimeout(1.0)
        self.calls.append((address, port, timeout))
        self.peers.append(peer)
        self.called.set()
        return local


def _public_resolver(_host: str, _port: int) -> Iterable[str]:
    return (_PUBLIC_V4,)


def _connect(proxy: BrowserEgressProxy) -> socket.socket:
    client = socket.create_connection((proxy.host, proxy.port), timeout=1.0)
    client.settimeout(1.0)
    return client


def _receive_until(connection: socket.socket, marker: bytes) -> bytes:
    output = bytearray()
    while marker not in output:
        chunk = connection.recv(4096)
        if not chunk:
            break
        output.extend(chunk)
    return bytes(output)


def _request(proxy: BrowserEgressProxy, payload: bytes) -> bytes:
    with _connect(proxy) as client:
        client.sendall(payload)
        return _receive_until(client, b"\r\n\r\n")


def test_proxy_starts_on_loopback_and_shutdown_is_idempotent() -> None:
    proxy = BrowserEgressProxy(resolver=_public_resolver, dialer=RecordingDialer()).start()
    assert proxy.host == "127.0.0.1"
    assert proxy.proxy_url == f"http://127.0.0.1:{proxy.port}"
    assert proxy.healthy

    endpoint = (proxy.host, proxy.port)
    proxy.close()
    proxy.close()

    assert not proxy.healthy
    with pytest.raises(OSError):
        socket.create_connection(endpoint, timeout=0.2)


@pytest.mark.parametrize("method", ["GET", "CONNECT"])
def test_command_proxy_credentials_are_isolated_and_never_forwarded(method):
    dialer = RecordingDialer()
    with (
        BrowserEgressProxy(
            require_authentication=True, resolver=_public_resolver, dialer=dialer
        ) as owner,
        BrowserEgressProxy(require_authentication=True) as other,
    ):

        def authorization(proxy):
            url = urlsplit(proxy.proxy_url)
            return b"Basic " + base64.b64encode(f"{url.username}:{url.password}".encode())

        target = b"example.test:443" if method == "CONNECT" else b"http://example.test/"
        header = method.encode() + b" " + target + b" HTTP/1.1\r\nHost: example.test\r\n"
        for auth in (b"", authorization(other)):
            request = (
                header + (b"Proxy-Authorization: " + auth + b"\r\n" if auth else b"") + b"\r\n"
            )
            assert _request(owner, request).startswith(b"HTTP/1.1 407")
        assert not dialer.calls
        with _connect(owner) as client:
            client.sendall(header + b"Proxy-Authorization: " + authorization(owner) + b"\r\n\r\n")
            assert dialer.called.wait(timeout=1)
            if method == "CONNECT":
                assert _receive_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
            else:
                upstream = _receive_until(dialer.peers[-1], b"\r\n\r\n")
                assert b"proxy-authorization" not in upstream.lower()
        for peer in dialer.peers:
            peer.close()


def test_exact_domain_policy_rejects_other_hosts_before_dns_or_connect():
    resolutions = []
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        allowed_hosts=frozenset({"example.test"}),
        resolver=lambda host, port: resolutions.append(host) or (_PUBLIC_V4,),
        dialer=dialer,
    ) as proxy:
        for host in ("other.test", "sub.example.test", "127.0.0.1", "example.test.evil.test"):
            response = _request(
                proxy, f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode()
            )
            assert response.startswith(b"HTTP/1.1 403")
        assert not resolutions and not dialer.calls
        with _connect(proxy) as client:
            client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
            assert _receive_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
    assert resolutions == ["example.test"]


def test_allowlisted_domain_does_not_allow_private_dns_answers():
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        allowed_hosts=frozenset({"example.test"}), resolver=lambda *_: ("127.0.0.1",), dialer=dialer
    ) as proxy:
        assert _request(
            proxy, b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n"
        ).startswith(b"HTTP/1.1 403")
        assert not dialer.calls


def test_connect_accepts_implicit_tls_host_port_without_relaxing_destination_policy():
    resolutions = []
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        allowed_hosts=frozenset({"example.test"}),
        resolver=lambda host, port: resolutions.append(host) or (_PUBLIC_V4,),
        dialer=dialer,
    ) as proxy:
        for target, host in (
            ("example.test:443", "other.test"),
            ("example.test:443", "example.test:8443"),
            ("example.test:8443", "example.test"),
        ):
            assert _request(
                proxy, f"CONNECT {target} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
            ).startswith(b"HTTP/1.1 400")
        assert _request(
            proxy, b"CONNECT other.test:443 HTTP/1.1\r\nHost: other.test\r\n\r\n"
        ).startswith(b"HTTP/1.1 403")
        assert not resolutions and not dialer.calls
        with _connect(proxy) as client:
            client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test\r\n\r\n")
            assert _receive_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
    assert resolutions == ["example.test"]


@pytest.mark.parametrize("version", [b"HTTP/1.0", b"HTTP/1.1"])
def test_absolute_form_http_is_sanitized_and_dials_only_numeric_ip(version: bytes) -> None:
    dialer = RecordingDialer()
    with BrowserEgressProxy(resolver=_public_resolver, dialer=dialer) as proxy:
        with _connect(proxy) as client:
            client.sendall(
                b"GET http://example.test/path?q=token-secret " + version + b"\r\n"
                b"Host: example.test\r\n"
                b"Proxy-Authorization: Basic should-not-leak\r\n"
                b"Proxy-Connection: keep-alive\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            assert dialer.called.wait(1.0)
            upstream = dialer.peers[0]
            forwarded = _receive_until(upstream, b"\r\n\r\n")
            assert forwarded.startswith(b"GET /path?q=token-secret HTTP/1.1\r\n")
            assert b"host: example.test\r\n" in forwarded
            assert b"connection: close\r\n" in forwarded
            assert b"Proxy-Authorization" not in forwarded
            assert b"should-not-leak" not in forwarded
            upstream.sendall(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            upstream.shutdown(socket.SHUT_WR)
            assert _receive_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 204")

    assert [(address, port) for address, port, _ in dialer.calls] == [(_PUBLIC_V4, 80)]


def test_connect_tunnels_buffered_bytes_to_pinned_numeric_address() -> None:
    dialer = RecordingDialer()
    with BrowserEgressProxy(resolver=_public_resolver, dialer=dialer) as proxy:
        with _connect(proxy) as client:
            client.sendall(
                b"CONNECT example.test:443 HTTP/1.1\r\n"
                b"Host: example.test:443\r\n\r\n"
                b"early-tls-bytes"
            )
            assert _receive_until(client, b"\r\n\r\n").startswith(
                b"HTTP/1.1 200 Connection Established"
            )
            assert dialer.called.wait(1.0)
            upstream = dialer.peers[0]
            assert upstream.recv(64) == b"early-tls-bytes"
            client.sendall(b"client-record")
            assert upstream.recv(64) == b"client-record"
            upstream.sendall(b"server-record")
            assert client.recv(64) == b"server-record"

    assert dialer.calls[0][0] == _PUBLIC_V4


@pytest.mark.parametrize(
    "answers",
    [
        b"GET http://example.test/ HTTP/2.0\r\nHost: example.test\r\n\r\n",
        b"POST http://example.test/ HTTP/1.0\r\nHost: example.test\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"GET http://example.test/ HTTP/1.0\r\nHost: other.test\r\n\r\n",
        b"GET http://example.test/ HTTP/1.0\r\n\r\n",
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("10.0.0.1",),
        (_PUBLIC_V4, "127.0.0.1"),
        ("::ffff:127.0.0.1",),
        ("2002:7f00:1::",),
        ("64:ff9b::7f00:1",),
    ],
)
def test_default_policy_rejects_private_metadata_transition_and_mixed_answers(
    answers: tuple[str, ...],
) -> None:
    dialer = RecordingDialer()
    with BrowserEgressProxy(resolver=lambda _host, _port: answers, dialer=dialer) as proxy:
        response = _request(
            proxy,
            b"CONNECT secret-name.test:443 HTTP/1.1\r\nHost: secret-name.test:443\r\n\r\n",
        )
    assert response.startswith(b"HTTP/1.1 403 Forbidden")
    assert b"secret-name" not in response
    assert dialer.calls == []


def test_resolution_is_pinned_and_rebinding_on_next_request_is_rejected() -> None:
    dialer = RecordingDialer()
    answers = iter(((_PUBLIC_V4,), ("127.0.0.1",)))

    def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return next(answers)

    with BrowserEgressProxy(resolver=resolver, dialer=dialer) as proxy:
        first = _connect(proxy)
        first.sendall(b"CONNECT rebind.test:443 HTTP/1.1\r\nHost: rebind.test:443\r\n\r\n")
        assert _receive_until(first, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
        first.close()
        second = _request(
            proxy,
            b"CONNECT rebind.test:443 HTTP/1.1\r\nHost: rebind.test:443\r\n\r\n",
        )

    assert second.startswith(b"HTTP/1.1 403")
    assert [(address, port) for address, port, _ in dialer.calls] == [(_PUBLIC_V4, 443)]


def test_explicit_local_mode_still_pins_and_honors_permanent_denials() -> None:
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        resolver=lambda _host, _port: ("127.0.0.1",),
        dialer=dialer,
        allow_local_destinations=True,
    ) as proxy:
        allowed = _connect(proxy)
        allowed.sendall(b"CONNECT local.test:8080 HTTP/1.1\r\nHost: local.test:8080\r\n\r\n")
        assert _receive_until(allowed, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
        allowed.close()
        proxy.deny_endpoint("127.0.0.1", 9222)
        denied = _request(
            proxy,
            b"CONNECT local.test:9222 HTTP/1.1\r\nHost: local.test:9222\r\n\r\n",
        )
        self_denied = _request(
            proxy,
            f"CONNECT 127.0.0.1:{proxy.port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{proxy.port}\r\n\r\n".encode(),
        )

    assert denied.startswith(b"HTTP/1.1 403")
    assert self_denied.startswith(b"HTTP/1.1 403")
    assert [(address, port) for address, port, _ in dialer.calls] == [("127.0.0.1", 8080)]


def test_owned_preview_origin_allows_only_exact_scheme_host_and_port() -> None:
    dialer = RecordingDialer()

    def owned_preview(scheme: str, host: str, port: int) -> bool:
        return (scheme, host, port) == ("http", "127.0.0.1", 3000)

    with BrowserEgressProxy(
        resolver=lambda _host, _port: ("127.0.0.1",),
        dialer=dialer,
        owned_preview_origin_allowed=owned_preview,
    ) as proxy:
        allowed_client = _connect(proxy)
        allowed_client.sendall(
            b"GET http://127.0.0.1:3000/app HTTP/1.1\r\nHost: 127.0.0.1:3000\r\n\r\n"
        )
        assert dialer.called.wait(1.0)
        dialer.peers[0].sendall(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
        dialer.peers[0].shutdown(socket.SHUT_WR)
        allowed = _receive_until(allowed_client, b"\r\n\r\n")
        allowed_client.close()
        wrong_port = _request(
            proxy,
            b"GET http://127.0.0.1:3001/app HTTP/1.1\r\nHost: 127.0.0.1:3001\r\n\r\n",
        )
        wrong_scheme = _request(
            proxy,
            b"CONNECT 127.0.0.1:3000 HTTP/1.1\r\nHost: 127.0.0.1:3000\r\n\r\n",
        )

    assert allowed.startswith(b"HTTP/1.1 204")
    assert wrong_port.startswith(b"HTTP/1.1 403")
    assert wrong_scheme.startswith(b"HTTP/1.1 403")
    assert [(address, port) for address, port, _ in dialer.calls] == [("127.0.0.1", 3000)]


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "224.0.0.1", "240.0.0.1", "::", "ff02::1", "::ffff:127.0.0.1"],
)
def test_local_mode_still_rejects_unspecified_multicast_reserved_and_transition(
    address: str,
) -> None:
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        resolver=lambda _host, _port: (address,),
        dialer=dialer,
        allow_local_destinations=True,
    ) as proxy:
        response = _request(
            proxy,
            b"CONNECT local.test:443 HTTP/1.1\r\nHost: local.test:443\r\n\r\n",
        )
    assert response.startswith(b"HTTP/1.1 403")
    assert dialer.calls == []


@pytest.mark.parametrize("address", ["10.0.0.8", "192.168.1.20", "169.254.1.2", "fe80::1"])
def test_loopback_only_mode_rejects_lan_and_link_local_addresses(address: str) -> None:
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        resolver=lambda _host, _port: (address,),
        dialer=dialer,
        allow_local_destinations=True,
        local_destinations_loopback_only=True,
    ) as proxy:
        response = _request(
            proxy,
            b"CONNECT local.test:443 HTTP/1.1\r\nHost: local.test:443\r\n\r\n",
        )
    assert response.startswith(b"HTTP/1.1 403")
    assert dialer.calls == []


def test_loopback_only_mode_allows_public_and_loopback_but_not_mixed_private_dns() -> None:
    dialer = RecordingDialer()
    answers = iter((("127.0.0.1",), (_PUBLIC_V4,), ("127.0.0.1", "10.0.0.8")))
    with BrowserEgressProxy(
        resolver=lambda _host, _port: next(answers),
        dialer=dialer,
        allow_local_destinations=True,
        local_destinations_loopback_only=True,
    ) as proxy:
        loopback = _request(
            proxy,
            b"CONNECT localhost.test:3000 HTTP/1.1\r\nHost: localhost.test:3000\r\n\r\n",
        )
        public = _request(
            proxy,
            b"CONNECT public.test:443 HTTP/1.1\r\nHost: public.test:443\r\n\r\n",
        )
        mixed = _request(
            proxy,
            b"CONNECT mixed.test:443 HTTP/1.1\r\nHost: mixed.test:443\r\n\r\n",
        )
    assert loopback.startswith(b"HTTP/1.1 200")
    assert public.startswith(b"HTTP/1.1 200")
    assert mixed.startswith(b"HTTP/1.1 403")


def test_resolver_iterable_is_consumed_only_to_the_configured_bound() -> None:
    limits = BrowserEgressProxyLimits(max_resolved_addresses=2)
    yielded: list[int] = []

    def resolver(_host: str, _port: int) -> Iterable[str]:
        for index in range(1000):
            yielded.append(index)
            yield _PUBLIC_V4

    dialer = RecordingDialer()
    with BrowserEgressProxy(limits=limits, resolver=resolver, dialer=dialer) as proxy:
        response = _request(
            proxy,
            b"CONNECT bounded.test:443 HTTP/1.1\r\nHost: bounded.test:443\r\n\r\n",
        )
    assert response.startswith(b"HTTP/1.1 403")
    assert yielded == [0, 1, 2]
    assert dialer.calls == []


def test_ipv6_public_answer_is_passed_to_dialer_as_numeric_ip() -> None:
    dialer = RecordingDialer()
    with BrowserEgressProxy(resolver=lambda _host, _port: (_PUBLIC_V6,), dialer=dialer) as proxy:
        client = _connect(proxy)
        client.sendall(b"CONNECT ipv6.test:443 HTTP/1.1\r\nHost: ipv6.test:443\r\n\r\n")
        assert _receive_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
        client.close()
    assert dialer.calls[0][0] == _PUBLIC_V6


@pytest.mark.parametrize(
    "raw_request",
    [
        b"GET /origin-form HTTP/1.1\r\nHost: example.test\r\n\r\n",
        b"GET https://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n",
        b"GET http://example.test/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
        b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\nHost: example.test\r\n\r\n",
        b"POST http://example.test/ HTTP/1.1\r\nHost: example.test\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"GET http://user:password@example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n",
        b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n folded: yes\r\n\r\n",
        b"CONNECT example.test HTTP/1.1\r\nHost: example.test\r\n\r\n",
        b"CONNECT example.test:443/path HTTP/1.1\r\nHost: example.test:443\r\n\r\n",
        b"CONNECT example.test:443 HTTP/1.1\r\nHost: other.test:443\r\n\r\n",
    ],
)
def test_malformed_and_smuggling_prone_requests_fail_before_dns_or_dial(
    raw_request: bytes,
) -> None:
    dialer = RecordingDialer()
    resolver_calls: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str, ...]:
        resolver_calls.append((host, port))
        return (_PUBLIC_V4,)

    with BrowserEgressProxy(resolver=resolver, dialer=dialer) as proxy:
        response = _request(proxy, raw_request)
    assert response.startswith(b"HTTP/1.1 4")
    assert resolver_calls == []
    assert dialer.calls == []


def test_oversized_and_slow_headers_are_bounded() -> None:
    limits = BrowserEgressProxyLimits(
        max_header_bytes=1024,
        max_header_line_bytes=512,
        header_timeout_seconds=0.05,
    )
    with BrowserEgressProxy(limits=limits, resolver=_public_resolver) as proxy:
        oversized = _request(
            proxy,
            b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\nX-Pad: "
            + (b"x" * 1100)
            + b"\r\n\r\n",
        )
        slow = _connect(proxy)
        slow.sendall(b"GET http://example.test/")
        timed_out = _receive_until(slow, b"\r\n\r\n")
        slow.close()
    assert oversized.startswith(b"HTTP/1.1 431")
    assert timed_out.startswith(b"HTTP/1.1 408")


def test_connection_limit_and_shutdown_close_active_client_and_tunnel() -> None:
    limits = BrowserEgressProxyLimits(max_connections=1)
    dialer = RecordingDialer()
    proxy = BrowserEgressProxy(limits=limits, resolver=_public_resolver, dialer=dialer).start()
    holding = _connect(proxy)
    holding.sendall(b"GET http://example.test/")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with proxy._lock:  # noqa: SLF001 - verifies the production bound deterministically
            if len(proxy._workers) == 1:  # noqa: SLF001
                break
        time.sleep(0.01)
    overloaded = _request(
        proxy,
        b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n",
    )
    assert overloaded.startswith(b"HTTP/1.1 503")

    proxy.close(timeout=1.0)
    assert holding.recv(1) == b""
    holding.close()
    assert not proxy.healthy
    with proxy._lock:  # noqa: SLF001
        assert not proxy._workers  # noqa: SLF001
        assert not proxy._active_sockets  # noqa: SLF001


def test_byte_limit_closes_connect_tunnel_without_forwarding_oversized_chunk() -> None:
    limits = BrowserEgressProxyLimits(
        max_bytes_per_connection=1024,
        max_bytes_total=1024,
    )
    dialer = RecordingDialer()
    with BrowserEgressProxy(limits=limits, resolver=_public_resolver, dialer=dialer) as proxy:
        client = _connect(proxy)
        client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
        assert _receive_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
        assert dialer.called.wait(1.0)
        client.sendall(b"x" * 1024)
        assert client.recv(1) == b""
        peer = dialer.peers[0]
        assert peer.recv(1) == b""
        client.close()


def test_shutdown_abandons_blocked_injected_dial_and_closes_late_socket() -> None:
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    peer_holder: list[socket.socket] = []

    def blocking_dialer(_address: str, _port: int, _timeout: float) -> socket.socket:
        entered.set()
        release.wait(2.0)
        local, peer = socket.socketpair()
        peer.settimeout(1.0)
        peer_holder.append(peer)
        returned.set()
        return local

    proxy = BrowserEgressProxy(resolver=_public_resolver, dialer=blocking_dialer).start()
    client = _connect(proxy)
    client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
    assert entered.wait(1.0)

    with pytest.raises(BrowserEgressProxyCleanupError, match="cleanup did not finish"):
        proxy.close(timeout=0.05)
    assert not proxy.healthy
    with proxy._lock:  # noqa: SLF001 - verifies the late helper remains owned
        assert proxy._workers  # noqa: SLF001
        assert all(item.fileno() < 0 for item in proxy._active_sockets)  # noqa: SLF001
        assert proxy._helpers  # noqa: SLF001
    release.set()
    assert returned.wait(1.0)
    assert peer_holder[0].recv(1) == b""
    peer_holder[0].close()
    client.close()
    proxy.close(timeout=1.0)
    with proxy._lock:  # noqa: SLF001 - exact retryable cleanup contract
        assert not proxy._workers  # noqa: SLF001
        assert not proxy._helpers  # noqa: SLF001
        assert not proxy._active_sockets  # noqa: SLF001


def test_blocked_dns_helper_keeps_cleanup_observable_until_retry() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_resolver(_host: str, _port: int) -> tuple[str, ...]:
        entered.set()
        release.wait(2.0)
        return (_PUBLIC_V4,)

    proxy = BrowserEgressProxy(resolver=blocking_resolver, dialer=RecordingDialer()).start()
    client = _connect(proxy)
    client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
    assert entered.wait(1.0)

    with pytest.raises(BrowserEgressProxyCleanupError, match="cleanup did not finish"):
        proxy.close(timeout=0.05)
    with proxy._lock:  # noqa: SLF001 - helper cannot silently outlive successful close
        assert any(thread.name == "alysis-browser-proxy-dns" for thread in proxy._helpers)  # noqa: SLF001

    release.set()
    proxy.close(timeout=1.0)
    client.close()
    with proxy._lock:  # noqa: SLF001 - retry proves exact helper ownership cleanup
        assert not proxy._helpers  # noqa: SLF001


def test_incomplete_worker_cleanup_is_observable_and_close_can_be_retried() -> None:
    proxy = BrowserEgressProxy().start()
    release = threading.Event()

    def non_cooperative_worker() -> None:
        release.wait(1.0)
        with proxy._lock:  # noqa: SLF001 - emulates a delayed production worker finalizer
            proxy._workers.discard(threading.current_thread())  # noqa: SLF001

    worker = threading.Thread(target=non_cooperative_worker, daemon=True)
    with proxy._lock:  # noqa: SLF001
        proxy._workers.add(worker)  # noqa: SLF001
    worker.start()

    with pytest.raises(BrowserEgressProxyCleanupError, match="cleanup did not finish"):
        proxy.close(timeout=0.05)
    assert isinstance(proxy.terminal_error, BrowserEgressProxyCleanupError)

    release.set()
    worker.join(timeout=1.0)
    proxy.close(timeout=1.0)


def test_invalid_policy_and_deny_endpoint_inputs_are_safe() -> None:
    with pytest.raises(BrowserEgressProxySecurityError, match="policy is invalid"):
        BrowserEgressProxy(allow_local_destinations=1)  # type: ignore[arg-type]
    proxy = BrowserEgressProxy()
    with pytest.raises(BrowserEgressProxySecurityError, match="endpoint is invalid"):
        proxy.deny_endpoint("user:secret@localhost", 9222)


@pytest.mark.parametrize(
    "pattern,apex_allowed", [("*.example.test", False), ("**.example.test", True)]
)
def test_domain_wildcards_observe_label_boundaries_before_dns(pattern, apex_allowed):
    resolutions = []
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        allowed_hosts=frozenset({pattern}),
        resolver=lambda h, p: resolutions.append(h) or (_PUBLIC_V4,),
        dialer=dialer,
    ) as proxy:
        for host in ("example.test.evil.test", "notexample.test", "127.0.0.1"):
            assert _request(
                proxy, f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
            ).startswith(b"HTTP/1.1 403")
        assert not resolutions
        for host in ("a.example.test", "a.b.example.test", "example.test"):
            with _connect(proxy) as client:
                client.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
                expected = (
                    b"HTTP/1.1 403"
                    if host == "example.test" and not apex_allowed
                    else b"HTTP/1.1 200"
                )
                assert _receive_until(client, b"\r\n\r\n").startswith(expected)


def test_private_exception_is_one_numeric_endpoint_not_general_private_dns_access():
    resolutions = []
    dialer = RecordingDialer()
    with BrowserEgressProxy(
        allowed_hosts=frozenset({"allowed.test"}),
        allowed_private_endpoints=frozenset({("127.0.0.1", 8080)}),
        resolver=lambda h, p: resolutions.append(h) or ("192.168.1.20",),
        dialer=dialer,
    ) as proxy:
        for target in (
            "127.0.0.1:8081",
            "127.0.0.2:8080",
            "allowed.test:8080",
            "169.254.169.254:80",
        ):
            assert _request(
                proxy, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
            ).startswith(b"HTTP/1.1 403")
        assert resolutions == ["allowed.test"] and not dialer.calls
        with _connect(proxy) as client:
            client.sendall(b"CONNECT 127.0.0.1:8080 HTTP/1.1\r\nHost: 127.0.0.1:8080\r\n\r\n")
            assert _receive_until(client, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
            client.sendall(b"arbitrary TCP data")
            assert dialer.peers[-1].recv(4096) == b"arbitrary TCP data"
    assert [(ip, port) for ip, port, _ in dialer.calls] == [("127.0.0.1", 8080)]
