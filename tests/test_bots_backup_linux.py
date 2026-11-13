"""Opt-in real storage/database restore and fresh Linux workspace qualification."""

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import (  # noqa: F401 (shared isolated DB fixture)
    databases,
    dump,
    enable_reviewed_synthetic_database,
    restore,
)

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.backup import create_backup, restore_backup
from alysis_code.bots.cloud_desktop import CloudDesktopFactory, CloudDesktopFiles
from alysis_code.bots.compute import ComputerIdentity, ComputerRef, ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, MemoryInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage, ObjectRef, ObjectStorageError
from alysis_code.bots.object_storage_azure import AzureBlobStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.worker_lease import WorkerLease
from alysis_code.cancellation import EventCancellationToken


@contextmanager
def storage_emulator():
    from azure.storage.blob import ContainerClient

    name = "alysis-bots-backup-storage-" + uuid4().hex[:12]
    key = base64.b64encode(os.urandom(32)).decode()
    url = "http://127.0.0.1:7094/backupstudy"
    env = {**os.environ, "AZURITE_ACCOUNTS": "backupstudy:" + key}
    objects = None
    started = False
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
                "mcr.microsoft.com/azure-storage/azurite:3.35.0",
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
            timeout=30,
        )
        started = True
        with ContainerClient(
            url,
            "synthetic",
            credential=key,
            retry_total=0,
            connection_timeout=1,
            read_timeout=1,
            logging_enable=False,
        ) as client:
            for attempt in range(30):
                try:
                    client.create_container()
                    break
                except Exception:
                    if attempt == 29:
                        raise RuntimeError("Study object service did not start.") from None
                    time.sleep(0.2)
        objects = AzureBlobStorage(url, "synthetic", key, emulator=True)
        yield objects
    finally:
        if objects:
            objects.close()
        if started:
            subprocess.run(
                ["docker", "stop", "--time", "2", name],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )


