"""Live, provider-free checks for the local Linux lab; no simulated agent responses."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from uuid import uuid4

from PIL import Image, ImageGrab

from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from alysis_code.ide.managed_browser import (
    BrowserCancelledError,
    BrowserOwnershipError,
    BrowserSecurityError,
    ManagedBrowserConfig,
    ManagedBrowserService,
)
from alysis_code.server.settings import resolve_server_settings
from alysis_code.server.store import ServerStore
from alysis_code.server.worker_runner import JobRunner

STATE = Path("/var/lib/alysis")
API = "http://127.0.0.1:7070"
FIXTURE = b"""<!doctype html><meta charset="utf-8"><title>Alysis Linux check</title>
<style>body{font:24px sans-serif;padding:48px}input,button{font:inherit}</style>
<h1>Alysis Linux development check</h1><p>This is a local test page.</p>
<input id="text" autofocus aria-label="Test text"><button id="copy">Show text</button>
<p id="result"></p><script>
document.getElementById('copy').onclick = () => {
  document.getElementById('result').textContent = document.getElementById('text').value;
};</script>"""


def command(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        args, input=input_text, text=True, check=True, capture_output=True, timeout=10
    ).stdout.strip()


def request(path: str, *, post: bool = False) -> dict:
    with urlopen(Request(API + path, data=b"" if post else None), timeout=3) as response:
        return json.load(response)


def health() -> None:
    assert request("/health")["ok"] is True
    command("xdpyinfo")
    command("xdotool", "search", "--onlyvisible", "--class", "chromium")
    with socket.create_connection(("127.0.0.1", 5900), timeout=3) as connection:
        assert connection.recv(12).startswith(b"RFB ")


def check_offline() -> None:
    # Only a TCP handshake, never a request to an inference provider.
    for address in ("1.1.1.1", "8.8.8.8"):
        try:
            connection = socket.create_connection((address, 443), timeout=2)
        except OSError:
            continue
        connection.close()
        raise AssertionError("Public internet is reachable; check the internal Compose network.")
    assert os.getuid() == 1000
    status = Path("/proc/self/status").read_text()
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert int(fields["CapEff"].strip(), 16) == 0
    assert fields["NoNewPrivs"].strip() == "1"
    print("PASS: non-root, no effective capabilities, no-new-privileges, public egress blocked")


def check_worker() -> None:
    settings = resolve_server_settings(data_dir=STATE / "server")
    assert settings.worker_sandbox_mode == "strict"
    assert settings.worker_network == "off"
    store = ServerStore(settings)
    run_id = request("/v1/runs/empty", post=True)["run_id"]
    workspace = store.get_run_paths(run_id).workspace_dir
    challenge = uuid4().hex
    runner = JobRunner(settings, store)
    job_id = None
    with TemporaryDirectory(prefix="outside-worker-", dir=STATE) as outside:
        marker = Path(outside) / "marker"
        marker.write_text(challenge)
        # Uses the actual server queue and Linux worker runner. This is a shell
        # infrastructure check, not an LLM coding task or an HTTP prompt test.
        program = (
            "import os; from pathlib import Path; "
            "assert os.getuid() == 1000; "
            f"assert not Path({str(marker)!r}).exists(); "
            "assert len(Path('/proc/net/route').read_text().splitlines()) == 1; "
            f"Path('worker-check.txt').write_text({challenge!r}); "
            "print('isolated worker completed')"
        )
        try:
            job_id = runner.start_job(run_id=run_id, command=[sys.executable, "-c", program])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = runner.get_status(job_id)
                if status.status in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError("Linux worker did not finish within 30 seconds.")
            assert status.status == "succeeded", runner.read_logs(job_id)
            assert (workspace / "worker-check.txt").read_text() == challenge
            print("PASS: HTTP workspace creation, real queued worker, filesystem/network isolation")
        finally:
            if job_id is not None:
                runner.cancel_job(job_id)
            runner.close()


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(FIXTURE)))
        self.end_headers()
        self.wfile.write(FIXTURE)

    def log_message(self, *_args: object) -> None:
        pass


def check_browser(url: str, output: Path, *, visible: bool = False) -> None:
    existing_windows = set(
        command("xdotool", "search", "--onlyvisible", "--class", "chromium").splitlines()
    )
    with TemporaryDirectory(prefix="managed-browser-") as data_dir:
        browser = ManagedBrowserService(
            config=ManagedBrowserConfig(
                data_root=Path(data_dir), linux_display=os.environ["DISPLAY"] if visible else None
            ),
            transport_factory=WebSocketCdpTransportFactory(),
        )
        owner = "linux-dev-check"
        try:
            session = browser.start(owner)
            try:
                browser.navigate(owner, session.session_id, url)
            except BrowserSecurityError:
                pass
            else:
                raise AssertionError("Default browser policy unexpectedly allowed loopback.")
            browser.close(owner, session.session_id)
            # Use the existing explicit owned-preview policy, without changing defaults.
            session = browser.start(owner, allowed_preview_urls_provider=lambda: [url])
            sid = session.session_id
            browser.navigate(owner, sid, url)
            challenge = f"Linux — Ελληνικά — 日本語 — {uuid4().hex}"
            # An empty-field smoke test cannot detect broken select-all/delete.
            browser.type_text(owner, sid, "#text", "old value that must disappear")
            browser.type_text(owner, sid, "#text", "")
            browser.type_text(owner, sid, "#text", "append after clearing", replace=False)
            browser.click(owner, sid, "#copy")
            assert (
                browser.snapshot(owner, sid, kind="text")["text"].splitlines()[-1]
                == "append after clearing"
            )
            browser.type_text(owner, sid, "#text", challenge)
            browser.click(owner, sid, "#copy")
            assert browser.snapshot(owner, sid, kind="text")["text"].splitlines()[-1] == challenge
            try:
                browser.snapshot("different-owner", sid)
            except BrowserOwnershipError:
                pass
            else:
                raise AssertionError("A different owner read this browser session.")
            try:
                browser.type_text(owner, sid, "#text", "must not be entered", cancel=lambda: True)
            except BrowserCancelledError:
                pass
            else:
                raise AssertionError("Cancelled input was dispatched.")
            browser.click(owner, sid, "#copy")
            assert browser.snapshot(owner, sid, kind="text")["text"].splitlines()[-1] == challenge
            if visible:
                deadline = time.monotonic() + 10
                while True:
                    windows = (
                        set(
                            command(
                                "xdotool", "search", "--onlyvisible", "--class", "chromium"
                            ).splitlines()
                        )
                        - existing_windows
                    )
                    matches = [
                        window
                        for window in windows
                        if "Alysis Linux check" in command("xdotool", "getwindowname", window)
                    ]
                    if len(matches) == 1:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            "Managed browser did not expose one matching visible window."
                        )
                    time.sleep(0.1)
                # Exercise both input routes against the SAME browser page. This
                # is a lab interoperability check, not a production takeover UI.
                window = matches[0]
                command("xdotool", "windowactivate", "--sync", window)
                browser.type_text(owner, sid, "#text", challenge)
                command("xdotool", "key", "--clearmodifiers", "End")
                command("xdotool", "type", "--clearmodifiers", " + desktop input")
                browser.click(owner, sid, "#copy")
                assert (
                    browser.snapshot(owner, sid, kind="text")["text"].splitlines()[-1]
                    == challenge + " + desktop input"
                )
            artifact = browser.screenshot(owner, sid)
            content = browser.read_artifact(owner, sid, artifact.artifact_id)
            assert not content["truncated"]
            pixels = base64.b64decode(content["content"], validate=True)
            with Image.open(io.BytesIO(pixels)) as screenshot:
                screenshot.verify()
            (
                output / ("managed-visible-page.png" if visible else "managed-browser.png")
            ).write_bytes(pixels)
            if visible:
                # Page.captureScreenshot above flushes Chromium's compositor;
                # a DOM read alone can precede the visible window's repaint.
                ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).save(
                    output / "managed-visible-desktop.png"
                )
            print(
                f"PASS: {'visible' if visible else 'headless'} sandboxed Chromium, navigation/owner policy, Unicode, cancellation, screenshot"
            )
        finally:
            browser.close_all()


def check_desktop(url: str, output: Path) -> None:
    window = command("xdotool", "search", "--onlyvisible", "--class", "chromium").splitlines()[0]
    command("xdotool", "windowactivate", "--sync", window)
    command("xdotool", "key", "--clearmodifiers", "ctrl+l")
    command("xdotool", "type", "--clearmodifiers", url)
    command("xdotool", "key", "Return")
    # Wait for the fixture title instead of guessing the browser load time.
    deadline = time.monotonic() + 10
    while "Alysis Linux check" not in command("xdotool", "getwindowname", window):
        if time.monotonic() > deadline:
            raise TimeoutError("Desktop browser did not load the local fixture.")
        time.sleep(0.1)
    challenge = uuid4().hex
    command("xdotool", "type", "--clearmodifiers", challenge)
    command("xdotool", "key", "--clearmodifiers", "ctrl+a", "ctrl+c")
    assert command("xclip", "-o", "-selection", "clipboard") == challenge
    command("xdotool", "key", "Tab", "Return")
    command("xdotool", "mousemove", "--sync", "400", "400")
    position = command("xdotool", "getmouselocation", "--shell")
    assert "X=400\nY=400" in position
    screenshot = ImageGrab.grab(xdisplay=os.environ["DISPLAY"])
    assert screenshot.size == (1280, 800)
    assert any(low != high for low, high in screenshot.getextrema())
    screenshot.save(output / "desktop.png")
    print("PASS: visible Linux desktop, keyboard, clipboard, pointer movement, screen capture")


def persistence(mode: str) -> None:
    manifest = STATE / "persistence-check.json"
    store = ServerStore(resolve_server_settings(data_dir=STATE / "server"))
    if mode == "prepare-persistence":
        marker = uuid4().hex
        run_id = request("/v1/runs/empty", post=True)["run_id"]
        (store.get_run_paths(run_id).workspace_dir / "persistence.txt").write_text(marker)
        Path(f"/tmp/{marker}").touch()
        manifest.write_text(json.dumps({"marker": marker, "run_id": run_id}))
        print("Prepared workspace persistence check; ready to recreate the lab container.")
    else:
        data = json.loads(manifest.read_text())
        assert not Path(f"/tmp/{data['marker']}").exists(), "Container was not recreated."
        workspace = store.get_run_paths(data["run_id"]).workspace_dir
        assert (workspace / "persistence.txt").read_text() == data["marker"]
        print("PASS: server workspace and file survived container replacement")


def smoke() -> None:
    health()
    check_offline()
    check_worker()
    output = STATE / "checks" / uuid4().hex
    output.mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        check_browser(url, output)
        check_browser(url, output, visible=True)
        check_desktop(url, output)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    print(f"Screenshots: {output}")
    print("Linux infrastructure checks passed. No model or Azure API was called.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "health":
        health()
    elif mode == "smoke":
        smoke()
    elif mode in {"prepare-persistence", "verify-persistence"}:
        persistence(mode)
    else:
        raise SystemExit("Expected health, smoke, prepare-persistence, or verify-persistence.")
