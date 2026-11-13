from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from websockets.sync.server import ServerConnection, serve

from alysis_code.ide.cdp_websocket_transport import (
    WebSocketCdpTransport,
    WebSocketCdpTransportFactory,
    validate_cdp_websocket_url,
)
from alysis_code.ide.managed_browser import (
    BrowserCancelledError,
    BrowserError,
    BrowserSecurityError,
    BrowserTimeoutError,
)

_CLOSED = object()


def test_global_download_events_do_not_consume_page_events():
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    try:
        download = {
            "method": "Browser.downloadProgress",
            "params": {"guid": "owned", "state": "completed"},
        }
        page = {"method": "Page.loadEventFired", "params": {}, "sessionId": "page"}
        connection.receive_json(page)
        connection.receive_json(download)
        assert transport.drain_browser_events(max_events=10, timeout=1) == [download]
        assert transport.drain_events(session_id="page", max_events=10, timeout=1) == [page]
    finally:
        transport.close(timeout=1)


class FakeWebSocket:
    def __init__(self) -> None:
        self.inbound: queue.Queue[object] = queue.Queue()
        self.outbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self.closed = threading.Event()
        self.close_calls = 0

    def send(self, message: str | bytes) -> None:
        if self.closed.is_set():
            raise ConnectionError("closed")
        self.outbound.put(json.loads(message))

    def recv(self, timeout: float | None = None, *, decode: bool | None = None) -> str | bytes:
        del decode
        try:
            value = self.inbound.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if value is _CLOSED:
            raise ConnectionError("closed")
        assert isinstance(value, str | bytes)
        return value

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()
        self.inbound.put(_CLOSED)

    def receive_json(self, payload: dict[str, Any]) -> None:
        self.inbound.put(json.dumps(payload, separators=(",", ":")))

    def next_command(self, method: str, timeout: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        unmatched: list[dict[str, Any]] = []
        try:
            while time.monotonic() < deadline:
                try:
                    command = self.outbound.get(timeout=min(0.05, deadline - time.monotonic()))
                except queue.Empty:
                    continue
                if command.get("method") == method:
                    return command
                unmatched.append(command)
        finally:
            for command in unmatched:
                self.outbound.put(command)
        raise AssertionError(f"CDP command {method} was not sent")

    def respond(self, command: dict[str, Any], result: dict[str, Any] | None = None) -> None:
        self.receive_json({"id": command["id"], "result": result or {}})


@pytest.mark.parametrize(
    "url",
    (
        "wss://127.0.0.1:9222/devtools/browser/id",
        "ws://localhost:9222/devtools/browser/id",
        "ws://192.168.1.4:9222/devtools/browser/id",
        "ws://127.0.0.1:9222/json/version",
        "ws://user:secret@127.0.0.1:9222/devtools/browser/id",
        "ws://127.0.0.1:9222/devtools/browser/id?token=value",
    ),
)
def test_endpoint_validation_rejects_nonliteral_or_untrusted_urls(url: str) -> None:
    with pytest.raises(BrowserSecurityError):
        validate_cdp_websocket_url(url)


@pytest.mark.parametrize(
    "url",
    (
        "ws://127.0.0.1:9222/devtools/browser/id-1",
        "ws://127.12.34.56:65535/devtools/browser/browser.1",
        "ws://[::1]:9222/devtools/browser/id_1",
    ),
)
def test_endpoint_validation_accepts_literal_loopback_urls(url: str) -> None:
    assert validate_cdp_websocket_url(url) == url


def test_factory_disables_proxy_compression_and_caps_messages() -> None:
    connection = FakeWebSocket()
    captured: dict[str, Any] = {}

    def connector(url: str, **kwargs: Any) -> FakeWebSocket:
        captured["url"] = url
        captured.update(kwargs)
        return connection

    transport = WebSocketCdpTransportFactory(connector=connector).connect(
        "ws://127.0.0.1:9222/devtools/browser/id",
        timeout=0.5,
    )
    try:
        assert captured["proxy"] is None
        assert captured["compression"] is None
        assert captured["max_size"] == 8 * 1024 * 1024
        assert captured["url"].startswith("ws://127.0.0.1:")
    finally:
        transport.close(timeout=0.2)


def test_default_websockets_adapter_round_trips_against_loopback_server() -> None:
    def handler(websocket: ServerConnection) -> None:
        command = json.loads(websocket.recv())
        websocket.send(json.dumps({"id": command["id"], "result": {"ready": True}}))

    server = serve(handler, "127.0.0.1", 0)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    port = server.socket.getsockname()[1]
    transport = WebSocketCdpTransportFactory().connect(
        f"ws://127.0.0.1:{port}/devtools/browser/integration",
        timeout=1,
    )
    try:
        assert transport.call("Browser.getVersion", timeout=1) == {"ready": True}
    finally:
        transport.close(timeout=0.2)
        server.shutdown()
        server_thread.join(timeout=1)


@pytest.mark.parametrize("cancelled", (False, True), ids=("timeout", "cancel"))
def test_factory_interrupts_slow_connect_and_closes_late_connection(cancelled: bool) -> None:
    connection = FakeWebSocket()
    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()

    def connector(url: str, **kwargs: Any) -> FakeWebSocket:
        del url, kwargs
        entered.set()
        release.wait(timeout=1)
        return connection

    if cancelled:
        threading.Thread(
            target=lambda: (entered.wait(timeout=1), cancel.set()),
            daemon=True,
        ).start()
    expected = BrowserCancelledError if cancelled else BrowserTimeoutError
    with pytest.raises(expected):
        WebSocketCdpTransportFactory(connector=connector).connect(
            "ws://127.0.0.1:9222/devtools/browser/id",
            timeout=0.05 if not cancelled else 1,
            cancel=cancel.is_set if cancelled else None,
        )
    release.set()
    assert connection.closed.wait(timeout=1)


def test_concurrent_calls_correlate_out_of_order_responses() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    results: dict[str, Any] = {}

    def invoke(name: str) -> None:
        results[name] = transport.call(name, timeout=1.0)

    first = threading.Thread(target=invoke, args=("Runtime.evaluate",))
    second = threading.Thread(target=invoke, args=("DOM.getDocument",))
    first.start()
    second.start()
    commands = [connection.outbound.get(timeout=1), connection.outbound.get(timeout=1)]
    for command in reversed(commands):
        connection.respond(command, {"method": command["method"]})
    first.join(timeout=1)
    second.join(timeout=1)
    try:
        assert results["Runtime.evaluate"] == {"method": "Runtime.evaluate"}
        assert results["DOM.getDocument"] == {"method": "DOM.getDocument"}
    finally:
        transport.close(timeout=0.2)


def test_unknown_and_duplicate_response_ids_fail_transport() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    connection.receive_json({"id": 999, "result": {}})
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="unknown or duplicated"):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_duplicate_response_id_after_completed_call_fails_transport() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    result: dict[str, Any] = {}

    def call() -> None:
        result.update(transport.call("Runtime.evaluate", timeout=1))

    thread = threading.Thread(target=call)
    thread.start()
    command = connection.outbound.get(timeout=1)
    connection.respond(command, {"value": 1})
    thread.join(timeout=1)
    assert result == {"value": 1}
    connection.respond(command, {"value": 2})
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="unknown or duplicated"):
        transport.call("DOM.getDocument", timeout=0.2)


