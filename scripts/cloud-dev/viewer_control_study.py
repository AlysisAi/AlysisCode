"""Real RFB input enforcement study on a disposable, private Linux display.

No model, host desktop, public listener or change to the existing lab viewer.
The small RFB client deliberately sends input even when the server is view-only.
This tests the server boundary rather than trusting a viewer UI preference.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from PIL import Image

from alysis_code.ide.browser_observation import compact_dom_snapshot
from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
from alysis_code.ide.linux_viewer import LinuxBrowserHandoff, LinuxViewerError, _pressed_x11_buttons
from alysis_code.ide.managed_browser import (
    BrowserControlError,
    BrowserTimeoutError,
    ManagedBrowserConfig,
    ManagedBrowserService,
)


class RfbProbe:
    """Bounded RFB 3.8/None/raw client for this loopback-only test server."""

    def __init__(self, port=None, *, connection=None):
        self.socket = connection or socket.create_connection(("127.0.0.1", port), timeout=5)
        self.socket.settimeout(5)
        assert self.read(12) == b"RFB 003.008\n"
        self.socket.sendall(b"RFB 003.008\n")
        security = self.read(self.read(1)[0])
        assert 1 in security
        self.socket.sendall(b"\x01")
        assert self.read(4) == b"\0" * 4
        self.socket.sendall(b"\x01")  # Shared; never disconnect another probe.
        init = self.read(24)
        self.width, self.height = struct.unpack(">HH", init[:4])
        assert 0 < self.width <= 1280 and 0 < self.height <= 800
        self.read(struct.unpack(">I", init[20:])[0])
        pixel_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.socket.sendall(b"\0\0\0\0" + pixel_format)
        self.socket.sendall(struct.pack(">BBHi", 2, 0, 1, 0))  # Raw encoding only.

    def read(self, count):
        assert 0 <= count <= 1280 * 800 * 4
        output = bytearray()
        while len(output) < count:
            part = self.socket.recv(count - len(output))
            if not part:
                raise EOFError("Research viewer disconnected")
            output.extend(part)
        return bytes(output)

    def frame(self, full=False):
        width, height = (self.width, self.height) if full else (1, 1)
        self.socket.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))
        pixels = Image.new("RGB", (width, height))
        for _ in range(100):
            kind = self.read(1)[0]
            if kind == 2:  # Bell.
                continue
            if kind == 3:  # Clipboard notifications are data, not commands.
                length = struct.unpack(">3xI", self.read(7))[0]
                self.read(length)
                continue
            assert kind == 0, kind
            count = struct.unpack(">xH", self.read(3))[0]
            assert count <= 4096
            for _ in range(count):
                x, y, w, h, encoding = struct.unpack(">HHHHi", self.read(12))
                assert encoding == 0 and x + w <= self.width and y + h <= self.height
                image = Image.frombytes("RGB", (w, h), self.read(w * h * 4), "raw", "BGRX")
                pixels.paste(image, (x, y))
            return pixels
        raise TimeoutError("No framebuffer reply")

    def key(self, keysym, down):
        self.socket.sendall(struct.pack(">BBHI", 4, int(down), 0, keysym))

    def type_ascii(self, text):
        for letter in text:
            assert ord(letter) < 128
            self.key(ord(letter), True)
            self.key(ord(letter), False)

    def pointer(self, x, y, buttons=0):
        self.socket.sendall(struct.pack(">BBHH", 5, buttons, x, y))

    def clipboard(self, text):
        data = text.encode("ascii")
        self.socket.sendall(struct.pack(">B3xI", 6, len(data)) + data)

    def close(self):
        self.socket.close()


class XState:
    def __init__(self, display):
        self.lib = ctypes.CDLL("libX11.so.6")
        self.lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.lib.XDefaultRootWindow.restype = ctypes.c_ulong
        self.lib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.lib.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
        self.lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.display = self.lib.XOpenDisplay(display.encode())
        assert self.display

    def snapshot(self):
        keys = (ctypes.c_ubyte * 32)()
        self.lib.XQueryKeymap(self.display, keys)
        code = self.lib.XKeysymToKeycode(self.display, 0xFFE1)  # X11 Shift_L.
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        x, y, wx, wy, mask = (ctypes.c_int() for _ in range(5))
        assert self.lib.XQueryPointer(
            self.display,
            self.lib.XDefaultRootWindow(self.display),
            ctypes.byref(root),
            ctypes.byref(child),
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(wx),
            ctypes.byref(wy),
            ctypes.byref(mask),
        )
        return {
            "x": x.value,
            "y": y.value,
            "shift": bool(keys[code // 8] & (1 << (code % 8))),
            "button1": bool(mask.value & (1 << 8)),
            "buttons": _pressed_x11_buttons(self.lib, self.display),
            "mask": mask.value,
        }

    def close(self):
        self.lib.XCloseDisplay(self.display)


def eventually(predicate, timeout=4, tick=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tick:
            tick()
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Expected real desktop state was not reached")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--process-handoff",
        action="store_true",
        help="Test owned single-socket revocation and the production handoff primitive",
    )
    parser.add_argument(
        "--default-optimizations",
        action="store_true",
        help="Reproduce the default input-loop baseline",
    )
    parser.add_argument(
        "--pump-during-control",
        action="store_true",
        help="Request real frames while awaiting remote control acknowledgements",
    )
    options = parser.parse_args()
    if options.process_handoff:
        process_handoff()
        return
    output = Path("/var/lib/alysis/checks") / ("viewer-control-" + uuid4().hex)
    output.mkdir(parents=True)
    report = {
        "kind": "real offline X11/RFB/CDP, no model",
        "checks": {},
        "server_options": {
            "wireframe": options.default_optimizations,
            "scroll_copyrect": options.default_optimizations,
            "pointer_mode": "default" if options.default_optimizations else 1,
        },
        "control_requests": [],
        "framebuffer_pump_during_control": options.pump_during_control,
        "passed": False,
        "evidence": str(output),
    }
    page = b"""<!doctype html><meta charset="utf-8"><title>Viewer control study</title>
