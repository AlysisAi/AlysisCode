"""Explicit Azure browser/workspace study, synthetic site, real operator provider.

No tokens are accepted by agent tools or saved in evidence. Call checks(provider,
spec, output) from an authenticated controller. Live model stage is separate.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.cloud_desktop import CloudDesktopFactory, CloudDesktopFiles
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken

URL = "http://127.0.0.1:8767/"
CSV = "city,amount\nΑθήνα,17.25\n東京,42.50\nZürich,8.75\n".encode()


class StudyFactory(CloudDesktopFactory):
    def __init__(self, *args, **kwargs):
        super().__init__(
            *args, browser_enabled=True, persist_workspace=True, preview_urls=(URL,), **kwargs
        )
        self.created = []
        self.executors = []

    def _acquire(self, *args):
        surface = super()._acquire(*args)
        if surface.ref not in self.created:
            self.created.append(surface.ref)
            self.executors.append(surface._computer)
            provider, ref = surface.provider, surface.ref
            script = Path(__file__).with_name("bots_cloud_browser_site.py").read_bytes()
            provider.upload(ref, "/tmp/alysis-study-site.py", [script], size=len(script))
            setup = """import pathlib,subprocess,os
root=pathlib.Path('/tmp/alysis-browser-study');root.mkdir(mode=0o700);os.chown(root,1000,1000)
(root/'sales.csv').write_bytes(bytes.fromhex(CSV_HEX));os.chown(root/'sales.csv',1000,1000)
os.chmod('/tmp/alysis-study-site.py',0o644)
subprocess.Popen(['/opt/alysis/.venv/bin/python','-I','/tmp/alysis-study-site.py'],user=1000,group=1000,extra_groups=[],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,env={'PATH':'/usr/bin:/bin'})
print('started')""".replace("CSV_HEX", repr(CSV.hex()))
            receipt = provider.run(
                ref, ["/opt/alysis/.venv/bin/python", "-I", "-c", setup], timeout=20
            )
            if receipt.exit_code != 0:
                surface.close()
                raise RuntimeError("Synthetic study site failed to start.")
        return surface


def checks(provider, spec, output):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = "cloud-browser-checks"
    store = Store(output / "bots.db")
    bot = store.save_bot(owner, BotInput(name="Browser checks", purpose="Synthetic verification"))
    chat = store.create_chat(owner, bot["id"], "Browser checks")
    task = store.enqueue(
        owner, chat["id"], TaskInput(message="Synthetic boundary checks", request_id=uuid4().hex)
    )
    assert store.claim(owner)["id"] == task["id"]
    views = ComputerViews()
    factory = StudyFactory(owner, provider, spec, output / "leases", store=store)
    browser = BrowserComputers(owner, views, factory.browser, store=store)
    desktop = DesktopComputers(
        owner, views, factory=factory, store=store, file_transfer=CloudDesktopFiles()
    )
    token = EventCancellationToken(threading.Event())
    evidence = {"passed": False, "model_calls": 0}

    def call(tool_name, **args):
        return browser.call(task, tool_name, args, token, approved_origins=(URL,), image_input=True)

    def node(view, identifier):
        return next(
            n["node"]
            for n in view["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == identifier
        )

    try:
        public = call("browser_navigate", url="https://example.com/")
        assert "Example Domain" in json.dumps(public)
        evidence["public_page"] = "https://example.com/"
        root = call("browser_navigate", url=URL)
        download = call(
            "browser_download",
            source_id=root["source_id"],
            node=node(root, "csv"),
            name="remote.csv",
        )
        assert download["sha256"] == hashlib.sha256(CSV).hexdigest()
        evidence["download"] = download
        root = call("browser_observe")
        uploaded = call(
            "browser_upload",
            source_id=root["source_id"],
            node=node(root, "file"),
            file_id=download["id"],
        )
        assert "Saved upload.bin" in json.dumps(uploaded)
        actual = b"".join(
            provider.download(
                factory.created[0], "/tmp/alysis-browser-study/upload.bin", size=len(CSV)
            )
        )
        assert actual == CSV
        evidence["upload_verified"] = True
        root = call("browser_observe")
        call("browser_click", source_id=root["source_id"], node=node(root, "review"))
        pages = call("browser_pages")
        review = next(p for p in pages["pages"] if p["url"].endswith("/review"))
        view = call("browser_switch_page", source_id=pages["source_id"], page_id=review["page_id"])
        try:
            browser.call(
                task,
                "browser_type",
                dict(source_id=view["source_id"], node=node(view, "note"), text="must not write"),
                token,
                approved_origins=("https://example.com",),
            )
        except ValueError as exc:
            assert "approval" in str(exc)
        else:
            raise AssertionError("Unapproved origin accepted")
        view = call(
            "browser_type", source_id=view["source_id"], node=node(view, "note"), text="Νεφέλη 東京"
        )
        call("browser_click", source_id=view["source_id"], node=node(view, "save"))
        screen = call("browser_screenshot")
        (output / "browser.png").write_bytes(
            __import__("base64").b64decode(screen.image_url.split(",", 1)[1])
        )
        evidence["tabs_origin_gate_screenshot"] = True
        evidence["imported"] = desktop.call(
            task, "computer_import_file", {"file_id": download["id"]}, token, approved=True
        )
        assert len(factory.created) == 1
        result = desktop.call(
            task,
            "computer_run",
            {
                "command": "mkdir -p 'nested/東京'; printf 'restored Ω' > 'nested/東京/saved.txt'; python3 -c \"import csv,decimal;print(sum(decimal.Decimal(r['amount']) for r in csv.DictReader(open('remote.csv'))))\"",
                "timeout": 20,
            },
            token,
            approved=True,
        )
        assert result["exit_code"] == 0, result
        assert "68.50" in result["output"]
        evidence["same_vm"] = factory.created[0].resource_id
        evidence["command"] = result
        browser.release(task["id"], cancelled=False)
        desktop.release(task["id"], cancelled=False)
        assert provider.status(factory.created[0]).state == "missing"
        store.finish(owner, task["id"], "completed", content="Direct tool checks completed.")
        task2 = store.enqueue(
            owner, chat["id"], TaskInput(message="Restore saved files", request_id=uuid4().hex)
        )
        assert store.claim(owner)["id"] == task2["id"]
        restored = desktop.call(
            task2,
            "computer_run",
            {"command": "cat 'nested/東京/saved.txt'; sha256sum remote.csv", "timeout": 20},
            token,
            approved=True,
        )
        assert restored["exit_code"] == 0, restored
        assert "restored Ω" in restored["output"]
        assert hashlib.sha256(CSV).hexdigest() in restored["output"]
        assert restored["workspace_reopened"] is True
        assert len(factory.created) == 2
        evidence["restored"] = restored
        desktop.release(task2["id"], cancelled=False)
        store.finish(owner, task2["id"], "completed", content="Restoration verified.")
        evidence["passed"] = True
    except Exception as exc:
        import traceback

        evidence["error"] = str(exc)
        evidence["traceback"] = traceback.format_exc()
        raise
    finally:
        browser.close()
        desktop.close()
        evidence["computers"] = [
            {"id": ref.resource_id, "state": provider.status(ref).state} for ref in factory.created
        ]
        (output / "receipt.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return evidence


def start(provider, spec, output, *, port=7092, max_calls=32):
    """Start a bounded real Coding Plan study without writing credentials."""
    import secrets
    import tarfile
    import time

    import uvicorn

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.config import load_config, resolve_profile_api_key
    from alysis_code.profiles import get_profile

    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = load_config()
    profile = get_profile(config, "zai-coding-plan")
    key = resolve_profile_api_key(config, "zai-coding-plan").key
    if not profile or not key or profile.protocol != "openai_compat":
        raise RuntimeError("Saved Coding Plan unavailable; no fallback.")
    settings = Settings(
        output / "bots.db",
        secrets.token_urlsafe(36),
        key,
        profile.base_url,
        "glm-5.3-flash",
        owner="cloud-browser-live",
        max_calls=max_calls,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
        output_tokens=4096,
    )
    factory = StudyFactory(settings.owner, provider, spec, output / "leases")
    app = create_app(settings, cloud_computers=factory)
    bot = app.state.store.save_bot(
        settings.owner,
        BotInput(
            name="Cloud browser demo",
            purpose="Help with real web research and file tasks on your private cloud computer. Verify saved results.",
        ),
    )

    @app.get("/study/receipt")
    def receipt():
        digest = None
        for file in factory.leases.root.rglob("candidate.tar"):
            with tarfile.open(file, "r:") as archive:
                try:
                    member = archive.getmember("archive/東京/state.bin")
                except KeyError:
                    continue
                if member.isfile() and member.size == 32:
                    with archive.extractfile(member) as stream:
                        digest = hashlib.sha256(stream.read(32)).hexdigest()
        return {
            "computers": [
                {
                    "id": instance.ref.resource_id,
                    "state": provider.status(instance.ref).state,
                    "task_id": instance._task_id,
                }
                for instance in factory.executors
            ],
            "checkpoint_state_sha256": digest,
            "usage": app.state.store.usage(settings.owner),
        }

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Study service did not start.")
    return (
        app,
        server,
        thread,
        factory,
        {
            "execute_live": True,
            "url": f"http://127.0.0.1:{port}",
            "token": settings.token,
            "bot": bot,
            "evidence": str(output.resolve()),
            "expected_total": "68.50",
            "site": URL,
        },
    )
