from __future__ import annotations

import faulthandler
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from alysis_code.error_text import redact_sensitive_error_text
from alysis_code.mcp.oauth import build_pkce_challenge
from alysis_code.mcp.transport_stdio import live_stdio_transport_diagnostics


@pytest.fixture
def configured_hosted_api(monkeypatch: pytest.MonkeyPatch) -> str:
    """Explicit deployment configuration for offline hosted-service tests."""
    api_url = "https://hosted.example.invalid/api"
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", api_url)
    for name in ("ALYSIS_GATEWAY_URL", "ALYSIS_SUPABASE_URL", "ALYSIS_CLOUD_PUBLIC_KEY"):
        monkeypatch.delenv(name, raising=False)
    return api_url


_FORGE_EXECUTION_TEST_FILES = {
    "test_forge_exec.py",
    "test_forge_swarm.py",
    "test_forge_review.py",
}
_DEFAULT_FORGE_TEST_TIMEOUT_S = 90.0
_MCP_STDIO_THREAD_PREFIXES = ("mcp-stdout-", "mcp-stderr-")
_MCP_STDIO_THREAD_CLEANUP_TIMEOUT_S = 2.0
_FORGE_WATCHDOG_EXIT_CODE = 124
_FORGE_WATCHDOG_LOCK_SCAN_LIMIT = 25
_FORGE_WATCHDOG_WORKTREE_SCAN_LIMIT = 12
_FORGE_WATCHDOG_CHILD_PROCESS_SCAN_LIMIT = 25
_FORGE_WATCHDOG_GIT_PROBE_TIMEOUT_S = 0.2


@pytest.fixture
def require_symlinks(tmp_path: Path) -> None:
    """Run link-boundary checks wherever the host permits creating real symlinks."""
    target = tmp_path / "symlink-probe-target"
    target.write_text("probe", encoding="utf-8")
    try:
        (tmp_path / "symlink-probe").symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires Developer Mode or elevated privilege")
        raise


@pytest.fixture(scope="session", autouse=True)
def isolate_terminal_ownership_records(tmp_path_factory: pytest.TempPathFactory):
    """Never let background-process crash markers from tests reach the real user profile."""
    key = "ALYSIS_TERMINAL_OWNERSHIP_DIR"
    previous = os.environ.get(key)
    os.environ[key] = os.fspath(tmp_path_factory.mktemp("terminal-ownership"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


class OAuthFixtureServer:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _OAuthFixtureHandler)
        self._server.oauth_fixture = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.reset()

    def reset(self) -> None:
        self.authorization_server_path = ""
        self.protected_resource_path = "/protected"
        self.challenge_includes_resource_metadata = True
        self.serve_rfc8414 = True
        self.serve_path_protected_resource_metadata = True
        self.serve_root_protected_resource_metadata = True
        self.serve_oidc_inserted_metadata = True
        self.serve_oidc_appended_metadata = True
        self.protected_resource_payload_override: dict[str, Any] | None = None
        self.path_protected_resource_payload_override: dict[str, Any] | None = None
        self.authorization_metadata_override: dict[str, Any] | None = None
        self.oidc_metadata_override: dict[str, Any] | None = None
        self.request_log: list[str] = []
        self.authorize_requests: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.expected_code_challenge: str | None = None
        self.valid_tokens = {"test_access", "refreshed_access"}
        self.issue_refresh_token = True
        self.rotate_refresh_token = True
        self.authorization_code_response_status = HTTPStatus.OK
        self.authorization_code_response_override: dict[str, Any] | None = None
        self.refresh_response_status = HTTPStatus.OK
        self.refresh_response_override: dict[str, Any] | None = None
        self.expected_authorize_resource: str | None = None
        self.expected_token_resource: str | None = None

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def protected_url(self) -> str:
        return f"{self.base_url}{self.protected_resource_path}"

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.base_url}/.well-known/oauth-protected-resource"

    @property
    def path_resource_metadata_path(self) -> str:
        return f"/.well-known/oauth-protected-resource{self.protected_resource_path}"

    @property
    def authorization_server_url(self) -> str:
        return f"{self.base_url}{self.authorization_server_path}"

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.base_url}/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url}/token"

    @property
    def rfc8414_metadata_path(self) -> str:
        if self.authorization_server_path:
            return f"/.well-known/oauth-authorization-server{self.authorization_server_path}"
        return "/.well-known/oauth-authorization-server"

    @property
    def oidc_metadata_path(self) -> str:
        return "/.well-known/openid-configuration"

    @property
    def oidc_inserted_metadata_path(self) -> str:
        return f"/.well-known/openid-configuration{self.authorization_server_path}"

    @property
    def oidc_appended_metadata_path(self) -> str:
        return f"{self.authorization_server_path}/.well-known/openid-configuration"

    def protected_resource_payload(self) -> dict[str, Any]:
        if self.protected_resource_payload_override is not None:
            return dict(self.protected_resource_payload_override)
        return {
            "resource": self.protected_url,
            "authorization_servers": [self.authorization_server_url],
        }

    def path_protected_resource_payload(self) -> dict[str, Any]:
        if self.path_protected_resource_payload_override is not None:
            return dict(self.path_protected_resource_payload_override)
        return self.protected_resource_payload()

    def authorization_metadata_payload(self) -> dict[str, Any]:
        if self.authorization_metadata_override is not None:
            return dict(self.authorization_metadata_override)
        return {
            "issuer": self.authorization_server_url,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "code_challenge_methods_supported": ["S256"],
            "response_types_supported": ["code"],
        }

    def oidc_metadata_payload(self) -> dict[str, Any]:
        if self.oidc_metadata_override is not None:
            return dict(self.oidc_metadata_override)
        return {
            "issuer": self.authorization_server_url,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "code_challenge_methods_supported": ["S256"],
            "response_types_supported": ["code"],
        }


