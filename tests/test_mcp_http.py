from __future__ import annotations

import gzip
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.mcp.client import (
    MCP_HTTP_PREFERRED_PROTOCOL_VERSION,
    McpClientError,
    McpHttpClient,
)
from alysis_code.mcp.config import load_resolved_mcp_config, user_mcp_config_path
from alysis_code.mcp.manager import McpManager
from alysis_code.mcp.oauth import (
    McpOAuthAuthRequiredError,
    McpOAuthInsufficientScopeError,
    McpOAuthReLoginRequired,
)
from alysis_code.mcp.oauth_store import (
    McpOAuthTokenRecord,
    load_oauth_token_record,
    save_oauth_token_record,
)
from alysis_code.mcp.transport_http import (
    McpHttpTransportAuthRequiredError,
    McpHttpTransportProtocolError,
    McpHttpTransportRemoteError,
    McpHttpTransportTimeoutError,
)
from alysis_code.runtime_kind import RuntimeKind


@dataclass(frozen=True)
class _RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


@dataclass(frozen=True)
class _ResponseSpec:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    body_chunks: tuple[tuple[bytes, float], ...] = ()
    send_content_length: bool = True
    linger_after_body_s: float = 0.0


class _ThreadedMcpHttpServer:
    def __init__(
        self,
        *,
        handler: Callable[[_ThreadedMcpHttpServer, _RecordedRequest], _ResponseSpec],
    ) -> None:
        self._handler = handler
        self.requests: list[_RecordedRequest] = []
        self.handler_errors: list[BaseException] = []

        server = self

        class _RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                self._serve()

            def do_DELETE(self) -> None:
                self._serve()

            def _serve(self) -> None:
                content_length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(content_length) if content_length > 0 else b""
                request = _RecordedRequest(
                    method=self.command,
                    path=self.path,
                    headers={str(key).lower(): str(value) for key, value in self.headers.items()},
                    body=body,
                )
                server.requests.append(request)
                try:
                    response = server._handler(server, request)
                except BaseException as exc:  # noqa: BLE001
                    server.handler_errors.append(exc)
                    payload = str(exc).encode("utf-8", errors="replace")
                    response = _ResponseSpec(
                        status=500,
                        headers={"Content-Type": "text/plain; charset=utf-8"},
                        body=payload,
                    )
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                content_length = (
                    sum(len(chunk) for chunk, _delay_after_s in response.body_chunks)
                    if response.body_chunks
                    else len(response.body)
                )
                if response.send_content_length:
                    self.send_header("Content-Length", str(content_length))
                else:
                    self.close_connection = True
                self.end_headers()
                if response.body_chunks:
                    for chunk, delay_after_s in response.body_chunks:
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        if delay_after_s > 0:
                            time.sleep(delay_after_s)
                    if response.linger_after_body_s > 0:
                        time.sleep(response.linger_after_body_s)
                else:
                    if response.body:
                        self.wfile.write(response.body)
                        self.wfile.flush()
                    if response.linger_after_body_s > 0:
                        time.sleep(response.linger_after_body_s)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RequestHandler)
        # Poll for shutdown promptly without changing request or timeout behavior.
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/mcp"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)
        if self.handler_errors:
            raise self.handler_errors[0]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _basic_cfg() -> AppConfig:
    return AppConfig(model="test-model", base_url="https://example.com/v1")


def _json_response(
    payload: dict[str, Any],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> _ResponseSpec:
    response_headers = {"Content-Type": "application/json"}
    if headers:
        response_headers.update(headers)
    return _ResponseSpec(
        status=status,
        headers=response_headers,
        body=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
    )


def _empty_response(
    *,
    status: int = 202,
    headers: dict[str, str] | None = None,
) -> _ResponseSpec:
    return _ResponseSpec(status=status, headers=dict(headers or {}), body=b"")


def _sse_event(
    *,
    data: dict[str, Any] | str | None = None,
    event: str | None = None,
    event_id: str | None = None,
    retry_ms: int | None = None,
    comment: str | None = None,
) -> str:
    lines: list[str] = []
    if comment is not None:
        lines.append(f": {comment}")
    if event is not None:
        lines.append(f"event: {event}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    if data is not None:
        if isinstance(data, str):
            data_text = data
        else:
            data_text = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        for line in data_text.splitlines() or [""]:
            lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _sse_response(
    events: list[str],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> _ResponseSpec:
    response_headers = {"Content-Type": "text/event-stream"}
    if headers:
        response_headers.update(headers)
    return _ResponseSpec(
        status=status,
        headers=response_headers,
        body="".join(events).encode("utf-8"),
    )


def _streamed_sse_response(
    events: list[tuple[str, float]],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    linger_after_body_s: float = 0.0,
) -> _ResponseSpec:
    response_headers = {"Content-Type": "text/event-stream"}
    if headers:
        response_headers.update(headers)
    return _ResponseSpec(
        status=status,
        headers=response_headers,
        body_chunks=tuple(
            (event_text.encode("utf-8"), delay_after_s) for event_text, delay_after_s in events
        ),
        send_content_length=False,
        linger_after_body_s=linger_after_body_s,
    )


def _fragment_sse_event_after_first_line(
    event_text: str,
    *,
    delay_before_terminator_s: float,
) -> tuple[tuple[str, float], tuple[str, float]]:
    first_line, separator, remainder = event_text.partition("\n")
    assert separator, "expected SSE event text to include at least one line terminator"
    return ((first_line + separator, delay_before_terminator_s), (remainder, 0.0))


def _fragment_crlf_sse_event_after_first_line_break(
    event_text: str,
    *,
    delay_before_remainder_s: float,
) -> tuple[tuple[str, float], tuple[str, float]]:
    crlf_text = event_text.replace("\n", "\r\n")
    first_break = crlf_text.find("\r\n")
    assert first_break >= 0, "expected SSE event text to include a line terminator"
    split_index = first_break + 1
    return (
        (crlf_text[:split_index], delay_before_remainder_s),
        (crlf_text[split_index:], 0.0),
    )


def _initialize_result(*, capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    if capabilities is None:
        capabilities = {"tools": {}}
    return {
        "protocolVersion": MCP_HTTP_PREFERRED_PROTOCOL_VERSION,
        "capabilities": capabilities,
        "serverInfo": {"name": "fixture-http", "version": "0.0.1"},
    }


def _tools_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": "alpha-tool",
            "description": "Alpha tool",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }
    ]


def _tool_call_result() -> dict[str, Any]:
    return {
        "isError": False,
        "structuredContent": {"ok": True},
        "content": [{"type": "text", "text": "ok"}],
    }


def _resources_payload() -> list[dict[str, Any]]:
    return [
        {
            "uri": "file:///alpha.txt",
            "name": "alpha",
            "description": "Alpha text resource",
            "mimeType": "text/plain",
            "size": 11,
        },
        {
            "uri": "https://example.com/spec.json",
            "name": "spec",
            "description": "JSON spec",
            "mimeType": "application/json",
            "size": 24,
        },
    ]


def _prompts_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": "review_pr",
            "title": "Review Pull Request",
            "description": "Review helper",
            "arguments": [{"name": "repo", "required": True}],
        },
        {
            "name": "draft_issue",
            "description": "Draft issue helper",
        },
    ]


