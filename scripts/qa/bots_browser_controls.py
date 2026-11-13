"""Synthetic controls and provider-free real Linux qualification.

Run with --linux inside the disposable lane-A image, mounting current source.
This fixture is also used by the separately opt-in real-model runtime study.
No credentials, external accounts or model responses are present here.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><meta charset="utf-8"><title>Browser controls study</title>
<style>body{font:22px system-ui;padding:30px}input,select{font:inherit;margin:10px}
#feed{height:150px;width:650px;overflow:auto;border:2px solid #555}#content{height:2400px;padding:10px}
</style><h1>Browser controls study</h1><p>Synthetic data only.</p>
<form id="form"><label>Note <input id="note" required></label>
<label>Region <select id="region"><option value="">Choose a region</option>
<option value="same">Ελλάδα</option><optgroup label="Asia"><option value="same">日本語</option>
<option value="disabled" disabled>Unavailable</option></optgroup></select></label></form>
<select id="other" aria-label="Other choices"><option>Other choice</option></select>
<p id="saved" role="status">No note saved.</p>
<h2>Feed</h2><div id="feed" tabindex="0"><div id="content">Scroll this feed to its bottom.</div></div>
<p id="scroll-status" role="status">Feed has not reached its bottom.</p>
<script>
const feed=document.querySelector('#feed');
feed.onscroll=()=>{document.querySelector('#scroll-status').textContent=
feed.scrollTop+feed.clientHeight>=feed.scrollHeight-2?'Feed reached its bottom.':'Feed position: '+Math.round(feed.scrollTop)};
document.querySelector('#form').onsubmit=async e=>{e.preventDefault();
const select=document.querySelector('#region');const note=document.querySelector('#note').value;
const data={note,region:select.selectedOptions[0].textContent,value:select.value,index:select.selectedIndex};
const r=await fetch('/save',{method:'POST',body:JSON.stringify(data)});
if(r.ok)document.querySelector('#saved').textContent='Saved: '+note+' / '+data.region};
</script>"""


def linux_checks():
    from alysis_code.bots.browser import BrowserComputers
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken

    saved = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            saved.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    views = ComputerViews()
    computers = BrowserComputers(
        "controls",
        views,
        lambda bot, cancel: LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (url + "/",)),
    )
    task = {"id": "controls", "bot_id": "controls"}
    event = threading.Event()
    token = EventCancellationToken(event)
    checks = []

    def call(name, args=None, *, granted=True, request=task):
        return computers.call(
            request, name, args or {}, token, approved_origins=(url,) if granted else ()
        )

    def nodes(obs):
        return obs["dom"]["documents"][0]["nodes"]

    def target(obs, identifier):
        return next(
            n["node"] for n in nodes(obs) if n.get("attributes", {}).get("id") == identifier
        )

    def option(obs, text):
        text_node = next(n for n in nodes(obs) if n.get("text", "").strip() == text)
        return text_node["parent"]

    def action(obs, name, identifier, **extra):
        return call(name, {"source_id": obs["source_id"], "node": target(obs, identifier), **extra})

    def denied(operation, text):
        try:
            operation()
        except ValueError:
            checks.append(text)
        else:
            raise AssertionError(text)

    try:
        observed = call("browser_navigate", {"url": url})
        assert len([n for n in nodes(observed) if n["name"] == "OPTION"]) == 5
        checks.append("Non-layout options and multilingual labels observed")
        denied(
            lambda: call(
                "browser_key",
                {
                    "source_id": observed["source_id"],
                    "node": target(observed, "note"),
                    "key": "Enter",
                },
                granted=False,
            ),
            "Input without task grant denied",
        )
        denied(
            lambda: action(
                observed, "browser_select", "other", option_node=option(observed, "日本語")
            ),
            "Cross-select option denied",
        )
        current = action(
            observed, "browser_select", "region", option_node=option(observed, "日本語")
        )
        assert next(n for n in nodes(current) if n["node"] == option(current, "日本語"))["selected"]
        checks.append("Observed option identity selected despite duplicate values")
        denied(
            lambda: action(
                observed, "browser_select", "region", option_node=option(observed, "Ελλάδα")
            ),
            "Prior action reference cannot replay",
        )
        note = "Νύχτα — 日本語 — ARBOR73"
        current = action(current, "browser_type", "note", text=note)
        assert (
            next(n for n in nodes(current) if n["node"] == target(current, "note"))["value"] == note
        )
        checks.append("Unicode text round trip")
        current = action(current, "browser_key", "note", key="Enter")
        deadline = time.monotonic() + 5
        while not saved and time.monotonic() < deadline:
            current = call("browser_observe")
        assert saved == [{"note": note, "region": "日本語", "value": "same", "index": 2}], saved
        assert "Saved: " + note in json.dumps(current, ensure_ascii=False)
        checks.append("Enter submitted once and real server receipt matches")
        current = action(current, "browser_scroll", "feed", delta_y=1500)
        current = action(current, "browser_scroll", "feed", delta_y=1500)
        assert "Feed reached its bottom." in json.dumps(current, ensure_ascii=False)
        checks.append("Nested feed scrolled with actual DOM confirmation")
        assert views.frame("controls", "other")["state"] == "not_running"
        checks.append("Other bot cannot view this computer")
        denied(
            lambda: call(
                "browser_scroll",
                {
                    "source_id": current["source_id"],
                    "node": target(current, "feed"),
                    "delta_y": 500,
                },
                request={"id": "other-task", "bot_id": "controls"},
            ),
            "Concurrent task cannot acquire computer",
        )
        computers.release(task["id"], cancelled=False)
        denied(
            lambda: action(current, "browser_scroll", "feed", delta_y=-500),
            "Released task needs new observation",
        )
        current = call("browser_observe")
        event.set()
        try:
            action(current, "browser_key", "note", key="Enter")
        except CooperativeCancellationError:
            checks.append("Cancelled token cannot dispatch input")
        else:
            raise AssertionError("Cancelled input accepted")
        assert len(saved) == 1
        event.clear()
        computers.release(task["id"], cancelled=True)
        assert views.frame("controls", "controls")["state"] == "not_running"
        checks.append("Cancellation revokes view and disposes computer")
        current = call("browser_navigate", {"url": url})
        denied(
            lambda: action(
                current, "browser_select", "region", option_node=option(current, "Unavailable")
            ),
            "Disabled native option rejected by real Chromium",
        )
        assert views.frame("controls", "controls")["state"] == "not_running"
        print(
            json.dumps(
                {"passed": True, "checks": checks, "saved": saved, "model_calls": 0},
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        computers.close()
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    args = parser.parse_args()
    if not args.linux:
        parser.error("Run --linux inside the isolated disposable lane-A container")
    try:
        linux_checks()
    except Exception as error:
        if error.__context__ is not None:
            print(type(error.__context__).__name__, str(error.__context__), flush=True)
        raise