@pytest.mark.parametrize(
    "payload",
    (
        b'{"id":1,"id":1,"result":{}}',
        b'{"method":"Runtime.event","params":{"value":NaN}}',
        b"[]",
        b"x" * (8 * 1024 * 1024 + 1),
    ),
    ids=("duplicate-key", "non-finite", "non-object", "oversized"),
)
def test_malformed_or_oversized_frames_fail_closed(payload: bytes) -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    connection.inbound.put(payload)
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_timeout_interrupts_all_pending_calls_and_closes_connection() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    started = time.monotonic()
    with pytest.raises(BrowserTimeoutError):
        transport.call("Runtime.evaluate", timeout=0.05)
    assert time.monotonic() - started < 0.5
    assert connection.closed.wait(timeout=1)
    assert not transport.healthy
    with pytest.raises(BrowserTimeoutError):
        transport.call("DOM.getDocument", timeout=0.2)


def test_timeout_interrupts_a_blocked_socket_write() -> None:
    class BlockingSendWebSocket(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.send_entered = threading.Event()
            self.send_release = threading.Event()

        def send(self, message: str | bytes) -> None:
            self.send_entered.set()
            self.send_release.wait(timeout=1)
            if not self.closed.is_set():
                super().send(message)

        def close(self) -> None:
            self.send_release.set()
            super().close()

    connection = BlockingSendWebSocket()
    transport = WebSocketCdpTransport(connection)
    started = time.monotonic()
    with pytest.raises(BrowserTimeoutError):
        transport.call("Runtime.evaluate", timeout=0.05)
    assert connection.send_entered.is_set()
    assert time.monotonic() - started < 0.5
    assert connection.closed.wait(timeout=1)


def test_cancellation_interrupts_pending_call() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    cancelled = threading.Event()

    def cancel() -> bool:
        return cancelled.is_set()

    def trigger() -> None:
        connection.outbound.get(timeout=1)
        cancelled.set()

    threading.Thread(target=trigger, daemon=True).start()
    with pytest.raises(BrowserCancelledError):
        transport.call("Runtime.evaluate", timeout=1, cancel=cancel)
    assert connection.closed.wait(timeout=1)
    assert not transport.healthy


def test_cancellation_before_dispatch_preserves_healthy_connection() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    try:
        assert transport.healthy
        with pytest.raises(BrowserCancelledError):
            transport.call("Runtime.evaluate", timeout=1, cancel=lambda: True)
        assert transport.healthy
        assert connection.outbound.empty()
    finally:
        transport.close(timeout=0.2)
    assert not transport.healthy


def test_event_queue_is_bounded_and_reports_drops() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection, max_event_count=2)
    for index in range(3):
        connection.receive_json(
            {
                "method": "Runtime.consoleAPICalled",
                "params": {"index": index},
                "sessionId": "session-1",
            }
        )
    deadline = time.monotonic() + 1
    while transport.dropped_event_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    events = transport.drain_events(session_id="session-1", max_events=10, timeout=0.2)
    try:
        assert events[0] == {
            "method": "Alysis.eventsDropped",
            "params": {"count": 1},
            "sessionId": "session-1",
        }
        assert [event["params"]["index"] for event in events[1:]] == [1, 2]
    finally:
        transport.close(timeout=0.2)


