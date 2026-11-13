"""Actual Linux popup/page control checks; synthetic sites, no external accounts."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

ROOT = """<!doctype html><meta charset="utf-8"><title>Workshop</title>
<style>body{font:24px sans-serif;padding:40px}a,button,input{font:inherit;margin:15px}</style>
<h1>Workshop</h1><a id="open" href="/review" target="_blank" rel="noopener">Open review in new tab</a>
<a id="cross" href="http://127.0.0.1:8768/review" target="_blank">Other site</a>
<a id="blocked" href="http://127.0.0.1:8769/review" target="_blank">Blocked private site</a>
<p id="receipt">No saved receipt yet</p><script>setInterval(async()=>{
let r=await(await fetch('/status')).json();if(r.saved.length)document.querySelector('#receipt').textContent='Saved '+r.reference+': '+r.saved[0];},300)</script>"""
REVIEW = """<!doctype html><meta charset="utf-8"><title>Review workshop</title>
<style>body{font:24px sans-serif;padding:40px}button,input{font:inherit;margin:15px}</style>
<h1>Review workshop</h1><label>Workshop note <input id="note"></label>
<button id="save">Save workshop once</button><p id="status">Not saved</p>
<button id="close">Close this review tab</button><script>
document.querySelector('#save').onclick=async e=>{e.currentTarget.disabled=true;
let r=await(await fetch('/save',{method:'POST',body:document.querySelector('#note').value})).json();
document.querySelector('#status').textContent='Saved '+r.reference;};
document.querySelector('#close').onclick=()=>window.close();</script>"""


class Site:
    def __init__(self):
        self.saved, self.cross_saved, self.private_gets = [], [], []
        self.reference = "TAB-" + uuid4().hex[:10]
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.server.server_port == 8769:
                    site.private_gets.append(self.path)
                body = (
                    json.dumps(dict(saved=site.saved, reference=site.reference))
                    if self.path == "/status"
                    else REVIEW
                    if self.path == "/review"
                    else ROOT
                ).encode()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/json" if self.path == "/status" else "text/html; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                value = self.rfile.read(int(self.headers["Content-Length"])).decode()
                (site.saved if self.server.server_port == 8767 else site.cross_saved).append(value)
                body = json.dumps(dict(reference=site.reference)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.servers = [
            ThreadingHTTPServer(("127.0.0.1", port), Handler) for port in (8767, 8768, 8769)
        ]
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:8767"

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()


def computers(site, owner):
    from alysis_code.bots.browser import BrowserComputers
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.computer import ComputerViews

    return BrowserComputers(
        owner,
        ComputerViews(),
        lambda bot, cancel: LinuxBrowserComputer(
            bot, cancel, preview_urls=lambda: (site.url + "/", "http://127.0.0.1:8768/")
        ),
        interactive_previews=False,
    )


def linux_checks():
    from alysis_code.cancellation import EventCancellationToken

    site = Site()
    adapter = computers(site, "checks")
    task = dict(id="checks", bot_id="checks")
    event = threading.Event()
    token = EventCancellationToken(event)

    def call(name, **args):
        return adapter.call(task, name, args, token, approved_origins=(site.url,))

    def target(view, identifier):
        return next(
            n["node"]
            for n in view["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == identifier
        )

    def click(view, identifier):
        return call("browser_click", source_id=view["source_id"], node=target(view, identifier))

    def inventory(count):
        for _ in range(40):
            listing = call("browser_pages")
            if len(listing["pages"]) == count:
                return listing
            time.sleep(0.1)
        raise AssertionError(listing)

    def switch(listing, url):
        page = next(p for p in listing["pages"] if p["url"] == url)
        return call("browser_switch_page", source_id=listing["source_id"], page_id=page["page_id"])

    try:
        root = call("browser_navigate", url=site.url + "/")
        click(root, "open")
        listing = inventory(2)
        review = switch(listing, site.url + "/review")
        try:
            click(root, "open")
        except ValueError:
            pass
        else:
            raise AssertionError("Old page reference accepted after switching")
        marker = "Νεφέλη / 東京 / " + uuid4().hex[:6]
        review = call(
            "browser_type", source_id=review["source_id"], node=target(review, "note"), text=marker
        )
        review = click(review, "save")
        for _ in range(60):
            if site.saved == [marker]:
                break
            time.sleep(0.1)
        assert site.saved == [marker], site.saved
        root = switch(inventory(2), site.url + "/")
        for _ in range(60):
            if site.reference in json.dumps(root, ensure_ascii=False):
                break
            time.sleep(0.1)
            root = call("browser_observe")
        assert site.reference in json.dumps(root, ensure_ascii=False)
        click(root, "cross")
        cross = switch(inventory(3), "http://127.0.0.1:8768/review")
        try:
            click(cross, "save")
        except ValueError as exc:
            assert "approval" in str(exc), str(exc)
        else:
            raise AssertionError("Unapproved popup action accepted")
        assert site.cross_saved == []
        review = switch(inventory(3), site.url + "/review")
        # Popup closure can race the post-action observation; no replay.
        try:
            click(review, "close")
        except ValueError:
            pass
        assert adapter._computers, "Closing a popup must preserve its sibling pages"
        listing = inventory(2)
        root = switch(listing, site.url + "/")
        root = call("browser_navigate", url=site.url + "/")
        event.set()
        adapter.reap()
        assert not adapter._computers
        event.clear()
        fresh = call("browser_navigate", url=site.url + "/")
        assert len(inventory(1)["pages"]) == 1
        assert fresh["source_id"] != root["source_id"]
        try:
            click(root, "open")
        except ValueError:
            pass
        else:
            raise AssertionError("Pre-restart reference accepted")
        try:
            click(fresh, "blocked")
            for _ in range(40):
                time.sleep(0.1)
                call("browser_observe")
        except ValueError:
            pass
        else:
            raise AssertionError("Private popup did not fail closed")
        assert site.private_gets == []
        assert not adapter._computers
        print(
            json.dumps(
                dict(
                    passed=True,
                    model_calls=0,
                    saved=site.saved,
                    blocked_cross_origin=True,
                    stopped_and_recreated=True,
                )
            ),
            flush=True,
        )
    except Exception as exc:
        print(
            json.dumps({"error_type": type(exc).__name__, "underlying": str(exc.__context__)}),
            flush=True,
        )
        raise
    finally:
        adapter.close()
        site.close()


def linux():
    import sys
    from pathlib import Path

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
        max_calls=14,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    adapter = computers(site, settings.owner)
    app = create_app(settings, browser_computers=adapter)
    store = app.state.store
    store.save_bot(
        settings.owner,
        BotInput(name="Computer view test", purpose="Help with a synthetic workshop review."),
    )
    store.save_bot(settings.owner, BotInput(name="No computer test", purpose="Remain idle."))
    attempts = []
    lock = threading.Lock()
    original = app.state.runtime.client_factory

    class Trace:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            with lock:
                if len(attempts) >= 16:
                    raise RuntimeError("Study call limit reached; no provider call attempted")
                record = dict(confirmed=False)
                attempts.append(record)
            try:
                response = self.client.chat(**kwargs)
                record.update(
                    confirmed=True,
                    content=response.content,
                    tools=[dict(name=c.name, arguments=c.arguments) for c in response.tool_calls],
                )
                return response
            except BaseException as exc:
                record["error_type"] = type(exc).__name__
                raise

    app.state.runtime.client_factory = lambda task: Trace(original(task))

    @app.get("/study/evidence")
    def evidence():
        return dict(
            saved=site.saved,
            cross_saved=site.cross_saved,
            reference=site.reference,
            attempts=attempts,
            usage=store.usage(settings.owner),
        )

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        adapter.close()
        site.close()


def exercise(client, bot, other, private, container):
    def api(method, path, data=None):
        response = client.request(method, path, **({"json": data} if data is not None else {}))
        response.raise_for_status()
        return response.json()

    def wait(task_id):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            task = api("GET", "/v1/tasks/" + task_id)
            if task["status"] in ("completed", "failed", "cancelled", "interrupted"):
                return task
            time.sleep(0.3)
        raise TimeoutError("Bounded popup study timed out")

    marker = "Νεφέλη / 東京 / " + uuid4().hex[:8]
    result = dict(passed=False, marker=marker)
    try:
        chat = api("POST", f"/v1/bots/{bot['id']}/chats", {})
        request = api(
            "POST",
            f"/v1/chats/{chat['id']}/tasks",
            dict(
                request_id=uuid4().hex,
                message=f"On http://127.0.0.1:8767/ use the Open review in new tab link. Switch to that review tab, enter exactly {marker} as the workshop note and save once. Then return to the original Workshop tab, wait for its actual saved receipt, and report the reference and exact saved note. Do not use the Other site link. Request website permission first and wait for approval.",
            ),
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
            dict(
                action="accept",
                expected_revision=proposal["revision"],
                browser_origin=proposal["browser_origin"],
            ),
        )
        result["action"] = wait(accepted["task_id"])
        evidence = api("GET", "/study/evidence")
        assert result["action"]["status"] == "completed", result["action"].get("error")
        assert evidence["saved"] == [marker], evidence["saved"]
        assert evidence["cross_saved"] == []
        assert evidence["reference"] in result["action"]["output"]
        tools = [c["name"] for attempt in evidence["attempts"] for c in attempt.get("tools", [])]
        assert tools.count("browser_switch_page") >= 2, tools
        assert api("GET", f"/v1/bots/{other['id']}/computer")["state"] == "not_running"
        result["passed"] = True
    finally:
        result["evidence"] = api("GET", "/study/evidence")
        (private / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        attempts = result["evidence"]["attempts"]
        print(
            json.dumps(
                dict(
                    passed=result["passed"],
                    attempted=len(attempts),
                    confirmed=sum(a["confirmed"] for a in attempts),
                    usage=result["evidence"]["usage"],
                    evidence=str(private),
                )
            ),
            flush=True,
        )


if __name__ == "__main__":
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux-checks", action="store_true")
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--existing-network")
    args = parser.parse_args()
    if args.linux_checks:
        linux_checks()
    elif args.linux:
        linux()
    elif args.execute_live:
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
            Path(__file__).resolve().parents[2],
            None,
            study_script=Path(__file__).resolve(),
            bootstrap=dict(key=key, base_url=profile.base_url),
            lazy_browser=True,
            exercise=exercise,
            port=7101,
            image="alysis-peer-20261019-vm-tabs:dev",
            name_prefix="alysis-peer-20261019-vm-tabs-",
            existing_network=args.existing_network,
            memory="2g",
            tmpfs_size="512m",
        )
    else:
        parser.error("Use --execute-live, or --linux-checks inside the isolated lab")