def _forge_test_timeout_seconds() -> float:
    raw_value = os.environ.get("ALYSIS_FORGE_TEST_TIMEOUT_S")
    if raw_value is None:
        return _DEFAULT_FORGE_TEST_TIMEOUT_S
    try:
        return float(raw_value)
    except ValueError:
        return _DEFAULT_FORGE_TEST_TIMEOUT_S


def _forge_test_timeout_file():
    stream = sys.__stderr__
    try:
        stream.fileno()  # type: ignore[union-attr]
    except (AttributeError, OSError, io.UnsupportedOperation):
        return tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    return stream


def _live_mcp_stdio_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(_MCP_STDIO_THREAD_PREFIXES)
    ]


def _mcp_thread_diagnostics(threads: list[threading.Thread]) -> str:
    return ", ".join(
        f"{thread.name}(ident={thread.ident}, daemon={thread.daemon}, alive={thread.is_alive()})"
        for thread in threads
    )


def _active_thread_diagnostics() -> list[dict[str, Any]]:
    return [
        {
            "name": thread.name,
            "ident": thread.ident,
            "daemon": thread.daemon,
            "alive": thread.is_alive(),
        }
        for thread in threading.enumerate()
    ]


def _redact_watchdog_text(value: object) -> str:
    return redact_sensitive_error_text(str(value or ""))


def _forge_tmp_path(request: pytest.FixtureRequest) -> Path | None:
    value = getattr(request.node, "funcargs", {}).get("tmp_path")
    if isinstance(value, Path):
        return value
    return None


