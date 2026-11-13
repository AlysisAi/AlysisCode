"""Opt-in GLM / Linux Chromium / real renderer file-sharing qualification.
Saved Coding Plan only, 18-call total ceiling. Synthetic local website/data.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def linux():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.models import BotInput

    config = json.loads(sys.stdin.readline())
    uploads = []

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            data = """<!doctype html><meta charset="utf-8"><h1>Synthetic document intake</h1>
<form><label>Project name<input id="project" required></label>
<label>Language<select id="language"><option value="el">Ελληνικά</option><option value="ja">日本語</option></select></label>
<label>File<input type="file" id="document" required></label><button>Submit file</button></form><output id="receipt"></output>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{const r=await fetch('/receive?project='+encodeURIComponent(project.value)+'&language='+language.value,{method:'POST',body:document.getElementById('document').files[0]});receipt.textContent=await r.text()}catch(e){receipt.textContent='Upload failed; inspect before retrying'}finally{button.disabled=false}};</script>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            from urllib.parse import parse_qs, urlsplit

            fields = parse_qs(urlsplit(self.path).query)
            remaining = int(self.headers["Content-Length"])
            total = remaining
            digest = hashlib.sha256()
            while remaining:
                data = self.rfile.read(min(256 * 1024, remaining))
                remaining -= len(data)
                digest.update(data)
            row = {
                "project": fields.get("project"),
                "language": fields.get("language"),
                "bytes": total,
                "sha256": digest.hexdigest(),
            }
            uploads.append(row)
            data = json.dumps({"accepted": True, "receipt": len(uploads), **row}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=18,
        output_tokens=2048,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
        browser_enabled=True,
        desktop_enabled=True,
    )
    app = create_app(settings)
    runtime = app.state.runtime
    runtime.browser_computers.factory = lambda bot, cancel: LinuxBrowserComputer(
        bot, cancel, preview_urls=lambda: [origin]
    )
    bot = app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Computer view test", purpose="Work with files and forms, verifying real results."
        ),
    )
    app.state.store.save_bot(
        settings.owner, BotInput(name="No computer test", purpose="Renderer transfers only.")
    )
    block = bytes(range(256)) * 1024
    source = app.state.store.save_stream(
        settings.owner, bot["id"], "analysis.bin", (block for _ in range(24)), len(block) * 24
    )
    traces = []
    original = runtime.client_factory

    class Trace:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            response = self.client.chat(**kwargs)
            traces.append(
                {
                    "content": response.content,
                    "tools": [
                        {"name": t.name, "arguments": t.arguments} for t in response.tool_calls
                    ],
                }
            )
            return response

    runtime.client_factory = lambda task: Trace(original(task))

    @app.get("/study/receipt")
    def receipt():
        return {
            "origin": origin,
            "source": source,
            "uploads": uploads,
            "traces": traces,
            "usage": app.state.store.usage(settings.owner),
        }

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        server.shutdown()


def exercise(app, client, bot, other, private, name):
    print(
        json.dumps(
            {"ready": True, "container": name, "evidence": str(private), "call_allowance": 18}
        ),
        flush=True,
    )
    config = {
        "execute_live": True,
        "url": str(client.base_url),
        "token": client.headers["Authorization"].removeprefix("Bearer "),
        "bot": bot,
        "other": other,
        "evidence": str(private),
        **client.get("/study/receipt").json(),
    }
    subprocess.run(
        ["node", str(app / "scripts/qa/bots-transfers-service-study.mjs")],
        input=json.dumps(config).encode(),
        cwd=app,
        check=True,
        timeout=900,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--linux", action="store_true")
    p.add_argument("--execute-live", action="store_true")
    p.add_argument("--app", type=Path)
    p.add_argument("--existing-network")
    args = p.parse_args()
    if args.linux:
        linux()
    elif args.execute_live and args.app and args.existing_network:
        from functools import partial

        from bots_computer_view import host

        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        config = load_config()
        profile = get_profile(config, "zai-coding-plan")
        key = resolve_profile_api_key(config, "zai-coding-plan").key
        if profile is None or not key or profile.protocol != "openai_compat":
            raise RuntimeError("Coding Plan unavailable; no fallback.")
        host(
            args.app.resolve(),
            None,
            study_script=Path(__file__).resolve(),
            bootstrap={"key": key, "base_url": profile.base_url},
            lazy_browser=True,
            exercise=partial(exercise, args.app.resolve()),
            port=7091,
            image="bots-durable-computer-20261019:dev",
            name_prefix="bots-transfers-recovery-20261019-",
            existing_network=args.existing_network,
            tmpfs_size="1g",
            memory="2g",
        )
    else:
        p.error("Explicit live opt-in, app and existing network required")
