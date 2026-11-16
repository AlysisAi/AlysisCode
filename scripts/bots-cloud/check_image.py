"""Qualify an actual disposable Linux worker image without accounts or model calls.

Run with its managed Python's -I flag, as UID 1000, in the same namespace-capable
container/VM used by the worker. The only website is an owned loopback fixture.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import distribution
from pathlib import Path
from uuid import uuid4

from PIL import Image

from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
from alysis_code.ide.managed_browser import BrowserLimitError


def package_matches_source():
    root = Path("/opt/alysis")
    package = distribution("alysis-code")
    expected = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if package.version != expected:
        raise RuntimeError("Installed engine version differs from the image source.")
    count = 0
    for source in (root / "src/alysis_code").rglob("*.py"):
        installed = Path(package.locate_file(source.relative_to(root / "src")))
        if not installed.is_file() or installed.read_bytes() != source.read_bytes():
            raise RuntimeError(f"Installed engine differs from source: {source.relative_to(root)}")
        count += 1
    if not count:
        raise RuntimeError("Image source inventory is missing.")
    return {"version": package.version, "matching_modules": count}


def visible_frame(capture, output):
    data = capture()
    with Image.open(io.BytesIO(data)) as image:
        if not any(low != high for low, high in image.convert("RGB").getextrema()):
            raise RuntimeError("Computer returned a uniform frame instead of a visible desktop.")
        size = image.size
    output.write_bytes(data)
    return {"size": size, "sha256": hashlib.sha256(data).hexdigest()}


def wait_for_panel(query):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if query():
            return
        time.sleep(0.1)
    raise RuntimeError("The desktop panel did not become visible.")


def click_launcher(desktop, owner, name):
    """Hit the installed launcher's current position, including a shrinking dock."""
    config = desktop.call(owner, "run", {"command": "cat /tmp/alysis-dock/tint2rc"})["output"]
    settings = dict(line.split(" = ", 1) for line in config.splitlines() if " = " in line)
    launchers = [
        Path(line.split(" = ", 1)[1]).stem
        for line in config.splitlines()
        if line.startswith("launcher_item_app = ")
    ]
    geometry = desktop.call(
        owner,
        "run",
        {"command": "xdotool search --onlyvisible --class tint2 getwindowgeometry --shell"},
    )["output"]
    bounds = dict(line.split("=", 1) for line in geometry.splitlines() if "=" in line)
    panel_padding = int(settings["panel_padding"].split()[0])
    padding, _, spacing = map(int, settings["launcher_padding"].split())
    size = int(settings["launcher_icon_size"])
    x = (
        int(bounds["X"])
        + panel_padding
        + padding
        + size // 2
        + launchers.index(name) * (size + spacing)
    )
    y = int(bounds["Y"]) + int(bounds["HEIGHT"]) // 2
    desktop.human_input({"kind": "click", "x": x, "y": y, "button": 1})


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            '<!doctype html><meta charset="utf-8"><title>Cloud image check</title>'
            '<body style="background:#e6f3ef;color:#132b24;font:28px sans-serif;padding:48px">'
            "<h1>Cloud browser is working</h1><p>Αθήνα · 東京</p></body>"
        ).encode()
        if "image-check=retained" in self.headers.get("Cookie", ""):
            body += b"<p>Fixture cookie retained</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "image-check=retained; Max-Age=3600; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def live_desktop_check(desktop, owner):
    """Real private-jail RFB handshake and pixels during an agent operation."""
    client, sequence, buffer = uuid4().hex, -1, bytearray()

    def exchange(data=b"", action="exchange"):
        nonlocal sequence
        sequence += 1
        value = desktop.rfb(
            {
                "client_id": client,
                "sequence": sequence,
                "action": action,
                "data": base64.b64encode(data).decode(),
            }
        )
        if value["sequence"] != sequence:
            raise RuntimeError("Live desktop receipt out of order.")
        buffer.extend(base64.b64decode(value["data"], validate=True))

    def read(size):
        deadline = time.monotonic() + 5
        while len(buffer) < size:
            if time.monotonic() > deadline:
                raise RuntimeError("Live desktop stream stalled.")
            exchange()
        value = bytes(buffer[:size])
        del buffer[:size]
        return value

    try:
        with ThreadPoolExecutor() as pool:
            operation = pool.submit(desktop.call, owner, "run", {"command": "sleep 2"})
            time.sleep(0.2)
            start = time.monotonic()
            exchange(action="open")
            version = read(12)
            if version != b"RFB 003.008\n":
                raise RuntimeError("Unexpected private desktop protocol.")
            if time.monotonic() - start > 1.5 or operation.done():
                raise RuntimeError("Agent execution blocked the live display.")
            exchange(version)
            security = read(read(1)[0])
            if 1 not in security:
                raise RuntimeError("Private inherited desktop channel unavailable.")
            exchange(b"\x01")
            if read(4) != b"\0" * 4:
                raise RuntimeError("Private desktop handshake rejected.")
            exchange(b"\x01")
            info = read(24)
            width, height = struct.unpack(">HH", info[:4])
            name_size = struct.unpack(">I", info[20:24])[0]
            if not (0 < width <= 2560 and 0 < height <= 1600 and name_size <= 4096):
                raise RuntimeError("Invalid desktop geometry.")
            read(name_size)
            exchange(
                b"\x02\0\0\x01"
                + struct.pack(">i", 0)
                + b"\x03\0"
                + struct.pack(">HHHH", 0, 0, min(width, 64), min(height, 64))
            )
            header = read(4)
            rectangles = struct.unpack(">H", header[2:])[0]
            if header[0] != 0 or not 1 <= rectangles <= 16:
                raise RuntimeError("No live framebuffer update.")
            pixels = 0
            for _ in range(rectangles):
                x, y, w, h, encoding = struct.unpack(">HHHHi", read(12))
                if encoding != 0 or w * h == 0 or x + w > width or y + h > height:
                    raise RuntimeError("Unexpected raw framebuffer update.")
                pixels += len(read(w * h * 4))
            if operation.result()["exit_code"] != 0:
                raise RuntimeError("Concurrent agent command failed.")
        desktop.human_input({"kind": "pointer", "x": 5, "y": 5, "buttons": 0})
        desktop.human_input({"kind": "release"})
        return {
            "protocol": "RFB 3.8",
            "size": [width, height],
            "pixel_bytes": pixels,
            "independent_of_agent_command": True,
            "pointer_input": True,
        }
    finally:
        exchange(action="close")


