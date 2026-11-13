"""Bounded live GLM recovery after actual offline-computer loss.

The study kills only its own executor after the first successful file export.
No model response, tool result or recovery action is supplied by the harness.
Saved Coding Plan only; ten-call service ceiling; credentials remain in stdin.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import sys
import time
from pathlib import Path
from uuid import uuid4


def linux():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.desktop_linux import OfflineLinuxDesktop
    from alysis_code.bots.models import BotInput

    config = json.loads(sys.stdin.readline())
    settings = Settings(
        Path("/tmp/bots/bots.db"),
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=10,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
    )
    text = "Café — Αθήνα — 東京 " + uuid4().hex[:8].upper()
    instances, receipts, traces = [], [], []
    fault = {}

    def factory(*args, **kwargs):
        executor = OfflineLinuxDesktop(*args, **kwargs)
        instances.append(executor)
        return executor

    class FaultStudy(DesktopComputers):
        def call(self, task, name, arguments, token, **kwargs):
            result = super().call(task, name, arguments, token, **kwargs)
            receipt = result.observation if hasattr(result, "observation") else result
            receipts.append(
                {"tool": name, "arguments": arguments, "result": copy.deepcopy(receipt)}
            )
            if name == "computer_save_file" and result.get("saved") and not fault:
                with self._lock:
                    computer = self._computers[task["bot_id"]]
                    fault.update(
                        {"after_file_id": result["id"], "old_session": computer.generation}
                    )
                    os.kill(computer.executor._process.pid, signal.SIGKILL)
                    computer.executor._process.wait(timeout=3)
                    fault["actual_executor_exited"] = True
            return result

    manager = FaultStudy(settings.owner, ComputerViews(), factory=factory)
    app = create_app(settings, desktop_computers=manager)
    original = app.state.runtime.client_factory

    class TraceClient:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def chat(self, **kwargs):
            response = self.client.chat(**kwargs)
            traces.append(
                {
                    "messages": copy.deepcopy(kwargs["messages"]),
                    "content": response.content,
                    "tool_calls": [
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                }
            )
            return response

    app.state.runtime.client_factory = lambda task_id: TraceClient(original(task_id))
    bot = app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Computer view test",
            purpose="Work with actual offline files and verify results honestly.",
        ),
    )
    app.state.store.save_bot(
        settings.owner, BotInput(name="No computer test", purpose="Remain idle.")
    )

    @app.get("/study/instructions")
    def instructions():
        return {"text": text, "bytes": len(text.encode())}

    @app.get("/study/evidence")
    def evidence():
        return {
            "fault": fault,
            "receipts": receipts,
            "traces": traces,
            "allocated": len(instances),
            "alive": [e.alive() for e in instances],
            "workspaces_present": [e.workspace.exists() for e in instances],
            "files": app.state.store.list_files(settings.owner, bot["id"]),
            "usage": app.state.store.usage(settings.owner),
        }

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        manager.close()


def exercise(client, bot, other, private, name):
    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def wait(task_id):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            task = get(f"/v1/tasks/{task_id}")
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return task
            time.sleep(0.3)
        raise RuntimeError("Recovery study deadline reached")

    instruction = get("/study/instructions")
    chat = client.post(f"/v1/bots/{bot['id']}/chats", json={"title": "Crash recovery study"}).json()
    evidence = {"passed": False, "instruction": instruction}
    print(json.dumps({"ready": True, "container": name, "evidence": str(private)}), flush=True)
    try:
        request = client.post(
            f"/v1/chats/{chat['id']}/tasks",
            json={
                "message": (
                    "Use your offline computer to create checkpoint.txt containing exactly "
                    + json.dumps(instruction["text"], ensure_ascii=False)
                    + " with no trailing newline. Save the actual file to your library. After saving, "
                    "independently read checkpoint.txt from the computer and measure its UTF-8 byte "
                    "length there; report the exact content and byte count you verified. If the "
                    "computer becomes unavailable, preserve the saved file and use supported recovery "
                    "without reconstructing the content from conversation. Explain any interruption "
                    "briefly and do not claim checks you could not complete. First request offline "
                    "computer permission and wait for approval."
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
        assert proposal["offline_computer"] and not proposal["browser_origin"]
        assert get("/study/evidence")["allocated"] == 0
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
        actual = get("/study/evidence")
        assert actual["fault"]["actual_executor_exited"] and actual["allocated"] == 2
        assert not any(actual["alive"]) and not any(actual["workspaces_present"])
        assert len(actual["files"]) == 1
        saved = get(f"/v1/bots/{bot['id']}/files/{actual['fault']['after_file_id']}")
        assert saved["name"] == "checkpoint.txt" and saved["content"] == instruction["text"]
        assert saved["source_task_id"] == evidence["action"]["id"]
        restores = [r for r in actual["receipts"] if r["tool"] == "computer_import_file"]
        assert len(restores) == 1 and restores[0]["result"]["source_file_id"] == saved["id"]
        fresh_session = restores[0]["result"]["computer_session_id"]
        assert fresh_session != actual["fault"]["old_session"]
        checks = [
            r
            for r in actual["receipts"]
            if r["tool"] == "computer_run"
            and r["result"]["computer_session_id"] == fresh_session
            and r["result"]["exit_code"] == 0
        ]
        assert any(
            instruction["text"] in r["result"]["output"]
            and str(instruction["bytes"]) in r["result"]["output"]
            for r in checks
        )
        assert instruction["text"] in evidence["action"]["output"]
        assert str(instruction["bytes"]) in evidence["action"]["output"]
        assert get(f"/v1/bots/{other['id']}/computer") == {"state": "not_running"}
        evidence["passed"] = True
    finally:
        evidence["actual"] = get("/study/evidence")
        (private / "desktop-recovery-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    "passed": evidence["passed"],
                    "usage": evidence["actual"]["usage"],
                    "evidence": str(private),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--port", type=int, default=7091)
    parser.add_argument("--image", default="bots-linux-night-20261019:actions")
    parser.add_argument("--existing-network")
    args = parser.parse_args()
    if args.linux:
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
            bootstrap={"key": key, "base_url": profile.base_url},
            lazy_browser=True,
            exercise=exercise,
            port=args.port,
            image=args.image,
            name_prefix="bots-linux-night-20261019-recovery-",
            existing_network=args.existing_network,
        )
    else:
        parser.error("Use --execute-live --app for the explicit bounded live study.")
