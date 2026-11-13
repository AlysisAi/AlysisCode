"""Opt-in real GLM/Linux: correction during work, durable partial results, Stop.

Synthetic Unicode ledger and delayed file only. Saved Coding Plan, twenty-call
ceiling, no fallback. All evidence is independently read from the real service.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


def linux():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
    from alysis_code.bots.models import BotInput, FileInput
    from alysis_code.bots.workspaces import WorkspaceStore

    config = json.loads(sys.stdin.readline())
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=12 if config.get("single_scenario") else 20,
        output_tokens=2048,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    instances, operations, traces = [], [], []

    class ObservedDesktop(OfflineLinuxDesktop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def call(self, owner, operation, arguments, **kwargs):
            receipt = {"operation": operation, "started_at": time.monotonic(), "ended_at": None}
            operations.append(receipt)
            try:
                result = super().call(owner, operation, arguments, **kwargs)
                receipt["result"] = result
                return result
            finally:
                receipt["ended_at"] = time.monotonic()

    computers = DesktopComputers(
        settings.owner,
        ComputerViews(),
        factory=ObservedDesktop,
        workspaces=WorkspaceStore(Path("/tmp/bots/workspaces")),
    )
    app = create_app(settings, desktop_computers=computers)
    original = app.state.runtime.client_factory

    class TraceClient:
        def __init__(self, client, task_id):
            self.client, self.task_id = client, task_id

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            response = self.client.chat(**kwargs)
            traces.append(
                {
                    "task_id": self.task_id,
                    "content": response.content,
                    "tools": [
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                }
            )
            return response

    app.state.runtime.client_factory = lambda task_id: TraceClient(original(task_id), task_id)
    bot = app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Computer view test",
            purpose="Do exact local data work and keep chatting while it runs.",
        ),
    )
    app.state.store.save_bot(
        settings.owner, BotInput(name="No computer test", purpose="Remain idle.")
    )
    clients = ["東京", "Αθήνα", "Café"]
    rows = [
        (clients[i % len(clients)], str(Decimal(int(uuid4().hex[:4], 16) % 999) / 100))
        for i in range(9)
    ]
    data = io.StringIO()
    writer = csv.writer(data)
    writer.writerow(["client", "amount"])
    writer.writerows(rows)
    source = app.state.store.save_file(
        settings.owner, bot["id"], FileInput(name="ledger.csv", content=data.getvalue())
    )

    @app.get("/study/receipt")
    def receipt():
        return {
            "source": source,
            "operations": operations,
            "traces": traces,
            "computers": [
                {
                    "alive": c.alive(),
                    "workspace_exists": c.workspace.exists(),
                    "started": (c.workspace / "started.txt").exists(),
                    "late": (c.workspace / "late.txt").exists(),
                }
                for c in instances
            ],
            "usage": app.state.store.usage(settings.owner),
        }

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        computers.close()


def exercise(client, bot, other, private, name, *, cancel_only=False, files_only=False):
    def get(path):
        result = client.get(path)
        result.raise_for_status()
        return result.json()

    def until(predicate, seconds=210):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.15)
        raise RuntimeError("Background desktop study deadline reached")

    def wait(task_id):
        return until(
            lambda: (
                t
                if (t := get(f"/v1/tasks/{task_id}"))["status"]
                in {"completed", "failed", "cancelled", "interrupted"}
                else None
            )
        )

    chat = client.post(
        f"/v1/bots/{bot['id']}/chats", json={"title": "Background desktop study"}
    ).json()
    evidence = {"passed": False}

    def send(message):
        reply = client.post(
            f"/v1/chats/{chat['id']}/tasks", json={"message": message, "request_id": uuid4().hex}
        )
        reply.raise_for_status()
        return reply.json()["id"]

    def approve(message):
        request = wait(send(message))
        assert request["status"] == "completed", request["error"]
        matches = [
            p
            for p in get(f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == request["id"]
        ]
        if not matches:
            evidence.setdefault("missing_proposal_replies", []).append(request)
            request = wait(
                send(
                    "No approval card was created. Please use the actual propose_action tool with offline_computer=true and the exact task I just requested. A written promise does not create the card."
                )
            )
            assert request["status"] == "completed", request["error"]
            matches = [
                p
                for p in get(f"/v1/bots/{bot['id']}/proposals")
                if p["source_task_id"] == request["id"]
            ]
        assert len(matches) == 1, "No actual proposal after one corrective follow-up"
        proposal = matches[0]
        assert proposal["offline_computer"] and not proposal["browser_origin"]
        reply = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            json={
                "action": "accept",
                "expected_revision": proposal["revision"],
                "offline_computer": True,
            },
        )
        reply.raise_for_status()
        return reply.json()["task_id"]

    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    try:
        if not cancel_only:
            source = get("/study/receipt")["source"]
            successful = approve(
                "Please use your offline computer to calculate exact totals by client from ledger.csv in my bot files. "
                "Preserve Unicode client names, use decimal arithmetic, sort clients by Unicode code point, and save totals.csv "
                "in my bot files with header client,total and two decimal places. Use computer_save_file to preserve the actual workspace result without transcribing its content. "
                "This is a responsiveness study: after approval first continue in background, then import the library file directly with computer_import_file and make your "
                "first computer_run wait 20 seconds before computing the totals in that same command (timeout 30 seconds). "
                "Stay available for chat during the wait. First propose this exact offline-computer task for approval."
            )

            def command_started():
                status = get(f"/v1/tasks/{successful}")
                assert status["status"] not in {
                    "failed",
                    "cancelled",
                    "interrupted",
                    "completed",
                }, status["error"] or "Task ended before the long command"
                return any(
                    op["operation"] == "run" and op["ended_at"] is None
                    for op in get("/study/receipt")["operations"]
                )

            until(command_started)
            assert get(f"/v1/tasks/{successful}")["status"] == "waiting", (
                "Command stayed on foreground"
            )
            chat_started = time.monotonic()
            reply_id = send(
                "Correction to the running report: export it as revised-totals.csv instead of totals.csv. Keep all other calculation requirements. Apply this correction to the current background task."
            )
            reply = wait(reply_id)
            snapshot = get("/study/receipt")
            assert reply["status"] == "completed", reply
            assert any(
                t["task_id"] == reply_id
                and any(c["name"] == "update_background_task" for c in t["tools"])
                for t in snapshot["traces"]
            )
            assert any(
                op["operation"] == "run" and op["ended_at"] is None for op in snapshot["operations"]
            ), "Chat completed only after command ended"
            evidence["chat_seconds"] = time.monotonic() - chat_started
            evidence["chat"] = reply
            evidence["successful_task"] = wait(successful)
            assert evidence["successful_task"]["status"] == "completed", evidence[
                "successful_task"
            ]["error"]
            result_file = next(
                f for f in get(f"/v1/bots/{bot['id']}/files") if f["name"] == "revised-totals.csv"
            )
            result = get(f"/v1/bots/{bot['id']}/files/{result_file['id']}")
            totals = defaultdict(Decimal)
            for row in csv.DictReader(io.StringIO(source["content"])):
                totals[row["client"]] += Decimal(row["amount"])
            expected = [[key, f"{totals[key]:.2f}"] for key in sorted(totals)]
            actual = list(csv.reader(io.StringIO(result["content"])))
            assert actual == [["client", "total"], *expected], actual
            assert source == get(f"/v1/bots/{bot['id']}/files/{source['id']}"), "Input file changed"
            evidence["artifact"] = result
            assert get(f"/v1/bots/{other['id']}/computer")["state"] == "not_running"

        if not files_only:
            stopped = approve(
                "Now a separate safe cancellation test. After approval, first continue in background. Then use computer_run "
                "with timeout 30 seconds: write started.txt containing started in /workspace, wait 20 seconds, then write "
                "late.txt containing late. Keep all files in /workspace. I will press Stop during the wait. Do not retry or "
                "replace the command if stopped. First propose this exact offline-computer task for approval."
            )
            until(
                lambda: any(
                    c["started"] and not c["late"] for c in get("/study/receipt")["computers"]
                )
            )
            stop_time = time.monotonic()
            cancelled = client.post(f"/v1/tasks/{stopped}/cancel", json={})
            cancelled.raise_for_status()
            evidence["stopped_task"] = wait(stopped)
            assert evidence["stopped_task"]["status"] == "cancelled", evidence["stopped_task"]
            until(
                lambda: all(
                    not c["alive"] and c["workspace_exists"]
                    for c in get("/study/receipt")["computers"]
                ),
                10,
            )
            evidence["stop_seconds"] = time.monotonic() - stop_time
            calls_after_stop = get("/study/receipt")["usage"]["calls"]
            # Independent observation beyond the command's intended write time.
            time.sleep(21)
            final = get("/study/receipt")
            assert all(
                not c["alive"] and c["workspace_exists"] and c["started"] and not c["late"]
                for c in final["computers"]
            )
            assert final["usage"]["calls"] == calls_after_stop
            assert get(f"/v1/bots/{bot['id']}/computer")["state"] == "not_running"
        if files_only:
            names = {c["name"] for t in get("/study/receipt")["traces"] for c in t["tools"]}
            assert {"computer_import_file", "computer_save_file"} <= names, names
            assert get(f"/v1/bots/{bot['id']}/computer")["state"] == "not_running"
        evidence["passed"] = True
    finally:
        evidence["receipt"] = get("/study/receipt")
        (private / "task-reliability-result.json").write_text(
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
    scenario = parser.add_mutually_exclusive_group()
    scenario.add_argument("--cancel-only", action="store_true")
    scenario.add_argument("--files-only", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--port", type=int, default=7091)
    parser.add_argument("--image", default="bots-linux-night-20261019:actions")
    parser.add_argument("--existing-network")
    args = parser.parse_args()
    if args.linux:
        linux()
    elif args.execute_live and args.app:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
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
            args.app.resolve(),
            None,
            study_script=Path(__file__).resolve(),
            bootstrap={
                "key": key,
                "base_url": profile.base_url,
                "single_scenario": args.cancel_only or args.files_only,
            },
            lazy_browser=True,
            exercise=partial(exercise, cancel_only=args.cancel_only, files_only=args.files_only),
            port=args.port,
            image=args.image,
            name_prefix="bots-task-reliability-20261019-",
            existing_network=args.existing_network,
        )
    else:
        parser.error(
            "Use --execute-live and --app for the explicitly authorized bounded model study."
        )
