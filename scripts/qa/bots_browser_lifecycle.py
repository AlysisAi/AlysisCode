"""Provider-free checks against real private Linux browser processes.

Run through stdin in the disposable browser study container, with its mounted
source tree. This does not touch the app's bot, display, credentials or database.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken

received = threading.Event()
release_response = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            received.set()
            release_response.wait(20)
        page = b"<!doctype html><title>Lifecycle check</title><p>Private lifecycle study.</p>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        try:
            self.wfile.write(page)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}/"
executors = []
clock = [0.0]
views = ComputerViews()


def factory(bot, cancel):
    executor = LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (url,))
    executors.append(executor)
    return executor


computers = BrowserComputers("study", views, factory, idle_seconds=10, clock=lambda: clock[0])
task = {"id": "study-task", "bot_id": "study-bot"}
cancel = threading.Event()
token = EventCancellationToken(cancel)
outcome = []


def slow_navigation():
    try:
        computers.call(task, "browser_navigate", {"url": url + "slow"}, token)
        outcome.append("unexpected_success")
    except CooperativeCancellationError:
        outcome.append("cancelled")


try:
    assert views.frame("study", "study-bot")["state"] == "not_running"
    worker = threading.Thread(target=slow_navigation)
    worker.start()
    assert received.wait(15), "Browser did not reach the delayed fixture"
    started = time.monotonic()
    cancel.set()
    worker.join(timeout=8)
    assert not worker.is_alive(), "Cancellation did not settle"
    assert outcome == ["cancelled"], outcome
    assert views.frame("study", "study-bot")["state"] == "not_running"
    assert all(p.poll() is not None for p in executors[0]._processes)
    cancelled_seconds = round(time.monotonic() - started, 3)
    release_response.set()
    cancel.clear()
    observation = computers.call(task, "browser_navigate", {"url": url}, token)
    assert observation["dom"]["documents"][0]["title"] == "Lifecycle check"
    assert views.frame("study", "study-bot")["state"] == "live"
    assert views.frame("study", "other-bot")["state"] == "not_running"
    clock[0] = 100
    computers.reap()
    assert executors[-1].alive(), "Active work must not expire"
    computers.release(task["id"], cancelled=False)
    clock[0] += 11
    computers.reap()
    assert views.frame("study", "study-bot")["state"] == "not_running"
    assert all(p.poll() is not None for executor in executors for p in executor._processes)
    print(
        json.dumps(
            {
                "passed": True,
                "cancelled_seconds": cancelled_seconds,
                "checks": [
                    "no initial allocation",
                    "in-flight HTTP cancellation",
                    "revoked view",
                    "fresh retry",
                    "other bot cannot view",
                    "active work survives idle deadline",
                    "idle processes closed",
                    "all owned X/WM processes reaped",
                ],
                "model_calls": 0,
            }
        ),
        flush=True,
    )
finally:
    release_response.set()
    computers.close()
    server.shutdown()
