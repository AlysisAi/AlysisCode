"""Launch isolated native host-access QA; UI is driven manually or with CUA.

Build app .dev/bots-host-preview first. --live uses only saved OpenCode Go
GLM-5.3-flash/low. No Azure, connected apps, paid fallback or stored credentials.
Commands in the printed control file: start, status, quit. Synthetic files/state are retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import uvicorn

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--port", type=int, default=7097)
    parser.add_argument("--electron", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    app_root = root.parent / "Alysis-Code-App-bots-dev"
    run = uuid4().hex[:12]
    folder = root / ".dev/bots-host-access-20261023" / run
    folder.mkdir(parents=True)
    fixture = Path(tempfile.mkdtemp(prefix="alysis-host-qa-"))
    payload = secrets.token_bytes(530013) + "Καλημέρα 東京".encode()
    (fixture / "input.bin").write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    token = secrets.token_urlsafe(40)
    key, url = "scripted", "https://example.invalid"
    if args.live:
        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        config = load_config()
        profile = get_profile(config, "opencode")
        if not profile or profile.base_url != "https://opencode.ai/zen/go/v1":
            raise RuntimeError("Saved Go profile unavailable; no fallback")
        key, url = resolve_profile_api_key(config, profile.name).key, profile.base_url
        if not key:
            raise RuntimeError("Saved Go key unavailable; no fallback")
    settings = Settings(
        folder / "bots.db",
        token,
        key,
        url,
        "glm-5.3-flash" if args.live else "scripted-host",
        reasoning_effort="low",
    )
    clients = {}

    class Model:
        model = "scripted-host"
        supports_tool_calling = True

        def __init__(self):
            self.stage = 0

        def chat(self, **kwargs):
            if kwargs.get("tools") is None:
                decision = (
                    "ready" if kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS else "allow"
                )
                return LLMResponse(
                    json.dumps({"decision": decision, "reason": "Scripted QA review"}),
                    [],
                    {},
                    usage=LLMUsage(1, 1, 2),
                )
            self.stage += 1
            device = runtime.local_computers.devices()[0]["id"]
            if self.stage == 1:
                name, arguments = (
                    "local_command",
                    {
                        "device_id": device,
                        "cwd": str(fixture),
                        "command": "set -eu; set -C; shasum -a 256 input.bin > command.txt; cat command.txt",
                    },
                )
            elif self.stage == 2:
                name, arguments = (
                    "import_local_file",
                    {
                        "device_id": device,
                        "path": str(fixture / "input.bin"),
                        "name": "host-roundtrip.bin",
                    },
                )
            elif self.stage == 3:
                file = store.list_files(settings.owner, bot["id"])[0]
                name, arguments = (
                    "export_local_file",
                    {
                        "device_id": device,
                        "file_id": file["id"],
                        "path": str(fixture / "roundtrip.bin"),
                    },
                )
            else:
                return LLMResponse(
                    "The command and both file transfers returned successful receipts with matching SHA-256 hashes.",
                    [],
                    {},
                    usage=LLMUsage(1, 1, 2),
                )
            return LLMResponse(
                "", [ToolCall(uuid4().hex, name, arguments)], {}, usage=LLMUsage(1, 1, 2)
            )

    app = create_app(
        settings,
        client_factory=None if args.live else lambda task: clients.setdefault(task, Model()),
    )
    runtime, store = app.state.runtime, app.state.store
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    bot = store.save_bot(
        settings.owner,
        BotInput(
            name="Host computer test",
            purpose="Perform only the explicitly requested synthetic host test. Do not access other files or services.",
        ),
    )
    chat = store.create_chat(settings.owner, bot["id"], "Local roundtrip")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("Service did not start")
        time.sleep(0.1)
    build = app_root / ".dev/bots-host-preview"
    # The normal E2E policy intentionally hides windows. This fixture entry only
    # makes its own window visible; all interactions are manual/CUA.
    (build / "host-qa-entry.mjs").write_text(
        "import { app } from 'electron';\napp.on('browser-window-created',(_,win)=>win.once('ready-to-show',()=>{win.setTitle('Alysis Code — Host computer test');win.show();}));\nawait import('./out/main/index.js');\n"
    )
    (build / "package.json").write_text(
        json.dumps(
            {
                "name": "alysis-host-qa",
                "version": "0.0.0",
                "type": "module",
                "main": "host-qa-entry.mjs",
            }
        )
    )
    cli = folder / "engine"
    cli.write_text(
        "#!/bin/sh\nexport PYTHONPATH="
        + shlex.quote(str(root / "src"))
        + "\nexec "
        + shlex.quote(sys.executable)
        + ' -m alysis_code "$@"\n'
    )
    cli.chmod(0o700)
    env = {
        **os.environ,
        "ALYSIS_E2E": "1",
        "ALYSIS_E2E_USER_DATA": str(folder / "app-profile"),
        "ALYSIS_ALLOW_MULTIPLE_INSTANCES": "1",
        "ALYSIS_APP_CLI": str(cli),
        "PYTHONPATH": str(root / "src"),
        "ALYSIS_BOTS_URL": f"http://127.0.0.1:{args.port}",
        "ALYSIS_BOTS_TOKEN": token,
    }
    env.pop("ELECTRON_RUN_AS_NODE", None)
    electron = (
        args.electron
        or app_root / "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
    )
    process = subprocess.Popen(
        [str(electron), str(build)],
        cwd=app_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def log():
        with (folder / "native.log").open("w") as output:
            for line in process.stdout:
                output.write(line.replace(token, "[redacted]").replace(key, "[redacted]"))
                output.flush()

    threading.Thread(target=log, daemon=True).start()
    task = None
    control = folder / "control.txt"
    control.write_text("")

    def commands():
        seen = 0
        while process.poll() is None:
            lines = control.read_text().splitlines()
            yield from lines[seen:]
            seen = len(lines)
            time.sleep(0.2)

    print(
        json.dumps(
            {
                "control": str(control),
                "ready": True,
                "live": args.live,
                "fixture": str(fixture),
                "evidence": str(folder),
                "bot_id": bot["id"],
            }
        ),
        flush=True,
    )
    try:
        for command in commands():
            if command.strip() == "quit":
                break
            if command.strip() == "start":
                task = store.enqueue(
                    settings.owner,
                    chat["id"],
                    TaskInput(
                        request_id=uuid4().hex,
                        message=f"Use my explicitly connected Mac for one synthetic test. Only access {fixture}. First run a shell command there to compute the SHA-256 of input.bin and exclusively create command.txt with the checksum, refusing overwrite. Then import input.bin into this Bot's Library as host-roundtrip.bin. Export that same Library file to {fixture / 'roundtrip.bin'}, without overwriting. Report actual byte counts and hashes from the receipts, including failures honestly. Request the normal local approvals. Do not access other files, apps, accounts, credentials, settings, network services, memory or routines. Do not repeat successful writes. Stop after reporting.",
                    ),
                )
                runtime.wake()
                print(json.dumps({"task_id": task["id"]}), flush=True)
            if command.strip() in {"status", "start"}:
                actual = store.task(settings.owner, task["id"]) if task else None
                files = store.list_files(settings.owner, bot["id"])
                exported = fixture / "roundtrip.bin"
                result = {
                    "status": actual["status"] if actual else "not_started",
                    "output": actual["output"] if actual else "",
                    "error": actual["error"] if actual else None,
                    "usage": store.usage(settings.owner),
                    "library_verified": any(
                        f["sha256"] == expected and f["size_bytes"] == len(payload) for f in files
                    ),
                    "roundtrip_verified": exported.exists() and exported.read_bytes() == payload,
                    "completion_review": bool(
                        actual
                        and any(e["kind"] == "reply_review_started" for e in actual["events"])
                    ),
                    "local_receipts": [
                        e["data"] for e in actual["events"] if e["kind"] == "local_computer_result"
                    ]
                    if actual
                    else [],
                }
                (folder / "result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2)
                )
                print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)
        server.should_exit = True
        thread.join(timeout=20)
        print(json.dumps({"stopped": True, "data_retained": str(folder)}), flush=True)


if __name__ == "__main__":
    main()
