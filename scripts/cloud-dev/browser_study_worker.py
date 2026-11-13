"""Offline, disposable browser fixture and real managed-browser tools for the study.

Runs inside the existing Linux lab. stdin/stdout is a host-controlled JSON-lines
protocol, not a network service. No provider key enters the desktop container.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
from collections import OrderedDict
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from alysis_code.ide.browser_observation import compact_dom_snapshot
from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from alysis_code.ide.managed_browser import (
    BrowserError,
    BrowserNotFoundError,
    ManagedBrowserConfig,
    ManagedBrowserService,
)


class SnapshotSources:
    """Worker-local, bounded original observations; never refresh on a source read."""

    def __init__(self, capacity: int = 4, chunk_chars: int = 4000):
        if capacity < 1 or chunk_chars < 1:
            raise ValueError("Source limits must be positive")
        self.capacity = capacity
        self.chunk_chars = chunk_chars
        self.sources: OrderedDict[str, str] = OrderedDict()

    def add(self, original: dict) -> tuple[str, int]:
        source_id = uuid4().hex
        value = json.dumps(original, ensure_ascii=False)
        self.sources[source_id] = value
        while len(self.sources) > self.capacity:
            self.sources.popitem(last=False)
        return source_id, len(value)

    def read(self, source_id: str, offset: int) -> dict:
        if not isinstance(source_id, str) or type(offset) is not int:
            raise ValueError("Source id and character offset required")
        value = self.sources[source_id]
        if not 0 <= offset < len(value):
            raise ValueError("Source offset out of range")
        end = min(offset + self.chunk_chars, len(value))
        return {
            "source_id": source_id,
            "offset": offset,
            "total_chars": len(value),
            "content": value[offset:end],
            "next_offset": end if end < len(value) else None,
        }

    def target(self, source_id: str, node: int) -> tuple[str, int]:
        """Resolve only a shown main-document element in retained original evidence."""
        if not isinstance(source_id, str) or type(node) is not int:
            raise ValueError("Source id and node index required")
        raw = json.loads(self.sources[source_id])["dom"]["data"]
        view = compact_dom_snapshot(raw)
        if not view["documents"]:
            raise ValueError("Observation has no actionable document")
        for record in view["documents"][0]["nodes"]:
            if record["node"] == node and "selector" in record:
                return record["selector"], raw["documents"][0]["nodes"]["backendNodeId"][node]
        raise ValueError("Target is not an actionable main-document element")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-mode", choices=("full", "compact"), default="full")
    parser.add_argument(
        "--scenario", choices=("form", "clipped-reference", "replaced-form"), default="form"
    )
    options = parser.parse_args()
    sources = SnapshotSources()
    observation_sizes = []
    fields = {name: "field-" + uuid4().hex for name in ("project", "note", "submit")}
    expected = {
        "project": "Meadow " + uuid4().hex[:8],
        "note": "Δοκιμή — 日本語 — offline import only",
    }
    reference = ""
    if options.scenario == "clipped-reference":
        expected["note"] = (
            "The pilot uses a local import and a separate review step before anyone changes the "
            "original material. Reviewers compare the imported paragraphs with the source and "
            "record any missing accents, punctuation, or ordering differences. The pilot owner "
            "keeps the original file available throughout the review so that a shortened preview "
            "never becomes the only evidence. After review, the team saves a complete copy of "
            "this reference note, including the final receipt, in the local study form. "
            "Ακριβές αντίγραφο — 日本語の確認 — receipt " + uuid4().hex
        )
        reference = f"<h2>Reference note</h2><p>{escape(expected['note'])}</p>"
        task = (
            f"Use this local form to save exactly one note for project {expected['project']}. "
            "Copy the entire Reference note from the page into the Note field, including its "
            "final receipt. Preserve the exact wording, punctuation and Unicode. Verify the "
            "visible saved confirmation, then report briefly. Do not navigate elsewhere."
        )
    else:
        task = (
            "Use this local form to save exactly one note with these exact JSON field values:\n"
            + json.dumps(expected, ensure_ascii=False)
            + "\nVerify the visible saved confirmation, then report briefly. Do not navigate elsewhere."
        )
    refresh = (
        '<button id="refresh-form" type="button" onclick="const f=document.querySelector(\'form\');f.replaceWith(f.cloneNode(true))">Refresh form</button>'
        if options.scenario == "replaced-form"
        else ""
    )
    saved: list[dict] = []
    page = f'''<!doctype html><meta charset="utf-8"><title>Alysis browser study</title>
<style>body{{font:22px sans-serif;max-width:720px;margin:70px auto}}label{{display:block;margin:20px 0}}input,textarea,button{{display:block;font:inherit;width:100%;box-sizing:border-box}}textarea{{height:100px}}</style>
<h1>Local pilot note</h1><p>This form stores a synthetic note in this temporary study only.</p>
{reference}
<form><label>Project<input id="{fields["project"]}" required></label>
<label>Note<textarea id="{fields["note"]}" required></textarea></label>
<button id="{fields["submit"]}">Save note</button></form><p role="status">No note saved.</p>
{refresh}
<script>document.addEventListener('submit',async(e)=>{{e.preventDefault();
const value={{project:document.getElementById('{fields["project"]}').value,note:document.getElementById('{fields["note"]}').value}};
document.querySelector('[role=status]').textContent='Saving…';
const r=await fetch('/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(value)}});
if(r.ok) document.querySelector('[role=status]').textContent='Saved '+value.project+': '+value.note;
}});</script>'''.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            size = int(self.headers.get("Content-Length", 0))
            if self.path != "/save" or not 0 < size <= 20000:
                self.send_error(400)
                return
            value = json.loads(self.rfile.read(size))
            saved.append(value)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = Path("/var/lib/alysis/checks") / ("browser-agent-" + uuid4().hex)
    output.mkdir(parents=True)
    with TemporaryDirectory(prefix="browser-agent-") as data:
        browser = ManagedBrowserService(
            config=ManagedBrowserConfig(
                data_root=Path(data), linux_display=os.environ["DISPLAY"], max_snapshot_bytes=24000
            ),
            transport_factory=WebSocketCdpTransportFactory(),
        )
        owner = "browser-study"
        url = f"http://127.0.0.1:{server.server_port}/"
        try:
            session = browser.start(owner, allowed_preview_urls_provider=lambda: [url])
            sid = session.session_id
            browser.navigate(owner, sid, url)

            def observe():
                original = {
                    "dom": browser.snapshot(owner, sid, kind="dom"),
                    "text": browser.snapshot(owner, sid, kind="text"),
                }
                if original["dom"]["truncated"]:
                    raise ValueError("Study requires a complete source snapshot")
                source_id, source_chars = sources.add(original)
                compact = {
                    "source_id": source_id,
                    "source_chars": source_chars,
                    "dom": compact_dom_snapshot(original["dom"]["data"]),
                }
                observation_sizes.append(
                    {
                        "full_chars": source_chars,
                        "compact_chars": len(json.dumps(compact, ensure_ascii=False)),
                    }
                )
                return compact if options.observation_mode == "compact" else original

            def emit(value):
                print(json.dumps(value, ensure_ascii=False), flush=True)

            initial = observe()
            if options.scenario == "replaced-form":
                # Real fixture mutation AFTER capture, BEFORE delivery. The
                # retained observation remains exact historical evidence.
                browser.click(owner, sid, "#refresh-form")
                changed = browser.snapshot(owner, sid, kind="dom")["data"]
                original = (
                    json.loads(sources.sources[initial["source_id"]])["dom"]["data"]
                    if options.observation_mode == "compact"
                    else initial["dom"]["data"]
                )
                assert (
                    changed["documents"][0]["nodes"]["backendNodeId"]
                    != original["documents"][0]["nodes"]["backendNodeId"]
                )
            emit(
                {
                    "task": task,
                    "scenario": options.scenario,
                    "fixture_expected": expected,
                    "observation": initial,
                }
            )
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    name, args = request["name"], request.get("arguments", {})
                    if name == "finish":
                        artifact = browser.screenshot(owner, sid)
                        content = browser.read_artifact(owner, sid, artifact.artifact_id)
                        (output / "page.png").write_bytes(
                            base64.b64decode(content["content"], validate=True)
                        )
                        emit(
                            {
                                "passed": saved == [expected],
                                "submissions": len(saved),
                                "expected": expected,
                                "actual": saved,
                                "evidence": str(output),
                                "final_observation": observe(),
                                "observation_sizes": observation_sizes,
                            }
                        )
                        break
                    if name == "browser_source" and set(args) == {"source_id", "offset"}:
                        emit({"ok": True, "source": sources.read(**args)})
                        continue
                    if name == "browser_observe" and not args:
                        pass
                    elif (
                        options.observation_mode == "compact"
                        and name == "browser_type"
                        and set(args) == {"source_id", "node", "text"}
                    ):
                        selector, backend = sources.target(args["source_id"], args["node"])
                        browser.type_text(
                            owner, sid, selector, args["text"], expected_backend_node_id=backend
                        )
                    elif (
                        options.observation_mode == "compact"
                        and name == "browser_click"
                        and set(args) == {"source_id", "node"}
                    ):
                        selector, backend = sources.target(**args)
                        browser.click(owner, sid, selector, expected_backend_node_id=backend)
                    elif (
                        options.observation_mode == "full"
                        and name == "browser_type"
                        and set(args) == {"selector", "text"}
                    ):
                        browser.type_text(owner, sid, args["selector"], args["text"])
                    elif (
                        options.observation_mode == "full"
                        and name == "browser_click"
                        and set(args) == {"selector"}
                    ):
                        browser.click(owner, sid, args["selector"])
                    else:
                        raise ValueError("Unsupported study tool")
                    emit({"ok": True, "observation": observe()})
                except (BrowserError, ValueError, TypeError, KeyError) as error:
                    result = {"ok": False, "error": type(error).__name__}
                    if isinstance(error, KeyError):
                        result["recovery"] = (
                            "Observation unavailable or expired. Observe again before choosing an action."
                        )
                    elif isinstance(error, BrowserNotFoundError):
                        result["recovery"] = (
                            "Target missing or changed. No action dispatched. Observe again before choosing an action."
                        )
                    emit(result)
        finally:
            browser.close_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