def _run_guarded_navigation(
    transport: WebSocketCdpTransport,
    authorize: Callable[[str], str],
    outcome: dict[str, Any],
) -> threading.Thread:
    def run() -> None:
        try:
            outcome["result"] = transport.guarded_navigate(
                "https://example.com/",
                session_id="session-1",
                authorize_url=authorize,
                timeout=2,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted by tests
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _pause(
    connection: FakeWebSocket,
    request_id: str,
    url: str,
    *,
    response: bool = False,
    session_id: str = "session-1",
    padding: str | None = None,
) -> None:
    params: dict[str, Any] = {
        "requestId": request_id,
        "request": {"url": url},
    }
    if response:
        params["responseStatusCode"] = 200
    if padding is not None:
        params["padding"] = padding
    connection.receive_json(
        {
            "method": "Fetch.requestPaused",
            "params": params,
            "sessionId": session_id,
        }
    )


def _enable_persistent_guard(connection: FakeWebSocket) -> dict[str, Any]:
    auto_attach = connection.next_command("Target.setAutoAttach")
    assert auto_attach["params"] == {
        "autoAttach": True,
        "waitForDebuggerOnStart": True,
        "flatten": True,
    }
    connection.respond(auto_attach)
    lifecycle = connection.next_command("Page.setLifecycleEventsEnabled")
    assert lifecycle["params"] == {"enabled": True}
    connection.respond(lifecycle)
    enable = connection.next_command("Fetch.enable")
    connection.respond(enable)
    return enable


def _emit_document_load(connection: FakeWebSocket, loader_id: str) -> None:
    connection.receive_json(
        {
            "method": "Page.lifecycleEvent",
            "params": {
                "frameId": "frame-1",
                "loaderId": loader_id,
                "name": "load",
                "timestamp": 1.0,
            },
            "sessionId": "session-1",
        }
    )


def _complete_public_navigation(
    transport: WebSocketCdpTransport,
    connection: FakeWebSocket,
    authorize: Callable[[str], str],
) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, authorize, outcome)
    _enable_persistent_guard(connection)
    navigate = connection.next_command("Page.navigate")
    _pause(connection, "initial-request", "https://example.com/")
    connection.respond(connection.next_command("Fetch.continueRequest"))
    _pause(connection, "initial-response", "https://example.com/", response=True)
    connection.respond(connection.next_command("Fetch.continueResponse"))
    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "loader-1"})
    _emit_document_load(connection, "loader-1")
    thread.join(timeout=1)
    assert thread.is_alive() is False
    assert "error" not in outcome
    return outcome["result"]


def _commit_without_load(connection: FakeWebSocket) -> None:
    _enable_persistent_guard(connection)
    navigate = connection.next_command("Page.navigate")
    _pause(connection, "initial-request", "https://example.com/")
    connection.respond(connection.next_command("Fetch.continueRequest"))
    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "loader-1"})


def test_a_committed_page_whose_load_event_is_late_is_returned_still_loading() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    outcome: dict[str, Any] = {}
    try:
        thread = _run_guarded_navigation(transport, lambda url: url, outcome)
        _commit_without_load(connection)
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert outcome["result"]["loaderId"] == "loader-1"
        assert outcome["result"]["loadEventPending"] is True
        assert transport.healthy and not connection.closed.is_set()
        # The guard stays installed for what the page loads afterwards.
        _pause(connection, "late-request", "https://cdn.example.com/app.js")
        continued = connection.next_command("Fetch.continueRequest")
        assert continued["params"]["requestId"] == "late-request"
        connection.respond(continued)
    finally:
        transport.close(timeout=0.2)


def test_a_ready_page_does_not_wait_for_its_load_event_forever(monkeypatch) -> None:
    from alysis_code.ide import cdp_websocket_transport as module

    monkeypatch.setattr(module, "_LOAD_GRACE_AFTER_READY_SECONDS", 0.2)
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    outcome: dict[str, Any] = {}
    try:
        thread = _run_guarded_navigation(transport, lambda url: url, outcome)
        _commit_without_load(connection)
        started = time.monotonic()
        connection.receive_json(
            {
                "method": "Page.lifecycleEvent",
                "params": {
                    "frameId": "frame-1",
                    "loaderId": "loader-1",
                    "name": "DOMContentLoaded",
                },
                "sessionId": "session-1",
            }
        )
        thread.join(timeout=3)
        assert outcome["result"]["loadEventPending"] is True
        assert time.monotonic() - started < 1.5  # well before the 2 s deadline
        assert transport.healthy
    finally:
        transport.close(timeout=0.2)


def test_a_network_error_page_keeps_the_browser_and_its_guard() -> None:
    from alysis_code.ide.managed_browser import BrowserPageUnreachableError

    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    outcome: dict[str, Any] = {}
    try:
        thread = _run_guarded_navigation(transport, lambda url: url, outcome)
        _enable_persistent_guard(connection)
        navigate = connection.next_command("Page.navigate")
        connection.respond(
            navigate,
            {
                "frameId": "frame-1",
                "loaderId": "l-1",
                "errorText": "net::ERR_TUNNEL_CONNECTION_FAILED",
            },
        )
        thread.join(timeout=2)
        error = outcome.get("error")
        assert isinstance(error, BrowserPageUnreachableError)
        assert error.net_error == "net::ERR_TUNNEL_CONNECTION_FAILED"
        assert "could not be reached (net::ERR_TUNNEL_CONNECTION_FAILED)" in str(error)
        assert transport.healthy and not connection.closed.is_set()
        _pause(connection, "next-request", "https://example.org/")
        continued = connection.next_command("Fetch.continueRequest")
        connection.respond(continued)
    finally:
        transport.close(timeout=0.2)


