"""Actual object-service outage during real bot runtime/Linux work.

The model is an explicit bounded script fixture. PostgreSQL, Azurite, computer
commands and API handlers are real. No external model or cloud calls are used.
"""

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_bots_backup import databases  # noqa: F401

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cloud_desktop import CloudDesktopFactory, CloudDesktopFiles
from alysis_code.bots.compute import ComputerIdentity, ComputerRef, ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.object_storage_azure import AzureBlobStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.worker_lease import WorkerLease
from alysis_code.bots.workspace_objects import WorkspaceObjects
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, ToolCall


def until(read, accept=bool, seconds=35):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = read()
        if accept(value):
            return value
        time.sleep(0.1)
    raise AssertionError("Storage outage study condition timed out.")


class Script:
    model = "explicit-storage-outage-fixture"
    supports_tool_calling = True

    def __init__(self, calls, *, pause=False):
        self.script, self.pause = calls, pause
        self.calls, self.results = 0, []
        self.ready, self.resume = threading.Event(), threading.Event()

    def chat(self, **kwargs):
        index = self.calls
        self.calls += 1
        self.results = [json.loads(m["content"]) for m in kwargs["messages"] if m["role"] == "tool"]
        if self.pause and index == 1:
            self.ready.set()
            assert self.resume.wait(20)
        if index < len(self.script):
            name, arguments = self.script[index]
            return LLMResponse("", [ToolCall(str(index), name, arguments)], {})
        return LLMResponse("Fixture command finished.", [], {})


@pytest.fixture
def blob_service():
    if os.environ.get("ALYSIS_BOTS_TEST_DOCKER") != "1":
        pytest.skip("Enable actual isolated Linux/storage services explicitly")
    from azure.storage.blob import ContainerClient

    name = "alysis-bots-storage-outage-" + uuid4().hex[:12]
    key = base64.b64encode(os.urandom(32)).decode()
    url = "http://127.0.0.1:7094/outagestudy"
    objects = client = None
    started = False

    def docker(*args, env=None):
        result = subprocess.run(["docker", *args], env=env, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Study storage container operation failed.")
        return result.stdout

    def ready():
        try:
            client.get_container_properties()
            return True
        except Exception:
            return False

    def stop():
        # Exercise endpoint unavailability, not Azurite's unflushed metadata
        # durability under SIGKILL. Keep the same synthetic storage on restart.
        docker("stop", "--time", "10", name)
        assert json.loads(docker("inspect", name))[0]["State"]["Running"] is False

    def start():
        docker("start", name)
        until(ready)

    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            "127.0.0.1:7094:10000",
            "--env",
            "AZURITE_ACCOUNTS",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "mcr.microsoft.com/azure-storage/azurite:3.35.0",
            "azurite-blob",
            "--blobHost",
            "0.0.0.0",
            "--silent",
            "--skipApiVersionCheck",
            env={**os.environ, "AZURITE_ACCOUNTS": "outagestudy:" + key},
        )
        started = True
        client = ContainerClient(
            url,
            "synthetic",
            credential=key,
            retry_total=0,
            connection_timeout=1,
            read_timeout=1,
            logging_enable=False,
        )
        for attempt in range(30):
            try:
                client.create_container()
                break
            except Exception:
                if attempt == 29:
                    raise RuntimeError("Study storage service did not start.") from None
                time.sleep(0.2)
        objects = AzureBlobStorage(url, "synthetic", key, emulator=True)
        yield objects, stop, start, name
    finally:
        if objects:
            objects.close()
        if client:
            client.close()
        if started:
            docker("rm", "--force", name)


def approved(store, owner, chat, bot):
    store.enqueue(owner, chat, TaskInput(message="Synthetic computer task", request_id=uuid4().hex))
    source = store.claim(owner)
    proposals = ProposalStore(store, owner)
    p = proposals.propose(
        source,
        ActionProposal(
            question="Run this synthetic private task?",
            instructions="Verify the saved workspace",
            offline_computer=True,
        ),
    )
    store.finish(owner, source["id"], "completed", content="Fixture approval")
    return proposals.decide(
        bot,
        p["id"],
        ActionDecision(action="accept", expected_revision=p["revision"], offline_computer=True),
    )["task_id"]


