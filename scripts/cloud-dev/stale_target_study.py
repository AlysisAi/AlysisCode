"""Reproduce stale CSS targets in real offline Chromium, without a provider.

Default mode records how unguarded positional selectors can affect new controls.
--guarded requires the observed backend node identity and verifies rejection,
fresh-observation recovery, same-selector replacement, and navigation.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from alysis_code.ide.browser_observation import compact_dom_snapshot
from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from alysis_code.ide.managed_browser import (
    BrowserNotFoundError,
    ManagedBrowserConfig,
    ManagedBrowserService,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guarded", action="store_true")
    options = parser.parse_args()
    page = b"""<!doctype html><meta charset="utf-8"><title>Changing target study</title>
<style>body{font:22px sans-serif;max-width:780px;margin:60px auto}label,input,button,output{display:block;margin:14px 0;font:inherit}</style>
<h1>Changing controls</h1>
<section id="fields"><label>Note<input id="note" value="original"></label></section>
<section id="actions"><button id="target">Record note</button></section>
<button id="change">Insert controls</button><button id="replace">Replace note</button>
<output id="result">No action</output>
<script>
document.querySelector('#change').onclick=()=>{
  const label=document.createElement('label'); label.textContent='New field';
  const input=document.createElement('input'); input.id='new-field'; input.value='untouched';
  label.append(input); document.querySelector('#fields').prepend(label);
  const button=document.createElement('button'); button.textContent='New action';
  button.id='new-action'; button.onclick=()=>{document.querySelector('#result').textContent='Wrong action';};
  document.querySelector('#actions').prepend(button);
};
document.querySelector('#replace').onclick=()=>{
  const previous=document.querySelector('#note'); const next=previous.cloneNode();
  next.value='replacement'; previous.replaceWith(next);
};
document.querySelector('#target').onclick=()=>{
  document.querySelector('#result').textContent='Recorded: '+document.querySelector('#note').value;
};
</script>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {
        "kind": "real offline Chromium, no model",
        "guarded": options.guarded,
        "mode": "guarded-verification" if options.guarded else "baseline-reproduction",
    }
    output = Path("/var/lib/alysis/checks") / ("stale-target-" + uuid4().hex)
    output.mkdir(parents=True)
    try:
        with TemporaryDirectory(prefix="stale-target-") as data:
            browser = ManagedBrowserService(
                config=ManagedBrowserConfig(
                    data_root=Path(data), linux_display=os.environ["DISPLAY"]
                ),
                transport_factory=WebSocketCdpTransportFactory(),
            )
            owner = "stale-target-study"
            url = f"http://127.0.0.1:{server.server_port}/"
            try:
                sid = browser.start(owner, allowed_preview_urls_provider=lambda: [url]).session_id
                browser.navigate(owner, sid, url)

                def observe():
                    snapshot = browser.snapshot(owner, sid, kind="dom")
                    assert not snapshot["truncated"]
                    raw = snapshot["data"]
                    view = compact_dom_snapshot(raw)
                    return {
                        node["attributes"]["id"]: {
                            **node,
                            "backend": raw["documents"][0]["nodes"]["backendNodeId"][node["node"]],
                        }
                        for node in view["documents"][0]["nodes"]
                        if node.get("attributes", {}).get("id")
                    }

                def act(action, target, *args):
                    identity = (
                        {"expected_backend_node_id": target["backend"]} if options.guarded else {}
                    )
                    try:
                        action(owner, sid, target["selector"], *args, **identity)
                    except BrowserNotFoundError:
                        return "stale_rejected"
                    return "dispatched"

                first = observe()
                browser.click(owner, sid, first["change"]["selector"])
                typed = act(browser.type_text, first["note"], "should reach original")
                clicked = act(browser.click, first["target"])
                current = observe()
                text = browser.snapshot(owner, sid, kind="text")["text"]
                report["structural_change"] = {
                    "type": typed,
                    "click": clicked,
                    "original_value": current["note"]["value"],
                    "new_field_value": current["new-field"]["value"],
                    "wrong_action_observed": "Wrong action" in text,
                }
                if options.guarded:
                    assert typed == clicked == "stale_rejected"
                    assert current["note"]["value"] == "original"
                    assert current["new-field"]["value"] == "untouched"
                    assert "No action" in text
                    expected = "Δοκιμή — 日本語 — " + uuid4().hex
                    assert act(browser.type_text, current["note"], expected) == "dispatched"
                    assert act(browser.click, current["target"]) == "dispatched"
                    assert (
                        "Recorded: " + expected in browser.snapshot(owner, sid, kind="text")["text"]
                    )
                    assert observe()["new-field"]["value"] == "untouched"
                    report["fresh_observation_recovered"] = True
                    browser.click(owner, sid, current["replace"]["selector"])
                    assert (
                        act(browser.type_text, current["note"], "must not replace")
                        == "stale_rejected"
                    )
                    replacement = observe()
                    assert replacement["note"]["value"] == "replacement"
                    assert replacement["note"]["backend"] != current["note"]["backend"]
                    report["same_selector_replacement_rejected"] = True
                    artifact = browser.screenshot(owner, sid)
                    content = browser.read_artifact(owner, sid, artifact.artifact_id)
                    (output / "page.png").write_bytes(
                        base64.b64decode(content["content"], validate=True)
                    )
                    browser.navigate(owner, sid, url)
                    # The stable ID selector exists again, but identifies a new document node.
                    replacement["note"]["selector"] = "#note"
                    assert (
                        act(browser.type_text, replacement["note"], "must not navigate")
                        == "stale_rejected"
                    )
                    assert observe()["note"]["value"] == "original"
                    report["navigation_rejected"] = True
                else:
                    assert typed == clicked == "dispatched"
                    assert current["note"]["value"] == "original"
                    assert current["new-field"]["value"] == "should reach original"
                    assert "Wrong action" in text
                report["passed"] = True
                report["target_safety_passed"] = options.guarded
            finally:
                browser.close_all()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    report["evidence"] = str(output)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