def test_restore_without_original_storage_uses_fresh_linux_computer(databases, tmp_path):  # noqa: F811
    if os.environ.get("ALYSIS_BOTS_TEST_DOCKER") != "1":
        pytest.skip("Enable actual isolated Linux computers explicitly")
    root = Path(__file__).resolve().parents[1]
    provider = DockerComputers(
        "desktop-linux", namespace_profile=root / "scripts/cloud-dev/seccomp.json"
    )
    owner = "backup-linux-study"
    computers, desktops, leases = [], [], []
    restored = None
    evidence = {
        "passed": False,
        "external_model_calls": 0,
        "cloud_resources": 0,
        "kind": "Actual local PostgreSQL 18, Azurite source, local destination, Linux",
    }

    def computer(store, label):
        lease = WorkerLease(store, owner)
        worker = lease.acquire()
        leases.append(lease)
        factory = CloudDesktopFactory(
            owner,
            provider,
            ComputerSpec("alysis-bots-shared-state-20261020:qa"),
            tmp_path / label,
            persist_workspace=True,
            store=worker,
        )
        factory.reconcile()
        desktop = DesktopComputers(
            owner, ComputerViews(), factory=factory, store=worker, file_transfer=CloudDesktopFiles()
        )
        desktops.append(desktop)
        return worker, lease, desktop

    def task(store, chat):
        store.enqueue(
            owner, chat, TaskInput(message="Verify saved workspace", request_id=uuid4().hex)
        )
        return store.claim(owner)

    def command(store, desktop, task, code):
        result = desktop.call(
            task,
            "computer_run",
            {"command": code, "timeout": 10},
            EventCancellationToken(threading.Event()),
            approved=True,
        )
        record = AllocationLedger(store, owner).get(task["bot_id"])
        computers.append(
            ComputerRef(
                record["provider"],
                record["scope"],
                record["resource_id"],
                ComputerIdentity(owner, task["bot_id"], record["allocation_id"]),
            )
        )
        assert result["exit_code"] == 0
        return result["output"].strip()

    try:
        with storage_emulator() as source_objects:
            source = databases(objects=source_objects)
            bot = source.save_bot(owner, BotInput(name="Δ 東京", purpose="Restore my saved work"))[
                "id"
            ]
            chat = source.create_chat(owner, bot, "Continuous")["id"]
            source.save_memory(
                owner, bot, MemoryInput(title="Preference", content="Ελληνικά 日本語")
            )
            schedule = source.save_schedule(
                owner,
                bot,
                ScheduleInput(
                    name="Check-in",
                    prompt="Review saved files",
                    timing={"kind": "interval", "every_minutes": 60},
                ),
            )
            payload = os.urandom(1048583)
            digest = hashlib.sha256(payload).hexdigest()
            file = source.save_stream(
                owner,
                bot,
                "資料.bin",
                (payload[i : i + 262144] for i in range(0, len(payload), 262144)),
                len(payload),
            )
            worker, lease, desktop = computer(source, "source-ledger")
            first = task(worker, chat)
            workspace_digest = command(
                worker,
                desktop,
                first,
                "python3 - <<'PY'\nfrom pathlib import Path\nimport hashlib,os\np=Path('archive/東京');p.mkdir(parents=True)\nd=os.urandom(4097);(p/'state.bin').write_bytes(d);print(hashlib.sha256(d).hexdigest())\nPY",
            )
            desktop.release(first["id"], cancelled=False)
            worker.finish(owner, first["id"], "completed", content="Verified saved workspace")
            lease.close()
            leases.remove(lease)
            backup = tmp_path / "backup"
            manifest = create_backup(source._dsn, source_objects, backup, dump=dump)
            assert len(manifest["objects"]) == 2
            source.close()
        # The original blob server is now removed. Neither source objects nor
        # source controller scratch are inputs to restore or the fresh computer.
        with pytest.raises(ObjectStorageError):
            next(source_objects.read(ObjectRef.decode(json.dumps(manifest["objects"][0]))))
        destination = databases(empty=True)
        target_objects = LocalObjectStorage(tmp_path / "independent-storage")
        result = restore_backup(destination, target_objects, backup, restore=restore)
        assert result["read_only"] and result["requires_operator_review"]
        enable_reviewed_synthetic_database(destination)
        restored = PostgresStore(
            destination, objects=target_objects, state_directory=tmp_path / "fresh-controller"
        )
        assert hashlib.sha256(restored.file_data(owner, bot, file["id"])).hexdigest() == digest
        assert restored.list_memories(owner, bot)[0]["content"] == "Ελληνικά 日本語"
        assert restored.list_schedules(owner, bot)[0]["id"] == schedule["id"]
        assert restored.task(owner, first["id"])["status"] == "completed"
        worker, lease, desktop = computer(restored, "target-ledger")
        second = task(worker, chat)
        actual = command(worker, desktop, second, "sha256sum archive/東京/state.bin")
        assert actual.split()[0] == workspace_digest
        assert computers[0].resource_id != computers[1].resource_id
        desktop.release(second["id"], cancelled=False)
        worker.finish(
            owner, second["id"], "completed", content="Fresh computer verified exact saved bytes"
        )
        evidence.update(
            passed=True,
            restored_objects=result["objects"],
            original_object_service_removed=True,
            fresh_database=True,
            fresh_controller_scratch=True,
            different_storage_adapter=True,
            workspace_sha256=workspace_digest,
            library_sha256=digest,
            memory_history_schedule_preserved=True,
        )
    finally:
        errors = []
        for desktop in reversed(desktops):
            try:
                desktop.close()
            except Exception:
                errors.append("computer_cleanup")
        for lease in reversed(leases):
            try:
                lease.close()
            except Exception:
                errors.append("lease_cleanup")
        if restored:
            restored.close()
        evidence["computers"] = [
            {"id": ref.resource_id, "state": provider.status(ref).state} for ref in computers
        ]
        evidence["cleanup_errors"] = errors
        evidence["passed"] = (
            evidence["passed"]
            and not errors
            and all(row["state"] == "missing" for row in evidence["computers"])
        )
        receipt = os.environ.get("ALYSIS_BOTS_BACKUP_RECEIPT")
        if receipt:
            Path(receipt).write_text(json.dumps(evidence, indent=2) + "\n")
        assert not errors and all(row["state"] == "missing" for row in evidence["computers"])
