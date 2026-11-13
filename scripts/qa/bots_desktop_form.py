"""Bounded GLM visual-only form study in the actual offline Linux computer.

Randomized synthetic text, independent in-jail HTTP receipts. No external
site, account or DOM shortcut. Only saved Coding Plan; twelve calls maximum.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

SERVER = r"""
from http.server import BaseHTTPRequestHandler, HTTPServer
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
import json
from uuid import uuid4
STYLE = '<style>body{font:26px sans-serif;margin:60px;color:#17243a;background:#f6f7fb}section{background:white;padding:28px;border:2px solid #cbd2df;max-width:900px}input,button{font:26px sans-serif;padding:15px;margin:14px 16px 14px 0}input{width:760px}button{cursor:pointer}h1{font-size:36px}</style>'
class Handler(BaseHTTPRequestHandler):
    def page(self, body):
        data=('<!doctype html><meta charset=utf-8><title>Offline delivery form</title>'+STYLE+body).encode()
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        self.page('<h1>Offline delivery form</h1><p>Synthetic test. Scroll down to the Delivery note form.</p><div style="height:850px"></div><section><h2>Delivery note</h2><form action="/review" method="post"><label>Note<br><input name="note" autocomplete="off" required></label><br><button>Review note</button></form></section>')
        Path('/workspace/ready').touch()
    def do_POST(self):
        data=parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode(),keep_blank_values=True)
        note=data.get('note',[''])[0]
        with Path('/workspace/events.jsonl').open('a') as file:
            file.write(json.dumps({'path':self.path,'note':note},ensure_ascii=False)+'\n')
        if self.path=='/review':
            self.page('<h1>Review delivery note</h1><section><p>'+escape(note)+'</p><form action="/confirm" method="post"><input type="hidden" name="note" value="'+escape(note,quote=True)+'"><button>Confirm once</button></form><a href="/">Back to edit</a></section>')
        elif self.path=='/confirm':
            reference='NOTE-'+uuid4().hex[:6].upper()
            Path('/workspace/receipt.json').write_text(json.dumps({'note':note,'reference':reference},ensure_ascii=False))
            self.page('<h1>Delivery note saved</h1><section><p>'+escape(note)+'</p><p>Reference: <strong>'+reference+'</strong></p></section>')
        else: self.page('<h1>Unknown action</h1>')
    def log_message(self,*_): pass
