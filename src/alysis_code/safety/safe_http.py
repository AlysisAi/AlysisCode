from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


class SafeHttpError(Exception):
    """``recoverable=True`` marks failures specific to the requested URL or host
    (a different target can succeed: oversized body, redirect problems, blocked
    or invalid host). The default ``False`` marks environment-level failures
    (network/DNS outage, invalid caller arguments)."""

    def __init__(self, message: str, *, recoverable: bool = False) -> None:
        super().__init__(message)
        self.recoverable = recoverable


_ALLOWED_SCHEMES = {"http", "https"}
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_DEFAULT_ACCEPT_ENCODING = "identity"
Resolver = Callable[[str, int], list[str]]
_DENIED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "::1/128",
        "169.254.0.0/16",
        "fe80::/10",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
        "0.0.0.0/32",
        "::/128",
        "224.0.0.0/4",
        "ff00::/8",
    )
)


@dataclass(frozen=True)
class _SafeTarget:
    request_url: str
    connect_url: str
    host_header: str


async def safe_http_request(
    method: str,
    url: str,
    *,
    timeout: float = 30.0,
    max_bytes: int = 10 * 1024 * 1024,
    allow_redirects: bool = True,
    max_redirects: int = 5,
    headers: dict[str, str] | None = None,
    json: Any = None,
    content: bytes | None = None,
    _transport: httpx.AsyncBaseTransport | None = None,
    _resolver: Resolver | None = None,
    truncate: bool = False,
) -> httpx.Response:
    """``truncate`` keeps the first ``max_bytes`` of a longer body instead of failing;
    the response's extensions then hold ``alysis_truncated: True``."""
    if max_bytes < 0:
        raise SafeHttpError("max_bytes must be non-negative.")
    if max_redirects < 0:
        raise SafeHttpError("max_redirects must be non-negative.")

    current_url = str(url or "").strip()
    if not current_url:
        raise SafeHttpError("url must be a non-empty string.")
    current_method = str(method or "").strip().upper() or "GET"
    request_headers = _headers_with_default_accept_encoding(headers)
    redirects_seen = 0
    request_json = json
    request_content = content

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        transport=_transport,
    ) as client:
        while True:
            target = _validate_and_resolve_target(
                current_url,
                resolver=_resolver or _resolve_host_addresses,
                use_resolved_connect_url=_transport is None,
            )
            try:
                response = await _single_request(
                    client=client,
                    method=current_method,
                    target=target,
                    headers=request_headers,
                    json=request_json,
                    content=request_content,
                    max_bytes=max_bytes,
                    truncate=truncate,
                )
            except httpx.DecodingError as exc:
                raise SafeHttpError(
                    "Response decompression failed: server or proxy returned invalid "
                    "Content-Encoding bytes. safe_http_request sends "
                    "Accept-Encoding: identity by default; check upstream compression handling.",
                    recoverable=True,
                ) from exc
            if not allow_redirects or response.status_code not in _REDIRECT_STATUS_CODES:
                return response

            location = str(response.headers.get("location") or "").strip()
            if not location:
                raise SafeHttpError(
                    f"Redirect response {response.status_code} missing Location header.",
                    recoverable=True,
                )
            if redirects_seen >= max_redirects:
                raise SafeHttpError(f"Too many redirects (>{max_redirects}).", recoverable=True)
            redirects_seen += 1
            current_url = urljoin(current_url, location)
            if response.status_code == 303 or (
                response.status_code in {301, 302} and current_method not in {"GET", "HEAD"}
            ):
                current_method = "GET"
                request_json = None
                request_content = None


def _headers_with_default_accept_encoding(headers: dict[str, str] | None) -> dict[str, str]:
    request_headers = dict(headers or {})
    if not any(key.lower() == "accept-encoding" for key in request_headers):
        request_headers["Accept-Encoding"] = _DEFAULT_ACCEPT_ENCODING
    return request_headers


