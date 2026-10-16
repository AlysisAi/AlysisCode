from __future__ import annotations

import json
import os
import queue
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.mcp import transport_stdio as transport_mod
from alysis_code.mcp.cancellation import mcp_cancellation_scope
from alysis_code.mcp.client import McpClientError, McpStdioClient
from alysis_code.mcp.config import load_resolved_mcp_config, user_mcp_config_path
from alysis_code.mcp.errors import McpProcessError
from alysis_code.mcp.jsonrpc import JsonRpcResponse
from alysis_code.mcp.transport_stdio import (
    McpStdioTransportError,
    McpStdioTransportProtocolError,
    McpStdioTransportTimeoutError,
    build_stdio_subprocess_env,
    resolve_windows_command,
    windows_batch_command_line,
)
from alysis_code.runtime_kind import RuntimeKind

_FIXTURE_SERVER = (
    Path(__file__).resolve().parent / "fixtures" / "mcp_servers" / "minimal_stdio_server.py"
)
_REPEAT_TRANSPORT_RACE_CASES = 8
_REPEAT_FATAL_STDERR_CASES = 4
_DELAYED_INITIALIZED_OUTPUT_S = 0.4
_CHAINED_FOLLOW_UP_NOTIFICATION_DELAY_S = 0.07
_CHAINED_FOLLOW_UP_UNSUPPORTED_REQUEST_DELAY_S = 0.05
_WARM_STARTUP_TIMEOUT_S = 2.0
_LOW_CALL_TIMEOUT_S = transport_mod._STDOUT_RESPONSE_QUIESCENCE_WAIT_S / 2


def test_turn_cancellation_notifies_only_its_request_and_connection_recovers(tmp_path, monkeypatch):
    marker = tmp_path / "request-started"
    cancelled = tmp_path / "request-cancelled"
    server = _resolved_stdio_script_server(
        tmp_path,
        monkeypatch,
        f"""
        import json, sys
        from pathlib import Path
        active = None
        for line in sys.stdin:
            request = json.loads(line)
            method = request['method']
            if method == 'tools/call':
                active = request['id']
                Path({str(marker)!r}).touch()
                continue
            if method == 'notifications/cancelled':
                assert request['params']['requestId'] == active
                Path({str(cancelled)!r}).touch()
                print(json.dumps({{'jsonrpc':'2.0','id':active,'result':{{}}}}), flush=True)
                continue
            if 'id' in request:
                print(json.dumps({{'jsonrpc':'2.0','id':request['id'],'result':{{}}}}), flush=True)
    """,
    )
    transport = transport_mod.McpStdioTransport(server=server, workspace_root=tmp_path)
    event = threading.Event()
    errors = []

    def request():
        try:
            with mcp_cancellation_scope(EventCancellationToken(event)):
                transport.request(method="tools/call", params={"name": "wait"}, timeout_s=30)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=request)
    try:
        worker.start()
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        event.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], CooperativeCancellationError)
        assert transport.request(method="ping", params={}, timeout_s=3).result == {}
        assert cancelled.exists()
        assert not transport.closed
    finally:
        event.set()
        transport.close()
        worker.join(timeout=2)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.parametrize("mixed_batch", [False, True])
