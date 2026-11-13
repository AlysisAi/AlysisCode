"""Opt-in bounded GLM + actual React/HTTP/browser/offline document study.

Credentials stay in memory/stdin. Exactly one 18-call allowance, no fallback.
The provider-free companion checks container replacement separately.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


def linux():
    import uvicorn
    from openpyxl import Workbook

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.models import BotInput

    config = json.loads(sys.stdin.readline())
    book = Workbook()
    book.active.append(["city", "amount"])
    values = [("東京", int(uuid4().hex[:4], 16)), ("Αθήνα", int(uuid4().hex[:4], 16))]
    for row in values:
        book.active.append(row)
    output = io.BytesIO()
    book.save(output)
    workbook = output.getvalue()

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            file = self.path == "/ledger.xlsx"
            data = (
                workbook
                if file
                else b'<h1>Synthetic sales ledger</h1><a href="/ledger.xlsx" download>Download sales workbook</a>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream" if file else "text/html")
            self.send_header("Content-Length", str(len(data)))
            if file:
                self.send_header("Content-Disposition", 'attachment; filename="ledger.xlsx"')
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
    runtime.browser_computers.factory = lambda bot_id, cancel: LinuxBrowserComputer(
        bot_id, cancel, preview_urls=lambda: [origin]
    )
    for name in ("Computer view test", "No computer test"):
        app.state.store.save_bot(
            settings.owner, BotInput(name=name, purpose="Process synthetic documents accurately.")
        )
    original = runtime.client_factory
    traces = []

    class TraceClient:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            reply = self.client.chat(**kwargs)
            traces.append(
                {
                    "content": reply.content,
                    "tools": [{"name": t.name, "arguments": t.arguments} for t in reply.tool_calls],
                }
            )
            return reply

    runtime.client_factory = lambda task_id: TraceClient(original(task_id))

    @app.get("/study/receipt")
    def receipt():
        return {
            "origin": origin,
            "expected_total": sum(v for _, v in values),
            "traces": traces,
            "usage": app.state.store.usage(settings.owner),
            "desktop_count": len(runtime.desktop_computers._computers),
            "browser_count": len(runtime.browser_computers._computers),
        }

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        server.shutdown()


def exercise(app, client, bot, other, private, name):
    data = client.get("/study/receipt").json()
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
        **data,
    }
    subprocess.run(
        ["node", str(app / "scripts/qa/bots-durable-service-study.mjs")],
        input=json.dumps(config).encode(),
        cwd=app,
        check=True,
        timeout=600,
    )
    # Independently parse the actual UI downloads; model statements are not the
    # verification oracle. The image's pinned libraries are available on Linux.
    checked = subprocess.check_output(
        [
            "docker",
            "exec",
            name,
            "python",
            "-c",
            "import io,json;from openpyxl import load_workbook;import pypdfium2 as pdf;"
            "from alysis_code.bots.store import Store;from pathlib import Path;"
            "s=Store(Path('/tmp/bots/bots.db'));"
            "b=next(b for b in s.list_bots('local-development') if b['name']=='Computer view test');"
            "f={f['name']:s.file_data('local-development',b['id'],f['id']) for f in s.list_files('local-development',b['id'])};"
            "w=load_workbook(io.BytesIO(f['summary.xlsx']),data_only=True);"
            "p=pdf.PdfDocument(f['report.pdf']);"
            "print(json.dumps({'total':w.active['B2'].value,'pdf_text':p[0].get_textpage().get_text_range()}))",
        ]
    )
    result = json.loads(checked)
    assert result["total"] == data["expected_total"], result
    assert str(data["expected_total"]) in result["pdf_text"], result
    (private / "document-check.json").write_text(json.dumps(result))
    print(json.dumps({"documents_verified": True, "total": result["total"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--existing-network")
    args = parser.parse_args()
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
            raise RuntimeError("Saved Coding Plan unavailable; no fallback.")
        host(
            args.app.resolve(),
            None,
            study_script=Path(__file__).resolve(),
            bootstrap={"key": key, "base_url": profile.base_url},
            lazy_browser=True,
            exercise=partial(exercise, args.app.resolve()),
            port=7091,
            image="bots-durable-computer-20261019:dev",
            name_prefix="bots-durable-computer-20261019-",
            existing_network=args.existing_network,
        )
    else:
        parser.error("Use --execute-live --app PATH --existing-network NAME")
