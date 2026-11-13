"""Opt-in real Bots runtime → private Linux browser → native Computer view.

Synthetic local form; only saved Z.ai Coding Plan, bounded to eight total model
calls (twelve for the approval flow). Credentials go over stdin, never files/arguments/logs. The browser starts
through actual model tool calls, not fixture responses. Ctrl+C cleans the study.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


def linux():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.browser import BrowserComputers
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.models import BotInput

    config = json.loads(sys.stdin.readline())
    public_read = bool(config.get("public_read"))
    background_read = bool(config.get("background_read"))
    approval_flow = bool(
        config.get("approval_flow") or config.get("controls_page") or config.get("visual")
    )
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=3 if public_read else 12 if approval_flow else 8,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
        browser_enabled=public_read,
    )
    saved = []
    marker = "Aster Ω — 日本語 — " + uuid4().hex[:8]
    slow_started, slow_release = threading.Event(), threading.Event()

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            if config.get("visual") and self.path == "/picture":
                picture = base64.b64decode(config["visual"]["image"])
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(picture)))
                self.end_headers()
                self.wfile.write(picture)
                return
            if background_read:
                if self.path == "/slow":
                    slow_started.set()
                    slow_release.wait(18)
                page = (
                    '<!doctype html><meta charset="utf-8"><title>Background continuity study</title>'
                    f"<h1>Background continuity study</h1><p>Observed marker: {marker}</p>"
                    f"<p>Page path: {self.path}</p>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            page = b"""<!doctype html><meta charset="utf-8"><title>Bot computer study</title>
