"""Synthetic loopback-only site for the explicit cloud-browser study."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/tmp/alysis-browser-study")
PAGE = """<!doctype html><meta charset="utf-8"><title>Cloud workshop</title>
<style>body{font:22px sans-serif;padding:36px}a,button,input{font:inherit;margin:16px}</style>
<h1>Cloud workshop</h1><p>Download the actual CSV, calculate the sum in your computer, and save your result.</p>
<a id="csv" href="/sales.csv" download>Download sales.csv</a>
<a id="review" href="/review" target="_blank">Open review</a>
<input id="file" type="file"><p id="upload-status">No upload selected</p>
<script>document.querySelector('#file').onchange=async e=>{const f=e.target.files[0];
const r=await fetch('/upload',{method:'POST',body:await f.arrayBuffer()});
document.querySelector('#upload-status').textContent=await r.text();};</script>"""
REVIEW = """<!doctype html><meta charset="utf-8"><title>Review</title>
<label>Review note<input id="note"></label><button id="save">Save once</button><p id="receipt">Not saved</p>
<script>document.querySelector('#save').onclick=async e=>{e.target.disabled=true;
const r=await fetch('/save',{method:'POST',body:document.querySelector('#note').value});
document.querySelector('#receipt').textContent=await r.text();};</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/sales.csv":
            data = (ROOT / "sales.csv").read_bytes()
            media = "text/csv"
        elif self.path == "/review":
            data, media = REVIEW.encode(), "text/html; charset=utf-8"
        elif self.path == "/receipt":
            data = json.dumps(
                {
                    name: (ROOT / name).read_text() if (ROOT / name).exists() else None
                    for name in ("note.txt", "upload.bin")
                }
            ).encode()
            media = "application/json"
        else:
            data, media = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        if self.path == "/sales.csv":
            self.send_header("Content-Disposition", 'attachment; filename="sales.csv"')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        if not 0 <= length <= 50 * 1024 * 1024 or self.path not in {"/upload", "/save"}:
            self.send_error(400)
            return
        data = self.rfile.read(length)
        name = "upload.bin" if self.path == "/upload" else "note.txt"
        with (ROOT / name).open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.send_response(200)
        self.end_headers()
        self.wfile.write(("Saved " + name + ": ").encode() + data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8767), Handler).serve_forever()