def test_agent_stop_reaches_stdio_tool_and_next_turn_recovers(tmp_path, monkeypatch, mixed_batch):
    from alysis_code.agent_loop import create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.types import LLMResponse, ToolCall

    started = tmp_path / "started"
    cancelled = tmp_path / "cancelled"
    _resolved_stdio_script_server(
        tmp_path,
        monkeypatch,
        f"""
        import json, sys
        from pathlib import Path
        for line in sys.stdin:
            request = json.loads(line)
            method = request['method']
            if method == 'notifications/cancelled':
                Path({str(cancelled)!r}).touch()
                continue
            if 'id' not in request:
                continue
            result = {{}}
            if method == 'initialize':
                result = {{'protocolVersion':'2024-11-05','capabilities':{{'tools':{{}}}},
                    'serverInfo':{{'name':'cancellation-fixture','version':'1'}}}}
            elif method == 'tools/list':
                result = {{'tools':[{{'name':'wait','description':'Wait for an external event.',
                    'inputSchema':{{'type':'object','properties':{{}}}}}}]}}
            elif method == 'tools/call':
                if not Path({str(cancelled)!r}).exists():
                    Path({str(started)!r}).touch()
                    continue
                result = {{'content':[{{'type':'text','text':'Recovered'}}]}}
            print(json.dumps({{'jsonrpc':'2.0','id':request['id'],'result':result}}), flush=True)
    """,
        trust="explicit",
        call_timeout_s=5,
    )
    cfg = AppConfig(model="mcp-cancellation-test", stream=False, routing_mode="code_only")
    cfg.web_search_mode = "off"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="fullaccess",
        yes=False,
        max_steps=4,
        no_log=False,
        api_key_override="test",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        enable_compaction=False,
    )

    class Client:
        model = cfg.model
        temperature = 0.2
        calls = 0

        def chat(self, *, messages, **kwargs):
            pending = set()
            for message in messages:
                if message.get("role") == "tool":
                    pending.discard(message.get("tool_call_id"))
                else:
                    assert not pending, f"Provider received unanswered tool calls: {pending}"
                    pending.update(call["id"] for call in message.get("tool_calls", []))
            assert not pending, f"Provider received unanswered tool calls: {pending}"
            self.calls += 1
            if self.calls == 1:
                calls = [ToolCall(id="wait", name="mcp__alpha__wait", arguments={})]
                if mixed_batch:
                    calls.insert(0, ToolCall(id="before", name="capability_status", arguments={}))
                    calls.append(ToolCall(id="after", name="capability_status", arguments={}))
                return LLMResponse(
                    content="",
                    tool_calls=calls,
                    raw={},
                )
            return LLMResponse(content="Recovered.", tool_calls=[], raw={})

    session.client = Client()
    event = threading.Event()
    errors = []

    def run():
        try:
            session.run_turn(
                "Use the MCP wait tool.", cancellation_token=EventCancellationToken(event)
            )
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists(), errors
        event.set()
        worker.join(timeout=2)
        assert not worker.is_alive(), "Stop did not reach the pending MCP request"
        assert len(errors) == 1 and isinstance(errors[0], CooperativeCancellationError)
        results = [message for message in session.messages if message.get("role") == "tool"]
        interrupted = [
            message
            for message in results
            if json.loads(message["content"]).get("status") == "interrupted"
        ]
        assert {message["tool_call_id"] for message in interrupted} == (
            {"wait", "after"} if mixed_batch else {"wait"}
        )
        if mixed_batch:
            assert sum(message["tool_call_id"] == "before" for message in results) == 1
        assert "tool_call_interrupted" in session.store.path.read_text()
        from alysis_code.mcp.cancellation import complete_interrupted_mcp_batch

        assert (
            complete_interrupted_mcp_batch(session.messages, mcp_tool_names={"mcp__alpha__wait"})
            == []
        )
        session.client = Client()
        assert session.run_turn("Use the MCP wait tool again.") == 0
        assert cancelled.exists()
        assert session.client.calls == 2
    finally:
        event.set()
        worker.join(timeout=6)
        session.close()


def test_interrupted_mcp_batch_cleanup_does_not_rewrite_previous_turns_or_other_tools():
    from copy import deepcopy

    from alysis_code.mcp.cancellation import complete_interrupted_mcp_batch

    previous = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "old",
                "function": {"name": "mcp__alpha__wait", "arguments": "{}"},
            }
        ],
    }
    for tail in (
        [{"role": "user", "content": "A new request"}],
        [
            {"role": "user", "content": "A new request"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "other",
                        "function": {"name": "capability_status", "arguments": "{}"},
                    }
                ],
            },
        ],
    ):
        messages = [deepcopy(previous), *tail]
        before = deepcopy(messages)
        assert complete_interrupted_mcp_batch(messages, mcp_tool_names={"mcp__alpha__wait"}) == []
        assert messages == before


def _resolved_stdio_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_payload: dict,
    **server_overrides: object,
):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    fixture_config_path = tmp_path / "fixture-server.json"
    _write_json(fixture_config_path, fixture_payload)

    server_payload = {
        "transport": "stdio",
        "command": sys.executable,
        # Timing-sensitive transport tests should measure MCP behavior, not whatever
        # `site` / `sitecustomize` / ambient startup plugins add to cold interpreter
        # launch on the host. Run the fixture with `-S` so explicit test delays, not
        # Python startup noise, control startup-timeout regressions.
        "args": ["-S", os.fspath(_FIXTURE_SERVER)],
        "env": {
            "ALYSIS_TEST_MCP_CONFIG": os.fspath(fixture_config_path),
        },
    }
    server_payload.update(server_overrides)
    _write_json(user_mcp_config_path(), {"servers": {"alpha": server_payload}})
    resolved = load_resolved_mcp_config(workspace_root=tmp_path)
    assert len(resolved.servers) == 1
    return resolved.servers[0]


def _resolved_stdio_script_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    **server_overrides: object,
):
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    script_path = tmp_path / "stdio-script-server.py"
    script_path.write_text(textwrap.dedent(script), encoding="utf-8")

    server_payload = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-S", os.fspath(script_path)],
    }
    server_payload.update(server_overrides)
    _write_json(user_mcp_config_path(), {"servers": {"alpha": server_payload}})
    resolved = load_resolved_mcp_config(workspace_root=tmp_path)
    assert len(resolved.servers) == 1
    return resolved.servers[0]


