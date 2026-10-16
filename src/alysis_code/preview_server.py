from __future__ import annotations

import argparse
import hashlib
import http.cookies
import ipaddress
import json
import os
import platform
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .command_text import command_text

PREVIEW_ACCESS_SCOPES = frozenset({"local", "lan"})
_TOKEN_QUERY_PARAMETER = "alysis_token"


class WorkspacePreviewHandler(SimpleHTTPRequestHandler):
    """Static handler constrained to one workspace directory."""

    server_version = "AlysisPreview/2"

    def __init__(
        self,
        *args: Any,
        directory: str,
        auth_token: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._preview_root = Path(directory).resolve()
        self._auth_token = auth_token
        digest = hashlib.sha256((auth_token or "local").encode("utf-8")).hexdigest()[:12]
        self._auth_cookie_name = f"alysis_preview_{digest}"
        super().__init__(*args, directory=os.fspath(self._preview_root), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP handler API
        if not self._authorize_request():
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - inherited HTTP handler API
        if not self._authorize_request():
            return
        super().do_HEAD()

    def _authorize_request(self) -> bool:
        if self._auth_token is None:
            return True

        parsed = urlsplit(self.path)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        supplied = next(
            (value for key, value in query_items if key == _TOKEN_QUERY_PARAMETER), None
        )
        if supplied is not None and secrets.compare_digest(supplied, self._auth_token):
            cleaned_query = urlencode(
                [(key, value) for key, value in query_items if key != _TOKEN_QUERY_PARAMETER]
            )
            location = urlunsplit(("", "", parsed.path or "/", cleaned_query, ""))
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header(
                "Set-Cookie",
                f"{self._auth_cookie_name}={self._auth_token}; Path=/; HttpOnly; SameSite=Strict",
            )
            self.end_headers()
            return False

        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            cookies.clear()
        morsel = cookies.get(self._auth_cookie_name)
        if morsel is not None and secrets.compare_digest(morsel.value, self._auth_token):
            return True

        body = b"Authentication is required for this network preview.\n"
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return False

    def send_head(self):  # type: ignore[no-untyped-def]
        translated = Path(self.translate_path(self.path))
        try:
            requested_relative = translated.relative_to(self._preview_root)
        except ValueError:
            self.send_error(403, "Path escapes the preview root")
            return None
        if any(part.startswith(".") for part in requested_relative.parts):
            self.send_error(403, "Hidden files are not available in previews")
            return None

        candidate = translated.resolve()
        try:
            candidate.relative_to(self._preview_root)
        except ValueError:
            self.send_error(403, "Path escapes the preview root")
            return None
        if candidate.is_dir():
            for index_name in ("index.html", "index.htm"):
                index_path = candidate / index_name
                if not index_path.exists():
                    continue
                try:
                    index_path.resolve().relative_to(self._preview_root)
                except ValueError:
                    self.send_error(403, "Index file escapes the preview root")
                    return None
                break
        return super().send_head()

    def list_directory(self, path: str):  # type: ignore[no-untyped-def]
        _ = path
        self.send_error(403, "Directory listing is disabled")
        return None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Access URLs contain a temporary credential. Do not copy request targets
        # into durable logs where the token could outlive the preview process.
        _ = (format, args)


class WorkspacePreviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_preview_server(
    *,
    root: Path,
    access: str,
    port: int = 0,
    ready_file: Path,
    token_file: Path | None = None,
) -> None:
    preview_root = root.expanduser().resolve()
    if not preview_root.is_dir():
        raise ValueError(f"Preview root is not a directory: {preview_root}")
    normalized_access = str(access or "").strip().lower()
    if normalized_access not in PREVIEW_ACCESS_SCOPES:
        raise ValueError(
            f"Preview access must be one of: {', '.join(sorted(PREVIEW_ACCESS_SCOPES))}"
        )
    if isinstance(port, bool) or port < 0 or port > 65535:
        raise ValueError("Preview port must be between 0 and 65535")

    auth_token = _read_auth_token(token_file)
    if normalized_access == "lan" and auth_token is None:
        raise ValueError("LAN previews require a temporary authentication token")
    handler = partial(
        WorkspacePreviewHandler,
        directory=os.fspath(preview_root),
        auth_token=auth_token,
    )
    runtime = _runtime_environment()
    server, family = _create_preview_server(
        access=normalized_access,
        port=port,
        handler=handler,
        runtime=runtime,
    )
    with server:
        actual_port = int(server.server_address[1])
        probe_host = _probe_host_for_family(family, actual_port)
        preview_urls = _preview_urls(
            access=normalized_access,
            family=family,
            port=actual_port,
            probe_host=probe_host,
        )
        _write_ready_file(
            ready_file,
            {
                "schema_version": 1,
                "access": normalized_access,
                "port": actual_port,
                "probe_host": probe_host,
                "preview_urls": preview_urls,
                "runtime": runtime,
                "authentication_required": auth_token is not None,
            },
        )
        print(f"Alysis Code preview ready at {preview_urls[0]}", flush=True)
        server.serve_forever(poll_interval=0.2)


def _create_preview_server(
    *,
    access: str,
    port: int,
    handler: Any,
    runtime: str,
) -> tuple[WorkspacePreviewHTTPServer, int]:
    host: str | None = None if access == "lan" else "localhost"
    flags = socket.AI_PASSIVE if access == "lan" else 0
    candidates = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
        flags=flags,
    )
    if access == "lan" or runtime == "wsl":
        # IPv4 remains the most consistently forwarded family across WSL and
        # consumer LAN routers. This orders system-provided candidates; it does
        # not select or embed a machine-specific address.
        candidates.sort(key=lambda candidate: candidate[0] != socket.AF_INET)
    errors: list[str] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for family, _socktype, _proto, _canonname, sockaddr in candidates:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        normalized_sockaddr = tuple(sockaddr)
        key = (family, normalized_sockaddr)
        if key in seen:
            continue
        seen.add(key)
        server_type = type(
            f"WorkspacePreviewHTTPServer_{family}",
            (WorkspacePreviewHTTPServer,),
            {"address_family": family},
        )
        try:
            return server_type(sockaddr, handler), family
        except OSError as exc:
            errors.append(str(exc))
    detail = "; ".join(dict.fromkeys(errors)) or "no usable TCP address was found"
    raise OSError(f"Unable to bind the {access} preview server: {detail}")


def _probe_host_for_family(family: int, port: int) -> str:
    candidates = socket.getaddrinfo(
        "localhost",
        port,
        family=family,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    if not candidates:
        raise OSError("The operating system did not provide a loopback address")
    return str(candidates[0][4][0])


def _preview_urls(*, access: str, family: int, port: int, probe_host: str) -> list[str]:
    if access == "local":
        hosts = [probe_host, "localhost"]
    else:
        hosts = _discover_lan_hosts(family)
        # A verified local URL remains useful when interface discovery is limited
        # by the platform (and is the correct Windows-facing path under WSL
        # localhost forwarding). Network addresses, when discoverable, stay first.
        hosts.append(probe_host)
    urls = [_http_url(host, port) for host in dict.fromkeys(hosts) if host]
    if not urls:
        raise OSError("Unable to determine a reachable preview URL")
    return urls


def _discover_lan_hosts(family: int) -> list[str]:
    hostname = socket.gethostname().strip()
    hosts: list[str] = []
    if hostname:
        candidates = _bounded_getaddrinfo(
            hostname,
            None,
            family=family,
            type=socket.SOCK_STREAM,
        )
        for candidate in candidates:
            host = str(candidate[4][0]).split("%", 1)[0]
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                continue
            if address.is_loopback or address.is_unspecified or address.is_link_local:
                continue
            hosts.append(address.compressed)
    hosts.extend(_discover_platform_lan_hosts(family))
    return list(dict.fromkeys(hosts))


def _discover_platform_lan_hosts(family: int) -> list[str]:
    system = platform.system().strip().lower()
    candidates: list[str] = []
    if system == "darwin" and family == socket.AF_INET:
        route = shutil.which("route")
        ipconfig = shutil.which("ipconfig")
        if route and ipconfig:
            route_output = _run_discovery_command([route, "-n", "get", "default"])
            match = re.search(r"^\s*interface:\s*(\S+)\s*$", route_output, flags=re.MULTILINE)
            if match:
                address = _run_discovery_command(
                    [ipconfig, "getifaddr", match.group(1)],
                ).strip()
                if address:
                    candidates.append(address)
    elif system == "linux":
        ip = shutil.which("ip")
        if ip:
            raw = _run_discovery_command([ip, "-j", "address", "show", "up"])
            try:
                interfaces = json.loads(raw)
            except json.JSONDecodeError:
                interfaces = []
            if isinstance(interfaces, list):
                expected_family = "inet" if family == socket.AF_INET else "inet6"
                for interface in interfaces:
                    if not isinstance(interface, dict):
                        continue
                    for info in interface.get("addr_info") or []:
                        if not isinstance(info, dict) or info.get("family") != expected_family:
                            continue
                        if info.get("scope") != "global":
                            continue
                        candidates.append(str(info.get("local") or ""))
        if not candidates:
            hostname = shutil.which("hostname")
            if hostname:
                candidates.extend(_run_discovery_command([hostname, "-I"]).split())

    discovered: list[str] = []
    expected_version = 4 if family == socket.AF_INET else 6
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            continue
        if address.version != expected_version:
            continue
        if address.is_loopback or address.is_unspecified or address.is_link_local:
            continue
        discovered.append(address.compressed)
    return list(dict.fromkeys(discovered))


def _run_discovery_command(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            **command_text(),
            timeout=0.75,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def _bounded_getaddrinfo(
    host: str,
    port: int | None,
    *,
    family: int,
    type: int,
    timeout_s: float = 0.5,
) -> list[tuple[Any, ...]]:
    results: queue.SimpleQueue[list[tuple[Any, ...]]] = queue.SimpleQueue()

    def _resolve() -> None:
        try:
            resolved = socket.getaddrinfo(host, port, family=family, type=type)
        except OSError:
            resolved = []
        results.put(resolved)

    worker = threading.Thread(target=_resolve, name="preview-address-discovery", daemon=True)
    worker.start()
    worker.join(max(0.01, timeout_s))
    if worker.is_alive():
        return []
    try:
        return results.get_nowait()
    except queue.Empty:
        return []


def _http_url(host: str, port: int) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    return f"http://{formatted_host}:{port}"


def _read_auth_token(path: Path | None) -> str | None:
    if path is None:
        return None
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("Preview authentication token is invalid")
    return token


def _write_ready_file(path: Path, payload: dict[str, Any]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(target)


def _runtime_environment() -> str:
    system = platform.system().strip().lower()
    if system == "linux":
        release = platform.release().lower()
        proc_version = ""
        try:
            proc_version = Path("/proc/version").read_text(encoding="utf-8").lower()
        except OSError:
            pass
        if "microsoft" in release or "microsoft" in proc_version or os.environ.get("WSL_INTEROP"):
            return "wsl"
    return system or "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a workspace directory for local preview.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--access", choices=sorted(PREVIEW_ACCESS_SCOPES), required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    run_preview_server(
        root=args.root,
        access=args.access,
        port=args.port,
        ready_file=args.ready_file,
        token_file=args.token_file,
    )


if __name__ == "__main__":
    main()
