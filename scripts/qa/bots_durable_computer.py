"""Provider-free qualification on real Chromium, bwrap and a durable volume.

Run --prepare then --reopen in separate disposable Linux containers using the
same /var/lib/alysis volume. Synthetic data only; fixture grants are not evidence
that a model chose the actions. No external service or account is contacted.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from openpyxl import Workbook, load_workbook

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.bots.workspaces import WorkspaceStore
from alysis_code.ide.managed_browser import BrowserError

ROOT = Path("/var/lib/alysis/durable-study")


def main(reopen=False):
    ROOT.mkdir(mode=0o700, exist_ok=True)
    store = Store(ROOT / "bots.db")
    checks = []
    if reopen:
        prior = json.loads((ROOT / "receipt.json").read_text())
        bot = store.bot("owner", prior["bot"])
        chat = {"id": prior["chat"]}
    else:
        bot = store.save_bot("owner", BotInput(name="Durable test", purpose="Synthetic files"))
        chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue("owner", chat["id"], TaskInput(message="Check files", request_id=uuid4().hex))
    task = store.claim("owner")
    token = BotRuntime(store, "owner", lambda _: None).task_token(task["id"])
    views = ComputerViews()
    desktop = DesktopComputers(
        "owner", views, store=store, workspaces=WorkspaceStore(ROOT / "workspaces")
    )

    def call(name, args):
        return desktop.call(task, name, args, token, approved=True)

    def run(code):
        result = call("computer_run", {"command": "python - <<'CODE'\n" + code + "\nCODE"})
        assert result["exit_code"] == 0, result
        return result

    browser = server = None
    try:
        if reopen:
            result = run(
                "import hashlib,json; from pathlib import Path; "
                "print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() "
                "for p in Path('/workspace').iterdir() if p.is_file()}))"
            )
            assert result["workspace_persistent"] and result["workspace_reopened"]
            assert result["workspace_id"] == prior["workspace_id"]
            assert result["computer_session_id"] != prior["computer_session_id"]
            files = json.loads(result["output"])
            assert all(files[name] == digest for name, digest in prior["files"].items())
            # Kill the actual owned bwrap executor, then observe honest recovery
            # with the same files and a fresh action/session identity.
            executor = desktop._computers[bot["id"]].executor
            executor._process.kill()
            executor._process.wait(timeout=5)
            recovered = run(
                "from pathlib import Path;print(Path('/workspace/result.xlsx').stat().st_size)"
            )
            assert recovered["workspace_unclean_shutdown"] and recovered["computer_created"]
            assert recovered["computer_session_id"] != result["computer_session_id"]
            assert all(
                hashlib.sha256((executor.workspace / name).read_bytes()).hexdigest() == digest
                for name, digest in prior["files"].items()
            )
            checks.append(
                "actual executor death preserves files and reports an unclean new session"
            )
            for file in store.list_files("owner", bot["id"]):
                assert (
                    hashlib.sha256(store.file_data("owner", bot["id"], file["id"])).hexdigest()
                    == file["sha256"]
                )
            checks.append("new container and executor recover exact workspace and library bytes")
        else:
            book = Workbook()
            sheet = book.active
            sheet.append(["client", "amount"])
            values = [("東京", int(uuid4().hex[:4], 16)), ("Αθήνα", int(uuid4().hex[:4], 16))]
            for row in values:
                sheet.append(row)
            buffer = io.BytesIO()
            book.save(buffer)
            source = buffer.getvalue()

            class Page(BaseHTTPRequestHandler):
                def do_GET(self):
                    file = self.path in {"/book", "/large"}
                    data = (
                        (source if self.path == "/book" else b"x" * (50 * 1024 * 1024 + 1))
                        if file
                        else b'<h1>Owned fixture</h1><a href="/book" download>Download workbook</a><a href="/large" download>Large file</a>'
                    )
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "application/octet-stream" if file else "text/html"
                    )
                    self.send_header("Content-Length", str(len(data)))
                    if file:
                        self.send_header(
                            "Content-Disposition", 'attachment; filename="server-name.xlsx"'
                        )
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                def log_message(self, *_):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = f"http://127.0.0.1:{server.server_port}"

            def factory(bot_id, cancel):
                executor = LinuxBrowserComputer(bot_id, cancel, preview_urls=lambda: [origin])
                download = executor.browser.download

                def observed_download(*args, **kwargs):
                    try:
                        return download(*args, **kwargs)
                    except BrowserError as error:
                        print(json.dumps({"synthetic_download_error": str(error)}), flush=True)
                        raise

                executor.browser.download = observed_download
                return executor

            browser = BrowserComputers(
                "owner",
                views,
                factory,
                store=store,
            )

            def browse(name, args):
                return browser.call(task, name, args, token, approved_origins=(origin,))

            page = browse("browser_navigate", {"url": origin})

            # Fixture only: select the actual observed link, never fabricate node IDs.
            def target(observation, href):
                node = next(
                    n
                    for n in observation["dom"]["documents"][0]["nodes"]
                    if n.get("attributes", {}).get("href") == href
                )
                return {"source_id": observation["source_id"], "node": node["node"]}

            downloaded = browse("browser_download", {**target(page, "/book"), "name": "入力.xlsx"})
            assert store.file_data("owner", bot["id"], downloaded["id"]) == source
            checks.append("actual observed Chromium download saves exact binary bytes and digest")
            imported = call("computer_import_file", {"file_id": downloaded["id"]})
            assert imported["sha256"] == hashlib.sha256(source).hexdigest()
            run(
                "from openpyxl import load_workbook,Workbook\n"
                "from reportlab.pdfgen.canvas import Canvas\n"
                "book=load_workbook('/workspace/入力.xlsx',read_only=True,data_only=True)\n"
                "total=sum(row[1] for row in book.active.iter_rows(min_row=2,values_only=True))\n"
                "out=Workbook();out.active.append(['total',total]);out.save('/workspace/result.xlsx')\n"
                "pdf=Canvas('/workspace/report.pdf');pdf.drawString(50,750,'Total: '+str(total));pdf.save()\n"
            )
            saved = call("computer_save_file", {"name": "result.xlsx"})
            actual = load_workbook(io.BytesIO(store.file_data("owner", bot["id"], saved["id"])))
            assert actual.active["B1"].value == sum(amount for _, amount in values)
            pdf = call("computer_save_file", {"name": "report.pdf"})
            assert store.file_data("owner", bot["id"], pdf["id"]).startswith(b"%PDF")
            assert store.file("owner", bot["id"], pdf["id"])["source_task_id"] == task["id"]
            checks.append("offline Python reads XLSX, computes real totals, exports XLSX and PDF")
            assert views.frame("owner", bot["id"])["state"] == "live"
            try:
                browser.call({**task, "id": "another-task"}, "browser_observe", {}, token)
            except ValueError:
                pass
            else:
                raise AssertionError("Another task took over the shared computer")
            page = browse("browser_observe", {})
            try:
                browse("browser_download", {**target(page, "/large"), "name": "large.bin"})
            except ValueError:
                pass
            else:
                raise AssertionError("Oversized download accepted")
            assert "large.bin" not in {f["name"] for f in store.list_files("owner", bot["id"])}
            checks.append("oversized browser download cannot publish partial bytes")
            run("import os;os.symlink('/etc/passwd','/workspace/unsafe')")
            try:
                call("computer_save_file", {"name": "unsafe"})
            except DesktopError:
                pass
            else:
                raise AssertionError("Symlink escaped the workspace")
            run("from pathlib import Path;Path('/workspace/unsafe').unlink()")
            first_session = imported["computer_session_id"]
            workspace = desktop._computers[bot["id"]].executor.workspace
            hashes = {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.iterdir()
            }
            desktop.release(task["id"], cancelled=False)
            restored = run(
                "from pathlib import Path;print(sorted(p.name for p in Path('/workspace').iterdir()))"
            )
            assert restored["computer_created"] and restored["workspace_reopened"]
            assert restored["computer_session_id"] != first_session
            assert all(
                hashlib.sha256((workspace / n).read_bytes()).hexdigest() == d
                for n, d in hashes.items()
            )
            checks.append("task-end closes applications; recreated computer preserves exact files")
            (ROOT / "receipt.json").write_text(
                json.dumps(
                    {
                        "bot": bot["id"],
                        "chat": chat["id"],
                        "files": hashes,
                        "workspace_id": restored["workspace_id"],
                        "computer_session_id": restored["computer_session_id"],
                    }
                )
            )
        store.finish("owner", task["id"], "completed", content="Independent checks passed")
    finally:
        desktop.close()
        if browser:
            browser.close()
        if server:
            server.shutdown()
    print(
        json.dumps(
            {
                "passed": True,
                "stage": "reopen" if reopen else "prepare",
                "checks": checks,
                "model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reopen", action="store_true")
    main(parser.parse_args().reopen)
