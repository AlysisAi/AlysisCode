"""Headless real React/HTTP/Linux peer Stop with a blocked synthetic client.

No provider is instantiated. The fixture call ledger is separate from live usage.
The random local API token travels only through stdin and process memory.
Native Electron IPC and the Mac foreground are not exercised.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4


def serve():
    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.models import BotInput, FileInput
    from alysis_code.llm.types import LLMResponse, ToolCall

    config = json.loads(sys.stdin.readline())
    started, exited = threading.Event(), threading.Event()
    first_task = []

    class Fixture:
        model = "blocked-peer-renderer-fixture"
        supports_tool_calling = True

        def __init__(self, task_id):
            self.task_id = task_id

        def chat(self, **kwargs):
            if kwargs["messages"][0]["content"].startswith("You are the receiving peer"):
                assert kwargs["tools"] is None
                started.set()
                try:
                    if not kwargs["cancellation_token"].wait(180):
                        raise RuntimeError("Fixture expected cancellation within its deadline")
                    kwargs["cancellation_token"].throw_if_cancelled()
                finally:
                    exited.set()
                raise AssertionError("Cancelled fixture must not publish a peer reply")
            if not first_task:
                first_task.append(self.task_id)
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "peer-request",
                            "request_peer",
                            {
                                "recipient_bot_id": reviewer["id"],
                                "brief": "Review the selected synthetic plan; preserve Νεφέλη / 東京.",
                                "shared_file_ids": [selected["id"]],
                            },
                        )
                    ],
                    {},
                )
            assert self.task_id != first_task[0], "Cancelled source must never report"
            return LLMResponse("Fixture acknowledgment: this chat remains responsive.", [], {})

    app = create_app(
        Settings(
            Path("/tmp/study/bots.db"),
            config["token"],
            "fixture-no-provider",
            "https://example.invalid",
            Fixture.model,
            owner="peer-renderer-fixture",
            max_calls=4,
        ),
        client_factory=Fixture,
    )
    store, owner = app.state.store, "peer-renderer-fixture"
    source = store.save_bot(
        owner, BotInput(name="Peer Planner", purpose="Coordinate selected work")
    )
    reviewer = store.save_bot(owner, BotInput(name="Peer Reviewer", purpose="Review scoped work"))
    selected = store.save_file(
        owner,
        source["id"],
        FileInput(name="selected-plan.txt", content="Νεφέλη / 東京: four attendees.\n"),
    )
    store.save_file(
        owner, source["id"], FileInput(name="private.txt", content="UNSELECTED_PRIVATE_FIXTURE")
    )

    @app.get("/study/receipt")
    def receipt():
        task = store.task(owner, first_task[0]) if first_task else None
        exchange = app.state.runtime.peers.for_task(first_task[0]) if first_task else None
        return {
            "source": source,
            "reviewer": reviewer,
            "selected_file_id": selected["id"],
            "peer_started": started.is_set(),
            "peer_exited": exited.is_set(),
            "task": task,
            "exchange": app.state.runtime.peers.public(exchange) if exchange else None,
            "conversation": store.conversation(owner, task["chat_id"]) if task else None,
            "fixture_usage": store.usage(owner),
            "live_model_calls": 0,
        }

    uvicorn.run(app, host="0.0.0.0", port=7102, access_log=False, log_level="error")


def host(app: Path):
    import httpx

    if time.time() >= 1792382220 - 120:
        raise RuntimeError("Too close to the authorized 03:57 UTC cutoff")
    root = Path(__file__).resolve().parents[2]
    name = "alysis-peer-20261019-renderer-" + uuid4().hex[:8]
    evidence = root / "reports/peer-cooperation-20261019" / name
    evidence.mkdir(parents=True)
    token = secrets.token_urlsafe(36)
    command = [
        "docker",
        "run",
        "--name",
        name,
        "-i",
        "--init",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus",
        "2",
        "--pids-limit",
        "128",
        "--memory",
        "768m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
        "-p",
        "127.0.0.1:7102:7102",
        "-e",
        "PYTHONPATH=/review/src",
        "-v",
        f"{root}:/review:ro",
        "alysis-peer-20261019:cooperation",
        "python",
        "/review/scripts/qa/bots_peer_renderer.py",
        "--serve-fixture",
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    process.stdin.write((json.dumps({"token": token}) + "\n").encode())
    process.stdin.flush()
    url = "http://127.0.0.1:7102"
    client = httpx.Client(
        base_url=url, headers={"Authorization": "Bearer " + token}, timeout=5, trust_env=False
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError("Isolated fixture service failed to start")
            try:
                response = client.get("/study/receipt")
                response.raise_for_status()
                initial = response.json()
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Isolated fixture service did not become ready") from None
                time.sleep(0.1)
        subprocess.run(
            ["node", str(app / "scripts/qa/bots-peer-service-study.mjs")],
            input=json.dumps(
                {
                    "execute_fixture": True,
                    "url": url,
                    "token": token,
                    "bot": initial["source"],
                    "reviewer": initial["reviewer"],
                    "evidence": str(evidence),
                }
            ).encode(),
            cwd=app,
            check=True,
            timeout=150,
        )
    finally:
        try:
            response = client.get("/study/receipt")
            if response.is_success:
                (evidence / "backend.json").write_text(
                    json.dumps(response.json(), ensure_ascii=False, indent=2) + "\n"
                )
        except httpx.HTTPError:
            pass
        client.close()
        subprocess.run(
            ["docker", "stop", "--time", "10", name], check=True, capture_output=True, timeout=20
        )
        output, error = process.communicate(timeout=15)
        # Fixture logs cannot include a provider key. Do not save request headers.
        (evidence / "service.log").write_bytes(output + error)
        state = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Running}}", name], text=True
        ).strip()
        assert state == "false"
        print(
            json.dumps(
                {
                    "container": name,
                    "stopped": True,
                    "evidence": str(evidence),
                    "live_model_calls": 0,
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-fixture", action="store_true")
    parser.add_argument("--execute-fixture", action="store_true")
    parser.add_argument("--app", type=Path)
    args = parser.parse_args()
    if args.serve_fixture:
        serve()
    elif args.execute_fixture and args.app:
        host(args.app.resolve())
    else:
        parser.error("Use --execute-fixture --app PATH for this provider-free study")