def shared_desktop_check(output, url):
    from alysis_code.bots.linux_display import PrivateLinuxDisplay
    from alysis_code.ide.browser_profiles import BrowserProfileStore

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace, launchers = root / "workspace", root / "launchers"
        workspace.mkdir()
        launchers.mkdir()
        display = PrivateLinuxDisplay()
        desktop = browser = None
        try:
            desktop = OfflineLinuxDesktop(
                "shared-check", workspace, shared_display=display.name, launchers=launchers
            )
            live_display = live_desktop_check(desktop, "shared-check")
            browser = LinuxBrowserComputer(
                "shared-check",
                lambda: False,
                preview_urls=lambda: (url,),
                shared_display=display.name,
                open_request=launchers / "open-browser",
                executable_path="/usr/bin/google-chrome-stable",
                profile_store=BrowserProfileStore(root / "profiles"),
                account_id="shared-check",
                system_trust=True,
            )
            assert browser.display == display.name
            status = browser.browser.status(browser.owner, browser.session_id)
            if status.product != "chrome":
                raise RuntimeError("The Cloud computer did not start Google Chrome.")

            # A human may open Chrome before the Owl makes any browser call.
            # The new window must already be guarded and visible to its tools.
            def pages():
                try:
                    return browser.browser.pages(browser.owner, browser.session_id)["pages"]
                except BrowserLimitError:
                    return []

            click_launcher(desktop, "shared-check", "chrome")
            wait_for_panel(lambda: len(pages()) == 2)
            desktop.human_input({"kind": "key", "keys": ["Control_L", "l"]})
            desktop.human_input({"kind": "text", "text": url})
            desktop.human_input({"kind": "key", "keys": ["Return"]})
            wait_for_panel(lambda: any(page["url"] == url for page in pages()))
            inventory = browser.browser.pages(browser.owner, browser.session_id)
            page = next(page for page in inventory["pages"] if page["url"] == url)
            browser.browser.switch_page(
                browser.owner,
                browser.session_id,
                page["page_id"],
                expected_generation=inventory["command_generation"],
            )
            if "Cloud browser is working" not in json.dumps(
                browser.browser.snapshot(browser.owner, browser.session_id, kind="text")
            ):
                raise RuntimeError("Managed Chrome did not render the fixture.")
            # Exercise the shared dock through the same human-input adapter as the viewer.
            for app, window_class in (("files", "Pcmanfm"), ("terminal", "XTerm")):
                click_launcher(desktop, "shared-check", app)
                wait_for_panel(
                    lambda window_class=window_class: (
                        desktop.call(
                            "shared-check",
                            "run",
                            {"command": "xdotool search --onlyvisible --class " + window_class},
                        )["exit_code"]
                        == 0
                    )
                )
            desktop.call(
                "shared-check",
                "run",
                {"command": "xdotool search --onlyvisible --class XTerm windowactivate --sync"},
            )
            desktop.human_input(
                {
                    "kind": "text",
                    "text": "printf 'shared Chrome terminal Αθήνα 東京' > /workspace/shared-input.txt",
                }
            )
            desktop.human_input({"kind": "key", "keys": ["Return"]})
            result = desktop.call(
                "shared-check", "run", {"command": "cat /workspace/shared-input.txt"}
            )
            if result.get("output") != "shared Chrome terminal Αθήνα 東京":
                raise RuntimeError("Shared desktop keyboard input did not reach Terminal.")
            # The app namespace shares pixels, not service files or the host network.
            isolated = desktop.call(
                "shared-check",
                "run",
                {
                    "command": "test ! -e /opt/alysis/src/alysis_code/bots/cloud_desktop_worker.py && "
                    "test ! -e /run/alysis-screen/screen.sock && "
                    "test ! -e /etc/alysis-bots/operations.json"
                },
            )
            if isolated["exit_code"] != 0:
                raise RuntimeError("Desktop app namespace exposes controller files.")
            from urllib.parse import urlsplit

            network_check = desktop.call(
                "shared-check",
                "run",
                {
                    "command": "/opt/alysis/.venv/bin/python -I -c 'import socket; s=socket.socket(); s.settimeout(1); "
                    f'assert s.connect_ex(("127.0.0.1", {urlsplit(url).port})) != 0' + "'"
                },
            )
            if network_check["exit_code"] != 0:
                raise RuntimeError("Desktop shell unexpectedly shares the browser network.")
            before = len(browser.browser.pages(browser.owner, browser.session_id)["pages"])
            click_launcher(desktop, "shared-check", "chrome")

            def new_window():
                try:
                    return (
                        len(browser.browser.pages(browser.owner, browser.session_id)["pages"])
                        > before
                    )
                except BrowserLimitError:
                    return False  # The dock's admitted open is still running.

            wait_for_panel(new_window)
            # Bring Terminal forward over Chrome: both remain on the same display.
            desktop.call(
                "shared-check",
                "run",
                {"command": "xdotool search --onlyvisible --class XTerm windowactivate --sync"},
            )
            shared_frame = visible_frame(desktop.capture, output / "shared.png")
            previous_session = browser.session_id
            owned = browser.browser._owned_session(browser.owner, previous_session)
            try:
                owned.transport.call("Browser.close", {}, timeout=5)
            except Exception:
                pass  # Chrome can close its transport before acknowledging shutdown.
            wait_for_panel(
                lambda: browser.browser.status(browser.owner, previous_session).state != "running"
            )
            if not desktop.alive():
                raise RuntimeError("Closing Chrome also stopped the desktop.")
            click_launcher(desktop, "shared-check", "chrome")
            wait_for_panel(lambda: browser.session_id != previous_session)
            browser.browser.navigate(browser.owner, browser.session_id, url)
            if "Cloud browser is working" not in json.dumps(
                browser.browser.snapshot(browser.owner, browser.session_id, kind="text")
            ):
                raise RuntimeError("Reopened managed Chrome cannot browse.")
            if "Fixture cookie retained" not in json.dumps(
                browser.browser.snapshot(browser.owner, browser.session_id, kind="text")
            ):
                raise RuntimeError("Closing and reopening Chrome lost its profile cookie.")
            return {
                "live_display": live_display,
                "browser_product": status.product,
                "human_first_navigation": "verified",
                "chrome_reopen": "verified",
                "terminal_input": "verified",
                "dock_chrome": "verified",
                "shared": shared_frame,
            }
        finally:
            if browser:
                browser.close()
            if desktop:
                desktop.close()
            display.close()