def _assert_unsupported_request_after_response(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_payload: dict,
    call_mode: str,
) -> None:
    for _ in range(_REPEAT_TRANSPORT_RACE_CASES):
        server = _resolved_stdio_server(tmp_path, monkeypatch, fixture_payload)
        client = McpStdioClient(server=server, workspace_root=tmp_path)
        try:
            with pytest.raises(McpStdioTransportProtocolError) as exc_info:
                if call_mode == "list":
                    client.list_tools()
                else:
                    client.list_tools()
                    client.call_tool(tool_name="alpha-tool", arguments={})
            message = str(exc_info.value)
            assert "server-initiated request" in message
            assert "roots/list" in message
        finally:
            client.close()


def _assert_tools_list_changed_after_response(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_payload: dict,
    call_mode: str,
) -> None:
    for _ in range(_REPEAT_TRANSPORT_RACE_CASES):
        server = _resolved_stdio_server(tmp_path, monkeypatch, fixture_payload)
        client = McpStdioClient(server=server, workspace_root=tmp_path)
        try:
            tools = client.list_tools()
            assert [tool.name for tool in tools] == ["alpha-tool"]
            if call_mode == "call":
                result = client.call_tool(tool_name="alpha-tool", arguments={})
                assert result.content_summary == "text(2 chars)"
            assert client.tools_list_changed is True
        finally:
            client.close()


def _assert_initialized_notification_effect(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_payload: dict,
    expect_error: bool,
) -> None:
    for _ in range(_REPEAT_TRANSPORT_RACE_CASES):
        server = _resolved_stdio_server(tmp_path, monkeypatch, fixture_payload)
        client = McpStdioClient(server=server, workspace_root=tmp_path)
        try:
            if expect_error:
                with pytest.raises(McpStdioTransportProtocolError) as exc_info:
                    client.ensure_initialized()
                message = str(exc_info.value)
                assert "server-initiated request" in message
                assert "roots/list" in message
            else:
                client.ensure_initialized()
                assert client.tools_list_changed is True
        finally:
            client.close()


def _wait_for_recorded_client_responses(path: Path) -> list[dict[str, object]]:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if path.exists():
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                return [json.loads(line) for line in lines]
        time.sleep(0.02)
    return []


def _wait_for_recorded_client_requests(path: Path) -> list[dict[str, object]]:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if path.exists():
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                return [json.loads(line) for line in lines]
        time.sleep(0.02)
    return []


def test_stdio_transport_env_policy_is_conservative_and_overlays_config() -> None:
    env = build_stdio_subprocess_env(
        overlay_env={"PATH": "/custom/bin", "API_TOKEN": "secret"},
        host_env={
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "TMPDIR": "/tmp",
            "SECRET": "hidden",
        },
    )

    assert env["PATH"] == "/custom/bin"
    assert env["HOME"] == "/home/tester"
    assert env["TMPDIR"] == "/tmp"
    assert env["API_TOKEN"] == "secret"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "SECRET" not in env


def test_stdio_transport_env_forwards_proxy_and_ca_bundle_plumbing() -> None:
    # A server behind a corporate proxy or a TLS-inspecting gateway cannot reach anything
    # without these; they are forwarded from the host while everything else stays out.
    env = build_stdio_subprocess_env(
        overlay_env={},
        host_env={
            "PATH": "/usr/bin",
            "HTTPS_PROXY": "http://proxy.corp.example:3128",
            "no_proxy": "localhost,.corp.example",
            "SSL_CERT_FILE": "/cache/ca-bundle-abc.pem",
            "NODE_EXTRA_CA_CERTS": "/etc/pki/corp-ca.pem",
            "OPENAI_API_KEY": "sk-hidden",
        },
    )

    assert env["HTTPS_PROXY"] == "http://proxy.corp.example:3128"
    assert env["no_proxy"] == "localhost,.corp.example"
    assert env["SSL_CERT_FILE"] == "/cache/ca-bundle-abc.pem"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/pki/corp-ca.pem"
    assert "OPENAI_API_KEY" not in env


def test_stdio_client_supports_initialize_paginated_list_and_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "description": "Alpha tool",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ],
                [
                    {
                        "name": "beta-tool",
                        "description": "Beta tool",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    }
                ],
            ],
            "tool_call_results": {
                "beta-tool": {
                    "isError": False,
                    "structuredContent": {"status": "ok"},
                    "content": [{"type": "text", "text": "hello from beta"}],
                }
            },
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool", "beta-tool"]
        assert client.server_capabilities == {"tools": {}}

        result = client.call_tool(tool_name="beta-tool", arguments={"name": "Apollo"})
        assert result.is_error is False
        assert result.structured_content == {"status": "ok"}
        assert result.extracted_text == "hello from beta"
        assert result.content_summary == "text(15 chars)"
        assert result.content[0]["type"] == "text"
    finally:
        client.close()