def test_unreachable_page_never_reflects_page_text() -> None:
    from alysis_code.ide.managed_browser import BrowserPageUnreachableError

    assert BrowserPageUnreachableError("<script>alert(1)</script>").net_error is None
    assert str(BrowserPageUnreachableError(None)) == "This address could not be reached."


@pytest.mark.parametrize("response_stage", [False, True])
def test_a_request_the_page_cancelled_while_paused_does_not_close_the_browser(
    response_stage: bool,
) -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    try:
        _complete_public_navigation(transport, connection, lambda url: url)
        _pause(connection, "cancelled", "https://cdn.example.com/a.js", response=response_stage)
        command = connection.next_command(
            "Fetch.continueResponse" if response_stage else "Fetch.continueRequest"
        )
        connection.receive_json(
            {"id": command["id"], "error": {"code": -32602, "message": "Invalid InterceptionId."}}
        )
        _pause(connection, "next", "https://example.com/b.js")
        continued = connection.next_command("Fetch.continueRequest")
        assert continued["params"]["requestId"] == "next"
        connection.respond(continued)
        assert transport.healthy and not connection.closed.is_set()
    finally:
        transport.close(timeout=0.2)


def test_other_refusals_to_continue_still_fail_closed() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    try:
        _complete_public_navigation(transport, connection, lambda url: url)
        _pause(connection, "odd", "https://cdn.example.com/a.js")
        command = connection.next_command("Fetch.continueRequest")
        connection.receive_json({"id": command["id"], "error": {"code": -32000, "message": "no"}})
        assert connection.closed.wait(timeout=1)
    finally:
        transport.close(timeout=0.2)


def test_a_navigation_that_never_commits_still_fails_closed() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    outcome: dict[str, Any] = {}
    try:
        thread = _run_guarded_navigation(transport, lambda url: url, outcome)
        _enable_persistent_guard(connection)
        connection.next_command("Page.navigate")  # never answered
        thread.join(timeout=4)
        assert isinstance(outcome.get("error"), BrowserTimeoutError)
        assert not transport.healthy
    finally:
        transport.close(timeout=0.2)


def test_dropped_diagnostics_do_not_abort_guarded_navigation() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection, max_event_count=2)
    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, lambda url: url, outcome)
    _enable_persistent_guard(connection)
    navigate = connection.next_command("Page.navigate")
    for index in range(20):
        connection.receive_json(
            {
                "method": "Runtime.consoleAPICalled",
                "params": {"index": index},
                "sessionId": "session-1",
            }
        )
    # Interception must still authorize the request after diagnostics overflow.
    _pause(connection, "top", "https://example.com/")
    connection.respond(connection.next_command("Fetch.continueRequest"))
    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "loader-1"})
    _emit_document_load(connection, "loader-1")
    thread.join(timeout=1)
    try:
        assert not thread.is_alive()
        assert outcome == {"result": {"frameId": "frame-1", "loaderId": "loader-1"}}
        assert transport.dropped_event_count > 0
        assert transport.healthy
    finally:
        transport.close(timeout=0.2)


def test_dropped_lifecycle_event_still_aborts_guarded_navigation() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection, max_event_count=1)
    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, lambda url: url, outcome)
    _enable_persistent_guard(connection)
    connection.next_command("Page.navigate")
    # Hold the consumer while the actual queue eviction path drops a load
    # event. This makes the loss deterministic regardless of thread scheduling.
    with transport._event_changed:
        transport._route_message(
            {
                "method": "Page.lifecycleEvent",
                "params": {"name": "load", "loaderId": "loader-1"},
                "sessionId": "session-1",
            },
            size_bytes=100,
        )
        transport._route_message(
            {"method": "Runtime.consoleAPICalled", "params": {}, "sessionId": "session-1"},
            size_bytes=100,
        )
    thread.join(timeout=1)
    try:
        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), BrowserSecurityError)
        assert "lifecycle events overflowed" in str(outcome["error"])
        assert not transport.healthy
    finally:
        transport.close(timeout=0.2)


def test_guarded_navigation_authorizes_redirects_subresources_and_both_stages() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    authorized: list[str] = []

    def authorize(url: str) -> str:
        authorized.append(url)
        return url

    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, authorize, outcome)
    enable = _enable_persistent_guard(connection)
    assert enable["params"]["patterns"] == [
        {"urlPattern": "*", "requestStage": "Request"},
        {"urlPattern": "*", "requestStage": "Response"},
    ]
    navigate = connection.next_command("Page.navigate")

    stages = (
        ("top-request", "https://example.com/", False, "Fetch.continueRequest"),
        ("top-response", "https://example.com/", True, "Fetch.continueResponse"),
        ("redirect", "https://www.example.com/redirected", False, "Fetch.continueRequest"),
        ("asset-request", "https://cdn.example.com/app.js", False, "Fetch.continueRequest"),
        ("asset-response", "https://cdn.example.com/app.js", True, "Fetch.continueResponse"),
    )
    for request_id, url, response, expected_method in stages:
        _pause(connection, request_id, url, response=response)
        command = connection.next_command(expected_method)
        assert command["params"]["requestId"] == request_id
        connection.respond(command)

    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "loader-1"})
    _emit_document_load(connection, "loader-1")
    thread.join(timeout=1)
    try:
        assert outcome == {"result": {"frameId": "frame-1", "loaderId": "loader-1"}}
        assert authorized == [
            "https://example.com/",
            "https://example.com/",
            "https://example.com/",
            "https://www.example.com/redirected",
            "https://cdn.example.com/app.js",
            "https://cdn.example.com/app.js",
        ]
    finally:
        transport.close(timeout=0.2)


