"""Explicit operator pilot: real GLM Bots service, cloud workers, no guest keys.

Call start(provider, spec, output, port=7091) from an authenticated operator
process. Credentials remain in that process; only a loopback API is exposed.
The returned UI configuration must travel on stdin, never into evidence files.
"""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

import uvicorn

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.models import BotInput, FileInput
from alysis_code.config import load_config, resolve_profile_api_key
from alysis_code.profiles import get_profile


def start(provider, spec, output: Path, *, port=7091):
    config = load_config()
    profile = get_profile(config, "zai-coding-plan")
    key = resolve_profile_api_key(config, "zai-coding-plan").key
    if profile is None or not key or profile.protocol != "openai_compat":
        raise RuntimeError("Saved Z.ai Coding Plan unavailable; no fallback.")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings = Settings(
        output / "bots.db",
        secrets.token_urlsafe(36),
        key,
        profile.base_url,
        "glm-5.3-flash",
        owner="cloud-jobs-study",
        max_calls=32,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
        output_tokens=4096,
    )
    instances = []

    class RecordingFactory(CloudDesktopFactory):
        def __call__(self, *args, **kwargs):
            instance = super().__call__(*args, **kwargs)
            instances.append(instance)
            return instance

    factory = RecordingFactory(settings.owner, provider, spec, output / "leases")
    app = create_app(settings, cloud_computers=factory)

    @app.get("/study/receipt")
    def receipt():
        computers = []
        for computer in instances:
            status = provider.status(computer.ref).state
            started = False
            if status == "running":
                try:
                    payload = b"".join(
                        provider.download(
                            computer.ref,
                            f"/tmp/alysis-cloud-{computer.identity.allocation_id}/workspace/phase.txt",
                            size=7,
                        )
                    )
                    started = payload == b"started"
                except Exception:
                    pass
            computers.append(
                {"id": computer.ref.resource_id, "state": status, "phase_started": started}
            )
        return {"computers": computers, "usage": app.state.store.usage(settings.owner)}

    bot = app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Cloud pilot",
            purpose="Help with files and tasks using your private computer. Report actual evidence and saved results.",
        ),
    )
    source = "city,amount\nΑθήνα,17.25\n東京,42.50\nZürich,8.75\n"
    app.state.store.save_file(
        settings.owner, bot["id"], FileInput(name="sales.csv", content=source)
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Private study service did not start.")
    ui = {
        "execute_live": True,
        "url": f"http://127.0.0.1:{port}",
        "token": settings.token,
        "bot": bot,
        "evidence": str(output.resolve()),
        "expected_total": "68.50",
    }
    return app, server, thread, factory, ui
