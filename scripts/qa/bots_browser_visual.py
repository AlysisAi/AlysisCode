"""Synthetic canvas for actual visual perception; no target coordinates in the DOM.

The PNG contains the labels. A real click posts canvas-relative coordinates;
only the test server knows which rectangle matches the user's requested label.
"""

from __future__ import annotations

import base64
import io
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

PAGE = """<!doctype html><meta charset="utf-8"><title>Visual browser study</title>
<style>body{font:24px system-ui;padding:35px}canvas{display:block;border:2px solid #999}</style>
<h1>Visual browser study</h1><p>Choose the requested tile using its visible label.</p>
<canvas width="900" height="320"></canvas><p role="status">No selection submitted.</p>
<script>
const canvas=document.querySelector('canvas');const ctx=canvas.getContext('2d');
const img=new Image();img.onload=()=>{ctx.drawImage(img,0,0);document.querySelector('[role=status]').textContent='Tiles ready.';};img.src='/picture';
canvas.onclick=async e=>{const rect=canvas.getBoundingClientRect();
const x=(e.clientX-rect.left-2),y=(e.clientY-rect.top-2);
const r=await fetch('/save',{method:'POST',body:JSON.stringify({x,y})});
document.querySelector('[role=status]').textContent=await r.text();};
</script>"""


def fixture():
    label = "CEDAR" + uuid4().hex[:4].upper()
    other = "MAPLE" + uuid4().hex[:4].upper()
    boxes = [(60, 70, 390, 245), (510, 70, 840, 245)]
    target = random.SystemRandom().randrange(2)
    image = Image.new("RGB", (900, 320), "#f4f6fa")
    draw = ImageDraw.Draw(image)
    font_path = next(
        p
        for p in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        )
        if Path(p).exists()
    )
    font = ImageFont.truetype(font_path, 42)
    for index, box in enumerate(boxes):
        draw.rounded_rectangle(
            box,
            radius=24,
            fill="#e8b83f" if index == target else "#72bbdf",
            outline="#243c5d",
            width=3,
        )
        text = label if index == target else other
        width = draw.textlength(text, font=font)
        draw.text(((box[0] + box[2] - width) / 2, 132), text, font=font, fill="#12243b")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return {
        "page": PAGE,
        "image": base64.b64encode(out.getvalue()).decode(),
        "target": boxes[target],
        "label": label,
    }


def linux_checks():
    from alysis_code.bots.browser import (
        BrowserComputers,
        BrowserImageResult,
        BrowserObservationExpired,
    )
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.cancellation import EventCancellationToken

    data = fixture()
    saved = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            picture = self.path == "/picture"
            payload = base64.b64decode(data["image"]) if picture else data["page"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if picture else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            left, top, right, bottom = data["target"]
            assert left <= value["x"] <= right and top <= value["y"] <= bottom, value
            saved.append(value)
            payload = ("Selected: " + data["label"]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    computers = BrowserComputers(
        "visual",
        ComputerViews(),
        lambda bot, cancel: LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (origin + "/",)),
    )
    task = {"id": "visual-task", "bot_id": "visual-bot"}
    token = EventCancellationToken(threading.Event())

    def call(name, args=None):
        return computers.call(
            task, name, args or {}, token, approved_origins=(origin,), image_input=True
        )

    checks = []
    try:
        observation = call("browser_navigate", {"url": origin})
        deadline = time.monotonic() + 5
        while "Tiles ready." not in json.dumps(observation):
            assert time.monotonic() < deadline
            observation = call("browser_observe")
        screenshot = call("browser_screenshot")
        assert isinstance(screenshot, BrowserImageResult)
        payload = base64.b64decode(screenshot.image_url.split(",", 1)[1])
        assert Image.open(io.BytesIO(payload)).size == (
            screenshot.observation["frame"]["width"],
            screenshot.observation["frame"]["height"],
        )
        checks.append("Actual owned PNG is separate from DOM text")
        executor = computers._computers[task["bot_id"]].executor
        executor.browser.navigate(executor.owner, executor.session_id, origin + "/?changed")
        try:
            call(
                "browser_visual_click",
                {"source_id": screenshot.observation["source_id"], "x": 10, "y": 10},
            )
        except BrowserObservationExpired:
            checks.append("Page navigation makes frame stale without dispatch")
        else:
            raise AssertionError("Stale frame accepted")
        assert saved == [] and executor.alive()
        screenshot = call("browser_screenshot")
        source = screenshot.observation["source_id"]
        frame = computers._computers[task["bot_id"]].visual_frame[1]
        # Independent no-model driver obtains the real canvas box. The live
        # model study gets only pixels and must choose its own coordinates.
        node = next(
            n
            for n in screenshot.observation["dom"]["documents"][0]["nodes"]
            if n["name"] == "CANVAS"
        )
        session = executor.browser._owned_session(executor.owner, executor.session_id)
        node_id = executor.browser._query_node(session, node["selector"], 5, None)
        box = session.transport.call(
            "DOM.getBoxModel", {"nodeId": node_id}, session_id=session.cdp_session_id, timeout=5
        )["model"]["content"]
        left, top, right, bottom = data["target"]
        x = int((box[0] + (left + right) / 2) * frame.width / frame.css_width)
        y = int((box[1] + (top + bottom) / 2) * frame.height / frame.css_height)
        after = call("browser_visual_click", {"source_id": source, "x": x, "y": y})
        assert len(saved) == 1
        actual = after.observation if isinstance(after, BrowserImageResult) else after
        assert actual["action"]["dispatched"] and "Selected: " + data["label"] in json.dumps(actual)
        checks.append("Frame-grounded click has one matching real HTTP receipt and new observation")
        try:
            call("browser_visual_click", {"source_id": source, "x": x, "y": y})
        except BrowserObservationExpired:
            checks.append("Used visual source cannot replay")
        else:
            raise AssertionError("Visual source replayed")
        assert len(saved) == 1
        print(
            json.dumps({"passed": True, "checks": checks, "saved": saved, "model_calls": 0}),
            flush=True,
        )
    finally:
        computers.close()
        server.shutdown()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    args = parser.parse_args()
    if not args.linux:
        parser.error("Run --linux only inside the isolated disposable study container")
    linux_checks()