def test_persistent_guard_authorizes_post_load_xhr() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    authorized: list[str] = []

    def authorize(url: str) -> str:
        authorized.append(url)
        return url

    try:
        _complete_public_navigation(transport, connection, authorize)
        _pause(connection, "post-load-xhr", "https://api.example.com/data")
        continued = connection.next_command("Fetch.continueRequest")
        assert continued["params"]["requestId"] == "post-load-xhr"
        connection.respond(continued)
        deadline = time.monotonic() + 1
        while "https://api.example.com/data" not in authorized and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "https://api.example.com/data" in authorized
        assert connection.closed.is_set() is False
    finally:
        transport.close(timeout=0.2)


@pytest.mark.parametrize(
    ("method", "params"),
    (
        ("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 1, "y": 1}),
        ("Input.insertText", {"text": "submit"}),
    ),
    ids=("click-triggered-navigation", "type-triggered-submission"),
)
def test_persistent_guard_blocks_private_request_during_input_action(
    method: str,
    params: dict[str, Any],
) -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "127.0.0.1" in url:
            raise BrowserSecurityError("private destination")
        return url

    _complete_public_navigation(transport, connection, authorize)
    outcome: dict[str, BaseException] = {}

    def input_action() -> None:
        try:
            transport.call(method, params, session_id="session-1", timeout=1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            outcome["error"] = exc

    action = threading.Thread(target=input_action)
    action.start()
    connection.next_command(method)
    _pause(connection, "private-after-input", "http://127.0.0.1/admin")
    failed = connection.next_command("Fetch.failRequest")
    assert failed["params"] == {
        "requestId": "private-after-input",
        "errorReason": "BlockedByClient",
    }
    connection.respond(failed)
    assert connection.closed.wait(timeout=1)
    action.join(timeout=1)
    assert isinstance(outcome.get("error"), BrowserSecurityError)


def test_a_request_whose_destination_cannot_be_checked_is_refused_and_the_browser_keeps_going() -> (
    None
):
    """A slow or failing DNS lookup (often an ad or tracker host) refuses that request only."""
    from alysis_code.ide.managed_browser import (
        BrowserDestinationTimeoutError,
        BrowserDestinationUnresolvedError,
    )

    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "slow-dns" in url:
            raise BrowserDestinationTimeoutError("Browser destination resolution timed out.")
        if "no-dns" in url:
            raise BrowserDestinationUnresolvedError(
                "Browser destination could not be resolved safely."
            )
        return url

    _complete_public_navigation(transport, connection, authorize)
    for request_id, url in (
        ("slow", "https://slow-dns.example/t.js"),
        ("none", "https://no-dns.example/p.gif"),
    ):
        _pause(connection, request_id, url)
        failed = connection.next_command("Fetch.failRequest")
        assert failed["params"] == {"requestId": request_id, "errorReason": "BlockedByClient"}
        connection.respond(failed)
    _pause(connection, "fine", "https://example.com/app.js")
    continued = connection.next_command("Fetch.continueRequest")
    assert continued["params"]["requestId"] == "fine"
    connection.respond(continued)
    assert not connection.closed.is_set()
    assert transport.healthy and transport.failure is None
    transport.close(timeout=0.2)


def test_persistent_guard_pauses_and_guards_popup_before_resuming_it() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "127.0.0.1" in url:
            raise BrowserSecurityError("private destination")
        return url

    _complete_public_navigation(transport, connection, authorize)
    connection.receive_json(
        {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": "popup-session",
                "waitingForDebugger": True,
                "targetInfo": {
                    "targetId": "popup-target",
                    "type": "page",
                    "url": "",
                },
            },
            "sessionId": "session-1",
        }
    )
    child_auto_attach = connection.next_command("Target.setAutoAttach")
    assert child_auto_attach["sessionId"] == "popup-session"
    assert child_auto_attach["params"]["waitForDebuggerOnStart"] is True
    connection.respond(child_auto_attach)
    child_fetch = connection.next_command("Fetch.enable")
    assert child_fetch["sessionId"] == "popup-session"
    assert child_fetch["params"]["patterns"] == [
        {"urlPattern": "*", "requestStage": "Request"},
        {"urlPattern": "*", "requestStage": "Response"},
    ]
    connection.respond(child_fetch)
    resume = connection.next_command("Runtime.runIfWaitingForDebugger")
    assert resume["sessionId"] == "popup-session"
    connection.respond(resume)

    _pause(
        connection,
        "private-popup-request",
        "http://127.0.0.1/private",
        session_id="popup-session",
    )
    failed = connection.next_command("Fetch.failRequest")
    assert failed["sessionId"] == "popup-session"
    assert failed["params"] == {
        "requestId": "private-popup-request",
        "errorReason": "BlockedByClient",
    }
    connection.respond(failed)
    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="private destination"):
        transport.call("Runtime.evaluate", timeout=0.2)


def _attach_child(connection, session_id: str, target_type: str, url: str) -> None:
    connection.receive_json(
        {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": session_id,
                "waitingForDebugger": True,
                "targetInfo": {"targetId": session_id + "-target", "type": target_type, "url": url},
            },
            "sessionId": "session-1",
        }
    )
    auto_attach = connection.next_command("Target.setAutoAttach")
    assert auto_attach["sessionId"] == session_id
    connection.respond(auto_attach)