def test_stdio_client_captures_bounded_stderr_tail_on_protocol_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "stderr_lines": [f"err-{index}" for index in range(120)],
            "malformed_stdout_line_on_start": True,
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        with pytest.raises(McpStdioTransportProtocolError) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "stderr tail" in message
        assert "err-119" in message
        assert "err-0" not in message
    finally:
        client.close()


def test_stdio_client_uses_latest_stabilized_stderr_tail_for_fatal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(_REPEAT_FATAL_STDERR_CASES):
        server = _resolved_stdio_server(
            tmp_path,
            monkeypatch,
            {
                "stderr_lines_before_malformed_stdout": ["early-0"],
                "malformed_stdout_line_on_start": True,
                "stderr_lines_after_malformed_stdout": [f"late-{index}" for index in range(120)],
            },
        )
        client = McpStdioClient(server=server, workspace_root=tmp_path)
        try:
            with pytest.raises(McpStdioTransportProtocolError) as exc_info:
                client.list_tools()
            message = str(exc_info.value)
            assert message.count("stderr tail:\n") == 1
            assert "late-119" in message
            assert "late-0" not in message
            assert "early-0" not in message
        finally:
            client.close()


def test_stdio_client_rejects_invalid_utf8_stdout_protocol_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_script_server(
        tmp_path,
        monkeypatch,
        r"""
        import json
        import sys

        def write_response(payload):
            sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        for raw_line in sys.stdin:
            payload = json.loads(raw_line)
            method = payload.get("method")
            if method == "initialize":
                write_response({
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "invalid-utf8", "version": "1"},
                    },
                })
            elif method == "tools/list":
                request_id = json.dumps(payload.get("id")).encode("ascii")
                sys.stderr.buffer.write(b"diagnostic-stderr-\xff\n")
                sys.stderr.buffer.flush()
                sys.stdout.buffer.write(
                    b'{"jsonrpc":"2.0","id":' + request_id + b',"result":\xff}\n'
                )
                sys.stdout.buffer.flush()
        """,
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        with pytest.raises(McpStdioTransportProtocolError) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "received invalid UTF-8 on stdio stdout" in message
        assert "\\xff" not in message
        assert "\ufffd" not in message.split("stderr tail:", 1)[0]
        assert "diagnostic-stderr-\ufffd" in message
    finally:
        client.close()


def test_stdio_client_rejects_trailing_invalid_utf8_stdout_before_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_script_server(
        tmp_path,
        monkeypatch,
        r"""
        import json
        import sys

        def write_response(payload):
            sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        for raw_line in sys.stdin:
            payload = json.loads(raw_line)
            method = payload.get("method")
            if method == "initialize":
                write_response({
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "trailing-invalid-utf8", "version": "1"},
                    },
                })
            elif method == "tools/list":
                request_id = json.dumps(payload.get("id")).encode("ascii")
                sys.stdout.buffer.write(
                    b'{"jsonrpc":"2.0","id":' + request_id + b',"result":\xff'
                )
                sys.stdout.buffer.flush()
                raise SystemExit(0)
        """,
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        with pytest.raises(McpStdioTransportProtocolError) as exc_info:
            client.list_tools()
        message = str(exc_info.value)
        assert "received invalid UTF-8 on stdio stdout" in message
        assert "\\xff" not in message
        assert "\ufffd" not in message
    finally:
        client.close()


def test_stdio_client_allows_lossy_invalid_utf8_stderr_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_script_server(
        tmp_path,
        monkeypatch,
        r"""
        import json
        import sys

        def write_response(payload):
            sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        sys.stderr.buffer.write(b"bad-stderr-\xff\n")
        sys.stderr.buffer.flush()

        for raw_line in sys.stdin:
            payload = json.loads(raw_line)
            method = payload.get("method")
            if method == "initialize":
                write_response({
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "lossy-stderr", "version": "1"},
                    },
                })
            elif method == "tools/list":
                write_response({
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"tools": []},
                })
        """,
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        assert client.list_tools() == ()
        assert "bad-stderr-\ufffd" in client.transport.stderr_tail()
    finally:
        client.close()


def test_stdio_client_fails_promptly_when_subprocess_crashes_mid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_script_server(
        tmp_path,
        monkeypatch,
        r"""
        import json
        import os
        import sys

        def write_response(payload):
            sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        for raw_line in sys.stdin:
            payload = json.loads(raw_line)
            method = payload.get("method")
            if method == "initialize":
                write_response({
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "crash-mid-call", "version": "1"},
                    },
                })
            elif method == "tools/list":
                os._exit(7)
        """,
        startup_timeout_s=2.0,
        call_timeout_s=2.0,
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    process = None
    try:
        client.ensure_initialized()
        start = time.monotonic()
        with pytest.raises(McpStdioTransportError) as exc_info:
            client.list_tools()
        elapsed = time.monotonic() - start
        process = client.transport._process
        assert elapsed < 1.5
        assert isinstance(exc_info.value, McpProcessError)
        message = str(exc_info.value)
        assert "stdio process exited" in message or "failed to write request" in message
    finally:
        client.close()
    if process is not None:
        assert process.poll() is not None


def test_stdio_client_handles_roots_list_and_keeps_active_request_successful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response_log = tmp_path / "roots-responses.jsonl"
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "record_client_responses_path": os.fspath(response_log),
            "post_tools_list_response_events": [{"kind": "unexpected_request"}],
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "description": "Alpha tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        roots_mode="workspace",
    )
    client = McpStdioClient(
        server=server,
        workspace_root=tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
        responses = _wait_for_recorded_client_responses(response_log)
        assert len(responses) == 1
        assert responses[0]["id"] == "server-request-1"
        assert responses[0]["result"] == {
            "roots": [
                {
                    "uri": tmp_path.resolve().as_uri(),
                    "name": tmp_path.name,
                }
            ]
        }
    finally:
        client.close()


def test_stdio_client_still_rejects_non_roots_server_requests_when_roots_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "unexpected_request_method": "sampling/createMessage",
            "unexpected_request_after_list_response": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "description": "Alpha tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        roots_mode="workspace",
    )
    client = McpStdioClient(
        server=server,
        workspace_root=tmp_path,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    try:
        with pytest.raises(McpStdioTransportProtocolError) as exc_info:
            client.list_tools()
        assert "sampling/createMessage" in str(exc_info.value)
    finally:
        client.close()


def test_stdio_client_enforces_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "method_delays": {"initialize": 0.25},
        },
        startup_timeout_s=0.05,
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        with pytest.raises(McpStdioTransportTimeoutError):
            client.list_tools()
    finally:
        client.close()


def test_stdio_client_allows_immediate_response_with_sub_window_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _LOW_CALL_TIMEOUT_S < transport_mod._STDOUT_RESPONSE_QUIESCENCE_WAIT_S
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        startup_timeout_s=_WARM_STARTUP_TIMEOUT_S,
        call_timeout_s=_LOW_CALL_TIMEOUT_S,
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        # This regression is about a warmed, immediate request response staying
        # valid below the reader follow-up window; it should not depend on cold
        # subprocess startup speed for `initialize`.
        client.ensure_initialized()
        tools = client.list_tools()
        assert [tool.name for tool in tools] == ["alpha-tool"]
    finally:
        client.close()


def test_stdio_transport_prioritizes_follow_up_timeout_over_late_quiet_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
    )
    transport = transport_mod.McpStdioTransport(
        server=server,
        workspace_root=tmp_path,
    )
    request_id = "req-1"
    response_queue: queue.Queue[object] = queue.Queue()
    response = JsonRpcResponse(id=request_id, result={"ok": True})
    now = transport_mod.time.monotonic()
    with transport._pending_lock:
        transport._pending[request_id] = transport_mod._PendingRequest(
            response_queue=response_queue,
            follow_up_budget_s=transport_mod._STDOUT_RESPONSE_FOLLOW_UP_BUDGET_S,
        )
        transport._set_request_state_locked(
            request_id, transport_mod._REQUEST_STATE_AWAITING_FOLLOW_UP
        )
    with transport._stdout_state_condition:
        transport._stdout_last_activity_monotonic = now - (
            transport_mod._STDOUT_RESPONSE_QUIESCENCE_WAIT_S + 0.05
        )
    deferred_responses = {
        request_id: transport_mod._DeferredResponse(
            response=response,
            follow_up_deadline_monotonic=now - 0.01,
        )
    }

    transport._flush_deferred_responses(deferred_responses)

    queued = response_queue.get_nowait()
    assert isinstance(queued, transport_mod._DeferredResponseFollowUpTimeout)
    assert deferred_responses == {}
    assert request_id not in transport._pending
    assert transport._request_states[request_id] == transport_mod._REQUEST_STATE_FOLLOW_UP_TIMED_OUT
    assert transport._fatal_state is None


def test_stdio_transport_bounds_retired_request_state_tracking_and_discards_recent_late_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport_mod, "_RETIRED_REQUEST_STATE_RETENTION_LIMIT", 2)
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
    )
    transport = transport_mod.McpStdioTransport(
        server=server,
        workspace_root=tmp_path,
    )
    with transport._pending_lock:
        transport._set_request_state_locked(
            "req-1", transport_mod._REQUEST_STATE_RESPONSE_TIMED_OUT
        )
        transport._set_request_state_locked(
            "req-2", transport_mod._REQUEST_STATE_FOLLOW_UP_TIMED_OUT
        )
        transport._set_request_state_locked("req-3", transport_mod._REQUEST_STATE_CANCELLED)

    assert "req-1" not in transport._request_states
    assert transport._request_states["req-2"] == transport_mod._REQUEST_STATE_FOLLOW_UP_TIMED_OUT
    assert transport._request_states["req-3"] == transport_mod._REQUEST_STATE_CANCELLED
    assert tuple(transport._retired_request_state_ids) == ("req-2", "req-3")

    transport._handle_response_message(
        JsonRpcResponse(id="req-3", result={"ok": True}),
        deferred_responses={},
    )

    assert transport._fatal_state is None


