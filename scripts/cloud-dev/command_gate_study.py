"""Real offline Chromium command admission study; no provider or viewer handoff.

An isolated X11 display has no VNC listener. HTTP and post-CDP-ack barriers let us
pause real in-flight operations deterministically; no browser result is fabricated.
Artifacts survive failures. This exercises the service primitive, not a Bots UI.
"""

from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from alysis_code.ide.browser_observation import compact_dom_snapshot
from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from alysis_code.ide.managed_browser import (
    BrowserCancelledError,
    BrowserControlError,
    BrowserLimitError,
    BrowserTimeoutError,
    ManagedBrowserConfig,
    ManagedBrowserService,
)


def require_error(expected, operation):
    try:
        operation()
    except expected:
        return expected.__name__
    raise AssertionError(f"Expected {expected.__name__}")


def main():
    output = Path("/var/lib/alysis/checks") / f"command-gate-{uuid.uuid4().hex}"
    output.mkdir(mode=0o700)
    report = {
        "kind": "real offline Linux Chromium; no provider or human viewer",
        "passed": False,
        "checks": {},
        "evidence": str(output),
        "model_calls": 0,
    }
    request_entered, response_release = threading.Event(), threading.Event()
    input_entered, input_release = threading.Event(), threading.Event()
    snapshot_entered, snapshot_release = threading.Event(), threading.Event()
    cancel_resume = threading.Event()
    page = """<!doctype html><meta charset="utf-8"><title>Command pause study</title>
    <style>body{font:22px sans-serif;padding:36px;background:#111827;color:#e5e7eb}
    input,button{font:inherit;padding:12px;margin:10px 0}input{width:90%}</style>
    <h1>Browser command pause study</h1><p>Isolated local Linux display · no viewer connected</p>
    <label>Draft<input id="draft" value="original draft"></label><br>
    <button id="show">Show saved draft</button><p id="value"></p>
    <script>document.querySelector('#show').onclick=()=>{
    document.querySelector('#value').textContent='Saved: '+document.querySelector('#draft').value;};</script>
    """.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                request_entered.set()
                response_release.wait(10)
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

    class BarrierTransport:
        delay_input = False
        delay_snapshot = False
        cancel_after_snapshot = False

        def __init__(self, actual):
            self.actual = actual

        def __getattr__(self, name):
            return getattr(self.actual, name)

        def call(self, method, params=None, **kwargs):
            result = self.actual.call(method, params, **kwargs)
            if (
                self.delay_input
                and method == "Input.dispatchKeyEvent"
                and params.get("type") == "keyDown"
                and params.get("key") == "Backspace"
            ):
                self.delay_input = False
                input_entered.set()
                assert input_release.wait(10), "Input barrier was not released."
            if method == "DOMSnapshot.captureSnapshot":
                if self.cancel_after_snapshot:
                    cancel_resume.set()
                if self.delay_snapshot:
                    self.delay_snapshot = False
                    snapshot_entered.set()
                    assert snapshot_release.wait(10), "Snapshot barrier was not released."
            return result

    class Factory:
        transport = None

        def connect(self, *args, **kwargs):
            self.transport = BarrierTransport(
                WebSocketCdpTransportFactory().connect(*args, **kwargs)
            )
            return self.transport

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    children = []
    browser = None
    logs = (output / "process.log").open("wb")
    temporary_directory = TemporaryDirectory(prefix="command-gate-")
    temporary = temporary_directory.name
    try:
        xvfb = subprocess.Popen(
            [
                "Xvfb",
                "-displayfd",
                "1",
                "-screen",
                "0",
                "1024x640x24",
                "-nolisten",
                "tcp",
                "-ac",
            ],
            stdout=subprocess.PIPE,
            stderr=logs,
        )
        children.append(xvfb)
        assert select.select([xvfb.stdout], [], [], 5)[0], "X display did not start."
        display = ":" + str(int(xvfb.stdout.readline()))
        children.append(
            subprocess.Popen(
                ["openbox", "--sm-disable"],
                env={**os.environ, "DISPLAY": display},
                stdout=logs,
                stderr=logs,
            )
        )
        factory = Factory()
        browser = ManagedBrowserService(
            config=ManagedBrowserConfig(
                data_root=Path(temporary) / "browser", linux_display=display
            ),
            transport_factory=factory,
        )
        url = f"http://127.0.0.1:{server.server_port}/"
        owner = "command-gate-study"
        sid = browser.start(owner, allowed_preview_urls_provider=lambda: [url]).session_id
        browser.navigate(owner, sid, url)

        def draft(snapshot):
            assert snapshot["truncated"] is False
            return next(
                node["value"]
                for node in compact_dom_snapshot(snapshot["data"])["documents"][0]["nodes"]
                if node.get("attributes", {}).get("id") == "draft"
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            navigation = pool.submit(browser.navigate, owner, sid, url + "slow")
            try:
                assert request_entered.wait(5), "Navigation did not reach local HTTP barrier."
                require_error(
                    BrowserTimeoutError, lambda: browser.pause_commands(owner, sid, timeout=0.1)
                )
                state = browser.status(owner, sid)
                assert (
                    state.commands_paused
                    and state.operation_active
                    and state.command_generation == 1
                )
                require_error(
                    BrowserLimitError,
                    lambda: browser.resume_commands(owner, sid, expected_generation=1),
                )
                require_error(
                    BrowserControlError,
                    lambda: browser.type_text(owner, sid, "#draft", "stale"),
                )
                require_error(
                    BrowserCancelledError,
                    lambda: browser.pause_commands(
                        owner,
                        sid,
                        cancel=lambda: browser.status(owner, sid).command_generation == 2,
                    ),
                )
                assert browser.status(owner, sid).commands_paused
            finally:
                response_release.set()
            assert navigation.result(5)["command_generation"] == 0
        fresh = browser.resume_commands(owner, sid, expected_generation=2)
        assert fresh["command_generation"] == 3 and draft(fresh) == "original draft"
        report["checks"]["inflight_navigation"] = {
            "pause_timeout_kept_admission_closed": True,
            "cancelled_pause_kept_admission_closed": True,
            "resume_rejected_before_drain": True,
            "original_operation_completed": True,
            "original_generation": 0,
            "fresh_observation_generation": 3,
        }

        factory.transport.delay_input = True
        exact = "Aster Notes — Δοκιμή 日本語 café 🌱"
        pause_seen = threading.Event()

        def observe_pause():
            if browser.status(owner, sid).commands_paused:
                pause_seen.set()
            return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            typing = pool.submit(
                browser.type_text, owner, sid, "#draft", exact, expected_generation=3
            )
            try:
                assert input_entered.wait(5), "Actual deletion was not acknowledged."
                draining = pool.submit(browser.pause_commands, owner, sid, cancel=observe_pause)
                assert pause_seen.wait(2), "Pause did not fence admission."
                assert not draining.done()
                assert browser.status(owner, sid).operation_active
                require_error(
                    BrowserControlError,
                    lambda: browser.click(owner, sid, "#show", expected_generation=3),
                )
            finally:
                input_release.set()
            assert typing.result(5)["command_generation"] == 3
            drained = draining.result(5)
        assert drained.commands_paused and not drained.operation_active
        assert drained.command_generation == 4
        report["checks"]["compound_input_drain"] = {
            "paused_after_actual_delete_acknowledgement": True,
            "waited_for_remaining_input": True,
            "new_click_rejected": True,
            "drained_status": asdict(drained),
        }
        factory.transport.cancel_after_snapshot = True
        require_error(
            BrowserCancelledError,
            lambda: browser.resume_commands(
                owner, sid, expected_generation=4, cancel=cancel_resume.is_set
            ),
        )
        assert browser.status(owner, sid).commands_paused
        assert browser.status(owner, sid).command_generation == 4
        factory.transport.cancel_after_snapshot = False
        fresh = browser.resume_commands(owner, sid, expected_generation=4)
        assert fresh["command_generation"] == 5 and draft(fresh) == exact
        report["checks"]["cancelled_resume"] = {
            "cancelled_after_actual_fresh_DOM_capture": True,
            "admission_stayed_closed": True,
            "subsequent_fresh_value_exact": True,
        }
        require_error(
            BrowserControlError,
            lambda: browser.type_text(
                owner, sid, "#draft", "obsolete operation", expected_generation=3
            ),
        )
        require_error(
            BrowserControlError,
            lambda: browser.type_text(owner, sid, "#draft", "missing token"),
        )
        assert draft(browser.snapshot(owner, sid, kind="dom")) == exact
        report["checks"]["stale_commands"] = {
            "pre_pause_generation_rejected": True,
            "missing_generation_rejected": True,
            "DOM_value_unchanged": True,
        }

        browser.pause_commands(owner, sid)
        factory.transport.delay_snapshot = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            resuming = pool.submit(browser.resume_commands, owner, sid, expected_generation=6)
            try:
                assert snapshot_entered.wait(5)
                require_error(
                    BrowserTimeoutError, lambda: browser.pause_commands(owner, sid, timeout=0.1)
                )
            finally:
                snapshot_release.set()
            require_error(BrowserControlError, lambda: resuming.result(5))
        assert browser.status(owner, sid).commands_paused
        fresh = browser.resume_commands(owner, sid, expected_generation=7)
        assert fresh["command_generation"] == 8 and draft(fresh) == exact
        report["checks"]["superseded_resume"] = {
            "new_pause_during_actual_snapshot": True,
            "late_resume_rejected": True,
            "fresh_generation": 8,
        }
        browser.click(owner, sid, "#show", expected_generation=8)
        assert f"Saved: {exact}" in browser.snapshot(owner, sid, kind="text")["text"]
        artifact = browser.screenshot(owner, sid)
        with (output / "browser.png").open("wb") as image:
            offset = 0
            while offset < artifact.size_bytes:
                chunk = browser.read_artifact(owner, sid, artifact.artifact_id, offset=offset)
                image.write(base64.b64decode(chunk["content"]))
                offset = chunk["next_offset"]
        report["passed"] = True
        browser.close_all()
        browser = None
    except BaseException as exc:
        report["passed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        response_release.set()
        input_release.set()
        snapshot_release.set()
        try:
            if browser is not None:
                browser.close_all()
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(3)
            server.shutdown()
            server.server_close()
            logs.close()
            temporary_directory.cleanup()
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