def test_dedicated_worker_without_fetch_domain_runs_under_its_documents_guard() -> None:
    """Chromium has no Fetch domain on dedicated workers; their requests pause on the page."""
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "127.0.0.1" in url:
            raise BrowserSecurityError("private destination")
        return url

    try:
        _complete_public_navigation(transport, connection, authorize)
        _attach_child(connection, "worker-session", "worker", "blob:https://example.com/w")
        fetch = connection.next_command("Fetch.enable")
        assert fetch["sessionId"] == "worker-session"
        connection.receive_json(
            {"id": fetch["id"], "error": {"code": -32601, "message": "'Fetch.enable' wasn't found"}}
        )
        resume = connection.next_command("Runtime.runIfWaitingForDebugger")
        assert resume["sessionId"] == "worker-session"
        connection.respond(resume)
        # The worker's request arrives on the owning document's session and is authorized.
        _pause(connection, "worker-fetch", "https://api.example.com/data")
        continued = connection.next_command("Fetch.continueRequest")
        assert continued["params"]["requestId"] == "worker-fetch"
        connection.respond(continued)
        assert transport.healthy and not connection.closed.is_set()
        # A private destination from it is still refused and fails closed.
        _pause(connection, "worker-private", "http://127.0.0.1/admin")
        failed = connection.next_command("Fetch.failRequest")
        connection.respond(failed)
        assert connection.closed.wait(timeout=1)
    finally:
        transport.close(timeout=0.2)


@pytest.mark.parametrize(
    ("target_type", "code"),
    (("shared_worker", -32601), ("service_worker", -32601), ("worker", -32000)),
)
def test_other_children_without_a_guard_still_fail_closed(target_type: str, code: int) -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    try:
        _complete_public_navigation(transport, connection, lambda url: url)
        _attach_child(connection, "child-session", target_type, "https://example.com/w.js")
        fetch = connection.next_command("Fetch.enable")
        connection.receive_json({"id": fetch["id"], "error": {"code": code, "message": "no"}})
        assert connection.closed.wait(timeout=1)
        assert not transport.healthy
    finally:
        transport.close(timeout=0.2)


def test_persistent_guard_fails_closed_for_unpaused_child_target() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    _complete_public_navigation(transport, connection, lambda url: url)

    connection.receive_json(
        {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": "unsafe-child",
                "waitingForDebugger": False,
                "targetInfo": {
                    "targetId": "unsafe-target",
                    "type": "worker",
                    "url": "https://example.com/worker.js",
                },
            },
            "sessionId": "session-1",
        }
    )

    assert connection.closed.wait(timeout=1)
    with pytest.raises(BrowserSecurityError, match="not safely paused"):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_persistent_guard_interception_queue_has_hard_byte_budget() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(
        connection,
        max_event_bytes=8 * 1024 * 1024,
    )
    _complete_public_navigation(transport, connection, lambda url: url)

    # Keep the broker occupied on one request while two individually valid
    # frames attempt to consume more than the aggregate interception budget.
    padding = "x" * (4 * 1024 * 1024 + 128 * 1024)
    _pause(connection, "broker-blocker", "https://example.com/one", padding=padding)
    connection.next_command("Fetch.continueRequest")
    _pause(connection, "queued-two", "https://example.com/two", padding=padding)
    _pause(connection, "queued-three", "https://example.com/three", padding=padding)

    assert connection.closed.wait(timeout=2)
    with pytest.raises(BrowserSecurityError, match="byte budget overflowed"):
        transport.call("Runtime.evaluate", timeout=0.2)


def test_guarded_navigation_fails_rejected_redirect_without_continuing_it() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)

    def authorize(url: str) -> str:
        if "127.0.0.1" in url:
            raise BrowserSecurityError("private destination")
        return url

    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, authorize, outcome)
    _enable_persistent_guard(connection)
    connection.next_command("Page.navigate")
    _pause(connection, "top", "https://example.com/")
    continued = connection.next_command("Fetch.continueRequest")
    connection.respond(continued)
    _pause(connection, "blocked", "http://127.0.0.1/secrets")
    failed = connection.next_command("Fetch.failRequest")
    assert failed["params"] == {"requestId": "blocked", "errorReason": "BlockedByClient"}
    connection.respond(failed)
    assert connection.closed.wait(timeout=1)
    thread.join(timeout=1)
    try:
        assert isinstance(outcome.get("error"), BrowserSecurityError)
        remaining = list(connection.outbound.queue)
        assert not any(
            command.get("method") == "Fetch.continueRequest"
            and command.get("params", {}).get("requestId") == "blocked"
            for command in remaining
        )
    finally:
        transport.close(timeout=0.2)


def test_stale_load_events_cannot_complete_a_new_navigation() -> None:
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    # Both legacy load events and lifecycle events from an older loader may be
    # queued before the new Page.navigate command is acknowledged.
    connection.receive_json(
        {
            "method": "Page.loadEventFired",
            "params": {"timestamp": 0.5},
            "sessionId": "session-1",
        }
    )
    _emit_document_load(connection, "old-loader")
    outcome: dict[str, Any] = {}
    thread = _run_guarded_navigation(transport, lambda url: url, outcome)
    _enable_persistent_guard(connection)
    navigate = connection.next_command("Page.navigate")
    _pause(connection, "top", "https://example.com/")
    continued = connection.next_command("Fetch.continueRequest")
    connection.respond(continued)
    connection.respond(navigate, {"frameId": "frame-1", "loaderId": "new-loader"})
    time.sleep(0.1)
    assert thread.is_alive(), "stale loader event ended guarded navigation"
    _emit_document_load(connection, "new-loader")
    thread.join(timeout=1)
    try:
        assert outcome == {"result": {"frameId": "frame-1", "loaderId": "new-loader"}}
    finally:
        transport.close(timeout=0.2)