def _resource_read_result(
    *,
    uri: str,
    mime_type: str = "text/plain",
    text: str = "hello from resource",
) -> dict[str, Any]:
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": mime_type,
                "text": text,
            }
        ]
    }


def _prompt_get_result(
    *,
    name: str,
    description: str | None = None,
    text: str = "prompt body",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": text,
                },
            }
        ],
    }
    if description is not None:
        payload["description"] = description
    return payload


def _jsonrpc_response(request: _RecordedRequest, result: dict[str, Any]) -> dict[str, Any]:
    payload = request.json()
    return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


def _jsonrpc_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _jsonrpc_request(method: str, *, request_id: str = "server-request-1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {},
    }


def _resolved_http_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    server_overrides: dict[str, object] | None = None,
):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    server_payload: dict[str, object] = {
        "transport": "http",
        "url": url,
    }
    if headers:
        server_payload["headers"] = headers
    if server_overrides:
        server_payload.update(server_overrides)
    _write_json(user_mcp_config_path(), {"servers": {"alpha": server_payload}})
    resolved = load_resolved_mcp_config(workspace_root=tmp_path)
    assert len(resolved.servers) == 1
    return resolved.servers[0]


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _ThreadedMcpHttpServer,
    *,
    headers: dict[str, str] | None = None,
    server_overrides: dict[str, object] | None = None,
    runtime_kind: RuntimeKind | str | None = None,
) -> McpHttpClient:
    resolved = _resolved_http_server(
        tmp_path,
        monkeypatch,
        server.base_url,
        headers=headers,
        server_overrides=server_overrides,
    )
    return McpHttpClient(server=resolved, workspace_root=tmp_path, runtime_kind=runtime_kind)


def _oauth_record(
    *,
    access_token: str,
    refresh_token: str | None,
    expires_delta_s: int = 3600,
    scopes: tuple[str, ...] = ("openid",),
) -> McpOAuthTokenRecord:
    obtained_at = datetime.now(UTC).replace(microsecond=0)
    return McpOAuthTokenRecord(
        access_token=access_token,
        token_type="Bearer",
        expires_at=obtained_at + timedelta(seconds=expires_delta_s),
        refresh_token=refresh_token,
        granted_scopes=scopes,
        obtained_at=obtained_at,
    )


@pytest.mark.parametrize("use_manager", [False, True])
def test_http_initialized_notification_waits_for_slow_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_manager: bool
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(_jsonrpc_response(request, _initialize_result()))
        if method == "notifications/initialized":
            time.sleep(0.8)
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    resolved_server = _resolved_http_server(
        tmp_path,
        monkeypatch,
        server.base_url,
        server_overrides={"startup_timeout_s": 2.0, "call_timeout_s": 0.2},
    )
    runtime: McpManager | McpHttpClient
    if use_manager:
        runtime = McpManager(
            resolved_config=load_resolved_mcp_config(workspace_root=tmp_path),
            workspace_root=tmp_path,
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        )
    else:
        runtime = McpHttpClient(server=resolved_server, workspace_root=tmp_path)
    try:
        if isinstance(runtime, McpManager):
            assert len(runtime.tool_bindings) == 1
        else:
            assert [tool.name for tool in runtime.list_tools()] == ["alpha-tool"]
        assert [request.json()["method"] for request in server.requests] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
    finally:
        runtime.close()
        server.close()


def test_http_initialized_notification_respects_short_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(_jsonrpc_response(request, _initialize_result()))
        if method == "notifications/initialized":
            time.sleep(0.8)
            return _empty_response(headers={"Connection": "close"})
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"startup_timeout_s": 0.2, "call_timeout_s": 2.0},
    )
    try:
        with pytest.raises(McpHttpTransportTimeoutError) as exc_info:
            client.ensure_initialized()
        assert "'notifications/initialized' timed out after 0.200s" in str(exc_info.value)
        assert client.transport.closed
        assert [request.json()["method"] for request in server.requests] == [
            "initialize",
            "notifications/initialized",
        ]
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize(
    ("roots_mode", "runtime_kind", "expected_capabilities"),
    [
        ("workspace", RuntimeKind.INTERACTIVE_CHAT, {"roots": {}}),
        ("workspace", RuntimeKind.ONE_SHOT, {"roots": {}}),
        ("workspace", RuntimeKind.FORGE_EXEC, {"roots": {}}),
        ("workspace", RuntimeKind.SWARM_WORKER, {}),
        ("workspace", RuntimeKind.SUBAGENT, {}),
        ("workspace", RuntimeKind.CONFLICT_AUTO_RESOLVE, {}),
        ("workspace", None, {}),
        ("disabled", RuntimeKind.INTERACTIVE_CHAT, {}),
    ],
)
def test_http_client_advertises_roots_capability_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roots_mode: str,
    runtime_kind: RuntimeKind | None,
    expected_capabilities: dict[str, object],
) -> None:
    initialize_capabilities: list[dict[str, object]] = []

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            initialize_capabilities.append(dict(payload["params"]["capabilities"]))
            return _json_response(_jsonrpc_response(request, _initialize_result()))
        if method == "notifications/initialized":
            return _empty_response()
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"roots_mode": roots_mode},
        runtime_kind=runtime_kind,
    )
    try:
        client.ensure_initialized()
        assert initialize_capabilities == [expected_capabilities]
        if "roots" in expected_capabilities:
            assert "listChanged" not in expected_capabilities["roots"]
    finally:
        client.close()
        server.close()


