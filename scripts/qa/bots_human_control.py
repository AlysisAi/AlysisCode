"""Local human-control qualification server. Real private Linux screens.

Default model is explicitly scripted; --live uses only bootstrap model credentials.
The fixture page is synthetic. Input values are never logged. Docker/credentials
are supplied by an operator over stdin; this script does not provision Azure.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import uvicorn

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

PAGE = """<!doctype html><meta charset="utf-8"><title>Human handoff check</title>
<style>body{font:22px system-ui;padding:36px;background:#edf2f7;color:#172336}label{display:block;margin:16px 0}input,textarea,button{font:22px system-ui;padding:12px}input,textarea{display:block;width:500px}button{margin-top:18px}#result{margin-top:24px}small{display:block;margin:20px 0;color:#526078}</style>
<h1>Human handoff check</h1><small>Synthetic local page · no real account or credential</small>
<form id="form"><label>Name<input id="name" autocomplete="off"></label><label>Verification<input id="secret" type="password" autocomplete="one-time-code"></label><button>Complete verification</button></form>
<div id="result">Waiting for you</div><textarea id="notes" aria-label="Notes" placeholder="Unicode editing check"></textarea>
<script>
let submits=0; const form=document.querySelector('#form'),result=document.querySelector('#result');
form.addEventListener('submit',e=>{e.preventDefault();submits++;result.textContent='Verified by the user; submissions: '+submits;form.hidden=true;fetch('/result',{method:'POST',body:JSON.stringify({submissions:submits,name:document.querySelector('#name').value,private_entered:document.querySelector('#secret').value.length>0})});});
</script>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    config = json.loads(sys.stdin.readline())
    page_result = {}

    class Page(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(
                self.rfile.read(min(4096, int(self.headers.get("Content-Length", 0))))
            )
            page_result.update(data)
            self.send_response(204)
            self.end_headers()

        def do_GET(self):
            payload = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 8099), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:8099/"
    views = ComputerViews()
    settings = Settings(
        Path("/var/lib/alysis/handoff/bots.db"),
        config["token"],
        config.get("api_key", "unused"),
        config.get("base_url", "https://example.invalid"),
        config.get("model", "scripted-handoff"),
        reasoning_effort="low",
        browser_enabled=True,
        desktop_enabled=True,
    )
    manager = BrowserComputers(
        settings.owner,
        views,
        lambda bot, cancel: LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (url,)),
        interactive_previews=True,
    )

    class ScriptedClient:
        model = "scripted-handoff"
        supports_tool_calling = True

        def __init__(self):
            self.step = 0

        def chat(self, **kwargs):
            # Production reviewer remains active; only its model is scripted.
            if kwargs.get("tools") is None:
                return LLMResponse(
                    '{"decision":"ready","reason":"Fixture final reply"}',
                    [],
                    {},
                    usage=LLMUsage(1, 1, 2),
                )
            self.step += 1
            tools = {
                1: ("browser_navigate", {"url": url}),
                2: (
                    "request_computer_control",
                    {
                        "reason": "Complete the synthetic verification in View computer, then give control back."
                    },
                ),
                3: ("browser_observe", {}),
            }
            call = tools.get(self.step)
            return LLMResponse(
                ""
                if call
                else "I inspected the current page after you gave control back. The synthetic verification was completed once.",
                [ToolCall(uuid4().hex, *call)] if call else [],
                {},
                usage=LLMUsage(1, 1, 2),
            )

    clients = {}

    def factory(task):
        return clients.setdefault(task, ScriptedClient())

    app = create_app(
        settings,
        browser_computers=manager,
        computer_views=views,
        client_factory=None if args.live else factory,
    )
    runtime, store = app.state.runtime, app.state.store
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    existing = store.list_bots(settings.owner)
    bot = (
        existing[0]
        if existing
        else store.save_bot(
            settings.owner,
            BotInput(
                name="Human control test",
                purpose="Work on the requested fixture. For a human verification step use request_computer_control, then inspect fresh state after control is returned. Never ask for credentials in chat.",
            ),
        )
    )
    chats = store.list_chats(settings.owner, bot["id"])
    chat = chats[0] if chats else store.create_chat(settings.owner, bot["id"], "Human handoff")

    @app.post("/qa/start")
    def start():
        page_result.clear()
        task = store.enqueue(
            settings.owner,
            chat["id"],
            TaskInput(
                message=f"Open {url} and ask me to complete its verification myself using computer control. After I give it back, inspect the page and report whether verification completed, without submitting again.",
                request_id=uuid4().hex,
            ),
        )
        runtime.wake()
        return {"bot_id": bot["id"], "task_id": task["id"]}

    @app.get("/qa/page")
    def page_state():
        return {
            "submissions": page_result.get("submissions", 0),
            "unicode_name": page_result.get("name") == "Καλημέρα 雲",
            "private_entered": page_result.get("private_entered", False),
        }

    @app.get("/qa/status")
    def status():
        return {
            "bot_id": bot["id"],
            "chat_id": chat["id"],
            "mode": "live" if args.live else "scripted",
            "computers": len(manager._computers),
        }

    print(
        json.dumps(
            {
                "ready_to_serve": True,
                "mode": "live" if args.live else "scripted",
                "bot_id": bot["id"],
            }
        ),
        flush=True,
    )
    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