async def _single_request(
    *,
    client: httpx.AsyncClient,
    method: str,
    target: _SafeTarget,
    headers: dict[str, str],
    json: Any,
    content: bytes | None,
    max_bytes: int,
    truncate: bool = False,
) -> httpx.Response:
    outbound_headers = dict(headers)
    outbound_headers["Host"] = target.host_header
    request = httpx.Request(method, target.request_url, headers=outbound_headers)
    body = bytearray()
    response_headers: httpx.Headers | None = None
    status_code = 0
    extensions: dict[str, Any] = {}
    async with client.stream(
        method,
        target.connect_url,
        headers=outbound_headers,
        json=json,
        content=content,
    ) as response:
        status_code = int(response.status_code)
        response_headers = httpx.Headers(response.headers)
        extensions = dict(response.extensions)
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                if not truncate:
                    raise SafeHttpError(
                        f"Response body exceeded max_bytes={max_bytes}.", recoverable=True
                    )
                body.extend(chunk[: max_bytes - len(body)])
                extensions["alysis_truncated"] = True
                break
            body.extend(chunk)
    return httpx.Response(
        status_code=status_code,
        headers=response_headers,
        content=bytes(body),
        request=request,
        extensions=extensions,
    )


def _validate_and_resolve_target(
    url: str,
    *,
    resolver: Resolver,
    use_resolved_connect_url: bool,
) -> _SafeTarget:
    split = urlsplit(str(url or "").strip())
    scheme = split.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SafeHttpError(
            f"Unsupported URL scheme: {split.scheme or '(missing)'}", recoverable=True
        )
    if split.hostname is None:
        raise SafeHttpError("URL must include a hostname.", recoverable=True)
    if split.username is not None or split.password is not None:
        raise SafeHttpError("Embedded URL credentials are not allowed.", recoverable=True)
    try:
        port = split.port
    except ValueError as exc:
        raise SafeHttpError("URL has an invalid port value.", recoverable=True) from exc
    if port is None:
        port = 443 if scheme == "https" else 80

    host = split.hostname
    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        reason = _blocked_ip_reason(literal_address)
        if reason is not None:
            raise SafeHttpError(
                f"Blocked URL host '{host}': resolved to denied address(es): "
                f"{literal_address.compressed} ({reason}).",
                recoverable=True,
            )

    addresses = resolver(host, port)
    parsed_addresses = [_parse_ip_address(address, host=host) for address in addresses]
    blocked = [
        f"{address.compressed} ({_blocked_ip_reason(address)})"
        for address in parsed_addresses
        if _blocked_ip_reason(address) is not None
    ]
    if blocked:
        raise SafeHttpError(
            f"Blocked URL host '{host}': resolved to denied address(es): {', '.join(blocked)}.",
            recoverable=True,
        )

    host_header = _host_header(host=host, port=split.port, scheme=scheme)
    request_url = urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))
    if use_resolved_connect_url and scheme != "https":
        connect_ip = parsed_addresses[0].compressed
        connect_host = f"[{connect_ip}]" if ":" in connect_ip else connect_ip
        connect_netloc = _host_header(host=connect_host, port=port, scheme=scheme, force_port=True)
        connect_url = urlunsplit((split.scheme, connect_netloc, split.path, split.query, ""))
    else:
        # HTTPS must keep the original hostname for SNI and certificate validation.
        # The DNS result is still preflight-validated above for SSRF-denied ranges.
        connect_url = request_url
    return _SafeTarget(
        request_url=request_url,
        connect_url=connect_url,
        host_header=host_header,
    )


def _resolve_host_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SafeHttpError(f"Failed to resolve host '{host}': {exc}") from exc
    addresses: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        if not sockaddr:
            continue
        address = str(sockaddr[0] or "").strip()
        if address and address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise SafeHttpError(f"Host '{host}' did not resolve to any address.", recoverable=True)
    return addresses


def _parse_ip_address(value: str, *, host: str) -> ipaddress._BaseAddress:
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise SafeHttpError(
            f"Resolver returned a non-IP address for host '{host}': {value!r}",
            recoverable=True,
        ) from exc


def _blocked_ip_reason(address: ipaddress._BaseAddress) -> str | None:
    for network in _DENIED_NETWORKS:
        if address.version == network.version and address in network:
            return str(network)
    return None


def _host_header(
    *,
    host: str,
    port: int | None,
    scheme: str,
    force_port: bool = False,
) -> str:
    bracketed_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = 443 if scheme == "https" else 80
    if port is None or (port == default_port and not force_port):
        return bracketed_host
    return f"{bracketed_host}:{port}"
