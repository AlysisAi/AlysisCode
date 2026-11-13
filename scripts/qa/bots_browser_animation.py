"""Real headless Chromium regression: CSS animation must not block a stable DOM click.

Uses the production browser service and Owl adapter, an ephemeral profile and a
loopback-only fixture. No model provider or Cloud computer is involved.
"""

import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from alysis_code.bots.browser import BrowserComputers, review_dom_state
from alysis_code.bots.computer import ComputerViews
from alysis_code.cancellation import EventCancellationToken
from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from alysis_code.ide.managed_browser import ManagedBrowserConfig, ManagedBrowserService

PAGE = b"""<!doctype html><meta charset="utf-8"><title>Animated page regression</title>
<style>@keyframes move {to {margin-left:350px}}
#mascot {height:100px;width:100px;background:green;animation:move 1s linear infinite alternate}
.paused #mascot {animation-play-state:paused}</style>
<div id="mascot"></div><button id="pause">Pause animation</button><p id="result">Waiting</p>
<script>document.getElementById('pause').onclick=()=>{
 document.body.classList.add('paused');document.getElementById('result').textContent='Paused';
 fetch('/receipt',{method:'POST'});
};</script>"""
receipts = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        receipts.append(self.path)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{server.server_port}"
with tempfile.TemporaryDirectory(prefix="alysis-animation-") as directory:
    service = ManagedBrowserService(
        config=ManagedBrowserConfig(data_root=Path(directory)),
        transport_factory=WebSocketCdpTransportFactory(),
    )

    class Executor:
        browser = service
        owner = "animation-test"

        def __init__(self, bot, cancel):
            self.session_id = service.start(
                self.owner,
                allow_local_destinations=True,
                local_destinations_loopback_only=True,
                allowed_preview_urls_provider=lambda: (origin + "/",),
            ).session_id

        def preview_urls(self):
            return (origin + "/",)

        def alive(self):
            return service.status(self.owner, self.session_id).state == "running"

        def capture(self):
            return service.capture_visual_frame(self.owner, self.session_id).png

        def close(self):
            service.close(self.owner, self.session_id)

    computers = BrowserComputers("animation-test", ComputerViews(), Executor)
    task = {"id": "animation-test", "bot_id": "animation-test"}
    token = EventCancellationToken(threading.Event())
    try:
        before = computers.call(task, "browser_navigate", {"url": origin}, token)
        target = next(
            n["node"]
            for n in before["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == "pause"
        )
        evidence = {}

        def review(*args):
            executor = computers._computers[task["bot_id"]].executor
            service.capture_visual_frame(executor.owner, executor.session_id)
            first = service.snapshot(executor.owner, executor.session_id, kind="dom")["data"]
            time.sleep(0.3)
            service.capture_visual_frame(executor.owner, executor.session_id)
            second = service.snapshot(executor.owner, executor.session_id, kind="dom")["data"]
            evidence["raw_layout_changed"] = first != second
            evidence["content_unchanged"] = review_dom_state(first) == review_dom_state(second)
            assert evidence["raw_layout_changed"] and evidence["content_unchanged"]

        result = computers.call(
            task,
            "browser_click",
            {"source_id": before["source_id"], "node": target},
            token,
            approved_origins=(origin,),
            review_action=review,
        )
        assert result["action"]["dispatched"] is True
        assert receipts == ["/receipt"], receipts
        assert any(n.get("text") == "Paused" for n in result["dom"]["documents"][0]["nodes"])
        print(json.dumps({"passed": True, **evidence, "clicks_received": len(receipts)}))
    finally:
        try:
            computers.close()
        finally:
            service.close_all()
            server.shutdown()