<style>body{font:24px system-ui;background:#f5f6f8;padding:50px}input,button{font:inherit;padding:12px}section{background:white;padding:30px;border-radius:15px}</style>
<h1>Your bot's Linux computer</h1><section><p>Synthetic local note; no external account.</p>
<form><label>Note <input id="note" required></label><button>Save note</button></form>
<p role="status">No note saved.</p></section>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();const note=document.querySelector('input').value;const r=await fetch('/save',{method:'POST',body:JSON.stringify({note})});if(r.ok)document.querySelector('[role=status]').textContent='Saved note: '+note}</script>"""
            if config.get("controls_page"):
                page = config["controls_page"].encode()
            if config.get("visual"):
                page = config["visual"]["page"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/save" or not 0 < size <= 20000:
                self.send_error(400)
                return
            data = json.loads(self.rfile.read(size))
            if config.get("visual"):
                left, top, right, bottom = config["visual"]["target"]
                hit = left <= data["x"] <= right and top <= data["y"] <= bottom
                saved.append(
                    {**data, "hit": hit, "label": config["visual"]["label"] if hit else None}
                )
                reply = (
                    "Selected: " + config["visual"]["label"]
                    if hit
                    else "That was not the requested tile."
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
                return
            saved.append(data)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    page = ThreadingHTTPServer(("127.0.0.1", 8767), Page)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    views = ComputerViews()
    computers = BrowserComputers(
        settings.owner,
        views,
        lambda bot_id, cancel: LinuxBrowserComputer(
            bot_id, cancel, preview_urls=lambda: ("http://127.0.0.1:8767/",)
        ),
        idle_seconds=120,
        interactive_previews=not (background_read or approval_flow),
    )
    if public_read:
        app = create_app(settings)
        computers = app.state.runtime.browser_computers
        views = computers.views
    else:
        app = create_app(settings, browser_computers=computers)
    traces = []
    if approval_flow:
        original_factory = app.state.runtime.client_factory

        class TraceClient:
            def __init__(self, client):
                self.client = client

            def __getattr__(self, name):
                return getattr(self.client, name)

            def chat(self, **kwargs):
                # Synthetic study only: never serialize the client/config or
                # credentials. These are the actual page observations and choices.
                response = self.client.chat(**kwargs)
                traces.append(
                    {
                        "messages": kwargs["messages"].copy(),
                        "content": response.content,
                        "tool_calls": [
                            {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                        ],
                    }
                )
                return response

        app.state.runtime.client_factory = lambda task_id: TraceClient(original_factory(task_id))

        @app.get("/study/traces")
        def study_traces():
            return traces

    bot = app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Computer view test",
            purpose="Read public pages using your Linux browser."
            if public_read
            else "Help with the synthetic local browser study. The owned preview is http://127.0.0.1:8767/.",
        ),
    )
    app.state.store.save_bot(
        settings.owner,
        BotInput(name="No computer test", purpose="Computer ownership isolation check."),
    )

    @app.get("/study/receipt")
    def receipt():
        state = "busy"
        if computers._lock.acquire(blocking=False):
            try:
                computer = computers._computers.get(bot["id"])
                state = (
                    "live"
                    if computer and not computer.retired and computer.executor.alive()
                    else "not_running"
                )
            finally:
                computers._lock.release()
        return {
            "saved": saved,
            "usage": app.state.store.usage(settings.owner),
            "computer_state": state,
        }

    if config.get("visual"):

        @app.get("/study/visual-label")
        def visual_label():
            return {"label": config["visual"]["label"]}

    if background_read:

        @app.get("/study/background-evidence")
        def background_evidence():
            with app.state.store.connect() as db:
                events = [
                    dict(r) for r in db.execute("SELECT task_id,kind,data FROM events ORDER BY seq")
                ]
                calls = [
                    dict(r) for r in db.execute("SELECT model,status,usage FROM provider_calls")
                ]
            return {
                "slow_started": slow_started.is_set(),
                "marker": marker,
                "events": events,
                "calls": calls,
            }

        @app.post("/study/release-page")
        def release_page():
            slow_release.set()
            return {"released": True}

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        computers.close()
        page.shutdown()


def background_exercise(client, bot, other, private, name):
    """Live model handoff plus a concurrent message during an actual HTTP read."""
    import base64

    chat_response = client.post(f"/v1/bots/{bot['id']}/chats", json={"title": "Browser continuity"})
    chat_response.raise_for_status()
    chat = chat_response.json()

    def send(message):
        response = client.post(
            f"/v1/chats/{chat['id']}/tasks",
            json={"message": message, "request_id": uuid4().hex},
        )
        response.raise_for_status()
        return response.json()

    task = send(
        "Run this read-only synthetic browser continuity check. First navigate to "
        "http://127.0.0.1:8767/ and retain its source_id. Then use continue_in_background. "
        "In the background, use browser_source to read that same retained observation, "
        "then navigate to http://127.0.0.1:8767/slow and report the exact observed marker "
        "and page path. Do not click, type, create files or memories, or use external sites. "
        "The slow page is deliberately delayed for a concurrent conversation check."
    )
    print(
        json.dumps(
            {"ready": True, "container": name, "evidence": str(private), "task": task["id"]}
        ),
        flush=True,
    )
    deadline = time.monotonic() + 150
    responsive = False
    reply_seconds = []
    while time.monotonic() < deadline:
        evidence = client.get("/study/background-evidence").json()
        if evidence["slow_started"]:
            reply_deadline = time.monotonic() + 16
            for question, expected in (("7 times 8", "56"), ("9 times 9", "81")):
                started = time.monotonic()
                second = send(
                    f"Separate quick question while the reading continues: what is {question}? Just answer the number; do not change or inspect the background task."
                )
                responsive = False
                while time.monotonic() < reply_deadline:
                    reply = client.get(f"/v1/tasks/{second['id']}").json()
                    if reply["status"] in {"completed", "failed", "cancelled"}:
                        reply_seconds.append(time.monotonic() - started)
                        original = client.get(f"/v1/tasks/{task['id']}").json()
                        responsive = (
                            reply["status"] == "completed"
                            and expected in reply["output"]
                            and original["status"] == "waiting"
                        )
                        break
                    time.sleep(0.2)
                if not responsive:
                    break
            client.post("/study/release-page").raise_for_status()
            break
        current = client.get(f"/v1/tasks/{task['id']}").json()
        if current["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    while time.monotonic() < deadline:
        current = client.get(f"/v1/tasks/{task['id']}").json()
        if current["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    evidence = client.get("/study/background-evidence").json()
    conversation = client.get(f"/v1/chats/{chat['id']}").json()
    receipt = client.get("/study/receipt").json()
    frame = client.get(f"/v1/bots/{bot['id']}/computer").json()
    if frame["state"] == "live":
        (private / "computer.jpg").write_bytes(base64.b64decode(frame["image"].split(",", 1)[1]))
    result = {
        "evidence": evidence,
        "conversation": conversation,
        "receipt": receipt,
        "concurrent_reply_before_read_completed": responsive,
        "concurrent_reply_seconds": reply_seconds,
        "computer_state": frame["state"],
        "other_bot_state": client.get(f"/v1/bots/{other['id']}/computer").json(),
    }
    (private / "background-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    assert current["status"] == "completed", current
    assert evidence["marker"] in current["output"], current["output"]
    assert responsive and len(reply_seconds) == 2, (
        "Concurrent reply did not complete during the delayed read; inspect saved evidence"
    )
    assert any(item["kind"] == "background_started" for item in evidence["events"])
    assert any(
        item["kind"] == "tool_completed" and "browser_source" in item["data"]
        for item in evidence["events"]
    )
    lifecycle = subprocess.run(
        ["docker", "--context", "desktop-linux", "exec", "-i", name, "python", "-"],
        input=Path(__file__).with_name("bots_browser_lifecycle.py").read_bytes(),
        capture_output=True,
        timeout=45,
        check=True,
    )
    (private / "lifecycle.json").write_bytes(lifecycle.stdout)
    print(
        json.dumps(
            {
                "passed": True,
                "evidence": str(private),
                "usage": receipt["usage"],
                "concurrent_reply_seconds": reply_seconds,
            }
        ),
        flush=True,
    )


def approval_exercise(client, bot, other, private, name):
    """Real model proposals and actions; only the synthetic user's approval is automated."""
    import base64

    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def post(path, body):
        response = client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def wait(task_id):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = get(f"/v1/tasks/{task_id}")
            if result["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return result
            time.sleep(0.3)
        raise RuntimeError("Approval study exceeded its bounded task deadline.")

    chat = post(f"/v1/bots/{bot['id']}/chats", {"title": "Website approval study"})
    marker = "Κέδρος — 日本語 — " + uuid4().hex[:8]
    evidence = {"marker": marker, "passed": False}
    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    try:
        request = post(
            f"/v1/chats/{chat['id']}/tasks",
            {
                "message": f"On http://127.0.0.1:8767/ save this exact note once: {marker}. Check that it was saved. If website permission is needed, show me the approval and wait.",
                "request_id": uuid4().hex,
            },
        )
        evidence["request"] = wait(request["id"])
        assert evidence["request"]["status"] == "completed"
        proposals = get(f"/v1/bots/{bot['id']}/proposals")
        proposal = next(p for p in proposals if p["source_task_id"] == request["id"])
        assert proposal["browser_origin"] == "http://127.0.0.1:8767"
        assert marker in proposal["instructions"]
        assert get("/study/receipt")["saved"] == []
        evidence["no_write_before_approval"] = True
        denied = client.patch(
            f"/v1/bots/{other['id']}/proposals/{proposal['id']}",
            json={
                "action": "accept",
                "expected_revision": proposal["revision"],
            },
        )
        assert denied.status_code == 404
        decision = {
            "action": "accept",
            "expected_revision": proposal["revision"],
            "browser_origin": proposal["browser_origin"],
        }
        accepted = client.patch(f"/v1/bots/{bot['id']}/proposals/{proposal['id']}", json=decision)
        accepted.raise_for_status()
        accepted = accepted.json()
        retry = client.patch(f"/v1/bots/{bot['id']}/proposals/{proposal['id']}", json=decision)
        retry.raise_for_status()
        assert retry.json()["task_id"] == accepted["task_id"]
        evidence["accepted"] = accepted
        evidence["action"] = wait(accepted["task_id"])
        receipt = get("/study/receipt")
        evidence["action_receipt"] = receipt
        assert evidence["action"]["status"] == "completed"
        assert receipt["saved"] == [{"note": marker}], receipt["saved"]
        assert marker in evidence["action"]["output"]
        frame = get(f"/v1/bots/{bot['id']}/computer")
        if frame["state"] == "live":
            (private / "approved-save.jpg").write_bytes(
                base64.b64decode(frame["image"].split(",", 1)[1])
            )
        # A new request must acquire a new grant, even though the computer is reused.
        followup = post(
            f"/v1/chats/{chat['id']}/tasks",
            {
                "message": f"Now save a second note on the same site, exactly: {marker} second. Request website permission if needed, and wait for me.",
                "request_id": uuid4().hex,
            },
        )
        evidence["followup"] = wait(followup["id"])
        assert evidence["followup"]["status"] == "completed"
        second = next(
            p
            for p in get(f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == followup["id"]
        )
        assert (
            second["status"] == "pending" and second["browser_origin"] == proposal["browser_origin"]
        )
        assert get("/study/receipt")["saved"] == [{"note": marker}]
        dismissed = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{second['id']}",
            json={
                "action": "dismiss",
                "expected_revision": second["revision"],
            },
        )
        dismissed.raise_for_status()
        evidence["permission_not_inherited"] = True
        evidence["passed"] = True
    finally:
        evidence["final_receipt"] = get("/study/receipt")
        evidence["conversation"] = get(f"/v1/chats/{chat['id']}")
        evidence["proposals"] = get(f"/v1/bots/{bot['id']}/proposals")
        evidence["traces"] = get("/study/traces")
        (private / "approval-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    "passed": evidence["passed"],
                    "evidence": str(private),
                    "usage": evidence["final_receipt"]["usage"],
                }
            ),
            flush=True,
        )


