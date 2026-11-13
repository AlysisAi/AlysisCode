"""Opt-in storage integration study against a real, loopback-only Azurite.

No model calls, Azure deployment or real credentials. A random emulator key lives
only in process/container environments. Fixtures are file data, not fake storage
responses. A controller/database relocation is tested, not database replication.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from azure.storage.blob import ContainerClient

from alysis_code.bots.files import FILE_CHUNK_BYTES, MAX_FILE_BYTES
from alysis_code.bots.models import BotInput, FileInput
from alysis_code.bots.object_storage import LocalObjectStorage, ObjectRef, ObjectStorageError
from alysis_code.bots.object_storage_azure import AzureBlobStorage
from alysis_code.bots.store import Store
from alysis_code.bots.workspace_objects import WorkspaceObjects

IMAGE = "mcr.microsoft.com/azure-storage/azurite:3.35.0"


def refs(store):
    with store.connect() as db:
        return [
            ObjectRef.decode(row[0])
            for row in db.execute(
                "SELECT ref FROM file_objects UNION SELECT ref FROM workspace_objects"
            )
        ]


def cloud_workspace_check(objects, output, provider, spec):
    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.models import TaskInput
    from alysis_code.cancellation import EventCancellationToken

    owner = "object-storage-cloud-study"
    settings = Settings(
        output / "cloud-source.db",
        "t" * 40,
        "unused",
        "https://example.invalid",
        "unused",
        owner=owner,
        max_calls=0,
    )
    allocations = []

    class RecordingProvider:
        def __getattr__(self, name):
            return getattr(provider, name)

        def create(self, identity, spec):
            ref = provider.create(identity, spec)
            allocations.append(ref)
            return ref

    observed = RecordingProvider()

    def service(settings, name):
        factory = CloudDesktopFactory(owner, observed, spec, output / name, persist_workspace=True)
        app = create_app(settings, cloud_computers=factory, object_storage=objects)
        return app.state.store, app.state.runtime.desktop_computers

    store, desktop = service(settings, "cloud-controller-a")
    bot = store.save_bot(
        owner, BotInput(name="Cloud storage check", purpose="Verify restored bytes")
    )["id"]
    token = EventCancellationToken(threading.Event())

    def claim(store):
        chat = store.create_chat(owner, bot, "Storage check")
        queued = store.enqueue(
            owner,
            chat["id"],
            TaskInput(message="Synthetic storage verification", request_id=uuid4().hex),
        )
        claimed = store.claim(owner)
        assert claimed["id"] == queued["id"]
        return claimed

    evidence = {
        "passed": False,
        "mode": "real Azure computer tools, real Azurite object storage, no model",
    }
    current = None
    try:
        current = claim(store)
        result = desktop.call(
            current,
            "computer_run",
            {
                "command": "python3 - <<'PY'\nimport pathlib,os,hashlib\np=pathlib.Path('archive/東京');p.mkdir(parents=True,exist_ok=True)\ndata=os.urandom(4097);(p/'state.bin').write_bytes(data)\nchecksum=hashlib.sha256(data).hexdigest()\npathlib.Path('checksum.txt').write_text(checksum)\nprint(checksum)\nPY",
                "timeout": 10,
            },
            token,
            approved=True,
        )
        assert result["exit_code"] == 0, result
        digest = result["output"].strip()
        assert len(digest) == 64
        desktop.call(current, "computer_save_file", {"name": "checksum.txt"}, token, approved=True)
        desktop.release(current["id"], cancelled=False)
        store.finish(owner, current["id"], "completed")
        first_checkpoint = WorkspaceObjects(store).current(owner, bot)
        assert first_checkpoint is not None
        library = store.list_files(owner, bot)[0]
        assert store.file_data(owner, bot, library["id"]).decode() == digest
        # A separate database path and entirely fresh allocation ledger. No
        # controller-local archive or guest filesystem is copied to the new host.
        replacement = output / "cloud-replacement.db"
        with sqlite3.connect(store.path) as source, sqlite3.connect(replacement) as target:
            source.backup(target)
        from dataclasses import replace

        store, desktop = service(replace(settings, database=replacement), "cloud-controller-b")
        current = claim(store)
        result = desktop.call(
            current,
            "computer_run",
            {
                "command": "sha256sum archive/東京/state.bin; printf 'discarded' > scratch.txt",
                "timeout": 10,
            },
            token,
            approved=True,
        )
        assert result["exit_code"] == 0 and result["output"].startswith(digest)
        desktop.release(current["id"], cancelled=True)
        store.finish(owner, current["id"], "cancelled")
        current = claim(store)
        result = desktop.call(
            current,
            "computer_run",
            {"command": "test ! -e scratch.txt && sha256sum archive/東京/state.bin", "timeout": 10},
            token,
            approved=True,
        )
        assert result["exit_code"] == 0 and result["output"].startswith(digest)
        desktop.release(current["id"], cancelled=True)
        store.finish(owner, current["id"], "cancelled")
        assert store.file_data(owner, bot, library["id"]).decode() == digest
        assert WorkspaceObjects(store).current(owner, bot) == first_checkpoint
        evidence.update(
            passed=True,
            workspace_sha256=digest,
            controller_relocation=True,
            fresh_computer_restore=True,
            cancelled_scratch_discarded=True,
            library_preserved=True,
            model_calls=0,
        )
    finally:
        if current is not None:
            desktop.release(current["id"], cancelled=True)
        evidence["computers"] = [
            {"id": ref.resource_id, "state": provider.status(ref).state} for ref in allocations
        ]
        (output / "cloud-receipt.json").write_text(json.dumps(evidence, indent=2))
    assert all(row["state"] == "missing" for row in evidence["computers"])
    return evidence


def run(output, *, port=7094, cloud=None):
    output.mkdir(parents=True, exist_ok=True)
    name = "alysis-bots-storage-" + uuid4().hex[:12]
    account, container = "botsstorage", "synthetic-study"
    key = base64.b64encode(os.urandom(32)).decode()
    environment = {**os.environ, "AZURITE_ACCOUNTS": account + ":" + key}
    url = f"http://127.0.0.1:{port}/{account}"
    evidence = {
        "passed": False,
        "mode": "real Azurite storage; synthetic data; no model",
        "image": IMAGE,
    }
    launched = False
    objects = None
    try:
        with tempfile.TemporaryDirectory(prefix="alysis-storage-qa-") as scratch:
            scratch = Path(scratch)
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--publish",
                    f"127.0.0.1:{port}:10000",
                    "--env",
                    "AZURITE_ACCOUNTS",
                    "--mount",
                    f"type=bind,src={scratch},dst=/data",
                    IMAGE,
                    "azurite-blob",
                    "--blobHost",
                    "0.0.0.0",
                    "--location",
                    "/data",
                    "--silent",
                    "--skipApiVersionCheck",
                ],
                env=environment,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            launched = True
            bootstrap = ContainerClient(
                url,
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
                        raise RuntimeError("Emulator did not become ready") from None
                    time.sleep(0.2)
            objects = AzureBlobStorage(url, container, key, emulator=True)
            if cloud is not None:
                evidence["cloud"] = cloud_workspace_check(objects, output, *cloud)
            store = Store(output / "source.db")
            bot = store.save_bot(
                "storage-study", BotInput(name="Files", purpose="Portable storage")
            )["id"]
            text = store.save_file(
                "storage-study", bot, FileInput(name="Αθήνα.txt", content="東京\nΚαλημέρα")
            )
            legacy = store.save_bytes("storage-study", bot, "random.bin", os.urandom(1111))
            store = Store(store.path, objects=objects)
            for item in [text, legacy]:
                assert store.externalize_file("storage-study", bot, item["id"]) == item
            payload = os.urandom(3 * FILE_CHUNK_BYTES + 97)
            saved = store.save_stream(
                "storage-study",
                bot,
                "résumé.bin",
                (
                    payload[i : i + FILE_CHUNK_BYTES]
                    for i in range(0, len(payload), FILE_CHUNK_BYTES)
                ),
                len(payload),
            )
            empty = store.save_bytes("storage-study", bot, "empty", b"")
            assert store.file_data("storage-study", bot, empty["id"]) == b""
            maximum_digest = hashlib.sha256()

            def maximum_chunks():
                for _ in range(MAX_FILE_BYTES // FILE_CHUNK_BYTES):
                    chunk = os.urandom(FILE_CHUNK_BYTES)
                    maximum_digest.update(chunk)
                    yield chunk

            maximum = store.save_stream(
                "storage-study", bot, "maximum.bin", maximum_chunks(), MAX_FILE_BYTES
            )
            read_digest = hashlib.sha256()
            for chunk in store.file_chunks("storage-study", bot, maximum["id"]):
                read_digest.update(chunk)
            assert read_digest.hexdigest() == maximum_digest.hexdigest() == maximum["sha256"]
            evidence["maximum_file_bytes"] = MAX_FILE_BYTES
            # Range read: exact arbitrary offset, all remaining bytes verified.
            assert (
                b"".join(
                    store.file_chunks(
                        "storage-study", bot, saved["id"], offset=FILE_CHUNK_BYTES - 1
                    )
                )
                == payload[FILE_CHUNK_BYTES - 1 :]
            )
            baseline = WorkspaceObjects(store).import_baseline(
                "storage-study", bot, [b"workspace baseline"], 18
            )
            subprocess.run(
                ["docker", "restart", name],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for attempt in range(30):
                try:
                    assert b"".join(objects.read(baseline)) == b"workspace baseline"
                    break
                except ObjectStorageError:
                    if attempt == 29:
                        raise
                    time.sleep(0.2)
            relocated = output / "relocated.db"
            with sqlite3.connect(store.path) as source, sqlite3.connect(relocated) as target:
                source.backup(target)
            new_controller = Store(
                relocated, objects=AzureBlobStorage(url, container, key, emulator=True)
            )
            assert (
                b"".join(new_controller.file_chunks("storage-study", bot, saved["id"])) == payload
            )
            assert new_controller.file("storage-study", bot, text["id"]) == text
            new_controller.objects.close()
            # A different adapter/storage root reads the same DB pointers after
            # verified copy: migration does not depend on Azure snapshot IDs.
            alternate = LocalObjectStorage(output / "alternate-storage")
            for ref in refs(store):
                alternate.copy_from(objects, ref)
            alternate_controller = Store(relocated, objects=alternate)
            assert (
                b"".join(alternate_controller.file_chunks("storage-study", bot, saved["id"]))
                == payload
            )
            assert WorkspaceObjects(alternate_controller).current("storage-study", bot) == baseline
            # Corrupt a synthetic cloud object deliberately, then prove it is
            # rejected rather than returned under the original integrity receipt.
            with store.connect() as db:
                ref = ObjectRef.decode(
                    db.execute(
                        "SELECT ref FROM file_objects WHERE file_id=?", (saved["id"],)
                    ).fetchone()[0]
                )
            bootstrap.upload_blob(ref.key, b"x" * len(payload), overwrite=True)
            try:
                next(store.file_chunks("storage-study", bot, saved["id"]))
            except ObjectStorageError:
                evidence["corruption_rejected"] = True
            else:
                raise AssertionError("Corrupt cloud object was accepted")
            evidence.update(
                passed=True,
                files=5,
                objects=len(refs(store)),
                bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                storage_restart=True,
                controller_relocation=True,
                cross_adapter_restore=True,
                model_calls=0,
            )
            bootstrap.close()
    except BaseException as exc:
        evidence["error_type"] = type(exc).__name__
        raise
    finally:
        if objects is not None:
            objects.close()
        if launched:
            result = subprocess.run(
                ["docker", "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            evidence["container_removed"] = result.returncode == 0
        (output / "receipt.json").write_text(json.dumps(evidence, indent=2))
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-emulator", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=7094)
    parser.add_argument("--azure-stdin", action="store_true")
    args = parser.parse_args()
    cloud = None
    if args.azure_stdin:
        import sys

        from alysis_code.bots.compute import ComputerSpec
        from alysis_code.bots.compute_azure import AzureComputers

        config = json.load(sys.stdin)
        provider = AzureComputers(**config["provider"], access_token=lambda: config["token"])
        cloud = (provider, ComputerSpec(config["image"]))
    try:
        print(json.dumps(run(args.output, port=args.port, cloud=cloud), indent=2))
    finally:
        if cloud is not None:
            cloud[0].close()
