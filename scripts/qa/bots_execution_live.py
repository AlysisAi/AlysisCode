"""Opt-in ordinary requests: real Coding Plan, Alysis Code, Linux and returned files.

No model fixtures, background keywords or internal tool names in user prompts.
Explicit bounded call allowance, isolated loopback-only lab, credentials through stdin.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import uuid4


def linux():
    import threading

    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.models import BotInput, FileInput

    config = json.loads(sys.stdin.readline())
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        config["model"],
        max_calls=config["max_calls"],
        output_tokens=4000,
        desktop_enabled=True,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    app = create_app(settings)
    # Bound the whole study through settings.max_calls and the host watchdog.
    # Use production task limits so legitimate verification/repair is not cut
    # off by a separate, artificial per-turn ceiling.
    bot = app.state.store.save_bot(
        settings.owner, BotInput(name="Computer view test", purpose="Help with documents and data.")
    )
    app.state.store.save_bot(
        settings.owner, BotInput(name="No computer test", purpose="Remain idle.")
    )
    app.state.store.save_file(
        settings.owner,
        bot["id"],
        FileInput(
            name="sales.csv", content="city,amount\nAthens,17.25\nTokyo,42.50\nZurich,8.75\n"
        ),
    )
    if config.get("library_limit_bytes") is not None:
        app.state.store.set_library_limit(settings.owner, config["library_limit_bytes"])
    # Traces contain synthetic prompts/results, never transport headers or keys.
    traces = []
    factory = app.state.runtime.client_factory
    # Optional synthetic timing gate: never fake a model response. Hold its
    # actual completed draft/tool proposal while a normal memory API edit races it.
    reference_gate = {"marker": None, "active": False, "draft": None, "mode": "draft"}
    release_reference = threading.Event()

    if config.get("reference_race"):

        @app.post("/study/reference-gate")
        def arm_reference_gate(data: dict):
            reference_gate.update(
                marker=data["marker"], active=False, draft=None, mode=data.get("mode", "draft")
            )
            release_reference.clear()
            return {"armed": True}

        @app.get("/study/reference-gate")
        def read_reference_gate():
            return reference_gate

        @app.post("/study/reference-release")
        def release_reference_gate():
            release_reference.set()
            return {"released": True}

    class TraceClient:
        def __init__(self, client, task_id):
            self.client, self.task_id = client, task_id

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            # Synthetic study only: keep exactly the tool evidence seen by the
            # model so output plumbing can be separated from interpretation.
            tool_evidence = [
                {k: m[k] for k in ("role", "tool_call_id", "content") if k in m}
                for m in kwargs["messages"]
                if m.get("role") == "tool"
            ]
            try:
                response = self.client.chat(**kwargs)
            except Exception as exc:
                # Only exception classes and HTTP status; never raw transport data.
                errors, cause = [], exc
                for _ in range(4):
                    if cause is None:
                        break
                    errors.append(
                        {
                            "type": type(cause).__name__,
                            "http_status": getattr(
                                getattr(cause, "response", None), "status_code", None
                            ),
                        }
                    )
                    cause = cause.__cause__ or cause.__context__
                traces.append({"task_id": self.task_id, "errors": errors})
                raise
            marker = reference_gate["marker"]
            proposed_actions = [
                {"name": call.name, "arguments": call.arguments} for call in response.tool_calls
            ]
            held_content = (
                json.dumps(proposed_actions, ensure_ascii=False)
                if reference_gate["mode"] == "actions"
                else response.content or ""
            )
            target_response = (
                any(
                    call.name in {"computer_run", "computer_launch"} for call in response.tool_calls
                )
                and app.state.store.task(settings.owner, self.task_id)["status"] == "waiting"
                if reference_gate["mode"] == "actions"
                else not response.tool_calls
            )
            if marker and kwargs.get("tools") and target_response and marker in held_content:
                reference_gate.update(marker=None, active=True, draft=held_content)
                if not release_reference.wait(30):
                    raise RuntimeError("Synthetic reference race gate was not released in time")
                reference_gate["active"] = False
            traces.append(
                {
                    "task_id": self.task_id,
                    "tool_evidence": tool_evidence,
                    "content": response.content,
                    "tools": [
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                }
            )
            return response

    app.state.runtime.client_factory = lambda task_id: TraceClient(factory(task_id), task_id)

    @app.get("/study/receipt")
    def receipt():
        return {"traces": traces, "usage": app.state.store.usage(settings.owner)}

    uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")


def exercise(client, bot, other, private, name, *, scenario="all"):
    evidence = {"kind": "Real GLM + Alysis Code + local Linux; not hosted Azure", "passed": False}
    deadline = time.monotonic() + 720

    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def until(check):
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.15)
        raise RuntimeError("Live study deadline reached.")

    def wait(task_id):
        return until(
            lambda: (
                t
                if (t := get(f"/v1/tasks/{task_id}"))["status"]
                in {"completed", "failed", "cancelled", "interrupted"}
                else None
            )
        )

    chat = client.post(f"/v1/bots/{bot['id']}/chats", json={"title": "Ordinary requests"}).json()

    def send(message):
        response = client.post(
            f"/v1/chats/{chat['id']}/tasks", json={"message": message, "request_id": uuid4().hex}
        )
        response.raise_for_status()
        return response.json()["id"]

    def approve(message, *, allow_direct=False):
        request = wait(send(message))
        evidence.setdefault("requests", []).append(request)
        assert request["status"] == "completed", request.get("error")
        matches = [
            p
            for p in get(f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == request["id"] and p["offline_computer"]
        ]
        if not matches and allow_direct:
            return request["id"]
        # No corrective prompt mentioning tool names or forcing a handoff.
        assert len(matches) == 1, "Normal request did not produce a real permission card."
        proposal = matches[0]
        response = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            json={
                "action": "accept",
                "expected_revision": proposal["revision"],
                "offline_computer": True,
            },
        )
        response.raise_for_status()
        return response.json()["task_id"]

    def started(task_id):
        status = get(f"/v1/tasks/{task_id}")
        assert status["status"] not in {"failed", "interrupted", "cancelled"}, status.get("error")
        return status if any(e["kind"] == "execution_started" for e in status["events"]) else None

    def download(filename):
        file = next(f for f in get(f"/v1/bots/{bot['id']}/files") if f["name"] == filename)
        response = client.get(f"/v1/bots/{bot['id']}/files/{file['id']}/download")
        response.raise_for_status()
        data = base64.b64decode(response.json()["data_base64"], validate=True)
        assert hashlib.sha256(data).hexdigest() == file["sha256"]
        (private / filename).write_bytes(data)
        return data

    def office_checks(docx, pptx, *, title, note=None):
        import io
        import zipfile
        from xml.etree import ElementTree as ET

        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            assert archive.testzip() is None
            document = ET.fromstring(archive.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text = " ".join(document.itertext())
            assert title in text and "68.50" in text
            assert all(city in text for city in ("Athens", "Tokyo", "Zurich"))
            assert document.findall(".//w:tbl", ns), "Word report must contain an editable table"
            if note:
                assert note in text
        with zipfile.ZipFile(io.BytesIO(pptx)) as archive:
            assert archive.testzip() is None
            slides = [
                p
                for p in archive.namelist()
                if p.startswith("ppt/slides/slide")
                and p.endswith(".xml")
                and "/" not in p.removeprefix("ppt/slides/")
            ]
            assert len(slides) == 3
            slide_text = [" ".join(ET.fromstring(archive.read(p)).itertext()) for p in slides]
            joined = " ".join(slide_text)
            assert title in joined and "68.50" in joined
            charts = [
                p
                for p in archive.namelist()
                if p.startswith("ppt/charts/chart") and p.endswith(".xml")
            ]
            assert charts, "The presentation needs an editable chart, not a chart image"
            chart_text = " ".join(
                " ".join(ET.fromstring(archive.read(p)).itertext()) for p in charts
            )
            for value in ("Athens", "Tokyo", "Zurich", "17.25", "42.5", "8.75"):
                assert value in chart_text
            if note:
                assert note in joined
        return {"word_text": text, "slide_text": slide_text, "native_chart": chart_text}

    try:
        print(json.dumps({"ready": True, "evidence": str(private), "container": name}), flush=True)
        if scenario == "library-full-computer":
            import subprocess

            def inspect_png():
                probe = (
                    "from pathlib import Path; import hashlib,json; "
                    "paths=list(Path('/tmp/bots/computer-workspaces').glob('*/files/sales-chart.png')); "
                    "assert len(paths)==1; p=paths[0]; "
                    "print(json.dumps(dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),"
                    "size=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns)))"
                )
                result = subprocess.run(
                    ["docker", "exec", name, "python", "-c", probe],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return json.loads(result.stdout)

            original_files = get(f"/v1/bots/{bot['id']}/files")
            work = approve("Create a bar chart from sales.csv and return it as sales-chart.png.")
            denied = wait(work)
            evidence["computer_export_denied"] = denied
            assert denied["status"] == "completed", denied.get("error")
            assert get(f"/v1/bots/{bot['id']}/files") == original_files
            preserved = inspect_png()
            evidence["workspace_png_before_recovery"] = preserved
            assert preserved["size"] > get("/v1/storage/library")["available_bytes"]
            # Operator-only synthetic allowance change in this disposable DB.
            # No production/user endpoint is introduced for changing entitlements.
            control = (
                "from pathlib import Path\nfrom alysis_code.bots.store import Store\n"
                "s=Store(Path('/tmp/bots/bots.db'))\n"
                "with s.connect() as db:\n"
                "    owners=db.execute('SELECT owner FROM library_allowances').fetchall()\n"
                "assert len(owners)==1\ns.set_library_limit(owners[0][0],2*1024*1024)\n"
            )
            subprocess.run(
                ["docker", "exec", name, "python", "-c", control],
                capture_output=True,
                text=True,
                check=True,
            )
            recovered = wait(
                approve(
                    "Library capacity is available now. Return the existing sales-chart.png "
                    "from your saved workspace without recreating it."
                )
            )
            evidence["computer_export_recovered"] = recovered
            assert recovered["status"] == "completed", recovered.get("error")
            payload = download("sales-chart.png")
            assert payload.startswith(b"\x89PNG\r\n\x1a\n")
            assert hashlib.sha256(payload).hexdigest() == preserved["sha256"]
            assert inspect_png() == preserved
            evidence["usage_after_recovery"] = get("/v1/storage/library")
            assert evidence["usage_after_recovery"]["reserved_bytes"] == 0
            evidence["passed"] = True
            return
        if scenario == "grounded-drafts":
            before = get(f"/v1/bots/{bot['id']}/files")
            evidence["drafts"] = []
            for prompt in (
                "Draft a short welcome message in Greek for a new volunteer workshop. "
                "Do not invent details or save anything.",
                "Draft a brief thank-you message in French for volunteers after a workshop. "
                "Do not invent details, make new commitments or change saved information.",
                "Write a short English update email from these facts: we finished planning, "
                "the prototype is ready, and testing starts tomorrow. Don't add facts or "
                "promises, and don't send or save it.",
            ):
                reply = wait(send(prompt))
                evidence["drafts"].append(reply)
                assert reply["status"] == "completed", reply.get("error")
            assert get(f"/v1/bots/{bot['id']}/files") == before
            assert not get(f"/v1/bots/{bot['id']}/memories")
            assert not get(f"/v1/bots/{bot['id']}/notes")
            assert not get(f"/v1/bots/{bot['id']}/schedules")
            # This flag establishes completion/no side effects only. Draft
            # faithfulness and prose quality require separate semantic review.
            evidence["passed"] = True
            return
        if scenario == "background-reference-race":
            root = f"/v1/bots/{bot['id']}/memories"
            headings = ["PROJECT-" + uuid4().hex[:12] for _ in range(2)]
            created = client.post(
                root,
                json={
                    "title": "Report heading",
                    "content": f"Use {headings[0]} as report heading.",
                },
            )
            created.raise_for_status()
            memory = created.json()
            client.post(
                "/study/reference-gate", json={"marker": headings[0], "mode": "actions"}
            ).raise_for_status()
            work = approve(
                "Use Python to calculate the total from sales.csv and create a Markdown report "
                "with the approved report heading. Return it as sales-report.md."
            )
            held = until(lambda: gate if (gate := get("/study/reference-gate"))["active"] else None)
            evidence["held_real_action_proposal"] = held["draft"]
            client.put(
                root + "/" + memory["id"],
                json={
                    "title": "Report heading",
                    "content": f"Use {headings[1]} as report heading.",
                },
            ).raise_for_status()
            client.post("/study/reference-release").raise_for_status()
            result = wait(work)
            evidence["background_reference_result"] = result
            assert result["status"] == "completed", result.get("error")
            assert any(e["kind"] == "execution_response_discarded" for e in result["events"])
            text = download("sales-report.md").decode("utf-8")
            assert headings[1] in text and headings[0] not in text and "68.50" in text
            evidence["report_text"] = text
            evidence["headings"] = headings
            evidence["passed"] = True
            return
        if scenario == "reference-race":
            root = f"/v1/bots/{bot['id']}/memories"
            markers = ["DEL-" + uuid4().hex[:12] for _ in range(2)]
            created = client.post(
                root,
                json={
                    "title": "Current delivery marker",
                    "content": f"Our current delivery marker is {markers[0]}.",
                },
            )
            created.raise_for_status()
            memory = created.json()
            evidence["reference_races"] = []
            for index, change in enumerate(("edit", "delete")):
                armed = client.post("/study/reference-gate", json={"marker": markers[index]})
                armed.raise_for_status()
                task_id = send(
                    "What is our current delivery marker? Answer just the marker if one is "
                    "approved; otherwise say it is not recorded. Do not quote old markers "
                    "or change anything."
                )
                held = until(
                    lambda: gate if (gate := get("/study/reference-gate"))["active"] else None
                )
                evidence["reference_races"].append(
                    {"change": change, "held_real_draft": held["draft"]}
                )
                if change == "edit":
                    changed = client.put(
                        root + "/" + memory["id"],
                        json={
                            "title": "Current delivery marker",
                            "content": f"Our current delivery marker is {markers[1]}.",
                        },
                    )
                else:
                    changed = client.delete(root + "/" + memory["id"])
                changed.raise_for_status()
                released = client.post("/study/reference-release")
                released.raise_for_status()
                result = wait(task_id)
                evidence["reference_races"][-1]["result"] = result
                assert result["status"] == "completed", result.get("error")
                assert any(
                    e["kind"] == "memory_refreshed" and e["data"].get("response_discarded")
                    for e in result["events"]
                )
                assert markers[index] not in result["output"]
                if change == "edit":
                    assert markers[1] in result["output"]
                else:
                    assert not get(root)
            evidence["passed"] = True
            return
        if scenario == "notes-search":
            from random import SystemRandom

            budget = SystemRandom().randrange(2400, 7900)
            marker = uuid4().hex[:10]
            root = f"/v1/bots/{bot['id']}/notes"
            source = client.post(
                root,
                json={
                    "title": f"Étude linguistique {marker}",
                    "content": f"Décision confirmée : le plafond consacré au recrutement de traducteurs "
                    f"pour les entretiens est de {budget} EUR. Les entretiens n'ont pas encore eu lieu. "
                    "Aucune date de début ni résultat n'est connu. Ne pas confondre le recrutement "
                    "avec les autres budgets du projet.",
                },
            )
            source.raise_for_status()
            evidence["source_note"] = source.json()
            for index in range(80):
                saved = client.post(
                    root,
                    json={
                        "title": f"Unrelated synthetic project {index}",
                        "content": f"Project {index} concerns cataloguing historical weather records. "
                        f"Its reference is {uuid4().hex}. Next action: check the source archive. "
                        "No research participants or recruitment spending has been agreed for this project.",
                    },
                )
                saved.raise_for_status()
            before = get(root)
            answer = wait(
                send(
                    "Ποιο ανώτατο ποσό είχαμε συμφωνήσει για να βρούμε μεταφραστές για τις "
                    "συνεντεύξεις, έχουν γίνει και πότε ξεκινάνε; Βρες την παλιότερη σχετική "
                    "σημείωση, απάντησε στα ελληνικά και μην αλλάξεις ή αποθηκεύσεις κάτι."
                )
            )
            evidence["cross_language_note_reply"] = answer
            assert answer["status"] == "completed", answer.get("error")
            # Semantic inspection must additionally check unknown date/results.
            digits = "".join(c for c in answer["output"] if c.isdecimal())
            assert str(budget) in digits
            assert get(root) == before
            assert not get(f"/v1/bots/{bot['id']}/memories")
            assert not get(f"/v1/bots/{bot['id']}/schedules")
            evidence["passed"] = True
            return
        if scenario == "library-full":
            contents = "Checkpoint " + uuid4().hex[:24]
            source = get(f"/v1/bots/{bot['id']}/files")[0]
            prompt = f"Save a text file named note.txt containing exactly this text: {contents}"
            before = get("/v1/storage/library")
            assert before["available_bytes"] < len(contents.encode()) < before["limit_bytes"]
            refused = wait(send(prompt))
            evidence["full_library_reply"] = refused
            assert refused["status"] == "completed", refused.get("error")
            assert get(f"/v1/bots/{bot['id']}/files") == [source]
            evidence["usage_after_refusal"] = get("/v1/storage/library")
            assert evidence["usage_after_refusal"] == before
            removed = client.delete(f"/v1/bots/{bot['id']}/files/{source['id']}")
            removed.raise_for_status()
            resumed = wait(send("I freed Library space. " + prompt))
            evidence["space_freed_reply"] = resumed
            assert resumed["status"] == "completed", resumed.get("error")
            assert download("note.txt").decode() == contents
            evidence["usage_after_success"] = get("/v1/storage/library")
            assert evidence["usage_after_success"]["used_bytes"] == len(contents.encode())
            assert evidence["usage_after_success"]["reserved_bytes"] == 0
            evidence["passed"] = True
            return
        if scenario in {"invoice-reconcile", "invoice-correction"}:
            import csv
            import io
            import subprocess
            from decimal import Decimal
            from random import SystemRandom

            rng = SystemRandom()
            marker = uuid4().hex[:8]
            ids = [f"INV-{marker}-{index}" for index in range(6)]
            amounts = [Decimal(rng.randrange(1000, 99000)) / 100 for _ in range(4)]
            records = [
                [ids[0], "Harbor Materials", str(amounts[0]), "EUR", "2026-11-02", "OPEN"],
                [ids[1], "Orchard Services", str(amounts[1]), "EUR", "", "OPEN"],
                [ids[2], "Harbor Materials", str(-amounts[2]), "EUR", "2026-11-04", "CREDIT NOTE"],
                [ids[3], "Cedar Labs", str(amounts[3]), "USD", "2026-11-07", "OPEN"],
                [ids[4], "River Office", "121.30", "EUR", "2026-11-08", "OPEN"],
                [ids[4], "River Office", "128.90", "EUR", "2026-11-08", "OPEN"],
                [ids[5], "Valley Supplies", "45.00", "EUR", "2026-11-10", "CANCELLED"],
            ]
            source_records = [*records, records[0]]
            # Fixture creation only, with the installed ReportLab in the owned
            # study container. The model receives the actual uploaded PDF, not
            # this generator's expected-answer data or a substitute tool reply.
            generator = """import base64,io,json,sys