def controls_exercise(client, bot, other, private, name):
    """Real GLM decisions using the richer controls; no scripted model calls."""
    import base64

    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def post(path, body):
        response = client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def wait(task_id):
        deadline = time.monotonic() + 220
        while time.monotonic() < deadline:
            result = get(f"/v1/tasks/{task_id}")
            if result["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return result
            time.sleep(0.3)
        raise RuntimeError("Controls study exceeded its task deadline.")

    chat = post(f"/v1/bots/{bot['id']}/chats", {"title": "Native browser controls"})
    marker = "Νύχτα — 日本語 — " + uuid4().hex[:8]
    evidence = {"passed": False, "marker": marker}
    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    try:
        request = post(
            f"/v1/chats/{chat['id']}/tasks",
            {
                "message": f"On http://127.0.0.1:8767/ choose 日本語 in Region, enter the exact Note {marker}, and submit once by pressing Enter in Note. Also scroll the Feed to its bottom. Confirm the saved note/region and the feed's actual final status. If website permission is needed, request approval and wait.",
                "request_id": uuid4().hex,
            },
        )
        evidence["request"] = wait(request["id"])
        assert evidence["request"]["status"] == "completed"
        proposal = next(
            p
            for p in get(f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == request["id"]
        )
        assert proposal["browser_origin"] == "http://127.0.0.1:8767"
        assert get("/study/receipt")["saved"] == []
        decision = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            json={
                "action": "accept",
                "expected_revision": proposal["revision"],
                "browser_origin": proposal["browser_origin"],
            },
        )
        decision.raise_for_status()
        evidence["action"] = wait(decision.json()["task_id"])
        receipt = get("/study/receipt")
        assert evidence["action"]["status"] == "completed", evidence["action"]
        assert receipt["saved"] == [
            {"note": marker, "region": "日本語", "value": "same", "index": 2}
        ], receipt["saved"]
        traces = get("/study/traces")
        used = {c["name"] for t in traces for c in t["tool_calls"]}
        assert {"browser_select", "browser_key", "browser_scroll"} <= used, used
        assert "Feed reached its bottom." in json.dumps(traces, ensure_ascii=False)
        assert marker in evidence["action"]["output"]
        assert get(f"/v1/bots/{other['id']}/computer")["state"] == "not_running"
        frame = get(f"/v1/bots/{bot['id']}/computer")
        if frame["state"] == "live":
            (private / "controls.jpg").write_bytes(
                base64.b64decode(frame["image"].split(",", 1)[1])
            )
        evidence["passed"] = True
    finally:
        evidence["receipt"] = get("/study/receipt")
        evidence["conversation"] = get(f"/v1/chats/{chat['id']}")
        evidence["traces"] = get("/study/traces")
        (private / "controls-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    "passed": evidence["passed"],
                    "evidence": str(private),
                    "usage": evidence["receipt"]["usage"],
                }
            ),
            flush=True,
        )