def test_http_client_handles_roots_list_before_matching_post_sse_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "roots-session-1"
    static_headers = {"X-Static-Roots-Test": "opaque"}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            assert payload["params"]["capabilities"] == {"roots": {}}
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(data=_jsonrpc_request("roots/list")),
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                ]
            )
        if "result" in payload and method == "":
            assert payload == {
                "jsonrpc": "2.0",
                "id": "server-request-1",
                "result": {
                    "roots": [
                        {
                            "uri": tmp_path.resolve().as_uri(),
                            "name": tmp_path.name,
                        }
                    ]
                },
            }
            assert request.headers.get("mcp-session-id") == session_id
            assert (
                request.headers.get("mcp-protocol-version") == MCP_HTTP_PREFERRED_PROTOCOL_VERSION
            )
            assert request.headers.get("x-static-roots-test") == "opaque"
            return _empty_response()
        raise AssertionError(f"unexpected request payload: {payload!r}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        headers=static_headers,
        server_overrides={"roots_mode": "workspace"},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()


def test_http_client_handles_roots_list_before_matching_open_ended_post_sse_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for iteration in range(3):
        session_id = f"roots-open-ended-session-{iteration}"

        def handler(
            _server: _ThreadedMcpHttpServer,
            request: _RecordedRequest,
            _session_id: str = session_id,
        ) -> _ResponseSpec:
            payload = request.json() if request.method == "POST" else {}
            method = str(payload.get("method") or "")
            if request.method == "DELETE":
                return _empty_response(status=204)
            if method == "initialize":
                return _json_response(
                    _jsonrpc_response(request, _initialize_result()),
                    headers={"MCP-Session-Id": _session_id},
                )
            if method == "notifications/initialized":
                return _empty_response()
            if method == "tools/list":
                return _streamed_sse_response(
                    [
                        (_sse_event(data=_jsonrpc_request("roots/list")), 0.03),
                        (
                            _sse_event(
                                data=_jsonrpc_response(request, {"tools": _tools_payload()})
                            ),
                            0.0,
                        ),
                    ]
                )
            if "result" in payload and method == "":
                return _empty_response()
            raise AssertionError(f"unexpected request payload: {payload!r}")

        server = _ThreadedMcpHttpServer(handler=handler)
        client = _client(
            tmp_path,
            monkeypatch,
            server,
            server_overrides={"roots_mode": "workspace"},
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        )
        try:
            tools = client.list_tools()
            assert [tool.name for tool in tools] == ["alpha-tool"]
        finally:
            client.close()
            server.close()


def test_http_client_rejects_roots_list_after_matching_bounded_post_sse_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            assert payload["params"]["capabilities"] == {"roots": {}}
            return _json_response(_jsonrpc_response(request, _initialize_result()))
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                    _sse_event(data=_jsonrpc_request("roots/list")),
                ]
            )
        if "result" in payload and method == "":
            raise AssertionError(
                "unexpected roots response POST after matching tools/list response"
            )
        raise AssertionError(f"unexpected request payload: {payload!r}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"roots_mode": "workspace"},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "roots/list" in message
        assert "follow-up" in message
    finally:
        client.close()
        server.close()


def test_http_client_rejects_roots_list_during_initialized_notification_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            assert payload["params"]["capabilities"] == {"roots": {}}
            return _json_response(_jsonrpc_response(request, _initialize_result()))
        if method == "notifications/initialized":
            return _json_response(_jsonrpc_request("roots/list"))
        if "result" in payload and method == "":
            raise AssertionError(
                "unexpected roots response POST during initialized notification flow"
            )
        raise AssertionError(f"unexpected request payload: {payload!r}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"roots_mode": "workspace"},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.ensure_initialized()
        message = str(exc_info.value)
        assert "roots/list" in message
        assert "notifications/initialized" in message
    finally:
        client.close()
        server.close()


def test_http_client_fails_clearly_when_roots_response_post_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(_jsonrpc_response(request, _initialize_result()))
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(data=_jsonrpc_request("roots/list")),
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                ]
            )
        if "result" in payload and method == "":
            return _json_response(
                {"error": "boom"},
                status=500,
                headers={"Content-Type": "application/json"},
            )
        raise AssertionError(f"unexpected request payload: {payload!r}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"roots_mode": "workspace"},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        with pytest.raises(McpHttpTransportRemoteError) as exc_info:
            client.list_tools()
        assert "server-initiated request 'roots/list'" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_still_rejects_non_roots_server_requests_before_matching_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(_jsonrpc_response(request, _initialize_result()))
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(data=_jsonrpc_request("sampling/createMessage")),
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                ]
            )
        raise AssertionError(f"unexpected request payload: {payload!r}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"roots_mode": "workspace"},
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.list_tools()
        assert "sampling/createMessage" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_json_initialize_list_and_call_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "json-session-1"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            assert request.headers["mcp-session-id"] == session_id
            assert request.headers["mcp-protocol-version"] == MCP_HTTP_PREFERRED_PROTOCOL_VERSION
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        if method == "tools/call":
            return _json_response(_jsonrpc_response(request, _tool_call_result()))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        result = client.call_tool(tool_name="alpha-tool", arguments={})
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert result.content_summary == "text(2 chars)"
        assert client.session_negotiated is True
        assert client.negotiated_protocol_version == MCP_HTTP_PREFERRED_PROTOCOL_VERSION
    finally:
        client.close()
        server.close()


def test_http_client_post_sse_initialize_list_and_call_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-1"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _sse_response(
                [
                    _sse_event(event_id="priming-only"),
                    _sse_event(
                        event="message",
                        retry_ms=50,
                        data=_jsonrpc_response(request, _initialize_result()),
                    ),
                ],
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _sse_response([_sse_event(event_id="notification-priming")])
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(event_id="list-priming"),
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                ]
            )
        if method == "tools/call":
            return _sse_response(
                [
                    _sse_event(
                        data=json.dumps(
                            _jsonrpc_response(request, _tool_call_result()),
                            indent=2,
                            sort_keys=True,
                        )
                    )
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        result = client.call_tool(tool_name="alpha-tool", arguments={})
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert result.content_summary == "text(2 chars)"
    finally:
        client.close()
        server.close()


def test_http_client_post_sse_notification_before_response_sets_tools_list_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-2"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(data=_jsonrpc_notification("notifications/tools/list_changed", {})),
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert client.tools_list_changed is True
    finally:
        client.close()
        server.close()


def test_http_client_post_sse_notification_before_response_sets_resources_list_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-resources-list-changed-session"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result(capabilities={"resources": {}})),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "resources/list":
            return _sse_response(
                [
                    _sse_event(
                        data=_jsonrpc_notification("notifications/resources/list_changed", {})
                    ),
                    _sse_event(
                        data=_jsonrpc_response(
                            request,
                            {"resources": [_resources_payload()[0]]},
                        )
                    ),
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        resources = client.list_resources()
        assert [resource.uri for resource in resources] == ["file:///alpha.txt"]
        assert client.resources_list_changed is True
        assert client.prompts_list_changed is False
    finally:
        client.close()
        server.close()


def test_http_client_post_sse_notification_before_response_sets_prompts_list_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-prompts-list-changed-session"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result(capabilities={"prompts": {}})),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "prompts/list":
            return _sse_response(
                [
                    _sse_event(
                        data=_jsonrpc_notification("notifications/prompts/list_changed", {})
                    ),
                    _sse_event(
                        data=_jsonrpc_response(
                            request,
                            {"prompts": [_prompts_payload()[0]]},
                        )
                    ),
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        prompts = client.list_prompts()
        assert [prompt.name for prompt in prompts] == ["review_pr"]
        assert client.prompts_list_changed is True
        assert client.resources_list_changed is False
    finally:
        client.close()
        server.close()


def test_http_client_post_sse_open_ended_response_completes_after_matching_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _ResponseSpec(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=_sse_event(
                    data=_jsonrpc_response(request, {"tools": _tools_payload()})
                ).encode("utf-8"),
                send_content_length=False,
                linger_after_body_s=0.5,
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"call_timeout_s": 0.2},
    )
    try:
        client.ensure_initialized()
        started = time.monotonic()
        tools = client.list_tools()
        elapsed = time.monotonic() - started
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert elapsed < 0.3
    finally:
        client.close()
        server.close()


def test_http_client_post_sse_open_ended_response_handles_gzip_content_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-gzip"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            compressed_body = gzip.compress(
                _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})).encode(
                    "utf-8"
                )
            )
            midpoint = len(compressed_body) // 2 or len(compressed_body)
            return _ResponseSpec(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Content-Encoding": "gzip",
                },
                body_chunks=(
                    (compressed_body[:midpoint], 0.0),
                    (compressed_body[midpoint:], 0.0),
                ),
                send_content_length=False,
                linger_after_body_s=0.2,
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()


def test_http_client_observes_open_ended_post_sse_notification_follow_up_after_tools_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for iteration in range(3):
        session_id_text = f"sse-session-open-ended-list-notify-{iteration}"

        def handler(
            _server: _ThreadedMcpHttpServer,
            request: _RecordedRequest,
            _session_id: str = session_id_text,
        ) -> _ResponseSpec:
            payload = request.json() if request.method == "POST" else {}
            method = str(payload.get("method") or "")
            if request.method == "DELETE":
                return _empty_response(status=204)
            if method == "initialize":
                return _json_response(
                    _jsonrpc_response(request, _initialize_result()),
                    headers={"MCP-Session-Id": _session_id},
                )
            if method == "notifications/initialized":
                return _empty_response()
            if method == "tools/list":
                return _streamed_sse_response(
                    [
                        (
                            _sse_event(
                                data=_jsonrpc_response(request, {"tools": _tools_payload()})
                            ),
                            0.03,
                        ),
                        (
                            _sse_event(
                                data=_jsonrpc_notification(
                                    "notifications/tools/list_changed",
                                    {},
                                )
                            ),
                            0.0,
                        ),
                    ]
                )
            raise AssertionError(f"unexpected method: {method}")

        server = _ThreadedMcpHttpServer(handler=handler)
        client = _client(tmp_path, monkeypatch, server)
        try:
            tools = client.list_tools()
            assert [tool.name for tool in tools] == ["alpha-tool"]
            assert client.tools_list_changed is True
        finally:
            client.close()
            server.close()


def test_http_client_observes_crlf_fragmented_open_ended_post_sse_notification_follow_up_after_tools_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-list-crlf-fragmented-notify"
    notification_chunks = _fragment_crlf_sse_event_after_first_line_break(
        _sse_event(
            data=json.dumps(
                _jsonrpc_notification("notifications/tools/list_changed", {}),
                indent=2,
                sort_keys=True,
            )
        ),
        delay_before_remainder_s=0.2,
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _streamed_sse_response(
                [
                    (_sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})), 0.0),
                    *notification_chunks,
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert client.tools_list_changed is True
    finally:
        client.close()
        server.close()


def test_http_client_observes_fragmented_open_ended_post_sse_notification_follow_up_after_tools_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-list-fragmented-notify"
    notification_chunks = _fragment_sse_event_after_first_line(
        _sse_event(data=_jsonrpc_notification("notifications/tools/list_changed", {})),
        delay_before_terminator_s=0.2,
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _streamed_sse_response(
                [
                    (_sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})), 0.0),
                    *notification_chunks,
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert client.tools_list_changed is True
    finally:
        client.close()
        server.close()


def test_http_client_observes_near_boundary_open_ended_post_sse_notification_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-boundary-notify"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _streamed_sse_response(
                [
                    (
                        _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                        0.09,
                    ),
                    (
                        _sse_event(
                            data=_jsonrpc_notification("notifications/tools/list_changed", {})
                        ),
                        0.0,
                    ),
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert client.tools_list_changed is True
    finally:
        client.close()
        server.close()


def test_http_client_rejects_crlf_fragmented_open_ended_post_sse_server_request_follow_up_after_tools_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-list-crlf-fragmented-request"
    request_chunks = _fragment_crlf_sse_event_after_first_line_break(
        _sse_event(
            data=json.dumps(
                _jsonrpc_request("roots/list"),
                indent=2,
                sort_keys=True,
            )
        ),
        delay_before_remainder_s=0.2,
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _streamed_sse_response(
                [
                    (_sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})), 0.0),
                    *request_chunks,
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.list_tools()
        assert "roots/list" in str(exc_info.value)
        assert "follow-up" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_rejects_fragmented_open_ended_post_sse_server_request_follow_up_after_tools_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-list-fragmented-request"
    request_chunks = _fragment_sse_event_after_first_line(
        _sse_event(data=_jsonrpc_request("roots/list")),
        delay_before_terminator_s=0.2,
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _streamed_sse_response(
                [
                    (_sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})), 0.0),
                    *request_chunks,
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.list_tools()
        assert "roots/list" in str(exc_info.value)
        assert "follow-up" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_rejects_open_ended_post_sse_server_request_follow_up_after_tools_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for iteration in range(3):
        session_id_text = f"sse-session-open-ended-list-request-{iteration}"

        def handler(
            _server: _ThreadedMcpHttpServer,
            request: _RecordedRequest,
            _session_id: str = session_id_text,
        ) -> _ResponseSpec:
            payload = request.json() if request.method == "POST" else {}
            method = str(payload.get("method") or "")
            if request.method == "DELETE":
                return _empty_response(status=204)
            if method == "initialize":
                return _json_response(
                    _jsonrpc_response(request, _initialize_result()),
                    headers={"MCP-Session-Id": _session_id},
                )
            if method == "notifications/initialized":
                return _empty_response()
            if method == "tools/list":
                return _streamed_sse_response(
                    [
                        (
                            _sse_event(
                                data=_jsonrpc_response(request, {"tools": _tools_payload()})
                            ),
                            0.03,
                        ),
                        (_sse_event(data=_jsonrpc_request("roots/list")), 0.0),
                    ]
                )
            raise AssertionError(f"unexpected method: {method}")

        server = _ThreadedMcpHttpServer(handler=handler)
        client = _client(tmp_path, monkeypatch, server)
        try:
            with pytest.raises(McpHttpTransportProtocolError) as exc_info:
                client.list_tools()
            assert "roots/list" in str(exc_info.value)
            assert "follow-up" in str(exc_info.value)
        finally:
            client.close()
            server.close()


def test_http_client_observes_open_ended_post_sse_notification_follow_up_after_tools_call_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for iteration in range(3):
        session_id_text = f"sse-session-open-ended-call-notify-{iteration}"

        def handler(
            _server: _ThreadedMcpHttpServer,
            request: _RecordedRequest,
            _session_id: str = session_id_text,
        ) -> _ResponseSpec:
            payload = request.json() if request.method == "POST" else {}
            method = str(payload.get("method") or "")
            if request.method == "DELETE":
                return _empty_response(status=204)
            if method == "initialize":
                return _json_response(
                    _jsonrpc_response(request, _initialize_result()),
                    headers={"MCP-Session-Id": _session_id},
                )
            if method == "notifications/initialized":
                return _empty_response()
            if method == "tools/list":
                return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
            if method == "tools/call":
                return _streamed_sse_response(
                    [
                        (_sse_event(data=_jsonrpc_response(request, _tool_call_result())), 0.03),
                        (
                            _sse_event(
                                data=_jsonrpc_notification(
                                    "notifications/tools/list_changed",
                                    {},
                                )
                            ),
                            0.0,
                        ),
                    ]
                )
            raise AssertionError(f"unexpected method: {method}")

        server = _ThreadedMcpHttpServer(handler=handler)
        client = _client(tmp_path, monkeypatch, server)
        try:
            result = client.call_tool(tool_name="alpha-tool", arguments={})
            assert result.content_summary == "text(2 chars)"
            assert client.tools_list_changed is True
        finally:
            client.close()
            server.close()


def test_http_client_observes_fragmented_open_ended_post_sse_notification_follow_up_after_tools_call_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-call-fragmented-notify"
    notification_chunks = _fragment_sse_event_after_first_line(
        _sse_event(data=_jsonrpc_notification("notifications/tools/list_changed", {})),
        delay_before_terminator_s=0.2,
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        if method == "tools/call":
            return _streamed_sse_response(
                [
                    (_sse_event(data=_jsonrpc_response(request, _tool_call_result())), 0.0),
                    *notification_chunks,
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        result = client.call_tool(tool_name="alpha-tool", arguments={})
        assert result.content_summary == "text(2 chars)"
        assert client.tools_list_changed is True
    finally:
        client.close()
        server.close()


def test_http_client_rejects_open_ended_post_sse_server_request_follow_up_after_tools_call_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for iteration in range(3):
        session_id_text = f"sse-session-open-ended-call-request-{iteration}"

        def handler(
            _server: _ThreadedMcpHttpServer,
            request: _RecordedRequest,
            _session_id: str = session_id_text,
        ) -> _ResponseSpec:
            payload = request.json() if request.method == "POST" else {}
            method = str(payload.get("method") or "")
            if request.method == "DELETE":
                return _empty_response(status=204)
            if method == "initialize":
                return _json_response(
                    _jsonrpc_response(request, _initialize_result()),
                    headers={"MCP-Session-Id": _session_id},
                )
            if method == "notifications/initialized":
                return _empty_response()
            if method == "tools/list":
                return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
            if method == "tools/call":
                return _streamed_sse_response(
                    [
                        (_sse_event(data=_jsonrpc_response(request, _tool_call_result())), 0.03),
                        (_sse_event(data=_jsonrpc_request("roots/list")), 0.0),
                    ]
                )
            raise AssertionError(f"unexpected method: {method}")

        server = _ThreadedMcpHttpServer(handler=handler)
        client = _client(tmp_path, monkeypatch, server)
        try:
            with pytest.raises(McpHttpTransportProtocolError) as exc_info:
                client.call_tool(tool_name="alpha-tool", arguments={})
            assert "roots/list" in str(exc_info.value)
            assert "follow-up" in str(exc_info.value)
        finally:
            client.close()
            server.close()


def test_http_client_rejects_fragmented_open_ended_post_sse_server_request_follow_up_after_tools_call_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-call-fragmented-request"
    request_chunks = _fragment_sse_event_after_first_line(
        _sse_event(data=_jsonrpc_request("roots/list")),
        delay_before_terminator_s=0.2,
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        if method == "tools/call":
            return _streamed_sse_response(
                [
                    (_sse_event(data=_jsonrpc_response(request, _tool_call_result())), 0.0),
                    *request_chunks,
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.call_tool(tool_name="alpha-tool", arguments={})
        assert "roots/list" in str(exc_info.value)
        assert "follow-up" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_open_ended_post_sse_follow_up_timeout_stays_within_request_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-hard-timeout"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _streamed_sse_response(
                [
                    (
                        _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                        0.08,
                    ),
                    (
                        _sse_event(
                            data=_jsonrpc_notification("notifications/tools/list_changed", {})
                        ),
                        0.0,
                    ),
                ],
                linger_after_body_s=0.2,
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"call_timeout_s": 0.15},
    )
    try:
        client.ensure_initialized()
        started = time.monotonic()
        with pytest.raises(McpHttpTransportTimeoutError):
            client.list_tools()
        elapsed = time.monotonic() - started
        assert elapsed < 0.3
        assert client.tools_list_changed is False
    finally:
        client.close()
        server.close()


def test_http_client_open_ended_post_sse_partial_follow_up_event_timeout_does_not_succeed_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-open-ended-partial-follow-up-timeout"
    partial_follow_up_line = (
        'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed","params"'
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _streamed_sse_response(
                [
                    (_sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})), 0.0),
                    (partial_follow_up_line, 0.0),
                ],
                linger_after_body_s=0.2,
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={"call_timeout_s": 0.15},
    )
    try:
        client.ensure_initialized()
        with pytest.raises(McpHttpTransportTimeoutError):
            client.list_tools()
        assert client.tools_list_changed is False
    finally:
        client.close()
        server.close()


def test_http_client_rejects_pre_response_unsupported_server_request_in_post_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-3"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(data=_jsonrpc_request("roots/list")),
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.list_tools()
        assert "server-initiated request" in str(exc_info.value)
        assert "roots/list" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_rejects_additional_jsonrpc_after_matching_post_sse_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "sse-session-post-response"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _sse_response(
                [
                    _sse_event(data=_jsonrpc_response(request, {"tools": _tools_payload()})),
                    _sse_event(data=_jsonrpc_notification("notifications/tools/list_changed", {})),
                ]
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.list_tools()
        assert "additional JSON-RPC message after the matching HTTP response" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_reuses_negotiated_session_id_and_protocol_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "session-reuse-1"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            assert "mcp-session-id" not in request.headers
            assert "mcp-protocol-version" not in request.headers
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            assert request.headers["mcp-session-id"] == session_id
            assert request.headers["mcp-protocol-version"] == MCP_HTTP_PREFERRED_PROTOCOL_VERSION
            return _empty_response()
        if method == "tools/list":
            assert request.headers["mcp-session-id"] == session_id
            assert request.headers["mcp-protocol-version"] == MCP_HTTP_PREFERRED_PROTOCOL_VERSION
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()


def test_http_client_ignores_mid_session_response_attempts_to_rotate_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial_session_id = "session-original"
    rotated_session_id = "session-rotated"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            assert request.headers["mcp-session-id"] == initial_session_id
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": initial_session_id},
            )
        if method == "notifications/initialized":
            assert request.headers["mcp-session-id"] == initial_session_id
            return _empty_response()
        if method == "tools/list":
            assert request.headers["mcp-session-id"] == initial_session_id
            return _json_response(
                _jsonrpc_response(request, {"tools": _tools_payload()}),
                headers={"MCP-Session-Id": rotated_session_id},
            )
        if method == "tools/call":
            assert request.headers["mcp-session-id"] == initial_session_id
            return _json_response(_jsonrpc_response(request, _tool_call_result()))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        result = client.call_tool(tool_name="alpha-tool", arguments={})
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert result.content_summary == "text(2 chars)"
    finally:
        client.close()
        server.close()


def test_http_client_attempts_delete_on_close_when_session_id_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "close-session-1"
    delete_seen = {"value": False}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            delete_seen["value"] = True
            assert request.headers["mcp-session-id"] == session_id
            assert request.headers["mcp-protocol-version"] == MCP_HTTP_PREFERRED_PROTOCOL_VERSION
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        client.list_tools()
    finally:
        client.close()
        server.close()
    assert delete_seen["value"] is True


def test_http_client_tolerates_delete_405_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "close-session-405"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=405)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        client.list_tools()
    finally:
        client.close()
        server.close()


def test_http_client_static_header_auth_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_HTTP_MCP_TOKEN", "Bearer http-secret")

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        assert request.headers.get("authorization") == "Bearer http-secret"
        payload = request.json() if request.method == "POST" else {}
        method = str(payload.get("method") or "")
        if request.method == "DELETE":
            return _empty_response(status=204)
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "auth-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        headers={"Authorization": "${ALYSIS_HTTP_MCP_TOKEN}"},
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()


def test_http_client_non_oauth_401_reports_http_auth_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        return _ResponseSpec(
            status=401,
            headers={"Content-Type": "text/plain"},
            body=b"auth required",
        )

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportAuthRequiredError) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "Configure static headers" in message
        assert "mcp auth login <server_id>" in message
    finally:
        client.close()
        server.close()


def test_http_client_initial_post_rejection_mentions_legacy_http_sse_out_of_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        return _ResponseSpec(
            status=405,
            headers={"Content-Type": "text/plain"},
            body=b"method not allowed",
        )

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportProtocolError) as exc_info:
            client.ensure_initialized()
        message = str(exc_info.value)
        assert "legacy HTTP+SSE" in message
        assert "out of scope" in message
    finally:
        client.close()
        server.close()


def test_http_client_reinitializes_once_and_retries_tools_list_after_session_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"initialize_count": 0}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            state["initialize_count"] += 1
            session_id = f"session-{state['initialize_count']}"
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            if request.headers.get("mcp-session-id") == "session-1":
                return _empty_response(status=404)
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
        assert state["initialize_count"] == 2
    finally:
        client.close()
        server.close()


