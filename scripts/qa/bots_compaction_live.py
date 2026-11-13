"""Opt-in GLM/Linux A/B context study. Synthetic archive; real model decisions.

Baseline uses the previous character-only history policy. Compacted mode uses
the shipped Bots limits. Both get identical source text and prompts. Subsequent
checks cover memory changes, process restart and peer reporting. No native UI.
All attempts, including summarization and failures, share a persisted 42-call cap.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4


def service(config):
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.models import BotInput, MemoryInput, TaskInput

    root = Path("/tmp/compaction")
    root.mkdir(exist_ok=True)
    settings = Settings(
        root / "bots.db",
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=config.get("max_calls", 42),
        output_tokens=2048,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    app = create_app(settings)
    store, owner, runtime = app.state.store, settings.owner, app.state.runtime
    path = root / "seed.json"
    if not path.exists():
        seed = {
            "docket": "東京-" + uuid4().hex[:12],
            "checksum": uuid4().hex,
            "old": "Παλαιό-" + uuid4().hex[:10],
            "new": "Νέο-" + uuid4().hex[:10],
            "release_old": "初版-" + uuid4().hex[:10],
            "release_new": "Διόρθωση-新版-" + uuid4().hex[:10],
            "private": "PRIVATE-" + uuid4().hex,
        }
        for mode, name in (("compacted", "Computer view test"), ("baseline", "No computer test")):
            bot = store.save_bot(
                owner,
                BotInput(
                    name=name, purpose="Maintain project context and retrieve original records."
                ),
            )
            chat = store.create_chat(owner, bot["id"], "Continuous conversation")
            memory = store.save_memory(
                owner, bot["id"], MemoryInput(title="Current review marker", content=seed["old"])
            )
            for i in range(75):
                text = (
                    f"The original handover docket is {seed['docket']}."
                    if i == 0
                    else f"The archived release checksum is {seed['checksum']}."
                    if i == 36
                    else f"Archived synthetic research entry {i}: " + "Δοκιμή 日本語 café. " * 120
                )
                if config.get("edge_check") and i == 0:
                    text += f" The initial project release code is {seed['release_old']}."
                task = store.enqueue(
                    owner, chat["id"], TaskInput(message=text, request_id=uuid4().hex)
                )
                store.finish(
                    owner,
                    task["id"],
                    "completed",
                    content="Synthetic archived receipt; no model was called.",
                )
            seed[mode] = {"bot": bot, "chat": chat, "memory": memory}
        if config.get("edge_check"):
            private_bot = seed["baseline"]["bot"]["id"]
            store.save_memory(
                owner,
                private_bot,
                MemoryInput(title="Private synthetic marker", content=seed["private"]),
            )
            private_task = store.enqueue(
                owner,
                seed["baseline"]["chat"]["id"],
                TaskInput(
                    message="This bot's private synthetic marker is " + seed["private"],
                    request_id=uuid4().hex,
                ),
            )
            store.finish(owner, private_task["id"], "completed", content="Private archived source.")
            seed["private_source"] = next(
                m["id"]
                for m in store.conversation(owner, private_task["chat_id"])["messages"]
                if m["task_id"] == private_task["id"] and m["role"] == "user"
            )
        peer = store.save_bot(
            owner,
            BotInput(
                name="Reviewer", purpose="Review delivery plans against your approved constraint."
            ),
        )
        store.save_memory(
            owner,
            peer["id"],
            MemoryInput(title="Delivery requirement", content="The report must use UTF-8 CSV."),
        )
        seed["peer"] = peer
        path.write_text(json.dumps(seed))
    seed = json.loads(path.read_text())
    mode_path = root / "mode"
    mode = mode_path.read_text() if mode_path.exists() else "baseline"
    defaults = runtime.limits
    runtime.limits = (
        replace(defaults, compaction_enabled=False, context_tokens=100000)
        if mode == "baseline"
        else defaults
    )
    original = runtime.client_factory
    trace_path = root / "traces.jsonl"
    gate_lock = threading.Lock()
    gate_release = threading.Event()
    gate = {}

    class Trace:
        def __init__(self, client, task_id):
            self.client, self.task_id = client, task_id

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            started = time.monotonic()
            task = store.task(owner, self.task_id)
            summary = runtime.context_summary.read(task["bot_id"])
            summary_visible = bool(summary["through_id"]) and any(
                summary["content"] in m.get("content", "")
                for m in kwargs["messages"]
                if isinstance(m.get("content"), str)
            )
            result = self.client.chat(**kwargs)
            record = {
                "task_id": self.task_id,
                "mode": mode,
                "tool_free": not kwargs.get("tools"),
                "seconds": time.monotonic() - started,
                "usage": asdict(result.usage) if result.usage else None,
                "tool_calls": [t.name for t in result.tool_calls],
                "content": result.content,
                "summary_visible": summary_visible,
                "background": task["status"] == "waiting",
                "bot_id": task["bot_id"],
                "other_private_in_input": seed["private"]
                in json.dumps(kwargs["messages"], ensure_ascii=False),
            }
            with trace_path.open("a") as target:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
            # Test-only delivery barrier AFTER the real provider returned. It
            # neither changes the response nor simulates a network failure.
            with gate_lock:
                hold = bool(
                    gate.get("armed")
                    and gate["bot_id"] == task["bot_id"]
                    and gate["stage"] == ("reply" if kwargs.get("tools") else "summary")
                )
                if hold:
                    gate.update(armed=False, held=True, task_id=self.task_id)
            if hold:
                released = gate_release.wait(timeout=90)
                with gate_lock:
                    gate.update(held=False, released=released)
            return result

    runtime.client_factory = lambda tid: Trace(original(tid), tid)

    @app.post("/study/gate")
    def arm_gate(data: dict):
        assert config.get("edge_check")
        assert data["stage"] in {"reply", "summary"}
        store.bot(owner, data["bot_id"])
        with gate_lock:
            assert not gate.get("armed") and not gate.get("held")
            gate_release.clear()
            gate.clear()
            gate.update(bot_id=data["bot_id"], stage=data["stage"], armed=True, held=False)
            return dict(gate)

    @app.get("/study/gate")
    def read_gate():
        with gate_lock:
            return dict(gate)

    @app.post("/study/gate/release")
    def release_gate():
        with gate_lock:
            gate["armed"] = False
        gate_release.set()
        return {"released": True}

    @app.post("/study/compact")
    def compact():
        nonlocal mode
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM tasks WHERE status IN ('queued','running','waiting','cancelling')"
                ).fetchone()[0]
                == 0
            )
        mode = "compacted"
        mode_path.write_text(mode)
        runtime.limits = defaults
        return {"mode": mode}

    @app.get("/study/evidence")
    def evidence():
        with store.connect() as db:
            summaries = [dict(r) for r in db.execute("SELECT * FROM bot_context_summaries")]
        return {
            "seed": seed,
            "pid": os.getpid(),
            "usage": store.usage(owner),
            "summaries": summaries,
            "traces": [json.loads(s) for s in trace_path.read_text().splitlines()]
            if trace_path.exists()
            else [],
        }

    uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")


def supervisor():
    config = json.loads(sys.stdin.readline())
    for _ in range(2):
        child = subprocess.Popen([sys.executable, "/study.py", "--service"], stdin=subprocess.PIPE)
        child.stdin.write((json.dumps(config) + "\n").encode())
        child.stdin.close()
        if child.wait() != -signal.SIGKILL:
            return


def exercise(client, bot, other, private, container):
    def api(method, path, data=None):
        reply = client.request(method, path, json=data)
        reply.raise_for_status()
        return reply.json() if reply.content else None

    def wait(task):
        end = time.monotonic() + 240
        while time.monotonic() < end:
            current = api("GET", f"/v1/tasks/{task['id']}")
            if current["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                result["tasks"].append(current)
                assert current["status"] == "completed", (current["status"], current["error"])
                return current
            time.sleep(0.3)
        raise TimeoutError("Bounded study task timed out")

    seed = api("GET", "/study/evidence")["seed"]
    result = {"passed": False, "checks": {}, "tasks": []}

    def ask(mode, prompt):
        chat = seed[mode]["chat"]["id"]
        return wait(
            api("POST", f"/v1/chats/{chat}/tasks", {"message": prompt, "request_id": uuid4().hex})
        )

    try:
        questions = [
            (
                "Find the original handover docket in our history. Read its original message and return the exact docket plus the source message ID. Do not create a note or file.",
                seed["docket"],
            ),
            (
                "Find the archived release checksum. Read its original message and return the exact checksum plus the source message ID. Do not create a note or file.",
                seed["checksum"],
            ),
            (
                "What is my current approved review marker? Return only its exact value; do not create or change anything.",
                seed["old"],
            ),
        ]
        for mode in ("baseline", "compacted"):
            if mode == "compacted":
                api("POST", "/study/compact")
            start = api("GET", "/v1/status")["usage"]
            for index, (prompt, expected) in enumerate(questions):
                answer = ask(mode, prompt)
                assert expected in answer["output"]
                result["checks"][f"{mode}_question_{index}"] = True
            end = api("GET", "/v1/status")["usage"]
            result[mode] = {
                k: end[k] - start[k]
                for k in ("calls", "reported_input_tokens", "reported_output_tokens")
            }
        evidence = api("GET", "/study/evidence")
        assert any(s["revision"] > 0 for s in evidence["summaries"])
        result["checks"]["real_summary_persisted"] = True
        # Raw originals remain searchable through the public, same-bot API.
        target = seed["compacted"]
        bot_id, memory_id = target["bot"]["id"], target["memory"]["id"]
        api(
            "PUT",
            f"/v1/bots/{bot_id}/memories/{memory_id}",
            {"title": "Current review marker", "content": seed["new"]},
        )
        answer = ask("compacted", questions[2][0])
        assert seed["new"] in answer["output"] and seed["old"] not in answer["output"]
        result["checks"]["memory_edit"] = True
        before_restart = api("GET", "/study/evidence")
        subprocess.run(
            ["docker", "exec", container, "kill", "-KILL", str(before_restart["pid"])],
            check=True,
            capture_output=True,
        )
        for _ in range(120):
            try:
                after = api("GET", "/study/evidence")
                if after["pid"] != before_restart["pid"]:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise TimeoutError("Service did not restart")
        assert after["summaries"] == before_restart["summaries"]
        answer = ask(
            "compacted",
            "Recall the original handover docket and current approved review marker. Return both exactly; no changes or actions.",
        )
        assert seed["docket"] in answer["output"] and seed["new"] in answer["output"]
        result["checks"]["restart_recall"] = True
        api("DELETE", f"/v1/bots/{bot_id}/memories/{memory_id}")
        answer = ask(
            "compacted",
            "Is there currently an approved review marker in saved memory? Check the current approved memory. If absent say MISSING; do not recover a previous marker from history or propose a replacement.",
        )
        assert (
            "MISSING" in answer["output"]
            and seed["new"] not in answer["output"]
            and seed["old"] not in answer["output"]
        )
        result["checks"]["memory_delete"] = True
        answer = ask(
            "compacted",
            "Ask Reviewer to review whether JSON is the right delivery format for our report using their own approved delivery requirement. Return their recommendation here. Do not create files or notes.",
        )
        assert "CSV" in answer["output"]
        exchanges = api("GET", f"/v1/bots/{bot_id}/exchanges")
        assert len(exchanges) == 1 and exchanges[0]["status"] == "completed"
        result["checks"]["peer_report"] = True
        assert (
            result["compacted"]["reported_input_tokens"]
            < result["baseline"]["reported_input_tokens"]
        )
        result["passed"] = True
    finally:
        result["evidence"] = api("GET", "/study/evidence")
        path = private / "compaction-result.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "passed": result["passed"],
                    "checks": result["checks"],
                    "baseline": result.get("baseline"),
                    "compacted": result.get("compacted"),
                    "usage": result["evidence"]["usage"],
                    "evidence": str(path),
                }
            ),
            flush=True,
        )


def exercise_background(client, bot, other, private, container):
    def api(method, path, data=None):
        reply = client.request(method, path, json=data)
        reply.raise_for_status()
        return reply.json() if reply.content else None

    result = {"passed": False}
    try:
        api("POST", "/study/compact")
        seed = api("GET", "/study/evidence")["seed"]
        task = api(
            "POST",
            f"/v1/chats/{seed['compacted']['chat']['id']}/tasks",
            {
                "message": "Continue in the background first. Then find the original handover docket and the archived release checksum in our conversation. Read both original source messages and return both exact values with source message IDs. Do not create notes, files or routines.",
                "request_id": uuid4().hex,
            },
        )
        end = time.monotonic() + 240
        while time.monotonic() < end:
            task = api("GET", f"/v1/tasks/{task['id']}")
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.3)
        result["task"] = task
        assert task["status"] == "completed", (task["status"], task["error"])
        assert seed["docket"] in task["output"] and seed["checksum"] in task["output"]
        assert any(e["kind"] == "background_started" for e in task["events"])
        traces = api("GET", "/study/evidence")["traces"]
        background = [t for t in traces if t["background"]]
        assert background and all(t["summary_visible"] for t in background)
        result["passed"] = True
    finally:
        result["evidence"] = api("GET", "/study/evidence")
        path = private / "background-result.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "passed": result["passed"],
                    "usage": result["evidence"]["usage"],
                    "evidence": str(path),
                }
            ),
            flush=True,
        )


def exercise_edges(client, bot, other, private, container):
    """Real responses plus an explicit delivery barrier; no scripted model output."""
    result = {"passed": False, "checks": {}, "failures": {}, "tasks": []}

    def api(method, path, data=None):
        reply = client.request(method, path, json=data)
        reply.raise_for_status()
        return reply.json() if reply.content else None

    seed = api("GET", "/study/evidence")["seed"]
    primary = seed["compacted"]["bot"]["id"]

    def summary(bot_id=primary):
        return next(
            (s for s in api("GET", "/study/evidence")["summaries"] if s["bot_id"] == bot_id), None
        )

    def send(prompt, mode="compacted"):
        return api(
            "POST",
            f"/v1/chats/{seed[mode]['chat']['id']}/tasks",
            {"message": prompt, "request_id": uuid4().hex},
        )

    def wait(task, status="completed"):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = api("GET", f"/v1/tasks/{task['id']}")
            if current["status"] in {"completed", "failed", "interrupted", "cancelled"}:
                result["tasks"].append(current)
                assert current["status"] == status, "Unexpected terminal task state"
                return current
            time.sleep(0.25)
        raise TimeoutError("Study task did not finish")

    def held(task):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = api("GET", "/study/gate")
            if state.get("held") and state.get("task_id") == task["id"]:
                return state
            current = api("GET", f"/v1/tasks/{task['id']}")
            assert current["status"] not in {"failed", "cancelled", "completed", "interrupted"}, (
                "Task finished before barrier"
            )
            time.sleep(0.25)
        raise TimeoutError("Real response did not reach barrier")

    def run_case(name, action):
        try:
            action()
            result["checks"][name] = True
        except Exception as exc:
            result["checks"][name] = False
            result["failures"][name] = type(exc).__name__
        finally:
            api("POST", "/study/gate/release")
        print(json.dumps({"check": name, "passed": result["checks"][name]}), flush=True)

    def repeated():
        first = wait(
            send(
                "What is the current project release code from our archived work? Return only the exact code. Do not change anything."
            )
        )
        assert seed["release_old"] in first["output"], "Initial archived decision was lost"
        revisions = [summary()["revision"]]
        corrected = wait(
            send(
                f"Correction: the current project release code is now {seed['release_new']}. This replaces the earlier release code. Update the project working note with this decision; this is project context, not a personal-memory proposal. Reply briefly with the current code."
            )
        )
        assert seed["release_new"] in corrected["output"], "Live correction was not applied"
        notes = api("GET", f"/v1/bots/{primary}/notes")
        assert any(seed["release_new"] in n["content"] for n in notes), "Project note not updated"
        revisions.append(summary()["revision"])
        for prompt in (
            "Return only the current project release code, using the latest correction.",
            "Ποιος είναι ο τρέχων κωδικός έκδοσης του έργου; Γράψε μόνο τον ακριβή κωδικό.",
            "現在のプロジェクトのリリースコードだけを正確に返してください。",
        ):
            answer = wait(send(prompt))
            assert (
                seed["release_new"] in answer["output"]
                and seed["release_old"] not in answer["output"]
            ), "An older summary overrode a current decision"
            revisions.append(summary()["revision"])
        assert all(b > a for a, b in zip(revisions, revisions[1:], strict=False)), (
            "The test did not exercise repeated compaction"
        )
        result["summary_revisions"] = revisions

    def memory_during_answer():
        api("POST", "/study/gate", {"bot_id": primary, "stage": "reply"})
        task = send(
            "What is my current approved review marker? Return only the exact marker from current saved memory. Do not change anything."
        )
        held(task)
        memory = seed["compacted"]["memory"]["id"]
        api(
            "PUT",
            f"/v1/bots/{primary}/memories/{memory}",
            {"title": "Current review marker", "content": seed["new"]},
        )
        api("POST", "/study/gate/release")
        answer = wait(task)
        assert seed["new"] in answer["output"] and seed["old"] not in answer["output"], (
            "A stale answer was published"
        )
        traces = [
            t
            for t in api("GET", "/study/evidence")["traces"]
            if t["task_id"] == task["id"] and not t["tool_free"]
        ]
        assert len(traces) >= 2 and seed["old"] in (traces[0]["content"] or ""), (
            "No real stale provider response was observed"
        )

    def memory_during_summary():
        target = seed["baseline"]
        bot_id = target["bot"]["id"]
        api("POST", "/study/gate", {"bot_id": bot_id, "stage": "summary"})
        task = send(
            "Return only the current approved review marker; no changes or actions.", "baseline"
        )
        held(task)
        api(
            "PUT",
            f"/v1/bots/{bot_id}/memories/{target['memory']['id']}",
            {"title": "Current review marker", "content": seed["new"]},
        )
        api("POST", "/study/gate/release")
        answer = wait(task)
        assert seed["new"] in answer["output"] and seed["old"] not in answer["output"]
        assert any(e["kind"] == "context_compaction_discarded" for e in answer["events"]), (
            "Stale summary was not discarded"
        )
        assert not summary(bot_id)["through_id"], "Stale summary advanced the cursor"

    def stop_during_summary():
        before = summary()
        api("POST", "/study/gate", {"bot_id": primary, "stage": "summary"})
        task = send(
            "Create a text file named cancelled-summary-check.txt containing the current project release code."
        )
        held(task)
        api("POST", f"/v1/tasks/{task['id']}/cancel")
        api("POST", "/study/gate/release")
        stopped = wait(task, "cancelled")
        after = summary()
        assert after["revision"] == before["revision"] and after["content"] == before["content"], (
            "Stop committed a summary"
        )
        assert not stopped["output"]
        assert not any(
            f["name"] == "cancelled-summary-check.txt"
            for f in api("GET", f"/v1/bots/{primary}/files")
        ), "Stopped task executed its write"
        result["cancelled_task"] = task["id"]

    def cancellation_report():
        assert result["checks"].get("stop_during_summary"), "Stop prerequisite failed"
        answer = wait(
            send(
                "Check whether cancelled-summary-check.txt was created by the task I stopped. Return NOT_CREATED if it does not exist. Do not create it or restart any work."
            )
        )
        assert "NOT_CREATED" in answer["output"], "Cancelled work was reported as complete"
        assert not any(
            f["name"] == "cancelled-summary-check.txt"
            for f in api("GET", f"/v1/bots/{primary}/files")
        )

    def isolation():
        answer = wait(
            send(
                f"Try read_message for source ID {seed['private_source']}. Do not consult any other bot or request cooperation. If this source is unavailable to you, return UNAVAILABLE. Do not guess its content."
            )
        )
        assert "UNAVAILABLE" in answer["output"] and seed["private"] not in answer["output"]
        events = answer["events"]
        assert any(
            e["kind"] == "tool_failed" and e["data"].get("name") == "read_message" for e in events
        ), "Scope rejection was not exercised"
        traces = [t for t in api("GET", "/study/evidence")["traces"] if t["bot_id"] == primary]
        assert all(
            not t["other_private_in_input"] and seed["private"] not in (t["content"] or "")
            for t in traces
        ), "Cross-bot private marker entered context"

    try:
        api("POST", "/study/compact")
        for name, action in (
            ("repeated_compaction_and_correction", repeated),
            ("memory_change_before_answer_commit", memory_during_answer),
            ("memory_change_before_summary_commit", memory_during_summary),
            ("stop_during_summary", stop_during_summary),
            ("cancelled_work_report", cancellation_report),
            ("bot_scope_rejection", isolation),
        ):
            run_case(name, action)
        result["passed"] = all(result["checks"].values())
    finally:
        api("POST", "/study/gate/release")
        result["evidence"] = api("GET", "/study/evidence")
        path = private / "edges-result.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "passed": result["passed"],
                    "checks": result["checks"],
                    "failures": result["failures"],
                    "usage": result["evidence"]["usage"],
                    "evidence": str(path),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--background-check", action="store_true")
    parser.add_argument("--edge-check", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--existing-network")
    args = parser.parse_args()
    if args.background_check and args.edge_check:
        parser.error("Choose one focused study per invocation")
    if args.linux:
        supervisor()
    elif args.service:
        service(json.loads(sys.stdin.readline()))
    elif args.execute_live and args.app:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from bots_coding_context_study import validate_plan
        from bots_computer_view import host

        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        config = load_config()
        profile = get_profile(config, "zai-coding-plan")
        validate_plan(profile)
        key = resolve_profile_api_key(config, "zai-coding-plan").key
        if not key:
            raise RuntimeError("Saved Z.ai Coding Plan unavailable; no fallback.")
        host(
            args.app.resolve(),
            None,
            study_script=Path(__file__).resolve(),
            bootstrap={
                "key": key,
                "base_url": profile.base_url,
                "max_calls": 12 if args.background_check else 48 if args.edge_check else 42,
                "edge_check": args.edge_check,
            },
            lazy_browser=True,
            exercise=exercise_background
            if args.background_check
            else exercise_edges
            if args.edge_check
            else exercise,
            port=7091,
            image="bots-durable-computer-20261019:dev",
            name_prefix="bots-compaction-20261019-",
            existing_network=args.existing_network,
            memory="2g",
        )
    else:
        parser.error("Explicit --execute-live and --app required (42 calls maximum).")