def test_large_upload_envelopes_require_registered_session_and_strip_only_body():
    from alysis_code.ide.cdp_websocket_transport import _decode_message

    size = 9 * 1024 * 1024
    frame = {
        "method": "Fetch.requestPaused",
        "sessionId": "owned",
        "params": {
            "requestId": "r",
            "request": {
                "url": "https://example.test/receive",
                "method": "POST",
                "postData": "x" * size,
            },
        },
    }
    raw = json.dumps(frame).encode()
    for allowances in ({}, {"other": size}, {"owned": size - 1}):
        with pytest.raises(BrowserSecurityError):
            _decode_message(raw, upload_allowances=allowances)
    value, retained = _decode_message(raw, upload_allowances={"owned": size})
    assert retained < 1024
    assert value["params"]["request"] == {"url": "https://example.test/receive", "method": "POST"}
    frame["method"] = "Runtime.consoleAPICalled"
    with pytest.raises(BrowserSecurityError):
        _decode_message(json.dumps(frame).encode(), upload_allowances={"owned": size})
    frame["method"] = "Fetch.requestPaused"
    frame["params"]["request"]["headers"] = {"giant": "y" * size}
    with pytest.raises(BrowserSecurityError):
        _decode_message(json.dumps(frame).encode(), upload_allowances={"owned": size})


def test_browser_level_popup_guard_processes_network_while_resume_waits():
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    outcome = {}

    def navigate():
        try:
            outcome["result"] = transport.guarded_navigate(
                "https://example.com/",
                session_id="root",
                root_target_id="root-target",
                authorize_url=lambda url: url,
                timeout=3,
            )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=navigate)
    thread.start()
    try:
        _enable_persistent_guard(connection)
        inventory = connection.next_command("Target.getTargets")
        connection.respond(
            inventory,
            {"targetInfos": [dict(targetId="root-target", type="page", url="about:blank")]},
        )
        watch = connection.next_command("Target.setAutoAttach")
        assert "sessionId" not in watch
        assert watch["params"]["filter"] == [{"type": "page"}, {"exclude": True}]
        connection.respond(watch)
        nav = connection.next_command("Page.navigate")
        connection.respond(nav, {"frameId": "frame-1", "loaderId": "loader-1"})
        connection.receive_json(
            {
                "method": "Page.lifecycleEvent",
                "params": {
                    "frameId": "frame-1",
                    "loaderId": "loader-1",
                    "name": "load",
                    "timestamp": 1,
                },
                "sessionId": "root",
            }
        )
        thread.join(timeout=1)
        assert "error" not in outcome
        assert not thread.is_alive()
        # Browser-level events have no parent sessionId, unlike iframe events.
        connection.receive_json(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "popup",
                    "waitingForDebugger": True,
                    "targetInfo": dict(targetId="popup-target", type="page", url=""),
                },
            }
        )
        connection.respond(connection.next_command("Target.setAutoAttach"))
        connection.respond(connection.next_command("Fetch.enable"))
        resume = connection.next_command("Runtime.runIfWaitingForDebugger")
        assert transport.guarded_page_sessions(session_id="root") == {}
        _pause(connection, "popup-request", "https://example.com/review", session_id="popup")
        request = connection.next_command("Fetch.continueRequest")
        assert request["sessionId"] == "popup"
        connection.respond(request)
        connection.respond(resume)
        deadline = time.monotonic() + 1
        while (
            not transport.guarded_page_sessions(session_id="root") and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert transport.guarded_page_sessions(session_id="root") == {"popup-target": "popup"}
        assert transport.guarded_page_sessions(session_id="unrelated") == {}
        # Closing the initial tab must not disable guarding of future pages.
        connection.receive_json(
            {"method": "Target.detachedFromTarget", "params": {"sessionId": "root"}}
        )
        connection.receive_json(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "second",
                    "waitingForDebugger": True,
                    "targetInfo": {"targetId": "second-target", "type": "page", "url": ""},
                },
            }
        )
        child = connection.next_command("Target.setAutoAttach")
        assert child["sessionId"] == "second"
        connection.respond(child)
        connection.respond(connection.next_command("Fetch.enable"))
        resume_second = connection.next_command("Runtime.runIfWaitingForDebugger")
        connection.respond(resume_second)
        connection.receive_json(
            {"method": "Target.detachedFromTarget", "params": {"sessionId": "second"}}
        )
        connection.receive_json(
            {"method": "Target.detachedFromTarget", "params": {"sessionId": "popup"}}
        )
        deadline = time.monotonic() + 1
        while transport.guarded_page_sessions(session_id="root") and time.monotonic() < deadline:
            time.sleep(0.005)
        assert transport.guarded_page_sessions(session_id="root") == {}
    finally:
        transport.close(timeout=1)
        thread.join(timeout=1)


