"""Opt-in OpenCode Go qualification of Bots browser lifecycle and interrupted work.

Run --execute-live on the host. Credentials travel only through stdin to a
temporary container. Sites, bots, approvals and data are synthetic. No Azure,
published ports, user browser profiles, or pay-as-you-go fallback are used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import secrets
import subprocess
import sys
import threading
import time
import zipfile
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


class Site:
    def __init__(self):
        self.quotes = [
            dict(name="Harbor", rate="18.25", code=uuid4().hex[:10]),
            dict(name="Garden", rate="21.50", code=uuid4().hex[:10]),
        ]
        self.registration_code = uuid4().hex[:12]
        self.saved = []
        self.draft_values = []
        self.draft_note = "Σχέδιο-東京-" + uuid4().hex
        self.ledger = "client,amount\n" + "".join(
            f"{name},{Decimal(secrets.randbelow(9000) + 1) / 100:.2f}\n"
            for name in ("Αθήνα", "東京", "Zürich")
        )
        self.slow_entered, self.slow_release = threading.Event(), threading.Event()
        self.write_received, self.write_release = threading.Event(), threading.Event()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/sales.csv":
                    data = site.ledger.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="sales.csv"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path == "/package":
                    body = '<h1>Monthly ledger</h1><a href="/sales.csv">Download sales.csv</a>'
                elif self.path == "/quotes":
                    body = "<h1>Venue quotes</h1>" + "".join(
                        f'<p><a href="/quote/{i}" target="_blank">{q["name"]} quote</a></p>'
                        for i, q in enumerate(site.quotes)
                    )
                elif self.path.startswith("/quote/"):
                    q = site.quotes[int(self.path.rsplit("/", 1)[1])]
                    body = f"<h1>{q['name']}</h1><p>Rate per person: EUR {q['rate']}. No extra charges.</p><p>Quote code: {q['code']}</p>"
                elif self.path == "/draft":
                    from html import escape

                    # Fresh page loads have a different default. The original
                    # note must come from the durable draft, not the new page.
                    note = site.draft_note if not site.draft_values else "Fresh-" + uuid4().hex
                    site.draft_values.append(note)
                    body = f"""<h1>Workshop draft</h1><label>Name <input id="name"></label>
                    <label>Draft note <textarea id="note">{escape(note)}</textarea></label>
                    <label>Rate <input id="rate"></label><button id="save">Save registration</button>
                    <p id="status">Not saved</p><script>
                    document.querySelector('#save').onclick=async e=>{{e.currentTarget.disabled=true;
                    let r=await(await fetch('/save',{{method:'POST',body:JSON.stringify({{
                    name:document.querySelector('#name').value,note:document.querySelector('#note').value,
                    rate:document.querySelector('#rate').value}})}})).json();
                    document.querySelector('#status').textContent='Saved: '+r.code;}};</script>"""
                elif self.path in {"/registration", "/registration-delayed"}:
                    body = """<h1>Workshop registration</h1>
                    <label>Name <input id="name"></label>
                    <a href="/rate" target="_blank">Current workshop rate</a>
                    <label>Rate <input id="rate"></label><button id="save">Save registration</button>
                    <p id="status">Not saved</p><script>
                    let dirty=false;document.querySelectorAll('input').forEach(e=>e.oninput=()=>dirty=true);
                    onbeforeunload=e=>{if(dirty){e.preventDefault();e.returnValue='';}};
                    document.querySelector('#save').onclick=async e=>{e.currentTarget.disabled=true;
                    let r=await(await fetch('/save',{method:'POST',body:JSON.stringify({
                    name:document.querySelector('#name').value,rate:document.querySelector('#rate').value})})).json();
                    dirty=false;document.querySelector('#status').textContent='Saved: '+r.code;};</script>"""
                    if self.path == "/registration-delayed":
                        body = body.replace("fetch('/save'", "fetch('/save-delayed'")
                elif self.path == "/registration-status":
                    from html import escape

                    body = (
                        "<h1>Registration status</h1><pre>"
                        + escape(
                            json.dumps(
                                dict(registrations=site.saved, code=site.registration_code),
                                ensure_ascii=False,
                            )
                        )
                        + "</pre>"
                    )
                elif self.path == "/rate":
                    body = "<h1>Current workshop rate</h1><p>Enter 34.75 in the registration rate field.</p>"
                elif self.path == "/slow":
                    site.slow_entered.set()
                    site.slow_release.wait(30)
                    body = "<h1>Delayed report</h1>"
                else:
                    self.send_error(404)
                    return
                data = ('<!doctype html><meta charset="utf-8"><title>Study</title>' + body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                if self.path not in {"/save", "/save-delayed"}:
                    self.send_error(404)
                    return
                site.saved.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if self.path == "/save-delayed":
                    site.write_received.set()
                    site.write_release.wait(30)
                data = json.dumps(dict(code=site.registration_code)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 8767), Handler)
        self.url = "http://127.0.0.1:8767"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.slow_release.set()
        self.write_release.set()
        self.server.shutdown()
        self.server.server_close()


def linux():
    import psutil

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.browser import BrowserComputers
    from alysis_code.bots.browser_linux import LinuxBrowserComputer
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
    from alysis_code.bots.models import BotInput, FileInput, TaskInput
    from alysis_code.bots.proposals import ActionDecision
    from alysis_code.bots.workspaces import WorkspaceStore
    from alysis_code.llm.types import LLMResponse, ToolCall

    config = json.loads(sys.stdin.readline())
    cases = config["cases"]
    if "rich_editor" in cases:
        from bots_rich_editor_probe import EditorSite

        assert cases == ["rich_editor"], "The editor study uses its own synthetic site"
        site = EditorSite()
    else:
        site = Site()
    attempts, actions, processes = [], [], {}
    seeded_fault_steps = []
    results = []
    desktop_actions = []
    command_received, command_release = threading.Event(), threading.Event()
    settings = Settings(
        Path("/tmp/study/bots.db"),
        secrets.token_urlsafe(36),
        config["key"],
        config["base_url"],
        config["model"],
        max_calls=64,
        output_tokens=8192,
        reasoning_effort="low",
    )

    def factory(bot, cancel):
        computer = LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (site.url + "/",))
        computer.retire_on_release = True
        return computer

    class ObservedBrowser(BrowserComputers):
        def call(self, task, name, args, token, **kwargs):
            record = dict(task_id=task["id"], tool=name, args=args)
            actions.append(record)
            try:
                result = super().call(task, name, args, token, **kwargs)
                record["result"] = result
                record["saved_registrations"] = len(site.saved)
                return result
            except Exception as exc:
                record["error"] = str(exc)
                raise
            finally:
                for process in psutil.Process().children(recursive=True):
                    processes[process.pid] = process

    adapter = ObservedBrowser(settings.owner, ComputerViews(), factory)

    class ObservedDesktop(OfflineLinuxDesktop):
        def call(self, owner, operation, arguments, **kwargs):
            record = dict(operation=operation, arguments=arguments)
            desktop_actions.append(record)
            result = super().call(owner, operation, arguments, **kwargs)
            record["result"] = result
            for process in psutil.Process().children(recursive=True):
                processes[process.pid] = process
            # Inject latency at a real command receipt, not model answers or tool
            # results. This creates a reproducible window for a user correction.
            if "correction" in cases and operation == "run" and not command_received.is_set():
                command_received.set()
                command_release.wait(60)
            return result

    desktop = (
        DesktopComputers(
            settings.owner,
            adapter.views,
            factory=ObservedDesktop,
            workspaces=WorkspaceStore(Path("/tmp/study/workspaces")),
        )
        if {"correction", "housekeeping", "helpers"}.intersection(cases)
        else None
    )
    app = create_app(settings, browser_computers=adapter, desktop_computers=desktop)
    runtime, store = app.state.runtime, app.state.store
    adapter.store = store
    original = runtime.client_factory

    class Trace:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            # An experiment budget only; production per-task defaults stay unbounded.
            if len(attempts) >= 64:
                raise RuntimeError("Experiment request budget exhausted")
            record = dict(
                confirmed=False,
                scripted_fault=bool(seeded_fault_steps),
                review=kwargs["messages"][0]["content"].startswith(
                    "Review a proposed Alysis bot reply"
                ),
            )
            started = time.monotonic()
            attempts.append(record)
            # Opt-in fault injection reproduces the observed redundant-card bug.
            # Tool execution and receipts remain real; review and repair use the provider.
            try:
                response = (
                    seeded_fault_steps.pop(0) if seeded_fault_steps else self.client.chat(**kwargs)
                )
            except Exception as exc:
                # Class names only: exception text may contain provider details.
                record["error_type"] = type(exc).__name__
                print(
                    json.dumps(
                        dict(
                            event="model_error",
                            attempt=len(attempts),
                            error_type=record["error_type"],
                        )
                    ),
                    flush=True,
                )
                raise
            finally:
                record["seconds"] = round(time.monotonic() - started, 3)
            record.update(
                confirmed=True,
                content=response.content,
                tools=[dict(name=c.name, arguments=c.arguments) for c in response.tool_calls],
            )
            print(
                json.dumps(
                    dict(
                        event="model_step",
                        attempt=len(attempts),
                        scripted_fault=record["scripted_fault"],
                        tools=[c.name for c in response.tool_calls],
                    )
                ),
                flush=True,
            )
            return response

    runtime.client_factory = lambda task: Trace(original(task))

    def submit(name, message, *, files=False, source=None):
        bot = store.save_bot(
            settings.owner,
            BotInput(
                name=name,
                purpose="Help with website research and everyday work.",
                memory_enabled=False,
                files_enabled=files,
            ),
        )
        chat = store.create_chat(settings.owner, bot["id"], "Conversation")
        if source:
            store.save_file(settings.owner, bot["id"], source)
        task = store.enqueue(
            settings.owner, chat["id"], TaskInput(message=message, request_id=uuid4().hex)
        )
        runtime.wake()
        return bot, task

    def wait(bot, task, timeout=300, *, offline=False, mixed=False, stop_after_write=False):
        deadline = time.monotonic() + timeout
        accepted = []
        while time.monotonic() < deadline:
            current = store.task(settings.owner, task["id"])
            if (
                stop_after_write
                and site.write_received.is_set()
                and current["status"] in {"running", "waiting"}
            ):
                runtime.cancel(task["id"])
            if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                proposals = [
                    p
                    for p in runtime.proposals.list(bot["id"])
                    if p["source_task_id"] == task["id"] and p["status"] == "pending"
                ]
                if proposals:
                    assert len(proposals) == 1
                    p = proposals[0]
                    assert (
                        (
                            p["browser_origin"] in (None, site.url)
                            and (p["offline_computer"] or p["browser_origin"])
                        )
                        if mixed
                        else p["offline_computer"] and not p["browser_origin"]
                        if offline
                        else p["browser_origin"] == site.url and not p["offline_computer"]
                    )
                    # Synthetic user approval through the real proposal path.
                    approved = runtime.proposals.decide(
                        bot["id"],
                        p["id"],
                        ActionDecision(
                            action="accept",
                            expected_revision=p["revision"],
                            browser_origin=p["browser_origin"],
                            offline_computer=bool(p["offline_computer"]),
                        ),
                    )
                    accepted.append(p["id"])
                    task = store.task(settings.owner, approved["task_id"])
                    runtime.wake()
                else:
                    return dict(task=current, approvals=accepted)
            time.sleep(0.1)
        runtime.cancel(task["id"])
        raise TimeoutError("Experiment task timed out")

    def reaped():
        for _ in range(60):
            live = [
                p.pid
                for p in processes.values()
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            ]
            if not live and not adapter._computers and (not desktop or not desktop._computers):
                return True
            time.sleep(0.1)
        return False

    samples = []
    sample_stop = threading.Event()

    def sample_resources():
        while not sample_stop.is_set():
            pss = 0
            children = psutil.Process().children(recursive=True)
            for process in children:
                try:
                    pss += process.memory_full_info().pss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            samples.append(
                dict(seconds=round(time.monotonic(), 2), processes=len(children), pss_bytes=pss)
            )
            sample_stop.wait(1)

    sampler = threading.Thread(target=sample_resources, daemon=True)
    if {"housekeeping", "helpers"}.intersection(cases):
        sampler.start()

    try:
        runtime.start()
        if "rich_editor" in cases:
            marker = "Νεφέλη / 東京 / " + uuid4().hex[:8]
            paragraph = "Keep the source unchanged — café."
            bot, task = submit(
                "Document editor",
                f'Open {site.url}/quill. Replace the document with these two plain-text paragraphs, in order: "{marker}" and "{paragraph}". Then ask me for the final sign-off sentence. Wait for my answer before saving the document.',
            )
            result = wait(bot, task, timeout=600)
            questions = runtime.clarifications.list(bot["id"])
            assert len(questions) == 1 and questions[0]["status"] == "pending"
            q = questions[0]
            from alysis_code.bots.clarifications import ReadBrowserDraft

            saved = runtime.clarifications.read_browser_draft(
                result["task"], ReadBrowserDraft(question_id=q["id"])
            )
            assert marker in saved["content"] and paragraph in saved["content"]
            assert not site.saved and reaped()
            runtime.stop()
            adapter = ObservedBrowser(settings.owner, ComputerViews(), factory)
            app = create_app(settings, browser_computers=adapter)
            runtime, store = app.state.runtime, app.state.store
            adapter.store = store
            original = runtime.client_factory
            runtime.client_factory = lambda task, client_factory=original: Trace(
                client_factory(task)
            )
            signoff = "Approved for the synthetic study — " + uuid4().hex[:8]
            followup = store.enqueue(
                settings.owner,
                task["chat_id"],
                TaskInput(
                    message=f'Add "{signoff}" as the third paragraph. Keep the first two paragraphs from my draft, then save the document once.',
                    request_id=uuid4().hex,
                ),
            )
            runtime.start()
            resumed = wait(bot, followup, timeout=600)
            expected = {"ops": [{"insert": marker + "\n" + paragraph + "\n" + signoff + "\n"}]}
            result.update(
                case="rich_editor",
                question=q,
                checkpoint=saved,
                resumed=resumed,
                computer_stopped_while_waiting=True,
                runtime_restarted=True,
                submissions=site.saved,
                correct=resumed["task"]["status"] == "completed"
                and len(site.saved) == 1
                and site.saved[0]["delta"] == expected
                and any(
                    t["name"] == "read_browser_draft" for a in attempts for t in a.get("tools", [])
                ),
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case="rich_editor", correct=result["correct"])),
                flush=True,
            )
        for case in ("resume_draft", "resume_chat"):
            if case not in cases:
                continue
            marker = "Νεφέλη / 東京 / " + uuid4().hex[:8]
            bot, task = submit(
                "Workshop registration",
                f'At {site.url}/draft, enter my complete Name value "{marker}". Keep the existing draft note unchanged. Ask me what Rate to use, and wait for my answer before saving the registration.',
            )
            result = wait(bot, task, timeout=600)
            questions = runtime.clarifications.list(bot["id"])
            assert len(questions) == 1 and questions[0]["status"] == "pending"
            q = questions[0]
            state = runtime.clarifications.context(result["task"])
            assert state[0].get("browser_draft"), "Missing actual browser draft"
            from alysis_code.bots.clarifications import QuestionDecision, ReadBrowserDraft

            saved = runtime.clarifications.read_browser_draft(
                result["task"], ReadBrowserDraft(question_id=q["id"])
            )
            assert marker in saved["content"] and site.draft_note in saved["content"]
            assert not site.saved and reaped()
            unrelated = store.enqueue(
                settings.owner,
                task["chat_id"],
                TaskInput(message="What is 17 plus 25?", request_id=uuid4().hex),
            )
            runtime.wake()
            small = wait(bot, unrelated)
            assert (
                small["task"]["status"] == "completed"
                and runtime.clarifications.list(bot["id"])[0]["status"] == "pending"
            )
            # Complete service restart: only the database survives.
            runtime.stop()
            adapter = ObservedBrowser(settings.owner, ComputerViews(), factory)
            app = create_app(settings, browser_computers=adapter)
            runtime, store = app.state.runtime, app.state.store
            adapter.store = store
            original = runtime.client_factory
            runtime.client_factory = lambda task, client_factory=original: Trace(
                client_factory(task)
            )
            answer_text = "Use 46.25. Keep my original name and the exact draft note from before, and save the registration once."
            if case == "resume_chat":
                followup = store.enqueue(
                    settings.owner,
                    task["chat_id"],
                    TaskInput(message=answer_text, request_id=uuid4().hex),
                )
            else:
                answered = runtime.clarifications.decide(
                    bot["id"],
                    q["id"],
                    QuestionDecision(
                        action="answer", expected_revision=q["revision"], answer=answer_text
                    ),
                )
                followup = store.task(settings.owner, answered["answer_task_id"])
            runtime.start()
            resumed = wait(bot, followup, timeout=600)
            result.update(
                case=case,
                question=q,
                checkpoint=saved,
                unrelated=small,
                resumed=resumed,
                computer_stopped_while_waiting=True,
                runtime_restarted=True,
                submissions=site.saved,
                page_defaults=site.draft_values,
                correct=runtime.clarifications.list(bot["id"])[0]["status"] == "answered"
                and resumed["task"]["status"] == "completed"
                and site.saved == [dict(name=marker, note=site.draft_note, rate="46.25")]
                and site.registration_code in resumed["task"]["output"]
                and any(
                    t["name"] == "read_browser_draft" for a in attempts for t in a.get("tools", [])
                ),
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case=case, correct=result["correct"])),
                flush=True,
            )
        if "download" in cases:
            bot, task = submit(
                "Download for later",
                f"Please download the actual sales.csv from {site.url}/package into my files and tell me when it is saved.",
                files=True,
            )
            result = wait(bot, task)
            files = store.list_files(settings.owner, bot["id"])
            file = next((f for f in files if f["name"] == "sales.csv"), None)
            result.update(
                case="download",
                correct=result["task"]["status"] == "completed"
                and file is not None
                and b"".join(store.file_chunks(settings.owner, bot["id"], file["id"]))
                == site.ledger.encode(),
                processes_reaped=reaped(),
                actual_csv=site.ledger,
                navigation_download_receipts=sum(
                    bool(a.get("result", {}).get("download_required")) for a in actions
                ),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case="download", correct=result["correct"])),
                flush=True,
            )
        if "helpers" in cases:
            marker = "Ίριδα-東京-" + uuid4().hex[:10]
            bot, task = submit(
                "Local website package",
                f"Build a tiny static website with an index.html titled '{marker}' and a short welcome paragraph. Start a temporary local preview server bound only to 127.0.0.1 on your private computer and verify that an actual HTTP request serves that page correctly. Then return site.zip containing index.html. Keep the source for later edits and verify the ZIP.",
                files=True,
            )
            result = wait(bot, task, offline=True)
            files = store.list_files(settings.owner, bot["id"])
            target = next((f for f in files if f["name"] == "site.zip"), None)
            valid = False
            if target:
                blob = b"".join(store.file_chunks(settings.owner, bot["id"], target["id"]))
                with zipfile.ZipFile(io.BytesIO(blob)) as zipped:
                    valid = (
                        zipped.testzip() is None and marker in zipped.read("index.html").decode()
                    )
            stopped = [
                a
                for a in desktop_actions
                if a["operation"] == "stop_process" and a.get("result", {}).get("stopped")
            ]
            result.update(
                case="helpers",
                correct=result["task"]["status"] == "completed" and valid and bool(stopped),
                archive_verified=valid,
                selective_stops=len(stopped),
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case="helpers", correct=result["correct"])),
                flush=True,
            )

        if "housekeeping" in cases:
            # Same bot, actual downloads, successive computers and immutable exports.
            bot, task = submit(
                "Monthly work package",
                f"Download the actual sales.csv from {site.url}/package into my files. Calculate its exact amount total on your private computer, then return report.txt and bundle.zip containing the original CSV and that report. Include the total to two decimals in the report. Preserve the source and verify the ZIP contents. Keep the working files for a later revision.",
                files=True,
            )
            result = wait(bot, task, mixed=True)
            assert result["task"]["status"] == "completed", result["task"]["status"]
            files = store.list_files(settings.owner, bot["id"])

            def payload(name):
                file = next(
                    f for f in store.list_files(settings.owner, bot["id"]) if f["name"] == name
                )
                return b"".join(store.file_chunks(settings.owner, bot["id"], file["id"]))

            total = sum(
                (Decimal(r["amount"]) for r in csv.DictReader(io.StringIO(site.ledger))), Decimal(0)
            )
            report, archive = payload("report.txt"), payload("bundle.zip")
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                archive_ok = (
                    sorted(zipped.namelist()) == ["report.txt", "sales.csv"]
                    and zipped.read("sales.csv") == site.ledger.encode()
                    and zipped.read("report.txt") == report
                )
            stages = [
                dict(
                    name="download_and_package",
                    completed=result["task"]["status"] == "completed",
                    reaped=reaped(),
                )
            ]
            snapshots = [
                dict(
                    stage=stages[-1]["name"],
                    temporary_directories=sorted(p.name for p in Path("/tmp").glob("alysis-*")),
                    file_count=len(files),
                )
            ]
            revision = "Αναθεώρηση-東京-" + uuid4().hex[:10]
            next_task = store.enqueue(
                settings.owner,
                task["chat_id"],
                TaskInput(
                    message=f"Use the existing report.txt in your saved workspace, without downloading or importing anything again. Make a copy named revised-report.txt with the exact note '{revision}' appended. Return that copy; keep the original report, source and ZIP unchanged. Verify the result.",
                    request_id=uuid4().hex,
                ),
            )
            runtime.wake()
            recovered = wait(bot, next_task, offline=True)
            assert recovered["task"]["status"] == "completed", recovered["task"]["status"]
            revised = payload("revised-report.txt")
            stages.append(
                dict(
                    name="workspace_revision",
                    completed=recovered["task"]["status"] == "completed",
                    reaped=reaped(),
                )
            )
            snapshots.append(
                dict(
                    stage=stages[-1]["name"],
                    temporary_directories=sorted(p.name for p in Path("/tmp").glob("alysis-*")),
                    file_count=len(store.list_files(settings.owner, bot["id"])),
                )
            )
            marker = "Νεφέλη / 東京 / " + uuid4().hex[:8]
            action_start = len(actions)
            next_task = store.enqueue(
                settings.owner,
                task["chat_id"],
                TaskInput(
                    message=f'At {site.url}/registration, enter the complete Name field value "{marker}" first. Then open Current workshop rate in another tab, return to my unfinished form, enter that rate and save once. Tell me the confirmation code.',
                    request_id=uuid4().hex,
                ),
            )
            runtime.wake()
            form = wait(bot, next_task)
            stages.append(
                dict(
                    name="unfinished_form",
                    completed=form["task"]["status"] == "completed",
                    reaped=reaped(),
                )
            )
            snapshots.append(
                dict(
                    stage=stages[-1]["name"],
                    temporary_directories=sorted(p.name for p in Path("/tmp").glob("alysis-*")),
                    file_count=len(store.list_files(settings.owner, bot["id"])),
                )
            )
            workspace_sources = list(desktop.workspaces.root.glob("*/files/sales.csv"))
            workspace_source_preserved = (
                len(workspace_sources) == 1
                and workspace_sources[0].read_bytes() == site.ledger.encode()
            )
            preserved = (
                workspace_source_preserved
                and payload("sales.csv") == site.ledger.encode()
                and payload("report.txt") == report
                and payload("bundle.zip") == archive
            )
            result.update(
                case="housekeeping",
                stages=stages,
                recovered=recovered,
                form=form,
                resource_snapshots=snapshots,
                exact_total=f"{total:.2f}",
                archive_verified=archive_ok,
                original_files_preserved=preserved,
                workspace_source_preserved=workspace_source_preserved,
                library_bytes=sum(
                    f["size_bytes"] for f in store.list_files(settings.owner, bot["id"])
                ),
                workspace_bytes=sum(
                    p.stat().st_size
                    for p in desktop.workspaces.root.glob("*/files/**/*")
                    if not p.is_symlink() and p.is_file()
                ),
                revision_verified=report in revised and revision.encode() in revised,
                proactive_tab_closes=sum(
                    a["tool"] == "browser_close_page" and a.get("result", {}).get("closed", False)
                    for a in actions[action_start:]
                ),
                file_hashes={
                    f["name"]: hashlib.sha256(payload(f["name"])).hexdigest()
                    for f in store.list_files(settings.owner, bot["id"])
                },
                correct=all(s["completed"] and s["reaped"] for s in stages)
                and archive_ok
                and preserved
                and f"{total:.2f}".encode() in report
                and report in revised
                and revision.encode() in revised
                and site.saved == [dict(name=marker, rate="34.75")]
                and site.registration_code in form["task"]["output"]
                and all(not s["temporary_directories"] for s in snapshots),
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case="housekeeping", correct=result["correct"])),
                flush=True,
            )
        if "correction" in cases:
            rows = [
                (client, Decimal(secrets.randbelow(999) + 1) / 100)
                for client in ("Αθήνα", "東京", "Zürich")
                for _ in range(3)
            ]
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(["client", "amount"])
            writer.writerows(rows)
            source = FileInput(name="ledger.csv", content=stream.getvalue())
            bot, task = submit(
                "Ledger report",
                "Create totals.xlsx from ledger.csv in my files, with one row per client, columns client and total, sorted by client. Calculate exact totals from the data and format amounts to two decimal places. Keep the source unchanged and verify the saved workbook.",
                files=True,
                source=source,
            )
            correction = {}

            def correct_while_busy():
                try:
                    assert command_received.wait(120), "No real command began"
                    active = runtime.background.active(bot["id"])
                    assert len(active) == 1, "Computer work did not free the conversation"
                    correction["active_task_id"] = active[0]["task_id"]
                    sent = time.monotonic()
                    update = store.enqueue(
                        settings.owner,
                        task["chat_id"],
                        TaskInput(
                            message="Actually exclude Zürich from the report, and call the workbook revised-totals.xlsx. Please apply this change to the report you are working on; leave the original ledger unchanged.",
                            request_id=uuid4().hex,
                        ),
                    )
                    runtime.wake()
                    correction["reply"] = wait(bot, update, timeout=50)
                    correction["chat_seconds"] = time.monotonic() - sent
                    correction["answered_while_work_waited"] = (
                        not command_release.is_set()
                        and store.task(settings.owner, active[0]["task_id"])["status"] == "waiting"
                    )
                except Exception as exc:
                    correction["error"] = str(exc)
                finally:
                    command_release.set()

            thread = threading.Thread(target=correct_while_busy, daemon=True)
            thread.start()
            result = wait(bot, task, offline=True)
            thread.join(timeout=65)
            files = store.list_files(settings.owner, bot["id"])
            target = next((f for f in files if f["name"] == "revised-totals.xlsx"), None)
            actual = []
            if target:
                from openpyxl import load_workbook

                payload = b"".join(store.file_chunks(settings.owner, bot["id"], target["id"]))
                workbook = load_workbook(io.BytesIO(payload), data_only=True)
                actual = list(workbook.active.values)
            totals = {}
            for client, amount in rows:
                if client != "Zürich":
                    totals[client] = totals.get(client, Decimal(0)) + amount
            expected = [(client, float(total)) for client, total in sorted(totals.items())]
            original_file = next(f for f in files if f["name"] == "ledger.csv")
            result.update(
                case="correction",
                correction=correction,
                workbook_rows=actual,
                expected_rows=expected,
                file_names=[f["name"] for f in files],
                correct=result["task"]["status"] == "completed"
                and not correction.get("error")
                and correction.get("answered_while_work_waited", False)
                and correction["reply"]["task"]["status"] == "completed"
                and actual == [("client", "total"), *expected]
                and len(files) == 2
                and store.file(settings.owner, bot["id"], original_file["id"])["content"]
                == source.content,
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case="correction", correct=result["correct"])),
                flush=True,
            )

        for case in ("stop_write", "stop_write_named"):
            if case not in cases:
                continue
            marker = "Αριάδνη / 東京 / " + uuid4().hex[:8]
            site.saved.clear()
            site.write_received.clear()
            site.write_release.clear()
            bot, task = submit(
                "Interrupted registration",
                (
                    f'At {site.url}/registration-delayed, enter the full name "{marker}" in the Name field, with the rate 34.75. Save it once and tell me the confirmation code.'
                    if case == "stop_write_named"
                    else f"At {site.url}/registration-delayed, register {marker} with the rate 34.75. Save it once and tell me the confirmation code."
                ),
            )
            result = wait(bot, task, stop_after_write=True)
            stopped_calls = len(attempts)
            site.write_release.set()
            time.sleep(3)
            stopped_cleanly = reaped()
            no_calls_after_stop = len(attempts) == stopped_calls
            followup = store.enqueue(
                settings.owner,
                task["chat_id"],
                TaskInput(
                    message=f"Did my registration go through? Check its status at {site.url}/registration-status before doing anything else. Do not submit it again.",
                    request_id=uuid4().hex,
                ),
            )
            runtime.wake()
            recovered = wait(bot, followup)
            result.update(
                case=case,
                followup=recovered,
                registrations=site.saved,
                no_calls_after_stop=no_calls_after_stop,
                correct=result["task"]["status"] == "cancelled"
                and site.saved == [dict(name=marker, rate="34.75")]
                and no_calls_after_stop
                and stopped_cleanly
                and recovered["task"]["status"] == "completed"
                and site.registration_code in recovered["task"]["output"],
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case=case, correct=result["correct"])),
                flush=True,
            )
        if "quotes" in cases:
            bot, task = submit(
                "Venue comparison",
                f"Check the two venue quotes linked from {site.url}/quotes and recommend the cheaper option for 12 people. Give the exact total for each venue and the quote code shown on its page. Use the links to get their current quotes.",
            )
            result = wait(bot, task)
            text = result["task"]["output"]
            result.update(
                case="quotes",
                correct=result["task"]["status"] == "completed"
                and all(
                    q["code"] in text and str(Decimal(q["rate"]) * 12) in text for q in site.quotes
                ),
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(
                    dict(
                        event="case_done",
                        case="quotes",
                        correct=result["correct"],
                        processes_reaped=result["processes_reaped"],
                    )
                ),
                flush=True,
            )

        for case in ("draft", "cleanup", "initiative"):
            if case not in cases:
                continue
            action_start = len(actions)
            marker = "Νεφέλη / 東京 / " + uuid4().hex[:8]
            site.saved.clear()
            extra = (
                " Close the reference tab once you have read it, keeping my unfinished form intact."
                if case == "cleanup"
                else ""
            )
            bot, task = submit(
                "Registration",
                f"At {site.url}/registration, enter the name {marker} first. Then check the Current workshop rate link.{extra} Return to the form with the name intact, enter that rate and save the registration once. Tell me its confirmation code.",
            )
            result = wait(bot, task)
            result.update(
                case=case,
                correct=result["task"]["status"] == "completed"
                and site.saved == [dict(name=marker, rate="34.75")]
                and site.registration_code in result["task"]["output"]
                and (
                    case not in {"cleanup", "initiative"}
                    or any(
                        a["tool"] == "browser_close_page"
                        and a.get("result", {}).get("closed")
                        and (case != "cleanup" or a["saved_registrations"] == 0)
                        for a in actions[action_start:]
                    )
                ),
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(
                    dict(
                        event="case_done",
                        case=case,
                        correct=result["correct"],
                        processes_reaped=result["processes_reaped"],
                    )
                ),
                flush=True,
            )

        if "events" in cases:
            bot, task = submit(
                "Project file watcher",
                "When I add a text file here, read it and tell me its project name and deadline, and point out missing information. Report in this chat; don't change files or contact anyone.",
                files=True,
            )
            setup_result = wait(bot, task)
            rules = runtime.triggers.list(bot["id"])
            assert len(rules) == 1 and rules[0]["state"] == "active", (
                "Ordinary request did not enable a single file review"
            )
            marker = "Έργο-東京-" + uuid4().hex[:10]
            uploaded = store.save_file(
                settings.owner,
                bot["id"],
                FileInput(
                    name="project.txt",
                    content=f"Project: {marker}\nDeadline: 2030-06-12\nOwner: not assigned yet.\n",
                ),
            )
            deadline = time.monotonic() + 90
            event_task = None
            while time.monotonic() < deadline:
                deliveries = runtime.triggers.list(bot["id"])[0]["recent_deliveries"]
                if deliveries and deliveries[0]["task_id"]:
                    event_task = store.task(settings.owner, deliveries[0]["task_id"])
                    break
                time.sleep(0.1)
            assert event_task, "Upload did not wake the bot without another chat message"
            result = wait(bot, event_task)
            pause_task = store.enqueue(
                settings.owner,
                task["chat_id"],
                TaskInput(message="Stop watching new files now.", request_id=uuid4().hex),
            )
            runtime.wake()
            pause_result = wait(bot, pause_task)
            paused = runtime.triggers.list(bot["id"])
            calls_after_pause = len(attempts)
            store.save_file(
                settings.owner,
                bot["id"],
                FileInput(name="after-pause.txt", content="No new review should run."),
            )
            time.sleep(7)  # Beyond the real file-event collection window.
            deliveries = runtime.triggers.list(bot["id"])[0]["recent_deliveries"]
            result.update(
                case="events",
                correct=result["task"]["status"] == "completed"
                and marker in result["task"]["output"]
                and "2030-06-12" in result["task"]["output"]
                and paused[0]["state"] == "paused"
                and len(deliveries) == 1
                and calls_after_pause == len(attempts)
                and len(store.list_files(settings.owner, bot["id"])) == 2,
                setup=setup_result,
                pause=pause_result,
                deliveries=deliveries,
                uploaded_file_id=uploaded["id"],
                no_calls_after_pause=calls_after_pause == len(attempts),
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case="events", correct=result["correct"])),
                flush=True,
            )

        if "retraction" in cases:
            seeded_fault_steps.extend(
                [
                    LLMResponse(
                        "",
                        [
                            ToolCall(
                                "seed-save", "save_file", {"name": "hello.txt", "content": "Hello"}
                            ),
                            ToolCall(
                                "seed-proposal",
                                "propose_action",
                                {
                                    "question": "May I save hello.txt?",
                                    "instructions": "Save hello.txt containing Hello.",
                                    "offline_computer": False,
                                },
                            ),
                        ],
                        {},
                    ),
                    LLMResponse("Saved hello.txt containing Hello.", [], {}),
                ]
            )
            bot, task = submit(
                "Proposal recovery",
                "Save hello.txt containing Hello and tell me when it is done.",
                files=True,
            )
            result = wait(bot, task)
            proposal = runtime.proposals.for_task(result["task"])
            files = store.list_files(settings.owner, bot["id"])
            result.update(
                case="retraction",
                correct=result["task"]["status"] == "completed"
                and proposal["status"] == "outdated"
                and len(files) == 1
                and files[0]["name"] == "hello.txt"
                and store.file(settings.owner, bot["id"], files[0]["id"])["content"] == "Hello"
                and any(
                    c["name"] == "withdraw_action"
                    for a in attempts
                    if not a["scripted_fault"]
                    for c in a.get("tools", [])
                ),
                seeded_fault_steps=2,
                proposal=proposal,
                processes_reaped=reaped(),
            )
            results.append(result)
            print(
                json.dumps(dict(event="case_done", case="retraction", correct=result["correct"])),
                flush=True,
            )

        if "stop" in cases:
            bot, task = submit(
                "Interrupted research", f"Open {site.url}/slow and tell me what the report says."
            )
            assert site.slow_entered.wait(90), "Model did not begin the delayed page request"
            runtime.cancel(task["id"])
            result = wait(bot, task, timeout=30)
            result.update(
                case="stop",
                correct=result["task"]["status"] == "cancelled",
                processes_reaped=reaped(),
            )
            results.append(result)
    finally:
        command_release.set()
        site.write_release.set()
        site.slow_release.set()
        runtime.stop()
        sample_stop.set()
        if sampler.is_alive():
            sampler.join(timeout=3)
        site.close()
        # Only synthetic messages/page data. Never serialize bootstrap/settings/client.
        evidence = dict(
            event="evidence",
            model=config["model"],
            output_allowance=settings.output_tokens,
            injected_command_latency="correction" in cases,
            results=results,
            actions=actions,
            desktop_actions=desktop_actions,
            resource_samples=samples,
            attempts=attempts,
            usage=store.usage(settings.owner),
            processes_reaped=reaped(),
        )
        print(json.dumps(evidence, ensure_ascii=False), flush=True)


def host(cases):
    from alysis_code.config import load_config, resolve_profile_api_key
    from alysis_code.profiles import get_profile

    root = Path(__file__).resolve().parents[2]
    cfg = load_config()
    profile = get_profile(cfg, "opencode")
    if (
        not profile
        or profile.base_url != "https://opencode.ai/zen/go/v1"
        or profile.protocol != "openai_compat"
    ):
        raise RuntimeError("This study requires the verified OpenCode Go profile; no fallback")
    key = resolve_profile_api_key(cfg, profile.name).key
    if not key:
        raise RuntimeError("Saved OpenCode key unavailable")
    name = "alysis-tab-model-" + uuid4().hex[:8]
    folder = root / ".dev" / name
    folder.mkdir(parents=True)
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "-i",
        "--name",
        name,
        "--read-only",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--security-opt",
        "seccomp=" + str(root / "scripts/cloud-dev/seccomp.json"),
        "--security-opt",
        "systempaths=unconfined",
        "--cpus",
        "1",
        "--memory",
        "2g",
        "--pids-limit",
        "512",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
        "-v",
        str(root) + ":/study:ro",
        "-e",
        "PYTHONPATH=/study/src",
        "alysis-bots-memory-storage-20261021:qa",
        "python",
        "/study/scripts/qa/" + Path(__file__).name,
        "--linux",
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    deadline = threading.Timer(
        1200 if {"resume_draft", "resume_chat", "rich_editor"}.intersection(cases) else 720,
        lambda: subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True),
    )
    deadline.start()
    evidence = None
    try:
        process.stdin.write(
            json.dumps(
                dict(key=key, base_url=profile.base_url, model=profile.default_model, cases=cases)
            )
            + "\n"
        )
        process.stdin.close()
        with (folder / "result.log").open("w") as log:
            for line in process.stdout:
                safe = line.replace(key, "[redacted]")
                log.write(safe)
                log.flush()
                if '"event": "evidence"' not in safe:
                    print(safe, end="", flush=True)
                else:
                    data = evidence = json.loads(safe)
                    (folder / "evidence.json").write_text(
                        json.dumps(data, ensure_ascii=False, indent=2)
                    )
                    print(
                        json.dumps(
                            dict(
                                results=[
                                    dict(
                                        case=r["case"],
                                        correct=r["correct"],
                                        status=r["task"]["status"],
                                        processes_reaped=r["processes_reaped"],
                                    )
                                    for r in data["results"]
                                ],
                                usage=data["usage"],
                                attempts=len(data["attempts"]),
                                confirmed=sum(
                                    a["confirmed"] and not a.get("scripted_fault", False)
                                    for a in data["attempts"]
                                ),
                                scripted_fault_steps=sum(
                                    a.get("scripted_fault", False) for a in data["attempts"]
                                ),
                                tab_closes=sum(
                                    a["tool"] == "browser_close_page" for a in data["actions"]
                                ),
                                evidence=str(folder / "evidence.json"),
                            )
                        ),
                        flush=True,
                    )
        if process.wait() != 0:
            raise RuntimeError("Study failed; sanitized evidence retained at " + str(folder))
        if (
            evidence is None
            or len(evidence["results"]) != len(cases)
            or not evidence["processes_reaped"]
            or not all(r["correct"] and r["processes_reaped"] for r in evidence["results"])
        ):
            raise RuntimeError(
                "Qualification failed; sanitized evidence retained at " + str(folder)
            )
    finally:
        deadline.cancel()
        if process.poll() is None:
            subprocess.run(["docker", "stop", "--time", "10", name], capture_output=True)
            process.wait(timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--linux", action="store_true")
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=(
            "quotes",
            "draft",
            "cleanup",
            "initiative",
            "events",
            "retraction",
            "stop",
            "stop_write",
            "stop_write_named",
            "correction",
            "housekeeping",
            "helpers",
            "resume_draft",
            "resume_chat",
            "download",
            "rich_editor",
        ),
        default=["quotes", "draft", "cleanup", "stop"],
    )
    args = parser.parse_args()
    if args.linux:
        linux()
    elif args.execute_live:
        host(args.cases)
    else:
        parser.error("Choose --execute-live or --linux")