def test_http_client_does_not_replay_tools_call_after_session_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"initialize_count": 0, "tools_call_count": 0}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            state["initialize_count"] += 1
            session_id = f"call-session-{state['initialize_count']}"
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        if method == "tools/call":
            state["tools_call_count"] += 1
            if request.headers.get("mcp-session-id") == "call-session-1":
                return _empty_response(status=404)
            return _json_response(_jsonrpc_response(request, _tool_call_result()))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        client.list_tools()
        with pytest.raises(McpClientError) as exc_info:
            client.call_tool(tool_name="alpha-tool", arguments={})
        message = str(exc_info.value)
        assert "not automatically retried" in message
        assert "side-effectful" in message

        result = client.call_tool(tool_name="alpha-tool", arguments={})
        assert result.content_summary == "text(2 chars)"
        assert state["initialize_count"] == 2
        assert state["tools_call_count"] == 2
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", b"nope"),
        ("application/json", b"not-json"),
    ],
)
def test_http_client_rejects_unsupported_or_malformed_initialize_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    body: bytes,
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        return _ResponseSpec(
            status=200,
            headers={"Content-Type": content_type},
            body=body,
        )

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpHttpTransportProtocolError):
            client.ensure_initialized()
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize(
    ("runtime_kind", "should_expose"),
    [
        (RuntimeKind.INTERACTIVE_CHAT, True),
        (RuntimeKind.ONE_SHOT, True),
        (RuntimeKind.FORGE_EXEC, True),
        (RuntimeKind.SWARM_WORKER, False),
        (RuntimeKind.SUBAGENT, False),
        (RuntimeKind.CONFLICT_AUTO_RESOLVE, False),
    ],
)
def test_http_manager_exposes_tools_only_in_supported_runtimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: RuntimeKind,
    should_expose: bool,
) -> None:
    session_id = "manager-session"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    _resolved_http_server(tmp_path, monkeypatch, server.base_url)
    session = create_session(
        cfg=_basic_cfg(),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=True,
        api_key_override="k",
        runtime_kind=runtime_kind,
        one_shot_execution=(runtime_kind == RuntimeKind.ONE_SHOT),
    )
    try:
        mcp_tools = [name for name in session.tools if name.startswith("mcp__")]
        assert bool(mcp_tools) is should_expose
    finally:
        session.close()
        server.close()


