"""Real PostgreSQL + Azurite + isolated Linux recovery qualification.

Run only against a disposable PostgreSQL 18 database via
ALYSIS_BOTS_TEST_POSTGRES_DSN. No model, Azure account or native preview is used.
Emulator credentials exist in memory/environment only. Receipts contain no keys.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from azure.storage.blob import ContainerClient

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.cloud_desktop import CloudDesktopFactory, CloudDesktopFiles
from alysis_code.bots.compute import ComputerIdentity, ComputerRef, ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import ObjectStorageError
from alysis_code.bots.object_storage_azure import AzureBlobStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.retention import ObjectRetention, RetentionPolicy
from alysis_code.bots.worker_lease import WorkerLease, WorkerLeaseLost

IMAGE = "mcr.microsoft.com/azure-storage/azurite:3.35.0"
ROOT = Path(__file__).resolve().parents[2]


def make_store(config, state):
    objects = AzureBlobStorage(config["url"], config["container"], config["key"], emulator=True)
    return PostgresStore(
        os.environ["ALYSIS_BOTS_TEST_POSTGRES_DSN"], objects=objects, state_directory=state
    )


def computer_manager(store, owner, state, config):
    provider = DockerComputers(
        config["context"], namespace_profile=ROOT / "scripts/cloud-dev/seccomp.json"
    )
    factory = CloudDesktopFactory(
        owner, provider, ComputerSpec(config["image"]), state, persist_workspace=True, store=store
    )
    desktop = DesktopComputers(
        owner, ComputerViews(), factory=factory, store=store, file_transfer=CloudDesktopFiles()
    )
    return provider, factory, desktop


def command(desktop, task, command):
    from alysis_code.cancellation import EventCancellationToken

    return desktop.call(
        task,
        "computer_run",
        {"command": command, "timeout": 10},
        EventCancellationToken(threading.Event()),
        approved=True,
    )


def queue(store, owner, chat):
    return store.enqueue(
        owner, chat, TaskInput(message="Synthetic recovery check", request_id=uuid4().hex)
    )


def child(mode):
    config = json.loads(sys.stdin.readline())
    with tempfile.TemporaryDirectory(prefix="bots-shared-child-") as directory:
        store = make_store(config, Path(directory).resolve() / "state")
        lease = WorkerLease(store, config["owner"], ttl=15)
        try:
            worker = lease.acquire()
        except WorkerLeaseLost:
            print(json.dumps({"blocked": True}), flush=True)
            return
        if mode == "contender":
            lease.close()
            raise AssertionError("A second worker unexpectedly acquired ownership")
        task = worker.claim(config["owner"])
        provider, factory, desktop = computer_manager(
            worker, config["owner"], Path(directory).resolve() / "ledger", config
        )
        result = command(
            desktop, task, "sha256sum archive/東京/state.bin; printf discarded > scratch.txt"
        )
        assert result["exit_code"] == 0
        record = AllocationLedger(worker, config["owner"]).get(task["bot_id"])
        # Renew once so the parent can prove contention before expiry. The crash
        # deliberately leaves the VM and database intent, without a clean close.
        lease.renew()
        print(
            json.dumps(
                {"task": task["id"], "record": record, "restored": result["output"].split()[0]}
            ),
            flush=True,
        )
        os._exit(0)


def run(output, config):
    output.mkdir(parents=True, exist_ok=True)
    name = "alysis-bots-shared-storage-" + uuid4().hex[:10]
    account, container = "sharedstate", "synthetic-study"
    key = base64.b64encode(os.urandom(32)).decode()
    config = {
        **config,
        "url": "http://127.0.0.1:7094/" + account,
        "container": container,
        "key": key,
        "owner": "shared-study-" + uuid4().hex,
    }
    env = {**os.environ, "AZURITE_ACCOUNTS": account + ":" + key}
    result = {
        "passed": False,
        "model_calls": 0,
        "services": ["PostgreSQL 18", "Azurite 3.35", "Docker Linux"],
        "native_preview_changed": False,
        "cloud_resources_created": 0,
    }
    store = lease = desktop = None
    allocations = []
    launched = False
    provider = DockerComputers(
        config["context"], namespace_profile=ROOT / "scripts/cloud-dev/seccomp.json"
    )
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--detach",
                "--name",
                name,
                "--publish",
                "127.0.0.1:7094:10000",
                "--env",
                "AZURITE_ACCOUNTS",
                IMAGE,
                "azurite-blob",
                "--blobHost",
                "0.0.0.0",
                "--silent",
                "--skipApiVersionCheck",
            ],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launched = True
        bootstrap = ContainerClient(
            config["url"],
            container,
            credential=key,
            retry_total=0,
            connection_timeout=2,
            read_timeout=2,
            logging_enable=False,
        )
        for attempt in range(30):
            try:
                bootstrap.create_container()
                break
            except Exception:
                if attempt == 29:
                    raise RuntimeError("Storage emulator did not start") from None
                time.sleep(0.2)
        store = make_store(config, output / "controller-a")
        owner = config["owner"]
        bot = store.save_bot(
            owner, BotInput(name="Recovery", purpose="Real shared state verification")
        )["id"]
        chat = store.create_chat(owner, bot, "Continuous")["id"]
        library_data = os.urandom(52428800)
        library = store.save_stream(
            owner,
            bot,
            "maximum.bin",
            (library_data[i : i + 262144] for i in range(0, len(library_data), 262144)),
            len(library_data),
        )
        digest = hashlib.sha256(library_data).hexdigest()
        assert library["sha256"] == digest
        del library_data
        lease = WorkerLease(store, owner)
        worker = lease.acquire()
        lease.start_heartbeat(lambda: None)
        queue(store, owner, chat)
        task = worker.claim(owner)
        _, factory, desktop = computer_manager(worker, owner, output / "ledger-a", config)
        done = command(
            desktop,
            task,
            "python3 - <<'PY'\nimport pathlib,os,hashlib\np=pathlib.Path('archive/東京');p.mkdir(parents=True,exist_ok=True)\nd=os.urandom(4097);(p/'state.bin').write_bytes(d)\nprint(hashlib.sha256(d).hexdigest())\nPY",
        )
        assert done["exit_code"] == 0
        workspace_digest = done["output"].strip()
        record = AllocationLedger(worker, owner).get(bot)
        allocations.append(record)
        desktop.release(task["id"], cancelled=False)
        worker.finish(owner, task["id"], "completed", content="Saved verified workspace")
        lease.close()
        lease = None
        queue(store, owner, chat)
        process = subprocess.Popen(
            [sys.executable, __file__, "--child", "crash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(json.dumps(config) + "\n", timeout=90)
        if process.returncode:
            raise RuntimeError("Crash worker setup failed: " + stderr[-1800:])
        crashed = json.loads(stdout)
        assert crashed["restored"] == workspace_digest
        allocations.append(crashed["record"])
        competing = subprocess.run(
            [sys.executable, __file__, "--child", "contender"],
            input=json.dumps(config) + "\n",
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert competing.returncode == 0 and json.loads(competing.stdout)["blocked"]
        result["competing_process_blocked"] = True
        # Real expiry, no database-clock or lease-row manipulation.
        deadline = time.monotonic() + 22
        while True:
            with store.connect() as db:
                left = db.execute(
                    "SELECT expires-extract(epoch FROM clock_timestamp()) FROM worker_leases WHERE owner=?",
                    (owner,),
                ).fetchone()[0]
            if left <= 0:
                break
            assert time.monotonic() < deadline
            time.sleep(0.25)
        replacement = make_store(config, output / "controller-b")
        lease = WorkerLease(replacement, owner)
        worker = lease.acquire()
        lease.start_heartbeat(lambda: None)
        _, fresh, desktop = computer_manager(worker, owner, output / "fresh-ledger", config)
        fresh.reconcile()
        old = crashed["record"]
        old_ref = ComputerRef(
            old["provider"],
            old["scope"],
            old["resource_id"],
            ComputerIdentity(owner, bot, old["allocation_id"]),
        )
        assert provider.status(old_ref).state == "missing"
        worker.recover(owner)
        assert worker.task(owner, crashed["task"])["status"] == "interrupted"
        assert worker.claim(owner) is None
        result["crashed_task_interrupted_without_replay"] = True
        result["orphan_computer_deleted_before_claim"] = True
        queue(replacement, owner, chat)
        task = worker.claim(owner)
        done = command(desktop, task, "test ! -e scratch.txt && sha256sum archive/東京/state.bin")
        assert done["exit_code"] == 0 and done["output"].split()[0] == workspace_digest
        allocations.append(AllocationLedger(worker, owner).get(bot))
        desktop.release(task["id"], cancelled=False)
        worker.finish(owner, task["id"], "completed", content="Restored only committed work")
        hasher = hashlib.sha256()
        for block in replacement.file_chunks(owner, bot, library["id"]):
            hasher.update(block)
        assert hasher.hexdigest() == digest
        result.update(
            maximum_file_bytes=52428800,
            library_sha256=digest,
            workspace_sha256=workspace_digest,
            restored_from_shared_storage=True,
            cancelled_scratch_discarded=True,
        )
        abandoned = replacement.put_object(owner, bot, [b"abandoned"], 9)
        gc = ObjectRetention(replacement, owner, RetentionPolicy(300, 300, 2, 300))
        assert gc.sweep(now=time.time() + 301)["deleted"] == 1
        try:
            list(replacement.objects.read(abandoned))
        except ObjectStorageError:
            result["abandoned_cloud_object_deleted"] = True
        else:
            raise AssertionError("Abandoned object survived deletion")
        assert replacement.file(owner, bot, library["id"])["sha256"] == digest
        result["passed"] = True
        bootstrap.close()
        replacement.objects.close()
    finally:
        cleanup_errors = []
        if desktop:
            try:
                desktop.close()
            except Exception:
                cleanup_errors.append("desktop cleanup pending")
        if lease:
            try:
                lease.close()
            except Exception:
                cleanup_errors.append("lease release pending")
        if store:
            with store.connect() as db:
                rows = db.execute(
                    "SELECT record FROM computer_allocations WHERE owner=?", (config["owner"],)
                ).fetchall()
            for row in rows:
                record = json.loads(row[0])
                if record.get("resource_id") and all(
                    item["resource_id"] != record["resource_id"] for item in allocations
                ):
                    allocations.append(record)
        result["cleanup_errors"] = cleanup_errors
        # Only exact allocations from this synthetic study are eligible.
        states = []
        for record in allocations:
            ref = ComputerRef(
                record["provider"],
                record["scope"],
                record["resource_id"],
                ComputerIdentity(config["owner"], bot, record["allocation_id"]),
            )
            state = provider.status(ref).state
            if state == "running":
                provider.stop(ref)
                state = provider.status(ref).state
            if state == "stopped":
                provider.delete(ref)
            states.append(provider.status(ref).state)
        result["computer_final_states"] = states
        if store:
            store.objects.close()
        if launched:
            subprocess.run(
                ["docker", "stop", name],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        result["storage_container_removed"] = launched
        (output / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    assert all(state == "missing" for state in states)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=["crash", "contender"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--context", default="desktop-linux")
    parser.add_argument("--image")
    args = parser.parse_args()
    if args.child:
        child(args.child)
    else:
        if args.output is None or args.image is None:
            parser.error("--output and an explicit prepared --image are required")
        run(args.output, {"context": args.context, "image": args.image})