<style>body{font:24px sans-serif;padding:40px}input{font:inherit;width:90%}</style>
<h1>Viewer control study</h1><p>Disposable local keyboard, pointer and clipboard test.</p>
<input id="note" aria-label="Viewer text" autofocus><p>Actual pixels arrive over RFB.</p>"""

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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    children, viewers = [], []
    browser = xstate = None
    with TemporaryDirectory(prefix="viewer-control-") as temporary:
        root = Path(temporary)
        logs = (output / "process.log").open("wb")
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
            assert select.select([xvfb.stdout], [], [], 5)[0]
            display = ":" + str(int(xvfb.stdout.readline()))
            env = {**os.environ, "DISPLAY": display}
            children.append(
                subprocess.Popen(["openbox", "--sm-disable"], env=env, stdout=logs, stderr=logs)
            )

            def command(*args, input=None):
                return subprocess.run(
                    args,
                    input=input,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=8,
                ).stdout.strip()

            channel = root / "control"
            channel.touch(mode=0o600)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    display,
                    "-listen",
                    "127.0.0.1",
                    "-no6",
                    "-rfbport",
                    str(port),
                    "-nopw",
                    "-forever",
                    "-shared",
                    "-viewonly",
                    "-novncconnect",
                    "-connect",
                    str(channel),
                    "-nocmds",
                    "-noxdamage",
                    *(
                        []
                        if options.default_optimizations
                        else ["-nowf", "-noscr", "-pointer_mode", "1"]
                    ),
                ],
                env=env,
                stdout=logs,
                stderr=logs,
            )
            children.append(vnc)

            def query(change=None, field="viewonly"):
                args = ["x11vnc", "-display", display, "-connect", str(channel)]
                if change:
                    args += ["-R", change]
                started = time.monotonic()
                entry = {"change": change, "field": field, "acknowledged": False}
                report["control_requests"].append(entry)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(command, *args, "-Q", field)
                    while not pending.done():
                        if options.pump_during_control:
                            for connected in viewers:
                                connected.frame()
                        time.sleep(0.05)
                    lines = pending.result().splitlines()
                if change:
                    assert len(lines) == 2 and lines[0] == "ack=1", lines
                else:
                    assert len(lines) == 1, lines
                entry.update(acknowledged=True, seconds=round(time.monotonic() - started, 3))
                return lines[-1]

            eventually(lambda: channel.exists() and vnc.poll() is None)
            assert query() == "ans=viewonly:1"
            browser = ManagedBrowserService(
                config=ManagedBrowserConfig(data_root=root / "browser", linux_display=display),
                transport_factory=WebSocketCdpTransportFactory(),
            )
            url = f"http://127.0.0.1:{server.server_port}/"
            sid = browser.start(
                "viewer-study", allowed_preview_urls_provider=lambda: [url]
            ).session_id
            browser.navigate("viewer-study", sid, url)
            window = command(
                "xdotool", "search", "--onlyvisible", "--name", "Viewer control study"
            ).splitlines()[-1]
            command("xdotool", "windowactivate", "--sync", window)

            def value():
                raw = browser.snapshot("viewer-study", sid, kind="dom")["data"]
                return next(
                    node["value"]
                    for node in compact_dom_snapshot(raw)["documents"][0]["nodes"]
                    if node.get("attributes", {}).get("id") == "note"
                )

            def clipboard():
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                return result.stdout.strip() if result.returncode == 0 else None

            viewer = RfbProbe(port)
            viewers.append(viewer)
            viewer.frame()
            # x11vnc delays its selection-owner window to survive X display
            # manager startup. Wait for that actual readiness signal before
            # judging clipboard input; early CutText can otherwise be dropped.
            eventually(
                lambda: "created selwin:" in (output / "process.log").read_text(), timeout=15
            )
            xstate = XState(display)
            browser.type_text("viewer-study", sid, "#note", "")
            # X11 clipboard ownership requires a live process. Keep it in the
            # foreground and own its lifetime instead of waiting for a forked
            # clipboard owner to close subprocess.run's captured output pipe.
            owner = subprocess.Popen(
                ["xclip", "-selection", "clipboard", "-quiet"],
                stdin=subprocess.PIPE,
                stdout=logs,
                stderr=logs,
                env=env,
            )
            children.append(owner)
            owner.stdin.write(b"original clipboard")
            owner.stdin.close()
            eventually(lambda: clipboard() == "original clipboard")
            before = xstate.snapshot()
            viewer.type_ascii("blocked")
            viewer.pointer(950, 550, 1)
            viewer.pointer(950, 550, 0)
            viewer.clipboard("blocked clipboard")
            viewer.frame()
            time.sleep(0.2)
            assert value() == "" and clipboard() == "original clipboard"
            assert xstate.snapshot() == before
            report["checks"]["server_watch_blocks_keyboard_pointer_clipboard"] = True

            assert query("noviewonly") == "ans=viewonly:0"
            viewer.type_ascii("viewer input accepted")
            viewer.clipboard("viewer clipboard accepted")
            viewer.pointer(950, 550)
            viewer.frame()
            eventually(lambda: value() == "viewer input accepted")
            eventually(lambda: clipboard() == "viewer clipboard accepted")
            assert xstate.snapshot()["x"] == 950 and xstate.snapshot()["y"] == 550
            report["checks"]["same_connection_control_accepts_input"] = True
            viewer.frame(full=True).save(output / "viewer.png")

            # No key/button-up is sent before revocation: expose held-input semantics.
            viewer.pointer(950, 550, 1)
            viewer.pointer(949, 551, 1)
            viewer.key(0xFFE1, True)
            viewer.frame()
            # Real viewers keep requesting framebuffer updates. Continue that
            # pump while awaiting X11 input; a single pixel reply is not an X11
            # input acknowledgement and the server may buffer pointer events.
            eventually(
                lambda: xstate.snapshot()["shift"] and xstate.snapshot()["button1"],
                tick=viewer.frame,
            )
            report["held_before_revocation"] = xstate.snapshot()
            assert query("viewonly") == "ans=viewonly:1"
            viewer.key(0xFFE1, False)
            viewer.pointer(950, 550, 0)
            viewer.frame()
            report["held_after_revocation_and_blocked_release"] = xstate.snapshot()
            # Trusted teardown completes held input; this is not rollback.
            query("clear_keys")
            query("fakebuttonevent:1,0")
            eventually(lambda: not xstate.snapshot()["shift"] and not xstate.snapshot()["button1"])
            report["checks"]["trusted_release_clears_held_input"] = True

            second = RfbProbe(port)
            viewers.append(second)
            second.frame()
            browser.type_text("viewer-study", sid, "#note", "")
            second.type_ascii("new viewer blocked")
            second.frame()
            time.sleep(0.2)
            assert value() == ""
            report["checks"]["new_connection_inherits_watch_only"] = True
            assert query("noviewonly") == "ans=viewonly:0"
            second.type_ascii("second viewer also accepted")
            second.frame()
            eventually(lambda: value() == "second viewer also accepted")
            report["global_control_allows_other_viewers"] = True
            assert query("viewonly") == "ans=viewonly:1"
            clients = query(field="clients")
            assert clients.startswith("aro=clients:"), clients
            entries = [
                entry.split(":") for entry in clients.removeprefix("aro=clients:").split(",")
            ]
            by_port = {int(entry[2]): entry[0] for entry in entries}
            first_id = by_port[viewer.socket.getsockname()[1]]
            assert first_id.startswith("0x") and int(first_id, 16) > 0
            # Empty `input:` restores defaults. A nonempty string containing
            # no permission letters disables input for existing AND new clients.
            assert query("input:-") == "ans=viewonly:1"
            assert query(f"client_input:{first_id}:KMBC") == "ans=viewonly:1"
            assert query("noviewonly") == "ans=viewonly:0"
            browser.type_text("viewer-study", sid, "#note", "")
            viewer.type_ascii("chosen viewer accepted")
            viewer.clipboard("chosen viewer clipboard")
            viewer.frame()
            eventually(lambda: value() == "chosen viewer accepted")
            eventually(lambda: clipboard() == "chosen viewer clipboard")
            before = xstate.snapshot()
            second.type_ascii("other viewer blocked")
            second.clipboard("other viewer clipboard blocked")
            second.pointer(200, 500, 1)
            second.pointer(200, 500, 0)
            second.frame()
            time.sleep(0.2)
            assert value() == "chosen viewer accepted"
            assert clipboard() == "chosen viewer clipboard"
            assert xstate.snapshot() == before
            report["checks"]["per_client_permission_blocks_other_existing_viewer"] = True
            third = RfbProbe(port)
            viewers.append(third)
            third.frame()
            third.type_ascii("new unauthorized viewer blocked")
            third.pointer(300, 500)
            third.clipboard("new unauthorized clipboard blocked")
            third.frame()
            time.sleep(0.2)
            assert value() == "chosen viewer accepted"
            assert clipboard() == "chosen viewer clipboard"
            assert xstate.snapshot() == before
            report["checks"]["per_client_permission_blocks_new_viewer"] = True
            assert query(f"client_input:{first_id}:") == "ans=viewonly:0"
            viewer.type_ascii("revoked viewer blocked")
            viewer.clipboard("revoked clipboard blocked")
            viewer.pointer(400, 500)
            viewer.frame()
            time.sleep(0.2)
            assert value() == "chosen viewer accepted"
            assert clipboard() == "chosen viewer clipboard"
            assert xstate.snapshot() == before
            report["checks"]["per_client_revocation_blocks_previous_owner"] = True
            # Disconnect a granted client with keys/buttons held while other
            # watch connections remain. Do not assume EOF releases X11 input.
            assert query(f"client_input:{first_id}:KMBC") == "ans=viewonly:0"
            viewer.pointer(950, 550, 1)
            viewer.pointer(949, 551, 1)
            viewer.key(0xFFE1, True)
            # Direct XTest release can leave x11vnc's client button bookkeeping
            # stale. Measure a second press before sending an explicit client
            # release; do not call external release a complete resume protocol.
            try:
                eventually(lambda: xstate.snapshot()["button1"], timeout=1, tick=viewer.frame)
                report["second_press_after_external_release_within_one_second"] = True
            except AssertionError:
                report["second_press_after_external_release_within_one_second"] = False
            viewer.pointer(949, 551, 0)
            viewer.key(0xFFE1, False)
            viewer.frame()
            eventually(lambda: not xstate.snapshot()["shift"] and not xstate.snapshot()["button1"])
            viewer.pointer(950, 550, 1)
            viewer.pointer(949, 551, 1)
            viewer.key(0xFFE1, True)
            eventually(
                lambda: xstate.snapshot()["shift"] and xstate.snapshot()["button1"],
                tick=viewer.frame,
            )
            viewer.close()
            viewers.remove(viewer)
            eventually(lambda: first_id + ":" not in query(field="clients"))
            report["held_after_controlling_client_disconnect"] = xstate.snapshot()
            assert query("viewonly") == "ans=viewonly:1"
            query("clear_keys")
            query("fakebuttonevent:1,0")
            eventually(lambda: not xstate.snapshot()["shift"] and not xstate.snapshot()["button1"])
            report["checks"]["trusted_release_after_disconnect"] = True
            # A vanished client id can receive an ACK without granting anyone
            # control. The coordinator must verify membership, not just ACK.
            assert query(f"client_input:{first_id}:KMBC") == "ans=viewonly:1"
            assert query("noviewonly") == "ans=viewonly:0"
            fourth = RfbProbe(port)
            viewers.append(fourth)
            fourth.frame()
            browser.type_text("viewer-study", sid, "#note", "")
            fourth.type_ascii("reconnected viewer blocked")
            fourth.frame()
            time.sleep(0.2)
            assert value() == ""
            report["checks"]["new_connection_does_not_inherit_vanished_client_grant"] = True
            assert query("viewonly") == "ans=viewonly:1"
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)
            raise
        finally:
            for viewer in viewers:
                viewer.close()
            if xstate:
                xstate.close()
            if browser:
                browser.close_all()
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            logs.close()
            server.shutdown()
            server.server_close()
    report["passed"] = True
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def cleanup_owned_viewer_resources(viewers, handoffs, xstate, browser, children):
    """Attempt every owned cleanup step; never turn failed revocation into resume."""
    failures = []

    def attempt(stage, action):
        try:
            action()
        except Exception as exc:
            # Do not copy arbitrary resource diagnostics into the evidence file.
            failures.append({"stage": stage, "error_type": type(exc).__name__})

    def stop(child):
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    for viewer in viewers:
        attempt("viewer_close", viewer.close)
    for handoff in reversed(handoffs):
        attempt("handoff_close", handoff.close)
    if xstate is not None:
        attempt("x11_connection_close", xstate.close)
    if browser is not None:
        attempt("browser_close", browser.close_all)
    for child in reversed(children):
        attempt("child_stop", lambda child=child: stop(child))
    return failures


def process_handoff():
    """No listener: the test acts as an already-authenticated local relay."""
    output = Path("/var/lib/alysis/checks") / ("process-handoff-" + uuid4().hex)
    output.mkdir(parents=True)
    report = {
        "kind": "real isolated Linux X11/RFB/CDP; socketpair stands in for authenticated relay",
        "passed": False,
        "checks": {},
        "evidence": str(output),
        "model_calls": 0,
    }
    page = b"""<!doctype html><meta charset="utf-8"><title>Owned viewer handoff</title>
    <style>body{font:24px sans-serif;padding:40px}input{font:inherit;width:90%}</style>
    <h1>Owned viewer handoff</h1><p>Real browser and viewer, on a private display.</p>
    <input id="note" aria-label="Viewer text" autofocus>"""
    navigation_entered, navigation_release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                navigation_entered.set()
                navigation_release.wait(10)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    children, viewers, handoffs = [], [], []
    browser = xstate = None
    try:
        with TemporaryDirectory(prefix="process-handoff-") as temporary:
            root = Path(temporary)
            with (output / "process.log").open("wb") as logs:
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
                    assert select.select([xvfb.stdout], [], [], 5)[0]
                    display = ":" + str(int(xvfb.stdout.readline()))
                    env = {**os.environ, "DISPLAY": display}
                    children.append(
                        subprocess.Popen(
                            ["openbox", "--sm-disable"], env=env, stdout=logs, stderr=logs
                        )
                    )
                    browser = ManagedBrowserService(
                        config=ManagedBrowserConfig(
                            data_root=root / "browser", linux_display=display
                        ),
                        transport_factory=WebSocketCdpTransportFactory(),
                    )
                    url = f"http://127.0.0.1:{server.server_port}/"
                    owner = "process-handoff-study"
                    sid = browser.start(
                        owner, allowed_preview_urls_provider=lambda: [url]
                    ).session_id
                    browser.navigate(owner, sid, url)
                    xstate = XState(display)
                    handoff = LinuxBrowserHandoff(browser, owner, sid)
                    handoffs.append(handoff)

                    def grant(target=handoff):
                        client, supplied = socket.socketpair()
                        try:
                            target.begin(supplied)
                            viewer = RfbProbe(connection=client)
                            viewers.append(viewer)
                            viewer.frame()
                            owned_fds = {
                                os.readlink(path)
                                for path in Path(f"/proc/{target._process.pid}/fd").iterdir()
                            }
                            listening = {
                                f"socket:[{fields[9]}]"
                                for table in ("/proc/net/tcp", "/proc/net/tcp6")
                                for line in Path(table).read_text().splitlines()[1:]
                                if (fields := line.split())[3] == "0A"
                            }
                            assert not owned_fds.intersection(listening)
                            assert not any(
                                line.split()[1] == "00000000" and int(line.split()[3], 16) & 2
                                for line in Path("/proc/net/route").read_text().splitlines()[1:]
                            )
                            report["checks"]["no_control_listener_or_public_network"] = True
                            return viewer
                        except BaseException:
                            client.close()
                            supplied.close()
                            raise

                    def value(snapshot=None):
                        # Test-only read oracle during human control. The public
                        # snapshot tool correctly stays fenced. This actual CDP
                        # capture never injects input or resumes the agent.
                        raw = (
                            snapshot
                            or browser._capture_snapshot(
                                browser._owned_session(owner, sid), "dom", 5, None
                            )
                        )["data"]
                        return next(
                            node["value"]
                            for node in compact_dom_snapshot(raw)["documents"][0]["nodes"]
                            if node.get("attributes", {}).get("id") == "note"
                        )

                    def rejected(operation):
                        try:
                            operation()
                        except BrowserControlError:
                            return
                        raise AssertionError("Agent input was not fenced")

                    def hold(viewer, *, extended=False):
                        mask = 129 if extended else 1
                        buttons = {1, 8} if extended else {1}
                        viewer.pointer(950, 550, mask)
                        viewer.pointer(949, 551, mask)
                        viewer.key(0xFFE1, True)
                        eventually(
                            lambda: (
                                xstate.snapshot()["shift"]
                                and set(xstate.snapshot()["buttons"]) == buttons
                            ),
                            tick=viewer.frame,
                        )

                    viewer = grant()
                    rejected(lambda: browser.start("second-visible-owner"))
                    report["checks"]["second_visible_session_rejected"] = True
                    other = LinuxBrowserHandoff(browser, owner, sid)
                    handoffs.append(other)
                    try:
                        grant(other)
                    except BrowserControlError:
                        pass
                    else:
                        raise AssertionError("Second viewer claimed the same browser")
                    rejected(
                        lambda: browser.resume_commands(
                            owner,
                            sid,
                            expected_generation=browser.status(owner, sid).command_generation,
                        )
                    )
                    report["checks"]["second_handoff_and_external_resume_rejected"] = True
                    rejected(lambda: browser.type_text(owner, sid, "#note", "must not type"))
                    viewer.type_ascii("Human edited this draft")
                    eventually(lambda: value() == "Human edited this draft", tick=viewer.frame)
                    viewer.frame(full=True).save(output / "viewer.png")
                    hold(viewer)
                    report["held_before_revocation"] = xstate.snapshot()
                    began = time.monotonic()
                    fresh = handoff.end()
                    report["revocation_seconds"] = round(time.monotonic() - began, 3)
                    assert not xstate.snapshot()["shift"] and not xstate.snapshot()["buttons"]
                    assert value(fresh) == "Human edited this draft"
                    report["checks"]["held_input_released_before_fresh_resume"] = True
                    # Buffered frames can precede EOF. Drain with a strict byte
                    # bound; a server process being reaped is the actual fence.
                    drained = 0
                    while chunk := viewer.socket.recv(65536):
                        drained += len(chunk)
                        assert drained < 8 * 1024 * 1024
                    try:
                        viewer.type_ascii("stale viewer must not type")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    assert value() == "Human edited this draft"
                    report["checks"]["old_socket_closed_and_stale_input_blocked"] = True
                    rejected(
                        lambda: browser.type_text(
                            owner, sid, "#note", "stale", expected_generation=0
                        )
                    )
                    browser.type_text(
                        owner,
                        sid,
                        "#note",
                        "Agent resumed after fresh observation",
                        expected_generation=fresh["command_generation"],
                    )
                    assert value() == "Agent resumed after fresh observation"
                    report["checks"]["old_generation_rejected_current_generation_works"] = True

                    extended = grant()
                    hold(extended, extended=True)
                    report["extended_buttons_held"] = xstate.snapshot()
                    fresh = handoff.end()
                    assert not xstate.snapshot()["shift"] and not xstate.snapshot()["buttons"]
                    # Releasing button 8 can complete Back navigation. The
                    # expected result is fresh actual state, not rollback or an
                    # assumption that the previous page is unchanged.
                    report["extended_release_fresh_url"] = compact_dom_snapshot(fresh["data"])[
                        "documents"
                    ][0]["url"]
                    report["checks"]["extended_button_release_and_fresh_page"] = True
                    browser.navigate(
                        owner, sid, url, expected_generation=fresh["command_generation"]
                    )

                    viewer = grant()
                    hold(viewer)  # No compensating up/down on the new connection.
                    viewer.close()
                    viewers.remove(viewer)
                    eventually(lambda: handoff.status()["state"] == "disconnected")
                    assert browser.status(owner, sid).commands_paused
                    handoff.end()
                    assert not xstate.snapshot()["shift"] and not xstate.snapshot()["buttons"]
                    report["checks"]["new_connection_and_disconnect_cleanup"] = True

                    broken = LinuxBrowserHandoff(
                        browser, owner, sid, executable="/missing-viewer-executable"
                    )
                    handoffs.append(broken)
                    try:
                        grant(broken)
                    except LinuxViewerError:
                        pass
                    else:
                        raise AssertionError("Unavailable viewer unexpectedly started")
                    assert broken.status()["state"] == "paused"
                    rejected(lambda: browser.type_text(owner, sid, "#note", "must not type"))
                    broken.end()
                    report["checks"]["failed_restart_keeps_agent_paused_until_explicit_resume"] = (
                        True
                    )

                    generation = browser.status(owner, sid).command_generation
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        pending = executor.submit(
                            browser.navigate,
                            owner,
                            sid,
                            url + "slow",
                            expected_generation=generation,
                        )
                        assert navigation_entered.wait(5)
                        client, supplied = socket.socketpair()
                        try:
                            try:
                                handoff.begin(supplied, timeout=0.1)
                            except BrowserTimeoutError:
                                pass
                            else:
                                raise AssertionError("Viewer was granted before navigation drained")
                            assert supplied.fileno() == -1
                            assert not handoff.status()["viewer_process_alive"]
                            assert handoff.status()["commands_paused"]
                        finally:
                            client.close()
                            navigation_release.set()
                        pending.result(timeout=10)
                    viewer = grant()
                    handoff.close()
                    assert handoff.status()["state"] == "closed"
                    assert browser.status(owner, sid).commands_paused
                    report["checks"]["inflight_work_drains_before_viewer_grant"] = True
                    browser.close(owner, sid)
                    handoff.close()  # Must not query a browser already disposed.
                    report["checks"]["terminal_close_after_browser_disposal"] = True
                except Exception as exc:
                    report["operation_error_type"] = type(exc).__name__
                    if isinstance(exc, LinuxViewerError):
                        report["operation_control_error"] = str(exc)
                    raise
                finally:
                    navigation_release.set()
                    report["cleanup_failures"] = cleanup_owned_viewer_resources(
                        viewers, handoffs, xstate, browser, children
                    )
                    if report["cleanup_failures"] and "operation_error_type" not in report:
                        raise LinuxViewerError(
                            "Owned study cleanup is incomplete; see cleanup stages."
                        )
        report["passed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, LinuxViewerError):
            report["control_error"] = str(exc)
        raise
    finally:
        server.shutdown()
        server.server_close()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
