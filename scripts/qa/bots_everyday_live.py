"""Opt-in real GLM/Linux everyday study; no native app or existing preview changes.

History and overdue schedules are synthetic seed data, not model-created history.
Memory edits, a process restart, wall-clock wakeup and peer/background work use
the real HTTP service. A hard persisted call allowance survives the restart.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


def linux_service(config):
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
    from alysis_code.bots.models import BotInput, MemoryInput, NoteInput, TaskInput
    from alysis_code.bots.scheduling import ScheduleInput
    from alysis_code.bots.workspaces import WorkspaceStore

    root = Path("/tmp/everyday")
    root.mkdir(exist_ok=True)
    settings = Settings(
        root / "bots.db",
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=28,
        output_tokens=2048,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    computers = DesktopComputers(
        settings.owner,
        ComputerViews(),
        factory=OfflineLinuxDesktop,
        workspaces=WorkspaceStore(root / "workspaces"),
    )
    app = create_app(settings, desktop_computers=computers)
    store, owner = app.state.store, settings.owner
    seeded = root / "seed.json"
    if not seeded.exists():
        bot = store.save_bot(
            owner,
            BotInput(
                name="Computer view test", purpose="Maintain the project and its current decisions."
            ),
        )
        other = store.save_bot(owner, BotInput(name="No computer test", purpose="Remain idle."))
        peer = store.save_bot(
            owner,
            BotInput(
                name="Reviewer", purpose="Review a plan against approved delivery constraints."
            ),
        )
        store.save_memory(
            owner,
            peer["id"],
            MemoryInput(
                title="Delivery constraint", content="The required delivery format is UTF-8 CSV."
            ),
        )
        store.save_memory(
            owner,
            other["id"],
            MemoryInput(title="Private marker", content="PRIVATE-" + uuid4().hex),
        )
        chat = store.create_chat(owner, bot["id"], "Continuous project chat")
        old, new, docket = (
            "Παλαιό-" + uuid4().hex[:8],
            "Νέο-" + uuid4().hex[:8],
            "東京-" + uuid4().hex[:8],
        )
        memory = store.save_memory(
            owner,
            bot["id"],
            MemoryInput(title="Review marker", content=f"The current review marker is {old}."),
        )
        note = store.save_note(
            owner,
            bot["id"],
            NoteInput(
                title="Delivery decision",
                content="Deliver the report in UTF-8 CSV. Offline work only.",
            ),
        )
        for index in range(75):
            task = store.enqueue(
                owner,
                chat["id"],
                TaskInput(
                    request_id=uuid4().hex,
                    message=(
                        f"The original handover docket is {docket}."
                        if index == 0
                        else f"Archived synthetic research entry {index}: "
                        + ("Δοκιμή 日本語 café. " * 120)
                    ),
                ),
            )
            store.finish(
                owner,
                task["id"],
                "completed",
                content="Synthetic archived receipt; no model was called.",
            )
        overdue = store.save_schedule(
            owner,
            other["id"],
            ScheduleInput(
                name="Missed synthetic occurrence",
                prompt="This overdue occurrence must not run.",
                timing={"kind": "once", "at": datetime.now(UTC) - timedelta(minutes=30)},
            ),
            now=time.time() - 3600,
        )
        seeded.write_text(
            json.dumps(
                dict(
                    bot=bot,
                    peer=peer,
                    chat=chat,
                    memory=memory,
                    note=note,
                    old=old,
                    new=new,
                    docket=docket,
                    overdue=overdue,
                )
            )
        )
    seed = json.loads(seeded.read_text())
    original = app.state.runtime.client_factory
    traces = root / "traces.jsonl"

    class TraceClient:
        def __init__(self, client, task_id):
            self.client, self.task_id = client, task_id

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            raw = json.dumps(kwargs["messages"], ensure_ascii=False)
            response = self.client.chat(**kwargs)
            with traces.open("a") as target:
                target.write(
                    json.dumps(
                        dict(
                            task_id=self.task_id,
                            input_has_docket=seed["docket"] in raw,
                            input_has_old=seed["old"] in raw,
                            input_has_new=seed["new"] in raw,
                            tools=[c.name for c in response.tool_calls],
                            content=response.content,
                        ),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            return response

    app.state.runtime.client_factory = lambda task_id: TraceClient(original(task_id), task_id)

    @app.get("/study/evidence")
    def evidence():
        return dict(
            seed=seed,
            pid=os.getpid(),
            usage=store.usage(owner),
            traces=[json.loads(line) for line in traces.read_text().splitlines()]
            if traces.exists()
            else [],
            overdue_runs=store.schedule_runs(
                owner, seed["overdue"]["bot_id"], seed["overdue"]["id"]
            ),
        )

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        computers.close()


def linux_supervisor():
    # Test supervisor only: one explicit SIGKILL/restart, never replay provider calls.
    config = json.loads(sys.stdin.readline())
    for _ in range(2):
        child = subprocess.Popen([sys.executable, "/study.py", "--service"], stdin=subprocess.PIPE)
        child.stdin.write((json.dumps(config) + "\n").encode())
        child.stdin.close()
        code = child.wait()
        if code != -signal.SIGKILL:
            return


def exercise(client, bot, other, private, container):
    import httpx

    result = {
        "kind": "Live GLM and real Linux HTTP service; seeded synthetic history/overdue run",
        "passed": False,
        "checks": {},
    }

    def api(method, path, data=None):
        response = client.request(method, path, **({"json": data} if data is not None else {}))
        response.raise_for_status()
        return response.json() if response.content else None

    def until(read, accepts=lambda value: bool(value), seconds=240):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                value = read()
                if accepts(value):
                    return value
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Everyday study deadline reached")

    def wait(task):
        final = until(
            lambda: api("GET", f"/v1/tasks/{task['id']}"),
            lambda value: value["status"] in {"completed", "failed", "cancelled", "interrupted"},
        )
        result.setdefault("tasks", []).append(final)
        assert final["status"] == "completed", final
        return final

    seed = api("GET", "/study/evidence")["seed"]
    chat = seed["chat"]["id"]

    def send(message):
        return api("POST", f"/v1/chats/{chat}/tasks", dict(message=message, request_id=uuid4().hex))

    try:
        recall = wait(
            send(
                "Find the original handover docket from our earlier conversation history and return its exact value. Do not guess."
            )
        )
        assert seed["docket"] in recall["output"]
        trace = [
            row for row in api("GET", "/study/evidence")["traces"] if row["task_id"] == recall["id"]
        ]
        assert not trace[0]["input_has_docket"] and any(
            "search_history" in row["tools"] or "read_message" in row["tools"] for row in trace
        )
        result["checks"]["archived_retrieval"] = True

        calls = api("GET", "/v1/status")["usage"]["calls"]
        task = send("What is my current approved review marker? Reply only with its exact value.")
        until(
            lambda: api("GET", "/v1/status")["usage"],
            lambda u: u["calls"] > calls and u["unconfirmed_calls"] > 0,
        )
        api(
            "PUT",
            f"/v1/bots/{bot['id']}/memories/{seed['memory']['id']}",
            dict(title="Review marker", content=f"The current review marker is {seed['new']}."),
        )
        corrected = wait(task)
        assert seed["new"] in corrected["output"] and seed["old"] not in corrected["output"]
        assert any(e["kind"] == "memory_refreshed" for e in corrected["events"])
        result["checks"]["inflight_memory_correction"] = True

        pid = api("GET", "/study/evidence")["pid"]
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-c",
                f"import os,signal;os.kill({pid},signal.SIGKILL)",
            ],
            check=True,
        )
        until(lambda: api("GET", "/study/evidence"), lambda e: e["pid"] != pid)
        restored = wait(
            send(
                "After the restart, what is my current approved review marker? Reply with its exact value."
            )
        )
        assert seed["new"] in restored["output"] and seed["old"] not in restored["output"]
        missed = api("GET", "/study/evidence")["overdue_runs"]["runs"]
        assert (
            len(missed) == 1
            and missed[0]["current_status"] == "missed"
            and not missed[0]["task_id"]
        )
        result["checks"]["process_restart_and_missed_no_replay"] = True

        wait(
            send(
                "In 30 seconds, remind me of the current delivery format in our project notes. Create this one-time reminder now."
            )
        )
        schedules = api("GET", f"/v1/bots/{bot['id']}/schedules")
        assert len(schedules) == 1 and schedules[0]["timing"]["kind"] == "once"
        schedule = schedules[0]
        runs = until(
            lambda: api("POST", f"/v1/bots/{bot['id']}/schedules/{schedule['id']}/runs", {}),
            lambda value: bool(value["runs"]),
        )["runs"]
        fired = wait({"id": runs[0]["task_id"]})
        assert "CSV" in fired["output"]
        time.sleep(2)
        assert (
            len(api("POST", f"/v1/bots/{bot['id']}/schedules/{schedule['id']}/runs", {})["runs"])
            == 1
        )
        result["checks"]["actual_wall_clock_reminder_once"] = True
        wait(send("Create a recurring routine every 5 minutes to review current delivery notes."))
        recurring = next(
            row
            for row in api("GET", f"/v1/bots/{bot['id']}/schedules")
            if row["timing"]["kind"] == "interval"
        )
        wait(send("Pause the recurring delivery-notes routine now."))
        paused = next(
            row
            for row in api("GET", f"/v1/bots/{bot['id']}/schedules")
            if row["id"] == recurring["id"]
        )
        assert paused["state"] == "paused" and paused["next_run_at"] is None
        result["checks"]["conversational_recurring_create_pause"] = True

        start = wait(
            send(
                "Use my private offline computer to run a Python command that writes started.txt, waits 25 seconds, then writes finished.txt containing 完了. Work in the background so we can keep chatting. Ask for the required computer permission first."
            )
        )
        proposals = api("GET", f"/v1/bots/{bot['id']}/proposals")
        proposal = next(
            p for p in proposals if p["source_task_id"] == start["id"] and p["status"] == "pending"
        )
        approved = api(
            "PATCH",
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            dict(action="accept", expected_revision=proposal["revision"], offline_computer=True),
        )
        work = approved["task_id"]
        until(
            lambda: api("GET", f"/v1/tasks/{work}"),
            lambda t: any(
                e["kind"] == "tool_started" and e.get("data", {}).get("name") == "computer_run"
                for e in t["events"]
            ),
        )
        review = wait(
            send(
                "Meanwhile ask Reviewer to review whether JSON is the right delivery format for our report, using their approved delivery constraint. Return their recommendation here. This is a separate text review; leave the running computer task alone."
            )
        )
        assert "CSV" in review["output"]
        exchanges = api("GET", f"/v1/bots/{bot['id']}/exchanges")
        assert len(exchanges) == 1 and exchanges[0]["status"] == "completed"
        work_final = wait({"id": work})
        assert exchanges[0]["created_at"] < work_final["updated_at"]
        result["checks"]["peer_review_with_background_work"] = True
        result["background_task"] = work_final["id"]
        result["passed"] = True
    finally:
        result["evidence"] = api("GET", "/study/evidence")
        (private / "everyday-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                dict(
                    passed=result["passed"],
                    checks=result["checks"],
                    usage=result["evidence"]["usage"],
                    evidence=str(private),
                )
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--service", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--existing-network", required=False)
    args = parser.parse_args()
    if args.linux:
        linux_supervisor()
    elif args.service:
        linux_service(json.loads(sys.stdin.readline()))
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
            bootstrap=dict(key=key, base_url=profile.base_url),
            lazy_browser=True,
            exercise=exercise,
            port=7091,
            image="bots-durable-computer-20261019:dev",
            name_prefix="bots-everyday-20261019-",
            existing_network=args.existing_network,
            memory="2g",
        )
    else:
        parser.error("Explicit --execute-live and --app required (28 calls maximum).")