from reportlab.pdfgen import canvas
output=io.BytesIO(); pdf=canvas.Canvas(output,pagesize=(595,842))
labels=['Invoice number','Vendor','Amount','Currency','Due date','Status']
for row in json.loads(sys.stdin.read()):
    pdf.setFont('Helvetica-Bold',20); pdf.drawString(50,780,'Supplier document')
    pdf.setFont('Helvetica',12)
    for index,(label,value) in enumerate(zip(labels,row)):
        pdf.drawString(50,730-index*32,f'{label}: {value}')
    pdf.showPage()
pdf.save(); print(base64.b64encode(output.getvalue()).decode())
"""
            generated = subprocess.run(
                ["docker", "exec", "-i", name, "python", "-c", generator],
                input=json.dumps(source_records),
                text=True,
                capture_output=True,
                check=True,
            )
            raw = base64.b64decode(generated.stdout.strip(), validate=True)
            (private / "invoices.pdf").write_bytes(raw)
            import pypdfium2

            source_pdf = pypdfium2.PdfDocument(raw)
            assert len(source_pdf) == len(source_records)
            for index, record in enumerate(source_records):
                extracted = source_pdf[index].get_textpage().get_text_range()
                assert all(value in extracted for value in record)
            source_pdf[0].render(scale=1).to_pil().save(private / "invoice-source-review.png")
            uploaded = client.post(
                f"/v1/bots/{bot['id']}/files/binary",
                json={"name": "invoices.pdf", "data_base64": base64.b64encode(raw).decode()},
            )
            uploaded.raise_for_status()
            evidence["invoice_source_records"] = source_records
            evidence["source_file"] = uploaded.json()
            work = approve(
                "Reconcile invoices.pdf into payments.csv (invoice_id,vendor,amount,currency,due_date), "
                "issues.csv (invoice_id,issue) and totals.csv (currency,total). Count exact duplicate "
                "invoices once; exclude conflicting copies and cancelled invoices from payments; "
                "retain credit notes as negative amounts; leave missing due dates empty. "
                "Reconcile totals by currency and return all three files."
            )
            result = wait(work)
            evidence["invoice_result"] = result
            assert result["status"] == "completed", result.get("error")
            tables = {
                filename: list(csv.DictReader(io.StringIO(download(filename).decode("utf-8-sig"))))
                for filename in ("payments.csv", "issues.csv", "totals.csv")
            }
            evidence["invoice_outputs"] = tables
            actual = tables["payments.csv"]
            assert len(actual) == 4
            assert {row["invoice_id"] for row in actual} == set(ids[:4])
            for row in actual:
                expected = records[ids.index(row["invoice_id"])]
                assert row["vendor"] == expected[1] and Decimal(row["amount"]) == Decimal(
                    expected[2]
                )
                assert row["currency"] == expected[3] and row["due_date"] == expected[4]
            # A cancellation is an explicit exclusion, not necessarily an issue.
            # Only the unresolved conflicting copies must appear in this sheet.
            assert ids[4] in {row["invoice_id"] for row in tables["issues.csv"]}
            assert {row["currency"]: Decimal(row["total"]) for row in tables["totals.csv"]} == {
                "EUR": amounts[0] + amounts[1] - amounts[2],
                "USD": amounts[3],
            }
            assert download("invoices.pdf") == raw
            if scenario == "invoice-correction":
                originals = {
                    filename: download(filename)
                    for filename in ("payments.csv", "issues.csv", "totals.csv")
                }
                revised = wait(
                    approve(
                        f"The supplier confirmed that {ids[4]} is valid for 128.90 EUR; "
                        f"the 121.30 copy is wrong. The due date for {ids[1]} is 2026-11-06. "
                        "Apply these corrections and return payments-updated.csv, issues-updated.csv "
                        "and totals-updated.csv with the same columns. Keep the earlier files "
                        "unchanged, preserve the other invoice details and retain the original rules."
                    )
                )
                evidence["invoice_correction_result"] = revised
                assert revised["status"] == "completed", revised.get("error")
                revised_tables = {
                    filename: list(
                        csv.DictReader(io.StringIO(download(filename).decode("utf-8-sig")))
                    )
                    for filename in (
                        "payments-updated.csv",
                        "issues-updated.csv",
                        "totals-updated.csv",
                    )
                }
                evidence["invoice_corrected_outputs"] = revised_tables
                revised_rows = revised_tables["payments-updated.csv"]
                assert len(revised_rows) == 5 and {r["invoice_id"] for r in revised_rows} == set(
                    ids[:5]
                )
                for row in revised_rows:
                    index = ids.index(row["invoice_id"])
                    expected = list(records[index])
                    if index == 1:
                        expected[4] = "2026-11-06"
                    if index == 4:
                        expected[2] = "128.90"
                    assert row["vendor"] == expected[1] and Decimal(row["amount"]) == Decimal(
                        expected[2]
                    )
                    assert row["currency"] == expected[3] and row["due_date"] == expected[4]
                assert {
                    row["currency"]: Decimal(row["total"])
                    for row in revised_tables["totals-updated.csv"]
                } == {
                    "EUR": amounts[0] + amounts[1] - amounts[2] + Decimal("128.90"),
                    "USD": amounts[3],
                }
                assert all(download(filename) == payload for filename, payload in originals.items())
                assert download("invoices.pdf") == raw
            evidence["passed"] = True
            return
        if scenario in {"data-cleanup", "data-cleanup-large"}:
            import csv
            import io
            from decimal import Decimal
            from random import SystemRandom

            rng = SystemRandom()
            amounts = [Decimal(rng.randrange(1000, 9999)) / 100 for _ in range(3)]
            marker = uuid4().hex[:10]
            rows = [
                [marker + "-1", "Αθήνα ταξί", str(amounts[0]), "EUR"],
                [marker + "-2", "東京 materials", str(amounts[1]), "EUR"],
                [marker + "-3", "Zürich refund", str(-amounts[2]), "EUR"],
                [marker + "-4", "Receipt pending", "", "EUR"],
                [marker + "-2", "Different receipt", "12.34", "EUR"],
            ]
            if scenario == "data-cleanup-large":
                for index in range(5, 53):
                    value = Decimal(rng.randrange(1000, 99999)) / 100
                    rows.append(
                        [
                            marker + f"-{index}",
                            f"Αθήνα / 東京 receipt {index}",
                            "" if index % 11 == 0 else str(-value if index % 7 == 0 else value),
                            "EUR",
                        ]
                    )
            duplicates = rows[1:9] if scenario == "data-cleanup-large" else [rows[1]]
            source_rows = [*rows, *duplicates]
            raw = io.StringIO()
            writer = csv.writer(raw)
            header = ["expense_id", "description", "amount", "currency"]
            writer.writerow(header)
            writer.writerows(source_rows)
            uploaded = client.post(
                f"/v1/bots/{bot['id']}/files",
                json={"name": "expenses.csv", "content": raw.getvalue()},
            )
            uploaded.raise_for_status()
            evidence["source_file"] = uploaded.json()
            work = approve(
                "Clean expenses.csv for me and return cleaned.csv, issues.csv and summary.txt. "
                "Remove only exact duplicate rows, preserving the order of the remaining rows "
                "and the original column names and values. Keep different rows that share an "
                "expense_id and flag that conflict in issues.csv. Flag missing amounts too; "
                "do not invent them. In summary.txt, give the total of all numeric amounts "
                "after deduplication, including refunds, with two decimal places and the "
                "stated currency, plus counts of original rows, removed duplicates, remaining "
                "rows and rows with missing amounts. Leave the original file unchanged.",
                allow_direct=True,
            )
            finished = wait(work)
            evidence["cleanup_reply"] = finished
            assert finished["status"] == "completed", finished.get("error")
            cleaned = download("cleaned.csv").decode("utf-8-sig")
            assert list(csv.reader(io.StringIO(cleaned))) == [header, *rows]
            evidence["issues"] = download("issues.csv").decode("utf-8-sig")
            issue_rows = list(csv.reader(io.StringIO(evidence["issues"])))
            assert len(issue_rows) > 1 and len(issue_rows[0]) >= 2
            assert all(len(row) == len(issue_rows[0]) for row in issue_rows)
            evidence["summary"] = download("summary.txt").decode("utf-8-sig")
            expected = sum((Decimal(row[2]) for row in rows if row[2]), Decimal(0))
            assert f"{expected:.2f}" in evidence["summary"] and "EUR" in evidence["summary"]
            assert marker + "-2" in evidence["issues"] and marker + "-4" in evidence["issues"]
            assert download("expenses.csv").decode() == raw.getvalue()
            evidence["expected"] = {
                "total": str(expected),
                "original_rows": len(source_rows),
                "duplicates": len(duplicates),
                "remaining_rows": len(rows),
                "missing_amounts": sum(not row[2] for row in rows),
            }
            evidence["passed"] = True
            return
        if scenario == "schedule-background":
            marker = uuid4().hex[:12]
            work = approve(
                "Run a timing check on your private computer: start a local Python process "
                "which writes its start time to started.txt, waits 90 seconds, then writes "
                "finished.txt with the start time, finish time and elapsed seconds. Wait for "
                "the real process to finish, verify the elapsed time, and return finished.txt."
            )
            until(lambda: started(work))
            requested = wait(
                send(
                    f"In 30 seconds remind me to review checkpoint {marker}. Leave the running "
                    "timing check alone; this is a separate reminder message."
                )
            )
            evidence["background_reminder_request"] = requested
            assert requested["status"] == "completed", requested.get("error")
            schedules = get(f"/v1/bots/{bot['id']}/schedules")
            assert len(schedules) == 1

            def delivered():
                response = client.post(
                    f"/v1/bots/{bot['id']}/schedules/{schedules[0]['id']}/runs", json={}
                )
                response.raise_for_status()
                runs = response.json()["runs"]
                return (
                    runs
                    if runs
                    and runs[0]["current_status"] in {"completed", "failed", "skipped", "missed"}
                    else None
                )

            runs = until(delivered)
            evidence["background_reminder_runs"] = runs
            evidence["work_state_at_reminder"] = get(f"/v1/tasks/{work}")["status"]
            assert len(runs) == 1 and runs[0]["current_status"] == "completed"
            reminder = get(f"/v1/tasks/{runs[0]['task_id']}")
            evidence["background_reminder_reply"] = reminder
            assert marker in reminder["output"]
            assert evidence["work_state_at_reminder"] == "waiting"
            finished = wait(work)
            evidence["timing_work"] = finished
            assert finished["status"] == "completed", finished.get("error")
            evidence["timing_file"] = download("finished.txt").decode()
            evidence["passed"] = True
            return
        if scenario == "reminder-update":
            marker = uuid4().hex[:10]
            note = client.post(
                f"/v1/bots/{bot['id']}/notes",
                json={
                    "title": f"Project {marker} current checkpoint",
                    "content": "Pilot is postponed. No date or outcome is known. Next action: finish the interview guide.",
                },
            )
            note.raise_for_status()
            note = note.json()
            request = wait(
                send(
                    f"In one minute, remind me of project {marker}'s current status and next "
                    "action from its latest project note. Use the latest saved decisions when "
                    "the reminder runs. Do not carry out the project work."
                )
            )
            evidence["reminder_update_request"] = request
            assert request["status"] == "completed", request.get("error")
            schedules = get(f"/v1/bots/{bot['id']}/schedules")
            assert len(schedules) == 1 and schedules[0]["state"] == "active"
            corrected = client.put(
                f"/v1/bots/{bot['id']}/notes/{note['id']}",
                json={
                    "title": note["title"],
                    "expected_revision": note["revision"],
                    "content": "Pilot is cancelled. Next action: archive the interview guide. No replacement date is agreed.",
                },
            )
            corrected.raise_for_status()
            evidence["reminder_note_revision"] = corrected.json()

            def fired():
                runs = client.post(
                    f"/v1/bots/{bot['id']}/schedules/{schedules[0]['id']}/runs", json={}
                )
                runs.raise_for_status()
                values = runs.json()["runs"]
                return (
                    values
                    if values and values[0]["current_status"] in {"completed", "failed"}
                    else None
                )

            runs = until(fired)
            reminder = get(f"/v1/tasks/{runs[0]['task_id']}")
            evidence["reminder_updated_reply"] = reminder
            assert reminder["status"] == "completed", reminder.get("error")
            assert "cancelled" in reminder["output"].casefold()
            assert "archive" in reminder["output"].casefold()
            assert len(runs) == 1
            current = get(f"/v1/bots/{bot['id']}/notes")
            evidence["reminder_notes_after"] = current
            # List responses add archival metadata to the saved-note fields.
            assert len(current) == 1 and all(
                current[0][key] == value for key, value in corrected.json().items()
            ), "Reminder unexpectedly changed the project."
            assert current[0]["consolidated_into"] is None
            assert len(get(f"/v1/bots/{bot['id']}/files")) == 1
            evidence["passed"] = True
            return
        if scenario == "planning":
            marker = uuid4().hex[:8]
            people = 25 + int(marker[:2], 16) % 15
            venue = 350 + int(marker[2:4], 16) % 100
            catering = 6 + int(marker[4:6], 16) % 5
            total = venue + people * catering
            first = wait(
                send(
                    f"Help me plan workshop Aster-{marker} in Athens on 14 November 2026, "
                    f"10:00–13:00, for {people} people. The venue quote is EUR {venue} "
                    f"and catering is EUR {catering} per person, both all-inclusive. "
                    "Give me the exact total and a short checklist of things I still need to arrange. "
                    "Keep track of this project so we can pick it up later. Nothing is booked: "
                    "do not book, send messages, create files or set reminders."
                )
            )
            evidence["planning_initial"] = first
            assert first["status"] == "completed", first.get("error")
            assert str(total) in first["output"]
            notes_before = get(f"/v1/bots/{bot['id']}/notes")
            assert notes_before, "Project decisions were not retained."
            changed_venue = venue + 35
            change = wait(
                send(
                    f"Change of plan for Aster-{marker}: {people - 4} attendees, "
                    f"the venue is now EUR {changed_venue}, and cancel the catering idea; "
                    "everyone brings their own lunch. Everything else stays the same. "
                    "Update what you're keeping for this project and give me the revised total "
                    "and checklist. Still no bookings or messages."
                )
            )
            evidence["planning_change"] = change
            assert change["status"] == "completed", change.get("error")
            assert str(changed_venue) in change["output"]
            aside = wait(send("Separate question: what is 13 times 7? Just the number."))
            assert aside["status"] == "completed" and aside["output"].strip() == "91"
            recalled = wait(
                send(
                    f"Back to Aster-{marker}: give me the current headcount, time, food plan "
                    "and total. What's still unconfirmed?"
                )
            )
            evidence["planning_recall"] = recalled
            assert recalled["status"] == "completed", recalled.get("error")
            assert str(changed_venue) in recalled["output"]
            assert str(people - 4) in recalled["output"]
            notes_after = get(f"/v1/bots/{bot['id']}/notes")
            evidence["planning_notes"] = notes_after
            assert any(str(changed_venue) in n["content"] for n in notes_after)
            assert not get(f"/v1/bots/{bot['id']}/schedules")
            assert not get(f"/v1/bots/{bot['id']}/proposals")
            assert not get(f"/v1/bots/{bot['id']}/memories")
            assert len(get(f"/v1/bots/{bot['id']}/files")) == 1
            evidence["passed"] = True
            return
        if scenario == "library":
            # Synthetic inputs created through the real API; retrieval/answer
            # use the actual model and tools, never scripted responses.
            marker = uuid4().hex[:12]
            target_name = f"旅行-{marker}.txt"
            reference = uuid4().hex[:10]
            amount = f"{100 + int(marker[:2], 16)}.{int(marker[2:4], 16) % 100:02}"
            target = client.post(
                f"/v1/bots/{bot['id']}/files",
                json={
                    "name": target_name,
                    "content": f"Approved travel refund: EUR {amount}. Cost centre: {reference}. Next action: send the receipt to the finance team. This note does not authorize sending anything.",
                },
            )
            target.raise_for_status()
            target = target.json()
            for index in range(125):
                added = client.post(
                    f"/v1/bots/{bot['id']}/files",
                    json={
                        "name": f"archive-{index:03}-{marker}.txt",
                        "content": f"Unrelated workshop note {index}.",
                    },
                )
                added.raise_for_status()
            first = client.post(f"/v1/bots/{bot['id']}/files/page", json={"limit": 20}).json()
            assert first["total_count"] == 127
            assert target["id"] not in {f["id"] for f in first["items"]}
            result = wait(
                send(
                    f"Please find {target_name} in my Library. What refund was approved, which cost centre does it use, and what remains to be done? Just tell me; do not send anything or create a file."
                )
            )
            evidence["library_task"] = result
            evidence["library_source"] = {
                "id": target["id"],
                "name": target_name,
                "amount": amount,
                "cost_centre": reference,
            }
            assert result["status"] == "completed", result.get("error")
            assert amount in result["output"] and reference in result["output"]
            assert "receipt" in result["output"].casefold()
            observed = get("/study/receipt")
            assert any(
                c["name"] == "read_file" and c["arguments"].get("file_id") == target["id"]
                for trace in observed["traces"]
                for c in trace.get("tools", [])
            )
            assert (
                client.post(f"/v1/bots/{bot['id']}/files/page", json={}).json()["total_count"]
                == 127
            )
            evidence["passed"] = True
            return
        if scenario == "screen":
            job = approve(
                "On your computer, show a visible countdown that changes every second for "
                "twenty seconds. When it finishes, save a short completion note as countdown.txt "
                "and give me the file."
            )
            evidence["screen_samples"] = []
            while time.monotonic() < deadline:
                status = get(f"/v1/tasks/{job}")
                events = status["events"]
                pending_runs = sum(
                    e["kind"] == "tool_started" and e["data"].get("name") == "computer_run"
                    for e in events
                ) - sum(
                    e["kind"] == "tool_completed" and e["data"].get("name") == "computer_run"
                    for e in events
                )
                started_at = time.monotonic()
                frame = get(f"/v1/bots/{bot['id']}/computer")
                sample = {
                    "state": frame["state"],
                    "seconds": time.monotonic() - started_at,
                    "pending_computer_runs": pending_runs,
                }
                if frame["state"] == "live":
                    data = base64.b64decode(frame["image"].split(",", 1)[1], validate=True)
                    sample["sha256"] = hashlib.sha256(data).hexdigest()
                    sample["width"], sample["height"] = frame["width"], frame["height"]
                    (private / f"screen-{len(evidence['screen_samples'])}.jpg").write_bytes(data)
                evidence["screen_samples"].append(sample)
                assert get(f"/v1/bots/{other['id']}/computer")["state"] == "not_running"
                if status["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    evidence["screen_job"] = status
                    break
                time.sleep(0.75)
            else:
                raise RuntimeError("Screen study deadline reached.")
            assert evidence["screen_job"]["status"] == "completed"
            frames = [s for s in evidence["screen_samples"] if s["state"] == "live"]
            assert len({s["sha256"] for s in frames}) >= 3
            evidence["completion_note"] = download("countdown.txt").decode()
            assert get(f"/v1/bots/{bot['id']}/computer")["state"] == "not_running"
            evidence["passed"] = True
            return
        if scenario in {"reliability", "reminders"}:
            evidence["ordinary_requests"] = []
            for prompt in (
                (
                    "What is 19 multiplied by 23?",
                    "Would a weekly summary be useful? Explain the pros and cons; don't schedule anything.",
                )
                if scenario == "reliability"
                else ()
            ):
                result = wait(send(prompt))
                evidence["ordinary_requests"].append(result)
                assert result["status"] == "completed", result.get("error")
            assert not get(f"/v1/bots/{bot['id']}/schedules")
            for prompt in (
                (
                    "Create a chart from sales.csv and return it as a PNG.",
                    "Δημιούργησε ένα επεξεργάσιμο αρχείο Word με τα στοιχεία του sales.csv και το σύνολό τους.",
                    "Can you package sales.csv and a short text explanation together as a ZIP?",
                )
                if scenario == "reliability"
                else ()
            ):
                result = wait(send(prompt))
                evidence["ordinary_requests"].append(result)
                assert result["status"] == "completed", result.get("error")
                proposals = [
                    p
                    for p in get(f"/v1/bots/{bot['id']}/proposals")
                    if p["source_task_id"] == result["id"] and p["offline_computer"]
                ]
                assert len(proposals) == 1, "No real permission card."
                proposal = proposals[0]
                dismissed = client.patch(
                    f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
                    json={"action": "dismiss", "expected_revision": proposal["revision"]},
                )
                dismissed.raise_for_status()
            evidence["reminders"] = []
            for prompt in (
                "Remind me in two minutes to review the sales report.",
                "Δημιούργησε ξεχωριστή υπενθύμιση σε δύο λεπτά να ετοιμάσω τη λίστα υλικών. Κράτησε και την προηγούμενη υπενθύμιση.",
            ):
                result = wait(send(prompt))
                evidence["ordinary_requests"].append(result)
                assert result["status"] == "completed", result.get("error")
                schedules = [
                    s
                    for s in get(f"/v1/bots/{bot['id']}/schedules")
                    if s["source_task_id"] == result["id"]
                ]
                assert len(schedules) == 1
                routine = schedules[0]
                evidence["reminders"].append(routine)
                assert routine["state"] == "active" and routine["next_run_at"] is not None
                assert abs(routine["next_run_at"] - result["created_at"] - 120) < 1
            assert len({r["id"] for r in evidence["reminders"]}) == 2
            for routine in evidence["reminders"]:
                path = f"/v1/bots/{bot['id']}/schedules/{routine['id']}/runs"

                def finished_reminder(path=path):
                    response = client.post(path, json={})
                    response.raise_for_status()
                    runs = response.json()["runs"]
                    return (
                        runs
                        if runs and runs[0]["current_status"] in {"completed", "failed"}
                        else None
                    )

                runs = until(finished_reminder)
                assert len(runs) == 1 and runs[0]["current_status"] == "completed", runs
                routine["observed_runs"] = runs
                routine["observed_task"] = get(f"/v1/tasks/{runs[0]['task_id']}")
            assert len({r["observed_task"]["id"] for r in evidence["reminders"]}) == 2
            evidence["passed"] = True
            return
        if scenario == "office":
            job = approve(
                "Use sales.csv to make an editable Word report called sales_brief.docx "
                "and a three-slide PowerPoint called sales_deck.pptx. Title both 'Q3 Sales Review'. "
                "The Word report should have a sales table, the exact total with two decimals "
                "and a short conclusion. The slides should cover the overview, an editable "
                "bar chart comparing the cities, and a conclusion. Keep them clear and readable."
            )
            evidence["office_create_job"] = wait(job)
            assert evidence["office_create_job"]["status"] == "completed"
            original_docx = download("sales_brief.docx")
            original_pptx = download("sales_deck.pptx")
            evidence["office_create_checks"] = office_checks(
                original_docx, original_pptx, title="Q3 Sales Review"
            )
            revised = approve(
                "Edit the existing sales_brief.docx and sales_deck.pptx: change their titles "
                "to 'Q4 Sales Review' and add 'Forecast pending' to the conclusion in each. "
                "Keep all sales numbers, the chart and the rest of the content unchanged. "
                "Save separate copies as sales_brief_v2.docx and sales_deck_v2.pptx, "
                "keeping the originals."
            )
            evidence["office_edit_job"] = wait(revised)
            assert evidence["office_edit_job"]["status"] == "completed"
            evidence["office_edit_checks"] = office_checks(
                download("sales_brief_v2.docx"),
                download("sales_deck_v2.pptx"),
                title="Q4 Sales Review",
                note="Forecast pending",
            )
            assert download("sales_brief.docx") == original_docx
            assert download("sales_deck.pptx") == original_pptx
            evidence["originals_preserved"] = True
            evidence["passed"] = True
            return
        if scenario == "native-cancellation":
            import os
            import subprocess

            app = Path(__file__).resolve().parents[3] / "Alysis-Code-App-bots-dev"
            with (private / "native.log").open("w") as log:
                subprocess.run(
                    ["node", "tests/e2e-bots-execution.mjs"],
                    cwd=app,
                    env={
                        **os.environ,
                        "ALYSIS_BOTS_EXECUTION_LIVE": "1",
                        "ALYSIS_BOTS_EXECUTION_EVIDENCE": str(private),
                        "ALYSIS_BOTS_URL": str(client.base_url),
                        "ALYSIS_BOTS_TOKEN": client.headers["Authorization"].removeprefix(
                            "Bearer "
                        ),
                    },
                    stdout=log,
                    stderr=log,
                    timeout=240,
                    check=True,
                )
            evidence["native"] = json.loads((private / "native-execution.json").read_text())
            evidence["passed"] = evidence["native"]["passed"]
            return
        if scenario == "all":
            job = approve(
                "Use sales.csv to create a one-page PDF sales report with a table, the exact total, "
                "and a short conclusion. Also give me an Excel workbook with the source data and a total formula. "
                "Name the files sales_report.pdf and sales.xlsx."
            )
            status = until(lambda: started(job))
            assert status["status"] == "waiting"
            begin = time.monotonic()
            question = wait(send("What is 7 times 8?"))
            evidence["concurrent_chat"] = {
                "seconds": time.monotonic() - begin,
                "reply": question,
                "work_state_at_reply": get(f"/v1/tasks/{job}")["status"],
            }
            assert question["status"] == "completed" and "56" in question["output"]
            assert evidence["concurrent_chat"]["work_state_at_reply"] == "waiting"
            evidence["report_job"] = wait(job)
            assert evidence["report_job"]["status"] == "completed", evidence["report_job"].get(
                "error"
            )
            files = get(f"/v1/bots/{bot['id']}/files")
            evidence["files"] = files
            for filename in ("sales_report.pdf", "sales.xlsx"):
                file = next(f for f in files if f["name"] == filename)
                response = client.get(f"/v1/bots/{bot['id']}/files/{file['id']}/download")
                response.raise_for_status()
                data = base64.b64decode(response.json()["data_base64"], validate=True)
                (private / filename).write_bytes(data)
                assert hashlib.sha256(data).hexdigest() == file["sha256"]
            import openpyxl
            import pypdfium2

            pdf = pypdfium2.PdfDocument(str(private / "sales_report.pdf"))
            text = "\n".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
            assert (
                len(pdf) == 1
                and "68.50" in text
                and all(city in text for city in ("Athens", "Tokyo", "Zurich"))
            )
            pdf[0].render(scale=1.5).to_pil().save(private / "pdf-review.png")
            workbook = openpyxl.load_workbook(private / "sales.xlsx")
            cells = [cell.value for sheet in workbook for row in sheet for cell in row]
            assert all(city in cells for city in ("Athens", "Tokyo", "Zurich"))
            assert any(isinstance(value, str) and value.startswith("=SUM(") for value in cells)
            evidence["artifact_checks"] = {"pdf_selectable_text": text, "xlsx_cells": cells}

        if scenario in {"all", "correction"}:
            corrected = approve(
                "Use sales.csv to create a ZIP with 200 individually numbered PDF sales reports, "
                "each labelled 'Draft — USD'. Save it as report_bundle.zip."
            )
            until(lambda: started(corrected))
            change = wait(
                send("For the report bundle, change the label to 'Draft — EUR' instead of USD.")
            )
            evidence["correction_reply"] = change
            evidence["corrected_job"] = wait(corrected)
            assert evidence["corrected_job"]["status"] == "completed", evidence[
                "corrected_job"
            ].get("error")
            bundle = next(
                f for f in get(f"/v1/bots/{bot['id']}/files") if f["name"] == "report_bundle.zip"
            )
            response = client.get(f"/v1/bots/{bot['id']}/files/{bundle['id']}/download")
            response.raise_for_status()
            data = base64.b64decode(response.json()["data_base64"], validate=True)
            (private / "report_bundle.zip").write_bytes(data)
            import io
            import zipfile

            import pypdfium2

            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.namelist()
                assert len(members) == 200
                for member in members:
                    doc = pypdfium2.PdfDocument(archive.read(member))
                    contents = "\n".join(
                        doc[i].get_textpage().get_text_range() for i in range(len(doc))
                    )
                    assert "EUR" in contents and "USD" not in contents
            evidence["corrected_files_checked"] = len(members)
            # Retain independent artifact evidence even if acknowledgement fails.
            assert change["status"] == "completed", change.get("error")

        if scenario in {"all", "cancellation"}:
            cancelled = approve(
                "Use sales.csv to create a ZIP with 1000 individually numbered sales report PDFs, called large_bundle.zip."
            )
            until(lambda: started(cancelled))
            stop = wait(send("Stop creating that new bundle now."))
            evidence["stop_reply"] = stop
            evidence["cancelled_job"] = wait(cancelled)
            assert evidence["cancelled_job"]["status"] == "cancelled"
            assert not any(
                f["name"] == "large_bundle.zip" for f in get(f"/v1/bots/{bot['id']}/files")
            )
        evidence["passed"] = True
    finally:
        evidence["receipt"] = get("/study/receipt")
        (private / "execution-live.json").write_text(
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--existing-network", help="Explicit idle local study network to reuse")
    parser.add_argument("--port", type=int, default=7111)
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument("--model", default="glm-5.3-flash")
    parser.add_argument("--library-limit-bytes", type=int)
    parser.add_argument(
        "--scenario",
        choices=(
            "all",
            "correction",
            "planning",
            "reminder-update",
            "schedule-background",
            "cancellation",
            "native-cancellation",
            "office",
            "reliability",
            "reminders",
            "screen",
            "library",
            "library-full",
            "library-full-computer",
            "data-cleanup",
            "data-cleanup-large",
            "notes-search",
            "reference-race",
            "background-reference-race",
            "grounded-drafts",
            "invoice-reconcile",
            "invoice-correction",
        ),
        default="all",
    )
    args = parser.parse_args()
    if args.linux:
        linux()
    elif args.execute_live:
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
            Path(__file__).resolve().parents[3] / "Alysis-Code-App-bots-dev",
            None,
            study_script=Path(__file__).resolve(),
            bootstrap={
                "key": key,
                "base_url": profile.base_url,
                "model": args.model,
                "max_calls": args.max_calls,
                "library_limit_bytes": args.library_limit_bytes,
                "reference_race": args.scenario in {"reference-race", "background-reference-race"},
            },
            lazy_browser=True,
            exercise=partial(exercise, scenario=args.scenario),
            port=args.port,
            image="alysis-bots-portable-20261019:pilot",
            name_prefix="bots-execution-20261020-",
            existing_network=args.existing_network,
        )
    else:
        parser.error("Use --execute-live for the explicitly authorized bounded model study.")