@pytest.mark.parametrize("phase", ["checkpoint", "library", "restore", "import"])
def test_actual_storage_outage_preserves_prior_data_and_retires_computer(
    databases,  # noqa: F811 (imported shared fixture)
    blob_service,
    tmp_path,
    phase,
):
    objects, stop_storage, start_storage, storage_name = blob_service
    store = databases(objects=objects)
    owner = "storage-outage-study"
    provider = DockerComputers(
        "desktop-linux",
        namespace_profile=Path(__file__).resolve().parents[1] / "scripts/cloud-dev/seccomp.json",
    )
    spec = ComputerSpec("alysis-bots-shared-state-20261020:qa")
    refs, stores, managers = [], [], []
    fixture = None
    receipt = {
        "passed": False,
        "phase": phase,
        "external_model_calls": 0,
        "kind": "Actual local PostgreSQL/Azurite/Linux/API; scripted model fixture",
        "storage": storage_name,
    }

    def record(current, bot):
        with current.connect() as db:
            row = db.execute(
                "SELECT record FROM computer_allocations WHERE owner=? AND bot_id=?", (owner, bot)
            ).fetchone()
        data = json.loads(row[0])
        ref = ComputerRef(
            data["provider"],
            data["scope"],
            data["resource_id"],
            ComputerIdentity(owner, bot, data["allocation_id"]),
        )
        if ref not in refs:
            refs.append(ref)
        return ref

    def factory(current, name):
        return CloudDesktopFactory(
            owner, provider, spec, tmp_path / name, persist_workspace=True, store=current
        )

    def app(current, script, label):
        settings = Settings(
            current.path,
            "t" * 40,
            "fixture-only",
            "https://example.invalid",
            script.model,
            owner=owner,
            max_calls=10,
        )
        return create_app(
            settings,
            task_store=current,
            cloud_computers=factory(current, label),
            client_factory=lambda _: script,
        )

    try:
        bot = store.save_bot(owner, BotInput(name="Storage", purpose="Keep verified files"))["id"]
        chat = store.create_chat(owner, bot, "Continuous")["id"]
        library_data = "Saved before outage — Αθήνα 東京\n".encode()
        library = store.save_file(
            owner, bot, FileInput(name="original.txt", content=library_data.decode())
        )
        lease = WorkerLease(store, owner)
        worker = lease.acquire()
        desktop = DesktopComputers(
            owner,
            ComputerViews(),
            factory=factory(worker, "baseline"),
            store=worker,
            file_transfer=CloudDesktopFiles(),
        )
        managers.append(desktop)
        try:
            store.enqueue(
                owner, chat, TaskInput(message="Synthetic baseline", request_id=uuid4().hex)
            )
            task = worker.claim(owner)
            baseline = desktop.call(
                task,
                "computer_run",
                {
                    "command": "python3 - <<'PY'\nfrom pathlib import Path\nimport os,hashlib\np=Path('archive/東京');p.mkdir(parents=True);d=os.urandom(8197);(p/'state.bin').write_bytes(d);print(hashlib.sha256(d).hexdigest())\nPY",
                    "timeout": 10,
                },
                EventCancellationToken(threading.Event()),
                approved=True,
            )
            assert baseline["exit_code"] == 0
            workspace_hash = baseline["output"].strip()
            record(worker, bot)
            desktop.release(task["id"], cancelled=False)
            worker.finish(owner, task["id"], "completed", content="Fixture baseline saved")
        finally:
            desktop.close()
            lease.close()
        original_checkpoint = WorkspaceObjects(store).current(owner, bot)
        failed_task = approved(store, owner, chat, bot)
        actions = [("computer_run", {"command": "printf uncommitted > new.txt", "timeout": 10})]
        if phase == "library":
            actions.append(("computer_save_file", {"name": "new.txt"}))
        elif phase == "import":
            actions.append(("computer_import_file", {"file_id": library["id"]}))
        fixture = Script(actions, pause=phase != "restore")
        if phase == "restore":
            stop_storage()
        started = time.monotonic()
        with TestClient(app(store, fixture, "failing-ledger")) as client:
            client.headers["Authorization"] = "Bearer " + "t" * 40
            if phase != "restore":
                assert fixture.ready.wait(20)
                ref = record(store, bot)
                assert provider.status(ref).state == "running"
                stop_storage()
                started = time.monotonic()
                fixture.resume.set()
            state = until(
                lambda: store.task(owner, failed_task),
                lambda row: row["status"] in {"failed", "completed", "interrupted"},
            )
            assert state["status"] == "failed"
            ref = record(store, bot)
            assert ref != refs[0]
            assert "File storage" in state["error"]
            assert "model connection" not in state["error"]
            if phase != "checkpoint":
                failed_tool = actions[-1][0]
                assert any(
                    e["kind"] == "tool_failed" and e["data"]["name"] == failed_tool
                    for e in state["events"]
                )
            until(lambda: provider.status(ref).state == "missing")
            elapsed = round(time.monotonic() - started, 3)
            assert WorkspaceObjects(store).current(owner, bot) == original_checkpoint
            assert [f["id"] for f in store.list_files(owner, bot)] == [library["id"]]
            with store.connect() as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM messages WHERE task_id=? AND role='assistant'",
                        (failed_task,),
                    ).fetchone()[0]
                    == 0
                )
            assert client.get(f"/v1/bots/{bot}/files").status_code == 200
            unavailable = client.get(f"/v1/bots/{bot}/files/{library['id']}/download")
            assert unavailable.status_code == 503
            assert "127.0.0.1" not in unavailable.text
            receipt.update(
                terminal_status=state["status"],
                terminal_error=state["error"],
                retired_while_storage_off_seconds=elapsed,
                download_status_while_off=503,
                original_checkpoint_preserved=True,
                no_false_completion_reply=True,
            )
        start_storage()
        fresh = PostgresStore(
            store._dsn, objects=objects, state_directory=tmp_path / "fresh-controller"
        )
        stores.append(fresh)
        assert fresh.file_data(owner, bot, library["id"]) == library_data
        resumed_task = approved(fresh, owner, chat, bot)
        check = Script(
            [
                (
                    "computer_run",
                    {
                        "command": "test ! -e new.txt && sha256sum archive/東京/state.bin",
                        "timeout": 10,
                    },
                )
            ]
        )
        with TestClient(app(fresh, check, "fresh-ledger")):
            done = until(
                lambda: fresh.task(owner, resumed_task),
                lambda row: row["status"] in {"completed", "failed"},
            )
            record(fresh, bot)
            assert done["status"] == "completed"
            assert check.results[-1]["exit_code"] == 0
            assert check.results[-1]["output"].split()[0] == workspace_hash
        receipt.update(
            passed=True,
            restored_workspace_sha256=workspace_hash,
            library_sha256=hashlib.sha256(library_data).hexdigest(),
            uncommitted_file_absent=True,
            fixture_calls=fixture.calls + check.calls,
        )
    finally:
        if fixture:
            fixture.resume.set()
        for manager in managers:
            manager.close()
        for other in stores:
            other.close()
        receipt["computers"] = [
            {"id": ref.resource_id, "state": provider.status(ref).state} for ref in refs
        ]
        receipt["passed"] = receipt["passed"] and all(
            row["state"] == "missing" for row in receipt["computers"]
        )
        output = os.environ.get("ALYSIS_BOTS_STORAGE_OUTAGE_RECEIPTS")
        if output:
            path = Path(output)
            path.mkdir(parents=True, exist_ok=True)
            (path / (phase + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
        assert all(row["state"] == "missing" for row in receipt["computers"])
