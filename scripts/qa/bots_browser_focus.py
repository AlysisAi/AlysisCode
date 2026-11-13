"""Real Linux regression for page handlers redirecting focus before input."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.cancellation import EventCancellationToken

PAGE = b"""<!doctype html><meta charset="utf-8"><title>Focus identity fixture</title>
<form id="expected"><label>Expected <input id="target" value="synthetic"></label></form>
<form id="other"><label>Other <input id="redirected" value="synthetic"></label></form>
<p id="receipt">No submission</p>
<script>
document.querySelector('#target').onfocus=()=>document.querySelector('#redirected').focus();
for (const form of document.forms) form.onsubmit=e=>{
  e.preventDefault(); document.querySelector('#receipt').textContent='Submitted '+form.id;
  fetch('/receipt', {method:'POST',body:form.id});
};
</script>"""
saved = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        saved.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{server.server_port}"
computers = BrowserComputers(
    "review",
    ComputerViews(),
    lambda bot, cancel: LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (origin + "/", "")),
)
task = {"id": "review", "bot_id": "review"}
token = EventCancellationToken(threading.Event())
try:
    before = computers.call(task, "browser_navigate", {"url": origin}, token)
    target = next(
        n["node"]
        for n in before["dom"]["documents"][0]["nodes"]
        if n.get("attributes", {}).get("id") == "target"
    )
    blocked = []
    for name, extra in [
        ("browser_key", {"key": "Enter"}),
        ("browser_type", {"text": "Must never reach other input"}),
    ]:
        if blocked:
            before = computers.call(task, "browser_navigate", {"url": origin}, token)
            target = next(
                n["node"]
                for n in before["dom"]["documents"][0]["nodes"]
                if n.get("attributes", {}).get("id") == "target"
            )
        try:
            computers.call(
                task,
                name,
                {"source_id": before["source_id"], "node": target, **extra},
                token,
                approved_origins=(origin,),
            )
        except ValueError as error:
            assert "Focus moved" in str(error.__context__), str(error.__context__)
            blocked.append(name)
        else:
            raise AssertionError("Redirected focus accepted input")
    assert saved == [], saved
    print(
        json.dumps({"passed": True, "blocked": blocked, "received_forms": saved, "model_calls": 0}),
        flush=True,
    )

finally:
    computers.close()
    server.shutdown()