def visual_exercise(client, bot, other, private, name):
    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def wait(task_id):
        deadline = time.monotonic() + 220
        while time.monotonic() < deadline:
            current = get(f"/v1/tasks/{task_id}")
            if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return current
            time.sleep(0.3)
        raise RuntimeError("Visual study task deadline reached")

    response = client.post(f"/v1/bots/{bot['id']}/chats", json={"title": "Visual browser study"})
    response.raise_for_status()
    chat = response.json()
    label = get("/study/visual-label")["label"]
    evidence = {"passed": False, "label": label}
    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    try:
        response = client.post(
            f"/v1/chats/{chat['id']}/tasks",
            json={
                "message": f"On http://127.0.0.1:8767/ use the browser screenshot to locate and click the tile labeled {label} once. The labels are painted in the canvas. Verify which tile was selected. Request website permission if needed and wait.",
                "request_id": uuid4().hex,
            },
        )
        response.raise_for_status()
        request = response.json()
        evidence["request"] = wait(request["id"])
        assert evidence["request"]["status"] == "completed"
        proposal = next(
            p
            for p in get(f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == request["id"]
        )
        assert get("/study/receipt")["saved"] == []
        decision = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            json={
                "action": "accept",
                "expected_revision": proposal["revision"],
                "browser_origin": proposal["browser_origin"],
            },
        )
        decision.raise_for_status()
        evidence["action"] = wait(decision.json()["task_id"])
        receipt = get("/study/receipt")
        assert evidence["action"]["status"] == "completed", evidence["action"]
        assert (
            len(receipt["saved"]) == 1
            and receipt["saved"][0]["hit"]
            and receipt["saved"][0]["label"] == label
        ), receipt["saved"]
        traces = get("/study/traces")
        tools = [c["name"] for t in traces for c in t["tool_calls"]]
        assert "browser_screenshot" in tools and "browser_visual_click" in tools
        assert "browser_source" not in tools
        assert label in evidence["action"]["output"]
        assert any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for t in traces
            for m in t["messages"]
        )
        assert get(f"/v1/bots/{other['id']}/computer")["state"] == "not_running"
        frame = get(f"/v1/bots/{bot['id']}/computer")
        if frame["state"] == "live":
            (private / "visual.jpg").write_bytes(base64.b64decode(frame["image"].split(",", 1)[1]))
        evidence["passed"] = True
    finally:
        evidence["receipt"] = get("/study/receipt")
        evidence["traces"] = get("/study/traces")
        evidence["conversation"] = get(f"/v1/chats/{chat['id']}")
        (private / "visual-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--public-read",
        action="store_true",
        help="Test the default read-only app wiring with three model calls maximum",
    )
    parser.add_argument("--app", type=Path)
    mode.add_argument(
        "--background-read",
        action="store_true",
        help="Bounded live background/read continuity check through the Linux API, without native UI",
    )
    mode.add_argument(
        "--approval-flow",
        action="store_true",
        help="Real task-scoped website approval; twelve calls maximum",
    )
    mode.add_argument(
        "--controls",
        action="store_true",
        help="Real native dropdown, Enter and scroll study; twelve calls maximum",
    )
    mode.add_argument(
        "--visual",
        action="store_true",
        help="Real screenshot-to-coordinate GLM study; twelve calls maximum",
    )
    parser.add_argument("--port", type=int, default=7082)
    parser.add_argument("--image", default="alysis-bots-dev:local")
    parser.add_argument("--existing-network")
    parser.add_argument(
        "--native-ui",
        action="store_true",
        help="Keep the isolated desktop study open for native checks",
    )
    parser.add_argument("--subnet")
    args = parser.parse_args()
    if args.linux:
        linux()
    elif args.execute_live and args.app:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from bots_computer_view import host

        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        cfg = load_config()
        profile = get_profile(cfg, "zai-coding-plan")
        key = resolve_profile_api_key(cfg, "zai-coding-plan").key
        if profile is None or not key or profile.protocol != "openai_compat":
            raise RuntimeError("Saved Z.ai Coding Plan unavailable; no fallback.")
        from bots_browser_controls import PAGE
        from bots_browser_visual import fixture

        host(
            args.app.resolve(),
            args.subnet,
            study_script=Path(__file__).resolve(),
            bootstrap={
                "key": key,
                "base_url": profile.base_url,
                "public_read": args.public_read,
                "background_read": args.background_read,
                "approval_flow": args.approval_flow,
                "controls_page": PAGE if args.controls else None,
                "visual": fixture() if args.visual else None,
            },
            lazy_browser=True,
            exercise=(
                None
                if args.native_ui
                else visual_exercise
                if args.visual
                else controls_exercise
                if args.controls
                else approval_exercise
                if args.approval_flow
                else background_exercise
                if args.background_read
                else None
            ),
            port=args.port,
            image=args.image,
            name_prefix="bots-linux-night-20261019-"
            if args.approval_flow or args.controls or args.visual
            else "alysis-computer-view-",
            existing_network=args.existing_network,
        )
    else:
        parser.error("Use --execute-live --app for the explicit bounded live study.")