def test_stdio_client_rejects_unsupported_server_initiated_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_initialized_notification_effect(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "unexpected_request_after_initialized": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        expect_error=True,
    )


def test_stdio_client_observes_delayed_post_initialized_notification_within_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_initialized_notification_effect(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "delay_after_initialized_output_s": _DELAYED_INITIALIZED_OUTPUT_S,
            "send_tools_list_changed_after_initialized": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        expect_error=False,
    )


def test_stdio_client_rejects_delayed_server_request_after_initialized_within_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_initialized_notification_effect(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "delay_after_initialized_output_s": _DELAYED_INITIALIZED_OUTPUT_S,
            "unexpected_request_after_initialized": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        expect_error=True,
    )


def test_stdio_client_rejects_server_request_after_tools_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_unsupported_request_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "unexpected_request_after_list_response": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        call_mode="list",
    )


def test_stdio_client_rejects_server_request_after_tools_list_response_single_write_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_unsupported_request_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "burst_unexpected_request_after_list_response": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        call_mode="list",
    )


def test_stdio_client_rejects_chained_server_request_after_tools_list_response_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_unsupported_request_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "post_tools_list_response_events": [
                {
                    "delay_s": _CHAINED_FOLLOW_UP_NOTIFICATION_DELAY_S,
                    "kind": "tools_list_changed",
                },
                {
                    "delay_s": _CHAINED_FOLLOW_UP_UNSUPPORTED_REQUEST_DELAY_S,
                    "kind": "unexpected_request",
                },
            ],
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        call_mode="list",
    )


