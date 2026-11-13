"""Real Chromium/Quill probe; scripted actions, no model or personal accounts.

Mount a pinned Quill 2.0.3 distribution at the asset path below. It is a QA
dependency only, never an application-specific production browser adapter.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.clarifications import ClarificationStore, ReadBrowserDraft
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken


class EditorSite:
    def __init__(self):
        self.saved = []
        self.write_release = threading.Event()
        self.slow_release = threading.Event()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/quill.js":
                    body = Path("/study/.dev/bots-rich-editor-20261022/quill-2.0.3.js").read_bytes()
                    mime = "text/javascript"
                else:
                    mode = self.path.strip("/")
                    editor = (
                        (
                            '<div id="container"></div><script src="/quill.js"></script>'
                            '<script>const q=new Quill("#container",{modules:{toolbar:false}});'
                            'q.root.id="editor";q.root.setAttribute("aria-label","Document");'
                            'q.setText("Original draft");</script>'
                        )
                        if mode == "quill"
                        else (
                            '<div id="editor" aria-label="Document" contenteditable="'
                            + (
                                "TRUE"
                                if mode == "uppercase"
                                else "plaintext-only"
                                if mode == "plain"
                                else "true"
                            )
                            + '"><p>Original <strong>draft</strong></p></div>'
                        )
                    )
                    body = (
                        '<!doctype html><meta charset="utf-8"><title>Editor study</title>'
                        "<style>#editor{min-height:80px;border:1px solid;padding:8px}</style>"
                        "<h1>Document</h1>" + editor + '<button id="save">Save document</button>'
                        '<p id="status">Not saved</p><script>'
                        'document.querySelector("#save").onclick=async()=>{'
                        'const e=document.querySelector("#editor");'
                        'const r=await fetch("/save",{method:"POST",body:JSON.stringify({'
                        "text:e.innerText,html:e.innerHTML,"
                        'delta:typeof q==="undefined"?null:q.getContents()})});'
                        'document.querySelector("#status").textContent=await r.text();};</script>'
                    ).encode()
                    mime = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                site.saved.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Saved document")

        self.server = ThreadingHTTPServer(("127.0.0.1", 8767), Handler)
        self.url = "http://127.0.0.1:8767"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    site = EditorSite()
    owner = "rich-probe"
    store = Store(Path("/tmp/rich-probe/bots.db"))
    bot = store.save_bot(owner, BotInput(name="Editor probe", purpose="Synthetic work"))
    chat = store.create_chat(owner, bot["id"], "Document")
    store.enqueue(owner, chat["id"], TaskInput(message="Edit document", request_id=uuid4().hex))
    task = store.claim(owner)
    token = EventCancellationToken(threading.Event())
    executors = []

    def factory(bot, cancel):
        executor = LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (site.url + "/",))
        executor.retire_on_release = True
        executors.append(executor)
        return executor

    adapter = BrowserComputers(owner, ComputerViews(), factory, store=store)

    def call(tool, **args):
        return adapter.call(task, tool, args, token, approved_origins=(site.url,))

    def node(view, identity):
        return next(
            n["node"]
            for n in view["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == identity
        )

    results = []
    try:
        for mode in ("native", "plain", "uppercase", "quill"):
            result = {"editor": mode}
            results.append(result)
            expected = "Νεφέλη / 東京 " + uuid4().hex + "\nSecond paragraph — café"
            try:
                view = call("browser_navigate", url=site.url + "/" + mode)
                view = call(
                    "browser_type",
                    source_id=view["source_id"],
                    node=node(view, "editor"),
                    text=expected,
                )
                draft = adapter.checkpoint_draft(task)
                result["draft"] = draft
                questions = ClarificationStore(store, owner)
                questions.save_browser_draft(task, draft)
                # Check exact typed data through the website's own save path.
                view = call("browser_observe")
                call("browser_click", source_id=view["source_id"], node=node(view, "save"))
                result["saved"] = site.saved[-1]
                saved = site.saved[-1]
                # innerText inserts visual spacing between <p> elements. Quill's
                # own document model supplies the canonical text/newline check.
                result["paragraphs_exact"] = saved["text"].split("\n\n") == expected.split("\n")
                result["editor_document_exact"] = (
                    saved["delta"] == {"ops": [{"insert": expected + "\n"}]}
                    if mode == "quill"
                    else None
                )
                adapter.release(task["id"], cancelled=False)
                recovered = ClarificationStore(Store(store.path), owner).read_browser_draft(
                    task, ReadBrowserDraft(source_task_id=task["id"])
                )
                result["durable_snapshot_equal"] = json.loads(recovered["content"]) == draft
                fields = draft["pages"][0]["fields"]
                editor = next(f for f in fields if f["type"] == "contenteditable")

                def texts(tree):
                    return (
                        [tree["text"]]
                        if "text" in tree
                        else [t for child in tree["children"] for t in texts(child)]
                    )

                result["draft_text_fragments_exact"] = texts(
                    editor["content_tree"]
                ) == expected.split("\n")
                result["partial_fidelity_explicit"] = (
                    not draft["complete"] and not editor["complete"]
                )
                result["computer_reaped"] = not executors[-1].alive()
            except Exception as exc:
                result["error"] = str(exc)
                adapter.release(task["id"], cancelled=False)
        print(
            json.dumps(
                {"results": results, "computers_reaped": all(not e.alive() for e in executors)},
                ensure_ascii=False,
            )
        )
        if not all(
            not r.get("error")
            and r.get("paragraphs_exact")
            and r.get("editor_document_exact") is not False
            and r.get("durable_snapshot_equal")
            and r.get("draft_text_fragments_exact")
            and r.get("partial_fidelity_explicit")
            and r.get("computer_reaped")
            for r in results
        ):
            raise SystemExit(1)
    finally:
        adapter.close()
        site.close()


if __name__ == "__main__":
    main()