def test_http_manager_snapshot_metadata_stays_non_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_HTTP_SECRET", "Bearer snapshot-secret")
    session_id = "snapshot-session-secret"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        assert request.headers.get("authorization") == "Bearer snapshot-secret"
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    resolved_server = _resolved_http_server(
        tmp_path,
        monkeypatch,
        server.base_url,
        headers={"Authorization": "${ALYSIS_HTTP_SECRET}"},
    )
    manager = McpManager(
        resolved_config=load_resolved_mcp_config(workspace_root=tmp_path),
        workspace_root=tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        session_id="sid",
    )
    try:
        bindings = manager.tool_bindings
        assert len(bindings) == 1
        snapshot = manager.catalog_snapshot_metadata()
        server_entry = snapshot["server_catalogs"][0]
        assert server_entry["transport"] == "http"
        assert server_entry["session_negotiated"] is True
        snapshot_text = json.dumps(snapshot, sort_keys=True)
        assert "snapshot-secret" not in snapshot_text
        assert session_id not in snapshot_text
        assert "Authorization" not in snapshot_text
        assert resolved_server.headers["Authorization"] == "Bearer snapshot-secret"
    finally:
        manager.close()
        server.close()


def test_http_client_supports_paginated_resources_list_and_read_over_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [
        [_resources_payload()[0]],
        [_resources_payload()[1]],
    ]

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"resources": {}}),
                ),
                headers={"MCP-Session-Id": "resources-json-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "resources/list":
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            cursor = params.get("cursor")
            if cursor is None:
                return _json_response(
                    _jsonrpc_response(
                        request,
                        {
                            "resources": pages[0],
                            "nextCursor": "page:1",
                        },
                    )
                )
            assert cursor == "page:1"
            return _json_response(_jsonrpc_response(request, {"resources": pages[1]}))
        if method == "resources/read":
            assert payload["params"]["uri"] == "https://example.com/spec.json"
            return _json_response(
                _jsonrpc_response(
                    request,
                    _resource_read_result(
                        uri="https://example.com/spec.json",
                        mime_type="application/json",
                        text='{"ok":true}',
                    ),
                )
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        resources = client.list_resources()
        allowed_uris = frozenset(resource.uri for resource in resources)
        assert [resource.uri for resource in resources] == [
            "file:///alpha.txt",
            "https://example.com/spec.json",
        ]

        read_result = client.read_resource(
            resource_uri="https://example.com/spec.json",
            allowed_uris=allowed_uris,
        )
        assert read_result.mime_type == "application/json"
        assert read_result.text == '{"ok":true}'
        assert "json text" in read_result.content_summary
    finally:
        client.close()
        server.close()


def test_http_client_supports_resources_read_over_post_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "resources-sse-session"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"resources": {}}),
                ),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "resources/read":
            return _sse_response(
                [
                    _sse_event(
                        data=_jsonrpc_response(
                            request,
                            _resource_read_result(uri="file:///alpha.txt", text="alpha body"),
                        )
                    )
                ],
                headers={"MCP-Session-Id": session_id},
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        read_result = client.read_resource(
            resource_uri="file:///alpha.txt",
            allowed_uris=frozenset({"file:///alpha.txt"}),
        )
        assert read_result.text == "alpha body"
        assert read_result.contents[0].text == "alpha body"
    finally:
        client.close()
        server.close()


def test_http_client_rejects_malformed_resources_list_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"resources": {}}),
                )
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "resources/list":
            return _json_response(_jsonrpc_response(request, {"resources": "broken"}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpClientError) as exc_info:
            client.list_resources()
        assert "resources/list result.resources must be an array" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_reinitializes_once_and_retries_resources_list_after_session_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"initialize_count": 0}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            state["initialize_count"] += 1
            session_id = f"resources-list-session-{state['initialize_count']}"
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"resources": {}}),
                ),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "resources/list":
            if request.headers.get("mcp-session-id") == "resources-list-session-1":
                return _empty_response(status=404)
            return _json_response(
                _jsonrpc_response(request, {"resources": [_resources_payload()[0]]})
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        resources = client.list_resources()
        assert [resource.uri for resource in resources] == ["file:///alpha.txt"]
        assert state["initialize_count"] == 2
    finally:
        client.close()
        server.close()


def test_http_client_reinitializes_once_and_retries_resources_read_after_session_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"initialize_count": 0}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            state["initialize_count"] += 1
            session_id = f"resources-read-session-{state['initialize_count']}"
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"resources": {}}),
                ),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "resources/read":
            if request.headers.get("mcp-session-id") == "resources-read-session-1":
                return _empty_response(status=404)
            return _json_response(
                _jsonrpc_response(
                    request,
                    _resource_read_result(uri="file:///alpha.txt", text="recovered body"),
                )
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpClientError) as exc_info:
            client.read_resource(resource_uri="file:///alpha.txt")
        assert "requires a frozen resource snapshot allowlist" in str(exc_info.value)

        result = client.read_resource(
            resource_uri="file:///alpha.txt",
            allowed_uris=frozenset({"file:///alpha.txt"}),
        )
        assert result.text == "recovered body"
        assert state["initialize_count"] == 2
    finally:
        client.close()
        server.close()