def test_stdio_client_rejects_server_request_after_tools_call_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_unsupported_request_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "unexpected_request_after_call_response": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "tool_call_results": {
                "alpha-tool": {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                }
            },
        },
        call_mode="call",
    )


def test_stdio_client_rejects_chained_server_request_after_tools_call_response_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_unsupported_request_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "post_tools_call_response_events": [
                {
                    "delay_s": _CHAINED_FOLLOW_UP_NOTIFICATION_DELAY_S,
                    "kind": "tools_list_changed",
                },
                {
                    "delay_s": _CHAINED_FOLLOW_UP_UNSUPPORTED_REQUEST_DELAY_S,
                    "kind": "unexpected_request",
                },
            ],
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "tool_call_results": {
                "alpha-tool": {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                }
            },
        },
        call_mode="call",
    )


def test_stdio_client_rejects_server_request_after_tools_call_response_single_write_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_unsupported_request_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "burst_unexpected_request_after_call_response": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "tool_call_results": {
                "alpha-tool": {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                }
            },
        },
        call_mode="call",
    )


def test_stdio_client_records_chained_tools_list_changed_follow_up_after_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_tools_list_changed_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "post_tools_list_response_events": [
                {
                    "delay_s": _CHAINED_FOLLOW_UP_NOTIFICATION_DELAY_S,
                    "kind": "tools_list_changed",
                }
            ],
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        call_mode="list",
    )


def test_stdio_client_records_tools_list_changed_notification_after_list_response_single_write_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_tools_list_changed_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "burst_tools_list_changed_after_list": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        call_mode="list",
    )


def test_stdio_client_records_tools_list_changed_notification_after_call_response_single_write_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_tools_list_changed_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "burst_tools_list_changed_after_call": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "tool_call_results": {
                "alpha-tool": {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                }
            },
        },
        call_mode="call",
    )


def test_stdio_client_rejects_duplicate_response_for_tools_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(_REPEAT_TRANSPORT_RACE_CASES):
        server = _resolved_stdio_server(
            tmp_path,
            monkeypatch,
            {
                "duplicate_tools_list_response": True,
                "tools_pages": [
                    [
                        {
                            "name": "alpha-tool",
                            "inputSchema": {"type": "object", "properties": {}, "required": []},
                        }
                    ]
                ],
            },
        )
        client = McpStdioClient(server=server, workspace_root=tmp_path)
        try:
            with pytest.raises(McpStdioTransportProtocolError) as exc_info:
                client.list_tools()
            assert "duplicate response" in str(exc_info.value)
        finally:
            client.close()


