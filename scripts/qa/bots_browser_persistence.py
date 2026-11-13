"""Real offline Linux profile continuity; no models or external accounts.

Run `seed`, then `restore` in separate disposable containers sharing only the
study volume. `crash` deliberately exits its container with a dirty profile;
`reject-dirty` verifies the next container cannot silently reuse that profile.
These are Chromium disk-profile checks, never microVM snapshot evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.cancellation import EventCancellationToken
from alysis_code.ide.browser_profiles import BrowserProfileError, BrowserProfileStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("seed", "restore", "crash", "reject-dirty"))
    parser.add_argument("--root", type=Path, default=Path("/var/lib/alysis/profile-study"))
    args = parser.parse_args()
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker_path = args.root / "synthetic-marker.json"
    if args.phase == "seed":
        with marker_path.open("x") as stream:
            json.dump("Aster Ω — 日本語 — " + uuid4().hex, stream, ensure_ascii=False)
    marker = json.loads(marker_path.read_text())
    profiles = BrowserProfileStore(args.root / "profiles")
    if args.phase == "reject-dirty":
        try:
            lease = profiles.acquire("research", "crashed-bot")
        except BrowserProfileError:
            print(
                json.dumps(
                    {
                        "passed": True,
                        "phase": args.phase,
                        "dirty_reuse_blocked": True,
                        "model_calls": 0,
                    }
                )
            )
            return
        lease.release()
        raise AssertionError("A crashed browser profile was reused")

    requests = []

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            seed = (
                f"localStorage.setItem('research-note', {json.dumps(marker)});"
                "document.cookie='research_note='+encodeURIComponent(localStorage.getItem('research-note'))+'; Max-Age=3600; SameSite=Lax';"
                if self.path == "/seed"
                else ""
            )
            page = (
                '<!doctype html><meta charset="utf-8"><title>Browser storage study</title>'
                '<h1>Saved browser data</h1><pre id="result"></pre><script>'
                + seed
                + "document.querySelector('#result').textContent=JSON.stringify({note:localStorage.getItem('research-note'),cookie:decodeURIComponent(document.cookie)});"
                + "</script>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 8768), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:8768/"
    views, executors, clock = ComputerViews(), [], [0.0]

    def factory(bot_id, cancel):
        executor = LinuxBrowserComputer(
            bot_id,
            cancel,
            preview_urls=lambda: (url,),
            profile_store=profiles,
            account_id="research",
        )
        executors.append(executor)
        return executor

    computers = BrowserComputers("research", views, factory, idle_seconds=1, clock=lambda: clock[0])
    bot_id = "crashed-bot" if args.phase == "crash" else "persistent-bot"
    task = {"id": "persistence-task", "bot_id": bot_id}
    token = EventCancellationToken(threading.Event())

    def read(path):
        result = computers.call(task, "browser_navigate", {"url": url + path}, token)
        return json.dumps(result, ensure_ascii=False)

    def observe_until(expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = computers.call(task, "browser_observe", {}, token)
            raw = json.dumps(result, ensure_ascii=False)
            if expected in raw:
                return raw
            time.sleep(0.05)
        raise AssertionError("Expected actual browser storage was not observed")

    try:
        assert views.frame("research", bot_id)["state"] == "not_running"
        blank = computers.call(task, "browser_observe", {}, token)
        assert "about:blank" in json.dumps(blank)
        assert not requests, "Saved tabs unexpectedly made requests at startup"
        try:
            unexpected = profiles.acquire("research", bot_id)
        except BrowserProfileError:
            pass
        else:
            unexpected.release()
            raise AssertionError("Concurrent profile lease was accepted")
        read("seed" if args.phase in ("seed", "crash") else "verify")
        raw = observe_until(marker)
        assert "research_note=" + marker in raw
        if args.phase == "crash":
            executor = executors[-1]
            session = executor.browser._sessions[executor.session_id]
            session.process.kill()
            session.process.wait(timeout=3)
            print(
                json.dumps(
                    {
                        "passed": True,
                        "phase": args.phase,
                        "browser_killed": True,
                        "profile_not_marked_clean": True,
                        "model_calls": 0,
                    }
                ),
                flush=True,
            )
            # End the disposable container; no success path or clean marker.
            os._exit(0)

        started = time.monotonic()
        computers.release(task["id"], cancelled=False)
        clock[0] += 2
        computers.reap()
        idle_seconds = round(time.monotonic() - started, 3)
        assert views.frame("research", bot_id)["state"] == "not_running"
        assert all(p.poll() is not None for executor in executors for p in executor._processes)
        request_count = len(requests)
        blank = computers.call(task, "browser_observe", {}, token)
        assert "about:blank" in json.dumps(blank)
        assert len(requests) == request_count, "Reopen replayed the previous page"
        read("verify")
        observe_until("research_note=" + marker)
        screenshot = executors[-1].capture()
        (args.root / (args.phase + "-computer.png")).write_bytes(screenshot)
        computers.release(task["id"], cancelled=False)
        clock[0] += 2
        computers.reap()

        # Same-origin storage must be absent for another bot and another account.
        for account, other_bot in (("research", "other-bot"), ("other-account", bot_id)):
            other = LinuxBrowserComputer(
                other_bot,
                lambda: False,
                preview_urls=lambda: (url,),
                profile_store=profiles,
                account_id=account,
            )
            try:
                other.browser.navigate(other.owner, other.session_id, url + "verify")
                result = other.browser.snapshot(other.owner, other.session_id, kind="dom")
                assert marker not in json.dumps(result, ensure_ascii=False)
            finally:
                other.close()
        report = {
            "passed": True,
            "phase": args.phase,
            "model_calls": 0,
            "checks": [
                "fresh blank tab",
                "exclusive lease",
                "persistent localStorage",
                "persistent synthetic cookie",
                "idle process retirement",
                "fresh process restoration",
                "no automatic page replay",
                "bot and account separation",
            ],
            "idle_close_seconds": idle_seconds,
            "profile_bytes": sum(
                p.stat().st_size
                for p in (args.root / "profiles").rglob("*")
                if p.is_file() and not p.is_symlink()
            ),
        }
        (args.root / (args.phase + "-report.json")).write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        computers.close()
        server.shutdown()


if __name__ == "__main__":
    main()
