"""Real Chromium controls and opt-in GLM workflow qualification.

Synthetic local website, no external account. Covers a covered button,
contenteditable text, checkbox state, a review dialog and delayed save.
The server receipt is independent of model prose. No model responses are faked.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

PAGE = """<!doctype html><meta charset="utf-8"><title>Delivery review</title>
<style>body{font:24px sans-serif;padding:30px}button{font:inherit;padding:14px}
#note{border:2px solid #aaa;padding:14px;min-height:45px;margin:20px 0}
#covered,#cover{position:absolute;left:30px;top:600px;width:500px;height:60px}
#cover{z-index:2}dialog{max-width:700px}input{width:25px;height:25px}</style>
<h1>Delivery review</h1><p>Synthetic local workflow.</p>
<label>Delivery note</label><div id="note" role="textbox" contenteditable="true">Replace this draft.</div>
<label><input id="notify" type="checkbox">Include local confirmation</label><br><br>
<button id="review"><span>Review delivery</span></button>
<p id="status" role="status">Nothing submitted.</p>
<button id="covered">Covered diagnostic control</button>
<button id="cover">Covering diagnostic control</button>
<dialog id="reviewDialog"><h2>Review delivery</h2><p id="preview"></p>
<button id="edit">Back to edit</button><button id="confirm"><span>Confirm delivery</span></button></dialog>
<script>
const note=document.querySelector('#note'),notify=document.querySelector('#notify');
const dialog=document.querySelector('dialog'),status=document.querySelector('#status');
document.querySelector('#review').onclick=()=>{
 document.querySelector('#preview').textContent=note.innerText+' / Local confirmation: '+notify.checked;
 dialog.showModal();
};
document.querySelector('#edit').onclick=()=>dialog.close();
document.querySelector('#confirm').onclick=async e=>{
 e.currentTarget.disabled=true;status.textContent='Saving; wait for confirmation.';
 const r=await fetch('/save',{method:'POST',body:JSON.stringify({note:note.innerText,notify:notify.checked})});
 const saved=await r.json();dialog.close();status.textContent='Saved delivery '+saved.reference+': '+saved.note;
};
for(const id of ['covered','cover'])document.querySelector('#'+id).onclick=()=>fetch('/diagnostic',{method:'POST',body:id});
</script>"""


class Site:
    def __init__(self):
        self.saved, self.diagnostics = [], []
        self.reference = "DEL-" + uuid4().hex[:10]
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/save":
                    data = json.loads(body)
                    site.saved.append(data)
                    time.sleep(1.5)  # Real asynchronous receipt; never replay a pending save.
                    response = json.dumps({**data, "reference": site.reference}).encode()
                else:
                    site.diagnostics.append(body.decode())
                    response = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 8767), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:8767"


def computers(site, owner, *, previews=True):
    from alysis_code.bots.browser import BrowserComputers
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.computer import ComputerViews

    return BrowserComputers(
        owner,
        ComputerViews(),
        lambda bot, cancel: LinuxBrowserComputer(
            bot, cancel, preview_urls=lambda: (site.url + "/",)
        ),
        interactive_previews=previews,
    )


def linux_checks():
    from alysis_code.cancellation import EventCancellationToken

    site = Site()
    adapter = computers(site, "checks")
    task = {"id": "checks", "bot_id": "checks"}
    token = EventCancellationToken(threading.Event())

    def call(name, args=None):
        return adapter.call(task, name, args or {}, token)

    def target(obs, name):
        return next(
            n["node"]
            for n in obs["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == name
        )

    try:
        obs = call("browser_navigate", {"url": site.url})
        try:
            call("browser_click", {"source_id": obs["source_id"], "node": target(obs, "covered")})
        except ValueError:
            pass
        else:
            raise AssertionError("Covered target dispatched input: " + str(site.diagnostics))
        assert site.diagnostics == []
        # A rejected action consumes its source; a fresh observation is required.
        obs = call("browser_observe")
        marker = "Δοκιμή — 日本語 — " + uuid4().hex[:8]
        obs = call(
            "browser_type",
            {"source_id": obs["source_id"], "node": target(obs, "note"), "text": marker},
        )
        assert marker in json.dumps(obs, ensure_ascii=False)
        obs = call("browser_click", {"source_id": obs["source_id"], "node": target(obs, "notify")})
        assert next(
            n for n in obs["dom"]["documents"][0]["nodes"] if n["node"] == target(obs, "notify")
        )["checked"]
        obs = call("browser_click", {"source_id": obs["source_id"], "node": target(obs, "review")})
        # The span inside this button must be an allowed hit, not mistaken for an overlay.
        obs = call("browser_click", {"source_id": obs["source_id"], "node": target(obs, "confirm")})
        deadline = time.monotonic() + 6
        while site.reference not in json.dumps(obs) and time.monotonic() < deadline:
            obs = call("browser_observe")
        assert site.reference in json.dumps(obs)
        assert site.saved == [{"note": marker, "notify": True}]
        print(
            json.dumps(
                {
                    "passed": True,
                    "model_calls": 0,
                    "covered_input_blocked": True,
                    "saved": site.saved,
                }
            ),
            flush=True,
        )
    finally:
        adapter.close()
        site.server.shutdown()


def linux():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.models import BotInput

    config = json.loads(sys.stdin.readline())
    site = Site()
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=config.get("max_calls", 24),
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    adapter = computers(site, settings.owner, previews=False)
    app = create_app(settings, browser_computers=adapter)
    store = app.state.store
    store.save_bot(
        settings.owner,
        BotInput(name="Computer view test", purpose="Help with a local synthetic delivery review."),
    )
    store.save_bot(settings.owner, BotInput(name="No computer test", purpose="Remain idle."))
    traces = []
    original = app.state.runtime.client_factory

    class Trace:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            response = self.client.chat(**kwargs)
            traces.append(
                {
                    "messages": kwargs["messages"].copy(),
                    "content": response.content,
                    "tools": [
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                }
            )
            return response

    app.state.runtime.client_factory = lambda task: Trace(original(task))

    @app.get("/study/evidence")
    def evidence():
        return dict(
            saved=site.saved,
            diagnostics=site.diagnostics,
            reference=site.reference,
            traces=traces,
            usage=store.usage(settings.owner),
        )

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        adapter.close()
        site.server.shutdown()


def exercise(client, bot, other, private, container):
    def api(method, path, data=None):
        r = client.request(method, path, **({"json": data} if data is not None else {}))
        r.raise_for_status()
        return r.json()

    def wait(task):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = api("GET", f"/v1/tasks/{task}")
            if result["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return result
            time.sleep(0.3)
        raise TimeoutError("Bounded workflow study timed out")

    marker = "Αθήνα — 東京 — Café " + uuid4().hex[:8]
    result = {"passed": False, "marker": marker}
    try:
        chat = api("POST", f"/v1/bots/{bot['id']}/chats", {})
        request = api(
            "POST",
            f"/v1/chats/{chat['id']}/tasks",
            {
                "request_id": uuid4().hex,
                "message": f"On http://127.0.0.1:8767/ replace the delivery draft with exactly: {marker}\nEnable local confirmation, review and confirm the delivery once. Wait for the saved reference and report it. Leave the diagnostic controls alone.",
            },
        )
        result["request"] = wait(request["id"])
        proposal = next(
            p
            for p in api("GET", f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == request["id"]
        )
        assert proposal["browser_origin"] == "http://127.0.0.1:8767"
        assert not api("GET", "/study/evidence")["saved"]
        accepted = api(
            "PATCH",
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            {
                "action": "accept",
                "expected_revision": proposal["revision"],
                "browser_origin": proposal["browser_origin"],
            },
        )
        result["action"] = wait(accepted["task_id"])
        evidence = api("GET", "/study/evidence")
        assert result["action"]["status"] == "completed", result["action"].get("error")
        assert evidence["saved"] == [{"note": marker, "notify": True}], evidence["saved"]
        assert evidence["diagnostics"] == []
        assert evidence["reference"] in result["action"]["output"]
        assert api("GET", f"/v1/bots/{other['id']}/computer")["state"] == "not_running"
        result["passed"] = True
    finally:
        result["evidence"] = api("GET", "/study/evidence")
        (private / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                dict(
                    passed=result["passed"],
                    usage=result["evidence"]["usage"],
                    evidence=str(private),
                )
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--linux-checks", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--existing-network")
    parser.add_argument("--port", type=int, default=7091)
    parser.add_argument("--max-calls", type=int, default=24)
    args = parser.parse_args()
    if args.linux_checks:
        linux_checks()
    elif args.linux:
        linux()
    elif args.execute_live and args.app:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from bots_coding_context_study import validate_plan
        from bots_computer_view import host

        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        cfg = load_config()
        profile = get_profile(cfg, "zai-coding-plan")
        validate_plan(profile)
        key = resolve_profile_api_key(cfg, "zai-coding-plan").key
        if not key:
            raise RuntimeError("Saved Coding Plan unavailable; no fallback")
        host(
            args.app.resolve(),
            None,
            study_script=Path(__file__).resolve(),
            bootstrap=dict(key=key, base_url=profile.base_url, max_calls=args.max_calls),
            lazy_browser=True,
            exercise=exercise,
            port=args.port,
            image="bots-durable-computer-20261019:dev",
            name_prefix="bots-browser-workflows-",
            existing_network=args.existing_network,
            memory="2g",
        )
    else:
        parser.error("Use --execute-live --app, or --linux-checks inside the isolated lab")