def _attach_test_popup(transport, connection):
    connection.receive_json(
        {
            "method": "Target.attachedToTarget",
            "sessionId": "session-1",
            "params": {
                "sessionId": "popup",
                "waitingForDebugger": True,
                "targetInfo": {"targetId": "popup-target", "type": "page", "url": ""},
            },
        }
    )
    connection.respond(connection.next_command("Target.setAutoAttach"))
    connection.respond(connection.next_command("Fetch.enable"))
    connection.respond(connection.next_command("Runtime.runIfWaitingForDebugger"))
    deadline = time.monotonic() + 1
    while (
        not transport.guarded_page_sessions(session_id="session-1") and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    assert transport.guarded_page_sessions(session_id="session-1") == {"popup-target": "popup"}


@pytest.mark.parametrize("detached", [True, False])
def test_popup_fetch_command_error_only_recovers_after_confirmed_detach(detached):
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    try:
        _complete_public_navigation(transport, connection, lambda url: url)
        _attach_test_popup(transport, connection)
        _pause(connection, "closing-request", "https://example.com/", session_id="popup")
        pending = connection.next_command("Fetch.continueRequest")
        if detached:
            # The interceptor is waiting for the command response, so the
            # reader must see teardown before the queued detach is processed.
            connection.receive_json(
                {
                    "method": "Target.detachedFromTarget",
                    "sessionId": "session-1",
                    "params": {"sessionId": "popup"},
                }
            )
        connection.receive_json(
            {"id": pending["id"], "error": {"code": -32001, "message": "Session closed"}}
        )
        if detached:
            # A root request behind that failed command proves the guard pump
            # continues. There must be no retry on the closed popup.
            _pause(connection, "root-still-live", "https://example.com/")
            continued = connection.next_command("Fetch.continueRequest")
            assert continued["sessionId"] == "session-1"
            assert continued["params"]["requestId"] == "root-still-live"
            connection.respond(continued)
            assert transport.healthy
            assert transport.guarded_page_sessions(session_id="session-1") == {}
        else:
            assert connection.closed.wait(1), "Live-target command failures must still fail closed"
    finally:
        transport.close(timeout=1)


def test_queued_fetch_for_detached_popup_does_not_break_sibling_guard():
    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    try:
        _complete_public_navigation(transport, connection, lambda url: url)
        _attach_test_popup(transport, connection)
        _pause(connection, "block-pump", "https://example.com/")
        pending = connection.next_command("Fetch.continueRequest")
        connection.receive_json(
            {
                "method": "Target.detachedFromTarget",
                "sessionId": "session-1",
                "params": {"sessionId": "popup"},
            }
        )
        _pause(connection, "closed-popup-event", "https://example.com/", session_id="popup")
        _pause(connection, "root-still-live", "https://example.com/")
        connection.respond(pending)
        continued = connection.next_command("Fetch.continueRequest")
        assert continued["sessionId"] == "session-1"
        assert continued["params"]["requestId"] == "root-still-live"
        connection.respond(continued)
        assert transport.healthy
    finally:
        transport.close(timeout=1)


@pytest.mark.parametrize("method", ["DOMSnapshot.captureSnapshot", "Input.dispatchMouseEvent"])
def test_detach_wakes_pending_page_command_without_retry_or_transport_failure(method):
    from alysis_code.ide.managed_browser import BrowserPageUnavailableError

    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    outcome = {}
    worker = None
    try:
        _complete_public_navigation(transport, connection, lambda url: url)
        _attach_test_popup(transport, connection)

        def command():
            try:
                transport.call(method, session_id="popup", timeout=0.3)
            except BrowserError as error:
                outcome["error"] = error

        worker = threading.Thread(target=command)
        worker.start()
        pending = connection.next_command(method)
        connection.receive_json(
            {
                "method": "Target.detachedFromTarget",
                "sessionId": "session-1",
                "params": {"sessionId": "popup"},
            }
        )
        worker.join(timeout=0.2)
        assert not worker.is_alive(), "Detached page read remained blocked until timeout"
        assert isinstance(outcome.get("error"), BrowserPageUnavailableError)
        assert transport.healthy
        with pytest.raises(BrowserPageUnavailableError):
            transport.call(method, session_id="popup", timeout=0.1)
        # Chromium may still emit one late response to the retired command.
        connection.respond(pending)
        _pause(connection, "sibling-request", "https://example.com/")
        continued = connection.next_command("Fetch.continueRequest")
        connection.respond(continued)
        assert transport.healthy
        assert not any(c["method"] == method for c in list(connection.outbound.queue))
        connection.respond(pending)
        assert connection.closed.wait(1), "A duplicate retired response must still fail closed"
    finally:
        transport.close(timeout=1)
        if worker:
            worker.join(timeout=1)


def test_download_navigation_is_typed_and_keeps_persistent_guard():
    from alysis_code.ide.managed_browser import BrowserDownloadRequiredError

    connection = FakeWebSocket()
    transport = WebSocketCdpTransport(connection)
    outcome = {}
    seen = []
    thread = _run_guarded_navigation(transport, lambda url: seen.append(url) or url, outcome)
    try:
        _enable_persistent_guard(connection)
        command = connection.next_command("Page.navigate")
        connection.respond(command, {"isDownload": True, "errorText": "net::ERR_ABORTED"})
        thread.join(2)
        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), BrowserDownloadRequiredError)
        assert not connection.closed.is_set()
        _pause(connection, "after-download", "https://example.com/still-guarded")
        continued = connection.next_command("Fetch.continueRequest")
        connection.respond(continued)
        assert "https://example.com/still-guarded" in seen
    finally:
        transport.close(timeout=1)
        thread.join(2)