def main(output):
    if sys.prefix == sys.base_prefix or os.getuid() != 1000:
        raise RuntimeError("Use the worker managed environment as UID 1000.")
    output.mkdir(parents=True, exist_ok=True)
    report = {"package": package_matches_source()}
    with tempfile.TemporaryDirectory() as directory:
        desktop = OfflineLinuxDesktop("image-check", Path(directory))
        try:
            wait_for_panel(
                lambda: (
                    desktop.call(
                        "image-check",
                        "run",
                        {"command": "xdotool search --onlyvisible --class tint2"},
                    )["exit_code"]
                    == 0
                )
            )
            # Exercise real applications, not just non-uniform wallpaper.
            for app, window_class in (
                ("files", "Pcmanfm"),
                ("terminal", "XTerm"),
            ):
                # Exercise actual dock launchers, including .desktop Exec parsing.
                click_launcher(desktop, "image-check", app)
                wait_for_panel(
                    lambda window_class=window_class: (
                        desktop.call(
                            "image-check",
                            "run",
                            {"command": f"xdotool search --onlyvisible --class {window_class}"},
                        )["exit_code"]
                        == 0
                    )
                )
                if app == "terminal":
                    desktop.call(
                        "image-check",
                        "run",
                        {
                            "command": "xdotool search --onlyvisible --class XTerm windowactivate --sync"
                        },
                    )
                    desktop.human_input(
                        {
                            "kind": "text",
                            "text": "printf 'terminal-input-verified' > /workspace/input-check.txt",
                        }
                    )
                    desktop.human_input({"kind": "key", "keys": ["Return"]})
                    result = desktop.call(
                        "image-check", "run", {"command": "cat /workspace/input-check.txt"}
                    )
                    if result.get("output") != "terminal-input-verified":
                        raise RuntimeError("Terminal did not consume viewer text input.")
                    report["terminal_input"] = "verified"
                report[app] = visible_frame(desktop.capture, output / f"{app}.png")
            report["desktop"] = visible_frame(desktop.capture, output / "desktop.png")
        finally:
            desktop.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser = None
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        browser = LinuxBrowserComputer("image-check", lambda: False, preview_urls=lambda: (url,))
        browser.browser.navigate(browser.owner, browser.session_id, url)
        snapshot = browser.browser.snapshot(browser.owner, browser.session_id, kind="text")
        if "Cloud browser is working" not in json.dumps(snapshot, ensure_ascii=False):
            raise RuntimeError("The browser did not render the owned test page.")
        wait_for_panel(
            lambda: (
                subprocess.run(
                    ["/usr/bin/xdotool", "search", "--onlyvisible", "--class", "tint2"],
                    env={"DISPLAY": browser.display},
                    capture_output=True,
                    timeout=2,
                ).returncode
                == 0
            )
        )
        report["browser"] = visible_frame(browser.capture, output / "browser.png")
        report["unified_desktop"] = shared_desktop_check(output, url)
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output)
