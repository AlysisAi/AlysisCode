from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..safety import SafeHttpError, safe_http_request
from ..web_research import canonicalize_web_url_input, normalize_web_url
from .http_timeout import build_http_timeout_budget, format_http_timeout_error


class WebFetchError(RuntimeError):
    """Raised for any web_fetch failure.

    ``recoverable=True`` marks failures the calling model can fix by adjusting its
    own tool arguments (input validation) or by picking a different source. The
    agent loop returns those to the model as plain errors instead of disabling
    web tools for the rest of the turn.

    ``remote_site_reason`` is a short user-facing phrase ("site requires
    sign-in") set when the failure is the remote site's doing rather than
    Alysis's or the local network's: it refused automated clients, stalled, or
    dropped the connection. Surfaces render these as neutral trace notices, not
    failures; the correct model response is to pick a different source, never to
    retry the same host. ``blocked_by_remote_site=True`` narrows that to explicit
    refusals — the server answered and said no (anti-bot/challenge pages, 401,
    403, 429, 451).
    """

    def __init__(
        self,
        message: str,
        *,
        recoverable: bool = False,
        blocked_by_remote_site: bool = False,
        remote_site_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.recoverable = recoverable
        self.blocked_by_remote_site = blocked_by_remote_site
        reason = str(remote_site_reason or "").strip()
        if blocked_by_remote_site and not reason:
            reason = _BOT_WALL_REASON
        self.remote_site_reason = reason


_DEFAULT_MAX_CHARS = 20_000
_MAX_MAX_CHARS = 50_000
_MAX_REDIRECTS = 5
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1_000_000
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_JSON_CONTENT_TYPES = {"application/json", "text/json"}
_XML_CONTENT_TYPES = {"application/xml", "text/xml"}
_EXTRA_TEXT_CONTENT_TYPES = {
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-www-form-urlencoded",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
}
_BLOCKED_IPV4_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("100.100.100.200/32"),
)
_TEXT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json;q=0.8,text/plain;q=0.8,*/*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_TAG_BLOCK_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?>.*?</\1>")
_HEAD_BLOCK_RE = re.compile(r"(?is)<head\b.*?</head>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
# A tag ends at the first > outside its quoted attribute values (data attributes often
# hold JSON or markup with > inside).
_TAG_RE = re.compile(r"""(?is)<(?:[^>"']|"[^"]*"|'[^']*')+>""")
_BREAK_TAG_RE = re.compile(r"(?is)<br\s*/?>")
_BLOCK_CLOSE_TAG_RE = re.compile(
    r"(?is)</(p|div|section|article|header|footer|aside|nav|h[1-6]|li|tr|td|th|"
    r"table|blockquote|ul|ol)>"
)
# Page furniture around the content: menus, banners, footers, side panels, search forms.
_LANDMARK_BLOCK_RE = re.compile(r"(?is)<(nav|header|footer|aside|form)\b[^>]*>.*?</\1>")
_MAIN_REGION_RE = re.compile(r"(?is)<(main|article)\b[^>]*>(.*)</\1>")
_MAIN_REGION_MIN_CHARS = 200
_ANCHOR_RE = re.compile(r"""(?is)<a\b((?:[^>"']|"[^"]*"|'[^']*')*)>(.*?)</a>""")
_HREF_RE = re.compile(r"""(?is)\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_LABEL_ATTR_RE = re.compile(r"""(?is)\b(?:aria-label|title)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_MAX_LINKS = 80
_HTML_HINT_RE = re.compile(r"(?is)<!doctype\s+html|<html\b|<body\b|<head\b|<title\b")
_CHARSET_RE = re.compile(r"charset=([^\s;]+)", re.IGNORECASE)
# Cloudflare interstitial wording. Trusted only on pages Cloudflare served:
# ordinary maintenance pages also say "just a moment".
_CLOUDFLARE_CHALLENGE_BODY_RE = re.compile(
    r"(?is)\b(checking your browser|just a moment|enable javascript and cookies)\b"
)
# Vendor-neutral bot-mitigation interstitial markers (Akamai, DataDome,
# PerimeterX/HUMAN, Imperva, Amazon, F5, Vercel, Cloudflare block pages).
_BOT_WALL_BODY_RE = re.compile(
    r"(?is)captcha|/cdn-cgi/challenge-platform|automated access|unusual traffic|"
    r"are you a robot|not a robot|verify (?:that )?you are (?:a )?human|"
    r"disable any ad blocker|access denied|access to this page has been denied|"
    r"you have been blocked|incapsula incident|pardon our interruption|"
    r"errors\.edgesuite\.net|the requested url was rejected|security checkpoint"
)
_BOT_WALL_SAMPLE_CHARS = 8192
# A missing page is this URL's problem, not a refusal — even when the site's
# 404 template happens to mention a captcha or "access denied".
_NOT_FOUND_STATUSES = frozenset({404, 410})

# User-facing trace reasons for failures that are the remote site's doing.
_BOT_WALL_REASON = "site doesn't allow automated access"
_STALLED_REASON = "site didn't respond"
_DROPPED_REASON = "site closed the connection"
_INVALID_RESPONSE_REASON = "site sent an invalid response"
_BAD_CERTIFICATE_REASON = "site's certificate couldn't be verified"
_TLS_FAILED_REASON = "secure connection to the site failed"

# The server answered and refused this client: its policy, not a fetch failure.
# Status -> (model-facing clause, user-facing trace reason).
_REFUSAL_STATUSES: dict[int, tuple[str, str]] = {
    401: ("remote site requires sign-in", "site requires sign-in"),
    403: ("remote site declined automated access", _BOT_WALL_REASON),
    429: ("remote site is rate-limiting requests", "site is rate-limiting requests"),
    451: (
        "remote site is unavailable for legal reasons",
        "site is unavailable for legal reasons",
    ),
    # LinkedIn's non-standard "request denied" status.
    999: ("remote site declined automated access", _BOT_WALL_REASON),
}

# getaddrinfo codes for a host that does not exist (NXDOMAIN / no address
# record), as opposed to a resolver that could not be reached (EAI_AGAIN).
_NONEXISTENT_HOST_ERRNOS = frozenset(
    code
    for code in (getattr(socket, "EAI_NONAME", None), getattr(socket, "EAI_NODATA", None))
    if code is not None
)


ResolverFn = Callable[[str, int], list[str]]


def _normalize_whitespace(text: str) -> str:
    return " ".join(str(text).split())


def _normalize_content_type(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    return text.split(";", 1)[0].strip().lower()


def _extract_charset(raw_content_type: str) -> str | None:
    match = _CHARSET_RE.search(str(raw_content_type or ""))
    if match is None:
        return None
    value = str(match.group(1) or "").strip().strip('"').strip("'")
    return value or None


def _decode_bytes(data: bytes, *, raw_content_type: str, fallback_encoding: str | None) -> str:
    encoding = _extract_charset(raw_content_type) or (fallback_encoding or "").strip() or "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:2048]
    text_like = set(range(32, 127)) | {9, 10, 13}
    non_text = sum(1 for byte in sample if byte not in text_like)
    return (non_text / max(1, len(sample))) > 0.30


def _looks_like_html(text: str) -> bool:
    return _HTML_HINT_RE.search(text) is not None


def _extract_html_title_and_text(html_text: str, *, main_content: bool = False) -> tuple[str, str]:
    title = ""
    title_match = _TITLE_RE.search(html_text)
    if title_match is not None:
        raw_title = _TAG_RE.sub(" ", str(title_match.group(1) or ""))
        title = _normalize_whitespace(html.unescape(raw_title))

    if main_content:
        # The page's own content first: its main/article region when it has a real one,
        # otherwise the whole page without its menus, banners, footers and side panels.
        stripped = _TAG_BLOCK_RE.sub(" ", html_text)
        region = _MAIN_REGION_RE.search(stripped)
        if region is not None:
            _, region_text = _extract_html_title_and_text(region.group(2))
            if len(region_text) >= _MAIN_REGION_MIN_CHARS:
                return title, region_text
        _, page_text = _extract_html_title_and_text(_LANDMARK_BLOCK_RE.sub(" ", stripped))
        if len(page_text) >= _MAIN_REGION_MIN_CHARS:
            return title, page_text

    body = _TAG_BLOCK_RE.sub(" ", html_text)
    body = _HEAD_BLOCK_RE.sub(" ", body)
    body = _BREAK_TAG_RE.sub("\n", body)
    body = _BLOCK_CLOSE_TAG_RE.sub("\n", body)
    body = _TAG_RE.sub(" ", body)
    body = html.unescape(body)

    lines = [_normalize_whitespace(line) for line in body.splitlines()]
    text = "\n".join(line for line in lines if line)
    if not text:
        text = _normalize_whitespace(body)
    return title, text


def _extract_links(
    html_text: str, base_url: str, *, limit: int = _MAX_LINKS
) -> list[dict[str, str]]:
    """The page's links as text and absolute http(s) address, first occurrence, in page order.

    A reader without a browser can only follow a link it can see the address of.
    """
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    body = _TAG_BLOCK_RE.sub(" ", _HEAD_BLOCK_RE.sub(" ", html_text))
    for match in _ANCHOR_RE.finditer(body):
        attributes, inner = match.group(1), match.group(2)
        href = _HREF_RE.search(attributes)
        if href is None:
            continue
        raw = html.unescape(next(group for group in href.groups() if group is not None)).strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            address = urljoin(base_url, raw).split("#", 1)[0]
        except ValueError:
            continue
        if urlsplit(address).scheme.lower() not in ("http", "https") or address in seen:
            continue
        text = _normalize_whitespace(html.unescape(_TAG_RE.sub(" ", inner)))
        if not text:
            label = _LABEL_ATTR_RE.search(attributes)
            text = (
                _normalize_whitespace(html.unescape(next((g for g in label.groups() if g), "")))
                if label
                else ""
            )
        if not text:
            continue
        seen.add(address)
        links.append({"text": text[:120], "url": address})
        if len(links) >= limit:
            break
    return links


_PDF_MAX_PAGES = 60


def _pdf_text(body: bytes, *, truncated: bool) -> str:
    """The text of a PDF's pages, in order, from its first pages."""
    if truncated:
        raise WebFetchError(
            "This PDF is larger than the reading limit, so its text could not be read.",
            recoverable=True,
        )
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # the documents extra is not installed
        raise WebFetchError("Reading PDFs is not available here.", recoverable=True) from e
    try:
        document = pdfium.PdfDocument(body)
    except Exception as e:  # noqa: BLE001 - any parser failure is this file's problem
        raise WebFetchError("This PDF could not be opened.", recoverable=True) from e
    pages: list[str] = []
    try:
        for index in range(min(len(document), _PDF_MAX_PAGES)):
            page = document[index]
            text_page = page.get_textpage()
            try:
                pages.append(text_page.get_text_range())
            finally:
                text_page.close()
                page.close()
    finally:
        document.close()
    return "\n\n".join(text.strip() for text in pages if text.strip())


def _is_json_content_type(content_type: str) -> bool:
    return content_type in _JSON_CONTENT_TYPES or content_type.endswith("+json")


def _is_xml_content_type(content_type: str) -> bool:
    return content_type in _XML_CONTENT_TYPES or content_type.endswith("+xml")


def _is_text_like_content_type(content_type: str) -> bool:
    if not content_type:
        return True
    if content_type.startswith("text/"):
        return True
    if _is_json_content_type(content_type):
        return True
    if _is_xml_content_type(content_type):
        return True
    return content_type in _EXTRA_TEXT_CONTENT_TYPES


def _clip_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _coerce_max_chars(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise WebFetchError(
            f"max_chars must be between 1 and {_MAX_MAX_CHARS}.",
            recoverable=True,
        ) from e
    if value <= 0 or value > _MAX_MAX_CHARS:
        raise WebFetchError(
            f"max_chars must be between 1 and {_MAX_MAX_CHARS}.",
            recoverable=True,
        )
    return value


def _split_url(raw_url: str) -> tuple[str, Any]:
    url = str(raw_url or "").strip()
    if not url:
        raise WebFetchError("url must be a non-empty string.", recoverable=True)
    split = urlsplit(url)
    scheme = split.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WebFetchError(
            f"Unsupported URL scheme: {split.scheme or '(missing)'}",
            recoverable=True,
        )
    if split.hostname is None:
        raise WebFetchError("URL must include a valid host.", recoverable=True)
    if split.username is not None or split.password is not None:
        raise WebFetchError("Embedded URL credentials are not allowed.", recoverable=True)
    try:
        _ = split.port
    except ValueError as e:
        raise WebFetchError("URL has an invalid port value.", recoverable=True) from e
    return url, split


def _blocked_ip_reason(ip: ipaddress._BaseAddress) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_reserved:
        return "reserved"
    if isinstance(ip, ipaddress.IPv4Address):
        if any(ip in network for network in _BLOCKED_IPV4_NETWORKS):
            return "cloud-metadata-or-special-range"
    return None


def _resolve_host_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        if isinstance(e, socket.gaierror) and e.errno in _NONEXISTENT_HOST_ERRNOS:
            # The name does not exist: a bad URL, not a DNS outage, so the model
            # can recover with a different source and web tools stay available.
            raise WebFetchError(
                f"Host '{host}' does not exist ({e}). Check the URL or use a different source.",
                recoverable=True,
            ) from e
        raise WebFetchError(f"Failed to resolve host '{host}': {e}") from e

    addresses: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        if not sockaddr:
            continue
        ip_text = str(sockaddr[0] or "").strip()
        if not ip_text:
            continue
        if ip_text not in seen:
            seen.add(ip_text)
            addresses.append(ip_text)
    if not addresses:
        raise WebFetchError(f"Host '{host}' did not resolve to any IP address.")
    return addresses


def _validate_fetch_target(url: str, *, resolver: ResolverFn) -> None:
    _url, split = _split_url(url)
    host = str(split.hostname or "")
    host_normalized = host.rstrip(".").casefold()
    if host_normalized == "localhost" or host_normalized.endswith(".localhost"):
        raise WebFetchError(f"Blocked URL host '{host}': localhost is not allowed.")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        reason = _blocked_ip_reason(literal_ip)
        if reason is not None:
            raise WebFetchError(f"Blocked URL host '{host}': {reason} address is not allowed.")
        return

    port = split.port
    if port is None:
        port = 443 if split.scheme.lower() == "https" else 80

    resolved_ips = resolver(host, port)
    blocked_entries: list[str] = []
    for ip_text in resolved_ips:
        try:
            parsed = ipaddress.ip_address(ip_text)
        except ValueError as e:
            raise WebFetchError(
                f"Resolver returned a non-IP address for host '{host}': {ip_text!r}"
            ) from e
        reason = _blocked_ip_reason(parsed)
        if reason is not None:
            blocked_entries.append(f"{parsed.compressed} ({reason})")

    if blocked_entries:
        details = ", ".join(blocked_entries)
        raise WebFetchError(
            f"Blocked URL host '{host}': resolved to blocked/local address(es): {details}."
        )


def _read_limited_response_bytes(response: httpx.Response, *, limit: int) -> tuple[bytes, bool]:
    buf = bytearray()
    truncated = False
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        remaining = limit - len(buf)
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            buf.extend(chunk[:remaining])
            truncated = True
            break
        buf.extend(chunk)
    return bytes(buf), truncated


_REMOTE_BLOCK_GUIDANCE = (
    " The block is the site's own policy, not a fetch failure; do not retry "
    "this host — use a different source."
)
_REMOTE_FAILURE_GUIDANCE = (
    " The failure is specific to this site, not a web outage; do not retry "
    "this host — use a different source."
)


def _looks_like_bot_wall(response_headers: dict[str, str], body_sample: str) -> bool:
    if str(response_headers.get("cf-mitigated") or "").strip().casefold() == "challenge":
        return True
    # AWS WAF sets this only when it served a CAPTCHA/challenge instead of the page.
    if str(response_headers.get("x-amzn-waf-action") or "").strip():
        return True
    server = str(response_headers.get("server") or "").strip().casefold()
    if "cloudflare" in server and _CLOUDFLARE_CHALLENGE_BODY_RE.search(body_sample):
        return True
    return _BOT_WALL_BODY_RE.search(body_sample) is not None


def _classify_http_status_error(
    *,
    status_code: int,
    final_url: str,
    response_headers: dict[str, str],
    decoded_body: str,
) -> tuple[str, bool, str]:
    """Return (error message, blocked_by_remote_site, remote_site_reason)."""
    prefix = f"HTTP error {status_code} while fetching '{final_url}'"
    # Bot walls also arrive as 401 (DataDome), 405 (AWS WAF), or 503 (Amazon),
    # so the interstitial is recognized by its markers, not only by 403.
    if status_code not in _NOT_FOUND_STATUSES and _looks_like_bot_wall(
        response_headers, decoded_body[:_BOT_WALL_SAMPLE_CHARS]
    ):
        return (
            f"{prefix}: remote site blocked automated retrieval with anti-bot/challenge "
            "protection." + _REMOTE_BLOCK_GUIDANCE,
            True,
            _BOT_WALL_REASON,
        )
    refusal = _REFUSAL_STATUSES.get(status_code)
    if refusal is not None:
        clause, reason = refusal
        return f"{prefix}: {clause}." + _REMOTE_BLOCK_GUIDANCE, True, reason
    return f"{prefix}.", False, ""


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _remote_transport_failure_reason(error: httpx.HTTPError) -> str:
    """Trace reason when a transport failure is specific to the remote host, else "".

    Qualifies only once the host was reached: a TLS handshake the server
    completed badly (untrusted certificate, protocol failure) or a connection
    the server dropped. Connect-phase failures (refused, unreachable, proxy)
    stay environment-level, since a network outage looks the same.
    """
    chain = list(_exception_chain(error))
    if "CERTIFICATE_VERIFY_FAILED" in str(error) or any(
        isinstance(exc, ssl.SSLCertVerificationError) for exc in chain
    ):
        return _BAD_CERTIFICATE_REASON
    if any(isinstance(exc, ssl.SSLError) for exc in chain):
        return _TLS_FAILED_REASON
    if isinstance(error, httpx.ReadError | httpx.WriteError):
        return _DROPPED_REASON
    if isinstance(error, httpx.RemoteProtocolError):
        detail = str(error).casefold()
        if "disconnect" in detail or "closed" in detail:
            return _DROPPED_REASON
        return _INVALID_RESPONSE_REASON
    return ""


def _with_guidance(message: str, guidance: str) -> str:
    return message.rstrip().rstrip(".") + "." + guidance


def web_fetch(
    *,
    url: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    transport: httpx.BaseTransport | None = None,
    truncate_oversized: bool = False,
    main_content: bool = False,
    include_links: bool = False,
    max_bytes: int | None = None,
    read_pdf: bool = False,
) -> dict[str, Any]:
    """``truncate_oversized`` reads a page longer than the byte limit up to that limit
    (marked truncated) instead of refusing it."""
    # Defense-in-depth runtime guard: even if a web_fetch call reaches this layer
    # through a path that bypassed tool registration (resume/replay, patched tool
    # tables), the master web-tools switch still hard-blocks execution.
    # Lazy import to avoid a module-level import cycle with config.
    from ..config import resolve_web_tools_enabled

    if not resolve_web_tools_enabled(None):
        raise WebFetchError(
            "web_fetch is disabled by the web-tools policy "
            "(ALYSIS_WEB_TOOLS=off / web_tools_enabled=false). "
            "No network fetch was performed.",
            recoverable=False,
        )
    raw_input_url = str(url or "").strip()
    requested_url, _split = _split_url(
        normalize_web_url(raw_input_url)
        or canonicalize_web_url_input(raw_input_url)
        or raw_input_url
    )
    clipped_max_chars = _coerce_max_chars(max_chars)

    resolver: ResolverFn = _resolve_host_addresses
    timeout_budget = build_http_timeout_budget(_DEFAULT_TIMEOUT_SECONDS, profile="fetch")

    current_url = requested_url
    final_url = requested_url
    status_code = 0
    raw_content_type = ""
    body = b""
    body_truncated = False
    response_encoding: str | None = None
    response_headers: dict[str, str] = {}

    _validate_fetch_target(current_url, resolver=resolver)
    try:
        response = asyncio.run(
            safe_http_request(
                "GET",
                current_url,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                max_bytes=max_bytes or _MAX_RESPONSE_BYTES,
                allow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                headers=_TEXT_HEADERS,
                _transport=transport,  # type: ignore[arg-type]
                _resolver=resolver,
                truncate=truncate_oversized,
            )
        )
    except SafeHttpError as e:
        # safe_http classifies URL-/host-specific failures (oversized body,
        # redirect problems, blocked or invalid hosts) as recoverable; propagate
        # that so one bad target does not disable web tools for the whole turn.
        raise WebFetchError(str(e), recoverable=getattr(e, "recoverable", False)) from e
    except httpx.TimeoutException as e:
        timeout_message = format_http_timeout_error(
            operation=f"web_fetch request to '{current_url}'",
            budget=timeout_budget,
            error=e,
        )
        if isinstance(e, httpx.ReadTimeout | httpx.WriteTimeout):
            # The connection was already up (a stalled connect raises
            # ConnectTimeout), so the network works: this host is slow or
            # deliberately stalls automated clients.
            raise WebFetchError(
                _with_guidance(timeout_message, _REMOTE_FAILURE_GUIDANCE),
                recoverable=True,
                remote_site_reason=_STALLED_REASON,
            ) from e
        raise WebFetchError(timeout_message) from e
    except httpx.HTTPError as e:
        failure_message = f"HTTP request failed for '{current_url}': {e}"
        transport_reason = _remote_transport_failure_reason(e)
        if transport_reason:
            raise WebFetchError(
                _with_guidance(failure_message, _REMOTE_FAILURE_GUIDANCE),
                recoverable=True,
                remote_site_reason=transport_reason,
            ) from e
        raise WebFetchError(failure_message) from e

    status_code = int(response.status_code)
    final_url = str(response.url)
    raw_content_type = str(response.headers.get("content-type") or "")
    response_encoding = response.encoding
    response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    body = response.content
    body_truncated = bool(response.extensions.get("alysis_truncated"))

    content_type = _normalize_content_type(raw_content_type)
    decoded = _decode_bytes(
        body, raw_content_type=raw_content_type, fallback_encoding=response_encoding
    )
    # The site responded, so the web itself is reachable: an HTTP error status or
    # unusable payload is specific to this URL and the model can recover by
    # choosing a different source. Only genuine connectivity signals (DNS
    # resolution outage, connect failures and connect timeouts) remain
    # unrecoverable.
    if status_code >= 400:
        status_message, blocked_by_remote_site, remote_site_reason = _classify_http_status_error(
            status_code=status_code,
            final_url=final_url,
            response_headers=response_headers,
            decoded_body=decoded,
        )
        raise WebFetchError(
            status_message,
            recoverable=True,
            blocked_by_remote_site=blocked_by_remote_site,
            remote_site_reason=remote_site_reason,
        )

    is_pdf = content_type == "application/pdf" or body[:5] == b"%PDF-"
    if read_pdf and is_pdf:
        content_type = "application/pdf"
    elif content_type and not _is_text_like_content_type(content_type):
        raise WebFetchError(
            f"Unsupported content type for web_fetch: '{content_type}'. Only text-like responses "
            "are supported.",
            recoverable=True,
        )
    if not content_type and _looks_binary(body):
        raise WebFetchError(
            "Unsupported binary response without a text-like content type.",
            recoverable=True,
        )

    title = ""
    links: list[dict[str, str]] = []
    if read_pdf and is_pdf:
        readable_content = _pdf_text(body, truncated=body_truncated)
        body_truncated = False  # the text is whole; clipping below marks a long one
    elif content_type in _HTML_CONTENT_TYPES or (not content_type and _looks_like_html(decoded)):
        if not content_type:
            content_type = "text/html"
        title, extracted = _extract_html_title_and_text(decoded, main_content=main_content)
        readable_content = extracted
        if include_links:
            links = _extract_links(decoded, final_url or requested_url)
    elif _is_json_content_type(content_type):
        try:
            parsed_json = json.loads(decoded)
        except json.JSONDecodeError:
            readable_content = decoded
        else:
            readable_content = json.dumps(parsed_json, ensure_ascii=False, indent=2)
    else:
        if not content_type:
            content_type = "text/plain"
        readable_content = decoded

    readable_content = readable_content.replace("\r\n", "\n").replace("\r", "\n")
    clipped_content, clipped = _clip_text(readable_content, max_chars=clipped_max_chars)
    truncated = bool(body_truncated or clipped)

    return {
        "url": requested_url,
        "final_url": final_url,
        "status_code": status_code,
        "content_type": content_type,
        "title": title,
        "content": clipped_content,
        "truncated": truncated,
        **({"links": links} if include_links else {}),
        "backend": "httpx",
        **(
            {"raw_input_url": raw_input_url}
            if raw_input_url and raw_input_url != requested_url
            else {}
        ),
    }
