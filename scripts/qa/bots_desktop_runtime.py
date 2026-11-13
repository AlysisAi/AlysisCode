"""Opt-in GLM → real Bots permission → isolated desktop image/action study.

Synthetic native dialog, saved Z.ai Coding Plan only, twelve-call service ceiling.
Credentials travel through the existing host harness in memory/stdin, not files.
"""

from __future__ import annotations

import argparse
import copy
import json
import shlex
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4


def linux(*, readiness_only=False):
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
    from alysis_code.bots.models import BotInput

    config = (
        dict(
            token="synthetic-readiness-unused-token-0000",
            key="unused",
            base_url="https://example.invalid",
            confirmation=True,
        )
        if readiness_only
        else json.loads(sys.stdin.readline())
    )
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=12,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    label, other_label = "CEDAR" + uuid4().hex[:4].upper(), "MAPLE" + uuid4().hex[:4].upper()
    confirmation = bool(config.get("confirmation"))
    reference = "SAVED-" + uuid4().hex[:8].upper()
    workspaces = []
    selection_receipts = {}
    setup_receipts = []

    class RecordedDesktop(OfflineLinuxDesktop):
        def close(self):
            # Record an independently read process result before task-end cleanup
            # removes scratch state. This does not change executor behavior.
            path = self.workspace / "selection.txt"
            if path.exists():
                selection_receipts[str(self.workspace)] = path.read_text()
            super().close()

    def factory(owner, workspace, *, cancel):
        desktop = RecordedDesktop(owner, workspace, cancel=cancel)
        workspaces.append(workspace)
        labels = [label + ":37", other_label + ":21"]
        if int(uuid4().hex[-1], 16) % 2:
            labels.reverse()
        command = (
            "xmessage -geometry 660x220+180+170 -buttons "
            + shlex.quote(",".join(labels))
            + " "
            + shlex.quote(
                "Choose the button requested in the conversation. This is a synthetic desktop test."
            )
            + "; printf '%s' $? > /workspace/selection.txt"
        )
        if confirmation:
            confirm_buttons = ["Confirm:64", "Cancel:21"]
            if int(uuid4().hex[-1], 16) % 2:
                confirm_buttons.reverse()
            # Trusted synthetic application setup, not agent work. A second
            # native window appears elsewhere, requiring a new visual decision.
            command += (
                '; if [ "$(cat /workspace/selection.txt)" = 37 ]; then '
                "xmessage -geometry 600x240+420+330 -buttons "
                + shlex.quote(",".join(confirm_buttons))
                + " "
                + shlex.quote("Review selection: " + label + ". Confirm this selection or cancel.")
                + "; choice_code=$?; printf ',%s' \"$choice_code\" >> /workspace/selection.txt; "
                'if [ "$choice_code" = 64 ]; then xmessage -geometry 720x220+100+110 -buttons Close:0 '
                + shlex.quote("Selection saved: " + label + ". Reference: " + reference)
                + "; fi; fi"
            )
        try:
            launched = desktop.call(
                owner,
                "launch",
                {"command": "(" + command + ") > /workspace/fixture.log 2>&1"},
                cancel=cancel,
            )
            setup = {"command": command, "launch": launched}
            setup_receipts.append(setup)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                setup["status"] = desktop.call(
                    owner, "status", {"process_id": launched["process_id"]}, cancel=cancel
                )
                setup["windows"] = desktop.call(
                    owner,
                    "run",
                    {"command": "xdotool search --onlyvisible --name xmessage"},
                    cancel=cancel,
                )
                if setup["windows"]["exit_code"] == 0:
                    break
                if not setup["status"]["running"]:
                    break
                time.sleep(0.1)
            setup["log"] = (
                (workspace / "fixture.log").read_text()
                if (workspace / "fixture.log").exists()
                else ""
            )
            if setup["windows"]["exit_code"] != 0:
                import subprocess

                setup["window_tree"] = desktop.call(
                    owner, "run", {"command": "xwininfo -root -tree"}, cancel=cancel
                )
                setup["processes"] = subprocess.run(
                    ["ps", "-eo", "pid,ppid,stat,wchan:30,comm"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                raise RuntimeError("Synthetic native dialog did not become visible")
            return desktop
        except BaseException:
            desktop.close()
            raise

    computers = DesktopComputers(settings.owner, ComputerViews(), factory=factory)
    app = create_app(settings, desktop_computers=computers)
    traces = []
    trace_lock = threading.Lock()
    original = app.state.runtime.client_factory

    class TraceClient:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            with trace_lock:
                if len(traces) >= int(config.get("call_limit", 16)):
                    raise RuntimeError("Study call limit reached; no provider call attempted")
                record = {
                    "messages": copy.deepcopy(kwargs["messages"]),
                    "tools": [s["function"]["name"] for s in kwargs["tools"] or []],
                    "confirmed": False,
                    "content": "",
                    "tool_calls": [],
                }
                traces.append(record)
            try:
                response = self.client.chat(**kwargs)
                record.update(
                    confirmed=True,
                    content=response.content,
                    tool_calls=[
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                )
                return response
            except BaseException as exc:
                record["error_type"] = type(exc).__name__
                raise

    app.state.runtime.client_factory = lambda task_id: TraceClient(original(task_id))
    app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Computer view test",
            purpose="Operate the supplied synthetic native dialog using real desktop images and report actual evidence.",
        ),
    )
    app.state.store.save_bot(
        settings.owner,
        BotInput(name="No computer test", purpose="Remain idle for the ownership check."),
    )

    @app.get("/study/instructions")
    def instructions():
        return {"label": label, "confirmation": confirmation}

    @app.get("/study/receipt")
    def receipt():
        saved = dict(selection_receipts)
        for workspace in workspaces:
            if (workspace / "selection.txt").exists():
                saved[str(workspace)] = (workspace / "selection.txt").read_text()
        return {
            "saved": list(saved.values()),
            "allocated": len(workspaces),
            "usage": app.state.store.usage(settings.owner),
            "reference": reference if confirmation else None,
            "setup": setup_receipts,
            "attempted": len(traces),
            "confirmed": sum(t["confirmed"] for t in traces),
        }

    @app.get("/study/traces")
    def trace():
        return traces

    if readiness_only:
        from concurrent.futures import ThreadPoolExecutor

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                for index in range(3):
                    workspace = Path(f"/tmp/native-readiness-{index}")
                    workspace.mkdir()
                    desktop = pool.submit(
                        factory, "readiness", workspace, cancel=lambda: False
                    ).result()
                    desktop.close()
            print(json.dumps({"passed": True, "model_calls": 0, "allocations": 3}), flush=True)
        finally:
            print(json.dumps({"setup": setup_receipts}), flush=True)
        return

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        computers.close()


def exercise(client, bot, other, private, name):
    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def wait(task_id):
        deadline = time.monotonic() + 210
        while time.monotonic() < deadline:
            task = get(f"/v1/tasks/{task_id}")
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return task
            time.sleep(0.3)
        raise RuntimeError("Offline desktop study deadline reached")

    instructions = get("/study/instructions")
    label = instructions["label"]
    confirmation = instructions["confirmation"]
    chat = client.post(
        f"/v1/bots/{bot['id']}/chats", json={"title": "Offline desktop study"}
    ).json()
    evidence = {"passed": False, "label": label}
    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    try:
        request = client.post(
            f"/v1/chats/{chat['id']}/tasks",
            json={
                "message": (
                    f"Use your offline computer to choose the native dialog button labeled {label}. "
                    + (
                        "Then review the selection in the next dialog and click Confirm once. Read and report the saved reference from the final window, leaving that window open. "
                        if confirmation
                        else "Inspect the new screenshot and report what actually happened. "
                    )
                    + "Use actual screenshots and coordinate clicks only, not commands or keyboard shortcuts. First request offline computer permission and wait for approval."
                ),
                "request_id": uuid4().hex,
            },
        )
        request.raise_for_status()
        evidence["request"] = wait(request.json()["id"])
        assert evidence["request"]["status"] == "completed", evidence["request"]["error"]
        proposal = next(
            p
            for p in get(f"/v1/bots/{bot['id']}/proposals")
            if p["source_task_id"] == evidence["request"]["id"]
        )
        assert proposal["offline_computer"] and not proposal["browser_origin"], proposal
        assert get("/study/receipt")["allocated"] == 0
        refused = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            json={"action": "accept", "expected_revision": proposal["revision"]},
        )
        assert refused.status_code == 409
        response = client.patch(
            f"/v1/bots/{bot['id']}/proposals/{proposal['id']}",
            json={
                "action": "accept",
                "expected_revision": proposal["revision"],
                "offline_computer": True,
            },
        )
        response.raise_for_status()
        evidence["action"] = wait(response.json()["task_id"])
        assert evidence["action"]["status"] == "completed", evidence["action"]["error"]
        receipt = get("/study/receipt")
        assert (
            receipt["saved"] == ["37,64" if confirmation else "37"] and receipt["allocated"] == 1
        ), receipt
        if confirmation:
            assert receipt["reference"] in evidence["action"]["output"]
        traces = get("/study/traces")
        tools = [c["name"] for t in traces for c in t["tool_calls"]]
        assert (
            tools.count("computer_click") == (2 if confirmation else 1)
            and "computer_observe" in tools
        ), tools
        assert not {"computer_run", "computer_key", "computer_type"}.intersection(tools), tools
        assert any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for t in traces
            for m in t["messages"]
        )
        assert get(f"/v1/bots/{other['id']}/computer")["state"] == "not_running"
        assert label in evidence["action"]["output"]
        frame = get(f"/v1/bots/{bot['id']}/computer")
        evidence["final_computer_state"] = frame["state"]
        assert frame["state"] == "not_running"
        evidence["passed"] = True
    finally:
        evidence["receipt"] = get("/study/receipt")
        evidence["traces"] = get("/study/traces")
        (private / "desktop-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    "passed": evidence["passed"],
                    "usage": evidence["receipt"]["usage"],
                    "attempted": evidence["receipt"]["attempted"],
                    "confirmed": evidence["receipt"]["confirmed"],
                    "evidence": str(private),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--linux-readiness", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--port", type=int, default=7091)
    parser.add_argument("--image", default="bots-linux-night-20261019:actions")
    parser.add_argument("--existing-network")
    parser.add_argument("--name-prefix", default="bots-linux-night-20261019-")
    parser.add_argument("--call-limit", type=int, choices=range(8, 17), default=16)
    parser.add_argument(
        "--confirmation",
        action="store_true",
        help="Two native windows and a screen-only saved reference",
    )
    args = parser.parse_args()
    if args.linux_readiness:
        linux(readiness_only=True)
    elif args.linux:
        linux()
    elif args.execute_live and args.app:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
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
                "confirmation": args.confirmation,
                "call_limit": args.call_limit,
            },
            lazy_browser=True,
            exercise=exercise,
            port=args.port,
            image=args.image,
            name_prefix=args.name_prefix,
            existing_network=args.existing_network,
        )
    else:
        parser.error("Use --execute-live --app for the explicit bounded live study.")