def test_stdio_client_records_tools_list_changed_notification_after_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_tools_list_changed_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "send_tools_list_changed_after_list": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        call_mode="list",
    )


def test_stdio_client_records_tools_list_changed_notification_after_call_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_tools_list_changed_after_response(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "send_tools_list_changed_after_call": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "tool_call_results": {
                "alpha-tool": {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                }
            },
        },
        call_mode="call",
    )


def test_stdio_client_records_tools_list_changed_notification_after_initialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_initialized_notification_effect(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fixture_payload={
            "send_tools_list_changed_after_initialized": True,
            "tools_pages": [
                [
                    {
                        "name": "alpha-tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
        },
        expect_error=False,
    )


def test_stdio_client_records_resources_list_changed_notification_after_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "capabilities": {"resources": {}},
            "resources_pages": [[{"uri": "file:///alpha.txt", "name": "alpha"}]],
            "send_resources_list_changed_after_list": True,
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        resources = client.list_resources()
        assert [resource.uri for resource in resources] == ["file:///alpha.txt"]
        assert client.resources_list_changed is True
        assert client.prompts_list_changed is False
    finally:
        client.close()


def test_stdio_client_records_prompts_list_changed_notification_after_list_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "capabilities": {"prompts": {}},
            "prompts_pages": [[{"name": "review_pr"}]],
            "send_prompts_list_changed_after_list": True,
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        prompts = client.list_prompts()
        assert [prompt.name for prompt in prompts] == ["review_pr"]
        assert client.prompts_list_changed is True
        assert client.resources_list_changed is False
    finally:
        client.close()


def test_stdio_client_supports_paginated_resources_list_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "capabilities": {"resources": {}},
            "resources_pages": [
                [
                    {
                        "uri": "file:///alpha.txt",
                        "name": "alpha",
                        "description": "Alpha resource",
                        "mimeType": "text/plain",
                        "size": 11,
                    }
                ],
                [
                    {
                        "uri": "https://example.com/spec.json",
                        "name": "spec",
                        "mimeType": "application/json",
                    }
                ],
            ],
            "resource_read_results": {
                "https://example.com/spec.json": {
                    "contents": [
                        {
                            "uri": "https://example.com/spec.json",
                            "mimeType": "application/json",
                            "text": '{"ok":true}',
                        }
                    ]
                }
            },
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        resources = client.list_resources()
        allowed_uris = frozenset(resource.uri for resource in resources)
        assert [resource.uri for resource in resources] == [
            "file:///alpha.txt",
            "https://example.com/spec.json",
        ]
        assert client.server_capabilities == {"resources": {}}

        read_result = client.read_resource(
            resource_uri="https://example.com/spec.json",
            allowed_uris=allowed_uris,
        )
        assert read_result.text == '{"ok":true}'
        assert read_result.mime_type == "application/json"
    finally:
        client.close()


def test_stdio_client_blocks_unsnapshotted_resource_reads_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_log = tmp_path / "resource-requests.jsonl"
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "capabilities": {"resources": {}},
            "resources_pages": [
                [
                    {
                        "uri": "file:///alpha.txt",
                        "name": "alpha",
                    }
                ]
            ],
            "record_client_requests_path": os.fspath(request_log),
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        client.ensure_initialized()
        with pytest.raises(McpClientError) as exc_info:
            client.read_resource(resource_uri="file:///alpha.txt")
        assert "requires a frozen resource snapshot allowlist" in str(exc_info.value)

        with pytest.raises(McpClientError) as exc_info:
            client.read_resource(
                resource_uri="file:///missing.txt",
                allowed_uris=frozenset({"file:///alpha.txt"}),
            )
        assert "was not snapshotted" in str(exc_info.value)

        requests = _wait_for_recorded_client_requests(request_log)
        methods = [str(item.get("method") or "") for item in requests]
        assert methods.count("resources/read") == 0
        assert "initialize" in methods
    finally:
        client.close()


def test_stdio_client_supports_paginated_prompts_list_and_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "capabilities": {"prompts": {}},
            "prompts_pages": [
                [
                    {
                        "name": "review_pr",
                        "title": "Review Pull Request",
                        "description": "Review helper",
                        "arguments": [{"name": "repo", "required": True}],
                    }
                ],
                [
                    {
                        "name": "draft_issue",
                        "description": "Draft issue helper",
                    }
                ],
            ],
            "prompt_get_results": {
                "review_pr": {
                    "name": "review_pr",
                    "description": "Review helper",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": "Review the repo prompt.",
                            },
                        }
                    ],
                }
            },
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        prompts = client.list_prompts()
        assert [prompt.name for prompt in prompts] == ["review_pr", "draft_issue"]
        assert prompts[0].arguments[0].name == "repo"
        assert client.server_capabilities == {"prompts": {}}

        prompt_result = client.get_prompt(
            name="review_pr",
            arguments={"repo": "owner/alysis"},
        )
        assert prompt_result.description == "Review helper"
        assert prompt_result.messages[0].role == "user"
        assert prompt_result.text == "Review the repo prompt."
        assert "user: text(23 chars)" in prompt_result.content_summary
    finally:
        client.close()