def _forge_run_lock_diagnostics(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    root = _forge_tmp_path(request)
    if root is None or not root.exists():
        return []
    locks: list[dict[str, Any]] = []
    for path in root.rglob("active_execution.lock.json"):
        entry: dict[str, Any] = {"path": os.fspath(path)}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            entry["read_error"] = str(exc)
        else:
            if isinstance(payload, dict):
                entry["run_id"] = payload.get("run_id")
                entry["mode"] = payload.get("mode")
                entry["kind"] = payload.get("kind")
                entry["pid"] = payload.get("pid")
                if payload.get("owner_token"):
                    entry["owner_token"] = "[redacted]"
        locks.append(entry)
        if len(locks) >= _FORGE_WATCHDOG_LOCK_SCAN_LIMIT:
            break
    return locks


def _git_worktree_probe(repo: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": os.fspath(repo),
        "exists": repo.exists(),
        "git_marker": (repo / ".git").exists(),
    }
    if not repo.exists():
        return entry
    for key, args in (
        ("branch", ["rev-parse", "--abbrev-ref", "HEAD"]),
        ("head", ["rev-parse", "--short", "HEAD"]),
        ("status", ["status", "--porcelain"]),
    ):
        try:
            completed = subprocess.run(
                ["git", "-C", os.fspath(repo), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=_FORGE_WATCHDOG_GIT_PROBE_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            entry[f"{key}_error"] = str(exc)
            continue
        if completed.returncode == 0:
            entry[key] = _redact_watchdog_text(completed.stdout.strip())
        else:
            entry[f"{key}_error"] = _redact_watchdog_text(
                (completed.stderr or completed.stdout).strip()
            )
    return entry


def _forge_worktree_diagnostics(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    root = _forge_tmp_path(request)
    if root is None or not root.exists():
        return []
    diagnostics: list[dict[str, Any]] = []
    for container_name in ("worktrees", "conflict_worktrees"):
        for container in root.rglob(container_name):
            if not container.is_dir():
                continue
            for task_dir in sorted(path for path in container.iterdir() if path.is_dir()):
                repo = task_dir / "repo"
                entry = _git_worktree_probe(repo)
                entry["kind"] = container_name
                entry["task_id"] = task_dir.name
                marker = task_dir / "failed_cleanup.json"
                if marker.exists():
                    entry["failed_cleanup_marker"] = os.fspath(marker)
                diagnostics.append(entry)
                if len(diagnostics) >= _FORGE_WATCHDOG_WORKTREE_SCAN_LIMIT:
                    return diagnostics
    return diagnostics


def _child_process_diagnostics() -> list[dict[str, Any]]:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    current_pid = os.getpid()
    children: set[int] = set()
    task_root = proc_root / str(current_pid) / "task"
    try:
        task_dirs = [path for path in task_root.iterdir() if path.is_dir()]
    except OSError:
        task_dirs = []
    for task_dir in task_dirs:
        children_file = task_dir / "children"
        try:
            raw_children = children_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        for raw_pid in raw_children.split():
            try:
                children.add(int(raw_pid))
            except ValueError:
                continue
            if len(children) >= _FORGE_WATCHDOG_CHILD_PROCESS_SCAN_LIMIT:
                break
        if len(children) >= _FORGE_WATCHDOG_CHILD_PROCESS_SCAN_LIMIT:
            break

    diagnostics: list[dict[str, Any]] = []
    for child_pid in sorted(children):
        entry: dict[str, Any] = {"pid": child_pid}
        status_path = proc_root / str(child_pid) / "status"
        try:
            for line in status_path.read_text(encoding="utf-8").splitlines():
                if line.startswith(("Name:", "State:", "PPid:")):
                    key, _, value = line.partition(":")
                    entry[key.lower()] = value.strip()
        except OSError as exc:
            entry["status_error"] = str(exc)
        try:
            raw_cmdline = (proc_root / str(child_pid) / "cmdline").read_bytes()
        except OSError as exc:
            entry["cmdline_error"] = str(exc)
        else:
            entry["cmdline"] = " ".join(
                part.decode("utf-8", errors="replace") for part in raw_cmdline.split(b"\0") if part
            )
            entry["cmdline"] = _redact_watchdog_text(entry["cmdline"])
        diagnostics.append(entry)
    return diagnostics


def _write_forge_watchdog_diagnostics(
    *,
    request: pytest.FixtureRequest,
    timeout_s: float,
) -> None:
    stream = sys.__stderr__
    payload = {
        "nodeid": request.node.nodeid,
        "timeout_s": timeout_s,
        "active_threads": _active_thread_diagnostics(),
        "child_processes": _child_process_diagnostics(),
        "live_mcp_stdio_transports": live_stdio_transport_diagnostics(),
        "run_locks": _forge_run_lock_diagnostics(request),
        "worktrees": _forge_worktree_diagnostics(request),
    }
    stream.write("\n=== Alysis Code Forge test watchdog timeout ===\n")
    stream.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    stream.write("=== Python stack traces ===\n")
    stream.flush()
    faulthandler.dump_traceback(file=stream, all_threads=True)
    stream.flush()


def _start_forge_test_watchdog(
    *,
    request: pytest.FixtureRequest,
    timeout_s: float,
) -> threading.Event:
    done = threading.Event()

    def watchdog() -> None:
        if done.wait(timeout_s):
            return
        _write_forge_watchdog_diagnostics(request=request, timeout_s=timeout_s)
        os._exit(_FORGE_WATCHDOG_EXIT_CODE)

    thread = threading.Thread(
        target=watchdog,
        name=f"forge-test-watchdog:{request.node.name}",
        daemon=True,
    )
    thread.start()
    return done


@pytest.fixture(autouse=True)
def block_live_ddgs_network(monkeypatch: pytest.MonkeyPatch):
    """Structural guard: the keyless ddgs web-search backend is ready by default,
    so any test that reaches it without stubbing would hit the live DuckDuckGo
    engines. Fail loudly instead; tests stub ddgs_search or pass text_search_fn."""

    def _blocked(query: str, *, max_results: int, timeout_s: float):
        raise RuntimeError(
            "live ddgs network call attempted in tests: stub "
            "alysis_code.tools.web_search.ddgs_search, pass text_search_fn, "
            "or set ALYSIS_WEB_SEARCH_KEYLESS=0"
        )

    monkeypatch.setattr(
        "alysis_code.tools.web_search_ddgs._default_text_search",
        _blocked,
    )
    yield


@pytest.fixture(autouse=True)
def reset_keyring_observations():
    """The credential store warns once per distinct keyring degradation.

    That state is process-global, so without a reset the first test to observe a
    fallback would silence the assertions of every later one.
    """
    from alysis_code.mcp.token_store import reset_keyring_observations as _reset

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def forge_execution_test_timeout(request: pytest.FixtureRequest):
    path = getattr(request, "path", None)
    if path is None or path.name not in _FORGE_EXECUTION_TEST_FILES:
        yield
        return
    timeout_s = _forge_test_timeout_seconds()
    if timeout_s <= 0:
        yield
        return
    timeout_file = _forge_test_timeout_file()
    watchdog_done = _start_forge_test_watchdog(request=request, timeout_s=timeout_s)
    faulthandler.dump_traceback_later(timeout_s + 5, repeat=False, file=timeout_file, exit=True)
    try:
        yield
    finally:
        watchdog_done.set()
        faulthandler.cancel_dump_traceback_later()
        if timeout_file is not sys.__stderr__:
            timeout_file.close()


@pytest.fixture(autouse=True)
def forge_mcp_stdio_thread_cleanup(request: pytest.FixtureRequest):
    path = getattr(request, "path", None)
    if path is None or path.name not in _FORGE_EXECUTION_TEST_FILES:
        yield
        return
    before = {id(thread) for thread in _live_mcp_stdio_threads()}
    before_transports = {int(item["transport_id"]) for item in live_stdio_transport_diagnostics()}
    yield
    deadline = time.monotonic() + _MCP_STDIO_THREAD_CLEANUP_TIMEOUT_S
    leaked: list[threading.Thread] = []
    leaked_transports: list[dict[str, Any]] = []
    while True:
        leaked = [thread for thread in _live_mcp_stdio_threads() if id(thread) not in before]
        leaked_transports = [
            item
            for item in live_stdio_transport_diagnostics()
            if int(item["transport_id"]) not in before_transports
        ]
        if (not leaked and not leaked_transports) or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    assert not leaked, "Forge test leaked MCP stdio reader thread(s): " + _mcp_thread_diagnostics(
        leaked
    )
    assert not leaked_transports, (
        "Forge test leaked MCP stdio transport/process state: "
        + json.dumps(leaked_transports, sort_keys=True)
    )


class _OAuthFixtureHandler(BaseHTTPRequestHandler):
    server_version = "OAuthFixture/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    @property
    def fixture(self) -> OAuthFixtureServer:
        return self.server.oauth_fixture  # type: ignore[attr-defined]

    def _json(
        self,
        payload: dict[str, Any],
        *,
        status: int = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        self.fixture.request_log.append(parsed.path)
        if (
            self.fixture.protected_resource_path != "/"
            and parsed.path == self.fixture.path_resource_metadata_path
        ):
            if not self.fixture.serve_path_protected_resource_metadata:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(self.fixture.path_protected_resource_payload())
            return
        if parsed.path == "/.well-known/oauth-protected-resource":
            if not self.fixture.serve_root_protected_resource_metadata:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(self.fixture.protected_resource_payload())
            return
        if parsed.path == self.fixture.rfc8414_metadata_path:
            if not self.fixture.serve_rfc8414:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(self.fixture.authorization_metadata_payload())
            return
        if (
            self.fixture.authorization_server_path
            and self.fixture.authorization_server_path != "/"
            and parsed.path == self.fixture.oidc_inserted_metadata_path
        ):
            if not self.fixture.serve_oidc_inserted_metadata:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(self.fixture.oidc_metadata_payload())
            return
        if (
            self.fixture.authorization_server_path
            and self.fixture.authorization_server_path != "/"
            and parsed.path == self.fixture.oidc_appended_metadata_path
        ):
            if not self.fixture.serve_oidc_appended_metadata:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(self.fixture.oidc_metadata_payload())
            return
        if parsed.path == self.fixture.oidc_metadata_path:
            self._json(self.fixture.oidc_metadata_payload())
            return
        if parsed.path == "/authorize":
            query = parse_qs(parsed.query)
            normalized_query = {key: values[0] for key, values in query.items() if values}
            self.fixture.authorize_requests.append(normalized_query)
            if query.get("code_challenge_method", [""])[0] != "S256":
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if query.get("response_type", [""])[0] != "code":
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            redirect_uri = query.get("redirect_uri", [""])[0]
            if not redirect_uri:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if self.fixture.expected_authorize_resource is not None:
                if query.get("resource", [""])[0] != self.fixture.expected_authorize_resource:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
            self.fixture.expected_code_challenge = query.get("code_challenge", [None])[0]
            state = query.get("state", [""])[0]
            separator = "&" if "?" in redirect_uri else "?"
            self._redirect(
                f"{redirect_uri}{separator}{urlencode({'code': 'TEST_CODE', 'state': state})}"
            )
            return
        if parsed.path == self.fixture.protected_resource_path:
            auth_header = self.headers.get("Authorization")
            if auth_header not in {f"Bearer {token}" for token in self.fixture.valid_tokens}:
                headers: dict[str, str] = {}
                if self.fixture.challenge_includes_resource_metadata:
                    headers["WWW-Authenticate"] = (
                        f'Bearer resource_metadata="{self.fixture.resource_metadata_url}"'
                    )
                else:
                    headers["WWW-Authenticate"] = "Bearer"
                self._json(
                    {"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED, headers=headers
                )
                return
            self._json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        self.fixture.request_log.append(parsed.path)
        if parsed.path != "/token":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        payload = parse_qs(self.rfile.read(length).decode("utf-8"))
        normalized_payload = {key: values[0] for key, values in payload.items() if values}
        self.fixture.token_requests.append(normalized_payload)
        grant_type = payload.get("grant_type", [""])[0]
        if self.fixture.expected_token_resource is not None:
            if payload.get("resource", [""])[0] != self.fixture.expected_token_resource:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
        if grant_type == "authorization_code":
            verifier = payload.get("code_verifier", [""])[0]
            if payload.get("code", [""])[0] != "TEST_CODE":
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if self.fixture.expected_code_challenge is None:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if build_pkce_challenge(verifier) != self.fixture.expected_code_challenge:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            response = (
                dict(self.fixture.authorization_code_response_override)
                if self.fixture.authorization_code_response_override is not None
                else {
                    "access_token": "test_access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            )
            if (
                self.fixture.authorization_code_response_override is None
                and self.fixture.issue_refresh_token
            ):
                response["refresh_token"] = "test_refresh"
            self._json(response, status=int(self.fixture.authorization_code_response_status))
            return
        if grant_type == "refresh_token":
            response = (
                dict(self.fixture.refresh_response_override)
                if self.fixture.refresh_response_override is not None
                else {
                    "access_token": "refreshed_access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            )
            if self.fixture.refresh_response_override is None and self.fixture.rotate_refresh_token:
                response["refresh_token"] = "rotated_refresh"
            self._json(response, status=int(self.fixture.refresh_response_status))
            return
        self.send_error(HTTPStatus.BAD_REQUEST)


@pytest.fixture
def oauth_fixture_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = OAuthFixtureServer()

    async def fixture_safe_http_request(
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
    ) -> httpx.Response:
        del max_redirects
        async with httpx.AsyncClient(
            follow_redirects=allow_redirects,
            timeout=httpx.Timeout(timeout),
        ) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                json=json,
                content=content,
            )
        if len(response.content) > max_bytes:
            from alysis_code.safety import SafeHttpError

            raise SafeHttpError(f"Response body exceeded max_bytes={max_bytes}.")
        return response

    monkeypatch.setattr(
        "alysis_code.mcp.oauth.safe_http_request",
        fixture_safe_http_request,
    )
    try:
        yield server
    finally:
        server.close()