HTTPServer(('127.0.0.1',8766),Handler).serve_forever()
"""


def linux():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
    from alysis_code.bots.models import BotInput

    config = json.loads(sys.stdin.readline())
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=12,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    note = "Αθήνα — 東京 — Café " + uuid4().hex[:4].upper()
    snapshots, operations, traces, instances = [], [], [], []

    class RecordedDesktop(OfflineLinuxDesktop):
        def call(self, owner, operation, arguments, **kwargs):
            record = {"operation": operation, "arguments": arguments}
            operations.append(record)
            try:
                result = super().call(owner, operation, arguments, **kwargs)
                observation = result if operation == "observe" else result.get("observation")
                if observation:
                    snapshots.append(observation["png"])
                record["result"] = {
                    k: v for k, v in result.items() if k not in {"png", "observation"}
                }
                return result
            except Exception as exc:
                record["error"] = str(exc)
                raise

        def close(self):
            if self.workspace.exists():
                self.saved = {}
                for name in ("events.jsonl", "receipt.json"):
                    if (self.workspace / name).exists():
                        self.saved[name] = (self.workspace / name).read_text()
            super().close()

    def factory(owner, workspace, *, cancel):
        desktop = RecordedDesktop(owner, workspace, cancel=cancel)
        instances.append(desktop)
        try:
            (workspace / "server.py").write_text(SERVER)
            # Trusted fixture setup only, recorded separately from model actions.
            OfflineLinuxDesktop.call(
                desktop, owner, "launch", {"command": "python /workspace/server.py"}, cancel=cancel
            )
            OfflineLinuxDesktop.call(
                desktop,
                owner,
                "launch",
                {
                    "command": "chromium --disable-dev-shm-usage --no-first-run --no-default-browser-check --user-data-dir=/tmp/chrome --kiosk http://127.0.0.1:8766/"
                },
                cancel=cancel,
            )
            deadline = time.monotonic() + 10
            while not (workspace / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not (workspace / "ready").exists():
                raise RuntimeError("Synthetic browser did not load")
            time.sleep(0.5)
            return desktop
        except BaseException:
            desktop.close()
            raise

    computers = DesktopComputers(settings.owner, ComputerViews(), factory=factory)
    app = create_app(settings, desktop_computers=computers)
    original = app.state.runtime.client_factory

    class TraceClient:
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
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                }
            )
            return response

    app.state.runtime.client_factory = lambda _: TraceClient(original(_))
    app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Computer view test",
            purpose="Operate a supplied synthetic desktop form using actual screenshots and native input.",
        ),
    )
    app.state.store.save_bot(
        settings.owner, BotInput(name="No computer test", purpose="Remain idle.")
    )

    @app.get("/study/instructions")
    def instructions():
        return {"note": note}

    @app.get("/study/receipt")
    def receipt():
        saved = []
        for desktop in instances:
            files = dict(getattr(desktop, "saved", {}))
            for filename in ("events.jsonl", "receipt.json"):
                if (desktop.workspace / filename).exists():
                    files[filename] = (desktop.workspace / filename).read_text()
            saved.append(
                {
                    "files": files,
                    "alive": desktop.alive(),
                    "workspace_exists": desktop.workspace.exists(),
                }
            )
        return {
            "instances": saved,
            "operations": operations,
            "traces": traces,
            "usage": app.state.store.usage(settings.owner),
        }

    @app.get("/study/frames")
    def frames():
        return snapshots

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        computers.close()


def exercise(client, bot, other, private, name):
    def get(path):
        result = client.get(path)
        result.raise_for_status()
        return result.json()

    def wait(task_id):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            task = get(f"/v1/tasks/{task_id}")
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return task
            time.sleep(0.2)
        raise RuntimeError("Visual form study deadline reached")

    note = get("/study/instructions")["note"]
    chat = client.post(f"/v1/bots/{bot['id']}/chats", json={"title": "Visual form study"}).json()
    evidence = {"passed": False, "expected_note": note}
    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    try:
        result = client.post(
            f"/v1/chats/{chat['id']}/tasks",
            json={
                "request_id": uuid4().hex,
                "message": f"Use the offline computer to scroll down to Delivery note, enter exactly this text: {note}\nReview the note and confirm it once, then report the visible saved reference. Use actual screenshots and native scroll/click/paste/key tools only. Do not use commands, read source files, inspect DOM or use scripting. First propose this exact task for offline computer approval and wait.",
            },
        )
        result.raise_for_status()
        evidence["request"] = wait(result.json()["id"])
        assert evidence["request"]["status"] == "completed", evidence["request"]["error"]
        proposals = [
            p
            for p in get(f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == evidence["request"]["id"]
        ]
        assert len(proposals) == 1 and proposals[0]["offline_computer"], (
            "Actual computer proposal missing"
        )
        assert not get("/study/receipt")["instances"]
        proposal = proposals[0]
        result = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            json={
                "action": "accept",
                "expected_revision": proposal["revision"],
                "offline_computer": True,
            },
        )
        result.raise_for_status()
        evidence["action"] = wait(result.json()["task_id"])
        assert evidence["action"]["status"] == "completed", evidence["action"]["error"]
        actual = get("/study/receipt")
        assert len(actual["instances"]) == 1
        instance = actual["instances"][0]
        events = [json.loads(s) for s in instance["files"]["events.jsonl"].splitlines()]
        assert events == [{"path": "/review", "note": note}, {"path": "/confirm", "note": note}], (
            events
        )
        saved = json.loads(instance["files"]["receipt.json"])
        assert saved["note"] == note and saved["reference"] in evidence["action"]["output"]
        tools = [c["name"] for t in actual["traces"] for c in t["tools"]]
        assert {"computer_observe", "computer_scroll", "computer_click", "computer_type"} <= set(
            tools
        ), tools
        assert not {
            "computer_run",
            "computer_launch",
            "computer_import_file",
            "computer_save_file",
            "read_file",
        }.intersection(tools), tools
        assert not instance["alive"] and not instance["workspace_exists"]
        assert get(f"/v1/bots/{other['id']}/computer")["state"] == "not_running"
        evidence["passed"] = True
    finally:
        evidence["receipt"] = get("/study/receipt")
        for i, png in enumerate(get("/study/frames")):
            (private / f"frame-{i:02d}.png").write_bytes(base64.b64decode(png))
        (private / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "passed": evidence["passed"],
                    "usage": evidence["receipt"]["usage"],
                    "evidence": str(private),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--port", type=int, default=7091)
    parser.add_argument("--image", default="bots-linux-night-20261019:actions")
    parser.add_argument("--existing-network")
    args = parser.parse_args()
    if args.linux:
        linux()
    elif args.execute_live and args.app:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
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
            exercise=exercise,
            port=args.port,
            image=args.image,
            name_prefix="bots-linux-night-20261019-",
            existing_network=args.existing_network,
        )
    else:
        parser.error("Use --execute-live --app for the explicit bounded live study.")
