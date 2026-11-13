"""Provider-free real Linux screen/viewer study, using a disposable computer.

Run with --app pointing at the built desktop checkout. Only the study's bot is
bound to this X server. No existing lab display, user bot or provider key is used.
Ctrl+C closes the study and its container. Native UI checks are performed in the
opened Electron app; the animated webpage is a fixture, the pixels/transport real.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import plistlib
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


def linux():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews, LinuxScreenCapture
    from alysis_code.bots.models import BotInput
    from alysis_code.ide.cdp_websocket_transport import WebSocketCdpTransportFactory
    from alysis_code.ide.managed_browser import ManagedBrowserConfig, ManagedBrowserService

    token = json.loads(sys.stdin.readline())["token"]
    views = ComputerViews()
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        token,
        "unused-fixture",
        "https://example.invalid",
        "No model calls",
        max_calls=0,
    )
    app = create_app(settings, computer_views=views)
    bot = app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Computer view test", purpose="A real isolated Linux screen; no model is running."
        ),
    )
    app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="No computer test", purpose="Verify that another bot cannot see this computer."
        ),
    )
    display = ":91"
    xserver = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x800x24", "-nolisten", "tcp", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wm = None
    browser = None
    generation = None

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            page = b"""<!doctype html><meta charset="utf-8"><title>Computer viewer check</title>
<style>body{font:24px system-ui;background:#f5f6f8;color:#20232b;padding:70px}h1{font-size:38px}section{background:white;padding:32px;border-radius:16px}small{color:#687080}input{font:inherit;padding:10px;width:420px}</style>
<h1>Alysis computer viewer</h1><section><p>This is a real Linux browser.</p><p>Live clock: <strong id="clock"></strong></p>
<p>Browser clicks: <strong id="clicks">0</strong></p><input placeholder="View only: typing stays in Alysis"><p><small>Synthetic local study. No model or account connected.</small></p></section>
<script>setInterval(()=>clock.textContent=new Date().toISOString(),250);let count=0;document.addEventListener('click',()=>clicks.textContent=++count)</script>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_args):
            pass

    page_server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=page_server.serve_forever, daemon=True).start()
    try:
        for _ in range(50):
            result = subprocess.run(
                ["xdpyinfo", "-display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Private study display failed")
        wm = subprocess.Popen(
            ["openbox", "--sm-disable"],
            env={"DISPLAY": display, "HOME": "/tmp", "PATH": "/usr/bin:/bin"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser = ManagedBrowserService(
            config=ManagedBrowserConfig(data_root=Path("/tmp/browser"), linux_display=display),
            transport_factory=WebSocketCdpTransportFactory(),
        )
        url = f"http://127.0.0.1:{page_server.server_port}/"
        session = browser.start(bot["id"], allowed_preview_urls_provider=lambda: [url])
        browser.navigate(bot["id"], session.session_id, url)
        generation = views.bind(
            settings.owner,
            bot["id"],
            capture=LinuxScreenCapture(display),
            alive=lambda: (
                xserver.poll() is None
                and browser.status(bot["id"], session.session_id).state == "running"
            ),
        )
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        if generation:
            views.revoke(settings.owner, bot["id"], generation)
        if browser:
            browser.close_all()
        page_server.shutdown()
        for process in (wm, xserver):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def host(
    app: Path,
    subnet: str | None,
    *,
    study_script: Path | None = None,
    bootstrap: dict | None = None,
    lazy_browser: bool = False,
    exercise=None,
    port: int = 7082,
    image: str = "alysis-bots-dev:local",
    name_prefix: str = "alysis-computer-view-",
    existing_network: str | None = None,
    tmpfs_size: str = "256m",
    memory: str = "1g",
):
    import httpx
    from PIL import Image

    ROOT = Path(__file__).resolve().parents[2]

    docker = ["docker", "--context", "desktop-linux"]
    name = name_prefix + uuid4().hex[:8]
    private = Path(tempfile.mkdtemp(prefix=name))
    # Pin this study's working-tree source so later edits cannot change a lazy
    # import mid-run. Keep only hashes/commit metadata in its result evidence.
    import hashlib

    source = private / "source"
    source.mkdir()
    manifest = {}
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "src"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    for relative in sorted(set(filter(None, paths))):
        original = ROOT / relative
        if not original.is_file():
            continue
        target = source / Path(relative).relative_to("src")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        manifest[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    (private / "source-manifest.json").write_text(
        json.dumps(
            {
                "commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "files": manifest,
            },
            indent=2,
        )
    )
    pinned_script = private / "study.py"
    shutil.copyfile(study_script or Path(__file__).resolve(), pinned_script)
    token = secrets.token_urlsafe(36)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    process = desktop = None
    if existing_network:
        info = json.loads(
            subprocess.check_output([*docker, "network", "inspect", existing_network])
        )[0]
        if info["Containers"] or info["Internal"] or info["Driver"] != "bridge":
            raise RuntimeError(
                "The explicitly selected study network must be idle and external bridge."
            )
    else:
        subprocess.run(
            [
                *docker,
                "network",
                "create",
                *(["--subnet", subnet] if subnet else []),
                name,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    try:
        with (private / "linux.log").open("w") as log:
            process = subprocess.Popen(
                [
                    *docker,
                    "run",
                    "--rm",
                    "-i",
                    "--init",
                    "--name",
                    name,
                    "--network",
                    existing_network or name,
                    "--read-only",
                    "--user",
                    "1000:1000",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--security-opt",
                    "seccomp=" + str(ROOT / "scripts/cloud-dev/seccomp.json"),
                    "--security-opt",
                    "systempaths=unconfined",
                    "--cpus",
                    "2",
                    "--memory",
                    memory,
                    "--pids-limit",
                    "512",
                    "--tmpfs",
                    f"/tmp:rw,nosuid,nodev,size={tmpfs_size},mode=1777",
                    "-p",
                    f"127.0.0.1:{port}:7080",
                    "-v",
                    f"{source}:/study-src:ro",
                    "-v",
                    f"{pinned_script}:/study.py:ro",
                    "-e",
                    "PYTHONPATH=/study-src",
                    image,
                    "python",
                    "/study.py",
                    "--linux",
                ],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
            )
            process.stdin.write((json.dumps({**(bootstrap or {}), "token": token}) + "\n").encode())
            process.stdin.close()
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + token},
            timeout=6,
        ) as client:
            for _ in range(80):
                if process.poll() is not None:
                    raise RuntimeError("Linux study exited; inspect " + str(private / "linux.log"))
                try:
                    response = client.get("/v1/bots")
                    if response.is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("Linux study did not become ready")
            bots = response.json()
            bot = next(item for item in bots if item["name"] == "Computer view test")
            other = next(item for item in bots if item["name"] == "No computer test")
            frame = client.get(f"/v1/bots/{bot['id']}/computer").json()
            assert frame["state"] == ("not_running" if lazy_browser else "live"), frame
            import base64

            if not lazy_browser:
                with Image.open(
                    io.BytesIO(base64.b64decode(frame["image"].split(",", 1)[1]))
                ) as im:
                    assert im.size == (1280, 800)
            assert client.get(f"/v1/bots/{other['id']}/computer").json() == {"state": "not_running"}
            assert client.get("/v1/status").json()["usage"]["calls"] == 0
            assert (
                httpx.get(f"http://127.0.0.1:{port}/v1/bots/{bot['id']}/computer").status_code
                == 401
            )
            if exercise is not None:
                # API studies reuse the same isolated Linux service and cleanup
                # without requiring an unlocked native desktop.
                exercise(client, bot, other, private, name)
                return
            profile = private / "desktop"
            profile.mkdir(mode=0o700)
            (profile / "runtime-supervisor.json").write_text(
                json.dumps({"customBinaryPath": str(ROOT / ".dev/bots-engine")})
            )
            env = {
                **os.environ,
                "ALYSIS_E2E": "1",
                "ALYSIS_E2E_USER_DATA": str(profile),
                "ALYSIS_ALLOW_MULTIPLE_INSTANCES": "1",
                "ALYSIS_BOTS_URL": f"http://127.0.0.1:{port}",
                "ALYSIS_BOTS_TOKEN": token,
            }
            # Give native accessibility a unique app identity, so the check
            # cannot accidentally operate the user's existing Electron preview.
            native_app = private / "Alysis Computer View Test.app"
            shutil.copytree(
                app / "node_modules/electron/dist/Electron.app", native_app, symlinks=True
            )
            info_path = native_app / "Contents/Info.plist"
            info = plistlib.loads(info_path.read_bytes())
            info["CFBundleIdentifier"] = "ai.alysis.computer-view-test"
            info["CFBundleName"] = "Alysis Computer View Test"
            info_path.write_bytes(plistlib.dumps(info))
            desktop = subprocess.Popen(
                [str(native_app / "Contents/MacOS/Electron"), str(app)],
                cwd=app,
                env=env,
                stdout=(private / "desktop.log").open("w"),
                stderr=subprocess.STDOUT,
            )
            print(
                json.dumps(
                    {
                        "ready": True,
                        "container": name,
                        "evidence": str(private),
                        "checks": [
                            "lazy computer allocation" if lazy_browser else "actual Linux pixels",
                            "correct bot binding",
                            "unauthorized viewer rejected",
                            "no model calls",
                        ],
                    }
                ),
                flush=True,
            )
            last_receipt = None
            while not stop.wait(0.5) and process.poll() is None and desktop.poll() is None:
                if lazy_browser:
                    receipt = client.get("/study/receipt").json()
                    if receipt != last_receipt:
                        last_receipt = receipt
                        (private / "receipt.json").write_text(
                            json.dumps(receipt, ensure_ascii=False, indent=2)
                        )
                        print(json.dumps({"study": receipt}, ensure_ascii=False), flush=True)
                        frame = client.get(f"/v1/bots/{bot['id']}/computer").json()
                        if frame["state"] == "live":
                            (private / "computer.jpg").write_bytes(
                                base64.b64decode(frame["image"].split(",", 1)[1])
                            )
    finally:
        if desktop and desktop.poll() is None:
            desktop.terminate()
            desktop.wait(timeout=10)
        if process and process.poll() is None:
            subprocess.run(
                [*docker, "stop", "--time", "10", name], stdout=subprocess.DEVNULL, check=False
            )
            process.wait(timeout=15)
        if not existing_network:
            subprocess.run([*docker, "network", "rm", name], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument(
        "--subnet", help="Unused private Docker subnet if the default pool is exhausted"
    )
    args = parser.parse_args()
    if args.linux:
        linux()
    elif args.app:
        host(args.app.resolve(), args.subnet)
    else:
        parser.error("Pass --app for the native Electron check")