def test_http_client_supports_paginated_prompts_list_and_get_over_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [[_prompts_payload()[0]], [_prompts_payload()[1]]]
    expected_text = "Review repo owner/alysis."

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"prompts": {}}),
                ),
                headers={"MCP-Session-Id": "prompts-json-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "prompts/list":
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            cursor = params.get("cursor")
            if cursor is None:
                return _json_response(
                    _jsonrpc_response(
                        request,
                        {
                            "prompts": pages[0],
                            "nextCursor": "page:1",
                        },
                    )
                )
            assert cursor == "page:1"
            return _json_response(_jsonrpc_response(request, {"prompts": pages[1]}))
        if method == "prompts/get":
            assert payload["params"]["name"] == "review_pr"
            assert payload["params"]["arguments"] == {"repo": "owner/alysis"}
            return _json_response(
                _jsonrpc_response(
                    request,
                    _prompt_get_result(
                        name="review_pr",
                        description="Review helper",
                        text=expected_text,
                    ),
                )
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        prompts = client.list_prompts()
        assert [prompt.name for prompt in prompts] == ["review_pr", "draft_issue"]

        prompt_result = client.get_prompt(
            name="review_pr",
            arguments={"repo": "owner/alysis"},
        )
        assert prompt_result.description == "Review helper"
        assert prompt_result.text == expected_text
        assert f"user: text({len(expected_text)} chars)" in prompt_result.content_summary
    finally:
        client.close()
        server.close()


def test_http_client_supports_prompt_get_over_post_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "prompts-sse-session"

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"prompts": {}}),
                ),
                headers={"MCP-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "prompts/get":
            return _sse_response(
                [
                    _sse_event(
                        data=_jsonrpc_response(
                            request,
                            _prompt_get_result(name="review_pr", text="Prompt over SSE."),
                        )
                    )
                ],
                headers={"MCP-Session-Id": session_id},
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        result = client.get_prompt(name="review_pr")
        assert result.text == "Prompt over SSE."
        assert result.messages[0].role == "user"
    finally:
        client.close()
        server.close()


def test_http_client_rejects_malformed_prompts_list_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"prompts": {}}),
                )
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "prompts/list":
            return _json_response(_jsonrpc_response(request, {"prompts": "broken"}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpClientError) as exc_info:
            client.list_prompts()
        assert "prompts/list result.prompts must be an array" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_rejects_malformed_prompt_get_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(
                    request,
                    _initialize_result(capabilities={"prompts": {}}),
                )
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "prompts/get":
            return _json_response(
                _jsonrpc_response(
                    request,
                    {
                        "name": "review_pr",
                        "messages": "broken",
                    },
                )
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(tmp_path, monkeypatch, server)
    try:
        with pytest.raises(McpClientError) as exc_info:
            client.get_prompt(name="review_pr")
        assert "prompts/get result.messages must be an array" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_oauth_injects_bearer_header_on_requests_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record(
        "alpha", _oauth_record(access_token="test_access", refresh_token="test_refresh")
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        assert request.headers.get("authorization") == "Bearer test_access"
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "oauth-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()

    assert {request.method for request in server.requests} == {"POST", "DELETE"}


def test_http_client_oauth_close_uses_stored_token_without_refresh_or_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record(
        "alpha", _oauth_record(access_token="test_access", refresh_token="test_refresh")
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            assert request.headers.get("authorization") == "Bearer expired-access"
            return _empty_response(status=204)
        assert request.headers.get("authorization") == "Bearer test_access"
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "close-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
        oauth_fixture_server.request_log.clear()
        oauth_fixture_server.refresh_response_status = 400
        oauth_fixture_server.refresh_response_override = {"error": "invalid_grant"}
        save_oauth_token_record(
            "alpha",
            _oauth_record(
                access_token="expired-access", refresh_token="test_refresh", expires_delta_s=-60
            ),
        )
    finally:
        client.close()
        server.close()

    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "expired-access"
    assert stored.refresh_token == "test_refresh"
    assert "/token" not in oauth_fixture_server.request_log


def test_http_client_oauth_refreshes_expired_token_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record(
        "alpha",
        _oauth_record(
            access_token="expired-access", refresh_token="test_refresh", expires_delta_s=-60
        ),
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        assert request.headers.get("authorization") == "Bearer refreshed_access"
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "refresh-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    oauth_fixture_server.expected_token_resource = server.base_url
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "scopes": ["openid", "profile"],
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()

    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "refreshed_access"
    assert stored.refresh_token == "rotated_refresh"
    assert stored.granted_scopes == ("openid", "profile")
    assert oauth_fixture_server.token_requests[-1]["scope"] == "openid profile"
    assert oauth_fixture_server.token_requests[-1]["resource"] == server.base_url
    assert "/token" in oauth_fixture_server.request_log


def test_http_client_oauth_refresh_preserves_granted_scopes_without_config_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    oauth_fixture_server.rotate_refresh_token = False
    oauth_fixture_server.refresh_response_override = {
        "access_token": "refreshed_access",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    save_oauth_token_record(
        "alpha",
        _oauth_record(
            access_token="expired-access",
            refresh_token="test_refresh",
            expires_delta_s=-60,
            scopes=("openid", "profile"),
        ),
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        assert request.headers.get("authorization") == "Bearer refreshed_access"
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "refresh-preserve-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    oauth_fixture_server.expected_token_resource = server.base_url
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()

    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "refreshed_access"
    assert stored.refresh_token == "test_refresh"
    assert stored.granted_scopes == ("openid", "profile")
    assert oauth_fixture_server.token_requests[-1]["scope"] == "openid profile"
    assert oauth_fixture_server.token_requests[-1]["resource"] == server.base_url


def test_http_client_oauth_retries_once_after_401_for_safe_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record(
        "alpha", _oauth_record(access_token="stale-access", refresh_token="test_refresh")
    )
    initialize_attempts = {"count": 0}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        auth_header = request.headers.get("authorization")
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            initialize_attempts["count"] += 1
            if auth_header == "Bearer stale-access":
                return _ResponseSpec(
                    status=401,
                    headers={"Content-Type": "application/json"},
                    body=b'{"error":"unauthorized"}',
                )
            assert auth_header == "Bearer refreshed_access"
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "retry-session"},
            )
        assert auth_header == "Bearer refreshed_access"
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _json_response(_jsonrpc_response(request, {"tools": _tools_payload()}))
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()
        server.close()

    assert initialize_attempts["count"] == 2
    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "refreshed_access"
    assert stored.refresh_token == "rotated_refresh"


def test_http_client_oauth_refresh_failure_clears_tokens_and_requires_relogin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    oauth_fixture_server.refresh_response_status = 400
    oauth_fixture_server.refresh_response_override = {"error": "invalid_grant"}
    save_oauth_token_record(
        "alpha",
        _oauth_record(
            access_token="expired-access", refresh_token="test_refresh", expires_delta_s=-60
        ),
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        raise AssertionError("request should not reach MCP server when pre-request refresh fails")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        with pytest.raises(McpOAuthReLoginRequired) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "alpha" in message
        assert "mcp auth login alpha" in message
    finally:
        client.close()
        server.close()

    assert load_oauth_token_record("alpha") is None


def test_http_client_oauth_missing_token_raises_auth_required_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        raise AssertionError("request should not reach MCP server without stored OAuth credentials")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        with pytest.raises(McpOAuthAuthRequiredError) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "mcp auth login alpha" in message
    finally:
        client.close()
        server.close()


def test_http_client_oauth_403_insufficient_scope_raises_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record(
        "alpha", _oauth_record(access_token="test_access", refresh_token="test_refresh")
    )

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        assert request.headers.get("authorization") == "Bearer test_access"
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "scope-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _ResponseSpec(
                status=403,
                headers={
                    "Content-Type": "application/json",
                    "WWW-Authenticate": 'Bearer error="insufficient_scope"',
                },
                body=b'{"error":"insufficient_scope"}',
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        with pytest.raises(McpOAuthInsufficientScopeError) as exc_info:
            client.list_tools()
        assert "insufficient_scope" in str(exc_info.value)
    finally:
        client.close()
        server.close()


def test_http_client_oauth_401_relogin_error_includes_challenge_scope_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record("alpha", _oauth_record(access_token="test_access", refresh_token=None))

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        assert request.headers.get("authorization") == "Bearer test_access"
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "hint-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/list":
            return _ResponseSpec(
                status=401,
                headers={
                    "Content-Type": "application/json",
                    "WWW-Authenticate": 'Bearer scope="openid profile" error="invalid_token"',
                },
                body=b'{"error":"unauthorized"}',
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        with pytest.raises(McpOAuthReLoginRequired) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "Server requested scopes: openid, profile." in message
        assert "test_access" not in message
    finally:
        client.close()
        server.close()


def test_http_client_oauth_tools_call_is_not_auto_replayed_after_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record(
        "alpha", _oauth_record(access_token="stale-access", refresh_token="test_refresh")
    )
    tool_call_attempts = {"count": 0}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        auth_header = request.headers.get("authorization")
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            return _json_response(
                _jsonrpc_response(request, _initialize_result()),
                headers={"MCP-Session-Id": "tool-call-session"},
            )
        if method == "notifications/initialized":
            return _empty_response()
        if method == "tools/call":
            tool_call_attempts["count"] += 1
            assert auth_header == "Bearer stale-access"
            return _ResponseSpec(
                status=401,
                headers={"Content-Type": "application/json"},
                body=b'{"error":"unauthorized"}',
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        with pytest.raises(McpOAuthAuthRequiredError) as exc_info:
            client.call_tool(tool_name="alpha-tool", arguments={})
        message = str(exc_info.value)
        assert "not automatically retried" in message
        assert "side-effectful" in message
    finally:
        client.close()
        server.close()

    assert tool_call_attempts["count"] == 1
    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "refreshed_access"
    assert stored.refresh_token == "rotated_refresh"


def test_http_client_oauth_retry_budget_is_bounded_to_one_refresh_and_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path / "cfg"))
    save_oauth_token_record(
        "alpha", _oauth_record(access_token="stale-access", refresh_token="test_refresh")
    )
    initialize_attempts = {"count": 0}

    def handler(_server: _ThreadedMcpHttpServer, request: _RecordedRequest) -> _ResponseSpec:
        if request.method == "DELETE":
            return _empty_response(status=204)
        payload = request.json()
        method = str(payload.get("method") or "")
        if method == "initialize":
            initialize_attempts["count"] += 1
            return _ResponseSpec(
                status=401,
                headers={"Content-Type": "application/json"},
                body=b'{"error":"unauthorized"}',
            )
        raise AssertionError(f"unexpected method: {method}")

    server = _ThreadedMcpHttpServer(handler=handler)
    client = _client(
        tmp_path,
        monkeypatch,
        server,
        server_overrides={
            "oauth": {
                "client_id": "test-client",
                "authorization_server_url": oauth_fixture_server.authorization_server_url,
            }
        },
    )
    try:
        with pytest.raises(McpOAuthReLoginRequired) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "after one OAuth refresh and retry" in message
    finally:
        client.close()
        server.close()

    assert initialize_attempts["count"] == 2
    assert load_oauth_token_record("alpha") is None