def test_stdio_client_rejects_malformed_prompt_get_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {
            "capabilities": {"prompts": {}},
            "prompts_pages": [[{"name": "review_pr"}]],
            "prompt_get_results": {
                "review_pr": {
                    "name": "review_pr",
                    "messages": "broken",
                }
            },
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        with pytest.raises(McpClientError) as exc_info:
            client.get_prompt(name="review_pr")
        assert "prompts/get result.messages must be an array" in str(exc_info.value)
    finally:
        client.close()


def test_resolve_windows_command_finds_script_launchers_on_the_child_path(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "npx.cmd"
    launcher.write_text("@echo off\n", encoding="utf-8")
    # Windows environment names are case-insensitive; `Path` is how many shells spell it.
    env = {"Path": f'{tmp_path / "empty"}{os.pathsep}"{bin_dir}"', "PATHEXT": ".exe;.cmd"}

    assert resolve_windows_command("npx", env=env) == os.fspath(launcher)
    assert resolve_windows_command("npx.cmd", env=env) == os.fspath(launcher)
    assert resolve_windows_command("uvx", env=env) is None
    assert resolve_windows_command(os.fspath(bin_dir / "npx"), env=env) is None
    assert resolve_windows_command("", env=env) is None


def test_windows_batch_command_line_quotes_cmd_metacharacters() -> None:
    command_line = windows_batch_command_line(
        r"C:\Program Files\nodejs\npx.cmd",
        ["-y", "@scope/server", "https://host/mcp?a=1&b=2", "two words", "C:\\dir name\\", ""],
    )

    assert command_line == (
        r'"C:\Program Files\nodejs\npx.cmd" -y @scope/server "https://host/mcp?a=1&b=2" '
        r'"two words" "C:\dir name\\" ""'
    )


@pytest.mark.parametrize("argument", ["50%", 'say "hi"', "two\nlines"])
def test_windows_batch_command_line_refuses_what_quoting_cannot_protect(argument: str) -> None:
    with pytest.raises(ValueError, match="argument 1 contains"):
        windows_batch_command_line(r"C:\tools\launcher.cmd", [argument])


@pytest.mark.skipif(os.name != "nt", reason="Windows command lookup")
def test_stdio_client_starts_a_bare_cmd_launcher_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_record = tmp_path / "argv.json"
    recorder = tmp_path / "record_argv.py"
    recorder.write_text(
        textwrap.dedent(
            f"""\
            import json, os, runpy, sys
            from pathlib import Path
            Path(os.environ["ARGV_RECORD"]).write_text(json.dumps(sys.argv[1:]))
            sys.argv = [{os.fspath(_FIXTURE_SERVER)!r}]
            runpy.run_path(sys.argv[0], run_name="__main__")
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "probe-launcher.cmd").write_bytes(
        f'@echo off\r\n"{sys.executable}" -S "{recorder}" %*\r\n'.encode()
    )
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {"tools_pages": [[{"name": "echo", "inputSchema": {"type": "object"}}]]},
        command="probe-launcher",
        args=["--marker=a&b", "two words"],
        env={
            "ALYSIS_TEST_MCP_CONFIG": os.fspath(tmp_path / "fixture-server.json"),
            "ARGV_RECORD": os.fspath(argv_record),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        assert [tool.name for tool in client.list_tools()] == ["echo"]
    finally:
        client.close()
    assert json.loads(argv_record.read_text(encoding="utf-8")) == ["--marker=a&b", "two words"]


@pytest.mark.skipif(os.name != "nt", reason="Windows command lookup")
def test_stdio_client_reports_an_unsafe_cmd_launcher_argument_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "probe-launcher.cmd").write_bytes(b"@echo off\r\n")
    server = _resolved_stdio_server(
        tmp_path,
        monkeypatch,
        {},
        command="probe-launcher",
        args=["100%"],
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
    )
    client = McpStdioClient(server=server, workspace_root=tmp_path)
    try:
        with pytest.raises(McpProcessError, match="cannot receive safely"):
            client.list_tools()
    finally:
        client.close()
