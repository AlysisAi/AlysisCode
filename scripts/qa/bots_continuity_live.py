"""Opt-in real GLM continuity study on isolated Linux, without a model-call ceiling.

Ordinary live requests surround explicitly seeded synthetic pressure history.
Only this study lowers the history-compaction threshold; the model window and
production defaults stay intact. Credentials enter through stdin, never artifacts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4


def service(config):
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
    from alysis_code.bots.models import BotInput, FileInput, TaskInput
    from alysis_code.bots.workspaces import WorkspaceStore

    root = Path("/tmp/continuity")
    root.mkdir(exist_ok=True)
    settings = Settings(
        root / "bots.db",
        config["token"],
        config["key"],
        config["base_url"],
        config["model"],
        provider_key="zai_coding_plan",
        reasoning_effort="low",
        max_calls=config.get("max_calls"),  # Optional persisted study-only bound.
    )
    computers = DesktopComputers(
        settings.owner,
        ComputerViews(),
        factory=OfflineLinuxDesktop,
        workspaces=WorkspaceStore(root / "workspaces"),
    )
    app = create_app(settings, desktop_computers=computers)
    store, runtime, owner = app.state.store, app.state.runtime, settings.owner
    defaults = asdict(runtime.limits)
    seed_path = root / "seed.json"
    if not seed_path.exists():
        bot = store.save_bot(
            owner,
            BotInput(
                name="Computer view test", purpose="Help with project planning and documents."
            ),
        )
        other = store.save_bot(
            owner, BotInput(name="No computer test", purpose="Keep a separate project.")
        )
        chat = store.create_chat(owner, bot["id"], "Continuous project chat")
        seed = {
            "bot": bot,
            "other": other,
            "chat": chat,
            "docket": "東京-" + uuid4().hex[:12],
            "codename": "Κέδρος-" + uuid4().hex[:8],
        }
        store.save_file(
            owner,
            bot["id"],
            FileInput(
                name="sales.csv", content="city,amount\nAthens,17.25\nTokyo,42.50\nZurich,8.75\n"
            ),
        )
        seed_path.write_text(json.dumps(seed))
    seed = json.loads(seed_path.read_text())
    pressure_path = root / "pressure.json"
    if pressure_path.exists():
        runtime.limits = replace(runtime.limits, history_tokens=6000)
    factory = runtime.client_factory
    trace_path = root / "traces.jsonl"

    class Trace:
        def __init__(self, client, task_id):
            self.client, self.task_id = client, task_id

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            start = time.monotonic()
            record = {"task_id": self.task_id, "tool_free": not kwargs.get("tools")}
            if config.get("trace_requests"):
                # Opt-in synthetic-memory study only; no production conversations.
                record["request_messages"] = kwargs["messages"]
            try:
                result = self.client.chat(**kwargs)
                record.update(
                    content=result.content,
                    tool_calls=[
                        {"name": c.name, "arguments": c.arguments} for c in result.tool_calls
                    ],
                    usage=asdict(result.usage) if result.usage else None,
                )
                return result
            except Exception as exc:
                record["error_type"] = type(exc).__name__
                raise
            finally:
                record["seconds"] = time.monotonic() - start
                with trace_path.open("a") as target:
                    target.write(json.dumps(record, ensure_ascii=False) + "\n")

    runtime.client_factory = lambda task_id: Trace(factory(task_id), task_id)

    @app.post("/study/pressure")
    def pressure():
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM tasks WHERE status IN ('queued','running','waiting','cancelling')"
                ).fetchone()[0]
                == 0
            )
        batch = (
            json.loads(pressure_path.read_text())["batches"] + 1 if pressure_path.exists() else 1
        )
        records = []
        # This is openly labelled archive seeding, not claimed past model work.
        # Vary incidental project records without providing answers to recall probes.
        for index in range(24):
            lines = []
            for row in range(12):
                lines.append(
                    f"Workshop observation {batch}.{index}.{row}: participant group {row + 1} "
                    f"reviewed sample {uuid4().hex[:10]}. The fictional exercise covered "
                    "document navigation, label readability and offline export. "
                    "These are archive-only research notes, not new decisions or actions. "
                    "The participant retained the text Αθήνα, 東京 and Zürich unchanged."
                )
            task = store.enqueue(
                owner,
                seed["chat"]["id"],
                TaskInput(
                    message="Synthetic archived workshop record.\n" + "\n".join(lines),
                    request_id=uuid4().hex,
                ),
            )
            store.finish(
                owner,
                task["id"],
                "completed",
                content="Archive fixture only; no model or action was executed.",
            )
            records.append(task["id"])
        pressure_path.write_text(json.dumps({"batches": batch}))
        runtime.limits = replace(runtime.limits, history_tokens=6000)
        return {
            "batch": batch,
            "seeded_task_ids": records,
            "history_tokens": 6000,
            "production_history_tokens": defaults["history_tokens"],
        }

    @app.get("/study/evidence")
    def evidence():
        with store.connect() as db:
            summaries = [dict(r) for r in db.execute("SELECT * FROM bot_context_summaries")]
        return {
            "seed": seed,
            "pid": os.getpid(),
            "usage": store.usage(owner),
            "defaults": defaults,
            "limits": asdict(runtime.limits),
            "summaries": summaries,
            "traces": [json.loads(s) for s in trace_path.read_text().splitlines()]
            if trace_path.exists()
            else [],
        }

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        computers.close()


def supervisor():
    config = json.loads(sys.stdin.readline())
    for _ in range(2):
        child = subprocess.Popen([sys.executable, "/study.py", "--service"], stdin=subprocess.PIPE)
        child.stdin.write((json.dumps(config) + "\n").encode())
        child.stdin.close()
        if child.wait() != -signal.SIGKILL:
            return


def exercise(client, bot, other, private, container):
    result = {
        "kind": "Live GLM + local Linux HTTP/Code; not Azure or native UI",
        "checks": {},
        "tasks": {},
        "pressure": [],
        "failures": [],
    }

    def api(method, path, data=None):
        reply = client.request(method, path, **({"json": data} if data is not None else {}))
        if reply.status_code >= 400:
            raise RuntimeError(f"Study HTTP {reply.status_code} at {path}")
        return reply.json() if reply.content else None

    seed = api("GET", "/study/evidence")["seed"]
    chat_id, bot_id = seed["chat"]["id"], bot["id"]

    def persist():
        result["evidence"] = api("GET", "/study/evidence")
        (private / "continuity-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )

    def check(label, value):
        result["checks"][label] = bool(value)
        print(json.dumps({"check": label, "passed": bool(value)}), flush=True)

    def wait(task_id, seconds=600):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            task = api("GET", f"/v1/tasks/{task_id}")
            if task["status"] in {"completed", "failed", "interrupted", "cancelled"}:
                return task
            time.sleep(0.4)
        api("POST", f"/v1/tasks/{task_id}/cancel")
        raise RuntimeError("Study watchdog cancelled a stalled task; not a product deadline.")

    def ask(label, prompt):
        submitted = api(
            "POST", f"/v1/chats/{chat_id}/tasks", {"message": prompt, "request_id": uuid4().hex}
        )
        task = wait(submitted["id"])
        result["tasks"][label] = task
        print(
            json.dumps({"task": label, "status": task["status"], "calls": task["calls"]}),
            flush=True,
        )
        persist()
        if task["status"] != "completed":
            result["failures"].append(
                {"label": label, "status": task["status"], "error": task.get("error")}
            )
        return task

    def contains(task, values):
        return task["status"] == "completed" and all(
            v.casefold() in task["output"].casefold() for v in values
        )

    try:
        defaults = api("GET", "/study/evidence")["defaults"]
        check(
            "no_fixed_call_cap",
            defaults["max_calls"] is None and defaults["calls_per_task"] is None,
        )
        initial = ask(
            "initial_project",
            f"We are planning the fictional Harbor Notes pilot, codename {seed['codename']}. "
            f"The initial audience is independent designers. Email integration is excluded. "
            f"Whether offline import is enough is unresolved. Our original handover reference is {seed['docket']}. "
            "Keep a working project note with the decisions and unknowns. We have not chosen a launch date or a budget.",
        )
        original = next(
            m
            for m in api("GET", f"/v1/chats/{chat_id}")["messages"]
            if m["task_id"] == initial["id"] and m["role"] == "user"
        )
        ask(
            "correction",
            "Change Harbor Notes to independent translators instead of designers. "
            "Offline import is sufficient for the pilot. Our next action is a three-translator pilot. "
            "Keep email excluded and update the existing project note. Do not invent a date or budget.",
        )
        ask(
            "preference",
            "Please remember my preference for brief answers with a short explanation of uncertainties.",
        )
        memories = api("GET", f"/v1/bots/{bot_id}/memories")
        pending = [m for m in memories if m["status"] == "pending"]
        for memory in pending:
            api("PATCH", f"/v1/bots/{bot_id}/memories/{memory['id']}", {"status": "active"})
        check("real_memory_proposal", bool(pending))
        notes = api("GET", f"/v1/bots/{bot_id}/notes")
        result["notes_before_pressure"] = notes
        check("real_note_correction", any(n["revision"] >= 2 for n in notes))

        result["pressure"].append(api("POST", "/study/pressure"))
        recall = ask(
            "recall_after_pressure",
            "Returning to Harbor Notes after those workshops: what did we finally agree "
            "about its audience, import approach, excluded integration and next pilot? Also give the codename "
            "and original handover reference. Separate current decisions from outdated ones and unknowns.",
        )
        check(
            "corrected_recall",
            contains(recall, ["translators", "email", "offline", seed["codename"], seed["docket"]]),
        )
        exact = ask(
            "original_source",
            "Please find the original message where I first supplied the handover reference. "
            "Give its exact reference and the source message ID, plus what the audience was at that time. "
            "Do not substitute the current project note for the historical source.",
        )
        source_tools = [
            e
            for e in exact["events"]
            if e["kind"] == "tool_completed"
            and e["data"].get("name") in {"search_history", "read_message"}
        ]
        check(
            "source_retrieval",
            contains(exact, [seed["docket"], "designers", original["id"]]) and bool(source_tools),
        )
        unknown = ask(
            "unknowns",
            "What exact launch date and budget have I approved for Harbor Notes? "
            "Only report agreed values. If we have not decided, tell me what is still open.",
        )
        result["unknown_answer"] = unknown["output"]
        result["pressure"].append(api("POST", "/study/pressure"))
        after = ask(
            "second_pressure",
            "Draft a short pilot checklist from the current Harbor Notes decisions. "
            "Keep the fixed scope and flag the missing decisions. Do not schedule or contact anyone.",
        )
        check("second_pressure_recall", contains(after, ["translators", "email", "offline"]))
        before = api("GET", "/study/evidence")
        summaries = before["summaries"]
        check("multiple_real_compactions", any(s["revision"] >= 2 for s in summaries))
        subprocess.run(
            ["docker", "exec", container, "kill", "-KILL", str(before["pid"])],
            check=True,
            capture_output=True,
        )
        for _ in range(120):
            try:
                restarted = api("GET", "/study/evidence")
                if restarted["pid"] != before["pid"]:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("Isolated Linux child did not restart.")
        check("summary_survived_restart", restarted["summaries"] == summaries)
        restored = ask(
            "restart_recall",
            "Continue our Harbor Notes planning. Remind me of the current audience, "
            "next pilot action, codename and original handover reference. What remains undecided?",
        )
        check(
            "restart_recall", contains(restored, ["translators", seed["codename"], seed["docket"]])
        )

        request = ask(
            "document_request",
            "Create a one-page editable Word handout for the current Harbor Notes pilot, "
            "using our latest decisions. Include the codename, handover reference and open decisions. "
            "Also use sales.csv from my files to calculate the total, and return a separate summary.txt "
            "with the total to two decimals. Keep it concise and verify the actual saved files.",
        )
        proposals = api("GET", f"/v1/bots/{bot_id}/proposals")
        matches = [
            p for p in proposals if p["source_task_id"] == request["id"] and p["offline_computer"]
        ]
        check("natural_computer_permission", len(matches) == 1)
        if len(matches) == 1:
            proposal = matches[0]
            accepted = api(
                "PATCH",
                f"/v1/bots/{bot_id}/proposals/{proposal['id']}",
                {
                    "action": "accept",
                    "expected_revision": proposal["revision"],
                    "offline_computer": True,
                },
            )
            completed = wait(accepted["task_id"], seconds=900)
            result["tasks"]["document_execution"] = completed
            check(
                "background_completed",
                completed["status"] == "completed"
                and any(e["kind"] == "background_started" for e in completed["events"]),
            )
            files = api("GET", f"/v1/bots/{bot_id}/files")
            result["files"] = files
            check(
                "requested_files_present",
                any(f["name"].endswith(".docx") for f in files)
                and any(f["name"] == "summary.txt" for f in files),
            )
            for file in files:
                if file["name"] == "sales.csv":
                    continue
                encoded = api("GET", f"/v1/bots/{bot_id}/files/{file['id']}/download")
                raw = base64.b64decode(encoded["data_base64"], validate=True)
                assert hashlib.sha256(raw).hexdigest() == file["sha256"]
                # Artifact names are model output; keep downloads inside the evidence folder.
                (private / Path(file["name"]).name).write_bytes(raw)
                if file["name"].endswith(".docx"):
                    from xml.etree import ElementTree

                    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                        assert archive.testzip() is None
                        text = " ".join(
                            ElementTree.fromstring(archive.read("word/document.xml")).itertext()
                        )
                    result["docx_text"] = text
                    check(
                        "docx_has_latest_source",
                        all(
                            v.casefold() in text.casefold()
                            for v in [seed["codename"], seed["docket"], "translators"]
                        ),
                    )
                if file["name"] == "summary.txt":
                    check("exact_file_total", raw.decode().strip() == "68.50")
        persist()
    finally:
        persist()
        print(
            json.dumps(
                {
                    "checks": result["checks"],
                    "failures": result["failures"],
                    "usage": result["evidence"]["usage"],
                    "evidence": str(private),
                }
            ),
            flush=True,
        )
    if result["failures"] or not all(result["checks"].values()):
        raise RuntimeError("Live study recorded failed checks; inspect the retained receipt.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--model", default="glm-5.3-flash")
    parser.add_argument("--existing-network")
    parser.add_argument("--port", type=int, default=7094)
    args = parser.parse_args()
    if args.service:
        service(json.loads(sys.stdin.readline()))
    elif args.linux:
        supervisor()
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
            raise RuntimeError("Saved Z.ai Coding Plan unavailable; no fallback.")
        host(
            args.app.resolve(),
            None,
            study_script=Path(__file__).resolve(),
            bootstrap={"key": key, "base_url": profile.base_url, "model": args.model},
            lazy_browser=True,
            exercise=exercise,
            port=args.port,
            image="bots-durable-computer-20261019:dev",
            name_prefix="bots-continuity-20261021-",
            existing_network=args.existing_network,
            memory="2g",
            tmpfs_size="1g",
        )
    else:
        parser.error("Use --execute-live --app PATH to opt into real model calls.")
