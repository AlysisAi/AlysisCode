"""Native Mac viewer against the actual cloud RPC worker, with a synthetic page.

Default uses the Docker provider and an explicitly prepared image. UI interaction
is manual/CUA. --build prepares an isolated native preview first.
--live uses saved OpenCode Go GLM-5.3-flash/low only. The printed
control file accepts start/status/quit. No credentials are written to evidence.
An authenticated operator can also supply a provider/spec to run_study in memory.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import uvicorn

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

URL = "http://127.0.0.1:8099/"


class StudyFactory(CloudDesktopFactory):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, browser_enabled=True, preview_urls=(URL,), **kwargs)
        self.created = []

    def _acquire(self, *args, **kwargs):
        surface = super()._acquire(*args, **kwargs)
        if surface.ref not in self.created:
            self.created.append(surface.ref)
            try:
                for name in ("bots_cloud_control_site.py", "bots_human_control.py"):
                    source = Path(__file__).with_name(name).read_bytes()
                    self.provider.upload(surface.ref, "/tmp/" + name, [source], size=len(source))
                code = "import subprocess;subprocess.Popen(['/opt/alysis/.venv/bin/python','/tmp/bots_cloud_control_site.py'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,env={'PATH':'/usr/bin:/bin'});print('started')"
                receipt = self.provider.run(
                    surface.ref, ["/opt/alysis/.venv/bin/python", "-c", code], timeout=10
                )
                if receipt.exit_code != 0:
                    raise RuntimeError("Fixture did not start")
                for _attempt in range(20):
                    receipt = self.provider.run(
                        surface.ref,
                        [
                            "/opt/alysis/.venv/bin/python",
                            "-c",
                            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8099/state').read()",
                        ],
                        timeout=10,
                    )
                    if receipt.exit_code == 0:
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Synthetic page did not become ready")
            except BaseException:
                surface.close()
                raise
        return surface


def run_study(provider, spec, *, live=False, electron=None):
    root = Path(__file__).resolve().parents[2]
    app_root = root.parent / "Alysis-Code-App-bots-dev"
    build = app_root / ".dev/bots-cloud-control-preview"
    if not (build / "out/main/index.js").is_file():
        raise RuntimeError("Build the isolated preview with --build first")
    folder = root / ".dev/bots-cloud-control-20261023" / uuid4().hex[:12]
    folder.mkdir(parents=True)
    token, key, endpoint = secrets.token_urlsafe(40), "unused", "https://example.invalid"
    if live:
        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        config = load_config()
        profile = get_profile(config, "opencode")
        if not profile or profile.base_url != "https://opencode.ai/zen/go/v1":
            raise RuntimeError("Saved Go profile unavailable; no fallback")
        key, endpoint = resolve_profile_api_key(config, profile.name).key, profile.base_url
        if not key:
            raise RuntimeError("Saved Go credential unavailable; no fallback")
    settings = Settings(
        folder / "bots.db",
        token,
        key,
        endpoint,
        "glm-5.3-flash" if live else "scripted-cloud-control",
        reasoning_effort="low",
        browser_enabled=True,
    )
    views = ComputerViews()
    factory = StudyFactory(settings.owner, provider, spec, folder / "leases")
    browser = BrowserComputers(settings.owner, views, factory.browser, interactive_previews=True)

    class Model:
        model = "scripted-cloud-control"
        supports_tool_calling = True

        def __init__(self):
            self.step = 0

        def chat(self, **kwargs):
            if kwargs.get("tools") is None:
                decision = (
                    "ready" if kwargs["messages"][0]["content"] == REVIEW_INSTRUCTIONS else "allow"
                )
                return LLMResponse(
                    json.dumps({"decision": decision, "reason": "Synthetic review"}),
                    [],
                    {},
                    usage=LLMUsage(1, 1, 2),
                )
            self.step += 1
            call = {
                1: ("browser_navigate", {"url": URL}),
                2: (
                    "request_computer_control",
                    {"reason": "Complete this synthetic verification, then give control back."},
                ),
                3: ("browser_observe", {}),
            }.get(self.step)
            return LLMResponse(
                ""
                if call
                else "The user completed verification once. I inspected the page after control returned.",
                [ToolCall(uuid4().hex, *call)] if call else [],
                {},
                usage=LLMUsage(1, 1, 2),
            )

    clients = {}
    app = create_app(
        settings,
        browser_computers=browser,
        computer_views=views,
        client_factory=None if live else lambda task: clients.setdefault(task, Model()),
    )
    runtime, store = app.state.runtime, app.state.store
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    bot = store.save_bot(
        settings.owner,
        BotInput(
            name="Cloud control test",
            purpose="Perform only the requested synthetic human handoff. Ask for human input with request_computer_control; never ask for credentials in chat.",
        ),
    )
    chat = store.create_chat(settings.owner, bot["id"], "Cloud handoff")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=7097, access_log=False, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("QA service failed to start")
        time.sleep(0.1)
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
        "ALYSIS_BOTS_URL": "http://127.0.0.1:7097",
        "ALYSIS_BOTS_TOKEN": token,
    }
    env.pop("ELECTRON_RUN_AS_NODE", None)
    process = subprocess.Popen(
        [
            str(
                electron
                or app_root / "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
            ),
            str(build),
        ],
        env=env,
        cwd=app_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )

    def log():
        with (folder / "native.log").open("w") as output:
            for line in process.stdout:
                output.write(line.replace(token, "[redacted]").replace(key, "[redacted]"))
                output.flush()

    threading.Thread(target=log, daemon=True).start()
    control = folder / "control.txt"
    control.write_text("")
    print(
        json.dumps({"control": str(control), "bot_id": bot["id"], "ready": True, "live": live}),
        flush=True,
    )
    seen, task, last_page = 0, None, {}
    try:
        while process.poll() is None:
            lines = control.read_text().splitlines()
            for command in lines[seen:]:
                if command == "quit":
                    return
                if command == "start":
                    task = store.enqueue(
                        settings.owner,
                        chat["id"],
                        TaskInput(
                            message=f"Open {URL} and ask me to complete the synthetic verification myself using Take control. After I give control back, inspect and report whether it completed without submitting again. Do not access other sites, accounts or files.",
                            request_id=uuid4().hex,
                        ),
                    )
                    runtime.wake()
                    print(json.dumps({"task_id": task["id"]}), flush=True)
                if command == "status":
                    actual = store.task(settings.owner, task["id"]) if task else None
                    page = last_page
                    for ref in factory.created:
                        if provider.status(ref).state == "running":
                            receipt = provider.run(
                                ref,
                                [
                                    "/opt/alysis/.venv/bin/python",
                                    "-c",
                                    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8099/state').read().decode())",
                                ],
                                timeout=10,
                            )
                            if receipt.exit_code == 0:
                                page = json.loads(receipt.stdout)
                                last_page = page
                    result = {
                        "task_id": task["id"] if task else None,
                        "status": actual["status"] if actual else "not_started",
                        "output": actual["output"] if actual else "",
                        "error": actual["error"] if actual else None,
                        "page": page,
                        "fixture_verified": page
                        == {
                            "submissions": 1,
                            "unicode_name": True,
                            "private_entered": True,
                        },
                        "usage": store.usage(settings.owner),
                        "completion_review": bool(
                            actual
                            and any(e["kind"] == "reply_review_started" for e in actual["events"])
                        ),
                        "computers": [
                            {"id": ref.resource_id, "state": provider.status(ref).state}
                            for ref in factory.created
                        ],
                    }
                    (folder / "result.json").write_text(json.dumps(result, indent=2))
                    print(json.dumps(result), flush=True)
            seen = len(lines)
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)
        server.should_exit = True
        thread.join(timeout=30)
        browser.close()
        states = [provider.status(ref).state for ref in factory.created]
        print(
            json.dumps(
                {
                    "stopped": not thread.is_alive()
                    and all(state == "missing" for state in states),
                    "data_retained": str(folder),
                    "computers": states,
                }
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--image", default="alysis-bots-cloud-control-20261023:qa")
    parser.add_argument("--electron", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.build:
        app_root = root.parent / "Alysis-Code-App-bots-dev"
        build = app_root / ".dev/bots-cloud-control-preview"
        build.mkdir(parents=True, exist_ok=True)
        (build / "vite.config.ts").write_text(
            "import config from '../../electron.vite.config';\n"
            "import { resolve } from 'node:path';\n"
            "const root=resolve(__dirname,'out');\n"
            "export default {...config,\n"
            "main:{...config.main,build:{...config.main?.build,outDir:resolve(root,'main')}},\n"
            "preload:{...config.preload,build:{...config.preload?.build,outDir:resolve(root,'preload')}},\n"
            "renderer:{...config.renderer,build:{...config.renderer?.build,outDir:resolve(root,'renderer')}}};\n"
        )
        (build / "package.json").write_text(
            json.dumps(
                {
                    "name": "alysis-cloud-control-qa",
                    "version": "0.0.0",
                    "type": "module",
                    "main": "qa-entry.mjs",
                }
            )
        )
        (build / "qa-entry.mjs").write_text(
            "import { app } from 'electron';\n"
            "app.on('browser-window-created',(_,win)=>win.once('ready-to-show',()=>{"
            "win.setTitle('Alysis Code — Cloud control test');win.show();}));\n"
            "await import('./out/main/index.js');\n"
        )
        with (build / "build.log").open("w") as output:
            subprocess.run(
                [
                    "node",
                    "node_modules/electron-vite/bin/electron-vite.js",
                    "build",
                    "--config",
                    str(build / "vite.config.ts"),
                ],
                cwd=app_root,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
    provider = DockerComputers(
        "desktop-linux", namespace_profile=root / "scripts/cloud-dev/seccomp.json"
    )
    run_study(provider, ComputerSpec(args.image, cpu=2), live=args.live, electron=args.electron)


if __name__ == "__main__":
    main()
