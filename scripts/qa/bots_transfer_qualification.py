"""Provider-free qualification on real Linux/Chromium: 50 MiB and crash recovery."""

import hashlib
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.bots.workspaces import WorkspaceStore
from alysis_code.ide.browser_profiles import BrowserProfileStore

root = Path("/tmp/qualification")
root.mkdir(mode=0o700)
store = Store(root / "bots.db")
bot = store.save_bot("owner", BotInput(name="Transfer qualification", purpose="Synthetic test"))
chat = store.create_chat("owner", bot["id"], "Chat")
store.enqueue(
    "owner",
    chat["id"],
    TaskInput(message="Synthetic transfer qualification", request_id=uuid4().hex),
)
task = store.claim("owner")
token = BotRuntime(store, "owner", lambda _: None).task_token(task["id"])
chunk = bytes(range(256)) * 1024
size = MAX_FILE_BYTES
expected = hashlib.sha256(chunk * (size // len(chunk))).hexdigest()
source = store.save_stream(
    "owner", bot["id"], "large.bin", (chunk for _ in range(size // len(chunk))), size
)
received = []


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/download":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="large.bin"')
            self.end_headers()
            for _ in range(size // len(chunk)):
                self.wfile.write(chunk)
        else:
            data = b"""<h1>Unicode form</h1><input id="upload" type="file"><output id="receipt"></output><a href="/download" download>Download</a>
<script>upload.onchange=async()=>{const r=await fetch('/receive',{method:'POST',body:upload.files[0]});receipt.textContent=await r.text()};localStorage.setItem('checkpoint','retained');</script>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_POST(self):
        remaining = int(self.headers["Content-Length"])
        total = remaining
        digest = hashlib.sha256()
        while remaining:
            data = self.rfile.read(min(FILE_CHUNK_BYTES, remaining))
            digest.update(data)
            remaining -= len(data)
        result = {"bytes": total, "sha256": digest.hexdigest()}
        received.append(result)
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{server.server_port}"
profiles = BrowserProfileStore(root / "profiles")
views = ComputerViews()
browser = BrowserComputers(
    "owner",
    views,
    lambda bot, cancel: LinuxBrowserComputer(
        bot, cancel, preview_urls=lambda: [origin], profile_store=profiles, account_id="owner"
    ),
    store=store,
)
desktop = DesktopComputers(
    "owner", views, store=store, workspaces=WorkspaceStore(root / "workspaces")
)


def browse(name, args):
    return browser.call(task, name, args, token, approved_origins=(origin,))


def target(page, key, value):
    node = next(
        n for n in page["dom"]["documents"][0]["nodes"] if n.get("attributes", {}).get(key) == value
    )
    return {"source_id": page["source_id"], "node": node["node"]}


checks = []
try:
    result = desktop.call(
        task, "computer_import_file", {"file_id": source["id"]}, token, approved=True
    )
    assert result["sha256"] == expected and result["bytes"] == size
    result = desktop.call(
        task,
        "computer_run",
        {"command": "cp /workspace/large.bin /workspace/result.bin"},
        token,
        approved=True,
    )
    assert result["exit_code"] == 0, result
    result = desktop.call(task, "computer_save_file", {"name": "result.bin"}, token, approved=True)
    assert result["bytes"] == size and result["sha256"] == expected
    checks.append("50 MiB real bwrap import/export, exact SHA-256")
    page = browse("browser_navigate", {"url": origin})
    browse("browser_upload", {**target(page, "id", "upload"), "file_id": source["id"]})
    deadline = time.monotonic() + 25
    while not received and time.monotonic() < deadline:
        time.sleep(0.05)
    assert received == [{"bytes": size, "sha256": expected}], received
    checks.append("50 MiB actual Chromium file input and HTTP upload, exact receiver digest")
    store.delete_item("owner", bot["id"], source["id"], kind="files")
    page = browse("browser_observe", {})
    downloaded = browse(
        "browser_download", {**target(page, "href", "/download"), "name": "download.bin"}
    )
    assert downloaded["sha256"] == expected and downloaded["bytes"] == size
    checks.append("50 MiB actual Chromium download streamed into library")
    executor = browser._computers[bot["id"]].executor
    owned = executor.browser._sessions[executor.session_id]
    old = executor.session_id
    os.killpg(owned.process.pid, signal.SIGKILL)
    owned.process.wait(timeout=5)
    time.sleep(0.3)
    page = browse("browser_observe", {})
    fresh = browser._computers[bot["id"]].executor
    assert fresh.session_id != old
    assert fresh._profile_lease.recovered
    assert "unclean shutdown" in page["lifetime"]
    assert page["dom"]["documents"][0]["url"] == "about:blank", page["dom"]
    checks.append(
        "actual Chromium SIGKILL: verified stopped group, retained profile, fresh blank page and honest receipt"
    )
    print(
        json.dumps(
            {
                "passed": True,
                "checks": checks,
                "bytes": size,
                "sha256": expected,
                "memory_peak_bytes": int(Path("/sys/fs/cgroup/memory.peak").read_text()),
            }
        ),
        flush=True,
    )
finally:
    browser.close()
    desktop.close()
    server.shutdown()
