"""Real offline Chromium cancellation study; no provider or external website.

Run via Docker exec stdin in the existing local lab. A slow local HTTP response
provides the navigation cancellation boundary. For input, a transport decorator
sets cancellation AFTER an actual Backspace acknowledgement; it never fabricates
a CDP response. The study distinguishes delivery from rollback and connection
health from a still-running browser process.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from alysis_code.ide.managed_browser import (
    BrowserCancelledError,
    BrowserError,
    ManagedBrowserConfig,
    ManagedBrowserService,
)


def main() -> None:
    entered = threading.Event()
    release = threading.Event()
    page = b"""<!doctype html><meta charset="utf-8"><title>Cancellation study</title>
    <label>Draft<input id="draft" value="original draft"></label>
    <button id="show">Show</button><p id="value"></p>
    <script>document.querySelector('#show').onclick=()=>{
    document.querySelector('#value').textContent='Value:['+
      document.querySelector('#draft').value+']';};</script>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                entered.set()
                release.wait(10)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            try:
                self.wfile.write(page)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    report = {"kind": "real offline Linux Chromium, no model calls", "checks": {}}
    try:
        with TemporaryDirectory(prefix="browser-cancel-") as data:
            browser = ManagedBrowserService(
                config=ManagedBrowserConfig(
                    data_root=Path(data),
                    linux_display=os.environ["DISPLAY"],
                ),
                transport_factory=WebSocketCdpTransportFactory(),
            )
            owner = "cancellation-study"
            try:
                session = browser.start(owner, allowed_preview_urls_provider=lambda: [url])
                sid = session.session_id
                browser.navigate(owner, sid, url)
                cancelled = threading.Event()
                done = threading.Event()
                outcome = []

                def navigate():
                    try:
                        browser.navigate(owner, sid, url + "slow", cancel=cancelled.is_set)
                        outcome.append("unexpected_success")
                    except BrowserError as exc:
                        outcome.append(type(exc).__name__)
                    finally:
                        done.set()

                worker = threading.Thread(target=navigate, daemon=True)
                worker.start()
                assert entered.wait(10), "Chromium never reached the slow local server."
                started = time.monotonic()
                cancelled.set()
                assert done.wait(3), "In-flight navigation did not observe cancellation."
                elapsed = time.monotonic() - started
                assert outcome == ["BrowserCancelledError"], outcome
                state = browser.status(owner, sid).state
                assert state == "disconnected", "Closed control connection was reported usable."
                try:
                    browser.snapshot(owner, sid, kind="text")
                    followup = "unexpected_success"
                except BrowserError as exc:
                    followup = type(exc).__name__
                assert followup != "unexpected_success"
                report["checks"]["navigation"] = {
                    "cancelled_after_request_reached_server": True,
                    "cancel_latency_seconds": round(elapsed, 3),
                    "outcome": outcome[0],
                    "reported_session_state": state,
                    "followup_observation": followup,
                }
            finally:
                release.set()
                browser.close_all()

        cancelled = threading.Event()

        class CancelAfterDelete:
            def __init__(self, transport):
                self.transport = transport

            def __getattr__(self, name):
                return getattr(self.transport, name)

            def call(self, method, params=None, **kwargs):
                result = self.transport.call(method, params, **kwargs)
                if method == "Input.dispatchKeyEvent" and params == {
                    "type": "keyDown",
                    "key": "Backspace",
                    "code": "Backspace",
                    "windowsVirtualKeyCode": 8,
                }:
                    cancelled.set()
                return result

        class Factory:
            def connect(self, *args, **kwargs):
                return CancelAfterDelete(WebSocketCdpTransportFactory().connect(*args, **kwargs))

        with TemporaryDirectory(prefix="browser-cancel-input-") as data:
            browser = ManagedBrowserService(
                config=ManagedBrowserConfig(
                    data_root=Path(data),
                    linux_display=os.environ["DISPLAY"],
                ),
                transport_factory=Factory(),
            )
            try:
                session = browser.start(owner, allowed_preview_urls_provider=lambda: [url])
                sid = session.session_id
                browser.navigate(owner, sid, url)
                try:
                    browser.type_text(owner, sid, "#draft", "new draft", cancel=cancelled.is_set)
                except BrowserCancelledError:
                    pass
                else:
                    raise AssertionError("Input did not observe cancellation between commands.")
                browser.click(owner, sid, "#show")
                text = browser.snapshot(owner, sid, kind="text")["text"]
                assert "Value:[]" in text, text
                report["checks"]["partial_input"] = {
                    "cancelled_after_real_delete_acknowledgement": True,
                    "replacement_not_inserted": True,
                    "original_value_not_rolled_back": True,
                    "reported_session_state": browser.status(owner, sid).state,
                    "followup_observation_succeeded": True,
                }
            finally:
                browser.close_all()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
